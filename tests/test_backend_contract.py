"""What the runtime relies on from an agent backend, checked against every backend.

The scripted backend always runs, as Copilot and as other coding agents' adapters show those
agents (scripted.CODEX_LIKE, CLAUDE_LIKE). The real GitHub Copilot backend runs when
CHIEFOFSTAFF_TEST_COPILOT=1 (it uses a few small model calls; CHIEFOFSTAFF_TEST_MODEL picks the
model). Run both before upgrading the Copilot SDK or CLI.
"""

import asyncio
import contextlib
import os
import re
import subprocess
from uuid import uuid4

import pytest
from copilot import define_tool
from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from pydantic import BaseModel

from chiefofstaff.backend import (
    SandboxPolicy,
    copilot_backend,
    running_background,
    sandbox_supported,
    stop_background,
    take_queued,
)
from chiefofstaff.delivery import queue_holds
from chiefofstaff.scripted import (
    CLAUDE_LIKE,
    CODEX_LIKE,
    AgentLikeBackend,
    Background,
    CallTool,
    Child,
    Say,
    ScriptedBackend,
    Shell,
)
from chiefofstaff.workspaces import protected_git_paths

REAL = os.environ.get("CHIEFOFSTAFF_TEST_COPILOT") == "1"
MODEL = os.environ.get("CHIEFOFSTAFF_TEST_MODEL", "claude-haiku-5.5")


async def contract_brain(session, prompt):
    """A tiny stand-in for a model: it understands the prompts these tests send."""
    if match := re.search(r"Call the (\w+) tool with text '([^']*)'", prompt):
        yield CallTool(match.group(1), {"text": match.group(2)})
    if match := re.search(r"Run the shell command `([^`]+)`", prompt):
        command = match.group(1)
        seconds = float(m.group(1)) if (m := re.match(r"sleep (\d+)", command)) else 0.1
        output = command.removeprefix("echo ") if command.startswith("echo ") else ""
        yield Shell(command, output=output, seconds=seconds)
    if match := re.search(r"Remember the word '(\w+)'", prompt):
        session.backend.memory = match.group(1)
    if "What word did I ask you to remember" in prompt:
        yield Say(session.backend.memory)
        return
    if match := re.search(r"Leave the command `([^`]+)` running for ([\d.]+) s", prompt):
        yield Background(match.group(1), seconds=float(match.group(2)), exit_code=3)
    if match := re.search(r"Have a child agent call the (\w+) tool with text '([^']*)'", prompt):
        yield Child([CallTool(match.group(1), {"text": match.group(2)})])
    if match := re.search(r"reply with just: (\w+)", prompt, re.IGNORECASE):
        yield Say(match.group(1))


PROFILES = {"codex-like": CODEX_LIKE, "claude-like": CLAUDE_LIKE}


@pytest.fixture(params=["scripted", *PROFILES, pytest.param("copilot", marks=pytest.mark.copilot)])
async def backend(request):
    if request.param == "copilot":
        if not REAL:
            pytest.skip("Set CHIEFOFSTAFF_TEST_COPILOT=1 to check the real Copilot backend.")
        backend = copilot_backend("copilot")
    elif request.param in PROFILES:
        backend = AgentLikeBackend(contract_brain, PROFILES[request.param])
    else:
        backend = ScriptedBackend(contract_brain)
    await backend.start()
    created = []
    backend.created = created
    yield backend
    for session_id in created:
        if request.param == "copilot":
            with contextlib.suppress(Exception):
                await backend.client.delete_session(session_id)
    await backend.stop()


class Echo(BaseModel):
    text: str


def agent_like(backend) -> bool:
    """Another coding agent's backend, as its adapter shows it (not Copilot's)."""
    return isinstance(backend, AgentLikeBackend)


def only_agent_like(backend):
    if not agent_like(backend):
        pytest.skip(
            "How another coding agent's adapter shows it: Copilot's are in test_held_turns."
        )


class Turn:
    """Collects one session's events; `idle` is set when a turn ends."""

    def __init__(self, session):
        self.events = []
        self.idle = asyncio.Event()
        session.on(self.on_event)

    def on_event(self, event):
        self.events.append(event)
        if event.type.value == "session.idle":
            self.idle.set()

    def kinds(self):
        return [event.type.value for event in self.events]

    def said(self):
        return " ".join(
            (getattr(e.data, "content", "") or "")
            for e in self.events
            if e.type.value == "assistant.message"
        )

    async def wait(self, seconds=120):
        await asyncio.wait_for(self.idle.wait(), seconds)
        self.idle.clear()


