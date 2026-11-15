"""Offline benchmarks of the paths Chief of Staff adds to a worker's work, Copilot, Codex and Claude
(more-agents.md, M2 scope D): no model is called and nothing leaves this machine.

Two parts:

- **CoS's own paths, in this process** (the same code for every agent): the permission check
  every tool call goes through (Events.on_permission and the policy, a command and an edit), the
  report tool (a task's status, its card), a turn's admission (Codex and Claude workers), with
  the runtime as tests run it.
- **The real CLIs, offline** (`codex` 0.167.2 with tests/scripted_model.py as its model; `claude`
  2.1.322 with tests/claude_standin.py, a made-up key, every proxy pointing at a catcher), at
  the adapter: a new worker's first turn (process start to its first model request), a warm
  turn (send to the model request), the adapter's own time per tool-call check (Claude's
  PreToolUse hook, Codex's approval request; the policy is the part above), a note steered into
  a running command's turn (send to the model request that has it), Stop (abort to the turn's
  end), a worker let go of when idle and resumed (to its first model request), a command left
  running ending (its end to the turn it wakes), and an idle worker's memory (its process tree's
  resident memory). Copilot's CLI can't run offline: its own numbers come from the live run
  (M3); the paths of Chief of Staff's it shares are in the first part.

p50 and p95 of --runs runs each (milliseconds; memory in MB). Run with the owner's own agents
installed; their configuration and sign-in are never used (each run has folders of its own).

    TMPDIR=… uv run python scripts/bench_agents.py [--runs 20] [--only codex|claude]
"""

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import claude_standin  # noqa: E402
import scripted_model  # noqa: E402
from conftest import CODEX_COMMAND, REAL_CLAUDE, Harness, call_tool  # noqa: E402
from copilot import define_tool  # noqa: E402
from copilot.generated.rpc import PermissionDecisionApproveOnce  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from chiefofstaff import agents, backend_claude  # noqa: E402
from chiefofstaff.backend import isolated, stop_background  # noqa: E402
from chiefofstaff.backend_claude import claude_backend  # noqa: E402
from chiefofstaff.backend_codex import CodexBackend  # noqa: E402
from chiefofstaff.claude_check import Catcher  # noqa: E402

PROVIDERS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    "ANTHROPIC_PROFILE", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_SHELL", "CODEX_HOME",
    "OPENAI_API_KEY",
)  # fmt: skip


def ms(values: list[float]) -> str:
    """p50 / p95 in milliseconds, of how many."""
    if not values:
        return "—"
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return f"{statistics.median(ordered) * 1000:.1f} / {p95 * 1000:.1f} (n={len(ordered)})"


class Timed:
    """Each call of a coroutine method, timed (patched on its class while in use)."""

    def __init__(self, owner, name: str, keep=lambda *a, **k: True):
        self.owner, self.name, self.keep = owner, name, keep
        self.times: list[float] = []
        self.original = getattr(owner, name)

    def __enter__(self):
        original, times, keep = self.original, self.times, self.keep

        async def timed(*args, **kwargs):
            started = time.perf_counter()
            try:
                return await original(*args, **kwargs)
            finally:
                if keep(*args, **kwargs):
                    times.append(time.perf_counter() - started)

        setattr(self.owner, self.name, timed)
        return self

    def __exit__(self, *exc):
        setattr(self.owner, self.name, self.original)


# Part 1: Chief of Staff's own paths, in this process.


