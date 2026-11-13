"""Live test of where the owner's other Copilot sessions stand (others.py), on real Copilot.

Real `copilot` CLIs run in pseudo-terminals, as the owner runs them, in a Copilot home of their
own (a copy of your sign-in: none of your sessions is read or changed), next to a Chief of
Staff that reads their logs (docs/design/adopt-sessions.md, section 7, "Live, on real Copilot"):

O1 a terminal session asks through ask_user (a), is answered there and answers (b), makes no
   progress on a long `sleep` (c: the threshold shortened to STALL seconds), exits (d); another
   is killed while it asks, and resumed (e).
O5 one is stopped with Esc in the middle of a step (a: the owner's turn, "Stopped by you", never
   working after the stop is logged), turns idle once that is IDLE seconds old (b: an hour in
   use), and is stopped when closed; another's turn fails with a session error, provoked by a
   model provider of the test's own that refuses every request (c: no model call).
O3 reading changes nothing in the home: every file of the sessions read, before and after
   passes, fresh listings and first looks.
O2 whose session it is (section 4.2), with a second Chief of Staff on the same home: this one's
   own sessions are never listed (a); the other's are left out (b); fake_terminal.py, another
   app that names itself, is the owner's (c); a terminal session last run by the other Chief of
   Staff gets no status (d, rule 5) and gets one again once run in a terminal (e, rule 6); the
   other's own session taken over in a terminal is the owner's (f, rule 6) until it runs it
   again (g, rule 4); a script's session (through the SDK, naming no app) is never listed, open
   or closed, until continued in a terminal (h).
O4 a closed session continued here works and leaves the list (a); an open one is refused (b).
O6 whose a `copilot -p` session is (provenance.py): five alike that a script runs one after
   the other are a batch, left out of the sidebar's rows and the Router's list (/api/external)
   once they ended, and kept so in the store, as is a single one named `batch: …` (a); a single
   one is the owner's, listed and finished (b); `chiefofstaff tidy-sessions` shows the batch,
   what would be dropped and what was learned from it, changing nothing, and leaves them while
   they are new (c); made two hours old, --apply moves them with a manifest, drops what was not
   learned yet and keeps the one-off and the rest; one moved back is listed by Copilot again
   and still left out (d).

They run in the order O1, O5, O3, O2, O4, O6, five to eight minutes in all; each logs "O1a
passed"… and the run ends with a report (each session's statuses as shown, Fresh and Found with
their p95, the CLI versions that ran) and "LIVE OTHERS PASSED".

All along, a tab's event stream is held open (the index reads only while one is) and
/api/others is read every second: every status given is checked against the session's log at
the position it is as of (its lease says how old that is), with the open flag its row shows,
and against what the terminal was driven to do; none may be wrong. A row's open flag is
measured as Found and must follow the terminal; whose a session is follows Copilot's listing,
which the index reads when what is open changes, else once a minute. Fresh (an event written to
its status shown) and Found (a terminal opening or closing a session to its row saying so) are
measured against section 4.6's targets.

The Chief of Staffs run on ports 18950 and 18951 (the refusing provider on 18952; another
three from LIVE_OTHERS_PORT), with CHIEFOFSTAFF_OTHERS_STALL_SECONDS and
CHIEFOFSTAFF_OTHERS_IDLE_SECONDS set; the CLIs in a Copilot home and folders of their own,
deleted at the end with every process. The terminals and `copilot -p` run the CLI as the owner
runs it (the version `copilot --version` shows; Chief of Staff's own CLI is the one the SDK
starts, with auto-update off, which may be older).
Uses your signed-in Copilot CLI and a few cheap model calls (LIVE_OTHERS_MODEL, by default
claude-haiku-5.5; O6 makes seven). When the model does not do as told, twice (it does not ask, or
does not run the command), or a terminal does not get where it was driven, the run ends with
"NOT THE PRODUCT" (exit code 2), not as a product failure.

    uv run --with pexpect --with pyte python scripts/live_others.py

LIVE_OTHERS_TMP: the folder its temporary folder goes in; LIVE_OTHERS_KEEP=1 keeps that.
"""

import asyncio
import bisect
import itertools
import json
import math
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

try:
    import pexpect
    import pyte
except ImportError:
    sys.exit("Run it with: uv run --with pexpect --with pyte python scripts/live_others.py")

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e
from live_e2e import App, log

from chiefofstaff.backend import copilot_backend
from chiefofstaff.others import FRESH as BEHIND
from chiefofstaff.others import SHOWN

BASE_PORT = int(os.environ.get("LIVE_OTHERS_PORT", "18950"))
PORT, OTHER_PORT = BASE_PORT, BASE_PORT + 1
MODEL = os.environ.get("LIVE_OTHERS_MODEL", "claude-haiku-5.5")
COPILOT = shutil.which("copilot") or "copilot"
# As the owner starts it in a terminal: the version they run (auto-update off would start the
# one the CLI keeps for that, which may be older).
CLI = ["--allow-all", "--model", MODEL]
# No progress after this many seconds without the log growing (15 minutes in use), and a
# command that runs well past it.
STALL = 12
SLEEP = 32
# Idle once a turn ended this long ago (an hour in use).
IDLE = 30
# A model provider of the test's own, that refuses every request: a session error, for free.
PROVIDER_PORT = BASE_PORT + 2
# The design's targets (section 4.6), and the most a single sample may take.
FRESH_TARGET, FOUND_TARGET = 4.0, 13.0
FOUND_MOST = 25.0
# What the test's own clocks and polling may be off by.
SLACK = 1.0
# A session becomes this Chief of Staff's in its store: left out by the index's next pass (2 s),
# seen in its store by this test within 2 s.
OWN_LAG = 5.0
# How late a change of whose a session is may be seen when nothing opens or closes: at the
# index's next listing, once a minute.
LISTING = 75.0
# The question form's mark on the option chosen, and on the owner's input.
CHOSEN = "\u276f"
# Sessions whose rows say whose they are, not where they stand.
NOT_OWNERS = {"unlisted", "unknown", "another"}

ASK = (
    "Before doing anything, ask me with your ask_user tool which port to use, offering the "
    "choices 8080 and 9090. Then write only the chosen number to port.txt and reply with just "
    "the number."
)
ASK_AGAIN = (
    "Use your ask_user tool now to ask me which port to use, offering the choices 8080 and "
    "9090. Then write only the chosen number to port.txt and reply with just the number."
)
SLEEPY = (
    f"Run the shell command `sleep {SLEEP}` in the foreground and wait for it to finish (it "
    f"takes {SLEEP} seconds, so give it an initial wait of 120 seconds). Then reply with just "
    "DONE."
)
OK = "Reply with just the word OK."
# O6: what a script labelling tickets of a made-up shop asks `copilot -p`, one ticket a call.
LABEL = (
    "You label support tickets for acme, a made-up shop. Reply with one word, billing, outage, "
    "feature or other, for this ticket: "
)
TICKETS = [
    "I was charged twice for one order.",
    "The checkout page does not load.",
    "Please add a dark mode.",
    "Where is my parcel?",
    "Refund the second charge, please.",
]
# How others.py words a turn the owner stopped.
STOPPED = "Stopped by you"


class NotTheProductError(Exception):
    """The model did not do what it was told (twice), or a terminal did not get where it was
    driven: not a product failure."""


def wall() -> float:
    return time.time()


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)] if ordered else float("nan")


