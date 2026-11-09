"""Bug bash round 9: away replies (G1-G3, G10, G13-G15), what the owner is told away (D1, D5,
D6), drafts that answer another message (D2), reviews drafted twice (H2, H7), and whose
question the owner answered (D7)."""

import json
import time
from datetime import UTC, datetime

import pytest
from conftest import Harness, call_tool, settle
from test_round6_drafts import TS, away, draft, entry, slack_harness, spawn, told
from test_round7_away import code_of, drafted, reply, status

from chiefofstaff.connector import answer_part
from chiefofstaff.models import DraftDecision, Settings

DM = "D0SELF001"
OWNER = "U0OWNER1"


def stamp(after: float) -> str:
    """A Slack ts `after` seconds from now: after the notification sent just now."""
    return f"{time.time() + after:.6f}"


def iso(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), UTC).isoformat()


def runs(runtime, messenger: str) -> int:
    return len(runtime.tasks.for_session(messenger))


def sends(harness) -> list[dict]:
    return [args for tool, args in harness.mcp.calls if tool == "slack_send_message"]


async def own_dm(tmp_path, *texts):
    """Drafts told in the owner's own Slack DM (version A); the Slack threads read by their ts
    (`threads`)."""
    settings = Settings(away_notify=True, away_how="my own Slack DM", slack_enabled=True)
    harness = Harness(tmp_path / "data", settings)
    runtime = await away(harness)
    worker, _task = await spawn(runtime, harness, "Answer the team")
    for text in texts:
        await call_tool(runtime, "draft_reply", worker, text=text, to="Bob", where="email")
    await runtime.connector.notify_away()
    await settle(runtime)
    messenger = runtime.toolset.last_special("messenger")["id"]
    sent = stamp(1)
    harness.mcp.sent_link = f"https://acme.slack.com/archives/{DM}/p{sent.replace('.', '')}"
    await call_tool(runtime, "send_owner_slack_dm", messenger, version="A")
    body = sends(harness)[-1]["message"]
    await call_tool(runtime, "report_notified", messenger, ok=True, where="your own Slack DM",
                    method="send_owner_slack_dm", replies="read_owner_slack_dm")  # fmt: skip
    harness.session(messenger).emit("session.idle")
    await settle(runtime)
    threads: dict[str, str] = {}
    plain = harness.mcp.call

    async def call(name, arguments):
        if name == "slack_read_thread":
            harness.mcp.calls.append((name, arguments))
            return threads.get(arguments["message_ts"], "")
        return await plain(name, arguments)

    harness.mcp.call = call
    harness.mcp.reads[DM] = entry(DM, OWNER, "Me", sent, body)
    return harness, runtime, messenger, body, sent, threads


async def test_a_reply_in_the_notifications_thread_is_read(tmp_path):
    """G1: "5NG5S no" in the thread of the notification in the owner's own DM was never read."""
    harness, runtime, messenger, body, sent, threads = await own_dm(tmp_path, "Friday works.")
    code = code_of(body, "Friday works")
    threads[sent] = (
        entry(DM, OWNER, "Me", sent, body)
        + "\n\n"
        + entry(DM, OWNER, "Me", stamp(10), f"{code} no")
    )
    before = runs(runtime, messenger)
    harness.store.set("away_read_at", "0")
    await runtime.connector.read_replies()
    await settle(runtime)
    assert runs(runtime, messenger) == before + 1, "they wrote in its thread: the messenger reads"
    read = (await call_tool(runtime, "read_owner_slack_dm", messenger)).text_result_for_llm
    assert f") {code} no" in read and "send · no" not in read, read


async def test_a_reply_without_a_code_answers_the_only_code_it_follows(tmp_path):
    """G2: "yes send that one to Alice" after a one-item notification was dropped silently."""
    harness, runtime, messenger, body = await drafted(tmp_path, "Thanks Alice, merged.")
    code = code_of(body, "Thanks Alice")
    harness.store.set("away_read_at", "0")
    await runtime.connector.read_replies()
    await settle(runtime)
    read = harness.session(messenger["id"]).sent[-1]
    assert "answers the only code that waits" in read and code in read, read
    harness.session(messenger["id"]).emit("session.idle")
    await settle(runtime)
    # One written before that notification answers nothing it told.
    stale = {"code": code, "text": "send", "intent": "send", "at": "2020-01-01T00:00:00Z"}
    assert await reply(runtime, messenger, stale) == "Nothing to apply."
    now = datetime.now(UTC).isoformat()
    said = await reply(runtime, messenger, {"code": "", "text": "yes send that one to Alice",
                                            "intent": "send", "at": now})  # fmt: skip
    assert f"{code}: marked as sent" in said, said


