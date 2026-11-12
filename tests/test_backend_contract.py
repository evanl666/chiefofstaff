"""What the runtime relies on from an agent backend, checked against every backend.

The scripted backend always runs, as Copilot and as other coding agents' adapters show those
agents (scripted.CODEX_LIKE, CLAUDE_LIKE), and so does the Codex adapter on a stand-in CLI
(tests/fake_codex.py). The real GitHub Copilot backend runs when CHIEFOFSTAFF_TEST_COPILOT=1 (it
uses a few small model calls; CHIEFOFSTAFF_TEST_MODEL picks the model), and the Codex adapter on
the real Codex CLI when CHIEFOFSTAFF_TEST_CODEX=1: its app-server, approvals, commands, child
agents and resumes, with a CODEX_HOME of the test's own and a stand-in model on this machine
(tests/scripted_model.py): no sign-in, no model call, the owner's ~/.codex untouched. Run them
before upgrading the Copilot SDK or CLI, or Codex.
"""

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from uuid import uuid4

import pytest
import scripted_model
from conftest import fake_codex
from copilot import define_tool
from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from pydantic import BaseModel

from chiefofstaff.backend import (
    SandboxPolicy,
    copilot_backend,
    process_table,
    running_background,
    sandbox_supported,
    stop_background,
    take_queued,
)
from chiefofstaff.backend_codex import CodexAgent, CodexBackend
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
REAL_CODEX = os.environ.get("CHIEFOFSTAFF_TEST_CODEX") == "1"


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


@pytest.fixture(
    params=[
        "scripted",
        *PROFILES,
        "fake-codex",
        pytest.param("copilot", marks=pytest.mark.copilot),
        pytest.param("codex", marks=pytest.mark.codex),
    ]
)
async def backend(request, tmp_path, monkeypatch):
    if request.param == "copilot":
        if not REAL:
            pytest.skip("Set CHIEFOFSTAFF_TEST_COPILOT=1 to check the real Copilot backend.")
        backend = copilot_backend("copilot")
    elif request.param == "codex":
        if not REAL_CODEX:
            pytest.skip("Set CHIEFOFSTAFF_TEST_CODEX=1 to check the Codex adapter on real Codex.")
        home = tmp_path / "codex-home"
        home.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(home))
        model = scripted_model.Model()
        # One app-server, as each worker has its own (CodexAgent); the adapter's contract.
        backend = CodexBackend(shutil.which("codex") or "codex")
        backend.model = model
        backend.arguments += model.arguments(tmp_path)
        backend.needs_sign_in = False
    elif request.param == "fake-codex":
        backend = CodexBackend(fake_codex(tmp_path / "fake", monkeypatch))
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
    if model := getattr(backend, "model", None):
        model.close()


class Echo(BaseModel):
    text: str


def unique_seconds(about: int) -> str:
    """How long a command sleeps, told apart from any other run's (pgrep finds it by that)."""
    return f"{about}.{uuid4().int % 10**6:06d}"


def agent_like(backend) -> bool:
    """Another coding agent's backend, as its adapter shows it (not Copilot's)."""
    return isinstance(backend, AgentLikeBackend | CodexBackend)


def only_agent_like(backend):
    if not isinstance(backend, AgentLikeBackend):
        pytest.skip(
            "How a scripted agent's profile shows it: Copilot's are in test_held_turns, "
            "Codex's below."
        )


def only_codex(backend):
    if not isinstance(backend, CodexBackend):
        pytest.skip("Codex's adapter only.")


def real_codex(backend) -> bool:
    return isinstance(backend, CodexBackend) and "fake" not in backend.command


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
    if not isinstance(backend, CodexBackend):
        backend.created.append(session_id)  # Codex chooses its threads' ids (below).
    options.setdefault(
        "on_permission_request", lambda request, inv: PermissionDecisionApproveOnce()
    )
    if not isinstance(backend, CodexBackend):
        options.setdefault("model", MODEL)  # Codex: its default model.
    session = await backend.create_session(
        session_id=session_id, name="contract test", working_directory=str(tmp_path), **options
    )
    backend.created.append(session.session_id)  # The id the agent chose, when it does.
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
    # A marker no other process on the machine has (live runs and other checkouts' tests run
    # `sleep` too): the fraction of a second makes it this run's alone.
    marker = f"sleep {40 + os.getpid() % 17}.{uuid4().int % 10**6:06d}"
    session, turn = await open_session(backend, tmp_path)
    await session.send(f"Run the shell command `{marker}` and then reply with just: slept")
    for _ in range(600):
        if "tool.execution_start" in turn.kinds():
            break
        await asyncio.sleep(0.1)
    assert "tool.execution_start" in turn.kinds()
    await asyncio.sleep(1)
    await session.abort()
    if isinstance(backend, CodexBackend):
        # Codex keeps a turn's commands running after it is interrupted (background terminals,
        # 0.167.2): what the runtime does on every stop ends them.
        await stop_background(session)
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
        return "Refused in this test."

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


