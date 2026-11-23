"""Prepared for you (docs/guide.md): Chief of Staff keeps the owner's PRs moving until they merge,
and lists what it finds on their other PRs.

One switch (Settings → General, *Keep my PRs moving*), off by default: off, nothing here runs,
reads, shows or changes what any session gets (the hooks below return at once). On:

- **Claims.** Each eligible PR (the owner's own, from a worker's task, same repository, not
  stacked, not a draft) has one holder, recorded in the control plane (control.py): the task
  whose worker made it, or the owner. Any head the journal does not explain pauses it until
  the owner says Resume.
- **Signals and attempts.** CI failing at the head (`fix-ci`) or review comments to address
  (`address-review`) become an attempt in the PR's own worker, at the kind's level: Suggest (a
  diagnosis) or Prepare (a plan). PR news that is such a signal is held from the worker for the
  attempt (it still reaches it, in the attempt's message, and with its next message).
- **The step profile.** An attempt runs in its worker's own Copilot session, switched to the
  step profile while it runs (the `step:<session>` mark): an OS sandbox with no GitHub
  credentials and no route to GitHub, no MCP servers, plugins or hooks, a tool allowlist, and
  the permission checks whatever the worker's mode. Where that cannot be guaranteed (no
  sandbox here, a Copilot version whose probe did not pass, another agent's worker, a busy
  worker), the attempt does not run and the PR's row says why.
- **Plans.** What an attempt prepared waits for the owner: the commit (its changes classed,
  approval-class hunks first; checked on the exact commits before and after), the replies, the
  reruns. Only Chief of Staff publishes, from the plan the owner approved, through the journal
  (publish.py), as the account bound to the plan.
- **Merge when ready**, per PR: a merge matched to a head the owner accepted (or one Chief of
  Staff pushed from a plan they approved), once every condition holds.
- **Credits.** Attempts nobody clicked reserve their permitted spend plus Copilot's overshoot
  from the day's budget (0 by default: nothing runs unasked); a click is charged, never held
  back. Each attempt is settled from its worker's own log, each usage event once.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import integrity, threads
from .backend import COPILOT_AGENT, StepSandboxPolicy
from .control import (
    APPLYING,
    CLAIM_ENDED,
    DISPATCHING,
    HANDING,
    IDLE,
    PAUSED,
    PLANNED,
    PREPARED,
    QUEUED,
    RUNNING,
    STEPPING,
    Control,
    today,
)
from .envelope import UNTRUSTED, wrap
from .keeping import Keeping
from .models import GITHUB_DOMAINS, GITHUB_EXACT
from .policy import GITHUB_MCP, step_denial
from .receipts import APPROVAL
from .roles import WORKER
from .steps import Steps
from .store import now
from .workspaces import service_secrets

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

KINDS = ("fix-ci", "address-review")
KIND_WORDS = {"fix-ci": "CI failures", "address-review": "Review comments"}
DEFAULT_LEVEL = "suggest"
# Credits: defaults, tuned from live data as it comes. What an attempt may spend until its kind
# has a measured history (then the 90th percentile of its last 20 attempts, never more than a
# PR's day); a PR's day; and how far a turn runs past its interrupt (raised when an attempt
# overshoots it, kv `proactive:margin`). Measured live (claude-sonnet-5.5, 2026-11): resuming a
# worker's session in the step profile costs 4 to 6 credits first (a prompt-cache write over its
# context), then about one a call; a fix (reproduce, fix, commit, the frozen runs, the results)
# takes 15 to 20 calls. At 8, every attempt was interrupted after three or four calls.
PERMITTED = {"fix-ci": 25.0, "address-review": 25.0}
PR_DAY_CREDITS = 75.0
MARGIN = 2.0
# How often a claimed PR is read when nothing came about it.
READ_SECONDS = 300
# Steps at once, across workers (a background slot each).
STEPS_AT_ONCE = 1
# What a PR's claim keeps beside it (kv `<prefix>:<target>`), gone when the PR merges or closes.
TARGET_KEYS = ("acceptnote", "capped", "checksbefore", "clickfirst", "mergewhy", "stopped",
               "signedbranch", "noteligible")  # fmt: skip
# A plan's diff and replies are kept this long after its outcome (its journal for good).
PLAN_DAYS = 30
# Said of any action while it is off.
OFF_WORDS = "Prepared for you is off: turn on Keep my PRs moving in Settings → General."
# A Copilot version that failed the safety check is checked again after this long.
PROBE_RETRY_SECONDS = 6 * 3600
# Why an attempt waits (not refused) while this Copilot version gets its safety check.
PROBING = "is being checked first"
# Each step's folder, in its worker's own folder.
STEP_FOLDER = ".shepherd"
# The tools a step has: Copilot's file and shell tools, and its own two.
STEP_TOOLS = (
    "view", "create", "edit", "str_replace_editor", "glob", "grep", "bash", "read_bash",
    "write_bash", "stop_bash", "list_bash", "report_intent", "shepherd_result",
    "draft_github_review",
)  # fmt: skip
STEP_OWN_TOOLS = ("shepherd_result", "draft_github_review")
# GitHub's hosts, never reachable from a step (the sandbox's blocked list).
BLOCKED_HOSTS = (
    *GITHUB_DOMAINS, *(f"*.{domain}" for domain in GITHUB_DOMAINS), *sorted(GITHUB_EXACT),
    "*.ghcr.io", "github.githubassets.com",
)  # fmt: skip
# Hosts an install of a project needs, by what the project has (routines.md's derivation).
PROJECT_HOSTS = {
    "package.json": ("registry.npmjs.org", "registry.yarnpkg.com"),
    "pnpm-lock.yaml": ("registry.npmjs.org",),
    "pyproject.toml": ("pypi.org", "files.pythonhosted.org"),
    "requirements.txt": ("pypi.org", "files.pythonhosted.org"),
    "uv.lock": ("pypi.org", "files.pythonhosted.org"),
    "go.mod": ("proxy.golang.org", "sum.golang.org", "storage.googleapis.com"),
    "Cargo.toml": ("crates.io", "index.crates.io", "static.crates.io"),
    "Gemfile": ("rubygems.org", "index.rubygems.org"),
    "build.gradle": ("repo.maven.apache.org", "services.gradle.org", "plugins.gradle.org"),
    "build.gradle.kts": ("repo.maven.apache.org", "services.gradle.org", "plugins.gradle.org"),
    "pom.xml": ("repo.maven.apache.org",),
}
# What a failed CI log says when the failure is the infrastructure's, not the code's.
INFRASTRUCTURE = re.compile(
    r"runner has received a shutdown signal|lost communication with the server|"
    r"No space left on device|The hosted runner encountered an error|"
    r"exceeded the maximum execution time|Resource not accessible by integration|"
    r"secrets? (?:are|is) not (?:available|set)|\b50[234] (?:Bad Gateway|Service Unavailable|"
    r"Gateway Time-?out)\b|Could not resolve host|ETIMEDOUT|ECONNRESET|"
    r"API rate limit exceeded|toomanyrequests|unexpected EOF while reading|"
    r"The operation was canceled",
    re.IGNORECASE,
)
# Labels that hold a merge.
HOLD_LABELS = re.compile(r"do[ -]?not[ -]?merge|\bdnm\b|\bwip\b|work in progress|\bhold\b|blocked",
                         re.IGNORECASE)  # fmt: skip
# Rules a fast-forward push may meet; anything else on the head branch refuses the push.
PUSH_RULES = frozenset({"non_fast_forward", "deletion", "creation"})
# A disclosure line, when the owner wants one in a repository.
DISCLOSURE = "\n\n<sub>Drafted with an AI assistant; reviewed and posted by me.</sub>"
SHA = re.compile(r"[0-9a-f]{40}")


def short_label(target: str) -> str:
    """`api#12` for `github:acme/api#12`."""
    return target.removeprefix("github:").split("/", 1)[-1]


def repo_number(target: str) -> tuple[str, int]:
    repo, number = target.removeprefix("github:").rsplit("#", 1)
    return repo, int(number)


FAILED_CHECK = frozenset(
    {"FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "CANCELLED"}
)


def failing(details: dict) -> list[dict]:
    """The checks that failed at the head (each by its latest run), not a person's approval."""
    return [
        check
        for check in details.get("checks") or []
        if check.get("status") == "COMPLETED"
        and check.get("conclusion") in FAILED_CHECK
        and not APPROVAL.search(check.get("name") or "")
    ]