async def test_a_reply_without_a_code_after_several_is_asked_back_once(tmp_path):
    """G2: with several codes open, a reply that names none is asked back (which one?), once;
    one after all is answered is just seen."""
    harness, runtime, messenger, body, _sent, _threads = await own_dm(
        tmp_path, "Friday works.", "Monday then."
    )
    first, second = code_of(body, "Friday works"), code_of(body, "Monday then")
    vague = stamp(10)
    harness.mcp.reads[DM] += "\n\n" + entry(DM, OWNER, "Me", vague, "yes send that one")
    seen = harness.store.get("away_dm_seen")
    await call_tool(runtime, "read_owner_slack_dm", messenger)
    assert harness.store.get("away_dm_seen") == seen, "not seen before it is reported"
    words = {"code": "", "text": "yes send that one", "intent": "unclear", "at": iso(vague)}
    said = await reply(runtime, {"id": messenger}, words)
    assert "asked which code" in said, said
    [asked] = [s for s in sends(harness) if "Which one?" in s["message"]]
    assert asked["channel_id"] == OWNER
    assert asked["message"] == f"Which one? Reply with its code: {first}, {second}"
    await reply(runtime, {"id": messenger}, words)
    assert len([s for s in sends(harness) if "Which one?" in s["message"]]) == 1, "once"
    for code in (first, second):
        await reply(runtime, {"id": messenger}, {"code": code, "text": f"{code} no",
                                                 "intent": "no"})  # fmt: skip
    thanks = stamp(20)
    harness.mcp.reads[DM] += "\n\n" + entry(DM, OWNER, "Me", thanks, "thanks!")
    await call_tool(runtime, "read_owner_slack_dm", messenger)
    assert float(harness.store.get("away_dm_seen")) == pytest.approx(float(thanks))


async def test_a_reply_to_a_code_answered_already_is_told_where_the_owner_is(tmp_path):
    """G3: "K43HE wait, don't send that yet" after it was sent was said only on Home."""
    harness, runtime, messenger, body = await drafted(tmp_path, "Looks good to me.")
    code = code_of(body, "Looks good")
    await reply(runtime, messenger, {"code": code, "text": f"{code} send", "intent": "send"})
    wait = {"code": code, "text": f"{code} wait, don't send that yet", "intent": "unclear"}
    said = await reply(runtime, messenger, wait)
    assert "you answered this code already (sent already)" in said, said
    _messenger, note = await told(runtime, harness)
    assert f"{code} was already sent at " in note, note
    assert not runtime.connector.away_notes(), "said once"
    assert await reply(runtime, messenger, wait) == "Nothing to apply."
    assert not runtime.connector.away_notes()


async def test_the_dm_is_seen_only_once_its_replies_are_accepted(tmp_path):
    """G13: a report refused (no intent for a draft) marked the replies read: lost."""
    harness, runtime, messenger, body, _sent, _threads = await own_dm(tmp_path, "Friday works.")
    code = code_of(body, "Friday works")
    harness.mcp.reads[DM] += "\n\n" + entry(DM, OWNER, "Me", stamp(10), f"{code} ok")
    await call_tool(runtime, "read_owner_slack_dm", messenger)
    seen = harness.store.get("away_dm_seen")
    refused = await call_tool(runtime, "report_owner_replies", messenger,
                              replies=[{"code": code, "text": f"{code} ok"}])  # fmt: skip
    assert refused.result_type == "failure", refused.text_result_for_llm
    assert harness.store.get("away_dm_seen") == seen
    before = runs(runtime, messenger)
    harness.store.set("away_read_at", "0")
    await runtime.connector.read_replies()
    assert runs(runtime, messenger) == before + 1, "read again"


