"""A backend without a model: each session follows a script. Runs the demo and contract tests.

A script ("brain") is an async generator called for every message a session receives. It
yields steps, and each `yield` returns the step's result, so a script can use what a tool said:

    async def brain(session, prompt):
        started = yield CallTool("spawn_worker", {"title": "Fix login", "instructions": "..."})
        yield Say("Started a worker.")

Steps go through the same callbacks as a real agent's actions: custom tools run their
handlers, shell commands and file writes ask on_permission_request first, and AskOwner asks
on_user_input_request. Events mirror the Copilot CLI's names and fields, so the runtime cannot
tell the difference.

AgentLikeBackend follows a script the way another coding agent's adapter would show that agent
to the runtime (a Profile: CODEX_LIKE, CLAUDE_LIKE): the same events, but steering only if the
agent can, a queue of its own whose next turn it starts only once admitted, commands left running
that end with end messages of its own (Background), child agents (Child), exit codes only if the
agent gives them, no Copilot credits, and session ids the agent may choose itself. Tests run the
runtime's paths that do not depend on the agent on them.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from uuid import uuid4

from copilot.generated.rpc import PermissionDecisionReject
from copilot.tools import ToolInvocation

from .backend import SESSION_ID, Capabilities


@dataclass
class Say:
    text: str


@dataclass
class CallTool:
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class Shell:
    command: str
    output: str = ""
    exit_code: int = 0
    seconds: float = 0.4
    # Run the command for real in the session's folder (the demo commits in its own repo).
    run: bool = False
    # Said on the way to the command, as an agent narrates its next step.
    note: str = ""


@dataclass
class WriteFile:
    """Write a file under the session's working directory (a real file, so diffs are real)."""

    path: str
    content: str


@dataclass
class AskOwner:
    question: str
    choices: list[str] = field(default_factory=list)


@dataclass
class Wait:
    seconds: float


@dataclass
class Background:
    """A command another agent leaves running (a dev server): the step returns at once, and the
    command ends after `seconds`, or when it is stopped, with the agent's own end message."""

    command: str
    seconds: float = 3600.0
    exit_code: int = 0
    # Once it ends by itself, the agent starts a turn of its own on that.
    wakes: bool = False


@dataclass
class Child:
    """A child agent of the session (a subagent, a child thread) taking these steps: its events
    are its parent call's, and its calls to Chief of Staff's tools are its session's."""

    steps: list[Any] = field(default_factory=list)


Step = Say | CallTool | Shell | WriteFile | AskOwner | Wait | Background | Child
Brain = Callable[["ScriptedSession", str], AsyncGenerator[Step, Any]]

SCRIPTED = Capabilities(
    name="Scripted demo",
    steering=True,
    resume=True,
    permission_callbacks=True,
    mcp_sign_in=False,
    sandbox=False,
)


def event(kind: str, /, **data: Any) -> SimpleNamespace:
    return SimpleNamespace(type=SimpleNamespace(value=kind), data=SimpleNamespace(**data))


