"""Other coding agents as the owner sees and sets them (more-agents.md 5.9-5.11, section 8): the
Coding agents group in Settings → Models and what it saves, the Router's choice of an agent,
workers on one in the page, and an agent turned off. Codex is the stand-in CLI
(tests/fake_codex.py) on the PATH, under the real supervisor."""

import asyncio
import json
import os
import re
import sqlite3
import sys
import time

import pytest
from conftest import (
    FakeClient,
    FakeSlackMcp,
    FakeTokens,
    Harness,
    call_tool,
    fake_codex,
    settle,
)
from copilot.tools import ToolInvocation
from fastapi.testclient import TestClient

from chiefofstaff import agents, prompts
from chiefofstaff.digest import digest
from chiefofstaff.models import Settings
from chiefofstaff.roles import ROUTER
from chiefofstaff.runtime import Runtime
from chiefofstaff.sources.github import GitHubSource
from chiefofstaff.sources.slack import SlackSource
from chiefofstaff.store import Store
from chiefofstaff.web import create_app, stopped_text

TOKEN = "launch-token"
ORIGIN = {"origin": "http://127.0.0.1:8765"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home of the test's own (where ~/.codex would be), never the owner's."""
    folder = tmp_path / "home"
    (folder / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(folder))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return folder


@pytest.fixture
def codex_on_path(tmp_path, monkeypatch, home):
    """The stand-in Codex CLI as `codex` on the PATH; each of its runs is listed."""
    command = fake_codex(tmp_path / "fake", monkeypatch)
    monkeypatch.setenv("PATH", f"{tmp_path / 'fake'}:{os.environ['PATH']}")
    runs = tmp_path / "codex-runs"
    monkeypatch.setenv("FAKE_CODEX_RUNS", str(runs))
    return command, runs


def ran(runs) -> list[str]:
    return runs.read_text().splitlines() if runs.exists() else []


@pytest.fixture
def web(tmp_path, home):
    def factory(store, bus):
        async def notifier(title, text):
            pass

        runtime = Runtime(
            store, bus, backend_factory=lambda settings: FakeClient(), notifier=notifier
        )

        async def gh(*arguments, payload=None, token=""):
            raise RuntimeError("not signed in")  # Not the owner's gh.

        runtime.slack = SlackSource(store, runtime, tokens=FakeTokens(""), mcp=FakeSlackMcp())
        runtime.github = GitHubSource(store, runtime, gh_command=gh)
        runtime.sandbox_host = True
        return runtime

    app = create_app(tmp_path / "data", TOKEN, 8765, runtime_factory=factory)
    with TestClient(app, base_url="http://127.0.0.1:8765") as http:
        assert http.post("/api/auth", json={"token": TOKEN}, headers=ORIGIN).status_code == 200
        http.headers["X-Chief-Key"] = TOKEN
        http.headers.update(ORIGIN)
        http.data = tmp_path / "data"
        yield http


def save(http, agents_part: dict, **settings):
    return http.post("/api/settings", json={**settings, "agents": agents_part})


def tables(data) -> set[str]:
    with sqlite3.connect(data / "chiefofstaff.db") as db:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def stored(data) -> dict:
    with sqlite3.connect(data / "chiefofstaff.db") as db:
        row = db.execute("SELECT value FROM kv WHERE key='agents'").fetchone()
    return json.loads(row[0]) if row else {}


# The Coding agents group in Settings → Models


def test_agents_are_looked_for_only_once_the_group_is_opened(web, codex_on_path):
    _, runs = codex_on_path
    state = web.get("/api/state").json()
    assert "agents" not in state, "nothing of agents in the page while none is on"
    assert ran(runs) == [], "no agent CLI runs before the owner opens the group"
    found = web.get("/api/agents").json()
    assert found["new_workers"] == "copilot" and found["sandboxed"] is False
    (codex,) = found["agents"]
    assert codex["name"] == "codex" and codex["label"] == "Codex" and codex["found"]
    assert codex["state"] == "0.167.2 · signed in with ChatGPT" and codex["problem"] == ""
    assert [model["id"] for model in codex["models"]] == ["gpt-test", "gpt-mini"]
    assert codex["enabled"] is False and codex["model"] == "" and codex["limit"] == 0
    assert ran(runs) == ['app-server -c approvals_reviewer="user"']
    assert stored(web.data) == {} and "agent_usage" not in tables(web.data), (
        "looking stores nothing"
    )


@pytest.mark.parametrize(
    ("env", "problem"),
    [
        ({"FAKE_CODEX_SIGNED_OUT": "1"}, "Codex is not signed in: run `codex login`."),
        ({"FAKE_CODEX_VERSION": "0.167.1"}, "Codex 0.167.1 is older than the versions tested"),
        (
            {"FAKE_CODEX_REQUIREMENTS": json.dumps({"allowedApprovalsReviewers": ["auto"]})},
            "Your organization's Codex settings do not allow the approvals reviewer",
        ),
    ],
)
def test_an_agent_that_cannot_run_here_says_why_in_its_row(
    web, codex_on_path, monkeypatch, env, problem
):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    (codex,) = web.get("/api/agents").json()["agents"]
    assert codex["found"] and codex["problem"].startswith(problem) and codex["state"] == ""


def test_with_no_agent_found_the_group_lists_none(web, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    assert web.get("/api/agents").json()["agents"] == []


def test_on_windows_codex_is_found_but_not_offered_and_never_run(web, codex_on_path, monkeypatch):
    _, runs = codex_on_path
    monkeypatch.setattr(sys, "platform", "win32")
    (codex,) = web.get("/api/agents").json()["agents"]
    assert codex["problem"] == "Codex workers need macOS or Linux for now."
    assert ran(runs) == []
    refused = save(web, {"codex": {"enabled": True}})
    assert refused.status_code == 400
    assert refused.json()["detail"] == (
        "Use Codex for workers: Codex workers need macOS or Linux for now."
    )


def test_turning_codex_on_stores_its_configuration_and_nothing_of_the_settings(web, codex_on_path):
    before = web.get("/api/state").json()["settings"]
    saved = save(web, {"codex": {"enabled": True, "limit": 50000}, "new_workers": "codex"})
    assert saved.status_code == 200, saved.text
    snapshot = saved.json()
    assert snapshot["settings"] == before, "the settings stay as they were"
    assert snapshot["agents"] == {
        "on": ["codex"],
        "names": {"codex": "Codex"},
        "new_workers": "codex",
    }
    assert "stopped" not in snapshot
    assert stored(web.data) == {
        "codex": {"enabled": True, "limit": 50000},
        "new_workers": "codex",
    }
    assert "agent_usage" in tables(web.data)
    assert web.get("/api/state").json()["agents"]["on"] == ["codex"]
    off = save(web, {"codex": {"enabled": False}}).json()
    assert "agents" not in off, "none on: the page is as without agents"
    assert stored(web.data)["codex"] == {"enabled": False, "limit": 50000}, "kept, marked off"
    assert stored(web.data)["new_workers"] == "copilot"


def test_saving_the_group_with_nothing_turned_on_stores_nothing(web, codex_on_path):
    saved = save(
        web, {"codex": {"enabled": False, "model": "", "limit": 0}, "new_workers": "copilot"}
    )
    assert saved.status_code == 200 and "agents" not in saved.json()
    assert stored(web.data) == {} and "agent_usage" not in tables(web.data)


@pytest.mark.parametrize(
    ("part", "detail"),
    [
        ({"codex": {"enabled": True, "limit": -1}}, "Limit per worker: a whole number"),
        ({"codex": {"enabled": True, "limit": 1.5}}, "Limit per worker: a whole number"),
        ({"codex": {"enabled": True, "limit": True}}, "Limit per worker: a whole number"),
        ({"codex": {"enabled": True, "limit": 10**12 + 1}}, "Limit per worker: at most"),
        ({"codex": {"enabled": "yes"}}, "Use Codex for workers: on or off."),
        ({"codex": {"enabled": True, "model": 5}}, "Model: a model's name"),
        ({"codex": {"enabled": True, "effort": "high"}}, "Coding agents: unknown setting"),
        ({"claude": {"enabled": True}}, "Coding agents: Chief of Staff does not run 'claude'."),
        ({"new_workers": "codex"}, "New workers use: Copilot, or an agent turned on below."),
        ([], "Coding agents: expected each agent's settings."),
    ],
)
def test_the_group_is_checked_field_by_field(web, codex_on_path, part, detail):
    refused = web.post("/api/settings", json={"agents": part})
    assert refused.status_code == 400 and refused.json()["detail"].startswith(detail)
    assert stored(web.data) == {}


def test_a_model_codex_does_not_offer_is_refused_once_its_models_are_known(web, codex_on_path):
    web.get("/api/agents")  # The group opened: its models are known.
    refused = save(web, {"codex": {"enabled": True, "model": "gpt-nope"}})
    assert refused.status_code == 400
    assert refused.json()["detail"] == (
        "Model: Codex offers no model 'gpt-nope'. Available: gpt-test, gpt-mini."
    )
    assert save(web, {"codex": {"enabled": True, "model": "gpt-mini"}}).status_code == 200


def test_codex_is_not_turned_on_while_workers_are_sandboxed(web, codex_on_path):
    assert web.post("/api/settings", json={"worker_sandbox": True}).status_code == 200
    (codex,) = web.get("/api/agents").json()["agents"]
    assert web.get("/api/agents").json()["sandboxed"] is True
    refused = save(web, {"codex": {"enabled": True}})
    assert refused.status_code == 400
    assert refused.json()["detail"].startswith(
        "Use Codex for workers: other agents can't run while workers are sandboxed"
    )
    assert codex["found"]


def test_codex_folder_stays_out_of_what_the_sandbox_may_write(web, codex_on_path, home):
    folder = str(home / ".codex")
    assert web.post("/api/settings", json={"sandbox_writable": [folder]}).status_code == 200
    refused = save(web, {"codex": {"enabled": True}})
    assert refused.status_code == 400
    assert refused.json()["detail"] == (
        f"Use Codex for workers: first take {folder} out of Also writable in the sandbox: it "
        "holds Codex's settings and hooks."
    )
    assert web.post("/api/settings", json={"sandbox_writable": []}).status_code == 200
    assert save(web, {"codex": {"enabled": True}}).status_code == 200
    refused = web.post("/api/settings", json={"sandbox_writable": [str(home)]})
    assert refused.status_code in (400, 422)  # Home itself is refused as before.
    refused = web.post("/api/settings", json={"sandbox_writable": [folder]})
    assert refused.status_code == 400
    assert refused.json()["detail"] == (
        f"Also writable in the sandbox: the sandbox cannot open {folder}: it holds keys or "
        "settings."
    )
    # Turned off, it may be listed again (as before agents).
    assert save(web, {"codex": {"enabled": False}}).status_code == 200
    assert web.post("/api/settings", json={"sandbox_writable": [folder]}).status_code == 200


def test_the_folder_codex_home_names_is_kept_out_too(web, codex_on_path, monkeypatch, tmp_path):
    other = tmp_path / "codex-home"
    other.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(other))
    assert save(web, {"codex": {"enabled": True}}).status_code == 200
    refused = web.post("/api/settings", json={"sandbox_writable": [str(other)]})
    assert refused.status_code == 400 and "cannot open" in refused.json()["detail"]


def test_without_agents_a_save_checks_nothing_of_them(web, monkeypatch):
    def unexpected(*args):
        raise AssertionError("checked")

    monkeypatch.setattr(Runtime, "check_agents", unexpected)
    assert web.post("/api/settings", json={"max_workers": 3}).status_code == 200
    assert stored(web.data) == {}


# The Router's choice of an agent (5.9)


async def started(tmp_path, settings=None):
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    return harness, runtime


async def saved(runtime, part: dict) -> list[str]:
    """The Coding agents group saved, as the page's Save does."""
    before = agents.configured(runtime.store)
    plan = agents.planned(runtime.store, part)
    runtime.check_agents(plan, runtime.store.settings())
    return await runtime.agents_saved(before) if agents.save(runtime.store, plan) else []


def router_options(harness, runtime) -> dict:
    return runtime.options(harness.store.session(harness.router_id))


def schema(options, name="spawn_worker") -> dict:
    return next(tool for tool in options["tools"] if tool.name == name).parameters


async def spawn_on(harness, runtime, **arguments):
    """The Router's spawn_worker as it has it now (with `agent` while one is on)."""
    tool = next(t for t in router_options(harness, runtime)["tools"] if t.name == "spawn_worker")
    result = await tool.handler(
        ToolInvocation(
            session_id=harness.router_id,
            tool_call_id="t1",
            tool_name="spawn_worker",
            arguments={"why_new": "t", **arguments},
        )
    )
    return result if result.result_type == "failure" else json.loads(result.text_result_for_llm)


async def test_without_an_agent_on_the_routers_tools_and_prompt_are_as_before(
    tmp_path, codex_on_path
):
    harness, runtime = await started(tmp_path)
    try:
        before = router_options(harness, runtime)
        tools_before = [(t.name, t.description, t.parameters) for t in before["tools"]]
        prompt_before = before["system_message"]["content"]
        assert "agent" not in schema(before)["properties"]
        await saved(runtime, {"codex": {"enabled": True}})
        on = router_options(harness, runtime)
        properties = schema(on)["properties"]
        assert properties["agent"]["enum"] == ["", "copilot", "codex"]
        assert "only when the owner named one" in properties["agent"]["description"]
        assert {k: v for k, v in properties.items() if k != "agent"} == schema(before)["properties"]
        assert [t.name for t in on["tools"]] == [name for name, _, _ in tools_before]
        added = on["system_message"]["content"].replace(
            prompt_before.rpartition("\n</chiefofstaff_role>")[0], ""
        )
        assert added.startswith("\n- Workers can run on Codex as well as Copilot")
        assert on["system_message"]["content"].count("spawn_worker's agent") == 1
        await saved(runtime, {"codex": {"enabled": False}})
        off = router_options(harness, runtime)
        assert [(t.name, t.description, t.parameters) for t in off["tools"]] == tools_before
        assert off["system_message"]["content"] == prompt_before
    finally:
        await runtime.stop()


async def test_turning_an_agent_on_or_off_refreshes_the_router(tmp_path, codex_on_path):
    _, runtime = await started(tmp_path)
    try:
        refreshed = []
        original = runtime.refresh

        async def refresh(*roles):
            refreshed.append(roles)
            await original(*roles)

        runtime.refresh = refresh  # type: ignore[method-assign]
        await saved(runtime, {"codex": {"enabled": True}})
        await saved(runtime, {"codex": {"enabled": True, "limit": 9}})
        await saved(runtime, {"codex": {"enabled": False}})
        assert refreshed == [(ROUTER,), (ROUTER,)], "not when the agents on stay the same"
    finally:
        await runtime.stop()


async def test_a_task_runs_on_the_agent_the_owner_named(tmp_path, codex_on_path):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        result = await spawn_on(
            harness, runtime, title="Fix it", instructions="Report done: Fixed it.", agent="codex"
        )
        worker = result["session_id"]
        assert runtime.agent(worker) == "codex"
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        copilot = await spawn_on(
            harness, runtime, title="Other", instructions="Do it.", agent="copilot"
        )
        assert runtime.agent(copilot["session_id"]) == "copilot"
        assert harness.store.get(f"agent:{copilot['session_id']}") is None, "Copilot's: no entry"
        listed = await call_tool(runtime, "list_sessions", harness.router_id)
        listed = json.loads(listed.text_result_for_llm)
        entries = {entry["session_id"]: entry for entry in listed["sessions"]}
        assert entries[worker]["agent"] == "codex" and "agent" not in entries[copilot["session_id"]]
        read = await call_tool(runtime, "read_session", harness.router_id, session_id=worker)
        assert json.loads(read.text_result_for_llm)["agent"] == "codex"
        other = await call_tool(
            runtime, "read_session", harness.router_id, session_id=copilot["session_id"]
        )
        assert "agent" not in json.loads(other.text_result_for_llm)
        assert listed["agents"] == {
            "new_workers": "copilot",
            "models": {"codex": ["gpt-test", "gpt-mini"]},
        }
    finally:
        await runtime.stop()


async def test_a_named_agent_that_cannot_run_is_an_error_not_copilot(
    tmp_path, codex_on_path, monkeypatch
):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        before = len(harness.store.open_sessions())
        result = await spawn_on(
            harness, runtime, title="Fix it", instructions="Do it.", agent="codex"
        )
        assert result.result_type == "failure"
        assert result.error == "Codex could not start: Codex is not signed in: run `codex login`."
        assert len(harness.store.open_sessions()) == before, "no worker made"
        slack = await spawn_on(
            harness, runtime, title="Answer", instructions="Do it.", agent="codex", slack=True
        )
        assert slack.result_type == "failure"
    finally:
        await runtime.stop()


async def test_when_the_default_agent_cannot_run_a_new_worker_starts_on_copilot_and_says_why(
    tmp_path, codex_on_path, monkeypatch
):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}, "new_workers": "codex"})
        monkeypatch.setenv("FAKE_CODEX_VERSION", "0.160.0")
        result = await spawn_on(harness, runtime, title="Fix it", instructions="Do it.")
        worker = result["session_id"]
        assert runtime.agent(worker) == "copilot"
        notices = [text for kind, text in harness.kinds(worker) if kind == "notice"]
        assert notices == [
            "Started on Copilot: Codex could not start: Codex 0.160.0 is older than the versions "
            "tested (0.167.2): update it."
        ]
    finally:
        await runtime.stop()


