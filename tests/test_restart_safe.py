"""No message is lost to a restart (0.1.61): a turn in flight as Chief of Staff stops, or as it
crashes, gets what it was given in that turn again, once, as soon as it is open again; never
what its agent's own record shows it finished. On Copilot, Codex and Claude, the Router's and a
worker's alike (Copilot is the harness's fake client; Codex and Claude the stand-in CLIs on the
PATH, under the real supervisor). A resume that is given up is not the end: the one recovery
path tries it again, the session reconnecting meanwhile, never "working". The stop ends the
agents' processes.

Live, on 0.1.60: the Router on Codex, restarted 3 s after the owner's message, waited out its
whole resume ("Resuming … took over 180 s: given up": resuming it took its own lock, held by
placing it), stayed "Thinking…" with no turn, and the message was never answered."""

import asyncio
import json
import sys
import time
from types import SimpleNamespace

import pytest
from conftest import FakeSession, Harness, call_tool, settle
from test_roles import PASSED, saved, until

from chiefofstaff import backend_claude, backend_codex
from chiefofstaff import runtime as runtime_module
from chiefofstaff.backend import process_table, running
from chiefofstaff.delivery import answered

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Other agents need macOS/Linux")

ASKED = "Wait 3 s. Then reply with just: answered"


@pytest.fixture(autouse=True)
def bounded(monkeypatch):
    """A resume given up after a few seconds (180 live): 0.1.60 gives up, and never answers."""
    monkeypatch.setattr(runtime_module, "SESSION_RESUME", 8.0)


@pytest.fixture
def codex_log(tmp_path, monkeypatch, codex_installed):
    """What Chief of Staff asked the stand-in Codex, one request a line."""
    log = tmp_path / "codex-log.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))

    def read(method: str) -> list[dict]:
        if not log.exists():
            return []
        lines = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        return [entry["params"] for entry in lines if entry.get("method") == method]

    return read


@pytest.fixture
def claude_checked(monkeypatch, claude_on_path):
    async def check(self, folder):
        return dict(PASSED)

    monkeypatch.setattr(backend_claude.ClaudeAgent, "check", check)
    monkeypatch.setattr(backend_claude, "LISTED", {})
    # Each prompt waits a little before Claude takes it: its turn is in flight meanwhile.
    monkeypatch.setenv("FAKE_CLAUDE_GATE_DELAY", "2")


async def router_on(tmp_path, agent: str):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    await saved(runtime, {agent: {"enabled": True}, "roles": {"router": agent}})
    assert runtime.agent(harness.router_id) == agent
    return harness, runtime


async def restarted(harness, **options) -> Harness:
    """Chief of Staff started again on the same data folder, as `start` does: Copilot connects
    (its sessions resumed), then the other agents with sessions at work."""
    again = Harness(harness.store.directory, client=harness.client, **options)
    again.runtime.mark_resuming()
    await connected(again)
    await again.runtime.recover_agents()
    return again


async def connected(harness):
    """Connected, what it started meanwhile left to go on (not waited for, as Harness.connect
    does: a resume Codex is slow to answer, here)."""
    runtime = harness.runtime
    runtime.loop = asyncio.get_running_loop()
    runtime.bus.bind(runtime.loop)
    await runtime.connect()
    return runtime


def owner_message(harness, text: str) -> dict:
    (found,) = [m for m in harness.store.messages(harness.router_id) if m["text"] == text]
    return found


def delivery(runtime, message: dict) -> str:
    message = runtime.store.message(message["id"])
    return runtime.delivery.delivery_for(message["session_id"], [message])[0]


def given(text: str, prompts: list[str]) -> int:
    return sum(text in prompt for prompt in prompts)


def started_with(read, thread: str) -> list[str]:
    """What each turn Codex was asked to start in a thread was given."""
    return [
        "\n".join(str(item.get("text") or "") for item in params.get("input") or [])
        for params in read("turn/start")
        if params.get("threadId") == thread
    ]


# The Router: its message answered after a restart mid-turn, given again once