def epoch(text: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except ValueError:
        return None


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def tree_of(pid: int) -> set[int]:
    """A process and its descendants."""
    table = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout
    children: dict[int, list[int]] = {}
    for line in table.splitlines():
        with suppress(ValueError):
            child, parent = map(int, line.split())
            children.setdefault(parent, []).append(child)
    found, todo = {pid}, [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in found:
                found.add(child)
                todo.append(child)
    return found


def command_of(pid: int) -> str:
    found = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
    return found.stdout.strip()


def make_home(root: Path, trusted: list[Path]) -> Path:
    """A Copilot home of its own: a copy of the owner's sign-in, the test's folders trusted, the
    cheap model by default (for fake_terminal.py's session too)."""
    home = root / "copilot-home"
    home.mkdir()
    text = (Path.home() / ".copilot" / "config.json").read_text()
    config = json.loads("\n".join(x for x in text.splitlines() if not x.lstrip().startswith("//")))
    config["trustedFolders"] = [str(folder) for folder in trusted]
    (home / "config.json").write_text(json.dumps(config))
    (home / "settings.json").write_text(json.dumps({"model": MODEL, "banner": "never"}))
    return home


def workspace(home: Path, session_id: str) -> dict[str, str]:
    """A session's workspace.yaml (flat keys): where Copilot keeps who made and last ran it."""
    path = home / "session-state" / session_id / "workspace.yaml"
    found = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        key, _, value = line.partition(":")
        found[key.strip()] = value.strip()
    return found


def snapshot(home: Path, ids: list[str]) -> dict[str, tuple[int, int, int]]:
    """Every file and folder of these sessions: its kind, size and change time."""
    found = {}
    for session_id in ids:
        folder = home / "session-state" / session_id
        for path in [folder, *sorted(folder.rglob("*"))]:
            info = path.lstat()
            found[str(path.relative_to(home))] = (
                stat.S_IFMT(info.st_mode),
                info.st_size,
                info.st_mtime_ns,
            )
    return found


# What a log says, as this test reads it


@dataclass(frozen=True)
class State:
    """Where a session stands at one position of its log."""

    ask: str | None = None  # the question an own ask_user waits on
    ask_call: str = ""
    ask_at: str = ""  # the ask's own time (the log's)
    ask_seen: float = 0.0  # when this test saw it written
    ask_arguments: dict = field(default_factory=dict, compare=False)
    turn: str = "none"  # none, open or ended
    ended: str = ""  # answered, stopped or failed
    ended_seen: float = 0.0  # when this test saw it end
    ended_own: float = 0.0  # its own time
    final: bool = False  # the open turn's final answer was seen
    words: str = ""  # its latest words, as the index keeps them
    pending: tuple[tuple[str, str], ...] = ()  # steps begun, not ended: (call, command)
    exited: bool = False
    partial: bool = False  # since a resume, until its next model call
    messages: int = 0  # the owner's messages
    answered: int = 0  # turns ended with an answer
    resumes: int = 0

    @property
    def step(self) -> str:
        return self.pending[-1][1] if self.pending else ""


def advance(state: State, kind: str, data: dict, timestamp: str, seen: float) -> State:
    """The state after one of the session's own events."""
    change: dict = {}
    pending = dict(state.pending)
    if kind == "session.resume":
        return State(
            partial=True,
            messages=state.messages,
            answered=state.answered,
            resumes=state.resumes + 1,
        )
    if kind == "assistant.turn_start":
        change = {"partial": False, "turn": "open", "final": False, "exited": False}
        if state.ask is not None:
            change |= {"ask": None, "ask_call": "", "words": ""}
        pending.clear()
    elif kind == "user.message":
        change = {"final": False, "messages": state.messages + bool(data.get("content"))}
        if state.turn != "open" and not state.partial:
            change["turn"] = "open"
    elif kind == "assistant.message":
        content = str(data.get("content") or "").strip()
        if content and state.ask is None:
            change["words"] = content
        change["final"] = not data.get("toolRequests")
    elif kind == "tool.execution_start":
        call, name = str(data.get("toolCallId")), data.get("toolName")
        arguments = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
        if name == "ask_user":
            question = str(arguments.get("message") or arguments.get("question") or "").strip()
            change = {
                "ask": question,
                "ask_call": call,
                "ask_at": timestamp,
                "ask_seen": seen,
                "ask_arguments": arguments,
                "words": question,
            }
        else:
            command = str(arguments.get("command") or name)
            pending[call] = command + (" (async)" if arguments.get("mode") == "async" else "")
    elif kind == "tool.execution_complete":
        call = str(data.get("toolCallId"))
        if call == state.ask_call:
            change = {"ask": None, "ask_call": "", "words": ""}
        pending.pop(call, None)
    elif kind == "assistant.turn_end":
        if state.turn == "open" and state.final and not pending and state.ask is None:
            change = {
                "turn": "ended",
                "ended": "answered",
                "ended_seen": seen,
                "ended_own": epoch(timestamp) or seen,
                "answered": state.answered + 1,
            }
    elif kind in {"abort", "session.error"}:
        change = {
            "turn": "ended",
            "ended_seen": seen,
            "ended_own": epoch(timestamp) or seen,
            "ask": None,
            "ask_call": "",
            "ended": "stopped" if kind == "abort" else "failed",
            # Its words: an error's message on one line, none for a stop.
            "words": " ".join(str(data.get("message") or "It failed.").split())
            if kind == "session.error"
            else "",
        }
        pending.clear()
    elif kind == "session.shutdown":
        change = {"exited": True, "ask": None, "ask_call": ""}
        pending.clear()
    else:
        return state
    fields = {**state.__dict__, **change, "pending": tuple(pending.items())}
    return State(**fields)


def expected(
    state: State, shown_open: bool, quiet: tuple[float, float], moment: float
) -> set[str | None]:
    """The statuses the index may give a session in this state, open as its row says, at
    `moment`."""
    if state.partial:
        return {None}
    if not shown_open:
        if state.turn == "none":
            return {None}
        return {None, "finished" if state.ended == "answered" else "stopped"}
    if state.exited or state.turn == "none":
        return {None}
    if state.ask is not None:
        return {None, "asks"}
    if state.turn == "ended":
        # How long ago it ended, as the index counts: from the pass that saw it come (up to a
        # pass after this test did), else from its own time.
        least = moment - state.ended_seen - 3
        most = moment - min(state.ended_seen, state.ended_own) + 1
        return (
            {None}
            | ({"your_turn"} if least < IDLE else set())
            | ({"idle"} if most >= IDLE else set())
        )
    least, most = quiet
    found: set[str | None] = {None}
    if most >= STALL - 3:
        found.add("no_progress")
    if least < STALL + 3:
        found.add("working")
    return found


def open_key(state: State) -> str | None:
    """What an open session in this state shows, right after the event that made it."""
    if state.partial or state.exited or state.turn == "none":
        return None
    if state.ask is not None:
        return "asks"
    return "your_turn" if state.turn == "ended" else "working"


class Truth:
    """One session as this test sees it: its log, read as it grows (each state with when it was
    seen written), and whether a program other than this Chief of Staff's has it open."""

    def __init__(self, ctx: "Ctx", session_id: str, label: str):
        self.ctx, self.session_id, self.label = ctx, session_id, label
        self.folder = ctx.home / "session-state" / session_id
        self.path = self.folder / "events.jsonl"
        self.offset = 0
        self.rest = b""
        self.states: list[tuple[float, State]] = [(0.0, State())]
        self.grew: list[float] = []
        self.opened: list[tuple[float, bool]] = []  # (when, open), from the first poll on
        self.key: str | None = None

    @property
    def state(self) -> State:
        return self.states[-1][1]

    @property
    def is_open(self) -> bool:
        return bool(self.opened) and self.opened[-1][1]

    def poll(self, now: float, excluded: set[int]):
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        if size > self.offset:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read(size - self.offset)
            self.offset += len(chunk)
            self.grew.append(now)
            *lines, self.rest = (self.rest + chunk).split(b"\n")
            for line in lines:
                with suppress(ValueError):
                    event = json.loads(line)
                    data = event.get("data") if isinstance(event.get("data"), dict) else {}
                    if data.get("agentId") or data.get("parentToolCallId"):
                        continue  # A sub-agent's.
                    stamp = event.get("timestamp")
                    state = advance(self.state, event.get("type"), data, stamp, now)
                    if state == self.state:
                        continue
                    self.states.append((now, state))  # Each one: a pass may sample between.
                    key = open_key(state)
                    if key != self.key:
                        self.key = key
                        # Written when it was made (its own time), unless that is not to trust.
                        own = epoch(stamp)
                        made = own if own is not None and now - 5 <= own <= now else now
                        self.ctx.watch.expect(self, key, made)
        held = False
        for lock in self.folder.glob("inuse.*.lock"):
            pid = lock.name.split(".")[1]
            if pid.isdigit() and int(pid) not in excluded and alive(int(pid)):
                held = True
        if not self.opened or self.opened[-1][1] != held:
            self.opened.append((now if self.opened else 0.0, held))

    def between(self, start: float, end: float) -> list[State]:
        """Every state the log was in from `start` to `end`."""
        times = [moment for moment, _ in self.states]
        first = max(0, bisect.bisect_right(times, start) - 1)
        return [state for moment, state in self.states[first:] if moment <= end]

    def last_growth(self, moment: float) -> float:
        index = bisect.bisect_right(self.grew, moment)
        return self.grew[index - 1] if index else -math.inf

    def quiet(self, start: float, end: float) -> tuple[float, float]:
        """The least and the most time the log had not grown for, from `start` to `end`."""
        inside = [moment for moment in self.grew if start < moment <= end]
        most = end - self.last_growth(end)
        for before, after in itertools.pairwise(inside):
            most = max(most, after - before)
        if inside:
            most = max(most, inside[0] - self.last_growth(start))
        least = 0.0 if inside else start - self.last_growth(start)
        return least, most

    async def until(self, what: str, check, timeout: float) -> State:
        log(f"waiting: {self.label}: {what}")
        began = wall()
        while wall() - began < timeout:
            if check(self.state):
                log(f"ok: {self.label}: {what} ({wall() - began:.1f}s)")
                return self.state
            await asyncio.sleep(0.1)
        raise NotTheProductError(
            f"{self.label}: {what}: not within {timeout:.0f}s; its log says {self.state}"
        )


# Chief of Staff, as a tab sees it


@dataclass
class Expect:
    key: str
    at: float
    counted: bool  # its row was listed, open and the owner's when it was written
    done: bool = False


class Watch:
    """Chief of Staff's sidebar as a tab has it: the event stream held open, /api/others read
    every second, and every status given checked against the truth."""

    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx
        self.rows: dict[str, dict] = {}
        self.received: dict[str, float] = {}  # When each row came.
        self.deadline: dict[str, float] = {}
        self.run = ""
        self.last_pass = -1
        self.passes = 0
        self.wrong: list[str] = []
        self.observations = 0
        self.statuses = 0
        self.keys: dict[str, int] = {}
        self.reasons: dict[str, int] = {}
        self.fresh: list[tuple[str, str, float]] = []
        self.missed: list[tuple[str, str, float]] = []
        self.found: list[tuple[str, str, float]] = []
        self.stalled: list[float] = []  # no progress shown, after the threshold
        self.idled: dict[str, float] = {}  # idle first shown, after the threshold
        self.expecting: dict[str, Expect] = {}
        self.opening: dict[str, int] = {}  # the next transition of a session's open to see
        self.timeline: dict[str, list[tuple[float, bool, str | None, str]]] = {}
        self.tasks: list[asyncio.Task] = []
        self.connected = asyncio.Event()

    def start(self):
        self.tasks = [asyncio.create_task(self.stream()), asyncio.create_task(self.poll())]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def problem(self, text: str):
        if text not in self.wrong[-20:]:
            log(f"WRONG: {text}")
        self.wrong.append(text)

    async def stream(self):
        app = self.ctx.app
        while True:
            try:
                timeout = httpx.Timeout(10, read=60)
                async with (
                    httpx.AsyncClient(base_url=app.base, timeout=timeout) as client,
                    client.stream(
                        "GET", "/api/events", params={"key": app.launch_token}
                    ) as response,
                ):
                    response.raise_for_status()
                    listing = await app.get("/api/others")  # As the page resyncs.
                    self.rows = {row["id"]: row for row in listing["rows"]}
                    self.connected.set()
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            payload = json.loads(line[6:])
                            if payload.get("type") == "others":
                                self.event(payload["data"], wall())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"the event stream broke ({type(exc).__name__}); connecting again")
            self.connected.clear()
            self.deadline.clear()  # The page drops every lease when its stream breaks.
            await asyncio.sleep(1)

    async def poll(self):
        while True:
            await asyncio.sleep(1)
            with suppress(Exception):
                listing = await self.ctx.app.get("/api/others")
                moment = wall()
                for row in listing["rows"]:
                    status = row["status"]
                    if status and not 0 <= row["valid_for"] <= BEHIND:
                        self.problem(f"{self.ctx.label(row['id'])}: valid_for {row['valid_for']}")
                    age = BEHIND - row["valid_for"] if status else 0.0
                    self.observe(row["id"], row, moment, age, "listing")

    def event(self, data: dict, moment: float):
        if data["run"] != self.run:
            self.run, self.last_pass = data["run"], -1
        if data["pass"] < self.last_pass:
            return  # Older news.
        self.last_pass = self.passes = data["pass"]
        leases = data["leases"]
        for row in data["rows"]:
            self.rows[row["id"]] = row
            self.received[row["id"]] = moment
            if row["status"] and row["id"] not in leases:
                self.problem(f"{self.ctx.label(row['id'])}: a status without a lease")
        for session_id in data["gone"]:
            self.rows.pop(session_id, None)
            self.deadline.pop(session_id, None)
            self.note(session_id, moment, None)
        for session_id, valid in leases.items():
            self.deadline[session_id] = moment + valid
        for session_id in {row["id"] for row in data["rows"]} | set(leases):
            if row := self.rows.get(session_id):
                age = BEHIND - leases.get(session_id, BEHIND) if row["status"] else 0.0
                self.observe(session_id, self.counted_on(row, moment), moment, age, "event")
                self.note(session_id, moment, row)

    def counted_on(self, row: dict, moment: float) -> dict:
        """A row renewed by a lease, its elapsed time counted on from when it came (as the page
        counts it)."""
        status = row["status"]
        if not status or status["elapsed"] is None:
            return row
        later = moment - self.received.get(row["id"], moment)
        return {**row, "status": {**status, "elapsed": status["elapsed"] + later}}

    def note(self, session_id: str, moment: float, row: dict | None):
        entry = (
            (moment, row["open"], (row["status"] or {}).get("key"), row["reason"])
            if row
            else (moment, False, None, "gone")
        )
        line = self.timeline.setdefault(session_id, [])
        if not line or line[-1][1:] != entry[1:]:
            line.append(entry)

    def shown(self, session_id: str) -> dict | None:
        """A row as the page draws it now: a status only while its lease holds."""
        row = self.rows.get(session_id)
        if row and row["status"] and wall() >= self.deadline.get(session_id, 0):
            return {**row, "status": None}
        return row

    def observe(self, session_id: str, row: dict, moment: float, age: float, source: str):
        self.observations += 1
        label = self.ctx.label(session_id)
        status = row["status"]
        key = status["key"] if status else None
        if self.ctx.forbidden(session_id, moment):
            self.problem(f"{label}: listed ({source}: {key}, {row['reason']}), but it is a Chief "
                         "of Staff's own session")  # fmt: skip
        if status:
            self.statuses += 1
            self.keys[key] = self.keys.get(key, 0) + 1
        else:
            self.reasons[row["reason"]] = self.reasons.get(row["reason"], 0) + 1
        truth = self.ctx.truths.get(session_id)
        if not truth:
            return
        # What the index read was written by now: read here too, as of now.
        truth.poll(max(moment, truth.states[-1][0]), self.ctx.excluded)
        if status:
            self.check(truth, row, moment, age, source)
        if source == "event":
            self.measure(truth, row, moment)
        changed = truth.opened[-1][0] if truth.opened else 0.0
        if truth.opened and row["open"] != truth.is_open and moment - changed > FOUND_MOST:
            self.problem(f"{label}: shown {'open' if row['open'] else 'closed'} for "
                         f"{moment - changed:.0f}s after it was not")  # fmt: skip

    def check(self, truth: Truth, row: dict, moment: float, age: float, source: str):
        status = row["status"]
        key, words = status["key"], status["words"]
        start = moment - age - SLACK
        quiet = truth.quiet(start, moment)
        states = truth.between(start, moment)
        fits = [s for s in states if key in expected(s, row["open"], quiet, moment)]
        said = (f"{truth.label}: {key} {words[:60]!r} (open {row['open']}, as of {age:.1f}s ago, "
                f"{source})")  # fmt: skip
        if not fits:
            kinds = sorted(
                {str(k) for s in states for k in expected(s, row["open"], quiet, moment)}
            )
            self.problem(f"{said} is wrong: its log allowed {kinds}; quiet {quiet}; "
                         f"states {states[-2:]}")  # fmt: skip
            return
        if not any(self.fits_words(status, s, moment, age, truth) for s in fits):
            self.problem(f"{said}: its words, time or elapsed are not its log's: {fits[-1]}")

    def fits_words(self, status: dict, state: State, moment: float, age: float, truth) -> bool:
        key, words, elapsed = status["key"], status["words"], status["elapsed"]
        room = age + 3 * SLACK + 2

        def near(seen: float) -> bool:
            return elapsed is not None and abs(elapsed - (moment - seen)) <= room

        def since(own: float, seen: float) -> bool:
            """Counted from when the index saw it come, or from its own time."""
            return elapsed is not None and (
                moment - seen - room <= elapsed <= moment - min(own, seen) + room
            )

        if key == "asks":
            since = epoch(status["since"])
            own = epoch(state.ask_at)
            return (
                words == (state.ask or "")[:SHOWN]
                and since is not None
                and own is not None
                and abs(since - own) < 0.01
                and near(state.ask_seen)
            )
        stopped = state.turn == "ended" and state.ended in {"stopped", "failed"}
        if key in {"your_turn", "idle", "stopped"} and stopped:
            return words == (STOPPED if state.ended == "stopped" else state.words)[:SHOWN] and (
                key == "stopped" or since(state.ended_own, state.ended_seen)
            )
        if key in {"your_turn", "idle", "finished"} and state.ended == "answered":
            return words == state.words[:SHOWN] and (
                key == "finished" or since(state.ended_own, state.ended_seen)
            )
        if key == "no_progress":
            return state.step in words and near(truth.last_growth(moment - age))
        return True

    def expect(self, truth: Truth, key: str | None, moment: float):
        """A change of status an open session's log just made: measured when it is shown."""
        session_id = truth.session_id
        if (before := self.expecting.get(session_id)) and not before.done and before.counted:
            lasted = moment - before.at
            self.missed.append((truth.label, before.key, lasted))
            if lasted > 2 * FRESH_TARGET:
                self.problem(f"{truth.label}: {before.key} for {lasted:.1f}s, never shown")
        if key is None:
            self.expecting.pop(session_id, None)
            return
        row = self.rows.get(session_id)
        counted = bool(
            row and row["open"] and truth.is_open and row["reason"] not in NOT_OWNERS
        )  # fmt: skip
        self.expecting[session_id] = Expect(key, moment, counted)

    def measure(self, truth: Truth, row: dict, moment: float):
        session_id = truth.session_id
        status = row["status"]
        expect = self.expecting.get(session_id)
        if expect and not expect.done and status and status["key"] == expect.key:
            expect.done = True
            if expect.counted:
                self.fresh.append((truth.label, expect.key, moment - expect.at))
        if status and status["key"] == "no_progress":
            began = truth.last_growth(moment) + STALL
            if not self.stalled or self.stalled[-1] < 0:
                self.stalled.append(moment - began)
        elif self.stalled and self.stalled[-1] >= 0:
            self.stalled.append(-1)  # Ended: the next one is another.
        if status and status["key"] == "idle" and truth.label not in self.idled:
            ended = min(truth.state.ended_seen, truth.state.ended_own)
            self.idled[truth.label] = moment - ended - IDLE
        index = self.opening.get(session_id, 1)
        moves = truth.opened[index:]
        if moves:
            changed, is_open = moves[-1]
            if row["open"] == is_open:
                if len(moves) == 1:
                    self.found.append((truth.label, "opened" if is_open else "closed",
                                       moment - changed))  # fmt: skip
                self.opening[session_id] = len(truth.opened)

    async def until(self, what: str, session_id: str, check, timeout: float) -> dict:
        label = self.ctx.label(session_id)
        log(f"waiting: {label}: {what}")
        began = wall()
        while wall() - began < timeout:
            row = self.shown(session_id)
            if check(row):
                log(f"ok: {label}: {what} ({wall() - began:.1f}s)")
                return row
            await asyncio.sleep(0.1)
        made = workspace(self.ctx.home, session_id)
        raise AssertionError(f"{label}: {what}: not within {timeout:.0f}s; the row: "
                             f"{self.shown(session_id)}; made by "
                             f"{made.get('client_name')!r}, last run by "
                             f"{made.get('last_client_name')!r}")  # fmt: skip

    def assert_right(self, scenario: str):
        assert not self.wrong, f"{scenario}: {len(self.wrong)} wrong: {self.wrong[:5]}"


def key_of(row: dict | None) -> str | None:
    return (row["status"] or {}).get("key") if row else None


def words_of(row: dict | None) -> str:
    return (row["status"] or {}).get("words", "") if row else ""


# The programs


class Screen(pyte.Screen):
    """A terminal's screen that answers what the CLI asks of it (pyte cannot answer a private
    status request: it is let be)."""

    def __init__(self, terminal: "Terminal", columns: int, lines: int):
        super().__init__(columns, lines)
        self.terminal = terminal

    def report_device_status(self, mode: int, **kwargs):
        if not kwargs.get("private"):
            super().report_device_status(mode)

    def write_process_input(self, data: str):
        with suppress(Exception):
            self.terminal.child.send(data)


class Terminal:
    """A real Copilot CLI in a pseudo-terminal, as the owner runs one."""

    def __init__(
        self,
        ctx: "Ctx",
        name: str,
        folder: Path,
        session_id: str,
        arguments: list,
        env: dict | None = None,
    ):
        self.name, self.folder, self.session_id = name, folder, session_id
        self.screen = Screen(self, 120, 40)
        self.stream = pyte.ByteStream(self.screen)
        self.guard = threading.Lock()
        self.pid = 0  # The CLI's own process: its lock holds the session.
        truth = ctx.truths.get(session_id)
        self.resumes = truth.state.resumes if truth else 0  # Resumes in its log before it ran.
        env = {**os.environ, "COPILOT_HOME": str(ctx.home), "TERM": "xterm-256color", **(env or {})}
        self.child = pexpect.spawn(
            COPILOT, arguments, cwd=str(folder), env=env, dimensions=(40, 120)
        )
        ctx.terminals.append(self)
        threading.Thread(target=self.pump, daemon=True).start()

    def pump(self):
        while True:
            try:
                data = self.child.read_nonblocking(65536, timeout=0.2)
            except pexpect.TIMEOUT:
                continue
            except Exception:
                return
            with self.guard, suppress(Exception):
                self.stream.feed(data)

    def text(self) -> str:
        with self.guard:
            return "\n".join(line.rstrip() for line in self.screen.display if line.strip())

    async def started(self, home: Path) -> int:
        """The CLI's own process, once it holds the session (the one its lock names)."""
        folder = home / "session-state" / self.session_id
        for _ in range(300):
            mine = await asyncio.to_thread(tree_of, self.child.pid)
            for lock in folder.glob("inuse.*.lock"):
                pid = int(lock.name.split(".")[1])
                if pid in mine:
                    self.pid = pid
                    return pid
            await asyncio.sleep(0.2)
        raise NotTheProductError(f"{self.name}: the CLI did not open its session:\n{self.text()}")

    async def resumed(self, ctx: "Ctx", truth: "Truth", text: str):
        """A resumed CLI takes its session (its lock) with the first message typed in it, once
        its prompt is drawn; its resume is in the log by then (at once after a clean exit, with
        that message after a kill)."""
        for _ in range(600):
            if "/ commands" in self.text():
                break
            await asyncio.sleep(0.1)
        else:
            raise NotTheProductError(f"{self.name}: the CLI did not get ready:\n{self.text()}")
        await asyncio.sleep(1)
        await self.say(truth, text)
        await self.started(ctx.home)
        await truth.until("its log says it was resumed", lambda s: s.resumes > self.resumes, 10)

    async def type(self, text: str):
        self.child.send(text)
        await asyncio.sleep(0.5)
        self.child.send("\r")

    async def say(self, truth: Truth, text: str):
        """Types a message and waits for the log to have it (typed again once if not)."""
        for _ in range(2):
            count = truth.state.messages
            await self.type(text)
            with suppress(NotTheProductError):
                await truth.until(
                    "the message is in its log", lambda s, count=count: s.messages > count, 20
                )
                return
        raise NotTheProductError(
            f"{self.name}: what was typed did not reach the CLI:\n{self.text()}"
        )

    async def choose(self, truth: Truth, choice: str):
        """Answers its question form in the terminal: the option `choice`, else typed."""
        for _ in range(100):
            if "needs information" in self.text():
                break
            await asyncio.sleep(0.1)
        lines = self.text().split("\n")
        form = next((n for n, line in enumerate(lines) if "needs information" in line), None)
        options = lines[form:] if form is not None else []
        at = next((n for n, line in enumerate(options) if line.lstrip().startswith(CHOSEN)), None)
        target = next(
            (n for n, line in enumerate(options) if line.strip().lstrip(CHOSEN).strip() == choice),
            None,
        )
        if at is not None and target is not None:
            for _ in range(abs(target - at)):
                self.child.send("\x1b[B" if target > at else "\x1b[A")
                await asyncio.sleep(0.2)
            self.child.send("\r")
        else:
            await self.type(choice)
        with suppress(NotTheProductError):
            await truth.until("the question is answered", lambda s: s.ask is None, 30)
            return
        raise NotTheProductError(f"{self.name}: could not answer its question:\n{self.text()}")

    async def interrupt(self, truth: "Truth"):
        """Stops its turn as the owner does: Esc, twice (once does not, on CLI 1.0.109)."""
        self.child.send("\x1b")
        await asyncio.sleep(0.4)
        self.child.send("\x1b")
        await truth.until(
            "its log says the owner stopped it",
            lambda s: s.turn == "ended" and s.ended == "stopped",
            15,
        )

    def stop9(self) -> float:
        """Killed outright, as a crash or a closed laptop lid would."""
        os.kill(self.pid, signal.SIGKILL)
        return wall()

    async def exit(self):
        for _ in range(2):
            await self.type("/exit")
            for _ in range(300):
                if not self.child.isalive():
                    return
                await asyncio.sleep(0.1)
        raise NotTheProductError(f"{self.name}: /exit did not end the CLI:\n{self.text()}")

    def close(self):
        with suppress(Exception):
            self.child.close(force=True)
        if self.pid and alive(self.pid) and "copilot" in command_of(self.pid):
            with suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGKILL)


