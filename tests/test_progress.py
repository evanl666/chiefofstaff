"""What the owner and the Router see of work in progress, and how the owner reaches one worker;
plus the failures a real setup throws at sessions (plugin hooks, efforts, late reports)."""

import asyncio
import json
import subprocess
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeSession, Harness, call_tool, env, settle, task_of

from chiefofstaff.models import DraftDecision, Settings
from chiefofstaff.sources.github import marked


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
    steps["c1"] = (time.time() - 600, *steps["c1"][1:])  # Ten minutes into that step.
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
        earlier="keep",
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
        'sessionStart hook from "context-repo@team-plugins" failed in the session sandbox: '
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
    other = await spawn(runtime, harness.router_id, "Another task")
    harness.session(other).emit("session.error", error_type="hook", message=message)
    await settle(runtime)
    assert not [t for _, t in harness.kinds(other) if "plugin hook failed" in t], "once a day"
    session.emit("session.error", error_type="model", message="invalid tool schema")
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


async def test_the_router_raises_a_workers_effort_but_never_lowers_the_owners(tmp_path):
    harness = Harness(tmp_path / "data", Settings(role_efforts={"worker": "high"}))
    runtime = await harness.connect()
    simple = await spawn(runtime, harness.router_id, "Sum a column", effort="medium")
    assert harness.session(simple).options["reasoning_effort"] == "high"
    hard = await spawn(runtime, harness.router_id, "Debug the race", effort="xhigh")
    assert harness.session(hard).options["reasoning_effort"] == "xhigh"
    harness.store.save_settings(Settings())  # No effort chosen: the Router's applies.
    chore = await spawn(runtime, harness.router_id, "Rename a file", effort="low")
    assert harness.session(chore).options["reasoning_effort"] == "low"


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
    await runtime.task_actions.follow_up(runtime.tasks.current(worker)["id"], "again, all of it")
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
    assert payload == {"body": marked("Fixed in f61047fa: opted-out titles are removed first.")}
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
    runtime.states[worker].steps["c1"] = (
        time.time() - 400,
        "bash trino -f big.sql",
        "bash trino -f big.sql",
    )
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
    harness.own_pull("acme/app", 7)
    await call_tool(
        runtime,
        "report",
        worker,
        status="done",
        summary="Done: opened https://github.com/acme/app/pull/7",
    )
    await settle(runtime)
    task = runtime.tasks.current(worker)
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Logout too"
    )
    await call_tool(runtime, "report", worker, status="done", summary="Logout too.")
    assert runtime.tasks.get(task["id"])["status"] == "review"
    runtime.unfollow("github:acme/app#7", finished=True, merged=True)
    assert runtime.tasks.get(task["id"])["status"] == "done"


async def test_a_merge_settles_only_the_task_that_opened_the_pr(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Survey")
    survey = runtime.tasks.current(worker)
    await call_tool(
        runtime, "report", worker, status="done", task_id=survey["id"],
        summary="Related: https://github.com/acme/app/pull/9 is how others did it.",
    )  # fmt: skip
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Fix it",
        task="new", title="Fix",
    )  # fmt: skip
    fix = runtime.tasks.current(worker)
    session = harness.session(worker)
    session.emit("tool.execution_start", tool_name="bash", tool_call_id="pr",
                 arguments={"command": "gh pr create --fill"})  # fmt: skip
    session.emit("tool.execution_complete", tool_call_id="pr", success=True,
                 result=SimpleNamespace(content="https://github.com/acme/app/pull/9\n"
                                        "<exited with exit code 0>"))  # fmt: skip
    await settle(runtime)
    await call_tool(
        runtime, "report", worker, status="done", task_id=fix["id"],
        summary="Fixed in https://github.com/acme/app/pull/9",
    )  # fmt: skip
    runtime.task_actions.pr_merged(worker, "github:acme/app#9")
    assert runtime.tasks.get(fix["id"])["status"] == "done"
    assert runtime.tasks.get(survey["id"])["status"] == "review", "it only mentioned it"


async def test_a_task_with_two_prs_is_done_when_the_last_is_merged(harness):
    """Bug bash: a task that opened two PRs was done once the first was merged."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Split the change")
    task = runtime.tasks.current(worker)
    session = harness.session(worker)
    for number in (9, 10):
        session.emit("tool.execution_start", tool_name="bash", tool_call_id=f"pr{number}",
                     arguments={"command": "gh pr create --fill"})  # fmt: skip
        session.emit("tool.execution_complete", tool_call_id=f"pr{number}", success=True,
                     result=SimpleNamespace(content=f"https://github.com/acme/app/pull/{number}\n"
                                            "<exited with exit code 0>"))  # fmt: skip
    await settle(runtime)
    await call_tool(runtime, "report", worker, status="done", summary=(
        "Opened https://github.com/acme/app/pull/9 and https://github.com/acme/app/pull/10"
    ))  # fmt: skip
    await settle(runtime)

    def prs(state10):
        return json.dumps({"prs": [
            {"label": "acme/app#9", "own": True, "state": "MERGED"},
            {"label": "acme/app#10", "own": True, "state": state10},
        ]})  # fmt: skip

    harness.store.execute("UPDATE runs SET evidence=? WHERE task_id=?", (prs("OPEN"), task["id"]))
    runtime.task_actions.pr_merged(worker, "github:acme/app#9")
    assert runtime.tasks.get(task["id"])["status"] == "review", "#10 is still open"
    harness.store.execute("UPDATE runs SET evidence=? WHERE task_id=?", (prs("MERGED"), task["id"]))
    runtime.task_actions.pr_merged(worker, "github:acme/app#10")
    assert runtime.tasks.get(task["id"])["status"] == "done"


async def test_a_task_whose_two_prs_merged_since_the_last_look_is_done(harness, monkeypatch):
    """Code review: both PRs merged within one refresh: each looked at the other as still
    open (the old state), and the task stayed in review."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Split the change")
    task = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="done", summary=(
        "Opened https://github.com/acme/app/pull/9 and https://github.com/acme/app/pull/10"
    ))  # fmt: skip
    old = [{"label": f"acme/app#{n}", "own": True, "state": "OPEN", "url": "u"} for n in (9, 10)]
    harness.store.execute(
        "UPDATE runs SET evidence=? WHERE task_id=?", (json.dumps({"prs": old}), task["id"])
    )

    async def merged(_task):
        return [{**pr, "state": "MERGED"} for pr in old]

    monkeypatch.setattr(runtime.task_actions, "read_prs", merged)
    await runtime.task_actions.refresh_prs(task["id"])
    assert runtime.tasks.get(task["id"])["status"] == "done"


