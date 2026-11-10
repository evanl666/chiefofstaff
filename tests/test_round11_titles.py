"""C4: a new task's title is checked as a rename's or a split's is: it says what the task
delivers (an action and its target), not "Fix issue" or "111"."""

import json

import pytest
from conftest import call_tool

from chiefofstaff.task_actions import deliverable_title

pytestmark = pytest.mark.titles


@pytest.mark.parametrize(
    "bad",
    ["", "111", "#12", "A", "Task", "Task 0", "Fix issue", "Check PR", "Look into it",
     "Fix the bug", "\u54c8\u54c8"],
)  # fmt: skip
def test_a_title_that_names_nothing_is_refused(bad):
    with pytest.raises(ValueError, match=r"what it delivers|what it is about"):
        deliverable_title(bad)


@pytest.mark.parametrize(
    "good",
    [
        "Fix failing integration tests in the notification service",
        "Review authentication changes and verify test results",
        "Review PR #12",
        "Fix #12",
        "Update the docs",
        "Lint",  # The owner's own name for it: a word of what.
        "Fix CI",
        "\u4fee\u590d\u767b\u5f55\u65e5\u671f",
        "\u0418\u0441\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0432\u0445\u043e\u0434",
        "\u0625\u0635\u0644\u0627\u062d \u0627\u0644\u062f\u062e\u0648\u0644",
        "\u0e41\u0e01\u0e49\u0e44\u0e02\u0e01\u0e32\u0e23\u0e40\u0e02\u0e49\u0e32",
    ],
)
def test_a_title_that_says_what_it_delivers_is_kept(good):
    assert deliverable_title(good) == good


async def test_the_router_cannot_start_a_task_with_a_vague_title(harness):
    runtime = await harness.connect()
    refused = await call_tool(runtime, "spawn_worker", harness.router_id, title="Fix issue",
                              instructions="Fix it.", why_new="test")  # fmt: skip
    assert "does not say what it is about" in refused.text_result_for_llm
    assert not runtime.tasks.listed(), "refused before a task was made"
    started = await call_tool(runtime, "spawn_worker", harness.router_id,
                              title="Fix the flaky login test", instructions="Fix it.",
                              why_new="test")  # fmt: skip
    worker = json.loads(started.text_result_for_llm)["session_id"]
    again = await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                            task="new", why_new="test", earlier="keep", title="Check PR",
                            text="Check the PR.")  # fmt: skip
    assert "does not say what it is about" in again.text_result_for_llm
    assert len(runtime.tasks.for_session(worker)) == 1


async def test_rename_and_split_refuse_what_creating_refuses(harness):
    runtime = await harness.connect()
    started = await call_tool(runtime, "spawn_worker", harness.router_id,
                              title="Fix the flaky login test", instructions="Fix it.",
                              why_new="test")  # fmt: skip
    task = runtime.tasks.current(json.loads(started.text_result_for_llm)["session_id"])
    with pytest.raises(ValueError, match="does not say what it is about"):
        runtime.task_actions.rename(task["id"], "Fix issue")
    with pytest.raises(ValueError, match="does not say what it is about"):
        runtime.task_actions.split_off(task, "Check PR")


async def test_a_refused_title_does_not_reopen_a_closed_worker(harness):
    runtime = await harness.connect()
    started = await call_tool(runtime, "spawn_worker", harness.router_id,
                              title="Fix the flaky login test", instructions="Fix it.",
                              why_new="test")  # fmt: skip
    worker = json.loads(started.text_result_for_llm)["session_id"]
    await call_tool(runtime, "report", worker, status="done", summary="Fixed.")
    await runtime.workers.close(worker)
    refused = await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                              task="new", why_new="test", earlier="keep", title="Check PR",
                              text="Check the PR.")  # fmt: skip
    assert "does not say what it is about" in refused.text_result_for_llm
    assert harness.store.session(worker)["closed_at"], "still closed"