class Instance(App):
    """A Chief of Staff on a port and in a data directory of its own (live_e2e.App runs on
    its module's PORT: set while it starts)."""

    def __init__(self, name: str, port: int, data: Path, logfile: Path, env: dict):
        super().__init__(data, logfile, env)
        self.name, self.port = name, port
        self.base = f"http://127.0.0.1:{port}"

    async def start(self):
        live_e2e.PORT, live_e2e.BASE = self.port, self.base
        await super().start()

        async def ready():
            state = await self.get("/api/state")
            return state if state["runtime"]["state"] == "ready" else None

        await live_e2e.wait_for(f"{self.name} connects to Copilot", ready, 120)
        await self.post(
            "/api/settings", {"model": MODEL, "mac_notifications": False, "digest_time": ""}
        )

    async def stop(self):
        with suppress(Exception):
            await super().stop()
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.wait(10)

    def ours(self) -> set[str]:
        """Every Copilot session this instance has (rule 1 of section 4.2)."""
        database = self.data / "chiefofstaff.db"
        if not database.exists():
            return set()
        with closing(sqlite3.connect(database, timeout=5)) as db:
            ids = {row[0] for row in db.execute("SELECT id FROM sessions")}
            for key, value in db.execute(
                "SELECT key, value FROM kv WHERE key LIKE 'alias:%' OR key LIKE 'copilot:%'"
            ):
                ids.add(key.removeprefix("alias:") if key.startswith("alias:") else value)
        return ids

    def made(self) -> dict[str, dict]:
        """Who made the sessions it found a worker's or a script's (provenance.py), as kept."""
        with closing(sqlite3.connect(self.data / "chiefofstaff.db", timeout=5)) as db:
            rows = db.execute("SELECT key, value FROM kv WHERE key LIKE 'made:%'").fetchall()
        return {key.removeprefix("made:"): json.loads(value) for key, value in rows}

    async def router(self) -> str:
        """Its Router's Copilot session."""
        row = (await self.get("/api/state"))["router_id"]
        with closing(sqlite3.connect(self.data / "chiefofstaff.db", timeout=5)) as db:
            found = db.execute("SELECT value FROM kv WHERE key=?", (f"copilot:{row}",)).fetchone()
        return found[0] if found else row

    async def reply(self, session_id: str, text: str, timeout: float = 240) -> str:
        """Says something in one of its sessions; its answer."""
        sent = await self.post(f"/api/sessions/{session_id}/messages", {"text": text})
        return await self.answer(session_id, sent["id"], timeout)

    async def answer(self, session_id: str, mark: int, timeout: float = 240) -> str:
        async def answered():
            messages = await self.get(f"/api/sessions/{session_id}/messages?after={mark}")
            said = [m["text"] for m in messages if m["kind"] == "assistant"]
            return said[-1] if said else None

        return await live_e2e.wait_for(f"{self.name} answers", answered, timeout)


