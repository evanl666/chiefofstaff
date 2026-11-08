import contextlib
import json
import sqlite3

import pytest
from conftest import Harness, call_tool, settle, task_of

from chiefofstaff.store import Store
from chiefofstaff.tasks import settle_adopted, settle_decision_changes


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


async def test_a_queued_worker_given_more_work_still_waits_for_its_first_tasks_deps(harness):
    runtime = await harness.connect()
    schema = await spawn(runtime, harness.router_id, "Schema")
    later = await spawn(runtime, harness.router_id, "Backfill", after=[schema["task_id"]])
    worker = later["session_id"]
    await call_tool(
        runtime,
        "message_session",
        harness.router_id,
        session_id=worker,
        text="Also add a metric",
        task="new",
        title="Metric",
    )
    metric = task_of(harness, worker)
    await runtime.workers.pump()
    row = harness.store.session(worker)
    assert row["status"] == "queued" and row["activity"] == "waiting for “Schema”"
    assert worker not in harness.client.sessions, "its backfill does not start before the schema"

    await call_tool(runtime, "report", schema["session_id"], status="done", summary="Schema in")
    await settle(runtime)
    [started] = harness.session(worker).sent
    assert "Backfill" in started and "Also add a metric" in started
    assert f"- Task {schema['task_id']} “Schema” (review): Schema in" in started
    assert task(harness, later["task_id"])["status"] == "running"
    assert task_of(harness, worker) == metric, "it is on the newest task"


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

    vague = await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Also add an index"
    )
    assert vague.result_type == "failure" and "task_id=" in vague.text_result_for_llm
    assert task(harness, first["task_id"])["status"] == "review", "nothing reopened by it"
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, text="Also add an index",
        task_id=first["task_id"],
    )  # fmt: skip
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
    harness.session(second["session_id"]).emit(
        "session.error", message="Execution failed: invalid tool schema"
    )
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
    harness.session(first["session_id"]).emit(
        "session.error", message="Execution failed: invalid tool schema"
    )
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
    worker.emit("session.error", message="Execution failed: invalid tool schema")
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


async def test_a_task_left_running_by_an_old_decision_change_goes_back_once(harness):
    runtime = await harness.connect()
    kept = await spawn(runtime, harness.router_id, "Away notices")
    other = await spawn(runtime, harness.router_id, "Unanswered")
    for made in (kept, other):
        await call_tool(runtime, "report", made["session_id"], status="done", summary="Not now.")
        harness.session(made["session_id"]).emit("session.idle")
    await settle(runtime)
    for made in (kept, other):
        runtime.tasks.move(made["task_id"], "running", reason="decision changed")
    harness.store.create_draft(
        harness.router_id,
        "change",
        {
            "decision": {"id": "dec1", "text": "Skip it"},
            "before": [],
            "tasks": [
                {"task_id": kept["task_id"], "was": "review", "ack": "Nothing to change."},
                {"task_id": other["task_id"], "was": "review", "ack": ""},
            ],
        },
        "Decision changed: Skip it",
    )
    harness.store.delete("settled_decision_changes")
    settle_decision_changes(harness.store)
    restored = task(harness, kept["task_id"])
    assert restored["status"] == "review" and restored["result"] == "Not now."
    assert "Nothing to change." in restored["progress"]
    assert runtime.tasks.runs(kept["task_id"])[-1]["ended_at"]
    assert task(harness, other["task_id"])["status"] == "running", "not answered: left alone"
    runtime.tasks.move(kept["task_id"], "running", reason="decision changed")
    settle_decision_changes(harness.store)
    assert task(harness, kept["task_id"])["status"] == "running", "only once"


async def test_a_task_is_renamed_and_keeps_its_earlier_names(harness):
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Handle the new Slack message")
    result = await call_tool(
        runtime, "rename_task", harness.router_id, task_id=made["task_id"],
        title="Answer Jingtao: why two parallel gets are slow",
    )  # fmt: skip
    assert result.result_type == "success"
    view = runtime.task_actions.view(runtime.tasks.get(made["task_id"]))
    assert view["title"] == "Answer Jingtao: why two parallel gets are slow"
    assert [item["title"] for item in view["earlier_titles"]] == ["Handle the new Slack message"]
    refused = await call_tool(
        runtime, "rename_task", harness.router_id, task_id=made["task_id"], title="  "
    )
    assert refused.result_type == "failure"


