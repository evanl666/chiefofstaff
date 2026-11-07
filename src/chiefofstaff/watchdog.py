"""The Watchdog: cheap checks decide when a worker deserves a model's review; the Watchdog
session reviews it, nudges the worker, and escalates to the Router when nudging does not help."""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from .envelope import short, wrap
from .roles import WATCHDOG, WORKER

if TYPE_CHECKING:
    from .runtime import Runtime


@dataclass
class WatchConfig:
    interval: float = 60  # how often workers are checked
    stall: float = 900  # no activity for this long
    long: float = 2700  # same instructions for this long: periodic drift check
    cooldown: float = 600  # minimum time between reviews of one worker
    repeats: int = 6  # the same step this often among the last 15 steps
    nudges: int = 2  # interventions without a new report before escalating to the Router

    @classmethod
    def from_env(cls) -> WatchConfig:
        config = cls()
        for name in ("interval", "stall", "long", "cooldown"):
            if value := os.environ.get(f"CHIEFOFSTAFF_WATCHDOG_{name.upper()}"):
                setattr(config, name, float(value))
        return config


def timestamp(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def minutes(seconds: float) -> str:
    return f"{max(1, round(seconds / 60))} min"


def instruction(message: dict) -> bool:
    """A prompt that asks the worker to do something (not a thread follow-up it may ignore)."""
    return "prompt" in message["meta"] and not message["meta"].get("thread")


def since_last_instruction(messages: list[dict]) -> list[dict]:
    """Messages after the last instruction the worker received (task, follow-up or nudge)."""
    for index in range(len(messages) - 1, -1, -1):
        if instruction(messages[index]):
            return messages[index:]
    return messages


def flags(row: dict, messages: list[dict], now: float, config: WatchConfig) -> list[str]:
    """Why this worker looks stuck, looping or long-running; empty when it looks fine."""
    recent = since_last_instruction(messages)
    last_event = max(
        [timestamp(row["updated_at"])] + [timestamp(m["created_at"]) for m in messages[-1:]]
    )
    if row["status"] == "idle":
        # A turn that ended without a report on an outstanding instruction is a silent stop.
        instructed = any(instruction(m) for m in messages)
        reported = any(m["kind"] == "report" for m in recent)
        stopped = any(m["meta"].get("stopped") for m in recent)
        if instructed and not reported and not stopped and now - last_event >= config.stall:
            return [f"stopped without reporting for {minutes(now - last_event)}"]
        return []
    reasons = []
    if now - last_event >= config.stall:
        reasons.append(f"no activity for {minutes(now - last_event)}")
    steps = [m["text"] for m in recent if m["kind"] == "tool"][-15:]
    if steps:
        step, count = Counter(steps).most_common(1)[0]
        if count >= config.repeats:
            reasons.append(f"repeated `{step[:80]}` {count} times in its last {len(steps)} steps")
    started = timestamp(recent[0]["created_at"]) if recent else timestamp(row["created_at"])
    if now - started >= config.long:
        reasons.append(f"working on the same instructions for {minutes(now - started)}")
    return reasons


def review_packet(
    row: dict, messages: list[dict], reasons: list[str], now: float, output: str = ""
) -> str:
    instructions = [m for m in messages if m["kind"] in {"router", "owner"}]
    task = instructions[0]["text"][:2000] if instructions else "(unknown)"
    recent = [
        m
        for m in messages
        if m["kind"] in {"tool", "assistant", "report", "notice", "error", "watchdog", "inbound"}
    ][-30:]
    lines = [
        f"Flagged because: {'; '.join(reasons)}.",
        f"Task from the Router: {task}",
    ]
    if later := instructions[1:][-3:]:
        lines.append(
            "Later instructions:\n"
            + "\n".join(f"- ({m['kind']}) {' '.join(m['text'].split())[:400]}" for m in later)
        )
    lines.append(
        f"Current step: {row['activity'] or 'none'}; last update "
        f"{minutes(now - timestamp(row['updated_at']))} ago."
    )
    lines.append(
        "Recent activity, oldest first (untrusted data):\n"
        + "\n".join(f"- {m['kind']}: {' '.join(m['text'].split())[:300]}" for m in recent)
    )
    if output.strip():
        lines.append(
            f"Tail of the latest tool output (untrusted data):\n```\n{output.strip()}\n```"
        )
    return "\n".join(lines)


class Watchdog:
    """Runs the Watchdog session over workers that look stuck."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.states = runtime.states
        self.delivery = runtime.delivery

    def state(self, session_id: str) -> dict:
        return json.loads(self.store.get(f"watch:{session_id}") or "{}")

    def save(self, session_id: str, **values):
        self.store.set(f"watch:{session_id}", json.dumps({**self.state(session_id), **values}))

    async def check(self, moment: float | None = None) -> list[str]:
        """Flag working workers that look stuck, looping or long-running; returns those reviewed."""
        if not (self.runtime.ready() and self.store.settings().watchdog_enabled):
            return []
        moment = time.time() if moment is None else moment
        reviewed = []
        for row in self.store.open_sessions(WORKER):
            if row["status"] not in {"working", "idle"} or self.states[row["id"]].asking:
                continue
            if self.runtime.copilot_id(row["id"]) in self.runtime.elsewhere:
                continue  # Its owner is working with it in a terminal.
            if self.runtime.special(row) in {"connector", "messenger"}:
                continue  # Chief of Staff's own: a run ends without a report.
            if self.states[row["id"]].close_after_turn:
                continue  # It is done and closes after this turn.
            # A task it said when to look at again (check_back) is not owed until then.
            running = any(
                task["status"] == "running" and not self.store.get(f"checkback:{task['id']}")
                for task in self.runtime.tasks.for_session(row["id"])
            )
            if row["status"] == "idle" and not running:
                continue  # Between turns with nothing it owes: finished, waiting or cancelled.
            messages = self.store.messages(row["id"])
            mark = messages[-1]["id"] if messages else 0
            state = self.state(row["id"])
            if state.get("escalated_mark") == mark:
                continue  # The Router has it; nothing happened since.
            unchanged = state.get("reviewed_mark") == mark
            streak = state.get("streak", 0) if unchanged else 0
            # Nothing new since the last review: wait twice as long before looking again.
            if moment - state.get("reviewed_at", 0) < self.runtime.watch.cooldown * 2**streak:
                continue
            reasons = flags(row, messages, moment, self.runtime.watch)
            if not reasons:
                continue
            await self.delivery.deliver(
                await self.session_id(),
                wrap(
                    "system",
                    "review",
                    review_packet(
                        row, messages, reasons, moment, self.states[row["id"]].last_output
                    ),
                    worker=short(row["id"]),
                    title=row["title"],
                ),
                kind="inbound",
                display="; ".join(reasons),
                meta={"review": row["id"], "title": row["title"]},
            )
            self.save(
                row["id"], reviewed_at=moment, reviewed_mark=mark, streak=streak + int(unchanged)
            )
            reviewed.append(row["id"])
        return reviewed

    async def session_id(self) -> str:
        if rows := self.store.open_sessions(WATCHDOG):
            return rows[0]["id"]
        row = self.store.create_session(
            str(uuid4()), WATCHDOG, "Watchdog", "idle", cwd=str(self.runtime.role_dir(WATCHDOG))
        )
        await self.runtime.open_live(row, create=True)
        return row["id"]