def pending(details: dict) -> list[dict]:
    return [check for check in details.get("checks") or [] if check.get("status") != "COMPLETED"]


def fingerprint(name: str, text: str) -> str:
    """A failure's identity: the check, and the error lines its log ends with (numbers, times
    and paths' variable parts left out), so the same failure is known again."""
    lines = [
        re.sub(r"\d+", "#", line.split("\t")[-1].strip())[:200]
        for line in text.splitlines()
        if re.search(r"error|fail|assert|exception|panic", line, re.IGNORECASE)
    ]
    digest = hashlib.sha1(  # noqa: S324 (an identity, not a secret)
        "\n".join([name, *sorted(set(lines))[-30:]]).encode()
    )
    return digest.hexdigest()[:16]


def writer(permission: str | None) -> bool:
    return permission in {"admin", "maintain", "write"}


class Shepherd(Keeping, Steps):
    """The feature's runtime part: the hooks today's paths call (inert while off), attempts in
    workers, plans and their approval, claims and merges."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.control = Control(runtime.store)
        # Workers in a step now: session id -> step id (the kv `step:<session>` marks, read at
        # start: a mark outlives a crash).
        self.stepping: dict[str, str] = {
            key.removeprefix("step:"): value for key, value in self.store.items("step:").items()
        }
        # What each running attempt spent so far, as its usage events came (the interrupt);
        # none for one whose first model call is not reported yet.
        self.spent: dict[str, float] = {}
        # Each running attempt's tool calls since its frozen runs were asked for.
        self.seen: dict[str, list[integrity.Ran]] = {}
        # One read, one attempt start and one dispatch at a time per PR.
        self.locks: dict[str, asyncio.Lock] = {}
        self.pump_lock = asyncio.Lock()
        # More git configuration for Chief of Staff's own fetch and push (tests point
        # github.com at a local repository).
        self.git_config: list[tuple[str, str]] = []
        # Content writes per account in the last hour (GitHub allows 80 a minute, 500 an hour).
        self.writes: dict[str, list[float]] = {}
        self.write_locks: dict[str, asyncio.Lock] = {}
        self.read_at: dict[str, float] = {}

    # The switch.

    def on(self) -> bool:
        return self.store.settings().proactive

    def active(self) -> bool:
        """On, not paused, with its tables."""
        settings = self.store.settings()
        return settings.proactive and not settings.paused and self.control.exists()

    def refusal(self) -> str:
        """Why the owner's actions on it do nothing now ("" when they do)."""
        settings = self.store.settings()
        if not settings.proactive or not self.control.exists():
            return OFF_WORDS
        return "Chief of Staff is paused: resume it first." if settings.paused else ""

    def level(self, kind: str) -> str:
        return self.store.settings().proactive_levels.get(kind, DEFAULT_LEVEL)  # type: ignore[arg-type]

    def handed_to(self, target: str) -> str:
        """The task a PR was handed to from Prepared for you ("" for none): its own work then."""
        return self.store.get(f"handed:{target}") or ""

    def lock(self, target: str) -> asyncio.Lock:
        return self.locks.setdefault(target, asyncio.Lock())

    # Hooks today's paths call: each returns at once while nothing is in a step.

    def marked(self, session_id: str) -> bool:
        return session_id in self.stepping

    def step_of(self, session_id: str) -> dict | None:
        step_id = self.stepping.get(session_id)
        return self.control.step(step_id) if step_id else None

    def folders(self, session_id: str) -> list[str]:
        """Where a step may write (its permission check's second line)."""
        step = self.step_of(session_id)
        if not step:
            return []
        evidence = step.get("evidence") or {}
        return [
            p for p in [step["tree"], step["folder"], *evidence.get("frozen", {}).values()] if p
        ]

    def denial(self, row: dict, request: Any) -> str | None:
        return step_denial(request, self.folders(row["id"]), row.get("cwd") or "")

    def owners_checkout(self, row: dict) -> str:
        """What a step hears of the owner's checkout (policy.owners_checkout refused it): never
        written, and never a copy of it or its worker's worktree instead (Runtime.own_copy):
        a step works in its own tree only."""
        tree = (self.step_of(row["id"]) or {}).get("tree") or "the step's own tree"
        return (
            "Denied in a shepherd step: that is the owner's own checkout, never written. Work "
            f"in {tree}."
        )

    def saw(self, session_id: str, name: str, arguments: Any):
        """A tool call of a step: the frozen runs' evidence is void if anything else ran."""
        step_id = self.stepping.get(session_id)
        if not step_id or step_id not in self.seen:
            return
        command = str((arguments or {}).get("command") or "") if isinstance(arguments, dict) else ""
        self.seen[step_id].append(integrity.Ran(tool=name, command=command))

    def usage(self, session_id: str, spent_now: float):
        """A step's usage event: interrupted once it reaches its permitted spend."""
        step_id = self.stepping.get(session_id)
        if not step_id:
            return
        spent = self.spent.get(step_id, 0.0) + spent_now
        self.spent[step_id] = spent
        step = self.control.step(step_id)
        if step and step["state"] == RUNNING and step["permitted"] and spent >= step["permitted"]:
            self.runtime.background(
                self.stop_step(session_id, "interrupted", "It reached the credits it may spend.")
            )

    # The step profile.

    def step_policy(self, row: dict, step: dict) -> StepSandboxPolicy:
        """What a step's commands may touch: its worktree (and its git folder, but what runs code
        for the owner), the frozen worktrees, its folder, and its worker's caches and temporary
        folder (the owner's caches and its toolchains read-only beneath, as for a sandboxed
        worker, Runtime.sandbox_policy); no credentials, no GitHub, only the hosts its project's
        install needs and the owner allowed; this computer's own servers only where the owner
        allowed them for the repository; none at all for a worker that reads Slack."""
        settings = self.store.settings()
        evidence = step.get("evidence") or {}
        own = [evidence["checkout"]] if evidence.get("checkout") else []
        trees = [c for c in [*own, *(evidence.get("frozen_checkouts") or {}).values()]
                 if c.get("worktree")]  # fmt: skip
        frozen = list((evidence.get("frozen") or {}).values())
        writes = [path for path in [step["tree"], *frozen, step["folder"]] if path]
        base = self.runtime.sandbox_policy(row, writes=writes, trees=trees)
        if base is None:  # (Never: a step's is made whatever its worker's.)
            raise ValueError("no sandbox for it")
        # The owner's git configuration, to read: without the credential proxy Copilot keeps it
        # out of reach, and git then refuses to run at all (a commit too).
        readonly = [*base.readonly, *(evidence.get("git_config") or [])]
        repo, _ = repo_number(step["target"])
        choice = settings.proactive_repos.get(repo)
        hosts = () if row["slack"] else self.step_hosts(step)
        return StepSandboxPolicy(
            readwrite=base.readwrite,
            readonly=tuple(dict.fromkeys(readonly)),
            allowed_hosts=hosts,
            local_network=bool(choice and choice.localhost) and not row["slack"],
            denied=tuple(dict.fromkeys([*base.denied, *step_denied()])),
            credentials=False,
            outbound=bool(hosts),
            blocked_hosts=BLOCKED_HOSTS,
            keychain=False,
        )

    def step_hosts(self, step: dict) -> tuple[str, ...]:
        found = list((step.get("evidence") or {}).get("hosts") or [])
        found += self.store.settings().step_hosts
        return tuple(dict.fromkeys(found))

    def step_options(self, row: dict, base: dict) -> dict:
        """A worker's options while its step runs: its own, with the step profile over them (its
        system prompt unchanged; the rules travel in the step's message)."""
        step = self.step_of(row["id"])
        if step is None:
            return base
        options = {
            key: value
            for key, value in base.items()
            if key not in {"mcp_servers", "skill_directories", "hook_directories",
                           "on_user_input_request", "on_elicitation_request", "tool_search"}
        }  # fmt: skip
        settings = self.store.settings()
        # Every MCP server the worker could have, by name: one already running in its session
        # stays, so each is also left out of its tools (available_tools).
        configured = sorted(
            set(self.copilot_mcp_servers()) | set(settings.mcp_servers)
            | set(base.get("mcp_servers") or {})
        )  # fmt: skip
        options["sandbox_config"] = self.runtime.connected().sandbox(self.step_policy(row, step))
        options["tools"] = self.runtime.toolset.step_tools()
        options["available_tools"] = list(STEP_TOOLS)
        options["enable_config_discovery"] = False
        options["enable_session_store"] = False
        options["disabled_mcp_servers"] = list(dict.fromkeys([GITHUB_MCP, *configured, "slack"]))
        if self.runtime.pre_tool_wanted(row, True):
            # Sandboxed, whatever its worker's mode: asked before each of its tools, as every
            # sandboxed worker is (its folder's settings looked at; a change stops the step).
            options["hooks"] = {"on_pre_tool_use": self.runtime.events.pre_tool}
        return options

    @staticmethod
    def copilot_mcp_servers() -> list[str]:
        """The MCP servers in the owner's Copilot configuration, by name (to deny each)."""
        from .backend import copilot_home

        path = copilot_home() / "mcp-config.json"
        try:
            servers = json.loads(path.read_text()).get("mcpServers") or {}
        except (OSError, ValueError, AttributeError):
            return []
        return [str(name) for name in servers] if isinstance(servers, dict) else []

    # Where an attempt may run.

    async def executor_refusal(self, row: dict | None, step: dict | None = None) -> str:
        """Why an attempt can't run in this worker on this computer ("" when it can), in the
        row's words."""
        if not row or row["closed_at"]:
            return "its worker was closed"
        if self.runtime.agent(row["id"]) != COPILOT_AGENT:
            return "its worker runs on Codex, not Copilot"
        if not (self.runtime.sandbox_host and self.runtime.backend
                and self.runtime.backend.capabilities.sandbox):  # fmt: skip
            return "this computer has no sandbox for it"
        if why := await self.probe_refusal():
            return why
        signed = float(self.store.get(f"signedbranch:{step['target']}") or 0) if step else 0
        if signed > time.time() - 86400:
            return "the branch requires signed commits"
        return ""

    def busy(self, row: dict) -> bool:
        """Whether its worker is at something now (a turn, a command it left running, its
        session open in a terminal, messages waiting for it): an attempt waits."""
        state = self.runtime.states[row["id"]]
        return (
            row["status"] not in {"idle", "done", "waiting"}
            or state.busy
            or state.held
            or self.runtime.copilot_id(row["id"]) in self.runtime.elsewhere
            or self.runtime.delivery.has_unsent(row["id"])
        )

    async def probe_refusal(self) -> str:
        """Why this Copilot version may not run steps: its probe has not passed here."""
        try:
            version = await self.runtime.connected().version()
        except Exception:
            return "Copilot's version could not be read"
        found = json.loads(self.store.get(f"stepprobe:{version}") or "null")
        if found and found.get("passed"):
            return ""
        if found and found.get("why") and not self.probe_due(found):
            return f"Copilot {version} failed the safety check: {found['why']}"
        self.runtime.background(self.run_probe(version))
        return f"Copilot {version} {PROBING}"

    @staticmethod
    def probe_due(found: dict | None) -> bool:
        """A version not checked yet, or one that failed a while ago: checked (again). A pass
        is kept for good; a failure is looked at again after PROBE_RETRY_SECONDS (a network
        down, a sandbox fixed in the same version's settings)."""
        if not found:
            return True
        if found.get("passed"):
            return False
        return time.time() - float(found.get("checked") or 0) >= PROBE_RETRY_SECONDS

    async def run_probe(self, version: str):
        from .step_probe import probe

        found = json.loads(self.store.get(f"stepprobe:{version}") or "null")
        if getattr(self, "probing", False) or not self.probe_due(found):
            return
        self.probing = True
        try:
            result = await probe(self.runtime)
        except Exception as exc:
            logger.warning("The step check could not run.", exc_info=True)
            result = {"passed": False, "why": f"it could not run ({str(exc)[:200]})", "at": now(),
                      "checked": time.time()}  # fmt: skip
        finally:
            self.probing = False
        self.store.set(f"stepprobe:{version}", json.dumps(result))
        self.publish()

    # Turning it on and off.

    async def settings_changed(self, old: Any, new: Any) -> list[str]:
        """On: its tables (once), and a first look. Off or paused: drained (nothing it began goes
        on; nothing it prepared waits; what was dispatching is reconciled). S1 leaves nothing on
        GitHub to undo, so off completes at once. Off before and after (a pause): nothing."""
        if not (old.proactive or new.proactive):
            return []
        was = old.proactive and not old.paused
        now_on = new.proactive and not new.paused
        if now_on and not was:
            self.control.ensure()
            self.runtime.background(self.tick())
        elif was and not now_on:
            await self.drain("Prepared for you was turned off" if not new.proactive else "paused")
        self.publish()
        return []

    async def drain(self, why: str):
        if not self.control.exists():
            return
        self.control.bump()
        for session_id in list(self.stepping):
            await self.stop_step(session_id, "withdrawn", f"Stopped: {why}.")
        for step in self.control.steps("", QUEUED, PREPARED):
            if self.control.move_step(step["id"], (QUEUED, PREPARED), state="withdrawn", why=why,
                                      ended_at=now()):  # fmt: skip
                self.control.withdraw_planned(step["id"], why)
        # Operations sent already are not recalled: those in flight end (bounded), then any left
        # dispatching are reconciled.
        for _ in range(50):
            if not self.control.actions("", DISPATCHING):
                break
            await asyncio.sleep(0.1)
        from .publish import reconcile_all

        await reconcile_all(self)
        for task_id in {claim["holder"] for claim in self.control.claims() if claim["holder"]}:
            if task := self.runtime.tasks.find(task_id):
                self.runtime.task_actions.publish(task)
        self.publish()

    async def start(self):
        """After a restart: what was dispatching is reconciled first; a session left in a step
        is not continued (Runtime.resume_working skips it: here its attempt ends, interrupted,
        to be admitted again if still current)."""
        if self.on() and not self.control.exists():
            self.control.ensure()  # On in its settings, its tables never made: made now.
        if not self.control.exists():
            return
        from .publish import dispatch, reconcile_all

        with suppress(Exception):
            await reconcile_all(self)
        for session_id, step_id in list(self.stepping.items()):
            await self.crashed(session_id, step_id)
        for step in self.control.steps("", RUNNING):
            # Running, with no worker marked for it: ended with the run before.
            self.control.move_step(step["id"], RUNNING, state="interrupted", ended_at=now(),
                                   why="Chief of Staff restarted during it")  # fmt: skip
            self.release_claim(step["target"])
        for step in self.control.steps("", APPLYING):
            if self.control.actions(step["id"], PLANNED):
                # Approved, and stopped before all of it went out: the rest goes now, each
                # operation through the barrier again (a change of hands, or off, withdraws it).
                self.runtime.background(dispatch(self, step["id"]))
            else:
                self.finish_applying(step["id"])

    async def crashed(self, session_id: str, step_id: str = ""):
        """A worker left marked by a stop mid-attempt: never continued in its normal profile;
        its attempt is interrupted, and admitted again later only if still current."""
        step_id = step_id or self.stepping.get(session_id, "")
        self.unmark(session_id)
        with suppress(Exception):
            await self.runtime.detach(session_id)
        step = self.control.step(step_id) if step_id else None
        if step and step["state"] == RUNNING:
            self.control.move_step(step_id, RUNNING, state="interrupted", ended_at=now(),
                                   why="Chief of Staff stopped during it")  # fmt: skip
            self.end_run(step)
            self.release_claim(step["target"])
        if (row := self.store.find_session(session_id)) and row["status"] == "working":
            self.runtime.update_session(session_id, status="idle", activity="")

    def unmark(self, session_id: str):
        self.stepping.pop(session_id, None)
        self.store.delete(f"step:{session_id}")

    def mark(self, session_id: str, step_id: str):
        self.store.set(f"step:{session_id}", step_id)
        self.stepping[session_id] = step_id

    def release_claim(self, target: str):
        """The PR's claim back from stepping to idle (its holder and generation unchanged)."""
        claim = self.control.claim(target)
        if claim and claim["state"] == STEPPING:
            self.control.set_state(target, (claim["holder"], claim["generation"]), IDLE)

    def publish(self):
        self.runtime.bus.publish("prepared", self.snapshot())

    def finish_applying(self, step_id: str):
        from .publish import finish_applying

        finish_applying(self, step_id)

    def snapshot(self) -> dict:
        """What Home and Settings show of it ({} while off and never used)."""
        if not self.on() and not self.control.exists():
            return {}
        from .discovery import rows_view

        return {
            "on": self.on(),
            "prepared": rows_view(self) if self.on() and self.control.exists() else {},
            "steps_here": self.steps_here(),
            "spent": self.control.spent_today() if self.control.exists() else {},
            "repositories": self.repositories(),
        }

    def steps_here(self) -> str:
        """Whether steps can run on this computer, in a line (Settings)."""
        if not (self.runtime.sandbox_host and self.runtime.backend
                and self.runtime.backend.capabilities.sandbox):  # fmt: skip
            return "This computer has no sandbox, so Chief of Staff can't prepare fixes here."
        found = [json.loads(value or "{}") for value in self.store.items("stepprobe:").values()]
        if any(item.get("passed") for item in found):
            return ""
        failed = next((item for item in found if item.get("why")), None)
        if failed:
            return f"This Copilot version failed the safety check for fixes: {failed['why']}"
        return "Copilot gets a safety check before the first fix is prepared."

    def repositories(self) -> list[str]:
        """The repositories with PRs it keeps moving (Settings → their choices)."""
        found = {
            claim["target"].removeprefix("github:").rsplit("#", 1)[0]
            for claim in self.control.claims()
        }
        return sorted(found | set(self.store.settings().proactive_repos))

    # Periodic work: claims for the worker PRs in scope, their reads, attempts, discovery.

    async def tick(self):
        if not self.active():
            return
        if self.store.get("proactive:pruned") != (day := today()):
            self.store.set("proactive:pruned", day)
            self.control.prune_plans(PLAN_DAYS)
        settings = self.store.settings()
        if settings.proactive_worker_prs:
            await self.claim_worker_prs()
            for claim in self.control.claims(IDLE, PAUSED, STEPPING):
                if claim["holder_kind"] != "task":
                    continue
                if time.time() - self.read_at.get(claim["target"], 0) >= READ_SECONDS or (
                    self.store.get(f"ghhead:{claim['target']}") not in {None, claim["head"]}
                ):
                    try:
                        await self.read(claim["target"])
                    except Exception:
                        logger.info("Could not read %s.", claim["target"], exc_info=True)
        await self.pump()
        if settings.proactive_my_prs:
            from .discovery import discover

            with suppress(Exception):
                await discover(self)

    async def claim_worker_prs(self):
        """A claim for each eligible PR a worker's task made and follows (its own, by the
        owner's account): to that task, its origin; one it opened and never followed is followed
        for it then. Read once to tell eligibility; a PR found not eligible is not read again
        for a day."""
        actions = self.runtime.task_actions
        found: list[tuple[str, dict, dict]] = []
        for task in self.runtime.tasks.listed(include_ended_since=_hours_ago(72)):
            if task["status"] in {"cancelled", "failed"} or self.store.get(
                f"setaside:{task['id']}"
            ):
                continue
            if task["id"] in actions.receipts_due:
                # Its result's PRs and commits are being read: what an Accept records as the
                # head the owner reviewed. Claimed once they are, not before.
                continue
            row = self.store.find_session(task["session_id"])
            if not row or row["closed_at"] or row["role"] != WORKER:
                continue
            followed = set(actions.followed_for(task, self.store.follows(row["id"])))
            opened = actions.opened_prs(task["id"])
            prs = list((actions.receipts(task) or {}).get("prs") or [])
            # One it opened that its receipts could not read (GitHub did not answer for it as
            # its result was read, and they keep it out until it does): its own all the same,
            # read for its author and state as it is claimed (give_task).
            read = {threads.normalize(f"github:{pr['label']}") for pr in prs}
            prs += [
                {"label": label, "own": True, "state": "", "opened": True}
                for label in sorted(opened)
                if threads.normalize(f"github:{label}") not in read
            ]
            for pr in prs:
                target = threads.normalize(f"github:{pr['label']}")
                if not pr.get("own") or pr.get("state") not in {"OPEN", ""}:
                    continue
                if target not in followed:
                    # One it opened and never named by its URL in a report was never followed:
                    # it is followed as it is claimed. Not one it followed before (its record
                    # keeps it) and no longer does: it, the owner or the Router stopped that.
                    if (
                        target.removeprefix("github:") not in opened
                        or target in json.loads(row["links"] or "[]")
                        or self.store.get(f"stopped:{row['id']}:{target}")
                        or self.store.get(f"stoppedfor:{task['id']}:{target}")
                    ):
                        continue
                    pr = {**pr, "follow": True}
                if self.control.claim(target):
                    continue
                if float(self.store.get(f"noteligible:{target}") or 0) > time.time() - 86400:
                    continue
                found.append((target, task, pr))
        if not found:
            return  # Nothing to claim: GitHub is not asked anything.
        try:
            mine = await self.runtime.github.logins()
        except Exception:
            return
        for target, task, pr in found:
            if not (pr.get("author") in mine or pr.get("opened")) or self.control.claim(target):
                continue
            if await self.give_task(target, task) and pr.get("follow"):
                self.runtime.follow(task["session_id"], [target], given=False, task_id=task["id"])

    async def eligible(self, details: dict, mine: set[str]) -> str:
        """Why a PR is not one the shepherd keeps moving now ("" when it is)."""
        if details.get("fork") or details.get("head_repo") != details.get("repo"):
            return "from a fork: not yet"
        if details.get("draft"):
            return "a draft: not yet"
        if details.get("author") not in mine:
            return "not yours"
        if details.get("base_ref") != details.get("default_branch"):
            return "stacked: not yet"
        repo = details.get("repo") or ""
        stacked = await self.runtime.github.stacked_on(repo, details.get("head_ref") or "")
        if stacked is None:
            return "whether it is stacked could not be read"
        if stacked:
            return "stacked: not yet"
        return ""

    async def give_task(self, target: str, task: dict, *, paused: str = "", clicked: bool = False):
        repo, number = repo_number(target)
        try:
            details = await self.runtime.github.pr_details(repo, number)
            mine = await self.runtime.github.logins()
        except Exception:
            logger.info("Could not read %s to claim it.", target, exc_info=True)
            return None
        why = await self.eligible(details, mine)
        if not why and (details.get("merged") or (details.get("state") or "OPEN") != "OPEN"):
            why = "not open"
        if why:
            self.store.set(f"noteligible:{target}", str(time.time()))
            logger.info("Not keeping %s moving: %s.", target, why)
            return None
        claim = self.control.give(target, task["id"], details["head"], paused=paused)
        self.control.update_claim(
            target, details=details, read_at=time.time(),
            delivered=int(task["status"] in {"review", "done"}),
        )  # fmt: skip
        self.read_at[target] = time.time()
        if clicked:
            self.store.set(f"clickfirst:{target}", "1")
        self.runtime.task_actions.publish(self.runtime.tasks.get(task["id"]))
        return claim

    # Reading a claimed PR: ownership, staleness, signals, merging.

    async def read(self, target: str) -> dict | None:
        """Read a claimed PR now and act on what it shows: ended, paused (a head the journal
        does not explain, stacked, a fork), a plan made stale, a signal to try, a merge."""
        async with self.lock(target):
            claim = self.control.claim(target)
            if not claim or claim["state"] in {CLAIM_ENDED, HANDING}:
                return claim
            repo, number = repo_number(target)
            details = await self.runtime.github.pr_details(repo, number)
            self.read_at[target] = time.time()
            self.control.update_claim(target, head=details["head"], details=details,
                                      read_at=time.time())  # fmt: skip
            moved = details["head"] != (claim.get("details") or {}).get("head")
            claim = self.control.claim(target) or claim
            self.learn_flaky(target, details)
            if moved:
                self.publish_holder(claim)  # Its row offers what it is at now (Accept this head).
            task = self.runtime.tasks.find(claim["holder"]) if claim["holder"] else None
            if details["merged"] or details["state"] == "MERGED":
                await self.ended(claim, "merged")
                return self.control.claim(target)
            if details["state"] == "CLOSED":
                self.stale_plans(target, "The PR was closed.")
                return claim
            if task and not claim["delivered"] and task["status"] in {"review", "done"}:
                self.control.update_claim(target, delivered=1)
                claim = self.control.claim(target) or claim
            head = details["head"]
            if not self.control.explains(claim, head):
                if not claim["delivered"] and task and task["status"] in {"queued", "running",
                                                                          "waiting"}:  # fmt: skip
                    # Its task is still at work on it: its own commits, before its result.
                    self.control.give(target, task["id"], head)
                    claim = self.control.claim(target) or claim
                elif claim["state"] != PAUSED:
                    await self.pause(claim, f"new commits it didn't push ({head[:7]})", head)
                    claim = self.control.claim(target) or claim
            if claim["state"] != PAUSED:
                mine = await self.runtime.github.logins()
                why = await self.eligible(details, mine)
                if why.startswith(("stacked", "from a fork")):
                    await self.pause(claim, why, head)
                    claim = self.control.claim(target) or claim
            self.stale_plans(target, "The PR changed since it was prepared.", keep=head)
            await self.vanished_checks(claim, details)
            if claim["state"] == IDLE and task and claim["keep"]:
                await self.signals(claim, task, details)
        if claim["merge_when_ready"]:
            from .publish import try_merge

            await try_merge(self, target)  # Its dispatch takes the PR's lock itself.
        if task:
            self.runtime.task_actions.publish(task)
        return self.control.claim(target)

    def learn_flaky(self, target: str, details: dict):
        """A check that failed at a head (an attempt or a rerun was made for it, with the
        failure's fingerprint) and passes now at that same head is known flaky in its
        repository, for that failure: the next time it fails so, its plan is a rerun
        (infrastructure)."""
        passed = {
            check["name"]
            for check in details.get("checks") or []
            if check.get("status") == "COMPLETED" and check.get("conclusion") == "SUCCESS"
        }
        if not passed:
            return
        for step in self.control.steps(target):
            if step["kind"] != "fix-ci" or step["h0"] != details["head"] or not step["fingerprint"]:
                continue
            for name in set((step["signal"] or {}).get("checks") or []) & passed:
                key = f"flaky:{details['repo']}:{name}:{step['fingerprint']}"
                if not self.store.get(key):
                    self.store.set(key, now())
                    logger.info("%s is known flaky on %s now.", name, details["repo"])

    async def pause(self, claim: dict, why: str, head: str):
        """Paused where it is: an attempt in flight stopped, plans withdrawn as stale."""
        self.control.pause(claim["target"], why, head)
        await self.stop_target(claim["target"], f"Paused: {why}.")
        self.stale_plans(claim["target"], f"Paused: {why}.")

    async def stop_target(self, target: str, why: str, state: str = "stale"):
        for session_id, step_id in list(self.stepping.items()):
            step = self.control.step(step_id)
            if step and step["target"] == target:
                await self.stop_step(session_id, state, why)

    def stale_plans(self, target: str, why: str, keep: str = "", state: str = "stale"):
        """Plans prepared on a head the PR is no longer at go stale (never published); those of
        a holder that let it go are withdrawn."""
        for step in self.control.steps(target, QUEUED, PREPARED):
            if keep and step["h0"] == keep:
                continue
            if self.control.move_step(step["id"], (QUEUED, PREPARED), state=state, why=why,
                                      ended_at=now()):  # fmt: skip
                self.control.withdraw_planned(step["id"], why)

    async def ended(self, claim: dict, how: str):
        """Merged (or closed for good): the claim ends, its plans with it; the row is kept."""
        self.stale_plans(claim["target"], f"The PR was {how}.")
        await self.stop_target(claim["target"], f"The PR was {how}.")
        self.control.end(claim["target"], how)
        self.drop_step_logs(claim["target"])
        for prefix in TARGET_KEYS:
            self.store.delete(f"{prefix}:{claim['target']}")

    def drop_step_logs(self, target: str):
        """The logs a step read go once the PR merges or closes."""
        for step in self.control.steps(target):
            if step["folder"]:
                with suppress(OSError):
                    shutil.rmtree(step["folder"])

    async def vanished_checks(self, claim: dict, details: dict):
        """CI's check set on a head Chief of Staff pushed must include the one before's: a check
        that vanished pauses the PR for the owner."""
        before = json.loads(self.store.get(f"checksbefore:{claim['target']}") or "null")
        if not before or details["head"] != before.get("head"):
            return
        names = {check["name"] for check in details.get("checks") or []}
        if pending(details) or not names:
            return
        gone = sorted(set(before.get("names") or []) - names)
        self.store.delete(f"checksbefore:{claim['target']}")
        if gone:
            await self.pause(claim, f"a check vanished after the push: {', '.join(gone[:3])}",
                             details["head"])  # fmt: skip

    async def signals(self, claim: dict, task: dict, details: dict, news: dict | None = None,
                      clicked: bool = False) -> list[dict]:  # fmt: skip
        """What the PR shows now that a kind above Off acts on: an attempt queued for each (one
        per signal: the same head and failure, or the same comments, is not tried twice)."""
        queued = []
        if task["status"] not in {"review", "done"}:
            return queued  # Its task is still at work on it: the worker handles its PR.
        target = claim["target"]
        head = details["head"]
        clicked = clicked or bool(self.store.get(f"clickfirst:{target}"))
        failed = failing(details)
        if failed and not pending(details) and self.level("fix-ci") != "off":
            names = sorted({check["name"] for check in failed})
            signal = {"head": head, "checks": names, "news": news or {}}
            if step := await self.queue(claim, task, "fix-ci", signal, details, clicked=clicked):
                queued.append(step)
        if self.level("address-review") != "off":
            asks = await self.review_asks(details)
            if asks:
                signal = {"head": head, "threads": asks, "news": news or {}}
                if step := await self.queue(claim, task, "address-review", signal, details,
                                            clicked=clicked):  # fmt: skip
                    queued.append(step)
        if queued and clicked:
            self.store.delete(f"clickfirst:{target}")
        return queued

    async def review_asks(self, details: dict) -> list[dict]:
        """The review comments to address: unresolved threads whose last comment is from
        someone with write permission (not the owner), and, with changes requested, the
        reviewers' summaries. A commenter's permission decides relevance, never authority."""
        mine = await self.runtime.github.logins()
        repo = details["repo"]
        asks = []
        for thread in details.get("threads") or []:
            if thread["resolved"] or not thread["comments"]:
                continue
            last = thread["comments"][-1]
            if last["author"] in mine or not last["author"]:
                continue
            permission = await self.permission(repo, last["author"])
            asks.append({
                "thread": thread["id"], "comment": thread["comments"][0]["id"],
                "last": last["id"], "author": last["author"], "path": thread["path"],
                "line": thread["line"], "said": last["body"][:2000], "url": last["url"],
                "permission": permission or "unknown", "writer": writer(permission),
            })  # fmt: skip
        if not any(ask["writer"] for ask in asks) and details.get("review") != "CHANGES_REQUESTED":
            return []
        return asks

    async def permission(self, repo: str, login: str) -> str | None:
        key = f"ghperm:{repo}:{login}"
        cached = json.loads(self.store.get(key) or "null")
        if cached and time.time() - cached["at"] < 86400:
            return cached["permission"]
        found = await self.runtime.github.permission(repo, login)
        if found is not None:
            self.store.set(key, json.dumps({"permission": found, "at": time.time()}))
        return found

    # PR news, before today's delivery.

    async def signal(self, thread: str, keys: list[str], text: str, display: str | None) -> bool:
        """News on a claimed PR: when it is a signal for a kind above Off, it is held from the
        worker for the attempt (it rides along with the worker's next message, and the
        attempt's message carries it) and True; else False, and it is delivered as today."""
        if not self.active() or not self.store.settings().proactive_worker_prs:
            return False
        claim = self.control.claim(thread)
        if not claim or claim["holder_kind"] != "task" or claim["state"] == CLAIM_ENDED:
            return False
        if any(key.startswith(("github:head:", "github:state:")) for key in keys):
            self.read_at[thread] = 0  # Read at the next round: who moved the head.
        ci = [key for key in keys if re.search(r"^github:ci:.*:failed-", key)]
        reviews = [
            key for key in keys if key.startswith(("github:review:", "github:reviewcomment:"))
        ]
        if not (ci or reviews) or claim["state"] != IDLE or not claim["keep"]:
            return False
        if ci and self.level("fix-ci") == "off":
            ci = []
        if reviews and self.level("address-review") == "off":
            reviews = []
        if not (ci or reviews):
            return False
        task = self.runtime.tasks.find(claim["holder"])
        row = self.store.find_session(task["session_id"]) if task else None
        if not task or not row or task["status"] not in {"review", "done"}:
            return False
        try:
            repo, number = repo_number(thread)
            details = await self.runtime.github.pr_details(repo, number)
        except Exception:
            return False
        self.control.update_claim(thread, head=details["head"], details=details,
                                  read_at=time.time())  # fmt: skip
        claim = self.control.claim(thread) or claim
        if not self.control.explains(claim, details["head"]):
            return False  # Read at the next round, and paused then.
        news = {"text": text[:4000], "display": (display or text)[:4000]}
        steps = await self.signals(claim, task, details, news=news)
        if not steps:
            # An attempt at this commit for it is there already (waiting for Fix it, running,
            # or its plan ready): the news is that attempt's too, not the worker's to act on.
            kinds = {"fix-ci"} if ci else set()
            kinds |= {"address-review"} if reviews else set()
            found = self.control.open_step(thread)
            if not (found and found["h0"] == details["head"] and found["kind"] in kinds):
                return False
            steps = [found]
        held = wrap("github", "thread", text, thread=thread, trust=UNTRUSTED, task=task["id"])
        self.runtime.delivery.hold(
            row["id"], held, kind="inbound", display=(display or text)[:4000],
            meta={"source": "github", "thread": thread, "task_id": task["id"]},
        )  # fmt: skip
        words = {
            "fix-ci": "a fix is being prepared",
            "address-review": "replies are being prepared",
        }
        with suppress(RuntimeError):
            self.runtime.add_message(
                self.runtime.router_id(), "notice",
                f"{threads.label(thread)} → “{task['title']}” ({words[steps[0]['kind']]})",
                {"session_id": row["id"]},
            )  # fmt: skip
        self.runtime.task_actions.publish(task)
        await self.pump()
        return True

    # Attempts: queued, admitted, run in the worker, ended.

    async def queue(self, claim: dict, task: dict, kind: str, signal: dict, details: dict,
                    clicked: bool = False, level: str = "") -> dict | None:  # fmt: skip
        """An attempt for a signal, unless one for it is open or was tried (caps), or CI waits
        for a person. CI failures of the infrastructure, or of a check known to be flaky, get a
        rerun plan at once (no worker, no credits)."""
        target = claim["target"]
        head = details["head"]
        level = level or ("prepare" if clicked else self.level(kind))
        if level == "off":
            return None
        open_step = self.control.open_step(target)
        if open_step and open_step["h0"] == head and open_step["kind"] == kind:
            if clicked and open_step["state"] == QUEUED and not open_step["clicked"]:
                self.control.move_step(open_step["id"], QUEUED, clicked=1, level="prepare")
            return None
        if open_step and open_step["state"] in {RUNNING, APPLYING}:
            return None  # One attempt per PR at a time: the next read brings this one back.
        print_ = ""
        threads_asked: list[str] = []
        said: list[str] = []
        evidence: dict[str, Any] = {}
        if kind == "fix-ci":
            repo = details["repo"]
            try:
                runs = await self.runtime.github.failed_runs(repo, head)
            except Exception:
                runs = []
            logs = ""
            for run in runs[:3]:
                with suppress(Exception):
                    logs += await self.runtime.github.failed_log(repo, run["id"])
            names = ",".join(signal["checks"])
            print_ = fingerprint(names, logs)
            evidence = {"runs": [{k: run[k] for k in ("id", "name", "url", "attempt")}
                                 for run in runs[:5]], "log": logs[-200_000:]}  # fmt: skip
            why = self.infrastructure(runs, logs, repo, signal["checks"], print_)
            if why:
                return self.rerun_plan(claim, task, signal, details, runs, why, print_)
        else:
            asks = [ask for ask in signal.get("threads") or [] if ask["writer"]]
            threads_asked = [ask["thread"] for ask in asks]
            said = [ask["last"] for ask in asks]
        if why := self.control.cap_reached(target, kind, print_, head, threads_asked,
                                           clicked=clicked, said=said):  # fmt: skip
            logger.info("No attempt on %s (%s): %s.", target, kind, why)
            self.store.set(f"capped:{target}", json.dumps({"kind": kind, "why": why, "head": head}))
            self.runtime.task_actions.publish(task)
            return None
        if self.loop_stop(target, kind, print_):
            return None
        if open_step and open_step["state"] in {QUEUED, PREPARED}:
            self.stale_plans(target, "A newer signal came.")
        return self.control.add_step(
            target, kind, level, session_id=task["session_id"], task_id=task["id"],
            holder=claim["holder"], generation=claim["generation"],
            consent=self.control.generation(), h0=head, signal=signal, fingerprint=print_,
            threads=threads_asked, clicked=int(clicked), evidence=evidence,
        )  # fmt: skip

    def infrastructure(self, runs: list[dict], logs: str, repo: str, checks: list[str],
                       print_: str) -> str:  # fmt: skip
        """Why a CI failure needs a rerun, not code ("" when it is real): the infrastructure's
        (a lost runner, a full disk, a registry's 5xx, a fork with no secrets, a timeout in
        setup), or a check known flaky here (it failed this way and then passed on a rerun at the
        same head before)."""
        if found := INFRASTRUCTURE.search(logs):
            return f"the infrastructure failed ({found.group(0)[:60]})"
        if any(run["conclusion"] == "startup_failure" for run in runs):
            return "the workflow could not start"
        for name in checks:
            if self.store.get(f"flaky:{repo}:{name}:{print_}"):
                return f"{name} is known to fail this way and pass when run again"
        return ""

    def loop_stop(self, target: str, kind: str, print_: str) -> bool:
        """The shepherd stops and hands the PR to the owner: a failure back after its own fix,
        or two attempts in a row with no progress."""
        tried = [
            step
            for step in self.control.steps(target)
            if step["kind"] == kind and step["started_at"]
        ]
        if print_ and any(
            step["fingerprint"] == print_ and step["state"] == "published" for step in tried
        ):
            self.loop_stopped(target, kind, "the same failure came back after its fix")
            return True
        last = tried[-2:]
        if len(last) == 2 and all(
            step["state"] in {"nothing", "failed", "diagnosis"} and step["level"] == "prepare"
            for step in last
        ):
            self.loop_stopped(target, kind, "two attempts in a row made no progress")
            return True
        return False

    def loop_stopped(self, target: str, kind: str, why: str):
        self.store.set(f"stopped:{target}", json.dumps({"kind": kind, "why": why, "at": now()}))

    def rerun_plan(self, claim: dict, task: dict, signal: dict, details: dict, runs: list[dict],
                   why: str, print_: str) -> dict | None:  # fmt: skip
        """A rerun of the failed jobs, prepared at once: once per check and head (a known flaky
        one at most twice a day on the PR)."""
        target, head = claim["target"], details["head"]
        done = [
            step
            for step in self.control.steps(target)
            if step["kind"] == "fix-ci" and (step["plan"] or {}).get("kind") == "rerun"
        ]
        if any(step["h0"] == head for step in done):
            return None
        if sum(1 for step in done if step["day"] == today()) >= 2:
            return None
        plan = {
            "kind": "rerun",
            "why": why,
            "summary": f"CI failed because {why}: run it again.",
            "runs": [
                {"id": run["id"], "name": run["name"], "attempt": run["attempt"]}
                for run in runs
                if run.get("id")
            ],
            "checks": signal["checks"],
        }
        if not plan["runs"]:
            return None
        return self.control.add_step(
            target, "fix-ci", "prepare", state=PREPARED, session_id=task["session_id"],
            task_id=task["id"], holder=claim["holder"], generation=claim["generation"],
            consent=self.control.generation(), h0=head, signal=signal, fingerprint=print_,
            plan=plan, started_at=now(), ended_at=now(),
        )  # fmt: skip


