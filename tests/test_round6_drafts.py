"""Bug bash 6: drafts and away notifications. A reply waits for a review filed under another
task; an answer to a follow-up does not replace the first answer; the Slack session leaves a
thread a worker took over; one decision is one item; what the owner reads names things once,
without ids, in their words."""

import json
import re
from pathlib import Path

import pytest
from conftest import Harness, call_tool, settle

from chiefofstaff.connector import gist
from chiefofstaff.digest import needs_you
from chiefofstaff.models import DraftDecision, Settings, Watch

TS = "1794157630.819840"
LATER = "1794157927.201838"
PR = "https://github.com/acme/app/pull/68"


def entry(channel, user, name, ts, text):
    """One message as Slack's MCP server lists it."""
    link = f"https://acme.slack.com/archives/{channel}/p{ts.replace('.', '')}"
    return (
        f"Channel: <#{channel}> | From: <@{user}> ({name}) | ts: {ts}\n"
        f"Text: {text}\nPermalink: {link}"
    )


async def slack_harness(tmp_path, **settings):
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True, **settings))
    runtime = await harness.connect()
    return harness, runtime, harness.store.open_sessions("slack")[0]["id"]


async def spawn(runtime, harness, title, **extra):
    result = await call_tool(runtime, "spawn_worker", harness.router_id, title=title,
                             instructions=title, why_new="test", **extra)  # fmt: skip
    made = json.loads(result.text_result_for_llm)
    return made["session_id"], made["task_id"]


async def draft(runtime, harness, session_id, channel, thread_ts, text, to=""):
    result = await call_tool(runtime, "draft_slack_reply", session_id, channel_id=channel,
                             thread_ts=thread_ts, text=text, to=to)  # fmt: skip
    assert result.result_type != "failure", result.text_result_for_llm
    return harness.store.drafts()[-1]


def review(harness, worker, task, verdict="request_changes", number=68, **extra):
    target = {"repo": "acme/app", "number": number, "url": PR, "verdict": verdict, "via": "task",
              "head_sha": "c0ffee", "pr": {"title": "Add median()"}, "task_id": task}  # fmt: skip
    return harness.store.create_draft(worker, "github", target | extra, "Empty input raises.")


def sends(harness):
    return [args for tool, args in harness.mcp.calls if tool == "slack_send_message"]


async def away(harness):
    runtime = await harness.connect()

    async def idle():
        return 3600

    runtime.connector.idle_seconds = idle
    return runtime


def notification(harness, messenger: dict) -> str:
    """The messenger's last job: the message with codes."""
    sent = harness.session(messenger["id"]).sent[-1]
    found = re.search(r"A: (/\S+-codes\.txt)", sent)
    assert found, sent
    return Path(found.group(1)).read_text()


def code_in(body: str, words: str) -> str:
    for section in body.split("\n\n"):
        if words in section and (found := re.search(r"\[([A-Z2-7]{5})\]", section)):
            return found.group(1)
    raise AssertionError(f"no code for {words!r} in {body!r}")


async def told(runtime, harness):
    """Notify the owner away; the messenger says it arrived. Its row and the message."""
    await runtime.connector.notify_away()
    await settle(runtime)
    messenger = runtime.toolset.last_special("messenger")
    body = notification(harness, messenger)
    await call_tool(runtime, "report_notified", messenger["id"], ok=True, where="Slack DM",
                    method="bot send", replies="bot read")  # fmt: skip
    harness.session(messenger["id"]).emit("session.idle")
    await settle(runtime)
    return messenger, body


