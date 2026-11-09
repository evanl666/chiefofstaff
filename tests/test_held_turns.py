"""Bug E13: a worker that left a command running attached in the background (a dev server)
ended its turn, but Copilot defers session.idle until that command ends. Chief of Staff took it
to be working for ever ("Thinking…"), and what was sent to it then was lost."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import call_tool, settle
from test_progress import busy_worker, spawn

from chiefofstaff import delivery, events


async def docs(runtime, harness, worker):
    """The Router gives it a second task."""
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="new",
                    why_new="test", earlier="keep", title="Docs",
                    text="Then write the docs.")  # fmt: skip


@pytest.fixture(autouse=True)
def promptly(monkeypatch):
    monkeypatch.setattr(events, "HELD_AFTER_IDLE", 0)
    monkeypatch.setattr(events, "HELD_AFTER_TURN", 0)


async def held_worker(runtime, harness, title):
    """A worker whose turn ended (it asked a question) with a dev server left running."""
    worker, session, task = await busy_worker(runtime, harness, title)
    session.background = {"devsrv9": "running"}
    await call_tool(runtime, "report", worker, status="question", summary="Push it?")
    # Copilot's loop is idle (after its last step's end, see the next tests); no session.idle:
    # the server holds it.
    session.emit("assistant.idle")
    await settle(runtime)
    return worker, session, task


async def test_a_turn_held_by_a_command_left_running_ends_and_the_owner_reaches_it(
    harness, monkeypatch
):
    runtime = await harness.connect()
    worker, session, _ = await held_worker(runtime, harness, "Preview")
    state = runtime.states[worker]
    assert harness.store.session(worker)["status"] != "working", "not Thinking… for ever"
    assert not state.busy and state.held
    assert harness.store.get(f"idle:{worker}"), "its turn's end was taken"
    assert runtime.view(harness.store.session(worker))["background"]

    await runtime.owner_send(worker, "lgtm, push")
    assert "lgtm, push" in session.sent[-1]
    assert session.modes[-1] == "immediate", "queued, it would wait for the server to end"
    await docs(runtime, harness, worker)
    assert session.modes[-1] == "immediate", "the Router's too"
    session.emit("user.message", content=session.sent[-1])
    session.emit("assistant.turn_start")
    await settle(runtime)
    assert not state.held and state.busy, "at work again"
    assert harness.store.session(worker)["status"] == "working"


async def test_the_session_idle_held_back_is_no_second_end_of_its_turn(harness, monkeypatch):
    runtime = await harness.connect()
    worker, session, _ = await held_worker(runtime, harness, "Preview")
    marked = harness.store.get(f"idle:{worker}")
    runtime.add_message(worker, "notice", "later")
    session.background = {"devsrv9": "completed"}  # The server ends.
    session.emit("session.idle")
    await settle(runtime)
    assert harness.store.get(f"idle:{worker}") == marked
    assert not runtime.states[worker].held


async def test_a_new_turn_or_a_session_idle_is_no_held_turn(harness, monkeypatch):
    runtime = await harness.connect()
    worker, session, _ = await busy_worker(runtime, harness, "Build")
    session.background = {"0": "running"}
    # One step ends and the next starts at once: its turn goes on.
    session.emit("assistant.turn_end")
    session.emit("assistant.turn_start")
    await settle(runtime)
    assert runtime.states[worker].busy and not runtime.states[worker].held
    assert harness.store.session(worker)["status"] == "working"


async def test_what_copilot_queued_behind_a_command_left_running_is_sent_again(
    harness, monkeypatch
):
    """Sent queued while it worked, a message waits in Copilot's queue for the command it left
    running, and Copilot says no assistant.idle then: its last step's end is the turn's end."""
    runtime = await harness.connect()
    harness.client.capabilities = replace(harness.client.capabilities, echoes=True)
    worker, session, _ = await busy_worker(runtime, harness, "Preview")
    await docs(runtime, harness, worker)
    assert session.modes[-1] == "enqueue", "a separate task waits for the turn to end"
    session.queued = {"1": session.raw_sent[-1]}
    session.background = {"devsrv9": "running"}
    session.emit("assistant.turn_end")
    await settle(runtime)
    assert session.queued == {}, "taken back from Copilot's queue"
    assert sum("Then write the docs." in sent for sent in session.sent) == 2
    assert session.modes[-1] == "immediate"
    assert session.background == {"devsrv9": "running"}, "its server goes on"
    # Its turn goes on with it, as it would have but for the server: no end of a turn, so
    # nothing it said is filed as the Docs task's report, and it is not asked for one.
    assert harness.store.session(worker)["status"] == "working"
    assert runtime.states[worker].busy and runtime.states[worker].held
    given = runtime.tasks.current(worker)
    assert given["title"] == "Docs" and given["status"] == "running" and not given["result"]
    assert not [m for m in harness.store.messages(worker) if m["meta"].get("report_nudge")]


async def test_a_copilot_that_cannot_say_what_it_queued_has_the_command_stopped(
    harness, monkeypatch
):
    runtime = await harness.connect()
    harness.client.capabilities = replace(harness.client.capabilities, echoes=True)
    worker, session, _ = await busy_worker(runtime, harness, "Preview")
    del session.rpc.queue
    await docs(runtime, harness, worker)
    session.background = {"devsrv9": "running"}
    session.emit("assistant.turn_end")
    await settle(runtime)
    assert session.background == {"devsrv9": "cancelled"}, "Copilot then starts on its queue"
    assert ("notice", delivery.HELD_STOPPED) in harness.kinds(worker)


async def test_a_message_held_by_a_command_left_running_stops_it_instead_of_failing(
    harness, monkeypatch
):
    """A message that never started a turn while a command runs in the background (a session
    resumed so, say): no "check its MCP settings" failure; the command is stopped."""
    monkeypatch.setattr(delivery, "START_CHECK", 3600)
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Preview")
    session = harness.session(worker)

    async def nothing():
        return []

    session.get_events = nothing
    session.background = {"devsrv9": "running"}
    runtime.states[worker].awaiting = ("sent", 0)
    await runtime.delivery.check_started(worker, session, "sent")
    assert session.background == {"devsrv9": "cancelled"}
    assert harness.store.session(worker)["status"] != "failed"
    assert runtime.states[worker].awaiting == ("sent", 0), "checked on again later"


async def test_cancelling_the_task_of_a_held_worker_stops_its_command(harness, monkeypatch):
    """Else the command ends later (killed for its port) and wakes it on a cancelled task."""
    runtime = await harness.connect()
    worker, session, _ = await held_worker(runtime, harness, "Preview")
    await docs(runtime, harness, worker)
    session.emit("user.message", content=session.sent[-1])
    session.emit("assistant.turn_start")
    await settle(runtime)
    await call_tool(runtime, "report", worker, status="question", summary="Which docs?",
                    task_id=runtime.tasks.current(worker)["id"])  # fmt: skip
    session.emit("assistant.turn_end")
    session.emit("assistant.idle")
    await settle(runtime)
    assert runtime.states[worker].held
    assert harness.store.session(worker)["status"] != "working"
    await runtime.task_actions.cancel(runtime.tasks.current(worker)["id"], "not now")
    assert session.background == {"devsrv9": "cancelled"}


async def test_closing_a_held_worker_stops_its_command(harness):
    runtime = await harness.connect()
    worker, session, _ = await held_worker(runtime, harness, "Preview")
    await runtime.workers.close(worker)
    assert session.background == {"devsrv9": "cancelled"}


async def test_a_new_copilot_connection_holds_nothing(harness):
    """Its commands ended with the Copilot CLI it was connected to: messages queue again."""
    runtime = await harness.connect()
    worker, _, _ = await held_worker(runtime, harness, "Preview")
    await runtime.reconnect()
    assert not runtime.states[worker].held


async def test_a_send_under_way_as_its_turn_ends_is_taken_back_too(harness):
    """What was being sent as its turn ended reaches Copilot's queue after: taken back all
    the same, not left there behind the server."""
    import asyncio

    runtime = await harness.connect()
    harness.client.capabilities = replace(harness.client.capabilities, echoes=True)
    worker, session, _ = await busy_worker(runtime, harness, "Preview")
    gate = asyncio.Event()
    sent = session.send

    async def slow(prompt, mode=None, attachments=None):
        if mode == "enqueue":
            await gate.wait()
            session.queued["7"] = prompt
        return await sent(prompt, mode, attachments)

    session.send = slow
    session.background = {"devsrv9": "running"}
    sending = asyncio.ensure_future(docs(runtime, harness, worker))
    await settle(runtime)
    session.emit("assistant.turn_end")
    await asyncio.sleep(0.05)
    gate.set()
    await sending
    await settle(runtime)
    assert session.queued == {}
    assert session.modes[-2:] == ["enqueue", "immediate"]
    assert runtime.states[worker].busy and runtime.states[worker].held


async def test_a_sub_agents_last_step_is_not_the_end_of_its_turn(harness):
    runtime = await harness.connect()
    worker, session, _ = await busy_worker(runtime, harness, "Explore")
    session.background = {"agent1": "running"}
    session.emit("assistant.turn_end", parent_tool_call_id="sub1")
    session.emit("assistant.idle", parent_tool_call_id="sub1")
    await settle(runtime)
    assert runtime.states[worker].busy and not runtime.states[worker].held
    assert harness.store.session(worker)["status"] == "working"


async def test_only_what_holds_the_session_counts_as_running_in_the_background():
    from chiefofstaff.backend import running_background

    def task(status, execution_mode=None, attachment_mode=None):
        return SimpleNamespace(status=status, execution_mode=execution_mode,
                               attachment_mode=attachment_mode)  # fmt: skip

    async def listed():
        return SimpleNamespace(tasks=[
            task("running", "sync", "attached"),  # A call its turn waits for.
            task("running", "background", "detached"),  # Started detached: holds nothing.
            task("completed", "background", "attached"),
            task("running", "background", "attached"),  # A dev server left attached.
            task("running"),  # A sub-agent (a Copilot that says no more).
        ])  # fmt: skip

    session = SimpleNamespace(rpc=SimpleNamespace(tasks=SimpleNamespace(list=listed)))
    assert await running_background(session) == 2


async def woken(runtime, session):
    """Copilot says a command it left running ended, and it starts a turn on its own."""
    session.aborted = False
    ended = SimpleNamespace(type="shell_completed", shell_id="s1", exit_code=0)
    event = SimpleNamespace(
        type=SimpleNamespace(value="system.notification"), data=SimpleNamespace(kind=ended)
    )
    for handler in session.handlers:
        handler(event)
    session.emit("assistant.turn_start")
    await settle(runtime)


async def test_a_stopped_worker_does_not_go_on_by_itself_when_its_command_ends(harness):
    runtime = await harness.connect()
    worker, session, _ = await held_worker(runtime, harness, "Preview")
    task = runtime.tasks.current(worker)
    await runtime.task_actions.follow_up(task["id"], "Yes, push it.")
    session.emit("user.message", content=session.sent[-1])
    session.emit("assistant.turn_start")
    await settle(runtime)
    await runtime.workers.interrupt(worker)
    assert runtime.tasks.get(task["id"])["status"] == "waiting", "stopped"
    session.emit("session.idle")
    await settle(runtime)
    await woken(runtime, session)
    assert session.aborted, "nothing to go on with: stopped again"
    assert harness.kinds(worker)[-1][1].startswith("It started again on its own")
    session.emit("session.idle")
    await settle(runtime)
    assert not [m for m in harness.store.messages(worker) if m["meta"].get("report_nudge")]
    assert harness.store.session(worker)["status"] != "working"


async def test_a_cancelled_worker_does_not_go_on_by_itself(harness):
    runtime = await harness.connect()
    _, session, task = await busy_worker(runtime, harness, "Preview")
    await runtime.task_actions.cancel(task["id"], "not now")
    session.emit("session.idle")
    await settle(runtime)
    await woken(runtime, session)
    assert session.aborted


async def test_a_worker_at_work_woken_by_its_command_goes_on(harness):
    """Its task is open: a command it waited on ended, and it goes on with it."""
    runtime = await harness.connect()
    worker, session, _ = await busy_worker(runtime, harness, "Build")
    session.emit("session.idle")
    await settle(runtime)
    await woken(runtime, session)
    assert not session.aborted
    assert harness.store.session(worker)["status"] == "working"


async def test_stopping_a_held_worker_says_so_and_stops_its_command(harness):
    runtime = await harness.connect()
    worker, session, _ = await held_worker(runtime, harness, "Preview")
    assert harness.store.session(worker)["status"] != "working"
    await runtime.workers.interrupt(worker)
    assert session.background == {"devsrv9": "cancelled"}
    assert ("notice", "Stopped by you.") in harness.kinds(worker)


async def test_a_turn_the_owner_starts_in_a_terminal_after_a_stop_goes_on(harness):
    """A message started it (typed in their terminal): not a turn it started on its own."""
    runtime = await harness.connect()
    _, session, task = await busy_worker(runtime, harness, "Preview")
    await runtime.task_actions.cancel(task["id"], "not now")
    session.emit("session.idle")
    await settle(runtime)
    ended = SimpleNamespace(type="shell_completed", shell_id="s1", exit_code=0)
    event = SimpleNamespace(
        type=SimpleNamespace(value="system.notification"), data=SimpleNamespace(kind=ended)
    )
    for handler in session.handlers:
        handler(event)
    session.aborted = False
    session.emit("user.message", content="Try the other port.")
    session.emit("assistant.turn_start")
    await settle(runtime)
    assert not session.aborted


async def test_a_held_session_is_not_let_go_of_until_its_command_ends(harness):
    """Live (round 10): its instructions changed while a command it left running went on; it
    was reconnected at once, and that command's end never woke it (8 minutes, until the
    Watchdog). Nor may a quiet worker's session be freed meanwhile."""
    import time

    runtime = await harness.connect()
    worker, session, _ = await held_worker(runtime, harness, "Preview")
    await runtime.refresh_session(harness.store.session(worker))
    assert runtime.states.handle(worker) is session, "its session kept"
    assert runtime.states[worker].stale, "reconnected once it is free"
    assert await runtime.workers.release_idle(time.time() + 86400) == []
    session.background = {"devsrv9": "completed"}
    session.emit("session.idle")
    await settle(runtime)
    assert runtime.states.handle(worker) is not session, "free now: reconnected"


