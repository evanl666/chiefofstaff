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
import re
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import BaseModel, Field

from .asks import asks_back, clock
from .envelope import wrap
from .ledger import related, same_value
from .models import DraftDecision, NestedArguments
from .roles import ROUTER, WORKER
from .store import hours_ago, now
from .tasks import OPEN

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

# How a decision card ends that nobody waits on once its worker closed.
CLOSED = "Its worker was closed."
# Changes of one decision this close together are one change card (E154).
CHANGE_DEBOUNCE_HOURS = 2
# Quotes and brackets around an option's words, as the owner may type them or not.
QUOTES = re.compile("[\"'`\u2018\u2019\u201c\u201d\u300c\u300d\u300e\u300f()\\[\\]]")


# Options any choice may offer: two of them in common say nothing.
TRIVIAL = frozenset({"", "yes", "no", "ok", "skip", "none", "other", "neither", "both", "later"})


def same_key(text: str) -> str:
    """An option as compared: case, spacing, quotes and final punctuation aside."""
    return " ".join(QUOTES.sub(" ", text.casefold()).split()).rstrip(".!?\u3002\uff01\uff1f")


def same_words(one: str, other: str) -> bool:
    """The same decision, written again (case, spacing and final punctuation aside)."""

    def plain(text: str) -> str:
        return " ".join(text.lower().split()).rstrip(".!?\u3002\uff01\uff1f")

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
    replaces: str = Field(
        default="",
        description="The id (dec…) of a decision in force that what you found since calls into "
        "question: the owner reconsiders it, and their answer replaces it. Say in why what "
        "changed",
    )
    task_id: str = Field(
        default="",
        description="The task (t…) the decision is for, when you have several; by default the "
        "one you are working on",
    )


