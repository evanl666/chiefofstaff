"""Where each of the owner's other Copilot sessions stands, read from the logs Copilot keeps.

The sidebar's Other Copilot sessions lists the owner's sessions that Chief of Staff does not
manage (Workers.external). With the `other_sessions` setting on, this index says where each one
stands: it asks the owner something, it is their turn, it works, it made no progress… It reads
the logs the CLI writes (docs/design/adopt-sessions.md, sections 4.2 to 4.6) in one thread,
within caps, only while a tab is open; starts no model call; and keeps what a session said in
memory only, at most 1 KB a session, for the owner's own tabs: never stored, logged, or given to
the Router, workers, the connector or habits.

What the page gets
------------------

``GET /api/others``: every session the index tracks (the owner's open ones, and those closed in
the last 24 hours; at most 200, open ones first)::

    {"run": "3f2a9c1e",     # this server's run: with another one, pass numbers start again
     "pass": 412,           # the latest pass: an `others` event with a lower one is older news
     "why": "",             # why no row can have a status, when none can ("" otherwise)
     "rows": [row, ...]}

A row is what ``GET /api/external`` gives for a session (id, summary, cwd, repository, branch,
modified, open), and::

     "status": status,      # null when it has none
     "reason": "reading",   # with no status, why not: a key of WHY ("" with a status)
     "why": "...",          # the same in words, for the row's tooltip
     "valid_for": 13.9      # with a status: for how many seconds from now it may be shown

A status::

    {"key": "asks",         # asks, your_turn, working, no_progress, idle, finished or stopped
     "elapsed": 720.4,      # seconds since `since` as of sending (null: not known); the page
                            # counts on with its own monotonic clock
     "since": "2026-11-10T14:10:00.000Z",  # the event's own time, to show; null when none is
                            # trusted (see Moment)
     "before": false,       # working only: the turn began before the first event read, so
                            # `since` is that event's time ("since before 2:10 PM")
     "words": "..."}        # at most 200 characters: the question, answer, step or error

``GET /api/others/{id}``: one session from the latest listing, as a row (with a status only
when it is tracked), for a selection kept across a reload; 404 for a session that is not the
owner's to see (Chief of Staff's own, remote, or not listed); 409 while Copilot is not connected
and nothing was listed yet.

The ``others`` event, after a pass with something to say (about every 2 s while a tab is open
and a session has a status)::

    {"run": "3f2a9c1e", "pass": 413,
     "rows": [row, ...],    # rows new or changed since the last event, without valid_for
     "gone": ["<id>"],      # rows no longer tracked
     "leases": {"<id>": 14.9}}  # every row whose status holds as of this pass: its valid_for

A status is shown only while its lease holds. Every pass renews it, unchanged ones included;
one not renewed in time is over, and the row looks as it does without the setting. The page
drops every lease when its stream breaks, then reads ``GET /api/others`` and ignores events of
an earlier pass of the same run.

With the setting off nothing runs: ``GET /api/others`` has no rows, ``GET /api/others/{id}`` is
404, and no event is sent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import stat
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .backend import OWN_CLIENTS, SESSION_ID, made_by_us
from .events import PLUMBING_TOOLS, describe_tool
from .workers import own_event

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)


def counting_sleep() -> Callable[[], float]:
    """The clock elapsed time is measured on. Monotonic, and going on while the machine sleeps:
    time.monotonic stands still while a Mac sleeps (mach_absolute_time), so a turn that ended the
    evening before would still be the owner's turn in the morning. CLOCK_MONOTONIC counts sleep
    on macOS, CLOCK_BOOTTIME on Linux."""
    if sys.platform == "linux" and hasattr(time, "CLOCK_BOOTTIME"):
        which = time.CLOCK_BOOTTIME
    elif sys.platform == "darwin" and hasattr(time, "CLOCK_MONOTONIC"):
        which = time.CLOCK_MONOTONIC
    else:
        return time.monotonic
    return lambda: time.clock_gettime(which)


# The clocks, as module attributes so tests can move them: elapsed time is measured on one that
# counts sleep; the wall clock only checks and dates the events' own times.
clock = counting_sleep()
wall = time.time

# A status is shown for at most this long after its log's size was last sampled.
FRESH = 15.0
# Passes run back to back when reading starts (a tab connects, the setting turns on, Copilot
# reconnects) until every session has had its first look, then one STEADY seconds after the
# last ended. Each reads at most so many bytes, for at most PASS_SECONDS in its thread.
STEADY = 2.0
STARTUP_BYTES = 16 << 20
STEADY_BYTES = 4 << 20
PASS_SECONDS = 0.1
# The least a session with bytes to read gets in a steady pass; the others wait their turn.
SHARE = 256 << 10
# Read at most this much at a time: the stop flag is checked between reads.
CHUNK = 1 << 20
# A line of a type the reducer uses that is longer than this is not parsed: a gap.
LINE_CAP = 2 << 20
# A session further behind than this jumps to its end: a gap.
JUMP = 32 << 20
# A log up to WHOLE is read from its start on a first look; a longer one from the smallest of
# these windows at its end that holds the start of one of its own model calls.
WHOLE = 4 << 20
WINDOWS = (64 << 10, 256 << 10, 1 << 20, 4 << 20)
# The first line of a long log (its format) is looked for in its first HEAD bytes.
HEAD = 64 << 10
# Copilot's list of sessions is read at most once a LIST_EVERY, and when what is open changes,
# at most once a LIST_AGAIN.
LIST_EVERY = 60.0
LIST_AGAIN = 2.0
# Sessions tracked; a closed one is tracked for CLOSED seconds after it last changed.
MOST = 200
CLOSED = 24 * 3600.0
# What is kept of a session: its latest words, its pending steps (each one's words), and what
# the page is given of the words.
WORDS = 1024
PENDING = 16
STEP = 128
SHOWN = 200
# An event's own time is shown only when it is no later than now and this.
AHEAD = 120.0
# Sample times kept to date what is read later than it was written.
SAMPLES = 64
# While a pass runs this long, what became too old to show is withdrawn meanwhile.
WATCH = 1.0
# No progress: an open turn whose log has not grown for this long. Idle: an open session whose
# turn ended this long ago. Constants, not settings; a live test shortens them.
STALLED = float(os.environ.get("CHIEFOFSTAFF_OTHERS_STALL_SECONDS", "900"))
IDLE = float(os.environ.get("CHIEFOFSTAFF_OTHERS_IDLE_SECONDS", "3600"))

# Section 4.3: the event types the reducer uses (Reducer.apply names the fields each needs)…
USED = frozenset(
    {
        "assistant.turn_start",
        "user.message",
        "assistant.message",
        "tool.execution_start",
        "tool.user_requested",
        "tool.execution_complete",
        "permission.requested",
        "permission.completed",
        "assistant.turn_end",
        "abort",
        "session.error",
        "session.shutdown",
        "session.resume",
        "session.snapshot_rewind",
    }
)
# …those seen in logs that change nothing a status depends on, passed over by their type
# without being parsed (system.notification: a turn it starts begins with a turn start)…
NEUTRAL = frozenset(
    {
        "human_response.recorded",
        "session.auto_mode_resolved",
        "session.binary_asset",
        "session.info",
        "session.warning",
        "session.model_change",
        "session.permissions_changed",
        "session.truncation",
        "system.message",
        "system.notification",
    }
)
NEUTRAL_FAMILIES = (
    "external_tool.",
    "hook.",
    "instruction.update",
    "model.",
    "session.compaction_",
    "session.usage_",
    "subagent.",
)
# …and any other type makes the state partial: a new type costs coverage, never correctness.

# How every line of a log starts: its type, read without parsing the line.
PREFIX = b'{"type":"'
# Where an own model call may start, in a long log's end (the line after a newline).
TURN_START = b'\n{"type":"assistant.turn_start"'

# Why a row has no status, for its tooltip.
WHY = {
    "unlisted": "Copilot did not say which sessions are yours.",
    "unknown": "Copilot did not say which app started it.",
    "another": "Another Chief of Staff ran it last.",
    "missing": "It has no log to read.",
    "unreadable": "Its log is not a file that can be read safely.",
    "format": "Its log is in a format this version does not read.",
    "reading": "Not caught up with its log yet.",
    "no_start": "Nothing recent its status can start from.",
    "partial": "Its log has something this version cannot follow: its status shows again from "
    "its next step.",
    "waiting": "Not watched long enough yet to tell.",
    "fresh": "It has not started on anything yet.",
    "closed": "Closed more than a day ago.",
}


def group(kind: str) -> str:
    """used, neutral or unknown (section 4.3)."""
    if kind in USED:
        return "used"
    if kind in NEUTRAL or kind.startswith(NEUTRAL_FAMILIES):
        return "neutral"
    return "unknown"


def type_of(data: bytes, start: int, end: int) -> str | None:
    """The type a line at `start` names first, read without parsing it; None when the line does
    not start with it within its first 128 bytes (before `end`)."""
    if not data.startswith(PREFIX, start, end):
        return None
    close = data.find(b'"', start + len(PREFIX), min(end, start + 128))
    if close < 0:
        return None
    try:
        return data[start + len(PREFIX) : close].decode("ascii")
    except UnicodeDecodeError:
        return None


def parse(line: bytes) -> dict | None:
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return event if isinstance(event, dict) else None


def cut(text: str, limit: int) -> str:
    """At most `limit` bytes of UTF-8, cut at a character boundary. What UTF-8 cannot hold (a
    lone surrogate: JSON can carry one, half an emoji) becomes "?": sent to the tabs, it would
    end the event stream of every one."""
    return text.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")


def one_line(value: Any, limit: int) -> str:
    return cut(" ".join(str(value or "").split()), limit)


def ident(value: Any) -> str:
    """An id an event names (a step's, an approval's), or "" when it names none usable."""
    return value if isinstance(value, str) and 0 < len(value) <= 128 else ""


def epoch(value: Any) -> float | None:
    """An event's own time (ISO, with its zone), in seconds; None when it does not parse."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        moment = datetime.fromisoformat(value)
        return moment.timestamp() if moment.tzinfo else None
    except (ValueError, OverflowError):
        return None


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds")[:23] + "Z"


