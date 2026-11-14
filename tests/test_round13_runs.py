"""QA round 13, on runs (E51 to E59): a check-back is on the runs it names, not every run of its
task (E51); one set before a done report in the same turn stays, and the reminder goes by
whether each run still going is covered once the task moved (E52); a run the owner stopped
tracking is no longer said to be running (E53); a dev server's page is not something a task
delivered (E55); a finished task with nothing to wait on but runs of unknown status needs the
owner (E56); a GitHub Actions run with a query is read, and one that cannot be read is said so
(E59)."""

import json
from datetime import UTC, datetime, timedelta

from conftest import call_tool, settle
from test_round10_lifecycle import spawn, view
from test_round12_checkbacks import idle, turn
from test_round12_runs import ACTIONS, FLYTE, NIGHTLY, ago, delivered, said_at

from chiefofstaff import task_actions
from chiefofstaff.digest import needs, waiting_parts
from chiefofstaff.task_actions import RUN_UNKNOWN_HOURS, deliverable_kind

SHARDS = "https://flyte.acme.dev/console/projects/ml/domains/prod/executions/b7c1"
BACKFILL = {"url": SHARDS, "name": "Backfill", "status": "running"}


def flows(runtime, task_id: str) -> dict[str, dict]:
    return {
        item["label"]: item
        for item in view(runtime, task_id)["monitoring"]
        if item["kind"] == "flow"
    }


async def two_runs(harness, *, named: bool):
    """QA's E51: its worker declared the nightly export, then (another turn) the backfill with
    a check-back on it (naming it, or not), and went quiet: both said running 27 hours ago."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Export the nightly data")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="progress", summary="Export started.",
                    artifacts=[NIGHTLY])  # fmt: skip
    await idle(runtime, session, "Export started.")
    await turn(runtime, session, "Go on.")
    await call_tool(runtime, "report", worker, status="progress", summary="Backfill started.",
                    artifacts=[BACKFILL])  # fmt: skip
    told = await call_tool(runtime, "check_back", worker, minutes=60,
                           what="whether the backfill finished",
                           **({"runs": ["Backfill"]} if named else {}))  # fmt: skip
    await idle(runtime, session, "Waiting for the backfill.")
    for url in (FLYTE, SHARDS):
        said_at(harness, task["id"], url, at=ago(hours=27))
    return runtime, worker, task, told.text_result_for_llm


async def test_a_check_back_is_on_the_runs_it_names_not_every_run_of_its_task(harness):
    """E51: renewing a check-back on the newest run kept three older ones "running" for days."""
    runtime, _worker, task, told = await two_runs(harness, named=True)
    found = flows(runtime, task["id"])
    assert found["Nightly export"]["unknown"] is True
    assert found["Nightly export"]["trigger"].startswith("Nothing tells where it stands now")
    assert found["Backfill"]["unknown"] is False
    assert found["Backfill"]["trigger"] == "Its worker's check-back on it"
    # Its worker hears which it covers, and which it does not.
    assert "and \u201cBackfill\u201d" in told, told
    assert "Not covered: \u201cNightly export\u201d" in told, told


async def test_a_check_back_naming_no_run_is_on_those_declared_in_its_turn(harness):
    runtime, _worker, task, _told = await two_runs(harness, named=False)
    found = flows(runtime, task["id"])
    assert (found["Nightly export"]["unknown"], found["Backfill"]["unknown"]) == (True, False)


async def test_a_check_back_an_earlier_version_set_is_on_every_run(harness):
    """Set before check-backs named runs: it was on its whole task, and stays so."""
    runtime, _worker, task, _told = await two_runs(harness, named=True)
    kept = json.loads(harness.store.get(f"checkback:{task['id']}"))
    harness.store.set(
        f"checkback:{task['id']}",
        json.dumps({key: kept[key] for key in ("at", "what", "url", "session_id")}),
    )
    found = flows(runtime, task["id"])
    assert (found["Nightly export"]["unknown"], found["Backfill"]["unknown"]) == (False, False)


async def test_a_check_back_set_before_its_run_was_declared_in_the_turn_covers_it(harness):
    """E52: check_back, then report(done) with the run in the same turn: the check-back was
    dropped as the task went to review, and the reminder skipped (one was set this turn)."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Export the nightly data")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "check_back", worker, minutes=60,
                    what="whether the nightly export finished")  # fmt: skip
    first = await call_tool(runtime, "report", worker, status="done", summary="Exported.",
                            artifacts=[NIGHTLY])  # fmt: skip
    assert "set a check-back on this task in this turn" in first.text_result_for_llm
    again = await call_tool(runtime, "report", worker, status="done", summary="Exported.",
                            artifacts=[NIGHTLY])  # fmt: skip
    await idle(runtime, session, "Exported.")
    assert runtime.tasks.get(task["id"])["status"] == "review"
    kept = runtime.task_actions.check_back(runtime.tasks.get(task["id"]))
    assert kept and kept["what"] == "whether the nightly export finished", kept
    # Nothing to remind it of: the check-back covers the run.
    assert "call check_back" not in again.text_result_for_llm
    said_at(harness, task["id"], FLYTE, at=ago(hours=27))
    assert flows(runtime, task["id"])["Nightly export"]["unknown"] is False
    sent = len(session.sent)
    await runtime.task_actions.wake_check_backs(datetime.now(UTC) + timedelta(hours=2))
    await settle(runtime)
    assert len(session.sent) == sent + 1 and "which you reported done" in session.sent[-1]


