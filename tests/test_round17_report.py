"""E115: answers and recent progress reports are never lost."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import Harness, call_tool, settle
from test_progress import step
from test_round5_results import RESULT, commit, ended, published
from test_round10_lifecycle import spawn, view
from test_round12_checkbacks import checking, idle, turn, woken

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


async def test_progress_after_its_done_report_is_no_answer(harness):
    """Its continue was answered by its done report: progress on that task later (woken by a
    command it left running, say) is its own news, not that answer again."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Tidy the schema")
    session = harness.session(worker)
    await turn(runtime, session)
    await continued(runtime, harness, worker, task, "Also drop the unused table.")
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="done", summary="Dropped `old_users`.")
    before = len(router_reports(harness))

    await call_tool(runtime, "report", worker, status="progress", summary="Vacuum finished.")

    assert len(router_reports(harness)) == before


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
        minutes=5,
        what="whether the export finished",
    )
    await idle(runtime, session)
    await runtime.task_actions.wake_check_backs(datetime.now(UTC) + timedelta(minutes=6))
    await settle(runtime)
    assert 'type="check-back"' in session.sent[-1], "woken by its check-back"
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
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(
        runtime, "report", worker, status="progress", summary="Measured the first shard."
    )
    await continued(runtime, harness, worker, task, "How long did it take?", how="note")
    await turn(runtime, session)
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


PR = "github:acme/app#7"


async def accepted_with_news(runtime, harness, title: str):
    """A worker that follows a PR for a task it reported and the owner accepted; the PR's CI
    has just failed (live G2), and its turn on that news has begun."""
    worker, task = await spawn(runtime, harness.router_id, title, follow=[PR])
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="done", summary="Validate passes on #7.")
    await idle(runtime, session)
    runtime.task_actions.accept(task["id"])
    await runtime.on_thread("github", PR, ["github:check:1"], "- CI on 03b9393: failed: validate")
    await settle(runtime)
    assert "failed: validate" in session.sent[-1]
    await turn(runtime, session)
    return worker, task, session


@pytest.mark.parametrize("status", ["answer", "progress"])
async def test_news_a_thread_brought_stays_in_sight_whatever_status_says_it(harness, status):
    """Live G2 (r70): a followed PR's CI failed after its task was accepted; its worker said
    so with status="answer" (the owner had asked to be told), which changed nothing on the
    task: the news never came under Needs you. What a thread's news brings is news on the
    task it is for, an answer or not."""
    runtime = await harness.connect()
    worker, task, _ = await accepted_with_news(runtime, harness, "Follow the CI of #7")
    alerts = len(harness.notifications)

    await call_tool(runtime, "report", worker, status=status, task_id=task["id"],
                    summary="CI changed on #7: validate now fails on 03b9393.")  # fmt: skip
    await settle(runtime)

    shown = view(runtime, task["id"])
    assert shown["status"] == "done"
    assert shown["news"] and "validate now fails" in shown["news"]["text"]
    assert shown["attention"] == "update"
    told = [m for m in router_reports(harness) if "validate now fails" in m["text"]]
    assert len(told) == 1 and told[0]["meta"]["task_id"] == task["id"]
    assert len(harness.notifications) == alerts + 1, "one PR update"


async def test_an_answer_on_a_thread_turn_is_news_on_a_result_to_review(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Open the fix PR", follow=[PR])
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="done", summary="Opened #7.")
    await idle(runtime, session)
    await runtime.on_thread("github", PR, ["github:review:1"], "bob approved #7")
    await settle(runtime)
    await turn(runtime, session)

    await call_tool(runtime, "report", worker, status="answer", task_id=task["id"],
                    summary="Bob approved #7.")  # fmt: skip

    shown = view(runtime, task["id"])
    assert (shown["status"], shown["result"]) == ("review", "Opened #7.")
    assert shown["news"] and "Bob approved" in shown["news"]["text"]
    assert len([m for m in router_reports(harness) if m["text"] == "Bob approved #7."]) == 1