async def own_paths(runs: int, folder: Path) -> dict[str, list[float]]:
    harness = Harness(folder / "data")
    runtime = await harness.connect()
    found: dict[str, list[float]] = {}
    try:
        row = harness.store.create_session(
            uuid4().hex, "worker", "bench", "idle", cwd=str(folder / "work")
        )
        invocation = {"session_id": row["id"]}
        shell = SimpleNamespace(  # noqa: S604 (a request as Copilot's SDK gives it)
            kind="shell", full_command_text="git status && npm test -- --watch=false",
            shell="bash", intention="", commands=[], possible_paths=[], possible_urls=[],
            has_write_file_redirection=False, can_offer_session_approval=False,
            tool_call_id="c1",
        )  # fmt: skip
        edit = SimpleNamespace(
            kind="write", file_name="src/app.py", resolved_path=str(folder / "src" / "app.py"),
            diff="", intention="", can_offer_session_approval=False, tool_call_id="c2",
            tool_name="Edit",
        )  # fmt: skip
        for label, request in (("check: a command", shell), ("check: an edit", edit)):
            for asks_first in (False, True):
                runtime.update_session(row["id"], yolo="off" if asks_first else "on")
                times = []
                for _ in range(runs):
                    started = time.perf_counter()
                    await runtime.events.on_permission(request, invocation)
                    times.append(time.perf_counter() - started)
                found[f"{label}{' (Ask first)' if asks_first else ''}"] = times
        # A turn's admission (Codex's and Claude's workers): the same door for both.
        agents.save(harness.store, agents.planned(harness.store, {"codex": {"enabled": True}}))
        await runtime.agents_saved({})
        agent_row = harness.store.create_session(
            uuid4().hex, "worker", "bench", "idle", cwd=str(folder / "work")
        )
        harness.store.set(f"agent:{agent_row['id']}", "codex")
        handle = SimpleNamespace(session_id=agent_row["id"], backend=SimpleNamespace(epoch="b"))
        times = []
        for _ in range(runs):
            started = time.perf_counter()
            why = await runtime.admit_turn(handle)
            times.append(time.perf_counter() - started)
            assert not why, why
            runtime.spending(agent_row["id"], False, "b")
        found["admission of a turn"] = times
        # A report (progress, then done): the task, its card and the Router's news.
        made = json.loads(
            (await call_tool(runtime, "spawn_worker", harness.router_id, title="Bench it",
                             instructions="Bench it", why_new="bench")).text_result_for_llm
        )  # fmt: skip
        worker = made["session_id"]
        times = []
        for index in range(runs):
            started = time.perf_counter()
            await call_tool(runtime, "report", worker, status="progress",
                            summary=f"Step {index} of the bench.")  # fmt: skip
            times.append(time.perf_counter() - started)
        found["report → task updated"] = times
        # The supervisor every Codex and Claude worker's process runs under: how much later
        # than without it the process it runs starts (what a worker's start waits for).
        said = [sys.executable, "-I", "-c", "import time; print(repr(time.time()))"]
        for label, command in (
            ("process start under the supervisor", isolated("chiefofstaff.agentrun", "--", *said)),
            ("process start alone (for comparison)", said),
        ):
            times = []
            for _ in range(min(runs, 30)):
                started = time.time()
                ran = await asyncio.to_thread(
                    subprocess.run, command, capture_output=True, text=True, check=True,
                    cwd=folder,
                )  # fmt: skip
                times.append(float(ran.stdout.strip()) - started)
            found[label] = times
    finally:
        await runtime.stop()
    return found


# Part 2: the real CLIs, offline.


class Stamped:
    """The stand-in model server's requests, timed as they arrive."""

    def __init__(self, server):
        self.server = server
        self.arrived: list[tuple[float, str]] = []
        handler = server.RequestHandlerClass
        original = handler.do_POST
        arrived = self.arrived

        def do_post(request):
            arrived.append((time.perf_counter(), request.path))
            return original(request)

        handler.do_POST = do_post

    def after(self, moment: float) -> float | None:
        later = [at for at, path in self.arrived if at >= moment and "count_tokens" not in path]
        return min(later) if later else None

    async def first_after(self, moment: float, seconds: float = 60) -> float:
        deadline = time.monotonic() + seconds
        while (at := self.after(moment)) is None:
            assert time.monotonic() < deadline, "no model request"
            await asyncio.sleep(0.005)
        return at - moment


class Turn:
    def __init__(self, session):
        self.events: list = []
        self.idle = asyncio.Event()
        session.on(self.seen)

    def seen(self, event):
        self.events.append((time.perf_counter(), event))
        if event.type.value in {"session.idle", "abort", "session.error"}:
            self.idle.set()

    async def wait(self, seconds: float = 120):
        await asyncio.wait_for(self.idle.wait(), seconds)
        self.idle.clear()

    async def settled(self):
        """Its turn ended, and no other followed it (a note it took up after its end)."""
        await self.wait()
        while True:
            await asyncio.sleep(0.3)
            if not self.idle.is_set() and not self.running():
                return
            await self.wait()

    def running(self) -> bool:
        kinds = [e.type.value for _, e in self.events]
        ended = max((i for i, k in enumerate(kinds) if k in {"session.idle", "abort"}), default=-1)
        return any(k == "assistant.turn_start" for k in kinds[ended + 1 :])

    async def started_tool(self, seconds: float = 60):
        deadline = time.monotonic() + seconds
        while not any(e.type.value == "tool.execution_start" for _, e in self.events):
            assert time.monotonic() < deadline, "no tool started"
            await asyncio.sleep(0.01)


class Echo(BaseModel):
    text: str


ECHO = define_tool(
    "echo", description="Echo text back", params_type=Echo,
    handler=lambda params, invocation: f"echo: {params.text}",
)  # fmt: skip


