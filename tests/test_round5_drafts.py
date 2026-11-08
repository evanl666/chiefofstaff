"""Bug bash 5: one reply per message, the owner's edits kept, a reply after its review, what a
merged PR leaves behind, words the owner reads, names for mentioned people."""

import json

import pytest
from conftest import Harness, call_tool, settle, slack_tool

from chiefofstaff.models import DraftDecision, Settings
from chiefofstaff.sources.slack import profile_name

TS = "1794138887.017740"


def entry(channel, user, name, ts, text):
    """One message as Slack's MCP server lists it."""
    link = f"https://acme.slack.com/archives/{channel}/p{ts.replace('.', '')}"
    return (
        f"Channel: <#{channel}> | From: <@{user}> ({name}) | ts: {ts}\n"
        f"Text: {text}\nPermalink: {link}"
    )


async def slack_harness(tmp_path):
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
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


async def test_a_slack_thread_a_worker_took_over_reaches_only_that_worker(tmp_path):
    """L1: Bob wrote again in a thread the Slack session followed (its holding reply) and a
    worker was put on: both drafted answers, contradicting each other."""
    harness, runtime, slack = await slack_harness(tmp_path)
    thread = f"slack:C0DEPLOYS:{TS}"
    runtime.follow(slack, [thread])
    worker, _ = await spawn(runtime, harness, "Merge date for Bob", slack=True, follow=[thread])
    said = "Bob: any date yet?"
    assert await runtime.on_thread("slack", thread, ["slack:1794139000.000100"], said, said)
    await settle(runtime)

    def heard(session_id):
        return [m for m in harness.store.messages(session_id) if m["meta"].get("thread")]

    assert len(heard(worker)) == 1
    assert heard(slack) == [], "the Slack session no longer answers there too"


async def test_one_reply_per_message_the_worker_that_took_over_answers(tmp_path):
    """L1: the Slack session's holding reply stayed beside the worker's answer, and in Bob's DM
    its "I'll confirm shortly" still waited under Needs you after the worker's was sent."""
    harness, runtime, slack = await slack_harness(tmp_path)
    holding = await draft(runtime, harness, slack, "C0DEPLOYS", TS, "I'll check and come back.")
    worker, _ = await spawn(runtime, harness, "Merge date for Bob", slack=True,
                            follow=[f"slack:C0DEPLOYS:{TS}"])  # fmt: skip
    answer = await draft(runtime, harness, worker, "C0DEPLOYS", TS, "It merges Thursday.")
    gone = harness.store.draft(holding["id"])
    assert gone["status"] == "rejected"
    assert gone["outcome"] == "Replaced by \u201cMerge date for Bob\u201d's draft.", "no id"
    refused = await call_tool(runtime, "draft_slack_reply", slack, channel_id="C0DEPLOYS",
                              thread_ts=TS, text="No date yet.")  # fmt: skip
    assert refused.result_type == "failure"
    assert "took this conversation over" in refused.text_result_for_llm
    assert [d["id"] for d in harness.store.drafts()] == [answer["id"]], "one card"

    dm = {"channel_id": "D0BOB0001", "thread_ts": "", "to": "Bob"}
    waiting = harness.store.create_draft(slack, "slack", dm, "I'll confirm shortly.")
    real = harness.store.create_draft(worker, "slack", dm, "It merges Thursday.")
    harness.mcp.sent_link = "https://acme.slack.com/archives/D0BOB0001/p1794139100000100"
    await runtime.drafts.resolve(real["id"], DraftDecision(action="send"))
    settled = harness.store.draft(waiting["id"])
    assert settled["status"] == "rejected"
    assert settled["outcome"] == "Withdrawn: answered by \u201cMerge date for Bob\u201d."
    shown = "Withdrawn: your reply to Bob in a DM, answered by \u201cMerge date for Bob\u201d."
    assert ("notice", shown) in harness.kinds(slack), "told, in words without ids"
    assert any(waiting["id"] in m["meta"].get("prompt", "") for m in
               harness.store.messages(harness.router_id or "")), "the Router hears it"  # fmt: skip


