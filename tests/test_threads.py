import json
import time
from types import SimpleNamespace

import pytest
from conftest import Harness, call_tool, env, settle

from chiefofstaff import threads
from chiefofstaff.models import DraftDecision, Settings


def test_thread_keys():
    assert threads.normalize(" github:Acme/App#7 ") == "github:acme/app#7"
    assert threads.normalize("slack:C0DEPLOYS:1712345678.000100")
    assert threads.normalize("slack:D0ALICE01")
    with pytest.raises(ValueError):
        threads.normalize("slack:C0DEPLOYS")  # a whole channel is too broad to follow
    text = (
        "https://acme.slack.com/archives/C0DEPLOYS/p1712345690000200"
        "?thread_ts=1712345678.000100&cid=C0DEPLOYS\n"
        "https://acme.slack.com/archives/D0ALICE01/p1712345699000300"
    )
    assert threads.slack_messages(text) == {
        "1712345690.000200": ["slack:C0DEPLOYS:1712345678.000100"],
        "1712345699.000300": ["slack:D0ALICE01:1712345699.000300", "slack:D0ALICE01"],
    }
    assert threads.pull_requests("PR: https://github.com/Acme/App/pull/12.") == [
        "github:acme/app#12"
    ]
    sent = "Message sent: https://acme.slack.com/archives/C0DEPLOYS/p1712345700000100"
    assert threads.slack_follow_key("C0DEPLOYS", "", sent) == "slack:C0DEPLOYS:1712345700.000100"
    assert threads.slack_follow_key("C0DEPLOYS", "1.0", "") == "slack:C0DEPLOYS:1.0"
    assert threads.slack_follow_key("U0ALICE1", "", "") is None
    dm = "Message sent: https://acme.slack.com/archives/D0ALICE01/p1712345700000100"
    assert threads.slack_follow_key("U0ALICE1", "", dm) == "slack:D0ALICE01"


async def test_router_hands_threads_to_the_worker_it_starts(harness):
    runtime = await harness.connect()
    refused = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="X", instructions="x", follow=["nope"]
    )
    assert "Unknown thread key" in refused.text_result_for_llm
    assert harness.store.open_sessions("worker") == []

    result = await call_tool(
        runtime,
        "spawn_worker",
        harness.router_id,
        title="Review",
        instructions="review #7",
        follow=["github:Acme/App#7"],
    )
    worker_id = json.loads(result.text_result_for_llm)["session_id"]
    assert harness.store.follows(worker_id) == ["github:acme/app#7"]
    assert runtime.view(harness.store.session(worker_id))["follows"] == ["github:acme/app#7"]

    await call_tool(
        runtime, "report", worker_id, status="progress", summary="https://github.com/a/b/pull/3"
    )
    assert "github:a/b#3" in harness.store.follows(worker_id)
    await call_tool(runtime, "unfollow_thread", worker_id, thread="github:a/b#3")
    await call_tool(runtime, "close_worker", harness.router_id, session_id=worker_id)
    assert harness.store.follows(worker_id) == []


async def test_follow_ups_skip_the_router(harness):
    runtime = await harness.connect()
    result = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="Fix", instructions="fix it"
    )
    worker_id = json.loads(result.text_result_for_llm)["session_id"]
    router = harness.session(harness.router_id)
    await call_tool(runtime, "follow_thread", worker_id, thread="github:acme/app#7")

    delivered = await runtime.on_thread(
        "github", "github:acme/app#7", ["github:review:1"], "bob requested changes", "bob: nit"
    )
    assert delivered is True
    assert harness.session(worker_id).sent[-1].startswith('<message from="github" type="thread"')
    assert harness.session(worker_id).sent[-1].endswith("\nbob requested changes\n</message>")
    assert harness.kinds(worker_id)[-1] == ("inbound", "bob: nit")
    assert router.sent == [], "the Router is not woken for follow-ups"
    assert harness.kinds(harness.router_id)[-1] == ("notice", "acme/app#7 → “Fix”")
    again = await runtime.on_thread("github", "github:acme/app#7", ["github:review:1"], "x")
    assert again is False
    assert await runtime.on_thread("github", "github:o/r#1", ["k"], "nobody follows") is False


async def test_sent_reply_makes_the_drafter_follow_the_conversation(tmp_path):
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    slack_id = harness.store.open_sessions("slack")[0]["id"]

    async def send(channel, thread_ts, link):
        harness.mcp.sent_link = link
        await call_tool(
            runtime,
            "draft_slack_reply",
            slack_id,
            channel_id=channel,
            thread_ts=thread_ts,
            text="ok",
        )
        draft = harness.store.drafts()[-1]
        await runtime.drafts.resolve(draft["id"], DraftDecision(action="send"))

    await send(
        "C0DEPLOYS",
        "1712345678.000100",
        "https://acme.slack.com/archives/C0DEPLOYS/p1712345680000100?thread_ts=1712345678.000100",
    )
    await send("D0ALICE01", "", "https://acme.slack.com/archives/D0ALICE01/p1712345681000100")
    assert harness.store.follows(slack_id) == [
        "slack:C0DEPLOYS:1712345678.000100",
        "slack:D0ALICE01",
    ]
    assert harness.store.one("SELECT id FROM triggers WHERE key='slack:1712345680.000100'")


