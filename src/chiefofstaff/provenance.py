"""Whose a Copilot session is, decided in one place: the sidebar's Other Copilot sessions
(others.py), the sessions the Router and the sidebar list (Workers.external), learning how the
owner writes (habits.py) and `chiefofstaff tidy-sessions` (tidy.py) all ask here.

Copilot keeps with each session the app that made it and the one that last ran a turn in it (see
backend.made_by_us and backend.made_by_a_program). Its CLI calls itself github/cli whether the
owner typed in it or a script ran `copilot -p` in a loop, and nothing in a session's files tells
the two apart for sure (CLI 1.0.110 to 1.0.112: the same events and fields; what differs, a
title the CLI makes up, its memories, how its model was settled, comes and goes with the
version and the account). So a session the CLI made is the owner's unless its own log shows a
single shot (`one_shot`: one message of its own, never resumed, ended) and one of:

- worker: it began while one of Chief of Staff's sessions ran a command (the commands table: to
  its end, when seen; else COMMAND_MOST, and on while that worker's sessions keep coming;
  never past the end of the run of Chief of Staff it began in: see window_end), and it ran in
  that worker's own folder; or, where the command ran (its folder, one it went to with `cd`)
  or in a temporary folder, the command runs `copilot -p` (see runs_copilot), or it is one of
  a batch that began soon after the command did (named a batch's, or ten or more alike close
  together; in a temporary folder or the worker's own, ten or more alike each within
  WORKER_GAP of the one before: a worker's calls may take minutes). The time and the folder
  alone are not enough: a worker may run a dev server for hours, in the owner's own project (a
  shared checkout, a session adopted from a terminal), and what the owner asks there meanwhile
  is theirs. What an earlier version found a worker made stays so unless the session's own
  log says it is the owner's (see below) or its folder rules the worker's commands out (see
  ruled_out): never for want of evidence.
- batch: at least HIDE_SIZE one-shot sessions whose first messages start alike (their first
  PREFIX characters, whitespace collapsed, case folded), each begun within GAP of the one
  before: a script's, whoever ran it; or any number named as a batch's (`copilot -p --name
  "batch: <task>"`, as workers are told to: see named). From BATCH_SIZE alike (a batch for a
  worker's command above) to fewer than HIDE_SIZE, tied to no worker, they are `alike`: the
  owner's still, listed; only tidy-sessions names them. The owner asks alike questions too
  (a review of each pull request, say), a few a day: never a batch.

A session open now is never decided on (it may be anything until it ends), except that one
named a batch's is one while it runs too (no row for each call of a worker's batch); nor is one
with more than one message of its own or resumed: an interactive session the owner asked one
question in and exited looks like a single shot, and stays theirs unless a worker or a batch
made it. What is decided is kept (kv `made:<session id>`, with the RULES it was decided by), so
it does not change when a batch's other sessions go (tidied away) or as commands are read
again; a session that changed since (continued in a terminal), or decided by other rules (an
earlier version's), is looked at again (until then, as it was decided).

Learning takes the owner's words only from sessions they talked in (talks: a second message of
their own, or resumed), never from a single shot, whoever made it. A message the CLI writes
itself (its memories) is no one's (see said_by_them).

Logs are read in a thread, each once while it does not change: at most SHAPE_MOST bytes of it
(a longer log is no single shot; for talks, only its start is read) and ROUND_BYTES a round, the
newest first (the rest in the next round: `pending`). Nothing a session said is kept: of its
first message only a hash of its start (or of its name), to find its batch.
"""

from __future__ import annotations

import asyncio
import bisect
import hashlib
import json
import logging
import math
import os
import re
import stat
import tempfile
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
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
# A few one-shot sessions alike, tied to no worker: the owner's, listed (tidy-sessions names
# them). Never kept, never left out.
ALIKE = "alike"

