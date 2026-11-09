"""Bug bash 6 (0.1.30): the Router kept when it cannot be resumed, Copilot's CLI watched, the
Slack session's answered questions, models that stick or do not exist, projects whose folder
went away, helpers in the handover, the credit limit."""

import asyncio
import json
import os
import signal
import subprocess
import time

import pytest
from conftest import FakeClient, FakeSlackMcp, FakeTokens, Harness, call_tool, settle
from fastapi.testclient import TestClient
from pydantic import ValidationError

from chiefofstaff import runtime as runtime_module
from chiefofstaff.backend import (
    CopilotBackend,
    copilot_records,
    descendants,
    process_table,
    running,
)
from chiefofstaff.models import AS_SAVED, DraftDecision, Settings
from chiefofstaff.roles import WORKER
from chiefofstaff.runtime import Runtime
from chiefofstaff.sources.slack import SlackSource
from chiefofstaff.store import Store, now
from chiefofstaff.tools import shortened
from chiefofstaff.web import create_app

TOKEN = "launch-token"
ORIGIN = {"origin": "http://127.0.0.1:8765"}


@pytest.fixture
def fast_recovery(monkeypatch):
    monkeypatch.setattr(runtime_module, "PROCESS_CHECK", 0.01)
    monkeypatch.setattr(runtime_module, "RECONNECT_DELAY", 0.01)


async def until(check, seconds: float = 5.0):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def router_with_history(harness):
    runtime = await harness.connect()
    router = harness.router_id
    await runtime.owner_send(router, "Remember: the launch is on Friday.")
    await settle(runtime)
    await runtime.disconnect()
    return runtime, router


# R1: the Router is never closed and replaced when it cannot be resumed.


async def test_a_router_open_in_a_terminal_waits_and_is_kept(tmp_path, fast_recovery):
    harness = Harness(tmp_path / "data")
    runtime, router = await router_with_history(harness)
    harness.client.in_use = {harness.store.get(f"copilot:{router}") or router: 53346}
    await runtime.connect()
    assert runtime.status["state"] == "error"
    assert runtime.status["error"].startswith(
        "The Router is open in another Copilot window (process 53346): exit it there"
    )
    assert harness.router_id == router and not harness.store.session(router)["closed_at"]
    assert [s["role"] for s in harness.store.all("SELECT role FROM sessions")] == ["router"]

    harness.client.in_use = {}  # The owner exited it there: it connects again by itself.
    await until(runtime.ready)
    assert harness.router_id == router
    assert router in harness.client.resumed
    owner = [m["text"] for m in harness.store.messages(router) if m["kind"] == "owner"]
    assert owner == ["Remember: the launch is on Friday."], "its conversation stays"
    await runtime.stop()


async def test_a_router_copilot_no_longer_has_gets_a_fresh_session_with_a_handover(tmp_path):
    harness = Harness(tmp_path / "data")
    runtime, router = await router_with_history(harness)
    del harness.client.sessions[router]  # Copilot's storage of it is gone.
    await runtime.connect()
    assert runtime.ready()
    assert harness.router_id == router and not harness.store.session(router)["closed_at"]
    fresh = harness.store.get(f"copilot:{router}")
    assert fresh and fresh != router and fresh in harness.client.created
    assert harness.store.get(f"alias:{fresh}") == router
    await settle(runtime)
    preamble = harness.store.get(f"preamble:{router}") or harness.client.sessions[fresh].sent[0]
    assert "could not be resumed" in preamble and "the launch is on Friday" in preamble
    notices = [m["text"] for m in harness.store.messages(router) if m["kind"] == "notice"]
    assert any("a fresh one took over" in text for text in notices)


