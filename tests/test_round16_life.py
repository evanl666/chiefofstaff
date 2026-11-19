"""QA round 7 on 0.1.46, a task's life: a worker waiting for the owner's word on its folder's
settings is under Needs you; an ended task follows nothing, and its Retry brings back what it
followed and tells the workers that were told it would not come; a replaced task hands its
dependents on; a check-back the owner's close ends, or that failed on accepted work, is theirs
to settle; focus ended early, a merged PR followed on request, the Watch scans that fail, an
undone split, a failed task about to leave Needs you."""

import json
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest
from conftest import Harness, call_tool, settle
from test_progress import busy_worker
from test_round10_lifecycle import spawn, view
from test_round12_checkbacks import checking
from test_ui_text import board, task
from test_web import ORIGIN, app_client, sign_in  # noqa: F401  (app_client: a fixture)

from chiefofstaff.digest import needs
from chiefofstaff.models import Settings, Watch
from chiefofstaff.store import hours_ago

PR = "github:acme/app#7"


def prompts(harness, session_id: str) -> list[str]:
    return [m["meta"]["prompt"] for m in harness.store.prompts(session_id)]


# E99: an ended task follows nothing; its Retry follows it again.


async def test_a_cancelled_task_stops_following_its_pr_and_its_retry_follows_it_again(harness):
    runtime = await harness.connect()
    worker, fix = await spawn(runtime, harness.router_id, "Fix login", follow=[PR])
    assert PR in harness.store.follows(worker)
    await runtime.task_actions.cancel(fix["id"])
    await settle(runtime)
    assert PR not in harness.store.follows(worker), "nothing of it is wanted now"
    sent = len(harness.session(worker).sent)
    await runtime.on_thread("github", PR, ["github:issuecomment:1"], "bob: rename the flag")
    await settle(runtime)
    assert len(harness.session(worker).sent) == sent, "news there wakes no one for it"
    await runtime.task_actions.retry(fix["id"])
    assert PR in harness.store.follows(worker), "wanted again: followed again"
    assert fix["id"] in harness.store.follow_tasks(worker, PR)
    await runtime.on_thread("github", PR, ["github:issuecomment:2"], "bob: and the docs")
    await settle(runtime)
    assert "and the docs" in harness.session(worker).sent[-1]


async def test_a_failed_task_stops_following_and_news_is_never_its_own(harness):
    runtime = await harness.connect()
    worker, fix = await spawn(runtime, harness.router_id, "Fix login", follow=[PR])
    await call_tool(runtime, "report", worker, status="failed", summary="No access to the repo.")
    await settle(runtime)
    assert runtime.tasks.get(fix["id"])["status"] == "failed"
    assert PR not in harness.store.follows(worker)
    # Bound to it before (an earlier version kept it): its news is not that task's.
    harness.store.follow(PR, worker)
    harness.store.set(f"followtask:{worker}:{PR}", json.dumps([fix["id"]]))
    assert runtime.thread_task(worker, PR) is None
    await runtime.task_actions.retry(fix["id"])
    assert PR in harness.store.follows(worker)
    assert runtime.thread_task(worker, PR)["id"] == fix["id"]


# E103: told it would not come, told when it comes back.


async def test_a_retried_task_tells_its_peers_and_a_later_failure_is_told_again(harness):
    runtime = await harness.connect()
    router = harness.router_id
    api, api_task = await spawn(runtime, router, "Orders API")
    client, _ = await spawn(runtime, router, "Orders client", works_with=["Orders API"])
    session = harness.session(client)
    await call_tool(runtime, "report", api, status="failed", summary="The schema is locked.")
    await settle(runtime)
    assert 'type="peer-stopped"' in session.sent[-1]
    await runtime.task_actions.retry(api_task["id"])
    await settle(runtime)
    assert 'type="peer-back"' in session.sent[-1], session.sent[-1]
    assert "Orders API" in session.sent[-1]
    assert not harness.store.get(f"peerstop:{api_task['id']}")
    await call_tool(runtime, "report", api, status="failed", summary="Locked again.")
    await settle(runtime)
    assert 'type="peer-stopped"' in session.sent[-1], "a second failure is told too"