async def test_a_pr_github_cannot_read_is_followed_only_when_it_opened_it(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Look around")
    harness.gh_replies["api repos/acme/app/pulls/5"] = RuntimeError("HTTP 502")
    await call_tool(
        runtime, "report", worker, status="progress",
        summary="See https://github.com/acme/app/pull/5 for context.",
    )  # fmt: skip
    await settle(runtime)
    assert "github:acme/app#5" not in harness.store.follows(worker)


async def test_another_tasks_pr_on_the_owners_account_is_not_this_ones(harness):
    runtime = await harness.connect()
    maker = await spawn(runtime, harness.router_id, "Make it")
    harness.own_pull("acme/app", 11)
    await call_tool(
        runtime,
        "report",
        maker,
        status="progress",
        summary="Opened https://github.com/acme/app/pull/11",
    )
    reader = await spawn(runtime, harness.router_id, "Read up", why_new="separate")
    await call_tool(
        runtime,
        "report",
        reader,
        status="done",
        summary="Builds on https://github.com/acme/app/pull/11",
    )
    await settle(runtime)
    assert "github:acme/app#11" in harness.store.follows(maker)
    assert "github:acme/app#11" not in harness.store.follows(reader)
    task = runtime.tasks.current(reader)
    own, about = await runtime.task_actions.pr_roles(
        task, [{"label": "acme/app#11", "author": "me", "created": "2999-01-01T00:00:00Z"}]
    )
    assert not own and not about


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
    worker = await spawn(runtime, harness.router_id, "Review checkout-service PR #1529")
    repo = "acme/checkout-service"
    harness.gh_replies[f"api repos/{repo}/pulls/1529/files"] = json.dumps(FILES)
    harness.gh_replies[f"api repos/{repo}/pulls/1529/reviews"] = json.dumps(
        {"id": 7, "html_url": "https://github.com/x/pull/1529#pullrequestreview-7"}
    )
    harness.gh_replies[f"api repos/{repo}/pulls/1529/reviews/7/comments"] = "[]"
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
    [(arguments, payload)] = [call for call in harness.gh_calls if call[1]]
    assert arguments[1] == f"repos/{repo}/pulls/1529/reviews"
    assert payload["commit_id"] == "c0ffee" and payload["body"] == marked("Summary")
    assert payload["comments"] == [
        {"path": "model/train.py", "line": 11, "side": "RIGHT", "body": marked("Why 3?")},
        {
            "path": "model/train.py",
            "line": 12,
            "side": "RIGHT",
            "body": marked("Please add a test"),
            "start_line": 10,
            "start_side": "RIGHT",
        },
    ]
    told = harness.store.messages(worker)[-1]["text"]
    assert "data/huge.sql:1: left out (not posted)" in told
    assert "model/train.py:12: posted as the owner rewrote it: Please add a test" in told


async def test_drafting_needs_a_pr_the_task_is_about_or_one_the_owner_reviews(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Look into flaky tests in checkout-service")
    repo = "acme/checkout-service"
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


NAMED = "acme/checkout-service"


@pytest.mark.parametrize(
    "text, named",
    [
        ("Review checkout-service PR #1529", True),
        ("帮我review一下 checkout-service 的 PR #1529", True),
        ("review一下checkout-service的PR 1529", True),
        ("checkout-service里面的 #1529 帮我看看", True),
        ("checkout-service#1529", True),
        ("checkout-service/pull/1529", True),
        ("https://github.com/acme/checkout-service/pull/1529", True),
        ("acme/checkout-service#1529", True),
        ("PR #1529 (checkout-service)", True),
        ("please look at PR 1529 in checkout-service", True),
        ("Review PRs checkout-service #1528 and #1529", True),
        ("checkout-service PRs #1528, #1529", True),
        ("review checkout-service pull request 1529", True),
        ("Checkout-Service PR #1529", True),
        ("checkout-service 1529", True),
        ("checkout-service-v2 PR #1529", False),
        ("old-checkout-service PR #1529", False),
        ("checkout-service.old PR #1529", False),
        ("checkout-service PR #15290", False),
        ("checkout-service issue #1529 is related; fix it", False),
        ("checkout-service: see JIRA-1529", False),
        ("evil/checkout-service PR #1529", False),
        ("PR #1529 in evil/checkout-service", False),
        ("In checkout-service, the last 1529 rows are wrong", False),
        ("checkout-service has a bug #1529", False),
        ("checkout-service. Separately, PR #1529 in other-repo", False),
    ],
)
def test_a_task_names_a_pr_by_its_repository_and_number(text, named):
    from chiefofstaff.tools import names_pr

    assert names_pr(text, NAMED, 1529) is named


def test_a_repository_name_in_a_path_or_near_an_issue_is_not_its_pr():
    from chiefofstaff.tools import names_pr

    assert names_pr("Review api PR #12; the change is in src/api/handlers.py", "acme/api", 12)
    assert names_pr("Review web PR #5 (touches packages/web)", "acme/web", 5)
    assert not names_pr("The app crashes on login; see issue #42", "someone/app", 42)
    assert not names_pr("Update api client (see #12)", "org/api", 12)
    assert not names_pr("Review github:acme/web#5 and acme/api#6", "acme/web", 6)


@pytest.mark.parametrize(
    "text, number, named",
    [
        ("Review acme/web#1529 and acme/api#1600", 1529, False),
        ("Review https://github.com/acme/web/pull/1529 and acme/api#1600", 1529, False),
        ("Review web #1529 and api #1600", 1529, False),
        ("web#1529, api#1600", 1529, False),
        ("web 的 #1529 和 api 的 #1600", 1529, False),
        ("https://ci.example.com/builds/1529 for api", 1529, False),
        ("Review github:acme/web#5 and acme/api#6", 5, False),
        ("acme/api#1600 and #1529 in acme/web", 1529, False),
        ("acme/api#1600 and #1529 in acme/api", 1529, True),
        ("web#1529 and api#1600", 1600, True),
        ("web 的 #1529 和 api 的 #1600", 1600, True),
    ],
)
def test_a_pr_number_belongs_to_the_repository_it_is_written_with(text, number, named):
    """A task listing PRs of several repositories names each with its own number only."""
    from chiefofstaff.tools import names_pr

    assert names_pr(text, "acme/api", number) is named


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
        if flag == "close_after_turn":
            # Closed by itself only once nothing of it is open (its PR merged, its task done).
            runtime.tasks.move(runtime.tasks.current(worker)["id"], "done")
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


async def test_the_answer_goes_to_the_report_of_the_task_it_was_on(harness):
    """It reported one task done while it has another one now: that report gets the answer."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Analyze sales")
    first = task_of(harness, worker)
    session = harness.session(worker)
    await call_tool(
        runtime,
        "message_session",
        harness.router_id,
        session_id=worker,
        text="Then chart it",
        task="new",
        title="Chart",
    )
    await call_tool(
        runtime, "report", worker, status="done", summary="Analysis complete.", task_id=first
    )
    answer = "Revenue per region: north 27,760.50; south 25,846.00; west 24,635.00. " * 4
    session.emit("assistant.message", content=answer)
    session.emit("session.idle")
    await settle(runtime)
    done = harness.store.one("SELECT result FROM tasks WHERE id=?", (first,))
    assert answer.strip() in done["result"]


async def test_review_cards_when_the_owner_drops_everything_or_the_pr_moves(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Review checkout-service PR #1529")
    repo = "acme/checkout-service"
    harness.gh_replies[f"api repos/{repo}/pulls/1529/files"] = json.dumps(FILES)
    harness.gh_replies[f"api repos/{repo}/pulls/1529"] = json.dumps(PULL)
    harness.gh_replies[f"api repos/{repo}/pulls/1529/reviews"] = json.dumps(
        {"id": 8, "html_url": "https://github.com/x/pull/1529#pullrequestreview-8"}
    )

    async def draft():
        await call_tool(
            runtime, "draft_github_review", worker, repo=repo, number=1529, body="Looks fine",
            comments=[{"path": "model/train.py", "line": 11, "body": "Why 3?"}],
        )  # fmt: skip
        return harness.store.drafts()[-1]

    card = await draft()
    await runtime.drafts.resolve(
        card["id"], DraftDecision(action="send", content={"comment:0": ""})
    )
    arguments, payload = harness.gh_calls[-1]
    assert arguments[1].endswith("/reviews") and "comments" not in payload, "all comments dropped"

    card = await draft()
    harness.gh_replies[f"api repos/{repo}/pulls/1529"] = json.dumps(
        {**PULL, "head": {"sha": "newer", "ref": "feature"}}
    )
    posts = len([c for c in harness.gh_calls if "--method=POST" in c[0]])
    with pytest.raises(RuntimeError, match="PR changed"):
        await runtime.drafts.resolve(
            card["id"], DraftDecision(action="send", content={"comment:0": "Edited"})
        )
    assert len([c for c in harness.gh_calls if "--method=POST" in c[0]]) == posts, "not posted"
    failed = harness.store.draft(card["id"])
    assert failed["status"] == "failed"
    assert failed["target"]["comments"][0]["body"] == "Edited", "the owner's edit is kept"
    told = harness.store.messages(worker)[-1]["text"]
    assert "PR changed" in told, "the worker hears it, to draft again on the new head"

    harness.gh_replies[f"api repos/{repo}/pulls/1529"] = json.dumps(PULL)
    card = await draft()
    await runtime.drafts.resolve(card["id"], DraftDecision(action="reject", reason="Not now"))
    assert harness.store.draft(card["id"])["status"] == "rejected"
    assert len([c for c in harness.gh_calls if "--method=POST" in c[0]]) == posts


async def test_stopping_a_worker_files_nothing_and_asks_nothing(harness):
    """Copilot ends the turn (idle) while the stop is still in flight."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Long analysis")
    session = harness.session(worker)
    await settle(runtime)
    session.emit("assistant.turn_start")
    session.emit(
        "assistant.message", content="Halfway: north 27,760.50 so far, still summing south."
    )

    async def abort():
        session.emit("session.idle")
        await asyncio.sleep(0.05)  # The idle event is handled before the abort returns.

    session.abort = abort
    sent = len(session.sent)
    await runtime.workers.interrupt(worker)
    await settle(runtime)
    assert not [text for kind, text in harness.kinds(worker) if kind == "report"]
    assert len(session.sent) == sent, "no report request to a stopped worker"
    task = harness.store.one("SELECT status FROM tasks WHERE session_id=?", (worker,))
    assert task["status"] == "waiting"


async def test_what_a_worker_said_last_decides_how_its_turn_end_is_read(harness):
    runtime = await harness.connect()

    async def ends_with(text, before=None):
        worker = await spawn(runtime, harness.router_id, f"Task {len(harness.store.drafts())}")
        session = harness.session(worker)
        await settle(runtime)
        if before:
            await before(worker)
        session.emit("assistant.message", content=text)
        session.emit("session.idle")
        await settle(runtime)
        task = harness.store.one("SELECT status FROM tasks WHERE session_id=?", (worker,))
        return worker, session, task["status"]

    question = "I found two export formats in the repo, CSV and Parquet. Which one should I change?"
    _, _, status = await ends_with(question)
    assert status == "waiting", "a question is filed as one"
    router = [text for kind, text in harness.kinds(harness.router_id) if kind == "inbound"]
    assert question in router[-1]

    async def progress(worker):
        await call_tool(runtime, "report", worker, status="progress", summary="Opened PR #12")

    _, session, status = await ends_with("CI runs for about ten minutes on PR #12 now.", progress)
    assert status == "running" and not any("report-missing" in p for p in session.sent), (
        "it said where it is: it may wait"
    )

    _worker, session, status = await ends_with(
        "The build is still compiling; I'll report once it finishes and the tests ran."
    )
    assert status == "running" and "report-missing" in session.sent[-1], "waiting: asked"
    sent = len(session.sent)
    session.emit("session.idle")
    await settle(runtime)
    assert len(session.sent) == sent, "asked once, then the Watchdog follows up"


async def test_the_owners_words_reach_an_existing_worker_and_combine(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Exports")
    first = await runtime.owner_send(harness.router_id, "Keep the CSV header as it is.")
    second = await runtime.owner_send(harness.router_id, "And add a row count at the end.")
    refs = first["meta"]["ref"], second["meta"]["ref"]
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker,
        text=f"Two notes from the owner:\n{{owner:{refs[0]}}}\n{{owner: {refs[1]}}}",
    )  # fmt: skip
    await settle(runtime)
    delivered = harness.session(worker).sent[-1]
    assert "Keep the CSV header as it is.\nAnd add a row count at the end." in delivered
    assert "{owner" not in delivered


async def test_a_decision_recorded_again_in_the_same_words_changes_nothing(harness):
    runtime = await harness.connect()
    first = await call_tool(
        runtime, "record_decision", harness.router_id, decision="API uses snake_case JSON keys"
    )
    decision = first.text_result_for_llm.split()[1].rstrip(",.")
    worker = await spawn(runtime, harness.router_id, "Write api.md", decisions=[decision])
    await settle(runtime)
    sent = len(harness.session(worker).sent)
    task = harness.store.one("SELECT id FROM tasks WHERE session_id=?", (worker,))["id"]
    again = await call_tool(
        runtime, "record_decision", harness.router_id,
        decision="API uses snake_case JSON keys.", tasks=[task], replaces=[decision],
    )  # fmt: skip
    assert "nothing changed" in again.text_result_for_llm
    await settle(runtime)
    assert len(harness.session(worker).sent) == sent, "the worker is not told to redo anything"
    assert not [d for d in harness.store.drafts() if d["kind"] == "change"], "no change card"


@pytest.mark.parametrize(
    "goal, summary, missing",
    [
        (
            "用 sales.csv 做个分析, 一步一步讲清楚, 最后用一个 markdown 表格总结",
            "分析完成。",
            "a table",
        ),
        ("Summarize the results in a table", "North leads.", "a table"),
        ("Explain why the export times out", "The cursor resets.", "an explanation"),
        ("Review PR #12 and walk me through the risks", "LGTM", "an explanation"),
        ("Add the orders table", "Added it.", ""),
        ("Drop the old sessions table", "Dropped.", ""),
        ("Put them in a markdown table", "| a | b |\n|---|---|", ""),
        ("What is 17x23?", "391", ""),
        (
            "List every row with its revenue as one markdown table",
            "All 540 rows are in rows.md; it is too large to paste in full here.\n| a |\n|---|",
            "the whole answer",
        ),
        ("把所有行列成表格", "表格太长, 只列前 10 行:\n| a |\n|---|", "the whole answer"),
        ("Tell me a bit about rounding", "Kept it short for brevity: half up.", "the whole answer"),
        (
            "list every row of data/sales.csv with its revenue (units * unit_price) as one "
            "markdown table, sorted by revenue descending. All rows, no summary.",
            "The full 540-row table (sorted by revenue descending, ties in file order) is saved "
            "at /tmp/w/sales_by_revenue.md. Top rows:\n| date | revenue |\n|---|---|",
            "the whole answer",
        ),
        (
            "把所有行列成表格",
            "完整表格已保存到 /tmp/rows.md, 前 10 行:\n| a |\n|---|",
            "the whole answer",
        ),
        (
            "Write every row as a markdown table to rows.md",
            "The full table is saved at rows.md.\n|",
            "",
        ),
        ("List all open PRs", "#12 and #14 are open. I saved the full list notes earlier.", ""),
        (
            "list every row of data/sales.csv with its revenue as one markdown table",
            "The full table (540 rows) is in the file below. Pasting it all inline would exceed "
            "the report size.\n\nFile: /tmp/w/t.md\n| a |\n|---|",
            "the whole answer",
        ),
        ("Profile the import job", "Peak memory would exceed 2 GB with 10x data.", ""),
        ("Store the results in a table in Postgres", "Done.", ""),
        ("Fix the EXPLAIN ANALYZE output parsing", "Fixed.", ""),
        ("Check how your service handles retries", "It retries 3 times.", ""),
        ("Back off when calls exceed the rate limit", "Done: it backs off past the limit.", ""),
        ("详细检查一下登录流程", "没有问题。", ""),
    ],
)
def test_what_a_one_line_report_cannot_answer(goal, summary, missing):
    from chiefofstaff.tools import short_of

    assert short_of(goal, summary).startswith(missing) and bool(short_of(goal, summary)) == bool(
        missing
    )


async def test_a_one_line_answer_to_an_explanation_is_sent_back_once(harness):
    runtime = await harness.connect()
    worker = await spawn(
        runtime, harness.router_id, "Sales analysis",
        instructions="Analyse sales.csv, explain step by step, end with a markdown table",
    )  # fmt: skip
    first = await call_tool(runtime, "report", worker, status="done", summary="Analysis done.")
    assert first.result_type == "failure" and "a table" in first.text_result_for_llm
    task = harness.store.one("SELECT status FROM tasks WHERE session_id=?", (worker,))
    assert task["status"] == "running", "nothing filed yet"
    again = await call_tool(runtime, "report", worker, status="done", summary="Analysis done.")
    assert again.result_type == "success", "the second time it is taken as the whole answer"


async def test_chief_of_staffs_tools_do_not_hide_a_workers_mcp_tools_sooner(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Work")
    options = harness.session(worker).options
    assert options["tool_search"]["defer_threshold"] == 30 + len(options["tools"])
    assert "tool_search" not in harness.session(harness.router_id).options


async def test_a_background_call_on_another_model_is_not_copilots_default(harness):
    """Copilot names sessions and summarizes with a cheaper model that takes no effort: taken
    for the default model, it made Chief of Staff drop the owner's worker effort."""
    harness.store.save_settings(Settings(role_efforts={"worker": "high"}))
    runtime = await harness.connect()
    router = harness.session(harness.router_id)
    router.emit(
        "assistant.usage", model="smart-model", cost=1, interaction_type="conversation-agent"
    )
    router.emit("assistant.usage", model="fast-model", interaction_type="conversation-title")
    router.emit("assistant.usage", model="fast-model", initiator="sub-agent")
    router.emit("assistant.usage", model="fast-model", is_auto=True)
    await settle(runtime)
    assert harness.store.get("copilot_default_model") == "smart-model"
    worker = await spawn(runtime, harness.router_id, "Analyse")
    assert harness.session(worker).options["reasoning_effort"] == "high"


async def test_the_router_hears_everything_the_owner_tells_a_worker_directly(harness):
    """A revision on a task card, a retry note or a message in a worker's own timeline: the
    Router must know, or it takes the worker's result for drift and has it undone."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Add shout()")
    await call_tool(runtime, "report", worker, status="done", summary="Added shout().")
    task = harness.store.one("SELECT id FROM tasks WHERE session_id=?", (worker,))["id"]
    await runtime.task_actions.follow_up(task, "Also add an exclamation mark.")
    await runtime.owner_send(worker, "Keep the docstring short.")
    notes = [
        m for m in harness.store.messages(harness.router_id)
        if m["meta"].get("to") == worker
    ]  # fmt: skip
    assert [m["text"] for m in notes] == [
        "Also add an exclamation mark.",
        "Keep the docstring short.",
    ]
    assert all(m["meta"]["deferred"] for m in notes), "with the Router's next message, no turn"
    await runtime.owner_send(harness.router_id, "What is open?")
    sent = harness.session(harness.router_id).sent[-1]
    assert "The owner told this worker directly: Also add an exclamation mark." in sent


def test_a_credit_limit_under_copilots_minimum_is_raised_to_it():
    """Copilot starts no session with a limit under 30 credits: every worker would fail."""
    assert Settings(worker_credit_limit=5).worker_credit_limit == 30
    assert Settings(worker_credit_limit=0).worker_credit_limit == 0
    assert Settings(worker_credit_limit=45).worker_credit_limit == 45


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        ("Which workers are open right now? One line.", "English"),
        ("帮我review一下 cos-account-routing-test 的 PR #1", "Chinese"),
        ("看一下 `uv run pytest -q` 为什么挂了", "Chinese"),
        ("Could you check https://github.com/a/b/pull/1 please", "English"),
        ("progress?", ""),
        ("LGTM", ""),
        ("修一下src/chiefofstaff/delivery.py里的bug", "Chinese"),
        ("帮我看下这个PR\uff1ahttps://github.com/org/repo/pull/12", "Chinese"),
        ("Please reply to 张伟 that we ship tomorrow", "English"),
        ("Was ist der Status?", ""),
        ("No me funciona el build", ""),
        ("Peux-tu regarder ce bug on a un problème", ""),
    ],
)
def test_the_language_of_a_message_when_it_is_clear(text, spoken):
    from chiefofstaff.envelope import language

    assert language(text) == spoken


async def test_answers_follow_the_language_the_owner_just_wrote_in(harness):
    runtime = await harness.connect()
    router = harness.session(harness.router_id)
    await runtime.owner_send(harness.router_id, "汇报先说结论, 再说细节.")
    assert 'reply_in="Chinese"' in router.sent[-1]
    runtime.add_message(harness.router_id, "assistant", "收到: 以后先说结论.")
    router.emit("assistant.turn_start")
    await runtime.owner_send(harness.router_id, "Which workers are open right now?")
    assert 'reply_in="English"' in router.sent[-1], "not the language of the conversation so far"
    assert "answer in English, not Chinese" in router.sent[-1], "said outright when it switches"
    assert router.modes[-1] == "enqueue", "its own turn, not the Chinese one running"
    runtime.add_message(harness.router_id, "assistant", "None are open.")
    await runtime.owner_send(harness.router_id, "And how many are queued?")
    assert router.modes[-1] == "immediate", "the same language: into the running turn"
    router.emit("session.idle")
    worker = await spawn(runtime, harness.router_id, "Count the files")
    assert 'reply_in="English"' in harness.session(worker).sent[-1], "its report is read as is"
    chinese_task = await spawn(runtime, harness.router_id, "数一下有几个文件")
    assert "Write your report in English" in harness.session(chinese_task).sent[-1]
    await runtime.owner_send(harness.router_id, "progress?")
    assert "reply_in" not in router.sent[-1], "no signal: the conversation's language"
    router.emit("session.idle")
    await runtime.owner_send(harness.router_id, "你能帮我做哪些事?")
    router.emit("assistant.turn_start")
    await runtime.owner_send(harness.router_id, "Which tasks are running right now?")
    assert router.modes[-1] == "enqueue", "it is still on the Chinese one, not yet answered"
    router.emit("session.idle")
    await call_tool(runtime, "report", worker, status="done", summary="3 files.")
    await settle(runtime)
    [report] = [prompt for prompt in router.sent if 'type="report"' in prompt]
    assert 'reply_in="English"' in report


async def test_a_new_task_answered_without_a_report_is_filed_not_left_running(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Analyze sales.csv")
    session = harness.session(worker)
    await call_tool(runtime, "report", worker, status="done", summary="Totals by region: …")
    session.emit("session.idle")
    await settle(runtime)
    assert harness.store.session(worker)["status"] == "done"
    given = await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, task="new",
        title="Monthly growth", text="Now the month-over-month growth, as a table.",
    )  # fmt: skip
    assert given.result_type != "failure", given.text_result_for_llm
    answer = "| month | growth |\n|---|---|\n| Feb | +4% |\n| Mar | +2% |\n" + "Computed so. " * 20
    session.emit("assistant.message", content=answer)
    session.emit("session.idle")
    await settle(runtime)
    task = runtime.tasks.current(worker)
    assert task["title"] == "Monthly growth"
    assert task["status"] == "review" and "| Feb | +4% |" in task["result"], task["status"]

    # Asked about finished work (no new task), it goes back to done after answering.
    await runtime.owner_send(worker, "Which month was best?")
    session.emit("assistant.message", content="February.")
    session.emit("session.idle")
    await settle(runtime)
    assert harness.store.session(worker)["status"] in {"done", "review"}


async def test_a_task_reopened_after_its_message_is_still_reported(harness):
    """A changed decision reaches a finished worker first and reopens its task after: the
    turn's answer is still filed, and the task never stays running on a done worker."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Login limits")
    session = harness.session(worker)
    await call_tool(runtime, "report", worker, status="done", summary="Limited per IP.")
    session.emit("session.idle")
    await settle(runtime)
    task = runtime.tasks.current(worker)
    await runtime.delivery.deliver(  # As Decisions.propagate does: tell it, then reopen.
        worker,
        env("router", "decision-changed", "Now per account: adjust it.", task=task["id"]),
        kind="router",
        meta={"task_id": task["id"]},
        steer=True,
    )
    runtime.tasks.move(task["id"], "running", reason="decision changed")
    session.emit("assistant.message", content="Switched the limit to per account. " * 6)
    session.emit("session.idle")
    await settle(runtime)
    assert runtime.tasks.get(task["id"])["status"] == "review"


async def test_a_follow_up_on_a_task_card_sets_the_owners_language(harness):
    runtime = await harness.connect()
    await runtime.owner_send(harness.router_id, "Count the files in the project please")
    worker = await spawn(runtime, harness.router_id, "Count files")
    task = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="done", summary="3 files.")
    await runtime.task_actions.follow_up(task["id"], "再把每个文件的行数也列出来")
    assert 'reply_in="Chinese"' in harness.session(worker).sent[-1]
    assert harness.store.get("owner_language") == "Chinese", "what the Router tells them next"


async def test_answering_a_task_that_needs_you_takes_it_out_of_needs_you(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Pick a port")
    task = runtime.tasks.current(worker)
    await call_tool(
        runtime, "report", worker, status="question", summary="Which port, 8080 or 9090?"
    )
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    assert runtime.tasks.get(task["id"])["status"] == "waiting"
    view = await runtime.task_actions.follow_up(task["id"], "9090")
    assert view["status"] == "running", "the card leaves Needs you at once"
    assert harness.session(worker).sent[-1].endswith("\n9090\n</message>")


async def test_a_report_that_misses_a_message_sent_meanwhile_waits_for_it(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Fix the parser")
    session = harness.session(worker)
    session.emit("user.message", content=session.sent[-1])
    await settle(runtime)
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Also cover tabs"
    )
    early = await call_tool(runtime, "report", worker, status="done", summary="Fixed.")
    assert early.result_type == "failure" and "Also cover tabs" in early.text_result_for_llm
    assert runtime.tasks.current(worker)["status"] == "running", "the Router is not told yet"
    session.emit("user.message", content=session.sent[-1])
    await settle(runtime)
    done = await call_tool(runtime, "report", worker, status="done", summary="Fixed, tabs too.")
    assert done.result_type == "success"


async def test_copilot_failing_on_its_side_is_tried_again_not_left_to_the_owner(harness):
    runtime = await harness.connect()
    router = harness.session(harness.router_id)
    await runtime.owner_send(harness.router_id, "What is open?")
    asked = router.sent[-1]
    router.emit("session.error", message="No response was returned. Send your message again.")
    await settle(runtime)
    await asyncio.sleep(0.05)
    await settle(runtime)
    assert router.sent[-1] == asked and len(router.sent) == 2, "the same turn, sent again"
    assert not [k for k, _ in harness.kinds(harness.router_id) if k == "error"], "no red error"

    worker = await spawn(runtime, harness.router_id, "Fix the build")
    session = harness.session(worker)
    step(session, "bash", {"command": "make"})
    session.emit("session.error", message="Execution failed: 503 Service Unavailable")
    await settle(runtime)
    await asyncio.sleep(0.05)
    await settle(runtime)
    assert "finish what is left of it" in session.sent[-1], "acted already: a note, not a resend"
    assert runtime.tasks.current(worker)["status"] == "running", "its task did not fail"
    for _ in range(2):
        session.emit("session.error", message="Execution failed: 503 Service Unavailable")
        await settle(runtime)
        await asyncio.sleep(0.05)
        await settle(runtime)
    assert runtime.tasks.current(worker)["status"] == "failed", "the third time it does"
    errors = [text for kind, text in harness.kinds(worker) if kind == "error"]
    assert errors and "3 times" in errors[-1]


async def reply_setup(harness, runtime):
    worker = await spawn(
        runtime, harness.router_id, "Address review comments",
        instructions="Answer the comments on https://github.com/acme/model/pull/1043",
    )  # fmt: skip
    harness.gh_replies["api repos/acme/model/pulls/1043"] = json.dumps(
        {"state": "open", "head": {"sha": "f61047fa", "ref": "fix"}, "user": {"login": "x"}}
    )
    harness.gh_replies["api repos/acme/model/pulls/comments/21"] = json.dumps({
        "user": {"login": "ygchen"}, "body": "Opt-out titles are not removed", "path": "seeds.py",
        "line": 117, "html_url": "https://github.com/acme/model/pull/1043#discussion_r21",
        "pull_request_url": "https://api.github.com/repos/acme/model/pulls/1043",
    })  # fmt: skip
    harness.gh_replies["api repos/acme/model/pulls/1043/comments/21/replies"] = json.dumps(
        {"id": 99, "html_url": "https://github.com/acme/model/pull/1043#discussion_r99"}
    )
    return worker


async def test_a_reply_on_a_line_can_resolve_its_conversation(harness):
    runtime = await harness.connect()
    worker = await reply_setup(harness, runtime)
    harness.gh_replies["api graphql -f query=query"] = json.dumps({"data": {"repository": {
        "pullRequest": {"reviewThreads": {"nodes": [
            {"id": "T_other", "isResolved": False, "comments": {"nodes": [{"databaseId": 5}]}},
            {"id": "T_21", "isResolved": False, "comments": {"nodes": [{"databaseId": 21}]}},
        ]}}}}})  # fmt: skip
    harness.gh_replies["api graphql -f query=mutation"] = json.dumps({"data": {}})
    refused = await call_tool(runtime, "draft_github_review", worker, repo="acme/model",
                              number=1043, body="Done.", resolve=True)  # fmt: skip
    assert "Only a conversation on a line" in refused.text_result_for_llm
    for resolve, expected in ((True, " · conversation resolved"), (False, "")):
        await call_tool(runtime, "draft_github_review", worker, repo="acme/model", number=1043,
                        body="Fixed in f61047fa.", in_reply_to=21, resolve=True)  # fmt: skip
        [draft] = harness.store.drafts()
        assert draft["target"]["resolve"] is True, "proposed; the owner decides on the card"
        harness.gh_calls.clear()
        decision = DraftDecision(action="send", content={"resolve": resolve})
        sent = await runtime.drafts.resolve(draft["id"], decision)
        assert (
            sent["outcome"] == "https://github.com/acme/model/pull/1043#discussion_r99" + expected
        )
        mutations = [a for a, _ in harness.gh_calls if a[:2] == ("api", "graphql")
                     and a[3].startswith("query=mutation")]  # fmt: skip
        assert len(mutations) == int(resolve)
        if resolve:
            assert "id=T_21" in mutations[0], "the thread the comment is in"


async def test_an_answer_to_a_conversation_comment_names_its_author(harness):
    runtime = await harness.connect()
    worker = await reply_setup(harness, runtime)
    harness.gh_replies["api repos/acme/model/issues/comments/55"] = json.dumps({
        "user": {"login": "ckrsek"}, "body": "Overall: please document the opt-out filter.",
        "html_url": "https://github.com/acme/model/pull/1043#issuecomment-55",
        "issue_url": "https://api.github.com/repos/acme/model/issues/1043",
    })  # fmt: skip
    harness.gh_replies["api repos/acme/model/issues/comments/77"] = RuntimeError("HTTP 404")
    harness.gh_replies["api repos/acme/model/pulls/1043/reviews/77"] = json.dumps({
        "user": {"login": "maya"}, "body": "Looks good overall; one question on naming.",
        "html_url": "https://github.com/acme/model/pull/1043#pullrequestreview-77",
    })  # fmt: skip
    harness.gh_replies["api repos/acme/model/issues/1043/comments"] = json.dumps(
        {"id": 501, "html_url": "https://github.com/acme/model/pull/1043#issuecomment-501"}
    )
    documented = "Documented it in README.md, see 3b6c1e0."
    await call_tool(runtime, "draft_github_review", worker, repo="acme/model", number=1043,
                    body=documented, reply_to_comment=55)  # fmt: skip
    await call_tool(runtime, "draft_github_review", worker, repo="acme/model", number=1043,
                    body="Thanks @Maya, renamed it.", reply_to_comment=77)  # fmt: skip
    first, second = harness.store.drafts()
    assert first["text"] == "@ckrsek Documented it in README.md, see 3b6c1e0."
    assert first["target"]["comment"]["author"] == "ckrsek"
    assert second["text"] == "Thanks @Maya, renamed it.", "named already: not twice"
    sent = await runtime.drafts.resolve(first["id"], DraftDecision(action="send"))
    assert sent["outcome"] == "https://github.com/acme/model/pull/1043#issuecomment-501"
    arguments, payload = harness.gh_calls[-1]
    assert arguments[1] == "repos/acme/model/issues/1043/comments"
    assert payload == {"body": marked("@ckrsek Documented it in README.md, see 3b6c1e0.")}
    both = await call_tool(runtime, "draft_github_review", worker, repo="acme/model",
                           number=1043, body="x", in_reply_to=21, reply_to_comment=55)  # fmt: skip
    assert "not both" in both.text_result_for_llm


async def test_a_draft_the_owner_posted_themself_is_settled(harness):
    runtime = await harness.connect()
    worker = await reply_setup(harness, runtime)
    harness.gh_replies["auth status --json hosts"] = json.dumps(
        {"hosts": {"github.com": [{"login": "Evan-L", "active": True, "state": "success"}]}}
    )
    await call_tool(runtime, "draft_github_review", worker, repo="acme/model", number=1043,
                    body="Fixed in f61047fa: opted-out titles are removed first, before "
                         "embedding inference.", in_reply_to=21)  # fmt: skip
    [draft] = harness.store.drafts()
    harness.store.execute(
        "UPDATE drafts SET created_at=? WHERE id=?", ("2026-01-01T00:00:00+00:00", draft["id"])
    )
    comments = [
        {"id": 7, "user": {"login": "ygchen"}, "in_reply_to_id": 21, "body": "Fixed in "
         "f61047fa: opted-out titles are removed first, before embedding inference.",
         "html_url": "u7"},
    ]  # fmt: skip
    harness.gh_replies["api repos/acme/model/pulls/1043/comments"] = json.dumps(comments)
    await runtime.drafts.check_posted_by_owner(every=0)
    assert harness.store.draft(draft["id"])["status"] == "pending", "someone else's comment"
    comments.append({"id": 8, "user": {"login": "evan-l"}, "html_url": "u8", "in_reply_to_id": 21,
                     "body":
                     "Fixed in **f61047fa**: opted-out titles are removed first, before the "
                     "embedding inference."})  # fmt: skip
    harness.gh_replies["api repos/acme/model/pulls/1043/comments"] = json.dumps(comments)
    await runtime.drafts.check_posted_by_owner(every=0)
    await settle(runtime)
    settled = harness.store.draft(draft["id"])
    assert settled["status"] == "sent" and settled["outcome"].endswith("u8")
    told = harness.store.messages(worker)[-1]
    assert told["meta"]["deferred"], "with its next message: no turn of its own"
    assert '<message from="system" type="draft-outcome"' in told["meta"]["prompt"]
    assert "Do not ask about sending it again" in told["text"]


async def test_a_sent_draft_settles_the_task_it_was_drafted_for(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Reply to Ann")
    reply = runtime.tasks.current(worker)
    draft = harness.store.create_draft(
        worker, "slack", {"channel_id": "C1", "thread_ts": "", "to": "Ann"}, "On it!"
    )
    assert draft["target"]["task_id"] == reply["id"]
    await call_tool(runtime, "report", worker, status="done", summary="Drafted the reply.")
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Now the docs",
        task="new", title="Docs",
    )  # fmt: skip
    docs = runtime.tasks.current(worker)
    await runtime.drafts.copied(harness.store.draft(draft["id"]), "On it!")
    await settle(runtime)
    assert runtime.tasks.get(reply["id"])["status"] == "done", "sent: the reply task is settled"
    assert runtime.tasks.get(docs["id"])["status"] == "running"
    assert not harness.store.get(f"draft_sent:{docs['id']}")


async def test_news_on_finished_work_stays_in_sight_until_seen(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Filter opt-outs")
    await call_tool(runtime, "report", worker, status="done", summary="Opened acme/app#71.")
    task = runtime.tasks.current(worker)
    runtime.task_actions.accept(task["id"])
    await call_tool(runtime, "report", worker, status="progress",
                    summary="PR 71 is approved and ready to merge.")  # fmt: skip
    view = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    assert (
        view["status"] == "done" and view["news"]["text"] == "PR 71 is approved and ready to merge."
    )
    seen = runtime.task_actions.view(runtime.task_actions.news_seen(task["id"]))
    assert seen["news"] is None
    await call_tool(runtime, "report", worker, status="done", summary="PR 71 is merged.")
    assert runtime.task_actions.view(runtime.tasks.get(task["id"]))["news"]["text"] == (
        "PR 71 is merged."
    )
    await runtime.task_actions.follow_up(task["id"], "Delete the audit tables.")
    assert runtime.task_actions.view(runtime.tasks.get(task["id"]))["news"] is None


async def test_a_sub_agents_commands_are_receipts(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Run the flow")
    session = harness.session(worker)
    for call, parent, output in [
        ("c1", None, "F\n1 failed in 3.0s\n<shellId: 1 completed with exit code 1>"),
        ("c2", "task-1", "....\n4 passed in 3.1s\n<shellId: 2 completed with exit code 0>"),
    ]:
        session.emit("tool.execution_start", tool_name="bash", tool_call_id=call,
                     parent_tool_call_id=parent,
                     arguments={"command": "uv run pytest -q flows/"})  # fmt: skip
        result = SimpleNamespace(content=output)
        session.emit("tool.execution_complete", tool_call_id=call, success=True,
                     parent_tool_call_id=parent, result=result)  # fmt: skip
    await settle(runtime)
    tests = runtime.task_actions.receipts(runtime.tasks.current(worker))["tests"]
    assert tests["passed"] is True and tests["failed_before"], "run again by a sub-agent"
    steps = [m["text"] for m in harness.store.messages(worker) if m["kind"] == "tool"]
    assert len(steps) == 1, "a sub-agent's steps are not the worker's own"


async def test_a_reply_to_copy_the_owner_posted_on_the_pr_is_settled(harness):
    runtime = await harness.connect()
    worker = await reply_setup(harness, runtime)
    harness.gh_replies["auth status --json hosts"] = json.dumps(
        {"hosts": {"github.com": [{"login": "evan-l", "active": True, "state": "success"}]}}
    )
    await call_tool(runtime, "draft_reply", worker, text="Yes, a README note about greet() "
                    "follows in this PR.", to="ckrsek", where="PR conversation",
                    link="https://github.com/acme/model/pull/1043#issuecomment-55")  # fmt: skip
    [draft] = harness.store.drafts()
    assert draft["target"]["repo"] == "acme/model" and draft["target"]["number"] == 1043
    harness.store.execute(
        "UPDATE drafts SET created_at=? WHERE id=?", ("2026-01-01T00:00:00+00:00", draft["id"])
    )
    harness.gh_replies["api repos/acme/model/pulls/1043/comments"] = "[]"
    harness.gh_replies["api repos/acme/model/pulls/1043/reviews"] = "[]"
    harness.gh_replies["api repos/acme/model/issues/1043/comments"] = json.dumps([
        {"id": 9, "user": {"login": "evan-l"}, "html_url": "u9",
         "body": "@ckrsek Yes, a README note about greet() follows in this PR. Thanks!"},
    ])  # fmt: skip
    # The PR's follower read it first: it is still the owner's own post.
    harness.store.add_trigger("github", "github:issuecomment:9", "evan-l commented")
    await runtime.drafts.check_posted_by_owner(every=0)
    settled = harness.store.draft(draft["id"])
    assert settled["status"] == "sent" and settled["outcome"].endswith("u9")
    told = [m["text"] for m in harness.store.messages(worker) if "posted draft" in m["text"]]
    assert told and "u9" in told[-1], "its worker is told"


async def test_only_what_the_owner_posted_after_the_draft_in_its_thread_settles_it(harness):
    runtime = await harness.connect()
    worker = await reply_setup(harness, runtime)
    harness.gh_replies["auth status --json hosts"] = json.dumps(
        {"hosts": {"github.com": [{"login": "evan-l", "active": True, "state": "success"}]}}
    )
    text = "Good catch: fixed in f61047fa, the opted-out titles are removed before inference."
    await call_tool(runtime, "draft_github_review", worker, repo="acme/model", number=1043,
                    body=text, in_reply_to=21)  # fmt: skip
    [draft] = harness.store.drafts()
    harness.store.execute("UPDATE drafts SET created_at=? WHERE id=?",
                          ("2026-11-06T10:00:00+00:00", draft["id"]))  # fmt: skip
    harness.store.add_trigger("github-sent", "github:reviewcomment:12", text)

    def comment(id_, at, thread, by="evan-l"):
        return {"id": id_, "user": {"login": by}, "in_reply_to_id": thread, "body": text,
                "created_at": at, "html_url": f"u{id_}"}  # fmt: skip

    harness.gh_replies["api repos/acme/model/pulls/1043/comments"] = json.dumps([
        comment(10, "2026-11-06T09:00:00Z", 21),  # Before the draft: another reply.
        comment(11, "2026-11-06T10:05:00Z", 34),  # In another thread.
        comment(12, "2026-11-06T10:06:00Z", 21),  # Chief of Staff posted it.
    ])  # fmt: skip
    await runtime.drafts.check_posted_by_owner(every=0)
    assert harness.store.draft(draft["id"])["status"] == "pending"
    # Live: posted the second the draft was written (GitHub's times are whole seconds).
    harness.store.execute("UPDATE drafts SET created_at=? WHERE id=?",
                          ("2026-11-06T10:07:00.600000+00:00", draft["id"]))  # fmt: skip
    harness.gh_replies["api repos/acme/model/pulls/1043/comments"] = json.dumps(
        [comment(13, "2026-11-06T10:07:00Z", 21)]
    )
    harness.store.delete(f"postcheck:{draft['id']}")
    await runtime.drafts.check_posted_by_owner(every=0)
    assert harness.store.draft(draft["id"])["status"] == "sent"
    from chiefofstaff.sources.github import same_text

    # A short reply is the same words, or another reply.
    assert same_text("Done, thanks.", "done thanks")
    assert not same_text("LGTM, merging now", "LGTM")


async def test_news_keeps_an_old_task_on_the_board(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Filter opt-outs")
    await call_tool(runtime, "report", worker, status="done", summary="Opened acme/app#71.")
    task = runtime.tasks.current(worker)
    runtime.task_actions.accept(task["id"])
    harness.store.execute("UPDATE tasks SET ended_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                          (task["id"],))  # fmt: skip
    since = "2026-11-01T00:00:00+00:00"
    assert task["id"] not in {t["id"] for t in runtime.tasks.listed(include_ended_since=since)}
    await call_tool(runtime, "report", worker, status="progress", summary="PR 71 merged.")
    assert task["id"] in {t["id"] for t in runtime.tasks.listed(include_ended_since=since)}


async def test_a_merged_pr_does_not_close_a_worker_with_other_open_work(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Fix login")
    await call_tool(
        runtime,
        "report",
        worker,
        status="done",
        summary="Opened https://github.com/acme/app/pull/7",
    )
    await settle(runtime)
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Now logout",
        task="new", title="Fix logout",
    )  # fmt: skip
    second = runtime.tasks.current(worker)
    runtime.unfollow("github:acme/app#7", finished=True, merged=True)
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    assert not harness.store.session(worker)["closed_at"], "its other task is still open"
    assert runtime.tasks.get(second["id"])["status"] != "cancelled"


async def test_a_question_stays_under_needs_you_until_answered(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Pick a db")
    task = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="question", summary="Postgres or SQLite?")
    assert runtime.snapshot()["questions"] == [], "its waiting task shows it"
    await runtime.task_actions.cancel(task["id"])
    [question] = runtime.snapshot()["questions"]
    assert question["text"] == "Postgres or SQLite?" and question["task_status"] == "cancelled"
    await runtime.task_actions.answer_question(worker, "Postgres")
    assert runtime.snapshot()["questions"] == [], "answered"
    assert runtime.tasks.get(task["id"])["status"] == "running", "answered, it goes on"
    sent = harness.session(worker).sent[-1]
    assert "Postgres" in sent and "running again" in sent
    assert 'type="cancelled"' not in sent, "not told to stop and go on at once"


async def test_an_ended_task_is_not_reopened_by_a_message(harness):
    runtime = await harness.connect()
    cancelled = await spawn(runtime, harness.router_id, "Cancelled")
    await runtime.task_actions.cancel(runtime.tasks.current(cancelled)["id"])
    await runtime.workers.close(cancelled)
    refused = await call_tool(
        runtime, "message_session", harness.router_id, session_id=cancelled, text="go on"
    )
    assert refused.result_type == "failure" and "was cancelled" in refused.text_result_for_llm
    assert harness.store.session(cancelled)["closed_at"], "not reopened for nothing"

    accepted = await spawn(runtime, harness.router_id, "Accepted")
    task = runtime.tasks.current(accepted)
    await call_tool(runtime, "report", accepted, status="done", summary="Done.")
    runtime.task_actions.accept(task["id"])
    refused = await call_tool(
        runtime, "message_session", harness.router_id, session_id=accepted, text="Add March"
    )
    assert refused.result_type == "failure" and "task='note'" in refused.text_result_for_llm
    told = await call_tool(
        runtime, "message_session", harness.router_id, session_id=accepted, text="thanks!"
    )
    assert told.result_type == "success", "an acknowledgement is a note"
    assert runtime.tasks.get(task["id"])["status"] == "done"
    assert 'type="note"' in harness.session(accepted).sent[-1]


async def test_an_acknowledgement_does_not_reopen_a_task_in_review(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Report")
    task = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="done", summary="The numbers.")
    for text in ["Thanks, received.", "收到\uff0c谢谢"]:
        await call_tool(runtime, "message_session", harness.router_id, session_id=worker, text=text)
        assert runtime.tasks.get(task["id"])["status"] == "review"
        assert 'type="note"' in harness.session(worker).sent[-1]


async def test_a_message_names_which_of_several_open_tasks_it_is_about(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "First")
    first = runtime.tasks.current(worker)
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Also this",
        task="new", earlier="keep", title="Second",
    )  # fmt: skip
    vague = await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Use Postgres"
    )
    assert vague.result_type == "failure" and "task_id" in vague.text_result_for_llm
    named = await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Use Postgres",
        task_id=first["id"],
    )  # fmt: skip
    assert named.result_type == "success" and first["id"] in named.text_result_for_llm
    assert f'task="{first["id"]}"' in harness.session(worker).sent[-1]
    wrong = await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="x",
        task_id="tnope",
    )  # fmt: skip
    assert wrong.result_type == "failure"


async def test_cancelling_a_follow_up_keeps_the_finished_result(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Report")
    task = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="done", summary="The numbers.")
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Add March",
        task_id=task["id"],
    )  # fmt: skip
    assert runtime.tasks.get(task["id"])["status"] == "running"
    await runtime.task_actions.cancel(task["id"])
    after = runtime.tasks.get(task["id"])
    assert after["status"] == "review" and after["result"] == "The numbers."
    assert "Follow-up cancelled" in after["progress"]


async def test_the_owners_follow_up_or_retry_cancelled_or_closed_gives_the_result_back(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Report")
    task = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="done", summary="The numbers.")
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    await runtime.task_actions.follow_up(task["id"], "Add March")
    await runtime.task_actions.cancel(task["id"])
    after = runtime.tasks.get(task["id"])
    assert after["status"] == "review" and after["result"] == "The numbers."

    await runtime.task_actions.follow_up(task["id"], "Add April")
    await runtime.workers.close(worker)
    after = runtime.tasks.get(task["id"])
    assert after["status"] == "review" and after["result"] == "The numbers."

    runtime.tasks.move(task["id"], "failed", progress="Crashed")
    await runtime.task_actions.retry(task["id"], "Once more")
    await runtime.task_actions.cancel(task["id"])
    assert runtime.tasks.get(task["id"])["status"] == "failed"


async def test_a_closed_worker_reopened_does_not_get_what_it_never_received(tmp_path):
    harness = Harness(tmp_path / "data", Settings(max_workers=1))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Old")
    await call_tool(runtime, "report", worker, status="done", summary="Done")
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    busy = await spawn(runtime, harness.router_id, "Busy", why_new="separate")
    await call_tool(
        runtime,
        "message_session",
        harness.router_id,
        session_id=worker,
        text="Old plan",
        task="new",
        title="Plan",
    )
    assert harness.store.session(worker)["status"] == "queued"
    plan = runtime.tasks.current(worker)
    await runtime.workers.close(worker)
    await call_tool(runtime, "report", busy, status="done", summary="Done")
    harness.session(busy).emit("session.idle")
    await settle(runtime)
    before = len(harness.session(worker).sent)
    await runtime.task_actions.retry(plan["id"], "New plan")
    await settle(runtime)
    sent = harness.session(worker).sent[before:]
    assert any("New plan" in text for text in sent)
    assert not any("Old plan" in text for text in sent), "its cancelled instruction is dropped"


async def test_a_failed_worker_does_not_start_a_turn_on_restart(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Build")
    session = harness.session(worker)
    session.emit("session.idle")
    await settle(runtime)
    runtime.add_message(worker, "router", "More", {"prompt": "More"})
    runtime.fail(harness.store.session(worker), "Copilot crashed")
    sent = len(session.sent)
    await runtime.resume_working()
    await settle(runtime)
    assert len(session.sent) == sent, "a failed worker waits for the owner"
    assert harness.store.session(worker)["status"] == "failed"


async def test_a_worker_with_several_open_tasks_names_the_one_it_reports(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "First")
    first = runtime.tasks.current(worker)
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Also this",
        task="new", earlier="keep", title="Second",
    )  # fmt: skip
    second = runtime.tasks.current(worker)
    assert first["id"] != second["id"]
    runtime.tasks.move(second["id"], "running")
    vague = await call_tool(runtime, "report", worker, status="done", summary="Done.")
    assert vague.result_type == "failure" and "task_id" in vague.text_result_for_llm
    named = await call_tool(
        runtime, "report", worker, status="done", summary="First is done.", task_id=first["id"]
    )
    assert named.result_type == "success"
    assert runtime.tasks.get(first["id"])["status"] == "review"
    assert runtime.tasks.get(second["id"])["status"] == "running"


async def test_a_worker_that_stays_silent_after_a_nudge_has_its_answer_filed(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Look into it")
    task = runtime.tasks.current(worker)
    session = harness.session(worker)
    for _ in range(2):
        session.emit("assistant.turn_start")
        session.emit("assistant.message", content="It was the cache: cleared it, works now.")
        session.emit("session.idle")
        await settle(runtime)
    assert "report" in session.sent[-1].lower(), "asked once to report"
    assert runtime.tasks.get(task["id"])["status"] == "review", "its answer is its report"
    assert "It was the cache" in runtime.tasks.get(task["id"])["result"]


def pr_view(**changes):
    view = {
        "number": 7, "url": "https://github.com/acme/app/pull/7", "state": "OPEN",
        "title": "Fix login", "isDraft": False, "mergeStateStatus": "CLEAN",
        "headRefOid": "abc1234", "author": {"login": "me"}, "reviewDecision": "APPROVED",
        "statusCheckRollup": [{"name": "tests", "conclusion": "SUCCESS"}],
        "headRefName": "fix-login", "createdAt": "2999-01-01T00:00:00Z",
    }  # fmt: skip
    return json.dumps(view | changes)


async def test_the_owners_pr_ready_to_merge_comes_as_a_card_once(harness):
    runtime = await harness.connect()
    harness.gh_replies["auth status --json hosts"] = json.dumps(
        {"hosts": {"github.com": [{"login": "me", "state": "success", "active": True}]}}
    )
    harness.gh_replies["pr view https://github.com/acme/app/pull/7"] = pr_view(
        mergeStateStatus="BLOCKED"
    )
    worker = await spawn(runtime, harness.router_id, "Fix login")
    task = runtime.tasks.current(worker)
    await call_tool(
        runtime,
        "report",
        worker,
        status="done",
        summary="Opened https://github.com/acme/app/pull/7",
    )
    await settle(runtime)
    assert not [d for d in harness.store.drafts() if d["kind"] == "merge"], "not ready yet"

    harness.gh_replies["pr view https://github.com/acme/app/pull/7"] = pr_view()
    await runtime.task_actions.refresh_prs(task["id"])
    await runtime.task_actions.refresh_prs(task["id"])
    [card] = [d for d in harness.store.drafts() if d["kind"] == "merge"]
    assert card["target"]["head"] == "abc1234" and card["target"]["task_id"] == task["id"]
    await settle(runtime)
    assert ("Ready to merge", "acme/app#7 is ready to merge") in harness.notifications

    harness.gh_replies["api repos/acme/app"] = json.dumps({"allow_squash_merge": True})
    harness.gh_replies["pr merge"] = ""
    harness.gh_replies["pr view https://github.com/acme/app/pull/7"] = pr_view(state="MERGED")
    decided = await runtime.drafts.resolve(card["id"], DraftDecision(action="send"))
    assert decided["status"] == "sent" and "squashed and merged" in decided["outcome"]
    assert runtime.tasks.get(task["id"])["status"] == "done", "merged: its task is done at once"
    merge = [args for args, _ in harness.gh_calls if args[:2] == ("pr", "merge")]
    assert merge and "--match-head-commit" in merge[0] and "abc1234" in merge[0]


async def test_a_prs_time_is_when_it_last_changed_not_when_it_was_read(harness, monkeypatch):
    """Suspected in the bug bash: every refresh made each PR row "just now"."""
    clock = iter(f"2026-11-08T10:{minute:02d}:00+00:00" for minute in range(60))
    monkeypatch.setattr("chiefofstaff.task_actions.now", lambda: next(clock))
    runtime = await harness.connect()
    harness.gh_replies["auth status --json hosts"] = json.dumps(
        {"hosts": {"github.com": [{"login": "me", "state": "success", "active": True}]}}
    )
    pr = "pr view https://github.com/acme/app/pull/7"
    harness.gh_replies[pr] = pr_view(mergeStateStatus="BLOCKED")
    worker = await spawn(runtime, harness.router_id, "Fix login")
    task = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="done",
                    summary="Opened https://github.com/acme/app/pull/7")  # fmt: skip
    await settle(runtime)

    def shown():
        [found] = runtime.task_actions.view(runtime.tasks.get(task["id"]))["receipts"]["prs"]
        return found

    first = shown()
    assert first["changed_at"] == first["at"]
    await runtime.task_actions.refresh_prs(task["id"])
    again = shown()
    assert again["at"] > first["at"] and again["changed_at"] == first["changed_at"]
    harness.gh_replies[pr] = pr_view(
        mergeStateStatus="BLOCKED", statusCheckRollup=[{"name": "tests", "conclusion": "FAILURE"}]
    )
    await runtime.task_actions.refresh_prs(task["id"])
    failed = shown()
    assert failed["checks"] != again["checks"] and failed["changed_at"] == failed["at"]
    assert failed["changed_at"] > again["changed_at"]


async def test_a_ready_to_merge_card_goes_when_the_pr_moves_on(harness):
    runtime = await harness.connect()
    harness.gh_replies["auth status --json hosts"] = json.dumps(
        {"hosts": {"github.com": [{"login": "me", "state": "success", "active": True}]}}
    )
    harness.gh_replies["pr view https://github.com/acme/app/pull/7"] = pr_view()
    worker = await spawn(runtime, harness.router_id, "Fix login")
    task = runtime.tasks.current(worker)
    await call_tool(
        runtime,
        "report",
        worker,
        status="done",
        summary="Opened https://github.com/acme/app/pull/7",
    )
    await settle(runtime)
    [card] = [d for d in harness.store.drafts() if d["kind"] == "merge"]
    harness.gh_replies["pr view https://github.com/acme/app/pull/7"] = pr_view(
        headRefOid="def5678", statusCheckRollup=[{"name": "tests", "status": "IN_PROGRESS"}]
    )
    await runtime.task_actions.refresh_prs(task["id"])
    assert harness.store.draft(card["id"])["status"] == "rejected", "its head moved"
    assert not [d for d in harness.store.drafts() if d["kind"] == "merge"]


async def test_a_pr_a_worker_reviewed_is_not_offered_to_merge(harness):
    runtime = await harness.connect()
    harness.gh_replies["auth status --json hosts"] = json.dumps(
        {"hosts": {"github.com": [{"login": "me", "state": "success", "active": True}]}}
    )
    harness.gh_replies["pr view https://github.com/acme/app/pull/7"] = pr_view()
    worker = await spawn(runtime, harness.router_id, "Review #7")
    review = {"repo": "acme/app", "number": 7, "url": "https://github.com/acme/app/pull/7",
              "verdict": "approve", "comments": []}  # fmt: skip
    harness.store.create_draft(worker, "github", review, "LGTM")
    await call_tool(
        runtime,
        "report",
        worker,
        status="done",
        summary="Opened https://github.com/acme/app/pull/7",
    )
    await settle(runtime)
    assert not [d for d in harness.store.drafts() if d["kind"] == "merge"]


def signed_in_as_me(harness):
    harness.gh_replies["auth status --json hosts"] = json.dumps(
        {"hosts": {"github.com": [{"login": "me", "state": "success", "active": True}]}}
    )


async def test_only_the_tasks_own_prs_settle_it_or_come_to_merge(harness):
    runtime = await harness.connect()
    signed_in_as_me(harness)
    # #8 is its own (opened since the task began); #6 merged long ago and #9 is an old open PR
    # of the owner's: its report only mentions them.
    harness.gh_replies["pr view https://github.com/acme/app/pull/8"] = pr_view(
        number=8, url="https://github.com/acme/app/pull/8", mergeStateStatus="BLOCKED"
    )
    harness.gh_replies["pr view https://github.com/acme/app/pull/6"] = pr_view(
        number=6, state="MERGED", author={"login": "someone"}, createdAt="2020-01-01T00:00:00Z"
    )
    harness.gh_replies["pr view https://github.com/acme/app/pull/9"] = pr_view(
        number=9, headRefName="old", createdAt="2020-01-01T00:00:00Z"
    )
    worker = await spawn(runtime, harness.router_id, "Fix the regression")
    task = runtime.tasks.current(worker)
    summary = (
        "Opened https://github.com/acme/app/pull/8, fixing the regression from "
        "https://github.com/acme/app/pull/6 (like https://github.com/acme/app/pull/9)."
    )
    await call_tool(runtime, "report", worker, status="done", summary=summary)
    await settle(runtime)
    await runtime.task_actions.refresh_prs(task["id"])
    assert runtime.tasks.get(task["id"])["status"] == "review", "its own PR is still open"
    assert not [d for d in harness.store.drafts() if d["kind"] == "merge"], "only its own"


async def test_a_ready_to_merge_card_comes_back_once_the_pr_is_ready_again(harness):
    runtime = await harness.connect()
    signed_in_as_me(harness)
    pr = "pr view https://github.com/acme/app/pull/7"
    harness.gh_replies[pr] = pr_view()
    worker = await spawn(runtime, harness.router_id, "Fix login")
    task = runtime.tasks.current(worker)
    await call_tool(
        runtime,
        "report",
        worker,
        status="done",
        summary="Opened https://github.com/acme/app/pull/7",
    )
    await settle(runtime)
    [first] = [d for d in harness.store.drafts() if d["kind"] == "merge"]

    def cards():
        return [d for d in harness.store.drafts() if d["kind"] == "merge"]

    # GitHub works out mergeability again (its base moved): the card stays as it is.
    harness.gh_replies[pr] = pr_view(mergeStateStatus="UNKNOWN")
    await runtime.task_actions.refresh_prs(task["id"])
    assert [card["id"] for card in cards()] == [first["id"]]
    # Its checks run again at the same commit: not ready for now, then ready again.
    harness.gh_replies[pr] = pr_view(statusCheckRollup=[{"name": "tests", "status": "QUEUED"}])
    await runtime.task_actions.refresh_prs(task["id"])
    assert not cards()
    harness.gh_replies[pr] = pr_view()
    await runtime.task_actions.refresh_prs(task["id"])
    [again] = cards()
    assert again["id"] != first["id"] and again["target"]["head"] == "abc1234"
    await settle(runtime)
    alerts = [n for n in harness.notifications if n[0] == "Ready to merge"]
    assert len(alerts) == 1, "the same commit is said once"
    await runtime.drafts.resolve(again["id"], DraftDecision(action="reject"))
    await runtime.task_actions.refresh_prs(task["id"])
    assert not cards(), "not now: stays so"


async def test_two_cancellations_in_a_minute_both_reach_the_worker(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "First")
    tasks = [runtime.tasks.current(worker)]
    for title in ("Second", "Third"):
        await call_tool(
            runtime, "message_session", harness.router_id, session_id=worker, text=title,
            task="new", earlier="keep", title=title,
        )  # fmt: skip
        tasks.append(runtime.tasks.current(worker))
    await runtime.task_actions.cancel(tasks[1]["id"])
    await runtime.task_actions.cancel(tasks[2]["id"])
    prompts = [
        m["meta"]["prompt"] for m in harness.store.messages(worker) if m["meta"].get("prompt")
    ]
    for task in tasks[1:]:
        assert any(task["id"] in prompt and "cancelled" in prompt for prompt in prompts), task

    session = harness.session(harness.router_id)
    for _ in range(2):  # The same answer twice (a turn retried): shown once.
        session.emit("assistant.turn_start")
        session.emit("assistant.message", content="All three are on it.")
        session.emit("session.idle")
        await settle(runtime)
    said = [m for m in harness.store.messages(harness.router_id) if m["kind"] == "assistant"]
    assert [m["text"] for m in said].count("All three are on it.") == 1


async def test_another_tasks_report_leaves_an_open_question_alone(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Pick a db")
    first = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="question", summary="Postgres or SQLite?")
    await runtime.task_actions.cancel(first["id"])
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Also the cache",
        task="new", earlier="keep", title="Cache",
    )  # fmt: skip
    second = runtime.tasks.current(worker)
    runtime.tasks.move(second["id"], "running")
    await call_tool(
        runtime, "report", worker, status="progress", summary="Cache half done.",
        task_id=second["id"],
    )  # fmt: skip
    [question] = runtime.snapshot()["questions"]
    assert question["task_id"] == first["id"], "task B's progress does not answer task A"
    await call_tool(runtime, "report", worker, status="question", summary="Redis or memcached?",
                    task_id=second["id"])  # fmt: skip
    assert len(runtime.asks.session_questions(worker)) == 2, "one per task"
    await runtime.task_actions.answer_question(question["id"], "Postgres")
    left = runtime.asks.session_questions(worker)
    assert [q["task_id"] for q in left] == [second["id"]]


