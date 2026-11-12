"""The supervisor another agent's processes run under (agentrun.py): the owner's environment
without Chief of Staff's variables, a process group of the agent's own, and nothing of the agent
left once its CLI exits or Chief of Staff goes away, however it stopped."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from chiefofstaff import agentrun
from chiefofstaff.backend import running

# A stand-in agent CLI: it says who it is, starts a command in a session of its own (as Codex
# runs a command left running, in a terminal of its own), prints a line on its stdout and waits;
# `exit` in its argument: it exits a moment later with code 7, leaving that command behind (what
# it started in the last moment before it exits by itself is not seen: LOOK_EVERY).
CLI = r"""
import json, os, subprocess, sys, time
out = sys.argv[1]
child = subprocess.Popen(["sleep", "300"], start_new_session=True)
env = {k: v for k, v in os.environ.items() if k.startswith(("CHIEFOFSTAFF_", "OWNER_"))}
with open(out, "w") as f:
    json.dump({"pid": os.getpid(), "pgid": os.getpgid(0), "child": child.pid, "env": env}, f)
print("hello from the agent", flush=True)
if "exit" in sys.argv[2:]:
    time.sleep(1)
    sys.exit(7)
time.sleep(300)
"""


def supervise(tmp_path: Path, *extra: str, lifeline: int | None = None, look: str = "0.2", **popen):
    out = tmp_path / "cli.json"
    command = [sys.executable, "-m", "chiefofstaff.agentrun"]
    if lifeline is not None:
        command += ["--lifeline", str(lifeline)]
    command += ["--", sys.executable, "-c", CLI, str(out), *extra]
    env = {
        **os.environ,
        "CHIEFOFSTAFF_AGENTRUN_LOOK": look,
        "CHIEFOFSTAFF_SLACK_USER_TOKEN": "xoxp-secret",
        "OWNER_SETTING": "kept",
    }
    process = subprocess.Popen(
        command,
        env=env,
        stdout=subprocess.PIPE,
        pass_fds=(lifeline,) if lifeline is not None else (),
        **popen,
    )
    return process, out


def started(out: Path) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            return json.loads(out.read_text())
        except (OSError, ValueError):
            time.sleep(0.05)
    raise AssertionError("the agent CLI never started")


def gone(*pids: int, seconds: float = 15) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not any(running(pid) for pid in pids):
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def leftovers():
    """Whatever a failed test leaves running is ended by pid."""
    pids: list[int] = []
    yield pids
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def test_the_agent_gets_the_owners_environment_and_a_process_group_of_its_own(tmp_path, leftovers):
    process, out = supervise(tmp_path)
    leftovers.append(process.pid)
    cli = started(out)
    leftovers += [cli["pid"], cli["child"]]
    assert cli["env"] == {"OWNER_SETTING": "kept"}, "no CHIEFOFSTAFF_* variable"
    assert cli["pgid"] == cli["pid"] != os.getpgid(process.pid)
    assert process.stdout and process.stdout.readline() == b"hello from the agent\n"
    process.terminate()
    assert process.wait(15) is not None
    assert gone(cli["pid"], cli["child"]), "asked to stop, it ends the agent's whole tree"


def test_what_the_agent_just_started_ends_with_it_too(tmp_path, leftovers):
    """Not looked at since it started: the supervisor looks again before it ends the agent."""
    process, out = supervise(tmp_path, look="600")
    leftovers.append(process.pid)
    cli = started(out)
    leftovers += [cli["pid"], cli["child"]]
    process.terminate()
    process.wait(15)
    assert gone(cli["pid"], cli["child"])


def test_when_the_agent_exits_what_it_left_running_ends_with_it(tmp_path, leftovers):
    process, out = supervise(tmp_path, "exit")
    leftovers.append(process.pid)
    cli = started(out)
    leftovers.append(cli["child"])
    assert process.wait(30) == 7, "its exit code is the agent's"
    assert gone(cli["child"])


def test_when_chief_of_staff_goes_away_the_agent_does_too(tmp_path, leftovers):
    read, write = os.pipe()
    process, out = supervise(tmp_path, lifeline=read)
    os.close(read)
    leftovers.append(process.pid)
    cli = started(out)
    leftovers += [cli["pid"], cli["child"]]
    os.close(write)  # As when Chief of Staff's process ends, however it stopped.
    assert process.wait(15) == -signal.SIGTERM or process.returncode is not None
    assert gone(cli["pid"], cli["child"])


def test_orphaned_it_ends_the_agent_even_without_a_lifeline(tmp_path, leftovers):
    """Chief of Staff killed (kill -9) without a lifeline to close: the supervisor, now another
    process's child, ends the agent."""
    out = tmp_path / "cli.json"
    middle = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time\n"
            "p = subprocess.Popen(sys.argv[1:])\n"
            "print(p.pid, flush=True)\n"
            "time.sleep(300)\n",
            sys.executable,
            "-m",
            "chiefofstaff.agentrun",
            "--",
            sys.executable,
            "-c",
            CLI,
            str(out),
        ],
        env={**os.environ, "CHIEFOFSTAFF_AGENTRUN_LOOK": "0.2"},
        stdout=subprocess.PIPE,
    )
    leftovers.append(middle.pid)
    supervisor = int(middle.stdout.readline()) if middle.stdout else 0
    leftovers.append(supervisor)
    cli = started(out)
    leftovers += [cli["pid"], cli["child"]]
    middle.kill()
    middle.wait(10)
    assert gone(supervisor, cli["pid"], cli["child"])