async def test_a_stopped_note_still_held_is_withdrawn_not_sent_late(harness):
    runtime = await harness.connect()
    router = harness.router_id
    api, api_task = await spawn(runtime, router, "Orders API")
    client, _ = await spawn(runtime, router, "Orders client", works_with=["Orders API"])
    session = harness.session(client)
    refusal = runtime.settings_refusal

    async def waits(row):  # The client's folder's settings changed: nothing reaches it.
        return "settings changed" if row["id"] == client else await refusal(row)

    runtime.settings_refusal = waits
    sent = len(session.sent)
    await call_tool(runtime, "report", api, status="failed", summary="The schema is locked.")
    await settle(runtime)
    assert len(session.sent) == sent, "held for the owner's word"
    await runtime.task_actions.retry(api_task["id"])
    await settle(runtime)
    stopped = [
        m for m in harness.store.messages(client) if "will not come" in m["text"]
    ]  # fmt: skip
    assert stopped and stopped[-1]["meta"].get("dropped"), "withdrawn, never sent"
    assert not any('type="peer-back"' in p for p in prompts(harness, client)), "nothing to undo"
    runtime.settings_refusal = refusal
    await runtime.delivery.deliver(client, "Go on.", kind="router")
    await settle(runtime)
    assert not any("will not come" in text for text in session.sent)


# E104: a replaced task's dependents wait for what replaces it.


async def test_a_task_waiting_on_a_replaced_task_waits_for_its_replacement(harness):
    runtime = await harness.connect()
    router = harness.router_id
    api, a = await spawn(runtime, router, "Build the API")
    _client, c = await spawn(runtime, router, "Build the client", after=[a["id"]])
    await call_tool(runtime, "report", api, status="failed", summary="Wrong base branch.")
    await runtime.workers.pump()
    await settle(runtime)
    assert harness.store.get(f"depblocked:{c['id']}")
    result = await call_tool(runtime, "spawn_worker", router, title="Build the API again",
                             instructions="Build it on main.", why_new="test",
                             replaces=[a["id"]])  # fmt: skip
    assert result.result_type == "success", result.text_result_for_llm
    a2 = json.loads(result.text_result_for_llm)["task_id"]
    assert [dep["id"] for dep in runtime.tasks.deps(c["id"])] == [a2]
    assert not harness.store.get(f"depblocked:{c['id']}")
    assert not view(runtime, c["id"])["blocked"]
    assert c["id"] in result.text_result_for_llm, "the Router hears which waits for it now"
    told = len(harness.store.messages(router))
    await runtime.workers.pump()
    await settle(runtime)
    assert not [
        m for m in harness.store.messages(router)[told:] if m["meta"].get("task_id") == c["id"]
    ], "nor that it is blocked, or no longer is"


# E105: the owner's final text, empty too.


async def test_a_review_summary_the_owner_removed_stays_removed_after_a_failed_send(harness):
    runtime = await harness.connect()
    worker, _ = await spawn(runtime, harness.router_id, "Review acme/app#7")
    target = {"repo": "acme/app", "number": 7, "verdict": "comment",
              "comments": [{"path": "app.py", "line": 3, "body": "Off by one?"}]}  # fmt: skip
    draft = harness.store.create_draft(worker, "github", target, "Looks good overall.")

    async def down(*arguments, **options):
        raise RuntimeError("GitHub is temporarily unavailable (502)")

    runtime.github.publish_review = down
    with suppress(RuntimeError):
        await runtime.drafts.decide(draft["id"], decision("send", content={"body": ""}))
    after = harness.store.draft(draft["id"])
    assert after["status"] == "pending" and after["final_text"] == ""
    assert after["has_final"], "the owner's own text is empty: not the drafted one"
    assert not harness.store.draft(
        harness.store.create_draft(worker, "github", target, "Other.")["id"]
    )["has_final"]


def decision(action, **fields):
    from chiefofstaff.models import DraftDecision

    return DraftDecision(action=action, **fields)


def test_the_page_shows_and_sends_the_owners_own_text_an_empty_one_too():
    from test_ui_text import run_js

    found = run_js(
        "return [s.finalText({ text: 'Summary', final_text: '', has_final: 1 }),"
        " s.finalText({ text: 'Summary', final_text: '', has_final: 0 })];"
    )
    assert found == ["", "Summary"]


# E107: a check-back the owner (or the Router) closed the worker on is over.


async def test_a_check_back_does_not_reopen_a_worker_the_owner_closed(harness):
    runtime, worker, a, _b, _session = await checking(harness, with_b=False)
    assert runtime.task_actions.check_back(runtime.tasks.get(a["id"]))
    await runtime.workers.close(worker, by="you")
    assert not harness.store.get(f"checkback:{a['id']}")
    await runtime.task_actions.wake_check_backs(datetime.now(UTC) + timedelta(hours=2))
    await settle(runtime)
    assert harness.store.session(worker)["closed_at"], "closed as the owner left it"


