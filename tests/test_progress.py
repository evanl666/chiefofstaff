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
from conftest import FakeSession, Harness, call_tool, env, settle

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
    worker = await spawn(runtime, harness.router_id, "Review checkout-service PR #1529")
    repo = "acme/checkout-service"
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
    await runtime.owner_send(harness.router_id, "Which workers are open right now?")
    assert 'reply_in="English"' in router.sent[-1], "not the language of the conversation so far"
    assert "answer in English, not Chinese" in router.sent[-1], "said outright when it switches"
    worker = await spawn(runtime, harness.router_id, "Count the files")
    assert 'reply_in="English"' in harness.session(worker).sent[-1], "its report is read as is"
    chinese_task = await spawn(runtime, harness.router_id, "数一下有几个文件")
    assert "Write your report in English" in harness.session(chinese_task).sent[-1]
    await runtime.owner_send(harness.router_id, "progress?")
    assert "reply_in" not in router.sent[-1], "no signal: the conversation's language"
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
