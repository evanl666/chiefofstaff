import json
import re
from datetime import datetime
from pathlib import Path

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


async def test_answers_to_away_notifications_are_read_at_night_and_on_weekends(tmp_path):
    watch = Watch(what="Slack DMs and mentions of me", how="Glean search")
    settings = Settings(
        watches=[watch], watch_after_hours_minutes=0, away_notify=True, away_how="DM me"
    )
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    connector = runtime.connector
    saturday, evening = datetime(2026, 11, 7, 10, 0), datetime(2026, 11, 4, 20, 0)
    assert connector.interval(saturday) == 0, "no watches on a Saturday"
    asker = await spawn(runtime, harness.router_id, "Pick a port")
    await call_tool(runtime, "report", asker, status="question", summary="8080 or 9090?")
    await connector.notify_away()
    await settle(runtime)
    assert connector.open_codes(), "told the owner, who may answer from there"
    assert connector.interval(saturday) == 5 and connector.interval(evening) == 5


def read(path: str) -> str:
    return Path(path).read_text()


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
    # The messenger sends it: outside the connector's sandbox (which reaches GitHub only, as it
    # reads others' messages), given Chief of Staff's own text in a file.
    messenger = runtime.toolset.last_special("messenger")
    assert messenger and not messenger["slack"] and connector.session() is None
    sent = harness.session(messenger["id"]).sent[-1]
    assert "DM me via the acme bot" in sent and "yes, friday works" not in sent
    body = read(re.search(r"(/\S+notification-\d+\.txt)", sent).group(1))
    assert f"[{draft_code}]" in body and "yes, friday works" in body
    assert f"[{task_code}]" in body and "8080 or 9090?" in body
    await call_tool(
        runtime, "report_notified", messenger["id"], ok=True, where="Slack DM from acmebot",
        method="acmebot send --file <file>",
    )  # fmt: skip
    harness.session(messenger["id"]).emit("session.idle")
    await settle(runtime)
    assert connector.due(), "it looks for the owner's answers even without watches"
    await connector.tick()
    await settle(runtime)
    assert len(harness.session(messenger["id"]).sent) == 1, "told once"
    row = connector.session()
    sent = harness.session(row["id"]).sent
    assert len(sent) == 1 and "Scan now" in sent[-1]
    assert draft_code in sent[-1] and "Send the owner" not in sent[-1]
    empty = await call_tool(runtime, "report_owner_replies", row["id"], answers=["send"])
    assert empty.result_type == "failure" and "No reply was given" in empty.text_result_for_llm
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


async def test_away_the_owner_merges_and_answers_a_cancelled_tasks_question(tmp_path):
    harness = Harness(tmp_path / "data", Settings(away_notify=True, away_how="DM me"))
    runtime = await harness.connect()
    connector = runtime.connector

    async def idle():
        return 3600

    connector.idle_seconds = idle
    pull = {
        "number": 7, "url": "https://github.com/acme/app/pull/7", "state": "OPEN",
        "title": "Fix login", "isDraft": False, "mergeStateStatus": "CLEAN",
        "headRefOid": "abc1234", "author": {"login": "me"}, "reviewDecision": "APPROVED",
        "statusCheckRollup": [{"name": "tests", "conclusion": "SUCCESS"}],
        "createdAt": "2999-01-01T00:00:00Z",
    }  # fmt: skip
    harness.gh_replies |= {
        "auth status --json hosts": json.dumps(
            {"hosts": {"github.com": [{"login": "me", "state": "success", "active": True}]}}
        ),
        "pr view https://github.com/acme/app/pull/7": json.dumps(pull),
        "api repos/acme/app": json.dumps({"allow_squash_merge": True}),
        "pr merge": "",
    }
    fixer = await spawn(runtime, harness.router_id, "Fix login")
    fix = runtime.tasks.current(fixer)
    await call_tool(
        runtime, "report", fixer, status="done", summary="https://github.com/acme/app/pull/7"
    )
    asker = await spawn(runtime, harness.router_id, "Pick a port")
    asked = runtime.tasks.current(asker)
    await call_tool(runtime, "report", asker, status="question", summary="8080 or 9090?")
    await runtime.task_actions.cancel(asked["id"])
    await settle(runtime)
    [card] = [d for d in harness.store.drafts() if d["kind"] == "merge"]
    [question] = runtime.snapshot()["questions"]
    merge_code = code_of(f"merge:acme/app#7@{card['target']['head']}")
    question_code = code_of(f"question:{asker}:{question['at']}")

    await connector.tick()
    await settle(runtime)
    messenger = runtime.toolset.last_special("messenger")
    body = read(
        re.search(r"(/\S+notification-\d+\.txt)", harness.session(messenger["id"]).sent[-1]).group(
            1
        )
    )
    assert f"[{merge_code}] Ready to merge: acme/app#7" in body
    assert f"[{question_code}]" in body and "8080 or 9090?" in body
    await call_tool(runtime, "report_notified", messenger["id"], ok=True, where="DM")
    harness.session(messenger["id"]).emit("session.idle")
    await settle(runtime)
    await connector.tick()
    await settle(runtime)
    harness.gh_replies["pr view https://github.com/acme/app/pull/7"] = json.dumps(
        pull | {"state": "MERGED"}
    )
    applied = await call_tool(
        runtime, "report_owner_replies", connector.session()["id"],
        replies=[
            {"code": merge_code, "text": f"{merge_code} merge"},
            {"code": question_code, "text": f"{question_code} 9090"},
        ],
    )  # fmt: skip
    text = applied.text_result_for_llm
    assert f"{merge_code}: acme/app#7 squashed and merged" in text, text
    assert f"{question_code}: sent to the worker" in text, text
    assert [args for args, _ in harness.gh_calls if args[:2] == ("pr", "merge")]
    assert runtime.tasks.get(fix["id"])["status"] == "done", "merged: its task is done"
    assert runtime.tasks.get(asked["id"])["status"] == "running", "answered: it goes on"
    assert "9090" in harness.session(asker).sent[-1]
    assert runtime.snapshot()["questions"] == []


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