async def test_the_router_on_codex_restarted_mid_turn_answers_the_owner_once_resumed(
    tmp_path, codex_log
):
    harness, runtime = await router_on(tmp_path, "codex")
    router = harness.router_id
    await runtime.owner_send(router, ASKED)
    await until(lambda: (s := runtime.states.handle(router)) and s.turn, 30)
    await asyncio.sleep(1)
    await runtime.stop()
    thread = runtime.copilot_id(router)
    assert harness.store.get(f"cutshort:{router}"), "what its turn was given: marked to go again"
    again = await restarted(harness)
    runtime = again.runtime
    try:
        await until(lambda: ("assistant", "answered") in again.kinds(router), 30)
        turns = started_with(codex_log, thread)
        assert given(ASKED, turns) == 2, "given again once, after the restart"
        assert "Chief of Staff restarted while you were working" in turns[-1]
        await until(lambda: again.store.session(router)["status"] == "idle", 30)
        assert delivery(runtime, owner_message(again, ASKED)) == "processed"
        await asyncio.sleep(1)
        assert given(ASKED, started_with(codex_log, thread)) == 2, "never a third time"
        assert not again.store.get(f"cutshort:{router}")
    finally:
        await runtime.stop()


async def test_the_router_on_claude_restarted_mid_turn_answers_the_owner_once_resumed(
    tmp_path, claude_checked, monkeypatch
):
    log = tmp_path / "claude-log.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    harness, runtime = await router_on(tmp_path, "claude")
    router = harness.router_id
    await runtime.owner_send(router, ASKED)
    await asyncio.sleep(1)  # Waiting for Claude to take it (its gate's delay).
    await runtime.stop()
    again = await restarted(harness)
    runtime = again.runtime
    try:
        await until(lambda: ("assistant", "answered") in again.kinds(router), 30)
        prompts = [line for line in log.read_text().splitlines() if ASKED in line]
        assert len(prompts) == 2, "given again once, after the restart"
        await until(lambda: again.store.session(router)["status"] == "idle", 30)
        assert delivery(runtime, owner_message(again, ASKED)) == "processed"
    finally:
        await runtime.stop()


async def test_the_router_on_copilot_restarted_mid_turn_is_given_the_message_again(harness):
    runtime = await harness.connect()
    router = harness.router_id
    await runtime.owner_send(router, "What changed in the build?")
    session = harness.session(router)
    assert given("What changed in the build?", session.sent) == 1
    asked = owner_message(harness, "What changed in the build?")
    await runtime.stop()
    # Not resumed yet: the owner's message waits to go again; the Router is not at work.
    again = Harness(harness.store.directory, client=harness.client)
    runtime = again.runtime
    runtime.mark_resuming()
    assert delivery(runtime, asked) == "queued"
    view = runtime.view(again.store.session(router))
    assert (view["status"], view["activity"]) == ("resuming", "Reconnecting to Copilot…")
    try:
        await again.connect()
        await settle(runtime)
        sent = again.session(router).sent
        assert given("What changed in the build?", sent) == 2, "once more, in full"
        assert sent[-1].startswith('<message from="system" type="restart">\nChief of Staff')
        assert delivery(runtime, asked) != "queued"
        assert runtime.view(again.store.session(router))["status"] == "working"
        again.session(router).emit("session.idle")
        await settle(runtime)
        assert given("What changed in the build?", again.session(router).sent) == 2
    finally:
        await runtime.stop()


async def test_what_the_agent_finished_before_the_stop_was_seen_is_not_given_again(
    harness, monkeypatch
):
    """Its record (Copilot's events) shows a final answer after the message: its turn ended
    before Chief of Staff saw it (it crashed). Nothing goes again; it is at rest."""
    runtime = await harness.connect()
    router = harness.router_id
    await runtime.owner_send(router, "Is the deploy done?")
    await runtime.stop()
    events = FakeSession.get_events

    async def answered(self):
        said = SimpleNamespace(content="Yes, it went out at 9:02.", tool_requests=None)
        return [
            *await events(self),
            SimpleNamespace(type=SimpleNamespace(value="assistant.message"), data=said),
        ]

    monkeypatch.setattr(FakeSession, "get_events", answered)
    sent = len(harness.session(router).sent)
    again = Harness(harness.store.directory, client=harness.client)
    runtime = await again.connect()
    try:
        await settle(runtime)
        assert len(again.session(router).sent) == sent, "nothing given again"
        assert again.store.session(router)["status"] == "idle"
        assert delivery(runtime, owner_message(again, "Is the deploy done?")) == "processed"
    finally:
        await runtime.stop()