async def test_new_workers_use_the_default_agent_and_workers_that_read_slack_copilot(
    tmp_path, codex_on_path
):
    harness, runtime = await started(tmp_path, Settings(slack_enabled=True))
    try:
        await saved(runtime, {"codex": {"enabled": True}, "new_workers": "codex"})
        worker = (await spawn_on(harness, runtime, title="Fix it", instructions="Do it."))[
            "session_id"
        ]
        assert runtime.agent(worker) == "codex"
        assert [k for k, _ in harness.kinds(worker)].count("notice") == 0
        reads = await spawn_on(harness, runtime, title="Answer", instructions="Do it.", slack=True)
        assert runtime.agent(reads["session_id"]) == "copilot"
        assert "notice" not in [k for k, _ in harness.kinds(reads["session_id"])], "nothing to say"
    finally:
        await runtime.stop()


async def test_a_model_for_a_worker_on_codex_is_one_codex_offers_and_kept_apart(
    tmp_path, codex_on_path
):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        refused = await spawn_on(
            harness,
            runtime,
            title="Fix it",
            instructions="Do it.",
            agent="codex",
            model="claude-sonnet-4.5",
        )
        assert refused.result_type == "failure"
        assert refused.error.startswith("Unknown model 'claude-sonnet-4.5' for Codex. Available:")
        result = await spawn_on(
            harness, runtime, title="Fix it", instructions="Do it.", agent="codex",
            model="gpt-mini", effort="high",
        )  # fmt: skip
        worker = result["session_id"]
        row = harness.store.session(worker)
        assert row["model"] == "" and row["effort"] == "", "its row's are Copilot's"
        assert runtime.agent_choice(worker) == {"model": "gpt-mini", "effort": "high"}
        assert runtime.agent_options(row)["model"] == "gpt-mini"
        dropped = await spawn_on(
            harness, runtime, title="Other", instructions="Do it.", agent="codex",
            model="gpt-mini", effort="max",
        )  # fmt: skip
        assert dropped["warning"] == "Effort max is not offered by this model (low, medium, high)."
        assert runtime.agent_choice(dropped["session_id"]) == {"model": "gpt-mini", "effort": ""}
    finally:
        await runtime.stop()


