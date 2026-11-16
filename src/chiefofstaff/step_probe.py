"""The step profile's probe (shepherd.py): run once for each Copilot CLI version, before any
shepherd step runs on it, on this computer, with no model call (each action is run through the
session's own tool pipeline, `session.rpc.tools.execute`, as a model's call would be) and with
every permission request approved (the operating system, not the permission check, must hold).

A throwaway session is created and then resumed with the step profile. Its tools, as Copilot
lists them, must be the profile's only (no MCP server's, no cross-session tool), and every one
of these must fail: a `git push` over HTTPS and over SSH, `gh api -X POST`, `curl -X POST` to
GitHub's API, a raw socket to github.com on ports 443 and 22, reading gh's hosts.yml, calling a
tool of a configured MCP server, and calling `tell_session`. An install from an allowed host must
succeed. Only a version whose probe passed gets steps; the result is kept per version
(kv `stepprobe:<version>`)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .backend import StepSandboxPolicy
from .store import now

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

# What a forbidden action prints only when it got through.
REACHED = "STEP-PROBE-REACHED"
FIXTURE = "octocat/Hello-World"
ALLOWED_HOST = "pypi.org"
PYTHON = shlex.quote(sys.executable)


def actions(folder: Path) -> list[tuple[str, str]]:
    """(what, command): each must not print REACHED. Their output is never trusted otherwise."""
    gh_hosts = Path.home() / ".config" / "gh" / "hosts.yml"
    socket = (
        f"{PYTHON} -c \"import socket; socket.create_connection(('github.com', {{port}}), 5); "
        f"print('{REACHED}')\""
    )
    return [
        (
            "git push over HTTPS",
            f"cd {shlex.quote(str(folder))} && git push --dry-run "
            f"https://github.com/{FIXTURE}.git HEAD:refs/heads/step-probe && echo {REACHED}",
        ),
        (
            "git push over SSH",
            f"cd {shlex.quote(str(folder))} && GIT_SSH_COMMAND='ssh -o "
            f"BatchMode=yes -o ConnectTimeout=5' git push --dry-run git@github.com:{FIXTURE}.git "
            f"HEAD:refs/heads/step-probe && echo {REACHED}",
        ),
        # Endpoints that change nothing (they render markdown), reached only with a route.
        ("gh api -X POST", f"gh api -X POST markdown -f text=probe && echo {REACHED}"),
        (
            "curl -X POST to api.github.com",
            "curl -sSf --max-time 10 -X POST -o /dev/null -d "
            f'\'{{"text":"probe"}}\' https://api.github.com/markdown && echo {REACHED}',
        ),
        ("a socket to github.com:443", socket.format(port=443)),
        ("a socket to github.com:22", socket.format(port=22)),
        (
            "reading gh's hosts.yml",
            f"test -e {shlex.quote(str(gh_hosts))} || exit 1; cat "
            f"{shlex.quote(str(gh_hosts))} > /dev/null && echo {REACHED}",
        ),
    ]


ALLOWED = ("an install from an allowed host", f"curl -sS --max-time 20 -o /dev/null "
           f"https://{ALLOWED_HOST}/simple/pip/ && echo {REACHED}")  # fmt: skip


def field(result: Any, *names: str) -> Any:
    """A result's field, whether Copilot's answer came as a mapping (as it does) or an object."""
    for name in names:
        if isinstance(result, dict) and name in result:
            return result[name]
        if not isinstance(result, dict) and hasattr(result, name):
            return getattr(result, name)
    return None


async def run_tool(session: Any, name: str, arguments: dict) -> tuple[bool, str, bool]:
    """(it ran, its output, the sandbox was applied to it) for one tool call through the
    session's own pipeline."""
    from copilot.generated.rpc import ToolsExecuteRequest

    try:
        result = await session.rpc.tools.execute(
            ToolsExecuteRequest(
                name=name, arguments=arguments, tool_call_id=f"probe-{uuid4().hex[:8]}"
            ),
            timeout=60,
        )
    except Exception as exc:
        return False, str(exc)[:500], False
    text = str(field(result, "textResultForLlm", "text_result_for_llm") or "")
    kind = field(result, "resultType", "result_type")
    kind = str(getattr(kind, "value", kind) or "")
    telemetry = field(result, "toolTelemetry", "tool_telemetry") or {}
    properties = (telemetry.get("properties") if isinstance(telemetry, dict) else None) or {}
    sandboxed = str(properties.get("sandboxApplied", "")).lower() == "true"
    return kind not in {"failure", "rejected", "denied", ""}, text, sandboxed


