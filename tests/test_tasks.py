import contextlib
import json
import sqlite3

from conftest import Harness, call_tool, settle, task_of

from chiefofstaff.store import Store
from chiefofstaff.tasks import settle_adopted


async def spawn(runtime, router_id, title, **extra):
    result = await call_tool(
        runtime, "spawn_worker", router_id, title=title, instructions=title, **extra
    )
    return json.loads(result.text_result_for_llm)


def task(harness, task_id):
    return harness.store.one("SELECT * FROM tasks WHERE id=?", (task_id,))


async def test_a_dependency_waits_for_the_new_work_of_a_reused_worker(harness):
    runtime = await harness.connect()
    login = await spawn(runtime, harness.router_id, "Fix login")
    worker = login["session_id"]
    await call_tool(runtime, "report", worker, status="done", summary="Login fixed")
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    assert task(harness, login["task_id"])["status"] == "review"

    reply = await call_tool(
        runtime,
        "message_session",
        harness.router_id,
        session_id=worker,
        text="Add a captcha to the login form",
        task="new",
        title="Add captcha",
    )
    captcha = task_of(harness, worker)
    assert captcha != login["task_id"] and captcha in reply.text_result_for_llm
    assert f'type="task" title="Add captcha" task="{captcha}"' in harness.session(worker).sent[-1]

    docs = await spawn(runtime, harness.router_id, "Document captcha", after=[worker])
    await runtime.workers.pump()
    assert docs["status"] == "queued", "it waits for the captcha, not yesterday's login fix"
    assert harness.store.session(docs["session_id"])["activity"] == "waiting for “Add captcha”"

    await call_tool(
        runtime, "report", worker, status="done", summary="Captcha added", task_id=captcha
    )
    await settle(runtime)
    assert task(harness, captcha)["status"] == "review"
    assert task(harness, login["task_id"])["result"] == "Login fixed", "each task keeps its result"
    started = harness.session(docs["session_id"]).sent[0]
    assert f"- Task {captcha} “Add captcha” (review): Captcha added" in started


async def test_reports_runs_and_usage_belong_to_the_task(harness):
    runtime = await harness.connect()
    first = await spawn(runtime, harness.router_id, "A")
    other = await spawn(runtime, harness.router_id, "B")
    worker = first["session_id"]
    refused = await call_tool(
        runtime, "report", worker, status="done", summary="x", task_id=other["task_id"]
    )
    assert refused.result_type == "failure", "a worker reports only on its own tasks"

    harness.session(worker).emit("assistant.usage", model="m", input_tokens=10, copilot_usage=None)
    await settle(runtime)
    await call_tool(runtime, "report", worker, status="question", summary="Which DB?")
    assert task(harness, first["task_id"])["status"] == "waiting"
    assert 'task="' + first["task_id"] + '"' in harness.session(harness.router_id).sent[-1]
    await call_tool(runtime, "report", worker, status="done", summary="Done with Postgres")
    runs = runtime.tasks.runs(first["task_id"])
    assert [run["status"] for run in runs] == ["succeeded"]
    usage = harness.store.all("SELECT run_id FROM usage WHERE session_id=?", (worker,))
    assert [row["run_id"] for row in usage] == [runs[0]["id"]]

    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Also add an index"
    )
    reopened = task(harness, first["task_id"])
    assert reopened["status"] == "running", "a follow-up reopens the finished task"
    assert [run["reason"] for run in runtime.tasks.runs(first["task_id"])] == ["start", "follow-up"]


async def test_cancelling_a_task_stops_its_worker_and_blocks_what_depends_on_it(harness):
    runtime = await harness.connect()
    first = await spawn(runtime, harness.router_id, "Migrate")
    later = await spawn(runtime, harness.router_id, "Backfill", after=[first["task_id"]])
    worker = first["session_id"]

    result = await call_tool(
        runtime, "cancel_task", harness.router_id, task_id=first["task_id"], reason="Not needed"
    )
    assert result.result_type == "success"
    await settle(runtime)
    assert harness.session(worker).aborted
    row = harness.store.session(worker)
    assert row["status"] == "idle" and not row["closed_at"], "the worker stays for other tasks"
    assert task(harness, first["task_id"])["status"] == "cancelled"
    assert runtime.tasks.runs(first["task_id"])[-1]["status"] == "cancelled"
    told = [p for p in harness.session(harness.router_id).sent if "ended without finishing" in p]
    assert len(told) == 1 and later["task_id"] in told[0]

    refused = await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Go on"
    )
    assert "task='new'" in refused.text_result_for_llm, "a cancelled task is not reopened"
    await call_tool(
        runtime,
        "message_session",
        harness.router_id,
        session_id=worker,
        text="Next",
        task="new",
        title="Next",
    )
    sent = harness.session(worker).sent[-1]
    assert 'type="cancelled"' in sent and sent.endswith("Next\n</message>")
    again = await call_tool(runtime, "cancel_task", harness.router_id, task_id=first["task_id"])
    assert again.result_type == "failure"


