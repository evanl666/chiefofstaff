"""QA round 7 (bug bash E85 to E112): which task a worker's words and asks belong to. What
it answers the Router about finished or waiting work is no report of another task nor a new
result, and reaches the Router; a message with no task_id goes to its open task; the task it
asks a decision for, and is named after, is the one its turn is on; a decision card stays as
long as someone waits on it; checks that turn a report back hold for each message and each
failing run, not once; more on a task it reported reopens it when it reads it."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from conftest import call_tool, settle
from test_progress import busy_worker, step
from test_round10_lifecycle import spawn
from test_round12_checkbacks import NIGHTLY, checking, idle, turn, woken
from test_round13_ui import BOARD
from test_ui_text import run_js

from chiefofstaff.digest import needs
from chiefofstaff.models import DraftDecision

LONG = (
    "The fix is in src/auth/session.py: the cookie's max_age was in minutes, not seconds, so "
    "the login timed out after a minute."
)


def nudged(session) -> list[str]:
    return [text for text in session.sent if "report-missing" in text]


def router_inbound(harness) -> list[dict]:
    return [m for m in harness.store.messages(harness.router_id) if m["kind"] == "inbound"]


async def router_says(runtime, harness, worker, task, how, text):
    return await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                           task=how, task_id=task["id"], text=text)  # fmt: skip


def decision_cards(harness) -> list[dict]:
    return [d for d in harness.store.drafts() if d["kind"] == "decision"]


async def finished_and_running(harness):
    """Task B reported done (in review), then task A given to the same worker and at work.
    Returns runtime, worker, B, A, its session."""
    runtime = await harness.connect()
    worker, b = await spawn(runtime, harness.router_id, "Fix the login timeout")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="done", summary="Fixed the timeout.")
    await idle(runtime, session)
    given = {"task": "new", "why_new": "test", "title": "Write the release notes"}
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    text="Write them.", **given)  # fmt: skip
    a = runtime.tasks.current(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="progress", task_id=a["id"],
                    summary="Drafting the notes.")  # fmt: skip
    await call_tool(runtime, "check_back", worker, task_id=a["id"], minutes=60,
                    what="whether the notes build")  # fmt: skip
    await idle(runtime, session, "Drafting the notes.")
    harness.store.delete(f"checkback:{a['id']}")  # A has nothing to wait for now.
    assert runtime.tasks.get(b["id"])["status"] == "review"
    assert runtime.tasks.get(a["id"])["status"] == "running"
    return runtime, worker, runtime.tasks.get(b["id"]), runtime.tasks.get(a["id"]), session


async def test_an_answer_to_a_note_on_finished_work_is_no_report_of_the_running_task(harness):
    """E96: the Router asked about finished B while A ran; the worker answered in its chat. It
    was asked for a report tagged to A ("call report now with your full result")."""
    runtime, worker, b, a, session = await finished_and_running(harness)
    note = {"task": "note", "task_id": b["id"], "text": "Which file is the fix in?"}
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, **note)
    await turn(runtime, session)
    await idle(runtime, session, "src/auth/session.py")
    assert not nudged(session), "no report is owed on A for an answer about B"
    assert runtime.tasks.get(a["id"])["status"] == "running"
    shown = runtime.tasks.get(b["id"])
    assert (shown["status"], shown["result"]) == ("review", b["result"])
    answer = router_inbound(harness)[-1]
    assert answer["meta"].get("answered_by") == worker and answer["text"] == "src/auth/session.py"
    assert answer["meta"].get("task_id") == b["id"]


async def test_a_long_answer_to_a_note_on_finished_work_keeps_its_result(harness):
    """E96: a long answer was filed as B's done report: its result replaced, and a second
    "finished" for it."""
    runtime, worker, b, a, session = await finished_and_running(harness)
    notified = len(harness.notifications)
    note = {"task": "note", "task_id": b["id"], "text": "Which file is the fix in?"}
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, **note)
    await turn(runtime, session)
    await idle(runtime, session, LONG)
    shown = runtime.tasks.get(b["id"])
    assert (shown["status"], shown["result"]) == ("review", b["result"])
    assert not [text for kind, text in harness.kinds(worker) if kind == "report" and LONG in text]
    assert not nudged(session) and runtime.tasks.get(a["id"])["status"] == "running"
    assert not [n for n in harness.notifications[notified:] if "finished" in n[0].lower()]
    assert router_inbound(harness)[-1]["text"] == LONG


async def test_an_answer_to_a_note_on_work_checked_on_reaches_the_router(harness):
    """E85: with a check-back set on finished B, the answer to the Router's note about it went
    nowhere: neither filed (rightly) nor passed on."""
    runtime, worker, b, _a, session = await finished_and_running(harness)
    harness.store.set(f"checkback:{b['id']}", '{"at": "2999-01-01T00:00:00+00:00", "what": "x"}')
    note = {"task": "note", "task_id": b["id"], "text": "Which file is the fix in?"}
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, **note)
    await turn(runtime, session)
    await idle(runtime, session, LONG)
    assert router_inbound(harness)[-1]["text"] == LONG
    assert runtime.tasks.get(b["id"])["result"] == b["result"] and not nudged(session)


async def test_an_acknowledgement_of_a_note_is_not_passed_on(harness):
    runtime, worker, b, _a, session = await finished_and_running(harness)
    before = len(router_inbound(harness))
    note = {"task": "note", "task_id": b["id"], "text": "The owner merged it."}
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, **note)
    await turn(runtime, session)
    await idle(runtime, session, "Thanks, noted.")
    assert len(router_inbound(harness)) == before and not nudged(session)


async def test_a_retried_turn_answering_a_note_on_finished_work_is_still_no_report(harness):
    """E96 after a retry: the turn on the Router's note failed on Copilot's side after a step,
    and the note to finish it became the turn's instruction: its answer was asked of the running
    task as a report (or filed as one), not passed on."""
    runtime, worker, b, a, session = await finished_and_running(harness)
    note = {"task": "note", "task_id": b["id"], "text": "Which file is the fix in?"}
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, **note)
    await turn(runtime, session)
    step(session, "bash", {"command": "git log -1 --stat"})
    session.emit("session.error", message="Execution failed: 503 Service Unavailable")
    await settle(runtime)
    await asyncio.sleep(0.05)
    await settle(runtime)
    assert "finish what is left of it" in session.sent[-1], "retried with a note"
    await turn(runtime, session)
    await idle(runtime, session, LONG)
    assert not nudged(session), "no report is owed on A"
    assert runtime.tasks.get(a["id"])["status"] == "running"
    shown = runtime.tasks.get(b["id"])
    assert (shown["status"], shown["result"]) == ("review", b["result"])
    answer = router_inbound(harness)[-1]
    assert answer["text"] == LONG and answer["meta"].get("answered_by") == worker


async def test_an_answer_to_the_routers_question_on_work_with_a_check_back_reaches_it(harness):
    """E85: the task waited on a run (a check-back set); the Router asked its worker for the
    running flows (a continue) and the worker answered in its chat without a report: the answer
    reached neither the Router nor the owner."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Run the experiments")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "check_back", worker, task_id=task["id"], minutes=60,
                    what="whether the flows finished")  # fmt: skip
    await idle(runtime, session, "Started; I will look again in an hour.")
    await router_says(runtime, harness, worker, task, "continue", "List the running flows.")
    await turn(runtime, session)
    await idle(runtime, session, LONG)
    answer = router_inbound(harness)[-1]
    assert answer["text"] == LONG and answer["meta"].get("answered_by") == worker
    shown = runtime.tasks.get(task["id"])
    assert shown["status"] == "running" and not shown["result"] and not nudged(session)


