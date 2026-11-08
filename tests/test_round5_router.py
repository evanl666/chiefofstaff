"""Bug bash round 5: what the Router tells the owner needs them (as Home counts it), what it
hears of their answers and cancels, the daily summary, tidying, report languages, the PRs a
task follows, and acting on a task that changed since the Router looked."""

import json
import time
from datetime import UTC, datetime, timedelta

from conftest import call_tool, settle

from chiefofstaff.digest import digest, needs, needs_you


async def spawn(runtime, router, title, **arguments):
    arguments.setdefault("instructions", title)
    result = await call_tool(
        runtime, "spawn_worker", router, title=title, why_new="test", **arguments
    )
    return json.loads(result.text_result_for_llm)


def reply_card(harness, worker, text="Holding reply: looking into it."):
    target = {
        "channel_id": "C0DEPLOYS",
        "thread_ts": "1794138887.017740",
        "to": "Alice Chen",
        "latest": "Alice Chen: can we deploy?",
        "task_id": worker["task_id"],
    }
    return harness.store.create_draft(worker["session_id"], "slack", target, text)


async def test_what_needs_the_owner_is_what_home_lists(harness):
    """Bug bash 5 (L4, W5): asked what needs the owner, the Router counted only cards and
    questions, called read-only reports results to accept, counted a result reviewed on its
    draft's card twice and left out results to review."""
    runtime = await harness.connect()
    router = harness.router_id
    fix = await spawn(runtime, router, "Fix login")
    compare = await spawn(runtime, router, "Compare libraries")
    answer = await spawn(runtime, router, "Answer Alice")
    asking = await spawn(runtime, router, "Pick a database")
    for worker in (fix, compare, answer):
        await call_tool(runtime, "report", worker["session_id"], status="done", summary="Done.")
    harness.store.set(f"reviewreq:{fix['task_id']}", "1")  # A result to accept.
    reply_card(harness, answer)
    await call_tool(runtime, "notify_owner", router, text="Renew your token today.",
                    needs_action=True)  # fmt: skip
    await call_tool(runtime, "report", asking["session_id"], status="question",
                    summary="Postgres or MySQL?")  # fmt: skip

    listed = json.loads((await call_tool(runtime, "list_sessions", router)).text_result_for_llm)
    found = listed["needs_owner"]
    assert found["count"] == needs_you(runtime) == 4
    [card] = found["cards"]
    assert card["result_to_review"].startswith(f"{answer['task_id']} “Answer Alice”")
    assert [(t["task"], t["needs"]) for t in found["tasks"]] == [
        (f"{fix['task_id']} “Fix login”", "to review")
    ]
    assert [q["question"] for q in found["questions"]] == ["Postgres or MySQL?"]
    assert [note["text"] for note in found["your_notes"]] == ["Renew your token today."]
    assert "1 result to review" in found["summary"] and "1 draft to approve" in found["summary"]
    status = {task["title"]: task["status"] for task in listed["tasks"]}
    assert status["Fix login"] == "review, to review (needs you)"
    assert status["Compare libraries"].startswith("review, report (read only")
    assert status["Answer Alice"] == "review, to review on its draft's card (needs you)"
    assert status["Pick a database"] == "waiting, question (needs you)"


