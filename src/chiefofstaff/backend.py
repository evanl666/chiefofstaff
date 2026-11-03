"""The agent backend that runs Chief of Staff's sessions.

Chief of Staff uses a small part of what an agent SDK offers. This module names that part, so the
rest of the code depends on these Protocols rather than on the Copilot SDK. GitHub Copilot is the
backend that ships; the scripted backend (`scripted.py`) runs the demo mode, and
tests/test_backend_contract.py checks that both behave the way the runtime relies on.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from copilot import CopilotClient, RuntimeConnection
from copilot.generated.rpc import (
    MCPOauthLoginRequest,
    SandboxConfig,
    SandboxConfigAuth,
    SandboxConfigUserPolicy,
    SandboxConfigUserPolicyFilesystem,
    SandboxConfigUserPolicyNetwork,
)


@dataclass(frozen=True)
class Capabilities:
    name: str
    # send(mode="immediate") reaches a turn in progress.
    steering: bool
    # A session can be resumed after a restart, with its conversation.
    resume: bool
    # Shell commands, file writes and MCP calls ask on_permission_request first.
    permission_callbacks: bool
    # MCP servers that need OAuth ask on_mcp_auth_request; sign-ins are kept by the backend.
    mcp_sign_in: bool
    # Shell commands can run in an OS sandbox (sandbox_config).
    sandbox: bool


SendMode = Literal["enqueue", "immediate"]


class AgentSession(Protocol):
    session_id: str

    async def send(self, prompt: str, *, mode: SendMode = "enqueue") -> Any:
        """Queue a message and return at once; the turn's progress arrives as events."""
        ...

    async def abort(self) -> None:
        """Stop the turn in progress."""
        ...

    async def disconnect(self) -> None:
        """Stop receiving events; the session stays resumable."""
        ...

    async def set_model(
        self, model: str, *, reasoning_effort: str | None = None, context_tier: Any = None
    ) -> None: ...

    async def get_events(self) -> list[Any]:
        """The session's history; its user.message events show what it received."""
        ...

    def on(self, handler: Callable[[Any], None]) -> Any:
        """Receive every event: `event.type.value` names it, `event.data` holds its fields."""
        ...


class AgentBackend(Protocol):
    capabilities: Capabilities

    async def start(self) -> None: ...

    async def stop(self) -> Any: ...

    async def version(self) -> str: ...

    async def get_auth_status(self) -> Any:
        """Has `isAuthenticated`."""
        ...

    async def list_models(self) -> list[Any]: ...

    async def list_sessions(self) -> list[Any]: ...

    async def get_session_metadata(self, session_id: str) -> Any: ...

    async def create_session(self, *, session_id: str, name: str, **options: Any) -> AgentSession:
        """Options: system_message, tools, available_tools, working_directory, model and the
        callbacks on_permission_request, on_user_input_request, on_elicitation_request and
        on_mcp_auth_request (see Runtime.options)."""
        ...

    async def resume_session(self, session_id: str, **options: Any) -> AgentSession: ...

    async def mcp_sign_in_url(self, session: AgentSession, server: str) -> str:
        """Where the owner signs in to an MCP server; "" when the backend has credentials."""
        ...

    def sandbox(self, policy: SandboxPolicy) -> Any:
        """The session option (sandbox_config) that applies this policy."""
        ...

    async def gh_works_in_sandbox(self) -> bool | None:
        """Whether gh can reach GitHub from sandboxed commands; None when not applicable."""
        ...

    async def open_elsewhere(self) -> dict[str, int]:
        """Sessions another program has open right now (a terminal): session id -> its pid."""
        ...

    async def read_history(self, session_id: str, offset: int) -> tuple[list[dict], int, float]:
        """A session's events after `offset` (its latest ones when offset is 0) without
        attaching to it, the offset to read from next time, and when it last changed."""
        ...


@dataclass(frozen=True)
class SandboxPolicy:
    """What a sandboxed session's shell commands may touch."""

    readwrite: tuple[str, ...]
    readonly: tuple[str, ...] = ()
    # Empty: any host. Otherwise only these hosts are reachable.
    allowed_hosts: tuple[str, ...] = ()


COPILOT = Capabilities(
    name="GitHub Copilot",
    steering=True,
    resume=True,
    permission_callbacks=True,
    mcp_sign_in=True,
    sandbox=True,
)
# The Copilot CLI versions this release was tested with (live end-to-end and contract tests).
TESTED_CLI = "1.0.105"
OLDEST_CLI = "1.0.100"


