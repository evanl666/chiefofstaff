"""Whose a Copilot session is, decided in one place: the sidebar's Other Copilot sessions
(others.py), the sessions the Router and the sidebar list (Workers.external), learning how the
owner writes (habits.py) and `chiefofstaff tidy-sessions` (tidy.py) all ask here.

Copilot keeps with each session the app that made it and the one that last ran a turn in it (see
backend.made_by_us and backend.made_by_a_program). Its CLI calls itself github/cli whether the
owner typed in it or a script ran `copilot -p` in a loop, so a session the CLI made is the
owner's unless its own log shows a single shot (`one_shot`: one message of its own, never
resumed, ended) and one of:

- worker: it began while one of Chief of Staff's sessions ran a command (the commands table),
  and something ties it to that command: it ran in that worker's own folder, the command names
  the `copilot` command, or it is one of a batch (below) that began soon after the command did.
  The time alone is not enough: a dev server a worker started runs for hours, and a
  `copilot -p` the owner runs meanwhile is theirs.
- batch: at least BATCH_SIZE one-shot sessions whose first messages start alike (their first
  PREFIX characters, whitespace collapsed, case folded), each begun within GAP of the one
  before: a script's, whoever ran it; or any number named as a batch's (`copilot -p --name
  "batch: <task>"`, as workers are told to: see named). A single `copilot -p` the owner runs now
  and then stays theirs.

A session open now is never decided on (it may be anything until it ends), except that one
named a batch's is one while it runs too (no row for each call of a worker's batch); nor is one
with more than one message of its own or resumed: an interactive session the owner asked one
question in and exited looks like a single shot, and stays theirs unless a worker or a batch
made it. What is
decided is kept (kv `made:<session id>`), so it does not change when a batch's other sessions
go (tidied away) or as commands are read again; a session that changed since (continued in a
terminal) is looked at again.

Learning takes the owner's words only from sessions they talked in (talks: a second message of
their own, or resumed), never from a single shot, whoever made it.

Logs are read in a thread, each once while it does not change: at most SHAPE_MOST bytes of it
(a longer log is no single shot; for talks, only its start is read) and ROUND_BYTES a round, the
newest first (the rest in the next round). Nothing a session said is kept: of its first message
only a hash of its start (or of its name), to find its batch.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import stat
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .backend import OWN_CLIENTS, made_by_a_program, made_by_us

if TYPE_CHECKING:
    from .store import Store

logger = logging.getLogger(__name__)

# Whose a listed session is (others.py, section 4.2 of docs/design/adopt-sessions.md).
OURS = "ours"  # Chief of Staff's own, here or under another data directory
REMOTE = "remote"
PROGRAM = "program"  # a program's, through Copilot's SDK, naming no app
MADE_BY_WORKER = "worker"  # a Chief of Staff worker's `copilot -p`
BATCH = "batch"  # one of a script's many one-shot sessions
UNLISTED = "unlisted"  # the listing does not say which app made it
UNKNOWN = "unknown"  # no app named
ANOTHER = "another"  # last run by a Chief of Staff
OWNER = "owner"
# Not listed at all, neither in the sidebar nor to the Router.
LEFT_OUT = frozenset({OURS, REMOTE, PROGRAM, MADE_BY_WORKER, BATCH})
# Not the owner's words: what was read from them and not learned from yet is dropped.
NOT_THEIRS = frozenset({PROGRAM, MADE_BY_WORKER, BATCH})

# A batch: at least BATCH_SIZE one-shot sessions whose first PREFIX characters are the same,
# each begun within GAP seconds of the one before.
BATCH_SIZE = 3
PREFIX = 64
GAP = 30 * 60.0
# A batch is a worker's when it began within LEAD seconds of the command it ran in.
LEAD = 10 * 60.0
# A log longer than this is no single shot, and is not read.
SHAPE_MOST = 4 << 20
# What one round reads at most; the rest is read in the next one.
ROUND_BYTES = 128 << 20
# A command that runs the Copilot CLI (not ~/.copilot, nor github-copilot or copilot_tools).
RUNS_COPILOT = re.compile(r"(?<![\w.-])copilot(?![\w-])")
PREFIX_TYPE = b'{"type":"'
KV = "made:"
# How a session is named as one of a batch (`copilot -p --name "batch: <task>"`).
NAMED = "batch:"


def managed(store: Store) -> set[str]:
    """Chief of Staff's own sessions here (rule 1): its rows, the Copilot sessions it moved the
    Router off (alias:) and those its rows run on now (copilot:)."""
    found = {row["id"] for row in store.all("SELECT id FROM sessions")}
    for row in store.all(
        "SELECT key, value FROM kv WHERE key LIKE 'alias:%' OR key LIKE 'copilot:%'"
    ):
        key = row["key"]
        found.add(key.removeprefix("alias:") if key.startswith("alias:") else row["value"])
    return found


def whose(
    meta: Any,
    held: Collection[str],
    made: Mapping[str, str] | None = None,
    is_open: bool = False,
) -> str:
    """Section 4.2: whose a listed session is, the first rule that matches. ours, remote, program,
    worker and batch are left out (LEFT_OUT); unlisted (the raw answer was not read), unknown (no
    maker named) and another (made by another client and last run by a Chief of Staff) are listed
    without a status; owner: the owner's own, with one. `made`: what Provenance.made found of
    the sessions that would otherwise be the owner's, closed. Open now (`is_open`), one may be
    anything until it ends: only its name says it is a batch's (see named)."""
    if meta.session_id in held or getattr(meta, "made_by_us", False):
        return OURS
    if getattr(meta, "is_remote", False):
        return REMOTE
    if getattr(meta, "made_by_program", False):
        return PROGRAM
    if not getattr(meta, "clients_listed", False):
        return UNLISTED
    maker = getattr(meta, "client_name", "") or ""
    last = getattr(meta, "last_client_name", "") or ""
    if not maker:
        return UNKNOWN
    if made_by_us({"clientName": maker, "lastClientName": last}):
        return OURS
    if made_by_a_program({"clientName": maker, "lastClientName": last}):
        return PROGRAM
    if last in OWN_CLIENTS:
        return ANOTHER
    if is_open:
        return BATCH if named(meta) else OWNER
    return (made or {}).get(meta.session_id, OWNER)