# Alike: at least BATCH_SIZE one-shot sessions whose first PREFIX characters are the same, each
# begun within GAP seconds of the one before; a batch (left out) from HIDE_SIZE on. A script
# runs its calls one after the other, seconds apart, by the dozen; the owner asks alike
# questions minutes apart, a few at a time.
BATCH_SIZE = 5
HIDE_SIZE = 10
PREFIX = 64
GAP = 120.0
# A batch is a worker's when it began within LEAD seconds of the command it ran in: alike, ten
# or more, each within WORKER_GAP of the one before (a worker's calls may take minutes each:
# agentic prompts, a backoff), in a temporary folder or the worker's own.
LEAD = 10 * 60.0
WORKER_GAP = 30 * 60.0
# A command runs from its start to its end, when Chief of Staff saw it end; one whose end it did
# not see (still running, left running in the background, or Chief of Staff stopped meanwhile)
# for COMMAND_MOST after it began, and past that only while its worker's sessions keep coming
# (each within WORKER_GAP of one before). Never past the end of the run of Chief of Staff it
# began in: Chief of Staff keeps its runs (kv "uptime": [start, last known running] each), the
# latter touched every ALIVE_EVERY seconds: a command may have begun up to ALIVE_SLACK after it.
COMMAND_MOST = 2 * 3600.0
ALIVE_EVERY = 60.0
ALIVE_SLACK = ALIVE_EVERY + 30.0
RUNS_KEPT = 500
# A log longer than this is no single shot, and is not read.
SHAPE_MOST = 4 << 20
# What one round reads at most; the rest is read in the next one.
ROUND_BYTES = 128 << 20
PREFIX_TYPE = b'{"type":"'
KV = "made:"
# The rules a kept decision was made by: one made by others (0.1.43's, without) is made again.
RULES = 2
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


def said_by_them(event: dict, data: dict) -> bool:
    """A user.message someone typed or sent (the owner, a script): not a sub-agent's, nor one
    the CLI writes itself, with a source and no words (its memories, since 1.0.105)."""
    content = data.get("content")
    return (
        own(event, data)
        and not data.get("source")
        and isinstance(content, str)
        and bool(content.strip())
    )


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
            if not said_by_them(event or {}, data):
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
    """A command one of Chief of Staff's sessions ran, from its start to its end (see
    window_end)."""

    by: str  # the session that ran it
    start: float
    end: float
    copilot: bool  # it runs `copilot -p` (see runs_copilot)
    folder: str = ""  # that session's own folder (its real path), "" when it has none
    places: tuple[str, ...] = ()  # where it ran (real paths): that session's folder, a cd's
    limit: float = 0.0  # its end not seen: how long it may have run at most (see window_end)

    def covers(self, at: float, came: Sequence[float]) -> bool:
        """Whether it may have been running when a session began: up to its end; past an end
        it was not seen to reach (up to its limit) while its worker's sessions keep coming,
        each within WORKER_GAP of one before (`came`: when each began, in order)."""
        if not self.start <= at <= max(self.end, self.limit):
            return False
        if at <= self.end:
            return True
        n = bisect.bisect_left(came, at)
        return bool(n) and at - came[n - 1] <= WORKER_GAP  # (One before it began: hours before.)


def inside(path: str, folder: str) -> bool:
    if not path or not folder:
        return False
    try:
        return Path(os.path.realpath(path)).is_relative_to(folder)
    except (OSError, ValueError):
        return False


def temporary_folders() -> list[str]:
    """Where a script puts what it throws away (its real paths): /tmp, and the system's own
    temporary folder ($TMPDIR)."""
    found = {"/tmp", "/private/tmp", os.environ.get("TMPDIR", ""), tempfile.gettempdir()}  # noqa: S108
    return sorted({os.path.realpath(folder) for folder in found if folder})


