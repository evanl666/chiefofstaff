"""Benchmarks of the index of the owner's other Copilot sessions (others.py) against the budgets
of docs/design/adopt-sessions.md, section 4.6.

It builds a reference corpus in a folder of its own (a temporary one unless --dir names one):
200 session logs sized as measured (median 58 KB, 85% under 4 MB, the rest up to 12 MB), a
large log (--large-mb: 1200 is the largest measured; 256 by default keeps a run under two
minutes) and 2 MB lines (binary assets) among them; the words are made up. Then, with the real
index on an event loop of its own and a stand-in for Copilot that lists the corpus:

- memory: what the index keeps for 200 sessions, and a pass's peak (tracemalloc);
- start: a tab connects; how long until every open session has its status, and until every
  session has had its first look (warm: the corpus was just written; cold is not measured);
- steady: 8 open sessions written at 5.5 MB a minute each by processes of their own (as
  Copilot's CLI writes them), for --seconds: how fresh statuses are, how soon a session opened
  in a terminal is found, the event loop's time a pass and its lag with the index and without,
  bytes read, CPU, Copilot's list read;
- no tab: nothing is read;
- stress: the 8 at 16 MB a minute, over the cap: rows may lose their status, none may show a
  wrong one.

    uv run python scripts/bench_others.py [--dir DIR] [--large-mb 256] [--seconds 20]
"""

import argparse
import asyncio
import gc
import json
import multiprocessing
import random
import shutil
import sys
import tempfile
import time
import tracemalloc
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from chiefofstaff import others
from chiefofstaff.bus import Bus
from chiefofstaff.others import Others
from chiefofstaff.store import Store

MB = 1 << 20
# A session opened in a terminal is seen by the lock scan, every 9 s (Runtime.elsewhere).
LOCK_SCAN = 9.0
# Made-up words, 3 MB of them.
FILLER = (
    " ".join(
        random.Random(3).choice(
            ["parse", "token", "branch", "fixture", "query", "render", "cache", "retry", "module"]
        )
        for _ in range(40_000)
    )
    * 12
)


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds")[:23] + "Z"


def line(type_: str, at: float, **data) -> bytes:
    event = {"type": type_, "data": data, "id": str(uuid.uuid4()), "timestamp": iso(at)}
    return (json.dumps({**event, "parentId": None}, separators=(",", ":")) + "\n").encode()


def call_lines(rng: random.Random, at: float, result: int) -> list[bytes]:
    """One step of a model call: its request, the tool running, its result, the usage."""
    call = f"call_{uuid.uuid4().hex[:24]}"
    command = {"command": f"pytest -k {rng.choice(['login', 'orders', 'cache'])}"}
    start = rng.randrange(0, len(FILLER) - result - 1)
    notes = {"notes": FILLER[: rng.randrange(500, 3000)]}
    return [
        line("assistant.message", at, content="", toolRequests=[{"toolCallId": call}]),
        line("tool.execution_start", at, toolCallId=call, toolName="bash", arguments=command),
        line(
            "tool.execution_complete",
            at,
            toolCallId=call,
            success=True,
            result={"content": FILLER[start : start + result]},
        ),
        line("session.usage_record", at, usage={"model": "a-model", "inputTokens": 9000}),
        line("model.call_success", at, modelCall=notes),
        line("assistant.turn_end", at, turnId="0"),
        line("assistant.turn_start", at, turnId="0"),
    ]


def turn_lines(rng: random.Random, at: float) -> bytes:
    lines = [
        line("user.message", at, content="Make the " + FILLER[:60]),
        line("assistant.turn_start", at, turnId="0"),
    ]
    for _ in range(rng.randint(1, 6)):
        lines += call_lines(rng, at, rng.choice([400, 2000, 8000, 30000]))
    lines += [
        line("assistant.message", at, content="Done: " + FILLER[: rng.randrange(200, 2000)]),
        line("assistant.turn_end", at, turnId="0"),
    ]
    return b"".join(lines)