# What a log says


@dataclass(frozen=True, slots=True)
class Shape:
    """What a session's log says of how it ran: `one_shot` (one message of its own, never
    resumed, ended), when it began (`at`, its session.start's time), its first message's start,
    hashed (`group`, see group_of); `talked`: someone talked in it (a second message of its own,
    or it was resumed); `read`: what it says is known (False: not read, or it holds what this
    cannot follow), so it is neither one shot nor known not to be talked in."""

    one_shot: bool
    at: float = 0.0
    group: str = ""
    talked: bool = False
    read: bool = True


# Read whole: one message at most, not one shot (still written, killed, no message at all).
NOT_ONE = Shape(False)
TALKED = Shape(False, talked=True)
UNREAD = Shape(False, read=False)


def group_of(text: str) -> str:
    """A first message's start, as batches are found by: whitespace collapsed, case folded, its
    first PREFIX characters, hashed (what a session said is never kept)."""
    start = " ".join(text.split()).casefold()[:PREFIX]
    return hashlib.sha256(start.encode("utf-8", "replace")).hexdigest()[:16]


def epoch(value: Any) -> float | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except (ValueError, OverflowError):
        return None
    return moment.timestamp() if moment.tzinfo else None


def parse(line: bytes) -> dict | None:
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return event if isinstance(event, dict) else None


def own(event: dict, data: dict) -> bool:
    """An event of the session itself, not of a sub-agent (as workers.own_event)."""
    return not event.get("agentId") and not data.get("parentToolCallId")


def shape(log: bytes, whole: bool = True) -> Shape:
    """What a log says (see Shape): all of it, or only its start (`whole` False: only whether
    someone talked in it). A log in another format, or with a line it cannot read, is UNREAD; a
    whole one whose last line is still being written is no single shot."""
    lines = log.split(b"\n")
    written = not lines[-1]  # Complete lines only (a log's start ends in the middle of one).
    first = parse(lines[0]) or {}
    head = first.get("data")
    if not (
        first.get("type") == "session.start"
        and isinstance(head, dict)
        and head.get("producer") == "copilot-agent"
        and type(head.get("version")) is int
        and head["version"] == 1
    ):
        return UNREAD
    at = epoch(first.get("timestamp")) or epoch(head.get("startTime"))
    said: str | None = None
    ended = False
    for line in lines[1:-1]:
        if line.startswith(PREFIX_TYPE):
            kind = line[len(PREFIX_TYPE) : line.find(b'"', len(PREFIX_TYPE))]
            event = None
        else:
            event = parse(line)
            if event is None or not isinstance(event.get("type"), str):
                return UNREAD  # A line it cannot read: what it holds is not known.
            kind = event["type"].encode()
        if kind == b"user.message":
            event = event or parse(line)
            data = (event or {}).get("data")
            if not isinstance(data, dict):
                return UNREAD
            if not own(event or {}, data):
                continue
            if said is not None:
                return TALKED  # A second message: a conversation.
            content = data.get("content")
            said = content if isinstance(content, str) else ""
        elif kind in {b"session.resume", b"session.snapshot_rewind"}:
            return TALKED
        elif kind == b"session.shutdown":
            ended = True
    if not whole:
        return UNREAD  # Nothing said yet of what comes after.
    if said is None or not ended or not written or not at:
        return NOT_ONE
    return Shape(True, at, group_of(said))


