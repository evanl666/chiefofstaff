"""The demo scenario end to end: the real runtime with scripted sessions, no accounts."""

import asyncio
import json

from chiefofstaff import demo
from chiefofstaff.bus import Bus
from chiefofstaff.models import DraftDecision
from chiefofstaff.store import Store
from chiefofstaff.watchdog import WatchConfig


async def until(check, seconds=20):
    for _ in range(int(seconds / 0.1)):
        if result := check():
            return result
        await asyncio.sleep(0.1)
    raise AssertionError("timed out")


async def test_the_demo_scenario_plays_through(tmp_path):
    project = demo.make_project(tmp_path / "projects")
    store = Store(tmp_path / "data")
    runtime = demo.DemoRuntime(
        store,
        Bus(),
        project,
        autoplay=False,
        watch=WatchConfig(interval=0.3, stall=3, long=600, cooldown=30),
        slack_delay=0,
    )
    await runtime.start()
    try:
        await runtime.owner_send(runtime.router_id(), demo.KICKOFF)
        tasks = await until(lambda: len(runtime.tasks.listed()) == 3 and runtime.tasks.listed())
        titles = {task["title"]: task for task in tasks}
        rate = titles["Rate-limit login attempts"]
        assert [dep["title"] for dep in runtime.tasks.deps(rate["id"])] == [
            "Fix the flaky login test"
        ]

        question = await until(lambda: store.drafts() and store.drafts()[0])
        assert question["kind"] == "decision" and question["target"]["recommendation"]
        await runtime.drafts.resolve(
            question["id"], DraftDecision(action="answer", text="Per account")
        )

        runtime.slack.wake()
        draft = await until(lambda: next((d for d in store.drafts() if d["kind"] == "slack"), None))
        assert draft["target"]["to"] == "Maya Chen"
        await runtime.drafts.resolve(draft["id"], DraftDecision(action="send"))
        assert runtime.slack.mcp.sent[0]["channel_id"] == demo.MAYA_DM

        done = await until(
            lambda: (
                all(t["status"] == "review" for t in runtime.tasks.listed())
                and runtime.tasks.listed()
            )
        )
        assert len(done) == 3
        reports = {t["title"]: t["result"] for t in done}
        assert "per account" in reports["Rate-limit login attempts"]
        nudged = store.all("SELECT text FROM messages WHERE kind='watchdog'")
        assert nudged, "the Watchdog unstuck the research worker"

        fix = runtime.task_actions.view(runtime.tasks.get(titles["Fix the flaky login test"]["id"]))
        await until(
            lambda: runtime.task_actions.view(runtime.tasks.get(fix["id"]))["receipts"]["checkouts"]
        )
        found = runtime.task_actions.view(runtime.tasks.get(fix["id"]))["receipts"]
        assert found["tests"]["passed"] and found["changed"]["commits"] == 1
        assert found["verdict"] == "verified" and found["tests"]["failed_before"]
        assert json.loads(json.dumps(found))  # The UI gets plain JSON.
        limited = runtime.task_actions.view(runtime.tasks.get(rate["id"]))["receipts"]
        assert [c["state"] for c in limited["criteria"]] == ["verified", "claimed"]

        # The owner changes the decision: the finished rate limiting is redone and confirmed.
        await runtime.owner_send(
            runtime.router_id(), "Change of plan: limit login attempts per IP address only"
        )
        redone = await until(
            lambda: (
                (task := runtime.tasks.get(rate["id"]))["status"] == "review"
                and "per IP address only" in task["result"]
                and task
            )
        )
        assert runtime.tasks.runs(redone["id"])[-1]["reason"] == "decision changed"
        [impact] = store.all("SELECT status FROM drafts WHERE kind='change'")
        assert impact["status"] == "answered", "its worker confirmed"
    finally:
        await runtime.stop()
