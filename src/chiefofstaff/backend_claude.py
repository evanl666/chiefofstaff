"""Claude workers: the owner's Claude Code CLI, driven through Anthropic's Agent SDK for Python
(the optional extra `chiefofstaff[claude]`), shown to the runtime as Copilot shows itself
(backend.AgentBackend and AgentSession), so the runtime cannot tell the difference.

- Each worker has a CLI process of its own (one process holds one session), started under Chief
  of Staff's supervisor (agentrun.py) by a launcher script: the SDK's `cli_path` is always the
  launcher, which runs the owner's installed `claude`, never the CLI the SDK bundles. Its
  environment is the owner's without CHIEFOFSTAFF_*; nothing about sign-in is added, so the CLI
  uses whatever it is signed in with (more-agents.md 5.12.1). Chief of Staff never reads,
  stores or forwards a credential: how it is signed in comes from `claude auth status`, of which
  four fields are kept.
- A session's id is its row's id as a UUID (`--session-id`, then `--resume`); open_live keeps it
  under `copilot:`/`alias:`.
- Every tool call reaches Chief of Staff's PreToolUse callback (`on_failure="block"`), shaped as
  the permission request Copilot sends, and Events.on_permission answers it; what still asks
  after it reaches can_use_tool, which repeats that answer. Every prompt reaches the
  UserPromptSubmit gate before the model: Chief of Staff's own messages pass (a turn's first only
  once admitted), a background command's end wakes the worker only once admitted
  (Runtime.admit_turn), anything else is blocked, at no cost.
- Chief of Staff's own tools are an in-process MCP server (`chief`, loaded upfront), its calls
  the worker's. Commands are `bash` with their exit codes where Claude says them; one run in the
  background holds the turn's end (assistant.idle) until it ends (system.notification).
- Usage is counted response by response during a turn and reconciled with Claude's totals at its
  result (`agent.usage`, cumulative, with this process as its epoch and what a resumed session
  restored, read before its first turn), then `agent.settled`.

Some of Claude's own capabilities (subagents, its worktrees, schedules, messages between
sessions, plan mode) are not tied into Chief of Staff yet: they are refused with why.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import os
import platform
import re
import shlex
import shutil
import signal
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from copilot.generated.rpc import PermissionDecisionReject
from copilot.tools import ToolInvocation

from . import agents
from .backend import (
    ATTACHED,
    Capabilities,
    attached_text,
    attachment_paths,
    descendants,
    end_processes,
    image_type,
    isolated,
    process_table,
)
from .machine import kill_process_tree
from .policy import GITHUB_MCP

logger = logging.getLogger(__name__)

CLAUDE_AGENT = "claude"

CLAUDE = Capabilities(
    name="Claude",
    steering=True,
    resume=True,
    permission_callbacks=True,
    mcp_sign_in=False,
    sandbox=False,
)
# The Claude Code versions this release was tested with: older ones (and one whose version
# cannot be told) do not run; workers that ask first run only up to the next minor version.
# Every version, patches included, is checked once on this machine before its workers run.
OLDEST_CLAUDE = "2.1.322"
ASK_FIRST_BEFORE = "2.2.0"
# The Agent SDK versions this release was tested with (the extra pins them too).
SDK_OLDEST = (0, 2, 182)
SDK_BEFORE = (0, 3, 0)
EXTRA = "Needs Chief of Staff's Claude extra: `uv tool install 'chiefofstaff[claude]'`"
NOT_FOUND = "Not found on this computer: install Claude Code."
NOT_SIGNED_IN = "Not signed in: run `claude` in a terminal and sign in."
# Claude never set up for the owner (no state file): `claude auth status` would make its files.
NOT_SET_UP = "Not set up: run `claude` in a terminal and sign in."
# How long Settings waits for `claude --version` and `claude auth status`.
LOOK_SECONDS = 30.0
# How long a hook of Chief of Staff's may take before Claude blocks what it was asked about.
HOOK_SECONDS = 120
# How long a worker's process may take to start and answer.
OPEN_SECONDS = float(os.environ.get("CHIEFOFSTAFF_CLAUDE_TIMEOUT", "90"))
# How long a request of Chief of Staff's to the CLI may take (an interrupt, a task's stop).
REQUEST_SECONDS = 30.0
# How long the read of a resumed session's totals may take.
PROBE_SECONDS = 30.0
# How long a worker's process whose settings changed has for its turn to end before it ends.
RETIRE_SECONDS = 15.0
# How often a worker's supervisor is looked at to see whether it still runs.
WATCH_SECONDS = 0.5
# How long a hook waits for the messages Claude sent before it to be handled.
CAUGHT_UP_SECONDS = 2.0
# How long a background command's end waits for the notice that says its exit code.
NOTICE_SECONDS = 2.0
# Claude's model aliases, offered before any worker's process has listed its models.
ALIASES = (("default", "Default"), ("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"))
# Of `claude auth status --json`, only these are read; the rest (who, which organization, which
# plan) is dropped as it is parsed, never kept, shown or logged.
AUTH_FIELDS = ("loggedIn", "authMethod", "apiProvider", "apiKeySource")
# The prompt a resumed session's process is sent first, blocked at the gate before any model
# call: Claude's answer says what the session restored (its totals), which its spend counts from.
PROBE = (
    "Chief of Staff reads what this conversation spent so far. This is not a request: it is "
    "stopped before the model sees it."
)
# How a background command's end says its exit code ("Background command "x" completed (exit
# code 0)") and a failed command's result begins ("Exit code 3").
NOTICE_CODE = re.compile(r"\(exit code (-?\d+)\)")
RESULT_CODE = re.compile(r"\AExit code (-?\d+)(?:\n|\Z)")
BACKGROUND_ID = re.compile(r"Command running in background with ID: (\S+?)\.?(?:\s|$)")
TASK_ID = re.compile(r"<task-id>([^<]+)</task-id>")
NOTIFICATION = "<task-notification>"
# A server that is GitHub's (its name, command or URL): its writes are the owner's to approve.
GITHUB = re.compile(r"github", re.IGNORECASE)
# A variable of Chief of Staff's the launcher's shell can unset by name.
OWN_NAME = re.compile(r"CHIEFOFSTAFF_[A-Za-z0-9_]+")
TERMINAL = frozenset({"completed", "failed", "killed", "stopped"})

# Claude's tools, as Chief of Staff's checks see them (more-agents.md 5.12.4).
SHELL_TOOLS = {"Bash": "command", "Monitor": "command"}
WRITE_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
READ_TOOLS = frozenset({"Read", "Grep", "Glob", "LSP", "NotebookRead"})
URL_TOOLS = frozenset({"WebFetch", "WebSearch"})
# Its own bookkeeping, and what acts on nothing outside its conversation: allowed.
FREE_TOOLS = frozenset(
    {
        "TodoWrite",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskUpdate",
        "TaskOutput",
        "TaskStop",
        "ToolSearch",
        "Skill",
        "AskUserQuestion",
    }
)
_SUBAGENTS = "Subagents aren't available to Claude workers yet: do this work yourself."
_SCHEDULES = (
    "Scheduled prompts aren't available to Claude workers yet: finish what you can now, and say "
    "in your report what to look at later."
)
_OWNER = 'Reach the owner through Chief of Staff instead: report(status="question") or report.'
# What is not tied into Chief of Staff yet: refused, with why (Claude reads it).
WITHHELD = {
    "Agent": _SUBAGENTS,
    "Task": _SUBAGENTS,
    "Workflow": _SUBAGENTS,
    "EnterWorktree": (
        "Claude's worktrees aren't tied into Chief of Staff yet: work in your folder and "
        "checkouts (add_repo gives you a worktree of another repository)."
    ),
    "ExitWorktree": "Claude's worktrees aren't tied into Chief of Staff yet.",
    "CronCreate": _SCHEDULES,
    "CronDelete": _SCHEDULES,
    "CronList": _SCHEDULES,
    "ScheduleWakeup": _SCHEDULES,
    "RemoteTrigger": _SCHEDULES,
    "SendMessage": "Reach other workers with tell_session or ask_session.",
    "ListAgents": "list_sessions shows the other workers.",
    "EnterPlanMode": "Plan mode isn't available to Claude workers yet: say your plan, then go on.",
    "ExitPlanMode": "Plan mode isn't available to Claude workers yet: say your plan, then go on.",
    "PushNotification": _OWNER,
    "SendUserFile": _OWNER,
    "Artifact": _OWNER,
    "ShareOnboardingGuide": _OWNER,
}
# Claude's tools by the names Chief of Staff's timeline and receipts know (Copilot's).
EVENT_NAMES = {
    "Bash": "bash",
    "Monitor": "bash",
    "Write": "create",
    "Edit": "edit",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
    "Read": "view",
    "NotebookRead": "view",
    "Grep": "grep",
    "Glob": "glob",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "Skill": "skill",
    "Agent": "task",
    "Task": "task",
    "AskUserQuestion": "ask_user",
    "TaskStop": "stop_bash",
    "TaskOutput": "read_bash",
}


class ClaudeError(RuntimeError):
    """Something Claude's CLI or the Agent SDK said went wrong."""


def event(kind: str, /, **data: Any) -> SimpleNamespace:
    return SimpleNamespace(type=SimpleNamespace(value=kind), data=SimpleNamespace(**data))


def mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def release(version: str) -> tuple[int, ...] | None:
    """A version, comparable: a pre-release ("2.2.0-beta.1") comes before its release."""
    found = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(-[\w.-]+)?", version or "")
    if not found:
        return None
    return (*map(int, found.group(1, 2, 3)), 0 if found.group(4) else 1)


def version_of(said: str) -> str:
    """The CLI's version, from what `claude --version` prints ("2.1.322 (Claude Code)")."""
    found = re.match(r"\s*(\d+\.\d+\.\d+[\w.-]*)", said or "")
    return found.group(1) if found else ""


def claude_refused(version: str) -> str:
    """Why this Claude Code does not run at all ("" when it does)."""
    if (found := release(version)) is None:
        return (
            "Chief of Staff could not tell which version of Claude Code this is "
            f"({version or 'none'} said): update it to {OLDEST_CLAUDE} or later."
        )
    if found < (release(OLDEST_CLAUDE) or ()):
        return f"Older than the versions tested ({OLDEST_CLAUDE}): update it (claude update)."
    return ""


def ask_first_tested(version: str) -> bool:
    found = release(version)
    oldest, before = release(OLDEST_CLAUDE) or (), release(ASK_FIRST_BEFORE) or ()
    return found is not None and oldest <= found and found[:3] < before[:3]


def ask_first_refusal(version: str, checked: dict | None) -> str:
    if not ask_first_tested(version):
        return (
            f"Claude Code {version} has not passed Chief of Staff's checks for Ask first (tested "
            f"from {OLDEST_CLAUDE}, before {ASK_FIRST_BEFORE}): let this worker act on its own, "
            "or use a tested version."
        )
    return (checked or {}).get("ask_first") or ""


def sdk_version() -> str:
    """The Agent SDK's version ("" when it is not installed: Chief of Staff's Claude extra)."""
    from importlib import metadata

    try:
        return metadata.version("claude-agent-sdk")
    except metadata.PackageNotFoundError:
        return ""


def sdk_refused() -> str:
    """Why the Agent SDK installed cannot run Claude workers ("" when it can)."""
    version = sdk_version()
    if not version:
        return EXTRA
    found = release(version)
    if found is None or not (SDK_OLDEST <= found[:3] < SDK_BEFORE):
        return (
            f"Chief of Staff's Claude extra has an Agent SDK it was not tested with ({version}): "
            "reinstall it: `uv tool install 'chiefofstaff[claude]'`"
        )
    return ""


def client_version() -> str:
    from importlib import metadata

    try:
        return metadata.version("chiefofstaff")
    except metadata.PackageNotFoundError:
        return "dev"


def dashed(session_id: str) -> str:
    """A session id as Claude takes it (a UUID): a row's 32 hex digits, dashed; another id stands
    for one UUID of its own, always the same."""
    try:
        return str(uuid.UUID(session_id))
    except (ValueError, TypeError, AttributeError):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"chiefofstaff:{session_id}"))


def owner_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("CHIEFOFSTAFF_")}


def signed_in(auth: dict) -> str:
    """How Claude is signed in, as Settings names it (agents.SIGNED_IN): never who."""
    provider = auth.get("apiProvider") or ""
    method = auth.get("authMethod") or ""
    if provider in {"bedrock", "amazonBedrock"}:
        return "amazonBedrock"
    if provider in {"vertex", "gcp"}:
        return "gcp"
    if provider == "foundry":
        return "foundry"
    if method == "claude.ai":
        return "claudePlan"
    if method in {"api_key", "apiKey"} or auth.get("apiKeySource"):
        return "apiKey"
    return ""


def bash_shell() -> str:
    """The shell Claude's Bash tool runs a command line in, as the policy reads it: the one
    CLAUDE_CODE_SHELL names, else the owner's login shell when it is bash or zsh, else bash."""
    for name in (os.environ.get("CLAUDE_CODE_SHELL"), os.environ.get("SHELL")):
        shell = Path(name or "").name
        if shell in {"bash", "zsh", "sh"}:
            return shell
    return "bash"


def counts(usage: Any) -> dict[str, int]:
    """One response's usage, or Claude's totals for a model, in the ledger's unit (tokens, cached
    input included, as Codex says them: runtime.counters)."""
    usage = mapping(usage)

    def number(*names: str) -> int:
        for name in names:
            value = usage.get(name)
            if isinstance(value, int) and value > 0:
                return value
        return 0

    fresh = number("input_tokens", "inputTokens")
    read = number("cache_read_input_tokens", "cacheReadInputTokens")
    written = number("cache_creation_input_tokens", "cacheCreationInputTokens")
    output = number("output_tokens", "outputTokens")
    given = fresh + read + written
    return {
        "inputTokens": given,
        "cachedInputTokens": read,
        "cacheWriteInputTokens": written,
        "outputTokens": output,
        "totalTokens": given + output,
    }


def added(*parts: dict[str, int]) -> dict[str, int]:
    found: dict[str, int] = {}
    for part in parts:
        for name, value in part.items():
            found[name] = found.get(name, 0) + value
    return found


def larger(one: dict[str, int], other: dict[str, int]) -> dict[str, int]:
    return {name: max(one.get(name, 0), other.get(name, 0)) for name in {*one, *other}}


def model_totals(model_usage: Any) -> dict[str, int]:
    """Claude's running totals (a result's `modelUsage`: each model's), as one count."""
    return added(*(counts(entry) for entry in mapping(model_usage).values()), counts({}))


class Tally:
    """What a process of a session spent, as Claude's totals have it: those known at its last
    result (from the totals it restored on), and each response since, counted once as it comes
    (one response arrives in parts: the most each part said). What is told never goes down (the
    ledger takes a decrease for a crash): a count past Claude's own is kept until Claude's
    passes it."""

    def __init__(self, base: dict[str, int] | None = None):
        self.base = dict(base or counts({}))
        self.responses: dict[str, dict[str, int]] = {}
        self.told = dict(self.base)

    def response(self, key: str, usage: Any) -> bool:
        now = counts(usage)
        before = self.responses.get(key, counts({}))
        merged = larger(before, now)
        merged["totalTokens"] = merged["inputTokens"] + merged["outputTokens"]
        if merged == before:
            return False
        self.responses[key] = merged
        return True

    def result(self, model_usage: Any) -> None:
        self.base = model_totals(model_usage)
        self.responses.clear()

    def total(self) -> dict[str, int]:
        self.told = larger(self.told, added(self.base, *self.responses.values()))
        return dict(self.told)


def text_of(content: Any) -> str:
    """The text of a message's content (a string, or blocks), as Claude's gate reads it."""
    if isinstance(content, str):
        return content
    return "\n".join(
        str(block.get("text") or "")
        for block in content or []
        if isinstance(block, dict) and block.get("type") == "text"
    )


def owners_text(content: Any) -> str:
    """A message's text as Chief of Staff wrote it: without the blocks it added (the lines
    naming files attached to it)."""
    if isinstance(content, str):
        return content
    return text_of(
        [b for b in content or [] if not str(mapping(b).get("text") or "").startswith(ATTACHED)]
    )


def result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        str(block.get("text") or "")
        for block in content or []
        if isinstance(block, dict) and block.get("type") == "text"
    )


def bash_exit(text: str, is_error: bool, structured: Any) -> int | None:
    """A Bash command's exit code, where Claude says it: 0 for one it ran to a plain success,
    the number its failure begins with ("Exit code 3"); None otherwise (a code Claude read as
    no failure, `grep` finding nothing, says only that; a time-out, a stop)."""
    if is_error:
        found = RESULT_CODE.match(text or "")
        return int(found.group(1)) if found else None
    if not isinstance(structured, dict) or structured.get("interrupted"):
        return None
    return None if structured.get("returnCodeInterpretation") else 0


def event_tool(name: str, arguments: Any) -> tuple[str, dict]:
    """A tool call as the timeline and receipts know it (Copilot's names and arguments)."""
    arguments = mapping(arguments)
    if name.startswith("mcp__"):
        server, _, tool = name[5:].partition("__")
        if server == "chief":
            return tool, arguments
        return f"{server}-{tool}", arguments
    shown = EVENT_NAMES.get(name, name)
    if name in SHELL_TOOLS:
        return shown, {
            "command": str(arguments.get("command") or ""),
            **({"description": arguments["description"]} if arguments.get("description") else {}),
        }
    if name in WRITE_TOOLS or name in {"Read", "NotebookRead"}:
        path = arguments.get("file_path") or arguments.get("notebook_path") or ""
        return shown, {"path": str(path)}
    if name in {"TaskStop", "TaskOutput"}:
        return shown, {"shellId": str(arguments.get("task_id") or arguments.get("shell_id") or "")}
    return shown, arguments


def joined(folder: str, path: str) -> str:
    """A path a tool names, from its folder (`..` taken away, links not followed)."""
    return os.path.normpath(Path(folder) / path)


def github_server(name: str, config: Any) -> bool:
    if GITHUB.search(name):
        return True
    config = mapping(config)
    parts = [config.get("command"), config.get("url"), *(config.get("args") or [])]
    return any(isinstance(part, str) and GITHUB.search(part) for part in parts)


def claude_mcp(entry: Any) -> dict | None:
    """An MCP server as Chief of Staff's Settings have it (Copilot's form: a command, or a URL),
    as Claude's configuration has one; None for one it cannot run."""
    entry = mapping(entry)
    if isinstance(url := entry.get("url"), str) and url:
        kind = "sse" if entry.get("type") == "sse" else "http"
        found: dict[str, Any] = {"type": kind, "url": url}
        if headers := mapping(entry.get("headers")):
            found["headers"] = {str(k): str(v) for k, v in headers.items()}
        return found
    if isinstance(command := entry.get("command"), str) and command:
        found = {"type": "stdio", "command": command}
        if args := entry.get("args"):
            found["args"] = [str(arg) for arg in args]
        if env := mapping(entry.get("env")):
            found["env"] = {str(k): str(v) for k, v in env.items()}
        if isinstance(cwd := entry.get("cwd"), str) and cwd:
            # Claude's servers have no folder of their own: started in it.
            words = [command, *found.get("args", [])]
            found["command"] = "/bin/sh"
            found["args"] = ["-c", f"cd {shlex.quote(cwd)} && exec " + shlex.join(words)]
        return found
    return None


def config_folder() -> Path:
    """Claude's configuration folder for the owner (its CLAUDE_CONFIG_DIR, else ~/.claude)."""
    given = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(given).expanduser() if given else Path.home() / ".claude"


def state_file() -> Path:
    """Claude's own state file (`.claude.json`): in CLAUDE_CONFIG_DIR when set, else home."""
    given = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(given).expanduser() / ".claude.json" if given else Path.home() / ".claude.json"


def owner_trusts(folder: str) -> bool:
    """Whether the owner accepted Claude's trust dialog for this folder or one above it: the one
    field Chief of Staff reads of Claude's state file (`projects[…].hasTrustDialogAccepted`),
    parsed and dropped. Then the repository's own settings load for the worker there, as for the
    owner (setting_sources)."""
    try:
        with state_file().open(encoding="utf-8") as file:
            projects = mapping(json.load(file).get("projects"))
    except (OSError, ValueError, AttributeError):
        return False
    place = Path(folder)
    for ancestor in [place, *place.parents]:
        if mapping(projects.get(str(ancestor))).get("hasTrustDialogAccepted") is True:
            return True
    return False


def quiet_folder(folder: Path | None) -> Path:
    """Where a short-lived `claude` command runs: Chief of Staff's own folder for Claude (made
    if missing), else the owner's home; never the folder Chief of Staff was started in (a
    repository, whose .claude/settings.json Claude would read)."""
    if folder is None:
        return Path.home()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


async def run_claude(
    command: str, *arguments: str, env: dict | None = None, folder: Path | None = None
) -> tuple[int, str]:
    """A short-lived `claude` command (its version, how it is signed in): its exit code and
    what it printed (never logged), run in `folder` (quiet_folder). Its process group ends if
    it does not answer in time."""
    cwd = await asyncio.to_thread(quiet_folder, folder)
    process = await asyncio.create_subprocess_exec(
        command,
        *arguments,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=owner_environment() if env is None else env,
        start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), LOOK_SECONDS)
    except TimeoutError:
        with suppress(PermissionError):
            kill_process_tree(process.pid)
        await process.wait()
        raise RuntimeError("Claude Code did not answer in time: try again.") from None
    return process.returncode or 0, output.decode(errors="replace")


async def claude_version(command: str, folder: Path | None = None) -> str:
    code, said = await run_claude(command, "--version", folder=folder)
    return version_of(said) if code == 0 else ""


def set_up() -> bool:
    """Whether Claude was set up for the owner: its state file is there. Without it, `claude auth
    status` makes the owner's Claude files (that file, a backups folder): it is not run then."""
    return state_file().is_file()


async def auth_status(command: str, folder: Path | None = None) -> dict:
    """How Claude is signed in: the four fields Chief of Staff reads of `claude auth status`
    (AUTH_FIELDS), and its exit code. Nothing else of it is kept. Raises NOT_SET_UP, running
    nothing, when Claude was never set up (set_up)."""
    if not await asyncio.to_thread(set_up):
        raise RuntimeError(NOT_SET_UP)
    code, said = await run_claude(command, "auth", "status", "--json", folder=folder)
    try:
        parsed = json.loads(said)
    except ValueError:
        parsed = {}
    kept = {name: mapping(parsed).get(name) for name in AUTH_FIELDS}
    del parsed, said
    return kept | {"exit": code}


def alias_models() -> list[SimpleNamespace]:
    return [model_entry({"value": value, "displayName": name}) for value, name in ALIASES]


def model_entry(model: dict) -> SimpleNamespace:
    efforts = [str(e) for e in model.get("supportedEffortLevels") or [] if isinstance(e, str)]
    value = str(model.get("value") or "")
    return SimpleNamespace(
        id=value,
        name=str(model.get("displayName") or value),
        policy=None,
        billing=None,
        capabilities=SimpleNamespace(limits=SimpleNamespace(max_prompt_tokens=0)),
        supported_reasoning_efforts=efforts if model.get("supportsEffort", bool(efforts)) else [],
        default_reasoning_effort="",
        is_default=value == "default",
    )


async def look(command: str = "claude", folder: Path | None = None) -> dict:
    """What Settings shows of the Claude Code on this computer, without starting a session (its
    version and how it is signed in, from two short-lived commands; its models as its workers
    last listed them, else Claude's aliases); raises with why its workers cannot run (not found,
    too old, the extra missing, signed out)."""
    path = shutil.which(command)
    if not path:
        raise RuntimeError(NOT_FOUND)
    if why := sdk_refused():
        raise RuntimeError(why)
    version = await claude_version(path, folder)
    if why := claude_refused(version):
        raise RuntimeError(why)
    auth = await auth_status(path, folder)
    if not auth.get("loggedIn"):
        raise RuntimeError(NOT_SIGNED_IN)
    return {
        "version": version,
        "account": signed_in(auth),
        "models": list(LISTED.get(version) or alias_models()),
        "check_key": await asyncio.to_thread(check_key, path, version),
    }


# Each version's models as the last worker's process listed them (initialize), for Settings.
LISTED: dict[str, list[SimpleNamespace]] = {}
# What tells one CLI binary from another (path, size, time) -> its SHA-256: hashed once.
DIGESTS: dict[tuple[str, int, int], str] = {}


def check_key(path: str, version: str) -> str:
    """What a version's check is kept by (agentcheck:claude:<key>): the CLI's resolved path,
    size, modification time and SHA-256, its version, the Agent SDK's, this release's, the
    platform and the check's own revision. Any change checks it again."""
    from . import claude_check

    real = Path(path).resolve()
    try:
        info = real.stat()
    except OSError:
        return f"{version}-unknown"
    key = (str(real), info.st_size, info.st_mtime_ns)
    if key not in DIGESTS:
        digest = hashlib.sha256()
        with real.open("rb") as file:
            for chunk in iter(lambda: file.read(1 << 20), b""):
                digest.update(chunk)
        DIGESTS[key] = digest.hexdigest()
    parts = [
        str(real),
        str(info.st_size),
        str(info.st_mtime_ns),
        DIGESTS[key],
        version,
        sdk_version(),
        client_version(),
        sys.platform,
        platform.machine(),
        str(claude_check.REVISION),
    ]
    return f"{version}-{hashlib.sha256(chr(0).join(parts).encode()).hexdigest()[:16]}"


def write_launcher(folder: Path, command: str, environment: Path | None = None) -> Path:
    """The program the Agent SDK starts as Claude Code (its `cli_path`), in this folder of Chief
    of Staff's own: its supervisor, isolated (agentrun.isolated: no file of the worker's folder
    runs as it), which starts the owner's own `claude` in the worker's folder (the SDK's
    CHIEFOFSTAFF_AGENT_CWD; none, it does not start); mode 0700, written again at each start."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    launcher = folder / ("check-launcher" if environment else "launcher")
    chosen = ["--environment", str(environment)] if environment else []
    supervisor = shlex.join(isolated("chiefofstaff.agentrun", *chosen))
    # Chief of Staff's own variables, named by it (OWN_UNSET), are unset before its supervisor
    # starts: the SDK passes its whole environment, and can only add to it.
    launcher.write_text(
        "#!/bin/sh\n"
        'cwd="${CHIEFOFSTAFF_AGENT_CWD:?}"\n'
        'for name in ${CHIEFOFSTAFF_AGENT_UNSET-}; do unset "$name"; done\n'
        "unset CHIEFOFSTAFF_AGENT_CWD CHIEFOFSTAFF_AGENT_UNSET\n"
        f'exec {supervisor} --cwd "$cwd" -- {shlex.quote(command)} "$@"\n'
    )
    launcher.chmod(0o700)
    return launcher


def write_private(path: Path, text: str):
    """A file only the owner can read (0600), made new (never one that was there)."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(text)


def private_folder(launcher: Path) -> Path:
    """Where a worker's process's MCP configuration and system prompt are, as files of their own
    (not its command line, which other users of the computer can read): beside its launcher."""
    folder = launcher.parent / "private"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    return folder


def clear_private(launcher: Path):
    """What processes of an earlier Chief of Staff left there (it was killed)."""
    agents.clear_private(launcher.parent, keep_own=True)


class Sent:
    """A message Chief of Staff wrote to a worker's CLI: its text as the gate sees it, the id it
    carries (the echo carries it back), and what became of it."""

    def __init__(
        self, content: Any, text: str, *, kind: str, probe: bool = False, shown: str | None = None
    ):
        self.content = content
        self.text = text
        # The message as Chief of Staff wrote it (its echo): without the lines it added.
        self.shown = text if shown is None else shown
        self.uuid = str(uuid.uuid4())
        # "turn": the first of a turn admitted; "steer": into the turn in progress.
        self.kind = kind
        self.probe = probe
        self.stopped = False
        self.steered = False


class Taken:
    """How Claude took up a message of Chief of Staff's (a user.message's `delivery`), read when
    Events handles it: "stopped" once the turn that took it up was stopped, else "steering" when
    it came into a turn in progress, else ""."""

    def __init__(self, sent: Sent):
        self.sent = sent

    @property
    def value(self) -> str:
        if self.sent.stopped:
            return "stopped"
        return "steering" if self.sent.steered else ""


class ClaudeSession:
    """A worker's Claude session, as an AgentSession: a handle on its process (ClaudeWorker).
    Let go of (disconnect), its process ends once nothing of it runs; a later handle on the
    same process (a resume while it ran) gets its events from then on."""

    def __init__(self, worker: ClaudeWorker):
        self.worker = worker
        self.session_id = worker.session_id
        self.handlers: list[Callable[[Any], None]] = []
        self.closed = False

    @property
    def backend(self) -> ClaudeWorker:
        return self.worker  # Its process: the epoch its usage counts in (Runtime.admit_turn).

    def on(self, handler: Callable[[Any], None]) -> Callable[[], None]:
        self.handlers.append(handler)
        return lambda: self.handlers.remove(handler) if handler in self.handlers else None

    async def send(
        self, prompt: str, *, mode: str = "enqueue", attachments: list[Any] | None = None
    ) -> str:
        if self.closed or not (self.worker.alive() or self.worker.restarting()):
            raise ConnectionResetError("This Claude session is disconnected.")
        return await self.worker.send(prompt, mode=mode, attachments=attachments)

    async def abort(self) -> None:
        await self.worker.abort()

    async def disconnect(self) -> None:
        self.closed = True
        self.handlers.clear()
        await self.worker.let_go(self)

    def idle(self) -> bool:
        return self.worker.idle()

    def usage_pending(self) -> bool:
        return self.worker.usage_pending()

    async def set_model(
        self, model: str, *, reasoning_effort: str | None = None, context_tier: Any = None
    ) -> None:
        await self.worker.set_model(model, reasoning_effort or "")

    async def get_events(self) -> list[Any]:
        return await self.worker.get_events()

    async def running_background(self) -> int:
        return self.worker.running_background()

    async def stop_background(self) -> int:
        return await self.worker.stop_background()

    async def take_queued(self) -> int:
        return self.worker.take_queued()

    async def queue_holds(self) -> bool:
        return self.worker.queue_holds()


class ClaudeWorker:
    """One worker's Claude Code process, and its one session. The process lives in an asyncio
    task of its own (run), which connects, reads Claude's messages in order and disconnects;
    what Chief of Staff sends goes to the CLI's input, and Claude's requests (hooks, permission
    questions, Chief of Staff's tools) are answered once what Claude said before them is
    handled (caught_up)."""

    def __init__(
        self,
        agent: ClaudeAgent,
        worker: str,
        session_id: str,
        options: dict,
        *,
        resume: bool,
        launcher: Path,
        checking: bool = False,
    ):
        self.agent = agent
        self.worker = worker
        self.session_id = session_id
        self.options = options
        self.resume = resume
        self.launcher = launcher
        self.checking = checking
        self.tools = {tool.name: tool for tool in options.get("tools") or []}
        self.cwd = options.get("working_directory") or str(Path.home())
        self.ask_first = bool(options.get("ask_first"))
        self.model = options.get("model") or ""
        self.effort = options.get("reasoning_effort") or ""
        self.process_effort = self.effort
        self.handle: ClaudeSession | None = None
        self.client: Any = None
        self.query: Any = None
        self.task: asyncio.Task | None = None
        self.reader: asyncio.Task | None = None
        self.ready: asyncio.Future | None = None
        self.probed: asyncio.Future | None = None
        self.stop_requested = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        self.stderr: list[str] = []
        # This process, as its usage's accounting epoch (5.8), and its supervisor.
        self.epoch = ""
        self.pid = 0
        self.started: dict[int, str] = {}
        self.initialized: dict = {}
        # A turn in progress (its id), and one admitted whose message the CLI has not begun.
        self.turn: str | None = None
        self.turns: set[str] = set()
        self.starting = False
        self.aborting = False
        # Each Stop (abort): a turn starting when one comes is stopped before it begins.
        self.aborts = 0
        self.unshown = ""
        self.wakeups = 0
        # Messages for after the turn (content, the text the gate sees), Chief of Staff's own
        # written and not yet echoed (by id), and those not yet seen at the gate, in order.
        self.queue: list[tuple[Any, str, str]] = []
        self.written: dict[str, Sent] = {}
        self.at_gate: list[Sent] = []
        # Tool calls started and not ended: id -> (Claude's name, the subagent call it is in);
        # those of its own loop still running (steering goes in only while one runs).
        self.calls: dict[str, tuple[str, str]] = {}
        self.running_calls: set[str] = set()
        # The CLI's tasks seen started (a background command's end may wake the worker), those
        # running, and the commands its turns left running: task id -> the call that started it.
        self.known_tasks: set[str] = set()
        self.running_tasks: dict[str, dict] = {}
        self.background: dict[str, str] = {}
        self.settled_tasks: set[str] = set()
        # The text of its own loop's latest response, not yet told (narration or its answer).
        self.pending_text: tuple[str, str] | None = None
        self.streaming = ""
        self.current_model = self.model
        self.tally = Tally()
        self.restored: dict[str, int] | None = None
        self.told_usage = False
        self.last_told: dict[str, int] = {}
        # PreToolUse's answers, by tool call (can_use_tool repeats them), and MCP servers as
        # Claude has them (for which are GitHub's, and which tools only read).
        self.allowed: dict[str, str] = {}
        self.servers: dict[str, dict] | None = None
        self.unhandled: set[str] = set()
        # Its process's end: asked (stop), by itself (a crash), or by Chief of Staff (why).
        self.stopping = False
        self.by_itself = False
        self.refusal = ""
        self.told_end = False
        self.opening = 0
        self.retiring: asyncio.Future | None = None
        self.on_end: Callable[[bool], None] | None = None
        self.handling = False
        # The current process's MCP configuration and system prompt, as files (private_files):
        # each process's go with it (run).
        self.private: list[Path] = []

    # The process

    def private_files(self) -> dict[str, str]:
        """Its MCP servers (the owner's, with their headers and variables, and the check's) and
        Chief of Staff's system prompt, in files only the owner can read, for this process: the
        CLI's arguments that name them (on its command line, only their paths)."""
        servers = {
            str(name): found
            for name, entry in mapping(self.options.get("mcp_servers")).items()
            if str(name) != "chief" and (found := claude_mcp(entry))
        }
        servers.update(mapping(self.options.get("checking_servers")))
        prompt = (self.options.get("system_message") or {}).get("content") or ""
        folder = private_folder(self.launcher)
        stem = f"{os.getpid()}-{self.epoch}"
        given: dict[str, str] = {}
        self.private = []
        if servers:
            path = folder / f"{stem}-mcp.json"
            self.private.append(path)
            write_private(path, json.dumps({"mcpServers": servers}))
            given["mcp-config"] = str(path)
        if prompt:
            path = folder / f"{stem}-prompt.md"
            self.private.append(path)
            write_private(path, prompt)
            given["append-system-prompt-file"] = str(path)
        return given

    def client_options(self) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

        folders = [f for f in self.options.get("folders") or [] if f and f != self.cwd]
        # The repository's own settings (its hooks, MCP servers, CLAUDE.md) only where the owner
        # trusts it in Claude.
        trusted = not self.checking and owner_trusts(self.cwd)
        sources: list[Any] = ["user", "project"] if trusted else ["user"]
        effort: Any = self.process_effort or None
        settings: dict[str, Any] = {"autoMemoryEnabled": False, "sandbox": {"enabled": False}}
        if self.ask_first:
            settings["disableAllHooks"] = True  # The owner's hooks and mods: off (5.7).
        files = self.private_files()
        # Chief of Staff's own variables (the Slack token among them) reach neither its
        # supervisor nor the CLI: the launcher unsets those it names (a name the shell cannot
        # unset is passed empty), and the folder the CLI starts in is said.
        own = [key for key in os.environ if key.startswith("CHIEFOFSTAFF_")]
        environment = {key: "" for key in own if not OWN_NAME.fullmatch(key)}
        environment |= {
            "CHIEFOFSTAFF_AGENT_CWD": self.cwd,
            "CHIEFOFSTAFF_AGENT_UNSET": " ".join(key for key in own if OWN_NAME.fullmatch(key)),
        }
        environment |= {"CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1"}
        servers: dict[str, Any] = {"chief": self.chief_server()}

        def every(hook: Any) -> Any:
            # Every tool and prompt; a hook that fails or takes too long blocks.
            return [
                HookMatcher(matcher=None, hooks=[hook], timeout=HOOK_SECONDS, on_failure="block")
            ]

        return ClaudeAgentOptions(
            cli_path=str(self.launcher),
            # The launcher's own folder, Chief of Staff's: the CLI starts in the worker's.
            cwd=str(self.launcher.parent),
            add_dirs=folders,
            permission_mode="default",
            setting_sources=sources,
            system_prompt={"type": "preset", "preset": "claude_code"},
            # Chief of Staff's tools, in this process; the other servers are in a file.
            mcp_servers=servers,
            hooks={
                "PreToolUse": every(self.pre_tool_use),
                "UserPromptSubmit": every(self.prompt_gate),
            },
            can_use_tool=self.can_use_tool,
            include_partial_messages=True,
            verbatim_prompts=True,
            extra_args={
                "replay-user-messages": None,
                "name": f"cos-{self.worker[:8]}",
                **files,
            },
            settings=json.dumps(settings),
            env=environment,
            model=self.model or None,
            effort=effort,
            stderr=self.on_stderr,
            # A message comes on one line (a command's whole output): the SDK's 1 MB is not
            # enough for every one.
            max_buffer_size=64 * 1024 * 1024,
            session_id=None if self.resume else self.session_id,
            resume=self.session_id if self.resume else None,
        )

    def on_stderr(self, line: str):
        self.stderr = [*self.stderr[-19:], line[:500]]
        logger.debug("claude %s: %s", self.worker, line[:500])

    async def open(self):
        """Its process, started, connected and read back; a resumed session's totals read. Raises
        with why it cannot run."""
        loop = asyncio.get_running_loop()
        self.ready = loop.create_future()
        self.stop_requested = asyncio.Event()
        self.stopping = self.by_itself = self.told_end = False
        self.epoch = uuid.uuid4().hex
        # What it spends in this process counts from what it restores on (its probe).
        self.restored, self.told_usage, self.last_told = None, False, {}
        self.tally = Tally()
        self.task = asyncio.ensure_future(self.run())
        try:
            await asyncio.wait_for(asyncio.shield(self.ready), OPEN_SECONDS)
        except TimeoutError:
            await self.stop()
            raise RuntimeError("Claude Code did not start in time: try again.") from None
        except BaseException:
            await self.stop()
            raise

    async def run(self):
        """The process's life, in this one task: connect, read until it ends or is stopped, then
        disconnect (stdin closed; the supervisor ends what it ran)."""
        from claude_agent_sdk import ClaudeSDKClient

        ready = self.ready or asyncio.get_running_loop().create_future()
        client = None
        stopped: asyncio.Task | None = None
        files: list[Path] = []
        try:
            try:
                options = self.client_options()
            finally:
                files = list(self.private)  # This process's, whatever comes after it.
            client = ClaudeSDKClient(options)
            await client.connect()
            self.client, self.query = client, getattr(client, "_query", None)
            transport = getattr(client, "_transport", None)
            process = getattr(transport, "_process", None)
            if self.query is None or process is None:
                raise RuntimeError(
                    f"The Agent SDK installed ({sdk_version()}) is not one Chief of Staff was "
                    "tested with: reinstall its Claude extra."
                )
            self.pid = process.pid
            self.read_back(mapping(getattr(self.query, "_initialization_result", None)))
            self.reader = asyncio.ensure_future(self.read())
            self.tasks.add(asyncio.ensure_future(self.watch(process)))
            if self.resume:
                await self.probe()
            if not ready.done():
                ready.set_result(None)
            stopped = asyncio.ensure_future(self.stop_requested.wait())
            await asyncio.wait({self.reader, stopped}, return_when=asyncio.FIRST_COMPLETED)
            self.by_itself = not self.stopping
        except Exception as exc:
            if not ready.done():
                ready.set_exception(self.start_error(exc))
            else:
                self.by_itself = not self.stopping
                logger.warning("Claude's process of %s failed: %r", self.worker, exc)
        finally:
            self.stopping = True
            if stopped:
                stopped.cancel()
            if client is not None:
                with suppress(Exception):
                    await asyncio.shield(client.disconnect())
            if self.reader and not self.reader.done():
                self.reader.cancel()
            for task in list(self.tasks):
                task.cancel()
            await asyncio.to_thread(self.end_leftovers)
            for path in files:
                with suppress(OSError):
                    path.unlink()
            self.lost()
            self.ended()

    def start_error(self, exc: BaseException) -> BaseException:
        """Why its process could not start, in words the owner can act on."""
        said = "\n".join(self.stderr)
        if "is already in use" in said:
            return SessionInUseError(f"Claude already has session {self.session_id}.")
        if isinstance(exc, RuntimeError) and not type(exc).__module__.startswith("claude_agent"):
            return exc
        last = next((line for line in reversed(self.stderr) if line.strip()), "")
        detail = last or str(exc) or type(exc).__name__
        return RuntimeError(f"Claude Code could not start this worker: {detail[:300]}")

    def read_back(self, initialized: dict):
        """What the CLI started with must be what Chief of Staff's checks need: its hooks in
        effect (they block when they fail), the default permission mode, this version."""
        self.initialized = initialized
        problems = []
        if initialized.get("hooks_applied") is not True:
            problems.append("its hooks not applied")
        if initialized.get("hook_on_failure_supported") is not True:
            problems.append("hooks that fail not blocking")
        if (mode := initialized.get("current_permission_mode")) not in {None, "default"}:
            problems.append(f"permission mode {mode}")
        version = str(initialized.get("claude_code_version") or "")
        if version and self.agent.cli_version and version != self.agent.cli_version:
            raise RuntimeError(
                f"Claude Code was updated ({self.agent.cli_version} to {version}): Chief of "
                "Staff checks it again first; send it again."
            )
        if problems:
            raise RuntimeError(
                "Claude did not start this worker with the settings Chief of Staff's checks need ("
                + ", ".join(problems)
                + "): an administrator's Claude settings may override them."
            )
        models = [model_entry(m) for m in initialized.get("models") or [] if isinstance(m, dict)]
        if models and version:
            LISTED[version] = models

    async def probe(self):
        """A resumed session's totals, as Claude restored them: a prompt the gate blocks before
        any model call, whose result says them. What it spends from now counts from there."""
        loop = asyncio.get_running_loop()
        self.probed = loop.create_future()
        sent = Sent(PROBE, PROBE, kind="probe", probe=True)
        await self.write(sent)
        try:
            self.restored = await asyncio.wait_for(asyncio.shield(self.probed), PROBE_SECONDS)
        except TimeoutError:
            raise RuntimeError(
                "Claude did not say what this conversation spent before: try again."
            ) from None
        self.tally = Tally(self.restored)

    async def watch(self, process: Any):
        """Its supervisor ended by itself (killed): what is left of its process ends with it."""
        while not self.stopping:
            if process.returncode is not None:
                self.by_itself = True
                self.stop_requested.set()
                return
            await asyncio.sleep(WATCH_SECONDS)

    def restarting(self) -> bool:
        """Its process restarts for a turn (a new effort): what is sent meanwhile waits for it
        (the turn's queue)."""
        return self.starting and self.opening > 0 and not self.refusal

    def alive(self) -> bool:
        return (
            self.task is not None
            and not self.task.done()
            and not self.stopping
            and self.reader is not None
            and not self.reader.done()
        )

    async def stop(self):
        """It ends (its turn, what it left running), in its own task; awaited."""
        self.stopping = True
        self.stop_requested.set()
        if self.task and self.task is not asyncio.current_task():
            with suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self.task), STOP_SECONDS)
        if self.task and not self.task.done():
            # Its disconnect did not end it: its supervisor is told, and ends its tree.
            with suppress(ProcessLookupError, PermissionError):
                if self.pid:
                    os.kill(self.pid, signal.SIGTERM)

    def processes(self, table: dict | None = None) -> dict[int, str]:
        if self.pid and self.alive():
            table = process_table() if table is None else table
            self.started |= {
                pid: table[pid][1]
                for pid in {self.pid, *descendants(table, self.pid)}
                if pid in table
            }
        return dict(self.started)

    def end_leftovers(self):
        """What outlived the supervisor (it was killed): ended as Copilot's leftovers are."""
        if not self.started:
            return
        table = process_table()
        left = {pid: c for pid, c in self.started.items() if table.get(pid, (0, ""))[1] == c}
        if left:
            end_processes(left)
        self.started = {}

    def lost(self):
        """Its process ended: nothing of it runs any more."""
        if self.ready and not self.ready.done():
            self.ready.set_exception(RuntimeError("Claude Code stopped."))
        if self.probed and not self.probed.done():
            self.probed.set_exception(RuntimeError("Claude Code stopped."))

    def ended(self):
        if self.on_end and not self.told_end:
            self.told_end = True
            try:
                self.on_end(self.by_itself)
            except Exception:
                logger.exception("Ending %s's Claude process failed.", self.worker)

    def stop_worker(self, why: str):
        """Something Chief of Staff does not let a worker do: its process ends at once, and its
        worker stops with why (Runtime.agent_process_ended)."""
        self.refusal = self.refusal or why
        logger.warning("Stopping %s's Claude process: %s", self.worker, why)
        self.background_task(self.stop())

    def background_task(self, coroutine: Awaitable[Any]):
        task = asyncio.ensure_future(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # Handles

    def attach(self, options: dict) -> ClaudeSession:
        """A handle on its session, taking its callbacks (the earlier handle's events stop)."""
        keep = ("tools", "system_message", "mcp_servers", "folders", "working_directory")
        self.options = {**options, **{k: self.options[k] for k in keep if k in self.options}}
        if self.handle:
            self.handle.closed = True
            self.handle.handlers.clear()
        self.handle = ClaudeSession(self)
        return self.handle

    async def let_go(self, handle: ClaudeSession):
        if handle is not self.handle:
            return
        if self.idle():
            self.queue.clear()
            self.release_if_idle()

    def release_if_idle(self):
        """Its handle let go of, nothing of it running, and none being opened on it: it ends (the
        worker's next message starts another, which resumes it)."""
        if self.stopping or self.opening or self.checking:
            return
        if (self.handle is None or self.handle.closed) and self.idle():
            self.stopping = True
            self.background_task(self.stop())

    def emit(self, kind: str, /, **data: Any):
        handle = self.handle
        if handle is None or handle.closed:
            return
        for handler in list(handle.handlers):
            handler(event(kind, **data))

    def account(self, kind: str, /, **data: Any):
        """What its turns spent, and that they spend no more: told Chief of Staff whatever
        became of its handle (on_accounting), for what was spent is spent."""
        if callable(tell := self.options.get("on_accounting")):
            tell(kind, event(kind, **data).data)
        else:
            self.emit(kind, **data)

    # What Chief of Staff sends

    async def send(self, prompt: str, *, mode: str, attachments: list[Any] | None) -> str:
        content, text = self.content(prompt, attachments)
        if self.turn or self.starting:
            # Into the turn while one of its tools runs (Claude reads it with that tool's
            # result), where its version's check showed that it does.
            steerable = self.turn and self.running_calls and not self.aborting
            if mode == "immediate" and steerable and self.agent.steers:
                sent = Sent(content, text, kind="steer", shown=prompt)
                sent.steered = True
                await self.write(sent)
                return sent.uuid
            self.queue.append((content, text, prompt))  # Taken up once its turn ends.
            return ""
        sent = await self.start_turn(content, text, prompt)
        return sent.uuid

    def content(self, prompt: str, attachments: list[Any] | None) -> tuple[Any, str]:
        """A message as Claude takes it, and its text as Claude's gate reads it (text blocks
        joined by line). Each file attached reaches it: an image as an image; any other (or an
        image that cannot be read) as a marked line with its path."""
        images, others, unreadable = [], [], False
        for path, name in attachment_paths(attachments):
            kind = image_type(path)
            data = b""
            if kind:
                try:
                    data = Path(path).read_bytes()
                except OSError:
                    unreadable = True
            if not data:
                others.append((path, name))
                continue
            source = {"type": "base64", "media_type": kind, "data": base64.b64encode(data).decode()}
            images.append({"type": "image", "source": source})
        texts = [prompt, *([attached_text(others, unreadable)] if others else [])]
        if not images and not others:
            return prompt, prompt
        return [*({"type": "text", "text": text} for text in texts), *images], "\n".join(texts)

    async def start_turn(self, content: Any, text: str, prompt: str) -> Sent:
        """A turn on this message, once Chief of Staff admits it; refused, it raises why.
        Admitted but not begun, nothing of it spends. Stopped meanwhile (abort: while its
        process restarted, or while it waited to be admitted), it ends at Claude's gate before
        it begins, as a message stopped once written does (gate_sent), never admitted then."""
        aborts = self.aborts
        self.starting, admitted = True, False
        try:
            if self.process_effort != self.effort and not self.background:
                await self.restart()  # Claude takes a thinking effort as its process starts.
            if self.aborts == aborts:
                admitted = True
                if why := await self.agent.admit_turn(self.handle_for_admission()):
                    admitted = False
                    raise RuntimeError(why)
            sent = Sent(content, text, kind="turn", shown=prompt)
            # Written now, nothing awaited since these checks: a Stop after them marks it as
            # written (abort), and a turn admitted before a change that makes this worker
            # ineligible never begins after it.
            if self.aborts != aborts:
                sent.stopped = True
            elif not self.agent.begins_turn(self.handle_for_admission()):
                raise RuntimeError("Chief of Staff's settings changed; send it again.")
            await self.write(sent)
            return sent
        except BaseException:
            self.starting = False
            if admitted:
                self.spent()
            if self.turn is None:
                self.queue.clear()  # Given back: Delivery sends what it holds again.
            raise

    def handle_for_admission(self) -> ClaudeSession:
        return self.handle or ClaudeSession(self)

    async def write(self, sent: Sent):
        if self.client is None or (not self.alive() and not sent.probe):
            raise ConnectionResetError("Claude Code is not running.")
        self.written[sent.uuid] = sent
        self.at_gate.append(sent)
        message = {
            "type": "user",
            "message": {"role": "user", "content": sent.content},
            "parent_tool_use_id": None,
            "session_id": "default",
            "uuid": sent.uuid,
        }

        async def one():
            yield message

        try:
            await self.client.query(one())
        except Exception as exc:
            self.written.pop(sent.uuid, None)
            if sent in self.at_gate:
                self.at_gate.remove(sent)
            raise ConnectionResetError(f"Claude Code is not running: {exc}") from None

    async def restart(self):
        """Its process ends, and another resumes its session (with what it starts with now)."""
        if self.agent.workers.get(self.worker) is not self:
            return
        self.opening += 1
        try:
            await self.stop()
            self.client = self.query = None
            self.process_effort = self.effort
            self.resume = True
            self.reset_turn_state()
            self.starting = True  # The turn it restarts for.
            await self.open()
            self.on_end = self.agent.ending(self.worker, self)
            if self.agent.on_start:
                self.agent.on_start()
        finally:
            self.opening -= 1

    def reset_turn_state(self):
        self.turn, self.starting, self.aborting, self.unshown = None, False, False, ""
        self.written.clear()
        self.at_gate.clear()
        self.calls.clear()
        self.running_calls.clear()
        self.running_tasks.clear()
        self.background.clear()
        self.pending_text = None
        self.allowed.clear()
        self.servers = None

    async def abort(self) -> None:
        """Its turn ends; what it left running goes on until stopped (stop_background). What
        waited for the turn's end, or was written and not yet taken up, is dropped (Delivery
        sends it again with its next message)."""
        self.queue.clear()
        self.aborts += 1  # A turn starting now (start_turn) is stopped before it begins.
        for sent in self.written.values():
            sent.stopped = True
        if self.turn and self.query is not None:
            self.aborting = True
            with suppress(Exception):
                await asyncio.wait_for(self.query.interrupt(), REQUEST_SECONDS)

    def idle(self) -> bool:
        """Nothing of it runs: no turn (nor one starting), no command it left running."""
        return not (self.turn or self.starting or self.background)

    def usage_pending(self) -> bool:
        """Whether something of it may still spend (a turn whose result has not come)."""
        return bool(self.turn)

    async def set_model(self, model: str, effort: str):
        """For its next turn and those after it: a model at once (Claude switches between
        turns), a thinking effort in the process its next turn starts in."""
        model = "" if model in {"", "auto", "default"} else model
        if model != self.model:
            self.model = model
            if self.query is not None and self.alive():
                with suppress(Exception):
                    await asyncio.wait_for(self.query.set_model(model or None), REQUEST_SECONDS)
        self.effort = effort

    async def get_events(self) -> list[Any]:
        """Its conversation, from Claude's transcript: what it received and what it said."""
        from claude_agent_sdk import get_session_messages

        messages = await asyncio.to_thread(get_session_messages, self.session_id, self.cwd)
        found = []
        for message in messages:
            content = mapping(message.message).get("content")
            if message.type == "user":
                if isinstance(content, list) and any(
                    mapping(block).get("type") == "tool_result" for block in content
                ):
                    continue
                text = owners_text(content)
                if text and text != PROBE and not text.startswith(NOTIFICATION):
                    found.append(event("user.message", content=text))
            elif message.type == "assistant" and (text := text_of(content)):
                found.append(event("assistant.message", content=text))
        return found

    def running_background(self) -> int:
        return len(self.background)

    async def stop_background(self) -> int:
        """Ends the commands its turns left running; how many it ended."""
        stopping = 0
        for task_id in list(self.background):
            if self.query is None:
                break
            try:
                await asyncio.wait_for(self.query.stop_task(task_id), REQUEST_SECONDS)
                stopping += 1
            except Exception as exc:
                if "not found" in str(exc).lower() or "not running" in str(exc).lower():
                    self.task_ended(task_id, "stopped", "")
                else:
                    logger.warning("Claude could not stop task %s: %s", task_id, exc)
        return stopping

    def take_queued(self) -> int:
        taken, self.queue = len(self.queue), []
        return taken

    def queue_holds(self) -> bool:
        waiting = any(not s.stopped and not s.probe for s in self.written.values())
        return bool(self.queue) or waiting

    # Claude's messages, in the order it sent them

    async def read(self):
        if self.query is None:
            return
        try:
            observe = self.options.get("on_message")  # The check's: what Claude says.
            async for message in self.query.receive_messages():
                if not isinstance(message, dict):
                    continue
                if callable(observe):
                    observe(message)
                self.handling = True
                try:
                    await self.handle_message(message)
                except Exception:
                    logger.exception("Handling Claude's %s failed.", message.get("type"))
                finally:
                    self.handling = False
        except Exception as exc:
            if not self.stopping:
                logger.warning("Reading from Claude failed for %s: %s", self.worker, exc)

    async def caught_up(self):
        """What Claude said before a request of its own is handled first (its requests come at
        once, its messages in order)."""
        query = self.query
        receive = getattr(query, "_message_receive", None)
        deadline = time.monotonic() + CAUGHT_UP_SECONDS
        while time.monotonic() < deadline:
            try:
                waiting = receive.statistics().current_buffer_used if receive else 0
            except Exception:
                waiting = 0
            if not waiting and not self.handling:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0)

    async def handle_message(self, message: dict):
        kind = message.get("type")
        if kind == "stream_event":
            self.stream_event(message)
        elif kind == "assistant":
            self.assistant(message)
        elif kind == "user":
            self.user(message)
        elif kind == "result":
            await self.result(message)
        elif kind == "system":
            self.system(message)
        elif kind == "rate_limit_event":
            self.agent.limit_seen(mapping(message.get("rate_limit_info")))
        elif kind == "conversation_reset":
            # Its conversation and totals start over: so does its count.
            self.tally = Tally()
            self.restored, self.told_usage = counts({}), False
            self.epoch = uuid.uuid4().hex
        elif kind == "command_lifecycle" and message.get("state") == "cancelled":
            sent = self.written.get(str(message.get("command_uuid") or ""))
            if sent and sent in self.at_gate:
                # Dropped before the gate saw it: never delivered (Delivery sends it again).
                self.at_gate.remove(sent)
                del self.written[sent.uuid]

    def system(self, message: dict):
        subtype = message.get("subtype")
        if subtype == "init":
            self.init(message)
        elif subtype == "task_started":
            task_id = str(message.get("task_id") or "")
            if task_id:
                self.known_tasks.add(task_id)
                self.running_tasks[task_id] = message
        elif subtype == "task_updated":
            patch = mapping(message.get("patch"))
            status = str(patch.get("status") or "")
            task_id = str(message.get("task_id") or "")
            if status in {"killed", "stopped"}:
                self.task_ended(task_id, status, "")
            elif status in TERMINAL:
                # Its notice, which says its exit code, follows: waited for a moment.
                loop = asyncio.get_running_loop()
                loop.call_later(NOTICE_SECONDS, self.task_ended, task_id, status, "")
        elif subtype == "task_notification":
            self.task_ended(
                str(message.get("task_id") or ""),
                str(message.get("status") or ""),
                str(message.get("summary") or ""),
            )
        elif subtype == "informational" and message.get("tag") == "hook_callback_failed":
            # Blocked, as a hook that fails is (the check makes one fail on purpose).
            log = logger.debug if self.checking else logger.warning
            log("A hook of Chief of Staff's failed for %s (blocked).", self.worker)

    def init(self, message: dict):
        """Each turn starts with what the CLI runs it with: it must still be what Chief of
        Staff set."""
        problems = []
        if message.get("permissionMode") != "default":
            problems.append(f"permission mode {message.get('permissionMode')}")
        servers = {
            mapping(s).get("name"): mapping(s).get("status")
            for s in message.get("mcp_servers") or []
        }
        if servers.get("chief") != "connected":
            problems.append("Chief of Staff's tools not connected")
        if message.get("session_id") not in {None, self.session_id}:
            problems.append("another session")
        version = message.get("claude_code_version")
        if version and self.agent.cli_version and version != self.agent.cli_version:
            problems.append(f"version {version}")
        if problems:
            self.stop_worker(
                "Stopped: Claude did not run this worker with the settings Chief of Staff's "
                f"checks need ({', '.join(problems)})."
            )
        if model := message.get("model"):
            self.current_model = str(model)

    def stream_event(self, message: dict):
        streamed = mapping(message.get("event"))
        kind = streamed.get("type")
        if kind == "message_start":
            response = mapping(streamed.get("message"))
            self.streaming = str(response.get("id") or "")
            if model := response.get("model"):
                self.current_model = str(model)
            self.count(self.streaming, response.get("usage"))
        elif kind == "message_delta":
            self.count(self.streaming, streamed.get("usage"))
            stop = mapping(streamed.get("delta")).get("stop_reason")
            if self.pending_text and self.pending_text[0] == self.streaming:
                self.tell_text(narration=stop == "tool_use")

    def count(self, key: str, usage: Any):
        """A response's usage as it comes: what the worker spent so far."""
        if not key or not isinstance(usage, dict):
            return
        if self.tally.response(key, usage):
            self.tell_usage()

    def tell_usage(self):
        self.last_told = self.tally.total()
        self.account(
            "agent.usage",
            thread_id=self.session_id,
            turn_id=self.turn or "",
            total=self.tally.total(),
            last={},
            model=self.current_model,
            child=False,
            epoch=self.epoch,
            restored=None if self.told_usage else (self.restored or counts({})),
        )
        self.told_usage = True

    def assistant(self, message: dict):
        response = mapping(message.get("message"))
        key = str(response.get("id") or "")
        parent = str(message.get("parent_tool_use_id") or "")
        if message.get("error"):
            return  # Claude's words for a failed request: its turn's error says them.
        if self.turn is None and not parent and not self.checking:
            # A response no turn of Chief of Staff's admitted: what passed its gate unseen.
            self.unadmitted()
            return
        self.count(key, response.get("usage"))
        extra = {"parent_tool_call_id": parent} if parent else {}
        for block in response.get("content") or []:
            block = mapping(block)
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                if parent:
                    self.emit("assistant.message", content=block["text"], **extra)
                    continue
                if self.pending_text and self.pending_text[0] != key:
                    self.tell_text(narration=False)
                self.pending_text = (key, str(block["text"]))
            elif block.get("type") == "tool_use":
                if not parent and self.pending_text and self.pending_text[0] == key:
                    self.tell_text(narration=True)
                self.call_started(block, parent)

    def tell_text(self, narration: bool):
        if not self.pending_text:
            return
        _, text = self.pending_text
        self.pending_text = None
        tools = [SimpleNamespace()] if narration else None
        self.emit("assistant.message", content=text, tool_requests=tools)

    def call_started(self, block: dict, parent: str):
        call_id, name = str(block.get("id") or ""), str(block.get("name") or "")
        shown, arguments = event_tool(name, block.get("input"))
        self.calls[call_id] = (name, parent)
        if not parent:
            self.running_calls.add(call_id)
        self.emit(
            "tool.execution_start",
            tool_call_id=call_id,
            tool_name=shown,
            arguments=arguments,
            **({"parent_tool_call_id": parent} if parent else {}),
        )

    def user(self, message: dict):
        content = mapping(message.get("message")).get("content")
        if isinstance(content, list) and any(
            mapping(block).get("type") == "tool_result" for block in content
        ):
            self.call_results(message, content)
            return
        sent = self.written.pop(str(message.get("uuid") or ""), None)
        if sent is None or sent.probe:
            return  # Not Chief of Staff's (an interrupt's mark, a wakeup's notice): never shown.
        if sent in self.at_gate:
            self.at_gate.remove(sent)
        self.emit("user.message", content=sent.shown, delivery=Taken(sent))
        if self.unshown:
            self.show_turn()

    def show_turn(self):
        turn, self.unshown = self.unshown, ""
        self.emit("assistant.turn_start", turn_id=turn)

    def call_results(self, message: dict, content: list):
        structured = message.get("tool_use_result")
        meta = {
            mapping(entry).get("id"): mapping(entry)
            for entry in message.get("tool_result_meta") or []
        }
        results = [
            mapping(block) for block in content if mapping(block).get("type") == "tool_result"
        ]
        for block in results:
            call_id = str(block.get("tool_use_id") or "")
            found = self.calls.pop(call_id, None)
            self.running_calls.discard(call_id)
            self.allowed.pop(call_id, None)
            if found is None:
                continue
            name, parent = found
            extra = {"parent_tool_call_id": parent} if parent else {}
            text = result_text(block.get("content"))
            failed = bool(block.get("is_error"))
            single = structured if len(results) == 1 else None
            never_ran = mapping(meta.get(call_id)).get("non_execution_kind") == "permission-rule"
            stopped = mapping(meta.get(call_id)).get("non_execution_kind") == "user-rejected"
            if name == "Bash" and not failed:
                found_id = mapping(single).get("backgroundTaskId") or (
                    match.group(1) if (match := BACKGROUND_ID.search(text)) else ""
                )
                if found_id:
                    # Shaped as Copilot says it, so receipts know the command runs on.
                    self.background[str(found_id)] = call_id
                    self.known_tasks.add(str(found_id))
                    self.emit(
                        "tool.execution_complete",
                        tool_call_id=call_id,
                        success=True,
                        result=SimpleNamespace(
                            content=f"<command started in background with shellId: {found_id}>"
                        ),
                        **extra,
                    )
                    continue
            error = SimpleNamespace(message=text or "Refused.") if never_ran else None
            shown = "" if never_ran or stopped else text
            done: dict[str, Any] = {}
            if name in SHELL_TOOLS:
                code = None if never_ran or stopped else bash_exit(text, failed, single)
                done["shell_execution"] = (
                    SimpleNamespace(exit_code=code) if code is not None else None
                )
            self.emit(
                "tool.execution_complete",
                tool_call_id=call_id,
                success=not failed,
                result=SimpleNamespace(content=shown),
                error=error,
                **done,
                **extra,
            )

    def task_ended(self, task_id: str, status: str, summary: str):
        """One of the CLI's tasks ended: a command its turn left running settles its receipt
        (with its exit code where its notice says it), once."""
        self.running_tasks.pop(task_id, None)
        if task_id not in self.background or task_id in self.settled_tasks:
            return
        if status not in TERMINAL:
            return
        self.settled_tasks.add(task_id)
        del self.background[task_id]
        code = None
        if status in {"completed", "failed"} and (found := NOTICE_CODE.search(summary)):
            code = int(found.group(1))
        kind = SimpleNamespace(type="shell_completed", shell_id=task_id, exit_code=code)
        self.emit("system.notification", kind=kind)
        self.after_background()

    def after_background(self):
        """A command its turn left running ended: once nothing is left and no turn runs, its
        session is idle."""
        if self.turn is None and not self.starting and not self.background:
            self.emit("session.idle")
            if self.handle is None or self.handle.closed:
                self.release_if_idle()

    async def result(self, message: dict):
        # A result of no model request (num_turns 0) ends no turn: a prompt the gate blocked, or
        # a task's notice that woke nothing (a stopped command's). Nothing ran, nothing was
        # spent; its totals are what this process restored on (its probe's answer).
        quiet = message.get("num_turns") == 0
        if self.turn is None or quiet:
            if self.probed and not self.probed.done() and self.turn is None:
                self.probed.set_result(model_totals(message.get("modelUsage")))
            if not quiet:
                logger.warning(
                    "Claude ended a turn of %s that was not admitted (%s).",
                    self.worker,
                    message.get("subtype"),
                )
            return
        self.tally.result(message.get("modelUsage"))
        self.tell_text(narration=False)
        if self.tally.total() != self.last_told:
            self.tell_usage()
        await self.end_turn(message)

    async def end_turn(self, message: dict):
        aborted = self.aborting
        self.settle_calls()
        self.turn = None
        self.aborting = False
        self.unshown = ""
        self.spent()
        if self.handle is None or self.handle.closed:
            self.queue.clear()  # Let go of: what it was sent is sent again (Delivery).
        if aborted:
            self.queue.clear()
            self.emit("abort", reason="user initiated")
        elif message.get("is_error"):
            self.queue.clear()  # Given back: what it was sent is sent again (Delivery).
            self.emit("session.error", message=self.failure(message))
            self.agent.turn_failed(message)
            return
        elif self.queue and self.process_effort != self.effort and not self.background:
            # Its next turn starts in a new process (a new effort), which ends this task, the
            # one that reads its messages: started from a task of its own.
            self.starting = True
            self.agent.apart(self.next_turn_apart())
            return
        elif self.queue and await self.next_turn():
            return
        self.rest()

    def rest(self):
        """No turn: what it left running holds the session; else it is idle."""
        if self.background:
            self.emit("assistant.idle")  # What it left running holds session.idle.
        else:
            self.emit("session.idle")
            if self.handle is None or self.handle.closed:
                self.release_if_idle()

    def failure(self, message: dict) -> str:
        said = message.get("result")
        errors = [str(e) for e in message.get("errors") or [] if e]
        text = said if isinstance(said, str) and said.strip() else "; ".join(errors)
        return (text or f"Claude's turn failed ({message.get('subtype') or 'error'}).")[:1500]

    def settle_calls(self):
        """Its turn ended with these calls not ended: they ended without a result."""
        for call_id in list(self.running_calls):
            name, parent = self.calls.pop(call_id, ("", ""))
            self.running_calls.discard(call_id)
            self.emit(
                "tool.execution_complete",
                tool_call_id=call_id,
                success=False,
                result=SimpleNamespace(content=""),
                **({"shell_execution": None} if name in SHELL_TOOLS else {}),
                **({"parent_tool_call_id": parent} if parent else {}),
            )

    def spent(self):
        """Nothing of it spends now: what its turns spent has all been said."""
        if self.turn is None and not self.starting:
            self.account("agent.settled")

    async def next_turn(self) -> bool:
        """The next message it queued, as a turn of its own once admitted; refused, what it
        queued is given back (Chief of Staff sends it again)."""
        if not self.queue:
            return False  # Stopped meanwhile.
        content, text, prompt = self.queue.pop(0)
        try:
            await self.start_turn(content, text, prompt)
        except Exception as exc:
            logger.info("%s's next turn did not start: %s", self.worker, exc)
            self.queue.clear()
            return False
        return True

    async def next_turn_apart(self):
        if not await self.next_turn():
            self.starting = False
            self.spent()
            self.rest()

    def unadmitted(self):
        """A model response with no turn of Chief of Staff's admitted: something passed its gate
        unseen. It is stopped, said, and the version is marked as failing its gate."""
        logger.warning("Claude answered a prompt Chief of Staff did not admit (%s).", self.worker)
        self.agent.gate_failed(
            "a turn began that Chief of Staff's gate did not admit; no Claude worker runs on "
            "this version"
        )
        self.stop_worker(
            "Stopped: Claude began a turn Chief of Staff did not admit, which its checks do not "
            "allow."
        )

    # Claude's requests

    async def prompt_gate(self, data: dict, tool_use_id: str | None, context: Any) -> dict:
        """Every prompt, before the model sees it: one answer for each kind (5.12.6)."""
        await self.caught_up()
        prompt = str(mapping(data).get("prompt") or "")
        sent = self.at_gate_for(prompt)
        if sent is not None:
            return await self.gate_sent(sent)
        if prompt.startswith(NOTIFICATION):
            return await self.gate_wakeup(prompt)
        return self.block(
            "Chief of Staff delivers only its own messages to this worker.",
            notice="A message Chief of Staff could not place was not delivered.",
        )

    def at_gate_for(self, prompt: str) -> Sent | None:
        for exact in (True, False):
            for sent in self.at_gate:
                if sent.text == prompt or (not exact and sent.text.strip() == prompt.strip()):
                    self.at_gate.remove(sent)
                    return sent
        return None

    async def gate_sent(self, sent: Sent) -> dict:
        if sent.probe:
            return self.block("Chief of Staff's own count, not a request.")
        if sent.stopped:
            self.written.pop(sent.uuid, None)
            if sent.kind == "turn" and self.starting and not self.turn:
                # Stopped before Claude began it: admitted, it spent nothing.
                self.starting = False
                self.spent()
                self.emit("abort", reason="user initiated")
                self.emit("session.idle")
            return self.block("Stopped by Chief of Staff.")
        if self.turn:
            if sent.kind == "turn":
                # Admitted while a turn the worker started by itself (gate_wakeup) began: it
                # is part of that turn.
                self.starting = False
            sent.steered = sent.kind == "steer"
            return {}  # Into the turn in progress.
        if self.starting and sent.kind == "turn":
            self.starting = False
            self.begin_turn(f"t-{sent.uuid}", show=False)
            return {}
        # Left over from a turn that ended (a steering message it had not taken up): a turn
        # of its own, once admitted; else given back (Chief of Staff sends it again).
        sent.steered = False
        why = await self.agent.admit_turn(self.handle_for_admission())
        if not why and sent.stopped:
            self.spent()
            why = "Stopped by Chief of Staff."
        elif not why and not self.agent.begins_turn(self.handle_for_admission()):
            self.spent()
            why = "Chief of Staff's settings changed."
        if why:
            self.written.pop(sent.uuid, None)
            return self.block(why)
        self.begin_turn(f"t-{sent.uuid}", show=False)
        return {}

    async def gate_wakeup(self, prompt: str) -> dict:
        """A background command's end wakes the worker: within a turn in progress (or one
        admitted and starting), part of it; else a turn it starts by itself, once admitted
        (counted to its task's run before it spends)."""
        if self.turn:
            return {}
        if self.starting and any(s.kind == "turn" and not s.stopped for s in self.at_gate):
            # Its admitted turn's message, written, waits behind this notice: the turn begins
            # with it. (One still waiting to be admitted, or restarting, is not: this one is
            # admitted on its own.)
            self.starting = False
            self.begin_turn(f"t-{uuid.uuid4().hex[:12]}", show=True)
            return {}
        found = TASK_ID.search(prompt)
        if not found or found.group(1).strip() not in self.known_tasks:
            return self.block("Chief of Staff did not see this task start.")
        why = await self.agent.admit_turn(self.handle_for_admission(), woken=True)
        if not why and not self.agent.begins_turn(self.handle_for_admission()):
            self.spent()
            why = "Chief of Staff's settings changed."
        if why:
            return self.block(why)
        self.wakeups += 1
        self.begin_turn(f"w-{self.wakeups}-{uuid.uuid4().hex[:8]}", show=True)
        return {}

    def begin_turn(self, turn: str, *, show: bool):
        self.turn = turn
        self.turns.add(turn)
        self.aborting = False
        if show:
            self.unshown = turn
            self.show_turn()
        else:
            self.unshown = turn  # Shown once its message is (as Copilot shows a turn).

    def block(self, why: str, notice: str = "") -> dict:
        if notice:
            self.emit("agent.notice", text=notice)
        return {"decision": "block", "reason": why}

    async def pre_tool_use(self, data: dict, tool_use_id: str | None, context: Any) -> dict:
        """Every tool call, before it runs: allowed or refused, never left to anything else."""
        await self.caught_up()
        data = mapping(data)
        name = str(data.get("tool_name") or "")
        call = str(data.get("tool_use_id") or tool_use_id or "")
        if self.turn is None and not self.checking:
            why = "Chief of Staff did not start this turn."
        else:
            why = await self.decide(name, mapping(data.get("tool_input")), call)
        self.allowed[call] = why
        if not why and name == "AskUserQuestion":
            # The owner answers, however long that takes: through can_use_tool, which has no
            # hook's time limit.
            decision = {"permissionDecision": "ask"}
        elif why:
            decision = {"permissionDecision": "deny", "permissionDecisionReason": why}
        else:
            decision = {"permissionDecision": "allow"}
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", **decision}}

    async def can_use_tool(self, name: str, data: dict, context: Any) -> Any:
        """What still asks after PreToolUse allowed it (the owner's ask rules, a question for
        the owner): the same answer. No rule is ever saved."""
        from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

        call = str(getattr(context, "tool_use_id", "") or "")
        if call in self.allowed:
            why = self.allowed[call]
        else:
            await self.caught_up()
            why = await self.decide(name, mapping(data), call)
            self.allowed[call] = why
        if why:
            return PermissionResultDeny(message=why)
        if name == "AskUserQuestion":
            answers = await self.ask_owner(mapping(data))
            if answers is None:
                return PermissionResultDeny(message="The owner did not answer; go on without it.")
            return PermissionResultAllow(updated_input={**mapping(data), "answers": answers})
        return PermissionResultAllow()

    async def decide(self, name: str, arguments: dict, call: str) -> str:
        """ "" when Chief of Staff allows this call, else why not."""
        if why := WITHHELD.get(name):
            self.emit("agent.notice", text=f"Refused Claude's {name}: {why}")
            return why
        if name in FREE_TOOLS:
            return ""
        request = await self.permission_request(name, arguments, call)
        if request is None:
            if self.ask_first:
                return (
                    f"Chief of Staff doesn't know Claude's {name} tool yet, so a worker that "
                    "asks first can't use it."
                )
            return ""
        return await self.permitted(request)

    async def permission_request(self, name: str, arguments: dict, call: str) -> Any:
        """A call as Copilot's permission request has it (Events.on_permission reads it); None
        for a tool Chief of Staff does not know."""
        if name in SHELL_TOOLS:
            return SimpleNamespace(
                kind="shell",
                full_command_text=str(arguments.get(SHELL_TOOLS[name]) or ""),
                shell=bash_shell(),  # As the policy reads it (zsh: Tally.zsh).
                intention=str(arguments.get("description") or ""),
                commands=[],
                possible_paths=[],
                possible_urls=[],
                has_write_file_redirection=False,
                can_offer_session_approval=False,
                tool_call_id=call,
            )
        if name in WRITE_TOOLS:
            path = str(arguments.get(WRITE_TOOLS[name]) or "")
            resolved = joined(self.cwd, path) if path else ""
            return SimpleNamespace(
                kind="write",
                file_name=path,
                resolved_path=resolved,
                diff="",
                intention="",
                can_offer_session_approval=False,
                tool_call_id=call,
                tool_name=name,
            )
        if name in READ_TOOLS:
            path = arguments.get("file_path") or arguments.get("path") or ""
            return SimpleNamespace(kind="read", path=str(path), tool_call_id=call)
        if name in URL_TOOLS:
            url = arguments.get("url") or f"https://search/?q={arguments.get('query') or ''}"
            return SimpleNamespace(
                kind="url", url=str(url), intention=str(arguments.get("prompt") or ""),
                tool_call_id=call,
            )  # fmt: skip
        if name.startswith("mcp__"):
            server, _, tool = name[5:].partition("__")
            if server == "chief":
                return SimpleNamespace(kind="custom-tool", tool_name=tool, tool_call_id=call)
            known = await self.mcp_server(server)
            annotations = next(
                (
                    mapping(mapping(entry).get("annotations"))
                    for entry in known.get("tools") or []
                    if mapping(entry).get("name") == tool
                ),
                {},
            )
            # A server it cannot tell apart counts as GitHub's (its writes are the owner's).
            github = not known or github_server(server, known.get("config"))
            return SimpleNamespace(
                kind="mcp",
                server_name=GITHUB_MCP if github else server,
                tool_name=tool,
                tool_title=tool,
                read_only=annotations.get("readOnly") is True,
                args=arguments,
                tool_call_id=call,
            )
        return None

    async def mcp_server(self, name: str) -> dict:
        """One of its MCP servers as Claude has it (its configuration, its tools' annotations);
        {} when Claude cannot say."""
        if self.servers is None or name not in self.servers:
            try:
                status = await asyncio.wait_for(self.query.get_mcp_status(), REQUEST_SECONDS)
                self.servers = {
                    str(mapping(entry).get("name")): mapping(entry)
                    for entry in mapping(status).get("mcpServers") or []
                }
            except Exception:
                logger.warning("Claude did not say what its MCP servers are.", exc_info=True)
                self.servers = self.servers or {}
        return self.servers.get(name, {})

    async def permitted(self, request: Any) -> str:
        check = self.options.get("on_permission_request")
        if not check:
            return "No permission check is set for this session."
        context = {"session_id": self.session_id, "sessionId": self.session_id}
        decision = check(request, context)
        if inspect.isawaitable(decision):
            decision = await decision
        if isinstance(decision, PermissionDecisionReject):
            return getattr(decision, "feedback", "") or "Refused."
        return ""

    async def ask_owner(self, data: dict) -> dict[str, str] | None:
        """Claude's AskUserQuestion, as the owner's card under Needs you (Asks.on_user_input):
        each question's answer, by its text; None when there is none."""
        ask = self.options.get("on_user_input_request")
        if not ask:
            return None
        context = {"session_id": self.session_id, "sessionId": self.session_id}
        answers = {}
        for question in data.get("questions") or []:
            question = mapping(question)
            text = str(question.get("question") or "")
            header = str(question.get("header") or "")
            choices = [
                str(mapping(option).get("label") or "")
                for option in question.get("options") or []
                if mapping(option).get("label")
            ]
            asked = ask(
                {
                    "question": f"{header}: {text}" if header else text,
                    "choices": choices,
                    "allowFreeform": True,
                },
                context,
            )
            if inspect.isawaitable(asked):
                asked = await asked
            reply = mapping(asked).get("answer")
            if reply:
                answers[text] = str(reply)
        return answers or None

    def chief_server(self) -> dict:
        """Chief of Staff's own tools, as an MCP server in this process, loaded upfront: their
        calls are this worker's (one process, one session)."""
        from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server

        tools = []
        for tool in self.tools.values():
            schema = dict(tool.parameters or {})
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            tools.append(
                SdkMcpTool(
                    name=tool.name,
                    description=tool.description or tool.name,
                    input_schema=schema,
                    handler=self.tool_handler(tool.name),
                )
            )
        server = create_sdk_mcp_server("chief", version=client_version(), tools=tools)
        return {**server, "alwaysLoad": True}

    def tool_handler(self, name: str) -> Callable[[dict], Awaitable[dict]]:
        async def handler(arguments: dict) -> dict:
            await self.caught_up()
            text, ok = await self.tool_call(name, arguments)
            return {"content": [{"type": "text", "text": text}], "is_error": not ok}

        return handler

    async def tool_call(self, name: str, arguments: dict) -> tuple[str, bool]:
        tool = self.tools.get(name)
        if not tool or not tool.handler:
            return f"Tool {name} is not available.", False
        if self.turn is None and not self.checking:
            return "Chief of Staff did not start this turn.", False
        call = next(
            (c for c, (n, _) in reversed(self.calls.items()) if n == f"mcp__chief__{name}"), ""
        )
        invocation = ToolInvocation(
            session_id=self.session_id, tool_call_id=call, tool_name=name, arguments=arguments
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
        return text, ok


class SessionInUseError(RuntimeError):
    """A session Claude already has (made before, its making not recorded): resumed instead."""


STOP_SECONDS = 40.0


class ClaudeAgent:
    """Claude Code for Chief of Staff's workers: a CLI process of each worker's own (more-agents
    5.12.2). Each starts with the worker's first turn or resume and ends once its session is let
    go of with nothing of it running; when one stops by itself, only its worker is affected
    (`ended`, Runtime.agent_process_ended)."""

    capabilities = CLAUDE

    def __init__(self, command: str, folder: Path | None = None):
        self.command = command
        # Where its launcher is written (Chief of Staff's data directory).
        self.folder = folder
        self.cli_version = ""
        self.auth: dict = {}
        self.checked: dict | None = None
        self.admit: Callable[..., Awaitable[str]] | None = None
        self.begins: Callable[[Any], bool] | None = None
        self.ended: Callable[[str, str, str], None] | None = None
        self.on_start: Callable[[], None] | None = None
        # Told when a plan limit changes, or when its gate was found missing a prompt.
        self.on_limit: Callable[[dict], None] | None = None
        self.on_refused: Callable[[str], None] | None = None
        self.workers: dict[str, ClaudeWorker] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.retired: set[ClaudeWorker] = set()
        # Workers' work started apart from their process's own tasks (ClaudeWorker.end_turn).
        self.started_apart: set[asyncio.Task] = set()
        self.running = False
        self.launcher: Path | None = None
        # The plan's limit as the stream last said it (status, window, reset), and a failure
        # of the last turn that says how it is signed in (until the next look).
        self.limit: dict = {}
        self.trouble = ""
        self.gate_failure = ""
        self.key = ""

    def notes_for(self, ask_first: bool) -> list[str]:
        """What a worker of it does differently from a Copilot worker, a short line each."""
        notes = ["Its subagents, worktrees and schedules are off for now"]
        return [*notes, "Your Claude hooks are off for it"] if ask_first else notes

    async def admit_turn(self, session: Any, woken: bool = False) -> str:
        """Chief of Staff's admission of a turn ("" when admitted): `woken`, one the worker
        starts by itself as a command it left running ended (Runtime.admit_turn)."""
        if not self.admit:
            return ""
        return await (self.admit(session, woken=True) if woken else self.admit(session))

    def begins_turn(self, session: Any) -> bool:
        return self.begins(session) if self.begins else True

    async def start(self) -> None:
        """What each worker's process needs (the CLI, its version, the extra, the sign-in),
        checked once now: a Claude that cannot run says why here."""
        if why := sdk_refused():
            raise RuntimeError(why)
        path = shutil.which(self.command)
        if not path:
            raise RuntimeError(f"Claude Code '{self.command}' not found. Install it and sign in.")
        self.command = path
        self.cli_version = await claude_version(path, self.folder)
        if why := claude_refused(self.cli_version):
            raise RuntimeError(why)
        self.auth = await auth_status(path, self.folder)
        if not self.auth.get("loggedIn"):
            raise RuntimeError(NOT_SIGNED_IN)
        self.key = await asyncio.to_thread(check_key, path, self.cli_version)
        if self.folder is None:
            raise RuntimeError("Chief of Staff has no folder for Claude's launcher.")
        self.launcher = await asyncio.to_thread(write_launcher, self.folder, path)
        await asyncio.to_thread(clear_private, self.launcher)
        self.trouble = ""
        self.running = True

    def apart(self, coroutine: Awaitable[Any]):
        task = asyncio.ensure_future(coroutine)
        self.started_apart.add(task)
        task.add_done_callback(self.started_apart.discard)

    async def stop(self) -> None:
        self.running = False
        for task in list(self.started_apart):
            task.cancel()
        workers, self.workers = [*self.workers.values(), *self.retired], {}
        self.retired = set()
        await asyncio.gather(*(worker.stop() for worker in workers), return_exceptions=True)

    def alive(self) -> bool:
        return self.running

    def epochs(self) -> set[str]:
        return {worker.epoch for worker in self.workers.values() if worker.alive()}

    def processes(self) -> dict[int, str]:
        found: dict[int, str] = {}
        if running := [worker for worker in list(self.workers.values()) if worker.alive()]:
            table = process_table()
            for worker in running:
                found |= {
                    pid: command
                    for pid, command in worker.processes(table).items()
                    if table.get(pid, (0, ""))[1] == command
                }
        return found

    async def check_key(self) -> str:
        return self.key

    async def version(self) -> str:
        return self.cli_version

    async def get_auth_status(self) -> Any:
        return SimpleNamespace(
            isAuthenticated=bool(self.auth.get("loggedIn")), authType=signed_in(self.auth)
        )

    async def about(self) -> dict:
        """Its version, how it is signed in (never who), its models, and its plan's limit."""
        if self.trouble:
            raise RuntimeError(self.trouble)
        return {
            "version": self.cli_version,
            "account": signed_in(self.auth),
            "models": await self.list_models(),
            "check_key": self.key,
            "limit": dict(self.limit),
        }

    async def list_models(self) -> list[Any]:
        return list(LISTED.get(self.cli_version) or alias_models())

    async def list_sessions(self) -> list[Any]:
        return []

    async def get_session_metadata(self, session_id: str) -> Any:
        from claude_agent_sdk import get_session_info

        found = await asyncio.to_thread(get_session_info, dashed(session_id))
        if found is None:
            return None
        return SimpleNamespace(session_id=dashed(session_id), cwd=getattr(found, "cwd", None))

    @property
    def steers(self) -> bool:
        """Whether a message sent during a turn reaches it, as its version's check showed (else
        it waits for the turn's end)."""
        return (self.checked or {}).get("steering", True) is not False

    @property
    def ask_first_refused(self) -> str:
        return ask_first_refusal(self.cli_version, self.checked)

    @property
    def acting_refused(self) -> str:
        if self.gate_failure:
            return self.gate_failure
        if why := (self.checked or {}).get("acting"):
            return why
        return self.limit_refusal()

    def limit_refusal(self) -> str:
        """While the plan's limit is reached (until its reset): why no worker runs. Nothing
        tries again by itself."""
        if self.limit.get("status") != "rejected":
            return ""
        line = agents.limit_line(self.limit)
        return f"{line} Tell the worker to go on after that." if line else ""

    def limit_seen(self, info: dict):
        """The plan's limit as a worker's stream said it (nothing about the account)."""
        status = str(info.get("status") or "")
        if not status:
            return
        resets = info.get("resetsAt")
        found = {
            "status": status,
            "window": str(info.get("rateLimitType") or ""),
            "resets_at": int(resets) if isinstance(resets, int | float) else 0,
        }
        if found != self.limit:
            self.limit = found
            if self.on_limit:
                with suppress(Exception):
                    self.on_limit(dict(found))

    def turn_failed(self, message: dict):
        """A turn that failed for how Claude is signed in: Settings says so until its next look."""
        said = str(message.get("result") or "")
        if any(
            word in said.lower() for word in ("invalid api key", "please run /login", "billing")
        ):
            self.trouble = said[:300]

    def gate_failed(self, why: str):
        self.gate_failure = (
            f"Chief of Staff's check of Claude Code {self.cli_version} found that {why}."
        )
        if self.on_refused:
            with suppress(Exception):
                self.on_refused(self.gate_failure)

    async def check(self, folder: Path) -> dict:
        from . import claude_check

        return await claude_check.check(self, folder)

    # Workers' processes

    async def open_worker(self, session_id: str, options: dict, *, resume: bool) -> ClaudeSession:
        if not self.running or self.launcher is None:
            raise RuntimeError("Claude Code is not running for Chief of Staff.")
        if why := self.acting_refused:
            raise RuntimeError(why)
        if options.get("ask_first") and (why := self.ask_first_refused):
            raise RuntimeError(why)
        name = options.get("worker") or session_id
        lock = self.locks.setdefault(name, asyncio.Lock())
        async with lock:
            worker = self.workers.get(name)
            if worker and worker.alive() and not worker.stopping and not worker.retiring:
                return worker.attach(options)
            if worker is not None:
                with suppress(Exception):
                    await asyncio.shield(worker.retiring or worker.stop())
            worker = ClaudeWorker(
                self, name, dashed(session_id), options, resume=resume, launcher=self.launcher
            )
            worker.opening += 1
            try:
                try:
                    await worker.open()
                except SessionInUseError:
                    worker = ClaudeWorker(
                        self, name, dashed(session_id), options, resume=True,
                        launcher=self.launcher,
                    )  # fmt: skip
                    worker.opening += 1
                    await worker.open()
                worker.on_end = self.ending(name, worker)
                self.workers[name] = worker
                if self.on_start:
                    self.on_start()
                return worker.attach(options)
            finally:
                worker.opening -= 1

    def ending(self, name: str, worker: ClaudeWorker) -> Callable[[bool], None]:
        return lambda by_itself: self.process_ended(name, worker, by_itself)

    def process_ended(self, name: str, worker: ClaudeWorker, by_itself: bool):
        if self.workers.get(name) is worker and not worker.opening:
            del self.workers[name]
        self.retired = {w for w in self.retired if w.task and not w.task.done()}
        if self.ended:
            why = worker.refusal or ("crash" if by_itself else "")
            try:
                self.ended(name, worker.epoch, why)
            except Exception:
                logger.exception("Ending %s's Claude process failed.", name)

    async def create_session(self, *, session_id: str, name: str, **options: Any) -> ClaudeSession:
        return await self.open_worker(session_id, options, resume=False)

    async def resume_session(self, session_id: str, **options: Any) -> ClaudeSession:
        return await self.open_worker(session_id, options, resume=True)

    async def retire(self, worker: str, seconds: float = RETIRE_SECONDS):
        """A worker whose settings changed: its process ends once nothing of it runs (its turn
        just stopped), `seconds` at the latest; its next message opens it in a new one."""
        process = self.workers.get(worker)
        if process is None:
            return
        if process.retiring is None:
            process.retiring = asyncio.ensure_future(self.retiring(process, seconds))
        await asyncio.shield(process.retiring)

    async def retiring(self, process: ClaudeWorker, seconds: float):
        deadline = time.monotonic() + seconds
        while True:  # Polled: its session says nothing when it comes to rest.
            if not process.alive() or process.idle() or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.1)
        await process.stop()

    async def archive(self, session_id: str):
        return None  # Claude keeps its sessions' transcripts (decision 28).

    async def mcp_sign_in_url(self, session: Any, server: str) -> str:
        return ""

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


def claude_backend(command: str = "claude", folder: Path | None = None) -> ClaudeAgent:
    return ClaudeAgent(shutil.which(command) or command, folder)