def _hours_ago(hours: float) -> str:
    from .store import hours_ago

    return hours_ago(hours)


# Kept apart for its tests: what an attempt's commit runs through before it is offered.
def token_values() -> list[str]:
    """Values of this process's environment that look like tokens (a commit must not hold one)."""
    return [
        value
        for key, value in os.environ.items()
        if re.search(r"TOKEN|SECRET|KEY|PASSWORD|AUTH", key, re.IGNORECASE)
        and integrity.TOKEN_LIKE.fullmatch(value)
    ]


def prepare_downgrade(store) -> list[str]:
    """--prepare-downgrade, with Chief of Staff not running: a worker left in a step is let go
    of (an earlier version would continue its turn in its normal profile), its attempt
    interrupted. Returns their titles."""
    from .control import Control

    control = Control(store)
    stopped = []
    for session_id, step_id in store.items("step:").items():
        store.delete(f"step:{session_id}")
        if control.exists():
            control.move_step(step_id, RUNNING, state="interrupted", ended_at=now(),
                              why="Stopped before a downgrade")  # fmt: skip
        row = store.find_session(session_id)
        if row and row["status"] in {"working", "queued", "starting"}:
            store.update_session(session_id, status="idle", activity="")
            store.add_message(session_id, "notice", "Stopped preparing it, before a downgrade.",
                              {"shepherd_step": step_id})  # fmt: skip
        if row:
            stopped.append(row["title"])
    return stopped


