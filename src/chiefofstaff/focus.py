"""Focus Shield: while the owner focuses, notifications wait and the Router handles what it
can; afterwards the owner gets one summary.
"""

from __future__ import annotations

import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .digest import finished_tasks, needs_you
from .envelope import wrap

if TYPE_CHECKING:
    from .runtime import Runtime


def duration(minutes: int) -> str:
    """45 min, 1h, 1h 30m."""
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60}h" + (f" {minutes % 60}m" if minutes % 60 else "")


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
        # How long, not until when: the page shows the time in the owner's own clock style.
        length = duration(minutes)
        note = (
            f"The owner is focusing for {length} (until {clock}). Handle what you can without "
            "them: forward Slack messages that need a reply to the Slack session (drafts wait "
            "for their approval), give code work to workers, ignore noise. Use "
            "notify_owner(urgent=true) only for what cannot wait until then."
        )
        with suppress(RuntimeError):
            await self.delivery.deliver(
                self.runtime.router_id(),
                wrap("system", "focus", note),
                batch=True,
                kind="notice",
                display=f"Focusing for {length}",
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
        """What came in, what was drafted and finished, and what needs the owner now: as Home
        counts it under Needs you."""
        since = datetime.fromtimestamp(started, UTC).isoformat()
        length = duration(max(1, round((time.time() - started) / 60)))
        # What reached Chief of Staff from outside (each Slack message, PR comment, CI result,
        # watch finding once), not the batches the Router got them in.
        handled = self.store.value(
            """SELECT count(*) AS n FROM triggers WHERE source IN ('slack', 'github', 'watch')
            AND text!='' AND created_at>=?""",
            (since,),
        )
        # Drafts that wait for the owner or went out: not those replaced or withdrawn since.
        drafted = self.store.value(
            """SELECT count(*) AS n FROM drafts WHERE kind IN ('slack','github') AND created_at>=?
            AND status IN ('pending', 'uncertain', 'sent')""",
            (since,),
        )
        finished = len(finished_tasks(self.store, started, time.time() + 1))
        needs = needs_you(self.runtime)
        parts = [
            f"{handled} message{'s' if handled != 1 else ''} handled" if handled else "",
            f"{drafted} repl{'ies' if drafted != 1 else 'y'} drafted for you" if drafted else "",
            f"{finished} task{'s' if finished != 1 else ''} finished" if finished else "",
            f"{needs} need{'s' if needs == 1 else ''} you" if needs else "",
        ]
        done = " · ".join(part for part in parts if part) or "nothing came up"
        return f"While you focused ({length}): {done}."

    def state(self) -> dict:
        if not self.active():
            return {"until": None}
        until = float(self.store.get("focus_until", "0"))
        return {"until": datetime.fromtimestamp(until, UTC).isoformat()}

    async def check(self, moment: float | None = None):
        until = float(self.store.get("focus_until") or 0)
        if until and (time.time() if moment is None else moment) >= until:
            await self.end()
