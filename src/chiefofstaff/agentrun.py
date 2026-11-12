"""The supervisor every process of another coding agent runs under (`python -m
chiefofstaff.agentrun`).

    python -m chiefofstaff.agentrun [--lifeline FD] -- codex app-server ...

It starts the agent's CLI in a process group of its own, with the owner's environment without
Chief of Staff's variables (CHIEFOFSTAFF_*, the Slack token among them), and the CLI talks to
Chief of Staff over the stdin and stdout it inherits. When the CLI exits, or when Chief of Staff
goes away however it stopped (its end of the lifeline pipe closes, or this process is orphaned),
it ends the CLI's whole process tree, commands left running in the background included, as
backend.end_processes does for Copilot's; then it exits with the CLI's code. It never writes to
stdout: that is the CLI's. macOS and Linux only (another agent's workers do not run on Windows
yet: Runtime.agent_block).
"""

from __future__ import annotations

import argparse
import ctypes
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import suppress

from .backend import descendants, end_processes, process_table
from .machine import kill_process_tree

# How often the CLI's process tree is looked at (seconds): what it started is ended with it,
# also once the CLI itself is gone and they were left to another parent. It also looks the
# moment the CLI starts a process (macOS: the kernel says so), so what a CLI that crashes
# started in its last moments is seen too; on Linux what it leaves comes to the supervisor
# (a subreaper); and when the supervisor ends the CLI, it looks first.
LOOK_EVERY = float(os.environ.get("CHIEFOFSTAFF_AGENTRUN_LOOK", "2"))
# prctl(2): the processes a dying descendant leaves come to this one, not to init.
PR_SET_CHILD_SUBREAPER = 36


def adopt_orphans() -> bool:
    """Linux: what the CLI's processes leave when they end comes to the supervisor."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        return ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def fork_watch(pid: int, lifeline: int | None):
    """macOS: a kqueue that wakes when the process starts another or exits, or when the
    lifeline closes; None elsewhere."""
    if sys.platform != "darwin":
        return None
    try:
        queue = select.kqueue()
        events = [
            select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD,
                fflags=select.KQ_NOTE_FORK | select.KQ_NOTE_EXIT,
            )
        ]
        if lifeline is not None:
            events.append(
                select.kevent(lifeline, filter=select.KQ_FILTER_READ, flags=select.KQ_EV_ADD)
            )
        queue.control(events, 0, 0)
    except OSError:
        return None  # It has exited already: nothing more to watch.
    return queue


def own_environment() -> dict[str, str]:
    """The owner's environment, without Chief of Staff's own settings."""
    return {key: value for key, value in os.environ.items() if not key.startswith("CHIEFOFSTAFF_")}


class Supervisor:
    def __init__(self, command: list[str], lifeline: int | None):
        self.command = command
        self.lifeline = lifeline
        self.parent = os.getppid()
        self.process: subprocess.Popen | None = None
        # What the CLI ran, as last seen: pid -> command line (a pid can be reused).
        self.tree: dict[int, str] = {}
        self.ending = False
        self.watch = None
        # The CLI started a process since the last look.
        self.forked = False
        # What the CLI's processes leave comes to this one (Linux).
        self.adopting = False

    def run(self) -> int:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, self.on_signal)
        self.adopting = adopt_orphans()
        self.process = subprocess.Popen(  # noqa: S603  (the agent's CLI, as configured)
            self.command, env=own_environment(), start_new_session=True
        )
        self.watch = fork_watch(self.process.pid, self.lifeline)
        next_look = 0.0
        while (code := self.process.poll()) is None:
            if self.ending or self.gone():
                break
            if self.forked or time.monotonic() >= next_look:
                self.forked = False
                self.look()
                next_look = time.monotonic() + LOOK_EVERY
            self.wait(0.2)
        self.end()
        return code if code is not None else (self.process.poll() or 0)

    def on_signal(self, signum, frame):
        self.ending = True

    def gone(self) -> bool:
        """Chief of Staff went away: its end of the lifeline closed, or this was orphaned."""
        if os.getppid() != self.parent:
            return True
        if self.lifeline is None:
            return False
        try:
            ready, _, _ = select.select([self.lifeline], [], [], 0)
            return bool(ready) and not os.read(self.lifeline, 1)
        except OSError:
            return True

    def wait(self, seconds: float):
        """Sleep, waking when the lifeline closes, or the CLI starts a process or exits."""
        if sys.platform == "darwin" and self.watch is not None:
            with suppress(OSError, InterruptedError):
                for event in self.watch.control(None, 8, seconds):
                    if event.filter == select.KQ_FILTER_PROC and event.fflags & select.KQ_NOTE_FORK:
                        self.forked = True
            return
        if self.lifeline is None:
            time.sleep(seconds)
            return
        with suppress(OSError, InterruptedError):
            select.select([self.lifeline], [], [], seconds)

    def look(self):
        """What the CLI runs now, added to what it ran that still runs (left to another parent
        once what started it ended), and what those started since."""
        if not self.process:
            return
        table = process_table()
        if not table:
            return
        self.tree = {pid: c for pid, c in self.tree.items() if table.get(pid, (0, ""))[1] == c}
        # This process's own (the CLI's, and on Linux what they left), and each known one's;
        # not the `ps` that made the table.
        own = os.getpid()
        for root in {own, *self.tree}:
            self.tree |= {
                pid: table[pid][1]
                for pid in descendants(table, root)
                if pid in table and not (table[pid][0] == own and table[pid][1].startswith("ps "))
            }
        self.reap()

    def reap(self):
        """What came to this process (a subreaper) and ended is let go of (not a zombie)."""
        own = self.process.pid if self.process else 0
        for pid in list(self.tree):
            if pid not in {own, os.getpid()}:
                with suppress(ChildProcessError, OSError):
                    os.waitpid(pid, os.WNOHANG)

    def end(self):
        """The CLI asked to stop, then everything it ran ended (killed when it does not)."""
        # Its processes now, while they are still its own (or, on Linux, come to this one).
        self.look()
        if self.process and self.process.poll() is None:
            with suppress(OSError):
                os.killpg(self.process.pid, signal.SIGTERM)
            with suppress(subprocess.TimeoutExpired):
                self.process.wait(3)
            if self.process.poll() is None:
                with suppress(OSError):
                    kill_process_tree(self.process.pid)
                with suppress(subprocess.TimeoutExpired):
                    self.process.wait(3)
        if self.process:
            self.tree.pop(self.process.pid, None)
        self.tree.pop(os.getpid(), None)
        if self.adopting:
            # What came to it ends without staying a zombie (the CLI's own end was read).
            signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        end_processes(self.tree)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m chiefofstaff.agentrun")
    parser.add_argument("--lifeline", type=int, default=None)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command to run")
    return Supervisor(command, args.lifeline).run()


if __name__ == "__main__":
    sys.exit(main())