async def test_a_stalled_task_is_asked_once_where_it_stands(harness):
    from datetime import UTC, datetime, timedelta

    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "SLM eval")
    task = runtime.tasks.current(worker)
    session = harness.session(worker)
    await call_tool(runtime, "report", worker, status="progress", summary="Started the run.")
    session.emit("session.idle")
    await settle(runtime)
    assert 'type="wait-missing"' in session.sent[-1], "asked what it waits for"
    session.emit("session.idle")  # And said nothing.
    await settle(runtime)
    later = datetime.now(UTC) + timedelta(hours=5)
    assert runtime.task_actions.stalled(runtime.tasks.get(task["id"]), later) >= 4
    nudges = lambda: [text for text in session.sent if 'type="nudge"' in text]  # noqa: E731
    await runtime.task_actions.nudge_stalled(later)
    await settle(runtime)
    assert len(nudges()) == 1
    await runtime.task_actions.nudge_stalled(later)
    await settle(runtime)
    assert len(nudges()) == 1, "once, until something happens on it again"

    await call_tool(runtime, "check_back", worker, minutes=60, what="the Flyte run",
                    task_id=task["id"])  # fmt: skip
    assert not runtime.task_actions.stalled(runtime.tasks.get(task["id"]), later), "it waits"


async def test_a_worker_that_checks_back_is_not_asked_to_report_meanwhile(harness):
    from datetime import UTC, datetime, timedelta

    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Nightly check")
    task = runtime.tasks.current(worker)
    session = harness.session(worker)
    await call_tool(runtime, "check_back", worker, minutes=30, what="the nightly run")
    session.emit("assistant.message", content="I'll look again in 30 minutes.")
    session.emit("session.idle")
    await settle(runtime)
    assert not [text for text in session.sent if "report-missing" in text], "it said when"
    assert runtime.tasks.get(task["id"])["status"] == "running"
    assert await runtime.watchdog.check(datetime.now(UTC).timestamp() + 5000) == []
    await runtime.task_actions.wake_check_backs(datetime.now(UTC) + timedelta(minutes=31))
    await settle(runtime)
    assert 'type="check-back"' in session.sent[-1]