async def test_an_answer_on_a_thread_turn_is_progress_on_work_still_running(harness):
    """At work still, a thread's news is its progress, on its card (its result comes with its
    done report), as a progress report there is."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Fix the CI of #7", follow=[PR])
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="progress", summary="Pushed a fix.")
    await idle(runtime, session)
    await runtime.on_thread("github", PR, ["github:check:2"], "- CI on 1a2b3c4: failed: lint")
    await settle(runtime)
    await turn(runtime, session)
    before = len(router_reports(harness))

    reply = await call_tool(runtime, "report", worker, status="answer", task_id=task["id"],
                            summary="Lint fails on 1a2b3c4.")  # fmt: skip

    shown = view(runtime, task["id"])
    assert (shown["status"], shown["progress"]) == ("running", "Lint fails on 1a2b3c4.")
    assert shown["progress_updates"][0]["text"] == "Lint fails on 1a2b3c4."
    assert len(router_reports(harness)) == before
    assert "news on its task" in reply.text_result_for_llm


async def test_an_answer_on_a_check_back_is_news_on_the_accepted_task(harness):
    runtime, worker, a, _, session = await checking(harness, with_b=False)
    await woken(runtime, session)

    await call_tool(runtime, "report", worker, status="answer", task_id=a["id"],
                    summary="The nightly export failed: the bucket is gone.")  # fmt: skip

    shown = view(runtime, a["id"])
    assert shown["status"] == "done"
    assert shown["news"] and "bucket is gone" in shown["news"]["text"]


@pytest.mark.parametrize("status", ["answer", "progress"])
async def test_a_peer_waits_and_hands_over_with_the_router_told_no_more(harness, status):
    """Live E3 (r71): the G2 rule (a check-back's or thread's news is news on its task) tells
    the Router nothing more in a hand-over between peers: the maker's answer after its tell
    reaches it once (its result, C3); the writer, waiting with a check-back and woken without
    the name, says so on its card only (an answer there reached the Router before), and
    neither turn is a thread's."""
    runtime = await harness.connect()
    maker, made = await spawn(runtime, harness.router_id, "Column Maker")
    writer, wanted = await spawn(runtime, harness.router_id, "Query Writer",
                                 works_with=[made["id"]])  # fmt: skip
    writing = harness.session(writer)
    await turn(runtime, writing)
    await call_tool(runtime, "check_back", writer, task_id=wanted["id"], minutes=5,
                    what="whether Column Maker sent the column name")  # fmt: skip
    await idle(runtime, writing, "我在等 Column Maker 告诉我列名。")
    await runtime.task_actions.wake_check_backs(datetime.now(UTC) + timedelta(minutes=6))
    await settle(runtime)
    assert 'type="check-back"' in writing.sent[-1], "woken by its check-back"
    await turn(runtime, writing)
    before = len(router_reports(harness))

    await call_tool(runtime, "report", writer, status=status, task_id=wanted["id"],
                    summary="还没有收到 Column Maker 的列名。")  # fmt: skip

    assert len(router_reports(harness)) == before, "the Router hears of no wait"
    assert runtime.tasks.get(wanted["id"])["status"] == "running"

    making = harness.session(maker)
    await turn(runtime, making)
    await call_tool(runtime, "tell_session", maker, session_id=f"worker:{writer[:8]}",
                    text="列名是 col_377a85。")  # fmt: skip
    assert not runtime.states[writer].thread_turn, "a peer's note is no thread's news"
    await call_tool(runtime, "report", maker, status="answer", task_id=made["id"],
                    summary="列名 col_377a85 已直接告知 Query Writer。")  # fmt: skip
    told = [m for m in router_reports(harness) if m["meta"]["from"] == maker]
    assert [(m["meta"]["status"], m["meta"]["task_id"]) for m in told] == [("done", made["id"])]
    assert runtime.tasks.get(made["id"])["status"] == "review", "its answer is its result (C3)"


# Live C3 (r72): an answer to the task a worker was given is that task's result.


