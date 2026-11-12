"""Backends beside Copilot's: the registry, each backend's own recovery, and the runtime's paths
that do not depend on the agent, on scripted agents as their adapters would show them
(scripted.CODEX_LIKE: steers, exact exit codes, ids it chooses; CLAUDE_LIKE: no steering, no exit
codes). Copilot's sessions are every session that names no other agent, and stay as they were
(test_copilot_pins.py)."""

import asyncio
import json
import re
import shutil
import sys

import pytest
from conftest import Harness, call_tool, settle
from test_runtime import at

from chiefofstaff import delivery, events
from chiefofstaff import runtime as runtime_module
from chiefofstaff.backend import COPILOT_AGENT
from chiefofstaff.delivery import HELD_STOPPED
from chiefofstaff.models import Settings
from chiefofstaff.roles import WORKER
from chiefofstaff.runtime import AgentConnection
from chiefofstaff.scripted import (
    CLAUDE_LIKE,
    CODEX_LIKE,
    AgentLikeBackend,
    AgentLikeSession,
    Background,
    CallTool,
    Child,
    Say,
    Shell,
    Wait,
)

PROFILES = {"codex-like": CODEX_LIKE, "claude-like": CLAUDE_LIKE}


@pytest.fixture(autouse=True)
def promptly(monkeypatch):
    monkeypatch.setattr(events, "HELD_AFTER_IDLE", 0)
    monkeypatch.setattr(events, "HELD_AFTER_TURN", 0)
    monkeypatch.setattr(runtime_module, "PROCESS_CHECK", 0.02)


async def brain(session, prompt):
    """Workers' scripts, by what the first message they get says."""
    found = re.search(r"<message [^>]*>\n(.*?)\n</message>", prompt, re.DOTALL)
    said = found.group(1) if found else prompt
    if said == "Fix the login test":
        yield Shell("pytest -q", output="3 passed in 0.1s")
        yield Child([Shell("ruff check .", output="All checks passed!")])
        yield CallTool("report", {"status": "done", "summary": "Fixed."})
    elif said.startswith("Preview the site"):
        yield Background("npm run dev", seconds=session.backend.serve, exit_code=3)
        if said.endswith("slowly"):
            yield Wait(0.3)
        yield Say("Serving.")
    elif said == "Watch the build":
        yield Background("npm run watch", seconds=1.0, wakes=True)
        yield Say("Watching.")
    elif said == "Think it over":
        yield Wait(0.3)
        yield Say("Thought.")
    elif said == "Ponder at length":
        yield Wait(30)
    elif said.endswith("ended."):
        yield Wait(0.5)  # Its model at work: what stops it comes before it says anything.
        yield Say("It ended; going on.")
    else:
        yield Say("Noted.")


def on(harness: Harness, name: str, histories: dict | None = None) -> list[AgentLikeBackend]:
    """A scripted agent in the registry: workers made from now on run on it (an `agent:` entry
    with the row). Its backends, one a start; they keep their sessions in `histories`, as the
    agent keeps them on disk across its processes."""
    started: list[AgentLikeBackend] = []
    kept = {} if histories is None else histories

    def start():
        backend = AgentLikeBackend(brain, PROFILES.get(name, CODEX_LIKE))
        backend.histories = kept
        backend.serve = 3600.0  # type: ignore[attr-defined]
        started.append(backend)
        return backend

    harness.runtime.agents[name] = AgentConnection(start)
    create = harness.store.create_session

    def creating(session_id, role, *args, **kwargs):
        row = create(session_id, role, *args, **kwargs)
        if role == WORKER:
            harness.store.set(f"agent:{session_id}", name)
        return row

    harness.store.create_session = creating  # type: ignore[method-assign]
    return started


