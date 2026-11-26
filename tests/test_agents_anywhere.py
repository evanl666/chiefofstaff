"""Every one of Chief of Staff's own sessions on any agent (docs/design/agents-anywhere.md): the
Slack session and the helpers on Codex and Claude with nothing but their tools, Slack's token in
no agent's hands, the next agent that can when one can't (each once, then one card), a helper's
job kept as it moves, the messenger's own way to the owner, the migration of earlier settings,
and every worker's agent said. Codex and Claude are the stand-in CLIs (tests/fake_codex.py,
fake_claude.py) on the PATH, under the real supervisor."""

import json
import sys

import pytest
from conftest import Harness, call_tool, settle
from test_roles import PASSED, saved, started, until

from chiefofstaff import agents
from chiefofstaff.models import Settings, Watch
from chiefofstaff.roles import ROUTER, SLACK

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Other agents need macOS/Linux")

TOKEN = "xoxp-test"  # The fake Slack's token (conftest.FakeTokens).


@pytest.fixture
def codex_log(tmp_path, monkeypatch, codex_installed):
    """What Chief of Staff asked the stand-in Codex, one request a line (as test_roles')."""
    log = tmp_path / "codex-log.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))

    def read(method: str) -> list[dict]:
        if not log.exists():
            return []
        lines = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        return [entry["params"] for entry in lines if entry.get("method") == method]

    return read


def thread_starts(codex_log) -> list[dict]:
    return codex_log("thread/start")


def tool_names(params: dict) -> list[str]:
    return sorted(tool["name"] for tool in params.get("dynamicTools") or [])


# The Slack session on Codex and Claude, Slack's token in no agent's hands


async def test_the_slack_session_runs_on_codex_with_its_tools_and_slacks_reads_never_its_token(
    tmp_path, codex_log, monkeypatch
):
    envs = tmp_path / "codex-envs.jsonl"
    monkeypatch.setenv("FAKE_CODEX_ENVS", str(envs))
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    try:
        await runtime.slack.discover()
        (slack,) = harness.store.open_sessions(SLACK)
        await saved(runtime, {"codex": {"enabled": True}, "roles": {"slack": "codex"}})
        await until(lambda: runtime.agent(slack["id"]) == "codex")
        await runtime.session_for(slack["id"])
        (params,) = [p for p in thread_starts(codex_log) if "draft_slack_reply" in tool_names(p)]
        names = tool_names(params)
        assert "slack_read_thread" in names and "draft_slack_reply" in names
        assert not [n for n in names if "send" in n and n.startswith("slack_")]
        assert "ask_user" not in names, "nothing but its own tools"
        config = params.get("config") or {}
        assert config.get("features.shell_tool") is False, "Codex's commands off"
        # Slack's token: in no request, argument or variable of Codex's.
        assert TOKEN not in json.dumps(codex_log("thread/start") + codex_log("turn/start"))
        assert TOKEN not in (tmp_path / "codex-runs").read_text()
        assert envs.exists() and TOKEN not in envs.read_text()
        # Its read tools are Chief of Staff's: a call reads Slack with the token it holds.
        tools = {tool.name: tool for tool in runtime.session_tools(slack)}
        harness.mcp.reads["C0DEPLOYS"] = "From: <@U0ALICE01> | Text: is the deploy done?"
        result = await tools["slack_read_thread"].handler(
            type("Call", (), {"arguments": {"channel_id": "C0DEPLOYS"}})()
        )
        assert "is the deploy done?" in result.text_result_for_llm
    finally:
        await runtime.stop()


async def test_the_slack_session_runs_on_claude_with_its_tools_and_never_slacks_token(
    tmp_path, claude_on_path, monkeypatch
):
    starts = tmp_path / "starts.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_STARTS", str(starts))
    monkeypatch.setattr("chiefofstaff.backend_claude.ClaudeAgent.check", _passed)
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    try:
        await runtime.slack.discover()
        (slack,) = harness.store.open_sessions(SLACK)
        await saved(runtime, {"claude": {"enabled": True}, "roles": {"slack": "claude"}})
        await until(lambda: runtime.agent(slack["id"]) == "claude")
        await runtime.session_for(slack["id"])
        started_once = [json.loads(line) for line in starts.read_text().splitlines()]
        assert started_once and all(entry["servers"] == ["chief"] for entry in started_once)
        assert TOKEN not in starts.read_text(), "no variable or argument of Claude's has it"
        private = harness.store.directory / "agents" / "claude" / "private"
        files = [path.read_text() for path in private.glob("*") if path.is_file()]
        assert not [text for text in files if TOKEN in text], "no file of Claude's has it"
    finally:
        await runtime.stop()