async def test_an_answer_to_its_own_task_is_its_result(harness):
    """Asked in its task's own instructions to report with status="answer" (as the Router's
    prompt had it ask for an update), a Codex worker did, and its task never finished: the
    Router waited for a result that had come. Nothing else was asked of it since its task (a
    peer's message is no question of the Router's or the owner's): it is its result."""
    runtime = await harness.connect()
    waiter, waiting = await spawn(
        runtime, harness.router_id, "Waiter",
        instructions="Run `sleep 90` and let it finish. Then report, word for word, any "
        'messages other workers sent you while you waited, with report(status="answer").',
    )  # fmt: skip
    teller, _ = await spawn(runtime, harness.router_id, "Tell Waiter the number")
    session = harness.session(waiter)
    await turn(runtime, session)
    await call_tool(runtime, "tell_session", teller, session_id=f"worker:{waiter[:8]}",
                    text="the number is 777")  # fmt: skip
    await turn(runtime, session)
    finished = len(harness.notifications)

    reply = await call_tool(runtime, "report", waiter, status="answer",
                            summary="the number is 777")  # fmt: skip
    await settle(runtime)

    shown = view(runtime, waiting["id"])
    assert (shown["status"], shown["result"]) == ("review", "the number is 777")
    told = [m for m in router_reports(harness) if m["meta"]["from"] == waiter]
    assert [m["meta"]["status"] for m in told] == ["done"]
    assert worker_reports(harness, waiter)[-1]["meta"]["status"] == "done"
    assert len(harness.notifications) == finished + 1, "its one “finished”"
    assert 'report(status="done")' in reply.text_result_for_llm
    assert f"result of task {waiting['id']}" in reply.text_result_for_llm


async def test_an_answer_to_a_task_made_in_its_chat_is_its_result(harness):
    """The owner's message to a worker about none of its tasks is a task of its own: its
    answer is that task's result too."""
    runtime = await harness.connect()
    worker, first = await spawn(runtime, harness.router_id, "Tidy the README")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="done", summary="Tidied the README.")
    await idle(runtime, session)
    second, _ = await spawn(runtime, harness.router_id, "Bump the version")
    await turn(runtime, harness.session(second))
    await call_tool(runtime, "report", second, status="done", summary="Bumped to 2.1.")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    task="new", why_new="test", earlier="keep", title="Lint the docs",
                    text="Lint the docs.")  # fmt: skip
    linting = runtime.tasks.current(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="done", task_id=linting["id"],
                    summary="Docs lint clean.")  # fmt: skip
    await idle(runtime, session)
    await runtime.owner_send(worker, "Which Python version does CI use?")
    made = runtime.tasks.current(worker)
    assert made["id"] not in {first["id"], linting["id"]} and made["status"] == "running"
    await turn(runtime, session)

    await call_tool(runtime, "report", worker, status="answer", task_id=made["id"],
                    summary="CI uses Python 3.12.")  # fmt: skip

    assert runtime.tasks.get(made["id"])["status"] == "review"


async def test_an_answer_after_the_results_it_waited_for_is_its_result(harness):
    """The results of the tasks it waited for come after its task: they ask nothing of it."""
    runtime = await harness.connect()
    first, schema = await spawn(runtime, harness.router_id, "Write the schema")
    backfill, later = await spawn(runtime, harness.router_id, "Count the rows to backfill",
                                  after=[schema["id"]])  # fmt: skip
    await call_tool(runtime, "report", first, status="done", summary="Schema written.")
    await settle(runtime)
    await runtime.workers.pump()
    await settle(runtime)
    session = harness.session(backfill)
    assert 'type="results"' in session.sent[-1] or 'type="results"' in "".join(session.sent)
    await turn(runtime, session)

    await call_tool(runtime, "report", backfill, status="answer", task_id=later["id"],
                    summary="1,204 rows need a backfill.")  # fmt: skip

    assert runtime.tasks.get(later["id"])["status"] == "review"


async def test_an_answer_to_a_question_about_its_task_stays_an_answer(harness):
    """Asked something about its task since (the Router's note or more of it, the owner's
    chat), its answer is that answer (E115), not its result."""
    runtime = await harness.connect()
    for asking in ("note", "continue", "chat"):
        worker, task = await spawn(runtime, harness.router_id, f"Profile the import {asking}")
        session = harness.session(worker)
        await turn(runtime, session)
        if asking == "chat":
            await runtime.owner_send_to(harness.router_id, worker, "Which stage is slowest?")
        else:
            await continued(runtime, harness, worker, task, "Which stage is slowest?", asking)
        await turn(runtime, session)

        await call_tool(runtime, "report", worker, status="answer", task_id=task["id"],
                        summary="Parsing is slowest.")  # fmt: skip

        assert runtime.tasks.get(task["id"])["status"] == "running", asking
        assert router_reports(harness)[-1]["meta"]["status"] == "answer", asking
