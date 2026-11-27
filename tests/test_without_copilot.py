"""Chief of Staff with no Copilot (docs/design/agents-anywhere.md 3, 12.2, 12.6): a first run
where only Codex is ready runs everything there; Copilot signed out at a start leaves Chief of
Staff on Codex, and back once it signs in, without a restart; Copilot turned off is never
started; no agent at all, it waits and starts by itself once one can; a worker open in its own
CLI is left alone with no Copilot. Codex is the stand-in CLI (tests/fake_codex.py) on the PATH,
Copilot the harness's fake client."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeClient, Harness, call_tool, settle
from test_roles import saved, until

from chiefofstaff import agents
from chiefofstaff import runtime as runtime_module
from chiefofstaff.models import Settings
from chiefofstaff.roles import ROUTER, WORKER

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Other agents need macOS/Linux")

NOT_FOUND = (
    "Copilot CLI 'copilot' not found. Install it and sign in, or fix the command in Settings."
)


class SignedOut(FakeClient):
    """A Copilot CLI that runs but is signed out until `signed_in`."""

    def __init__(self, signed_in: bool = False):
        super().__init__()
        self.signed_in = signed_in

    async def get_auth_status(self):
        return SimpleNamespace(isAuthenticated=self.signed_in)


class Stops(SignedOut):
    """A signed-in Copilot whose connection stops answering (`dead`, as CopilotBackend's once
    its pings go unanswered); a new one, started, is signed in or not as `signed_in` says."""

    def __init__(self):
        super().__init__(signed_in=True)
        self.dead = ""

    def alive(self):
        return not self.dead and not self.crashed

    async def start(self):
        self.dead = ""


def counting(runtime, name: str) -> list:
    """Each call of the runtime's `name` (a coroutine method), counted."""
    calls: list = []
    method = getattr(runtime, name)

    async def counted(*args):
        calls.append(1)
        return await method(*args)

    setattr(runtime, name, counted)
    return calls


def not_installed(calls: list):
    def factory(settings):
        calls.append(settings.copilot_command)
        raise RuntimeError(NOT_FOUND)

    return factory


def notices(harness, session_id) -> list[str]:
    return [text for kind, text in harness.kinds(session_id) if kind == "notice"]


def copilot_status(runtime) -> dict:
    return runtime.agent_status("copilot")


def owners_codex(folder: Path) -> Path:
    """A `codex` of the owner's own, run in a terminal (it only waits)."""
    script = folder / "codex"
    folder.mkdir()
    script.write_text("#!/bin/sh\nsleep 60\n")
    script.chmod(0o755)
    return script


async def spawned(runtime, harness, title: str = "Fix it") -> str:
    made = await call_tool(runtime, "spawn_worker", harness.router_id, title=title,
                           instructions="Reply with just: done", why_new="test")  # fmt: skip
    return json.loads(made.text_result_for_llm)["session_id"]


async def test_a_first_run_with_only_codex_ready_runs_everything_there(tmp_path, codex_installed):
    calls: list = []
    harness = Harness(tmp_path / "data")
    harness.runtime.backend_factory = not_installed(calls)
    runtime = await harness.connect()
    try:
        assert runtime.status["state"] == "ready", runtime.status
        assert runtime.ready() and not runtime.copilot_ready()
        store = harness.store
        assert agents.enabled(store, "codex") and not agents.copilot_on(store)
        assert agents.default_agent(store) == "codex"
        router = harness.router_id
        assert runtime.agent(router) == "codex"
        assert notices(harness, router)[0] == (
            "Chief of Staff runs on Codex (uses your ChatGPT sign-in). Change it in Settings → "
            "Agents & models."
        )
        worker = await spawned(runtime, harness)
        assert runtime.agent(worker) == "codex"
        await until(lambda: harness.store.session(worker)["status"] in {"idle", "done"}, 30)
        status = copilot_status(runtime)
        assert (status["on"], status["status"], status["fix"]["command"]) == (
            False, "missing", "npm install -g @github/copilot"
        )  # fmt: skip
        # Copilot's own: nothing asked of it, nothing said wrong.
        assert await runtime.available_models() == []
        assert await runtime.workers.copilot_external() == []
        await runtime.recover_now()
        assert len(calls) == 1, "off from then on: never started again"
        # The second start: as the first left it, not a first run any more.
        await runtime.stop()
        again = Harness(tmp_path / "data")
        again.runtime.backend_factory = not_installed(calls)
        runtime = await again.connect()
        assert runtime.ready() and runtime.agent(router) == "codex"
        assert len(calls) == 1
        assert notices(again, router).count(notices(harness, router)[0]) == 1, "said once"
    finally:
        await runtime.stop()