async def _passed(self, folder):
    return dict(PASSED)


# The next agent that can, each once, then one card; across a restart; and back


@pytest.fixture
def claude_checked(monkeypatch):
    monkeypatch.setattr("chiefofstaff.backend_claude.ClaudeAgent.check", _passed)
    monkeypatch.setattr("chiefofstaff.backend_claude.LISTED", {})


def wait_cards(harness, router: str) -> list[dict]:
    return [m for m in harness.store.messages(router) if (m["meta"] or {}).get("role_wait")]


async def test_a_role_goes_through_the_agents_once_then_waits_with_one_card_and_comes_back(
    tmp_path, codex_log, claude_on_path, claude_checked, monkeypatch
):
    """agents-anywhere.md 12.5: the Router chosen for Claude, Claude signed out: it runs on
    Copilot (the next that can); Copilot loses its sign-in in its turn: on Codex (the next);
    Codex loses its own in its turn: each agent was tried once, none is left, so it waits, with
    one card. A restart keeps each agent's wait and the Router's; Claude signed in again, it
    goes back there by itself and the card says so."""
    monkeypatch.setattr("chiefofstaff.runtime.ROLE_GLANCE", 0.0)
    monkeypatch.setenv("FAKE_CLAUDE_SIGNED_OUT", "1")
    harness, runtime = await started(tmp_path)
    router = harness.router_id
    try:
        await saved(runtime, {"claude": {"enabled": True}, "codex": {"enabled": True},
                              "roles": {"router": "claude"}})  # fmt: skip
        assert runtime.agent(router) == "copilot", "the next that can: Copilot, first"
        assert runtime.role_held(ROUTER, "claude")["kind"] == "start"
        # Copilot loses its sign-in in the middle of the Router's turn: on to Codex.
        await runtime.session_for(router)
        harness.session(router).emit("session.error", message="401 Unauthorized: not signed in")
        await settle(runtime)
        await until(lambda: runtime.agent(router) == "codex")
        assert runtime.role_held(ROUTER, "copilot")["why"].startswith("Copilot is signed out")
        notices = [text for kind, text in harness.kinds(router) if kind == "notice"]
        assert notices[-1].startswith("The Router runs on Codex for now: "), notices[-1]
        # Codex loses its own: none is left.
        await runtime.owner_send(router, "Lose the sign-in.")
        await until(lambda: bool(runtime.role_wait(ROUTER)), 30)
        wait = runtime.role_wait(ROUTER)
        assert wait["agent"] == "claude" and wait["instead"] == ""
        (card,) = wait_cards(harness, router)
        assert card["text"].startswith("The Router waits for Claude."), card["text"]
        assert "instead" not in card["meta"], "no agent can run it meanwhile"
        await runtime.roles_check()
        await settle(runtime)
        assert len(wait_cards(harness, router)) == 1, "one card, whatever is tried again"
    finally:
        await runtime.stop()
    # A restart in the middle: each agent's wait and the Router's are kept.
    again = Harness(tmp_path / "data")
    runtime = await again.connect()
    try:
        for agent in ("claude", "copilot", "codex"):
            assert runtime.role_held(ROUTER, agent), agent
        assert runtime.role_wait(ROUTER)
        await runtime.roles_check()
        assert runtime.role_wait(ROUTER), "still no agent that can run it"
        assert len(wait_cards(again, router)) == 1
        monkeypatch.delenv("FAKE_CLAUDE_SIGNED_OUT")
        await runtime.roles_check()
        await settle(runtime)
        await until(lambda: runtime.agent(router) == "claude")
        assert not runtime.role_wait(ROUTER)
        assert again.store.message(card["id"])["meta"]["resumed"] == "Runs on Claude now"
    finally:
        await runtime.stop()


