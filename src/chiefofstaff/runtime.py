"""Chief of Staff's session runtime.

One Router, one Slack session, a Watchdog and any number of workers, all long-lived Copilot
sessions driven through the Copilot SDK. The Router only routes; workers do the work in their
own folders; the Slack session drafts replies. Everything a session says is recorded for the
UI, and anything that speaks publicly as the owner waits for the owner's approval.

Runtime owns the Copilot connection and each session's lifecycle (create, resume, rotate), and
wires together the parts that do the rest, each in its own module:

    events.py        what sessions report as they work
    delivery.py      getting messages to sessions: stored first, batched, steered
    workers.py       the worker queue; starting, closing and reopening workers
    task_actions.py  reports, cancellation, review and retries of tasks (tasks.py stores them)
    tools.py         the tools sessions call
    drafts.py        what the owner approves before it is sent in their name
    asks.py          questions to the owner, sign-ins, and questions between sessions
    focus.py         Focus Shield
    watchdog.py      the Watchdog
"""

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Awaitable, Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from . import agents, caches, prompts, settings_guard, threads, updates
from . import workspaces as workspaces_module
from .abilities import Abilities
from .asks import Asks
from .backend import (
    COPILOT_AGENT,
    OLDEST_CLI,
    REFUSED_CLI,
    AgentBackend,
    CopilotBackend,
    SandboxPolicy,
    copilot_backend,
    copilot_home,
    copilot_orphans,
    copilot_ours,
    copilot_records,
    copilot_tree,
    copilot_trusts,
    end_processes,
    older,
    owner_tool_search,
    sandbox_supported,
    shell_init,
    switch_model,
)
from .bus import Bus
from .checks import Checks
from .connector import Connector
from .decisions import Decisions
from .delivery import Delivery
from .digest import digest, needs, task_words
from .drafts import Drafts
from .envelope import (
    UNTRUSTED,
    clean_title,
    language,
    sender,
    short,
    task_named,
    tasks_named,
    wrap,
)
from .events import EFFORT_REFUSED, MODEL_REFUSED, ROUTER_CONTEXT, Events
from .focus import Focus
from .habits import Habits
from .learning import Learning
from .models import AS_SAVED, Attachment, SentItem, Settings
from .notify import mac_notification
from .others import Others
from .peers import Peers
from .policy import (
    AGENT_SETTINGS,
    CODEX_SETTINGS,
    GITHUB_MCP,
    REPOSITORY_SETTINGS,
    SANDBOX_HELD,
    within,
)
from .provenance import ALIVE_EVERY, RUNS_KEPT, Provenance, log_of, runs_of
from .roles import ROUTER, SLACK, WATCHDOG, WORKER
from .send import shell_command
from .sendsocket import LimitedError, SendSocket
from .sendsocket import headline as sent_headline
from .sendsocket import parts as sent_parts
from .sendsocket import prompt as sent_prompt
from .sendsocket import shown as sent_shown
from .settings_guard import SettingsChangedError, checkout_top, outermost_top, read_regular
from .sources import Source
from .sources.github import GitHubSource
from .sources.slack import SlackSource, describe
from .state import SessionStates
from .storage import Storage
from .store import Store, ago_minutes, hours_ago, now
from .task_actions import TaskActions
from .tasks import (
    ENDED,
    HELPER_KINDS,
    OPEN,
    TaskBook,
    settle_adopted,
    settle_decision_changes,
    settle_followups,
)
from .tasks import migrate as migrate_tasks
from .tools import Tools
from .watchdog import WatchConfig, Watchdog
from .workers import SETTINGS_STOP, SETTINGS_WAIT, Workers
from .workspaces import (
    ABSENT_HELD_CLI,
    BRANCH_PREFIX,
    Workspaces,
    common_git_dir,
    git_dirs,
    holds_alternates,
    identity_of,
    own_code,
    protected_git_paths,
    registry_secrets,
    replaceable_git_paths,
    sandbox_paths,
    sandbox_platform,
    service_secrets,
    too_broad,
    watched_git_paths,
)

logger = logging.getLogger(__name__)

# How long a look at a worker's folder (settings_refusal, the backstop) may take: past that it
# can't be checked, and the worker waits (looked at again as workers start: Workers.pump).
SETTINGS_SECONDS = float(os.environ.get("CHIEFOFSTAFF_SETTINGS_SECONDS", "15"))
# The threads those looks read the file system in, and how many may be under way or waiting
# for one at once: past that a look is not started (it can't be checked now), so looks past
# their time never pile up. Chief of Staff's own git guard has threads of its own.
SETTINGS_THREADS = 4
SETTINGS_WAITING = 16
GUARD_THREADS = 2


def clip_words(text: str, size: int) -> str:
    """Text cut to `size` at a word, marked with "…" (not cut mid-word without a sign)."""
    if len(text) <= size:
        return text
    cut = text[: size - 1]
    if (space := max(cut.rfind(" "), cut.rfind("\n"))) > size - 200:
        cut = cut[:space]
    return cut.rstrip() + "\u2026"


# A repository's own Copilot configuration that Copilot loads only in a trusted folder.
TRUST_GATED = REPOSITORY_SETTINGS


def needs_trust(top: Path, cwd: Path) -> bool:
    """Whether the repository (at its top or in the folders down to cwd) has such config."""
    folders = [cwd, *[parent for parent in cwd.parents if parent.is_relative_to(top)]]
    return any((folder / name).exists() for folder in folders for name in TRUST_GATED)


# Copilot's own threshold for deferring MCP tools behind tool search (its SDK's default).
COPILOT_DEFER_THRESHOLD = 30

PROMPTS = {
    ROUTER: prompts.ROUTER,
    SLACK: prompts.SLACK,
    WORKER: prompts.WORKER,
    WATCHDOG: prompts.WATCHDOG,
}
# How long after its start Chief of Staff first looks whether a newer one is out (updates.py),
# and how often an update waiting for the running work to end looks again.
UPDATE_DELAY = 60
UPDATE_POLL = 2.0

ROUTER_TOOLS = [
    "spawn_worker",
    "ask_workspace",
    "message_session",
    "list_sessions",
    "find_sessions",
    "notify_owner",
    "close_worker",
    "distill_profile",
    "read_session",
    "answer_session",
    "cancel_task",
    "stop_worker",
    "stop_following",
    "add_watch",
    "rename_task",
    "split_task",
    "record_decision",
    "check_prs",
]
# Files opened from a report or message show at most this much.
MAX_FILE_SHOWN = 1_000_000
# How much of the conversation a fresh Router session is given (characters; about 15k tokens).
HANDOVER_CHARS = 60_000
# The owner's images: what each type starts with, and its file extension.
IMAGE_MAGIC = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
    "image/gif": b"GIF8",
    "image/webp": b"RIFF",
}
IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
MAX_ATTACHMENT = 5 * 1024 * 1024
ATTACHMENT_NAME = re.compile(r"\b([0-9a-f]{32}\.(?:png|jpg|gif|webp))\b")
# Workers Chief of Staff starts for itself that have nothing but their own tools: they read
# quoted data (the owner's words, test output) and propose; they run nothing.
TOOLLESS = {"learner": ["update_style", "propose_lessons"], "checker": ["propose_checks"]}
SPECIALS = HELPER_KINDS
CONNECTOR_TOOLS = ["report_found", "report_owner_words"]
# The messenger sends away notifications and reads the owner's replies to them; through Chief of
# Staff's Slack connection (when there is one) only to and from the owner's own DM: no Slack
# tools of its own.
MESSENGER_TOOLS = ["report_notified", "report_owner_replies"]
SLACK_DM_TOOLS = ["send_owner_slack_dm", "read_owner_slack_dm"]
# Owner messages at least this long are instructions the learner looks at ("progress?" is not).
INSTRUCTION_CHARS = 25
# A message to a worker this long into a step gets a note that it waits for the step to end.
STEP_NOTE_SECONDS = 20
# Tasks that ended within this many hours are still shown (and can be reviewed or retried).
TASKS_SHOWN_HOURS = 72
# A task its worker is on (or will be, once the owner's answer comes).
OPEN_WORK = frozenset({"running", "waiting"})
# The same message again within this long is not shown twice.
REPEAT_SECONDS = 60
# Another agent's sandbox, until its version's check showed it holding (agent_refused).
SANDBOX_UNPROVEN = "its sandbox has not passed Chief of Staff's checks yet"
GITHUB_HOSTS = (
    "github.com",
    "api.github.com",
    "uploads.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
)
# How a configured project whose folder is gone is named to the Router: it is not "not a
# configured project".
MISSING_PROJECT = (
    "(configured, but its folder is not on disk: moved or deleted? Tell the owner; they "
    "restore it or remove it in Settings)"
)
# Copilot will not start a session on the model named in its error ('Model "x.y" is not
# available': a model id has dots, which MODEL_REFUSED's pattern does not cross).
MODEL_NOT_OFFERED = re.compile(r"\bnot (?:available|supported|enabled|allowed)\b", re.IGNORECASE)
# How often the Copilot CLI is checked to still run (seconds), and how long reconnecting waits
# after a failure (doubling up to the most).
PROCESS_CHECK = float(os.environ.get("CHIEFOFSTAFF_PROCESS_CHECK", "2"))
RECONNECT_DELAY = float(os.environ.get("CHIEFOFSTAFF_RECONNECT_DELAY", "3"))
RECONNECT_MOST = 300.0
# Items sent with Send to Chief: at most this many in a while (seconds) are taken; the same
# note and capture again within SENT_AGAIN_SECONDS is the command run twice.
SENT_RATES = ((60, 10, "a minute"), (86400, 300, "a day"))
SENT_AGAIN_SECONDS = 600


# How long a turn of another agent waits for what the last one spent (seconds).
USAGE_SETTLES = 60.0
# How soon an agent's check that could not run is tried again, at the earliest (seconds); and
# how soon after its last a worker's process stopping by itself again fails it instead of
# starting another (never a loop of starts).
AGENT_CHECK_AGAIN = 60.0
AGENT_LOST_AGAIN = 120.0


def clock_time(at: float) -> str:
    """A time of day as the page says it: "7:17 AM" (this computer's time)."""
    return datetime.fromtimestamp(at, UTC).astimezone().strftime("%I:%M %p").lstrip("0")


def counters(totals: Any) -> dict[str, int]:
    """An agent's cumulative token counts (Codex's `tokenUsage.total`), as the ledger keeps
    them; `tokens`: what counts toward a limit (input and output)."""
    totals = totals if isinstance(totals, dict) else {}

    def count(name: str) -> int:
        value = totals.get(name)
        return value if isinstance(value, int) and value > 0 else 0

    found = {
        "input": count("inputTokens"),
        "cached": count("cachedInputTokens"),
        "cache_write": count("cacheWriteInputTokens"),
        "output": count("outputTokens"),
    }
    return found | {"tokens": count("totalTokens") or found["input"] + found["output"]}


def difference(now: dict[str, int], then: dict[str, int]) -> dict[str, int]:
    return {name: max(0, value - then.get(name, 0)) for name, value in now.items()}


@dataclass
class AgentConnection:
    """Another coding agent's backend in the registry (Runtime.backends), started on first use:
    while it runs, and how many times it was disconnected, as Runtime.backend and
    Runtime.connections are Copilot's."""

    start: Callable[[], AgentBackend]
    backend: AgentBackend | None = None
    connections: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    watch: asyncio.Task | None = None
    # Whether the owner has it turned on (Settings → Models): off, none of its workers runs.
    enabled: Callable[[], bool] = lambda: True
    # It stopped by itself and could not start again: it is tried again with its next worker's
    # message (Settings says so).
    offline: bool = False


class IneligibleError(RuntimeError):
    """A worker on another agent may not run now (Runtime.agent_block says why)."""


class OpenElsewhereError(RuntimeError):
    """A session is open in another Copilot window (a terminal): `pid` runs it."""

    def __init__(self, message: str, pid: int):
        super().__init__(message)
        self.pid = pid


def span(iso: str) -> str:
    """How long since an ISO time, in words: "under a minute", "1 min", "12 min"."""
    minutes = ago_minutes(iso)
    return f"{minutes} min" if minutes else "under a minute"


def read_text_file(raw: str, cwd: str, roots: list[str]) -> dict:
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        if not cwd:
            raise ValueError("That path is relative; open it from the session that wrote it.")
        path = Path(cwd) / path
    resolved = path.resolve()
    if not any(within(str(resolved), root) for root in roots if root):
        raise ValueError("Only files in workers' own folders open here.")
    if not resolved.is_file():
        raise ValueError("That is not a file (any more).")
    size = resolved.stat().st_size
    with resolved.open("rb") as handle:
        data = handle.read(MAX_FILE_SHOWN)
    if b"\0" in data[:8000]:
        raise ValueError("That is not a text file.")
    return {
        "path": str(resolved),
        "name": resolved.name,
        "text": data.decode("utf-8", errors="replace"),
        "truncated": size > MAX_FILE_SHOWN,
    }


def default_backend(settings: Settings) -> AgentBackend:
    return copilot_backend(settings.copilot_command)


def default_effort(info: dict) -> str:
    """The model's default effort; Copilot does not always say, and medium is the usual one."""
    efforts = info["efforts"]
    if info["default_effort"] in efforts:
        return info["default_effort"]
    return "medium" if "medium" in efforts else efforts[len(efforts) // 2]


def describe_model(model: Any) -> dict:
    """The model facts the UI and session options need."""
    prices = model.billing.token_prices if model.billing else None
    long = prices.long_context if prices else None
    limits = model.capabilities.limits
    return {
        "id": model.id,
        "name": model.name,
        "efforts": list(model.supported_reasoning_efforts or []),
        "default_effort": model.default_reasoning_effort or "",
        "context": (prices and (prices.max_prompt_tokens or prices.context_max))
        or limits.max_prompt_tokens
        or 0,
        "long_context": (long and (long.max_prompt_tokens or long.context_max)) or 0,
    }


def connection(
    connect: Callable[["Runtime"], Awaitable[None]],
) -> Callable[["Runtime"], Awaitable[None]]:
    """Runtime.connect: Runtime.connecting is done once it is over (it ran, failed or was
    cancelled), for what waits for it (Runtime.until_connected)."""

    async def connecting(runtime: "Runtime") -> None:
        pending = runtime.connecting
        mine = (
            pending
            if pending and not pending.done()
            else asyncio.get_running_loop().create_future()
        )
        runtime.connecting = mine
        try:
            await connect(runtime)
        finally:
            if not mine.done():
                mine.set_result(None)

    return connecting


def model_price(model: Any) -> tuple[float, float]:
    """What a model's tokens cost (input, output), at its default context; unknown: the most."""
    prices = model.billing.token_prices if getattr(model, "billing", None) else None

    def price(name: str) -> float:
        value = getattr(prices, name, None)
        return float(value) if isinstance(value, int | float) else float("inf")

    return price("input_price"), price("output_price")


def described(model: Any) -> dict:
    """One of another agent's models, as describe_model has Copilot's, and whether it is that
    agent's default."""
    return describe_model(model) | {"default": bool(getattr(model, "is_default", False))}


def identity_digest(paths: list[str]) -> str:
    """What these .git folders are now, in a few characters (the owner's word is for these)."""
    found = {path: identity_of(path) for path in sorted(paths)}
    return hashlib.sha256(json.dumps(found).encode()).hexdigest()[:16]


def git_name(path: str) -> str:
    """A path in a .git, as its line names it ("config.worktree", "worktrees/x/commondir")."""
    marker = f"{os.sep}.git{os.sep}"
    return path.rsplit(marker, 1)[1] if marker in path else Path(path).name


def home_shown(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home + os.sep) else path


# A command that updates a branch (and what it updates from): the subcommand as a whole word
# (not `git merge-base`, `git merge-tree`).
BRANCH_UPDATE = re.compile(
    r"\bgit\b(?:\s+(?:-[cC]\s+\S+|--[\w-]+(?:=\S+)?))*"
    r"\s+(merge|rebase|pull|cherry-pick|am|revert)(?![\w-])([^;&|\n]*)"
)
# What a failed one's output says when git could not change a file of the work tree (one the
# sandbox holds): git's own words for each file, as checkout and merge say them. Not an ordinary
# conflict, which the worker resolves (or aborts) itself; not a file of a `.git` (FETCH_HEAD);
# nor any update whose fetch was refused (an SSH key the sandbox keeps unreadable): nothing
# changed then.
UNCHANGEABLE = re.compile(
    r"(?:unable to (?:unlink(?: old)?|create (?:file|symlink)|rmdir)|cannot create directory at)"
    r" '?(?P<path>[^'\n]*?)'?: (?:operation not permitted|permission denied|read-only file system)",
    re.IGNORECASE,
)
IN_GIT_FOLDER = re.compile(r"(?:^|/)\.git(?:/|$)")
NOT_FETCHED = re.compile(
    r"permission denied \(publickey|could not read from remote repository", re.IGNORECASE
)
UPDATE_FAILED = (
    "That branch update failed partway; put your tree back as it was (e.g. `git merge --abort` / "
    "`git rebase --abort`, or restore what it changed) before going on."
)


def server_labels(servers: list[str]) -> str:
    """Servers (settings_guard: "mcp/name", "lsp/name", "mcp/name@agent"), as the owner reads
    them: "MCP server self", "language server x and MCP server t (of the custom agent a)"."""
    labels = []
    for server in servers:
        kind, _, name = server.partition("/")
        name, _, agent = name.partition("@")
        what = "language server" if kind == "lsp" else "MCP server"
        labels.append(f"{what} {name}" + (f" (of the custom agent {agent})" if agent else ""))
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]


def servers_digest(servers: dict[str, list[str]]) -> str:
    """What a "Use these settings" for servers off names: those servers (not what they run: a
    build between the line and its click changes nothing the owner was asked)."""
    return "servers:" + hashlib.sha256(json.dumps(sorted(servers)).encode()).hexdigest()[:16]


