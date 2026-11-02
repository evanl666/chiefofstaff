"""What happens to tasks: reports, cancellation, review, retries and follow-ups, and the
receipts and shareable report of finished work.
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from . import receipts, threads
from .envelope import wrap
from .roles import WORKER
from .store import now
from .tasks import ENDED, OPEN
from .workspaces import changes

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

PROJECT_URL = "https://github.com/evanl666/chiefofstaff"


STATUS_WORDS = {
    "queued": "Queued",
    "running": "In progress",
    "waiting": "Waiting for an answer",
    "review": "Done, ready for review",
    "done": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
}


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

    def receipts(self, task: dict) -> dict | None:
        """Observed commands, changes and PRs for a task, beside its report."""
        commands = self.store.all(
            """SELECT c.command, c.kind, c.exit_code FROM commands c JOIN runs r ON r.id=c.run_id
            WHERE r.task_id=? ORDER BY c.id""",
            (task["id"],),
        )
        latest = self.store.one(
            "SELECT evidence FROM runs WHERE task_id=? AND evidence!='' ORDER BY started_at DESC",
            (task["id"],),
        )
        evidence = json.loads(latest["evidence"]) if latest else None
        if not commands and not evidence:
            return None
        return receipts.summarize(commands, evidence, task["result"] or task["progress"])

    async def collect_evidence(self, task_id: str):
        """After a task is reported done: its checkouts' changes and its PRs' state."""
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
        for repo, number in list(dict.fromkeys(threads.PR_URL.findall(task["result"])))[:5]:
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
            "SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (task_id,)
        )
        if run:
            evidence = {"checkouts": checkouts, "prs": prs, "at": now()}
            self.store.execute(
                "UPDATE runs SET evidence=? WHERE id=?", (json.dumps(evidence), run["id"])
            )
            self.publish(self.tasks.get(task_id))

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
        observed += [f"- ⚠ {flag}" for flag in found.get("flags", [])]
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
        return self.tasks.move(task_id, "done")

    async def follow_up(self, task_id: str, text: str) -> dict:
        """The owner adds to a task: its worker continues it (a finished task reopens)."""
        task = self.tasks.get(task_id)
        if task["status"] == "cancelled":
            raise ValueError("That task was cancelled. Ask the Router for new work instead.")
        row = self.store.session(task["session_id"])
        if row["closed_at"]:
            await self.runtime.workers.reopen(row["id"])
        before = task["status"]
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
        }

    def publish(self, task: dict):
        self.bus.publish("task", self.view(task))
        # Whether a task is blocked depends on the tasks it waits for.
        for dependent in self.tasks.dependents(task["id"]):
            if dependent["status"] in OPEN:
                self.bus.publish("task", self.view(dependent))
