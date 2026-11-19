"""Chief of Staff's own sessions on another coding agent (agent-roles.md): the Router and the
Watchdog where Settings → Coding agents puts them, with nothing but their own tools; a switch
that hands the Router what it knew; Copilot while that agent can't run them, said in one line,
and back by themselves once it can; Watch for you and Slack kept on Copilot. Codex and Claude
are the stand-in CLIs (tests/fake_codex.py, fake_claude.py) on the PATH, under the real
supervisor."""

import asyncio
import json
import sys
import time

import pytest
from conftest import Harness, call_tool, settle

from chiefofstaff import agents, backend_claude
from chiefofstaff import runtime as runtime_module
from chiefofstaff.roles import ROUTER, WATCHDOG, WORKER

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Other agents need macOS/Linux")

PASSED = {"ran": True, "acting": "", "ask_first": "", "steering": True, "usage": True}


@pytest.fixture(autouse=True)
def claude_checked(monkeypatch):
    """Each Claude version's check, as it finds one that keeps the contract."""

    async def check(self, folder):
        return dict(PASSED)

    monkeypatch.setattr(backend_claude.ClaudeAgent, "check", check)
    monkeypatch.setattr(backend_claude, "LISTED", {})


@pytest.fixture
def codex_log(tmp_path, monkeypatch, codex_installed):
    """What Chief of Staff asked the stand-in Codex, one request a line."""
    log = tmp_path / "codex-log.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))

    def read(method: str) -> list[dict]:
        if not log.exists():
            return []
        lines = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        return [entry["params"] for entry in lines if entry.get("method") == method]

    return read


async def started(tmp_path):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    return harness, runtime


async def saved(runtime, part: dict) -> list[str]:
    """The Coding agents group saved, as the page's Save does."""
    before = agents.configured(runtime.store)
    plan = agents.planned(runtime.store, part)
    runtime.check_agents(plan, runtime.store.settings())
    return await runtime.agents_saved(before) if agents.save(runtime.store, plan) else []


async def until(check, seconds: float = 20):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.05)


def notices(harness, session_id) -> list[str]:
    return [text for kind, text in harness.kinds(session_id) if kind == "notice"]


def turns(read, thread: str) -> list[str]:
    """The text of each turn Codex was asked to start in a thread."""
    return [
        "\n".join(str(item.get("text") or "") for item in params.get("input") or [])
        for params in read("turn/start")
        if params.get("threadId") == thread
    ]


def spawn(title: str, instructions: str) -> str:
    arguments = {"title": title, "instructions": instructions, "why_new": "a new task"}
    return f"Call the spawn_worker tool with arguments {json.dumps(arguments)}"


# Settings


def test_a_role_runs_only_on_an_agent_on_that_can_run_it(tmp_path):
    harness = Harness(tmp_path / "data")
    store = harness.store
    assert agents.roles(store) == dict.fromkeys(agents.ROLES, "copilot")
    with pytest.raises(ValueError, match="The Router runs on: Copilot, or an agent turned on"):
        agents.planned(store, {"roles": {"router": "codex"}})
    agents.save(store, agents.planned(store, {"codex": {"enabled": True}}))
    plan = agents.planned(store, {"roles": {"router": "codex", "watchdog": "copilot"}})
    assert plan["roles"] == {"router": "codex"}, "Copilot's: no entry"
    with pytest.raises(ValueError, match="Slack stays on Copilot: what reads your Slack"):
        agents.planned(store, {"roles": {"slack": "codex"}})
    with pytest.raises(ValueError, match="Watch for you stays on Copilot: Codex can't sandbox"):
        agents.planned(store, {"roles": {"connector": "codex"}})
    with pytest.raises(ValueError, match="no role 'learner'"):
        agents.planned(store, {"roles": {"learner": "codex"}})
    agents.save(store, plan)
    assert agents.role_agent(store, "router") == "codex"
    # Codex turned off: the Router is put back on Copilot, and stays there once it is on again.
    agents.save(store, agents.planned(store, {"codex": {"enabled": False}}))
    assert "roles" not in agents.stored(store)
    agents.save(store, agents.planned(store, {"codex": {"enabled": True}}))
    assert agents.role_agent(store, "router") == "copilot"


