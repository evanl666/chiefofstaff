"""What Chief of Staff does differently on Windows. Tests of its logic run everywhere; those of
Windows itself run there."""

import asyncio
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from chiefofstaff import machine
from chiefofstaff.__main__ import lock_data_file
from chiefofstaff.runtime import Runtime

windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows itself")


def test_the_data_directory_is_locked_by_one_process_at_a_time(tmp_path):
    path = tmp_path / "service.lock"
    code = (
        "import sys; from chiefofstaff.__main__ import lock_data_file; "
        "lock_data_file(open(sys.argv[1], 'a'))"
    )

    def second() -> int:
        return subprocess.run(
            [sys.executable, "-c", code, str(path)], capture_output=True
        ).returncode

    with path.open("a") as lock:
        lock_data_file(lock)
        assert second() != 0, "a second Chief of Staff is turned away"
    assert second() == 0, "free once the first one stops"


@windows
def test_a_new_data_directory_and_its_launch_link_are_the_owners_alone(tmp_path):
    from chiefofstaff.__main__ import make_private

    def readers(path: Path) -> tuple[set[str], bool]:
        """Who its access control list lets in (SY, BA: SYSTEM and Administrators in SDDL),
        and whether it inherits nothing from the folder it is in ("D:P")."""
        saved = tmp_path / "acl.txt"
        subprocess.run(["icacls", str(path), "/save", str(saved)], capture_output=True, check=True)
        sddl = saved.read_bytes().decode("utf-16-le").splitlines()[1]
        saved.unlink()
        return set(re.findall(r"\(A;[^;]*;[^;]*;;;([^)]+)\)", sddl)), sddl.startswith("D:P")

    user = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True)
    me = user.stdout.strip().rsplit(",", 1)[-1].strip('"')
    # SDDL names the built-in Administrator account (RID 500, CI runners' user) LA.
    mine = {me, "LA"} if me.endswith("-500") else {me}
    # OW (Owner Rights) only reaches whoever owns it: this user, as with POSIX's owner bits.
    allowed = mine | {"SY", "BA", "OW"}
    folder = tmp_path / "data"
    folder.mkdir()
    make_private(folder)
    (folder / "launch-url").write_text("http://127.0.0.1:8765/#token=secret")
    sids, protected = readers(folder)
    assert protected and sids & mine and sids <= allowed, sids
    assert readers(folder / "launch-url")[0] <= allowed, "it inherits the folder's"


@windows
def test_running_checks_a_process_without_ending_it():
    from chiefofstaff.backend import process_table, running, start_times

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert running(process.pid)
        assert process.pid in process_table() and process.pid in start_times()
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=10)
    assert not running(process.pid)


def test_a_program_in_a_folder_is_seen_whatever_the_case_or_slashes(tmp_path):
    tree = tmp_path / "Worktree"
    assert machine.in_use(str(tree), {str(tree / "nested")})
    assert not machine.in_use(str(tree), {str(tmp_path / "Worktree-other")})
    if sys.platform == "win32":
        assert machine.in_use(str(tree) + "\\", {str(tree / "nested").swapcase()})
        assert machine.in_use(str(tree), {str(tree).replace("\\", "/")})


@windows
async def test_a_program_running_in_a_worktree_is_found_as_a_normal_user(tmp_path):
    """The real scan: this user's programs are read; those it may not read (administrators',
    other users', the system's) are left out rather than making every folder unknown."""
    folder = tmp_path / "tree" / "web"
    folder.mkdir(parents=True)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)", cwd=folder
    )
    try:
        await asyncio.sleep(0.3)
        cwds = await machine.process_cwds()
        assert cwds is not None and machine.in_use(str(tmp_path / "tree"), cwds)
        assert not machine.in_use(str(tmp_path / "elsewhere"), cwds)
    finally:
        process.kill()
        await process.wait()


@windows
async def test_a_timed_out_command_is_stopped_with_what_it_started(tmp_path):
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
    for _ in range(50):
        if not psutil.pid_exists(pid):
            break
        await asyncio.sleep(0.1)
    assert not psutil.pid_exists(pid)


def test_logs_are_read_from_where_asked(tmp_path):
    from chiefofstaff.others import read_chunk

    path = tmp_path / "events.jsonl"
    path.write_bytes(b"first\r\nsecond\n")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        assert read_chunk(fd, 6, 7) == b"second"
        assert read_chunk(fd, 7, 0) == b"first\r\n", "bytes as written: no newline translation"
    finally:
        os.close(fd)