async def test_a_reply_waits_for_a_review_its_worker_filed_under_another_task(tmp_path):
    """T1: "Updated review on #68" went out to Bob before the review was posted: the review
    was the PR task's, the reply a newer task's. N10: its card says what it waits for."""
    harness, runtime, _slack = await slack_harness(tmp_path)
    worker, first = await spawn(runtime, harness, "Review median() PR", slack=True)
    posted = review(harness, worker, first)
    second = runtime.tasks.create(worker, "Re-check Bob's fix", "x", status="running")["id"]
    harness.store.set(f"turntask:{worker}", second)
    reply = await draft(runtime, harness, worker, "D0BOB0001", "", "Updated review on #68.")
    assert reply["target"]["task_id"] == second
    assert reply["target"]["after_review"] == {
        "id": posted["id"],
        "label": "your review of acme/app#68",
    }, "its card says which review it waits for"
    with pytest.raises(ValueError, match="post your review of acme/app#68 first"):
        await runtime.drafts.resolve(reply["id"], DraftDecision(action="send"))
    assert sends(harness) == []
    # On the owner's own PR the card says "Reply on", and so does the refusal.
    own = review(harness, worker, first, verdict="comment", number=69, own=True)
    elsewhere = await draft(runtime, harness, worker, "D0CAROL01", "", "Reviewed PR #69.")
    with pytest.raises(ValueError, match="post your reply on acme/app#69 first"):
        await runtime.drafts.resolve(elsewhere["id"], DraftDecision(action="send"))
    assert harness.store.draft(elsewhere["id"])["target"]["after_review"]["id"] == own["id"]
    await runtime.drafts.resolve(posted["id"], DraftDecision(action="reject"))
    assert "after_review" not in harness.store.draft(reply["id"])["target"], "it may go now"


async def test_an_answer_to_a_follow_up_does_not_replace_the_first_answer(tmp_path):
    """T2: Erin reported a bug, then asked about chunk(items, 0): the answer to her follow-up
    (another task) replaced the fix's reply, which she never got."""
    harness, runtime, _slack = await slack_harness(tmp_path)
    dm = "D0ERIN001"
    harness.mcp.reads[dm] = entry(dm, "U0ERIN001", "Erin", TS, "chunk() drops my last row")
    worker, first = await spawn(runtime, harness, "Fix chunk()", slack=True)
    harness.store.set(f"turntask:{worker}", first)
    fixed = await draft(runtime, harness, worker, dm, "", "Fixed: it keeps the last group.")
    harness.mcp.reads[dm] = entry(dm, "U0ERIN001", "Erin", LATER, "chunk(items, 0) loops?")
    second = runtime.tasks.create(worker, "Answer Erin: size 0", "x", status="running")["id"]
    harness.store.set(f"turntask:{worker}", second)
    result = await call_tool(runtime, "draft_slack_reply", worker, channel_id=dm,
                             text="Size 0 raises now.")  # fmt: skip
    answer = harness.store.drafts()[-1]
    assert [d["id"] for d in harness.store.drafts()] == [fixed["id"], answer["id"]], "both wait"
    said = result.text_result_for_llm
    assert "It replaces" not in said and f"withdraw_draft {fixed['id']}" in said
    # The owner sends both: the first is not held for her follow-up, which the other answers.
    harness.mcp.sent_link = f"https://acme.slack.com/archives/{dm}/p1794158000000100"
    assert (await runtime.drafts.resolve(fixed["id"], DraftDecision(action="send")))[
        "status"
    ] == "sent"
    assert sends(harness)[-1]["message"] == "Fixed: it keeps the last group."


async def test_away_a_code_for_a_replaced_or_withdrawn_draft_says_what_became_of_it(tmp_path):
    """T2: "QTQQ6 (ok, send it)" for a reply that had been replaced came back "nothing waits for
    that code (answered already?)"."""
    settings = Settings(slack_enabled=True, away_notify=True, away_how="DM me")
    harness = Harness(tmp_path / "data", settings)
    runtime = await away(harness)
    worker, _task = await spawn(runtime, harness, "Fix chunk()", slack=True)
    await draft(runtime, harness, worker, "D0ERIN001", "", "Fixed: it keeps the last group.")
    messenger, body = await told(runtime, harness)
    code = code_in(body, "keeps the last group")
    newer = await draft(runtime, harness, worker, "D0ERIN001", "", "Fixed, with a test.")
    ok = "\u597d\u7684\uff0c\u53d1\u5427"
    replied = await call_tool(runtime, "report_owner_replies", messenger["id"],
                              replies=[{"code": code, "text": f"{code} {ok}"}])  # fmt: skip
    said = replied.text_result_for_llm
    assert "replaced by a newer one: \u201cFixed, with a test.\u201d; nothing was sent" in said
    assert "It waits for you on Home" in said and "nothing waits" not in said
    await call_tool(runtime, "withdraw_draft", worker, draft_id=newer["id"],
                    reason="Erin sorted it out")  # fmt: skip
    gone = runtime.connector.code_for(f"draft:{newer['id']}")
    replied = await call_tool(runtime, "report_owner_replies", messenger["id"],
                              replies=[{"code": gone, "text": f"{gone} send"}])  # fmt: skip
    assert f"{gone}: withdrawn: Erin sorted it out" in replied.text_result_for_llm