async def test_a_check_backs_own_wake_is_not_passed_on(harness):
    runtime, _worker, _a, _b, session = await checking(harness, with_b=False)
    before = len(router_inbound(harness))
    await woken(runtime, session)
    await idle(runtime, session, LONG)
    assert len(router_inbound(harness)) == before


async def two_given(harness):
    """Task A at work, then task B given (earlier="keep") while its turn on A goes on."""
    runtime = await harness.connect()
    worker, a = await spawn(runtime, harness.router_id, "Fix the login")
    session = harness.session(worker)
    await turn(runtime, session)
    given = {"task": "new", "why_new": "test", "earlier": "keep", "title": "Write the docs"}
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    text="Then write the docs.", **given)  # fmt: skip
    b = runtime.tasks.current(worker)
    assert b["id"] != a["id"] and runtime.working_on(worker) == a["id"]
    return runtime, worker, a, b, session


async def test_a_decision_is_asked_for_the_task_at_work_not_the_one_given_last(harness):
    """E101 (E86): the card said "Decision for Write the docs", that task waited for it and
    the login fix ran on; the answer went to the docs."""
    runtime, worker, a, b, _session = await two_given(harness)
    await call_tool(runtime, "ask_decision", worker, question="Keep the old login page?")
    card = next(d for d in harness.store.drafts() if d["kind"] == "decision")
    assert [x["task_id"] for x in card["target"]["askers"]] == [a["id"]]
    assert card["target"]["askers"][0]["title"] == "Fix the login"
    assert runtime.tasks.get(a["id"])["status"] == "waiting"
    assert runtime.tasks.get(b["id"])["status"] == "running"


