import asyncio
import json
import signal
import socket
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from conftest import FakeClient, FakeSession, FakeSlackMcp, FakeTokens, call_tool, env, said
from fastapi.testclient import TestClient

from chiefofstaff.__main__ import LAUNCH_FILE, Server, running_instance
from chiefofstaff.bus import Bus
from chiefofstaff.runtime import Runtime
from chiefofstaff.sources.slack import SlackSource
from chiefofstaff.store import Store
from chiefofstaff.updates import installed
from chiefofstaff.web import create_app

TOKEN = "launch-token"
ORIGIN = {"origin": "http://127.0.0.1:8765"}


@pytest.fixture
def app_client(tmp_path):
    client = FakeClient()

    def factory(store, bus):
        async def notifier(title, text):
            pass

        runtime = Runtime(store, bus, backend_factory=lambda settings: client, notifier=notifier)
        runtime.slack = SlackSource(store, runtime, tokens=FakeTokens(""), mcp=FakeSlackMcp())
        return runtime

    app = create_app(tmp_path / "data", TOKEN, 8765, runtime_factory=factory)
    with TestClient(app, base_url="http://127.0.0.1:8765") as http:
        http.fake = client
        yield http


def sign_in(http):
    """As the page does: the token checked, then sent with every request."""
    assert http.post("/api/auth", json={"token": TOKEN}, headers=ORIGIN).status_code == 200
    http.headers["X-Chief-Key"] = TOKEN


def test_api_needs_the_launch_token_and_a_trusted_origin(app_client):
    assert app_client.get("/api/state").status_code == 401
    assert app_client.post("/api/auth", json={"token": "nope"}, headers=ORIGIN).status_code == 401
    signed = app_client.post("/api/auth", json={"token": TOKEN}, headers=ORIGIN)
    assert signed.status_code == 200 and TOKEN not in signed.headers.get("set-cookie", ""), (
        "never a cookie: a browser sends those to every local server"
    )
    assert app_client.get("/api/state").status_code == 401, "the token comes with each request"
    assert app_client.get("/api/state", cookies={"chiefofstaff_8765": TOKEN}).status_code == 401
    # In the address only for the event stream (a browser cannot send it a header): never for a
    # link or an image, whose address would land in the history or a copied link.
    assert app_client.get(f"/api/state?key={TOKEN}").status_code == 401
    assert app_client.get(f"/api/attachments/shot.png?key={TOKEN}").status_code == 401
    with app_client.stream("GET", f"/api/events?key={TOKEN}") as stream:
        assert stream.status_code == 200
        hello = json.loads(next(stream.iter_lines()).removeprefix("data: "))
        assert hello == {"type": "hello", "data": {"version": installed()}}
    sign_in(app_client)
    state = app_client.get("/api/state").json()
    assert state["runtime"]["state"] == "ready"
    assert [s["role"] for s in state["sessions"]] == ["router"]
    router = state["router_id"]
    response = app_client.post(f"/api/sessions/{router}/messages", json={"text": "hi"})
    assert response.status_code == 403, "writes without a same-origin header are refused"


def test_owner_talks_to_the_router(app_client):
    sign_in(app_client)
    router = app_client.get("/api/state").json()["router_id"]
    sent = app_client.post(
        f"/api/sessions/{router}/messages", json={"text": "Ship the release notes"}, headers=ORIGIN
    )
    assert sent.status_code == 200 and sent.json()["kind"] == "owner"
    assert app_client.fake.sessions[router].sent == [said("Ship the release notes")]
    messages = app_client.get(f"/api/sessions/{router}/messages").json()
    assert [m["text"] for m in messages] == ["Ship the release notes"]
    after = app_client.get(f"/api/sessions/{router}/messages?after={messages[-1]['id']}").json()
    assert after == []
    empty = app_client.post(f"/api/sessions/{router}/messages", json={"text": ""}, headers=ORIGIN)
    assert empty.status_code == 422
    closing = app_client.post(f"/api/sessions/{router}/close", headers=ORIGIN)
    assert closing.status_code == 400
    assert app_client.get(f"/api/sessions/{router}").json()["role"] == "router"


def test_settings_are_validated(app_client, tmp_path):
    sign_in(app_client)
    project = tmp_path / "project"
    project.mkdir()
    saved = app_client.post(
        "/api/settings", json={"projects": [str(project)], "max_workers": 3}, headers=ORIGIN
    )
    assert saved.status_code == 200
    assert saved.json()["settings"]["projects"] == [str(project.resolve())]
    containing_data = app_client.post(
        "/api/settings", json={"projects": [str(tmp_path)]}, headers=ORIGIN
    )
    assert "private data directory" in containing_data.json()["detail"]
    missing = app_client.post(
        "/api/settings", json={"projects": [str(tmp_path / "nope")]}, headers=ORIGIN
    )
    assert missing.status_code == 422


