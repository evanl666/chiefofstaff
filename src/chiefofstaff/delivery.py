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
from typing import TYPE_CHECKING

from .roles import ROUTER, SLACK, WORKER
from .sources.slack import describe

if TYPE_CHECKING:
    from .backend import SendMode
    from .runtime import Runtime

logger = logging.getLogger(__name__)

# Events for the Router and the Slack session wait this long, so a burst becomes one turn.
EVENT_DEBOUNCE = float(os.environ.get("CHIEFOFSTAFF_EVENT_DEBOUNCE", "2"))

# Held messages that could not be sent are retried after this, doubling up to a minute.
RETRY_DELAY = 2.0


class Delivery:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.states = runtime.states

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
    ) -> dict:
        """Give a session a message: stored first, then sent with what else it has not received.

        `steer` (the owner's messages) reaches a session that is working within its current
        turn. `batch` (events and reports for the Router or the Slack session) waits until
        the session is idle and a short burst has passed, so several become one turn.
        """
        row = self.store.session(session_id)
        if row["closed_at"]:
            raise ValueError("That session is closed.")
        if row["role"] == WORKER and not self.states.handle(session_id):
            # Not attached: what it did in a terminal meanwhile goes first, before this.
            await self.runtime.workers.catch_up(row)
        # The exact prompt is kept so a queued worker or a restart can replay it.
        message = self.runtime.add_message(
            session_id,
            kind,
            prompt if display is None else display,
            {**(meta or {}), "prompt": prompt},
        )
        if row["role"] == WORKER:
            self.rest_after(row)
        if batch and row["role"] in {ROUTER, SLACK}:
            if row["status"] == "working":
                return message  # It gets this with the next flush, when it is idle.
            if EVENT_DEBOUNCE > 0:
                self.schedule_flush(session_id)
                return message
        claimed = not self.states[session_id].busy
        if row["role"] == WORKER and not await self.runtime.workers.claim_slot(session_id):
            return message
        working = row["status"] == "working" and self.states.is_live(session_id)
        mode = (
            "immediate" if steer and working and not self.states[session_id].retuned else "enqueue"
        )
        try:
            sent = await self.flush(session_id, await self.runtime.session_for(session_id), mode)
        except asyncio.CancelledError:
            if claimed:
                self.states[session_id].busy = False
                self.runtime.background(self.runtime.workers.pump())
            raise
        except Exception as exc:
            if claimed or row["role"] != WORKER:
                self.states[session_id].busy = False
                self.runtime.background(self.runtime.workers.pump())  # Its slot is free.
            # Not replayed later: the caller sees the error (a watcher re-polls the event).
            self.store.update_message_meta(message["id"], meta or {})
            self.runtime.add_message(session_id, "error", f"Delivery failed: {describe(exc)}")
            raise RuntimeError(f"Could not deliver the message: {describe(exc)}") from exc
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
                return  # Stopping or reconnecting: they are sent once connected (resume).
            try:
                if await self.flush(session_id, await self.runtime.session_for(session_id)):
                    self.runtime.update_session(session_id, status="working")
            except Exception as exc:
                logger.warning("Could not deliver held messages to %s.", session_id, exc_info=True)
                if attempt == 0:
                    self.runtime.add_message(
                        session_id, "error", f"Delivery failed: {describe(exc)}. Retrying."
                    )
                self.schedule_flush(session_id, min(60, RETRY_DELAY * 2**attempt), attempt + 1)

        self.runtime.background(later())

    def defer_pending(self, session_id: str):
        """Whatever it has not received yet rides along with its next message (it was stopped
        or its task cancelled): a restart must not start a turn for it."""
        mark = int(self.store.get(f"sent:{session_id}") or 0)
        for message in self.store.messages(session_id, after=mark):
            if "prompt" in message["meta"] and not message["meta"].get("deferred"):
                self.store.update_message_meta(message["id"], {**message["meta"], "deferred": True})

    def has_unsent(self, session_id: str) -> bool:
        """Whether it has messages to receive that are worth a turn (a deferred note, such as
        a cancellation, only rides along with the next one)."""
        mark = int(self.store.get(f"sent:{session_id}") or 0)
        return any(
            "prompt" in m["meta"] and not m["meta"].get("deferred")
            for m in self.store.messages(session_id, after=mark)
        )

    async def flush(self, session_id: str, session, mode: SendMode = "enqueue") -> bool:
        """Send every prompt this session has not received yet, as one message."""
        async with self.states[session_id].sending:
            # The Router's Copilot session may have been replaced while this waited.
            session = self.states.handle(session_id) or session
            mark = int(self.store.get(f"sent:{session_id}") or 0)
            pending = [
                message
                for message in self.store.messages(session_id, after=mark)
                if "prompt" in message["meta"]
            ]
            if not pending:
                return False
            prompts = [message["meta"]["prompt"] for message in pending]
            if preamble := self.store.get(f"preamble:{session_id}"):
                prompts.insert(0, preamble)  # A new Router's handover, before what is new.
            text = "\n\n".join(prompts)
            marker = {"upto": pending[-1]["id"], "digest": digest(text)}
            self.store.set(f"sending:{session_id}", json.dumps(marker))
            try:
                await session.send(text, mode=mode)
            except BaseException:
                # The caller hears of the failure and the messages count as not delivered.
                self.store.delete(f"sending:{session_id}")
                raise
            self.store.set(f"sent:{session_id}", str(pending[-1]["id"]))
            self.store.delete(f"sending:{session_id}")
            self.store.delete(f"fresh:{self.runtime.copilot_id(session_id)}")
            if preamble:
                self.store.delete(f"preamble:{session_id}")
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
                    for event in await session.get_events()
                )
        except Exception:
            logger.warning("Could not check what %s received.", session_id, exc_info=True)
        if arrived:
            self.store.set(f"sent:{session_id}", str(marker["upto"]))
        self.store.delete(f"sending:{session_id}")
        return arrived

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

    async def resume(self, session_id: str):
        """Send what an idle session was given but never received (after a restart)."""
        row = self.store.session(session_id)
        if row["closed_at"] or row["status"] in {"queued", "starting", "working"}:
            return
        if not self.has_unsent(session_id):
            return  # Only deferred notes: they wait for its next message.
        if row["role"] in {ROUTER, SLACK}:
            self.schedule_flush(session_id, 0)
        elif await self.runtime.workers.claim_slot(session_id):
            self.rest_after(row)  # As deliver does.
            try:
                if await self.flush(session_id, await self.runtime.session_for(session_id)):
                    self.runtime.update_session(session_id, status="working")
                else:
                    self.states[session_id].busy = False
                    self.runtime.background(self.runtime.workers.pump())
            except Exception:
                self.states[session_id].busy = False
                self.runtime.background(self.runtime.workers.pump())
                logger.warning("Could not resend to %s.", session_id, exc_info=True)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]