# Codex's adapter: what it relies on from the app-server, on the stand-in CLI always and on the
# owner's own Codex with CHIEFOFSTAFF_TEST_CODEX=1 (design: more-agents.md, section 7).

MARKERS = Path(__file__).parent / "marker_mcp.py"


async def another_codex(backend, tmp_path, monkeypatch, *arguments: str, **env: str):
    """A second app-server like this one's, with more of the owner's configuration (`-c`
    arguments, as their config.toml would give) or, for the stand-in, its FAKE_CODEX_* settings."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    other = CodexBackend(backend.command)
    other.arguments = [*backend.arguments, *arguments]
    other.needs_sign_in = backend.needs_sign_in
    other.created = []
    return other


async def wait_for(check, seconds: float = 120):
    for _ in range(int(seconds * 10)):
        if check():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("never happened")


async def test_codex_threads_keep_the_settings_its_checks_need(backend, tmp_path, monkeypatch):
    """Full access, every action asked about, and Chief of Staff the one who answers: also when
    the owner's configuration hands approvals to a reviewer agent. A thread that does not read
    back as asked is refused, and so is an organization's requirement that forbids it."""
    only_codex(backend)
    reviewer = await another_codex(
        backend, tmp_path, monkeypatch, "-c", 'approvals_reviewer="auto_review"'
    )
    await reviewer.start()
    try:
        session, _ = await open_session(reviewer, tmp_path)
        assert session.session_id, "read back as full access, untrusted, reviewer user"
        await reviewer.archive(session.session_id)
    finally:
        await reviewer.stop()
    if real_codex(backend):
        return  # An administrator's override is the stand-in's to show.
    overridden = await another_codex(
        backend, tmp_path, monkeypatch, FAKE_CODEX_ANSWER='{"approvalsReviewer": "auto_review"}'
    )
    await overridden.start()
    try:
        with pytest.raises(RuntimeError, match="reviewer auto_review"):
            await open_session(overridden, tmp_path)
    finally:
        await overridden.stop()
    forbidding = await another_codex(
        backend,
        tmp_path,
        monkeypatch,
        FAKE_CODEX_ANSWER="{}",
        FAKE_CODEX_REQUIREMENTS='{"allowedSandboxModes": ["read-only"]}',
    )
    with pytest.raises(RuntimeError, match="do not allow the sandbox mode"):
        await forbidding.start()
    assert not forbidding.alive()


async def test_every_edit_asks_with_each_file_and_a_refused_one_is_not_written(backend, tmp_path):
    only_codex(backend)
    asked = []
    # One of them where the thread's own folder is not: a checkout under the temporary folder.
    elsewhere = Path(tempfile.gettempdir()) / f"cos-contract-{uuid4().hex[:8]}" / "b.txt"

    def deny(request, invocation):
        asked.append((request.kind, getattr(request, "file_name", "")))
        if request.kind == "write" and Path(request.file_name).name == "b.txt":
            return PermissionDecisionReject(feedback="Not in this test.")
        return PermissionDecisionApproveOnce()

    session, turn = await open_session(backend, tmp_path, on_permission_request=deny)
    await session.send(
        f"Write the file `a.txt` and the file `{elsewhere}` with your file editing tool "
        "(apply_patch), not the shell, each with the text 'x'. Then reply with just: written"
    )
    await turn.wait()
    writes = {Path(path).name for kind, path in asked if kind == "write"}
    assert writes == {"a.txt", "b.txt"}, asked
    assert not elsewhere.exists(), "refused: not written"
    edits = [e.data for e in turn.events if e.type.value == "tool.execution_start"]
    assert {d.tool_name for d in edits} <= {"create", "edit", "bash"}, edits