def ending(state: str, at: float) -> bytes:
    """How a log ends: asking, at work, the owner's turn, or exited after an answer."""
    if state == "your_turn":
        return line("assistant.message", at, content="All done.") + line("assistant.turn_end", at)
    lines = line("user.message", at, content="Next") + line("assistant.turn_start", at)
    if state == "asks":
        asked = {"message": "Which branch?"}
        return lines + line("tool.execution_start", at, toolCallId="c-ask", toolName="ask_user",
                            arguments=asked)  # fmt: skip
    if state == "working":
        run = {"command": "make test"}
        return lines + line("tool.execution_start", at, toolCallId="c-run", toolName="bash",
                            arguments=run)  # fmt: skip
    return (
        lines
        + line("assistant.message", at, content="Bye.")
        + line("assistant.turn_end", at)
        + line("session.shutdown", at)
    )


def write_log(path: Path, rng: random.Random, size: int, state: str, block: bytes = b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    at = time.time() - 600
    head = {"sessionId": path.parent.name, "version": 1, "producer": "copilot-agent"}
    with path.open("wb") as log:
        written = log.write(line("session.start", at - 60, startTime=iso(at - 60), **head))
        next_asset = 6 * MB
        while written < size:
            if block:
                written += log.write(block)
                continue
            if size > 4 * MB and written > next_asset:
                asset = FILLER[: 2 * MB - 200]
                written += log.write(line("session.binary_asset", at, assetId="a", data=asset))
                next_asset += 6 * MB
            written += log.write(turn_lines(rng, at))
        log.write(ending(state, at))


# What each ending shows, open and closed.
SHOWN = {
    True: {"your_turn": "your_turn", "asks": "asks", "working": "working"},
    False: {"your_turn": "finished", "asks": "stopped", "working": "stopped", "exited": "finished"},
}


def build(home: Path, large_mb: int) -> tuple[list[str], dict[str, str], list[str]]:
    """The corpus: session ids, what each should show, and the open ones."""
    rng = random.Random(7)
    ids = [str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(200)]
    opened = [*ids[:12], ids[199]]
    expected = {}
    for n, session_id in enumerate(ids):
        size = (
            min(4 * MB - 1, int(58_000 * rng.lognormvariate(0, 1.4)))
            if n < 170
            else rng.randrange(4 * MB, 12 * MB)
        )
        is_open = session_id in opened
        state = list(SHOWN[is_open])[n % len(SHOWN[is_open])]
        expected[session_id] = SHOWN[is_open][state]
        path = home / "session-state" / session_id / "events.jsonl"
        if n == 199:
            block = b"".join(turn_lines(rng, time.time() - 600) for _ in range(100))
            block += line("session.binary_asset", time.time(), assetId="b", data=FILLER[: 2 * MB])
            write_log(path, rng, large_mb * MB, state, block)
        else:
            write_log(path, rng, size, state)
    return ids, expected, opened


class Backend:
    """Copilot's list of the corpus, and where each log is."""

    def __init__(self, home: Path, ids: list[str]):
        self.home = home
        self.lists = 0
        self.metas = [self.meta(session_id) for session_id in ids]

    @staticmethod
    def meta(session_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            session_id=session_id,
            summary="A session",
            is_remote=False,
            modified_time=datetime.now(UTC),
            context=SimpleNamespace(working_directory="/work", repository="", branch="main"),
            client_name="github/cli",
            last_client_name="github/cli",
            clients_listed=True,
        )

    async def list_sessions(self):
        self.lists += 1
        return list(self.metas)

    def session_log(self, session_id: str) -> Path:
        return self.home / "session-state" / session_id / "events.jsonl"


class App:
    """What of the runtime the index uses."""

    def __init__(self, store: Store, backend: Backend, opened: list[str]):
        self.store, self.bus, self.backend = store, Bus(), backend
        self.bus.bind(asyncio.get_running_loop())
        self.elsewhere = dict.fromkeys(opened, 4242)
        self.connections = 0

    def ready(self) -> bool:
        return True

    def require_ready(self):
        return self.backend


class Measured(Others):
    """The index, timing its work on the event loop pass by pass: the time the loop's thread
    spends on it (waiting for another thread to let go of the interpreter is the lag's)."""

    def __init__(self, app):
        super().__init__(app)
        self.loop_time = 0.0
        self.per_pass: list[float] = []

    def timed(self, work, *arguments):
        began = time.thread_time()
        try:
            return work(*arguments)
        finally:
            self.loop_time += time.thread_time() - began

    def track(self):
        self.timed(super().track)

    def merge(self, found):
        self.timed(super().merge, found)

    def publish(self, generation: int):
        self.timed(super().publish, generation)
        if self.passes > len(self.per_pass):
            self.per_pass.append(self.loop_time)
            self.loop_time = 0.0


PASSES: list[dict] = []
real_pass = others.read_pass


def counted_pass(logs, startup, cursor, stopped):
    found = real_pass(logs, startup, cursor, stopped)
    PASSES.append({"startup": startup, "bytes": found[3]})
    return found


others.read_pass = counted_pass


def write_session(path: str, rate: float, seconds: float, seed: int, out):
    """An open session in a terminal, in a process of its own as Copilot's CLI is: it works,
    asks now and then, and ends turns, writing its log at a rate. Sends back when it was in
    which state (the monotonic clock is the machine's)."""
    rng = random.Random(seed)
    states: list[tuple[float, str]] = []
    began = time.monotonic()
    deadline, written = began + seconds, 0

    def put(log, data: bytes, state: str = ""):
        nonlocal written
        log.write(data)
        log.flush()
        if state:
            states.append((time.monotonic(), state))
        written += len(data)
        # At its rate on average: what a wait held back comes in a burst after it.
        time.sleep(max(0.0, began + written / rate - time.monotonic()))

    with open(path, "ab") as log:  # noqa: PTH123
        while time.monotonic() < deadline:
            now = time.time()
            prompt = line("user.message", now, content="Go on") + line("assistant.turn_start", now)
            put(log, prompt, "working")
            steps = 0
            # Two steps or more: as many as bring it back to its rate after a wait.
            while steps < 2 or began + written / rate < min(time.monotonic(), deadline) - 0.1:
                put(log, b"".join(call_lines(rng, time.time(), rng.choice([2000, 8000, 30000]))))
                steps += 1
            if rng.random() < 0.5:
                call, asked = f"call_{uuid.uuid4().hex[:24]}", {"message": "Ship it?"}
                put(log, line("tool.execution_start", time.time(), toolCallId=call,
                              toolName="ask_user", arguments=asked), "asks")  # fmt: skip
                time.sleep(min(rng.uniform(2, 4), max(0.0, deadline - time.monotonic())))
                done = line("tool.execution_complete", time.time(), toolCallId=call, success=True)
                put(log, done, "working")
            ended = line("assistant.message", time.time(), content="Done.")
            put(log, ended + line("assistant.turn_end", time.time()), "your_turn")
            time.sleep(min(rng.uniform(0.5, 1.5), max(0.0, deadline - time.monotonic())))
    out.send(states)


class Writers:
    """Sessions written by processes of their own for a while."""

    def __init__(self, paths: list[Path], rate: float, seconds: float, before: dict[str, str]):
        context = multiprocessing.get_context("spawn")
        self.paths, self.before = paths, before
        self.pipes = [context.Pipe(duplex=False) for _ in paths]
        self.processes = [
            context.Process(target=write_session, args=(str(path), rate, seconds, n, sending))
            for n, (path, (_, sending)) in enumerate(zip(paths, self.pipes, strict=True))
        ]
        self.states: dict[str, list[tuple[float, str]]] = {}

    def start(self):
        for process in self.processes:
            process.start()

    async def join(self):
        for path, (receiving, _), process in zip(
            self.paths, self.pipes, self.processes, strict=True
        ):
            self.states[path.parent.name] = await asyncio.to_thread(receiving.recv)
            await asyncio.to_thread(process.join)

    def was(self, session_id: str, key: str, since: float, until: float) -> bool:
        """Whether this session was in this state at some time between the two."""
        then = self.before[session_id]
        for at, state in self.states[session_id]:
            if at < since:
                then = state
            elif at > until:
                break
            elif state == key:
                return True
        return then == key

    def asked(self, session_id: str) -> list[float]:
        return [at for at, state in self.states[session_id] if state == "asks"]


class Tab:
    """A connected tab: what it heard, and when."""

    def __init__(self, app: App):
        self.app = app
        self.events: list[tuple[float, dict]] = []
        self.shown: dict[str, dict] = {}

    async def listen(self):
        async with self.app.bus.subscribe() as queue:
            while (payload := await queue.get()) is not None:
                event = json.loads(payload)
                if event["type"] == "others":
                    self.events.append((time.monotonic(), event["data"]))
                    self.shown |= {row["id"]: row for row in event["data"]["rows"]}

    def __enter__(self):
        self.task = asyncio.ensure_future(self.listen())
        return self

    def __exit__(self, *_):
        self.task.cancel()


async def lag(stop: asyncio.Event, samples: list[float]):
    while not stop.is_set():
        began = time.perf_counter()
        await asyncio.sleep(0.01)
        samples.append(time.perf_counter() - began - 0.01)


def p(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * percent / 100))] if ordered else 0.0


