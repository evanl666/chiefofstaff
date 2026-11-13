"""What happens when things go wrong at the worst moment.

Each test is one failure the runtime must survive without losing, duplicating or stranding
work. See also test_backend_contract.py (abort stops a running command, with real Copilot).
"""

import asyncio
import json
import time

import pytest
from conftest import FakeSession, Harness, call_tool, settle, task_of

from chiefofstaff.delivery import digest
from chiefofstaff.models import DraftDecision, Settings
from chiefofstaff.sources.github import POSTED_MARK


def interrupt_after_send(harness, session_id, status="idle"):
    """As if Chief of Staff stopped after its last send went out but before the mark was saved
    (and so before the session was marked working)."""
    last = harness.store.one(
        "SELECT max(id) AS id FROM messages WHERE session_id=? AND json_extract(meta, '$.prompt')"
        " IS NOT NULL",
        (session_id,),
    )["id"]
    before = harness.store.one(
        "SELECT max(id) AS id FROM messages WHERE session_id=? AND id<? AND"
        " json_extract(meta, '$.prompt') IS NOT NULL",
        (session_id, last),
    )["id"]
    text = harness.session(session_id).sent[-1]
    raw = harness.session(session_id).raw_sent[-1]
    harness.store.set(f"sent:{session_id}", str(before or 0))
    harness.store.set(f"sending:{session_id}", json.dumps({"upto": last, "digest": digest(raw)}))
    harness.store.update_session(session_id, status=status)
    return text


async def test_a_send_that_arrived_before_a_stop_is_not_sent_again(tmp_path):
    first = Harness(tmp_path / "data")
    runtime = await first.connect()
    await runtime.owner_send(first.router_id, "Ship it")
    router = first.session(first.router_id)
    router.emit("session.idle")
    await settle(runtime)
    text = interrupt_after_send(first, first.router_id)
    await runtime.stop()

    second = Harness(tmp_path / "data", client=first.client)
    await second.connect()
    assert second.session(second.router_id).sent.count(text) == 1, "no duplicate"
    assert not second.store.get(f"sending:{second.router_id}")


async def test_a_send_that_did_not_arrive_goes_out_after_a_restart(tmp_path):
    first = Harness(tmp_path / "data")
    runtime = await first.connect()
    worker = json.loads(
        (
            await call_tool(
                runtime, "spawn_worker", first.router_id, title="A", instructions="Do A"
            )
        ).text_result_for_llm
    )["session_id"]
    first.session(worker).emit("session.idle")
    await settle(runtime)
    await runtime.owner_send(worker, "Also B")
    text = interrupt_after_send(first, worker)
    first.session(worker).sent.pop()  # It never reached Copilot.
    first.session(worker).raw_sent.pop()
    await runtime.stop()

    second = Harness(tmp_path / "data", client=first.client)
    restarted = await second.connect()
    await settle(restarted)
    assert second.session(worker).sent[-1] == text, "sent exactly once, after the restart"
    assert second.session(worker).sent.count(text) == 1


async def test_late_events_from_a_replaced_router_session_are_ignored(harness):
    runtime = await harness.connect()
    old = harness.session(harness.router_id)
    old.emit("assistant.usage", model="m", input_tokens=200_000)
    old.emit("session.idle")
    await settle(runtime)
    assert harness.store.get(f"copilot:{harness.router_id}"), "the Router rotated"
    before = len(harness.store.messages(harness.router_id))
    old.emit("assistant.message", content="A late reply", tool_requests=None)
    old.emit("assistant.turn_start")
    await settle(runtime)
    assert len(harness.store.messages(harness.router_id)) == before
    assert harness.store.session(harness.router_id)["status"] == "idle"


async def test_a_worker_that_keeps_going_after_reporting_done(tmp_path):
    harness = Harness(tmp_path / "data", Settings(max_workers=1))
    runtime = await harness.connect()
    first = json.loads(
        (
            await call_tool(runtime, "spawn_worker", harness.router_id, title="A", instructions="A")
        ).text_result_for_llm
    )
    worker = harness.session(first["session_id"])
    await call_tool(runtime, "report", first["session_id"], status="done", summary="Done")
    queued = json.loads(
        (
            await call_tool(
                runtime,
                "spawn_worker",
                harness.router_id,
                title="B",
                instructions="B",
                why_new="separate",
            )
        ).text_result_for_llm
    )
    assert queued["status"] == "queued", "A's slot is busy until its turn ends"
    worker.emit("tool.execution_start", tool_name="bash", arguments={"command": "git push"})
    await settle(runtime)
    assert (
        harness.store.one("SELECT status FROM tasks WHERE id=?", (first["task_id"],))["status"]
        == "review"
    )
    worker.emit("session.idle")
    await settle(runtime)
    assert harness.store.session(first["session_id"])["status"] == "done"
    assert harness.store.session(queued["session_id"])["status"] == "working"