async def until(check, seconds: float = 10.0):
    for _ in range(int(seconds / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


async def spawn(runtime, harness, title) -> str:
    result = await call_tool(
        runtime, "spawn_worker", harness.router_id, title=title, instructions=title, why_new="t"
    )
    return json.loads(result.text_result_for_llm)["session_id"]


def live(runtime, worker) -> AgentLikeSession:
    return runtime.states.handle(worker)


def in_turn(runtime, worker) -> bool:
    session = live(runtime, worker)
    return bool(session and session.turn and not session.turn.done())


FILED = ("notice", "It stopped again without a report: filed as one.")


def settled(harness, runtime, worker) -> bool:
    """A worker that answers without a report is through with its turns: also the one it is
    asked for a report in (between the two it is in no turn, yet not through)."""
    return FILED in harness.kinds(worker) and not in_turn(runtime, worker)


def received(session) -> list[str]:
    return [text for who, text in session.history if who == "user"]


def commands(harness, worker) -> list[tuple]:
    rows = harness.store.all(
        "SELECT command, exit_code, ended_at IS NOT NULL AS ended FROM commands "
        "WHERE session_id=? ORDER BY id",
        (worker,),
    )
    return [(row["command"], row["exit_code"], bool(row["ended"])) for row in rows]


def running_command(harness, worker, command):
    harness.store.execute(
        "INSERT INTO commands(session_id, run_id, call_id, command, kind, started_at) "
        "VALUES (?, '', ?, ?, '', '2026-01-01T00:00:00')",
        (worker, command, command),
    )


async def test_sessions_that_name_no_agent_are_copilots_and_nothing_names_one(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness, "Fix the login test")
    row = harness.store.session(worker)
    assert runtime.backends == {"copilot": harness.client}
    assert (runtime.agent(worker), runtime.backend_for(row)) == ("copilot", harness.client)
    assert runtime.connection("copilot") == (harness.client, runtime.connections)
    assert not harness.store.keys("agent:"), "nothing names an agent"

    # A row naming an agent this Chief of Staff does not run is never run on Copilot instead.
    harness.store.create_session("elsewhere-1", WORKER, "Other", "idle", cwd=row["cwd"])
    harness.store.set("agent:elsewhere-1", "unknown")
    harness.store.set("created:elsewhere-1", "1")
    created, resumed = list(harness.client.created), list(harness.client.resumed)
    with pytest.raises(RuntimeError, match="runs on unknown, which is not available"):
        await runtime.session_for("elsewhere-1")
    with pytest.raises(RuntimeError, match=r"agent \(unknown\) is not running"):
        runtime.backend_for(harness.store.session("elsewhere-1"))
    assert (harness.client.created, harness.client.resumed) == (created, resumed)
    assert runtime.connection("unknown") == (None, 0)


async def test_with_no_other_agent_nothing_of_one_is_looked_for_imported_or_stored(
    harness, monkeypatch
):
    looked = []
    which = shutil.which

    def looking(name, *args, **kwargs):
        looked.append(name)
        return which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", looking)
    # Imported by another test in this process, an agent's module is taken out to be seen if
    # this run imports it again.
    for name in [name for name in sys.modules if re.search(r"claude|codex|agent_client", name)]:
        monkeypatch.delitem(sys.modules, name)
    runtime = await harness.connect()
    await runtime.recover_agents()
    worker = await spawn(runtime, harness, "Fix the login test")
    await call_tool(runtime, "report", worker, status="done", summary="Fixed.")
    await runtime.stop()
    assert not [name for name in looked if re.search(r"codex|claude", str(name), re.IGNORECASE)]
    assert not [name for name in sys.modules if re.search(r"claude|codex|agent_client", name)]
    assert not harness.store.keys("agent"), "nothing stored about other agents"


async def test_a_worker_of_an_agent_this_one_does_not_run_fails_and_is_never_copilots(harness):
    """As after a downgrade from a release that ran it: at work, it fails at the start; given
    work, it fails to start; Copilot is never asked for it."""
    runtime = await harness.connect()
    on(harness, "gone-agent")  # Workers made now name it ...
    working = await spawn(runtime, harness, "Ponder at length")
    await until(lambda: in_turn(runtime, working))
    await runtime.stop()
    harness.runtime.agents.pop("gone-agent")  # ... which the next start does not run.
    again = Harness(harness.store.directory, client=harness.client)
    runtime = await again.connect()
    await runtime.recover_agents()
    assert again.kinds(working)[-1] == (
        "error",
        "Could not resume after restart: This session runs on gone-agent, which is not available.",
    )
    again.store.set("agent:queued-1", "gone-agent")
    again.store.create_session("queued-1", WORKER, "Queued", "starting", cwd="/tmp")
    runtime.states["queued-1"].busy = True
    await runtime.workers.start(again.store.session("queued-1"))
    assert again.kinds("queued-1")[-1] == (
        "error", "Could not start: This session runs on gone-agent, which is not available."
    )  # fmt: skip
    asked = set(harness.client.created + harness.client.resumed)
    assert not {working, runtime.copilot_id(working), "queued-1"} & asked


@pytest.mark.parametrize("name", PROFILES)
async def test_a_worker_on_another_agent_goes_through_the_same_paths(harness, name):
    runtime = await harness.connect()
    started = on(harness, name)
    worker = await spawn(runtime, harness, "Fix the login test")
    task = runtime.tasks.current(worker)
    await until(lambda: harness.store.session(worker)["status"] == "done")
    [backend] = started
    session = live(runtime, worker)
    assert runtime.backends == {"copilot": harness.client, name: backend}
    assert runtime.backend_for(harness.store.session(worker)) is backend
    assert session.session_id in backend.sessions and worker not in harness.client.sessions
    assert runtime.tasks.get(task["id"])["result"] == "Fixed.", "its report, through its tool"
    if PROFILES[name].assigns_ids:
        # Its own id, found as the Router's successive Copilot sessions are.
        assert session.session_id != worker
        assert harness.store.get(f"copilot:{worker}") == session.session_id
        assert harness.store.get(f"alias:{session.session_id}") == worker
    else:
        assert session.session_id == worker and not harness.store.get(f"copilot:{worker}")
    assert harness.store.get(f"created:{worker}") == "1"
    # The child agent's command is a receipt too; exit codes only where the agent gives them.
    code = 0 if PROFILES[name].exit_codes else None
    assert commands(harness, worker) == [("pytest -q", code, True), ("ruff check .", code, True)]
    # Its usage is not in Copilot's credits, nor does it tell Copilot's default model.
    assert not harness.store.all("SELECT id FROM usage WHERE session_id=?", (worker,))
    assert harness.store.get("copilot_default_model") is None
    assert not [m for m in harness.store.messages(worker) if m["kind"] == "error"]


async def test_a_session_another_agent_made_is_resumed_never_made_again(harness, monkeypatch):
    """Only Copilot's session is made again if it never had a message (`fresh:`): another
    agent's is its agent's to resume."""
    runtime = await harness.connect()
    on(harness, "claude-like")

    async def refused(self, prompt, **kwargs):
        raise RuntimeError("not now")

    monkeypatch.setattr(AgentLikeSession, "send", refused)
    worker = await spawn(runtime, harness, "Fix the login test")
    await until(lambda: harness.store.session(worker)["status"] == "failed")
    assert harness.store.get(f"created:{worker}") == "1"
    assert not harness.store.get(f"fresh:{runtime.copilot_id(worker)}")


async def test_a_worker_that_cannot_steer_reads_a_message_when_its_turn_ends(harness):
    runtime = await harness.connect()
    on(harness, "claude-like")
    worker = await spawn(runtime, harness, "Think it over")
    await until(lambda: in_turn(runtime, worker) and received(live(runtime, worker)))
    session = live(runtime, worker)
    await runtime.owner_send(worker, "Also check the docs.")
    assert "Also check the docs." in session.inbox[0] and not session.steering, "it waits"
    await until(lambda: len(received(session)) == 2)
    assert [text for who, text in session.history if who == "assistant"][:1] == ["Thought."]
    assert "Also check the docs." in received(session)[1], "read once its turn ended"
    await until(lambda: harness.store.session(worker)["status"] != "working")


@pytest.mark.parametrize("name", PROFILES)
async def test_a_command_it_leaves_running_holds_it_and_stop_ends_it(harness, name):
    runtime = await harness.connect()
    on(harness, name)
    worker = await spawn(runtime, harness, "Preview the site")
    await until(lambda: runtime.states[worker].held)
    session = live(runtime, worker)
    assert runtime.view(harness.store.session(worker))["background"]
    assert harness.store.session(worker)["status"] != "working"
    assert commands(harness, worker) == [("npm run dev", None, False)], "running, not exit 0"

    await runtime.owner_send(worker, "Is it up?")
    await until(lambda: "Is it up?" in received(session)[-1])
    await until(lambda: runtime.states[worker].held)
    await runtime.workers.interrupt(worker)
    assert not session.left_running, "Stop ended what it left running"
    await until(lambda: commands(harness, worker) == [("npm run dev", None, True)])
    assert ("notice", "Stopped by you.") in harness.kinds(worker)


@pytest.mark.parametrize("name", PROFILES)
async def test_a_command_it_left_running_settles_its_receipt_by_its_own_end(harness, name):
    runtime = await harness.connect()
    started = on(harness, name)
    worker = await spawn(runtime, harness, "Fix the login test")
    await until(lambda: harness.store.session(worker)["status"] != "working")
    started[0].serve = 0.2  # type: ignore[attr-defined]
    await runtime.owner_send(worker, "Preview the site")
    await until(lambda: len(commands(harness, worker)) == 3 and commands(harness, worker)[-1][2])
    code = 3 if PROFILES[name].exit_codes else None
    assert commands(harness, worker)[-1] == ("npm run dev", code, True)
    await until(lambda: not runtime.states[worker].held)


async def test_what_it_queued_as_its_turn_ended_is_taken_back_from_its_own_queue(harness):
    """Its turn ended with a server left running while its next turn waits to be admitted: what
    it queued is taken back and sent at once, as from Copilot's queue (Delivery.take_back)."""
    runtime = await harness.connect()
    started = on(harness, "claude-like")
    worker = await spawn(runtime, harness, "Preview the site slowly")
    await until(lambda: in_turn(runtime, worker) and live(runtime, worker).left_running)
    session = live(runtime, worker)
    gate = asyncio.Event()

    async def admit(asking):
        await gate.wait()
        return ""

    started[0].admit = admit
    sent = []
    send = session.send

    async def sending(prompt, **kwargs):
        sent.append(prompt)
        return await send(prompt, **kwargs)

    session.send = sending  # type: ignore[method-assign]
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    task="new", why_new="t", earlier="keep", title="Docs",
                    text="Then write the docs.")  # fmt: skip
    await until(lambda: len(sent) == 2)
    assert sent[0] == sent[1] and "Then write the docs." in sent[1], "taken back, sent again"
    assert session.left_running, "its server goes on"
    assert HELD_STOPPED not in [text for _, text in harness.kinds(worker)]
    gate.set()
    await until(lambda: any("Then write the docs." in text for text in received(session)))
    assert sum("Then write the docs." in text for text in received(session)) == 1


async def test_a_message_its_agent_gave_back_is_sent_again(harness, monkeypatch):
    """Refused admission at its turn's end, the agent gives what it queued back: neither shown
    nor queued, it is sent again (as one Copilot lost)."""
    monkeypatch.setattr(delivery, "ECHO_CHECK", 0.05)
    runtime = await harness.connect()
    started = on(harness, "claude-like")
    worker = await spawn(runtime, harness, "Think it over")
    await until(lambda: in_turn(runtime, worker) and received(live(runtime, worker)))
    session = live(runtime, worker)
    refused = []

    async def refuse(asking):
        refused.append(asking)
        started[0].admit = None  # Once.
        return "Refused in this test."

    started[0].admit = refuse
    await runtime.owner_send(worker, "And the changelog.")
    await until(lambda: any("And the changelog." in text for text in received(session)))
    assert refused == [session]


async def test_a_stopped_worker_its_agent_starts_again_is_stopped_again(harness):
    runtime = await harness.connect()
    on(harness, "codex-like")
    worker = await spawn(runtime, harness, "Watch the build")
    await until(lambda: runtime.states[worker].held)
    session = live(runtime, worker)

    async def too_late():
        raise RuntimeError("the agent did not answer")

    session.stop_background = too_late  # type: ignore[method-assign]
    await runtime.workers.interrupt(worker)
    assert session.left_running, "its command goes on"
    await until(lambda: harness.kinds(worker)[-1][1].startswith("It started again on its own"))
    await until(lambda: not in_turn(runtime, worker))
    assert harness.store.session(worker)["status"] == "idle"
    assert ("assistant", "It ended; going on.") not in session.history


async def test_a_worker_at_work_its_agent_wakes_goes_on(harness):
    runtime = await harness.connect()
    on(harness, "codex-like")
    worker = await spawn(runtime, harness, "Watch the build")
    await until(lambda: live(runtime, worker) is not None)
    session = live(runtime, worker)
    await until(lambda: ("assistant", "It ended; going on.") in session.history)
    assert not any(text.startswith("It started again") for _, text in harness.kinds(worker))


async def test_copilots_model_fallbacks_are_not_another_agents(harness):
    runtime = await harness.connect()
    on(harness, "codex-like")
    worker = await spawn(runtime, harness, "Ponder at length")
    await until(lambda: in_turn(runtime, worker))
    for refused in (
        "Execution failed: The requested model is not supported.",
        "Execution failed: Reasoning effort 'high' is not supported for model 'gpt-6'.",
    ):
        runtime.update_session(worker, status="working")
        live(runtime, worker).emit("session.error", message=refused)
        await settle(runtime)
        assert harness.kinds(worker)[-1] == ("error", refused)
        assert harness.store.session(worker)["status"] == "failed"
    assert not {"refused_models", f"fallback:{worker}", f"effortcleared:{worker}"} & set(
        harness.store.keys("")
    )


@pytest.mark.parametrize(
    ("error", "refused"),
    [
        ('Model "gpt-6" is not available.', "model"),
        ('Reasoning effort is not supported when using the "auto" model.', "reasoning_effort"),
    ],
)
async def test_a_start_another_agent_refuses_is_not_copilots_to_fall_back_from(
    tmp_path, monkeypatch, error, refused
):
    harness = Harness(tmp_path / "data", Settings(model="gpt-6", role_efforts={WORKER: "high"}))
    runtime = await harness.connect()
    on(harness, "claude-like")
    # The model and effort chosen for that agent (Copilot's are not its own: agent_options).
    chosen = runtime.agent_options
    monkeypatch.setattr(
        runtime,
        "agent_options",
        lambda row: chosen(row) | {"model": "gpt-6", "reasoning_effort": "high"},
    )
    create = AgentLikeBackend.create_session

    async def refuse(self, **options):
        if refused in options:
            raise RuntimeError(error)
        return await create(self, **options)

    monkeypatch.setattr(AgentLikeBackend, "create_session", refuse)
    worker = await spawn(runtime, harness, "Fix the login test")
    await until(lambda: harness.store.session(worker)["status"] == "failed")
    assert harness.kinds(worker)[-1] == ("error", f"Could not start: {error}")
    assert not {"refused_models", f"fallback:{worker}", "effort_unsupported_noted"} & set(
        harness.store.keys("")
    )


async def test_a_terminal_and_copilots_logs_are_copilots_only(harness):
    runtime = await harness.connect()
    on(harness, "codex-like")
    worker = await spawn(runtime, harness, "Fix the login test")
    await until(lambda: harness.store.session(worker)["status"] == "done")
    await runtime.detach(worker)
    thread = runtime.copilot_id(worker)
    typed = {
        "type": "user.message",
        "data": {"content": "Typed in a terminal"},
        "timestamp": at(5),
    }
    usage = {"type": "session.usage_record", "data": {"usage": {"model": "m", "inputTokens": 9}}}
    harness.client.logs[thread] = [typed, usage]
    harness.client.in_use = {thread: 4242}
    await runtime.workers.sync_all_usage()
    assert not harness.store.all("SELECT id FROM usage WHERE session_id=?", (worker,))
    await runtime.owner_send(worker, "Ship it")
    assert "Typed in a terminal" not in [m["text"] for m in harness.store.messages(worker)]
    assert not harness.store.get(f"refusedopen:{worker}"), "not open in Copilot: not refused"
    await until(lambda: any("Ship it" in text for text in received(live(runtime, worker))))


async def test_reopening_another_agents_worker_asks_that_agent(harness):
    runtime = await harness.connect()
    started = on(harness, "codex-like")
    worker = await spawn(runtime, harness, "Fix the login test")
    await until(lambda: harness.store.session(worker)["status"] == "done")
    await runtime.workers.close(worker, by="you")
    asked = []
    lookup = started[0].get_session_metadata

    async def metadata(session_id):
        asked.append(session_id)
        return await lookup(session_id)

    started[0].get_session_metadata = metadata  # type: ignore[method-assign]
    await runtime.workers.reopen(worker)
    assert harness.store.session(worker)["closed_at"] is None
    assert asked == [runtime.copilot_id(worker)] != [worker], "by its own id"


async def test_a_send_its_agent_failed_is_said_and_one_its_reconnect_cut_short_waits(harness):
    runtime = await harness.connect()
    on(harness, "claude-like")
    worker = await spawn(runtime, harness, "Fix the login test")
    await until(lambda: harness.store.session(worker)["status"] == "done")
    session = live(runtime, worker)

    async def failing(prompt, **kwargs):
        raise RuntimeError("bad request")

    session.send = failing  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="Could not deliver the message: bad request"):
        await runtime.owner_send(worker, "One thing")
    assert harness.kinds(worker)[-1] == ("error", "Delivery failed: bad request.")

    async def reconnected(prompt, **kwargs):
        await runtime.disconnect_agent("claude-like")
        raise RuntimeError("the thread is gone")

    session.send = reconnected  # type: ignore[method-assign]
    await runtime.owner_send(worker, "One more thing")
    assert harness.kinds(worker)[-1] == (
        "notice",
        "claude-like stopped; starting it again. Your message goes out when it is back.",
    )