async def test_with_ask_first_every_mcp_call_asks_and_githubs_writes_stay_refused(
    backend, tmp_path, monkeypatch
):
    """The owner's config lets a GitHub write tool run without asking: with Ask first it still
    asks, and Chief of Staff's checks refuse it; acting on its own, it runs as configured."""
    only_codex(backend)
    marks = tmp_path / "marks"
    marks.mkdir()
    server = {
        "command": sys.executable,
        "args": [str(MARKERS)],
        "env": {"MARKER_DIR": str(marks)},
        "tools": {"create_pull_request_review": {"approval_mode": "approve"}},
    }
    owner = await another_codex(
        backend,
        tmp_path,
        monkeypatch,
        "-c",
        f'mcp_servers.github.command="{sys.executable}"',
        "-c",
        f'mcp_servers.github.args=["{MARKERS}"]',
        "-c",
        f'mcp_servers.github.env={{MARKER_DIR="{marks}"}}',
        "-c",
        'mcp_servers.github.tools.create_pull_request_review.approval_mode="approve"',
        FAKE_CODEX_CONFIG=json.dumps({"mcp_servers": {"github": server}}),
    )
    await owner.start()
    try:
        asked = []

        def check(request, invocation):
            asked.append(request)
            if request.kind == "mcp" and not request.read_only:
                return PermissionDecisionReject(feedback="Publishing goes through a draft.")
            return PermissionDecisionApproveOnce()

        prompt = (
            "Call the MCP tool create_pull_request_review of github, then call the MCP tool "
            "get_me of github (read-only). Then reply with what each returned."
        )
        session, turn = await open_session(
            owner, tmp_path, on_permission_request=check, ask_first=True
        )
        await session.send(prompt)
        await turn.wait()
        mcp = [(r.server_name, r.tool_name, r.read_only) for r in asked if r.kind == "mcp"]
        assert ("github-mcp-server", "create_pull_request_review", False) in mcp, asked
        assert not (marks / "review-published").exists(), "refused: never published"
        await owner.archive(session.session_id)

        asked.clear()
        session, turn = await open_session(owner, tmp_path, on_permission_request=check)
        await session.send(prompt)
        await turn.wait()
        assert not [r for r in asked if r.kind == "mcp"], "acting on its own: as configured"
        if real_codex(owner):
            assert (marks / "review-published").exists()
        await owner.archive(session.session_id)
    finally:
        await owner.stop()


async def test_a_command_it_leaves_running_holds_its_turns_end_until_it_ends(backend, tmp_path):
    only_codex(backend)
    session, turn = await open_session(backend, tmp_path)
    await session.send(
        "Leave the command `sleep 4; exit 3` running in the background: start it, do not wait "
        "for it to finish and do not use &. As soon as it has started, reply with just: started"
    )
    await wait_for(lambda: "assistant.idle" in turn.kinds())
    assert "session.idle" not in turn.kinds(), "what it left running holds the session"
    assert await running_background(session) == 1
    left = [
        e.data
        for e in turn.events
        if e.type.value == "tool.execution_complete"
        and "started in background" in e.data.result.content
    ]
    assert len(left) == 1
    await turn.wait(60)
    ended = next(e.data.kind for e in turn.events if e.type.value == "system.notification")
    assert (ended.type, ended.shell_id, ended.exit_code) == (
        "shell_completed",
        left[0].tool_call_id,
        3,
    )
    assert turn.kinds()[-2:] == ["system.notification", "session.idle"]


async def test_stopping_what_it_left_running_ends_it(backend, tmp_path):
    only_codex(backend)
    marker = f"sleep {unique_seconds(200)}"
    session, turn = await open_session(backend, tmp_path)
    await session.send(
        f"Leave the command `{marker}` running in the background: start it, do not wait for "
        "it to finish and do not use &. As soon as it has started, reply with just: started"
    )
    await wait_for(lambda: "assistant.idle" in turn.kinds())
    assert await stop_background(session) == 1
    await turn.wait(30)
    ended = next(e.data.kind for e in turn.events if e.type.value == "system.notification")
    assert ended.exit_code is None, "stopped: no result"
    found = await asyncio.to_thread(
        subprocess.run, ["pgrep", "-f", marker], capture_output=True, text=True
    )
    assert not found.stdout.strip()


