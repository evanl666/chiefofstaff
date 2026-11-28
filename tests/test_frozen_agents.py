"""A coding agent's process that stops answering (alive, answering nothing: a hang) is noticed, as
Copilot's is since 0.1.59: the owner's Stop acts within seconds, the process is started again,
and the session goes on in the new one (its turn given again, the next message answered).

Live, on 0.1.61 (QA R6): the Router on Codex, its app-server frozen mid-turn, showed "Thinking…"
for ever; Stop answered only after 60 s (turn/interrupt waits its request timeout), the owner's
next message stayed "Thinking…" for over 4 minutes, and Codex was never started again. Claude
Code's process frozen the same way: Stop waited 30 s (its interrupt), and nothing more answered.

The stand-in CLIs run under the real supervisor; a process is frozen with SIGSTOP."""

import asyncio
import contextlib
import os
import signal
import sys
import time

import pytest
from conftest import settle
from test_restart_safe import claude_checked, router_on  # noqa: F401  (fixtures)
from test_roles import until

from chiefofstaff import backend_claude, backend_codex
from chiefofstaff import runtime as runtime_module

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Other agents need macOS/Linux")

# Each stand-in's own program, as its command line names it (not its supervisor's).
PROGRAM = {"codex": "fake_codex.py", "claude": "stand-in/claude"}


@pytest.fixture(autouse=True)
def bounded(monkeypatch):
    monkeypatch.setattr(runtime_module, "SESSION_RESUME", 8.0)


@pytest.fixture
def quick(monkeypatch):
    """A hang noticed within seconds (live: PROBE_IDLE 30 s of silence in a turn, then two pings
    of PROBE_TIMEOUT 5 s)."""
    for module in (backend_codex, backend_claude):
        monkeypatch.setattr(module, "PROBE_EVERY", 0.2, raising=False)
        monkeypatch.setattr(module, "PROBE_IDLE", 1.0, raising=False)
        monkeypatch.setattr(module, "PROBE_TIMEOUT", 1.0, raising=False)


@pytest.fixture
def agent_on(request, tmp_path, monkeypatch):
    """The Router on this agent's stand-in."""
    agent = request.param
    if agent == "codex":
        request.getfixturevalue("codex_installed")
        monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex-log.jsonl"))
    else:
        request.getfixturevalue("claude_checked")
    return agent


async def programs(runtime, agent: str) -> list[int]:
    """The agent's own programs running (not their supervisors)."""
    backend, _ = runtime.connection(agent)
    for _ in range(3):  # (A loaded machine's `ps` can come back empty.)
        found = await asyncio.to_thread(backend.processes)
        if pids := [pid for pid, command in found.items() if PROGRAM[agent] in command]:
            return pids
        await asyncio.sleep(0.5)
    raise AssertionError(f"no {agent} program running: {found}")


async def frozen(runtime, agent: str) -> list[int]:
    """Its process's own program stopped (SIGSTOP): alive, answering nothing."""
    pids = await programs(runtime, agent)
    for pid in pids:
        os.kill(pid, signal.SIGSTOP)
    return pids


def thawed(pids: list[int]):
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGCONT)


def turning(handle) -> bool:
    """Its turn has begun (Codex's thread, or Claude's process, has one in flight)."""
    held = getattr(handle, "worker", handle)  # (A Claude session's process holds its turn.)
    return bool(handle and (held.turn or held.starting))


async def in_turn(runtime, router: str):
    await until(lambda: turning(runtime.states.handle(router)), 30)


async def test_r6_a_codex_that_stops_answering_mid_turn_is_noticed(
    tmp_path, codex_installed, monkeypatch
):
    """The Router on Codex, its app-server frozen mid-turn (alive, answering nothing: a hang).
    Copilot that stops answering is noticed and started again (0.1.59); Codex should be too,
    so that the owner's next message is answered. Found: nothing notices; Stop waits out the
    request timeout (60 s live), and the next message stays "Thinking…" for ever (4 minutes
    live, nothing in the log)."""
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex-log.jsonl"))
    monkeypatch.setattr(backend_codex, "REQUEST_TIMEOUT", 3.0)
    harness, runtime = await router_on(tmp_path, "codex")
    router = harness.router_id
    await runtime.owner_send(router, "Wait 20 s. Then reply with just: thawed")
    await until(lambda: (s := runtime.states.handle(router)) and s.turn, 30)
    pids = await frozen(runtime, "codex")
    try:
        began = time.monotonic()
        await runtime.workers.interrupt(router)
        assert time.monotonic() - began < 10
        await runtime.owner_send(router, "Reply with just: nextone")
        await until(lambda: ("assistant", "nextone") in harness.kinds(router), 60)
    finally:
        thawed(pids)
        await runtime.stop()


