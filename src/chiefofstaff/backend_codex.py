"""Codex workers: the owner's Codex CLI, driven through its app-server (JSON-RPC 2.0 over stdio).

One `codex app-server` process, started under Chief of Staff's supervisor (agentrun.py), runs
every Codex thread, as one Copilot CLI runs every Copilot session. The adapter shows Codex to
the runtime the way Copilot shows itself (backend.AgentBackend and AgentSession), so the runtime
cannot tell the difference:

- Threads are sessions. Codex chooses their ids; open_live keeps them under `copilot:`/`alias:`.
  Each thread starts with `sandbox: danger-full-access`, `approvalPolicy: untrusted` and
  `approvalsReviewer: user` (also pinned on the app-server's command line), and the thread's
  response must say so: every command no rule of the owner's allows, every edit and, with Ask
  first, every MCP call then asks Chief of Staff's permission callback (Events.on_permission),
  as Copilot's requests do. A refusal's reason reaches the model as a note steered into its turn.
- Items are events with Copilot's names: a `userMessage` is the message's acknowledgement
  (user.message), commands are `bash` with their exact exit codes, each changed file an edit,
  Chief of Staff's own tools (`dynamicTools`) and MCP calls tool calls. A child thread (a
  subagent) shows as one `task` call of its worker, its calls to Chief of Staff's tools are its
  worker's, and its usage is its worker's.
- A command the turn left running is a background terminal: the turn's end is held
  (assistant.idle) until it ends, which settles its receipt (system.notification).
- Messages sent while a turn runs steer into it (`turn/steer`) or wait in the adapter's queue;
  the next turn on them starts only once Chief of Staff admits it (`admit`, Runtime.admit_turn).
- Usage arrives as cumulative counters per thread (`agent.usage` events), in tokens.

The repository's own `.codex/` configuration loads only where the owner trusts that repository
in Codex: each thread is told its folder's trust explicitly, because Codex otherwise marks a
folder it starts a writable thread in as trusted in the owner's config.toml (0.167.2).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shlex
import shutil
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from copilot.generated.rpc import PermissionDecisionReject
from copilot.tools import ToolInvocation

from .backend import Capabilities, descendants, end_processes, older, process_table
from .envelope import wrap
from .policy import GITHUB_MCP

logger = logging.getLogger(__name__)

CODEX_AGENT = "codex"

CODEX = Capabilities(
    name="Codex",
    steering=True,
    resume=True,
    permission_callbacks=True,
    mcp_sign_in=False,
    sandbox=False,
)
# The Codex CLI versions this release was tested with (the contract test, live runs). Older ones
# do not run; with Ask first, only those up to the next minor version, whose every kind of
# action was shown to ask Chief of Staff first.
OLDEST_CODEX = "0.167.2"
ASK_FIRST_BEFORE = "0.168"
# How long a request to the app-server may take (seconds), and one that starts a thread.
REQUEST_TIMEOUT = float(os.environ.get("CHIEFOFSTAFF_CODEX_TIMEOUT", "60"))
# What a thread's settings must read back as (thread/start and thread/resume answers).
APPROVAL_POLICY = "untrusted"
REVIEWER = "user"
FULL_ACCESS = "dangerFullAccess"
# A shell Codex runs a command line in: `/bin/zsh -lc '<command>'`.
SHELLS = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh"})
# Turns' items a worker's timeline does not show.
QUIET_ITEMS = frozenset({"reasoning", "plan", "hookPrompt", "contextCompaction", "sleep"})
# How the adapter's own notes (why Chief of Staff refused an action) begin: not messages it was
# sent.
REFUSED_NOTE = '<message from="system" type="refused">'
# A server that is GitHub's (its name, command or URL): its writes are the owner's to approve.
GITHUB = re.compile(r"github", re.IGNORECASE)


class CodexError(RuntimeError):
    """An error the app-server answered a request with."""


def inner_command(command: str) -> str:
    """The command line a worker ran, without the shell Codex wraps it in."""
    try:
        words = shlex.split(command)
    except ValueError:
        return command
    if (
        len(words) == 3
        and Path(words[0]).name in SHELLS
        and words[1].startswith("-")
        and words[1].endswith("c")
    ):
        return words[2]
    return command


def mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def text_of(content: Any) -> str:
    """The text of a message's input items (images and mentions left out)."""
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


def version_of(user_agent: str) -> str:
    """The CLI's version, from the app-server's user agent ("chiefofstaff/0.167.2 (Mac OS…")."""
    found = re.search(r"/(\d+\.\d+\.\d+[\w.-]*)", user_agent or "")
    return found.group(1) if found else ""


def github_server(name: str, config: Any) -> bool:
    """Whether an MCP server in the owner's Codex config is GitHub's: by its name, its command
    and arguments, or its URL."""
    if GITHUB.search(name):
        return True
    if not isinstance(config, dict):
        return False
    parts = [config.get("command"), config.get("url"), *(config.get("args") or [])]
    return any(isinstance(part, str) and GITHUB.search(part) for part in parts)


def trust_levels(owners: dict, pairs: list[tuple[str, str]]) -> dict[str, dict]:
    """Each folder's trust for a thread (`projects` in its config): as the owner trusts, in Codex,
    the repository it is a checkout of (`pairs`: folder, repository), under both paths."""
    trusted = [
        Path(path).expanduser().resolve()
        for path, project in owners.items()
        if isinstance(project, dict) and project.get("trust_level") == "trusted"
    ]
    projects = {}
    for folder, origin in pairs:
        target = Path(origin).expanduser().resolve()
        level = "trusted" if any(target.is_relative_to(root) for root in trusted) else "untrusted"
        for path in {folder, origin, str(Path(folder).resolve()), str(target)}:
            projects[path] = {"trust_level": level}
    return projects


def event(kind: str, /, **data: Any) -> SimpleNamespace:
    return SimpleNamespace(type=SimpleNamespace(value=kind), data=SimpleNamespace(**data))


def ask_first_tested(version: str) -> bool:
    return bool(version) and not older(version, OLDEST_CODEX) and older(version, ASK_FIRST_BEFORE)


