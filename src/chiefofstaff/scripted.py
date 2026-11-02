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
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from copilot.generated.rpc import PermissionDecisionReject
from copilot.tools import ToolInvocation

from .backend import Capabilities


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


Step = Say | CallTool | Shell | WriteFile | AskOwner | Wait
Brain = Callable[["ScriptedSession", str], AsyncIterator[Step]]

SCRIPTED = Capabilities(
    name="Scripted demo",
    steering=True,
    resume=True,
    permission_callbacks=True,
    mcp_sign_in=False,
    sandbox=False,
)


def event(kind: str, **data: Any) -> SimpleNamespace:
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

    def emit(self, kind: str, **data: Any):
        if self.closed:
            return
        for handler in list(self.handlers):
            handler(event(kind, **data))

    async def send(self, prompt: str, mode: str = "enqueue") -> str:
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

    async def set_model(self, model: str, **tuning: Any):
        self.model = model

    async def get_events(self) -> list[Any]:
        return [event("user.message", content=text) for who, text in self.history if who == "user"]

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
        while self.steering:
            steer = self.steering.pop(0)
            self.history.append(("user", steer))
            self.emit("user.message", content=steer)
            await self.follow(self.backend.brain(self, steer))
        self.emit("assistant.turn_end")

    async def follow(self, script: AsyncIterator[Step]):
        result: Any = None
        try:
            step = await anext(script)
            while True:
                result = await self.do(step)
                if self.steering and isinstance(step, (Shell, Wait)):
                    return  # The owner steered: the rest of this script no longer applies.
                step = await script.asend(result)
        except StopAsyncIteration:
            pass

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

    def start_tool(self, name: str, arguments: dict) -> str:
        call_id = f"call_{next(self.calls)}"
        self.usage(name)
        request = SimpleNamespace(tool_call_id=call_id, name=name, arguments=arguments)
        self.emit("assistant.message", content="", tool_requests=[request])
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
        call_id = self.start_tool("bash", {"command": step.command})
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
        text = f"{output}\n<shellId: {next(self.calls)} completed with exit code {code}>".lstrip()
        self.end_tool(call_id, text, code == 0)
        return output

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


async def nothing(session: ScriptedSession, prompt: str) -> AsyncIterator[Step]:
    yield Say("OK")


class ScriptedBackend:
    capabilities = SCRIPTED

    def __init__(self, brain: Brain = nothing):
        self.brain = brain
        self.histories: dict[str, list[tuple[str, str]]] = {}
        self.sessions: dict[str, ScriptedSession] = {}

    async def start(self):
        pass

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
        return []

    async def get_session_metadata(self, session_id: str) -> Any:
        return SimpleNamespace(session_id=session_id) if session_id in self.histories else None

    async def create_session(self, *, session_id: str, name: str, **options: Any):
        self.histories[session_id] = []
        session = ScriptedSession(self, session_id, options)
        self.sessions[session_id] = session
        return session

    async def resume_session(self, session_id: str, **options: Any):
        if session_id not in self.histories:
            raise RuntimeError("Session not found")
        session = ScriptedSession(self, session_id, options)
        self.sessions[session_id] = session
        return session

    async def mcp_sign_in_url(self, session: Any, server: str) -> str:
        return ""

    def sandbox(self, policy: Any) -> None:
        return None

    async def gh_works_in_sandbox(self) -> bool | None:
        return None