async def test_a_check_back_not_on_the_run_still_going_leaves_the_reminder(harness):
    """E52: the reminder goes by whether each run still going is covered once the report moved
    the task, not by whether a check-back was set in the turn."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Export the nightly data")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="progress", summary="Started.",
                    artifacts=[NIGHTLY])  # fmt: skip
    await idle(runtime, session, "Started.")
    await turn(runtime, session, "Go on.")
    await call_tool(runtime, "check_back", worker, minutes=60, what="whether CI passed",
                    url="https://github.com/acme/app/pull/7")  # fmt: skip
    await call_tool(runtime, "report", worker, status="done", summary="Exported.")
    told = await call_tool(runtime, "report", worker, status="done", summary="Exported.")
    assert "\u201cNightly export\u201d still goes on" in told.text_result_for_llm
    assert "call check_back for it now" in told.text_result_for_llm
    assert runtime.task_actions.check_back(runtime.tasks.get(task["id"])) is None


async def test_a_github_run_needs_no_check_back_and_is_not_said_uncovered(harness):
    runtime = await harness.connect()
    worker, _task = await spawn(runtime, harness.router_id, "Deploy the app")
    session = harness.session(worker)
    await turn(runtime, session)
    deploy = {"url": ACTIONS, "name": "Deploy", "status": "running"}
    await call_tool(runtime, "report", worker, status="progress", summary="Deploying.",
                    artifacts=[deploy, NIGHTLY])  # fmt: skip
    await idle(runtime, session, "Deploying.")
    await turn(runtime, session, "Go on.")
    told = await call_tool(runtime, "check_back", worker, minutes=30, what="whether QA signed off")
    said = told.text_result_for_llm
    assert "Not covered: \u201cNightly export\u201d." in said, said


async def test_a_check_back_on_finished_work_must_name_a_run_still_going(harness):
    """E51: on finished work a check-back is only for its runs: one that covers none would
    wake nobody (it is dropped), so it is refused with why."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Export the nightly data")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="done", summary="Exported.",
                    artifacts=[NIGHTLY])  # fmt: skip
    await idle(runtime, session, "Exported.")
    await turn(runtime, session, "Anything else?")
    refused = await call_tool(runtime, "check_back", worker, task_id=task["id"], minutes=60,
                              what="whether it finished")  # fmt: skip
    assert (
        "Not set" in refused.text_result_for_llm and "Nightly export" in refused.text_result_for_llm
    )
    assert runtime.store.get(f"checkback:{task['id']}") is None
    await call_tool(runtime, "check_back", worker, task_id=task["id"], minutes=60,
                    what="whether it finished", runs=["nightly EXPORT"])  # fmt: skip
    assert runtime.task_actions.check_back(runtime.tasks.get(task["id"]))