@dataclass(frozen=True, slots=True)
class Moment:
    """When something happened, on two clocks never mixed. `shown`: the event's own time, to
    show ("" when it is not trusted: unparsable, later than now and AHEAD, or before the
    session's start). `seen`: the time (on `clock`) elapsed time is counted from. `ahead`: how long
    before `seen` it happened: 0 for what the reader saw come (dated by the sample that first
    held it); for what was written before the reader watched, now less its own time when it
    read it; None when that is not known, so it is at least now less `seen`."""

    shown: str
    seen: float
    ahead: float | None

    def elapsed(self, now: float) -> float | None:
        return None if self.ahead is None else self.ahead + max(0.0, now - self.seen)

    def at_least(self, now: float) -> float:
        return (self.ahead or 0.0) + max(0.0, now - self.seen)


@dataclass(frozen=True, slots=True)
class Step:
    """A step begun and not ended: a tool call, or an approval asked for."""

    words: str  # what it runs (at most STEP bytes); an ask_user's question is the words'
    at: Moment
    asks: bool  # it waits for the owner (ask_user, an approval)
    call: str = ""  # the tool call's id (an approval's is its request id)


class Reducer:
    """What a session's own events say about where it stands (section 4.4), applied in order.

    The state is complete after a reset point (the log's start, an own model call's start) with
    no gap since; a gap, an event it does not know, a rewind, a resume, a completion of a step
    it does not hold or too many pending steps make it partial: what it holds is dropped, never
    carried across, until the next reset point."""

    __slots__ = (
        "approvals",
        "complete",
        "ended",
        "ended_at",
        "exited",
        "final",
        "first",
        "last",
        "last_step",
        "pending",
        "question",
        "since",
        "turn",
        "words",
    )

    def __init__(self, complete: bool):
        self.complete = complete
        # A turn open, from the owner's message to its end; None: not known (since a gap, until
        # a turn is seen to end).
        self.turn: bool | None = False if complete else None
        self.since: Moment | None = None  # when the open turn began; None: before `first`
        self.first: Moment | None = None  # the first event since the start or the last gap
        self.last: Moment | None = None  # the last own event
        self.ended = ""  # how the last turn ended: answered, stopped or failed
        self.ended_at: Moment | None = None
        self.final = False  # the open turn's final answer was seen
        self.pending: dict[str, Step] = {}
        self.approvals: dict[str, Step] = {}
        self.words = ""  # its latest words: an answer, a question asked, an error
        self.question = ""  # the ask_user call whose question `words` holds
        self.last_step = ""
        self.exited = False

    def partial(self):
        self.complete = False
        self.turn, self.since, self.first = None, None, None
        self.ended, self.ended_at, self.final = "", None, False
        self.pending.clear()
        self.approvals.clear()
        self.words, self.question, self.last_step = "", "", ""
        self.exited = False

    def reset(self, at: Moment):
        """An own model call starts: every step begun before it ended (the invariant of section
        4.1), the turn is open and its final answer is still to come."""
        if self.turn is False:
            self.since = at  # A turn it began by itself (after a notification).
        elif self.turn is None:
            self.since = None  # It began before the first event read.
        self.turn, self.complete, self.final, self.exited = True, True, False, False
        self.ended, self.ended_at = "", None
        self.pending.clear()
        self.approvals.clear()
        if self.question:
            self.words, self.question = "", ""

    def end(self, how: str, at: Moment, words: str = ""):
        self.turn, self.ended, self.ended_at, self.final = False, how, at, False
        self.pending.clear()
        self.approvals.clear()
        self.words, self.question = words, ""

    def held(self):
        if len(self.pending) + len(self.approvals) > PENDING:
            self.partial()

    def apply(self, kind: str, event: dict, at: Moment):
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        self.last = at
        if self.first is None:
            self.first = at
        if kind == "assistant.turn_start":
            self.reset(at)
        elif kind == "user.message":
            if self.turn is False:
                self.turn, self.since = True, at
                self.ended, self.ended_at = "", None
            self.final = False  # What answers it is still to come.
        elif kind == "assistant.message":
            content = data.get("content")
            if isinstance(content, str) and content.strip() and not self.question:
                self.words = cut(content.strip(), WORDS)
            self.final = not data.get("toolRequests")
        elif kind in {"tool.execution_start", "tool.user_requested"}:
            self.started(data, at)
        elif kind == "tool.execution_complete":
            self.completed(self.pending, ident(data.get("toolCallId")))
        elif kind == "permission.requested":
            if not (request := ident(data.get("requestId"))):
                return self.partial()
            asked = data.get("permissionRequest")
            asked = asked if isinstance(asked, dict) else {}
            what = next(
                (
                    asked[key]
                    for key in ("fullCommandText", "intention", "path", "url")
                    if asked.get(key)
                ),
                asked.get("kind") or "a step",
            )
            words = one_line(f"Permission for {one_line(what, STEP)}", STEP)
            self.approvals[request] = Step(words, at, True, request)
            self.held()
        elif kind == "permission.completed":
            self.completed(self.approvals, ident(data.get("requestId")))
        elif kind == "assistant.turn_end":
            if (
                self.complete
                and self.turn
                and self.final
                and not self.pending
                and not self.approvals
            ):
                self.end("answered", at, self.words)
        elif kind == "abort":
            self.end("stopped", at)
        elif kind == "session.error":
            self.end("failed", at, one_line(data.get("message") or "It failed.", WORDS))
        elif kind == "session.shutdown":
            self.pending.clear()
            self.approvals.clear()
            self.exited = True
        else:  # session.resume (a new process may go on with pending work, or not); a rewind
            self.partial()
        return None

    def started(self, data: dict, at: Moment):
        if not (call := ident(data.get("toolCallId"))):
            return self.partial()
        name = one_line(data.get("toolName") or "tool", 64)
        arguments = data.get("arguments")
        if name == "ask_user":
            if isinstance(arguments, str):
                arguments = parse(arguments.encode("utf-8", "replace"))
            asked = arguments if isinstance(arguments, dict) else {}
            question = asked.get("message") or asked.get("question") or ""
            self.words, self.question = cut(str(question).strip(), WORDS), call
            self.pending[call] = Step("", at, True, call)
        else:
            words = "" if name in PLUMBING_TOOLS else cut(describe_tool(name, arguments), STEP)
            self.pending[call] = Step(words, at, False, call)
            self.last_step = words or self.last_step
        self.held()
        return None

    def completed(self, held: dict[str, Step], key: str):
        if not key:
            self.partial()
        elif held.pop(key, None) is None:
            if self.complete:
                self.partial()  # A reset that was wrong: it held a step it did not know.
        elif key == self.question:
            self.words, self.question = "", ""


