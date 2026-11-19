"""QA round 6, from the owner's report: what they wrote a worker that stayed "Waiting to be
sent", a result and its reply as one decision, the task a direct message is filed under, an old
update shown as new, and one piece of work shown twice."""

import asyncio
import time
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


async def worker_with_two_tasks(runtime, harness):
    """A worker that did task A (accepted), then task B the Router gave it, since reported."""
    router = harness.router_id
    worker = await spawn(runtime, router, "Benchmark the two cache backends")
    first = runtime.tasks.current(worker)
    await call_tool(runtime, "report", worker, status="done", summary="Backend B is 20% faster.")
    runtime.task_actions.accept(first["id"])
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    await call_tool(runtime, "message_session", router, session_id=worker, task="new",
                    why_new="another question", title="Answer whether the API docs cover paging",
                    text="Do the API docs cover paging?")  # fmt: skip
    await settle(runtime)
    second = runtime.tasks.current(worker)
    await call_tool(
        runtime, "report", worker, status="done", summary="Yes, under the list endpoints."
    )
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    assert harness.store.session(worker)["status"] != "working"
    return worker, first, runtime.tasks.get(second["id"])


def last_owner_message(harness, worker) -> dict:
    return [m for m in harness.store.messages(worker) if m["kind"] == "owner"][-1]


async def test_a_message_written_at_a_task_in_its_chat_is_filed_under_that_task(harness):
    """#3/#4: the owner opened the worker's chat at task A (its row's Worker link) and wrote
    more for A; it was filed under B, the task the Router gave it last: Home showed a row
    named B doing A's work, beside A's own row. Filed under A, the worker is named for A."""
    runtime = await harness.connect()
    worker, first, second = await worker_with_two_tasks(runtime, harness)
    published = []
    publish = runtime.bus.publish
    runtime.bus.publish = lambda kind, data: (published.append((kind, data)), publish(kind, data))
    await runtime.owner_send(worker, "Also time the cold start", task_id=first["id"])
    await settle(runtime)
    assert last_owner_message(harness, worker)["meta"]["task_id"] == first["id"]
    # Home hears A is followed up now (its row under Running), not only the worker's state.
    told = [d for kind, d in published if kind == "task" and d["id"] == first["id"]]
    assert told and told[-1]["follow_up"], "A's row moves to Running at once"
    view = runtime.view(harness.store.session(worker))
    assert view["display_title"] == first["title"], "named for what it does now"
    assert view["turn_task"] == first["id"]
    assert len(runtime.tasks.for_session(worker)) == 2, "no other task"
    assert runtime.tasks.get(second["id"])["status"] == "review", "B is left as it was"


async def test_a_message_about_none_of_its_tasks_in_particular_is_a_task_of_its_own(harness):
    """#3: several finished tasks, the latest given by the Router, and nothing says which one
    the owner means: not the latest blindly, a task of its own named from the message."""
    runtime = await harness.connect()
    worker, first, second = await worker_with_two_tasks(runtime, harness)
    view = runtime.view(harness.store.session(worker))
    assert view["chat_task"]["id"] == "", "the chat says it starts a task of its own"
    assert view["chat_task"]["titles"] == {first["id"]: first["title"],
                                           second["id"]: second["title"]}  # fmt: skip
    await runtime.owner_send(worker, "Also time the cold start\nRun it five times.")
    await settle(runtime)
    made = runtime.tasks.for_session(worker)[-1]
    assert made["id"] not in {first["id"], second["id"]}
    assert made["title"] == "Also time the cold start"
    assert made["status"] == "running"
    assert last_owner_message(harness, worker)["meta"]["task_id"] == made["id"]
    assert f"starts task {made['id']}" in harness.session(worker).sent[-1]
    view = runtime.view(harness.store.session(worker))
    assert view["display_title"] == made["title"] and view["turn_task"] == made["id"]


