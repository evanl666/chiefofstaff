"""Autopilot's record (autopilot/logbook.py): made on first use, append-only and chained, with
complete payloads; its outbox, its own decisions, and the owner's acts it may cite."""

import os
import sqlite3
from contextlib import closing

import pytest

from chiefofstaff.autopilot.logbook import (
    ACTIVE,
    CHANGED,
    CLAIMED,
    DONE,
    GENESIS,
    HEAD_KEY,
    INLINE,
    PART,
    QUEUED,
    TABLES,
    UNKNOWN,
    WITHDRAWN,
    Logbook,
)
from chiefofstaff.store import Store


@pytest.fixture
def book(tmp_path):
    made = Logbook(Store(tmp_path / "data"))
    made.ensure()
    return made


def tables(store):
    return {row["name"] for row in store.all("SELECT name FROM sqlite_master")}


def raw(book):
    """A connection of its own, as anything outside Chief of Staff would open the file."""
    return closing(sqlite3.connect(book.store.directory / "chiefofstaff.db"))


def test_nothing_exists_until_first_use(tmp_path):
    store = Store(tmp_path / "data")
    book = Logbook(store)
    assert not book.exists() and not tables(store) & set(TABLES)
    assert book.runs() == [] and book.entries() == [] and book.outbox() == []
    assert book.decisions() == [] and book.act("card", "c1") is None and book.size() == 0
    assert book.check().ok and not store.keys("autopilot:")
    book.ensure()
    assert set(TABLES) <= tables(store)
    assert not store.keys("autopilot:"), "no kv key either, until something needs one"


def test_made_inside_a_transaction_that_fails_leaves_nothing(tmp_path):
    store = Store(tmp_path / "data")

    def failing():
        with store.transaction():
            Logbook(store).ensure()
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        failing()
    assert not tables(store) & set(TABLES)


def test_entries_are_chained_and_point_at_what_they_change(book):
    run = book.new_run(until=None, started_by="home", rules={"version": 1})
    first = book.append(run["id"], "started", "On", why={"rule": "owner"})
    second = book.append(run["id"], "accepted", "Accepted a result", task_id="t1")
    undo = book.append(run["id"], "undone", "Undone", of=second["id"], state="undone")
    assert first["prev"] == GENESIS and second["prev"] == first["hash"]
    assert undo["prev"] == second["hash"]
    assert book.latest(second["id"])["id"] == undo["id"]
    assert book.latest(first["id"])["id"] == first["id"]
    assert [e["id"] for e in book.entries(run_id=run["id"])] == [
        first["id"],
        second["id"],
        undo["id"],
    ]
    assert [e["id"] for e in book.entries(task_id="t1")] == [second["id"]]
    assert [e["id"] for e in book.entries(of=second["id"])] == [undo["id"]]
    assert book.check().ok and book.check().entries == 3


def test_entries_and_blobs_refuse_update_and_delete_from_any_code_path(book):
    run = book.new_run(until=None, started_by="home", rules={})
    entry = book.append(run["id"], "nudged", "Nudged", payload={"text": "x" * (INLINE + 1)})
    for sql in (
        "UPDATE autopilot_log SET title='changed'",
        "DELETE FROM autopilot_log",
        "UPDATE autopilot_blobs SET size=0",
        "DELETE FROM autopilot_blobs",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="kept as it was written"):
            book.store.execute(sql)
        with raw(book) as db, pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)  # An older version, or any other program, through SQLite.
    assert book.entry(entry["id"])["title"] == "Nudged"


def test_a_changed_entry_breaks_the_chain_from_where_it_was_changed(book):
    run = book.new_run(until=None, started_by="home", rules={})
    kept = book.append(run["id"], "started", "On")
    changed = book.append(run["id"], "accepted", "Accepted a result")
    book.append(run["id"], "nudged", "Nudged")
    with raw(book) as db:
        db.execute("DROP TRIGGER autopilot_log_kept_update")
        db.execute("UPDATE autopilot_log SET title='Accepted something else' WHERE id=?",
                   (changed["id"],))  # fmt: skip
        db.commit()
    found = book.check()
    assert not found.ok and found.seq == changed["seq"] and found.after == kept["at"]