async def extra_tools(session: Any) -> list[str]:
    """The session's tools beyond the step's allowlist (an MCP server's, a cross-session one),
    as Copilot lists them once built: none, or what failed. Unlisted fails too (fail-closed)."""
    from .shepherd import STEP_TOOLS

    try:
        with contextlib.suppress(Exception):
            await session.rpc.tools.initialize_and_validate(timeout=60)
        listed = await session.rpc.tools.get_current_metadata(timeout=60)
    except Exception as exc:
        return [f"listing its tools ({str(exc)[:200]})"]
    if listed.tools is None:
        return ["listing its tools (none were built)"]
    extra = sorted({tool.name for tool in listed.tools} - set(STEP_TOOLS))
    return [f"the tools {', '.join(extra[:5])}"] if extra else []


def fixture(folder: Path):
    """A repository with one commit, for the pushes (nothing in it is ever sent)."""
    folder.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for command in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-c", "user.email=probe@example.com", "-c", "user.name=probe", "commit", "-q",
         "--allow-empty", "-m", "probe"],
    ):  # fmt: skip
        subprocess.run(command, cwd=folder, env=env, check=True, capture_output=True)  # noqa: S603


async def probe(runtime: Runtime, mcp_tool: str = "") -> dict:
    """Run the probe against this runtime's Copilot: {passed, why, failed: [...], version, at}.
    `mcp_tool`: a configured MCP server's tool to try (the first one Copilot offers otherwise)."""
    from copilot.generated.rpc import PermissionDecisionApproveOnce

    from .policy import GITHUB_MCP
    from .shepherd import BLOCKED_HOSTS, STEP_TOOLS, step_denied

    backend = runtime.connected()
    version = await backend.version()
    base = Path(tempfile.mkdtemp(prefix="cos-step-probe-"))
    folder = base / "repo"
    await asyncio.to_thread(fixture, folder)
    # The steps' own policy (shepherd.step_policy), with the allowed host to install from.
    policy = StepSandboxPolicy(
        readwrite=(str(base),), credentials=False, outbound=True,
        allowed_hosts=(ALLOWED_HOST, "files.pythonhosted.org"), blocked_hosts=BLOCKED_HOSTS,
        denied=step_denied(), keychain=False,
    )  # fmt: skip

    async def approve(request: Any, invocation: dict):
        return PermissionDecisionApproveOnce()

    # The step profile's options (shepherd.step_options), for a session of its own.
    configured = sorted(set(runtime.shepherd.copilot_mcp_servers())
                        | set(runtime.store.settings().mcp_servers))  # fmt: skip
    options = {
        "on_permission_request": approve,
        "working_directory": str(folder),
        "client_name": "chiefofstaff",
        "sandbox_config": backend.sandbox(policy),
        "enable_config_discovery": False,
        "enable_session_store": False,
        "available_tools": list(STEP_TOOLS),
        "tools": runtime.toolset.step_tools(),
        "disabled_mcp_servers": [GITHUB_MCP, *configured, "slack"],
    }
    session_id = str(uuid4())
    failed: list[str] = []
    session = None
    try:
        name = "Chief of Staff · step check"
        created = await backend.create_session(session_id=session_id, name=name, **options)
        with contextlib.suppress(Exception):
            await created.disconnect()
        session = await backend.resume_session(session_id, **options)
        failed += await extra_tools(session)
        for what, line in actions(folder):
            ran, text, sandboxed = await run_tool(
                session, "bash", {"command": line, "description": what}
            )
            if ran and (REACHED in text or not sandboxed):
                failed.append(what if REACHED in text else f"{what} (unsandboxed)")
        ran, _, _ = await run_tool(session, "tell_session", {"session": "x", "text": "probe"})
        if ran:
            failed.append("calling tell_session")
        if mcp_tool:
            ran, _, _ = await run_tool(session, mcp_tool, {})
            if ran:
                failed.append(f"calling the MCP tool {mcp_tool}")
        what, line = ALLOWED
        ran, text, _ = await run_tool(session, "bash", {"command": line, "description": what})
        allowed = ran and REACHED in text
    finally:
        if session is not None:
            with contextlib.suppress(Exception):
                await session.disconnect()
        with contextlib.suppress(Exception):
            client = getattr(backend, "client", None)
            if client is not None:
                await client.delete_session(session_id)
        await asyncio.to_thread(_remove, base)
    why = ""
    if failed:
        why = f"{', '.join(failed)} got through"
    elif not allowed:
        why = f"an install from {ALLOWED_HOST} did not work"
    return {"passed": not why, "why": why, "failed": failed, "version": version, "at": now(),
            "checked": time.time()}  # fmt: skip


def _remove(path: Path):
    import shutil

    shutil.rmtree(path, ignore_errors=True)