class CodexBackend:
    """The owner's Codex CLI: one app-server process runs every thread."""

    capabilities = CODEX

    def __init__(self, command: str):
        self.command = command
        # Approvals are Chief of Staff's to answer, never a reviewer agent's.
        self.arguments = ["app-server", "-c", f'approvals_reviewer="{REVIEWER}"']
        self.process: asyncio.subprocess.Process | None = None
        # Chief of Staff's end of the supervisor's lifeline: closed with this process.
        self.lifeline: int | None = None
        self.reader: asyncio.Task | None = None
        self.errors: asyncio.Task | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.ids = 0
        self.cli_version = ""
        # Live sessions by thread id; a child thread (a subagent) by the thread that started it.
        self.sessions: dict[str, CodexSession] = {}
        self.parents: dict[str, str] = {}
        # The paths each file change touches, by item id (its approval does not name them).
        self.changes: dict[str, list[dict]] = {}
        self.tasks: set[asyncio.Task] = set()
        # The supervisor's process and those it started, as last seen: pid -> command line.
        self.started: dict[int, str] = {}
        # Chief of Staff's admission, asked before a turn starts: "" when admitted, else why
        # not (Runtime.admit_turn); and whether that admission still holds just before the
        # turn begins (Runtime.begins_turn). Without them every turn is admitted.
        self.admit: Callable[[CodexSession], Awaitable[str]] | None = None
        self.begins: Callable[[CodexSession], bool] | None = None

    # The process

    async def start(self) -> None:
        if not shutil.which(self.command):
            raise RuntimeError(f"Codex CLI '{self.command}' not found. Install it and sign in.")
        read, write = os.pipe()
        try:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "chiefofstaff.agentrun",
                "--lifeline",
                str(read),
                "--",
                self.command,
                *self.arguments,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={k: v for k, v in os.environ.items() if not k.startswith("CHIEFOFSTAFF_")},
                pass_fds=(read,),
                # Thread items (a command's whole output, a diff) come on one line.
                limit=64 * 1024 * 1024,
            )
        except BaseException:
            os.close(write)
            raise
        finally:
            os.close(read)
        self.lifeline = write
        self.reader = asyncio.ensure_future(self.read())
        self.errors = asyncio.ensure_future(self.read_errors())
        try:
            answer = await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "chiefofstaff",
                        "title": "Chief of Staff",
                        "version": client_version(),
                    },
                    # Chief of Staff's own tools and commands left running need it.
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.notify("initialized")
            self.cli_version = version_of(answer.get("userAgent", ""))
            if older(self.cli_version, OLDEST_CODEX):
                raise RuntimeError(
                    f"Codex {self.cli_version or '(unknown version)'} is older than the versions "
                    f"tested ({OLDEST_CODEX}): update it."
                )
            await self.check_requirements()
            if not (await self.get_auth_status()).isAuthenticated:
                raise RuntimeError("Codex is not signed in: run `codex login`.")
        except BaseException:
            await self.stop()
            raise

    async def check_requirements(self):
        """An administrator's requirements may forbid what Chief of Staff's checks rely on."""
        answer = await self.request("configRequirements/read", {})
        required = answer.get("requirements") or {}
        allowed = {
            "approval policy": (required.get("allowedApprovalPolicies"), APPROVAL_POLICY),
            "sandbox mode": (required.get("allowedSandboxModes"), "danger-full-access"),
            "approvals reviewer": (required.get("allowedApprovalsReviewers"), REVIEWER),
        }
        for what, (values, needed) in allowed.items():
            if isinstance(values, list) and needed not in values:
                raise RuntimeError(
                    f"Your organization's Codex settings do not allow the {what} Chief of Staff "
                    f"needs ({needed})."
                )

    async def stop(self) -> None:
        process, self.process = self.process, None
        for session in list(self.sessions.values()):
            session.closed = True
            if session.pump:
                session.pump.cancel()
        if process:
            if process.stdin and not process.stdin.is_closing():
                process.stdin.close()  # The app-server ends at the end of its input.
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.terminate()  # The supervisor ends the CLI's tree.
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
        if self.lifeline is not None:
            with suppress(OSError):
                os.close(self.lifeline)
            self.lifeline = None
        for task in (self.reader, self.errors, *self.tasks):
            if task and task is not asyncio.current_task():
                task.cancel()
        self.fail_pending(ConnectionResetError("Codex stopped."))
        # What outlived the supervisor (it was killed): ended as Copilot's leftovers are.
        if self.started:
            left = await asyncio.to_thread(self.leftovers)
            if left:
                await asyncio.to_thread(end_processes, left)

    def leftovers(self) -> dict[int, str]:
        table = process_table()
        return {pid: c for pid, c in self.started.items() if table.get(pid, (0, ""))[1] == c}

    def alive(self) -> bool:
        process = self.process
        return (
            process is not None
            and process.returncode is None
            and self.reader is not None
            and not self.reader.done()
        )

    def processes(self) -> dict[int, str]:
        if self.process and self.process.returncode is None:
            table = process_table()
            self.started |= {
                pid: table[pid][1] for pid in descendants(table, self.process.pid) if pid in table
            }
        return dict(self.started)

    # JSON-RPC

    def write(self, message: dict):
        if not self.process or not self.process.stdin or self.process.stdin.is_closing():
            raise ConnectionResetError("Codex is not running.")
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())

    def send_request(self, method: str, params: dict) -> asyncio.Future:
        """Write a request now (nothing awaited before it is written); its answer comes later."""
        self.ids += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.ids] = future
        try:
            self.write({"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params})
        except BaseException:
            self.pending.pop(self.ids, None)
            raise
        return future

    async def request(self, method: str, params: dict) -> dict:
        return await self.answer(method, self.send_request(method, params))

    async def answer(self, method: str, future: asyncio.Future) -> dict:
        try:
            message = await asyncio.wait_for(future, REQUEST_TIMEOUT)
        except TimeoutError:
            raise CodexError(f"Codex did not answer {method} in time.") from None
        if "error" in message:
            error = message["error"] or {}
            raise CodexError(str(error.get("message") or error)[:500])
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict | None = None):
        self.write({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def respond(self, request_id: Any, result: dict | None = None, error: str = ""):
        with suppress(OSError, RuntimeError):
            if error:
                body = {"error": {"code": -32601, "message": error}}
            else:
                body = {"result": result or {}}
            self.write({"jsonrpc": "2.0", "id": request_id, **body})

    def fail_pending(self, error: BaseException):
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)
        self.pending.clear()

    async def read(self):
        if not (self.process and self.process.stdout):
            return
        try:
            async for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    logger.warning("Codex wrote a line that is not JSON: %r", line[:200])
                    continue
                if isinstance(message, dict):
                    self.received(message)
        except (ValueError, ConnectionError) as exc:  # A line past the limit, a pipe gone.
            logger.warning("Reading from Codex failed: %s", exc)
        finally:
            self.fail_pending(ConnectionResetError("Codex stopped."))
            for session in list(self.sessions.values()):
                session.lost()

    async def read_errors(self):
        if not (self.process and self.process.stderr):
            return
        async for line in self.process.stderr:
            text = line.decode(errors="replace").rstrip()
            if text:
                logger.debug("codex: %s", text[:1000])

    def received(self, message: dict):
        method = message.get("method")
        if method is None:
            key = message.get("id")
            future = self.pending.pop(key, None) if isinstance(key, int) else None
            if future and not future.done():
                future.set_result(message)
            return
        params = mapping(message.get("params"))
        if "id" in message:
            task = asyncio.ensure_future(self.serve(message["id"], method, params))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return
        self.observe(method, params)
        thread = params.get("threadId") or (params.get("thread") or {}).get("id") or ""
        if session := self.session_of(thread, params):
            session.inbox.put_nowait((method, params, thread))

    def observe(self, method: str, params: dict):
        """What requests and routing need before the session's events are handled in order."""
        item = mapping(params.get("item"))
        if method == "item/started" and item.get("type") == "fileChange":
            self.changes[item.get("id", "")] = list(item.get("changes") or [])
        elif method == "item/completed" and item.get("type") == "fileChange":
            self.changes.pop(item.get("id", ""), None)
        if item.get("type") == "subAgentActivity" and item.get("agentThreadId"):
            # A child thread (a subagent) of this one: its work is its worker's.
            self.parents.setdefault(item["agentThreadId"], params.get("threadId", ""))

    def root(self, thread: str) -> str:
        seen = set()
        while thread in self.parents and thread not in seen:
            seen.add(thread)
            thread = self.parents[thread]
        return thread

    def session_of(self, thread: str, params: dict) -> CodexSession | None:
        if not thread:
            return None
        if (session := self.sessions.get(self.root(thread))) is not None:
            return session
        # A child thread whose start was not seen: its turn names the turn that led to it.
        led_by = (params.get("turn") or {}).get("rootTurnId") or ""
        for session in self.sessions.values():
            if led_by and led_by in session.turns:
                self.parents[thread] = session.session_id
                return session
        return None

    async def serve(self, request_id: Any, method: str, params: dict):
        thread = params.get("threadId") or ""
        session = self.session_of(thread, params)
        try:
            if session is None:
                raise CodexError("Unknown thread.")
            result = await session.request(method, params, thread)
        except Exception as exc:
            if not isinstance(exc, CodexError):
                logger.warning("Answering Codex's %s failed.", method, exc_info=True)
            self.respond(request_id, error=str(exc)[:300] or type(exc).__name__)
            return
        self.respond(request_id, result)

    # The backend

    async def version(self) -> str:
        return self.cli_version

    async def get_auth_status(self) -> Any:
        answer = await self.request("account/read", {})
        account = answer.get("account") or {}
        # Only how it is signed in; never who, nor any credential.
        return SimpleNamespace(
            isAuthenticated=bool(account), authType=account.get("type", "") if account else ""
        )

    async def list_models(self) -> list[Any]:
        models, cursor = [], None
        for _ in range(20):
            answer = await self.request("model/list", {"cursor": cursor} if cursor else {})
            models += [model for model in answer.get("data") or [] if isinstance(model, dict)]
            if not (cursor := answer.get("nextCursor")):
                break
        return [
            SimpleNamespace(
                id=model.get("id", ""),
                name=model.get("displayName") or model.get("id", ""),
                policy=None,
                billing=None,
                capabilities=SimpleNamespace(limits=SimpleNamespace(max_prompt_tokens=0)),
                supported_reasoning_efforts=[
                    effort.get("reasoningEffort")
                    for effort in model.get("supportedReasoningEfforts") or []
                    if isinstance(effort, dict)
                ],
                default_reasoning_effort=model.get("defaultReasoningEffort") or "",
                is_default=bool(model.get("isDefault")),
            )
            for model in models
            if not model.get("hidden")
        ]

    async def list_sessions(self) -> list[Any]:
        return []  # The owner's own sessions listed are Copilot's (others.py).

    async def get_session_metadata(self, session_id: str) -> Any:
        try:
            answer = await self.request("thread/read", {"threadId": session_id})
        except CodexError:
            return None
        thread = answer.get("thread") or {}
        return SimpleNamespace(session_id=thread.get("id", session_id), cwd=thread.get("cwd"))

    async def create_session(self, *, session_id: str, name: str, **options: Any) -> CodexSession:
        params = await self.thread_params(options)
        params["dynamicTools"] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description or tool.name,
                "inputSchema": tool.parameters or {"type": "object", "properties": {}},
            }
            for tool in options.get("tools") or []
        ]
        answer = await self.request("thread/start", params)
        try:
            session = self.opened(answer, options)
        except RuntimeError:
            if thread := (answer.get("thread") or {}).get("id"):
                with suppress(Exception):
                    await self.archive(thread)  # Not one Chief of Staff can use.
            raise
        if name:
            with suppress(CodexError):
                await self.request(
                    "thread/name/set", {"threadId": session.session_id, "name": name}
                )
        return session

    async def resume_session(self, session_id: str, **options: Any) -> CodexSession:
        params = await self.thread_params(options)
        params.update(threadId=session_id, excludeTurns=True)
        answer = await self.request("thread/resume", params)
        return self.opened(answer, options)

    @property
    def ask_first_refused(self) -> str:
        """Why its workers cannot ask first ("" when they can): only a version whose every kind
        of action was shown to ask Chief of Staff first runs them (Runtime.agent_block)."""
        if ask_first_tested(self.cli_version):
            return ""
        return (
            f"Codex {self.cli_version} has not passed Chief of Staff's checks for Ask first "
            f"(tested from {OLDEST_CODEX}, before {ASK_FIRST_BEFORE}): let this worker act on its "
            "own, or use a tested version."
        )

    async def thread_params(self, options: dict) -> dict:
        if options.get("ask_first") and (why := self.ask_first_refused):
            raise RuntimeError(why)
        cwd = options.get("working_directory") or str(Path.home())
        prompt = (options.get("system_message") or {}).get("content") or ""
        return {
            "cwd": cwd,
            "sandbox": "danger-full-access",
            "approvalPolicy": APPROVAL_POLICY,
            "approvalsReviewer": REVIEWER,
            **({"model": options["model"]} if options.get("model") else {}),
            **({"developerInstructions": prompt} if prompt else {}),
            "config": await self.thread_config(options, cwd),
        }

    async def thread_config(self, options: dict, cwd: str) -> dict:
        """The thread's own settings, over the owner's: whether its folders' repositories are
        trusted (as the owner trusts them in Codex), and with Ask first, every MCP call asks."""
        owner = (await self.request("config/read", {"cwd": cwd})).get("config") or {}
        # Which of its MCP servers is GitHub's is told from their configuration (elicitation).
        options["mcp_configs"] = owner.get("mcp_servers") or {}
        pairs = options.get("trust") or [(cwd, cwd)]
        projects = await asyncio.to_thread(trust_levels, owner.get("projects") or {}, pairs)
        # Nested, this replaces the owner's table for the thread (only its folders matter there).
        config: dict[str, Any] = {"projects": projects}
        if options.get("ask_first"):
            for server, entry in (owner.get("mcp_servers") or {}).items():
                if "." in server or not isinstance(entry, dict):
                    continue  # Not addressable as a dotted key: left as configured.
                # Dotted keys merge into the owner's server; a nested one would replace it.
                config[f"mcp_servers.{server}.default_tools_approval_mode"] = "prompt"
                for tool in entry.get("tools") or {}:
                    if "." not in tool:
                        config[f"mcp_servers.{server}.tools.{tool}.approval_mode"] = "prompt"
        return config

    def opened(self, answer: dict, options: dict) -> CodexSession:
        thread = answer.get("thread") or {}
        sandbox = answer.get("sandbox") or {}
        actual = (
            answer.get("approvalPolicy"),
            answer.get("approvalsReviewer"),
            sandbox.get("type") if isinstance(sandbox, dict) else sandbox,
        )
        if actual != (APPROVAL_POLICY, REVIEWER, FULL_ACCESS) or not thread.get("id"):
            raise RuntimeError(
                "Codex did not start the thread with the settings Chief of Staff's checks need "
                f"(approval policy {actual[0]}, reviewer {actual[1]}, sandbox {actual[2]}): an "
                "administrator's Codex settings may override them."
            )
        session = CodexSession(self, thread["id"], options)
        session.current_model = answer.get("model") or ""
        if previous := self.sessions.get(session.session_id):
            session.carry(previous)
        self.sessions[session.session_id] = session
        session.pump = asyncio.ensure_future(session.handle_events())
        return session

    async def mcp_sign_in_url(self, session: Any, server: str) -> str:
        return ""  # Not routed: Codex keeps its own MCP sign-ins.

    def sandbox(self, policy: Any) -> None:
        return None

    async def gh_works_in_sandbox(self) -> bool | None:
        return None

    async def open_elsewhere(self) -> dict[str, int]:
        return {}

    async def read_history(self, session_id: str, offset: int) -> tuple[list[dict], int, float]:
        return [], offset, 0.0

    async def read_usage(self, session_id: str, offset: int) -> tuple[list[dict], int]:
        return [], offset

    def session_log(self, session_id: str) -> Path | None:
        return None

    async def archive(self, session_id: str):
        """Put a thread away in Codex (its history is kept there)."""
        await self.request("thread/archive", {"threadId": session_id})


