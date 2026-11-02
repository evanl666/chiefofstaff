"""The agent backend that runs Chief of Staff's sessions.

Chief of Staff uses a small part of what an agent SDK offers. This module names that part, so the
rest of the code depends on these Protocols rather than on the Copilot SDK. GitHub Copilot is the
backend that ships; the scripted backend (`scripted.py`) runs the demo mode, and
tests/test_backend_contract.py checks that both behave the way the runtime relies on.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

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


class AgentSession(Protocol):
    session_id: str

    async def send(self, prompt: str, mode: str = "enqueue") -> Any:
        """Queue a message and return at once; the turn's progress arrives as events."""

    async def abort(self) -> None:
        """Stop the turn in progress."""

    async def disconnect(self) -> None:
        """Stop receiving events; the session stays resumable."""

    async def set_model(self, model: str, **tuning: Any) -> None: ...

    async def get_events(self) -> list[Any]:
        """The session's history; its user.message events show what it received."""

    def on(self, handler: Callable[[Any], None]) -> Any:
        """Receive every event: `event.type.value` names it, `event.data` holds its fields."""


class AgentBackend(Protocol):
    capabilities: Capabilities

    async def start(self) -> None: ...

    async def stop(self) -> Any: ...

    async def version(self) -> str: ...

    async def get_auth_status(self) -> Any:
        """Has `isAuthenticated`."""

    async def list_models(self) -> list[Any]: ...

    async def list_sessions(self) -> list[Any]: ...

    async def get_session_metadata(self, session_id: str) -> Any: ...

    async def create_session(self, *, session_id: str, name: str, **options: Any) -> AgentSession:
        """Options: system_message, tools, available_tools, working_directory, model and the
        callbacks on_permission_request, on_user_input_request, on_elicitation_request and
        on_mcp_auth_request (see Runtime.options)."""

    async def resume_session(self, session_id: str, **options: Any) -> AgentSession: ...

    async def mcp_sign_in_url(self, session: AgentSession, server: str) -> str:
        """Where the owner signs in to an MCP server; "" when the backend has credentials."""

    def sandbox(self, policy: SandboxPolicy) -> Any:
        """The session option (sandbox_config) that applies this policy."""

    async def gh_works_in_sandbox(self) -> bool | None:
        """Whether gh can reach GitHub from sandboxed commands; None when not applicable."""


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

    def sandbox(self, policy: SandboxPolicy) -> SandboxConfig:
        return SandboxConfig(
            enabled=True,
            allow_bypass=False,
            allow_dev_tool_access=True,
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
        return parts(version) < parts(than)
    except ValueError:
        return False