@dataclass
class Ctx:
    root: Path
    home: Path
    app: Instance
    other: Instance
    watch: Watch = None  # type: ignore[assignment]
    truths: dict[str, Truth] = field(default_factory=dict)
    terminals: list[Terminal] = field(default_factory=list)
    helpers: list[subprocess.Popen] = field(default_factory=list)
    closers: list = field(default_factory=list)
    banned: set[str] = field(default_factory=set)  # Never listed, besides this one's own.
    own: dict[str, float] = field(default_factory=dict)  # This one's sessions: since when.
    labels: dict[str, str] = field(default_factory=dict)
    excluded: set[int] = field(default_factory=set)

    def label(self, session_id: str) -> str:
        return self.labels.get(session_id, session_id[:8])

    def forbidden(self, session_id: str, moment: float) -> bool:
        """Whether a session must not be listed: this Chief of Staff's (rule 1) since longer
        than a pass and this test's reading of its store, or another's (rule 4) for now."""
        return session_id in self.banned or moment - self.own.get(session_id, math.inf) > OWN_LAG

    def track(self, session_id: str, label: str) -> Truth:
        self.labels[session_id] = label
        truth = self.truths[session_id] = Truth(self, session_id, label)
        truth.poll(wall(), self.excluded)
        return truth

    def folder(self, name: str) -> Path:
        return self.root / name

    async def follow(self):
        """Reads the tracked logs every 0.1 s, and which sessions this Chief of Staff has."""
        refreshed = 0.0
        while True:
            now = wall()
            if now - refreshed > 2:
                refreshed = now
                with suppress(Exception):
                    self.excluded = await asyncio.to_thread(tree_of, self.app.process.pid)
                    for session_id in await asyncio.to_thread(self.app.ours):
                        self.own.setdefault(session_id, now)
            for truth in list(self.truths.values()):
                with suppress(Exception):
                    truth.poll(wall(), self.excluded)
            await asyncio.sleep(0.1)