async def test_the_owners_edited_reply_stays_sendable_beside_a_new_draft(tmp_path):
    """G6/N1: the owner's edit, held because Bob wrote again, was replaced by the worker's new
    draft: Send then could not send their words."""
    harness, runtime, _slack = await slack_harness(tmp_path)
    worker, _task = await spawn(runtime, harness, "Answer Bob", slack=True,
                                follow=[f"slack:C0DEPLOYS:{TS}"])  # fmt: skip
    harness.mcp.reads["C0DEPLOYS"] = entry("C0DEPLOYS", "U0BOB0001", "Bob", TS, "deploy?")
    card = await draft(runtime, harness, worker, "C0DEPLOYS", TS, "Yes.")
    harness.mcp.reads["C0DEPLOYS"] = entry("C0DEPLOYS", "U0BOB0001", "Bob", LATER, "and when?")
    mine = "Yes, after 3pm. Thursday for the date."
    with pytest.raises(ValueError, match="wrote since"):
        await runtime.drafts.resolve(card["id"], DraftDecision(action="send", text=mine))
    again = await draft(runtime, harness, worker, "C0DEPLOYS", TS, "Thursday.")
    assert [d["id"] for d in harness.store.drafts()] == [card["id"], again["id"]]
    harness.mcp.sent_link = "https://acme.slack.com/archives/C0DEPLOYS/p1794158000000100"
    sent = await runtime.drafts.resolve(card["id"], DraftDecision(action="send", text=mine))
    assert sent["status"] == "sent" and sends(harness)[-1]["message"] == mine


async def test_the_slack_session_leaves_a_thread_a_worker_took_over(tmp_path):
    """T3: told to reply to Dave, the Slack session drafted into his outage thread, which the
    format_money worker followed (its reply was sent already, none waited)."""
    harness, runtime, slack = await slack_harness(tmp_path)
    thread = f"slack:C0DEPLOYS:{TS}"
    _worker, task = await spawn(runtime, harness, "Fix format_money crash", slack=True,
                               follow=[thread])  # fmt: skip
    runtime.tasks.move(task, "review", result="Fixed; the reply to Dave was sent.")
    refused = await call_tool(runtime, "draft_slack_reply", slack, channel_id="C0DEPLOYS",
                              thread_ts=TS, text="No-go for today.")  # fmt: skip
    said = refused.text_result_for_llm
    assert refused.result_type == "failure" and "\u201cFix format_money crash\u201d" in said
    assert "took this conversation over" in said and "message_session" in said
    assert harness.store.drafts() == []
    runtime.tasks.move(task, "cancelled")
    await draft(runtime, harness, slack, "C0DEPLOYS", TS, "No-go for today.")


async def test_a_question_about_its_own_draft_is_one_item_and_the_draft_answers_it(tmp_path):
    """T6: "Decision for you: approve the draft, or hold it" showed as a question beside the
    review's card, and the owner's DM got two codes for one decision."""
    harness = Harness(tmp_path / "data", Settings(away_notify=True))
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness, "Review median() PR")
    await call_tool(runtime, "report", worker, status="question", summary="Bob fixed both "
                    "blockers.\n- Decision for you: approve the draft, or hold it.")  # fmt: skip
    approval = review(harness, worker, task, verdict="approve")
    assert runtime.tasks.get(task)["status"] == "waiting"
    assert [item["id"] for item in runtime.connector.waiting_items()] == [f"draft:{approval['id']}"]
    assert needs_you(runtime) == 1, "Needs you counts one decision"
    await runtime.drafts.resolve(approval["id"], DraftDecision(action="reject", reason="hold"))
    assert runtime.asks.owner_question(f"{worker}:{task}") is None, "answered on the card"
    assert runtime.tasks.get(task)["status"] == "running"