async def test_a_pr_opened_in_a_turn_belongs_to_the_task_that_turn_is_on(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Fix login")
    fix = runtime.tasks.current(worker)
    session = harness.session(worker)
    session.emit("user.message", content=harness.session(worker).sent[0])
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Then the docs",
        task="new", earlier="keep", title="Docs",
    )  # fmt: skip
    docs = runtime.tasks.current(worker)
    session.emit("tool.execution_start", tool_name="bash", tool_call_id="pr",
                 arguments={"command": "gh pr create --fill"})  # fmt: skip
    session.emit("tool.execution_complete", tool_call_id="pr", success=True,
                 result=SimpleNamespace(content="https://github.com/acme/app/pull/9\n"
                                        "<exited with exit code 0>"))  # fmt: skip
    await settle(runtime)
    assert runtime.task_actions.opened_prs(fix["id"]) == {"acme/app#9"}
    assert not runtime.task_actions.opened_prs(docs["id"]), "given meanwhile, not what it ran"


async def test_a_question_kept_by_an_older_version_is_cleared_by_its_answer(tmp_path):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Pick a DB")
    task = runtime.tasks.current(worker)
    harness.store.set(
        f"ownerq:{worker}",
        json.dumps({"text": "Postgres or SQLite?", "task_id": task["id"], "at": "2026-11-01"}),
    )
    runtime.asks.migrate_questions()  # At startup.
    [question] = runtime.asks.owner_questions()
    assert question["id"] == f"{worker}:{task['id']}"
    await runtime.task_actions.follow_up(task["id"], "Postgres")
    assert runtime.asks.owner_questions() == []