# The Copilot CLI found by its path: `$(which copilot)`, `` `command -v copilot` ``.
WHICH_COPILOT = re.compile(
    r"\$\(\s*(?:which|command\s+-v|type\s+-p)\s+copilot\s*\)"
    r"|`\s*(?:which|command\s+-v|type\s+-p)\s+copilot\s*`"
)
# A command's parts that run one after the other or apart (a loop's body, a pipe's end, $(..),
# what `find -exec` runs). Commands are kept to their first 2000 characters (events.py).
COMMAND_PARTS = re.compile(r"&&|\|\||\$\(|[;&|(){}`\n]|\s-(?:exec|execdir|ok)\s")
# Its words: one quoted is one (spaces and all), as is one with `\ `; a list's brackets and
# commas aside (`["copilot", "-p", prompt]`).
COMMAND_WORDS = re.compile(r""""([^"]*)"|'([^']*)'|((?:\\.|[^\s'"\[\],])+)""")
# What may come before the command a part runs: a loop's keyword, a variable set for it, a
# program that runs it, and their options and an option's value (`sudo -u me`, `xargs -I {}`,
# `timeout 60s`, `npm exec`).
BEFORE_COMMAND = frozenset(
    {"do", "then", "else", "!", "{}", "sudo", "env", "exec", "nohup", "time", "command", "nice",
     "timeout", "xargs", "parallel", "npx", "npm", "pnpm", "yarn", "dlx", "bunx", "bash", "sh",
     "zsh"}
)  # fmt: skip
SETTING_OR_DURATION = re.compile(r"[A-Za-z_]\w*=|\d+(?:\.\d+)?[smhd]?$")
PROMPT = frozenset({"-p", "--prompt"})


def program(word: str) -> str:
    """The program a word names: its last part (of a path with / or \\), without a package's
    @version (`@github/copilot@latest`) or Windows' .exe or .cmd."""
    name = re.split(r"[\\/]", word.strip())[-1]
    name = name.split("@", 1)[0]
    return name[:-4] if name.lower().endswith((".exe", ".cmd")) else name


def words_of(part: str) -> list[str]:
    """A command part's words (see COMMAND_WORDS)."""
    found = []
    for match in COMMAND_WORDS.finditer(part):
        double, single, bare = match.groups()
        found.append(next(word for word in (double, single, bare) if word is not None))
    return found


def runs_copilot(command: str) -> bool:
    """Whether a command runs `copilot -p` (or --prompt): the Copilot CLI as the command of one
    of its parts (`copilot`, a path to it, `npx @github/copilot`, `$(which copilot)`, what
    `bash -c` runs), a prompt given to it. Not the word in a path (`cd ~/src/copilot`,
    `git -C copilot log -p`) or as a plain word (`echo copilot -p`), nor the CLI without a
    prompt (an interactive one, the owner's way)."""
    for part in COMMAND_PARTS.split(WHICH_COPILOT.sub(" copilot ", command)):
        words = words_of(part)
        value = False  # The word before was an option of a program before the command.
        for n, word in enumerate(words):
            if program(word) == "copilot":
                rest = words[n + 1 :]
                if any(w in PROMPT or w.startswith("--prompt=") for w in rest):
                    return True
                break
            if " " in word.strip() and runs_copilot(word):
                return True  # What `bash -c "..."` runs.
            if word in BEFORE_COMMAND or SETTING_OR_DURATION.match(word):
                value = False
            elif word.startswith("-"):
                value = "=" not in word
            elif value:
                value = False  # That option's value.
            else:
                break  # Another command: copilot after it is a word of its.
    return False


# `cd <folder>` (or pushd) in a command, where what follows it runs.
CHANGES_FOLDER = re.compile(
    r"""(?:^|&&|\|\||[;&|(){}\n"'`]|\b(?:do|then|else)\b)\s*(?:cd|pushd)\s+(?:-[LPe@]+\s+)*"""
    r"""(?:--\s+)?("[^"]*"|'[^']*'|(?:\\.|[^\s;&|(){}'"])+)"""
)


def places(command: str, cwd: str) -> tuple[str, ...]:
    """Where a command ran (real paths): the folder its session runs commands in (`cwd`), and
    each it went to with `cd` or pushd (~ and $HOME read; one it cannot tell, left out)."""
    found = [os.path.realpath(cwd)] if cwd else []
    here = cwd
    home = str(Path.home())
    for match in CHANGES_FOLDER.finditer(command):
        target = match.group(1)
        quoted = target[:1] in {'"', "'"}
        target = target[1:-1] if quoted else target.replace("\\ ", " ")
        for name in ("${HOME}", "$HOME"):
            if target == name or target.startswith(name + "/"):
                target = home + target[len(name) :]
        if target.startswith("~"):
            target = str(Path(target).expanduser())
        if not target or target == "-" or "$" in target or "`" in target:
            continue
        if not Path(target).is_absolute():
            if not here:
                continue
            target = str(Path(here) / target)
        here = target
        found.append(os.path.realpath(target))
    return tuple(dict.fromkeys(found))