@dataclass(frozen=True, slots=True)
class Facts:
    """What a pass found of one session, for the event loop to say where it stands: never
    changed after, so it is read while the next pass runs (which makes new ones only for the
    sessions it changes; when each sample was taken is beside them)."""

    reason: str  # why there is nothing to tell from, or ""
    caught: bool  # read to the end of its latest sample
    grew: Moment | None  # when its log last grew
    complete: bool
    turn: bool | None
    since: Moment | None
    first: Moment | None
    ended: str
    ended_at: Moment | None
    exited: bool
    asking: Step | None  # what waits for the owner, the earliest
    asked: str  # its words: the question, or what the approval is for
    step: str  # the latest pending step's words
    last_step: str
    words: str
    last: Moment | None


def read_chunk(fd: int, size: int, position: int) -> bytes:
    return os.pread(fd, size, position)


class Budget:
    """What one pass may still read, and whether it must stop."""

    def __init__(self, limit: int, stopped: Callable[[], bool]):
        self.limit = limit
        self.used = 0
        self.began = time.perf_counter()
        self.stopped = stopped

    def spent(self) -> bool:
        return (
            self.used >= self.limit
            or time.perf_counter() - self.began >= PASS_SECONDS
            or self.stopped()
        )

    def room(self, wanted: int) -> int:
        """How much of `wanted` it may still read."""
        return max(0, min(wanted, self.limit - self.used))