def test_what_an_agents_record_counts_as_finished():
    """A final answer after the message, in a turn that ended: Codex's says so (`final`);
    Copilot's and Claude's ask for no tool. Not a subagent's within the turn, nor one after
    the turn was stopped (Copilot's abort, Claude's interrupt), nor Codex's in a turn cut
    short."""

    def said(kind: str, content: str = "", **data) -> tuple[str, SimpleNamespace]:
        return kind, SimpleNamespace(content=content, **data)

    asked = said("user.message", "Is the deploy done?")
    yes = said("assistant.message", "Yes.", tool_requests=None)
    assert answered([asked, yes], 0)
    assert not answered([asked], 0)
    assert not answered([asked, said("assistant.message", "Looking.", tool_requests=[{}])], 0)
    child = said("assistant.message", "Found it.", tool_requests=None, parent_tool_call_id="c")
    assert not answered([asked, child], 0), "a subagent's, not its answer"
    assert not answered([asked, said("abort"), yes], 0)
    stopped = said("user.message", "[Request interrupted by user]")
    assert not answered([asked, stopped, yes], 0)
    assert answered([asked, said("assistant.message", "Yes.", final=True)], 0)
    assert not answered([asked, said("assistant.message", "Checking.", final=False)], 0)


# Workers


async def test_a_codex_worker_restarted_mid_turn_goes_on_with_its_task_once(tmp_path, codex_log):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    await saved(runtime, {"codex": {"enabled": True}, "new_workers": "codex"})
    made = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="Port it",
        instructions="Wait 3 s. Then report done: Ported.", why_new="t",
    )  # fmt: skip
    worker = json.loads(made.text_result_for_llm)["session_id"]
    assert runtime.agent(worker) == "codex"
    await until(lambda: (s := runtime.states.handle(worker)) and s.turn, 30)
    thread = runtime.copilot_id(worker)
    await runtime.stop()
    again = await restarted(harness)
    runtime = again.runtime
    try:
        await until(lambda: again.store.session(worker)["status"] == "done", 30)
        assert given("Wait 3 s. Then report done", started_with(codex_log, thread)) == 2
    finally:
        await runtime.stop()


async def test_a_worker_that_asked_the_owner_mid_turn_goes_on_after_a_restart(harness):
    """Waiting for the owner while its turn went on (its status says waiting, not working):
    its turn is cut short all the same, and goes on after the restart (0.1.60 left it)."""
    runtime = await harness.connect()
    made = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="Pick a name",
        instructions="Find a name for the export job.", why_new="t",
    )  # fmt: skip
    worker = json.loads(made.text_result_for_llm)["session_id"]
    await settle(runtime)
    runtime.update_session(worker, status="waiting")
    runtime.states[worker].busy = True  # Its turn goes on.
    await runtime.stop()
    again = Harness(harness.store.directory, client=harness.client)
    runtime = await again.connect()
    try:
        await settle(runtime)
        sent = again.session(worker).sent
        assert given("Find a name for the export job.", sent) == 2
        assert sent[-1].startswith('<message from="system" type="restart">')
    finally:
        await runtime.stop()


# A resume given up is tried again; reconnecting meanwhile, never "working"


@pytest.fixture
def codex_slow(monkeypatch):
    """Codex's resume of a thread does not answer until `released` (as slow as the live one,
    past the bound); then it does."""
    resume = backend_codex.CodexAgent.resume_session
    released = asyncio.Event()
    calls: list[str] = []

    async def slow(self, session_id, **options):
        calls.append(session_id)
        await released.wait()
        return await resume(self, session_id, **options)

    monkeypatch.setattr(backend_codex.CodexAgent, "resume_session", slow)
    monkeypatch.setattr(runtime_module, "SESSION_RESUME", 1.0)
    return SimpleNamespace(released=released, calls=calls)


