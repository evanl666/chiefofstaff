import asyncio
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows compatibility")


def test_data_directory_lock_is_exclusive(tmp_path):
    from chiefofstaff.__main__ import lock_data_file

    path = tmp_path / "service.lock"
    code = (
        "import sys; from chiefofstaff.__main__ import lock_data_file; "
        "f = open(sys.argv[1], 'a+b'); lock_data_file(f)"
    )
    with path.open("a+b") as lock:
        lock_data_file(lock)
        blocked = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True)
        assert blocked.returncode != 0
    available = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True)
    assert available.returncode == 0, available.stderr


def test_running_checks_process_without_terminating_it():
    from chiefofstaff.backend import process_table, running, start_times

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert running(process.pid)
        assert process.poll() is None
        assert process.pid in process_table()
        assert process.pid in start_times()
    finally:
        process.terminate()
        process.wait(timeout=10)
    assert not running(process.pid)


def test_in_use_recognizes_windows_descendants_and_other_drives(tmp_path):
    from chiefofstaff.machine import in_use

    tree = tmp_path / "worktree"
    assert in_use(str(tree), {str(tree / "nested").swapcase()})
    assert not in_use(str(tree), {str(tmp_path / "worktree-other"), "Z:\\other"})


def test_other_session_log_reads_from_requested_offset(tmp_path):
    import os

    from chiefofstaff.others import read_chunk

    path = tmp_path / "events.jsonl"
    path.write_bytes(b"first\nsecond\n")
    fd = os.open(path, os.O_RDONLY | os.O_BINARY)
    try:
        assert read_chunk(fd, 6, 6) == b"second"
        assert read_chunk(fd, 5, 0) == b"first"
    finally:
        os.close(fd)


def test_send_to_chief_is_explicitly_unavailable(tmp_path, capsys):
    from chiefofstaff import send
    from chiefofstaff.sendsocket import NotOpenedError, prepare

    with pytest.raises(NotOpenedError, match="not supported on Windows"):
        prepare(tmp_path)
    assert send.main(["hello", "--data-dir", str(tmp_path)]) == send.ABSENT
    assert "not supported on Windows" in capsys.readouterr().err


async def test_unknown_windows_process_directory_prevents_cleanup(monkeypatch, tmp_path):
    import psutil

    from chiefofstaff import machine

    known = SimpleNamespace(pid=123, cwd=lambda: str(tmp_path))
    monkeypatch.setattr(psutil, "process_iter", lambda: [known])
    assert await machine.process_cwds() == {str(tmp_path)}

    def denied():
        raise psutil.AccessDenied(pid=124)

    monkeypatch.setattr(
        psutil, "process_iter", lambda: [known, SimpleNamespace(pid=124, cwd=denied)]
    )
    assert await machine.process_cwds() is None


async def test_timed_out_command_stops_its_child(tmp_path):
    import psutil

    from chiefofstaff.workspaces import TimedOutError, command

    pid_file = tmp_path / "child.pid"
    code = (
        "import pathlib, subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)"
    )
    with pytest.raises(TimedOutError):
        await command(sys.executable, "-c", code, str(pid_file), limit=3)
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while psutil.pid_exists(pid) and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.1)
    assert not psutil.pid_exists(pid)
