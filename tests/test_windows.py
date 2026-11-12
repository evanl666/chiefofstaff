"""What Chief of Staff does differently on Windows. Tests of pure logic run everywhere (the
platform is a parameter); those of the operating system itself run where they can."""

import asyncio
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows itself")


def locker(path: Path) -> subprocess.CompletedProcess:
    """Another process trying to take the data directory's lock."""
    code = (
        "import sys; from chiefofstaff.__main__ import lock_data_file; "
        "f = open(sys.argv[1], 'a+b'); lock_data_file(f)"
    )
    return subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True)


def test_the_data_directory_is_locked_by_one_process_at_a_time(tmp_path):
    from chiefofstaff.__main__ import lock_data_file

    path = tmp_path / "service.lock"
    with path.open("a+b") as lock:
        lock_data_file(lock)
        assert locker(path).returncode != 0, "a second Chief of Staff is turned away"
        assert path.read_bytes() == b"", "the lock file stays readable"
    assert locker(path).returncode == 0, "free once the first one stops"


@windows
def test_the_data_directory_and_launch_link_are_the_owners_alone(tmp_path):
    from chiefofstaff.__main__ import make_private, write_private

    def readers(path: Path) -> tuple[set[str], bool]:
        """Who its access control list lets in (SIDs; SY and BA for SYSTEM and Administrators,
        as SDDL writes them), and whether it inherits nothing ("D:P")."""
        saved = tmp_path / "acl.txt"
        subprocess.run(["icacls", str(path), "/save", str(saved)], capture_output=True, check=True)
        sddl = saved.read_bytes().decode("utf-16-le").splitlines()[1]
        saved.unlink()
        return set(re.findall(r"\(A;[^;]*;[^;]*;;;([^)]+)\)", sddl)), sddl.startswith("D:P")

    me = (
        subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True)
        .stdout.strip().rsplit(",", 1)[-1].strip('"')
    )  # fmt: skip
    allowed = {me, "SY", "BA"}
    folder = tmp_path / "data"
    folder.mkdir()
    make_private(folder, folder=True)
    launch = folder / "launch-url"
    write_private(launch, "http://127.0.0.1:8765/#token=secret")
    for path in (folder, launch):
        sids, protected = readers(path)
        assert protected, f"{path} inherits nothing from the folder it is in"
        assert me in sids and sids <= allowed, (path, sids)
    assert launch.read_text() == "http://127.0.0.1:8765/#token=secret"


@windows
def test_running_checks_a_process_without_ending_it():
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


@windows
def test_a_program_in_a_folder_is_seen_whatever_the_case_or_slashes(tmp_path):
    from chiefofstaff.machine import in_use

    tree = tmp_path / "Worktree"
    assert in_use(str(tree), {str(tree / "nested").swapcase()})
    assert in_use(str(tree) + "\\", {str(tree).replace("\\", "/")})
    assert not in_use(str(tree), {str(tmp_path / "Worktree-other"), "Z:\\other"})


@windows
async def test_a_program_running_in_a_worktree_is_found_as_a_normal_user(tmp_path):
    """The real scan: the user's own programs' folders are read; those it may not read
    (administrator, other users, the system) are left out rather than making all unknown."""
    from chiefofstaff import machine

    folder = tmp_path / "tree" / "web"
    folder.mkdir(parents=True)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)", cwd=folder
    )
    try:
        await asyncio.sleep(0.3)
        cwds = await machine.process_cwds()
        assert cwds is not None
        assert machine.in_use(str(tmp_path / "tree"), cwds)
        assert not machine.in_use(str(tmp_path / "elsewhere"), cwds)
    finally:
        process.kill()
        await process.wait()


@windows
async def test_programs_it_may_not_read_are_left_out(monkeypatch, tmp_path):
    import psutil

    from chiefofstaff import machine

    def denied():
        raise psutil.AccessDenied(pid=124)

    def gone():
        raise psutil.NoSuchProcess(pid=125)

    listed = [
        SimpleNamespace(pid=123, cwd=lambda: str(tmp_path)),
        SimpleNamespace(pid=124, cwd=denied),
        SimpleNamespace(pid=125, cwd=gone),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda: iter(listed))
    assert await machine.process_cwds() == {str(tmp_path)}

    def unlistable():
        raise psutil.Error("no process list")

    monkeypatch.setattr(psutil, "process_iter", unlistable)
    assert await machine.process_cwds() is None, "unknown: every folder may be in use"


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
    deadline = time.monotonic() + 5
    while psutil.pid_exists(pid) and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.1)
    assert not psutil.pid_exists(pid)


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


@pytest.mark.parametrize(
    ("folder", "launcher", "platform", "expected"),
    [
        ("/work/app", "/usr/bin/code", "linux", ["code", "/work/app"]),
        ("/work/app", None, "darwin", ["open", "-a", "Visual Studio Code", "/work/app"]),
        ("C:\\a b\\app", "C:\\VS Code\\bin\\code.cmd", "win32",
         ["C:\\VS Code\\bin\\code.cmd", "C:\\a b\\app"]),
        ("C:\\a&b\\app", "C:\\VS Code\\bin\\code.exe", "win32",
         ["C:\\VS Code\\bin\\code.exe", "C:\\a&b\\app"]),
    ],
)  # fmt: skip
def test_vs_code_is_given_the_folder_as_one_argument(folder, launcher, platform, expected):
    from chiefofstaff.runtime import vscode_command

    assert vscode_command(folder, launcher, platform) == expected


@pytest.mark.parametrize("folder", ["C:\\a&calc\\app", "C:\\100%PATH%", "C:\\x^y", "C:\\hi!"])
def test_no_path_cmd_exe_would_misread_goes_to_a_cmd_launcher(folder):
    from chiefofstaff.runtime import vscode_command

    with pytest.raises(RuntimeError, match="File Explorer"):
        vscode_command(folder, "C:\\VS Code\\bin\\code.cmd", "win32")
    with pytest.raises(RuntimeError, match="No app"):
        vscode_command("/work", None, "linux")


@pytest.mark.parametrize(
    ("repo", "platform", "local"),
    [
        ("/home/me/app", "linux", True),
        ("~/app", "linux", True),
        ("acme/app", "linux", False),
        ("git@github.com:acme/app.git", "linux", False),
        ("C:\\code\\app", "win32", True),
        ("C:/code/app", "win32", True),
        ("\\\\server\\share\\app", "win32", True),
        ("~\\code\\app", "win32", True),
        ("acme/app", "win32", False),
        ("https://github.com/acme/app.git", "win32", False),
        ("git@github.com:acme/app.git", "win32", False),
    ],
)
def test_add_repo_tells_a_folder_from_a_repository_to_clone(repo, platform, local):
    from chiefofstaff.runtime import local_repository

    assert (local_repository(repo, platform) is not None) == local


@pytest.mark.parametrize("repo", ["\\code\\app", "/code/app", "C:code\\app"])
def test_a_windows_path_without_its_drive_is_refused(repo):
    from chiefofstaff.runtime import local_repository

    with pytest.raises(ValueError, match="with its drive"):
        local_repository(repo, "win32")