def test_a_settings_save_changes_only_what_it_sends(app_client, tmp_path):
    """Bug bash 5 (A13): `POST /api/settings {}` reset every setting (projects emptied)."""
    sign_in(app_client)
    project = tmp_path / "project"
    project.mkdir()
    first = {"projects": [str(project)], "max_workers": 3}
    assert app_client.post("/api/settings", json=first, headers=ORIGIN).status_code == 200
    kept = app_client.post("/api/settings", json={}, headers=ORIGIN).json()["settings"]
    assert kept["projects"] == [str(project.resolve())] and kept["max_workers"] == 3
    paused = app_client.post("/api/settings", json={"paused": True}, headers=ORIGIN).json()
    assert paused["settings"]["paused"] and paused["settings"]["projects"] == kept["projects"]
    wrong = app_client.post("/api/settings", json={"max_workers": 0}, headers=ORIGIN)
    assert wrong.status_code == 422, "an invalid value still names its field"
    assert wrong.json()["detail"][0]["loc"] == ["body", "max_workers"]


def test_a_stale_settings_tab_keeps_what_another_tab_changed(app_client, tmp_path):
    """Bug bash 5 (A13): with Settings open in two tabs, the one saved last put back every
    setting as it showed them, undoing the other's change."""
    sign_in(app_client)
    project = tmp_path / "project"
    project.mkdir()
    opened = app_client.get("/api/state").json()["settings"]  # Both tabs show these.
    first = {**opened, "projects": [str(project)], "base": opened}
    assert app_client.post("/api/settings", json=first, headers=ORIGIN).status_code == 200
    second = {**opened, "max_workers": 7, "base": opened}
    saved = app_client.post("/api/settings", json=second, headers=ORIGIN).json()["settings"]
    assert saved["projects"] == [str(project.resolve())], "the other tab's change stays"
    assert saved["max_workers"] == 7
    other = tmp_path / "other"
    other.mkdir()
    clash = {**opened, "projects": [str(other)], "base": opened}
    refused = app_client.post("/api/settings", json=clash, headers=ORIGIN)
    assert refused.status_code == 409 and "another tab" in refused.json()["detail"]
    assert app_client.get("/api/state").json()["settings"]["projects"] == saved["projects"]


def test_mcp_servers_copilot_cannot_start_are_refused(app_client):
    """Bug bash 5 (A3): an http server without a url, empty fields, an ftp:// url were saved,
    and every new worker then showed working for 90 s before failing."""
    sign_in(app_client)
    bad = {
        '"type": "http"': ({"type": "http", "command": "echo"}, "url is missing"),
        "empty": ({"url": "", "command": ""}, "give a command (local) or a url"),
        "ftp": ({"url": "ftp://x"}, "url must be an http:// or https:// address"),
        "local": ({"type": "local", "url": "https://x.example.com"}, "command is missing"),
    }
    for case, (config, problem) in bad.items():
        refused = app_client.post(
            "/api/settings", json={"mcp_servers": {"probe": config}}, headers=ORIGIN
        )
        assert refused.status_code == 422, case
        assert f"MCP server probe: {problem}" in refused.json()["detail"][0]["msg"], case
    fine = {
        "local": {"command": "npx", "args": ["-y", "jira-mcp"]},
        "stdio": {"type": "stdio", "command": "probe"},
        "remote": {"url": "https://mcp.example.com/mcp"},
        "http": {"type": "http", "url": "https://mcp.example.com/mcp", "headers": {"A": "b"}},
        "sse": {"type": "sse", "url": "http://127.0.0.1:3000/sse"},
    }
    saved = app_client.post("/api/settings", json={"mcp_servers": fine}, headers=ORIGIN)
    assert saved.status_code == 200 and set(saved.json()["settings"]["mcp_servers"]) == set(fine)


def test_saved_mcp_servers_copilot_would_refuse_are_kept_off_not_lost(app_client):
    """Code review (A3): a server an earlier version saved that the stricter check refuses made
    loading drop every MCP server, the valid ones too, and the next save wrote that to disk."""
    sign_in(app_client)
    store = app_client.app.state.store
    good = {"command": "npx", "args": ["-y", "jira-mcp"], "tools": ["*"]}
    bad = {"type": "http", "command": "npx srv", "tools": ["*"]}
    saved_before = {"max_workers": 2, "mcp_servers": {"jira": good, "old": bad}}
    store.execute("INSERT OR REPLACE INTO settings VALUES (1, ?)", (json.dumps(saved_before),))
    shown = app_client.get("/api/state").json()["settings"]
    assert shown["mcp_servers"] == {"jira": good}, "the valid one is used"
    assert shown["mcp_servers_off"] == {"old": bad} and shown["max_workers"] == 2
    other = app_client.post("/api/settings", json={"max_workers": 3}, headers=ORIGIN).json()
    saved = other["settings"]
    assert saved["mcp_servers"] == {"jira": good} and saved["mcp_servers_off"] == {"old": bad}
    on_disk = json.loads(store.one("SELECT value FROM settings WHERE id=1")["value"])
    assert on_disk["mcp_servers"] == {"jira": good} and on_disk["mcp_servers_off"] == {"old": bad}
    # As the Settings dialog saves: every server in its box, the one off as it was.
    page = {**saved, "mcp_servers": {"jira": good, "old": bad}, "mcp_servers_off": {}}
    again = app_client.post("/api/settings", json={**page, "base": saved}, headers=ORIGIN)
    assert again.status_code == 200, again.json()
    assert again.json()["settings"]["mcp_servers_off"] == {"old": bad}
    # Changed and still wrong: refused, naming it. Fixed: on.
    changed = {**page, "mcp_servers": {"jira": good, "old": {**bad, "timeout": 5000}}}
    refused = app_client.post("/api/settings", json=changed, headers=ORIGIN)
    assert refused.status_code == 422
    assert "MCP server old: url is missing" in refused.json()["detail"][0]["msg"]
    fixed = {**page, "mcp_servers": {"jira": good, "old": {"url": "https://mcp.example.com"}}}
    on = app_client.post("/api/settings", json=fixed, headers=ORIGIN).json()["settings"]
    assert set(on["mcp_servers"]) == {"jira", "old"} and on["mcp_servers_off"] == {}