def batches(
    shots: Iterable[OneShot], gap: float = GAP, least: int = BATCH_SIZE
) -> dict[str, list[OneShot]]:
    """The one-shot sessions alike (see the module's docstring), each with the others it is
    alike with (in the order they began): at least `least`, each within `gap` of the one
    before, or any number named a batch's."""
    alike: dict[str, list[OneShot]] = {}
    for shot in shots:
        alike.setdefault(shot.group, []).append(shot)
    found: dict[str, list[OneShot]] = {}
    for members in alike.values():
        members.sort(key=lambda shot: (shot.at, shot.session_id))
        run: list[OneShot] = []
        for shot in [*members, None]:
            if shot is None or (run and shot.at - run[-1].at > gap):
                if len(run) >= least or any(member.named for member in run):
                    found |= dict.fromkeys((member.session_id for member in run), run)
                run = []
            if shot is not None:
                run.append(shot)
    return found


def ruled_out(cwd: str, at: float, by: str, windows: list[Window], temporary: list[str]) -> bool:
    """Whether a session's folder (`cwd`, a real path) rules out that a worker's command made
    it: that worker ran commands then, none where it ran (nor in a temporary folder or its
    own). Without a command of the worker's then, nothing does."""
    if not cwd or any(inside(cwd, folder) for folder in temporary):
        return False
    mine = [w for w in windows if w.by == by and w.start <= at <= max(w.end, w.limit)]
    return bool(mine) and not any(cwd in w.places or inside(cwd, w.folder) for w in mine)


def decide(
    fresh: Iterable[OneShot],
    known: Iterable[OneShot],
    windows: list[Window],
    tied: Mapping[str, Iterable[float]] | None = None,
    kept: Mapping[str, dict] | None = None,
) -> dict[str, dict]:
    """Who made each fresh one-shot session (see the module's docstring): its record, as kept
    (`as`: worker or batch; `by`: the worker's session; `why`: folder, command, batch or named;
    `named`: named as a batch's; `rules`: RULES), or `as` alike (the owner's still: never kept).
    Those not in it are the owner's. `known`: those decided before, which batches may
    continue; `tied`: when each session decided a worker's began, by worker (see
    Window.covers); `kept`: what an earlier version decided a worker made, kept unless these
    rules find it is not (see ruled_out) or the owner's own log says so (not a single shot)."""
    fresh = sorted(fresh, key=lambda shot: (shot.at, shot.session_id))
    known = list(known)
    close = batches([*fresh, *known])
    loose = batches([*fresh, *known], WORKER_GAP, HIDE_SIZE)
    temporary = temporary_folders()
    came = {by: sorted(ats) for by, ats in (tied or {}).items()}
    decided = {}
    for shot in fresh:
        cwd = os.path.realpath(shot.cwd) if shot.cwd else ""
        scratch = bool(cwd) and any(inside(cwd, folder) for folder in temporary)
        during = [w for w in windows if w.covers(shot.at, came.get(w.by, []))]
        own = [w for w in during if inside(cwd, w.folder)]
        # Where the command ran, or a temporary folder: not the owner's own project.
        there = [w for w in during if w in own or (cwd and (cwd in w.places or scratch))]
        tied_to = [(w, "folder") for w in own]
        tied_to += [(w, "command") for w in there if w.copilot]
        run = close.get(shot.session_id)
        kind = "named" if shot.named else "batch"
        if not tied_to:
            # A batch that began soon after a command it runs in did: named a batch's, or a
            # script's (ten or more alike, close together); or, in a temporary folder or the
            # worker's own (where the owner's alike questions are not), ten or more alike each
            # within WORKER_GAP of the one before. Never by the folder and the time alone.
            for w in there:
                runs = [run] if shot.named or (run and len(run) >= HIDE_SIZE) else []
                if not shot.named and (scratch or w in own):
                    runs.append(loose.get(shot.session_id))
                for members in filter(None, runs):
                    began = min(m.at for m in members if m.at >= w.start)
                    if began - w.start <= LEAD:
                        tied_to.append((w, kind))
                        break
        record = {"group": shot.group, "at": shot.at, "rules": RULES} | (
            {"named": True} if shot.named else {}
        )
        old = (kept or {}).get(shot.session_id) or {}
        if tied_to:
            order = {"folder": 2, "command": 1, "batch": 0, "named": 0}
            window, why = max(tied_to, key=lambda pair: (order[pair[1]], pair[0].start))
            decided[shot.session_id] = {"as": MADE_BY_WORKER, "by": window.by, "why": why, **record}
            bisect.insort(came.setdefault(window.by, []), shot.at)
        elif run and (shot.named or len(run) >= HIDE_SIZE):
            decided[shot.session_id] = {"as": BATCH, "by": "", "why": kind, **record}
        elif old.get("as") == MADE_BY_WORKER and not ruled_out(
            cwd, shot.at, str(old.get("by") or ""), windows, temporary
        ):
            decided[shot.session_id] = old | {"rules": RULES}
            bisect.insort(came.setdefault(str(old.get("by") or ""), []), shot.at)
        elif run:
            decided[shot.session_id] = {"as": ALIKE, "by": "", "why": kind, **record}
    return decided


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def runs_of(store: Store) -> list[tuple[float, float]]:
    """Chief of Staff's runs, as kept: (start, when it was last known running), oldest first."""
    try:
        found = json.loads(store.get("uptime") or "[]")
        return [(float(start), float(end)) for start, end in found]
    except (ValueError, TypeError):
        return []