def client_version() -> str:
    from importlib import metadata

    try:
        return metadata.version("chiefofstaff")
    except metadata.PackageNotFoundError:
        return "dev"


class CodexSession:
    """A Codex thread, as an AgentSession."""

    def __init__(self, backend: CodexBackend, thread_id: str, options: dict):
        self.backend = backend
        self.session_id = thread_id
        self.options = options
        self.tools = {tool.name: tool for tool in options.get("tools") or []}
        self.model = options.get("model") or ""
        self.effort = options.get("reasoning_effort") or ""
        self.current_model = ""
        self.handlers: list[Callable[[Any], None]] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.pump: asyncio.Task | None = None
        self.closed = False
        # The turn in progress (its id once Codex answers), the turns it has had here, and
        # whether one is being started now.
        self.turn: str | None = None
        self.turns: set[str] = set()
        self.ended: set[str] = set()
        self.starting = False
        # Messages for after the turn: their input items and the id they were sent with.
        self.queue: list[tuple[list[dict], str]] = []
        # Items started and not completed: id -> item (and child threads' items, by id).
        self.items: dict[str, dict] = {}
        # Commands its turns (and its child agents') left running (background terminals): item
        # id -> the thread that runs it.
        self.left_running: dict[str, str] = {}
        # Child threads at work: thread id -> the `task` call that shows its turn, and its turn;
        # and every child thread it had (what they run is ended with its own).
        self.children: dict[str, str] = {}
        self.child_turns: dict[str, str] = {}
        self.child_threads: set[str] = set()
        # Ids of what it was sent: steered into a turn, and the adapter's own notes (refusals).
        self.steered: set[str] = set()
        self.own: set[str] = set()
        # A turn Codex said started, shown once its message is (as Copilot shows a turn: the
        # message first, then its start).
        self.unshown: str = ""

    def carry(self, previous: CodexSession):
        """The thread was open on another handle (detached, then resumed): what runs on it is
        this one's now."""
        previous.closed = True
        if previous.pump:
            previous.pump.cancel()
        for name in (
            "turn",
            "turns",
            "ended",
            "items",
            "left_running",
            "children",
            "child_turns",
            "child_threads",
            "own",
        ):
            setattr(self, name, getattr(previous, name))

    # AgentSession

    def on(self, handler: Callable[[Any], None]) -> Callable[[], None]:
        self.handlers.append(handler)
        return lambda: self.handlers.remove(handler) if handler in self.handlers else None

    def emit(self, kind: str, /, **data: Any):
        if self.closed:
            return
        for handler in list(self.handlers):
            handler(event(kind, **data))

    async def send(
        self, prompt: str, *, mode: str = "enqueue", attachments: list[Any] | None = None
    ) -> str:
        if self.closed or not self.backend.alive():
            raise ConnectionResetError("This Codex session is disconnected.")
        items = [{"type": "text", "text": prompt}]
        for attachment in attachments or []:
            path = attachment.get("path") if isinstance(attachment, dict) else None
            if path:
                items.append({"type": "localImage", "path": path})
        sent = f"cos-{uuid4().hex[:12]}"
        if self.turn or self.starting:
            if mode == "immediate" and self.turn and await self.steer(items, sent):
                return sent
            if self.turn or self.starting:
                self.queue.append((items, sent))  # Taken up once its turn ends.
                return sent
        await self.start_turn(items, sent)
        return sent

    async def steer(self, items: list[dict], sent: str) -> bool:
        """Into the turn in progress; False when it has just ended."""
        params = {
            "threadId": self.session_id,
            "expectedTurnId": self.turn,
            "input": items,
            "clientUserMessageId": sent,
        }
        self.steered.add(sent)
        try:
            await self.backend.request("turn/steer", params)
        except CodexError:
            self.steered.discard(sent)
            return False
        return True

    async def start_turn(self, items: list[dict], sent: str):
        """A turn on these, once Chief of Staff admits it; refused, it raises why."""
        self.starting = True
        try:
            if self.backend.admit and (why := await self.backend.admit(self)):
                raise RuntimeError(why)
            params: dict[str, Any] = {
                "threadId": self.session_id,
                "input": items,
                "clientUserMessageId": sent,
            }
            if self.model:
                params["model"] = self.model
            if self.effort:
                params["effort"] = self.effort
            # Written now, nothing awaited since the check: a turn admitted before a change
            # that makes this worker ineligible never begins after it.
            if self.backend.begins and not self.backend.begins(self):
                raise RuntimeError("Chief of Staff's settings changed; send it again.")
            future = self.backend.send_request("turn/start", params)
            answer = await self.backend.answer("turn/start", future)
            turn = (answer.get("turn") or {}).get("id")
            if turn and not self.turn and turn not in self.ended:
                self.turn = turn  # Its events may come before this answer is read: not after.
                self.turns.add(turn)
        finally:
            self.starting = False

    async def abort(self) -> None:
        """Its turn ends, and its child agents' with it; what it left running goes on until
        stopped (stop_background). What waited for the turn's end is dropped."""
        self.queue.clear()
        for _ in range(500):
            if not self.starting:
                break
            await asyncio.sleep(0.01)  # A turn starting now is stopped once it has started.
        for child, turn in list(self.child_turns.items()):
            if turn:
                with suppress(Exception):
                    await self.backend.request(
                        "turn/interrupt", {"threadId": child, "turnId": turn}
                    )
        if self.turn:
            with suppress(Exception):
                await self.backend.request(
                    "turn/interrupt", {"threadId": self.session_id, "turnId": self.turn}
                )

    async def disconnect(self) -> None:
        """Its events stop; the thread stays resumable. While something of it runs (a turn, a
        command it left running), what that does is still followed, for the handle that
        resumes it."""
        self.closed = True
        self.handlers.clear()
        if self.turn or self.left_running or self.child_turns:
            return
        if self.backend.sessions.get(self.session_id) is self:
            del self.backend.sessions[self.session_id]
            with suppress(Exception):
                await self.backend.request("thread/unsubscribe", {"threadId": self.session_id})
        if self.pump and self.pump is not asyncio.current_task():
            self.pump.cancel()

    def lost(self):
        """Its app-server stopped: no turn of it runs any more."""
        self.closed = True

    async def set_model(
        self, model: str, *, reasoning_effort: str | None = None, context_tier: Any = None
    ) -> None:
        """For its next turn and those after it."""
        self.model = model if model and model != "auto" else ""
        self.effort = reasoning_effort or ""

    async def get_events(self) -> list[Any]:
        """Its conversation (the latest turns): what it received and what it said."""
        turns, cursor = [], None
        for _ in range(5):
            answer = await self.backend.request(
                "thread/turns/list",
                {
                    "threadId": self.session_id,
                    "itemsView": "full",
                    "sortDirection": "desc",
                    "limit": 20,
                    **({"cursor": cursor} if cursor else {}),
                },
            )
            turns += answer.get("data") or []
            if not (cursor := answer.get("nextCursor")):
                break
        found = []
        for turn in reversed(turns):
            for item in turn.get("items") or []:
                text = text_of(item.get("content"))
                if item.get("type") == "userMessage" and not text.startswith(REFUSED_NOTE):
                    found.append(event("user.message", content=text))
                elif item.get("type") == "agentMessage" and item.get("text"):
                    found.append(event("assistant.message", content=item["text"]))
        return found

    async def running_background(self) -> int:
        return len(self.left_running) + len(self.child_turns) * (self.turn is None)

    async def stop_background(self) -> int:
        """Ends the commands its turns left running, and child agents still at work after its
        turn ended. Codex keeps a turn's commands running when the turn is interrupted (0.167.2):
        what its thread runs is asked of Codex, not only what its turns' ends have shown here."""
        stopping = 0
        for thread in {self.session_id, *self.left_running.values(), *self.child_threads}:
            listed = await self.backend.request(
                "thread/backgroundTerminals/list", {"threadId": thread}
            )
            if found := listed.get("data"):
                stopping += len(found)
                await self.backend.request("thread/backgroundTerminals/clean", {"threadId": thread})
        if self.turn is None:
            for child, turn in list(self.child_turns.items()):
                if turn:
                    with suppress(Exception):
                        await self.backend.request(
                            "turn/interrupt", {"threadId": child, "turnId": turn}
                        )
                stopping += 1
        return stopping

    async def take_queued(self) -> int:
        taken, self.queue = len(self.queue), []
        return taken

    async def queue_holds(self) -> bool:
        return bool(self.queue)

    # Events, in the order Codex sent them

    async def handle_events(self):
        while True:
            method, params, thread = await self.inbox.get()
            try:
                await self.handle(method, params, thread)
            except Exception:
                logger.exception("Handling Codex's %s failed.", method)

    async def handle(self, method: str, params: dict, thread: str):
        child = thread != self.session_id
        if method == "turn/started":
            turn = (params.get("turn") or {}).get("id", "")
            if child:
                self.child_started(thread, turn)
                return
            self.turn = turn
            self.turns.add(turn)
            self.unshown = turn
            return
        if not child and self.unshown and self.shows_turn(method, params):
            self.show_turn()  # A turn on no message of Chief of Staff's: shown as it goes.
        if method == "turn/completed":
            turn = params.get("turn") or {}
            if child:
                await self.child_ended(thread, turn)
            else:
                await self.turn_ended(turn)
        elif method == "item/started":
            self.item_started(params.get("item") or {}, thread)
        elif method == "item/completed":
            await self.item_completed(params.get("item") or {}, thread)
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            self.emit(
                "agent.usage",
                thread_id=thread,
                turn_id=params.get("turnId", ""),
                total=usage.get("total") or {},
                last=usage.get("last") or {},
                model=self.current_model,
                child=child,
            )
        elif method == "model/rerouted" and not child:
            self.current_model = params.get("toModel") or params.get("model") or self.current_model

    def shows_turn(self, method: str, params: dict) -> bool:
        """Whether this is the turn's first step that is not its message (Codex says other
        things meanwhile: its status, MCP servers starting)."""
        if method == "turn/completed":
            return True
        item = mapping(params.get("item"))
        return method in {"item/started", "item/completed"} and item.get("type") != "userMessage"

    def show_turn(self):
        turn, self.unshown = self.unshown, ""
        self.emit("assistant.turn_start", turn_id=turn)

    def parent_call(self, thread: str) -> dict:
        """What a child thread's events carry: the `task` call its turn shows as."""
        if thread == self.session_id:
            return {}
        return {"parent_tool_call_id": self.children.get(thread) or f"child-{thread}"}

    def child_started(self, thread: str, turn: str):
        self.child_threads.add(thread)
        call = f"child-{turn}"
        self.children[thread] = call
        self.child_turns[thread] = turn
        self.emit(
            "tool.execution_start",
            tool_call_id=call,
            tool_name="task",
            arguments={"description": "A child agent"},
        )

    async def child_ended(self, thread: str, turn: dict):
        await self.settle_items(turn.get("status") == "interrupted", thread)
        call = self.children.pop(thread, None)
        self.child_turns.pop(thread, None)
        if call:
            done = turn.get("status") == "completed"
            self.emit(
                "tool.execution_complete",
                tool_call_id=call,
                success=done,
                result=SimpleNamespace(content="The child agent finished." if done else ""),
            )
        self.after_background()

    def after_background(self):
        """Something its turn left (a command, a child agent) ended: once nothing is left and no
        turn runs, its session is idle."""
        if self.turn is None and not self.child_turns and not self.left_running:
            self.emit("session.idle")

    def item_started(self, item: dict, thread: str):
        kind, item_id = item.get("type"), item.get("id", "")
        parent = self.parent_call(thread)
        if kind == "userMessage" and not parent:
            sent = item.get("clientId") or ""
            if sent in self.own:
                if self.unshown:
                    self.show_turn()
                return  # The adapter's own note: not a message Chief of Staff sent.
            delivery = SimpleNamespace(value="steering") if sent in self.steered else None
            self.steered.discard(sent)
            self.emit("user.message", content=text_of(item.get("content")), delivery=delivery)
            if self.unshown:
                self.show_turn()
            return
        if kind in QUIET_ITEMS or kind in {"agentMessage", "collabAgentToolCall"}:
            return
        if kind == "subAgentActivity":
            # Its child thread's turns show as `task` calls (child_started); started, the child
            # is at work from now on, even before its turn says so.
            child = item.get("agentThreadId") or ""
            if item.get("kind") == "started" and child:
                self.child_turns.setdefault(child, "")
            elif (
                item.get("kind") in {"completed", "interrupted"}
                and self.child_turns.get(child, "-") == ""
            ):  # Done without a turn of its own seen.
                del self.child_turns[child]
                self.after_background()
            return
        self.items[item_id] = {**item, "_thread": thread}
        if kind == "commandExecution":
            command = inner_command(item.get("command") or "")
            self.emit(
                "tool.execution_start",
                tool_call_id=item_id,
                tool_name="bash",
                arguments={"command": command},
                **parent,
            )
        elif kind == "fileChange":
            for index, change in enumerate(item.get("changes") or []):
                added = (change.get("kind") or {}).get("type") == "add"
                self.emit(
                    "tool.execution_start",
                    tool_call_id=f"{item_id}#{index}",
                    tool_name="create" if added else "edit",
                    arguments={"path": change.get("path", "")},
                    **parent,
                )
        elif kind == "mcpToolCall":
            self.emit(
                "tool.execution_start",
                tool_call_id=item_id,
                tool_name=f"{item.get('server', '')}-{item.get('tool', '')}",
                arguments=item.get("arguments") or {},
                **parent,
            )
        elif kind == "dynamicToolCall":
            self.emit(
                "tool.execution_start",
                tool_call_id=item_id,
                tool_name=item.get("tool", ""),
                arguments=item.get("arguments") or {},
                **parent,
            )
        elif kind == "webSearch":
            self.emit(
                "tool.execution_start",
                tool_call_id=item_id,
                tool_name="web_search",
                arguments={"query": item.get("query") or ""},
                **parent,
            )
        elif kind == "imageView":
            self.emit(
                "tool.execution_start",
                tool_call_id=item_id,
                tool_name="view",
                arguments={"path": item.get("path") or ""},
                **parent,
            )
        else:
            self.items.pop(item_id, None)

    async def item_completed(self, item: dict, thread: str):
        kind, item_id = item.get("type"), item.get("id", "")
        parent = self.parent_call(thread)
        if kind == "agentMessage":
            text = item.get("text") or ""
            if text.strip():
                # Commentary on the way to its next steps is narration, as with Copilot.
                narration = [SimpleNamespace()] if item.get("phase") == "commentary" else None
                self.emit("assistant.message", content=text, tool_requests=narration, **parent)
            return
        if item_id in self.left_running:
            # A command its turn left running ended: its receipt settles.
            del self.left_running[item_id]
            self.items.pop(item_id, None)
            code = item.get("exitCode")
            stopped = item.get("status") != "completed" and code in (None, -1)
            kind_ = SimpleNamespace(
                type="shell_completed", shell_id=item_id, exit_code=None if stopped else code
            )
            self.emit("system.notification", kind=kind_)
            self.after_background()
            return
        if self.items.pop(item_id, None) is None:
            return
        self.completed(item, parent)

    def completed(self, item: dict, parent: dict, stopped: bool = False):
        kind, item_id, status = item.get("type"), item.get("id", ""), item.get("status")
        declined = status == "declined"
        error = SimpleNamespace(message="Refused.") if declined else None
        if kind == "commandExecution":
            # -1: a terminal Codex ended (cleaned), not a command's own exit code.
            code = None if item.get("exitCode") == -1 else item.get("exitCode")
            self.emit(
                "tool.execution_complete",
                tool_call_id=item_id,
                success=status == "completed",
                result=SimpleNamespace(
                    content="" if declined else (item.get("aggregatedOutput") or "")
                ),
                shell_execution=None
                if code is None or stopped
                else SimpleNamespace(exit_code=code),
                error=error,
                **parent,
            )
        elif kind == "fileChange":
            for index, _ in enumerate(item.get("changes") or []):
                self.emit(
                    "tool.execution_complete",
                    tool_call_id=f"{item_id}#{index}",
                    success=status == "completed",
                    result=SimpleNamespace(content=""),
                    error=error,
                    **parent,
                )
        else:
            result = item.get("result") or {}
            if kind == "dynamicToolCall":
                parts = item.get("contentItems") or []
            else:
                parts = result.get("content") if isinstance(result, dict) else []
            text = "\n".join(
                str(part.get("text") or "") for part in parts or [] if isinstance(part, dict)
            )
            failure = (item.get("error") or {}).get("message") if item.get("error") else ""
            self.emit(
                "tool.execution_complete",
                tool_call_id=item_id,
                success=status == "completed" and item.get("success") is not False,
                result=SimpleNamespace(content=text or failure or ""),
                **parent,
            )

    async def turn_ended(self, turn: dict):
        status = turn.get("status")
        await self.settle_items(status == "interrupted")
        self.turn = None
        self.ended.add(turn.get("id", ""))
        if status == "failed":
            self.queue.clear()  # Given back: what it was sent is sent again (Delivery).
            error = turn.get("error") or {}
            self.emit("session.error", message=str(error.get("message") or "Codex failed."))
            return
        if status == "interrupted":
            self.queue.clear()
            self.emit("abort", reason="user initiated")
        elif self.queue and await self.next_turn():
            return
        if self.left_running or self.child_turns:
            self.emit("assistant.idle")  # What it left running holds session.idle.
        else:
            self.emit("session.idle")

    async def settle_items(self, interrupted: bool, thread: str = ""):
        """A turn of this thread (or of a child's, `thread`) ended with these still started:
        commands it left running hold its end; whatever was cut short ended without a result."""
        thread = thread or self.session_id
        mine = {
            item_id: item for item_id, item in self.items.items() if item.get("_thread") == thread
        }
        commands = [item for item in mine.values() if item.get("type") == "commandExecution"]
        running = set()
        if commands and interrupted:
            with suppress(Exception):
                answer = await self.backend.request(
                    "thread/backgroundTerminals/list", {"threadId": thread}
                )
                running = {entry.get("itemId") for entry in answer.get("data") or []}
        elif commands:
            running = {item.get("id") for item in commands}  # A finished turn waits for none.
        for item_id, item in mine.items():
            if item.get("type") == "commandExecution" and item_id in running:
                if item_id in self.left_running:
                    continue
                self.left_running[item_id] = thread
                # Shaped as Copilot says it, so receipts know the command runs on.
                self.emit(
                    "tool.execution_complete",
                    tool_call_id=item_id,
                    success=True,
                    result=SimpleNamespace(
                        content=f"<command started in background with shellId: {item_id}>"
                    ),
                    **self.parent_call(thread),
                )
                continue
            del self.items[item_id]
            self.completed({**item, "status": "failed"}, self.parent_call(thread), stopped=True)

    async def next_turn(self) -> bool:
        """The next message it queued, as a turn of its own, once admitted; refused, what it
        queued is given back (Chief of Staff sends it again)."""
        items, sent = self.queue.pop(0)
        try:
            await self.start_turn(items, sent)
        except Exception as exc:
            logger.info("%s's next turn did not start: %s", self.session_id, exc)
            self.queue.clear()
            return False
        return True

    # Codex's requests

    async def request(self, method: str, params: dict, thread: str) -> dict:
        context = {"session_id": self.session_id, "sessionId": self.session_id}
        if method == "item/commandExecution/requestApproval":
            return await self.command_approval(params, context)
        if method == "item/fileChange/requestApproval":
            return await self.change_approval(params, context)
        if method == "mcpServer/elicitation/request":
            return await self.elicitation(params, context)
        if method == "item/tool/call":
            return await self.tool_call(params)
        if method == "item/tool/requestUserInput":
            return await self.user_input(params, context)
        if method == "item/permissions/requestApproval":
            # More access than full access is not a thing; nothing more is granted.
            return {"permissions": {}}
        raise CodexError(f"{method} is not supported by Chief of Staff.")

    async def permitted(self, request: SimpleNamespace, context: dict) -> str:
        """ "" when Chief of Staff allows it, else why not (and the turn hears why)."""
        check = self.options.get("on_permission_request")
        if not check:
            return "No permission check is set for this session."
        decision = check(request, context)
        if inspect.isawaitable(decision):
            decision = await decision
        if isinstance(decision, PermissionDecisionReject):
            why = getattr(decision, "feedback", "") or "Refused."
            await self.tell(why)
            return why
        return ""

    async def tell(self, why: str):
        """The model sees only "rejected": the reason is steered into its turn."""
        if not self.turn:
            return
        sent = f"cos-note-{uuid4().hex[:12]}"
        self.own.add(sent)
        note = wrap("system", "refused", f"Chief of Staff refused that: {why}")
        with suppress(Exception):
            await self.backend.request(
                "turn/steer",
                {
                    "threadId": self.session_id,
                    "expectedTurnId": self.turn,
                    "input": [{"type": "text", "text": note}],
                    "clientUserMessageId": sent,
                },
            )

    async def command_approval(self, params: dict, context: dict) -> dict:
        text = inner_command(params.get("command") or "")
        network = params.get("networkApprovalContext") or {}
        if network.get("host"):
            request = SimpleNamespace(
                kind="url",
                url=f"{network.get('protocol', 'https')}://{network['host']}",
                intention=params.get("reason") or "",
                tool_call_id=params.get("itemId"),
            )
        else:
            request = SimpleNamespace(
                kind="shell",
                full_command_text=text,
                intention=params.get("reason") or "",
                commands=[],
                possible_paths=[],
                possible_urls=[],
                has_write_file_redirection=False,
                can_offer_session_approval=False,
                tool_call_id=params.get("itemId"),
            )
        refused = await self.permitted(request, context)
        return {"decision": "decline" if refused else "accept"}

    async def change_approval(self, params: dict, context: dict) -> dict:
        changes = self.backend.changes.get(params.get("itemId", ""))
        if not changes:
            return {"decision": "decline"}  # Which files it writes is not known: not written.
        for change in changes:
            path = str(change.get("path") or "")
            request = SimpleNamespace(
                kind="write",
                file_name=path,
                resolved_path=path,
                diff=change.get("diff") or "",
                intention=params.get("reason") or "",
                can_offer_session_approval=False,
                tool_call_id=params.get("itemId"),
            )
            if await self.permitted(request, context):
                return {"decision": "decline"}
        return {"decision": "accept"}

    async def elicitation(self, params: dict, context: dict) -> dict:
        meta = mapping(params.get("_meta"))
        server = params.get("serverName") or ""
        if meta.get("codex_approval_kind") == "mcp_tool_call":
            annotations = meta.get("tool_annotations") or {}
            request = SimpleNamespace(
                kind="mcp",
                server_name=GITHUB_MCP if self.github(server) else server,
                tool_name=meta.get("tool_name") or "",
                tool_title=meta.get("tool_name") or "",
                read_only=bool(annotations.get("readOnlyHint")),
                args=meta.get("tool_params"),
            )
            refused = await self.permitted(request, context)
            return {"action": "decline" if refused else "accept"}
        ask = self.options.get("on_elicitation_request")
        if not ask or params.get("mode") not in {"form", "url"}:
            return {"action": "decline"}  # A kind of form the owner's cards do not show.
        answer = ask(
            {
                **context,
                "elicitationSource": server,
                "message": params.get("message") or "",
                "mode": params.get("mode"),
                "url": params.get("url") or "",
                "requestedSchema": params.get("requestedSchema") or {},
            }
        )
        if inspect.isawaitable(answer):
            answer = await answer
        answer = answer if isinstance(answer, dict) else {}
        result = {"action": answer.get("action") or "cancel"}
        if answer.get("content") is not None:
            result["content"] = answer["content"]
        return result

    def github(self, server: str) -> bool:
        return github_server(server, (self.options.get("mcp_configs") or {}).get(server))

    async def tool_call(self, params: dict) -> dict:
        name = params.get("tool") or ""
        tool = self.tools.get(name)
        if not tool or not tool.handler:
            text, ok = f"Tool {name} is not available.", False
        else:
            invocation = ToolInvocation(
                session_id=self.session_id,
                tool_call_id=params.get("callId") or "",
                tool_name=name,
                arguments=params.get("arguments"),
            )
            try:
                result = tool.handler(invocation)
                if inspect.isawaitable(result):
                    result = await result
                text = getattr(result, "text_result_for_llm", None) or str(result)
                ok = getattr(result, "result_type", "success") == "success"
            except Exception as exc:
                logger.warning("Tool %s failed.", name, exc_info=True)
                text, ok = f"The tool failed: {exc}", False
        return {"contentItems": [{"type": "inputText", "text": text}], "success": ok}

    async def user_input(self, params: dict, context: dict) -> dict:
        ask = self.options.get("on_user_input_request")
        answers = {}
        for question in params.get("questions") or []:
            if not ask or question.get("isSecret"):
                continue  # A secret is never asked for through Chief of Staff.
            text = " ".join(
                part for part in (question.get("header"), question.get("question")) if part
            )
            choices = [
                str(option.get("label") or "")
                for option in question.get("options") or []
                if isinstance(option, dict) and option.get("label")
            ]
            answer = ask({"question": text, "choices": choices, "allowFreeform": True}, context)
            if inspect.isawaitable(answer):
                answer = await answer
            reply = (answer or {}).get("answer") if isinstance(answer, dict) else ""
            if reply:
                answers[question.get("id", "")] = {"answers": [str(reply)]}
        return {"answers": answers}


def codex_backend(command: str = "codex") -> CodexBackend:
    return CodexBackend(shutil.which(command) or command)