async def test_the_own_dm_is_checked_every_few_minutes_all_day(tmp_path):
    """G14: two hours after a notification, a reply in their own DM waited half an hour."""
    harness, runtime, messenger, body, _sent, _threads = await own_dm(tmp_path, "Friday works.")
    code = code_of(body, "Friday works")
    last = json.loads(harness.store.get("away_last"))
    two_hours_ago = datetime.fromtimestamp(time.time() - 7200, UTC).isoformat()
    harness.store.set("away_last", json.dumps({**last, "at": two_hours_ago}))
    before = runs(runtime, messenger)
    harness.store.set("away_read_at", str(time.time() - 6 * 60))
    harness.store.set("away_ran_at", str(time.time() - 6 * 60))
    await runtime.connector.read_replies()
    assert runs(runtime, messenger) == before, "nothing new: no run"
    harness.mcp.reads[DM] += "\n\n" + entry(DM, OWNER, "Me", stamp(10), f"{code} send")
    harness.store.set("away_read_at", str(time.time() - 6 * 60))
    await runtime.connector.read_replies()
    assert runs(runtime, messenger) == before + 1, "they wrote: read now"


@pytest.mark.parametrize(
    ("text", "answer"),
    [
        ("no 52YVY, too long", "no, too long"),
        ("ok so for lqy55: send please, thanks", "ok so for: send please, thanks"),
        ("send it - 52YVY thanks", "send it - thanks"),
    ],
)
def test_words_on_both_sides_of_a_code_keep_their_separation(text, answer):
    """G15: "no 52YVY, too long" was the reason "no too long"."""
    code = "LQY55" if "lqy55" in text else "52YVY"
    assert answer_part(text, code, {code}) == answer


async def test_asked_again_the_reason_is_not_said_twice_and_the_draft_has_its_line(tmp_path):
    """G15, D5: "Not sent yet · …" then "not sent: answer "send" … or "no". Thanks Bob, …"."""
    harness, runtime, messenger, body = await drafted(tmp_path, "Thanks Bob, shipped.")
    code = code_of(body, "Thanks Bob")
    await reply(runtime, messenger, {"code": code, "text": f"{code} hmm", "intent": "unclear"})
    harness.session(messenger["id"]).emit("session.idle")
    await settle(runtime)
    _messenger, again = await told(runtime, harness)
    section = next(part for part in again.split("\n\n") if code in part)
    lines = section.splitlines()
    assert lines[0] == f"*[{code}]* Not sent yet \u00b7 Draft to approve \u00b7 Bob", section
    assert lines[1] == 'Answer "send" when it should go, or "no".', section
    assert lines[2] == "Thanks Bob, shipped.", section


async def test_an_urgent_router_note_reaches_the_owner_away(tmp_path):
    """D1: the Router's urgent note (Home: "The Router · Urgent") was never sent away."""
    harness = Harness(tmp_path / "data", Settings(away_notify=True, away_how="DM me"))
    runtime = await away(harness)
    runtime.add_message(harness.router_id, "update", "Bob says prod is down and needs you now.",
                        {"urgent": True})  # fmt: skip
    messenger, body = await told(runtime, harness)
    code = code_of(body, "prod is down")
    assert f"*[{code}]* Urgent \u00b7 Chief of Staff" in body, body
    router = harness.session(harness.router_id)
    said = await reply(runtime, messenger, {"code": code, "text": f"{code} on it, call me"})
    assert f"{code}: sent to the Router" in said and "on it, call me" in router.sent[-1]
    runtime.add_message(harness.router_id, "update", "Alice is blocked on you today.",
                        {"urgent": True})  # fmt: skip
    [item] = runtime.connector.waiting_items()
    assert item["id"].startswith("router:"), item
    runtime.update_seen(item["ask"]["id"])
    assert runtime.connector.waiting_items() == [], "seen on Home: not told"


