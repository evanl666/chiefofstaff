"""Bug E14: a message sent to a worker was taken for delivered though its conversation never
showed it (sent into a turn that was over), and nothing said so. Each message now goes queued,
sent, received (its conversation shows it), processed (that turn ended); one neither shown nor
waiting in Copilot's queue is sent again, and the owner hears when it never gets through."""

import asyncio
from dataclasses import replace

import pytest
from conftest import settle
from test_progress import busy_worker, spawn

from chiefofstaff import delivery


@pytest.fixture(autouse=True)
def promptly(monkeypatch):
    monkeypatch.setattr(delivery, "ECHO_CHECK", 0.01)


def states(runtime, harness, worker) -> dict[str, str]:
    """Where the owner's messages to it stand, as the chat API says."""
    marks = runtime.delivery.delivery_marks(worker)
    return {
        m["text"]: runtime.delivery.delivery_of(m, marks)
        for m in harness.store.messages(worker)
        if m["kind"] == "owner"
    }


async def echoing(harness):
    runtime = await harness.connect()
    harness.client.capabilities = replace(harness.client.capabilities, echoes=True)
    return runtime


async def nothing():
    return []


async def wait(runtime, seconds=0.06):
    await asyncio.sleep(seconds)
    await settle(runtime)


async def test_a_message_goes_sent_received_processed(harness):
    runtime = await echoing(harness)
    worker = await spawn(runtime, harness.router_id, "Lint")
    session = harness.session(worker)
    session.emit("session.idle")
    await settle(runtime)
    published = []
    runtime.bus.publish = lambda kind, data: published.append((kind, data))
    await runtime.owner_send(worker, "Go on")
    assert states(runtime, harness, worker)["Go on"] == "sent"
    assert ("sent", True) in [(d.get("delivery"), d.get("amended")) for _, d in published]
    session.emit("user.message", content=session.raw_sent[-1])
    session.emit("assistant.turn_start")
    await settle(runtime)
    assert states(runtime, harness, worker)["Go on"] == "received", "Go on got through"
    session.emit("session.idle")
    await settle(runtime)
    assert states(runtime, harness, worker)["Go on"] == "processed"
    shown = [d.get("delivery") for _, d in published if d.get("text") == "Go on"]
    assert shown[-1] == "processed", shown


async def test_a_message_its_conversation_never_shows_is_sent_again_once_shown_no_more(
    harness, monkeypatch
):
    monkeypatch.setattr(delivery, "ECHO_CHECK", 0.05)
    runtime = await echoing(harness)
    worker, session, _ = await busy_worker(runtime, harness, "Lint")
    session.get_events = nothing
    await runtime.owner_send(worker, "Also fix the README")
    sent = session.raw_sent[-1]
    assert session.modes[-1] == "immediate"
    for _ in range(100):
        await asyncio.sleep(0.005)
        if session.raw_sent.count(sent) > 1:
            break
    assert session.raw_sent.count(sent) == 2, "neither shown nor queued: lost, sent again"
    assert session.modes[-1] == "immediate", "into the turn it is in"
    assert states(runtime, harness, worker)["Also fix the README"] == "resending"
    session.emit("user.message", content=sent)
    await wait(runtime, 0.1)
    assert session.raw_sent.count(sent) == 2, "shown now: not again"
    assert states(runtime, harness, worker)["Also fix the README"] == "received"


async def test_a_message_waiting_in_copilots_queue_or_shown_is_not_sent_again(harness):
    runtime = await echoing(harness)
    worker, session, _ = await busy_worker(runtime, harness, "Lint")
    await runtime.owner_send(worker, "Also fix the README")
    sent = session.raw_sent[-1]
    # Its echo was missed, but its conversation has it: received, not sent twice.
    await wait(runtime)
    assert session.raw_sent.count(sent) == 1
    assert states(runtime, harness, worker)["Also fix the README"] == "received"

    session.get_events = nothing
    await runtime.owner_send(worker, "And the CHANGELOG")
    session.queued = {"1": session.raw_sent[-1]}  # Behind its turn: it comes.
    await wait(runtime)
    assert session.raw_sent.count(session.raw_sent[-1]) == 1
    assert states(runtime, harness, worker)["And the CHANGELOG"] == "sent"


async def test_a_message_that_never_gets_through_is_said_so(harness):
    runtime = await echoing(harness)
    worker, session, _ = await busy_worker(runtime, harness, "Lint")
    session.get_events = nothing
    await runtime.owner_send(worker, "Stop pushing")
    sent = session.raw_sent[-1]
    for _ in range(10):
        await wait(runtime, 0.1)
        if states(runtime, harness, worker)["Stop pushing"] == "undelivered":
            break
    assert session.raw_sent.count(sent) == 1 + delivery.ECHO_RESENDS
    assert states(runtime, harness, worker)["Stop pushing"] == "undelivered"
    kinds = harness.kinds(worker)
    assert kinds[-1][0] == "error" and "did not reach it" in kinds[-1][1]


async def test_a_stop_takes_over_what_was_never_shown(harness):
    """Stopped, what it never saw goes with its next message (one send, not echo resends)."""
    runtime = await echoing(harness)
    worker, session, _ = await busy_worker(runtime, harness, "Lint")
    session.get_events = nothing
    await runtime.owner_send(worker, "Also fix the README")
    sent = session.raw_sent[-1]
    await runtime.workers.interrupt(worker)
    await wait(runtime)
    assert session.raw_sent.count(sent) == 1
    assert states(runtime, harness, worker)["Also fix the README"] == "queued"


async def test_a_cancelled_tasks_message_is_not_sent_again_after_the_cancel(harness):
    """Its aborted turn dropped what it had not taken up: sent again, it would start the
    work that was called off."""
    runtime = await echoing(harness)
    worker, session, task = await busy_worker(runtime, harness, "Deploy")
    session.get_events = nothing
    await runtime.owner_send(worker, "Also deploy it")
    sent = session.raw_sent[-1]
    await runtime.task_actions.cancel(task["id"], "not now")
    await wait(runtime)
    assert session.raw_sent.count(sent) == 1


async def test_a_message_sent_again_carries_its_image(harness, monkeypatch):
    import base64

    from chiefofstaff.models import Attachment

    monkeypatch.setattr(delivery, "ECHO_CHECK", 0.05)
    runtime = await echoing(harness)
    worker, session, _ = await busy_worker(runtime, harness, "Fix the layout")
    session.get_events = nothing
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()
    image = Attachment(name="shot.png", type="image/png", data=png)
    await runtime.owner_send(worker, "Look at this", attachments=[image])
    sent = session.raw_sent[-1]
    for _ in range(100):
        await asyncio.sleep(0.005)
        if session.raw_sent.count(sent) > 1:
            break
    assert session.raw_sent.count(sent) == 2
    assert session.attachments[-1] and session.attachments[-1] == session.attachments[-2]
