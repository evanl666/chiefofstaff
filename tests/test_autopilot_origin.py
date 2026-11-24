"""Autopilot's origin (autopilot/origin.py): a worker it continued keeps it until the owner
writes to it in the page; the Router carries it for a turn that takes in anything that carries
it, and passes it on to what that turn starts; a turn that carries it never publishes."""

import json

from conftest import Harness, call_tool, settle, slack_tool
from test_delegate_reply import ASK, DM, handed, reactions, reply, sends
from test_slack_source import search_answer, thread_answer

from chiefofstaff import ingress
from chiefofstaff.autopilot.origin import CARRIED, TAG
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


async def test_a_claude_process_it_opened_carries_nothing_once_the_owner_writes(
    tmp_path, claude_on_path, monkeypatch
):
    """Review 5: Autopilot's answer reaches a Claude worker whose process was closed: its new
    process (and the tasks that read it) opens with no origin of its own; once the owner
    writes to that worker in the page, nothing it does carries Autopilot's origin."""
    from test_claude_workers import PASSED, saved, spawn_on, until

    from chiefofstaff import backend_claude

    async def check(self, folder):
        return dict(PASSED)

    monkeypatch.setattr(backend_claude.ClaudeAgent, "check", check)
    monkeypatch.setattr(backend_claude, "LISTED", {})
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    await saved(runtime, {"claude": {"enabled": True}})
    made = await spawn_on(harness, runtime, title="Fix it", instructions="Report done: Fixed.",
                          agent="claude")  # fmt: skip
    worker = made["session_id"]
    await until(lambda: harness.store.session(worker)["status"] == "done", 30)
    claude = runtime.states.handle(worker).worker
    await runtime.agent_mode_changed(harness.store.session(worker), asks_first=False)
    await until(lambda: claude.task is None or claude.task.done(), 30)  # Its process ended.
    assert runtime.states.handle(worker) is None
    entry = await autopilot_entry(runtime)
    run = runtime.autopilot.run()
    runtime.autopilot.book.queue(run["id"], entry["id"], "worker", {
        "session_id": worker, "task_id": made["task_id"], "text": "Report done: Answered.",
        "display": "Autopilot answered.", "go_on": True,
    }, runtime.autopilot.book.generation())  # fmt: skip
    await runtime.autopilot.dispatch()
    router = harness.router_id

    def reports() -> list[dict]:
        return [m for m in harness.store.messages(router)
                if m["kind"] == "inbound" and m["meta"].get("from") == worker]  # fmt: skip

    await until(lambda: len(reports()) >= 2, 30)
    assert TAG in reports()[-1]["meta"], "the turn Autopilot continued carries it"
    await until(lambda: harness.store.session(worker)["status"] == "done", 30)
    acting = []
    add = runtime.add_message

    def stored(session_id, kind, text, meta=None, **how):
        if session_id != worker:  # What its turn causes elsewhere: the origin it acts with.
            acting.append(CARRIED.get())
        return add(session_id, kind, text, meta, **how)

    monkeypatch.setattr(runtime, "add_message", stored)
    with ingress.through(ingress.PAGE):
        await runtime.owner_send(worker, "Report done: Mine now.")
    await until(lambda: len(reports()) >= 3, 30)
    await until(lambda: harness.store.session(worker)["status"] == "done", 30)
    assert acting and all(origin is None for origin in acting), acting
    assert TAG not in reports()[-1]["meta"], "the owner's own turn carries nothing"
    assert runtime.autopilot.origins.of(worker) is None


async def claude_done(tmp_path, monkeypatch):
    """A Claude worker (the stand-in) that did its task, in a process opened acting on its own
    (the owner's setting for workers)."""
    from test_claude_workers import PASSED, saved, spawn_on, until

    from chiefofstaff import backend_claude

    async def check(self, folder):
        return dict(PASSED)

    monkeypatch.setattr(backend_claude.ClaudeAgent, "check", check)
    monkeypatch.setattr(backend_claude, "LISTED", {})
    harness = Harness(tmp_path / "data", Settings(worker_yolo=True, fork_busy=False))
    runtime = await harness.connect()
    await saved(runtime, {"claude": {"enabled": True}})
    made = await spawn_on(harness, runtime, title="Fix it", instructions="Report done: Fixed.",
                          agent="claude")  # fmt: skip
    await until(lambda: harness.store.session(made["session_id"])["status"] == "done", 30)
    assert runtime.states[made["session_id"]].opened_asking is False
    return harness, runtime, made


