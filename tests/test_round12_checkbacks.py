"""QA round 12, review of E41: a check-back on finished work (a run its worker said goes on) is
that work's alone. Woken for it while another task of the worker runs, its silence is no report
of the other task, and its answer is never filed as a result of either; the Watchdog lets it
be. It is kept while the task goes back and forth between finished and not, and goes once its
runs are over."""

import json
import time
from datetime import UTC, datetime, timedelta

from conftest import call_tool, settle
from test_round10_lifecycle import spawn, view

from chiefofstaff.store import now

FLYTE = "https://flyte.acme.dev/console/projects/ml/domains/prod/executions/f3a9"
ACTIONS = "https://github.com/acme/app/actions/runs/123"
NIGHTLY = {"url": FLYTE, "name": "Nightly export", "status": "running"}
ENDED = NIGHTLY | {"status": "succeeded"}
LONG = (
    "Checked the nightly export: it is still running, about two thirds of the shards are done, "
    "so I will look again in an hour."
)


async def turn(runtime, session, text: str = ""):
    """Its next turn: on the message it was sent last (or `text`)."""
    session.emit("user.message", content=text or session.sent[-1])
    await settle(runtime)


async def idle(runtime, session, answer: str = ""):
    if answer:
        session.emit("assistant.message", content=answer)
    session.emit("session.idle")
    await settle(runtime)


async def checking(harness, *, with_b: bool = True):
    """Task A reported done with its run going and a check-back on it, accepted; task B given
    to the same worker since, running. Returns runtime, worker, A, B (or None), its session."""
    runtime = await harness.connect()
    worker, a = await spawn(runtime, harness.router_id, "Export the nightly data")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="done", summary="Exported.",
                    artifacts=[NIGHTLY])  # fmt: skip
    await call_tool(runtime, "check_back", worker, task_id=a["id"], minutes=60,
                    what="whether the nightly export finished", url=FLYTE)  # fmt: skip
    await idle(runtime, session)
    runtime.task_actions.accept(a["id"])
    b = None
    if with_b:
        given = {"task": "new", "why_new": "test", "earlier": "keep"}
        await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                        title="Write the release notes", text="Write it.", **given)  # fmt: skip
        b = next(t for t in runtime.tasks.for_session(worker) if t["title"].startswith("Write"))
        await turn(runtime, session)
        await call_tool(runtime, "report", worker, status="progress", task_id=b["id"],
                        summary="Drafting the notes.")  # fmt: skip
        await idle(runtime, session, "Drafting the notes.")
        assert runtime.tasks.get(b["id"])["status"] == "running"
    return runtime, worker, runtime.tasks.get(a["id"]), b, session


async def woken(runtime, session, hours: float = 2):
    sent = len(session.sent)
    await runtime.task_actions.wake_check_backs(datetime.now(UTC) + timedelta(hours=hours))
    await settle(runtime)
    assert len(session.sent) == sent + 1 and "which you reported done" in session.sent[-1]
    await turn(runtime, session)


def nudged(session) -> list[str]:
    return [text for text in session.sent if "report-missing" in text]


async def test_a_check_back_on_finished_work_is_no_other_tasks_report(harness):
    """Review 1: woken for A while B ran, it said "not yet" and checked back again: it was
    asked for a report tagged to B, and the next silent turn filed its words as B's result."""
    runtime, worker, a, b, session = await checking(harness)
    await woken(runtime, session)
    await call_tool(runtime, "check_back", worker, task_id=a["id"], minutes=60,
                    what="whether the nightly export finished", url=FLYTE)  # fmt: skip
    await idle(runtime, session, "Still running.")
    assert not nudged(session), "no report is owed on B for a check on A"
    await idle(runtime, session)
    assert (
        runtime.tasks.get(b["id"])["status"] == "running"
        and not runtime.tasks.get(b["id"])["result"]
    )
    shown = runtime.tasks.get(a["id"])
    assert (shown["status"], shown["result"]) == ("done", a["result"])
    assert not view(runtime, a["id"])["news"]


