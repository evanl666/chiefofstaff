"""Delivering messages to sessions: stored first, then sent with whatever else is pending.

Every message for a session is a row in `messages` whose meta holds the exact prompt. A session's
`sent:` mark is the id of the last message it was given, so whatever comes after it is its
outbox. Delivery is at-least-once: if Chief of Staff stops between a send and the mark, those
messages are sent again after the restart; envelopes carry the message id so a session can tell.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from .roles import ROUTER, SLACK, WORKER
from .sources.slack import describe

if TYPE_CHECKING:
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
        # The exact prompt is kept so a queued worker or a restart can replay it.
        message = self.runtime.add_message(
            session_id,
            kind,
            prompt if display is None else display,
            {**(meta or {}), "prompt": prompt},
        )
        if row["role"] == WORKER and row["status"] in {"done", "waiting"}:
            # Unless this turn reports anew, the worker returns to this state afterwards.
            self.states[session_id].resting = row["status"]
        if batch and row["role"] in {ROUTER, SLACK}:
            if row["status"] == "working":
                return message  # It gets this with the next flush, when it is idle.
            if EVENT_DEBOUNCE > 0:
                self.schedule_flush(session_id)
                return message
        if row["role"] == WORKER and not await self.runtime.claim_slot(session_id):
            return message
        working = row["status"] == "working" and self.states.is_live(session_id)
        mode = (
            "immediate" if steer and working and not self.states[session_id].retuned else "enqueue"
        )
        try:
            sent = await self.flush(session_id, await self.runtime.session_for(session_id), mode)
        except Exception as exc:
            self.states[session_id].busy = False
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

    def has_unsent(self, session_id: str) -> bool:
        mark = int(self.store.get(f"sent:{session_id}") or 0)
        return any("prompt" in m["meta"] for m in self.store.messages(session_id, after=mark))

    async def flush(self, session_id: str, session, mode: str = "enqueue") -> bool:
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
            await session.send("\n\n".join(prompts), mode=mode)
            self.store.set(f"sent:{session_id}", str(pending[-1]["id"]))
            if preamble:
                self.store.execute("DELETE FROM kv WHERE key=?", (f"preamble:{session_id}",))
            return True