async def test_a_chinese_owner_reads_the_notification_in_chinese(tmp_path):
    """D5, D6: "Chief of Staff needs you (3)", "Draft to approve · …", "→ not done: not sent:
    “先别” sounds like not now…" for an owner who writes Chinese."""
    harness = Harness(tmp_path / "data", Settings(away_notify=True, away_how="DM me"))
    runtime = await away(harness)
    harness.store.set("owner_language", "Chinese")
    worker, _task = await spawn(runtime, harness, "Answer Bob")
    await call_tool(runtime, "draft_reply", worker, text="Friday works.", to="Bob", where="email")
    messenger, body = await told(runtime, harness)
    code = code_of(body, "Friday works")
    assert "*Chief of Staff \u9700\u8981\u4f60\uff081\uff09*" in body, body
    assert "\u5f85\u6279\u51c6\u7684\u8349\u7a3f \u00b7 Bob" in body, body
    assert f"`{code} send \u00b7 no`\uff08\u53d1\u9001 \u00b7 \u4e0d\u53d1\uff09" in body, body
    assert "Reply" not in body, body
    later = "\u5148\u522b\u53d1"  # "Don't send yet."
    said = await reply(runtime, messenger, {"code": code, "text": f"{code} {later}",
                                            "intent": "unclear"})  # fmt: skip
    assert "\u672a\u5b8c\u6210\uff1a\u672a\u53d1\u9001\uff1a" in said, said
    assert "not sent" not in said and "sounds like" not in said, said


async def test_a_reply_to_another_message_does_not_replace_the_first(tmp_path):
    """D2: Bob asked for an ETA, then about slugify in the same thread: the answer to his
    question was replaced by the slugify one, silently."""
    harness, runtime, slack = await slack_harness(tmp_path)
    channel = "C0ACME001"
    harness.mcp.reads[channel] = entry(channel, "U0BOB0001", "Bob", TS, "ETA on format_money?")
    eta = await draft(runtime, harness, slack, channel, TS, "Friday.")
    later = "1794157927.201838"
    harness.mcp.reads[channel] = entry(channel, "U0BOB0001", "Bob", later, "slugify unicode?")
    result = await call_tool(runtime, "draft_slack_reply", slack, channel_id=channel,
                             thread_ts=TS, text="It keeps accents.")  # fmt: skip
    said = result.text_result_for_llm
    slug = harness.store.drafts()[-1]
    assert {d["id"] for d in harness.store.drafts()} == {eta["id"], slug["id"]}, "both wait"
    assert "answers an earlier message" in said and f"withdraw_draft {eta['id']}" in said
    assert slug["outcome"].startswith("Another reply there waits too"), slug["outcome"]
    # A newer answer to the same message replaces it, saying why.
    again = await draft(runtime, harness, slack, channel, TS, "Accents stay, as is.")
    assert {d["id"] for d in harness.store.drafts()} == {eta["id"], again["id"]}
    outcome = harness.store.draft(slug["id"])["outcome"]
    assert outcome == "Replaced by a newer draft: it answers the same message " + (
        "(\u201cBob: slugify unicode?\u201d)."
    )


async def test_a_dm_to_a_user_id_reads_as_a_dm(harness):
    """G10: a reply drafted to a user id read "your reply to Alice Chen in #Alice Chen"."""
    runtime = await harness.connect()
    harness.store.set("slackname:U0ALICE01", "Alice Chen")
    reply_ = harness.store.create_draft(
        harness.router_id, "slack", {"channel_id": "U0ALICE01", "to": "Alice Chen"}, "Hi."
    )
    assert runtime.drafts.shown(reply_) == "your reply to Alice Chen in a DM"


async def test_the_owner_answered_the_slack_sessions_question(tmp_path):
    """D7: "The owner answered “Slack”'s question"."""
    harness, runtime, slack = await slack_harness(tmp_path)
    await call_tool(runtime, "report", slack, status="question", summary="Tell Bob it moved?")
    [question] = runtime.asks.session_questions(slack)
    await runtime.task_actions.answer_question(question["id"], "Yes")
    shown = [text for kind, text in harness.kinds(harness.router_id) if kind == "notice"]
    assert "You answered the Slack session's question" in shown, shown


async def reviewer(harness, runtime):
    worker, _task = await spawn(runtime, harness, "Review https://github.com/acme/app/pull/92")
    harness.store.set("ghaccount:acme", "me")
    harness.gh_replies["api repos/acme/app/pulls/92"] = json.dumps(
        {"state": "open", "head": {"sha": "c0ffee", "ref": "fix"}, "user": {"login": "bob"}}
    )
    return worker