async def test_a_helper_whose_turn_lost_its_agent_keeps_its_job_on_the_next(tmp_path, codex_log):
    """agents-anywhere.md 12.4: Watch for you on Codex; its scan's turn loses Codex's sign-in:
    Codex is held for it and the scan goes on on Copilot, the same job (its task still running,
    what its turn was given sent there), not failed."""
    watch = "Mentions of me. Lose the sign-in."
    harness = Harness(
        tmp_path / "data", Settings(watches=[Watch(what=watch)], watch_after_hours_minutes=0)
    )
    runtime = await harness.connect()
    try:
        await saved(runtime, {"codex": {"enabled": True}, "roles": {"connector": "codex"}})
        await runtime.connector.scan()
        row = runtime.connector.session()
        await until(lambda: runtime.agent(row["id"]) == "copilot", 30)
        await settle(runtime)
        assert runtime.role_held("connector", "codex")["why"].startswith("Codex is signed out")
        (task,) = runtime.tasks.for_session(row["id"])
        assert task["status"] == "running", "its job is kept"
        assert any("Mentions of me" in sent for sent in harness.session(row["id"]).sent)
        notices = [text for kind, text in harness.kinds(row["id"]) if kind == "notice"]
        assert notices[-1].startswith("Watch for you runs on Copilot for now: Codex is signed")
    finally:
        await runtime.stop()


# The helpers on Codex


async def test_the_learner_runs_on_codex_with_nothing_but_its_tools(tmp_path, codex_log):
    _, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}, "roles": {"helpers": "codex"},
                              "models": {"helpers": {"codex": {"model": "gpt-mini"}}}})  # fmt: skip
        made = await runtime.toolset.start_special(
            "learner", "Learn how you work", "Learn from these: reply with just: ok"
        )
        assert runtime.agent(made["session_id"]) == "codex"
        await until(lambda: any("update_style" in tool_names(p) for p in thread_starts(codex_log)))
        (params,) = [p for p in thread_starts(codex_log) if "update_style" in tool_names(p)]
        assert tool_names(params) == ["propose_lessons", "update_style"]
        assert (params.get("config") or {}).get("features.shell_tool") is False
        assert params.get("model") == "gpt-mini", "the helpers' own model on Codex"
    finally:
        await runtime.stop()


async def test_the_messenger_on_codex_asks_first_and_sends_and_reads_over_its_own_way(
    tmp_path, codex_log
):
    """agents-anywhere.md 12.8: the messenger on Codex asks first; its owner's own way (a bot's
    CLI here, not Slack) is let through Chief of Staff's check: it sends a notification and
    reads the owner's reply there."""
    from test_round6_drafts import away
    from test_round6_drafts import spawn as spawn_worker

    sent = tmp_path / "bot-sent.txt"
    bot = tmp_path / "owner-bot"
    bot.write_text(f"#!/bin/sh\necho \"$@\" >> '{sent}'\necho 'ABCDE yes please'\n")
    bot.chmod(0o755)
    report = {
        "ok": True,
        "where": "the owner's bot",
        "method": "owner-bot send",
        "replies": "owner-bot read",
    }
    how = (
        f"Run the shell command `'{bot}' send the-notification` then Call the report_notified "
        f"tool with arguments {json.dumps(report)}"
    )
    harness = Harness(tmp_path / "data", Settings(away_notify=True, away_how=how))
    runtime = await away(harness)
    try:
        await saved(runtime, {"codex": {"enabled": True}, "roles": {"helpers": "codex"}})
        worker, _task = await spawn_worker(runtime, harness, "Answer the team")
        await call_tool(
            runtime, "draft_reply", worker, text="Monday works.", to="Bob", where="mail"
        )
        await runtime.connector.notify_away()
        messenger = runtime.toolset.last_special("messenger")
        assert runtime.agent(messenger["id"]) == "codex"
        assert runtime.agent_options(harness.store.session(messenger["id"]))["ask_first"] is True
        await until(lambda: sent.exists() and "send the-notification" in sent.read_text(), 30)
        await until(lambda: json.loads(harness.store.get("away_last") or "{}").get("ok"), 30)
        # Its replies, read the same way.
        code = min(runtime.connector.reply_codes())
        replies = {"replies": [{"code": code, "text": f"{code} later", "intent": "unclear"}]}
        harness.store.set(
            "away_replies",
            f"Run the shell command `'{bot}' read` then Call the report_owner_replies tool with "
            f"arguments {json.dumps(replies)}",
        )
        await until(lambda: harness.store.session(messenger["id"])["status"] != "working", 30)
        harness.store.set("away_read_at", "0")
        await runtime.connector.read_replies()
        await until(lambda: "read" in sent.read_text().split(), 30)
        router = harness.router_id
        await until(
            lambda: any(f"[{code}]" in m["text"] for m in harness.store.messages(router)), 30
        )
    finally:
        await runtime.stop()


