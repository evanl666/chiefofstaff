"""E115: answers and recent progress reports are never lost."""

from datetime import UTC, datetime, timedelta

from conftest import call_tool, settle
from test_progress import step
from test_round10_lifecycle import spawn, view
from test_round12_checkbacks import turn


def router_reports(harness) -> list[dict]:
    return [
        message
        for message in harness.store.messages(harness.router_id)
        if message["kind"] == "inbound" and message["meta"].get("status")
    ]


def worker_reports(harness, worker: str) -> list[dict]:
    return [message for message in harness.store.messages(worker) if message["kind"] == "report"]


async def test_a_router_continue_answer_reported_as_progress_reaches_home(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Count the rows")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(
        runtime,
        "message_session",
        harness.router_id,
        session_id=worker,
        task="continue",
        task_id=task["id"],
        text="How many rows are there now?",
    )
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
    assert "Answer/Update" in reply.text_result_for_llm


async def test_an_owner_chat_answer_reported_as_progress_reaches_home(harness):
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

    told = router_reports(harness)[-1]
    assert told["meta"]["status"] == "answer"
    assert told["meta"]["task_id"] == task["id"]
    assert told["text"] == "The rejected rows are in export_rejections."
    assert worker_reports(harness, worker)[-1]["meta"]["told"] is True


async def test_a_check_back_progress_report_stays_quiet(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Watch the export")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(
        runtime,
        "check_back",
        worker,
        task_id=task["id"],
        minutes=1,
        what="whether the export finished",
    )
    await runtime.task_actions.wake_check_backs(datetime.now(UTC) + timedelta(minutes=2))
    await settle(runtime)
    await turn(runtime, session)
    before = len(router_reports(harness))

    reply = await call_tool(
        runtime, "report", worker, status="progress", summary="The export is still running."
    )

    assert len(router_reports(harness)) == before
    assert "task card" in reply.text_result_for_llm
    assert 'status="answer"' in reply.text_result_for_llm
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
    assert "session and on the task card" in reply.text_result_for_llm
    assert 'status="answer"' in reply.text_result_for_llm
    assert not worker_reports(harness, worker)[-1]["meta"].get("told")


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
    await call_tool(
        runtime,
        "message_session",
        harness.router_id,
        session_id=worker,
        task="continue",
        task_id=task["id"],
        text="How many retries succeeded?",
    )
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