async def test_watchdog_reviews_and_intervenes(tmp_path):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    result = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="Migrate", instructions="migrate db"
    )
    worker_id = json.loads(result.text_result_for_llm)["session_id"]
    worker = harness.session(worker_id)
    now = time.time()

    worker.emit(
        "tool.execution_complete",
        result=SimpleNamespace(content="ERROR: connection refused (attempt 9), retrying"),
    )
    await settle(runtime)
    assert await runtime.watchdog.check(now) == [], "a fresh worker is fine"
    assert await runtime.watchdog.check(now + 1000) == [worker_id]
    assert await runtime.watchdog.check(now + 1100) == [], "one review per cooldown"
    watchdog_id = harness.store.open_sessions("watchdog")[0]["id"]
    packet = harness.session(watchdog_id).sent[-1]
    assert packet.startswith(
        f'<message from="system" type="review" worker="{worker_id[:8]}" title="Migrate">'
    )
    assert "no activity for 17 min" in packet and "migrate db" in packet
    assert "connection refused (attempt 9)" in packet
    assert harness.session(watchdog_id).options["available_tools"] == ["assess_worker"]

    refused = await call_tool(
        runtime,
        "assess_worker",
        worker_id,
        session_id=worker_id,
        verdict="stuck",
        action="nudge",
        reason="x",
    )
    assert "not available to the worker" in refused.text_result_for_llm

    async def assess(action, verdict="stuck"):
        await call_tool(
            runtime,
            "assess_worker",
            watchdog_id,
            session_id=worker_id,
            verdict=verdict,
            action=action,
            reason="The migration command has hung for 17 minutes.",
            message="Kill the hung migration and retry with --timeout 60.",
        )
        await settle(runtime)

    await assess("none", "on_track")
    assert worker.sent[-1].startswith('<message from="router" type="task"')
    await assess("interrupt")
    assert worker.aborted
    assert worker.sent[-1] == env(
        "watchdog", "nudge", "Kill the hung migration and retry with --timeout 60."
    )
    await assess("nudge")
    router_before = len(harness.session(harness.router_id).sent)
    await assess("nudge")
    assert len(worker.sent) == 3, "after two nudges the Router decides"
    escalation = harness.session(harness.router_id).sent[router_before]
    assert escalation.startswith('<message from="watchdog" type="escalation"')
    assert 'title="Migrate"' in escalation and "looks stuck" in escalation

    await call_tool(runtime, "report", worker_id, status="progress", summary="retrying")
    assert runtime.watchdog.state(worker_id)["nudges"] == 0


async def test_watchdog_can_be_switched_off(tmp_path):
    harness = Harness(tmp_path / "data", Settings(watchdog_enabled=False))
    runtime = await harness.connect()
    await call_tool(runtime, "spawn_worker", harness.router_id, title="A", instructions="a")
    assert await runtime.watchdog.check(time.time() + 5000) == []
    assert harness.store.open_sessions("watchdog") == []


async def test_a_broken_follower_falls_back_to_the_router(harness):
    runtime = await harness.connect()
    result = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="A", instructions="a"
    )
    worker_id = json.loads(result.text_result_for_llm)["session_id"]
    runtime.follow(worker_id, ["github:acme/app#7"])

    async def broken(prompt, mode=None):
        raise RuntimeError("session gone")

    harness.session(worker_id).send = broken
    assert await runtime.on_thread("github", "github:acme/app#7", ["k1"], "comment") is True
    assert harness.session(harness.router_id).sent[-1] == env(
        "github", "event", "comment", trust="untrusted"
    )
    assert harness.store.follows(worker_id) == []


async def test_events_without_followers_stay_unseen(harness):
    await harness.connect()
    assert await harness.runtime.on_thread("slack", "slack:D0ALICE01", ["slack:1.1"], "hi") is False
    assert not harness.store.one("SELECT id FROM triggers WHERE key='slack:1.1'")


def test_quiet_slack_follows_expire_and_refollowing_refreshes(tmp_path):
    from chiefofstaff.store import Store

    store = Store(tmp_path / "data")
    store.create_session("s1", "slack", "Slack", "idle")
    store.follow("slack:C0DEPLOYS:1712345678.000100", "s1")
    store.follow("github:acme/app#7", "s1")
    old = time.time() - 4 * 86400
    store.execute("UPDATE subscriptions SET touched=?", (old,))
    store.follow("github:acme/app#7", "s1")
    assert [row["thread"] for row in store.followed("")] == ["github:acme/app#7"]