@pytest.mark.parametrize("agent_on", ["codex", "claude"], indirect=True)
async def test_stop_on_a_frozen_agent_acts_within_seconds_and_the_next_message_is_answered(
    tmp_path, agent_on
):
    """Stop, with the agent's own request timeouts (60 s for Codex's turn/interrupt, 30 s for
    Claude's interrupt): within a few seconds the turn is stopped and its frozen process ended;
    the owner's next message starts a new one and is answered. No turn goes on by itself."""
    agent = agent_on
    harness, runtime = await router_on(tmp_path, agent)
    router = harness.router_id
    await runtime.owner_send(router, "Wait 20 s. Then reply with just: thawed")
    await in_turn(runtime, router)
    await asyncio.sleep(0.5)
    pids = await frozen(runtime, agent)
    try:
        began = time.monotonic()
        await runtime.workers.interrupt(router)
        assert time.monotonic() - began < 8, "Stop acts within seconds"
        await until(lambda: not any(map(running_unstopped, pids)), 10)
        await runtime.owner_send(router, "Reply with just: nextone")
        await until(lambda: ("assistant", "nextone") in harness.kinds(router), 60)
        await settle(runtime)
        kinds = harness.kinds(router)
        assert ("assistant", "thawed") not in kinds, "stopped, not gone on"
        name = "Codex" if agent == "codex" else "Claude"
        said = ("notice", f"{name} stopped answering: it starts again with the next message.")
        assert kinds.index(said) < kinds.index(("owner", "Reply with just: nextone"))
    finally:
        thawed(pids)
        await runtime.stop()


def running_unstopped(pid: int) -> bool:
    """Still there (a frozen process counts until it is gone)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("agent_on", ["codex", "claude"], indirect=True)
async def test_a_frozen_agent_mid_turn_is_started_again_and_its_turn_goes_on(
    tmp_path, agent_on, quick
):
    """Frozen mid-turn, nobody pressing Stop: noticed (a turn silent for a while, then pings
    unanswered), its process ended and started again, said in one calm line, and the turn
    given again: the owner's message is answered."""
    agent = agent_on
    harness, runtime = await router_on(tmp_path, agent)
    router = harness.router_id
    await runtime.owner_send(router, "Wait 4 s. Then reply with just: thawed")
    await in_turn(runtime, router)
    await asyncio.sleep(0.5)
    pids = await frozen(runtime, agent)
    try:
        name = "Codex" if agent == "codex" else "Claude"
        line = f"{name} stopped answering; restarting it…"
        await until(lambda: ("notice", line) in harness.kinds(router), 30)
        await until(lambda: ("assistant", "thawed") in harness.kinds(router), 60)
        kinds = harness.kinds(router)
        assert kinds.index(("notice", line)) < kinds.index(("assistant", "thawed"))
        assert ("notice", f"{name} was restarted; asked the session to continue.") in kinds
        assert not any(map(running_unstopped, pids)), "the frozen process is gone"
    finally:
        thawed(pids)
        await runtime.stop()


@pytest.mark.parametrize("agent_on", ["codex", "claude"], indirect=True)
async def test_an_agent_quiet_in_a_long_turn_but_answering_is_left_alone(tmp_path, agent_on, quick):
    """Silent for a while in its turn (a long command), but answering when asked: never taken
    for a hang (its process is not started again, its turn ends as it would)."""
    agent = agent_on
    harness, runtime = await router_on(tmp_path, agent)
    router = harness.router_id
    await runtime.owner_send(router, "Wait 4 s. Then reply with just: quietly")
    try:
        await in_turn(runtime, router)
        before = await programs(runtime, agent)
        await until(lambda: ("assistant", "quietly") in harness.kinds(router), 60)
        assert not [text for kind, text in harness.kinds(router) if "stopped answering" in text]
        assert all(map(running_unstopped, before)), "the same process"
    finally:
        await runtime.stop()
