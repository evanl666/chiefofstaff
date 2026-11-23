"""E34: a task kept showing an old result when its worker reported again on it (the newer
report became news); its newer report is its result now, the earlier ones kept with it."""

import pytest
from conftest import call_tool, settle
from test_progress import spawn


def view(runtime, task_id):
    return runtime.task_actions.view(runtime.tasks.get(task_id))


async def reported(runtime, harness, title, first):
    worker = await spawn(runtime, harness.router_id, title)
    task = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="done", summary=first)
    await settle(runtime)
    return worker, task["id"]


async def test_a_newer_report_on_a_result_to_accept_replaces_it(harness):
    runtime = await harness.connect()
    worker, task = await reported(runtime, harness, "Compare vector stores", "Qdrant fits.")
    runtime.task_actions.mark_read(task)
    await runtime.owner_send(worker, "Did you check pgvector too?")
    await call_tool(runtime, "report", worker, status="done", task_id=task, replaces_result=True,
                    summary="Checked pgvector too: Qdrant still fits best.")  # fmt: skip
    await settle(runtime)
    shown = view(runtime, task)
    assert shown["result"] == "Checked pgvector too: Qdrant still fits best."
    assert [item["text"] for item in shown["earlier_results"]] == ["Qdrant fits."]
    assert shown["status"] == "review" and not shown["news"], "a result to accept, not news"
    assert not shown["read"], "a new result, not read yet"


async def test_a_newer_report_on_an_accepted_result_replaces_it_and_is_news(harness):
    runtime = await harness.connect()
    worker, task = await reported(runtime, harness, "Compare vector stores", "Qdrant fits.")
    runtime.task_actions.accept(task)
    await runtime.owner_send(worker, "Did you check pgvector too?")
    await call_tool(runtime, "report", worker, status="done", task_id=task, replaces_result=True,
                    summary="Checked pgvector too: Qdrant still fits best.")  # fmt: skip
    await settle(runtime)
    shown = view(runtime, task)
    assert shown["result"].startswith("Checked pgvector")
    assert shown["earlier_results"][0]["text"] == "Qdrant fits."
    assert shown["news"], "the owner sees it came"


async def test_progress_on_finished_work_is_news_not_its_result(harness):
    runtime = await harness.connect()
    worker, task = await reported(runtime, harness, "Compare vector stores", "Qdrant fits.")
    await call_tool(runtime, "report", worker, status="progress", task_id=task,
                    summary="Its PR got an approval.")  # fmt: skip
    await settle(runtime)
    shown = view(runtime, task)
    assert shown["result"] == "Qdrant fits." and shown["earlier_results"] == []
    assert shown["news"]["text"] == "Its PR got an approval."


async def test_an_answer_about_finished_work_is_news_and_its_result_stays(harness):
    """Not E34: a question about it answered ("3 lines.") is not its result."""
    runtime = await harness.connect()
    worker, task = await reported(runtime, harness, "Compare vector stores", "Qdrant fits.")
    await runtime.owner_send(worker, "How many did you compare?")
    await call_tool(runtime, "report", worker, status="done", task_id=task, summary="Three.")
    await settle(runtime)
    shown = view(runtime, task)
    assert shown["result"] == "Qdrant fits." and shown["earlier_results"] == []
    assert shown["news"]["text"] == "Three."
    # Item 2 of the owner's report (0.1.53): on Home too, as an Answer/Update, once.
    [line] = on_home(harness, task, "Three.")
    assert line["meta"]["status"] == "answer"


async def test_the_worker_hears_its_result_was_replaced(harness):
    runtime = await harness.connect()
    worker, task = await reported(runtime, harness, "Compare vector stores", "Qdrant fits.")
    await runtime.owner_send(worker, "Redo it with pgvector.")
    said = await call_tool(runtime, "report", worker, status="done", task_id=task,
                           replaces_result=True, summary="Redone: Qdrant fits.")  # fmt: skip
    assert "result is this report now" in said.text_result_for_llm


async def test_a_split_does_not_show_its_result_again_as_an_earlier_one(harness):
    runtime = await harness.connect()
    worker, task = await reported(runtime, harness, "Compare vector stores", "Qdrant fits.")
    await runtime.task_actions.follow_up(task, "Also benchmark big exports.")
    await call_tool(runtime, "report", worker, status="done", task_id=task,
                    summary="Benchmarked big exports: 2 s each.")  # fmt: skip
    await settle(runtime)
    assert view(runtime, task)["earlier_results"][0]["text"] == "Qdrant fits."
    runtime.task_actions.split(task, "Benchmark big exports")
    shown = view(runtime, task)
    assert shown["result"] == "Qdrant fits." and shown["earlier_results"] == []


def on_home(harness, task, text):
    """The lines of the Router's chat on Home that show this report of the task."""
    return [
        m
        for m in harness.store.messages(harness.router_id)
        if m["kind"] == "inbound" and m["meta"].get("task_id") == task and m["text"] == text
    ]


