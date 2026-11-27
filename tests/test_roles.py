"""Chief of Staff's own sessions on another coding agent (agent-roles.md): the Router and the
Watchdog where Settings → Agents & models puts them, with nothing but their own tools; a switch
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
    """The Agents & models group saved, as the page's Save does."""
    before = agents.configured(runtime.store)
    models = agents.work_view(runtime.store)["models"]
    plan = agents.planned(runtime.store, part)
    runtime.check_agents(plan, runtime.store.settings())
    changed = agents.save(runtime.store, plan)
    return await runtime.agents_saved(before, models=models) if changed else []


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


def test_every_role_runs_on_any_agent_on(tmp_path):
    """agents-anywhere.md 2: each of Chief of Staff's own sessions (the Router, the Watchdog,
    the Slack session, Watch for you, the helpers) runs on any agent on; none is refused."""
    harness = Harness(tmp_path / "data")
    store = harness.store
    assert agents.roles(store) == dict.fromkeys(agents.ROLES, "copilot")
    with pytest.raises(ValueError, match="The Router runs on: an agent turned on below"):
        agents.planned(store, {"roles": {"router": "codex"}})
    agents.save(store, agents.planned(store, {"codex": {"enabled": True}}))
    plan = agents.planned(store, {"roles": {"router": "codex", "watchdog": "copilot"}})
    assert plan["roles"] == {"router": "codex"}, "Copilot's: no entry"
    every = dict.fromkeys(["slack", "connector", "helpers", "router", "watchdog"], "codex")
    assert agents.planned(store, {"roles": every})["roles"] == dict(sorted(every.items()))
    with pytest.raises(ValueError, match="no role 'learner'"):
        agents.planned(store, {"roles": {"learner": "codex"}})
    agents.save(store, plan)
    assert agents.role_agent(store, "router") == "codex"
    # Codex turned off: the Router is put back on Copilot, and stays there once it is on again.
    agents.save(store, agents.planned(store, {"codex": {"enabled": False}}))
    assert "roles" not in agents.stored(store)
    agents.save(store, agents.planned(store, {"codex": {"enabled": True}}))
    assert agents.role_agent(store, "router") == "copilot"


def test_a_choice_of_an_earlier_version_it_held_back_runs_as_chosen_said_once(tmp_path):
    """agents-anywhere.md 7: Watch for you or the Slack session chosen for an agent while an
    earlier version kept them on Copilot runs there now; the migration says which, once."""
    harness = Harness(tmp_path / "data")
    store = harness.store
    store.set(
        agents.CONFIG,
        json.dumps(
            {"claude": {"enabled": True}, "roles": {"connector": "claude", "router": "claude"}}
        ),
    )
    assert agents.migrate(store) == [("connector", "claude")], "the Router ran there already"
    assert agents.role_agent(store, "connector") == "claude"
    assert agents.migrate(store) == [], "once"


async def test_settings_lists_every_role_and_where_one_runs_for_now(
    tmp_path, codex_installed, monkeypatch
):
    _, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        looked = await runtime.look_for_agents()
        assert [(r["role"], r["chosen"], r["now"], r["line"]) for r in looked["roles"]] == [
            (role, "copilot", "copilot", "") for role in agents.ROLES
        ], "every role listed, each on Copilot"
        assert looked["workers"] == {"chosen": "copilot", "now": "copilot", "now_model": "",
                                     "why": ""}  # fmt: skip
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        await saved(runtime, {"roles": {"router": "codex"}, "new_workers": "codex"})
        looked = await runtime.look_for_agents()
        router = next(r for r in looked["roles"] if r["role"] == "router")
        assert (router["chosen"], router["now"], router["waits"]) == ("codex", "copilot", False)
        assert router["line"] == (
            "On Copilot for now: Codex is not signed in: sign in from Settings → Agents & models."
        )
        assert looked["workers"]["now"] == "copilot" and looked["workers"]["why"]
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


