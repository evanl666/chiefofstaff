import json
from datetime import datetime

import pytest
from conftest import Harness, call_tool, settle

from chiefofstaff.connector import code_of, in_hours
from chiefofstaff.models import Settings, Watch
from chiefofstaff.runtime import CONNECTOR_TOOLS


async def spawn(runtime, router, title):
    result = await call_tool(
        runtime, "spawn_worker", router, title=title, instructions=title, why_new="test"
    )
    return json.loads(result.text_result_for_llm)["session_id"]


def test_watches_run_often_in_work_hours_and_less_after():
    assert in_hours("09:00-18:00", datetime(2026, 11, 4, 10, 0))  # A Wednesday.
    assert not in_hours("09:00-18:00", datetime(2026, 11, 4, 18, 30))
    assert not in_hours("09:00-18:00", datetime(2026, 11, 7, 10, 0)), "not on a Saturday"
    assert in_hours("22:00-06:00", datetime(2026, 11, 4, 23, 0)), "across midnight"


async def test_a_scan_hands_what_needs_the_owner_to_the_router_once(tmp_path):
    watch = Watch(what="Slack DMs and mentions of me", how="Glean search")
    settings = Settings(watches=[watch], watch_after_hours_minutes=0)
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    connector = runtime.connector
    assert connector.interval(datetime(2026, 11, 4, 10, 0)) == 5
    assert connector.interval(datetime(2026, 11, 4, 20, 0)) == 0, "none after hours"

    await connector.scan()
    await settle(runtime)
    row = connector.session()
    session = harness.session(row["id"])
    assert [tool.name for tool in session.options["tools"]] == CONNECTOR_TOOLS
    assert row["slack"], "it reads untrusted content: sandboxed like a Slack reader"
    assert "Slack DMs and mentions of me (use: Glean search)" in session.sent[-1]
    with pytest.raises(ValueError, match="running already"):
        await connector.scan()

    item = {
        "key": "https://acme.slack.com/p1",
        "source": "slack",
        "sender": "Bob",
        "text": "can you review my PR today?",
        "link": "https://acme.slack.com/p1",
    }
    found = await call_tool(
        runtime, "report_found", row["id"], items=[item], methods="glean search from:@bob"
    )
    assert found.text_result_for_llm.startswith("1 new item")
    await settle(runtime)
    router = harness.session(harness.router_id).sent[-1]
    assert 'from="watch" type="event"' in router and "can you review my PR today?" in router
    again = await call_tool(runtime, "report_found", row["id"], items=[item])
    assert again.text_result_for_llm.startswith("0 new item"), "seen before"
    session.emit("session.idle")
    await settle(runtime)

    harness.store.set("watch_scanned_at", "0")
    await connector.scan()
    await settle(runtime)
    assert "glean search from:@bob" in session.sent[-1], "how it worked before"

    listed = json.loads(
        (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
    )
    assert row["id"] not in {entry["session_id"] for entry in listed["sessions"]}
    assert not any(task["worker"] == row["id"][:8] for task in listed["tasks"])
    refused = await call_tool(
        runtime, "message_session", harness.router_id, session_id=row["id"], text="fix a bug"
    )
    assert "helper" in refused.text_result_for_llm
    worker = await spawn(runtime, harness.router_id, "Other work")
    stranger = await call_tool(runtime, "report_found", worker, items=[item])
    assert stranger.result_type == "failure", "only the connector hands in findings"


async def test_away_the_owner_is_told_where_they_are_and_answers_from_there(tmp_path):
    settings = Settings(away_notify=True, away_minutes=5, away_how="DM me via the acme bot")
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    connector = runtime.connector

    async def idle():
        return 3600

    connector.idle_seconds = idle
    writer = await spawn(runtime, harness.router_id, "Answer Carol")
    await call_tool(
        runtime, "draft_reply", writer, text="yes, friday works 🙏", to="Carol", where="email"
    )
    asker = await spawn(runtime, harness.router_id, "Pick a port")
    await call_tool(runtime, "report", asker, status="question", summary="8080 or 9090?")
    harness.session(asker).emit("session.idle")
    await settle(runtime)
    [draft] = [d for d in harness.store.drafts() if d["kind"] == "reply"]
    task = runtime.tasks.current(asker)
    draft_code, task_code = code_of(draft["id"]), code_of(task["id"])

    await connector.tick()
    await settle(runtime)
    row = connector.session()
    sent = harness.session(row["id"]).sent[-1]
    assert "DM me via the acme bot" in sent
    assert f"[{draft_code}]" in sent and "yes, friday works" in sent
    assert f"[{task_code}]" in sent and "8080 or 9090?" in sent
    await call_tool(
        runtime, "report_notified", row["id"], ok=True, where="Slack DM from acmebot",
        method="acmebot.send_dm(to=me)",
    )  # fmt: skip
    harness.session(row["id"]).emit("session.idle")
    await settle(runtime)
    assert connector.due(), "it looks for the owner's answers even without watches"
    await connector.tick()
    await settle(runtime)
    sent = harness.session(row["id"]).sent
    assert len(sent) == 2 and sent[-1].startswith('<message from="system" type="scan">')
    assert draft_code in sent[-1] and "Send the owner" not in sent[-1], "told once"
    applied = await call_tool(
        runtime, "report_owner_replies", row["id"],
        replies=[
            {"code": draft_code, "text": f"{draft_code} send"},
            {"code": task_code, "text": f"{task_code} 9090 please"},
            {"code": "ZZZZZ", "text": "ZZZZZ send"},
        ],
    )  # fmt: skip
    text = applied.text_result_for_llm
    assert f"{draft_code}: marked as sent" in text and f"{task_code}: sent to the worker" in text
    assert "ZZZZZ: nothing waits" in text
    assert harness.store.draft(draft["id"])["status"] == "sent"
    assert runtime.tasks.get(task["id"])["status"] == "running"
    assert "9090 please" in harness.session(asker).sent[-1]
    notices = [
        m["text"] for m in harness.store.messages(harness.router_id) if m["kind"] == "notice"
    ]
    assert any("You answered from Slack DM from acmebot" in note for note in notices)
    assert json.loads(harness.store.get("away_last"))["ok"]


async def test_the_owners_words_reach_learning_through_any_tool(harness):
    runtime = await harness.connect()
    runtime.learning.record("follow-up", context="t", owner="Shorter please")
    await runtime.habits.learn_now()
    await settle(runtime)
    row = runtime.connector.session()
    assert row and "Find messages the owner themself wrote" in harness.session(row["id"]).sent[-1]
    await call_tool(
        runtime, "report_owner_words", row["id"],
        items=[{"key": "s1", "kind": "slack_dm", "text": "lgtm, ship it 🙏"}],
    )  # fmt: skip
    assert runtime.habits.view()["observed"]["slack_dm"] == 1
    assert not await runtime.connector.collect_words(), "once a day"


SKILL = """---
name: deploy-notes
description: Write deploy notes the way the team likes them
---
Start with the ticket, then the risk.
"""


async def test_the_owners_skills_mcp_servers_and_plugins_reach_workers(tmp_path):
    glean = {"type": "http", "url": "https://glean.example/mcp"}
    harness = Harness(
        tmp_path / "data", Settings(mcp_servers={"glean": glean}, plugins=["acme/tools"])
    )
    runtime = await harness.connect()
    abilities = runtime.abilities
    with pytest.raises(ValueError, match="front matter"):
        await abilities.add_skill("just some text without a name")
    added = await abilities.add_skill(SKILL)
    assert added["name"] == "deploy-notes"
    assert abilities.view()["skills"] == [
        {"name": "deploy-notes", "description": "Write deploy notes the way the team likes them"}
    ]
    worker = await spawn(runtime, harness.router_id, "Ship it")
    options = harness.session(worker).options
    assert str(abilities.skills_dir) in options["skill_directories"]
    assert (abilities.skills_dir / "deploy-notes" / "SKILL.md").read_text() == SKILL
    assert options["mcp_servers"]["glean"]["url"] == glean["url"]
    assert "glean" not in harness.session(harness.router_id).options.get("mcp_servers", {})

    installs = []

    async def install(source):
        installs.append(source)
        if source == "acme/broken":
            raise RuntimeError("repository not found")
        return "Installed."

    abilities.install = install
    await abilities.sync()
    old = harness.store.settings()
    new = old.model_copy(update={"plugins": ["acme/tools", "acme/broken"]})
    harness.store.save_settings(new)
    await abilities.sync()
    assert installs == ["acme/tools", "acme/broken"], "each installed once"
    status = abilities.plugin_status()
    assert status["acme/tools"]["state"] == "installed"
    assert status["acme/broken"] == {"state": "failed", "error": "repository not found"}
    await abilities.remove_skill("deploy-notes")
    assert abilities.view()["skills"] == []


async def test_a_question_the_router_asked_in_the_chat_reaches_the_owner_away(tmp_path):
    harness = Harness(tmp_path / "data", Settings(away_notify=True))
    runtime = await harness.connect()
    connector = runtime.connector
    await runtime.owner_send(harness.router_id, "Write the port into config.txt")
    router = harness.session(harness.router_id)
    router.emit("assistant.message", content="Should it be 8080 or 9090?", tool_requests=None)
    router.emit("session.idle")
    await settle(runtime)
    [item] = connector.waiting_items()
    assert "Should it be 8080 or 9090?" in connector.describe(item)
    assert await connector.apply(item, f"{item['code']} 9090") == "sent to the Router"
    assert router.sent[-1].count("9090") == 1 and '<message from="owner"' in router.sent[-1]
    assert connector.waiting_items() == [], "answered"


async def test_a_task_waiting_on_a_decision_is_told_once(harness):
    runtime = await harness.connect()
    asker = await spawn(runtime, harness.router_id, "Clean the data")
    await call_tool(
        runtime, "ask_decision", asker, question="Drop or correct the two bad rows?",
        why="They look wrong.", options=[{"choice": "Drop"}, {"choice": "Correct"}],
    )  # fmt: skip
    await settle(runtime)
    items = runtime.connector.waiting_items()
    assert [item["draft"]["kind"] for item in items] == ["decision"]
    assert "1) Drop; 2) Correct" in runtime.connector.describe(items[0])