async def test_the_check_back_wake_asks_for_every_run_still_running(harness):
    """E51: woken for one run, its worker is asked where each run said to be running stands."""
    runtime, worker, task, _told = await two_runs(harness, named=True)
    session = harness.session(worker)
    sent = len(session.sent)
    await runtime.task_actions.wake_check_backs(datetime.now(UTC) + timedelta(hours=2))
    await settle(runtime)
    assert len(session.sent) == sent + 1
    woke = session.sent[-1]
    assert "Still said to be running: \u201cNightly export\u201d, \u201cBackfill\u201d" in woke, (
        woke
    )
    assert task["id"] in woke


async def test_a_run_no_longer_tracked_is_not_said_to_run(harness):
    """E53: after Stop tracking, Artifacts still said "running" (blue), counted as running."""
    runtime = await harness.connect()
    _worker, task, _told = await delivered(harness, runtime, NIGHTLY)
    said_at(harness, task["id"], FLYTE, at=ago(hours=30))
    runtime.task_actions.end_runs(task["id"], [FLYTE], "untracked")
    [run] = [item for item in view(runtime, task["id"])["deliverables"] if item["kind"] == "flow"]
    assert (run["untracked"], run["said_by"], run["status"]) == (True, "you", "running")


def test_a_dev_servers_page_is_no_document(tmp_path, monkeypatch):
    """E55: a page on this computer (a name the hosts file maps here, a port of its own) was
    listed as a Doc, "running" after its task ended."""
    hosts = tmp_path / "hosts"
    hosts.write_text(
        "127.0.0.1 localhost\n# A comment\n10.1.2.3   devbox devbox.acme.test  # mine\n"
        "93.184.216.34 wiki.acme.test\n"
    )
    monkeypatch.setattr(task_actions, "HOSTS_FILE", (0.0, 0.0, {}))
    real = task_actions.Path
    monkeypatch.setattr(
        task_actions, "Path", lambda *parts: hosts if parts == ("/etc/hosts",) else real(*parts)
    )
    local = [
        "https://devbox.acme.test/report",
        "https://devbox/report",
        "http://localhost/report",
        "http://127.0.0.1/report",
        "http://[::1]/report",
        "http://192.168.1.20/report",
        "http://169.254.1.1/report",
        "https://preview.local/report",
        "https://app.localhost/report",
        "https://docs.acme.test:8443/report",
        "http://docs.acme.test:3000/dags/nightly/grid",
    ]
    assert {url: deliverable_kind(url, declared=True) for url in local} == dict.fromkeys(
        local, ("", "")
    )
    assert deliverable_kind("https://wiki.acme.test/page", declared=True)[0] == "doc"
    assert deliverable_kind("https://docs.acme.test:443/page", declared=True)[0] == "doc"


async def test_a_link_said_to_be_running_is_no_document(harness):
    """E55: a page its worker said is "running" is a server or a job, not a document; the
    task keeps it among its links."""
    runtime = await harness.connect()
    page = {"url": "https://preview.acme.dev/app", "name": "Preview", "status": "running"}
    doc = {"url": "https://wiki.acme.dev/plan", "name": "Plan", "status": "waiting for review"}
    _worker, task, _told = await delivered(harness, runtime, page, doc)
    shown = view(runtime, task["id"])
    assert [item["title"] for item in shown["deliverables"]] == ["Plan"]
    assert {item["url"] for item in shown["artifacts"]} == {page["url"], doc["url"]}


async def test_a_task_waiting_only_on_runs_of_unknown_status_needs_the_owner(harness):
    """E56: done, its last report "closed, nothing more to do", one run declared running two
    days before: Monitoring for days, its way out behind ⋯, no notice."""
    runtime = await harness.connect()
    worker, task, _told = await delivered(harness, runtime, NIGHTLY)
    session = harness.session(worker)
    shown = view(runtime, task["id"])
    assert shown["attention"] == ""  # Waited for: a day is not over.
    await turn(runtime, session, "Is it finished?")
    await call_tool(runtime, "report", worker, status="done", task_id=task["id"],
                    summary="Closed: nothing more to do.")  # fmt: skip
    await idle(runtime, session, "Closed.")
    runtime.task_actions.news_seen(task["id"])  # Its news is seen (Got it).
    said_at(harness, task["id"], FLYTE, at=ago(hours=RUN_UNKNOWN_HOURS + 24))
    shown = view(runtime, task["id"])
    assert shown["attention"] == "runs-unknown", shown["attention"]
    assert "\u201cNightly export\u201d" in shown["attention_why"]
    [run] = shown["monitoring"]
    assert run["unknown"] and run["unlisted"], run
    found = needs(runtime)
    assert found.markers[task["id"]] == "runs-unknown"
    assert any("run status unknown" in part for part in waiting_parts(found))
    runtime.task_actions.end_runs(task["id"], [FLYTE], "ended")
    assert view(runtime, task["id"])["attention"] == ""
    assert task["id"] not in needs(runtime).markers