async def test_a_new_worker_can_build_on_earlier_results(harness):
    runtime = await harness.connect()
    research = await spawn(runtime, harness.router_id, "Research caching")
    await call_tool(
        runtime,
        "report",
        research["session_id"],
        status="done",
        summary="Use Redis; notes in /tmp/notes.md",
    )
    built = await spawn(
        runtime,
        harness.router_id,
        "Implement caching",
        context_from=[research["task_id"]],
        why_new="An independent code change",
    )
    first = harness.session(built["session_id"]).sent[0]
    assert 'from="system" type="context"' in first
    assert f"Task {research['task_id']} “Research caching” (review)" in first
    assert "Result: Use Redis; notes in /tmp/notes.md" in first


async def test_the_owner_accepts_and_retries_tasks(harness):
    runtime = await harness.connect()
    first = await spawn(runtime, harness.router_id, "A")
    worker = first["session_id"]
    await call_tool(runtime, "report", worker, status="done", summary="ok")
    assert runtime.task_actions.accept(first["task_id"])["status"] == "done"

    second = await spawn(runtime, harness.router_id, "B", why_new="separate")
    harness.session(second["session_id"]).emit("session.error", message="model overloaded")
    await settle(runtime)
    assert task(harness, second["task_id"])["status"] == "failed"
    retried = await runtime.task_actions.retry(second["task_id"], "Use the smaller model")
    assert retried["status"] == "running"
    assert "Use the smaller model" in harness.session(second["session_id"]).sent[-1]
    assert [r["reason"] for r in runtime.tasks.runs(second["task_id"])] == ["start", "retry"]


async def test_a_blocked_task_shows_at_once_and_can_still_be_retried(harness):
    runtime = await harness.connect()
    published = []
    publish = runtime.bus.publish

    def spy(kind, data=None):
        if kind == "task":
            published.append((data["id"], data["blocked"]))
        publish(kind, data)

    runtime.bus.publish = spy
    first = await spawn(runtime, harness.router_id, "Migrate")
    later = await spawn(runtime, harness.router_id, "Backfill", after=[first["task_id"]])
    harness.session(first["session_id"]).emit("session.error", message="model overloaded")
    await settle(runtime)
    assert (later["task_id"], True) in published, "the inbox learns that it is blocked"

    # Cancelled before its worker ever started; retrying starts that worker.
    await runtime.task_actions.cancel(later["task_id"])
    assert later["session_id"] not in harness.client.sessions
    retried = await runtime.task_actions.retry(later["task_id"])
    assert retried["status"] == "running"
    sent = harness.session(later["session_id"]).sent[-1]
    assert "Backfill" in sent and "try task" in sent


async def test_a_retry_that_cannot_reach_its_worker_leaves_the_task_as_it_was(harness):
    runtime = await harness.connect()
    first = await spawn(runtime, harness.router_id, "A")
    worker = harness.session(first["session_id"])
    worker.emit("session.error", message="model overloaded")
    await settle(runtime)

    async def broken(prompt, mode=None):
        raise RuntimeError("socket closed")

    worker.send = broken
    with contextlib.suppress(RuntimeError):
        await runtime.task_actions.retry(first["task_id"])
    assert task(harness, first["task_id"])["status"] == "failed"
    assert [r["status"] for r in runtime.tasks.runs(first["task_id"])] == ["failed", "failed"]


async def test_workers_from_before_tasks_get_one_each(tmp_path):
    data = tmp_path / "data"
    store = Store(data)
    stamp = "2026-01-01T00:00:00+00:00"
    for session_id, status, closed in (("a" * 36, "done", None), ("b" * 36, "queued", None)):
        store.execute(
            """INSERT INTO sessions(id, role, title, status, task, report, closed_at, created_at,
            updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                session_id,
                "worker",
                status.title(),
                status,
                "do it",
                "it is done",
                closed,
                stamp,
                stamp,
            ),
        )
    store.set("after:" + "b" * 36, json.dumps(["a" * 36]))
    store.close()
    with sqlite3.connect(data / "chiefofstaff.db") as db:
        db.execute("DELETE FROM tasks")

    harness = Harness(data)
    runtime = await harness.connect()
    first, second = runtime.tasks.current("a" * 36), runtime.tasks.current("b" * 36)
    assert (first["status"], first["result"]) == ("review", "it is done")
    assert second["status"] == "running", "it had no unmet dependency, so it started"
    assert [dep["id"] for dep in runtime.tasks.deps(second["id"])] == [first["id"]]


async def test_a_session_adopted_before_tasks_is_not_shown_as_running(harness):
    await harness.connect()
    store = harness.store
    for identifier in ("adopted", "busy"):
        store.create_session(identifier, "worker", identifier.title(), "idle", cwd="/tmp")
        created = harness.runtime.tasks.create(identifier, identifier.title(), "")
        harness.runtime.tasks.move(created["id"], "running")
    harness.runtime.add_message("busy", "router", "Fix it", {"prompt": "Fix it"})
    settle_adopted(store)
    assert harness.runtime.tasks.current("adopted")["status"] == "done"
    assert harness.runtime.tasks.current("busy")["status"] == "running", "a real task stays"