async def test_a_check_back_reopens_a_worker_closed_for_being_idle(harness):
    runtime, worker, a, _b, _session = await checking(harness, with_b=False)
    await runtime.workers.close(worker)  # Chief of Staff's own: nothing of it was open.
    assert harness.store.get(f"checkback:{a['id']}")
    await runtime.task_actions.wake_check_backs(datetime.now(UTC) + timedelta(hours=2))
    await settle(runtime)
    assert not harness.store.session(worker)["closed_at"], "reopened when due"


# E109: a failed check-back on accepted work, one rule on Home and on the server.


async def failed_check_back(harness):
    runtime, worker, a, _b, session = await checking(harness, with_b=False)
    found = json.loads(harness.store.get(f"checkback:{a['id']}"))
    found |= {"attempts": 3, "error": "Copilot is not running"}
    harness.store.set(f"checkback:{a['id']}", json.dumps(found))
    return runtime, worker, a, session


async def test_a_failed_check_back_on_accepted_work_needs_the_owner_on_the_server_too(harness):
    runtime, _worker, a, _session = await failed_check_back(harness)
    shown = view(runtime, a["id"])
    assert shown["status"] == "done" and shown["attention"] == "check-failed"
    assert a["id"] in needs(runtime).markers
    assert needs(runtime).markers[a["id"]] == "check-failed"


async def test_a_failed_check_back_is_retried_or_dropped_by_the_owner(harness):
    runtime, _worker, a, session = await failed_check_back(harness)
    sent = len(session.sent)
    await runtime.task_actions.check_back_again(a["id"])
    await settle(runtime)
    assert len(session.sent) == sent + 1 and "which you reported done" in session.sent[-1]
    assert view(runtime, a["id"])["attention"] != "check-failed"
    runtime, _worker, b, _session = await failed_check_back(Harness(harness.store.directory.parent
                                                                    / "again"))  # fmt: skip
    runtime.task_actions.stop_check_back(b["id"])
    shown = view(runtime, b["id"])
    assert not shown["check_back"] and shown["attention"] != "check-failed"
    assert not runtime.task_actions.runs_going(b["id"]), "nothing is followed up for it now"


# E83: a retried task cancelled, or its worker closed, is cancelled (not Failed again).


async def failed_then_retried(harness, title="Plan the expansion"):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, title)
    await call_tool(runtime, "report", worker, status="failed", summary="Context too large.")
    await settle(runtime)
    await runtime.task_actions.retry(task["id"])
    await settle(runtime)
    assert runtime.tasks.get(task["id"])["status"] == "running"
    return runtime, worker, task


async def test_cancelling_a_retried_task_cancels_it(harness):
    runtime, _worker, task = await failed_then_retried(harness)
    await runtime.task_actions.cancel(task["id"], "Replaced.", by="the Router")
    assert runtime.tasks.get(task["id"])["status"] == "cancelled"


async def test_closing_the_worker_of_a_retried_task_cancels_it(harness):
    runtime, worker, task = await failed_then_retried(harness)
    await runtime.workers.close(worker, by="the Router")
    assert runtime.tasks.get(task["id"])["status"] == "cancelled"


async def test_the_routers_continue_on_a_failed_task_then_a_cancel_cancels_it(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Plan the expansion")
    await call_tool(runtime, "report", worker, status="failed", summary="Context too large.")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    task="continue", task_id=task["id"], text="Try again with less.")  # fmt: skip
    assert runtime.tasks.get(task["id"])["status"] == "running"
    await runtime.task_actions.cancel(task["id"])
    assert runtime.tasks.get(task["id"])["status"] == "cancelled"


async def test_a_replaced_task_is_not_retried_and_a_retry_says_it_reopens_a_closed_worker(
    harness,
):
    runtime = await harness.connect()
    router = harness.router_id
    worker, task = await spawn(runtime, router, "Plan the expansion")
    await call_tool(runtime, "report", worker, status="failed", summary="Context too large.")
    result = await call_tool(runtime, "spawn_worker", router, title="Plan the expansion again",
                             instructions="Plan it.", why_new="test",
                             replaces=[task["id"]])  # fmt: skip
    new = json.loads(result.text_result_for_llm)["task_id"]
    with pytest.raises(ValueError, match=new):  # Open the task that replaced it.
        await runtime.task_actions.retry(task["id"])
    # Another failed task, its worker closed by the Router: a Retry reopens it, said so.
    other, failed = await spawn(runtime, router, "Count the rows")
    await call_tool(runtime, "report", other, status="failed", summary="No access.")
    await runtime.workers.close(other, by="the Router")
    await runtime.task_actions.retry(failed["id"])
    assert not harness.store.session(other)["closed_at"]
    told = [p for p in prompts(harness, router) if 'type="reopened"' in p and failed["id"] in p]
    assert told and "you closed" in told[-1]


