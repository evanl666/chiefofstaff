"""QA round 9 (E140, E142): the Watchdog read a worker's activity from its chat only, so a
27-minute model call and a 33-minute command were "no activity", and it acted minutes later
without looking again; its packet saw the last 30 messages only. A nudge to a worker at work on
a follow-up of finished work was "not sent: it has no running task", and the escalation that
followed made the Router give it a duplicate task. A session's events count as activity (an
open model call, a command whose output still grows), the state is read again just before
acting, the packet says what it is at and its latest reports, and a follow-up turn is nudged
within it."""

import time

from conftest import call_tool, settle
from test_round10_lifecycle import spawn
from test_round12_checkbacks import idle, turn
from test_watchdog import NOW, TASK, message, worker

from chiefofstaff.watchdog import WatchConfig, flags

CONFIG = WatchConfig()


def test_a_long_model_call_is_activity_and_a_command_as_old_as_its_output():
    """Unit: with what its session's events say (Live), a 25-minute model call is not
    silence; a step's output that stopped growing 17 minutes ago is."""
    from chiefofstaff.watchdog import Live

    chat = [TASK, message("tool", "bash ./train.sh", -1500)]
    model = Live(NOW - 1600, NOW, "model call since 12:00")
    assert flags(worker(updated=-1500), chat, NOW, CONFIG, model) == []
    grows = Live(NOW - 1600, NOW - 60, "tool `./train.sh` running")
    assert flags(worker(updated=-1500), chat, NOW, CONFIG, grows) == []
    stopped = Live(NOW - 1600, NOW - 1000, "tool `./train.sh` running")
    assert flags(worker(updated=-1500), chat, NOW, CONFIG, stopped) == ["no activity for 17 min"]


async def in_turn(harness, title="Re-review acme/app#7"):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, title)
    session = harness.session(worker)
    await turn(runtime, session)
    return runtime, worker, task, session


async def test_a_worker_in_a_long_model_call_is_not_flagged(harness):
    """Live 13:04 PT: one model call from 12:37 to 13:04 (the review it then reported) was
    "no activity for 20 min"."""
    runtime, worker, _task, session = await in_turn(harness)
    session.emit("assistant.turn_start")
    await settle(runtime)
    assert worker not in await runtime.watchdog.check(time.time() + 25 * 60)
    session.emit("assistant.message", content="Here is the review.")
    await settle(runtime)
    assert worker in await runtime.watchdog.check(time.time() + 25 * 60), "its call is over"


async def test_a_long_command_is_flagged_only_once_its_output_stops_growing(harness):
    """Live 13:04 PT: a 33-minute command (a narrowed scan) was "gone silent for 26 min"."""
    runtime, worker, _task, session = await in_turn(harness, "Scan the exports")
    session.emit("tool.execution_start", tool_name="bash", tool_call_id="c1",
                 arguments={"command": "./scan.sh --narrow"})  # fmt: skip
    session.emit("tool.shell_output", tool_call_id="c1", text="12000 rows\n", sequence=1)
    await settle(runtime)
    state = runtime.states[worker]
    later = time.time() + 25 * 60  # 25 minutes into the command, its output grew a minute ago.
    state.event_at = state.output_at["c1"] = later - 60
    assert worker not in await runtime.watchdog.check(later)
    state.event_at = state.output_at["c1"] = later - 16 * 60
    assert worker in await runtime.watchdog.check(later), "its output stopped growing"
    packet = harness.session(await runtime.watchdog.session_id()).sent[-1]
    assert "Now: in a turn since " in packet and "tool `bash ./scan.sh --narrow` running" in packet
    assert "its output last grew at" in packet


async def test_the_packet_has_its_latest_reports_however_far_back(harness):
    """Live: "the owner's question … has no reported answer", said of a worker that had
    answered it twice, outside the packet's 30 messages."""
    runtime, worker, task, session = await in_turn(harness)
    await call_tool(runtime, "report", worker, status="progress", task_id=task["id"],
                    summary="Answered the owner: the flag ships off by default.")  # fmt: skip
    for step in range(40):
        session.emit("tool.execution_start", tool_name="bash", tool_call_id=f"s{step}",
                     arguments={"command": f"grep -n flag part{step}.py"})  # fmt: skip
        session.emit("tool.execution_complete", tool_call_id=f"s{step}")
    await settle(runtime)
    assert worker in await runtime.watchdog.check(time.time() + 20 * 60)
    packet = harness.session(await runtime.watchdog.session_id()).sent[-1]
    assert "Its latest reports" in packet
    assert "Answered the owner: the flag ships off by default." in packet


