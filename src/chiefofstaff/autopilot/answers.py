"""Autopilot answers (autopilot.md §5.1, A1, A4, A6): a worker's question or decision is answered
when the owner's own words settle it and it asks no permission; otherwise it is parked: left for
the owner under Needs you, saying why, while the work around it goes on.

- **A question card** in a worker's turn (Copilot's ask_user, Claude's AskUserQuestion): not
  drawn while it is judged (two minutes at most); answered into that turn, which goes on under
  the owner's settings (and Ask first, from then on, if that line is on); or answered with
  *the owner is away* and kept under Needs you. A form or a link (an MCP tool's) is declined and
  kept. A Codex worker's, or one in a Claude process opened acting on its own while *workers ask
  first* is on, is parked: its turn can't take the Ask-first checks.
- **A decision card**: decided in Autopilot's own table (never the shared decisions), its
  askers told in Autopilot's envelope, never the owner's; or it stays, drawn, saying why.
- **A question a worker reported** (report status question): once the Router's turn on it ended
  with it still open, answered in a message to its worker, which goes on; or left as it is.

Each is one transaction: the item's change, its entry, and the delivery in the outbox (sent
once it commits). Nothing is learned from Autopilot's answers."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..asks import DECLINED
from ..backend import COPILOT_AGENT
from ..envelope import wrap
from ..store import now
from . import basis as owner
from . import judge
from .logbook import PARKED
from .origin import CARRIED

if TYPE_CHECKING:
    from .core import Autopilot

logger = logging.getLogger(__name__)

# The longest a card waits, not drawn, while it is judged; and the judge's own time.
DECIDING = 120.0
JUDGE_SECONDS = 110.0
# Card kinds it answers in a worker's turn, and those it only parks.
ANSWERED = frozenset({"question"})
PARKED_ONLY = frozenset({"form", "link"})
# What the judge's helper session is called (store.py HELPER_KINDS).
JUDGE = "judge"
# A report question asking for one of these waits for the owner, whatever was said.
OWNER_ONLY = ("access", "a key", "the key", "money", "budget", "account", "sign in", "log in")


class Later(Exception):  # noqa: N818  (not an error: delivered later)
    """A delivery that waits (stays queued): the owner's work goes first."""


def clock(iso: str) -> str:
    try:
        moment = datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return ""
    return f"{moment.strftime('%a')} {moment.hour % 12 or 12}:{moment.minute:02d} " + (
        "AM" if moment.hour < 12 else "PM"
    )