async def test_a_child_agents_calls_and_usage_are_its_workers(backend, tmp_path):
    only_codex(backend)
    calls = []

    def echo(params: Echo, invocation):
        calls.append((params.text, invocation.session_id))
        return f"echo: {params.text}"

    tool = define_tool("echo", description="Echo text back", params_type=Echo, handler=echo)
    session, turn = await open_session(backend, tmp_path, tools=[tool])
    await session.send(
        "Have a child agent call the echo tool with text 'deep': spawn one sub-agent for it and "
        "wait for it to finish. Then reply with just: done"
    )
    await turn.wait()
    assert ("deep", session.session_id) in calls, "a child's call is its worker's"
    starts = [e.data for e in turn.events if e.type.value == "tool.execution_start"]
    task = next(d for d in starts if d.tool_name == "task")
    echoed = next(d for d in starts if d.tool_name == "echo")
    assert echoed.parent_tool_call_id == task.tool_call_id
    usage = [e.data for e in turn.events if e.type.value == "agent.usage"]
    assert any(u.child for u in usage) and any(not u.child for u in usage)
    assert all(u.total.get("inputTokens", 0) >= u.last.get("inputTokens", 0) for u in usage)


async def test_stop_ends_its_child_agents_too(backend, tmp_path):
    only_codex(backend)
    marker = f"sleep {unique_seconds(300)}"
    session, turn = await open_session(backend, tmp_path)
    await session.send(
        f"Have a child agent call the echo tool with text 'x' and run `{marker}`: spawn one "
        "sub-agent that runs that shell command and waits for it. Wait for the sub-agent, then "
        "reply with just: done"
    )
    await wait_for(lambda: running(marker))
    await session.abort()
    await stop_background(session)  # As the runtime stops a worker: with what it left running.
    await turn.wait(30)
    await asyncio.sleep(1)
    assert not await asyncio.to_thread(running, marker), "the child's command ended with the turn"


async def test_two_sessions_calling_one_tool_are_told_apart(backend, tmp_path):
    only_codex(backend)
    calls = []

    def echo(params: Echo, invocation):
        calls.append((params.text, invocation.session_id))
        return "ok"

    tool = define_tool("echo", description="Echo text back", params_type=Echo, handler=echo)
    first, one = await open_session(backend, tmp_path, tools=[tool])
    second, two = await open_session(backend, tmp_path, tools=[tool])
    await first.send("Call the echo tool with text 'one', then reply with just: done")
    await second.send("Call the echo tool with text 'two', then reply with just: done")
    await one.wait()
    await two.wait()
    assert sorted(calls) == [("one", first.session_id), ("two", second.session_id)]


async def test_models_are_listed_and_a_switch_holds_from_the_next_turn(backend, tmp_path):
    only_codex(backend)
    models = await backend.list_models()
    assert models and all(model.id for model in models)
    assert any(model.supported_reasoning_efforts for model in models)
    session, turn = await open_session(backend, tmp_path)
    other = next(model.id for model in models if not model.is_default)
    await session.set_model(other, reasoning_effort="low")
    await session.send("Reply with just: ok")
    await turn.wait()
    assert (await backend.request("thread/read", {"threadId": session.session_id}))["thread"][
        "model"
    ] == other


async def test_an_image_reaches_the_model(backend, tmp_path):
    only_codex(backend)
    image = tmp_path / "red.png"
    image.write_bytes(red_png())
    session, turn = await open_session(backend, tmp_path)
    await session.send(
        "What colour is this image? Reply with just: red, green or blue.",
        attachments=[{"type": "file", "path": str(image), "displayName": "red.png"}],
    )
    await turn.wait()
    if real_codex(backend):
        assert "red" in turn.said().lower()


async def test_a_thread_resumes_after_its_app_server_is_killed_mid_turn(
    backend, tmp_path, monkeypatch
):
    only_codex(backend)
    session, turn = await open_session(backend, tmp_path)
    await session.send("Remember the word 'pineapple'. Reply with just: ok")
    await turn.wait()
    await session.send("Run the shell command `sleep 30` and then reply with just: slept")
    await wait_for(lambda: "tool.execution_start" in turn.kinds())
    table = await asyncio.to_thread(process_table)
    cli = next(pid for pid, (parent, _) in table.items() if parent == backend.process.pid)
    os.kill(cli, signal.SIGKILL)  # The CLI the supervisor runs.
    await wait_for(lambda: not backend.alive(), 30)
    again = await another_codex(backend, tmp_path, monkeypatch)
    await again.start()
    try:
        resumed = await again.resume_session(
            session.session_id,
            on_permission_request=lambda request, inv: PermissionDecisionApproveOnce(),
            working_directory=str(tmp_path),
        )
        turn = Turn(resumed)
        await resumed.send("What word did I ask you to remember? Reply with just the word.")
        await turn.wait()
        assert "pineapple" in turn.said().lower()
        users = [
            e.data.content for e in await resumed.get_events() if e.type.value == "user.message"
        ]
        assert any("pineapple" in text for text in users), users
    finally:
        if real_codex(again):
            with contextlib.suppress(Exception):
                await again.archive(session.session_id)  # Its first app-server is gone.
        await again.stop()