async def until(check, seconds: float = 20):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.05)


# An agent turned off after use (5.11)


def database(data) -> dict[str, dict]:
    """Every table's rows as they are now, by their first column (an id or a key)."""
    with sqlite3.connect(data / "chiefofstaff.db") as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        found = {}
        for name in names:
            cursor = db.execute(f"SELECT * FROM {name}")  # noqa: S608
            columns = [column[0] for column in cursor.description]
            found[name] = {row[0]: dict(zip(columns, row, strict=True)) for row in cursor}
        return found


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_turning_codex_off_drains_it_to_as_without_agents(tmp_path, codex_on_path):
    harness, runtime = await started(tmp_path)
    try:
        plain = router_options(harness, runtime)
        await saved(runtime, {"codex": {"enabled": True}, "new_workers": "codex"})
        worker = (
            await spawn_on(
                harness,
                runtime,
                title="Serve it",
                instructions="Leave the command `sleep 300` running. Report done: Serving.",
            )
        )["session_id"]
        await until(lambda: runtime.held(worker), 30)
        backend = runtime.agents["codex"].backend
        processes = backend.processes()
        assert any("sleep 300" in command for command in processes.values())
        before = database(harness.store.directory)
        generation = runtime.admission_generation

        stopped = await saved(runtime, {"codex": {"enabled": False}})
        await settle(runtime)

        assert stopped == ["Codex is turned off in Settings → Models."]
        assert runtime.admission_generation > generation, "nothing admitted before begins"
        assert runtime.connection("codex") == (None, 1) and not backend.alive()
        await until(lambda: not any(alive(pid) for pid in processes), 10)
        row = harness.store.session(worker)
        assert runtime.agent_block(row) == "Codex is turned off in Settings → Models."
        with pytest.raises(RuntimeError, match="turned off"):
            await runtime.session_for(worker)
        off = router_options(harness, runtime)
        assert [(t.name, t.parameters) for t in off["tools"]] == [
            (t.name, t.parameters) for t in plain["tools"]
        ]
        assert off["system_message"]["content"] == plain["system_message"]["content"]
        listed = json.loads(
            (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
        )
        assert "agents" not in listed
        assert all("agent" not in entry for entry in listed["sessions"])
        changed = changes(before, database(harness.store.directory))
        assert drained_only(changed) == [], "only the stop and what settles with it"
        assert ("kv", "agentpids:codex", "*") in {change[:3] for change in changed}
        assert any(change[:3] == ("messages", change[1], "*") for change in changed)
        (ended,) = harness.store.all("SELECT ended_at FROM commands WHERE session_id=?", (worker,))
        assert ended["ended_at"], "its command ended with its process"
    finally:
        await runtime.stop()


def changes(before: dict, after: dict) -> list[tuple[str, object, str, object, object]]:
    """Each value that changed: (table, row, column, before, after); a row gone or new is its
    column "*"."""
    found = []
    for table in sorted(set(before) | set(after)):
        old, new = before.get(table, {}), after.get(table, {})
        for key in sorted(set(old) | set(new), key=str):
            if key not in old or key not in new:
                found.append((table, key, "*", old.get(key), new.get(key)))
                continue
            for column, value in new[key].items():
                if old[key][column] != value:
                    found.append((table, key, column, old[key][column], value))
    return found


# What turning an agent off may write (5.11): the stop itself (the worker's status, its stopped
# tasks, the "Stopped by Settings" notice); what settles as its processes stop (usage, commands
# and task ends); news held for its workers; its process records, cleared; its configuration,
# marked off.
DRAIN_WRITES = {
    ("sessions", "status"), ("sessions", "activity"), ("sessions", "updated_at"),
    ("tasks", "status"), ("tasks", "progress"), ("tasks", "updated_at"), ("tasks", "ended_at"),
    ("commands", "ended_at"), ("commands", "exit_code"), ("commands", "output"),
}  # fmt: skip
DRAIN_KEYS = re.compile(r"^(agents|agentpids:\w+|agentepoch:.+|stopped:.+|goingon:.+|heldnews:.+)$")


def drained_only(changed: list) -> list:
    """Of these changes, those turning an agent off may not make."""
    return [
        change
        for change in changed
        if not (
            (change[0], change[2]) in DRAIN_WRITES
            or (change[0] == "kv" and DRAIN_KEYS.match(str(change[1])))
            or (change[0] == "agent_usage" and change[2] == "*" and change[3] is None)
            or (
                change[0] == "messages"
                and change[2] == "*"
                and change[3] is None
                and (change[4]["kind"], change[4]["text"]) == ("notice", "Stopped by Settings.")
            )
        )
    ]


# Workers on Codex in the page and the Router's tools (5.4, 5.8, 5.10)


async def codex_worker(tmp_path, instructions="Report done: Fixed it.", settings=None):
    harness, runtime = await started(tmp_path, settings)
    await saved(runtime, {"codex": {"enabled": True}, "new_workers": "codex"})
    result = await spawn_on(harness, runtime, title="Fix it", instructions=instructions)
    worker = result["session_id"]
    await until(lambda: harness.store.session(worker)["status"] == "done", 30)
    return harness, runtime, worker, result["task_id"]


def test_a_codex_worker_is_told_in_codex_terms_what_copilot_is_told_in_its_own():
    for copilot in prompts.AGENT_WORDS["codex"]:
        assert prompts.WORKER.count(copilot) == 1, copilot
    said = prompts.worker_on("codex", prompts.WORKER)
    for word in ("Copilot", "ask_user", 'mode="async"', "detach: true"):
        assert word not in said, word
    assert 'report(status="question")' in said and "codex mcp login" in said
    assert prompts.worker_on("copilot", prompts.WORKER) == prompts.WORKER


async def test_a_codex_worker_has_its_words_and_a_copilot_worker_its_own(tmp_path, codex_on_path):
    harness, runtime, worker, _ = await codex_worker(tmp_path)
    try:
        copilot = (
            await spawn_on(harness, runtime, title="Other", instructions="Do it.", agent="copilot")
        )["session_id"]
        on_codex = runtime.agent_options(harness.store.session(worker))
        assert on_codex["system_message"]["content"].startswith(
            prompts.worker_on("codex", prompts.WORKER)
        )
        own = runtime.options(harness.store.session(copilot))["system_message"]["content"]
        assert own.startswith(prompts.WORKER), "a Copilot worker's prompt is as it was"
    finally:
        await runtime.stop()


async def test_a_codex_workers_view_says_its_agent_model_and_tokens(tmp_path, codex_on_path):
    harness, runtime, worker, task_id = await codex_worker(tmp_path)
    try:
        view = runtime.view(harness.store.session(worker))
        assert view["agent"] == "codex" and view["agent_name"] == "Codex"
        assert view["model"] == "gpt-test", "the model Codex runs it on"
        assert view["credits"] == 0 and view["tokens"] > 0
        assert view["agent_notes"] == [] and view["ask_first_refused"] == ""
        task = runtime.task_actions.view(runtime.tasks.get(task_id))
        assert task["agent"] == "codex" and task["agent_name"] == "Codex"
        assert task["tokens"] == view["tokens"] and task["credits"] == 0
        router = runtime.view(harness.store.session(harness.router_id))
        assert "agent" not in router and "tokens" not in router
        usage = runtime.usage(0, time.time() + 1)
        (part,) = usage["agents"]
        assert part["agent"] == "codex" and part["name"] == "Codex" and part["unknown"] == []
        assert [(row["model"], row["tokens"]) for row in part["models"]] == [
            ("gpt-test", view["tokens"])
        ]
        assert usage["total"]["credits"] == 0, "Copilot's totals have none of it"
    finally:
        await runtime.stop()


async def test_without_agents_views_and_usage_are_as_before(tmp_path):
    harness, runtime = await started(tmp_path)
    try:
        assert "agents" not in runtime.usage(0, time.time() + 1)
        assert "agents" not in runtime.snapshot()
        assert "agent" not in runtime.view(harness.store.session(harness.router_id))
        assert "agent_usage" not in tables(harness.store.directory)
    finally:
        await runtime.stop()


async def test_the_daily_summary_says_codex_tokens_only_on_a_day_it_spent_some(
    tmp_path, codex_on_path
):
    harness, runtime, worker, _ = await codex_worker(tmp_path)
    try:
        tokens = harness.store.agent_spent(worker)
        said = digest(runtime, 0, time.time() + 1)
        assert said.splitlines()[-1].endswith(f"credits and {tokens:,} tokens on Codex")
        quiet = digest(runtime, time.time() + 10, time.time() + 20)
        assert "tokens on" not in quiet
    finally:
        await runtime.stop()


async def test_a_codex_workers_model_is_chosen_from_codex_and_kept_apart(tmp_path, codex_on_path):
    harness, runtime, worker, _ = await codex_worker(tmp_path)
    try:
        refused_models = harness.store.get("refused_models")
        with pytest.raises(ValueError, match="Unknown model 'gpt-5' for Codex"):
            await runtime.set_session_model(worker, "gpt-5")
        with pytest.raises(ValueError, match=r"That model's thinking efforts: low, medium, high\."):
            await runtime.set_session_model(worker, "gpt-mini", "max")
        with pytest.raises(ValueError, match="no long-context option"):
            await runtime.set_session_model(worker, "gpt-mini", "", "long_context")
        view = await runtime.set_session_model(worker, "gpt-mini", "high")
        assert view["model"] == "gpt-mini" and view["model_choice"] == "gpt-mini"
        assert view["effort_choice"] == "high"
        row = harness.store.session(worker)
        assert row["model"] == "" and row["effort"] == ""
        assert harness.store.get("refused_models") == refused_models
        assert runtime.agent_options(row)["model"] == "gpt-mini"
        await runtime.set_session_model(worker, "")
        assert harness.store.get(f"agentmodel:{worker}") is None
        assert "model" not in runtime.agent_options(harness.store.session(worker))
    finally:
        await runtime.stop()


async def test_a_settings_model_change_leaves_codex_workers_on_their_model(tmp_path, codex_on_path):
    harness, runtime, worker, _ = await codex_worker(tmp_path)
    try:
        session = runtime.states.handle(worker)
        assert session is not None
        old = harness.store.settings()
        new = old.model_copy(update={"model": "claude-test"})
        harness.store.save_settings(new)
        await runtime.settings_changed(old, new)
        assert session.model == "", "Copilot's default model is not a Codex worker's"
        assert harness.store.get(f"pinned:{worker}") is None
    finally:
        await runtime.stop()


async def test_news_for_a_codex_worker_that_cannot_run_waits_for_it(tmp_path, codex_on_path):
    harness, runtime, worker, task_id = await codex_worker(tmp_path)
    try:
        key = "github:acme/api#7"
        runtime.follow(worker, [key], task_id=task_id)
        await saved(runtime, {"codex": {"enabled": False}})
        took = await runtime.on_thread("github", key, ["github:issuecomment:1"], "bob: a test?")
        assert took and harness.store.follows(worker) == [key], "it still follows the PR"
        (held,) = json.loads(harness.store.get(f"heldnews:{worker}"))
        assert "bob: a test?" in held["display"]
        await runtime.workers.refresh_elsewhere()
        assert harness.store.get(f"heldnews:{worker}"), "kept while it cannot run"
        await saved(runtime, {"codex": {"enabled": True}})
        await runtime.workers.refresh_elsewhere()
        await settle(runtime)
        assert not harness.store.get(f"heldnews:{worker}"), "delivered once it can"
        await until(
            lambda: any("bob: a test?" in m["text"] for m in harness.store.messages(worker))
        )
    finally:
        await runtime.stop()


async def test_ask_first_on_a_codex_worker_asks_its_agent_first(
    tmp_path, codex_on_path, monkeypatch
):
    harness, runtime, worker, _ = await codex_worker(tmp_path)
    try:
        await runtime.disconnect_agent("codex")
        monkeypatch.setenv("FAKE_CODEX_VERSION", "0.168.1")
        with pytest.raises(ValueError, match="has not passed Chief of Staff's checks for Ask"):
            await runtime.set_session_yolo(worker, "off")
        assert runtime.view(harness.store.session(worker))["ask_first_refused"]
    finally:
        await runtime.stop()


def test_a_save_that_stops_workers_says_how_many_and_why():
    assert stopped_text(["Codex is turned off in Settings → Models."]) == (
        "Stopped 1 worker: Codex is turned off in Settings → Models. Tell it to go on from Needs "
        "you once that changes."
    )
    assert stopped_text(["Codex is turned off in Settings → Models."] * 2) == (
        "Stopped 2 workers: Codex is turned off in Settings → Models. Tell them to go on from "
        "Needs you once that changes."
    )


async def test_codex_that_stopped_by_itself_and_could_not_start_again_says_so(
    tmp_path, codex_on_path, monkeypatch
):
    _, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        backend = await runtime.connect_agent("codex")
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        backend.process.kill()
        await until(lambda: runtime.agents["codex"].offline, 20)
        monkeypatch.delenv("FAKE_CODEX_SIGNED_OUT")
        (codex,) = (await runtime.look_for_agents())["agents"]
        assert codex["problem"] == (
            "Stopped unexpectedly; starts again with its next worker's message."
        )
        await runtime.connect_agent("codex")
        assert not runtime.agents["codex"].offline
        (codex,) = (await runtime.look_for_agents())["agents"]
        assert codex["state"] == "0.167.2 · signed in with ChatGPT", "asked of the one running"
    finally:
        await runtime.stop()


async def test_a_codex_whose_check_found_edits_not_asking_says_why(tmp_path, codex_on_path):
    harness, runtime = await started(tmp_path)
    try:
        why = "Codex 0.167.2 applied an edit without asking Chief of Staff first."
        harness.store.set("agentcheck:codex:0.167.2", json.dumps({"ran": True, "acting": why}))
        (codex,) = (await runtime.look_for_agents())["agents"]
        assert codex["problem"] == why and codex["state"] == ""
    finally:
        await runtime.stop()


def test_new_workers_use_an_agent_only_while_it_is_on(tmp_path):
    store = Store(tmp_path / "data")
    assert agents.new_workers(store) == "copilot"
    agents.save(store, agents.planned(store, {"codex": {"enabled": True}, "new_workers": "codex"}))
    assert agents.new_workers(store) == "codex"
    agents.configure(store, "codex", enabled=False)  # Its choice is left as it was.
    assert agents.stored(store)["new_workers"] == "codex"
    assert agents.new_workers(store) == "copilot"


async def test_an_agent_never_turned_on_is_said_to_be_off(tmp_path, codex_on_path):
    harness, runtime = await started(tmp_path)
    try:
        assert "codex" not in runtime.agents
        refused = await runtime.toolset.spawn_on(["codex"]).handler(
            ToolInvocation(
                session_id=harness.router_id,
                tool_call_id="t1",
                tool_name="spawn_worker",
                arguments={"title": "Fix it", "instructions": "Do it.", "agent": "codex"},
            )
        )
        assert refused.error == "Codex is turned off in Settings → Models."
    finally:
        await runtime.stop()


async def test_codex_workers_take_a_new_model_from_the_group_with_their_next_turn(
    tmp_path, codex_on_path
):
    harness, runtime, worker, _ = await codex_worker(tmp_path)
    try:
        own = (await spawn_on(harness, runtime, title="Own", instructions="Report done: Ok.",
                              model="gpt-test"))["session_id"]  # fmt: skip
        await until(lambda: harness.store.session(own)["status"] == "done", 30)
        assert runtime.states.handle(worker) and runtime.states.handle(own)
        await saved(runtime, {"codex": {"enabled": True, "model": "gpt-mini"}})
        assert runtime.states.handle(worker) is None, "reconnects with the new model"
        assert runtime.states.handle(own), "its own model stays"
        assert runtime.agent_options(harness.store.session(worker))["model"] == "gpt-mini"
    finally:
        await runtime.stop()


def test_an_agents_models_are_listed_only_while_it_is_on(web, codex_on_path):
    assert web.get("/api/models?agent=codex").status_code == 404
    assert web.get("/api/models?agent=claude").status_code == 404
    assert save(web, {"codex": {"enabled": True}}).status_code == 200
    listed = web.get("/api/models?agent=codex")
    assert listed.status_code == 200, listed.text
    assert [model["id"] for model in listed.json()] == ["gpt-test", "gpt-mini"]
