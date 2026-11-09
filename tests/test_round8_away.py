"""Bug bash round 8: away replies (R1-R3, R5, R8-R11, R14)."""

import json

import pytest
from conftest import Harness, call_tool, settle
from test_round6_drafts import away, spawn, told
from test_round7_away import code_of, drafted, reply
from test_round7_backend import URL, opened_pr, pr_view

from chiefofstaff.connector import answer_part, holds_send
from chiefofstaff.models import Settings


def notes(harness, code: str) -> list[str]:
    return [
        m["text"] for m in harness.store.messages(harness.router_id) if f"[{code}]" in m["text"]
    ]


async def asked(tmp_path, *questions):
    """Workers that each asked the owner a question; the owner told of them away."""
    harness = Harness(tmp_path / "data", Settings(away_notify=True, away_how="DM me"))
    runtime = await away(harness)
    workers = []
    for number, question in enumerate(questions):
        worker, _task = await spawn(runtime, harness, f"Ask {number}")
        await call_tool(runtime, "report", worker, status="question", summary=question)
        harness.session(worker).emit("session.idle")
        workers.append(worker)
    await settle(runtime)
    messenger, body = await told(runtime, harness)
    return harness, runtime, messenger, body, workers


async def test_a_send_with_a_link_in_it_is_sent(tmp_path):
    """R1: "send it. Source: <https://…/sales?tab=rows|sales sheet>" was held back for the
    "?" in the link's address."""
    _harness, runtime, messenger, body = await drafted(tmp_path, "Numbers are in.")
    code = code_of(body, "Numbers are in")
    link = "<https://acme.example.com/sheets/sales?tab=rows|sales sheet>"
    text = f"[{code.lower()}] looks right, send it. Source: {link}"
    said = await reply(runtime, messenger, {"code": code, "text": text, "intent": "send"})
    assert f"{code}: marked as sent" in said, said
    assert not holds_send("send it, notes at https://acme.example.com/next/before-first")
    assert holds_send("send it <https://acme.example.com/a|after the call>") == "after"


async def test_an_answer_before_its_code_counts_and_a_refused_one_is_asked_again(tmp_path):
    """R2: "Teal, please. 4IF37" was "write your answer after the code"; and an answer that was
    not applied was asked again away only for drafts."""
    harness, runtime, messenger, body, [worker] = await asked(tmp_path, "Teal or navy?")
    code = code_of(body, "Teal or navy?")
    said = await reply(runtime, messenger, {"code": code, "text": code})
    assert "write your answer after the code" in said, said
    _messenger, again = await told(runtime, harness)
    assert f"*[{code}]* Not answered yet \u00b7" in again, again
    said = await reply(runtime, messenger, {"code": code, "text": f"Teal, please. {code}"})
    assert f"{code}: sent to the worker" in said, said
    assert "Teal, please." in harness.session(worker).sent[-1]


async def test_a_draft_with_blanks_is_completed_from_away(tmp_path):
    """R3: words for a draft's blanks were "unclear", answered 'answer "send"…', and asked
    again with `send · no`: a loop."""
    harness, runtime, messenger, body = await drafted(
        tmp_path, "Attending: [yes / no]. T-shirt size: ___"
    )
    code = code_of(body, "Attending")
    assert f"`{code} <words for the blanks> · no`" in body, body
    harness.store.set("away_read_at", "0")
    await runtime.connector.read_replies()
    await settle(runtime)
    read = harness.session(messenger["id"]).sent[-1]
    assert "with blanks to fill: \u201c[yes / no]\u201d, \u201c___\u201d" in read, read
    harness.session(messenger["id"]).emit("session.idle")
    await settle(runtime)
    said = await reply(
        runtime, messenger, {"code": code, "text": f"{code} hmm", "intent": "unclear"}
    )
    assert "say the words for its blanks" in said and 'answer "send"' not in said, said
    _messenger, again = await told(runtime, harness)
    assert "Not sent yet" in again and f"`{code} <words for the blanks> · no`" in again, again
    words = {"code": code, "text": f"{code} attending yes, size M", "intent": "change"}
    said = await reply(runtime, messenger, words)
    assert f"{code}: to be drafted again as you say" in said, said
    [row] = harness.store.all("SELECT session_id FROM drafts")
    assert (
        "Fill in its blanks: attending yes, size M" in harness.session(row["session_id"]).sent[-1]
    )