async def test_a_long_answer_on_a_check_is_never_filed_as_a_result(harness):
    """Review 1: a long answer was filed as report(done, A); refused once, it went through on
    the next wake and replaced A's accepted result (and brought it under Needs you)."""
    runtime, worker, a, _b, session = await checking(harness)
    for _cycle in range(2):
        await woken(runtime, session)
        await call_tool(runtime, "check_back", worker, task_id=a["id"], minutes=60,
                        what="whether the nightly export finished", url=FLYTE)  # fmt: skip
        await idle(runtime, session, LONG)
    shown = runtime.tasks.get(a["id"])
    assert (shown["status"], shown["result"]) == ("done", a["result"])
    assert not view(runtime, a["id"])["news"] and not nudged(session)
    assert not [text for kind, text in harness.kinds(worker) if kind == "report" and LONG in text]


async def test_a_check_with_no_new_check_back_files_nothing(harness):
    """Review 1: woken, it answered at length and set no new check-back (the run nearly done):
    filed as A's result, replacing the accepted one."""
    runtime, _worker, a, _b, session = await checking(harness)
    await woken(runtime, session)
    await idle(runtime, session, LONG)
    shown = runtime.tasks.get(a["id"])
    assert (shown["status"], shown["result"]) == ("done", a["result"])
    assert not view(runtime, a["id"])["news"] and not nudged(session)


async def test_a_note_on_finished_work_with_its_run_checked_on_is_no_report(harness):
    """Review 1: the Router's note about A (its run checked on later) while B runs: the answer
    to it was filed as A's result, or B was asked to report."""
    runtime, worker, a, _b, session = await checking(harness)
    note = {"task": "note", "task_id": a["id"], "text": "The export window moved to 2am."}
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, **note)
    await turn(runtime, session)
    await idle(runtime, session, LONG)
    shown = runtime.tasks.get(a["id"])
    assert (shown["status"], shown["result"]) == ("done", a["result"])
    assert not view(runtime, a["id"])["news"] and not nudged(session)


async def test_b_s_own_check_back_still_holds_its_report(harness):
    """Review 1, B with a check-back of its own: neither is asked for a report."""
    runtime, worker, a, b, session = await checking(harness)
    await turn(runtime, session, "go on")
    await call_tool(runtime, "check_back", worker, task_id=b["id"], minutes=600,
                    what="whether the release is tagged")  # fmt: skip
    await idle(runtime, session)
    await woken(runtime, session)
    await idle(runtime, session, LONG)
    assert not nudged(session)
    assert harness.store.get(f"checkback:{b['id']}"), "B's check-back stands"
    assert runtime.tasks.get(a["id"])["result"] == a["result"]


async def test_the_watchdog_lets_a_check_on_finished_work_be(harness):
    """Review 1: idle after a check on A, with B running and no check-back on B, the Watchdog
    took it for a worker that stopped without reporting."""
    runtime, worker, a, _b, session = await checking(harness)
    await woken(runtime, session)
    await call_tool(runtime, "check_back", worker, task_id=a["id"], minutes=60,
                    what="whether the nightly export finished", url=FLYTE)  # fmt: skip
    await idle(runtime, session, "Still running.")
    later = time.time() + 2 * 3600
    assert worker not in await runtime.watchdog.check(later)


async def test_the_watchdog_still_flags_a_worker_that_stopped_on_its_running_task(harness):
    runtime, worker, _a, b, session = await checking(harness)
    await turn(
        runtime,
        session,
        '<message from="router" type="message" task="' + b["id"] + '">More</message>',
    )
    runtime.add_message(worker, "router", "More", {"task_id": b["id"], "prompt": "More"})
    await idle(runtime, session)
    assert worker in await runtime.watchdog.check(time.time() + 2 * 3600)


