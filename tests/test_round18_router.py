"""QA round 9 (E141, E145, E148). The Router's continue and new refused each other on a task
that had just finished ("accepted by the owner, use new" / "given minutes ago, use continue"),
and said "accepted by the owner" of tasks nobody accepted. "This report was written before a
newer message" came of a bot's PR comment, a peer's note and a check-back's wake, not only of
instructions. A new task for a busy worker went to a second worker that nothing in the owner's
chat explained (its line folded away), while the first went on with the work the owner had
just rejected; and ten minutes in a turn was "busy"."""

import json
import time

from conftest import Harness, call_tool, settle
from test_progress import busy_worker
from test_round10_lifecycle import spawn
from test_round12_checkbacks import idle, turn

from chiefofstaff.envelope import UNTRUSTED, wrap
from chiefofstaff.models import DraftDecision, Settings


async def message(runtime, harness, worker, **arguments):
    return await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                           **arguments)  # fmt: skip


async def finished(runtime, harness, title="Summarize the launch notes"):
    worker, task = await spawn(runtime, harness.router_id, title)
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="done", summary="Five bullets: …")
    await idle(runtime, session)
    return worker, runtime.tasks.get(task["id"])


async def test_continue_and_new_on_a_task_just_accepted_do_not_refuse_each_other(harness):
    """Live 13:42 PT: one more bullet on a summary the owner had just accepted took three
    calls: continue said to use new, new said to use continue."""
    runtime = await harness.connect()
    worker, task = await finished(runtime, harness)
    runtime.task_actions.accept(task["id"])
    refused = await message(runtime, harness, worker, task="continue", task_id=task["id"],
                            text="Add one more bullet on pricing.")  # fmt: skip
    said = refused.text_result_for_llm
    assert refused.result_type == "failure" and "task='new'" in said
    assert "was accepted by the owner and is done" in said
    given = await message(runtime, harness, worker, task="new", title="Add a pricing bullet",
                          text="Add one more bullet on pricing.")  # fmt: skip
    assert given.result_type == "success", given.text_result_for_llm


async def test_a_task_in_review_does_not_hold_back_a_new_one(harness):
    runtime = await harness.connect()
    worker, task = await finished(runtime, harness)
    assert task["status"] == "review"
    given = await message(runtime, harness, worker, task="new", title="Add a pricing bullet",
                          text="Add one more bullet on pricing.")  # fmt: skip
    assert given.result_type == "success", given.text_result_for_llm


async def test_a_done_task_nobody_accepted_is_not_said_to_be_accepted(harness):
    """The refusal said "accepted by the owner" of tasks that reached done without them."""
    runtime = await harness.connect()
    worker, task = await finished(runtime, harness)
    runtime.tasks.move(task["id"], "done")  # Not by the owner's Accept.
    for arguments in ({"task_id": task["id"]}, {}):
        refused = await message(runtime, harness, worker, task="continue", text="More.",
                                **arguments)  # fmt: skip
        said = refused.text_result_for_llm
        assert f"“{task['title']}” is done" in said and "accepted by" not in said, said
    runtime.tasks.move(task["id"], "review")
    runtime.task_actions.accept(task["id"], as_is=False, by="settled")  # A report it read.
    refused = await message(runtime, harness, worker, task="continue", task_id=task["id"],
                            text="More.")  # fmt: skip
    assert "accepted by" not in refused.text_result_for_llm


def thread_news(task):
    return {
        "prompt": wrap(
            "github",
            "thread",
            "codecov: coverage 88.1% (+0.2%)",
            thread="github:acme/app#7",
            trust=UNTRUSTED,
            task=task["id"],
        ),
        "kind": "inbound",
        "meta": {"source": "github", "thread": "github:acme/app#7", "task_id": task["id"]},
    }


def check_back(task):
    return {
        "prompt": wrap("system", "check-back", "Look at the nightly run again.", task=task["id"]),
        "kind": "router",
        "meta": {"task_id": task["id"], "check_back": True},
    }


