"""Autopilot's origin (autopilot/origin.py): a worker it continued keeps it until the owner
writes to it in the page; the Router carries it for a turn that takes in anything that carries
it, and passes it on to what that turn starts; a turn that carries it never publishes."""

import json

from conftest import Harness, call_tool, settle, slack_tool
from test_delegate_reply import ASK, DM, handed, reactions, reply, sends
from test_slack_source import search_answer, thread_answer

from chiefofstaff import ingress
from chiefofstaff.autopilot.origin import TAG
from chiefofstaff.models import Settings


async def autopilot_entry(runtime, title="Answered: Postgres"):
    """An action of a run (an answer it gave), as the engine records one."""
    old = runtime.store.settings()
    new = old.model_copy(update={"autopilot": True})
    runtime.store.save_settings(new)
    await runtime.settings_changed(old, new)
    state = await runtime.autopilot.start(None, by="home")
    return runtime.autopilot.book.append(state["run"], "answered", title)


async def spawned(runtime, router, title, **extra):
    result = await call_tool(
        runtime, "spawn_worker", router, title=title, instructions=title, why_new="t", **extra
    )
    return json.loads(result.text_result_for_llm)


async def continued(runtime, router, title="Write the client"):
    """A worker whose turn Autopilot continued (its answer delivered to it)."""
    worker = await spawned(runtime, router, title)
    entry = await autopilot_entry(runtime)
    runtime.autopilot.origins.keep(worker["session_id"], {"entries": [entry["id"]],
                                                          "ask_first": True})  # fmt: skip
    return worker, entry


def router_turn(runtime, router):
    return runtime.autopilot.origins.turns.get(router)


async def end_turn(harness, session_id):
    harness.session(session_id).emit("session.idle")
    await settle(harness.runtime)


async def test_an_autopilot_report_then_the_owners_reaction_in_another_turn_sends_as_today(
    tmp_path,
):
    harness, runtime, _ = await handed(tmp_path, spawn=False)
    router = harness.router_id
    await end_turn(harness, router)  # The delegated message's turn: the owner's own.
    assert router_turn(runtime, router) is None
    worker, entry = await continued(runtime, router)
    await call_tool(runtime, "report", worker["session_id"], status="done", summary="Client done.")
    await settle(runtime)
    assert router_turn(runtime, router)["entries"] == [entry["id"]], "it took in the report"
    await end_turn(harness, router)
    assert router_turn(runtime, router) is None, "per turn, never kept"
    [after] = runtime.autopilot.book.entries(kinds=("after",))
    assert after["of"] == entry["id"] and after["subject"] == f"session:{router}"
    # The owner's reaction comes in a turn of its own: the reply goes out at once, as today.
    result = await call_tool(runtime, "spawn_worker", router, title="Check the deploy",
                             instructions="Check the deploy for Alice", why_new="test", slack=True,
                             follow=[f"slack:{DM}:{ASK[2]}"])  # fmt: skip
    slack_worker = json.loads(result.text_result_for_llm)["session_id"]
    assert runtime.autopilot.origins.of(slack_worker) is None
    said = await reply(runtime, slack_worker, "Hi Alice, the deploy is green again.")
    assert "sent at once in the thread" in said and len(sends(harness)) == 1


async def test_both_in_one_router_turn_carry_the_origin_and_the_reply_waits_as_a_card(tmp_path):
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    router = harness.router_id
    harness.mcp.tools.append(slack_tool("slack_get_reactions"))
    await runtime.slack.discover()
    query = "hasmy::robot_face:"
    harness.mcp.results[query] = search_answer(query)
    await runtime.slack.poll_delegated("robot_face")  # From now on.
    worker, entry = await continued(runtime, router)
    sent = harness.session(router).sent
    before = len(sent)
    # The Router is at work: the report and the owner's reaction wait for its next turn.
    harness.store.update_session(router, status="working")
    await call_tool(runtime, "report", worker["session_id"], status="done", summary="Client done.")
    harness.mcp.results[query] = search_answer(query, (DM, "DM", *ASK, ""))
    await runtime.slack.poll_delegated("robot_face")
    harness.mcp.reactions[ASK[2]] = reactions("U0OWNER1")
    harness.mcp.reads[DM] = thread_answer(ASK)
    await settle(runtime)
    assert len(sent) == before, "held for the Router's next turn"
    harness.store.update_session(router, status="idle")
    runtime.delivery.schedule_flush(router)
    await settle(runtime)
    turn = "\n".join(sent[before:])
    assert "Client done." in turn and "robot_face" in turn, "one turn, both inputs"
    assert router_turn(runtime, router)["entries"] == [entry["id"]]
    result = await call_tool(runtime, "spawn_worker", router, title="Check the deploy",
                             instructions="Check the deploy for Alice", why_new="test", slack=True,
                             follow=[f"slack:{DM}:{ASK[2]}"])  # fmt: skip
    slack_worker = json.loads(result.text_result_for_llm)["session_id"]
    assert runtime.autopilot.origins.of(slack_worker)["entries"] == [entry["id"]]
    await reply(runtime, slack_worker, "Hi Alice, the deploy is green again.")
    assert sends(harness) == [], "never through the owner's delegation"
    [card] = harness.store.drafts("pending")
    assert card["outcome"] == "Not sent at once: Autopilot continued this work."