async def test_a_check_back_on_finished_work_survives_its_question_being_answered(harness):
    """Review 2: asked about finished work (waiting), answered, back to done: its check-back
    on the run was dropped, and the run never checked again."""
    runtime, worker, a, _b, session = await checking(harness, with_b=False)
    await turn(runtime, session, "go on")
    await call_tool(runtime, "report", worker, status="question", task_id=a["id"],
                    summary="Rerun the failed shard now, or wait for the retry?")  # fmt: skip
    assert runtime.tasks.get(a["id"])["status"] == "waiting"
    await call_tool(runtime, "report", worker, status="progress", task_id=a["id"],
                    summary="Waiting for the nightly retry, as you said.")  # fmt: skip
    assert runtime.tasks.get(a["id"])["status"] == "done"
    assert harness.store.get(f"checkback:{a['id']}"), "still checked on when it ends"
    assert runtime.task_actions.check_back(runtime.tasks.get(a["id"]))


async def test_a_check_back_on_its_run_survives_request_changes_and_a_new_result(harness):
    """Review 2: review → running (Request changes) → review dropped it."""
    runtime = await harness.connect()
    worker, a = await spawn(runtime, harness.router_id, "Export the nightly data")
    await call_tool(runtime, "report", worker, status="done", summary="Exported.",
                    artifacts=[NIGHTLY])  # fmt: skip
    await call_tool(runtime, "check_back", worker, task_id=a["id"], minutes=60,
                    what="whether the nightly export finished", url=FLYTE)  # fmt: skip
    await runtime.task_actions.request_changes(a["id"], "Also export the archive table.")
    assert runtime.tasks.get(a["id"])["status"] == "running"
    await turn(runtime, harness.session(worker))
    await call_tool(runtime, "report", worker, status="done", task_id=a["id"],
                    summary="Exported, the archive table too.")  # fmt: skip
    assert runtime.tasks.get(a["id"])["status"] == "review"
    assert harness.store.get(f"checkback:{a['id']}")
    # Its run over, the next result drops it.
    await call_tool(runtime, "report", worker, status="progress", task_id=a["id"],
                    summary="It finished.", artifacts=[ENDED])  # fmt: skip
    assert runtime.task_actions.check_back(runtime.tasks.get(a["id"])) is None


async def test_a_check_back_on_a_run_that_ended_while_reopened_goes_with_its_result(harness):
    """Review 2: kept only while its runs go on."""
    runtime = await harness.connect()
    worker, a = await spawn(runtime, harness.router_id, "Export the nightly data")
    await call_tool(runtime, "report", worker, status="done", summary="Exported.",
                    artifacts=[NIGHTLY])  # fmt: skip
    await call_tool(runtime, "check_back", worker, task_id=a["id"], minutes=60,
                    what="whether the nightly export finished", url=FLYTE)  # fmt: skip
    await runtime.task_actions.request_changes(a["id"], "Also export the archive table.")
    await turn(runtime, harness.session(worker))
    await call_tool(runtime, "report", worker, status="progress", task_id=a["id"],
                    summary="It finished.", artifacts=[ENDED])  # fmt: skip
    assert harness.store.get(f"checkback:{a['id']}"), "still at work on it: its own to keep"
    await call_tool(runtime, "report", worker, status="done", task_id=a["id"],
                    summary="Exported, the archive table too.")  # fmt: skip
    assert runtime.tasks.get(a["id"])["status"] == "review"
    assert not harness.store.get(f"checkback:{a['id']}")


async def test_a_check_back_left_on_finished_work_with_no_run_going_counts_for_nothing(harness):
    """Review 3: whatever left it (no path does now), it keeps nothing in Monitoring."""
    runtime, worker, a, _b, _session = await checking(harness, with_b=False)
    harness.store.set(f"artifacts:{a['id']}", json.dumps([ENDED | {"at": now()}]))
    assert harness.store.get(f"checkback:{a['id']}")
    assert runtime.task_actions.check_back(runtime.tasks.get(a["id"])) is None
    assert view(runtime, a["id"])["monitoring"] == [] and runtime.workers.finished(worker)


