"""What the owner and the Router see of work in progress, and how the owner reaches one worker;
plus the failures a real setup throws at sessions (plugin hooks, efforts, late reports)."""

import asyncio
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeSession, Harness, call_tool, settle

from chiefofstaff.models import DraftDecision, Settings


async def spawn(runtime, router, title, **extra):
    extra.setdefault("instructions", title)
    extra.setdefault("why_new", "test")
    result = await call_tool(runtime, "spawn_worker", router, title=title, **extra)
    return json.loads(result.text_result_for_llm)["session_id"]


def step(session, tool, arguments):
    session.emit("assistant.turn_start")
    session.emit("tool.execution_start", tool_name=tool, arguments=arguments, tool_call_id="c1")


async def test_progress_shows_the_step_it_is_in_and_what_it_said_last(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Repopulate the table")
    session = harness.session(worker)
    session.emit(
        "assistant.message",
        content="Running the dry run on a 0.1% sample.",
        tool_requests=[{"name": "bash"}],
    )
    step(session, "bash", {"command": "spark-submit job.py --sample 0.001"})
    await settle(runtime)
    view = runtime.view(harness.store.session(worker))
    assert view["step"] == "$ spark-submit job.py --sample 0.001" or "spark-submit" in view["step"]
    assert view["step_since"] and view["said"] == "Running the dry run on a 0.1% sample."

    steps = runtime.states[worker].steps
    steps["c1"] = (time.time() - 600, steps["c1"][1])  # Ten minutes into that step.
    read = json.loads(
        (
            await call_tool(runtime, "read_session", harness.router_id, session_id=worker[:8])
        ).text_result_for_llm
    )
    assert read["status"] == "working" and "in a step for 10 min" in read["now"]
    assert "spark-submit" in read["now"] and "0.1% sample" in read["now"]
    assert any("said: Running the dry run" in entry for entry in read["latest"])
    assert any("1 step(s), the last" in entry for entry in read["latest"])
    listed = json.loads(
        (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
    )
    [row] = [item for item in listed["sessions"] if item["session_id"] == worker]
    assert "in a step for 10 min" in row["now"]

    # The owner asks it directly: it reads that only after the step, and is told so at once.
    await runtime.owner_send(worker, "progress?")
    kinds = harness.kinds(worker)
    assert kinds[-2] == ("owner", "progress?")
    assert kinds[-1][0] == "notice" and kinds[-1][1].startswith("It reads this when its current")
    assert "in a step for 10 min" in kinds[-1][1]
    session.emit("tool.execution_complete", tool_call_id="c1", success=True)
    await settle(runtime)
    assert runtime.view(harness.store.session(worker))["step"] == "", "between steps"


async def test_the_router_reaches_a_busy_worker_within_its_turn(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Long job")
    session = harness.session(worker)
    session.emit("assistant.turn_start")
    await call_tool(
        runtime,
        "message_session",
        harness.router_id,
        session_id=worker,
        text="Send a short status now.",
    )
    assert session.modes[-1] == "immediate", "steered into the running turn"
    await call_tool(
        runtime,
        "message_session",
        harness.router_id,
        session_id=worker,
        text="Then write the docs.",
        task="new",
        title="Docs",
    )
    assert session.modes[-1] == "enqueue", "a separate task waits for the turn to end"


async def test_the_owner_addresses_one_worker_from_the_router_chat(harness):
    runtime = await harness.connect()
    first = await spawn(runtime, harness.router_id, "Backend")
    second = await spawn(runtime, harness.router_id, "Frontend")
    router = harness.session(harness.router_id)
    sent = len(router.sent)
    await runtime.owner_send_to(harness.router_id, second, "Use the new API name: accounts")
    assert "Use the new API name: accounts" in harness.session(second).sent[-1]
    assert not any("accounts" in prompt for prompt in harness.session(first).sent)
    record = harness.store.messages(harness.router_id)[-1]
    assert record["kind"] == "owner" and record["meta"]["to"] == second
    await settle(runtime)
    assert len(router.sent) == sent, "the Router is not woken up for it"
    await runtime.owner_send(harness.router_id, "How is it going?")
    told = router.sent[-1]
    assert 'type="to-worker"' in told and "Use the new API name: accounts" in told
    assert 'from="system" type="to-worker"' in told, "not an owner message of its own"
    with pytest.raises(ValueError, match="Address a worker"):
        await runtime.owner_send_to(harness.router_id, harness.router_id, "x")


async def test_one_message_per_answer_to_the_owner(harness):
    runtime = await harness.connect()
    router = harness.session(harness.router_id)
    await runtime.owner_send(harness.router_id, "Is the table done?")
    router.emit("user.message", content=router.sent[-1])
    await settle(runtime)
    refused = await call_tool(runtime, "notify_owner", harness.router_id, text="Done: 10,756 rows")
    assert "say this in your reply" in refused.text_result_for_llm
    assert not [m for m in harness.store.messages(harness.router_id) if m["kind"] == "update"]
    router.emit("user.message", content='<message from="worker:abc" type="report">\nx\n</message>')
    await settle(runtime)
    await call_tool(runtime, "notify_owner", harness.router_id, text="Needs you: pick a filter")
    assert harness.kinds(harness.router_id)[-1] == ("update", "Needs you: pick a filter")


async def test_the_router_knows_sessions_open_in_a_terminal(harness, tmp_path):
    runtime = await harness.connect()
    context = SimpleNamespace(working_directory=str(tmp_path), repository="", branch="")
    harness.client.metas = [
        SimpleNamespace(
            session_id=identifier,
            summary=summary,
            is_remote=False,
            modified_time=datetime(2026, 11, day),
            context=context,
        )
        for identifier, summary, day in [("term-1", "Tune the ranker", 3), ("old-1", "Old work", 1)]
    ]
    harness.client.in_use = {"term-1": 4242}
    harness.client.logs["term-1"] = [
        {"type": "user.message", "data": {"content": "Try lambda 0.3"}, "timestamp": "t1"},
        {"type": "assistant.message", "data": {"content": "NDCG went up 2%."}, "timestamp": "t2"},
    ]
    listed = json.loads(
        (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
    )
    assert [item["session_id"] for item in listed["terminal_sessions"]] == ["term-1"]
    read = json.loads(
        (
            await call_tool(runtime, "read_session", harness.router_id, session_id="term-1")
        ).text_result_for_llm
    )
    assert read["open_in_terminal"] and read["latest"][-1] == "assistant: NDCG went up 2%."
    refused = await call_tool(
        runtime, "message_session", harness.router_id, session_id="term-1", text="Try 0.5"
    )
    assert "open in the owner's terminal" in refused.text_result_for_llm
    assert not harness.store.find_session("term-1")
    # One not open anywhere continues here only once the owner agrees.
    harness.client.sessions["old-1"] = FakeSession("old-1", {})
    asked = await call_tool(
        runtime, "message_session", harness.router_id, session_id="old-1", text="Rerun the old eval"
    )
    assert "they were asked" in asked.text_result_for_llm
    assert not harness.store.find_session("old-1"), "not taken over by the Router itself"
    [card] = [draft for draft in harness.store.drafts() if draft["kind"] == "adopt"]
    assert card["target"]["title"] == "Old work" and card["target"]["text"] == "Rerun the old eval"
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer"))
    assert harness.store.find_session("old-1")["role"] == "worker"
    task = runtime.tasks.current("old-1")
    assert task["title"] == "Rerun the old eval" and task["status"] == "running"
    assert "Rerun the old eval" in harness.session("old-1").sent[-1]


async def test_a_plugin_hook_that_fails_does_not_fail_the_worker(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Explain the solution")
    session = harness.session(worker)
    session.emit("assistant.turn_start")
    message = (
        'sessionStart hook from "context-repo@linkedin-plugins" failed in the session sandbox: '
        "Hook command failed with code 1. If it needs more access, allow it in the sandbox policy."
    )
    session.emit("session.error", error_type="hook", message=message)
    session.emit(
        "session.error", error_type="hook", message=message.replace("sessionStart", "agentStop")
    )
    await settle(runtime)
    row = harness.store.session(worker)
    assert row["status"] == "working" and runtime.states[worker].busy
    assert runtime.tasks.current(worker)["status"] == "running"
    notes = [text for kind, text in harness.kinds(worker) if "plugin hook failed" in text]
    assert len(notes) == 1 and "Also writable in the sandbox" in notes[0]
    assert not [m for m in harness.store.messages(harness.router_id) if m["kind"] == "inbound"]
    session.emit("session.error", error_type="model", message="overloaded")
    await settle(runtime)
    assert harness.store.session(worker)["status"] == "failed", "other errors still fail it"


async def test_folders_also_writable_in_the_sandbox(tmp_path):
    journal = tmp_path / "journal"
    journal.mkdir()
    harness = Harness(
        tmp_path / "data", Settings(sandbox_writable=[str(journal), ""], worker_sandbox=True)
    )
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Work")
    policy = runtime.sandbox_policy(harness.store.session(worker))
    assert str(journal.resolve()) in policy.readwrite
    with pytest.raises(ValueError, match="whole home"):
        Settings(sandbox_writable=["~"])


async def test_the_chosen_effort_applies_before_the_default_model_is_known(tmp_path):
    harness = Harness(tmp_path / "data", Settings(role_efforts={"worker": "high"}))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Think hard")
    assert harness.session(worker).options["reasoning_effort"] == "high"
    assert "model" not in harness.session(worker).options, "Copilot's default, not pinned"

    # Copilot's default is Auto, which takes no effort: opened without one, said once.
    create = harness.client.create_session

    async def auto_default(**options):
        if "reasoning_effort" in options:
            raise RuntimeError('Reasoning effort is not supported when using the "auto" model.')
        return await create(**options)

    harness.client.create_session = auto_default
    second = await spawn(runtime, harness.router_id, "Also think")
    assert harness.store.session(second)["status"] == "working"
    assert "reasoning_effort" not in harness.session(second).options
    await spawn(runtime, harness.router_id, "Third")
    notes = [
        text
        for row in harness.store.open_sessions()
        for kind, text in harness.kinds(row["id"])
        if "takes no thinking effort" in text
    ]
    assert len(notes) == 1


async def test_a_late_report_does_not_reopen_a_cancelled_task(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Research the table")
    task = runtime.tasks.current(worker)
    await runtime.workers.interrupt(worker)
    await runtime.task_actions.cancel(task["id"], "not needed")
    router_before = len(harness.store.messages(harness.router_id))
    reply = await call_tool(runtime, "report", worker, status="done", summary="Found 18 columns")
    assert "stays in your session" in reply.text_result_for_llm
    assert runtime.tasks.get(task["id"])["status"] == "cancelled"
    await settle(runtime)
    assert len(harness.store.messages(harness.router_id)) == router_before
    # The owner asks it for more after all: that work comes back for review.
    await runtime.owner_send(worker, "Actually, list the columns for me")
    await call_tool(runtime, "report", worker, status="done", summary="The 18 columns: …")
    assert runtime.tasks.get(task["id"])["status"] == "review"


async def test_a_long_report_reaches_the_owner_whole(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Explain")
    table = "| step | rank |\n|---|---|\n" + "".join(f"| s{i} | {i} |\n" for i in range(400))
    summary = "Walkthrough:\n\n" + table
    assert len(summary) > 4000
    await call_tool(runtime, "report", worker, status="done", summary=summary)
    assert runtime.tasks.current(worker)["result"] == summary
    huge = "line\n" * 8000
    await runtime.owner_send(worker, "again please, with everything")
    await call_tool(runtime, "report", worker, status="done", summary=huge)
    result = runtime.tasks.current(worker)["result"]
    assert len(result) <= 30100 and result.endswith("the whole report is in the worker's session)")


async def test_files_workers_wrote_open_for_the_owner(harness, tmp_path):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Write it up")
    row = harness.store.session(worker)
    (tmp_path / "secret.txt").write_text("not yours")
    walkthrough = runtime.workspaces.workspace(row) / "walkthrough_zh.md"
    walkthrough.parent.mkdir(parents=True, exist_ok=True)
    walkthrough.write_text("# 逐步讲解\n\n1. 社区\n")
    shown = await runtime.read_file(str(walkthrough))
    assert shown["name"] == "walkthrough_zh.md" and shown["text"].startswith("# 逐步讲解")
    with pytest.raises(ValueError, match="workers' own folders"):
        await runtime.read_file(str(tmp_path / "secret.txt"))
    with pytest.raises(ValueError, match="workers' own folders"):
        await runtime.read_file(str(walkthrough.parent / ".." / ".." / ".." / "secret.txt"))
    (walkthrough.parent / "run.log").write_text("ok")
    relative = (
        await runtime.read_file("run.log", worker)
        if row["cwd"] == str(walkthrough.parent)
        else await runtime.read_file(str(walkthrough.parent / "run.log"))
    )
    assert relative["text"] == "ok"
    (walkthrough.parent / "data.bin").write_bytes(b"\0\1\2")
    with pytest.raises(ValueError, match="not a text file"):
        await runtime.read_file(str(walkthrough.parent / "data.bin"))


async def test_repeated_instructions_are_kept_for_learning(harness):
    runtime = await harness.connect()
    await runtime.owner_send(harness.router_id, "progress?")
    await runtime.owner_send(
        harness.router_id, "先跑小规模 smoke 再跑全量, use the Darwin config I gave"
    )
    kinds = [(row["kind"], row["owner"]) for row in runtime.learning.unlearned()]
    assert kinds == [("instruction", "先跑小规模 smoke 再跑全量, use the Darwin config I gave")]


async def test_watchdog_nudges_say_why(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Filter products")
    watchdog = await runtime.watchdog.session_id()
    await call_tool(
        runtime,
        "assess_worker",
        watchdog,
        session_id=worker,
        verdict="looping",
        action="nudge",
        reason="Edited filter.py 9 times with no new result.",
        message="Write the rule once, then run it.",
    )
    nudge = harness.store.messages(worker)[-1]
    assert nudge["kind"] == "watchdog" and nudge["meta"]["verdict"] == "looping"
    assert nudge["meta"]["reason"] == "Edited filter.py 9 times with no new result."


async def test_replies_to_review_comments_on_the_workers_own_pr(harness):
    runtime = await harness.connect()
    worker = await spawn(
        runtime,
        harness.router_id,
        "Address review comments",
        instructions="Fix ygchen's 5 comments on https://github.com/acme/model/pull/1043",
    )
    harness.gh_replies["api repos/acme/model/pulls/comments/21"] = json.dumps(
        {
            "user": {"login": "ygchen"},
            "body": "Opt-out titles are not removed",
            "path": "seeds.py",
            "line": 117,
            "html_url": "https://github.com/acme/model/pull/1043#discussion_r21",
            "pull_request_url": "https://api.github.com/repos/acme/model/pulls/1043",
        }
    )
    harness.gh_replies["api repos/acme/model/pulls/1043"] = json.dumps(
        {"state": "open", "head": {"sha": "f61047fa", "ref": "fix"}, "user": {"login": "x"}}
    )
    harness.gh_replies["api repos/acme/model/pulls/1043/comments/21/replies"] = json.dumps(
        {"id": 99, "html_url": "https://github.com/acme/model/pull/1043#discussion_r99"}
    )
    unrelated = await call_tool(
        runtime,
        "draft_github_review",
        worker,
        repo="acme/other",
        number=5,
        body="x",
        in_reply_to=21,
    )
    assert unrelated.result_type == "failure", "not a PR its task is about, not connected"
    await call_tool(
        runtime,
        "draft_github_review",
        worker,
        repo="acme/model",
        number=1043,
        body="Fixed in f61047fa: opted-out titles are removed first.",
        in_reply_to=21,
    )
    [draft] = harness.store.drafts()
    assert draft["target"]["comment"]["author"] == "ygchen" and draft["target"]["via"] == "task"
    sent = await runtime.drafts.resolve(draft["id"], DraftDecision(action="send"))
    assert sent["outcome"] == "https://github.com/acme/model/pull/1043#discussion_r99"
    arguments, payload = harness.gh_calls[-1]
    assert arguments[1] == "repos/acme/model/pulls/1043/comments/21/replies"
    assert payload == {"body": "Fixed in f61047fa: opted-out titles are removed first."}
    assert "github:acme/model#1043" in harness.store.follows(worker)


async def test_the_current_step_is_the_longest_running_one(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Build")
    session = harness.session(worker)
    session.emit("assistant.turn_start")
    session.emit(
        "tool.execution_start",
        tool_name="bash",
        arguments={"command": "make test-all"},
        tool_call_id="long",
    )
    session.emit(
        "tool.execution_start", tool_name="view", arguments={"path": "a.py"}, tool_call_id="quick"
    )
    session.emit("tool.execution_complete", tool_call_id="quick", success=True)
    await settle(runtime)
    assert "make test-all" in runtime.view(harness.store.session(worker))["step"]
    await runtime.reconnect()
    await settle(runtime)
    assert runtime.view(harness.store.session(worker))["step"] == "", "no step after a reconnect"


async def test_the_owner_sees_the_step_note_where_they_wrote(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Long query")
    step(harness.session(worker), "bash", {"command": "trino -f big.sql"})
    await settle(runtime)
    runtime.states[worker].steps["c1"] = (time.time() - 400, "bash trino -f big.sql")
    await runtime.owner_send_to(harness.router_id, worker, "progress?")
    kind, text = harness.kinds(harness.router_id)[-1]
    assert kind == "notice" and "It reads this when its current step ends" in text


async def test_a_question_about_a_cancelled_task_does_not_revive_it(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Research")
    task = runtime.tasks.current(worker)
    await runtime.task_actions.cancel(task["id"])
    await runtime.owner_send(worker, "What had you found before I cancelled it?")
    await call_tool(runtime, "report", worker, status="progress", summary="Found 18 columns.")
    await call_tool(runtime, "report", worker, status="question", summary="Want the list?")
    task = runtime.tasks.get(task["id"])
    assert task["status"] == "cancelled" and task["progress"] == "Want the list?"
    assert not [run for run in runtime.tasks.runs(task["id"]) if not run["ended_at"]]


async def test_only_many_instructions_start_learning_and_canned_ones_do_not_count(harness):
    runtime = await harness.connect()
    for number in range(6):
        await runtime.owner_send(
            harness.router_id, f"Please look into the flaky test number {number}"
        )
    await runtime.owner_send(
        harness.router_id, "Build my profile: distill what my work says", learn=False
    )
    await settle(runtime)
    assert len(runtime.learning.unlearned()) == 6
    assert not runtime.learning.learners(), "six plain requests are no pattern yet"


async def test_a_needed_decision_still_reaches_the_owner_in_an_answer_turn(harness):
    runtime = await harness.connect()
    router = harness.session(harness.router_id)
    await runtime.owner_send(harness.router_id, "thanks")
    router.emit("user.message", content=router.sent[-1])
    await settle(runtime)
    await call_tool(
        runtime, "notify_owner", harness.router_id, text="Drop the legacy table?", needs_action=True
    )
    assert harness.kinds(harness.router_id)[-1] == ("update", "Drop the legacy table?")


async def test_a_worker_cannot_grant_itself_a_pr_to_draft_on(harness):
    runtime = await harness.connect()
    worker = await spawn(
        runtime,
        harness.router_id,
        "Fix",
        instructions="Address the comments on github:acme/app#123",
    )
    harness.gh_replies["api repos/acme/app/pulls/7"] = json.dumps(
        {"state": "open", "head": {"sha": "abc", "ref": "someone-else"}}
    )
    await call_tool(runtime, "follow_thread", worker, thread="github:acme/app#7")
    refused = await call_tool(
        runtime, "draft_github_review", worker, repo="acme/app", number=7, body="LGTM"
    )
    assert refused.result_type == "failure", "following it itself grants nothing"
    harness.gh_replies["api repos/acme/app/pulls/1"] = json.dumps(
        {"state": "open", "head": {"sha": "abc", "ref": "x"}}
    )
    named = await call_tool(
        runtime, "draft_github_review", worker, repo="acme/app", number=1, body="LGTM"
    )
    assert named.result_type == "failure", "#1 is not #123"
    harness.gh_replies["api repos/acme/app/pulls/123"] = json.dumps(
        {"state": "open", "head": {"sha": "abc", "ref": "x"}}
    )
    given = await call_tool(
        runtime, "draft_github_review", worker, repo="acme/app", number=123, body="Addressed"
    )
    assert given.result_type != "failure"


async def test_a_session_started_in_home_never_gets_home_to_write(harness):
    runtime = await harness.connect()
    row = harness.store.create_session("adopted-1", "worker", "Old", "idle", cwd=str(Path.home()))
    roots = runtime.workspaces.writable(row)
    assert str(Path.home()) not in roots and roots
    with pytest.raises(ValueError, match="workers' own folders"):
        await runtime.read_file(str(Path.home() / ".zshrc"))


def test_folders_the_sandbox_never_opens():
    above_home = str(Path.home().parent)  # /Users on macOS, /home on Linux
    for path in (above_home, "~", "~/.copilot", "~/.copilot/session-state", "~/.ssh"):
        with pytest.raises(ValueError):
            Settings(sandbox_writable=[path])
    assert Settings(sandbox_writable=["~/.copilot/context-repo"]).sandbox_writable == [
        str(Path("~/.copilot/context-repo").expanduser().resolve())
    ]


async def test_news_about_a_finished_task_does_not_reopen_it(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Ship it")
    await call_tool(runtime, "report", worker, status="done", summary="PR opened")
    task = runtime.tasks.current(worker)
    assert task["status"] == "review"
    await call_tool(runtime, "report", worker, status="progress", summary="Renamed x as bob asked")
    assert runtime.tasks.get(task["id"])["status"] == "review", "still ready for review"
    runtime.task_actions.accept(task["id"])
    await call_tool(runtime, "report", worker, status="progress", summary="CI is green again")
    task = runtime.tasks.get(task["id"])
    assert task["status"] == "done" and task["progress"] == "CI is green again"
    assert not [run for run in runtime.tasks.runs(task["id"]) if not run["ended_at"]]


async def test_one_card_per_session_to_continue_and_the_router_hears_the_answer(harness, tmp_path):
    runtime = await harness.connect()
    context = SimpleNamespace(working_directory=str(tmp_path), repository="", branch="")
    harness.client.metas = [
        SimpleNamespace(
            session_id="old-2",
            summary="Old eval",
            is_remote=False,
            modified_time=datetime(2026, 11, 1),
            context=context,
        )
    ]
    harness.client.sessions["old-2"] = FakeSession("old-2", {})
    for text in ("Rerun the eval", "Rerun the eval on the new split"):
        await call_tool(
            runtime, "message_session", harness.router_id, session_id="old-2", text=text
        )
    [card] = [draft for draft in harness.store.drafts() if draft["kind"] == "adopt"]
    assert card["target"]["text"] == card["text"] == "Rerun the eval on the new split"
    router = harness.session(harness.router_id)
    before = len(router.sent)
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer"))
    await settle(runtime)
    assert len(router.sent) > before and "continued “Old eval” here" in router.sent[-1]


async def test_a_blocker_on_finished_work_needs_the_owner(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Login")
    await call_tool(runtime, "report", worker, status="done", summary="Login works")
    task = runtime.tasks.current(worker)
    await runtime.owner_send(worker, "Also make it work with SSO login, please")
    await call_tool(
        runtime,
        "report",
        worker,
        status="blocked",
        summary="SSO needs a client secret I don't have",
    )
    assert runtime.tasks.get(task["id"])["status"] == "waiting", "shown under Needs you"


async def test_a_merged_pr_settles_its_task_after_later_reports(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Login")
    await call_tool(
        runtime, "report", worker, status="done", summary="Done: https://github.com/acme/app/pull/7"
    )
    task = runtime.tasks.current(worker)
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Logout too"
    )
    await call_tool(runtime, "report", worker, status="done", summary="Logout too.")
    assert runtime.tasks.get(task["id"])["status"] == "review"
    runtime.unfollow("github:acme/app#7", finished=True, merged=True)
    assert runtime.tasks.get(task["id"])["status"] == "done"


async def test_list_sessions_stays_readable_with_many_sessions(tmp_path):
    harness = Harness(tmp_path / "data", Settings(max_workers=12))
    runtime = await harness.connect()
    for number in range(40):
        worker = await spawn(runtime, harness.router_id, f"Task {number} on the features table")
        await call_tool(runtime, "report", worker, status="done", summary="Details. " * 120)
    listed = (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
    assert len(listed.encode()) < 50_000, "Copilot hands larger outputs over as a file"
    data = json.loads(listed)
    assert "terminal_sessions" in data and len(data["sessions"]) >= 40


async def test_workers_share_only_caches_whose_contents_are_checked(tmp_path, monkeypatch):
    home = tmp_path / "home"
    names = (".gradle/caches", ".gradle/daemon", ".gradle/wrapper", ".npm/_cacache")
    for name in (*names, ".m2/repository", ".cache/pre-commit"):
        (home / name).mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    harness = Harness(tmp_path / "data", Settings(worker_sandbox=True))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Build")
    policy = runtime.sandbox_policy(harness.store.session(worker))
    assert str(home / ".npm/_cacache") in policy.readwrite, "npm checks each package"
    assert str(home / ".npm") not in policy.readwrite, "not _npx: npx runs what is there"
    for unchecked in (".gradle/caches", ".gradle/wrapper", ".m2/repository", ".cache/pre-commit"):
        assert str(home / unchecked) not in policy.readwrite, f"{unchecked} runs as it is"
    assert str(home / ".gradle/daemon") in policy.denied, "not the owner's own daemons"
    prompt = harness.session(worker).options["system_message"]["content"]
    assert "GRADLE_RO_DEP_CACHE=$HOME/.gradle/caches" in prompt, "Gradle's cache, read-only"
    harness.store.execute("UPDATE sessions SET slack=1 WHERE id=?", (worker,))
    assert (
        str(home / ".npm/_cacache")
        not in runtime.sandbox_policy(harness.store.session(worker)).readwrite
    )
    harness.store.execute("UPDATE sessions SET slack=0 WHERE id=?", (worker,))
    harness.store.save_settings(Settings(shared_caches=False, worker_sandbox=True))
    policy = runtime.sandbox_policy(harness.store.session(worker))
    assert str(home / ".npm/_cacache") not in policy.readwrite
    assert str(home / "Library/Jupyter/runtime") in policy.denied, "denied before it exists"


def with_origin(repo: Path):
    origin = repo.parent / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(origin)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)
    subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin"], check=True)


async def test_workers_starting_together_fetch_only_the_default_branch_once(git_repo, monkeypatch):
    from chiefofstaff import workspaces

    with_origin(git_repo)
    fetches = []
    real = workspaces.git

    async def counted(*arguments, **options):
        if "fetch" in arguments:
            fetches.append(arguments)
        return await real(*arguments, **options)

    monkeypatch.setattr(workspaces, "git", counted)
    monkeypatch.setattr(workspaces, "FETCHED", {})
    results = await asyncio.gather(*(workspaces.base_ref(git_repo) for _ in range(3)))
    assert [ref for ref, _ in results] == ["origin/main"] * 3
    assert len(fetches) == 1 and fetches[0][-1] == "main", fetches


async def test_workers_reach_localhost_unless_they_read_slack(harness):
    harness.store.save_settings(Settings(worker_sandbox=True))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Run the server tests")
    assert runtime.sandbox_policy(harness.store.session(worker)).local_network
    assert (
        "Servers on localhost work" in harness.session(worker).options["system_message"]["content"]
    )
    harness.store.execute("UPDATE sessions SET slack=1 WHERE id=?", (worker,))
    assert not runtime.sandbox_policy(harness.store.session(worker)).local_network


PULL = {
    "state": "open",
    "head": {"sha": "c0ffee", "ref": "feature"},
    "user": {"login": "bob"},
    "requested_reviewers": [{"login": "Evan-Corp"}],
}
FILES = [
    {
        "filename": "model/train.py",
        "patch": "@@ -10,3 +10,4 @@ def train():\n     a = 1\n-    b = 2\n+    b = 3\n+    c = 4\n"
        "     return a",
    },
    {"filename": "data/big.parquet"},  # Binary: no lines to comment on.
    {"filename": "data/huge.sql", "additions": 90000},  # Too large for a patch: not checked.
]


async def test_reviews_a_company_pr_with_inline_comments(harness):
    """The owner asks to review a PR by its repository's short name; the worker drafts a review
    with comments on lines; the owner edits one, drops one and sends."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Review tscp-targeting PR #1529")
    repo = "linkedin-multiproduct/tscp-targeting"
    harness.gh_replies[f"api repos/{repo}/pulls/1529/files"] = json.dumps(FILES)
    harness.gh_replies[f"api repos/{repo}/pulls/1529/reviews"] = json.dumps(
        {"id": 7, "html_url": "https://github.com/x/pull/1529#pullrequestreview-7"}
    )
    harness.gh_replies[f"api repos/{repo}/pulls/1529"] = json.dumps(PULL)

    async def draft(**extra):
        return await call_tool(
            runtime, "draft_github_review", worker, repo=repo, number=1529, body="Summary", **extra
        )

    misplaced = await draft(
        comments=[
            {"path": "model/train.py", "line": 20, "body": "x"},
            {"path": "README.md", "line": 1, "body": "y"},
            {"path": "model/train.py", "line": 11, "side": "LEFT", "body": "z"},
            {"path": "data/big.parquet", "line": 1, "body": "w"},
        ]
    )
    assert misplaced.result_type == "failure"
    assert "model/train.py:20 (RIGHT): not in the diff" in misplaced.text_result_for_llm
    assert "README.md is not changed" in misplaced.text_result_for_llm
    assert "train.py:11 (LEFT)" not in misplaced.text_result_for_llm, "a removed line is fine"
    assert "data/big.parquet:1 (RIGHT): not in the diff" in misplaced.text_result_for_llm
    assert not harness.store.drafts()

    made = await draft(
        comments=[
            {"path": "model/train.py", "line": 11, "body": "Why 3?"},
            {"path": "model/train.py", "start_line": 10, "line": 12, "body": "Add a test"},
            {"path": "data/huge.sql", "line": 1, "body": "Is this needed?"},
        ]
    )
    assert made.result_type == "success", made.text_result_for_llm
    [card] = harness.store.drafts()
    assert card["target"]["via"] == "task" and len(card["target"]["comments"]) == 3
    assert "model/train.py:11: Why 3?" in harness.kinds(worker)[-1][1]

    await runtime.drafts.resolve(
        card["id"],
        DraftDecision(action="send", content={"comment:1": "Please add a test", "comment:2": ""}),
    )
    arguments, payload = harness.gh_calls[-1]
    assert arguments[1] == f"repos/{repo}/pulls/1529/reviews"
    assert payload["commit_id"] == "c0ffee" and payload["body"] == "Summary"
    assert payload["comments"] == [
        {"path": "model/train.py", "line": 11, "side": "RIGHT", "body": "Why 3?"},
        {
            "path": "model/train.py",
            "line": 12,
            "side": "RIGHT",
            "body": "Please add a test",
            "start_line": 10,
            "start_side": "RIGHT",
        },
    ]
    told = harness.store.messages(worker)[-1]["text"]
    assert "data/huge.sql:1: left out (not posted)" in told
    assert "model/train.py:12: posted as the owner rewrote it: Please add a test" in told


async def test_drafting_needs_a_pr_the_task_is_about_or_one_the_owner_reviews(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Look into flaky tests in tscp-targeting")
    repo = "linkedin-multiproduct/tscp-targeting"
    other = {**PULL, "requested_reviewers": []}
    harness.gh_replies[f"api repos/{repo}/pulls/1529"] = json.dumps(other)

    async def draft():
        return await call_tool(
            runtime, "draft_github_review", worker, repo=repo, number=1529, body="LGTM"
        )

    refused = await draft()
    assert refused.result_type == "failure" and "task is about" in refused.text_result_for_llm
    harness.gh_replies[f"api repos/{repo}/pulls/1529"] = json.dumps(PULL)
    harness.gh_replies["auth status --json hosts"] = json.dumps(
        {"hosts": {"github.com": [{"login": "evan-corp", "active": False, "state": "success"}]}}
    )
    assert (await draft()).result_type == "success", "GitHub asks the owner to review it"


def test_a_task_names_a_pr_by_its_repository_and_number():
    from chiefofstaff.tools import names_pr

    repo = "linkedin-multiproduct/tscp-targeting"
    assert names_pr("Review tscp-targeting PR #1529", repo, 1529)
    assert names_pr("please look at PR 1529 in tscp-targeting", repo, 1529)
    assert names_pr("https://github.com/linkedin-multiproduct/tscp-targeting/pull/1529", repo, 1529)
    assert not names_pr("Review evil/tscp-targeting#1529", repo, 1529), "another owner's"
    assert not names_pr("The app crashes on login; see issue #42", "someone/app", 42)
    assert not names_pr("Update api client (see #12)", "org/api", 12)
    assert not names_pr("tscp-targeting PR #15290", repo, 1529)
    assert names_pr("Review api PR #12; the change is in src/api/handlers.py", "acme/api", 12)
    assert names_pr("Review web PR #5 (touches packages/web)", "acme/web", 5)


async def test_a_pr_no_signed_in_account_can_see_says_how_to_fix_it(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Review https://github.com/corp/app/pull/3")
    harness.gh_replies["auth status --json hosts"] = json.dumps(
        {"hosts": {"github.com": [{"login": "evanl666", "active": True, "state": "success"}]}}
    )
    harness.gh_replies["api repos/corp/app/pulls/3"] = RuntimeError(
        "GitHub request failed: gh: Not Found (HTTP 404)"
    )
    result = await call_tool(
        runtime, "draft_github_review", worker, repo="corp/app", number=3, body="LGTM"
    )
    assert result.result_type == "failure"
    assert "signed in: evanl666" in result.text_result_for_llm
    assert "gh auth login" in result.text_result_for_llm
    harness.gh_replies["api repos/corp/app --jq"] = "corp/app"
    missing = await call_tool(
        runtime, "draft_github_review", worker, repo="corp/app", number=3, body="LGTM"
    )
    assert "corp/app has no pull request #3" in missing.text_result_for_llm, "not an account issue"


async def test_a_worker_whose_answer_is_only_in_its_chat_reports_it(harness):
    """The owner sees reports: its final answer becomes one, as written, without a turn."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Analyze sales")
    session = harness.session(worker)
    await settle(runtime)
    answer = "Revenue per region: north 27,760.50, south 25,846.00, west 24,635.00. April leads."
    session.emit("assistant.message", content="Checking the outliers.", tool_requests=[{}])
    session.emit("assistant.message", content=answer)
    sent = len(session.sent)
    session.emit("session.idle")
    await settle(runtime)
    assert len(session.sent) == sent, "no extra turn"
    reports = [text for kind, text in harness.kinds(worker) if kind == "report"]
    assert reports == [answer]
    assert harness.store.one("SELECT status FROM tasks WHERE session_id=?", (worker,))[
        "status"
    ] in {"review", "done"}


async def test_a_worker_that_stops_without_reporting_is_asked_once(harness):
    """It stopped without an answer: it is asked once to report."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Analyze sales")
    session = harness.session(worker)
    await settle(runtime)
    session.emit("assistant.message", content="Looked at it.")
    session.emit("session.idle")
    await settle(runtime)
    assert "report-missing" in session.sent[-1]
    notices = [text for kind, text in harness.kinds(worker) if kind == "notice"]
    assert "It stopped without a report: asked for one." in notices
    sent = len(session.sent)
    session.emit("session.idle")  # It stops again without one: the Watchdog takes over.
    await settle(runtime)
    assert len(session.sent) == sent

    await call_tool(runtime, "report", worker, status="done", summary="North leads.")
    await runtime.owner_send(worker, "Which month was best?")
    await settle(runtime)
    session.emit("assistant.message", content="April.")
    session.emit("session.idle")
    await settle(runtime)
    assert "report-missing" not in session.sent[-1], "the owner read its answer in its chat"


async def test_the_router_hands_on_the_owners_words_without_copying_them(harness):
    runtime = await harness.connect()
    text = "In the api repo, find why exports time out for accounts with 10k+ rows; keep CSV."
    first = await runtime.owner_send(harness.router_id, text)
    await runtime.owner_send(harness.router_id, "Also: what is the export limit?")  # Right after.
    ref = first["meta"]["ref"]
    assert f'ref="{ref}"' in harness.session(harness.router_id).raw_sent[0]
    worker = await spawn(
        runtime,
        harness.router_id,
        "Export timeouts",
        instructions=f"{{owner:{ref}}}\n\nProject: api",
    )
    task = harness.store.one("SELECT goal FROM tasks WHERE session_id=?", (worker,))
    assert task["goal"] == f"{text}\n\nProject: api", "that message, not the latest"
    assert text in harness.session(worker).sent[0]
    wrong = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="X", instructions="{owner}", why_new="t"
    )
    assert wrong.result_type == "success", "{owner} alone is text (GitHub's /repos/{owner}/...)"
    unknown = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="Y", instructions="{owner:o000000}",
        why_new="t",
    )  # fmt: skip
    assert unknown.result_type == "failure" and "ref attribute" in unknown.text_result_for_llm


async def test_no_report_request_to_a_worker_being_closed_or_reattached(harness):
    runtime = await harness.connect()
    for flag in ("close_after_turn", "after_refresh"):
        worker = await spawn(runtime, harness.router_id, f"Work {flag}")
        session = harness.session(worker)
        await settle(runtime)
        setattr(
            runtime.states[worker],
            flag,
            "Go on with the task." if flag == "after_refresh" else True,
        )
        if flag == "after_refresh":
            runtime.states[worker].stale = True
        session.emit("session.idle")
        await settle(runtime)
        assert not any("report-missing" in prompt for prompt in session.sent), flag


async def test_a_worker_that_stopped_to_wait_is_asked_not_filed_as_done(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Write the export docs")
    session = harness.session(worker)
    await settle(runtime)
    session.emit(
        "assistant.message",
        content="I've asked the Export schema worker which columns the export has; "
        "I'll write the docs once it answers.",
    )
    session.emit("session.idle")
    await settle(runtime)
    assert not [text for kind, text in harness.kinds(worker) if kind == "report"]
    assert "report-missing" in session.sent[-1], "it says itself whether it is waiting"


async def test_a_one_line_report_gets_the_answer_its_worker_wrote(harness):
    """“Analysis complete” as the report, the numbers only in its chat: the owner's card and the
    Router chat get the answer."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Analyze sales")
    session = harness.session(worker)
    await settle(runtime)
    await call_tool(runtime, "report", worker, status="done", summary="Analysis complete.")
    answer = "Revenue per region: north 27,760.50; south 25,846.00; west 24,635.00. " * 4
    session.emit("assistant.message", content=answer)
    session.emit("session.idle")
    await settle(runtime)
    task = harness.store.one("SELECT result FROM tasks WHERE session_id=?", (worker,))
    assert answer.strip() in task["result"]
    [report] = [text for kind, text in harness.kinds(worker) if kind == "report"]
    assert report.startswith("Analysis complete.") and answer.strip() in report
    inbound = harness.kinds(harness.router_id)
    assert any(kind == "inbound" and answer.strip() in text for kind, text in inbound)