async def until(check, seconds: float) -> float | None:
    began = time.monotonic()
    while time.monotonic() - began < seconds:
        if check():
            return time.monotonic() - began
        await asyncio.sleep(0.005)
    return None


async def load(writers: Writers, seconds: float) -> dict:
    """Sessions written for a while, the lag probe running: what this process spent."""
    stop, samples = asyncio.Event(), []
    probe = asyncio.ensure_future(lag(stop, samples))
    cpu, began = time.process_time(), time.monotonic()
    writers.start()
    await asyncio.sleep(seconds)
    elapsed, cpu = time.monotonic() - began, time.process_time() - cpu
    stop.set()
    await probe
    await writers.join()
    return {"lag": samples, "cpu": cpu / elapsed, "seconds": elapsed}


def wrong_leased(tab: Tab, writers: Writers) -> tuple[int, int]:
    """Statuses held while each pass renewed them, against what the session was doing when
    their log was sampled: how many differed, of how many."""
    wrong = held = 0
    shown: dict[str, dict] = {}
    for at, event in tab.events:
        shown |= {row["id"]: row for row in event["rows"]}
        for session_id, valid_for in event["leases"].items():
            if session_id not in writers.states:
                continue
            held += 1
            sampled = at - (others.FRESH - valid_for)
            key = shown[session_id]["status"]["key"]
            wrong += not writers.was(session_id, key, sampled - 0.5, sampled + 0.5)
    return wrong, held


