"""Claude's adapter (backend_claude.py) on a stand-in Claude Code (tests/fake_claude.py): what it
relies on of the CLI and the Agent SDK, and what it shows the runtime. The contract tests
(test_backend_contract.py) run the parts every backend shares; these, Claude's own."""

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import SHADOWED, cwd_of, fake_claude, shadowing_repository
from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

from chiefofstaff import backend_claude
from chiefofstaff.backend import copilot_orphans, copilot_records
from chiefofstaff.backend_claude import claude_backend

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Claude workers need macOS/Linux")


class Turn:
    """One session's events; `ended` waits for a turn's end (or what holds it)."""

    def __init__(self, session):
        self.events = []
        self.done = asyncio.Event()
        session.on(self.on_event)

    def on_event(self, event):
        self.events.append(event)
        if event.type.value in {"session.idle", "assistant.idle", "session.error", "abort"}:
            self.done.set()

    def kinds(self):
        """What it showed, its accounting aside (told apart: on_accounting)."""
        accounting = {"agent.usage", "agent.settled"}
        return [e.type.value for e in self.events if e.type.value not in accounting]

    def of(self, kind):
        return [event.data for event in self.events if event.type.value == kind]

    def said(self):
        return [data.content for data in self.of("assistant.message")]

    async def ended(self, seconds=20):
        await asyncio.wait_for(self.done.wait(), seconds)
        self.done.clear()