class Runtime:
    def __init__(
        self,
        store: Store,
        bus: Bus,
        *,
        backend_factory: Callable[[Settings], AgentBackend] = default_backend,
        notifier: Callable[[str, str], Awaitable[None]] = mac_notification,
        slack: SlackSource | None = None,
        github: GitHubSource | None = None,
        watch: WatchConfig | None = None,
    ):
        self.store = store
        self.bus = bus
        self.backend_factory = backend_factory
        self.notifier = notifier
        self.slack = slack or SlackSource(store, self)
        self.github = github or GitHubSource(store, self)
        self.watch = watch or WatchConfig.from_env()
        # More event sources (see sources/__init__.py); Slack and GitHub are built in.
        self.sources: list[Source] = []
        self.watch_job: asyncio.Task | None = None
        # Keeps when this run was last known running (see alive).
        self.alive_job: asyncio.Task | None = None
        # Whether a newer Chief of Staff is out (updates.py), looked at once a day.
        self.update_job: asyncio.Task | None = None
        # Updating itself (the owner's Update and restart): the version it goes to, and where
        # that stands ("waiting" for the running work to end, "fetching", "restarting",
        # "failed" with why); the job doing it; and how to stop to start again (__main__).
        self.updating: dict | None = None
        self.updater: asyncio.Task | None = None
        self.restarter: Callable[[updates.Launch], None] | None = None
        self.started_at: float | None = None
        # Watches that Copilot's CLI still runs; reconnects after it stopped by itself, or once
        # the Router is no longer open in a terminal.
        self.process_job: asyncio.Task | None = None
        self.recovery: asyncio.Task | None = None
        self.stopping = False
        self.backend: AgentBackend | None = None
        self.connections = 0  # Counts disconnects: a session opened before one is not usable.
        # Other coding agents' backends, by agent (see backends): those the owner turned on at
        # some point (agents.py); none until then.
        self.agents: dict[str, AgentConnection] = {
            name: self.agent_connection(name) for name in agents.configured(store)
        }
        # Each agent's models, as last listed (Copilot's are in models_cache).
        self.agent_models_cache: dict[str, tuple[float, list[dict]]] = {}
        # Every turn of another agent's worker begins only once admitted (admit_turn), one at a
        # time; what can make a worker ineligible bumps the generation under this lock first.
        self.admission = asyncio.Lock()
        self.admission_generation = 0
        self.admitted: dict[str, int] = {}
        # Workers whose admitted turn has begun, until their agent says nothing of it spends
        # (a turn Codex has not yet said started is stopped too: stop_ineligible).
        self.begun: set[str] = set()
        # Workers Chief of Staff stopped (their spend, a change) whose turn has not settled:
        # what their turn says meanwhile (its start shown late) does not stop them again.
        self.halted: set[str] = set()
        # When each agent's check last ran (recheck_agent), and when each worker's own process
        # last stopped by itself (worker_lost).
        self.agent_checked_at: dict[str, float] = {}
        self.lost_at: dict[str, float] = {}
        # Each agent's processes as last recorded (kv `agentpids:<agent>`).
        self.agent_pids: dict[str, dict[int, str]] = {}
        # Other agents' usage: the run each turn's usage counts for, and the workers being
        # stopped at their limit.
        self.turn_runs: dict[tuple[str, str], str] = {}
        self.spend_stopping: set[str] = set()
        self.states = SessionStates()
        # Whether this machine can run Copilot's command sandbox, and whether gh works inside it.
        self.sandbox_host = sandbox_supported()
        self.sandbox_gh: bool | None = None
        self.connect_lock = asyncio.Lock()
        # Done once the connection under way (a start, or connecting again) is over.
        self.connecting: asyncio.Future | None = None
        self.jobs: set[asyncio.Task] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.status = {"state": "starting", "error": ""}
        self.models_cache: tuple[float, list[dict]] = (0.0, [])
        # Copilot workers that wait for the owner to say to use their folder's changed agent
        # settings (settings_stop): not started from the queue meanwhile; and why, in words.
        self.settings_held: set[str] = set()
        self.settings_reasons: dict[str, str] = {}
        # A worker's settings as the file system said them at its last full check (glance),
        # and where those are: (top, cwd, the files their hooks run and its .git folders).
        self.settings_sigs: dict[str, str] = {}
        self.settings_paths: dict[str, tuple[Path, Path, tuple[str, ...]]] = {}
        # Where those looks read the file system (settings_thread): never the default pool,
        # which the SDK and everything else share; and where Chief of Staff's git guard does
        # (guard_thread), so they never hold up its git.
        self.settings_pool: ThreadPoolExecutor | None = None
        self.settings_room = threading.BoundedSemaphore(SETTINGS_WAITING)
        self.guard_pool: ThreadPoolExecutor | None = None
        # Servers a Copilot worker's folder settings run whose files changed (servers_seen):
        # {session: {server: those files}}; and those its live session was opened without.
        self.servers_off: dict[str, dict[str, list[str]]] = {}
        self.servers_opened: dict[str, frozenset[str]] = {}
        # Copilot workers whose live session was opened with every MCP tool behind tool search
        # (all_searched); the tool count of each MCP server a session loaded; models' prices
        # (input, output), cheapest first when one is chosen for a session (larger_model).
        self.searched_opened: dict[str, bool] = {}
        self.mcp_tools: dict[str, dict[str, int]] = {}
        self.tool_tokens: dict[str, int] = {}  # Its tools' size at its last model call.
        self.model_prices: dict[str, tuple[float, float]] = {}
        # Copilot workers whose session was opened with Chief of Staff's pre-tool hook
        # (pre_tool_wanted): {session: whether it is sandboxed}. The others are glanced at as
        # each tool starts, not waited for.
        self.pre_tool_on: dict[str, bool] = {}
        # The worker commands (ids) whose failed branch update it was told of (update_failed).
        self.updates_said: set[int] = set()
        # Sessions another program (a terminal) has open: session id -> pid. Refreshed by the
        # watch loop and before resuming a session.
        self.elsewhere: dict[str, int] = {}
        # Other agents' conversations open in a terminal, by agent (agent_elsewhere).
        self.elsewhere_on: dict[str, dict[str, int]] = {}
        # What workers open in a terminal are doing, read from their logs (Workers.mirror).
        self.terminal: dict[str, dict] = {}
        self.mirror_job: asyncio.Task | None = None
        self.project_cache: dict[str, tuple[float, dict]] = {}
        self.rotate_lock = asyncio.Lock()
        self.work_root = store.directory / "work"
        self.workspaces = Workspaces(store, self.work_root)
        workspaces_module.GIT_GUARD = weakref.WeakMethod(self.git_guard)
        workspaces_module.CLONE_REMADE = weakref.WeakMethod(self.clone_remade)
        self.workspaces.github_token = lambda repo: self.github.token_for(repo)
        self.tasks = TaskBook(store, on_change=self.task_changed)
        migrate_tasks(store)
        settle_adopted(store)
        settle_followups(store)
        settle_decision_changes(store)
        self.delivery = Delivery(self)
        # The parts, by responsibility; each keeps a reference to this runtime.
        self.events = Events(self)
        self.workers = Workers(self)
        self.task_actions = TaskActions(self)
        self.drafts = Drafts(self)
        self.asks = Asks(self)
        self.asks.migrate_questions()
        self.decisions = Decisions(self)
        self.peers = Peers(self)
        self.learning = Learning(self)
        self.habits = Habits(self)
        self.checks = Checks(self)
        self.focus = Focus(self)
        self.watchdog = Watchdog(self)
        self.abilities = Abilities(self)
        self.connector = Connector(self)
        self.storage = Storage(self)
        # Which Copilot sessions a worker or a script made, not the owner (provenance.py).
        self.provenance = Provenance(
            store,
            lambda session_id: log_of(self.backend, session_id),
            self.workspaces.own_folder,
            uptime=self.uptime,
        )
        # Where the owner's other Copilot sessions stand (the sidebar), while the setting is on.
        self.others = Others(self)
        # Send to Chief's socket, while the setting is on, and its command in the owner's shell.
        self.send = SendSocket(self)
        self.send_command = shell_command()
        # Chief of Staff's own code, read-only for sandboxed workers (see own_code).
        self.own_code_paths: list[str] | None = None
        # Sandboxed workers' caches (caches.py): their tier, their init scripts, what was found
        # about the owner's tools (GOPROXY, a Java truststore), and the background seeding.
        self.cache_tier = caches.default_tier()
        self.cache_scripts = store.directory / caches.SCRIPTS
        self.cache_facts: dict[str, Any] = {}
        self.cache_facts_job: asyncio.Task | None = None
        self.truststore_job: asyncio.Task | None = None
        # Whether a worker's folders hold a Rust project (rustup's toolchains), per worker,
        # looked at once as it opens (ready_caches).
        self.cache_rust: dict[str, bool] = {}
        self.cache_seeding = False
        self.cache_seeded = 0.0
        self.toolset = Tools(self)
        self.tools = self.toolset.definitions()

    # Lifecycle

    async def start(self):
        self.stopping = False
        self.loop = asyncio.get_running_loop()
        self.bus.bind(self.loop)
        self.started_at = time.time()
        self.alive()  # Before its commands of before are ended (end_commands, at connect).
        self.alive_job = asyncio.create_task(self.keep_alive())
        if not getattr(self, "demo", False) and os.environ.get("CHIEFOFSTAFF_UPDATE_CHECK") != "0":
            self.update_job = asyncio.create_task(self.watch_updates())
        self.store.execute(
            "UPDATE drafts SET status='uncertain', outcome=? WHERE status='sending'",
            ("Chief of Staff stopped while sending. Check whether it was delivered.",),
        )
        self.asks.expire_requests()
        self.task_actions.settle_old_reports(TASKS_SHOWN_HOURS)
        await self.connect()
        self.after_update()
        await self.recover_agents()
        for source in self.all_sources():
            await source.start()
        self.watch_job = asyncio.create_task(self.watch_loop())
        self.mirror_job = asyncio.create_task(self.workers.mirror_loop())
        self.others.start()
        if self.store.settings().send_to_chief:
            self.send.open()
        await self.habits.start()
        await self.connector.start()
        self.background(self.abilities.sync())
        if self.store.settings().worker_sandbox and self.sandbox_host:
            self.seed_caches_soon()
        if self.store.drafts("uncertain"):
            self.background(self.drafts.check_uncertain())

    def alive(self):
        """This run is running now: kept with Chief of Staff's runs (kv "uptime", at most
        RUNS_KEPT of them), so that a command it saw start and not end never counts as running
        past it (provenance.window_end)."""
        if self.started_at is None:
            return
        runs = [run for run in runs_of(self.store) if run[0] != self.started_at]
        runs.append((self.started_at, time.time()))
        self.store.set("uptime", json.dumps(runs[-RUNS_KEPT:]))

    async def keep_alive(self):
        while True:
            await asyncio.sleep(ALIVE_EVERY)
            self.alive()

    async def watch_updates(self):
        """Whether a newer Chief of Staff is out: a minute after the start (not on its way),
        then each hour, PyPI read when the last look is a day old (updates.check)."""
        await asyncio.sleep(UPDATE_DELAY)
        while True:
            before = updates.notice(self.store)
            with suppress(Exception):
                await updates.check(self.store)
            if updates.notice(self.store) != before:
                self.publish_state()
            await asyncio.sleep(3600)

    def work_running(self) -> bool:
        """Whether any session has a turn running or starting (an update waits for none)."""
        return any(
            row["status"] in {"working", "starting"} or self.states[row["id"]].busy
            for row in self.store.open_sessions()
        )

    async def update_now(self, cancel: bool = False) -> dict:
        """The owner's Update and restart: once no turn runs (never stopping one), Chief of
        Staff updates itself and starts again in its own place, as after any restart; or, with
        `cancel`, it doesn't. ValueError when it can't update itself (the command says how)."""
        if cancel:
            if self.updater and self.updating and self.updating.get("state") != "restarting":
                self.updater.cancel()
                self.updater, self.updating = None, None
            self.publish_state()
            return self.snapshot().get("update") or {}
        found = updates.notice(self.store)
        launch = updates.launch_now()
        if not found:
            raise ValueError("This is the newest Chief of Staff.")
        if not launch.restart or not self.restarter:
            raise ValueError(f"Update it with: {launch.command}")
        if not (self.updater and not self.updater.done()):
            self.updating = {"to": found["version"], "state": "waiting"}
            launch = launch.to(found["version"])
            self.updater = self.background(self.update_when_idle(launch, found["version"]))
        self.publish_state()
        return self.snapshot().get("update") or {}

    async def update_when_idle(self, launch: updates.Launch, version: str):
        fetched = False
        while True:
            if self.work_running():
                self.set_updating("waiting")
                await asyncio.sleep(UPDATE_POLL)
                continue
            if fetched:
                break
            self.set_updating("fetching")
            fetched, why = await updates.fetch(launch)
            if not fetched:
                logger.warning("Could not update Chief of Staff: %s", why)
                self.set_updating("failed", why or "it could not be downloaded")
                return
        self.set_updating("restarting")
        self.store.set(updates.UPDATING, json.dumps({"from": updates.installed(), "to": version}))
        await asyncio.sleep(0.5)  # (The page hears it.)
        if self.restarter:
            self.restarter(launch)

    def set_updating(self, state: str, error: str = ""):
        if self.updating and (self.updating.get("state"), self.updating.get("error")) != (
            state,
            error or None,
        ):
            self.updating = {
                "to": self.updating["to"],
                "state": state,
                **({"error": error} if error else {}),
            }
            self.publish_state()

    def after_update(self):
        """After a restart to update: on Home, in one line, that it did, or why it didn't."""
        if not (asked := self.store.get(updates.UPDATING)):
            return
        self.store.delete(updates.UPDATING)
        note = self.store.directory / updates.UPDATE_NOTE
        why = ""
        with suppress(OSError):
            why = note.read_text().strip()[:300]
            note.unlink()
        try:
            asked_for = json.loads(asked)
            wanted, before = str(asked_for["to"]), str(asked_for["from"])
        except (ValueError, TypeError, KeyError):
            return
        running = updates.installed()
        if updates.newer(running, before) and not updates.newer(wanted, running):
            text = f"Chief of Staff updated to {running}."
        else:
            text = f"Chief of Staff could not update to {wanted}"
            text += f" ({why})" if why else ""
            text += f"; it runs {running} as before."
        if router := self.store.get("router_id"):
            self.add_message(router, "notice", text)

    def uptime(self) -> list[tuple[float, float]]:
        """Chief of Staff's runs, this one's running until now."""
        runs = [run for run in runs_of(self.store) if run[0] != self.started_at]
        if self.started_at is not None:
            runs.append((self.started_at, time.time()))
        return runs

    def all_sources(self) -> list[Source]:
        return [self.slack, self.github, *self.sources]

    async def stop(self):
        self.stopping = True
        self.send.close()
        if workspaces_module.GIT_GUARD and workspaces_module.GIT_GUARD() == self.git_guard:
            workspaces_module.GIT_GUARD = None
        remade = workspaces_module.CLONE_REMADE
        if remade and remade() == self.clone_remade:
            workspaces_module.CLONE_REMADE = None
        for job in (
            self.recovery,
            self.process_job,
            self.watch_job,
            self.mirror_job,
            self.alive_job,
            self.update_job,
        ):
            if job:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
        if self.started_at is not None:
            self.alive()  # Stopped: its run ends here.
            self.started_at = None
        await self.others.stop()
        for source in self.all_sources():
            await source.stop()
        await self.habits.stop()
        await self.connector.stop()
        for agent in self.agents:
            await self.disconnect_agent(agent)
        await self.disconnect()
        for task in list(self.jobs):
            task.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        for pool in (self.settings_pool, self.guard_pool):
            if pool:
                pool.shutdown(wait=False, cancel_futures=True)
        self.settings_pool = self.guard_pool = None

    async def until_connected(self):
        """A session used while Copilot connects (a start, or again: its backend is not there
        yet) waits for the connection, then is refused as require_ready says."""
        if (pending := self.connecting) and not pending.done() and not self.stopping:
            await asyncio.shield(pending)
        if not self.backend:
            self.require_ready()

    @connection
    async def connect(self):
        async with self.connect_lock:
            # What other agents' workers left in their private files when an earlier Chief of
            # Staff was killed (this one's own workers' files are theirs until each ends).
            for name in agents.NAMES:
                await asyncio.to_thread(
                    agents.clear_private, self.agent_folder(name), keep_own=True
                )
            # Copilot starts afresh (at start, or reconnecting): the commands its shells were
            # running ended with the one before.
            self.end_commands(COPILOT_AGENT)
            starting = False
            self.status = {"state": "starting", "error": ""}
            self.publish_state()
            try:
                # Copilot CLIs started before that outlived their connection (orphaned when
                # their loader was killed, or this app stopped abruptly) hold sessions open. Only
                # those still this data directory's to stop: never an original's processes that
                # a copy of its data directory names (backend.copilot_orphans).
                if self.own_code_paths is None:
                    self.own_code_paths = await asyncio.to_thread(own_code)
                self.find_cache_facts()
                records = self.recorded_processes()
                directory = self.store.directory
                if started := await asyncio.to_thread(copilot_orphans, records, directory):
                    await asyncio.to_thread(lambda: end_processes(copilot_tree(started)))
                self.backend = self.backend_factory(self.store.settings())
                if isinstance(self.backend, CopilotBackend):
                    self.backend.note = self.backend_note
                    self.backend.refusals = directory / REFUSED_CLI
                starting = True
                await self.backend.start()
                starting = False
                auth = await self.backend.get_auth_status()
                # It answered: its processes run now. Remembered, so they never count as the
                # owner's terminal (even orphaned) and are stopped when they outlive it: with
                # what tells them from another instance's later (backend.copilot_records).
                pids = await asyncio.to_thread(self.backend.processes)
                records = await asyncio.to_thread(copilot_records, pids, directory)
                self.store.set("copilot_pids", json.dumps(records))
                if not auth.isAuthenticated:
                    raise RuntimeError("Copilot is not signed in. Run `copilot login`.")
                warning = await self.version_warning()
                with suppress(Exception):
                    version = await self.backend.version()
                    workspaces_module.ABSENT_HELD = not older(version, ABSENT_HELD_CLI)
                self.sandbox_gh = await self.backend.gh_works_in_sandbox()
                await self.ensure_router()
                self.toolset.ensure_profile_card()
                # A worker whose start a stop interrupted starts again.
                self.store.execute(
                    "UPDATE sessions SET status='queued' WHERE status='starting' "
                    "AND closed_at IS NULL"
                )
                await self.reconcile_sends()
                if self.connections:
                    # Copilot stopped (crashed, killed) and started again: what the turns it cut
                    # short used is in their logs, not in events that came by.
                    await self.workers.sync_all_usage()
                await self.resume_working()
                try:
                    await self.ensure_slack_session()
                except Exception as exc:
                    # Slack must not keep the Router and the workers from running.
                    logger.warning("The Slack session could not start.", exc_info=True)
                    warning = (warning + " " if warning else "") + (
                        f"The Slack session could not start: {describe(exc)[:200]}"
                    )
                self.status = {"state": "ready", "error": "", "warning": warning}
            except OpenElsewhereError as exc:
                # The Router waits until the owner exits it there: connecting again then.
                self.status = {"state": "error", "error": str(exc)}
                self.recover_later()
            except Exception as exc:
                logger.exception("Copilot runtime failed to start")
                error = describe(exc)[:500]
                if starting:
                    # Copilot would not start at all: a newer Chief of Staff may run it.
                    error += " " + updates.start_hint(self.store)
                self.status = {"state": "error", "error": error}
                if self.backend and not self.backend.alive():
                    self.recover_later()  # Copilot stopped while connecting: again, later.
            self.publish_state()
        if self.ready():
            # The one watching an earlier connection ends by itself.
            self.process_job = asyncio.create_task(
                self.watch_process(self.connected(), self.connections)
            )
            self.background(self.workers.pump())
            self.background(self.workers.refresh_elsewhere())
            self.background(self.github.signed_in())  # Which GitHub accounts workers can use.

    def backend_note(self, text: str) -> None:
        """What Copilot's start is doing when it takes a while: said as it connects."""
        self.status = {"state": "starting", "error": "", "warning": text}
        self.publish_state()

    async def version_warning(self) -> str:
        """A note for the owner when the agent CLI is older than the versions we tested, or
        newer than this release supports (another Copilot runs instead: backend.fallback)."""
        backend = self.connected()
        if fallback := getattr(backend, "fallback", None):
            theirs, ours = fallback
            return (
                f"Your Copilot CLI{f' ({theirs})' if theirs else ''} is newer than Chief of Staff "
                f"supports; using Copilot {ours} for now."
            )
        try:
            version = await backend.version()
        except Exception:
            return ""
        if older(version, OLDEST_CLI):
            name = backend.capabilities.name
            return f"{name} CLI {version} is older than {OLDEST_CLI}. Update it: copilot update"
        return ""

    async def disconnect(self):
        # Not ready from the first moment: nothing starts (a pump, a delivery) while it stops.
        backend, self.backend = self.backend, None
        self.connections += 1
        mine = self.on_agent(COPILOT_AGENT, self.states.ids())
        sessions = self.states.take_handles(mine)
        # Their turns end with the connection: no idle event will come to free their slots.
        self.states.end_turns(mine)
        await asyncio.gather(*(s.disconnect() for s in sessions), return_exceptions=True)
        if backend:
            with suppress(Exception):
                await backend.stop()

    async def reconnect(self):
        if not (self.connecting and not self.connecting.done()):
            # From now: what is sent to a session meanwhile waits for it (until_connected).
            self.connecting = asyncio.get_running_loop().create_future()
        await self.disconnect()
        await self.connect()

    # Backends: Copilot's, and other coding agents' beside it (none ships yet). Each has its own
    # connections, and its sessions' handles, turns, commands and recovery are its own.

    @property
    def backends(self) -> dict[str, AgentBackend]:
        """The backends running now, by agent: Copilot's first (connected first, as `backend`),
        then other agents', each started on first use."""
        running = {COPILOT_AGENT: self.backend} if self.backend else {}
        return running | {
            name: entry.backend for name, entry in self.agents.items() if entry.backend
        }

    def agent_folder(self, name: str) -> Path:
        """Chief of Staff's own folder for an agent (its processes start there)."""
        return self.store.directory / "agents" / name

    def agent_connection(self, name: str) -> AgentConnection:
        return AgentConnection(
            lambda: agents.backend(name, self.agent_folder(name)),
            enabled=lambda: agents.enabled(self.store, name),
        )

    def agent(self, session_id: str) -> str:
        """The agent a session runs on, for its life: Copilot, unless its row names another (an
        `agent:<id>` entry, made with the row)."""
        return self.store.get(f"agent:{session_id}") or COPILOT_AGENT

    def on_copilot(self, session_id: str) -> bool:
        return self.agent(session_id) == COPILOT_AGENT

    def made(self, session_id: str) -> bool:
        """Whether its session was made on its agent (else it is made at its start). Copilot's:
        `created:`. Another agent's rows have `created:` from the start, so an earlier version,
        which reads every worker as Copilot's, only ever tries to resume them, never makes one
        in their place (more-agents.md 8); theirs is made once the id it chose is known."""
        if self.agents and not self.on_copilot(session_id):
            return bool(self.store.get(f"copilot:{session_id}"))
        return bool(self.store.get(f"created:{session_id}"))

    def on_agent(self, agent: str, session_ids: Iterable[str]) -> list[str]:
        """Of these sessions, those that run on this agent."""
        named = self.store.items("agent:")
        return [key for key in session_ids if named.get(key, COPILOT_AGENT) == agent]

    def connection(self, agent: str) -> tuple[AgentBackend | None, int]:
        """An agent's backend while it runs (None otherwise), and how many times it was
        disconnected: a session opened on it before one is not usable."""
        if agent == COPILOT_AGENT:
            return self.backend, self.connections
        entry = self.agents.get(agent)
        return (entry.backend, entry.connections) if entry else (None, 0)

    def backend_for(self, row: dict) -> AgentBackend:
        """The backend a session runs on: Copilot's (also while connecting, as connected()), or
        its agent's while that runs."""
        agent = self.agent(row["id"])
        if agent == COPILOT_AGENT:
            return self.connected()
        backend, _ = self.connection(agent)
        if backend is None:
            raise RuntimeError(f"This session's agent ({agent}) is not running.")
        return backend

    async def start_agent_of(self, session_id: str):
        """Another agent's backend for this session, started on first use (Copilot's runs from
        connect); refused with why when the session may not run on it now (agent_block). Not
        while holding the session's locks: recovering that agent's sessions may take them."""
        if (agent := self.agent(session_id)) != COPILOT_AGENT:
            await self.agent_ready(agent, self.store.find_session(session_id))

    async def agent_ready(self, agent: str, row: dict | None):
        """Its agent started for this worker, which may run on it: asked before and again once
        it runs (what its version's check found is known only then). Refused for spend not
        known, the card its refusal points at is there first."""
        if row:
            await self.ensure_spend_card(row)
        if row and (why := self.agent_block(row)):
            raise RuntimeError(why)
        await self.connect_agent(agent)
        await self.recheck_agent(agent)
        if row and (why := self.agent_block(row)):
            raise RuntimeError(why)

    async def connect_agent(self, agent: str) -> AgentBackend:
        """Another agent's backend, started on first use, as connect starts Copilot's: the
        commands its sessions ran ended with its process before, and their interrupted sends and
        turns are recovered."""
        entry = self.agents.get(agent)
        if entry is None:
            raise RuntimeError(f"This session runs on {agent}, which is not available.")
        if entry.backend:
            return entry.backend
        async with entry.lock:
            if entry.backend:
                return entry.backend
            if self.stopping:
                raise RuntimeError("Chief of Staff is stopping.")
            if sys.platform == "win32":
                raise RuntimeError(self.not_on_windows(agent))
            if not entry.enabled():
                raise RuntimeError(self.turned_off(agent))
            self.end_commands(agent)
            # What its processes left when Chief of Staff last stopped abruptly (the supervisor
            # ends them unless it was killed too): only this data directory's, as for Copilot.
            key, directory = f"agentpids:{agent}", self.store.directory
            records = self.recorded(key)
            if left := await asyncio.to_thread(copilot_orphans, records, directory):
                await asyncio.to_thread(lambda: end_processes(copilot_tree(left)))
            backend = entry.start()
            try:
                await backend.start()
                await self.check_agent(agent, backend)
            except BaseException:
                with suppress(Exception):
                    await backend.stop()
                raise
            pids = await asyncio.to_thread(backend.processes)
            records = await asyncio.to_thread(copilot_records, pids, directory, hashed=True)
            self.store.set(key, json.dumps(records))
            self.agent_pids[agent] = pids
            # Its turns begin only once admitted (the next message in its own queue too).
            if hasattr(backend, "admit"):
                backend.admit = self.admit_turn  # type: ignore[attr-defined]
            if hasattr(backend, "begins"):
                backend.begins = self.begins_turn  # type: ignore[attr-defined]
            if hasattr(backend, "ended"):
                # A worker's own process ended: only that worker is affected.
                backend.ended = lambda worker, process, why: self.agent_process_ended(  # type: ignore[attr-defined]
                    agent, worker, process, why
                )
            if hasattr(backend, "on_start"):
                backend.on_start = lambda: self.agent_process_started(agent, backend)  # type: ignore[attr-defined]
            if hasattr(backend, "on_limit"):
                # A plan's limit as a worker's stream said it (nothing about the account).
                backend.on_limit = lambda limit: self.store.set(  # type: ignore[attr-defined]
                    f"agentlimit:{agent}", json.dumps(limit)
                )
            if hasattr(backend, "on_refused"):
                backend.on_refused = lambda why: self.agent_gate_failed(agent, backend, why)  # type: ignore[attr-defined]
            entry.backend = backend
            entry.offline = False
            entry.watch = asyncio.ensure_future(self.watch_agent(agent, backend, entry.connections))
            try:
                await self.end_epochs(agent, backend)
                await self.reconcile_sends(agent)
                await self.resume_working(agent)
            except Exception:
                logger.exception("Recovering the sessions of %s failed", agent)
        return backend

    async def check_agent(self, agent: str, backend: Any):
        """What Chief of Staff's checks rely on, checked once per version of another agent (its
        backend's `check`, on this machine without a model call) and remembered; what it found
        keeps its workers from running (agent_block), also before its next start (kv
        `agentrefused:<agent>`). A check that could not run is tried again (recheck_agent)."""
        if not hasattr(backend, "check"):
            return
        version = await backend.version()
        # A version, or (Claude's) what tells one build of it from another (check_key).
        checked = await backend.check_key() if hasattr(backend, "check_key") else version
        key = f"agentcheck:{agent}:{checked}"
        try:
            found = json.loads(self.store.get(key) or "null")
        except ValueError:
            found = None
        if not isinstance(found, dict):
            self.agent_checked_at[agent] = time.monotonic()
            found = await backend.check(self.work_root / f"{agent}-check")
            if found.get("ran"):
                self.store.set(key, json.dumps(found))
        backend.checked = found
        refused = {
            "version": version,
            "acting": getattr(backend, "acting_refused", ""),
            "ask_first": getattr(backend, "ask_first_refused", ""),
            "sandbox": getattr(backend, "sandbox_refused", SANDBOX_UNPROVEN),
        }
        if self.store.get(f"agentrefused:{agent}") != json.dumps(refused):
            self.store.set(f"agentrefused:{agent}", json.dumps(refused))

    def agent_gate_failed(self, agent: str, backend: Any, why: str):
        """Another agent began a turn its gate did not admit (more-agents.md 5.12.5): no worker
        runs on that version again, also after a restart (its check's record says why)."""

        async def record():
            checked = await backend.check_key() if hasattr(backend, "check_key") else ""
            key = f"agentcheck:{agent}:{checked or await backend.version()}"
            try:
                found = json.loads(self.store.get(key) or "{}")
            except ValueError:
                found = {}
            found = (found if isinstance(found, dict) else {}) | {"ran": True, "acting": why}
            self.store.set(key, json.dumps(found))
            backend.checked = found
            refused = {
                "version": await backend.version(), "acting": why, "ask_first": why,
                "sandbox": why,
            }  # fmt: skip
            self.store.set(f"agentrefused:{agent}", json.dumps(refused))

        self.background(record())

    async def recheck_agent(self, agent: str):
        """A check that could not run (its version's workers wait meanwhile) is tried again
        with a worker's start, a minute after the last try at most once."""
        entry = self.agents.get(agent)
        backend = entry.backend if entry else None
        checked = getattr(backend, "checked", None)
        if entry is None or backend is None or not isinstance(checked, dict) or checked.get("ran"):
            return
        if time.monotonic() - self.agent_checked_at.get(agent, 0.0) < AGENT_CHECK_AGAIN:
            return
        async with entry.lock:
            if entry.backend is backend and not (backend.checked or {}).get("ran"):  # type: ignore[attr-defined]
                with suppress(Exception):
                    await self.check_agent(agent, backend)

    async def disconnect_agent(self, agent: str):
        """As disconnect, for another agent's backend: only its own sessions' handles and turns
        end."""
        entry = self.agents[agent]
        backend, entry.backend = entry.backend, None
        entry.connections += 1
        if entry.watch and entry.watch is not asyncio.current_task():
            entry.watch.cancel()
        entry.watch = None
        mine = self.on_agent(agent, self.states.ids())
        sessions = self.states.take_handles(mine)
        self.states.end_turns(mine)
        self.turn_gone(*mine)
        await asyncio.gather(*(s.disconnect() for s in sessions), return_exceptions=True)
        if backend:
            with suppress(Exception):
                await backend.stop()
        if not self.stopping:
            self.background(self.workers.pump())  # Their worker slots are free.

    async def watch_agent(self, agent: str, backend: AgentBackend, connection: int):
        """Another agent's backend stopped by itself (crashed, killed): its own sessions' turns
        end and it starts again, recovering them; Copilot's sessions are not touched."""
        entry = self.agents[agent]
        while entry.backend is backend and entry.connections == connection:
            if backend.alive():
                await self.record_agent_processes(agent, backend)
            if not backend.alive():
                logger.warning("%s stopped by itself; starting it again.", agent)
                # Its sessions at work as it stopped: what ending their turns sets off may change
                # their status before it is known whether it starts again.
                working = self.at_work_on(agent)
                await self.disconnect_agent(agent)
                try:
                    await self.connect_agent(agent)
                except Exception as exc:
                    logger.warning("Starting %s again failed.", agent, exc_info=True)
                    entry.offline = True
                    why = f"Its agent stopped and could not start again: {describe(exc)}"
                    self.fail_working(agent, why, working)
                return
            await asyncio.sleep(PROCESS_CHECK)

    async def record_agent_processes(self, agent: str, backend: Any):
        """What its workers' processes run now (each its own: its supervisor and their tree),
        recorded as they change, for a later start or a downgrade to end if Chief of Staff and
        those supervisors were all killed."""
        pids = await asyncio.to_thread(backend.processes)
        entry = self.agents.get(agent)
        if not entry or entry.backend is not backend or self.agent_pids.get(agent) == pids:
            return
        self.agent_pids[agent] = pids
        records = await asyncio.to_thread(copilot_records, pids, self.store.directory, hashed=True)
        if entry.backend is backend:
            self.store.set(f"agentpids:{agent}", json.dumps(records))

    def agent_process_started(self, agent: str, backend: Any):
        """A worker's own process of another agent started: it runs again (Settings stops
        saying it stopped), and what it runs is recorded."""
        if entry := self.agents.get(agent):
            entry.offline = False
        self.background(self.record_agent_processes(agent, backend))

    def agent_process_ended(self, agent: str, worker: str, process: str, why: str):
        """A worker's own process of another agent ended: its usage epoch ends (unknown if a
        turn was spending); one that stopped by itself, or that Chief of Staff stopped at once
        (`why`), affects that worker alone (worker_lost)."""
        with suppress(Exception):
            self.end_epoch(worker, process)
        self.turn_gone(worker)
        if why and not self.stopping:
            self.background(self.worker_lost(agent, worker, why))

    async def worker_lost(self, agent: str, worker: str, why: str):
        """Its process stopped by itself ("crash") or was stopped at once (why): its turns and
        commands end, and no other worker's. One at work goes on in a new process, as after a
        restart of its agent, unless its process stopped by itself shortly after the last time
        (it fails then: never a loop of starts) or Chief of Staff stopped it (it fails, with
        why); one with a limit and what it spent unknown waits for Go on."""
        working = [key for key in self.at_work_on(agent) if key == worker]
        sessions = self.states.take_handles([worker])
        self.states.end_turns([worker])
        self.turn_gone(worker)
        await asyncio.gather(*(s.disconnect() for s in sessions), return_exceptions=True)
        self.store.execute(
            "UPDATE commands SET ended_at=? WHERE ended_at IS NULL AND session_id=?",
            (now(), worker),
        )
        row = self.store.find_session(worker)
        self.background(self.workers.pump())  # Its slot is free.
        if not row or row["closed_at"]:
            return
        if why != "crash":
            if working or row["status"] in {"working", "queued", "starting"}:
                self.fail(row, why)
            else:
                self.add_message(worker, "notice", why)
            return
        name = agents.display(agent)
        logger.warning("%s's %s process stopped by itself.", worker, name)
        again = time.monotonic() - self.lost_at.get(worker, -AGENT_LOST_AGAIN) < AGENT_LOST_AGAIN
        self.lost_at[worker] = time.monotonic()
        if agents.limit(self.store, agent) and self.store.agent_unknown(worker):
            await self.stop_for_spend(row)
            return
        if not working:
            return
        if again:
            self.fail(
                row,
                f"{name} stopped unexpectedly twice within {int(AGENT_LOST_AGAIN / 60)} minutes; "
                "send it a message to try again.",
            )
            return
        try:
            await self.reconcile_sends(agent, only=[worker])
            await self.resume_working(agent, only=[worker])  # Failed with why if it can't.
        except Exception as exc:
            logger.warning("Resuming %s failed.", worker, exc_info=True)
            self.fail(row, f"{name} stopped and could not start again: {describe(exc)}")
        after = self.store.find_session(worker)
        if after and after["status"] == "failed" and (entry := self.agents.get(agent)):
            entry.offline = True  # Settings says so until one of its processes starts.

    async def recover_agents(self):
        """At start: other agents whose sessions were at work, or had messages on their way, are
        started now, so their sessions go on as Copilot's do (resume_working). One that cannot
        start (or that this Chief of Staff does not run) fails its sessions at work, with why."""
        if not (named := set(self.store.items("agent:").values()) - {COPILOT_AGENT}):
            return
        rows = {row["id"]: row for row in self.store.open_sessions()}
        for agent in sorted(named):
            if any(
                rows[key]["status"] == "working"
                or self.store.get(f"sending:{key}")
                or self.delivery.has_unsent(key)
                for key in self.on_agent(agent, rows)
            ):
                try:
                    await self.connect_agent(agent)
                except Exception as exc:
                    logger.warning("%s could not start.", agent, exc_info=True)
                    self.fail_working(agent, f"Could not resume after restart: {describe(exc)}")

    def at_work_on(self, agent: str) -> list[str]:
        """This agent's open sessions at work now: in a turn (also one its status does not say
        yet), or sent a message they have not started on."""
        rows = self.store.open_sessions()
        found = [row["id"] for row in rows if row["status"] == "working"]
        found += [
            key for key in self.states.ids() if self.states[key].busy or self.states[key].awaiting
        ]
        open_now = {row["id"] for row in rows}
        return self.on_agent(agent, [key for key in dict.fromkeys(found) if key in open_now])

    def fail_working(self, agent: str, why: str, working: list[str] | None = None):
        """The agent cannot start: its sessions at work (as of `working`, else now) fail, with
        why (nothing else would end their turns, and only its own sessions are touched)."""
        for key in self.at_work_on(agent) if working is None else working:
            if (row := self.store.find_session(key)) and not row["closed_at"]:
                self.fail(row, why)

    # Admission: whether another agent's worker may run now. Copilot's sessions always may.

    def turned_off(self, agent: str) -> str:
        return f"{agents.display(agent)} is turned off in Settings → Models."

    @staticmethod
    def not_on_windows(agent: str) -> str:
        return f"{agents.display(agent)} workers need macOS or Linux for now."

    def agent_block(self, row: dict) -> str:
        """Why this session may not run now ("" when it may): never for Copilot's. Another
        agent's runs only the owner's own workers, not on Windows yet, only while that agent is
        on and the worker sandbox off (its sandbox has not passed Chief of Staff's checks), only
        on a version whose check found nothing unsafe, with Ask first only on a version whose
        every kind of action was shown to ask first (its backend says so), and within its token
        limit (none spent unknown that the owner has not accepted)."""
        agent = self.agent(row["id"])
        if agent == COPILOT_AGENT:
            return ""
        return self.blocked_on(agent, row)

    def blocked_on(self, agent: str, row: dict) -> str:
        """Why this worker (or one about to be made) may not run on that agent now."""
        name = agents.display(agent)
        if sys.platform == "win32":
            return self.not_on_windows(agent)
        entry = self.agents.get(agent)
        if entry is None and agent not in agents.NAMES:
            return f"This session runs on {agent}, which is not available."
        if entry is None or not entry.enabled():
            return self.turned_off(agent)
        if row["role"] != WORKER or self.special(row):
            return f"Only your own workers run on {name}."
        refused = self.agent_refused(agent)
        sandbox = refused.get("sandbox", SANDBOX_UNPROVEN)
        if row["slack"]:
            # The same guarantees as a Copilot worker that reads Slack: its commands sandboxed
            # (GitHub only), and asking first, on a version shown to do both.
            why = (
                "this computer can't sandbox workers" if not self.sandbox_host
                else sandbox or refused.get("ask_first") or (
                    "it acts on its own" if self.yolo(row) else ""
                )
            )  # fmt: skip
            if why:
                return f"Workers that read Slack run on Copilot, not {name}: {why}."
        if self.store.settings().worker_sandbox and self.sandbox_host and sandbox:
            return (
                f"{name} can't run while workers are sandboxed: {sandbox}. Turn the sandbox off "
                "in Settings → Workers to go on."
            )
        if why := refused.get("acting"):
            return why
        if not self.yolo(row) and (why := refused.get("ask_first")):
            return why
        if limit := agents.limit(self.store, agent):
            if self.store.agent_unknown(row["id"]):
                return (
                    f"{name} stopped in the middle of a turn, so what this worker spent then is "
                    f"not known and its token limit can't be kept: {self.spend_place(row)}"
                )
            if (spent := self.store.agent_spent(row["id"])) >= limit:
                return f"This worker used its {name} token limit ({spent:,} of {limit:,})."
        return ""

    def agent_refused(self, agent: str) -> dict:
        """What the check of an agent's version found it may not do (acting on its own, Ask
        first): from its backend while it runs, else as it was last found (a worker may be
        refused before any process of it starts; a new version is checked as it starts)."""
        entry = self.agents.get(agent)
        if entry and entry.backend and getattr(entry.backend, "checked", None) is not None:
            return {
                "acting": getattr(entry.backend, "acting_refused", ""),
                "ask_first": getattr(entry.backend, "ask_first_refused", ""),
                "sandbox": getattr(entry.backend, "sandbox_refused", SANDBOX_UNPROVEN),
            }
        try:
            found = json.loads(self.store.get(f"agentrefused:{agent}") or "{}")
        except ValueError:
            return {}
        return found if isinstance(found, dict) else {}

    async def new_worker_agent(self, named: str, slack: bool) -> tuple[str, str]:
        """The agent a new worker of the owner's runs on, and what its timeline says of it: the
        one the owner named (refused with why when it cannot run it now: never moved to
        another), else the one Settings chose for new workers, or Copilot with why when that one
        cannot run it. One that reads Slack runs on Copilot."""
        chosen = named or (agents.new_workers(self.store) if self.agents else COPILOT_AGENT)
        if chosen == COPILOT_AGENT:
            return chosen, ""
        if chosen not in agents.NAMES:
            raise ValueError(f"Chief of Staff does not run {chosen!r}.")
        row = {"id": "", "role": WORKER, "slack": slack, "yolo": "", "closed_at": None}
        if slack and not named and self.blocked_on(chosen, row):
            return COPILOT_AGENT, ""  # Not this agent's to run (it reads Slack): nothing to say.
        if not (why := self.blocked_on(chosen, row)):
            try:
                await self.connect_agent(chosen)
                await self.recheck_agent(chosen)
            except Exception as exc:
                why = f"{agents.display(chosen)} could not start: {describe(exc)}"
            else:
                why = self.blocked_on(chosen, row)  # What its version showed, now known.
        if not why:
            return chosen, ""
        if named:
            raise ValueError(why)
        return COPILOT_AGENT, f"Started on Copilot: {why}"

    async def check_agent_model(self, agent: str, model: str):
        """A model the Router picked for a worker on another agent: one that agent offers."""
        if not model:
            return
        offered = [item["id"] for item in await self.agent_models(agent, start=True)]
        if model not in offered:
            raise ValueError(
                f"Unknown model {model!r} for {agents.display(agent)}. Available: "
                f"{', '.join(offered) or 'none listed'}"
            )

    async def admit_turn(self, session: Any, woken: bool = False) -> str:
        """The one door for a turn of another agent's worker (what CoS sends, the next message
        in the agent's own queue, and, `woken`, one it starts by itself as a command it left
        running ended): "" when it may begin now, else why not. Under the admission lock, so a
        change that makes it ineligible (stop_ineligible) is either seen here or comes after,
        and then stops it."""
        row = self.store.find_session(self.row_id(session.session_id))
        if not row or row["closed_at"]:
            return "That session is closed."
        if woken and row["role"] == WORKER and self.events.stopped_work(row):
            # What it was on is stopped or cancelled: it does not start again by itself.
            return "What it was on is stopped or cancelled."
        await self.usage_settled(row, session)
        await self.ensure_spend_card(row)  # Refused for spend not known: its Go on is there.
        async with self.admission:
            if why := self.agent_block(row):
                return why
            self.admitted[row["id"]] = self.admission_generation
            # Its process (the epoch its usage counts in), stamped now: if it ends before its
            # first usage update, what the turn spent is known to be unknown.
            process = getattr(getattr(session, "backend", None), "epoch", "") or ""
            self.spending(row["id"], True, process)
            return ""

    async def usage_settled(self, row: dict, session: Any):
        """A turn begins once what the last one spent is recorded: what its agent said is
        handled first (Codex says a turn's usage before its end); an agent whose usage comes
        after the turn (`usage_pending`) is waited for, a minute at most, then recorded as
        unknown."""
        await asyncio.sleep(0)  # What the agent said so far is handled before this goes on.
        pending = getattr(session, "usage_pending", None)
        deadline = time.monotonic() + USAGE_SETTLES
        while pending and pending():
            if time.monotonic() >= deadline:
                epoch = self.epoch_of(row["id"])
                self.store.record_agent_usage(
                    [self.unknown(row["id"])], f"agentepoch:{row['id']}", epoch
                )
                return
            await asyncio.sleep(0.2)

    def begins_turn(self, session: Any) -> bool:
        """Just before the turn admitted begins (nothing awaited in between): whether its
        admission still holds."""
        key = self.row_id(session.session_id)
        if self.admitted.pop(key, None) != self.admission_generation:
            return False
        self.begun.add(key)
        return True

    async def stop_ineligible(self) -> list[str]:
        """Something changed (a Settings save): the generation moves first, so no turn admitted
        before can begin, then every other agent's worker that may not run now is stopped with
        what it left running. Its tasks wait under Needs you, as after any stop. Returns why
        each one stopped, by worker."""
        async with self.admission:
            self.admission_generation += 1
        named = self.store.items("agent:")
        stopped = {}
        for row in self.store.open_sessions():
            if named.get(row["id"], COPILOT_AGENT) == COPILOT_AGENT:
                continue
            if not (why := self.agent_block(row)):
                continue
            if (
                row["status"] in {"working", "queued", "starting"}
                or self.held(row["id"])
                # A turn that began as this came, its status not said yet (its first event
                # stops it as one at work: Events.turn_start).
                or self.states[row["id"]].busy
                or row["id"] in self.begun
            ):
                at_work = row["status"] in {"working", "queued", "starting"}
                if row["id"] in self.begun and not at_work and not self.held(row["id"]):
                    # Its turn began, its status not said yet: stopped as one at work (its task
                    # waits under Needs you).
                    self.update_session(row["id"], status="working")
                self.halted.add(row["id"])
                with suppress(Exception):
                    await self.workers.interrupt(row["id"], by="Settings")
                stopped[row["id"]] = why
        return list(stopped.values())

    async def stop_blocked(self, session_id: str, why: str):
        """A turn of a worker that may not run now began anyway (its admission held as a change
        came): stopped at its first event, as stop_ineligible stops one at work, saying why."""
        self.halted.add(session_id)
        with suppress(Exception):
            await self.workers.interrupt(session_id, by="Settings", notice=f"Stopped: {why}")

    async def agents_saved(self, before: dict[str, dict]) -> list[str]:
        """The owner saved the Coding agents group (Settings → Models): an agent turned on
        starts with its first worker; one turned off drains (more-agents.md 5.11): its workers
        stop at once with their commands, its process ends, and with none on the Router's tools
        and prompt are as without agents. Workers whose model came from the group take the new
        one with their next turn. Returns why each worker stopped."""
        after = agents.configured(self.store)
        was_on = sorted(name for name, entry in before.items() if entry.get("enabled"))
        now = agents.turned_on(self.store)
        for name in now:
            if name not in self.agents:
                self.agents[name] = self.agent_connection(name)
        stopped = await self.stop_ineligible()
        for name in sorted(set(was_on) - set(now)):
            if self.agents[name].backend:
                await self.disconnect_agent(name)
            # Its process is gone, and what it ran with it: its records go, its commands end,
            # and no private file of its workers is left.
            self.end_commands(name)
            self.store.delete(f"agentpids:{name}")
            await asyncio.to_thread(agents.clear_private, self.agent_folder(name))
            self.agents[name].offline = False
        if was_on != now:
            await self.refresh(ROUTER)  # Its spawn_worker and prompt name the agents on.
        retuned = {
            name
            for name in now
            if before.get(name, {}).get("model") != after.get(name, {}).get("model")
        }
        for row in self.store.open_sessions(WORKER):
            if self.agent(row["id"]) in retuned and not self.agent_choice(row["id"]).get("model"):
                await self.refresh_session(row)
        relimited = {
            name for name in after if before.get(name, {}).get("limit") != after[name].get("limit")
        }
        for row in self.store.open_sessions(WORKER):
            # Tasks its limit stopped say what Go on does now (it goes on once raised).
            if self.agent(row["id"]) in relimited:
                for task in self.tasks.for_session(row["id"]):
                    if self.store.get(f"stopped:{task['id']}") == agents.SPEND_STOP:
                        self.task_actions.publish(task)
                # A limit set on a worker whose spend is partly unknown: its Go on card, also
                # at rest, so what its next message is told to press is there.
                await self.ensure_spend_card(row)
        self.publish_state()
        return stopped

    # Other agents' usage (more-agents.md 5.8): a ledger of their own (agent_usage), kept per
    # accounting epoch (each process of a session: kv `agentepoch:<session>`), whose totals per
    # thread give each update's difference; a stop in the middle of a turn leaves what it spent
    # then unknown.

    def epoch_of(self, session_id: str) -> dict:
        try:
            epoch = json.loads(self.store.get(f"agentepoch:{session_id}") or "{}")
        except ValueError:
            return {}
        return epoch if isinstance(epoch, dict) else {}

    def spending(self, session_id: str, busy: bool, process: str = ""):
        """Whether a turn of it may be spending now (admitted, until its agent says nothing of
        it runs), in which process: its process ending then leaves what it spent unknown."""
        epoch, entries = self.epoch_of(session_id), []
        if process and (epoch.get("process") != process or epoch.get("ended")):
            epoch, entries = self.next_epoch(session_id, epoch, process)
        if busy and not epoch.get("busy"):
            epoch = epoch | {"since": time.time()}  # What it spends from now may be unknown.
        if bool(epoch.get("busy")) != busy or entries or process:
            self.store.record_agent_usage(
                entries, f"agentepoch:{session_id}", epoch | {"busy": busy}
            )

    def next_epoch(self, session_id: str, epoch: dict, process: str) -> tuple[dict, list]:
        """Its epoch in a new process: the totals each thread had in the one before are where
        its counts go on from; that one ended (if nothing said so, it ended as it was: with
        what it spent unknown if a turn was spending)."""
        # Not stamped with a process: nothing of it ran in one yet (its turn was admitted).
        lost = epoch.get("process") and not epoch.get("ended") and epoch.get("busy")
        ended = epoch.get("ended") or ("crash" if lost else "clean")
        entries = [self.unknown(session_id)] if lost else []
        before = (epoch.get("before") or {}) | (epoch.get("threads") or {})
        fresh = {
            "process": process,
            "threads": {},
            "before": before,
            "crashed": ended == "crash",
            "busy": False,
        }
        return fresh, entries

    def end_epoch(self, session_id: str, process: str) -> bool:
        """A process of its agent ended: its epoch there ends, cleanly when nothing of it was
        spending, else with what it spent then unknown. Whether it was spending."""
        epoch = self.epoch_of(session_id)
        if not epoch or epoch.get("ended") or epoch.get("process", "") != process:
            return False
        crashed = bool(epoch.get("busy"))
        epoch = epoch | {"ended": "crash" if crashed else "clean", "busy": False}
        self.store.record_agent_usage(
            [self.unknown(session_id)] if crashed else [], f"agentepoch:{session_id}", epoch
        )
        return crashed

    def agent_settled(self, row: dict):
        self.turn_gone(row["id"])
        self.spending(row["id"], False)

    def turn_gone(self, *session_ids: str):
        """Their turns ended without their agent saying they settled (their process ended,
        their handle let go of): none is begun or halted any more, so no later change stops
        what does not run, nor is one that runs again kept from being stopped."""
        for session_id in session_ids:
            self.begun.discard(session_id)
            self.halted.discard(session_id)

    def agent_accounting(self, session_id: str, kind: str, data: Any):
        """What another agent's worker spent (agent.usage), and that its turns spend no more
        (agent.settled): recorded whatever became of its handle, its worker closed included
        (Events drops a closed worker's events): what was spent is spent, and its epoch ends
        as it was."""
        try:
            if not (row := self.store.find_session(session_id)):
                return
            if kind == "agent.usage":
                self.record_agent_usage(row, data)
            elif kind == "agent.settled":
                self.agent_settled(row)
        except Exception:
            logger.exception("Recording %s of %s failed.", kind, session_id)

    def unknown(self, session_id: str) -> dict:
        """A row of the ledger for what a session spent that is not known: with a limit, it
        runs no turn until the owner accepts that (Go on); without one there is nothing to
        keep, so it is recorded as accepted (Usage still shows it)."""
        agent = self.agent(session_id)
        return {
            "agent": agent,
            "session_id": session_id,
            "kind": "unknown",
            "accepted": 0 if agents.limit(self.store, agent) else 1,
            "at": time.time(),
        }

    async def end_epochs(self, agent: str, backend: Any):
        """It started afresh: each of its sessions' epochs in a process that no longer runs
        ended (as end_epoch; also one stamped before its process said anything); a worker with
        a limit whose spend is then unknown is stopped until the owner accepts that. So is one
        still at work whose epoch ended as Chief of Staff stopped (its turn cut short then):
        resumed, its turn would be refused (agent_block) and the worker failed."""
        if hasattr(backend, "epochs"):
            live: set[str] = set(backend.epochs())
        else:
            live = {getattr(backend, "epoch", "")}
        for session_id in self.on_agent(agent, self.store.items("agentepoch:")):
            epoch = self.epoch_of(session_id)
            if epoch.get("process") in live:
                continue
            ended = self.end_epoch(session_id, epoch.get("process", ""))
            row = self.store.find_session(session_id)
            if not row or row["closed_at"] or not agents.limit(self.store, agent):
                continue
            if ended or self.store.agent_unknown(session_id):
                await self.stop_for_spend(row)

    async def stop_for_spend(self, row: dict):
        """A worker past what its limit lets it spend stops, as after any stop; its timeline
        says which in one line (spent, or not known), and its stopped task waits for Go on
        (what is unknown accepted, or its limit raised: spend_stop) or cancel."""
        if row["status"] in {"working", "queued", "starting"} or self.held(row["id"]):
            name = agents.display(self.agent(row["id"]))
            if self.store.agent_unknown(row["id"]):
                notice = f"Stopped: {name} stopped mid-turn, so what it spent is unknown."
            else:
                limit = agents.limit(self.store, self.agent(row["id"]))
                notice = f"Stopped: it used its {limit:,}-token limit."
            if self.states.handle(row["id"]) is not None:
                self.halted.add(row["id"])  # Until its turn settles (none runs without one).
            full = self.spend_stop(row)["spend_why"]  # As its card says it (cut there at 1440).
            with suppress(Exception):
                await self.workers.interrupt(
                    row["id"], by=agents.SPEND_STOP, notice=notice, full=full
                )

    def spend_stop(self, row: dict) -> dict:
        """A worker its token limit stopped, as its task under Needs you says it: what spent
        is not known (Go on accepts that), or its limit used (Go on only once it is raised)."""
        agent = self.agent(row["id"])
        name, limit = agents.display(agent), agents.limit(self.store, agent)
        if self.store.agent_unknown(row["id"]):
            since = self.epoch_of(row["id"]).get("since") or self.store.agent_unknown_at(row["id"])
            when = f" since {clock_time(since)}" if since else ""
            return {
                "spend_stop": "unknown",
                "spend_why": f"{name} stopped mid-turn, so what it spent{when} isn't known. "
                "Go on accepts that and continues.",
                "go_on": True,
            }
        if not limit:
            why = "Its token limit is off now: Go on continues."
        elif self.store.agent_spent(row["id"]) < limit:
            why = f"Its token limit is {limit:,} now: Go on continues."
        else:
            why = f"Used its {limit:,}-token limit. Raise it in Settings → Models, or cancel."
        return {"spend_stop": "limit", "spend_why": why, "go_on": not self.agent_block(row)}

    def record_agent_usage(self, row: dict, data: Any):
        """One usage update of another agent's session: what it spent since the one before, in
        its epoch (each thread's totals; a child agent's counts for its worker), for the run its
        turn is on; within the limit, or the worker is stopped."""
        agent = self.agent(row["id"])
        if agent == COPILOT_AGENT:
            return
        key = f"agentepoch:{row['id']}"
        epoch = self.epoch_of(row["id"])
        process = getattr(data, "epoch", "") or ""
        thread = getattr(data, "thread_id", "") or row["id"]
        total = counters(getattr(data, "total", None))
        restored = getattr(data, "restored", None)
        entries: list[dict] = []
        if epoch.get("process") != process:
            busy = bool(epoch.get("busy"))
            epoch, entries = self.next_epoch(row["id"], epoch, process)
            epoch["busy"] = busy
        last = epoch["threads"].get(thread)
        before = epoch.get("before", {}).get(thread)
        spent, least, kind, unknown = total, None, "exact", False
        if last is not None:
            if total["tokens"] >= last["tokens"]:
                spent = difference(total, last)
            else:  # Its totals went down within one process: as a crash.
                unknown, least, kind = True, 0, "range"
        elif restored is not None:
            spent = difference(total, counters(restored))  # Its offset, as Codex restored it.
        elif before is not None:
            if epoch.get("crashed") or total["tokens"] < before["tokens"]:
                least, kind = max(0, total["tokens"] - before["tokens"]), "range"
            else:
                spent = difference(total, before)
        epoch["threads"][thread] = total
        if unknown:
            entries.append(self.unknown(row["id"]))
        if spent["tokens"] or kind != "exact":
            entries.append(
                {
                    "agent": agent,
                    "session_id": row["id"],
                    "run_id": self.run_for_turn(row["id"], getattr(data, "turn_id", "") or ""),
                    "model": getattr(data, "model", "") or "",
                    "input_tokens": spent["input"],
                    "cached_input_tokens": spent["cached"],
                    "cache_write_tokens": spent["cache_write"],
                    "output_tokens": spent["output"],
                    "tokens": spent["tokens"],
                    "least": least,
                    "kind": kind,
                    "at": time.time(),
                }
            )
        self.store.record_agent_usage(entries, key, epoch)
        limit = agents.limit(self.store, agent)
        if (
            limit
            and not row["closed_at"]
            and row["id"] not in self.spend_stopping
            and (self.store.agent_unknown(row["id"]) or self.store.agent_spent(row["id"]) >= limit)
        ):
            self.spend_stopping.add(row["id"])
            self.background(self.stop_spending(row["id"]))

    async def stop_spending(self, session_id: str):
        try:
            if row := self.store.find_session(session_id):
                await self.stop_for_spend(row)
        finally:
            self.spend_stopping.discard(session_id)

    def run_for_turn(self, session_id: str, turn: str) -> str:
        """The run a turn's usage counts for: that of the task the turn is on when it first
        spends (Runtime.turn_task), also once the run ended (a report before its usage)."""
        key = (session_id, turn)
        if key not in self.turn_runs:
            task = self.turn_task(session_id)
            found = (
                self.store.one(
                    "SELECT id FROM runs WHERE task_id=? AND session_id=? "
                    "ORDER BY started_at DESC LIMIT 1",
                    (task, session_id),
                )
                if task
                else self.tasks.run(session_id)
            )
            if len(self.turn_runs) > 2000:
                self.turn_runs.clear()
            self.turn_runs[key] = found["id"] if found else ""
        return self.turn_runs[key]

    def accept_spend(self, session_id: str):
        """The owner said Go on (or Retry) to a worker whose spend is not all known: they
        accept it."""
        if not self.on_copilot(session_id):
            self.store.accept_agent_unknown(session_id)

    def spend_card(self, session_id: str) -> dict | None:
        """Its task under Needs you whose Go on accepts what it spent unknown (stopped for its
        token limit), if it has one."""
        for task in reversed(self.tasks.for_session(session_id)):
            if task["status"] == "waiting" and (
                self.store.get(f"stopped:{task['id']}") == agents.SPEND_STOP
            ):
                return task
        return None

    def spend_place(self, row: dict) -> str:
        """Where the owner accepts a worker's unknown spend, as its refusals say it: the Go on
        of its stopped task, the Retry of its failed one, else its limit."""
        if self.spend_card(row["id"]):
            return "press Go on under Needs you to accept that and let it continue."
        if row["closed_at"]:
            return "Retry its task to accept that and let it continue."  # Reopened, accepted.
        tasks = [t for t in self.tasks.for_session(row["id"]) if t["status"] != "cancelled"]
        if tasks and tasks[-1]["status"] == "failed":
            return "Retry its task to accept that and let it continue."
        name = agents.display(self.agent(row["id"]))
        return f"turn its limit off in Settings → Models ({name}) to let it continue."

    async def ensure_spend_card(self, row: dict) -> dict | None:
        """A worker with a token limit whose spend is partly unknown, not accepted: the Go on
        card the owner accepts that with, made if it has none, so what its refusals point at
        exists (at work, it stops as its limit stops it; else its latest task waits, stopped).
        A failed one is retried instead (Retry accepts)."""
        if (
            row["closed_at"]
            or self.on_copilot(row["id"])
            or not agents.limit(self.store, self.agent(row["id"]))
            or not self.store.agent_unknown(row["id"])
        ):
            return None
        if card := self.spend_card(row["id"]):
            return card
        if row["status"] in {"working", "queued", "starting"} or self.held(row["id"]):
            await self.stop_for_spend(row)
            return self.spend_card(row["id"])
        tasks = [t for t in self.tasks.for_session(row["id"]) if t["status"] != "cancelled"]
        if not tasks or tasks[-1]["status"] == "failed":
            return None
        task = tasks[-1]
        notice = (
            f"Stopped: {agents.display(self.agent(row['id']))} stopped mid-turn, so what it "
            "spent is unknown."
        )
        self.store.set(f"stopped:{task['id']}", agents.SPEND_STOP)
        self.store.delete(f"goingon:{task['id']}")
        self.tasks.move(task["id"], "waiting", progress=notice)
        full = self.spend_stop(row)["spend_why"]
        self.add_message(row["id"], "notice", notice, {"stopped": True, "full": full})
        self.task_actions.publish(self.tasks.get(task["id"]))
        return self.spend_card(row["id"])

    def end_commands(self, agent: str):
        """An agent's process starts afresh: the commands its sessions' shells were running
        ended with the one before. Only its own sessions'."""
        # Copilot's: every session not named for another agent. Another's: those named for it.
        named = "SELECT substr(key, 7) FROM kv WHERE key >= 'agent:' AND key < 'agent;' AND value"
        mine = f"NOT IN ({named} != ?)" if agent == COPILOT_AGENT else f"IN ({named} = ?)"
        self.store.execute(
            "UPDATE commands SET ended_at=? WHERE ended_at IS NULL "  # noqa: S608
            f"AND session_id {mine}",  # (mine: constant SQL)
            (now(), agent),
        )

    def recorded(self, key: str) -> dict:
        """Another agent's process records (backend.copilot_records), as connect_agent kept
        them."""
        try:
            records = json.loads(self.store.get(key) or "{}")
        except ValueError:
            return {}
        return records if isinstance(records, dict) else {}

    def recorded_processes(self) -> dict:
        """What connect recorded of the processes of the Copilot CLI it started last (see
        backend.copilot_records): pid -> its record."""
        try:
            records = json.loads(self.store.get("copilot_pids") or "{}")
        except ValueError:
            return {}
        if not isinstance(records, dict):
            return {}
        if records and all(isinstance(record, str) for record in records.values()):
            # An earlier version kept only pid and command: they cannot be told from another
            # instance's (one whose data directory this is a copy of), so they are left
            # running. Forgotten, so this is said once.
            logger.warning(
                "Left running: Copilot processes %s recorded by an earlier version, which "
                "cannot be told from another Chief of Staff's.",
                ", ".join(records),
            )
            self.store.set("copilot_pids", "{}")
            return {}
        return records

    async def open_elsewhere(self) -> dict[str, int]:
        """Sessions another program (a terminal) has open now: session id -> its pid. Never
        those a Copilot CLI this app started has open, even one its loader no longer runs;
        other agents' too (a worker's conversation the owner resumed in a terminal)."""
        found = await self.connected().open_elsewhere()
        ours = copilot_ours(self.recorded_processes(), self.store.directory)
        return {session_id: pid for session_id, pid in found.items() if pid not in ours}

    async def agent_elsewhere(self, agent: str) -> dict[str, int]:
        """Another agent's conversations the owner resumed in a terminal: id -> its pid."""
        entry = self.agents.get(agent)
        found: dict[str, int] = {}
        if entry and entry.backend and entry.backend.alive():
            with suppress(Exception):
                found = await entry.backend.open_elsewhere()
        self.elsewhere_on[agent] = found
        return found

    def is_elsewhere(self, row: dict) -> int:
        """The process that has this session open in a terminal, as last seen (0: none)."""
        agent = self.agent(row["id"])
        seen = self.elsewhere if agent == COPILOT_AGENT else self.elsewhere_on.get(agent, {})
        return seen.get(self.copilot_id(row["id"]), 0)

    async def terminal_command(self, session_id: str) -> dict:
        """The command that continues a worker's conversation in a terminal (its agent's own),
        once Chief of Staff let go of it: never two programs writing to one conversation. Not
        while it works; it stays left alone while open there (refuse_if_open_elsewhere)."""
        row = self.store.session(session_id)
        if row["role"] != WORKER or row["closed_at"]:
            raise ValueError("Only an open worker can continue in a terminal.")
        if row["status"] in {"working", "starting", "queued"} or self.states[session_id].held:
            raise ValueError("It is at work: stop it first, or wait until it is done.")
        agent = self.agent(session_id)
        if self.states.is_live(session_id):
            await self.detach(session_id)
        return {
            "command": agents.resume_command(agent, self.copilot_id(session_id), row["cwd"]),
            "agent": agent,
        }

    async def watch_process(self, backend: AgentBackend, connection: int):
        """Copilot's CLI stopped by itself (crashed, killed): every request would fail, so the
        owner sees it, and it connects again."""
        while self.backend is backend and self.connections == connection:
            if not backend.alive():
                logger.warning("The Copilot CLI stopped by itself; connecting again.")
                self.status = {
                    "state": "error",
                    "error": "Copilot stopped unexpectedly. Connecting again…",
                }
                self.publish_state()
                self.recover_later(at_once=True)
                return
            await asyncio.sleep(PROCESS_CHECK)

    def recover_later(self, at_once: bool = False):
        if not self.stopping and not (self.recovery and not self.recovery.done()):
            self.recovery = asyncio.ensure_future(self.recover(at_once))

    async def recover(self, at_once: bool = False):
        """Connect again until Copilot runs, waiting longer after each failure; while the Router
        is open in a terminal, as soon as it is closed there."""
        delay = RECONNECT_DELAY
        wait = 0.0 if at_once else delay
        while not self.ready() and not self.stopping:
            await asyncio.sleep(wait)
            if self.ready() or self.stopping:
                return
            if await self.router_elsewhere():
                wait = RECONNECT_DELAY  # Looked at again shortly.
                continue
            try:
                await self.reconnect()
            except Exception:
                logger.warning("Connecting to Copilot again failed.", exc_info=True)
            wait, delay = delay, min(delay * 2, RECONNECT_MOST)

    async def router_elsewhere(self) -> int:
        """The process that has the Router open in another Copilot window, if one does."""
        router_id = self.store.get("router_id")
        if not router_id or not self.backend:
            return 0
        with suppress(Exception):
            self.elsewhere = await self.open_elsewhere()
        return self.elsewhere.get(self.copilot_id(router_id), 0)

    def ready(self) -> bool:
        return self.status["state"] == "ready" and self.backend is not None

    def require_ready(self) -> AgentBackend:
        """The connected backend; raises when Copilot is not connected."""
        if not self.ready() or self.backend is None:
            raise RuntimeError(
                "Copilot is not connected"
                + (f": {self.status['error']}" if self.status["error"] else ".")
            )
        return self.backend

    def connected(self) -> AgentBackend:
        """The backend, also while connecting (before the runtime is ready)."""
        if self.backend is None:
            return self.require_ready()
        return self.backend

    def background(self, coroutine: Awaitable) -> asyncio.Task:
        task = asyncio.ensure_future(coroutine)
        self.jobs.add(task)

        def done(finished: asyncio.Task):
            self.jobs.discard(finished)
            if not finished.cancelled() and finished.exception():
                logger.error("Background task failed", exc_info=finished.exception())

        task.add_done_callback(done)
        return task

    # Sessions

    def router_id(self) -> str:
        router_id = self.store.get("router_id")
        if not router_id:
            raise RuntimeError("The Router has not started yet.")
        return router_id

    def sandboxed(self, row: dict) -> bool:
        """Whether a worker's shell commands run in the sandbox (sandbox_policy)."""
        return bool(
            row["role"] == WORKER
            # Workers that read Slack always: what they read must not leave through a command.
            and (self.store.settings().worker_sandbox or row["slack"])
            and self.sandbox_host
            and self.sandboxes(row)
        )

    def sandbox_policy(self, row: dict) -> SandboxPolicy | None:
        """What a worker's shell commands may touch; None when they run unsandboxed, like the
        owner's own Copilot sessions (whose Copilot sandbox settings then apply, if any)."""
        if not self.sandboxed(row):
            return None
        checkouts = self.workspaces.checkouts(row)
        repositories = git_dirs(checkouts)
        settings = self.store.settings()
        writes = self.workspaces.writes(row)
        writable = [*writes, *repositories, *settings.sandbox_writable]
        # Its tools' caches: workers' own, the owner's read-only beneath (see caches.py).
        cached = self.cache_plan(row)
        writable += cached.readwrite
        protected = [
            path for git_dir in repositories for path in protected_git_paths(git_dir, checkouts)
        ]
        # One not there yet is held only where it would change what git runs or which
        # repository it uses (each held path costs every command a little): denied on macOS,
        # looked for after each turn on Linux (sandbox_paths).
        watched = set(watched_git_paths(repositories, checkouts, sandbox_platform() != "linux"))
        protected = [p for p in protected if p in watched or os.path.lexists(p)]
        if sandbox_platform() == "linux":
            self.git_watch(row["id"], [p for p in watched if not os.path.lexists(p)])
        readonly = list(protected)
        # Repository settings an agent would load and the files their hooks run, in each folder
        # it may write and those above it up to its top: its commands never change them, nor
        # (macOS) make one. In the owner's checkout, every agent's (the owner's own sessions
        # load them, with no check of Chief of Staff's). In a folder of its own, only what
        # Copilot loads by itself mid-session (SANDBOX_HELD: each path held costs every command
        # a little): the others load as its session starts or resumes, after the full check,
        # or through a tool, after its pre-tool check (pre_tool_wanted). A branch update that
        # needs to change a held one is not started: its prompt says to tell the owner. (What a
        # server runs stays writable: a change turns that server off, servers_off.) Not its own
        # workspace when it works in a checkout there: nothing loads them from that folder of
        # Chief of Staff's.
        gated: dict[str, None] = {}
        workspace = os.path.realpath(self.workspaces.workspace(row))
        cwd = os.path.realpath(row["cwd"])
        recorded = {os.path.realpath(c.get("path") or ""): c for c in checkouts}
        for folder in map(os.path.realpath, self.workspaces.writes(row)):
            if folder == workspace != cwd:
                continue
            real = Path(folder)
            checkout = recorded.get(folder)
            if checkout:
                top = checkout_top(checkout) or real
            else:
                top = Path(workspace) if within(folder, workspace) else outermost_top(real)
            owners = not within(folder, workspace) or bool(checkout and checkout.get("shared"))
            places = [real, *[p for p in real.parents if p.is_relative_to(top)]]
            for place in places:
                for name in AGENT_SETTINGS if owners else SANDBOX_HELD:
                    gated[str(place / name)] = None
            gated |= dict.fromkeys(self.run_files(top))
        readonly += list(gated)
        ssh = Path.home() / ".ssh"
        readonly += [str(ssh / name) for name in ("known_hosts", "config") if (ssh / name).exists()]
        if agent := os.environ.get("SSH_AUTH_SOCK"):
            writable.append(agent)  # Signing through the agent; key files stay unreadable.
        # Held as Copilot's sandbox can hold them: one that does not exist yet is denied on macOS,
        # not held on Linux (sandbox_paths).
        readonly, absent = sandbox_paths(readonly)
        # Chief of Staff's own code: held only where something writable holds it (nothing else
        # of it is writable to them, and workers never run it: no rule to pay for at each
        # command).
        readonly += caches.carve_outs(self.own_code(writes), writable)
        readonly += cached.readonly
        # Workers that read Slack must not read the owner's registry credentials. Nothing
        # else of the owner's needs closing: Copilot grants nothing of its own (dev-tool
        # access is off, see caches.TOOLCHAINS).
        closed = registry_secrets() if row["slack"] else []
        return SandboxPolicy(
            readwrite=tuple(dict.fromkeys(path for path in writable if path)),
            readonly=tuple(caches.fewest(list(dict.fromkeys(readonly)), writable)),
            # Workers that read Slack must not carry what they read anywhere but GitHub.
            allowed_hosts=GITHUB_HOSTS if row["slack"] else (),
            # Tests that start a server, Gradle's daemon and the like need localhost; not for
            # workers that read Slack, whose content must not reach the owner's local services.
            local_network=not row["slack"],
            # Its own daemons, yes; the owner's (their tokens are in these files), no.
            denied=tuple(dict.fromkeys([*service_secrets(), *closed, *cached.denied, *absent])),
        )

    def held_shown(self, row: dict) -> list[str]:
        """The files its folder's hooks run, as its prompt names them (from its folder's top;
        a few, then how many more)."""
        top = self.settings_place(row)[0]
        paths = sorted(
            os.path.relpath(path, top) if Path(path).is_relative_to(top) else path
            for path in self.run_files(top)
        )
        return [f"`{path}`" for path in paths[:8]] + (
            [f"and {len(paths) - 8} more"] if len(paths) > 8 else []
        )

    def held_run_files(self, row: dict) -> list[str]:
        """The files its folder's hooks run (run_files), for a worker's write."""
        if row["role"] != WORKER or not row["cwd"]:
            return []
        return self.run_files(self.settings_place(row)[0])

    def run_files(self, top: Path, servers: bool = False) -> list[str]:
        """The files the hooks (plugins, extensions) of a folder's top run, as its snapshot
        holds them, there or expected there: read-only to workers' commands (denied while not
        there, macOS) and refused to their edits, as the settings are. Not what a server runs
        (but with `servers`, for a look): a worker developing that server builds and edits it
        (a change turns it off for the worker, servers_off)."""
        snapshot = settings_guard.load(self.store, top)
        found = []
        for entries in (snapshot or {}).get("folders", {}).values():
            for entry, value in entries.items():
                _, ran, server = settings_guard.ran_of(entry)
                if not ran or value[0] == settings_guard.PROGRAM or (server and not servers):
                    continue
                found.append(str(Path(ran) if Path(ran).is_absolute() else top / ran))
        return found

    def pre_tool_wanted(self, row: dict, sandboxed: bool) -> bool:
        """Whether Copilot asks Chief of Staff before each of a worker's tools (Events.pre_tool):
        for every sandboxed worker, which keeps the sandbox's word: a setting it puts in place
        past the sandbox's paths (a folder renamed over `.github`) is found before the tool
        that would load it (a custom agent's `task`, the lsp tool, an extensions reload) and
        before its folder's own hook scripts' answers stand. Not for a worker without the
        sandbox (its commands run as the owner, and it would pay a few ms a tool): its tools
        are glanced at as they start (Events.tool_start), its turns checked before they start;
        unless a language server or a custom agent's is off for it (the tool that would start
        it, denied)."""
        if row["role"] != WORKER or not row["cwd"]:
            return False
        return sandboxed or any(
            server.startswith("lsp/") or "@" in server
            for server in self.servers_off.get(row["id"], {})
        )

    def sandboxes(self, row: dict) -> bool:
        """Whether its agent sandboxes its commands as Chief of Staff's sandbox says: Copilot's,
        and another's whose version's check showed its sandbox holding (agent_refused)."""
        if self.on_copilot(row["id"]) if row.get("id") else True:
            return bool(self.backend and self.backend.capabilities.sandbox)
        return self.agent_refused(self.agent(row["id"])).get("sandbox", SANDBOX_UNPROVEN) == ""

    def cache_plan(self, row: dict) -> caches.Plan:
        """A sandboxed worker's caches (caches.plan): what they add to its sandbox, and its
        shell's init script."""
        workspace = self.workspaces.workspace(row)
        rust = self.cache_rust.get(row["id"])
        if rust is None:  # Not opened through ready_caches (looked at here, once).
            rust = self.cache_rust[row["id"]] = caches.rust_project(self.project_folders(row))
        facts = self.cache_facts
        return caches.plan(
            self.cache_tier,
            workspace,
            row["id"],
            self.cache_scripts,
            slack=bool(row["slack"]),
            read_through=self.store.settings().shared_caches,
            go=facts.get("go"),
            truststore_path=facts.get("truststore"),
            rust=rust,
        )

    def project_folders(self, row: dict) -> list[Path]:
        return [
            self.workspaces.workspace(row),
            *(Path(c["path"]) for c in self.workspaces.checkouts(row)),
        ]

    def find_cache_facts(self):
        """What workers' caches need to know of the owner's tools, looked up as Copilot
        connects, on its own (whatever the caches settings, before any worker opens or seeding
        starts): GOPROXY and Go's caches, an old uv, node-gyp's headers, the Java truststore."""
        if self.cache_facts_job is None or self.cache_facts_job.done():
            self.cache_facts_job = asyncio.create_task(self.cache_facts_now())

    async def cache_facts_now(self):
        found: dict[str, Any] = {
            "go": await asyncio.to_thread(caches.go_env),
            "uv": await asyncio.to_thread(caches.uv_stamp),
            "old_uv": await asyncio.to_thread(caches.old_uv),
            "ca": await asyncio.to_thread(caches.authority_stamp, copilot_home()),
        }
        self.cache_facts.update(found)
        await self.new_truststore(found["ca"])

    async def new_truststore(self, authority: int):
        """Workers' Java truststore for Copilot's authority made at `authority`: named now (in
        their scripts, which use it once it is there) and made in the background (keytool takes
        about a second), so that no worker waits for it."""
        self.cache_facts["truststore"] = None
        if not authority:
            return
        try:
            await asyncio.to_thread(caches.java_folder, self.cache_tier)
        except OSError as error:
            logger.warning("workers' Java truststore: %s", error)
            return
        self.cache_facts["truststore"] = caches.truststore_file(self.cache_tier, authority)
        self.truststore_job = asyncio.create_task(self.make_truststore())

    async def make_truststore(self):
        try:
            await asyncio.to_thread(caches.truststore, self.cache_tier, copilot_home())
        except Exception as error:  # Java's tools go without it.
            logger.warning("workers' Java truststore: %s", error)

    async def ready_caches(self, row: dict):
        """Before a worker's session opens: the facts its caches need (waited for: they take
        well under a second, once a run), uv looked at again when its program changed (an
        upgrade), the truststore made again for a new authority, and whether its folders hold
        a Rust project (in a thread)."""
        if row["role"] != WORKER:
            return
        if self.cache_facts_job is None:
            self.find_cache_facts()
        with suppress(Exception):
            await asyncio.shield(self.cache_facts_job)  # type: ignore[arg-type]
        stamp = await asyncio.to_thread(caches.uv_stamp)
        if stamp != self.cache_facts.get("uv"):
            self.cache_facts["uv"] = stamp
            self.cache_facts["old_uv"] = await asyncio.to_thread(caches.old_uv)
        authority = await asyncio.to_thread(caches.authority_stamp, copilot_home())
        if authority != self.cache_facts.get("ca"):
            self.cache_facts["ca"] = authority
            await self.new_truststore(authority)
        self.cache_rust[row["id"]] = await asyncio.to_thread(
            caches.rust_project, self.project_folders(row)
        )

    async def apply_cache_env(self, row: dict, session: Any):
        """A sandboxed worker's shell gets its tools pointed at its caches: an init script
        Copilot sources before each of its commands (set at every open, created or resumed)."""
        found = self.cache_plan(row)
        try:
            path = await asyncio.to_thread(caches.prepare, found, row["id"], self.cache_scripts)
            if path is None or not await shell_init(session, [str(path)]):
                logger.warning("Worker %s's tools could not be given their caches", row["id"])
        except Exception:
            logger.warning("Worker %s's tools could not be given their caches", row["id"],
                           exc_info=True)  # fmt: skip
        if not row["slack"]:
            self.seed_caches_soon()

    def seed_caches_soon(self):
        """Fill workers' caches from the owner's, in the background (at most every
        caches.SEED_EVERY seconds): never on a worker's way."""
        if (
            not self.store.settings().shared_caches
            or self.cache_seeding
            or (self.cache_seeded and time.monotonic() - self.cache_seeded < caches.SEED_EVERY)
        ):
            return
        self.cache_seeding = True
        self.background(self.seed_caches())

    async def seed_caches(self):
        try:
            if self.cache_facts_job is None:
                self.find_cache_facts()
            with suppress(Exception):
                await asyncio.shield(self.cache_facts_job)  # type: ignore[arg-type]
            timings = await asyncio.to_thread(caches.seed, self.cache_tier, self.cache_owner())
            logger.info("Workers' caches filled from yours: %s", timings)
        finally:
            self.cache_seeding = False
            self.cache_seeded = time.monotonic()

    def cache_owner(self) -> caches.Owner:
        """Where the owner's caches and Go tools are (Go's as `go env` says, cache_facts)."""
        go = self.cache_facts.get("go") or {}
        return caches.Owner(
            Path.home(), go.get("GOMODCACHE", ""), go.get("GOCACHE", ""), caches.go_bin(go)
        )

    def caches_in_use(self) -> bool:
        """Whether a sandboxed worker on the owner's own tasks is working: its tools are in
        workers' caches (a worker that reads Slack has its own)."""
        rows = self.store.all(
            "SELECT * FROM sessions WHERE role=? AND closed_at IS NULL"
            " AND status IN ('working', 'starting')",
            (WORKER,),
        )
        return any(not row["slack"] and self.sandboxed(row) for row in rows)

    def own_code(self, writes: list[str]) -> list[str]:
        """Chief of Staff's own code and Python, read-only for every sandboxed worker
        (workspaces.own_code; found at connect, here only if that failed), but for what is in
        a folder the worker writes: Chief of Staff's own checkout, which the owner asked it to
        change in place."""
        if self.own_code_paths is None:
            self.own_code_paths = own_code()
        theirs = [os.path.realpath(path) for path in writes if not too_broad(path)]
        return [
            path
            for path in self.own_code_paths
            if not any(Path(os.path.realpath(path)).is_relative_to(folder) for folder in theirs)
        ]

    def yolo(self, row: dict) -> bool:
        """Whether this worker acts without permission checks (a session choice, else Settings)."""
        if row["role"] != WORKER:
            return False
        if row.get("yolo") in {"on", "off"}:
            return row["yolo"] == "on"
        # What it reads in Slack must not steer unchecked commands: on for it only by choice.
        return self.store.settings().worker_yolo and not row.get("slack")

    async def set_session_yolo(self, session_id: str, mode: str) -> dict:
        row = self.store.session(session_id)
        if row["role"] != WORKER:
            raise ValueError("Only workers have permission checks to switch.")
        if mode == "off" and not self.on_copilot(session_id):
            # Ask first, only where its agent's version was shown to ask before every action
            # (started to find out).
            await self.connect_agent(self.agent(session_id))
            if why := self.agent_refused(self.agent(session_id)).get("ask_first"):
                raise ValueError(why)
        acted = self.yolo(row)
        row = self.update_session(session_id, yolo=mode)
        stopped = False
        if not self.on_copilot(session_id) and self.yolo(row) != acted:
            stopped = await self.agent_mode_changed(row, asks_first=acted)
        else:
            await self.refresh_session(row)  # Its instructions mention the mode.
        if not stopped:
            return self.view(row)
        # Stopped on a task, it has Go on under Needs you; on a message alone, it is sent again.
        card = any(
            t["status"] == "waiting" and self.store.get(f"stopped:{t['id']}") == "Settings"
            for t in self.tasks.for_session(session_id)
        )
        return self.view(row) | {"switch_stopped": True, "switch_card": card}

    async def agent_mode_changed(self, row: dict, asks_first: bool) -> bool:
        """A worker on another agent switched between acting on its own and Ask first. Its
        agent takes up what differs (Codex: its apps, subagents, and which MCP calls ask) only
        in a conversation opened in a new process: its process ends, and its next message
        opens its conversation again in a new one. Switched to Ask first, one at work (or
        holding what it left running) stops first, its commands with it, as a Settings change
        stops a worker that may not run as it is: nothing more of it runs as it was. Switched
        the other way, it finishes its turn first (its settings were the stricter ones).
        Returns whether it was stopped."""
        session_id, state = row["id"], self.states[row["id"]]
        at_work = (
            row["status"] in {"working", "queued", "starting"}
            or state.held
            or state.busy
            or session_id in self.begun
        )
        if at_work and not asks_first:
            state.stale = True  # Opened again once its turn is over: in a new process.
            return False
        if at_work:
            # As stop_ineligible stops one: no turn admitted before begins after this, and one
            # that began with its status not said yet is stopped as one at work (its task waits
            # under Needs you, with Go on). Not halted: interrupt marks its late first event as
            # stopped (after_abort), and the detach below ends what was begun.
            async with self.admission:
                self.admission_generation += 1
            # What interrupt stops and leaves waiting with Go on: its task, if it is on one.
            on = self.tasks.find(self.working_on(session_id) or "")
            on_task = bool(on and on["status"] in {"queued", "running"}) or (
                row["status"] in {"queued", "starting"}
                and any(t["status"] == "queued" for t in self.tasks.for_session(session_id))
            )
            if row["status"] not in {"working", "queued", "starting"} and not state.held:
                self.update_session(session_id, status="working")
            name = agents.display(self.agent(session_id))
            then = "Go on continues" if on_task else "Send your message again to go on"
            await self.workers.interrupt(
                session_id,
                by="Settings",
                notice=f"Stopped: it asks first from now on, which {name} takes up in a new "
                f"conversation process. {then}, asking first.",
            )
        await self.detach(session_id)
        backend, _ = self.connection(self.agent(session_id))
        if retire := getattr(backend, "retire", None):
            await retire(session_id)  # Its next message opens it in a new process.
        return at_work

    async def refresh_session(self, row: dict):
        """The session picks up new instructions from its next turn."""
        if self.states[row["id"]].awaiting and self.states.is_live(row["id"]):
            # It has not started on what it was sent (Copilot may have given up on it, on the
            # settings it had): sent again now, on a new connection with the new ones.
            await self.send_again(row)
        # A worker that reported "done" may still be finishing its turn; one whose turn is over
        # with a command it left running (held) would lose that command's end, which wakes it.
        elif row["status"] == "working" or self.states[row["id"]].busy or self.held(row["id"]):
            self.states[row["id"]].stale = True  # Refreshed once its current turn ends.
        else:
            await self.detach(row["id"])

    def held(self, session_id: str) -> bool:
        return self.states[session_id].held

    async def send_again(self, row: dict):
        """What it was sent and never started on goes out again on a new connection."""
        state = self.states[row["id"]]
        if not state.awaiting:
            return
        _, mark = state.awaiting
        state.awaiting = None
        self.store.set(f"sent:{row['id']}", str(mark))
        await self.detach(row["id"])
        if row["status"] == "working":
            self.update_session(row["id"], status="idle", activity="")
        self.background(self.delivery.resume(row["id"]))

    async def not_started(self, session_id: str):
        """Copilot took a message and never started on it: it gives up on a session whose
        settings it cannot load (an MCP server's, say), with no error the session sees. The
        session says so, instead of working forever; what it was sent goes out again on a new
        connection, with the settings as they are then (Retry, or the next message)."""
        state = self.states[session_id]
        row = self.store.find_session(session_id)
        if not row or row["closed_at"] or not state.awaiting:
            return
        _, mark = state.awaiting
        state.awaiting = None
        self.store.set(f"sent:{session_id}", str(mark))
        await self.detach(session_id)
        message = (
            "Copilot took the message but never started on it. It does this when it cannot load "
            "a session's settings, such as an MCP server in Settings → Connections: check them, "
            + ("then Retry." if row["role"] == WORKER else "then write again.")
        )
        self.add_message(session_id, "error", message)
        if row["role"] != WORKER:
            self.update_session(session_id, status="idle", activity="")
            return
        self.worker_failed(row, message)

    async def add_repo(self, caller: str, repo: str):
        row = self.store.session(caller)
        workspace = self.workspaces.workspace(row)
        await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        slug = row["branch"].removeprefix(BRANCH_PREFIX) if row["branch"] else workspace.name
        checkouts = self.workspaces.checkouts(row)
        if repo.startswith("~") or Path(repo).is_absolute():
            local: Path | None = Path(repo)
        else:
            if row["slack"] and not self.yolo(row):
                # A clone URL can carry data out; Slack content must not steer that.
                raise ValueError("Workers that read Slack can only add configured projects.")
            self.store.record(caller, "links", [repo])
            # A copy on this machine (a project, or a fresh clone into a code folder) gets a
            # worktree like any project; without one the clone goes into the worker's folder.
            local = await self.workspaces.local_copy(repo)
        if local:
            # The project folder a worker shares with the owner is not a checkout of its own:
            # adding that project gives it one, a worktree to change it in.
            have = {
                checkout.get("repo", checkout["path"])
                for checkout in checkouts
                if checkout.get("branch") or checkout.get("worktree")
            }
            found = await self.workspaces.make_checkout(
                workspace, slug, str(local), True, have, carry=not row["slack"]
            )
            if not found:
                return "You already have a checkout of that repository: " + ", ".join(
                    checkout["path"] for checkout in checkouts
                )
            checkout = found
        else:
            checkout = await self.workspaces.clone_checkout(workspace, slug, repo)
        self.update_session(caller, repos=json.dumps([*checkouts, checkout]))
        self.add_message(caller, "notice", f"Added {checkout['path']} ({checkout['branch']})")
        result = {key: checkout[key] for key in ("path", "branch", "base")}
        if checkout.get("worktree") and self.sandbox_policy(row):
            # A worktree's history is in its repository's .git, outside what the sandbox let
            # this session write when it started: it is reattached with it after this turn.
            state = self.states[caller]
            state.stale = True
            state.after_refresh = (
                f"Your shell may now run git in {checkout['path']} (its history is "
                f"{checkout.get('git_dir', '')}). Go on with the task."
            )
            result["note"] = (
                "Edit files there now. git commands there (add, commit) work from your next "
                "turn, which Chief of Staff starts as soon as this one ends: end this turn when "
                "the rest is done."
            )
        return result

    async def add_folder(self, raw: str) -> dict:
        """A folder the owner added from the inbox: a repository becomes a project; a folder
        that is not one (like ~/workspace) becomes a code folder, whose repositories count."""
        path = await self.owner_path(raw)
        if await common_git_dir(path):
            return {"path": await self.add_project(str(path)), "kind": "project"}
        old = self.store.settings()
        if path in {Path.home(), Path(path.anchor)}:
            # Home holds the data too: what is wrong first is that it is not a code folder.
            raise ValueError("Choose the folder your repositories are in, like ~/workspace.")
        if self.store.directory.is_relative_to(path):
            raise ValueError("Choose a folder that does not contain Chief of Staff's own data.")
        if old.code_folder and old.code_folder != str(path):
            raise ValueError(f"Your code folder is {old.code_folder}; change it in Settings.")
        if old.code_folder != str(path):
            new = Settings.model_validate(
                {**old.model_dump(), "code_folder": str(path)}, context=AS_SAVED
            )
            self.store.save_settings(new)
            await self.settings_changed(old, new)
        repositories = [
            repo
            for repo in await asyncio.to_thread(self.workspaces.projects)
            if within(repo, str(path))
        ]
        return {"path": str(path), "kind": "code_folder", "repositories": len(repositories)}

    async def owner_path(self, raw: str) -> Path:
        path = Path(await asyncio.to_thread(os.path.expanduser, raw.strip()))
        if not path.is_absolute():
            raise ValueError("Give the folder as an absolute path, e.g. ~/code/app.")
        path = await asyncio.to_thread(path.resolve)
        if not await asyncio.to_thread(path.is_dir):
            raise ValueError(f"Not an existing folder: {raw}")
        return path

    async def add_project(self, raw: str) -> str:
        """Make a folder the owner chose a configured project (the owner's choice approves it)."""
        path = await self.owner_path(raw)
        old = self.store.settings()
        if any(within(str(path), project) for project in self.workspaces.projects()):
            return str(path)
        if self.store.directory.is_relative_to(path):
            raise ValueError("A project must not contain Chief of Staff's private data directory.")
        new = Settings.model_validate(
            {**old.model_dump(), "projects": [*old.projects, str(path)]}, context=AS_SAVED
        )
        self.store.save_settings(new)
        await self.settings_changed(old, new)
        return str(path)

    async def open_folder(self, session_id: str, app: str):
        row = self.store.session(session_id)
        folder = row["cwd"]
        if not folder or not await asyncio.to_thread(os.path.isdir, folder):
            raise ValueError(
                "Its worktree was removed when it closed; reopen it to bring it back."
                if row["closed_at"]
                else "This session's folder does not exist."
            )
        if sys.platform == "win32":
            # Through the shell: File Explorer, or VS Code by its URL (no code.cmd, so no cmd.exe).
            target = (
                folder if app != "vscode" else "vscode://file/" + quote(Path(folder).as_posix())
            )
            await asyncio.to_thread(os.startfile, target)
            return {"ok": True}
        if app == "vscode":
            command = ["code", folder] if shutil.which("code") else None
            if not command and sys.platform == "darwin":
                command = ["open", "-a", "Visual Studio Code", folder]
        else:
            command = ["open", folder] if sys.platform == "darwin" else ["xdg-open", folder]
        if not command or not shutil.which(command[0]):
            raise RuntimeError("No app to open the folder with was found.")
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, error = await asyncio.wait_for(process.communicate(), 15)
        except BaseException:
            with suppress(ProcessLookupError):
                process.kill()
            raise
        if process.returncode:
            raise RuntimeError(f"Could not open the folder: {error.decode(errors='replace')[:200]}")
        return {"ok": True}

    def role_dir(self, name: str) -> Path:
        path = self.work_root / name
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def helper_ids(self) -> set[str]:
        return {key.split(":", 1)[1] for kind in SPECIALS for key in self.store.keys(f"{kind}:")}

    def special(self, row: dict) -> str:
        """The kind of worker Chief of Staff started for itself ("" for the owner's work)."""
        if row["role"] != WORKER:
            return ""
        return next((kind for kind in SPECIALS if self.store.get(f"{kind}:{row['id']}")), "")

    def tool_names(self, row: dict) -> list[str]:
        if row["role"] == ROUTER:
            return ROUTER_TOOLS
        if (special := self.special(row)) in TOOLLESS:
            return TOOLLESS[special]
        if special == "connector":
            return CONNECTOR_TOOLS
        if special == "messenger":
            return MESSENGER_TOOLS + (SLACK_DM_TOOLS if self.connector.slack_dm() else [])
        if row["role"] == WATCHDOG:
            return ["assess_worker"]
        names = ["report", "follow_thread", "unfollow_thread"]
        if row["role"] == WORKER:
            names += [
                "check_back",
                "find_sessions",
                "ask_session",
                "answer_session",
                "tell_session",
                "add_repo",
            ]
            # Questions it writes reach other workers' prompts: not from Slack content.
            names += ["ack_decision"] if row["slack"] else ["ask_decision", "ack_decision"]
            if not row["slack"]:
                names.append("record_setup")
        if self.store.get(f"distill:{row['id']}"):
            names.append("propose_profile")
        if self.store.get(f"learner:{row['id']}"):
            names.append("propose_lessons")
        if row["role"] == SLACK or row["slack"]:
            names.append("draft_slack_reply")
        if row["role"] == WORKER:
            names += ["draft_reply", "draft_github_review"]
        if row["role"] == SLACK or row["slack"] or row["role"] == WORKER:
            names.append("withdraw_draft")  # Its own draft that turned out wrong.
        return names

    def session_prompt(self, row: dict, sandbox: SandboxPolicy | None) -> str:
        """The instructions a session runs with: its role's, with what it needs to know of its
        folders, sandbox and the owner (the same for every agent a worker runs on)."""
        settings = self.store.settings()
        prompt = PROMPTS[row["role"]]
        if (
            self.agents
            and row["role"] == WORKER
            and (agent := self.agent(row["id"])) != COPILOT_AGENT
        ):
            prompt = prompts.worker_on(agent, prompt)  # Its few Copilot lines in its words.
        if self.special(row) == "connector":
            prompt = prompts.CONNECTOR  # Not a worker with tasks to report.
        elif self.special(row) == "messenger":
            prompt = prompts.MESSENGER
        if row["role"] == WORKER:
            prompt += "\n" + prompts.worker_context(
                row["cwd"],
                str(self.workspaces.workspace(row)),
                self.workspaces.checkouts(row),
                self.yolo(row),
                ("on-no-gh" if self.sandbox_gh is False else "on") if sandbox else "",
                caches=bool(sandbox) and not row["slack"],
                local=bool(sandbox and sandbox.local_network),
                old_uv=self.cache_facts.get("old_uv", "") if sandbox else "",
                gh_accounts=self.github.accounts_cache[1],
                gh_acting=self.store.items("ghaccount:"),
                slack=bool(sandbox) and bool(row["slack"]),
                notes=self.cache_plan(row).notes if sandbox else [],
                held=self.held_shown(row) if sandbox else None,
            )
        if row["role"] == ROUTER and settings.send_to_chief:
            prompt = prompts.with_send_rule(prompt)
        if row["role"] == ROUTER and (on := self.agents_on()):
            prompt = prompts.with_agents_rule(prompt, [agents.display(name) for name in on])
        if (profile := self.store.get("profile", "")) and row["role"] != WATCHDOG:
            prompt += f"\n<owner_profile>\n{profile}\n</owner_profile>"
        prompt += self.learning.block(row)
        prompt += self.habits.block(row)
        if row["role"] == SLACK:
            prompt += self.drafts.style_examples()
        if row["role"] in {ROUTER, WORKER}:
            prompt += self.asks.setup_block()
        return prompt

    def options(self, row: dict) -> dict:
        if self.special(row) in TOOLLESS:
            return self.helper_options(row)
        settings = self.store.settings()
        sandbox = self.sandbox_policy(row)
        prompt = self.session_prompt(row, sandbox)
        tools = [self.tools[name] for name in self.tool_names(row)]
        if row["role"] == ROUTER and (on := self.agents_on()):
            # Its spawn_worker names the agents on (as without them otherwise).
            spawn = self.toolset.spawn_on(on)
            tools = [spawn if tool.name == "spawn_worker" else tool for tool in tools]
        options: dict[str, Any] = {
            # Copilot's record of past sessions, which the owner's own sessions can search: for
            # workers that do not read Slack. Sessions that read Slack are not recorded in it,
            # so what anyone wrote on Slack never reaches a session through it.
            "enable_session_store": row["role"] == WORKER and not row["slack"],
            "on_permission_request": self.events.on_permission,
            "tools": tools,
            "system_message": {"mode": "append", "content": prompt},
            "working_directory": row["cwd"] or str(self.role_dir("scratch")),
            "client_name": "chiefofstaff",
            # Commits made for the owner are theirs: no Copilot co-author line in them.
            "coauthor_enabled": False,
            "on_mcp_auth_request": self.asks.on_mcp_auth,
        }
        if row["role"] == WORKER and sandbox:
            options["sandbox_config"] = self.connected().sandbox(sandbox)
        if row["role"] == WORKER:
            # Everything the owner's own Copilot sessions load: plugins, the repository's MCP
            # servers, skills and custom agents (by default an SDK session loads only the
            # owner's MCP servers).
            options["enable_config_discovery"] = True
            options["tool_search"] = self.tool_search(row, len(tools))
            # Workers ask the owner directly: Copilot's ask_user, MCP forms and links.
            options["on_user_input_request"] = self.asks.on_user_input
            options["on_elicitation_request"] = self.asks.on_elicitation
            # What the owner added in Settings (see abilities.py).
            if skills := self.abilities.skill_directories():
                options["skill_directories"] = skills
            if settings.mcp_servers:
                options["mcp_servers"] = dict(settings.mcp_servers)
            if extra := os.environ.get("CHIEFOFSTAFF_WORKER_MCP"):
                options["mcp_servers"] = {**options.get("mcp_servers", {}), **json.loads(extra)}
            if hooks := os.environ.get("CHIEFOFSTAFF_WORKER_HOOKS"):
                options["hook_directories"] = [hooks]  # Test hooks only (like a plugin's).
            if self.pre_tool_wanted(row, sandbox is not None):
                # Before each of its tools (after its folder's own hooks): its folder's settings
                # looked at (Events.pre_tool); a server off for it, not started.
                options["hooks"] = {"on_pre_tool_use": self.events.pre_tool}
            if disabled := self.disabled_servers(row):
                options["disabled_mcp_servers"] = disabled
        tuning = self.tuning(row)
        model = self.model_for(row)
        if tuning and not model and (model := self.effective_model(row)):
            # Effort and context tier were checked against this model: pin it so they fit.
            self.store.set(f"pinned:{row['id']}", "1")
        if model:
            options["model"] = model
        options.update(tuning)
        if row["role"] == WORKER and settings.worker_credit_limit:
            options["session_limits"] = {"max_ai_credits": settings.worker_credit_limit}
        mcp = self.slack.session_mcp_config() if row["role"] == SLACK or row["slack"] else None
        if mcp:
            options["mcp_servers"] = {**options.get("mcp_servers", {}), "slack": mcp}
        if row["role"] != WORKER:
            # Router and Slack get nothing but their own tools (and Slack's read tools).
            slack_tools = [f"slack-{name}" for name in mcp["tools"]] if mcp else []
            options["available_tools"] = [tool.name for tool in tools] + slack_tools
            options["disabled_mcp_servers"] = [GITHUB_MCP]
            options["tool_search"] = self.tool_search(row, len(options["available_tools"]))
        return options

    def tool_search(self, row: dict, count: int) -> dict:
        """Copilot's tool search for a session with `count` tools of Chief of Staff's own (or,
        limited to a list, that many in all): on, whatever Copilot's own default is (off in
        1.0.112), unless the owner turned it off for their own sessions (Copilot keeps that over
        a session's). Copilot hides MCP tools behind a search once a session has more than 30
        tools: Chief of Staff's own (never hidden) must not tip a session over sooner than the
        owner's own sessions, whose MCP tools stay in plain sight in both, or are searched in
        both. Every MCP tool is searched once a worker's did not fit its model (fit_tools)."""
        enabled = owner_tool_search() is not False
        if self.all_searched(row):
            return {"enabled": enabled, "defer_threshold": 0}
        return {"enabled": enabled, "defer_threshold": COPILOT_DEFER_THRESHOLD + count}

    def searchable(self, row: dict) -> bool:
        """Whether a session loads MCP tools it does not list (a worker's), which a search can
        find: other sessions have their own tools only (and Slack's), all in sight."""
        return row["role"] == WORKER and self.special(row) not in TOOLLESS

    def all_searched(self, row: dict) -> bool:
        return self.searchable(row) and bool(self.store.get(f"toolfit:{row['id']}"))

    def helper_options(self, row: dict) -> dict:
        """A worker that only reads what it is given and calls its own tools."""
        tools = [self.tools[name] for name in self.tool_names(row)]
        options: dict[str, Any] = {
            "on_permission_request": self.events.on_permission,
            "tools": tools,
            "available_tools": [tool.name for tool in tools],
            "disabled_mcp_servers": [GITHUB_MCP],
            "system_message": {"mode": "append", "content": prompts.HELPER},
            "working_directory": str(self.role_dir("scratch")),
            "client_name": "chiefofstaff",
            "tool_search": self.tool_search(row, len(tools)),
        }
        if model := self.model_for(row):
            options["model"] = model
        options.update(self.tuning(row))
        return options

    def agent_options(self, row: dict) -> dict:
        """What another agent's worker runs with, in neutral form for its adapter to map: the
        same instructions and tools as a Copilot worker's, its folder, the owner's permission
        checks and questions, whether it acts on its own, the folders it works in (the agent
        trusts each as it would for the owner), and the model chosen for that agent. Copilot's
        options and model state are not used."""
        cwd = row["cwd"] or str(self.role_dir("scratch"))
        folders = [cwd] + [
            checkout["path"]
            for checkout in self.workspaces.checkouts(row)
            if checkout.get("path") and checkout["path"] != cwd
        ]
        chosen = agents.configured(self.store).get(self.agent(row["id"]), {})
        chosen = chosen | {
            key: value for key, value in self.agent_choice(row["id"]).items() if value
        }
        options: dict[str, Any] = {
            "system_message": {"content": self.session_prompt(row, None)},
            "tools": [self.tools[name] for name in self.tool_names(row)],
            "working_directory": cwd,
            "on_permission_request": self.events.on_permission,
            "on_user_input_request": self.asks.on_user_input,
            "on_elicitation_request": self.asks.on_elicitation,
            "on_mcp_auth_request": self.asks.on_mcp_auth,
            "ask_first": not self.yolo(row),
            "folders": folders,
            # The worker (its own process of the agent), and the child agents it had: the only
            # other threads its turns may reach.
            "worker": row["id"],
            "children": self.agent_children(row["id"]),
            # What tells the repository's instructions its thread was given (none: not yet).
            "docs_given": self.store.get(f"agentdocs:{row['id']}") or "",
            "on_repository_docs": lambda digest: self.store.set(f"agentdocs:{row['id']}", digest),
            # Whether its thread was made with Chief of Staff's ask_user (Codex's, since 0.1.47).
            "asks_owner": bool(self.store.get(f"agentasks:{row['id']}")),
            "on_asks_owner": lambda: self.store.set(f"agentasks:{row['id']}", "1"),
            # The folders whose Codex settings changed since Chief of Staff last saw them: its
            # threads do not trust them (codex_settings, which says so in its timeline).
            "settings_changed": lambda: self.codex_settings(self.store.session(row["id"])),
            # What its turns spend, told whatever became of its handle (agent_accounting).
            "on_accounting": lambda kind, data: self.agent_accounting(row["id"], kind, data),
        }
        # What the owner added in Settings, as a Copilot worker has it: MCP servers and skills.
        servers = dict(self.store.settings().mcp_servers)
        if extra := os.environ.get("CHIEFOFSTAFF_WORKER_MCP"):
            servers |= json.loads(extra)
        if servers:
            options["mcp_servers"] = servers
        if skills := self.abilities.skill_directories():
            options["skill_directories"] = skills
        if sandbox := self.sandbox_policy(row):
            options["sandbox"] = sandbox  # Its agent's own, as Chief of Staff's (agent_block).
        if row["slack"] and (slack := self.slack.session_mcp_config()):
            # Slack's read tools, as a Copilot worker that reads Slack has them (only those).
            options["mcp_servers"] = {**options.get("mcp_servers", {}), "slack": slack}
            options["slack_tools"] = list(slack.get("tools") or [])
        if model := chosen.get("model"):
            options["model"] = model
        if effort := chosen.get("effort"):
            options["reasoning_effort"] = effort
        return options

    def agent_children(self, session_id: str) -> list[str]:
        try:
            children = json.loads(self.store.get(f"agentchildren:{session_id}") or "[]")
        except ValueError:
            return []
        return [str(child) for child in children] if isinstance(children, list) else []

    def agent_notice(self, row: dict, text: str):
        self.add_message(row["id"], "notice", text)

    def agent_child(self, row: dict, thread: str):
        """A child agent a worker on another agent spawned: its turns may reach it, also from
        another process of that agent later (a resumed worker)."""
        children = self.agent_children(row["id"])
        if thread and thread not in children:
            self.store.set(f"agentchildren:{row['id']}", json.dumps([*children, thread]))

    def agent_choice(self, session_id: str) -> dict:
        """The model and effort a worker on another agent was given for itself (the Router's
        or the owner's choice; else its agent's in Settings): kept apart from its row's, which
        are Copilot's."""
        try:
            choice = json.loads(self.store.get(f"agentmodel:{session_id}") or "{}")
        except ValueError:
            return {}
        return choice if isinstance(choice, dict) else {}

    async def agent_models(self, name: str, *, start: bool = False) -> list[dict]:
        """An agent's models (as available_models has Copilot's): from its process while it
        runs (`start`: started for this), else as last listed."""
        cached_at, models = self.agent_models_cache.get(name, (0.0, []))
        if models and time.monotonic() - cached_at < 600:
            return models
        backend, _ = self.connection(name)
        if backend is None and start:
            backend = await self.connect_agent(name)
        if backend is None:
            return models
        models = [described(model) for model in await backend.list_models()]
        self.agent_models_cache[name] = (time.monotonic(), models)
        return models

    def agent_model_info(self, agent: str, model: str) -> dict | None:
        """One of an agent's models as last listed (its default for ""), as model_info."""
        models = self.agent_models_cache.get(agent, (0.0, []))[1]
        wanted = [m for m in models if (m["id"] == model if model else m["default"])]
        return wanted[0] if wanted else None

    async def look_for_agents(self) -> dict:
        """What the Coding agents group in Settings → Models shows, looked for only once the
        owner opens it: each supported agent found on this computer (or on), with its version
        and sign-in or why its workers cannot run here, and its models."""
        settings = self.store.settings()
        found = [await self.look_for_agent(name) for name in agents.NAMES]
        return {
            "agents": [entry for entry in found if entry["found"] or entry["enabled"]],
            # What it supports, for when none is found ("the Codex CLI").
            "supported": [agents.INSTALL[name] for name in agents.NAMES],
            "new_workers": agents.new_workers(self.store),
            "sandboxed": bool(settings.worker_sandbox and self.sandbox_host),
        }

    async def look_for_agent(self, name: str) -> dict:
        chosen = agents.configured(self.store).get(name, {})
        entry: dict[str, Any] = {
            "name": name,
            "label": agents.display(name),
            "enabled": bool(chosen.get("enabled")),
            "model": chosen.get("model") or "",
            "limit": agents.limit(self.store, name),
            "found": True,
            "state": "",
            "problem": "",
            "models": [],
        }
        if sys.platform == "win32":  # Said without looking for it.
            entry["problem"] = self.not_on_windows(name)
            return entry
        if not shutil.which(agents.command(name)):
            entry["found"] = False
            entry["problem"] = f"Not found on this computer: install {agents.INSTALL[name]}."
            return entry
        connection = self.agents.get(name)
        try:
            if connection and connection.backend:
                facts = await connection.backend.about()  # type: ignore[attr-defined]
            else:
                facts = await agents.look(name, self.agent_folder(name))
        except Exception as exc:
            entry["problem"] = describe(exc)
            return entry
        models = [described(model) for model in facts["models"]]
        self.agent_models_cache[name] = (time.monotonic(), models)
        key = facts.get("check_key") or facts["version"]
        try:
            checked = json.loads(self.store.get(f"agentcheck:{name}:{key}") or "{}")
        except ValueError:
            checked = {}
        signed = agents.SIGNED_IN.get(facts["account"], "signed in")
        entry |= {"version": facts["version"], "models": models}
        if isinstance(checked, dict) and checked.get("acting"):
            entry["problem"] = checked["acting"]
        elif connection and connection.offline:
            entry["problem"] = "Stopped unexpectedly; starts again with its next worker's message."
        else:
            entry["state"] = f"{facts['version']} · {signed}"
        if note := agents.PLAN_NOTES.get(facts["account"]):
            entry["note"] = note
        if limit := agents.plan_limit(self.store, name, facts.get("limit")):
            entry["limit_line"] = limit
        return entry

    def check_agents(self, plan: dict | None, settings: Settings):
        """A Settings save as it leaves the agents (`plan`: the Coding agents group saved, or
        None): one turned on must be able to run here (not on Windows, nor while workers are
        sandboxed, nor while its own folder is in Also writable in the sandbox); while one is
        on, its folder stays out of that list. Turning one off is never refused."""
        before = agents.turned_on(self.store)
        after = (
            before
            if plan is None
            else [n for n in agents.NAMES if (plan.get(n) or {}).get("enabled")]
        )
        home = Path.home().resolve()
        for name in after:
            label, fresh = agents.display(name), name not in before
            if fresh and sys.platform == "win32":
                raise ValueError(f"Use {label} for workers: {self.not_on_windows(name)}")
            if (
                fresh
                and settings.worker_sandbox
                and self.sandbox_host
                and not agents.can_sandbox(name)
            ):
                raise ValueError(
                    f"Use {label} for workers: {label} can't run while workers are sandboxed: "
                    f"{SANDBOX_UNPROVEN}."
                )
            for own in agents.folders(name, home):
                for folder in map(Path, settings.sandbox_writable):
                    if not (own.is_relative_to(folder) or folder.is_relative_to(own)):
                        continue
                    if fresh:
                        raise ValueError(
                            f"Use {label} for workers: first take {folder} out of Also writable "
                            f"in the sandbox: it holds {label}'s settings and hooks."
                        )
                    raise ValueError(
                        f"Also writable in the sandbox: the sandbox cannot open {folder}: it "
                        "holds keys or settings."
                    )

    async def check_agent_models(self, plan: dict):
        """A model newly named for an agent must be one it offers (as check_models does for
        Copilot's), where its models are known."""
        for name in agents.NAMES:
            model = (plan.get(name) or {}).get("model") or ""
            if not model or model == agents.configured(self.store).get(name, {}).get("model"):
                continue
            try:
                offered = [item["id"] for item in await self.agent_models(name)]
            except Exception:
                offered = []  # Unchecked while they cannot be listed.
            if offered and model not in offered:
                raise ValueError(
                    f"Model: {agents.display(name)} offers no model {model!r}. Available: "
                    f"{', '.join(offered)}."
                )

    def agents_on(self) -> list[str]:
        """The other coding agents on now (none, at no cost, when none was ever on)."""
        return agents.turned_on(self.store) if self.agents else []

    def agents_view(self) -> dict:
        """What the page knows of the agents on (in the snapshot only while one is)."""
        on = agents.turned_on(self.store)
        return {
            "on": on,
            "names": {name: agents.display(name) for name in on},
            "new_workers": agents.new_workers(self.store),
        }

    def agent_view(self, row: dict, agent: str) -> dict:
        """What a worker's view says of the agent it runs on, when that is not Copilot: its
        model there, its tokens (no credits), what it does differently and why it cannot ask
        first, where that applies."""
        own = self.agent_choice(row["id"])
        backend, _ = self.connection(agent)
        running = getattr(self.states.handle(row["id"]), "current_model", "")
        chosen = agents.configured(self.store).get(agent, {})
        return {
            "agent": agent,
            "agent_name": agents.display(agent),
            "model": own.get("model") or running or chosen.get("model") or "",
            "model_choice": own.get("model") or "",
            "effort": own.get("effort") or "",
            "effort_choice": own.get("effort") or "",
            "context": "",
            "credits": 0,
            "tokens": self.store.agent_spent(row["id"]),
            "agent_notes": notes(not self.yolo(row))
            if (notes := getattr(backend, "notes_for", None))
            else [],
            "ask_first_refused": getattr(backend, "ask_first_refused", "") if backend else "",
        }

    def chosen_model(self, row: dict) -> str:
        """The model chosen for the session: its own, its role's, or the default ("" for
        Copilot's)."""
        settings = self.store.settings()
        return row.get("model") or settings.role_models.get(row["role"]) or settings.model

    def fallback(self, row: dict) -> str:
        """How far Copilot's refusal took the session ("tuning": its model without the effort
        and context chosen; "model": Copilot's default model), while its model is still the one
        that was refused; "" otherwise."""
        if not row.get("id") or not (stage := self.store.get(f"fallback:{row['id']}")):
            return ""
        refused = self.store.get(f"fallbackfor:{row['id']}")
        return stage if refused is None or refused == self.chosen_model(row) else ""

    def forget_fallback(self, session_id: str):
        self.store.delete(f"fallback:{session_id}")
        self.store.delete(f"fallbackfor:{session_id}")

    def model_for(self, row: dict) -> str:
        chosen = self.chosen_model(row)
        if self.fallback(row) == "model":
            return ""  # Copilot refused it for this session: its default model instead.
        if chosen and chosen in self.refused_models():
            return ""
        return chosen

    def refused_models(self) -> set[str]:
        """Models Copilot refused to run ("not supported": the company's policy, say)."""
        return set(json.loads(self.store.get("refused_models") or "[]"))

    def effective_model(self, row: dict) -> str:
        """The configured model, or else the Copilot default as last seen in use."""
        return self.model_for(row) or self.store.get("copilot_default_model", "")

    def model_info(self, model: str) -> dict | None:
        return next((item for item in self.models_cache[1] if item["id"] == model), None)

    def tuning(self, row: dict, *, switching: bool = False) -> dict:
        """The chosen thinking effort and context tier, where the session's model supports them.

        Copilot rejects an effort the model does not offer, so nothing unchecked is passed. A
        model switch keeps the previous effort unless told, so `switching` also sets defaults.
        """
        info = self.model_info(self.effective_model(row))
        effort = row.get("effort") or self.store.settings().role_efforts.get(row["role"], "")
        if self.fallback(row):
            # Copilot refused this session's model with these options: the model's own defaults.
            if not info:
                return {}
            defaults: dict[str, str | None] = {}
            if info["efforts"]:
                defaults["reasoning_effort"] = default_effort(info)
            else:
                defaults["reasoning_effort"] = None  # Takes none: the one it had is cleared.
            if info["long_context"]:
                defaults["context_tier"] = "default"
            return defaults if switching else {}
        if not info:
            # Copilot's default model, before its first turn shows which it is: Copilot applies
            # the effort to it (open_live retries without one if the default takes none).
            return {"reasoning_effort": effort} if effort else {}
        result: dict[str, str | None] = {}
        if info["efforts"]:
            if effort in info["efforts"]:
                result["reasoning_effort"] = effort
            elif switching:
                result["reasoning_effort"] = default_effort(info)
        elif switching:
            result["reasoning_effort"] = None  # Takes none: the one it had is cleared.
        if info["long_context"]:
            if row.get("context") == "long_context":
                result["context_tier"] = "long_context"
            elif switching:
                result["context_tier"] = "default"
        return result

    def shown_effort(self, row: dict) -> str:
        info = self.model_info(self.effective_model(row))
        return self.tuning(row).get("reasoning_effort") or (info or {}).get("default_effort", "")

    def fall_back(self, row: dict) -> str:
        """Copilot refused the session's model: the next step back ("tuning", then "model"),
        recorded with the model it is for; "" when nothing is left to fall back to."""
        session_id = row["id"]
        stage = self.fallback(row)
        tuned = bool(self.tuning(row))
        chosen = self.model_for(row)
        if not stage and tuned:
            stage = "tuning"
        elif stage != "model" and chosen:
            stage = "model"
            refused = sorted(self.refused_models() | {chosen})
            self.store.set("refused_models", json.dumps(refused))
        else:
            return ""
        self.store.set(f"fallback:{session_id}", stage)
        self.store.set(f"fallbackfor:{session_id}", self.chosen_model(row))
        return stage

    def refused_at_start(self, row: dict, error: str) -> bool:
        """Copilot would not start the session on its model ("Model X is not available"): it
        starts on Copilot's default model instead, and so do others given that model, and the
        owner hears it in plain words. False when it had no model of its own to drop, and for
        another agent's session (Copilot's models are not its own)."""
        if not self.on_copilot(row["id"]):
            return False
        if not (chosen := self.model_for(row)) or not (
            MODEL_REFUSED.search(error) or (chosen in error and MODEL_NOT_OFFERED.search(error))
        ):
            return False
        self.store.set("refused_models", json.dumps(sorted(self.refused_models() | {chosen})))
        self.store.set(f"fallback:{row['id']}", "model")
        self.store.set(f"fallbackfor:{row['id']}", self.chosen_model(row))
        self.add_message(
            row["id"],
            "notice",
            f"Copilot does not offer the model {chosen}, so this worker uses Copilot's default "
            "model. Choose another in Settings (Models) or in this worker's header.",
        )
        return True

    async def recover_model(self, row: dict, error: str) -> bool:
        """Copilot refused the session's model ("not supported"): first without the extra
        options (thinking effort, long context), then on Copilot's default model. The turn that
        failed is sent again. False when there is nothing left to fall back to."""
        session_id = row["id"]
        chosen = self.model_for(row)
        if not (stage := self.fall_back(row)):
            return False
        what = (
            "the thinking effort and context size chosen for it"
            if stage == "tuning"
            else f"the model {chosen}"
        )
        self.add_message(
            session_id,
            "notice",
            f"Copilot refused {what} ({error.strip()[:160]}). It goes on with "
            + ("the model's own settings" if stage == "tuning" else "Copilot's default model")
            + "; choose another in Settings or this session's header. Your message is sent again.",
        )
        if session := self.states.handle(session_id):
            model = self.effective_model(self.store.session(session_id)) or "auto"
            tuning = self.tuning(self.store.session(session_id), switching=True)
            if model == "auto":
                tuning.pop("reasoning_effort", None)
            with suppress(Exception):
                await switch_model(session, model, **tuning)
        # What the failed turn was given is sent again.
        if (before := self.store.get(f"prevsent:{session_id}")) is not None:
            self.store.set(f"sent:{session_id}", before)
        self.background(self.delivery.resume(session_id))
        return True

    # A session whose tools do not fit its model: Copilot would not call the model, as the
    # instructions and tool definitions each turn starts with exceed what it takes
    # (Events.error, STATIC_BLOCKED). The steps that may make them fit, each taken once per
    # session, in this order; `toolfit:<id>` lists those taken ("none" once nothing is left).

    def next_fit(self, row: dict) -> str:
        """The next step for a session whose tools did not fit: "search" (every MCP tool behind
        tool search, Chief of Staff's own loaded; where the owner did not turn tool search off,
        which Copilot keeps over a session's), "long" (its model's long context, where it has
        one), "model" (a model whose own context is larger: larger_model); "" when none is."""
        taken = [step for step in (self.store.get(f"toolfit:{row['id']}") or "").split(",") if step]
        steps = ["search", "long", "model"]
        left = steps[steps.index(taken[-1]) + 1 :] if taken and taken[-1] in steps else steps
        if taken and taken[-1] not in steps:
            left = []
        info = self.model_info(self.effective_model(row))
        for step in left:
            if (
                (step == "search" and self.searchable(row) and owner_tool_search() is not False)
                or (
                    step == "long"
                    and info
                    and info["long_context"]
                    and row.get("context") != "long_context"
                )
                or (step == "model" and self.larger_model(row))
            ):
                return step
        return ""

    def larger_model(self, row: dict) -> str:
        """The model a session runs on once its tools fit neither its model's context nor that
        model's long context: one whose own context is larger than the largest tried, of the
        same maker where one is, the cheapest of those. "" when none is (or its model is not
        known)."""
        current = self.effective_model(row)
        if not (info := self.model_info(current)):
            return ""
        tried = info["context"]
        if row.get("context") == "long_context":
            tried = max(tried, info["long_context"])
        skip = self.refused_models() | {current, "auto"}
        larger = [m for m in self.models_cache[1] if m["id"] not in skip and m["context"] > tried]
        if not larger:
            return ""
        maker = current.split("-")[0]
        unknown = (float("inf"), float("inf"))
        chosen = min(
            larger,
            key=lambda m: (
                m["id"].split("-")[0] != maker,
                self.model_prices.get(m["id"], unknown),
                m["context"],
            ),
        )
        return chosen["id"]

    async def fit_tools(self, row: dict, message: str):
        """A session's tools did not fit its model: the next step (next_fit) is taken, said in
        a quiet line when it changes what the owner pays or which model runs, and its turn goes
        again on a session opened with it. With none left, it fails (tools_dont_fit)."""
        session_id = row["id"]
        try:
            if not (step := self.next_fit(row)):
                await self.tools_dont_fit(row)
                return
            taken = self.store.get(f"toolfit:{session_id}") or ""
            self.store.set(f"toolfit:{session_id}", f"{taken},{step}".lstrip(","))
            was = self.effective_model(row)
            noun = "worker" if row["role"] == WORKER else "session"
            if step == "long":
                row = self.update_session(session_id, context="long_context")
                self.add_message(
                    session_id,
                    "notice",
                    f"Used long context for this {noun}: its tools didn't fit {was}'s usual "
                    "context (long context costs more).",
                )
            elif step == "model":
                chosen = self.larger_model(row)
                row = self.update_session(session_id, model=chosen, context="")
                self.add_message(
                    session_id,
                    "notice",
                    f"Ran this {noun} on {chosen}: its tools didn't fit {was}.",
                )
            logger.info("%s's tools did not fit its model: %s.", session_id, step)
            await self.detach(session_id)  # Opened again with it, for the turn that goes again.
            self.events.turn_again(row, message)
            if row["role"] == WORKER:
                await self.delivery.resume(session_id)
            else:
                self.delivery.schedule_flush(session_id, 0)
        finally:
            self.states[session_id].retrying = False

    async def tools_dont_fit(self, row: dict):
        """Nothing made a session's tools fit: its turn fails, said in one plain line. A worker's
        task of that turn fails (those queued behind it wait, as they were), and the Router
        hears, once until the owner writes to it, that this is not a worker out of context: a
        new one fails the same way, and none starts for the same project (fit_refusal)."""
        session_id = row["id"]
        taken = [s for s in (self.store.get(f"toolfit:{session_id}") or "").split(",") if s]
        if "none" not in taken:
            self.store.set(f"toolfit:{session_id}", ",".join([*taken, "none"]))
        words = {"search": "tool search", "long": "long context", "model": "a larger model"}
        tried = [words[step] for step in taken if step in words]
        tried = [", ".join(tried[:-1]), tried[-1]] if len(tried) > 1 else tried
        servers = sorted(self.mcp_tools.get(session_id, {}).items(), key=lambda item: -item[1])
        most = ", ".join(f"{name} ({count} tools)" for name, count in servers[:2] if count)
        model = self.effective_model(row)
        info = self.model_info(model)
        size = ""
        if (tokens := self.tool_tokens.get(session_id)) and info:
            takes = info["long_context"] if row.get("context") == "long_context" else 0
            size = f" ({tokens:,} tokens of tools; it takes {takes or info['context']:,})"
        line = (
            f"Couldn't run this {'worker' if row['role'] == WORKER else 'session'}: its tools "
            f"don't fit {model or 'the model'}'s context{size}"
            + (f", even with {' and '.join(tried)}" if tried else "")
            + (f". Its MCP servers with the most tools: {most}" if most else "")
            + ". With fewer MCP servers it fits."
        )
        self.add_message(session_id, "error", line)
        if row["role"] != WORKER:
            self.update_session(session_id, status="idle", activity="")
            return
        task = self.worker_failed(row, line, told=None)
        if self.special(row) == "connector":
            return  # Watch for you's scan failed: told as its own (worker_failed).
        mark = self.owner_mark()
        if not self.special(row):
            self.store.set(f"nofit:{row.get('project') or ''}", str(mark))
        told = self.store.get(f"nofittold:{session_id}")
        self.store.set(f"nofittold:{session_id}", str(mark))
        if told is None or mark > int(told or 0):
            await self.tell_router(
                row,
                "failed",
                f"{line} This is not the worker running out of context: a new worker fails the "
                "same way, so don't start one for it (spawn_worker refuses one on this project "
                "until the owner writes to you). Tell the owner in one line.",
                task,
            )

    def owner_mark(self) -> int:
        """The owner's latest message to the Router (its id; 0 for none)."""
        router_id = self.store.get("router_id")
        found = self.store.value(
            "SELECT max(id) FROM messages WHERE session_id=? AND kind='owner'", (router_id,)
        )
        return int(found or 0)

    def fit_refusal(self, project: str) -> str:
        """Why no new Copilot worker starts on this project: one's tools fit no model
        (tools_dont_fit) and the owner has not written to the Router since; "" otherwise."""
        key = f"nofit:{project}"
        if (mark := self.store.get(key)) is None:
            return ""
        if self.owner_mark() > int(mark or 0):
            self.store.delete(key)
            return ""
        return (
            "Not started: a worker on this project just failed because its tools don't fit any "
            "model's context. That is not running out of context: a new worker fails the same "
            "way. Tell the owner in one line; once they write to you, one can start again."
        )

    def tools_dropped(self, row: dict, count: int, sent: set[str]):
        """Copilot left `count` of a session's tools out of its model call to fit (`sent`: those
        it sent). A worker's MCP tools all go behind tool search from its next turn (its own
        stay loaded: next_fit's first step, taken silently); if tools are still left out, the
        owner hears it once, and whether Chief of Staff's own were among them."""
        session_id = row["id"]
        if not self.on_copilot(session_id):
            return
        if self.next_fit(row) == "search":
            taken = self.store.get(f"toolfit:{session_id}") or ""
            self.store.set(f"toolfit:{session_id}", f"{taken},search".lstrip(","))
            logger.info(
                "%s: %d tools left out; its MCP tools go behind tool search.", session_id, count
            )
            return
        if self.store.get(f"toolsdropped:{session_id}"):
            return
        self.store.set(f"toolsdropped:{session_id}", "1")
        own = sorted(set(self.tool_names(row)) - sent)
        named = ", ".join(own[:3]) + ("…" if len(own) > 3 else "")
        noun = "worker" if row["role"] == WORKER else "session"
        # (Copilot keeps the owner's tool search off over a session's: said, as why.)
        off = (
            " (tool search is off in your Copilot settings)" if owner_tool_search() is False else ""
        )
        self.add_message(
            session_id,
            "notice",
            f"Copilot left out {count} of this {noun}'s tools to fit its model{off}, "
            + (
                f"Chief of Staff's own among them ({named})."
                if own
                else "none of Chief of Staff's own."
            ),
        )

    def effort_clearable(self, row: dict) -> bool:
        """Whether a turn refused for its thinking effort is cleared and sent again: once an
        hour at most (else it fails as any refused turn)."""
        key = f"effortcleared:{row['id']}"
        if time.time() - float(self.store.get(key) or 0) < 3600:
            return False
        self.store.set(key, str(time.time()))
        return True

    async def clear_effort(self, row: dict):
        """Copilot kept the thinking effort of the model the session had before, which its
        model now takes none of: cleared, and the failed turn sent again."""
        session_id = row["id"]
        if (session := self.states.handle(session_id)) and (model := self.effective_model(row)):
            with suppress(Exception):
                await switch_model(session, model, reasoning_effort=None)
        if (before := self.store.get(f"prevsent:{session_id}")) is not None:
            self.store.set(f"sent:{session_id}", before)
        self.background(self.delivery.resume(session_id))

    async def apply_model(self, row: dict):
        if row["status"] == "working":
            self.states[row["id"]].retuned = True
        if session := self.states.handle(row["id"]):
            if not self.model_for(row):
                self.store.set(f"pinned:{row['id']}", "1")
            model = self.effective_model(row) or "auto"
            tuning = self.tuning(row, switching=True)
            if model == "auto":
                tuning.pop("reasoning_effort", None)  # Auto takes none.
            await switch_model(session, model, **tuning)

    async def refresh(self, *roles: str):
        """Sessions of these roles pick up new prompts (profile, style) on their next use."""
        for row in self.store.open_sessions():
            if row["role"] in roles:
                await self.refresh_session(row)

    async def reattach(self, session_id: str):
        """After a turn: reconnect with current options, and go on if it was told it would."""
        note, self.states[session_id].after_refresh = self.states[session_id].after_refresh, ""
        await self.detach(session_id)
        if note and (row := self.store.find_session(session_id)) and not row["closed_at"]:
            try:
                await self.delivery.deliver(
                    session_id, wrap("system", "context", note), kind="notice", display=note
                )
            except Exception:
                logger.warning("Could not continue %s after a refresh.", session_id, exc_info=True)

    async def detach(self, session_id: str):
        """Disconnect from Copilot; the next use reconnects with the current options."""
        async with self.states[session_id].lifecycle:
            await self.drop_handle(session_id)

    async def drop_handle(self, session_id: str):
        """Disconnect; the caller holds the session's lifecycle lock."""
        state = self.states[session_id]
        state.stale = False
        state.after_refresh = ""  # The next connection has the new options from the start.
        state.steps.clear()  # A disconnected session is in no step.
        state.unread.clear()  # What it was sent is resent or replayed, not waiting in a turn.
        state.unseen.clear()
        state.echo_wait.clear()
        state.held = False  # Its next connection holds nothing.
        state.awaiting = None
        state.echoes = False
        if state.busy:
            # A disconnected session ends no turn: free its worker slot.
            state.busy = False
            self.background(self.workers.pump())
        self.turn_gone(session_id)
        if session := self.states.take_handle(session_id):
            with suppress(Exception):
                await session.disconnect()

    async def available_models(self) -> list[dict]:
        cached_at, models = self.models_cache
        if models and time.monotonic() - cached_at < 600:
            return models
        listed = [
            model
            for model in await self.connected().list_models()
            if not model.policy or model.policy.state != "disabled"
        ]
        models = [describe_model(model) for model in listed]
        self.model_prices = {model.id: model_price(model) for model in listed}
        self.models_cache = (time.monotonic(), models)
        return models

    async def check_model(self, model: str):
        if model and model not in {item["id"] for item in await self.available_models()}:
            ids = ", ".join(item["id"] for item in await self.available_models())
            raise ValueError(f"Unknown model {model!r}. Available: {ids}")

    async def set_session_model(
        self, session_id: str, model: str, effort: str = "", context: str = ""
    ) -> dict:
        if self.agents and not self.on_copilot(session_id):
            return await self.set_agent_model(session_id, model, effort, context)
        await self.check_model(model)
        row = self.store.session(session_id)
        info = self.model_info(self.effective_model({**row, "model": model}))
        if effort and effort not in (info["efforts"] if info else []):
            supported = ", ".join(info["efforts"]) if info and info["efforts"] else "none"
            raise ValueError(f"That model's thinking efforts: {supported}.")
        if context and not (info and info["long_context"]):
            raise ValueError("That model has no long-context option.")
        row = self.update_session(session_id, model=model, effort=effort, context=context)
        # The owner's choice now: tried again, even if Copilot refused it (or another) before.
        self.forget_fallback(session_id)
        self.unrefuse(model)
        await self.apply_model(row)
        return self.view(row)

    async def set_agent_model(self, session_id: str, model: str, effort: str, context: str) -> dict:
        """A worker on another agent: its model and effort there, from its next turn (kept apart
        from its row's, which are Copilot's; none of Copilot's model state changes)."""
        agent = self.agent(session_id)
        if context:
            raise ValueError("That model has no long-context option.")
        await self.check_agent_model(agent, model)
        configured = agents.configured(self.store).get(agent, {}).get("model") or ""
        info = self.agent_model_info(agent, model or configured)
        if effort and effort not in (info["efforts"] if info else []):
            supported = ", ".join(info["efforts"]) if info and info["efforts"] else "none"
            raise ValueError(f"That model's thinking efforts: {supported}.")
        if model or effort:
            self.store.set(
                f"agentmodel:{session_id}", json.dumps({"model": model, "effort": effort})
            )
        else:
            self.store.delete(f"agentmodel:{session_id}")
        if session := self.states.handle(session_id):
            # Back to the default: its agent's model in Settings, else that agent's own.
            fallback = model or configured or (self.agent_model_info(agent, "") or {}).get("id", "")
            await session.set_model(fallback, reasoning_effort=effort or None)
        return self.view(self.store.session(session_id))

    def unrefuse(self, *models: str):
        """Models the owner chose again since Copilot refused them are tried again."""
        refused = self.refused_models()
        if refused & set(models):
            self.store.set("refused_models", json.dumps(sorted(refused - set(models))))

    def copilot_id(self, row_id: str) -> str:
        """The Copilot session behind one of our sessions (the Router's changes when rotated)."""
        return self.store.get(f"copilot:{row_id}") or row_id

    def row_id(self, copilot_id: str) -> str:
        """The Chief of Staff session a Copilot session id belongs to."""
        return self.store.get(f"alias:{copilot_id}") or copilot_id

    async def open_live(self, row: dict, *, create: bool = False, copilot: str = ""):
        """Connect a session to Copilot: create it, or resume it from Copilot's own storage.

        Callers hold the session's lifecycle lock, or the session is new and nobody else knows it.
        """
        agent = self.agent(row["id"])
        if agent == COPILOT_AGENT:
            with suppress(Exception):
                await self.available_models()  # Efforts and context tiers depend on the model.
            # Agent settings in its folder changed since Chief of Staff last saw them (hooks, MCP
            # servers it would load): it does not open there until the owner says to use them.
            # (First: which of its servers it opens without is known then.)
            if line := await self.settings_refusal(row):
                raise SettingsChangedError(line)
            await self.ready_caches(row)
            options, trusted = self.options(row), self.trust(row)
        else:
            options, trusted = self.agent_options(row), {}
        copilot_id = copilot or self.copilot_id(row["id"])
        # Copilot refuses a session name with a double quote ("Write "x" to y").
        name = ("Chief of Staff · " + clean_title(row["title"]).replace('"', "'"))[:100]
        _, connection = self.connection(agent)
        if create:
            session = await self.without_unsupported_effort(
                row,
                options | trusted,
                lambda tuned: self.backend_for(row).create_session(
                    session_id=copilot_id, name=name, **tuned
                ),
            )
            self.store.set(f"created:{row['id']}", "1")
            if session.session_id != copilot_id:
                # The agent chose its session's id (Copilot takes ours): found as the Router's
                # successive Copilot sessions are.
                copilot_id = session.session_id
                self.store.set(f"alias:{copilot_id}", row["id"])
                self.store.set(f"copilot:{row['id']}", copilot_id)
            elif agent != COPILOT_AGENT:
                # It took the row's own id (Claude's): made, as made() reads an agent's row.
                self.store.set(f"copilot:{row['id']}", copilot_id)
            if agent == COPILOT_AGENT:
                # Copilot keeps a session only once it has had a message: until then it cannot
                # be resumed after a restart, and is created again instead.
                self.store.set(f"fresh:{copilot_id}", "1")
        else:
            await self.refuse_if_open_elsewhere(row)
            try:
                session = await self.without_unsupported_effort(
                    row,
                    options,
                    lambda tuned: self.backend_for(row).resume_session(copilot_id, **tuned),
                )
            except Exception as exc:
                if elsewhere := getattr(exc, "elsewhere", ""):
                    # Open in the owner's own app of that agent: left to it until a later try
                    # opens it here (nothing tries again by itself; its news waits for that).
                    self.store.set(f"agentopen:{row['id']}", now())
                    raise OpenElsewhereError(
                        f"“{row['title']}” is open in another {elsewhere} window: close it "
                        "there, then try again.",
                        0,
                    ) from None
                if not self.store.get(f"fresh:{copilot_id}"):
                    raise
                session = await self.without_unsupported_effort(
                    row,
                    options | trusted,
                    lambda tuned: self.backend_for(row).create_session(
                        session_id=copilot_id, name=name, **tuned
                    ),
                )
        if self.connection(agent)[1] != connection:
            # Its backend reconnected meanwhile: this session belongs to the old connection.
            with suppress(Exception):
                await session.disconnect()
            if agent == COPILOT_AGENT:
                raise RuntimeError("Copilot reconnected meanwhile; try again.")
            raise RuntimeError(f"{agents.display(agent)} started again meanwhile; try again.")
        if agent == COPILOT_AGENT and row["role"] == WORKER and self.sandbox_policy(row):
            await self.apply_cache_env(row, session)
        session_id = row["id"]
        loop = self.loop or asyncio.get_running_loop()

        def forward(event: Any):
            # The SDK calls this from its reader thread; events are handled on the loop.
            loop.call_soon_threadsafe(self.events.on_event, session_id, event, session)

        session.on(forward)
        self.states[session_id].handle = session
        if agent == COPILOT_AGENT and row["role"] == WORKER:
            self.servers_opened[session_id] = frozenset(self.servers_off.get(session_id, {}))
            self.searched_opened[session_id] = self.all_searched(row)
            if "hooks" in options:
                self.pre_tool_on[session_id] = "sandbox_config" in options
            else:
                self.pre_tool_on.pop(session_id, None)
            self.servers_said(row)
        if agent != COPILOT_AGENT and self.store.get(f"agentopen:{session_id}"):
            self.store.delete(f"agentopen:{session_id}")  # Closed there: its news goes now.
        model = self.model_for(row) if agent == COPILOT_AGENT else ""
        if not create and model and (info := self.model_info(model)) and not info["efforts"]:
            # Resumed on a model that takes no thinking effort: Copilot would keep the one it
            # had (from before the owner chose this model) and refuse every turn.
            with suppress(Exception):
                await switch_model(session, model, **self.tuning(row, switching=True))
        if not create and row["role"] == WORKER:
            await self.workers.import_history(row, session)
        self.publish_session(self.store.session(session_id))
        return session

    # The agent settings a worker's folder may carry (settings_guard): as Chief of Staff last
    # saw them, before every open and every turn.

    def settings_place(self, row: dict) -> tuple[Path, Path, dict | None]:
        """(top, cwd, checkout): whose agent settings a worker's turns load: from the top Chief
        of Staff recorded as it handed the folder out (checkout_top), never from what is in the
        folder now (a worker can make a `.git` folder that is no repository, where an agent
        looks past it); else its workspace; else the outermost top an agent would take."""
        cwd = Path(os.path.realpath(row["cwd"]))
        for checkout in json.loads(row.get("repos") or "[]"):
            top = checkout_top(checkout)
            if top and (cwd == top or cwd.is_relative_to(top)):
                return top, cwd, checkout
        workspace = Path(os.path.realpath(self.workspaces.workspace(row)))
        if cwd == workspace or cwd.is_relative_to(workspace):
            return workspace, cwd, None
        return outermost_top(cwd), cwd, None

    def settings_read(
        self, row: dict, names: tuple[str, ...]
    ) -> tuple[Path, Path, dict | None, dict, str, list[str]]:
        """(top, cwd, checkout, what these settings (one agent's) hold now, why they can't be
        read ("" when they can), the project code they run that nothing holds). Blocking: in a
        thread, once per check."""
        top, cwd, checkout = self.settings_place(row)
        code: list[str] = []
        try:
            now = settings_guard.scan(top, cwd, names, code, self.writable_roots)
            return top, cwd, checkout, now, "", code
        except settings_guard.TooLargeError as exc:
            return top, cwd, checkout, {}, str(exc), []

    async def settings_verdict(self, row: dict, names: tuple[str, ...]) -> dict | None:
        """What of these agent settings (one agent's) differs in a worker's folder from what
        Chief of Staff last saw: {"top", "cwd", "changes", "now", "digest", "unreadable",
        "first" (only settings seen there for the first time), "refused" (some it can never
        take as unchanged), "folders" (where they differ)}, or None for nothing. Read from the
        files every time: they are few and small. A folder first handed out (no worker had it,
        and this one has run nothing there yet) is kept as it is: the owner's. Any other folder
        (or setting) seen for the first time is known from then on with nothing in it taken:
        what is there is the owner's to look at once."""
        top, cwd, _, now, unreadable, code = await self.settings_thread(
            self.settings_read, row, names
        )
        stored = settings_guard.load(self.store, top)
        if stored is None and not unreadable:
            stored, said = await self.settings_thread(self.settings_handout, row, top, cwd)
            if said:
                self.add_message(row["id"], "notice", said)  # (On the event loop.)
        snapshot = stored or settings_guard.fresh()
        folders = [settings_guard.folder_key(top, f) for f in settings_guard.chain(top, cwd)]
        key = settings_guard.unchecked_key(names, folders)
        unchecked = key in snapshot["unchecked"]
        if unreadable:
            seen = await self.settings_thread(settings_guard.glimpse, top, cwd, names)
            if snapshot["unchecked"].get(key) == seen:
                return None  # The owner said to use them as they are (as far as they can be read).
            return {
                "top": top, "cwd": cwd, "changes": [], "now": {}, "unreadable": unreadable,
                "digest": seen, "first": False, "refused": False, "folders": folders,
            }  # fmt: skip
        if unchecked:
            # Checkable again: as they are now (the owner took them unchecked).
            snapshot["unchecked"].pop(key)
            settings_guard.taken(snapshot, now, folders, names)
        missing = {
            folder: wanted
            for folder in folders
            if (wanted := tuple(n for n in names if n not in snapshot["known"].get(folder, [])))
        }
        if missing or unchecked or stored is None:
            if stored is None:
                snapshot["untracked"] = True  # (Workers had it before this kept track.)
            settings_guard.unseen(snapshot, now, missing)
            settings_guard.save(self.store, top, snapshot)
        then = settings_guard.restricted(
            {folder: snapshot["folders"].get(folder, {}) for folder in folders}, names
        )
        now_named = settings_guard.restricted(now, names)
        self.settings_code(row, top, snapshot, code, names)
        # What a Copilot server runs turns that server off when it changed (servers_off); a
        # Codex thread does not trust the folder's settings then, as for any of them.
        servers = names == CODEX_SETTINGS
        if names == REPOSITORY_SETTINGS:
            self.servers_seen(row, settings_guard.servers_changed(now_named, then))
        found = settings_guard.changes(now_named, then, servers)
        if not found:
            return None
        shown = now_named if servers else settings_guard.without_servers(now_named)
        before = then if servers else settings_guard.without_servers(then)
        first = settings_guard.first_seen(snapshot, now_named, then, "first", servers)
        return {
            "top": top, "cwd": cwd, "changes": found, "now": now, "unreadable": "",
            "digest": settings_guard.digest(shown), "first": first,
            # (Where a worker could have written them: said so.)
            "touched": first and await self.settings_thread(self.workspaces.touched, top),
            "carried": settings_guard.first_seen(snapshot, now_named, then, "carried", servers),
            "untracked": bool(snapshot.get("untracked")),
            "refused": any(what == "refused" for what, _ in found),
            "folders": sorted(
                folder for folder in shown.keys() | before.keys()
                if (shown.get(folder) or {}) != (before.get(folder) or {})
            ),
        }  # fmt: skip

    def writable_roots(self) -> tuple[str, ...]:
        """The folders any worker's commands may write (real paths): workspaces, temporary
        folders, the owner's checkouts open workers change, their repositories' .git, what
        Settings adds. A setting leading there is never taken (settings_guard.inside)."""
        roots = [
            str(self.work_root), str(self.workspaces.scratch_root()), tempfile.gettempdir(),
            "/tmp", "/private/tmp", "/private/var/folders",  # noqa: S108  (Where workers write.)
            *self.store.settings().sandbox_writable,
            # Workers' own caches and, beside them, their temporary folders (caches.py).
            str(self.cache_tier), str(self.cache_tier.parent / caches.TEMP),
        ]  # fmt: skip
        for row in self.store.open_sessions(WORKER):
            roots += [*self.workspaces.writes(row), *git_dirs(self.workspaces.checkouts(row))]
        return tuple(dict.fromkeys(os.path.realpath(root) for root in roots if root))

    def settings_handout(self, row: dict, top: Path, cwd: Path) -> tuple[dict | None, str]:
        """A folder handed to a worker for the first time, before it ran anything there, no
        other worker having had it: its settings are the owner's, kept as they are (each
        agent's), and the line its timeline says once, of which ones its workers use. None when
        a worker may have run there already (one handed it out before snapshots were kept, or
        another's). Blocking (it reads them): no message is added from here."""
        if self.store.get(f"sent:{row['id']}") or self.workspaces.touched(top, row["id"]):
            return None, ""
        settings_guard.keep(self.store, top, cwd, (REPOSITORY_SETTINGS, CODEX_SETTINGS))
        snapshot = settings_guard.load(self.store, top)
        if not snapshot or not (listed := settings_guard.named(snapshot["folders"])):
            return snapshot, ""
        shown = ", ".join(listed[:5]) + (f" and {len(listed) - 5} more" if len(listed) > 5 else "")
        return snapshot, f"Workers in {home_shown(str(top))} use the agent settings there: {shown}."

    def settings_code(
        self, row: dict, top: Path, snapshot: dict, code: list[str], names: tuple[str, ...]
    ):
        """Its timeline says once (for the folder, while they stay the same) which commands of
        its settings run project code no snapshot holds (`uv run`, `npm run`, a module not found): a
        change to that code is not caught."""
        said = settings_guard.digest({"code": {line: [] for line in sorted(set(code))}})
        if not code or snapshot.get("code") == said:
            return
        snapshot["code"] = said
        settings_guard.save(self.store, top, snapshot)
        agent = "Codex" if names == CODEX_SETTINGS else "Copilot"
        unique = sorted(set(code))
        listed = "; ".join(unique[:2]) + (f" and {len(unique) - 2} more" if len(unique) > 2 else "")
        self.add_message(
            row["id"],
            "notice",
            f"{agent} settings in {home_shown(str(top))} run project code Chief of Staff can't "
            f"check ({listed}): a change to that code takes effect without asking you.",
        )

    def settings_said(
        self,
        session_id: str,
        line: str,
        verdict: dict,
        alert: bool = True,
        meta: dict | None = None,
    ):
        """Its timeline says it once for each change, with "Use these settings" (the owner's to
        choose: accept_settings) when they could be read and may be used; the owner hears of it
        when the worker waits for them (`alert`)."""
        said = settings_guard.digest({"line": {line: [verdict.get("digest") or ""]}})
        if self.store.get(f"settingssaid:{session_id}") == said:
            return
        self.store.set(f"settingssaid:{session_id}", said)
        meta = dict(meta or {})
        if not meta and verdict.get("digest") and not verdict.get("refused"):
            meta["agent_settings"] = {
                "digest": verdict["digest"],
                "changes": settings_guard.described(verdict["changes"]),
            }
        self.add_message(session_id, "notice", line, meta)
        if alert:
            self.alert(line, session_id=session_id, needs_action=True)
            # The Router hears it once too: what it sends the worker waits meanwhile.
            row = self.store.session(session_id)
            self.background(
                self.tell_router(
                    row, "waiting", f"It waits for the owner, its folder's settings changed: {line}"
                )
            )

    def settings_waiting(self, task: dict) -> str:
        """Why a task waits for the owner's word on its worker's folder's settings, "" when it
        does not: the one its worker is on (one card under Needs you, bug E98), or one they
        stopped mid-work (its Go on would wait for that word too)."""
        session_id = task["session_id"]
        if session_id not in self.settings_held:
            return ""
        row = self.store.find_session(session_id)
        if not row or row["closed_at"]:
            return ""
        if task["status"] == "waiting":
            if self.store.get(f"stopped:{task['id']}") != SETTINGS_STOP:
                return ""
        elif task["status"] not in {"queued", "running"} or task["id"] != (
            self.working_on(session_id) or row["task_id"]
        ):
            return ""
        reason = self.settings_reasons.get(session_id) or "Its folder's settings changed"
        return f"{reason}: it waits for your word"

    def settings_card(self, session_id: str) -> dict | None:
        """What its card's "Use these settings" sends (as its timeline's line): the newest
        change it was shown, when it can be used as it is."""
        found = self.store.one(
            "SELECT id, meta FROM messages WHERE session_id=? AND kind='notice' AND "
            "(json_extract(meta, '$.agent_settings') IS NOT NULL "
            "OR json_extract(meta, '$.agent_servers') IS NOT NULL) ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        if not found:
            return None
        meta = json.loads(found["meta"])
        return {**(meta.get("agent_settings") or meta["agent_servers"]), "message_id": found["id"]}

    def settings_shown(self, session_id: str):
        """Its tasks' cards say now whether they wait for the owner's word on its settings."""
        for task in self.tasks.for_session(session_id):
            if task["status"] in OPEN:
                self.task_actions.publish(task)

    def settings_unsaid(self, session_id: str):
        """Its folder's settings are as Chief of Staff last saw them again (or as the owner said
        to use them): one that waited for that goes on."""
        if self.store.get(f"settingssaid:{session_id}"):
            self.store.delete(f"settingssaid:{session_id}")
        self.settings_reasons.pop(session_id, None)
        if session_id in self.settings_held:
            self.settings_held.discard(session_id)
            self.settings_shown(session_id)
            row = self.store.find_session(session_id)
            if row and row["activity"] == SETTINGS_WAIT:
                self.update_session(session_id, activity="")
            if row and not row["closed_at"]:
                self.background(self.settings_go_on(row))
            self.background(self.workers.pump())

    @staticmethod
    def settings_reason(agent: str, verdict: dict) -> str:
        """Why its folder's settings stop it, in a few words (`agent`: "Copilot", "Codex")."""
        if verdict["unreadable"]:
            return f"{agent} settings in this folder can't be checked ({verdict['unreadable']})"
        if verdict["refused"]:
            refused = [path for what, path in verdict["changes"] if what == "refused"]
            return f"{agent} settings in this folder can't be used ({', '.join(refused[:3])})"
        listed = settings_guard.listed_entries(verdict["changes"])
        if verdict.get("carried"):
            # Carried from the owner's checkout, unlike what it holds: a worker could have
            # planted them there.
            return f"{agent} settings added since workers had this checkout ({listed})"
        if verdict["first"]:
            # Never as the owner's own: a worker could have put them there.
            if verdict.get("untracked"):
                return (
                    f"{agent} settings Chief of Staff hasn't checked yet, in a checkout workers "
                    f"have had ({listed})"
                )
            if verdict.get("touched"):
                return (
                    f"{agent} settings in a folder of this checkout workers haven't used yet but "
                    f"could write ({listed}); look at them once"
                )
            return (
                f"{agent} settings in a folder of this checkout workers haven't used yet "
                f"({listed}); look at them once"
            )
        changed = settings_guard.described(verdict["changes"])
        return f"{agent} settings in this folder changed ({changed})"

    async def settings_refusal(self, row: dict) -> str:
        """Before a Copilot worker opens or starts a turn: why not (its folder's Copilot
        settings differ from what the owner last let it use: Copilot would load them), "" when
        it may; said in its timeline. Within SETTINGS_SECONDS: past that, its folder can't be
        checked, and it waits."""
        if row["role"] != WORKER or not row["cwd"]:
            return ""
        try:
            return await asyncio.wait_for(self.settings_refused(row), SETTINGS_SECONDS)
        except TimeoutError:
            logger.warning("No look at %s's folder in time.", row["id"])
            return self.settings_unchecked(row, f"looking at it took over {SETTINGS_SECONDS:g} s")
        except settings_guard.SettingsBusyError:
            logger.warning("No look at %s's folder: too many at once.", row["id"])
            return self.settings_unchecked(row, "too many checks at once; it's looked at again")

    def settings_unchecked(self, row: dict, why: str) -> str:
        """Its line when its folder could not be looked at: it waits until it can (said once)."""
        reason = f"This worker's folder can't be checked ({why})"
        line = f"{reason}; Chief of Staff won't start the worker there until it can."
        self.settings_reasons[row["id"]] = reason
        self.settings_said(row["id"], line, {"digest": ""})
        return line

    async def settings_refused(self, row: dict) -> str:
        if replaced := await self.settings_thread(self.git_replaced, row):
            return self.git_replaced_said(row, replaced)
        if self.store.get(f"gitappeared:{row['id']}") and (line := await self.git_appeared(row)):
            return line
        if self.agent(row["id"]) != COPILOT_AGENT:
            return ""
        # Taken before they are read: a change while they are is a change at the next look.
        seen = await self.settings_thread(self.settings_signature, row)
        verdict = await self.settings_verdict(row, REPOSITORY_SETTINGS)
        if verdict is None:
            self.settings_unsaid(row["id"])
            self.settings_sigs[row["id"]] = seen
            return ""
        reason = self.settings_reason("Copilot", verdict)
        joined = f"{reason}{'. ' if '; ' in reason else '; '}Chief of Staff won't start the worker"
        if verdict["refused"]:
            line = f"{joined} there until that's removed."
        else:
            line = f"{joined} there until you say to use them."
        self.settings_reasons[row["id"]] = reason
        self.settings_said(row["id"], line, verdict)
        return line

    async def settings_stop(self, session_id: str):
        """A Copilot worker whose folder's settings changed: no turn starts until the owner says
        to use them (or they are as they were). One at work is stopped (their word goes on with
        it); one that is not is let go of, and what it was sent and never saw waits for it."""
        self.settings_held.add(session_id)
        row = self.store.session(session_id)
        if not self.states.is_live(session_id):
            if row["status"] == "working":
                # Nothing of it runs (it was to go on after a restart): at rest until then.
                self.update_session(session_id, status="idle")
        else:
            if row["status"] == "working" or self.states[session_id].held:
                reason = self.settings_reasons.get(session_id) or "its folder's settings changed"
                await self.workers.interrupt(
                    session_id, by=SETTINGS_STOP, notice=f"Stopped: {reason}."
                )
            else:
                self.delivery.unsee(session_id)
            await self.detach(session_id)
            if self.store.session(session_id)["status"] == "working":
                # (Let go of as its stopped turn was ending: no end of it comes now.)
                self.update_session(session_id, status="idle")
        if session_id in self.settings_held:  # (Not released meanwhile.)
            self.update_session(session_id, activity=SETTINGS_WAIT)
            self.settings_shown(session_id)

    # Linux: the files of a repository's .git that would change what git runs or which
    # repository it uses (workspaces.watched_git_paths: a commondir, a config.worktree where
    # those are read, hooks where it had none), not there as a worker's sandbox was set up and
    # not held by it when absent (workspaces.sandbox_paths). Looked for after each of its turns,
    # as it stops or closes, and before Chief of Staff runs git, off the event loop. One found
    # is never moved out of a repository the owner uses: the worker waits for the owner's word,
    # and Chief of Staff runs no git there until then.

    def git_watch(self, session_id: str, absent: list[str]):
        known = json.loads(self.store.get(f"gitwatch:{session_id}") or "[]")
        merged = sorted({*known, *absent})
        if merged != known:
            self.store.set(f"gitwatch:{session_id}", json.dumps(merged))

    def git_watch_scan(self, session_id: str = "") -> dict[str, list[str]]:
        """Blocking (a few lstat calls; in a thread): of this worker's watched paths (or every
        worker's: each to the first that looks for it), those there now, newly, kept as
        appeared (kv gitappeared): {worker: those}."""
        found: dict[str, list[str]] = {}
        keys = [f"gitwatch:{session_id}"] if session_id else self.store.keys("gitwatch:")
        for key in keys:
            owner = key.removeprefix("gitwatch:")
            pending = json.loads(self.store.get(f"gitappeared:{owner}") or "[]")
            watched = json.loads(self.store.get(key) or "[]")
            if new := [p for p in watched if p not in pending and os.path.lexists(p)]:
                self.store.set(f"gitappeared:{owner}", json.dumps(sorted({*pending, *new})))
                found[owner] = new
        return found

    async def git_watch_after(self, session_id: str, closing: bool = False):
        """Its turn over (or it stopped, or closes): what appeared, said; it waits."""
        found = await self.settings_thread(self.git_watch_scan, session_id)
        if closing:
            self.store.delete(f"gitwatch:{session_id}")
        for owner in found:
            row = self.store.find_session(owner)
            said = bool(row and await self.git_appeared(row))
            if said and row and not row["closed_at"] and not closing:
                await self.settings_stop(owner)

    async def git_appeared(self, row: dict) -> str:
        """Its line while a git file appeared in a .git it can write waits for the owner ("" for
        none: one they removed is let go of). "Use it"; "Keep it removed" too where only
        Chief of Staff's own worktree folders hold them."""
        pending = json.loads(self.store.get(f"gitappeared:{row['id']}") or "[]")
        there = [p for p in pending if await self.settings_thread(os.path.lexists, p)]
        if there != pending:
            if there:
                self.store.set(f"gitappeared:{row['id']}", json.dumps(there))
            else:
                self.store.delete(f"gitappeared:{row['id']}")
        if not there:
            return ""
        names = ", ".join(git_name(path) for path in there)
        line = f"A git file appeared in .git ({names}). If you made it, use it."
        admins = [c.get("admin") or "" for c in self.workspaces.checkouts(row) if c.get("worktree")]
        removable = all(any(a and within(p, a) for a in admins) for p in there)
        self.settings_reasons[row["id"]] = f"A git file appeared in .git ({names})"
        digest = settings_guard.digest({"appeared": {p: [] for p in there}})
        meta = {"git_quarantine": {"paths": there, "removable": removable}}
        self.settings_said(row["id"], line, {"digest": digest}, meta=meta)
        return line

    async def answer_git_quarantine(self, session_id: str, use: bool) -> str:
        """The owner's word on git files that appeared in a .git a worker can write: "Use it"
        (no longer looked for, by any worker) or, only in Chief of Staff's own worktree
        folders, "Keep it removed" (moved into its data folder, never deleted); the worker goes
        on."""
        row = self.store.session(session_id)
        pending = json.loads(self.store.get(f"gitappeared:{session_id}") or "[]")
        if not pending:
            return "Nothing waits for your word there."
        names = ", ".join(git_name(path) for path in pending)
        if use:
            for key in self.store.keys("gitwatch:"):
                watched = json.loads(self.store.get(key) or "[]")
                if kept := [path for path in watched if path not in pending]:
                    self.store.set(key, json.dumps(kept))
                else:
                    self.store.delete(key)
            said = f"You let workers use {names}."
        else:
            admins = [
                c.get("admin") or "" for c in self.workspaces.checkouts(row) if c.get("worktree")
            ]
            if not all(any(a and within(p, a) for a in admins) for p in pending):
                raise ValueError("That is your repository's: remove it yourself, or use it.")
            aside = self.store.directory / "quarantine" / session_id / str(int(time.time() * 1000))
            for path in pending:
                if await asyncio.to_thread(os.path.lexists, path):
                    moved = aside / path.lstrip(os.sep)
                    await asyncio.to_thread(moved.parent.mkdir, parents=True, exist_ok=True)
                    await asyncio.to_thread(shutil.move, path, moved)
            said = f"{names}: kept removed (a copy is in {home_shown(str(aside))})."
        self.store.delete(f"gitappeared:{session_id}")
        self.add_message(session_id, "notice", said)
        self.store.delete(f"settingssaid:{session_id}")
        await self.release_held(session_id)
        return said

    # A .git a worker replaced (workspaces.replaceable_git_paths: the sandbox holds paths, and
    # a folder moved into place brings what it holds past them): each folder's identity, kept
    # the first time it is seen; looked at before each turn, after each, and before Chief of
    # Staff runs git. One that is another waits for the owner's word.

    def git_known(self) -> dict[str, list]:
        """(Read each time: Chief of Staff forgets a worktree's when it makes it again.)"""
        return {
            key.removeprefix("gitid:"): json.loads(self.store.get(key) or "[]")
            for key in self.store.keys("gitid:")
        }

    def git_record(self, path: str, identity: list):
        self.store.set(f"gitid:{path}", json.dumps(identity))

    def git_replaced(self, row: dict) -> list[str]:
        """Of the .git folders this worker's commands may write, those replaced since first
        seen (kept then). One gone is let go of (an owner's worktree removed); an `objects/info`
        that wasn't there counts only once it holds alternates. Blocking (a few lstat calls)."""
        checkouts = self.workspaces.checkouts(row)
        paths = replaceable_git_paths(checkouts, self.workspaces.writes(row))
        return [path for path in paths if self.git_differs(path)]

    def git_differs(self, path: str, known_ids: dict | None = None) -> bool:
        raw = self.store.get(f"gitid:{path}") if known_ids is None else None
        known = known_ids.get(path) if known_ids is not None else (json.loads(raw) if raw else None)
        now = identity_of(path)
        if known is None or (not known and now and not holds_alternates(path)):
            if now or known is None:
                self.git_record(path, now + self.git_served(path, now))
            return False
        tree = known[-1] if known and isinstance(known[-1], dict) else None
        if not now:
            if tree and not os.path.lexists(tree["tree"]) and not self.git_tree_guarded(tree):
                # A worktree removed (its folder with it: `git worktree remove`): forgotten, so
                # one made again under that name is new.
                self.store.delete(f"gitid:{path}")
            return False
        if now == (known[:-1] if tree else known):
            return False
        if tree and self.git_remade(path, tree):
            self.git_record(path, now + self.git_served(path, now))
            return False
        return True

    def git_served(self, path: str, now: list) -> list:
        """Of a worktree's private folder in a repository (`.git/worktrees/x`): that
        worktree's folder (where its `gitdir` leads), as it is, for telling a worktree made
        again (git worktree remove, add) from a swap of the private folder under one that
        stays."""
        parent = Path(path).parent
        if not now or now[0] != "d" or parent.name != "worktrees":
            return []
        try:
            named = read_regular(Path(path) / "gitdir", 65536).decode(errors="replace").strip()
        except OSError:
            return []
        tree = os.path.realpath(Path(named).parent)
        return [{"tree": tree, "id": identity_of(tree)}]

    def git_remade(self, path: str, tree: dict) -> bool:
        """Whether a worktree's private folder was made again with its worktree (`git worktree
        add` under the same name, the one before removed): its worktree is another folder too,
        and the private folder still serves it. Never for a worktree a worker could make again
        itself that Chief of Staff or the owner run git in (git_tree_guarded): there, only the
        private folder as it was counts."""
        served = self.git_served(path, identity_of(path))
        if not served or served[0]["tree"] != tree["tree"] or served[0]["id"] == tree["id"]:
            return False
        return not self.git_tree_guarded(tree)

    def git_tree_guarded(self, tree: dict) -> bool:
        """Whether a worktree is a checkout handed to an open worker (Chief of Staff runs git
        there; the owner too, in theirs) that a worker's commands can reach: one they could
        remove and make again, with a private folder of their own."""
        place = tree["tree"]
        if not any(
            place == root or place.startswith(root + os.sep) for root in self.writable_roots()
        ):
            return False  # (Out of every worker's reach: made again by the owner.)
        for row in self.store.open_sessions(WORKER):
            for checkout in self.workspaces.checkouts(row):
                for key in ("path", "worktree", "top"):
                    if checkout.get(key) and os.path.realpath(checkout[key]) == place:
                        return True
        return False

    def clone_remade(self, checkout: dict):
        """A clone in a worker's folder that its worker made again where it was (deleted, cloned
        anew): read as it is now (its config never is: workspaces.private_git), its new .git
        recorded in that worker's checkouts and said once on its timeline."""
        for row in self.store.all(
            "SELECT id, repos FROM sessions WHERE role=? AND repos LIKE '[{%'", (WORKER,)
        ):
            repos = json.loads(row["repos"])
            found = [
                item for item in repos
                if item.get("remote") and item.get("repo") == checkout["repo"]
                and item.get("clone_id") != checkout["clone_id"]
            ]  # fmt: skip
            if not found:
                continue
            for item in found:
                item |= {"clone_git": checkout["clone_git"], "clone_id": checkout["clone_id"]}
            self.update_session(row["id"], repos=json.dumps(repos))
            self.add_message(
                row["id"],
                "notice",
                f"Its clone {home_shown(checkout['repo'])} was made again where it was: Chief "
                "of Staff reads the new one.",
            )

    def git_replaced_said(self, row: dict, replaced: list[str]) -> str:
        outermost = [
            path
            for path in replaced
            if not any(path.startswith(other.rstrip(os.sep) + os.sep) for other in replaced)
        ]  # (What a .git replaced holds was replaced with it: said once.)
        shown = [home_shown(path) for path in outermost]
        names = ", ".join(shown[:3]) + (f" and {len(shown) - 3} more" if len(shown) > 3 else "")
        reason = (
            f"{names} {'was' if len(shown) == 1 else 'were'} replaced since Chief of Staff first "
            "saw it, so git there could run what this worker put in its place"
        )
        line = (
            f"{reason}; Chief of Staff won't start the worker, nor run git there, until you say "
            "to use it."
        )
        self.settings_reasons[row["id"]] = reason
        digest = identity_digest(replaced)
        self.settings_said(
            row["id"], line, {"digest": digest}, meta={"git_replaced": {"digest": digest}}
        )
        return line

    def git_after_turn(self, session_id: str):
        """Its turn over: a .git it replaced is said now, and it waits (not at its next turn)."""
        row = self.store.find_session(session_id)
        if row and row["role"] == WORKER and row["cwd"] and (replaced := self.git_replaced(row)):
            self.git_replaced_said(row, replaced)
            self.background(self.settings_stop(session_id))

    def update_failed(self, row: dict, ran: dict, code: int | None, output: str):
        """A sandboxed worker's command that updates its branch (a merge, a rebase, a pull)
        failed where git could not change a file of its work tree (one the sandbox holds): the
        worker is told at once, within its turn, to put its tree back before going on. Words
        only: Chief of Staff runs no git in a worker's repository for this (a filter or a
        promisor there would run as it). Ordinary conflicts are the worker's own to resolve."""
        if (
            row["role"] != WORKER
            or code in (0, None)
            or ran["id"] in self.updates_said
            or not BRANCH_UPDATE.search(ran["command"])
            or not self.pre_tool_on.get(row["id"])  # (Its session runs sandboxed.)
            or NOT_FETCHED.search(output or "")
            or not any(
                not IN_GIT_FOLDER.search(found["path"])
                for found in UNCHANGEABLE.finditer(output or "")
            )
        ):
            return
        self.updates_said.add(ran["id"])
        self.background(
            self.delivery.deliver(
                row["id"],
                wrap("system", "update-failed", UPDATE_FAILED),
                kind="notice",
                display="Its branch update failed partway: asked it to put its tree back first.",
                steer=True,
            )
        )

    async def accept_git(self, session_id: str, digest: str) -> str:
        """The owner's word on a .git a worker's folder had replaced (git_replaced): what is
        there now is what Chief of Staff compares with from now on; the worker goes on."""
        row = self.store.session(session_id)
        replaced = await self.settings_thread(self.git_replaced, row)
        if not replaced:
            said = "It is as it was: nothing to change."
        elif identity_digest(replaced) != digest:
            raise ValueError("It changed again since: look at the latest line and try again.")
        else:
            for path in replaced:
                self.git_record(path, identity_of(path) + self.git_served(path, identity_of(path)))
            said = f"You let workers use {', '.join(home_shown(p) for p in replaced)} as it is."
            self.add_message(session_id, "notice", said)
            self.store.delete(f"settingssaid:{session_id}")
        await self.release_held(session_id)
        return said

    def git_guard(self, where: str = "", roots: tuple[str, ...] = ()):
        """Before Chief of Staff runs git (workspaces.guarded, in a thread): on Linux, what
        workers made in .git folders, looked for; then no git (ValueError) where a .git was
        replaced or a git file appeared, until the owner's word."""
        try:
            if sandbox_platform() == "linux" and self.store.keys("gitwatch:"):
                self.git_watch_scan()
            known = self.git_known()
            appeared = [
                path
                for key in self.store.keys("gitappeared:")
                for path in json.loads(self.store.get(key) or "[]")
            ]
        except Exception:
            logger.warning("Could not look at the .git folders workers use.", exc_info=True)
            return  # (A runtime stopping: its store closed.)
        if not (known or appeared) or not (where or any(roots)):
            return
        real = Path(os.path.realpath(where)) if where else None
        near = {str(place / ".git") for place in ([real, *real.parents] if real else [])}
        near |= {os.path.realpath(root) for root in roots if root}

        def close(path: str) -> bool:
            return any(path == root or path.startswith(root + os.sep) for root in near)

        if found := next((p for p in appeared if close(p) and os.path.lexists(p)), ""):
            raise ValueError(
                f"Chief of Staff runs no git there: {git_name(found)} appeared in its .git, and "
                "waits for your word."
            )
        for path in list(known):
            if close(path) and self.git_differs(path, known):
                raise ValueError(
                    f"Chief of Staff runs no git there: {home_shown(path)} was replaced "
                    "since it first saw it, and waits for your word."
                )

    async def release_held(self, session_id: str):
        """This worker goes on, and every other that waited for its folder (as it is now)."""
        for held in dict.fromkeys([session_id, *self.settings_held]):
            other = self.store.find_session(held)
            if not other or other["closed_at"]:
                self.settings_held.discard(held)
                continue
            if await self.settings_refusal(other):
                continue
            waited = held in self.settings_held
            self.settings_unsaid(held)  # (One that waited goes on.)
            if not waited:
                self.background(self.settings_go_on(other))

    async def settings_thread(self, function: Callable[..., Any], *args: Any) -> Any:
        """The blocking part of a look at a worker's folder (file system reads), in a few
        threads of its own: a file system slow to answer holds those, never the default pool
        the SDK and the rest use. At most SETTINGS_WAITING under way or waiting (one whose
        asker stopped waiting counts until it ends): past that, SettingsBusyError at once."""
        if self.settings_pool is None:
            if self.stopping:
                return await asyncio.to_thread(function, *args)  # (Its threads ended.)
            self.settings_pool = ThreadPoolExecutor(SETTINGS_THREADS, thread_name_prefix="settings")
        if not self.settings_room.acquire(blocking=False):
            raise settings_guard.SettingsBusyError("too many checks at once")
        try:
            future = self.settings_pool.submit(function, *args)
        except BaseException:
            self.settings_room.release()
            raise
        future.add_done_callback(lambda _: self.settings_room.release())
        return await asyncio.wrap_future(future)

    async def guard_thread(self, function: Callable[..., Any], *args: Any) -> Any:
        """Chief of Staff's git guard (workspaces.guarded), in threads of its own."""
        if self.guard_pool is None:
            if self.stopping:
                return await asyncio.to_thread(function, *args)
            self.guard_pool = ThreadPoolExecutor(GUARD_THREADS, thread_name_prefix="git-guard")
        return await asyncio.wrap_future(self.guard_pool.submit(function, *args))

    def settings_signature(self, row: dict) -> str:
        """Blocking (some lstat calls): settings_guard.signature of its folder's settings, the
        files their hooks and servers run and the .git folders it could replace; where those
        are is kept for the glances until its next full check (settings_look)."""
        top, cwd, _ = self.settings_place(row)
        also = [*self.run_files(top, servers=True)]
        also += replaceable_git_paths(self.workspaces.checkouts(row), self.workspaces.writes(row))
        self.settings_paths[row["id"]] = (top, cwd, tuple(also))
        return settings_guard.signature(top, cwd, AGENT_SETTINGS, also)

    def settings_glance(self, session_id: str):
        """As a Copilot worker starts a sub-agent (Copilot loads a custom agent and its MCP
        servers there, mid-turn), or a tool when Copilot does not ask first (pre_tool_wanted):
        what the file system says of its settings, against its last full check's; the full
        check (stopping it) only when that changed. Off the event loop; it does not wait for
        this."""
        if session_id not in self.settings_sigs:
            return
        self.background(self.settings_glanced(session_id))

    async def settings_look(self, row: dict) -> str:
        """Before each tool a Copilot worker runs (Events.pre_tool, which Copilot waits for), and
        as each of its model rounds starts (settings_backstop): what the file system says of its
        folder's settings (some lstat calls, off the event loop), against its last full check's;
        when that changed, the full check, whose line says why the tool may not run ("" when it
        may: a script Copilot made executable)."""
        if paths := self.settings_paths.get(row["id"]):
            # (Where its last full check found them: the files' places alone, no snapshot read.)
            now = await self.settings_thread(settings_guard.signature, paths[0], paths[1],
                                          AGENT_SETTINGS, list(paths[2]))  # fmt: skip
        else:
            now = await self.settings_thread(self.settings_signature, row)
        if now == self.settings_sigs.get(row["id"]):
            return ""
        if line := await self.settings_refusal(row):
            self.background(self.settings_stop(row["id"]))
        return line

    # A server a Copilot worker's folder settings run (MCP, language, a custom agent's) whose
    # files differ from what the owner last let it use (a build's output, its edited source):
    # off for that worker from its next start, never a wait nor a stop mid-turn; its timeline
    # says so once, and "Use these settings" there takes it as it is then.

    def servers_seen(self, row: dict, servers: dict[str, list[str]]):
        """From each check of a Copilot worker's folder (settings_verdict): its servers off now.
        A live session opened otherwise is opened again before its next turn (servers_turn),
        or once the one under way is over."""
        if servers:
            self.servers_off[row["id"]] = servers
        else:
            self.servers_off.pop(row["id"], None)
        state = self.states[row["id"]]
        if (
            self.states.is_live(row["id"])
            and self.servers_opened.get(row["id"], frozenset()) != frozenset(servers)
            and (row["status"] == "working" or state.busy or state.held)
        ):
            state.stale = True

    async def servers_turn(self, session_id: str, claimed: bool = False):
        """Before a turn Chief of Staff sends a Copilot worker: a session opened with other
        servers off than now is opened again (Copilot turns MCP servers off as a session opens,
        not in one already running). `claimed`: its worker slot is taken for this turn."""
        if not self.states.is_live(session_id):
            return
        row = self.store.session(session_id)
        if self.servers_opened.get(session_id, frozenset()) == frozenset(
            self.servers_off.get(session_id, {})
        ) and self.searched_opened.get(session_id, False) == self.all_searched(row):
            return
        state = self.states[session_id]
        if row["status"] == "working" or state.held or (state.busy and not claimed):
            state.stale = True
            return
        busy, state.busy = state.busy, False  # (Its slot stays taken.)
        try:
            async with state.lifecycle:
                if self.store.session(session_id)["status"] == "working" or state.held:
                    # A turn Copilot started itself meanwhile: opened again after it.
                    logger.info("%s began a turn as its servers changed.", session_id)
                    state.stale = True
                    return
                logger.info("Opening %s again: its servers changed.", session_id)
                await self.drop_handle(session_id)
        finally:
            state.busy = busy

    def disabled_servers(self, row: dict) -> list[str]:
        """The MCP servers (a custom agent's too) a Copilot worker opens without."""
        return sorted(
            {
                server.partition("/")[2].partition("@")[0]
                for server in self.servers_off.get(row["id"], {})
                if server.startswith("mcp/")
            }
        )

    def server_refusal(self, row: dict, tool: str, arguments: Any) -> str:
        """Before a tool (Events.pre_tool), where a server off for the worker would start: a
        language server's (the lsp tool), a custom agent's (the task that runs that agent)."""
        off = self.servers_off.get(row["id"])
        if not off:
            return ""
        servers: list[str] = []
        if tool == "lsp":
            servers = [server for server in off if server.startswith("lsp/")]
        elif tool == "task" and isinstance(arguments, dict):
            # (Copilot takes the agent's name as it is written; any case here.)
            agent = str(arguments.get("agent_type") or "").casefold()
            servers = [s for s in off if agent and s.partition("@")[2].casefold() == agent]
        if not servers:
            return ""
        return (
            f"The {server_labels(servers)} changed in this folder since the owner last let you use "
            f"{'it' if len(servers) == 1 else 'them'}: it's off for you. Work without it; the "
            "owner can turn it on from your timeline."
        )

    def servers_said(self, row: dict):
        """Opened with servers off: its timeline says so once (each, while it stays off)."""
        off = self.servers_off.get(row["id"], {})
        key = f"serversoff:{row['id']}"
        said = set(json.loads(self.store.get(key) or "[]"))
        if set(off) == said:
            return
        if off:
            self.store.set(key, json.dumps(sorted(off)))
        else:
            self.store.delete(key)
        if not set(off) - said:
            return  # (Fewer off: said as they were turned on.)
        paths = sorted({path for server in off for path in off[server]})
        line = (
            f"Its {server_labels(sorted(off))} changed in this worker's folder, so it runs "
            f"without {'it' if len(off) == 1 else 'them'}."
        )
        self.add_message(
            row["id"],
            "notice",
            line,
            {"agent_servers": {"digest": servers_digest(off), "changes": ", ".join(paths)}},
        )

    async def accept_servers(self, session_id: str, digest: str) -> str:
        """The owner says to use the servers its line names (Use these settings): what they
        run now becomes what Chief of Staff compares with; its next turn opens with them."""
        row = self.store.session(session_id)
        verdict = await self.settings_verdict(row, REPOSITORY_SETTINGS)
        off = self.servers_off.get(session_id, {})
        if not off:
            self.servers_said(row)
            return "They are as they were: nothing to change."
        if servers_digest(off) != digest:
            raise ValueError("They changed again since: look at the latest line and try again.")
        if verdict and verdict["unreadable"]:
            raise ValueError(f"These can't be checked ({verdict['unreadable']}).")
        top, _, _, now, unreadable, _ = await self.settings_thread(
            self.settings_read, row, REPOSITORY_SETTINGS
        )
        if unreadable:
            raise ValueError(f"These can't be checked ({unreadable}).")
        snapshot = settings_guard.load(self.store, top) or settings_guard.fresh()
        settings_guard.taken_servers(snapshot, now, set(off))
        settings_guard.save(self.store, top, snapshot)
        said = f"You let this worker use its {server_labels(sorted(off))} again."
        self.servers_off.pop(session_id, None)
        self.servers_said(row)
        self.add_message(session_id, "notice", said)
        await self.refresh_session(row)  # Its next turn opens with them.
        return said

    async def settings_glanced(self, session_id: str):
        await self.settings_backstop(session_id)

    async def settings_backstop(self, session_id: str):
        """A turn a Copilot worker started (from Copilot's own queue, say), each of its model
        rounds: stopped at once if its folder's settings changed meanwhile (a glance, the full
        check only when that changed: settings_look), or if they could not be looked at."""
        row = self.store.find_session(session_id)
        if not row or row["closed_at"] or not row["cwd"]:
            return
        try:
            await asyncio.wait_for(self.settings_look(row), SETTINGS_SECONDS)
        except Exception as exc:
            if self.stopping:
                return
            logger.warning("Could not look at %s's agent settings.", session_id, exc_info=exc)
            if isinstance(exc, TimeoutError):
                self.settings_unchecked(row, f"looking at it took over {SETTINGS_SECONDS:g} s")
            elif isinstance(exc, settings_guard.SettingsBusyError):
                self.settings_unchecked(row, "too many checks at once; it's looked at again")
            else:
                self.settings_unchecked(row, "Chief of Staff could not read it")
            await self.settings_stop(session_id)

    async def codex_settings(self, row: dict) -> list[str]:
        """The folders whose Codex settings differ from what Chief of Staff last saw (a Codex
        thread is not to trust them); its timeline says so."""
        verdict = await self.settings_verdict(row, CODEX_SETTINGS)
        if verdict is None:
            self.settings_unsaid(row["id"])
            return []
        reason = self.settings_reason("Codex", verdict)
        joined = f"{reason}{'. Codex' if '; ' in reason else ', so Codex'} won't load them for this"
        if verdict["unreadable"]:
            line = f"{joined} worker."
        elif verdict["refused"]:
            line = f"{joined} worker until that's removed."
        else:
            line = f"{joined} worker until you say to use them."
        top = verdict["top"]
        folders = [str(top if folder == "." else top / folder) for folder in verdict["folders"]]
        self.settings_said(row["id"], line, verdict, alert=False)  # It goes on without them.
        return folders

    async def accept_settings(self, session_id: str, digest: str) -> str:
        """The owner says to use the settings now in a worker's folder (its timeline's "Use
        these settings"): they become what Chief of Staff compares with, and the worker goes on
        with what waited for it. Only when they are still what the owner was shown; ones too
        many to check are used unchecked from then on (until they can be checked again)."""
        if digest.startswith("servers:"):
            return await self.accept_servers(session_id, digest)
        row = self.store.session(session_id)
        names = REPOSITORY_SETTINGS if self.agent(session_id) == COPILOT_AGENT else CODEX_SETTINGS
        verdict = await self.settings_verdict(row, names)
        if verdict is None:
            said = "They are as they were: nothing to change."
        elif verdict["digest"] != digest:
            raise ValueError("They changed again since: look at the latest line and try again.")
        elif verdict["refused"]:
            raise ValueError(f"{self.settings_reason('These', verdict)}: remove that first.")
        else:
            top, cwd = verdict["top"], verdict["cwd"]
            snapshot = settings_guard.load(self.store, top) or settings_guard.fresh()
            if verdict["unreadable"]:
                folders = [
                    settings_guard.folder_key(top, f) for f in settings_guard.chain(top, cwd)
                ]
                snapshot["unchecked"][settings_guard.unchecked_key(names, folders)] = digest
                said = f"You let this worker use them unchecked ({verdict['unreadable']})."
            else:
                # As the owner was shown them (a change since is a change again).
                folders = [
                    settings_guard.folder_key(top, f) for f in settings_guard.chain(top, cwd)
                ]
                settings_guard.taken(snapshot, verdict["now"], folders, names)
                if verdict.get("carried"):
                    self.carried_theirs(row, top, verdict["now"])
                changed = settings_guard.described(verdict["changes"])
                said = f"You let this worker use these settings ({changed})."
            settings_guard.save(self.store, top, snapshot)
            self.add_message(session_id, "notice", said)
            self.store.delete(f"settingssaid:{session_id}")
        if names == CODEX_SETTINGS:
            if verdict is not None and self.states.is_live(session_id):
                # Its thread trusts them once opened again: now, or once its turn is over.
                await self.refresh_session(row)
            return said
        # This worker goes on, and every other that waited for them (in the same folder).
        await self.release_held(session_id)
        return said

    def carried_theirs(self, row: dict, top: Path, now: dict[str, dict]):
        """Settings carried into a worker's worktree from the owner's checkout that the owner
        said to use: theirs in that checkout too, as they are now (its snapshot's), so the next
        worktree that carries the same is not asked about again."""
        for checkout in self.workspaces.checkouts(row):
            if checkout_top(checkout) != top or not checkout.get("carried"):
                continue
            owner = Path(os.path.realpath(checkout.get("repo") or checkout["project"]))
            theirs = settings_guard.load(self.store, owner)
            if theirs is None:
                continue  # No worker had it: what is there now is theirs already (vet).
            carried = set(checkout["carried"])
            for folder, entries in now.items():
                prefix = "" if folder == "." else f"{folder}/"
                for entry, value in entries.items():
                    setting, ran, _ = settings_guard.ran_of(entry)
                    path = ran or prefix + setting.split("\0")[0]
                    if path in carried or any(path.startswith(f"{c}/") for c in carried):
                        theirs["folders"].setdefault(folder, {})[entry] = value
            settings_guard.save(self.store, owner, theirs)

    async def settings_go_on(self, row: dict):
        """What waited for a worker while its folder's settings were changed: the task they
        stopped it at goes on (as its Go on does), news held for it goes to it, then what it
        was sent and never received. One taken up from a terminal as they changed (Workers.adopt)
        opens now, its conversation so far imported, as it would have then."""
        if not self.states.is_live(row["id"]) and not self.store.one(
            "SELECT id FROM messages WHERE session_id=? AND kind!='notice' LIMIT 1", (row["id"],)
        ):
            try:
                await self.session_for(row["id"])
            except Exception:
                logger.warning("Could not open %s.", row["id"], exc_info=True)
        for task in self.tasks.for_session(row["id"]):
            if (
                task["status"] == "waiting"
                and self.store.get(f"stopped:{task['id']}") == SETTINGS_STOP
            ):
                await self.task_actions.follow_up(task["id"], "Go on where you stopped.")
        await self.workers.closed_elsewhere(self.store.session(row["id"]))
        await self.delivery.resume(row["id"])

    def trust(self, row: dict) -> dict:
        """A worker in a worktree of a project the owner trusts in Copilot gets the trust its
        project has: Copilot then loads the repository's own MCP servers and hooks there too.
        Copilot keeps it among the owner's trusted folders (a trust for the session alone is
        lost at every resume), so only a worktree with such configuration gets it."""
        cwd = row["cwd"]
        if row["role"] != WORKER or not cwd or copilot_trusts(cwd):
            return {}
        for checkout in self.workspaces.checkouts(row):
            if (
                checkout.get("worktree")
                and checkout["path"] == cwd
                and copilot_trusts(checkout["project"] or checkout.get("repo") or "")
                and needs_trust(Path(checkout["worktree"]), Path(cwd))
            ):
                return {"trust_working_directory": "user"}
        return {}

    async def without_unsupported_effort(self, row: dict, options: dict, open_session):
        """Open with the chosen thinking effort; without it if the model takes none (Copilot's
        default can be Auto, which picks a model each turn)."""
        try:
            return await open_session(options)
        except Exception as exc:
            if (
                "reasoning_effort" not in options
                or not EFFORT_REFUSED.search(str(exc))
                or not self.on_copilot(row["id"])  # Copilot's default model is not its own.
            ):
                raise
        options = {key: value for key, value in options.items() if key != "reasoning_effort"}
        session = await open_session(options)
        if not self.store.get("effort_unsupported_noted"):
            self.store.set("effort_unsupported_noted", "1")
            self.add_message(
                row["id"],
                "notice",
                "Copilot's default model takes no thinking effort (Auto picks a model each turn), "
                "so this session uses the model's own. Choose a model in Settings to set one.",
            )
        return session

    async def refuse_if_open_elsewhere(self, row: dict, tell: bool = True):
        """Two programs writing to one Copilot session would interleave its history: a session
        open in a terminal is left alone until it is closed there. `tell`: once it is closed,
        the Router hears it can be reached again (what was refused came from it). Only Copilot's
        sessions: another agent's is never open in a Copilot terminal."""
        if self.on_copilot(row["id"]):
            self.elsewhere = await self.open_elsewhere()
        else:
            await self.agent_elsewhere(self.agent(row["id"]))
        if pid := self.is_elsewhere(row):
            if tell:
                self.store.set(f"refusedopen:{row['id']}", now())
            name = (
                agents.display(self.agent(row["id"]))
                if not self.on_copilot(row["id"])
                else ("Copilot")
            )
            raise OpenElsewhereError(
                f"“{row['title']}” is open in another {name} window (process {pid}). Exit it "
                "there (/exit), then try again.",
                pid,
            )

    async def session_for(self, session_id: str):
        if session := self.states.handle(session_id):
            return session
        if (agent := self.agent(session_id)) != COPILOT_AGENT:
            await self.agent_ready(agent, self.store.find_session(session_id))  # On first use.
        elif not self.backend:
            await self.until_connected()
        async with self.states[session_id].lifecycle:
            if session := self.states.handle(session_id):
                return session
            row = self.store.session(session_id)
            created = self.made(session_id)
            if row["role"] == WORKER and not created:
                # Cancelled, stopped or failed before it ever started: it starts now.
                return await self.open_live(row, create=True)
            try:
                return await self.open_live(row)
            except Exception:
                # A Router/Slack/Watchdog session whose creation failed earlier: create it now.
                if created:
                    raise
                logger.warning("Creating %s session %s again.", row["role"], session_id)
                return await self.open_live(row, create=True)

    async def rotate_router(self) -> bool:
        """Give the Router a fresh Copilot session when its context has grown long.

        The Chief of Staff session (its id, timeline and usage) stays; a new Copilot session takes
        over and gets a handover built from Chief of Staff's own records with its next message.
        """
        router_id = self.router_id()
        # Holding both locks, senders wait and then reach the new session: nothing goes to a
        # session that is gone or does not exist yet.
        async with (
            self.rotate_lock,
            self.states[router_id].lifecycle,
            self.states[router_id].sending,
        ):
            row = self.store.session(router_id)
            context = int(self.store.get(f"context:{router_id}") or 0)
            if (
                row["status"] == "working"
                or row["closed_at"]
                or context <= self.router_context_limit(row)
                or time.time() < float(self.store.get(f"rotate_after:{router_id}") or 0)
            ):
                return False
            old, new = self.copilot_id(router_id), str(uuid4())
            self.store.set(f"alias:{old}", router_id)
            self.store.set(f"alias:{new}", router_id)  # Its events may arrive during creation.
            await self.drop_handle(router_id)
            try:
                await self.open_live(row, create=True, copilot=new)
            except Exception:
                # The old session resumes with the next message; this is retried after its turn.
                self.store.execute("DELETE FROM kv WHERE key=?", (f"alias:{new}",))
                logger.warning("Could not rotate the Router.", exc_info=True)
                return False
            self.store.set(f"copilot:{router_id}", new)
            # Until the fresh session has answered once, a failure goes back to the old one.
            self.store.set(f"rotated:{router_id}", old)
            self.store.set(f"preamble:{router_id}", self.handover())
            self.store.set(f"context:{router_id}", "0")
            self.add_message(
                router_id,
                "notice",
                f"The Router's context reached {context // 1000}k tokens: a fresh Router session "
                "took over with a summary.",
            )
        if self.delivery.has_unsent(router_id):
            self.delivery.schedule_flush(router_id, 0)  # Held during the rotation.
        return True

    async def undo_rotation(self, error: str) -> bool:
        """The fresh Router session failed before answering once: the earlier one goes on, and
        what it was given is sent again. False when no rotation is pending."""
        router_id = self.router_id()
        old = self.store.get(f"rotated:{router_id}")
        if not old:
            return False
        async with (
            self.rotate_lock,
            self.states[router_id].lifecycle,
            self.states[router_id].sending,
        ):
            self.store.delete(f"rotated:{router_id}")
            self.store.set(f"copilot:{router_id}", old)
            self.store.delete(f"preamble:{router_id}")
            # Not again for a while: the same failure would follow every turn.
            self.store.set(f"rotate_after:{router_id}", str(time.time() + 3600))
            await self.drop_handle(router_id)
        if (before := self.store.get(f"prevsent:{router_id}")) is not None:
            self.store.set(f"sent:{router_id}", before)
        self.add_message(
            router_id,
            "notice",
            f"The fresh Router session failed ({error.strip()[:160]}); the earlier one goes on, "
            "and your message is sent again.",
        )
        self.background(self.delivery.resume(router_id))
        return True

    def backend_echoes(self, session_id: str) -> bool:
        """Whether a message sent shows in the session's conversation when it is taken up."""
        backend, _ = self.connection(self.agent(session_id))
        capabilities = getattr(backend, "capabilities", None)
        return bool(getattr(capabilities, "echoes", False))

    def router_context_limit(self, row: dict) -> int:
        """When the Router hands over to a fresh session: most of its model's context window
        (the longer one when chosen), never below ROUTER_CONTEXT. A long conversation keeps
        more of what the owner said; Copilot summarizes within the session meanwhile."""
        if "CHIEFOFSTAFF_ROUTER_CONTEXT" in os.environ:
            return ROUTER_CONTEXT  # Set exactly (tests).
        info = self.model_info(self.effective_model(row)) or {}
        window = info.get("context") or 0
        if row.get("context") == "long_context" and info.get("long_context"):
            window = info["long_context"]
        return max(ROUTER_CONTEXT, int(window * 0.8))

    def handover(self, why: str = "whose context had grown long") -> str:
        """What a new Router session needs to know, from Chief of Staff's records."""

        def clip(text: str, size: int = 240) -> str:
            text = " ".join((text or "").split())
            return text if len(text) <= size else text[: size - 1] + "…"

        router = self.router_id()
        lines = [
            f"You take over as the owner's Router from an earlier Router session {why}. This "
            "is what it knew; list_sessions has the current details."
        ]
        if spoken := self.store.get("owner_language"):
            # Without the conversation that set it, a fresh session would answer in English.
            lines.append(
                f"The owner has been writing in {spoken}: write for them in {spoken}, also short "
                "remarks after a report, except that a message of theirs whose reply_in names "
                "another language is answered in that one (they switched)."
            )
        helpers = self.helper_ids()  # Chief of Staff's own: not the owner's work.
        sessions = [
            row
            for row in self.store.open_sessions()
            if row["role"] != ROUTER and row["id"] not in helpers
        ]
        lines.append("\nOpen sessions:" if sessions else "\nOpen sessions: none.")
        for row in sessions:
            lines.append(
                f"- {short(row['id'])} “{row['title']}” ({row['role']}, {row['status']})"
                + (f" task: {clip(row['task'], 160)}" if row["task"] else "")
                + (f" | last report: {clip(row['report'], 200)}" if row["report"] else "")
            )
        home = needs(self)
        if tasks := self.tasks.listed():
            lines.append(
                "\nTasks not done yet, as Home shows them (every finished result waits for the "
                "owner's Accept: “to review”; one on its draft's card is reviewed on that card; "
                "“accepted, monitoring …” is still followed up):"
            )
            lines += [
                f"- {t['id']} “{t['title']}” ({task_words(t, home)}, worker "
                f"{short(t['session_id'])})"
                + (
                    f" waits for {', '.join(d['id'] for d in deps)}"
                    if (deps := self.tasks.unmet(t))
                    else ""
                )
                for t in tasks
            ]
        if closed := [r for r in self.workers.recently_closed(20) if r["id"] not in helpers][:8]:
            lines.append("\nRecently closed workers (message_session reopens one):")
            lines += [
                f"- {short(r['id'])} “{r['title']}”: {clip(r['report'], 160)}" for r in closed
            ]
        # What was cancelled lately (by the owner, or with the worker they closed): a fresh
        # Router that does not know would start it again. And what failed: the owner retries it
        # (or the Router has it done over, replacing it), never both.
        ended = self.tasks.listed(include_ended_since=hours_ago(TASKS_SHOWN_HOURS))
        for status, heading in (
            ("cancelled", "Cancelled in the last days (do not start them again unless the owner "
             "asks):"),
            ("failed", "Failed in the last days (the owner can retry them; to do one over, "
             "message_session its worker with task='continue' and its task_id, or pass "
             "replaces=[its id]):"),
        ):  # fmt: skip
            if found := [task for task in ended if task["status"] == status]:
                lines.append(f"\n{heading}")
                lines += [
                    f"- {t['id']} “{t['title']}” (worker {short(t['session_id'])}): "
                    f"{clip(t['progress'], 160)}"
                    for t in found[-20:]
                ]
        if drafts := self.store.drafts():
            lines.append(
                "\nCards waiting for the owner (what each is, to whom, what it answers, for which "
                "task; you hear when one is sent, rejected or withdrawn):"
            )
            lines += [
                f"- {d['id']}: {self.drafts.line(d)}. Text: {clip(d['text'])}" for d in drafts
            ]
        waiting = self.toolset.needs_owner(home)
        lines.append(
            f"\nUnder the owner's Needs you now (Home's count): {waiting['count']}: "
            f"{waiting['summary']}."
        )
        if owner_questions := waiting["questions"]:
            lines.append("\nQuestions workers asked the owner, not answered yet:")
            lines += [
                f"- “{q['worker']}”"
                + (f" (task {q['task']})" if q.get("task") else "")
                + f": {clip(q['question'])}"
                for q in owner_questions
            ]
        if questions := self.store.all(
            "SELECT * FROM questions WHERE target=? AND status='open'", (router,)
        ):
            lines.append("\nQuestions from workers you have not answered (answer_session):")
            lines += [f"- {q['id']} from {short(q['asker'])}: {clip(q['text'])}" for q in questions]
        if decisions := self.decisions.active():
            lines.append("\nThe owner's standing decisions:")
            lines += [f"- {d['id']}: {clip(d['text'], 400)}" for d in decisions]
        # The conversation, newest first until the budget: everything the owner said (their
        # requests, facts to remember, preferences) matters most, so theirs are kept long.
        talk, size = [], 0
        for message in self.store.all(
            """SELECT * FROM messages WHERE session_id=?
            AND kind IN ('owner','update','assistant') ORDER BY id DESC LIMIT 2000""",
            (router,),
        ):
            meta = json.loads(message["meta"] or "{}")
            if meta.get("internal"):
                continue
            text = clip(message["text"], 3000 if message["kind"] == "owner" else 600)
            if size + len(text) > HANDOVER_CHARS:
                break
            talk.append((message, meta, text))
            size += len(text)
        if talk:
            lines.append(
                "\nThe conversation with the owner, oldest first (what they told you still holds):"
            )
            who = {"owner": "owner", "assistant": "you", "update": "you notified the owner"}
            for message, meta, text in reversed(talk):
                ref = meta.get("ref")
                lines.append(f"- {f'owner (ref {ref})' if ref else who[message['kind']]}: {text}")
        return wrap("system", "handover", "\n".join(lines))

    async def ensure_router(self):
        router_id = self.store.get("router_id")
        row = self.store.find_session(router_id) if router_id else None
        if row and not row["closed_at"]:
            # The Router is never replaced: its timeline, tasks and what the owner told it stay.
            async with self.states[row["id"]].lifecycle:
                if self.states.handle(row["id"]):
                    return
                backend, connection = self.connected(), self.connections
                try:
                    await self.open_live(row)
                    return
                except OpenElsewhereError as exc:
                    raise OpenElsewhereError(
                        f"The Router is open in another Copilot window (process {exc.pid}): "
                        "exit it there (/exit). Chief of Staff connects again once it is closed.",
                        exc.pid,
                    ) from None
                except Exception as exc:
                    if self.connections != connection or not backend.alive():
                        raise  # Copilot itself stopped: its session may well be fine.
                    logger.warning("The Router's Copilot session could not be resumed; a fresh "
                                   "one takes over.", exc_info=True)  # fmt: skip
                    await self.renew_router(row, describe(exc))
                    return
        session_id = str(uuid4())
        row = self.store.create_session(
            session_id, ROUTER, "Router", "idle", cwd=str(self.role_dir(ROUTER))
        )
        self.store.set("router_id", session_id)
        await self.open_live(row, create=True)

    async def renew_router(self, row: dict, error: str):
        """The Router's Copilot session cannot be resumed (Copilot no longer has it, say): a
        fresh one takes over, with a handover, as rotate_router does. The caller holds the
        Router's lifecycle lock."""
        router_id = row["id"]
        old, new = self.copilot_id(router_id), str(uuid4())
        self.store.set(f"alias:{old}", router_id)
        self.store.set(f"alias:{new}", router_id)
        try:
            await self.open_live(row, create=True, copilot=new)
        except Exception:
            self.store.delete(f"alias:{new}")
            raise
        self.store.set(f"copilot:{router_id}", new)
        self.store.delete(f"rotated:{router_id}")  # Nothing to go back to.
        self.store.set(f"preamble:{router_id}", self.handover("that could not be resumed"))
        self.store.set(f"context:{router_id}", "0")
        self.add_message(
            router_id,
            "notice",
            f"The Router's Copilot session could not be resumed ({error.strip()[:160]}): a fresh "
            "one took over with a summary of what it knew. Your conversation stays.",
        )

    async def ensure_slack_session(self):
        if not self.slack.configured():
            return
        rows = self.store.open_sessions(SLACK)
        if rows:
            try:
                await self.session_for(rows[0]["id"])
                return
            except OpenElsewhereError:
                raise  # Open in a terminal: it stays, and waits until it is closed there.
            except Exception:
                if not self.connected().alive():
                    raise  # Copilot itself stopped: its session may well be fine.
                # Copilot may no longer have it (its session state was cleaned up).
                logger.warning("The Slack session could not be resumed; starting a new one.",
                               exc_info=True)  # fmt: skip
                self.store.unfollow_session(rows[0]["id"])
                self.update_session(rows[0]["id"], closed_at=now())
        row = self.store.create_session(
            str(uuid4()), SLACK, "Slack", "idle", cwd=str(self.role_dir(SLACK))
        )
        await self.open_live(row, create=True)

    async def refresh_slack(self):
        """Reattach Slack-capable sessions so they pick up the current token and settings."""
        for row in self.store.open_sessions():
            if not (row["role"] == SLACK or row["slack"]):
                continue
            if row["status"] == "working" or self.states[row["id"]].busy or self.held(row["id"]):
                # Reattached once it is free (its turn over, or what it left running ended):
                # a held one's command would end with none to wake.
                self.states[row["id"]].stale = True
            else:
                await self.detach(row["id"])
        if self.ready():
            await self.ensure_slack_session()
        self.publish_state()

    async def reconcile_sends(self, agent: str = COPILOT_AGENT, only: list[str] | None = None):
        """Sends to the agent's sessions (`only` these) that a stop interrupted: check whether
        they arrived before anything is resent."""
        for key in self.store.keys("sending:"):
            session_id = key.removeprefix("sending:")
            if self.agent(session_id) != agent or (only is not None and session_id not in only):
                continue
            row = self.store.find_session(session_id)
            if not row or row["closed_at"]:
                self.store.delete(key)
                continue
            arrived = await self.delivery.reconcile(session_id)
            outcome = {True: "it arrived", False: "resending", None: "unknown, resending"}
            logger.info("Interrupted send to %s: %s.", session_id, outcome[arrived])

    async def resume_working(self, agent: str = COPILOT_AGENT, only: list[str] | None = None):
        """After a restart, the agent's sessions that were mid-turn continue, with what they had
        not handled. What restarted: this app, or (connected before) only the agent; `only`
        these sessions: their own process of the agent."""
        backend, connections = self.connection(agent)
        named = backend.capabilities.name if backend and agent != COPILOT_AGENT else "Copilot"
        what = f"{named} was restarted" if connections or only else "Chief of Staff restarted"
        rows = self.store.open_sessions()
        mine = set(self.on_agent(agent, [row["id"] for row in rows]))
        if only is not None:
            mine &= set(only)
        for row in rows:
            if row["id"] not in mine:
                continue
            if row["role"] == SLACK and not self.slack.configured():
                continue
            if row["status"] != "working":
                if self.delivery.has_unsent(row["id"]):
                    # Events held, or a send interrupted, when Chief of Staff stopped.
                    self.background(self.delivery.resume(row["id"]))
                continue
            mark = int(self.store.get(f"idle:{row['id']}", "0"))
            sent = int(self.store.get(f"sent:{row['id']}") or 0)
            since = [
                message
                for message in self.store.messages(row["id"], after=mark)
                if "prompt" in message["meta"]
            ]
            given = [message for message in since if message["id"] <= sent]
            # Sent, but not in its conversation (Copilot stopped before it wrote them down):
            # they go out again in full, as messages it never received.
            received = await self.delivery.received(row["id"], given)
            if resent := given[len(received) :]:
                self.store.set(f"sent:{row['id']}", str(resent[0]["id"] - 1))
            # Messages it never received go out as they are, with the note; only the rest
            # are quoted.
            unhandled = [message["meta"]["prompt"] for message in received]
            # Quoted, their tasks are taken up (their instructions show only as quotes).
            if row["role"] == WORKER and (
                named := [task for m in given for task in tasks_named(m["meta"]["prompt"])]
            ):
                self.took_up(row["id"], named, switch=False)
            if any(message["kind"] == "owner" for message in since):
                self.states[row["id"]].owner_turn = True
            note = f"{what} while you were working. Continue where you left off."
            if unhandled:
                # The newest whole (a task's instructions, say); older ones cut.
                quoted = unhandled[-10:]
                note += (
                    " Messages you received since you were last idle (some may already be "
                    "handled), quoted:\n\n"
                    + "\n\n".join([text[:1500] for text in quoted[:-1]] + quoted[-1:])
                )
            if resent:
                note = (
                    note + "\n\nThe message(s) before this note had not reached you yet: they "
                    "are new to you."
                    if unhandled
                    else f"{what} just after sending you the message(s) above, before they "
                    "reached you: they are new to you."
                )
            prompt = wrap("system", "restart", note)
            try:
                await self.delivery.deliver(
                    row["id"],
                    prompt,
                    kind="notice",
                    display=f"{what}; asked the session to continue.",
                )
            except Exception as exc:
                self.states[row["id"]].owner_turn = False
                self.fail(row, f"Could not resume after restart: {describe(exc)}")

    # Messages

    def add_message(
        self,
        session_id: str,
        kind: str,
        text: str,
        meta: dict | None = None,
        *,
        unread: bool = False,
    ) -> dict:
        if (
            kind in {"assistant", "notice"}
            and not (meta or {}).get("prompt")  # Something to deliver is never dropped.
            and (same := self.repeated(session_id, kind, text, meta or {}))
        ):
            return same  # The same answer again within a minute (a turn retried, say): once.
        message = self.store.add_message(session_id, kind, text, meta)
        # What the UI shows, without the prompt it was sent as (the same text again, or more).
        shown = {k: v for k, v in (message.get("meta") or {}).items() if k != "prompt"}
        self.bus.publish("message", {**message, "meta": shown})
        if unread:
            row = self.store.session(session_id)
            self.update_session(session_id, unread=row["unread"] + 1)
        return message

    def repeated(self, session_id: str, kind: str, text: str, meta: dict) -> dict | None:
        """The session's last message when it says the same (with the same meta), and came
        within REPEAT_SECONDS."""
        last = self.store.one(
            "SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1", (session_id,)
        )
        if not last or last["kind"] != kind or last["text"].strip() != text.strip():
            return None
        if json.loads(last["meta"] or "{}") != meta:
            return None
        try:
            age = time.time() - datetime.fromisoformat(last["created_at"]).timestamp()
        except ValueError:
            return None
        if age > REPEAT_SECONDS:
            return None
        last["meta"] = json.loads(last["meta"])
        return last

    def update_session(self, session_id: str, **values) -> dict:
        row = self.store.update_session(session_id, **values)
        self.publish_session(row)
        if row["role"] == WORKER and ("status" in values or "closed_at" in values):
            self.storage.workers_changed()  # Clear is offered while no worker uses caches.
        return row

    def task_changed(self, task: dict, was: str | None = None):
        """Every created or changed task: shown, and as it ends (done, failed, cancelled), a
        decision card it waited on no longer waits for it. Not at every change of a task that
        ended before (bug E100: finished work's own card closed at its next progress report).
        `was`: its status before, when this change set it."""
        self.task_actions.publish(task)
        if (
            task["status"] in ENDED
            and was is not None
            and (was not in ENDED or (task["status"] != was and task["status"] != "done"))
        ):
            self.decisions.drop_task(task)
        if task["status"] != "waiting" and self.waits_for_nothing(task["session_id"]):
            # Its question settled some other way (the draft it asked about sent, say): its
            # worker no longer waits for the owner (bug bash 9: its box kept "waits for you").
            self.update_session(task["session_id"], status="idle")

    def waits_for_nothing(self, session_id: str) -> bool:
        """A session shown as waiting for the owner that waits for nothing of theirs now: no
        task of it waits, nothing it asked is open (a question, a decision card)."""
        row = self.store.find_session(session_id)
        return bool(
            row
            and row["status"] == "waiting"
            and not self.states[session_id].asking
            and not any(task["status"] == "waiting" for task in self.tasks.for_session(session_id))
            and not self.store.one(
                "SELECT id FROM drafts WHERE session_id=? AND kind='decision' AND status='pending'",
                (session_id,),
            )
        )

    def fail(self, row: dict, message: str):
        self.states[row["id"]].busy = False
        self.add_message(row["id"], "error", message)
        self.delivery.defer_pending(row["id"])  # Not a turn by itself after a restart.
        if row["role"] != WORKER:
            self.update_session(row["id"], status="failed", activity="")
            return
        # As when Copilot fails a turn: the Router and the owner hear of it.
        self.worker_failed(row, message, told=None if self.special(row) else message)
        if not self.special(row):
            self.alert(
                f"“{self.display_title(row)}” failed: {message[:160]}",
                session_id=row["id"],
                needs_action=True,
                title="Failed",
            )

    def worker_failed(self, row: dict, message: str, told: str | None = "") -> dict | None:
        """A worker's turn, or its start, failed: the one path for every way one does (Copilot's
        error or refusal, a message it never started on, a start that failed, tools that fit
        no model). It is Failed; the task that turn was on fails, not those queued behind it
        (TaskActions.fail); its slot is free for a queued worker. Watch for you's scan failed
        as its own (Connector.failed: no task of its own, bug E102); for any other the Router
        hears `told` (the message when ""; None: the caller tells it). The task that failed."""
        self.update_session(row["id"], status="failed", activity="")
        task = self.task_actions.fail(row["id"], message)
        if self.special(row) == "connector":
            self.connector.failed(message)
        elif told is not None:
            self.background(self.tell_router(row, "failed", told or message, task))
        self.background(self.workers.pump())
        return task

    @property
    def attachments_dir(self) -> Path:
        return self.store.directory / "attachments"

    def save_attachments(self, attachments: Sequence[Attachment]) -> list[dict]:
        """The owner's images, kept as files: sessions get them with the message, and the
        Router can hand them on to a worker by path."""
        saved = []
        folder = self.attachments_dir
        folder.mkdir(parents=True, exist_ok=True)
        for item in attachments:
            try:
                data = base64.b64decode(item.data, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError(f"{item.name}: not a valid image.") from None
            if len(data) > MAX_ATTACHMENT:
                raise ValueError(f"{item.name} is over 5 MB.")
            if not data.startswith(IMAGE_MAGIC[item.type]) or (
                item.type == "image/webp" and data[8:12] != b"WEBP"
            ):
                raise ValueError(f"{item.name} is not a {item.type.split('/')[1].upper()} image.")
            name = uuid4().hex + IMAGE_EXTENSIONS[item.type]
            (folder / name).write_bytes(data)
            saved.append(
                {
                    "name": " ".join(item.name.split())[:120] or "image",
                    "type": item.type,
                    "path": str(folder / name),
                    "url": f"/api/attachments/{name}",
                }
            )
        return saved

    def attachments_in(self, text: str) -> list[dict]:
        """The owner's images a message names by path (the Router handing them on)."""
        folder = str(self.attachments_dir)
        found = []
        for name in dict.fromkeys(ATTACHMENT_NAME.findall(text or "")):
            path = Path(folder) / name
            if path.is_file():
                found.append({"name": name, "path": str(path), "url": f"/api/attachments/{name}"})
        return found

    async def owner_send(
        self,
        session_id: str,
        text: str,
        *,
        learn: bool = True,
        tell_router: bool = True,
        attachments: Sequence[Attachment] = (),
        clear_questions: bool = True,
        from_home: bool = False,
        task_id: str = "",
    ) -> dict:
        """`learn`: the owner typed it (not a message the app sends for them). A message to a
        worker is also noted for the Router (`tell_router`), which must know what the owner
        asked for. `from_home`: written on Home (@ a worker), where its answer is shown too.
        `task_id`: the task of the worker's they look at in its chat (see chat_task)."""
        row = self.store.session(session_id)
        if row["closed_at"] and row["role"] == WORKER:
            await self.workers.reopen(session_id, restore=True)
        # The task this is about: the one whose question it answers, the one the owner looks
        # at, the one it is on… (see chat_task).
        about = self.chat_task(session_id, task_id) if row["role"] == WORKER else None
        if row["role"] == WORKER and not attachments and text.strip():
            # Waiting on its question card: this is the answer (sent as a message, it would only
            # be read once the card times out).
            with suppress(ValueError):
                if card := self.asks.answer_in_chat(session_id, text.strip()):
                    meta = {"answered": card["id"], **({"home": True} if from_home else {})}
                    if about:
                        meta["task_id"] = about["id"]
                    message = self.add_message(session_id, "owner", text, meta)
                    if tell_router:
                        self.owner_told(row, text)
                    return message
        if row["role"] in {WORKER, SLACK} and clear_questions:
            # The owner answers what it asked on that task (others' questions stay).
            self.asks.answered(session_id, about["id"] if about else "")
            self.asks.answered(session_id, "")
        # Waiting on its decision card: what the owner writes it is their answer to it (unless
        # they ask something back: then the card stays, and this is chat).
        decided = (
            self.decisions.answered_in_chat(session_id, about, text.strip())
            if row["role"] == WORKER and clear_questions and text.strip()
            else []
        )
        asked_back = (
            self.decisions.asked_back(session_id, about, text.strip())
            if row["role"] == WORKER and clear_questions
            else []
        )
        # Answers come in the language of this message, whatever the conversation used before.
        spoken = self.heard(text, home=row["role"] == ROUTER) if learn else language(text)
        prompt = wrap("owner", "message", text, reply_in=spoken)
        meta: dict[str, Any] = {}
        switched = self.switched_language(session_id, spoken)
        notes = [switched]
        made = None
        # About none of its tasks in particular (several finished, the latest given by the
        # Router): a task of its own, named from it, so what Home shows it doing is this.
        apart = (
            row["role"] == WORKER
            and not about
            and bool(text.strip())
            and not decided
            and not asked_back
            and not self.special(row)
        )
        if row["role"] == WORKER and about and about["status"] != "cancelled":
            notes.append(self.apart_note(about))
        notes += [f"This answers your decision card: {c['target']['question']}" for c in decided]
        notes += [
            f"The owner asks about your decision card before deciding: answer them. The card "
            f"({c['target']['question']}) stays open for their decision."
            for c in asked_back
        ]
        if attachments:
            meta["attachments"] = self.save_attachments(attachments)
            paths = ", ".join(f"{a['path']} ({a['name']})" for a in meta["attachments"])
            notes.append(
                f"The owner attached {len(meta['attachments'])} image(s), which you see with "
                f"this message: {paths}."
                + (
                    " To hand them to a worker, put these paths in its instructions or message: "
                    "it gets the images too."
                    if row["role"] == ROUTER
                    else ""
                )
            )
        if row["role"] == ROUTER:
            # The Router hands this message on as {owner:<ref>} instead of copying it.
            meta["ref"] = "o" + uuid4().hex[:6]
            prompt = wrap("owner", "message", text, ref=meta["ref"], reply_in=spoken)
            notes += [self.projects_note(), self.toolset.related_hint(text)]
        was = self.store.value("SELECT task_id FROM sessions WHERE id=?", (session_id,))
        if apart:
            about = made = self.new_chat_task(session_id, text)
            notes.append(
                f"This message starts task {made['id']} \u201c{made['title']}\u201d: the owner "
                "wrote it in your chat, about none of your earlier tasks in particular. Do it "
                f"and report it with report(task_id='{made['id']}'); if it only asks a "
                "question, report your answer (status done)."
            )
        context = [wrap("system", "context", note) for note in notes if note]
        prompt = "\n\n".join([prompt, *context])
        task = about
        if made:
            meta |= {"task_id": made["id"]}  # Its instruction, as a task's first one is.
        elif task:
            # What its turn is about now; a chat with it, not a task card's follow-up.
            meta |= {"task_id": task["id"], "chat": True}
        if from_home:
            meta["home"] = True  # Asked on Home: its answer is shown there too.
        if task and task["status"] in {"review", "done", "failed", "cancelled"}:
            # A question about finished work changes nothing: what it reports is news on it.
            self.store.set(f"followup:{task['id']}", "chat")
            # Its checkouts now: what this task changes from here is a new result.
            await self.task_actions.record_chat_base(task)
        elif task and task["status"] in OPEN_WORK and not task["result"] and not made:
            # Its checkouts now: separate work this asks for starts here (see split_off).
            await self.task_actions.record_asked_base(task)
        try:
            message = await self.delivery.deliver(
                session_id,
                prompt,
                kind="owner",
                display=text,
                meta=meta,
                # Steered into a Router turn written in the other language, the answer stays in
                # it; its own turn (Router turns are short) starts in the owner's.
                steer=not (switched and row["role"] == ROUTER),
            )
        except Exception:
            # Not sent (a cancellation is not this: what it was given may yet go out).
            if made:
                self.drop_chat_task(made, was)
            raise
        if task and (made or self.store.get(f"followup:{task['id']}")):
            # Home shows it at work on that task (bug QA6-4: its row stayed under Monitoring,
            # and a row of its worker's beside it under Running said the same work).
            self.task_actions.publish(self.tasks.get(task["id"]))
        if row["role"] == WORKER and tell_router:
            self.owner_told(row, text, message)
        if about and about["status"] == "waiting" and clear_questions and not asked_back:
            # It has the owner's answer now: it goes on (not "waits for your answer" until its
            # next report).
            self.asks.go_on(about)
        if learn and len(text.strip()) >= INSTRUCTION_CHARS:
            # What the owner asks for again and again becomes a lesson (they review it first).
            self.learning.record(
                "instruction",
                context=f"to {row['title']}",
                owner=text,
                scope=self.learning.scope_of(session_id),
            )
        if note := self.busy_note(session_id):
            self.add_message(session_id, "notice", note, {"progress": True})
        return message

    @staticmethod
    def apart_note(task: dict) -> str:
        """For the owner's message to a worker: the task it is about, unless its worker finds
        it asks for separate work with a result of its own; that work it reports as a task of
        its own (the worker reads the message: no word list decides). A question is answered
        in its reply, not reported."""
        kept = (
            "keeps its result" if task["result"] else "goes on: report it by itself when it is done"
        )
        return (
            f"This message is about task {task['id']} \u201c{task['title']}\u201d unless it asks "
            "for separate work with a result of its own (another branch, PR, document, run or "
            "flow, a change, or what the owner asks to be a task of its own). A question (about "
            "this task, or a quick one about anything else) you answer in your reply, without a "
            "report. Separate work you do and report with report(new_task_title=<what it "
            f"delivers>, status=…): it becomes a task of its own, and task {task['id']} {kept}."
        )

    def heard(self, text: str, home: bool = False) -> str:
        """The language of what the owner wrote. Written to the Router (`home`), it is
        remembered when clear: what the Router tells them next and new tasks' reports are in it.
        Not what they write to one worker: that worker answers in it, nothing else changes."""
        spoken = language(text)
        if spoken and home:
            self.store.set("owner_language", spoken)
        return spoken

    def switched_language(self, session_id: str, spoken: str) -> str:
        """A note when the owner writes in another language than the session last answered in
        (or than their message it is still on): a conversation in one language pulls a model's
        answer back into it."""
        last = self.store.one(
            "SELECT text FROM messages WHERE session_id=? "
            "AND kind IN ('assistant', 'report', 'owner') ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        before = language(last["text"]) if last else ""
        if not spoken or not before or before == spoken:
            return ""
        return f"The owner wrote this in {spoken}: answer in {spoken}, not {before}."

    def busy_note(self, session_id: str) -> str:
        """For a message to a worker deep in a step: it steers in at once (Copilot moves a
        command it runs to the background, where it goes on), unless its model changed in this
        turn (then it waits for the turn's end)."""
        row = self.store.session(session_id)
        since, _ = self.states[session_id].step()
        if row["role"] != WORKER or not since or time.time() - since < STEP_NOTE_SECONDS:
            return ""
        line = self.progress_line(row)
        line = line[:1].upper() + line[1:]
        if self.states[session_id].retuned:
            return f"It reads this when its current turn ends. {line}"
        return f"It reads this now; the step it is in goes on in the background. {line}"

    async def owner_send_to(
        self, router_id: str, worker_id: str, text: str, attachments: Sequence[Attachment] = ()
    ) -> dict:
        """From the Router chat, straight to one worker; the Router hears of it later."""
        row = self.store.session(worker_id)
        if row["role"] != WORKER:
            raise ValueError("Address a worker.")
        message = await self.owner_send(row["id"], text, attachments=attachments, from_home=True)
        # Not when it answered the question card the worker was waiting on.
        note = "" if message["meta"].get("answered") else self.busy_note(row["id"])
        if note:  # Where the owner is: Chat or the inbox.
            self.add_message(router_id, "notice", note, {"session_id": row["id"]})
        return {**message, "note": note}

    async def send_now(self, session_id: str) -> dict:
        """What waits to be sent to a worker goes now, as the owner asks where it says it waits:
        its worker waits for a free slot (it takes one over the parallel-worker limit), or was
        stopped and holds it for its next message. Not while memory or the disk is short, nor
        before the task it is given waits for another: that is said, and it waits on."""
        row = self.store.session(session_id)
        if row["role"] != WORKER or row["closed_at"]:
            raise ValueError("Only an open worker's messages wait to be sent.")
        if session_id in self.settings_held:
            # Its folder changed (settings_stop): it goes on once the owner says to use them.
            raise ValueError(f"It waits: {SETTINGS_WAIT}.")
        if row["status"] == "queued":
            if row["activity"]:
                raise ValueError(f"It waits: {row['activity']}.")
            self.states[session_id].now = True
            await self.workers.pump()
        elif row["status"] not in {"starting", "working"} and (
            self.delivery.undefer(session_id) or self.delivery.has_unsent(session_id)
        ):
            # Over the limit for what is due now, not for whatever comes next (review L1).
            self.states[session_id].now = True
            try:
                await self.delivery.resume(session_id, now=True)
            finally:
                if self.store.session(session_id)["status"] != "queued":
                    self.states[session_id].now = False
        return self.view(self.store.session(session_id))

    def owner_told(self, row: dict, text: str, sent: dict | None = None):
        """What the owner told a worker directly (in its timeline, @ in Chat, on a task card):
        the Router hears of it with its next message, so it never takes the result for drift.
        `sent`: the worker's own message, as it was given it: Home's copy stands where that one
        does (Delivery.delivery_for)."""
        given = sent and "prompt" in (sent.get("meta") or {})
        with suppress(RuntimeError):
            copy = self.add_message(
                self.router_id(),
                "owner",
                text,
                {
                    "to": row["id"],
                    "to_title": self.display_title(row),
                    **({"sent_as": sent["id"]} if sent and given else {}),
                    # Not a turn of its own: it goes with the Router's next message.
                    "prompt": wrap(
                        "system",
                        "to-worker",
                        f"The owner told this worker directly: {text}",
                        worker=short(row["id"]),
                        title=row["title"],
                    ),
                    "deferred": True,
                },
            )
            if given:
                # Where the worker has it, at once (it was sent before this copy was made).
                self.delivery.publish_delivery(copy["session_id"], [copy])

    def usage(self, since: float, until: float) -> dict:
        """What was spent in a range; each worker by the name it has everywhere (its work's,
        not its first prompt's)."""
        found = self.store.usage(since, until)
        for item in found["by_session"]:
            if item.get("role") == WORKER and (row := self.store.find_session(item["session_id"])):
                item["title"] = self.display_title(row)
        if self.agents and (parts := self.agent_usage_parts(since, until)):
            found["agents"] = parts  # Only where another agent has rows in the range.
        return found

    def agent_usage_parts(self, since: float, until: float) -> list[dict]:
        """The Usage view's part for each other agent with rows in a range: its models' tokens
        (a range as a range) and each interval whose spend is unknown."""
        parts: dict[str, dict] = {}
        for row in self.store.agent_usage_in(since, until):
            part = parts.setdefault(
                row["agent"],
                {
                    "agent": row["agent"],
                    "name": agents.display(row["agent"]),
                    "models": [],
                    "unknown": [],
                },
            )
            if row.get("unknown"):
                found = self.store.find_session(row["session_id"])
                title = self.display_title(found) if found else row["title"] or ""
                part["unknown"].append(
                    {"session_id": row["session_id"], "title": title, "at": row["at"]}
                )
            else:
                part["models"].append(
                    {key: row[key] for key in ("model", "updates", "tokens", "least")}
                )
        return list(parts.values())

    def told_prompt(self, message: dict) -> str:
        """What the owner told a worker directly, as the Router hears of it: with what the
        worker answered since, so the Router does not take the question as still open."""
        prompt = message["meta"]["prompt"]
        worker = message["meta"].get("to")
        if not worker or 'type="to-worker"' not in prompt:
            return prompt
        # Its answer to this message: what it said before the owner's next message to it.
        later = self.store.value(
            "SELECT min(id) FROM messages WHERE session_id=? AND kind='owner' AND id>?",
            (worker, message["id"]),
        )
        answered = self.store.one(
            "SELECT text FROM messages WHERE session_id=? AND id>? AND id<? AND kind IN "
            "('report', 'assistant') AND json_extract(meta, '$.narration') IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (worker, message["id"], later or 2**63 - 1),
        )
        if not answered:
            return prompt
        said = " ".join(answered["text"].split())
        said = said if len(said) <= 400 else said[:399] + "\u2026"
        row = self.store.find_session(worker) or {"id": worker, "title": ""}
        return wrap(
            "system",
            "to-worker",
            f"The owner told this worker directly: {message['text']}\nIt answered already "
            f"(the owner read it in its chat): {said}",
            worker=short(row["id"]),
            title=row["title"],
        )

    def projects_note(self) -> str:
        """Tell the Router when the configured projects changed since it last heard (also when
        a project's folder went missing, or came back)."""
        # os.path.isdir, not Path.is_dir: a folder it cannot look at is missing, not an error.
        projects = [
            project if os.path.isdir(project) else f"{project} {MISSING_PROJECT}"  # noqa: PTH112
            for project in self.workspaces.projects()
        ]
        heard = self.store.get("router_projects")
        if heard == json.dumps(projects):
            return ""
        self.store.set("router_projects", json.dumps(projects))
        if heard is None:
            return ""  # The Router's first look at projects is through list_sessions.
        listed = "\n".join(f"- {project}" for project in projects[:40]) or "- (none)"
        return f"The configured projects are now:\n{listed}"

    async def tell_router(
        self, row: dict, status: str, summary: str, task: dict | None = None, note: str = ""
    ):
        """`note`: what the Router should know of it (it crossed the Router's message)."""
        title = task["title"] if task else row["title"]
        prompt = wrap(
            sender(row),
            "report",
            summary,
            status=status,
            task=task["id"] if task else None,
            title=title,
            # What the Router tells the owner about it is in the owner's language.
            reply_in=self.store.get("owner_language"),
        )
        if note:
            prompt += "\n\n" + wrap("system", "context", note)
        await self.delivery.deliver(
            self.router_id(),
            prompt,
            batch=True,
            kind="inbound",
            display=summary,
            meta={
                "from": row["id"],
                "title": title,
                "status": status,
                "task_id": task["id"] if task else None,
            },
        )

    async def on_trigger(
        self,
        source: str,
        keys: list[str],
        text: str,
        display: str | None = None,
        kind: str = "event",
    ) -> bool:
        """A new event for the Router; kind="delegated" when the owner handed it over."""
        self.require_ready()
        fresh = [key for key in keys if self.store.add_trigger(source, key, text[:4000])]
        if not fresh:
            return False
        meta = {"source": source, **({"delegated": True} if kind == "delegated" else {})}
        if len(fresh) > 1:
            meta["count"] = len(fresh)  # Its chat line says how many came (not only the first).
        try:
            await self.delivery.deliver(
                self.router_id(),
                # What the Router tells the owner about it is in the owner's language.
                wrap(
                    source, kind, text, trust=UNTRUSTED, reply_in=self.store.get("owner_language")
                ),
                batch=True,
                kind="inbound",
                display=clip_words(display or text, 4000),
                meta=meta,
            )
        except Exception:
            for key in fresh:
                self.store.execute("DELETE FROM triggers WHERE key=?", (key,))
            raise
        return True

    async def on_sent(self, item: SentItem, client: str) -> dict:
        """An item sent with Send to Chief (sendsocket.py): one batched message for the Router,
        delegated, taken once. Its receipt says when it was taken, and whether it had been
        already (`duplicate`): under this item id (a retry), or with the same note and capture
        under another within ten minutes (the command run twice: `earlier` names that item).
        Refused while Copilot is not connected, and over the rates, with nothing kept."""
        key = f"send:{item.item}"
        if found := self.store.sent(key):
            return self.receipt(item.item, {"taken": False, **found})
        self.require_ready()
        for seconds, most, said in SENT_RATES:
            since = datetime.fromtimestamp(time.time() - seconds, UTC).isoformat()
            sent = self.store.value(
                "SELECT count(*) FROM triggers WHERE source='capture' AND created_at>=?", (since,)
            )
            if sent >= most:
                raise LimitedError(f"Chief of Staff takes at most {most} items {said}: not taken.")
        content = json.dumps([item.note, item.url, item.title, item.text])
        digest = hashlib.sha256(content.encode()).hexdigest()
        window = datetime.fromtimestamp(time.time() - SENT_AGAIN_SECONDS, UTC).isoformat()
        taken = self.store.take_sent(key, digest, window)
        if not taken["taken"]:
            return self.receipt(item.item, taken)
        # In the language of the note when it is clear; the owner's remembered one is left as it
        # is (a note sent from outside is not what they wrote here).
        reply_in = language(item.note) or self.store.get("owner_language", "")
        meta = {
            "source": "capture",
            "delegated": True,
            "client": client,
            "item": item.item,
            "line": sent_headline(item),
            "sent": sent_parts(item),
        }
        try:
            await self.delivery.deliver(
                self.router_id(),
                sent_prompt(item, client, reply_in),
                batch=True,
                kind="inbound",
                display=sent_shown(item),
                meta=meta,
            )
        except Exception:
            self.store.execute("DELETE FROM triggers WHERE key=?", (key,))
            raise
        return self.receipt(item.item, taken)

    @staticmethod
    def receipt(item: str, taken: dict) -> dict:
        earlier = taken["key"].removeprefix("send:")
        return {
            "item": item,
            "taken_at": taken["created_at"],
            "duplicate": not taken["taken"],
            **({"earlier": earlier} if earlier != item else {}),
        }

    # Thread subscriptions

    @staticmethod
    def check_follow(row: dict, keys: list[str]):
        """Slack threads come only to sessions made to read Slack (restricted network, no
        record_setup, no .env files): never to a worker without them."""
        slack = [key for key in keys if key.startswith("slack:")]
        if slack and row["role"] != SLACK and not row.get("slack"):
            raise ValueError(
                f"{', '.join(slack)}: only a session with Slack access follows Slack "
                "threads (spawn_worker with slack=true, or the Slack session)."
            )

    def follow(
        self,
        session_id: str,
        keys: list[str],
        *,
        given: bool = True,
        task_id: str | None = None,
        asked: Sequence[str] = (),
    ) -> list[str]:
        """`given`: the Router or the owner gave the session these threads (not the session
        itself), so it may also draft replies there. `task_id`: the task it follows them for
        (default: the one its turn is on, else its current one); a thread waited on is that
        task's, not every task of the worker. `asked`: those of them it was asked to follow
        (follow_thread, a `follow` the Router passed), not only named: a PR closed already
        stays followed a while, so its reopening is news (GitHubSource.poll_pull). One the Router
        or the owner stopped it following stays stopped until they give it back: the session
        does not follow it again by itself (a later report naming the PR); nor for a task it
        was stopped following it for (see unfollow_for)."""
        followed = [threads.normalize(key) for key in keys]
        wanted = {threads.normalize(key) for key in asked}
        row = self.store.session(session_id)
        if task_id is None and row["role"] == WORKER:
            current = self.tasks.current(session_id)
            task_id = self.turn_task(session_id) or (current["id"] if current else "")
        if given:
            for key in followed:
                self.store.delete(f"stopped:{session_id}:{key}")
                if task_id:
                    self.store.delete(f"stoppedfor:{task_id}:{key}")
        else:
            followed = [
                key
                for key in followed
                if not self.store.get(f"stopped:{session_id}:{key}")
                and not (task_id and self.store.get(f"stoppedfor:{task_id}:{key}"))
            ]
        self.check_follow(row, followed)
        for key in followed:
            if key.startswith("github:") and not self.store.followers(key):
                # Followed afresh: nothing of an earlier follow (its failures, its backoff)
                # carries over. One another session follows keeps its health as it stands.
                self.github.forget(key)
            self.store.follow(key, session_id)
            if given:
                self.store.set(f"given:{session_id}:{key}", "1")
            if key in wanted and key.startswith("github:"):
                self.store.set(f"ghkeep:{key}", str(time.time()))
            if task_id:
                bound = self.store.follow_tasks(session_id, key)
                if task_id not in bound:
                    self.store.set(f"followtask:{session_id}:{key}", json.dumps([*bound, task_id]))
            if key.startswith("slack:"):
                for prefix in ("slackfail", "slackskip"):
                    self.store.set(f"{prefix}:{key}", "0")
        self.store.record(session_id, "links", followed)
        if followed:
            self.publish_session(self.store.session(session_id))
        return followed

    def followed(self, prefix: str) -> list[dict]:
        return self.store.followed(prefix)

    def missing(self, thread: str, why: str):
        """A followed thread that does not exist: its followers stop following it, and say so
        (once; nothing of their tasks changes). Nothing reads it again."""
        if thread.startswith("github:"):
            self.github.forget(thread)
        for session_id in self.store.followers(thread):
            self.store.unfollow(thread, session_id)
            row = self.store.find_session(session_id)
            if not row:
                continue
            text = f"Stopped following {thread}: {why}."
            # Its worker hears it with its next message (bug E111: it took it for followed).
            prompt = wrap("system", "unfollowed", text)
            self.add_message(
                session_id, "notice", text, {"thread": thread, "prompt": prompt, "deferred": True}
            )
            with suppress(RuntimeError):
                self.add_message(
                    self.router_id(),
                    "notice",
                    f"“{self.display_title(row)}” stopped following {thread}: {why}.",
                    {"session_id": session_id},
                )
            self.publish_session(row)
            for task in self.tasks.for_session(session_id):
                if task["status"] not in {"done", "cancelled"} or self.store.get(
                    f"news:{task['id']}"
                ):
                    self.task_actions.publish(task)

    def unfollow_for(
        self, session_id: str, thread: str, task_id: str = "", by: str = "The owner"
    ) -> bool:
        """Stop a session following a thread for one task (`task_id`), or for all of them.
        It goes on following it for its other tasks that follow it; only when none is left
        does it stop following it. True when it no longer follows it at all. A task still at
        work that now waits on nothing is told so by its worker (`by` stopped it)."""
        thread = threads.normalize(thread)
        before = self.store.follows(session_id)
        affected = [
            task
            for task in self.tasks.for_session(session_id)
            if (task["id"] == task_id if task_id else True)
            and thread in self.task_actions.followed_for(task, before)
        ]
        others: list[str] = []
        if task_id:
            bound = self.store.follow_tasks(session_id, thread)
            listed = bound or [
                task["id"]
                for task in self.tasks.for_session(session_id)
                if thread in self.task_actions.followed_for(task, [thread])
            ]
            others = [
                found["id"]
                for other in listed
                if other != task_id
                and (found := self.tasks.find(other))
                and found["status"] not in {"cancelled", "failed"}
            ]
        if others:
            self.store.set(f"followtask:{session_id}:{thread}", json.dumps(others))
            self.store.set(f"stoppedfor:{task_id}:{thread}", "1")
        else:
            self.store.unfollow(thread, session_id)
            self.store.set(f"stopped:{session_id}:{thread}", "1")
            if thread.startswith("github:") and not self.store.followers(thread):
                self.github.forget(thread)  # Nobody else follows it: nothing reads it now.
        self.publish_session(self.store.session(session_id))
        for task in self.tasks.for_session(session_id):
            self.task_actions.publish(task)
        follows = self.store.follows(session_id)
        for task in affected:
            if (
                task["status"] == "running"
                and not self.task_actions.followed_for(task, follows)
                and not self.task_actions.check_back(task)
            ):
                # It waited on that thread alone: not left hanging, waiting for nothing.
                text = (
                    f"{by} stopped you following {threads.label(thread)} for task {task['id']} "
                    f"\u201c{task['title']}\u201d: nothing there reaches you now. If the task only "
                    "waited on it, report it now (done, with where it stands; or blocked); else "
                    "go on with it."
                )
                self.background(
                    self.delivery.deliver(
                        session_id,
                        wrap("system", "notice", text, task=task["id"]),
                        kind="notice",
                        display=f"{by} stopped following {threads.label(thread)}.",
                        meta={"task_id": task["id"]},
                    )
                )
        return not others

    def read_health_changed(self, thread: str):
        """A followed thread could not be read for a while, or can be again: the cards of the
        tasks that follow it say so now (not at their next change)."""
        for session_id in self.store.followers(thread):
            if not (row := self.store.find_session(session_id)):
                continue
            self.publish_session(row)
            for task in self.tasks.for_session(session_id):
                self.task_actions.publish(task)

    def follow_why(self, thread: str) -> dict:
        """Why a followed PR could not be read last: {kind (permission, not_found, rate_limited
        or transient), error (in words, with when it is tried again), step, at, retry_at (ISO,
        or None), detail (GitHub's words)} (see GitHubSource.read_failed); {} when it was read
        since."""
        return self.github.why(thread)

    async def retry_follow(self, session_id: str, key: str) -> dict:
        """The owner's "try again now" on a PR a session follows that could not be read: its
        backoff cleared, it is read at once. Its failures in a row after it (0: it was read),
        why (as follow_why), when it was last read and when it is read next (ISO)."""
        key = threads.normalize(key)
        if key not in self.store.follows(session_id):
            raise ValueError(f"This session does not follow {threads.label(key)}.")
        if not threads.GITHUB_PR.fullmatch(key):
            raise ValueError("Only a followed GitHub PR can be read again now.")
        if self.store.settings().paused:
            raise ValueError("Chief of Staff is paused: resume it to read followed PRs.")
        await self.github.retry(key)
        failures = int(self.store.get(f"ghfail:{key}") or 0)
        if failures:
            self.read_health_changed(key)  # When it is tried next changed on its card.
        return {
            "key": key,
            "failures": failures,
            "why": self.follow_why(key),
            "read_at": self.store.get(f"ghread:{key}"),
            "next_at": self.store.get(f"ghnext:{key}"),
        }

    def follow_health(self, session_id: str) -> dict[str, int]:
        """Its followed threads that could not be read lately: failures in a row."""
        found = {}
        for key in self.store.follows(session_id):
            prefix = "ghfail" if key.startswith("github:") else "slackfail"
            if failures := int(self.store.get(f"{prefix}:{key}") or 0):
                found[key] = failures
        return found

    def unfollow(self, thread: str, finished: bool = False, merged: bool = False):
        """Stop following; `finished` means the PR was merged or closed while followed."""
        followers = self.store.followers(thread)
        self.store.unfollow(thread)
        if thread.startswith("github:"):
            self.github.forget(thread)
        for session_id in followers:
            row = self.store.session(session_id)
            self.publish_session(row)
            if merged and row["role"] == WORKER:
                self.task_actions.pr_merged(session_id, thread)
            if (
                finished
                and row["role"] == WORKER
                and not self.store.follows(session_id)
                and self.workers.finished(session_id)
            ):
                # Its PR is merged or closed: the task is over once this news is handled (and
                # closed only if nothing else of it is open then).
                if self.states[session_id].busy or row["status"] == "working":
                    self.states[session_id].close_after_turn = True
                else:
                    self.background(self.workers.close(session_id))

    async def on_thread(
        self,
        source: str,
        thread: str,
        keys: list[str],
        text: str,
        display: str | None = None,
        replies: dict[str, tuple[str, str]] | None = None,
    ) -> bool:
        """New activity in a followed thread goes straight to its followers, not the Router.
        `replies`: of a PR's items, those that answer a comment Chief of Staff posted there
        (key -> the task that posted it, "" when not known, and the item's line)."""
        self.require_ready()
        followers = self.store.followers(thread)
        if thread.startswith("slack:"):
            # A second guard: Slack content reaches only sessions that may read Slack.
            followers = [
                session_id
                for session_id in followers
                if (row := self.store.find_session(session_id))
                and (row["role"] == SLACK or row["slack"])
            ]
        if not followers:
            return False  # Leaves the keys unseen, so the event can still reach the Router.
        fresh = [key for key in keys if self.store.add_trigger(source, key, text[:4000])]
        if not fresh:
            return False
        self.store.touch(thread)
        if thread.startswith("github:"):
            # CI or a review came in: the PR's chips change now, not at the next round.
            self.background(self.task_actions.refresh_open_prs(thread.removeprefix("github:")))
        owners = self.thread_owners(followers, thread)
        recipients = []
        for session_id in owners:
            task = self.thread_task(session_id, thread)
            row = self.store.find_session(session_id)
            if not task and threads.GITHUB_PR.fullmatch(thread) and row and row["role"] == WORKER:
                # Which of its tasks this PR is about is not known (none made it, none follows
                # it): the Router decides, as for news nobody follows.
                continue
            recipients.append((session_id, task, text, display))
        bystanders = [s for s in followers if s not in owners] if replies else []
        for session_id in bystanders:
            # Not its PR, but someone answered its own comment there (a review it posted).
            task, lines = self.replied_to(session_id, thread, replies or {}, fresh)
            if lines:
                said = "\n".join(f"- {line}" for line in lines)
                recipients.append(
                    (
                        session_id,
                        task,
                        f"Replies to your comments on {threads.label(thread)}, a PR you "
                        f"follow:\n{said}",
                        said,
                    )
                )
        delivered = []
        for session_id, task, told, shown in recipients:
            attributes = {"task": task["id"]} if task else {}
            held = {
                "prompt": wrap(
                    source, "thread", told, thread=thread, trust=UNTRUSTED, **attributes
                ),
                "display": clip_words(shown or told, 4000),
                "meta": {"source": source, "thread": thread}
                | ({"task_id": task["id"]} if task else {})
                # Its chat line says how many came.
                | ({"count": len(keys)} if len(keys) > 1 else {}),
            }
            title = task["title"] if task else self.display_title(self.store.session(session_id))
            try:
                # Also when live here: the owner may have opened it in a terminal since.
                await self.refuse_if_open_elsewhere(self.store.session(session_id), False)
                if self.agents and (why := self.agent_block(self.store.session(session_id))):
                    # Its agent may not run it now (turned off, say): it gets this once it may.
                    if await self.ensure_spend_card(self.store.session(session_id)):
                        why = self.agent_block(self.store.session(session_id))
                    raise IneligibleError(why)
                await self.delivery.deliver(
                    session_id, held["prompt"], batch=True, kind="inbound",
                    display=held["display"], meta=held["meta"],
                )  # fmt: skip
                delivered.append(title)
            except OpenElsewhereError:
                # Open in a terminal: it keeps following, and gets this once it is closed there.
                news = json.loads(self.store.get(f"heldnews:{session_id}") or "[]")
                self.store.set(f"heldnews:{session_id}", json.dumps([*news, held][-20:]))
                delivered.append(f"{title} (held: open in a terminal)")
            except IneligibleError:
                news = json.loads(self.store.get(f"heldnews:{session_id}") or "[]")
                self.store.set(f"heldnews:{session_id}", json.dumps([*news, held][-20:]))
                delivered.append(f"{title} (held: its agent cannot run it now)")
            except Exception:
                # A follower that cannot take messages stops following; others still get them.
                logger.warning("Could not deliver a thread follow-up.", exc_info=True)
                self.store.unfollow(thread, session_id)
        if not delivered:
            for key in fresh:
                self.store.execute("DELETE FROM triggers WHERE key=?", (key,))
            return await self.on_trigger(source, fresh, text, display)
        with suppress(RuntimeError):
            self.add_message(
                self.router_id(),
                "notice",
                f"{threads.label(thread, self.slack.display_names())} → "
                f"{', '.join(f'“{t}”' for t in delivered)}",
                {"session_id": followers[0]},
            )
        return True

    def alert(
        self,
        text: str,
        *,
        session_id: str,
        needs_action: bool,
        title: str = "Chief of Staff",
        urgent: bool = False,
    ):
        """A toast and a desktop notification; held back while the owner focuses."""
        if self.focus.active() and not urgent:
            return  # It waits in the inbox; the focus summary counts it.
        toast = {"text": text, "session_id": session_id, "needs_action": needs_action}
        self.bus.publish("toast", {**toast, "urgent": urgent})
        if self.store.settings().mac_notifications:
            self.background(self.notifier(title, text))

    # Periodic jobs

    async def watch_loop(self):
        while True:
            await asyncio.sleep(self.watch.interval)
            jobs = (
                self.watchdog.check,
                self.task_actions.refresh_open_prs,
                self.task_actions.reread_roles,
                self.task_actions.check_runs,
                self.workers.tidy,
                self.workers.release_idle,
                self.drafts.check_posted_by_owner,
                self.task_actions.wake_check_backs,
                self.task_actions.nudge_stalled,
                self.task_actions.remind_failed,
                # Queued work starts once memory frees up, not only when a turn ends.
                self.workers.pump,
                self.digest_once,
                self.asks.expire_questions,
                self.focus.check,
                self.workers.refresh_elsewhere,
                # Disk space: read, measured and cleaned in the background (see storage.py).
                self.storage.tick,
            )
            for job in jobs:
                try:
                    await job()
                except Exception:
                    logger.exception("Periodic %s failed", job.__name__)

    async def digest_once(self, moment: float | None = None) -> str | None:
        """Once a day at the configured time: one update with what happened and what waits."""
        settings = self.store.settings()
        moment = time.time() if moment is None else moment
        local = datetime.fromtimestamp(moment).astimezone()
        if not settings.digest_time or local.strftime("%H:%M") < settings.digest_time:
            return None
        day = local.date().isoformat()
        if self.store.get("digest_day") == day or not self.ready():
            return None
        self.store.set("digest_day", day)
        since = float(self.store.get("digest_at") or moment - 86400)
        self.store.set("digest_at", str(moment))
        text = digest(self, since, moment)
        if text:
            self.add_message(self.router_id(), "update", text, {"digest": True}, unread=True)
            self.alert(text.splitlines()[0], session_id=self.router_id(), needs_action=False)
        return text

    # UI snapshot

    def view(self, row: dict) -> dict:
        asking = self.states[row["id"]].asking
        if asking and not row["closed_at"]:
            text = next(iter(asking.values()))
            row = {**row, "status": "waiting", "activity": f"Waiting for you: {text[:80]}"}
        return {
            **row,
            "slack": bool(row["slack"]),
            "live": self.states.is_live(row["id"]),
            "follows": self.store.follows(row["id"]),
            "follow_health": self.follow_health(row["id"]),
            "model": self.effective_model(row),
            "model_choice": row["model"],
            "effort": self.shown_effort(row),
            "effort_choice": row["effort"],
            "context": row["context"],
            "yolo": self.yolo(row),
            "yolo_choice": row["yolo"],
            # Chief of Staff's own helper (learning, checks, profile): not one of the owner's.
            "helper": self.special(row) if row["role"] == WORKER else "",
            # It holds one of the owner's worker slots (in a turn, or finishing one).
            "slot": row["role"] == WORKER and self.states[row["id"]].busy,
            # Its turn is over, and a command it left running (a dev server) goes on.
            "background": self.states[row["id"]].held,
            "checkouts": self.workspaces.checkouts(row),
            # Its own folder (its checkouts are in it, or its files when it has none).
            "workspace": str(self.workspaces.workspace(row)) if row["role"] == WORKER else "",
            "credits": self.store.value(
                "SELECT coalesce(sum(credits), 0) AS c FROM usage WHERE session_id=?", (row["id"],)
            ),
            "task": self.toolset.task_brief(self.tasks.current(row["id"])),
            # The task its current (or last) turn is about: what its steps are for.
            "turn_task": self.working_on(row["id"]) if row["role"] == WORKER else "",
            # Where the owner's next message in its chat goes, unless they look at a task there.
            "chat_task": self.chat_view(row),
            "display_title": self.display_title(row),
            "elsewhere": bool(self.is_elsewhere(row)) and not row["closed_at"],
            "terminal": self.terminal.get(row["id"]),
            **(self.progress(row) if row["role"] == WORKER else {}),
            # Only for a worker on another agent (none without agents).
            **(
                self.agent_view(row, agent)
                if self.agents and (agent := self.agent(row["id"])) != COPILOT_AGENT
                else {}
            ),
        }

    async def read_file(self, raw: str, session_id: str = "") -> dict:
        """A text file in a worker's own folders (what it wrote), for the owner to read."""
        row = self.store.find_session(session_id) if session_id else None
        rows = self.store.all(
            "SELECT * FROM sessions WHERE role=? ORDER BY created_at DESC LIMIT 300", (WORKER,)
        )
        roots = [str(self.work_root)] + [
            root for item in rows for root in self.workspaces.writable(item)
        ]
        return await asyncio.to_thread(read_text_file, raw, row["cwd"] if row else "", roots)

    def turn_task(self, session_id: str) -> str:
        """The task its current (or last) turn is about: named by the latest instruction its
        conversation took up (one sent mid-turn waits until then). Commands it runs belong to
        it, also after it reported (a final push)."""
        if known := self.store.get(f"turntask:{session_id}"):
            return known
        rows = self.store.all(
            """SELECT json_extract(meta, '$.prompt') AS prompt FROM messages WHERE session_id=?
            AND kind IN ('router', 'owner', 'notice') AND json_extract(meta, '$.prompt') IS NOT
            NULL AND json_extract(meta, '$.task_id') IS NOT NULL ORDER BY id DESC LIMIT 20""",
            (session_id,),
        )
        return next((named for row in rows if (named := task_named(row["prompt"]))), "")

    def hidden_artifacts(self) -> list[dict]:
        """What the owner took off their Artifacts list, each with how it stood then (one
        that fails since comes back)."""
        found = json.loads(self.store.get("artifacts_hidden") or "[]")
        return [item if isinstance(item, dict) else {"key": item, "tone": ""} for item in found]

    def hide_artifact(
        self,
        key: str,
        hidden: bool = True,
        tone: str = "",
        also: Sequence[str] = (),
        together: Sequence[tuple[str, str]] = (),
    ) -> list[dict]:
        """The owner's Artifacts list without (or again with) this one: a PR's key or a link.
        `also`: the keys an earlier version gave the same one, replaced by this. `together`:
        more (key, tone) with it (a task's runs, taken off from their line)."""
        key = key.strip()
        if not key:
            raise ValueError("Which artifact?")
        more = [(other.strip(), shade) for other, shade in together if other.strip()]
        gone = {key, *(old.strip() for old in also), *(other for other, _shade in more)}
        found = [item for item in self.hidden_artifacts() if item["key"] not in gone]
        if hidden:
            found = [
                *found,
                *({"key": other, "tone": shade[:20]} for other, shade in [(key, tone), *more]),
            ][-500:]
        self.store.set("artifacts_hidden", json.dumps(found))
        self.publish_state()
        return found

    def chat_task(self, session_id: str, viewing: str = "") -> dict | None:
        """The task the owner's chat with a worker is about: the one they look at in its chat
        (`viewing`: opened at it, or scrolled to it), else the one with its question to the
        owner (the newest asked), else one waiting for them, else the one it is on, else its
        only one, else the one it was given word of last: the owner's message about it, or the
        Router's on a task it had (a check-back, a note, more of it, a decision changed). Not
        blindly the one given last (bug QA6-3: a request that went on with an earlier task was
        filed under the one the Router gave it since): when the Router's latest word started a
        task (that task's first instruction), it cannot be told, None, and the message is a task
        of its own (see owner_send)."""
        mine = self.tasks.for_session(session_id)
        looked = next((t for t in mine if viewing and t["id"] == viewing), None)
        if looked and looked["status"] != "cancelled":
            return looked
        asking = [q for q in self.asks.session_questions(session_id) if q.get("task_id")]
        for question in reversed(asking):
            task = self.tasks.find(question["task_id"])
            if task and task["session_id"] == session_id and task["status"] in OPEN:
                return task
        waiting = [t for t in mine if t["status"] == "waiting"]
        if waiting:
            return waiting[-1]
        if on := self.working_on(session_id):
            return self.tasks.find(on)
        kept = [t for t in mine if t["status"] != "cancelled"]
        if len(kept) <= 1 or self.special(self.store.session(session_id)):
            return kept[-1] if kept else self.tasks.current(session_id)
        given = self.store.one(
            "SELECT id, kind, json_extract(meta, '$.task_id') AS task_id FROM messages "
            "WHERE session_id=? AND kind IN ('owner', 'router') "
            "AND json_extract(meta, '$.prompt') IS NOT NULL "
            "AND json_extract(meta, '$.task_id') IS NOT NULL ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        if not given:
            return None
        if given["kind"] == "router":
            # Review M1: a check-back, a note or a changed decision on a task it had is about
            # that task; only the instruction that started one leaves it open.
            started = self.store.value(
                "SELECT min(id) FROM messages WHERE session_id=? AND kind IN ('owner', 'router') "
                "AND json_extract(meta, '$.prompt') IS NOT NULL "
                "AND json_extract(meta, '$.task_id')=?",
                (session_id, given["task_id"]),
            )
            if started == given["id"]:
                return None
        return next((t for t in kept if t["id"] == given["task_id"]), None)

    def chat_view(self, row: dict) -> dict | None:
        """Where the owner's next message in a worker's chat goes, unless they look at one of
        its tasks there: {"id", "title"} of that task, or "" for a task of its own (see
        chat_task), with the titles of its tasks (the one they look at is named by it); None
        when it has one task or none (nothing to choose from)."""
        if row["role"] != WORKER or row["closed_at"] or self.special(row):
            return None
        kept = [t for t in self.tasks.for_session(row["id"]) if t["status"] != "cancelled"]
        if len(kept) <= 1:
            return None
        task = self.chat_task(row["id"])
        return {
            "id": task["id"] if task else "",
            "title": task["title"] if task else "",
            "titles": {t["id"]: t["title"] for t in kept},
        }

    def drop_chat_task(self, task: dict, was: str | None):
        """A task made of the owner's message that never reached its worker (its delivery
        failed): gone again, and its worker is named as before (review L2)."""
        self.store.execute("DELETE FROM runs WHERE task_id=?", (task["id"],))
        self.store.execute("DELETE FROM tasks WHERE id=?", (task["id"],))
        self.store.execute(
            "UPDATE messages SET meta=json_remove(meta, '$.task_id') WHERE session_id=? "
            "AND json_extract(meta, '$.task_id')=?",
            (task["session_id"], task["id"]),
        )
        self.store.execute("UPDATE sessions SET task_id=? WHERE id=?", (was, task["session_id"]))
        self.publish_state()  # Gone from every list.
        self.publish_session(self.store.session(task["session_id"]))

    def new_chat_task(self, session_id: str, text: str) -> dict:
        """The owner's message to a worker that is about none of its tasks in particular: a task
        of its own, named from what they wrote, its worker on it now (the row on Home says so)."""
        lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        return self.tasks.create(
            session_id,
            clean_title(lines[0] if lines else text),
            text.strip()[:4000],
            status="running",
            why_new="You wrote it in its chat, about none of its tasks in particular.",
        )

    def took_up(self, session_id: str, tasks: list[str], switch: bool = True, news: bool = False):
        """Its conversation shows instructions on these tasks: its turn is on the last; each has
        been taken up (one given while it was on another waited its turn until now).
        `switch=False`: taken up, but its turn stays on the task it is on (a message about
        another task steered into its turn: an answer it asked for, say). `news`: a followed
        thread's news for them (a PR's review): one the owner stopped stays stopped."""
        fresh = [task_id for task_id in tasks if not self.store.get(f"started:{task_id}")]
        turn = self.turn_task(session_id)
        for task_id in tasks:
            # Taken up again after its result: other work may have come in between. Back to it
            # from another task: what it changes from here starts where its checkouts are now.
            again = task_id not in fresh and self.store.get(f"taskend:{task_id}")
            if again and (task := self.tasks.find(task_id)):
                resumed = turn != task_id
                self.background(self.task_actions.record_restart(task, resumed=resumed))
        for task_id in tasks:
            self.store.set(f"started:{task_id}", "1")
            if self.store.get(f"stopped:{task_id}") and not news:
                # Told to go on with a task the owner stopped: once it works, it runs again.
                self.store.set(f"goingon:{task_id}", "1")
        if fresh:
            # Where its checkouts are now: what comes after is this task's (see Scope).
            job = self.background(self.task_actions.record_base(session_id, fresh, time.time()))
            self.task_actions.basing.update(dict.fromkeys(fresh, job))
        if not switch:
            on = self.tasks.find(self.turn_task(session_id))
            if on and on["session_id"] == session_id and on["status"] in OPEN_WORK:
                return
        self.store.set(f"turntask:{session_id}", tasks[-1])

    def working_on(self, session_id: str) -> str:
        """The task it is on, as the owner sees it: its turn's task while that is open (or the
        owner asked about it after it finished); once reported or accepted, the task it still
        has open: its current one, else its newest running one."""
        turn = self.turn_task(session_id)
        found = self.tasks.find(turn) if turn else None
        if (
            found
            and found["session_id"] == session_id
            and (found["status"] in OPEN_WORK or self.store.get(f"followup:{turn}"))
        ):
            return turn
        open_ = [t for t in self.tasks.for_session(session_id) if t["status"] in OPEN_WORK]
        current = self.tasks.current(session_id)
        if current and current["status"] in OPEN_WORK:
            return current["id"]
        return open_[-1]["id"] if open_ else ""

    def at_work(self, session_id: str) -> dict | None:
        """The task a worker's words, reports and asks belong to now: in a turn, the one that
        turn is on (turn_task: finished work it checks on too, not one that ended without a
        result); else the one it is on (working_on); else the one it was given or finished
        last. Not that one first (bug E86): it may be done long ago, or wait its turn."""
        if turn := self.in_turn_on(session_id):
            return turn
        on = self.working_on(session_id)
        return (self.tasks.find(on) if on else None) or self.tasks.current(session_id)

    def in_turn_on(self, session_id: str) -> dict | None:
        """The task of the turn a worker is in now (finished work it checks on, or reported in
        it, too; not one that ended without a result); None between turns."""
        if not self.states[session_id].busy:
            return None
        turn = self.tasks.find(self.turn_task(session_id) or "")
        if (
            turn
            and turn["session_id"] == session_id
            and turn["status"] not in {"cancelled", "failed"}
        ):
            return turn
        return None

    def progress(self, row: dict) -> dict:
        """What a session is doing now and what it said last: progress at a glance."""
        since, step, command = self.states[row["id"]].current_step()
        said = json.loads(self.store.get(f"said:{row['id']}") or "{}")
        running = bool(since) and row["status"] == "working"
        return {
            "step": step if running else "",
            "step_command": command if running else "",
            "step_since": datetime.fromtimestamp(since, UTC).isoformat() if running else "",
            "said": said.get("text", ""),
            "said_at": said.get("at", ""),
        }

    def progress_line(self, row: dict, brief: bool = False) -> str:
        """The same in words, for the Router and for the owner's messages to a busy worker."""
        seen = self.progress(row)
        parts = []
        limit = 120 if brief else 300
        if seen["step"]:
            parts.append(f"now in a step for {span(seen['step_since'])}: {seen['step'][:limit]}")
        elif row["status"] == "working":
            parts.append("working (thinking between steps)")
        if seen["said"]:
            said = " ".join(seen["said"].split())[:limit]
            parts.append(f"said {span(seen['said_at'])} ago: “{said}”")
        return "; ".join(parts)

    def publish_session(self, row: dict):
        self.bus.publish("session", self.view(row))

    def remember_pr_title(self, label: str, title: str):
        """A PR's title ("owner/repo#n"), shown with its number wherever the PR is named."""
        title = " ".join((title or "").split())[:200]
        key = f"prtitle:{label.lower()}"
        if title and self.store.get(key) != title:
            self.store.set(key, title)

    def display_title(self, row: dict) -> str:
        """A worker by what it is on: the task it works on now (see working_on), else its newest
        open task, else its newest task; the title it was started with when it has none. The
        same name everywhere: sidebar, Home, notices and away messages."""
        if row["role"] != WORKER:
            return row["title"]
        tasks = self.tasks.for_session(row["id"])
        if not tasks:
            return row["title"]
        # The task its turn is on (bug E88: not one given since, nor one a note was about).
        if turn := self.in_turn_on(row["id"]):
            return turn["title"]
        # The task it works on now (not one given last that waits its turn).
        if (on := self.working_on(row["id"])) and (task := self.tasks.find(on)):
            return task["title"]
        if open_ := [task for task in tasks if task["status"] in OPEN]:
            return open_[-1]["title"]

        # Nothing open: the task it did last (not the newest made: one split off keeps the
        # worker's name with the task it came from, until the worker works on it).
        def last_active(task: dict) -> tuple:
            split = json.loads(self.store.get(f"splitfrom:{task['id']}") or "null")
            if split:
                worked = self.store.value(
                    "SELECT max(id) FROM messages WHERE session_id=? AND id>? "
                    "AND json_extract(meta, '$.task_id')=?",
                    (row["id"], split["mark"], task["id"]),
                )
                return (1, worked) if worked else (0, 0)
            # Not what Chief of Staff told it about the task (cancelled, closed: bug E88).
            worked = self.store.value(
                "SELECT max(id) FROM messages WHERE session_id=? AND kind!='notice' "
                "AND json_extract(meta, '$.task_id')=?",
                (row["id"], task["id"]),
            )
            return (1, worked or 0)

        return max(tasks, key=last_active)["title"]

    def made_pr(self, task: dict, label: str, creator: str | None = None) -> bool:
        """Whether this task made the PR ("owner/repo#n", lowercase): it opened it (by record,
        or its report says so and no other task opened it), or its PR chips count it as its
        own. Not a task that only names, reads or reviews it. `creator`: the PR's, when read
        already (TaskActions.creator)."""
        if creator is None:
            creator = self.task_actions.creator(label)
        return (
            task["id"] == creator
            or label in self.task_actions.opened_prs(task["id"])
            or any(
                pr.get("own") and pr["label"].lower() == label
                for pr in self.task_actions.previous_prs(task["id"])
            )
        )

    def pr_maker(self, session_id: str, thread: str) -> dict | None:
        """The worker's task that made the PR a thread key names, the newest first (one still
        standing before a cancelled or failed one); None for a Slack thread or when none of
        its tasks made it."""
        found = threads.GITHUB_PR.fullmatch(thread)
        if not found:
            return None
        label = f"{found.group(1)}#{found.group(2)}".lower()
        creator = self.task_actions.creator(label)
        made = [
            task
            for task in reversed(self.tasks.for_session(session_id))
            if self.made_pr(task, label, creator)
        ]
        return next(
            (task for task in made if task["status"] not in {"cancelled", "failed"}),
            made[0] if made else None,
        )

    def draft_task(self, session_id: str, target: dict) -> str:
        """The task a worker's draft is for: the one following the thread it answers (a Slack
        thread, a PR), else the one its turn is on; "" when neither is known (see
        Store.create_draft)."""
        row = self.store.find_session(session_id)
        if not row or row["role"] != WORKER:
            return ""
        thread = ""
        if target.get("channel_id"):
            channel, ts = target["channel_id"], target.get("thread_ts") or ""
            thread = f"slack:{channel}:{ts}" if ts else f"slack:{channel}"
        elif target.get("repo") and target.get("number"):
            thread = f"github:{str(target['repo']).lower()}#{target['number']}"
        turn = self.tasks.find(self.turn_task(session_id))
        if not (turn and turn["session_id"] == session_id and turn["status"] != "cancelled"):
            turn = None
        if thread and (task := self.thread_task(session_id, thread)):
            return task["id"]
        return turn["id"] if turn else ""

    def thread_task(self, session_id: str, thread: str) -> dict | None:
        """Which of a worker's tasks a followed thread is about: for a PR, the task that made
        it (its news is that work's, whichever later task of the worker follows or names it
        too), unless it no longer follows it (stopped for it, or followed for other tasks
        only); else the newest one the follow was made for, not failed or cancelled (bug E99:
        its news woke its worker for work nobody wants). None when neither is: a task
        that only names it (its report, its instructions) does not make it its own (bug E46:
        news and drafts went to the latest that did); the Router is told instead."""
        maker = self.pr_maker(session_id, thread)
        bound = self.store.follow_tasks(session_id, thread)
        if (
            maker
            and maker["status"] not in {"cancelled", "failed"}
            and not self.store.get(f"stoppedfor:{maker['id']}:{thread}")
            and (not bound or maker["id"] in bound)
        ):
            return maker
        for task in reversed(self.tasks.for_session(session_id)):
            if task["id"] in bound and task["status"] not in {"cancelled", "failed"}:
                return task
        return None

    def thread_owners(self, followers: list[str], thread: str) -> list[str]:
        """Of the workers following a PR, those with a task that made it (any of their tasks,
        not only the newest that names it): its news is theirs to act on (a worker that only
        reviewed, read or reported on it would ask the owner the same). Everyone following it
        when none made it. Of a Slack thread's followers, the workers: one that follows it took
        it over from the Slack session (one reply per message, not one from each)."""
        if thread.startswith("slack:"):
            workers = [
                session_id
                for session_id in followers
                if (row := self.store.find_session(session_id)) and row["role"] == WORKER
            ]
            return workers or followers
        if not threads.GITHUB_PR.fullmatch(thread):
            return followers
        owners = [session_id for session_id in followers if self.pr_maker(session_id, thread)]
        return owners or followers

    def replied_to(
        self, session_id: str, thread: str, replies: dict[str, tuple[str, str]], keys: list[str]
    ) -> tuple[dict | None, list[str]]:
        """Of a PR's news, the replies to comments this worker posted there (`replies`: an
        item's key -> the task whose comment it answers, "" when not known, and its line):
        that task, and those lines. A reviewer hears what answers its own review even when
        the PR's maker gets the rest."""
        tasks = {task["id"]: task for task in self.tasks.for_session(session_id)}
        found = threads.GITHUB_PR.fullmatch(thread)
        label = f"{found.group(1)}#{found.group(2)}".lower() if found else ""
        reviewer = label in self.task_actions.reviewed_prs(session_id)
        task, lines = None, []
        for key in keys:
            task_id, line = replies.get(key, ("", ""))
            if task_id in tasks or (line and not task_id and reviewer):
                task = task or tasks.get(task_id) or self.thread_task(session_id, thread)
                lines.append(line)
        return task, lines

    def publish_state(self):
        self.bus.publish("state")

    def slack_names(self) -> dict[str, str]:
        """Names by Slack id, for the page: the owner's own id too (Slack lists it without a
        name), as "you" when no name was read for it."""
        names = self.slack.display_names()
        if owner := self.slack.status.get("user_id"):
            names = {**names, owner: names.get(owner) or "you"}
        return names

    def owner_updates(self) -> list[dict]:
        """The Router's notes that need the owner (needs_action or urgent) from the last three
        days: under Needs you until the owner says they saw one or writes to the Router after it."""
        router = self.store.get("router_id")
        rows = self.store.all(
            "SELECT id, text, meta, created_at FROM messages WHERE session_id=? AND kind='update'"
            " AND created_at > ? AND id > coalesce((SELECT id FROM messages WHERE session_id=?"
            " AND kind='owner' AND json_extract(meta, '$.to') IS NULL ORDER BY id DESC LIMIT 1),"
            " 0) ORDER BY id",
            (router, hours_ago(72), router),
        )
        seen = self.store.items("updateseen:")
        updates = []
        for row in rows:
            meta = json.loads(row["meta"] or "{}")
            if (meta.get("needs_action") or meta.get("urgent")) and str(row["id"]) not in seen:
                found = {"id": row["id"], "text": row["text"], "at": row["created_at"]}
                updates.append({**found, "urgent": bool(meta.get("urgent"))})
        return updates

    def update_seen(self, message_id: int):
        """The owner saw a note of the Router's that needed them: it leaves Needs you."""
        message = self.store.message(message_id)
        if message["kind"] != "update" or message["session_id"] != self.store.get("router_id"):
            raise ValueError("Only the Router's updates can be marked seen.")
        self.store.set(f"updateseen:{message_id}", now())
        self.publish_state()

    def snapshot(self) -> dict:
        return {
            "runtime": self.status,
            # Why Send to Chief's socket is not open, while the setting is on.
            **({"send_problem": self.send.problem} if self.send.problem else {}),
            # Its command, when it is not `chiefofstaff` in the owner's shell (bug E63: started
            # with uvx, there is no such command there).
            **(
                {"send_command": {"command": self.send_command[0], "install": self.send_command[1]}}
                if self.send_command[0] != "chiefofstaff"
                else {}
            ),
            "demo": getattr(self, "demo", False),
            # A newer Chief of Staff, how it updates, and where its update stands (no line).
            **(
                {"update": {**notice, **(self.updating or {})}}
                if (notice := updates.notice(self.store))
                else {}
            ),
            "focus": self.focus.state(),
            "router_id": self.store.get("router_id"),
            "sessions": [self.view(row) for row in self.store.open_sessions()],
            "drafts": self.store.drafts(),
            "uncertain": self.store.drafts("uncertain"),
            # Drafts withdrawn or replaced lately: what their lines in a timeline say.
            "draft_outcomes": {
                row["id"]: row["outcome"]
                for row in self.store.all(
                    "SELECT id, outcome FROM drafts WHERE status IN ('rejected', 'withdrawn', "
                    "'replaced') AND resolved_at > ? AND (status != 'rejected' OR outcome LIKE "
                    "'Withdrawn%' OR outcome LIKE 'Replaced by%')",
                    (hours_ago(72),),
                )
            },
            # PRs by "owner/repo#n" with their titles; followed threads with a link to them.
            "pr_titles": self.store.items("prtitle:"),
            "slack_names": self.slack_names(),
            "artifacts_hidden": self.hidden_artifacts(),
            "thread_links": self.store.items("threadlink:"),
            # As the owner reads them: no draft ids, Slack ids or timestamps.
            "questions": [
                {
                    **question,
                    "text": self.drafts.owner_text(question.get("text") or ""),
                    "why": self.drafts.owner_text(question.get("why") or ""),
                    "next": self.drafts.owner_text(question.get("next") or ""),
                }
                for question in self.asks.owner_questions()
            ],
            "updates": self.owner_updates(),
            "tasks": [
                self.task_actions.view(task)
                for task in self.tasks.listed(include_ended_since=hours_ago(TASKS_SHOWN_HOURS))
            ],
            "settings": self.store.settings().model_dump(),
            # Only while another coding agent is on: Settings → Models shows its group.
            **({"agents": self.agents_view()} if self.agents_on() else {}),
            # Configured projects and the code folder's repositories (where a habit applies).
            "projects": self.workspaces.projects(),
            # Configured projects whose folder is gone: Settings says so.
            "missing_projects": [
                project
                for project in self.store.settings().projects
                if not os.path.isdir(project)  # noqa: PTH112  (False when it cannot look)
            ],
            "slack": {
                **self.slack.status,
                "token_saved": bool(self.slack.tokens.get()),
                "token_source": self.slack.token_source(),
            },
            "github": self.github.status,
            "closed": [self.view(row) for row in self.workers.recently_closed(20)],
            "closed_count": self.workers.closed_count(),
            "profile": self.store.get("profile", ""),
            "profile_draft": self.store.get("profile_draft", ""),
            "learning": self.learning.view(),
            "habits": self.habits.view(),
            "abilities": self.abilities.view(),
            "connector": self.connector.view(),
            "checks": self.checks.view(),
            "setup_notes": self.asks.setup_notes(),
            "sandbox": {
                "supported": self.sandbox_host,
                "gh_trusted": self.sandbox_gh,
            },
        }

    async def settings_changed(self, old: Settings, new: Settings) -> list[str]:
        """Returns why each worker it stopped stopped (another agent's, made ineligible)."""
        stopped: list[str] = []
        if old.other_sessions != new.other_sessions:
            # First, before anything awaits: once the save returns, nothing more is told.
            self.others.switch(new.other_sessions)
        if old.send_to_chief != new.send_to_chief:
            # Off: the socket closes before anything awaits; the Router's rule goes with its
            # next turn.
            self.send.switch(new.send_to_chief)
            await self.refresh(ROUTER)
        if self.agents and (
            old.worker_sandbox != new.worker_sandbox or old.worker_yolo != new.worker_yolo
        ):
            # Other agents' workers do not run sandboxed, and act on their own or ask first only
            # where their version's check allows it: stopped at once, with their commands.
            stopped = await self.stop_ineligible()
        if self.agents and old.worker_yolo != new.worker_yolo:
            # Those following the default switched between acting on their own and Ask first,
            # which their agent takes up in a new process (agent_mode_changed).
            for row in self.store.open_sessions(WORKER):
                if self.on_copilot(row["id"]) or row.get("yolo") in {"on", "off"} or row["slack"]:
                    continue
                if await self.agent_mode_changed(row, asks_first=not new.worker_yolo):
                    stopped.append(agents.switched_to_ask_first(self.agent(row["id"])))
        if old.mcp_servers != new.mcp_servers:
            # Other MCP servers: a worker's tools may fit now (fit_refusal).
            for key in self.store.keys("nofit:"):
                self.store.delete(key)
        defaults = ("model", "role_models", "role_efforts")
        if any(getattr(old, key) != getattr(new, key) for key in defaults):
            # Models and efforts chosen now are tried, even where Copilot refused one before.
            for key in self.store.keys("fallback:"):
                row = self.store.find_session(key.removeprefix("fallback:"))
                if not (row and row["model"] and row["effort"]):  # Settings choose for it.
                    self.forget_fallback(key.removeprefix("fallback:"))
            self.unrefuse(
                *({new.model} - {old.model}),
                *(m for role, m in new.role_models.items() if old.role_models.get(role) != m),
            )
        if old.copilot_command != new.copilot_command or not self.ready():
            await self.reconnect()
        elif old.slack_enabled != new.slack_enabled:
            await self.refresh_slack()
        if self.ready() and any(getattr(old, key) != getattr(new, key) for key in defaults):
            for session_id in self.states.live_ids():
                row = self.store.find_session(session_id)
                # Copilot's models: not for a worker on another agent (its own, in its group).
                if row and not (row["model"] and row["effort"]) and self.on_copilot(row["id"]):
                    with suppress(Exception):
                        await self.apply_model(row)
        changed = (
            "worker_credit_limit",
            "worker_yolo",
            "worker_sandbox",
            "sandbox_writable",
            "shared_caches",
            "skill_folders",
            "mcp_servers",
        )
        if any(getattr(old, key) != getattr(new, key) for key in changed):
            await self.refresh(WORKER)
        for source in self.all_sources():
            source.wake()
        if new.plugins != old.plugins:
            self.background(self.abilities.sync())
        if new.learn_from_work and not old.learn_from_work and self.ready():
            self.background(self.habits.catch_up())
        self.background(self.workers.pump())
        self.publish_state()
        return stopped