def test_watch_for_you_waits_for_claudes_slack_safeguards(tmp_path, monkeypatch):
    harness = Harness(tmp_path / "data")
    store = harness.store
    agents.save(store, agents.planned(store, {"claude": {"enabled": True}}))
    with pytest.raises(ValueError, match="Watch for you stays on Copilot: what reads your Slack"):
        agents.planned(store, {"roles": {"connector": "claude"}})
    assert agents.role_agents("connector") == frozenset()
    monkeypatch.setattr(agents, "SLACK_READERS", frozenset({"claude"}))
    assert agents.planned(store, {"roles": {"connector": "claude"}})["roles"] == {
        "connector": "claude"
    }


async def test_settings_lists_the_roles_an_agent_can_run_and_why_one_is_on_copilot(
    tmp_path, codex_installed, monkeypatch
):
    _, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        looked = await runtime.look_for_agents()
        assert [(r["role"], r["agents"], r["chosen"], r["line"]) for r in looked["roles"]] == [
            ("router", ["claude", "codex"], "copilot", ""),
            ("watchdog", ["claude", "codex"], "copilot", ""),
        ], "Slack and Watch for you run on no other agent: no choice for them"
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        await saved(runtime, {"roles": {"router": "codex"}})
        looked = await runtime.look_for_agents()
        router = next(r for r in looked["roles"] if r["role"] == "router")
        assert router["chosen"] == "codex"
        assert router["line"] == (
            "On Copilot for now: Codex is not signed in: run codex login in a terminal."
        )
    finally:
        await runtime.stop()


# The Router on Codex


async def test_the_router_runs_on_codex_with_nothing_but_its_tools(tmp_path, codex_log):
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        before = runtime.copilot_id(router)
        await saved(runtime, {"codex": {"enabled": True}, "roles": {"router": "codex"}})
        assert runtime.agent(router) == "codex"
        thread = runtime.copilot_id(router)
        assert thread != before and harness.store.get(f"alias:{thread}") == router
        assert harness.store.get(f"alias:{before}") == router, "its Copilot session still read"
        (started_thread,) = [
            params
            for params in codex_log("thread/start")
            if params["cwd"] == str(runtime.role_dir(ROUTER))
        ]
        config = started_thread["config"]
        assert {key: config[key] for key in config if key.startswith("features.")} | {
            "web_search": config["web_search"]
        } == {
            "features.memories": False,
            "features.apps": False,
            "features.multi_agent": False,
            "features.multi_agent_v2": False,
            "features.shell_tool": False,
            "features.view_image": False,
            "features.image_generation": False,
            "features.goals": False,
            "features.hooks": False,
            "features.plugins": False,
            "web_search": "disabled",
        }
        assert config["tools"] == {"experimental_request_user_input": {"enabled": False}}
        offered = [tool["name"] for tool in started_thread["dynamicTools"]]
        assert offered == [
            tool.name for tool in runtime.session_tools(harness.store.session(router))
        ]
        assert "ask_user" not in offered and "spawn_worker" in offered
        assert notices(harness, router)[-1] == (
            "The Router runs on Codex now, with a summary of what it knew."
        )
        # It routes: a task becomes a worker (on Copilot), as from its Copilot session.
        workers = len(harness.store.open_sessions(WORKER))
        await runtime.owner_send(router, spawn("Fix the login test", "Fix the login test."))
        await until(lambda: len(harness.store.open_sessions(WORKER)) == workers + 1, 30)
        first = turns(codex_log, thread)[0]
        assert "You take over as the owner's Router from an earlier Router session on Copilot" in (
            first
        ), "its first message brings what it knew"
        assert "Fix the login test" in first
        await until(lambda: harness.store.session(router)["status"] != "working", 30)
        # A worker's report reaches it.
        (worker,) = [
            row
            for row in harness.store.open_sessions(WORKER)
            if row["title"] == "Fix the login test"
        ]
        await call_tool(runtime, "report", worker["id"], status="done", summary="Fixed the test.")
        await until(lambda: any("Fixed the test." in text for text in turns(codex_log, thread)), 30)
        await until(lambda: harness.store.session(router)["status"] != "working", 30)
        # The owner's decision, recorded as on Copilot.
        decision = {"decision": "Login v1: email only, no SMS"}
        await runtime.owner_send(
            router, f"Call the record_decision tool with arguments {json.dumps(decision)}"
        )
        await until(
            lambda: any(d["text"] == decision["decision"] for d in runtime.decisions.active()), 30
        )
    finally:
        await runtime.stop()