async def test_two_workers_answering_one_thread_both_reach_the_owner(tmp_path):
    """Review of L1: "fix the deploy and the flaky test?" went to two workers following the
    thread; the second one's draft rejected the first one's unseen, and a reply sent did too."""
    harness, runtime, _slack = await slack_harness(tmp_path)
    thread = f"slack:C0DEPLOYS:{TS}"
    deploy, _ = await spawn(runtime, harness, "Fix the deploy", slack=True, follow=[thread])
    flaky, _ = await spawn(runtime, harness, "Fix the flaky test", slack=True, follow=[thread])
    first = await draft(runtime, harness, deploy, "C0DEPLOYS", TS, "Deploy fixed.")
    second = await draft(runtime, harness, flaky, "C0DEPLOYS", TS, "Flaky test fixed.")
    assert {d["id"] for d in harness.store.drafts()} == {first["id"], second["id"]}
    harness.mcp.sent_link = "https://acme.slack.com/archives/C0DEPLOYS/p1794139200000100"
    await runtime.drafts.resolve(second["id"], DraftDecision(action="send"))
    assert harness.store.draft(first["id"])["status"] == "pending", "the owner still sees it"


async def test_the_owners_edit_of_a_held_reply_is_not_withdrawn_by_its_worker(tmp_path):
    """W2: the owner's edit was held (Alice wrote since); Alice's message reached the worker,
    which withdrew the draft: the card and the owner's text were gone."""
    harness, runtime, _slack = await slack_harness(tmp_path)
    worker, _ = await spawn(runtime, harness, "Answer Alice", slack=True,
                            follow=[f"slack:C0DEPLOYS:{TS}"])  # fmt: skip
    harness.mcp.reads["C0DEPLOYS"] = entry("C0DEPLOYS", "U0ALICE1", "Alice Chen", TS, "deploy?")
    card = await draft(runtime, harness, worker, "C0DEPLOYS", TS, "Yes, go ahead.")
    later = entry("C0DEPLOYS", "U0ALICE1", "Alice Chen", "1794139000.000100", "Dana confirmed")
    harness.mcp.reads["C0DEPLOYS"] = later
    mine = "Yes, after 3pm please."
    with pytest.raises(ValueError, match="wrote since"):
        await runtime.drafts.resolve(card["id"], DraftDecision(action="send", text=mine))
    held = [m for m in harness.store.messages(worker) if mine in m["meta"].get("prompt", "")]
    assert held and held[0]["meta"]["deferred"], "it hears their text with its next message"

    refused = await call_tool(runtime, "withdraw_draft", worker, draft_id=card["id"],
                              reason="Alice already confirmed with Dana")  # fmt: skip
    assert refused.result_type == "failure" and mine in refused.text_result_for_llm
    kept = harness.store.draft(card["id"])
    assert kept["status"] == "pending" and kept["final_text"] == mine
    # Drafted again: their card stays (G6, round 6), the new one says it waits beside it.
    again = await draft(runtime, harness, worker, "C0DEPLOYS", TS, "OK, after 3pm then.")
    assert harness.store.draft(card["id"])["status"] == "pending"
    assert (
        again["outcome"]
        == f"Your edited reply there still waits on its own card: \u201c{mine}\u201d"
    )


