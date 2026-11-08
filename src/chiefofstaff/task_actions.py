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
from .envelope import wrap
from .roles import WORKER
from .sources.slack import describe
from .store import hours_ago, now
from .tasks import ENDED, OPEN
from .workspaces import (
    base_of,
    changes,
    checkout_git,
    diff,
    same_version,
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
            if reason := await self.changed_since(evidence, live):
                self.stale(task["id"], reason)

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

    async def collect_evidence(self, task_id: str, keep_stale: bool = False):
        """After a task is reported done: its checkouts' changes and its PRs' state.
        `keep_stale`: not after a report (a PR opened), so what was checked did not change."""
        task = self.tasks.get(task_id)
        row = self.store.find_session(task["session_id"])
        if not row:
            return
        checkouts = []
        for checkout in self.workspaces.checkouts(row):
            with suppress(Exception):
                if found := await changes(checkout):
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
        for pr in prs:
            pr["own"] = pr["label"].lower() in own
            pr["about"] = pr["label"].lower() in about
        return prs

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
        """The PRs a worker reviewed or replied on ("owner/repo#n"): others' work, not its own."""
        return {
            f"{target['repo']}#{target['number']}".lower()
            for row in self.store.all(
                "SELECT target FROM drafts WHERE session_id=? AND kind='github'", (session_id,)
            )
            if (target := json.loads(row["target"])).get("repo") and target.get("number")
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
            if label not in reviewed and (
                label in opened
                or (
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
        # Its own PR merged: what it delivers is in. Not a PR its report only mentions.
        merged = [pr["label"] for pr in prs if pr.get("state") == "MERGED" and pr.get("own")]
        if merged and task["status"] == "review":
            for label in merged:
                self.pr_merged(task["session_id"], f"github:{label.lower()}", task["id"])
        shown = [{k: v for k, v in pr.items() if k != "at"} for pr in evidence["prs"]]
        changed = shown != [{k: v for k, v in pr.items() if k != "at"} for pr in prs]
        evidence |= {"prs": prs, "prs_at": time.time()}
        self.store.execute(
            "UPDATE runs SET evidence=? WHERE id=?", (json.dumps(evidence), run["id"])
        )
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
                    found.append(await diff(checkout))
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
        body = self.report_markdown(task_id).split("\n", 2)[-1]  # Its title is the PR's.
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
                    "--title", task["title"], "--body", body,
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
        on_it = bool(row) and row["task_id"] == task_id and task["status"] in OPEN
        why = f"Cancelled by {by}" + (f": {reason}" if reason else ".")
        if by == "you" and reason.strip():
            self.runtime.learning.record(
                "cancel",
                context=self.about(task, task["goal"]),
                owner=reason,
                scope=self.runtime.learning.scope_of(task["session_id"]),
            )
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
            if received and row["status"] == "working" and self.states.is_live(row["id"]):
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
                if handle := self.states.handle(row["id"]):
                    with suppress(Exception):
                        await handle.abort()  # Copilot also stops the running command.
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
            self.runtime.add_message(
                self.runtime.router_id(),
                "notice",
                f"Task “{task['title']}” cancelled",
                {"session_id": task["session_id"]},
            )
        self.runtime.background(
            self.runtime.workers.pump()
        )  # Tasks that depended on it are now blocked.
        return task

    def rename(self, task_id: str, title: str, by: str = "you") -> dict:
        """A task named for what it delivers; its earlier names are kept (shown on hover)."""
        from .envelope import clean_title

        title = clean_title(title.strip())
        if not title or title == "Task":
            raise ValueError("Give it a title saying what it delivers.")
        task = self.tasks.get(task_id)
        if title == task["title"]:
            return self.view(task)
        names = json.loads(self.store.get(f"titles:{task_id}") or "[]")
        names.append({"title": task["title"], "at": now(), "by": by})
        self.store.set(f"titles:{task_id}", json.dumps(names[-20:]))
        task = self.tasks.update(task_id, title=title)
        self.runtime.publish_session(self.store.session(task["session_id"]))
        return self.view(task)

    def split(self, task_id: str, title: str, by: str = "you") -> dict:
        """A finished task that came to hold two deliverables: its latest result becomes a
        task of its own, named for it; the task keeps the result it had before. Its reports
        move with it, so each task's report is its own."""
        from .envelope import clean_title

        title = clean_title(title.strip())
        if not title or title == "Task":
            raise ValueError("Give the new task a title saying what it delivers.")
        task = self.tasks.get(task_id)
        if task["status"] not in {"review", "done"}:
            raise ValueError(f"Task {task_id} is {task['status']}: split it once it is finished.")
        reports = self.store.all(
            "SELECT id, text, meta, created_at FROM messages WHERE session_id=? "
            "AND kind='report' AND json_extract(meta, '$.task_id')=? "
            "AND json_extract(meta, '$.status')='done' ORDER BY id",
            (task["session_id"], task_id),
        )
        if len(reports) < 2:
            raise ValueError(
                "It has one result only: rename it instead, or give the other work a new task."
            )
        latest, earlier = reports[-1], reports[-2]
        # Finished work split in two: what its worker is on now stays its current task.
        current = self.store.value("SELECT task_id FROM sessions WHERE id=?", (task["session_id"],))
        made = self.tasks.create(
            task["session_id"], title, f"Split off “{task['title']}” ({task_id}).",
            status=task["status"],
        )  # fmt: skip
        self.store.execute(
            "UPDATE sessions SET task_id=? WHERE id=?", (current, task["session_id"])
        )
        self.tasks.start_run(made["id"], "split")
        made = self.tasks.move(made["id"], task["status"], result=latest["text"])
        # Its reports and messages since the earlier result are the new task's.
        for row in self.store.all(
            "SELECT id, meta FROM messages WHERE session_id=? AND id>? "
            "AND json_extract(meta, '$.task_id')=?",
            (task["session_id"], earlier["id"], task_id),
        ):
            meta = json.loads(row["meta"] or "{}")
            self.store.update_message_meta(row["id"], {**meta, "task_id": made["id"]})
        names = json.loads(self.store.get(f"titles:{made['id']}") or "[]")
        names.append({"title": f"split from “{task['title']}”", "at": now(), "by": by})
        self.store.set(f"titles:{made['id']}", json.dumps(names))
        self.tasks.update(task_id, result=earlier["text"])
        self.publish(self.tasks.get(task_id))
        self.runtime.publish_session(self.store.session(task["session_id"]))
        return self.view(self.tasks.get(made["id"]))

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
            # Answered after it was cancelled: the owner wants it after all. The note that it
            # was cancelled, not sent yet, is moot.
            mark = int(self.store.get(f"sent:{row['id']}") or 0)
            for message in self.store.messages(row["id"], after=mark):
                prompt = message["meta"].get("prompt", "")
                if 'type="cancelled"' in prompt and f'task="{task_id}"' in prompt:
                    kept = {k: v for k, v in message["meta"].items() if k != "prompt"}
                    self.store.update_message_meta(message["id"], {**kept, "dropped": True})
        if before in {"review", "done", "failed", "cancelled"}:
            # Cancelling this follow-up gives the finished result back.
            self.store.set(f"reopened:{task_id}", before)
            task = self.tasks.move(task_id, "running", reason="follow-up")
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
            if pr:
                label = f"{pr.group(1)}#{pr.group(2)}".lower()
                if self.creator(label) == task["id"] or names_pr(
                    asked, pr.group(1), int(pr.group(2))
                ):
                    found.append(key)
            elif task["status"] in OPEN and current and current["id"] == task["id"]:
                found.append(key)  # A Slack thread: the task it was on.
        return found

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
            # What it made or runs, as its worker last said: a short name and status each.
            "artifacts": json.loads(self.store.get(f"artifacts:{task['id']}") or "[]"),
        }

    def declare_artifacts(self, task_id: str, items: list[dict]):
        """Its worker says what the task made or runs and where each stands: kept by link, the
        latest word on each; a link said again moves to the top."""
        known = json.loads(self.store.get(f"artifacts:{task_id}") or "[]")
        stamp = now()
        for item in items:
            url = str(item.get("url") or "").strip()
            if not re.match(r"https?://\S+$", url) or threads.PR_URL.match(url):
                continue  # Not a link; a PR's status comes from GitHub.
            entry = {
                "url": url[:600],
                "name": " ".join(str(item.get("name") or "").split())[:60],
                "status": " ".join(str(item.get("status") or "").split())[:40],
                "at": stamp,
            }
            known = [other for other in known if other["url"] != url] + [entry]
        self.store.set(f"artifacts:{task_id}", json.dumps(known[-20:]))
        self.publish(self.tasks.get(task_id))

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
