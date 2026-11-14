"""QA round 12, E41: a run its worker said is running is not waited for for ever. Nothing
telling of it for a day, it is said to be unknown (not "Waiting for the run"), and the owner
marks it ended or stops tracking it; "checked" only when Chief of Staff read it itself (a
GitHub Actions run, on GitHub); its worker, reporting done with one still going and nothing
to wake it, is told once how the owner hears its end."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeClient, FakeSlackMcp, FakeTokens, call_tool, settle
from fastapi.testclient import TestClient
from test_round10_lifecycle import spawn, view
from test_web import ORIGIN, TOKEN, sign_in

from chiefofstaff.runtime import Runtime
from chiefofstaff.sources.slack import SlackSource
from chiefofstaff.task_actions import RUN_CHECK_SECONDS, RUN_UNKNOWN_HOURS
from chiefofstaff.web import create_app

FLYTE = "https://flyte.acme.dev/console/projects/ml/domains/prod/executions/f3a9"
ACTIONS = "https://github.com/acme/app/actions/runs/123"
NIGHTLY = {"url": FLYTE, "name": "Nightly export", "status": "running"}


def ago(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


def said_at(harness, task_id: str, url: str, **fields):
    """Its declared run as if what is given (when it was said, read, tried) was so."""
    known = json.loads(harness.store.get(f"artifacts:{task_id}"))
    for item in known:
        if item["url"] == url:
            item.update(fields)
    harness.store.set(f"artifacts:{task_id}", json.dumps(known))


async def delivered(harness, runtime, *runs: dict, title="Export the nightly data"):
    worker, task = await spawn(runtime, harness.router_id, title)
    told = await call_tool(runtime, "report", worker, status="done", summary="Exported.",
                           artifacts=list(runs))  # fmt: skip
    await settle(runtime)
    runtime.task_actions.accept(task["id"])
    return worker, task, told.text_result_for_llm


async def test_a_run_nothing_tells_of_for_a_day_is_unknown_not_waited_for(harness):
    runtime = await harness.connect()
    _worker, task, _told = await delivered(harness, runtime, NIGHTLY)
    [watch] = view(runtime, task["id"])["monitoring"]
    assert (watch["waiting_for"], watch["unknown"], watch["status"]) == (
        "the run",
        False,
        "running",
    )
    # Chief of Staff never read it: not "checked" when its worker said it.
    assert watch["last_check"] is None and watch["reported_at"]
    assert watch["trigger"] == "As its worker reported it: nothing checks it", (
        "no next report claimed: its worker is not at it, and has no check-back"
    )
    said_at(harness, task["id"], FLYTE, at=ago(hours=RUN_UNKNOWN_HOURS, minutes=1))
    [watch] = view(runtime, task["id"])["monitoring"]
    assert watch["unknown"] and watch["waiting_for"] == "" and watch["next_check"] is None
    assert "mark it ended or stop tracking it" in watch["trigger"]
    assert runtime.task_actions.monitoring_words(runtime.tasks.get(task["id"])) == (
        "Nightly export (status unknown)"
    )
    said_at(harness, task["id"], FLYTE, at=ago(hours=RUN_UNKNOWN_HOURS - 1))
    assert not view(runtime, task["id"])["monitoring"][0]["unknown"], "a day: not before"


async def test_the_owner_marks_a_run_ended_or_stops_tracking_it(harness):
    runtime = await harness.connect()
    weekly = {"url": FLYTE.replace("f3a9", "b7c2"), "name": "Weekly export", "status": "queued"}
    _worker, task, _told = await delivered(harness, runtime, NIGHTLY, weekly)
    for url in (FLYTE, weekly["url"]):
        said_at(harness, task["id"], url, at=ago(days=2))
    actions = runtime.task_actions
    shown = actions.end_runs(task["id"], [FLYTE], "ended")
    [left] = shown["monitoring"]
    assert left["label"] == "Weekly export" and left["unknown"]
    ended = {d["title"]: d for d in shown["deliverables"]}["Nightly export"]
    assert (ended["status"], ended["said_by"]) == ("ended", "you")
    shown = actions.end_runs(task["id"], [weekly["url"]], "untracked")
    assert shown["monitoring"] == [], "it leaves Monitoring: none of its runs is followed"
    weekly_shown = {d["title"]: d for d in shown["deliverables"]}["Weekly export"]
    assert weekly_shown["status"] == "queued", "as it was last said: no word on how it ended"
    # Its worker saying where one stands again is the latest word: followed again.
    worker = task["session_id"]
    await call_tool(runtime, "report", worker, status="progress", task_id=task["id"],
                    summary="Rerun.", artifacts=[weekly | {"status": "running"}])  # fmt: skip
    assert [w["label"] for w in view(runtime, task["id"])["monitoring"]] == ["Weekly export"]
    with pytest.raises(ValueError, match="not followed as going"):
        actions.end_runs(task["id"], ["https://flyte.acme.dev/other"], "ended")


async def test_a_worker_reporting_done_with_a_run_going_is_told_once_how_its_end_is_heard(
    harness,
):
    """(a): accepted as it is, with a word on what wakes it for the run; once."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Export the nightly data")
    told = await call_tool(runtime, "report", worker, status="done", summary="Exported.",
                           artifacts=[NIGHTLY])  # fmt: skip
    said = told.text_result_for_llm
    assert said.startswith("Reported to the Router.") and "\u201cNightly export\u201d" in said
    assert "call check_back for it now" in said and runtime.tasks.get(task["id"])["status"] == (
        "review"
    )
    checked = await call_tool(runtime, "check_back", worker, task_id=task["id"], minutes=60,
                              what="whether the nightly export finished", url=FLYTE)  # fmt: skip
    assert checked.result_type == "success", checked.text_result_for_llm
    runtime.task_actions.accept(task["id"])
    shown = view(runtime, task["id"])
    flow = next(w for w in shown["monitoring"] if w["kind"] == "flow")
    assert flow["trigger"] == "Its worker's check-back on it" and flow["next_check"]
    said_at(harness, task["id"], FLYTE, at=ago(days=2))
    flow = next(w for w in view(runtime, task["id"])["monitoring"] if w["kind"] == "flow")
    assert not flow["unknown"], "its check-back will tell"
    # Woken then, it says where the run stands: news, its result stays.
    await runtime.task_actions.wake_check_backs(datetime.now(UTC) + timedelta(hours=2))
    await settle(runtime)
    woken = harness.session(worker).sent[-1]
    # Each call names the task: its worker may be on another one by then.
    assert "which you reported done" in woken
    assert f"report(status=\"progress\", task_id='{task['id']}')" in woken
    assert f"check_back again with task_id='{task['id']}'" in woken
    result = runtime.tasks.get(task["id"])["result"]
    await call_tool(runtime, "report", worker, status="progress", task_id=task["id"],
                    summary="The export finished.",
                    artifacts=[NIGHTLY | {"status": "succeeded"}])  # fmt: skip
    finished = runtime.tasks.get(task["id"])
    assert (finished["status"], finished["result"]) == ("done", result)
    assert view(runtime, task["id"])["monitoring"] == []


