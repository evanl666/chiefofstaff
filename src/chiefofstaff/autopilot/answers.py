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
from .logbook import ACTIVE, PARKED, UNDONE, WITHDRAWN
from .origin import CARRIED

if TYPE_CHECKING:
    from .core import Autopilot

logger = logging.getLogger(__name__)

# The longest a card waits, not drawn, while it is judged; and the judge's own time.
DECIDING = 120.0
JUDGE_SECONDS = 110.0
# How long a worker opened again asking first waits for a slot taken for a send meanwhile to be
# given back (reopen_asking): REOPEN_WAITS of REOPEN_PAUSE seconds.
REOPEN_PAUSE = 0.05
REOPEN_WAITS = 200
# Card kinds it answers in a worker's turn, and those it only parks.
ANSWERED = frozenset({"question"})
PARKED_ONLY = frozenset({"form", "link"})
# What the judge's helper session is called (store.py HELPER_KINDS).
JUDGE = "judge"
# A session at work (its turn not over).
BUSY = frozenset({"queued", "starting", "working"})
# A report question asking for one of these waits for the owner, whatever was said.
OWNER_ONLY = ("access", "a key", "the key", "money", "budget", "account", "sign in", "log in")


# Why what was in flight when Autopilot stopped goes to the owner instead.
STOPPED = "Autopilot was stopped before it decided"


class Later(Exception):  # noqa: N818  (not an error: delivered later)
    """A delivery that waits (stays queued): the owner's work goes first."""