async def test_watchdog_leaves_escalated_and_stopped_workers_alone(tmp_path):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    result = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="A", instructions="a"
    )
    worker_id = json.loads(result.text_result_for_llm)["session_id"]
    now = time.time()
    assert await runtime.watchdog.check(now + 1000) == [worker_id]
    watchdog_id = harness.store.open_sessions("watchdog")[0]["id"]
    await call_tool(
        runtime,
        "assess_worker",
        watchdog_id,
        session_id=worker_id,
        verdict="stuck",
        action="escalate",
        reason="hung",
    )
    assert await runtime.watchdog.check(now + 5000) == [], "the Router has it; nothing new happened"

    await runtime.workers.interrupt(worker_id)
    assert harness.kinds(worker_id)[-1] == ("notice", "Stopped by you.")
    assert await runtime.watchdog.check(now + 9000) == [], "the owner stopped it on purpose"


async def test_unchanged_workers_are_reviewed_less_and_less(tmp_path):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    await call_tool(runtime, "spawn_worker", harness.router_id, title="A", instructions="a")
    now = time.time()
    reviews = [bool(await runtime.watchdog.check(now + 1000 + minute * 60)) for minute in range(60)]
    assert sum(reviews) == 3, "after 0, 10 and 30 minutes; the next would be after 70"


async def test_a_done_worker_woken_by_a_follow_up_stays_done(harness):
    runtime = await harness.connect()
    result = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="A", instructions="a"
    )
    worker_id = json.loads(result.text_result_for_llm)["session_id"]
    await call_tool(
        runtime, "report", worker_id, status="done", summary="https://github.com/a/b/pull/1"
    )
    harness.session(worker_id).emit("session.idle")
    await settle(runtime)
    await runtime.on_thread("github", "github:a/b#1", ["github:ci:1"], "CI on abc: all passed")
    assert harness.store.session(worker_id)["status"] == "working"
    harness.session(worker_id).emit("session.idle")
    await settle(runtime)
    # It said nothing to the owner about it: asked to, once; its task stays done.
    assert "has not\nheard of it" in harness.session(worker_id).sent[-1]
    harness.session(worker_id).emit("session.idle")
    await settle(runtime)
    assert harness.store.session(worker_id)["status"] == "done"
    assert await runtime.watchdog.check(time.time() + 5000) == []


async def test_a_failed_watchdog_creation_is_retried(tmp_path):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    await call_tool(runtime, "spawn_worker", harness.router_id, title="A", instructions="a")
    create = harness.client.create_session
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return await create(**kwargs)

    harness.client.create_session = flaky
    now = time.time()
    with pytest.raises(RuntimeError):
        await runtime.watchdog.check(now + 1000)
    assert await runtime.watchdog.check(now + 1001) != []
    watchdog_id = harness.store.open_sessions("watchdog")[0]["id"]
    assert harness.session(watchdog_id).sent[-1].startswith('<message from="system" type="review"')


async def test_a_pr_a_worker_was_given_is_followed_and_its_news_reaches_the_owner(harness):
    runtime = await harness.connect()
    url = "https://github.com/acme/app/pull/7"
    result = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="Review #7",
        instructions=f"Review {url}", why_new="t",
    )  # fmt: skip
    worker = json.loads(result.text_result_for_llm)["session_id"]
    assert "github:acme/app#7" in harness.store.follows(worker), "followed without being told to"
    docs = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="Docs", instructions="docs", why_new="t"
    )
    other = json.loads(docs.text_result_for_llm)["session_id"]
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=other,
        text="Also look at github:acme/web#3",
    )  # fmt: skip
    assert "github:acme/web#3" in harness.store.follows(other)

    await call_tool(runtime, "report", worker, status="done", summary="Reviewed.")
    session = harness.session(worker)
    session.emit("session.idle")
    await settle(runtime)
    runtime.task_actions.accept(runtime.tasks.current(worker)["id"])
    await runtime.on_thread(
        "github", "github:acme/app#7", ["github:comment:1"], "bob replied to your comment: done"
    )
    await settle(runtime)
    assert "bob replied" in session.sent[-1]
    session.emit("session.idle")  # It handled it without a word to the owner.
    await settle(runtime)
    assert "the owner has not\nheard of it" in session.sent[-1], "asked once to tell the owner"
    session.emit("session.idle")
    await settle(runtime)
    assert (
        session.sent[-1].count("has not") == 1
        and len([s for s in session.sent if "has not\nheard" in s]) == 1
    ), "once"

    await runtime.on_thread("github", "github:acme/app#7", ["github:ci:2"], "CI on abc: all passed")
    await settle(runtime)
    harness.notifications.clear()
    await call_tool(runtime, "report", worker, status="progress", summary="#7: CI passed now.")
    await settle(runtime)
    shown = [m for m in harness.store.messages(harness.router_id) if m["kind"] == "inbound"]
    assert shown[-1]["text"] == "#7: CI passed now." and shown[-1]["meta"]["status"] == "progress"
    assert any("Update on" in text for _, text in harness.notifications)