async def test_a_question_asked_just_after_its_draft_goes_on_its_card(tmp_path):
    """R5: a worker drafted a reply, then asked "Should the reply be posted?" (about_draft) two
    seconds later: a Question card of its own, and two codes away for one decision."""
    harness = Harness(tmp_path / "data", Settings(away_notify=True, away_how="DM me"))
    runtime = await away(harness)
    worker, task = await spawn(runtime, harness, "Answer Alice")
    await call_tool(runtime, "draft_reply", worker, text="Friday works.", to="Alice", where="mail")
    await call_tool(runtime, "report", worker, status="question", about_draft=True,
                    summary="Should the reply be posted?")  # fmt: skip
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    [draft] = harness.store.drafts()
    assert runtime.task_actions.view(runtime.tasks.get(task))["question_draft"] == draft["id"]
    assert [item["id"] for item in runtime.connector.waiting_items()] == [f"draft:{draft['id']}"]
    # The owner wrote to it in between: a question of its own.
    other, task = await spawn(runtime, harness, "Answer Bob")
    await call_tool(runtime, "draft_reply", other, text="Monday then.", to="Bob", where="mail")
    runtime.add_message(other, "owner", "Hold on, Bob may cancel.")
    await call_tool(runtime, "report", other, status="question", about_draft=True,
                    summary="Should the reply be posted?")  # fmt: skip
    assert not runtime.task_actions.view(runtime.tasks.get(task))["question_draft"]


async def merge_told(tmp_path):
    """A PR of the owner's, ready to merge, told away: its code."""
    harness = Harness(tmp_path / "data", Settings(away_notify=True, away_how="DM me"))
    runtime = await away(harness)
    _worker, task = await opened_pr(harness, runtime)
    messenger, body = await told(runtime, harness)
    return harness, runtime, messenger, task, code_of(body, "Ready to merge")


async def test_a_merge_card_after_its_pr_closed_and_reopened(tmp_path):
    """R8: "APIMP merge" after the PR closed was "nothing waits for that code"; reopened at the
    same commit, its new card was never told; the same reply again was "Nothing to apply.\""""
    harness, runtime, messenger, task, code = await merge_told(tmp_path)
    harness.gh_replies[f"pr view {URL}"] = pr_view(state="CLOSED")
    await runtime.task_actions.refresh_prs(task)
    merge = {"code": code, "text": f"{code} merge", "intent": "send"}
    said = await reply(runtime, messenger, merge)
    assert f"{code}: not merged: the PR is closed." in said, said
    assert await reply(runtime, messenger, merge) == "Nothing to apply.", "read again: said once"
    harness.gh_replies[f"pr view {URL}"] = pr_view()
    await runtime.task_actions.refresh_prs(task)
    _messenger, body = await told(runtime, harness)
    new = code_of(body, "Ready to merge")
    assert new != code
    said = await reply(runtime, messenger, merge)
    assert f"It is ready to merge again: answer it with its code {new}" in said, said
    assert len(notes(harness, code)) == 2
    harness.gh_replies |= {
        "api repos/acme/app": json.dumps({"allow_squash_merge": True}),
        "pr merge": "",
        f"pr view {URL}": pr_view(state="MERGED"),
    }
    said = await reply(runtime, messenger, {"code": new, "text": f"{new} merge", "intent": "send"})
    assert f"{new}: acme/app#7 squashed and merged" in said, said


async def test_a_merge_not_done_says_why_and_is_asked_again_as_not_merged(tmp_path):
    """R9: "Not sent yet · Ready to merge…" and 'not merged: answer "merge"…', not why."""
    harness, runtime, messenger, _task, code = await merge_told(tmp_path)
    later = {"code": code, "text": f"{code} merge after the demo", "intent": "unclear"}
    said = await reply(runtime, messenger, later)
    assert "not merged: \u201cafter\u201d sounds like not now" in said, said
    _messenger, again = await told(runtime, harness)
    assert f"*[{code}]* Not merged yet \u00b7 Ready to merge" in again, again