async def test_the_router_goes_back_to_copilot_with_what_it_knew_on_codex(tmp_path, codex_log):
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        await saved(runtime, {"codex": {"enabled": True}, "roles": {"router": "codex"}})
        await runtime.owner_send(router, "Remember the word 'pelican'")
        await until(lambda: harness.store.session(router)["status"] != "working", 30)
        thread = runtime.copilot_id(router)
        await saved(runtime, {"roles": {"router": "copilot"}})
        assert runtime.agent(router) == "copilot"
        assert harness.store.get(f"agent:{router}") is None, "Copilot's: no entry"
        assert harness.store.get(f"alias:{thread}") == router
        assert notices(harness, router)[-1] == (
            "The Router runs on Copilot now, with a summary of what it knew."
        )
        await runtime.owner_send(router, "What word?")
        await settle(runtime)
        sent = harness.session(router).sent
        assert sent and "from an earlier Router session on Codex" in sent[0]
        assert "Remember the word 'pelican'" in sent[0], "the conversation comes with it"
    finally:
        await runtime.stop()


async def test_codex_turned_off_puts_the_router_back_on_copilot(tmp_path, codex_log):
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        await saved(runtime, {"codex": {"enabled": True}, "roles": {"router": "codex"}})
        assert runtime.agent(router) == "codex"
        stopped = await saved(runtime, {"codex": {"enabled": False}})
        assert stopped == [], "the Router is never stopped: it moves"
        assert runtime.agent(router) == "copilot"
        assert runtime.states.handle(router) is not None
        assert notices(harness, router)[-1] == (
            "The Router runs on Copilot now, with a summary of what it knew."
        )
    finally:
        await runtime.stop()


# Codex can't run it: Copilot, and back


async def test_a_signed_out_codex_leaves_the_router_on_copilot_until_it_is_back(
    tmp_path, codex_log, monkeypatch
):
    monkeypatch.setattr(runtime_module, "ROLE_RETRY", 0.0)
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        await saved(runtime, {"codex": {"enabled": True}, "roles": {"router": "codex"}})
        assert runtime.agent(router) == "copilot"
        assert notices(harness, router) == [
            "The Router runs on Copilot for now: Codex is not signed in: run codex login in a "
            "terminal."
        ]
        await runtime.owner_send(router, "Hello")
        await settle(runtime)
        assert harness.session(router).sent[-1].endswith("Hello</message>") or any(
            "Hello" in text for text in harness.session(router).sent
        ), "it answers on Copilot meanwhile"
        harness.session(router).emit("session.idle")
        await settle(runtime)
        await runtime.roles_check()
        assert runtime.agent(router) == "copilot", "still signed out: nothing changes"
        assert len(notices(harness, router)) == 1, "said once"
        monkeypatch.delenv("FAKE_CODEX_SIGNED_OUT")
        await runtime.roles_check()
        assert runtime.agent(router) == "codex"
        assert notices(harness, router)[-1] == (
            "The Router runs on Codex now, with a summary of what it knew."
        )
        assert runtime.role_line(ROUTER) == ""
    finally:
        await runtime.stop()