async def test_a_newer_review_of_a_pr_replaces_the_older_and_one_sent_settles_the_rest(tmp_path):
    """H2: three "Review of …#92" cards waited at once; after one was sent, another stayed
    sendable."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    worker = await reviewer(harness, runtime)
    await call_tool(runtime, "draft_github_review", worker, repo="acme/app", number=92,
                    body="Summary only.")  # fmt: skip
    [first] = harness.store.drafts()
    newer = await call_tool(runtime, "draft_github_review", worker, repo="acme/app", number=92,
                            body="Summary, with the edge cases.")  # fmt: skip
    assert f"It replaces your earlier review {first['id']}" in newer.text_result_for_llm
    gone = harness.store.draft(first["id"])
    assert gone["status"] == "rejected" and gone["outcome"].startswith("Replaced by a newer draft")
    [second] = harness.store.drafts()
    # A reply to a review comment is no review: it stays.
    comment = {"author": "bob", "body": "Why?"}
    harness.store.create_draft(worker, "github", {"repo": "acme/app", "number": 92,
                               "in_reply_to": 5, "comment": comment}, "Because.")  # fmt: skip
    # One drafted another way (before this fix, say) is settled once a review is sent.
    stray = harness.store.create_draft(worker, "github", second["target"], "Old summary.")
    harness.gh_replies["api repos/acme/app/pulls/92/reviews"] = json.dumps({"id": 9})
    await runtime.drafts.resolve(second["id"], DraftDecision(action="send"))
    stray = harness.store.draft(stray["id"])
    assert stray["status"] == "rejected", stray
    assert stray["outcome"] == "Withdrawn: a review of this PR was sent already."
    assert [d["text"] for d in harness.store.drafts()] == ["Because."]


async def test_a_stale_review_says_why_once_without_retry_advice(tmp_path):
    """H7: "…nothing was published.. Check before retrying." for a draft failed for good."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    worker = await reviewer(harness, runtime)
    await call_tool(runtime, "draft_github_review", worker, repo="acme/app", number=92,
                    body="LGTM")  # fmt: skip
    harness.gh_replies["api repos/acme/app/pulls/92"] = json.dumps(
        {"state": "open", "head": {"sha": "beef", "ref": "fix"}, "user": {"login": "bob"}}
    )
    [review] = harness.store.drafts()
    with pytest.raises(RuntimeError) as failed:
        await runtime.drafts.resolve(review["id"], DraftDecision(action="send"))
    said = str(failed.value)
    assert "changed after the draft was written" in said, said
    assert ".." not in said and "Check before retrying" not in said, said


async def test_a_reply_without_a_code_is_asked_about_while_an_earlier_code_waits(tmp_path):
    """Code review: X told first, Y in a second notification (new items only): "yes send it"
    with no code, which the messenger passed with the latest code, sent Y while it answered X."""
    harness, runtime, _messenger, body = await drafted(tmp_path, "Friday works.")
    first = code_of(body, "Friday works")
    worker, _task = await spawn(runtime, harness, "Answer Carol")
    await call_tool(runtime, "draft_reply", worker, text="Monday then.", to="Carol",
                    where="email")  # fmt: skip
    messenger, later = await told(runtime, harness)
    second = code_of(later, "Monday then")
    assert first not in later, "the second notification lists the new item only"
    now = datetime.now(UTC).isoformat()
    said = await reply(runtime, messenger, {"code": second, "text": "yes send it",
                                            "intent": "send", "at": now})  # fmt: skip
    assert "asked which code" in said, said
    assert status(harness, "Friday works") == status(harness, "Monday then") == "pending"


async def test_a_notification_thread_that_cannot_be_read_does_not_stop_the_rest(tmp_path):
    """Code review: one notification deleted (thread_not_found) made every read of the owner's
    own DM fail for a day, top-level replies too."""
    from chiefofstaff.sources.slack import SlackError

    harness, runtime, messenger, body, _sent, _threads = await own_dm(tmp_path, "Friday works.")
    code = code_of(body, "Friday works")
    harness.mcp.reads[DM] += "\n\n" + entry(DM, OWNER, "Me", stamp(10), f"{code} send")
    reading = harness.mcp.call

    async def call(name, arguments):
        if name == "slack_read_thread":
            raise SlackError("Slack rejected slack_read_thread: thread_not_found")
        return await reading(name, arguments)

    harness.mcp.call = call
    read = await call_tool(runtime, "read_owner_slack_dm", messenger)
    assert read.result_type == "success" and f"{code} send" in read.text_result_for_llm
    assert json.loads(harness.store.get("away_slack_dm"))["threads"] == [], "not read again"
