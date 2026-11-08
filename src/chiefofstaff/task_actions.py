"""What happens to tasks: reports, cancellation, review, retries and follow-ups, and the
receipts and shareable report of finished work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from . import receipts, threads
from .backend import stop_background
from .envelope import wrap
from .policy import within
from .roles import WORKER
from .sources.github import marked
from .sources.slack import describe
from .store import hours_ago, now
from .tasks import ENDED, OPEN
from .workspaces import (
    Scope,
    base_of,
    changes,
    checkout_git,
    checkout_top,
    diff,
    same_version,
    share_files,
    snapshot,
    tree_names,
    untracked_work,
    version,
    worth_committing,
)

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)
CHECK_BACK_RETRY_MINUTES = 5
# Reads of a followed thread failed in a row before it shows as failing.
FAILING_READS = 3

PROJECT_URL = "https://github.com/evanl666/chiefofstaff"
GITHUB_REMOTE = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


STATUS_WORDS = {
    "queued": "Queued",
    "running": "In progress",
    "waiting": "Waiting for an answer",
    "review": "Done, ready for review",
    "done": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
}


def moved(before: list[dict], now: list[dict]) -> str:
    """Why checkouts' code is not what it was (commit or edits), or ""."""
    current = {checkout["path"]: checkout for checkout in now}
    for recorded in before:
        found = current.get(recorded["path"])
        if not found or not recorded.get("commit") or not found.get("commit"):
            continue
        if found["commit"] != recorded["commit"]:
            return (
                f"Out of date: the code changed since it was checked "
                f"({recorded['commit'][:7]} → {found['commit'][:7]})."
            )
        if not same_version(recorded, found):
            return "Out of date: the code was edited since it was checked."
    return ""


# A recent task's open PRs are read again this often (seconds), and for this long (hours).
PR_REFRESH = 180
PR_HOURS = 72


# A report line that says its worker opened the PR it names (not that someone else did).
OPENED_PR = re.compile(
    r"\b(opened|created|raised|submitted|filed|put up|sent out|pushed and opened)\b"
    r"|\bPR\b[^.\n]{0,20}\b(is )?(up|open(ed)?|created)\b|开了|创建了?|提了|提交了|已开",
    re.IGNORECASE,
)
SOMEONE_ELSE = re.compile(
    r"\b(another|other|someone|their|his|her|by @?\w+)\b|另一个|别的|其他|由.{0,12}负责|他们",
    re.IGNORECASE,
)