async def test_a_resume_given_up_is_tried_again_and_the_router_answers(
    tmp_path, codex_log, codex_slow, monkeypatch
):
    monkeypatch.setattr(runtime_module, "COPILOT_RETRY", 0.5)
    harness, runtime = await router_on(tmp_path, "codex")
    router = harness.router_id
    await runtime.owner_send(router, ASKED)
    await until(lambda: (s := runtime.states.handle(router)) and s.turn, 30)
    await runtime.stop()
    again = Harness(harness.store.directory, client=harness.client)
    runtime = again.runtime
    runtime.mark_resuming()
    shown: list[dict] = []
    publish = runtime.bus.publish

    def publishing(kind, data=None):
        if kind == "session" and isinstance(data, dict) and data.get("id") == router:
            shown.append(data)
        publish(kind, data)

    runtime.bus.publish = publishing
    try:
        await connected(again)
        view = runtime.view(again.store.session(router))
        assert (view["status"], view["activity"]) == ("resuming", "Reconnecting to Codex…")
        await runtime.recover_agents()
        await until(lambda: runtime.unresumed.get(router), 30)  # Given up once.
        view = runtime.view(again.store.session(router))
        assert (view["status"], view["retry"]) == ("resuming", True), "never working"
        assert "working" not in [data["status"] for data in shown]
        codex_slow.released.set()  # Codex answers again: the next try reopens it.
        await until(lambda: ("assistant", "answered") in again.kinds(router), 30)
        assert router not in runtime.reconnecting and router not in runtime.unresumed
        thread = runtime.copilot_id(router)
        assert given(ASKED, started_with(codex_log, thread)) == 2, "given again once"
    finally:
        codex_slow.released.set()
        await runtime.stop()


async def test_stop_while_reconnecting_keeps_its_message_for_the_next_one(
    tmp_path, codex_log, codex_slow, monkeypatch
):
    """Stop on a session reconnecting: it does not go on by itself; the owner's message it was
    on waits (held) and goes with their next one. Retry tries again at once."""
    monkeypatch.setattr(runtime_module, "COPILOT_RETRY", 600)
    harness, runtime = await router_on(tmp_path, "codex")
    router = harness.router_id
    await runtime.owner_send(router, ASKED)
    await until(lambda: (s := runtime.states.handle(router)) and s.turn, 30)
    await runtime.stop()
    again = await restarted(harness)
    runtime = again.runtime
    try:
        await until(lambda: runtime.unresumed.get(router), 30)
        tries = runtime.unresumed[router]
        runtime.retry_resume(router)
        await until(lambda: runtime.unresumed.get(router, 0) > tries, 30)  # Tried at once.
        await runtime.workers.interrupt(router)
        assert again.store.session(router)["status"] == "idle"
        assert runtime.view(again.store.session(router))["status"] == "idle"
        assert delivery(runtime, owner_message(again, ASKED)) == "held"
        with pytest.raises(ValueError, match="not reconnecting"):
            runtime.retry_resume(router)
        codex_slow.released.set()
        await runtime.owner_send(router, "And thanks.")
        await until(lambda: ("assistant", "answered") in again.kinds(router), 30)
        turns = started_with(codex_log, runtime.copilot_id(router))
        assert given(ASKED, turns) == 2 and "And thanks." in turns[-1], "with the next one"
    finally:
        codex_slow.released.set()
        await runtime.stop()


# Once settled or let rest, a cut turn is never continued again


async def spawned(runtime, harness, instructions: str) -> str:
    made = await call_tool(
        runtime, "spawn_worker", harness.router_id, title=instructions[:30],
        instructions=instructions, why_new="t",
    )  # fmt: skip
    return json.loads(made.text_result_for_llm)["session_id"]


def notes(sent: list[str]) -> int:
    return sum(p.startswith('<message from="system" type="restart">') for p in sent)