class ScriptedSession:
    def __init__(self, backend: ScriptedBackend, session_id: str, options: dict):
        self.backend = backend
        self.session_id = session_id
        self.options = options
        self.model = options.get("model") or "demo-model"
        self.tools = {tool.name: tool for tool in options.get("tools") or []}
        self.history: list[tuple[str, str]] = backend.histories[session_id]
        self.handlers: list[Callable[[Any], None]] = []
        self.inbox: list[str] = []
        self.steering: list[str] = []
        self.turn: asyncio.Task | None = None
        self.closed = False
        self.calls = itertools.count(1)

    @property
    def system_prompt(self) -> str:
        return (self.options.get("system_message") or {}).get("content", "")

    @property
    def cwd(self) -> Path:
        return Path(self.options.get("working_directory") or ".")

    def on(self, handler: Callable[[Any], None]) -> Callable[[], None]:
        self.handlers.append(handler)
        return lambda: self.handlers.remove(handler)

    def emit(self, kind: str, /, **data: Any):
        if self.closed:
            return
        for handler in list(self.handlers):
            handler(event(kind, **data))

    async def send(
        self, prompt: str, *, mode: str | None = "enqueue", attachments: list | None = None
    ) -> str:
        if self.closed:
            raise RuntimeError("The session is disconnected.")
        if mode == "immediate" and self.turn and not self.turn.done():
            self.steering.append(prompt)
        else:
            self.inbox.append(prompt)
            if not self.turn or self.turn.done():
                self.turn = asyncio.ensure_future(self.run())
        return f"m-{uuid4().hex[:8]}"

    async def abort(self):
        if self.turn and not self.turn.done():
            self.turn.cancel()
            await asyncio.gather(self.turn, return_exceptions=True)
            self.inbox.clear()
            self.emit("abort", reason="user initiated")
            self.emit("session.idle")

    async def disconnect(self):
        if self.turn and not self.turn.done():
            self.turn.cancel()
            await asyncio.gather(self.turn, return_exceptions=True)
        self.closed = True

    async def set_model(
        self, model: str, *, reasoning_effort: str | None = None, context_tier: Any = None
    ):
        self.model = model

    async def get_events(self) -> list[Any]:
        kinds = {"user": "user.message", "assistant": "assistant.message"}
        return [event(kinds[who], content=text) for who, text in self.history if who in kinds]

    async def run(self):
        try:
            while self.inbox and not self.closed:
                await self.answer(self.inbox.pop(0))
        except Exception as exc:  # A script failed: report it as the CLI reports errors.
            self.emit("session.error", message=f"{type(exc).__name__}: {exc}")
            return
        self.emit("session.idle")

    async def answer(self, prompt: str):
        self.history.append(("user", prompt))
        self.emit("user.message", content=prompt)
        self.emit("assistant.turn_start", turn_id=str(next(self.calls)))
        await self.follow(self.backend.brain(self, prompt))
        await self.take_steering()
        self.emit("assistant.turn_end")

    async def follow(self, script: AsyncGenerator[Step, Any]):
        result: Any = None
        try:
            step = await anext(script)
            while True:
                result = await self.do(step)
                # A message steered into the turn is read after this step; then the script
                # goes on (it can check what it was told and stop).
                await self.take_steering()
                step = await script.asend(result)
        except StopAsyncIteration:
            pass

    async def take_steering(self):
        while self.steering:
            steer = self.steering.pop(0)
            self.history.append(("user", steer))
            self.emit("user.message", content=steer)
            await self.follow(self.backend.brain(self, steer))

    def usage(self, text: str):
        self.emit(
            "assistant.usage",
            model=self.model,
            input_tokens=2000 + 40 * len(self.history),
            output_tokens=max(1, len(text) // 4),
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost=0,
            copilot_usage=SimpleNamespace(total_nano_aiu=40_000_000),
        )

    async def do(self, step: Step) -> Any:
        await asyncio.sleep(0)  # Earlier events are handled first, as with a real backend.
        if isinstance(step, Say):
            self.usage(step.text)
            self.history.append(("assistant", step.text))
            self.emit("assistant.message", content=step.text, tool_requests=None)
            return None
        if isinstance(step, Wait):
            # A message steered into the turn ends the wait, as it would interrupt a model.
            waited = 0.0
            while waited < step.seconds and not self.steering:
                await asyncio.sleep(0.2)
                waited += 0.2
            return None
        if isinstance(step, CallTool):
            return await self.call_tool(step)
        if isinstance(step, Shell):
            return await self.shell(step)
        if isinstance(step, WriteFile):
            return await self.write(step)
        if isinstance(step, AskOwner):
            ask = self.options.get("on_user_input_request")
            if not ask:
                return {"answer": "", "wasFreeform": True}
            request = {"question": step.question, "choices": step.choices, "allowFreeform": True}
            return await ask(request, {"sessionId": self.session_id})
        raise TypeError(f"Unknown step {step!r}")

    def start_tool(self, name: str, arguments: dict, note: str = "") -> str:
        call_id = f"call_{next(self.calls)}"
        self.usage(name)
        request = SimpleNamespace(tool_call_id=call_id, name=name, arguments=arguments)
        self.emit("assistant.message", content=note, tool_requests=[request])
        self.emit("tool.execution_start", tool_call_id=call_id, tool_name=name, arguments=arguments)
        return call_id

    def end_tool(self, call_id: str, text: str, success: bool):
        self.history.append(("tool", text))
        result = SimpleNamespace(content=text)
        self.emit("tool.execution_complete", tool_call_id=call_id, success=success, result=result)

    async def call_tool(self, step: CallTool) -> str:
        tool = self.tools.get(step.name)
        call_id = self.start_tool(step.name, step.arguments)
        if not tool:
            text = f"Tool {step.name} is not available."
            self.end_tool(call_id, text, False)
            return text
        invocation = ToolInvocation(
            session_id=self.session_id,
            tool_call_id=call_id,
            tool_name=step.name,
            arguments=step.arguments,
        )
        result = tool.handler(invocation)
        if inspect.isawaitable(result):
            result = await result
        text = getattr(result, "text_result_for_llm", None) or str(result)
        self.end_tool(call_id, text, getattr(result, "result_type", "success") == "success")
        return text

    async def permitted(self, request: SimpleNamespace) -> str:
        """ "" when allowed, else the reason it was denied."""
        check = self.options.get("on_permission_request")
        if not check:
            return ""
        decision = check(request, {"session_id": self.session_id})
        if inspect.isawaitable(decision):
            decision = await decision
        if isinstance(decision, PermissionDecisionReject):
            return getattr(decision, "feedback", "") or "Denied."
        return ""

    async def shell(self, step: Shell) -> str:
        request = SimpleNamespace(
            kind="shell", full_command_text=step.command, possible_urls=[], intention=""
        )
        if denied := await self.permitted(request):
            return f"Permission denied: {denied}"
        call_id = self.start_tool("bash", {"command": step.command}, step.note)
        output, code = step.output, step.exit_code
        if step.run:
            process = await asyncio.create_subprocess_shell(
                step.command,
                cwd=self.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            raw, _ = await process.communicate()
            output, code = raw.decode(errors="replace").strip(), process.returncode or 0
        await asyncio.sleep(step.seconds)
        self.end_shell(call_id, output, code)
        return output

    def end_shell(self, call_id: str, output: str, code: int):
        text = f"{output}\n<shellId: {next(self.calls)} completed with exit code {code}>".lstrip()
        self.end_tool(call_id, text, code == 0)

    async def write(self, step: WriteFile) -> str:
        target = (self.cwd / step.path).resolve()
        request = SimpleNamespace(kind="write", resolved_path=str(target), file_name=step.path)
        if denied := await self.permitted(request):
            return f"Permission denied: {denied}"
        call_id = self.start_tool("create", {"path": str(target)})
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, step.content)
        self.end_tool(call_id, f"Created file {target}", True)
        return str(target)


async def nothing(session: ScriptedSession, prompt: str) -> AsyncGenerator[Step, Any]:
    yield Say("OK")


class ScriptedBackend:
    capabilities = SCRIPTED
    session_type: ClassVar[type[ScriptedSession]] = ScriptedSession

    def __init__(
        self, brain: Brain = nothing, others: list[dict] | None = None, home: Path | None = None
    ):
        self.brain = brain
        self.histories: dict[str, list[tuple[str, str]]] = {}
        self.sessions: dict[str, ScriptedSession] = {}
        # Sessions of the owner that Chief of Staff does not manage: their metadata (as
        # list_sessions returns it), "open" (in a terminal) and "log" (their events).
        self.others = others or []
        # A Copilot home of its own: start() writes those sessions' logs there, as the CLI
        # would, for others.py to read (session_log). Without one they have no log file.
        self.home = home

    async def start(self):
        if self.home:
            await asyncio.to_thread(self.write_logs)

    def write_logs(self):
        for other in self.others:
            log = self.session_log(other["meta"].session_id)
            if log and "log" in other:
                log.parent.mkdir(parents=True, exist_ok=True)
                lines = [
                    json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in other["log"]
                ]
                log.write_bytes("".join(line + "\n" for line in lines).encode())

    async def stop(self):
        for session in self.sessions.values():
            await session.disconnect()

    async def version(self) -> str:
        return "scripted"

    async def get_auth_status(self) -> Any:
        return SimpleNamespace(isAuthenticated=True)

    async def list_models(self) -> list[Any]:
        prices = SimpleNamespace(max_prompt_tokens=200_000, context_max=None, long_context=None)
        return [
            SimpleNamespace(
                id="demo-model",
                name="Demo model",
                policy=None,
                billing=SimpleNamespace(token_prices=prices),
                capabilities=SimpleNamespace(limits=SimpleNamespace(max_prompt_tokens=200_000)),
                supported_reasoning_efforts=["low", "medium", "high"],
                default_reasoning_effort="medium",
            )
        ]

    async def list_sessions(self) -> list[Any]:
        return [other["meta"] for other in self.others]

    def other(self, session_id: str) -> dict | None:
        return next((o for o in self.others if o["meta"].session_id == session_id), None)

    async def get_session_metadata(self, session_id: str) -> Any:
        if other := self.other(session_id):
            return other["meta"]
        return SimpleNamespace(session_id=session_id) if session_id in self.histories else None

    async def create_session(self, *, session_id: str, name: str, **options: Any):
        self.histories[session_id] = []
        session = self.session_type(self, session_id, options)
        self.sessions[session_id] = session
        return session

    async def resume_session(self, session_id: str, **options: Any):
        if session_id not in self.histories and (other := self.other(session_id)):
            # One of the owner's own sessions: its conversation so far is its log's.
            said = {"user.message": "user", "assistant.message": "assistant"}
            self.histories[session_id] = [
                (said[entry["type"]], entry["data"].get("content", ""))
                for entry in other.get("log", [])
                if entry["type"] in said and entry["data"].get("content")
            ]
        if session_id not in self.histories:
            raise RuntimeError("Session not found")
        session = self.session_type(self, session_id, options)
        self.sessions[session_id] = session
        return session

    async def mcp_sign_in_url(self, session: Any, server: str) -> str:
        return ""

    def sandbox(self, policy: Any) -> None:
        return None

    async def gh_works_in_sandbox(self) -> bool | None:
        return None

    async def open_elsewhere(self) -> dict[str, int]:
        return {other["meta"].session_id: 1 for other in self.others if other.get("open")}

    def processes(self) -> dict[int, str]:
        return {}

    def alive(self) -> bool:
        return True

    async def read_usage(self, session_id: str, offset: int) -> tuple[list[dict], int]:
        return [], offset

    def session_log(self, session_id: str) -> Path | None:
        if not self.home or not SESSION_ID.fullmatch(session_id):
            return None
        return self.home / "session-state" / session_id / "events.jsonl"

    async def read_history(self, session_id: str, offset: int) -> tuple[list[dict], int, float]:
        if other := self.other(session_id):
            log = other.get("log", [])
            return log[offset:], len(log), 0.0
        return [], offset, 0.0


@dataclass(frozen=True)
class Profile:
    """How another coding agent shows to the runtime through its adapter."""

    capabilities: Capabilities
    # Its commands' ends carry their exit codes.
    exit_codes: bool
    # It chooses its sessions' ids (Chief of Staff's is not taken).
    assigns_ids: bool


def agent_capabilities(name: str, steering: bool) -> Capabilities:
    return Capabilities(
        name=name,
        steering=steering,
        resume=True,
        permission_callbacks=True,
        mcp_sign_in=False,
        sandbox=False,
    )


# Codex through its app-server: it steers, gives exact exit codes and chooses its threads' ids.
CODEX_LIKE = Profile(
    agent_capabilities("Codex-like", steering=True), exit_codes=True, assigns_ids=True
)
# Claude Code through its SDK: a message waits for the turn's end, no exit codes, and its
# sessions take Chief of Staff's ids.
CLAUDE_LIKE = Profile(
    agent_capabilities("Claude-like", steering=False), exit_codes=False, assigns_ids=False
)


# What a child agent's steps show as: its parent call's (as a Copilot sub-agent's do).
CHILD_EVENTS = frozenset({"assistant.message", "tool.execution_start", "tool.execution_complete"})


class AgentLikeSession(ScriptedSession):
    """A session of another coding agent, as its adapter shows it (see Profile)."""

    backend: AgentLikeBackend

    def __init__(self, backend: AgentLikeBackend, session_id: str, options: dict):
        super().__init__(backend, session_id, options)
        self.profile = backend.profile
        # Commands it left running: shell id -> set to stop it.
        self.left_running: dict[str, asyncio.Event] = {}
        self.ends: set[asyncio.Task] = set()
        self.shells = itertools.count(1)
        # The call of the child agent whose steps it takes now.
        self.child_of = ""
        # Its model is at work on a turn (a steer reaches it), not between turns.
        self.answering = False

    async def send(
        self, prompt: str, *, mode: str | None = "enqueue", attachments: list | None = None
    ) -> str:
        if self.closed:
            raise RuntimeError("The session is disconnected.")
        if not self.turn or self.turn.done():
            self.turn = asyncio.ensure_future(self.turns(prompt))  # A turn on it starts now.
        elif mode == "immediate" and self.profile.capabilities.steering and self.answering:
            self.steering.append(prompt)
        else:
            self.inbox.append(prompt)  # In its queue, for its turn's end.
        return f"m-{uuid4().hex[:8]}"

    def emit(self, kind: str, /, **data: Any):
        if self.child_of and kind in CHILD_EVENTS:
            data.setdefault("parent_tool_call_id", self.child_of)
        super().emit(kind, **data)

    async def turns(self, prompt: str, woken: bool = False):
        """A turn on what it was sent (`woken`: one it starts by itself on a notice of its own),
        then one on each message it queued meanwhile, each only once admitted; at the end, idle
        unless a command it left running holds the session."""
        try:
            await self.answer(prompt, woken)
            while self.inbox and not self.closed:
                if not await self.admitted():
                    self.inbox.clear()  # Given back: Chief of Staff sends it again.
                    break
                await self.answer(self.inbox.pop(0))
        except Exception as exc:
            self.emit("session.error", message=f"{type(exc).__name__}: {exc}")
            return
        self.turn_over()

    async def answer(self, prompt: str, woken: bool = False):
        self.answering = True
        try:
            if not woken:
                await super().answer(prompt)
                return
            self.emit("assistant.turn_start", turn_id=str(next(self.calls)))
            await self.follow(self.backend.brain(self, prompt))
            await self.take_steering()
            self.emit("assistant.turn_end")
        finally:
            self.answering = False

    async def admitted(self) -> bool:
        admit = self.backend.admit
        return admit is None or not await admit(self)

    async def abort(self):
        """Its turn ends (and its child agents with it); what it left running goes on until
        stopped (stop_background)."""
        if self.turn and not self.turn.done():
            self.turn.cancel()
            await asyncio.gather(self.turn, return_exceptions=True)
            self.inbox.clear()
            self.emit("abort", reason="user initiated")
            self.turn_over()

    def turn_over(self):
        if self.left_running:
            self.emit("assistant.idle")  # What it left running holds session.idle.
        else:
            self.emit("session.idle")

    def usage(self, text: str):
        """Its usage is in its own unit, not Copilot's credits: no assistant.usage."""

    async def do(self, step: Step) -> Any:
        if isinstance(step, Background):
            await asyncio.sleep(0)
            return await self.leave_running(step)
        if isinstance(step, Child):
            await asyncio.sleep(0)
            return await self.child(step)
        return await super().do(step)

    def end_shell(self, call_id: str, output: str, code: int):
        self.history.append(("tool", output))
        self.emit(
            "tool.execution_complete",
            tool_call_id=call_id,
            success=code == 0,
            result=SimpleNamespace(content=output),
            shell_execution=SimpleNamespace(exit_code=code) if self.profile.exit_codes else None,
        )

    async def leave_running(self, step: Background) -> str:
        request = SimpleNamespace(
            kind="shell", full_command_text=step.command, possible_urls=[], intention=""
        )
        if denied := await self.permitted(request):
            return f"Permission denied: {denied}"
        call_id = self.start_tool("bash", {"command": step.command})
        shell = f"bg-{next(self.shells)}"
        self.left_running[shell] = asyncio.Event()
        ending = asyncio.ensure_future(self.keep_running(shell, step))
        self.ends.add(ending)
        ending.add_done_callback(self.ends.discard)
        # Shaped as Copilot says it, so receipts know the command runs on.
        self.end_tool(call_id, f"<command started in background with shellId: {shell}>", True)
        return shell

    async def keep_running(self, shell: str, step: Background):
        try:
            await asyncio.wait_for(self.left_running[shell].wait(), step.seconds)
            stopped = True
        except TimeoutError:
            stopped = False
        del self.left_running[shell]
        code = step.exit_code if self.profile.exit_codes and not stopped else None
        kind = SimpleNamespace(type="shell_completed", shell_id=shell, exit_code=code)
        self.emit("system.notification", kind=kind)
        if self.turn and not self.turn.done():
            return  # Its turn goes on.
        if step.wakes and not stopped and not self.closed:
            self.turn = asyncio.ensure_future(self.turns(f"`{step.command}` ended.", woken=True))
        elif not self.left_running:
            self.emit("session.idle")

    async def child(self, step: Child) -> None:
        call_id = self.start_tool("task", {"description": "a child agent"})
        self.child_of = call_id
        try:
            for inner in step.steps:
                await self.do(inner)
        finally:
            self.child_of = ""
        self.end_tool(call_id, "The child agent finished.", True)

    async def running_background(self) -> int:
        return len(self.left_running)

    async def stop_background(self) -> int:
        stopping = list(self.left_running.values())
        for stop in stopping:
            stop.set()
        await asyncio.gather(*self.ends, return_exceptions=True)
        return len(stopping)

    async def take_queued(self) -> int:
        taken, self.inbox = len(self.inbox), []
        return taken

    async def queue_holds(self) -> bool:
        return bool(self.inbox or self.steering)


class AgentLikeBackend(ScriptedBackend):
    """Another coding agent's backend, scripted (see Profile)."""

    session_type = AgentLikeSession

    def __init__(self, brain: Brain = nothing, profile: Profile = CODEX_LIKE):
        super().__init__(brain)
        self.profile = profile
        self.capabilities = profile.capabilities
        # Chief of Staff's admission, asked before a turn on what it queued ("" when admitted,
        # else why not): refused, it gives what it queued back.
        self.admit: Callable[[AgentLikeSession], Awaitable[str]] | None = None
        self.crashed = False

    def alive(self) -> bool:
        return not self.crashed

    async def create_session(self, *, session_id: str, name: str, **options: Any):
        if self.profile.assigns_ids:
            session_id = str(uuid4())
        return await super().create_session(session_id=session_id, name=name, **options)

    async def stop(self):
        for session in self.sessions.values():
            if isinstance(session, AgentLikeSession):
                await session.stop_background()
        await super().stop()
