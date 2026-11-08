import asyncio
import json
import socket

import pytest
from conftest import FakeClient, FakeSlackMcp, FakeTokens, call_tool, env, said
from fastapi.testclient import TestClient

from chiefofstaff.__main__ import LAUNCH_FILE, running_instance
from chiefofstaff.bus import Bus
from chiefofstaff.runtime import Runtime
from chiefofstaff.sources.slack import SlackSource
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
    assert app_client.get(f"/api/state?key={TOKEN}").status_code == 200, "an image, the stream"
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
    assert app_client.fake.sessions[worker].sent[-1] == env(
        "owner", "message", "Add a FAQ", task=task_id
    )
    app_client.portal.call(
        lambda: call_tool(runtime, "report", worker, status="done", summary="FAQ added")
    )
    accepted = app_client.post(f"/api/tasks/{task_id}/accept", headers=ORIGIN)
    assert accepted.json()["status"] == "done" and accepted.json()["runs"] == 2
    refused = app_client.post(f"/api/tasks/{task_id}/cancel", json={}, headers=ORIGIN)
    assert refused.status_code == 400


async def test_closing_the_bus_ends_every_event_stream():
    bus = Bus()
    bus.bind(asyncio.get_running_loop())
    async with bus.subscribe() as open_tab:
        bus.close()
        assert await open_tab.get() is None, "Ctrl+C does not wait for open tabs"
    async with bus.subscribe() as reconnecting:
        assert reconnecting.get_nowait() is None


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
    taken = app_client.post("/api/artifacts/hide", json={"key": key}, headers=ORIGIN)
    assert taken.status_code == 200 and taken.json()["hidden"] == [key]
    assert app_client.get("/api/state").json()["artifacts_hidden"] == [key]
    back = app_client.post(
        "/api/artifacts/hide", json={"key": key, "hidden": False}, headers=ORIGIN
    )
    assert back.json()["hidden"] == []
    blank = app_client.post("/api/artifacts/hide", json={"key": "  "}, headers=ORIGIN)
    assert blank.status_code == 400