async def test_the_task_the_owner_looks_at_wins_over_one_that_waits(harness):
    """#3 (review): with another task stopped (waiting for Go on), a message written at task A
    in its chat went to the stopped one, and set it going again."""
    runtime = await harness.connect()
    worker, first, _second = await worker_with_two_tasks(runtime, harness)
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker, task="new",
                    why_new="third", title="Export the weekly numbers",
                    text="Export them.")  # fmt: skip
    third = runtime.tasks.current(worker)
    await runtime.workers.interrupt(worker)
    assert runtime.tasks.get(third["id"])["status"] == "waiting"
    await runtime.owner_send(worker, "Also time the cold start", task_id=first["id"])
    assert last_owner_message(harness, worker)["meta"]["task_id"] == first["id"]
    assert not harness.store.get(f"goingon:{third['id']}"), "the stopped task stays stopped"
    assert runtime.tasks.get(third["id"])["status"] == "waiting"


async def test_the_owners_own_conversation_with_a_worker_goes_on_about_its_task(harness):
    """#3: what the owner wrote about A last, the next message goes on with (not a new task),
    unless they look at another task in its chat."""
    runtime = await harness.connect()
    worker, first, second = await worker_with_two_tasks(runtime, harness)
    await runtime.owner_send(worker, "Also time the cold start", task_id=first["id"])
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    assert runtime.view(harness.store.session(worker))["chat_task"]["id"] == first["id"]
    await runtime.owner_send(worker, "And keep the older snapshots too")
    assert last_owner_message(harness, worker)["meta"]["task_id"] == first["id"]
    await runtime.owner_send(worker, "Cite the doc's section", task_id=second["id"])
    assert last_owner_message(harness, worker)["meta"]["task_id"] == second["id"]
    assert len(runtime.tasks.for_session(worker)) == 2


async def test_a_check_back_that_finds_its_runs_still_going_is_no_update(harness):
    """#5: an Update card came under Needs you an hour after the last one, saying the run was
    still going: each check-back on finished work asks its worker to report where its runs
    stand (status progress), and each such report was news. Its runs all still going, as
    before, Monitoring says so; one ended is news."""
    from test_round12_checkbacks import checking, woken
    from test_round12_runs import FLYTE, NIGHTLY

    runtime, worker, a, _b, session = await checking(harness, with_b=False)
    await woken(runtime, session)
    await call_tool(runtime, "report", worker, status="progress", task_id=a["id"],
                    summary="Still running; 2 of 5 shards written.",
                    artifacts=[NIGHTLY])  # fmt: skip
    assert not harness.store.get(f"news:{a['id']}"), "no Update for no change"
    shown = runtime.task_actions.view(runtime.tasks.get(a["id"]))
    assert shown["progress"].startswith("Still running") and shown["monitoring"]
    await call_tool(runtime, "check_back", worker, task_id=a["id"], minutes=60,
                    what="whether the nightly export finished", url=FLYTE)  # fmt: skip
    await woken(runtime, session)
    await call_tool(runtime, "report", worker, status="progress", task_id=a["id"],
                    summary="The export finished: 5 shards, 12M rows.",
                    artifacts=[{**NIGHTLY, "status": "succeeded"}])  # fmt: skip
    news = harness.store.get(f"news:{a['id']}")
    assert news and "12M rows" in news, "a run that ended is news"
    # Not woken by its check-back (the owner's chat, a PR's news): what it says is news, runs
    # or no runs.
    runtime.task_actions.news_seen(a["id"])
    await runtime.owner_send(worker, "Did Bob answer on the PR?")
    await call_tool(runtime, "report", worker, status="progress", task_id=a["id"],
                    summary="Bob approved the PR; the export runs again.",
                    artifacts=[NIGHTLY])  # fmt: skip
    news = harness.store.get(f"news:{a['id']}")
    assert news and "Bob approved" in news


