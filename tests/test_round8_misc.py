"""Bug bash round 8: a test run in which no test ran, a stopped PR follow, "new because" as
the owner reads it, and habits waiting on their card."""

import json
from types import SimpleNamespace

import pytest
from conftest import call_tool, settle

from chiefofstaff import receipts
from chiefofstaff.prompts import worker_context
from chiefofstaff.tools import mid_sentence

MISSING = (
    "ERROR: file or directory not found: tests/test_does_not_exist.py\n\n"
    "no tests ran in 0.00s\n<exited with exit code 0>"
)
PIPED = "uv run --with pytest pytest tests/test_does_not_exist.py 2>&1 | tail -20"


async def spawn(runtime, router, title, **extra):
    extra.setdefault("instructions", title)
    extra.setdefault("why_new", "test")
    result = await call_tool(runtime, "spawn_worker", router, title=title, **extra)
    return json.loads(result.text_result_for_llm)


def run(session, call, command, output):
    session.emit("tool.execution_start", tool_name="bash", arguments={"command": command},
                 tool_call_id=call)  # fmt: skip
    session.emit("tool.execution_complete", tool_call_id=call, success=True,
                 result=SimpleNamespace(content=output))  # fmt: skip


async def test_a_run_in_which_no_test_ran_is_no_pass(harness):
    """U2: a green "tests" chip for `pytest <missing file> | tail -20`: "no tests ran", tail's 0."""
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Fix")
    run(harness.session(made["session_id"]), "c1", PIPED, MISSING)
    await settle(runtime)
    found = runtime.task_actions.receipts(runtime.tasks.get(made["task_id"]))["tests"]
    assert found["passed"] is False and found["exit_code"] != 0, found


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (MISSING, "none"),
        ("Ran 0 tests in 0.000s\n\nOK", "none"),
        ("Ran 0 tests in 0.000s\n\nNO TESTS RAN", "none"),
        (
            "No tests found, exiting with code 1\nRun with `--passWithNoTests` to exit with 0",
            "none",
        ),
        ("No test files found, exiting with code 1", "none"),
        ("?   \tacme/cmd\t[no test files]", "none"),
        ("ok  \tacme/pkg\t0.01s [no tests to run]", "none"),
        ("===== 5 deselected in 0.01s =====", "none"),
        # One package without tests among others that ran theirs: they ran.
        ("?   \tacme/cmd\t[no test files]\nok  \tacme/pkg\t0.012s", "passed"),
        ("Ran 5 tests in 0.010s\n\nOK", "passed"),
        ("3 passed, 2 deselected in 0.1s", "passed"),
        ("ERROR tests/test_x.py\n!!!! Interrupted: 1 error during collection !!!!", "failed"),
    ],
)
def test_runs_in_which_no_test_ran(output, expected):
    assert receipts.last_summary(output) == expected


def test_a_piped_test_run_passes_only_by_a_summary_it_shows():
    assert receipts.test_exit(PIPED, MISSING, 0) == receipts.NO_TESTS_RAN
    assert receipts.test_exit(PIPED, ".....\n5 passed in 0.1s\n<exited with exit code 0>", 0) == 0
    cut = "tests/test_a.py ....\ntests/test_b.py ..\n<exited with exit code 0>"
    assert receipts.test_exit("uv run pytest -v 2>&1 | head -2", cut, 0) is None, "cut: unknown"
    assert receipts.test_exit("uv run pytest -v", cut, 0) == 0, "not piped: its own exit code"
    assert receipts.test_exit("bash -c 'pytest -v | head -2'", cut, 0) is None
    assert receipts.test_exit("echo y | pytest -v", cut, 0) == 0, "piped into the tests"
    # Nothing of its own seen yet (Copilot's notice that it ended): its exit code for now.
    assert receipts.test_exit(PIPED, "<command started in background with shellId: 5>", 0) == 0


def test_a_claimed_pass_with_no_test_result_is_flagged():
    ended = [{"command": "pytest -v | head -2", "kind": "test", "exit_code": None,
              "ended_at": "2026-11-08T10:00:00+00:00"}]  # fmt: skip
    found = receipts.summarize(ended, None, "All tests pass.")
    assert found["tests"] is None
    assert found["flags"] == ["Says tests pass, but no test run showed whether they pass."]


async def test_stopping_a_pr_follow_says_nobody_reads_it_now(harness):
    """R6: after "stop following PR #89", the Router said its CI and reviews now came to it:
    nothing reads a PR nobody follows."""
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Lerp")
    worker = made["session_id"]
    runtime.follow(worker, ["github:acme/app#89"], task_id=made["task_id"])
    result = await call_tool(runtime, "stop_following", harness.router_id, session_id=worker,
                             thread="github:acme/app#89")  # fmt: skip
    text = result.text_result_for_llm
    assert "Nobody follows it now" in text and "reach no one" in text, text
    assert "come to you" not in text, text

    other = await spawn(runtime, harness.router_id, "Review")
    for session_id in (worker, other["session_id"]):
        runtime.follow(session_id, ["github:acme/app#90"])
    result = await call_tool(runtime, "stop_following", harness.router_id, session_id=worker,
                             thread="github:acme/app#90")  # fmt: skip
    text = result.text_result_for_llm
    assert "\u201cReview\u201d still follows it" in text and "Nobody" not in text, text