async def test_its_shell_sees_no_chief_of_staff_variable(backend, tmp_path, monkeypatch):
    only_codex(backend)
    monkeypatch.setenv("CHIEFOFSTAFF_CONTRACT_SECRET", "s3cret")
    clean = await another_codex(backend, tmp_path, monkeypatch)
    await clean.start()
    try:
        session, turn = await open_session(clean, tmp_path)
        await session.send(
            "Run the shell command `env | grep -c CHIEFOFSTAFF_ || true` and then reply with "
            "just: counted"
        )
        await turn.wait()
        done = next(e.data for e in turn.events if e.type.value == "tool.execution_complete")
        assert done.result.content.strip() == "0", done.result.content
        await clean.archive(session.session_id)
    finally:
        await clean.stop()


async def test_a_worktree_loads_its_repositorys_codex_config_only_where_the_owner_trusts_it(
    backend, tmp_path, monkeypatch
):
    """Codex marks a folder it starts a writable thread in as trusted in the owner's config:
    every thread is told its folders' trust instead, as Codex itself trusts them for the owner:
    a worktree by its main checkout's entry, never a folder by an ancestor's."""
    only_codex(backend)
    if not real_codex(backend):
        pytest.skip("Which configuration loads is Codex's own: the real one shows it.")
    config = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    before = config.read_bytes() if config.exists() else None
    for trusted in ("repository", "", "ancestor"):
        place = tmp_path / (trusted or "untrusted")
        repo, worktree = place / "repo", place / "work"
        await asyncio.to_thread(repository_with_codex_config, place, repo, worktree)
        # The owner's trust, as their config.toml says it (a dotted -c key would split the path).
        folder = {"repository": repo.resolve(), "ancestor": place.resolve()}.get(trusted)
        trust = f'projects={{"{folder}"={{trust_level="trusted"}}}}'
        owner = ["-c", trust] if folder else []
        codex = await another_codex(backend, tmp_path, monkeypatch, *owner)
        await codex.start()
        try:
            session = await codex.create_session(
                session_id="t",
                name="trust",
                working_directory=str(worktree),
                folders=[str(worktree)],
                on_permission_request=lambda request, inv: PermissionDecisionApproveOnce(),
            )
            await asyncio.sleep(5)  # Its MCP servers start with the thread.
            assert (place / "started-project").exists() == (trusted == "repository"), trusted
            await codex.archive(session.session_id)
        finally:
            await codex.stop()
    assert (config.read_bytes() if config.exists() else None) == before, "never written"


async def test_the_repositorys_instructions_reach_the_model_once_trusted_or_not(
    backend, tmp_path, monkeypatch
):
    """Where Codex trusts the folder it gives the repository's AGENTS.md itself; where it does
    not, Chief of Staff gives the same text in the thread's instructions: never both."""
    only_codex(backend)
    if not real_codex(backend):
        pytest.skip("What reaches the model is Codex's own: the real one shows it.")
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(repo)], check=True)
    (repo / "AGENTS.md").write_text("Use tabs.\n")
    (repo / "sub" / "AGENTS.override.md").write_text("Here, spaces.\n")
    seen = {}
    for trusted in (True, False):
        owner = (
            ["-c", f'projects={{"{repo.resolve()}"={{trust_level="trusted"}}}}'] if trusted else []
        )
        codex = await another_codex(backend, tmp_path, monkeypatch, *owner)
        await codex.start()
        try:
            session, turn = await open_session(
                codex,
                repo / "sub",
                system_message={"content": "Chief's rules."},
                folders=[str(repo / "sub")],
            )
            sent = len(backend.model.bodies)
            await session.send("Reply with just: ok")
            await turn.wait()
        finally:
            await codex.stop()
        body = backend.model.bodies[sent]
        texts = {  # Each part of each message.
            role: [
                str(part.get("text") or "")
                for item in body["input"]
                if item.get("role") == role
                for part in item.get("content") or []
            ]
            for role in ("developer", "user")
        }
        seen[trusted] = texts
    own = [t for t in seen[True]["user"] if t.startswith("# AGENTS.md instructions")]
    assert len(own) == 1 and "Use tabs." in own[0] and "Here, spaces." in own[0]
    assert not any("<repository_instructions>" in t for t in seen[True]["developer"])
    assert not any(t.startswith("# AGENTS.md instructions") for t in seen[False]["user"])
    given = [t for t in seen[False]["developer"] if "<repository_instructions>" in t]
    assert len(given) == 1 and own[0] in given[0], "the same text Codex itself gives"