async def test_the_router_on_codex_takes_its_own_new_model_and_effort_unless_chosen_in_chat(
    tmp_path, codex_log
):
    """agents-anywhere.md 2.2: a Router placed on Codex runs with its own model and effort
    there (Who does the work), not Codex's workers': a change of its own takes effect at rest,
    one of the workers' does not reach it. An effort chosen in its own chat stays."""
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        await saved(runtime, {"codex": {"enabled": True}, "roles": {"router": "codex"}})
        assert runtime.agent(router) == "codex" and runtime.states.handle(router)
        await saved(runtime, {"models": {"router": {"codex": {"effort": "high"}}}})
        assert runtime.states.handle(router) is None, "opened again with the new effort"
        options = runtime.role_options(harness.store.session(router))
        assert options["reasoning_effort"] == "high"
        await runtime.session_for(router)
        assert runtime.states.handle(router)
        await saved(runtime, {"codex": {"model": "gpt-mini", "effort": "low"}})
        assert runtime.states.handle(router), "Codex's workers' model is theirs: not opened again"
        assert "model" not in runtime.role_options(harness.store.session(router))
        await saved(runtime, {"models": {"router": {"codex": {"model": "gpt-mini"}}}})
        assert runtime.states.handle(router) is None, "opened again with the new model"
        assert runtime.role_options(harness.store.session(router))["model"] == "gpt-mini"
        await runtime.session_for(router)
        harness.store.set(f"agentmodel:{router}", json.dumps({"model": "", "effort": "low"}))
        await saved(runtime, {"models": {"router": {"codex": {"effort": "medium"}}}})
        assert runtime.states.handle(router), "its own effort stays: not opened again"
        assert runtime.role_options(harness.store.session(router))["reasoning_effort"] == "low"
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
            "The Router runs on Copilot for now: Codex is not signed in: sign in from Settings → "
            "Agents & models."
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
        assert said.startswith(
            "The Router runs on Copilot for now: Codex's 5-hour limit is used up · resets "
        ), said
        held = runtime.role_held(ROUTER, "codex")
        assert held["agent"] == "codex" and held["until"] > time.time() + 3000
        assert not [k for k, _ in harness.kinds(router) if k == "error"], "no error: it went on"
    finally:
        await runtime.stop()


async def test_a_plan_limit_with_ask_me_says_why_without_a_doubled_full_stop(tmp_path, codex_log):
    """Ask me, and Codex's plan limit: the Router's card says why in one sentence (the reason
    ends with its own full stop: "resets 10:02 PM.. What it is sent" read twice)."""
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        await saved(
            runtime,
            {"codex": {"enabled": True}, "roles": {"router": "codex"},
             "role_unavailable": {"router": "ask"}},
        )  # fmt: skip
        await runtime.owner_send(router, "Hit the usage limit, then plan the release")
        await until(lambda: bool(runtime.role_wait(ROUTER)), 30)
        (card,) = [m for m in harness.store.messages(router) if m["meta"].get("role_wait")]
        assert "Codex can't run it: Codex's 5-hour limit is used up" in card["text"], card["text"]
        assert ".." not in card["text"], card["text"]
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
        # Nor does its process refresh the owner's plugin marketplaces, as a worker's does not.
        assert start["no_updates"] and start["hooks_off"]
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


@pytest.mark.parametrize("agent", ["codex", "claude"])
async def test_the_router_on_another_agent_is_given_its_whole_role(
    tmp_path, monkeypatch, request, agent
):
    """The Router on Codex or Claude is given its role as on Copilot, word for word, 0.1.51's
    rules too: a new worker when the owner asks for one in so many words (spawn_worker's why_new
    says so too), and a new task's result is report(status="done"), never an answer."""
    starts = tmp_path / "claude-starts.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_STARTS", str(starts))
    read = request.getfixturevalue("codex_log" if agent == "codex" else "claude_on_path")
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        await saved(runtime, {agent: {"enabled": True}, "roles": {"router": agent}})
        assert runtime.agent(router) == agent
        role = runtime.session_prompt(harness.store.session(router), None)
        if agent == "codex":
            (start,) = [
                p for p in read("thread/start") if p["cwd"] == str(runtime.role_dir(ROUTER))
            ]
            given = start["developerInstructions"]
            (tool,) = [t for t in start["dynamicTools"] if t["name"] == "spawn_worker"]
            why_new = tool["inputSchema"]["properties"]["why_new"]["description"]
            assert "At once when the owner asked for a new worker in so many words" in why_new
        else:
            (start,) = [json.loads(line) for line in starts.read_text().splitlines()]
            given = start["appended"]
        assert given == role
        for rule in (
            "When the owner asks for a new worker in so many words",
            'A new task\'s instructions (spawn_worker,\n  or message_session task="new") never '
            'ask for status="answer"',
        ):
            assert rule in given, rule
    finally:
        await runtime.stop()


# A check of Chief of Staff's that could not run (a machine busy with other work): never final

LATE = (
    "Chief of Staff's check of Claude Code 2.1.322 could not finish (step 'background' took too "
    "long)"
)


def late_once(monkeypatch) -> list[int]:
    """Claude's check, which could not finish its first run (a step ran over its time, twice),
    then keeps the contract; how many times it ran."""
    ran = [0]

    async def check(self, folder):
        ran[0] += 1
        if ran[0] == 1:
            return {
                "ran": False,
                "unrun": LATE,
                "ask_first": f"{LATE}: workers that ask first do not run on it until it does.",
                "acting": "",
            }
        return dict(PASSED)

    monkeypatch.setattr(backend_claude.ClaudeAgent, "check", check)
    return ran