@pytest.mark.parametrize("accepted", [True, False])
async def test_a_correction_asked_in_its_chat_stays_on_home_during_the_next_follow_up(
    harness, accepted
):
    """Owner report (0.1.53) item 2: the owner asked a finished task's worker in its chat for a
    correction, which it reported done; asked it something more, Needs you showed nothing of
    the task (its worker follows up) and the Router's chat had no line of it: the corrected
    result was in neither. It is a line there, once, on its task, the Router not woken for it
    (nothing it answers, "(quiet)" or not, hides it)."""
    runtime = await harness.connect()
    worker, task = await reported(runtime, harness, "Compare stores", "First result.")
    if accepted:
        runtime.task_actions.accept(task)
    await runtime.owner_send(worker, "Include pgvector.")
    router = harness.session(harness.router_id)
    told = len(router.sent)
    summary = "Checked pgvector. Qdrant still fits."
    await call_tool(runtime, "report", worker, status="done", task_id=task,
                    replaces_result=True, summary=summary)  # fmt: skip
    await settle(runtime)
    await runtime.owner_send(worker, "How about Redis?")
    await settle(runtime)
    [line] = on_home(harness, task, summary)
    assert line["meta"]["status"] == "done" and line["meta"]["from"] == worker
    assert runtime.tasks.get(task)["result"] == summary
    assert [t["id"] for t in runtime.tasks.for_session(worker)] == [task], "its own task still"
    assert len(router.sent) == told, "the owner asked its worker: the Router is not woken"


async def test_a_correction_that_crossed_the_owners_next_message_shows_once(harness):
    """Item 2's crossed messages: the owner wrote again while its worker was reporting the
    correction; each result it reports shows once."""
    runtime = await harness.connect()
    worker, task = await reported(runtime, harness, "Compare stores", "First result.")
    runtime.task_actions.accept(task)
    session = harness.session(worker)
    await runtime.owner_send(worker, "Include pgvector.")
    session.emit("user.message", content=session.sent[-1])
    await settle(runtime)
    await runtime.owner_send(worker, "And Redis?")
    first = "Checked pgvector. Qdrant still fits."
    await call_tool(runtime, "report", worker, status="done", task_id=task,
                    replaces_result=True, summary=first)  # fmt: skip
    session.emit("session.idle")
    await settle(runtime)
    session.emit("user.message", content=session.sent[-1])
    second = "Checked Redis too. Qdrant still fits."
    await call_tool(runtime, "report", worker, status="done", task_id=task,
                    replaces_result=True, summary=second)  # fmt: skip
    session.emit("session.idle")
    await settle(runtime)
    assert len(on_home(harness, task, first)) == 1 and len(on_home(harness, task, second)) == 1
    assert runtime.tasks.get(task)["result"] == second


async def test_a_report_amended_in_its_turn_shows_on_home_as_amended(harness):
    """Reported again in the same turn (with more, say): its line in the Router's chat says
    the result as it is now, once."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Compare stores")
    task = runtime.tasks.current(worker)["id"]
    await call_tool(runtime, "report", worker, status="done", summary="Qdrant fits.")
    amended = "Qdrant fits: 2 ms per query on the 1M set."
    await call_tool(runtime, "report", worker, status="done", task_id=task, summary=amended)
    await settle(runtime)
    assert len(on_home(harness, task, amended)) == 1 and not on_home(harness, task, "Qdrant fits.")


async def test_news_on_an_accepted_tasks_pr_that_redoes_it_shows_on_home(harness):
    """Item 2's PR following: review comments on its PR after the owner accepted it, handled
    and reported done, show in the Router's chat."""
    runtime = await harness.connect()
    worker, task = await reported(runtime, harness, "Fix login", "Fixed in #7.")
    runtime.task_actions.record_opened(task, ["https://github.com/acme/app/pull/7"])
    runtime.follow(worker, ["github:acme/app#7"], task_id=task)
    runtime.task_actions.accept(task)
    await runtime.on_thread("github", "github:acme/app#7", ["k1"], "Review: rename the flag.")
    await settle(runtime)
    session = harness.session(worker)
    session.emit("user.message", content=session.sent[-1])
    await settle(runtime)
    summary = "Renamed the flag as the review asked; pushed to #7."
    await call_tool(runtime, "report", worker, status="done", task_id=task, summary=summary)
    await settle(runtime)
    assert len(on_home(harness, task, summary)) == 1


async def test_a_correction_asked_on_home_is_its_result_there_once(harness):
    """From Home (@): its line there is the corrected result (not the words it said after
    it), once."""
    runtime = await harness.connect()
    worker, task = await reported(runtime, harness, "Compare stores", "First result.")
    runtime.task_actions.accept(task)
    await runtime.owner_send_to(harness.router_id, worker, "Include pgvector.")
    summary = "Checked pgvector. Qdrant still fits."
    await call_tool(runtime, "report", worker, status="done", task_id=task,
                    replaces_result=True, summary=summary)  # fmt: skip
    session = harness.session(worker)
    session.emit("assistant.message", content="Done.")
    session.emit("session.idle")
    await settle(runtime)
    lines = [
        (m["kind"], m["text"])
        for m in harness.store.messages(harness.router_id)
        if m["kind"] == "answer" or (m["kind"] == "inbound" and m["meta"].get("from") == worker)
    ]
    assert lines == [("inbound", "First result."), ("inbound", summary)]