async def test_away_a_review_says_its_verdict_and_the_word_that_posts_it(harness):
    """T7: "Draft to approve · https://github.com/…/pull/68 … send · no": "send" posted an
    approval unsaid."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness, "Review median() PR")
    describe = runtime.connector.describe
    approval = review(harness, worker, task, verdict="approve")
    what, _said, hint = describe({"id": "x", "draft": approval})
    assert (what, hint) == ("Review of app#68 \u201cAdd median()\u201d: Approve", "approve · no")
    changes = review(harness, worker, task)
    what, _said, hint = describe({"id": "x", "draft": changes})
    assert (what, hint) == (
        "Review of app#68 \u201cAdd median()\u201d: Request changes",
        "post · no",
    )
    with pytest.raises(ValueError, match="not an approval"):
        await runtime.connector.apply({"draft": changes}, "QQQQQ approve", "QQQQQ")
    assert harness.store.draft(changes["id"])["status"] == "pending"
    own = review(harness, worker, task, verdict="comment", number=69, own=True)
    what, _said, hint = describe({"id": "x", "draft": own})
    assert (what, hint) == ("Reply on app#69 \u201cAdd median()\u201d", "send · no")


async def test_what_the_owner_reads_of_drafts_has_no_ids(tmp_path):
    """T5: "Replaced by a newer draft (d8589006d).", and "(d4d31ea2f)" in an away notification."""
    harness, runtime, _slack = await slack_harness(tmp_path)
    worker, task = await spawn(runtime, harness, "Answer Feng", slack=True)
    older = await draft(runtime, harness, worker, "D0FENG001", "", "Fixed.")
    newer = await draft(runtime, harness, worker, "D0FENG001", "", "Fixed, with a test.")
    assert harness.store.draft(older["id"])["outcome"] == "Replaced by a newer draft."
    gone = await runtime.drafts.withdraw(
        worker, newer["id"], f"Covered by the review ({older['id']})"
    )
    assert gone["outcome"] == "Withdrawn: Covered by the review"
    runtime.tasks.move(task, "waiting", progress=f"I withdrew the old draft ({older['id']}). Ok?")
    [item] = [item for item in runtime.connector.waiting_items() if "task" in item]
    assert runtime.connector.describe(item)[1] == "I withdrew the old draft. Ok?"


async def test_chat_lines_name_a_place_once_and_never_by_its_id(harness):
    """T14: "your reply to Dave (#deploys) in a thread in #deploys", "DM with D0ERIN001"."""
    runtime = await harness.connect()
    harness.store.set("slacknames", json.dumps({"C0DEPLOYS": "deploys", "D0ERIN001": "D0ERIN001"}))
    shown = runtime.drafts.shown
    thread = {"channel_id": "C0DEPLOYS", "thread_ts": TS}

    def slack(**target):
        return shown({"kind": "slack", "target": target})

    assert slack(**thread, to="Dave (#deploys)") == "your reply to Dave (#deploys) in a thread"
    assert slack(**thread, to="Dave (#deploys thread)") == "your reply to Dave (#deploys thread)"
    assert slack(**thread, to="Dave") == "your reply to Dave in a thread in #deploys"
    assert slack(channel_id="D0ERIN001", thread_ts="") == "your reply in a DM"


async def test_a_reply_without_a_recipient_is_named_from_slack(tmp_path):
    """N14: to="" made the card "Slack reply · direct message" and the notification "Draft to
    approve" with no name."""
    harness, runtime, slack = await slack_harness(tmp_path)
    harness.store.set("slacknames", json.dumps({"C0DEPLOYS": "deploys", "D0BOB0001": "Bob"}))
    harness.mcp.reads["D0BOB0001"] = entry("D0BOB0001", "U0BOB0001", "Bob Li", TS, "any news?")
    dm = await draft(runtime, harness, slack, "D0BOB0001", "", "Tomorrow.")
    assert dm["target"]["to"] == "Bob Li (DM)"
    harness.mcp.reads["C0DEPLOYS"] = entry("C0DEPLOYS", "U0DAVE001", "Dave", TS, "down?")
    thread = await draft(runtime, harness, slack, "C0DEPLOYS", TS, "Looking.")
    assert thread["target"]["to"] == "Dave (#deploys)"