async def test_a_plan_limit_mid_turn_moves_the_router_to_copilot_with_the_message(
    tmp_path, codex_log
):
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        await saved(runtime, {"codex": {"enabled": True}, "roles": {"router": "codex"}})
        await runtime.owner_send(router, "Hit the usage limit, then plan the release")
        await until(lambda: runtime.agent(router) == "copilot", 30)
        await until(lambda: bool(harness.client.sessions.get(runtime.copilot_id(router))), 30)
        await until(lambda: bool(harness.session(router).sent), 30)
        (sent,) = harness.session(router).sent
        assert "plan the release" in sent, "what that turn was given goes to Copilot"
        assert "from an earlier Router session on Codex" in sent
        said = notices(harness, router)[-1]
        assert said.startswith("The Router runs on Copilot for now: Your Codex plan's"), said
        held = runtime.role_held(ROUTER)
        assert held["agent"] == "codex" and held["until"] > time.time() + 3000
        assert not [k for k, _ in harness.kinds(router) if k == "error"], "no error: it went on"
    finally:
        await runtime.stop()


# The Router on Claude


async def test_the_router_runs_on_claude_with_nothing_but_its_tools(
    tmp_path, claude_on_path, monkeypatch
):
    starts = tmp_path / "claude-starts.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_STARTS", str(starts))
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        await saved(runtime, {"claude": {"enabled": True}, "roles": {"router": "claude"}})
        assert runtime.agent(router) == "claude"
        (start,) = [json.loads(line) for line in starts.read_text().splitlines()]
        assert start["tools"] == "" and start["strict_mcp"], "none of Claude's own tools"
        assert start["servers"] == ["chief"], "no MCP server but Chief of Staff's"
        workers = len(harness.store.open_sessions(WORKER))
        await runtime.owner_send(router, spawn("Port the parser", "Port the parser."))
        await until(lambda: len(harness.store.open_sessions(WORKER)) == workers + 1, 30)
        await until(lambda: harness.store.session(router)["status"] != "working", 30)
        # Asked for one of Claude's own tools, it has none: nothing runs.
        await runtime.owner_send(router, "Run the shell command `touch ran`")
        await until(lambda: harness.store.session(router)["status"] != "working", 30)
        assert not (tmp_path / "data" / "work" / ROUTER / "ran").exists()
        assert harness.store.agent_spent(router) > 0, "its tokens, on Claude's part of Usage"
        assert runtime.view(harness.store.session(router))["token_limit"] == 0
    finally:
        await runtime.stop()


# The Watchdog


async def test_the_watchdog_runs_on_codex_with_its_one_tool(tmp_path, codex_log):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}, "roles": {"watchdog": "codex"}})
        watchdog = await runtime.watchdog.session_id()
        session = await runtime.session_for(watchdog)
        assert runtime.agent(watchdog) == "codex"
        assert session.session_id == runtime.copilot_id(watchdog)
        assert not harness.client.created[1:], "never made on Copilot first"
        (started_thread,) = [
            params
            for params in codex_log("thread/start")
            if [tool["name"] for tool in params["dynamicTools"]] == ["assess_worker"]
        ]
        assert started_thread["config"]["features.shell_tool"] is False
        assert runtime.agent(harness.router_id) == "copilot", "the Router stays where it was"
    finally:
        await runtime.stop()


# A downgrade


async def test_prepare_downgrade_puts_the_router_back_on_its_copilot_session(tmp_path, codex_log):
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    copilot = runtime.copilot_id(router)
    await saved(runtime, {"codex": {"enabled": True}, "roles": {"router": "codex"}})
    assert runtime.agent(router) == "codex"
    await runtime.stop()
    stopped, left = agents.prepare_downgrade(harness.store)
    assert stopped == [] and left == {}
    assert harness.store.get(f"agent:{router}") is None
    assert harness.store.get(f"copilot:{router}") == copilot
    assert "roles" not in agents.stored(harness.store)
    assert WATCHDOG  # (The Watchdog, never made here, has nothing to put back.)