class RecordDecision(BaseModel):
    decision: str = Field(
        description="The decision in one line, as the owner made it, e.g. 'Login v1: email "
        "only, no SMS; the launch date stays'"
    )
    tasks: list[str] = Field(
        default_factory=list,
        description="Task ids (t…) or worker ids whose work already given relies on this "
        "decision. Not the work you are about to give: tasks you start in this turn rely on it "
        "by themselves",
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


def value(text: str) -> str:
    """What a decision settles on: its answer ("Q → A"), or all of it when it has none."""
    return text.partition(" \u2192 ")[2] or text


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

    def current(self, identifier: str) -> dict:
        """A decision as it stands now: the one that replaced it (and so on), if one did."""
        found = self.get(identifier)
        seen = {found["id"]}
        while found["replaced_by"] and found["replaced_by"] not in seen:
            found = self.get(found["replaced_by"])
            seen.add(found["id"])
        return found

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

    def waits_on(self, task_id: str) -> bool:
        """Whether a task waits on an open decision card."""
        return any(
            asker.get("task_id") == task_id
            for card in self.open_cards("decision")
            for asker in card["target"]["askers"]
        )

    # Asking the owner

    async def t_ask_decision(self, params: AskDecision, caller: str):
        row = self.store.session(caller)
        if row["role"] != WORKER:
            raise ValueError("Only workers ask the owner for decisions.")
        question = clean(params.question, 300)
        if not question:
            raise ValueError("Say what the owner should decide.")
        if params.task_id.strip():
            task = self.tasks.find(params.task_id.strip())
            if not task or task["session_id"] != caller:
                raise ValueError(f"You have no task {params.task_id.strip()}.")
        else:
            # The task it is at work on (bug E101: not the one given last, which may wait).
            task = self.runtime.at_work(caller)
        asker = {
            "session_id": caller,
            "task_id": task["id"] if task else "",
            "title": task["title"] if task else row["title"],
        }
        cards = self.open_cards("decision")
        reconsidered = self.reconsidered(params, task)
        if not reconsidered and (said := self.runtime.asks.held_ask(caller, task)):
            # What the owner (or the Router) sent about this task is unread: no card built
            # without it. Only a reconsideration of their answer is asked as such.
            return said
        # Its own open cards on this task.
        mine = [
            card
            for card in cards
            if any(self.asker_of(a, caller, task) for a in card["target"]["askers"])
        ]
        if (own := params.same_as.strip()) and (
            card := next((c for c in mine if c["id"] == own), None)
        ):
            # What it found since, on the card it asked (E155): the same card, updated.
            return self.update_card(card, params, question, caller)
        ledger = self.runtime.ledger
        anchor = ledger.anchor(caller, task["id"] if task else "")
        # The subject it asks about (E159: Chief of Staff tells which asks are one choice, not
        # the asking model): joined within its request, decided already, or a card of its own.
        subject: dict | None = None
        found: list[dict] = []
        linked = {d["id"] for d in self.for_task(task["id"])} if task else set()
        if not (params.same_as.strip() or reconsidered):
            same = [card for card in mine if same_words(question, card["target"]["question"])]
            if same:
                return (
                    f"Asked already: card {same[0]['id']} waits for the owner's decision. It "
                    'arrives as a type="decision" message. End your turn now, unless parts of '
                    "your task do not depend on it."
                )
            joined = [c for c in mine if any(a.get("joined") for a in c["target"]["askers"])]
            if mine and not params.distinct:
                # Asked again in other words while its card waits (never a second card of its
                # own for it).
                return (
                    f"Asked already: your card {mine[0]['id']} (“{mine[0]['target']['question']}”) "
                    "waits for the owner's decision on this task. To add what you found since, "
                    f'call ask_decision with same_as="{mine[0]["id"]}" and the question, why and '
                    "options as they stand now: that card is updated. Only if yours is another "
                    "decision, ask it with distinct=true."
                )
            if joined:
                # Another decision after all: off the card it was joined to.
                for card in joined:
                    ledger.drop("decision", card["id"], caller)
                self.drop_askers(
                    lambda a: bool(a.get("joined")) and self.asker_of(a, caller, task),
                    "expired",
                    CLOSED,
                )
            found = [m for m in ledger.match(question, anchor) if m["level"] == "same"]
            decision = next(
                (
                    d
                    for match in found
                    if (d := ledger.decided(match["subject"]))
                    and (match["joins"] or d["id"] in linked)
                ),
                None,
            ) or next(
                (
                    self.current(d["id"])
                    for d in reversed(self.for_task(task["id"]) if task else [])
                    if d["status"] == "active"
                    and (
                        related(question, d["text"].partition(" \u2192 ")[0])
                        or self.same_options(d, params.options)
                    )
                ),
                None,
            )
            if decision and not self.shown(caller, task, [decision], params.distinct):
                # A choice the owner made already (its request's, or one its task relies on
                # in other words): asked again, it gets their decision (owner report 0.1.57,
                # R1). Not another question on that task (owner report 0.1.58). A new card
                # is a reconsideration that says why, or another decision it says is.
                return (
                    f"The owner decided this already ({clock(decision['created_at'])}): "
                    f"{decision['text']} ({decision['id']}). Go on with it: do not ask "
                    "again. If what you found since calls it into question, ask again with "
                    f'replaces="{decision["id"]}" and say in why what changed: the owner '
                    "reconsiders it. Only if yours is another decision that this does not "
                    "settle, ask it again with distinct=true."
                )
            if not params.distinct:
                subject = next((m["subject"] for m in found if m["joins"]), None)
            if subject and (
                card := next(
                    (
                        c
                        for c in cards
                        if (ledger.subject_of("decision", c["id"]) or {}).get("id") == subject["id"]
                    ),
                    None,
                )
            ):
                # The same choice another asker's card waits on: one card, one answer.
                params = params.model_copy(update={"same_as": card["id"]})
        in_force = [
            self.get(d["id"])
            for d in reversed(self.for_task(task["id"]) if task else [])
            if d["status"] == "active" and not (subject and ledger.decided(subject) == d)
        ]
        precedents = [] if params.same_as.strip() or reconsidered else in_force
        if reconsidered and (
            joined_card := next(
                (c for c in cards if c["target"].get("replaces") == reconsidered["id"]), None
            )
        ):
            # Another worker asks the owner to reconsider it already: one card for it.
            params = params.model_copy(update={"same_as": joined_card["id"]})
        if reconsidered:
            subject = ledger.of_decision(reconsidered)
        if params.same_as.strip():
            card = next((c for c in cards if c["id"] == params.same_as.strip()), None)
            if not card:
                raise ValueError(f"No open decision card {params.same_as}. Ask without same_as.")
            target = card["target"]
            auto = not reconsidered and subject is not None and card not in mine
            if caller not in {a["session_id"] for a in target["askers"]}:
                target["askers"].append({**asker, **({"joined": True} if auto else {})})
                self.bus.publish("draft", self.store.update_draft_target(card["id"], target))
            ledger.file(
                "decision",
                card["id"],
                caller,
                asker["task_id"],
                question,
                options=[o["choice"] for o in target.get("options") or []],
                subject=(ledger.subject_of("decision", card["id"]) or {}).get("id", ""),
            )
            self.runtime.add_message(
                caller, "notice", f"Waits for the owner's decision: {target['question']}"
            )
            if auto:
                others = ", ".join(
                    f"“{a['title']}”" for a in target["askers"] if a["session_id"] != caller
                )
                if task and task["status"] in OPEN:
                    self.tasks.move(
                        task["id"], "waiting", progress=f"Needs your decision: {target['question']}"
                    )
                self.runtime.update_session(caller, status="waiting")
                return (
                    f"Asked already: {others or 'another worker'} asked the owner the same "
                    f"(card {card['id']}: “{target['question']}”). You wait on that card now: "
                    'one answer reaches you both, as a type="decision" message. End your turn '
                    "now, unless parts of your task do not depend on it. Only if yours is "
                    "another decision, ask again with distinct=true and say in why how it "
                    "differs."
                )
        else:
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
            if reconsidered:
                # What it reconsiders, said on its card (why: what changed since).
                target |= {
                    "replaces": reconsidered["id"],
                    "previous": reconsidered["text"],
                    "previous_at": reconsidered["created_at"],
                }
            card = self.store.create_draft(caller, "decision", target, question)
            ledger.file(
                "decision",
                card["id"],
                caller,
                asker["task_id"],
                question,
                options=[o["choice"] for o in options],
                subject=subject["id"] if subject else "",
                join=False,
            )
            self.runtime.add_message(
                caller, "draft", question, {"draft_id": card["id"], "label": "Decision"}
            )
            self.bus.publish("draft", card)
            self.runtime.alert(
                question,
                session_id=caller,
                needs_action=True,
                title=f"Decision · {row['title']}",
                draft_id=card["id"],
            )
        if task and task["status"] in OPEN:
            self.tasks.move(task["id"], "waiting", progress=f"Needs your decision: {question}")
        self.runtime.update_session(caller, status="waiting")
        # Decided elsewhere in the same words (another request's): the card says so, for the
        # owner to answer it with that decision in one click (Merge), never by itself (E159).
        before = [
            d
            for match in found
            if not match["joins"] and (d := self.runtime.ledger.decided(match["subject"]))
        ][:2]
        said = "".join(
            f" The owner decided something like it before ({clock(d['created_at'])}): "
            f"“{d['text']}” ({d['id']}): the card shows it to them."
            for d in before
        )
        return (
            f'Asked the owner (card {card["id"]}). The decision arrives as a type="decision" '
            f"message. End your turn now, unless parts of your task do not depend on it.{said}"
        )

    def same_options(self, decision: dict, options: list[DecisionOption]) -> bool:
        """Whether a question offers the choices a decision its task relies on was made among
        (live D2: "Which file format does the export use?" with CSV and JSON, after "CSV or
        JSON?"): two of them or more, not counting yes, no and the like."""
        asked = {plain for o in options if (plain := same_key(o.choice)) not in TRIVIAL}
        if len(asked) < 2:
            return False
        decided = {same_key(value(decision["text"]))}
        subject = self.runtime.ledger.of_decision(decision)
        for row in self.store.all(
            "SELECT options FROM asks WHERE subject_id=? AND options!=''",
            (subject["id"] if subject else "",),
        ):
            decided |= {same_key(choice) for choice in json.loads(row["options"])}
        return len(asked & (decided - TRIVIAL)) >= 2

    def shown(self, caller: str, task: dict | None, decisions: list[dict], distinct: bool) -> bool:
        """Whether it was shown these decisions its task relies on and says its question is
        another one (distinct); shown them now, if not."""
        key = f"decisionsshown:{caller}:{task['id'] if task else '-'}"
        seen = set(json.loads(self.store.get(key) or "[]"))
        ids = {d["id"] for d in decisions}
        if distinct and ids <= seen:
            return True
        self.store.set(key, json.dumps(sorted(seen | ids)))
        return False

    def update_card(self, card: dict, params: AskDecision, question: str, caller: str) -> str:
        """Its own open card, asked again with what it found since (E155): the same card, its
        question, why, options and recommendation as they stand now, stamped updated."""
        target = dict(card["target"])
        options = [
            {"choice": clean(o.choice, 120), "effect": clean(o.effect, 300)}
            for o in params.options
            if o.choice.strip()
        ][:4]
        changed = {
            "question": question,
            "why": clean(params.why, 600),
            "options": options,
            "recommendation": clean(params.recommendation, 300),
        }
        target |= {key: said for key, said in changed.items() if said}
        if target == card["target"]:
            return (
                f"Card {card['id']} asks that already: it waits for the owner's decision. End "
                "your turn now, unless parts of your task do not depend on it."
            )
        target["updated_at"] = now()
        card = self.store.update_draft_target(card["id"], target)
        self.bus.publish("draft", card)
        for asker in target["askers"]:
            if asker["session_id"] == caller:
                self.runtime.ledger.file(
                    "decision",
                    card["id"],
                    asker["session_id"],
                    asker.get("task_id") or "",
                    target["question"],
                    options=[o["choice"] for o in target.get("options") or []],
                )
        return (
            f"Updated card {card['id']}: the owner sees what you found since, on the card they "
            'have. The decision arrives as a type="decision" message. End your turn now, '
            "unless parts of your task do not depend on it."
        )

    def reconsidered(self, params: AskDecision, task: dict | None) -> dict | None:
        """The decision in force a question asks the owner to reconsider (as it stands now),
        with why: what changed since."""
        if not params.replaces.strip():
            return None
        if params.same_as.strip():
            raise ValueError("A reconsideration is a card of its own: ask without same_as.")
        try:
            found = self.current(params.replaces)
        except ValueError:
            raise ValueError(
                f"No decision {params.replaces.strip()}: replaces names one ask_decision listed "
                "(dec…)."
            ) from None
        if not params.why.strip():
            raise ValueError(
                f"Say in why what changed since the owner decided “{found['text']}”: the card "
                "shows it, for them to reconsider it."
            )
        return found

    async def answer(self, card: dict, decision: DraftDecision) -> dict:
        """The owner decided (or declined to, or kept the decision a reconsideration asks them
        to change): every worker waiting for it hears."""
        target = card["target"]
        question = target["question"]
        kept = self.current(target["replaces"]) if target.get("replaces") else None
        if decision.action == "choose" and decision.choice == "keep":
            if not kept:
                raise ValueError("This card asks a new decision: there is none to keep.")
            return await self.keep(card, kept)
        if decision.action == "reject" and kept:
            # Not now on a reconsideration keeps the decision in force for now: its
            # recommendation is the change (before, "go on with your recommendation" made it).
            return await self.keep(card, kept, clean(decision.reason, 300), later=True)
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
            await self.tell_askers(card, text, "Not decided", "", "Not decided by you")
            await self.settled(card, reason, declined=True)
            return card
        if decision.action != "answer":
            raise ValueError("Decide, or say Not now.")
        choice = clean(decision.text, 500)
        if not choice:
            raise ValueError("Pick an option or write your decision.")
        if kept and target.get("options") and not self.named(target, choice):
            # A reconsideration answered in words that pick none of its options (E154: "try
            # more variants first" replaced the decision in force and reopened six finished
            # tasks): the owner's note to the workers that asked; what was decided stays. On a
            # first card, their words are their decision.
            return await self.note(card, kept, choice)
        tasks = [asker["task_id"] for asker in target["askers"] if asker["task_id"]]
        replaces = [self.current(target["replaces"])["id"]] if target.get("replaces") else []
        recorded = await self.record(
            f"{question} → {choice}", tasks, replaces, f"card:{card['id']}"
        )
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
        short = choice if len(choice) <= 80 else choice[:79].rstrip() + "…"
        await self.tell_askers(
            card, text, f"Decision: {choice}", recorded["decision"]["id"], f"Decided: {short}"
        )
        await self.settled(card, choice, recorded["decision"])
        return card

    async def settled(
        self,
        card: dict,
        answer: str,
        decision: dict | None = None,
        note: bool = False,
        declined: bool = False,
    ):
        """The owner answered a card: the other asks on its subject (other workers' questions,
        the Router's note) are answered with it (E151)."""
        try:
            await self.runtime.ledger.settle(
                "decision", card["id"], answer, decision=decision, note=note, declined=declined
            )
        except Exception:
            logger.warning("Could not pass the answer to %s on.", card["id"], exc_info=True)

    async def keep(self, card: dict, kept: dict, reason: str = "", later: bool = False) -> dict:
        """The owner keeps the decision a reconsideration asked them to change (Keep; Not now,
        `later`: for now): it stays in force, and the workers that asked go on with it."""
        answer = kept["text"].partition(" \u2192 ")[2] or kept["text"]
        short = answer if len(answer) <= 80 else answer[:79].rstrip() + "\u2026"
        outcome = f"{'Kept for now' if later else 'Kept'}: {short}"
        card = self.close(card["id"], "rejected" if later else "answered", outcome)
        if reason:
            target = card["target"]
            self.runtime.learning.record(
                "decision-declined",
                context=f"{target['question']} {target.get('why', '')}",
                proposed=target.get("recommendation", ""),
                owner=reason,
                scope=self.scope_of(target),
            )
        text = (
            f"The owner keeps their decision{' for now' if later else ''}: {kept['text']}. Go on "
            "with it, and do not change it" + (f". They said: {reason}" if reason else ".")
        )
        await self.tell_askers(card, text, f"Kept: {short}", kept["id"], f"Kept: {short}")
        await self.settled(card, answer, kept)
        return card

    async def note(self, card: dict, kept: dict, said: str) -> dict:
        """The owner answered a reconsideration in words that pick none of its options: their
        note to the workers that asked (an instruction), not a decision; the decision in force
        stays."""
        card = self.close(card["id"], "answered", "Kept your decision; sent your note")
        text = (
            f"The owner picked none of the options: {said}\nTheir decision stays in force: "
            f"{kept['text']}.\nTake this as their instruction and go on; if a decision is still "
            "needed after it, ask again."
        )
        short = said if len(said) <= 80 else said[:79].rstrip() + "\u2026"
        await self.tell_askers(card, text, f"Note: {said}", kept["id"], f"Your note: {short}")
        await self.settled(card, said, kept, note=True)
        return card

    @staticmethod
    def named(target: dict, text: str) -> str:
        """The option an answer picks or clearly names ("" for none, or for more than one): its
        words, words that hold one option's ("C and D, but log it"), or a part of one option
        only ("hola" of "Spanish: hola")."""

        def plain(value: str) -> str:
            value = QUOTES.sub(" ", value.casefold())
            return " ".join(value.split()).rstrip(".!?\u3002\uff01\uff1f")

        def within(part: str, whole: str) -> bool:
            if part.isascii():
                # A one-letter option ("A") is named only first ("A, please"), never by the "a"
                # of "add a test".
                start = "^" if len(part) == 1 else r"(?<!\w)"
                return re.search(rf"{start}{re.escape(part)}(?!\w)", whole) is not None
            return part in whole  # (No word boundaries in Chinese.)

        said = plain(text)
        choices = [o["choice"] for o in target.get("options") or [] if plain(o["choice"])]
        if not said:
            return ""
        if exact := [choice for choice in choices if plain(choice) == said]:
            return exact[0]
        found = [choice for choice in choices if within(plain(choice), said)]
        # "C only" holds "C" too: the longer one is meant.
        found = [c for c in found if not any(c != o and within(plain(c), plain(o)) for o in found)]
        if not found:
            found = [choice for choice in choices if within(said, plain(choice))]
        return found[0] if len(found) == 1 else ""

    async def tell_askers(
        self, card: dict, text: str, display: str, identifier: str, progress: str = ""
    ):
        """Every worker that waits on a card hears the owner's answer at once: steered into the
        turn it is in when it asked and went on (else it starts its next one), so nothing it
        does after the answer is done without it. `progress`: what its task shows at work now
        (not "Needs your decision" any more)."""
        target = card["target"]
        for asker in target["askers"]:
            row = self.store.find_session(asker["session_id"])
            if not row or row["closed_at"]:
                continue
            task = self.tasks.find(asker["task_id"]) if asker["task_id"] else None
            if task and task["status"] == "waiting":
                self.tasks.move(task["id"], "running", reason="decision", progress=progress)
            # Only the owner's own words go in the owner's envelope; the question was a worker's.
            asked = wrap("system", "context", f"Your decision card asked: {target['question']}")
            try:
                await self.delivery.deliver(
                    row["id"],
                    asked + "\n\n" + wrap("owner", "decision", text, decision=identifier or None),
                    kind="owner",
                    display=display,
                    # The owner's answer to its card, their words (Asks.held_ask reads it so).
                    meta={
                        **({"task_id": task["id"]} if task else {}),
                        "answers": card["id"],
                        "asked": target["question"],
                        "answer": display,
                        "owner_said": True,
                        **({"decision": identifier} if identifier else {}),
                    },
                    steer=True,
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
        self.drop_askers(lambda asker: asker["session_id"] == session_id, "expired", CLOSED)

    def waited_on(self, session_id: str) -> dict[str, list[str]]:
        """The open decision cards a worker's tasks wait on: task id -> card ids."""
        found: dict[str, list[str]] = {}
        for card in self.open_cards("decision"):
            for asker in card["target"]["askers"]:
                if asker["session_id"] == session_id and asker.get("task_id"):
                    found.setdefault(asker["task_id"], []).append(card["id"])
        return found

    def wait_again(self, task: dict, cards: list[str]):
        """A task back from its worker's close waits on the decision cards it waited on then
        (bug E106: its task waited for "your decision", its card gone): one that closing ended
        is open again; one others still wait on has it back among them. Not one the owner
        answered or dismissed meanwhile."""
        asker = {"session_id": task["session_id"], "task_id": task["id"], "title": task["title"]}
        for card_id in cards:
            try:
                card = self.store.draft(card_id)
            except LookupError:
                continue
            askers = card["target"]["askers"]
            if card["status"] == "expired" and card["outcome"] == CLOSED:
                askers = []
            elif card["status"] != "pending" or asker in askers:
                continue
            self.store.update_draft_target(card_id, {**card["target"], "askers": [*askers, asker]})
            if card["status"] != "pending":
                self.store.update_draft(card_id, status="pending", outcome="", resolved_at=None)
            self.bus.publish("draft", self.store.draft(card_id))

    def drop_task(self, task: dict):
        """A task that ended (done, failed, cancelled) no longer waits for a decision. Its
        worker hears so with its next message (bug E100: it waited on, unaware)."""
        left = self.drop_askers(
            lambda asker: asker.get("task_id") == task["id"], "expired", "Its task ended."
        )
        row = self.store.find_session(task["session_id"])
        if not left or not row or row["closed_at"]:
            return
        asked = "; ".join(f"“{card['target']['question']}”" for card in left)
        self.delivery.hold(
            row["id"],
            wrap(
                "system",
                "decision-expired",
                f"Task {task['id']} “{task['title']}” ended ({task['status']}): the owner's "
                f"decision on your card {asked} will not come. Do not wait for it.",
                task=task["id"],
            ),
            kind="notice",
            display=f"Its decision card closed: “{task['title']}” ended.",
        )

    async def answered_in_chat(self, session_id: str, task: dict | None, text: str) -> list[dict]:
        """The owner wrote to a worker that waits on its decision card: that is their answer to
        it, for that worker (the card leaves once nobody else waits on it), kept as their
        decision for its task (asked again, it is the answer). The cards. Not a question back
        ("before I pick: what does B change?"): the card stays (`asked_back`)."""
        if asks_back(text):
            return []
        left = self.drop_askers(
            lambda asker: self.asker_of(asker, session_id, task),
            "answered",
            f"Answered in chat: {clean(text, 300)}",
        )
        for card in left:
            target = card["target"]
            if target.get("replaces") and target.get("options") and not self.named(target, text):
                # A reconsideration, and none of its options: their chat with it, the decision
                # as it was (E154).
                if self.store.draft(card["id"])["status"] != "pending":
                    self.close(card["id"], "answered", "Kept your decision; sent your note")
                with suppress(Exception):
                    kept = self.current(target["replaces"])
                    await self.runtime.ledger.settle(
                        "decision", card["id"], text, session_id=session_id, decision=kept,
                        note=True,
                    )  # fmt: skip
                continue
            tasks = [
                asker["task_id"]
                for asker in target["askers"]
                if self.asker_of(asker, session_id, task) and asker.get("task_id")
            ]
            try:
                replaces = (
                    [self.current(target["replaces"])["id"]] if target.get("replaces") else []
                )
                recorded = await self.record(
                    f"{target['question']} → {text}", tasks, replaces, f"chat:{card['id']}"
                )
                # Any answer settles its subject (E151): the others that wait on it hear it.
                await self.runtime.ledger.settle(
                    "decision", card["id"], text, session_id=session_id,
                    decision=recorded["decision"],
                )  # fmt: skip
            except Exception:
                # Their message reaches the worker whatever becomes of keeping it.
                logger.warning("Could not keep the decision answered in chat.", exc_info=True)
        return left

    def asked_back(self, session_id: str, task: dict | None, text: str) -> list[dict]:
        """The decision cards this worker waits on that the owner's message asks about instead
        of answering."""
        if not asks_back(text):
            return []
        return [
            card
            for card in self.open_cards("decision")
            if any(self.asker_of(a, session_id, task) for a in card["target"]["askers"])
        ]

    @staticmethod
    def asker_of(asker: dict, session_id: str, task: dict | None) -> bool:
        return asker["session_id"] == session_id and (
            not task or not asker.get("task_id") or asker["task_id"] == task["id"]
        )

    def drop_askers(self, gone: Callable[[dict], bool], status: str, outcome: str) -> list[dict]:
        """Askers that no longer wait leave their cards; a card nobody waits for ends with
        `status` and `outcome`. The cards they left."""
        left = []
        for card in self.open_cards("decision"):
            askers = [a for a in card["target"]["askers"] if not gone(a)]
            if len(askers) == len(card["target"]["askers"]):
                continue
            left.append(card)
            if askers:
                target = {**card["target"], "askers": askers}
                self.bus.publish("draft", self.store.update_draft_target(card["id"], target))
            else:
                self.close(card["id"], status, outcome)
        return left

    # Recording and changing

    async def t_record_decision(self, params: RecordDecision, caller: str):
        if self.store.session(caller)["role"] != ROUTER:
            raise ValueError("Only the Router records decisions.")
        named = [self.runtime.task_actions.task_for(ref) for ref in params.tasks]
        state = self.runtime.states[caller]
        self.check_finished(caller, params.decision, named)
        tasks = [task["id"] for task in named]
        if not params.replaces:
            # Tasks it gave out in this turn came with this decision in the owner's message.
            tasks = list(dict.fromkeys(tasks + state.turn_tasks))
        replaces = list(params.replaces)
        same, possibly = None, []
        if not replaces:
            # The owner's decision in force on the same choice is what this one changes (one
            # value for one choice: E151, E153); one only possibly the same stays, said.
            same, possibly = self.runtime.ledger.decided_as(params.decision, tasks)
            replaces = [same["id"]] if same else []
        # Workers given their task before it hear it; those given one in this turn had it in
        # the owner's message.
        given = [task["id"] for task in named]
        # Replaced by itself, in other words: the same value, kept quietly (E154: no worker
        # told to redo anything, no finished task reopened). Named by the Router: as asked.
        reworded = bool(same and same_value(same["text"], params.decision))
        result = await self.record(
            params.decision, tasks, replaces, "owner", given, reworded=reworded
        )
        decision, affected = result["decision"], result["affected"]
        state.turn_decisions.append(decision["id"])
        if not replaces:
            self.runtime.ledger.recorded(decision, tasks)
        head = f"Recorded {decision['id']}"
        if same:
            head += (
                f" in place of {same['id']} (“{clean(same['text'], 160)}”), the owner's decision "
                "in force on the same choice"
            )
        tail = "".join(
            f" Possibly the same as {d['id']} (\u2018{clean(d['text'], 160)}\u2019); pass "
            "replaces if it changes it."
            for d in possibly
        )
        if result.get("same") and reworded:
            return (
                f"{head}: the same value, reworded. Nothing changed: no worker was told, no "
                f"finished task reopened. Linked to {len(tasks)} more task(s)"
                f"{self.titles(tasks)}.{tail}"
            )
        if result.get("same"):
            return (
                f"{head}, the same decision as before: nothing changed, no worker was told. "
                f"Linked to {len(tasks)} more task(s){self.titles(tasks)}.{tail}"
            )
        if not replaces:
            told = len(result.get("told") or [])
            said = f" {told} of their worker(s) were told." if told else ""
            for question in result.get("open") or []:
                said += (
                    f" Task {question['task_id']}'s question to the owner stays open: "
                    f"“{clean(question['text'], 200)}”. If this decision answers it, "
                    f'message_session(task="note", task_id="{question["task_id"]}", '
                    "answers_question=true)."
                )
            return f"{head}, linked to {len(tasks)} task(s){self.titles(tasks)}.{said}{tail}"
        if not affected:
            return f"{head}. No open or finished task relied on what it changes.{tail}"
        told = [item for item in affected if item["told"]]
        redo = sum(item["was"] in {"review", "done"} for item in told)
        missed = [item["title"] for item in affected if not item["told"]]
        text = f"{head}. It affects {len(affected)} task(s)"
        if redo:
            text += f"; {redo} checked result(s) are out of date and reopened"
        if told:
            text += f". {len(told)} worker(s) were told and must confirm"
        if missed:
            text += f". Could not tell: {', '.join(missed)} (their results are marked out of date)"
        return text + f". The owner sees the impact on a card. Tell the owner in one line.{tail}"

    def check_finished(self, caller: str, text: str, named: list[dict]):
        """Asked once of finished tasks the Router names for a decision (live e2e r81: told a
        decision with the new task relying on it, it linked a finished, unrelated one too, which
        a change of it then reopened): one relies on it only when its delivered work does."""
        finished = [task for task in named if task["status"] in {"review", "done"}]
        if not finished:
            return
        asked = json.dumps([clean(text, 500), sorted(task["id"] for task in finished)])
        if self.store.get(f"decisionfinished:{caller}") == asked:
            return
        self.store.set(f"decisionfinished:{caller}", asked)
        listed = "; ".join(f"{task['id']} \u201c{task['title']}\u201d" for task in finished)
        raise ValueError(
            f"Not recorded yet: {listed} {'is' if len(finished) == 1 else 'are'} finished. Link "
            "a finished task only when the work it delivered relies on this decision (a change of "
            "the decision reopens it): if so, call again with the same tasks. Otherwise "
            "call again without it: the tasks you start in this turn rely on the decision by "
            "themselves."
        )

    def titles(self, tasks: list[str]) -> str:
        """Which tasks a decision is linked to, for the Router: ": t1 “Write api.md”; …"."""
        found = [task for task_id in tasks if (task := self.tasks.find(task_id))]
        listed = "; ".join(f"{task['id']} \u201c{task['title']}\u201d" for task in found)
        return f": {listed}" if listed else ""

    async def record(
        self,
        text: str,
        tasks: list[str],
        replaces: list[str],
        source: str,
        tell: list[str] | None = None,
        reworded: bool = False,
    ) -> dict:
        """`tell`: tasks of a new decision (replacing none) whose workers hear it now.
        `reworded`: it says what the one it replaces said, in other words (Ledger.same_value):
        nothing changed."""
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
        # Reconsiderations of what this replaces that still wait (the owner decided another
        # way: told the Router, answered another card or in a worker's chat): nothing is left
        # for them to decide.
        moot = [
            card
            for card in (self.open_cards("decision") if old else [])
            if card["target"].get("replaces") in {row["id"] for row in old}
            and source != f"card:{card['id']}"
        ]
        waited = [a["task_id"] for card in moot for a in card["target"]["askers"] if a["task_id"]]
        relying = list(
            dict.fromkeys(tasks + waited + [t for row in old for t in self.linked(row["id"])])
        )
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
        # The subjects the old ones settled are this one's now.
        self.runtime.ledger.carry([row["id"] for row in old], identifier)
        for card in moot:
            self.close(card["id"], "expired", f"Decided meanwhile: {text}")
        short = text if len(text) <= 80 else text[:79].rstrip() + "\u2026"
        for task_id in waited:
            if (task := self.tasks.find(task_id)) and task["status"] == "waiting":
                # Told of the change below, as every task that relied on it.
                self.tasks.move(task_id, "running", reason="decision", progress=f"Decided: {short}")
        affected = []
        if (
            old
            and not moot
            and (reworded or all(same_words(value(row["text"]), value(text)) for row in old))
        ):
            # Recorded again in the same words, or the same answer in other words (to link more
            # tasks, say): nothing changed, so no worker is told to redo anything, and no
            # finished task reopens (E154).
            for task_id in relying:
                self.runtime.task_actions.publish(self.tasks.get(task_id))
            return {"decision": decision, "affected": [], "same": True}
        if not old and tell:
            # A new decision the Router recorded for tasks already given: their workers hear it
            # (E153: one waited for it, unaware, and asked the owner again).
            told, still = await self.tell_linked(decision, tell)
            for task_id in relying:
                self.runtime.task_actions.publish(self.tasks.get(task_id))
            return {"decision": decision, "affected": [], "told": told, "open": still}
        if old and source == "owner":
            self.runtime.learning.record(
                "decision-changed",
                context="Before: " + "; ".join(row["text"] for row in old),
                owner=decision["text"],
            )
        if old:
            workers: dict[str, list[dict]] = {}
            for task_id in relying:
                task = self.tasks.get(task_id)
                if task["status"] in {"failed", "cancelled"}:
                    continue
                if source.startswith(("card:", "chat:", "ask:")) and task_id in tasks:
                    continue  # It asked: the owner's answer reaches it as such.
                workers.setdefault(task["session_id"], []).append(task)
            for its in workers.values():
                # One message for each worker, whatever number of its tasks rely on it (E154:
                # six to four workers).
                affected += await self.propagate(its, old, decision)
            if affected:
                self.change_card(decision, old, affected)
        for task_id in relying:
            self.runtime.task_actions.publish(self.tasks.get(task_id))
        return {"decision": decision, "affected": affected}

    async def propagate(self, tasks: list[dict], old: list[dict], decision: dict) -> list[dict]:
        """A worker's tasks relied on a decision that changed: it adjusts and confirms, told
        once for all of them."""
        before = "; ".join(row["text"] for row in old)
        for task in tasks:
            if task["status"] in {"review", "done"}:
                self.runtime.task_actions.stale(
                    task["id"],
                    f"Out of date: the decision “{before}” changed after it was checked.",
                )
        row = self.store.session(tasks[0]["session_id"])
        named = ", ".join(f"{task['id']} “{task['title']}”" for task in tasks)
        relies = "relies" if len(tasks) == 1 else "rely"
        note = (
            f"The owner changed a decision that task{'s' if len(tasks) > 1 else ''} {named} "
            f"{relies} on.\nBefore: {before}\nNow: {decision['text']}\n"
            "Stop work that relies on the old decision. Keep what still fits and change what "
            f'does not, then call ack_decision(decision="{decision["id"]}", plan=what you '
            "change, or why nothing has to). Check your Done when criteria again before you "
            "report done."
        )
        one = tasks[0]["id"] if len(tasks) == 1 else None
        try:
            if row["closed_at"]:
                await self.runtime.workers.reopen(row["id"])
            await self.delivery.deliver(
                row["id"],
                wrap("router", "decision-changed", note, decision=decision["id"], task=one),
                kind="router",
                display=f"Decision changed: {decision['text']}",
                meta={"task_id": one} if one else None,
                steer=True,
            )
        except Exception as exc:
            logger.warning("Could not tell %s about the change.", row["id"], exc_info=True)
            # Nothing will redo it: the card says so instead of waiting for a confirmation.
            error = str(exc)[:200] or type(exc).__name__
        else:
            error = ""
        affected = []
        for task in tasks:
            was = task["status"]
            if not error and was in {"review", "done"}:
                # Remembered: if nothing has to change, it goes back there (ack_decision).
                self.store.set(f"decision_was:{task['id']}", was)
                self.tasks.move(task["id"], "running", reason="decision changed")
            affected.append(
                {
                    "task_id": task["id"],
                    "title": task["title"],
                    "session_id": row["id"],
                    "was": was,
                    "told": not error,
                    "error": error,
                    "ack": "",
                }
            )
        return affected

    async def tell_linked(self, decision: dict, tasks: list[str]) -> tuple[list[str], list[dict]]:
        """A new decision linked to tasks given already: each of their workers with one of them
        open hears it in one note (E153), at once when it waits for the owner on one of them.
        A question it asked the owner on one stays open (it may ask something else: owner
        report, 0.1.58): the Router says when the decision answers it (message_session's
        answers_question). The workers told, and the questions left open."""
        workers: dict[str, list[dict]] = {}
        for task_id in tasks:
            task = self.tasks.find(task_id)
            row = self.store.find_session(task["session_id"]) if task else None
            if task and row and not row["closed_at"] and task["status"] in OPEN:
                workers.setdefault(row["id"], []).append(task)
        told, still = [], []
        for session_id, its in workers.items():
            ids = {task["id"] for task in its}
            asked = [
                question
                for question in self.runtime.asks.session_questions(session_id)
                if question.get("task_id") in ids and not question.get("went_on")
            ]
            still += asked
            waits = any(task["status"] == "waiting" for task in its)
            one = its[0]["id"] if len(its) == 1 else None
            named = ", ".join(f"{task['id']} “{task['title']}”" for task in its)
            text = f"The owner decided: {decision['text']} ({decision['id']}). Your task"
            text += f"s {named} rely" if len(its) > 1 else f" {named} relies"
            text += " on it: work to it."
            if asked:
                listed = "; ".join(f"“{clean(q['text'], 200)}”" for q in asked)
                text += (
                    f" Your question to the owner ({listed}) stays open under Needs you until "
                    "they or the Router answer it."
                )
            meta: dict = {"decision": decision["id"]}
            if one:
                meta["task_id"] = one
            try:
                await self.delivery.deliver(
                    session_id,
                    wrap("router", "decision", text, decision=decision["id"], task=one),
                    kind="router",
                    display=f"Decision: {decision['text']}",
                    meta=meta,
                    steer=waits,
                )
                told.append(session_id)
            except Exception:
                logger.warning("Could not tell %s the decision.", session_id, exc_info=True)
        return told, still

    def change_card(self, decision: dict, old: list[dict], affected: list[dict]):
        changed = {row["id"] for row in old}
        since = hours_ago(CHANGE_DEBOUNCE_HOURS)
        for card in self.open_cards("change"):
            target = card["target"]
            if target["decision"]["id"] not in changed or card["created_at"] < since:
                continue
            # The same decision changed again soon after (E154: a second card an hour later):
            # one card, from what it was first to what it is now, each task as told last.
            items = {item["task_id"]: item for item in target["tasks"]}
            items |= {item["task_id"]: item for item in affected}
            target |= {
                "decision": {"id": decision["id"], "text": decision["text"]},
                "tasks": list(items.values()),
            }
            self.bus.publish("draft", self.store.update_draft_target(card["id"], target))
            return
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
        for card in self.change_cards(decision["id"]):
            target = card["target"]
            for item in target["tasks"]:
                if item["task_id"] in mine:
                    item["ack"] = plan
                    item["changes"] = params.changes
            card = self.store.update_draft_target(card["id"], target)
            if card["status"] != "pending":
                # Seen already (Got it): its confirmations are kept on it all the same (E160).
                self.bus.publish("draft", card)
                continue
            told = [item for item in target["tasks"] if item.get("told", True)]
            every = len(told) == len(target["tasks"])
            if not (told and all(item["ack"] for item in told) and every):
                self.bus.publish("draft", card)
            elif all(item.get("changes") is False for item in told):
                # Nothing changes for anyone (E123): it closes by itself, handled for the owner.
                card = self.close(
                    card["id"], "answered", "Every affected worker said nothing changes."
                )
                count = len(told)
                self.runtime.handled.record(
                    "change",
                    f"Closed “Decision changed: {target['decision']['text']}”: "
                    f"{count} worker{'s' if count > 1 else ''} said nothing changes for "
                    f"{'them' if count > 1 else 'it'}",
                    ref=card["id"],
                )
            else:
                # Work changes because of it (or a confirmation from before says not whether):
                # the card stays for the owner to see who redoes what.
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

    def change_cards(self, identifier: str) -> list[dict]:
        """The change cards of a decision, whatever became of them."""
        return [
            self.store.draft(row["id"])
            for row in self.store.all(
                "SELECT id FROM drafts WHERE kind='change' "
                "AND json_extract(target, '$.decision.id')=? ORDER BY created_at",
                (identifier,),
            )
        ]

    def dismiss(self, card: dict) -> dict:
        return self.close(card["id"], "answered", "Seen.")