async def test_after_a_review_is_posted_its_worker_hears_what_it_went_out_as(tmp_path):
    """N15: a reply said "requested changes" while the review (own PR) went as a comment."""
    harness, runtime, _slack = await slack_harness(tmp_path)
    worker, task = await spawn(runtime, harness, "Review #69", slack=True)
    posted = review(harness, worker, task, verdict="comment", number=69, own=True)
    reply = await draft(runtime, harness, worker, "D0BOB0001", "", "Requested changes on #69.")
    harness.gh_replies["api repos/acme/app/pulls/69"] = json.dumps(
        {"state": "open", "head": {"sha": "c0ffee", "ref": "feature"}}
    )
    harness.gh_replies["api repos/acme/app/pulls/69/reviews"] = json.dumps({"id": 9})
    await runtime.drafts.resolve(posted["id"], DraftDecision(action="send"))
    await settle(runtime)
    heard = harness.session(worker).sent[-1]
    assert "It was posted as a comment" in heard and reply["id"] in heard


async def test_a_merged_prs_withdrawn_reply_does_not_read_as_posted(harness):
    """G10: "PR merged: your reply to bob on …#59" read as if it was posted."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness, "Fix #59")
    runtime.task_actions.record_opened(task, ["https://github.com/acme/app/pull/59"])
    comment = {"author": "bob", "body": "Why this?"}
    harness.store.create_draft(worker, "github", {"repo": "acme/app", "number": 59, "url": PR,
                               "in_reply_to": 5, "comment": comment, "task_id": task},
                               "Because.")  # fmt: skip
    runtime.follow(worker, ["github:acme/app#59"])
    runtime.unfollow("github:acme/app#59", finished=True, merged=True)
    await settle(runtime)
    shown = "Withdrawn (the PR was merged): your reply to bob on acme/app#59"
    assert ("notice", shown) in harness.kinds(harness.router_id)


async def test_the_routers_answer_to_the_owners_question_is_not_a_question(tmp_path):
    """T8: "what did my team ask me today?" was answered with their questions; the answer went
    to the owner's DM as the Router asking."""
    harness = Harness(tmp_path / "data", Settings(away_notify=True))
    runtime = await harness.connect()
    connector = runtime.connector
    router = harness.router_id or ""
    await runtime.owner_send(router, "what did my team ask me today?")
    answer = "Here is what your team asked you today:\n\n- Deprecate parse_amount in 0.5?\n- Can "
    runtime.add_message(router, "assistant", answer + "Gina own the Decimal move?")
    assert connector.waiting_items() == []
    # Nor what it quotes someone asking, in a note of its own.
    runtime.add_message(router, "update", "News from #textkit:\n\n- **Gina (#textkit):** two "
                        "decisions.\n  - Deprecate it in 0.5?\n  - Can I own it?")  # fmt: skip
    assert connector.waiting_items() == []
    runtime.add_message(router, "update", "News from #textkit.\n\n- **Decision:** reply yes?")
    [item] = connector.waiting_items()
    assert item["ask"]["text"].endswith("reply yes?")


async def test_away_where_it_went_is_said_in_the_owners_words(tmp_path):
    """T9: Settings said "On · Owner's own Slack DM", the chat "You answered from Owner's own
    Slack DM"."""
    settings = Settings(away_notify=True, away_how="my own Slack DM", slack_enabled=True)
    harness = Harness(tmp_path / "data", settings)
    runtime = await away(harness)
    worker, _task = await spawn(runtime, harness, "Answer Carol")
    await call_tool(runtime, "draft_reply", worker, text="friday works", to="Carol",
                    where="email")  # fmt: skip
    await runtime.connector.notify_away()
    await settle(runtime)
    messenger = runtime.toolset.last_special("messenger")["id"]
    harness.mcp.sent_link = "https://acme.slack.com/archives/D0SELF001/p1712345678000200"
    await call_tool(runtime, "send_owner_slack_dm", messenger, version="A")
    await call_tool(runtime, "report_notified", messenger, ok=True, where="Owner's Slack DM",
                    method="send_owner_slack_dm", replies="read_owner_slack_dm")  # fmt: skip
    assert runtime.connector.view()["away"]["where"] == "your own Slack DM"
    # Another way: its words, in the second person.
    harness.store.set("away_retry_at", "0")
    harness.session(messenger).emit("session.idle")
    await settle(runtime)
    await runtime.connector.notify_away(test=True)
    await settle(runtime)
    await call_tool(runtime, "report_notified", messenger, ok=True,
                    where="a DM to the owner from acmebot", replies="r")  # fmt: skip
    assert runtime.connector.view()["away"]["where"] == "a DM to you from acmebot"


