"""QA round 6, from the owner's report: what they wrote a worker that stayed "Waiting to be
sent", a result and its reply as one decision, the task a direct message is filed under, an old
update shown as new, and one piece of work shown twice."""

from dataclasses import replace

from conftest import call_tool, settle
from test_progress import busy_worker, spawn


async def nothing():
    return []


async def echoing(harness):
    runtime = await harness.connect()
    harness.client.capabilities = replace(harness.client.capabilities, echoes=True)
    return runtime


def shown(runtime, session_id: str) -> list[dict]:
    """A session's messages as its chat shows them (the messages API)."""
    rows = runtime.store.messages(session_id)
    found = runtime.delivery.delivery_for(session_id, rows)
    return [{**row, "delivery": delivery} for row, delivery in zip(rows, found, strict=True)]


def home_copy(runtime, harness, worker: str, text: str) -> dict:
    return next(
        m
        for m in shown(runtime, harness.router_id)
        if m["kind"] == "owner" and m["meta"].get("to") == worker and m["text"] == text
    )


async def test_the_home_copy_of_a_message_to_a_worker_stands_where_the_worker_has_it(harness):
    """#1: the Router's copy of what the owner wrote a worker (Home shows it, "→ worker") goes
    to the Router only with its next message: it said "Waiting to be sent" until then, hours
    after the worker had it and answered."""
    runtime = await echoing(harness)
    worker = await spawn(runtime, harness.router_id, "Fix the login timeout")
    session = harness.session(worker)
    session.emit("session.idle")
    await settle(runtime)
    published = []
    publish = runtime.bus.publish
    runtime.bus.publish = lambda kind, data: (published.append((kind, data)), publish(kind, data))
    await runtime.owner_send(worker, "Also check the session cookie")
    await settle(runtime)
    copy = home_copy(runtime, harness, worker, "Also check the session cookie")
    assert copy["delivery"] == "sent", "the worker has it: not waiting for the Router's turn"
    told = [d.get("delivery") for _, d in published if d.get("id") == copy["id"]]
    assert told[-1] == "sent", "said as soon as Home has the copy"
    session.emit("user.message", content=session.raw_sent[-1])
    session.emit("assistant.turn_start")
    await settle(runtime)
    assert home_copy(runtime, harness, worker, "Also check the session cookie")["delivery"] == (
        "received"
    )
    session.emit("session.idle")
    await settle(runtime)
    assert home_copy(runtime, harness, worker, "Also check the session cookie")["delivery"] == (
        "processed"
    )
    # Home hears of it as it changes, not only when the Router's next turn starts.
    told = [d.get("delivery") for _, d in published if d.get("id") == copy["id"]]
    assert told[-1] == "processed", told
    # The Router's own turn later does not set it back.
    await runtime.delivery.deliver(harness.router_id, "news", kind="inbound", display="news")
    await settle(runtime)
    assert home_copy(runtime, harness, worker, "Also check the session cookie")["delivery"] == (
        "processed"
    )
    told = [d.get("delivery") for _, d in published if d.get("id") == copy["id"]]
    assert told[-1] == "processed", told


async def test_a_message_held_by_a_stop_says_so_and_goes_when_asked(harness):
    """#1: sent while it worked, then Stop: what it never saw waits for its next message. The
    chat said "Waiting to be sent" for ever; it says it is held, and Send now sends it."""
    runtime = await echoing(harness)
    worker, session, _ = await busy_worker(runtime, harness, "Fix the login timeout")
    session.get_events = nothing
    await runtime.owner_send(worker, "Use the staging config")
    sent = session.raw_sent[-1]
    published = []
    publish = runtime.bus.publish
    runtime.bus.publish = lambda kind, data: (published.append((kind, data)), publish(kind, data))
    await runtime.workers.interrupt(worker)
    await settle(runtime)
    copy = home_copy(runtime, harness, worker, "Use the staging config")
    mine = next(m for m in shown(runtime, worker) if m["text"] == "Use the staging config")
    assert mine["delivery"] == copy["delivery"] == "held"
    # Said as it changes, in its chat and on Home (not only once the chat is opened again).
    for message in (mine, copy):
        told = [d.get("delivery") for _, d in published if d.get("id") == message["id"]]
        assert told and told[-1] == "held", told
    await runtime.send_now(worker)
    await settle(runtime)
    assert session.raw_sent[-1].startswith(sent), "the held message goes now"
    assert session.modes[-1] == "immediate", "after a stop, steered in at once"
    mine = next(m for m in shown(runtime, worker) if m["text"] == "Use the staging config")
    assert mine["delivery"] == "sent"


