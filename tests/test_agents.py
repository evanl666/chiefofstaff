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

from chiefofstaff import agents
from chiefofstaff.models import Settings
from chiefofstaff.roles import ROUTER
from chiefofstaff.runtime import Runtime
from chiefofstaff.sources.github import GitHubSource
from chiefofstaff.sources.slack import SlackSource
from chiefofstaff.web import create_app

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
