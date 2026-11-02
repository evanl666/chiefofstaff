"""Focus Shield: while the owner focuses, notifications wait and the Router handles what it
can; afterwards the owner gets one summary.
"""

from __future__ import annotations

import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .envelope import wrap

if TYPE_CHECKING:
    from .runtime import Runtime


class Focus:
    """Starts and ends the owner's focus time."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.bus = runtime.bus
        self.delivery = runtime.delivery

    def active(self) -> bool:
        return time.time() < float(self.store.get("focus_until") or 0)

    async def start(self, minutes: int) -> dict:
        """Hold back notifications for a while; the Router handles what it can meanwhile."""
        if minutes <= 0:
            return await self.end()
        started = float(self.store.get("focus_started") or 0) if self.active() else time.time()
        until = time.time() + minutes * 60
        self.store.set("focus_started", str(started))
        self.store.set("focus_until", str(until))
        clock = datetime.fromtimestamp(until).astimezone().strftime("%H:%M")
        note = (
            f"The owner is focusing until {clock}. Handle what you can without them: forward "
            "Slack messages that need a reply to the Slack session (drafts wait for their "
            "approval), give code work to workers, ignore noise. Use notify_owner(urgent=true) "
            "only for what cannot wait until then."
        )
        with suppress(RuntimeError):
            await self.delivery.deliver(
                self.runtime.router_id(),
                wrap("system", "focus", note),
                batch=True,
                kind="notice",
                display=f"Focusing until {clock}",
            )
        self.runtime.publish_state()
        return self.state()

    async def end(self) -> dict:
        """Focus is over: one summary of what happened instead of the notifications."""
        started = float(self.store.get("focus_started") or 0)
        self.store.delete("focus_until")
        self.store.delete("focus_started")
        if started:
            text = self.summary(started)
            with suppress(RuntimeError):
                self.runtime.add_message(
                    self.runtime.router_id(), "update", text, {"focus_summary": True}, unread=True
                )
                self.runtime.alert(text, session_id=self.runtime.router_id(), needs_action=False)
            self.bus.publish("focus_summary", {"text": text})
        self.runtime.publish_state()
        return self.state()

    def summary(self, started: float) -> str:
        since = datetime.fromtimestamp(started, UTC).isoformat()
        minutes = max(1, round((time.time() - started) / 60))
        length = f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes} min"
        handled = self.store.one(
            """SELECT count(*) AS n FROM messages WHERE kind='inbound' AND created_at>=?
            AND (json_extract(meta, '$.source') IS NOT NULL)""",
            (since,),
        )["n"]
        drafted = self.store.one(
            "SELECT count(*) AS n FROM drafts WHERE kind IN ('slack','github') AND created_at>=?",
            (since,),
        )["n"]
        finished = self.store.one(
            """SELECT count(DISTINCT task_id) AS n FROM runs WHERE status='succeeded'
            AND ended_at>=?""",
            (since,),
        )["n"]
        needs = (
            len(self.store.drafts())
            + self.store.one(
                "SELECT count(*) AS n FROM tasks WHERE status IN ('waiting','failed')"
            )["n"]
        )
        parts = [
            f"{handled} message{'s' if handled != 1 else ''} handled" if handled else "",
            f"{drafted} repl{'ies' if drafted != 1 else 'y'} drafted for you" if drafted else "",
            f"{finished} task{'s' if finished != 1 else ''} ready to review" if finished else "",
            f"{needs} need{'s' if needs == 1 else ''} you" if needs else "",
        ]
        done = " · ".join(part for part in parts if part) or "nothing came up"
        return f"While you focused ({length}): {done}."

    def state(self) -> dict:
        if not self.active():
            return {"until": None}
        until = float(self.store.get("focus_until"))
        return {"until": datetime.fromtimestamp(until, UTC).isoformat()}

    async def check(self, moment: float | None = None):
        until = float(self.store.get("focus_until") or 0)
        if until and (time.time() if moment is None else moment) >= until:
            await self.end()
