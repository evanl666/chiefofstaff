"""QA round 9 (E139): a worker that waits for another worker had its answer filed as a done
report, so its task finished before its work started ("Nothing is launched yet"), and again
when it acknowledged the Router's "keep it open". A wait is read from whom it names (or whom its
task says it waits for) before its wording; nothing is ever filed as done from a turn that
ended without report(); an acknowledgement of "keep it open" is never filed. Home and the
Router see whom it waits for, and that worker's message wakes it."""

import json

import pytest
from conftest import call_tool, settle
from test_round10_lifecycle import spawn
from test_round12_checkbacks import idle, turn

from chiefofstaff.workers import WAITS

# The two live texts (names are placeholders): its turn's end, and its answer to the Router's
# "This task isn't done… keep it open".
LIVE_WAIT = (
    "The release builder expects them ready around 15:50 to 16:15 PT. Nothing is launched yet. "
    "I'll launch each pair of lower-weight twins once {peer} sends that release's snapshotId, "
    "then report the six model ids."
)
LIVE_ACK = (
    "Understood. This task stays open until all six lower-weight twins are trained and "
    "reported. Nothing is launched yet."
)
PARAPHRASES = [
    "Nothing is launched yet: I'll start the six training runs as soon as the release arrives, "
    "then report the six model ids.",
    "Nothing is launched yet. The training starts after the data worker's check passes, and I "
    "report the six model ids then.",
    "Nothing is launched yet, and nothing will be until the release snapshot ids come in; then "
    "I train the six twins and report their ids.",
]
TRAIN = (
    "Train the six lower-weight twins once the Release Builder sends the release snapshot ids "
    "(expected 15:50 to 16:15 PT); report the six model ids."
)
DONE_LIKE = (
    "Trained all six lower-weight twins on the new release: their model ids are m-101 to "
    "m-106, each within 1% of its baseline."
)


def nudged(session) -> list[str]:
    return [text for text in session.sent if "report-missing" in text]


def reports(harness, worker) -> list[dict]:
    return [m for m in harness.store.messages(worker) if m["kind"] == "report"]


async def waiting_pair(harness):
    """The Release Builder at work, and the trainer told to wait for its ids. Returns runtime,
    builder, trainer, the trainer's task and session."""
    runtime = await harness.connect()
    builder, _ = await spawn(runtime, harness.router_id, "Release Builder")
    trainer, task = await spawn(runtime, harness.router_id, "Train lower-weight twins",
                                instructions=TRAIN)  # fmt: skip
    session = harness.session(trainer)
    await turn(runtime, session)
    return runtime, builder, trainer, task, session


@pytest.mark.parametrize("said", [LIVE_WAIT, *PARAPHRASES], ids=["live", "soon", "after", "until"])
def test_the_live_wait_and_its_paraphrases_read_as_waiting(said):
    assert WAITS.search(said.format(peer="5c1f0e2a")), said


@pytest.mark.parametrize(
    "said",
    [
        DONE_LIKE,
        "I ran the suite twice after the fix; all green.",
        "After the fix everything is green, and the six ids are in the table above.",
        "I opened the PR as soon as the tests passed.",
    ],
)
def test_finished_work_does_not_read_as_waiting(said):
    assert not WAITS.search(said)


@pytest.mark.parametrize("said", [LIVE_WAIT, *PARAPHRASES], ids=["live", "soon", "after", "until"])
async def test_a_worker_waiting_for_a_peer_stays_running_and_is_woken_by_it(harness, said):
    """Live (14:45 PT): its turn ended with LIVE_WAIT, the task went to done ("It ended
    without a report: its answer is filed as one"), and it sat under Nothing open."""
    runtime, builder, trainer, task, session = await waiting_pair(harness)
    await idle(runtime, session, said.format(peer=builder[:8]))
    assert runtime.tasks.get(task["id"])["status"] == "running", "never done before it starts"
    assert not reports(harness, trainer) and not nudged(session)
    assert harness.store.session(trainer)["activity"] == "waiting for “Release Builder”"
    listed = json.loads(
        (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
    )
    entry = next(s for s in listed["sessions"] if s["session_id"] == trainer)
    assert entry["task"]["waiting_for"] == "“Release Builder”", "the Router sees whom"
    reviewed = await runtime.watchdog.check(10**10)
    assert trainer not in reviewed, "waiting is not stopping without a report"

    told = await call_tool(runtime, "tell_session", builder, session_id=f"worker:{trainer[:8]}",
                           text="Release r42 is out: snapshotId s-9.")  # fmt: skip
    assert told.text_result_for_llm == "Told “Train lower-weight twins”."
    assert "snapshotId s-9" in session.sent[-1], "its message wakes it"
    session.emit("assistant.turn_start")
    await settle(runtime)
    assert harness.store.session(trainer)["activity"] == "", "woken, it waits no more"


async def test_an_acknowledgement_of_keep_it_open_is_never_filed(harness):
    """Live (14:45:23): the Router answered the done filing with "This task isn't done… keep
    it open"; the worker's "Understood. This task stays open…" was filed as done again."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Train lower-weight twins")
    session = harness.session(worker)
    await turn(runtime, session)
    await call_tool(runtime, "report", worker, status="progress", summary="Preparing configs.")
    await idle(runtime, session, "Preparing configs.")
    keep = "This task isn't done: keep it open until all six are trained."
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    task="continue", task_id=task["id"], text=keep)  # fmt: skip
    await turn(runtime, session)
    before = len(reports(harness, worker))
    await idle(runtime, session, LIVE_ACK)
    shown = runtime.tasks.get(task["id"])
    assert (shown["status"], shown["result"]) == ("running", "")
    assert len(reports(harness, worker)) == before and not nudged(session)


async def test_a_long_answer_without_a_report_is_progress_until_the_worker_says_done(harness):
    """Never done from a turn that ended without report(): its answer is progress on the task
    (nothing is lost), and it is asked once whether it is finished. Silent again, its words go
    to the owner's review, never accepted for them, and the Router hears where they come from."""
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Train lower-weight twins")
    session = harness.session(worker)
    await turn(runtime, session)
    await idle(runtime, session, DONE_LIKE)
    shown = runtime.tasks.get(task["id"])
    assert (shown["status"], shown["progress"], shown["result"]) == ("running", DONE_LIKE, "")
    [asked] = nudged(session)
    assert "progress, not as its result" in asked
    assert ("notice", "It ended without a report: its answer is filed as progress; asked "
            "whether it is finished.") in harness.kinds(worker)  # fmt: skip
    told = [m for m in harness.store.messages(harness.router_id) if m["kind"] == "inbound"]
    assert not [m for m in told if m["meta"].get("status") == "done"], "the Router hears no done"

    await turn(runtime, session)
    await idle(runtime, session, DONE_LIKE)
    shown = runtime.tasks.get(task["id"])
    assert (shown["status"], shown["result"]) == ("review", DONE_LIKE)
    assert not runtime.task_actions.informational(shown), "the owner's to accept"
    last = [m for m in harness.store.messages(harness.router_id) if m["kind"] == "inbound"][-1]
    assert "Filed from its worker's own words" in last["meta"]["prompt"]


async def test_a_task_that_needs_the_owners_acceptance_files_the_answer_for_review(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Train lower-weight twins",
                               review=True)  # fmt: skip
    session = harness.session(worker)
    await turn(runtime, session)
    await idle(runtime, session, DONE_LIKE)
    shown = runtime.tasks.get(task["id"])
    assert (shown["status"], shown["result"]) == ("review", DONE_LIKE)
    assert not nudged(session)