# O1: a real terminal session


async def asked(ctx: Ctx, term: Terminal, truth: Truth):
    """Waits for its ask_user question; asks again once when the model answered without it."""
    for attempt in (1, 2):
        count = truth.state.answered
        state = await truth.until(
            "it asks through ask_user (or answers without)",
            lambda s, count=count: s.ask is not None or s.answered > count,
            120,
        )
        if state.ask is not None:
            return
        if attempt == 1:
            log(f"{truth.label}: the model answered without ask_user; telling it again")
            await term.say(truth, ASK_AGAIN)
    raise NotTheProductError(f"{truth.label}: the model did not use ask_user, twice")


async def slept(ctx: Ctx, term: Terminal, truth: Truth, told: bool = False):
    """Has it run `sleep` (`told`: its first prompt said so already); tells it again once when
    the model ran something else."""
    for attempt in (1, 2):
        count = truth.state.answered
        if attempt == 2 or not told:
            await term.say(truth, SLEEPY)
        state = await truth.until(
            f"it runs `sleep {SLEEP}` (or answers without)",
            lambda s, count=count: f"sleep {SLEEP}" in s.step or s.answered > count,
            120,
        )
        if f"sleep {SLEEP}" in state.step and "(async)" not in state.step:
            return
        if attempt == 1:
            log(f"{truth.label}: the model did not run the command as told; telling it again")
            await truth.until("its turn ends", lambda s, count=count: s.answered > count, 180)
    raise NotTheProductError(f"{truth.label}: the model did not run `sleep {SLEEP}`, twice")


async def o1_terminal(ctx: Ctx) -> tuple[str, str]:
    watch = ctx.watch
    # a. It asks the owner, through ask_user.
    first = str(uuid.uuid4())
    folder = ctx.folder("terminal-a")
    truth = ctx.track(first, "A")
    term = Terminal(ctx, "A", folder, first, ["--session-id", first, *CLI, "-i", ASK])
    await term.started(ctx.home)
    await asked(ctx, term, truth)
    question = truth.state.ask or ""
    row = await watch.until("asks you, in the sidebar", first, lambda r: key_of(r) == "asks", 30)
    assert row["open"] and words_of(row) == question[:SHOWN], row
    log(f"O1a passed: open, asks {words_of(row)!r} since {row['status']['since']}")

    # b. Answered in the terminal: it works on, then it is the owner's turn.
    answered = wall()
    await term.choose(truth, "9090")
    state = await truth.until("its turn ends", lambda s: s.turn == "ended", 120)
    row = await watch.until(
        "your turn, with its answer",
        first,
        lambda r: key_of(r) == "your_turn" and words_of(r) == state.words[:SHOWN],
        30,
    )
    assert (folder / "port.txt").read_text().strip() == "9090", "the answer reached the CLI"
    worked = [e for e in watch.timeline[first] if e[0] >= answered and e[2] == "working"]
    log(f"O1b passed: your turn {words_of(row)!r} (working shown in between: {bool(worked)})")

    # c. A command that writes nothing for longer than the threshold: no progress, naming it.
    await slept(ctx, term, truth)
    row = await watch.until(
        "working on the command", first, lambda r: key_of(r) in {"working", "no_progress"}, 30
    )
    row = await watch.until(
        f"no progress after {STALL}s, naming the step",
        first,
        lambda r: key_of(r) == "no_progress",
        STALL + 20,
    )
    assert f"sleep {SLEEP}" in words_of(row), row
    log(f"no progress: {words_of(row)!r}, for {row['status']['elapsed']}s")
    state = await truth.until("the command and the turn end", lambda s: s.turn == "ended", 120)
    await watch.until(
        "your turn again, with its answer",
        first,
        lambda r: key_of(r) == "your_turn" and words_of(r) == state.words[:SHOWN],
        30,
    )
    log(f"O1c passed: working, no progress on {words_of(row)!r}, then your turn")

    # d. It exits: finished, and closed.
    await term.exit()
    row = await watch.until(
        "closed, finished", first, lambda r: not r["open"] and key_of(r) == "finished", 40
    )
    assert words_of(row) == state.words[:SHOWN], row
    log(f"O1d passed: closed, finished {words_of(row)!r}")

    # e. Killed while it asks, then resumed in a terminal.
    second = str(uuid.uuid4())
    folder = ctx.folder("terminal-b")
    truth = ctx.track(second, "B")
    term = Terminal(ctx, "B", folder, second, ["--session-id", second, *CLI, "-i", ASK])
    await term.started(ctx.home)
    await asked(ctx, term, truth)
    await watch.until("asks you", second, lambda r: key_of(r) == "asks", 30)
    term.stop9()
    log(f"killed B's CLI (process {term.pid}) while it asks")
    row = await watch.until("closed", second, lambda r: not r["open"], FOUND_MOST + 15)
    row = await watch.until(
        "stopped (or no status)", second, lambda r: key_of(r) in {"stopped", None}, 10
    )
    await asyncio.sleep(5)  # Checked all along: never a status it does not have.
    log(f"after the kill: {key_of(watch.shown(second))} ({watch.shown(second)['reason']})")
    term.close()
    term = Terminal(ctx, "B again", folder, second, ["--resume", second, *CLI])
    await term.resumed(ctx, truth, OK)
    await watch.until("open again", second, lambda r: r["open"], FOUND_MOST + 15)
    state = await truth.until("its turn ends", lambda s: s.turn == "ended", 120)
    await watch.until(
        "your turn, with its answer",
        second,
        lambda r: key_of(r) == "your_turn" and words_of(r) == state.words[:SHOWN],
        30,
    )
    await term.exit()
    await watch.until(
        "closed, finished", second, lambda r: not r["open"] and key_of(r) == "finished", 40
    )
    log("O1e passed: asks, killed (closed, then stopped), resumed (open), your turn, finished")
    watch.assert_right("O1")
    log("O1 passed")
    return first, second


# O5: stopped by the owner, idle, failed


class Provider:
    """A model provider of the test's own (the CLI's COPILOT_PROVIDER_* variables) that refuses
    every request, as one does a model it does not have: the CLI's turn fails with a session
    error, at once and without a model call."""

    def __init__(self):
        self.requests = 0
        outer = self

        class Refuse(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.requests += 1
                self.rfile.read(int(self.headers.get("content-length") or 0))
                error = {"message": "The model `no-such-model` does not exist",
                         "type": "invalid_request_error", "code": "model_not_found"}  # fmt: skip
                body = json.dumps({"error": error}).encode()
                self.send_response(400)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *arguments):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", PROVIDER_PORT), Refuse)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.env = {
            "COPILOT_PROVIDER_BASE_URL": f"http://127.0.0.1:{PROVIDER_PORT}/v1",
            "COPILOT_PROVIDER_API_KEY": "not-a-key",
            "COPILOT_MODEL": "no-such-model",
        }

    def close(self):
        self.server.shutdown()
        self.server.server_close()