async def test_only_an_instruction_holds_a_report_back_or_crosses_it(harness):
    """Live (six times in two days): a bot's PR comment, a code-review bot's summary, a peer's
    "PASS, no blockers" and a check-back's wake each made a done report "written before a newer
    message": the report was held back, the Router told to wait, and a second "nothing new"
    report followed. Only the Router's and the owner's messages (and the owner's reason for
    what they did with a draft) count."""
    runtime = await harness.connect()
    peer, _ = await spawn(runtime, harness.router_id, "Review acme/app#7")
    worker, _session, task = await busy_worker(runtime, harness, "Fix acme/app#7")
    for made in (thread_news(task), check_back(task)):
        await runtime.delivery.deliver(worker, made["prompt"], kind=made["kind"],
                                       display="news", meta=made["meta"], steer=True)  # fmt: skip
    await call_tool(runtime, "tell_session", peer, session_id=f"worker:{worker[:8]}",
                    text="PASS, no blockers.")  # fmt: skip
    assert len(runtime.states[worker].unread) == 3, "sent within its turn, not seen yet"
    done = await call_tool(runtime, "report", worker, status="done", summary="Fixed #7.")
    assert done.result_type == "success", done.text_result_for_llm
    told = [m for m in harness.store.messages(harness.router_id) if m["kind"] == "inbound"][-1]
    assert "written before" not in told["meta"]["prompt"]
    listed = json.loads(
        (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
    )
    assert "unread" not in next(s for s in listed["sessions"] if s["session_id"] == worker)


async def test_the_routers_message_still_holds_a_report_back(harness):
    runtime = await harness.connect()
    worker, _session, task = await busy_worker(runtime, harness, "Fix acme/app#7")
    await message(runtime, harness, worker, task="continue", task_id=task["id"],
                  text="Also cover the empty cart.")  # fmt: skip
    held = await call_tool(runtime, "report", worker, status="done", summary="Fixed #7.")
    assert held.result_type == "failure" and "Also cover the empty cart." in (
        held.text_result_for_llm
    )


async def at_work(runtime, harness, worker, minutes):
    session = harness.session(worker)
    session.emit("user.message", content=session.sent[-1])
    session.emit("assistant.turn_start")
    await settle(runtime)
    runtime.update_session(worker, status="working")
    runtime.states[worker].busy_since = time.monotonic() - minutes * 60


async def new_task(runtime, harness, worker, title, **extra):
    from copilot.tools import ToolInvocation

    arguments = {"session_id": worker, "task": "new", "title": title, "text": title,
                 "why_new": "test", "earlier": "keep", **extra}  # fmt: skip
    result = await runtime.toolset.message_forks.handler(
        ToolInvocation(session_id=harness.router_id, tool_call_id="t1",
                       tool_name="message_session", arguments=arguments)
    )  # fmt: skip
    text = result if isinstance(result, str) else result.text_result_for_llm
    return json.loads(text) if text.startswith("{") else text


async def test_ten_minutes_in_a_turn_is_not_busy_twenty_is(harness):
    """Live 14:42 PT: 17 minutes in a turn made a worker "busy"; the new task went to a second
    worker nobody asked for."""
    runtime = await harness.connect()
    worker, _ = await spawn(runtime, harness.router_id, "Analyze the reach drop")
    await at_work(runtime, harness, worker, 17)
    told = await new_task(runtime, harness, worker, "Check the docs")
    assert isinstance(told, str) and told.startswith("Delivered") and not harness.client.forks
    runtime.states[worker].busy_since = time.monotonic() - 21 * 60
    forked = await new_task(runtime, harness, worker, "Check the changelog")
    assert forked["session_id"] != worker and harness.client.forks


async def test_a_second_worker_is_one_line_in_the_owners_chat_saying_why(harness):
    runtime = await harness.connect()
    worker, _ = await spawn(runtime, harness.router_id, "Analyze the reach drop")
    await at_work(runtime, harness, worker, 21)
    name = runtime.display_title(harness.store.session(worker))
    await new_task(runtime, harness, worker, "Analyze the reach drop again")
    line = [m for m in harness.store.messages(harness.router_id) if m["kind"] == "notice"][-1]
    assert line["text"] == (
        f"Started a second worker from “{name}” (it was busy) for “Analyze the reach drop "
        "again” · new because test"
    )
    assert line["meta"]["fork"] is True, "a line of its own, never folded with routing lines"


async def test_a_busy_worker_whose_draft_was_rejected_is_told_and_its_task_left_be(tmp_path):
    """Live 14:42 PT: the owner rejected a worker's draft and asked for a new analysis; that
    went to a second worker, and the first went on with the rejected work (its next report
    "written before a newer message": the rejection). It is told, once, in a plain note; its
    task stays as it is (only the Router or the owner cancels it), and the Router hears so."""
    harness = Harness(tmp_path / "data", Settings())
    runtime = await harness.connect()
    worker, old = await spawn(runtime, harness.router_id, "Analyze the reach drop")
    draft = harness.store.create_draft(worker, "reply", {"to": "Ann", "task_id": old["id"]},
                                       "Reach fell 4% because of the holiday.")  # fmt: skip
    await at_work(runtime, harness, worker, 21)
    await runtime.drafts.resolve(
        draft["id"], DraftDecision(action="reject", reason="Wrong cause: redo it by region.")
    )
    session = harness.session(worker)
    sent = len(session.sent)
    forked = await new_task(runtime, harness, worker, "Analyze the reach drop by region")
    await settle(runtime)
    assert forked["session_id"] != worker
    assert runtime.tasks.get(old["id"])["status"] == "running", "not cancelled on a guess"
    assert not harness.store.get(f"supersededby:{old['id']}") and not session.aborted
    [note] = session.sent[sent:]
    assert (
        "Your draft was rejected; a second worker is doing the new request. Stop the old one "
        "unless told otherwise." in note
    )
    assert f'task="{old["id"]}"' in note
    assert old["id"] in forked["rejected_draft"] and "cancel_task" in forked["rejected_draft"]
    again = await new_task(runtime, harness, worker, "Chart the reach drop by region")
    assert len(session.sent) == sent + 1 and "rejected_draft" not in again, "once"
    # It stops; no report is asked of the task, which waits for the Router's or owner's word.
    await idle(runtime, session, "Stopped the holiday analysis, as the owner rejected it.")
    assert len(session.sent) == sent + 1, "no report asked for"
    assert runtime.tasks.get(old["id"])["status"] == "running"


async def test_a_busy_workers_other_work_goes_on(harness):
    """Not when the owner said no to nothing of it: its task stays running."""
    runtime = await harness.connect()
    worker, old = await spawn(runtime, harness.router_id, "Analyze the reach drop")
    await at_work(runtime, harness, worker, 21)
    forked = await new_task(runtime, harness, worker, "Check the changelog")
    assert forked["session_id"] != worker and "rejected_draft" not in forked
    assert runtime.tasks.get(old["id"])["status"] == "running"