async def test_a_stalled_task_needs_the_owner_as_on_home(harness):
    """Bug bash 5 (W9): Home puts a task whose worker did not answer where it stands under
    Needs you (No progress); the count of what needs the owner left it out."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "SLM eval")
    runtime.update_session(worker["session_id"], status="idle")  # Between steps, quiet.
    runtime.states[worker["session_id"]].busy = False
    asked = datetime.now(UTC) + timedelta(hours=5)
    harness.store.set(f"stallnudge:{worker['task_id']}", asked.isoformat())
    assert needs(runtime, asked + timedelta(minutes=30)).markers == {}, "it may still answer"
    found = needs(runtime, asked + timedelta(hours=2))
    assert found.markers == {worker["task_id"]: "stalled"} and len(found.items) == 1


async def test_the_daily_summary_counts_what_home_lists(harness):
    """Bug bash 5 (W9): "N drafts to approve" counted every card (a decision, a question), a
    result reviewed on its draft's card was counted again, and the Router's notes were left
    out."""
    runtime = await harness.connect()
    router = harness.router_id
    answer = await spawn(runtime, router, "Answer Alice")
    await call_tool(runtime, "report", answer["session_id"], status="done", summary="Drafted.")
    harness.store.set(f"reviewreq:{answer['task_id']}", "1")
    reply_card(harness, answer)
    harness.store.create_draft(router, "decision", {"question": "Ship Friday?"}, "Ship Friday?")
    await call_tool(runtime, "notify_owner", router, text="Renew your token today.",
                    needs_action=True)  # fmt: skip
    text = digest(runtime, time.time() - 60, time.time() + 1)
    waiting = next(line for line in text.splitlines() if line.startswith("• Waiting for you"))
    assert waiting == (
        "• Waiting for you: 1 draft to approve · 1 card to answer (decision) · "
        "1 note of mine to act on"
    ), "the result is reviewed on its card: once"


async def test_the_router_hears_when_the_owner_answers_a_workers_question(harness):
    """Bug bash 5 (L4): the Router said a worker's question was still open after the owner had
    answered it on its card."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Pick a database")
    await call_tool(runtime, "report", worker["session_id"], status="question",
                    summary="Postgres or MySQL?")  # fmt: skip
    [question] = runtime.asks.session_questions(worker["session_id"])
    await runtime.task_actions.answer_question(question["id"], "Postgres")
    [note] = [
        m for m in harness.store.messages(harness.router_id)
        if 'type="question-closed"' in m["meta"].get("prompt", "")
    ]  # fmt: skip
    assert note["meta"]["deferred"], "with its next message, not a turn of its own"
    assert "The owner answered “Pick a database”'s question" in note["meta"]["prompt"]
    assert "Postgres or MySQL?" in note["meta"]["prompt"]
    assert "no longer open" in note["meta"]["prompt"]


async def test_a_note_asking_a_workers_open_question_again_is_refused(harness):
    """Bug bash 5 (W6): the Router posted its own note asking what a worker's question under
    Needs you already asked; answered on the worker's card, the note stayed until Got it."""
    runtime = await harness.connect()
    router = harness.router_id
    worker = await spawn(runtime, router, "Fix the rounding bug")
    await call_tool(runtime, "report", worker["session_id"], status="question",
                    summary="Found a second bug in parse(): fix it too?")  # fmt: skip
    await settle(runtime)
    refused = await call_tool(runtime, "notify_owner", router, needs_action=True,
                              text="Should the worker fix the parse() bug as well?")  # fmt: skip
    assert refused.text_result_for_llm.startswith("Not shown: the owner has this")
    assert runtime.owner_updates() == []
    token = "Your GitHub token expires today: renew it."
    other = await call_tool(runtime, "notify_owner", router, needs_action=True, text=token)
    assert other.text_result_for_llm.startswith("Not shown"), "this turn only brought that"
    other = await call_tool(runtime, "notify_owner", router, needs_action=True, text=token)
    assert other.text_result_for_llm == "Shown to the owner.", "sent again: about another thing"

    urgent = await call_tool(runtime, "notify_owner", router, needs_action=True, urgent=True,
                             text="“Fix the rounding bug” waits: prod is down.")  # fmt: skip
    assert urgent.text_result_for_llm == "Shown to the owner."
    assert len(runtime.owner_updates()) == 2
    [question] = runtime.asks.session_questions(worker["session_id"])
    await runtime.task_actions.answer_question(question["id"], "Yes, fix it.")
    assert [u["text"] for u in runtime.owner_updates()] == [
        "Your GitHub token expires today: renew it."
    ], "the note about the question goes with it"


async def test_idle_workers_whose_results_wait_for_the_owner_stay_open(harness):
    """Bug bash 5 (L10): after a day, workers whose results still waited for review under
    Needs you were closed: they left the sidebar and the @ list."""
    runtime = await harness.connect()
    review = await spawn(runtime, harness.router_id, "Fix login")
    report = await spawn(runtime, harness.router_id, "Compare libraries")
    for worker in (review, report):
        await call_tool(runtime, "report", worker["session_id"], status="done", summary="Done.")
    harness.store.set(f"reviewreq:{review['task_id']}", "1")
    await settle(runtime)
    assert await runtime.workers.tidy(time.time() + 2 * 86400) == [report["session_id"]]
    assert not harness.store.session(review["session_id"])["closed_at"]