async def test_a_task_that_drifted_is_split_with_each_result_its_own(harness):
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Schedule BG features weekly")
    worker = made["session_id"]
    await call_tool(runtime, "report", worker, status="done", summary="Scheduled for Mondays.")
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker,
        text="Does Qwen need a weekly rerun?", task_id=made["task_id"],
    )  # fmt: skip
    await call_tool(runtime, "report", worker, status="done", summary="No: vectors are built in.")
    bad = await call_tool(
        runtime, "split_task", harness.router_id, task_id=made["task_id"], title="111"
    )
    assert bad.result_type == "failure" and "what it delivers" in bad.text_result_for_llm
    preview = runtime.task_actions.split_preview(made["task_id"])
    assert preview["keeps"]["result"] == "Scheduled for Mondays."
    assert preview["moves"]["result"] == "No: vectors are built in."
    assert [m["kind"] for m in preview["moves"]["messages"]] == ["router", "report"], (
        "the question that asked for it and its answer: not the earlier work"
    )
    early = await call_tool(
        runtime, "split_task", harness.router_id, task_id=made["task_id"], title="Qwen rerun answer"
    )
    assert early.result_type == "success"
    [split] = [t for t in runtime.tasks.for_session(worker) if t["id"] != made["task_id"]]
    assert split["title"] == "Qwen rerun answer" and split["result"] == "No: vectors are built in."
    assert runtime.display_title(harness.store.session(worker)) == "Schedule BG features weekly", (
        "a split does not rename the worker"
    )
    undone = runtime.task_actions.merge_back(split["id"])
    assert undone["result"] == "No: vectors are built in." and not runtime.tasks.find(split["id"])
    assert len(runtime.tasks.for_session(worker)) == 1
    await call_tool(runtime, "split_task", harness.router_id, task_id=made["task_id"],
                    title="Qwen rerun answer")  # fmt: skip
    [split] = [t for t in runtime.tasks.for_session(worker) if t["id"] != made["task_id"]]
    kept = runtime.tasks.get(made["task_id"])
    assert kept["result"] == "Scheduled for Mondays." and kept["status"] == "review"
    moved = [
        m for m in harness.store.messages(worker)
        if m["kind"] == "report" and m["text"].startswith("No:")
    ]  # fmt: skip
    assert moved[0]["meta"]["task_id"] == split["id"], "its report is the new task's"
    assert len(runtime.tasks.runs(split["id"])) == 1
    # Split while its worker is on other work: that stays its current task.
    await call_tool(runtime, "report", worker, status="done", summary="Also: Qwen is fine.",
                    task_id=split["id"])  # fmt: skip
    other = await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker, task="new",
        title="Backfill the BG table", text="Backfill it",
    )  # fmt: skip
    assert other.result_type == "success", other.text_result_for_llm
    busy = runtime.tasks.current(worker)
    assert busy["title"] == "Backfill the BG table"
    await call_tool(runtime, "split_task", harness.router_id, task_id=split["id"],
                    title="Qwen is fine note")  # fmt: skip
    assert runtime.tasks.current(worker)["id"] == busy["id"]
    single = await spawn(runtime, harness.router_id, "One result", why_new="separate")
    await call_tool(runtime, "report", single["session_id"], status="done", summary="Done.")
    refused = await call_tool(
        runtime, "split_task", harness.router_id, task_id=single["task_id"], title="Another result"
    )
    assert refused.result_type == "failure" and "rename" in refused.text_result_for_llm


async def test_a_split_off_task_worked_on_since_cannot_be_undone(harness):
    """Code review: Undo split deleted a split-off task followed up on since (the follow-up's
    reports pointed at a deleted task)."""
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Schedule BG features weekly")
    worker = made["session_id"]
    await call_tool(runtime, "report", worker, status="done", summary="Scheduled for Mondays.")
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker,
        text="Does Qwen need a weekly rerun?", task_id=made["task_id"],
    )  # fmt: skip
    await call_tool(runtime, "report", worker, status="done", summary="No: vectors are built in.")
    split = runtime.task_actions.split(made["task_id"], "Qwen rerun answer")
    assert split["split_from"]["until"], "undo offered at first"
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    text="And the 8B model?", task_id=split["id"])  # fmt: skip
    shown = runtime.task_actions.view(runtime.tasks.get(split["id"]))
    assert shown["split_from"]["until"] == "", "no Undo once it is worked on"
    with pytest.raises(ValueError, match="worked on since the split"):
        runtime.task_actions.merge_back(split["id"])
    assert runtime.tasks.find(split["id"]), "the task and its follow-up stay"


