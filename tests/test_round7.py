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
    assert failure_kind("gh: Resource not accessible (HTTP 403)") == "permission"
    assert failure_kind("API rate limit exceeded (HTTP 403)") == "rate_limited"


def test_a_cited_test_command_is_shown_without_its_redirections():
    """R6: "`uv run --with pytest pytest -q 2 >& 1`" in a report's verified line."""
    from chiefofstaff.receipts import cited_part

    line = "cd app && uv run --with pytest pytest -q 2>&1 | tail -5"
    assert cited_part("ran `uv run --with pytest pytest -q`", line) == (
        "uv run --with pytest pytest -q"
    )
    # Code review: a file after ">&" is where it writes, not an argument.
    assert cited_part("ran `pytest -q`", "pytest -q >& out.log; tail out.log") == "pytest -q"


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


def test_a_command_quoted_as_evidence_is_found_in_a_line_with_others():
    """D9: `python -m textkit 'hello big world'` ran (then `; pytest`), and its criterion
    said only "claimed": the `;` was taken as part of the quoted argument."""
    from chiefofstaff.receipts import backing

    line = "cd app; python -m textkit 'hello big world'; uv run --with pytest pytest -q"
    found = backing("ran `python -m textkit 'hello big world'`", [{"command": line}])
    assert found == {"command": line}


async def test_a_task_that_only_waited_on_a_pr_is_told_when_it_is_let_go(tmp_path):
    """G8: "Stop following PR #77 for task t…" left a watch-only task running, waiting on
    nothing, and its worker was never told."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Watch #72 until it is merged")
    worker, task_id = made["session_id"], made["task_id"]
    runtime.follow(worker, [KEY], task_id=task_id)
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    await call_tool(runtime, "stop_following", harness.router_id, session_id=task_id)
    await settle(runtime)
    told = [sent for sent in harness.session(worker).sent if "stopped you following" in sent]
    assert told and "report it now" in told[-1], harness.session(worker).sent[-1:]


async def test_a_worker_cut_off_by_copilot_restarting_is_told_what_restarted(tmp_path):
    """R8: after the Copilot CLI was killed, its worker's notice said "Chief of Staff
    restarted", and what the cut turn used was counted only at the app's next start."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Count the files")
    worker = made["session_id"]
    runtime.update_session(worker, status="working")
    synced = []
    original = runtime.workers.sync_all_usage

    async def counted():
        synced.append(1)
        await original()

    runtime.workers.sync_all_usage = counted
    await runtime.reconnect()
    await settle(runtime)
    notices = [m["text"] for m in harness.store.messages(worker) if m["kind"] == "notice"]
    assert "Copilot was restarted; asked the session to continue." in notices, notices
    assert synced, "what the cut turn used is read from its log at once"
    await runtime.stop()


async def test_work_that_waits_is_said_queued_not_started(tmp_path):
    """Bug bash 7 (suspicion): "Started “Add the README section”" while it waited for the task
    it comes after, and "Started" for a task that waited for a worker slot."""
    from chiefofstaff.models import Settings

    harness = Harness(tmp_path / "data", Settings(max_workers=1))
    runtime = await harness.connect()
    first = await spawn(runtime, harness.router_id, "Add truncate")
    runtime.update_session(first["session_id"], status="working")
    runtime.workers.states[first["session_id"]].busy = True
    await spawn(runtime, harness.router_id, "Add the README section", after=[first["task_id"]])
    await spawn(runtime, harness.router_id, "Count the files")
    notices = [text for kind, text in harness.kinds(harness.router_id) if kind == "notice"]
    said = [text.split(" · new because")[0] for text in notices]
    assert said == [
        "Started “Add truncate”",
        "Queued “Add the README section” · after “Add truncate”",
        "Queued “Count the files” · waits for a worker slot (1 busy)",
    ]


async def test_news_held_for_a_terminal_goes_out_after_a_restart_too(tmp_path):
    """R3 (review): the held news went out only when this app saw the terminal close; closed
    while it was not running, it never did."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Add cube")
    worker = made["session_id"]
    held = {"prompt": "news: bob commented", "display": "bob commented", "meta": {}}
    harness.store.set(f"heldnews:{worker}", json.dumps([held]))
    await runtime.workers.refresh_elsewhere()  # Never seen open here: closed meanwhile.
    await settle(runtime)
    assert any("bob commented" in sent for sent in harness.session(worker).sent)
    assert not harness.store.get(f"heldnews:{worker}")


async def test_a_pr_two_tasks_follow_is_counted_once_as_home_shows_it(tmp_path):
    """Code review: Home shows one row for a PR two delivered tasks of a worker follow; the
    Router, Focus and the summary counted both."""
    from chiefofstaff.digest import needs

    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Add cube")
    worker, first = made["session_id"], made["task_id"]
    await call_tool(runtime, "report", worker, status="done", summary="Opened the PR.")
    await settle(runtime)
    second = runtime.tasks.create(worker, "Answer the review", "x", status="running")["id"]
    await call_tool(runtime, "report", worker, status="done", summary="Pushed.", task_id=second)
    await settle(runtime)
    pr = {"label": "acme/app#72", "own": True, "state": "OPEN", "checks": "failing", "about": True}
    for task_id in (first, second):
        runtime.tasks.move(task_id, "done")
        runtime.follow(worker, [KEY], task_id=task_id)
        run_id = runtime.store.value(
            "SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC", (task_id,)
        )
        runtime.store.execute("UPDATE runs SET evidence=? WHERE id=?",
                              (json.dumps({"checkouts": [], "prs": [pr]}), run_id))  # fmt: skip
    found = needs(runtime)
    rows = [item for item in found.items if item.get("marker") == "pr-failing"]
    assert len(rows) == 1, [item["task"]["title"] for item in rows]