def tree_rss(pid: int) -> float:
    """The resident memory of a process and those it started, in MB."""
    table = subprocess.run(["ps", "-axo", "pid=,ppid=,rss="], capture_output=True, text=True)
    children: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for line in table.stdout.splitlines():
        child, parent, size = (int(x) for x in line.split())
        children.setdefault(parent, []).append(child)
        rss[child] = size
    total, todo = 0, [pid]
    while todo:
        current = todo.pop()
        total += rss.get(current, 0)
        todo += children.get(current, [])
    return total / 1024


async def real_agent(name: str, runs: int, folder: Path) -> dict[str, list[float]]:
    """One agent's real CLI, offline, at the adapter."""
    saved = {key: os.environ.get(key) for key in (*PROVIDERS, "HTTPS_PROXY", "HTTP_PROXY",
             "ALL_PROXY", "https_proxy", "http_proxy", "NO_PROXY", "no_proxy",
             "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC")}  # fmt: skip
    for key in PROVIDERS:
        os.environ.pop(key, None)
    catcher = None
    if name == "codex":
        (folder / "codex-home").mkdir(parents=True)
        os.environ["CODEX_HOME"] = str(folder / "codex-home")
        model = scripted_model.Model()
        backend = CodexBackend(CODEX_COMMAND)
        backend.arguments += model.arguments(folder)
        backend.needs_sign_in = False
    else:
        model, catcher = claude_standin.Model(), Catcher()
        (folder / "claude-config").mkdir(parents=True)
        (folder / "claude-config" / ".claude.json").write_text("{}\n")
        os.environ |= {
            "CLAUDE_CONFIG_DIR": str(folder / "claude-config"),
            "ANTHROPIC_API_KEY": "sk-ant-api03-bench-not-a-key",
            "ANTHROPIC_BASE_URL": model.url,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        }  # fmt: skip
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
            os.environ[key] = os.environ[key.lower()] = catcher.url
        backend = claude_backend(REAL_CLAUDE, folder / "agent")
    stamped = Stamped(model)
    found: dict[str, list[float]] = {
        key: []
        for key in ("first turn of a new worker (process start)", "warm turn: send → model",
                    "a tool of Chief of Staff's: call → result at the model",
                    "a command (`true`, checked): call → result at the model",
                    "check: the adapter's own time", "steering: note accepted (send)",
                    "steering: note → model (after the command)",
                    "Stop: abort → turn ended", "resumed after release: → model",
                    "command left running ended → worker released",
                    "idle worker's memory (MB)")
    }  # fmt: skip
    approve = lambda request, invocation: PermissionDecisionApproveOnce()  # noqa: E731
    work = folder / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    await backend.start()
    if name == "codex":
        checks = Timed(CodexBackend, "serve", lambda self, _id, method, *a: "Approval" in method
                       or method.endswith("requestApproval"))  # fmt: skip
        probes = contextlib.nullcontext(SimpleNamespace(times=[]))
    else:
        checks = Timed(backend_claude.ClaudeWorker, "pre_tool_use")
        probes = Timed(backend_claude.ClaudeWorker, "probe")
    try:
        with checks, probes as probed:
            for run in range(runs):
                started = time.perf_counter()
                session = await backend.create_session(
                    session_id=str(uuid4()), name="bench", working_directory=str(work),
                    on_permission_request=approve, tools=[ECHO],
                )  # fmt: skip
                turn = Turn(session)
                await session.send("Reply with just: ok")
                found["first turn of a new worker (process start)"].append(
                    await stamped.first_after(started)
                )
                await turn.wait()
                sent = time.perf_counter()
                await session.send("Reply with just: again")
                found["warm turn: send → model"].append(await stamped.first_after(sent))
                await turn.wait()
                for metric, asked in (
                    ("a tool of Chief of Staff's: call → result at the model",
                     "Call the echo tool with text 'hi', then reply with just: done"),
                    ("a command (`true`, checked): call → result at the model",
                     "Run the shell command `true` and then reply with just: ran"),
                ):  # fmt: skip
                    sent = time.perf_counter()
                    await session.send(asked)
                    await turn.wait()
                    asked_at = [at for at, path in stamped.arrived if at >= sent]
                    # Its first request (the model calls it), then the one with its result.
                    found[metric].append(asked_at[1] - asked_at[0])
                # Steering: a note while its command runs.
                command = "true && sleep 3" if name == "claude" else "sleep 3"
                await session.send(f"Run the shell command `{command}` and then reply with just: x")
                await turn.started_tool()
                await asyncio.sleep(0.3)
                noted = time.perf_counter()
                await session.send("Then reply with just: noted", mode="immediate")
                found["steering: note accepted (send)"].append(time.perf_counter() - noted)
                found["steering: note → model (after the command)"].append(
                    await stamped.first_after(noted, 30)
                )
                await turn.settled()
                # A command left running, ending: its end (the time it writes) to the adapter's
                # word that nothing holds the worker now (what Chief of Staff wakes it on).
                ended = work / f"ended-{run}"
                stamp = f"python3 -c 'import time; print(repr(time.time()))' > {ended.name}"
                if name == "claude":
                    asked = f"Leave the command `{stamp}` running for 1.5 s"
                else:
                    asked = f"Leave the command `sleep 1.5; {stamp}` running"
                await session.send(f"{asked}, then reply with just: left")
                # Its end: held while the command runs, released once it ended (and, on Claude,
                # once the turn Claude starts on it ended).
                await turn.wait()
                deadline = time.monotonic() + 30
                while not ended.exists() or not ended.read_text().strip():
                    assert time.monotonic() < deadline, (
                        "the command did not end",
                        [(e.type.value, str(getattr(e.data, "content", "") or "")[:200])
                         for _, e in turn.events[-12:]],
                    )  # fmt: skip
                    await asyncio.sleep(0.01)
                released = [at for at, e in turn.events if e.type.value == "session.idle"][-1]
                gap = time.time() - (time.perf_counter() - released) - float(ended.read_text())
                found["command left running ended → worker released"].append(max(gap, 0.0))
                # Stop (last: the stand-ins take the next prompt with what it cut short).
                marker = f"sleep 40.{uuid4().int % 10**6:06d}"
                command = f"true && {marker}" if name == "claude" else marker
                await session.send(f"Run the shell command `{command}` and then reply with just: x")
                await turn.started_tool()
                await asyncio.sleep(0.3)
                aborted = time.perf_counter()
                await session.abort()
                if name == "codex":
                    await stop_background(session)
                deadline = time.monotonic() + 30
                while not (ends := [at for at, e in turn.events if at >= aborted
                                    and e.type.value in {"abort", "session.idle"}]):  # fmt: skip
                    assert time.monotonic() < deadline, "the turn did not end"
                    await asyncio.sleep(0.005)
                found["Stop: abort → turn ended"].append(ends[0] - aborted)
                with contextlib.suppress(TimeoutError):
                    await turn.wait(5)  # (Its end, as Turn hears it: not the next step's.)
                # Its memory, idle.
                await asyncio.sleep(1)
                if name == "codex":
                    pids = [backend.process.pid] if backend.process else []
                else:
                    pids = [w.pid for w in backend.workers.values() if w.pid]
                if pids:
                    found["idle worker's memory (MB)"].append(
                        sum(tree_rss(pid) for pid in pids) / 1000
                    )
                # Let go of when idle, and resumed.
                await session.disconnect()
                again = time.perf_counter()
                resumed = await backend.resume_session(
                    session.session_id, on_permission_request=approve,
                    working_directory=str(work),
                )  # fmt: skip
                turn = Turn(resumed)
                await resumed.send("Reply with just: back")
                found["resumed after release: → model"].append(await stamped.first_after(again))
                await turn.wait()
                await resumed.disconnect()
        found["check: the adapter's own time"] = checks.times
        if probed.times:
            found["resume: its restored totals read (probe)"] = probed.times
    finally:
        await backend.stop()
        model.close()
        if catcher:
            catcher.close()
            assert catcher.seen == [], "nothing left this machine"
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return found


async def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--only", choices=("codex", "claude"))
    args = parser.parse_args()
    results: dict[str, dict[str, list[float]]] = {}
    with tempfile.TemporaryDirectory(prefix="cos-bench-") as temporary:
        root = Path(temporary).resolve()
        results["Chief of Staff (any agent)"] = await own_paths(max(args.runs, 200), root / "own")
        for name, found in (("codex", shutil.which("codex")), ("claude", REAL_CLAUDE)):
            if args.only and name != args.only:
                continue
            if not found:
                print(f"{name}: not installed, left out", flush=True)
                continue
            results[name] = await real_agent(name, args.runs, root / name)
    print(json.dumps(results, indent=1))
    for part, found in results.items():
        print(f"\n## {part}")
        for metric, values in found.items():
            if metric.endswith("(MB)"):
                shown = f"{statistics.median(values) * 1000:.0f} MB" if values else "—"
                print(f"- {metric}: {shown}")
            else:
                print(f"- {metric}: {ms(values)}")


if __name__ == "__main__":
    asyncio.run(main())