def test_one_piece_of_work_is_one_row_on_the_board():
    """#6: accepted work its worker goes on with as a task of its own (the Router gives more of
    an accepted task as a new one, under the same name), the same runs on both: a row under
    Running for the new task, waiting for its background process, and one under Monitoring for
    the accepted one, with the same two runs. Watched by the open task, the accepted one is
    that task's: one row, where the open one stands."""
    from test_ui_text import board, task

    def run(name):
        return {"key": f"https://flyte.acme.dev/console/executions/{name}", "kind": "flow",
                "label": name, "url": f"https://flyte.acme.dev/console/executions/{name}",
                "waiting_for": "the run", "status": "running", "unknown": False}  # fmt: skip

    runs = [run("nightly-a"), run("nightly-b")]
    title = "Tune the retry policy, option 1"
    workers = [{"id": "w1", "status": "idle", "background": True, "turn_task": "x"},
               {"id": "w2", "status": "idle", "turn_task": "z"}]  # fmt: skip
    tasks = [
        task("y", "w1", "done", title=title, monitoring=runs, result="Both runs started."),
        task("x", "w1", "running", title=title, monitoring=runs, started=True),
        # Another worker's accepted task on the same runs is its own work: still monitored.
        task("z", "w2", "done", title="Report on the retry runs", monitoring=runs[:1]),
        # Its own accepted task with a run the open one does not watch: still monitored.
        task("v", "w1", "done", title="Export the shards", monitoring=[run("q1")]),
    ]
    found = board(tasks, workers)
    assert found["phases"]["x"] == "running/background"
    assert found["phases"]["y"] == "finished/still-following", found["phases"]
    assert found["whys"]["y"] == "Still following, in a newer task of the same name"
    assert sorted(found["sections"]["monitoring"]) == ["v", "z"]


async def test_the_routers_word_on_an_earlier_task_is_about_that_task(harness):
    """Review M1: after a check-back on A (or the Router's note on it, a changed decision), the
    owner's next message became a task of its own: a second row for A's work. Only a task the
    Router started (its first instruction) leaves it open."""
    from chiefofstaff.envelope import wrap

    runtime = await harness.connect()
    worker, first, _second = await worker_with_two_tasks(runtime, harness)
    await runtime.delivery.deliver(
        worker,
        wrap("system", "check-back", "Time to check the load test.", task=first["id"]),
        kind="router",
        display="Check back: the load test",
        meta={"task_id": first["id"], "check_back": True},
    )
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    assert runtime.view(harness.store.session(worker))["chat_task"]["id"] == first["id"]
    await runtime.owner_send(worker, "Also time the cold start")
    assert last_owner_message(harness, worker)["meta"]["task_id"] == first["id"]
    assert len(runtime.tasks.for_session(worker)) == 2, "no task of its own"


async def test_a_routers_note_on_an_earlier_task_is_about_that_task(harness):
    runtime = await harness.connect()
    worker, first, _second = await worker_with_two_tasks(runtime, harness)
    await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                    task="note", task_id=first["id"],
                    text="Backend B is the one we keep.")  # fmt: skip
    await runtime.owner_send(worker, "Also time the cold start")
    assert last_owner_message(harness, worker)["meta"]["task_id"] == first["id"]
    assert len(runtime.tasks.for_session(worker)) == 2


async def test_a_failure_said_on_a_check_back_is_an_update(harness):
    """Review M2: on a check-back, a report whose runs all still went was no Update, also one
    that said a failure in words only (no runs declared), or declared a new failed run beside
    the one still going. Only a report that says every run again as it stood, still going, is
    left to Monitoring."""
    from test_round12_checkbacks import checking, woken
    from test_round12_runs import FLYTE, NIGHTLY

    runtime, worker, a, _b, session = await checking(harness, with_b=False)
    await woken(runtime, session)
    await call_tool(runtime, "report", worker, status="progress", task_id=a["id"],
                    summary="The export failed: out of disk on shard 3.")  # fmt: skip
    news = harness.store.get(f"news:{a['id']}")
    assert news and "out of disk" in news, "said in words only: an Update"
    runtime.task_actions.news_seen(a["id"])
    await call_tool(runtime, "check_back", worker, task_id=a["id"], minutes=60,
                    what="whether the nightly export finished", url=FLYTE)  # fmt: skip
    await woken(runtime, session)
    await call_tool(runtime, "report", worker, status="progress", task_id=a["id"],
                    summary="Still running; the backfill run failed.",
                    artifacts=[NIGHTLY, {"url": FLYTE + "b2", "name": "Backfill",
                                         "status": "failed"}])  # fmt: skip
    news = harness.store.get(f"news:{a['id']}")
    assert news and "backfill" in news.lower(), "a new failed run: an Update"