async def test_copilot_signed_out_at_a_start_runs_on_codex_and_back_without_a_restart(
    tmp_path, codex_installed
):
    harness = Harness(tmp_path / "data", client=SignedOut(signed_in=True))
    runtime = await harness.connect()
    router = harness.router_id
    await saved(runtime, {"codex": {"enabled": True}})
    await runtime.stop()
    client = SignedOut()
    again = Harness(tmp_path / "data", client=client)
    runtime = await again.connect()
    try:
        assert runtime.ready() and not runtime.copilot_ready(), runtime.status
        await until(lambda: runtime.agent(router) == "codex")
        assert notices(again, router)[-1] == (
            "The Router runs on Codex for now: Copilot is not signed in."
        )
        status = copilot_status(runtime)
        assert (status["on"], status["status"], status["fix"]["command"]) == (
            True, "signin", "copilot login"
        )  # fmt: skip
        worker = await spawned(runtime, again)
        assert runtime.agent(worker) == "codex", "the next agent that can"
        # Signed in: tried again by itself, the Router back where it was put.
        assert agents.copilot_on(again.store), "a later start never turns Copilot off"
        client.signed_in = True
        await runtime.recover_now()
        await settle(runtime)
        await until(lambda: runtime.agent(router) == "copilot")
        assert runtime.copilot_ready() and copilot_status(runtime)["status"] == "ready"
        assert runtime.agent(await spawned(runtime, again, "Next")) == "copilot"
    finally:
        await runtime.stop()


async def test_a_later_start_without_copilot_keeps_the_owners_choices(tmp_path, codex_installed):
    """Only a true first start turns Copilot off: a later one where Copilot is gone (not
    installed) runs on the agents the owner turned on, Copilot still on and their choices as
    they were; with none but Copilot ever on, nothing is turned on: it waits for Copilot."""
    harness = Harness(tmp_path / "data")
    await (await harness.connect()).stop()
    router = harness.router_id
    calls: list = []
    alone = Harness(tmp_path / "data")
    alone.runtime.backend_factory = not_installed(calls)
    runtime = await alone.connect()
    try:
        assert runtime.status["state"] == "error" and runtime.status.get("waits")
        assert not runtime.status.get("setup"), "Copilot's own card, as before"
        assert alone.store.get(agents.CONFIG) is None, "Codex, ready here, is not turned on"
        assert agents.copilot_on(alone.store)
    finally:
        await runtime.stop()
    on = Harness(tmp_path / "data")
    await (await on.connect()).stop()  # (Copilot back: Codex turned on, nothing else.)
    store = on.store
    store.set(agents.CONFIG, json.dumps({"codex": {"enabled": True}}))
    later = Harness(tmp_path / "data")
    later.runtime.backend_factory = not_installed(calls)
    runtime = await later.connect()
    try:
        assert runtime.ready()
        await until(lambda: runtime.agent(router) == "codex")
        assert agents.copilot_on(later.store) and copilot_status(runtime)["on"]
        assert json.loads(later.store.get(agents.CONFIG)) == {"codex": {"enabled": True}}
        assert not [
            line for line in notices(later, router) if line.startswith("Chief of Staff runs")
        ]
        assert runtime.role_line(ROUTER).startswith("On Codex for now: Copilot CLI")
    finally:
        await runtime.stop()


async def test_stored_settings_make_a_start_no_first_one(tmp_path, codex_installed):
    """A data folder with no Router yet but the owner's choices of models (Settings: kept in the
    agents' configuration from the start, agents.migrate) is not a first start: Copilot not
    ready, it waits for Copilot, nothing turned on or off."""
    harness = Harness(tmp_path / "data", Settings(role_models={"worker": "smart-model"}))
    harness.runtime.backend_factory = not_installed([])
    runtime = await harness.connect()
    try:
        assert runtime.status["state"] == "error" and runtime.status["error"] == NOT_FOUND
        assert not agents.turned_on(harness.store) and agents.copilot_on(harness.store)
    finally:
        await runtime.stop()