async def open_session(backend, tmp_path, **options):
    session_id = str(uuid4())
    backend.created.append(session_id)
    options.setdefault(
        "on_permission_request", lambda request, inv: PermissionDecisionApproveOnce()
    )
    session = await backend.create_session(
        session_id=session_id,
        name="contract test",
        model=MODEL,
        working_directory=str(tmp_path),
        **options,
    )
    return session, Turn(session)


async def test_send_returns_at_once_and_the_turn_reports_its_progress(backend, tmp_path):
    calls = []

    def echo(params: Echo, invocation):
        calls.append((params.text, invocation.session_id))
        return f"echo: {params.text}"

    tool = define_tool("echo", description="Echo text back", params_type=Echo, handler=echo)
    session, turn = await open_session(backend, tmp_path, tools=[tool])
    await session.send("Call the echo tool with text 'hi', then reply with just: done")
    assert not turn.idle.is_set(), "send returns before the turn ends"
    await turn.wait()
    kinds = turn.kinds()
    assert calls == [("hi", session.session_id)], "the tool ran with its parameters"
    order = ["user.message", "assistant.turn_start", "tool.execution_start"]
    positions = [kinds.index(kind) for kind in order]
    assert positions == sorted(positions), kinds
    assert kinds.index("tool.execution_complete") > kinds.index("tool.execution_start")
    assert kinds[-1] == "session.idle"
    started = next(e for e in turn.events if e.type.value == "tool.execution_start")
    assert started.data.tool_name == "echo" and started.data.arguments == {"text": "hi"}
    assert "done" in turn.said().lower()
    # Usage in Copilot's credits comes from Copilot only: another agent's is in its own unit.
    assert ("assistant.usage" in kinds) == (not agent_like(backend))


async def test_shell_commands_ask_for_permission_first(backend, tmp_path):
    asked = []

    def deny(request, invocation):
        asked.append((request.kind, getattr(request, "full_command_text", "")))
        return PermissionDecisionReject(feedback="Not in this test.")

    session, turn = await open_session(backend, tmp_path, on_permission_request=deny)
    await session.send(
        "Run the shell command `mkdir contract-check` and then reply with just: finished"
    )
    await turn.wait()
    assert any(kind == "shell" and "contract-check" in command for kind, command in asked), asked
    assert not (tmp_path / "contract-check").exists(), "a denied command does not run"


async def test_a_session_resumes_with_its_conversation(backend, tmp_path):
    session, turn = await open_session(backend, tmp_path)
    await session.send("Remember the word 'pineapple'. Reply with just: ok")
    await turn.wait()
    await session.disconnect()
    resumed = await backend.resume_session(
        session.session_id,
        on_permission_request=lambda request, inv: PermissionDecisionApproveOnce(),
        working_directory=str(tmp_path),
    )
    turn = Turn(resumed)
    await resumed.send("What word did I ask you to remember? Reply with just the word.")
    await turn.wait()
    assert "pineapple" in turn.said().lower()
    await resumed.disconnect()


async def test_abort_ends_the_turn_and_its_command(backend, tmp_path):
    marker = f"sleep {40 + os.getpid() % 17}"
    session, turn = await open_session(backend, tmp_path)
    await session.send(f"Run the shell command `{marker}` and then reply with just: slept")
    for _ in range(600):
        if "tool.execution_start" in turn.kinds():
            break
        await asyncio.sleep(0.1)
    assert "tool.execution_start" in turn.kinds()
    await asyncio.sleep(1)
    await session.abort()
    await turn.wait(30)
    assert "slept" not in turn.said().lower()
    await asyncio.sleep(1)
    found = await asyncio.to_thread(
        subprocess.run, ["pgrep", "-f", marker], capture_output=True, text=True
    )
    running = found.stdout
    assert not running.strip(), f"the aborted command still runs: {running}"