async def test_cancelling_a_running_task_frees_its_slot(tmp_path):
    harness = Harness(tmp_path / "data", Settings(max_workers=1))
    runtime = await harness.connect()

    async def spawn(title):
        result = await call_tool(
            runtime,
            "spawn_worker",
            harness.router_id,
            title=title,
            instructions=title,
            why_new="separate",
        )
        return json.loads(result.text_result_for_llm)

    running, waiting = await spawn("Long build"), await spawn("Next")
    assert waiting["status"] == "queued"
    await runtime.task_actions.cancel(running["task_id"], "Wrong branch")
    await settle(runtime)
    assert harness.session(running["session_id"]).aborted, "the turn and its command stop"
    assert harness.store.session(waiting["session_id"])["status"] == "working"
    assert task_of(harness, waiting["session_id"]) == waiting["task_id"]


async def test_cancelling_one_of_a_workers_tasks_leaves_its_other_task_running(harness):
    runtime = await harness.connect()
    result = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="Build", instructions="Build"
    )
    build = json.loads(result.text_result_for_llm)
    worker = build["session_id"]
    await call_tool(
        runtime,
        "message_session",
        harness.router_id,
        session_id=worker,
        text="Also update the docs",
        task="new",
        why_new="test",
        earlier="keep",
        title="Docs",
    )
    docs = task_of(harness, worker)
    await runtime.task_actions.cancel(docs, "Not needed")
    await settle(runtime)
    assert not harness.session(worker).aborted, "its turn on the other task goes on"
    assert harness.store.one("SELECT status FROM tasks WHERE id=?", (build["task_id"],)) == {
        "status": "running"
    }
    assert task_of(harness, worker) == build["task_id"]
    assert harness.store.session(worker)["status"] == "working"
    told = harness.session(worker).sent[-1]
    assert 'type="cancelled"' in told and docs in told, "it hears of it within its turn"
    session = harness.session(worker)
    session.emit("assistant.message", content="Still building.")
    session.emit("session.idle")
    await settle(runtime)
    assert "report-missing" in session.sent[-1], "it still owes a report on its build"


async def test_cancelling_a_queued_workers_new_task_drops_only_that_instruction(tmp_path):
    harness = Harness(tmp_path / "data", Settings(max_workers=1))
    runtime = await harness.connect()

    async def spawn(title):
        result = await call_tool(
            runtime,
            "spawn_worker",
            harness.router_id,
            title=title,
            instructions=title,
            why_new="separate",
        )
        return json.loads(result.text_result_for_llm)

    first, second = await spawn("Long build"), await spawn("Migrate")
    assert second["status"] == "queued"
    worker = second["session_id"]
    await call_tool(
        runtime,
        "message_session",
        harness.router_id,
        session_id=worker,
        text="Then rename the table",
        task="new",
        why_new="test",
        title="Rename",
    )
    rename = task_of(harness, worker)
    await runtime.task_actions.cancel(rename, "Keep the name")
    await settle(runtime)
    assert harness.store.session(worker)["status"] == "queued", "it still waits for a slot"
    assert task_of(harness, worker) == second["task_id"]
    await call_tool(runtime, "report", first["session_id"], status="done", summary="Built")
    harness.session(first["session_id"]).emit("session.idle")
    await settle(runtime)
    started = harness.session(worker).sent
    assert len(started) == 1 and "Migrate" in started[0]
    assert "rename the table" not in started[0] and 'type="cancelled"' not in started[0]