async def test_the_word_on_a_run_going_is_said_once(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Export the nightly data")
    told = await call_tool(runtime, "report", worker, status="done", summary="Exported.",
                           artifacts=[NIGHTLY])  # fmt: skip
    assert "call check_back for it now" in told.text_result_for_llm
    again = await call_tool(runtime, "report", worker, status="done", task_id=task["id"],
                            summary="Exported, all of it.", artifacts=[NIGHTLY])  # fmt: skip
    assert again.text_result_for_llm.startswith(f"Task {task['id']}'s report is updated.")
    assert "check_back" not in again.text_result_for_llm


async def test_no_word_on_a_run_when_it_is_checked_on_or_read_on_github(harness):
    runtime = await harness.connect()
    worker, _task = await spawn(runtime, harness.router_id, "Deploy the app")
    await call_tool(runtime, "check_back", worker, minutes=30, what="whether the deploy finished")
    refused = await call_tool(runtime, "report", worker, status="done", summary="Deployed.",
                              artifacts=[NIGHTLY])  # fmt: skip
    assert "set a check-back on this task in this turn" in refused.text_result_for_llm
    told = await call_tool(runtime, "report", worker, status="done", summary="Deployed.",
                           artifacts=[NIGHTLY])  # fmt: skip
    assert told.text_result_for_llm == "Reported to the Router."
    _w, _t, said = await delivered(
        harness, runtime, {"url": ACTIONS, "name": "Deploy", "status": "in progress"},
        title="Deploy the API",
    )  # fmt: skip
    assert said == "Reported to the Router.", "Chief of Staff reads it on GitHub itself"


async def test_a_finished_task_with_no_run_going_takes_no_check_back(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Write the notes")
    await call_tool(runtime, "report", worker, status="done", summary="Written.")
    refused = await call_tool(runtime, "check_back", worker, task_id=task["id"], minutes=30,
                              what="whether anyone read it")  # fmt: skip
    assert refused.result_type == "failure"
    assert "a run of a finished one that still goes on" in refused.text_result_for_llm


async def test_a_github_actions_run_is_read_on_github_and_checked_is_true(harness):
    """(d): its status is what GitHub says; "checked" is when Chief of Staff read it."""
    runtime = await harness.connect()
    actions = runtime.task_actions
    deploy = {"url": ACTIONS, "name": "Deploy", "status": "queued"}
    _worker, task, _said = await delivered(harness, runtime, deploy, title="Deploy the app")
    path = "api repos/acme/app/actions/runs/123"
    harness.gh_replies[path] = json.dumps({"status": "requested", "conclusion": None})
    await actions.check_runs()
    assert view(runtime, task["id"])["monitoring"][0]["status"] == "queued"
    said_at(harness, task["id"], ACTIONS, checked_at=ago(minutes=6))
    harness.gh_replies[path] = json.dumps({"status": "in_progress", "conclusion": None})
    await actions.check_runs()
    [watch] = view(runtime, task["id"])["monitoring"]
    assert (watch["status"], watch["waiting_for"]) == ("in progress", "the run")
    assert watch["last_check"] and watch["trigger"].startswith("Chief of Staff reads it on GitHub")
    due = datetime.fromisoformat(watch["last_check"]) + timedelta(seconds=RUN_CHECK_SECONDS)
    assert watch["next_check"] == due.isoformat()
    reads = lambda: [c for c, _ in harness.gh_calls if " ".join(c).startswith(path)]  # noqa: E731
    await actions.check_runs()
    assert len(reads()) == 2, "read every few minutes, not every round"
    # Its worker's word days old, read on GitHub a moment ago: not unknown.
    said_at(harness, task["id"], ACTIONS, at=ago(days=2))
    assert not view(runtime, task["id"])["monitoring"][0]["unknown"]
    said_at(harness, task["id"], ACTIONS, checked_at=ago(minutes=6))
    harness.gh_replies[path] = json.dumps({"status": "completed", "conclusion": "failure"})
    await actions.check_runs()
    shown = view(runtime, task["id"])
    assert shown["monitoring"] == [], "over: nothing to wait for"
    [run] = [d for d in shown["deliverables"] if d["url"] == ACTIONS]
    assert (run["status"], run["said_by"]) == ("failure", "GitHub")


async def test_a_github_run_that_cannot_be_read_is_tried_again_and_goes_unknown(harness):
    runtime = await harness.connect()
    actions = runtime.task_actions
    deploy = {"url": ACTIONS, "name": "Deploy", "status": "queued"}
    _worker, task, _said = await delivered(harness, runtime, deploy, title="Deploy the app")
    path = "api repos/acme/app/actions/runs/123"
    harness.gh_replies[path] = RuntimeError("HTTP 404: Not Found")
    await actions.check_runs()
    await actions.check_runs()
    reads = [c for c, _ in harness.gh_calls if " ".join(c).startswith(path)]
    assert len(reads) == 1, "tried again a few minutes later, not every round"
    [watch] = view(runtime, task["id"])["monitoring"]
    assert watch["last_check"] is None and watch["status"] == "queued"
    said_at(harness, task["id"], ACTIONS, at=ago(days=2))
    assert view(runtime, task["id"])["monitoring"][0]["unknown"]


def test_the_owner_ends_runs_from_home(tmp_path):
    held = {}

    def factory(store, bus):
        async def notifier(title, text):
            pass

        runtime = Runtime(store, bus, backend_factory=lambda _: FakeClient(), notifier=notifier)
        runtime.slack = SlackSource(store, runtime, tokens=FakeTokens(""), mcp=FakeSlackMcp())
        held["runtime"] = runtime
        return runtime

    app = create_app(tmp_path / "data", TOKEN, 8765, runtime_factory=factory)
    with TestClient(app, base_url="http://127.0.0.1:8765") as http:
        sign_in(http)
        runtime = held["runtime"]
        store = runtime.store
        task = runtime.tasks.create("w", "Export the nightly data", "x", status="done")
        store.create_session("w", "worker", "Export", "idle")
        store.execute("UPDATE tasks SET session_id='w'")
        store.set(f"artifacts:{task['id']}", json.dumps([NIGHTLY | {"at": ago(days=2)}]))
        assert runtime.task_actions.view(runtime.tasks.get(task["id"]))["monitoring"][0]["unknown"]
        path = f"/api/tasks/{task['id']}/runs"
        refused = http.post(path, json={"how": "gone", "urls": [FLYTE]}, headers=ORIGIN)
        assert refused.status_code == 422
        done = http.post(path, json={"how": "ended", "urls": [FLYTE]}, headers=ORIGIN)
        assert done.status_code == 200, done.text
        assert done.json()["monitoring"] == []


def test_home_says_a_run_is_unknown_and_checked_only_when_it_was():
    """(b), (c): "Waiting for the run · No updates for 47h · checked 1d ago" of a run nobody
    checked, for days."""
    from test_ui_text import STATIC, run_js

    found = run_js(
        f"const k = await import({json.dumps((STATIC / 'tasks.js').as_uri())});\n"
        f"const b = await import({json.dumps((STATIC / 'board.js').as_uri())});\n"
        "const at = (h) => new Date(Date.now() - h * 3600000).toISOString();\n"
        "const flow = (extra) => ({ key: 'f', kind: 'flow', label: 'Nightly export',"
        " url: 'https://x/f', waiting_for: 'the run', status: 'running', unknown: false,"
        " reported_at: at(3), last_check: null, next_check: null, trigger: '',"
        " changed_at: at(3), ...extra });\n"
        "const lost = flow({ unknown: true, waiting_for: '', reported_at: at(49),"
        " changed_at: at(49) });\n"
        "const pr = { key: 'github:o/app#7', kind: 'pr', label: 'app#7', url: '',"
        " waiting_for: 'approval', last_check: null, next_check: null, trigger: '',"
        " changed_at: at(5) };\n"
        "const task = (...items) => ({ id: 't', status: 'done', monitoring: items });\n"
        "return [b.monitorWhy(task(flow({}))), k.monitorBits(task(flow({}))).checks,"
        " k.monitorBits(task(flow({ last_check: at(0.1) }))).checks,"
        " b.monitorWhy(task(lost)), k.monitorBits(task(lost)).checks,"
        " b.monitorWhy(task(lost, pr)), k.monitorBits(task(lost, pr)).more,"
        " b.monitorWhy(task(flow({ reported_at: at(30), changed_at: at(30) }))),"
        " b.monitorWhy(task({ ...pr, changed_at: at(40) }, { ...lost, changed_at: at(30) }))];"
    )
    assert found == [
        "Waiting for the run · No updates for 3h",
        "reported running 3h ago",
        "checked 6 min ago",
        "Status unknown · reported running 2d ago",
        "",
        "Waiting for approval · No updates for 5h",
        1,
        "Waiting for the run · No updates for 30h",
        "Waiting for approval · No updates for 40h",
    ]


async def test_a_run_its_worker_is_at_is_waited_for_its_next_report(harness):
    runtime = await harness.connect()
    worker, task, _said = await delivered(harness, runtime, NIGHTLY)
    said_at(harness, task["id"], FLYTE, at=ago(days=2))
    harness.store.set(f"turntask:{worker}", task["id"])  # Its turn is on it (the owner asked).
    runtime.update_session(worker, status="working")
    [watch] = view(runtime, task["id"])["monitoring"]
    assert not watch["unknown"] and watch["trigger"] == "Its worker's next report on it"
    runtime.update_session(worker, status="idle")
    assert view(runtime, task["id"])["monitoring"][0]["unknown"]


async def test_github_runs_are_read_in_the_background(harness, monkeypatch):
    runtime = await harness.connect()
    called = asyncio.Event()

    async def check_runs():
        called.set()

    monkeypatch.setattr(runtime.task_actions, "check_runs", check_runs)
    monkeypatch.setattr(runtime.watch, "interval", 0.01)
    loop = asyncio.create_task(runtime.watch_loop())
    try:
        await asyncio.wait_for(called.wait(), 10)
    finally:
        loop.cancel()


def test_a_tasks_runs_are_folded_under_its_prs_on_artifacts():
    """E43: 22 rows, 18 of them runs (one task 9, another 6): the PRs pushed down. A task's
    deliverables together, its PRs and docs first; its runs in a line ("9 runs · 3 running ·
    1 failed") with the latest and the failed ones under it, the rest behind "+N"."""
    from test_ui_text import STATIC, run_js

    found = run_js(
        f"const a = await import({json.dumps((STATIC / 'artifacts.js').as_uri())});\n"
        "const at = (h) => new Date(Date.now() - h * 3600000).toISOString();\n"
        "const states = ['running', 'running', 'succeeded', 'failed', 'running', 'succeeded',"
        " 'succeeded', 'succeeded', 'succeeded'];\n"
        "const runs = states.map((status, i) => ({ kind: 'flow', url: `https://flyte.x/e/${i}`,"
        " title: `Export ${i}`, status, at: at(i + 1) }));\n"
        "const shipping = { id: 'a', title: 'Ship the export', status: 'done', updated_at: at(0),"
        " deliverables: [{ kind: 'pr', url: 'https://github.com/o/app/pull/7', title: 'app#7 ·"
        " Export', role: 'created', status: 'in review' }, ...runs, { kind: 'doc',"
        " url: 'https://docs.google.com/document/d/abc123/edit', title: 'Plan',"
        " status: 'final' }] };\n"
        "const other = { id: 'b', title: 'Tune the cache', status: 'done', updated_at: at(0),"
        " deliverables: [{ kind: 'pr', url: 'https://github.com/o/app/pull/8', title: 'app#8 ·"
        " Cache', role: 'created', status: 'CI failing' }, ...runs.slice(0, 2).map((run, i) =>"
        " ({ ...run, url: `https://flyte.x/c/${i}` }))] };\n"
        "const items = a.collectArtifacts([shipping, other]);\n"
        "const shape = (groups) => groups.map((g) => ({ task: g.task.id,"
        " mains: g.mains.map((i) => i.kind), runs: g.runs.map((i) => i.name),"
        " folded: g.folded && { count: g.folded.count, hidden: g.folded.hidden,"
        " words: g.folded.words, tone: g.folded.tone } }));\n"
        "return [shape(a.groupArtifacts(items)), shape(a.groupArtifacts(items, new Set(['a'])))];"
    )
    folded, unfolded = found
    assert folded == [
        {"task": "b", "mains": ["PR"], "runs": ["Export 0", "Export 1"], "folded": None},
        {
            "task": "a",
            "mains": ["PR", "Doc"],
            "runs": ["Export 0", "Export 3"],
            # Round 13 review: what needs a look most first.
            "folded": {"count": 9, "hidden": 7, "words": "1 failed · 3 running", "tone": "bad"},
        },
    ]
    assert unfolded[1]["runs"] == [f"Export {i}" for i in range(9)]
    assert unfolded[1]["folded"]["hidden"] == 0


def test_a_rows_menu_is_built_again_when_a_run_turns_unknown():
    """Review: Mark ended and Stop tracking are in a row's menu, built with its card: a run
    that turned unknown while Home was open left them out until the page was loaded again."""
    from test_ui_text import STATIC, run_js

    keys = run_js(
        f"const k = await import({json.dumps((STATIC / 'tasks.js').as_uri())});\n"
        "const flow = (unknown) => ({ key: 'f', kind: 'flow', label: 'Export', url: 'https://x/f',"
        " waiting_for: unknown ? '' : 'the run', status: 'running', unknown, reported_at: '',"
        " last_check: null, next_check: null, trigger: '', changed_at: '' });\n"
        "const task = (unknown) => ({ id: 't', status: 'done', title: 'Export', session_id: 'w',"
        " updated_at: '', created_at: '', monitoring: [flow(unknown)] });\n"
        "s.state.snapshot = { settings: { max_workers: 3 }, sessions: [], tasks: [] };\n"
        "const phase = { section: 'monitoring', marker: 'delivered', why: '' };\n"
        "return [k.taskItem(task(false), phase).key, k.taskItem(task(true), phase).key];"
    )
    assert keys[0] != keys[1]
