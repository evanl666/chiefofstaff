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
import contextvars
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
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from copilot import Tool, ToolResult

from . import (
    agents,
    agentsetup,
    caches,
    claude_setup,
    compat,
    prompts,
    settings_guard,
    threads,
    updates,
)
from . import workspaces as workspaces_module
from .abilities import Abilities
from .accounts import Accounts
from .asks import Asks
from .backend import (
    CLIENT_NAME,
    COPILOT_AGENT,
    OLDEST_CLI,
    REFUSED_CLI,
    AgentBackend,
    CopilotBackend,
    SandboxPolicy,
    UnavailableError,
    big_log,
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
from .backend import COPILOT as COPILOT_CAPS
from .bus import Bus
from .checks import Checks
from .connector import Connector
from .decisions import Decisions
from .delivery import Delivery, TooLargeError
from .digest import digest, needs, task_words
from .drafts import Drafts
from .envelope import (
    UNTRUSTED,
    clean_title,
    language,
    sender,
    short,
    switched_note,
    task_named,
    tasks_named,
    wrap,
)
from .events import EFFORT_REFUSED, MODEL_REFUSED, ROUTER_CONTEXT, Events
from .focus import Focus
from .forks import Forks
from .habits import Habits
from .handled import Handled
from .learning import Learning
from .models import AS_SAVED, Attachment, SentItem, Settings
from .notify import mac_notification
from .others import Others
from .peers import Peers
from .policy import (
    AGENT_SETTINGS,
    CLAUDE_SETTINGS,
    CODEX_SETTINGS,
    GITHUB_MCP,
    OWN_COPY,
    OWN_WORKTREE,
    REPOSITORY_SETTINGS,
    SANDBOX_HELD,
    checkout_of,
    checkout_reading,
    owners_checkouts,
    within,
)
from .provenance import ALIVE_EVERY, RUNS_KEPT, Provenance, log_of, runs_of
from .relations import Relations
from .roles import ROUTER, SLACK, WATCHDOG, WORKER
from .send import shell_command
from .sendsocket import LimitedError, SendSocket
from .sendsocket import headline as sent_headline
from .sendsocket import parts as sent_parts
from .sendsocket import prompt as sent_prompt
from .sendsocket import shown as sent_shown
from .settings_guard import SettingsChangedError, checkout_top, outermost_top, read_regular
from .shares import Shares
from .shepherd import Shepherd
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
    migrate_replacedby,
    settle_adopted,
    settle_decision_changes,
    settle_followups,
)
from .tasks import migrate as migrate_tasks
from .teams import Teams
from .teams_lite import TeamsLite
from .tools import SpawnWorker, Tools
from .watchdog import WatchConfig, Watchdog
from .workers import SETTINGS_STOP, SETTINGS_WAIT, Workers
from .workspaces import (
    ABSENT_HELD_CLI,
    BRANCH_PREFIX,
    Committed,
    Workspaces,
    blob_id,
    checkout_git,
    common_git_dir,
    git,
    git_dirs,
    holds_alternates,
    identity_of,
    own_code,
    own_tree,
    place_in,
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

# A Copilot session that could not be resumed for a version mismatch (Runtime.continue_fresh):
# what its worker shows meanwhile, and the line its timeline keeps (for the extensions it asks
# for; for anything else of the old one this Copilot can't read).
RECONNECTING = "Reconnecting this worker…"
CONTINUED_FRESH = (
    "Continued in a fresh Copilot session: the old one used an extension setup this Copilot "
    "can't resume."
)
CONTINUED_AFRESH = (
    "Continued in a fresh Copilot session: this Copilot version can't resume the old one."
)
# What the owner writes a worker held for its folder's settings, in its own chat, that says to
# use them (E121): only these, the whole message. A bare "ok" or "yes" only right after the line
# that asks (settings_typed). Anything else, however close, gets the line's buttons.
SAID_USE = re.compile(
    r"(?:use (?:these|the) settings|use them|approved?(?: (?:them|it|these|these settings|the "
    r"settings))?|i(?:'ve| have)? approved(?: (?:them|it|these|these settings|the settings))?|go "
    r"ahead with (?:the|these) settings|同意|批准|可以用|用这些设置)[.!。\uff01]?",
    re.IGNORECASE,
)
SAID_YES = re.compile(r"(?:ok(?:ay)?|yes)[.!。\uff01]?", re.IGNORECASE)
APPROVAL_LIKE = re.compile(
    r".{0,12}\b(?:ok(?:ay)?|yes|approved?|use (?:it|them)|go ahead|lgtm)\b.{0,24}"
    r"|.{0,6}(?:同意|批准|可以|好的?).{0,12}",
    re.IGNORECASE,
)
USE_THE_BUTTON = "Use the button so the approval is tied to the settings shown."
CHANGED_SINCE = "They changed after you wrote that: look at them again, then use the button."
# How long "Undo" on the owner's word on a folder's settings stays offered.
SETTINGS_UNDO_SECONDS = 24 * 3600
# How long a worker in a worktree Chief of Staff made waits for that word before its task goes
# on in a new worker instead (Runtime.move_long_held).
SETTINGS_MOVE_SECONDS = 15 * 60

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
    "read_team_thread",
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
# As Copilot connects, its sessions that were at work or mid-send are resumed (resume_sessions):
# in the background, each in SESSION_RESUME seconds at most; the connection waits for them up to
# RESUME_WAIT seconds (all of them, as a rule), then is ready without them. What is sent to one
# of them meanwhile waits for it (session_for).
RESUME_WAIT = float(os.environ.get("CHIEFOFSTAFF_RESUME_WAIT", "5"))
SESSION_RESUME = float(os.environ.get("CHIEFOFSTAFF_SESSION_RESUME", "180"))
# Set in resume_sessions' own work (what it sends does not wait for itself).
RESUMING: contextvars.ContextVar[bool] = contextvars.ContextVar("resuming", default=False)
# How long Copilot's sessions are given to let go as it disconnects (seconds).
DISCONNECT_WAIT = 5.0
# A worker whose Copilot session is too large for Copilot to reopen here (Runtime.too_large):
# at rest, with this, until the owner starts a fresh one for it.
TOO_LARGE_WAIT = "waiting for you: its Copilot session is too large to reopen here"
TOO_LARGE = "is too large for Copilot to reopen here"
# Recovery (Runtime.recover) waits RECONNECT_DELAY seconds at first while Chief of Staff can't
# run, and this long while it runs on another agent with Copilot on but unable to run (signed
# out, not installed, can't start), doubling up to RECONNECT_MOST.
COPILOT_RETRY = float(os.environ.get("CHIEFOFSTAFF_COPILOT_RETRY", "30"))
# Items sent with Send to Chief: at most this many in a while (seconds) are taken; the same
# note and capture again within SENT_AGAIN_SECONDS is the command run twice.
SENT_RATES = ((60, 10, "a minute"), (86400, 300, "a day"))
SENT_AGAIN_SECONDS = 600
# How long the owner's message to the Router waits for the listing of their sessions that names
# the ones opened in a terminal since (seconds; terminals_note).
TERMINALS_WAIT = 3.0
# How long the owner's sends from the page are known by the id the page gave each (seconds):
# one sent again with it in that time is answered with what the first stored (owner_send_once).
CLIENT_SEND_SECONDS = 300.0
# How many of a session's latest owner messages are looked through for one (sent_with), and how
# long a page that reloaded waits for its send still on its way (client_sent; seconds).
CLIENT_SENT_LOOK = 200
CLIENT_SENT_WAIT = 20.0


# How long a turn of another agent waits for what the last one spent (seconds).
USAGE_SETTLES = 60.0
# How long which other agents are installed here is taken as known (agents_found).
FOUND_SECONDS = 60.0
# How long an agent's glance is taken as its status when the page asks again (agent_status).
STATUS_SECONDS = 30.0
# How soon one that could not run (signed out, not installed) is looked at again when asked.
UNREADY_SECONDS = 5.0
# One whose look failed otherwise (it timed out, say): looked at again twice as late each time,
# up to this.
PROBLEM_SECONDS = 60.0
# How long the page's ask for statuses waits for the looks it started: one slower goes on, and
# the page hears once its status changes.
REFRESH_WAIT = 3.0
# How long held work waits, "Resuming…" with Cancel, once its agent can run it (resume_held).
RESUME_SECONDS = 5.0
# What held work's card says once it went on, by why it was held.
RESUMED = {
    agents.SIGN_IN: "resumed after sign-in",
    agents.MISSING: "resumed once installed",
    agents.LIMITED: "resumed once its limit reset",
    agents.UNCHECKED: "resumed once Chief of Staff's check of it ran",
}
# How long past a plan limit's reset its agent is asked again (its clock and this one's), and
# how long it may take to say.
LIMIT_MARGIN = 5.0
LOOK_LIMIT_SECONDS = 30.0
# What a worker has done once it said any of these: a step, a report, an update (until then,
# Restart on… starts its task on another agent instead; Runtime.restart_on).
DOING = frozenset({"tool", "report", "update"})
# How long a worker's dropped schedules take to be deleted in Claude, at most (its turn included).
SCHEDULES_DELETE_SECONDS = 120.0
# How soon an agent's check that could not run is tried again, at the earliest (seconds), and
# after each that could not run in a row (CHECK_AGAIN: by itself meanwhile, while a role or held
# work wants that agent: never final); and how soon after its last a worker's process stopping
# by itself again fails it instead of starting another (never a loop of starts).
AGENT_CHECK_AGAIN = 60.0
CHECK_AGAIN = (AGENT_CHECK_AGAIN, 120.0, 300.0, 600.0, 1800.0)
AGENT_LOST_AGAIN = 120.0
# Chief of Staff's own sessions Settings can put on another agent and that keep a session of
# their own (agent-roles.md, agents-anywhere.md): placed by Runtime.place_role. Watch for you
# and the helpers run as helper workers, on their agent from their next run (helper_agent).
PLACED_ROLES = (ROUTER, WATCHDOG, SLACK)
# A role on Copilot because its agent failed it (its sign-in, its process) tries that agent
# again after this long (seconds), doubling up to the most; after a plan's limit with no reset
# said, after ROLE_LIMIT_WAIT.
ROLE_RETRY = float(os.environ.get("CHIEFOFSTAFF_ROLE_RETRY", "300"))
ROLE_RETRY_MOST = 3600.0
ROLE_LIMIT_WAIT = 1800.0
# Meanwhile, an agent that could not start (signed out, not installed) is glanced at this often
# (its CLI's version and sign-in, as for a first-run card): signed in again, it is tried at once.
ROLE_GLANCE = float(os.environ.get("CHIEFOFSTAFF_ROLE_GLANCE", "60"))
# What an agent says when it is not signed in any more (Codex's and Claude's words).
SIGNED_OUT = re.compile(
    r"\b401\b|unauthori[sz]ed|not (?:signed|logged) in|invalid api key|please run /login"
    r"|oauth token has expired|authentication (?:failed|error)",
    re.IGNORECASE,
)


def plain_why(why: str) -> str:
    """Why an agent can't run something, as one line says it after a colon ("Codex is not
    signed in: …", not "Codex could not start: Codex is not signed in: …")."""
    head, _, rest = why.partition(" could not start: ")
    if rest.startswith(head + " "):
        why = rest
    why = why.strip()
    return why if why.endswith((".", "!", "?")) else why + "."


def clock_time(at: float) -> str:
    """A time of day as the page says it: "7:17 AM" (this computer's time)."""
    return datetime.fromtimestamp(at, UTC).astimezone().strftime("%I:%M %p").lstrip("0")


def said_parts(iso: str) -> tuple[datetime, str, str, str]:
    """(the time, its day as a prefix: "" today, "Thu " this week, "Nov 12, " before; its
    clock, "3:02"; "AM" or "PM"), in this computer's time."""
    at = datetime.fromisoformat(iso).astimezone()
    days = (datetime.now().astimezone().date() - at.date()).days
    day = "" if days == 0 else f"{at:%a} " if 0 < days < 7 else f"{at:%b} {at.day}, "
    return at, day, f"{at.hour % 12 or 12}:{at.minute:02d}", "AM" if at.hour < 12 else "PM"


def said_at(iso: str) -> str:
    """When something was, as the owner reads it: "3:02 PM", "Thu 3:02 PM", "Nov 12, 3:02 PM"."""
    _, day, clock, half = said_parts(iso)
    return f"{day}{clock} {half}"


DASH = "\u2013"  # (An en dash: "3:02-5:40 PM" as the owner reads it.)


def said_between(first: str, last: str) -> str:
    """When things were, from the first to the last, with an en dash between: "Thu 3:02-5:40
    PM", "Thu 11:50 AM-1:10 PM", "Thu 3:02 PM - Fri 9:01 AM"; one time when they are the same
    minute."""
    start, day, clock, half = said_parts(first)
    end, day2, clock2, half2 = said_parts(last)
    if (day, clock, half) == (day2, clock2, half2):
        return f"{day}{clock} {half}"
    if start.date() == end.date():
        if half == half2:
            return f"{day}{clock}{DASH}{clock2} {half}"
        return f"{day}{clock} {half}{DASH}{clock2} {half2}"
    return f"{day}{clock} {half} {DASH} {day2}{clock2} {half2}"


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
    # Whether the owner has it turned on (Settings → Agents & models): off, none of its workers
    # runs.
    enabled: Callable[[], bool] = lambda: True
    # It stopped by itself and could not start again: it is tried again with its next worker's
    # message (Settings says so).
    offline: bool = False


class IneligibleError(RuntimeError):
    """A worker on another agent may not run now (Runtime.agent_block says why)."""


class NoAgentError(RuntimeError):
    """No coding agent can run Chief of Staff now (Copilot off or unable, no other on that
    can): why each can't, for Home's card (`setup`); it goes on once one can."""

    def __init__(self, message: str, setup: bool = True):
        super().__init__(message)
        self.setup = setup


# Copilot's Use switch off (Settings → Agents & models): nothing runs on it; turned on again,
# it starts.
COPILOT_OFF = "Copilot is turned off in Settings → Agents & models."
COPILOT_STARTING = "Copilot is starting."


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
    """One of another agent's models, as describe_model has Copilot's, whether it is that
    agent's default, and the exact model it stands for now (Claude's aliases)."""
    resolved = getattr(model, "resolved", "")
    return (
        describe_model(model)
        | {"default": bool(getattr(model, "is_default", False))}
        | ({"resolved": resolved} if isinstance(resolved, str) and resolved else {})
    )


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


def agent_settings(agent: str) -> tuple[str, ...]:
    """The repository settings an agent loads from a worker's folder (policy)."""
    return {"claude": CLAUDE_SETTINGS, "codex": CODEX_SETTINGS}.get(agent, REPOSITORY_SETTINGS)


def settings_agent(names: tuple[str, ...]) -> str:
    """Whose settings these are, as a line names them."""
    return {CLAUDE_SETTINGS: "Claude", CODEX_SETTINGS: "Codex"}.get(names, "Copilot")


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


def mcp_called(off: dict[str, list[str]], tool: str) -> list[str]:
    """Of the servers off for a worker, the MCP server a tool is of (Copilot names an MCP
    server's tools "<server>-<tool>")."""
    return [
        server
        for server in off
        if server.startswith("mcp/")
        and "@" not in server
        and tool.startswith(server.partition("/")[2] + "-")
    ]


def slack_tool(spec: dict, call: Callable[[str, dict], Awaitable[Any]]) -> Tool:
    """One of Slack's read tools as Chief of Staff's own tool: called by Chief of Staff with the
    token it holds (Runtime.slack_read_tools)."""
    name = str(spec["name"])

    async def handle(invocation: Any) -> ToolResult:
        try:
            text = str(await call(name, dict(invocation.arguments or {})))
        except Exception as exc:
            said = f"Slack: {describe(exc)}"[:600]
            return ToolResult(text_result_for_llm=said, result_type="failure", error=said)
        return ToolResult(text_result_for_llm=text)

    return Tool(
        name=name,
        description=str(spec.get("description") or name),
        parameters=dict(spec.get("input_schema") or {"type": "object", "properties": {}}),
        handler=handle,
        skip_permission=True,
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
        # The one recovery path (recover): its task, what asks it for its next step now, and
        # what keeps its steps one at a time.
        self.recovery: asyncio.Task | None = None
        self.recover_soon = asyncio.Event()
        self.recover_lock = asyncio.Lock()
        # Resuming the sessions that were at work as Copilot connected (resume_sessions), and
        # each of those still to be resumed: set once it is.
        self.resume_job: asyncio.Task | None = None
        self.resumes: dict[str, asyncio.Event] = {}
        # Copilot sessions its connection died after (Runtime.disconnect): how many times.
        self.killed_by: dict[str, int] = {}
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
        # Other agents installed here (agents_found), and the first-run cards for those
        # installed and signed in that the owner was never asked about (offer_agents).
        self.found_cache: tuple[float, list[str]] = (float("-inf"), [])
        self.offers: dict[str, dict] = {}
        self.offering: asyncio.Task | None = None
        # Each agent's last glance (its version and sign-in, or why it cannot run), and when:
        # what the @ picker and Settings say of it (agent_status).
        self.glanced: dict[str, tuple[float, dict | str]] = {}
        # The agents seen able to run since they last could not (agent_up: what waited on one
        # goes on), and the words a worker's turn last lost its agent's sign-in with.
        self.ready_seen: set[str] = set()
        self.down_seen: set[str] = set()
        self.lost_sign_in: dict[str, str] = {}
        # Each agent's look running now (one at a time), and how many failed in a row for a
        # reason other than its sign-in or install (refresh_agent_status: looked at later).
        self.glancing: dict[str, asyncio.Task] = {}
        self.glance_failures: dict[str, int] = {}
        # Each agent's wait for its plan limit's reset (arm_limit).
        self.limit_waits: dict[str, asyncio.Task] = {}
        # Held work's cards resuming now (resume_held: once each); roles whose wait for the
        # owner ended, what they were sent meanwhile to go to them (role_waits).
        self.resuming: set[str] = set()
        self.roles_unwaited: set[str] = set()
        # Sign-ins and installs the owner started from the page (agentsetup.py).
        self.setups = agentsetup.Setups(self)
        # Claude's Agent SDK, set up by Chief of Staff itself while Claude is in use.
        self.claude_setup = claude_setup.ClaudeSetup(store, self.claude_in_use)
        self.claude_setup.changed = self.claude_setup_changed
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
        # When each agent's check last ran (recheck_agent), how many in a row could not run,
        # and its wait to try it again (check_later); and when each worker's own process last
        # stopped by itself (worker_lost).
        self.agent_checked_at: dict[str, float] = {}
        self.check_tries: dict[str, int] = {}
        self.check_waits: dict[str, asyncio.Task] = {}
        # One Continue with… of a worker at a time, and one answer to an agent refusal's card
        # at a time (a double click, a stale card, two tabs: the second finds it done).
        self.continue_locks: dict[str, asyncio.Lock] = {}
        self.refusal_locks: dict[str, asyncio.Lock] = {}
        self.lost_at: dict[str, float] = {}
        # Each agent's processes as last recorded (kv `agentpids:<agent>`).
        self.agent_pids: dict[str, dict[int, str]] = {}
        # Other agents' usage: the run each turn's usage counts for, and the workers being
        # stopped at their limit.
        self.turn_runs: dict[tuple[str, str], str] = {}
        self.spend_stopping: set[str] = set()
        # Why another agent last failed to start for a new worker (agent_unavailable).
        self.agent_failed: dict[str, str] = {}
        # One placement of each of Chief of Staff's own sessions at a time (place_role), and
        # the agent each was put on as last saved (agents_saved: a new choice is tried at once).
        self.role_locks: dict[str, asyncio.Lock] = {}
        # Slack's read tools as made for the read tools last discovered (slack_read_tools).
        self.slack_tools_made: list[Any] = [(), []]
        self.role_glanced: dict[str, float] = {}
        self.roles_seen: dict[str, str] = agents.roles(store) if self.agents else {}
        # Workers whose dropped schedules are being deleted in Claude now.
        self.deleting_schedules: set[str] = set()
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
        # Why Copilot can't run while Chief of Staff runs on another agent ("" while Copilot
        # runs, or while Chief of Staff itself waits for it): tried again by itself (recover).
        self.copilot_problem = ""
        # Connecting: Copilot's CLI starting (a failure then may be a newer one's), no agent
        # able to run Chief of Staff (it waits for one); why each other agent could not.
        self.copilot_starting = self.agent_waits = False
        self.hosts_failed: dict[str, str] = {}
        self.models_cache: tuple[float, list[dict]] = (0.0, [])
        # Copilot workers that wait for the owner to say to use their folder's changed agent
        # settings (settings_stop): not started from the queue meanwhile; and why, in words.
        self.settings_held: set[str] = set()
        self.settings_reasons: dict[str, str] = {}
        self.settings_orphans_closing: set[str] = set()
        # Settings first seen in an untracked folder found not to be the owner's as they are
        # (settings_proven): {where: their digest then}.
        self.settings_tried: dict[str, str] = {}
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
        # Those of them the Router was last given (terminals_note); None before its first look.
        self.terminals_given: set[str] | None = None
        # The owner's latest sends from the page, by session and the page's id for each:
        # when it came, and what it stored (owner_send_once).
        self.client_sends: dict[tuple[str, str], tuple[float, asyncio.Future]] = {}
        # What workers open in a terminal are doing, read from their logs (Workers.mirror).
        self.terminal: dict[str, dict] = {}
        self.mirror_job: asyncio.Task | None = None
        self.project_cache: dict[str, tuple[float, dict]] = {}
        self.rotate_lock = asyncio.Lock()
        self.work_root = store.directory / "work"
        self.workspaces = Workspaces(store, self.work_root)
        self.workspaces.guarding = self.sandbox_for
        workspaces_module.GIT_GUARD = weakref.WeakMethod(self.git_guard)
        workspaces_module.CLONE_REMADE = weakref.WeakMethod(self.clone_remade)
        self.workspaces.github_token = lambda repo: self.github.token_for(repo)
        self.tasks = TaskBook(store, on_change=self.task_changed)
        migrate_tasks(store)
        migrate_replacedby(store)  # (Before release_unguarded reads them: E126.)
        settle_adopted(store)
        settle_followups(store)
        settle_decision_changes(store)
        self.delivery = Delivery(self)
        # The parts, by responsibility; each keeps a reference to this runtime.
        self.events = Events(self)
        self.workers = Workers(self)
        self.task_actions = TaskActions(self)
        self.drafts = Drafts(self)
        # What it settled for the owner instead of asking (E123).
        self.handled = Handled(self)
        self.asks = Asks(self)
        self.asks.migrate_questions()
        self.accounts = Accounts(self)
        self.decisions = Decisions(self)
        self.peers = Peers(self)
        self.relations = Relations(self)
        self.teams = Teams(self)
        # What the owner sees of the work several workers do for one request (teams-v2.md).
        self.teams_lite = TeamsLite(self)
        # A busy worker's new task, given to a second worker with its context (forks.py).
        self.forks = Forks(self)
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
        # Share links (shares.py): nothing of them before the owner's first link.
        self.shares = Shares(self)
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
        # Prepared for you: keeping the owner's PRs moving, while the setting is on.
        self.shepherd = Shepherd(self)
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
        self.accounts.restore()
        agentsetup.kept_paths(self.store)  # Agents installed from here, found as before.
        if not getattr(self, "demo", False):
            # Claude's Agent SDK: as set up before, else set up now while Claude is in use.
            self.claude_setup.load()
            self.claude_setup.at_start()
        for name in agents.NAMES:
            self.arm_limit(name)  # A limit used up: its reset waited for again.
            self.arm_check(name)  # A check that could not run: tried again by itself.
        self.task_actions.settle_reports()
        await self.connect()
        self.after_update()
        # Before anything else resumes: a worker left in a shepherd step is not continued.
        await self.shepherd.start()
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
        self.task_actions.arm_scheduled()
        if self.store.settings().worker_sandbox and self.sandbox_host:
            self.seed_caches_soon()
        if self.store.drafts("uncertain"):
            self.background(self.drafts.check_uncertain())
        if self.shares.exists():
            # Links that expired while it was off are deleted; the timer set for the next.
            self.background(self.shares.resume())

    def tell_freed_roles(self):
        """Once, on Home: a session an earlier version kept on Copilot whatever the owner chose
        for it runs where they put it now (agents.migrate)."""
        if not (freed := self.store.get("rolesfreed")) or not self.store.get("router_id"):
            return
        with suppress(ValueError, TypeError):
            for role, agent in json.loads(freed):
                label = agents.ROLES.get(role, role)
                self.add_message(
                    self.router_id(),
                    "notice",
                    f"{label} now runs on {agents.display(agent)}, as you chose earlier.",
                )
        self.store.delete("rolesfreed")

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
        self.shares.stop()
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
        await self.claude_setup.stop()
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
        if freed := agents.migrate(self.store):
            # Chosen before for an agent an earlier version kept it off: said once, on Home.
            self.store.set("rolesfreed", json.dumps(freed))
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
            self.copilot_starting = self.agent_waits = False
            self.status = {"state": "starting", "error": ""}
            self.publish_state()
            try:
                if self.own_code_paths is None:
                    self.own_code_paths = await asyncio.to_thread(own_code)
                self.find_cache_facts()
                failed: Exception | None = None
                warning = ""
                if agents.copilot_on(self.store):
                    try:
                        warning = await self.start_copilot()
                    except Exception as exc:
                        failed = exc
                else:
                    failed = RuntimeError(COPILOT_OFF)
                if failed is not None:
                    # Copilot can't run (off, not installed, signed out, it would not start):
                    # Chief of Staff runs on another agent on that can, Copilot tried again by
                    # itself (recover); none can, it waits for one (recover too).
                    self.copilot_problem = self.copilot_why(failed)
                    if not await self.host_without_copilot():
                        self.agent_waits = True
                        raise self.no_agent(failed)
                    self.copilot_starting = False
                    await self.drop_copilot(failed)
                else:
                    self.copilot_problem = ""
                await self.ensure_router()
                self.tell_freed_roles()
                self.tell_first_agent()
                self.toolset.ensure_profile_card()
                # A worker whose start a stop interrupted starts again.
                self.store.execute(
                    "UPDATE sessions SET status='queued' WHERE status='starting' "
                    "AND closed_at IS NULL"
                )
                if self.backend is not None:
                    await self.copilot_resumes()
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
                if isinstance(exc, UnavailableError):
                    logger.error("Copilot did not start: %s", exc)  # (Said: no traceback.)
                else:
                    logger.exception("Copilot runtime failed to start")
                error = describe(exc)[:500]
                if self.copilot_starting:
                    # Copilot would not start at all: a newer Chief of Staff may run it.
                    error += " " + updates.start_hint(self.store)
                self.status = {"state": "error", "error": error}
                if self.agent_waits:
                    # No agent can run it: it goes on by itself once one can (recover);
                    # Home's card lists them, but for Copilot's own (as before) when it is
                    # the only one, and says so calmly while Claude is being set up (it starts
                    # once that is done: claude_setup_changed).
                    self.status |= {"waits": True, "setup": isinstance(exc, NoAgentError)}
                    if self.claude_setup.running():
                        self.status["settingup"] = True
                    if sys.platform == "win32":
                        self.status["windows"] = True  # (Copilot only, for now.)
                    self.recover_later()
                if self.backend and not self.backend.alive():
                    self.recover_later()  # Copilot stopped while connecting: again, later.
            self.publish_state()
        if self.ready():
            if self.copilot_wanted():
                self.recover_later()  # Copilot on but unable to run: tried again by itself.
            if self.backend is not None:
                # The one watching an earlier connection ends by itself.
                self.process_job = asyncio.create_task(
                    self.watch_process(self.backend, self.connections)
                )
            self.background(self.workers.pump())
            self.background(self.workers.refresh_elsewhere())
            self.background(self.github.signed_in())  # Which GitHub accounts workers can use.
            # Installed, signed in, never asked: a card (`offering`: until it has looked).
            self.offering = self.background(self.offer_agents())

    async def start_copilot(self) -> str:
        """Copilot's CLI started and signed in (`backend`): raises with why not. Returns a note
        for the owner on its version (version_warning)."""
        # Copilot CLIs started before that outlived their connection (orphaned when their
        # loader was killed, or this app stopped abruptly) hold sessions open. Only those still
        # this data directory's to stop: never an original's processes that a copy of its data
        # directory names (backend.copilot_orphans).
        records = self.recorded_processes()
        directory = self.store.directory
        if started := await asyncio.to_thread(copilot_orphans, records, directory):
            await asyncio.to_thread(lambda: end_processes(copilot_tree(started)))
        self.backend = self.backend_factory(self.store.settings())
        if isinstance(self.backend, CopilotBackend):
            self.backend.note = self.backend_note
            self.backend.refusals = directory / REFUSED_CLI
        self.copilot_starting = True
        await self.backend.start()
        self.copilot_starting = False
        auth = await self.backend.get_auth_status()
        # It answered: its processes run now. Remembered, so they never count as the owner's
        # terminal (even orphaned) and are stopped when they outlive it: with what tells them
        # from another instance's later (backend.copilot_records).
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
        return warning

    async def copilot_resumes(self):
        """Copilot runs (at start, or again): its sessions that were at work or mid-send are
        resumed in the background, each bounded, so that no session (nor Copilot hanging on
        one) holds the connection; it waits for them RESUME_WAIT seconds at most."""
        self.resumes = {session_id: asyncio.Event() for session_id in self.to_resume(COPILOT_AGENT)}
        self.resume_job = self.background(self.resume_sessions(bool(self.connections)))
        await asyncio.wait({self.resume_job}, timeout=RESUME_WAIT)
        resumed = self.resume_job
        if resumed.done() and not resumed.cancelled() and (error := resumed.exception()):
            raise error

    # Without Copilot (agents-anywhere.md 3): Chief of Staff runs on another agent while Copilot
    # is off or can't run, and waits for an agent (bootstrap) while none can.

    def copilot_down(self) -> str:
        """Why Copilot can't run Chief of Staff's work now ("" when it can, or while Chief of
        Staff, on Copilot alone, waits for it to connect)."""
        if not agents.copilot_on(self.store):
            return COPILOT_OFF
        return self.copilot_problem

    @staticmethod
    def copilot_why(failed: BaseException) -> str:
        return COPILOT_OFF if str(failed) == COPILOT_OFF else describe(failed)[:300]

    def first_run(self) -> bool:
        """A data directory Chief of Staff never ran in: no Router yet, no agent chosen."""
        return not self.store.get("router_id") and self.store.get(agents.CONFIG) is None

    async def first_agent(self) -> str:
        """A first run where Copilot can't run (not installed, signed out): the first other
        agent installed and signed in (Codex, then Claude) is turned on and runs everything,
        Copilot turned off (the owner may turn it on again); said once on Home. "" when none
        is ready."""
        if sys.platform == "win32":
            return ""  # (Codex and Claude need macOS or Linux for now.)
        for name in agents.NAMES:
            try:
                if name == "claude" and self.claude_setup.refused(fresh=True):
                    # Installed and signed in is enough: its Agent SDK is Chief of Staff's to set
                    # up (claude_setup.py), started as it is turned on here.
                    facts = await agents.glance(name, self.agent_folder(name), sdk=False)
                else:
                    facts = await self.glance_agent(name)
            except Exception:  # noqa: S112 (Not ready: the next one is looked at.)
                continue
            config = agents.stored(self.store)
            config[name] = {**(config.get(name) or {}), "enabled": True}
            config[agents.COPILOT] = {"enabled": False}
            self.store.set(agents.CONFIG, json.dumps(config))
            self.store.set(f"{agents.OFFERED}{name}", "first run")
            self.agents[name] = self.agent_connection(name)
            self.roles_seen = agents.roles(self.store)
            if name == "claude":
                self.claude_setup.turned_on()
            pays = agents.pays(name, facts.get("account", ""), facts.get("key_env", ""))
            self.store.set("firstagent", json.dumps([name, pays]))
            return name
        return ""

    def tell_first_agent(self):
        """Once, on Home: the agent a first run found Chief of Staff running on."""
        if not (found := self.store.get("firstagent")) or not self.store.get("router_id"):
            return
        self.store.delete("firstagent")
        with suppress(ValueError, TypeError):
            name, pays = json.loads(found)
            pays = f" ({pays[:1].lower()}{pays[1:]})" if pays else ""
            self.add_message(
                self.router_id(),
                "notice",
                f"Chief of Staff runs on {agents.display(name)}{pays}. Change it in Settings → "
                "Agents & models.",
            )

    async def host_without_copilot(self) -> str:
        """Copilot off or unable to run: the first other agent on (Codex, then Claude) that is
        signed in and starts, to run Chief of Staff's Router; "" when none can (why each can't:
        `hosts_failed`). A first run turns on the first one ready (first_agent)."""
        self.hosts_failed = {}
        if self.first_run() and not self.agents:
            await self.first_agent()
        for name in agents.NAMES:
            entry = self.agents.get(name)
            if entry is None or not entry.enabled():
                continue
            try:
                if name == "claude" and self.claude_setup.refused():
                    # Its Agent SDK set up first (as at a start, tried again at most once per
                    # version). Not waited for here, so the page opens at once and says so
                    # calmly; Chief of Staff starts on it once set up (claude_setup_changed).
                    if self.claude_setup.at_start():
                        raise RuntimeError(claude_setup.BEING_SET_UP)
                elif not entry.backend:
                    await self.glance_agent(name)  # Signed in (no model call), installed.
                await self.connect_agent(name)
                await self.recheck_agent(name)
            except Exception as exc:
                self.hosts_failed[name] = self.agent_start_failed(name, exc)
                continue
            if why := self.role_block(ROUTER, name):
                self.hosts_failed[name] = why
                continue
            return name
        return ""

    def no_agent(self, failed: Exception) -> Exception:
        """What Chief of Staff says when no agent can run it: Copilot's own error when it is
        the only one (as before), else why each can't (Home's card lists them, with what to
        do); it goes on by itself once one can (recover)."""
        if sys.platform == "win32" or (
            not self.agents and agents.copilot_on(self.store) and not self.first_run()
        ):
            return failed
        parts = [describe(failed).rstrip(".") + "."]
        parts += [why.rstrip(".") + "." for why in self.hosts_failed.values()]
        error = NoAgentError(" ".join(parts))
        error.__cause__ = failed
        return error

    async def drop_copilot(self, failed: BaseException):
        """Copilot can't run while Chief of Staff runs on another agent: its backend stops,
        why is kept for its row (`copilot_problem`), and it is tried again by itself
        (recover). Its sessions at work go on elsewhere (Chief of Staff's own) or fail with why
        (the owner's workers: Retry, or Continue with another agent)."""
        why = self.copilot_problem = self.copilot_why(failed)
        if self.backend is not None:
            with suppress(Exception):
                await self.disconnect()
        for key in self.at_work_on(COPILOT_AGENT):
            row = self.store.find_session(key)
            if not row or row["closed_at"]:
                continue
            if self.role_key(row) and self.role_failed(row, f"Copilot can't run: {why}"):
                continue
            self.fail(row, f"Could not go on: Copilot can't run ({why}).")

    def copilot_wanted(self) -> bool:
        """Chief of Staff runs on another agent while Copilot is on but can't run (signed out,
        not installed, can't start): tried again by itself (recover)."""
        return bool(self.copilot_problem) and agents.copilot_on(self.store) and self.ready()

    async def restart_copilot(self):
        """recover's step while Chief of Staff runs without Copilot that is on: Copilot started
        again once it may (installed, signed in or turned on since), its sessions resumed in
        the background (copilot_resumes), and what was chosen for it goes back to it
        (place_roles)."""
        if self.connecting and not self.connecting.done():
            return
        if not self.copilot_found():
            if self.copilot_problem == COPILOT_STARTING:
                command = self.store.settings().copilot_command or "copilot"
                self.copilot_problem = f"Copilot CLI '{command}' not found."
                self.publish_state()
            return  # (Not installed: looked at again, nothing started.)
        async with self.connect_lock:
            if not self.copilot_wanted():
                return
            try:
                await self.start_copilot()
            except Exception as exc:
                logger.info("Copilot still can't run: %s", describe(exc))
                self.copilot_problem = self.copilot_why(exc)
                if self.backend is not None:
                    with suppress(Exception):
                        await self.disconnect()
                self.publish_state()
                return
            self.copilot_problem = ""
            try:
                await self.copilot_resumes()
            except Exception:
                logger.exception("Recovering Copilot's sessions failed")
        if self.backend is not None:
            self.process_job = asyncio.create_task(
                self.watch_process(self.backend, self.connections)
            )
        self.publish_state()
        # What was chosen for Copilot goes back to it (what failed there while it could not run
        # is past); what waited for it starts.
        for role in agents.ROLES:
            self.clear_role_holds(role, COPILOT_AGENT)
        await self.place_roles()
        self.background(self.workers.pump())
        self.background(self.workers.refresh_elsewhere())

    async def copilot_switched(self) -> bool:
        """Copilot's Use switch saved: turned off, its workers at work stop (their tasks wait
        under Needs you, as for any agent turned off) and its process ends, Chief of Staff's own
        sessions going on elsewhere (place_roles); turned on, it starts now (recover_now).
        Whether it was switched."""
        on = agents.copilot_on(self.store)
        if not on and self.copilot_problem != COPILOT_OFF and self.status["state"] == "ready":
            for key in self.at_work_on(COPILOT_AGENT):
                row = self.store.find_session(key)
                if row and not row["closed_at"] and not self.role_key(row):
                    with suppress(Exception):
                        await self.workers.interrupt(key, by="Settings")
            await self.drop_copilot(RuntimeError(COPILOT_OFF))
            return True
        if on and self.copilot_problem == COPILOT_OFF:
            # Started now, before what was chosen for it is placed (else each would say it
            # runs elsewhere "for now" while it starts).
            self.copilot_problem = COPILOT_STARTING
            await self.recover_now()
            return True
        return False

    def copilot_found(self) -> bool:
        """Whether Copilot's CLI is on this computer (Settings' command), as far as `which`
        tells; another backend (a test's, the demo's) unless its last start said not found."""
        if self.backend_factory is copilot_backend:
            return bool(shutil.which(self.store.settings().copilot_command or "copilot"))
        problem = self.copilot_problem
        return not problem or agents.status_of(COPILOT_AGENT, problem)["status"] != agents.MISSING

    async def agent_found(self) -> bool:
        """While no agent could run Chief of Staff: whether one may now. Copilot signed in on
        the CLI that runs (asked: no model call), or installed while none runs (not started, or
        stopped or stopped answering: a new one started, as connecting again does); another
        agent on (on a first run, any) whose glance finds it signed in."""
        if agents.copilot_on(self.store):
            backend = self.backend
            if backend is not None and backend.alive():
                with suppress(Exception):
                    if (await backend.get_auth_status()).isAuthenticated:
                        return True
            elif self.copilot_found():
                return True
        first = self.first_run()
        for name in agents.NAMES:
            entry = self.agents.get(name)
            if not first and (entry is None or not entry.enabled()):
                continue
            with suppress(Exception):
                await self.glance_agent(name)
                return True
        return False

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
        if self.resume_job and not self.resume_job.done():
            self.resume_job.cancel()
        for gate in self.resumes.values():
            gate.set()  # (What waits for one goes on: to a new connection.)
        if backend and (culprit := getattr(backend, "culprit", "")):
            await self.after_death(backend, culprit)
        mine = self.on_agent(COPILOT_AGENT, self.states.ids())
        sessions = self.states.take_handles(mine)
        # Their turns end with the connection: no idle event will come to free their slots.
        self.states.end_turns(mine)
        # (A dead connection lets go of none: not waited for long.)
        with suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(s.disconnect() for s in sessions), return_exceptions=True),
                DISCONNECT_WAIT,
            )
        if backend:
            with suppress(Exception):
                await backend.stop()

    async def after_death(self, backend: AgentBackend, copilot_id: str):
        """Copilot's connection died after this Copilot session was resumed or read whole
        (backend.suspect). Too large for Copilot to reopen here when its log is big
        (backend.BIG_LOG), or when it happened after it twice: never resumed again."""
        self.killed_by[copilot_id] = self.killed_by.get(copilot_id, 0) + 1
        size = await asyncio.to_thread(big_log, backend.session_log(copilot_id))
        if size or self.killed_by[copilot_id] >= 2:
            self.mark_too_large(copilot_id, size)

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

    def claude_in_use(self) -> bool:
        """Whether Claude is in use: on for workers, or one of Chief of Staff's own sessions put
        on it (what its Agent SDK is set up for: claude_setup.py)."""
        if getattr(self, "demo", False):
            return False
        return agents.enabled(self.store, "claude") or "claude" in agents.roles(self.store).values()

    def claude_setup_changed(self, done: bool | None):
        """Claude's setup started (None) or ended: its status is said; set up, Claude is looked
        at again at once, so its row turns Ready by itself and what waited on it goes on
        (agent_up)."""
        if done:
            self.glanced.pop("claude", None)
            self.glance_failures.pop("claude", None)
            self.agent_failed.pop("claude", None)  # (Its start failed for its setup: past.)
            if "claude" in self.agents_found():
                self.glance_soon("claude")
        if done is not None and self.status.get("waits"):
            # Chief of Staff waits for an agent (Claude, being set up, its only one): it starts
            # now, or says why not (Home's card; Claude's row in Settings has Retry).
            self.background(self.recover_now())
        self.publish_state()

    async def retry_claude_setup(self, name: str) -> dict:
        """The owner's Retry (or Set up) on Claude's row: its Agent SDK installed again, whatever
        was tried before; every agent's status, as the page shows them."""
        if name != "claude":
            raise LookupError(f"Nothing of {agents.display(name)} is set up by Chief of Staff.")
        if getattr(self, "demo", False):
            raise ValueError("Not in the demo: it has no coding agents of its own.")
        self.claude_setup.retry()
        self.publish_state()
        return {"agents_status": self.agents_status()}

    def private_folders(self) -> list[str]:
        """Chief of Staff's private folders for other agents' workers (a Claude worker's MCP
        configuration and prompt): no worker reads or changes them (policy.private_denial, and
        the sandbox's denied paths). Those of an agent on, or there from before; none at all with
        Copilot alone."""
        return [
            str(folder)
            for name in agents.NAMES
            if (folder := self.agent_folder(name) / "private").is_dir()
            or (self.agents and agents.enabled(self.store, name))
        ]

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
        if row and (why := self.agent_block(row)) and self.blocked_on(agent, row, rechecked=True):
            raise RuntimeError(why)  # (Not only its check that could not run: tried again.)
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
            if agent == "claude" and (why := await self.claude_setup.wait()):
                # Its Agent SDK being set up (waited for a little), or its setup failed.
                raise RuntimeError(why)
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
                backend.on_limit = lambda limit: self.plan_limit_seen(agent, limit)  # type: ignore[attr-defined]
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
        unrun = self.check_unrun(agent)
        if not isinstance(found, dict):
            self.agent_checked_at[agent] = time.monotonic()
            found = await backend.check(self.work_root / f"{agent}-check")
            self.agent_checked_at[agent] = time.monotonic()
            if found.get("ran"):
                self.store.set(key, json.dumps(found))
        backend.checked = found
        refused = {
            "version": version,
            "acting": getattr(backend, "acting_refused", ""),
            "ask_first": getattr(backend, "ask_first_refused", ""),
            "sandbox": getattr(backend, "sandbox_refused", SANDBOX_UNPROVEN),
            # Whether its check ran (one that could not is never final: check_later).
            "ran": bool(found.get("ran")),
            **({"unrun": str(found.get("unrun") or "")} if not found.get("ran") else {}),
        }
        if self.store.get(f"agentrefused:{agent}") != json.dumps(refused):
            self.store.set(f"agentrefused:{agent}", json.dumps(refused))
        if not found.get("ran"):
            self.check_tries[agent] = self.check_tries.get(agent, 0) + 1
            self.arm_check(agent)
        elif self.check_tries.pop(agent, 0) or unrun:
            # Its check ran at last: what waited for it goes on (check_passed).
            self.background(self.check_passed(agent))

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
                "sandbox": why, "ran": True,
            }  # fmt: skip
            self.store.set(f"agentrefused:{agent}", json.dumps(refused))

        self.background(record())

    async def recheck_agent(self, agent: str):
        """A check that could not run (its version's workers wait meanwhile) is tried again
        where a role or a worker wants its agent, and by itself (check_later): a minute after
        the last try at the earliest, later after each that could not run in a row
        (CHECK_AGAIN)."""
        entry = self.agents.get(agent)
        backend = entry.backend if entry else None
        checked = getattr(backend, "checked", None)
        if entry is None or backend is None or not isinstance(checked, dict) or checked.get("ran"):
            return
        if time.monotonic() < self.check_due(agent):
            return
        async with entry.lock:
            if entry.backend is backend and not (backend.checked or {}).get("ran"):  # type: ignore[attr-defined]
                with suppress(Exception):
                    await self.check_agent(agent, backend)

    def check_due(self, agent: str) -> float:
        """When an agent's check that could not run may be tried again (monotonic)."""
        tries = max(1, self.check_tries.get(agent, 1))
        wait = CHECK_AGAIN[min(tries, len(CHECK_AGAIN)) - 1]
        return self.agent_checked_at.get(agent, float("-inf")) + wait

    def check_unrun(self, agent: str) -> str:
        """Why an agent's check by Chief of Staff could not run, as its workers wait for it
        (from its backend while it runs, else as it was last found); "" when it ran, or was
        never tried."""
        entry = self.agents.get(agent)
        checked = getattr(entry.backend, "checked", None) if entry and entry.backend else None
        found = checked if isinstance(checked, dict) else self.agent_refused(agent)
        if found.get("ran"):
            return ""
        # (Recorded before a record said whether its check ran, as 0.1.53's: by its words.)
        for said in (found.get("unrun"), found.get("ask_first"), found.get("acting")):
            if isinstance(said, str) and (seen := agents.CHECK_UNRUN.match(said)):
                return seen.group(1)
        name = agents.display(agent)
        return f"Chief of Staff's check of {name} could not run" if "ran" in found else ""

    def check_wanted(self, agent: str) -> bool:
        """Whether a role or work held for it wants an agent (its check that could not run is
        tried again by itself meanwhile)."""
        if self.agents and agent in agents.roles(self.store).values():
            return True
        for raw in self.store.items("agentrefusal:").values():
            with suppress(ValueError):
                found = json.loads(raw)
                if found.get("agent") == agent and found.get("status") == agents.UNCHECKED:
                    return True
        return False

    def arm_check(self, agent: str):
        """An agent's check that could not run, tried again by itself while a role or held work
        wants it (one wait per agent; from startup too, its first try a minute on)."""
        if agent not in self.agents or not self.check_unrun(agent) or not self.check_wanted(agent):
            return
        if (waiting := self.check_waits.get(agent)) and not waiting.done():
            return
        self.agent_checked_at.setdefault(agent, time.monotonic())
        self.check_waits[agent] = self.background(self.check_later(agent))

    async def check_later(self, agent: str):
        while not self.stopping and self.check_unrun(agent) and self.check_wanted(agent):
            await asyncio.sleep(max(0.0, self.check_due(agent) - time.monotonic()))
            if self.stopping or not self.check_unrun(agent) or not self.check_wanted(agent):
                return
            if time.monotonic() < self.check_due(agent):
                continue  # Tried meanwhile (a worker's start, a role's).
            tried = self.agent_checked_at.get(agent)
            try:
                await self.check_again(agent)
            except Exception:
                logger.warning("Checking %s again failed.", agent, exc_info=True)
            if self.agent_checked_at.get(agent) == tried:
                self.check_tried(agent)  # Not tried (it can't start now): later.

    def check_tried(self, agent: str):
        self.agent_checked_at[agent] = time.monotonic()
        self.check_tries[agent] = self.check_tries.get(agent, 0) + 1

    async def check_again(self, agent: str) -> str:
        """An agent's check that could not run, tried again (when due: recheck_agent), its
        agent started for it first (where it does not run, its start checks it): why it could
        not start, else ""."""
        entry = self.agents.get(agent)
        if entry is None or not entry.enabled() or sys.platform == "win32":
            return ""
        try:
            await self.connect_agent(agent)
        except Exception as exc:
            self.check_tried(agent)
            return self.agent_start_failed(agent, exc)
        self.agent_failed.pop(agent, None)
        await self.recheck_agent(agent)
        return ""

    async def check_passed(self, agent: str):
        """An agent's check that could not run has: the work held for it goes on (resume_held,
        once), and a role of Chief of Staff's put on it moves there (place_role: at once at
        rest, else at its turn's end)."""
        await self.agent_back(agent, frozenset({agents.UNCHECKED}))

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
        if (
            agents.limit(self.store, agent)
            and not self.role_key(row)
            and self.store.agent_unknown(worker)
        ):
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
        return f"{agents.display(agent)} is turned off in Settings → Agents & models."

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

    def blocked_on(self, agent: str, row: dict, rechecked: bool = False) -> str:
        """Why this worker (or one about to be made) may not run on that agent now; `rechecked`:
        but for its check that could not run (tried again first)."""
        name = agents.display(agent)
        if sys.platform == "win32":
            return self.not_on_windows(agent)
        entry = self.agents.get(agent)
        if entry is None and agent not in agents.NAMES:
            return f"This session runs on {agent}, which is not available."
        if entry is None or not entry.enabled():
            return self.turned_off(agent)
        role = self.role_key(row)
        if role in PLACED_ROLES:
            return self.role_block(role, agent, rechecked)
        if role and (why := self.role_block(role, agent, rechecked)):
            return why
        if row["role"] != WORKER and not role:
            return f"Only your own workers run on {name}."
        unrun = rechecked and self.check_unrun(agent)
        refused = {"sandbox": ""} if unrun else self.agent_refused(agent)
        sandbox = refused.get("sandbox", SANDBOX_UNPROVEN)
        # A worker that reads Slack runs on any agent (agents-anywhere.md 4): sandboxed where
        # its agent's sandbox passed Chief of Staff's checks (Copilot's, Claude's), asking first
        # unless set otherwise, Slack read through Chief of Staff (open_live: read_proxy).
        if self.store.settings().worker_sandbox and self.sandbox_host and sandbox:
            return (
                f"{name} can't run while workers are sandboxed: {sandbox}. Turn the sandbox off "
                "in Settings → General to go on."
            )
        if (
            agent == "claude"
            and self.sandbox_host
            and (self.store.settings().worker_sandbox or row["slack"])
        ):
            # Its sandbox as Chief of Staff's, not widened by Claude's own settings.
            from .backend_claude import sandbox_widened

            cwd, slack = str(row.get("cwd") or ""), bool(row["slack"])
            if why := sandbox_widened(cwd, local=not slack, restricted=slack):
                return why
        if why := refused.get("acting"):
            return why
        if not self.yolo(row) and (why := refused.get("ask_first")):
            return why
        if (limit := agents.limit(self.store, agent)) and not role:
            if self.store.agent_unknown(row["id"]):
                return (
                    f"{name} stopped in the middle of a turn, so what this worker spent then is "
                    f"not known and its token limit can't be kept: {self.spend_place(row)}"
                )
            if (spent := self.store.agent_spent(row["id"])) >= limit:
                return f"This worker used its {name} token limit ({spent:,} of {limit:,})."
        return ""

    # Chief of Staff's own sessions on another agent (agent-roles.md): the Router and the
    # Watchdog run where Settings → Agents & models puts them; on Copilot while that agent can't
    # run them (said in one line), and back by themselves once it can.

    def role_key(self, row: dict) -> str:
        """Which of Chief of Staff's own sessions a session is (agents.ROLES; "" for the
        owner's workers)."""
        if row["role"] in {ROUTER, WATCHDOG, SLACK}:
            return row["role"]
        if row["role"] != WORKER or not row.get("id"):
            return ""
        kind = agents.kind_of(WORKER, self.special(row))
        return kind if kind in agents.ROLES else ""

    def role_row(self, role: str) -> dict | None:
        """The open session of a role Runtime.place_role places (none yet: made on first use)."""
        if role == ROUTER:
            router = self.store.get("router_id")
            row = self.store.find_session(router) if router else None
        else:
            rows = self.store.open_sessions(role)
            row = rows[0] if rows else None
        return row if row and not row["closed_at"] else None

    def role_held(self, role: str, agent: str) -> dict:
        """Why that agent last failed a role, until when it waits before trying it again (one
        record per role and agent: kv `roleheld:<role>:<agent>`; 0.1.57's `roleheld:<role>` read
        for the agent it names)."""
        for key in (f"roleheld:{role}:{agent}", f"roleheld:{role}"):
            try:
                held = json.loads(self.store.get(key) or "{}")
            except ValueError:
                continue
            if isinstance(held, dict) and held and held.get("agent", agent) == agent:
                return held
        return {}

    def clear_role_holds(self, role: str, agent: str = ""):
        """A role's waits for its agents (for `agent` only, when named) over: tried now."""
        for name in [agent] if agent else agents.AGENTS:
            self.store.delete(f"roleheld:{role}:{name}")
        legacy = self.role_held(role, agent) if agent else {"agent": ""}
        if legacy and self.store.get(f"roleheld:{role}"):
            self.store.delete(f"roleheld:{role}")

    def role_block(self, role: str, agent: str, rechecked: bool = False) -> str:
        """Why a role can't run on that agent now ("" when it can, as far as is known before it
        starts there); `rechecked`: but for its check that could not run (tried again first)."""
        name = agents.display(agent)
        held = self.role_held(role, agent)
        if agent == COPILOT_AGENT:
            if why := self.copilot_down():
                return why
            if held and time.time() < float(held.get("until") or 0):
                return str(held.get("why") or f"{name} could not run it.")
            return ""
        if sys.platform == "win32":
            return f"{name} needs macOS or Linux for now."
        entry = self.agents.get(agent)
        if entry is None or not entry.enabled():
            return self.turned_off(agent)
        if not shutil.which(agents.command(agent)):
            return f"{name} is not found on this computer: install {agents.INSTALL[agent]}."
        if entry.offline:
            return f"{name} stopped and could not start again."
        if (why := self.agent_failed.get(agent)) and not (entry.backend and entry.backend.alive()):
            return why
        if unrun := self.check_unrun(agent):
            if not rechecked:
                return f"{unrun}. It's tried again by itself."
        else:
            refused = self.agent_refused(agent)
            if (
                (why := refused.get("acting"))
                and callable(limited := getattr(entry.backend, "limit_refusal", None))
                and why == limited()
            ):
                why = agents.limit_line(getattr(entry.backend, "limit", {}), name=agent)
            if why := why or refused.get("ask_first"):
                return why
        if held and time.time() < float(held.get("until") or 0):
            return str(held.get("why") or f"{name} could not run it.")
        limit = agents.live_limit(self.store, agent, getattr(entry.backend, "limit", None))
        resets = limit.get("resets_at")
        if agents.blocked(limit) and isinstance(resets, int | float) and resets > time.time():
            return agents.limit_line(limit, name=agent)
        return ""

    def role_target(self, role: str, skip: Iterable[str] = ()) -> tuple[str, str]:
        """Where a role runs now: the agent chosen for it, else the first other agent on that
        can run it, in the one order (agents.fallbacks: Copilot, Codex, Claude), with why the
        chosen one can't; ("", why) when none can. `skip`: agents this placement tried."""
        wanted = agents.role_agent(self.store, role)
        skipped = set(skip)
        why = self.role_block(role, wanted) or (
            f"{agents.display(wanted)} could not run it." if wanted in skipped else ""
        )
        if not why:
            return wanted, ""
        for agent in agents.fallbacks(self.store, wanted):
            if agent not in skipped and not self.role_block(role, agent):
                return agent, why
        return "", why

    def role_line(self, role: str) -> str:
        """Settings' line under a role's choice: where it runs for now and why, or that it
        waits ("" when it runs where it was put)."""
        wanted = agents.role_agent(self.store, role)
        if not self.agents and wanted == COPILOT_AGENT:
            return ""
        target, why = self.role_target(role)
        if not why:
            return ""
        if not target or self.role_wait(role):
            return f"Waits for {agents.display(wanted)}: {plain_why(why)}"
        return f"On {agents.display(target)} for now: {plain_why(why)}"

    def agent_start_failed(self, agent: str, error: BaseException) -> str:
        why = f"{agents.display(agent)} could not start: {describe(error)}"
        if agent != COPILOT_AGENT:
            self.agent_failed[agent] = why
            self.agent_down(agent)
        return why

    async def place_role(self, role: str):
        """Put a role's session where it runs now (role_target), its agent started first: one
        that can't start leaves it on the next agent that can, saying why; none can, it waits
        (one card). Switched at once when it is at rest (its agent stopped under it, too), else
        at its turn's end (Events.idle)."""
        if not self.agents or role not in PLACED_ROLES:
            return
        async with self.role_locks.setdefault(role, asyncio.Lock()):
            if (row := self.role_row(role)) is None:
                return
            fell = self.store.get(f"rolefallback:{role}")
            target, why = await self.role_start(role)
            row = self.store.session(row["id"])
            waited = bool(self.role_wait(role))
            try:
                if await self.role_waits(role, row, target, why):
                    changed = not waited
                else:
                    changed = await self.put_role(role, row, target, why, fell) or waited
            finally:
                if role in self.roles_unwaited:
                    # What waited for it goes to it now, where it runs.
                    self.roles_unwaited.discard(role)
                    self.delivery.schedule_flush(row["id"])
        if changed:
            self.publish_state()

    async def put_role(self, role: str, row: dict, target: str, why: str, fell: str | None) -> bool:
        """place_role's switch, its role's lock held: whether it switched. One that fails tries
        the next agent that can (each once); none left, the role waits."""
        if target == self.agent(row["id"]):
            if why and why != fell:
                # Chosen for an agent that can't run it now: said once, where it runs.
                label = agents.ROLES[role]
                self.add_message(
                    row["id"],
                    "notice",
                    f"{label} runs on {agents.display(target)} for now: {plain_why(why)}",
                )
            return False
        busy = row["status"] == "working" or self.states[row["id"]].busy
        if busy and self.states.handle(row["id"]):
            return False  # At its turn's end.
        wanted = agents.role_agent(self.store, role)
        tried: set[str] = set()
        while target:
            try:
                await self.switch_role(row, role, target, "fallback" if why else "", why)
                return True
            except Exception as exc:
                tried.add(target)
                failed = self.agent_start_failed(target, exc)
                logger.warning("The %s could not open on %s.", role, target, exc_info=True)
                self.hold_role(role, target, failed)
                why = why or failed if target != wanted else failed
                self.store.set(f"rolefallback:{role}", why)
                target, _ = self.role_target(role, skip=tried)
                if target == self.agent(row["id"]) and not self.role_block(role, target):
                    return True  # It stays where it runs.
                row = self.store.session(row["id"])
        await self.role_waits(role, row, "", why)
        return True

    async def role_waits(self, role: str, row: dict, target: str, why: str) -> bool:
        """Whether a role waits now, its messages kept: no agent on can run it, or its agent
        can't and the owner chose to be asked (Settings: Wait, and ask me) and has not said Use
        <another> until then. One card in the Router's chat says why, with Sign in (or Install),
        and Use <another> until then where one can run it. Once its agent can run it again, or
        another can and they said so, it goes on with what was kept."""
        wanted = agents.role_agent(self.store, role)
        waiting = self.role_wait(role)
        label = agents.ROLES[role]
        if not why:
            self.store.delete(f"roleok:{role}")
            if waiting:
                self.role_waited(role, waiting, f"Runs on {agents.display(wanted)} now")
            return False
        asked = agents.role_unavailable(self.store, role) == agents.ASK and (
            self.store.get(f"roleok:{role}") != wanted
        )
        if target and not asked:
            if waiting:
                self.role_waited(role, waiting, f"On {agents.display(target)} until then")
            return False
        if waiting:
            if waiting.get("instead", "") != target:
                # Another agent can run it meanwhile now (or none can any more): its card says.
                self.refusal_said(waiting, {"instead": target})
                self.store.set(f"rolewait:{role}", json.dumps(waiting | {"instead": target}))
            return True
        found = self.unready(wanted, why) if wanted != COPILOT_AGENT else {"status": "problem"}
        name = agents.display(wanted)
        if found["status"] == agents.SIGN_IN:
            needs = f"{name} needs you to sign in"
        elif found["status"] == agents.MISSING:
            needs = f"{name} isn't installed"
        elif found["status"] == agents.UNCHECKED:
            needs = f"{self.check_unrun(wanted)}; it's tried again by itself"
        else:
            needs = f"{name} can't run it: {plain_why(why)}"
        router = self.store.get("router_id")
        card = self.add_message(
            router or row["id"],
            "notice",
            f"{label} waits for {name}. {needs}. What it is sent is kept until then.",
            {
                "role_wait": role,
                "agent": wanted,
                "status": found["status"],
                **({"instead": target} if target else {}),
                **({"fix": found["fix"]} if found.get("fix") else {}),
            },
        )
        self.store.set(
            f"rolewait:{role}",
            json.dumps({"agent": wanted, "why": why, "card": card["id"], "instead": target}),
        )
        return True

    def role_wait(self, role: str) -> dict:
        try:
            found = json.loads(self.store.get(f"rolewait:{role}") or "{}")
        except ValueError:
            return {}
        return found if isinstance(found, dict) else {}

    def role_waited(self, role: str, waiting: dict, said: str):
        """A role's wait over: its card says how, and what it was sent goes to it."""
        self.store.delete(f"rolewait:{role}")
        self.refusal_said(waiting, {"resumed": said})
        self.roles_unwaited.add(role)

    def role_waiting(self, row: dict) -> bool:
        """Whether nothing is sent to this session now: its role waits for the owner."""
        return row["role"] in PLACED_ROLES and bool(self.store.get(f"rolewait:{row['role']}"))

    async def answer_role_wait(self, role: str):
        """Use <another> until then, on a waiting role's card: it goes on there, with what was
        kept, and back on its agent once that can run it."""
        if role not in PLACED_ROLES or not self.role_wait(role):
            raise ValueError("That card was answered already.")
        self.store.set(f"roleok:{role}", agents.role_agent(self.store, role))
        await self.place_role(role)

    async def role_start(self, role: str) -> tuple[str, str]:
        """Where a role runs now (role_target), its agent started: going through the agents in
        the one order once (each tried at most once: `tried`), one that can't start is held
        (hold_role) and the next tried; why it runs elsewhere is kept for Settings' line. Its
        agent's check that could not run is tried again first (when due: never final)."""
        wanted = agents.role_agent(self.store, role)
        if (
            wanted != COPILOT_AGENT
            and self.check_unrun(wanted)
            and not self.role_block(role, wanted, rechecked=True)
            and (failed := await self.check_again(wanted))
        ):
            self.hold_role(role, wanted, failed)
        target, why = self.role_target(role)
        tried: set[str] = set()
        while target and target != COPILOT_AGENT and target not in tried:
            tried.add(target)
            try:
                await self.connect_agent(target)
                await self.recheck_agent(target)
            except Exception as exc:
                self.hold_role(role, target, self.agent_start_failed(target, exc))
            else:
                self.agent_failed.pop(target, None)
                if not self.role_block(role, target):  # What its check found.
                    break
            target, why = self.role_target(role, skip=tried)
        if why:
            self.store.set(f"rolefallback:{role}", why)
            if wanted != COPILOT_AGENT:
                self.arm_check(wanted)
        elif self.store.get(f"rolefallback:{role}") is not None:
            self.store.delete(f"rolefallback:{role}")
        return target, why

    async def helper_agent(self, role: str) -> str:
        """The agent a helper's next run starts on (Watch for you, the helpers): the one chosen
        for its kind once it can run it, else the next that can (helpers never wait: they run
        now and then), else the one chosen (its run then fails with why, and is tried again)."""
        if not self.agents:
            return agents.default_agent(self.store)
        async with self.role_locks.setdefault(role, asyncio.Lock()):
            target, _ = await self.role_start(role)
        return target or agents.role_agent(self.store, role)

    async def switch_role(self, row: dict, role: str, target: str, kind: str, why: str):
        """A role's session on another agent (or Copilot): a fresh one there takes over, the
        Router's with a summary of what it knew (handover); its timeline stays one, and the one
        before stays in its agent's own history. What a turn cut short was given goes to it."""
        session_id = row["id"]
        state = self.states[session_id]
        locks = [self.rotate_lock] if role == ROUTER else []
        async with AsyncExitStack() as stack:
            for lock in [*locks, state.lifecycle, state.sending]:
                await stack.enter_async_context(lock)
            row = self.store.session(session_id)
            cut = row["status"] == "working"
            before, old = self.agent(session_id), self.copilot_id(session_id)
            self.store.set(f"alias:{old}", session_id)
            if before == COPILOT_AGENT and self.store.get(f"created:{session_id}"):
                # Its Copilot session, for an earlier version to go on with (prepare_downgrade).
                self.store.set(f"rolecopilot:{session_id}", old)
            await self.drop_handle(session_id)
            if before != COPILOT_AGENT:
                self.spending(session_id, False)
            self.set_agent(session_id, target)
            fresh = str(uuid4())
            self.store.set(f"alias:{fresh}", session_id)
            try:
                session = await self.open_live(row, create=True, copilot=fresh)
            except BaseException:
                self.store.delete(f"alias:{fresh}")
                self.set_agent(session_id, before)
                raise
            self.store.set(f"copilot:{session_id}", session.session_id)
            for key in ("agentmodel", "rotated", "rotate_after"):
                self.store.delete(f"{key}:{session_id}")
            self.store.set(f"context:{session_id}", "0")
            if role == ROUTER:
                was = agents.display(before)
                self.store.set(f"preamble:{session_id}", self.handover(f"on {was}"))
            if cut and (sent := self.store.get(f"prevsent:{session_id}")) is not None:
                self.store.set(f"sent:{session_id}", sent)  # Its turn was cut short.
            if cut:
                self.update_session(session_id, status="idle", activity="")
        label, name = self.role_label(role, row), agents.display(target)
        summary = ", with a summary of what it knew" if role == ROUTER else ""
        if kind == "fallback":
            text = f"{label} runs on {name} for now: {plain_why(why)}"
        elif kind == "renew":
            text = (
                f"{label}'s {name} session could not be resumed ({why.strip()[:160]}): a fresh "
                f"one took over{summary}."
            )
        else:
            text = f"{label} runs on {name} now{summary}."
        self.add_message(session_id, "notice", text)
        if self.delivery.has_unsent(session_id):
            self.delivery.schedule_flush(session_id, 0)

    def set_agent(self, session_id: str, agent: str):
        """The agent a session runs on from now (Copilot's: no entry)."""
        if agent == COPILOT_AGENT:
            self.store.delete(f"agent:{session_id}")
        else:
            self.store.set(f"agent:{session_id}", agent)

    async def open_role(self, role: str):
        """A role's session placed and opened ahead of its first message."""
        try:
            await self.place_role(role)
            if row := self.role_row(role):
                await self.session_for(row["id"])
        except Exception:
            logger.warning("The %s could not open.", role, exc_info=True)

    async def place_roles(self):
        for role in PLACED_ROLES:
            try:
                await self.place_role(role)
            except Exception:
                logger.warning("Could not place the %s.", role, exc_info=True)

    def role_label(self, role: str, row: dict) -> str:
        """How a role's notices name it ("The Router"; a helper by its title)."""
        if role == "helpers":
            return f"“{clean_title(row['title'])}”"
        return agents.ROLES.get(role, role)

    def role_failed(self, row: dict, why: str, until: float = 0.0, kind: str = "lost") -> bool:
        """A turn of one of Chief of Staff's own sessions failed for what keeps its agent from
        running it (its plan's limit, its sign-in, its process), on any agent: that agent is
        held for the role, and it goes on with the next agent that can (agents-anywhere.md
        2.3), given again what that turn was given; back once the chosen one can (roles_check).
        A helper keeps its job: its run goes on there. False when no other agent can run it
        now (it fails as any session does), and for the owner's workers."""
        role, agent = self.role_key(row), self.agent(row["id"])
        if not role:
            return False
        others = [a for a in [agents.role_agent(self.store, role), *agents.fallbacks(
            self.store, agent)] if a != agent and not self.role_block(role, a)]  # fmt: skip
        if agent == COPILOT_AGENT and not others:
            return False  # Copilot's own recovery (reconnect) is its only way on.
        if role not in PLACED_ROLES and not others:
            return False
        self.hold_role(role, agent, why, until, kind)
        self.states[row["id"]].busy = False
        row = self.store.session(row["id"])
        if row["status"] == "working":
            # Its turn ended there: what it was given goes to it where it runs next.
            if (sent := self.store.get(f"prevsent:{row['id']}")) is not None:
                self.store.set(f"sent:{row['id']}", sent)
            self.update_session(row["id"], status="idle", activity="")
        if role in PLACED_ROLES:
            self.background(self.place_role(role))
        else:
            self.background(self.move_helper(row["id"], role, why))
        return True

    async def move_helper(self, session_id: str, role: str, why: str):
        """A helper whose agent failed its run goes on with the next agent that can, its job
        (its task, what its turn was given) kept."""
        async with self.role_locks.setdefault(role, asyncio.Lock()):
            target, _ = await self.role_start(role)
            row = self.store.session(session_id)
            if not target or target == self.agent(session_id) or row["closed_at"]:
                return
            try:
                await self.switch_role(row, role, target, "fallback", why)
            except Exception:
                logger.warning("Could not move %s to %s.", session_id, target, exc_info=True)

    def hold_role(self, role: str, agent: str, why: str, until: float = 0.0, kind: str = "start"):
        """That agent failed a role: it is tried again for it after `until`, else after a wait
        that doubles with each failure in a row (up to ROLE_RETRY_MOST; afresh after that long).
        One record per role and agent (kv `roleheld:<role>:<agent>`), kept across a restart.
        `kind`: "start" (it could not start: signed out, not installed; glanced at meanwhile),
        "limit" (its plan's), "lost" (its process, its session)."""
        held = self.role_held(role, agent)
        tries = 1
        if held and time.time() - float(held.get("until") or 0) < ROLE_RETRY_MOST:
            tries = int(held.get("tries") or 0) + 1
        wait = min(ROLE_RETRY * 2 ** (tries - 1), ROLE_RETRY_MOST)
        record = {
            "agent": agent,
            "why": why,
            "until": until or time.time() + wait,
            "tries": tries,
            "kind": kind,
        }
        self.store.set(f"roleheld:{role}:{agent}", json.dumps(record))

    def role_turn_failed(self, row: dict, message: str, kind: str, limit: Any) -> bool:
        """A role's turn its agent refused for a plan's limit or a sign-in: role_failed."""
        agent = self.agent(row["id"])
        if kind == "plan_limit":
            limit = dict(limit) if isinstance(limit, dict) else {}
            resets = limit.get("resets_at")
            until = resets if isinstance(resets, int | float) and resets > time.time() else 0
            line = agents.limit_line(limit, name=agent) or (
                f"{agents.display(agent)} refused it for a limit of its plan or account."
            )
            return self.role_failed(row, line, until or time.time() + ROLE_LIMIT_WAIT, "limit")
        if SIGNED_OUT.search(message):
            why = f"{agents.display(agent)} is signed out: {message[:200]}"
            return self.role_failed(row, why, kind="start")
        return False

    async def roles_check(self):
        """Chief of Staff's own sessions go back to the agent chosen for them once it can run
        them (its wait over, it is started: signed in again, installed, its limit reset), and a
        role that waits (no agent could run it) is placed again once one can: also one whose
        session is recorded on its chosen agent, stranded while that could not run."""
        if not self.agents:
            return
        for role in PLACED_ROLES:
            wanted = agents.role_agent(self.store, role)
            row = self.role_row(role)
            if row is None:
                continue
            waits = bool(self.role_wait(role))
            if self.agent(row["id"]) == wanted and not waits:
                continue
            entry = self.agents.get(wanted)
            if (wanted != COPILOT_AGENT and (entry is None or not entry.enabled())) or (
                wanted == COPILOT_AGENT and self.copilot_down()
            ):
                if waits:
                    await self.place_again(role)  # Another may run it now.
                continue
            held = self.role_held(role, wanted)
            if time.time() < float(held.get("until") or 0):
                # Its wait goes on; one that could not start is glanced at meanwhile (signed in
                # again, installed: tried at once).
                if held.get("kind", "start") != "start" or wanted == COPILOT_AGENT:
                    if waits:
                        await self.place_again(role)
                    continue
                if time.monotonic() - self.role_glanced.get(role, float("-inf")) < ROLE_GLANCE:
                    continue
                self.role_glanced[role] = time.monotonic()
                try:
                    await self.glance_agent(wanted)
                except Exception as exc:
                    logger.debug("%s still can't run the %s: %s", wanted, role, describe(exc))
                    if waits:
                        await self.place_again(role)
                    continue
            if wanted != COPILOT_AGENT and entry is not None:
                try:
                    await self.connect_agent(wanted)
                except Exception as exc:
                    self.hold_role(role, wanted, self.agent_start_failed(wanted, exc))
                    if waits:
                        await self.place_again(role)
                    continue
                self.agent_failed.pop(wanted, None)
                entry.offline = False
            self.clear_role_holds(role, wanted)  # It runs: tried now.
            await self.place_again(role)

    async def place_again(self, role: str):
        try:
            await self.place_role(role)
        except Exception:
            logger.warning("Could not place the %s.", role, exc_info=True)

    def sandboxed_refusal(self, agent: str) -> str:
        """Why none of an agent's workers can start while workers are sandboxed, on its row in
        Settings → Agents & models ("" when they can: Claude's, on a version whose sandbox held, or
        not checked yet, where your Claude settings would not widen its sandbox)."""
        refused = self.agent_refused(agent)
        why = refused.get("sandbox", "" if agent == "claude" else SANDBOX_UNPROVEN)
        if why:
            return f"Can't run while workers are sandboxed: {why}."
        if agent == "claude":
            from .backend_claude import sandbox_widened

            return sandbox_widened("", trusted=False)
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

    async def new_worker_agent(self, named: str, slack: bool, picked: str = "") -> tuple[str, str]:
        """The agent a new worker of the owner's runs on, and what its timeline says of it: the
        one the owner named (refused with why when it cannot run it now: never moved to
        another), else the one the Router chose (`picked`) or Settings chose for new workers,
        or, with why, the next agent on that can run it (agents.fallbacks)."""
        chosen = (
            named or picked or (agents.new_workers(self.store) if self.agents else COPILOT_AGENT)
        )
        if chosen != COPILOT_AGENT and chosen not in agents.NAMES:
            raise ValueError(f"Chief of Staff does not run {chosen!r}.")
        row = {"id": "", "role": WORKER, "slack": slack, "yolo": "", "closed_at": None}
        if not (why := await self.worker_agent_why(chosen, row)):
            return chosen, ""
        if named:
            raise ValueError(why)
        for other in agents.fallbacks(self.store, chosen):
            if not await self.worker_agent_why(other, row):
                return other, f"Started on {agents.display(other)}: {why}"
        raise ValueError(why)

    async def worker_agent_why(self, agent: str, row: dict) -> str:
        """Why a new worker can't start on this agent now ("" when it can): its agent started
        for it (another's on first use)."""
        if agent == COPILOT_AGENT:
            return self.copilot_down()
        if said := await self.looked_again(agent):
            # Signed out (in a terminal, a turn that lost it) while its process runs: held.
            how = "run" if agent in self.lost_sign_in else "start"
            return f"{agents.display(agent)} could not {how}: {said}"
        if why := self.limit_held(agent) or self.blocked_on(agent, row, rechecked=True):
            return why
        try:
            await self.connect_agent(agent)
            await self.recheck_agent(agent)
        except Exception as exc:
            why = f"{agents.display(agent)} could not start: {describe(exc)}"
            self.agent_failed[agent] = why  # Said to the Router (agent_unavailable).
            self.agent_down(agent)
            return why
        self.agent_failed.pop(agent, None)
        self.agent_up(agent)
        return self.blocked_on(agent, row)  # What its version showed, now known.

    def agent_unavailable(self, name: str) -> str:
        """Why a new worker of the owner's could not run on this agent now ("" when it could,
        as far as is known before it starts)."""
        if name == COPILOT_AGENT:
            return self.copilot_down()
        entry = self.agents.get(name)
        if entry is not None and entry.offline:
            return f"{agents.display(name)} stopped and could not start again."
        backend, _ = self.connection(name)
        if (why := self.agent_failed.get(name)) and not (backend and backend.alive()):
            return why  # Its last start failed (signed out, say), and it has not run since.
        row = {"id": "", "role": WORKER, "slack": 0, "yolo": "", "closed_at": None, "cwd": ""}
        return self.blocked_on(name, row)

    def owner_words_now(self) -> str:
        """What the owner wrote the Router since its last turn ended (the messages its turn now
        answers; not those addressed to one worker)."""
        router = self.store.get("router_id")
        if not router:
            return ""
        since = int(self.store.get(f"idle:{router}") or 0)
        return "\n".join(
            message["text"]
            for message in self.store.messages(router, after=since, limit=20)
            if message["kind"] == "owner" and not (message["meta"] or {}).get("to")
        )

    def chosen_agents(self) -> list[str]:
        """The agents the owner chose in the composer for what the Router does now: one
        (@codex), or several to compare (@codex @claude); those on only."""
        router = self.router_id()
        since = int(self.store.get(f"idle:{router}") or 0)
        on = {COPILOT_AGENT, *self.agents_on()}
        for message in reversed(self.store.messages(router, after=since, limit=20)):
            meta = message["meta"] or {}
            if message["kind"] == "owner" and (meta.get("agents") or meta.get("agent")):
                chosen = meta.get("agents") or [meta["agent"]]
                return [name for name in chosen if name in on]
        return []

    def chosen_agent(self) -> str:
        """The agent the owner chose in the composer (@codex) for what the Router does now:
        on the owner's message since its last turn, if it is on (none for several: Compare)."""
        chosen = self.chosen_agents()
        return chosen[0] if len(chosen) == 1 else ""

    def chip_model(self, agent: str) -> dict:
        """The model and effort the owner chose with that agent in the composer (its chip's
        popover), for the Router's workers on it now."""
        router = self.router_id()
        since = int(self.store.get(f"idle:{router}") or 0)
        for message in reversed(self.store.messages(router, after=since, limit=20)):
            meta = message["meta"] or {}
            if message["kind"] == "owner" and meta.get("agent") == agent:
                return {
                    key: meta[f"agent_{key}"]
                    for key in ("model", "effort")
                    if meta.get(f"agent_{key}")
                }
        return {}

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
        if not self.role_key(row):
            # (Chief of Staff's own sessions have no token limit: none waits for its spend.)
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
            if self.role_key(row) in PLACED_ROLES:
                continue  # Placed where it can run instead (place_role), never stopped.
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
                    if paused := self.plan_paused(named[row["id"]], why, row["id"]):
                        await self.workers.interrupt(row["id"], by=agents.PLAN_STOP, notice=paused)
                    else:
                        await self.workers.interrupt(row["id"], by="Settings")
                stopped[row["id"]] = why
        return list(stopped.values())

    async def stop_blocked(self, session_id: str, why: str):
        """A turn of a worker that may not run now began anyway (its admission held as a change
        came): stopped at its first event, as stop_ineligible stops one at work, saying why (its
        plan's limit: paused, as when its request is refused)."""
        self.halted.add(session_id)
        with suppress(Exception):
            if paused := self.plan_paused(self.agent(session_id), why, session_id):
                await self.workers.interrupt(session_id, by=agents.PLAN_STOP, notice=paused)
            else:
                await self.workers.interrupt(session_id, by="Settings", notice=f"Stopped: {why}")

    def plan_paused(self, agent: str, why: str, session_id: str) -> str:
        """What a worker's timeline says when `why` it may not run is its agent's plan's limit
        reached ("" for any other reason): paused, not stopped by Settings (what its row says
        of the limit, kept: record_pause)."""
        entry = self.agents.get(agent)
        backend = entry.backend if entry else None
        refusal = getattr(backend, "limit_refusal", None)
        if not why or not callable(refusal) or refusal() != why:
            return ""
        limit = getattr(backend, "limit", {})
        if not (line := agents.limit_line(limit, name=agent)):
            return ""
        self.record_pause(session_id, agent, limit)
        return f"Paused: {line}"

    def record_pause(self, session_id: str, agent: str, limit: Any):
        """A worker its agent paused for a limit: what its task's row says of that limit (its
        reset first), kept until it pauses again."""
        self.store.set(f"planpause:{session_id}", json.dumps(agents.pause_facts(limit, agent)))

    def pause_view(self, task: dict, row: dict) -> dict:
        """A task its worker's agent paused for a limit: the agent, when the limit resets (if it
        said), whether it is a rate limit, and when it goes on by itself (the owner's Go on at
        <reset>: 0 when not asked)."""
        try:
            facts = json.loads(self.store.get(f"planpause:{row['id']}") or "{}")
        except ValueError:
            facts = {}
        if not isinstance(facts, dict) or not facts.get("agent"):
            facts = agents.pause_facts({}, self.agent(row["id"]))
        return {
            **facts,
            "agent_name": agents.display(str(facts["agent"])),
            "line": agents.pause_line(facts),
            "go_on_at": int(self.store.get(f"goonat:{task['id']}") or 0),
        }

    async def agents_saved(
        self, before: dict[str, dict], chose: bool | None = None, models: dict | None = None
    ) -> list[str]:
        """The owner saved Agents & models (or answered a first-run card): an agent turned on
        starts with its first worker; one turned off drains (more-agents.md 5.11): its workers
        stop at once with their commands, its process ends, and with none on the Router's tools
        and prompt are as without agents. Workers whose model came from the group take the new
        one with their next turn. Returns why each worker stopped."""
        after = agents.configured(self.store)
        was_on = sorted(name for name, entry in before.items() if entry.get("enabled"))
        now = agents.turned_on(self.store)
        if "claude" in now and "claude" not in was_on:
            self.claude_setup.turned_on()  # Its Agent SDK set up now, where it is missing.
        elif "claude" in was_on and "claude" not in now:
            await self.claude_setup.stop()  # Off before it was set up: nothing more installed.
        for name in now:
            if name not in self.agents:
                self.agents[name] = self.agent_connection(name)
            # Decided on in Settings: its first-run card is not shown again (nor once off).
            if not self.store.get(f"{agents.OFFERED}{name}"):
                self.store.set(f"{agents.OFFERED}{name}", "settings")
            if self.offers.pop(name, None):
                self.publish_state()
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
        switched = await self.copilot_switched()
        chosen = agents.roles(self.store)
        for role, agent in chosen.items():
            if agent != self.roles_seen.get(role, COPILOT_AGENT):
                # The owner's choice: tried now, whatever failed it before (its check too).
                self.clear_role_holds(role)
                self.agent_failed.pop(agent, None)
                self.agent_checked_at.pop(agent, None)
        self.roles_seen = chosen
        if (
            was_on != now
            or switched
            or (chose is not None and chose != agents.router_chooses(self.store))
        ):
            await self.refresh(ROUTER)  # Its spawn_worker and prompt name the agents on.
        await self.place_roles()  # (Opened anew where it runs now, with those.)
        # Sessions that take an agent's model or effort from Settings take a new one at rest:
        # its workers and the roles on it, each but for what it chose itself (its ⋯, a task's
        # chip).
        changed = {
            name: {
                key
                for key in ("model", "effort")
                if (before.get(name, {}).get(key) or "") != (after.get(name, {}).get(key) or "")
            }
            for name in now
        }
        # (Chief of Staff's own sessions: their kind's model and effort on the agent each runs
        # on, from `models`, as agents.work_view had them before.)
        roles = {row["id"]: role for role in PLACED_ROLES if (row := self.role_row(role))}
        rows = [*self.store.open_sessions(WORKER), *map(self.store.session, roles)]
        for row in rows:
            agent = self.agent(row["id"])
            if role := roles.get(row["id"]):
                then = ((models or {}).get(role) or {}).get(agent) or {}
                now = agents.kind_model(self.store, role, agent)
                keys = {key for key in ("model", "effort") if (then.get(key) or "") != now[key]}
                keys = keys if models is not None and agent != COPILOT_AGENT else set()
            else:
                keys = changed.get(agent) or set()
            own = self.agent_choice(row["id"])
            if any(not own.get(key) for key in keys):
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
        row = self.store.find_session(session_id)
        limited = agents.limit(self.store, agent) and not (row and self.role_key(row))
        return {
            "agent": agent,
            "session_id": session_id,
            "kind": "unknown",
            "accepted": 0 if limited else 1,
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
            if self.role_key(row):
                continue  # No token limit of its own.
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
            why = (
                f"Used its {limit:,}-token limit. Raise it in Settings → Agents & models, or "
                "cancel."
            )
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
        limit = 0 if self.role_key(row) else agents.limit(self.store, agent)
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
        return f"turn its limit off in Settings → Agents & models ({name}) to let it continue."

    async def ensure_spend_card(self, row: dict) -> dict | None:
        """A worker with a token limit whose spend is partly unknown, not accepted: the Go on
        card the owner accepts that with, made if it has none, so what its refusals point at
        exists (at work, it stops as its limit stops it; else its latest task waits, stopped).
        A failed one is retried instead (Retry accepts)."""
        if (
            row["closed_at"]
            or self.on_copilot(row["id"])
            or self.role_key(row)
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

    async def agent_elsewhere(self, agent: str) -> dict[str, int] | None:
        """Another agent's conversations the owner resumed in a terminal: id -> its pid. Read
        also while it has no backend (then no process is its workers'); None, and nothing
        recorded, while its backend starts or ends (its processes not told from the owner's)."""
        entry = self.agents.get(agent)
        backend = entry.backend if entry else None
        if backend is not None and not backend.alive():
            return None
        found: dict[str, int] = {}
        with suppress(Exception):
            found = await (
                backend.open_elsewhere()  # type: ignore[attr-defined]
                if backend is not None
                else agents.opened_elsewhere(agent)
            )
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
        if why := await self.terminal_settings(row, agent):
            raise ValueError(why)  # (Its timeline's line has Use these settings.)
        if self.store.get(f"schedulesdelete:{session_id}"):
            # Ended with its task, but still in its Claude conversation, which would bring them
            # back in the terminal: deleted there first (delete_dropped_schedules). Why that did
            # not happen yet, and what lets it, if it was tried and failed.
            failed = json.loads(self.store.get(f"schedulesfailed:{session_id}") or "{}")
            if session_id not in self.deleting_schedules:
                self.background(self.delete_dropped_schedules(session_id))
            if failed.get("why"):
                raise ValueError(
                    "Its schedules ended with its task but are still in its Claude "
                    f"conversation, where they would run in your terminal. Deleting them there "
                    f"failed: {failed['why']} {failed['next']}"
                )
            raise ValueError(
                "Its schedules ended with its task but are still in its Claude conversation, "
                "where they would run in your terminal: Chief of Staff is deleting them. Try "
                "again in a moment."
            )
        if schedules := json.loads(self.store.get(f"agentschedules:{session_id}") or "[]"):
            # Claude brings a worker's schedules back with its conversation, so they would run
            # in the terminal, where Chief of Staff holds nothing of them.
            when = ", ".join(str(entry.get("human") or "scheduled") for entry in schedules[:3])
            raise ValueError(
                f"It has schedules ({when}) that would go on running in your terminal: tell it "
                "to cancel them first (CronDelete), then continue there."
            )
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
                if getattr(backend, "dead", ""):
                    logger.warning("The Copilot CLI stopped answering; connecting again.")
                    error = "Copilot stopped answering. Connecting again…"
                else:
                    logger.warning("The Copilot CLI stopped by itself; connecting again.")
                    error = "Copilot stopped unexpectedly. Connecting again…"
                self.status = {"state": "error", "error": error}
                self.publish_state()
                self.recover_later(at_once=True)
                return
            await asyncio.sleep(PROCESS_CHECK)

    def recover_later(self, at_once: bool = False):
        """Recovery (recover) runs, unless it already does; `at_once`: its next step now."""
        if self.stopping:
            return
        if self.recovery and not self.recovery.done():
            if at_once:
                self.recover_soon.set()
            return
        self.recovery = asyncio.ensure_future(self.recover(at_once))

    def recovering(self) -> bool:
        """Whether there is something to recover: Chief of Staff not running (Copilot stopped
        or stopped answering, a start failed, the Router open in a terminal, no agent ready),
        or running on another agent while Copilot is on but can't run."""
        return not self.stopping and (not self.ready() or self.copilot_wanted())

    def recover_delay(self) -> float:
        """recover's first wait: short while Chief of Staff can't run, longer while it runs."""
        return COPILOT_RETRY if self.ready() else RECONNECT_DELAY

    async def recover(self, at_once: bool = False):
        """The one recovery path: until Chief of Staff runs, and Copilot with it while it is
        on, what is missing is tried again (recover_step), waiting longer after each failure
        (recover_delay, doubling up to RECONNECT_MOST; from the start again when what is
        missing changes); while the Router is open in a terminal, as soon as it is closed
        there. What may let it go on (Copilot dead, turned on; Claude set up) asks for its next
        step now (recover_later, at_once)."""
        delay = self.recover_delay()
        wait = 0.0 if at_once else delay
        while self.recovering():
            with suppress(TimeoutError):
                await asyncio.wait_for(self.recover_soon.wait(), wait)
            self.recover_soon.clear()
            if not self.recovering():
                return
            if not self.ready() and await self.router_elsewhere():
                wait = RECONNECT_DELAY  # Looked at again shortly.
                continue
            before = self.recover_delay()
            try:
                await self.recover_step()
            except Exception:
                logger.warning("Recovering failed.", exc_info=True)
            if (base := self.recover_delay()) != before:
                delay = base
            wait, delay = delay, min(delay * 2, RECONNECT_MOST)

    async def recover_step(self):
        """recover's one step. Chief of Staff not running: connected again (Copilot's client
        replaced, its sessions resumed in the background, each bounded: connect), at once when
        Copilot stopped or a start failed, and while it waits for an agent once one may run it
        (agent_found) or Claude's setup ended (to say how). Running without Copilot that is on:
        Copilot started again (restart_copilot). One step at a time; none while a connect goes
        on (its outcome decides)."""
        async with self.recover_lock:
            if self.stopping or self.connect_lock.locked():
                return
            if not self.ready():
                if self.status.get("waits"):
                    ended = self.status.get("settingup") and not self.claude_setup.running()
                    if not (ended or await self.agent_found()):
                        return
                await self.reconnect()
            elif self.copilot_wanted():
                await self.restart_copilot()

    async def recover_now(self):
        """recover's step now (Copilot turned on, Claude set up); it goes on by itself after
        that while anything is still missing."""
        await self.recover_step()
        self.recover_later()

    async def router_elsewhere(self) -> int:
        """The process that has the Router open in another Copilot window, if one does."""
        router_id = self.store.get("router_id")
        if not router_id or not self.backend:
            return 0
        with suppress(Exception):
            self.elsewhere = await self.open_elsewhere()
        return self.elsewhere.get(self.copilot_id(router_id), 0)

    def ready(self) -> bool:
        """Chief of Staff is up: its Router runs, on Copilot, or, while Copilot is off or can't
        run, on another agent (copilot_down)."""
        return self.status["state"] == "ready" and (
            self.backend is not None or bool(self.copilot_down())
        )

    def copilot_ready(self) -> bool:
        """Chief of Staff is up and Copilot runs: for what only Copilot has (its models, its
        sessions' list and logs, its sandbox)."""
        return self.ready() and self.backend is not None and not self.copilot_problem

    def require_up(self):
        """Raises while Chief of Staff is not up (ready): "Copilot is not connected" where
        Copilot is the only agent (as before), else Chief of Staff's own words."""
        if not self.ready():
            who = "Copilot" if agents.usable(self.store) == [COPILOT_AGENT] else "Chief of Staff"
            raise RuntimeError(
                f"{who} is not connected"
                + (f": {self.status['error']}" if self.status["error"] else ".")
            )

    def require_ready(self) -> AgentBackend:
        """Copilot's connected backend; raises when Copilot is not connected."""
        if not self.ready() or self.backend is None:
            why = self.status["error"] or (self.ready() and self.copilot_down())
            raise RuntimeError("Copilot is not connected" + (f": {why}" if why else "."))
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
        """Whether a worker's shell commands run in the sandbox (sandbox_policy): as Settings
        say, where there is one, on an agent whose sandbox holds (sandboxes)."""
        return bool(
            row["role"] == WORKER
            and (self.store.settings().worker_sandbox or row["slack"])
            and self.sandbox_host
            and self.sandboxes(row)
        )

    def sandbox_for(self, slack: bool) -> bool:
        """Whether a worker's commands run in the sandbox, as Settings say and where there is
        one: those of workers that read Slack (`slack`) always, as what they read must not leave
        through a command."""
        return bool(
            (self.store.settings().worker_sandbox or slack)
            and self.sandbox_host
            and self.copilot_sandbox()
        )

    def copilot_sandbox(self) -> bool:
        """Whether Copilot has its sandbox here: its backend's, or, while Copilot does not run
        (Chief of Staff on another agent), as Copilot has one."""
        return bool(self.backend.capabilities.sandbox if self.backend else COPILOT_CAPS.sandbox)

    def held_where_it_runs(self, row: dict, sandbox: SandboxPolicy) -> SandboxPolicy:
        """A Claude worker's sandbox: Claude's lets a command write the folder it runs in,
        whatever its list of writable folders says (2.1.322, contract test), so its folder and
        checkouts that are not its to write (the owner's, given to read) are held read-only."""
        places = [row["cwd"], *(c.get("path") for c in self.workspaces.checkouts(row))]
        held = [
            os.path.realpath(place)
            for place in dict.fromkeys(place for place in places if place)
            if not any(within(place, root) for root in sandbox.readwrite)
        ]
        if not held:
            return sandbox
        return replace(sandbox, readonly=(*sandbox.readonly, *held))

    def settings_guarded(self, row: dict) -> bool:
        """Whether a worker's folder settings (and the .git it can write) wait for the owner's
        word when they change: only while its commands run in the sandbox. Without it, a worker
        already runs commands as the owner: a hold there adds friction, not protection, and none
        of the looks (settings_refusal, the glances) run. Whatever its agent: a Claude worker
        without the sandbox is not guarded either (one with it runs in Claude's own sandbox, or
        not at all, as a Codex worker can't yet)."""
        return row["role"] == WORKER and self.sandbox_for(bool(row["slack"]))

    def sandbox_policy(
        self,
        row: dict,
        writes: list[str] | None = None,
        trees: Sequence[dict] = (),
        private: str = "",
    ) -> SandboxPolicy | None:
        """What a worker's shell commands may touch; None when they run unsandboxed, like the
        owner's own Copilot sessions (whose Copilot sandbox settings then apply, if any).
        `writes`: a shepherd step's folders instead of the worker's (Shepherd.step_policy, which
        narrows it further), whatever the worker's own sandbox: its caches, held files and
        denials as the worker's, with `trees` (the worktrees it made) as the worker's own, and
        nothing the owner added for workers (folders, their SSH agent). Its git: `private`, its
        own folder (stepgit.py), with its HEAD and index its own; never the repositories its
        checkouts share with the owner and other workers, which stay read-only to it."""
        step = writes is not None
        if not step and not self.sandboxed(row):
            return None
        checkouts = [*self.workspaces.checkouts(row), *trees]
        repositories = ([private] if private else []) if step else git_dirs(checkouts)
        settings = self.store.settings()
        writes = self.workspaces.writes(row) if writes is None else writes
        writable = [*writes, *repositories, *([] if step else settings.sandbox_writable)]
        # Its tools' caches: workers' own, the owner's read-only beneath (see caches.py).
        cached = self.cache_plan(row)
        writable += cached.readwrite
        protected = [
            path
            for git_dir in repositories
            for path in protected_git_paths(git_dir, checkouts, own_head=step)
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
        for folder in map(os.path.realpath, writes):
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
        if not step:
            readonly += [str(ssh / n) for n in ("known_hosts", "config") if (ssh / n).exists()]
        if (agent := os.environ.get("SSH_AUTH_SOCK")) and not step:
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
            # Its own daemons, yes; the owner's (their tokens are in these files), no. Nor
            # Chief of Staff's private files for other agents' workers.
            denied=tuple(
                dict.fromkeys(
                    [*service_secrets(), *closed, *cached.denied, *absent, *self.private_folders()]
                )
            ),
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
        sandbox: its commands run as the owner, and nothing of its folder's settings is
        checked (settings_guarded)."""
        return bool(row["role"] == WORKER and row["cwd"] and sandboxed)

    def sandboxes(self, row: dict) -> bool:
        """Whether its agent sandboxes its commands as Chief of Staff's sandbox says: Copilot's,
        and another's whose version's check showed its sandbox holding (agent_refused)."""
        if self.on_copilot(row["id"]) if row.get("id") else True:
            return self.copilot_sandbox()
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
        path = await self.cache_script(row)
        try:
            if path is not None and not await shell_init(session, [str(path)]):
                logger.warning("Worker %s's tools could not be given their caches", row["id"])
        except Exception:
            logger.warning("Worker %s's tools could not be given their caches", row["id"],
                           exc_info=True)  # fmt: skip

    async def cache_script(self, row: dict) -> Path | None:
        """A sandboxed worker's init script, which points its tools at its caches (made at every
        open; its temporary folder and links first): its path, None when it could not be made.
        Workers' caches are filled from the owner's soon after."""
        found = self.cache_plan(row)
        path = None
        try:
            path = await asyncio.to_thread(caches.prepare, found, row["id"], self.cache_scripts)
            if path is None:
                logger.warning("Worker %s's tools could not be given their caches", row["id"])
        except Exception:
            logger.warning("Worker %s's tools could not be given their caches", row["id"],
                           exc_info=True)  # fmt: skip
        if not row["slack"]:
            self.seed_caches_soon()
        return path

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
        """Whether this worker acts without permission checks (a session choice, else Settings).
        Never while it is in a shepherd step: its checks apply then, whatever its mode."""
        if row["role"] == WORKER and self.shepherd.marked(row["id"]):
            return False
        return self.yolo_choice(row)

    def yolo_choice(self, row: dict) -> bool:
        """The worker's own mode: a session choice, else Settings."""
        if row["role"] != WORKER:
            return False
        if row.get("id") and self.agent(row["id"]) == "codex" and self.special(row) == "messenger":
            # It sends as the owner's own notifications go (their bot's CLI, a server): on Codex,
            # which has no sandbox Chief of Staff checked, every such command asks it first.
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

    async def checkout_place(self, row: dict) -> tuple[Path, str]:
        """Where a worker's new checkout goes (its folder, made now) and its branch's name."""
        workspace = self.workspaces.workspace(row)
        await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        slug = row["branch"].removeprefix(BRANCH_PREFIX) if row["branch"] else workspace.name
        return workspace, slug

    def taken(self, row: dict, checkouts: list[dict], checkout: dict) -> bool:
        """A checkout made for a worker, among its own from now on; whether its sandbox takes
        it in from its next turn (a worktree's history is in its repository's .git, outside what
        the sandbox let this session write when it started: reattached after this turn)."""
        self.update_session(row["id"], repos=json.dumps([*checkouts, checkout]))
        if not (checkout.get("worktree") and self.sandboxed(row)):
            return False
        self.states[row["id"]].stale = True
        return True

    async def own_copy(self, row: dict, request: Any) -> str | None:
        """A worker's shell command refused in a checkout of the owner's it was given only to
        read, or one its own worktree was made from (policy.owners_checkout: it writes there, or
        runs what can't be read there): its own copy of that checkout, made as add_repo makes a
        worktree, from the owner's commit with their uncommitted work copied in
        (Workspaces.make_checkout `copy`), or, for a line naming what it writes there, a
        worktree of that repository it has. What it hears then (policy.OWN_COPY); None when it
        has none (one is tried once a repository: one that could not be made is not tried
        again)."""
        command = getattr(request, "full_command_text", "") or ""
        folder = str(
            getattr(request, "resolved_working_directory", None)
            or getattr(request, "cwd", None)
            or row["cwd"]
            or ""
        )
        shell = str(getattr(request, "shell", "") or "")
        try:
            async with self.states[row["id"]].checkouts:
                return await self.copy_for(self.store.session(row["id"]), command, folder, shell)
        except Exception:
            logger.warning("Could not make %s a copy of a checkout.", row["id"], exc_info=True)
            return None

    async def copy_for(self, row: dict, command: str, folder: str, shell: str = "") -> str | None:
        """own_copy's work, its checkouts held (no add_repo meanwhile)."""
        checkouts = self.workspaces.checkouts(row)
        # Given to read, or one its own worktree was made from.
        _, behind = owners_checkouts(row, checkouts)
        kept = await asyncio.to_thread(checkout_of, command, folder, [
            *(c for c in checkouts
              if c.get("shared") and c.get("git_dir") and not c.get("in_place")),
            *({"path": repo, "top": repo} for repo in behind),
        ])  # fmt: skip
        if not kept:
            return None
        top = kept.get("top") or await git("-C", kept["path"], "rev-parse", "--show-toplevel")
        top = await asyncio.to_thread(os.path.realpath, top.strip())
        there = await asyncio.to_thread(os.path.realpath, kept["path"])
        # A line that only writes files there it names may write them where it works (a
        # worktree of its own); one that runs a program there (a test run, a build, one not
        # known) runs on the owner's files as they are: in a copy of them, never in a worktree
        # of its own, on its branch and maybe behind them (live r78 e2e: its tests weren't there).
        reading = await asyncio.to_thread(
            checkout_reading, command, folder, (kept["path"],), shell, bool(kept.get("shared"))
        )
        copies = reading.ran or not reading.wrote
        if own := await asyncio.to_thread(own_tree, checkouts, top, there, copies):
            line = OWN_COPY if own.get("copy") else OWN_WORKTREE
            at = await asyncio.to_thread(place_in, own["at"], there, folder)
            return line.format(path=at, left_out="")
        state = self.states[row["id"]]
        if top in state.copies_tried or own is not None:
            return None
        state.copies_tried.add(top)
        workspace, slug = await self.checkout_place(row)
        mine = await asyncio.to_thread(
            lambda: [c for c in checkouts if os.path.realpath(c.get("repo") or c["path"]) == top]
        )
        if any(c.get("branch") == BRANCH_PREFIX + slug for c in mine):
            slug += "-copy"  # (Its own worktree of it is on that branch.)
        have = {
            c.get("repo", c["path"])
            for c in checkouts
            if (c.get("branch") or c.get("worktree")) and c not in mine
        }
        checkout = await self.workspaces.make_checkout(
            workspace, slug, kept["path"], True, have, carry=not row["slack"], copy=True
        )
        if not checkout:
            return None
        left = checkout.pop("left_out", [])
        self.taken(row, checkouts, checkout)
        try:
            await self.task_actions.copied_in(row["id"], checkout)
        except Exception:  # (Its receipts may count what was copied in: it has its copy.)
            logger.warning("Could not record where %s's copy starts.", row["id"], exc_info=True)
        at = await asyncio.to_thread(place_in, checkout["path"], there, folder)
        shown = ", ".join(left[:5]) + (f", and {len(left) - 5} more" if len(left) > 5 else "")
        return OWN_COPY.format(path=at, left_out=f" Not copied in: {shown}." if left else "")

    async def add_repo(self, caller: str, repo: str):
        row = self.store.session(caller)
        workspace, slug = await self.checkout_place(row)
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
            checkout = await self.workspaces.clone_checkout(
                workspace, slug, repo, slack=bool(row["slack"])
            )
        stale = self.taken(row, checkouts, checkout)
        self.add_message(caller, "notice", f"Added {checkout['path']} ({checkout['branch']})")
        result = {key: checkout[key] for key in ("path", "branch", "base")}
        if stale:
            state = self.states[caller]
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
                self.yolo_choice(row),
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
        if row["role"] == ROUTER and settings.fork_busy:
            prompt = prompts.with_fork_rule(prompt)
        if row["role"] == ROUTER and settings.send_to_chief:
            prompt = prompts.with_send_rule(prompt)
        if row["role"] == ROUTER:
            default = agents.default_agent(self.store)
            prompt = prompts.router_on(agents.display(default), prompt)
            others = [name for name in agents.usable(self.store) if name != default]
            if self.agents and others:
                prompt = prompts.with_agents_rule(
                    prompt,
                    [agents.display(name) for name in others],
                    chooses=agents.router_chooses(self.store),
                    default=agents.display(default),
                )
        if row["role"] == ROUTER and self.shepherd.on():
            prompt = prompts.with_shepherd_rule(prompt)
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
        if row["role"] == WORKER and self.shepherd.marked(row["id"]):
            # A shepherd step runs now: its own options over the worker's (shepherd.py).
            return self.shepherd.step_options(row, self.worker_options(row))
        return self.worker_options(row)

    def worker_options(self, row: dict) -> dict:
        settings = self.store.settings()
        sandbox = self.sandbox_policy(row)
        prompt = self.session_prompt(row, sandbox)
        tools = self.session_tools(row)
        options: dict[str, Any] = {
            # Copilot's record of past sessions, which the owner's own sessions can search: for
            # workers that do not read Slack. Sessions that read Slack are not recorded in it,
            # so what anyone wrote on Slack never reaches a session through it.
            "enable_session_store": row["role"] == WORKER and not row["slack"],
            "on_permission_request": self.events.on_permission,
            "tools": tools,
            "system_message": {"mode": "append", "content": prompt},
            "working_directory": row["cwd"] or str(self.role_dir("scratch")),
            "client_name": CLIENT_NAME,
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
        if row["role"] != WORKER:
            # Router and Slack get nothing but their own tools (Slack's read tools among them:
            # session_tools).
            options["available_tools"] = [tool.name for tool in tools]
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

    def session_tools(self, row: dict) -> list:
        """Chief of Staff's tools a session has, on whichever agent it runs."""
        tools = [self.tools[name] for name in self.tool_names(row)]
        router = row["role"] == ROUTER
        on = self.agents_on() if router else []
        kept = router and self.shepherd.on()
        forks = router and self.store.settings().fork_busy
        if on or kept or forks:
            # Its spawn_worker names the agents on (as without them otherwise); with Prepared
            # for you on, spawn_worker and message_session take a PR to keep moving; with second
            # workers on (forks.py), spawn_worker can start from a related worker's conversation
            # (fork_of) and message_session can keep a new task for a busy worker.
            chooses = bool(on) and agents.router_chooses(self.store)
            copilot = agents.copilot_on(self.store)
            variants = {
                "spawn_worker": self.toolset.spawn_on(
                    on, chooses=chooses, shepherd=kept, forks=forks, copilot=copilot
                ),
                "message_session": self.toolset.message_on(kept, forks=forks),
            }
            tools = [variants.get(tool.name) or tool for tool in tools]
        if on:
            # It can move a worker's work to another (continue_worker).
            tools.append(self.toolset.continue_on(on, agents.copilot_on(self.store)))
        return tools + self.slack_read_tools(row)

    def slack_read_tools(self, row: dict) -> list:
        """Slack's read tools for a session that reads Slack (the Slack session, a worker that
        reads Slack), on any agent, as Chief of Staff's own tools: each call is Chief of Staff's,
        with the token it holds (Slack.read_proxy, discovered as the session opens), so no
        agent's configuration, argument or variable ever has that token."""
        if not (row["role"] == SLACK or row["slack"]) or not self.slack.read_specs:
            return []
        if not self.slack.configured():
            return []
        made = self.slack_tools_made
        if made[0] != (specs := tuple(sorted(self.slack.read_specs))):
            made[:] = [specs, [slack_tool(spec, self.slack.read_call)
                               for spec in self.slack.read_specs.values()]]  # fmt: skip
        return list(made[1])

    def agent_model_choice(self, row: dict) -> dict:
        """The model and effort a session on another agent runs with: its own (its ⋯, the
        Router's or a task's chip), else its kind of work's on that agent in Settings."""
        kind = agents.kind_of(row["role"], self.special(row))
        chosen = agents.kind_model(self.store, kind, self.agent(row["id"]))
        return chosen | {key: value for key, value in self.agent_choice(row["id"]).items() if value}

    def role_options(self, row: dict) -> dict:
        """What one of Chief of Staff's own sessions that needs only its own tools (the Router,
        the Watchdog, the Slack session, the helpers that only read what they are given) runs
        with on another agent, in neutral form for its adapter to map: its instructions and
        tools as on Copilot, and nothing else (`role_only`: none of the agent's own tools,
        servers, hooks or plugins; every other call asks, and on_permission refuses it), asking
        first, its kind's model on that agent in Settings or the one chosen in its chat. The
        Slack session's read tools come through Chief of Staff (open_live: read_proxy)."""
        cwd = row["cwd"] or str(self.role_dir("scratch"))
        chosen = self.agent_model_choice(row)
        # A helper that only reads what it is given (TOOLLESS) has the helpers' instructions.
        helper = self.special(row) in TOOLLESS
        options: dict[str, Any] = {
            "system_message": {
                "content": prompts.HELPER if helper else self.session_prompt(row, None)
            },
            "tools": self.session_tools(row),
            "working_directory": cwd,
            "on_permission_request": self.events.on_permission,
            "ask_first": True,
            "role_only": True,
            "folders": [cwd],
            "worker": row["id"],
            "children": [],
            "private_folders": self.private_folders(),
            "on_accounting": lambda kind, data: self.agent_accounting(row["id"], kind, data),
        }
        if model := chosen.get("model"):
            options["model"] = model
        if effort := chosen.get("effort"):
            options["reasoning_effort"] = effort
        return options

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
            "client_name": CLIENT_NAME,
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
        if self.special(row) == "messenger":
            # The notification it sends, written there for it (Connector.write_notification).
            folders.append(str(self.role_dir("away")))
        chosen = self.agent_model_choice(row)
        options: dict[str, Any] = {
            "system_message": {"content": self.session_prompt(row, None)},
            "tools": [self.tools[name] for name in self.tool_names(row)]
            + self.slack_read_tools(row),
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
            # Chief of Staff's private files, which no worker reads (policy.private_denial).
            "private_folders": self.private_folders(),
            # It reads Slack: other sessions neither hear from it nor message it.
            "reads_slack": bool(row["slack"]),
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
            if self.agent(row["id"]) == "claude":
                options["sandbox"] = self.held_where_it_runs(row, sandbox)
        if row["slack"] and self.agent(row["id"]) == "claude":
            # Slack's read tools are Chief of Staff's own (slack_read_tools): no server named
            # slack is its to call.
            options["slack_tools"] = []
        if model := chosen.get("model"):
            options["model"] = model
        if effort := chosen.get("effort"):
            options["reasoning_effort"] = effort
        if (thread := self.store.get(f"agentcontinues:{row['id']}")) and not self.made(row["id"]):
            options["continues"] = thread  # The owner's thread it takes over (Workers.adopt).
        if self.agent(row["id"]) == "claude":
            # Claude's own capabilities, tied into Chief of Staff (more-agents.md 5.12.4).
            sid = row["id"]
            options |= {
                "token_limit": agents.limit(self.store, "claude"),
                "schedules": json.loads(self.store.get(f"agentschedules:{sid}") or "[]"),
                "on_schedules": lambda found: self.agent_schedules(sid, found),
                # Held after a stop (until the owner says go on); ended with its task.
                "schedules_held": lambda: bool(self.store.get(f"scheduleshold:{sid}")),
                "dropped_schedules": json.loads(self.store.get(f"schedulesdropped:{sid}") or "[]"),
                # Kept apart from the hook (Claude waits for it), as the checkouts' lock allows.
                "on_worktree": lambda path, removed: self.background(
                    self.agent_worktree(sid, path, removed)
                ),
                "on_peer": lambda kind, data: self.agent_peer(sid, kind, data),
                "on_notify": lambda text: self.agent_notified(sid, text),
                # Its folder's MCP servers whose files changed since the owner last let it use
                # them: off for it (servers_off), as for a Copilot worker.
                "disabled_mcp_servers": self.disabled_servers(row),
                # Before a subagent starts (Claude loads its definition then): its folder's
                # settings looked at, and a server off for it not started (as Events.pre_tool).
                "on_subagent": lambda tool, arguments: self.agent_subagent(sid, tool, arguments),
            }
        return options

    async def agent_subagent(self, session_id: str, tool: str, arguments: Any) -> str:
        """Why a Claude worker's subagent may not start ("" when it may): its folder's settings
        changed (a glance, the full check only when that changed: settings_look, which stops
        the worker; a sandboxed one only: settings_guarded), or the subagent's own server is off
        for it (server_refusal)."""
        row = self.store.find_session(session_id)
        if not row or row["closed_at"] or not row["cwd"] or row["role"] != WORKER:
            return ""
        if not self.settings_guarded(row):
            return self.server_refusal(row, tool, arguments)
        try:
            why = await asyncio.wait_for(self.settings_look(row), SETTINGS_SECONDS)
        except Exception:
            logger.warning("Could not look at %s's agent settings.", session_id, exc_info=True)
            why = "Chief of Staff could not look at this folder's settings: try again later."
        return why or self.server_refusal(row, tool, arguments)

    async def terminal_settings(self, row: dict, agent: str) -> str:
        """Why a Claude or Codex worker may not go to a terminal now ("" when it may): its
        folder's settings of that agent differ from what the owner last let workers use (the
        owner's own session there would load them, with no check of Chief of Staff's), or a
        .git there was replaced. Its timeline says so, with Use these settings. A sandboxed
        worker's only (settings_guarded): without the sandbox its commands ran as the owner."""
        if agent not in {"claude", "codex"} or not row["cwd"] or not self.settings_guarded(row):
            return ""
        if replaced := await self.settings_thread(self.git_replaced, row):
            return self.git_replaced_said(row, replaced)
        names = agent_settings(agent)
        verdict = await self.settings_verdict(row, names)
        if verdict is None:
            return ""
        label = settings_agent(names)
        reason = self.settings_reason(label, verdict)
        stop = ". " if "; " in reason else ": "
        if verdict["refused"]:
            line = f"{reason}{stop}{label} would load them in your terminal. Remove that first."
        else:
            line = (
                f"{reason}{stop}{label} would load them in your terminal. Look at them, then "
                "Use these settings here first."
            )
        self.settings_said(row["id"], line, verdict, alert=False)
        return line

    def agent_children(self, session_id: str) -> list[str]:
        try:
            children = json.loads(self.store.get(f"agentchildren:{session_id}") or "[]")
        except ValueError:
            return []
        return [str(child) for child in children] if isinstance(children, list) else []

    def agent_notice(self, row: dict, text: str):
        self.add_message(row["id"], "notice", text)

    def agent_refused_card(
        self,
        agent: str,
        why: str,
        params: dict,
        worker: str = "",
        text: str = "",
        replay: dict | None = None,
        request: str = "",
    ):
        """A worker the owner named an agent for could not start on it (more-agents.md 5.12.8,
        recovery), or (`worker`) what the Router gave a worker of theirs on an agent that
        cannot run now (signed out, say), `text` (`replay`: that message as the Router sent
        it), did not reach it: the owner's card in the Router's chat says why in one line and
        offers what fixes it (Sign in, Install: agentsetup.py), or one click to run it on
        Copilot instead (continue that worker's work there, with what it was given), or to
        drop it; the Router not saying it again; nothing starts elsewhere unasked. Held for a
        sign-in or an install, it is kept: it goes on by itself once that agent can run it
        (resume_held), as nothing of it ran yet."""
        key = uuid4().hex[:10]
        instead = self.instead_of(agent)
        found = self.unready(agent, why)
        record = {"agent": agent, "params": params, "instead": instead, "status": found["status"]}
        if request:
            # The owner request it was asked in: whatever starts it later makes its task for
            # that request (Teams), never for the turn running then.
            record["request"] = request
        if worker:
            record |= {"worker": worker, "text": text, **({"replay": replay} if replay else {})}
        name = agents.display(agent)
        title = params.get("title") or "The task"
        if found["status"] in agents.WAITS:
            needs, once = {
                agents.SIGN_IN: (f"{name} needs you to sign in", "once you have"),
                agents.MISSING: (f"{name} isn't installed", "once it is"),
                agents.LIMITED: (why, "once it resets"),
                agents.UNCHECKED: (
                    f"{self.check_unrun(agent) or why}; it's tried again by itself",
                    "once it has run",
                ),
            }[found["status"]]
            goes = f"gets it on {name}" if worker else f"starts on {name}"
            line = f"{needs}. Your message is saved: “{title}” {goes} {once}."
        else:
            # One line: why it cannot run (its status's words where they say it).
            said = f"{found['said'].lower()}." if found.get("fix") else why
            line = (
                f"“{title}” did not get this on {name}: {said}"
                if worker
                else f"“{title}” did not start on {name}: {said}"
            )
        message = self.add_message(
            self.router_id(),
            "notice",
            line,
            {
                "agent_refused": key,
                "agent": agent,
                "instead": instead,
                "status": found["status"],
                **({"worker": worker} if worker else {}),
                **({"fix": found["fix"]} if found.get("fix") else {}),
            },
        )
        record["card"] = message["id"]
        self.store.set(f"agentrefusal:{key}", json.dumps(record))
        if found["status"] == agents.UNCHECKED:
            self.arm_check(agent)

    def refusal_said(self, found: dict, change: dict):
        """A refusal's card as it stands now (resuming, resumed, answered): its meta changed,
        and the page told."""
        card = found.get("card")
        if not card:
            return
        with suppress(LookupError):
            message = self.store.message(int(card))
            meta = {k: v for k, v in (message["meta"] or {}).items() if k != "resuming"}
            self.store.update_message_meta(message["id"], {**meta, **change})
            self.bus.publish("message", {**self.store.message(message["id"]), "amended": True})

    async def resume_held(self, name: str, held: frozenset[str] = agents.HELD):
        """An agent that can run now (signed in, installed): each card of work it held for that
        (agent_refused_card) goes on by itself on it, once, after RESUME_SECONDS in which the
        owner can Cancel ("Resuming…"); a receipt says so, and it is listed under Handled for
        you. One the owner answered, kept for them to start (Cancel), or held for another
        reason is left as it is."""
        armed = False
        for key, raw in self.store.items("agentrefusal:").items():
            try:
                found = json.loads(raw)
            except ValueError:
                continue
            if (
                found.get("agent") != name
                or found.get("status") not in held
                or found.get("kept")
                or key in self.resuming
            ):
                continue
            until = time.time() + RESUME_SECONDS
            self.resuming.add(key)
            self.store.set(f"agentrefusal:{key}", json.dumps(found | {"resuming": until}))
            self.refusal_said(found, {"resuming": until})
            self.background(self.resume_later(key, until))
            armed = True
        if armed:
            self.publish_state()

    async def resume_later(self, key: str, until: float):
        try:
            await asyncio.sleep(max(0.0, until - time.time()))
            async with self.refusal_locks.setdefault(key, asyncio.Lock()):
                raw = self.store.get(f"agentrefusal:{key}")
                if not raw:
                    return  # Answered meanwhile (Continue with Copilot, say).
                found = json.loads(raw)
                if found.get("resuming") != until:
                    return  # Cancelled meanwhile.
                await self.resume_refused(key, found, after=RESUMED[found.get("status", "")])
        finally:
            self.resuming.discard(key)

    async def resume_refused(self, key: str, found: dict, after: str):
        """Held work started on its agent now: its card done first (never twice: a restart
        meanwhile finds nothing to do), then started as it was given; refused again (still not
        able to run), a new card says why."""
        self.store.delete(f"agentrefusal:{key}")
        agent = found["agent"]
        name = agents.display(agent)
        title = (found.get("params") or {}).get("title") or "the work"
        words = after[:1].upper() + after[1:]
        self.refusal_said(found, {"resumed": words})
        try:
            if found.get("worker"):
                from .tools import MessageSession

                replay = found.get("replay") or {
                    "session_id": found["worker"],
                    "text": found.get("text", ""),
                }
                await self.toolset.t_message_session(
                    MessageSession.model_validate(replay),
                    self.router_id(),
                    request=found.get("request", ""),
                )
                self.add_message(
                    self.router_id(),
                    "notice",
                    f"Gave “{title}” what it was waiting for on {name}: {after}",
                    {"session_id": found["worker"]},
                )
                made = {"session_id": found["worker"]}
            else:
                from .tools import SpawnWorker

                params = {k: v for k, v in found["params"].items() if k in SpawnWorker.model_fields}
                made = await self.toolset.t_spawn_worker(
                    SpawnWorker.model_validate(params),
                    self.router_id(),
                    agent=agent,
                    instead=f" on {name}: {after}",
                    request=found.get("request", ""),
                )
        except ValueError as exc:
            # Refused again: its new card says why (spawn_worker's, message_session's).
            logger.info("Held work for %s refused again: %s", agent, exc)
            self.refusal_said(found, {"resumed": f"Not resumed: {name} still can't run it"})
            return
        except Exception as exc:
            logger.warning("Held work for %s did not resume.", agent, exc_info=True)
            self.refusal_said(found, {"resumed": f"Not resumed: {describe(exc)}"})
            return
        once = {
            agents.SIGN_IN: "once you signed in",
            agents.LIMITED: "once its limit reset",
            agents.UNCHECKED: "once Chief of Staff's check of it ran",
        }.get(found.get("status", ""), "once installed")
        did = f"Gave “{title}” your message" if found.get("worker") else f"Started “{title}”"
        self.handled.record(
            "resumed",
            f"{did} on {name} {once}",
            session_id=made.get("session_id", ""),
            ref=key,
        )

    async def worker_agent_down(self, agent: str) -> str:
        """Why a worker on another agent cannot be given anything now: its agent does not
        start (signed out, not installed, …), tried now as its next message would; "" when
        it runs."""
        if said := await self.looked_again(agent) or self.limit_held(agent):
            return said  # Signed out while its process runs; its plan's limit used up.
        try:
            await self.connect_agent(agent)
        except Exception as exc:
            self.agent_start_failed(agent, exc)
            return describe(exc)
        self.agent_failed.pop(agent, None)
        return ""

    def instead_of(self, agent: str) -> str:
        """The agent a task the owner named another for is offered on instead: the one new
        workers start on, where it can run them now; else the first other that can, in the one
        order (Copilot, Codex, Claude)."""
        others = [
            other
            for other in [agents.new_workers(self.store), *agents.fallbacks(self.store, agent)]
            if other != agent
        ]
        return next(
            (other for other in others if not self.agent_unavailable(other)),
            others[0] if others else COPILOT_AGENT,
        )

    async def answer_refusal(self, key: str, action: str) -> dict:
        """The owner's answer on an agent refusal's card: start it on the agent it offers
        instead (Copilot, or the one new workers start on); start it on its own agent now
        (`now`: it can run there; held for its check, that is tried now); keep it, not going on
        by itself (`keep`: Cancel while it was resuming); or drop it (`wait`: nothing starts).
        One answer per card: another meanwhile (a double click, two tabs) finds it answered."""
        async with self.refusal_locks.setdefault(key, asyncio.Lock()):
            return await self.answering_refusal(key, action)

    async def answering_refusal(self, key: str, action: str) -> dict:
        raw = self.store.get(f"agentrefusal:{key}")
        if not raw:
            raise ValueError("That card was answered already.")
        found = json.loads(raw)
        if action == "keep":
            kept = {k: v for k, v in found.items() if k != "resuming"} | {"kept": True}
            self.store.set(f"agentrefusal:{key}", json.dumps(kept))
            self.refusal_said(found, {"kept": True})
            self.publish_state()
            return {}
        if action == "now":
            if found.get("status") == agents.UNCHECKED:
                self.agent_checked_at.pop(found["agent"], None)  # The owner's: its check now.
            await self.resume_refused(key, found, after="started when you asked")
            return {}
        if found.get("worker") and action == "copilot":
            # One of the owner's workers on an agent that could not run: its work goes on, with
            # what the Router gave it, on the agent offered (as Continue with… does); continued
            # already (its ⋯, before), what it was given goes to the worker it continued on.
            if done := self.continued_already(found["worker"]):
                return await self.refusal_to_continuation(key, found, done)
            instead = found.get("instead") or COPILOT_AGENT
            made = await self.continue_on(
                found["worker"],
                instead,
                also=found.get("text", ""),
                also_request=found.get("request", ""),
            )
            self.store.delete(f"agentrefusal:{key}")
            self.refusal_said(found, {"resumed": f"Continued on {agents.display(instead)}"})
            title = (found.get("params") or {}).get("title") or "the work"
            self.add_message(
                self.router_id(),
                "notice",
                f"Continued “{title}” on {agents.display(instead)}",
                {"session_id": made["session_id"], "task_id": made["task_id"]},
            )
            return made
        self.store.delete(f"agentrefusal:{key}")
        if action != "copilot":
            self.refusal_said(found, {"resumed": "Not run"})
            self.add_message(
                self.router_id(),
                "notice",
                f"Not run on {agents.display(found['agent'])}: ask again whenever you like.",
            )
            return {}
        from .tools import SpawnWorker

        instead = found.get("instead") or COPILOT_AGENT
        self.refusal_said(found, {"resumed": f"Started on {agents.display(instead)} instead"})
        params = {k: v for k, v in found["params"].items() if k in SpawnWorker.model_fields}
        await self.toolset.t_spawn_worker(
            SpawnWorker.model_validate(params | {"model": ""}),
            self.router_id(),
            agent=instead,
            instead=f" on {agents.display(instead)} instead",
            request=found.get("request", ""),
        )
        return {}

    async def refusal_to_continuation(self, key: str, found: dict, done: dict) -> dict:
        """Continue with Copilot on a card for a worker whose work went on already with a new
        one (Continue with…): what the Router gave the first goes to that one instead, as the
        Router sent it (a task the first had is the new one's now); nothing new is made."""
        from .tools import MessageSession

        target = done["session_id"]
        replay = dict(found.get("replay") or {"text": found.get("text", "")})
        replay["session_id"] = target
        if replay.get("task") == "continue":
            given = str(replay.get("task_id") or "")
            replay["task_id"] = self.store.get(f"replacedby:{given}") or done.get("task_id") or ""
        elif replay.get("task") == "new" and not replay.get("earlier"):
            replay["earlier"] = "keep"  # The owner's: more for it, its own task goes on.
        await self.toolset.t_message_session(
            MessageSession.model_validate(replay),
            self.router_id(),
            request=found.get("request", ""),
        )
        self.store.delete(f"agentrefusal:{key}")
        name = agents.display(self.agent(target))
        title = self.display_title(self.store.session(target))
        # The card says where it went; message_session's own line in the chat says what it was.
        self.refusal_said(found, {"resumed": f"Sent to “{title}” on {name}"})
        return {**done, "sent": True}

    def plan_limit_seen(self, agent: str, limit: dict):
        """An agent's plan limit as its worker last said it: Settings says it. A worker pauses
        only when its own request is refused (Events.plan_limited): a plan's limit reached with
        extra usage on refuses nothing, and each other worker meets it on its next request.
        Used up until a time it said: the page says until when, new work for it is held, and
        its reset is waited for (limit_reset); said to be over, what it held goes on."""
        before = agents.live_limit(self.store, agent)
        self.store.set(f"agentlimit:{agent}", json.dumps(limit))
        if agents.limit_status(limit, agent):
            self.arm_limit(agent)
            self.publish_state()
        elif agents.limit_status(before, agent):
            self.background(self.limit_over(agent))

    def limit_held(self, agent: str) -> str:
        """Why work for an agent waits now: its plan's limit used up until a time it said (the
        line the page and its card say); "" else."""
        entry = self.agents.get(agent)
        live = agents.live_limit(self.store, agent, getattr(entry and entry.backend, "limit", None))
        found = agents.limit_status(live, agent)
        return found["line"] if found else ""

    def arm_limit(self, agent: str):
        """An agent's plan limit used up, its reset waited for (one wait per agent; a restart
        arms it again, one whose time passed meanwhile at once)."""
        limit = agents.live_limit(self.store, agent)
        resets = limit.get("resets_at")
        if not agents.blocked(limit) or not isinstance(resets, int | float) or resets <= 0:
            return
        if (waiting := self.limit_waits.get(agent)) and not waiting.done():
            waiting.cancel()
        self.limit_waits[agent] = self.background(self.limit_reset(agent, int(resets)))

    async def limit_reset(self, agent: str, at: int):
        """An agent's plan limit's time come: Codex asked again (its rate limits now; used up
        still, its new reset waited for), then what it held goes on (limit_over)."""
        await asyncio.sleep(max(0.0, at - time.time()) + LIMIT_MARGIN)
        if agents.live_limit(self.store, agent).get("resets_at") != at:
            return  # Said again meanwhile (its own wait armed then).
        fresh = await self.read_limit(agent)
        if fresh and agents.limit_status(fresh, agent):
            self.plan_limit_seen(agent, fresh)  # Not over after all.
            return
        await self.limit_over(agent)

    async def read_limit(self, agent: str) -> dict | None:
        """An agent's plan limit as it says it now (Codex: its rate limits read), {} when none
        is used up; None when it can't say (Claude says it only in a worker's turn)."""
        entry = self.agents.get(agent)
        backend = entry.backend if entry else None
        reading: Callable[[], Awaitable[dict | None]] | None = getattr(backend, "plan_limit", None)
        if not callable(reading):
            return None
        try:
            return await asyncio.wait_for(reading(), LOOK_LIMIT_SECONDS)
        except Exception:
            logger.info("Could not read %s's plan limits.", agent, exc_info=True)
            return None

    async def limit_over(self, agent: str):
        """An agent's plan limit reset: the page says it can run, and the work held for it
        (agent_refused_card) goes on, once, as after a sign-in (resume_held)."""
        found = agents.live_limit(self.store, agent)
        if agents.blocked(found):
            self.store.set(f"agentlimit:{agent}", json.dumps(found | {"status": "allowed"}))
        self.publish_state()
        await self.resume_held(agent, frozenset({agents.LIMITED}))

    def continue_checks(self, row: dict, agent: str):
        """Why a worker cannot continue on that agent now (raises), as Continue with… asks."""
        if row["role"] != WORKER or row["closed_at"] or self.special(row):
            raise ValueError("Only an open worker of yours can continue on another agent.")
        if row["status"] in {"working", "starting", "queued"} or self.states[row["id"]].held:
            raise ValueError("It is at work: stop it first, or wait until it is done.")
        if self.is_elsewhere(row):
            raise ValueError("It is open in a terminal: exit it there first.")
        if agent == self.agent(row["id"]):
            raise ValueError(f"It already runs on {agents.display(agent)}.")

    @staticmethod
    def stays_open(checkouts: list[dict]) -> bool:
        """Whether a worker continued elsewhere stays open: its worktrees are copied for the
        new one, never shared; one that writes in the owner's folder (in place) or in a clone
        of its own hands that over, and closes (never two writing one folder)."""
        return not any(
            not checkout.get("worktree")
            and (
                checkout.get("in_place") or (checkout.get("branch") and not checkout.get("shared"))
            )
            for checkout in checkouts
        )

    async def continue_preview(self, session_id: str, agent: str) -> dict:
        """What Continue with <agent>… sends, in one line: its summary's length, its changes
        (files, lines added and removed since it began), and whether this one stays open."""
        if found := self.continued_already(session_id):
            new = self.store.session(found["session_id"])
            name = agents.display(found.get("agent") or self.agent(new["id"]))
            return {
                "line": f"Already continued on {name} by a new worker, “{new['title']}”.",
                "already": new["id"],
            }
        row = self.store.session(session_id)
        self.continue_checks(row, agent)
        summary = await self.handoff_summary(row, self.agent(session_id))
        checkouts = self.workspaces.checkouts(row)
        files, added, removed = 0, 0, 0
        for checkout in checkouts:
            tree = checkout.get("worktree") or (checkout["path"] if checkout.get("branch") else "")
            if not tree:
                continue
            with suppress(Exception):
                base = checkout.get("base_ref") or "HEAD"
                counted = await checkout_git(checkout, tree, "diff", "--numstat", base)
                for line in counted.splitlines():
                    more, fewer, _ = [*line.split("\t", 2), "", ""][:3]
                    files += 1
                    added += int(more) if more.isdigit() else 0
                    removed += int(fewer) if fewer.isdigit() else 0
                new = await checkout_git(
                    checkout, tree, "ls-files", "--others", "--exclude-standard"
                )
                files += len(new.splitlines())
        words = len(summary.split())
        keeps = self.stays_open(checkouts)
        changes = (
            f"its changes ({files} file{'s' if files != 1 else ''}, +{added} −{removed})"
            if files
            else "no changes"
        )
        where = " in a copy of its worktree" if any(c.get("worktree") for c in checkouts) else ""
        line = (
            f"Sends a summary ({words} words) and {changes} to a new "
            f"{agents.display(agent)} worker{where if keeps else ''}; this one "
            + ("stays open." if keeps else "closes: the new one goes on in its folders.")
        )
        return {
            "line": line,
            "words": words,
            "files": files,
            "added": added,
            "removed": removed,
            "stays_open": keeps,
        }

    def continued_already(self, session_id: str) -> dict | None:
        """The new worker this one's work went on with (Continue with…), while nothing new was
        given to this one since: asked again (a double click, a stale card, two tabs), that one
        is pointed to and nothing new is made."""
        try:
            found = json.loads(self.store.get(f"continuedon:{session_id}") or "null")
        except ValueError:
            return None
        if not isinstance(found, dict) or not self.store.find_session(found.get("session_id", "")):
            return None
        tasks = self.tasks.for_session(session_id)
        if (tasks[-1]["id"] if tasks else "") != found.get("last", ""):
            return None  # Given more work since: continuing that is another matter.
        keys = ("session_id", "task_id", "stays_open", "agent")
        return {key: found.get(key) for key in keys} | {"already": True}

    async def continue_on(
        self, session_id: str, agent: str, also: str = "", also_request: str = ""
    ) -> dict:
        """Continue with <agent>… (the owner's, from a worker's ⋯ or by asking the Router): a
        new worker on that agent goes on with this one's work, from a summary of where it
        stands, in a copy of its worktrees: this one stays open. One that writes in the owner's
        folder or a clone of its own hands its folders over and closes (never two writing
        one). Only an idle worker of the owner's; the agent must be able to run it now (else
        why not, nothing changed). `also`: what the Router gave it that did not reach it (its
        agent could not run), which the new one does too. Its open tasks are the new one's now
        (replaced: what waited for them waits for it; none goes on by itself at a limit's
        reset). Once: asked again, the new worker it went on with (continued_already), but
        with `also` (work that is not in that one's). `also_request`: the owner request `also`
        was asked in (its card's): the new task is that request's only when it and every task
        it replaces agree (Teams)."""
        async with self.continue_locks.setdefault(session_id, asyncio.Lock()):
            if not also.strip() and (found := self.continued_already(session_id)):
                return found
            return await self.continue_now(session_id, agent, also, also_request)

    async def continue_now(
        self, session_id: str, agent: str, also: str, also_request: str = ""
    ) -> dict:
        row = self.store.session(session_id)
        self.continue_checks(row, agent)
        current = self.agent(session_id)
        if agent != COPILOT_AGENT:
            await self.new_worker_agent(agent, bool(row["slack"]))  # Raises why it cannot.
        summary = await self.handoff_summary(row, current)
        if also.strip():
            summary += (
                "\n\nThen this, which the Router gave it and which did not reach it (its agent "
                f"could not run): do it too.\n{also.strip()[:3000]}"
            )
        # As it is now (a turn may have begun while the summary was written).
        row = self.store.session(session_id)
        if row["closed_at"]:
            raise ValueError("It was closed meanwhile.")
        self.continue_checks(row, agent)
        new_id = str(uuid4())
        title = row["title"]
        before = self.tasks.for_session(session_id)
        replaced = [task for task in before if task["status"] in OPEN]
        checkouts = self.workspaces.checkouts(row)
        keeps, forked = self.stays_open(checkouts), []
        if keeps:
            workspace, forked = await self.workspaces.fork_checkouts(
                new_id, title, checkouts, carry=not row["slack"]
            )
            for old, new in (
                (o, n) for n in forked for o in checkouts if o.get("repo") == n.get("repo")
            ):
                summary = summary.replace(old["path"], new["path"])
            shared = [c for c in checkouts if not c.get("worktree")]
            handed = {"repos": json.dumps([*forked, *shared]), "project": row["project"] or ""}
            cwd = ([*forked, *shared] or [{"path": str(workspace)}])[0]["path"]
            branch = forked[0]["branch"] if forked else ""
        else:
            handed = {"repos": row["repos"] or "[]", "project": row["project"] or ""}
            workspace, cwd, branch = self.workspaces.workspace(row), row["cwd"], row["branch"]
            # Its checkouts are the new worker's: closing this one removes none of them, nor
            # its workspace (Workspaces.release). It closes first, a turn begun meanwhile with
            # it, and what it left running: its process gone before another writes there.
            self.store.update_session(session_id, repos="[]", project="")
            self.store.set(f"continued:{session_id}", new_id)
            await self.workers.close(session_id, by="you")
            await self.worker_gone(session_id, current)
        self.store.create_session(
            new_id, WORKER, title, "queued", cwd=cwd, branch=branch, slack=row["slack"]
        )
        if agent != COPILOT_AGENT:
            self.store.set(f"agent:{new_id}", agent)
            self.store.set(f"created:{new_id}", "1")
        self.store.set(f"workspace:{new_id}", str(workspace))
        # Its task, folders and permission checks as this one's (Ask first, if chosen for it).
        self.store.update_session(
            new_id,
            task=row["task"] or "",
            project=handed["project"],
            repos=handed["repos"],
            yolo=row["yolo"] or "",
        )
        self.store.set(f"continues:{new_id}", session_id)
        if group := self.store.get(f"compare:{session_id}"):
            self.store.set(f"compare:{new_id}", group)
        # One request only when every piece of work it carries came from it: else none.
        pieces = [self.store.get(f"req:{task['id']}") or "" for task in replaced]
        if also.strip():
            pieces.append(also_request)
        request = pieces[0] if pieces and pieces[0] and len(set(pieces)) == 1 else ""
        task = self.tasks.create(new_id, title, summary[:4000], request=request)
        label = agents.display(agent)
        self.store.set(
            f"continuedon:{session_id}",
            json.dumps({
                "session_id": new_id, "task_id": task["id"], "stays_open": keeps, "agent": agent,
                "last": before[-1]["id"] if before else "",
            }),
        )  # fmt: skip
        await self.task_actions.handed_over(session_id, replaced, task, label)
        self.add_message(
            session_id,
            "notice",
            f"Continued on {label} by a new worker, “{title}”"
            + (", in a copy of its worktree" if forked else "")
            + "; this one stays open."
            if keeps
            else f"Continued on {label} by a new worker, “{title}”, in its folders.",
            {"continued": new_id},
        )
        self.add_message(
            new_id,
            "notice",
            f"Continues “{title}” from a summary of the worker that ran on "
            f"{agents.display(current)} (not its whole conversation).",
            {"continues": session_id},
        )
        # Its first message, as a new worker's task is given (spawn_worker).
        prompt = wrap("router", "task", summary, title=title, task=task["id"])
        self.add_message(new_id, "router", summary, {"prompt": prompt, "task_id": task["id"]})
        self.publish_session(self.store.session(new_id))
        self.background(self.workers.pump())
        agents.used(self.store, agent)
        return {"session_id": new_id, "task_id": task["id"], "stays_open": keeps}

    async def restart_on(
        self, session_id: str, agent: str, model: str = "", effort: str = ""
    ) -> dict:
        """Restart on <agent> (a worker's start line, its agent before its first step): the
        same task, its first message as it was, on another agent; the first worker closes, its
        folders and task handed over. Only a worker of the owner's that has done nothing yet;
        the agent must be able to run it now (else why not, nothing changed). No step to
        confirm: nothing was done to lose."""
        row = self.store.session(session_id)
        if row["role"] != WORKER or row["closed_at"] or self.special(row):
            raise ValueError("Only an open worker of yours can start over on another agent.")
        if not self.store.get(f"untouched:{session_id}"):
            raise ValueError("It has begun its work: use Continue with… instead.")
        if self.is_elsewhere(row):
            raise ValueError("It is open in a terminal: exit it there first.")
        current = self.agent(session_id)
        if agent == current:
            raise ValueError(f"It already runs on {agents.display(agent)}.")
        if agent != COPILOT_AGENT:
            await self.new_worker_agent(agent, bool(row["slack"]))  # Raises why it cannot.
            await self.check_agent_model(agent, model)
        else:
            await self.check_model(model)
        first = next((m for m in self.store.messages(session_id) if m["kind"] == "router"), None)
        task = self.tasks.current(session_id)
        row = self.store.session(session_id)
        if row["closed_at"] or not self.store.get(f"untouched:{session_id}"):
            raise ValueError("It has begun its work meanwhile: use Continue with… instead.")
        new_id, title = str(uuid4()), row["title"]
        handed = {"repos": row["repos"] or "[]", "project": row["project"] or ""}
        self.store.update_session(session_id, repos="[]", project="")
        self.store.set(f"continued:{session_id}", new_id)
        if task:
            self.tasks.hand_to(task["id"], new_id)  # Its task is the new worker's (not ended).
        self.store.delete(f"untouched:{session_id}")
        await self.workers.close(session_id, by="you")
        await self.worker_gone(session_id, current)
        self.store.create_session(
            new_id, WORKER, title, "queued", cwd=row["cwd"], branch=row["branch"],
            slack=row["slack"],
        )  # fmt: skip
        if agent != COPILOT_AGENT:
            self.store.set(f"agent:{new_id}", agent)
            self.store.set(f"created:{new_id}", "1")
            if model or effort:
                self.store.set(
                    f"agentmodel:{new_id}", json.dumps({"model": model, "effort": effort})
                )
        elif model or effort:
            self.store.update_session(new_id, model=model, effort=effort)
        self.store.set(f"workspace:{new_id}", str(self.workspaces.workspace(row)))
        self.store.update_session(
            new_id, task=row["task"] or "", project=handed["project"], repos=handed["repos"],
            yolo=row["yolo"] or "",
        )  # fmt: skip
        if task:
            self.store.execute("UPDATE sessions SET task_id=? WHERE id=?", (task["id"], new_id))
        self.store.set(f"continues:{new_id}", session_id)
        self.store.set(f"untouched:{new_id}", "1")
        if group := self.store.get(f"compare:{session_id}"):
            self.store.set(f"compare:{new_id}", group)
        label = agents.display(agent)
        self.add_message(
            session_id, "notice", f"Restarted on {label} by a new worker.", {"continued": new_id}
        )
        if first:
            meta = dict(first["meta"] or {})
            self.add_message(new_id, "router", first["text"], meta)
        self.add_message(
            self.router_id(),
            "notice",
            f"Restarted “{title}” on {label}",
            {"session_id": new_id, "agent": agent, **({"task_id": task["id"]} if task else {})},
        )
        self.publish_session(self.store.session(new_id))
        self.background(self.workers.pump())
        agents.used(self.store, agent)
        return {"session_id": new_id, **({"task_id": task["id"]} if task else {})}

    async def worker_gone(self, session_id: str, agent: str):
        """Its own process (another agent's that runs one per worker: Claude's) has ended."""
        backend, _ = self.connection(agent)
        ended: Any = getattr(backend, "worker_ended", None)
        if ended is not None:
            await ended(session_id)

    async def handoff_summary(self, row: dict, agent: str) -> str:
        """What the worker that continues another's work is told (its first message): the
        task, where it stands (its last results and words), and its folders' state (branch,
        what changed and is not committed); its earlier conversation is not given."""
        lines = [
            f"You continue the work of another worker, “{row['title']}”, which ran on "
            f"{agents.display(agent)}: the owner moved it to you. You get a summary, not its "
            "conversation: say so in your first reply, in one line.",
            "",
            f"Its task:\n{(row['task'] or row['title']).strip()[:3000]}",
        ]
        tasks = self.tasks.for_session(row["id"])
        if results := [t["result"] for t in tasks if t.get("result")][-2:]:
            lines += ["", "What it reported:", *[f"- {clip_words(r, 1500)}" for r in results]]
        said = [
            m["text"]
            for m in self.store.messages(row["id"])
            if m["kind"] in {"assistant", "owner"} and m["text"]
        ][-4:]
        if said:
            lines += ["", "Its last messages (the owner's and its own):"]
            lines += [f"- {clip_words(text, 800)}" for text in said]
        for checkout in self.workspaces.checkouts(row)[:5]:
            path = checkout.get("path") or ""
            if not path or not await asyncio.to_thread(os.path.isdir, path):
                continue
            state = ""
            with suppress(Exception):
                branch = await checkout_git(checkout, path, "rev-parse", "--abbrev-ref", "HEAD")
                status = await checkout_git(checkout, path, "status", "--short")
                changed = status.strip().splitlines()
                state = f"on {branch.strip()}; " + (
                    f"{len(changed)} uncommitted change(s): "
                    + ", ".join(line[3:] for line in changed[:20])
                    if changed
                    else "nothing uncommitted"
                )
            lines += ["", f"Folder {path}: {state or 'as it was'}."]
        lines += [
            "",
            "Look at the folder (git log, git diff) for what it did before you go on, then go "
            "on with the task and report as any worker does.",
        ]
        return "\n".join(lines)

    async def agent_worktree(self, session_id: str, path: str, removed: bool) -> None:
        """A worktree another agent made for a worker (Claude's EnterWorktree, a subagent's
        isolation): one of its checkouts from then on, as one it made with `git worktree add`
        (receipts, Storage, its folders, removed with the others as it closes); one it
        removed, no longer."""
        row = self.store.find_session(session_id)
        if not row or row["closed_at"]:
            return
        real = await asyncio.to_thread(os.path.realpath, path)
        async with self.states[session_id].checkouts:
            row = self.store.session(session_id)
            checkouts = self.workspaces.checkouts(row)
            paths = await asyncio.to_thread(
                lambda: [os.path.realpath(c.get("path") or "") for c in checkouts]
            )
            if removed:
                kept = [c for c, at in zip(checkouts, paths, strict=True) if at != real]
                if len(kept) != len(checkouts):
                    self.update_session(session_id, repos=json.dumps(kept))
                return
            if real in paths:
                return
            checkout = await self.workspaces.claimed_worktree(row, path)
            if checkout is None:
                self.agent_notice(
                    row,
                    f"Its worktree {path} is not one Chief of Staff can keep as its checkout "
                    "(not of a repository it works in).",
                )
                return
            self.update_session(session_id, repos=json.dumps([*checkouts, checkout]))
        branch = f" on {checkout['branch']}" if checkout.get("branch") else ""
        self.agent_notice(row, f"Made the worktree {path}{branch}: one of its checkouts now.")

    def hold_schedules(self, session_id: str):
        """Stopped (by the owner, the Watchdog, a limit, its spend): its own schedules (Claude's)
        start no turn until the owner tells it to go on (release_schedules)."""
        if self.store.get(f"agentschedules:{session_id}"):
            self.store.set(f"scheduleshold:{session_id}", "1")

    def forget_go_on_at(self, session_id: str):
        """Something else was said to this worker (by the owner, the Router or another worker):
        its tasks no longer go on by themselves as their limit resets (TaskActions.go_on_at)."""
        for task in self.tasks.for_session(session_id):
            if self.store.get(f"goonat:{task['id']}"):
                self.store.delete(f"goonat:{task['id']}")
                self.task_actions.stop_going_on(task["id"])
                self.task_actions.publish(task)

    def release_schedules(self, session_id: str):
        if self.store.get(f"scheduleshold:{session_id}"):
            self.store.delete(f"scheduleshold:{session_id}")

    def drop_schedules(self, session_id: str, delete: bool = True) -> bool:
        """Its task cancelled or set aside, or it closed: its schedules end. Claude would bring
        them back with its conversation (resumed here, or in a terminal): each firing is refused
        here, quietly (their prompts kept), and they are deleted in Claude itself (`delete`: now,
        in the background: delete_dropped_schedules); it is not handed to a terminal until then.
        Whether any is left to delete in Claude."""
        schedules = json.loads(self.store.get(f"agentschedules:{session_id}") or "[]")
        if not schedules:
            return bool(self.store.get(f"schedulesdelete:{session_id}"))
        dropped = json.loads(self.store.get(f"schedulesdropped:{session_id}") or "[]")
        prompts = [str(entry.get("prompt") or "") for entry in schedules]
        self.store.set(f"schedulesdropped:{session_id}", json.dumps([*dropped, *prompts][-50:]))
        self.store.delete(f"scheduleshold:{session_id}")
        pending = json.loads(self.store.get(f"schedulesdelete:{session_id}") or "{}")
        keys = [str(entry.get("id") or "") for entry in schedules]
        ids = {*pending.get("ids", []), *(k for k in keys if k and not k.startswith("wakeup-"))}
        loop = bool(pending.get("loop")) or any(k.startswith("wakeup-") for k in keys)
        self.store.set(
            f"schedulesdelete:{session_id}", json.dumps({"ids": sorted(ids), "loop": loop})
        )
        if callable(drop := getattr(self.states.handle(session_id), "drop_schedules", None)):
            drop()  # Its process forgets them too (agent_schedules then clears them).
        self.agent_schedules(session_id, [])
        if delete:
            self.background(self.delete_dropped_schedules(session_id))
        return True

    async def delete_dropped_schedules(self, session_id: str) -> str:
        """Its dropped schedules (drop_schedules) deleted in Claude, by a turn of Chief of
        Staff's that does only that, once it is not at work: in its process, opened for this if
        none runs (and let go of after). "" once Claude's own tools show them gone (or none was
        left), else why not, said in its chat; tried again as it is next handed to a terminal."""
        key = f"schedulesdelete:{session_id}"
        if not (pending := json.loads(self.store.get(key) or "null")):
            return ""
        if session_id in self.deleting_schedules:
            return "Chief of Staff is deleting them now."
        self.deleting_schedules.add(session_id)
        opened, why = False, ""
        # What lets it happen, by why it did not: its work done; Claude able to run it; or
        # asking it again (or the owner telling it).
        busy = "Stop it, or wait until it is done: they are deleted then."
        unable = "They are deleted once Claude can run it again; then continue in a terminal."
        again = (
            "Chief of Staff asks it again as you try again; or tell it in its chat to delete its "
            "schedules (CronDelete), then try again."
        )
        unblock = again
        try:
            deadline = time.monotonic() + SCHEDULES_DELETE_SECONDS
            while (row := self.store.find_session(session_id)) and not row["closed_at"]:
                if (
                    row["status"] not in {"working", "starting"}
                    and not self.states[session_id].held
                ):
                    break
                if time.monotonic() > deadline:
                    unblock = busy
                    raise RuntimeError("It was still at work.")
                await asyncio.sleep(0.2)
            if not row or row["closed_at"]:
                return "It is closed."
            opened = not self.states.is_live(session_id)
            unblock = unable
            session = await self.session_for(session_id)
            deleting: Any = getattr(session, "delete_schedules", None)
            if deleting is None:
                self.store.delete(key)  # None of its agent's to delete.
                return ""
            self.states[session_id].after_abort = True  # Its turn owes no report.
            unblock = again
            why = await deleting(pending["ids"], bool(pending["loop"]), SCHEDULES_DELETE_SECONDS)
            if why.startswith("It is at work"):
                unblock = busy
            elif why.startswith("Claude was not asked"):
                unblock = unable
        except Exception as exc:
            why = describe(exc)
        finally:
            self.deleting_schedules.discard(session_id)
            if opened:
                with suppress(Exception):
                    await self.detach(session_id)
        if not (row := self.store.find_session(session_id)):
            return why
        if why:
            self.store.set(
                f"schedulesfailed:{session_id}", json.dumps({"why": why, "next": unblock})
            )
            self.agent_notice(
                row,
                f"Its schedules are not deleted in Claude yet: {why} It is not handed to a "
                f"terminal until they are. {unblock}",
            )
        else:
            self.store.delete(key)
            self.store.delete(f"schedulesfailed:{session_id}")
            self.agent_notice(row, "Its schedules are deleted in Claude: none comes back.")
        return why

    def agent_schedules(self, session_id: str, schedules: list[dict]):
        """What another agent's worker scheduled (Claude's CronCreate, ScheduleWakeup): kept
        for its next process (Claude restores them), which admits each firing (its gate)."""
        if schedules:
            self.store.set(f"agentschedules:{session_id}", json.dumps(schedules[:50]))
        else:
            self.store.delete(f"agentschedules:{session_id}")
        if row := self.store.find_session(session_id):
            self.publish_session(row)

    async def agent_peer(self, session_id: str, kind: str, data: dict) -> str | None:
        """Claude's messages between sessions, through Chief of Staff (more-agents.md 5.12.5).
        `send`: the worker's SendMessage; to one of Chief of Staff's workers (`cos-…`,
        `worker:…`, its id), its tell_session (what it was told: Claude does not send it), else
        None (another session: the owner says whether Claude sends it, each time). `inbound`:
        another session's message to it, shown in its timeline and to the Router as that
        session's (by the name it gives itself, never one of Chief of Staff's workers: theirs
        come as tell_session), within the hourly limit of what a worker is told: "" to deliver
        it, else why not."""
        from .peers import MAX_TELLS_PER_HOUR, ROUTER_COPY_CHARS, TellSession, clip

        row = self.store.session(session_id)
        if kind == "send":
            to = str(data.get("to") or "")
            # Its Claude session's name (cos-…), or as Chief of Staff names its workers to them
            # (worker:…, its id): live G8's went to the owner's card as another session's.
            found = re.fullmatch(
                r"cos-([0-9a-f]{8})|(?:worker:|session:)?([0-9a-f]{8}(?:-?[0-9a-f]){0,24})",
                to.strip().lower(),
            )
            prefix = (found.group(1) or found.group(2)).replace("-", "") if found else ""
            targets = [
                other
                for other in self.store.open_sessions(WORKER)
                if found and other["id"].replace("-", "").startswith(prefix)
            ]
            if not found:
                return None
            if len(targets) != 1:
                return f"No worker of Chief of Staff's is named {to} now: find_sessions lists them."
            try:
                told = await self.peers.t_tell_session(
                    TellSession(session_id=targets[0]["id"], text=str(data.get("text") or "")),
                    session_id,
                )
            except ValueError as exc:
                return f"Chief of Staff did not deliver it: {exc}"
            return f"Delivered through Chief of Staff: {told}"
        name = str(data.get("name") or "unnamed")
        if re.fullmatch(r"cos-[0-9a-f]{8}", name):
            name = f"{name}, not a worker of Chief of Staff's"  # A worker's name, given by another.
        key = f"told:session:{name}:{session_id}"
        hour = time.time() - 3600
        told = [t for t in json.loads(self.store.get(key) or "[]") if t > hour]
        if len(told) >= MAX_TELLS_PER_HOUR:
            return (
                f"Claude session “{name}” sent this worker {MAX_TELLS_PER_HOUR} messages this "
                "hour: no more are delivered."
            )
        self.store.set(key, json.dumps([*told, time.time()]))
        text = str(data.get("text") or "").strip()[:6000]
        self.add_message(session_id, "inbound", text, {"claude_session": name})
        self.peers.copy_to_router(
            f"Claude session “{name}” told “{self.display_title(row)}”: "
            f"{clip(text, ROUTER_COPY_CHARS)}",
            {"peer_to": session_id},
        )
        return ""

    def agent_notified(self, session_id: str, text: str):
        """A worker's own notification (Claude's PushNotification): in its timeline, and to
        the owner as its other news is (held while they focus)."""
        row = self.store.session(session_id)
        self.add_message(session_id, "notice", f"Notified you: {text[:500]}")
        self.alert(
            text[:300], session_id=session_id, needs_action=False, title=self.display_title(row)
        )

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
        if getattr(backend, "models_known", True):
            self.agent_models_cache[name] = (time.monotonic(), models)
        return models

    def agent_model_info(self, agent: str, model: str) -> dict | None:
        """One of an agent's models as last listed (its default for ""), as model_info."""
        models = self.agent_models_cache.get(agent, (0.0, []))[1]
        wanted = [m for m in models if (m["id"] == model if model else m["default"])]
        return wanted[0] if wanted else None

    async def look_for_agents(self) -> dict:
        """What Settings' Agents & models page shows, looked for only once the
        owner opens it: each supported agent found on this computer (or on), with its version
        and sign-in or why its workers cannot run here, and its models. One not installed is
        not offered there (the @ list does that): listed only while on, to be turned off."""
        settings = self.store.settings()
        found = [await self.look_for_agent(name) for name in agents.NAMES]
        return {
            "agents": [entry for entry in found if entry["found"] or entry["enabled"]],
            # What it supports, for when none is found ("the Codex CLI").
            "supported": [agents.INSTALL[name] for name in agents.NAMES],
            "new_workers": agents.new_workers_choice(self.store),
            # The agent Last used starts them on now.
            "last_used": self.store.get(agents.LAST_USED) or COPILOT_AGENT,
            "router_chooses": bool(agents.stored(self.store).get(agents.ROUTER_CHOOSES)),
            "sandboxed": bool(settings.worker_sandbox and self.sandbox_host),
            # Chief of Staff's own sessions: where each was put, and where it runs for now and
            # why (or that it waits).
            "roles": [self.role_view(role) for role in agents.ROLES],
            # New workers: the agent they start on now, and why when not the one chosen.
            "workers": self.new_workers_view(),
        }

    def role_view(self, role: str) -> dict:
        """One of Chief of Staff's own sessions as Settings shows it: the agent chosen for it,
        the one it runs on now ("" while it waits) and its model there, and why (role_line)."""
        wanted = agents.role_agent(self.store, role)
        target, why = self.role_target(role) if self.agents else (wanted, "")
        waits = bool(why) and (not target or bool(self.role_wait(role)))
        line = ""
        if why:
            line = (
                f"Waits for {agents.display(wanted)}: {plain_why(why)}"
                if waits
                else f"On {agents.display(target)} for now: {plain_why(why)}"
            )
        return {
            "role": role,
            "label": agents.ROLES[role],
            "chosen": wanted,
            "now": target,
            "now_model": agents.kind_model(self.store, role, target)["model"] if target else "",
            "why": plain_why(why) if why else "",
            "waits": waits,
            "line": line,
            "unavailable": agents.role_unavailable(self.store, role),
        }

    def new_workers_view(self) -> dict:
        """New workers as Settings shows them: the agent chosen, and the one they start on now
        (the next that can, when that can't: why), as far as is known without starting any."""
        chosen = agents.new_workers(self.store)
        why = self.agent_unavailable(chosen)
        now = chosen
        if why:
            now = next(
                (
                    other
                    for other in agents.fallbacks(self.store, chosen)
                    if not self.agent_unavailable(other)
                ),
                "",
            )
        return {
            "chosen": chosen,
            "now": now,
            "now_model": agents.kind_model(self.store, agents.WORKERS, now)["model"] if now else "",
            "why": plain_why(why) if why else "",
        }

    async def look_for_agent(self, name: str) -> dict:
        """One agent's row (look_for_agents), with its status as the @ picker says it."""
        entry = await self.looked_at_agent(name)
        status = self.agent_status(name)
        kept = ("status", "said", "pays", "fix", "ways", "install", "expires", "key_env", "setup")
        if "setup" in status:
            entry["problem"] = ""  # Its Agent SDK: its status says where its setup stands.
        return entry | {key: status[key] for key in kept if key in status}

    async def looked_at_agent(self, name: str) -> dict:
        chosen = agents.configured(self.store).get(name, {})
        entry: dict[str, Any] = {
            "name": name,
            "label": agents.display(name),
            "enabled": bool(chosen.get("enabled")),
            "model": chosen.get("model") or "",
            "effort": chosen.get("effort") or "",
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
            self.glanced[name] = (time.monotonic(), entry["problem"])
            return entry
        self.seen_ready(name, facts)
        models = [described(model) for model in facts["models"]]
        if facts.get("models_known", True):
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
        usage = agents.plan_usage(self.store, name, facts)
        if usage:
            entry["usage"] = usage
        settings = self.store.settings()
        if settings.worker_sandbox and self.sandbox_host:
            # Why its workers can't start while sandboxed, said on its row (none: they can),
            # and what they do differently then.
            entry["sandbox"] = self.sandboxed_refusal(name)
            if note := agents.SANDBOX_NOTES.get(name):
                entry["sandbox_note"] = note
        # Its plan's limit in words whenever it says something (extra usage carrying its workers
        # on, a limit used up with no reset known, a rate limit), but a close one beside the
        # percentages its meter shows.
        live = agents.live_limit(self.store, name, facts.get("limit"))
        close = live.get("status") == "allowed_warning" and bool(usage)
        if not close and (line := agents.limit_line(live, name=name)):
            entry["limit_line"] = line
        return entry

    def check_agents(self, plan: dict | None, settings: Settings):
        """A Settings save as it leaves the agents (`plan`: the Agents & models group saved, or
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
                raise ValueError(f"{label}, Use for workers: {self.not_on_windows(name)}")
            if (
                fresh
                and settings.worker_sandbox
                and self.sandbox_host
                and not agents.can_sandbox(name)
            ):
                raise ValueError(
                    f"{label}, Use for workers: {label} can't run while workers are "
                    f"sandboxed: {SANDBOX_UNPROVEN}."
                )
            for own in agents.folders(name, home):
                for folder in map(Path, settings.sandbox_writable):
                    if not (own.is_relative_to(folder) or folder.is_relative_to(own)):
                        continue
                    if fresh:
                        raise ValueError(
                            f"{label}, Use for workers: first take {folder} out of Also "
                            f"writable in the sandbox: it holds {label}'s settings and hooks."
                        )
                    raise ValueError(
                        f"Also writable in the sandbox: the sandbox cannot open {folder}: it "
                        "holds keys or settings."
                    )

    async def check_agent_models(self, plan: dict):
        """A model newly named for an agent must be one it offers (as check_models does for
        Copilot's), where its models are known: its workers', and each kind of work's on it."""
        named = [
            (name, (plan.get(name) or {}).get("model") or "",
             agents.configured(self.store).get(name, {}).get("model"))
            for name in agents.NAMES
        ]  # fmt: skip
        kinds = plan.get(agents.MODELS) if isinstance(plan.get(agents.MODELS), dict) else {}
        for kind, per_agent in (kinds or {}).items():
            for name, entry in (per_agent or {}).items():
                was = agents.kind_model(self.store, kind, name)["model"]
                named.append((name, (entry or {}).get("model") or "", was))
        for name, model, was in named:
            if not model or model == was or model.lower() == "auto":
                continue
            try:
                listed = (
                    await self.available_models()
                    if name == COPILOT_AGENT
                    else await self.agent_models(name)
                )
                offered = [item["id"] for item in listed]
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
        # The model Settings chose for each (its workers' "Settings: …" choice says which).
        configured = {
            name: config["model"]
            for name, config in agents.configured(self.store).items()
            if name in on and config.get("model")
        }
        return {
            "on": on,
            "names": {name: agents.display(name) for name in on},
            **({"configured_models": configured} if configured else {}),
            "new_workers": agents.new_workers(self.store),
            "router_chooses": agents.router_chooses(self.store),
            # The agent the Router runs on now (the @ list marks it).
            "router_on": (
                self.agent(found) if (found := self.store.get("router_id")) else COPILOT_AGENT
            ),
            # Each one's models' names as last listed (nothing started for them): the page
            # names a model as its agent does, wherever it shows one.
            **({"model_names": names} if (names := self.agent_model_names(on)) else {}),
        }

    def agent_model_names(self, on: list[str]) -> dict[str, dict[str, str]]:
        names = {
            name: {model["id"]: model.get("name") or model["id"] for model in listed}
            for name in on
            if (listed := self.agent_models_cache.get(name, (0.0, []))[1])
        }
        return {name: found for name, found in names.items() if found}

    def agent_status(self, name: str) -> dict:
        """An agent as the @ picker and Settings say it: ready (and whose plan or key it
        uses), its sign-in needed or not installed (and the command that makes it ready, the
        owner's to run), or why it cannot run; whether it is on for workers. From what is known
        (its glance, its process, its last start), nothing run for it."""
        on = (
            agents.copilot_on(self.store)
            if name == COPILOT_AGENT
            else (name in agents.turned_on(self.store))
        )
        entry: dict[str, Any] = {"name": name, "label": agents.display(name), "on": on}
        if name == COPILOT_AGENT:
            return entry | self.copilot_status(on)
        if name not in self.agents_found():
            return entry | self.unready(name, "not installed")
        if name == "claude" and (setup := self.claude_setup.status()):
            return entry | setup  # Its Agent SDK, being set up or not yet.
        seen = self.glanced.get(name, (0.0, None))[1]
        facts = seen if isinstance(seen, dict) else {}
        backend, _ = self.connection(name)
        why = "" if backend and backend.alive() else self.agent_failed.get(name, "")
        if not why and isinstance(seen, str):
            # Its last glance failed: why, unless its backend runs, as it did since then (a
            # start clears it: agent_up), and the glance found no sign-in or install missing.
            alive = backend and backend.alive()
            if not alive or agents.status_of(name, seen)["status"] in agents.HELD:
                why = seen
        if why := self.lost_sign_in.get(name) or why:
            return entry | self.unready(name, why)
        if not facts and not (backend and backend.alive()):
            return entry | {"status": agents.CHECKING, "said": agents.STATUS_WORDS["checking"]}
        live = agents.live_limit(self.store, name, getattr(backend, "limit", None))
        if limited := agents.limit_status(live, name):
            # Its plan's limit used up: until when (the page says it in its own time).
            pays = agents.pays(name, facts.get("account", ""), facts.get("key_env", ""))
            return entry | limited | ({"pays": pays} if pays else {})
        return entry | {
            "status": agents.READY,
            "said": agents.STATUS_WORDS[agents.READY],
            "pays": agents.pays(name, facts.get("account", ""), facts.get("key_env", "")),
            # When its sign-in ends (the page warns a week before), and a key it uses instead.
            **({"expires": facts["expires"]} if facts.get("expires") else {}),
            **({"key_env": facts["key_env"]} if facts.get("key_env") else {}),
            # Signing in again is the owner's to ask for (Settings: Sign in again).
            "ways": agentsetup.sign_in_methods(name),
        }

    def copilot_status(self, on: bool) -> dict:
        """Copilot as the @ picker and Settings say it: ready, starting, or why it can't run
        (its sign-in, not installed, its start), with the command that makes it ready; off,
        as found or not."""
        ready = {"status": agents.READY, "said": "Ready", "pays": agents.COPILOT_PAYS}
        if not on:
            return ready if self.copilot_found() else agents.status_of(COPILOT_AGENT, "not found")
        if (why := self.copilot_problem) == COPILOT_STARTING:
            return {"status": agents.CHECKING, "said": "Starting…"}
        if why:
            found = agents.status_of(COPILOT_AGENT, why)
            return found | ({"said": "Can't start"} if found["status"] == agents.PROBLEM else {})
        if self.backend is None:
            return {"status": agents.CHECKING, "said": "Starting…"}
        return ready

    @staticmethod
    def unready(name: str, why: str) -> dict:
        """An agent that cannot run, as agents.status_of says it, with what the page can do for
        it: its ways to sign in (agentsetup.SIGN_IN) or its installers (the official one
        first), each its own command."""
        found = agents.status_of(name, why)
        if found["status"] == agents.SIGN_IN:
            found["ways"] = agentsetup.sign_in_methods(name)
        elif found["status"] == agents.MISSING:
            found["install"] = agentsetup.install_methods(name)
        return found

    def agents_status(self) -> list[dict]:
        """Every agent's status (agent_status), Copilot first: only where another agent is
        installed or on (with Copilot alone, nothing of agents is in the page)."""
        if not (self.agents_found() or agents.turned_on(self.store)):
            return []
        return [self.agent_status(name) for name in (COPILOT_AGENT, *agents.NAMES)]

    async def refresh_agent_status(self) -> list[dict]:
        """The page asks (the @ picker opened, the window focused, Settings shown): each
        installed agent glanced at again (its version and sign-in, in its own folder of Chief
        of Staff's) unless it was within STATUS_SECONDS (UNREADY_SECONDS for one that could not
        run: signed in meanwhile, it turns Ready at once; later each time for one whose look
        failed otherwise), also while its workers run (signed out meanwhile, it says so before
        their next turn fails); its status then. The looks run at once, one per agent, waited
        for REFRESH_WAIT at most (a slower one's change is published as it ends). One being
        signed in is looked at by its sign-in (agentsetup.POLL_SECONDS)."""
        if time.monotonic() - self.found_cache[0] > UNREADY_SECONDS:
            self.found_cache = (float("-inf"), [])  # Installed meanwhile: found now.
        looks = []
        for name in self.agents_found():
            at = self.glanced.get(name, (float("-inf"), None))[0]
            if time.monotonic() - at < self.look_every(name) or self.setups.running(name):
                continue
            if name == "claude" and self.claude_setup.running():
                continue  # Looked at once set up (claude_setup_changed).
            looks.append(self.glance_soon(name))
        if looks:
            await asyncio.wait(looks, timeout=REFRESH_WAIT)
        return self.agents_status()

    def look_every(self, name: str) -> float:
        """How long an agent's last look stands when the page asks (refresh_agent_status)."""
        status = self.agent_status(name)["status"]
        if status in {agents.READY, agents.CHECKING, agents.LIMITED}:
            return STATUS_SECONDS
        if status in agents.HELD:
            return UNREADY_SECONDS
        return min(PROBLEM_SECONDS, UNREADY_SECONDS * 2 ** self.glance_failures.get(name, 0))

    def glance_soon(self, name: str) -> asyncio.Task:
        """An agent's look, one at a time: asked for while one runs, that one."""
        task = self.glancing.get(name)
        if task is None or task.done():
            task = self.background(self.quiet_glance(name))
            self.glancing[name] = task
        return task

    async def quiet_glance(self, name: str):
        with suppress(Exception):
            await self.glance_agent(name)  # (Why it cannot run is its status.)

    async def glance_agent(self, name: str) -> dict:
        """An agent's glance (agents.glance), kept as its status: raises with why it cannot
        run, kept too. The page is told where its status changed."""
        before = self.agent_status(name)["status"]
        try:
            facts = await agents.glance(name, self.agent_folder(name))
        except Exception as exc:
            said = describe(exc)
            self.glanced[name] = (time.monotonic(), said)
            if agents.status_of(name, said)["status"] in agents.HELD:
                self.glance_failures.pop(name, None)
                failed = self.agent_failed.get(name)
                if failed and agents.status_of(name, failed)["status"] in agents.HELD:
                    # Why its last start failed (not installed, say) is past: this look's now.
                    self.agent_failed[name] = f"{agents.display(name)} could not start: {said}"
            else:
                self.glance_failures[name] = self.glance_failures.get(name, 0) + 1
            self.agent_down(name)
            self.status_changed(name, before)
            raise
        self.glance_failures.pop(name, None)
        self.seen_ready(name, facts)
        self.status_changed(name, before)
        return facts

    def status_changed(self, name: str, before: str):
        """The page told of an agent's status changed by a look: one it showed (Ready, Sign-in
        needed, …), not its first look's (Checking…, as at startup: the page reads it then)."""
        if before != agents.CHECKING and self.agent_status(name)["status"] != before:
            self.publish_state()

    async def looked_again(self, name: str) -> str:
        """Before work goes to an agent whose sign-in a turn lost, or that the page says is
        signed out while its process runs all the same (one Codex app-server for all its
        workers; Claude's of a worker at work: never told, it would take the work and fail):
        one fresh look. Why it still can't (the turn's words, or its look's), else "" (signed
        in meanwhile, it runs; not running, its start finds out itself)."""
        if name not in agents.NAMES:
            return ""
        backend, _ = self.connection(name)
        signed_out = self.agent_status(name)["status"] == agents.SIGN_IN
        if name not in self.lost_sign_in and not (signed_out and backend and backend.alive()):
            return ""
        try:
            await self.glance_agent(name)
        except Exception as exc:
            if lost := self.lost_sign_in.get(name):
                return lost
            if agents.status_of(name, said := describe(exc))["status"] == agents.SIGN_IN:
                self.agent_start_failed(name, exc)
                return said
        return ""

    def seen_ready(self, name: str, facts: dict):
        """An agent's glance or look found it signed in: its status, and a start that failed
        for its sign-in (or as not installed) is past, as is a sign-in a turn lost."""
        kept = {key: facts[key] for key in ("expires", "key_env") if facts.get(key)}
        self.glanced[name] = (
            time.monotonic(),
            {"version": facts.get("version", ""), "account": facts.get("account", ""), **kept},
        )
        failed = self.agent_failed.get(name)
        if failed and agents.status_of(name, failed)["status"] in {agents.SIGN_IN, agents.MISSING}:
            self.agent_failed.pop(name, None)
        self.lost_sign_in.pop(name, None)
        self.agent_up(name)

    def agent_up(self, name: str):
        """An agent seen able to run (its glance or look, a start), the first time since it
        could not: what waited on it goes on (agent_back), and the page says Ready (told only
        where it was seen unable to before: at startup, nothing changed for the page)."""
        if name in self.ready_seen or name not in agents.NAMES:
            return
        self.ready_seen.add(name)
        if isinstance(self.glanced.get(name, (0.0, None))[1], str):
            self.glanced.pop(name, None)  # (A glance that failed before: past.)
        if name in self.down_seen:
            self.down_seen.discard(name)
            self.publish_state()
        if self.store.items("agentrefusal:") or (
            self.agents and name in agents.roles(self.store).values()
        ):
            self.background(self.agent_back(name))

    def agent_down(self, name: str):
        """An agent seen unable to run (signed out, not installed, …): seen able again, what
        waits on it goes on (agent_up)."""
        self.ready_seen.discard(name)
        self.down_seen.add(name)

    def sign_in_lost(self, agent: str, message: str):
        """A turn of an agent's session failed for its sign-in (signed out, expired): it says
        Sign-in needed now, wherever its status shows, until a look finds it signed in."""
        if agent not in agents.NAMES or not agents.NEEDS_SIGN_IN.search(message):
            return
        self.lost_sign_in[agent] = message[:300]
        self.glanced[agent] = (time.monotonic(), message[:300])
        self.agent_down(agent)
        self.publish_state()

    async def agent_back(self, name: str, held: frozenset[str] = agents.HELD):
        """An agent that could not run can now (signed in, installed; `held`: why the work it
        held waited): that work goes on (resume_held), and a role of Chief of Staff's put on it
        moves back there at once."""
        await self.resume_held(name, held)
        if not self.agents:
            return
        for role in PLACED_ROLES:
            if agents.role_agent(self.store, role) != name:
                continue
            row = self.role_row(role)
            if row is None or (self.agent(row["id"]) == name and not self.role_wait(role)):
                continue
            self.clear_role_holds(role, name)
            try:
                await self.place_role(role)
            except Exception:
                logger.warning("Could not put the %s back on %s.", role, name, exc_info=True)

    def agents_found(self) -> list[str]:
        """The other agents installed here (their CLI on the PATH: nothing runs), looked at a
        minute apart at most; none on Windows, nor in the demo (its own world)."""
        if getattr(self, "demo", False):
            return []
        at, found = self.found_cache
        if time.monotonic() - at > FOUND_SECONDS:
            found = agents.found()
            self.found_cache = (time.monotonic(), found)
        return found

    async def offer_agents(self):
        """Each agent installed and signed in that the owner was never asked about: a first-run
        card on Home (agent_offers), once a glance at it (its CLI's version and sign-in, in Chief
        of Staff's own folder: agents.glance) finds it can run. Its full look (an app-server,
        its models and limits) waits until the owner opens Settings → Agents & models."""
        on = agents.turned_on(self.store)
        for name in self.agents_found():
            if name in on or self.store.get(f"{agents.OFFERED}{name}") or name in self.offers:
                continue
            try:
                facts = await self.glance_agent(name)
            except Exception as exc:
                logger.info("No first-run card for %s: %s", name, describe(exc))
                continue
            if self.store.get(f"{agents.OFFERED}{name}"):
                continue  # Decided on meanwhile.
            signed = agents.SIGNED_IN.get(facts["account"], "signed in")
            note = agents.PLAN_NOTES.get(facts["account"])
            self.offers[name] = {
                "name": name,
                "label": agents.display(name),
                "state": f"{facts['version']} · {signed}",
                **({"note": note} if note else {}),
            }
            self.publish_state()

    def agent_offers(self) -> list[dict]:
        on = agents.turned_on(self.store)
        return [offer for name, offer in self.offers.items() if name not in on]

    async def answer_offer(self, name: str, allow: bool) -> dict:
        """The owner's answer on an agent's first-run card: Allow turns it on (as Settings
        would; new workers stay on Copilot), Not now leaves it off. Asked once either way."""
        if name not in agents.NAMES:
            raise ValueError(f"Chief of Staff does not run {name!r}.")
        stopped: list[str] = []
        if allow:
            plan = agents.planned(self.store, {name: {"enabled": True}})
            self.check_agents(plan, self.store.settings())
            before = agents.configured(self.store)
            if agents.save(self.store, plan):
                stopped = await self.agents_saved(before)
        self.store.set(f"{agents.OFFERED}{name}", "allowed" if allow else "not now")
        self.offers.pop(name, None)
        self.publish_state()
        return {"stopped": stopped}

    def agent_view(self, row: dict, agent: str) -> dict:
        """What a worker's view says of the agent it runs on, when that is not Copilot: its
        model there, its tokens (no credits), what it does differently and why it cannot ask
        first, where that applies."""
        own = self.agent_choice(row["id"])
        backend, _ = self.connection(agent)
        running = getattr(self.states.handle(row["id"]), "current_model", "")
        kind = agents.kind_of(row["role"], self.special(row))
        chosen = agents.kind_model(self.store, kind, agent)
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
            "token_limit": 0 if self.role_key(row) else agents.limit(self.store, agent),
            "agent_notes": notes(not self.yolo(row), self.sandboxed(row))
            if (notes := getattr(backend, "notes_for", None))
            else [],
            "ask_first_refused": getattr(backend, "ask_first_refused", "") if backend else "",
        }

    def chosen_model(self, row: dict) -> str:
        """The model chosen for the session on Copilot: its own, its kind of work's (Watch for
        you's and the helpers' their own), or the default ("" for Copilot's)."""
        settings = self.store.settings()
        if kind := self.helper_kind(row):
            own = agents.kind_model(self.store, kind, COPILOT_AGENT, settings)["model"]
            return row.get("model") or own or settings.model
        return row.get("model") or settings.role_models.get(row["role"]) or settings.model

    def helper_kind(self, row: dict) -> str:
        """Watch for you's kind or the helpers' for one of Chief of Staff's helper workers ("" for
        any other session): their model and effort are their own (agents.kind_model)."""
        if row["role"] != WORKER or not row.get("id"):
            return ""
        kind = agents.kind_of(WORKER, self.special(row))
        return kind if kind in {"connector", "helpers"} else ""

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
        if kind := self.helper_kind(row):
            own = agents.kind_model(self.store, kind, COPILOT_AGENT)["effort"]
        else:
            own = self.store.settings().role_efforts.get(row["role"], "")
        effort = row.get("effort") or own
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
            "model. Choose another in Settings → Agents & models or in this worker's header.",
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
        """After a turn: reconnect with current options, and go on if it was told it would;
        what waited for its new session (sent while its sandbox went on) goes to it there."""
        note, self.states[session_id].after_refresh = self.states[session_id].after_refresh, ""
        waited = self.states[session_id].confined
        await self.detach(session_id)
        row = self.store.find_session(session_id)
        if waited and not note and row and not row["closed_at"]:
            self.background(self.delivery.resume(session_id))
        if note and row and not row["closed_at"]:
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
        state.confined = False  # Its next connection is sandboxed from the start.
        state.after_refresh = ""  # The next connection has the new options from the start.
        state.steps.clear()  # A disconnected session is in no step.
        state.mcp_calls.clear()
        state.unread.clear()  # What it was sent is resent or replayed, not waiting in a turn.
        state.unread_orders.clear()
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
        """Copilot's models (none while Chief of Staff runs without Copilot)."""
        if self.backend is None and self.copilot_down():
            return []
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
        if row["role"] == SLACK or row["slack"]:
            # Slack's read tools as Slack's server describes them, for slack_read_tools.
            await self.slack.read_proxy()
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
            if row["role"] == WORKER and self.shepherd.marked(row["id"]):
                # A shepherd step's profile is Copilot's only (Shepherd.executor_refusal): never
                # this worker's own options in its place.
                raise RuntimeError(
                    f"A fix is never prepared on {agents.display(agent)}: it runs on Copilot only."
                )
            if agent == "claude":
                # As a Copilot worker's: Claude loads its folder's settings as its process
                # starts (a resume too), so not before the owner says to use them.
                if line := await self.settings_refusal(row):
                    raise SettingsChangedError(line)
                await self.ready_caches(row)
            # Chief of Staff's own sessions that need only its own tools: nothing else there.
            placed = self.role_key(row) in PLACED_ROLES or self.special(row) in TOOLLESS
            options = self.role_options(row) if placed else self.agent_options(row)
            trusted = {}
            if (
                agent == "claude"
                and row["role"] == WORKER
                and options.get("sandbox")
                and (script := await self.cache_script(row))
            ):
                # Its commands' caches, as a Copilot worker's: the same init script, which
                # Claude sources before each command (CLAUDE_ENV_FILE).
                options["env_file"] = str(script)
        # A helper's fresh run on another agent has an id of its own (Tools.rerun_special).
        rerun = self.store.get(f"rerun:{row['id']}") if create and agent != COPILOT_AGENT else ""
        copilot_id = copilot or rerun or self.copilot_id(row["id"])
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
                # It took the id it was given (Claude's): made, as made() reads an agent's row.
                self.store.set(f"copilot:{row['id']}", copilot_id)
            if rerun:
                self.store.delete(f"rerun:{row['id']}")
            if agent == COPILOT_AGENT:
                # Copilot keeps a session only once it has had a message: until then it cannot
                # be resumed after a restart, and is created again instead.
                self.store.set(f"fresh:{copilot_id}", "1")
        else:
            if agent == COPILOT_AGENT and self.store.get(f"toolarge:{copilot_id}"):
                # Its resume killed Copilot's connection: never tried again (too_large).
                raise TooLargeError(TOO_LARGE)
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
                if agent == COPILOT_AGENT and compat.mismatch(exc):
                    return await self.continue_fresh(row, copilot_id, exc)
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
        if agent == COPILOT_AGENT and row["role"] == WORKER and "sandbox_config" in options:
            # (A worker's sandbox, or its shepherd step's.)
            await self.apply_cache_env(row, session)
        session_id = row["id"]
        loop = self.loop or asyncio.get_running_loop()

        def forward(event: Any):
            # The SDK calls this from its reader thread; events are handled on the loop.
            loop.call_soon_threadsafe(self.events.on_event, session_id, event, session)

        session.on(forward)
        self.states[session_id].handle = session
        if agent in {COPILOT_AGENT, "claude"} and row["role"] == WORKER:
            self.servers_opened[session_id] = frozenset(self.servers_off.get(session_id, {}))
            self.searched_opened[session_id] = self.all_searched(row)
            if "hooks" in options and agent == COPILOT_AGENT:
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

    def too_large(self, session_id: str) -> bool:
        """Whether its Copilot session is too large for Copilot to reopen here (once its resume
        killed Copilot's connection: mark_too_large). Never resumed then: it waits for the
        owner's Start fresh, which a fresh session goes on with (start_fresh)."""
        return bool(self.store.get(f"toolarge:{self.copilot_id(session_id)}"))

    def mark_too_large(self, copilot_id: str, size: int):
        """Copilot's connection died after this Copilot session was resumed or read (backend
        culprit), and its log is big, or it did twice: too large for Copilot to reopen here."""
        row = self.store.find_session(self.row_id(copilot_id))
        if not row or row["closed_at"] or self.copilot_id(row["id"]) != copilot_id:
            return
        if row["role"] != WORKER:
            return  # (The Router's goes on in a fresh session as its context grows.)
        why = f" (a log of {size:,} bytes)" if size else " twice"
        logger.warning("Copilot stopped answering after resuming %s%s: it is not resumed again.",
                       row["id"], why)  # fmt: skip
        self.store.set(f"toolarge:{copilot_id}", str(size))
        self.hold_too_large(row)

    def hold_too_large(self, row: dict):
        """A session too large for Copilot to reopen here, at rest: said once in its chat, with
        Start fresh (Delivery.fresh_needed); what it is sent waits for that."""
        self.states[row["id"]].busy = False
        self.delivery.fresh_needed(row, TOO_LARGE)
        if row["role"] == WORKER:
            values: dict[str, Any] = {"activity": TOO_LARGE_WAIT}
            if row["status"] in {"working", "starting"}:
                values["status"] = "idle"  # Nothing of it runs: it was to go on.
            self.update_session(row["id"], **values)
        elif row["status"] in {"working", "starting"}:
            self.update_session(row["id"], status="idle", activity="")

    async def continue_fresh(self, row: dict, old: str, error: BaseException):
        """Its Copilot session could not be resumed for a version mismatch (compat.MISMATCHES:
        the extensions it asks for, even declined; a method, a parameter or a session format
        this Copilot doesn't know): a fresh one goes on as the same Chief of Staff
        session (its timeline, tasks and folders stay), told what it needs from Chief of Staff's
        records; what it was not given yet goes with that. Once: a fresh one failing so too is
        said as it is. The caller holds its lifecycle lock."""
        session_id = row["id"]
        if self.store.get(f"continued:{session_id}") == old:
            raise error
        logger.warning("%s session %s could not be resumed (%s): a fresh one goes on.",
                       row["role"], session_id, describe(error))  # fmt: skip
        new = str(uuid4())
        self.store.set(f"alias:{old}", session_id)
        self.store.set(f"alias:{new}", session_id)  # Its events may arrive during creation.
        before = self.store.session(session_id)["activity"]
        if row["role"] == WORKER:
            self.update_session(session_id, activity=RECONNECTING)
        try:
            session = await self.open_live(row, create=True, copilot=new)
        except Exception:
            self.store.delete(f"alias:{new}")
            raise
        finally:
            if row["role"] == WORKER and self.store.session(session_id)["activity"] == RECONNECTING:
                self.update_session(session_id, activity=before)
        self.store.set(f"copilot:{session_id}", new)
        self.store.set(f"continued:{session_id}", new)
        self.store.set(f"context:{session_id}", "0")
        if row["role"] == ROUTER:
            self.store.delete(f"rotated:{session_id}")  # Nothing to go back to.
            preamble = self.handover("that could not be resumed", upto_sent=True)
        else:
            preamble = self.continuation(row)
        self.store.set(f"preamble:{session_id}", preamble)
        other = compat.mismatch(error) not in ("", "extensions")
        self.add_message(session_id, "notice", CONTINUED_AFRESH if other else CONTINUED_FRESH)
        return session

    async def start_fresh(self, session_id: str) -> str:
        """The owner's Start fresh, where a session Copilot can't reopen, nor a fresh one go on
        for, said so (Delivery.fresh_needed): a fresh one goes on for it as continue_fresh's
        does, and gets what was kept for it."""
        row = self.store.session(session_id)
        if row["closed_at"]:
            raise ValueError("That session is closed.")
        async with self.states[session_id].lifecycle:
            if not self.states.handle(session_id):
                self.store.delete(f"continued:{session_id}")  # The owner's word: once more.
                try:
                    await self.continue_fresh(
                        row, self.copilot_id(session_id), RuntimeError("Started fresh.")
                    )
                except Exception as exc:
                    logger.warning("Could not start %s fresh.", session_id, exc_info=True)
                    raise RuntimeError(
                        "Copilot couldn't start a fresh session for it; try again in a while."
                    ) from exc
        self.delivery.fresh_started(session_id)
        if self.store.session(session_id)["activity"] == TOO_LARGE_WAIT:
            self.update_session(session_id, activity="")
            if (task := self.tasks.current(session_id)) and task["status"] == "running":
                # It was at work: its fresh session goes on with that (told where it was).
                note = "Continue the work of your earlier session where it left off."
                await self.delivery.deliver(
                    session_id,
                    wrap("system", "restart", note),
                    kind="notice",
                    display="Started a fresh session; asked it to continue.",
                )
                return "Started a fresh worker with its context."
        if row["role"] != WORKER:
            if self.delivery.has_unsent(session_id):
                self.delivery.schedule_flush(session_id, 0)
        elif self.store.session(session_id)["status"] in {"starting", "working"}:
            # At work when Copilot refused it: what it was kept goes on in its slot.
            if await self.delivery.flush(session_id, await self.session_for(session_id)):
                self.update_session(session_id, status="working")
        else:
            self.states[session_id].now = True  # As the owner's Send now.
            try:
                await self.delivery.resume(session_id, now=True)
            finally:
                if self.store.session(session_id)["status"] != "queued":
                    self.states[session_id].now = False
        who = "worker" if row["role"] == WORKER else "session"
        return f"Started a fresh {who} with its context."

    def continuation(self, row: dict) -> str:
        """What a fresh session of a worker (or Watchdog, Slack) needs from Chief of Staff's
        records to go on where its earlier one was."""
        lines = [
            "You go on from an earlier session of yours that could not be resumed. This is "
            "where it was; what it did stays done: don't do it again."
        ]
        lines.append(f"This session: “{row['title']}” ({row['role']}).")
        if task := self.tasks.current(row["id"]):
            lines.append(
                f"Its task {task['id']} “{task['title']}” ({task['status']}): "
                f"{clip_words(task['goal'], 2000)}"
            )
            if task["progress"]:
                lines.append(f"Its progress: {clip_words(task['progress'], 1000)}")
            if task["result"]:
                lines.append(f"Its last report: {clip_words(task['result'], 1000)}")
        if row["role"] == WORKER:
            for checkout in self.workspaces.checkouts(row):
                if path := checkout.get("path"):
                    branch = f" (branch {checkout['branch']})" if checkout.get("branch") else ""
                    lines.append(f"Checkout: {path}{branch}")
        if row["cwd"]:
            lines.append(f"Working folder: {row['cwd']}")
        return wrap("system", "handover", "\n".join(lines))

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
            settings_guard.keep_shapes(self.store, top, now)
            return top, cwd, checkout, now, "", code
        except settings_guard.TooLargeError as exc:
            return top, cwd, checkout, {}, str(exc), []

    def settings_inert(
        self, top: Path, snapshot: dict, now: dict[str, dict], then: dict[str, dict]
    ) -> bool:
        """Of the keyed settings files (settings.json) that differ from what was taken, those
        whose only change is in keys that run nothing (settings_guard.inert_change) are taken
        as they are now. Whether any was. Blocking (it reads them)."""
        changed = [
            (folder, entry)
            for folder in now.keys() | then.keys()
            for entry in (now.get(folder, {}).keys() | then.get(folder, {}).keys())
            if settings_guard.keyed(entry)
            and now.get(folder, {}).get(entry) != then.get(folder, {}).get(entry)
        ]
        if not changed:
            return False
        objects: dict[tuple[str, str], dict | None] = {}
        for folder, entries in now.items():
            place = top if folder == "." else top / folder
            for entry, value in entries.items():
                if settings_guard.keyed(entry):
                    objects[(folder, entry)] = (
                        settings_guard.read_keyed(place / entry, value[1])
                        if value[0] == "f"
                        else None
                    )
        marketplaces: dict = {}
        for found in objects.values():
            theirs = (found or {}).get(settings_guard.MARKETPLACES)
            if isinstance(theirs, dict):
                for name, definition in theirs.items():
                    # (Defined twice, differently: as no definition of the owner's.)
                    marketplaces[name] = (
                        definition if marketplaces.get(name, definition) == definition else None
                    )
        owner = self.owner_agent_settings()
        taken = False
        for folder, entry in changed:
            value, was = now.get(folder, {}).get(entry), then.get(folder, {}).get(entry)
            if (value and value[0] != "f") or (was and was[0] != "f"):
                continue
            found = objects.get((folder, entry)) if value else {}
            before = self.settings_shape(was[1]) if was else {}
            if found is None or not settings_guard.inert_change(
                settings_guard.keyed(entry),
                before,
                found,
                lambda found: settings_guard.plugins_inert(found, owner, marketplaces),
            ):
                continue
            kept = snapshot["folders"].setdefault(folder, {})
            if value:
                kept[entry] = value
            else:
                kept.pop(entry, None)
            taken = True
        return taken

    def settings_shape(self, digest: str) -> dict[str, str] | None:
        raw = self.store.get(settings_guard.SHAPE + digest)
        try:
            found = json.loads(raw) if raw else None
        except ValueError:
            return None
        return found if isinstance(found, dict) else None

    @staticmethod
    def owner_agent_settings() -> dict:
        """The owner's own Copilot user settings (settings.json, its whole-line comments left
        out), as its workers' Copilot reads them; {} when there are none to read."""
        try:
            text = read_regular(copilot_home() / "settings.json", 1 << 20).decode()
            found = json.loads(
                "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
            )
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    async def settings_proven(
        self,
        top: Path,
        checkout: dict | None,
        names: tuple[str, ...],
        now: dict[str, dict],
        snapshot: dict,
        folders: list[str],
    ) -> bool:
        """Of the settings first seen in a folder workers had before Chief of Staff kept its
        snapshot (an "untracked" one: handed out by an earlier version, or while the sandbox was
        off), those byte for byte the owner's are taken as unchanged (bug E122): as the owner's
        checkout's snapshot holds them, or as the commit the checkout was made from has them
        (workspaces.Committed). A setting wholly or not at all; never one with a link, a special
        file, a program or a folder it runs. Whether any was."""
        wanted = {
            folder: [name for name in snapshot["first"].get(folder, []) if name in names]
            for folder in folders
        }
        if not snapshot.get("untracked") or not any(wanted.values()) or not checkout:
            return False
        theirs = None
        owner = checkout.get("repo") if checkout.get("worktree") else None
        if owner and not checkout.get("remote"):
            real = await self.settings_thread(os.path.realpath, owner)
            if real != await self.settings_thread(os.path.realpath, top):
                theirs = settings_guard.load(self.store, Path(real))
        base = checkout.get("base_ref") or checkout.get("settings_base") or ""
        try:
            committed = Committed(checkout, str(top), base)
        except ValueError:
            committed = None
        proven = False
        for folder, found in wanted.items():
            for name in found:
                entries = settings_guard.restricted({folder: now.get(folder, {})}, (name,))
                entries = entries.get(folder, {})
                tried = f"{top}\0{folder}\0{name}"
                seen = settings_guard.digest({folder: entries})
                if (
                    not entries
                    or self.settings_tried.get(tried) == seen
                    or not all(
                        value[0] == "f" or (value[0] == "-" and settings_guard.ran_of(entry)[1])
                        for entry, value in entries.items()
                    )
                ):
                    continue
                if (theirs and self.owner_holds(theirs, folder, name, entries)) or (
                    committed and await self.as_committed(committed, top, folder, name, entries)
                ):
                    settings_guard.taken(snapshot, {folder: entries}, [folder], (name,))
                    self.settings_tried.pop(tried, None)
                    proven = True
                else:
                    self.settings_tried[tried] = seen  # (Not read again while they stay so.)
        return proven

    @staticmethod
    def owner_holds(theirs: dict, folder: str, name: str, entries: dict) -> bool:
        """Whether the owner's checkout's snapshot holds a setting so, as taken there."""
        if name not in theirs["known"].get(folder, []) or any(
            name in theirs.get(marks, {}).get(folder, []) for marks in ("first", "carried")
        ):
            return False
        kept = settings_guard.restricted({folder: theirs["folders"].get(folder, {})}, (name,))
        return kept.get(folder, {}) == entries

    async def as_committed(
        self, committed: Committed, top: Path, folder: str, name: str, entries: dict
    ) -> bool:
        """Whether a setting's files (and those its commands run) are, every byte, what the
        commit has there, and the commit has no other file under it."""
        prefix = "" if folder == "." else f"{folder}/"
        try:
            there = await committed.files(prefix + name)
            own: dict[str, list] = {}
            for entry, value in entries.items():
                setting, ran, _ = settings_guard.ran_of(entry)
                if not ran:
                    if "\0" in setting:
                        return False
                    own[prefix + setting] = value
                elif value[0] == "f":
                    if Path(ran).is_absolute() or ".." in Path(ran).parts:
                        return False
                    found = await committed.find(ran)
                    if not found or not await self.blob_is(committed, top / ran, value, found):
                        return False
            if set(own) != set(there):
                return False
            for path, value in own.items():
                if not await self.blob_is(committed, top / path, value, there[path]):
                    return False
        except (ValueError, OSError, settings_guard.SettingsBusyError):
            return False
        return True

    async def blob_is(
        self, committed: Committed, path: Path, value: list, found: tuple[str, str]
    ) -> bool:
        """Whether a file, as its snapshot entry says it is now, is that committed file."""
        if found[0] not in {"100644", "100755"}:
            return False

        def read() -> str:
            data = settings_guard.read_regular(path, settings_guard.MOST_BYTES)
            if hashlib.sha256(data).hexdigest() != value[1]:
                return ""  # (Changed since it was read.)
            return blob_id(data, committed.long)

        return await self.settings_thread(read) == found[1]

    async def settings_verdict(self, row: dict, names: tuple[str, ...]) -> dict | None:
        """What of these agent settings (one agent's) differs in a worker's folder from what
        Chief of Staff last saw: {"top", "cwd", "changes", "now", "digest", "unreadable",
        "first" (only settings seen there for the first time), "refused" (some it can never
        take as unchanged), "folders" (where they differ)}, or None for nothing. Read from the
        files every time: they are few and small. A folder first handed out (no worker had it,
        and this one has run nothing there yet) is kept as it is: the owner's. Any other folder
        (or setting) seen for the first time is known from then on with nothing in it taken:
        what is there is the owner's to look at once."""
        top, cwd, checkout, now, unreadable, code = await self.settings_thread(
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
        if await self.settings_proven(top, checkout, names, now, snapshot, folders):
            settings_guard.save(self.store, top, snapshot)
        then = settings_guard.restricted(
            {folder: snapshot["folders"].get(folder, {}) for folder in folders}, names
        )
        now_named = settings_guard.restricted(now, names)
        self.settings_code(row, top, snapshot, code, names)
        # What a Copilot server runs turns that server off when it changed (servers_off); a
        # Codex thread does not trust the folder's settings then, as for any of them.
        servers = names == CODEX_SETTINGS
        if names in (REPOSITORY_SETTINGS, CLAUDE_SETTINGS):
            self.servers_seen(row, settings_guard.servers_changed(now_named, then))
        found = settings_guard.changes(now_named, then, servers)
        if found and await self.settings_thread(
            self.settings_inert, top, snapshot, now_named, then
        ):
            # Only keys that run nothing changed (E122): as the owner's.
            settings_guard.save(self.store, top, snapshot)
            then = settings_guard.restricted(
                {folder: snapshot["folders"].get(folder, {}) for folder in folders}, names
            )
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
        agent = settings_agent(names)
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
        if not row or row["closed_at"] or not self.settings_guarded(row):
            return ""  # (No hold without the sandbox: one left from before goes at its next look.)
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

    def settings_hold_message(self, message: dict, text: str, changed: bool = False) -> dict:
        """What the owner wrote a worker held for its folder's settings, waiting: under it at
        once, that line's choices (E121): its "Use these settings" (for what it shows: its
        digest), a new worker instead, or not sending it. Approval-like words, which never use
        them, say to use the button (`changed`: they changed after the owner wrote them)."""
        session_id = message["session_id"]
        card = self.settings_card(session_id)
        if session_id not in self.settings_held or not card or not card.get("digest"):
            return message
        hold = {
            "digest": card["digest"],
            "changes": card.get("changes", ""),
            "session_id": session_id,
            "reason": self.settings_reasons.get(session_id) or "Its folder's settings changed",
        }
        if changed:
            hold["guidance"] = CHANGED_SINCE
        elif APPROVAL_LIKE.fullmatch(" ".join(text.split())):
            hold["guidance"] = USE_THE_BUTTON
        meta = {**message["meta"], "settings_hold": hold}
        self.store.update_message_meta(message["id"], meta)
        updated = self.store.message(message["id"])
        self.delivery.publish_delivery(session_id, [updated])
        return {**updated, "delivery": self.delivery.delivery_for(session_id, [updated])[0]}

    def settle_settings_messages(self, session_id: str, status: str, text: str):
        """Its hold over, the choices under what the owner wrote it give way to what became of
        it, in a word."""
        for found in self.store.all(
            "SELECT id FROM messages WHERE session_id=? AND kind='owner' "
            "AND json_extract(meta, '$.settings_hold') IS NOT NULL",
            (session_id,),
        ):
            message = self.store.message(found["id"])
            meta = {key: value for key, value in message["meta"].items() if key != "settings_hold"}
            meta["settings_handled"] = {"status": status, "text": text, "session_id": session_id}
            self.store.update_message_meta(message["id"], meta)
            self.delivery.publish_delivery(session_id, [self.store.message(message["id"])])

    async def settings_typed(
        self, row: dict, text: str, shown: str, task_id: str = "", client_id: str = ""
    ) -> dict | None:
        """The owner, in the chat of a worker held for its folder's settings (never Home, Slack,
        Send to Chief or a worker's words), saying to use them in so many words (SAID_USE; a
        bare "ok" or "yes" only when the line that asks is the last thing there): taken as its
        "Use these settings" for the line they saw (`shown`: its digest, as their page had it),
        and only that one. Their message stays in the chat, not sent to the worker. None when it
        is not that: it goes as any other (with the line's buttons under it)."""
        session_id = row["id"]
        words = " ".join(text.split())
        if (
            session_id not in self.settings_held
            or not shown
            or not self.settings_guarded(row)
            or not (card := self.settings_card(session_id))
            or card.get("digest") != shown
            or not (
                SAID_USE.fullmatch(words)
                or (SAID_YES.fullmatch(words) and self.asked_last(session_id, card["message_id"]))
            )
        ):
            return None
        meta = {
            "settings_typed": shown,
            **({"task_id": task_id} if task_id else {}),
            **({"client_id": client_id} if client_id else {}),
        }
        message = self.add_message(session_id, "owner", text, meta)
        try:
            await self.accept_settings(session_id, shown, typed=True)
        except ValueError:
            # Changed since the line they saw: not used; the new line, and its choices under
            # their message.
            await self.settings_refusal(self.store.session(session_id))
            plain = {key: value for key, value in meta.items() if key != "settings_typed"}
            self.store.update_message_meta(message["id"], plain)
            return self.settings_hold_message(self.store.message(message["id"]), text, True)
        return self.store.message(message["id"])

    def asked_last(self, session_id: str, notice_id: int) -> bool:
        """Whether the line asking about its folder's settings is the last thing in its chat
        (the stop it made, said after it, aside)."""
        for found in self.store.all(
            "SELECT id, kind, text, meta FROM messages WHERE session_id=? AND id>=? ORDER BY id",
            (session_id, notice_id),
        ):
            if found["id"] == notice_id:
                continue
            meta = json.loads(found["meta"] or "{}")
            if not (found["kind"] == "notice" and meta.get("stopped")):
                return False
        return True

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
            self.store.delete(f"settingsheldat:{session_id}")
        self.settings_reasons.pop(session_id, None)
        if session_id in self.settings_held:
            self.settings_held.discard(session_id)
            self.store.delete(f"settingsheldat:{session_id}")
            self.settings_shown(session_id)
            self.settle_settings_messages(session_id, "released", "Nothing holds it now.")
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
        """Before a Copilot or Claude worker opens or starts a turn: why not (its folder's
        settings of its agent differ from what the owner last let it use: the agent would load
        them), "" when it may; said in its timeline. Only for a sandboxed one
        (settings_guarded): "" at once for any other. Within SETTINGS_SECONDS: past that, its
        folder can't be checked, and it waits. (Any worker: a .git replaced.)"""
        if row["role"] != WORKER or not row["cwd"]:
            return ""
        if not self.settings_guarded(row):
            if row["id"] in self.settings_held:
                self.settings_unsaid(row["id"])  # (The sandbox was turned off meanwhile.)
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
        if (agent := self.agent(row["id"])) not in {COPILOT_AGENT, "claude"}:
            return ""  # (Codex: a thread doesn't trust such a folder, codex_settings.)
        names = agent_settings(agent)
        # Taken before they are read: a change while they are is a change at the next look.
        seen = await self.settings_thread(self.settings_signature, row)
        verdict = await self.settings_verdict(row, names)
        if verdict is None:
            self.settings_unsaid(row["id"])
            self.settings_sigs[row["id"]] = seen
            return ""
        reason = self.settings_reason(settings_agent(names), verdict)
        joined = f"{reason}{'. ' if '; ' in reason else '; '}Chief of Staff won't start the worker"
        if verdict["refused"]:
            line = f"{joined} there until that's removed."
        else:
            line = f"{joined} there until you say to use them."
        self.settings_reasons[row["id"]] = reason
        self.settings_said(row["id"], line, verdict)
        return line

    async def settings_stop(self, session_id: str):
        """A worker whose folder's settings changed: no turn starts until the owner says
        to use them (or they are as they were). One at work is stopped (their word goes on with
        it); one that is not is let go of, and what it was sent and never saw waits for it."""
        self.settings_held.add(session_id)
        if not self.store.get(f"settingsheldat:{session_id}"):
            self.store.set(f"settingsheldat:{session_id}", str(time.time()))
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
        if (
            session_id not in found
            and session_id not in self.settings_held
            and self.store.get(f"gitappeared:{session_id}")
        ):
            # Found first by the look before Chief of Staff's own git (git_guard), which says
            # nothing: said now all the same (not again once it waits for the owner).
            found[session_id] = []
        if closing:
            self.store.delete(f"gitwatch:{session_id}")
        for owner in found:
            row = self.store.find_session(owner)
            said = bool(row and await self.git_appeared(row))
            if said and row and not row["closed_at"] and not closing:
                await self.settings_stop(owner)

    async def git_appeared(self, row: dict) -> str:
        """Its line while a git file appeared in a .git it can write waits for the owner ("" for
        none: one they removed is let go of, one in Chief of Staff's own worktree folders is
        moved aside). "Use it"; "Keep it removed" too where only those folders hold them (one
        asked about before they were moved aside by themselves)."""
        pending = json.loads(self.store.get(f"gitappeared:{row['id']}") or "[]")
        there = [p for p in pending if await self.settings_thread(os.path.lexists, p)]
        if there != pending:
            if there:
                self.store.set(f"gitappeared:{row['id']}", json.dumps(there))
            else:
                self.store.delete(f"gitappeared:{row['id']}")
        if not there:
            return ""
        admins = [c.get("admin") or "" for c in self.workspaces.checkouts(row) if c.get("worktree")]
        asked = self.git_asked(row["id"])
        if own := [p for p in there if p not in asked and any(a and within(p, a) for a in admins)]:
            # In Chief of Staff's own worktree folders (E123): moved aside at once, the worker
            # goes on; Put back under Handled for you. One in the owner's own repository (or
            # asked about already) still waits for their word.
            aside = await self.git_set_aside(row["id"], own)
            there = [path for path in there if path not in own]
            if there:
                self.store.set(f"gitappeared:{row['id']}", json.dumps(there))
            else:
                self.store.delete(f"gitappeared:{row['id']}")
            moved = ", ".join(git_name(path) for path in own)
            self.add_message(
                row["id"],
                "notice",
                f"Moved {moved} out of this worker's worktree .git: Chief of Staff's own folder, "
                f"not your repository (a copy is in {home_shown(aside)}).",
            )
            self.handled.record(
                "git",
                f"Moved {moved} out of “{self.display_title(row)}”'s worktree .git",
                session_id=row["id"],
                ref=aside,
                undo="Put back",
                data={"paths": own, "aside": aside},
            )
            if not there:
                return ""
        names = ", ".join(git_name(path) for path in there)
        removable = all(any(a and within(p, a) for a in admins) for p in there)
        line = f"A git file appeared in .git ({names}). If you made it, use it."
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
            aside = await self.git_set_aside(session_id, pending)
            said = f"{names}: kept removed (a copy is in {home_shown(aside)})."
        self.store.delete(f"gitappeared:{session_id}")
        self.add_message(session_id, "notice", said)
        self.store.delete(f"settingssaid:{session_id}")
        await self.release_held(session_id)
        return said

    def git_asked(self, session_id: str) -> set[str]:
        """The git files its timeline last asked the owner about (left to their word)."""
        found = self.store.one(
            "SELECT meta FROM messages WHERE session_id=? AND kind='notice' "
            "AND json_extract(meta, '$.git_quarantine') IS NOT NULL ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        return set(json.loads(found["meta"])["git_quarantine"]["paths"]) if found else set()

    async def git_set_aside(self, session_id: str, paths: list[str]) -> str:
        """Git files moved into Chief of Staff's data folder (never deleted): where."""
        aside = self.store.directory / "quarantine" / session_id / str(int(time.time() * 1000))
        for path in paths:
            if await asyncio.to_thread(os.path.lexists, path):
                moved = aside / path.lstrip(os.sep)
                await asyncio.to_thread(moved.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.move, path, moved)
        return str(aside)

    async def git_put_back(self, entry: dict) -> str:
        """Put back, under Handled for you: the git files moved aside are where they were, and
        used as the owner's "Use it" would (no longer looked for)."""
        aside, paths = Path(entry["data"]["aside"]), entry["data"]["paths"]
        moves = [(aside / path.lstrip(os.sep), path) for path in paths]

        def put_back():
            if not all(os.path.lexists(kept) for kept, _ in moves):
                raise ValueError("The copy is gone: there's nothing to put back.")
            if any(os.path.lexists(path) for _, path in moves):
                raise ValueError("Something is in its place now: it stays as it is.")
            for kept, path in moves:
                shutil.move(kept, path)

        await asyncio.to_thread(put_back)
        for key in self.store.keys("gitwatch:"):
            watched = json.loads(self.store.get(key) or "[]")
            if kept_paths := [path for path in watched if path not in paths]:
                self.store.set(key, json.dumps(kept_paths))
            else:
                self.store.delete(key)
        said = f"Put back {', '.join(git_name(path) for path in paths)}: workers use them."
        if self.store.find_session(entry["session_id"]):
            self.add_message(entry["session_id"], "notice", said)
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
            if tree and (served := self.git_served(path, now)) and served[0] != tree:
                # Its worktree made again while the private folder kept its identity (ext4 can
                # give both their inode numbers back): kept as it is now, so that a swap of the
                # private folder later is never taken for this remake (git_remade).
                self.git_record(path, now + served)
            return False
        if tree and self.git_remade(path, tree):
            self.git_record(path, now + self.git_served(path, now))
            return False
        return True

    def git_served(self, path: str, now: list) -> list:
        """Of a worktree's private folder in a repository (`.git/worktrees/x`): that
        worktree's folder (where its `gitdir` leads), as it is, and its `.git` file with when
        it was written, for telling a worktree made again (git worktree remove, add) from a
        swap of the private folder under one that stays."""
        parent = Path(path).parent
        if not now or now[0] != "d" or parent.name != "worktrees":
            return []
        try:
            named = read_regular(Path(path) / "gitdir", 65536).decode(errors="replace").strip()
        except OSError:
            return []
        tree = os.path.realpath(Path(named).parent)
        pointer = str(Path(tree) / ".git")
        file = identity_of(pointer)
        with suppress(OSError):
            file = file and [*file, os.lstat(pointer).st_mtime_ns]
        return [{"tree": tree, "id": identity_of(tree), "pointer": file}]

    def git_remade(self, path: str, tree: dict) -> bool:
        """Whether a worktree's private folder was made again with its worktree (`git worktree
        add` under the same name, the one before removed): its worktree is another folder too,
        and the private folder still serves it. Never for a worktree a worker could make again
        itself that Chief of Staff or the owner run git in (git_tree_guarded): there, only the
        private folder as it was counts.

        Another folder: another identity, or (ext4 gives a removed folder's inode number to the
        next one made, often at once) a `.git` file written since. Out of every worker's reach,
        only the owner's git writes it again."""
        served = self.git_served(path, identity_of(path))
        if not served or served[0]["tree"] != tree["tree"]:
            return False
        pointer = served[0].get("pointer")
        rewritten = bool(pointer and tree.get("pointer") and pointer != tree["pointer"])
        if served[0]["id"] == tree["id"] and not rewritten:
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
        if (
            row
            and row["role"] == WORKER
            and row["cwd"]
            and self.settings_guarded(row)
            and (replaced := self.git_replaced(row))
        ):
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
        if not self.settings_guarded(row):
            await self.release_unguarded()
            return "Nothing waits: without the sandbox, workers use their folders as they are."
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
        if not self.settings_guarded(row):
            return ""
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
        elif tool in {"task", "Agent", "Task"} and isinstance(arguments, dict):
            # (Copilot takes the agent's name as it is written; any case here. Claude's Agent
            # tool names it its subagent_type.)
            agent = str(arguments.get("agent_type") or arguments.get("subagent_type") or "")
            agent = agent.casefold()
            servers = [s for s in off if agent and s.partition("@")[2].casefold() == agent]
        else:
            servers = mcp_called(off, tool)
        if not servers:
            return ""
        self.servers_asked(row, servers)  # It does call it: now the owner is asked.
        return (
            f"The {server_labels(servers)} changed in this folder since the owner last let you use "
            f"{'it' if len(servers) == 1 else 'them'}: it's off for you. Work without it; the "
            "owner can turn it on from your timeline."
        )

    def servers_said(self, row: dict):
        """Opened with servers off: quietly (E123), under Handled for you with Turn on, once
        (each, while it stays off). Its timeline asks the owner only once it calls one
        (servers_asked)."""
        off = self.servers_off.get(row["id"], {})
        key = f"serversoff:{row['id']}"
        said = set(json.loads(self.store.get(key) or "[]"))
        if set(off) == said:
            return
        if off:
            self.store.set(key, json.dumps(sorted(off)))
        else:
            self.store.delete(key)
            self.store.delete(f"serversasked:{row['id']}")
        if not set(off) - said:
            return  # (Fewer off: said as they were turned on.)
        digest = servers_digest(off)
        self.handled.record(
            "servers",
            f"Kept its {server_labels(sorted(off))} off for “{self.display_title(row)}”: "
            "changed in its folder",
            session_id=row["id"],
            ref=digest,
            undo="Turn on",
            data={"digest": digest},
        )

    def servers_asked(self, row: dict, servers: list[str]):
        """A worker calls a server off for it: its timeline says so, with Use these settings
        (once for each, while it stays off)."""
        off = self.servers_off.get(row["id"], {})
        key = f"serversasked:{row['id']}"
        asked = set(json.loads(self.store.get(key) or "[]"))
        if not set(servers) - asked or not off:
            return
        self.store.set(key, json.dumps(sorted(asked | set(servers))))
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

    def servers_called(self, row: dict, tool: str):
        """A worker's tool as it starts: one of an MCP server off for it asks the owner now."""
        if (off := self.servers_off.get(row["id"])) and (called := mcp_called(off, tool)):
            self.servers_asked(row, called)

    async def servers_back(self, entry: dict) -> str:
        """Turn on, under Handled for you: as Use these settings on its line."""
        if not self.store.find_session(entry["session_id"]):
            raise ValueError("That worker is gone.")
        return await self.accept_servers(entry["session_id"], entry["data"]["digest"])

    async def accept_servers(self, session_id: str, digest: str) -> str:
        """The owner says to use the servers its line names (Use these settings): what they
        run now becomes what Chief of Staff compares with; its next turn opens with them."""
        row = self.store.session(session_id)
        names = agent_settings(self.agent(session_id))
        verdict = await self.settings_verdict(row, names)
        off = self.servers_off.get(session_id, {})
        if not off:
            self.servers_said(row)
            return "They are as they were: nothing to change."
        if servers_digest(off) != digest:
            raise ValueError("They changed again since: look at the latest line and try again.")
        if verdict and verdict["unreadable"]:
            raise ValueError(f"These can't be checked ({verdict['unreadable']}).")
        top, _, _, now, unreadable, _ = await self.settings_thread(self.settings_read, row, names)
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
        if not row or row["closed_at"] or not row["cwd"] or not self.settings_guarded(row):
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
        thread is not to trust them); its timeline says so. None of them without the sandbox:
        its threads trust them as the owner's own Codex sessions do."""
        if not self.settings_guarded(row):
            return []
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

    async def accept_settings(self, session_id: str, digest: str, typed: bool = False) -> str:
        """The owner says to use the settings now in a worker's folder (its timeline's "Use
        these settings", or their own words in its chat: `typed`, settings_typed): they become
        what Chief of Staff compares with, and the worker goes on with what waited for it. Only
        when they are still what the owner was shown; ones too many to check are used unchecked
        from then on (until they can be checked again). Undo is offered on its line while
        nothing of them changed since (undo_settings)."""
        row = self.store.session(session_id)
        if not self.settings_guarded(row):
            # (A line from before the sandbox was turned off.)
            await self.release_unguarded()
            return "Nothing waits: without the sandbox, workers use their folder's settings."
        if digest.startswith("servers:"):
            return await self.accept_servers(session_id, digest)
        names = agent_settings(self.agent(session_id))
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
            folders = [settings_guard.folder_key(top, f) for f in settings_guard.chain(top, cwd)]
            undo: dict = {}
            if verdict["unreadable"]:
                snapshot["unchecked"][settings_guard.unchecked_key(names, folders)] = digest
                said = f"You let this worker use them unchecked ({verdict['unreadable']})."
            else:
                # As the owner was shown them (a change since is a change again).
                before = self.settings_part(snapshot, folders, names)
                settings_guard.taken(snapshot, verdict["now"], folders, names)
                if verdict.get("carried"):
                    # (Theirs in the owner's checkout too from now on: not undone here.)
                    self.carried_theirs(row, top, verdict["now"])
                else:
                    undo = {
                        "digest": digest, "top": str(top), "folders": folders,
                        "names": list(names), "before": before, "at": time.time(),
                        "after": self.settings_part(snapshot, folders, names),
                    }  # fmt: skip
                changed = settings_guard.described(verdict["changes"])
                said = (
                    f"Using these settings ({changed}) because you said so."
                    if typed
                    else f"You let this worker use these settings ({changed})."
                )
            settings_guard.save(self.store, top, snapshot)
            if undo:
                self.store.set(f"settingsundo:{session_id}", json.dumps(undo, sort_keys=True))
            self.add_message(
                session_id,
                "notice",
                said,
                {"settings_undo": {"digest": digest}} if undo else None,
            )
            self.store.delete(f"settingssaid:{session_id}")
            self.settle_settings_messages(session_id, "approved", said)
        if verdict is None:
            self.settle_settings_messages(session_id, "unchanged", said)
        if names == CODEX_SETTINGS:
            if verdict is not None and self.states.is_live(session_id):
                # Its thread trusts them once opened again: now, or once its turn is over.
                await self.refresh_session(row)
            return said
        # This worker goes on, and every other that waited for them (in the same folder).
        await self.release_held(session_id)
        return said

    @staticmethod
    def settings_part(snapshot: dict, folders: list[str], names: tuple[str, ...]) -> dict:
        """What a snapshot holds of these settings in these folders, and how it marks them."""
        return {
            folder: {
                "entries": settings_guard.restricted(
                    {folder: snapshot["folders"].get(folder, {})}, names
                ).get(folder, {}),
                **{
                    marks: sorted(n for n in snapshot.get(marks, {}).get(folder, []) if n in names)
                    for marks in ("known", "first", "carried")
                },
            }
            for folder in folders
        }

    async def undo_settings(self, session_id: str, digest: str) -> str:
        """The owner takes back their word on a folder's settings (Undo on its line): what was
        taken before comes back, and every worker there waits again. Only while nothing has
        changed since, neither the files nor what Chief of Staff holds of them (a later word,
        another worker's), within SETTINGS_UNDO_SECONDS: never trust it no longer had."""
        raw = self.store.get(f"settingsundo:{session_id}")
        try:
            undo = json.loads(raw) if raw else {}
        except ValueError:
            undo = {}
        if (
            not isinstance(undo, dict)
            or undo.get("digest") != digest
            or time.time() - float(undo.get("at") or 0) > SETTINGS_UNDO_SECONDS
        ):
            raise ValueError("That can no longer be undone.")
        row = self.store.session(session_id)
        names, folders = tuple(undo["names"]), list(undo["folders"])
        top, _, _, now, unreadable, _ = await self.settings_thread(self.settings_read, row, names)
        if unreadable or str(top) != undo["top"]:
            raise ValueError("That can no longer be undone: its folder can't be checked as it was.")
        shown = settings_guard.without_servers(settings_guard.restricted(now, names))
        snapshot = settings_guard.load(self.store, top) or settings_guard.fresh()
        if (
            settings_guard.digest(shown) != digest
            or self.settings_part(snapshot, folders, names) != undo["after"]
        ):
            raise ValueError("They changed since, so your word on them stays.")
        for folder in folders:
            was = undo["before"].get(folder, {})
            kept = {
                entry: value
                for entry, value in snapshot["folders"].get(folder, {}).items()
                if not any(settings_guard.under(entry, name) for name in names)
            }
            snapshot["folders"][folder] = {**kept, **was.get("entries", {})}
            for marks in ("known", "first", "carried"):
                held = snapshot.setdefault(marks, {})
                others = [n for n in held.get(folder, []) if n not in names]
                if both := sorted({*others, *was.get(marks, [])}):
                    held[folder] = both
                else:
                    held.pop(folder, None)
        settings_guard.save(self.store, top, snapshot)
        self.store.delete(f"settingsundo:{session_id}")
        for found in self.store.all(
            "SELECT id FROM messages WHERE session_id=? AND kind='notice' "
            "AND json_extract(meta, '$.settings_undo.digest')=?",
            (session_id, digest),
        ):
            message = self.store.message(found["id"])
            meta = {k: v for k, v in message["meta"].items() if k != "settings_undo"}
            self.store.update_message_meta(message["id"], {**meta, "settings_undone": True})
            self.bus.publish("message", {**self.store.message(message["id"]), "amended": True})
        said = "Undone: this worker waits for your word on its folder's settings again."
        self.add_message(session_id, "notice", said)
        # Every worker there: looked at again now, as at its next turn.
        for other in self.store.open_sessions(WORKER):
            if other["id"] == session_id or self.settings_place(other)[0] == top:
                self.settings_sigs.pop(other["id"], None)
                if self.settings_guarded(other) and await self.settings_refusal(other):
                    await self.settings_stop(other["id"])
        return said

    def held_owner_message(self, message_id: int, digest: str) -> tuple[dict, dict]:
        """A still-unsent owner message and the settings card it was shown with."""
        message = self.store.message(message_id)
        if message["kind"] != "owner":
            raise ValueError("That is not your queued message.")
        session_id = message["session_id"]
        hold = message["meta"].get("settings_hold") or {}
        if hold.get("digest") != digest or session_id not in self.settings_held:
            raise ValueError("That settings hold changed: use the latest card.")
        delivery = self.delivery.delivery_for(session_id, [message])[0]
        if delivery not in {"queued", "held"}:
            raise ValueError("That message is no longer waiting.")
        return message, hold

    def drop_held_owner_message(
        self,
        message: dict,
        status: str,
        text: str,
        *,
        forwarded_to: str = "",
        cleanup: bool = False,
    ) -> dict:
        """Keep a queued message in history but remove the prompt so it can never arrive;
        `cleanup`: as Chief of Staff started (E125)."""
        meta = {
            key: value
            for key, value in message["meta"].items()
            if key not in {"prompt", "settings_hold", "deferred"}
        }
        meta |= {
            "dropped": True,
            "settings_handled": {
                "status": status,
                "text": text,
                "session_id": message["session_id"],
            },
            **({"forwarded_to": forwarded_to} if forwarded_to else {}),
            **({"cleanup": True} if cleanup else {}),
        }
        self.store.update_message_meta(message["id"], meta)
        updated = self.store.message(message["id"])
        self.delivery.publish_delivery(message["session_id"], [updated])
        return updated

    async def cancel_held_owner_message(self, message_id: int, digest: str) -> dict:
        """Cancel one message, not the settings hold or unrelated queued work."""
        message, _ = self.held_owner_message(message_id, digest)
        updated = self.drop_held_owner_message(
            message, "cancelled", "Message cancelled; it will not be delivered."
        )
        task = self.tasks.find(message["meta"].get("task_id") or "")
        if (
            message["meta"].get("chat_task_new")
            and task
            and task["session_id"] == message["session_id"]
            and task["status"] in OPEN
        ):
            with suppress(ValueError):
                await self.task_actions.cancel(task["id"])
        return updated

    async def start_new_worker_for_held_message(self, message_id: int, digest: str) -> dict:
        """ "Start a new worker" under a message held with its worker: the task it is about goes
        on in a new worker in a fresh checkout (move_held_task), the message with it."""
        message, _ = self.held_owner_message(message_id, digest)
        row = self.store.session(message["session_id"])
        task = self.tasks.find(message["meta"].get("task_id") or "")
        why = "this one waits for its folder's settings"
        if task and task["session_id"] == row["id"] and task["status"] in OPEN:
            return await self.move_held_task(row, task, why)
        return await self.move_held_chat(row, [message], why)

    async def move_held_chat(self, row: dict, messages: list[dict], why: str) -> dict:
        """What the owner wrote a held worker about finished work (or none), as a new worker's
        task, told of that work (context_from): sent there, not to the held one, which closes
        once nothing of it is open."""
        task = self.tasks.find(messages[-1]["meta"].get("task_id") or "")
        new = await self.spawn_instead(
            row,
            title=task["title"] if task else clip_words(messages[-1]["text"], 60),
            goal="\n\n".join(message["text"] for message in messages),
            why=why,
            context_from=[task["id"]] if task else [],
        )
        for message in messages:
            self.drop_held_owner_message(
                message,
                "forwarded",
                "Sent to a new worker instead.",
                forwarded_to=new["session_id"],
            )
        await self.release_orphaned_settings_worker(row["id"])
        return new

    def held_task(self, row: dict) -> dict | None:
        """The open task a worker held for its folder's settings is on: the one the hold
        stopped, else the one it was given last, else any open one."""
        open_ = [task for task in self.tasks.for_session(row["id"]) if task["status"] in OPEN]
        stopped = [t for t in open_ if self.store.get(f"stopped:{t['id']}") == SETTINGS_STOP]
        current = [t for t in open_ if t["id"] == row["task_id"]]
        return next(iter(stopped or current or open_), None)

    async def move_held_task(self, row: dict, task: dict, why: str) -> dict:
        """A task of a worker held for its folder's settings, done instead by a new worker in a
        fresh checkout of the same projects (a worktree from their base: nothing of the held
        folder's is carried), told what was done so far (context_from). The task it replaces
        is over; the held worker, with no open task left, closes, what the owner wrote it
        following its task (release_orphaned_settings_worker)."""
        criteria = json.loads(task.get("acceptance") or "[]")
        replacement = await self.spawn_instead(
            row,
            title=task["title"],
            goal=task["goal"],
            why=why,
            context_from=[task["id"]],
            replaces=[task["id"]],
            done_when=criteria if isinstance(criteria, list) else [],
            review=bool(self.store.get(f"reviewreq:{task['id']}")),
        )
        await self.release_orphaned_settings_worker(row["id"])
        return replacement

    async def spawn_instead(self, row: dict, title: str, goal: str, why: str, **more) -> dict:
        """A new worker on the projects a held worker has, each in a fresh checkout of its own
        (a worktree from its base where it is a repository): nothing of the held folder's."""
        checkouts = self.workspaces.checkouts(row)
        projects = list(
            dict.fromkeys(
                path for c in checkouts if (path := c.get("project") or c.get("repo") or "")
            )
        )
        project = row["project"] or (projects[0] if projects else "")
        return await self.toolset.t_spawn_worker(
            SpawnWorker(
                title=title,
                instructions=goal,
                project=project,
                more_projects=[path for path in projects if path != project],
                isolate=bool(project and any(c.get("git_dir") or c.get("repo") for c in checkouts)),
                slack=bool(row["slack"]),
                why_new=why,
                **more,
            ),
            self.router_id(),
        )

    def in_made_worktree(self, row: dict) -> bool:
        """Whether a worker's folder is a worktree Chief of Staff made for it from a project
        (never the owner's own checkout, nor a folder they named), as all its checkouts are."""
        checkouts = self.workspaces.checkouts(row)
        place = self.settings_place(row)[2]
        return bool(
            checkouts
            and place in checkouts
            and all(
                c.get("worktree") and c.get("project") and not c.get("shared") for c in checkouts
            )
        )

    async def move_long_held(self, moment: float | None = None):
        """Nothing waits hours for the owner's word on a folder's settings: a worker held for
        them SETTINGS_MOVE_SECONDS, in a worktree Chief of Staff made (in_made_worktree), has
        its task (or what the owner wrote it about finished work) done by a new worker in a
        fresh one instead (move_held_task, move_held_chat); one line on Home says so. In the
        owner's own checkout it waits for their word."""
        moment = time.time() if moment is None else moment
        for session_id in list(self.settings_held):
            since = float(self.store.get(f"settingsheldat:{session_id}") or moment)
            row = self.store.find_session(session_id)
            if (
                moment - since < SETTINGS_MOVE_SECONDS
                or not row
                or row["closed_at"]
                or not self.settings_guarded(row)
                or not self.in_made_worktree(row)
            ):
                continue
            task, chat = self.held_task(row), self.unsent_owner_chat(session_id)
            if not task and not chat:
                continue  # (Nothing waits on it.)
            self.store.set(f"settingsheldat:{session_id}", str(moment))  # (Once a while.)
            why = "the earlier one waited for its folder's settings"
            try:
                if task:
                    new = await self.move_held_task(row, task, why)
                else:
                    new = await self.move_held_chat(row, chat, why)
            except Exception:
                logger.warning(
                    "Could not move %s's task to a new worker.", session_id, exc_info=True
                )
                continue
            with suppress(RuntimeError, LookupError):
                there = self.display_title(self.store.session(new["session_id"]))
                self.add_message(
                    self.router_id(),
                    "notice",
                    f"“{self.display_title(row)}” waited {SETTINGS_MOVE_SECONDS // 60} minutes for "
                    f"its folder's settings: its task goes on in a new worker, “{there}”, in a "
                    "fresh checkout.",
                    {"session_id": new["session_id"]},
                )

    def unsent_owner_chat(self, session_id: str) -> list[dict]:
        """What the owner wrote a worker that has not reached it (nor been dropped)."""
        mark = int(self.store.get(f"sent:{session_id}") or 0)
        return [
            message
            for message in self.store.prompts(session_id, after=mark)
            if message["kind"] == "owner" and not message["meta"].get("dropped")
        ]

    async def settle_held_chat(
        self, row: dict, closing: bool, cleanup: bool = False
    ) -> list[tuple[str, dict]]:
        """What the owner wrote a worker held for its folder's settings, as the hold ends some
        other way than its going on with all of it (E124): each message still waiting stays
        unless the task it names is over (cancelled, failed, replaced, set aside) or another's,
        or the worker is `closing`; then it goes to the task that replaced the one it names,
        when that is another worker's and open (replacedby proves it); else it is dropped,
        never delivered later. What became of them is said in one line for each worker they
        went to, and one for those dropped (E125), with when they were written and what became
        of their task, in the Router's chat. For a cleanup as Chief of Staff starts (`cleanup`:
        release_unguarded) the messages are marked so and the lines are only returned, with
        what Handled for you keeps of them, for the caller to say there (cleaned_up)."""
        moved: dict[str, list[dict]] = {}
        dropped: list[tuple[dict, str]] = []
        for message in self.unsent_owner_chat(row["id"]):
            named = self.tasks.find(message["meta"].get("task_id") or "")
            if not closing and (
                not named
                or (
                    named["session_id"] == row["id"]
                    and named["status"] not in {"cancelled", "failed"}
                    and not self.store.get(f"replacedby:{named['id']}")
                    and not self.store.get(f"setaside:{named['id']}")
                )
            ):
                continue  # (Still its: a follow-up on its result reopens that task.)
            instead = (
                self.tasks.find(self.store.get(f"replacedby:{named['id']}") or "")
                if (named and named["session_id"] == row["id"])
                else None
            )
            if (
                named
                and instead
                and instead["session_id"] != row["id"]
                and instead["status"] in OPEN
                and await self.forwarded_held_chat(message, named, instead)
            ):
                there = self.display_title(self.store.session(instead["session_id"]))
                self.drop_held_owner_message(
                    message, "forwarded", f"Sent to “{there}”, which took over its task.",
                    forwarded_to=instead["session_id"], cleanup=cleanup,
                )  # fmt: skip
                moved.setdefault(instead["session_id"], []).append(message)
            else:
                outcome = self.held_outcome(row, named)
                self.drop_held_owner_message(
                    message, "dropped", f"Not sent: {outcome}.", cleanup=cleanup
                )
                dropped.append((message, outcome))
        title = clip_words(self.display_title(row), 80)
        said: list[tuple[list[dict], str]] = []
        for session_id, messages in moved.items():
            there = clip_words(self.display_title(self.store.session(session_id)), 80)
            said.append(
                (messages, f"to “{title}”{{when}} went to “{there}”, which took over its task.")
            )
        if dropped:
            messages = [message for message, _ in dropped]
            outcome = "; ".join(dict.fromkeys(outcome for _, outcome in dropped))
            one = len(messages) == 1
            later = "" if cleanup else f" {'It' if one else 'They'} won't be sent later."
            verb = "was" if one else "were"
            said.append((messages, f"to “{title}”{{when}} {verb} not sent: {outcome}.{later}"))
        lines = []
        for messages, line in said:
            first, last = messages[0]["created_at"], messages[-1]["created_at"]
            whose = (
                f"Your message “{clip_words(messages[0]['text'], 100)}”"
                if len(messages) == 1
                else f"Your {len(messages)} messages"
            )
            # (Under Handled for you, the time beside it says when they were written.)
            when = "" if cleanup else f" ({said_between(first, last)})"
            text = f"{whose} {line.format(when=when)}"
            lines.append((text, {"at": [first, last], "messages": [m["id"] for m in messages]}))
            if cleanup:
                continue
            with suppress(RuntimeError):
                meta = {"session_id": row["id"], "at": [first, last]}
                self.add_message(self.router_id(), "notice", text, meta)
        return lines

    def cleaned_up(self, row: dict, lines: list[tuple[str, dict]], closed: bool = False):
        """What a start's cleanup did for a worker (release_unguarded), under Handled for you,
        not in the Router's chat (E125): its messages' lines (settle_held_chat), the last of them
        saying it `closed` too (E128), or that alone."""
        if closed:
            said = " Closed the worker too: nothing was left for it."
            if lines:
                lines = [*lines[:-1], (lines[-1][0] + said, lines[-1][1])]
            else:
                title = clip_words(self.display_title(row), 80)
                text = f"Closed “{title}”, which waited for its folder's settings: nothing was"
                lines = [(f"{text} left for it.", {})]
        for text, data in lines:
            self.handled.record(
                "cleanup", text, session_id=row["id"], ref=row["id"], data=data or None
            )

    def held_outcome(self, row: dict, named: dict | None) -> str:
        """What became of the task a message held for a worker named, in a few words (E125):
        where it moved and how it went there, or how it ended."""
        if not named:
            return "it has no task open"
        if instead := self.tasks.find(self.store.get(f"replacedby:{named['id']}") or ""):
            worker = self.store.find_session(instead["session_id"])
            there = clip_words(self.display_title(worker), 80) if worker else instead["title"]
            moved = f"its task moved to “{there}”"
            if instead["status"] in {"review", "done"}:
                finished = (
                    self.store.value(
                        "SELECT max(ended_at) FROM runs WHERE task_id=?", (instead["id"],)
                    )
                    or instead["ended_at"]
                    or instead["updated_at"]
                )
                return f"{moved}, which finished it {said_at(finished)}"
            if instead["status"] == "failed":
                return f"{moved}, where it failed"
            if instead["status"] == "cancelled":
                return f"{moved}, where it was cancelled"
            return moved
        if named["session_id"] != row["id"]:
            return "its task is another worker's"
        if self.store.get(f"setaside:{named['id']}"):
            return "its task was set aside"
        return {
            "cancelled": "its task was cancelled",
            "failed": "its task failed",
            "review": "its task was finished",
            "done": "its task was finished",
        }.get(named["status"], "its task is over")

    async def forwarded_held_chat(self, message: dict, old: dict, new: dict) -> bool:
        """A held message the owner wrote about a task, to the worker of the task replacing it;
        False when it could not be (then it is dropped)."""
        meta = {
            key: value
            for key, value in message["meta"].items()
            if key not in {"prompt", "settings_hold", "deferred", "dropped", "chat_task_new"}
        }
        context = wrap(
            "system",
            "context",
            f"The owner wrote this about task {old['id']}, which your task {new['id']} replaces: "
            "it is about your task now.",
            task=new["id"],
        )
        try:
            await self.delivery.deliver(
                new["session_id"],
                f"{message['meta']['prompt']}\n\n{context}",
                kind="owner",
                display=message["text"],
                meta={**meta, "task_id": new["id"], "forwarded_from": message["id"]},
                steer=True,
            )
        except Exception:
            logger.warning("Could not send %s on to %s.", message["id"], new["id"], exc_info=True)
            return False
        return True

    async def release_orphaned_settings_worker(self, session_id: str, ended: str = ""):
        """A worker held for its folder's settings with no open task left (cancelled, replaced,
        moved, its result set aside: `ended`, not one the owner accepted): it closes, and what
        the owner wrote it goes where its task went or is dropped (settle_held_chat), never
        delivered later."""
        if session_id in self.settings_orphans_closing:
            return
        self.settings_orphans_closing.add(session_id)
        try:
            await asyncio.sleep(0)  # (Whatever ended its task finishes first.)
            row = self.store.find_session(session_id)
            task = self.tasks.find(ended) if ended else None
            if (
                (task and task["status"] == "done" and not self.store.get(f"setaside:{ended}"))
                or session_id not in self.settings_held
                or not row
                or row["closed_at"]
                or any(task["status"] in OPEN for task in self.tasks.for_session(session_id))
            ):
                return
            await self.settle_held_chat(row, closing=True)
            self.settings_held.discard(session_id)
            self.settings_reasons.pop(session_id, None)
            for key in (f"settingssaid:{session_id}", f"settingsheldat:{session_id}"):
                self.store.delete(key)
            await self.workers.close(session_id)
        finally:
            self.settings_orphans_closing.discard(session_id)

    async def release_unguarded(self):
        """Workers held for their folder's settings (or a .git they could write) that are not
        guarded now (settings_guarded: the sandbox is off, after an update or turned off in
        Settings) go on: what the owner wrote them goes out, but for a task that is over or
        another's, which is dropped (settle_held_chat: a cleanup, under Handled for you). One
        with no open task and nothing left for it closes (E128)."""
        for row in self.store.open_sessions(WORKER):
            if self.settings_guarded(row):
                continue
            self.servers_off.pop(row["id"], None)  # (What a look found, before.)
            stopped = any(
                task["status"] == "waiting"
                and self.store.get(f"stopped:{task['id']}") == SETTINGS_STOP
                for task in self.tasks.for_session(row["id"])
            )
            if (
                row["id"] not in self.settings_held
                and row["activity"] != SETTINGS_WAIT
                and not stopped
            ):
                continue
            self.settings_sigs.pop(row["id"], None)
            for key in (f"gitappeared:{row['id']}", f"gitwatch:{row['id']}"):
                self.store.delete(key)
            self.settings_held.add(row["id"])
            try:
                # A .git it replaced is what Chief of Staff's own git compares with from now on.
                for path in await self.settings_thread(self.git_replaced, row):
                    identity = identity_of(path)
                    self.git_record(path, identity + self.git_served(path, identity))
                lines = await self.settle_held_chat(row, closing=False, cleanup=True)
            except Exception:
                # (It stays held: tried again at the next start or sandbox change.)
                logger.warning("Could not let %s go on.", row["id"], exc_info=True)
                continue
            if any(task["status"] in OPEN for task in self.tasks.for_session(row["id"])) or (
                self.unsent_owner_chat(row["id"])
            ):
                self.cleaned_up(row, lines)
                self.settings_unsaid(row["id"])  # (It goes on: settings_go_on.)
                continue
            # Nothing open, nothing left for it (E128): closed, as when its task ends held, said
            # with its messages' line under Handled for you (not in the Router's chat).
            self.cleaned_up(row, lines, closed=True)
            self.settings_held.discard(row["id"])
            self.settings_reasons.pop(row["id"], None)
            for key in (f"settingssaid:{row['id']}", f"settingsheldat:{row['id']}"):
                self.store.delete(key)
            if row["activity"] == SETTINGS_WAIT:
                self.update_session(row["id"], activity="")
            await self.workers.close(row["id"], quiet=True)

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
        gate = self.resumes.get(session_id)
        if gate and not gate.is_set() and not RESUMING.get():
            # Being resumed as Copilot connected (resume_sessions): after that.
            with suppress(TimeoutError):
                await asyncio.wait_for(gate.wait(), SESSION_RESUME)
            if session := self.states.handle(session_id):
                return session
        row = self.store.find_session(session_id) if self.agents else None
        if row and (role := self.role_key(row)) in PLACED_ROLES:
            return await self.role_session(row, role)
        return await self.open_session(session_id)

    async def role_session(self, row: dict, role: str):
        """A role's session where it runs now (place_role): its agent's, or Copilot's while
        that can't open it (said in one line; back once it can). An agent that runs but no
        longer has its session gives it a fresh one there, with a summary for the Router."""
        session_id = row["id"]
        await self.place_role(role)
        try:
            return await self.open_session(session_id)
        except OpenElsewhereError:
            raise
        except Exception as exc:
            agent = self.agent(session_id)
            if agent == COPILOT_AGENT or self.states.handle(session_id):
                raise
            why = f"{agents.display(agent)} could not open it: {describe(exc)}"
            backend, _ = self.connection(agent)
            if backend is not None and backend.alive() and self.made(session_id):
                logger.warning("The %s's %s session could not be resumed.", role, agent,
                               exc_info=True)  # fmt: skip
                try:
                    async with self.role_locks.setdefault(role, asyncio.Lock()):
                        await self.switch_role(row, role, agent, "renew", describe(exc))
                    return await self.open_session(session_id)
                except Exception as again:
                    why = f"{agents.display(agent)} could not open it: {describe(again)}"
            self.hold_role(role, agent, why)
            await self.place_role(role)
            return await self.open_session(session_id)

    async def open_session(self, session_id: str):
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

    def handover(self, why: str = "whose context had grown long", upto_sent: bool = False) -> str:
        """What a new Router session needs to know, from Chief of Staff's records. `upto_sent`:
        the conversation up to what it was given (what it was not, it is given after this)."""

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
        mark = int(self.store.get(f"sent:{router}") or 0)
        unsent = {m["id"] for m in self.store.prompts(router, after=mark)} if upto_sent else set()
        for message in self.store.all(
            """SELECT * FROM messages WHERE session_id=?
            AND kind IN ('owner','update','assistant') ORDER BY id DESC LIMIT 2000""",
            (router,),
        ):
            meta = json.loads(message["meta"] or "{}")
            if meta.get("internal") or message["id"] in unsent:
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
        if (
            row
            and not row["closed_at"]
            and self.agents
            and (
                not self.on_copilot(row["id"])
                or agents.role_agent(self.store, ROUTER) != COPILOT_AGENT
                or self.backend is None
            )
        ):
            # On another agent (or to be, or Copilot can't run it): it starts there aside,
            # Chief of Staff ready meanwhile; on the next agent that can while that one can't
            # (place_role).
            self.background(self.open_role(ROUTER))
            return
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
        await self.place_new_role(ROUTER, row)  # (Made where it runs: Copilot's, or not.)
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
                backend, _ = self.connection(self.agent(rows[0]["id"]))
                if backend is None or not backend.alive():
                    raise  # Its agent itself stopped: its session may well be fine.
                # Its agent may no longer have it (its session state was cleaned up).
                logger.warning("The Slack session could not be resumed; starting a new one.",
                               exc_info=True)  # fmt: skip
                self.store.unfollow_session(rows[0]["id"])
                self.update_session(rows[0]["id"], closed_at=now())
        row = self.store.create_session(
            str(uuid4()), SLACK, "Slack", "idle", cwd=str(self.role_dir(SLACK))
        )
        await self.place_new_role(SLACK, row)
        await self.open_live(row, create=True)

    async def place_new_role(self, role: str, row: dict):
        """A new session of one of Chief of Staff's own roles made where it runs now
        (role_start), rather than made on Copilot and moved."""
        if not self.agents:
            return
        async with self.role_locks.setdefault(role, asyncio.Lock()):
            target, _ = await self.role_start(role)
        if target and target != COPILOT_AGENT:
            self.set_agent(row["id"], target)

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

    def to_resume(self, agent: str) -> list[str]:
        """The agent's open sessions that resume_sessions resumes: mid-send when it stopped, or
        at work."""
        sending = {key.removeprefix("sending:") for key in self.store.keys("sending:")}
        rows = self.store.open_sessions()
        ids = [row["id"] for row in rows if row["status"] == "working" or row["id"] in sending]
        return self.on_agent(agent, ids)

    async def resume_sessions(self, reconnected: bool):
        """As Copilot connects: its sessions' interrupted sends checked, then those at work
        continued (each in SESSION_RESUME seconds at most). What is sent to one of them waits
        until it is done with it (session_for)."""
        RESUMING.set(True)
        try:
            await self.reconcile_sends()
            if reconnected:
                # Copilot stopped (crashed, killed) and started again: what the turns it cut
                # short used is in their logs, not in events that came by.
                await self.workers.sync_all_usage()
            # Held for their folder's settings by an earlier version (or with the sandbox
            # on then): without it now, they go on (release_unguarded).
            await self.release_unguarded()
            await self.resume_working()
        finally:
            for gate in self.resumes.values():
                gate.set()

    def resumed(self, session_id: str) -> None:
        """resume_sessions is done with this session."""
        if gate := self.resumes.get(session_id):
            gate.set()

    async def bounded(self, session_id: str, work: Awaitable, what: str) -> Any:
        """One session's part of resume_sessions, in SESSION_RESUME seconds at most: None when it
        took longer (said in the log; Copilot's connection is watched on its own)."""
        try:
            return await asyncio.wait_for(work, SESSION_RESUME)
        except TimeoutError:
            logger.warning("%s %s took over %d s: given up.", what, session_id, SESSION_RESUME)
            return None

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
            if self.too_large(session_id):
                continue  # Never resumed for it: what it was sent waits for a fresh one.
            arrived = await self.bounded(
                session_id, self.delivery.reconcile(session_id), "Checking a send to"
            )
            outcome = {True: "it arrived", False: "resending", None: "unknown, resending"}
            logger.info("Interrupted send to %s: %s.", session_id, outcome[arrived])
            if row["status"] != "working":
                self.resumed(session_id)

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
            if self.shepherd.marked(row["id"]):
                # Left in a shepherd step: never continued in its normal profile.
                await self.shepherd.crashed(row["id"])
                continue
            if row["role"] == SLACK and not self.slack.configured():
                continue
            if row["status"] != "working":
                if self.delivery.has_unsent(row["id"]):
                    # Events held, or a send interrupted, when Chief of Staff stopped.
                    self.background(self.delivery.resume(row["id"]))
                continue
            if agent == COPILOT_AGENT and self.too_large(row["id"]):
                self.hold_too_large(row)  # Never resumed: it waits for a fresh one.
                self.resumed(row["id"])
                continue
            await self.bounded(row["id"], self.continue_working(row, what), "Resuming")
            self.resumed(row["id"])

    async def continue_working(self, row: dict, what: str):
        """resume_working's for one session at work: it continues, told what it had not
        handled."""
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
        if kind in DOING and self.store.get(f"untouched:{session_id}"):
            # Its first step: Restart on… gives way to Continue with… (its view says so with
            # the activity or status its step publishes).
            self.store.delete(f"untouched:{session_id}")
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
        self.teams_lite.task_changed(task, was)
        self.task_actions.publish(task)
        if (
            task["status"] in ENDED
            and was is not None
            and (was not in ENDED or (task["status"] != was and task["status"] != "done"))
        ):
            self.decisions.drop_task(task)
            # What its worker went on without asking the owner ends with it (or goes on with
            # the task that replaced it).
            self.asks.task_ended(task)
            if task["status"] in {"cancelled", "done"} and task["session_id"] in self.settings_held:
                # (Cancelled, replaced, moved, set aside: E124.)
                self.background(
                    self.release_orphaned_settings_worker(task["session_id"], ended=task["id"])
                )
        if task["status"] != "waiting" and self.waits_for_nothing(task["session_id"]):
            # Its question settled some other way (the draft it asked about sent, say): its
            # worker no longer waits for the owner (bug bash 9: its box kept "waits for you").
            # One whose turn goes on (it asked and kept at it) is at work in it.
            busy = self.states[task["session_id"]].busy
            self.update_session(task["session_id"], status="working" if busy else "idle")

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
        if (
            self.agents
            and not self.on_copilot(row["id"])
            and self.role_key(row) in PLACED_ROLES
            and self.role_failed(row, message)
        ):
            return  # On the next agent that can run it now, given again what it was on.
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

    async def owner_send_once(
        self,
        session_id: str,
        client_id: str,
        send: Callable[[], Awaitable[dict]],
        again: Callable[[dict], dict] | None = None,
    ) -> dict:
        """The owner's send from the page, once for the id the page gave it (`client_id`): the
        same again within CLIENT_SEND_SECONDS (a retry, a second Enter that got through while
        the first was on its way) gets what the first stored, and nothing is sent twice. Later,
        or after a restart, one stored for it (sent_with) is answered with `again` (the page
        sends it again under its id once it reloaded mid-send: see client_sent). One that failed
        may be tried again."""
        if not client_id:
            return await send()
        now = time.monotonic()
        for key, (at, _) in list(self.client_sends.items()):
            if now - at > CLIENT_SEND_SECONDS:
                del self.client_sends[key]
        key = (session_id, client_id)
        if key in self.client_sends:
            return await asyncio.shield(self.client_sends[key][1])
        if again and (found := self.sent_with(session_id, client_id)):
            return again(found)
        sent: asyncio.Future = asyncio.get_running_loop().create_future()
        self.client_sends[key] = (now, sent)
        try:
            message = await send()
        except BaseException as exc:
            self.client_sends.pop(key, None)
            if isinstance(exc, Exception):
                sent.set_exception(exc)
                sent.exception()  # (Retrieved: one waiting for it hears of it, or none does.)
            else:
                sent.cancel()
            raise
        sent.set_result(message)
        return message

    def sent_with(self, session_id: str, client_id: str) -> dict | None:
        """The owner's message stored in a session for the page's send `client_id` (its own, or
        Home's copy of one sent to a worker from there), among its latest CLIENT_SENT_LOOK."""
        if not client_id:
            return None
        for row in self.store.all(
            "SELECT * FROM messages WHERE session_id=? AND kind='owner' ORDER BY id DESC LIMIT ?",
            (session_id, CLIENT_SENT_LOOK),
        ):
            meta = json.loads(row["meta"] or "{}")
            if meta.get("client_id") == client_id:
                return {**row, "meta": meta}
        return None

    async def client_sent(self, session_id: str, client_id: str) -> dict | None:
        """Whether the page's send `client_id` was stored, for a page that reloaded while it was
        on its way (its answer lost): one still on its way here is waited for, CLIENT_SENT_WAIT
        at most. The message, or None."""
        if entry := self.client_sends.get((session_id, client_id)):
            await asyncio.wait({entry[1]}, timeout=CLIENT_SENT_WAIT)
        return self.sent_with(session_id, client_id)

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
        agent: str = "",
        task_id: str = "",
        compare: Sequence[str] = (),
        agent_model: str = "",
        agent_effort: str = "",
        typed: bool = False,
        settings_digest: str = "",
        shared_card: str = "",
        client_id: str = "",
        answers_card: bool = True,
        extra_meta: dict | None = None,
    ) -> dict:
        """`learn`: the owner typed it (not a message the app sends for them). A message to a
        worker is also noted for the Router (`tell_router`), which must know what the owner
        asked for. `from_home`: written on Home (@ a worker), where its answer is shown too.
        `answers_card`: to a worker waiting on its question card, it is the answer (not a word
        to several parts of a team: Tell the team); `extra_meta`, kept on the message.
        `agent`: the coding agent the owner chose for it in the composer (@codex: the Router's
        new workers for it run there; `agent_model` and `agent_effort`, chosen with it).
        `compare`: several agents chosen (@codex @claude): the task runs on each. `task_id`:
        the task of the worker's they look at in its chat (see chat_task). `typed`: written
        in that worker's own chat, which showed `settings_digest` as the line on its folder's
        settings that waits for them (settings_typed). `shared_card`: on Home, a card someone
        shared that the owner opened here, quoted after their words as untrusted data.
        `client_id`: their page's id for this send, kept with what it stores (its chat shows it
        there at once, Home's copy too)."""
        row = self.store.session(session_id)
        sent_as = {"client_id": client_id} if client_id else {}
        # Someone else's card: after the owner's words, never instructions (shares.quoted); one
        # removed meanwhile is said before anything is done.
        quoted, shared = (
            self.shares.quoted(shared_card) if shared_card and row["role"] == ROUTER else ("", {})
        )
        if (
            typed
            and row["role"] == WORKER
            and not attachments
            and not row["closed_at"]
            and (
                said := await self.settings_typed(
                    row, text, settings_digest, task_id, client_id=client_id
                )
            )
        ):
            return said
        for chosen in (agent, *compare) if row["role"] == ROUTER else ():
            if chosen and chosen not in {COPILOT_AGENT, *self.agents_on()}:
                # Chosen in the composer, then turned off before it was sent: said, not dropped.
                raise ValueError(
                    f"{agents.display(chosen)} is turned off in Settings → Agents & models: turn "
                    "it on, or send this without choosing it."
                )
        if row["closed_at"] and row["role"] == WORKER:
            await self.workers.reopen(session_id, restore=True)
        if row["role"] == WORKER:
            self.release_schedules(session_id)  # The owner's word: its schedules fire again.
        # The task this is about: the one whose question it answers, the one the owner looks
        # at, the one it is on… (see chat_task).
        about = self.chat_task(session_id, task_id) if row["role"] == WORKER else None
        if row["role"] == WORKER and not attachments and text.strip() and answers_card:
            # Waiting on its question card: this is the answer (sent as a message, it would only
            # be read once the card times out).
            with suppress(ValueError):
                if card := self.asks.answer_in_chat(session_id, text.strip()):
                    meta = {
                        "answered": card["id"],
                        **({"home": True} if from_home else {}),
                        **sent_as,
                    }
                    if about:
                        meta["task_id"] = about["id"]
                    message = self.add_message(session_id, "owner", text, meta)
                    self.forget_go_on_at(session_id)  # Their answer, not going on by itself.
                    if tell_router:
                        self.owner_told(row, text, client_id=client_id)
                    return message
        answered: list[dict] = []
        if row["role"] in {WORKER, SLACK} and clear_questions:
            # The owner answers what it asked on that task (others' questions stay).
            answered = self.asks.answered(session_id, about["id"] if about else "", text=text)
            answered += self.asks.answered(session_id, "", text=text)
        # Waiting on its decision card: what the owner writes it is their answer to it (unless
        # they ask something back: then the card stays, and this is chat).
        decided = (
            await self.decisions.answered_in_chat(session_id, about, text.strip())
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
        meta: dict[str, Any] = dict(sent_as)
        if asked := [c["target"]["question"] for c in decided] + [q["text"] for q in answered]:
            # Its answer to what the worker asked (Asks.held_ask reads it so).
            answers = decided[0]["id"] if decided else answered[0]["id"]
            meta |= {"answers": answers, "asked": asked[0][:300], "answer": text.strip()}
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
        if quoted:
            meta["shared_card"] = shared
        if row["role"] == ROUTER:
            # The Router hands this message on as {owner:<ref>} instead of copying it.
            meta["ref"] = "o" + uuid4().hex[:6]
            prompt = wrap("owner", "message", text, ref=meta["ref"], reply_in=spoken)
            notes += [
                self.projects_note(),
                self.toolset.related_hint(text),
                await self.terminals_note(),
            ]
            compared = list(dict.fromkeys(compare))
            if len(compared) > 1:
                # Several chosen in the composer: the task on each, side by side.
                meta["agents"] = compared
                notes.append(
                    "The owner chose "
                    + " and ".join(agents.display(name) for name in compared)
                    + " for this, to compare: call spawn_worker once (its agent and "
                    "compare_with are set for you), then give both results side by side."
                )
            elif agent in {COPILOT_AGENT, *self.agents_on()}:
                # Chosen in the composer: as if they had named it.
                meta["agent"] = agent
                meta |= {
                    f"agent_{key}": value
                    for key, value in (("model", agent_model), ("effort", agent_effort))
                    if value
                }
                notes.append(
                    f"The owner chose {agents.display(agent)} for this: spawn_worker's agent "
                    f'is "{agent}" for the workers it needs.'
                )
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
        prompt = "\n\n".join([prompt, *([quoted] if quoted else []), *context])
        task = about
        if made:
            meta |= {
                "task_id": made["id"],
                "chat_task_new": True,
            }  # Its instruction, as a task's first one is.
        elif task:
            # What its turn is about now; a chat with it, not a task card's follow-up.
            meta |= {"task_id": task["id"], "chat": True}
        if from_home:
            meta["home"] = True  # Asked on Home: its answer is shown there too.
        meta |= extra_meta or {}
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
        if row["role"] == WORKER and session_id in self.settings_held:
            words = " ".join(text.split())
            message = self.settings_hold_message(
                message,
                text,
                # (Their word on a line that changed since: the new line's, under it.)
                changed=bool(typed and settings_digest)
                and settings_digest != (self.settings_card(session_id) or {}).get("digest")
                and bool(SAID_USE.fullmatch(words) or SAID_YES.fullmatch(words)),
            )
        if task and (made or self.store.get(f"followup:{task['id']}")):
            # Home shows it at work on that task (bug QA6-4: its row stayed under Monitoring,
            # and a row of its worker's beside it under Running said the same work).
            self.task_actions.publish(self.tasks.get(task["id"]))
        if row["role"] == WORKER:
            # It goes on as the owner says now (taken), not by itself at its limit's reset.
            self.forget_go_on_at(session_id)
        if row["role"] == WORKER and tell_router:
            self.owner_told(row, text, message, client_id=client_id)
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
        return switched_note(spoken, before)

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
        self,
        router_id: str,
        worker_id: str,
        text: str,
        attachments: Sequence[Attachment] = (),
        client_id: str = "",
    ) -> dict:
        """From the Router chat, straight to one worker; the Router hears of it later. With
        Home's copy of it (`copy`, "→ worker"), what the chat there shows, by the page's
        `client_id` for this send."""
        row = self.store.session(worker_id)
        if row["role"] != WORKER:
            raise ValueError("Address a worker.")
        message = await self.owner_send(
            row["id"], text, attachments=attachments, from_home=True, client_id=client_id
        )
        # Not when it answered the question card the worker was waiting on.
        note = "" if message["meta"].get("answered") else self.busy_note(row["id"])
        if note:  # Where the owner is: Chat or the inbox.
            self.add_message(router_id, "notice", note, {"session_id": row["id"]})
        found = client_id and self.store.one(
            "SELECT id FROM messages WHERE session_id=? AND kind='owner' "
            "AND id>? AND json_extract(meta, '$.client_id')=? ORDER BY id DESC LIMIT 1",
            (router_id, message["id"], client_id),
        )
        if not found:
            return {**message, "note": note}
        copy = self.store.message(found["id"])
        delivery = self.delivery.delivery_for(router_id, [copy])[0]
        meta = {key: value for key, value in copy["meta"].items() if key != "prompt"}
        return {**message, "note": note, "copy": {**copy, "meta": meta, "delivery": delivery}}

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

    def owner_told(self, row: dict, text: str, sent: dict | None = None, client_id: str = ""):
        """What the owner told a worker directly (in its timeline, @ in Chat, on a task card):
        the Router hears of it with its next message, so it never takes the result for drift.
        `sent`: the worker's own message, as it was given it: Home's copy stands where that one
        does (Delivery.delivery_for). `client_id`: their page's for that send (owner_send)."""
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
                    **({"client_id": client_id} if client_id else {}),
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
        """What was spent in a range. A worker's session by the name it has everywhere, with
        the tasks its spend went to (its name is its current work's: bug E136, the whole spend
        read as that task's); each task with its worker's name."""
        found = self.store.usage(since, until)
        names: dict[str, str] = {}

        def name(session_id: str) -> str:
            if session_id not in names:
                row = self.store.find_session(session_id)
                names[session_id] = self.display_title(row) if row else ""
            return names[session_id]

        tasks: dict[str, list[dict]] = {}
        for task in found["by_task"]:
            if task["task_id"]:
                task["worker"] = name(task["session_id"])
                tasks.setdefault(task["session_id"], []).append(task)
        for item in found["by_session"]:
            if item.get("role") == WORKER and (row := self.store.find_session(item["session_id"])):
                item["title"] = name(row["id"])
                item["worker"] = not self.special(row)
                item["tasks"] = [
                    {key: task[key] for key in ("task_id", "title", "calls", "credits")}
                    for task in tasks.get(row["id"], [])
                ]
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

    def open_terminals(self) -> list[str]:
        """The sessions open in another program now, as last looked (every agent's)."""
        found = set(self.elsewhere)
        for seen in self.elsewhere_on.values():
            found |= set(seen)
        return sorted(found)

    def saw_terminals(self, listed: list[str]):
        """The Router was given these sessions as open in the owner's terminal now."""
        self.terminals_given = set(listed)

    async def terminals_note(self) -> str:
        """The owner's sessions opened in a terminal since the Router last looked at them, named
        in the owner's next message (live e2e r81r: asked about one opened a minute after its
        last list_sessions, the Router answered from that list and found it nowhere). Their
        message waits for the listing (the CLI's, slow under load) TERMINALS_WAIT at most:
        longer, those are named with their next message instead (the listing goes on, and is
        there by then)."""
        now = set(self.open_terminals())
        given, self.terminals_given = self.terminals_given, now
        opened = now - given if given is not None else set()
        if not opened:
            return ""  # The Router's first look at them is through list_sessions.
        listing = self.background(self.terminals_listing(opened))
        done, _ = await asyncio.wait({listing}, timeout=TERMINALS_WAIT)
        if not done:
            self.terminals_given = (self.terminals_given or set()) - opened
            return ""
        items = listing.result()
        if items is None:
            return ""
        named = [
            f"\u201c{item['summary']}\u201d (session {item['id']}"
            + (f", in {item['cwd']}" if item["cwd"] else "")
            + ")"
            for item in items
            if item["open"] and item["id"] in opened
        ]
        if not named:
            return ""  # Chief of Staff's own, a program's: not the owner's to see.
        return (
            f"Opened in the owner's terminal since you last looked: {'; '.join(named[:5])}. "
            "list_sessions().terminal_sessions has every session open there now."
        )

    async def terminals_listing(self, opened: set[str]) -> list[dict] | None:
        """The owner's sessions as listed, with those `opened` in a terminal: listed again only
        when one is in neither the listing nor what it left out as never the owner's, and that
        listing was not taken just now (another one now would say the same). None when they
        can't be listed."""
        started = time.monotonic()
        try:
            items = await self.workers.external(agents_too=True)
            known = {item["id"] for item in items} | self.workers.never_owners
            if opened <= known | self.workers.agents_never_owners:
                return items
            if self.workers.external_cache[0] >= started and (
                not self.agents_on() or self.workers.agent_cache[0] >= started
            ):
                return items
            return await self.workers.external(refresh=True, agents_too=True)
        except Exception:
            return None

    def report_on_home(self, row: dict, task: dict, status: str, summary: str):
        """What a worker reported to the owner who wrote to it in its chat about finished work
        (a corrected result, or an answer): a line in the Router's chat on Home, as a report
        the Router hears of shows, without waking it. Needs you does not show the task while its
        worker follows up on the owner's next message (owner report 0.1.53, item 2: a corrected
        result was in neither)."""
        with suppress(RuntimeError):
            self.add_message(
                self.router_id(),
                "inbound",
                summary,
                {
                    "from": row["id"],
                    "title": task["title"],
                    "status": status,
                    "task_id": task["id"],
                },
            )

    def amend_on_home(self, session_id: str, task_id: str, summary: str):
        """Its report of a task amended: its line in the Router's chat on Home says it as it is
        now (once: not a line more)."""
        with suppress(RuntimeError):
            line = self.store.one(
                "SELECT id FROM messages WHERE session_id=? AND kind='inbound' "
                "AND json_extract(meta, '$.from')=? AND json_extract(meta, '$.task_id')=? "
                "ORDER BY id DESC LIMIT 1",
                (self.router_id(), session_id, task_id),
            )
            if line:
                message = self.store.update_message_text(line["id"], summary)
                shown = {k: v for k, v in (message.get("meta") or {}).items() if k != "prompt"}
                self.bus.publish("message", {**message, "meta": shown, "amended": True})

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
        self.require_up()
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
        Refused while Chief of Staff is not ready, and over the rates, with nothing kept."""
        key = f"send:{item.item}"
        if found := self.store.sent(key):
            return self.receipt(item.item, {"taken": False, **found})
        self.require_up()
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
                if key.startswith("slack:") and (
                    handed := self.store.get(f"delegated:{key.removeprefix('slack:')}")
                ):
                    # The thread of a message the owner handed over with their delegate emoji:
                    # this task's reply there may go out at once (Drafts.handed_over).
                    self.store.set(f"delegatedtask:{task_id}:{key}", handed)
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
        if thread.startswith("github:") and self.shepherd.control.exists():
            self.background(self.shepherd.unfollowed(session_id, thread))
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
        self.require_up()
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
        if thread.startswith("github:") and await self.shepherd.signal(
            thread, fresh, text, display
        ):
            # Kept for the PR's shepherd step (it rides along with its worker's next message).
            self.store.touch(thread)
            self.background(self.task_actions.refresh_open_prs(thread.removeprefix("github:")))
            return True
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
                # Reports only, read (or a day old): accepted for the owner (E123).
                self.task_actions.settle_reports_now,
                # Queued work starts once memory frees up, not only when a turn ends.
                self.workers.pump,
                self.move_long_held,
                self.digest_once,
                self.asks.expire_questions,
                self.focus.check,
                self.workers.refresh_elsewhere,
                # Chief of Staff's own sessions back on the agent chosen for them, once it can.
                self.roles_check,
                # Disk space: read, measured and cleaned in the background (see storage.py).
                self.storage.tick,
                # Prepared for you, while on (shepherd.py).
                self.shepherd.tick,
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
            **(
                {"settings_hold": self.settings_card(row["id"])}
                if row["id"] in self.settings_held
                else {}
            ),
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
            # A worker's short name and its team (E129): the same everywhere.
            **(
                {"label": self.teams.label(row), "team": self.teams.team_of(row["id"])}
                if row["role"] == WORKER
                else {}
            ),
            "elsewhere": bool(self.is_elsewhere(row)) and not row["closed_at"],
            "terminal": self.terminal.get(row["id"]),
            **(self.progress(row) if row["role"] == WORKER else {}),
            # A second worker: the one it was started from, with that one's context.
            **({"fork_of": origin} if (origin := self.forks.origin(row["id"])) else {}),
            # Only for a worker on another agent (none without agents).
            **(
                self.agent_view(row, agent)
                if self.agents and (agent := self.agent(row["id"])) != COPILOT_AGENT
                else {}
            ),
            # One the agent Settings chose could not run: why it runs on Copilot (its badge).
            **(
                {"agent_fallback": why}
                if self.agents and (why := self.store.get(f"agentfallback:{row['id']}"))
                else {}
            ),
            # Continued on another agent: the worker that goes on with its work.
            **(
                {"continued": new}
                if self.agents and (new := self.store.get(f"continued:{row['id']}"))
                else {}
            ),
            # Nothing done yet (no step, no report): it can start over on another agent.
            **(
                {"untouched": True}
                if self.agents and not row["closed_at"] and self.store.get(f"untouched:{row['id']}")
                else {}
            ),
            # One of the workers doing the same task on several agents (Compare: <title>).
            **(
                {"compare": json.loads(group)}
                if self.agents and (group := self.store.get(f"compare:{row['id']}"))
                else {}
            ),
            # Only while it runs a shepherd step: its work then is the step's, not a task's.
            **({"shepherd_step": True} if self.shepherd.marked(row["id"]) else {}),
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
        # No request: the owner's own word to one worker, not a request the Router split.
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
        # worker's name with the task it came from, until the worker works on it). By its own
        # turns first; else by what reached it about a task, never what Chief of Staff told it
        # (cancelled, closed: bug E88), nor what never reached it: a message dropped, sent on
        # to another worker, or past what it has seen (E127: a held worker was named after
        # the cancelled task the owner had written to it about). Not by what it said of a task
        # while its turn was on another (live J2: its answer to the Router's note about its
        # first task, after it reported its second, named it after the first again).
        seen = int(self.store.get(f"seen:{row['id']}") or self.store.get(f"sent:{row['id']}") or 0)

        def last_active(task: dict) -> tuple:
            split = json.loads(self.store.get(f"splitfrom:{task['id']}") or "null")
            found = self.store.one(
                """SELECT max(CASE WHEN kind IN ('assistant', 'report', 'tool', 'update')
                AND json_extract(meta, '$.off_turn') IS NULL THEN id END) AS own,
                max(CASE WHEN ? OR kind!='notice' THEN id END) AS given
                FROM messages WHERE session_id=? AND id>? AND json_extract(meta, '$.task_id')=?
                AND json_extract(meta, '$.dropped') IS NULL
                AND json_extract(meta, '$.forwarded_to') IS NULL
                AND json_extract(meta, '$.undelivered') IS NULL
                AND (json_extract(meta, '$.prompt') IS NULL OR id<=?)""",
                (bool(split), row["id"], split["mark"] if split else 0, task["id"], seen),
            ) or {"own": None, "given": None}
            if found["own"]:
                return (2, found["own"])
            if split:
                return (1, found["given"]) if found["given"] else (0, 0)
            return (1, found["given"] or 0)

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
        listed = self.tasks.listed(include_ended_since=hours_ago(TASKS_SHOWN_HOURS))
        settings = self.store.settings()
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
            # Questions workers wait on each other for (Home: "1 waiting 9 min").
            "peer_waiting": self.asks.peer_waiting(),
            # Teams lite: the work several workers do for one of the owner's requests, as
            # candidates (where each stands is Home's, in the page); Past teams' count; the
            # questions holding a worker up half an hour (Needs you, one each).
            **self.teams_lite.snapshot(listed),
            "updates": self.owner_updates(),
            "tasks": [self.task_actions.view(task) for task in listed],
            "settings": settings.model_dump(),
            # Off macOS no desktop notification can show (notify.py): Settings leaves out its line.
            **({} if sys.platform == "darwin" else {"desktop_notifications": False}),
            # Only once Prepared for you was turned on: its rows, and where steps can run.
            **({"shepherd": kept} if (kept := self.shepherd.snapshot()) else {}),
            # Only while another coding agent is on: Settings' Agents & models page shows it.
            **({"agents": self.agents_view()} if self.agents_on() else {}),
            # Who does the work (Settings → Agents & models), every agent included.
            "work": agents.work_view(self.store, settings)
            | {"copilot_default": self.store.get("copilot_default_model", "")},
            # Only where another agent is installed: its Settings tab, and first-run cards.
            **({"agents_found": found} if (found := self.agents_found()) else {}),
            # Each agent's status for the @ picker (ready, its sign-in needed, not installed).
            **({"agents_status": status} if (status := self.agents_status()) else {}),
            **({"agent_offers": offers} if (offers := self.agent_offers()) else {}),
            # Sign-ins and installs started from the page, while they run (and their last words).
            **({"agent_setups": setups} if (setups := self.setups.view()) else {}),
            # Roles waiting for the owner (If it can't run there: Ask me), their cards' actions
            # open.
            **(
                {"role_waits": waits}
                if (waits := sorted(role for role in PLACED_ROLES if self.role_wait(role)))
                else {}
            ),
            # Cards for workers their named agent could not start, not answered yet.
            **(
                {"agent_refusals": sorted(refusals)}
                if (refusals := self.store.items("agentrefusal:"))
                else {}
            ),
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
            # What Chief of Staff settled for the owner instead of asking, the last week's.
            "handled": self.handled.view(),
            "sandbox": {
                "supported": self.sandbox_host,
                "gh_trusted": self.sandbox_gh,
            },
        }

    async def settings_changed(self, old: Settings, new: Settings) -> list[str]:
        """Returns why each worker it stopped stopped (another agent's, made ineligible)."""
        stopped: list[str] = []
        if (old.proactive, old.paused) != (new.proactive, new.paused):
            await self.shepherd.settings_changed(old, new)
        if old.proactive != new.proactive:
            await self.refresh(ROUTER)  # Its one rule and its tools' shepherd field.
        if old.other_sessions != new.other_sessions:
            # First, before anything awaits: once the save returns, nothing more is told.
            self.others.switch(new.other_sessions)
        if old.send_to_chief != new.send_to_chief:
            # Off: the socket closes before anything awaits; the Router's rule goes with its
            # next turn.
            self.send.switch(new.send_to_chief)
            await self.refresh(ROUTER)
        if old.worker_sandbox and not new.worker_sandbox:
            # Without the sandbox nothing waits for its folder's settings: held ones go on.
            self.background(self.release_unguarded())
        if old.fork_busy != new.fork_busy:
            await self.refresh(ROUTER)  # Its rule and same_worker, from its next turn.
        if self.agents and (
            old.worker_sandbox != new.worker_sandbox or old.worker_yolo != new.worker_yolo
        ):
            # Other agents' workers do not run sandboxed, and act on their own or ask first only
            # where their version's check allows it: stopped at once, with their commands.
            stopped = await self.stop_ineligible()
        if new.worker_sandbox and not old.worker_sandbox and self.sandbox_host:
            # A worker at work ends its turn as it began: nothing more goes into that turn,
            # unsandboxed; what it is sent meanwhile waits for its sandboxed session (live H4: a
            # follow-up steered into it wrote outside its folders).
            for row in self.store.open_sessions(WORKER):
                state = self.states[row["id"]]
                if row["status"] in {"working", "starting"} or state.busy or state.held:
                    state.confined = True
        elif old.worker_sandbox and not new.worker_sandbox:
            for row in self.store.open_sessions(WORKER):
                self.states[row["id"]].confined = False  # As its turn began again: unsandboxed.
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
        if self.copilot_ready() and any(getattr(old, key) != getattr(new, key) for key in defaults):
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