def read_log(path: Path, most: int, start: bool = False) -> bytes | None:
    """A whole log of at most `most` bytes (`start`: the first `most` bytes of a longer one),
    opened without following a link and without waiting (a pipe put there); None for anything
    else."""
    try:
        folder = os.lstat(path.parent)
    except OSError:
        return None
    if not stat.S_ISDIR(folder.st_mode):
        return None
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (info.st_size > most and not start):
            return None
        chunks, left = [], min(info.st_size, most)
        while left > 0:
            data = os.read(fd, min(left, 1 << 20))
            if not data:
                break
            chunks.append(data)
            left -= len(data)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def identity(path: Path) -> tuple[int, int, int, int] | None:
    """A log as it is now: (device, inode, size, change time); None without one."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


# Who made a session that ran once


@dataclass(frozen=True, slots=True)
class OneShot:
    session_id: str
    at: float  # when it began
    group: str  # its first message's start (see group_of), or its name (see named)
    cwd: str = ""  # the folder it ran in, as the listing has it
    named: bool = False  # named a batch's (see named)


def named(meta: Any) -> str:
    """The name a session was given as one of a batch (`copilot -p --name "batch: <task>"`,
    as workers are told to): a batch's whatever their number; "" for any other."""
    name = " ".join(str(getattr(meta, "name", "") or "").split())
    return name if name.casefold().startswith(NAMED) else ""


@dataclass(frozen=True, slots=True)
class Window:
    """A command one of Chief of Staff's sessions ran, from its start to its end (now while it
    runs)."""

    by: str  # the session that ran it
    start: float
    end: float
    copilot: bool  # it names the copilot command
    folder: str = ""  # that session's own folder (its real path), "" when it has none


def inside(path: str, folder: str) -> bool:
    if not path or not folder:
        return False
    try:
        return Path(os.path.realpath(path)).is_relative_to(folder)
    except (OSError, ValueError):
        return False


def batches(shots: Iterable[OneShot]) -> dict[str, list[OneShot]]:
    """The one-shot sessions that are one of a batch, each with its batch (in the order they
    began)."""
    alike: dict[str, list[OneShot]] = {}
    for shot in shots:
        alike.setdefault(shot.group, []).append(shot)
    found: dict[str, list[OneShot]] = {}
    for members in alike.values():
        members.sort(key=lambda shot: (shot.at, shot.session_id))
        run: list[OneShot] = []
        for shot in [*members, None]:
            if shot is None or (run and shot.at - run[-1].at > GAP):
                if len(run) >= BATCH_SIZE or any(member.named for member in run):
                    found |= dict.fromkeys((member.session_id for member in run), run)
                run = []
            if shot is not None:
                run.append(shot)
    return found