def test_a_command_is_needed():
    with pytest.raises(SystemExit):
        agentrun.main([])


def test_its_own_environment_leaves_out_chief_of_staffs_settings(monkeypatch):
    monkeypatch.setenv("CHIEFOFSTAFF_SLACK_USER_TOKEN", "xoxp-secret")
    monkeypatch.setenv("OWNER_SETTING", "kept")
    env = agentrun.own_environment()
    assert "CHIEFOFSTAFF_SLACK_USER_TOKEN" not in env and env["OWNER_SETTING"] == "kept"


# The same, run in this process (what the subprocesses above do is not measured).


@pytest.fixture
def own_signals():
    """The supervisor takes SIGTERM, SIGINT and SIGHUP: given back to pytest after."""
    kept = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    }
    yield
    for signum, handler in kept.items():
        signal.signal(signum, handler)


def test_in_process_its_exit_code_is_the_agents(tmp_path, monkeypatch, own_signals, leftovers):
    monkeypatch.setattr(agentrun, "LOOK_EVERY", 0.05)
    out = tmp_path / "cli.json"
    assert agentrun.main(["--", sys.executable, "-c", CLI, str(out), "exit"]) == 7
    child = started(out)["child"]
    leftovers.append(child)
    assert gone(child)


def test_in_process_it_ends_the_agent_when_told_or_left(
    tmp_path, monkeypatch, own_signals, leftovers
):
    monkeypatch.setattr(agentrun, "LOOK_EVERY", 0.05)
    out = tmp_path / "cli.json"
    read, write = os.pipe()
    supervisor = agentrun.Supervisor([sys.executable, "-c", CLI, str(out)], read)
    threading.Thread(target=lambda: (started(out), os.close(write)), daemon=True).start()
    assert supervisor.run() == -signal.SIGTERM, "Chief of Staff went away: the agent is ended"
    os.close(read)
    cli = started(out)
    leftovers += [cli["pid"], cli["child"]]
    assert gone(cli["pid"], cli["child"])

    out.unlink()
    supervisor = agentrun.Supervisor([sys.executable, "-c", CLI, str(out)], None)
    threading.Thread(
        target=lambda: (started(out), supervisor.on_signal(signal.SIGTERM, None)), daemon=True
    ).start()
    supervisor.run()
    cli = started(out)
    leftovers += [cli["pid"], cli["child"]]
    assert gone(cli["pid"], cli["child"]), "asked to stop"

    out.unlink()
    parent = os.getppid()
    supervisor = agentrun.Supervisor([sys.executable, "-c", CLI, str(out)], None)

    def orphaned():
        started(out)
        monkeypatch.setattr(os, "getppid", lambda: parent + 1)

    threading.Thread(target=orphaned, daemon=True).start()
    supervisor.run()
    monkeypatch.undo()
    cli = started(out)
    leftovers += [cli["pid"], cli["child"]]
    assert gone(cli["pid"], cli["child"]), "orphaned"


def test_in_process_an_agent_that_will_not_stop_is_killed(
    tmp_path, monkeypatch, own_signals, leftovers
):
    """One that ignores being asked to stop is killed."""
    stubborn = (
        "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(300)\n"
    )
    monkeypatch.setattr(agentrun, "LOOK_EVERY", 0.05)
    supervisor = agentrun.Supervisor([sys.executable, "-c", stubborn], None)
    threading.Timer(1, supervisor.on_signal, (signal.SIGTERM, None)).start()
    assert supervisor.run() == -signal.SIGKILL