def test_slack_token_must_be_a_user_token(app_client):
    sign_in(app_client)
    bot = app_client.post("/api/slack/token", json={"token": "xoxb-123"}, headers=ORIGIN)
    assert bot.status_code == 422
    saved = app_client.post("/api/slack/token", json={"token": "xoxp-1-abc"}, headers=ORIGIN)
    assert saved.status_code == 200 and saved.json()["slack"]["token_saved"] is True
    removed = app_client.delete("/api/slack/token", headers=ORIGIN)
    assert removed.json()["slack"]["token_saved"] is False


def test_unknown_draft_and_session_are_404(app_client):
    sign_in(app_client)
    assert app_client.get("/api/sessions/nope/messages").status_code == 404
    response = app_client.post("/api/drafts/dnope", json={"action": "send"}, headers=ORIGIN)
    assert response.status_code == 404


async def test_a_stalled_event_stream_is_closed_so_the_browser_resyncs():
    import asyncio

    from chiefofstaff.bus import Bus

    bus = Bus()
    bus.bind(asyncio.get_running_loop())
    async with bus.subscribe() as queue:
        for index in range(1002):
            bus.publish("message", index)
        items = [queue.get_nowait() for _ in range(queue.qsize())]
    assert items[-1] is None
    assert not bus.queues


def test_models_usage_and_profile_endpoints(app_client):
    sign_in(app_client)
    router = app_client.get("/api/state").json()["router_id"]
    assert [m["id"] for m in app_client.get("/api/models").json()] == [
        "auto",
        "fast-model",
        "smart-model",
    ]
    switched = app_client.post(
        f"/api/sessions/{router}/model", json={"model": "fast-model"}, headers=ORIGIN
    )
    assert switched.json()["model_choice"] == "fast-model"
    bad = app_client.post(f"/api/sessions/{router}/model", json={"model": "nope"}, headers=ORIGIN)
    assert bad.status_code == 400
    assert app_client.get("/api/usage?since=0").json()["total"]["calls"] == 0
    saved = app_client.post("/api/profile", json={"text": "- likes short replies"}, headers=ORIGIN)
    assert saved.json()["profile"] == "- likes short replies"
    # Started at once (not left to the Router), and only once while it works.
    started = app_client.post("/api/profile/distill", headers=ORIGIN).json()
    again = app_client.post("/api/profile/distill", headers=ORIGIN).json()
    assert started["session_id"] == again["session_id"]
    assert app_client.app.state.runtime.store.get(f"distill:{started['session_id']}")


def test_the_owner_reviews_and_follows_up_on_tasks(app_client):
    sign_in(app_client)
    runtime = app_client.app.state.runtime
    router = app_client.get("/api/state").json()["router_id"]
    result = app_client.portal.call(
        lambda: call_tool(runtime, "spawn_worker", router, title="Docs", instructions="Write docs")
    )
    spawned = json.loads(result.text_result_for_llm)
    task_id, worker = spawned["task_id"], spawned["session_id"]
    app_client.portal.call(
        lambda: call_tool(runtime, "report", worker, status="done", summary="Docs written")
    )
    listed = {task["id"]: task for task in app_client.get("/api/state").json()["tasks"]}
    assert listed[task_id]["status"] == "review" and listed[task_id]["worker"] == "Docs"

    followed = app_client.post(
        f"/api/tasks/{task_id}/follow-up", json={"text": "Add a FAQ"}, headers=ORIGIN
    )
    assert followed.status_code == 200 and followed.json()["status"] == "running"
    assert (
        app_client.fake.sessions[worker]
        .sent[-1]
        .startswith(env("owner", "message", "Add a FAQ", task=task_id))
    )
    app_client.portal.call(
        lambda: call_tool(runtime, "report", worker, status="done", summary="FAQ added")
    )
    accepted = app_client.post(f"/api/tasks/{task_id}/accept", headers=ORIGIN)
    assert accepted.json()["status"] == "done" and accepted.json()["runs"] == 2
    refused = app_client.post(f"/api/tasks/{task_id}/cancel", json={}, headers=ORIGIN)
    assert refused.status_code == 400


