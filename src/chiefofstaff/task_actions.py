"""What happens to tasks: reports, cancellation, review, retries and follow-ups, and the
receipts and shareable report of finished work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from . import receipts, threads
from .envelope import wrap
from .roles import WORKER
from .store import now
from .tasks import ENDED, OPEN
from .workspaces import base_of, changes, diff, git, same_version, version

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

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
            """SELECT c.command, c.kind, c.exit_code FROM commands c JOIN runs r ON r.id=c.run_id
            WHERE r.task_id=? ORDER BY c.id""",
            (task["id"],),
        )
        # Claims are saved with the report; what was observed follows from git and gh.
        latest = self.store.one(
            """SELECT evidence, criteria FROM runs WHERE task_id=?
            AND (evidence!='' OR criteria!='') ORDER BY started_at DESC""",
            (task["id"],),
        )
        evidence = json.loads(latest["evidence"]) if latest and latest["evidence"] else None
        criteria = self.tasks.criteria(task)
        if not commands and not evidence and not criteria:
            return None
        claims = json.loads(latest["criteria"]) if latest and latest["criteria"] else []
        return receipts.summarize(
            commands, evidence, task["result"] or task["progress"], criteria, claims
        )

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
        prs = []
        for repo, number in threads.PR_URL.findall(" ".join(self.pr_urls(task)))[:5]:
            try:
                view = json.loads(
                    await self.runtime.github.gh(
                        "pr",
                        "view",
                        f"https://github.com/{repo}/pull/{number}",
                        "--json",
                        "number,url,state,statusCheckRollup,reviewDecision",
                    )
                )
            except Exception:
                logger.info("Could not read %s#%s for receipts.", repo, number, exc_info=True)
                continue
            prs.append(
                {
                    "label": f"{repo}#{number}",
                    "url": view.get("url") or f"https://github.com/{repo}/pull/{number}",
                    "state": view.get("state", ""),
                    "checks": receipts.checks_state(view.get("statusCheckRollup") or []),
                    "review": view.get("reviewDecision") or "",
                }
            )
        run = self.store.one(
            "SELECT id, evidence FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
            (task_id,),
        )
        if run:
            evidence = {"checkouts": checkouts, "prs": prs, "at": now()}
            before = json.loads(run["evidence"]) if keep_stale and run["evidence"] else {}
            if reason := before.get("stale") or moved(before.get("checkouts", []), checkouts):
                # The checks were about the code as it was then, not as it is now.
                evidence |= {"checkouts": before["checkouts"], "stale": reason}
            self.store.execute(
                "UPDATE runs SET evidence=? WHERE id=?", (json.dumps(evidence), run["id"])
            )
            self.publish(self.tasks.get(task_id))

    def pr_urls(self, task: dict) -> list[str]:
        """The task's PRs: those opened with Create PR, and those its report links to."""
        opened = json.loads(self.store.get(f"prs:{task['id']}") or "[]")
        cited = [
            f"https://github.com/{repo}/pull/{number}"
            for repo, number in threads.PR_URL.findall(task["result"] or "")
        ]
        return list(dict.fromkeys(opened + cited))

    def pr_merged(self, session_id: str, thread: str):
        """A worker's PR was merged: the task it delivers is done."""
        for task in self.tasks.for_session(session_id):
            if task["status"] == "review" and thread in threads.pull_requests(
                " ".join(self.pr_urls(task))
            ):
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
                plans.append(await self.pr_plan(checkout))
            plans = [plan for plan in plans if plan]
            if not plans:
                raise ValueError("Nothing to open a PR from: the worker committed nothing.")
            for repo, slug, branch in plans:
                await git(
                    "-C", repo, "push", "--quiet", "origin",
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
        known = json.loads(self.store.get(f"prs:{task_id}") or "[]")
        self.store.set(f"prs:{task_id}", json.dumps(list(dict.fromkeys(known + opened))))
        if not row["closed_at"]:
            # Its reviews, comments and CI come to the worker; merged, the task is done.
            followed = self.runtime.follow(row["id"], threads.pull_requests(" ".join(opened)))
            note = (
                f"The owner opened {', '.join(opened)} from your branch. You follow it: its "
                "reviews, comments and CI results come to you."
            )
            self.runtime.add_message(
                row["id"],
                "notice",
                f"PR opened: {', '.join(opened)}",
                {"prompt": wrap("system", "context", note)} if followed else {},
            )
        await self.collect_evidence(task_id, keep_stale=True)
        return self.tasks.get(task_id)

    async def pr_plan(self, checkout: dict) -> tuple[str, str, str] | None:
        """(repository, GitHub owner/name, branch) to open a PR from, or None: no commits."""
        repo, branch = checkout["repo"], checkout["branch"]
        name = Path(repo).name
        tree = checkout.get("worktree") or checkout["path"]
        live = await asyncio.to_thread(os.path.isdir, tree)
        if live and (await git("-C", tree, "status", "--porcelain")).strip():
            raise ValueError(
                f"{name} has uncommitted changes. Follow up and ask the worker to commit "
                "them, then create the PR."
            )
        commits = await git(
            "-C", repo, "rev-list", "--count", f"{base_of(checkout)}..refs/heads/{branch}"
        )
        if not int(commits.strip() or 0):
            return None
        try:
            remote = (await git("-C", repo, "config", "--get", "remote.origin.url")).strip()
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
                mark = "passed" if item["passed"] else f"failed (exit {item['exit_code']})"
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
        task = self.tasks.current(session_id)
        if task and task["status"] in OPEN:
            self.tasks.move(task["id"], "failed", progress=message[:500])

    def report_task(self, caller: str, reference: str) -> dict | None:
        if not reference:
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
        task = self.tasks.move(task_id, "cancelled", progress=why)
        if row and not row["closed_at"]:
            if on_it:
                self.runtime.asks.cancel_requests(row["id"], why)
                if handle := self.states.handle(row["id"]):
                    with suppress(Exception):
                        await handle.abort()  # Copilot also stops the running command.
                self.states[row["id"]].busy = False
                if row["status"] in {"working", "queued", "starting", "waiting"}:
                    self.runtime.update_session(row["id"], status="idle", activity="")
            note = f"Task {task_id} “{task['title']}” was cancelled. Stop work on it; no report."
            # Pending, not sent: it reaches the worker with its next message, not as a new turn.
            self.runtime.add_message(
                row["id"],
                "notice",
                why,
                {"prompt": wrap("system", "cancelled", note, task=task_id), "stopped": True},
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

    def accept(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if task["status"] != "review":
            raise ValueError(f"Task {task_id} is {task['status']}, not ready for review.")
        task = self.tasks.move(task_id, "done")
        self.runtime.background(self.runtime.workers.pump())  # Tasks waiting for its check.
        scope = self.runtime.learning.scope_of(task["session_id"])
        self.runtime.learning.record("accept", context=task["title"], scope=scope, agreed=True)
        return task

    async def follow_up(self, task_id: str, text: str) -> dict:
        """The owner adds to a task: its worker continues it (a finished task reopens)."""
        task = self.tasks.get(task_id)
        if task["status"] == "cancelled":
            raise ValueError("That task was cancelled. Ask the Router for new work instead.")
        row = self.store.session(task["session_id"])
        if row["closed_at"]:
            await self.runtime.workers.reopen(row["id"])
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
        if before in {"review", "done", "failed"}:
            task = self.tasks.move(task_id, "running", reason="follow-up")
        try:
            await self.delivery.deliver(
                row["id"],
                wrap("owner", "message", text, task=task_id),
                kind="owner",
                display=text,
                meta={"task_id": task_id},
                steer=True,
            )
        except Exception:
            if task["status"] != before:
                self.tasks.move(task_id, before)  # The follow-up never reached its worker.
            raise
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
        task = self.tasks.move(task_id, "running", reason="retry", progress="")
        body = f"The owner asks you to try task {task_id} “{task['title']}” again."
        if text.strip():
            body += f"\n\n{text.strip()}"
        try:
            await self.delivery.deliver(
                row["id"],
                wrap("owner", "message", body, task=task_id),
                kind="owner",
                display=text.strip() or "Try again.",
                meta={"task_id": task_id},
            )
        except Exception:
            self.tasks.move(task_id, before["status"], progress=before["progress"])
            raise
        return task

    def view(self, task: dict) -> dict:
        row = self.store.find_session(task["session_id"]) or {}
        return {
            **task,
            "worker": row.get("title", ""),
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
        }

    def publish(self, task: dict):
        self.bus.publish("task", self.view(task))
        # Whether a task is blocked depends on the tasks it waits for.
        for dependent in self.tasks.dependents(task["id"]):
            if dependent["status"] in OPEN:
                self.bus.publish("task", self.view(dependent))