async def test_a_decision_names_its_task(harness):
    runtime, worker, a, b, _session = await two_given(harness)
    await call_tool(runtime, "ask_decision", worker, question="Docs in English only?",
                    task_id=b["id"])  # fmt: skip
    card = next(d for d in harness.store.drafts() if d["kind"] == "decision")
    assert [x["task_id"] for x in card["target"]["askers"]] == [b["id"]]
    assert runtime.tasks.get(a["id"])["status"] == "running"
    wrong = await call_tool(runtime, "ask_decision", worker, question="x?", task_id="tnope")
    assert wrong.result_type == "failure" and "tnope" in wrong.text_result_for_llm


async def stale_pointer(harness):
    """Task B open (waiting on a run), then the Router's continue on finished A, reported and
    accepted: the worker's pointer (the task given last) is done A, its open work B."""
    runtime = await harness.connect()
    worker, a = await spawn(runtime, harness.router_id, "Run the experiments")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="done", summary="Ran them.")
    await idle(runtime, session)
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="new",
                    why_new="test", title="Run the ablation", text="Run it.")  # fmt: skip
    b = runtime.tasks.current(worker)
    await turn(runtime, session)
    await call_tool(runtime, "check_back", worker, task_id=b["id"], minutes=60,
                    what="whether the ablation finished")  # fmt: skip
    await idle(runtime, session, "Started.")
    await router_says(runtime, harness, worker, a, "continue", "Add the seeds.")
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="done", task_id=a["id"],
                    summary="Ran them; seeds 1-3.")  # fmt: skip
    await idle(runtime, session)
    runtime.task_actions.accept(a["id"])
    assert runtime.tasks.current(worker)["id"] == a["id"], "the pointer is on done A"
    assert runtime.tasks.get(b["id"])["status"] == "running"
    return runtime, worker, a, b, session


@pytest.mark.parametrize("how", ["note", "continue"])
async def test_a_message_naming_no_task_goes_to_the_only_open_one(harness, how):
    """E86: with no task_id, a continue or note went to the task given last, done A (a continue
    was refused as "accepted", or reopened it), not to B, the one open task."""
    runtime, worker, a, b, session = await stale_pointer(harness)
    told = await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                           task=how, text="Use seed 4 as well.")  # fmt: skip
    assert told.result_type == "success", told.text_result_for_llm
    assert f'task="{b["id"]}"' in session.sent[-1]
    assert runtime.tasks.get(a["id"])["status"] == "done"
    if how == "continue":
        assert runtime.tasks.current(worker)["id"] == b["id"], "the task it is on now"
    entry = next(
        s
        for s in json.loads(
            (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
        )["sessions"]
        if s["session_id"] == worker
    )
    assert entry["task"]["task_id"] == b["id"], "the Router sees the task at work"


async def test_a_continue_naming_no_task_with_none_open_lists_the_recent_ones(harness):
    runtime, worker, a, b, _session = await stale_pointer(harness)
    runtime.tasks.move(b["id"], "review", result="Ablation done.")
    refused = await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                              text="One more seed.")  # fmt: skip
    assert refused.result_type == "failure"
    said = refused.text_result_for_llm
    assert "task_id" in said and a["id"] in said and b["id"] in said