def test_a_pr_that_may_miss_work_says_so_and_opens_on_the_owners_word(app_client, monkeypatch):
    """Why it didn't open, with what the owner may do anyway; done anyway, on their word."""
    from chiefofstaff.task_actions import MayHoldWorkError

    sign_in(app_client)
    runtime = app_client.app.state.runtime
    asked = []

    async def create_pr(task_id: str, anyway: bool = False):
        asked.append(anyway)
        if not anyway:
            raise MayHoldWorkError("The app repository may have uncommitted changes in lib.")
        raise LookupError("opened")  # (What comes after is another test's.)

    monkeypatch.setattr(runtime.task_actions, "create_pr", create_pr)
    refused = app_client.post("/api/tasks/t1/pr", json={}, headers=ORIGIN)
    assert refused.status_code == 400
    assert refused.json() == {
        "detail": "The app repository may have uncommitted changes in lib.",
        "anyway": "Open it anyway",
    }
    plain = app_client.post("/api/tasks/t1/follow-up", json={"text": ""}, headers=ORIGIN)
    assert "anyway" not in plain.json()
    app_client.post("/api/tasks/t1/pr", json={"anyway": True}, headers=ORIGIN)
    assert asked == [False, True]


def test_the_page_hears_which_session_to_start_fresh_and_the_owner_starts_it(
    app_client, monkeypatch
):
    """A session Copilot can't reopen: the page hears which, for its Start fresh button, and
    the owner's click starts it."""
    from chiefofstaff import delivery

    sign_in(app_client)
    runtime = app_client.app.state.runtime
    started = []

    async def answer_question(question_id, text):
        raise delivery.FreshStartError("This worker's Copilot session can't be reopened.", "w1")

    async def start_fresh(session_id):
        started.append(session_id)
        return "Started a fresh worker with its context."

    monkeypatch.setattr(runtime.task_actions, "answer_question", answer_question)
    monkeypatch.setattr(runtime, "start_fresh", start_fresh)
    router = app_client.get("/api/state").json()["router_id"]
    refused = app_client.post(
        f"/api/questions/{router}:q/answer", json={"text": "Yes"}, headers=ORIGIN
    )
    assert refused.status_code == 409
    assert refused.json() == {
        "detail": "This worker's Copilot session can't be reopened.",
        "fresh": "w1",
    }
    done = app_client.post("/api/sessions/w1/fresh", headers=ORIGIN)
    assert done.json() == {"ok": True, "said": "Started a fresh worker with its context."}
    assert started == ["w1"]


def test_the_owner_requests_changes_and_sees_where_a_message_stands(app_client):
    sign_in(app_client)
    runtime = app_client.app.state.runtime
    router = app_client.get("/api/state").json()["router_id"]
    result = app_client.portal.call(
        lambda: call_tool(runtime, "spawn_worker", router, title="Docs", instructions="Write docs")
    )
    spawned = json.loads(result.text_result_for_llm)
    task_id, worker = spawned["task_id"], spawned["session_id"]
    app_client.portal.call(
        lambda: call_tool(runtime, "report", worker, status="done", summary="Docs written")
    )
    empty = app_client.post(f"/api/tasks/{task_id}/request-changes", json={"text": " "},
                            headers=ORIGIN)  # fmt: skip
    assert empty.status_code == 422, "what to change is said"
    changed = app_client.post(
        f"/api/tasks/{task_id}/request-changes", json={"text": "Shorter, please"}, headers=ORIGIN
    )
    assert changed.status_code == 200 and changed.json()["status"] == "running"
    assert "Shorter, please" in app_client.fake.sessions[worker].sent[-1]
    shown = app_client.get(f"/api/sessions/{worker}/messages").json()
    [asked] = [m for m in shown if m["kind"] == "owner"]
    assert asked["delivery"] == "", "a backend that shows no echo: nothing to claim"
    assert "prompt" not in asked["meta"]

    refused = app_client.post(f"/api/sessions/{worker}/follow-retry",
                              json={"key": "github:acme/app#7"}, headers=ORIGIN)  # fmt: skip
    assert refused.status_code == 400, "a PR it does not follow"


def sandboxed(app_client) -> Runtime:
    """Workers sandboxed: the one way their folders' settings wait for the owner's word."""
    runtime = app_client.app.state.runtime
    runtime.sandbox_host = True  # Whatever machine the tests run on.
    runtime.store.save_settings(
        runtime.store.settings().model_copy(update={"worker_sandbox": True})
    )
    return runtime


def held_worker(app_client, runtime) -> tuple[str, Path, dict]:
    """A sandboxed worker at rest whose folder gained a .mcp.json, and the owner's message that
    found it: it waits, saying why on its line."""
    router = app_client.get("/api/state").json()["router_id"]
    result = app_client.portal.call(
        lambda: call_tool(runtime, "spawn_worker", router, title="Docs", instructions="Write docs")
    )
    spawned = json.loads(result.text_result_for_llm)
    worker, cwd = spawned["session_id"], Path(spawned["cwd"])
    app_client.portal.call(
        lambda: call_tool(runtime, "report", worker, status="done", summary="Docs written")
    )
    (cwd / ".mcp.json").write_text('{"mcpServers": {}}')
    posted = app_client.post(
        f"/api/sessions/{worker}/messages", json={"text": "And a FAQ"}, headers=ORIGIN
    )
    assert posted.status_code == 200 and worker in runtime.settings_held
    return worker, cwd, posted.json()


def until(app_client, done, seconds: float = 5.0):
    """Waits (the app's loop running) until `done()` holds."""
    for _ in range(int(seconds / 0.02)):
        if done():
            return
        app_client.portal.call(lambda: asyncio.sleep(0.02))
    assert done()