class Log:
    """One session's log as the reader follows it. Changed by the reading thread during a pass
    and by the event loop only between passes; the event loop reads only the Facts a pass
    returns."""

    def __init__(self, session_id: str, path: Path | None):
        self.session_id = session_id
        self.path = path
        self.folder = os.fspath(path.parent) if path else ""
        self.known: Facts | None = None  # What the last pass that changed it found.
        self.forget("missing" if path is None else "")

    def forget(self, problem: str):
        """From nothing: a first look comes next (gone, replaced, rewritten or refused)."""
        self.changed = True
        self.problem = problem  # missing, unreadable, format or no_start
        self.ident: tuple[int, int] | None = None  # the file read: (device, inode)
        self.size = 0  # at the latest sample
        self.sampled = 0.0
        self.before = 0  # its size when the reader first looked: what was written before
        self.samples: deque[tuple[int, float]] = deque()  # (size, when it was first seen)
        self.grew: Moment | None = None
        self.started: float | None = None  # the session's start (its first line's time)
        self.head: bool | None = None  # its first line is a log this version reads
        self.looked = False  # where to read from is chosen (a first look)
        self.look_end = 0  # a long log's size when its first look began…
        self.low = 0  # …and how far back from there it has looked for a model call's start
        self.offset = 0  # where the next line to read starts
        self.read_to = 0  # how far the log has been read (into a line being passed over)
        self.skip = False  # in the middle of a line being passed over
        self.reducer = Reducer(complete=True)

    def sample(self, now: float, at: float):
        """Its size now. Only a regular file in a real folder is read: never a link, a pipe (it
        would block the thread) or another kind of file."""
        if self.path is None:
            return
        try:
            folder = os.lstat(self.folder)
            info = os.lstat(self.path)
        except OSError:
            self.refuse("missing")
            return
        if not (stat.S_ISDIR(folder.st_mode) and stat.S_ISREG(info.st_mode)):
            self.refuse("unreadable")
            return
        if self.ident and ((info.st_dev, info.st_ino) != self.ident or info.st_size < self.size):
            self.forget("")  # Replaced or rewritten: looked at afresh.
        if self.ident is None or info.st_size > self.size:
            # When it grew, by the file system's clock (the wall clock, as the events' times).
            changed = info.st_mtime
            trusted = changed <= at + AHEAD and (self.started is None or changed >= self.started)
            shown = iso(changed) if trusted else ""
            self.changed = True
            if self.ident is None:
                self.problem = ""
                self.ident = (info.st_dev, info.st_ino)
                self.size = self.before = self.look_end = self.low = info.st_size
                self.grew = Moment(shown, now, max(0.0, at - changed) if trusted else None)
            else:
                self.samples.append((info.st_size, now))
                if len(self.samples) > SAMPLES:
                    del self.samples[1]  # Read later than it came, it is dated a little late.
                self.size = info.st_size
                self.grew = Moment(shown, now, 0.0)
        self.sampled = now
        if self.looked and self.size - (self.read_to if self.skip else self.offset) > JUMP:
            # Too far behind: a gap, then its end is looked at as on a first look.
            self.reducer.partial()
            self.looked, self.skip = False, False
            self.look_end = self.low = self.size

    def refuse(self, problem: str):
        if self.ident or self.problem != problem:
            self.forget(problem)

    def unread(self) -> bool:
        return (
            self.ident is not None
            and self.head is not False
            and (not self.looked or self.read_to < self.size)
        )

    def open(self) -> int | None:
        """The log, opened without following a link and without waiting (a pipe put there
        since it was sampled), while it is still the file sampled."""
        if self.path is None:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(self.path, flags | getattr(os, "O_CLOEXEC", 0))
        except OSError:
            self.forget("missing")
            return None
        try:
            info = os.fstat(fd)
        except OSError:
            os.close(fd)
            return None
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != self.ident:
            os.close(fd)
            self.forget("" if stat.S_ISREG(info.st_mode) else "unreadable")
            return None
        return fd

    def read(self, share: int, budget: Budget, now: float, at: float) -> int:
        """Reads on, at most `share` bytes of lines (a first look's search besides); returns
        how much it read."""
        before = budget.used
        fd = self.open()
        if fd is None:
            return 0
        try:
            if not self.looked and not self.look(fd, budget):
                return budget.used - before
            if self.head is False:
                self.offset = self.read_to = self.size
            else:
                start = self.read_to if self.skip else self.offset
                self.scan(fd, min(self.size, start + share), budget, now, at)
        finally:
            os.close(fd)
        return budget.used - before

    def look(self, fd: int, budget: Budget) -> bool:
        """A first look: where to read from. True once chosen (it may take passes)."""
        if self.size <= WHOLE:
            self.reducer = Reducer(complete=True)  # From its start, a reset point.
            self.offset = self.read_to = 0
            self.looked, self.skip = True, False
            return True
        if self.head is None:
            if budget.room(HEAD) < HEAD:
                return False  # Its first line is read whole, in the next pass.
            data = read_chunk(fd, HEAD, 0)
            budget.used += len(data)
            end = data.find(b"\n")
            self.check(data[:end] if end >= 0 else b"")
            if not self.head:
                self.looked = True
                return True
        found = self.search(fd, budget)
        if found is None:
            return False
        self.reducer = Reducer(complete=False)
        if found < 0:
            self.problem = "no_start"
            found = self.look_end
        # From the line after the one it is in (it starts a line when a newline is before it).
        self.offset = self.read_to = max(0, found - 1)
        self.looked, self.skip = True, True
        return True

    def search(self, fd: int, budget: Budget) -> int | None:
        """Where the smallest window at the log's end that holds an own model call's start
        begins; -1 when even the largest holds none; None when the pass ran out first (the
        next one goes on from where it stopped)."""
        end = self.look_end
        for window in WINDOWS:
            floor = max(0, end - window)
            while self.low > floor:
                # From the byte before (a line starting at `low`) to just past what was searched
                # already (a start cut by the last read).
                room = budget.room(CHUNK) - len(TURN_START) - 1
                if budget.spent() or room <= 0:
                    return None
                low = max(floor, self.low - room)
                start, stop = max(0, low - 1), min(end, self.low + len(TURN_START))
                data = read_chunk(fd, stop - start, start)
                budget.used += len(data)
                self.low = low
                if self.own_start_in(fd, data, start, budget):
                    return floor
        return -1

    def own_start_in(self, fd: int, data: bytes, base: int, budget: Budget) -> bool:
        at = len(data)
        while (at := data.rfind(TURN_START, 0, at)) >= 0:
            end = data.find(b"\n", at + 1)
            if end >= 0:
                line = data[at + 1 : end]
            else:
                line = read_chunk(fd, budget.room(HEAD), base + at + 1)
                budget.used += len(line)
                line = line[: line.find(b"\n")] if b"\n" in line else b""
            event = parse(line)
            if event and isinstance(event.get("data", {}), dict) and own_event(event):
                return True
        return False

    def check(self, line: bytes):
        """The log's first line: a session's start, by Copilot's agent, in the format this
        version reads (section 4.3)."""
        event = parse(line) or {}
        data = event.get("data")
        if not (
            event.get("type") == "session.start"
            and isinstance(data, dict)
            and data.get("producer") == "copilot-agent"
            and type(data.get("version")) is int
            and data["version"] == 1
        ):
            self.head, self.problem = False, "format"
            return
        self.head = True
        self.started = epoch(event.get("timestamp")) or epoch(data.get("startTime"))

    def scan(self, fd: int, end: int, budget: Budget, now: float, at: float):
        """Reads lines up to `end`, a chunk at a time. Lines of neutral types are passed over by
        their type, whatever their length; a line of another type longer than LINE_CAP is a
        gap. A line not complete by `end` is read again next time."""
        position = self.read_to if self.skip else self.offset
        carry = b""
        while position < end and not budget.spent():
            data = read_chunk(fd, budget.room(min(CHUNK, end - position)), position)
            if not data:
                break
            budget.used += len(data)
            base, size = position, len(data)
            position += size
            i = 0
            while i < size:
                if self.skip:
                    newline = data.find(b"\n", i)
                    if newline < 0:
                        break
                    i, self.skip = newline + 1, False
                    self.offset = base + i
                    continue
                newline = data.find(b"\n", i)
                stop = newline if newline >= 0 else size
                if carry:
                    if len(carry) + stop - i > LINE_CAP:
                        carry = b""
                        self.gap()
                        continue
                    carry += data[i:stop]
                    if newline < 0:
                        break
                    line, carry = carry, b""
                else:
                    kind = type_of(data, i, stop)
                    if kind is None and newline < 0 and i > 0 and size - i < 128 and position < end:
                        position = base + i  # Its type is in the next read.
                        break
                    if kind is not None and self.head is not None and group(kind) == "neutral":
                        self.skip = True
                        continue
                    if stop - i > LINE_CAP:
                        self.gap()
                        continue
                    if newline < 0:
                        carry = data[i:size]
                        break
                    line = data[i:newline]
                i = newline + 1
                self.offset = base + i
                self.apply(line, base + i, now, at)
                if self.head is False:
                    self.read_to = self.offset = self.size
                    return
        self.read_to = max(position, self.offset)

    def gap(self):
        """A line it will not parse: passed over, and what it held is not known."""
        if self.head is None:
            self.head, self.problem = False, "format"
        self.reducer.partial()
        self.skip = True

    def apply(self, line: bytes, end: int, now: float, at: float):
        if self.head is None:
            self.check(line)
            return
        kind = type_of(line, 0, len(line))
        event = None
        if kind is None:
            event = parse(line)
            kind = event.get("type") if event else None
            if not isinstance(kind, str):
                self.reducer.partial()  # A line it cannot read.
                return
        found = group(kind)
        if found == "neutral":
            return
        if found == "unknown":
            self.reducer.partial()
            return
        event = event or parse(line)
        if event is None or not isinstance(event.get("data", {}), dict):
            self.reducer.partial()
            return
        if own_event(event):
            self.reducer.apply(kind, event, self.moment(event, end, now, at))

    def moment(self, event: dict, end: int, now: float, at: float) -> Moment:
        """When an event ending at `end` happened: dated by the sample that first held it when
        the reader saw it come; else by its own time."""
        own = epoch(event.get("timestamp"))
        if own is not None and (
            own > at + AHEAD or (self.started is not None and own < self.started)
        ):
            own = None
        shown = iso(own) if own is not None else ""
        if end > self.before:
            while len(self.samples) > 1 and self.samples[0][0] < end:
                self.samples.popleft()
            seen = self.samples[0][1] if self.samples else now
            return Moment(shown, seen, 0.0)
        return Moment(shown, now, None if own is None else max(0.0, at - own))

    def facts(self) -> Facts:
        reducer = self.reducer
        if reducer.complete and self.problem == "no_start":
            self.problem = ""
        held = (*reducer.pending.values(), *reducer.approvals.values())
        asking = min((step for step in held if step.asks), key=lambda s: s.at.seen, default=None)
        step = next((s.words for s in reversed(reducer.pending.values()) if s.words), "")
        if self.problem:
            reason = self.problem
        elif not self.looked or self.read_to < self.size or self.ident is None:
            reason = "reading"
        else:
            reason = ""
        return Facts(
            reason=reason,
            caught=self.looked and self.read_to >= self.size,
            grew=self.grew,
            complete=reducer.complete,
            turn=reducer.turn,
            since=reducer.since,
            first=reducer.first,
            ended=reducer.ended,
            ended_at=reducer.ended_at,
            exited=reducer.exited,
            asking=asking,
            asked=(
                reducer.words
                if asking and asking.call == reducer.question
                else (asking.words if asking else "")
            ),
            step=step,
            last_step=reducer.last_step,
            words=reducer.words,
            last=reducer.last,
        )