@pytest.mark.parametrize("made", [False, True])
async def test_a_start_its_agents_reconnect_cuts_short_is_requeued_once_then_starts(
    harness, monkeypatch, made
):
    """Its agent reconnected while its session was being made (`made`: it was made, on the
    connection that ended): not its failure. It waits in the queue, and starts on the next
    connection."""
    runtime = await harness.connect()
    started = on(harness, "claude-like")
    create = AgentLikeBackend.create_session
    cut = []

    async def reconnecting(self, **options):
        session = await create(self, **options) if made else None
        if cut:
            return session or await create(self, **options)
        cut.append(session)
        await runtime.disconnect_agent("claude-like")
        if session is None:
            raise RuntimeError("the agent went away")
        return session

    monkeypatch.setattr(AgentLikeBackend, "create_session", reconnecting)
    starts = []
    start = runtime.workers.start

    async def starting(row):
        starts.append(row["id"])
        await start(row)

    monkeypatch.setattr(runtime.workers, "start", starting)
    worker = await spawn(runtime, harness, "Fix the login test")
    await until(lambda: harness.store.session(worker)["status"] == "done")
    assert len(starts) == 2 and len(started) == 2, "requeued once, started on the next one"
    if made:
        assert cut[0].closed, "let go of: it belongs to the connection that ended"
    assert harness.kinds(worker)[-1] == ("report", "Fixed.")