async def test_a_router_turn_it_led_to_reopens_a_claude_worker_asking_first(
    tmp_path, claude_on_path, monkeypatch
):
    """Review 3: workers ask first reaches every path that gives a worker Autopilot's origin,
    not only Autopilot's own deliveries: a Router turn that carries it messages a Claude worker
    opened acting on its own; that worker's process is opened again asking first before the
    message reaches it (never mid-turn), and the owner's setting stays as it was."""
    from test_claude_workers import until

    harness, runtime, made = await claude_done(tmp_path, monkeypatch)
    worker = made["session_id"]
    before = runtime.states.handle(worker).worker
    entry = await autopilot_entry(runtime)
    router = harness.store.session(harness.router_id)
    runtime.autopilot.origins.taken(router, [{"meta": {TAG: {"entries": [entry["id"]],
                                                             "ask_first": True}}}])  # fmt: skip
    told = await call_tool(runtime, "message_session", router["id"], session_id=worker,
                           text="Report done: Again.", task="new", title="Fix it again",
                           why_new="t")  # fmt: skip
    assert told.result_type != "failure", told.text_result_for_llm
    await until(lambda: len([t for t in runtime.tasks.for_session(worker)
                             if t["status"] in {"review", "done"}]) >= 2, 30)  # fmt: skip
    assert runtime.states[worker].opened_asking is True, "opened again asking first"
    assert runtime.states.handle(worker).worker is not before
    assert harness.store.settings().worker_yolo is True, "never the owner's setting"


async def test_a_claude_worker_at_work_takes_it_only_once_opened_again_asking_first(
    tmp_path, claude_on_path, monkeypatch
):
    """Review 3: the same, the worker at work in its own process: what carries Autopilot's
    origin waits for that turn to end (never into it), then reaches a process opened asking
    first."""
    from test_claude_workers import until

    harness, runtime, made = await claude_done(tmp_path, monkeypatch)
    worker = made["session_id"]
    router = harness.store.session(harness.router_id)
    await call_tool(runtime, "message_session", router["id"], session_id=worker,
                    text="Run the shell command `sleep 2` Report done: Slept.", task="new",
                    title="Sleep a while", why_new="t")  # fmt: skip
    await until(lambda: harness.store.session(worker)["status"] == "working", 30)
    first = runtime.states.handle(worker).worker
    entry = await autopilot_entry(runtime)
    runtime.autopilot.origins.taken(router, [{"meta": {TAG: {"entries": [entry["id"]],
                                                             "ask_first": True}}}])  # fmt: skip
    told = await call_tool(runtime, "message_session", router["id"], session_id=worker,
                           text="Report done: Again.", task="new", title="Fix it again",
                           why_new="t", earlier="keep")  # fmt: skip
    assert told.result_type != "failure", told.text_result_for_llm
    assert runtime.states[worker].reopen_asking, "held: its turn goes on as it began"
    await until(lambda: any(t["title"] == "Fix it again" and t["status"] in {"review", "done"}
                            for t in runtime.tasks.for_session(worker)), 30)  # fmt: skip
    assert runtime.states[worker].opened_asking is True
    assert runtime.states.handle(worker).worker is not first
    assert first.task is None or first.task.done(), "the process acting on its own ended"


async def test_a_turn_from_two_entries_writes_one_after_entry_its_receipts_once(harness):
    """Review 8: a turn several entries led to ends with one `after` entry listing them all (in
    one transaction), its receipts kept once, as a blob its hash names; each entry's record
    shows what came next."""
    from chiefofstaff.autopilot import view
    from chiefofstaff.autopilot.logbook import BLOB

    runtime = await harness.connect()
    router = harness.router_id
    worker, first = await continued(runtime, router)
    second = runtime.autopilot.book.append(first["run_id"], "decided", "Decided: Redis")
    runtime.autopilot.origins.keep(worker["session_id"], {"entries": [second["id"]],
                                                          "ask_first": True})  # fmt: skip
    session = worker["session_id"]
    runtime.autopilot.turn_ended(harness.store.session(session))
    [after] = runtime.autopilot.book.entries(kinds=("after",))
    assert after["payload"]["of"] == [first["id"], second["id"]]
    blob = after["evidence"]["receipts"]
    assert set(blob) == {BLOB, "size", "head"}
    assert runtime.autopilot.book.blob(blob[BLOB]) is not None
    shown = {a["id"]: a["then"] for a in view.actions(runtime.autopilot, first["run_id"])}
    assert shown[first["id"]] == shown[second["id"]] == [after["title"].removeprefix("then: ")]
    for source in (first, second):
        later = view.entry(runtime.autopilot, source["id"])["later"]
        assert [x["kind"] for x in later] == ["after"]
