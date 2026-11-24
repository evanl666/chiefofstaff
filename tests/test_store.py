import json
import sqlite3
import threading
from contextlib import closing, suppress

import pytest

from chiefofstaff.store import Store


def test_settings_carry_over_from_the_previous_chiefofstaff(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    legacy = {
        "backend": "copilot",
        "copilot_command": "copilot",
        "model": "auto",
        "project_path": str(project),
        "slack_enabled": True,
        "slack_owner": "U1",
        "github_enabled": True,
        "github_repos": ["Acme/App"],
    }
    with closing(sqlite3.connect(data / "work-buddy.db")) as db:
        db.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO settings VALUES (1, ?)", (json.dumps(legacy),))
        db.commit()

    settings = Store(data).settings()
    assert settings.projects == [str(project.resolve())]
    assert settings.model == ""
    assert settings.github_enabled and settings.github_repos == ["acme/app"]
    assert settings.slack_enabled is False, "the old bot integration is not the MCP one"


def test_a_database_from_before_the_rename_is_kept(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with closing(sqlite3.connect(data / "work-buddy-sessions.db")) as db:
        db.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO kv VALUES ('router_id', 'r1')")
        db.commit()
    store = Store(data)
    assert store.get("router_id") == "r1"
    assert not (data / "work-buddy-sessions.db").exists()


def other_connection(data):
    """A second connection to the same database, as another process would read it."""
    return closing(sqlite3.connect(data / "chiefofstaff.db"))


def test_each_write_commits_on_its_own_outside_a_transaction(tmp_path):
    data = tmp_path / "data"
    store = Store(data)
    store.set("a", "1")
    store.execute("INSERT INTO kv VALUES ('b', '2')")
    store.set_many([("c", "3")])
    with store.lock, store.db:  # The control plane's own blocks (control.py).
        store.db.execute("INSERT INTO kv VALUES ('d', '4')")
    with other_connection(data) as db:
        assert {row[0] for row in db.execute("SELECT key FROM kv")} >= {"a", "b", "c", "d"}


def test_a_transaction_commits_once_at_its_end(tmp_path):
    data = tmp_path / "data"
    store = Store(data)
    with store.transaction():
        store.set("a", "1")
        store.execute("INSERT INTO kv VALUES ('b', '2')")
        store.set_many([("c", "3")], deleted=["a"])
        with store.lock, store.db:
            store.db.execute("INSERT INTO kv VALUES ('d', '4')")
        with other_connection(data) as db:
            assert not db.execute("SELECT key FROM kv WHERE key IN ('b','c','d')").fetchall()
        assert store.get("b") == "2", "its own writes are seen inside it"
    with other_connection(data) as db:
        assert {row[0] for row in db.execute("SELECT key FROM kv")} >= {"b", "c", "d"}
    assert store.get("a") is None


def test_a_transaction_rolls_back_everything_on_an_exception(tmp_path):
    store = Store(tmp_path / "data")
    store.set("kept", "1")

    def changes():
        with store.transaction():
            store.set("a", "1")
            store.delete("kept")
            with store.lock, store.db:
                store.db.execute("INSERT INTO kv VALUES ('d', '4')")
            raise RuntimeError("injected after the change, before the commit")

    with pytest.raises(RuntimeError, match="injected"):
        changes()
    assert store.get("a") is None and store.get("d") is None and store.get("kept") == "1"
    store.set("after", "1")  # The connection is usable again, committing on its own.
    assert store.get("after") == "1"


def test_a_nested_transaction_joins_the_outer_one(tmp_path):
    data = tmp_path / "data"
    store = Store(data)
    with store.transaction():
        store.set("outer", "1")
        with store.transaction():
            store.set("inner", "1")
        with other_connection(data) as db:
            assert not db.execute("SELECT key FROM kv WHERE key='inner'").fetchall()
        # An inner part that fails is undone alone, as a block of its own would be.
        with suppress(ValueError), store.transaction():
            store.set("failed", "1")
            raise ValueError
        with suppress(ValueError), store.lock, store.db:
            store.db.execute("INSERT INTO kv VALUES ('failed2', '1')")
            raise ValueError
    assert store.get("outer") == store.get("inner") == "1"
    assert store.get("failed") is None and store.get("failed2") is None


def test_a_transaction_holds_other_threads_until_it_ends(tmp_path):
    store = Store(tmp_path / "data")
    seen = []
    with store.transaction():
        store.set("a", "1")
        thread = threading.Thread(target=lambda: seen.append(store.get("a")))
        thread.start()
        thread.join(0.2)
        assert thread.is_alive(), "another thread waits for the store's lock"
        store.delete("a")
    thread.join(5)
    assert seen == [None], "it sees only what was committed"