@windows
def test_send_to_chief_says_it_is_not_available(tmp_path, capsys):
    from chiefofstaff import send
    from chiefofstaff.sendsocket import NotOpenedError, prepare

    with pytest.raises(NotOpenedError, match="not supported on Windows"):
        prepare(tmp_path)
    assert send.main(["hello", "--data-dir", str(tmp_path)]) == send.ABSENT
    assert "not supported on Windows" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("app", "opened"),
    [("finder", "C:/a & b"), ("vscode", "vscode://file/C%3A/a%20%26%20b")],
)
async def test_windows_opens_a_folder_through_the_shell(monkeypatch, tmp_path, app, opened):
    """File Explorer, or VS Code by its URL: never through code.cmd, which cmd.exe would run."""
    started = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os.path, "isdir", lambda path: True)
    monkeypatch.setattr(os, "startfile", started.append, raising=False)
    owner = SimpleNamespace(store=SimpleNamespace(session=lambda _: {"cwd": "C:/a & b"}))
    assert await Runtime.open_folder(owner, "s", app) == {"ok": True}  # type: ignore[arg-type]
    assert started == [opened]


async def test_chiefs_own_git_runs_none_of_a_repositorys_hooks(tmp_path):
    """SAFE_GIT points core.hooksPath at /dev/null; Git for Windows reads that as no folder,
    not one at the drive's root."""
    from chiefofstaff.workspaces import git

    repo = tmp_path / "repo"
    await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(repo)], check=True)
    ran = tmp_path / "ran"
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text(f'#!/bin/sh\necho ran > "{ran.as_posix()}"\n', newline="\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    (repo / "a.txt").write_text("a")
    identity = ("-c", "user.name=t", "-c", "user.email=t@t")
    await git("-C", str(repo), "add", "a.txt")
    await git("-C", str(repo), *identity, "commit", "-qm", "a")
    assert not ran.exists(), "the repository's hook did not run"
    plain = ["git", "-C", str(repo), *identity, "commit", "-q", "--allow-empty", "-m", "b"]
    await asyncio.to_thread(subprocess.run, plain, check=True)
    assert ran.exists(), "the hook itself works: git without SAFE_GIT runs it"


@windows
def test_a_native_hook_planted_where_dev_null_would_point_never_runs(tmp_path):
    """Git for Windows can read /dev/null as <drive>:\\dev\\null, a folder any user may make
    at a drive's root, and runs a hook there with .exe added: Chief's git never looks there."""
    from chiefofstaff.workspaces import git

    root = tmp_path / "drive"
    (root / "dev" / "null").mkdir(parents=True)
    hostname = Path(os.environ["SYSTEMROOT"], "System32", "hostname.exe")
    shutil.copy(hostname, root / "dev" / "null" / "post-commit.exe")
    drive = next(f"{c}:" for c in "QRSTUVWXYZ" if not Path(f"{c}:\\").exists())
    subprocess.run(["subst", drive, str(root)], check=True)
    try:
        repo = Path(drive + "\\", "repo")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "a.txt").write_text("a")
        trace = tmp_path / "trace.txt"
        identity = ("-c", "user.name=t", "-c", "user.email=t@t")
        os.environ["GIT_TRACE"] = str(trace)
        try:
            asyncio.run(git("-C", str(repo), "add", "a.txt"))
            asyncio.run(git("-C", str(repo), *identity, "commit", "-qm", "a"))
            assert "post-commit" not in trace.read_text(), "Chief's git ran the planted hook"
            planted = f"core.hooksPath={drive}/dev/null"
            again = ["commit", "-q", "--allow-empty", "-m", "b"]
            subprocess.run(["git", "-C", str(repo), "-c", planted, *identity, *again], check=True)
            assert "post-commit.exe" in trace.read_text(), "the planted hook itself runs"
        finally:
            del os.environ["GIT_TRACE"]
    finally:
        subprocess.run(["subst", drive, "/d"], check=False)


@windows
def test_no_other_socket_can_share_the_port_chief_listens_on(tmp_path):
    """On Windows SO_REUSEADDR would let another user's socket bind the same port and take
    the page with its token: Chief binds it exclusively."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    chief = subprocess.Popen(
        [sys.executable, "-m", "chiefofstaff", "--demo", "--no-browser", "--port", str(port),
         "--data-dir", str(tmp_path / "data")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        for _ in range(300):
            if (tmp_path / "data" / "launch-url").exists():
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Chief of Staff did not start")
        with socket.socket() as other:
            other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(PermissionError):  # WSAEACCES
                other.bind(("127.0.0.1", port))
    finally:
        chief.kill()
        chief.wait()