async def test_a_router_is_not_replaced_while_copilot_itself_is_down(tmp_path, fast_recovery):
    harness = Harness(tmp_path / "data")
    runtime, router = await router_with_history(harness)
    harness.client.crashed = True
    resume = harness.client.resume_session

    async def refuse(session_id, **options):
        raise RuntimeError("CLI process exited with code -15")

    harness.client.resume_session = refuse
    await runtime.connect()
    assert runtime.status["state"] == "error"
    assert harness.store.get(f"copilot:{router}") is None, "its Copilot session is kept"
    assert harness.router_id == router and not harness.store.session(router)["closed_at"]
    harness.client.crashed, harness.client.resume_session = False, resume  # Copilot runs again.
    await until(runtime.ready)
    assert router in harness.client.resumed and harness.store.get(f"copilot:{router}") is None
    await runtime.stop()


# R2: Copilot's CLI is watched; its own processes are never the owner's terminal.


async def test_the_slack_session_is_kept_when_it_cannot_be_resumed_for_now(tmp_path):
    """Open in a terminal, or Copilot down while connecting: not closed and replaced."""
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    slack = harness.store.open_sessions("slack")[0]["id"]
    harness.store.delete(f"fresh:{slack}")  # It had a message: Copilot keeps it.
    resume = harness.client.resume_session

    async def down(session_id, **options):
        if session_id == slack:
            raise RuntimeError("CLI process exited with code -15")
        return await resume(session_id, **options)

    for trouble in ("terminal", "down"):
        await runtime.disconnect()
        harness.client.in_use = {slack: 4242} if trouble == "terminal" else {}
        harness.client.crashed = trouble == "down"
        harness.client.resume_session = down if trouble == "down" else resume
        await runtime.connect()
        assert runtime.ready()
        assert "The Slack session could not start" in runtime.status["warning"]
        assert [row["id"] for row in harness.store.open_sessions("slack")] == [slack], trouble
    await runtime.stop()


async def test_a_copilot_cli_that_stops_by_itself_is_noticed_and_reconnected(
    tmp_path, fast_recovery
):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    states = []
    publish = runtime.publish_state

    def record():
        states.append(dict(runtime.status))
        publish()

    runtime.publish_state = record
    stopped = []
    stop = harness.client.stop

    async def restart():
        stopped.append(True)
        harness.client.crashed = False  # The next CLI runs.
        await stop()

    harness.client.stop = restart
    before = runtime.connections
    harness.client.crashed = True
    await until(lambda: stopped and runtime.ready())
    assert any(s["error"].startswith("Copilot stopped unexpectedly") for s in states)
    assert runtime.connections > before
    await runtime.stop()


async def test_sessions_its_own_cli_has_open_are_not_a_terminals(tmp_path):
    """An orphaned CLI this app started (its loader killed) still has the Router open: not the
    owner's terminal, so nothing is refused or shown as open elsewhere."""
    harness = Harness(tmp_path / "data")
    harness.client.pids = {999_991, 999_992}
    runtime = await harness.connect()
    router = harness.router_id
    assert sorted(map(int, json.loads(harness.store.get("copilot_pids") or "{}"))) == [
        999_991,
        999_992,
    ]
    harness.client.in_use = {harness.store.get(f"copilot:{router}") or router: 999_992}
    await runtime.reconnect()
    assert runtime.ready() and harness.router_id == router
    await runtime.workers.refresh_elsewhere()
    assert runtime.elsewhere == {}
    assert runtime.view(harness.store.session(router))["elsewhere"] is False
    await runtime.stop()


def test_stopping_copilot_also_ends_the_program_its_loader_left_running(tmp_path):
    """The npm loader runs Copilot's own program; killed alone, that program kept running with
    its sessions open (and what it started, a worker's command)."""
    program = tmp_path / "copilot"
    program.write_text("#!/bin/sh\nsleep 300 &\nwait\n")
    loader = tmp_path / "loader"
    loader.write_text(f'#!/bin/sh\n"{program}"\n')
    for path in (program, loader):
        path.chmod(0o755)
    process = subprocess.Popen([str(loader)])

    async def nothing():
        pass

    backend = CopilotBackend(
        type("Client", (), {"_cli_process": process, "stop": staticmethod(nothing)})()  # type: ignore[arg-type]
    )
    deadline = time.monotonic() + 5
    while len(backend.processes()) < 3:  # The loader, the program, its command.
        assert time.monotonic() < deadline
        time.sleep(0.05)
    started = set(backend.processes()) - {process.pid}
    process.send_signal(signal.SIGKILL)
    process.wait()
    assert not backend.alive()
    assert all(running(pid) for pid in started), "orphaned, still running"
    asyncio.run(backend.stop())
    deadline = time.monotonic() + 5
    while any(running(pid) for pid in started) and time.monotonic() < deadline:
        time.sleep(0.05)
    left = [pid for pid in started if running(pid)]
    for pid in left:
        os.kill(pid, signal.SIGKILL)
    assert not left


