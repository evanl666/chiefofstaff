"""E115: answers and recent progress reports are never lost."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import Harness, call_tool, settle
from test_progress import step
from test_round5_results import RESULT, commit, ended, published
from test_round10_lifecycle import spawn, view
from test_round12_checkbacks import idle, turn

from chiefofstaff.envelope import wrap
from chiefofstaff.models import Settings


def router_reports(harness) -> list[dict]:
    return [
        message
        for message in harness.store.messages(harness.router_id)
        if message["kind"] == "inbound" and message["meta"].get("status")
    ]


def worker_reports(harness, worker: str) -> list[dict]:
    return [message for message in harness.store.messages(worker) if message["kind"] == "report"]


async def continued(runtime, harness, worker: str, task: dict, text: str, how="continue"):
    """The Router's message to a worker at work on `task`."""
    await call_tool(
        runtime,
        "message_session",
        harness.router_id,
        session_id=worker,
        task=how,
        task_id=task["id"],
        text=text,
    )


async def test_a_router_continue_answer_reported_as_progress_reaches_home(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Count the rows")
    session = harness.session(worker)
    await turn(runtime, session)
    await continued(runtime, harness, worker, task, "How many rows are there now?")
    await turn(runtime, session)

    reply = await call_tool(
        runtime, "report", worker, status="progress", summary="There are 1,240 rows."
    )

    told = router_reports(harness)[-1]
    assert told["meta"]["status"] == "answer"
    assert told["meta"]["task_id"] == task["id"]
    assert told["text"] == "There are 1,240 rows."
    assert worker_reports(harness, worker)[-1]["meta"]["told"] is True
    assert runtime.tasks.get(task["id"])["status"] == "running"
    assert 'report(status="answer")' in reply.text_result_for_llm


async def test_an_owner_chat_answer_reported_as_progress_reaches_home_once(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Inspect the export")
    session = harness.session(worker)
    await turn(runtime, session)
    await runtime.owner_send_to(
        harness.router_id, worker, "Which table contains the rejected rows?"
    )
    await turn(runtime, session)

    await call_tool(
        runtime,
        "report",
        worker,
        status="progress",
        summary="The rejected rows are in export_rejections.",
    )
    await idle(runtime, session)

    told = router_reports(harness)[-1]
    assert told["meta"]["status"] == "answer"
    assert told["meta"]["task_id"] == task["id"]
    assert told["text"] == "The rejected rows are in export_rejections."
    assert worker_reports(harness, worker)[-1]["meta"]["told"] is True
    home = harness.store.messages(harness.router_id)
    assert not [m for m in home if m["kind"] == "answer"], "not shown on Home a second time"


async def test_a_check_back_progress_report_stays_quiet(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Watch the export")
    session = harness.session(worker)
    await turn(runtime, session)
    await continued(runtime, harness, worker, task, "Is the export still on?", how="note")
    await turn(runtime, session)
    await call_tool(
        runtime,
        "check_back",
        worker,
        task_id=task["id"],
        minutes=1,
        what="whether the export finished",
    )
    await idle(runtime, session)
    await runtime.task_actions.wake_check_backs(datetime.now(UTC) + timedelta(minutes=2))
    await settle(runtime)
    await turn(runtime, session)
    before = len(router_reports(harness))

    reply = await call_tool(
        runtime, "report", worker, status="progress", summary="The export is still running."
    )

    assert len(router_reports(harness)) == before
    assert "the Router is not told" in reply.text_result_for_llm
    assert not worker_reports(harness, worker)[-1]["meta"].get("told")


async def test_routine_progress_stays_quiet_and_says_where_it_goes(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Index the documents")
    session = harness.session(worker)
    await turn(runtime, session)
    before = len(router_reports(harness))

    reply = await call_tool(
        runtime, "report", worker, status="progress", summary="Indexed 400 of 900 documents."
    )

    assert len(router_reports(harness)) == before
    assert runtime.tasks.get(task["id"])["progress"] == "Indexed 400 of 900 documents."
    said = reply.text_result_for_llm
    assert "on the task's card" in said and "the Router is not told" in said
    assert 'report(status="answer")' in said
    assert not worker_reports(harness, worker)[-1]["meta"].get("told")


async def test_a_router_note_not_seen_yet_is_not_what_progress_answers(harness):
    """Sent into its turn, a note it has not been shown yet: what it reports is about the work
    before it. Once its conversation shows the note, its progress answers it."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Scan the logs")
    session = harness.session(worker)
    await turn(runtime, session)
    await continued(runtime, harness, worker, task, "How many errors so far?", how="note")
    before = len(router_reports(harness))

    await call_tool(runtime, "report", worker, status="progress", summary="Scanned 3 of 9 days.")
    assert len(router_reports(harness)) == before

    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="progress", summary="17 errors so far.")
    assert router_reports(harness)[-1]["text"] == "17 errors so far."
    assert router_reports(harness)[-1]["meta"]["status"] == "answer"


async def test_answer_never_changes_the_task_and_is_told_once(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Measure the import")
    await call_tool(
        runtime, "report", worker, status="progress", summary="Measured the first shard."
    )
    before = runtime.tasks.get(task["id"])
    notifications = len(harness.notifications)

    reply = await call_tool(
        runtime,
        "report",
        worker,
        status="answer",
        task_id=task["id"],
        summary="The first shard took 18 seconds.",
    )

    assert reply.result_type == "success"
    assert "Answer/Update" in reply.text_result_for_llm
    after = runtime.tasks.get(task["id"])
    assert {key: after[key] for key in ("status", "result", "progress", "updated_at")} == {
        key: before[key] for key in ("status", "result", "progress", "updated_at")
    }
    told = router_reports(harness)
    assert len([message for message in told if message["text"].startswith("The first shard")]) == 1
    assert told[-1]["meta"]["status"] == "answer"
    assert told[-1]["meta"]["task_id"] == task["id"]
    assert worker_reports(harness, worker)[-1]["meta"]["told"] is True
    assert len(harness.notifications) <= notifications + 1
    assert [u["text"] for u in view(runtime, task["id"])["progress_updates"]] == [
        "Measured the first shard."
    ], "an answer is no progress"


async def test_an_answer_leaves_finished_and_waiting_work_as_it_was(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Export the report")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="done", summary="Exported to out.csv.")
    await idle(runtime, session)
    finished = runtime.tasks.get(task["id"])
    await continued(runtime, harness, worker, task, "Which delimiter did you use?", how="note")
    await turn(runtime, session)

    await call_tool(runtime, "report", worker, status="answer", summary="A comma.")
    await idle(runtime, session)

    now = runtime.tasks.get(task["id"])
    assert (now["status"], now["result"], now["progress"]) == (
        finished["status"],
        finished["result"],
        finished["progress"],
    )
    assert harness.store.session(worker)["status"] == "done", "back where it was"
    assert router_reports(harness)[-1]["meta"]["status"] == "answer"

    other, waiting = await spawn(runtime, harness.router_id, "Pick a colour")
    await call_tool(runtime, "report", other, status="question", summary="Red or blue?")
    await call_tool(runtime, "report", other, status="answer", summary="I lean to blue.")
    assert runtime.tasks.get(waiting["id"])["status"] == "waiting"
    assert runtime.asks.session_questions(other), "its question stays open"


async def test_an_answer_as_progress_keeps_why_a_task_failed(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Restore the backup")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="failed", summary="The bucket is gone.")
    await idle(runtime, session)
    await continued(runtime, harness, worker, task, "When was it deleted?", how="note")
    await turn(runtime, session)

    await call_tool(runtime, "report", worker, status="progress", summary="On Monday.")

    assert router_reports(harness)[-1]["meta"]["status"] == "answer"
    failed = runtime.tasks.get(task["id"])
    assert (failed["status"], failed["progress"]) == ("failed", "The bucket is gone.")


async def test_an_answer_on_a_running_task_with_nothing_to_wake_it_asks_what_next(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Train the model")
    session = harness.session(worker)
    await turn(runtime, session)
    await continued(runtime, harness, worker, task, "Which learning rate?")
    await turn(runtime, session)

    await call_tool(runtime, "report", worker, status="answer", summary="0.001.")
    await idle(runtime, session)

    assert 'type="wait-missing"' in session.sent[-1]
    assert "after an answer" in session.sent[-1]


async def test_task_api_keeps_the_last_five_progress_reports(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Import the archive")
    for number in range(1, 7):
        await call_tool(
            runtime,
            "report",
            worker,
            status="progress",
            summary=f"Meaningful update {number}.",
        )
    # The same words again are one update.
    await call_tool(runtime, "report", worker, status="progress", summary="Meaningful update 6.")

    stored = runtime.tasks.get(task["id"])
    shown = view(runtime, task["id"])
    assert stored["progress"] == "Meaningful update 6."
    assert [item["text"] for item in shown["progress_updates"]] == [
        "Meaningful update 6.",
        "Meaningful update 5.",
        "Meaningful update 4.",
        "Meaningful update 3.",
        "Meaningful update 2.",
    ]
    assert all(item["at"] for item in shown["progress_updates"])


async def test_a_retry_keeps_the_router_continue_as_the_answer_instruction(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Count the retries")
    session = harness.session(worker)
    await turn(runtime, session)
    await continued(runtime, harness, worker, task, "How many retries succeeded?")
    await turn(runtime, session)
    step(session, "bash", {"command": "git log -1 --stat"})
    session.emit("session.error", message="Execution failed: 503 Service Unavailable")
    await settle(runtime)
    await settle(runtime)
    assert "finish what is left of it" in session.sent[-1]
    await turn(runtime, session)

    await call_tool(
        runtime, "report", worker, status="progress", summary="Three retries succeeded."
    )

    told = router_reports(harness)[-1]
    assert told["meta"]["status"] == "answer"
    assert told["meta"]["task_id"] == task["id"]
    assert told["text"] == "Three retries succeeded."


async def test_a_restart_keeps_the_router_continue_as_the_answer_instruction(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Count the users")
    session = harness.session(worker)
    await turn(runtime, session)
    await continued(runtime, harness, worker, task, "How many users signed up today?")
    await turn(runtime, session)
    note = "Chief of Staff restarted while you were working. Continue where you left off."
    await runtime.delivery.deliver(worker, wrap("system", "restart", note), kind="notice")
    await turn(runtime, session)

    await call_tool(runtime, "report", worker, status="progress", summary="42 signed up today.")

    assert router_reports(harness)[-1]["text"] == "42 signed up today."
    assert router_reports(harness)[-1]["meta"]["status"] == "answer"


async def test_changed_code_after_a_result_is_not_taken_for_an_answer(tmp_path):
    """Changed in the owner's chat after its result, its code is a new result to review: told as
    progress, it is asked for that result, as before; answered, it is asked once its turn ends."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    made = await call_tool(runtime, "spawn_worker", harness.router_id, title="Calc",
                           instructions="x", project=str(app), isolate=True,
                           why_new="test")  # fmt: skip
    worker = json.loads(made.text_result_for_llm)
    session_id, task, tree = worker["session_id"], worker["task_id"], Path(worker["cwd"])
    session = harness.session(session_id)
    commit(tree, "calc.py", "def add(a, b):\n    return a + b\n")
    await call_tool(runtime, "report", session_id, status="done", summary=RESULT)
    await ended(runtime, task)
    await idle(runtime, session)
    await runtime.owner_send(session_id, "Add type hints to add, please.")
    await turn(runtime, session)
    commit(tree, "calc.py", "def add(a: int, b: int) -> int:\n    return a + b\n")

    refused = await call_tool(runtime, "report", session_id, status="progress",
                              summary="Added the type hints.")  # fmt: skip
    assert refused.result_type == "failure"
    assert "new result for the owner to review" in refused.text_result_for_llm

    answered = await call_tool(runtime, "report", session_id, status="answer",
                               summary="Added them.")  # fmt: skip
    assert answered.result_type == "success"
    await idle(runtime, session)
    assert "You changed files for task" in session.sent[-1]