class Answers:
    """The mixin of Autopilot that answers or parks what workers ask."""

    # The judge: one item at a time, its proposal through its one tool.

    async def ask_judge(self: Autopilot, text: str) -> judge.ProposeAnswer | None:  # type: ignore[misc]
        """The judge's proposal for one item (None when it gave none in time)."""
        async with self.judge_lock:
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self.proposal = future
            token = CARRIED.set(None)  # The judge is Autopilot's own, never continued work.
            started = datetime.now(UTC).timestamp()
            session_id = ""
            try:
                made = await self.runtime.toolset.start_special(JUDGE, "Autopilot's judge", text)
                session_id = made["session_id"]
                return await asyncio.wait_for(future, JUDGE_SECONDS)
            except Exception:
                logger.warning("Autopilot's judge gave no proposal.", exc_info=True)
                return None
            finally:
                CARRIED.reset(token)
                self.proposal = None
                if session_id:
                    self.spend(self.usage_since(session_id, started))

    async def t_propose_answer(self: Autopilot, params: judge.ProposeAnswer, caller: str):  # type: ignore[misc]
        if not self.store.get(f"{JUDGE}:{caller}"):
            raise ValueError("Only Autopilot's judge proposes answers.")
        if self.proposal is None or self.proposal.done():
            raise ValueError("Nothing is being judged now.")
        self.proposal.set_result(params)
        return "Proposed. End your turn."

    def usage_since(self: Autopilot, session_id: str, since: float) -> float:  # type: ignore[misc]
        return float(
            self.store.value(
                "SELECT coalesce(sum(credits), 0) FROM usage WHERE session_id=? AND at>=?",
                (session_id, since),
            )
        )

    async def judged(
        self: Autopilot,  # type: ignore[misc]
        *,
        kind: str,
        question: str,
        options: list[str],
        why: str,
        task: dict,
        context: str,
    ) -> tuple[judge.Verdict, list[owner.Item]]:
        """Code's verdict on one item (the judge asked only when code can't settle it)."""
        if judge.permission(question, *options):
            return judge.Verdict(parked="it asks for a permission only you can give"), []
        offered = owner.basis_for(self.runtime, task)
        if not offered:
            return judge.Verdict(parked="nothing you said settles it"), []
        if self.paused():
            return judge.Verdict(parked="today's credits for Autopilot are used"), offered
        text = judge.prompt(kind=kind, question=question, options=options, why=why, task=task,
                            offered=offered, context=context)  # fmt: skip
        proposal = await self.ask_judge(text)
        if proposal is None:
            return judge.Verdict(parked="it couldn't be judged just now"), offered
        cited = [cite.model_dump() for cite in proposal.basis]
        refusal = owner.refusal(self.runtime, task, cited, offered)
        verdict = judge.accepted(proposal, question=question, options=options, offered=offered,
                                 basis_refusal=refusal)  # fmt: skip
        return verdict, offered

    def basis_still(self: Autopilot, task: dict, verdict: judge.Verdict,  # type: ignore[misc]
                    offered: list[owner.Item]) -> str:  # type: ignore[misc]  # fmt: skip
        """Its basis checked again just before the answer is delivered."""
        return owner.refusal(self.runtime, task, list(verdict.basis), offered)

    def because(self: Autopilot, verdict: judge.Verdict, offered: list[owner.Item]) -> str:  # type: ignore[misc]
        """“because you said “Postgres for v1” (Mon 10:02 AM)”."""
        at = {item.id: item.at for item in offered}
        said = [
            f"“{cite['quote']}”" + (f" ({when})" if (when := clock(at.get(cite["id"], ""))) else "")
            for cite in verdict.basis
        ]
        return "because you said " + " and ".join(said) if said else ""

    # Which items it takes.

    def takes(self: Autopilot, row: dict) -> bool:  # type: ignore[misc]
        """Whether a run is on and this is a worker of the owner's in scope."""
        if not self.ticked or row.get("role") != "worker" or self.runtime.special(row):
            return False
        if self.run() is None:
            return False
        repos = self.store.settings().autopilot_repos
        if not repos:
            return True
        found = {r.lower() for r in json.loads(row.get("repos") or "[]") if isinstance(r, str)}
        return bool(found & set(repos))

    def task_of(self: Autopilot, session_id: str, task_id: str = "") -> dict | None:  # type: ignore[misc]
        tasks = self.runtime.tasks
        return (tasks.find(task_id) if task_id else None) or tasks.current(session_id)

    def context_of(self: Autopilot, task: dict) -> str:  # type: ignore[misc]
        """What the judge reads as data: the task's instructions and its latest report."""
        report = self.store.one(
            "SELECT text FROM messages WHERE session_id=? AND kind='report' ORDER BY id DESC "
            "LIMIT 1",
            (task["session_id"],),
        )
        latest = (report or {}).get("text", "")
        return f"Instructions: {task.get('goal', '')}\n\nLatest report: {latest}"

    # A1 to A3: a card in a worker's turn.

    def holds_card(self: Autopilot, row: dict, kind: str) -> bool:  # type: ignore[misc]
        """Whether this card is Autopilot's to decide first (not drawn meanwhile)."""
        return kind in ANSWERED | PARKED_ONLY and self.takes(row)

    async def decide_card(self: Autopilot, row: dict, draft: dict, answer: asyncio.Future):  # type: ignore[misc]
        """A card held from the page while it is judged: answered into its turn, or parked (then
        drawn, once, with its notification)."""
        self.deciding.add(draft["id"])
        self.runtime.publish_state()
        target, kind = draft["target"], draft["kind"]
        task = self.task_of(row["id"])
        reason, verdict, offered = "", judge.Verdict(), []
        if kind in PARKED_ONLY:
            reason = "it asks for a sign-in or private values only you can give"
        elif not task:
            reason = "it is not about a task"
        elif not self.in_turn_answer(row):
            reason = "its worker can't take Autopilot's answer in this turn"
        else:
            with suppress(TimeoutError):
                verdict, offered = await asyncio.wait_for(
                    self.judged(kind="question", question=target.get("question") or draft["text"],
                                options=list(target.get("choices") or []), why="", task=task,
                                context=self.context_of(task)),
                    DECIDING,
                )  # fmt: skip
            reason = verdict.parked or ("" if verdict.answer else "it couldn't be judged in time")
        try:
            if answer.done():
                return  # The owner answered it first.
            if not reason and task:
                reason = self.basis_still(task, verdict, offered)
            if reason:
                self.park_card(row, draft, answer, task, reason, verdict.reason)
            else:
                self.answer_card(row, draft, answer, task or {}, verdict, offered)
        finally:
            self.deciding.discard(draft["id"])
            self.runtime.publish_state()

    def in_turn_answer(self: Autopilot, row: dict) -> bool:  # type: ignore[misc]
        """Whether an answer may go into this worker's running turn: Copilot takes the Ask-first
        checks per request from then on; Claude only in a process opened asking first (or with
        *workers ask first* off); Codex never (its policy is set when its thread starts)."""
        agent = self.runtime.agent(row["id"])
        if agent == COPILOT_AGENT:
            return True
        if agent != "claude":
            return False
        if not self.store.settings().autopilot_ask_first:
            return True
        return self.runtime.states[row["id"]].opened_asking is True

    async def ready(self: Autopilot, row: dict) -> bool:  # type: ignore[misc]
        """Before Autopilot continues a worker on another agent with *workers ask first* on: its
        process, opened acting on its own, is opened again asking first, only when it is not at
        work (then this waits). Never the owner's saved setting."""
        if self.runtime.agent(row["id"]) == COPILOT_AGENT:
            return True
        if not self.store.settings().autopilot_ask_first:
            return True
        state = self.runtime.states[row["id"]]
        if state.opened_asking is not False or not state.handle:
            return True  # Asking first already, or opened afresh (asking first) for this.
        if row["status"] in {"working", "starting"} or state.busy or state.held:
            return False
        await self.runtime.agent_mode_changed(row, asks_first=True)
        return True

    def answer_card(self: Autopilot, row: dict, draft: dict, answer: asyncio.Future, task: dict,  # type: ignore[misc]
                    verdict: judge.Verdict, offered: list[owner.Item]):  # type: ignore[misc]  # fmt: skip
        run = self.run() or {}
        because = self.because(verdict, offered)
        with self.store.transaction():
            entry = self.book.append(
                run.get("id", ""), "answered",
                f"Answered “{draft['text'][:120]}”: {verdict.answer}",
                subject=f"card:{draft['id']}", task_id=task.get("id", ""),
                why={"rule": "owner-basis", "basis": list(verdict.basis), "judge": verdict.reason},
                payload={"question": draft["text"], "answer": verdict.answer},
            )  # fmt: skip
            said = f"Answered by Autopilot: {verdict.answer[:400]}"
            self.store.update_draft(draft["id"], status="answered", resolved_at=now(), outcome=said)
        origin = {"entries": [entry["id"]], "ask_first": self.store.settings().autopilot_ask_first}
        self.origins.keep(row["id"], origin)  # The turn goes on Autopilot's word from here.
        self.notice(row["id"], task, entry, f"Autopilot answered: {verdict.answer}, {because}.")
        self.runtime.bus.publish("draft", self.store.draft(draft["id"]))
        if not answer.done():
            answer.set_result({
                "answer": f"{verdict.answer}\n\n(Answered for the owner by Autopilot, from what "
                          f"they said: {because.removeprefix('because you said ')}.)",
                "wasFreeform": verdict.answer not in (draft["target"].get("choices") or []),
            })  # fmt: skip

    def park_card(self: Autopilot, row: dict, draft: dict, answer: asyncio.Future,  # type: ignore[misc]
                  task: dict | None, reason: str, judged: str = ""):  # type: ignore[misc]  # fmt: skip
        """Its turn goes on without it; it waits under Needs you, saying why, drawn once."""
        run = self.run() or {}
        kind = draft["kind"]
        with self.store.transaction():
            entry = self.book.append(
                run.get("id", ""), PARKED, f"Set aside “{draft['text'][:120]}”: {reason}",
                state=PARKED, subject=f"card:{draft['id']}", task_id=(task or {}).get("id", ""),
                why={"rule": "parked", "reason": reason, "judge": judged},
                payload={"question": draft["text"], "kind": kind},
            )  # fmt: skip
            self.store.update_draft(draft["id"], status="answered", resolved_at=now(),
                                    outcome=f"Set aside by Autopilot: {reason}")  # fmt: skip
            self.store.set(f"autopilot:parked:{row['id']}:{(task or {}).get('id', '')}",
                           json.dumps({"why": reason, "entry": entry["id"]}))  # fmt: skip
        choices = draft["target"].get("choices") or []
        listed = f"\n\nChoices: {', '.join(map(str, choices))}" if choices else ""
        self.runtime.asks.record_question(
            row["id"], draft["text"] + listed, (task or {}).get("id", ""),
            why=f"Set aside: {reason}.",
        )  # fmt: skip
        self.runtime.bus.publish("draft", self.store.draft(draft["id"]))
        title = f"Set aside by Autopilot · {self.runtime.display_title(row)}"
        self.runtime.alert(draft["text"], session_id=row["id"], needs_action=True, title=title)
        if not answer.done():
            answer.set_result(
                DECLINED[kind] if kind in PARKED_ONLY
                else {"answer": judge.PARKED_ANSWER, "wasFreeform": True}
            )  # fmt: skip

    def notice(self: Autopilot, session_id: str, task: dict | None, entry: dict, text: str):  # type: ignore[misc]
        """One line in the worker's chat, marked as Autopilot's, linking to its entry."""
        self.runtime.add_message(
            session_id, "notice", text,
            {"autopilot": {"entry": entry["id"], "run": entry["run_id"]},
             **({"task_id": task["id"]} if task else {})},
        )  # fmt: skip

    # A4: decision cards.

    def holds_decision(self: Autopilot, row: dict) -> bool:  # type: ignore[misc]
        return self.takes(row)

    async def decide_decision(self: Autopilot, row: dict, card: dict):  # type: ignore[misc]
        """A decision card held from the page while it is judged."""
        self.deciding.add(card["id"])
        self.runtime.publish_state()
        try:
            target = card["target"]
            task = self.task_of(row["id"], (target.get("askers") or [{}])[0].get("task_id", ""))
            options = [str(option.get("choice") or "") for option in target.get("options") or []]
            reason, verdict, offered = "", judge.Verdict(), []
            if target.get("replaces"):
                reason = "it reconsiders what you decided"
            elif not task:
                reason = "it is not about a task"
            else:
                with suppress(TimeoutError):
                    verdict, offered = await asyncio.wait_for(
                        self.judged(kind="decision", question=target["question"], options=options,
                                    why=target.get("why", ""), task=task,
                                    context=self.context_of(task)),
                        DECIDING,
                    )  # fmt: skip
                reason = verdict.parked or ("" if verdict.answer else "it wasn't judged in time")
            card = self.store.draft(card["id"])
            if card["status"] != "pending":
                return  # The owner (or the Router) decided it first.
            if not reason and task:
                reason = self.basis_still(task, verdict, offered)
            if reason:
                self.park_decision(row, card, task, reason, verdict.reason)
            else:
                self.decide(row, card, task or {}, verdict, offered)
        finally:
            self.deciding.discard(card["id"])
            self.runtime.publish_state()

    def decide(self: Autopilot, row: dict, card: dict, task: dict, verdict: judge.Verdict,  # type: ignore[misc]
               offered: list[owner.Item]):  # type: ignore[misc]  # fmt: skip
        run = self.run() or {}
        target = card["target"]
        because = self.because(verdict, offered)
        with self.store.transaction():
            fresh = self.store.draft(card["id"])
            if fresh["status"] != "pending":
                return
            entry = self.book.append(
                run.get("id", ""), "decided", f"“{target['question'][:120]}” → {verdict.answer}",
                subject=f"card:{card['id']}", task_id=task.get("id", ""),
                why={"rule": "owner-basis", "basis": list(verdict.basis), "judge": verdict.reason},
                payload={"question": target["question"], "options": target.get("options") or [],
                         "choice": verdict.answer},
            )  # fmt: skip
            decided = self.book.decide(
                run_id=run.get("id", ""), entry_id=entry["id"], card_id=card["id"],
                question=target["question"], options=target.get("options") or [],
                choice=verdict.answer, basis=list(verdict.basis), askers=target.get("askers") or [],
            )  # fmt: skip
            said = f"Decided by Autopilot: {verdict.answer[:400]}"
            self.store.update_draft(card["id"], status="answered", resolved_at=now(), outcome=said)
            for asker in target.get("askers") or []:
                said = (
                    f"Your decision card asked: {target['question']}\n\nDecided for the owner by "
                    f"Autopilot (they are away), from what they said: {verdict.answer}. "
                    f"{because[0].upper() + because[1:]}."
                    if because
                    else ""
                )
                self.book.queue(run.get("id", ""), entry["id"], "worker", {
                    "session_id": asker["session_id"], "task_id": asker.get("task_id") or "",
                    "text": said, "display": f"Autopilot decided: {verdict.answer}",
                    "decision": decided, "go_on": True,
                }, self.book.generation())  # fmt: skip
        self.runtime.bus.publish("draft", self.store.draft(card["id"]))
        self.runtime.background(self.dispatch())

    def park_decision(self: Autopilot, row: dict, card: dict, task: dict | None, reason: str,  # type: ignore[misc]
                      judged: str = ""):  # type: ignore[misc]  # fmt: skip
        """It stays for the owner, now drawn, with why; its worker goes on meanwhile."""
        run = self.run() or {}
        with self.store.transaction():
            entry = self.book.append(
                run.get("id", ""), PARKED,
                f"Set aside “{card['target']['question'][:120]}”: {reason}", state=PARKED,
                subject=f"card:{card['id']}", task_id=(task or {}).get("id", ""),
                why={"rule": "parked", "reason": reason, "judge": judged},
                payload={"question": card["target"]["question"]},
            )  # fmt: skip
            self.store.set(f"autopilot:setaside:{card['id']}",
                           json.dumps({"why": reason, "entry": entry["id"]}))  # fmt: skip
        self.runtime.add_message(row["id"], "draft", card["text"],
                                 {"draft_id": card["id"], "label": "Decision"})  # fmt: skip
        self.runtime.bus.publish("draft", self.store.draft(card["id"]))
        self.runtime.alert(card["text"], session_id=row["id"], needs_action=True,
                           title=f"Decision · {row['title']}")  # fmt: skip

    # A6: a question a worker reported, once the Router's turn on it is over.

    async def check_questions(self: Autopilot):  # type: ignore[misc]
        """Questions under Needs you that came in this run, the Router's turn over: judged."""
        run = self.run()
        if run is None:
            return
        router = self.store.find_session(self.runtime.router_id())
        if (
            not router
            or router["status"] == "working"
            or self.runtime.delivery.has_unsent(router["id"])
        ):
            return  # Its turn on them first (the Router may answer them).
        for question_id in [key.removeprefix("ownerq:") for key in self.store.keys("ownerq:")]:
            question = self.runtime.asks.owner_question(question_id)
            if not question:
                continue
            key = f"autopilot:judged:{question['id']}"
            if question.get("at", "") < run["started_at"] or self.store.get(key):
                continue
            session_id = question["id"].split(":", 1)[0]
            row = self.store.find_session(session_id)
            if not row or not self.takes(row) or question.get("about_draft"):
                continue
            self.store.set(key, question.get("at", ""))
            await self.decide_question(row, question)

    async def decide_question(self: Autopilot, row: dict, question: dict):  # type: ignore[misc]
        task = self.task_of(row["id"], question.get("task_id") or "")
        reason, verdict, offered = "", judge.Verdict(), []
        text = question["text"]
        if question.get("kind") == "blocked" and any(w in text.lower() for w in OWNER_ONLY):
            reason = "it asks for an access, a key or money"
        elif not task or task["id"] != (question.get("task_id") or task["id"]):
            reason = "it is not about a task"
        else:
            verdict, offered = await self.judged(
                kind="question", question=text, options=list(question.get("options") or []),
                why=question.get("why") or "", task=task, context=self.context_of(task),
            )  # fmt: skip
            reason = verdict.parked
            if not reason:
                reason = self.basis_still(task, verdict, offered)
        run = self.run() or {}
        current = self.runtime.asks.owner_question(question["id"])
        if not current or current.get("at") != question.get("at"):
            return  # Answered (by the owner, the Router) meanwhile, or asked again.
        if reason:
            with self.store.transaction():
                entry = self.book.append(
                    run.get("id", ""), PARKED, f"Set aside “{text[:120]}”: {reason}",
                    state=PARKED, subject=f"question:{question['id']}",
                    task_id=(task or {}).get("id", ""),
                    why={"rule": "parked", "reason": reason, "judge": verdict.reason},
                    payload={"question": text},
                )  # fmt: skip
                self.store.set(f"autopilot:setaside:{question['id']}",
                               json.dumps({"why": reason, "entry": entry["id"]}))  # fmt: skip
            self.runtime.publish_state()
            return
        because = self.because(verdict, offered)
        with self.store.transaction():
            entry = self.book.append(
                run.get("id", ""), "answered", f"Answered “{text[:120]}”: {verdict.answer}",
                subject=f"question:{question['id']}", task_id=(task or {}).get("id", ""),
                why={"rule": "owner-basis", "basis": list(verdict.basis), "judge": verdict.reason},
                payload={"question": text, "answer": verdict.answer},
            )  # fmt: skip
            self.store.delete(f"ownerq:{question['id']}")
            self.book.queue(run.get("id", ""), entry["id"], "worker", {
                "session_id": row["id"], "task_id": (task or {}).get("id", ""),
                "text": f"Your question: {text}\n\nAnswered for the owner by Autopilot (they are "
                        f"away), from what they said: {verdict.answer}. "
                        f"{because[0].upper() + because[1:]}.",
                "display": f"Autopilot answered: {verdict.answer}, {because}.",
                "go_on": True,
            }, self.book.generation())  # fmt: skip
        # The Router hears it is no longer open (with its next message), as when a worker moves on.
        self.runtime.asks.closed(row["id"], [question], "")
        self.runtime.publish_state()
        await self.dispatch()

    # The outbox: a message to a worker.

    async def deliver_worker(self: Autopilot, item: dict) -> str:  # type: ignore[misc]
        """An answer, a decision, a nudge: in Autopilot's envelope, never the owner's; its turn
        carries the entry's origin. Waits (stays queued) while the owner's work wants a slot."""
        payload: dict[str, Any] = item["payload"]
        row = self.store.find_session(payload["session_id"])
        if not row or row["closed_at"]:
            return "its worker was closed"
        task = self.runtime.tasks.find(payload["task_id"]) if payload.get("task_id") else None
        asks = self.store.settings().autopilot_ask_first
        origin = {"entries": [item["entry"]], "ask_first": asks}
        attributes = {"task": task["id"]} if task else {}
        if payload.get("decision"):
            attributes["decision"] = payload["decision"]
        prompt = wrap("system", "autopilot", payload["text"], **attributes)
        meta = {"autopilot": {"entry": item["entry"], "run": item["run_id"]}, "outbox": item["id"],
                **({"task_id": task["id"]} if task else {})}  # fmt: skip
        others = [o for o in self.store.open_sessions("worker") if o["id"] != row["id"]]
        if any(self.runtime.workers.wants_slot(other) for other in others):
            raise Later("the owner's work waits for a slot")
        self.origins.keep(row["id"], origin)
        if not await self.ready(row):
            raise Later("its worker is at work in a process that acts without asking")
        if task and payload.get("go_on") and task["status"] == "waiting":
            self.runtime.asks.go_on(task, chat=False)
        with self.origins.carrying(origin):
            message = await self.runtime.delivery.deliver(
                row["id"], prompt, kind="notice", display=payload["display"], meta=meta
            )
        self.book.stored(item["id"], message["id"])
        return ""