async def test_a_cli_an_earlier_start_left_running_is_stopped_when_connecting(tmp_path):
    """This app stopped abruptly: the Copilot it ran, orphaned, still had its sessions open."""
    program = tmp_path / "copilot"
    program.write_text("#!/bin/sh\nsleep 300 &\nwait\n")
    program.chmod(0o755)
    shell = await asyncio.to_thread(
        subprocess.run,
        ["/bin/sh", "-c", f'"{program}" >/dev/null 2>&1 & echo $!'],
        capture_output=True,
        text=True,
        check=True,
    )
    orphan = int(shell.stdout.strip())
    deadline = time.monotonic() + 5
    while len(descendants(process_table(), orphan)) < 2:  # Its command started.
        assert time.monotonic() < deadline
        await asyncio.sleep(0.05)
    tree = descendants(process_table(), orphan)
    try:
        harness = Harness(tmp_path / "data")
        records = copilot_records({orphan: process_table()[orphan][1]}, tmp_path / "data")
        harness.store.set("copilot_pids", json.dumps(records))
        runtime = await harness.connect()
        assert runtime.ready()
        assert not any(running(pid) for pid in tree)
    finally:
        for pid in tree:
            if running(pid):
                os.kill(pid, signal.SIGKILL)


# R3: the Slack session's question is settled by the answer or the reply sent.


async def slack_harness(tmp_path):
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    return harness, runtime, harness.store.open_sessions("slack")[0]["id"]


ALICE = (
    "Alice Chen (DM D0ALICE01, thread_ts 1794157783.114139) asks if the owner is joining the "
    "planning sync tomorrow at 10. Is the owner attending? Once I know, I'll draft the reply."
)
BOB = "Bob (#deploys C0DEPLOYS, thread 1794138887.017740) asks for the merge date. Which?"
ERIN = "Erin (D0ERIN001) asks if you'll join Friday \u2014 yes or no?"
BOB_LOG = "Bob asks whether the CHANGELOG is ready (#deploys C0DEPLOYS, thread 1794138887.017740)."
CAROL = "Carol (#deploys C0DEPLOYS, thread 1794139999.000300) asks who reviews her PR."


async def tell_slack(runtime, harness, slack, text):
    result = await call_tool(runtime, "message_session", harness.router_id, session_id=slack,
                             text=text)  # fmt: skip
    assert result.result_type != "failure", result.text_result_for_llm


def waiting(runtime):
    return [q["text"] for q in runtime.asks.owner_questions()]


async def test_the_routers_answer_settles_the_slack_sessions_question_about_it(tmp_path):
    harness, runtime, slack = await slack_harness(tmp_path)
    for asked in (ALICE, BOB):
        await call_tool(runtime, "report", slack, status="question", summary=asked)
    await tell_slack(runtime, harness, slack, "The owner answered: yes, they're joining. Draft "
                     "the reply to Alice in D0ALICE01.")  # fmt: skip
    assert waiting(runtime) == [BOB], "Bob's still waits"
    await runtime.owner_send(harness.router_id, "Thursday works for the merge.")
    said = [m for m in harness.store.messages(harness.router_id) if m["kind"] == "owner"]
    await tell_slack(runtime, harness, slack, f"{{owner:{said[-1]['meta']['ref']}}} Reply to "
                     "Bob in slack:C0DEPLOYS:1794138887.017740.")  # fmt: skip
    assert waiting(runtime) == []