async def test_steering_reaches_the_turn_in_progress(backend, tmp_path):
    session, turn = await open_session(backend, tmp_path)
    await session.send("Run the shell command `sleep 6` and then reply with just: first")
    for _ in range(600):
        if "tool.execution_start" in turn.kinds():
            break
        await asyncio.sleep(0.1)
    await session.send(
        "Change of plan: do not reply 'first'. Reply with just: steered", mode="immediate"
    )
    await turn.wait()
    assert "steered" in turn.said().lower()
    if backend.capabilities.steering:
        assert turn.kinds().count("session.idle") == 1, "one turn, not a new one"
    else:
        # It cannot steer: the message waited for the turn's end, as one queued does.
        said = [
            (e.type.value, e.data.content.split(":")[0])
            for e in turn.events
            if e.type.value in {"assistant.message", "user.message"} and e.data.content
        ]
        assert said == [
            ("user.message", "Run the shell command `sleep 6` and then reply with just"),
            ("assistant.message", "first"),
            ("user.message", "Change of plan"),
            ("assistant.message", "steered"),
        ]


def sandboxed_repository(root):
    """A repository with the owner's own worktree, a submodule's git dir and a worker's worktree."""

    def git(*arguments, cwd=root / "repo"):
        subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)

    (root / "repo").mkdir()
    git("init", "-q", "-b", "main")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    git("worktree", "add", "-q", "-b", "mine", str(root / "mine"))
    git("worktree", "add", "-q", "-b", "work", str(root / "work"))
    modules = root / "repo" / ".git" / "modules" / "lib"
    modules.mkdir(parents=True)
    (modules / "config").write_text("[core]\n")
    return (root / "repo" / ".git").resolve(), (root / "work").resolve()


async def test_the_sandbox_keeps_what_runs_code_outside_it_read_only(backend, tmp_path):
    if not (backend.capabilities.sandbox and sandbox_supported()):
        pytest.skip("Only a backend with an OS sandbox on a supported machine enforces this.")
    git_dir, work = sandboxed_repository(tmp_path)
    policy = SandboxPolicy(
        readwrite=(str(work), str(git_dir)),
        readonly=tuple(protected_git_paths(str(git_dir), [{"path": str(work)}])),
    )
    session, turn = await open_session(backend, work, sandbox_config=backend.sandbox(policy))
    targets = [
        git_dir / "config",
        git_dir / "hooks" / "pre-commit",
        git_dir / "modules" / "lib" / "config",
        git_dir / "worktrees" / "mine" / "HEAD",
    ]
    # A script, so the model runs one harmless-looking command; the commit shows that it ran.
    script = "".join(f"echo marker >> {target} 2>/dev/null\n" for target in targets)
    script += "git -c user.email=t@t -c user.name=t commit -q --allow-empty -m sandboxed\n"
    (work / "check.sh").write_text(script)
    await session.send("Run the shell command `sh check.sh` and then reply with just: finished")
    await turn.wait()
    assert last_commit(work) == "sandboxed", (
        "the script ran, and the worker commits in its worktree"
    )
    for target in targets:
        assert "marker" not in contents(target), target


def contents(path) -> str:
    return path.read_text() if path.exists() else ""


def last_commit(checkout) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=checkout, capture_output=True, text=True
    ).stdout.strip()


async def test_a_message_enqueued_mid_turn_shows_as_a_user_message(backend, tmp_path):
    """Chief of Staff learns which task a worker's turn is on from the user.message events: a
    message sent while it works ("enqueue") shows as one, with its text, when it is taken up
    (after the work in progress, not in the middle of it)."""
    session, turn = await open_session(backend, tmp_path)
    await session.send("Run the shell command `sleep 6` and then reply with just: first")
    for _ in range(600):
        if "tool.execution_start" in turn.kinds():
            break
        await asyncio.sleep(0.1)
    await session.send("Marker q7z: reply with just: second", mode="enqueue")

    def users():
        return [
            getattr(e.data, "content", "") or ""
            for e in turn.events
            if e.type.value == "user.message"
        ]

    for _ in range(900):
        if any("q7z" in content for content in users()) and turn.kinds()[-1] == "session.idle":
            break
        await asyncio.sleep(0.2)
    assert any("q7z" in content for content in users()), users()


# Other coding agents, as their adapters show them: what the runtime's paths rely on.