async def o5_stopped_idle_failed(ctx: Ctx):
    watch = ctx.watch
    # a. Stopped with Esc in the middle of a step: the owner's turn, "Stopped by you".
    session_id = str(uuid.uuid4())
    truth = ctx.track(session_id, "E")
    term = Terminal(
        ctx, "E", ctx.folder("terminal-e"), session_id,
        ["--session-id", session_id, *CLI, "-i", SLEEPY],
    )  # fmt: skip
    await term.started(ctx.home)
    await slept(ctx, term, truth, told=True)
    await watch.until(
        f"working on `sleep {SLEEP}`",
        session_id,
        lambda r: key_of(r) in {"working", "no_progress"} and f"sleep {SLEEP}" in words_of(r),
        STALL + 15,
    )
    await asyncio.sleep(2)  # In the middle of the step.
    await term.interrupt(truth)
    row = await watch.until(
        f"your turn, {STOPPED!r}",
        session_id,
        lambda r: r and r["open"] and key_of(r) == "your_turn" and words_of(r) == STOPPED,
        30,
    )
    log(f"O5a passed: stopped with Esc during `sleep {SLEEP}`: {key_of(row)} {words_of(row)!r}")

    # b. Idle once its turn ended IDLE seconds ago (an hour in use).
    row = await watch.until(
        f"idle after {IDLE}s",
        session_id,
        lambda r: r and r["open"] and key_of(r) == "idle" and words_of(r) == STOPPED,
        IDLE + 20,
    )
    await term.exit()
    await watch.until(
        f"closed, stopped, {STOPPED!r}",
        session_id,
        lambda r: r and not r["open"] and key_of(r) == "stopped" and words_of(r) == STOPPED,
        FOUND_MOST + 15,
    )
    log(f"O5b passed: idle {watch.idled.get('E', math.nan):.1f}s after the threshold; closed: "
        "stopped")  # fmt: skip

    # c. Its turn fails (a session error): the owner's turn with the error, stopped when closed.
    provider = Provider()
    ctx.closers.append(provider.close)
    session_id = str(uuid.uuid4())
    truth = ctx.track(session_id, "X")
    term = Terminal(
        ctx, "X", ctx.folder("terminal-x"), session_id,
        ["--session-id", session_id, "--allow-all", "-i", OK],
        env=provider.env,
    )  # fmt: skip
    await term.started(ctx.home)
    state = await truth.until("its turn fails", lambda s: s.ended == "failed", 60)
    assert provider.requests, "the error came from the test's provider"
    row = await watch.until(
        "your turn, with the error",
        session_id,
        lambda r: r and r["open"] and key_of(r) == "your_turn" and words_of(r) == state.words,
        FOUND_MOST + 15,
    )
    await term.exit()
    await watch.until(
        "closed, stopped, with the error",
        session_id,
        lambda r: r and not r["open"] and key_of(r) == "stopped" and words_of(r) == state.words,
        FOUND_MOST + 15,
    )
    provider.close()
    log(f"O5c passed: a session error: your turn, then stopped: {words_of(row)!r}")
    watch.assert_right("O5")
    log("O5 passed")


# O3: nothing in the home changed


async def o3_untouched(ctx: Ctx):
    listing = await ctx.app.get("/api/others")
    ids = [row["id"] for row in listing["rows"]]
    assert ids and all(not row["open"] for row in listing["rows"]), listing
    before = snapshot(ctx.home, ids)
    first = listing["pass"]
    for _ in range(2):
        # Off then on: a fresh listing and first looks of every log.
        await ctx.app.post("/api/settings", {"other_sessions": False})
        assert (await ctx.app.get("/api/others"))["rows"] == []
        await ctx.app.post("/api/settings", {"other_sessions": True})

        async def looked():
            rows = (await ctx.app.get("/api/others"))["rows"]
            return {row["id"] for row in rows if row["status"]} >= set(ids)

        await live_e2e.wait_for("every session has its status again", looked, 30)
    await asyncio.sleep(3 * 2.5)
    for session_id in ids:
        assert (await ctx.app.get(f"/api/others/{session_id}"))["id"] == session_id
    passes = (await ctx.app.get("/api/others"))["pass"] - first
    after = snapshot(ctx.home, ids)
    changed = sorted(set(before.items()) ^ set(after.items()))
    assert before == after, f"the index changed the Copilot home: {changed[:10]}"
    log(f"O3 passed: {len(before)} files and folders of {len(ids)} sessions unchanged after "
        f"{passes} passes, two fresh listings and first looks")  # fmt: skip


# O2: whose session it is


async def o2_provenance(ctx: Ctx, first: str) -> str:
    watch, other = ctx.watch, ctx.other
    # b. A second Chief of Staff, same Copilot home: its sessions are left out.
    await other.start()
    said = await other.reply((await other.get("/api/state"))["router_id"], OK)
    router = await other.router()
    ctx.labels[router] = "the other's Router"
    ctx.banned |= {router}
    log(f"the other Chief of Staff's Router said {said!r}")
    await asyncio.sleep(FOUND_TARGET)  # Its CLI opened the session: listed again since.
    response = await ctx.app.http.get(f"/api/others/{router}")
    assert response.status_code == 404, response.text
    external = await ctx.app.get("/api/external?refresh=true")
    assert router not in {item["id"] for item in external}
    made = workspace(ctx.home, router)
    assert made.get("client_name") == "chiefofstaff", made
    watch.assert_right("O2b")
    log(f"O2b passed: the other's Router ({made.get('client_name')}) is not listed, and 404")

    # c. Another app that names itself (fake_terminal.py, through the SDK): the owner's.
    folder = ctx.folder("fake-terminal")
    helper = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).parent / "fake_terminal.py"),
            str(folder),
            OK,
            "acme-notes",
        ],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "COPILOT_HOME": str(ctx.home)},
        start_new_session=True,
    )
    ctx.helpers.append(helper)
    line = await asyncio.wait_for(asyncio.to_thread(helper.stdout.readline), 240)
    assert line.startswith("READY"), line
    fake = line.split()[1]
    ctx.track(fake, "F")
    client = workspace(ctx.home, fake).get("client_name")
    if client:
        row = await watch.until(
            "open, your turn",
            fake,
            lambda r: r and r["open"] and key_of(r) == "your_turn",
            FOUND_MOST + 15,
        )
    else:
        row = await watch.until(
            "open, no status: no maker named",
            fake,
            lambda r: r and r["open"] and r["reason"] == "unknown",
            FOUND_MOST + 15,
        )
    log(f"O2c passed: another app ({client or 'no client named'}): {key_of(row)} "
        f"{words_of(row)!r}")  # fmt: skip

    # d. The terminal session A, continued by the other Chief of Staff: rule 5.
    response = await other.http.post(f"/api/external/{first}/messages", json={"text": OK})
    assert response.status_code == 200, response.text
    said = await other.answer(first, response.json()["id"])
    log(f"the other Chief of Staff continued A: {said!r}")
    await other.stop()
    made = workspace(ctx.home, first)
    assert made.get("last_client_name") == "chiefofstaff", made
    row = await watch.until(
        "closed, no status: another Chief of Staff ran it last",
        first,
        lambda r: r and not r["open"] and r["reason"] == "another" and not r["status"],
        FOUND_MOST + 15,
    )
    log(f"O2d passed: {made.get('client_name')} → {made.get('last_client_name')}: "
        f"{row['why']!r}")  # fmt: skip

    # e. Run in a terminal again: rule 6, its status back.
    truth = ctx.truths[first]
    term = Terminal(ctx, "A again", ctx.folder("terminal-a"), first, ["--resume", first, *CLI])
    await term.resumed(ctx, truth, OK)
    await watch.until("open", first, lambda r: r and r["open"], FOUND_MOST + 15)
    state = await truth.until("its turn ends", lambda s: s.turn == "ended", 120)
    await term.exit()
    made = workspace(ctx.home, first)
    assert made.get("last_client_name") not in {"chiefofstaff", "work-buddy"}, made
    row = await watch.until(
        "closed, finished: the owner's again",
        first,
        lambda r: r and not r["open"] and key_of(r) == "finished",
        FOUND_MOST + 15,
    )
    assert words_of(row) == state.words[:SHOWN], row
    log(f"O2e passed: last run by {made.get('last_client_name')}: finished {words_of(row)!r}")

    # f. The other's Router, taken over in a terminal: rule 6.
    truth = ctx.track(router, "the other's Router")
    term = Terminal(ctx, "R", ctx.folder("terminal-r"), router, ["--resume", router, *CLI])
    ctx.banned.discard(router)  # Once its terminal runs a turn, it is the owner's.
    await term.resumed(ctx, truth, OK)
    state = await truth.until("its turn ends", lambda s: s.turn == "ended", 120)
    await term.exit()
    made = workspace(ctx.home, router)
    row = await watch.until(
        "listed, closed, finished",
        router,
        lambda r: r and not r["open"] and key_of(r) == "finished",
        FOUND_MOST + 15,
    )
    assert words_of(row) == state.words[:SHOWN], row
    log(f"O2f passed: {made.get('client_name')} → {made.get('last_client_name')}: finished "
        f"{words_of(row)!r}")  # fmt: skip

    # g. Run by the other Chief of Staff again: rule 4, left out.
    await other.start()
    assert await other.router() == router, "the other kept its Router"
    said = await other.reply((await other.get("/api/state"))["router_id"], OK)
    log(f"the other Chief of Staff's Router said {said!r}")
    await other.stop()
    stopped = wall()
    made = workspace(ctx.home, router)
    assert made.get("last_client_name") == "chiefofstaff", made
    await watch.until("left out again (at a listing)", router, lambda r: r is None, LISTING)
    ctx.banned.add(router)
    log(f"O2g passed: {made.get('client_name')} → {made.get('last_client_name')}: left out "
        f"{wall() - stopped:.0f}s after the other stopped")  # fmt: skip

    # h. A script's session, through the SDK naming no app (one of a batch job's many): never
    # listed, open or closed; continued in a terminal, the owner's (rule 6).
    script = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "fake_terminal.py"), str(ctx.folder("script")),
         OK, ""],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "COPILOT_HOME": str(ctx.home)},
        start_new_session=True,
    )  # fmt: skip
    ctx.helpers.append(script)
    line = await asyncio.wait_for(asyncio.to_thread(script.stdout.readline), 240)
    assert line.startswith("READY"), line
    batch = line.split()[1]
    ctx.banned.add(batch)
    made = workspace(ctx.home, batch)
    assert made.get("client_name") == "sdk", made
    for state in ("open", "closed"):
        await asyncio.sleep(FOUND_TARGET)  # Its opening (or closing) brought a listing.
        response = await ctx.app.http.get(f"/api/others/{batch}")
        assert response.status_code == 404, (state, response.text)
        external = await ctx.app.get("/api/external?refresh=true")
        assert batch not in {item["id"] for item in external}, state
        if state == "open":
            os.killpg(script.pid, signal.SIGTERM)
            script.wait(30)
            ctx.helpers.remove(script)  # O4 takes the other app's, the last one.
    truth = ctx.track(batch, "the script's")
    term = Terminal(ctx, "S", ctx.folder("terminal-s"), batch, ["--resume", batch, *CLI])
    ctx.banned.discard(batch)  # Once its terminal runs a turn, it is the owner's.
    await term.resumed(ctx, truth, OK)
    state = await truth.until("its turn ends", lambda s: s.turn == "ended", 120)
    await term.exit()
    exited = wall()
    made = workspace(ctx.home, batch)
    assert made.get("last_client_name") == "github/cli", made
    # Whose it is follows Copilot's listing (as for g).
    row = await watch.until(
        "listed, closed, finished (at a listing)",
        batch,
        lambda r: r and not r["open"] and key_of(r) == "finished",
        LISTING,
    )
    assert words_of(row) == state.words[:SHOWN], row
    log(f"O2h passed: a script's session ({made.get('client_name')}) never listed, open or "
        f"closed; continued in a terminal ({made.get('last_client_name')}): listed "
        f"{wall() - exited:.0f}s after it exited, finished {words_of(row)!r}")  # fmt: skip
    watch.assert_right("O2")
    log(f"O2a passed: this Chief of Staff's {len(ctx.own)} sessions never listed in "
        f"{watch.observations} rows read")  # fmt: skip
    log("O2 passed")
    return fake


# O4: continuing one here


async def o4_continue(ctx: Ctx, closed: str, opened: str):
    watch, app = ctx.watch, ctx.app
    # b. Open in another program: refused, as today.
    response = await app.http.post(f"/api/external/{opened}/messages", json={"text": OK})
    detail = response.json().get("detail", "")
    assert response.status_code == 400 and "open in another Copilot window" in detail, detail
    assert opened not in app.ours()
    assert watch.shown(opened)["open"]
    log(f"O4b passed: refused: {detail!r}")

    # a. Closed: continued here; it is this Chief of Staff's from then on, not listed: from its
    # next pass (rule 1), not its next listing. Nothing opens or closes meanwhile: what did
    # before is listed by now (a lock scan, then a listing).
    await asyncio.sleep(FOUND_TARGET)
    began = wall()
    response = await app.http.post(f"/api/external/{closed}/messages", json={"text": OK})
    assert response.status_code == 200, response.text
    assert closed in app.ours()
    await watch.until(
        "left out: this Chief of Staff's now", closed, lambda r: r is None, OWN_LAG + 5
    )
    gone = wall() - began
    said = await app.answer(closed, response.json()["id"])
    external = await app.get("/api/external?refresh=true")
    assert closed not in {item["id"] for item in external}
    log(f"O4a passed: continued here ({said!r}); out of the list {gone:.1f}s after")

    # The other app closes: closed, finished.
    helper = ctx.helpers[-1]
    os.killpg(helper.pid, signal.SIGTERM)
    helper.wait(30)
    await watch.until(
        "closed, finished", opened, lambda r: not r["open"] and key_of(r) == "finished", 40
    )
    watch.assert_right("O4")
    log("O4 passed")


# O6: a script's `copilot -p` sessions


def sessions_in(home: Path) -> set[str]:
    return {path.name for path in (home / "session-state").iterdir() if path.is_dir()}


async def prompt_mode(ctx: Ctx, folder: Path, prompt: str, *extra: str) -> str:
    """One `copilot -p`, as a script runs it (QA's: no tools, quiet): its session, once ended."""
    before = sessions_in(ctx.home)
    done = await asyncio.to_thread(
        subprocess.run,
        [
            COPILOT,
            "-p",
            prompt,
            "-s",
            "--no-color",
            "--available-tools",
            "",
            "--model",
            MODEL,
            *extra,
        ],
        cwd=str(folder),
        env={**os.environ, "COPILOT_HOME": str(ctx.home)},
        capture_output=True,
        text=True,
        timeout=240,
    )
    made = [
        key
        for key in sessions_in(ctx.home) - before
        if workspace(ctx.home, key).get("client_name") == "github/cli"
    ]
    if done.returncode or len(made) != 1:
        raise NotTheProductError(
            f"`copilot -p` exited {done.returncode} with sessions {made}: "
            f"{(done.stderr or done.stdout)[-400:]}"
        )
    return made[0]


def tidy(ctx: Ctx, *arguments: str) -> subprocess.CompletedProcess:
    """`chiefofstaff tidy-sessions` in the owner's terminal, for this Chief of Staff and home."""
    done = subprocess.run(
        [sys.executable, "-m", "chiefofstaff", "tidy-sessions", "--data-dir", str(ctx.app.data),
         *arguments],
        env={**os.environ, "COPILOT_HOME": str(ctx.home)},
        capture_output=True,
        text=True,
        timeout=180,
    )  # fmt: skip
    log(f"tidy-sessions {' '.join(arguments)} (exit {done.returncode}):\n{done.stdout}"
        f"{done.stderr}")  # fmt: skip
    return done


async def listed_by_copilot(ctx: Ctx) -> set[str]:
    """The sessions Copilot itself lists in the home (its own CLI, as Chief of Staff asks it)."""
    os.environ["COPILOT_HOME"] = str(ctx.home)
    try:
        backend = copilot_backend("copilot")
        await backend.start()
        try:
            return {meta.session_id for meta in await backend.list_sessions()}
        finally:
            await backend.stop()
    finally:
        os.environ.pop("COPILOT_HOME", None)


def observe(ctx: Ctx, session_id: str, learned: bool = False) -> int:
    """An observation read from a session as the owner's words (as habits.collect_sessions
    adds one), in this Chief of Staff's store."""
    with closing(sqlite3.connect(ctx.app.data / "chiefofstaff.db", timeout=5)) as db, db:
        return (
            db.execute(
                "INSERT INTO observations(source, kind, ref, text, learned, created_at) VALUES "
                "('copilot', 'instruction', ?, ?, ?, 'now')",
                (f"copilot:{session_id}:e{int(learned)}", LABEL, int(learned)),
            ).lastrowid
            or 0
        )


def observed(ctx: Ctx) -> dict[str, int]:
    with closing(sqlite3.connect(ctx.app.data / "chiefofstaff.db", timeout=5)) as db:
        rows = db.execute("SELECT ref, learned FROM observations WHERE source='copilot'")
        return dict(rows.fetchall())