async def test_a_message_waiting_for_a_slot_goes_now_when_asked(harness):
    """#1: every worker slot taken, a message to an idle worker waits for one; Send now takes
    one over the limit, once (not while memory is short: said, and it waits on)."""
    harness.store.save_settings(harness.store.settings().model_copy(update={"max_workers": 1}))
    runtime = await echoing(harness)
    other = await spawn(runtime, harness.router_id, "Update the docs")
    await call_tool(runtime, "report", other, status="done", summary="Updated the docs.")
    harness.session(other).emit("session.idle")
    await settle(runtime)
    await busy_worker(runtime, harness, "Fix the login timeout")
    before = len(harness.session(other).raw_sent)
    await runtime.owner_send(other, "Add the FAQ too")
    await settle(runtime)
    assert harness.store.session(other)["status"] == "queued"
    assert next(m for m in shown(runtime, other) if m["text"] == "Add the FAQ too")["delivery"] == (
        "queued"
    )
    view = await runtime.send_now(other)
    await settle(runtime)
    assert view["status"] in {"starting", "working"}
    assert len(harness.session(other).raw_sent) == before + 1
    assert next(m for m in shown(runtime, other) if m["text"] == "Add the FAQ too")["delivery"] == (
        "sent"
    )
    assert not runtime.states[other].now, "once"


async def reported_with_reply(runtime, harness, title="Answer Ann about the release date"):
    """A worker's result to accept, and the reply it drafted for the owner to send themself."""
    worker = await spawn(runtime, harness.router_id, title)
    task = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="done", summary="Ann gets Friday.")
    await settle(runtime)
    draft = harness.store.create_draft(
        worker, "reply", {"to": "Ann", "where": "email", "task_id": task["id"]}, "Friday works."
    )
    return worker, runtime.tasks.get(task["id"]), draft


def draft_notes(harness) -> list[str]:
    """What the Router hears of the owner's drafts (with its next message)."""
    return [m["text"] for m in harness.store.messages(harness.router_id)
            if m["meta"].get("draft_id")]  # fmt: skip


async def test_copying_a_reply_from_its_results_card_accepts_that_result(harness):
    """#2: a card holding a result (Awaiting acceptance, Accept) and a reply drafted for it
    (Copy & mark sent) was two decisions: one click left the other half of the card behind
    (another reply of it still waiting kept the result unaccepted, say). Sent from that card,
    the reply settles the result too; the Router hears of it once."""
    from chiefofstaff.models import DraftDecision

    runtime = await harness.connect()
    worker, task, draft = await reported_with_reply(runtime, harness)
    other = harness.store.create_draft(
        worker, "reply", {"to": "Bo", "where": "email", "task_id": task["id"]}, "Friday."
    )
    assert task["status"] == "review"
    await runtime.drafts.resolve(
        draft["id"], DraftDecision(action="send", text="Friday works.", accept=task["id"])
    )
    await settle(runtime)
    assert harness.store.draft(draft["id"])["status"] == "sent"
    assert runtime.tasks.get(task["id"])["status"] == "done", "one decision: accepted with it"
    assert harness.store.draft(other["id"])["status"] == "pending", "its own card, still to send"
    assert len(draft_notes(harness)) == 1, draft_notes(harness)
    # Accepted first (elsewhere), its reply still goes: nothing half-done, nothing refused.
    await runtime.drafts.resolve(
        other["id"], DraftDecision(action="send", text="Friday.", accept=task["id"])
    )
    assert harness.store.draft(other["id"])["status"] == "sent"
    assert runtime.tasks.get(task["id"])["status"] == "done"
    assert len(draft_notes(harness)) == 2, "one note per reply, none for the accept"


async def test_a_reply_sent_from_a_card_with_news_on_its_result_settles_the_news(harness):
    """#2: news on an accepted result shows on the card of the reply drafted for it (Got it);
    the reply sent from there has seen it."""
    from chiefofstaff.models import DraftDecision

    runtime = await harness.connect()
    _, task, draft = await reported_with_reply(runtime, harness)
    runtime.task_actions.accept(task["id"])
    runtime.task_actions.news(task["id"], "Ann asked for Thursday instead.")
    await runtime.drafts.resolve(
        draft["id"], DraftDecision(action="send", text="Thursday works.", accept=task["id"])
    )
    assert not harness.store.get(f"news:{task['id']}")
    assert runtime.tasks.get(task["id"])["status"] == "done"