async def test_drafts_interrupted_while_sending_are_checked_before_anything_is_resent(tmp_path):
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    slack = harness.store.create_draft(
        harness.router_id, "slack", {"channel_id": "C1", "thread_ts": "", "to": "Ann"}, "On it!"
    )
    review = harness.store.create_draft(
        harness.router_id,
        "github",
        {"repo": "acme/app", "number": 7, "head_sha": "abc"},
        "Looks good.",
    )
    lost = harness.store.create_draft(
        harness.router_id, "slack", {"channel_id": "C2", "thread_ts": "", "to": "Bo"}, "Later"
    )
    harness.store.execute("UPDATE drafts SET status='uncertain'")
    stamp = f"{time.time():.6f}".replace(".", "")
    harness.mcp.results['"On it!" from:me'] = (
        f"From you in #general: On it!\nhttps://acme.slack.com/archives/C1/p{stamp}"
    )
    # An "On it!" from before the draft was written is another message.
    harness.mcp.results['"Later" from:me'] = (
        "From you in #random: Later\nhttps://acme.slack.com/archives/C2/p1712345678000200"
    )
    harness.gh_replies["api repos/acme/app/pulls/7/reviews"] = json.dumps(
        [{"body": "Something else", "html_url": "https://github.com/acme/app/pull/7#r1"}]
    )
    await runtime.drafts.check_uncertain()
    assert harness.store.draft(slack["id"])["status"] == "sent"
    assert harness.store.draft(review["id"])["status"] == "pending", "not published: approve again"
    assert harness.store.draft(lost["id"])["status"] == "uncertain", "Slack search may lag"
    assert "Send it again" in harness.store.draft(lost["id"])["outcome"]
    assert not [call for call in harness.mcp.calls if call[0] == "slack_send_message"]


async def test_an_interrupted_review_and_reply_posted_with_the_mark_are_found(harness):
    """Code review: what Chief of Staff posted carried its mark (before 0.1.43); after a
    restart, the review and the reply it posted must be found as posted, not offered to post
    again."""
    runtime = await harness.connect()
    target = {"repo": "acme/app", "number": 7, "head_sha": "abc"}
    review = harness.store.create_draft(harness.router_id, "github", target, "Looks good.")
    reply = harness.store.create_draft(
        harness.router_id, "github", {**target, "in_reply_to": 21}, "Fixed in f61047fa."
    )
    harness.store.execute("UPDATE drafts SET status='uncertain'")
    harness.gh_replies["api repos/acme/app/pulls/7/reviews"] = json.dumps(
        [
            {
                "body": f"Looks good.\n\n{POSTED_MARK}",
                "html_url": "https://github.com/acme/app/pull/7#r1",
            }
        ]
    )
    harness.gh_replies["api repos/acme/app/pulls/7/comments"] = json.dumps(
        [{"in_reply_to_id": 21, "body": f"Fixed in f61047fa.\n\n{POSTED_MARK}",
          "html_url": "https://github.com/acme/app/pull/7#discussion_r2"}]
    )  # fmt: skip
    await runtime.drafts.check_uncertain()
    assert harness.store.draft(review["id"])["status"] == "sent"
    assert harness.store.draft(reply["id"])["status"] == "sent"


async def test_an_interrupted_approval_without_a_body_is_found_not_reposted(harness):
    runtime = await harness.connect()
    target = {"repo": "acme/app", "number": 7, "head_sha": "abc", "verdict": "approve"}
    bare = harness.store.create_draft(harness.router_id, "github", target, "")
    other = harness.store.create_draft(harness.router_id, "github", {**target, "number": 8}, "")
    harness.store.execute("UPDATE drafts SET status='uncertain'")
    harness.gh_replies["auth status --json hosts"] = json.dumps(
        {"hosts": {"github.com": [{"login": "Evan", "active": True, "state": "success"}]}}
    )
    harness.gh_replies["api repos/acme/app/pulls/7/reviews"] = json.dumps(
        [{"user": {"login": "evan"}, "state": "APPROVED", "commit_id": "abc", "body": "",
          "submitted_at": "2999-01-01T00:00:00Z", "html_url": "https://x/pull/7#r9"}]
    )  # fmt: skip
    harness.gh_replies["api repos/acme/app/pulls/8/reviews"] = json.dumps(
        [{"user": {"login": "bob"}, "state": "APPROVED", "commit_id": "abc", "body": "",
          "submitted_at": "2999-01-01T00:00:00Z", "html_url": "https://x/pull/8#r1"}]
    )  # fmt: skip
    await runtime.drafts.check_uncertain()
    assert harness.store.draft(bare["id"])["status"] == "sent"
    assert harness.store.draft(other["id"])["status"] == "pending", "Bob's approval is not ours"


async def test_the_owner_can_settle_a_draft_while_it_is_being_checked(tmp_path):
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    review = harness.store.create_draft(
        harness.router_id,
        "github",
        {"repo": "acme/app", "number": 7, "head_sha": "abc"},
        "Looks good.",
    )
    harness.store.execute("UPDATE drafts SET status='uncertain'")
    looking = asyncio.Event()
    answer = asyncio.Event()

    async def slow(target, text, since=""):
        looking.set()
        await answer.wait()
        return ""

    runtime.github.find_review = slow
    check = asyncio.create_task(runtime.drafts.check_uncertain())
    await looking.wait()
    await runtime.drafts.resolve(review["id"], DraftDecision(action="reject"))
    answer.set()
    await check
    assert harness.store.draft(review["id"])["status"] == "rejected", "dropped stays dropped"