async def test_a_reply_for_a_task_waits_for_its_review_to_be_posted(tmp_path):
    """W12: "Reviewed and approved #61" could go out before the review (only a comment) was
    posted; it was caught only because it was drafted 0.6 s before the review."""
    harness, runtime, _slack = await slack_harness(tmp_path)
    thread = f"slack:C0DEPLOYS:{TS}"
    worker, task = await spawn(runtime, harness, "Review #7", slack=True,
                               follow=[thread, "github:acme/app#7"])  # fmt: skip
    target = {"repo": "acme/app", "number": 7, "url": "https://github.com/acme/app/pull/7",
              "via": "task", "head_sha": "c0ffee", "verdict": "comment",
              "task_id": task}  # fmt: skip
    review = harness.store.create_draft(worker, "github", target, "Two nits.")
    reply = await draft(runtime, harness, worker, "C0DEPLOYS", TS, "Reviewed and approved #7.")
    with pytest.raises(ValueError, match="post your review of acme/app#7 first"):
        await runtime.drafts.resolve(reply["id"], DraftDecision(action="send"))
    assert not [c for c in harness.mcp.calls if c[0] == "slack_send_message"]
    assert harness.store.draft(reply["id"])["status"] == "pending"

    harness.gh_replies["api repos/acme/app/pulls/7"] = json.dumps(
        {"state": "open", "head": {"sha": "c0ffee", "ref": "feature"}}
    )
    harness.gh_replies["api repos/acme/app/pulls/7/reviews"] = json.dumps({"id": 9})
    session = harness.session(worker)
    await runtime.drafts.resolve(review["id"], DraftDecision(action="send"))
    await settle(runtime)
    told = session.sent[-1]
    assert f"draft {reply['id']}, waits for the owner" in told, "asked to check what it says"
    sent = await runtime.drafts.resolve(reply["id"], DraftDecision(action="send"))
    assert sent["status"] == "sent", "after the review, it goes"


async def merged_pr_task(harness, runtime, number):
    worker, task = await spawn(runtime, harness, f"Fix #{number}")
    pr = f"https://github.com/acme/app/pull/{number}"
    runtime.task_actions.record_opened(task, [pr])
    runtime.tasks.move(task, "review", result=f"Opened {pr} as a draft PR.")
    runtime.task_actions.news(task, "It's still a draft; mark it ready and merge it yourself.")
    comment = {"author": "bob", "body": "Why this?"}
    reply = harness.store.create_draft(worker, "github", {
        "repo": "acme/app", "number": number, "url": pr, "in_reply_to": 5, "comment": comment,
        "task_id": task,
    }, "Because of the login bug.")  # fmt: skip
    return worker, task, reply


async def test_a_pr_merged_from_its_card_leaves_nothing_stale_behind(harness):
    """S12: after "#58 squashed and merged" the worker's reply on it still waited, its Update
    still said it was a draft, and the worker was never told."""
    runtime = await harness.connect()
    worker, task, reply = await merged_pr_task(harness, runtime, 58)
    card = harness.store.create_draft(worker, "merge", {
        "label": "acme/app#58", "repo": "acme/app", "number": 58, "title": "Fix login",
        "url": "https://github.com/acme/app/pull/58", "head": "abc", "task_id": task,
    }, "Fix login")  # fmt: skip
    harness.gh_replies["api repos/acme/app"] = json.dumps({"allow_squash_merge": True})
    harness.gh_replies["pr merge"] = ""
    await runtime.drafts.resolve(card["id"], DraftDecision(action="send"))
    await settle(runtime)
    gone = harness.store.draft(reply["id"])
    assert gone["status"] == "rejected" and gone["outcome"] == "Withdrawn: the PR was merged."
    assert not harness.store.get(f"news:{task}"), "its stale Update gives way"
    told = [m for m in harness.store.messages(worker) if "merged" in m["text"]]
    assert told[-1]["text"] == "You merged it from its card: acme/app#58 squashed and merged."
    assert (
        "out of draft" in told[-1]["meta"]["prompt"] and reply["id"] in told[-1]["meta"]["prompt"]
    )