Found = tuple[str, dict[str, Facts], dict[str, float], int]


def read_pass(logs: list[Log], startup: bool, cursor: str, stopped: Callable[[], bool]) -> Found:
    """One pass, in the reading thread: samples every log, then reads those with bytes to read
    within the pass's caps. On a start, in their order (open ones first) until each has had its
    first look; then round-robin from where the last pass stopped, each up to its share.
    Returns where the next pass starts, what it found, when each log was sampled, and how many
    bytes it read."""
    now, at = clock(), wall()
    budget = Budget(STARTUP_BYTES if startup else STEADY_BYTES, stopped)
    for log in logs:
        log.sample(now, at)
    waiting = [log for log in logs if log.unread()]
    if not startup:
        # Round-robin: from the one after where the last pass stopped.
        after = next((n for n, log in enumerate(logs) if log.session_id == cursor), -1) + 1
        later = {log.session_id for log in logs[after:]}
        waiting = [log for log in waiting if log.session_id in later] + [
            log for log in waiting if log.session_id not in later
        ]
    share = budget.limit if startup else max(SHARE, budget.limit // max(1, len(waiting)))
    for log in waiting:
        if budget.spent():
            break
        read = log.read(share, budget, now, at)
        log.changed = True
        if read >= share or not log.unread() or not budget.spent():
            cursor = log.session_id  # It had its share: the next pass starts after it.
    found = {}
    for log in logs:
        if log.changed or log.known is None:
            log.known, log.changed = log.facts(), False
        found[log.session_id] = log.known
    return cursor, found, {log.session_id: log.sampled for log in logs}, budget.used


@dataclass(frozen=True, slots=True)
class Row:
    """A listed session the sidebar shows, as the latest listing has it."""

    item: dict  # as Workers.external gives it, without "open"
    whose: str  # owner, unknown, unlisted or another (section 4.2)
    modified: float
    meta: Any = field(compare=False)  # The listing's: kept while a listing has it unchanged.


def whose(meta: Any, managed: set[str]) -> str:
    """Section 4.2: whose a listed session is, the first rule that matches. ours and remote are
    left out; unlisted (the raw answer was not read), unknown (no maker named) and another (made
    by another client and last run by a Chief of Staff) are listed without a status; owner: the
    owner's own, with one."""
    if meta.session_id in managed:
        return "ours"
    if getattr(meta, "is_remote", False):
        return "remote"
    if not getattr(meta, "clients_listed", False):
        return "unlisted"
    made = getattr(meta, "client_name", "") or ""
    last = getattr(meta, "last_client_name", "") or ""
    if not made:
        return "unknown"
    if getattr(meta, "made_by_us", False) or made_by_us(
        {"clientName": made, "lastClientName": last}
    ):
        return "ours"
    if last in OWN_CLIENTS:
        return "another"
    return "owner"


def item_of(meta: Any) -> dict:
    """A listed session as the sidebar's list has it (Workers.external), without "open"."""
    context = getattr(meta, "context", None)
    modified = getattr(meta, "modified_time", None)
    # The listing's words are the CLI's (a summary cut in the middle of an emoji): see cut.
    return {
        "id": cut(meta.session_id, 200),
        "summary": cut((getattr(meta, "summary", "") or "Untitled session")[:200], 800),
        "cwd": cut((context.working_directory if context else "") or "", 4096),
        "repository": cut((context.repository if context else "") or "", 400),
        "branch": cut((context.branch if context else "") or "", 400),
        "modified": modified.isoformat() if isinstance(modified, datetime) else "",
    }


def where(facts: Facts | None, sampled: float, whose: str, is_open: bool, now: float) -> Any:
    """Where a session stands (section 4.5), from what was found as of a sample taken at
    `sampled`: (key, since, before, words), or why it cannot be said (a key of WHY)."""
    if whose != "owner":
        return whose
    if facts is None:
        return "reading"
    if facts.reason:
        return facts.reason
    if not facts.caught or now - sampled > FRESH:
        return "reading"
    if not facts.complete:
        return "partial"
    if not is_open:
        last = facts.last.elapsed(now) if facts.last else None
        if last is not None and last > CLOSED:
            return "closed"
        if facts.turn is False and facts.ended == "answered":
            return ("finished", facts.last, False, facts.words)
        if facts.turn is False and not facts.ended:
            return "fresh"
        # Its last turn ended stopped or failed, or did not end: its last step then.
        ended = stopped(facts) if facts.turn is False else ""
        return ("stopped", facts.last, False, ended or facts.last_step)
    if facts.exited:
        return "reading"  # Open again, and its log has not said so yet.
    if facts.asking:
        return ("asks", facts.asking.at, False, facts.asked)
    if facts.turn is False:
        if facts.ended_at is None:
            return "fresh"
        words = stopped(facts) or facts.words
        age = facts.ended_at.elapsed(now)
        if age is None:
            return (
                ("idle", facts.ended_at, False, words)
                if facts.ended_at.at_least(now) >= IDLE
                else "waiting"
            )
        return ("your_turn" if age < IDLE else "idle", facts.ended_at, False, words)
    quiet = facts.grew.elapsed(now) if facts.grew else None
    if quiet is None:
        if facts.grew and facts.grew.at_least(now) >= STALLED:
            return ("no_progress", facts.grew, False, facts.step or facts.words)
        return "waiting"
    if quiet >= STALLED:
        return ("no_progress", facts.grew, False, facts.step or facts.words)
    if facts.since:
        return ("working", facts.since, False, facts.step or facts.words)
    return ("working", facts.first, True, facts.step or facts.words)


def stopped(facts: Facts) -> str:
    if facts.ended == "stopped":
        return "Stopped by you"
    return facts.words if facts.ended == "failed" else ""


def status_of(found: tuple, now: float) -> dict:
    key, since, before, words = found
    elapsed = since.elapsed(now) if since else None
    return {
        "key": key,
        "elapsed": None if elapsed is None else round(elapsed, 1),
        "since": (since.shown or None) if since else None,
        "before": before,
        "words": (words or "")[:SHOWN],
    }


def where_failed(exc: BaseException) -> str:
    """An error, by its type and where it happened: never its message, which can hold what a
    session said."""
    frame = exc.__traceback__
    while frame and frame.tb_next:
        frame = frame.tb_next
    place = f" at {Path(frame.tb_frame.f_code.co_filename).name}:{frame.tb_lineno}" if frame else ""
    return f"{type(exc).__name__}{place}"


class Others:
    """The index (section 4.6): which of the owner's other sessions to follow, the passes that
    read their logs, and what the page is told."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.bus = runtime.bus
        self.run = secrets.token_hex(4)
        # Bumped when the setting turns off: what a pass of an earlier one found is dropped.
        self.generation = 0
        self.allowed = False  # The runtime runs (between start and stop).
        self.job: asyncio.Task | None = None
        self.woken: asyncio.Event | None = None
        self.stopping = threading.Event()
        self.reading: asyncio.Future | None = None  # The pass in the thread.
        self.passes = 0
        self.clear()

    def clear(self):
        """Forgets every session, and what they said with them."""
        self.listing: list[Any] | None = None
        self.listed_at = float("-inf")
        self.listed_open: frozenset[str] = frozenset()
        # Open sessions the last listing did not have, with their log's size then (see grown).
        self.unlisted: dict[str, int] = {}
        self.managed: set[str] = set()
        self.tracked_for: tuple[int, frozenset[str]] | None = None
        self.listings = 0
        self.connection = -1
        self.rows: dict[str, Row] = {}
        self.order: list[str] = []
        self.logs: dict[str, Log] = {}
        self.facts: dict[str, Facts] = {}
        self.sampled: dict[str, float] = {}  # When each log was last sampled (monotonic).
        self.sent: dict[str, tuple] = {}
        self.cursor = ""
        self.fresh = True

    # Switching

    def on(self) -> bool:
        return self.store.settings().other_sessions

    def start(self):
        self.allowed = True
        if self.on():
            self.launch()

    async def stop(self):
        self.allowed = False
        job, reading = self.job, self.reading
        self.halt()
        for task in (job, reading):
            if task:
                await asyncio.gather(task, return_exceptions=True)

    def switch(self, on: bool):
        """The setting changed. Off takes effect before this returns: nothing is published from
        now on, what was read goes, and the thread stops after the chunk it reads."""
        if on and self.allowed:
            self.launch()
        elif not on:
            self.halt()

    def launch(self):
        if self.job and not self.job.done():
            return
        self.stopping = threading.Event()
        self.woken = asyncio.Event()
        self.bus.on_subscribe = self.wake
        self.job = asyncio.create_task(self.loop(self.generation, self.stopping))

    def halt(self):
        self.generation += 1
        self.stopping.set()
        if self.bus.on_subscribe == self.wake:
            self.bus.on_subscribe = None
        if self.job:
            self.job.cancel()
        self.job, self.woken = None, None
        self.clear()

    def wake(self):
        if self.woken:
            self.woken.set()

    def eligible(self) -> bool:
        """Read only with a tab to tell, and Copilot connected (phase 1 has no alerts)."""
        return bool(self.bus.queues) and self.runtime.ready() and self.on()

    # The passes

    async def loop(self, generation: int, stopping: threading.Event):
        while True:
            try:
                if not self.eligible():
                    self.fresh = True  # Back to back again once it may read.
                    await self.idle()
                    continue
                await self.step(generation, stopping)
                if self.fresh:
                    await asyncio.sleep(0)
                else:
                    await asyncio.sleep(STEADY)
            except Exception as exc:
                logger.warning("Reading other Copilot sessions failed: %s", where_failed(exc))
                await asyncio.sleep(STEADY)

    async def idle(self):
        woken = self.woken or asyncio.Event()
        woken.clear()
        # Waiting for Copilot to connect is checked now and then; a tab connecting wakes it.
        timeout = STEADY if self.bus.queues and self.on() else None
        with suppress(TimeoutError):
            await asyncio.wait_for(woken.wait(), timeout)

    async def step(self, generation: int, stopping: threading.Event):
        """One pass: the listing when it is due, then reading, then telling the tabs."""
        if self.runtime.connections != self.connection:
            # Copilot (re)connected: its list again, and passes back to back.
            self.connection = self.runtime.connections
            self.listed_at, self.fresh = float("-inf"), True
        now = clock()
        open_now = frozenset(self.runtime.elsewhere)
        if now - self.listed_at >= LIST_EVERY or (
            now - self.listed_at >= LIST_AGAIN and (open_now != self.listed_open or self.grown())
        ):
            await self.list_sessions()
        if generation != self.generation:
            return
        self.continued_here()
        self.track()
        found = await self.read(generation, stopping)
        if found is None or generation != self.generation:
            return  # Off meanwhile: what it read goes.
        self.merge(found)
        self.publish(generation)

    def merge(self, found: Found):
        """What a pass found, now the event loop's: the next pass starts where it stopped."""
        self.cursor, facts, self.sampled, _ = found
        self.facts = {key: value for key, value in facts.items() if key in self.logs}
        self.passes += 1
        if self.fresh and all(f.reason != "reading" for f in self.facts.values()):
            self.fresh = False  # Every session has had its first look.

    async def read(self, generation: int, stopping: threading.Event):
        if self.reading and not self.reading.done():
            # An earlier generation's pass ends within a chunk: one pass at a time.
            await asyncio.gather(asyncio.shield(self.reading), return_exceptions=True)
        logs = [self.logs[key] for key in self.order if key in self.logs]

        def stopped() -> bool:
            return stopping.is_set() or self.generation != generation

        reading = asyncio.ensure_future(
            asyncio.to_thread(read_pass, logs, self.fresh, self.cursor, stopped)
        )
        self.reading = reading
        while not reading.done():
            await asyncio.wait({reading}, timeout=WATCH)
            if not reading.done():
                # A pass that takes long: what is now too old to show is withdrawn meanwhile.
                self.publish(generation)
        if generation != self.generation:
            return None
        return reading.result()

    async def list_sessions(self):
        """Copilot's list of sessions, and which of them are Chief of Staff's here (rule 1)."""
        backend = self.runtime.require_ready()
        self.listed_at, self.listed_open = clock(), frozenset(self.runtime.elsewhere)
        try:
            listing = list(await backend.list_sessions())
        except Exception as exc:
            logger.warning("Could not list Copilot's sessions: %s", where_failed(exc))
            return
        managed = {row["id"] for row in self.store.all("SELECT id FROM sessions")}
        for row in self.store.all(
            "SELECT key, value FROM kv WHERE key LIKE 'alias:%' OR key LIKE 'copilot:%'"
        ):
            managed.add(
                row["key"].removeprefix("alias:")
                if row["key"].startswith("alias:")
                else row["value"]
            )
        self.listing, self.managed = listing, managed
        self.listings += 1
        listed = {meta.session_id for meta in listing}
        self.unlisted = {key: self.size_of(key) for key in self.listed_open - listed}

    def continued_here(self):
        """Rule 1 from the next pass, not the next listing: a session continued here since the
        listing (Workers.adopt) is this Chief of Staff's, and leaves the rows."""
        if not self.rows:
            return
        ids = tuple(self.rows)  # At most MOST: within SQLite's limit on placeholders.
        marks = ",".join("?" * len(ids))
        found = self.store.all(f"SELECT id FROM sessions WHERE id IN ({marks})", ids)  # noqa: S608
        if found:
            self.managed |= {row["id"] for row in found}
            self.tracked_for = None  # Tracked again, without them.

    def grown(self) -> bool:
        """Whether an open session the last listing did not have wrote to its log since. Copilot
        lists a session from its first message on, seconds after its lock (often found first):
        it is listed again then, not a minute later."""
        return any(self.size_of(key) != size for key, size in self.unlisted.items())

    def size_of(self, session_id: str) -> int:
        """Its log's size (-1 without one), from a stat: nothing is read."""
        path = getattr(self.runtime.backend, "session_log", None)
        path = path(session_id) if path and SESSION_ID.fullmatch(session_id) else None
        try:
            return os.lstat(path).st_size if path else -1
        except OSError:
            return -1

    def track(self):
        """The rows to show and the logs to read, from the latest listing and what is open:
        the owner's open sessions first, then those closed in the last day; at most MOST."""
        open_now = self.runtime.elsewhere
        key = (self.listings, frozenset(open_now))
        if key == self.tracked_for or self.listing is None:
            return
        self.tracked_for = key
        at = wall()
        found = []
        for meta in self.listing:
            owner = whose(meta, self.managed)
            if owner in {"ours", "remote"}:
                continue
            modified = getattr(meta, "modified_time", None)
            changed = modified.timestamp() if isinstance(modified, datetime) else 0.0
            is_open = meta.session_id in open_now
            if is_open or at - changed <= CLOSED:
                found.append((is_open, changed, meta, owner))
        found.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        rows = {}
        for _, changed, meta, owner in found[:MOST]:
            row = self.rows.get(meta.session_id)
            rows[meta.session_id] = (
                row if row and row.meta is meta else Row(item_of(meta), owner, changed, meta)
            )
        self.rows = rows
        self.order = list(self.rows)
        backend = self.runtime.backend
        logs = {}
        for session_id, row in self.rows.items():
            if row.whose != "owner":
                continue
            if not (log := self.logs.get(session_id)):
                path = getattr(backend, "session_log", None)
                path = path(session_id) if path and SESSION_ID.fullmatch(session_id) else None
                log = Log(session_id, path)
            logs[session_id] = log
        self.logs = logs
        self.facts = {key: value for key, value in self.facts.items() if key in logs}

    # Telling the page

    def said(self, session_id: str, now: float) -> tuple:
        """What a row says now: where it stands (see where), whether it is open, its row."""
        row = self.rows[session_id]
        is_open = session_id in self.runtime.elsewhere
        sampled = self.sampled.get(session_id, 0.0)
        return where(self.facts.get(session_id), sampled, row.whose, is_open, now), is_open, row

    def row(self, said: tuple, now: float, lease: bool) -> dict:
        """A row as the page gets it."""
        found, is_open, row = said
        shown = {**row.item, "open": is_open}
        if isinstance(found, str):
            shown |= {"status": None, "reason": found, "why": WHY.get(found, "")}
        else:
            shown |= {"status": status_of(found, now), "reason": "", "why": ""}
        if lease:
            shown["valid_for"] = self.valid_for(row.item["id"], now) if shown["status"] else None
        return shown

    def valid_for(self, session_id: str, now: float) -> float:
        return round(max(0.0, FRESH - (now - self.sampled.get(session_id, 0.0))), 1)

    def publish(self, generation: int):
        """One `others` event: the rows that changed, and a lease for every status."""
        if generation != self.generation or not self.bus.queues or not self.on():
            return
        now = clock()
        rows, leases = [], {}
        for session_id in self.order:
            said = self.said(session_id, now)
            if self.sent.get(session_id) != said:
                self.sent[session_id] = said
                rows.append(self.row(said, now, lease=False))
            if not isinstance(said[0], str):
                leases[session_id] = self.valid_for(session_id, now)
        gone = [session_id for session_id in self.sent if session_id not in self.rows]
        for session_id in gone:
            del self.sent[session_id]
        if rows or gone or leases:
            self.bus.publish(
                "others",
                {
                    "run": self.run,
                    "pass": self.passes,
                    "rows": rows,
                    "gone": gone,
                    "leases": leases,
                },
            )

    def view(self) -> dict:
        """GET /api/others."""
        if not self.on():
            return {"run": self.run, "pass": self.passes, "why": "", "rows": []}
        self.wake()
        now = clock()
        unlisted = bool(self.rows) and all(row.whose == "unlisted" for row in self.rows.values())
        return {
            "run": self.run,
            "pass": self.passes,
            "why": WHY["unlisted"] if unlisted else "",
            "rows": [self.row(self.said(key, now), now, lease=True) for key in self.order],
        }

    async def find(self, session_id: str) -> dict:
        """GET /api/others/{id}: one session of the latest listing (read now if there is none
        yet), under section 4.2's rules."""
        if not self.on():
            raise LookupError("Session not found.")
        if session_id in self.rows:
            now = clock()
            return self.row(self.said(session_id, now), now, lease=True)
        if self.listing is None:
            await self.list_sessions()
        meta = next((m for m in self.listing or [] if m.session_id == session_id), None)
        owner = whose(meta, self.managed) if meta else "ours"
        if meta is None or owner in {"ours", "remote"}:
            raise LookupError("Session not found.")
        found = "closed" if owner == "owner" else owner
        is_open = session_id in self.runtime.elsewhere
        if owner == "owner" and is_open:
            found = "reading"  # Followed from the next listing.
        return {
            **item_of(meta),
            "open": is_open,
            "status": None,
            "reason": found,
            "why": WHY.get(found, ""),
            "valid_for": None,
        }