async def test_a_task_the_router_spawns_from_such_a_turn_carries_it_on(harness):
    runtime = await harness.connect()
    router = harness.router_id
    other = await spawned(runtime, router, "API")
    worker, entry = await continued(runtime, router)
    await call_tool(runtime, "report", worker["session_id"], status="done", summary="Client done.")
    await settle(runtime)
    result = await call_tool(runtime, "spawn_worker", router, title="Write the docs",
                             instructions="Docs for the client", why_new="test")  # fmt: skip
    docs = json.loads(result.text_result_for_llm)["session_id"]
    carried = runtime.autopilot.origins.of(docs)
    assert carried["entries"] == [entry["id"]] and carried["ask_first"] is True
    [first] = [m for m in harness.store.messages(docs) if m["kind"] == "router"]
    assert first["meta"][TAG]["entries"] == [entry["id"]]
    # So does what the Router tells an existing worker in that turn.
    assert runtime.autopilot.origins.of(other["session_id"]) is None
    await call_tool(runtime, "message_session", router, session_id=other["session_id"],
                    text="The client is done: use its /v2 calls.")  # fmt: skip
    await settle(runtime)
    assert runtime.autopilot.origins.of(other["session_id"])["entries"] == [entry["id"]]
    await end_turn(harness, router)
    # A turn of the owner's own after it: nothing it starts carries it.
    await runtime.owner_send(router, "Start the release notes")
    result = await call_tool(runtime, "spawn_worker", router, title="Release notes",
                             instructions="Notes", why_new="test")  # fmt: skip
    notes = json.loads(result.text_result_for_llm)["session_id"]
    assert runtime.autopilot.origins.of(notes) is None


async def test_a_worker_keeps_it_across_its_own_continuations_until_the_owner_writes(harness):
    runtime = await harness.connect()
    router = harness.router_id
    worker, entry = await continued(runtime, router)
    session = worker["session_id"]
    row = harness.store.session(session)
    assert runtime.yolo(row) is False, "workers ask first while it carries it"
    await end_turn(harness, session)
    assert runtime.autopilot.origins.of(session)["entries"] == [entry["id"]], "kept"
    # A note it sends another worker is held for that one's next turn, never steered.
    other = await spawned(runtime, router, "API")
    told = await call_tool(runtime, "tell_session", session, session_id=other["session_id"],
                           text="The client needs /v2/users.")  # fmt: skip
    assert "reads this with its next message" in told.text_result_for_llm
    [note] = [m for m in harness.store.messages(other["session_id"]) if m["kind"] == "inbound"]
    assert note["meta"]["deferred"] and note["meta"][TAG]["entries"] == [entry["id"]]
    # The owner writes to it in the page: theirs again.
    with ingress.through(ingress.PAGE):
        await runtime.owner_send(session, "Thanks, use the v2 API.")
    assert runtime.autopilot.origins.of(session) is None and runtime.yolo(row) is True
    handed_back = runtime.autopilot.book.entries(kinds=("handed-back",))
    assert [e["of"] for e in handed_back] == [entry["id"]]
    # An away answer is not the owner's handoff.
    runtime.autopilot.origins.keep(session, {"entries": [entry["id"]], "ask_first": True})
    with ingress.through(ingress.AWAY):
        await runtime.owner_send(session, "ok")
    assert runtime.autopilot.origins.of(session) is not None


async def test_nothing_is_tagged_or_kept_when_autopilot_never_acted(harness):
    runtime = await harness.connect()
    router = harness.router_id
    session = (await spawned(runtime, router, "A"))["session_id"]
    await call_tool(runtime, "report", session, status="done", summary="Done.")
    await settle(runtime)
    assert not runtime.autopilot.origins.any() and not harness.store.keys("autopilot:")
    assert not any(TAG in m["meta"] for s in (router, session) for m in harness.store.messages(s))
