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

# What an action prints: RAN as it starts (else it proves nothing), REACHED only when it got
# through. Neither is ever in a command's text (the shell prints a command line that fails to
# run: it would be read as reached): commands print them with printf's %s.
REACHED = "STEP-PROBE-REACHED"
RAN = "STEP-PROBE-RAN"
FIXTURE = "octocat/Hello-World"
ALLOWED_HOST = "pypi.org"


def say(marker: str) -> str:
    """A command that prints a marker, without the marker in its text."""
    head, tail = marker.rsplit("-", 1)
    return f"printf '{head}-%s\\n' {tail}"


def actions(folder: Path, github_ip: str = "") -> list[tuple[str, str]]:
    """(what, command): each must print RAN and must not print REACHED. Sockets are opened by
    the shell itself (bash's /dev/tcp): no interpreter that might not start in the sandbox."""
    gh_hosts = Path.home() / ".config" / "gh" / "hosts.yml"
    where = shlex.quote(str(folder))

    def socket(host: str, port: int) -> str:
        return f"( exec 3<>/dev/tcp/{host}/{port} ) && {say(REACHED)}"

    found = [
        (
            "git push over HTTPS",
            f"cd {where} && git push --dry-run https://github.com/{FIXTURE}.git "
            f"HEAD:refs/heads/step-probe && {say(REACHED)}",
        ),
        (
            "git push over SSH",
            f"cd {where} && GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=5' git push "
            f"--dry-run git@github.com:{FIXTURE}.git HEAD:refs/heads/step-probe && {say(REACHED)}",
        ),
        # Endpoints that change nothing (they render markdown), reached only with a route.
        ("gh api -X POST", f"gh api -X POST markdown -f text=probe && {say(REACHED)}"),
        (
            "curl -X POST to api.github.com",
            "curl -sSf --max-time 10 -X POST -o /dev/null -d "
            f'\'{{"text":"probe"}}\' https://api.github.com/markdown && {say(REACHED)}',
        ),
        ("a socket to github.com:443", socket("github.com", 443)),
        ("a socket to github.com:22", socket("github.com", 22)),
        (
            "reading gh's hosts.yml",
            f"test -e {shlex.quote(str(gh_hosts))} || exit 1; cat "
            f"{shlex.quote(str(gh_hosts))} > /dev/null && {say(REACHED)}",
        ),
    ]
    if github_ip:
        found.append(
            (f"a socket to github.com's address ({github_ip}):443", socket(github_ip, 443))
        )
    return [(what, f"{say(RAN)}; {line}") for what, line in found]


def github_address() -> str:
    """github.com's address, looked up here (outside the sandbox): a socket to it by number."""
    import socket

    try:
        return socket.getaddrinfo("github.com", 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except OSError:
        return ""


def allowed(folder: Path) -> list[tuple[str, str]]:
    """(what, command): what a step needs, each of which must print REACHED: an install from
    its project's registry, and a commit in its worktree."""
    where = shlex.quote(str(folder))
    return [
        ("an install from pypi.org", f"{say(RAN)}; curl -sS --max-time 20 -o /dev/null "
         f"https://{ALLOWED_HOST}/simple/pip/ && {say(REACHED)}"),
        ("a commit in its worktree", f"{say(RAN)}; cd {where} && git -c user.name=probe -c "
         f"user.email=probe@example.com commit -q --allow-empty -m probe && {say(REACHED)}"),
    ]  # fmt: skip


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
    from .shepherd import BLOCKED_HOSTS, STEP_TOOLS, git_config_files, step_denied

    backend = runtime.connected()
    version = await backend.version()
    base = Path(tempfile.mkdtemp(prefix="cos-step-probe-"))
    folder = base / "repo"
    await asyncio.to_thread(fixture, folder)
    # The steps' own policy (shepherd.step_policy), with the allowed host to install from.
    policy = StepSandboxPolicy(
        readwrite=(str(base),), readonly=tuple(await asyncio.to_thread(git_config_files)),
        credentials=False, outbound=True,
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
    unchecked: list[str] = []
    missing: list[str] = []
    session = None
    try:
        name = "Chief of Staff · step check"
        created = await backend.create_session(session_id=session_id, name=name, **options)
        with contextlib.suppress(Exception):
            await created.disconnect()
        session = await backend.resume_session(session_id, **options)
        failed += await extra_tools(session)
        address = await asyncio.to_thread(github_address)
        for what, line in actions(folder, address):
            ran, text, sandboxed = await run_tool(
                session, "bash", {"command": line, "description": what}
            )
            if not ran or RAN not in text:
                unchecked.append(what)  # It never ran: it shows nothing either way.
            elif REACHED in text or not sandboxed:
                failed.append(what if REACHED in text else f"{what} (unsandboxed)")
        ran, _, _ = await run_tool(session, "tell_session", {"session": "x", "text": "probe"})
        if ran:
            failed.append("calling tell_session")
        if mcp_tool:
            ran, _, _ = await run_tool(session, mcp_tool, {})
            if ran:
                failed.append(f"calling the MCP tool {mcp_tool}")
        for what, line in allowed(folder):
            ran, text, _ = await run_tool(session, "bash", {"command": line, "description": what})
            if not (ran and RAN in text and REACHED in text):
                missing.append(what)
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
    elif unchecked:
        why = f"{', '.join(unchecked)} could not be tried"
    elif missing:
        why = f"{' and '.join(missing)} did not work"
    return {"passed": not why, "why": why, "failed": failed, "unchecked": unchecked,
            "version": version, "at": now(), "checked": time.time()}  # fmt: skip


def _remove(path: Path):
    import shutil

    shutil.rmtree(path, ignore_errors=True)
