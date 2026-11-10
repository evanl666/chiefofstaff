"""E34: a task kept showing an old result when its worker reported again on it (the newer
report became news); its newer report is its result now, the earlier ones kept with it."""

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