async def test_a_slack_event_forwarded_to_the_slack_session_answers_no_question(tmp_path):
    """Review of R3: the Slack session's only question went away with any message to it, a
    Slack event the Router forwarded too."""
    harness, runtime, slack = await slack_harness(tmp_path)
    await call_tool(runtime, "report", slack, status="question", summary=ERIN)
    for text in (
        "Dave in #deploys (slack:C0DEPLOYS:1794139000.000100): DEPLOYMENT done, CHANGELOG next.",
        "Erin again (slack:D0ERIN001): bring the slides, please.",
        "The owner says yes.",  # Which conversation is not said.
    ):
        await tell_slack(runtime, harness, slack, text)
        assert waiting(runtime) == [ERIN], text


async def test_only_the_question_about_the_conversation_named_is_settled(tmp_path):
    harness, runtime, slack = await slack_harness(tmp_path)
    for asked in (ERIN, BOB_LOG, CAROL):
        await call_tool(runtime, "report", slack, status="question", summary=asked)
    await tell_slack(runtime, harness, slack, "The owner answered yes: reply to Erin in "
                     "D0ERIN001, and say the CHANGELOG is on its way.")  # fmt: skip
    assert waiting(runtime) == [BOB_LOG, CAROL], "CHANGELOG is no conversation"
    await tell_slack(runtime, harness, slack, "The owner decided: it is ready. Reply in "
                     "slack:C0DEPLOYS:1794138887.017740.")  # fmt: skip
    assert waiting(runtime) == [CAROL], "another thread of the channel"


async def test_a_reply_sent_settles_the_question_asked_before_it_was_drafted(tmp_path):
    harness, runtime, slack = await slack_harness(tmp_path)
    for asked in (ALICE, BOB):
        await call_tool(runtime, "report", slack, status="question", summary=asked)
    result = await call_tool(runtime, "draft_slack_reply", slack, channel_id="D0ALICE01",
                             thread_ts="", text="Yes, I'll be there.")  # fmt: skip
    assert result.result_type != "failure", result.text_result_for_llm
    [card] = harness.store.drafts()
    await runtime.drafts.resolve(card["id"], DraftDecision(action="send"))
    await settle(runtime)
    assert harness.store.draft(card["id"])["status"] == "sent"
    assert [q["text"] for q in runtime.asks.owner_questions()] == [BOB]
    other = await call_tool(runtime, "draft_slack_reply", slack, channel_id="C0DEPLOYS",
                            thread_ts="1794138000.000100", text="Not this thread.")  # fmt: skip
    assert other.result_type != "failure", other.text_result_for_llm
    await runtime.drafts.resolve(harness.store.drafts()[-1]["id"], DraftDecision(action="send"))
    await settle(runtime)
    assert [q["text"] for q in runtime.asks.owner_questions()] == [BOB], "another thread"


# R4: a model fallback ends when the owner chooses again.


async def refused(harness, runtime, model: str):
    router = harness.store.session(harness.router_id)
    assert await runtime.recover_model(router, f"Model {model} is not supported")
    await settle(runtime)
    return harness.store.session(harness.router_id)


async def test_choosing_the_routers_model_again_ends_its_fallback(tmp_path):
    harness = Harness(tmp_path / "data", Settings(role_models={"router": "fast-model"}))
    runtime = await harness.connect()
    router = await refused(harness, runtime, "fast-model")
    assert runtime.model_for(router) == ""
    # Allowed again (the company changed its policy): the owner picks it in the header.
    view = await runtime.set_session_model(router["id"], "fast-model")
    assert view["model"] == "fast-model"
    assert harness.session(router["id"]).models[-1] == "fast-model"


async def test_a_model_chosen_since_the_refusal_is_used(tmp_path):
    harness = Harness(tmp_path / "data", Settings(role_models={"router": "gpt-nonexistent-1"}))
    runtime = await harness.connect()
    router = await refused(harness, runtime, "gpt-nonexistent-1")
    harness.store.save_settings(Settings(role_models={"router": "smart-model"}))
    assert runtime.model_for(harness.store.session(router["id"])) == "smart-model"