# The migration of earlier settings


def test_the_default_model_is_written_into_each_kind_that_took_it(tmp_path):
    harness = Harness(
        tmp_path / "data",
        Settings(
            model="fast-model",
            role_models={"router": "smart-model"},
            role_efforts={"worker": "high"},
        ),
    )
    store = harness.store
    store.set(
        agents.CONFIG,
        json.dumps(
            {
                "codex": {"enabled": True, "model": "gpt-big", "effort": "low"},
                "roles": {"watchdog": "codex"},
            }
        ),
    )
    assert agents.migrate(store) == []
    settings = store.settings()
    assert settings.model == "", "the Default model is gone"
    assert settings.role_models == {
        "router": "smart-model", "worker": "fast-model", "slack": "fast-model",
        "watchdog": "fast-model",
    }  # fmt: skip
    for kind in ("connector", "helpers"):
        assert agents.kind_model(store, kind, "copilot") == {"model": "fast-model",
                                                              "effort": "high"}  # fmt: skip
    assert agents.kind_model(store, "watchdog", "codex") == {"model": "gpt-big", "effort": "low"}
    assert agents.kind_model(store, "router", "codex") == {"model": "", "effort": ""}
    assert agents.migrate(store) == [] and store.settings() == settings, "once"


async def test_a_choice_held_back_before_runs_as_chosen_and_home_says_so_once(
    tmp_path, claude_on_path, monkeypatch
):
    monkeypatch.setattr("chiefofstaff.backend_claude.ClaudeAgent.check", _passed)
    harness = Harness(tmp_path / "data")
    harness.store.set(
        agents.CONFIG,
        json.dumps({"claude": {"enabled": True}, "roles": {"connector": "claude"}}),
    )
    runtime = await harness.connect()
    try:
        said = [text for kind, text in harness.kinds(harness.router_id) if kind == "notice"]
        assert said.count("Watch for you now runs on Claude, as you chose earlier.") == 1
        await runtime.reconnect()
        said = [text for kind, text in harness.kinds(harness.router_id) if kind == "notice"]
        assert said.count("Watch for you now runs on Claude, as you chose earlier.") == 1
        assert agents.role_agent(harness.store, "connector") == "claude"
    finally:
        await runtime.stop()


# Every worker says its agent


async def test_every_workers_start_line_names_its_agent_also_once_closed(tmp_path):
    from chiefofstaff.web import with_agents

    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    try:
        made = await call_tool(runtime, "spawn_worker", harness.router_id, title="Fix it",
                               instructions="Fix it.", why_new="test")  # fmt: skip
        worker = json.loads(made.text_result_for_llm)["session_id"]
        (line,) = [m for m in harness.store.messages(harness.router_id)
                   if (m["meta"] or {}).get("session_id") == worker]  # fmt: skip
        assert line["meta"]["agent"] == "copilot", "said where it is made"
        # A line of before 0.1.59 (no agent): its worker's, whether or not it is open.
        older = harness.store.add_message(
            harness.router_id, "notice", "Started “Old one”", {"session_id": "w-old"}
        )
        harness.store.set("agent:w-old", "codex")
        rows = with_agents(harness.store, harness.store.messages(harness.router_id))
        (found,) = [row for row in rows if row["id"] == older["id"]]
        assert found["meta"]["agent"] == "codex"
    finally:
        await runtime.stop()