async def test_a_worker_is_named_by_what_its_turn_is_on(harness):
    """E88: a worker in its turn on A (reported, it still pushes) was named after B, given
    since and not seen yet; once B was cancelled and nothing was open, after B again (its
    cancel the newest message about a task). The Router saw the first title."""
    runtime = await harness.connect()
    worker, session, a = await busy_worker(runtime, harness, "Fix the login")
    given = {"task": "new", "why_new": "test", "earlier": "keep", "title": "Write the docs"}
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    text="Then write the docs.", **given)  # fmt: skip
    b = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="done", task_id=a["id"], summary="Fixed.")
    row = harness.store.session(worker)
    assert runtime.states[worker].busy and runtime.display_title(row) == "Fix the login"
    await runtime.task_actions.cancel(b["id"], "Not needed.", by="the Router")
    await idle(runtime, session)
    row = harness.store.session(worker)
    assert runtime.display_title(row) == "Fix the login"
    harness.store.update_session(worker, title="Started as this")
    listed = json.loads(
        (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
    )
    entry = next(s for s in listed["sessions"] if s["session_id"] == worker)
    assert entry["title"] == "Fix the login"


async def asking(harness):
    """A worker that asked the owner a question on its task: waiting, under Needs you."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Fix the login timeout")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="question",
                    summary="Seconds or minutes for the cookie's max_age?")  # fmt: skip
    await idle(runtime, session)
    assert runtime.tasks.get(task["id"])["status"] == "waiting"
    assert needs(runtime).markers.get(task["id"]) == "question"
    return runtime, worker, task, session


@pytest.mark.parametrize("how", ["note", "continue"])
async def test_the_routers_answer_to_a_question_moves_its_task_on(harness, how):
    """E97: the Router answered from what the owner said before; the task stayed "waiting"
    under Needs you (its question gone) until the worker's next report."""
    runtime, worker, task, session = await asking(harness)
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task=how,
                    task_id=task["id"], text="Seconds, as the owner said.")  # fmt: skip
    assert runtime.tasks.get(task["id"])["status"] == "running"
    assert task["id"] not in needs(runtime).markers
    assert "Seconds, as the owner said." in session.sent[-1]


async def test_the_routers_note_leaves_a_stopped_task_or_a_decision_waiting(harness):
    runtime, worker, task, _session = await asking(harness)
    harness.store.set(f"stopped:{task['id']}", "you")
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    task="note", task_id=task["id"], text="Seconds.")  # fmt: skip
    assert runtime.tasks.get(task["id"])["status"] == "waiting", "only Go on resumes it"
    harness.store.delete(f"stopped:{task['id']}")
    await call_tool(runtime, "ask_decision", worker, question="Ship it on Friday?")
    await router_says(runtime, harness, worker, task, "note", "FYI: the review is tomorrow.")
    assert runtime.tasks.get(task["id"])["status"] == "waiting", "its card waits for the owner"


async def test_a_decision_asked_on_accepted_work_outlives_its_next_progress_report(harness):
    """E100: woken to check the run of accepted work, it asked a decision and reported
    progress as told: the card closed at once ("Its task ended."), and nobody told it."""
    runtime, worker, a, _b, session = await checking(harness, with_b=False)
    await woken(runtime, session)
    await call_tool(runtime, "ask_decision", worker, question="Rerun the failed shards?")
    card = decision_cards(harness)[0]
    assert [x["task_id"] for x in card["target"]["askers"]] == [a["id"]]
    await call_tool(runtime, "report", worker, status="progress", task_id=a["id"],
                    summary="Two shards failed; asked whether to rerun them.",
                    artifacts=[NIGHTLY])  # fmt: skip
    assert harness.store.draft(card["id"])["status"] == "pending"
    assert runtime.tasks.get(a["id"])["status"] == "done"


async def test_a_worker_hears_when_the_decision_it_waits_for_expires(harness):
    """E100: a card that really expires (its task accepted as it is) tells its worker so, with
    its next message (no turn of its own)."""
    runtime = await harness.connect()
    worker, a = await spawn(runtime, harness.router_id, "Fix the login")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="done", summary="Fixed.")
    await call_tool(runtime, "ask_decision", worker, question="Keep the old login page?")
    await idle(runtime, session)
    card = decision_cards(harness)[0]
    sent = len(session.sent)
    runtime.task_actions.accept(a["id"])
    await settle(runtime)
    assert harness.store.draft(card["id"])["status"] == "expired"
    assert len(session.sent) == sent, "no turn of its own"
    await runtime.owner_send(worker, "Thanks")
    assert "Keep the old login page?" in session.sent[-1] and "will not come" in session.sent[-1]