async def test_stopping_a_slack_follow_still_says_its_messages_come_to_the_router(harness):
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Answer Bob")
    worker = made["session_id"]
    key = "slack:C0123ABCD:1700000000.000100"
    harness.store.follow(key, worker)
    result = await call_tool(runtime, "stop_following", harness.router_id, session_id=worker,
                             thread=key)  # fmt: skip
    assert "new messages there come to you" in result.text_result_for_llm


@pytest.mark.parametrize(
    ("why", "expected"),
    [
        ("Long Count is busy with the parser.", "Long Count is busy with the parser"),
        ("Alice asked for it on its own.", "Alice asked for it on its own"),
        ("The task is busy.", "the task is busy"),
        ("Different subject than the other workers.", "different subject than the other workers"),
        ("The PR is merged already", "the PR is merged already"),
        ("PR of its own, as you asked.", "PR of its own, as you asked"),
        ("I need a clean branch", "I need a clean branch"),
        ("The Long Count task is busy", "The Long Count task is busy"),
    ],
)
def test_new_because_keeps_names_and_titles(why, expected):
    """R12: "new because long Count is busy…" (the task is "Long Count")."""
    assert mid_sentence(why) == expected


def test_new_because_keeps_a_title_that_starts_as_a_sentence_does():
    assert mid_sentence("Different strokes is busy", ["Different strokes"]) == (
        "Different strokes is busy"
    )


async def test_new_because_in_english_is_not_shown_to_an_owner_who_writes_chinese(harness):
    """U11: a Chinese request's notice read "new because separate decision task, no code
    change": English, and the Router's reasoning."""
    runtime = await harness.connect()
    harness.store.set("owner_language", "Chinese")
    await spawn(runtime, harness.router_id, "决定发布日期",
                why_new="separate decision task, no code change")  # fmt: skip
    await spawn(runtime, harness.router_id, "整理会议纪要", why_new="你要单独的一份纪要")
    notices = [m["text"] for m in harness.store.messages(harness.router_id)
               if m["kind"] == "notice"]  # fmt: skip
    assert notices[-2] == "Started “决定发布日期”", notices
    assert notices[-1] == "Started “整理会议纪要” · new because 你要单独的一份纪要", notices
    harness.store.set("owner_language", "English")
    await spawn(runtime, harness.router_id, "Fix the parser", why_new="A different repository.")
    notices = [m["text"] for m in harness.store.messages(harness.router_id)
               if m["kind"] == "notice"]  # fmt: skip
    assert notices[-1] == "Started “Fix the parser” · new because a different repository"


def test_why_new_asks_for_the_owners_language_addressed_to_them():
    from chiefofstaff.tools import MessageSession, SpawnWorker

    for model in (SpawnWorker, MessageSession):
        said = model.model_fields["why_new"].description or ""
        assert "Chinese" in said and "not shown" in said and "never your reasoning" in said


@pytest.mark.parametrize(("count", "said"), [(1, "1 new habit waits"), (3, "3 new habits wait")])
async def test_habits_waiting_are_counted_and_found_on_home(harness, count, said):
    """U12: Settings said "3 habit(s) wait for your OK." above "Habits · 0", with nothing to
    approve there."""
    runtime = await harness.connect()
    card = {"id": "d1", "target": {"lessons": [{"id": str(n)} for n in range(count)]}}
    runtime.learning.pending_card = lambda: card
    runtime.habits.summarize("learner")
    text = runtime.habits.view()["last"]["text"]
    assert text == f"{said} for your OK under Needs you on Home.", text
    harness.store.set("learned_styles:learner", json.dumps({"chat": 2}))
    runtime.habits.summarize("learner")
    text = runtime.habits.view()["last"]["text"]
    assert text.endswith(f"It applies now; {said} for your OK under Needs you on Home."), text
    runtime.learning.pending_card = lambda: None  # Kept or dropped on Home.
    assert runtime.habits.view()["last"]["text"] == (
        "Learned how you write: Answers and reports to you (v2). It applies now."
    )


def test_a_worker_in_the_owners_folder_runs_nothing_there_that_writes_files(tmp_path):
    """Hunter: a pytest run by a worker on a task that changes no code left .venv,
    .pytest_cache and uv.lock in the owner's checkout."""
    checkout = {"path": str(tmp_path), "branch": "", "shared": True, "git_dir": str(tmp_path)}
    text = worker_context(str(tmp_path), str(tmp_path / "ws"), [checkout], yolo=False)
    assert "run nothing there that writes files" in text and "uv.lock" in text, text