async def test_a_done_report_in_the_turn_that_set_a_check_back_is_refused_once(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Nightly check")
    task = runtime.tasks.current(worker)
    await call_tool(runtime, "check_back", worker, minutes=5, what="the nightly run")
    early = await call_tool(runtime, "report", worker, status="done", summary="Woken; all good.")
    assert early.result_type == "failure" and "End your turn" in early.text_result_for_llm
    assert runtime.tasks.get(task["id"])["status"] == "running"
    again = await call_tool(runtime, "report", worker, status="done", summary="It had finished.")
    assert again.result_type == "success", "said twice: taken"


async def test_the_first_to_mention_a_pr_does_not_own_it(harness):
    """#432's worker named #72 first while answering a question; #72's own worker opened it
    later: #72 is the second task's, the first is only about it."""
    runtime = await harness.connect()
    harness.own_pull("acme/app", 72)
    asker = await spawn(runtime, harness.router_id, "Schedule BG features")
    first = runtime.tasks.current(asker)
    await call_tool(runtime, "report", asker, status="progress",
                    summary="No Qwen rerun: see https://github.com/acme/app/pull/72")  # fmt: skip
    await call_tool(runtime, "report", asker, status="progress",
                    summary="https://github.com/acme/app/pull/72 is another worker's; I'm not "
                    "following it.")  # fmt: skip
    maker = await spawn(runtime, harness.router_id, "Precompute embeddings", why_new="separate")
    second = runtime.tasks.current(maker)
    await call_tool(runtime, "report", maker, status="done",
                    summary="PR opened: https://github.com/acme/app/pull/72")  # fmt: skip
    pull = [{"label": "acme/app#72", "author": "me", "created": "2999-01-01T00:00:00Z"}]
    own, about = await runtime.task_actions.pr_roles(first, pull)
    assert not own and about == {"acme/app#72"}, "it brought it up: about it, not its own"
    own, _ = await runtime.task_actions.pr_roles(second, pull)
    assert own == {"acme/app#72"}
    assert runtime.task_actions.creator("acme/app#72") == second["id"]


async def test_an_opener_on_record_wins_over_an_earlier_claim(harness):
    runtime = await harness.connect()
    claimer = await spawn(runtime, harness.router_id, "Claimer")
    claimed = runtime.tasks.current(claimer)
    await call_tool(runtime, "report", claimer, status="progress",
                    summary="Opened https://github.com/acme/app/pull/80")  # fmt: skip
    opener = await spawn(runtime, harness.router_id, "Opener", why_new="separate")
    opened = runtime.tasks.current(opener)
    runtime.task_actions.record_opened(opened["id"], ["https://github.com/acme/app/pull/80"])
    pull = [{"label": "acme/app#80", "author": "me", "created": "2999-01-01T00:00:00Z"}]
    harness.own_pull("acme/app", 80)
    assert (await runtime.task_actions.pr_roles(claimed, pull))[0] == set()
    assert (await runtime.task_actions.pr_roles(opened, pull))[0] == {"acme/app#80"}
    assert not runtime.task_actions.owns_pr(claimed, "acme/app#80")
    assert runtime.task_actions.owns_pr(opened, "acme/app#80")


async def test_a_merge_card_on_a_task_that_does_not_own_the_pr_goes(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Reader")
    task = runtime.tasks.current(worker)
    card = harness.store.create_draft(worker, "merge", {
        "label": "acme/app#72", "repo": "acme/app", "number": 72, "url": "u", "title": "t",
        "head": "abc", "task_id": task["id"], "review": "",
    }, "t")  # fmt: skip
    harness.gh_replies["auth status --json hosts"] = json.dumps(
        {"hosts": {"github.com": [{"login": "me", "active": True, "state": "success"}]}}
    )
    pr = {"label": "acme/app#72", "state": "OPEN", "checks": "passing", "merge_state": "CLEAN",
          "head": "abc", "author": "me", "url": "u"}  # fmt: skip
    await runtime.task_actions.offer_merge(task, [pr])
    closed = harness.store.draft(card["id"])
    assert closed["status"] == "rejected" and "Not this task's" in closed["outcome"]


async def test_a_pr_opened_with_a_github_tool_is_the_tasks_own(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Fix it")
    task = runtime.tasks.current(worker)
    session = harness.session(worker)
    session.emit("tool.execution_start", tool_name="github-mcp-server-create_pull_request",
                 tool_call_id="mcp1", arguments={"title": "Fix"})  # fmt: skip
    session.emit("tool.execution_complete", tool_call_id="mcp1", success=True,
                 result=SimpleNamespace(content='{"html_url": '
                                        '"https://github.com/acme/app/pull/31"}'))  # fmt: skip
    await settle(runtime)
    assert runtime.task_actions.opened_prs(task["id"]) == {"acme/app#31"}


async def test_a_check_back_that_cannot_be_delivered_is_kept_and_tried_again(harness):
    from datetime import UTC, datetime, timedelta

    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Nightly check")
    task = runtime.tasks.current(worker)
    await call_tool(runtime, "check_back", worker, minutes=5, what="the nightly run")
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    deliver = runtime.delivery.deliver

    async def failing(*args, **kwargs):
        raise RuntimeError("Copilot is restarting")

    runtime.delivery.deliver = failing
    later = datetime.now(UTC) + timedelta(minutes=6)
    await runtime.task_actions.wake_check_backs(later)
    kept = runtime.task_actions.check_back(runtime.tasks.get(task["id"]))
    assert kept and kept["attempts"] == 1 and "restarting" in kept["error"]
    assert datetime.fromisoformat(kept["at"]) > later, "tried again a few minutes later"
    await runtime.task_actions.wake_check_backs(later)
    assert runtime.task_actions.check_back(runtime.tasks.get(task["id"]))["attempts"] == 1

    runtime.delivery.deliver = deliver
    await runtime.task_actions.wake_check_backs(later + timedelta(minutes=6))
    await settle(runtime)
    assert not runtime.task_actions.check_back(runtime.tasks.get(task["id"])), "delivered: done"
    assert 'type="check-back"' in harness.session(worker).sent[-1]


async def test_a_progress_report_then_silence_is_asked_what_it_waits_for_once(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Realtime interview")
    task = runtime.tasks.current(worker)
    session = harness.session(worker)
    await call_tool(runtime, "report", worker, status="progress",
                    summary="Voice works; I need your OpenAI key to test it live.")  # fmt: skip
    session.emit("session.idle")
    await settle(runtime)
    asked = [text for text in session.sent if 'type="wait-missing"' in text]
    assert len(asked) == 1 and "report blocked or question" in asked[0]
    await call_tool(runtime, "report", worker, status="progress", summary="Still waiting.")
    session.emit("session.idle")
    await settle(runtime)
    assert len([text for text in session.sent if 'type="wait-missing"' in text]) == 1, "once"
    await call_tool(runtime, "report", worker, status="blocked",
                    summary="I need your OpenAI API key.", task_id=task["id"])  # fmt: skip
    assert runtime.tasks.get(task["id"])["status"] == "waiting", "now it waits for the owner"


async def test_a_progress_report_with_a_check_back_is_not_asked(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Eval run")
    session = harness.session(worker)
    await call_tool(runtime, "report", worker, status="progress", summary="Started the eval.")
    await call_tool(runtime, "check_back", worker, minutes=30, what="the eval run")
    session.emit("session.idle")
    await settle(runtime)
    assert not [text for text in session.sent if 'type="wait-missing"' in text]


async def test_a_new_task_needs_a_title_saying_what_it_delivers(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Review #589")
    refused = await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker,
        text="Handle the new Slack message\nJingtao asks why two gets are slow", task="new",
    )  # fmt: skip
    assert refused.result_type == "failure" and "title" in refused.text_result_for_llm
    assert len(runtime.tasks.for_session(worker)) == 1, "no task named after a first line"


async def test_a_note_about_a_finished_task_does_not_move_its_worker_off_the_one_it_is_on(harness):
    """D2: the worker is on B; the Router notes something about A (delivered already); A is
    reported done. B is what it works on: a note names no work, and a reported task is not
    what it is on."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Write the AI interviewer spec")
    spec = runtime.tasks.current(worker)
    session = harness.session(worker)
    session.emit("user.message", content=session.sent[0])
    await call_tool(runtime, "report", worker, status="progress",
                    summary="Spec written; I need your OpenAI key for the voice test.")  # fmt: skip
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, task="new",
        earlier="keep", title="Test the AI interviewer end to end", text="Run it end to end",
    )  # fmt: skip
    e2e = runtime.tasks.current(worker)
    session.emit("user.message", content=session.sent[-1])
    await settle(runtime)
    assert runtime.turn_task(worker) == e2e["id"]
    view = {t["id"]: t for t in map(runtime.task_actions.view, runtime.tasks.for_session(worker))}
    assert view[spec["id"]]["started"] and view[e2e["id"]]["started"]
    runtime.update_session(worker, status="working")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="note",
                    task_id=spec["id"], text="FYI: the spec was delivered already.")  # fmt: skip
    note = harness.store.messages(worker)[-1]["meta"]["prompt"]
    assert 'type="note"' in note and spec["id"] in note
    session.emit("user.message", content=note)
    await settle(runtime)
    assert runtime.turn_task(worker) == e2e["id"], "a note does not change what it works on"
    # Even an older turn on A: once A is reported, the worker is on what it still has open.
    harness.store.set(f"turntask:{worker}", spec["id"])
    await call_tool(runtime, "report", worker, status="done", task_id=spec["id"],
                    summary="The spec is in the doc.")  # fmt: skip
    assert runtime.tasks.get(spec["id"])["status"] == "review"
    assert runtime.working_on(worker) == e2e["id"]
    assert runtime.view(harness.store.session(worker))["turn_task"] == e2e["id"]
    assert not runtime.task_actions.view(runtime.tasks.get(spec["id"]))["follow_up"]
    # The owner asks about A in the chat: following up on it now.
    harness.store.set(f"followup:{spec['id']}", "1")
    assert runtime.working_on(worker) == spec["id"]


async def test_a_new_task_says_what_becomes_of_the_running_one(harness):
    """D1: a new task for a worker whose task still runs (delivered, or waiting on the owner)
    says what becomes of it, so that one does not sit as up next."""
    runtime = await harness.connect()

    async def worker_with(title):
        worker = await spawn(runtime, harness.router_id, title)
        harness.session(worker).emit("user.message", content=harness.session(worker).sent[0])
        await settle(runtime)
        return worker, runtime.tasks.current(worker)

    def new(worker, title, **extra):
        return call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                         task="new", title=title, text=title, **extra)  # fmt: skip

    worker, spec = await worker_with("Write the spec")
    refused = await new(worker, "Build it")
    assert refused.result_type == "failure"
    assert spec["id"] in refused.text_result_for_llm and "earlier=" in refused.text_result_for_llm
    assert [t["title"] for t in runtime.tasks.for_session(worker)] == ["Write the spec"]
    finish = await new(worker, "Build it", earlier="finish")
    assert finish.result_type == "success", finish.text_result_for_llm
    prompt = harness.store.messages(worker)[-1]["meta"]["prompt"]
    assert f"report task {spec['id']}" in prompt and "blocked or question" in prompt
    assert runtime.tasks.get(spec["id"])["status"] == "running"

    worker, old = await worker_with("Plan the migration")
    await new(worker, "Plan the migration in two steps", earlier="superseded")
    assert runtime.tasks.get(old["id"])["status"] == "cancelled"
    assert "Superseded" in (runtime.tasks.get(old["id"])["result"] or "") or any(
        "Superseded" in m["text"] for m in harness.store.messages(worker)
    )

    worker, first = await worker_with("Fix login")
    harness.session(worker).emit("assistant.turn_start")  # At work on it.
    await settle(runtime)
    await new(worker, "Then the docs", earlier="keep")
    prompt = harness.store.messages(worker)[-1]["meta"]["prompt"]
    assert f"Task {first['id']} “Fix login” stays open" in prompt
    assert runtime.tasks.get(first["id"])["status"] == "running"
    # Not taken up yet: the new one waits its turn (it is sent when this turn ends).
    docs = runtime.tasks.current(worker)
    assert not runtime.task_actions.view(docs)["started"]
    assert runtime.task_actions.view(runtime.tasks.get(first["id"]))["started"]
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    assert runtime.task_actions.view(runtime.tasks.get(docs["id"]))["started"], "sent now"


async def test_a_report_given_again_in_the_same_turn_amends_it_quietly(harness):
    """Reported done, then again in the same turn with its criteria: the card has the latest,
    the Router and the owner heard of it once. In a later turn (a fix after review comments)
    it is news again."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Write spec.txt")
    task = runtime.tasks.current(worker)
    session = harness.session(worker)
    session.emit("user.message", content=session.sent[0])
    await settle(runtime)

    def told():
        return [m for m in harness.store.messages(harness.router_id) if m["kind"] == "inbound"]

    await call_tool(runtime, "report", worker, status="done", summary="Wrote spec.txt.")
    again = await call_tool(runtime, "report", worker, status="done",
                            summary="Wrote spec.txt (217 bytes), checked with ls.")  # fmt: skip
    assert "not told again" in again.text_result_for_llm
    assert runtime.tasks.get(task["id"])["result"] == "Wrote spec.txt (217 bytes), checked with ls."
    assert len(told()) == 1
    session.emit("session.idle")
    session.emit("user.message", content='<message from="system" type="thread">CI</message>')
    await settle(runtime)
    await call_tool(runtime, "report", worker, status="done", summary="Fixed the review note.")
    assert len(told()) == 2, "a later turn's report is heard"


async def test_a_new_task_sent_while_its_worker_finishes_does_not_hold_back_the_report(harness):
    """Unread news about the task it reports holds the report back once (it would be without
    it); a new task, sent while it finishes another, does not: that one is done, and the new
    one comes next (in the live run the worker did the new one first and the turn task stuck)."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Write the spec")
    spec = runtime.tasks.current(worker)
    session = harness.session(worker)
    session.emit("user.message", content=session.sent[0])
    session.emit("assistant.turn_start")
    await settle(runtime)
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, task="new",
        earlier="keep", title="Build it", text="Then build it",
    )  # fmt: skip
    build = runtime.tasks.current(worker)
    assert runtime.states[worker].unread, "sent mid-turn: not shown to it yet"
    done = await call_tool(runtime, "report", worker, status="done", summary="Spec written.",
                           task_id=spec["id"])  # fmt: skip
    assert done.result_type == "success", done.text_result_for_llm
    assert runtime.tasks.get(spec["id"])["status"] == "review"
    session.emit("user.message", content=session.sent[-1])
    session.emit("assistant.turn_start")
    await settle(runtime)
    assert runtime.turn_task(worker) == build["id"]
    runtime.update_session(worker, status="working")  # At work on it.
    await runtime.task_actions.follow_up(build["id"], "Use the release config")
    held = await call_tool(runtime, "report", worker, status="done", summary="Built.",
                           task_id=build["id"])  # fmt: skip
    assert held.result_type == "failure" and "Use the release config" in held.text_result_for_llm


async def busy_worker(runtime, harness, title):
    """A worker in a turn on its first task (its conversation shows what it is sent)."""
    worker = await spawn(runtime, harness.router_id, title)
    session = harness.session(worker)
    session.emit("user.message", content=session.sent[0])
    session.emit("assistant.turn_start")
    await settle(runtime)
    runtime.update_session(worker, status="working")
    return worker, session, runtime.tasks.current(worker)


async def test_stop_stops_the_task_at_work_and_resends_what_it_never_saw(harness):
    """B1/B3: a task given while it works, then Stop: the task it was on is stopped (not the
    new one, which is no question), and the new task's instruction it never saw goes out with
    its next message."""
    runtime = await harness.connect()
    worker, session, sleep = await busy_worker(runtime, harness, "Sleep A")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="new",
                    earlier="keep", title="Write B", text="Write B into b.txt")  # fmt: skip
    write_b = runtime.tasks.current(worker)
    assert runtime.states[worker].unread, "sent within its turn, not seen yet"
    await runtime.workers.interrupt(worker)
    assert session.aborted
    stopped = runtime.task_actions.view(runtime.tasks.get(sleep["id"]))
    assert stopped["status"] == "waiting" and stopped["stopped"] == "you"
    assert runtime.tasks.get(write_b["id"])["status"] == "running", "not started: no question"
    assert not runtime.task_actions.view(runtime.tasks.get(write_b["id"]))["stopped"]
    await runtime.task_actions.follow_up(sleep["id"], "Go on where you stopped.")
    assert "Write B into b.txt" in session.sent[-1], "what it never saw comes with it"
    assert runtime.tasks.get(sleep["id"])["status"] == "running"
    assert not runtime.task_actions.view(runtime.tasks.get(sleep["id"]))["stopped"]


async def test_cancelling_the_task_at_work_stops_it_and_it_goes_on_with_the_other(harness):
    runtime = await harness.connect()
    worker, session, sleep = await busy_worker(runtime, harness, "Sleep A")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="new",
                    earlier="keep", title="Write B", text="Write B into b.txt")  # fmt: skip
    write_b = runtime.tasks.current(worker)
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    task=sleep["id"], text="Also sleep 5 more")  # fmt: skip
    await runtime.task_actions.cancel(sleep["id"], "Not needed")
    await settle(runtime)
    assert session.aborted, "its work on the cancelled task stops now"
    told = session.sent[-1]
    assert 'type="cancelled"' in told and write_b["id"] in told and "Write B into b.txt" in told
    assert "Also sleep 5 more" not in told, "what it never saw about the cancelled task is dropped"
    assert runtime.tasks.get(write_b["id"])["status"] == "running"


async def test_the_owners_chat_answers_the_task_that_asked(harness):
    """Its question was on A; B is newer: the owner's answer in chat is A's."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Pick a number")
    ask = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="question", summary="1 or 2?",
                    task_id=ask["id"])  # fmt: skip
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="new",
                    title="Write hello", text="Write hello")  # fmt: skip
    hello = runtime.tasks.current(worker)
    assert hello["id"] != ask["id"]
    message = await runtime.owner_send(worker, "2")
    assert message["meta"]["task_id"] == ask["id"]
    assert not [q for q in runtime.asks.session_questions(worker) if q["task_id"] == ask["id"]]


async def test_a_task_the_owner_answered_goes_on_before_its_next_report(harness):
    """Bug bash 3 (G17): answered in chat, a task said "waits for your answer" until its worker
    reported again; finished work it asked about goes back where it was."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Pick a number")
    ask = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="question", summary="1 or 2?")
    assert runtime.tasks.get(ask["id"])["status"] == "waiting"
    await runtime.owner_send(worker, "2")
    assert runtime.tasks.get(ask["id"])["status"] == "running"
    await call_tool(runtime, "report", worker, status="done", summary="Picked 2.")
    await call_tool(runtime, "report", worker, status="question", summary="Merge the PR now?")
    assert runtime.tasks.get(ask["id"])["status"] == "waiting"
    await runtime.owner_send(worker, "Yes, merge it")
    after = runtime.tasks.get(ask["id"])
    assert after["status"] == "review" and after["result"] == "Picked 2."
    await call_tool(runtime, "report", worker, status="done", summary="Merged.")
    after = runtime.task_actions.view(runtime.tasks.get(ask["id"]))
    assert after["result"] == "Picked 2." and after["news"]["text"] == "Merged."


async def test_a_chat_about_finished_work_is_news_not_a_new_result(harness):
    """B2: asked something in chat, an accepted task stays accepted and keeps its result;
    what the worker says is an update on it. From Home (@), its answer shows there too."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Alpha file")
    task = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="done", summary="alpha and beta written.")
    runtime.task_actions.accept(task["id"])
    await runtime.owner_send_to(harness.router_id, worker, "what is 17*3?")
    result = await call_tool(runtime, "report", worker, status="done", summary="17*3 = 51.")
    assert "update" in result.text_result_for_llm
    after = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    assert after["status"] == "done" and after["result"] == "alpha and beta written."
    assert after["news"]["text"] == "17*3 = 51."
    session = harness.session(worker)
    session.emit("assistant.message", content="It is 51.")
    session.emit("session.idle")
    await settle(runtime)
    answers = [m for m in harness.store.messages(harness.router_id) if m["kind"] == "answer"]
    assert len(answers) == 1 and answers[0]["text"] == "It is 51."
    assert answers[0]["meta"]["from_title"] == "Alpha file"
    session.emit("session.idle")
    await settle(runtime)
    assert len([m for m in harness.store.messages(harness.router_id) if m["kind"] == "answer"]) == 1