async def test_a_check_that_could_not_run_is_tried_again_and_the_router_moves_by_itself(
    tmp_path, claude_on_path, monkeypatch
):
    """The owner put the Router on Claude while their machine was busy: Chief of Staff's check
    of it could not finish. The Router goes on on Copilot, Settings saying why as a role's line
    says it, and the check is tried again by itself (never final): once it runs, the Router
    moves to Claude, without the owner doing anything."""
    monkeypatch.setattr(runtime_module, "CHECK_AGAIN", (0.5,) * 5, raising=False)
    ran = late_once(monkeypatch)
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        await saved(runtime, {"claude": {"enabled": True}, "roles": {"router": "claude"}})
        assert runtime.agent(router) == "copilot"
        assert (
            runtime.role_line(ROUTER) == f"On Copilot for now: {LATE}. It's tried again by itself."
        )
        assert notices(harness, router) == [
            f"The Router runs on Copilot for now: {LATE}. It's tried again by itself."
        ]
        moved = "The Router runs on Claude now, with a summary of what it knew."
        await until(lambda: notices(harness, router)[-1] == moved, 15)
        assert runtime.agent(router) == "claude"
        assert ran[0] == 2, "tried again once, by itself"
        assert runtime.role_line(ROUTER) == ""
        assert harness.store.get(f"rolefallback:{ROUTER}") is None
    finally:
        await runtime.stop()


@pytest.mark.parametrize("kept", ["0.1.53", "0.1.54"])
async def test_after_a_restart_a_check_that_could_not_run_is_tried_again(
    tmp_path, claude_on_path, monkeypatch, kept
):
    """What 0.1.53 kept of a check that could not run (its words only), or 0.1.54 (that it did
    not run, and why: its interrupt step, which a data folder with a space in its path always
    failed), the Router on Copilot since: started again (updated), Chief of Staff tries the
    check again and puts the Router on Claude."""
    late_once(monkeypatch)
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        await saved(runtime, {"claude": {"enabled": True}, "roles": {"router": "claude"}})
        assert runtime.agent(router) == "copilot"
    finally:
        await runtime.stop()
    refused = json.loads(harness.store.get("agentrefused:claude"))
    if kept == "0.1.53":
        said = (
            "Chief of Staff's check of Claude Code 2.1.322 could not run (a step of the check "
            "did not happen in time): workers that ask first do not run on it until it does."
        )
        refused = {k: v for k, v in refused.items() if k not in {"ran", "unrun"}}
        refused["ask_first"] = said
    else:
        unrun = (
            "Chief of Staff's check of Claude Code 2.1.322 could not finish (step 'interrupt' "
            "took too long)"
        )
        said = f"{unrun}: workers that ask first do not run on it until it does."
        refused |= {"ran": False, "unrun": unrun, "ask_first": said}
    harness.store.set("agentrefused:claude", json.dumps(refused))
    again = Harness(harness.store.directory, client=harness.client)
    runtime = await again.connect()
    try:
        moved = "The Router runs on Claude now, with a summary of what it knew."
        await until(lambda: notices(harness, router)[-1] == moved, 15)
        assert harness.store.get(f"rolefallback:{ROUTER}") is None
    finally:
        await runtime.stop()


async def test_putting_the_router_on_claude_again_tries_its_check_at_once(
    tmp_path, claude_on_path, monkeypatch
):
    """The owner's choice in Settings is tried at once, its check that could not run too (not
    a minute after the last try)."""
    ran = late_once(monkeypatch)
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        await saved(runtime, {"claude": {"enabled": True}, "roles": {"router": "claude"}})
        assert runtime.agent(router) == "copilot" and ran[0] == 1
        await saved(runtime, {"roles": {"router": "copilot"}})
        await saved(runtime, {"roles": {"router": "claude"}})
        assert ran[0] == 2
        await until(lambda: runtime.agent(router) == "claude")
    finally:
        await runtime.stop()


async def test_a_worker_that_asks_first_tries_a_check_that_could_not_run_again(
    tmp_path, claude_on_path, monkeypatch
):
    """A worker of the owner's that asks first, on a Claude whose check could not run: its
    start tries the check again (a minute after the last at the earliest), and runs once it
    has."""
    monkeypatch.setattr(runtime_module, "CHECK_AGAIN", (0.2,) * 5, raising=False)
    ran = late_once(monkeypatch)
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        await runtime.connect_agent("claude")
        row = harness.store.create_session("w1", WORKER, "Fix", str(tmp_path))
        runtime.set_agent("w1", "claude")
        row = {**harness.store.session("w1"), "yolo": "off"}
        assert runtime.agent_block(row).startswith(f"{LATE}: workers that ask first")
        await asyncio.sleep(0.3)
        await runtime.agent_ready("claude", row)
        assert ran[0] == 2 and runtime.agent_block(row) == ""
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