async def test_a_nudge_is_dropped_when_the_worker_is_at_work_again(harness):
    """Live: the Watchdog acted seven minutes after flagging, both workers then seconds from
    their reports; its escalation made a duplicate task. Read again just before acting."""
    runtime, worker, _task, session = await in_turn(harness)
    assert worker in await runtime.watchdog.check(time.time() + 20 * 60)
    session.emit("assistant.turn_start")  # Its next model call: at work.
    await settle(runtime)
    sent = len(session.sent)
    watchdog = await runtime.watchdog.session_id()
    router = len(harness.session(harness.router_id).sent)
    for action in ("nudge", "escalate"):
        result = await call_tool(runtime, "assess_worker", watchdog, session_id=worker,
                                 verdict="stuck", action=action, reason="Silent for 20 min.",
                                 message="Report where you are.")  # fmt: skip
        assert result.text_result_for_llm.startswith("Not sent: it is still at work, in a turn")
    assert len(session.sent) == sent and len(harness.session(harness.router_id).sent) == router
    looping = await call_tool(runtime, "assess_worker", watchdog, session_id=worker,
                              verdict="looping", action="nudge", reason="Same edit 9 times.",
                              message="Try another way.")  # fmt: skip
    assert looping.text_result_for_llm == "Recorded: nudge.", "working is no answer to looping"


async def following_up(harness):
    """A worker whose only task is finished (in review), at work on a follow-up of it (a PR
    thread's news)."""
    runtime, worker, task, session = await in_turn(harness)
    await call_tool(runtime, "report", worker, status="done", summary="Reviewed: 2 comments.")
    await idle(runtime, session)
    assert runtime.tasks.get(task["id"])["status"] == "review"
    harness.store.set(f"turntask:{worker}", task["id"])
    runtime.states[worker].busy = True
    runtime.update_session(worker, status="working")
    runtime.states[worker].event_at = time.time() - 20 * 60
    return runtime, worker, task, session


async def test_a_nudge_reaches_a_worker_at_work_on_a_follow_up(harness):
    """Live 13:04 PT: "Not sent: it has no running task now" to a worker in a turn on its
    finished review's PR thread; a steered nudge would have reached it."""
    runtime, worker, _task, session = await following_up(harness)
    sent = len(session.sent)
    watchdog = await runtime.watchdog.session_id()
    result = await call_tool(runtime, "assess_worker", watchdog, session_id=worker,
                             verdict="stuck", action="nudge", reason="Quiet for 20 min.",
                             message="Say where the follow-up stands.")  # fmt: skip
    assert result.text_result_for_llm == "Recorded: nudge."
    assert len(session.sent) == sent + 1 and "Say where the follow-up stands." in session.sent[-1]


async def test_an_escalation_of_a_follow_up_says_where_its_report_lands(harness):
    runtime, worker, task, _session = await following_up(harness)
    watchdog = await runtime.watchdog.session_id()
    await call_tool(runtime, "assess_worker", watchdog, session_id=worker, verdict="stuck",
                    action="escalate", reason="It never reported its re-review.")  # fmt: skip
    escalation = harness.session(harness.router_id).sent[-1]
    assert escalation.startswith('<message from="watchdog" type="escalation"')
    assert f"It is working on a follow-up of task {task['id']}" in escalation
    assert "its report lands on that task. No new task is needed for it." in escalation


async def test_an_idle_worker_with_nothing_running_is_still_not_nudged(harness):
    runtime, worker, _task, session = await following_up(harness)
    runtime.states[worker].busy = False
    runtime.update_session(worker, status="idle")
    sent = len(session.sent)
    watchdog = await runtime.watchdog.session_id()
    result = await call_tool(runtime, "assess_worker", watchdog, session_id=worker,
                             verdict="stuck", action="nudge", reason="Quiet.",
                             message="Report.")  # fmt: skip
    assert result.text_result_for_llm == "Not sent: it has no running task now."
    assert len(session.sent) == sent