async def test_a_pr_opened_as_asked_in_chat_is_followed(harness):
    """Code review: the chat's update returned before following the PR it opened."""
    runtime = await harness.connect()
    harness.own_pull("acme/app", 7)
    worker = await spawn(runtime, harness.router_id, "Fix login")
    task = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="done", summary="Fixed on a branch.")
    await runtime.owner_send(worker, "Looks good, open a PR")
    await call_tool(runtime, "report", worker, status="done",
                    summary="Opened https://github.com/acme/app/pull/7")  # fmt: skip
    await settle(runtime)
    assert "github:acme/app#7" in harness.store.follows(worker)
    assert runtime.tasks.get(task["id"])["result"] == "Fixed on a branch."
    assert harness.store.session(worker)["status"] == "done", "done again, so it is let go"


async def test_a_stopped_task_is_stopped_until_its_worker_reports_on_it(harness):
    """Code review: "Stopped" stayed after the worker went on and asked a question; a chat
    with it must not restart the task (the Watchdog would resume stopped work)."""
    runtime = await harness.connect()
    worker, _session, task = await busy_worker(runtime, harness, "Measure")
    await runtime.workers.interrupt(worker)
    await settle(runtime)
    assert runtime.task_actions.view(runtime.tasks.get(task["id"]))["stopped"] == "you"
    await runtime.owner_send(worker, "What were you about to change?")
    shown = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    assert shown["status"] == "waiting" and shown["stopped"] == "you", "only a chat"
    await call_tool(runtime, "report", worker, status="question", summary="Which DB?")
    shown = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    assert shown["status"] == "waiting" and not shown["stopped"], "a question, not Stopped"


async def test_one_answer_about_two_reported_tasks_is_added_to_neither(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Alpha")
    alpha = runtime.tasks.current(worker)
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="new",
                    earlier="keep", title="Count", text="Count rows")  # fmt: skip
    count = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="done", summary="Alpha done.",
                    task_id=alpha["id"])  # fmt: skip
    await call_tool(runtime, "report", worker, status="done", summary="540 rows.",
                    task_id=count["id"])  # fmt: skip
    session = harness.session(worker)
    session.emit("assistant.message", content="Both tasks are done. " + "Alpha: wrote it. " * 40)
    runtime.workers.complete_report(worker)
    assert runtime.tasks.get(count["id"])["result"] == "540 rows."