async def test_a_first_message_its_agent_never_starts_on_is_said(harness, monkeypatch):
    """As for Copilot (Delivery.check_started): its agent shows what it takes up."""
    monkeypatch.setattr(delivery, "START_CHECK", 0.05)
    send = AgentLikeSession.send

    async def swallowed(self, prompt, **kwargs):
        if "Never start" not in prompt:
            return await send(self, prompt, **kwargs)
        return "m-1"

    monkeypatch.setattr(AgentLikeSession, "send", swallowed)
    runtime = await harness.connect()
    on(harness, "codex-like")
    worker = await spawn(runtime, harness, "Never start")
    await until(lambda: harness.store.session(worker)["status"] == "failed")
    assert harness.kinds(worker)[-1][1].startswith("Copilot took the message but never started")


def cannot_start(harness, name) -> list[str]:
    """From now on the agent's backend cannot start (its CLI is gone): the reasons given."""
    tried = []

    class Missing(AgentLikeBackend):
        async def start(self):
            tried.append("codex")
            raise FileNotFoundError("codex: command not found")

    harness.runtime.agents[name].start = lambda: Missing(brain, PROFILES[name])
    return tried


async def test_an_agent_that_cannot_start_fails_a_start_with_why_once(harness):
    """Its workers' failures stay theirs (and are said), never a loop of starts."""
    runtime = await harness.connect()
    on(harness, "codex-like")
    tried = cannot_start(harness, "codex-like")
    worker = await spawn(runtime, harness, "Fix the login test")
    await until(lambda: harness.store.session(worker)["status"] != "queued", 2)
    await asyncio.sleep(0.2)
    assert tried == ["codex"], "tried once"
    assert harness.store.session(worker)["status"] == "failed"
    assert harness.kinds(worker)[-1] == ("error", "Could not start: codex: command not found")
    assert runtime.tasks.current(worker)["status"] == "failed"

    # One it made before, given work again while its agent cannot start: the same.
    on(harness, "claude-like")
    made = await spawn(runtime, harness, "Fix the login test")
    await until(lambda: harness.store.session(made)["status"] == "done")
    await runtime.disconnect_agent("claude-like")
    again = cannot_start(harness, "claude-like")
    runtime.update_session(made, status="starting")
    runtime.states[made].busy = True
    await runtime.workers.start(harness.store.session(made))
    await asyncio.sleep(0.2)
    assert again == ["codex"] and harness.store.session(made)["status"] == "failed"
    with pytest.raises(RuntimeError, match="codex: command not found"):
        await runtime.owner_send(made, "Try again")
    assert again == ["codex", "codex"], "once a message"