async def test_new_settings_end_a_fallback_of_the_effort(tmp_path):
    settings = Settings(role_models={"router": "smart-model"}, role_efforts={"router": "high"})
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    router = await refused(harness, runtime, "smart-model")
    assert runtime.fallback(router) == "tuning" and runtime.tuning(router) == {}
    new = settings.model_copy(update={"role_efforts": {"router": "low"}})
    harness.store.save_settings(new)
    await runtime.settings_changed(settings, new)
    router = harness.store.session(router["id"])
    assert runtime.tuning(router) == {"reasoning_effort": "low"}


async def test_a_refused_model_chosen_again_in_settings_is_tried_again(tmp_path):
    first = Settings(role_models={"router": "fast-model"})
    harness = Harness(tmp_path / "data", first)
    runtime = await harness.connect()
    router = await refused(harness, runtime, "fast-model")
    for old, new in ((first, Settings()), (Settings(), first)):  # Reset, then chosen again.
        harness.store.save_settings(new)
        await runtime.settings_changed(old, new)
    assert runtime.model_for(harness.store.session(router["id"])) == "fast-model"


# R5: models that do not exist.


def make_app(tmp_path, client):
    def factory(store, bus):
        async def notifier(title, text):
            pass

        runtime = Runtime(store, bus, backend_factory=lambda settings: client, notifier=notifier)
        runtime.slack = SlackSource(store, runtime, tokens=FakeTokens(""), mcp=FakeSlackMcp())
        return runtime

    return create_app(tmp_path / "data", TOKEN, 8765, runtime_factory=factory)


def signed_in(tmp_path):
    return TestClient(make_app(tmp_path, FakeClient()), base_url="http://127.0.0.1:8765")


def test_settings_refuse_a_model_copilot_does_not_offer(tmp_path):
    with signed_in(tmp_path) as http:
        assert http.post("/api/auth", json={"token": TOKEN}, headers=ORIGIN).status_code == 200
        http.headers["X-Chief-Key"] = TOKEN
        wrong = http.post(
            "/api/settings", json={"role_models": {"worker": "claude-sonet-9.9"}}, headers=ORIGIN
        )
        assert wrong.status_code == 422
        [problem] = wrong.json()["detail"]
        assert problem["loc"] == ["body", "role_models", "worker"]
        assert problem["msg"].startswith("Workers model: Copilot offers no model")
        assert "smart-model" in problem["msg"]
        assert http.get("/api/state").json()["settings"]["role_models"] == {}
        fine = {"model": "auto", "role_models": {"worker": "smart-model", "router": ""}}
        assert http.post("/api/settings", json=fine, headers=ORIGIN).status_code == 200


async def test_a_worker_whose_model_copilot_refuses_starts_on_the_default(tmp_path):
    harness = Harness(tmp_path / "data", Settings(role_models={"worker": "claude-sonet-9.9"}))
    create = harness.client.create_session

    async def refusing(*, session_id, name, **options):
        if options.get("model") == "claude-sonet-9.9":
            raise RuntimeError(
                "JSON-RPC Error -32603: Request session.create failed with message: Model "
                '"claude-sonet-9.9" is not available.'
            )
        return await create(session_id=session_id, name=name, **options)

    harness.client.create_session = refusing
    runtime = await harness.connect()
    result = await call_tool(runtime, "spawn_worker", harness.router_id, title="Fix the parser",
                             instructions="Fix the parser", why_new="test")  # fmt: skip
    worker = json.loads(result.text_result_for_llm)["session_id"]
    await settle(runtime)
    row = harness.store.session(worker)
    assert row["status"] == "working"
    assert "model" not in harness.session(worker).options
    tasks = runtime.tasks.for_session(worker)
    assert [t["status"] for t in tasks] == ["running"]
    shown = [m["text"] for m in harness.store.messages(worker) if m["kind"] in {"notice", "error"}]
    assert shown == [
        "Copilot does not offer the model claude-sonet-9.9, so this worker uses Copilot's "
        "default model. Choose another in Settings (Models) or in this worker's header."
    ]


# R6: a project whose folder is gone stays configured, shown as not found.