async def test_copilot_turned_off_is_never_started_and_turned_on_starts_at_once(
    tmp_path, codex_installed
):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    router = harness.router_id
    try:
        await saved(runtime, {"codex": {"enabled": True}, "copilot": {"enabled": False}})
        await until(lambda: runtime.agent(router) == "codex")
        assert runtime.backend is None and runtime.ready()
        # (Codex is the default agent now: where everything runs, not "for now".)
        assert notices(harness, router)[-1] == (
            "The Router runs on Codex now, with a summary of what it knew."
        )
    finally:
        await runtime.stop()
    calls: list = []
    again = Harness(tmp_path / "data")
    factory = again.runtime.backend_factory

    def counted(settings):
        calls.append(1)
        return factory(settings)

    again.runtime.backend_factory = counted
    runtime = await again.connect()
    try:
        assert calls == [] and runtime.ready() and runtime.agent(router) == "codex"
        assert copilot_status(runtime)["on"] is False
        await saved(runtime, {"copilot": {"enabled": True}})
        await until(lambda: runtime.copilot_ready())
        await until(lambda: runtime.agent(router) == "copilot")
        assert calls == [1]
    finally:
        await runtime.stop()


async def test_with_no_agent_it_waits_and_starts_by_itself_once_one_can(
    tmp_path, codex_installed, monkeypatch
):
    """agents-anywhere.md 12.2: nothing ready at a first run (Copilot not installed, Codex
    signed out): Chief of Staff says why each can't, and starts by itself once Codex is signed
    in, without a restart."""
    monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
    calls: list = []
    harness = Harness(tmp_path / "data")
    harness.runtime.backend_factory = not_installed(calls)
    runtime = await harness.connect()
    try:
        status = runtime.status
        assert (status["state"], status.get("waits"), status.get("setup")) == ("error", True, True)
        assert status["error"].startswith(NOT_FOUND), status["error"]
        assert not runtime.ready() and not harness.router_id
        await runtime.recover_now()
        assert runtime.status["state"] == "error", "still none"
        monkeypatch.delenv("FAKE_CODEX_SIGNED_OUT")
        await runtime.recover_now()
        await settle(runtime)
        assert runtime.ready(), runtime.status
        assert runtime.agent(harness.router_id) == "codex"
        assert not agents.copilot_on(harness.store)
    finally:
        await runtime.stop()


async def test_copilot_alone_signed_out_starts_by_itself_once_signed_in(tmp_path):
    """Copilot the only agent, signed out: its own error, as before, and Chief of Staff starts by
    itself once it is signed in (no Retry, no restart)."""
    harness = Harness(tmp_path / "data")
    await (await harness.connect()).stop()
    client = SignedOut()
    harness = Harness(tmp_path / "data", client=client)
    runtime = await harness.connect()
    try:
        assert runtime.status["error"] == "Copilot is not signed in."
        assert runtime.status.get("waits") and not runtime.status.get("setup")
        await runtime.recover_now()
        assert not runtime.ready()
        client.signed_in = True
        await runtime.recover_now()
        await settle(runtime)
        assert runtime.copilot_ready() and harness.router_id
    finally:
        await runtime.stop()