async def test_watch_for_you_scans_on_claude_and_back_on_copilot(
    tmp_path, claude_on_path, monkeypatch
):
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
        held = runtime.role_held(ROUTER, "codex")
        assert held["kind"] == "start" and held["until"] > time.time() + 200
        await runtime.roles_check()
        assert runtime.agent(router) == "copilot", "a glance: still signed out"
        monkeypatch.delenv("FAKE_CODEX_SIGNED_OUT")
        await runtime.roles_check()
        assert runtime.agent(router) == "codex", "signed in: back at once"
        assert runtime.role_held(ROUTER, "codex") == {}
    finally:
        await runtime.stop()


async def test_signed_in_from_the_page_the_router_goes_back_at_once(
    tmp_path, codex_log, monkeypatch
):
    """Signed in with Sign in (agentsetup.py): the Router moves back to Codex as soon as the
    sign-in is seen, without waiting for its next check."""
    from pathlib import Path

    from chiefofstaff import agentsetup

    monkeypatch.setattr(agentsetup, "POLL_SECONDS", 0.1)
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        await saved(runtime, {"codex": {"enabled": True}, "roles": {"router": "codex"}})
        assert runtime.agent(router) == "copilot"
        await runtime.setups.sign_in("codex")
        await until(lambda: runtime.setups.view()["codex"].get("url"))
        (Path(runtime_module.os.environ["FAKE_CODEX_HOME"]) / "approve").write_text("ok")
        await until(lambda: runtime.agent(router) == "codex")
        assert runtime.role_held(ROUTER, "codex") == {}
    finally:
        await runtime.stop()


@pytest.mark.parametrize("then", ["copilot", "signed in"])
async def test_ask_me_keeps_the_routers_messages_until_the_owner_says(
    tmp_path, codex_log, monkeypatch, then
):
    """If unavailable: Ask me. Codex can't run the Router: it waits, what it is sent kept, its
    card saying why with Sign in; Use Copilot until then (or signing in) and it goes on with
    what was kept, once."""
    monkeypatch.setattr(runtime_module, "UNREADY_SECONDS", 0.0)
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        await saved(
            runtime,
            {
                "codex": {"enabled": True},
                "roles": {"router": "codex"},
                "role_unavailable": {"router": "ask"},
            },
        )
        assert agents.role_unavailable(harness.store, ROUTER) == "ask"
        assert runtime.snapshot()["role_waits"] == [ROUTER]
        (card,) = [m for m in harness.store.messages(router) if m["meta"].get("role_wait")]
        assert card["text"] == (
            "The Router waits for Codex. Codex needs you to sign in. What it is sent is kept "
            "until then."
        )
        assert card["meta"]["status"] == agents.SIGN_IN
        assert not any("runs on Copilot for now" in text for text in notices(harness, router))
        sent_before = list(harness.session(router).sent)
        await runtime.owner_send(router, "Hello")
        await settle(runtime)
        assert harness.session(router).sent == sent_before, "kept, not sent"
        if then == "copilot":
            await runtime.answer_role_wait(ROUTER)
            await until(lambda: any("Hello" in text for text in harness.session(router).sent))
            assert runtime.agent(router) == "copilot"
            said = "On Copilot until then"
        else:
            monkeypatch.delenv("FAKE_CODEX_SIGNED_OUT")
            await runtime.refresh_agent_status()
            await until(lambda: runtime.agent(router) == "codex")
            inputs = lambda: [  # noqa: E731
                "\n".join(str(item.get("text") or "") for item in params.get("input") or [])
                for params in codex_log("turn/start")
            ]
            await until(lambda: any("Hello" in text for text in inputs()))
            said = "Runs on Codex now"
        assert harness.store.message(card["id"])["meta"]["resumed"] == said
        assert "role_waits" not in runtime.snapshot()
        with pytest.raises(ValueError, match="answered already"):
            await runtime.answer_role_wait(ROUTER)
    finally:
        await runtime.stop()


def test_if_unavailable_is_another_agent_unless_ask_me_is_chosen(tmp_path):
    from chiefofstaff.store import Store

    store = Store(tmp_path)
    agents.configure(store, "codex", enabled=True)
    assert agents.role_unavailable(store, ROUTER) == "next"
    plan = agents.planned(store, {"role_unavailable": {"watchdog": "ask"}})
    assert plan["role_unavailable"] == {"watchdog": "ask"}
    agents.save(store, plan)
    plan = agents.planned(store, {"role_unavailable": {"watchdog": "next"}})
    assert "role_unavailable" not in plan
    plan = agents.planned(store, {"role_unavailable": {"watchdog": "copilot"}})
    assert "role_unavailable" not in plan, "0.1.57's word for the same"
    with pytest.raises(ValueError, match="If its agent can't run it"):
        agents.planned(store, {"role_unavailable": {"watchdog": "later"}})