async def test_a_check_back_goes_once_its_run_is_over(harness):
    """Review 3: the run read on GitHub as finished, its check-back kept the task in Monitoring
    and its worker open for the seven days it was set for."""
    runtime = await harness.connect()
    worker, a = await spawn(runtime, harness.router_id, "Deploy the app")
    deploy = {"url": ACTIONS, "name": "Deploy", "status": "in progress"}
    await call_tool(runtime, "report", worker, status="done", summary="Deploying.",
                    artifacts=[deploy])  # fmt: skip
    await call_tool(runtime, "check_back", worker, task_id=a["id"], minutes=7 * 24 * 60,
                    what="whether the deploy finished", url=ACTIONS)  # fmt: skip
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    runtime.task_actions.accept(a["id"])
    assert view(runtime, a["id"])["monitoring"] and not runtime.workers.finished(worker)
    path = "api repos/acme/app/actions/runs/123"
    harness.gh_replies[path] = json.dumps({"status": "completed", "conclusion": "success"})
    await runtime.task_actions.check_runs()
    assert view(runtime, a["id"])["monitoring"] == []
    assert runtime.workers.finished(worker)
    assert not harness.store.get(f"checkback:{a['id']}")


async def test_a_check_back_goes_when_the_owner_marks_its_run_ended(harness):
    runtime, worker, a, _b, _session = await checking(harness, with_b=False)
    harness.store.set(
        f"artifacts:{a['id']}",
        json.dumps([NIGHTLY | {"at": (datetime.now(UTC) - timedelta(days=2)).isoformat()}]),
    )
    runtime.task_actions.end_runs(a["id"], [FLYTE], "ended")
    assert not harness.store.get(f"checkback:{a['id']}")
    assert view(runtime, a["id"])["monitoring"] == [] and runtime.workers.finished(worker)


async def test_a_check_back_goes_when_its_worker_says_the_run_ended_or_it_is_set_aside(harness):
    runtime, worker, a, _b, session = await checking(harness, with_b=False)
    await turn(runtime, session, "go on")
    await call_tool(runtime, "report", worker, status="progress", task_id=a["id"],
                    summary="It finished.", artifacts=[ENDED])  # fmt: skip
    assert runtime.task_actions.check_back(runtime.tasks.get(a["id"])) is None
    assert view(runtime, a["id"])["monitoring"] == []
    # Going again, then set aside: nothing to check either.
    await call_tool(runtime, "report", worker, status="progress", task_id=a["id"],
                    summary="Rerun.", artifacts=[NIGHTLY])  # fmt: skip
    await call_tool(runtime, "check_back", worker, task_id=a["id"], minutes=60,
                    what="whether the rerun finished", url=FLYTE)  # fmt: skip
    assert runtime.task_actions.check_back(runtime.tasks.get(a["id"]))
    harness.store.set(f"setaside:{a['id']}", "1")
    assert runtime.task_actions.check_back(runtime.tasks.get(a["id"])) is None


async def test_a_check_back_on_a_running_task_still_owes_its_report(harness):
    """Only finished work is let be: woken on a task it is still on, and silent, it is asked to
    report as before."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Export the nightly data")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "check_back", worker, task_id=task["id"], minutes=60,
                    what="whether the nightly export finished", url=FLYTE)  # fmt: skip
    await idle(runtime, session)
    await runtime.task_actions.wake_check_backs(datetime.now(UTC) + timedelta(hours=2))
    await settle(runtime)
    await turn(runtime, session)
    await idle(runtime, session, "Looked.")
    assert nudged(session) and f'task="{task["id"]}"' in nudged(session)[-1]