async def test_a_new_tasks_report_follows_the_language_of_its_request(harness):
    """Bug bash 5 (W11): one Chinese follow-up to one worker made new tasks for English
    requests report in Chinese; in a turn without the owner (a Slack message), a task is
    reported in the language of what it serves."""
    runtime = await harness.connect()
    router = harness.router_id
    await runtime.owner_send(router, "Count the files in the project please")
    first = await spawn(runtime, router, "Count files")
    await call_tool(runtime, "report", first["session_id"], status="done", summary="3 files.")
    await runtime.task_actions.follow_up(first["task_id"], "再把每个文件的行数也列出来")
    second = await spawn(runtime, router, "Count the tests")
    assert 'reply_in="English"' in harness.session(second["session_id"]).sent[-1]

    await runtime.owner_send(router, "以后用中文汇报")
    runtime.states[router].event_turn = True  # A Slack message's turn: no owner message.
    slack = await spawn(runtime, router, "Answer Bob", instructions="Bob asks in #deploys "
                        "whether calc 1.2 is out. Please answer him in the thread.")  # fmt: skip
    sent = harness.session(slack["session_id"]).sent[-1]
    assert 'reply_in="English"' in sent and "Write your report in" not in sent


async def test_a_pr_a_task_only_mentions_is_not_followed(harness):
    """Bug bash 5 (S13): a task whose instructions only mentioned a PR (a pointer to write into
    a file) followed it, and showed under Monitoring as still following it."""
    runtime = await harness.connect()
    pointer = await spawn(runtime, harness.router_id, "Open pointer-note PR",
        instructions='On a new branch add notes/own.md with exactly one line: "Rounding notes '
        'live in PR #60: https://github.com/acme/app/pull/60". Commit, push, and open a PR. '
        "Do not comment on or touch PR #60 itself.")  # fmt: skip
    assert harness.store.follows(pointer["session_id"]) == []
    review = await spawn(runtime, harness.router_id, "Review #61",
                         instructions="Review https://github.com/acme/app/pull/61")  # fmt: skip
    assert harness.store.follows(review["session_id"]) == ["github:acme/app#61"]


async def test_the_router_does_not_redo_a_task_that_changed_since_it_looked(harness):
    """Bug bash 5 (A1): the owner retried a failed task and it reported done while a Router
    turn about the failure went on; the Router then continued it from what it had read, and
    the worker did it all again."""
    runtime = await harness.connect()
    router = harness.router_id
    worker = await spawn(runtime, router, "Start the service")
    await call_tool(runtime, "report", worker["session_id"], status="failed",
                    summary="Could not start: port in use.")  # fmt: skip
    await settle(runtime)
    harness.session(router).emit("assistant.turn_start")  # Its turn on the failure.
    await call_tool(runtime, "read_session", router, session_id=worker["session_id"])
    await runtime.task_actions.retry(worker["task_id"], "")
    await call_tool(runtime, "report", worker["session_id"], status="done", summary="Started.")
    again = {"session_id": worker["session_id"], "task": "continue", "task_id": worker["task_id"],
             "text": "Retry: your first start failed."}  # fmt: skip
    refused = await call_tool(runtime, "message_session", router, **again)
    assert refused.result_type == "failure"
    text = refused.text_result_for_llm
    assert "changed since you looked (it was failed; it is review" in text
    assert "its worker reported (done): “Started.”" in text and "the owner told" in text
    assert runtime.tasks.get(worker["task_id"])["status"] == "review", "not reopened"
    done = await call_tool(runtime, "message_session", router, **again)
    assert done.result_type != "failure", "told once: it knows now"


async def test_a_cancel_during_the_routers_turn_reaches_that_turn(harness):
    """Bug bash 5 (A4): the owner cancelled the task the Router had just started; the
    Router, still in that turn, said it had started it."""
    runtime = await harness.connect()
    router = harness.router_id
    session = harness.session(router)
    await runtime.owner_send(router, "Write is_palindrome(s) with tests")
    session.emit("assistant.turn_start")
    worker = await spawn(runtime, router, "Write is_palindrome")
    await runtime.task_actions.cancel(worker["task_id"], "", by="you")
    await settle(runtime)
    assert f"The owner cancelled task {worker['task_id']}" in session.sent[-1]
    assert session.modes[-1] == "immediate", "into the turn going on"