def git_config_files() -> list[str]:
    """The files of the owner's own git configuration that exist (as git reads them, includes
    too) and its folder under XDG_CONFIG_HOME: what a step's git must read. Blocking: call it in
    a thread."""
    import subprocess

    found: list[str] = []
    with suppress(OSError, subprocess.SubprocessError):
        listed = subprocess.run(
            ["git", "config", "--global", "--includes", "--list", "--show-origin"],  # noqa: S607
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout  # fmt: skip
        for line in listed.splitlines():
            origin = line.split("\t", 1)[0]
            if origin.startswith("file:"):
                found.append(str(Path(origin.removeprefix("file:")).expanduser()))
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git"
    candidates = [*found, str(Path.home() / ".gitconfig"), str(xdg)]
    return [path for path in dict.fromkeys(candidates) if Path(path).exists()]


def step_denied() -> tuple[str, ...]:
    """What a step's commands may neither read nor write: the services' secrets, gh's and git's
    credentials, SSH keys and the keychains."""
    home = Path.home()
    return tuple(dict.fromkeys([
        *service_secrets(),
        *(str(home / name) for name in (".config/gh", ".git-credentials", ".netrc", ".ssh")),
        str(home / "Library" / "Keychains"),
    ]))  # fmt: skip


def git_env(token: str, extra: list[tuple[str, str]]) -> dict[str, str]:
    """git's configuration for Chief of Staff's own fetch and push: the bound account's token in
    an HTTP header for github.com, and no other credential helper (the checkout's, the owner's
    or one a worker wrote never runs)."""
    pairs = [("credential.helper", "")]
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        pairs.append(("http.https://github.com/.extraheader", f"AUTHORIZATION: basic {basic}"))
    pairs += extra
    env = {"GIT_CONFIG_COUNT": str(len(pairs))}
    for index, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


__all__ = ["KINDS", "Shepherd", "git_env", "token_values"]
