"""Bug bash round 7, and what the owner found on a running instance (E11, E12)."""

import json

from conftest import Harness, call_tool, settle

KEY = "github:acme/app#72"


async def spawn(runtime, router, title, **extra):
    result = await call_tool(runtime, "spawn_worker", router, title=title, instructions=title,
                             why_new="test", **extra)  # fmt: skip
    return json.loads(result.text_result_for_llm)


async def test_an_unreadable_pr_is_said_on_its_own_task_and_let_go_for_one_task(tmp_path):
    """E11: "Can't read …#72" showed on every task of the worker that followed it, said no
    reason, and "stop following" there let it go for all of them."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Fix the parser")
    worker, maker = made["session_id"], made["task_id"]
    await call_tool(runtime, "report", worker, status="done", summary="Opened the PR.")
    await settle(runtime)
    sent = await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                           text="Answer Bob's review on the PR.", task="new",
                           title="Answer Bob's review")  # fmt: skip
    assert sent.result_type == "success", sent.text_result_for_llm
    [other] = [t["id"] for t in runtime.tasks.for_session(worker) if t["id"] != maker]
    runtime.task_actions.record_opened(maker, ["https://github.com/acme/app/pull/72"])
    runtime.follow(worker, [KEY], task_id=maker)
    runtime.follow(worker, [KEY], task_id=other)
    harness.store.set(f"ghfail:{KEY}", "3")
    harness.store.set(f"ghwhy:{KEY}", json.dumps(
        {"kind": "access", "step": "CI checks", "error": "HTTP 403", "at": ""}))  # fmt: skip
    views = {t: runtime.task_actions.view(runtime.tasks.get(t)) for t in (maker, other)}
    assert views[maker]["watch_failing"] == [KEY], "on the task the PR is"
    assert views[other]["watch_failing"] == [], "not again on the other"
    assert KEY not in views[other]["watching"], "nor said to be waited on there"
    assert views[maker]["watch_failing_why"][KEY]["step"] == "CI checks"
    # Let go for the other task: its worker still follows it for the one that made it.
    assert runtime.unfollow_for(worker, KEY, other) is False
    assert harness.store.follows(worker) == [KEY]
    assert harness.store.follow_tasks(worker, KEY) == [maker]
    assert runtime.follow(worker, [KEY], given=False, task_id=other) == [], "not again by itself"
    # The Router stops it for the task that made it too: then it is let go.
    result = await call_tool(runtime, "stop_following", harness.router_id, session_id=maker)
    assert result.result_type == "success", result.text_result_for_llm
    assert harness.store.follows(worker) == []
