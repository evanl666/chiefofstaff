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
import json
import logging
import os
import re
import shutil
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import prompts, threads
from .abilities import Abilities
from .asks import Asks
from .backend import (
    OLDEST_CLI,
    AgentBackend,
    SandboxPolicy,
    copilot_backend,
    copilot_trusts,
    older,
    sandbox_supported,
)
from .bus import Bus
from .checks import Checks
from .connector import Connector
from .decisions import Decisions
from .delivery import Delivery
from .digest import digest
from .drafts import Drafts
from .envelope import UNTRUSTED, clean_title, language, sender, short, task_named, wrap
from .events import ROUTER_CONTEXT, Events
from .focus import Focus
from .habits import Habits
from .learning import Learning
from .models import Attachment, Settings
from .notify import mac_notification
from .peers import Peers
from .policy import GITHUB_MCP, within
from .roles import ROUTER, SLACK, WATCHDOG, WORKER
from .sources import Source
from .sources.github import GitHubSource
from .sources.slack import SlackSource, describe
from .state import SessionStates
from .store import Store, ago_minutes, hours_ago, now
from .task_actions import TaskActions
from .tasks import (
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
from .workers import Workers
from .workspaces import (
    BRANCH_PREFIX,
    Workspaces,
    common_git_dir,
    git_dirs,
    protected_git_paths,
    service_secrets,
    shared_caches,
)

logger = logging.getLogger(__name__)

# A repository's own Copilot configuration that Copilot loads only in a trusted folder.
TRUST_GATED = (".mcp.json", ".github/mcp.json", ".github/hooks", ".github/copilot")


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
# The messenger sends away notifications and reads the owner's replies to them.
MESSENGER_TOOLS = ["report_notified", "report_owner_replies"]
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
GITHUB_HOSTS = (
    "github.com",
    "api.github.com",
    "uploads.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
)


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
        self.backend: AgentBackend | None = None
        self.connections = 0  # Counts disconnects: a session opened before one is not usable.
        self.states = SessionStates()
        # Whether this machine can run Copilot's command sandbox, and whether gh works inside it.
        self.sandbox_host = sandbox_supported()
        self.sandbox_gh: bool | None = None
        self.connect_lock = asyncio.Lock()
        self.jobs: set[asyncio.Task] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.status = {"state": "starting", "error": ""}
        self.models_cache: tuple[float, list[dict]] = (0.0, [])
        # Sessions another program (a terminal) has open: session id -> pid. Refreshed by the
        # watch loop and before resuming a session.
        self.elsewhere: dict[str, int] = {}
        # What workers open in a terminal are doing, read from their logs (Workers.mirror).
        self.terminal: dict[str, dict] = {}
        self.mirror_job: asyncio.Task | None = None
        self.project_cache: dict[str, tuple[float, dict]] = {}
        self.rotate_lock = asyncio.Lock()
        self.work_root = store.directory / "work"
        self.workspaces = Workspaces(store, self.work_root)
        self.workspaces.github_token = lambda repo: self.github.token_for(repo)
        self.tasks = TaskBook(store, on_change=lambda task: self.task_actions.publish(task))
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
        self.toolset = Tools(self)
        self.tools = self.toolset.definitions()

    # Lifecycle

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.bus.bind(self.loop)
        self.store.execute(
            "UPDATE drafts SET status='uncertain', outcome=? WHERE status='sending'",
            ("Chief of Staff stopped while sending. Check whether it was delivered.",),
        )
        self.asks.expire_requests()
        await self.connect()
        for source in self.all_sources():
            await source.start()
        self.watch_job = asyncio.create_task(self.watch_loop())
        self.mirror_job = asyncio.create_task(self.workers.mirror_loop())
        await self.habits.start()
        await self.connector.start()
        self.background(self.abilities.sync())
        if self.store.drafts("uncertain"):
            self.background(self.drafts.check_uncertain())

    def all_sources(self) -> list[Source]:
        return [self.slack, self.github, *self.sources]

    async def stop(self):
        for job in (self.watch_job, self.mirror_job):
            if job:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
        for source in self.all_sources():
            await source.stop()
        await self.habits.stop()
        await self.connector.stop()
        await self.disconnect()
        for task in list(self.jobs):
            task.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)

    async def connect(self):
        async with self.connect_lock:
            # Copilot starts afresh (at start, or reconnecting): the commands its shells were
            # running ended with the one before.
            self.store.execute("UPDATE commands SET ended_at=? WHERE ended_at IS NULL", (now(),))
            self.status = {"state": "starting", "error": ""}
            self.publish_state()
            try:
                self.backend = self.backend_factory(self.store.settings())
                await self.backend.start()
                auth = await self.backend.get_auth_status()
                if not auth.isAuthenticated:
                    raise RuntimeError("Copilot is not signed in. Run `copilot login`.")
                warning = await self.version_warning()
                self.sandbox_gh = await self.backend.gh_works_in_sandbox()
                await self.ensure_router()
                self.toolset.ensure_profile_card()
                # A worker whose start a stop interrupted starts again.
                self.store.execute(
                    "UPDATE sessions SET status='queued' WHERE status='starting' "
                    "AND closed_at IS NULL"
                )
                await self.reconcile_sends()
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
            except Exception as exc:
                logger.exception("Copilot runtime failed to start")
                self.status = {"state": "error", "error": describe(exc)[:500]}
            self.publish_state()
        if self.ready():
            self.background(self.workers.pump())
            self.background(self.workers.refresh_elsewhere())
            self.background(self.github.signed_in())  # Which GitHub accounts workers can use.

    async def version_warning(self) -> str:
        """A note for the owner when the agent CLI is older than the versions we tested."""
        backend = self.connected()
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
        sessions = self.states.take_all_handles()
        # Their turns end with the connection: no idle event will come to free their slots.
        self.states.end_turns()
        await asyncio.gather(*(s.disconnect() for s in sessions), return_exceptions=True)
        if backend:
            with suppress(Exception):
                await backend.stop()

    async def reconnect(self):
        await self.disconnect()
        await self.connect()

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

    def background(self, coroutine: Awaitable):
        task = asyncio.ensure_future(coroutine)
        self.jobs.add(task)

        def done(finished: asyncio.Task):
            self.jobs.discard(finished)
            if not finished.cancelled() and finished.exception():
                logger.error("Background task failed", exc_info=finished.exception())

        task.add_done_callback(done)

    # Sessions

    def router_id(self) -> str:
        router_id = self.store.get("router_id")
        if not router_id:
            raise RuntimeError("The Router has not started yet.")
        return router_id

    def sandbox_policy(self, row: dict) -> SandboxPolicy | None:
        """What a worker's shell commands may touch; None when they run unsandboxed, like the
        owner's own Copilot sessions (whose Copilot sandbox settings then apply, if any)."""
        if not (
            row["role"] == WORKER
            # Workers that read Slack always: what they read must not leave through a command.
            and (self.store.settings().worker_sandbox or row["slack"])
            and self.sandbox_host
            and self.backend
            and self.backend.capabilities.sandbox
        ):
            return None
        checkouts = self.workspaces.checkouts(row)
        repositories = git_dirs(checkouts)
        settings = self.store.settings()
        writable = [*self.workspaces.writable(row), *repositories, *settings.sandbox_writable]
        if settings.shared_caches and not row["slack"]:
            # Packages its installs need are what the owner's installs already have (checked
            # against lockfiles before use). Not for workers that read Slack.
            writable += shared_caches()
        readonly = [
            path for git_dir in repositories for path in protected_git_paths(git_dir, checkouts)
        ]
        ssh = Path.home() / ".ssh"
        readonly += [str(ssh / name) for name in ("known_hosts", "config") if (ssh / name).exists()]
        if agent := os.environ.get("SSH_AUTH_SOCK"):
            writable.append(agent)  # Signing through the agent; key files stay unreadable.
        return SandboxPolicy(
            readwrite=tuple(dict.fromkeys(path for path in writable if path)),
            readonly=tuple(readonly),
            # Workers that read Slack must not carry what they read anywhere but GitHub.
            allowed_hosts=GITHUB_HOSTS if row["slack"] else (),
            # Tests that start a server, Gradle's daemon and the like need localhost; not for
            # workers that read Slack, whose content must not reach the owner's local services.
            local_network=not row["slack"],
            # Its own daemons, yes; the owner's (their tokens are in these files), no.
            denied=tuple(service_secrets()),
        )

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
        row = self.update_session(session_id, yolo=mode)
        await self.refresh_session(row)  # Its instructions mention the mode.
        return self.view(row)

    async def refresh_session(self, row: dict):
        """The session picks up new instructions from its next turn."""
        # A worker that reported "done" may still be finishing its turn.
        if row["status"] == "working" or self.states[row["id"]].busy:
            self.states[row["id"]].stale = True  # Refreshed once its current turn ends.
        else:
            await self.detach(row["id"])

    async def add_repo(self, caller: str, repo: str):
        row = self.store.session(caller)
        workspace = self.workspaces.workspace(row)
        await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        slug = row["branch"].removeprefix(BRANCH_PREFIX) if row["branch"] else workspace.name
        checkouts = self.workspaces.checkouts(row)
        if repo.startswith(("/", "~")):
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
        if self.store.directory.is_relative_to(path):
            raise ValueError("Choose a folder that does not contain Chief of Staff's own data.")
        if old.code_folder and old.code_folder != str(path):
            raise ValueError(f"Your code folder is {old.code_folder}; change it in Settings.")
        if old.code_folder != str(path):
            new = Settings(**{**old.model_dump(), "code_folder": str(path)})
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
        new = Settings(**{**old.model_dump(), "projects": [*old.projects, str(path)]})
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
        _, error = await asyncio.wait_for(process.communicate(), 15)
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
            return MESSENGER_TOOLS
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
        return names

    def options(self, row: dict) -> dict:
        if self.special(row) in TOOLLESS:
            return self.helper_options(row)
        settings = self.store.settings()
        prompt = PROMPTS[row["role"]]
        if self.special(row) == "connector":
            prompt = prompts.CONNECTOR  # Not a worker with tasks to report.
        elif self.special(row) == "messenger":
            prompt = prompts.MESSENGER
        sandbox = self.sandbox_policy(row)
        if row["role"] == WORKER:
            prompt += "\n" + prompts.worker_context(
                row["cwd"],
                str(self.workspaces.workspace(row)),
                self.workspaces.checkouts(row),
                self.yolo(row),
                ("on-no-gh" if self.sandbox_gh is False else "on") if sandbox else "",
                caches=bool(sandbox) and settings.shared_caches and not row["slack"],
                local=bool(sandbox and sandbox.local_network),
                gradle=bool(sandbox) and (Path.home() / ".gradle" / "caches").is_dir(),
                gh_accounts=self.github.accounts_cache[1],
            )
        if (profile := self.store.get("profile", "")) and row["role"] != WATCHDOG:
            prompt += f"\n<owner_profile>\n{profile}\n</owner_profile>"
        prompt += self.learning.block(row)
        prompt += self.habits.block(row)
        if row["role"] == SLACK:
            prompt += self.drafts.style_examples()
        if row["role"] in {ROUTER, WORKER}:
            prompt += self.asks.setup_block()
        tools = [self.tools[name] for name in self.tool_names(row)]
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
            # Copilot hides MCP tools behind a search once a session has more than 30 tools.
            # Chief of Staff's own tools must not tip a worker over sooner than the owner's own
            # sessions: their MCP tools stay in plain sight in both, or are searched in both.
            options["tool_search"] = {"defer_threshold": COPILOT_DEFER_THRESHOLD + len(tools)}
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
            "client_name": "chiefofstaff",
        }
        if model := self.model_for(row):
            options["model"] = model
        options.update(self.tuning(row))
        return options

    def model_for(self, row: dict) -> str:
        settings = self.store.settings()
        chosen = row.get("model") or settings.role_models.get(row["role"]) or settings.model
        if row.get("id") and self.store.get(f"fallback:{row['id']}") == "model":
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
        if row.get("id") and self.store.get(f"fallback:{row['id']}"):
            # Copilot refused this session's model with these options: the model's own defaults.
            if not info:
                return {}
            defaults: dict[str, str] = {}
            if info["efforts"]:
                defaults["reasoning_effort"] = default_effort(info)
            if info["long_context"]:
                defaults["context_tier"] = "default"
            return defaults if switching else {}
        if not info:
            # Copilot's default model, before its first turn shows which it is: Copilot applies
            # the effort to it (open_live retries without one if the default takes none).
            return {"reasoning_effort": effort} if effort else {}
        result = {}
        if info["efforts"]:
            if effort in info["efforts"]:
                result["reasoning_effort"] = effort
            elif switching:
                result["reasoning_effort"] = default_effort(info)
        if info["long_context"]:
            if row.get("context") == "long_context":
                result["context_tier"] = "long_context"
            elif switching:
                result["context_tier"] = "default"
        return result

    def shown_effort(self, row: dict) -> str:
        info = self.model_info(self.effective_model(row))
        return self.tuning(row).get("reasoning_effort") or (info or {}).get("default_effort", "")

    async def recover_model(self, row: dict, error: str) -> bool:
        """Copilot refused the session's model ("not supported"): first without the extra
        options (thinking effort, long context), then on Copilot's default model. The turn that
        failed is sent again. False when there is nothing left to fall back to."""
        session_id = row["id"]
        stage = self.store.get(f"fallback:{session_id}") or ""
        tuned = bool(self.tuning(row))
        chosen = self.model_for(row)
        if not stage and tuned:
            stage = "tuning"
        elif stage != "model" and chosen:
            stage = "model"
            refused = sorted(self.refused_models() | {chosen})
            self.store.set("refused_models", json.dumps(refused))
        else:
            return False
        self.store.set(f"fallback:{session_id}", stage)
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
                await session.set_model(model, **tuning)
        # What the failed turn was given is sent again.
        if (before := self.store.get(f"prevsent:{session_id}")) is not None:
            self.store.set(f"sent:{session_id}", before)
        self.background(self.delivery.resume(session_id))
        return True

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
            await session.set_model(model, **tuning)

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
        state.echoes = False
        if state.busy:
            # A disconnected session ends no turn: free its worker slot.
            state.busy = False
            self.background(self.workers.pump())
        if session := self.states.take_handle(session_id):
            with suppress(Exception):
                await session.disconnect()

    async def available_models(self) -> list[dict]:
        cached_at, models = self.models_cache
        if models and time.monotonic() - cached_at < 600:
            return models
        models = [
            describe_model(model)
            for model in await self.connected().list_models()
            if not model.policy or model.policy.state != "disabled"
        ]
        self.models_cache = (time.monotonic(), models)
        return models

    async def check_model(self, model: str):
        if model and model not in {item["id"] for item in await self.available_models()}:
            ids = ", ".join(item["id"] for item in await self.available_models())
            raise ValueError(f"Unknown model {model!r}. Available: {ids}")

    async def set_session_model(
        self, session_id: str, model: str, effort: str = "", context: str = ""
    ) -> dict:
        await self.check_model(model)
        row = self.store.session(session_id)
        info = self.model_info(self.effective_model({**row, "model": model}))
        if effort and effort not in (info["efforts"] if info else []):
            supported = ", ".join(info["efforts"]) if info and info["efforts"] else "none"
            raise ValueError(f"That model's thinking efforts: {supported}.")
        if context and not (info and info["long_context"]):
            raise ValueError("That model has no long-context option.")
        row = self.update_session(session_id, model=model, effort=effort, context=context)
        await self.apply_model(row)
        return self.view(row)

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
        with suppress(Exception):
            await self.available_models()  # Efforts and context tiers depend on the model.
        options = self.options(row)
        copilot_id = copilot or self.copilot_id(row["id"])
        name = ("Chief of Staff · " + clean_title(row["title"]))[:100]
        connection = self.connections
        if create:
            session = await self.without_unsupported_effort(
                row,
                options | self.trust(row),
                lambda tuned: self.connected().create_session(
                    session_id=copilot_id, name=name, **tuned
                ),
            )
            self.store.set(f"created:{row['id']}", "1")
            # Copilot keeps a session only once it has had a message: until then it cannot
            # be resumed after a restart, and is created again instead.
            self.store.set(f"fresh:{copilot_id}", "1")
        else:
            await self.refuse_if_open_elsewhere(row)
            try:
                session = await self.without_unsupported_effort(
                    row, options, lambda tuned: self.connected().resume_session(copilot_id, **tuned)
                )
            except Exception:
                if not self.store.get(f"fresh:{copilot_id}"):
                    raise
                session = await self.without_unsupported_effort(
                    row,
                    options | self.trust(row),
                    lambda tuned: self.connected().create_session(
                        session_id=copilot_id, name=name, **tuned
                    ),
                )
        if self.connections != connection:
            # Copilot reconnected meanwhile: this session belongs to the old connection.
            with suppress(Exception):
                await session.disconnect()
            raise RuntimeError("Copilot reconnected meanwhile; try again.")
        session_id = row["id"]
        loop = self.loop or asyncio.get_running_loop()

        def forward(event: Any):
            # The SDK calls this from its reader thread; events are handled on the loop.
            loop.call_soon_threadsafe(self.events.on_event, session_id, event, session)

        session.on(forward)
        self.states[session_id].handle = session
        if not create and row["role"] == WORKER:
            await self.workers.import_history(row, session)
        self.publish_session(self.store.session(session_id))
        return session

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
            if "reasoning_effort" not in options or "effort is not supported" not in str(exc):
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

    async def refuse_if_open_elsewhere(self, row: dict):
        """Two programs writing to one Copilot session would interleave its history: a session
        open in a terminal is left alone until it is closed there."""
        self.elsewhere = await self.connected().open_elsewhere()
        if pid := self.elsewhere.get(self.copilot_id(row["id"])):
            raise RuntimeError(
                f"“{row['title']}” is open in another Copilot window (process {pid}). Exit it "
                "there (/exit), then try again."
            )

    async def session_for(self, session_id: str):
        if session := self.states.handle(session_id):
            return session
        if not self.backend:
            self.require_ready()
        async with self.states[session_id].lifecycle:
            if session := self.states.handle(session_id):
                return session
            row = self.store.session(session_id)
            created = self.store.get(f"created:{session_id}")
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

    def handover(self) -> str:
        """What a new Router session needs to know, from Chief of Staff's records."""

        def clip(text: str, size: int = 240) -> str:
            text = " ".join((text or "").split())
            return text if len(text) <= size else text[: size - 1] + "…"

        router = self.router_id()
        lines = [
            "You take over as the owner's Router from an earlier Router session whose context "
            "had grown long. This is what it knew; list_sessions has the current details."
        ]
        sessions = [row for row in self.store.open_sessions() if row["role"] != ROUTER]
        lines.append("\nOpen sessions:" if sessions else "\nOpen sessions: none.")
        for row in sessions:
            lines.append(
                f"- {short(row['id'])} “{row['title']}” ({row['role']}, {row['status']})"
                + (f" task: {clip(row['task'], 160)}" if row["task"] else "")
                + (f" | last report: {clip(row['report'], 200)}" if row["report"] else "")
            )
        if tasks := self.tasks.listed():
            lines.append("\nTasks not done yet (review = finished, waiting for the owner's check):")
            lines += [
                f"- {t['id']} “{t['title']}” ({t['status']}, worker {short(t['session_id'])})"
                + (
                    f" waits for {', '.join(d['id'] for d in deps)}"
                    if (deps := self.tasks.unmet(t))
                    else ""
                )
                for t in tasks
            ]
        if closed := self.workers.recently_closed(8):
            lines.append("\nRecently closed workers (message_session reopens one):")
            lines += [
                f"- {short(r['id'])} “{r['title']}”: {clip(r['report'], 160)}" for r in closed
            ]
        if drafts := self.store.drafts():
            lines.append("\nCards waiting for the owner:")
            lines += [
                f"- {d['kind']} from {short(d['session_id'])}: {clip(d['text'])}" for d in drafts
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
            try:
                await self.open_live(row)
                return
            except Exception:
                logger.warning("Router could not be resumed; starting a new one.", exc_info=True)
                self.store.update_session(row["id"], closed_at=now())
        session_id = str(uuid4())
        row = self.store.create_session(
            session_id, ROUTER, "Router", "idle", cwd=str(self.role_dir(ROUTER))
        )
        self.store.set("router_id", session_id)
        await self.open_live(row, create=True)

    async def ensure_slack_session(self):
        if not self.slack.configured():
            return
        rows = self.store.open_sessions(SLACK)
        if rows:
            try:
                await self.session_for(rows[0]["id"])
                return
            except Exception:
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
            if (row["role"] == SLACK or row["slack"]) and row["status"] != "working":
                await self.detach(row["id"])
        if self.ready():
            await self.ensure_slack_session()
        self.publish_state()

    async def reconcile_sends(self):
        """Sends that a stop interrupted: check whether they arrived before anything is resent."""
        for key in self.store.keys("sending:"):
            session_id = key.removeprefix("sending:")
            row = self.store.find_session(session_id)
            if not row or row["closed_at"]:
                self.store.delete(key)
                continue
            arrived = await self.delivery.reconcile(session_id)
            outcome = {True: "it arrived", False: "resending", None: "unknown, resending"}
            logger.info("Interrupted send to %s: %s.", session_id, outcome[arrived])

    async def resume_working(self):
        """After a restart, sessions that were mid-turn continue, with what they had not handled."""
        for row in self.store.open_sessions():
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
            # Messages it never received go out as they are, with the note; only the rest
            # are quoted.
            unhandled = [message["meta"]["prompt"] for message in since if message["id"] <= sent]
            if any(message["kind"] == "owner" for message in since):
                self.states[row["id"]].owner_turn = True
            note = "Chief of Staff restarted while you were working. Continue where you left off."
            if unhandled:
                note += (
                    " Messages you received since you were last idle (some may already be "
                    "handled), quoted:\n\n" + "\n\n".join(text[:1500] for text in unhandled[-10:])
                )
            prompt = wrap("system", "restart", note)
            try:
                await self.delivery.deliver(
                    row["id"],
                    prompt,
                    kind="notice",
                    display="Chief of Staff restarted; asked the session to continue.",
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
        self.bus.publish("message", message)
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
        return row

    def fail(self, row: dict, message: str):
        self.states[row["id"]].busy = False
        self.add_message(row["id"], "error", message)
        self.update_session(row["id"], status="failed", activity="")
        self.delivery.defer_pending(row["id"])  # Not a turn by itself after a restart.
        self.task_actions.fail(row["id"], message)
        if row["role"] == WORKER:
            self.background(self.workers.pump())  # Its slot is free for a queued worker.

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
    ) -> dict:
        """`learn`: the owner typed it (not a message the app sends for them). A message to a
        worker is also noted for the Router (`tell_router`), which must know what the owner
        asked for."""
        row = self.store.session(session_id)
        if row["closed_at"] and row["role"] == WORKER:
            await self.workers.reopen(session_id)
        if row["role"] == WORKER and clear_questions:
            # The owner answers what it asked on the task it is on (others' questions stay).
            current = self.tasks.current(session_id)
            self.asks.answered(session_id, current["id"] if current else "")
            self.asks.answered(session_id, "")
        # Answers come in the language of this message, whatever the conversation used before.
        spoken = self.heard(text) if learn else language(text)
        prompt = wrap("owner", "message", text, reply_in=spoken)
        meta: dict[str, Any] = {}
        switched = self.switched_language(session_id, spoken)
        notes = [switched]
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
        context = [wrap("system", "context", note) for note in notes if note]
        prompt = "\n\n".join([prompt, *context])
        task = self.tasks.current(session_id) if row["role"] == WORKER else None
        if task:
            # What its turn is about now; a chat with it, not a task card's follow-up.
            meta |= {"task_id": task["id"], "chat": True}
        if task and task["status"] in {"review", "done", "failed", "cancelled"}:
            # A question changes nothing, but new work it reports comes back for review.
            self.store.set(f"followup:{task['id']}", "1")
        message = await self.delivery.deliver(
            session_id,
            prompt,
            kind="owner",
            display=text,
            meta=meta,
            # Steered into a Router turn written in the other language, the answer stays in it;
            # its own turn (Router turns are short) starts in the owner's.
            steer=not (switched and row["role"] == ROUTER),
        )
        if row["role"] == WORKER and tell_router:
            self.owner_told(row, text)
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

    def heard(self, text: str) -> str:
        """The language of what the owner wrote, remembered when clear: what they read next
        (reports, the Router's notes about them, new tasks) is in it."""
        spoken = language(text)
        if spoken:
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
        """For a message to a worker deep in a step: it reads it only when the step ends."""
        row = self.store.session(session_id)
        since, _ = self.states[session_id].step()
        if row["role"] != WORKER or not since or time.time() - since < STEP_NOTE_SECONDS:
            return ""
        return "It reads this when its current step ends. " + self.progress_line(row)

    async def owner_send_to(
        self, router_id: str, worker_id: str, text: str, attachments: Sequence[Attachment] = ()
    ) -> dict:
        """From the Router chat, straight to one worker; the Router hears of it later."""
        row = self.store.session(worker_id)
        if row["role"] != WORKER:
            raise ValueError("Address a worker.")
        message = await self.owner_send(row["id"], text, attachments=attachments)
        note = self.busy_note(row["id"])
        if note:  # Where the owner is: Chat or the inbox.
            self.add_message(router_id, "notice", note, {"session_id": row["id"]})
        return {**message, "note": note}

    def owner_told(self, row: dict, text: str):
        """What the owner told a worker directly (in its timeline, @ in Chat, on a task card):
        the Router hears of it with its next message, so it never takes the result for drift."""
        with suppress(RuntimeError):
            self.add_message(
                self.router_id(),
                "owner",
                text,
                {
                    "to": row["id"],
                    "to_title": row["title"],
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

    def projects_note(self) -> str:
        """Tell the Router when the configured projects changed since it last heard."""
        projects = self.workspaces.projects()
        heard = self.store.get("router_projects")
        if heard == json.dumps(projects):
            return ""
        self.store.set("router_projects", json.dumps(projects))
        if heard is None:
            return ""  # The Router's first look at projects is through list_sessions.
        listed = "\n".join(f"- {project}" for project in projects[:40]) or "- (none)"
        return f"The configured projects are now:\n{listed}"

    async def tell_router(self, row: dict, status: str, summary: str, task: dict | None = None):
        title = task["title"] if task else row["title"]
        await self.delivery.deliver(
            self.router_id(),
            wrap(
                sender(row),
                "report",
                summary,
                status=status,
                task=task["id"] if task else None,
                title=title,
                # What the Router tells the owner about it is in the owner's language.
                reply_in=self.store.get("owner_language"),
            ),
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
        try:
            await self.delivery.deliver(
                self.router_id(),
                # What the Router tells the owner about it is in the owner's language.
                wrap(
                    source, kind, text, trust=UNTRUSTED, reply_in=self.store.get("owner_language")
                ),
                batch=True,
                kind="inbound",
                display=(display or text)[:2000],
                meta=meta,
            )
        except Exception:
            for key in fresh:
                self.store.execute("DELETE FROM triggers WHERE key=?", (key,))
            raise
        return True

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
        self, session_id: str, keys: list[str], *, given: bool = True, task_id: str | None = None
    ) -> list[str]:
        """`given`: the Router or the owner gave the session these threads (not the session
        itself), so it may also draft replies there. `task_id`: the task it follows them for
        (default: the one its turn is on, else its current one); a thread waited on is that
        task's, not every task of the worker."""
        followed = [threads.normalize(key) for key in keys]
        row = self.store.session(session_id)
        self.check_follow(row, followed)
        if task_id is None and row["role"] == WORKER:
            current = self.tasks.current(session_id)
            task_id = self.turn_task(session_id) or (current["id"] if current else "")
        for key in followed:
            self.store.follow(key, session_id)
            if given:
                self.store.set(f"given:{session_id}:{key}", "1")
            if task_id:
                bound = self.store.follow_tasks(session_id, key)
                if task_id not in bound:
                    self.store.set(f"followtask:{session_id}:{key}", json.dumps([*bound, task_id]))
            for prefix in ("ghfail", "slackfail", "gh404", "ghskip", "slackskip"):
                self.store.set(f"{prefix}:{key}", "0")
        self.store.record(session_id, "links", followed)
        if followed:
            self.publish_session(self.store.session(session_id))
        return followed

    def followed(self, prefix: str) -> list[dict]:
        return self.store.followed(prefix)

    def missing(self, thread: str, why: str):
        """A followed thread that does not exist: its followers stop following it, and say so
        (once; nothing of their tasks changes)."""
        for session_id in self.store.followers(thread):
            self.store.unfollow(thread, session_id)
            row = self.store.find_session(session_id)
            if not row:
                continue
            text = f"Stopped following {thread}: {why}."
            self.add_message(session_id, "notice", text, {"thread": thread})
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
        self, source: str, thread: str, keys: list[str], text: str, display: str | None = None
    ) -> bool:
        """New activity in a followed thread goes straight to its followers, not the Router."""
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
        delivered = []
        for session_id in followers:
            try:
                await self.delivery.deliver(
                    session_id,
                    wrap(source, "thread", text, thread=thread, trust=UNTRUSTED),
                    batch=True,
                    kind="inbound",
                    display=(display or text)[:2000],
                    meta={"source": source, "thread": thread},
                )
                delivered.append(self.thread_task_title(session_id, thread))
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
                f"{threads.label(thread)} → {', '.join(f'“{t}”' for t in delivered)}",
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
        self.bus.publish(
            "toast", {"text": text, "session_id": session_id, "needs_action": needs_action}
        )
        if self.store.settings().mac_notifications:
            self.background(self.notifier(title, text))

    # Periodic jobs

    async def watch_loop(self):
        while True:
            await asyncio.sleep(self.watch.interval)
            jobs = (
                self.watchdog.check,
                self.task_actions.refresh_open_prs,
                self.workers.tidy,
                self.workers.release_idle,
                self.drafts.check_posted_by_owner,
                self.task_actions.wake_check_backs,
                self.task_actions.nudge_stalled,
                # Queued work starts once memory frees up, not only when a turn ends.
                self.workers.pump,
                self.digest_once,
                self.asks.expire_questions,
                self.focus.check,
                self.workers.refresh_elsewhere,
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
        text = digest(self.store, since, moment)
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
            "checkouts": self.workspaces.checkouts(row),
            # Its own folder (its checkouts are in it, or its files when it has none).
            "workspace": str(self.workspaces.workspace(row)) if row["role"] == WORKER else "",
            "credits": self.store.value(
                "SELECT coalesce(sum(credits), 0) AS c FROM usage WHERE session_id=?", (row["id"],)
            ),
            "task": self.toolset.task_brief(self.tasks.current(row["id"])),
            # The task its current (or last) turn is about: what its steps are for.
            "turn_task": self.working_on(row["id"]) if row["role"] == WORKER else "",
            "display_title": self.display_title(row),
            "elsewhere": self.copilot_id(row["id"]) in self.elsewhere and not row["closed_at"],
            "terminal": self.terminal.get(row["id"]),
            **(self.progress(row) if row["role"] == WORKER else {}),
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

    def hidden_artifacts(self) -> list[str]:
        return json.loads(self.store.get("artifacts_hidden") or "[]")

    def hide_artifact(self, key: str, hidden: bool = True) -> list[str]:
        """The owner's Artifacts list without (or again with) this one: a PR's key or a link."""
        key = key.strip()[:600]
        if not key:
            raise ValueError("Which artifact?")
        found = [item for item in self.hidden_artifacts() if item != key]
        if hidden:
            found = [*found, key][-500:]
        self.store.set("artifacts_hidden", json.dumps(found))
        self.publish_state()
        return found

    def took_up(self, session_id: str, tasks: list[str]):
        """Its conversation shows instructions on these tasks: its turn is on the last; each has
        been taken up (one given while it was on another waited its turn until now)."""
        for task_id in tasks:
            self.store.set(f"started:{task_id}", "1")
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
        """A worker by what it is on: its current task while that is open, else its newest open
        task, else its newest task; the title it was started with when it has none. The same
        name everywhere: sidebar, Home, notices and away messages."""
        if row["role"] != WORKER:
            return row["title"]
        tasks = self.tasks.for_session(row["id"])
        if not tasks:
            return row["title"]
        current = self.tasks.current(row["id"])
        if current and current["status"] in OPEN:
            return current["title"]
        if open_ := [task for task in tasks if task["status"] in OPEN]:
            return open_[-1]["title"]
        return tasks[-1]["title"]

    def thread_task_title(self, session_id: str, thread: str) -> str:
        """Which of a worker's tasks a followed thread is about, by its title: the task whose
        PRs or instructions name it, else the worker's display title."""
        from .tools import names_pr

        row = self.store.session(session_id)
        found = threads.GITHUB_PR.fullmatch(thread)
        for task in reversed(self.tasks.for_session(session_id)):
            if found:
                repo, number = found.groups()
                if thread in threads.pull_requests(
                    " ".join(self.task_actions.pr_urls(task))
                ) or names_pr(f"{task['title']}\n{task['goal'] or ''}", repo, int(number)):
                    return task["title"]
        return self.display_title(row)

    def publish_state(self):
        self.bus.publish("state")

    def snapshot(self) -> dict:
        return {
            "runtime": self.status,
            "demo": getattr(self, "demo", False),
            "focus": self.focus.state(),
            "router_id": self.store.get("router_id"),
            "sessions": [self.view(row) for row in self.store.open_sessions()],
            "drafts": self.store.drafts(),
            "uncertain": self.store.drafts("uncertain"),
            # PRs by "owner/repo#n" with their titles; followed threads with a link to them.
            "pr_titles": self.store.items("prtitle:"),
            "artifacts_hidden": self.hidden_artifacts(),
            "thread_links": self.store.items("threadlink:"),
            "questions": self.asks.owner_questions(),
            "tasks": [
                self.task_actions.view(task)
                for task in self.tasks.listed(include_ended_since=hours_ago(TASKS_SHOWN_HOURS))
            ],
            "settings": self.store.settings().model_dump(),
            "slack": {**self.slack.status, "token_saved": bool(self.slack.tokens.get())},
            "github": self.github.status,
            "closed": [self.view(row) for row in self.workers.recently_closed(20)],
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

    async def settings_changed(self, old: Settings, new: Settings):
        if old.copilot_command != new.copilot_command or not self.ready():
            await self.reconnect()
        elif old.slack_enabled != new.slack_enabled:
            await self.refresh_slack()
        defaults = ("model", "role_models", "role_efforts")
        if self.ready() and any(getattr(old, key) != getattr(new, key) for key in defaults):
            for session_id in self.states.live_ids():
                row = self.store.find_session(session_id)
                if row and not (row["model"] and row["effort"]):
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