async def test_a_turn_held_while_its_instructions_changed_reconnects_only_once_free(harness):
    runtime = await harness.connect()
    worker, session, _ = await busy_worker(runtime, harness, "Preview")
    runtime.states[worker].stale = True  # Changed during its turn.
    session.background = {"devsrv9": "running"}
    await call_tool(runtime, "report", worker, status="question", summary="Push it?")
    session.emit("assistant.idle")
    await settle(runtime)
    assert runtime.states[worker].held and runtime.states.handle(worker) is session
    session.background = {"devsrv9": "completed"}
    session.emit("session.idle")
    await settle(runtime)
    assert runtime.states.handle(worker) is not session


async def test_saving_slack_settings_does_not_let_go_of_a_held_worker(harness):
    runtime = await harness.connect()
    worker, session, _ = await held_worker(runtime, harness, "Preview")
    harness.store.execute("UPDATE sessions SET slack=1 WHERE id=?", (worker,))
    await runtime.refresh_slack()
    assert runtime.states.handle(worker) is session and runtime.states[worker].stale


async def test_a_detached_session_holds_nothing(harness):
    runtime = await harness.connect()
    worker, _, _ = await held_worker(runtime, harness, "Preview")
    await runtime.detach(worker)
    assert not runtime.states[worker].held


async def test_a_worker_waiting_on_its_command_is_not_asked_for_a_report_meanwhile(harness):
    """Live (round 10): "Waiting for `sleep 75` to finish", its turn over and that command
    still running: asked for a report then, it reported work in the middle of it (and its
    other task again at the end). It is asked only if it does not go on once it ends."""
    runtime = await harness.connect()
    worker, session, _ = await busy_worker(runtime, harness, "Build")
    session.background = {"0": "running"}
    session.emit("assistant.message", content="Waiting for `sleep 75` to finish.")
    session.emit("assistant.idle")
    await settle(runtime)
    assert runtime.states[worker].held

    def nudged():
        return [m for m in harness.store.messages(worker) if m["meta"].get("report_nudge")]

    assert not nudged()
    session.background = {"0": "completed"}
    session.emit("session.idle")  # It did not go on.
    await settle(runtime)
    assert nudged(), "its turn is over now: asked"