async def test_a_worker_that_cannot_resume_is_failed_and_the_router_hears_it(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Resume me")
    task = runtime.tasks.current(worker)
    runtime.fail(harness.store.session(worker), "Session not found")
    await settle(runtime)
    assert runtime.tasks.get(task["id"])["status"] == "failed"
    told = [
        m for m in harness.store.messages(harness.router_id)
        if m["kind"] == "inbound" and m["meta"].get("status") == "failed"
    ]  # fmt: skip
    assert told and told[-1]["meta"]["task_id"] == task["id"]


async def test_a_worker_reports_a_task_it_cannot_do_as_failed(harness):
    """Bug bash 3 (L11): a worker had no way to say a task cannot be done."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Fix the flaky test")
    task = runtime.tasks.current(worker)
    result = await call_tool(runtime, "report", worker, status="failed",
                             summary="The repository is gone (404).")  # fmt: skip
    assert result.result_type == "success", result.text_result_for_llm
    after = runtime.tasks.get(task["id"])
    assert after["status"] == "failed" and "404" in after["progress"]
    told = [m for m in harness.store.messages(harness.router_id)
            if m["kind"] == "inbound" and m["meta"].get("status") == "failed"]  # fmt: skip
    assert told and told[-1]["meta"]["task_id"] == task["id"]
    assert harness.store.session(worker)["status"] == "failed"
    await runtime.task_actions.retry(task["id"], "It is back now.")
    await call_tool(runtime, "report", worker, status="failed", summary="Still 404.")
    assert runtime.tasks.get(task["id"])["status"] == "failed", "the retry failed too"


async def test_a_failed_start_names_its_task_and_a_task_doing_it_over_replaces_it(harness):
    """Bug bash 3 (L9/U5): a worker that could not start told the Router of no task; the
    Router started the work anew and the failed task kept its Retry beside it."""
    runtime = await harness.connect()

    async def broken(*, session_id, name, **options):
        raise RuntimeError("model unavailable")

    harness.client.create_session = broken
    worker = await spawn(runtime, harness.router_id, "Count rows")
    await settle(runtime)
    failed = runtime.tasks.current(worker)
    assert failed["status"] == "failed"
    told = [m for m in harness.store.messages(harness.router_id)
            if m["kind"] == "inbound" and m["meta"].get("status") == "failed"]  # fmt: skip
    assert told and told[-1]["meta"]["task_id"] == failed["id"]
    assert f'task="{failed["id"]}"' in told[-1]["meta"]["prompt"]
    assert "Failed in the last days" in runtime.handover(), "a new Router knows it too"
    del harness.client.create_session  # Copilot works again.
    result = await call_tool(runtime, "spawn_worker", harness.router_id, title="Count rows again",
                             instructions="Count rows", why_new="test",
                             replaces=[failed["id"]])  # fmt: skip
    assert result.result_type == "success", result.text_result_for_llm
    new = json.loads(result.text_result_for_llm)["task_id"]
    after = runtime.tasks.get(failed["id"])
    assert after["status"] == "cancelled" and new in after["progress"], "no Retry beside it"
    refused = await call_tool(runtime, "spawn_worker", harness.router_id, title="Count once more",
                              instructions="Count", why_new="test",
                              replaces=[new, "t0000000"])  # fmt: skip
    assert refused.result_type == "failure"
    assert runtime.tasks.get(new)["status"] != "cancelled", "refused before anything changed"


async def test_a_message_steered_into_a_turn_on_another_task_keeps_the_turn(harness):
    """B7: answering A's question while its worker is at work on B: B stays what it is on."""
    runtime = await harness.connect()
    worker, session, first = await busy_worker(runtime, harness, "Write hello")
    other = runtime.tasks.create(worker, "Pick a number", "pick", status="running")
    steered = env("owner", "message", "42", task=other["id"])
    session.emit("user.message", content=steered, delivery=SimpleNamespace(value="steering"))
    await settle(runtime)
    assert runtime.turn_task(worker) == first["id"]
    assert runtime.task_actions.view(runtime.tasks.get(other["id"]))["started"]


async def test_tasks_quoted_after_a_restart_are_taken_up(harness):
    runtime = await harness.connect()
    worker, _session, alpha = await busy_worker(runtime, harness, "Alpha")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="new",
                    earlier="keep", title="Count", text="Count rows")  # fmt: skip
    count = runtime.tasks.current(worker)
    runtime.states[worker].unread.clear()
    await runtime.resume_working()
    await settle(runtime)
    assert runtime.task_actions.view(runtime.tasks.get(count["id"]))["started"]
    assert runtime.turn_task(worker) == alpha["id"], "it goes on with what it was on"


async def test_the_router_hears_when_a_blocked_task_is_unblocked(harness):
    runtime = await harness.connect()
    first = await spawn(runtime, harness.router_id, "Build")
    build = runtime.tasks.current(first)
    result = await call_tool(runtime, "spawn_worker", harness.router_id, title="Deploy",
                             instructions="Deploy it", after=[build["id"]],
                             why_new="separate")  # fmt: skip
    deploy = json.loads(result.text_result_for_llm)
    await runtime.task_actions.cancel(build["id"])
    await runtime.workers.pump()
    await settle(runtime)
    blocked = [m for m in harness.store.messages(harness.router_id) if "type=\"blocked\"" in
               m["meta"].get("prompt", "")]  # fmt: skip
    assert blocked
    await runtime.task_actions.retry(build["id"])
    await runtime.workers.pump()
    await settle(runtime)
    unblocked = [m for m in harness.store.messages(harness.router_id)
                 if "type=\"unblocked\"" in m["meta"].get("prompt", "")]  # fmt: skip
    assert unblocked and deploy["task_id"] in unblocked[-1]["meta"]["prompt"]


async def test_notices_of_new_tasks_say_which_task(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Fix login")
    first = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="done", summary="Fixed.")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="new",
                    title="Then the docs", text="Then the docs")  # fmt: skip
    docs = runtime.tasks.current(worker)
    notices = [m for m in harness.store.messages(harness.router_id) if m["kind"] == "notice"]
    started = next(m for m in notices if m["text"].startswith("Started"))
    new = next(m for m in notices if m["text"].startswith("New task"))
    assert started["meta"]["task_id"] == first["id"] and new["meta"]["task_id"] == docs["id"]


async def test_a_worker_is_named_by_the_task_it_is_on(harness):
    runtime = await harness.connect()
    worker, _session, _check = await busy_worker(runtime, harness, "Check the totals")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="new",
                    earlier="keep", title="Save monthly.csv", text="Save it")  # fmt: skip
    row = harness.store.session(worker)
    assert runtime.display_title(row) == "Check the totals", "not the one given last"
    await runtime.owner_send_to(harness.router_id, worker, "how far?")
    to = [m for m in harness.store.messages(harness.router_id) if m["meta"].get("to")][-1]
    assert to["meta"]["to_title"] == "Check the totals"


async def test_links_a_task_reported_stay_on_it_and_links_it_was_given_are_told_apart(harness):
    """Bug bash #9/#10: a doc vanished from the task (and Artifacts) once a later report did not
    repeat it; a doc the owner gave counted as one it made."""
    runtime = await harness.connect()
    given = "https://docs.google.com/document/d/GIVEN123456/edit"
    made = "https://docs.google.com/document/d/MADE7654321/edit"
    worker = await spawn(runtime, harness.router_id, "Write the plan", instructions=f"Use {given}")
    task = runtime.tasks.current(worker)
    summary = f"Drafted {made}. Read {given}.\n```\ncurl https://example.com/x\n```"
    await call_tool(runtime, "report", worker, status="progress", summary=summary)
    await call_tool(runtime, "report", worker, status="done", summary="Plan written.")
    shown = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    assert made in shown["links"] and "https://example.com/x" not in shown["links"]
    assert given in shown["given_links"] and made not in shown["given_links"]
    await runtime.owner_send(worker, f"Add a summary to {made.replace('/edit', '/view')}")
    shown = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    assert made not in shown["given_links"], "its own doc, pasted back: still its own"
    assert not any("MADE7654321" in url for url in shown["given_links"])
    run = "https://airflow.example.com/dags/x/grid?dag_run_id=new"
    await call_tool(runtime, "report", worker, status="progress", summary=f"Ran {run}")
    baseline = "https://airflow.example.com/dags/x/grid?dag_run_id=baseline"
    await runtime.owner_send(worker, f"Compare with {baseline}")
    shown = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    assert baseline in shown["given_links"], "another run of the same DAG: given"


async def test_a_stop_cancels_what_it_left_running_and_go_on_steers_in(harness):
    """Bug bash 2: stopped while a command it left running in the background was pending,
    Copilot kept the session busy and held the owner's Go on until that command ended (and the
    Watchdog stepped in meanwhile)."""
    runtime = await harness.connect()
    worker, session, task = await busy_worker(runtime, harness, "Nap")
    session.background = {"0": "running", "1": "completed"}
    await runtime.workers.interrupt(worker)
    assert session.background == {"0": "cancelled", "1": "completed"}
    await runtime.task_actions.follow_up(task["id"], "Go on, but sleep 3.")
    assert session.modes[-1] == "immediate", "steered in: not queued behind a busy session"
    session.emit("session.idle")
    await settle(runtime)
    assert not [m for m in harness.store.messages(worker) if m["meta"].get("report_nudge")], (
        "the aborted turn owes no report"
    )


async def test_a_stop_before_it_saw_its_first_instruction_sends_it_again(harness):
    runtime = await harness.connect()
    harness.client.capabilities = replace(harness.client.capabilities, echoes=True)
    worker = await spawn(runtime, harness.router_id, "Write alpha")
    session = harness.session(worker)
    task = runtime.tasks.current(worker)
    first = session.sent[0]
    runtime.update_session(worker, status="working")
    await runtime.workers.interrupt(worker)
    await runtime.task_actions.follow_up(task["id"], "Go on.")
    assert "Write alpha" in session.sent[-1] and "Go on." in session.sent[-1], (
        "what it never saw goes out with the Go on"
    )
    assert first in session.sent[-1]


async def test_a_stopped_task_runs_again_when_its_worker_works_on_it(harness):
    runtime = await harness.connect()
    worker, session, task = await busy_worker(runtime, harness, "Measure")
    await runtime.workers.interrupt(worker)
    await settle(runtime)
    assert runtime.tasks.get(task["id"])["status"] == "waiting"
    session.emit("tool.execution_start", tool_call_id="late", tool_name="bash",
                 arguments={"command": "echo still the old turn"})  # fmt: skip
    await settle(runtime)
    assert runtime.tasks.get(task["id"])["status"] == "waiting", "the aborted turn's last steps"
    await runtime.owner_send(worker, "Sorry, keep going")
    session.emit("user.message", content='<message from="owner">Sorry, keep going</message>')
    session.emit("assistant.turn_start")
    session.emit("tool.execution_start", tool_call_id="c2", tool_name="bash",
                 arguments={"command": "sleep 5"})  # fmt: skip
    await settle(runtime)
    shown = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    assert shown["status"] == "running" and not shown["stopped"]


async def test_a_stopped_task_stays_stopped_while_its_worker_works_on_something_else(harness):
    """Review: any later tool call resumed the only stopped task (PR news, another task)."""
    runtime = await harness.connect()
    worker, session, task = await busy_worker(runtime, harness, "Measure")
    await runtime.workers.interrupt(worker)
    session.emit("session.idle")  # The aborted turn ends.
    await settle(runtime)
    # News on a PR of its (no task named), then a new task from the Router: it works on those.
    await runtime.delivery.deliver(
        worker, '<message from="github" type="thread">CI passed on #7</message>', kind="inbound"
    )
    session.emit("user.message", content="CI passed on #7")
    session.emit("tool.execution_start", tool_call_id="n1", tool_name="bash",
                 arguments={"command": "gh pr view 7"})  # fmt: skip
    session.emit("session.idle")
    await settle(runtime)
    assert runtime.tasks.get(task["id"])["status"] == "waiting", "PR news is not a go-on"
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="new",
                    earlier="keep", title="Write B", text="Write B into b.txt")  # fmt: skip
    session.emit("user.message", content="Write B into b.txt")
    session.emit("tool.execution_start", tool_call_id="n2", tool_name="bash",
                 arguments={"command": "echo B > b.txt"})  # fmt: skip
    await settle(runtime)
    shown = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    assert shown["status"] == "waiting" and shown["stopped"] == "you", "another task's work"


async def test_cancelling_another_task_does_not_resume_the_stopped_one(harness):
    """Review: cancelling a queued task told a stopped worker to go on with the stopped one."""
    runtime = await harness.connect()
    worker, session, sleep = await busy_worker(runtime, harness, "Sleep A")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="new",
                    earlier="keep", title="Write B", text="Write B into b.txt")  # fmt: skip
    write_b = runtime.tasks.current(worker)
    await runtime.workers.interrupt(worker)
    await settle(runtime)
    # Something held back for its next message (a peer's note, say).
    runtime.delivery.hold(worker, "A peer says hi.", kind="notice", display="A peer says hi.")
    sent = len(session.sent)
    await runtime.task_actions.cancel(write_b["id"], "")
    await settle(runtime)
    assert not [text for text in session.sent[sent:] if "Go on with" in text], session.sent[sent:]
    assert runtime.task_actions.view(runtime.tasks.get(sleep["id"]))["stopped"] == "you"


async def test_the_router_stops_work_without_cancelling_it(harness):
    """Bug bash 3 (L8): with no Stop of its own, the Router cancelled everything when the owner
    said "stop all my work" (and the owner lost Go on)."""
    runtime = await harness.connect()
    worker, session, task = await busy_worker(runtime, harness, "Measure")
    result = await call_tool(runtime, "stop_worker", harness.router_id, session_id=task["id"])
    assert result.result_type == "success" and task["id"] in result.text_result_for_llm
    assert session.aborted
    shown = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    assert shown["status"] == "waiting" and shown["stopped"] == "the Router", "Go on stays"
    again = await call_tool(runtime, "stop_worker", harness.router_id, session_id=worker)
    assert "nothing to stop" in again.text_result_for_llm


async def test_a_watch_the_router_proposes_is_added_once_the_owner_confirms(harness):
    """Bug bash 3 (U3): "watch #deploys for X" was refused: the Router knew of no watches."""
    runtime = await harness.connect()
    result = await call_tool(runtime, "add_watch", harness.router_id,
                             what="New messages in #deploys about payments")  # fmt: skip
    assert result.result_type == "success", result.text_result_for_llm
    [card] = [d for d in harness.store.drafts() if d["kind"] == "form"]
    assert not harness.store.settings().watches, "not before the owner says so"
    await runtime.drafts.resolve(
        card["id"],
        DraftDecision(action="answer", content={"what": "Messages in #deploys about payments"}),
    )
    await settle(runtime)
    assert [w.what for w in harness.store.settings().watches] == [
        "Messages in #deploys about payments"
    ]
    held = harness.store.messages(harness.router_id)[-1]
    assert held["meta"]["deferred"] and "added the watch" in held["meta"]["prompt"]


async def test_stopping_a_queued_worker_stops_its_task_for_the_owner(harness):
    runtime = await harness.connect()
    settings = harness.store.settings().model_copy(update={"max_workers": 1})
    harness.store.save_settings(settings)
    await spawn(runtime, harness.router_id, "Busy one")
    worker = await spawn(runtime, harness.router_id, "Waiting one")
    assert harness.store.session(worker)["status"] == "queued"
    task = runtime.tasks.current(worker)
    await runtime.workers.interrupt(worker)
    shown = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    assert shown["status"] == "waiting" and shown["stopped"] == "you", "not queued forever"