def claim_lines(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|(?<=[\u3002\uff01\uff1f])\s*|\n", text or "")
    return [part for part in parts if part.strip()]


def claims_opening(line: str) -> bool:
    return bool(OPENED_PR.search(line)) and not SOMEONE_ELSE.search(line)


def not_before(when: str | None, start: str) -> bool:
    """Whether ISO time `when` (GitHub's "…Z" or ours) is at or after `start`; False when
    either cannot be read."""
    try:
        return datetime.fromisoformat((when or "").replace("Z", "+00:00")) >= (
            datetime.fromisoformat(start.replace("Z", "+00:00"))
        )
    except ValueError:
        return False


# What a PR's standing is: a change in one of these is news on the owner's list.
PR_STANDING = ("state", "draft", "checks", "review", "merge_state", "awaiting", "head")


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


SPLIT_UNDO_MINUTES = 10


def deliverable_title(title: str) -> str:
    """A task's title as the owner gives it: it must say what the task delivers ("111" does
    not). Chinese, Japanese and Korean say it in fewer characters: each counts as a word."""
    from .envelope import clean_title

    title = clean_title(title.strip())
    letters = sum(char.isalpha() for char in title)
    words = len(CJK.findall(title))
    if title == "Task" or (words < 4 and (len(title) < 8 or letters < 3)):
        raise ValueError(
            "Give it a title saying what it delivers, in words (at least 8 characters, or 4 "
            "Chinese, Japanese or Korean ones), e.g. “Add a retry to the upload step”."
        )
    return title


# A character that is a word of its own (Han, kana, Hangul).
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def ran_in(command: str) -> str:
    """Where a command line runs, when it says so (`cd folder && …`); "" otherwise."""
    found = re.match(r"\s*cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*(?:&&|;)", command or "")
    return found.group(1).strip("\"'") if found else ""


class TaskActions:
    """Changes tasks on behalf of sessions and the owner."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.bus = runtime.bus
        self.states = runtime.states
        self.tasks = runtime.tasks
        self.delivery = runtime.delivery
        self.workspaces = runtime.workspaces
        self.waking: set[str] = set()  # Check-backs being delivered now.

    def about(self, task: dict, detail: str) -> str:
        """What a piece of feedback was about, for learning: a Slack-reading worker's text may
        carry Slack content, so its task's title is all that is kept."""
        row = self.store.find_session(task["session_id"]) or {}
        if row.get("slack"):
            return task["title"]
        return f"{task['title']}: {detail[:600]}"

    def receipts(self, task: dict) -> dict | None:
        """Observed commands, changes and PRs for a task, beside its report."""
        commands = self.store.all(
            """SELECT c.command, c.kind, c.exit_code, c.ended_at FROM commands c
            JOIN runs r ON r.id=c.run_id WHERE r.task_id=? ORDER BY c.id""",
            (task["id"],),
        )
        # Claims are saved with the report; what was observed follows from git and gh.
        latest = self.store.one(
            """SELECT evidence, criteria FROM runs WHERE task_id=?
            AND (evidence!='' OR criteria!='') ORDER BY started_at DESC""",
            (task["id"],),
        )
        evidence = json.loads(latest["evidence"]) if latest and latest["evidence"] else None
        if evidence and evidence.get("prs"):
            # Only the PRs it is about: one its report merely mentions (another worker's) is a
            # link in its report, not one of its chips or flags.
            evidence["prs"] = [pr for pr in evidence["prs"] if pr.get("about", True)]
        criteria = self.tasks.criteria(task)
        # A check learned from a mistake that still fails when the worker said done.
        failing = self.runtime.checks.flags(task["id"])
        if not commands and not evidence and not criteria and not failing:
            return None
        claims = json.loads(latest["criteria"]) if latest and latest["criteria"] else []
        found = receipts.summarize(
            commands, evidence, task["result"] or task["progress"], criteria, claims
        )
        found["flags"] += failing
        if found.get("tests"):
            # Tests of its own work: run where it changed code. A task with no checkout ran
            # someone else's tests (a clone it read): shown as where it ran them, not a pass.
            found["tests"]["own"] = bool((evidence or {}).get("checkouts"))
            found["tests"]["cwd"] = ran_in(found["tests"]["command"]) or (
                (self.store.find_session(task["session_id"]) or {}).get("cwd") or ""
            )
        return found

    async def check_fresh(self, session_id: str):
        """Mark the evidence of a worker's finished tasks out of date once its code changed:
        a "verified" must be about the code there is now."""
        row = self.store.find_session(session_id)
        if not row or row["role"] != WORKER:
            return
        live = {checkout["path"]: checkout for checkout in self.workspaces.checkouts(row)}
        for task in self.tasks.for_session(session_id):
            if task["status"] not in {"review", "done"}:
                continue
            run = self.store.one(
                """SELECT id, evidence FROM runs WHERE task_id=? AND evidence!=''
                ORDER BY started_at DESC""",
                (task["id"],),
            )
            evidence = json.loads(run["evidence"]) if run else {}
            if not evidence or evidence.get("stale"):
                continue
            ended = json.loads(self.store.get(f"taskend:{task['id']}") or "{}")
            if ended:
                # Its result's own files, since its result: not a later task's work.
                reason = await self.own_files_changed(task, live, ended)
            else:
                reason = await self.changed_since(evidence, live)
            if reason:
                self.stale(task["id"], reason)

    async def own_files_changed(self, task: dict, live: dict[str, dict], ended: dict) -> str:
        """Why a finished task's own files are not what its result left (or "")."""
        for path, end in ended.items():
            checkout = live.get(path)
            if not checkout or not (scope := self.scope(task, checkout)):
                continue
            with suppress(Exception):
                now_ = await snapshot(checkout)
                if not (now_ and now_["tree"]) or now_["tree"] == end["tree"]:
                    continue
                top = await checkout_top(checkout)
                differ = await tree_names(checkout, top, end["tree"], now_["tree"])
                if hit := sorted(set(differ) & await share_files(checkout, scope)):
                    return (
                        "Out of date: its files changed since it was checked ("
                        + ", ".join(hit[:3])
                        + (f" and {len(hit) - 3} more" if len(hit) > 3 else "")
                        + ")."
                    )
        return ""

    @staticmethod
    async def changed_since(evidence: dict, live: dict[str, dict]) -> str:
        for recorded in evidence.get("checkouts", []):
            checkout = live.get(recorded["path"])
            if not checkout or not recorded.get("commit"):
                continue
            with suppress(Exception):
                current = await version(checkout)
                if current and (
                    reason := moved([recorded], [{**current, "path": recorded["path"]}])
                ):
                    return reason
        return ""

    def stale(self, task_id: str, reason: str):
        """Its latest evidence no longer holds (the code or a decision changed)."""
        run = self.store.one(
            """SELECT id, evidence FROM runs WHERE task_id=? AND evidence!=''
            ORDER BY started_at DESC""",
            (task_id,),
        )
        if not run:
            return
        evidence = json.loads(run["evidence"])
        if evidence.get("stale"):
            return
        evidence["stale"] = reason
        self.store.execute(
            "UPDATE runs SET evidence=? WHERE id=?", (json.dumps(evidence), run["id"])
        )
        self.publish(self.tasks.get(task_id))

    def unstale(self, task_id: str):
        """Its evidence holds again (the change it was out of date for changed nothing)."""
        run = self.store.one(
            """SELECT id, evidence FROM runs WHERE task_id=? AND evidence!=''
            ORDER BY started_at DESC""",
            (task_id,),
        )
        if run and (evidence := json.loads(run["evidence"])).pop("stale", None):
            self.store.execute(
                "UPDATE runs SET evidence=? WHERE id=?", (json.dumps(evidence), run["id"])
            )

    async def collect_evidence(self, task_id: str, keep_stale: bool = False, ended: bool = False):
        """After a task is reported done: its checkouts' changes and its PRs' state.
        `keep_stale`: not after a report (a PR opened), so what was checked did not change.
        `ended`: its result was just reported: what its checkouts hold now is what it made."""
        task = self.tasks.get(task_id)
        row = self.store.find_session(task["session_id"])
        if not row:
            return
        if ended:
            await self.record_end(task)  # Its result: its share ends here.
        checkouts = []
        for checkout in self.workspaces.checkouts(row):
            with suppress(Exception):
                if found := await changes(checkout, self.scope(task, checkout)):
                    checkouts.append(found)
        prs = await self.read_prs(task)
        await self.offer_merge(task, prs)
        run = self.store.one(
            "SELECT id, evidence FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
            (task_id,),
        )
        if run:
            evidence = {"checkouts": checkouts, "prs": prs, "at": now(), "prs_at": time.time()}
            before = json.loads(run["evidence"]) if keep_stale and run["evidence"] else {}
            if reason := before.get("stale") or moved(before.get("checkouts", []), checkouts):
                # The checks were about the code as it was then, not as it is now.
                evidence |= {"checkouts": before["checkouts"], "stale": reason}
            self.store.execute(
                "UPDATE runs SET evidence=? WHERE id=?", (json.dumps(evidence), run["id"])
            )
            self.publish(self.tasks.get(task_id))

    def scope(self, task: dict, checkout: dict) -> Scope | None:
        """This task's share of a checkout its worker used for several tasks (see Scope); None
        for a task from before its start was recorded: the checkout's work, as before."""
        found = self.store.get(f"taskbase:{task['id']}")
        if not found:
            return None
        base = json.loads(found)
        touched = set(json.loads(self.store.get(f"touched:{task['id']}") or "[]"))
        # Its result's snapshot: what it delivered (later tasks' work is theirs). Not while it
        # is open: then its share is up to now.
        ended = json.loads(self.store.get(f"taskend:{task['id']}") or "{}")
        end = ended.get(checkout["path"]) if task["status"] not in OPEN else None
        others: set[str] = set()
        commands = self.shell_text(task) if checkout.get("shared") else ""
        if checkout.get("shared"):
            for row in self.store.all(
                "SELECT key, value FROM kv WHERE key LIKE 'touched:%' AND key!=?",
                (f"touched:{task['id']}",),
            ):
                others.update(json.loads(row["value"] or "[]"))
        return Scope(
            base.get("commits", {}).get(checkout["path"], ""),
            base.get("at", 0),
            touched,
            start=base.get("trees", {}).get(checkout["path"], ""),
            end=end,
            others=others,
            head=base.get("heads", {}).get(checkout["path"], ""),
            large=set(base.get("large", {}).get(checkout["path"], [])),
            commands=commands,
            gaps=[
                (
                    gap["end"][checkout["path"]],
                    gap["start"][checkout["path"]],
                    gap.get("end_commits", {}).get(checkout["path"], ""),
                    gap.get("start_commits", {}).get(checkout["path"], ""),
                    float(gap.get("at") or 0),
                )
                for gap in json.loads(self.store.get(f"taskgaps:{task['id']}") or "[]")
                if checkout["path"] in gap["end"] and checkout["path"] in gap["start"]
            ],
        )

    async def record_restart(self, task: dict):
        """A finished task taken up again (a follow-up, the owner's chat): when other work
        changed its checkouts since its result, that gap is kept (see Scope.gaps)."""
        ended = json.loads(self.store.get(f"taskend:{task['id']}") or "{}")
        row = self.store.find_session(task["session_id"])
        if not ended or not row:
            return
        at = time.time()  # Taken up again now: what it commits from here on is its own.
        result = json.dumps({p: e["tree"] for p, e in sorted(ended.items())})
        checked = json.loads(self.store.get(f"restarted:{task['id']}") or "{}")
        if isinstance(checked, dict) and checked.get("result") == result:
            return  # Checked once for this result: later messages are about its own work.
        gaps = json.loads(self.store.get(f"taskgaps:{task['id']}") or "[]")
        now_trees, now_commits = {}, {}
        for checkout in self.workspaces.checkouts(row):
            with suppress(Exception):
                if (found := await snapshot(checkout)) and found["tree"]:
                    now_trees[checkout["path"]] = found["tree"]
                    now_commits[checkout["path"]] = found["commit"]
        end_trees = {path: end["tree"] for path, end in ended.items()}
        end_commits = {path: end["commit"] for path, end in ended.items()}
        if any(now_trees.get(path, tree) != tree for path, tree in end_trees.items()):
            gap = {"end": end_trees, "start": now_trees, "end_commits": end_commits,
                   "start_commits": now_commits, "at": at}  # fmt: skip
            gaps = [*gaps, gap][-5:]
            self.store.set(f"taskgaps:{task['id']}", json.dumps(gaps))
        self.store.set(f"restarted:{task['id']}", json.dumps({"result": result, "at": at}))

    def shell_text(self, task: dict, since: str = "") -> str:
        """The shell commands its worker ran for this task (since a time)."""
        rows = self.store.all(
            "SELECT c.command FROM commands c JOIN runs r ON r.id=c.run_id WHERE r.task_id=? "
            "AND c.started_at>=? ORDER BY c.id",
            (task["id"], since),
        )
        return "\n".join(row["command"] for row in rows)

    async def record_chat_base(self, task: dict):
        """Its checkouts when the owner asks about a finished task in chat: what changes from
        then on, by this task, is a new result (see changed_since_result)."""
        row = self.store.find_session(task["session_id"])
        if not row:
            return
        chat = json.loads(self.store.get(f"chatbase:{task['id']}") or "{}")
        results = json.loads(self.store.get(f"taskends:{task['id']}") or "[]")
        if chat.get("at") and (not results or chat["at"] > results[-1]["at"]):
            # Asked again before a new result: what it changed since the first ask still
            # counts (it is not the base of the next one).
            await self.record_restart(task)
            return
        trees = {}
        for checkout in self.workspaces.checkouts(row):
            with suppress(Exception):
                if (found := await snapshot(checkout)) and found["tree"]:
                    trees[checkout["path"]] = found
        self.store.set(f"chatbase:{task['id']}", json.dumps({"at": now(), "trees": trees}))
        await self.record_restart(task)

    async def changed_since_result(self, task: dict) -> bool:
        """Whether this task changed its checkouts since the owner asked about it in chat (else
        since its result): then what it reports is a new result. Not another task's changes
        since, nor the owner's, nor what running code left behind."""
        chat = json.loads(self.store.get(f"chatbase:{task['id']}") or "{}")
        ended = json.loads(self.store.get(f"taskend:{task['id']}") or "{}")
        row = self.store.find_session(task["session_id"])
        if not row or not (chat.get("trees") or ended):
            return False
        touched = set(json.loads(self.store.get(f"touched:{task['id']}") or "[]"))
        for checkout in self.workspaces.checkouts(row):
            before = (chat.get("trees") or {}).get(checkout["path"]) or ended.get(checkout["path"])
            with suppress(Exception):
                now_ = await snapshot(checkout)
                if not (before and now_ and before["tree"] and now_["tree"]):
                    continue
                if before["tree"] == now_["tree"]:
                    continue
                top = await checkout_top(checkout)
                differ = await tree_names(checkout, top, before["tree"], now_["tree"])
                # Not what running code left behind (new `__pycache__/`, a `.coverage`).
                names = [n for n, status in differ.items() if status != "A" or untracked_work([n])]
                if checkout.get("shared"):
                    # In a folder of the owner's: only what its edits wrote or its commands named.
                    commands = self.shell_text(task, chat.get("at", ""))
                    scope = Scope(touched=touched, commands=commands)
                    names = [n for n in names if scope.wrote(top, n) or scope.named(top, n)]
                if names:
                    return True
        return False

    async def record_end(self, task: dict):
        """Its checkouts as its result left them: its share ends there (a later task on the
        same branch or folder does not add to it)."""
        row = self.store.find_session(task["session_id"])
        if not row or not self.store.get(f"taskbase:{task['id']}"):
            return
        ended = {}
        for checkout in self.workspaces.checkouts(row):
            with suppress(Exception):
                if (found := await snapshot(checkout)) and found["tree"]:
                    ended[checkout["path"]] = found
        if ended:
            self.store.set(f"taskend:{task['id']}", json.dumps(ended))
            # Each result's end, kept: a split gives each result its own share.
            history = json.loads(self.store.get(f"taskends:{task['id']}") or "[]")
            history = [*history, {"at": now(), "ended": ended}][-6:]
            self.store.set(f"taskends:{task['id']}", json.dumps(history))

    def split_changes(self, task: dict, made: dict, latest: dict, first_at: str) -> dict:
        """A split's checkouts: the new task's share runs from the end of the result the task
        keeps to the end of the one that moves (with the commands run for it); the task's ends
        where its kept result did. What undoing it puts back."""
        history = json.loads(self.store.get(f"taskends:{task['id']}") or "[]")
        kept = [h for h in history if h["at"] < latest["created_at"]]
        moved_end = [h for h in history if h["at"] >= latest["created_at"]]
        before = json.loads(self.store.get(f"taskend:{task['id']}") or "null")
        undo = {"ended": before}
        if kept and (moved_end or before):
            start, end = kept[-1], (moved_end[-1]["ended"] if moved_end else before)
            at = datetime.fromisoformat(start["at"]).timestamp()
            ended = start["ended"]
            self.store.set(
                f"taskbase:{made['id']}",
                json.dumps(
                    {"at": at, "commits": {p: e["commit"] for p, e in ended.items()},
                     "trees": {p: e["tree"] for p, e in ended.items()},
                     "heads": {p: e["commit"] for p, e in ended.items()},
                     "large": {p: e.get("large", []) for p, e in ended.items()}}
                ),
            )  # fmt: skip
            self.store.set(f"taskend:{made['id']}", json.dumps(end))
            self.store.set(f"taskend:{task['id']}", json.dumps(ended))
        run = self.store.one(
            "SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (made["id"],)
        )
        if run:
            # Its tests and commands: those run since it was asked for.
            moved = self.store.all(
                "SELECT c.id FROM commands c JOIN runs r ON r.id=c.run_id WHERE r.task_id=? "
                "AND c.started_at>=?",
                (task["id"], first_at),
            )
            ids = [row["id"] for row in moved]
            for identifier in ids:
                self.store.execute(
                    "UPDATE commands SET run_id=? WHERE id=?", (run["id"], identifier)
                )
            undo |= {"commands": ids}
        touched = self.store.get(f"touched:{task['id']}")
        if touched:
            self.store.set(f"touched:{made['id']}", touched)
        self.runtime.background(self.collect_evidence(made["id"]))
        self.runtime.background(self.collect_evidence(task["id"]))
        return undo

    async def record_base(self, session_id: str, task_ids: list[str], at: float):
        """Where its checkouts are when a worker takes a task up: that task's changes are what
        comes after (an earlier task's commits on the same branch are not its own)."""
        row = self.store.find_session(session_id)
        task_ids = [task_id for task_id in task_ids if not self.store.get(f"taskbase:{task_id}")]
        if not row or not task_ids:
            return
        commits, trees, heads, large = {}, {}, {}, {}
        first = self.tasks.for_session(session_id)[:1]
        # Its first task's work starts where its branch did (in a folder of the owner's, where
        # the folder is now); a later one's where it is now: its commit and all its files.
        later = not (first and all(task_id == first[0]["id"] for task_id in task_ids))
        for checkout in self.workspaces.checkouts(row):
            if not (later or checkout.get("shared")):
                continue
            with suppress(Exception):
                if found := await snapshot(checkout):
                    if found["tree"]:
                        trees[checkout["path"]] = found["tree"]
                    heads[checkout["path"]] = found["commit"]
                    if found.get("large"):
                        large[checkout["path"]] = found["large"]
                    if later:
                        commits[checkout["path"]] = found["commit"]
        for task_id in task_ids:
            if not self.store.get(f"taskbase:{task_id}"):
                self.store.set(
                    f"taskbase:{task_id}",
                    json.dumps(
                        {
                            "at": at,
                            "commits": commits,
                            "trees": trees,
                            "heads": heads,
                            "large": large,
                        }
                    ),
                )

    def needs_acceptance(self, task: dict) -> bool:
        """Whether a finished result waits for the owner to accept it, as Home decides (board.js
        needsAcceptance): it changed code, opened a PR still open, has criteria not verified,
        a flag, or the Router said the owner wants to see it. Else it only informs."""
        if task["status"] != "review":
            return False
        shown = self.view(task)
        if shown.get("review_required"):
            return True
        receipts = shown.get("receipts") or {}
        changed = receipts.get("changed") or {}
        if changed.get("files") or changed.get("commits") or changed.get("uncommitted"):
            return True
        if any(
            pr.get("own") and pr.get("state") not in {"MERGED", "CLOSED"}
            for pr in receipts.get("prs") or []
        ):
            return True
        if receipts.get("criteria") and receipts.get("verdict") != "verified":
            return True
        return bool(receipts.get("flags"))

    def status_words(self, task: dict) -> str:
        """A task's status for the Router: a finished one says whether it waits for the owner."""
        if task["status"] == "review":
            return "review, to accept" if self.needs_acceptance(task) else "review, report"
        if task["status"] == "waiting" and self.store.get(f"stopped:{task['id']}"):
            return "waiting, stopped by the owner"
        return task["status"]

    def resume_stopped(self, session_id: str):
        """Its worker is at work again on a task it was stopped on: one it was told to go on
        with since (in its chat, by the Router, a nudge: an instruction naming it, see
        `Runtime.took_up`) runs again. Work on anything else (another task, PR news) leaves
        a stopped task stopped."""
        for task in self.tasks.for_session(session_id):
            if (
                task["status"] == "waiting"
                and self.store.get(f"stopped:{task['id']}")
                and self.store.get(f"goingon:{task['id']}")
            ):
                self.publish(self.tasks.move(task["id"], "running", progress=""))

    def touched(self, task_id: str, paths: list[str]):
        """Files a worker's edits wrote for a task (in a folder of the owner's, only those are
        its changes)."""
        if not task_id or not paths:
            return
        known = json.loads(self.store.get(f"touched:{task_id}") or "[]")
        added = [os.path.realpath(path) for path in paths if path]
        merged = list(dict.fromkeys([*known, *added]))[-500:]
        if merged != known:
            self.store.set(f"touched:{task_id}", json.dumps(merged))

    async def read_prs(self, task: dict) -> list[dict]:
        """The task's PRs as GitHub has them now: open or merged, checks, review."""
        prs = []
        for repo, number in threads.PR_URL.findall(" ".join(self.pr_urls(task)))[:5]:
            try:
                view = json.loads(
                    await self.runtime.github.gh(
                        "pr",
                        "view",
                        f"https://github.com/{repo}/pull/{number}",
                        "--json",
                        "number,url,state,statusCheckRollup,reviewDecision,title,isDraft,"
                        "mergeStateStatus,headRefOid,headRefName,createdAt,author",
                    )
                )
            except Exception:
                logger.info("Could not read %s#%s for receipts.", repo, number, exc_info=True)
                continue
            rollup = view.get("statusCheckRollup") or []
            prs.append(
                {
                    "label": f"{repo}#{number}",
                    "url": view.get("url") or f"https://github.com/{repo}/pull/{number}",
                    "state": view.get("state", ""),
                    "title": (view.get("title") or "")[:200],
                    "draft": bool(view.get("isDraft")),
                    "checks": receipts.checks_state(rollup),
                    "failing": receipts.failing_checks(rollup)[:5],
                    "awaiting": receipts.awaiting_approval(rollup)[:3],
                    "checks_started": receipts.checks_started(rollup),
                    "review": view.get("reviewDecision") or "",
                    "merge_state": view.get("mergeStateStatus") or "",
                    "head": view.get("headRefOid") or "",
                    "branch": view.get("headRefName") or "",
                    "created": view.get("createdAt") or "",
                    "author": ((view.get("author") or {}).get("login") or "").lower(),
                    "at": now(),
                }
            )
            if view.get("title"):
                self.runtime.remember_pr_title(f"{repo}#{number}", view["title"])
        own, about = await self.pr_roles(task, prs)
        reviewed = self.reviewed_prs(task["session_id"])
        # When each last changed (its state, checks, review, mergeability), not when it was
        # read: "2h ago" on the owner's list says how long it has stood like this.
        before = {pr["label"].lower(): pr for pr in self.previous_prs(task["id"])}
        for pr in prs:
            pr["own"] = pr["label"].lower() in own
            pr["about"] = pr["label"].lower() in about
            # Reviewed by its worker (others' work it was asked to look at): a main chip too.
            pr["reviewed"] = pr["label"].lower() in reviewed and not pr["own"]
            old = before.get(pr["label"].lower()) or {}
            same = old and all(old.get(key) == pr.get(key) for key in PR_STANDING)
            pr["changed_at"] = (
                (old.get("changed_at") or old.get("at") or pr["at"]) if same else pr["at"]
            )
        return prs

    def previous_prs(self, task_id: str) -> list[dict]:
        found = self.store.one(
            "SELECT evidence FROM runs WHERE task_id=? AND evidence!='' ORDER BY started_at DESC",
            (task_id,),
        )
        evidence = json.loads(found["evidence"]) if found else {}
        return evidence.get("prs") or []

    def set_check_back(self, task: dict, at: str, what: str, url: str):
        """The worker is woken at `at` (ISO) to check `what`; one per task, the latest."""
        value = {"at": at, "what": what, "url": url, "session_id": task["session_id"]}
        self.store.set(f"checkback:{task['id']}", json.dumps(value))
        self.publish(self.tasks.get(task["id"]))

    def check_back(self, task: dict) -> dict | None:
        found = self.store.get(f"checkback:{task['id']}")
        return json.loads(found) if found and task["status"] in {"running", "waiting"} else None

    async def wake_check_backs(self, moment: datetime | None = None):
        """Workers whose check-back time came are woken with what they wanted to check. The
        check-back is kept until its worker has the message: one that could not be delivered
        (its worker could not be reopened, Copilot failed) is tried again a few minutes later,
        and says why on the task's card."""
        moment = moment or datetime.now(UTC)
        for task_id, raw in self.store.items("checkback:").items():
            found = json.loads(raw)
            if datetime.fromisoformat(found["at"]) > moment or task_id in self.waking:
                continue
            task = self.tasks.find(task_id)
            if not task or task["status"] not in {"running", "waiting"}:
                self.store.delete(f"checkback:{task_id}")
                continue
            row = self.store.find_session(task["session_id"])
            if not row:
                self.store.delete(f"checkback:{task_id}")
                continue
            self.waking.add(task_id)
            try:
                if row["closed_at"]:
                    await self.runtime.workers.reopen(row["id"])
                link = f" ({found['url']})" if found.get("url") else ""
                text = (
                    f"Time to check {found['what']}{link} for task {task_id} “{task['title']}”. "
                    "Check it now. Done: report. Not yet: check_back again, or report what "
                    "blocks it."
                )
                await self.delivery.deliver(
                    row["id"],
                    wrap("system", "check-back", text, task=task_id),
                    kind="router",
                    display=f"Check back: {found['what']}{link}",
                    meta={"task_id": task_id, "check_back": True},
                )
            except Exception as exc:
                logger.warning("Could not wake %s to check back.", task_id, exc_info=True)
                attempts = int(found.get("attempts", 0)) + 1
                retry = moment + timedelta(minutes=CHECK_BACK_RETRY_MINUTES)
                if self.store.get(f"checkback:{task_id}") == raw:  # Not set again meanwhile.
                    self.store.set(
                        f"checkback:{task_id}",
                        json.dumps(
                            found
                            | {
                                "at": retry.isoformat(),
                                "attempts": attempts,
                                "error": describe(exc)[:300],
                                "due": found.get("due") or found["at"],
                            }
                        ),
                    )
            else:
                if self.store.get(f"checkback:{task_id}") == raw:  # Not set again meanwhile.
                    self.store.delete(f"checkback:{task_id}")
            finally:
                self.waking.discard(task_id)
            self.publish(self.tasks.get(task_id))

    async def learn_pr_titles(self, text: str):
        """The titles of the PRs a text names, read from GitHub once (a few at a time)."""
        learned = False
        for key in threads.pull_requests(text)[:5]:
            label = key.removeprefix("github:")
            if self.store.get(f"prtitle:{label}"):
                continue
            repo, number = label.rsplit("#", 1)
            try:
                pull = await self.runtime.github.pull(repo, int(number))
            except Exception:
                logger.info("Could not read the title of %s.", label, exc_info=True)
                continue
            if pull.get("title"):
                self.runtime.remember_pr_title(label, pull["title"])
                learned = True
        if learned:
            self.runtime.publish_state()

    def reviewed_prs(self, session_id: str) -> set[str]:
        """The PRs a worker drafted a review of ("owner/repo#n"): others' work, not its own. Not
        those it only replied on: it answers comments on its own PRs too."""
        return {
            f"{target['repo']}#{target['number']}".lower()
            for row in self.store.all(
                "SELECT target FROM drafts WHERE session_id=? AND kind='github'", (session_id,)
            )
            if (target := json.loads(row["target"])).get("repo")
            and target.get("number")
            and not (target.get("in_reply_to") or target.get("reply_to_comment"))
        }

    def first_reporter(self, label: str) -> str:
        """The task ("session_id|task_id") whose report named this PR ("owner/repo#n") first:
        the work that brought it up. Several workers push as the owner, so a PR another worker
        brought up is that one's. Read once from the reports, then kept; "" when none did."""
        key = f"prfirst:{label.lower()}"
        if found := self.store.get(key):
            return found
        repo, number = label.lower().rsplit("#", 1)
        named = re.compile(rf"github\.com/{re.escape(repo)}/pull/{number}(?!\d)", re.IGNORECASE)
        for row in self.store.all(
            "SELECT session_id, text, meta FROM messages WHERE kind='report' AND text LIKE ? "
            "ORDER BY id",
            (f"%/pull/{number}%",),
        ):
            if named.search(row["text"]):
                task_id = json.loads(row["meta"] or "{}").get("task_id") or ""
                found = f"{row['session_id']}|{task_id}"
                self.store.set(key, found)
                return found
        return ""

    def opener(self, label: str) -> str:
        """The task that opened this PR ("owner/repo#n") by record (Create PR, `gh pr create`,
        a create-PR tool), "" when none did."""
        label = label.lower()
        for task_id in self.store.items("prs:"):
            if label in self.opened_prs(task_id):
                return task_id
        return ""

    def claimed_by(self, label: str) -> str:
        """The task whose report first says it opened this PR ("Opened <url>", "PR 已开 <url>"),
        "" when none did. A report that only names it (to read it, about someone else's) is
        no claim. Read once from the reports, then kept."""
        key = f"prclaim:{label.lower()}"
        if (found := self.store.get(key)) is not None:
            return found
        repo, number = label.lower().rsplit("#", 1)
        named = re.compile(rf"github\.com/{re.escape(repo)}/pull/{number}(?!\d)", re.IGNORECASE)
        found = ""
        for row in self.store.all(
            "SELECT text, meta FROM messages WHERE kind='report' AND text LIKE ? ORDER BY id",
            (f"%/pull/{number}%",),
        ):
            task_id = json.loads(row["meta"] or "{}").get("task_id") or ""
            if task_id and any(
                named.search(line) and claims_opening(line) for line in claim_lines(row["text"])
            ):
                found = task_id
                break
        if found:
            self.store.set(key, found)
        return found

    def creator(self, label: str) -> str:
        """The task that made this PR: the one that opened it by record, else the first whose
        report says it opened it. A mention is no proof: the first to name a PR may only be
        reading it, or naming another worker's."""
        return self.opener(label) or self.claimed_by(label)

    async def pr_roles(self, task: dict, prs: list[dict]) -> tuple[set[str], set[str]]:
        """Which of these PRs ("owner/repo#n", lowercase) are the task's own work, and which it
        is about. Own: opened by it (by record), from its worker's branch, or opened by the
        owner's account since the task began when its report says it opened it; never when
        another task opened it. About: those, the PRs its worker reviewed or was given to
        follow, those its title or instructions name, and those it brought up first. A PR its
        report only mentions after another task did is neither."""
        from .tools import names_pr

        opened = self.opened_prs(task["id"])
        row = self.store.find_session(task["session_id"])
        branches = {c.get("branch") or "" for c in self.workspaces.checkouts(row)} if row else set()
        branches = {branch for branch in branches | {(row or {}).get("branch") or ""} if branch}
        try:
            mine = await self.runtime.github.logins()
        except Exception:
            mine = set()
        reviewed = self.reviewed_prs(task["session_id"])
        asked = f"{task.get('title') or ''}\n{task.get('goal') or ''}"
        own: set[str] = set()
        about: set[str] = set()
        for pr in prs:
            label = pr["label"].lower()
            repo, number = label.rsplit("#", 1)
            first = self.first_reporter(label)
            # Several workers push as the owner: only the task that brought it up first.
            brought = first == f"{task['session_id']}|{task['id']}" or (
                first == f"{task['session_id']}|" and not task["id"]
            )
            maker = self.opener(label)
            # What it opened is its own, whatever else it drafted on it.
            if label in opened or (
                label not in reviewed
                and (
                    not maker
                    and (
                        (pr.get("branch") and pr.get("branch") in branches)
                        or (
                            pr.get("author") in mine
                            and not_before(pr.get("created"), task["created_at"])
                            and self.claimed_by(label) == task["id"]
                        )
                    )
                )
            ):
                own.add(label)
            if (
                label in own
                or label in reviewed
                or self.store.get(f"given:{task['session_id']}:github:{label}")
                or names_pr(asked, repo, int(number))
                or (first and brought)
            ):
                about.add(label)
        return own, about

    async def own_prs(self, task: dict, prs: list[dict]) -> set[str]:
        return (await self.pr_roles(task, prs))[0]

    async def offer_merge(self, task: dict, prs: list[dict]):
        """A PR the task delivered for the owner that can be merged now (open, not a draft, CI
        passing, nothing blocking on GitHub) gets a card: merge it, or not now. Once per head
        commit; the card goes when the PR changes, is merged or closed, and comes back once it
        is ready again."""
        try:
            mine = await self.runtime.github.logins()
        except Exception:
            return
        cards = [draft for draft in self.store.drafts() if draft["kind"] == "merge"]
        own = {pr["label"].lower() for pr in prs if pr.get("own")}
        for pr in prs:
            label = pr["label"].lower()
            mergeable = (
                pr.get("state") == "OPEN"
                and not pr.get("draft")
                and pr.get("checks") in {"passing", "none"}
                and pr.get("merge_state") in {"CLEAN", "HAS_HOOKS"}
                and pr.get("review") != "CHANGES_REQUESTED"
                and pr.get("head")
            )
            for card in cards:
                target = card["target"]
                if (
                    target["label"].lower() == label
                    and target.get("task_id") == task["id"]
                    and label not in own
                ):
                    # Offered on a task that only names it (another task made it).
                    self.store.delete(f"mergeoffer:{label}@{target['head']}")
                    closed = self.store.update_draft(
                        card["id"], status="rejected", outcome="Not this task's PR.",
                        resolved_at=now(),
                    )  # fmt: skip
                    self.bus.publish("draft", closed)
                    continue
                # GitHub works out whether it can merge (after its base moved, say): the card
                # stays; Merge is only ever of the head it shows.
                recomputing = (
                    pr.get("state") == "OPEN"
                    and pr.get("merge_state") == "UNKNOWN"
                    and target["head"] == pr.get("head")
                )
                if (
                    target["label"].lower() == label
                    and not recomputing
                    and not (mergeable and target["head"] == pr["head"])
                ):
                    gone = pr.get("state") in {"MERGED", "CLOSED"}
                    why = (
                        f"The PR is {pr['state'].lower()}."
                        if gone
                        else "The PR changed since: offered again once it is ready."
                    )
                    if not gone:
                        # Not ready for now (a check run again, changes asked for and then
                        # dismissed): offered again when it is, even at the same head.
                        self.store.delete(f"mergeoffer:{label}@{target['head']}")
                    closed = self.store.update_draft(
                        card["id"], status="rejected", outcome=why, resolved_at=now()
                    )
                    self.bus.publish("draft", closed)
            ready = mergeable and label in own and pr.get("author") in mine
            key = f"mergeoffer:{label}@{pr.get('head')}"
            if not ready or self.store.get(key):
                continue
            self.store.set(key, "1")
            repo, number = pr["label"].rsplit("#", 1)
            target = {
                "label": pr["label"],
                "repo": repo.lower(),
                "number": int(number),
                "url": pr["url"],
                "title": pr.get("title", ""),
                "head": pr["head"],
                "task_id": task["id"],
                "review": pr.get("review", ""),
                "checks": pr.get("checks", ""),
            }
            card = self.store.create_draft(task["session_id"], "merge", target, pr.get("title", ""))
            self.bus.publish("draft", card)
            alerted = f"mergealert:{label}@{pr['head']}"
            if not self.store.get(alerted):  # Offered again at the same commit: said once.
                self.store.set(alerted, "1")
                self.runtime.alert(
                    f"{pr['label']} is ready to merge", session_id=task["session_id"],
                    needs_action=True, title="Ready to merge",
                )  # fmt: skip

    async def refresh_prs(self, task_id: str) -> bool:
        """The PR chips of a task show what is true now, not what was when it reported: a PR
        whose failing checks pass on a later run turns green. True when anything changed."""
        run = self.store.one(
            """SELECT id, evidence FROM runs WHERE task_id=? AND evidence!=''
            ORDER BY started_at DESC LIMIT 1""",
            (task_id,),
        )
        if not run:
            return False
        evidence = json.loads(run["evidence"])
        if not evidence.get("prs"):
            return False
        prs = await self.read_prs(self.tasks.get(task_id))
        if not prs:
            return False
        task = self.tasks.get(task_id)
        await self.offer_merge(task, prs)
        shown = [{k: v for k, v in pr.items() if k != "at"} for pr in evidence["prs"]]
        changed = shown != [{k: v for k, v in pr.items() if k != "at"} for pr in prs]
        evidence |= {"prs": prs, "prs_at": time.time()}
        # Saved first: whether its other PRs are still open is read from what is true now.
        self.store.execute(
            "UPDATE runs SET evidence=? WHERE id=?", (json.dumps(evidence), run["id"])
        )
        # Its own PR merged: what it delivers is in. Not a PR its report only mentions.
        merged = [pr["label"] for pr in prs if pr.get("state") == "MERGED" and pr.get("own")]
        if merged and task["status"] == "review":
            for label in merged:
                self.pr_merged(task["session_id"], f"github:{label.lower()}", task["id"])
        if changed:
            self.publish(self.tasks.get(task_id))
        return changed

    async def refresh_open_prs(self, pull: str = "", every: float = PR_REFRESH):
        """Every few minutes (and at once for `pull`, "owner/repo#n", when something happened
        on it): the PRs of recent tasks that are not merged or closed yet."""
        for task in self.tasks.listed(include_ended_since=hours_ago(PR_HOURS)):
            run = self.store.one(
                """SELECT evidence FROM runs WHERE task_id=? AND evidence!=''
                ORDER BY started_at DESC LIMIT 1""",
                (task["id"],),
            )
            evidence = json.loads(run["evidence"]) if run else {}
            prs = evidence.get("prs") or []
            if not prs or all(pr.get("state") in {"MERGED", "CLOSED"} for pr in prs):
                continue
            named = pull and any(pr.get("label", "").lower() == pull.lower() for pr in prs)
            if not named and time.time() - evidence.get("prs_at", 0) < every:
                continue
            try:
                await self.refresh_prs(task["id"])
            except Exception:
                logger.info("Could not refresh the PRs of %s.", task["id"], exc_info=True)

    def record_opened(self, task_id: str, urls: list[str]):
        """PRs this task's worker opened (Create PR, or `gh pr create` in its shell): its own
        work, which a merge settles."""
        known = json.loads(self.store.get(f"prs:{task_id}") or "[]")
        self.store.set(f"prs:{task_id}", json.dumps(list(dict.fromkeys(known + urls))))

    def opened_prs(self, task_id: str) -> set[str]:
        """Those PRs, as "owner/repo#n" (lowercase)."""
        opened = json.loads(self.store.get(f"prs:{task_id}") or "[]")
        return {key.removeprefix("github:") for key in threads.pull_requests(" ".join(opened))}

    def owns_pr(self, task: dict, label: str) -> bool:
        """Whether a PR ("owner/repo#n") is this task's own work by what is known without
        GitHub: it opened it, or said it did and no other task opened it."""
        return self.creator(label) == task["id"]

    def pr_urls(self, task: dict) -> list[str]:
        """The task's PRs: those opened with Create PR, and those any of its reports linked to."""
        opened = json.loads(self.store.get(f"prs:{task['id']}") or "[]")
        cited = json.loads(self.store.get(f"cited:{task['id']}") or "[]") + [
            f"https://github.com/{repo}/pull/{number}"
            for repo, number in threads.PR_URL.findall(task["result"] or "")
        ]
        return list(dict.fromkeys(opened + cited))

    def remember_prs(self, task_id: str, report: str):
        """PRs a done report links to stay the task's when a later report does not repeat them
        (a merged PR then still settles it)."""
        cited = json.loads(self.store.get(f"cited:{task_id}") or "[]")
        found = [
            f"https://github.com/{repo}/pull/{n}" for repo, n in threads.PR_URL.findall(report)
        ]
        if found:
            self.store.set(f"cited:{task_id}", json.dumps(list(dict.fromkeys(cited + found))))

    def pr_merged(self, session_id: str, thread: str, task_id: str = ""):
        """A worker's PR was merged: the task that delivers it is done (`task_id`, when known
        already). Not a task whose report only mentions it."""
        label = thread.removeprefix("github:")
        for task in self.tasks.for_session(session_id):
            if task["status"] != "review" or (task_id and task["id"] != task_id):
                continue
            if not task_id and not self.owns_pr(task, label):
                continue
            if thread in threads.pull_requests(" ".join(self.pr_urls(task))):
                # Its other PRs still open: it is done when the last of its own is merged.
                still = [
                    pr["label"]
                    for pr in self.previous_prs(task["id"])
                    if pr.get("own")
                    and pr.get("state") == "OPEN"
                    and pr["label"].lower() != label.lower()
                ]
                if still:
                    continue
                self.tasks.move(task["id"], "done")
                self.runtime.background(self.runtime.workers.pump())

    async def diff(self, task_id: str) -> list[dict]:
        """What the task's worker changed, per checkout, as unified diffs."""
        task = self.tasks.get(task_id)
        row = self.store.session(task["session_id"])
        found = []
        async with self.states[row["id"]].checkouts:  # Not while its worktrees are removed.
            for checkout in self.workspaces.checkouts(row):
                try:
                    found.append(await diff(checkout, self.scope(task, checkout)))
                except ValueError as exc:
                    logger.info("No diff for %s: %s", checkout["path"], exc)
        return found

    async def create_pr(self, task_id: str) -> dict:
        """Push the task's branches and open a PR from each, or find the one already open."""
        task = self.tasks.get(task_id)
        if task["status"] not in {"review", "done"}:
            raise ValueError(f"The task is {task['status']}: open a PR once it is finished.")
        row = self.store.session(task["session_id"])
        gh = self.runtime.github.gh
        title, body = self.pr_text(task)
        opened: list[str] = []
        async with self.states[row["id"]].checkouts:
            plans = []
            for checkout in self.workspaces.checkouts(row):
                if not (checkout.get("repo") and checkout.get("branch")):
                    continue  # A shared project folder has no branch of its own.
                if plan := await self.pr_plan(checkout):
                    plans.append((checkout, *plan))
            if not plans:
                raise ValueError("Nothing to open a PR from: the worker committed nothing.")
            for checkout, repo, slug, branch in plans:
                await checkout_git(
                    checkout, repo, "push", "--quiet", "origin",
                    f"refs/heads/{branch}:refs/heads/{branch}", limit=180,
                )  # fmt: skip
                listed = await gh(
                    "pr", "list", "--repo", slug, "--head", branch, "--state", "open",
                    "--json", "url", "--limit", "1",
                )  # fmt: skip
                if existing := json.loads(listed or "[]"):
                    opened.append(existing[0]["url"])
                    continue
                created = await gh(
                    "pr", "create", "--repo", slug, "--head", branch,
                    "--title", title, "--body", body,
                )  # fmt: skip
                if not (found := threads.PR_URL.search(created)):
                    raise ValueError(f"GitHub did not say which PR it opened: {created[:300]}")
                opened.append(found.group(0))
        self.record_opened(task_id, opened)
        if not row["closed_at"]:
            # Its reviews, comments and CI come to the worker; merged, the task is done.
            followed = self.runtime.follow(
                row["id"], threads.pull_requests(" ".join(opened)), task_id=task_id
            )
            note = (
                f"The owner opened {', '.join(opened)} from your branch. You follow it: its "
                "reviews, comments and CI results come to you."
            )
            self.runtime.add_message(
                row["id"],
                "notice",
                f"PR opened: {', '.join(opened)}",
                {"prompt": wrap("system", "context", note), "deferred": True} if followed else {},
            )
        await self.collect_evidence(task_id, keep_stale=True)
        return self.tasks.get(task_id)

    def pr_text(self, task: dict) -> tuple[str, str]:
        """A PR's title and description for a task's work: what changed, as its worker
        reported it, and its checks. Not its instructions (written for the worker: "do not
        open a PR"), its credits or the project's name in its title."""
        title = re.sub(r"\s*\u00b7\s*[\w.-]+$", "", task["title"]).strip() or task["title"]
        found = self.view(task)["receipts"] or {}
        lines = [(task["result"] or task["progress"] or "").strip(), ""]
        checks = []
        for label, key in (("Tests", "tests"), ("Checks", "checks"), ("Build", "build")):
            if (
                (item := found.get(key))
                and item.get("passed") is not None
                and item.get("own", True)
            ):
                outcome = "passed" if item["passed"] else f"failed (exit {item['exit_code']})"
                checks.append(f"- {label}: `{item['command']}` {outcome}")
        for item in found.get("criteria") or []:
            if item["state"] == "verified":
                checks.append(
                    f"- {item['criterion']}" + (f": `{item['command']}`" if item["command"] else "")
                )
        if checks:
            lines += ["**Checked**", "", *checks, ""]
        return title[:250], marked("\n".join(lines).strip())

    async def pr_plan(self, checkout: dict) -> tuple[str, str, str] | None:
        """(repository, GitHub owner/name, branch) to open a PR from, or None: no commits."""
        repo, branch = checkout["repo"], checkout["branch"]
        name = Path(repo).name
        tree = checkout.get("worktree") or checkout["path"]
        live = await asyncio.to_thread(os.path.isdir, tree)
        status = await checkout_git(checkout, tree, "status", "--porcelain") if live else ""
        if worth_committing(status):
            raise ValueError(
                f"{name} has uncommitted changes. Follow up and ask the worker to commit "
                "them, then create the PR."
            )
        if live:
            # The branch it is on now (one the owner named, say), not the one it was given.
            with suppress(ValueError):
                current = (
                    await checkout_git(checkout, tree, "rev-parse", "--abbrev-ref", "HEAD")
                ).strip()
                default = base_of(checkout).removeprefix("origin/")
                if current and current not in {"HEAD", default, "main", "master"}:
                    branch = current
        commits = await checkout_git(
            checkout, repo, "rev-list", "--count", f"{base_of(checkout)}..refs/heads/{branch}"
        )
        if not int(commits.strip() or 0):
            return None
        try:
            remote = (
                await checkout_git(checkout, repo, "config", "--get", "remote.origin.url")
            ).strip()
        except ValueError:
            remote = ""
        match = GITHUB_REMOTE.search(remote)
        if not match:
            raise ValueError(f"{name} has no GitHub origin to open a PR on.")
        return repo, match.group(1), branch

    def report_markdown(self, task_id: str) -> str:
        """A task as markdown to paste into a PR description or a message."""
        task = self.view(self.tasks.get(task_id))
        found = task["receipts"] or {}
        lines = [f"## {task['title']}", ""]
        facts: list[str] = [STATUS_WORDS.get(task["status"]) or task["status"]]
        if task["runs"] > 1:
            facts.append(f"{task['runs']} runs")
        if task["credits"] >= 0.05:
            facts.append(f"{task['credits']:.1f} Copilot credits")
        lines += [" · ".join(facts), ""]
        if task["goal"]:
            lines += ["**Goal**", "", task["goal"].strip(), ""]
        if text := task["result"] or task["progress"]:
            lines += ["**Result** (as reported)", "", text.strip(), ""]
        observed = []
        for label, key in (("Tests", "tests"), ("Checks", "checks"), ("Build", "build")):
            if item := found.get(key):
                mark = (
                    "running"
                    if item["passed"] is None
                    else "passed"
                    if item["passed"]
                    else f"failed (exit {item['exit_code']})"
                )
                observed.append(f"- {label}: `{item['command']}` {mark}")
        changed = found.get("changed") or {}
        if changed.get("files"):
            observed.append(
                f"- Changes: +{changed['insertions']} −{changed['deletions']} in "
                f"{changed['files']} file{'s' if changed['files'] != 1 else ''}"
                + (f", {changed['commits']} commit(s)" if changed.get("commits") else "")
            )
        for checkout in found.get("checkouts", []):
            if checkout.get("branch"):
                observed.append(f"- Branch `{checkout['branch']}`")
        for pr in found.get("prs", []):
            observed.append(f"- [{pr['label']}]({pr['url']}): checks {pr['checks']}")
        for item in found.get("version", []):
            edited = " with uncommitted edits" if item["edited"] else ""
            observed.append(f"- Checked at `{item['commit'][:7]}`{edited}")
        observed += [f"- ⚠ {flag}" for flag in found.get("flags", [])]
        if results := found.get("criteria"):
            marks = {"verified": "✅", "claimed": "☑️", "contradicted": "❌", "not_met": "❌"}
            words = {
                "verified": "verified",
                "claimed": "reported met, not observed",
                "contradicted": "reported met, but the check failed",
                "not_met": "not met",
                "not_checked": "not checked",
            }
            lines += ["**Done when**", ""]
            for item in results:
                how = f": `{item['command']}`" if item["command"] else ""
                lines.append(
                    f"- {marks.get(item['state'], '⬜')} {item['criterion']} "
                    f"({words[item['state']]}{how})"
                )
            lines.append("")
        if observed:
            lines += ["**Observed**", "", *observed, ""]
        if self.store.settings().report_footer:
            lines.append(f"<sub>Orchestrated with [Chief of Staff]({PROJECT_URL})</sub>")
        return "\n".join(lines).strip() + "\n"

    def fail(self, session_id: str, message: str):
        """Its session failed: none of its open tasks goes on."""
        for task in self.tasks.for_session(session_id):
            if task["status"] in OPEN:
                self.tasks.move(task["id"], "failed", progress=message[:500])

    def report_task(self, caller: str, reference: str) -> dict | None:
        """The task a report is about: the one it names; else its only open task (not the one
        given last, which may be finished); else the task it is on."""
        if not reference:
            open_ = [task for task in self.tasks.for_session(caller) if task["status"] in OPEN]
            if len(open_) == 1:
                return open_[0]
            if len(open_) > 1:
                # Filed against whichever was given last, it would be another task's report.
                listed = "; ".join(f"{task['id']} “{task['title']}”" for task in open_)
                raise ValueError(
                    f"You have {len(open_)} open tasks ({listed}): call report again with the "
                    "task_id of the one this is about."
                )
            # Nothing open: news on a thread it follows is about the task it follows it for.
            last = self.store.one(
                "SELECT id FROM messages WHERE session_id=? AND kind='report' "
                "ORDER BY id DESC LIMIT 1",
                (caller,),
            )
            news = self.store.one(
                "SELECT json_extract(meta, '$.task_id') AS task_id FROM messages "
                "WHERE session_id=? AND kind='inbound' AND id>? "
                "AND json_extract(meta, '$.thread') IS NOT NULL "
                "AND json_extract(meta, '$.task_id') IS NOT NULL ORDER BY id DESC LIMIT 1",
                (caller, last["id"] if last else 0),
            )
            if news and (task := self.tasks.find(news["task_id"])):
                return task
            return self.tasks.current(caller)
        task = self.tasks.find(reference)
        if not task or task["session_id"] != caller:
            raise ValueError(f"You have no task {reference}.")
        return task

    def task_for(self, reference: str) -> dict:
        """A task by its id, or a worker's current task by the worker's id."""
        if task := self.tasks.find(reference):
            return task
        try:
            row = self.store.session(reference)
        except LookupError:
            raise ValueError(f"No task or worker {reference}; list_sessions lists them.") from None
        if row["role"] != WORKER or not (task := self.tasks.current(row["id"])):
            raise ValueError(f"{reference} is not a worker with a task.")
        return task

    async def cancel(self, task_id: str, reason: str = "", by: str = "you") -> dict:
        """End a task for good. Its worker stops if it is on this task, and stays open."""
        task = self.tasks.get(task_id)
        if task["status"] in ENDED:
            raise ValueError(f"Task {task_id} is {task['status']} already.")
        row = self.store.find_session(task["session_id"])
        # At work on it now: the task its turn is on (not merely the one it was given last).
        on_it = (
            bool(row)
            and task["status"] in OPEN
            and (self.runtime.working_on(row["id"]) or row["task_id"]) == task_id
        )
        why = f"Cancelled by {by}" + (f": {reason}" if reason else ".")
        if by == "you" and reason.strip():
            self.runtime.learning.record(
                "cancel",
                context=self.about(task, task["goal"]),
                owner=reason,
                scope=self.runtime.learning.scope_of(task["session_id"]),
            )
        was_stopped = bool(self.store.get(f"stopped:{task_id}"))
        if was := self.store.get(f"reopened:{task_id}"):
            # Only the follow-up on finished work is cancelled: its result stands.
            self.store.delete(f"reopened:{task_id}")
            task = self.tasks.move(task_id, was, progress=f"Follow-up cancelled ({why})")
        else:
            task = self.tasks.move(task_id, "cancelled", progress=why)
        others = [
            other
            for other in self.tasks.for_session(task["session_id"])
            if other["id"] != task_id and other["status"] in OPEN
        ]
        note = (
            f"The follow-up on task {task_id} “{task['title']}” was cancelled: stop that work. "
            "Its earlier result stands; no report."
            if was
            else f"Task {task_id} “{task['title']}” was cancelled. Stop work on it; no report."
        )
        if row and not row["closed_at"] and others:
            # Its other tasks go on: its turn is not stopped (it may be on another task), only
            # this task's instructions it has not received are dropped, and it hears of the
            # cancellation within its turn, if it knew of the task at all.
            received = self.delivery.drop_pending(row["id"], task_id)
            if row["task_id"] == task_id:
                self.store.execute(
                    "UPDATE sessions SET task_id=? WHERE id=?", (others[-1]["id"], row["id"])
                )
            if on_it and row["status"] == "working" and self.states.is_live(row["id"]):
                # At work on it: that work stops now (the running command too), and it goes on
                # with its other tasks, with what it was sent for them and never saw.
                self.runtime.asks.cancel_requests(row["id"], why, keep_questions=task_id)
                self.states[row["id"]].after_abort = True  # Its aborted turn owes no report.
                if handle := self.states.handle(row["id"]):
                    with suppress(Exception):
                        await handle.abort()
                    await stop_background(handle)
                self.states[row["id"]].busy = False
                self.states[row["id"]].steps.clear()
                if self.delivery.unsee(row["id"]):
                    # What it never saw about this task is not sent now: only the others'.
                    self.delivery.drop_pending(row["id"], task_id)
                self.runtime.update_session(row["id"], status="idle", activity="")
                listed = "; ".join(f"{other['id']} “{other['title']}”" for other in others)
                await self.delivery.deliver(
                    row["id"],
                    wrap("system", "cancelled", f"{note} Go on with your other open task(s): "
                         f"{listed}.", task=task_id),
                    kind="notice",
                    display=why,
                    meta={"task_id": task_id},
                )  # fmt: skip
            elif (
                was_stopped
                and (going := [o for o in others if not self.store.get(f"stopped:{o['id']}")])
                and row["status"] != "working"
                and self.delivery.undefer(row["id"])
            ):
                # Stopped on this task, then it is cancelled: what it was given for its other
                # tasks and held back goes out now, so it goes on with them (not with another
                # task the owner stopped). After a restart too: delivering resumes its session.
                listed = "; ".join(f"{other['id']} “{other['title']}”" for other in going)
                text = (f"{note} " if received else "") + f"Go on with your open task(s): {listed}."
                await self.delivery.deliver(
                    row["id"],
                    wrap("system", "cancelled" if received else "go-on", text, task=task_id),
                    kind="notice",
                    display=why,
                    meta={"task_id": task_id},
                )
            elif received and row["status"] == "working" and self.states.is_live(row["id"]):
                # Not "stopped": its turn goes on, and it still owes a report on its other task.
                await self.delivery.deliver(
                    row["id"],
                    wrap("system", "cancelled", note, task=task_id),
                    kind="notice",
                    display=why,
                    meta={"task_id": task_id},
                    steer=True,
                )
            elif received:
                self.runtime.add_message(
                    row["id"],
                    "notice",
                    why,
                    {
                        "prompt": wrap("system", "cancelled", note, task=task_id),
                        "deferred": True,
                        "task_id": task_id,
                    },
                )
        elif row and not row["closed_at"]:
            if on_it:
                self.runtime.asks.cancel_requests(row["id"], why, keep_questions=task_id)
                self.states[row["id"]].after_abort = True
                if handle := self.states.handle(row["id"]):
                    with suppress(Exception):
                        await handle.abort()
                    await stop_background(handle)  # And what it left running in the background.
                self.states[row["id"]].busy = False
                self.states[row["id"]].after_refresh = ""  # Not "go on" once it is stopped.
                self.delivery.defer_pending(row["id"])  # Its task, if it never started.
                if row["status"] in {"working", "queued", "starting", "waiting"}:
                    self.runtime.update_session(row["id"], status="idle", activity="")
            # Pending, not sent: it reaches the worker with its next message, not as a new turn.
            self.runtime.add_message(
                row["id"],
                "notice",
                why,
                {
                    "prompt": wrap("system", "cancelled", note, task=task_id),
                    "stopped": True,
                    "deferred": True,
                },
            )
        with suppress(RuntimeError):
            display = f"Task “{task['title']}” cancelled"
            meta = {"session_id": task["session_id"], "task_id": task_id}
            if by.lower().removeprefix("the ") == "router":
                self.runtime.add_message(self.runtime.router_id(), "notice", display, meta)
            else:
                # The Router hears of it with its next message (not a turn of its own): it must
                # not start that work again by itself, here or in another worker.
                who = "The owner" if by == "you" else by[:1].upper() + by[1:]
                told = (
                    f"{who} cancelled the follow-up on task {task_id} “{task['title']}”; its "
                    "earlier result stands."
                    if was
                    else f"{who} cancelled task {task_id} “{task['title']}”."
                )
                told += f" Reason: {reason.strip()}" if reason.strip() else ""
                told += (
                    " Do not start that work again (in this worker or another) unless the owner "
                    "asks for it."
                )
                self.delivery.hold(
                    self.runtime.router_id(),
                    wrap("system", "cancelled", told, task=task_id, title=task["title"]),
                    kind="notice",
                    display=display,
                    meta=meta,
                )
        self.runtime.background(
            self.runtime.workers.pump()
        )  # Tasks that depended on it are now blocked.
        return task

    def rename(self, task_id: str, title: str, by: str = "you") -> dict:
        """A task named for what it delivers; its earlier names are kept (shown on hover)."""
        title = deliverable_title(title)
        task = self.tasks.get(task_id)
        if title == task["title"]:
            return self.view(task)
        names = json.loads(self.store.get(f"titles:{task_id}") or "[]")
        names.append({"title": task["title"], "at": now(), "by": by})
        self.store.set(f"titles:{task_id}", json.dumps(names[-20:]))
        task = self.tasks.update(task_id, title=title)
        self.runtime.publish_session(self.store.session(task["session_id"]))
        return self.view(task)

    def split_parts(self, task: dict) -> tuple[dict, dict, list[dict]]:
        """What a split would do: (the result it keeps, the latest result that moves, the
        messages that move with it: from the instruction that asked for that result on).
        Raises when there is nothing to split off."""
        if task["status"] not in {"review", "done"}:
            raise ValueError(
                f"Task {task['id']} is {task['status']}: split it once it is finished."
            )
        messages = self.store.all(
            "SELECT id, kind, text, meta, created_at FROM messages WHERE session_id=? "
            "AND json_extract(meta, '$.task_id')=? ORDER BY id",
            (task["session_id"], task["id"]),
        )
        reports = [
            m for m in messages
            if m["kind"] == "report" and json.loads(m["meta"] or "{}").get("status") == "done"
        ]  # fmt: skip
        if len(reports) < 2:
            raise ValueError(
                "It has one result only: rename it instead, or give the other work a new task."
            )
        earlier, latest = reports[-2], reports[-1]
        # The instructions that asked for the latest result: those since the worker last
        # answered before it (a reply that asks nothing back, or a report on this task). An
        # exchange about the earlier result (a decision on it, a question answered) stays.
        between = self.store.all(
            "SELECT id, kind, text, meta FROM messages WHERE session_id=? AND id>? AND id<? "
            "ORDER BY id DESC",
            (task["session_id"], earlier["id"], latest["id"]),
        )
        asked: list[int] = []
        for message in between:
            meta = json.loads(message["meta"] or "{}")
            if message["kind"] in {"router", "owner"} and meta.get("task_id") == task["id"]:
                asked.append(message["id"])
            elif (
                asked
                and not meta.get("narration")
                and (
                    message["kind"] == "assistant"
                    or (message["kind"] == "report" and meta.get("task_id") == task["id"])
                )
                and not message["text"].rstrip().endswith(("?", "\uff1f"))
            ):
                break
        first = min(asked) if asked else latest["id"]
        moving = [m for m in messages if m["id"] >= first]
        return dict(earlier), dict(latest), [dict(m) for m in moving]

    def splittable(self, task: dict) -> bool:
        """Whether its latest result looks like other work than the one before (it names other
        PRs, files or links), so splitting it off is worth offering."""
        try:
            earlier, latest, _ = self.split_parts(task)
        except ValueError:
            return False

        def named(text: str) -> set[str]:
            found = {f"{repo}#{number}".lower() for repo, number in threads.PR_URL.findall(text)}
            found |= {threads.link_key(url) for url in threads.links(text)}
            found |= {code for code in re.findall(r"`([^`\n]{3,80})`", text) if "." in code}
            return found

        new = named(latest["text"])
        return bool(new) and not new <= named(earlier["text"])

    def split_preview(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        try:
            earlier, latest, moving = self.split_parts(task)
        except ValueError as exc:
            return {"allowed": False, "why": str(exc)}
        return {
            "allowed": True,
            "why": "" if self.splittable(task) else "Its results look like the same work.",
            "keeps": {"result": earlier["text"][:4000], "at": earlier["created_at"]},
            "moves": {
                "result": latest["text"][:4000],
                "at": latest["created_at"],
                "messages": [
                    {k: m[k] for k in ("id", "kind", "created_at")} | {"text": m["text"][:300]}
                    for m in moving
                ],
            },
        }

    def split(self, task_id: str, title: str, by: str = "you") -> dict:
        """A finished task that came to hold two deliverables: its latest result becomes a
        task of its own, named for it; the task keeps the result it had before. That result's
        report and the messages that asked for it move with it; the worker's name, the task's
        follows and the rest stay. Undone with merge_back within SPLIT_UNDO_MINUTES."""
        title = deliverable_title(title)
        task = self.tasks.get(task_id)
        earlier, latest, moving = self.split_parts(task)
        # Finished work split in two: what its worker is on now stays its current task.
        current = self.store.value("SELECT task_id FROM sessions WHERE id=?", (task["session_id"],))
        made = self.tasks.create(
            task["session_id"], title, f"Split off task {task_id}.", status=task["status"]
        )
        self.store.execute(
            "UPDATE sessions SET task_id=? WHERE id=?", (current, task["session_id"])
        )
        self.tasks.start_run(made["id"], "split")
        made = self.tasks.move(made["id"], task["status"], result=latest["text"])
        for row in moving:
            meta = json.loads(row["meta"] or "{}")
            self.store.update_message_meta(row["id"], {**meta, "task_id": made["id"]})
        undo = self.split_changes(task, made, latest, moving[0]["created_at"] if moving else now())
        names = json.loads(self.store.get(f"titles:{made['id']}") or "[]")
        names.append({"title": f"split from “{task['title']}”", "at": now(), "by": by})
        self.store.set(f"titles:{made['id']}", json.dumps(names))
        mark = self.store.value("SELECT max(id) FROM messages") or 0
        until = (datetime.now(UTC) + timedelta(minutes=SPLIT_UNDO_MINUTES)).isoformat()
        self.store.set(
            f"splitfrom:{made['id']}",
            json.dumps({
                "task_id": task_id, "until": until, "mark": mark, "status": made["status"],
                "moved": [m["id"] for m in moving], "result": task["result"], "undo": undo,
            }),
        )  # fmt: skip
        self.tasks.update(task_id, result=earlier["text"])
        self.publish(self.tasks.get(task_id))
        self.runtime.publish_session(self.store.session(task["session_id"]))
        return self.view(self.tasks.get(made["id"]))

    def last_message(self, session_id: str, task_id: str) -> int:
        """The id of the newest message about this task: when it last saw activity."""
        return (
            self.store.value(
                "SELECT max(id) FROM messages WHERE session_id=? "
                "AND json_extract(meta, '$.task_id')=?",
                (session_id, task_id),
            )
            or 0
        )

    def worked_on_since_split(self, task_id: str, found: dict) -> bool:
        """Whether the task split off saw work since (a follow-up, a report, a status of its
        own): undoing the split then would lose it."""
        task = self.tasks.find(task_id)
        if not task or task["status"] != found.get("status", task["status"]):
            return True
        return self.last_message(task["session_id"], task_id) > int(found.get("mark") or 0)

    def split_from(self, task_id: str) -> dict | None:
        found = json.loads(self.store.get(f"splitfrom:{task_id}") or "null")
        if found and self.worked_on_since_split(task_id, found):
            # Its Undo is gone: it is a task of its own now.
            return {"task_id": found["task_id"], "until": ""}
        return {"task_id": found["task_id"], "until": found["until"]} if found else None

    def merge_back(self, task_id: str) -> dict:
        """Undo a split, soon after: the result and messages go back to the task they came from."""
        found = json.loads(self.store.get(f"splitfrom:{task_id}") or "null")
        if not found:
            raise ValueError("That task was not split off.")
        if found["until"] < now():
            raise ValueError("Too late to undo the split: rename or cancel the task instead.")
        if self.worked_on_since_split(task_id, found):
            raise ValueError(
                "It was worked on since the split, so it stays a task of its own: rename or "
                "cancel it instead."
            )
        made = self.tasks.get(task_id)
        original = found["task_id"]
        for message_id in found["moved"]:
            row = self.store.one("SELECT meta FROM messages WHERE id=?", (message_id,))
            if row:
                meta = json.loads(row["meta"] or "{}")
                self.store.update_message_meta(message_id, {**meta, "task_id": original})
        self.store.delete(f"splitfrom:{task_id}")
        self.store.execute(
            "DELETE FROM task_deps WHERE task_id=? OR depends_on=?", (task_id, task_id)
        )
        undo = found.get("undo") or {}
        if undo.get("ended"):
            self.store.set(f"taskend:{original}", json.dumps(undo["ended"]))
        back = self.store.one(
            "SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (original,)
        )
        if back:
            for identifier in undo.get("commands") or []:
                self.store.execute(
                    "UPDATE commands SET run_id=? WHERE id=?", (back["id"], identifier)
                )
        for key in ("taskbase", "taskend", "taskends", "touched"):
            self.store.delete(f"{key}:{task_id}")
        self.store.execute("DELETE FROM runs WHERE task_id=?", (task_id,))
        self.store.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        restored = self.tasks.update(original, result=found.get("result") or made["result"])
        self.runtime.background(self.collect_evidence(original))
        self.publish(restored)
        self.runtime.publish_state()  # The split-off task is gone from every list.
        self.runtime.publish_session(self.store.session(made["session_id"]))
        return self.view(restored)

    def news(self, task_id: str, text: str):
        """What a worker reported about its finished work (its PR approved or merged, a reply
        handled): kept in sight on the board until the owner has seen it."""
        self.store.set(f"news:{task_id}", json.dumps({"text": text.strip()[:2000], "at": now()}))

    def news_seen(self, task_id: str) -> dict:
        self.store.delete(f"news:{task_id}")
        task = self.tasks.get(task_id)
        self.publish(task)
        return task

    def accept(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if task["status"] != "review":
            raise ValueError(f"Task {task_id} is {task['status']}, not ready for review.")
        self.store.delete(f"news:{task_id}")  # Seen with the result.
        task = self.tasks.move(task_id, "done")
        self.runtime.background(self.runtime.workers.pump())  # Tasks waiting for its check.
        scope = self.runtime.learning.scope_of(task["session_id"])
        self.runtime.learning.record("accept", context=task["title"], scope=scope, agreed=True)
        return task

    async def answer_question(self, question_id: str, text: str) -> dict:
        """The owner answers a worker's question kept under Needs you (by its id, or by its
        worker's). Answering it, they want the work: a task cancelled or finished meanwhile
        goes on with the answer."""
        question = self.runtime.asks.owner_question(question_id)
        if not question:
            raise ValueError("That question was answered or dismissed already.")
        session_id = question.get("id", question_id).split(":", 1)[0]
        task = self.tasks.find(question["task_id"]) if question["task_id"] else None
        if not task:
            self.runtime.asks.answered(session_id, "")
            await self.runtime.owner_send(session_id, text, clear_questions=False)
            return {"session_id": session_id}
        return await self.follow_up(task["id"], text, resume=True)

    async def follow_up(self, task_id: str, text: str, resume: bool = False) -> dict:
        """The owner adds to a task: its worker continues it (a finished task reopens).
        `resume`: the owner answered its question after it was cancelled: it goes on."""
        task = self.tasks.get(task_id)
        if task["status"] == "cancelled" and not resume:
            raise ValueError("That task was cancelled. Ask the Router for new work instead.")
        row = self.store.session(task["session_id"])
        if row["closed_at"]:
            await self.runtime.workers.reopen(row["id"])
        self.store.delete(f"news:{task_id}")  # Answering it, the owner has seen its news.
        self.runtime.asks.answered(task["session_id"], task_id)
        before = task["status"]
        self.runtime.learning.record(
            "follow-up",
            context=self.about(task, task["goal"]),
            proposed=task["result"],
            owner=text,
            scope=self.runtime.learning.scope_of(task["session_id"]),
            agreed=False if before in {"review", "done"} else None,
            untrusted=bool(row["slack"]),
        )
        if before == "cancelled":
            # Answered after it was cancelled: the owner wants it after all.
            self.drop_cancelled_note(row["id"], task_id)
            with suppress(RuntimeError):
                self.drop_cancelled_note(self.runtime.router_id(), task_id)
        if before in {"review", "done", "failed", "cancelled"}:
            # Cancelling this follow-up gives the finished result back.
            self.store.set(f"reopened:{task_id}", before)
            task = self.tasks.move(task_id, "running", reason="follow-up")
        elif before == "waiting" and self.store.get(f"askedafter:{task_id}"):
            # Finished work it asked the owner about: answered, it is back where it was, and
            # what its worker says next is news on it.
            task = self.runtime.asks.go_on(task)
        elif before == "waiting":
            # The owner answered what it waited for: it goes on (and asks again if need be).
            task = self.tasks.move(task_id, "running", progress="")
        prompt = wrap("owner", "message", text, task=task_id, reply_in=self.runtime.heard(text))
        if before == "cancelled":
            prompt += "\n\n" + wrap(
                "system",
                "context",
                f"Task {task_id} “{task['title']}” was cancelled before; with this answer the "
                "owner wants it after all, and it is running again. Do it now with their "
                "answer, and report.",
            )
        try:
            await self.delivery.deliver(
                row["id"],
                prompt,
                kind="owner",
                display=text,
                meta={"task_id": task_id},
                steer=True,
            )
        except Exception:
            if task["status"] != before:
                self.store.delete(f"reopened:{task_id}")
                self.tasks.move(task_id, before)  # The follow-up never reached its worker.
            raise
        self.runtime.owner_told(row, text)
        return task

    def drop_cancelled_note(self, session_id: str, task_id: str):
        """The note that a task was cancelled, not sent yet, is moot once it is wanted again."""
        mark = int(self.store.get(f"sent:{session_id}") or 0)
        for message in self.store.messages(session_id, after=mark):
            prompt = message["meta"].get("prompt", "")
            if 'type="cancelled"' in prompt and f'task="{task_id}"' in prompt:
                kept = {k: v for k, v in message["meta"].items() if k != "prompt"}
                self.store.update_message_meta(message["id"], {**kept, "dropped": True})

    async def retry(self, task_id: str, text: str = "") -> dict:
        """Run a failed or cancelled task again in its worker (reopened if closed)."""
        task = self.tasks.get(task_id)
        if task["status"] not in {"failed", "cancelled"}:
            raise ValueError(
                f"Task {task_id} is {task['status']}; only failed or cancelled tasks are retried."
            )
        row = self.store.session(task["session_id"])
        if row["closed_at"]:
            await self.runtime.workers.reopen(row["id"])
        before = task
        if text.strip():
            self.runtime.learning.record(
                "retry",
                context=self.about(task, task["progress"]),
                owner=text,
                scope=self.runtime.learning.scope_of(task["session_id"]),
            )
        self.store.set(f"reopened:{task_id}", before["status"])
        if before["status"] == "cancelled":
            self.drop_cancelled_note(row["id"], task_id)  # Moot: it is wanted after all.
            with suppress(RuntimeError):
                self.drop_cancelled_note(self.runtime.router_id(), task_id)
        task = self.tasks.move(task_id, "running", reason="retry", progress="")
        body = f"The owner asks you to try task {task_id} “{task['title']}” again."
        if text.strip():
            body += f"\n\n{text.strip()}"
        try:
            await self.delivery.deliver(
                row["id"],
                wrap(
                    "owner",
                    "message",
                    body,
                    task=task_id,
                    reply_in=self.runtime.heard(text) or self.store.get("owner_language"),
                ),
                kind="owner",
                display=text.strip() or "Try again.",
                meta={"task_id": task_id},
            )
        except Exception:
            self.store.delete(f"reopened:{task_id}")
            self.tasks.move(task_id, before["status"], progress=before["progress"])
            raise
        self.runtime.owner_told(row, body)
        return task

    def followed_for(self, task: dict, follows: list[str]) -> list[str]:
        """The threads and PRs its worker follows for this task: those it was given or started
        following for it; from before follows were tied to a task, its own PRs and those its
        title or instructions name, and a Slack thread when the worker is on it. Not every
        follow of the worker: one tied to another task, or to none, is not this one's."""
        from .tools import names_pr

        if task["status"] in {"cancelled", "failed"} or not follows:
            return []
        session_id = task["session_id"]
        asked = f"{task['title']}\n{task['goal'] or ''}"
        current = self.tasks.current(session_id)
        found = []
        for key in follows:
            bound = self.store.follow_tasks(session_id, key)
            if bound:
                if task["id"] in bound:
                    found.append(key)
                continue
            pr = threads.GITHUB_PR.fullmatch(key)
            if pr and self.store.get(f"splitfrom:{task['id']}"):
                continue  # Split off: the follows stay with the task it came from.
            if pr:
                label = f"{pr.group(1)}#{pr.group(2)}".lower()
                if self.creator(label) == task["id"] or names_pr(
                    asked, pr.group(1), int(pr.group(2))
                ):
                    found.append(key)
            elif task["status"] in OPEN and current and current["id"] == task["id"]:
                found.append(key)  # A Slack thread: the task it was on.
        return found

    def watch_standing(self, task: dict, follows: list[str]) -> dict:
        """What the PRs it follows are to it ("own": its delivery waiting on others;
        "reviewed": others' work it reviewed) and when any last changed."""
        watched = {
            f"{found.group(1)}#{found.group(2)}".lower()
            for key in self.watching(task, follows)
            if (found := threads.GITHUB_PR.fullmatch(key))
        }
        prs = [pr for pr in self.previous_prs(task["id"]) if pr["label"].lower() in watched]
        reviewed = any(pr.get("reviewed") for pr in prs) or bool(
            watched and watched <= self.reviewed_prs(task["session_id"])
        )
        kind = "own" if any(pr.get("own") for pr in prs) else "reviewed" if reviewed else ""
        changed = max((pr.get("changed_at") or pr.get("at") or "" for pr in prs), default="")
        return {"watch_kind": kind, "watch_changed_at": changed}

    def watching(self, task: dict, follows: list[str]) -> list[str]:
        """The threads it waits on for this task that can be read: what may bring it news."""
        failing = self.failing(task, follows)
        return [key for key in self.followed_for(task, follows) if key not in failing]

    def failing(self, task: dict, follows: list[str]) -> list[str]:
        """Its threads for this task that could not be read several times in a row: shown as
        such, not as something it waits on."""
        row = self.store.find_session(task["session_id"])
        health = self.runtime.follow_health(row["id"]) if row else {}
        return [
            key for key in self.followed_for(task, follows) if health.get(key, 0) >= FAILING_READS
        ]

    def view(self, task: dict) -> dict:
        row = self.store.find_session(task["session_id"]) or {}
        follows = self.store.follows(row["id"]) if row else []
        return {
            **task,
            "worker": self.runtime.display_title(row) if row else "",
            "worker_status": row.get("status", ""),
            "worker_closed": bool(row.get("closed_at")),
            "cwd": row.get("cwd", ""),
            "branch": row.get("branch", ""),
            "after": [dep["id"] for dep in self.tasks.deps(task["id"])],
            "runs": len(self.tasks.runs(task["id"])),
            "credits": self.tasks.credits(task["id"]),
            "blocked": task["status"] == "queued" and bool(self.tasks.broken(task)),
            "receipts": self.receipts(task),
            "decisions": self.runtime.decisions.for_task(task["id"]),
            "news": json.loads(self.store.get(f"news:{task['id']}") or "null"),
            # What tells a running task's state apart: woken later to check something, the
            # threads it waits on, and when its worker last did anything.
            "check_back": self.check_back(task),
            "watching": self.watching(task, follows),
            **self.watch_standing(task, follows),
            "watch_failing": self.failing(task, follows),
            "earlier_titles": json.loads(self.store.get(f"titles:{task['id']}") or "[]"),
            # When its worker was last asked where this task stands (stalled).
            "nudged_at": self.store.get(f"stallnudge:{task['id']}") or "",
            "active_at": self.active_at(task, row) if row else task["updated_at"],
            # Its worker has taken it up (a task given while it is on another is not, until
            # that one's turn ends): one not taken up waits its turn; one taken up and left
            # is paused, not up next.
            "started": bool(task["progress"] or task["result"])
            or bool(self.store.get(f"started:{task['id']}")),
            # The owner asked about it after it finished: its worker at it is following up.
            "follow_up": bool(self.store.get(f"followup:{task['id']}")),
            # Stopped by the owner (or the Watchdog) mid-work: it waits to be told to go on.
            "stopped": self.store.get(f"stopped:{task['id']}") or "",
            # What it made or runs, as its worker last said: a short name and status each.
            "artifacts": json.loads(self.store.get(f"artifacts:{task['id']}") or "[]"),
            # Every link its reports gave, and those it was given (not its own work).
            "links": json.loads(self.store.get(f"links:{task['id']}") or "[]"),
            "given_links": self.given_links(task, row),
            # What its reports call those links ([the plan](…)), and the ones they say it made.
            "link_names": json.loads(self.store.get(f"linknames:{task['id']}") or "{}"),
            "made_links": json.loads(self.store.get(f"madelinks:{task['id']}") or "[]"),
            # Files it wrote in its own folder; whether the owner read its result; whether the
            # Router said the owner wants to see it (then it waits for their OK).
            "files": self.made_files(task, row),
            "read": bool(self.store.get(f"read:{task['id']}")),
            "review_required": bool(self.store.get(f"reviewreq:{task['id']}")),
            # Its latest result looks like other work (offered to split off), and, split off
            # from another task, until when that can be undone.
            "splittable": task["status"] in {"review", "done"} and self.splittable(task),
            "split_from": self.split_from(task["id"]),
        }

    def mark_read(self, task_id: str) -> dict:
        """The owner read its result (opened its report)."""
        task = self.tasks.get(task_id)
        if not self.store.get(f"read:{task_id}"):
            self.store.set(f"read:{task_id}", now())
            self.publish(task)
        return self.view(task)

    def declare_artifacts(self, task_id: str, items: list[dict]):
        """Its worker says what the task made or runs and where each stands: kept by link (or
        by name: declared again under another link, it is the same thing moved on), the latest
        word on each; one said again moves to the top."""
        known = json.loads(self.store.get(f"artifacts:{task_id}") or "[]")
        stamp = now()
        row = self.store.find_session(self.tasks.get(task_id)["session_id"])
        for item in items:
            url = str(item.get("url") or "").strip()
            path = self.local_file(row, url)
            if path:
                url = Path(path).as_uri()  # A file it made (a mockup): opened from its folder.
            elif not re.match(r"https?://\S+$", url) or threads.PR_URL.match(url):
                continue  # Not a link; a PR's status comes from GitHub.
            entry = {
                **({"path": path} if path else {}),
                "url": url[:2000],
                "name": clip(" ".join(str(item.get("name") or "").split()), 60),
                "status": clip(" ".join(str(item.get("status") or "").split()), 40),
                "at": stamp,
            }
            known = [
                other
                for other in known
                if other["url"] != url and not (entry["name"] and other["name"] == entry["name"])
            ] + [entry]
        self.store.set(f"artifacts:{task_id}", json.dumps(known[-20:]))
        self.publish(self.tasks.get(task_id))

    def local_file(self, row: dict | None, given: str) -> str:
        """A file in one of the worker's folders, by its path or file:// link; "" otherwise."""
        if not row or not given:
            return ""
        path = given.removeprefix("file://") if given.startswith("file://") else given
        if not os.path.isabs(path):
            return ""
        real = os.path.realpath(path)
        # Its folders: its workspace (its scratch files too), its checkouts.
        folders = self.workspaces.writable(row) if row.get("id") else [row.get("cwd") or ""]
        if not os.path.isfile(real) or not any(
            folder and within(real, os.path.realpath(folder)) for folder in folders
        ):
            return ""
        return real

    def made_files(self, task: dict, row: dict) -> list[dict]:
        """Files its edits wrote in its own folder (not in a checkout, whose changes are its
        receipts) that are still there: a mockup, a plan, a CSV it delivered."""
        if not row or not row.get("cwd"):
            return []
        checkouts = [os.path.realpath(c["path"]) for c in self.workspaces.checkouts(row)]
        folders = [
            os.path.realpath(folder)
            for folder in self.workspaces.writable(row)
            if folder and os.path.realpath(folder) not in checkouts
        ]
        found = []
        for path in json.loads(self.store.get(f"touched:{task['id']}") or "[]"):
            if not any(within(path, folder) for folder in folders) or any(
                within(path, c) for c in checkouts
            ):
                continue
            with suppress(OSError):
                size = os.path.getsize(path)
                found.append({"path": path, "name": os.path.basename(path), "size": size})
        return found[-20:]

    def reported_links(self, task_id: str, text: str):
        """Every link its reports gave, kept: a later report that does not repeat a doc it made
        does not take it off the task (nor off the owner's Artifacts). With when each was first
        reported: pasted back to it later, it is still its own, not one it was given."""
        if not (found := threads.links(text)):
            return
        if names := threads.named_links(text):
            kept = json.loads(self.store.get(f"linknames:{task_id}") or "{}")
            self.store.set(
                f"linknames:{task_id}", json.dumps(dict(list({**kept, **names}.items())[-60:]))
            )
        if made := threads.made_links(text):
            kept = json.loads(self.store.get(f"madelinks:{task_id}") or "[]")
            self.store.set(
                f"madelinks:{task_id}", json.dumps(list(dict.fromkeys([*kept, *made]))[-60:])
            )
        known = json.loads(self.store.get(f"links:{task_id}") or "[]")
        merged = list(dict.fromkeys([*known, *found]))[-60:]
        if merged != known:
            self.store.set(f"links:{task_id}", json.dumps(merged))
        first = json.loads(self.store.get(f"linksat:{task_id}") or "{}")
        stamp = now()
        new = {key: stamp for key in map(threads.link_key, found) if key not in first}
        if new:
            self.store.set(
                f"linksat:{task_id}", json.dumps(dict(list({**first, **new}.items())[-200:]))
            )

    def given_links(self, task: dict, row: dict) -> list[str]:
        """Links the task was given (its instructions, the owner's and the Router's messages
        about it): what it works from, not what it made. Not its own work, pasted back to it
        ("add a summary to <its doc>")."""
        given = threads.links(task["goal"] or "")
        if row:
            first = json.loads(self.store.get(f"linksat:{task['id']}") or "{}")
            for item in self.store.all(
                "SELECT text, created_at FROM messages WHERE session_id=? AND kind IN "
                "('router', 'owner') AND json_extract(meta, '$.task_id')=? ORDER BY id",
                (row["id"], task["id"]),
            ):
                for url in threads.links(item["text"]):
                    reported = first.get(threads.link_key(url))
                    if not (reported and reported <= item["created_at"]):
                        given.append(url)
        return list(dict.fromkeys(given))[:60]

    def active_at(self, task: dict, row: dict) -> str:
        """When anything last happened on this task: its own progress or messages, and its
        worker's steps while its turn was on it."""
        about = self.store.value(
            "SELECT max(created_at) FROM messages WHERE session_id=? "
            "AND json_extract(meta, '$.task_id')=?",
            (row["id"], task["id"]),
        )
        stamps = [task["updated_at"], about or ""]
        if self.runtime.working_on(row["id"]) == task["id"]:
            last = "SELECT max(created_at) FROM messages WHERE session_id=?"
            stamps.append(self.store.value(last, (row["id"],)) or "")
        return max(stamp for stamp in stamps if stamp is not None)

    def stalled(self, task: dict, now: datetime | None = None) -> float:
        """Hours a running task has been stalled (0 when not): its worker is not at work, it
        waits on nothing it follows and has no check-back, and nothing happened on it for
        stall_hours."""
        if task["status"] != "running":
            return 0
        row = self.store.find_session(task["session_id"])
        if not row or row["closed_at"] or row["status"] in {"working", "starting", "queued"}:
            return 0
        if self.check_back(task) or self.watching(task, self.store.follows(row["id"])):
            return 0
        now = now or datetime.now(UTC)
        idle = (now - datetime.fromisoformat(self.active_at(task, row))).total_seconds() / 3600
        return idle if idle >= self.store.settings().stall_hours else 0

    async def nudge(self, task_id: str, by: str = "you") -> dict:
        """Ask a stalled task's worker where it stands: it goes on, reports, or says what it
        waits on (check_back) or what blocks it."""
        task = self.tasks.get(task_id)
        if task["status"] not in {"running", "waiting"}:
            raise ValueError(f"Task {task_id} is {task['status']}: nothing to nudge.")
        row = self.store.session(task["session_id"])
        if row["closed_at"]:
            await self.runtime.workers.reopen(row["id"])
        asker = "The owner asks" if by == "you" else "Chief of Staff asks"
        text = (
            f"{asker} where task {task_id} “{task['title']}” stands: nothing has happened on it "
            f"for a while. Go on with it now; or report it (done, or a question); or, if it waits "
            "for something outside (a run, a review), check_back when to look again."
        )
        await self.delivery.deliver(
            row["id"],
            wrap("system", "nudge", text, task=task_id),
            kind="notice",
            display="Asked where it stands." if by == "you" else "Stalled: asked where it stands.",
            meta={"task_id": task_id},
            steer=True,
        )
        self.store.set(f"stallnudge:{task_id}", now())  # After it: the nudge is not activity.
        return self.view(self.tasks.get(task_id))

    async def nudge_stalled(self, moment: datetime | None = None):
        """Each stalled task's worker is asked once where it stands (again only after more
        activity and another stall)."""
        for task in self.store.all("SELECT * FROM tasks WHERE status='running'"):
            if not self.stalled(task, moment):
                continue
            nudged = self.store.get(f"stallnudge:{task['id']}") or ""
            row = self.store.find_session(task["session_id"])
            if not row or (nudged and nudged >= self.active_at(task, row)):
                continue
            try:
                await self.nudge(task["id"], by="watchdog")
            except Exception:
                logger.warning("Could not nudge stalled task %s.", task["id"], exc_info=True)
            self.publish(self.tasks.get(task["id"]))

    def publish(self, task: dict):
        self.bus.publish("task", self.view(task))
        if task["status"] in {"failed", "cancelled"} and not self.store.get(
            f"peerstop:{task['id']}"
        ):
            # Once: the workers it works with stop waiting for it.
            self.store.set(f"peerstop:{task['id']}", "1")
            self.runtime.background(self.runtime.peers.stopped(task))
        # Whether a task is blocked depends on the tasks it waits for.
        for dependent in self.tasks.dependents(task["id"]):
            if dependent["status"] in OPEN:
                self.bus.publish("task", self.view(dependent))