async def test_a_command_left_running_holds_the_turns_end_until_its_own_end_message(
    backend, tmp_path
):
    only_agent_like(backend)
    session, turn = await open_session(backend, tmp_path)
    await session.send(
        "Leave the command `npm run dev` running for 0.5 s, then reply with just: up"
    )
    for _ in range(100):
        if "assistant.idle" in turn.kinds():
            break
        await asyncio.sleep(0.02)
    assert "session.idle" not in turn.kinds(), "what it left running holds the session"
    assert await running_background(session) == 1
    done = next(e for e in turn.events if e.type.value == "tool.execution_complete")
    assert done.data.result.content == "<command started in background with shellId: bg-1>"
    await turn.wait(5)
    ended = next(e for e in turn.events if e.type.value == "system.notification")
    code = 3 if backend.profile.exit_codes else None
    assert (ended.data.kind.type, ended.data.kind.shell_id, ended.data.kind.exit_code) == (
        "shell_completed",
        "bg-1",
        code,
    )
    assert turn.kinds()[-2:] == ["system.notification", "session.idle"]
    assert await running_background(session) == 0


async def test_stopping_what_it_left_running_ends_each_with_its_end_message(backend, tmp_path):
    only_agent_like(backend)
    session, turn = await open_session(backend, tmp_path)
    await session.send("Leave the command `npm run dev` running for 60 s, then reply with just: up")
    for _ in range(100):
        if "assistant.idle" in turn.kinds():
            break
        await asyncio.sleep(0.02)
    assert await stop_background(session) == 1
    await turn.wait(5)
    ended = next(e for e in turn.events if e.type.value == "system.notification")
    assert ended.data.kind.exit_code is None, "stopped: no result"
    assert await running_background(session) == 0 and await stop_background(session) == 0


async def test_what_it_queued_is_taken_back_and_its_next_turn_waits_to_be_admitted(
    backend, tmp_path
):
    only_agent_like(backend)
    session, turn = await open_session(backend, tmp_path)
    asked = []

    async def refuse(asking):
        asked.append(asking.session_id)
        return False

    backend.admit = refuse
    await session.send("Run the shell command `sleep 1` and then reply with just: first")
    await session.send("Reply with just: second")
    await session.send("Reply with just: third")
    assert await queue_holds(session)
    assert await take_queued(session) == 2, "the two it had not started on"
    assert not await queue_holds(session)
    await session.send("Reply with just: fourth")
    await turn.wait(10)
    assert asked == [session.session_id], "asked once its turn ended"
    users = [e.data.content for e in turn.events if e.type.value == "user.message"]
    assert len(users) == 1 and "fourth" not in turn.said(), "refused: given back, not started"
    assert not await queue_holds(session)

    backend.admit = None
    await session.send("Run the shell command `sleep 1` and then reply with just: first")
    await session.send("Reply with just: second")
    await turn.wait(10)
    assert "second" in turn.said(), "admitted, its next turn starts"


async def test_a_child_agents_calls_are_its_sessions(backend, tmp_path):
    only_agent_like(backend)
    calls = []

    def echo(params: Echo, invocation):
        calls.append((params.text, invocation.session_id))
        return f"echo: {params.text}"

    tool = define_tool("echo", description="Echo text back", params_type=Echo, handler=echo)
    session, turn = await open_session(backend, tmp_path, tools=[tool])
    await session.send("Have a child agent call the echo tool with text 'deep'")
    await turn.wait(5)
    assert calls == [("deep", session.session_id)]
    starts = [e.data for e in turn.events if e.type.value == "tool.execution_start"]
    assert [(d.tool_name, getattr(d, "parent_tool_call_id", None)) for d in starts] == [
        ("task", None),
        ("echo", starts[0].tool_call_id),
    ]


async def test_exit_codes_only_where_the_agent_gives_them(backend, tmp_path):
    only_agent_like(backend)
    session, turn = await open_session(backend, tmp_path)
    await session.send("Run the shell command `echo hi` and then reply with just: ran")
    await turn.wait(5)
    done = next(e for e in turn.events if e.type.value == "tool.execution_complete")
    assert done.data.result.content == "hi" and done.data.success is True
    execution = done.data.shell_execution
    assert (execution.exit_code if execution else None) == (
        0 if backend.profile.exit_codes else None
    )


async def test_the_agent_chooses_its_sessions_ids_or_takes_ours(backend, tmp_path):
    only_agent_like(backend)
    asked = str(uuid4())
    session = await backend.create_session(session_id=asked, name="ids", tools=[])
    assert (session.session_id != asked) == backend.profile.assigns_ids
    await session.send("Reply with just: ok")
    await asyncio.sleep(0.1)
    await session.disconnect()
    resumed = await backend.resume_session(session.session_id)
    assert [e.data.content for e in await resumed.get_events()] == ["Reply with just: ok", "ok"]