async def test_a_project_whose_folder_is_gone_stays_and_is_named_so(tmp_path):
    vanish, ronly = tmp_path / "vanish", tmp_path / "ronly"
    vanish.mkdir()
    ronly.mkdir()
    harness = Harness(tmp_path / "data", Settings(projects=[str(vanish), str(ronly)]))
    runtime = await harness.connect()
    assert runtime.projects_note() == ""  # The Router's first look.
    vanish.rmdir()
    assert harness.store.settings().projects == [str(vanish), str(ronly)]
    assert runtime.snapshot()["missing_projects"] == [str(vanish)]
    note = runtime.projects_note()
    assert f"- {vanish} (configured, but its folder is not on disk" in note
    cards = await runtime.workspaces.project_cards()
    assert [card.get("missing", "")[:10] for card in cards] == ["configured", ""]
    big = {"tasks": [], "recently_closed": [], "sessions": [{"title": "x" * 40000}]}
    short = shortened({**big, "projects": cards})["projects"]
    assert short[0]["missing"].startswith("configured") and "missing" not in short[1]


def test_settings_read_with_another_problem_keep_a_project_whose_folder_is_gone(tmp_path):
    vanish, code = tmp_path / "vanish", tmp_path / "code"
    vanish.mkdir()
    code.mkdir()
    store = Store(tmp_path / "data")
    store.save_settings(Settings(projects=[str(vanish)], code_folder=str(code)))
    vanish.rmdir()
    code.rmdir()  # Invalid too: the settings are read again without what is wrong.
    assert store.settings().projects == [str(vanish)]
    assert store.settings().code_folder == ""


def test_saving_other_settings_keeps_a_project_whose_folder_is_gone(tmp_path):
    vanish = tmp_path / "vanish"
    vanish.mkdir()
    store = Store(tmp_path / "data")
    store.save_settings(Settings(projects=[str(vanish)]))
    store.close()
    vanish.rmdir()
    with signed_in(tmp_path) as http:
        assert http.post("/api/auth", json={"token": TOKEN}, headers=ORIGIN).status_code == 200
        http.headers["X-Chief-Key"] = TOKEN
        saved = http.post("/api/settings", json={"max_workers": 3}, headers=ORIGIN)
        assert saved.status_code == 200
        assert saved.json()["settings"]["projects"] == [str(vanish)]
        projects = [str(vanish), str(tmp_path / "nope")]
        new = http.post("/api/settings", json={"projects": projects}, headers=ORIGIN)
        assert new.status_code == 422, "a new one must exist"


# R8: helpers are not the owner's closed workers.


async def test_the_handover_does_not_list_closed_helpers(tmp_path):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    for session_id, title in (("helper01", "Learn how you work"), ("worker01", "Fix the parser")):
        harness.store.create_session(session_id, WORKER, title, "idle", cwd="")
        harness.store.update_session(session_id, closed_at=now())
    harness.store.set("learner:helper01", "1")
    handover = runtime.handover()
    assert "Fix the parser" in handover
    assert "Learn how you work" not in handover


# R10: a credit limit under Copilot's minimum is refused, not changed.


def test_a_credit_limit_under_the_minimum_is_refused(tmp_path):
    with pytest.raises(ValidationError, match="at least 30"):
        Settings(worker_credit_limit=5)
    assert Settings(worker_credit_limit=0).worker_credit_limit == 0
    saved = Settings.model_validate({"worker_credit_limit": 5}, context=AS_SAVED)
    assert saved.worker_credit_limit == 30, "saved by an earlier version: Copilot's minimum"
    with signed_in(tmp_path) as http:
        assert http.post("/api/auth", json={"token": TOKEN}, headers=ORIGIN).status_code == 200
        http.headers["X-Chief-Key"] = TOKEN
        wrong = http.post("/api/settings", json={"worker_credit_limit": 5}, headers=ORIGIN)
        assert wrong.status_code == 422
        [problem] = wrong.json()["detail"]
        assert problem["loc"] == ["body", "worker_credit_limit"]
        assert "Credit limit per worker: at least 30" in problem["msg"]
        assert http.get("/api/state").json()["settings"]["worker_credit_limit"] == 0