# E84: a failed turn fails the task it was on, not the others.


async def test_a_failed_turn_fails_only_the_task_it_was_on(harness):
    runtime = await harness.connect()
    worker, session, coverage = await busy_worker(runtime, harness, "Measure coverage")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="new",
                    why_new="test", earlier="keep", title="Write the plan",
                    text="Then write the plan.")  # fmt: skip
    plan = runtime.tasks.current(worker)
    assert plan["id"] != coverage["id"]
    session.emit("session.error", message="Execution failed: invalid tool schema")
    await settle(runtime)
    assert runtime.tasks.get(coverage["id"])["status"] == "failed"
    assert runtime.tasks.get(plan["id"])["status"] == "running", "not taken up: not failed"
    told = [
        m for m in harness.store.messages(harness.router_id) if m["meta"].get("status") == "failed"
    ]
    assert told and told[-1]["meta"]["task_id"] == coverage["id"]


# E89: a failed task about to leave Needs you.


async def test_a_failed_task_is_told_once_before_it_leaves_needs_you(harness):
    runtime = await harness.connect()
    worker, fix = await spawn(runtime, harness.router_id, "Fix login")
    await call_tool(runtime, "report", worker, status="failed", summary="No access.")
    await settle(runtime)
    toasts = []
    runtime.bus.publish = lambda kind, data=None: toasts.append(data) if kind == "toast" else None
    await runtime.task_actions.remind_failed()
    assert not toasts, "not while it is new"
    runtime.tasks.update(fix["id"], ended_at=hours_ago(50))
    await runtime.task_actions.remind_failed()
    await runtime.task_actions.remind_failed()
    told = [toast["text"] for toast in toasts if "Fix login" in toast["text"]]
    assert len(told) == 1 and "retry or dismiss" in told[0], toasts
    lines = [m for m in harness.store.messages(harness.router_id) if "Fix login" in m["text"]]
    assert lines and lines[-1]["meta"].get("task_id") == fix["id"]


async def test_a_failed_task_past_needs_you_stays_under_recently_finished(harness):
    runtime = await harness.connect()
    worker, fix = await spawn(runtime, harness.router_id, "Fix login")
    await call_tool(runtime, "report", worker, status="failed", summary="No access.")
    runtime.tasks.update(fix["id"], ended_at=hours_ago(80))
    listed = {t["id"] for t in runtime.tasks.listed(include_ended_since=hours_ago(72))}
    assert fix["id"] in listed, "reachable still"
    shown = view(runtime, fix["id"])
    assert shown["aged"] and shown["attention"] == ""
    assert fix["id"] not in needs(runtime).markers
    found = board([task("x", "w1", "failed", aged=True), task("y", "w1", "failed")],
                  [{"id": "w1", "status": "failed"}])  # fmt: skip
    assert found["phases"] == {"x": "finished/failed", "y": "needs/failed"}


# E89: a failed task is dismissed without its worker.


async def test_a_failed_task_is_dismissed_without_reopening_its_worker(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Plan the expansion")
    await call_tool(runtime, "report", worker, status="failed", summary="Context too large.")
    await runtime.workers.close(worker, by="the Router")
    sent = len(harness.session(worker).sent)
    dismissed = await runtime.task_actions.cancel(task["id"])
    assert dismissed["status"] == "cancelled"
    assert harness.store.session(worker)["closed_at"], "nothing of its worker is reopened"
    assert len(harness.session(worker).sent) == sent
    other, failed = await spawn(runtime, harness.router_id, "Count the rows")
    await call_tool(runtime, "report", other, status="failed", summary="No access.")
    result = await call_tool(runtime, "cancel_task", harness.router_id, task_id=failed["id"],
                             reason="Not needed.")  # fmt: skip
    assert result.result_type == "success", result.text_result_for_llm
    assert runtime.tasks.get(failed["id"])["status"] == "cancelled"
