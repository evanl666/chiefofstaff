"""Watchdog heuristics: cheap checks that decide when a worker deserves a model's review."""

import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime


@dataclass
class WatchConfig:
    interval: float = 60  # how often workers are checked
    stall: float = 900  # no activity for this long
    long: float = 2700  # same instructions for this long: periodic drift check
    cooldown: float = 600  # minimum time between reviews of one worker
    repeats: int = 6  # the same step this often among the last 15 steps
    nudges: int = 2  # interventions without a new report before escalating to the Router

    @classmethod
    def from_env(cls) -> "WatchConfig":
        config = cls()
        for name in ("interval", "stall", "long", "cooldown"):
            if value := os.environ.get(f"WORK_BUDDY_WATCHDOG_{name.upper()}"):
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
