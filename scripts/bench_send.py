"""How long the owner waits after Enter: each step of Runtime.owner_send before their message is
stored (its event goes out then, and the chat shows it), and until the send returns, on Home (to
the Router) and in a worker's chat. No model is called and nothing leaves this machine.

The runtime runs as tests run it, on a data directory the size of a busy owner's (workers with
their tasks, reports and steps), with what the real Copilot CLI does around a message made as
slow as it is under load: listing the owner's sessions (--listing-ms, the CLI's RPC) and reading
the process table (`ps`, for real). On Home a session opened in a terminal since the last message
(as builders' and the owner's own CLIs do all day), with one of Chief of Staff's own beside it;
in a worker's chat, its task still at work in a real git worktree (--files files).

p50 and p95 of --runs runs each, in milliseconds, under whatever load this machine has now:

    TMPDIR=… nice -n 15 uv run --extra dev python scripts/bench_send.py [--runs 10]
        [--listing-ms 800] [--workers 25] [--messages 17000] [--files 3000]
"""

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from conftest import FakeClient, Harness, call_tool, settle  # noqa: E402

from chiefofstaff import caches, machine, processes  # noqa: E402
from chiefofstaff import runtime as runtime_module  # noqa: E402
from chiefofstaff.models import Settings  # noqa: E402

WORDS = (
    "研究 股票 基本面 估值 the parser fails on empty input; fix the flaky login test, add a "
    "retry, review acme/exports#7, write the API changelog, 复测 游戏 质量 历史 问题, deploy "
    "staging and check the health page, rename the flag across the repo"
).split()


def ms(values: list[float]) -> str:
    if not values:
        return "—"
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return f"{statistics.median(ordered) * 1000:8.1f} / {p95 * 1000:8.1f}"


class SlowCopilot(FakeClient):
    """The fake Copilot of the tests, with its session listing as slow as --listing-ms and the
    process table read for real (as sessions_in_use does)."""

    def __init__(self, listing: float):
        super().__init__()
        self.listing = listing

    async def list_sessions(self):
        await asyncio.sleep(self.listing)
        return self.metas

    async def open_elsewhere(self):
        await asyncio.to_thread(processes.process_table)
        return dict(self.in_use)


class Steps:
    """Each call of the named methods, timed from the start of the send being measured."""

    def __init__(self):
        self.started = 0.0
        self.times: dict[str, list[float]] = {}
        self.stored: list[float] = []
        self.patched: list[tuple[object, str]] = []

    def add(self, label: str, value: float):
        self.times.setdefault(label, []).append(value)

    def wrap(self, owner, name: str, label: str = ""):
        label = label or name
        original = getattr(owner, name)
        steps = self
        if asyncio.iscoroutinefunction(original):

            async def timed(*args, **kwargs):
                begun = time.perf_counter()
                try:
                    return await original(*args, **kwargs)
                finally:
                    steps.add(label, time.perf_counter() - begun)

        else:

            def timed(*args, **kwargs):
                begun = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    steps.add(label, time.perf_counter() - begun)

        setattr(owner, name, timed)
        self.patched.append((owner, name))

    def watch_store(self, runtime):
        original = runtime.add_message
        steps = self

        def add_message(session_id, kind, text, meta=None, **kwargs):
            found = original(session_id, kind, text, meta, **kwargs)
            if kind == "owner" and "prompt" in (meta or {}) and not (meta or {}).get("to"):
                steps.stored.append(time.perf_counter() - steps.started)
            return found

        runtime.add_message = add_message
        self.patched.append((runtime, "add_message"))

    def undo(self):
        for owner, name in self.patched:
            vars(owner).pop(name, None)
        self.patched = []


def run(*command, cwd=None):
    subprocess.run(command, cwd=cwd, check=True, capture_output=True)


def repository(folder: Path, files: int) -> Path:
    """A project with `files` files, committed, and published (its worker gets a worktree)."""
    origin = folder / "app.git"
    run("git", "init", "-q", "--bare", "-b", "main", str(origin))
    local = folder / "app"
    run("git", "clone", "-q", str(origin), str(local))
    run("git", "checkout", "-q", "-b", "main", cwd=local)
    for index in range(files):
        part = local / f"src/part{index % 40}"
        part.mkdir(parents=True, exist_ok=True)
        (part / f"module{index}.py").write_text(f"VALUE = {index}\n" * 20)
    run("git", "add", ".", cwd=local)
    run("git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-qm", "init", cwd=local)
    run("git", "push", "-q", "origin", "main", cwd=local)
    return local