async def test_each_backends_reconnect_leaves_the_others_sessions_alone(harness):
    runtime = await harness.connect()
    copilot_worker = await spawn(runtime, harness, "Copilot's work")
    started = on(harness, "codex-like")
    agent_worker = await spawn(runtime, harness, "Preview the site")
    await until(lambda: runtime.states[agent_worker].held)
    agent_session = live(runtime, agent_worker)
    copilot_session = harness.session(copilot_worker)
    running_command(harness, copilot_worker, "sleep 9")
    running_command(harness, agent_worker, "sleep 9")

    await runtime.reconnect()  # Copilot's.
    assert copilot_session.disconnected and not agent_session.closed
    assert runtime.states.handle(agent_worker) is agent_session
    assert runtime.states[agent_worker].held and agent_session.left_running
    assert commands(harness, agent_worker)[-1] == ("sleep 9", None, False)
    assert commands(harness, copilot_worker)[-1] == ("sleep 9", None, True)
    assert runtime.connection("codex-like") == (started[0], 0)

    copilot_session = await runtime.session_for(copilot_worker)
    runtime.states[copilot_worker].busy = True
    running_command(harness, copilot_worker, "sleep 8")
    await runtime.disconnect_agent("codex-like")
    await runtime.connect_agent("codex-like")
    assert runtime.states.handle(copilot_worker) is copilot_session
    assert runtime.states[copilot_worker].busy and not copilot_session.disconnected
    assert commands(harness, copilot_worker)[-1] == ("sleep 8", None, False)
    assert agent_session.closed and runtime.states.handle(agent_worker) is None
    assert not runtime.states[agent_worker].held
    assert commands(harness, agent_worker)[-1] == ("sleep 9", None, True)
    assert runtime.connection("codex-like") == (started[1], 1)