async def test_an_agent_that_lost_the_routers_session_gives_it_a_fresh_one_there(
    tmp_path, codex_log
):
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        await saved(runtime, {"codex": {"enabled": True}, "roles": {"router": "codex"}})
        lost = runtime.copilot_id(router)
        await runtime.detach(router)
        backend = runtime.agents["codex"].backend

        async def resume_session(session_id, **options):
            raise RuntimeError("no rollout found for thread id")

        backend.resume_session = resume_session
        await runtime.owner_send(router, "Hello")
        await until(lambda: runtime.copilot_id(router) != lost, 30)
        thread = runtime.copilot_id(router)
        assert runtime.agent(router) == "codex", "still on Codex"
        assert thread in {params["threadId"] for params in codex_log("turn/start")}
        assert notices(harness, router)[-1] == (
            "The Router's Codex session could not be resumed (no rollout found for thread id): "
            "a fresh one took over, with a summary of what it knew."
        )
    finally:
        await runtime.stop()


# Watch for you


def watching(what: str):
    from chiefofstaff.models import Settings, Watch

    return Settings(watches=[Watch(what=what)], watch_after_hours_minutes=0)


async def test_watch_for_you_stays_on_copilot_while_claude_does_not_read_slack(
    tmp_path, claude_on_path, monkeypatch
):
    harness = Harness(tmp_path / "data", watching("Mentions of me"))
    runtime = await harness.connect()
    try:
        monkeypatch.setattr(agents, "SLACK_READERS", frozenset({"claude"}))
        await saved(runtime, {"claude": {"enabled": True}, "roles": {"connector": "claude"}})
        monkeypatch.setattr(agents, "SLACK_READERS", frozenset())  # (Held again.)
        await runtime.connector.scan()
        await settle(runtime)
        row = runtime.connector.session()
        assert runtime.agent(row["id"]) == "copilot"
        assert "Mentions of me" in harness.session(row["id"]).sent[-1]
    finally:
        await runtime.stop()


async def test_watch_for_you_scans_on_claude_once_claude_reads_slack(
    tmp_path, claude_on_path, monkeypatch
):
    monkeypatch.setattr(agents, "SLACK_READERS", frozenset({"claude"}))
    monkeypatch.setitem(PASSED, "sandbox", "")  # Its version's sandbox held.
    report = {"items": [], "methods": "the Slack MCP"}
    harness = Harness(
        tmp_path / "data",
        watching(f"Call the report_found tool with arguments {json.dumps(report)}"),
    )
    runtime = await harness.connect()
    try:
        await saved(runtime, {"claude": {"enabled": True}, "roles": {"connector": "claude"}})
        await runtime.connector.scan()
        await until(lambda: runtime.connector.view()["scanned_at"], 30)
        row = runtime.connector.session()
        assert runtime.agent(row["id"]) == "claude"
        assert harness.store.get("watch_methods") == "the Slack MCP"
        # Back on Copilot (Settings): its next run is a fresh one there.
        await saved(runtime, {"roles": {"connector": "copilot"}})
        await until(lambda: harness.store.session(row["id"])["status"] != "working", 30)
        harness.store.set("watch_scanned_at", "0")
        await runtime.connector.scan()
        await settle(runtime)
        assert runtime.agent(row["id"]) == "copilot"
        assert harness.session(row["id"]).sent, "a fresh run on Copilot"
    finally:
        await runtime.stop()


async def test_signed_in_again_the_router_goes_back_without_waiting_out_its_retry(
    tmp_path, codex_log, monkeypatch
):
    monkeypatch.setattr(runtime_module, "ROLE_GLANCE", 0.0)
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        await saved(runtime, {"codex": {"enabled": True}, "roles": {"router": "codex"}})
        assert runtime.agent(router) == "copilot"
        held = runtime.role_held(ROUTER)
        assert held["kind"] == "start" and held["until"] > time.time() + 200
        await runtime.roles_check()
        assert runtime.agent(router) == "copilot", "a glance: still signed out"
        monkeypatch.delenv("FAKE_CODEX_SIGNED_OUT")
        await runtime.roles_check()
        assert runtime.agent(router) == "codex", "signed in: back at once"
        assert runtime.role_held(ROUTER) == {}
    finally:
        await runtime.stop()