async def test_a_scan_that_finds_nothing_new_says_none_found(tmp_path):
    """T13: Settings said "2 found" when the scan handed nothing new over."""
    harness = Harness(tmp_path / "data", Settings(watches=[Watch(what="DMs")]))
    runtime = await harness.connect()
    await runtime.connector.scan()
    await settle(runtime)
    row = runtime.connector.session()
    assert row
    item = {"key": "https://acme.slack.com/p1", "source": "slack", "text": "review my PR?"}
    await call_tool(runtime, "report_found", row["id"], items=[item])
    assert runtime.connector.view()["last"]["found"] == 1
    answered = {**item, "key": "https://acme.slack.com/p2", "owner_replied": True}
    await call_tool(runtime, "report_found", row["id"], items=[item, answered])
    last = runtime.connector.view()["last"]
    assert (last["found"], last["seen"]) == (0, 2)


async def test_away_each_refused_reply_is_said_once(tmp_path):
    """N2: "IHXYQ not yet" and "IHXYQ ok wait" were said again at every read."""
    harness = Harness(tmp_path / "data", Settings(away_notify=True, away_how="DM me"))
    runtime = await away(harness)
    worker, _task = await spawn(runtime, harness, "Answer Carol")
    await call_tool(runtime, "draft_reply", worker, text="friday works", to="Carol",
                    where="email")  # fmt: skip
    messenger, body = await told(runtime, harness)
    code = code_in(body, "friday works")
    replies = [{"code": code, "text": f"{code} not yet"}, {"code": code, "text": f"{code} ok wait"}]
    for _read in range(3):
        await call_tool(runtime, "report_owner_replies", messenger["id"], replies=replies)
    notes = [m for m in harness.store.messages(harness.router_id or "") if f"[{code}]" in m["text"]]
    assert len(notes) == 2, [m["text"] for m in notes]


async def test_away_a_failed_task_the_router_retells_is_told_once(tmp_path):
    """N5: "Failed · Answer Bob" and "Chief of Staff asks: Bob's question … has no answer"."""
    harness = Harness(tmp_path / "data", Settings(away_notify=True))
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness, "Answer Bob")
    failure = "Bob's question about the deploy date has no answer: the calendar is unreachable."
    await call_tool(runtime, "report", worker, status="failed", summary=failure)
    await settle(runtime)
    runtime.add_message(harness.router_id or "", "update", f"{failure} Retry it?")
    ids = [item["id"].split(":")[0] for item in runtime.connector.waiting_items()]
    assert ids == ["task"], ids
    assert runtime.tasks.get(task)["status"] == "failed"


def test_a_file_name_keeps_its_underscores_in_a_notification():
    """N13: `textkit/__init__.py` arrived as "textkit/*init*.py"."""
    said = gist("Fixed textkit/__init__.py and `__main__.py`; __this__ matters")
    assert said == "Fixed textkit/__init__.py and `__main__.py`; *this* matters"


async def test_away_merge_and_folder_cards_say_what_their_cards_say(harness, tmp_path):
    """R7: a merge card's item said "(checks pass)" for a PR with no checks; a folder choice
    listed no numbers, nor "No project" nor another folder."""
    runtime = await harness.connect()
    router = harness.router_id
    describe = runtime.connector.describe
    merge = harness.store.create_draft(router, "merge", {
        "label": "acme/app#7", "title": "Fix login", "head": "abc", "checks": "none",
    }, "Fix login")  # fmt: skip
    assert describe({"draft": merge})[1] == "Fix login (no checks)"
    folder = harness.store.create_draft(router, "workspace", {"title": "Fix search",
                                        "options": []}, "Where?")  # fmt: skip
    _what, said, hint = describe({"draft": folder})
    assert "none: no project" in said and hint == "<none or a path>"
    (tmp_path / "app").mkdir()
    chose = await runtime.connector.apply({"draft": folder}, f"QQQQQ {tmp_path / 'app'}", "QQQQQ")
    assert chose == f"chose {(tmp_path / 'app').resolve()}"
    other = harness.store.create_draft(router, "workspace", {"title": "Fix docs", "options": [
        {"path": str(tmp_path / "app"), "configured": True}]}, "Where?")  # fmt: skip
    assert describe({"draft": other})[1].startswith(f"Folders: 1) {tmp_path / 'app'} · none")
    assert await runtime.connector.apply({"draft": other}, "QQQQQ none", "QQQQQ") == (
        "no project: it starts in a folder of its own"
    )