def window_end(
    start: float, ended: float | None, runs: list[tuple[float, float]]
) -> tuple[float, float] | None:
    """Until when a command may have run (`end`), and at most (`limit`, past `end` only while
    its worker's sessions keep coming: see Window.covers): its end when Chief of Staff saw it
    end, in the run it began in; else (still running, left running in the background, its end
    never told, or Chief of Staff stopped meanwhile: an end its next start gave it,
    end_commands) COMMAND_MOST after it began, never past that run's end (its limit). None for
    one that began while Chief of Staff was not running; one from before its runs were kept (an
    older version's data, where an end seen and one given at a start look alike) for
    COMMAND_MOST at most, up to its end."""
    most = start + COMMAND_MOST
    run = next((run for run in runs if run[0] - 5 <= start <= run[1] + ALIVE_SLACK), None)
    if run is None:
        if runs and start >= runs[0][0]:
            return None
        end = min(ended or math.inf, most)
        return end, max(end, ended or end)
    stop = run[1] + ALIVE_SLACK
    if ended is not None and ended <= stop:
        return ended, ended
    return min(stop, most), stop


def windows(
    store: Store,
    low: float,
    high: float,
    folder_of: Callable[[dict], Path | None],
    runs: list[tuple[float, float]],
) -> list[Window]:
    """The commands Chief of Staff's sessions ran that may have been running at some time from
    `low` to `high` (see window_end), while Chief of Staff was (`runs`)."""
    rows = store.all(
        "SELECT session_id, command, started_at, ended_at FROM commands "
        "WHERE started_at <= ? AND (ended_at IS NULL OR ended_at >= ?)",
        (iso(high + 60), iso(low - 60)),
    )
    sessions: dict[str, tuple[str, str]] = {}
    found = []
    for row in rows:
        start = epoch(row["started_at"])
        if start is None:
            continue
        span = window_end(start, epoch(row["ended_at"]) if row["ended_at"] else None, runs)
        if span is None or span[1] < low or start > high:
            continue
        by = row["session_id"]
        if by not in sessions:
            session = store.find_session(by)
            folder = folder_of(session) if session else None
            sessions[by] = (
                os.path.realpath(folder) if folder else "",
                str((session or {}).get("cwd") or ""),
            )
        folder, cwd = sessions[by]
        command = row["command"] or ""
        found.append(
            Window(by, start, span[0], runs_copilot(command), folder, places(command, cwd), span[1])
        )
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
    """What a round of reading found: the size of each log decided before by these RULES
    (None: gone or not a log), the shape of each other one read, and how many were left for the
    next round (`pending`: those have none)."""

    sizes: dict[str, int | None]
    shapes: dict[str, Shape]
    pending: int = 0