class CopilotBackend:
    """GitHub Copilot through its Python SDK: one CLI process runs every session."""

    capabilities = COPILOT

    def __init__(self, client: CopilotClient, command: str = "copilot"):
        self.client = client
        self.command = command

    async def start(self) -> None:
        await self.client.start()

    async def stop(self) -> Any:
        return await self.client.stop()

    async def version(self) -> str:
        return (await self.client.get_status()).version

    async def get_auth_status(self) -> Any:
        return await self.client.get_auth_status()

    async def list_models(self) -> list[Any]:
        return await self.client.list_models()

    async def list_sessions(self) -> list[Any]:
        return await self.client.list_sessions()

    async def get_session_metadata(self, session_id: str) -> Any:
        return await self.client.get_session_metadata(session_id)

    async def create_session(self, *, session_id: str, name: str, **options: Any) -> AgentSession:
        return await self.client.create_session(session_id=session_id, name=name, **options)

    async def resume_session(self, session_id: str, **options: Any) -> AgentSession:
        return await self.client.resume_session(session_id, **options)

    async def mcp_sign_in_url(self, session: Any, server: str) -> str:
        login = await session.rpc.mcp.oauth.login(MCPOauthLoginRequest(server_name=server))
        return getattr(login, "authorization_url", None) or ""

    async def gh_works_in_sandbox(self) -> bool | None:
        return await gh_trusts_sandbox(self.command)

    async def open_elsewhere(self) -> dict[str, int]:
        return await asyncio.to_thread(sessions_in_use, copilot_home())

    async def read_history(self, session_id: str, offset: int) -> tuple[list[dict], int, float]:
        if not SESSION_ID.fullmatch(session_id):
            raise ValueError("Not a Copilot session id.")
        log = copilot_home() / "session-state" / session_id / "events.jsonl"
        return await asyncio.to_thread(read_log, log, offset)

    def sandbox(self, policy: SandboxPolicy) -> SandboxConfig:
        return SandboxConfig(
            enabled=True,
            allow_bypass=False,
            allow_dev_tool_access=True,
            # MCP servers are what the owner or their company configured, not what an agent
            # wrote: they run as they do in Copilot, with their own sign-ins and network.
            sandbox_mcp_servers=False,
            # git and gh get the owner's GitHub credentials through Copilot's local proxy;
            # commands only ever see stand-in values.
            auth=SandboxConfigAuth(gh=True, git=True),
            user_policy=SandboxConfigUserPolicy(
                filesystem=SandboxConfigUserPolicyFilesystem(
                    readwrite_paths=list(policy.readwrite), readonly_paths=list(policy.readonly)
                ),
                network=SandboxConfigUserPolicyNetwork(
                    allow_outbound=True,
                    allow_local_network=False,
                    allowed_hosts=list(policy.allowed_hosts) or None,
                ),
            ),
        )


def copilot_backend(command: str) -> CopilotBackend:
    path = shutil.which(command) if command else None
    if not path:
        raise RuntimeError(
            f"Copilot CLI '{command}' not found. Install it and sign in, or fix the command in "
            "Settings."
        )
    # The CLI and every worker shell inherit this environment; keep our own settings out.
    env = {key: value for key, value in os.environ.items() if not key.startswith("CHIEFOFSTAFF_")}
    client = CopilotClient(connection=RuntimeConnection.for_stdio(path=path), env=env)
    return CopilotBackend(client, path)


def copilot_home() -> Path:
    """Where the Copilot CLI keeps its state."""
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")


def own_processes() -> set[int]:
    """This process and its descendants (the Copilot CLI it runs)."""
    try:
        table = subprocess.run(
            ["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {os.getpid()}
    children: dict[int, list[int]] = {}
    for line in table.splitlines():
        pid, _, ppid = line.strip().partition(" ")
        if pid.isdigit() and ppid.strip().isdigit():
            children.setdefault(int(ppid.strip()), []).append(int(pid))
    found, todo = set(), [os.getpid()]
    while todo:
        pid = todo.pop()
        if pid not in found:
            found.add(pid)
            todo += children.get(pid, [])
    return found


def running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Someone else's process, but it exists.
    return True


def sessions_in_use(home: Path) -> dict[str, int]:
    """Sessions that another running program has open.

    The Copilot CLI keeps an ``inuse.<pid>.lock`` file in the folder of each session it has open;
    a lock whose process is gone is stale. Sessions this process's own CLI has open do not count.
    """
    locks = []
    for lock in (home / "session-state").glob("*/inuse.*.lock"):
        pid = lock.name.split(".")[1]
        if pid.isdigit() and running(int(pid)):
            locks.append((lock.parent.name, int(pid)))
    if not locks:
        return {}
    own = own_processes()
    return {session_id: pid for session_id, pid in locks if pid not in own}


SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The latest part of a session's log that is read when nothing was read before.
LOG_TAIL = 12_000_000
LOG_CHUNK = 8_000_000


def read_log(log: Path, offset: int) -> tuple[list[dict], int, float]:
    """Events appended to a Copilot session log (JSON lines) since `offset`.

    The CLI appends one event per line while it runs, so reading is safe while a terminal has
    the session open; a line still being written is left for the next read.
    """
    try:
        stat = log.stat()
    except OSError:
        return [], offset, 0.0
    if offset > stat.st_size:
        offset = 0  # The log was rewritten.
    start = offset or max(0, stat.st_size - LOG_TAIL)
    with log.open("rb") as file:
        file.seek(start)
        data = file.read(min(stat.st_size - start, LOG_CHUNK))
    end = data.rfind(b"\n") + 1
    lines = data[:end].split(b"\n")
    if start and not offset:
        lines = lines[1:]  # Started mid-line.
    events = []
    for line in lines:
        with suppress(ValueError):
            if line.strip() and isinstance(event := json.loads(line), dict):
                events.append(event)
    return events, start + end, stat.st_mtime


def sandbox_supported() -> bool:
    """Whether this machine can run Copilot's command sandbox."""
    if sys.platform == "darwin":
        return bool(shutil.which("sandbox-exec"))
    return sys.platform.startswith("linux") and bool(shutil.which("bwrap"))


async def gh_trusts_sandbox(command: str = "copilot") -> bool | None:
    """Whether gh can reach GitHub from inside the sandbox (macOS needs Copilot's CA trusted)."""
    if sys.platform != "darwin" or not shutil.which(command):
        return None
    process = await asyncio.create_subprocess_exec(
        command,
        "sandbox",
        "ca",
        "status",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(process.wait(), 15) == 0
    except TimeoutError:
        process.kill()
        return None


def older(version: str, than: str) -> bool:
    def parts(text: str) -> tuple[int, ...]:
        return tuple(int(part) for part in text.split("-")[0].split(".") if part.isdigit())

    try:
        mine = parts(version)
        return bool(mine) and mine < parts(than)
    except ValueError:
        return False
