"""Autopilot's switch and runs (autopilot/core.py): nothing of it while its line is unticked; a
run from Home or a command, its Stop, its end, a restart."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import settle

from chiefofstaff.autopilot.logbook import QUEUED, TABLES, WITHDRAWN
from chiefofstaff.models import Settings


def tables(store):
    return {row["name"] for row in store.all("SELECT name FROM sqlite_master WHERE type='table'")}


async def tick(runtime, **changes):
    """The Settings line ticked (or anything else changed), saved as Settings saves it."""
    old = runtime.store.settings()
    new = old.model_copy(update={"autopilot": True, **changes})
    runtime.store.save_settings(new)
    await runtime.settings_changed(old, new)
    await settle(runtime)


def notices(harness):
    return [m for m in harness.store.messages(harness.router_id) if m["kind"] == "notice"]


async def test_nothing_of_it_while_its_line_was_never_ticked(harness):
    runtime = await harness.connect()
    snapshot = runtime.snapshot()
    assert "autopilot" not in snapshot
    settings = snapshot["settings"]
    assert settings["autopilot"] is False and settings["autopilot_credits"] == 100
    assert settings["autopilot_repos"] == [] and settings["autopilot_ask_first"] is True
    await runtime.autopilot.tick()
    await runtime.autopilot.started()
    assert not tables(harness.store) & set(TABLES) and not harness.store.keys("autopilot:")
    with pytest.raises(ValueError, match="tick it in Settings"):
        await runtime.autopilot.start(None, by="home")


def test_settings_saved_before_it_load_with_it_off(tmp_path):
    from chiefofstaff.store import Store

    store = Store(tmp_path / "data")
    saved = Settings().model_dump()
    for key in ("autopilot", "autopilot_credits", "autopilot_repos", "autopilot_ask_first"):
        saved.pop(key)
    store.execute("INSERT OR REPLACE INTO settings VALUES (1, ?)", (json.dumps(saved),))
    settings = store.settings()
    assert settings.autopilot is False and settings.autopilot_credits == 100
    assert settings.autopilot_ask_first is True
    assert Settings(autopilot_repos=["Acme/API", "acme/api"]).autopilot_repos == ["acme/api"]


async def test_ticking_the_line_makes_its_tables_and_starts_nothing(harness):
    runtime = await harness.connect()
    await tick(runtime)
    assert set(TABLES) <= tables(harness.store)
    assert runtime.snapshot()["autopilot"]["run"] is None
    assert runtime.autopilot.book.runs() == []


async def test_home_turns_it_on_until_a_time_with_one_note_to_the_router(harness):
    runtime = await harness.connect()
    await tick(runtime)
    state = await runtime.autopilot.start(runtime.autopilot.until_of("2h"), by="home")
    await settle(runtime)
    assert state["run"] and state["done"] == 0 and state["paused"] == ""
    until = datetime.fromisoformat(state["until"])
    assert timedelta(hours=1, minutes=59) < until - datetime.now(UTC) <= timedelta(hours=2)
    note = notices(harness)[-1]
    assert note["text"].startswith("Autopilot on until ")
    assert note["meta"]["autopilot"]["run"] == state["run"]
    assert 'type="autopilot"' in note["meta"]["prompt"]
    book = runtime.autopilot.book
    assert [e["kind"] for e in book.entries(run_id=state["run"])] == ["started"]
    assert book.outbox(QUEUED) == [] and book.check().ok
    assert runtime.snapshot()["autopilot"]["run"] == state["run"]


async def test_a_command_ticks_the_line_and_is_answered_without_the_routers_model(harness):
    runtime = await harness.connect()
    router = harness.router_id
    turns = len(harness.session(router).sent)
    said = await runtime.owner_send(router, "autopilot on", typed=True)
    await settle(runtime)
    assert harness.store.settings().autopilot is True
    assert said["text"].startswith("Autopilot on until ")
    owner = [m for m in harness.store.messages(router) if m["kind"] == "owner"][-1]
    assert owner["text"] == "autopilot on" and "prompt" not in owner["meta"]
    assert owner["meta"]["ingress"] == "page" and owner["meta"]["autopilot_command"]
    assert not any("autopilot on" in prompt for prompt in harness.session(router).sent[turns:])
    run = runtime.autopilot.run()
    assert run and run["started_by"] == "chat"
    until = datetime.fromisoformat(run["until"])
    assert until - datetime.now(UTC) <= timedelta(hours=2)
    # Its Chinese twin, while on: the new end, said in Chinese.
    said = await runtime.owner_send(router, "开 autopilot 直到我关掉", typed=True)
    assert said["text"] == "Autopilot 已开启\uff0c直到你关闭"
    assert runtime.autopilot.run()["until"] is None
    assert [e["kind"] for e in runtime.autopilot.book.entries(run_id=run["id"])] == [
        "started",
        "renewed",
    ]


async def test_a_near_miss_goes_to_the_router_and_typed_commands_only_from_the_page(harness):
    runtime = await harness.connect()
    router = harness.router_id
    await runtime.owner_send(router, "turn on autopilot please", typed=True)
    await runtime.owner_send(router, "autopilot on")  # Not typed in the page: an away answer.
    await settle(runtime)
    assert harness.store.settings().autopilot is False
    sent = [m for m in harness.store.messages(router) if m["kind"] == "owner"]
    assert all("prompt" in m["meta"] for m in sent), "both went to the Router as before"


async def test_stop_withdraws_what_was_queued_and_ends_with_its_summary(harness):
    runtime = await harness.connect()
    await tick(runtime)
    state = await runtime.autopilot.start(None, by="home")
    await settle(runtime)
    book = runtime.autopilot.book
    with harness.store.transaction():
        entry = book.append(state["run"], "nudged", "Nudged", task_id="t1")
        item = book.queue(state["run"], entry["id"], "router-note", {"note": "x", "display": "y"},
                          book.generation())  # fmt: skip
    stopped = await runtime.autopilot.stop(by="you", why="you pressed Stop")
    await settle(runtime)
    assert stopped["run"] is None and runtime.autopilot.run() is None
    assert stopped["said"].startswith("Autopilot stopped.")
    assert book.item(item)["state"] == WITHDRAWN
    kinds = [e["kind"] for e in book.entries(run_id=state["run"])]
    assert kinds == ["started", "nudged", "stopping", "withdrawn", "ended"]
    run = book.run(state["run"])
    assert run["state"] == "ended" and run["ended_by"] == "you"
    summary = harness.store.messages(harness.router_id)[-1]
    assert summary["kind"] == "update" and summary["meta"]["autopilot_summary"]["run"] == run["id"]
    assert summary["text"] == "Autopilot did 1 thing and left 0 for you."
    assert summary["meta"]["deferred"], "the Router hears it is off with its next message"
    assert book.check().ok and harness.store.get("autopilot:head")
    # Stop again: nothing on.
    again = await runtime.autopilot.stop(by="you", why="you pressed Stop")
    assert again["said"] == "Autopilot is off."


async def test_autopilot_off_by_chat_says_so_in_its_language(harness):
    runtime = await harness.connect()
    router = harness.router_id
    said = await runtime.owner_send(router, "关 autopilot", typed=True)
    assert said["text"] == "Autopilot 没有开着\u3002"
    await runtime.owner_send(router, "autopilot for 3h", typed=True)
    said = await runtime.owner_send(router, "stop autopilot", typed=True)
    assert said["text"] == "Autopilot stopped." and runtime.autopilot.run() is None


async def test_a_run_ends_at_its_time_with_a_quiet_line_when_nothing_came_up(harness):
    runtime = await harness.connect()
    await tick(runtime)
    moment = datetime.now().astimezone()
    await runtime.autopilot.start(moment + timedelta(seconds=1), by="home", moment=moment)
    run = runtime.autopilot.run()
    harness.store.execute(
        "UPDATE autopilot_runs SET until=? WHERE id=?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), run["id"]),
    )
    await runtime.autopilot.tick()
    await settle(runtime)
    ended = runtime.autopilot.book.run(run["id"])
    assert ended["state"] == "ended" and ended["ended_by"] == "time"
    summary = harness.store.messages(harness.router_id)[-1]
    assert (
        summary["text"].startswith("Autopilot ended at ") and "nothing came up" in (summary["text"])
    )


async def test_unticking_the_line_stops_a_run_and_keeps_the_record(harness):
    runtime = await harness.connect()
    await tick(runtime)
    state = await runtime.autopilot.start(None, by="home")
    await tick(runtime, autopilot=False)
    assert "autopilot" not in runtime.snapshot()
    run = runtime.autopilot.book.run(state["run"])
    assert run["state"] == "ended" and run["ended_by"] == "settings"
    assert set(TABLES) <= tables(harness.store)


async def test_a_restart_ends_a_run_whose_time_passed_and_says_an_open_ended_one_is_on(harness):
    runtime = await harness.connect()
    await tick(runtime)
    book = runtime.autopilot.book
    past = book.new_run(until=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                        started_by="home", rules={})  # fmt: skip
    await runtime.autopilot.started()
    await settle(runtime)
    assert book.run(past["id"])["ended_by"] == "time"
    await runtime.autopilot.start(None, by="home")
    await settle(runtime)
    before = len(harness.store.messages(harness.router_id))
    await runtime.autopilot.started()
    line = harness.store.messages(harness.router_id)[before:]
    assert [m["text"] for m in line if m["meta"].get("autopilot", {}).get("still_on")] == [
        line[0]["text"]
    ] and line[0]["text"].startswith("Autopilot is still on (until you turn it off), since ")
    # A timed one goes on silently.
    await runtime.autopilot.start(runtime.autopilot.until_of("2h"), by="home")
    await settle(runtime)
    before = len(harness.store.messages(harness.router_id))
    await runtime.autopilot.started()
    assert len(harness.store.messages(harness.router_id)) == before


async def test_an_item_claimed_when_it_stopped_is_reconciled_at_start(harness):
    runtime = await harness.connect()
    await tick(runtime)
    state = await runtime.autopilot.start(runtime.autopilot.until_of("2h"), by="home")
    book = runtime.autopilot.book
    with harness.store.transaction():
        entry = book.append(state["run"], "nudged", "N")
        lost = book.queue(state["run"], entry["id"], "router-note", {}, book.generation())
        kept = book.queue(state["run"], entry["id"], "router-note", {}, book.generation())
    book.claim(lost)
    book.claim(kept)
    book.stored(kept, 1)
    await runtime.autopilot.started()
    assert book.item(lost)["state"] == "unknown" and book.item(kept)["state"] == "done"


async def test_the_page_shows_the_menu_durations(harness):
    runtime = await harness.connect()
    await tick(runtime, digest_time="08:30")
    moment = datetime(2026, 11, 24, 23, 0).astimezone()
    autopilot = runtime.autopilot
    assert autopilot.until_of("2h", moment) == moment + timedelta(hours=2)
    morning = autopilot.until_of("morning", moment)
    assert (morning.hour, morning.minute, morning.day) == (8, 30, 25)
    assert autopilot.until_of("off", moment) is None
    with pytest.raises(ValueError, match="Choose"):
        autopilot.until_of("forever", moment)