def test_a_chain_rewritten_or_cut_short_is_found_by_the_head_kept_at_a_run_end(book):
    run = book.new_run(until=None, started_by="home", rules={})
    book.append(run["id"], "started", "On")
    last = book.append(run["id"], "ended", "Off")
    book.keep_head()
    assert book.store.get(HEAD_KEY) == f"{last['seq']}:{last['hash']}"
    with raw(book) as db:
        db.execute("DROP TRIGGER autopilot_log_kept_delete")
        db.execute("DELETE FROM autopilot_log WHERE id=?", (last["id"],))
        db.commit()
    found = book.check()
    assert not found.ok and found.seq == last["seq"]


def test_payloads_are_kept_whole_as_compressed_blobs_in_parts(book):
    run = book.new_run(until=None, started_by="home", rules={})
    # Random text barely compresses: more than one 256 KB part.
    output = os.urandom(400 * 1024).hex()
    entry = book.append(
        run["id"], "accepted", "Accepted", evidence={"runs": [{"output": output, "exit": 0}]},
        payload={"diff": "small"},
    )  # fmt: skip
    stored = entry["evidence"]["runs"][0]["output"]
    assert stored["size"] == len(output) and stored["head"] == output[:400]
    parts = book.store.value(
        "SELECT count(*) FROM autopilot_blobs WHERE hash=?", (stored["$blob"],)
    )
    assert parts == -(-book.store.value(
        "SELECT sum(length(data)) FROM autopilot_blobs WHERE hash=?", (stored["$blob"],)
    ) // PART) and parts > 1  # fmt: skip
    assert book.whole(entry["evidence"]) == {"runs": [{"output": output, "exit": 0}]}
    assert book.whole(entry["payload"]) == {"diff": "small"}
    # The same content again is the same blob.
    again = book.append(run["id"], "accepted", "Again", evidence={"output": output})
    assert again["evidence"]["output"]["$blob"] == stored["$blob"]
    assert book.size() > len(output) // 3


def test_a_changed_blob_is_said_and_not_read_back_as_if_whole(book):
    run = book.new_run(until=None, started_by="home", rules={})
    entry = book.append(run["id"], "accepted", "A", payload={"diff": "d" * (INLINE + 10)})
    digest = entry["payload"]["diff"]["$blob"]
    with raw(book) as db:
        db.execute("DROP TRIGGER autopilot_blobs_kept_update")
        db.execute("UPDATE autopilot_blobs SET data=? WHERE hash=?", (b"x", digest))
        db.commit()
    assert book.blob(digest) is None
    assert book.whole(entry["payload"])["diff"]["changed"] is True


def test_the_entry_hash_covers_its_blobs(book):
    run = book.new_run(until=None, started_by="home", rules={})
    entry = book.append(run["id"], "accepted", "A", payload={"diff": "d" * (INLINE + 10)})
    other = "e" * (INLINE + 10)
    book.put(other.encode())
    with raw(book) as db:
        db.execute("DROP TRIGGER autopilot_log_kept_update")
        db.execute(
            "UPDATE autopilot_log SET payload=replace(payload, ?, ?) WHERE id=?",
            (entry["payload"]["diff"]["$blob"], book.put(other.encode()), entry["id"]),
        )
        db.commit()
    assert not book.check().ok


def test_a_transaction_that_fails_leaves_neither_the_change_nor_its_entry(book):
    run = book.new_run(until=None, started_by="home", rules={})
    store = book.store
    store.set("task-status", "review")

    def accept():
        with store.transaction():
            store.set("task-status", "done")
            entry = book.append(run["id"], "accepted", "Accepted", task_id="t1")
            book.queue(run["id"], entry["id"], "tell", {"session": "s1"}, book.generation())
            raise RuntimeError("injected after the change and its entry, before the commit")

    with pytest.raises(RuntimeError, match="injected"):
        accept()
    assert store.get("task-status") == "review"
    assert book.entries(task_id="t1") == [] and book.outbox(QUEUED) == []
    # The chain goes on from where it was.
    assert book.append(run["id"], "nudged", "N")["prev"] == GENESIS and book.check().ok


def test_an_outbox_item_is_delivered_once_after_its_commit(book):
    run = book.new_run(until=None, started_by="home", rules={})
    with book.store.transaction():
        entry = book.append(run["id"], "nudged", "Nudged")
        item = book.queue(run["id"], entry["id"], "tell", {"text": "hi"}, book.generation())
    assert [row["id"] for row in book.outbox()] == [item]
    claimed = book.claim(item)
    assert claimed["state"] == CLAIMED and claimed["payload"] == {"text": "hi"}
    assert book.claim(item) is None, "claimed once"
    assert book.outbox(CLAIMED)[0]["id"] == item, "unfinished: reconciled at start"
    assert book.finish(item, DONE) and not book.finish(item, DONE)
    assert book.item(item)["state"] == DONE and book.outbox(QUEUED, CLAIMED) == []
    with pytest.raises(ValueError, match="outcome"):
        book.finish(item, QUEUED)


def test_stop_withdraws_what_was_queued_each_with_an_entry(book):
    run = book.new_run(until=None, started_by="home", rules={})
    generation = book.generation()
    with book.store.transaction():
        entry = book.append(run["id"], "nudged", "Nudged", task_id="t1")
        first = book.queue(run["id"], entry["id"], "tell", {}, generation)
        second = book.queue(run["id"], entry["id"], "tell", {}, generation)
    book.claim(first)
    assert book.withdraw("You pressed Stop.", run["id"]) == 1
    assert book.item(second)["state"] == WITHDRAWN and book.item(first)["state"] == CLAIMED
    withdrawn = book.entries(of=entry["id"])
    assert [e["kind"] for e in withdrawn] == [WITHDRAWN] and withdrawn[0]["task_id"] == "t1"
    # Queued before a Stop, claimed after it: withdrawn then.
    third = book.queue(run["id"], entry["id"], "tell", {}, generation)
    book.bump()
    assert book.claim(third) is None and book.item(third)["state"] == WITHDRAWN
    assert book.finish(first, UNKNOWN)
    assert book.check().ok


def test_runs_move_by_compare_and_set(book):
    run = book.new_run(until="2026-11-24T18:00:00+00:00", started_by="chat", rules={})
    assert book.open_runs()[0]["id"] == run["id"] and run["state"] == "on"
    assert book.move_run(run["id"], "on", state="stopping")
    assert not book.move_run(run["id"], "on", state="ended"), "it moved on already"
    assert book.move_run(run["id"], ("on", "stopping"), state="ended", ended_by="you")
    assert book.open_runs() == [] and book.run(run["id"])["ended_by"] == "you"
    with pytest.raises(ValueError, match="run field"):
        book.move_run(run["id"], "ended", generation=3)


def test_its_decisions_live_apart_from_the_owners(book):
    run = book.new_run(until=None, started_by="home", rules={})
    entry = book.append(run["id"], "decided", "Decided")
    made = book.decide(
        run_id=run["id"], entry_id=entry["id"], card_id="c1", question="Which store?",
        options=[{"choice": "Postgres"}], choice="Postgres",
        basis=[{"id": "m12", "quote": "Postgres for v1"}],
        askers=[{"session_id": "s1", "task_id": "t1", "title": "Sessions"}],
    )  # fmt: skip
    assert book.store.value("SELECT count(*) FROM decisions") == 0
    assert book.store.value("SELECT count(*) FROM task_decisions") == 0
    assert [d["id"] for d in book.decisions(task_id="t1")] == [made]
    assert book.decisions(task_id="t2") == [] and book.decisions(card_id="c1")[0]["choice"] == (
        "Postgres"
    )
    assert book.decision(made)["status"] == ACTIVE
    assert book.change_decision(made, "owner") and not book.change_decision(made, "owner")
    assert book.decision(made)["status"] == CHANGED
    assert book.decisions(status=ACTIVE) == []


def test_the_owners_acts_are_kept_once_by_kind_and_id(book):
    act = book.owner_act("card", "c1", "Postgres", channel="page", scope={"tasks": ["t1"]})
    assert act["channel"] == "page" and act["scope"] == {"tasks": ["t1"]}
    again = book.owner_act("card", "c1", "MySQL", channel="page")
    assert again["text"] == "Postgres", "the first answer recorded stays"
    assert book.act("card", "c2") is None


def test_tables_made_in_a_transaction_rolled_back_are_known_to_be_gone(tmp_path):
    store = Store(tmp_path / "data")
    book = Logbook(store)

    def made_then_failed():
        with store.transaction():
            book.ensure()
            assert book.exists()
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        made_then_failed()
    assert not book.exists()
    book.ensure()
    assert book.exists() and book.made
