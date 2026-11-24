"""Delivering messages to sessions: stored first, then sent with whatever else is pending.

Every message for a session is a row in `messages` whose meta holds the exact prompt. A session's
`sent:` mark is the id of the last message it was given, so whatever comes after it is its
outbox.

A send is bracketed by a `sending:` marker (how far it goes and a digest of the text). If
Chief of Staff stops in between, the next start checks the session's own history: when the
text arrived, only the mark moves; when it did not, the messages go out again. Without a
readable history (a backend without get_events) delivery falls back to at-least-once.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Callable
from contextlib import nullcontext, suppress
from typing import TYPE_CHECKING

from . import agents, compat
from .autopilot.origin import TAG, merged
from .backend import (
    COPILOT_AGENT,
    NotStartedError,
    big_log,
    logged_event,
    read_log,
    received_in_log,
    running_background,
    stop_background,
    take_queued,
)
from .envelope import news_tasks, tasks_named
from .roles import ROUTER, SLACK, WORKER
from .settings_guard import SettingsChangedError
from .sources.slack import describe

if TYPE_CHECKING:
    from .backend import SendMode
    from .runtime import Runtime

logger = logging.getLogger(__name__)

# Events for the Router and the Slack session wait this long, so a burst becomes one turn.
EVENT_DEBOUNCE = float(os.environ.get("CHIEFOFSTAFF_EVENT_DEBOUNCE", "2"))

# Held messages that could not be sent are retried after this, doubling up to a minute.
RETRY_DELAY = 2.0

# A message that starts a turn shows in the session's conversation at once (Copilot writes it
# down before its first step). One that does not after this long was never started on.
START_CHECK = float(os.environ.get("CHIEFOFSTAFF_START_CHECK", "90"))

# A message sent to a session shows in its conversation within seconds (a steer, at its next
# step; one queued behind its turn waits there, which Copilot can say). One that is neither
# shown nor waiting after this long was lost: it is sent again, up to ECHO_RESENDS times.
ECHO_CHECK = float(os.environ.get("CHIEFOFSTAFF_ECHO_CHECK", "20"))
ECHO_RESENDS = 2
# Kinds of messages whose delivery the chats show as it changes (the owner's and the Router's
# words to a session; not every event batched to the Router).
TRACKED = {"owner", "router"}

# Said when a command a session left running in the background held its messages and was
# stopped for them.
HELD_STOPPED = (
    "Stopped a command it left running in the background (a server, a watcher): Copilot held "
    "the messages sent to it until that command ended."
)

# Errors of a send to a Copilot CLI that stopped (crashed, killed): the connection is gone; or
# that did not start.
CONNECTION_LOST = (BrokenPipeError, ConnectionResetError, EOFError, NotStartedError)


def reason(error: BaseException) -> str:
    """Why a send failed, in a line (not the CLI's whole output), without a closing period."""
    lines = describe(error).strip().splitlines() or [type(error).__name__]
    return lines[0].strip().rstrip(".")[:300]


def refused_for_version(error: BaseException | None) -> bool:
    """Whether an error (or one it came from) is Copilot refusing to open a session for a
    version mismatch (compat.MISMATCHES: the extensions it asks for among them)."""
    for _ in range(10):  # A chain of causes, not a loop.
        if error is None:
            return False
        if compat.mismatch(error):
            return True
        error = error.__cause__ or error.__context__
    return False


class FreshStartError(RuntimeError):
    """A session Copilot refused to reopen for a version mismatch (its extensions, say), which no
    fresh Copilot session could go on for either (Runtime.continue_fresh): what it was sent
    waits for the owner's Start fresh (Runtime.start_fresh). `fresh`: the session."""

    def __init__(self, text: str, session_id: str):
        super().__init__(text)
        self.fresh = session_id


class TooLargeError(RuntimeError):
    """A Copilot session too large for Copilot to reopen here (Runtime.too_large): never
    resumed; what it is sent waits for the owner's Start fresh, as FreshStartError's."""


def connection_lost(error: BaseException | None) -> bool:
    """Whether an error (or one it came from) says the Copilot CLI's connection ended."""
    for _ in range(10):  # A chain of causes, not a loop.
        if error is None:
            return False
        if type(error).__name__ == "ProcessExitedError" or isinstance(error, CONNECTION_LOST):
            return True
        error = error.__cause__ or error.__context__
    return False


class Delivery:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.states = runtime.states
        # What each big Copilot session's log showed it received (conversation): Copilot
        # session id -> (where that read ended, its user messages).
        self.logs_read: dict[str, tuple[int, list[dict]]] = {}

    async def conversation(self, session_id: str, session, latest: bool = False) -> list:
        """A session's history as get_events gives it, of which its user messages are read
        (what it received). Asked of the session; but a Copilot session whose log is
        backend.BIG_LOG or more is never asked for it whole (an answer that size killed
        Copilot's connection): its user messages are read from that log instead, from where
        the last read ended; or, `latest`, the events of its log's latest part (LOG_TAIL)."""
        if self.runtime.agent(session_id) == COPILOT_AGENT:
            backend, _ = self.runtime.connection(COPILOT_AGENT)
            copilot = getattr(session, "session_id", "") or self.runtime.copilot_id(session_id)
            log = backend.session_log(copilot) if backend else None
            if log and await asyncio.to_thread(big_log, log):
                if latest:
                    events, _, _ = await asyncio.to_thread(read_log, log, 0)
                    return [logged_event(event) for event in events]
                known = self.logs_read.get(copilot, (0, []))
                read = await asyncio.to_thread(received_in_log, log, known)
                self.logs_read[copilot] = read
                return [logged_event(event) for event in read[1]]
        return await session.get_events()

    async def deliver(
        self,
        session_id: str,
        prompt: str,
        *,
        kind: str,
        display: str | None = None,
        meta: dict | None = None,
        steer: bool = False,
        batch: bool = False,
        check: Callable[[], None] | None = None,
    ) -> dict:
        """Give a session a message: stored first, then sent with what else it has not received.

        `steer` (the owner's messages) reaches a session that is working within its current
        turn. `batch` (events and reports for the Router or the Slack session) waits until
        the session is idle and a short burst has passed, so several become one turn. `check`
        runs just before it is stored (nothing awaited between): it raises to store nothing.
        """
        row = self.store.session(session_id)
        if row["closed_at"]:
            raise ValueError("That session is closed.")
        if row["role"] == WORKER and self.runtime.shepherd.marked(session_id):
            # In a shepherd step: it stops first; nothing else reaches the step's session.
            await self.runtime.shepherd.yield_to(session_id)
        if row["role"] == WORKER and not self.states.handle(session_id):
            # Not attached: what it did in a terminal meanwhile goes first, before this.
            await self.runtime.workers.catch_up(row)
        # The exact prompt is kept so a queued worker or a restart can replay it. With a
        # `check`, in one transaction with what it does (all of it stored, or none).
        with self.store.transaction() if check is not None else nullcontext():
            if check is not None:
                check()
            message = self.runtime.add_message(
                session_id,
                kind,
                prompt if display is None else display,
                {**(meta or {}), "prompt": prompt},
            )
        if row["role"] == WORKER:
            # A worktree removed under it (by hand, a cleanup) is back before it reads this.
            with suppress(Exception):
                await self.runtime.workspaces.restore(row)
            if await self.runtime.settings_refusal(row):
                # Its folder's agent settings changed (Runtime.settings_refusal): no turn starts
                # there until the owner says to use them; this waits for it until then.
                await self.runtime.settings_stop(session_id)
                return message
            # A server of its folder's changed or was let again: opened anew for this turn.
            await self.runtime.servers_turn(session_id)
            self.rest_after(row)
        if batch and row["role"] in {ROUTER, SLACK}:
            if row["status"] == "working":
                return message  # It gets this with the next flush, when it is idle.
            if EVENT_DEBOUNCE > 0:
                self.schedule_flush(session_id)
                return message
        if row["role"] == WORKER and self.states[session_id].confined:
            # Its sandbox went on during the turn it is in: kept for its sandboxed session,
            # which opens once that turn is over (Runtime.reattach).
            return message
        claimed = not self.states[session_id].busy
        if row["role"] == WORKER and not await self.runtime.workers.claim_slot(session_id):
            return message
        # A worker that asked the owner (waiting) while its turn goes on is at work in that turn
        # as much as one working: steered, the owner's answer reaches it there (R1: queued
        # behind its turn, it was read 43 minutes later, after a card built without it).
        asked = row["role"] == WORKER and row["status"] == "waiting" and not claimed
        working = (row["status"] == "working" or asked) and self.states.is_live(session_id)
        mode = (
            "immediate" if steer and working and not self.states[session_id].retuned else "enqueue"
        )
        if row["role"] == WORKER and self.states[session_id].after_abort:
            # Stopped: Copilot may hold the session busy (a command it left running) and keep a
            # queued message until then. Steered, it takes it at once.
            mode = "immediate"
        agent = self.runtime.agent(session_id)
        backend, connection = self.runtime.connection(agent)
        try:
            sent = await self.flush(
                session_id,
                await self.runtime.session_for(session_id),
                mode,
                busy=working,
                # In a turn still (it reported, its turn goes on: a final push): this waits for
                # that turn's end, it does not start one (no check that it started).
                joins=not claimed,
            )
        except asyncio.CancelledError:
            if claimed:
                self.states[session_id].busy = False
                self.runtime.background(self.runtime.workers.pump())
            raise
        except SettingsChangedError:
            # Its folder's settings changed as it opened: this waits, as above.
            if claimed:
                self.states[session_id].busy = False
                self.runtime.background(self.runtime.workers.pump())
            self.runtime.background(self.runtime.settings_stop(session_id))
            return message
        except Exception as exc:
            if claimed or row["role"] != WORKER:
                self.states[session_id].busy = False
                self.runtime.background(self.runtime.workers.pump())  # Its slot is free.
            if self.copilot_gone(exc, backend, connection, agent):
                # Kept to send: it goes out once Copilot runs again (Runtime.resume_working).
                self.copilot_down(session_id)
                return message
            if agent == COPILOT_AGENT and (
                refused_for_version(exc) or isinstance(exc, TooLargeError)
            ):
                raise self.fresh_needed(row) from exc  # Kept for it, unlike any other.
            # Not replayed later: the caller sees the error (a watcher re-polls the event).
            self.store.update_message_meta(message["id"], meta or {})
            self.runtime.add_message(session_id, "error", f"Delivery failed: {reason(exc)}.")
            raise RuntimeError(f"Could not deliver the message: {reason(exc)}.") from exc
        if sent:
            self.runtime.update_session(session_id, status="working")
        return message

    def schedule_flush(self, session_id: str, delay: float = EVENT_DEBOUNCE, attempt: int = 0):
        """Flush once after `delay`, collecting whatever arrives meanwhile; retry on failure."""
        state = self.states[session_id]
        if state.flush_scheduled:
            return
        state.flush_scheduled = True

        async def later():
            await asyncio.sleep(delay)
            self.states[session_id].flush_scheduled = False
            row = self.store.find_session(session_id)
            if not row or row["closed_at"] or row["status"] == "working":
                return  # Closed, or busy: it gets them when idle.
            if not self.runtime.ready():
                if self.runtime.status["state"] == "starting" and not self.runtime.stopping:
                    # Connecting: its resume (before it is ready) may have come too early.
                    self.schedule_flush(session_id, 0.5, attempt)
                return  # Stopping or reconnecting: they are sent once connected (resume).
            agent = self.runtime.agent(session_id)
            backend, connection = self.runtime.connection(agent)
            if agent == COPILOT_AGENT and backend is None and (why := self.runtime.copilot_down()):
                # Chief of Staff runs without Copilot now: kept until Copilot runs again
                # (Runtime.recover resumes it).
                self.copilot_down(session_id, why)
                return
            try:
                if await self.flush(session_id, await self.runtime.session_for(session_id)):
                    self.runtime.update_session(session_id, status="working")
            except Exception as exc:
                logger.warning("Could not deliver held messages to %s.", session_id, exc_info=True)
                if self.copilot_gone(exc, backend, connection, agent):
                    self.copilot_down(session_id)  # Not Copilot's own output: it reconnects.
                elif agent == COPILOT_AGENT and (
                    refused_for_version(exc) or isinstance(exc, TooLargeError)
                ):
                    self.fresh_needed(row)
                    return  # Kept until the owner starts it fresh: tried again, it would fail.
                elif attempt == 0:
                    failed = reason(exc)
                    note = self.runtime.add_message(
                        session_id,
                        "error",
                        f"Delivery failed: {failed}. Retrying.",
                        {"undelivered": f"Delivery failed at first ({failed}); sent on a retry."},
                    )
                    self.note_undelivered(session_id, note["id"])
                self.schedule_flush(session_id, min(60, RETRY_DELAY * 2**attempt), attempt + 1)

        self.runtime.background(later())

    def fresh_needed(self, row: dict, why: str = "") -> FreshStartError:
        """A session Copilot can't reopen, nor a fresh one go on for: said once in its chat,
        plainly (`why` it can't, when said), with Start fresh; what it was sent kept for
        that."""
        who = "This worker's" if row["role"] == WORKER else "Its"
        why = why or "can't be reopened"
        text = f"{who} Copilot session {why}; your message is kept for a fresh start."
        if not self.store.get(f"freshneeded:{row['id']}"):
            note = self.runtime.add_message(row["id"], "error", text, {"fresh": True})
            self.store.set(f"freshneeded:{row['id']}", str(note["id"]))
        return FreshStartError(text, row["id"])

    def fresh_started(self, session_id: str):
        """Started fresh (Runtime.start_fresh): the line that asked for it says so."""
        if not (noted := self.store.get(f"freshneeded:{session_id}")):
            return
        self.store.delete(f"freshneeded:{session_id}")
        with suppress(LookupError):
            message = self.store.message(int(noted))
            self.store.update_message_meta(message["id"], {**message["meta"], "resolved": True})
            shown = self.store.message(message["id"]) | {"amended": True}
            self.runtime.bus.publish("message", shown)

    def copilot_gone(
        self, error: BaseException, backend: object, connection: int, agent: str
    ) -> bool:
        """Whether a send failed because the session's backend (Copilot's CLI, or its agent's)
        stopped (crashed, killed) or was reconnected meanwhile: what was not sent goes out once
        it runs again."""
        if backend is None:
            return False  # Not connected when it began: said as it is.
        running, connections = self.runtime.connection(agent)
        if running is not backend or connections != connection:
            return True
        alive = getattr(backend, "alive", None)
        return (callable(alive) and not alive()) or connection_lost(error)

    def copilot_down(self, session_id: str, why: str = ""):
        """Said once in the session's chat while its backend (Copilot, or its agent) is down
        (`why`: Copilot can't run while Chief of Staff runs on another agent): what it was given
        waits."""
        if self.store.get(f"undelivered:{session_id}"):
            return
        mark = int(self.store.get(f"sent:{session_id}") or 0)
        owner = any(m["kind"] == "owner" for m in self.store.prompts(session_id, after=mark))
        waiting = (
            ("Your message", "your message") if owner else ("What is waiting", "what was waiting")
        )
        agent = self.runtime.agent(session_id)
        if agent == COPILOT_AGENT and why:
            down = f"Copilot can't run now: {why.rstrip('.')}. {waiting[0]} goes out once it can."
            back = f"Copilot runs again; {waiting[1]} went out."
        elif agent == COPILOT_AGENT:
            down = f"Copilot stopped; reconnecting. {waiting[0]} goes out when it is back."
            back = f"Copilot stopped and reconnected; {waiting[1]} went out."
        else:
            name = agents.display(agent)
            down = f"{name} stopped; starting it again. {waiting[0]} goes out when it is back."
            back = f"{name} stopped and started again; {waiting[1]} went out."
        note = self.runtime.add_message(session_id, "notice", down, {"undelivered": back})
        self.note_undelivered(session_id, note["id"])

    def note_undelivered(self, session_id: str, message_id: int):
        noted = json.loads(self.store.get(f"undelivered:{session_id}") or "[]")
        self.store.set(f"undelivered:{session_id}", json.dumps([*noted, message_id]))

    def resolve_undelivered(self, session_id: str):
        """What was not sent went out: the lines that said so say it did."""
        noted = json.loads(self.store.get(f"undelivered:{session_id}") or "[]")
        self.store.delete(f"undelivered:{session_id}")
        for message_id in noted:
            try:
                message = self.store.message(message_id)
            except LookupError:
                continue
            meta = {**message["meta"], "resolved": True}
            text = meta.pop("undelivered", "") or message["text"]
            self.store.update_message_meta(message_id, meta)
            self.runtime.bus.publish(
                "message", self.store.update_message_text(message_id, text) | {"amended": True}
            )

    def hold(
        self, session_id: str, prompt: str, *, kind: str, display: str, meta: dict | None = None
    ) -> dict:
        """Give a session a message that rides along with its next one: worth knowing, not
        worth a turn of its own (what workers told each other, for the Router)."""
        return self.runtime.add_message(
            session_id, kind, display, {**(meta or {}), "prompt": prompt, "deferred": True}
        )

    def defer_pending(self, session_id: str):
        """Whatever it has not received yet rides along with its next message (it was stopped
        or its task cancelled): a restart must not start a turn for it. The chats say so (held)."""
        mark = int(self.store.get(f"sent:{session_id}") or 0)
        held = []
        for message in self.store.prompts(session_id, after=mark):
            if not message["meta"].get("deferred"):
                message["meta"] = {**message["meta"], "deferred": True}
                self.store.update_message_meta(message["id"], message["meta"])
                held.append(message)
        self.publish_delivery(session_id, held)

    def undefer(self, session_id: str) -> bool:
        """What was held back for its next message (it was stopped) is due now; whether any."""
        mark = int(self.store.get(f"sent:{session_id}") or 0)
        found = False
        for message in self.store.prompts(session_id, after=mark):
            if message["meta"].get("deferred"):
                kept = {k: v for k, v in message["meta"].items() if k != "deferred"}
                self.store.update_message_meta(message["id"], kept)
                found = True
        return found

    def unsee(self, session_id: str) -> bool:
        """Its turn was stopped (Copilot drops what it queued for that turn): what it was sent
        and never saw is sent again, with its next message."""
        state = self.states[session_id]
        state.echo_wait.clear()  # Sent again with its next message, under a new digest.
        firsts = [entry[2] for entry in state.unread.values()] + list(state.unseen.values())
        state.unread.clear()
        state.unread_orders.clear()
        state.unseen.clear()
        if not firsts:
            return False
        mark = int(self.store.get(f"sent:{session_id}") or 0)
        if min(firsts) - 1 < mark:
            self.store.set(f"sent:{session_id}", str(min(firsts) - 1))
            # Not sent after all, as the chats say (until it goes with its next message).
            self.publish_delivery(session_id, self.store.prompts(session_id, after=min(firsts) - 1))
        return True

    def drop_pending(self, session_id: str, task_id: str = "") -> bool:
        """What it has not received yet is not sent at all (its task was cancelled, or it was
        closed); `task_id`: only what is about that task. Whether it had received anything
        about that task before."""
        mark = int(self.store.get(f"sent:{session_id}") or 0)
        received = False
        dropped = []
        for message in self.store.prompts(session_id):
            if task_id and message["meta"].get("task_id") != task_id:
                continue
            if message["id"] <= mark:
                received = True
                continue
            kept = {k: v for k, v in message["meta"].items() if k != "prompt"}
            message["meta"] = {**kept, "dropped": True}
            self.store.update_message_meta(message["id"], message["meta"])
            dropped.append(message)
        self.publish_delivery(session_id, dropped)  # Not sent, nor waiting to be: nothing said.
        return received

    def unread(self, session_id: str, task_id: str = "") -> list[dict]:
        """What it was given that asks something of it (`instructs`) and that it has not read
        yet, oldest first: sent into its turn and not shown in its conversation yet, or not sent
        at all (waiting for its next message, a slot, its backend). `task_id`: about that task,
        or about none."""
        state = self.states[session_id]
        mark = int(self.store.get(f"sent:{session_id}") or 0)
        spans = [
            (first, entry[1] if (entry := state.echo_wait.get(key)) else mark)
            for key, (_, _, first) in state.unread.items()
        ]
        after = min([first for first, _ in spans] + [mark + 1]) - 1
        return [
            message
            for message in self.store.prompts(session_id, after=after)
            if (message["id"] > mark or any(a <= message["id"] <= b for a, b in spans))
            and instructs(message)
            and (not task_id or (message["meta"].get("task_id") or "") in {task_id, ""})
        ]

    def has_unsent(self, session_id: str) -> bool:
        """Whether it has messages to receive that are worth a turn (a deferred note, such as
        a cancellation, only rides along with the next one)."""
        mark = int(self.store.get(f"sent:{session_id}") or 0)
        return any(
            not m["meta"].get("deferred") for m in self.store.prompts(session_id, after=mark)
        )

    def held_to_ask_first(self, row: dict, pending: list[dict], claimed: bool) -> bool:
        """Whether what a worker is given waits for its process to open again asking first
        (autopilot/answers.py reopen_first): what it carries Autopilot's origin, asking first,
        and its process (another agent's) was opened acting on its own. Not at work: opened
        again now, then given it; at work: once its turn is over (never mid-turn). `claimed`:
        this send took its slot (not one joining a turn); held, it gives it back."""
        autopilot = self.runtime.autopilot
        carried = None
        for message in pending:
            carried = merged(carried, message["meta"].get(TAG))
        carried = carried or autopilot.origins.sticky.get(row["id"])
        if not carried:
            return False
        state = self.states[row["id"]]
        if state.reopen_asking:
            job = autopilot.reopening.get(row["id"])
            if job and not job.done():
                self.give_back(row["id"], claimed)
                return True  # Being opened again now.
            # Held for its turn's end (confined, which held it until then, went with its
            # process or that turn): looked at again as it is now.
            state.reopen_asking = False
        how = autopilot.reopen_first(row, carried, claimed=claimed)
        if not how:
            return False
        # It carries the origin from now (its next process opens asking first: Runtime.yolo).
        autopilot.origins.keep(row["id"], carried)
        state.reopen_asking = True
        self.give_back(row["id"], claimed)
        if how == "after":
            # Kept for its next process (Runtime.reattach, once its turn is over).
            state.confined = state.stale = True
            return True
        autopilot.reopening[row["id"]] = self.runtime.background(
            autopilot.reopen_asking(row, carried)
        )
        return True

    def give_back(self, session_id: str, claimed: bool):
        """A slot this send took (claimed), nothing sent: free until it is given what it holds."""
        if claimed and self.states[session_id].busy:
            self.states[session_id].busy = False
            self.runtime.background(self.runtime.workers.pump())

    async def flush(
        self,
        session_id: str,
        session,
        mode: SendMode = "enqueue",
        busy: bool = False,
        joins: bool = False,
    ) -> bool:
        """Send every prompt this session has not received yet, as one message. `busy`: it is
        in a turn, which this joins or follows (not a turn of its own starting now)."""
        async with self.states[session_id].sending:
            return await self.send_pending(session_id, session, mode, busy, joins)

    async def send_pending(
        self, session_id: str, session, mode: SendMode, busy: bool, joins: bool
    ) -> bool:
        """flush, with the session's `sending` lock held."""
        # The Router's Copilot session may have been replaced while this waited.
        session = self.states.handle(session_id) or session
        mark = int(self.store.get(f"sent:{session_id}") or 0)
        pending = self.store.prompts(session_id, after=mark)
        if not pending:
            return False
        worker = self.store.session(session_id)
        if worker["role"] == WORKER and self.states[session_id].confined:
            return False  # Kept for its sandboxed session (see deliver).
        if self.runtime.role_waiting(worker):
            # Its role waits for the owner (its agent can't run it; Ask me): kept until then.
            return False
        if worker["role"] == WORKER and await self.runtime.settings_refusal(worker):
            # No turn starts while its folder's agent settings are changed (see deliver).
            self.runtime.background(self.runtime.settings_stop(session_id))
            return False
        if worker["role"] == WORKER and self.held_to_ask_first(worker, pending, not joins):
            return False
        if any(TAG in message["meta"] for message in pending):
            # Messages that carry Autopilot's origin: so does the turn that takes them in.
            self.runtime.autopilot.origins.taken(worker, pending)
        prompts = [self.runtime.told_prompt(message) for message in pending]
        # What workers that work together told each other since: who and how often, with what
        # still waits (Teams.digest), riding along with this turn of the Router's.
        team_digest, digest_upto = (
            self.runtime.teams.digest() if worker["role"] == ROUTER else ("", 0)
        )
        if team_digest:
            prompts.insert(0, team_digest)
        # The owner's decisions its open tasks rely on that it was not given yet (none: no line).
        in_force, given = (
            self.runtime.ledger.in_force(session_id, "\n\n".join(prompts))
            if worker["role"] == WORKER
            else ("", [])
        )
        if in_force:
            prompts.insert(0, in_force)
        if preamble := self.store.get(f"preamble:{session_id}"):
            prompts.insert(0, preamble)  # A new Router's handover, before what is new.
        text = "\n\n".join(prompts)
        marker = {"upto": pending[-1]["id"], "digest": digest(text)}
        self.store.set(f"sending:{session_id}", json.dumps(marker))
        row = self.store.session(session_id)
        state = self.states[session_id]
        if state.held:
            # Its turn is over, but a command it left running holds what Copilot queues for
            # it: sent at once, this starts a turn.
            mode = "immediate"
        mid_turn = (
            row["role"] == WORKER
            and (row["status"] == "working" or (busy and row["status"] == "waiting"))
            and state.echoes
        )
        attachments = [
            {"type": "file", "path": item["path"], "displayName": item["name"]}
            for message in pending
            for item in message["meta"].get("attachments") or []
        ]
        # A turn starts on it now: its conversation shows it soon, or Copilot gave up on it.
        # Marked before the send (its echo may come before the send returns).
        starts = (
            self.runtime.backend_echoes(session_id)
            and not joins
            and not mid_turn
            and not (busy and state.echoes)
            and not state.after_abort
            and state.awaiting is None
        )
        if starts:
            state.awaiting = (marker["digest"], mark)
        try:
            if attachments:
                await session.send(text, mode=mode, attachments=attachments)
            else:
                await session.send(text, mode=mode)
        except BaseException:
            # The caller hears of the failure and the messages count as not delivered.
            self.store.delete(f"sending:{session_id}")
            if starts:
                state.awaiting = None
            raise
        self.runtime.ledger.given(session_id, given)
        if starts:
            asyncio.get_running_loop().call_later(
                START_CHECK,
                lambda: self.runtime.background(
                    self.check_started(session_id, session, marker["digest"])
                ),
            )
        if row["role"] == WORKER and any(m["meta"].get("thread") for m in pending):
            state.thread_turn, state.thread_nudged = True, False
        # What it is given now, not what rides along with it (instructions held back
        # while it was stopped are taken up when their turn comes).
        fresh = "\n\n".join(m["meta"]["prompt"] for m in pending if not m["meta"].get("deferred"))
        # The owner's chat about one of its tasks names none in its words: its turn is on the
        # task it is filed under all the same (Home names the worker by it: bug QA6-3).
        chats = [
            m["meta"]["task_id"]
            for m in pending
            if m["kind"] == "owner" and m["meta"].get("task_id") and not m["meta"].get("deferred")
        ]
        if (
            row["role"] == WORKER
            and not mid_turn
            and not busy
            and (named := tasks_named(fresh or text) or chats[-1:])
        ):
            # A turn on it starts now; sent while it works, once its conversation shows it
            # (a connection that shows none: when its turn on it comes, see working_on).
            self.runtime.took_up(session_id, named)
        elif (
            row["role"] == WORKER
            and not mid_turn
            and not busy
            and (named := news_tasks(fresh or text))
        ):
            # News on a thread it follows for a task: its turn is on that task.
            self.runtime.took_up(session_id, named, news=True)
        if row["role"] == WORKER:
            # The owner's chat about a task they stopped (its prompt names no task): once
            # it works on it, it runs again (TaskActions.resume_stopped).
            for message in pending:
                about = message["meta"].get("task_id") if message["kind"] == "owner" else ""
                if about and self.store.get(f"stopped:{about}"):
                    self.store.set(f"goingon:{about}", "1")
        if state.echoes or self.runtime.backend_echoes(session_id):
            # Until its conversation shows it: lost on the way, it is sent again.
            state.echo_wait[marker["digest"]] = (
                pending[0]["id"],
                pending[-1]["id"],
                text,
                attachments,
                0,
            )
            self.check_echo_later(session_id, session, marker["digest"])
        if row["role"] == WORKER and (state.echoes or self.runtime.backend_echoes(session_id)):
            # Until its conversation shows it: a stop before then sends it again.
            state.unseen[marker["digest"]] = pending[0]["id"]
        if mid_turn:
            # It sees this once its conversation shows it (events.user_message): a report
            # before that is about the work without it.
            state.unread[marker["digest"]] = (
                pending[-1]["text"][:300],
                tuple(m["meta"].get("task_id") or "" for m in pending),
                pending[0]["id"],
            )
            if orders := [m for m in pending if instructs(m)]:
                # What of it asks something of it: what a report before it is held back for.
                state.unread_orders[marker["digest"]] = (
                    orders[-1]["text"][:300],
                    tuple(m["meta"].get("task_id") or "" for m in orders),
                )
        self.store.set(f"prevsent:{session_id}", str(mark))  # To send again after a failure.
        self.store.set(f"sent:{session_id}", str(pending[-1]["id"]))
        if digest_upto:
            self.store.set("teamdigest:upto", str(digest_upto))
        self.store.delete(f"sending:{session_id}")
        self.store.delete(f"fresh:{self.runtime.copilot_id(session_id)}")
        if preamble:
            self.store.delete(f"preamble:{session_id}")
        if self.store.get(f"undelivered:{session_id}"):
            self.resolve_undelivered(session_id)
        self.publish_delivery(session_id, pending)
        return True

    async def reconcile(self, session_id: str) -> bool | None:
        """After a stop during a send: True if it arrived, False if not, None if unknown."""
        marker = json.loads(self.store.get(f"sending:{session_id}") or "null")
        if not marker:
            return None
        arrived = None
        try:
            session = await self.runtime.session_for(session_id)
            if hasattr(session, "get_events"):
                arrived = any(
                    getattr(event.type, "value", "") == "user.message"
                    and digest(getattr(event.data, "content", "") or "") == marker["digest"]
                    for event in await self.conversation(session_id, session)
                )
        except SettingsChangedError:
            pass  # Not opened (its timeline says why): unknown, sent again once it may.
        except Exception:
            logger.warning("Could not check what %s received.", session_id, exc_info=True)
        if arrived:
            self.store.set(f"sent:{session_id}", str(marker["upto"]))
        self.store.delete(f"sending:{session_id}")
        return arrived

    async def received(self, session_id: str, messages: list[dict]) -> list[dict]:
        """Of messages counted as sent (oldest first), those its conversation shows it received:
        Copilot can lose what it took just before it stopped (send returned, nothing written).
        Up to the last one it shows; all of them when its conversation cannot be read."""
        if not messages:
            return messages
        try:
            session = await self.runtime.session_for(session_id)
            if not hasattr(session, "get_events"):
                return messages
            shown = "\n".join(
                getattr(event.data, "content", "") or ""
                for event in await self.conversation(session_id, session)
                if getattr(event.type, "value", "") == "user.message"
            )
        except SettingsChangedError:
            return messages  # Not opened (its timeline says why).
        except Exception:
            logger.warning("Could not check what %s received.", session_id, exc_info=True)
            return messages
        last = -1
        for index, message in enumerate(messages):
            prompt = message["meta"]["prompt"]
            if prompt in shown or self.runtime.told_prompt(message) in shown:
                last = index
        return messages[: last + 1]

    # Delivery, as the chats show it: queued (not sent yet) -> sent -> received (its
    # conversation shows it) -> processed (the turn it was in ended); or resending, undelivered;
    # or held (for its next message: it was stopped).

    def delivery_marks(self, session_id: str) -> dict:
        seen = self.store.get(f"seen:{session_id}")
        handled = self.store.get(f"handled:{session_id}")
        waiting = self.states[session_id].echo_wait.values()
        return {
            "sent": int(self.store.get(f"sent:{session_id}") or 0),
            "seen": int(seen) if seen else None,
            "handled": int(handled) if handled else None,
            "waiting": [(first, last, resent) for first, last, _, _, resent in waiting],
        }

    @staticmethod
    def delivery_of(message: dict, marks: dict) -> str:
        """Where one message stands ("" for one not sent as a prompt, or from before Chief of
        Staff kept track)."""
        meta = message.get("meta") or {}
        if not isinstance(meta, dict) or "prompt" not in meta or meta.get("dropped"):
            return ""
        if meta.get("undelivered"):
            return "undelivered"
        if message["id"] > marks["sent"]:
            # Held back for its next message (it was stopped, or failed): no turn of its own.
            return "held" if meta.get("deferred") else "queued"
        for first, last, resent in marks["waiting"]:
            if first <= message["id"] <= last:
                return "resending" if resent else "sent"
        if marks["handled"] is not None and message["id"] <= marks["handled"]:
            return "processed"
        if marks["seen"] is not None and message["id"] <= marks["seen"]:
            return "received"
        return "sent" if marks["seen"] is not None else ""

    def delivery_for(self, session_id: str, messages: list[dict]) -> list[str]:
        """Where each of a session's messages stands, as its chat shows it. The Router's copy of
        what the owner wrote a worker (Home shows it, "→ worker") stands where that worker has
        it: the Router hears of it only with its next message, which is not the owner's message
        getting there (bug QA6-1: it said "Waiting to be sent" hours after the worker answered)."""
        marks = self.delivery_marks(session_id)
        others: dict[str, dict] = {}
        return [
            self.copy_delivery(meta, others)
            if isinstance(meta := message.get("meta"), dict) and meta.get("to")
            else self.delivery_of(message, marks)
            for message in messages
        ]

    def copy_delivery(self, meta: dict, marks: dict[str, dict]) -> str:
        """Where the worker's own message stands (`sent_as`); "" for a copy of none (an answer
        to its question card, not sent as a message; a copy from before copies said which)."""
        worker, sent_as = meta.get("to"), meta.get("sent_as")
        if not sent_as or not isinstance(worker, str):
            return ""
        try:
            original = self.store.message(int(sent_as))
        except (LookupError, TypeError, ValueError):
            return ""
        if original["session_id"] != worker:
            return ""
        if worker not in marks:
            marks[worker] = self.delivery_marks(worker)
        return self.delivery_of(original, marks[worker])

    def publish_delivery(self, session_id: str, messages: list[dict]):
        """The chats show where these messages stand now: Home's copies of the owner's too."""
        tracked = [m for m in messages if m["kind"] in TRACKED]
        if not tracked:
            return
        for message, delivery in zip(tracked, self.delivery_for(session_id, tracked), strict=True):
            meta = {k: v for k, v in (message.get("meta") or {}).items() if k != "prompt"}
            self.runtime.bus.publish(
                "message", {**message, "meta": meta, "delivery": delivery, "amended": True}
            )
        owners = [m["id"] for m in tracked if m["kind"] == "owner"]
        try:
            router = self.runtime.router_id()
        except RuntimeError:
            return
        if not owners or router == session_id:
            return
        copies = self.store.all(
            "SELECT id FROM messages WHERE session_id=? AND kind='owner' "
            "AND json_extract(meta, '$.to')=? AND json_extract(meta, '$.sent_as') BETWEEN ? AND ?",
            (router, session_id, min(owners), max(owners)),
        )
        if copies:
            self.publish_delivery(router, [self.store.message(row["id"]) for row in copies])

    def publish_range(self, session_id: str, first: int, last: int):
        found = self.store.all(
            "SELECT id FROM messages WHERE session_id=? AND id BETWEEN ? AND ? AND kind IN "
            "('owner', 'router') ORDER BY id DESC LIMIT 20",
            (session_id, first, last),
        )
        self.publish_delivery(session_id, [self.store.message(row["id"]) for row in found])

    def echoed(self, session_id: str, content: str):
        """Its conversation shows a message it was sent: received."""
        state = self.states[session_id]
        if not state.echo_wait:
            return
        seen = digest(content)
        # That one (or, changed on the way, the oldest).
        first, last, *_ = state.echo_wait.pop(
            seen if seen in state.echo_wait else next(iter(state.echo_wait))
        )
        mark = int(self.store.get(f"seen:{session_id}") or 0)
        self.store.set(f"seen:{session_id}", str(max(mark, last)))
        self.publish_range(session_id, first, last)

    def handled(self, session_id: str):
        """Its turn ended: what it received is processed."""
        seen = self.store.get(f"seen:{session_id}")
        before = int(self.store.get(f"handled:{session_id}") or 0)
        if seen and int(seen) > before:
            self.store.set(f"handled:{session_id}", seen)
            self.publish_range(session_id, before + 1, int(seen))

    def check_echo_later(self, session_id: str, session, sent: str, after: float = 0):
        asyncio.get_running_loop().call_later(
            after or ECHO_CHECK,
            lambda: self.runtime.background(self.check_echo(session_id, session, sent, after)),
        )

    async def check_echo(self, session_id: str, session, sent: str, waited: float = 0):
        """Bug E14: a message whose conversation never showed it was taken for delivered (sent
        into a turn that was over, say) and silently lost. Not shown a while after it was sent,
        and not waiting in Copilot's queue either: it is sent again, the same message (so its
        echo settles it), a couple of times, then the owner hears it did not get through."""
        state = self.states[session_id]
        entry = state.echo_wait.get(sent)
        if entry is None:
            return  # Shown.
        if state.after_abort:
            # Stopped, or its task cancelled: Copilot dropped what that turn had not taken up.
            # Sent again now it would start the work called off (Stop sends it again with its
            # next message instead; a cancellation drops it).
            state.echo_wait.pop(sent, None)
            return
        if self.states.handle(session_id) is not session:
            state.echo_wait.pop(sent, None)  # Reconnected: what it missed is replayed or resent.
            return
        later = min(max(waited * 2, ECHO_CHECK), 300)
        if state.awaiting and state.awaiting[0] == sent:
            self.check_echo_later(session_id, session, sent, later)  # check_started has it.
            return
        try:
            events = await self.conversation(session_id, session)
        except Exception:
            logger.warning("Could not check whether %s got a message.", session_id, exc_info=True)
            self.check_echo_later(session_id, session, sent, later)
            return
        shown = [
            content
            for event in events
            if getattr(event.type, "value", "") == "user.message"
            and digest(content := getattr(event.data, "content", "") or "") == sent
        ]
        if shown:
            if sent in state.echo_wait:
                self.echoed(session_id, shown[0])
            return
        if await queue_holds(session):
            # Queued behind its turn, or a steer waiting for its next step: it comes.
            self.check_echo_later(session_id, session, sent, later)
            return
        row = self.store.find_session(session_id)
        if not row or row["closed_at"] or sent not in state.echo_wait or state.after_abort:
            return
        first, last, text, attachments, resent = state.echo_wait[sent]
        if resent >= ECHO_RESENDS:
            state.echo_wait.pop(sent, None)
            self.undelivered_after_resends(session_id, first, last)
            return
        if row["role"] == WORKER and await self.runtime.settings_refusal(row):
            # Sent again, it would start a turn: it waits with what else it was sent.
            self.runtime.background(self.runtime.settings_stop(session_id))
            return
        async with state.sending:
            if (
                sent not in state.echo_wait
                or state.after_abort
                or self.states.handle(session_id) is not session
            ):
                return
            working = row["role"] == WORKER and (
                row["status"] == "working" or (row["status"] == "waiting" and state.busy)
            )
            mode: SendMode = "immediate" if state.held or working else "enqueue"
            try:
                if attachments:
                    await session.send(text, mode=mode, attachments=attachments)
                else:
                    await session.send(text, mode=mode)
            except Exception:
                logger.warning("Could not send %s a message again.", session_id, exc_info=True)
            state.echo_wait[sent] = (first, last, text, attachments, resent + 1)
        logger.info("A message to %s never showed in its conversation: sent again.", session_id)
        self.publish_range(session_id, first, last)
        self.check_echo_later(session_id, session, sent, later)

    def undelivered_after_resends(self, session_id: str, first: int, last: int):
        found = self.store.all(
            "SELECT id FROM messages WHERE session_id=? AND id BETWEEN ? AND ?",
            (session_id, first, last),
        )
        for row in found:
            message = self.store.message(row["id"])
            if "prompt" in (message.get("meta") or {}):
                self.store.update_message_meta(
                    message["id"], {**message["meta"], "undelivered": True}
                )
        self.publish_range(session_id, first, last)
        text = (
            "A message did not reach it, though it was sent again: Copilot never showed it in "
            "its conversation. Send it again, or Stop and Go on."
        )
        self.runtime.add_message(session_id, "error", text)
        self.runtime.alert(text, session_id=session_id, needs_action=True)

    async def take_back(self, session_id: str, session) -> bool:
        """Its turn ended with a command left running in the background (Events.held_idle):
        what it was sent while it worked waits in Copilot's queue until that command ends (a
        dev server: for ever). Taken back from there, it is sent again at once. Whether its
        turn goes on with it (as it would have, but for that command): then it did not end."""
        state = self.states[session_id]
        # Under the lock: a send under way when its turn ended is in Copilot's queue by then.
        async with state.sending:
            if not (state.unseen or state.unread):
                return False
            taken = await take_queued(session)
            if taken is None:
                # Copilot cannot say what it queued: once nothing runs in the background, it
                # starts on it.
                if not await stop_background(session):
                    return False
                self.runtime.add_message(session_id, "notice", HELD_STOPPED)
                return True
            if not taken or not self.unsee(session_id):
                return False
            logger.info(
                "Took back %d message(s) Copilot queued for %s to resend.", taken, session_id
            )
            state.held = True  # Sent at once, as what comes next.
            await self.send_pending(session_id, session, "immediate", busy=True, joins=True)
            return True

    async def check_started(
        self, session_id: str, session, sent: str, stopped_background: bool = False
    ):
        """A message that should have started a turn a while ago. Unless its conversation shows
        it, Copilot took it and gave up: a setting it cannot load (an MCP server's) stops every
        message to the session, with no error event. The session says so (Runtime.not_started)
        instead of working forever. Or a command it left running in the background holds it:
        that command is stopped, and the message checked on again."""
        state = self.states[session_id]
        if not state.awaiting or state.awaiting[0] != sent:
            return  # It started (events.user_message), or was sent again since.
        if self.states.handle(session_id) is not session:
            return
        try:
            events = await self.conversation(session_id, session)
        except Exception:
            logger.warning("Could not check whether %s started.", session_id, exc_info=True)
            return
        if any(
            getattr(event.type, "value", "") == "user.message"
            and digest(getattr(event.data, "content", "") or "") == sent
            for event in events
        ):
            state.awaiting = None
            return
        if not (state.awaiting and state.awaiting[0] == sent):
            return
        if not stopped_background and await running_background(session):
            if await stop_background(session):
                self.runtime.add_message(session_id, "notice", HELD_STOPPED)
            asyncio.get_running_loop().call_later(
                START_CHECK,
                lambda: self.runtime.background(
                    self.check_started(session_id, session, sent, stopped_background=True)
                ),
            )
            return
        await self.runtime.not_started(session_id)

    def rest_after(self, row: dict):
        """A done or waiting worker asked about its work returns to that state after a turn
        without a report. Not one given work to do (a new task, or its task reopened): that
        work is reported, and an answer it leaves without a report is filed as one."""
        task = self.runtime.tasks.current(row["id"])
        state = self.states[row["id"]]
        if task and task["status"] == "running":
            state.resting = ""
        elif row["status"] in {"done", "waiting"}:
            state.resting = row["status"]

    async def resume(self, session_id: str, now: bool = False):
        """Send what an idle session was given but never received (after a restart). `now`:
        the owner asked for it (Runtime.send_now), a failed worker too."""
        row = self.store.session(session_id)
        busy = {"queued", "starting", "working"} | (set() if now else {"failed"})
        if row["closed_at"] or row["status"] in busy:
            return  # A failed one gets what it missed with the owner's next message.
        if not self.has_unsent(session_id):
            return  # Only deferred notes: they wait for its next message.
        if row["role"] in {ROUTER, SLACK}:
            self.schedule_flush(session_id, 0)
        elif await self.runtime.workers.claim_slot(session_id):
            self.rest_after(row)  # As deliver does.
            # Stopped, Copilot may hold its session busy a while: steered, it takes it at once.
            mode: SendMode = "immediate" if self.states[session_id].after_abort else "enqueue"
            try:
                if await self.flush(session_id, await self.runtime.session_for(session_id), mode):
                    self.runtime.update_session(session_id, status="working")
                else:
                    self.states[session_id].busy = False
                    self.runtime.background(self.runtime.workers.pump())
            except SettingsChangedError:
                # Its timeline says why; it waits for the owner (Runtime.settings_stop).
                self.states[session_id].busy = False
                self.runtime.background(self.runtime.workers.pump())
                await self.runtime.settings_stop(session_id)
            except Exception:
                self.states[session_id].busy = False
                self.runtime.background(self.runtime.workers.pump())
                logger.warning("Could not resend to %s.", session_id, exc_info=True)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def instructs(message: dict) -> bool:
    """Whether a message to a worker asks something of it (E145): the Router's or the owner's
    own (not a check-back's wake, nor the results of tasks it waited for), or what the owner
    did with its draft when they said why. Not a thread's or PR's news (untrusted), a peer's
    note, or another of Chief of Staff's wakes and notices: a report written before it reached
    the worker is no less its report."""
    meta = message["meta"]
    prompt = str(meta.get("prompt") or "")
    if prompt.startswith(('<message from="router"', '<message from="owner"')):
        return not meta.get("check_back") and not prompt.startswith(
            '<message from="router" type="results"'
        )
    return bool(meta.get("owner_said"))


async def queue_holds(session) -> bool:
    """Whether Copilot holds messages for the session (queued for after its turn, or steers
    for its next step); True when it cannot say (so nothing is sent twice). A session that
    queues by itself (another agent's adapter) says so itself."""
    if own := getattr(session, "queue_holds", None):
        try:
            return await own()
        except Exception:
            return True
    try:
        listed = await session.rpc.queue.pending_items()
    except Exception:
        return True
    return bool(
        listed.items
        or getattr(listed, "steering_messages", None)
        or getattr(listed, "in_flight_steering_count", 0)
    )
