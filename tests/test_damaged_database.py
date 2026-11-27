"""A database Chief of Staff can't use stops its start with one plain line (what is wrong, its
file, what to do) and no traceback, as a second instance's start does; an emptied one is never
taken for a first run.

Live, on 0.1.61 (QA R4): a corrupt database ended the start with a traceback ending "sqlite3.
DatabaseError: database disk image is malformed", one held by another program with "database is
locked", and a 0-byte one in a folder full of earlier runs started a new, empty Chief of Staff
without a word."""

import os
import sqlite3
import subprocess
import sys

import pytest

from chiefofstaff import store as store_module
from chiefofstaff.store import DB_NAME, Store

# (Looked up as each test runs: one that fails on a release without it.)
PROBLEM = getattr(store_module, "DatabaseProblemError", sqlite3.Error)


def start(data) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "chiefofstaff", "--no-browser", "--port", "23101",
         "--data-dir", str(data)],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "CHIEFOFSTAFF_UPDATE_CHECK": "0"},
    )  # fmt: skip


@pytest.mark.parametrize("damage", ["corrupt", "locked"])
def test_r4_a_damaged_or_locked_database_is_said_plainly(tmp_path, damage):
    """Started on a data folder whose database is corrupt (or held by another program): the
    start ends with a plain line saying what is wrong and what to do (its file, a backup, the
    program holding it), as a second instance does."""
    data = tmp_path / "data"
    data.mkdir()
    db = data / DB_NAME
    holder = None
    if damage == "corrupt":
        db.write_bytes(b"SQLite format 3\x00" + os.urandom(8192))
    else:
        sqlite3.connect(db).execute("CREATE TABLE t (x)").connection.close()
        holder = subprocess.Popen(
            [sys.executable, "-c", "import sqlite3,sys,time; d=sqlite3.connect(sys.argv[1], "
             "isolation_level=None); d.execute('BEGIN EXCLUSIVE'); print('ok', flush=True); "
             "time.sleep(60)", str(db)],
            stdout=subprocess.PIPE, text=True,
        )  # fmt: skip
        holder.stdout.readline()
    try:
        done = start(data)
    finally:
        if holder:
            holder.kill()
            holder.wait()
    assert done.returncode != 0
    assert "Traceback" not in done.stderr, done.stderr[-400:]
    said = done.stderr.strip().splitlines()[-1]
    assert str(db) in said
    if damage == "corrupt":
        assert "is damaged" in said and "Restore it from a backup" in said
    else:
        assert said.startswith("Another program holds Chief of Staff's database")


def test_an_emptied_database_is_not_taken_for_a_first_run(tmp_path):
    """A 0-byte database in a folder that holds what earlier runs made (its workspaces): said,
    and left as it is; never a new, empty Chief of Staff in its place. (Started, its start ends
    with that line, as for a damaged one above.)"""
    data = tmp_path / "data"
    (data / "work").mkdir(parents=True)
    (data / DB_NAME).write_bytes(b"")
    with pytest.raises(PROBLEM) as said:
        Store(data)
    assert str(said.value).startswith(
        f"Chief of Staff's database ({data / DB_NAME}) is empty (0 bytes), but its folder"
    )
    assert (data / DB_NAME).stat().st_size == 0, "left as it was"


def test_a_first_start_cut_short_opens_as_a_first_run(tmp_path):
    """A 0-byte database alone with what a start makes first (a first start cut short before it
    wrote anything): a first run, opened as ever."""
    first = tmp_path / "first"
    first.mkdir()
    (first / DB_NAME).write_bytes(b"")
    (first / "service.lock").write_text("")
    store = Store(first)
    store.set("router_id", "r1")
    store.close()
    assert Store(first).get("router_id") == "r1"


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX modes, not root")
def test_a_database_it_cannot_write_is_said_plainly(tmp_path):
    data = tmp_path / "data"
    Store(data).close()
    (data / DB_NAME).chmod(0o400)
    try:
        with pytest.raises(PROBLEM, match="can't write its database"):
            Store(data)
    finally:
        (data / DB_NAME).chmod(0o600)