def decide(
    fresh: Iterable[OneShot], known: Iterable[OneShot], windows: list[Window]
) -> dict[str, dict]:
    """Who made each fresh one-shot session (see the module's docstring): its record, as kept
    (`as`: worker or batch; `by`: the worker's session; `why`: folder, command, batch or named;
    `named`: named as a batch's). Those not in it are the owner's. `known`: those decided
    before, which batches may continue."""
    fresh = list(fresh)
    batch = batches([*fresh, *known])
    decided = {}
    for shot in fresh:
        during = [w for w in windows if w.start <= shot.at <= w.end]
        tied = [(w, "folder") for w in during if w.folder and inside(shot.cwd, w.folder)]
        tied += [(w, "command") for w in during if w.copilot]
        run = batch.get(shot.session_id)
        kind = "named" if shot.named else "batch"
        if not tied and run:
            # A batch that began soon after a command it runs in did.
            for w in during:
                began = min(m.at for m in run if w.start <= m.at <= w.end)
                if began - w.start <= LEAD:
                    tied.append((w, kind))
        record = {"group": shot.group, "at": shot.at} | ({"named": True} if shot.named else {})
        if tied:
            order = {"folder": 2, "command": 1, "batch": 0, "named": 0}
            window, why = max(tied, key=lambda pair: (order[pair[1]], pair[0].start))
            decided[shot.session_id] = {"as": MADE_BY_WORKER, "by": window.by, "why": why, **record}
        elif run:
            decided[shot.session_id] = {"as": BATCH, "by": "", "why": kind, **record}
    return decided


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def windows(
    store: Store, low: float, high: float, folder_of: Callable[[dict], Path | None]
) -> list[Window]:
    """The commands Chief of Staff's sessions ran that were running at some time from `low` to
    `high`."""
    rows = store.all(
        "SELECT session_id, command, started_at, ended_at FROM commands "
        "WHERE started_at <= ? AND (ended_at IS NULL OR ended_at >= ?)",
        (iso(high + 60), iso(low - 60)),
    )
    folders: dict[str, str] = {}
    found = []
    now = time.time()
    for row in rows:
        start = epoch(row["started_at"])
        end = epoch(row["ended_at"]) if row["ended_at"] else now
        if start is None or end is None:
            continue
        by = row["session_id"]
        if by not in folders:
            session = store.find_session(by)
            folder = folder_of(session) if session else None
            folders[by] = os.path.realpath(folder) if folder else ""
        copilot = bool(RUNS_COPILOT.search(row["command"] or ""))
        found.append(Window(by, start, end, copilot, folders[by]))
    return found


# What a Chief of Staff (or tidy-sessions) knows


def known(store: Store) -> dict[str, dict]:
    """What was decided before: session id -> its record."""
    found = {}
    for key, value in store.items(KV).items():
        try:
            record = json.loads(value)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("as") in {MADE_BY_WORKER, BATCH}:
            found[key] = record
    return found


@dataclass
class Look:
    """What a round of reading found: the size of each log decided before (None: gone or not a
    log), and the shape of each other one read (those left for the next round have none)."""

    sizes: dict[str, int | None]
    shapes: dict[str, Shape]