def test_a_reply_passed_on_its_own_is_not_dropped():
    from chiefofstaff.connector import ReportOwnerReplies

    flat = ReportOwnerReplies.model_validate({"code": "7AUBM", "text": "7AUBM drop them"})
    assert [(r.code, r.text) for r in flat.replies] == [("7AUBM", "7AUBM drop them")]
    assert ReportOwnerReplies.model_validate({}).replies == []


async def test_a_workers_question_the_router_retells_is_told_once(harness):
    runtime = await harness.connect()
    router = harness.router_id
    worker = await spawn(runtime, router, "Clean data/sales.csv")
    question = ("Two rows look wrong: line 42 has negative units and line 302 has price 1200. "
                "Drop or correct them?")  # fmt: skip
    await call_tool(runtime, "report", worker, status="question", summary=question)
    await settle(runtime)
    runtime.add_message(router, "assistant", "Clean data/sales.csv found 2 odd rows: line 42 "
                        "negative units, line 302 price 1200. Drop or correct?")  # fmt: skip
    items = runtime.connector.waiting_items()
    assert [item["id"].split(":")[0] for item in items] == ["task"], "once, as the task's"
    runtime.add_message(router, "assistant", "Separately: should I also archive last year's "
                        "invoices in the billing folder?")  # fmt: skip
    kinds = sorted(item["id"].split(":")[0] for item in runtime.connector.waiting_items())
    assert kinds == ["router", "task"], "a question of its own is told"


async def test_away_failures_are_said_once_until_it_works(tmp_path):
    harness = Harness(tmp_path / "data", Settings(away_notify=True, away_how="notabot"))
    runtime = await harness.connect()
    await runtime.connector.notify_away(test=True)
    await settle(runtime)
    messenger = runtime.toolset.last_special("messenger")
    for _ in range(2):
        await call_tool(
            runtime, "report_notified", messenger["id"], ok=False, error="authn-cli failed"
        )
    notices = [
        m for m in harness.store.messages(harness.router_id) if "Could not reach you" in m["text"]
    ]
    assert len(notices) == 1
    await call_tool(runtime, "report_notified", messenger["id"], ok=True, where="DM")
    await call_tool(runtime, "report_notified", messenger["id"], ok=False, error="down again")
    notices = [
        m for m in harness.store.messages(harness.router_id) if "Could not reach you" in m["text"]
    ]
    assert len(notices) == 2, "said again once it had worked in between"


async def test_a_found_message_the_owner_answered_is_left_out(tmp_path):
    settings = Settings(watches=[Watch(what="Questions for me")])
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    await runtime.connector.scan()
    await settle(runtime)
    row = runtime.connector.session()
    link = "https://acme.slack.com/archives/C0123ABC/p1700000000000100"
    found = await call_tool(
        runtime, "report_found", row["id"],
        items=[
            {"key": "a", "source": "slack", "sender": "Megha", "text": "ACL ok?", "link": link,
             "when": "2026-11-07T01:00:00Z", "owner_replied_at": "2026-11-07T02:31:00Z"},
            {"key": "b", "source": "slack", "sender": "Bob", "text": "review?", "link": link,
             "context": "Bob says the owner never answered"},
        ],
    )  # fmt: skip
    assert found.text_result_for_llm.startswith("1 new item") and "already answered" in (
        found.text_result_for_llm
    )
    await settle(runtime)
    router = harness.session(harness.router_id).sent[-1]
    assert "review?" in router and "ACL ok?" not in router
    assert "Thread: slack:C0123ABC:1700000000.000100 (Slack is not connected here" in router
    assert "Context (as the watcher read it)" in router