async def test_a_pr_seen_merged_withdraws_what_was_drafted_on_it(harness):
    """S12: the same when the PR is seen merged on GitHub."""
    runtime = await harness.connect()
    worker, task, reply = await merged_pr_task(harness, runtime, 59)
    runtime.follow(worker, ["github:acme/app#59"])
    runtime.unfollow("github:acme/app#59", finished=True, merged=True)
    await settle(runtime)
    assert harness.store.draft(reply["id"])["outcome"] == "Withdrawn: the PR was merged."
    assert json.loads(harness.store.get(f"news:{task}"))["text"] == "acme/app#59 was merged."
    runtime.task_actions.news(task, "Also answered Bob.")
    runtime.task_actions.pr_merged(worker, "github:acme/app#59", task)  # Seen again later.
    assert json.loads(harness.store.get(f"news:{task}"))["text"] == "Also answered Bob."


async def test_outcomes_are_told_to_the_owner_in_their_words(tmp_path):
    """A8: "For “…” the owner chose /path (now in list_sessions().projects)", "Sent by the owner:
    Slack reply to DM, Slack DM with D0BOB", "The owner approved draft dfc6b6577"."""
    harness, runtime, _slack = await slack_harness(tmp_path)
    router = harness.router_id or ""
    folder = tmp_path / "notes"
    folder.mkdir()
    card = harness.store.create_draft(router, "workspace", {"title": "Fix search"}, "Where?")
    await runtime.drafts.resolve(card["id"], DraftDecision(action="choose", choice=str(folder)))
    assert harness.kinds(router)[-1] == (
        "notice",
        f"You chose {folder.resolve()} for \u201cFix search\u201d.",
    )
    harness.store.set("slacknames", json.dumps({"C0DEPLOYS": "deploys"}))
    worker, _ = await spawn(runtime, harness, "Answer Bob", slack=True)
    reply = await draft(runtime, harness, worker, "C0DEPLOYS", TS, "Thursday.", to="Bob")
    await runtime.drafts.resolve(reply["id"], DraftDecision(action="send"))
    await settle(runtime)
    shown = "your reply to Bob in a thread in #deploys"
    assert ("notice", f"Sent: {shown}.") in harness.kinds(worker)
    assert ("notice", f"Sent: {shown}") in harness.kinds(router)


async def test_people_a_quoted_message_mentions_are_named(tmp_path):
    """A10: a draft card quoted "@U0ALICE1 might ask too": only senders had names."""
    harness, runtime, slack = await slack_harness(tmp_path)
    harness.mcp.tools.append(slack_tool("slack_read_user_profile"))
    await runtime.slack.discover()
    said = "cc <@U0ALICE1> might ask too"
    harness.mcp.reads["C0DEPLOYS"] = entry("C0DEPLOYS", "U0BOB0001", "Bob", TS, said)
    harness.mcp.results[""] = "Real name: Alice Chen\nDisplay name: alice"
    await draft(runtime, harness, slack, "C0DEPLOYS", TS, "Noted.")
    assert runtime.slack_names()["U0ALICE1"] == "Alice Chen"
    await draft(runtime, harness, slack, "C0DEPLOYS", TS, "Noted, thanks.")
    asked = [call for call in harness.mcp.calls if call[0] == "slack_read_user_profile"]
    assert asked == [("slack_read_user_profile", {"user_id": "U0ALICE1"})], "looked up once"
    assert profile_name('{"user": {"profile": {"real_name": "Dana Wu"}}}') == "Dana Wu"


async def test_a_dm_reply_is_not_said_to_be_in_a_dm_twice(harness):
    """Live (browser smoke): "your reply to Alice Chen (DM) in a DM"."""
    runtime = await harness.connect()
    draft = {"kind": "slack", "target": {"channel_id": "D0ALICE01", "thread_ts": "",
                                         "to": "Alice Chen (DM)"}}  # fmt: skip
    assert runtime.drafts.shown(draft) == "your reply to Alice Chen (DM)"
    draft["target"]["to"] = "Alice Chen"
    assert runtime.drafts.shown(draft) == "your reply to Alice Chen in a DM"