async def test_a_worker_reopened_waits_on_its_decision_card_again(harness):
    """E106: closed and reopened, its task waited for "your decision" again, but the card that
    closing expired stayed gone: only a chat reply was left, not recorded as a decision."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Fix the login")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "ask_decision", worker, question="Keep the old login page?")
    await idle(runtime, session)
    card = decision_cards(harness)[0]
    await runtime.workers.close(worker)
    assert harness.store.draft(card["id"])["status"] == "expired", "nobody waits on it now"
    await runtime.workers.reopen(worker, restore=True)
    assert runtime.tasks.get(task["id"])["status"] == "waiting"
    back = harness.store.draft(card["id"])
    assert back["status"] == "pending"
    assert [a["task_id"] for a in back["target"]["askers"]] == [task["id"]]
    await runtime.decisions.answer(back, DraftDecision(action="answer", text="Keep it"))
    await settle(runtime)
    assert runtime.tasks.get(task["id"])["status"] == "running"
    assert "The owner decided: Keep it" in session.sent[-1]


async def test_each_unread_message_holds_back_a_report_once(harness):
    """E92: a note unread held a done report back once; the Router's continue, sent later and
    unread too, held back nothing (the marker of the first was never cleared): the report went
    through without the continue's work."""
    runtime = await harness.connect()
    worker, session, task = await busy_worker(runtime, harness, "Write the bug report")
    await router_says(runtime, harness, worker, task, "note", "The owner reads it tonight.")
    first = await call_tool(runtime, "report", worker, status="done", summary="Written.")
    assert first.result_type == "failure" and "newer message" in first.text_result_for_llm
    session.emit("user.message", content=session.sent[-1])  # It sees the note.
    await settle(runtime)
    await router_says(runtime, harness, worker, task, "continue", "Add two more entries.")
    second = await call_tool(runtime, "report", worker, status="done", summary="Written.")
    assert second.result_type == "failure" and "Add two more entries" in (
        second.text_result_for_llm
    )
    assert runtime.tasks.get(task["id"])["status"] == "running"
    again = await call_tool(runtime, "report", worker, status="done", summary="Written.")
    assert again.result_type == "success", "the same message holds it back once"


async def crossing(harness):
    """The Router's continue on a running task, sent within its worker's turn; the worker
    reports done before it reads it (held back once, then through). Returns runtime, worker,
    its session, the task, the continue as sent."""
    runtime = await harness.connect()
    worker, session, task = await busy_worker(runtime, harness, "Write the bug report")
    await router_says(runtime, harness, worker, task, "continue", "Add two more entries.")
    sent = session.sent[-1]
    held = await call_tool(runtime, "report", worker, status="done", summary="Written: 12 bugs.")
    assert held.result_type == "failure"
    through = await call_tool(runtime, "report", worker, status="done", summary="Written: 12 bugs.")
    assert through.result_type == "success"
    assert runtime.tasks.get(task["id"])["status"] == "review"
    return runtime, worker, session, task, sent


async def test_a_reported_task_is_at_work_again_when_its_worker_reads_a_continue(harness):
    """E93: the continue crossed the report; when the worker read it and worked on it, the task
    stayed "Awaiting acceptance" (Accept would end work under way)."""
    runtime, _worker, session, task, sent = await crossing(harness)
    session.emit("user.message", content=sent)
    await settle(runtime)
    shown = runtime.tasks.get(task["id"])
    assert shown["status"] == "running" and shown["result"] == "Written: 12 bugs."
    await runtime.task_actions.cancel(task["id"])
    back = runtime.tasks.get(task["id"])
    assert (back["status"], back["result"]) == ("review", "Written: 12 bugs."), "its result stands"


async def test_a_note_read_after_the_report_reopens_nothing(harness):
    runtime = await harness.connect()
    worker, session, task = await busy_worker(runtime, harness, "Write the bug report")
    await router_says(runtime, harness, worker, task, "note", "The owner reads it tonight.")
    sent = session.sent[-1]
    await call_tool(runtime, "report", worker, status="done", summary="Written.")
    await call_tool(runtime, "report", worker, status="done", summary="Written.")
    session.emit("user.message", content=sent)
    await settle(runtime)
    assert runtime.tasks.get(task["id"])["status"] == "review"


async def test_a_report_that_crossed_a_message_says_so_to_the_router(harness):
    """E95: the report came two seconds after the Router's continue, without what it asked;
    nothing said the worker had not read it yet, and the Router moved that work into a new
    task."""
    runtime, worker, _session, task, _sent = await crossing(harness)
    report = [m for m in router_inbound(harness) if m["meta"].get("status") == "done"][-1]
    prompt = report["meta"]["prompt"]
    assert "Add two more entries." in prompt and "before" in prompt
    assert f"task_id='{task['id']}'" in prompt
    listed = json.loads(
        (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
    )
    entry = next(s for s in listed["sessions"] if s["session_id"] == worker)
    assert any("Add two more entries." in text for text in entry["unread"])


async def test_a_note_on_a_task_its_worker_has_not_seen_yet_comes_after_it(harness):
    """E94: a note about a new task, given while its worker was in a turn on another, was
    steered in at once and read before the task itself (queued until the turn's end)."""
    runtime = await harness.connect()
    worker, session, a = await busy_worker(runtime, harness, "Fix the login")
    given = {"task": "new", "why_new": "test", "earlier": "keep", "title": "Write the docs"}
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    text="Then write the docs.", **given)  # fmt: skip
    b = runtime.tasks.current(worker)
    assert session.modes[-1] == "enqueue", "a new task waits for the turn's end"
    await router_says(runtime, harness, worker, b, "note", "Use the new style guide.")
    assert session.modes[-1] == "enqueue", "after the task it is about, not before"
    await router_says(runtime, harness, worker, a, "note", "The owner tests it tonight.")
    assert session.modes[-1] == "immediate", "about the task at work: within its turn"
    session.emit("user.message", content=session.sent[-3])  # It reads the new task.
    await settle(runtime)
    await router_says(runtime, harness, worker, b, "note", "In English.")
    assert session.modes[-1] == "immediate"