async def test_with_codex_alone_a_worker_open_in_its_cli_is_left_alone(
    tmp_path, codex_installed, monkeypatch
):
    """agents-anywhere.md 12.6: Copilot off, a worker the owner resumed in their own Codex CLI
    (`codex resume <id>`) is seen as open there (Workers.refresh_elsewhere) and the Watchdog
    leaves it alone."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    terminal = None
    try:
        await saved(runtime, {"codex": {"enabled": True}, "copilot": {"enabled": False}})
        assert runtime.backend is None
        worker = await spawned(runtime, harness)
        assert runtime.agent(worker) == "codex"
        await until(lambda: harness.store.session(worker)["status"] in {"idle", "done"}, 30)
        script = owners_codex(tmp_path / "owner")
        terminal = await asyncio.create_subprocess_exec(
            str(script), "resume", runtime.copilot_id(worker)
        )
        await runtime.workers.refresh_elsewhere()
        row = harness.store.session(worker)
        assert runtime.is_elsewhere(row) == terminal.pid
        harness.store.update_session(worker, status="working")
        assert worker not in await runtime.watchdog.check(), "its owner works with it there"
        assert harness.store.session(worker)["role"] == WORKER
    finally:
        if terminal:
            terminal.kill()
            await terminal.wait()
        await runtime.stop()


async def test_the_router_and_new_workers_follow_copilots_own_problems(tmp_path, codex_installed):
    """Copilot off: the Router's tools offer only the agents that are on (no Copilot), and new
    workers in Settings' view start on the default agent."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    try:
        await saved(runtime, {"codex": {"enabled": True}, "copilot": {"enabled": False}})
        router = harness.store.session(harness.router_id)
        (spawn,) = [t for t in runtime.session_tools(router) if t.name == "spawn_worker"]
        schema = json.dumps(spawn.parameters)
        assert '"codex"' in schema and '"copilot"' not in schema
        view = runtime.new_workers_view()
        assert (view["chosen"], view["now"], view["why"]) == ("codex", "codex", "")
    finally:
        await runtime.stop()


async def test_copilot_that_stops_answering_comes_back_by_the_one_recovery_path(
    tmp_path, codex_installed, monkeypatch
):
    """0.1.59's client restart and agents-anywhere.md 12.2 as one path (Runtime.recover):
    Copilot stops answering while Codex is on, and the new Copilot is signed out: Chief of
    Staff goes on on Codex meanwhile, and back on Copilot once it is signed in, by one recovery
    task, connecting again once (no restart, nothing else connecting meanwhile)."""
    for name, value in (("PROCESS_CHECK", 0.02), ("RECONNECT_DELAY", 0.05)):
        monkeypatch.setattr(runtime_module, name, value)
    monkeypatch.setattr(runtime_module, "COPILOT_RETRY", 0.05)
    client = Stops()
    harness = Harness(tmp_path / "data", client=client)
    runtime = await harness.connect()
    router = harness.router_id
    reconnects = counting(runtime, "reconnect")
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        assert runtime.copilot_ready() and not runtime.recovery
        client.signed_in = False
        client.dead = "Copilot stopped answering."
        await until(lambda: runtime.ready() and runtime.agent(router) == "codex")
        task = runtime.recovery
        assert task and not task.done(), "Copilot is tried again"
        assert copilot_status(runtime)["status"] == "signin"
        client.signed_in = True
        await until(lambda: runtime.copilot_ready() and runtime.agent(router) == "copilot")
        await until(task.done)
        assert runtime.recovery is task, "one recovery task throughout"
        assert reconnects == [1], "connected again once; Copilot came back by itself"
    finally:
        await runtime.stop()


async def test_waiting_for_an_agent_the_one_recovery_task_looks_and_connects_once_one_can(
    tmp_path, codex_installed, monkeypatch
):
    """No agent ready at a first start: the recovery task only looks (agent_found), nothing
    connecting again meanwhile, and connects once Codex is signed in."""
    monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
    monkeypatch.setattr(runtime_module, "RECONNECT_DELAY", 0.05)
    monkeypatch.setattr(runtime_module, "RECONNECT_MOST", 0.1)
    harness = Harness(tmp_path / "data")
    harness.runtime.backend_factory = not_installed([])
    runtime = await harness.connect()
    looks = counting(runtime, "agent_found")
    reconnects = counting(runtime, "reconnect")
    try:
        task = runtime.recovery
        assert task and not task.done() and runtime.status.get("waits")
        await until(lambda: len(looks) >= 3)
        assert reconnects == [] and not runtime.ready()
        monkeypatch.delenv("FAKE_CODEX_SIGNED_OUT")
        await until(runtime.ready)
        assert reconnects == [1] and runtime.recovery is task
        assert runtime.agent(harness.router_id) == "codex"
        await until(task.done)
    finally:
        await runtime.stop()