def current(record: dict) -> bool:
    """Whether a kept decision was made by these RULES (one by 0.1.43's is made again)."""
    return record.get("rules") == RULES


class Provenance:
    """Which of the sessions that would be the owner's a worker or a batch made (see the
    module's docstring), for the index, Workers.external, habits and tidy-sessions alike."""

    def __init__(
        self,
        store: Store,
        log_of: Callable[[str], Path | None],
        folder_of: Callable[[dict], Path | None],
        keep: bool = True,
        uptime: Callable[[], list[tuple[float, float]]] | None = None,
    ):
        self.store = store
        self.log_of = log_of
        self.folder_of = folder_of
        self.keep = keep  # Records what it decides (tidy-sessions' preview changes nothing).
        # Chief of Staff's runs (see window_end): a running one's own ends now.
        self.uptime = uptime or (lambda: runs_of(store))
        self.lock = asyncio.Lock()
        # Each log read: its identity then, and its shape.
        self.shapes: dict[str, tuple[tuple, Shape]] = {}
        # What was decided (kept in the store, and what this one decided when not kept), read
        # from the store again only when it holds another number of them; and by batch: group
        # -> session id -> when it began.
        self.cache: dict[str, dict] | None = None
        self.counted = -1
        self.decided: dict[str, dict] = {}  # What it decided (when not kept).
        self.groups: dict[str, dict[str, float]] = {}
        self.workers: dict[str, dict[str, float]] = {}  # Worker -> its sessions -> began.
        # One-shot sessions found the owner's, as they were then, with those they were alike
        # with (see batches): looked at again when either changes.
        self.settled: dict[str, tuple[OneShot, tuple[str, ...]]] = {}
        # Those found alike, tied to no worker (see ALIKE): the owner's; tidy-sessions names them.
        self.alike: dict[str, dict] = {}
        # How many logs of the last listing were left for the next round (see look).
        self.pending = 0

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
        self.pending = 0
        if not candidates:
            return {}
        # The newest first: what is shown first is decided first.
        candidates.sort(key=lambda meta: stamp(getattr(meta, "modified_time", None)), reverse=True)
        async with self.lock:
            records = await self.records()
            paths = {meta.session_id: self.log_of(meta.session_id) for meta in candidates}
            look = await asyncio.to_thread(self.look, paths, records)
            self.pending = look.pending
            # Changed since (continued in a terminal, say): looked at again.
            gone = [
                key
                for key, size in look.sizes.items()
                if size is not None and size != records[key].get("size")
            ]
            self.drop(gone)
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
            # Those decided before, of the batches these may be of (a named one's group is its
            # name's: a new one of it is named itself); by other rules too, not read yet: when
            # and how it began are as they were (a batch read again in parts stays one).
            fresh = {shot.session_id for shot in shots}
            earlier = [
                OneShot(key, at, group)
                for group in {shot.group for shot in shots}
                for key, at in self.groups.get(group, {}).items()
                if key not in fresh
            ]
            # One found the owner's stays theirs unless those it is alike with change (the
            # commands it began during are known by the time it ended).
            batched = batches([*shots, *earlier])

            def alike(shot: OneShot) -> tuple[str, ...]:
                return tuple(member.session_id for member in batched.get(shot.session_id, ()))

            due = [
                shot for shot in shots if self.settled.get(shot.session_id) != (shot, alike(shot))
            ]
            found = {}
            if due:
                spans = [shot.at for shot in due]
                runs = self.uptime()
                commands = windows(self.store, min(spans), max(spans), self.folder_of, runs)
                tied = {w.by: self.workers.get(w.by, {}).values() for w in commands}
                # What an earlier version found a worker made: kept unless these rules or the
                # session's own log find otherwise (see decide).
                kept = {
                    shot.session_id: records[shot.session_id]
                    for shot in due
                    if shot.session_id in records and not current(records[shot.session_id])
                }
                found = decide(due, earlier, commands, tied, kept)
            for shot in due:
                record = found.get(shot.session_id)
                if record and record["as"] != ALIKE:
                    record["size"] = self.shapes[shot.session_id][0][2]
                    self.settled.pop(shot.session_id, None)
                    self.alike.pop(shot.session_id, None)
                    continue
                self.settled[shot.session_id] = (shot, alike(shot))
                if record:
                    self.alike[shot.session_id] = record
                else:
                    self.alike.pop(shot.session_id, None)
            made = {key: record for key, record in found.items() if record["as"] != ALIKE}
            # Decided by other rules (an earlier version's), read again: the owner's, unless
            # decided so again; one whose log could not be read, as it was unless it changed.
            redone = [
                key
                for key, shape_ in look.shapes.items()
                if key in records
                and not current(records[key])
                and key not in made
                and (shape_.read or self.shapes[key][0][2] != records[key].get("size"))
            ]
            self.drop(redone)
            self.record(made, gone + redone)
        return {
            meta.session_id: records[meta.session_id]["as"]
            for meta in candidates
            if meta.session_id in records
        }

    async def records(self) -> dict[str, dict]:
        """What was decided, by session id (see cache): read in a thread when it changed."""
        count = int(
            self.store.value("SELECT count(*) FROM kv WHERE key >= ? AND key < ?", (KV, "made;"))
        )
        if self.cache is None or count != self.counted:
            cache = await asyncio.to_thread(known, self.store)
            if not self.keep:
                cache |= self.decided
            self.cache, self.counted = cache, count
            self.groups, self.workers = {}, {}
            for key, record in cache.items():
                self.index(key, record)
        return self.cache

    async def kept(self) -> set[str]:
        """Every session decided a worker's or a script's, listed or not (moved away)."""
        async with self.lock:
            return set(await self.records())

    def index(self, key: str, record: dict):
        group = str(record.get("group") or "")
        self.groups.setdefault(group, {})[key] = float(record.get("at") or 0)
        if record.get("as") == MADE_BY_WORKER and (by := record.get("by")):
            self.workers.setdefault(str(by), {})[key] = float(record.get("at") or 0)

    def drop(self, keys: Iterable[str]):
        for key in keys:
            record = (self.cache or {}).pop(key, None)
            if record is not None:
                self.groups.get(str(record.get("group") or ""), {}).pop(key, None)
                self.workers.get(str(record.get("by") or ""), {}).pop(key, None)

    def look(self, paths: Mapping[str, Path | None], records: Mapping[str, dict]) -> Look:
        """In a thread: the size of each log decided before by these RULES, and the shape of the
        others (one decided by other rules too), read within the round's bytes."""
        sizes: dict[str, int | None] = {}
        shapes: dict[str, Shape] = {}
        left, pending = ROUND_BYTES, 0
        for key, path in paths.items():
            now = identity(path) if path else None
            record = records.get(key)
            if record is not None and current(record):
                sizes[key] = now[2] if now else None
                if now is None or now[2] == record.get("size"):
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
                pending += 1
                continue  # In the next round.
            else:
                log = read_log(path, SHAPE_MOST) if path else None
                left -= now[2]
                if log is None or identity(path) != now:  # type: ignore[arg-type]
                    pending += 1
                    continue  # Changed while read: read again next round.
                found = shape(log)
            self.shapes[key] = (now, found)
            shapes[key] = found
        return Look(sizes, shapes, pending)

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
        if self.cache is not None:
            self.cache |= found
            for key, record in found.items():
                self.index(key, record)
        if not self.keep:
            for key in gone:
                self.decided.pop(key, None)
            self.decided |= found
            return
        self.store.set_many(
            [(KV + key, json.dumps(record, sort_keys=True)) for key, record in found.items()],
            [KV + key for key in gone],
        )
        if found or gone:
            self.counted = int(
                self.store.value(
                    "SELECT count(*) FROM kv WHERE key >= ? AND key < ?", (KV, "made;")
                )
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