async def test_a_split_moves_only_what_asked_for_the_latest_result(harness):
    """Bug bash 3: the preview moved the owner's earlier word on the first result too."""
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Schedule BG features weekly")
    worker = made["session_id"]
    await call_tool(runtime, "report", worker, status="done", summary="Scheduled for Mondays.")
    await runtime.owner_send(worker, "Keep Mondays.")
    harness.session(worker).emit("assistant.message", content="Noted: Mondays it stays.")
    await settle(runtime)
    await call_tool(
        runtime, "message_session", harness.router_id, session_id=worker,
        text="Does Qwen need a weekly rerun?", task_id=made["task_id"],
    )  # fmt: skip
    await call_tool(runtime, "report", worker, status="done", summary="No: vectors are built in.")
    moves = runtime.task_actions.split_preview(made["task_id"])["moves"]["messages"]
    assert [m["text"] for m in moves] == ["Does Qwen need a weekly rerun?",
                                          "No: vectors are built in."]  # fmt: skip


def test_a_short_title_in_chinese_says_what_it_delivers():
    from chiefofstaff.task_actions import deliverable_title

    assert deliverable_title("修复登录日期") == "修复登录日期"
    for bad in ("111", "修复", "Fix it"):
        with pytest.raises(ValueError, match="what it delivers"):
            deliverable_title(bad)


async def test_files_in_a_workers_own_folder_are_artifacts_and_a_name_said_again_replaces(
    tmp_path, git_repo
):
    """Bug bash: mockups a worker made in its workspace (beside its checkout) and declared were
    dropped; an artifact declared again under another link stayed twice."""
    from chiefofstaff.models import Settings

    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    made = await spawn(runtime, harness.router_id, "Mockups", project=str(git_repo), isolate=True)
    row = harness.store.session(made["session_id"])
    scratch = runtime.workspaces.workspace(row) / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "mock-a.html").write_text("<p>a</p>")
    (scratch / "job.sh").write_text("run")
    (scratch / "done.txt").write_text("done")
    runtime.task_actions.declare_artifacts(made["task_id"], [
        {"url": str(scratch / "mock-a.html"), "name": "Mockup A", "status": "draft"},
        {"url": str(scratch / "job.sh"), "name": "Nightly export job", "status": "running"},
    ])  # fmt: skip
    runtime.task_actions.declare_artifacts(made["task_id"], [
        {"url": str(scratch / "done.txt"), "name": "Nightly export job", "status": "done"},
    ])  # fmt: skip
    found = runtime.task_actions.view(runtime.tasks.get(made["task_id"]))["artifacts"]
    assert [(item["name"], item["status"]) for item in found] == [
        ("Mockup A", "draft"), ("Nightly export job", "done"),
    ]  # fmt: skip
    assert found[0]["url"].startswith("file://") and found[0]["path"].endswith("mock-a.html")


async def test_a_worker_says_what_its_task_made_and_where_each_stands(harness):
    runtime = await harness.connect()
    worker = (await spawn(runtime, harness.router_id, "Run the ICP flow"))["session_id"]
    task = runtime.tasks.current(worker)
    flow = "https://flyte.example.com/console/projects/p/domains/d/executions/abc123"
    doc = "https://docs.google.com/document/d/abc/edit"
    await call_tool(runtime, "report", worker, status="progress", summary="Started the run.",
                    artifacts=[{"url": flow, "name": "ICP flow, full run", "status": "running"},
                               {"url": "https://github.com/acme/app/pull/7", "name": "PR",
                                "status": "open"},
                               {"url": "not a link", "name": "x", "status": "y"}])  # fmt: skip
    await call_tool(runtime, "report", worker, status="done", summary="Ran it; results in the doc.",
                    artifacts=[{"url": doc, "name": "ICP results", "status": "final"},
                               {"url": flow, "name": "ICP flow, full run",
                                "status": "succeeded"}])  # fmt: skip
    found = runtime.task_actions.view(runtime.tasks.get(task["id"]))["artifacts"]
    assert [(item["url"], item["name"], item["status"]) for item in found] == [
        (doc, "ICP results", "final"),
        (flow, "ICP flow, full run", "succeeded"),
    ], "kept by link, the latest word on each; PRs come from GitHub; not a link: dropped"
