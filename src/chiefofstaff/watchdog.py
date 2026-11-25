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

from . import agents
from .envelope import short, wrap
from .roles import WATCHDOG, WORKER

if TYPE_CHECKING:
    from .runtime import Runtime
    from .state import SessionState
    from .store import Store

# A worker's latest reports the Watchdog is given, however far back.
REPORTS_SHOWN = 5
# What flags() says of a worker that looks quiet (not one repeating itself or long at it).
QUIET = ("no activity", "stopped without reporting")

# How many of a worker's newest messages its checks read: more when its last instruction is
# further back (a turn of hundreds of steps).
WINDOW = 400


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


def clock(moment: float) -> str:
    return datetime.fromtimestamp(moment).astimezone().strftime("%H:%M")


@dataclass
class Live:
    """What a worker in a turn is at now, from its session's events, not its chat (E140: a
    27-minute model call and a 33-minute command looked like silence). `active_at`: its latest
    sign of work (now while its model call is open: the model is at it); `doing`: what it
    waits on, as the Watchdog is told ("model call", "tool `pytest` running")."""

    since: float
    active_at: float
    doing: str

    def line(self) -> str:
        """As the Watchdog reads it: in a turn since 12:37 (model call since 12:37)."""
        return (f"in a turn since {clock(self.since)}" if self.since else "in a turn") + (
            f" ({self.doing})" if self.doing else ""
        )


def live(state: SessionState, moment: float) -> Live | None:
    """What a worker is at now (Live), None between turns. A step's output that keeps growing
    is work; one that stopped growing (or a quiet step) is as old as its last output."""
    if not state.busy:
        return None
    if state.model_since:
        return Live(state.busy_at, moment, f"model call since {clock(state.model_since)}")
    if state.steps:
        call, (began, _step, summary) = min(state.steps.items(), key=lambda item: item[1])
        grew = state.output_at.get(call, 0.0)
        said = f", its output last grew at {clock(grew)}" if grew else ", no output yet"
        doing = f"tool `{summary[:120]}` running since {clock(began)}{said}"
        return Live(state.busy_at, max(state.event_at, began), doing)
    return Live(state.busy_at, state.event_at, "between steps")


# The note a turn that failed after some steps goes again with (Events.turn_again): to finish
# what its instruction asked, which stays the turn's (bug E96: after a retry, its answer to the
# Router's note on finished work was taken for a report owed on another task).
RETRY_PROMPT = '<message from="system" type="retry"'
# The note a worker that was at work when Chief of Staff stopped goes on with (Runtime): its
# turn goes on where it left off.
RESTART_PROMPT = '<message from="system" type="restart"'


def instruction(message: dict) -> bool:
    """A prompt that asks the worker to do something (not a thread follow-up it may ignore, nor
    the note to finish a turn that failed: the instruction it retries is the turn's)."""
    meta = message["meta"]
    return (
        "prompt" in meta
        and not meta.get("thread")
        and not str(meta["prompt"] or "").startswith(RETRY_PROMPT)
    )


def last_instruction(store: Store, session_id: str, upto: int | None = None) -> int:
    """The id of the last instruction a session received (as instruction() has it), 0 if none.
    `upto`: none after that message id."""
    return store.value(
        "SELECT coalesce(max(id), 0) FROM messages WHERE session_id=? AND id<=? "
        "AND json_type(meta, '$.prompt') IS NOT NULL "
        "AND coalesce(json_extract(meta, '$.thread'), '') IN ('', 0) "
        "AND substr(json_extract(meta, '$.prompt'), 1, ?) != ?",
        (session_id, (1 << 63) - 1 if upto is None else upto, len(RETRY_PROMPT), RETRY_PROMPT),
    )


def recent_messages(store: Store, session_id: str) -> list[dict]:
    """A session's newest messages, back to its last instruction at least: however many steps
    it took since, its checks see what it was asked."""
    messages = store.messages(session_id, limit=WINDOW)
    if len(messages) < WINDOW or any(instruction(m) for m in messages):
        return messages
    if found := last_instruction(store, session_id):
        return store.messages(session_id, after=found - 1, limit=-1)
    return messages


