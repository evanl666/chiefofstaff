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
import uuid
from pathlib import Path

import pytest

from chiefofstaff import agentrun
from chiefofstaff.backend import process_table, running

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


# One that crashes a moment after it starts a command (after the supervisor's first look).
CRASHING = r"""
import json, os, signal, subprocess, sys, time
time.sleep(0.5)
late = subprocess.Popen(["sleep", "301"], start_new_session=True)
with open(sys.argv[1], "w") as f:
    json.dump({"pid": os.getpid(), "child": late.pid}, f)
time.sleep(0.3)
os.kill(os.getpid(), signal.SIGKILL)
"""


# One whose command starts another later (a dev server's own process), then crashes: its command
# was seen, what that started since was not.
STARTING_LATER = r"""
import json, os, signal, subprocess, sys, time
shell = subprocess.Popen(["sh", "-c", "sleep 0.6; sleep 302"], start_new_session=True)
with open(sys.argv[1], "w") as f:
    json.dump({"pid": os.getpid(), "child": shell.pid}, f)
time.sleep(1.2)
os.kill(os.getpid(), signal.SIGKILL)
"""


# One whose command, in a session of its own, puts a server in the background with nohup and
# exits (the server is another parent's from then on), as a worker starting a dev server does.
NOHUP = r"""
import json, os, subprocess, sys, time
shell = subprocess.Popen(
    ["sh", "-c", "sleep 0.5; nohup sleep 3031 >/dev/null 2>&1 &"], start_new_session=True
)
with open(sys.argv[1], "w") as f:
    json.dump({"pid": os.getpid(), "child": shell.pid}, f)
time.sleep(300)
"""

# One whose command starts a daemon: it forks, the child makes a session of its own and forks
# the daemon, then exits (the daemon is another parent's, in another group). The child lives a
# moment, as the kernel's word of its fork is read: one that exits at once can be missed on
# macOS, a known limit (SECURITY.md), so it is not what this checks.
DAEMON = r"""
import json, os, subprocess, sys, time
script = (
    "import os, time\n"
    "time.sleep(0.5)\n"
    "if os.fork() == 0:\n"
    "    os.setsid()\n"
    "    if os.fork() == 0:\n"
    "        os.execvp('sleep', ['sleep', '3032'])\n"
    "    time.sleep(0.5)\n"
    "    os._exit(0)\n"
    "os.wait()\n"
)
command = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
with open(sys.argv[1], "w") as f:
    json.dump({"pid": os.getpid(), "child": command.pid}, f)
time.sleep(300)
"""


# One that forks many short commands one after another, as a build or a test run does.
FORKING = r"""
import json, os, subprocess, sys
with open(sys.argv[1], "w") as f:
    json.dump({"pid": os.getpid(), "child": 0}, f)
for _ in range(400):
    subprocess.run(["/usr/bin/true"])
"""


@pytest.fixture(autouse=True)
def no_subreaper(monkeypatch):
    """In this process (pytest's), the supervisor does not take what others leave."""
    monkeypatch.setattr(agentrun, "adopt_orphans", lambda: False)


def supervise(
    tmp_path: Path,
    *extra: str,
    lifeline: int | None = None,
    look: str = "0.2",
    cli: str = CLI,
    path_first: Path | None = None,
    **popen,
):
    out = tmp_path / "cli.json"
    command = [sys.executable, "-m", "chiefofstaff.agentrun"]
    if lifeline is not None:
        command += ["--lifeline", str(lifeline)]
    command += ["--", sys.executable, "-c", cli, str(out), *extra]
    env = {
        **os.environ,
        "CHIEFOFSTAFF_AGENTRUN_LOOK": look,
        "CHIEFOFSTAFF_SLACK_USER_TOKEN": "xoxp-secret",
        "OWNER_SETTING": "kept",
    }
    if path_first is not None:
        env["PATH"] = f"{path_first}{os.pathsep}{env.get('PATH', '')}"
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


def test_what_a_crashing_agent_started_in_its_last_moments_ends_too(tmp_path, leftovers):
    """Review: a command started within the last look's interval before the CLI crashed was
    left running. The supervisor looks the moment the CLI starts a process (macOS), or takes
    what the CLI leaves (Linux)."""
    process, out = supervise(tmp_path, look="600", cli=CRASHING)
    leftovers.append(process.pid)
    cli = started(out)
    leftovers.append(cli["child"])
    assert process.wait(30) == -signal.SIGKILL % 256, "it ended as the agent did"
    assert gone(cli["child"])