async def test_a_reply_to_a_used_code_is_read_when_nothing_else_waits(tmp_path):
    """R10: "HSOJQ wait, don't send that yet" was read only once a new code was issued."""
    harness, runtime, messenger, body = await drafted(tmp_path, "Noted.")
    code = code_of(body, "Noted.")
    said = await reply(runtime, messenger, {"code": code, "text": f"{code} send", "intent": "send"})
    assert "marked as sent" in said, said
    assert not runtime.connector.open_codes() and not runtime.connector.told_codes()
    runs = len(runtime.tasks.for_session(messenger["id"]))
    harness.store.set("away_read_at", "0")
    await runtime.connector.read_replies()
    await settle(runtime)
    assert len(runtime.tasks.for_session(messenger["id"])) == runs + 1
    assert code in harness.session(messenger["id"]).sent[-1]


async def test_a_draft_dropped_from_away_keeps_the_owners_reason(tmp_path):
    """R11: "You rejected its draft. Reason: From away Nothing was sent." for "no thanks,
    I'll answer Alice myself"."""
    harness = Harness(tmp_path / "data", Settings(away_notify=True, away_how="DM me"))
    runtime = await away(harness)
    alice, task = await spawn(runtime, harness, "Answer Alice")
    await call_tool(runtime, "draft_reply", alice, text="Happy to help.", to="Alice", where="mail")
    runtime.tasks.move(task, "review")
    bob, _task = await spawn(runtime, harness, "Answer Bob")
    await call_tool(runtime, "draft_reply", bob, text="Sure thing.", to="Bob", where="mail")
    messenger, body = await told(runtime, harness)
    first, second = code_of(body, "Happy to help"), code_of(body, "Sure thing")
    words = "no thanks, I'll answer Alice myself"
    await reply(runtime, messenger, {"code": first, "text": f"{first} {words}", "intent": "no"},
                {"code": second, "text": f"{second} no", "intent": "no"})  # fmt: skip
    outcomes = {d["text"]: d["outcome"] for d in harness.store.all("SELECT * FROM drafts")}
    assert outcomes == {"Happy to help.": words, "Sure thing.": "From away"}, outcomes
    news = json.loads(harness.store.get(f"news:{task}"))["text"]
    assert f"Reason: {words}. Nothing was sent." in news, news


@pytest.mark.parametrize(
    ("text", "answer"),
    [
        ("ABCDE .env", ".env"),
        ("ABCDE Pasta please.", "Pasta please."),
        ("ABCDE: 9090", "9090"),
        ("ABCDE - 9090", "9090"),
        ("9090; ABCDE", "9090"),
        ("[abcde] -5 degrees", "-5 degrees"),
    ],
)
def test_an_answer_keeps_its_own_punctuation(text, answer):
    """R14: "ABCDE .env" was "env", "Pasta please." lost its period."""
    assert answer_part(text, "ABCDE", {"ABCDE"}) == answer


async def test_an_answer_reaches_its_worker_with_its_punctuation(tmp_path):
    """R14, as the owner answers from away."""
    harness, runtime, messenger, body, workers = await asked(
        tmp_path, "Which file has the key?", "How cold is it?"
    )
    file, cold = code_of(body, "Which file"), code_of(body, "How cold")
    said = await reply(runtime, messenger, {"code": file, "text": f"{file} .env"},
                       {"code": cold, "text": f"{cold} -5 degrees"})  # fmt: skip
    assert f"{file}: sent to the worker" in said and f"{cold}: sent to the worker" in said, said
    assert ".env" in harness.session(workers[0]).sent[-1]
    assert "-5 degrees" in harness.session(workers[1]).sent[-1]
    assert f"[{file}] .env \u2192" in notes(harness, file)[0]
