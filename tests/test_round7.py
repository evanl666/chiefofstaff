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
                           text="Answer Bob's review on the PR.", task="new", why_new="test",
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


async def test_the_router_and_the_summary_see_a_delivered_pr_to_fix_as_home_does(tmp_path):
    """R1: Home put a delivered task whose PR had CI failing under Needs you; "What needs
    me?", Focus and the daily summary said nothing needed the owner."""
    from datetime import UTC, datetime

    from chiefofstaff.digest import needs_marker

    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Add cube")
    worker, task_id = made["session_id"], made["task_id"]
    await call_tool(runtime, "report", worker, status="done", summary="Opened the PR.")
    await settle(runtime)
    runtime.tasks.update(task_id, status="done")
    runtime.follow(worker, [KEY], task_id=task_id)
    pr = {"label": "acme/app#72", "own": True, "state": "OPEN", "checks": "failing",
          "failing": ["lint"], "about": True}  # fmt: skip
    run_id = runtime.store.value("SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC",
                                 (task_id,))  # fmt: skip
    runtime.store.execute("UPDATE runs SET evidence=? WHERE id=?",
                          (json.dumps({"checkouts": [], "prs": [pr]}), run_id))  # fmt: skip
    task = runtime.tasks.get(task_id)
    assert needs_marker(runtime, task, datetime.now(UTC)) == "pr-failing"
    assert runtime.task_actions.own_pr_standing(task) == (
        "pr-failing",
        "CI failing on app#72 (lint)",
    )


def test_github_not_answering_is_told_apart_from_no_access():
    """R5: a later pattern of the same name hid this one: a 502 was never "not answering"."""
    from chiefofstaff.sources.github import failure_kind

    assert failure_kind("HTTP 502: Bad Gateway") == "transient"
    assert failure_kind("GitHub request timed out.") == "transient"
    assert failure_kind("gh: Resource not accessible (HTTP 403)") == "access"
    assert failure_kind("API rate limit exceeded (HTTP 403)") == "transient"


def test_a_cited_test_command_is_shown_without_its_redirections():
    """R6: "`uv run --with pytest pytest -q 2 >& 1`" in a report's verified line."""
    from chiefofstaff.receipts import cited_part

    line = "cd app && uv run --with pytest pytest -q 2>&1 | tail -5"
    assert cited_part("ran `uv run --with pytest pytest -q`", line) == (
        "uv run --with pytest pytest -q"
    )


async def test_stopping_a_follow_for_one_task_names_that_task(tmp_path):
    """R7: the result named the worker's newest task and a raw id, and said news "comes to
    you" while the worker still followed the PR for its other task."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Fix the parser")
    worker, first = made["session_id"], made["task_id"]
    await call_tool(runtime, "report", worker, status="done", summary="Done.")
    await settle(runtime)
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    text="Answer Bob's review.", task="new", title="Answer Bob's review",
                    why_new="another deliverable")  # fmt: skip
    [second] = [t["id"] for t in runtime.tasks.for_session(worker) if t["id"] != first]
    runtime.follow(worker, [KEY], task_id=first)
    runtime.follow(worker, [KEY], task_id=second)
    said = await call_tool(runtime, "stop_following", harness.router_id, session_id=first)
    text = said.text_result_for_llm
    assert "\u201cFix the parser\u201d" in text and "still goes to it" in text, text
    assert "come to you" not in text
    [notice] = [
        m["text"] for m in harness.store.messages(worker) if "No longer following" in m["text"]
    ]
    assert "\u201cFix the parser\u201d" in notice and first not in notice, notice


async def test_news_for_a_worker_open_in_a_terminal_waits_and_it_keeps_following(tmp_path):
    """R3: a PR comment for a worker open in a terminal made it stop following the PR, said
    nowhere; the Router promised to resend once it was closed, and was never told it was."""
    import time

    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Add cube")
    worker, task_id = made["session_id"], made["task_id"]
    await call_tool(runtime, "report", worker, status="done", summary="Opened the PR.")
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    runtime.follow(worker, [KEY], task_id=task_id)
    assert worker in await runtime.workers.release_idle(time.time() + 86400)
    harness.client.in_use = {runtime.copilot_id(worker): 4242}
    took = await runtime.on_thread(
        "github", KEY, ["github:issuecomment:9"], "bob: please add a test"
    )
    assert took, "not handed to the Router as if nobody followed it"
    assert harness.store.follows(worker) == [KEY], "it still follows the PR"
    assert json.loads(harness.store.get(f"heldnews:{worker}"))[0]["display"]
    refused = await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                              text="Add a test.", task="continue", task_id=task_id)  # fmt: skip
    assert refused.result_type == "failure", refused.text_result_for_llm
    harness.client.in_use = {}  # The owner exited it there.
    await runtime.workers.refresh_elsewhere()
    await settle(runtime)
    assert any("please add a test" in sent for sent in harness.session(worker).sent)
    assert not harness.store.get(f"heldnews:{worker}")
    told = [m["text"] for m in harness.store.messages(harness.router_id or "")]
    assert any("no longer open in a terminal" in text for text in told), told[-2:]