def since_last_instruction(messages: list[dict]) -> list[dict]:
    """Messages after the last instruction the worker received (task, follow-up or nudge)."""
    for index in range(len(messages) - 1, -1, -1):
        if instruction(messages[index]):
            return messages[index:]
    return messages


def flags(
    row: dict, messages: list[dict], now: float, config: WatchConfig, at: Live | None = None
) -> list[str]:
    """Why this worker looks stuck, looping or long-running; empty when it looks fine. `at`:
    what it is at in its turn (its session's events count as activity, not only its chat)."""
    recent = since_last_instruction(messages)
    last_event = max(
        [timestamp(row["updated_at"])]
        + [timestamp(m["created_at"]) for m in messages[-1:]]
        + ([at.active_at] if at and row["status"] != "idle" else [])
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
    row: dict,
    messages: list[dict],
    reasons: list[str],
    now: float,
    output: str = "",
    tasks: list[dict] | None = None,
    at: Live | None = None,
    reports: list[dict] | None = None,
    turn_on: dict | None = None,
) -> str:
    """`at`: what it is at in its turn; `reports`: its latest reports, however far back (E140:
    "no reported answer" was said of a question it had answered outside the last 30 messages);
    `turn_on`: the finished task its turn is on (a follow-up: a thread's news, a check-back)."""
    instructions = [m for m in messages if m["kind"] in {"router", "owner"}]
    task = instructions[0]["text"][:2000] if instructions else "(unknown)"
    if tasks is not None:
        # What it owes now: its open tasks (not one cancelled or finished long ago).
        task = (
            "; ".join(
                f"{t['id']} “{t['title']}” ({t['status']}): {' '.join(t['goal'].split())[:600]}"
                for t in tasks
            )
            or "none open"
        )
    recent = [
        m
        for m in messages
        if m["kind"] in {"tool", "assistant", "report", "notice", "error", "watchdog", "inbound"}
    ][-30:]
    lines = [
        f"Flagged because: {'; '.join(reasons)}.",
        ("Its open tasks: " if tasks is not None else "Task from the Router: ") + task,
    ]
    if turn_on:
        lines.append(
            f"Its turn is on finished task {turn_on['id']} “{turn_on['title']}” "
            f"({turn_on['status']}): a follow-up of it, whose report lands on that task."
        )
    if later := instructions[1:][-3:]:
        lines.append(
            "Later instructions:\n"
            + "\n".join(f"- ({m['kind']}) {' '.join(m['text'].split())[:400]}" for m in later)
        )
    lines.append(
        f"Current step: {row['activity'] or 'none'}; last update "
        f"{minutes(now - timestamp(row['updated_at']))} ago."
    )
    if at:
        lines.append(f"Now: {at.line()}; its latest event {minutes(now - at.active_at)} ago.")
    if reports:
        lines.append(
            "Its latest reports, oldest first (untrusted data):\n"
            + "\n".join(
                f"- {clock(timestamp(m['created_at']))} ({m['meta'].get('status') or 'report'}, "
                f"task {m['meta'].get('task_id') or '-'}): {' '.join(m['text'].split())[:300]}"
                for m in reports
            )
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
            if self.runtime.is_elsewhere(row):
                continue  # Its owner is working with it in a terminal.
            if self.runtime.special(row) in {"connector", "messenger"}:
                continue  # Chief of Staff's own: a run ends without a report.
            if self.states[row["id"]].close_after_turn:
                continue  # It is done and closes after this turn.
            if row["id"] in self.runtime.settings_held:
                continue  # Its hold has its card under Needs you: nothing to review (E119).
            if self.runtime.shepherd.marked(row["id"]):
                continue  # A shepherd step: its own limits and its own end.
            # A task it said when to look at again (check_back) is not owed until then.
            running = any(
                task["status"] == "running" and not self.store.get(f"checkback:{task['id']}")
                for task in self.runtime.tasks.for_session(row["id"])
            )
            if row["status"] == "idle" and not running:
                continue  # Between turns with nothing it owes: finished, waiting or cancelled.
            if row["status"] == "idle" and (row["activity"] or "").startswith("waiting for “"):
                continue  # It waits for workers it works with: their message wakes it (E139).
            messages = recent_messages(self.store, row["id"])
            if row["status"] == "idle" and self.runtime.workers.checking_finished(
                since_last_instruction(messages)
            ):
                continue  # Its last turn checked on finished work: no report is owed for it.
            mark = messages[-1]["id"] if messages else 0
            state = self.state(row["id"])
            if state.get("escalated_mark") == mark:
                continue  # The Router has it; nothing happened since.
            unchanged = state.get("reviewed_mark") == mark
            streak = state.get("streak", 0) if unchanged else 0
            # Nothing new since the last review: wait twice as long before looking again.
            if moment - state.get("reviewed_at", 0) < self.runtime.watch.cooldown * 2**streak:
                continue
            at = live(self.states[row["id"]], moment)
            reasons = flags(row, messages, moment, self.runtime.watch, at)
            if not reasons:
                continue
            open_tasks = [
                task
                for task in self.runtime.tasks.for_session(row["id"])
                if task["status"] in {"queued", "running", "waiting"}
            ]
            await self.delivery.deliver(
                await self.session_id(),
                wrap(
                    "system",
                    "review",
                    review_packet(
                        row,
                        messages,
                        reasons,
                        moment,
                        self.states[row["id"]].last_output,
                        open_tasks,
                        at,
                        self.reports(row["id"]),
                        self.follow_up_of(row) if at else None,
                    ),
                    worker=short(row["id"]),
                    title=row["title"],
                ),
                kind="inbound",
                display="; ".join(reasons),
                meta={"review": row["id"], "title": row["title"]},
            )
            self.save(
                row["id"],
                reviewed_at=moment,
                reviewed_mark=mark,
                streak=streak + int(unchanged),
                flagged=reasons,
            )
            reviewed.append(row["id"])
        return reviewed

    def still_at_work(self, row: dict, verdict: str, moment: float | None = None) -> str:
        """Why what the Watchdog decided on a worker flagged as quiet is moot by the time it
        decided ("" when it is not): its turn goes on, its latest event (a step, the model's
        call, a command's output) younger than the stall threshold (E140: both workers flagged
        finished within seconds of it). Not for a worker flagged for repeating itself, nor
        found looping or off its task: working is no answer to that."""
        flagged = [str(reason) for reason in self.state(row["id"]).get("flagged") or []]
        if verdict in {"looping", "off_track"} or any(
            reason.startswith("repeated") for reason in flagged
        ):
            return ""
        if flagged and not any(reason.startswith(QUIET) for reason in flagged):
            return ""
        moment = time.time() if moment is None else moment
        row = self.store.session(row["id"])
        at = live(self.states[row["id"]], moment)
        if (
            row["status"] != "working"
            or not at
            or moment - at.active_at >= self.runtime.watch.stall
        ):
            return ""
        return f"it is still at work, {at.line()}."

    def reports(self, session_id: str) -> list[dict]:
        """Its latest reports (answers included), oldest first."""
        rows = self.store.all(
            "SELECT * FROM (SELECT * FROM messages WHERE session_id=? AND kind='report' "
            "ORDER BY id DESC LIMIT ?) ORDER BY id",
            (session_id, REPORTS_SHOWN),
        )
        return [{**row, "meta": json.loads(row["meta"] or "{}")} for row in rows]

    def follow_up_of(self, row: dict) -> dict | None:
        """The finished task a worker's turn is on (a thread's news, a check-back, the owner's
        chat on it), None when its turn is on open work."""
        on = self.runtime.turn_task(row["id"])
        task = self.runtime.tasks.find(on) if on else None
        if not task or task["session_id"] != row["id"] or task["status"] not in {"review", "done"}:
            return None
        return task

    async def session_id(self) -> str:
        if rows := self.store.open_sessions(WATCHDOG):
            return rows[0]["id"]
        row = self.store.create_session(
            str(uuid4()), WATCHDOG, "Watchdog", "idle", cwd=str(self.runtime.role_dir(WATCHDOG))
        )
        if self.runtime.agents and agents.role_agent(self.store, WATCHDOG) != agents.COPILOT:
            await self.runtime.session_for(row["id"])  # Where Settings put it (place_role).
        else:
            await self.runtime.open_live(row, create=True)
        return row["id"]