async def test_the_owner_settles_an_interrupted_draft(tmp_path):
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    sent = harness.store.create_draft(
        harness.router_id, "slack", {"channel_id": "C1", "thread_ts": "", "to": "Ann"}, "Yes"
    )
    again = harness.store.create_draft(
        harness.router_id, "slack", {"channel_id": "C1", "thread_ts": "", "to": "Ann"}, "No"
    )
    harness.store.execute("UPDATE drafts SET status='uncertain'")
    marked = await runtime.drafts.resolve(sent["id"], DraftDecision(action="mark_sent"))
    assert marked["status"] == "sent"
    resent = await runtime.drafts.resolve(again["id"], DraftDecision(action="send"))
    assert resent["status"] == "sent"
    assert [
        call[1]["message"] for call in harness.mcp.calls if call[0] == "slack_send_message"
    ] == ["No"]


async def test_a_reply_cleared_on_its_card_is_refused_not_sent_as_drafted(tmp_path):
    """Bug bash: clearing a draft's text and pressing Send posted the original draft."""
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    targets = [
        ("slack", {"channel_id": "C0DEPLOYS", "thread_ts": ""}),
        ("reply", {"to": "Carol", "where": "email", "link": ""}),
        ("github", {"repo": "acme/app", "number": 7, "in_reply_to": 21}),
        ("github", {"repo": "acme/app", "number": 7, "reply_to_comment": 5}),
    ]
    for kind, target in targets:
        draft = harness.store.create_draft(harness.router_id, kind, target, "Approved")
        for cleared in ("", "   "):
            with pytest.raises(ValueError, match="The reply is empty"):
                await runtime.drafts.resolve(
                    draft["id"], DraftDecision(action="send", text=cleared)
                )
        assert harness.store.draft(draft["id"])["status"] == "pending"
    assert not [call for call in harness.mcp.calls if call[0] == "slack_send_message"]
    assert not [call for call in harness.gh_calls if call[1]], "nothing posted on GitHub"
    agreed = runtime.learning.agreement()["now"]["drafts"]
    assert agreed == [0, 0], "not sent as written: not sent at all"
    # A send that says nothing of the text (an answer from away) sends it as drafted.
    copied = harness.store.drafts()[1]
    assert (await runtime.drafts.resolve(copied["id"], DraftDecision(action="send")))[
        "final_text"
    ] == "Approved"


async def test_instructions_copilot_never_wrote_down_go_out_in_full_after_a_restart(
    tmp_path, monkeypatch
):
    """Bug bash 4 (E1): a restart right after a worker started: its send had returned, so its
    instructions counted as received, but Copilot never wrote them down; it got them quoted
    and cut at 1500 characters."""
    first = Harness(tmp_path / "data")
    runtime = await first.connect()
    instructions = "Learn how the owner works." + " Observation." * 300
    result = await call_tool(
        runtime, "spawn_worker", first.router_id, title="Learn", instructions=instructions
    )
    worker = json.loads(result.text_result_for_llm)["session_id"]
    await runtime.stop()

    async def nothing_written(self):
        return []

    monkeypatch.setattr(FakeSession, "get_events", nothing_written)
    second = Harness(tmp_path / "data", client=first.client)
    restarted = await second.connect()
    await settle(restarted)
    sent = second.session(worker).sent[-1]
    assert instructions in sent, "sent again in full"
    assert sent.index(instructions) < sent.index('type="restart"'), "as itself, not quoted"
    assert "before they reached you" in sent


async def test_the_newest_message_a_worker_received_is_quoted_whole_after_a_restart(tmp_path):
    first = Harness(tmp_path / "data")
    runtime = await first.connect()
    instructions = "Rename the columns." + " Column." * 300
    result = await call_tool(
        runtime, "spawn_worker", first.router_id, title="Rename", instructions=instructions
    )
    worker = json.loads(result.text_result_for_llm)["session_id"]
    await runtime.stop()

    second = Harness(tmp_path / "data", client=first.client)
    await settle(await second.connect())
    sent = second.session(worker).sent
    assert sent[-1].startswith('<message from="system" type="restart">')
    assert sent.count(sent[0]) == 1, "received: not sent again"
    assert instructions in sent[-1], "quoted whole, not cut"