async def test_another_agent_that_stops_by_itself_starts_again_and_its_worker_goes_on(harness):
    runtime = await harness.connect()
    copilot_worker = await spawn(runtime, harness, "Copilot's work")
    started = on(harness, "codex-like")
    worker = await spawn(runtime, harness, "Ponder at length")
    await until(lambda: in_turn(runtime, worker))
    copilot = runtime.connections
    started[0].crashed = True
    await until(lambda: len(started) == 2 and live(runtime, worker) is not None)
    resumed = live(runtime, worker)
    await until(lambda: any("Codex-like was restarted" in text for text in received(resumed)))
    assert runtime.connections == copilot, "Copilot's connection is not touched"
    assert runtime.connection("codex-like") == (started[1], 1)

    # Stopped again, and it cannot start now: its worker at work fails with why, once.
    await until(lambda: settled(harness, runtime, worker))
    await runtime.owner_send(worker, "Ponder at length")
    await until(lambda: in_turn(runtime, worker))
    tried = cannot_start(harness, "codex-like")
    started[1].crashed = True
    await until(lambda: harness.store.session(worker)["status"] == "failed")
    await asyncio.sleep(0.2)
    assert tried == ["codex"] and runtime.connection("codex-like") == (None, 2)
    assert harness.kinds(worker)[-1] == (
        "error",
        "Its agent stopped and could not start again: codex: command not found",
    )
    assert harness.store.session(copilot_worker)["status"] == "working", "only its own"