def sentence(index: int, length: int = 24) -> str:
    return " ".join(WORDS[(index * 7 + k * 3) % len(WORDS)] for k in range(length))


def populate(runtime, workers: int, messages: int):
    """Workers, open and closed, with tasks, the Router's instructions, the owner's messages,
    reports and many steps: as a busy owner's data directory holds them."""
    store = runtime.store
    router = store.get("router_id")
    each = max(1, messages // max(1, workers))
    for index in range(workers):
        session = str(uuid4())
        store.create_session(session, "worker", sentence(index, 6), "idle", cwd=str(ROOT))
        for number in range(3):
            runtime.tasks.create(session, sentence(index + number, 6), sentence(index + number))
        rows = []
        for step in range(each):
            kind = ("router", "owner", "report")[step % 3] if step % 50 == 0 else "tool"
            rows.append(
                (session, kind, sentence(index + step), "{}", datetime.now(UTC).isoformat())
            )
        store.db.executemany(
            "INSERT INTO messages(session_id,kind,text,meta,created_at) VALUES(?,?,?,?,?)", rows
        )
        if index % 3 == 0:
            store.update_session(session, closed_at=datetime.now(UTC).isoformat(), status="done")
        else:
            store.update_session(session, report=sentence(index, 40))
    store.db.executemany(
        "INSERT INTO messages(session_id,kind,text,meta,created_at) VALUES(?,?,?,?,?)",
        [(router, ("owner", "assistant", "notice")[k % 3], sentence(k), "{}",
          datetime.now(UTC).isoformat()) for k in range(400)],
    )  # fmt: skip
    store.db.commit()


def terminal(client, identifier: str, summary: str, made: str = "github/cli"):
    found = SimpleNamespace(
        session_id=identifier,
        summary=summary,
        is_remote=False,
        modified_time=datetime.now(UTC),
        context=SimpleNamespace(working_directory=str(ROOT), repository="", branch=""),
    )
    vars(found).update(client_name=made, last_client_name="", clients_listed=True)
    client.metas.append(found)


ROUTER_STEPS = (
    ("heard", "language"),
    ("switched_language", "language switch"),
    ("projects_note", "projects note"),
    ("terminals_note", "terminals note"),
)
WORKER_STEPS = (
    ("settings_typed", "settings typed"),
    ("chat_task", "chat task"),
    ("heard", "language"),
    ("switched_language", "language switch"),
    ("new_chat_task", "new chat task"),
)


async def home(runtime, client, runs: int) -> Steps:
    router = runtime.store.get("router_id")
    steps = Steps()
    for name, label in ROUTER_STEPS:
        steps.wrap(runtime, name, label)
    steps.wrap(runtime.toolset, "related_hint", "related workers")
    steps.wrap(runtime.workers, "external", "  session listing")
    steps.wrap(runtime.delivery, "flush", "send (after it is stored)")
    steps.watch_store(runtime)
    try:
        for index in range(runs):
            # Opened in a terminal since the last message (a builder's, the owner's own), with
            # one of Chief of Staff's own beside it; the last listing older than its 15 s.
            mine, ours = str(uuid4()), str(uuid4())
            terminal(client, mine, f"Owner's terminal session {index}")
            terminal(client, ours, "A worker of another Chief of Staff", "copilot-cli/chiefofstaff")
            client.in_use |= {mine: 40000 + index, ours: 50000 + index}
            await runtime.workers.refresh_elsewhere()
            runtime.workers.external_cache = (float("-inf"), [])
            await settle(runtime)
            steps.started = time.perf_counter()
            await runtime.owner_send(router, "你去研究一下股票 开一个新的worker", typed=True)
            steps.add("returned", time.perf_counter() - steps.started)
            runtime.update_session(router, status="idle")
            runtime.states[router].busy = False
            await settle(runtime)
    finally:
        steps.undo()
    return steps


async def chat(runtime, harness, folder: Path, runs: int, files: int) -> tuple[Steps, Steps]:
    app = repository(folder, files)
    runtime.store.save_settings(
        runtime.store.settings().model_copy(update={"projects": [str(app)]})
    )
    made = await call_tool(runtime, "spawn_worker", harness.router_id, title="Fix the parser",
                           instructions="Fix the parser", why_new="bench", project=str(app),
                           isolate=True)  # fmt: skip
    worker = json.loads(made.text_result_for_llm)
    session_id = worker["session_id"]
    for _ in range(200):
        await settle(runtime)
        if runtime.store.get(f"taskbase:{worker['task_id']}"):
            break
        await asyncio.sleep(0.03)
    steps = Steps()
    for name, label in WORKER_STEPS:
        steps.wrap(runtime, name, label)
    steps.wrap(runtime.decisions, "answered_in_chat", "decision cards")
    steps.wrap(runtime.task_actions, "record_asked_base", "checkouts now (git)")
    steps.wrap(runtime.task_actions, "record_chat_base", "checkouts now (git)")
    steps.wrap(runtime.workspaces, "restore", "worktree there")
    steps.wrap(runtime.delivery, "flush", "send (after it is stored)")
    steps.watch_store(runtime)
    finished = Steps()
    try:
        for index in range(runs):
            (Path(worker["cwd"]) / f"edited{index}.py").write_text("x = 1\n")
            runtime.update_session(session_id, status="working")
            await settle(runtime)
            steps.started = time.perf_counter()
            await runtime.owner_send(session_id, f"Also keep {index} in its test.", typed=True)
            steps.add("returned", time.perf_counter() - steps.started)
            await settle(runtime)
        steps.undo()
        # Its task done: the owner asks about it in its chat (its checkouts then are a base).
        await call_tool(runtime, "report", session_id, status="done", summary="Fixed it.")
        for _ in range(200):
            await settle(runtime)
            if runtime.store.get(f"taskend:{worker['task_id']}"):
                break
            await asyncio.sleep(0.03)
        runtime.update_session(session_id, status="idle")
        for name, label in WORKER_STEPS:
            finished.wrap(runtime, name, label)
        finished.wrap(runtime.task_actions, "record_chat_base", "checkouts now (git)")
        finished.wrap(runtime.delivery, "flush", "send (after it is stored)")
        finished.watch_store(runtime)
        for index in range(runs):
            (Path(worker["cwd"]) / f"later{index}.py").write_text("y = 2\n")
            runtime.store.delete(f"chatbase:{worker['task_id']}")
            runtime.store.delete(f"restarted:{worker['task_id']}")
            finished.started = time.perf_counter()
            await runtime.owner_send(session_id, f"Why {index} there?", typed=True)
            finished.add("returned", time.perf_counter() - finished.started)
            runtime.update_session(session_id, status="idle")
            runtime.states[session_id].busy = False
            await settle(runtime)
    finally:
        steps.undo()
        finished.undo()
    return steps, finished


def report(title: str, steps: Steps):
    print(f"\n{title}  (ms, p50 / p95)")
    for label, values in steps.times.items():
        if label != "returned":
            print(f"  {label:<28}{ms(values)}  x{len(values)}")
    print(f"  {'STORED (shown) after':<28}{ms(steps.stored)}")
    print(f"  {'RETURNED after':<28}{ms(steps.times.get('returned', []))}")


async def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--listing-ms", type=float, default=800)
    parser.add_argument("--workers", type=int, default=25)
    parser.add_argument("--messages", type=int, default=17000)
    parser.add_argument("--files", type=int, default=3000)
    options = parser.parse_args()
    # As the tests' own fixtures have it: no caches seeded from this machine's, no scan of its
    # uv cache, its free memory not in the way.
    caches.seed = lambda *args, **kwargs: {}
    caches.go_env = dict
    caches.old_uv = lambda: ""
    caches.truststore = lambda *args: None
    caches.uv_stamp = lambda: ("", 0)
    caches.authority_stamp = lambda copilot_home: 0
    runtime_module.own_code = list
    machine.free_memory = lambda: None
    with tempfile.TemporaryDirectory(prefix="cos-bench-send-") as temporary:
        folder = Path(temporary)
        caches.default_tier = lambda: folder / "worker-caches"
        client = SlowCopilot(options.listing_ms / 1000)
        harness = Harness(folder / "data", Settings(), client=client)
        runtime = await harness.connect()
        try:
            populate(runtime, options.workers, options.messages)
            print(
                f"{options.workers} workers, {options.messages} of their messages; session "
                f"listing {options.listing_ms:.0f} ms; load {machine_load()}"
            )
            report("Home: to the Router", await home(runtime, client, options.runs))
            at_work, finished = await chat(runtime, harness, folder, options.runs, options.files)
            report("A worker's chat: its task at work, in a worktree", at_work)
            report("A worker's chat: about its finished task", finished)
        finally:
            await runtime.stop()


def machine_load() -> str:
    try:
        import os

        return " ".join(f"{value:.1f}" for value in os.getloadavg())
    except OSError:
        return "?"


if __name__ == "__main__":
    asyncio.run(main())