async def main(args) -> int:
    root = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="bench-others-"))
    root.mkdir(parents=True, exist_ok=True)
    home = root / "copilot"
    results: list[tuple[str, str, str, bool]] = []

    def budget(name: str, target: str, measured: str, ok: bool):
        results.append((name, target, measured, ok))

    try:
        began = time.monotonic()
        ids, expected, opened = build(home, args.large_mb)
        size = sum((home / "session-state" / i / "events.jsonl").stat().st_size for i in ids)
        print(f"Corpus: {len(ids)} logs, {size / MB:.0f} MB, in {time.monotonic() - began:.1f} s")
        backend = Backend(home, ids)
        store = Store(root / "data")

        # Memory: what the index keeps for 200 sessions, and a pass's peak.
        app = App(store, backend, opened)
        app.bus.queues.add(asyncio.Queue())
        index = Others(app)
        filters = [tracemalloc.Filter(True, others.__file__)]
        tracemalloc.start(1)
        gc.collect()
        before, peaks = tracemalloc.take_snapshot().filter_traces(filters), []
        while index.fresh:
            tracemalloc.reset_peak()
            current = tracemalloc.get_traced_memory()[0]
            await index.step(index.generation, index.stopping)
            peaks.append(tracemalloc.get_traced_memory()[1] - current)
        gc.collect()
        after = tracemalloc.take_snapshot().filter_traces(filters)
        kept = sum(stat.size_diff for stat in after.compare_to(before, "filename"))
        tracemalloc.stop()
        index.halt()
        budget("Memory kept by the index", "≤ 3 MB, 200 sessions", f"{kept / MB:.2f} MB",
               kept <= 3 * MB)  # fmt: skip
        budget("Memory in a pass at its peak", "≤ 16 MB", f"{max(peaks) / MB:.1f} MB",
               max(peaks) <= 16 * MB)  # fmt: skip

        # Start: a tab connects.
        PASSES.clear()
        app = App(store, backend, opened)
        index = Measured(app)
        index.allowed = True
        index.launch()
        with Tab(app) as tab:
            want = {i for i in opened if expected[i]}
            shown = await until(
                lambda: want <= {i for i, r in tab.shown.items() if r["status"]}, 10
            )
            looked = await until(lambda: not index.fresh, 30)
            budget("Statuses of open sessions after a tab connects", "≤ 1 s warm",
                   f"{shown:.2f} s" if shown is not None else "never",
                   shown is not None and shown <= 1)  # fmt: skip
            budget("First looks of every tracked session", "≤ 3 s warm",
                   f"{looked:.2f} s" if looked is not None else "never",
                   looked is not None and looked <= 3)  # fmt: skip
            wrong = [
                i for i, row in tab.shown.items()
                if row["status"] and row["status"]["key"] != expected[i]
            ]  # fmt: skip
            with_status = sum(1 for row in tab.shown.values() if row["status"])
            budget("Statuses that differ from the truth, at start", "0",
                   f"{len(wrong)} of {with_status}", not wrong)  # fmt: skip
            startup = [x["bytes"] for x in PASSES if x["startup"]]
            budget("Bytes read a startup pass", "≤ 16 MB",
                   f"{max(startup) / MB:.1f} MB ({len(startup)} passes)",
                   max(startup) <= 16 * MB)  # fmt: skip

            # Steady: 8 open sessions written at 5.5 MB a minute each.
            busy = [home / "session-state" / i / "events.jsonl" for i in opened[:8]]
            writers = Writers(busy, 5.5 * MB / 60, args.seconds, expected)
            PASSES.clear()
            index.per_pass.clear()
            lists, found_in = backend.lists, []

            async def open_later():
                for n in range(3):
                    await asyncio.sleep(args.seconds / 4)
                    session_id = str(uuid.UUID(int=random.Random(n).getrandbits(128), version=4))
                    write_log(backend.session_log(session_id), random.Random(n), 20_000, "working")
                    backend.metas.append(backend.meta(session_id))
                    app.elsewhere[session_id] = 4242  # The lock scan saw it.
                    took = await until(lambda found=session_id: found in tab.shown, 15)
                    found_in.append(took if took is not None else float("inf"))

            opening = asyncio.ensure_future(open_later())
            steady = await load(writers, args.seconds)
            await opening
            fresh = []
            for session_id in writers.states:
                for asked in writers.asked(session_id):
                    heard = next(
                        (at for at, event in tab.events if at >= asked for row in event["rows"]
                         if row["id"] == session_id and (row["status"] or {}).get("key") == "asks"),
                        None,
                    )  # fmt: skip
                    if heard is not None:
                        fresh.append(heard - asked)
            loop = [seconds * 1000 for seconds in index.per_pass]
            passes = len(loop)
            steady_bytes = [x["bytes"] for x in PASSES if not x["startup"]]
            per_second = sum(steady_bytes) / steady["seconds"]
            minutes = steady["seconds"] / 60
            budget("Fresh (an event to the tab), steady load", "≤ 4 s at p95",
                   f"{p(fresh, 95):.2f} s ({len(fresh)} questions)",
                   bool(fresh) and p(fresh, 95) <= 4)  # fmt: skip
            found = p(found_in, 95) + LOCK_SCAN
            budget("Found (opened in a terminal to the tab)", "≤ 13 s at p95",
                   f"{found:.1f} s ({LOCK_SCAN:.0f} s lock scan + {p(found_in, 95):.2f} s)",
                   found <= 13)  # fmt: skip
            budget("Bytes read a steady pass", "≤ 4 MB", f"{max(steady_bytes) / MB:.2f} MB",
                   max(steady_bytes) <= 4 * MB)  # fmt: skip
            budget("Bytes read, sustained", "≤ 2 MB/s", f"{per_second / MB:.2f} MB/s",
                   per_second <= 2 * MB)  # fmt: skip
            listed = backend.lists - lists
            budget("session.list calls", "≤ 1 a minute, + 1 a change",
                   f"{listed} in {minutes:.1f} min, 3 changes",
                   listed <= int(minutes) + 1 + 3)  # fmt: skip
            behind = max((others.FRESH - v for _, e in tab.events for v in e["leases"].values()),
                         default=0)  # fmt: skip
            budget("Behind (a shown status's sample age)", "≤ 15 s", f"{behind:.1f} s at most",
                   behind <= others.FRESH)  # fmt: skip
            wrong, held = wrong_leased(tab, writers)
            budget("Statuses that differ from the truth, steady", "0", f"{wrong} of {held}",
                   wrong == 0)  # fmt: skip

        # No tab: nothing is read.
        PASSES.clear()
        await asyncio.sleep(5)
        budget("Reads with no tab", "0", f"{len(PASSES)} passes", not PASSES)
        await index.stop()

        # The same load with the index off.
        quiet = await load(Writers(busy, 5.5 * MB / 60, args.seconds, expected), args.seconds)
        added = p(steady["lag"], 99) - p(quiet["lag"], 99)
        budget("Event-loop lag, on against off", "+ ≤ 5 ms at p99",
               f"{p(steady['lag'], 99) * 1000:.2f} against {p(quiet['lag'], 99) * 1000:.2f} ms",
               added <= 0.005)  # fmt: skip
        cpu = (steady["cpu"] - quiet["cpu"]) * 100
        budget("CPU of this process, steady", "+ ≤ 1% of a core",
               f"{steady['cpu'] * 100:.2f}% against {quiet['cpu'] * 100:.2f}%",
               cpu <= 1)  # fmt: skip

        # Stress: 16 MB a minute each, over the cap.
        app = App(store, backend, opened)
        index = Measured(app)
        index.allowed = True
        index.launch()
        with Tab(app) as tab:
            await until(lambda: not index.fresh, 30)
            # As the steady load left them.
            before = {key: states[-1][1] for key, states in writers.states.items()}
            writers = Writers(busy, 16 * MB / 60, args.seconds, before)
            PASSES.clear()
            index.per_pass.clear()
            await load(writers, args.seconds)
            wrong, held = wrong_leased(tab, writers)
            budget("Statuses that differ from the truth, stress", "0", f"{wrong} of {held}",
                   wrong == 0)  # fmt: skip
            loop += [seconds * 1000 for seconds in index.per_pass]
            budget("Event-loop time added per pass", "≤ 2 ms at p99",
                   f"{p(loop, 99):.2f} ms ({passes} steady, {len(loop) - passes} stress passes)",
                   p(loop, 99) <= 2)  # fmt: skip
            stress = [x["bytes"] for x in PASSES if not x["startup"]]
            budget("Bytes read a pass, stress", "≤ 4 MB", f"{max(stress) / MB:.2f} MB",
                   max(stress) <= 4 * MB)  # fmt: skip
        await index.stop()
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)

    width = max(len(name) for name, *_ in results)
    print(f"\n{'Budget'.ljust(width)}  {'Target'.ljust(26)}  Measured")
    for name, target, measured, ok in results:
        print(f"{name.ljust(width)}  {target.ljust(26)}  {measured}  {'ok' if ok else 'OVER'}")
    return 0 if all(ok for *_, ok in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", help="where to build the corpus (a temporary folder otherwise)")
    parser.add_argument("--large-mb", type=int, default=256, help="the large log's size")
    parser.add_argument("--seconds", type=float, default=20, help="how long each load runs")
    parser.add_argument("--keep", action="store_true", help="keep the corpus")
    sys.exit(asyncio.run(main(parser.parse_args())))