def running(pattern: str) -> bool:
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


@pytest.mark.codex
async def test_a_codex_worker_reaches_no_other_workers_thread(tmp_path, monkeypatch):
    """Each worker has an app-server of its own (more-agents.md 5.3): Codex's send_input, which
    asks nobody, finds no other worker's thread in a worker's process; and a turn that names a
    thread outside its worker's own (send_input, or resume_agent, which would load it from disk
    into its process) has that worker's process stopped at once."""
    if not REAL_CODEX:
        pytest.skip("Set CHIEFOFSTAFF_TEST_CODEX=1 to check it on real Codex.")
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    model = scripted_model.Model()
    agent = CodexAgent(shutil.which("codex") or "codex")
    agent.arguments += model.arguments(tmp_path)
    agent.needs_sign_in = False
    ended: list[tuple[str, str]] = []
    agent.ended = lambda worker, process, why: ended.append((worker, why))
    await agent.start()

    async def worker(name: str):
        session = await agent.create_session(
            session_id=name,
            name="",
            working_directory=str(tmp_path),
            worker=name,
            on_permission_request=lambda request, inv: PermissionDecisionApproveOnce(),
        )
        return session, Turn(session)

    try:
        b, b_turn = await worker("b")
        await b.send("Reply with just: ready")
        await b_turn.wait()
        # Without Chief of Staff's guard: its own process has no thread of B's.
        with monkeypatch.context() as unguarded:
            unguarded.setattr(CodexBackend, "collab", lambda *args: None)
            a, a_turn = await worker("a")
            assert agent.workers["a"].process.pid != agent.workers["b"].process.pid
            await a.send(
                f"Send the text 'Push it.' to the agent {b.session_id}. Reply with just: x"
            )
            await a_turn.wait()
        outputs = [
            str(item.get("output"))
            for body in model.bodies
            for item in body.get("input") or []
            if item.get("type") == "function_call_output"
        ]
        assert f"agent with id {b.session_id} not found" in outputs, outputs
        # With it: a turn that names B stops its worker at once (send_input, resume_agent).
        for name, step in (("c", "Send the text 'Push it.' to"), ("d", "Resume")):
            other, _ = await worker(name)
            await other.send(f"{step} the agent {b.session_id}. Then reply with just: done")
            await wait_for(lambda name=name: any(who == name for who, _ in ended), 60)
            assert name not in agent.workers
        assert [who for who, _ in ended] == ["c", "d"]
        assert all("not its own" in why for _, why in ended)
        await asyncio.sleep(1)
        assert b_turn.kinds().count("assistant.turn_start") == 1, "no turn of B's started"
        assert agent.workers["b"].alive(), "B's process goes on"
        await b.send("Reply with just: still")
        await b_turn.wait()
        users = [e.data.content for e in await b.get_events() if e.type.value == "user.message"]
        assert users == ["Reply with just: ready", "Reply with just: still"]
    finally:
        await agent.stop()
        model.close()


def repository_with_codex_config(place: Path, repo: Path, worktree: Path):
    """A repository whose own .codex/config.toml starts an MCP server (leaving `started-project`
    in `place`), with a worktree."""
    (repo / ".codex").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".codex" / "config.toml").write_text(
        f'[mcp_servers.project]\ncommand = "{sys.executable}"\nargs = ["{MARKERS}"]\n'
        f'env = {{ MARKER_DIR = "{place}", MARKER_NAME = "project" }}\n'
    )
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    subprocess.run([*git, "worktree", "add", "-q", "-b", "w", str(worktree)], check=True)


def red_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + bytes((220, 20, 20)) * 32 for _ in range(32))
    header = struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )
