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
  as Copilot's requests do (each file an edit writes, a move's destination too). With Ask first,
  ChatGPT's apps are off for the thread, as read back. A refusal's reason reaches the model as a
  note steered into its turn.
- Items are events with Copilot's names: a `userMessage` is the message's acknowledgement
  (user.message), commands are `bash` with their exact exit codes, each changed file an edit,
  Chief of Staff's own tools (`dynamicTools`) and MCP calls tool calls. A child thread (a
  subagent) shows as one `task` call of its worker, its calls to Chief of Staff's tools are its
  worker's, and its usage is its worker's.
- A command the turn left running is a background terminal: the turn's end is held
  (assistant.idle) until it ends, which settles its receipt (system.notification).
- Messages sent while a turn runs steer into it (`turn/steer`) or wait in the adapter's queue;
  the next turn on them starts only once Chief of Staff admits it (`admit`, Runtime.admit_turn).
- Usage arrives as cumulative counters per thread (`agent.usage` events, in tokens, with this
  process as their epoch and what a resumed thread restored), and `agent.settled` once nothing
  of the session spends (Runtime.record_agent_usage keeps the ledger).

A repository's own `.codex/` configuration loads only where Codex itself would trust it for the
owner (trust_levels): each thread is told its folders' trust explicitly, because Codex otherwise
records a folder it starts a writable thread in as trusted in the owner's config.toml (0.167.2).
Each Codex version is checked once, offline, before its workers run (check, codex_check.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import shlex
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from copilot.tools import ToolInvocation

from . import codex_check
from .backend import Capabilities, descendants, end_processes, process_table
from .envelope import inert, wrap
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
# The Codex CLI versions this release was tested with (the contract test, live runs): older
# ones (and one whose version cannot be told) do not run; workers that ask first run only up to
# the next minor version, whose every kind of action was shown to ask Chief of Staff first.
# Newer ones run workers that act on their own.
OLDEST_CODEX = "0.167.2"
# How long Settings waits for a Codex started to say what it is (look).
LOOK_SECONDS = 30
ASK_FIRST_BEFORE = "0.168.0"
# How long a request to the app-server may take (seconds), and one that starts a thread.
REQUEST_TIMEOUT = float(os.environ.get("CHIEFOFSTAFF_CODEX_TIMEOUT", "60"))
# How long the check of a Codex version may take, all told (seconds).
CHECK_TIMEOUT = 90.0
# What Codex says of an MCP server that needs the owner to sign in (codex-mcp startup.rs).
NOT_SIGNED_IN = re.compile(r"MCP server (?:is not logged in|requires OAuth reauthentication)")
# How often a worker's process is looked at to see whether its supervisor still runs.
WATCH_SECONDS = 0.5
# How long after a turn was stopped a command it was starting may still show as running (Codex
# keeps it as a terminal of its thread, 0.167.2), and how often that is looked for meanwhile.
LATE_TERMINALS = 3.0
LATE_TERMINALS_EVERY = 0.2
# What Chief of Staff uses of Codex's app-server protocol: each request it sends with the fields
# it may give, the requests of Codex it answers, the news it follows. A version's check finds
# each in that version's own schema (`codex app-server generate-json-schema`, offline) before
# anything else; the tests keep it to what is sent.
CODEX_SENDS: dict[str, tuple[str, ...]] = {
    "initialize": ("clientInfo", "capabilities"),
    "configRequirements/read": (),
    "account/read": (),
    "model/list": ("cursor",),
    "config/read": ("cwd", "includeLayers"),
    "plugin/installed": ("cwds",),
    "plugin/read": ("marketplacePath", "pluginName"),
    "skills/extraRoots/set": ("extraRoots",),
    "experimentalFeature/list": ("threadId",),
    "thread/start": (
        "cwd",
        "model",
        "approvalPolicy",
        "approvalsReviewer",
        "sandbox",
        "config",
        "developerInstructions",
        "dynamicTools",
        "ephemeral",
    ),
    "thread/resume": (
        "threadId",
        "cwd",
        "model",
        "approvalPolicy",
        "approvalsReviewer",
        "sandbox",
        "config",
        "developerInstructions",
        "excludeTurns",
    ),
    "thread/read": ("threadId",),
    "thread/turns/list": ("threadId", "itemsView", "limit", "sortDirection"),
    "thread/name/set": ("threadId", "name"),
    "thread/inject_items": ("threadId", "items"),
    "thread/archive": ("threadId",),
    "thread/unsubscribe": ("threadId",),
    "thread/backgroundTerminals/list": ("threadId",),
    "thread/backgroundTerminals/clean": ("threadId",),
    "turn/start": ("threadId", "input", "clientUserMessageId", "model", "effort"),
    "turn/steer": ("threadId", "input", "clientUserMessageId", "expectedTurnId"),
    "turn/interrupt": ("threadId", "turnId"),
}
CODEX_NOTIFIES = ("initialized",)
CODEX_ANSWERS = (
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "item/tool/call",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
)
CODEX_FOLLOWS = (
    "turn/started",
    "turn/completed",
    "item/started",
    "item/completed",
    "thread/tokenUsage/updated",
    "mcpServer/startupStatus/updated",
    "model/rerouted",
    "thread/closed",
    "thread/compacted",
)
SCHEMA_SECONDS = 20.0
# How long a stopping app-server has to end by itself at the end of its input (graceful: its
# threads written down) before its supervisor is told to end its tree.
STOP_GRACE = 5.0
# How long a worker's process whose settings changed has for its turn to end (it was just
# stopped) before it ends anyway.
RETIRE_SECONDS = 15.0
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
# Codex's own server for ChatGPT's apps (connectors): their calls take approval from Codex's
# `apps` settings, not a server's approval mode, so a worker that asks first runs without them.
CODEX_APPS = "codex_apps"


class CodexError(RuntimeError):
    """An error the app-server answered a request with."""


class OpenInCodexError(CodexError):
    """A thread another app-server has open (the owner's Codex app or IDE): Codex lets one
    write it at a time (0.167.2: "thread <id> already has an active writer")."""

    elsewhere = "Codex"


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


class Taken:
    """How Codex took up a message of Chief of Staff's (a user.message's `delivery`), as Events
    reads it, when it handles the message (on the loop, maybe after a stop that came
    meanwhile): "stopped" once the turn that took it up was stopped (CodexSession.abort), else
    "steering" when it was steered into a turn, else ""."""

    def __init__(self, session: CodexSession, sent: str, steered: bool):
        self.session, self.sent, self.steered = session, sent, steered

    @property
    def value(self) -> str:
        if self.sent in self.session.stopped_sends:
            return "stopped"
        return "steering" if self.steered else ""


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


def github_app(meta: dict) -> bool:
    """Whether a call of ChatGPT's apps is GitHub's: by its connector or its tool."""
    parts = [meta.get(key) for key in ("connector_id", "connector_name", "tool_name", "tool_title")]
    return any(isinstance(part, str) and GITHUB.search(part) for part in parts)


# Trust, as Codex 0.167.2 decides it (codex-rs/config/src/loader/mod.rs: project_trust_context,
# ProjectTrustContext::decision_for_dir, normalized_project_trust_keys, discover_project_root;
# codex-rs/git-utils/src/trust.rs: resolve_root_git_project_for_trust). A folder Codex reads a
# `.codex/` from is trusted by the entry for that very folder, else by the one for its project
# root (the nearest folder with a marker, `.git` by default), else by the one for its
# repository's root (for a linked worktree, its main checkout's); never by an ancestor's entry.
# Each folder is looked up by its canonical path first, then as spelled.
GIT_METADATA_LIMIT = 64 * 1024


def metadata(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except (OSError, ValueError):
        return None


def is_folder(path: Path) -> bool:
    return (found := metadata(path)) is not None and S_ISDIR(found.st_mode)


def canonical(path: Path | str) -> str:
    try:
        return os.path.realpath(path, strict=True)
    except (OSError, ValueError):
        return ""


def trust_keys(path: str) -> list[str]:
    real = canonical(path)
    return [path] if not real or real == path else [real, path]


def project_root(folder: Path, markers: list[str]) -> Path | None:
    """The nearest folder (this one or above) with one of the markers."""
    for ancestor in [folder, *folder.parents]:
        for marker in markers:
            if (found := metadata(ancestor / marker)) is None:
                continue
            if marker == ".git" and S_ISDIR(found.st_mode) and not metadata(ancestor / ".git/HEAD"):
                continue  # A folder named .git that is not a repository.
            return ancestor
    return None


def metadata_file(path: Path) -> bytes:
    """A small regular file of git's (not a link to one), as Codex reads it; b"" otherwise."""
    found = metadata(path)
    if found is None or not S_ISREG(found.st_mode) or path.is_symlink():
        return b""
    if found.st_size > GIT_METADATA_LIMIT:
        return b""
    try:
        data = path.read_bytes()
    except OSError:
        return b""
    return data if len(data) <= GIT_METADATA_LIMIT else b""


def joined(base: Path, named: bytes) -> Path:
    """A path a git file names, from `base` (as Codex joins them: `..` taken away)."""
    return Path(os.path.normpath(base / os.fsdecode(named)))


def gitdir_file(path: Path) -> Path | None:
    """Where a `.git` file points (`gitdir: <path>`, from its folder)."""
    contents = metadata_file(path).strip()
    if not contents.startswith(b"gitdir:"):
        return None
    if not (target := contents.removeprefix(b"gitdir:").strip()):
        return None
    return joined(path.parent, target)


def repository_root(folder: Path) -> Path | None:
    """The root Codex trusts a folder's repository by: its checkout's, or for a linked worktree
    its main checkout's, once git's files show they belong together."""
    base = folder if is_folder(folder) else folder.parent
    while True:
        found = [a for a in [base, *base.parents] if metadata(a / ".git") is not None]
        if not found or (dot_git := metadata(found[0] / ".git")) is None:
            return None
        candidate = found[0]
        if not S_ISDIR(dot_git.st_mode) or metadata(candidate / ".git/HEAD"):
            break
        if (base := candidate.parent) == candidate:
            return None
    if S_ISDIR(dot_git.st_mode):
        return candidate
    git_dir = gitdir_file(candidate / ".git")
    if git_dir is None or not is_folder(git_dir) or git_dir.is_symlink():
        return None
    real_git_dir = Path(canonical(git_dir) or "/")
    if real_git_dir.parent.name != "worktrees":
        return None
    common = str(real_git_dir.parent.parent)
    registered = metadata_file(real_git_dir / "gitdir").strip()
    common_named = metadata_file(real_git_dir / "commondir").strip()
    if not registered or not common_named:
        return None
    registered_dot_git = joined(real_git_dir, registered)
    if registered_dot_git.name != ".git":
        return None
    checkout = canonical(registered_dot_git.parent)
    if not checkout or checkout != canonical(candidate):
        return None
    if canonical(joined(real_git_dir, common_named)) != common:
        return None
    # The main checkout, as the worktree's `.git` names it, owns that repository.
    main = git_dir.parent.parent.parent
    if (found_main := metadata(main / ".git")) is None:
        return None
    main_git_dir = main / ".git" if S_ISDIR(found_main.st_mode) else gitdir_file(main / ".git")
    if main_git_dir is None or canonical(main_git_dir) != common:
        return None
    return main


def owner_trust(projects: Any) -> dict[str, str]:
    """The trust the owner gave folders in Codex (`projects` in its config), by key."""
    return {
        key: entry["trust_level"]
        for key, entry in mapping(projects).items()
        if isinstance(entry, dict) and entry.get("trust_level") in {"trusted", "untrusted"}
    }


def trust_levels(
    owners: Any, folders: list[str], markers: Any = None, answered: dict[str, str] | None = None
) -> dict[str, dict]:
    """The `projects` of a thread's config: each folder Codex would read a `.codex/` from for
    these folders (each one, and those above it up to its project root) gets the trust Codex
    itself gives it for the owner, or what Codex said of it (`answered`, from config/read);
    untrusted otherwise, said explicitly: Codex records a folder without a decision as trusted
    in the owner's config.toml when a writable thread starts there."""
    levels = owner_trust(owners)
    if not isinstance(markers, list) or not all(isinstance(m, str) for m in markers):
        markers = [".git"]
    projects: dict[str, dict] = {}
    for folder in map(Path, folders):
        root = project_root(folder, markers) or folder
        repository = repository_root(folder)
        looked_up = trust_keys(str(root)) + (trust_keys(str(repository)) if repository else [])
        chain = [folder, *folder.parents]
        for place in map(str, chain[: chain.index(root) + 1]):
            level = (answered or {}).get(place) or next(
                (levels[key] for key in trust_keys(place) + looked_up if key in levels),
                "untrusted",
            )
            for key in trust_keys(place):
                projects[key] = {"trust_level": level}
    return projects


# The repository's instructions, as Codex 0.167.2 reads them where it trusts a folder
# (codex-rs/core/src/agents_md.rs: agents_md_paths, candidate_filenames, read_agents_md;
# codex-rs/core/src/context/user_instructions.rs): in each folder from the project root (the
# nearest with a marker, `.git` by default; none: the folder alone) down to the thread's own, the
# first of AGENTS.override.md, AGENTS.md and the owner's project_doc_fallback_filenames that is a
# file; their text in that order, at most project_doc_max_bytes (32 KiB) in all, the last cut.
DOC_NAMES = ("AGENTS.override.md", "AGENTS.md")
DOC_BYTES = 32 * 1024


def doc_names(fallbacks: Any) -> list[str]:
    names: list[str] = list(DOC_NAMES)
    for given in fallbacks if isinstance(fallbacks, list) else []:
        name = given.strip() if isinstance(given, str) else ""
        path_like = name in {".", ".."} or "/" in name or "\0" in name
        if name and not path_like and name not in names:
            names.append(name)
    return names


def project_docs(folder: str, markers: Any, budget: Any, fallbacks: Any) -> str:
    """The repository's instructions Codex would give a thread in `folder` it trusts: each
    file's text, joined as Codex joins them ("" for none)."""
    if not isinstance(markers, list) or not all(isinstance(m, str) for m in markers):
        markers = [".git"]
    remaining = budget if isinstance(budget, int) and budget >= 0 else DOC_BYTES
    place = Path(folder)
    chain = [place, *place.parents]
    # Any marker that exists makes a project root here (no HEAD asked of a `.git` folder).
    root = next((a for a in chain if any(metadata(a / m) for m in markers)), None)
    folders = chain[: chain.index(root) + 1] if root is not None else [place]
    names = doc_names(fallbacks)
    found = []
    for directory in reversed(folders):
        for name in names:
            if (info := metadata(directory / name)) is not None and S_ISREG(info.st_mode):
                found.append(directory / name)
                break
    texts = []
    for path in found:
        if remaining <= 0:
            break
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            return ""  # As Codex: an unreadable one leaves out all of them.
        data = data[:remaining]
        text = data.decode("utf-8", errors="replace")
        if text.strip():
            texts.append(text)
            remaining -= len(data)
    return "\n\n".join(texts)


def docs_digest(docs: str) -> str:
    """What tells one text of the repository's instructions from another ("" for none)."""
    return hashlib.sha256(docs.encode()).hexdigest()[:16] if docs else ""


def repository_message(cwd: str, docs: str, *, replacing: bool = False, again: bool = False):
    """The repository's instructions as a message of the thread's conversation, where Codex
    gives its own AGENTS.md (a user's message, never developer instructions): marked as the
    repository's text, with every tag in it escaped (it can open or close no block of Chief of
    Staff's, nor pose as one of its messages). No docs: those given before are withdrawn."""
    if not docs:
        text = (
            "The repository's instructions given earlier in this conversation no longer apply "
            "(the folder has none, or Codex gives them itself)."
        )
    else:
        first = (
            "These replace the repository's instructions given earlier.\n\n"
            if replacing
            else "Given again, as the conversation was summarized.\n\n"
            if again
            else ""
        )
        text = (
            f"{first}The repository's own instructions (its AGENTS.md files) for {inert(cwd)}, "
            "as a coding agent reads them in this folder. They are the repository's text, not "
            "Chief of Staff's nor the owner's, and a tag in them is only text.\n\n"
            f"<repository_instructions>\n{inert(docs.strip(chr(10)))}\n</repository_instructions>"
        )
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def trust_answered(layers: Any) -> dict[str, str]:
    """What Codex said of each folder whose `.codex/` it found (config/read's layers): trusted
    when that layer loads, else untrusted."""
    answered = {}
    for layer in layers if isinstance(layers, list) else []:
        name = mapping(mapping(layer).get("name"))
        if name.get("type") == "project" and isinstance(name.get("dotCodexFolder"), str):
            folder = str(Path(name["dotCodexFolder"]).parent)
            answered[folder] = "untrusted" if layer.get("disabledReason") else "trusted"
    return answered


def event(kind: str, /, **data: Any) -> SimpleNamespace:
    return SimpleNamespace(type=SimpleNamespace(value=kind), data=SimpleNamespace(**data))


def release(version: str) -> tuple[int, ...] | None:
    """A version, comparable: a pre-release ("0.168.0-alpha.2") comes before its release."""
    found = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(-[\w.-]+)?", version or "")
    if not found:
        return None
    return (*map(int, found.group(1, 2, 3)), 0 if found.group(4) else 1)


def codex_refused(version: str) -> str:
    """Why this Codex does not run at all ("" when it does)."""
    if (found := release(version)) is None:
        return (
            f"Chief of Staff could not tell which version of Codex this is ({version or 'none'} "
            f"said): update it to {OLDEST_CODEX} or later."
        )
    if found < (release(OLDEST_CODEX) or ()):
        return f"Codex {version} is older than the versions tested ({OLDEST_CODEX}): update it."
    return ""


def ask_first_refusal(version: str, checked: dict | None) -> str:
    if not ask_first_tested(version):
        return (
            f"Codex {version} has not passed Chief of Staff's checks for Ask first (tested from "
            f"{OLDEST_CODEX}, before {ASK_FIRST_BEFORE}): let this worker act on its own, or use "
            "a tested version."
        )
    return (checked or {}).get("ask_first") or ""


def ask_first_tested(version: str) -> bool:
    found = release(version)
    oldest, before = release(OLDEST_CODEX) or (), release(ASK_FIRST_BEFORE) or ()
    # Not a pre-release of the next minor version either.
    return found is not None and oldest <= found and found[:3] < before[:3]


class CodexBackend:
    """One app-server process of the owner's Codex CLI, and the threads it runs. Workers each
    have one of their own (CodexAgent); Chief of Staff's check and Settings' look use one too."""

    capabilities = CODEX

    def __init__(self, command: str, owner: CodexAgent | None = None):
        self.command = command
        # The worker processes this belongs to (CodexAgent), which keeps its verdicts and hooks.
        self.owner = owner
        # Approvals are Chief of Staff's to answer, never a reviewer agent's.
        self.arguments = ["app-server", "-c", f'approvals_reviewer="{REVIEWER}"']
        # Codex's experimental API: Chief of Staff's own tools (dynamicTools) and the commands a
        # turn left running (thread/backgroundTerminals/*) need it; Settings' look does not.
        self.experimental = True
        # The skill folders from Chief of Staff's Settings this process reads (skill_roots).
        self.extra_roots: list[str] = []
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
        # This process, as its usage's accounting epoch (5.8): its own id, from its start; and
        # the usage each thread resumed here restored (thread/tokenUsage/updated before the
        # resume's answer).
        self.epoch = ""
        self.restored: dict[str, dict] = {}
        self.tasks: set[asyncio.Task] = set()
        # The supervisor's process and those it started, as last seen: pid -> command line.
        self.started: dict[int, str] = {}
        # Chief of Staff's admission, asked before a turn starts: "" when admitted, else why
        # not (Runtime.admit_turn); and whether that admission still holds just before the
        # turn begins (Runtime.begins_turn). Without them every turn is admitted.
        self.admit: Callable[[CodexSession], Awaitable[str]] | None = None
        self.begins: Callable[[CodexSession], bool] | None = None
        # What its check showed on this version (check): why workers may not run on it.
        self.checked: dict | None = None
        # Whether it needs the owner's sign-in (not for the check's model, on this machine).
        self.needs_sign_in = True
        # Told once its process has ended: whether by itself (a crash) rather than stopped.
        self.on_end: Callable[[bool], None] | None = None
        self.stopping = False
        self.by_itself = False
        self.told_end = False
        self.ending: asyncio.Task | None = None
        self.ended_process: asyncio.subprocess.Process | None = None
        # Why Chief of Staff stopped it at once (a turn reached outside its worker's threads),
        # and how many threads are being opened on it now.
        self.refusal = ""
        self.opening = 0
        # Ending as its worker's settings changed (CodexAgent.retire): it opens no thread.
        self.retiring: asyncio.Future | None = None

    # The process

    async def start(self) -> None:
        if not shutil.which(self.command):
            raise RuntimeError(f"Codex CLI '{self.command}' not found. Install it and sign in.")
        if self.ending is not None and self.ending.done():
            # Started again once its process ended (Chief of Staff starts a new one for each
            # worker; tests start one again): a process of its own, ended by its own stop.
            self.stopping = self.by_itself = self.told_end = False
            self.ending, self.sessions = None, {}
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
        self.epoch = uuid4().hex
        self.reader = asyncio.ensure_future(self.read())
        self.errors = asyncio.ensure_future(self.read_errors())
        if self.owner is not None:
            self.tasks.add(asyncio.ensure_future(self.watch(self.process)))
        try:
            answer = await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "chiefofstaff",
                        "title": "Chief of Staff",
                        "version": client_version(),
                    },
                    "capabilities": {"experimentalApi": self.experimental},
                },
            )
            self.notify("initialized")
            self.cli_version = version_of(answer.get("userAgent", ""))
            if why := codex_refused(self.cli_version):
                raise RuntimeError(why)
            await self.check_requirements()
            if self.needs_sign_in and not (await self.get_auth_status()).isAuthenticated:
                raise RuntimeError("Codex is not signed in: run codex login in a terminal.")
        except BaseException:
            await self.stop()
            raise

    async def check_requirements(self):
        """An administrator's requirements may forbid what Chief of Staff's checks rely on."""
        answer = await self.request("configRequirements/read", None)
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
        """It ends, in a task of its own (what asks again waits for that one; one that was
        cancelled is done again)."""
        self.stopping = True
        if self.ending is None or self.ending.cancelled():
            self.ending = asyncio.ensure_future(self.end())
        await asyncio.shield(self.ending)

    async def end(self) -> None:
        process = self.process or self.ended_process
        self.process, self.ended_process = None, process
        for session in list(self.sessions.values()):
            session.closed = True
            if session.pump:
                session.pump.cancel()
        if process:
            if process.stdin and not process.stdin.is_closing():
                process.stdin.close()  # The app-server ends at the end of its input.
            try:
                await asyncio.wait_for(process.wait(), STOP_GRACE)
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

    def processes(self, table: dict | None = None) -> dict[int, str]:
        if self.process and self.process.returncode is None:
            table = process_table() if table is None else table
            self.started |= {
                pid: table[pid][1] for pid in descendants(table, self.process.pid) if pid in table
            }
        return dict(self.started)

    # JSON-RPC

    def write(self, message: dict):
        if not self.process or not self.process.stdin or self.process.stdin.is_closing():
            raise ConnectionResetError("Codex is not running.")
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())

    def send_request(self, method: str, params: dict | None) -> asyncio.Future:
        """Write a request now (nothing awaited before it is written); its answer comes later.
        Once nothing reads its answers (Codex stopped), it fails at once. `params` None: a
        method that takes none."""
        if self.reader is not None and self.reader.done():
            raise ConnectionResetError("Codex stopped.")
        self.ids += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.ids] = future
        try:
            given = {} if params is None else {"params": params}
            self.write({"jsonrpc": "2.0", "id": self.ids, "method": method, **given})
        except BaseException:
            self.pending.pop(self.ids, None)
            raise
        return future

    async def request(self, method: str, params: dict | None) -> dict:
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
            if not self.stopping and self.owner is not None:
                self.by_itself = True  # What is left of it ends (its supervisor ends its tree).
                self.tasks.add(asyncio.ensure_future(self.stop()))
            self.ended()

    async def watch(self, process: asyncio.subprocess.Process):
        """Its supervisor ended by itself (killed): Codex, left without it, is ended with it.
        (Its exit is seen as it happens; wait() would wait for Codex's output to close too.)"""
        while True:  # Polled: nothing is told when its returncode is set.
            if process.returncode is not None or self.stopping:
                break
            await asyncio.sleep(WATCH_SECONDS)
        if not self.stopping:
            self.by_itself = True
            await self.stop()

    def ended(self):
        if self.on_end and not self.told_end:
            self.told_end = True
            self.on_end(self.by_itself)

    def release_if_idle(self):
        """A worker's own process whose threads are all let go of, with nothing of them
        running (and none being opened on it): it ends (the worker's next message starts
        another, which resumes them)."""
        if self.owner is None or self.stopping or self.opening:
            return
        if all(session.closed and session.idle() for session in self.sessions.values()):
            self.stopping = True  # From now on no thread is opened on it (CodexAgent).
            task = asyncio.ensure_future(self.stop())
            self.tasks.add(task)

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
            # What its session had been told before this request, to be read before its tools
            # answer (Codex's requests come at once, its news in order).
            asked = self.session_of(params.get("threadId") or "", params)
            told = (asked, asked.told) if asked else None
            task = asyncio.ensure_future(self.serve(message["id"], method, params, told))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return
        self.observe(method, params)
        thread = params.get("threadId") or (params.get("thread") or {}).get("id") or ""
        if session := self.session_of(thread, params):
            session.told += 1
            session.inbox.put_nowait((method, params, thread))
        elif method == "thread/tokenUsage/updated" and thread:
            # What a thread resumed in this process restored, said before the resume's answer.
            self.restored[thread] = mapping(mapping(params.get("tokenUsage")).get("total"))

    def observe(self, method: str, params: dict):
        """What requests and routing need before the session's events are handled in order."""
        item = mapping(params.get("item"))
        if method == "item/started" and item.get("type") == "fileChange":
            self.changes[item.get("id", "")] = list(item.get("changes") or [])
        elif method == "item/completed" and item.get("type") == "fileChange":
            self.changes.pop(item.get("id", ""), None)
        if item.get("type") == "subAgentActivity" and item.get("agentThreadId"):
            # A child thread (a subagent) of this one: its work is its worker's. Multi-agent v2
            # (as OpenAI's models choose) says a spawn only so: no spawnAgent call names it.
            self.parents.setdefault(item["agentThreadId"], params.get("threadId", ""))
            self.adopt(params.get("threadId") or "", str(item["agentThreadId"]))
        if item.get("type") == "collabAgentToolCall":
            self.collab(method, item, params.get("threadId") or "")

    def collab(self, method: str, item: dict, thread: str):
        """A turn's call on other threads, seen before it acts: a child it spawns joins its
        worker's tree; any other thread it names must be in that tree (Codex lets a turn
        resume any thread it can name, from disk). Handled as read, not in turn."""
        session = self.session_of(thread, {})
        threads = [str(t) for t in item.get("receiverThreadIds") or [] if t]
        if session is None or session.closed:
            return
        if item.get("tool") == "spawnAgent":
            for child in threads:
                self.adopt(thread, child)
            return
        foreign = [t for t in threads if t not in session.tree]
        if method == "item/started" and item.get("tool") != "listAgents" and foreign:
            session.reached_out(str(item.get("tool")), foreign)

    def adopt(self, thread: str, child: str):
        """A child `thread` spawned joins its worker's tree, recorded for later resumes."""
        session = self.session_of(thread, {})
        if session is None or session.closed or child in session.tree:
            return
        session.tree.add(child)
        self.parents.setdefault(child, thread)
        session.emit("agent.child", thread_id=child)

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

    async def serve(
        self, request_id: Any, method: str, params: dict, told: tuple[Any, int] | None = None
    ):
        if method == "currentTime/read":
            # Asked when the owner's Codex takes its time from its client (an external clock).
            self.respond(request_id, {"currentTimeAt": int(time.time())})
            return
        thread = params.get("threadId") or ""
        session = self.session_of(thread, params)
        try:
            if session is None:
                raise CodexError("Unknown thread.")
            before = told[1] if told and told[0] is session else 0
            result = await session.request(method, params, thread, before)
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

    async def about(self) -> dict:
        """Its version, how it is signed in (the kind of account, never who) and its models."""
        auth = await self.get_auth_status()
        return {
            "version": self.cli_version,
            "account": auth.authType if auth.isAuthenticated else "",
            "models": await self.list_models(),
        }

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
            await self.read_back(answer, options)
            session = self.opened(answer, options)
            session.docs_given = ""  # A new thread: nothing given to it yet.
            await session.give_docs()
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
        try:
            answer = await self.request("thread/resume", params)
        except CodexError as exc:
            if "already has an active writer" in str(exc):
                raise OpenInCodexError(str(exc)) from None
            raise
        try:
            await self.read_back(answer, options)
            session = self.opened(answer, options)
            await session.give_docs()
        except RuntimeError:
            if thread := (answer.get("thread") or {}).get("id"):
                with suppress(Exception):
                    await self.request("thread/unsubscribe", {"threadId": thread})
            raise
        return session

    async def read_back(self, answer: dict, options: dict):
        """The thread must read back with Codex's memories off where this Codex has them, and,
        with Ask first, ChatGPT's apps and its subagents off, as set (thread_config)."""
        thread = (answer.get("thread") or {}).get("id") or ""
        try:
            listed = await self.request("experimentalFeature/list", {"threadId": thread})
        except CodexError as exc:
            raise RuntimeError(
                f"Codex could not say which features this worker has: {exc}"
            ) from exc
        features = {mapping(f).get("name"): mapping(f) for f in listed.get("data") or []}
        kept = "an administrator's Codex settings may keep them on."
        if "memories" in features and features["memories"].get("enabled") is not False:
            raise RuntimeError(
                "Codex did not turn its memories off for this worker (their background agent "
                f"runs commands nobody is asked about, and Stop does not end it): {kept}"
            )
        if not options.get("ask_first"):
            return
        if features.get("apps", {}).get("enabled") is not False:
            raise RuntimeError(
                "Codex did not turn ChatGPT's apps off for this worker, which Ask first needs "
                f"(their actions are not checked by Chief of Staff): {kept}"
            )
        if any(
            features.get(name, {}).get("enabled") not in {False, None}
            for name in ("multi_agent", "multi_agent_v2")
        ):
            raise RuntimeError(
                "Codex did not turn its subagents off for this worker, which Ask first needs "
                f"(one could open another worker's conversation): {kept}"
            )

    @property
    def ask_first_refused(self) -> str:
        """Why its workers cannot ask first ("" when they can): only a version tested for it,
        whose check here showed every kind of action asking Chief of Staff first, runs them
        (Runtime.agent_block). A worker's process answers as its owner does."""
        if self.owner is not None:
            return self.owner.ask_first_refused
        return ask_first_refusal(self.cli_version, self.checked)

    @property
    def acting_refused(self) -> str:
        """Why none of its workers runs, even acting on its own ("" when they do): its check
        here showed edits not asking Chief of Staff first (the owner's checkouts are kept by
        that ask, also for a worker acting on its own)."""
        if self.owner is not None:
            return self.owner.acting_refused
        return (self.checked or {}).get("acting") or ""

    async def check(self, folder: Path) -> dict:
        """What Chief of Staff's checks rely on, on this Codex, without a model call: a thread
        set up as a worker that asks first is (read back), whose one turn, from a stand-in
        model on this machine (codex_check), calls one of Chief of Staff's tools, runs a
        command, calls an MCP tool, adds two files and moves one. Each must reach Chief of
        Staff, which refuses each (an edit at its last file, so every file of it is asked
        about); nothing refused may happen. `ran`: False when the check itself could not run
        (then it is not remembered)."""
        work = folder / "work"
        asked: list[str] = []
        model = other = None
        try:
            await asyncio.to_thread(shutil.rmtree, folder, True)
            await asyncio.to_thread(work.mkdir, parents=True)
            if missing := await self.protocol_missing(folder / "schema"):
                why = (
                    f"Codex {self.cli_version} does not have what Chief of Staff uses of it ("
                    + ", ".join(missing[:4])
                    + (", …" if len(missing) > 4 else "")
                    + "): no worker runs on it. Use a tested version."
                )
                return {"ran": True, "ask_first": why, "acting": why}
            (work / codex_check.MOVED_FROM).write_text("a\n")
            model = await asyncio.to_thread(codex_check.Model, work)
            other = CodexBackend(self.command)
            other.arguments += model.arguments(folder)
            other.needs_sign_in = False
            await asyncio.wait_for(self.checking(other, work, asked), CHECK_TIMEOUT)
            happened = [
                name
                for name in (
                    codex_check.COMMAND_MARK,
                    codex_check.SERVER_MARK,
                    *codex_check.ADDED,
                    codex_check.MOVED_TO,
                )
                if (work / name).exists()
            ]
        except Exception as exc:  # Whatever stops the check, it says why.
            why = (
                f"Chief of Staff's check of Codex {self.cli_version} could not run ("
                + ((str(exc) or type(exc).__name__)[:200])
            )
            return {
                "ran": False,
                "ask_first": f"{why}): workers that ask first do not run on it until it does.",
                # A version newer than those tested runs no worker until its check has run.
                "acting": ""
                if ask_first_tested(self.cli_version)
                else f"{why}): no worker runs on this newer version until it does.",
            }
        finally:
            if other:
                await other.stop()
            if model:
                await asyncio.to_thread(model.close)
            await asyncio.to_thread(shutil.rmtree, folder, True)
        offered = sorted(model.subagents) if model else []
        return check_verdict(self.cli_version, asked, happened, offered)

    async def protocol_missing(self, folder: Path) -> list[str]:
        """What Chief of Staff uses of Codex's protocol (CODEX_SENDS and the rest) this version's
        own schema does not have, found offline; nothing when its schema cannot be had."""
        try:
            process = await asyncio.create_subprocess_exec(
                self.command,
                "app-server",
                "generate-json-schema",
                "--experimental",
                "--out",
                str(folder),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return []
        try:
            if await asyncio.wait_for(process.wait(), SCHEMA_SECONDS) != 0:
                return []
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            return []
        try:
            return await asyncio.to_thread(protocol_missing, folder)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return []

    async def checking(self, other: CodexBackend, work: Path, asked: list[str]):
        await other.start()
        ended = asyncio.Event()

        def check(request: Any, invocation: dict) -> Any:
            kind = getattr(request, "kind", "")
            if kind != "write":
                asked.append(kind)
                return PermissionDecisionReject(feedback="Chief of Staff's check.")
            name = Path(request.resolved_path).name
            asked.append(f"write {name}")
            # An edit's last file is refused, so each of its files is asked about first.
            if name in {codex_check.ADDED[-1], codex_check.MOVED_TO}:
                return PermissionDecisionReject(feedback="Chief of Staff's check.")
            return PermissionDecisionApproveOnce()

        def tool(invocation: Any) -> str:
            asked.append("tool")
            return "checked"

        session = await other.create_session(
            session_id="check",
            name="",
            working_directory=str(work),
            folders=[str(work)],
            ask_first=True,
            ephemeral=True,
            checking=str(work),
            tools=[
                SimpleNamespace(
                    name=codex_check.TOOL,
                    description="Chief of Staff's check.",
                    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
                    handler=tool,
                )
            ],
            on_permission_request=check,
        )

        said: list[str] = []
        failed: list[str] = []

        def on_event(event: Any):
            kind = event.type.value
            if kind == "assistant.message":
                said.append(str(getattr(event.data, "content", "") or "").strip())
            elif kind == "session.error":
                failed.append(str(getattr(event.data, "message", "") or "its turn failed"))
            if kind in {"session.idle", "session.error"}:
                ended.set()

        session.on(on_event)
        await session.send(codex_check.PROMPT.format(work=work))
        await ended.wait()
        if failed or not said or said[-1] != codex_check.REPLY:
            # Its turn did not run to its end: what it found is not all there is.
            raise RuntimeError(failed[0] if failed else "its turn ended before its last step")

    async def thread_params(self, options: dict) -> dict:
        # Chief of Staff's check opens its thread as a worker that asks first, on whatever
        # version it checks: the verdicts it is to find do not apply to it.
        if not options.get("checking"):
            if why := self.acting_refused:
                raise RuntimeError(why)
            if options.get("ask_first") and (why := self.ask_first_refused):
                raise RuntimeError(why)
        cwd = options.get("working_directory") or str(Path.home())
        config = await self.thread_config(options, cwd)
        prompt = (options.get("system_message") or {}).get("content") or ""
        return {
            "cwd": cwd,
            "sandbox": "danger-full-access",
            "approvalPolicy": APPROVAL_POLICY,
            "approvalsReviewer": REVIEWER,
            **({"model": options["model"]} if options.get("model") else {}),
            **({"developerInstructions": prompt} if prompt else {}),
            **({"ephemeral": True} if options.get("ephemeral") else {}),
            "config": config,
        }

    async def thread_config(self, options: dict, cwd: str) -> dict:
        """The thread's own settings, over the owner's: whether its folders are trusted (as
        Codex trusts them for the owner), and with Ask first, every MCP call asks and ChatGPT's
        apps are off."""
        read = await self.request("config/read", {"cwd": cwd, "includeLayers": True})
        owner = mapping(read.get("config"))
        # The MCP servers the owner added in Chief of Staff's Settings, as Codex has one.
        provided = {
            str(name): found
            for name, entry in mapping(options.get("mcp_servers")).items()
            if (found := codex_mcp(entry))
        }
        # Which of its MCP servers is GitHub's is told from their configuration (elicitation).
        options["mcp_configs"] = {**mapping(owner.get("mcp_servers")), **provided}
        await self.skill_roots(options.get("skill_directories") or [])
        folders = [cwd, *(f for f in options.get("folders") or [] if f != cwd)]
        projects = await asyncio.to_thread(
            trust_levels,
            owner.get("projects"),
            folders,
            owner.get("project_root_markers"),
            trust_answered(read.get("layers")),
        )
        # Untrusted, Codex reads none of the repository's AGENTS.md: its text is given to the
        # thread (give_docs), as a Copilot worker's repository instructions are (nothing of the
        # repository is run).
        options["repository_docs"] = ""
        if projects.get(cwd, {}).get("trust_level") != "trusted":
            options["repository_docs"] = await asyncio.to_thread(
                project_docs,
                cwd,
                owner.get("project_root_markers"),
                owner.get("project_doc_max_bytes"),
                owner.get("project_doc_fallback_filenames"),
            )
        # Codex merges a table given here into the owner's config files' tables, key by key
        # (config/src/merge.rs); a dotted key would split a name with a dot in it (overrides.rs),
        # so tables are nested. (With the process's own `-c` flags it makes one layer, where a
        # table replaces what is at its key: none of those set what is set here.)
        config: dict[str, Any] = {"projects": projects}
        if self.owner is not None:
            self.owner.owner_memories = feature_on(owner, "memories")
        # Codex's memories, for every worker: in a process with them on, a consolidation agent
        # of its own runs commands with no approval asked (and, under a worker's full access,
        # without a sandbox), which Stop does not end and no receipt shows; generate_memories
        # alone does not stop it starting (0.167.2). Off here, and the thread never feeds the
        # owner's memories (memory_mode "disabled").
        config["features.memories"] = False
        config["memories"] = {"generate_memories": False}
        # Its commands run in no login shell (`zsh -c`, not `-lc`, nor the snapshot of the
        # owner's login shell Codex sources then): they see the environment Chief of Staff
        # gives the worker, PATH as it is, as a Copilot worker's commands do (macOS's login
        # shell puts the system's folders first: another `python3`). The owner's decision.
        config["allow_login_shell"] = False
        if provided:
            # Codex merges these into the owner's own servers, as Copilot does its session's.
            config["mcp_servers"] = dict(provided)
        if options.get("ask_first"):
            config["features.apps"] = False
            # Its subagents: a child's resume_agent opens any thread Codex can name (another
            # worker's): none for a worker that asks first. `agents.enabled` overrides what a
            # model's own catalog entry says; v2 would win over both (config/mod.rs).
            config["features.multi_agent"] = False
            config["features.multi_agent_v2"] = False
            config["agents"] = {"enabled": False}
            # Every server it can name asks, each tool any table names too: Codex merges a
            # server's table here into the owner's key by key, so a tool set to `approve` there
            # (Codex's own "Always allow", or a trusted repository's .codex/) would outrank the
            # server's default, also for a Settings server of the same name.
            known = owner_servers(read)
            servers = {}
            for name in {*known, *provided}:
                entry, given = mapping(known.get(name)), mapping(provided.get(name))
                tools = {**mapping(entry.get("tools")), **mapping(given.get("tools"))}
                checking = bool(options.get("checking")) and name not in provided
                servers[name] = {**given, **asking({"tools": tools}, checking=checking)}
            if servers:
                config["mcp_servers"] = servers
            # A plugin's MCP servers have their policy in its own table: each asks too.
            plugins = {} if options.get("checking") else await self.plugin_servers(cwd)
            configured = mapping(owner.get("plugins"))
            if plugins:
                config["plugins"] = {
                    plugin: {
                        "mcp_servers": {
                            name: asking(mapping(configured.get(plugin)).get("mcp_servers"), name)
                            for name in names
                        }
                    }
                    for plugin, names in plugins.items()
                }
        if checking := options.get("checking"):
            # In the same table as the owner's servers' policies (one key per table here).
            config.setdefault("mcp_servers", {})[codex_check.SERVER] = {
                "command": sys.executable,
                "args": ["-m", "chiefofstaff.codex_check", "--mcp", checking],
                "default_tools_approval_mode": "prompt",
            }
        return config

    async def skill_roots(self, folders: list[str]):
        """The skill folders the owner added in Settings, for this process (each worker has its
        own): Codex reads each as it does its own `skills` folder."""
        roots = [str(Path(folder)) for folder in folders]
        if roots == self.extra_roots:
            return
        await self.request("skills/extraRoots/set", {"extraRoots": roots})
        self.extra_roots = roots

    async def plugin_servers(self, cwd: str) -> dict[str, list[str]]:
        """The MCP servers each of Codex's plugins on for this folder adds (plugin id -> their
        names); raises with why when they cannot all be read (Ask first is then refused: a
        server whose calls cannot be made to ask is not let run unasked)."""
        try:
            answer = await self.request("plugin/installed", {"cwds": [cwd]})
            found: dict[str, list[str]] = {}
            for market in answer.get("marketplaces") or []:
                for plugin in market.get("plugins") or []:
                    if not (plugin.get("installed") and plugin.get("enabled")):
                        continue
                    where = (
                        {"marketplacePath": market["path"]}
                        if market.get("path")
                        else {"remoteMarketplaceName": market.get("name")}
                    )
                    read = await self.request(
                        "plugin/read", {"pluginName": plugin.get("name") or "", **where}
                    )
                    if names := mapping(read.get("plugin")).get("mcpServers") or []:
                        found[str(plugin.get("id"))] = [str(name) for name in names]
        except CodexError as exc:
            raise RuntimeError(
                "Codex's plugins could not be read, so Chief of Staff can't make their MCP "
                f"tools ask first ({exc}): let this worker act on its own."
            ) from None
        return found

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
        if (restored := self.restored.pop(session.session_id, None)) is not None:
            session.restored[session.session_id] = restored
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


class CodexAgent:
    """Codex for Chief of Staff's workers: an app-server process of each worker's own (its child
    agents run in it), so no thread can reach another worker's through Codex's thread registry
    (more-agents.md 5.3). Each is started with the worker's first turn or resume and ends once
    its threads are let go of with nothing of them running; when one stops by itself, only its
    worker is affected (`ended`, Runtime.agent_process_ended)."""

    capabilities = CODEX

    def __init__(self, command: str):
        self.command = command
        self.arguments: list[str] = []  # More `-c` settings for each process (the check's).
        self.needs_sign_in = True
        self.cli_version = ""
        self.checked: dict | None = None
        self.admit: Callable[[CodexSession], Awaitable[str]] | None = None
        self.begins: Callable[[CodexSession], bool] | None = None
        # Told when a worker's process ends: the worker, the process (its usage epoch), and
        # why: "" when let go of or stopped, "crash" when it stopped by itself, else why
        # Chief of Staff stopped it at once; and when one has started.
        self.ended: Callable[[str, str, str], None] | None = None
        self.on_start: Callable[[], None] | None = None
        self.workers: dict[str, CodexBackend] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.running = False
        # Workers' processes that ended or are ending (their stop is awaited with this one's).
        self.retired: set[CodexBackend] = set()
        # Whether the owner's own Codex has its memories on (as its last thread read it): then
        # a worker's header says they are off for it (thread_config).
        self.owner_memories = False

    def notes_for(self, ask_first: bool) -> list[str]:
        """What a worker of it does differently from a Copilot worker, a short line each."""
        notes = ["Its Codex memories are off"] if self.owner_memories else []
        return [*notes, "Its subagents are off"] if ask_first else notes

    def process(self) -> CodexBackend:
        process = CodexBackend(self.command, owner=self)
        process.arguments += self.arguments
        process.needs_sign_in = self.needs_sign_in
        process.admit, process.begins = self.admit_turn, self.begins_turn
        return process

    async def admit_turn(self, session: CodexSession) -> str:
        return await self.admit(session) if self.admit else ""

    def begins_turn(self, session: CodexSession) -> bool:
        return self.begins(session) if self.begins else True

    async def start(self) -> None:
        """What each worker's process checks as it starts (the CLI, its version, the
        administrator's requirements, the sign-in), once now in a process of its own: a Codex
        that cannot run says why here."""
        probe = self.process()
        try:
            await probe.start()
            self.cli_version = probe.cli_version
        finally:
            await probe.stop()
        self.running = True

    async def stop(self) -> None:
        self.running = False
        workers, self.workers = [*self.workers.values(), *self.retired], {}
        self.retired = set()
        await asyncio.gather(*(process.stop() for process in workers), return_exceptions=True)

    def alive(self) -> bool:
        return self.running

    def epochs(self) -> set[str]:
        """Its workers' processes running now, as usage epochs."""
        return {process.epoch for process in self.workers.values() if process.alive()}

    def processes(self) -> dict[int, str]:
        """The processes its workers' processes run now (each supervisor and its tree)."""
        found: dict[int, str] = {}
        if running := [process for process in list(self.workers.values()) if process.alive()]:
            table = process_table()
            for process in running:
                found |= {
                    pid: command
                    for pid, command in process.processes(table).items()
                    if table.get(pid, (0, ""))[1] == command
                }
        return found

    async def process_for(self, worker: str) -> CodexBackend:
        """A worker's own process, started on first use (and after it ended)."""
        lock = self.locks.setdefault(worker, asyncio.Lock())
        async with lock:
            process = self.workers.get(worker)
            if process and process.alive() and not process.stopping and not process.retiring:
                process.opening += 1  # Until its thread is open (opened): not let go of.
                return process
            if process is not None:
                # Ending: its threads are let go of first (Codex lets one process write a
                # thread at a time), so that they open again in the new one.
                with suppress(Exception):
                    await asyncio.shield(process.retiring or process.stop())
            if not self.running:
                raise RuntimeError("Codex is not running for Chief of Staff.")
            process = self.process()
            await process.start()
            if process.cli_version != self.cli_version:
                await process.stop()
                self.running = False  # Checked again before its workers go on (Runtime).
                raise RuntimeError(
                    f"Codex was updated ({self.cli_version} to {process.cli_version}): Chief "
                    "of Staff checks it again first; send it again."
                )
            process.on_end = lambda by_itself: self.process_ended(worker, process, by_itself)
            self.workers[worker] = process
            process.opening += 1
            if self.on_start:
                self.on_start()
            return process

    async def retire(self, worker: str, seconds: float = RETIRE_SECONDS):
        """A worker whose conversation's settings changed: its process opens no more thread
        (its next one opens in a new process, with its new settings) and ends once nothing of
        it runs (its turn just stopped), `seconds` at the latest."""
        process = self.workers.get(worker)
        if process is None:
            return
        if process.retiring is None:
            process.retiring = asyncio.ensure_future(self.retiring(process, seconds))
        await asyncio.shield(process.retiring)

    async def retiring(self, process: CodexBackend, seconds: float):
        deadline = time.monotonic() + seconds
        while True:  # Polled: its sessions say nothing when they come to rest.
            busy = any(not session.idle() for session in list(process.sessions.values()))
            if not busy or not process.alive() or process.stopping:
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.1)
        await process.stop()

    def process_ended(self, worker: str, process: CodexBackend, by_itself: bool):
        if self.workers.get(worker) is process:
            del self.workers[worker]
        self.retired = {p for p in self.retired if not (p.ending and p.ending.done())}
        self.retired.add(process)
        if self.ended:
            why = process.refusal or ("crash" if by_itself else "")
            try:
                self.ended(worker, process.epoch, why)
            except Exception:
                logger.exception("Ending %s's Codex process failed.", worker)

    async def any_process(self, call: Callable[[CodexBackend], Awaitable[Any]]) -> Any:
        """What any process can answer (models, a thread's metadata): a worker's running one,
        else one started for it."""
        for process in list(self.workers.values()):
            if process.alive():
                return await call(process)
        process = self.process()
        try:
            await process.start()
            return await call(process)
        finally:
            await process.stop()

    @property
    def ask_first_refused(self) -> str:
        return ask_first_refusal(self.cli_version, self.checked)

    @property
    def acting_refused(self) -> str:
        return (self.checked or {}).get("acting") or ""

    async def check(self, folder: Path) -> dict:
        checker = self.process()
        checker.cli_version = self.cli_version
        checker.owner = None  # Its own verdicts are to be found, not the owner's.
        return await checker.check(folder)

    async def version(self) -> str:
        return self.cli_version

    async def get_auth_status(self) -> Any:
        return await self.any_process(lambda process: process.get_auth_status())

    async def about(self) -> dict:
        return await self.any_process(lambda process: process.about())

    async def list_models(self) -> list[Any]:
        return await self.any_process(lambda process: process.list_models())

    async def list_sessions(self) -> list[Any]:
        return []

    async def get_session_metadata(self, session_id: str) -> Any:
        return await self.any_process(lambda process: process.get_session_metadata(session_id))

    async def create_session(self, *, session_id: str, name: str, **options: Any) -> CodexSession:
        process = await self.process_for(options.get("worker") or session_id)
        try:
            return await process.create_session(session_id=session_id, name=name, **options)
        finally:
            self.opened(process)

    async def resume_session(self, session_id: str, **options: Any) -> CodexSession:
        process = await self.process_for(options.get("worker") or session_id)
        try:
            return await process.resume_session(session_id, **options)
        finally:
            self.opened(process)

    def opened(self, process: CodexBackend):
        """A thread was opened on a worker's process (or could not be): one that has none
        open ends."""
        process.opening -= 1
        process.release_if_idle()

    async def archive(self, session_id: str):
        await self.any_process(lambda process: process.archive(session_id))

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


def owner_servers(read: dict) -> dict[str, dict]:
    """The MCP servers of the owner's configuration for a folder (config/read), each with the
    tools any of its loaded layers names (user, a trusted project's): their policies are merged
    key by key."""
    servers = {
        str(k): dict(mapping(v))
        for k, v in mapping(mapping(read.get("config")).get("mcp_servers")).items()
    }
    for layer in read.get("layers") or []:
        if not isinstance(layer, dict) or layer.get("disabledReason"):
            continue
        for name, entry in mapping(mapping(layer.get("config")).get("mcp_servers")).items():
            found = servers.setdefault(str(name), {})
            tools = {**mapping(found.get("tools")), **mapping(mapping(entry).get("tools"))}
            if tools:
                found["tools"] = tools
    return servers


def asking(entry: Any, name: str = "", checking: bool = False) -> dict:
    """An MCP server's policy for a worker that asks first: every tool of it asks (also those
    the owner set to run unasked); `name`: the entry of `entry` (a plugin's servers) to read."""
    entry = mapping(mapping(entry).get(name)) if name else mapping(entry)
    policy: dict[str, Any] = {"default_tools_approval_mode": "prompt"}
    if tools := mapping(entry.get("tools")):
        policy["tools"] = {str(tool): {"approval_mode": "prompt"} for tool in tools}
    if checking:
        policy["enabled"] = False  # Not started for Chief of Staff's check.
    return policy


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
        # How much of what Codex said of it was put in its inbox, and read from there.
        self.told = 0
        self.read = 0
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
        # Requests of Codex it does not handle, each said once (refused, never left waiting).
        self.unhandled: set[str] = set()
        # A turn Codex said started, shown once its message is (as Copilot shows a turn: the
        # message first, then its start).
        self.unshown: str = ""
        # Stopped (abort) as its turn was starting or running: what Codex said of that turn
        # and Chief of Staff handles after the stop (its message taken up, its start) is not
        # news of a turn at work, which would undo the stop (its task running again): the
        # messages sent until then read as taken up by a stopped turn (Taken), and that turn
        # is not shown as starting.
        self.stopped_sends: set[str] = set()
        self.hiding = False
        # When a turn of it was last stopped, and what ends what that turn was still starting
        # as it was stopped (stop_background, sweep).
        self.stopped_at = 0.0
        self.sweeper: asyncio.Task | None = None
        # The usage its thread restored when resumed in this process: its epoch's offset.
        self.restored: dict[str, dict] = {}
        # The ids of the messages Chief of Staff sent it (a message with another id, or none,
        # is never the owner's), and turns it did not start that were stopped at once: none of
        # their requests is granted, nothing of them is shown.
        self.sent: set[str] = set()
        self.refused_turns: set[str] = set()
        # The MCP calls asked about, by server, not yet ended (each one asks first, if so); and
        # the servers the owner was asked to sign in to.
        self.asked_mcp: dict[str, int] = {}
        self.signing_in: set[str] = set()
        # Its child agents (threads it spawned, also before this process): the only threads
        # besides its own its turns may reach.
        self.tree: set[str] = {thread_id, *(options.get("children") or [])}
        # The repository's instructions it is to have (thread_config), and what tells those it
        # was given (kept by Chief of Staff, as its thread keeps them across processes); due
        # again once Codex summarized its conversation, which may have left them out.
        self.docs: str = options.get("repository_docs") or ""
        self.docs_given: str = options.get("docs_given") or ""
        self.docs_due = False
        # Let go of (disconnect) while something of it ran, and now nothing does: its events
        # are no longer followed.
        self.finished = False

    async def give_docs(self):
        """The repository's instructions, as a message of its conversation: once, then again
        only when they changed, or once its conversation was summarized (raises why it could
        not)."""
        digest = docs_digest(self.docs)
        if digest == self.docs_given and not (self.docs_due and self.docs):
            self.docs_due = False
            return
        cwd = self.options.get("working_directory") or ""
        item = repository_message(
            cwd, self.docs, replacing=bool(self.docs_given) and digest != self.docs_given,
            again=self.docs_due,
        )  # fmt: skip
        try:
            await self.backend.request(
                "thread/inject_items", {"threadId": self.session_id, "items": [item]}
            )
        except CodexError as exc:
            raise RuntimeError(
                f"Codex could not give the worker its repository's instructions: {exc}"
            ) from exc
        self.docs_given, self.docs_due = digest, False
        if callable(keep := self.options.get("on_repository_docs")):
            keep(digest)

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
            "sent",
            "refused_turns",
            "asked_mcp",
            "docs_given",
            "docs_due",
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

    def account(self, kind: str, /, **data: Any):
        """What its turns spent, and that they spend no more (agent.usage, agent.settled): told
        Chief of Staff (on_accounting) whatever became of this handle, let go of or its worker
        closed mid-turn, for what was spent is spent; without that, as its other events."""
        if callable(tell := self.options.get("on_accounting")):
            tell(kind, event(kind, **data).data)
        else:
            self.emit(kind, **data)

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
        self.sent.add(sent)
        try:
            await self.backend.request("turn/steer", params)
        except CodexError:
            self.steered.discard(sent)
            return False
        return True

    async def start_turn(self, items: list[dict], sent: str):
        """A turn on these, once Chief of Staff admits it; refused, it raises why. Admitted but
        not begun, nothing of it spends."""
        self.starting = admitted = True
        try:
            if self.backend.admit and (why := await self.backend.admit(self)):
                admitted = False
                raise RuntimeError(why)
            if self.docs_due:
                try:
                    await self.give_docs()
                except RuntimeError:
                    logger.warning("Codex: the repository's instructions were not given again")
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
            self.sent.add(sent)
            if self.hiding:
                self.stopped_sends.add(sent)  # Stopped as it started.
            future = self.backend.send_request("turn/start", params)
            answer = await self.backend.answer("turn/start", future)
            turn = (answer.get("turn") or {}).get("id")
            if turn and not self.turn and turn not in self.ended:
                self.turn = turn  # Its events may come before this answer is read: not after.
                self.turns.add(turn)
        except BaseException:
            self.starting = False
            if not self.turn:
                self.hiding = False  # No turn of it began: none to stop.
            if admitted:
                self.spent()  # Admitted, it began nothing.
            raise
        finally:
            self.starting = False

    async def abort(self) -> None:
        """Its turn ends, and its child agents' with it; what it left running goes on until
        stopped (stop_background). What waited for the turn's end is dropped."""
        self.queue.clear()
        self.stopped_sends |= self.sent
        if self.turn or self.starting:
            self.hiding, self.unshown = True, ""
            self.stopped_at = time.monotonic()
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
        self.backend.release_if_idle()
        if self.pump and self.pump is not asyncio.current_task():
            self.pump.cancel()

    def idle(self) -> bool:
        """Nothing of it runs: no turn, no command it left running, no child agent at work."""
        return not (self.turn or self.starting or self.left_running or self.child_turns)

    async def finish_closing(self):
        """Let go of while something of it ran (disconnect): once nothing does, it is let go
        of as disconnect lets go of one at rest, and its worker's process may end."""
        if (
            not self.closed
            or not self.idle()
            or self.backend.sessions.get(self.session_id) is not self
        ):
            return
        del self.backend.sessions[self.session_id]
        self.finished = True
        with suppress(Exception):
            await self.backend.request("thread/unsubscribe", {"threadId": self.session_id})
        self.backend.release_if_idle()

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
                ours = str(item.get("clientId") or "").startswith("cos-")
                if item.get("type") == "userMessage" and ours and not text.startswith(REFUSED_NOTE):
                    found.append(event("user.message", content=text))
                elif item.get("type") == "agentMessage" and item.get("text"):
                    found.append(event("assistant.message", content=item["text"]))
        return found

    async def running_background(self) -> int:
        return len(self.left_running) + len(self.child_turns) * (self.turn is None)

    async def stop_background(self) -> int:
        """Ends the commands its turns left running, and child agents still at work after its
        turn ended; how many it ended. Codex keeps a turn's commands running when the turn is
        interrupted (0.167.2): what each of its threads runs is asked of Codex (its own thread
        first), not only what its turns' ends have shown here. A thread that cannot be asked
        does not keep the others' from being ended; one Codex no longer has (a child agent it
        closed) is let go of."""
        stopping = 0
        others = sorted({*self.left_running.values(), *self.child_threads} - {self.session_id})
        for thread in [self.session_id, *others]:
            try:
                listed = await self.backend.request(
                    "thread/backgroundTerminals/list", {"threadId": thread}
                )
                if found := listed.get("data"):
                    await self.backend.request(
                        "thread/backgroundTerminals/clean", {"threadId": thread}
                    )
                    stopping += len(found)
            except CodexError as exc:
                if thread != self.session_id and "not found" in str(exc):
                    await self.let_go(thread)
            except ConnectionError:
                break  # Codex stopped: what it ran ended with it.
        if self.turn is None:
            for child, turn in list(self.child_turns.items()):
                if not turn:
                    continue
                with suppress(Exception):
                    await self.backend.request(
                        "turn/interrupt", {"threadId": child, "turnId": turn}
                    )
                    stopping += 1
        if time.monotonic() - self.stopped_at < LATE_TERMINALS and self.sweeper is None:
            self.sweeper = asyncio.ensure_future(self.sweep())
            self.backend.tasks.add(self.sweeper)
            self.sweeper.add_done_callback(self.backend.tasks.discard)
        return stopping

    async def sweep(self):
        """A turn stopped as it was starting a command: Codex keeps that command running as a
        terminal of its thread (0.167.2), which may show only after stop_background looked. It
        is ended as it shows, for a short while, unless a turn starts meanwhile (what runs then
        is that turn's)."""
        turns = len(self.turns)
        try:
            while time.monotonic() - self.stopped_at < LATE_TERMINALS:
                await asyncio.sleep(LATE_TERMINALS_EVERY)
                if self.starting or len(self.turns) != turns:
                    return
                thread = {"threadId": self.session_id}
                listed = await self.backend.request("thread/backgroundTerminals/list", thread)
                if listed.get("data"):
                    await self.backend.request("thread/backgroundTerminals/clean", thread)
        except (CodexError, ConnectionError):
            return  # Its process ended (what it ran ended with it), or Codex cannot say.
        finally:
            self.sweeper = None

    async def let_go(self, thread: str):
        """A child thread Codex no longer has (closed): nothing of it can be followed or ended
        through Codex now. Its commands left running are settled without an exit code, and its
        turn as cut short."""
        self.child_threads.discard(thread)
        settled = False
        for item_id, owner in list(self.left_running.items()):
            if owner == thread:
                del self.left_running[item_id]
                self.items.pop(item_id, None)
                kind = SimpleNamespace(type="shell_completed", shell_id=item_id, exit_code=None)
                self.emit("system.notification", kind=kind)
                settled = True
        if thread in self.child_turns:
            await self.child_ended(thread, {"status": "interrupted"})
        elif settled:
            self.after_background()

    async def take_queued(self) -> int:
        taken, self.queue = len(self.queue), []
        return taken

    async def queue_holds(self) -> bool:
        return bool(self.queue)

    # Events, in the order Codex sent them

    async def handle_events(self):
        while not self.finished:
            method, params, thread = await self.inbox.get()
            try:
                await self.handle(method, params, thread)
            except Exception:
                logger.exception("Handling Codex's %s failed.", method)
            finally:
                self.read += 1

    async def handle(self, method: str, params: dict, thread: str):
        child = thread != self.session_id
        if (params.get("turnId") or (params.get("turn") or {}).get("id")) in self.refused_turns:
            if method == "turn/completed":
                self.spent()  # Admitted before it was stopped, it spends no more.
            return  # A turn it was stopped in: nothing of it is shown.
        if method == "turn/started":
            turn = (params.get("turn") or {}).get("id", "")
            if child:
                self.child_started(thread, turn)
                return
            if not await self.solicited(params.get("turn") or {}):
                return
            self.turn = turn
            self.turns.add(turn)
            self.unshown = "" if self.hiding else turn
            return
        if not child and (
            method == "thread/compacted"
            or (
                method == "item/completed"
                and (params.get("item") or {}).get("type") == "contextCompaction"
            )
        ):
            self.docs_due = True  # Given again before its next turn.
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
            self.account(
                "agent.usage",
                thread_id=thread,
                turn_id=params.get("turnId", ""),
                total=usage.get("total") or {},
                last=usage.get("last") or {},
                model=self.current_model,
                child=child,
                epoch=self.backend.epoch,
                restored=self.restored.pop(thread, None),
            )
        elif method == "mcpServer/startupStatus/updated" and not child:
            self.mcp_started(params)
        elif method == "model/rerouted" and not child:
            self.current_model = params.get("toModel") or params.get("model") or self.current_model
        elif method == "thread/closed" and child:
            await self.let_go(thread)

    def mcp_started(self, params: dict):
        """One of its MCP servers did not start for want of the owner's sign-in: the owner is
        asked as for Copilot's, with what signs in (Codex keeps the sign-in; Chief of Staff
        never sees it). Once per server."""
        name, error = str(params.get("name") or ""), str(params.get("error") or "")
        signs_in = params.get("failureReason") == "reauthenticationRequired" or bool(
            NOT_SIGNED_IN.search(error)
        )
        if params.get("status") != "failed" or not signs_in or not name or name in self.signing_in:
            return
        self.signing_in.add(name)
        if ask := self.options.get("on_mcp_auth_request"):
            request = {"serverName": name, "command": f"codex mcp login {shlex.quote(name)}"}
            task = asyncio.ensure_future(ask(request, {"sessionId": self.session_id}))
            self.backend.tasks.add(task)
            task.add_done_callback(self.backend.tasks.discard)

    async def known_turn(self, turn: str, thread: str) -> bool:
        """Whether a request comes from a turn that may run: on its own thread, once its start
        has been told apart (its events are handled in order, a request at once)."""
        if not turn or thread != self.session_id:
            return turn not in self.refused_turns
        for _ in range(300):
            if turn in self.refused_turns:
                return False
            if turn == self.turn or turn in self.turns:
                return True
            await asyncio.sleep(0.01)
        return False

    def reached_out(self, tool: str, threads: list[str]):
        """Its turn tried to reach a thread outside its own tree (another worker's, or one of
        the owner's own Codex sessions, read from disk): its worker stops at once."""
        logger.warning("%s tried %s on %s: stopped.", self.session_id, tool, ", ".join(threads))
        self.stop_worker(
            "Stopped: it tried to reach a Codex conversation that is not its own (another "
            "worker's, or one of yours), which Chief of Staff does not allow."
        )

    def stop_worker(self, why: str):
        """Something Chief of Staff does not let a worker do: its process ends at once, with
        whatever it loaded and runs, nothing more of its turn is granted, and its worker stops
        with why (Runtime.agent_process_ended)."""
        if self.turn:
            self.refused_turns.add(self.turn)
        self.backend.refusal = self.backend.refusal or why
        task = asyncio.ensure_future(self.backend.stop())
        self.backend.tasks.add(task)

    async def solicited(self, turn: dict) -> bool:
        """Whether a turn that started on its thread may run: one Chief of Staff starts, or one
        Codex starts on the worker's own work (it follows a turn of its own: admitted as any).
        Any other (another thread's input) is stopped at once, and none of its requests is
        granted."""
        turn_id, led_by = turn.get("id", ""), turn.get("rootTurnId") or ""
        if turn_id == self.turn or turn_id in self.turns:
            return True
        if self.starting and led_by in {"", turn_id}:
            return True  # Its own, its start not answered yet.
        why = "Chief of Staff did not start this turn."
        if led_by and led_by != turn_id and led_by in self.turns:
            why = await self.backend.admit(self) if self.backend.admit else ""
            if not why and (not self.backend.begins or self.backend.begins(self)):
                return True
            why = why or "Chief of Staff's settings changed."
        await self.refuse_turn(turn_id, why)
        return False

    async def refuse_turn(self, turn_id: str, why: str):
        self.refused_turns.add(turn_id)
        logger.warning("Stopped a turn of %s at its start: %s", self.session_id, why)
        with suppress(Exception):
            await self.backend.request(
                "turn/interrupt", {"threadId": self.session_id, "turnId": turn_id}
            )
        self.emit("agent.notice", text=f"Stopped a turn Codex began on its own: {why}")

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
        self.spent()
        self.after_background()
        await self.finish_closing()

    def spent(self):
        """Nothing of it spends now (no turn of its own, no child agent at work): what its
        turns spent has all been said (Codex says a turn's usage before its end)."""
        if self.turn is None and not self.starting and not self.child_turns:
            self.account("agent.settled")

    def after_background(self):
        """Something its turn left (a command, a child agent) ended: once nothing is left and no
        turn runs, its session is idle."""
        if self.turn is None and not self.child_turns and not self.left_running:
            self.emit("session.idle")
            if self.closed:
                self.backend.release_if_idle()  # Let go of meanwhile: its process may end.

    def item_started(self, item: dict, thread: str):
        kind, item_id = item.get("type"), item.get("id", "")
        parent = self.parent_call(thread)
        if kind == "userMessage" and not parent:
            sent = item.get("clientId") or ""
            if sent in self.own or sent not in self.sent:
                # The adapter's own note, or input Chief of Staff did not send (from Codex,
                # another thread): never shown as the owner's, nor taken up as a task.
                if self.unshown:
                    self.show_turn()
                return
            delivery = Taken(self, sent, sent in self.steered)
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
                self.spent()
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
        if (
            kind == "mcpToolCall"
            and self.options.get("ask_first")
            and not self.options.get("checking")  # The check finds out what asks itself.
            and self.ran_unasked(item)
        ):
            server = item.get("server") or "an MCP server"
            self.stop_worker(
                f"Stopped: Codex ran a tool of {server} without asking first, though this worker "
                "asks first. Check that server's settings in Codex, or let it act on its own."
            )
            return
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
        try:
            await self.end_turn(turn)
        finally:
            await self.finish_closing()

    async def end_turn(self, turn: dict):
        status = turn.get("status")
        await self.settle_items(status == "interrupted")
        self.turn = None
        self.hiding = False
        self.ended.add(turn.get("id", ""))
        self.spent()
        if self.closed:
            self.queue.clear()  # Let go of: what it was sent is sent again (Delivery).
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

    async def request(self, method: str, params: dict, thread: str, told: int = 0) -> dict:
        if not await self.known_turn(params.get("turnId") or "", thread):
            raise CodexError("Chief of Staff did not start this turn.")  # Declined, all of it.
        context = {"session_id": self.session_id, "sessionId": self.session_id}
        if method == "item/commandExecution/requestApproval":
            return await self.command_approval(params, context)
        if method == "item/fileChange/requestApproval":
            return await self.change_approval(params, context)
        if method == "mcpServer/elicitation/request":
            return await self.elicitation(params, context)
        if method == "item/tool/call":
            # Chief of Staff's tools act on what came before (a report on its commands' ends,
            # say): answered once that is read, as Copilot's come in order.
            for _ in range(1000):
                if self.read >= told:
                    break
                await asyncio.sleep(0.01)
            else:
                logger.warning("Codex: a tool answered before what came before it was read.")
            # What that emitted reaches Chief of Staff on the loop (call_soon): handled first.
            await asyncio.sleep(0)
            return await self.tool_call(params)
        if method == "item/tool/requestUserInput":
            return await self.user_input(params, context)
        if method == "item/permissions/requestApproval":
            # More access than full access is not a thing; nothing more is granted.
            return {"permissions": {}}
        if method not in self.unhandled:
            self.unhandled.add(method)
            self.emit(
                "agent.notice",
                text=f"Codex asked for something Chief of Staff does not handle ({method}); "
                "it was told no.",
            )
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
            # A move (apply_patch's `*** Move to:`) writes its destination too.
            moved = mapping(change.get("kind")).get("move_path")
            for path in [str(change.get("path") or ""), *([moved] if moved else [])]:
                request = SimpleNamespace(
                    kind="write",
                    file_name=str(path),
                    resolved_path=str(path),
                    diff=change.get("diff") or "",
                    intention=params.get("reason") or "",
                    can_offer_session_approval=False,
                    tool_call_id=params.get("itemId"),
                )
                if await self.permitted(request, context):
                    return {"decision": "decline"}
        return {"decision": "accept"}

    def ran_unasked(self, item: dict) -> bool:
        """Whether this MCP call ran with no question about it (Ask first: each one asks)."""
        server = str(item.get("server") or "")
        if self.asked_mcp.get(server):
            self.asked_mcp[server] -= 1
            return False
        return item.get("status") == "completed" or item.get("result") is not None

    async def elicitation(self, params: dict, context: dict) -> dict:
        meta = mapping(params.get("_meta"))
        server = params.get("serverName") or ""
        if meta.get("codex_approval_kind") == "mcp_tool_call":
            # Asked before its call runs (its end comes only once this is answered).
            self.asked_mcp[server] = self.asked_mcp.get(server, 0) + 1
            read_only = bool(mapping(meta.get("tool_annotations")).get("readOnlyHint"))
            github = self.github(server) or (server == CODEX_APPS and github_app(meta))
            if (
                server == CODEX_APPS
                and not github
                and not read_only
                and self.options.get("ask_first")
            ):
                # Apps are off for it (read_back): one that still asks is not let act.
                await self.tell("ChatGPT's apps do not act for a worker that asks first.")
                return {"action": "decline"}
            request = SimpleNamespace(
                kind="mcp",
                server_name=GITHUB_MCP if github else server,
                tool_name=meta.get("tool_name") or "",
                tool_title=meta.get("tool_name") or "",
                read_only=read_only,
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


def codex_mcp(entry: Any) -> dict | None:
    """An MCP server as Chief of Staff's Settings have it (Copilot's form: a command, or a URL;
    `tools` it may use, `*` for all), as Codex's config has one; None for one it cannot run."""
    entry = mapping(entry)
    found: dict[str, Any] = {}
    if isinstance(url := entry.get("url"), str) and url:
        found["url"] = url
        if headers := mapping(entry.get("headers")):
            found["http_headers"] = {str(k): str(v) for k, v in headers.items()}
    elif isinstance(command := entry.get("command"), str) and command:
        found["command"] = command
        if args := entry.get("args"):
            found["args"] = [str(arg) for arg in args]
        if env := mapping(entry.get("env")):
            found["env"] = {str(k): str(v) for k, v in env.items()}
        if isinstance(cwd := entry.get("cwd"), str) and cwd:
            found["cwd"] = cwd
    else:
        return None
    tools = entry.get("tools") or ["*"]
    if isinstance(tools, list) and "*" not in tools:
        found["enabled_tools"] = [str(tool) for tool in tools]
    return found


def feature_on(config: dict, name: str) -> bool:
    """Whether a config (config/read's) has the feature on: `features.<name>` true, or a table
    of it whose `enabled` is not false."""
    value = mapping(config.get("features")).get(name)
    if isinstance(value, dict):
        return value.get("enabled") is not False
    return value is True


def protocol_missing(folder: Path) -> list[str]:
    """What of CODEX_SENDS, CODEX_NOTIFIES, CODEX_ANSWERS and CODEX_FOLLOWS the protocol schema
    in `folder` does not have: "<method>" or "<method> <field>"."""

    def methods(name: str) -> dict[str, set[str]]:
        whole = json.loads((folder / name).read_text())
        definitions = whole.get("definitions") or {}
        found = {}
        for entry in whole.get("oneOf") or []:
            method = entry["properties"]["method"]["enum"][0]
            params = (entry["properties"].get("params") or {}).get("$ref", "")
            schema = definitions.get(params.rsplit("/", 1)[-1], {}) if params else {}
            found[method] = set(schema.get("properties") or {})
        return found

    requests = methods("ClientRequest.json")
    missing = []
    for method, fields in CODEX_SENDS.items():
        if method not in requests:
            missing.append(method)
        else:
            missing += [f"{method} {field}" for field in fields if field not in requests[method]]
    for name, used in (
        ("ClientNotification.json", CODEX_NOTIFIES),
        ("ServerRequest.json", CODEX_ANSWERS),
        ("ServerNotification.json", CODEX_FOLLOWS),
    ):
        known = methods(name)
        missing += [method for method in used if method not in known]
    return missing


def check_verdict(
    version: str, asked: list[str], happened: list[str], subagents: list[str] | None = None
) -> dict:
    """What a check showed (CodexBackend.check): why workers that ask first may not run on
    this version (an action not asked about, one that happened though refused, subagents
    offered to its thread), and why none may (an edit that did not ask, or happened though
    refused)."""
    wanted = {
        "tool": "a call of Chief of Staff's own tools",
        "shell": "a command",
        "mcp": "an MCP tool's call",
        **{f"write {name}": "a new file" for name in codex_check.ADDED},
        f"write {codex_check.MOVED_FROM}": "a file moved",
        f"write {codex_check.MOVED_TO}": "a file moved (where to)",
    }
    missing = list(dict.fromkeys(what for key, what in wanted.items() if key not in asked))
    edits = [key for key in wanted if key.startswith("write ")]
    unsafe = any(key not in asked for key in edits) or any(
        name in {*codex_check.ADDED, codex_check.MOVED_TO} for name in happened
    )
    said = f"Chief of Staff's check of Codex {version}"
    ask_first = ""
    if missing or happened or subagents:
        found = []
        if missing:
            found.append("did not ask first for " + ", ".join(missing))
        if happened:
            found.append("did what was refused (" + ", ".join(happened) + ")")
        if subagents:
            found.append("offered subagents though they were turned off")
        ask_first = (
            f"{said} found that it {' and '.join(found)}: workers that ask first do not run on it."
        )
    acting = (
        f"{said} found that its edits do not all ask Chief of Staff first, which keeps your "
        "checkouts unchanged: no worker runs on it. Use a tested version."
        if unsafe
        else ""
    )
    return {"ran": True, "ask_first": ask_first, "acting": acting}


def codex_backend(command: str = "codex") -> CodexAgent:
    return CodexAgent(shutil.which(command) or command)


async def look(command: str = "codex") -> dict:
    """What Settings shows of the Codex on this computer, from an app-server started for this
    and stopped: its version, how it is signed in and its models; raises with why its workers
    cannot run (not found, too old, signed out, an administrator's requirements)."""
    backend = CodexBackend(shutil.which(command) or command)
    backend.experimental = False
    try:
        await asyncio.wait_for(backend.start(), LOOK_SECONDS)
        return await asyncio.wait_for(backend.about(), LOOK_SECONDS)
    except TimeoutError:
        raise RuntimeError("Codex did not answer in time: try again.") from None
    finally:
        with suppress(Exception):
            await backend.stop()