class Provenance:
    """Which of the sessions that would be the owner's a worker or a batch made (see the
    module's docstring), for the index, Workers.external, habits and tidy-sessions alike."""

    def __init__(
        self,
        store: Store,
        log_of: Callable[[str], Path | None],
        folder_of: Callable[[dict], Path | None],
        keep: bool = True,
    ):
        self.store = store
        self.log_of = log_of
        self.folder_of = folder_of
        self.keep = keep  # Records what it decides (tidy-sessions' preview changes nothing).
        self.lock = asyncio.Lock()
        # Each log read: its identity then, and its shape.
        self.shapes: dict[str, tuple[tuple, Shape]] = {}
        self.decided: dict[str, dict] = {}  # What it decided (when not kept).
        # One-shot sessions found the owner's, as they were then.
        self.settled: dict[str, OneShot] = {}

    async def made(self, listing: Iterable[Any], open_now: Collection[str]) -> dict[str, str]:
        """Of the listed sessions that would be the owner's and are not open now, those a
        worker or a batch made: session id -> worker or batch. When that cannot be told, none:
        they are listed as before."""
        try:
            return await self.decide(list(listing), open_now)
        except Exception as exc:
            logger.warning(
                "Could not tell which Copilot sessions a worker or a script made: %s",
                type(exc).__name__,
            )
            return {}

    async def decide(self, listing: list[Any], open_now: Collection[str]) -> dict[str, str]:
        held = managed(self.store)
        candidates = [
            meta
            for meta in listing
            if meta.session_id not in open_now and whose(meta, held) == OWNER
        ]
        if not candidates:
            return {}
        # The newest first: what is shown first is decided first.
        candidates.sort(key=lambda meta: stamp(getattr(meta, "modified_time", None)), reverse=True)
        async with self.lock:
            records = known(self.store) | self.decided
            paths = {meta.session_id: self.log_of(meta.session_id) for meta in candidates}
            look = await asyncio.to_thread(self.look, paths, records)
            # Changed since (continued in a terminal, say): looked at again.
            gone = [
                key
                for key, size in look.sizes.items()
                if size is not None and size != records[key].get("size")
            ]
            for key in gone:
                records.pop(key)
            shots = [
                OneShot(
                    meta.session_id,
                    found.at,
                    group_of(f"\0name {name}") if (name := named(meta)) else found.group,
                    cwd_of(meta),
                    bool(name),
                )
                for meta in candidates
                if (found := look.shapes.get(meta.session_id)) and found.one_shot
            ]
            # (A named one's group is its name's: a new one of it is named itself.)
            earlier = [
                OneShot(key, float(record.get("at") or 0), str(record.get("group") or ""))
                for key, record in records.items()
            ]
            # One found the owner's stays theirs unless a batch it is one of comes along (the
            # commands it began during are known by the time it ended).
            batched = batches([*shots, *earlier])
            due = [
                shot
                for shot in shots
                if shot.session_id in batched or self.settled.get(shot.session_id) != shot
            ]
            found = {}
            if due:
                spans = [shot.at for shot in due]
                commands = windows(self.store, min(spans), max(spans), self.folder_of)
                found = decide(due, earlier, commands)
            for shot in due:
                if shot.session_id in found:
                    found[shot.session_id]["size"] = self.shapes[shot.session_id][0][2]
                    self.settled.pop(shot.session_id, None)
                else:
                    self.settled[shot.session_id] = shot
            self.record(found, gone)
            records |= found
        listed = {meta.session_id for meta in candidates}
        return {key: record["as"] for key, record in records.items() if key in listed}

    def look(self, paths: Mapping[str, Path | None], records: Mapping[str, dict]) -> Look:
        """In a thread: the size of each log decided before, and the shape of the others, read
        within the round's bytes."""
        sizes: dict[str, int | None] = {}
        shapes: dict[str, Shape] = {}
        left = ROUND_BYTES
        for key, path in paths.items():
            now = identity(path) if path else None
            if key in records:
                sizes[key] = now[2] if now else None
                if now is None or now[2] == records[key].get("size"):
                    continue
            if now is None:
                continue
            seen = self.shapes.get(key)
            if seen and seen[0] == now:
                shapes[key] = seen[1]
                continue
            if now[2] > SHAPE_MOST:
                found = UNREAD
            elif now[2] > left:
                continue  # In the next round.
            else:
                log = read_log(path, SHAPE_MOST) if path else None
                left -= now[2]
                if log is None or identity(path) != now:  # type: ignore[arg-type]
                    continue  # Changed while read: read again next round.
                found = shape(log)
            self.shapes[key] = (now, found)
            shapes[key] = found
        return Look(sizes, shapes)

    async def talks(self, session_ids: Iterable[str]) -> dict[str, bool]:
        """Whether someone talked in each of these sessions (two messages of its own, or it was
        resumed: True) or not (one message at most: False), as far as their logs say; those
        whose logs say neither (not read, too long without a second message at their start, in
        a format it cannot follow) are left out. Open ones too: learning asks."""
        paths = {key: self.log_of(key) for key in session_ids}
        async with self.lock:
            return await asyncio.to_thread(self.read_talks, paths)

    def read_talks(self, paths: Mapping[str, Path | None]) -> dict[str, bool]:
        found = {}
        for key, path in paths.items():
            now = identity(path) if path else None
            if now is None or path is None:
                continue
            seen = self.shapes.get(key)
            if not (seen and seen[0] == now and seen[1].read):
                # A long one: only its start, where a second message usually is.
                log = read_log(path, SHAPE_MOST, start=True)
                if log is None or identity(path) != now:
                    continue
                seen = (now, shape(log, whole=now[2] <= SHAPE_MOST))
                self.shapes[key] = seen
            if seen[1].read:
                found[key] = seen[1].talked
        return found

    def record(self, found: dict[str, dict], gone: list[str]):
        if not self.keep:
            for key in gone:
                self.decided.pop(key, None)
            self.decided |= found
            return
        self.store.set_many(
            [(KV + key, json.dumps(record, sort_keys=True)) for key, record in found.items()],
            [KV + key for key in gone],
        )
        if found:
            logger.info(
                "%d Copilot sessions a worker or a script made are left out of your sessions.",
                len(found),
            )


def stamp(moment: Any) -> float:
    return moment.timestamp() if isinstance(moment, datetime) else 0.0


def cwd_of(meta: Any) -> str:
    context = getattr(meta, "context", None)
    return (getattr(context, "working_directory", "") or "") if context else ""


def log_of(backend: Any, session_id: str) -> Path | None:
    """Where a backend keeps a session's log (None: it keeps none)."""
    where = getattr(backend, "session_log", None)
    return where(session_id) if where else None