def worker_state(app_client, worker: str) -> dict:
    return next(s for s in app_client.get("/api/state").json()["sessions"] if s["id"] == worker)


def test_the_owner_lets_a_worker_use_the_settings_in_its_folder(app_client):
    """Only the owner's page says to use a worker's changed agent settings, as it was shown
    them: a change since is refused."""
    sign_in(app_client)
    runtime = sandboxed(app_client)
    router = app_client.get("/api/state").json()["router_id"]
    result = app_client.portal.call(
        lambda: call_tool(runtime, "spawn_worker", router, title="Docs", instructions="Write docs")
    )
    spawned = json.loads(result.text_result_for_llm)
    worker, cwd = spawned["session_id"], Path(spawned["cwd"])
    app_client.portal.call(
        lambda: call_tool(runtime, "report", worker, status="done", summary="Docs written")
    )
    (cwd / ".mcp.json").write_text('{"mcpServers": {}}')
    sent = len(app_client.fake.sessions[worker].sent)
    posted = app_client.post(
        f"/api/sessions/{worker}/messages", json={"text": "And a FAQ"}, headers=ORIGIN
    )
    assert posted.status_code == 200 and len(app_client.fake.sessions[worker].sent) == sent
    shown = app_client.get(f"/api/sessions/{worker}/messages").json()
    (line,) = [m for m in shown if m["meta"].get("agent_settings")]
    assert line["meta"]["agent_settings"]["changes"] == "added .mcp.json"
    digest = line["meta"]["agent_settings"]["digest"]
    path = f"/api/sessions/{worker}/agent-settings"
    assert app_client.post(path, json={"digest": digest}).status_code == 403, "another page's"
    forged = {**ORIGIN, "X-Chief-Key": "a guess"}
    assert app_client.post(path, json={"digest": digest}, headers=forged).status_code == 401
    (cwd / ".mcp.json").write_text('{"mcpServers": {"x": {"command": "sh"}}}')
    stale = app_client.post(path, json={"digest": digest}, headers=ORIGIN)
    assert stale.status_code == 400 and "changed again" in stale.json()["detail"]
    (cwd / ".mcp.json").write_text('{"mcpServers": {}}')
    used = app_client.post(path, json={"digest": digest}, headers=ORIGIN)
    assert used.json() == {
        "ok": True,
        "said": "You let this worker use these settings (added .mcp.json).",
    }
    until(app_client, lambda: "And a FAQ" in app_client.fake.sessions[worker].sent[-1])