def test_a_task_its_worker_has_not_seen_shows_up_next_not_at_work():
    """E94: the new task showed Running with the worker's current step (another task's) before
    the worker had seen it."""
    found = run_js(
        BOARD + "const worker = { status: 'working', turn_task: 'a', task: { task_id: 'b' } };\n"
        "const list = [{ id: 'a', title: 'Fix the login', status: 'review', created_at: '1' },"
        " { id: 'b', title: 'Write the docs', status: 'running', created_at: '2',"
        " started: false }];\n"
        "const context = b.workerContext(worker, list);\n"
        "const phase = b.taskPhase(list[1], worker, context);\n"
        "return [context.active, phase.section, phase.marker];"
    )
    assert found == ["", "monitoring", "turn"]


async def test_a_superseding_task_comes_before_the_note_that_names_it(harness):
    """E108: earlier="superseded" cancelled the old task at once, and its note said "go on with
    your other open task(s): <the new one>" before the new task's instruction was sent. The
    cancel read "Cancelled by router:"."""
    runtime = await harness.connect()
    worker, a = await spawn(runtime, harness.router_id, "Fix the login")
    session = harness.session(worker)
    await turn(runtime, session)
    sent = len(session.sent)
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    task="new", why_new="test", earlier="superseded", title="Rewrite the login",
                    text="Rewrite it from scratch.")  # fmt: skip
    await settle(runtime)
    b = runtime.tasks.current(worker)
    given = [i for i, text in enumerate(session.sent) if i >= sent and 'type="task"' in text]
    named = [i for i, text in enumerate(session.sent) if i >= sent and 'type="cancelled"' in text]
    assert given, "the new task is given"
    assert not named or given[0] <= named[0], "its instruction comes first"
    cancelled = runtime.tasks.get(a["id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["progress"].startswith("Cancelled by the Router")
    assert b["status"] == "running"


def failing_run(session, call: str):
    command = "uv run --with pytest pytest -q"
    session.emit("tool.execution_start", tool_name="bash", arguments={"command": command},
                 tool_call_id=call)  # fmt: skip
    output = SimpleNamespace(content="F\n1 failed in 0.1s\n<exited with exit code 1>")
    session.emit("tool.execution_complete", tool_call_id=call, success=True, result=output)


async def test_each_failing_test_run_holds_back_a_done_report_once(harness):
    """E112 (E92's pattern): "your last test run failed" held back a done report once per task,
    ever: a later failing run, or the same one after a follow-up, went through unasked."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Fix the parser")
    session = harness.session(worker)
    await turn(runtime, session)
    failing_run(session, "c1")
    await settle(runtime)
    first = await call_tool(runtime, "report", worker, status="done", summary="Fixed.")
    assert first.result_type == "failure" and "last test run failed" in first.text_result_for_llm
    said = await call_tool(runtime, "report", worker, status="done",
                           summary="Fixed; that failure is the known flaky test.")  # fmt: skip
    assert said.result_type == "success"
    await runtime.task_actions.follow_up(task["id"], "Also handle empty input.")
    await turn(runtime, session)
    again = await call_tool(runtime, "report", worker, status="done", summary="Handled it.")
    assert again.result_type == "failure", "a follow-up's report with its tests failing"
    await call_tool(runtime, "report", worker, status="done", summary="Handled; known flake.")
    await runtime.task_actions.follow_up(task["id"], "And whitespace.")
    await turn(runtime, session)
    failing_run(session, "c2")
    await settle(runtime)
    later = await call_tool(runtime, "report", worker, status="done", summary="Done too.")
    assert later.result_type == "failure", "a new failing run"