async def o6_batch(ctx: Ctx):
    watch, app = ctx.watch, ctx.app
    # a. Five alike, one after the other: a batch, left out once they ended.
    folder = ctx.folder("batch")
    batch = []
    for ticket in TICKETS:
        batch.append(await prompt_mode(ctx, folder, LABEL + ticket))
        ctx.labels[batch[-1]] = f"batch {len(batch)}"
    shown_open = {key for key in batch if any(e[1] for e in watch.timeline.get(key, []))}
    log(f"the script's five sessions ended; shown open while they ran: {len(shown_open)}")
    for session_id in batch:
        await watch.until("left out: one of a batch", session_id, lambda r: r is None, LISTING)
    ctx.banned |= set(batch)  # Never listed from now on.
    for session_id in batch:
        response = await app.http.get(f"/api/others/{session_id}")
        assert response.status_code == 404, response.text
    external = {item["id"] for item in await app.get("/api/external?refresh=true")}
    assert external.isdisjoint(batch), "the Router's list has none of them"
    made = app.made()
    assert {key: made[key]["as"] for key in batch if key in made} == dict.fromkeys(batch, "batch")
    log(f"O6a passed: {len(batch)} `copilot -p` alike: left out of the rows and the Router's "
        f"list, kept as a batch ({made[batch[0]]['why']})")  # fmt: skip

    # One named as a batch's, as workers name theirs: left out on its own.
    named = await prompt_mode(ctx, folder, OK, "--name", "batch: Label the acme tickets")
    ctx.labels[named] = "named"
    await watch.until("left out: named a batch's", named, lambda r: r is None, LISTING)
    ctx.banned.add(named)
    assert (await app.http.get(f"/api/others/{named}")).status_code == 404
    assert named not in {item["id"] for item in await app.get("/api/external?refresh=true")}
    assert app.made()[named]["why"] == "named", app.made()[named]
    log('O6a passed: one `copilot -p --name "batch: …"` alone: left out, kept as named')

    # b. A single one of the owner's: theirs.
    one_off = await prompt_mode(ctx, ctx.folder("one-off"), OK)
    # Followed from when its row says it closed (it ended before this test knew its id).
    await watch.until("listed, closed", one_off, lambda r: r and not r["open"], LISTING)
    ctx.track(one_off, "P")
    row = await watch.until(
        "listed: closed, finished",
        one_off,
        lambda r: r and not r["open"] and key_of(r) == "finished",
        LISTING,
    )
    assert one_off in {item["id"] for item in await app.get("/api/external?refresh=true")}
    assert one_off not in app.made()
    log(f"O6b passed: a single `copilot -p`: the owner's, finished {words_of(row)!r}")

    # c. tidy-sessions: what it would move and drop, and what was learned; nothing changes,
    # and nothing is moved while new.
    observe(ctx, batch[0])
    observe(ctx, one_off)
    learned = observe(ctx, batch[1], learned=True)
    with closing(sqlite3.connect(app.data / "chiefofstaff.db", timeout=5)) as db, db:
        db.execute(
            "INSERT INTO style_versions(section, text, evidence, status, created_at) VALUES "
            "('chat', 'One word answers.', ?, 'active', 'now')",
            (json.dumps([learned]),),
        )
    with closing(sqlite3.connect(app.data / "chiefofstaff.db", timeout=5)) as db:
        # This Chief of Staff may have learned a style of its own meanwhile.
        (version,) = db.execute(
            "SELECT count(*) FROM style_versions WHERE section='chat' AND status!='undone'"
        ).fetchone()
    before, words = snapshot(ctx.home, [*batch, one_off]), observed(ctx)
    shown = tidy(ctx)
    assert shown.returncode == 0, shown.stderr
    for said in (
        "5 sessions of a script's batch (one-shot, alike)",
        "1 session named as a batch's",
        "Left where they are: 6 sessions changed in the last hour.",
        "1 prompt of theirs, read as yours and not learned from yet, would be dropped.",
        f"How you write, Answers and reports to you (version {version}): 1 of its 1 example",
    ):
        assert said in shown.stdout, said
    assert snapshot(ctx.home, [*batch, one_off]) == before and observed(ctx) == words
    log("O6c passed: tidy-sessions showed the batch, what it would drop and what was learned; "
        "nothing changed, nothing new moved")  # fmt: skip

    # d. Two hours old: moved, with a manifest; what was not learned from yet dropped; one
    # moved back is Copilot's again, and still left out.
    old = wall() - 2 * 3600
    for session_id in batch:
        for path in [ctx.home / "session-state" / session_id,
                     *(ctx.home / "session-state" / session_id).rglob("*")]:  # fmt: skip
            os.utime(path, (old, old), follow_symlinks=False)
    moved = tidy(ctx, "--apply")
    assert moved.returncode == 0 and "Moved 5 sessions" in moved.stdout, moved.stdout
    (folder_moved,) = (ctx.home / "session-state-moved-by-chiefofstaff").iterdir()
    manifest = json.loads((folder_moved / "manifest.json").read_text())
    assert sorted(entry["id"] for entry in manifest["sessions"]) == sorted(batch)
    assert sessions_in(ctx.home).isdisjoint(batch) and one_off in sessions_in(ctx.home)
    assert observed(ctx) == {
        key: value for key, value in words.items() if key != f"copilot:{batch[0]}:e0"
    }, "only the batch's prompt not learned yet is dropped"
    with closing(sqlite3.connect(app.data / "chiefofstaff.db", timeout=5)) as db:
        kept = db.execute("SELECT status FROM style_versions WHERE evidence=?", (f"[{learned}]",))
        assert kept.fetchall() == [("active",)], "what was learned stays"
    listed = await listed_by_copilot(ctx)
    assert listed.isdisjoint(batch) and one_off in listed
    (folder_moved / batch[-1]).rename(ctx.home / "session-state" / batch[-1])
    assert batch[-1] in await listed_by_copilot(ctx), "moved back: Copilot's again"
    await asyncio.sleep(LISTING)  # A listing has it again.
    response = await app.http.get(f"/api/others/{batch[-1]}")
    assert response.status_code == 404, response.text
    external = {item["id"] for item in await app.get("/api/external?refresh=true")}
    assert batch[-1] not in external
    watch.assert_right("O6")
    log("O6d passed: moved 5 with a manifest, dropped what was not learned yet, kept the rest; "
        "one moved back is listed by Copilot again and still left out")  # fmt: skip
    log("O6 passed")


def versions(home: Path) -> dict[str, int]:
    """The Copilot CLI versions that ran in the home (from their process logs)."""
    found: dict[str, int] = {}
    for path in (home / "logs").glob("process-*.log"):
        with suppress(OSError):
            for line in path.read_text(errors="replace").splitlines():
                if "Starting Copilot CLI: " in line:
                    version = line.rsplit(": ", 1)[1].strip()
                    found[version] = found.get(version, 0) + 1
    return found


def report(ctx: Ctx, began: float):
    watch = ctx.watch
    print("\n================ LIVE OTHERS REPORT ================")
    for session_id, line in watch.timeline.items():
        steps = " → ".join(
            f"{moment - began:.0f}s {'open' if is_open else 'closed'} {key or reason}"
            for moment, is_open, key, reason in line
        )
        print(f"{ctx.label(session_id)}: {steps}")
    print(f"rows read {watch.observations}, statuses checked {watch.statuses}: {watch.keys}")
    print(f"Copilot CLI versions run (processes): {versions(ctx.home)}")
    print(f"rows without a status, by reason: {watch.reasons}")
    fresh = [seconds for _, _, seconds in watch.fresh]
    found = [seconds for _, _, seconds in watch.found]
    stalled = [seconds for seconds in watch.stalled if seconds >= 0]
    print("Fresh: " + ", ".join(f"{who} {key} {s:.1f}s" for who, key, s in watch.fresh))
    print("Found: " + ", ".join(f"{who} {what} {s:.1f}s" for who, what, s in watch.found))
    print(f"no progress shown after the threshold: {[round(s, 1) for s in stalled]}")
    print(f"idle shown after the threshold: { {k: round(v, 1) for k, v in watch.idled.items()} }")
    if watch.missed:
        print("statuses gone before shown: " + ", ".join(
            f"{who} {key} ({s:.1f}s)" for who, key, s in watch.missed))  # fmt: skip
    print(f"Fresh p95 {p95(fresh):.1f}s over {len(fresh)} (target ≤ {FRESH_TARGET:.0f}s); "
          f"Found p95 {p95(found):.1f}s over {len(found)} (target ≤ {FOUND_TARGET:.0f}s); "
          f"wrong statuses {len(watch.wrong)}")  # fmt: skip
    assert fresh and p95(fresh) <= FRESH_TARGET, f"Fresh p95 {p95(fresh):.1f}s"
    assert found and p95(found) <= FOUND_TARGET, f"Found p95 {p95(found):.1f}s"
    assert max(found) <= FOUND_MOST and max(fresh) <= BEHIND


async def main():
    # As a terminal the owner opens: nothing of a Copilot that may be running this test.
    for key in [key for key in os.environ if key.startswith("COPILOT_")]:
        os.environ.pop(key)
    root = Path(tempfile.mkdtemp(prefix="cos-others-", dir=os.environ.get("LIVE_OTHERS_TMP")))
    root = root.resolve()
    names = [
        "terminal-a",
        "terminal-b",
        "terminal-e",
        "terminal-x",
        "terminal-r",
        "fake-terminal",
        "script",
        "terminal-s",
        "batch",
        "one-off",
    ]
    for name in names:
        (root / name).mkdir()
    home = make_home(root, [root, *(root / name for name in names)])
    env = {
        "COPILOT_HOME": str(home),
        "CHIEFOFSTAFF_OTHERS_STALL_SECONDS": str(STALL),
        "CHIEFOFSTAFF_OTHERS_IDLE_SECONDS": str(IDLE),
    }
    app = Instance("Chief of Staff", PORT, root / "data", root / "chiefofstaff.log", env)
    other = Instance(
        "the other Chief of Staff", OTHER_PORT, root / "data2", root / "other.log", env
    )
    ctx = Ctx(root, home, app, other)
    ctx.watch = Watch(ctx)
    follow = None
    failed = True
    began = wall()
    # Stopped (Ctrl-C, a kill), it still cleans up: its processes and its folder.
    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    for number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(number, task.cancel)  # type: ignore[union-attr]
    try:
        await app.start()
        follow = asyncio.create_task(ctx.follow())
        ctx.watch.start()
        await asyncio.wait_for(ctx.watch.connected.wait(), 30)
        log(f"a tab is connected; the home is {home}")
        first, second = await o1_terminal(ctx)
        await o5_stopped_idle_failed(ctx)
        await o3_untouched(ctx)
        fake = await o2_provenance(ctx, first)
        await o4_continue(ctx, second, fake)
        await o6_batch(ctx)
        report(ctx, began)
        failed = False
        log("LIVE OTHERS PASSED")
    except asyncio.CancelledError:
        log("INTERRUPTED: cleaning up")
        raise SystemExit(130) from None
    except NotTheProductError as exc:
        log(f"NOT THE PRODUCT (the model or a terminal): {exc}")
        raise SystemExit(2) from exc
    except BaseException as exc:
        log(f"PRODUCT FAILURE: {type(exc).__name__}: {exc}")
        with suppress(Exception):
            report(ctx, began)
        raise
    finally:
        await ctx.watch.stop()
        if follow:
            follow.cancel()
        for term in ctx.terminals:
            if failed and term.child.isalive():
                print(f"--- {term.name}'s screen\n{term.text()}")
            term.close()
        for close in ctx.closers:
            with suppress(Exception):
                close()
        for helper in ctx.helpers:
            with suppress(ProcessLookupError):
                os.killpg(helper.pid, signal.SIGKILL)
            helper.wait(10)
        await app.stop()
        await other.stop()
        if failed:
            for name in ("chiefofstaff.log", "other.log"):
                with suppress(OSError):
                    tail = (root / name).read_text().splitlines()[-40:]
                    print(f"--- {name} (tail)\n" + "\n".join(tail))
        left = [
            lock
            for lock in home.glob("session-state/*/inuse.*.lock")
            if alive(int(lock.name.split(".")[1]))
        ]
        if left:
            log(f"still open: {[str(lock) for lock in left]}")
        if os.environ.get("LIVE_OTHERS_KEEP") == "1":
            log(f"kept {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