async def test_send_now_with_nothing_due_takes_no_slot_later(harness):
    """Review L1: Send now with nothing waiting (or on a busy worker) left its "now" set, and the
    Router's next message to it went over the parallel-worker limit."""
    harness.store.save_settings(harness.store.settings().model_copy(update={"max_workers": 1}))
    runtime = await echoing(harness)
    other = await spawn(runtime, harness.router_id, "Update the docs")
    await call_tool(runtime, "report", other, status="done", summary="Updated the docs.")
    harness.session(other).emit("session.idle")
    await settle(runtime)
    await busy_worker(runtime, harness, "Fix the login timeout")
    await runtime.send_now(other)
    assert not runtime.states[other].now, "nothing was due"
    await call_tool(runtime, "message_session", harness.router_id, session_id=other,
                    task="note", text="FYI: the docs site moved.")  # fmt: skip
    await settle(runtime)
    assert harness.store.session(other)["status"] == "queued", "it waits for a slot"


async def test_a_new_task_whose_message_never_went_is_rolled_back(harness, monkeypatch):
    """Review L2: the task made of the owner's message (about none of its tasks) stayed running,
    and named the worker, when the message could not be delivered."""
    runtime = await harness.connect()
    worker, _first, second = await worker_with_two_tasks(runtime, harness)

    async def refused(*args, **kwargs):
        raise RuntimeError("Could not deliver the message: connection lost.")

    monkeypatch.setattr(runtime.delivery, "deliver", refused)
    try:
        await runtime.owner_send(worker, "Also time the cold start")
    except RuntimeError:
        pass
    else:
        raise AssertionError("the delivery error is the caller's")
    assert [t["id"] for t in runtime.tasks.for_session(worker)] == [_first["id"], second["id"]]
    assert runtime.tasks.current(worker)["id"] == second["id"]
    assert runtime.view(harness.store.session(worker))["display_title"] == second["title"]


async def test_a_newer_release_is_one_quiet_line_read_from_pypi_once_a_day(harness, monkeypatch):
    """Owner's ask (a stale uvx cache kept a user on 0.1.45): when a newer Chief of Staff is
    out, the page says so in one line with the command that updates it; PyPI is read at most
    once a day, never while Settings turns the line off."""
    from chiefofstaff import runtime as runtime_module
    from chiefofstaff import updates

    reads = []

    async def latest() -> str:
        reads.append(1)
        return "9.0.0"

    monkeypatch.setattr(updates, "fetch_latest", latest)
    monkeypatch.setattr(updates, "installed", lambda: "0.1.47")
    monkeypatch.setattr(updates, "upgrade_command", lambda: "uvx chiefofstaff@latest")
    monkeypatch.setattr(runtime_module, "UPDATE_DELAY", 0)
    runtime = await harness.connect()
    assert "update" not in runtime.snapshot()
    watching = asyncio.create_task(runtime.watch_updates())
    await settle(runtime)
    assert runtime.snapshot()["update"] == {
        "version": "9.0.0",
        "command": "uvx chiefofstaff@latest",
    }
    watching.cancel()
    at = time.time()
    assert await updates.check(harness.store, now=at + 3600) == "9.0.0" and len(reads) == 1
    assert await updates.check(harness.store, now=at + 86400) == "9.0.0" and len(reads) == 2
    harness.store.save_settings(
        harness.store.settings().model_copy(update={"update_notice": False})
    )
    assert "update" not in runtime.snapshot()
    assert await updates.check(harness.store, now=at + 3 * 86400) == "" and len(reads) == 2
    monkeypatch.setattr(updates, "installed", lambda: "9.0.0")
    harness.store.save_settings(harness.store.settings().model_copy(update={"update_notice": True}))
    assert "update" not in runtime.snapshot(), "not newer than this one"


def test_the_update_command_is_the_way_chief_of_staff_was_installed():
    from chiefofstaff.updates import newer, upgrade_command

    command = upgrade_command.__wrapped__
    assert command("/Users/acme/.cache/uv/archive-v0/x1y2") == "uvx chiefofstaff@latest"
    assert command("/Users/acme/.local/share/uv/tools/chiefofstaff") == (
        "uv tool upgrade chiefofstaff"
    )
    assert command("/Users/acme/.local/pipx/venvs/chiefofstaff") == "pipx upgrade chiefofstaff"
    assert command("/Users/acme/tools/.venv") == "pip install --upgrade chiefofstaff"
    assert newer("0.1.10", "0.1.9") and not newer("0.1.47", "0.1.47")
    assert not newer("0.2.0rc1", "0.1.47"), "never a pre-release"