async def until(check, seconds: float = 20):
    for _ in range(int(seconds * 100)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never came")


class Raw:
    """What Claude says, message by message (the options' on_message)."""

    def __init__(self):
        self.messages = []

    def __call__(self, message):
        self.messages.append(message)

    def blocked(self) -> int:
        """Results of no model request: prompts its gate blocked."""
        return sum(m.get("type") == "result" and m.get("num_turns") == 0 for m in self.messages)


@pytest.fixture
async def agent(tmp_path, monkeypatch):
    command = fake_claude(tmp_path / "fake", monkeypatch)
    agent = claude_backend(command, folder=tmp_path / "agents" / "claude")
    await agent.start()
    yield agent
    await agent.stop()


def approve(request, context):
    return PermissionDecisionApproveOnce()


def options(tmp_path, **more):
    found = {
        "system_message": {"content": "A worker."},
        "tools": [],
        "working_directory": str(tmp_path),
        "folders": [str(tmp_path)],
        "on_permission_request": approve,
        "worker": "w" * 32,
    }
    return found | more


async def test_a_turn_runs_its_tools_and_ends_as_copilots_do(agent, tmp_path):
    calls = []

    def echo(invocation):
        calls.append((invocation.arguments, invocation.session_id))
        return "echoed"

    tool = SimpleNamespace(
        name="echo",
        description="Echo",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        handler=echo,
    )
    session = await agent.create_session(
        session_id="a" * 32, name="t", **options(tmp_path, tools=[tool])
    )
    assert session.session_id == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    turn = Turn(session)
    await session.send("Call the echo tool with text 'hi', then reply with just: done")
    await turn.ended()
    assert calls == [({"text": "hi"}, session.session_id)]
    kinds = turn.kinds()
    assert kinds[:3] == ["user.message", "assistant.turn_start", "tool.execution_start"], kinds
    assert kinds[-1] == "session.idle"
    assert turn.said() == ["done"]
    started = turn.of("tool.execution_start")[0]
    assert (started.tool_name, started.arguments) == ("echo", {"text": "hi"})
    await session.disconnect()


def accounting(turn):
    return [e.data for e in turn.events if e.type.value in {"agent.usage", "agent.settled"}]


async def test_a_refused_command_never_runs_and_says_why(agent, tmp_path):
    asked = []

    def deny(request, context):
        asked.append((request.kind, request.full_command_text, request.shell))
        return PermissionDecisionReject(feedback="Not in this test.")

    session = await agent.create_session(
        session_id="b" * 32, name="t", **options(tmp_path, on_permission_request=deny)
    )
    turn = Turn(session)
    await session.send("Run the shell command `mkdir made-here`, then reply with just: ok")
    await turn.ended()
    assert asked == [("shell", "mkdir made-here", backend_claude.bash_shell())]
    assert not (tmp_path / "made-here").exists()
    (done,) = turn.of("tool.execution_complete")
    assert done.success is False and done.result.content == ""
    assert done.error.message == "PreToolUse:Bash hook error: Not in this test."
    assert done.shell_execution is None, "never ran: no exit code"


@pytest.mark.parametrize(("command", "code"), [("true", 0), ("exit 4", 4)])
async def test_a_commands_exit_code_is_claudes(agent, tmp_path, command, code):
    session = await agent.create_session(session_id="c" * 32, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send(f"Run the shell command `{command}`, then reply with just: ok")
    await turn.ended()
    (done,) = turn.of("tool.execution_complete")
    assert done.shell_execution.exit_code == code and done.success is (code == 0)


async def test_an_edit_asks_with_its_resolved_path_and_a_refused_one_is_not_written(
    agent, tmp_path
):
    asked = []

    def deny(request, context):
        asked.append((request.kind, request.resolved_path))
        return PermissionDecisionReject(feedback="No.")

    session = await agent.create_session(
        session_id="d" * 32, name="t", **options(tmp_path, on_permission_request=deny)
    )
    turn = Turn(session)
    await session.send("Write the file `notes.txt`, then reply with just: ok")
    await turn.ended()
    assert asked == [("write", str(tmp_path / "notes.txt"))]
    assert not (tmp_path / "notes.txt").exists()
    (started,) = turn.of("tool.execution_start")
    assert (started.tool_name, started.arguments) == (
        "create",
        {"path": str(tmp_path / "notes.txt")},
    )


async def test_a_command_left_running_holds_the_turn_and_its_end_wakes_it_once_admitted(
    agent, tmp_path
):
    admitted = []

    async def admit(session, woken=False):
        admitted.append(woken)
        return ""

    agent.admit = admit
    session = await agent.create_session(session_id="e" * 32, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Leave the command `echo up` running for 0.5 s, then reply with just: up")
    await turn.ended()
    held = turn.kinds()[: turn.kinds().index("assistant.idle") + 1]
    assert "session.idle" not in held, "what it left running holds the session"
    (done,) = turn.of("tool.execution_complete")
    shell = done.result.content.removeprefix("<command started in background with shellId: ")
    assert done.result.content.endswith(">") and shell
    assert await session.running_background() == 1
    await turn.ended()  # Its end: the receipt settles, then the session is idle.
    (ended,) = turn.of("system.notification")
    assert (ended.kind.type, ended.kind.shell_id, ended.kind.exit_code) == (
        "shell_completed",
        shell.rstrip(">"),
        3,
    )
    kinds = turn.kinds()
    ending = kinds.index("system.notification")
    assert kinds[ending + 1] == "session.idle", kinds  # (The woken turn may already follow.)
    await turn.ended()  # The turn its end started, admitted as one the worker starts itself.
    assert admitted == [False, True]
    assert turn.kinds()[ending + 2 :] == [
        "assistant.turn_start",
        "assistant.message",
        "session.idle",
    ]
    assert turn.said()[-1] == "noted"
    assert await session.running_background() == 0


async def test_a_wakeup_not_admitted_is_blocked_before_the_model(agent, tmp_path):
    async def admit(session, woken=False):
        return "Stopped by you." if woken else ""

    agent.admit = admit
    raw = Raw()
    session = await agent.create_session(
        session_id="f" * 32, name="t", **options(tmp_path, on_message=raw)
    )
    turn = Turn(session)
    await session.send("Leave the command `echo up` running for 0.3 s, then reply with just: up")
    await turn.ended()
    await turn.ended()
    shown = len(turn.kinds())
    await until(lambda: raw.blocked() == 1)  # Its end's notice, at the gate: blocked.
    await asyncio.sleep(0.05)
    assert len(turn.kinds()) == shown, "blocked: no turn, nothing shown"
    assert turn.said() == ["up"] and not session.backend.turn


async def test_a_wakeup_while_a_message_waits_to_be_admitted_is_admitted_on_its_own(
    agent, tmp_path
):
    # The message's admission is under way (not yet written) as a command it left running ends:
    # the turn that end starts is admitted itself, never begun on the message's admission.
    asked, wakeup = [], asyncio.Event()

    async def admit(session, woken=False):
        asked.append(woken)
        if len(asked) == 1:
            return ""
        if woken:
            wakeup.set()
            return "Stopped by you."
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wakeup.wait(), 5)
        return "Stopped by you."

    agent.admit = admit
    session = await agent.create_session(session_id="f1" * 16, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Leave the command `echo up` running for 0.5 s, then reply with just: up")
    await turn.ended()
    with pytest.raises(RuntimeError, match="Stopped by you"):
        await session.send("Reply with just: later")
    assert asked == [False, False, True], "the wakeup asked for itself"
    await until(lambda: not session.backend.background)
    assert turn.said() == ["up"], "nothing ran that was not admitted"
    assert not session.backend.turn and not session.backend.starting


async def test_a_new_effort_with_a_message_queued_starts_its_turn_in_a_new_process(agent, tmp_path):
    session = await agent.create_session(session_id="c1" * 16, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Run the shell command `sleep 2`, then reply with just: first")
    await until(lambda: "tool.execution_start" in turn.kinds())
    worker, first = session.backend, session.backend.epoch
    await session.set_model("", reasoning_effort="high")
    await session.send("Then reply with just: second")
    assert worker.queue, "it waits for the turn's end"
    await turn.ended(30)
    assert turn.said() == ["first", "second"]
    assert worker.epoch != first and worker.process_effort == "high" and worker.alive()
    assert turn.kinds().count("session.idle") == 1 and agent.workers[worker.worker] is worker
    assert turn.kinds().count("assistant.turn_start") == 2 and not worker.queue


async def test_a_message_sent_while_its_process_restarts_waits_for_it(agent, tmp_path):
    session = await agent.create_session(session_id="c4" * 16, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Run the shell command `sleep 1`, then reply with just: first")
    await until(lambda: "tool.execution_start" in turn.kinds())
    worker = session.backend
    await session.set_model("", reasoning_effort="high")
    await session.send("Then reply with just: second")
    await until(lambda: worker.opening > 0, 30)
    assert await session.send("And then reply with just: third") == "", "queued, not refused"
    for _ in range(3):
        if turn.said() == ["first", "second", "third"]:
            break
        await turn.ended(30)
    assert turn.said() == ["first", "second", "third"]
    assert worker.process_effort == "high" and worker.alive()


@pytest.mark.parametrize("during", ["restart", "admission"])
async def test_a_stop_while_its_turn_starts_stops_it_before_it_begins(agent, tmp_path, during):
    raw, admitting = Raw(), asyncio.Event()
    session = await agent.create_session(
        session_id="c2" * 16, name="t", **options(tmp_path, on_message=raw)
    )
    turn = Turn(session)
    await session.send("Reply with just: ok")
    await turn.ended()
    worker = session.backend
    if during == "restart":
        await session.set_model("", reasoning_effort="high")
    else:
        release = asyncio.Event()

        async def admit(session, woken=False):
            admitting.set()
            await release.wait()
            return ""

        agent.admit = admit
    sending = asyncio.ensure_future(session.send("Reply with just: stopped"))
    if during == "restart":
        await until(lambda: worker.opening > 0)
    else:
        await admitting.wait()
    await session.abort()
    if during == "admission":
        release.set()
    await sending
    await turn.ended()
    assert turn.kinds()[-2:] == ["abort", "session.idle"]
    await until(lambda: raw.blocked() >= 1)  # Its message, at the gate: blocked.
    assert turn.said() == ["ok"], "the turn stopped never began"
    assert not worker.turn and not worker.starting and worker.alive()


async def test_stopping_what_it_left_running_settles_it_without_a_code(agent, tmp_path):
    session = await agent.create_session(session_id="1" * 32, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Leave the command `echo up` running for 60 s, then reply with just: up")
    await turn.ended()
    assert await session.stop_background() == 1
    await turn.ended()
    (ended,) = turn.of("system.notification")
    assert ended.kind.exit_code is None
    assert await session.running_background() == 0 and await session.stop_background() == 0


async def test_abort_ends_the_turn_and_its_command(agent, tmp_path):
    marker = tmp_path / "pid"
    session = await agent.create_session(session_id="2" * 32, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send(f"Run the shell command `echo $$ > {marker}; exec sleep 30`")
    for _ in range(200):
        if marker.exists() and marker.read_text().strip():
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)
    await session.abort()
    await turn.ended()
    assert "abort" in turn.kinds()
    pid = int(marker.read_text())

    def gone():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    await until(gone, 5)
    (done,) = turn.of("tool.execution_complete")
    assert done.success is False and done.shell_execution is None


async def test_a_version_that_does_not_steer_holds_notes_for_the_turns_end(agent, tmp_path):
    agent.checked = {"ran": True, "acting": "", "ask_first": "", "steering": False}
    session = await agent.create_session(session_id="3a" * 16, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Run the shell command `sleep 1`, then reply with just: first")
    for _ in range(100):
        if "tool.execution_start" in turn.kinds():
            break
        await asyncio.sleep(0.02)
    await session.send("Change of plan. Reply with just: second", mode="immediate")
    assert await session.queue_holds() and session.backend.queue
    await turn.ended()
    assert turn.said() == ["first", "second"], "a turn of its own after the first"
    assert turn.kinds().count("assistant.turn_start") == 2 and turn.of("session.idle")


async def test_a_note_sent_during_a_command_steers_the_turn(agent, tmp_path):
    session = await agent.create_session(session_id="3" * 32, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Run the shell command `sleep 1`, then reply with just: first")
    for _ in range(100):
        if "tool.execution_start" in turn.kinds():
            break
        await asyncio.sleep(0.02)
    await session.send("Change of plan. Reply with just: steered", mode="immediate")
    await turn.ended()
    assert turn.said() == ["steered"]
    assert [m.delivery.value for m in turn.of("user.message")] == ["", "steering"]
    assert turn.kinds().count("session.idle") == 1, "one turn"


async def test_what_waits_for_the_turn_is_taken_back_or_starts_once_admitted(agent, tmp_path):
    asked = []

    async def refuse(session, woken=False):
        asked.append(session.session_id)
        return "Refused in this test." if len(asked) > 1 else ""

    agent.admit = refuse
    session = await agent.create_session(session_id="4" * 32, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Run the shell command `sleep 0.5`, then reply with just: first")
    await session.send("Reply with just: second")
    await session.send("Reply with just: third")
    assert await session.queue_holds()
    assert await session.take_queued() == 2
    await session.send("Reply with just: fourth")
    await turn.ended()
    assert turn.said() == ["first"], "refused: given back, not started"
    assert not await session.queue_holds()


async def test_usage_is_told_response_by_response_and_settles_at_the_end(agent, tmp_path):
    told = []
    session = await agent.create_session(
        session_id="5" * 32,
        name="t",
        **options(tmp_path, on_accounting=lambda kind, data: told.append((kind, data))),
    )
    turn = Turn(session)
    await session.send("Run the shell command `true`, then reply with just: ok")
    await turn.ended()
    usages = [data for kind, data in told if kind == "agent.usage"]
    totals = [data.total["totalTokens"] for data in usages]
    assert totals == sorted(totals) and len(totals) >= 2, "counted as it came, never less"
    assert usages[0].restored == backend_claude.counts({}), "a new session restored nothing"
    assert all(data.restored is None for data in usages[1:])
    assert {data.epoch for data in usages} == {session.backend.epoch}
    assert told[-1][0] == "agent.settled"
    # Two responses of the fake's: what Claude's result says, all told.
    assert usages[-1].total == backend_claude.model_totals(
        json.loads(
            (
                Path(os.environ["FAKE_CLAUDE_HOME"]) / "sessions" / f"{session.session_id}.json"
            ).read_text()
        )["totals"]
    )


async def test_a_resumed_session_counts_from_what_it_restored(agent, tmp_path):
    told = []
    worker = options(tmp_path, on_accounting=lambda kind, data: told.append((kind, data)))
    session = await agent.create_session(session_id="6" * 32, name="t", **worker)
    turn = Turn(session)
    await session.send("Remember the word 'pineapple'. Reply with just: ok")
    await turn.ended()
    before = [data for kind, data in told if kind == "agent.usage"][-1].total
    await session.disconnect()
    for _ in range(100):
        if not agent.workers:
            break
        await asyncio.sleep(0.05)
    assert not agent.workers, "let go of at rest: its process ended"
    told.clear()
    resumed = await agent.resume_session(session.session_id, **worker)
    turn = Turn(resumed)
    await resumed.send("What word did I ask you to remember? Reply with just the word.")
    await turn.ended()
    assert turn.said() == ["pineapple"]
    first = next(data for kind, data in told if kind == "agent.usage")
    assert first.restored == before, "its offset: what the resume restored"
    assert first.total["totalTokens"] > before["totalTokens"]
    assert first.epoch == resumed.backend.epoch != session.backend.epoch


async def test_what_is_not_tied_into_chief_of_staff_yet_is_refused_with_why(agent, tmp_path):
    session = await agent.create_session(session_id="7" * 32, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Use Claude's EnterWorktree tool, then reply with just: ok")
    await turn.ended()
    (done,) = turn.of("tool.execution_complete")
    assert done.success is False
    assert "worktrees aren't tied into Chief of Staff yet" in done.error.message
    (notice,) = turn.of("agent.notice")
    assert notice.text.startswith("Refused Claude's EnterWorktree:")


@pytest.mark.parametrize("ask_first", [True, False])
async def test_a_tool_chief_of_staff_does_not_know_runs_only_for_a_worker_acting_alone(
    agent, tmp_path, ask_first
):
    session = await agent.create_session(
        session_id="8" * 32, name="t", **options(tmp_path, ask_first=ask_first)
    )
    turn = Turn(session)
    await session.send("Use Claude's FutureTool tool, then reply with just: ok")
    await turn.ended()
    (done,) = turn.of("tool.execution_complete")
    assert done.success is not ask_first
    if ask_first:
        assert "doesn't know Claude's FutureTool tool yet" in done.error.message


async def test_a_question_for_the_owner_is_their_card_and_its_answer_reaches_claude(
    agent, tmp_path
):
    asked = []

    def ask(request, context):
        asked.append(request)
        return {"answer": "blue"}

    session = await agent.create_session(
        session_id="9" * 32, name="t", **options(tmp_path, on_user_input_request=ask)
    )
    turn = Turn(session)
    await session.send("Ask the owner which color, then reply with just: ok")
    await turn.ended()
    assert asked == [{"question": "Color: Which color?", "choices": ["red", "blue"],
                      "allowFreeform": True}]  # fmt: skip
    (done,) = turn.of("tool.execution_complete")
    assert done.result.content == 'Your questions have been answered: "Which color?"="blue".'


@pytest.mark.parametrize(
    ("server", "tool", "seen"),
    [
        ("github", "write_marker", ("github-mcp-server", False)),
        ("notes", "read_marker", ("notes", True)),
    ],
)
async def test_an_mcp_call_asks_as_copilots_with_githubs_told_apart(
    agent, tmp_path, server, tool, seen
):
    asked = []

    def check(request, context):
        asked.append((request.kind, request.server_name, request.read_only))
        return PermissionDecisionApproveOnce()

    servers = {server: {"command": "true", "tools": ["*"]}}
    session = await agent.create_session(
        session_id="a1" * 16,
        name="t",
        **options(tmp_path, on_permission_request=check, mcp_servers=servers),
    )
    turn = Turn(session)
    await session.send(f"Call the MCP tool {tool} of {server}, then reply with just: ok")
    await turn.ended()
    assert asked == [("mcp", *seen)]


async def test_a_worker_whose_hooks_are_not_in_effect_does_not_run(agent, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_INIT", json.dumps({"hooks_applied": None}))
    with pytest.raises(RuntimeError, match="its hooks not applied"):
        await agent.create_session(session_id="a2" * 16, name="t", **options(tmp_path))
    assert not agent.workers


async def test_a_turn_run_with_other_settings_stops_its_worker(agent, tmp_path, monkeypatch):
    ended = []
    agent.ended = lambda worker, epoch, why: ended.append(why)
    monkeypatch.setenv("FAKE_CLAUDE_SYSTEM", json.dumps({"permissionMode": "bypassPermissions"}))
    session = await agent.create_session(session_id="a3" * 16, name="t", **options(tmp_path))
    await session.send("Reply with just: ok")
    for _ in range(200):
        if ended:
            break
        await asyncio.sleep(0.05)
    assert ended == [
        "Stopped: Claude did not run this worker with the settings Chief of Staff's checks need "
        "(permission mode bypassPermissions)."
    ]


async def test_a_turn_no_gate_admitted_stops_its_worker_and_its_version(
    agent, tmp_path, monkeypatch
):
    ended, refused = [], []
    agent.ended = lambda worker, epoch, why: ended.append(why)
    agent.on_refused = refused.append
    monkeypatch.setenv("FAKE_CLAUDE_ON_ITS_OWN", "1")
    session = await agent.create_session(session_id="a4" * 16, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Reply with just: ok")
    await turn.ended()
    for _ in range(200):
        if ended:
            break
        await asyncio.sleep(0.05)
    assert ended and ended[0].startswith(
        "Stopped: Claude began a turn Chief of Staff did not admit"
    )
    assert refused and "no Claude worker runs on this version" in agent.acting_refused
    with pytest.raises(RuntimeError, match="did not admit"):
        await agent.create_session(session_id="a5" * 16, name="t", **options(tmp_path))


@pytest.mark.parametrize(
    ("env", "problem"),
    [
        ({"FAKE_CLAUDE_VERSION": "2.1.300"}, "Older than the versions tested (2.1.322)"),
        ({"FAKE_CLAUDE_VERSION": "nonsense"}, "could not tell which version"),
        ({"FAKE_CLAUDE_SIGNED_OUT": "1"}, "Not signed in: run `claude` in a terminal"),
    ],
)
async def test_a_claude_that_cannot_run_says_why(tmp_path, monkeypatch, env, problem):
    command = fake_claude(tmp_path / "fake", monkeypatch)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=problem.replace("(", r"\(").replace(")", r"\)")):
        await claude_backend(command, folder=tmp_path / "agent").start()
    with pytest.raises(RuntimeError, match=problem.replace("(", r"\(").replace(")", r"\)")):
        await backend_claude.look(command)


async def test_without_its_extra_claude_says_what_to_install(tmp_path, monkeypatch):
    command = fake_claude(tmp_path / "fake", monkeypatch)
    monkeypatch.setattr(backend_claude, "sdk_version", lambda: "")
    with pytest.raises(RuntimeError, match=r"Needs Chief of Staff's Claude extra"):
        await claude_backend(command, folder=tmp_path / "agent").start()
    with pytest.raises(RuntimeError, match=r"Needs Chief of Staff's Claude extra"):
        await backend_claude.look(command)


async def test_the_look_keeps_how_it_is_signed_in_and_never_who(tmp_path, monkeypatch):
    command = fake_claude(tmp_path / "fake", monkeypatch)
    monkeypatch.setattr(backend_claude, "LISTED", {})  # No worker listed its models yet.
    runs = tmp_path / "runs"
    monkeypatch.setenv("FAKE_CLAUDE_RUNS", str(runs))
    found = await backend_claude.look(command)
    assert (found["version"], found["account"]) == ("2.1.322", "claudePlan")
    assert [model.id for model in found["models"]] == ["default", "opus", "sonnet", "haiku"]
    assert "example" not in json.dumps(found, default=str).lower(), "no email, no organization"
    assert [json.loads(line) for line in runs.read_text().splitlines()] == [
        ["--version"],
        ["auth", "status", "--json"],
    ], "no session started"
    agent = claude_backend(command, folder=tmp_path / "agent")
    await agent.start()
    assert agent.auth == {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                          "apiKeySource": None, "exit": 0}  # fmt: skip


@pytest.mark.parametrize("configured", [True, False], ids=["CLAUDE_CONFIG_DIR", "home"])
async def test_a_claude_never_set_up_is_said_so_and_none_of_its_files_is_made(
    tmp_path, monkeypatch, configured
):
    # `claude auth status` makes the owner's .claude.json and a backups folder where Claude was
    # never set up (2.1.322): it does not run then, and nothing of Claude's is made.
    command = fake_claude(tmp_path / "fake", monkeypatch)
    home, config = tmp_path / "home", tmp_path / "fake" / "config"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (config / ".claude.json").unlink()
    if not configured:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR")
        config.rmdir()
    runs = tmp_path / "runs"
    monkeypatch.setenv("FAKE_CLAUDE_RUNS", str(runs))
    with pytest.raises(RuntimeError, match=r"^Not set up: run `claude` in a terminal and sign in"):
        await backend_claude.look(command)
    with pytest.raises(RuntimeError, match=r"^Not set up: run `claude`"):
        await claude_backend(command, folder=tmp_path / "agent").start()
    ran = [json.loads(line) for line in runs.read_text().splitlines()]
    assert ran == [["--version"], ["--version"]], "only its version, which makes nothing"
    assert sorted(path.name for path in home.iterdir()) == []
    assert not config.exists() or sorted(path.name for path in config.iterdir()) == []
    # Set up (where it keeps its state): looked at as before.
    (config if configured else home).mkdir(exist_ok=True)
    (config if configured else home).joinpath(".claude.json").write_text("{}\n")
    assert (await backend_claude.look(command))["account"] == "claudePlan"


async def test_looking_at_claude_runs_it_in_chief_of_staffs_folder_not_the_one_it_started_in(
    tmp_path, monkeypatch
):
    # Started inside a repository, whose .claude/settings.json Claude would read there.
    repository = tmp_path / "repository"
    (repository / ".claude").mkdir(parents=True)
    (repository / ".claude" / "settings.json").write_text('{"env": {"PLANTED": "1"}}')
    monkeypatch.chdir(repository)
    command = fake_claude(tmp_path / "fake", monkeypatch)
    cwds = tmp_path / "cwds"
    monkeypatch.setenv("FAKE_CLAUDE_CWDS", str(cwds))
    folder = tmp_path / "data" / "agents" / "claude"
    assert (await backend_claude.look(command, folder))["version"] == "2.1.322"
    agent = claude_backend(command, folder=folder)
    await agent.start()
    await agent.stop()
    ran = [json.loads(line) for line in cwds.read_text().splitlines()]
    assert len(ran) == 4 and set(ran) == {str(folder.resolve())}
    assert oct(folder.stat().st_mode & 0o777) == "0o700"


async def test_a_session_claude_already_has_is_resumed_not_made_again(agent, tmp_path):
    session = await agent.create_session(session_id="a6" * 16, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Remember the word 'kiwi'. Reply with just: ok")
    await turn.ended()
    await session.disconnect()
    for _ in range(100):
        if not agent.workers:
            break
        await asyncio.sleep(0.05)
    again = await agent.create_session(session_id="a6" * 16, name="t", **options(tmp_path))
    turn = Turn(again)
    await again.send("What word did I ask you to remember? Reply with just the word.")
    await turn.ended()
    assert turn.said() == ["kiwi"]
    assert await agent.get_session_metadata("a6" * 16) is not None
    assert await agent.get_session_metadata("a7" * 16) is None
    assert [e.data.content for e in await again.get_events()] == [
        "Remember the word 'kiwi'. Reply with just: ok",
        "ok",
        "What word did I ask you to remember? Reply with just the word.",
        "kiwi",
    ]


async def test_its_cli_runs_under_the_supervisor_with_none_of_chief_of_staffs_variables(
    agent, tmp_path, monkeypatch
):
    monkeypatch.setenv("CHIEFOFSTAFF_SECRET", "x")
    launcher = agent.launcher.read_text()
    assert launcher.startswith("#!/bin/sh\n") and "\nexec " in launcher
    assert ' -I -m chiefofstaff.agentrun --cwd "$cwd" -- ' in launcher
    assert agent.command in launcher and "_bundled" not in launcher
    assert oct(agent.launcher.stat().st_mode & 0o777) == "0o700"
    session = await agent.create_session(session_id="a8" * 16, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Run the shell command `env`, then reply with just: ok")
    await turn.ended()
    (done,) = turn.of("tool.execution_complete")
    assert "CHIEFOFSTAFF_" not in done.result.content
    assert "CLAUDE_CODE_ENTRYPOINT=sdk-py" in done.result.content
    processes = agent.processes()
    assert any("chiefofstaff.agentrun" in line for line in processes.values())
    assert not any("_bundled" in line for line in processes.values())


# Chief of Staff's variables the guide documents (docs/guide.md), each set as an owner might.
DOCUMENTED = {
    "CHIEFOFSTAFF_DISK_CRITICAL_GB": "5",
    "CHIEFOFSTAFF_DISK_WARNING_GB": "20",
    "CHIEFOFSTAFF_EVENT_DEBOUNCE": "0.5",
    "CHIEFOFSTAFF_MIN_FREE_MEMORY": "5",
    "CHIEFOFSTAFF_QUESTION_TTL": "600",
    "CHIEFOFSTAFF_RELEASE_IDLE_MINUTES": "10",
    "CHIEFOFSTAFF_REQUEST_TIMEOUT": "60",
    "CHIEFOFSTAFF_ROUTER_CONTEXT": "Owner works in Lisbon.",
    "CHIEFOFSTAFF_SLACK_MCP_URL": "https://slack.example.com/mcp",
    "CHIEFOFSTAFF_SLACK_USER_TOKEN": "xoxp-secret",
    "CHIEFOFSTAFF_WATCHDOG_INTERVAL": "120",
}


async def test_its_workers_and_check_run_whichever_of_chief_of_staffs_variables_are_set(
    agent, tmp_path, monkeypatch
):
    for name, value in DOCUMENTED.items():
        monkeypatch.setenv(name, value)
    starts = tmp_path / "starts.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_STARTS", str(starts))
    session = await agent.create_session(session_id="c3" * 16, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Reply with just: ok")
    await turn.ended()
    assert turn.said() == ["ok"]
    (started,) = [json.loads(line) for line in starts.read_text().splitlines()]
    assert started["own"] == [], "none of them reaches the CLI"
    found = await agent.check(tmp_path / "check")
    assert found["ran"] is True and found["acting"] == "" and found["ask_first"] == ""


async def test_a_repositorys_python_files_never_run_as_chief_of_staff(agent, tmp_path, monkeypatch):
    # Its supervisor starts in Chief of Staff's own folder, isolated; only Claude enters the
    # worker's folder, where a repository's typing.py or chiefofstaff/ would have run as it.
    marks = tmp_path / "marks"
    repository = shadowing_repository(tmp_path / "repository", marks)
    starts = tmp_path / "starts.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_STARTS", str(starts))
    monkeypatch.setenv("CHIEFOFSTAFF_SECRET", "x")
    session = await agent.create_session(
        session_id="a7" * 16, name="t", **options(repository, folders=[str(repository)])
    )
    turn = Turn(session)
    await session.send("Run the shell command `pwd`, then reply with just: ok")
    await turn.ended()
    assert turn.said() == ["ok"]
    assert sorted(path.name for path in marks.iterdir()) == [], "none of them ran"
    (started,) = [json.loads(line) for line in starts.read_text().splitlines()]
    assert started["cwd"] == started["pwd"] == str(repository.resolve())
    assert started["own"] == [], "none of Chief of Staff's variables"
    assert cwd_of(session.backend.pid) == str(agent.launcher.parent.resolve())
    assert len(SHADOWED) >= 8


async def test_its_mcp_servers_and_prompt_are_in_files_only_the_owner_reads(
    agent, tmp_path, monkeypatch
):
    starts = tmp_path / "starts.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_STARTS", str(starts))
    servers = {
        "remote": {
            "type": "http",
            "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": "Bearer s3cret-header"},
        },
        "local": {"command": "a-server", "args": ["--x"], "env": {"API_TOKEN": "s3cret-env"}},
    }
    session = await agent.create_session(
        session_id="a6" * 16,
        name="t",
        **options(tmp_path, mcp_servers=servers, system_message={"content": "Rules s3cret-rule"}),
    )
    turn = Turn(session)
    await session.send("Reply with just: ok")
    await turn.ended()
    worker = session.backend
    files = list(worker.private)
    assert sorted(path.suffix for path in files) == [".json", ".md"]
    assert {oct(path.stat().st_mode & 0o777) for path in files} == {"0o600"}
    assert oct(files[0].parent.stat().st_mode & 0o777) == "0o700"
    (started,) = [json.loads(line) for line in starts.read_text().splitlines()]
    assert started["servers"] == ["chief", "local", "remote"]
    assert started["appended"] == "Rules s3cret-rule"
    status = {entry["name"]: entry for entry in (await worker.query.get_mcp_status())["mcpServers"]}
    assert status["local"]["config"]["env"] == {"API_TOKEN": "s3cret-env"}, "as configured"
    processes = agent.processes()
    assert len(processes) >= 2 and not any("s3cret" in line for line in processes.values())
    # What Chief of Staff records of them (Runtime: agentpids) names no command, and still finds
    # them.
    records = copilot_records(processes, tmp_path, hashed=True)
    assert "s3cret" not in json.dumps(records) and "chiefofstaff.agentrun" not in json.dumps(
        records
    )
    assert all(record["command"].startswith("sha256:") for record in records.values())
    assert set(copilot_orphans(records, tmp_path)) <= set(processes)
    await agent.stop()
    assert not any(path.exists() for path in files), "gone with its process"


async def test_what_a_killed_chief_of_staffs_workers_left_in_its_files_goes_at_start(
    tmp_path, monkeypatch
):
    command = fake_claude(tmp_path / "fake", monkeypatch)
    private = tmp_path / "agent" / "private"
    private.mkdir(parents=True)
    left = private / "999999999-0123-mcp.json"
    left.write_text("{}")
    mine = private / f"{os.getpid()}-4567-prompt.md"
    mine.write_text("A worker.")
    agent = claude_backend(command, folder=tmp_path / "agent")
    await agent.start()
    try:
        assert not left.exists() and mine.exists(), "another process's, not this one's"
    finally:
        await agent.stop()


async def test_a_sandboxed_workers_commands_stay_in_its_folders_and_reach_only_its_hosts(
    agent, tmp_path
):
    from chiefofstaff.backend import SandboxPolicy

    policy = SandboxPolicy(readwrite=(str(tmp_path),), allowed_hosts=("github.com",))
    session = await agent.create_session(
        session_id="d1" * 16, name="t", **options(tmp_path, sandbox=policy, slack_tools=["read"])
    )
    turn = Turn(session)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    for command in (f"touch {outside}", "curl -sS http://example.com/"):
        await session.send(f"Run the shell command `{command}`, then reply with just: ok")
        await turn.ended()
    results = [done.result.content for done in turn.of("tool.execution_complete")]
    assert "Operation not permitted" in results[0] and not outside.exists()
    assert "example.com blocked" in results[1]
    worker = session.backend
    assert worker.network_refused("api.github.com") == "" and worker.network_refused("x.io")
    assert await worker.decide("Bash", {"command": "ls", "dangerouslyDisableSandbox": True}, "c")
    assert await worker.decide("mcp__slack__send", {}, "c") == (
        "Only Slack's read tools are a worker's to use."
    )
    settings = backend_claude.sandbox_settings(policy)
    assert settings["allowUnsandboxedCommands"] is False and settings["failIfUnavailable"]
    assert settings["filesystem"]["allowWrite"] == [str(tmp_path)]
    assert settings["network"] == {"allowLocalBinding": False, "allowedDomains": ["github.com"]}


async def test_a_plan_limit_reached_keeps_workers_from_running_until_it_resets(
    agent, tmp_path, monkeypatch
):
    limits = []
    agent.on_limit = limits.append
    resets = int(__import__("time").time()) + 3600
    monkeypatch.setenv(
        "FAKE_CLAUDE_LIMIT",
        json.dumps({"status": "rejected", "rateLimitType": "five_hour", "resetsAt": resets}),
    )
    session = await agent.create_session(session_id="a9" * 16, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Reply with just: ok")
    await turn.ended()
    assert limits == [{"status": "rejected", "window": "five_hour", "resets_at": resets}]
    assert agent.acting_refused.startswith("Your Claude plan's 5-hour limit is reached; it resets")
    agent.limit["resets_at"] = 1
    assert agent.acting_refused == "", "past its reset: it runs again"


async def test_a_new_effort_starts_its_next_turn_in_a_new_process(agent, tmp_path):
    told = []
    session = await agent.create_session(
        session_id="b1" * 16,
        name="t",
        **options(tmp_path, on_accounting=lambda kind, data: told.append((kind, data))),
    )
    turn = Turn(session)
    await session.send("Reply with just: ok")
    await turn.ended()
    first = session.backend.epoch
    before = [data for kind, data in told if kind == "agent.usage"][-1].total
    files = list(session.backend.private)
    assert files and all(path.exists() for path in files)
    told.clear()
    await session.set_model("sonnet", reasoning_effort="high")
    assert session.backend.model == "sonnet" and session.backend.epoch == first
    await session.send("Reply with just: again")
    await turn.ended()
    assert session.backend.epoch != first and session.backend.process_effort == "high"
    assert turn.said() == ["ok", "again"]
    usage = [data for kind, data in told if kind == "agent.usage"]
    assert usage[0].epoch == session.backend.epoch and usage[0].restored == before, (
        "the new process counts from what it restored"
    )
    assert not any(path.exists() for path in files), "the first process's files went with it"
    assert session.backend.private != files and all(p.exists() for p in session.backend.private)


async def test_a_process_that_stops_by_itself_affects_its_worker_alone(agent, tmp_path):
    ended = []
    agent.ended = lambda worker, epoch, why: ended.append((worker, why))
    session = await agent.create_session(session_id="b2" * 16, name="t", **options(tmp_path))
    os.kill(session.backend.pid, 9)
    for _ in range(200):
        if ended:
            break
        await asyncio.sleep(0.05)
    assert ended == [("w" * 32, "crash")]
    assert not session.backend.alive()


# Chief of Staff's check of each version (claude_check.py), on the stand-in CLI: it takes its
# turns from the check's stand-in Messages API, as Claude Code would.


@pytest.fixture
def check_sees(monkeypatch):
    """The stand-in's own settings reach it through the check's allowlisted environment."""
    from chiefofstaff import claude_check

    monkeypatch.setattr(
        claude_check, "KEPT", (*claude_check.KEPT, "FAKE_CLAUDE_UNASKED", "FAKE_CLAUDE_HOME")
    )


async def test_the_check_of_a_claude_that_keeps_the_contract_finds_nothing(
    agent, tmp_path, check_sees
):
    found = await agent.check(tmp_path / "check")
    assert found == {
        "ran": True,
        "acting": "",
        "ask_first": "",
        "steering": True,
        "usage": True,
        "background_code": 0,
        "sandbox": "",
        "tools_upfront": True,
    }
    assert not (tmp_path / "check").exists(), "its folder goes"


@pytest.mark.parametrize(
    ("unasked", "acting", "ask_first"),
    [
        ("Write", "its edits do not all ask Chief of Staff first", "a new file, a second"),
        ("Edit", "its edits do not all ask Chief of Staff first", "for an edit"),
        ("Bash", "", "did not ask first for a command"),
        ("mcp__chiefofstaff_check__write_marker", "", "an MCP tool's call"),
    ],
)
async def test_the_check_finds_actions_that_do_not_ask(
    agent, tmp_path, monkeypatch, check_sees, unasked, acting, ask_first
):
    monkeypatch.setenv("FAKE_CLAUDE_UNASKED", unasked)
    found = await agent.check(tmp_path / "check")
    assert found["ran"] is True
    assert (acting in found["acting"]) if acting else found["acting"] == ""
    assert ask_first in found["ask_first"]


async def test_a_note_steered_into_a_stopped_turn_starts_no_turn_after_it(agent, tmp_path):
    marker, raw = tmp_path / "pid", Raw()
    session = await agent.create_session(
        session_id="b3" * 16, name="t", **options(tmp_path, on_message=raw)
    )
    turn = Turn(session)
    await session.send(f"Run the shell command `echo $$ > {marker}; exec sleep 30`")
    for _ in range(200):
        if marker.exists() and marker.read_text().strip():
            break
        await asyncio.sleep(0.05)
    await session.send("Change of plan. Reply with just: steered", mode="immediate")
    await session.abort()
    await turn.ended()
    # Claude takes the note up as a turn of its own after the stop (2.1.322): its gate blocks it.
    await until(lambda: raw.blocked() >= 1)
    await asyncio.sleep(0.05)
    assert [m.content for m in turn.of("user.message")] == [
        f"Run the shell command `echo $$ > {marker}; exec sleep 30`"
    ], "the note never shows: Chief of Staff sends it again with the next message"
    assert turn.said() == [] and turn.kinds().count("abort") == 1
    assert not await session.queue_holds()


async def test_a_message_stopped_before_claude_took_it_up_starts_nothing(
    agent, tmp_path, monkeypatch
):
    told = []
    monkeypatch.setenv("FAKE_CLAUDE_GATE_DELAY", "0.5")
    session = await agent.create_session(
        session_id="b4" * 16,
        name="t",
        **options(tmp_path, on_accounting=lambda kind, data: told.append(kind)),
    )
    turn = Turn(session)
    await session.send("Reply with just: never")
    await session.abort()
    await turn.ended()
    assert turn.kinds() == ["abort", "session.idle"] and turn.said() == []
    assert told == ["agent.settled"], "admitted, it spent nothing"
    assert session.idle()


async def test_a_note_left_from_a_turn_that_ended_is_a_turn_once_admitted(
    agent, tmp_path, monkeypatch
):
    admitted = []

    async def admit(session, woken=False):
        admitted.append(woken)
        return ""

    agent.admit = admit
    monkeypatch.setenv("FAKE_CLAUDE_NO_STEER", "1")
    session = await agent.create_session(session_id="b5" * 16, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Run the shell command `sleep 1`, then reply with just: first")
    for _ in range(100):
        if "tool.execution_start" in turn.kinds():
            break
        await asyncio.sleep(0.02)
    await session.send("Then reply with just: second", mode="immediate")
    await turn.ended()
    await turn.ended()
    assert turn.said() == ["first", "second"]
    assert [m.delivery.value for m in turn.of("user.message")] == ["", ""], "its own turn"
    assert admitted == [False, False], "admitted as a turn of its own"


async def test_a_failed_turn_says_claudes_words_once(agent, tmp_path):
    session = await agent.create_session(session_id="b6" * 16, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Fail this turn, please.")
    await turn.ended()
    (failed,) = turn.of("session.error")
    assert failed.message == "API Error: 529 Overloaded"
    assert turn.said() == [], "Claude's error is the turn's error, not its answer"


async def test_an_image_goes_with_the_message(agent, tmp_path):
    image = tmp_path / "red.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    session = await agent.create_session(session_id="b7" * 16, name="t", **options(tmp_path))
    turn = Turn(session)
    attachments = [
        {"type": "file", "path": str(image), "displayName": "red.png"},
        {"type": "file", "path": str(tmp_path / "missing.png")},
        {"type": "file", "path": str(tmp_path / "notes.txt")},
    ]
    await session.send("Reply with just: seen", attachments=attachments)
    await turn.ended()
    (shown,) = turn.of("user.message")
    assert shown.content == "Reply with just: seen" and turn.said() == ["seen"]
    # Every file reaches it: the image as an image, the others (one not readable as an image)
    # as a marked line each, which its gate reads too and its echo leaves out.
    sent, text = session.backend.content("x", attachments)
    assert [block["type"] for block in sent] == ["text", "text", "image"]
    assert sent[2]["source"]["media_type"] == "image/png"
    lines = sent[1]["text"].splitlines()
    assert lines[0] == backend_claude.ATTACHED
    assert lines[1:3] == [f"- {tmp_path / 'missing.png'}", f"- {tmp_path / 'notes.txt'}"]
    assert (
        lines[3].startswith("(Some could not be read as images") and text == f"x\n{sent[1]['text']}"
    )
    assert [e.data.content for e in await session.get_events()][:1] == ["Reply with just: seen"]
    assert session.backend.content("plain", []) == ("plain", "plain")


async def test_a_worker_whose_settings_changed_ends_once_at_rest(agent, tmp_path):
    ended = []
    agent.ended = lambda worker, epoch, why: ended.append(why)
    session = await agent.create_session(session_id="b8" * 16, name="t", **options(tmp_path))
    turn = Turn(session)
    await session.send("Reply with just: ok")
    await turn.ended()
    await agent.retire("w" * 32, seconds=5)
    assert ended == [""] and not session.backend.alive()
    await agent.retire("nobody")


async def test_what_claude_says_of_its_conversation_and_dropped_messages(agent, tmp_path):
    session = await agent.create_session(session_id="b9" * 16, name="t", **options(tmp_path))
    worker = session.backend
    before = worker.epoch
    await worker.handle_message({"type": "conversation_reset", "new_conversation_id": "x"})
    assert worker.epoch != before and worker.tally.total() == backend_claude.counts({})
    sent = backend_claude.Sent("hi", "hi", kind="turn")
    worker.written[sent.uuid] = sent
    worker.at_gate.append(sent)
    await worker.handle_message(
        {"type": "command_lifecycle", "command_uuid": sent.uuid, "state": "cancelled"}
    )
    assert sent.uuid not in worker.written and sent not in worker.at_gate
    limits = []
    agent.on_limit = limits.append
    info = {"status": "allowed_warning", "rateLimitType": "seven_day", "resetsAt": 5}
    await worker.handle_message({"type": "rate_limit_event", "rate_limit_info": info})
    await worker.handle_message({"type": "rate_limit_event", "rate_limit_info": info})
    assert limits == [{"status": "allowed_warning", "window": "seven_day", "resets_at": 5}]
    assert agent.acting_refused == "", "a warning keeps nothing from running"
