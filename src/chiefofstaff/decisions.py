"""Decisions: what the owner decided that work depends on, and what happens when it changes.

- A worker that needs the owner to decide something (scope, a trade-off, a risky change) asks
  with ask_decision: a card saying why it comes up, the options and what each means, and what
  the worker recommends. Workers that hit the same question join that card; one answer goes to
  all of them.
- An answer, or a decision the Router records from what the owner said, is kept together with
  the tasks that rely on it.
- When the owner changes a decision, every task that relies on it hears about it: open work
  adjusts, finished results reopen and their evidence is marked out of date, and each worker
  confirms (ack_decision) that it now works to the new decision. A card shows the owner the
  impact until all of them have.
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import BaseModel, Field

from . import catalog
from .envelope import wrap
from .models import DraftDecision, NestedArguments
from .roles import ROUTER, WORKER
from .store import now
from .tasks import OPEN

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)


def same_words(one: str, other: str) -> bool:
    """The same decision, written again (case, spacing and final punctuation aside)."""

    def plain(text: str) -> str:
        return " ".join(text.lower().split()).rstrip(".!")

    return plain(one) == plain(other)


class DecisionOption(BaseModel):
    choice: str = Field(description="The option, in a few words")
    effect: str = Field(default="", description="What choosing it means: cost, risk, what changes")


class AskDecision(NestedArguments):
    question: str = Field(description="The decision the owner has to make, as one short question")
    why: str = Field(default="", description="Why it comes up now, from what you found (1-2 lines)")
    options: list[DecisionOption] = Field(default_factory=list, description="2-4 options")
    recommendation: str = Field(
        default="", description="The option you recommend (its choice) and why, in one line"
    )
    same_as: str = Field(
        default="",
        description="The id of an open decision card that asks the same thing: join it instead "
        "of asking again",
    )
    distinct: bool = Field(
        default=False,
        description="After ask_decision listed open decisions: true when yours is a different one",
    )
    seen_precedents: bool = Field(
        default=False,
        description="After ask_decision listed the owner's earlier decisions on similar questions: "
        "true when none of them settles yours",
    )


class RecordDecision(BaseModel):
    decision: str = Field(
        description="The decision in one line, as the owner made it, e.g. 'Login v1: email "
        "only, no SMS; the launch date stays'"
    )
    tasks: list[str] = Field(
        default_factory=list,
        description="Task ids (t…) or worker ids whose work relies on this decision",
    )
    replaces: list[str] = Field(
        default_factory=list,
        description="Ids (dec…) of earlier decisions this one changes, from "
        "list_sessions().decisions: their tasks are told and must confirm",
    )


class AckDecision(BaseModel):
    decision: str = Field(description="The decision id (dec…) from the message")
    plan: str = Field(description="What you change because of it, or why nothing has to change")
    changes: bool = Field(
        default=True,
        description="False when nothing in your work has to change: a finished task goes back "
        "to where it was (its result stands), with no new report needed",
    )


def decision_id() -> str:
    return "dec" + uuid4().hex[:6]


def clean(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


class Decisions:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.bus = runtime.bus
        self.tasks = runtime.tasks
        self.delivery = runtime.delivery

    # Reading

    def get(self, identifier: str) -> dict:
        row = self.store.one("SELECT * FROM decisions WHERE id=?", (identifier.strip(),))
        if not row:
            raise ValueError(f"No decision {identifier}; list_sessions().decisions lists them.")
        return row

    def active(self) -> list[dict]:
        """Decisions in force, newest first, with the tasks that rely on them."""
        return [
            {**row, "tasks": self.linked(row["id"])}
            for row in self.store.all(
                "SELECT * FROM decisions WHERE status='active' ORDER BY created_at DESC LIMIT 30"
            )
        ]

    def linked(self, identifier: str) -> list[str]:
        """The tasks that rely on a decision, oldest first."""
        return [
            row["task_id"]
            for row in self.store.all(
                """SELECT l.task_id FROM task_decisions l JOIN tasks t ON t.id=l.task_id
                WHERE l.decision_id=? ORDER BY t.created_at, t.id""",
                (identifier,),
            )
        ]

    def for_task(self, task_id: str) -> list[dict]:
        return self.store.all(
            """SELECT d.id, d.text, d.status, l.ack FROM task_decisions l
            JOIN decisions d ON d.id=l.decision_id WHERE l.task_id=? ORDER BY d.created_at""",
            (task_id,),
        )

    def similar(self, question: str, limit: int = 3) -> list[dict]:
        """The owner's decisions in force that are about something like this question."""
        rows = {
            row["id"]: row
            for row in self.store.all(
                "SELECT * FROM decisions WHERE status='active' ORDER BY created_at DESC LIMIT 200"
            )
        }
        docs = {identifier: {"title": row["text"]} for identifier, row in rows.items()}
        ranked = catalog.rank(question, docs)
        # One shared word is not the same question.
        return [rows[identifier] for identifier, terms in ranked if len(terms) >= 2][:limit]

    @staticmethod
    def recommended(target: dict) -> str:
        """The option the worker recommended, if its recommendation names one."""
        lowered = (target.get("recommendation") or "").lower()
        for option in target.get("options") or []:
            if option["choice"].lower() in lowered:
                return option["choice"]
        return ""

    def open_cards(self, kind: str) -> list[dict]:
        return [draft for draft in self.store.drafts() if draft["kind"] == kind]

    # Asking the owner

    async def t_ask_decision(self, params: AskDecision, caller: str):
        row = self.store.session(caller)
        if row["role"] != WORKER:
            raise ValueError("Only workers ask the owner for decisions.")
        question = clean(params.question, 300)
        if not question:
            raise ValueError("Say what the owner should decide.")
        task = self.tasks.current(caller)
        asker = {
            "session_id": caller,
            "task_id": task["id"] if task else "",
            "title": task["title"] if task else row["title"],
        }
        cards = self.open_cards("decision")
        precedents = [] if params.same_as.strip() else self.similar(question)
        if precedents and not params.seen_precedents:
            listed = "\n".join(f"- {p['text']} ({p['created_at'][:10]})" for p in precedents)
            raise ValueError(
                f"The owner decided similar questions before:\n{listed}\nIf one settles yours, "
                "follow it without asking and say so in your report, unless your task asks you "
                "to ask the owner this time (then ask anyway). Otherwise call ask_decision again "
                "with seen_precedents=true: the card shows these to the owner."
            )
        if params.same_as.strip():
            card = next((c for c in cards if c["id"] == params.same_as.strip()), None)
            if not card:
                raise ValueError(f"No open decision card {params.same_as}. Ask without same_as.")
            target = card["target"]
            if caller not in {a["session_id"] for a in target["askers"]}:
                target["askers"].append(asker)
                self.bus.publish("draft", self.store.update_draft_target(card["id"], target))
            self.runtime.add_message(
                caller, "notice", f"Waits for the owner's decision: {target['question']}"
            )
        else:
            others = [
                {
                    "id": card["id"],
                    "question": card["target"]["question"],
                    "asked_by": [a["title"] for a in card["target"]["askers"]],
                }
                for card in cards
                if caller not in {a["session_id"] for a in card["target"]["askers"]}
            ]
            if others and not params.distinct:
                raise ValueError(
                    "Other workers wait for these decisions. If yours is the same question, call "
                    "ask_decision with same_as=<its id>; otherwise call it again with "
                    "distinct=true.\n" + json.dumps(others, ensure_ascii=False)
                )
            options = [
                {"choice": clean(o.choice, 120), "effect": clean(o.effect, 300)}
                for o in params.options
                if o.choice.strip()
            ][:4]
            target = {
                "question": question,
                "why": clean(params.why, 600),
                "options": options,
                "recommendation": clean(params.recommendation, 300),
                "askers": [asker],
                "precedents": [p["text"] for p in precedents],
            }
            card = self.store.create_draft(caller, "decision", target, question)
            self.runtime.add_message(
                caller, "draft", question, {"draft_id": card["id"], "label": "Decision"}
            )
            self.bus.publish("draft", card)
            self.runtime.alert(
                question, session_id=caller, needs_action=True, title=f"Decision · {row['title']}"
            )
        if task and task["status"] in OPEN:
            self.tasks.move(task["id"], "waiting", progress=f"Needs your decision: {question}")
        self.runtime.update_session(caller, status="waiting")
        return (
            f'Asked the owner (card {card["id"]}). The decision arrives as a type="decision" '
            "message. End your turn now, unless parts of your task do not depend on it."
        )

    async def answer(self, card: dict, decision: DraftDecision) -> dict:
        """The owner decided (or declined to): every worker waiting for it hears."""
        target = card["target"]
        question = target["question"]
        if decision.action == "reject":
            reason = clean(decision.reason, 300)
            card = self.close(card["id"], "rejected", reason or "Not decided")
            if reason:
                self.runtime.learning.record(
                    "decision-declined",
                    context=f"{question} {target.get('why', '')}",
                    proposed=target.get("recommendation", ""),
                    owner=reason,
                    scope=self.scope_of(target),
                )
            text = (
                "The owner did not decide" + (f": {reason}" if reason else ".")
                + " Go on with your recommendation only if it is safe and easy to undo; "
                "otherwise report(status=\"blocked\")."
            )  # fmt: skip
            await self.tell_askers(target, text, "Not decided", "")
            return card
        if decision.action != "answer":
            raise ValueError("Decide, or say Not now.")
        choice = clean(decision.text, 500)
        if not choice:
            raise ValueError("Pick an option or write your decision.")
        tasks = [asker["task_id"] for asker in target["askers"] if asker["task_id"]]
        recorded = await self.record(f"{question} → {choice}", tasks, [], f"card:{card['id']}")
        best = self.recommended(target)
        self.runtime.learning.record(
            "decision",
            context=f"{question} {target.get('why', '')}",
            proposed=target.get("recommendation", ""),
            owner=choice,
            scope=self.scope_of(target),
            agreed=(choice.lower() == best.lower()) if best else None,
        )
        card = self.close(card["id"], "answered", choice)
        text = f"The owner decided: {choice}"
        await self.tell_askers(target, text, f"Decision: {choice}", recorded["decision"]["id"])
        return card

    async def tell_askers(self, target: dict, text: str, display: str, identifier: str):
        for asker in target["askers"]:
            row = self.store.find_session(asker["session_id"])
            if not row or row["closed_at"]:
                continue
            task = self.tasks.find(asker["task_id"]) if asker["task_id"] else None
            if task and task["status"] == "waiting":
                self.tasks.move(task["id"], "running", reason="decision")
            # Only the owner's own words go in the owner's envelope; the question was a worker's.
            asked = wrap("system", "context", f"Your decision card asked: {target['question']}")
            try:
                await self.delivery.deliver(
                    row["id"],
                    asked + "\n\n" + wrap("owner", "decision", text, decision=identifier or None),
                    kind="owner",
                    display=display,
                    meta={"task_id": task["id"]} if task else None,
                )
            except Exception:
                logger.warning("Could not tell %s the decision.", row["id"], exc_info=True)

    def scope_of(self, target: dict) -> str:
        askers = target.get("askers") or []
        return self.runtime.learning.scope_of(askers[0]["session_id"]) if askers else ""

    def close(self, card_id: str, status: str, outcome: str) -> dict:
        card = self.store.update_draft(card_id, status=status, outcome=outcome, resolved_at=now())
        self.bus.publish("draft", card)
        return card

    def drop_asker(self, session_id: str):
        """A closed worker no longer waits; a card nobody waits for ends."""
        for card in self.open_cards("decision"):
            askers = [a for a in card["target"]["askers"] if a["session_id"] != session_id]
            if len(askers) == len(card["target"]["askers"]):
                continue
            if askers:
                target = {**card["target"], "askers": askers}
                self.bus.publish("draft", self.store.update_draft_target(card["id"], target))
            else:
                self.close(card["id"], "expired", "Its worker was closed.")

    # Recording and changing

    async def t_record_decision(self, params: RecordDecision, caller: str):
        if self.store.session(caller)["role"] != ROUTER:
            raise ValueError("Only the Router records decisions.")
        tasks = [self.runtime.task_actions.task_for(ref)["id"] for ref in params.tasks]
        state = self.runtime.states[caller]
        if not params.replaces:
            # Tasks it gave out in this turn came with this decision in the owner's message.
            tasks = list(dict.fromkeys(tasks + state.turn_tasks))
        result = await self.record(params.decision, tasks, params.replaces, "owner")
        decision, affected = result["decision"], result["affected"]
        state.turn_decisions.append(decision["id"])
        if result.get("same"):
            return (
                f"Recorded {decision['id']} in the same words as before: nothing changed, no "
                f"worker was told. Linked to {len(tasks)} more task(s)."
            )
        if not params.replaces:
            return f"Recorded {decision['id']}, linked to {len(tasks)} task(s)."
        if not affected:
            return f"Recorded {decision['id']}. No open or finished task relied on what it changes."
        told = [item for item in affected if item["told"]]
        redo = sum(item["was"] in {"review", "done"} for item in told)
        missed = [item["title"] for item in affected if not item["told"]]
        text = f"Recorded {decision['id']}. It affects {len(affected)} task(s)"
        if redo:
            text += f"; {redo} checked result(s) are out of date and reopened"
        if told:
            text += f". {len(told)} worker(s) were told and must confirm"
        if missed:
            text += f". Could not tell: {', '.join(missed)} (their results are marked out of date)"
        return text + ". The owner sees the impact on a card. Tell the owner in one line."

    async def record(self, text: str, tasks: list[str], replaces: list[str], source: str) -> dict:
        text = clean(text, 500)
        if not text:
            raise ValueError("Say what was decided.")
        old = [self.get(identifier) for identifier in dict.fromkeys(replaces)]
        for row in old:
            if row["status"] != "active":
                raise ValueError(f"{row['id']} was already replaced by {row['replaced_by']}.")
        identifier = decision_id()
        self.store.execute(
            """INSERT INTO decisions(id, text, status, replaces, source, created_at)
            VALUES (?,?,?,?,?,?)""",
            (identifier, text, "active", json.dumps([r["id"] for r in old]), source, now()),
        )
        relying = list(dict.fromkeys(tasks + [t for row in old for t in self.linked(row["id"])]))
        for task_id in relying:
            self.store.execute(
                "INSERT OR IGNORE INTO task_decisions(task_id, decision_id) VALUES (?,?)",
                (task_id, identifier),
            )
        for row in old:
            self.store.execute(
                "UPDATE decisions SET status='replaced', replaced_by=? WHERE id=?",
                (identifier, row["id"]),
            )
        decision = self.get(identifier)
        affected = []
        if old and all(same_words(row["text"], text) for row in old):
            # Recorded again in the same words (to link more tasks, say): nothing changed, so
            # no worker is told to redo anything.
            for task_id in relying:
                self.runtime.task_actions.publish(self.tasks.get(task_id))
            return {"decision": decision, "affected": [], "same": True}
        if old and source == "owner":
            self.runtime.learning.record(
                "decision-changed",
                context="Before: " + "; ".join(row["text"] for row in old),
                owner=decision["text"],
            )
        if old:
            for task_id in relying:
                task = self.tasks.get(task_id)
                if task["status"] in {"failed", "cancelled"}:
                    continue
                affected.append(await self.propagate(task, old, decision))
            if affected:
                self.change_card(decision, old, affected)
        for task_id in relying:
            self.runtime.task_actions.publish(self.tasks.get(task_id))
        return {"decision": decision, "affected": affected}

    async def propagate(self, task: dict, old: list[dict], decision: dict) -> dict:
        """One task relied on a decision that changed: its worker adjusts and confirms."""
        was = task["status"]
        before = "; ".join(row["text"] for row in old)
        if was in {"review", "done"}:
            self.runtime.task_actions.stale(
                task["id"], f"Out of date: the decision “{before}” changed after it was checked."
            )
        row = self.store.session(task["session_id"])
        note = (
            f"The owner changed a decision that task {task['id']} “{task['title']}” relies on.\n"
            f"Before: {before}\nNow: {decision['text']}\n"
            "Stop work that relies on the old decision. Keep what still fits and change what "
            f'does not, then call ack_decision(decision="{decision["id"]}", plan=what you '
            "change, or why nothing has to). Check your Done when criteria again before you "
            "report done."
        )
        try:
            if row["closed_at"]:
                await self.runtime.workers.reopen(row["id"])
            await self.delivery.deliver(
                row["id"],
                wrap("router", "decision-changed", note, decision=decision["id"], task=task["id"]),
                kind="router",
                display=f"Decision changed: {decision['text']}",
                meta={"task_id": task["id"]},
                steer=True,
            )
        except Exception as exc:
            logger.warning("Could not tell %s about the change.", row["id"], exc_info=True)
            # Nothing will redo it: the card says so instead of waiting for a confirmation.
            error = str(exc)[:200] or type(exc).__name__
        else:
            error = ""
            if was in {"review", "done"}:
                # Remembered: if nothing has to change, it goes back there (ack_decision).
                self.store.set(f"decision_was:{task['id']}", was)
                self.tasks.move(task["id"], "running", reason="decision changed")
        return {
            "task_id": task["id"],
            "title": task["title"],
            "session_id": row["id"],
            "was": was,
            "told": not error,
            "error": error,
            "ack": "",
        }

    def change_card(self, decision: dict, old: list[dict], affected: list[dict]):
        target = {
            "decision": {"id": decision["id"], "text": decision["text"]},
            "before": [row["text"] for row in old],
            "tasks": affected,
        }
        card = self.store.create_draft(
            self.runtime.router_id(), "change", target, f"Decision changed: {decision['text']}"
        )
        self.bus.publish("draft", card)

    async def t_ack_decision(self, params: AckDecision, caller: str):
        row = self.store.session(caller)
        decision = self.get(params.decision)
        plan = clean(params.plan, 400)
        if not plan:
            raise ValueError("Say what you change, or why nothing has to.")
        mine = [
            link["task_id"]
            for link in self.store.all(
                """SELECT l.task_id FROM task_decisions l JOIN tasks t ON t.id=l.task_id
                WHERE l.decision_id=? AND t.session_id=?""",
                (decision["id"], caller),
            )
        ]
        if not mine:
            raise ValueError(f"None of your tasks relies on {decision['id']}.")
        for task_id in mine:
            self.store.execute(
                "UPDATE task_decisions SET ack=? WHERE task_id=? AND decision_id=?",
                (plan, task_id, decision["id"]),
            )
            was = self.store.get(f"decision_was:{task_id}")
            self.store.delete(f"decision_was:{task_id}")
            task = self.tasks.get(task_id)
            if was and not params.changes and task["status"] == "running":
                # Nothing changes: its result stands, as it was before the change.
                self.runtime.task_actions.unstale(task_id)
                self.tasks.move(task_id, was, progress=f"Unchanged by the new decision: {plan}")
        self.runtime.add_message(caller, "notice", f"Works to the new decision: {plan}")
        for card in self.open_cards("change"):
            target = card["target"]
            if target["decision"]["id"] != decision["id"]:
                continue
            for item in target["tasks"]:
                if item["task_id"] in mine:
                    item["ack"] = plan
            card = self.store.update_draft_target(card["id"], target)
            told = [item for item in target["tasks"] if item.get("told", True)]
            if told and all(item["ack"] for item in told) and len(told) == len(target["tasks"]):
                card = self.close(card["id"], "answered", "Every affected worker confirmed.")
            else:
                self.bus.publish("draft", card)
        with suppress(RuntimeError):
            self.runtime.add_message(
                self.runtime.router_id(),
                "notice",
                f"“{row['title']}” works to the new decision: {plan}",
                {"session_id": caller},
            )
        if not params.changes:
            return "Noted: your finished work stands as it was. Nothing more to do or report."
        return "Noted. Go on with the task under the new decision."

    def dismiss(self, card: dict) -> dict:
        return self.close(card["id"], "answered", "Seen.")