async def test_a_restart_resumes_another_agents_worker_once_copilot_runs(tmp_path):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    histories: dict = {}
    on(harness, "claude-like", histories)
    worker = await spawn(runtime, harness, "Ponder at length")
    await until(lambda: in_turn(runtime, worker))
    before = live(runtime, worker)
    await runtime.stop()
    assert not runtime.backends and before.closed, "its agent stops with Chief of Staff"
    # It stopped while sending to it.
    marker = {"upto": harness.store.value("SELECT max(id) FROM messages"), "digest": "-"}
    harness.store.set(f"sending:{worker}", json.dumps(marker))

    again = Harness(tmp_path / "data", client=harness.client)
    started = on(again, "claude-like", histories)
    await again.connect()
    assert not started, "Copilot's recovery leaves it alone"
    assert again.store.get(f"sending:{worker}"), "not Copilot's send to check"
    await again.runtime.recover_agents()
    assert len(started) == 1 and not again.store.get(f"sending:{worker}")
    session = live(again.runtime, worker)
    await until(lambda: any("Chief of Staff restarted" in text for text in received(session)))
    await again.runtime.stop()


async def test_an_agents_sessions_at_work_are_known_before_their_status_says_so(harness):
    """An agent that stops and cannot start again fails its sessions at work: one in a turn its
    status does not say yet, or sent a message it has not started on, is at work too (a CI run
    on a slow machine left such a worker idle, its turn gone)."""
    runtime = await harness.connect()
    on(harness, "codex-like")
    store = harness.store
    ids = {}
    for name in ("working", "busy", "awaiting", "resting", "closed"):
        ids[name] = await spawn(runtime, harness, "Noted")
    await until(lambda: all(store.session(key)["status"] != "queued" for key in ids.values()))
    for key in ids.values():
        store.update_session(key, status="idle")
        runtime.states[key].busy, runtime.states[key].awaiting = False, None
    store.update_session(ids["working"], status="working")
    runtime.states[ids["busy"]].busy = True
    runtime.states[ids["awaiting"]].awaiting = ("sent", 1)
    runtime.states[ids["closed"]].busy = True
    store.update_session(ids["closed"], closed_at="2026-11-12T00:00:00+00:00")
    runtime.states[harness.router_id].busy = True  # Copilot's: not this agent's.
    assert sorted(runtime.at_work_on("codex-like")) == sorted(
        [ids["working"], ids["busy"], ids["awaiting"]]
    )
    assert runtime.at_work_on(COPILOT_AGENT) == [harness.router_id]