class Withdrawn(Exception):  # noqa: N818  (not an error: Stop came first)
    """A delivery Stop overtook after its claim: never stored for its session."""


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

    async def ask_judge(self: Autopilot, text: str,  # type: ignore[misc]
                        ticket: tuple[str, int] | None = None) -> judge.ProposeAnswer | None:  # type: ignore[misc]  # fmt: skip
        """The judge's proposal for one item (None when it gave none in time, or Stop came
        while another item was judged before it: never asked then)."""
        async with self.judge_lock:
            if ticket is not None and not self.holds(ticket):
                return None
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self.proposal = future
            token = CARRIED.set(None)  # The judge is Autopilot's own, never continued work.
            started = datetime.now(UTC).timestamp()
            session_id = ""
            try:
                for _ in range(60):  # Its last turn ending (it ends once it proposed): 30 s.
                    last = self.runtime.toolset.last_special(JUDGE)
                    if future.done() or not last or last["status"] not in BUSY:
                        break
                    await asyncio.sleep(0.5)
                if future.done():
                    return None  # Stopped meanwhile (abandon).
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
        # Its task is over with its proposal: its turn ends there (no report owed, so no turn
        # after it that would keep the next item from being judged).
        task = self.runtime.tasks.current(caller)
        if task and task["status"] not in {"done", "cancelled"}:
            self.runtime.tasks.move(task["id"], "done", result="Proposed an answer.")
        return "Proposed. You are done: end your turn, and do not call report."

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
        ticket: tuple[str, int] | None = None,
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
        proposal = await self.ask_judge(text, ticket)
        if proposal is None:
            return judge.Verdict(parked="it couldn't be judged just now"), offered
        cited = [cite.model_dump() for cite in proposal.basis]
        refusal = owner.refusal(self.runtime, task, cited, offered, proposal.answer, options)
        verdict = judge.accepted(proposal, question=question, options=options, offered=offered,
                                 basis_refusal=refusal)  # fmt: skip
        return verdict, offered

    def basis_still(self: Autopilot, task: dict, verdict: judge.Verdict,  # type: ignore[misc]
                    offered: list[owner.Item]) -> str:  # type: ignore[misc]  # fmt: skip
        """Its basis checked again just before the answer is delivered."""
        return owner.refusal(self.runtime, task, list(verdict.basis), offered, verdict.answer,
                             verdict.options)  # fmt: skip

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

    def given(self: Autopilot, task_id: str, ids: list[int]):  # type: ignore[misc]
        """The owner's messages to the Router a task was given by their refs ({owner:<ref>}):
        in its scope from then on (basis.py). Kept only while its line is ticked."""
        if not self.ticked or not task_id or not ids:
            return
        key = f"{owner.REFS}{task_id}"
        kept = set(json.loads(self.store.get(key) or "[]"))
        if not set(ids) <= kept:
            self.store.set(key, json.dumps(sorted(kept | set(ids))))

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
        ticket = self.ticket()  # What it acts for: filed only if still so (holds).
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
                                context=self.context_of(task), ticket=ticket),
                    DECIDING,
                )  # fmt: skip
            reason = verdict.parked or ("" if verdict.answer else "it couldn't be judged in time")
        try:
            if answer.done():
                return  # The owner answered it first.
            if not reason and task:
                reason = self.basis_still(task, verdict, offered)
            if not self.holds(ticket):
                self.release_card(row, draft, task, ticket)
            elif reason:
                self.park_card(row, draft, answer, task, reason, verdict.reason, ticket=ticket)
            elif not self.answer_card(row, draft, answer, task or {}, verdict, offered, ticket):
                self.release_card(row, draft, task, ticket)
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

    def reopen_first(self: Autopilot, row: dict, carried: dict | None = None,  # type: ignore[misc]
                     claimed: bool = False) -> str:  # type: ignore[misc]  # fmt: skip
        """Whether a worker on another agent must be opened again asking first before an origin
        that asks first reaches it (`carried`, else what it carries): "" no (on Copilot, which
        takes the checks per request; asking first already; no process open, so it opens
        asking first), "now" (not at work), or "after" (at work: once its turn is over, never
        mid-turn). `claimed`: its slot was just taken for this send (Workers.claim_slot), which
        it gets only with no turn holding it: then only a turn its agent began and has not
        settled (Runtime.begun), or a command it left running, is one at work. Never the owner's
        saved setting."""
        if row.get("role") != "worker" or self.runtime.agent(row["id"]) == COPILOT_AGENT:
            return ""
        origin = carried or self.origins.sticky.get(row["id"])
        if not origin or not origin.get("ask_first"):
            return ""
        state = self.runtime.states[row["id"]]
        if state.opened_asking is not False or not state.handle:
            return ""
        turn = state.held or row["id"] in self.runtime.begun
        if not claimed:
            turn = turn or state.busy or row["status"] in {"working", "starting"}
        return "after" if turn else "now"

    async def reopen_now(self: Autopilot, session_id: str):  # type: ignore[misc]
        """Its process ends (nothing of it at work: as Runtime.agent_mode_changed does it for one
        at rest, never stopping a turn); its next message opens it in a new one, asking first."""
        await self.runtime.detach(session_id)
        backend, _ = self.runtime.connection(self.runtime.agent(session_id))
        if retire := getattr(backend, "retire", None):
            await retire(session_id)

    async def reopen_asking(self: Autopilot, row: dict, carried: dict):  # type: ignore[misc]
        """A worker held what carries its origin (Delivery.held_to_ask_first): its process is
        opened again asking first, if it still must be and is still not at work, then it is
        given what it holds."""
        state = self.runtime.states[row["id"]]
        try:
            for _ in range(REOPEN_WAITS):
                fresh = self.store.find_session(row["id"])
                if not fresh or fresh["closed_at"]:
                    return
                if not self.reopen_first(fresh, carried):
                    break
                if self.reopen_first(fresh, carried, claimed=True) == "after":
                    state.confined = state.stale = True  # Its turn began meanwhile: after it.
                    return
                if not state.busy and fresh["status"] not in {"working", "starting"}:
                    await self.reopen_now(row["id"])
                    break
                # Its slot taken for a send meanwhile: that send is held (reopen_asking) and
                # gives it back.
                await asyncio.sleep(REOPEN_PAUSE)
            else:
                await self.reopen_now(row["id"])  # (A slot never given back: freed with it.)
            state.reopen_asking = False
            await self.runtime.delivery.resume(row["id"])
        finally:
            if not state.confined:
                state.reopen_asking = False
            if self.reopening.get(row["id"]) is asyncio.current_task():
                del self.reopening[row["id"]]

    async def ready(self: Autopilot, row: dict, origin: dict) -> bool:  # type: ignore[misc]
        """Before Autopilot continues a worker with `origin`: on another agent, opened again
        asking first (reopen_first) when not at work; at work, this waits (False)."""
        how = self.reopen_first(row, origin)
        if how == "after":
            return False
        if how == "now":
            await self.reopen_now(row["id"])
        return True

    def answer_card(self: Autopilot, row: dict, draft: dict, answer: asyncio.Future, task: dict,  # type: ignore[misc]
                    verdict: judge.Verdict, offered: list[owner.Item],  # type: ignore[misc]
                    ticket: tuple[str, int] | None) -> bool:  # type: ignore[misc]  # fmt: skip
        """Its answer into the turn, filed under the run it was judged in, if that still holds
        (else nothing: False)."""
        because = self.because(verdict, offered)
        with self.store.transaction():
            if not ticket or not self.holds(ticket):
                return False
            entry = self.book.append(
                ticket[0], "answered",
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
        return True

    def release_card(self: Autopilot, row: dict, draft: dict, task: dict | None,  # type: ignore[misc]
                     ticket: tuple[str, int] | None):  # type: ignore[misc]  # fmt: skip
        """Stopped while it was judged: the card is the owner's, drawn as if Autopilot had never
        held it (its turn waits for their answer), saying why; nothing answered."""
        if ticket:
            with self.store.transaction():
                entry = self.book.append(
                    ticket[0], PARKED, f"Set aside “{draft['text'][:120]}”: {STOPPED}",
                    state=PARKED, subject=f"card:{draft['id']}",
                    task_id=(task or {}).get("id", ""),
                    why={"rule": "parked", "reason": STOPPED},
                    payload={"question": draft["text"], "kind": draft["kind"]},
                )  # fmt: skip
                self.store.set(f"autopilot:setaside:{draft['id']}",
                               json.dumps({"why": STOPPED, "entry": entry["id"]}))  # fmt: skip
        self.deciding.discard(draft["id"])
        self.runtime.asks.draw(row, draft)

    def park_card(self: Autopilot, row: dict, draft: dict, answer: asyncio.Future,  # type: ignore[misc]
                  task: dict | None, reason: str, judged: str = "",  # type: ignore[misc]
                  ticket: tuple[str, int] | None = None):  # type: ignore[misc]  # fmt: skip
        """Its turn goes on without it; it waits under Needs you, saying why, drawn once."""
        kind = draft["kind"]
        with self.store.transaction():
            entry = self.book.append(
                ticket[0] if ticket else "", PARKED, f"Set aside “{draft['text'][:120]}”: {reason}",
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
        ticket = self.ticket()  # What it acts for: filed only if still so (holds).
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
                                    context=self.context_of(task), ticket=ticket),
                        DECIDING,
                    )  # fmt: skip
                reason = verdict.parked or ("" if verdict.answer else "it wasn't judged in time")
            card = self.store.draft(card["id"])
            if card["status"] != "pending":
                return  # The owner (or the Router) decided it first.
            if not reason and task:
                reason = self.basis_still(task, verdict, offered)
            if not self.holds(ticket):
                self.park_decision(row, card, task, STOPPED, ticket=ticket)
            elif reason:
                self.park_decision(row, card, task, reason, verdict.reason, ticket=ticket)
            elif not self.decide(row, card, task or {}, verdict, offered, ticket):
                self.park_decision(row, card, task, STOPPED, ticket=ticket)
        finally:
            self.deciding.discard(card["id"])
            self.runtime.publish_state()

    def decide(self: Autopilot, row: dict, card: dict, task: dict, verdict: judge.Verdict,  # type: ignore[misc]
               offered: list[owner.Item], ticket: tuple[str, int] | None) -> bool:  # type: ignore[misc]  # fmt: skip
        """Its decision, filed under the run it was judged in if that still holds (else nothing:
        False; True too when the owner decided first)."""
        target = card["target"]
        because = self.because(verdict, offered)
        with self.store.transaction():
            fresh = self.store.draft(card["id"])
            if fresh["status"] != "pending":
                return True
            if not ticket or not self.holds(ticket):
                return False
            run_id = ticket[0]
            entry = self.book.append(
                run_id, "decided", f"“{target['question'][:120]}” → {verdict.answer}",
                subject=f"card:{card['id']}", task_id=task.get("id", ""),
                why={"rule": "owner-basis", "basis": list(verdict.basis), "judge": verdict.reason},
                payload={"question": target["question"], "options": target.get("options") or [],
                         "choice": verdict.answer},
            )  # fmt: skip
            decided = self.book.decide(
                run_id=run_id, entry_id=entry["id"], card_id=card["id"],
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
                self.book.queue(run_id, entry["id"], "worker", {
                    "session_id": asker["session_id"], "task_id": asker.get("task_id") or "",
                    "text": said, "display": f"Autopilot decided: {verdict.answer}",
                    "decision": decided, "go_on": True,
                }, ticket[1])  # fmt: skip
        self.runtime.bus.publish("draft", self.store.draft(card["id"]))
        self.runtime.background(self.dispatch())
        return True

    def park_decision(self: Autopilot, row: dict, card: dict, task: dict | None, reason: str,  # type: ignore[misc]
                      judged: str = "", ticket: tuple[str, int] | None = None):  # type: ignore[misc]  # fmt: skip
        """It stays for the owner, now drawn, with why; its worker goes on meanwhile."""
        with self.store.transaction():
            entry = self.book.append(
                ticket[0] if ticket else "", PARKED,
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

    def change(self: Autopilot, entry_id: str) -> dict:  # type: ignore[misc]
        """*Change* on a decision it made: the card again, for the owner; their answer is
        recorded as theirs and every task that asked is told (Decisions.answer)."""
        entry = self.book.entry(entry_id)
        decided = self.book.decisions(card_id=(entry or {}).get("subject", "").partition(":")[2])
        if not entry or entry["kind"] != "decided" or not decided:
            raise ValueError("Only a decision Autopilot made can be changed here.")
        decision = decided[-1]
        if decision["status"] != "active":
            raise ValueError("You changed it already.")
        card = self.store.draft(decision["card_id"])
        askers = [a for a in decision["askers"] if self.store.find_session(a["session_id"])]
        if not askers:
            raise ValueError("The workers that asked are gone: there's nothing to change.")
        target = {**card["target"], "askers": askers, "changes": decision["id"],
                  "previous": decision["choice"]}  # fmt: skip
        fresh = self.store.create_draft(askers[0]["session_id"], "decision", target, card["text"])
        self.runtime.add_message(askers[0]["session_id"], "draft", card["text"],
                                 {"draft_id": fresh["id"], "label": "Decision"})  # fmt: skip
        self.runtime.bus.publish("draft", fresh)
        return fresh

    def decision_changed(self: Autopilot, decision_id: str, choice: str):  # type: ignore[misc]
        """The owner answered its *Change* card: theirs now, Autopilot's no longer in force."""
        decision = self.book.decision(decision_id)
        if not decision or not self.book.change_decision(decision_id, "owner"):
            return
        self.book.append(
            decision["run_id"], UNDONE, f"Changed by you: → {choice}", state=UNDONE,
            of=decision["entry"], subject=f"card:{decision['card_id']}",
            why={"rule": "owner", "by": "you", "ingress": "page"},
            payload={"was": decision["choice"], "now": choice},
        )  # fmt: skip
        self.runtime.publish_state()

    # A6: a question a worker reported, once the Router's turn on it is over.

    async def check_questions(self: Autopilot):  # type: ignore[misc]
        """Questions under Needs you that came in this run, the Router's turn over: judged."""
        run = self.run()
        ticket = self.ticket()
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
            await self.decide_question(row, question, ticket)
            if not self.holds(ticket):
                return  # Stopped: the rest wait for the owner.

    async def decide_question(self: Autopilot, row: dict, question: dict,  # type: ignore[misc]
                              ticket: tuple[str, int] | None = None):  # type: ignore[misc]  # fmt: skip
        ticket = ticket or self.ticket()
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
                ticket=ticket,
            )  # fmt: skip
            reason = verdict.parked
            if not reason:
                reason = self.basis_still(task, verdict, offered)
        current = self.runtime.asks.owner_question(question["id"])
        if not current or current.get("at") != question.get("at"):
            return  # Answered (by the owner, the Router) meanwhile, or asked again.
        if not ticket:
            return
        run_id = ticket[0]
        if not self.holds(ticket):
            reason = STOPPED.replace("decided", "answered")
        if reason:
            with self.store.transaction():
                entry = self.book.append(
                    run_id, PARKED, f"Set aside “{text[:120]}”: {reason}",
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
            if not self.holds(ticket):
                return  # (Not reached: nothing awaited since holds was read.)
            entry = self.book.append(
                run_id, "answered", f"Answered “{text[:120]}”: {verdict.answer}",
                subject=f"question:{question['id']}", task_id=(task or {}).get("id", ""),
                why={"rule": "owner-basis", "basis": list(verdict.basis), "judge": verdict.reason},
                payload={"question": text, "answer": verdict.answer},
            )  # fmt: skip
            # Under Needs you until the answer is stored for its worker (deliver_worker).
            self.book.queue(run_id, entry["id"], "worker", {
                "session_id": row["id"], "task_id": (task or {}).get("id", ""),
                "text": f"Your question: {text}\n\nAnswered for the owner by Autopilot (they are "
                        f"away), from what they said: {verdict.answer}. "
                        f"{because[0].upper() + because[1:]}.",
                "display": f"Autopilot answered: {verdict.answer}, {because}.",
                "go_on": True,
                "question": {"id": question["id"], "at": question.get("at", ""), "text": text,
                             "task_id": question.get("task_id") or ""},
            }, ticket[1])  # fmt: skip
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
        if not await self.ready(row, origin):
            raise Later("its worker is at work in a process that acts without asking")

        def holds():
            """Stop since its claim withdraws it (Withdrawn); nothing else is done here."""
            if not self.ticked or self.book.generation() != item["generation"]:
                raise Withdrawn("Autopilot was stopped before it went out.")

        def still():
            """In the transaction that stores it for the worker (nothing awaited after): still
            Autopilot's to give (else withdrawn); then what it answers leaves Needs you, and the
            worker carries its origin from here, its task going on."""
            holds()
            if asked := payload.get("question"):
                current = self.runtime.asks.owner_question(asked["id"])
                if not current or current.get("at") != asked.get("at"):
                    raise Withdrawn("Its question was answered before it went out.")
                self.store.delete(f"ownerq:{asked['id']}")
                self.store.delete(f"autopilot:setaside:{asked['id']}")
                # The Router hears it is no longer open (with its next message).
                self.runtime.asks.closed(row["id"], [asked], "")
            decided = payload.get("decision")
            if decided and (self.book.decision(decided) or {}).get("status") != ACTIVE:
                raise Withdrawn("Its decision was changed before it went out.")
            self.origins.keep(row["id"], origin)
            fresh = self.runtime.tasks.find(task["id"]) if task else None
            if fresh and payload.get("go_on") and fresh["status"] == "waiting":
                self.runtime.asks.go_on(fresh, chat=False)

        holds()
        with self.origins.carrying(origin):
            message = await self.runtime.delivery.deliver(
                row["id"], prompt, kind="notice", display=payload["display"], meta=meta,
                check=still,
            )  # fmt: skip
        self.book.stored(item["id"], message["id"])
        if payload.get("question"):
            self.runtime.publish_state()
        return ""

    def draw_returned(self: Autopilot, rows: list[dict]):  # type: ignore[misc]
        """What withdrawn answers were for, the owner's again (Logbook.give_back): each card
        drawn for them, with its notification, as one set aside is; Needs you says why."""
        drawn = set()
        for row in rows:
            decision = self.book.decision((row.get("payload") or {}).get("decision") or "")
            if not decision or decision["status"] != WITHDRAWN or decision["card_id"] in drawn:
                continue
            card = self.store.draft(decision["card_id"])
            if not card or card["status"] != "pending":
                continue
            drawn.add(card["id"])
            asker = (decision["askers"] or [{}])[0].get("session_id") or ""
            session = self.store.find_session(asker or row["payload"].get("session_id", ""))
            if session and not session["closed_at"]:
                self.runtime.add_message(session["id"], "draft", card["text"],
                                         {"draft_id": card["id"], "label": "Decision"})  # fmt: skip
                self.runtime.alert(card["text"], session_id=session["id"], needs_action=True,
                                   title=f"Decision · {session['title']}")  # fmt: skip
            self.runtime.bus.publish("draft", card)
        if rows:
            self.runtime.publish_state()