def own(seconds: str) -> str:
    """A command's duration no other test run's has (runs side by side see each other's
    processes): its own processes are told apart by it."""
    return f"{seconds}.{uuid.uuid4().int % 10**6:06d}"


def test_what_its_commands_started_since_they_were_seen_ends_too(tmp_path, leftovers):
    late_one = own("302")
    process, out = supervise(
        tmp_path, look="600", cli=STARTING_LATER.replace("sleep 302", f"sleep {late_one}")
    )
    leftovers.append(process.pid)
    cli = started(out)
    leftovers.append(cli["child"])
    process.wait(30)
    late = [pid for pid, (_, line) in process_table().items() if line == f"sleep {late_one}"]
    leftovers += late
    assert gone(cli["child"]) and not late, "the shell, and the server it started since"


@pytest.mark.parametrize(("cli", "seconds"), [(NOHUP, "3031"), (DAEMON, "3032")])
def test_what_a_command_left_to_another_parent_ends_too(tmp_path, leftovers, cli, seconds):
    """Review: `nohup server &` from a shell that exited, or a daemon that forked into a session
    of its own, is another parent's before the supervisor looks again: it still ends with the
    agent once Chief of Staff goes away."""
    mine = own(seconds)
    cli, left = cli.replace(seconds, mine), f"sleep {mine}"
    read, write = os.pipe()
    process, out = supervise(tmp_path, lifeline=read, cli=cli)
    os.close(read)
    leftovers.append(process.pid)
    cli_ = started(out)
    leftovers += [cli_["pid"], cli_["child"]]
    deadline = time.monotonic() + 15
    while not (found := [p for p, (_, line) in process_table().items() if line == left]):
        assert time.monotonic() < deadline, "it never started"
        time.sleep(0.1)
    leftovers += found
    # Its own parent gone, it is another's: init's (macOS) or the supervisor's (Linux, where
    # the supervisor is a subreaper).
    others = {1, process.pid}
    while not all(process_table().get(pid, (1, ""))[0] in others for pid in found):
        assert time.monotonic() < deadline, "another parent's now"
        time.sleep(0.1)
    os.close(write)  # As when Chief of Staff's process ends, however it stopped.
    process.wait(15)
    assert gone(*found, seconds=10), "nothing of the agent is left"


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


def test_it_stays_light_without_the_copilot_sdk():
    """One supervisor runs per Codex worker: it imports nothing of Copilot's SDK (25 MB rather
    than 80, and no wait for that import as each worker starts)."""
    loaded = subprocess.run(
        [sys.executable, "-c", "import sys, chiefofstaff.agentrun; print(sorted(sys.modules))"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "'copilot'" not in loaded and "'chiefofstaff.backend'" not in loaded


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


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS: the kernel says each fork")
def test_a_storm_of_forks_is_no_storm_of_looks(tmp_path, leftovers):
    """Review (performance): each fork woke a look, which runs `ps`: a build used about a core
    per worker. Looks on forks come at most once a second; each fork is followed in between."""
    shim, calls = tmp_path / "bin", tmp_path / "ps-calls"
    shim.mkdir()
    (shim / "ps").write_text(f'#!/bin/sh\necho look >> "{calls}"\nexec /bin/ps "$@"\n')
    (shim / "ps").chmod(0o755)
    began = time.monotonic()
    process, out = supervise(tmp_path, look="600", cli=FORKING, path_first=shim)
    leftovers.append(process.pid)
    started(out)
    assert process.wait(120) == 0
    took = time.monotonic() - began
    looks = len(calls.read_text().splitlines()) if calls.exists() else 0
    assert 1 <= looks <= 3 + int(took / agentrun.FORK_LOOK_EVERY), (looks, took)


def test_a_group_followed_since_the_table_was_made_is_kept():
    """A group the kernel's word of a fork added after `ps` ran is not in its table: it is
    kept (it was let go of, and what ran in it outlived the agent); one known before that the
    table does not show is let go of."""
    supervisor = agentrun.Supervisor(["true"], None)
    supervisor.groups = {990001, 990002}
    supervisor.in_groups({1: (0, "launchd")}, {1: 1}, known={990001})
    assert supervisor.groups == {990002}
