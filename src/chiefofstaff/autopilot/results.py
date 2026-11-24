"""Autopilot accepts verified results and starts what waited on them (autopilot.md §5.1, B1, B3),
and nudges a stalled task once more (B9).

- **A result to accept** (a task in review, its receipts read, in scope): verified by Chief
  itself (verify.py), once per commit; accepted when that passes and its branch head is still
  that commit, as the owner's Accept moves it but learning nothing, never recording the head
  for *Merge when ready*, and marked *Accepted by Autopilot*. *Undo* puts it back to review.
  One that does not verify waits for the owner, saying why.
- **What waited on it** (a task whose dependency needs it accepted) starts, carrying the
  acceptance's origin.
- **A stalled task** (*No progress*: asked once where it stands, nothing since) is asked once
  more, then waits.

Each is one transaction: the change, its entry, and what to do after it in the outbox."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from ..digest import stalled
from ..workspaces import checkout_git
from . import verify
from .logbook import UNDONE

if TYPE_CHECKING:
    from .core import Autopilot

logger = logging.getLogger(__name__)


class Results:
    """The mixin of Autopilot that verifies, accepts and nudges."""

    async def check_results(self: Autopilot):  # type: ignore[misc]
        """Results waiting for Accept, in this run's scope: verified one at a time, at the lowest
        priority (never while the owner's work waits for a slot)."""
        ticket = self.ticket()
        if self.verifying or ticket is None:
            return
        workers = self.store.open_sessions("worker")
        if any(self.runtime.workers.wants_slot(row) for row in workers):
            return
        for task in self.store.all("SELECT * FROM tasks WHERE status='review' ORDER BY updated_at"):
            row = self.store.find_session(task["session_id"])
            if (
                not row
                or not self.takes(row)
                or task["id"] in self.runtime.task_actions.receipts_due
            ):
                continue
            version = self.result_version(task)
            if self.store.get(f"autopilot:tried:{task['id']}") == version:
                continue
            self.store.set(f"autopilot:tried:{task['id']}", version)
            self.verifying = task["id"]
            self.verify_job = self.runtime.background(self.verify_and_accept(task, ticket))
            return

    def result_version(self: Autopilot, task: dict) -> str:  # type: ignore[misc]
        """Which result this is: its latest run (a newer result is verified again)."""
        found = self.store.one(
            "SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (task["id"],)
        )
        return (found or {}).get("id") or task["updated_at"]

    async def verify_and_accept(self: Autopilot, task: dict, ticket: tuple[str, int]):  # type: ignore[misc]
        """Verified, then accepted or left, under the run it began in only if that still holds:
        else (Stop, the run's end) it stays in review, to verify in a later run."""
        filed = False
        try:
            outcome = await verify.verify(self.runtime, task, self.runner)
            if outcome["passed"] and not await self.head_holds(task, outcome["record"]["commit"]):
                outcome = {**outcome, "passed": False, "why": "its branch moved on since"}
            if outcome["passed"]:
                filed = self.accept_verified(task, outcome["record"], ticket)
            else:
                filed = self.not_accepted(task, outcome["why"], outcome["record"], ticket)
        except Exception:
            logger.warning("Autopilot could not verify %s.", task["id"], exc_info=True)
            filed = True
        finally:
            if not filed:
                self.store.delete(f"autopilot:tried:{task['id']}")  # (Cancelled too: Stop.)
            self.verifying = ""
            self.verify_job = None
            self.runtime.publish_state()

    async def head_holds(self: Autopilot, task: dict, commit: str) -> bool:  # type: ignore[misc]
        """Its branch's head is still the commit verified (re-read just before accepting)."""
        row = self.store.find_session(task["session_id"])
        for checkout in self.runtime.workspaces.checkouts(row) if row else []:
            if checkout.get("shared"):
                continue
            try:
                head = await checkout_git(checkout, checkout["path"], "rev-parse", "HEAD")
            except ValueError:
                return False
            return head.strip() == commit
        return False

    def accepted_entry(self: Autopilot, task: dict) -> str:  # type: ignore[misc]
        """Its acceptance's entry in the record, for a done task Autopilot accepted ("" when
        the owner did; never looked up where Autopilot was never used)."""
        if task.get("status") != "done" or not self.book.made:
            return ""
        return self.store.get(f"autopilot:accepted:{task['id']}") or ""

    def accept_verified(self: Autopilot, task: dict, record: dict,  # type: ignore[misc]
                        ticket: tuple[str, int]) -> bool:  # type: ignore[misc]  # fmt: skip
        """Accepted, if the run it was verified in still holds (else nothing: False)."""
        run = {"id": ticket[0]}
        tasks = self.runtime.tasks
        released = []
        with self.store.transaction():
            fresh = tasks.find(task["id"])
            if not fresh or fresh["status"] != "review":
                return True  # The owner accepted it (or sent it back) first.
            if not self.holds(ticket):
                return False
            self.store.delete(f"news:{task['id']}")
            self.store.delete(f"setaside:{task['id']}")
            tasks.move(task["id"], "done")
            entry = self.book.append(
                run.get("id", ""), "accepted",
                f"Accepted “{task['title']}”: its checks pass at {verify.short(record['commit'])}",
                subject=f"task:{task['id']}", task_id=task["id"],
                why={"rule": "verified", "verification": "Chief's own run, offline"},
                evidence=record, payload={"commit": record["commit"], "tree": record["tree"]},
            )  # fmt: skip
            self.store.set(f"autopilot:accepted:{task['id']}", entry["id"])
            origin = {"entries": [entry["id"]],
                      "ask_first": self.store.settings().autopilot_ask_first}  # fmt: skip
            for waiting in self.store.all(
                "SELECT t.* FROM task_deps d JOIN tasks t ON t.id=d.task_id WHERE "
                "d.depends_on=? AND d.needs='accepted' AND t.status='queued'",
                (task["id"],),
            ):
                started = self.book.append(
                    run.get("id", ""), "released", f"Started “{waiting['title']}”",
                    of=entry["id"], subject=f"task:{waiting['id']}", task_id=waiting["id"],
                    why={"rule": "dependency", "waited_on": task["id"]},
                )  # fmt: skip
                self.origins.keep(waiting["session_id"], {**origin, "entries": [started["id"]]})
                released.append(waiting["id"])
            self.book.queue(run.get("id", ""), entry["id"], "accepted",
                            {"task_id": task["id"], "released": released},
                            ticket[1])  # fmt: skip
        self.runtime.background(self.dispatch())
        return True

    def not_accepted(self: Autopilot, task: dict, why: str, record: dict,  # type: ignore[misc]
                     ticket: tuple[str, int]) -> bool:  # type: ignore[misc]  # fmt: skip
        """It waits for the owner's Accept, saying why (once per result), if the run it was
        verified in still holds (else nothing: False)."""
        run = {"id": ticket[0]}
        with self.store.transaction():
            if not self.holds(ticket):
                return False
            entry = self.book.append(
                run.get("id", ""), "not-done", f"Left “{task['title']}” for you: {why}",
                state="parked", subject=f"task:{task['id']}", task_id=task["id"],
                why={"rule": "verified", "reason": why}, evidence=record,
            )  # fmt: skip
            self.store.set(f"autopilot:setaside:{task['id']}",
                           json.dumps({"why": f"Not accepted for you: {why}",
                                       "entry": entry["id"]}))  # fmt: skip
        return True

    async def deliver_accepted(self: Autopilot, item: dict) -> str:  # type: ignore[misc]
        """After an acceptance commits: its task shown done, a line in its worker's chat (as
        Autopilot's, linking to its entry), what waited on it started."""
        actions = self.runtime.task_actions
        task = self.runtime.tasks.find(item["payload"]["task_id"])
        entry = self.book.entry(item["entry"])
        if task:
            actions.publish(task)
            if entry and task["session_id"]:
                commit = verify.short((entry["payload"] or {}).get("commit", ""))
                self.notice(task["session_id"], task, entry,
                            f"Autopilot accepted it: its checks pass at {commit}.")  # fmt: skip
        await self.runtime.workers.pump()
        return ""

    def undo(self: Autopilot, entry_id: str) -> str:  # type: ignore[misc]
        """Undo of an acceptance: back to review (what it started goes on); re-checked first."""
        entry = self.book.entry(entry_id)
        if not entry or entry["kind"] != "accepted":
            raise ValueError("Only an acceptance can be undone here.")
        if self.book.entries(of=entry_id, kinds=(UNDONE,)):
            raise ValueError("It was undone already.")
        tasks = self.runtime.tasks
        with self.store.transaction():
            task = tasks.find(entry["task_id"])
            if not task or task["status"] != "done":
                raise ValueError("It moved on since: there's nothing to undo.")
            tasks.move(task["id"], "review")
            self.store.set(f"autopilot:tried:{task['id']}", self.result_version(task))
            self.store.delete(f"autopilot:accepted:{task['id']}")
            self.book.append(
                entry["run_id"], UNDONE, f"Undone: “{task['title']}” waits for your Accept",
                state=UNDONE, of=entry_id, subject=entry["subject"], task_id=task["id"],
                why={"rule": "owner", "by": "you"},
            )  # fmt: skip
        self.runtime.task_actions.publish(tasks.get(task["id"]))
        self.runtime.publish_state()
        return f"“{task['title']}” waits for your Accept again."

    # B9: one more nudge.

    async def check_stalled(self: Autopilot, moment: datetime | None = None):  # type: ignore[misc]
        run = self.run()
        if run is None or self.paused():
            return
        moment = moment or datetime.now().astimezone()
        for task in self.store.all("SELECT * FROM tasks WHERE status='running'"):
            row = self.store.find_session(task["session_id"])
            if not row or not self.takes(row) or not stalled(self.runtime, task, moment):
                continue
            asked = self.store.get(f"stallnudge:{task['id']}") or ""
            if self.store.get(f"autopilot:nudged:{task['id']}"):
                continue  # Once: then it waits for the owner.
            text = (
                f"Autopilot asks again, for the owner (they are away), where task {task['id']} "
                f"“{task['title']}” stands: nothing has happened on it since it was asked. Go on "
                "with it now; or report it (done, or a question); or, if it waits for something "
                "outside, check_back when to look again."
            )
            with self.store.transaction():
                entry = self.book.append(
                    run["id"], "nudged", f"Asked “{task['title']}” where it stands, once more",
                    subject=f"task:{task['id']}", task_id=task["id"],
                    why={"rule": "stalled", "asked_before": asked},
                )  # fmt: skip
                self.store.set(f"autopilot:nudged:{task['id']}", asked or entry["at"])
                self.book.queue(run["id"], entry["id"], "worker", {
                    "session_id": row["id"], "task_id": task["id"], "text": text,
                    "display": "Autopilot asked where it stands.",
                }, self.book.generation())  # fmt: skip
        self.runtime.background(self.dispatch())  # (Not awaited: the periodic round goes on.)