async def test_an_agent_that_cannot_start_again_fails_a_worker_it_had_not_started_on(harness):
    """A worker sent a message it had not started on as its agent stopped (its status does
    not say working) fails with why when the agent cannot start again: ending the agent's turns
    forgets what was sent, so who was at work is known before."""
    runtime = await harness.connect()
    started = on(harness, "codex-like")
    worker = await spawn(runtime, harness, "Noted")
    await until(lambda: settled(harness, runtime, worker))
    runtime.states[worker].awaiting = ("sent", 1)
    tried = cannot_start(harness, "codex-like")
    started[-1].crashed = True
    await until(lambda: harness.store.session(worker)["status"] == "failed")
    assert tried == ["codex"]
    assert harness.kinds(worker)[-1] == (
        "error",
        "Its agent stopped and could not start again: codex: command not found",
    )


async def test_a_worker_closed_while_its_agent_starts_again_is_left_closed(harness, monkeypatch):
    """At work as its agent stopped, then closed by the owner before the agent's start failed:
    it stays closed, with no error."""
    runtime = await harness.connect()
    started = on(harness, "codex-like")
    worker = await spawn(runtime, harness, "Ponder at length")
    await until(lambda: in_turn(runtime, worker))
    await until(lambda: harness.store.session(worker)["status"] == "working")

    async def closed_then_failed(agent):
        await runtime.workers.close(worker, "you")
        raise FileNotFoundError("codex: command not found")

    monkeypatch.setattr(runtime, "connect_agent", closed_then_failed)
    kinds = len(harness.kinds(worker))
    started[-1].crashed = True
    await until(lambda: harness.store.session(worker)["closed_at"])
    await asyncio.sleep(0.2)
    assert harness.store.session(worker)["status"] != "failed"
    assert "error" not in [kind for kind, _ in harness.kinds(worker)[kinds:]]