def test_a_message_queued_for_changed_settings_has_its_own_actions(app_client):
    """E121: what the owner writes a worker held for its folder's settings shows at once, under
    it, that line's choices for the settings shown. "ok" there is not their word on them: it
    says to use the button. Cancel sends it nowhere; a new worker takes its task, and it."""
    sign_in(app_client)
    runtime = sandboxed(app_client)
    worker, _, first = held_worker(app_client, runtime)
    hold = first["meta"]["settings_hold"]
    assert hold["digest"] and hold["changes"] == "added .mcp.json" and "guidance" not in hold
    asked = app_client.post(
        f"/api/sessions/{worker}/messages",
        json={"text": "approved, thanks", "settings_digest": hold["digest"]},
        headers=ORIGIN,
    ).json()
    assert asked["delivery"] == "queued" and worker in runtime.settings_held
    assert asked["meta"]["settings_hold"]["guidance"] == (
        "Use the button so the approval is tied to the settings shown."
    )

    cancelled = app_client.post(
        f"/api/messages/{asked['id']}/settings-hold/cancel",
        json={"digest": hold["digest"]},
        headers=ORIGIN,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["meta"]["settings_handled"]["status"] == "cancelled"
    assert worker in runtime.settings_held, "cancelling a message is not a word on settings"

    replacement = app_client.post(
        f"/api/messages/{first['id']}/settings-hold/new-worker",
        json={"digest": hold["digest"]},
        headers=ORIGIN,
    )
    assert replacement.status_code == 200
    new = replacement.json()["session_id"]
    assert new != worker
    until(app_client, lambda: runtime.store.session(worker)["closed_at"])
    given = [m["text"] for m in runtime.store.messages(new)]
    assert "And a FAQ" in given and "approved, thanks" not in given, "the cancelled one stays"
    assert runtime.store.message(first["id"])["meta"]["forwarded_to"] == new
    assert not (Path(runtime.store.session(new)["cwd"]) / ".mcp.json").exists()


def test_the_owners_words_in_a_held_workers_chat_use_the_settings_shown(app_client):
    """E121: "use these settings" (and a bare "ok" right after the line that asks) in a held
    worker's own chat is its Use these settings, for the line the page showed: one line says
    so, with Undo. Settings changed since are not used: their new line's choices are shown."""
    sign_in(app_client)
    runtime = sandboxed(app_client)
    worker, cwd, _ = held_worker(app_client, runtime)
    shown = worker_state(app_client, worker)["settings_hold"]["digest"]
    said = app_client.post(
        f"/api/sessions/{worker}/messages",
        json={"text": "ok", "settings_digest": shown},
        headers=ORIGIN,
    ).json()
    assert said["meta"]["settings_typed"] == shown and "prompt" not in said["meta"]
    assert worker not in runtime.settings_held
    sent = app_client.fake.sessions[worker].sent
    until(app_client, lambda: "And a FAQ" in sent[-1])
    assert not any("\nok\n" in text for text in sent), "their word, not the worker's"
    (line,) = [m for m in runtime.store.messages(worker) if m["meta"].get("settings_undo")]
    assert line["text"] == "Using these settings (added .mcp.json) because you said so."

    undone = app_client.post(
        f"/api/sessions/{worker}/agent-settings/undo", json={"digest": shown}, headers=ORIGIN
    )
    assert undone.status_code == 200 and worker in runtime.settings_held
    shown = worker_state(app_client, worker)["settings_hold"]["digest"]
    (cwd / ".mcp.json").write_text('{"mcpServers": {"x": {"command": "sh"}}}')
    stale = app_client.post(
        f"/api/sessions/{worker}/messages",
        json={"text": "Use these settings", "settings_digest": shown},
        headers=ORIGIN,
    ).json()
    assert "settings_typed" not in stale["meta"] and worker in runtime.settings_held
    hold = stale["meta"]["settings_hold"]
    assert hold["digest"] != shown and hold["guidance"].startswith("They changed after you wrote")


def test_only_clear_words_in_the_workers_own_chat_use_its_settings(app_client):
    """Never from Home (nor Slack, Send to Chief or a worker); a bare "ok" not right after the
    line that asks, or words that are not plainly that, are messages: the line's buttons say."""
    sign_in(app_client)
    runtime = sandboxed(app_client)
    worker, _, _ = held_worker(app_client, runtime)
    router = app_client.get("/api/state").json()["router_id"]
    shown = worker_state(app_client, worker)["settings_hold"]["digest"]
    for path, body in [
        (f"/api/sessions/{router}/messages", {"text": "use these settings", "to": worker}),
        (f"/api/sessions/{worker}/messages", {"text": "use these settings"}),
        (f"/api/sessions/{worker}/messages", {"text": "sure, whatever", "settings_digest": shown}),
        (f"/api/sessions/{worker}/messages", {"text": "ok", "settings_digest": shown}),
    ]:
        posted = app_client.post(path, json=body, headers=ORIGIN)
        assert posted.status_code == 200 and "settings_typed" not in posted.json()["meta"]
        assert worker in runtime.settings_held, body
    assert not [m for m in runtime.store.messages(worker) if m["meta"].get("settings_undo")]


async def test_closing_the_bus_ends_every_event_stream():
    bus = Bus()
    bus.bind(asyncio.get_running_loop())
    async with bus.subscribe() as open_tab:
        bus.close()
        assert await open_tab.get() is None, "Ctrl+C does not wait for open tabs"
    async with bus.subscribe() as reconnecting:
        assert reconnecting.get_nowait() is None


async def test_after_ctrl_c_writes_wait_for_the_disk_no_more_and_closing_keeps_them(tmp_path):
    """Each write waited for the disk until the stop: a Ctrl+C while Chief of Staff was busy
    waited for dozens of them (seconds on a Linux CI runner's disk). From Ctrl+C on they don't;
    closing still writes them all to the disk (SQLite's last checkpoint syncs)."""
    store, bus = Store(tmp_path), Bus()
    bus.bind(asyncio.get_running_loop())
    server = Server(uvicorn.Config(app=None), bus, store)
    full, normal = 2, 1  # PRAGMA synchronous: a sync at each commit, or at checkpoints only.
    assert store.value("PRAGMA synchronous") == full
    async with bus.subscribe() as open_tab:
        server.handle_exit(signal.SIGINT, None)
        assert await open_tab.get() is None, "Ctrl+C does not wait for open tabs"
    assert server.should_exit and store.value("PRAGMA synchronous") == normal
    store.set("uptime", "its run's end")
    store.close()
    server.stopping()  # Ctrl+C again as it ends: the store is closed, nothing to do.
    assert Store(tmp_path).get("uptime") == "its run's end"


def test_a_second_start_finds_only_an_instance_that_answers(tmp_path):
    assert running_instance(tmp_path) == ""
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    (tmp_path / LAUNCH_FILE).write_text(f"http://127.0.0.1:{port}/#token=abc")
    assert running_instance(tmp_path) == "", "a stale file from a crash is not trusted"


def test_the_owner_attaches_an_image_and_the_router_hands_it_on(app_client):
    import base64

    sign_in(app_client)
    router = app_client.get("/api/state").json()["router_id"]
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
    image = {"name": "bug.png", "type": "image/png", "data": base64.b64encode(png).decode()}
    url = f"/api/sessions/{router}/messages"
    fake = {**image, "data": base64.b64encode(b"<svg/>").decode()}
    refused = app_client.post(url, json={"attachments": [fake]}, headers=ORIGIN)
    assert refused.status_code == 400 and "not a PNG" in refused.text
    sent = app_client.post(url, json={"text": "", "attachments": [image]}, headers=ORIGIN)
    assert sent.status_code == 200
    [saved] = sent.json()["meta"]["attachments"]
    assert app_client.get(saved["url"]).content == png
    assert app_client.get("/api/attachments/..%2Fdata.db").status_code == 404
    session = app_client.fake.sessions[router]
    assert session.attachments[-1] == [
        {"type": "file", "path": saved["path"], "displayName": "bug.png"}
    ]
    assert saved["path"] in session.sent[-1], "the Router can hand it on by path"

    result = app_client.portal.call(
        lambda: call_tool(
            app_client.app.state.runtime,
            "spawn_worker",
            router,
            title="Fix the layout",
            instructions=f"Fix what this screenshot shows: {saved['path']}",
        )
    )
    worker = json.loads(result.text_result_for_llm)["session_id"]
    assert app_client.fake.sessions[worker].attachments[-1][0]["path"] == saved["path"]


def test_an_artifact_is_taken_off_the_list_and_brought_back(app_client):
    sign_in(app_client)
    key = "https://flyte.example.com/console/projects/p/domains/d/executions/abc123"
    taken = app_client.post(
        "/api/artifacts/hide", json={"key": key, "tone": "active"}, headers=ORIGIN
    )
    assert taken.status_code == 200 and taken.json()["hidden"] == [{"key": key, "tone": "active"}]
    assert app_client.get("/api/state").json()["artifacts_hidden"] == [
        {"key": key, "tone": "active"}
    ]
    long = "https://dash.example.com/" + "x" * 1500
    assert app_client.post("/api/artifacts/hide", json={"key": long}, headers=ORIGIN).is_success
    app_client.post("/api/artifacts/hide", json={"key": long, "hidden": False}, headers=ORIGIN)
    back = app_client.post(
        "/api/artifacts/hide", json={"key": key, "hidden": False}, headers=ORIGIN
    )
    assert back.json()["hidden"] == []
    blank = app_client.post("/api/artifacts/hide", json={"key": "  "}, headers=ORIGIN)
    assert blank.status_code == 400


def test_a_router_note_that_needs_the_owner_stays_until_seen(app_client):
    sign_in(app_client)
    runtime = app_client.app.state.runtime
    router = app_client.get("/api/state").json()["router_id"]
    notes = [
        {"text": "Alice asks for the release date.", "needs_action": True},
        {"text": "The deploy is down.", "urgent": True},
        {"text": "Two PRs merged."},
    ]
    for note in notes:
        app_client.portal.call(lambda note=note: call_tool(runtime, "notify_owner", router, **note))
    runtime.add_message(router, "update", "Daily summary\n…", {"digest": True})
    updates = app_client.get("/api/state").json()["updates"]
    assert [(u["text"], u["urgent"]) for u in updates] == [
        ("Alice asks for the release date.", False),
        ("The deploy is down.", True),
    ], "only what needs the owner"

    seen = app_client.post(f"/api/messages/{updates[0]['id']}/seen", headers=ORIGIN)
    assert seen.status_code == 200
    left = app_client.get("/api/state").json()["updates"]
    assert [u["text"] for u in left] == ["The deploy is down."]
    runtime.add_message(router, "owner", "Use the cache", {"to": "w1", "to_title": "Cache"})
    assert app_client.get("/api/state").json()["updates"] == left, "told a worker, not the Router"
    wrote = {"text": "On it"}
    owner = app_client.post(f"/api/sessions/{router}/messages", json=wrote, headers=ORIGIN)
    for message_id, status in [(owner.json()["id"], 400), (999999, 404)]:
        refused = app_client.post(f"/api/messages/{message_id}/seen", headers=ORIGIN)
        assert refused.status_code == status, "only the Router's updates"
    assert app_client.get("/api/state").json()["updates"] == [], "the owner wrote after it"


def test_a_session_title_cut_in_the_middle_of_an_emoji_does_not_break_its_list(app_client):
    """JSON can carry a lone surrogate (the CLI cut a title in the middle of an emoji); UTF-8
    cannot: the sidebar's list and the Router's list_sessions failed whole."""
    sign_in(app_client)
    context = SimpleNamespace(
        working_directory="/work/app \udc00", repository="o/app\ud800", branch="fix-\udbff"
    )
    app_client.fake.metas = [
        SimpleNamespace(
            session_id=session_id,
            summary=summary,
            is_remote=False,
            modified_time=datetime(2026, 11, day, tzinfo=UTC),
            context=context,
        )
        for session_id, summary, day in [
            ("cut-\udc00", "Ship the release \ud83d", 2),
            ("whole", "Plan 🚀", 1),
        ]
    ]
    app_client.fake.in_use = {"cut-\udc00": 4242, "whole": 4243}
    listed = app_client.get("/api/external?refresh=true")
    assert listed.status_code == 200
    fields = ("id", "summary", "cwd", "repository", "branch", "open")
    assert [tuple(item[key] for key in fields) for item in listed.json()] == [
        ("cut-?", "Ship the release ?", "/work/app ?", "o/app?", "fix-?", True),
        ("whole", "Plan 🚀", "/work/app ?", "o/app?", "fix-?", True),
    ], "a whole emoji as it was"
    runtime = app_client.app.state.runtime
    router = app_client.get("/api/state").json()["router_id"]
    result = app_client.portal.call(call_tool, runtime, "list_sessions", router)
    assert result.result_type == "success"
    told = json.loads(result.text_result_for_llm.encode("utf-8"))["terminal_sessions"]
    assert [session["title"] for session in told] == ["Ship the release ?", "Plan 🚀"]


def test_a_session_log_with_half_an_emoji_still_shows(app_client, monkeypatch):
    """What a session's log says, in the view behind a row of Other Copilot sessions and in a
    worker's timeline (mirrored from its terminal, or brought along when it is adopted)."""
    sign_in(app_client)
    runtime = app_client.app.state.runtime
    logged = [
        {"type": "user.message", "data": {"content": "Ship it \ud83d"}},
        {"type": "assistant.message", "data": {"content": "Shipped 🚀 \udc00"}},
    ]
    app_client.fake.logs["ext-1"] = logged
    history = app_client.get("/api/external/ext-1/history")
    assert history.status_code == 200
    assert [entry["text"] for entry in history.json()["entries"]] == ["Ship it ?", "Shipped 🚀 ?"]

    runtime.store.create_session("term-1", "worker", "Ship", "idle", cwd="/tmp")
    app_client.fake.in_use = {"term-1": 4242}
    app_client.fake.logs["term-1"] = logged
    app_client.portal.call(runtime.workers.refresh_elsewhere)
    app_client.portal.call(runtime.workers.mirror)
    mirrored = app_client.get("/api/sessions/term-1/messages")
    assert mirrored.status_code == 200
    assert [m["text"] for m in mirrored.json()][1:] == ["Ship it ?", "Shipped 🚀 ?"]

    async def events(session):
        said = SimpleNamespace(content="Fix it \ud83d", parent_tool_call_id=None)
        return [SimpleNamespace(type=SimpleNamespace(value="user.message"), data=said)]

    monkeypatch.setattr(FakeSession, "get_events", events)
    app_client.fake.metas = [
        SimpleNamespace(
            session_id="ext-2",
            summary="Fix it",
            is_remote=False,
            modified_time=datetime(2026, 11, 1, tzinfo=UTC),
            context=None,
        )
    ]
    app_client.fake.sessions["ext-2"] = FakeSession("ext-2", {})
    assert app_client.post("/api/external/ext-2/adopt", headers=ORIGIN).status_code == 200
    adopted = app_client.get("/api/sessions/ext-2/messages")
    assert adopted.status_code == 200 and "Fix it ?" in [m["text"] for m in adopted.json()]


async def test_no_event_can_end_the_tabs_live_updates():
    """Every tab's stream sends the same events: one UTF-8 cannot hold ended all of them."""
    bus = Bus()
    bus.bind(asyncio.get_running_loop())
    data = {"text": "Done \ud83d", "emoji": "Shipped 🚀", "words": "é漢字"}
    async with bus.subscribe() as tab:
        bus.publish("message", data)
        payload = tab.get_nowait()
    payload.encode("utf-8")  # As the stream sends it.
    sent = {"type": "message", "data": {**data, "text": "Done ?"}}
    assert payload == json.dumps(sent, ensure_ascii=False), "the rest exactly as before"


def test_a_teams_thread_a_workers_short_name_and_a_nudge_from_the_page(app_client):
    sign_in(app_client)
    runtime = app_client.app.state.runtime
    router = app_client.get("/api/state").json()["router_id"]

    def spawn(title, **extra):
        result = app_client.portal.call(
            lambda: call_tool(
                runtime,
                "spawn_worker",
                router,
                title=title,
                instructions=title,
                why_new="test",
                **extra,
            )
        )
        return json.loads(result.text_result_for_llm)

    scout = spawn("Token scout")
    writer = spawn("Token writer", works_with=[scout["task_id"]])["session_id"]
    asked = app_client.portal.call(
        lambda: call_tool(
            runtime, "ask_session", writer, session_id=scout["session_id"], question="Scope?"
        )
    )
    question = asked.text_result_for_llm.split("(")[1].split(")")[0]
    team = f"team-{scout['session_id'][:8]}"
    thread = app_client.get(f"/api/teams/{team}/thread").json()
    assert thread["team"]["name"] == "Token scout & Token writer"
    assert [(item["kind"], item["status"]) for item in thread["items"]] == [("ask", "open")]
    assert app_client.get("/api/teams/team-nobody00/thread").status_code == 404
    named = app_client.post(
        f"/api/sessions/{writer}/label", json={"text": "Client"}, headers=ORIGIN
    )
    assert named.json() == {"label": "Client"}
    assert (
        app_client.get(f"/api/teams/{team}/thread").json()["team"]["name"] == "Token scout & Client"
    )
    refused = app_client.post(f"/api/sessions/{router}/label", json={"text": "x"}, headers=ORIGIN)
    assert refused.status_code == 400
    nudged = app_client.post(
        f"/api/teams/{team}/nudge", json={"question_id": question}, headers=ORIGIN
    )
    assert nudged.status_code == 200 and nudged.json()["said"].startswith("Nudged “Token scout”")
    again = app_client.post(
        f"/api/teams/{team}/nudge", json={"question_id": question}, headers=ORIGIN
    )
    assert again.status_code == 400 and "Nudged already" in again.json()["detail"]