async def test_a_cut_turn_the_owners_message_settled_first_is_not_continued_again(
    tmp_path, monkeypatch
):
    """Review (High): three workers cut short; resuming the first two is slow (each just within
    its bound), and the owner writes to the third meanwhile: their message, after its wait,
    settles its cut turn (once). Resumes reached it later still continued it from its old row,
    marking its turn anew: it was sent everything again, with a second note."""
    monkeypatch.setattr(runtime_module, "SESSION_RESUME", 2.0)
    monkeypatch.setattr(runtime_module, "RESUME_WAIT", 0.1)  # Connected while they resume.
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    first, second, third = [await spawned(runtime, harness, f"Port module {n}") for n in "ABC"]
    await settle(runtime)
    await runtime.stop()
    client = harness.client
    resume = client.resume_session

    async def slow(session_id, **options):
        if session_id in {first, second}:
            await asyncio.sleep(1.5)
        return await resume(session_id, **options)

    client.resume_session = slow
    again = Harness(harness.store.directory, client=client)
    runtime = again.runtime
    runtime.mark_resuming()
    try:
        await connected(again)
        await runtime.owner_send(third, "Also port module D.")  # Waits its 2 s, then goes.
        await until(lambda: runtime.resume_job.done(), 30)
        await settle(runtime)
        sent = again.session(third).sent
        assert given('title="Port module C"', sent) == 2, "its task once more, not twice"
        assert given("Also port module D.", sent) == 1
        assert notes(sent) == 1, "one note"
        for worker in (first, second):
            assert notes(again.session(worker).sent) == 1
    finally:
        await runtime.stop()


async def test_a_cut_turn_let_rest_while_others_resume_is_not_continued(tmp_path):
    """Review (High): Stop on a worker reconnecting while another's resume is held: it rests,
    what it was on held. The resume reaching it later sent it its task and a note, working
    again."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    first = await spawned(runtime, harness, "Port module A")
    second = await spawned(runtime, harness, "Port module B")
    await settle(runtime)
    await runtime.stop()
    client = harness.client
    resume, held = client.resume_session, asyncio.Event()

    async def holding(session_id, **options):
        if session_id == first:
            await held.wait()
        return await resume(session_id, **options)

    client.resume_session = holding
    again = Harness(harness.store.directory, client=client)
    runtime = again.runtime
    runtime.mark_resuming()
    try:
        await connected(again)
        before = len(again.session(second).sent)
        await runtime.workers.interrupt(second)
        assert again.store.session(second)["status"] == "idle"
        held.set()
        await until(lambda: runtime.resume_job.done(), 30)
        await settle(runtime)
        assert len(again.session(second).sent) == before, "nothing sent: it rests"
        assert again.store.session(second)["status"] == "idle"
        assert runtime.view(again.store.session(second))["status"] == "idle"
        task = [m for m in again.store.prompts(second) if "Port module B" in m["text"]][-1]
        assert delivery(runtime, task) == "held", "for its next message"
        assert notes(again.session(first).sent) == 1
    finally:
        held.set()
        await runtime.stop()


# The stop ends the agents' processes


def tree(pids: dict[int, str]) -> list[int]:
    table = process_table()
    return [pid for pid, command in pids.items() if table.get(pid, (0, ""))[1] == command]


@pytest.mark.parametrize("agent", ["codex", "claude"])
async def test_the_stop_ends_every_process_of_the_agents_mid_turn(
    tmp_path, request, monkeypatch, agent
):
    """Mid-turn, a CLI that does not end at the end of its input (Codex), or takes its time
    (Claude): the stop ends it and its supervisor, before it returns, a few seconds at most."""
    if agent == "codex":
        request.getfixturevalue("codex_log")
        monkeypatch.setenv("FAKE_CODEX_HOLDS_ON", "1")
        monkeypatch.setattr(backend_codex, "STOP_GRACE", 1.0)
    else:
        request.getfixturevalue("claude_checked")
        monkeypatch.setenv("FAKE_CLAUDE_EXIT_DELAY", "60")
    harness, runtime = await router_on(tmp_path, agent)
    router = harness.router_id
    await runtime.owner_send(router, ASKED)
    await asyncio.sleep(1)
    backend, _ = runtime.connection(agent)
    pids = await asyncio.to_thread(backend.processes)
    assert tree(pids), "its supervisor and the CLI run"
    began = time.monotonic()
    await runtime.stop()
    assert time.monotonic() - began < 20
    left = tree(pids)
    assert not left, f"still running: {left}"
    assert not [pid for pid in pids if running(pid) and pid in process_table()]
