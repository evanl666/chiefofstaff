"""What happens when things go wrong at the worst moment.

Each test is one failure the runtime must survive without losing, duplicating or stranding
work. See also test_backend_contract.py (abort stops a running command, with real Copilot).
"""

import json

from conftest import Harness, call_tool, settle, task_of

from chiefofstaff.delivery import digest
from chiefofstaff.models import DraftDecision, Settings


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
    harness.store.set(f"sent:{session_id}", str(before or 0))
    harness.store.set(f"sending:{session_id}", json.dumps({"upto": last, "digest": digest(text)}))
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
    await runtime.cancel_task(running["task_id"], "Wrong branch")
    await settle(runtime)
    assert harness.session(running["session_id"]).aborted, "the turn and its command stop"
    assert harness.store.session(waiting["session_id"])["status"] == "working"
    assert task_of(harness, waiting["session_id"]) == waiting["task_id"]


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
    harness.mcp.results['"On it!" from:me'] = (
        "From you in #general: On it!\nhttps://acme.slack.com/archives/C1/p1712345678000200"
    )
    harness.gh_replies["api repos/acme/app/pulls/7/reviews"] = json.dumps(
        [{"body": "Something else", "html_url": "https://github.com/acme/app/pull/7#r1"}]
    )
    await runtime.check_uncertain_drafts()
    assert harness.store.draft(slack["id"])["status"] == "sent"
    assert harness.store.draft(review["id"])["status"] == "pending", "not published: approve again"
    assert harness.store.draft(lost["id"])["status"] == "uncertain", "Slack search may lag"
    assert "Send it again" in harness.store.draft(lost["id"])["outcome"]
    assert not [call for call in harness.mcp.calls if call[0] == "slack_send_message"]


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
    marked = await runtime.resolve_draft(sent["id"], DraftDecision(action="mark_sent"))
    assert marked["status"] == "sent"
    resent = await runtime.resolve_draft(again["id"], DraftDecision(action="send"))
    assert resent["status"] == "sent"
    assert [
        call[1]["message"] for call in harness.mcp.calls if call[0] == "slack_send_message"
    ] == ["No"]