async def test_cancelling_the_task_at_work_does_not_ask_for_the_others_report(harness):
    runtime = await harness.connect()
    worker, session, sleep = await busy_worker(runtime, harness, "Sleep A")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="new",
                    earlier="keep", title="Write B", text="Write B into b.txt")  # fmt: skip
    await runtime.task_actions.cancel(sleep["id"], "Not needed")
    session.emit("session.idle")  # The aborted turn ends.
    await settle(runtime)
    assert not [m for m in harness.store.messages(worker) if m["meta"].get("report_nudge")]


async def test_cancelling_a_stopped_task_lets_its_worker_go_on_with_the_others(harness):
    runtime = await harness.connect()
    worker, session, sleep = await busy_worker(runtime, harness, "Sleep A")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="new",
                    earlier="keep", title="Write B", text="Write B into b.txt")  # fmt: skip
    write_b = runtime.tasks.current(worker)
    await runtime.workers.interrupt(worker)
    await settle(runtime)
    sent = len(session.sent)
    await runtime.task_actions.cancel(sleep["id"], "")
    await settle(runtime)
    assert len(session.sent) == sent + 1, "it goes on now, not after a stall"
    assert "Write B into b.txt" in session.sent[-1] and write_b["id"] in session.sent[-1]


async def test_a_watchdog_nudge_decided_before_the_worker_reported_is_not_sent(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Count")
    session = harness.session(worker)
    messages = harness.store.messages(worker)
    runtime.watchdog.save(worker, reviewed_mark=messages[-1]["id"])
    await call_tool(runtime, "report", worker, status="done", summary="12.")
    sent = len(session.sent)
    watchdog = await runtime.watchdog.session_id()
    result = await call_tool(runtime, "assess_worker", watchdog, session_id=worker,
                             verdict="stuck", action="nudge", reason="quiet",
                             message="Call report now")  # fmt: skip
    assert "Not sent" in result.text_result_for_llm and len(session.sent) == sent


async def test_closing_a_worker_keeps_the_question_it_asked_with_ask_user(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Pick a style")
    task = runtime.tasks.current(worker)
    asking = asyncio.create_task(
        runtime.asks.on_user_input({"question": "Tabs or spaces?"}, {"sessionId": worker})
    )
    await settle(runtime)
    await runtime.workers.close(worker)
    await asking
    kept = [q for q in runtime.asks.owner_questions() if q["task_id"] == task["id"]]
    assert kept and "Tabs or spaces?" in kept[0]["text"]


async def test_the_router_hears_which_finished_results_wait_for_the_owner(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Count rows")
    task = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="done", summary="540 rows.")
    assert runtime.task_actions.status_words(runtime.tasks.get(task["id"])) == "review, report"
    assert "review, report" in runtime.handover()
    runtime.store.execute("UPDATE tasks SET acceptance=? WHERE id=?",
                          (json.dumps(["rows counted twice"]), task["id"]))  # fmt: skip
    assert runtime.task_actions.status_words(runtime.tasks.get(task["id"])) == "review, to accept"


async def test_retrying_a_cancelled_task_does_not_send_its_cancelled_note(harness):
    runtime = await harness.connect()
    worker, session, _task = await busy_worker(runtime, harness, "Sleep A")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="new",
                    earlier="keep", title="Write B", text="Write B")  # fmt: skip
    write_b = runtime.tasks.current(worker)
    runtime.update_session(worker, status="idle")
    runtime.states[worker].busy = False
    await runtime.task_actions.cancel(write_b["id"], "")
    await runtime.task_actions.retry(write_b["id"], "Do it after all.")
    assert 'type="cancelled"' not in session.sent[-1] and "Do it after all." in session.sent[-1]


async def test_the_router_hears_the_owner_cancelled_a_task_and_a_new_router_knows(harness):
    """Bug bash 3 (G7/L1): the owner's cancel reached the Router only as a notice; after a
    handover it started the cancelled work again."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Count rows")
    task = runtime.tasks.current(worker)
    mine = await spawn(runtime, harness.router_id, "Sum rows", why_new="separate")
    router = harness.session(harness.router_id)
    sent = len(router.sent)
    await runtime.task_actions.cancel(task["id"], "Not needed any more")
    await call_tool(runtime, "cancel_task", harness.router_id, task_id=mine)
    await settle(runtime)
    assert len(router.sent) == sent, "not a turn of its own"
    await runtime.owner_send(harness.router_id, "What is open?")
    told = router.sent[-1]
    assert 'type="cancelled"' in told and f'task="{task["id"]}"' in told
    assert "Not needed any more" in told and "unless the owner asks" in told
    assert task_of(harness, mine) not in told, "its own cancel is not told back to it"
    handover = runtime.handover()
    assert "Cancelled in the last days" in handover and task["id"] in handover


async def test_closing_a_working_worker_tells_the_router_and_reopening_brings_its_task_back(
    harness,
):
    """Bug bash 3 (L10): closing a worker at work cancelled its task silently, for good."""
    runtime = await harness.connect()
    worker, _session, task = await busy_worker(runtime, harness, "Measure")
    await runtime.workers.close(worker, by="you")
    assert runtime.tasks.get(task["id"])["status"] == "cancelled"
    [held] = [m for m in harness.store.messages(harness.router_id)
              if 'type="closed"' in m["meta"].get("prompt", "")]  # fmt: skip
    assert task["id"] in held["meta"]["prompt"] and held["meta"]["deferred"]
    await runtime.workers.reopen(worker, restore=True)
    shown = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    assert shown["status"] == "waiting" and shown["stopped"] == "you", "back, for Go on"
    await runtime.task_actions.follow_up(task["id"], "Go on where you stopped.")
    assert runtime.tasks.get(task["id"])["status"] == "running"


async def test_cancelling_a_stopped_task_after_a_restart_lets_its_worker_go_on(tmp_path):
    """Bug bash 3 (L5): after a restart the worker's session was not live, so it never went on
    with its other task."""
    first = Harness(tmp_path / "data")
    runtime = await first.connect()
    worker, _session, sleep = await busy_worker(runtime, first, "Sleep A")
    await call_tool(runtime, "message_session", first.router_id, session_id=worker, task="new",
                    earlier="keep", title="Write B", text="Write B into b.txt")  # fmt: skip
    write_b = runtime.tasks.current(worker)
    await runtime.workers.interrupt(worker)
    await settle(runtime)
    await runtime.stop()
    second = Harness(tmp_path / "data", client=first.client)
    runtime = await second.connect()
    assert not runtime.states.is_live(worker)
    await runtime.task_actions.cancel(sleep["id"], "")
    await settle(runtime)
    sent = second.session(worker).sent[-1]
    assert "Write B into b.txt" in sent and write_b["id"] in sent


async def test_an_answer_to_an_at_from_home_that_reported_is_shown_once(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Bg wait")
    await runtime.owner_send_to(harness.router_id, worker, "How many seconds are left?")
    await call_tool(runtime, "report", worker, status="done", summary="0 left: done.")
    session = harness.session(worker)
    session.emit("assistant.message", content="0 seconds are left.")
    session.emit("session.idle")
    await settle(runtime)
    router = harness.store.messages(harness.router_id)
    assert [m for m in router if m["kind"] == "inbound" and "0 left" in m["text"]]
    assert not [m for m in router if m["kind"] == "answer"], "its report is on Home already"


async def test_the_router_hears_what_a_worker_the_owner_asked_directly_answered(harness):
    """Bug bash: the Router said "it is answering your question" long after it had."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Detached nap")
    await call_tool(runtime, "report", worker, status="done", summary="Started.")
    await runtime.owner_send(worker, "Is fin.txt there yet?")
    harness.session(worker).emit("assistant.message", content="No, not yet: 1 minute left.")
    await settle(runtime)
    await runtime.owner_send(harness.router_id, "What is going on?")
    sent = harness.session(harness.router_id).sent[-1]
    assert "Is fin.txt there yet?" in sent and "It answered already" in sent
    assert "1 minute left" in sent


async def test_the_router_hears_each_direct_question_with_its_own_answer(harness):
    """Bug bash 3: two questions to a worker before the Router's next turn both carried the
    newest answer."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Detached nap")
    await call_tool(runtime, "report", worker, status="done", summary="Started.")
    session = harness.session(worker)
    await runtime.owner_send(worker, "Is fin.txt there yet?")
    session.emit("assistant.message", content="No, not yet: 1 minute left.")
    await settle(runtime)
    await runtime.owner_send(worker, "And now?")
    session.emit("assistant.message", content="Yes, it is there.")
    await settle(runtime)
    await runtime.owner_send(harness.router_id, "What is going on?")
    sent = harness.session(harness.router_id).sent[-1]
    first, second = sent.split("And now?")
    assert "1 minute left" in first and "it is there" not in first
    assert "Yes, it is there." in second


def test_a_tool_call_with_leaked_parameter_markup_is_split_back():
    """Bug bash: after a restart a worker's report() failed three times: its status read
    'done</parameter>\n<parameter name="summary">Ran …' and summary was missing."""
    from chiefofstaff.models import leak_proof
    from chiefofstaff.tools import Report

    model = leak_proof(Report)
    assert model.model_json_schema() == Report.model_json_schema()
    found = model.model_validate(
        {"status": 'done</parameter>\n<parameter name="summary">Ran sleep 120.</parameter>'}
    )
    assert (found.status, found.summary) == ("done", "Ran sleep 120.")


def test_leaked_markup_at_the_end_of_a_call_and_leaked_lists_are_repaired():
    """Bug bash 3: a lone trailing </parameter>, one with </invoke> after it, and criteria that
    leaked as their JSON text all failed the report."""
    from chiefofstaff.models import leak_proof
    from chiefofstaff.tools import Report

    model = leak_proof(Report)
    found = model.model_validate({"status": "done", "summary": "Ran it.</parameter>"})
    assert found.summary == "Ran it."
    found = model.model_validate({"status": "done", "summary": "Ran it.</parameter>\n</invoke>"})
    assert found.summary == "Ran it."
    criteria = '[{"criterion": "tests pass", "result": "met", "evidence": "`pytest` passed"}]'
    found = model.model_validate(
        {"status": f'done</parameter>\n<parameter name="summary">Fixed.</parameter>\n'
                   f'<parameter name="criteria">{criteria}</parameter>\n</invoke>'}
    )  # fmt: skip
    assert (found.status, found.summary) == ("done", "Fixed.")
    assert [(c.criterion, c.result) for c in found.criteria] == [("tests pass", "met")]


async def test_a_task_keeps_the_pr_it_opened_after_its_worker_replies_on_it(harness):
    """GitHub bug bash 1: a reply drafted on its own PR made the task lose the PR (no Artifacts
    row, no Ready to merge card)."""
    runtime = await harness.connect()
    harness.gh_replies["auth status --json hosts"] = json.dumps(
        {"hosts": {"github.com": [{"login": "me", "state": "success", "active": True}]}}
    )
    harness.gh_replies["pr view https://github.com/acme/app/pull/7"] = pr_view()
    worker = await spawn(runtime, harness.router_id, "Fix login")
    task = runtime.tasks.current(worker)
    runtime.task_actions.record_opened(task["id"], ["https://github.com/acme/app/pull/7"])
    await call_tool(runtime, "draft_github_review", worker, repo="acme/app", number=7,
                    body="Fixed in abc.", in_reply_to=21)  # fmt: skip
    await call_tool(runtime, "report", worker, status="done",
                    summary="Opened https://github.com/acme/app/pull/7")  # fmt: skip
    await settle(runtime)
    [pr] = runtime.task_actions.view(runtime.tasks.get(task["id"]))["receipts"]["prs"]
    assert pr["own"] and not pr["reviewed"]
    assert [d for d in harness.store.drafts() if d["kind"] == "merge"], "offered to merge"


async def test_pr_news_goes_to_the_task_that_made_the_pr_and_names_it(harness):
    """GitHub bug bash 3/5: news on a PR reached every follower without saying which task it
    was about; a finished review on the same worker was reopened with it."""
    runtime = await harness.connect()
    maker = await spawn(runtime, harness.router_id, "Fix login")
    fix = runtime.tasks.current(maker)
    runtime.task_actions.record_opened(fix["id"], ["https://github.com/acme/app/pull/7"])
    runtime.follow(maker, ["github:acme/app#7"], task_id=fix["id"])
    told = await call_tool(runtime, "message_session", harness.router_id, session_id=maker,
                           task="new", title="Review PR acme/app#8", text="Review #8",
                           earlier="keep")  # fmt: skip
    assert told.result_type == "success", told.text_result_for_llm
    review = runtime.tasks.current(maker)
    assert review["id"] != fix["id"]
    await call_tool(runtime, "report", maker, status="done", summary="Reviewed.",
                    task_id=review["id"])  # fmt: skip
    await call_tool(runtime, "report", maker, status="done", summary="Fixed.", task_id=fix["id"])
    watcher = await spawn(runtime, harness.router_id, "Report on #7", why_new="separate")
    runtime.follow(watcher, ["github:acme/app#7"])
    await runtime.on_thread("github", "github:acme/app#7", ["k1"], "CI failed on #7")
    await settle(runtime)
    sent = harness.session(maker).sent[-1]
    assert 'type="thread"' in sent and f'task="{fix["id"]}"' in sent
    assert not any("CI failed on #7" in s for s in harness.session(watcher).sent), (
        "the task that made it acts on it; a worker only reporting on it is not asked too"
    )
    await call_tool(runtime, "report", maker, status="question", summary="CI failed: fix it?")
    assert runtime.tasks.get(fix["id"])["status"] == "waiting"
    assert runtime.tasks.get(review["id"])["status"] == "review", "not the other finished task"
    await call_tool(runtime, "report", maker, status="progress", summary="It passed on rerun.",
                    task_id=fix["id"])  # fmt: skip
    after = runtime.task_actions.view(runtime.tasks.get(fix["id"]))
    assert after["status"] == "review" and after["result"] == "Fixed.", (
        "back where it was, its result kept: not running with nothing at work"
    )
    assert after["news"]["text"] == "It passed on rerun."


async def test_links_a_report_made_and_named_and_one_cut_short(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Eval SLM")
    task = runtime.tasks.current(worker)
    doc = "https://docs.google.com/document/d/1SPkOjC0abcdef/edit"
    cut = "`[x](https://docs.google.com/\u2026)`"
    summary = f"Wrote [qwen3_8b SFT 11-05]({doc}).\nBackground: https://example.com/paper and {cut}"
    await call_tool(runtime, "report", worker, status="done", summary=summary)
    shown = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    assert shown["link_names"][doc] == "qwen3_8b SFT 11-05"
    assert shown["made_links"] == [doc]
    assert "https://example.com/paper" in shown["links"]
    assert not any(link.startswith("https://docs.google.com/\u2026") for link in shown["links"])
    assert "https://docs.google.com/" not in shown["links"]


async def test_a_report_task_records_what_the_owner_read_and_the_files_it_made(harness):
    runtime = await harness.connect()
    made = await call_tool(runtime, "spawn_worker", harness.router_id, title="Draw the mockup",
                           instructions="Make a mockup", why_new="test", review=True)  # fmt: skip
    worker = json.loads(made.text_result_for_llm)["session_id"]
    task = runtime.tasks.current(worker)
    folder = Path(harness.store.session(worker)["cwd"])
    folder.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    (folder / "mock.html").write_text("<p>mock</p>")
    harness.session(worker).emit("tool.execution_start", tool_call_id="w", tool_name="create",
                                 arguments={"path": str(folder / "mock.html")})  # fmt: skip
    await call_tool(runtime, "report", worker, status="done", summary="Mockup drawn.",
                    artifacts=[{"url": str(folder / "mock.html"), "name": "Home mockup",
                                "status": "draft"}])  # fmt: skip
    await settle(runtime)
    shown = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    assert shown["review_required"] and not shown["read"]
    assert [f["name"] for f in shown["files"]] == ["mock.html"]
    [art] = shown["artifacts"]
    assert art["url"].startswith("file://") and art["name"] == "Home mockup"
    assert runtime.task_actions.status_words(runtime.tasks.get(task["id"])) == "review, to accept"
    assert runtime.task_actions.mark_read(task["id"])["read"]