async def test_a_run_still_listed_is_not_said_to_be_left_out(harness):
    runtime = await harness.connect()
    worker, task, _told = await delivered(harness, runtime, NIGHTLY)
    session = harness.session(worker)
    await turn(runtime, session, "Is it finished?")
    await call_tool(runtime, "report", worker, status="done", task_id=task["id"],
                    summary="The Nightly export still runs.")  # fmt: skip
    said_at(harness, task["id"], FLYTE, at=ago(hours=30))
    [run] = view(runtime, task["id"])["monitoring"]
    assert run["unknown"] and "unlisted" not in run


def test_a_github_actions_run_is_known_by_its_path():
    """E59: "…/actions/runs/123?pr=7" was a flow nobody read (the pattern took the whole link)."""
    focused = "https://www.github.com/acme/app/actions/runs/123?check_suite_focus=true"
    for url in (ACTIONS, ACTIONS + "/", ACTIONS + "?pr=7", ACTIONS + "#summary", focused):
        assert task_actions.github_run(url) == ("acme/app", "123"), url
    for url in ("https://github.com/acme/app/actions/runs/123/job/9",
                "https://github.com/acme/app/actions", "https://gitlab.com/acme/app/actions/runs/1",
                "http://github.com/acme/app/actions/runs/123"):  # fmt: skip
        assert task_actions.github_run(url) is None, url


async def test_a_run_with_a_query_is_read_on_github_and_a_failing_read_is_said(harness):
    """E59: never read; and one whose reads fail was still said to be read "every few
    minutes", its "reported … ago" from its worker's word, not the last read."""
    runtime = await harness.connect()
    run = {"url": ACTIONS + "?pr=7", "name": "Deploy", "status": "running"}
    _worker, task, told = await delivered(harness, runtime, run)
    assert "call check_back" not in told  # Chief of Staff reads it itself.
    harness.gh_replies["api repos/acme/app/actions/runs/123"] = json.dumps(
        {"status": "in_progress", "conclusion": None}
    )
    await runtime.task_actions.check_runs()
    assert harness.gh_calls[-1][0][:2] == ("api", "repos/acme/app/actions/runs/123")
    [watch] = flows(runtime, task["id"]).values()
    assert watch["last_check"] and watch["status"] == "in progress"
    read = watch["last_check"]
    harness.gh_replies["api repos/acme/app/actions/runs/123"] = RuntimeError(
        "GitHub request failed: HTTP 502: Bad Gateway (https://api.github.com/...)\nmore"
    )
    said_at(harness, task["id"], run["url"], checked_at=ago(hours=2), at=ago(hours=30))
    await runtime.task_actions.check_runs()
    [watch] = flows(runtime, task["id"]).values()
    assert watch["read_error"].startswith("HTTP 502: Bad Gateway"), watch
    assert watch["trigger"].startswith("Chief of Staff could not read it on GitHub (HTTP 502")
    assert "every few minutes" in watch["trigger"] and watch["last_check"] != read
    # Read since: no error left.
    harness.gh_replies["api repos/acme/app/actions/runs/123"] = json.dumps(
        {"status": "completed", "conclusion": "success"}
    )
    said_at(harness, task["id"], run["url"], tried_at=ago(hours=1))
    await runtime.task_actions.check_runs()
    known = json.loads(harness.store.get(f"artifacts:{task['id']}"))
    assert known[0]["status"] == "success" and "read_error" not in known[0]
