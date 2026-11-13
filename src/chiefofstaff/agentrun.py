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

What a command left behind may be another parent's by then: a shell that ran `nohup server &`
and exited, or a daemon that forked into a session of its own and let its first child exit. On
Linux they come to the supervisor (a subreaper). On macOS it watches every process of the tree
(kqueue: each one's forks and exit), and takes the process groups it saw the tree in, and
those its processes made (a group's id is the pid of the process that made it), as the
agent's: what runs in them ends with it.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import select
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress

# Not the backend module: the supervisor of each worker's process stays light (no Copilot SDK).
from .machine import kill_process_tree
from .processes import descendants, end_processes, process_table

# How often the CLI's process tree is looked at (seconds): what it started is ended with it,
# also once the CLI itself is gone and they were left to another parent. It also looks the
# moment the CLI starts a process (macOS: the kernel says so), so what a CLI that crashes
# started in its last moments is seen too; on Linux what it leaves comes to the supervisor
# (a subreaper); and when the supervisor ends the CLI, it looks first.
LOOK_EVERY = float(os.environ.get("CHIEFOFSTAFF_AGENTRUN_LOOK", "2"))
# At most this often for a fork (a build or a test run forks thousands of times, and a look runs
# `ps`): in between, the kernel's word of each fork is followed (`follow`, proc_listpids), and a
# look always comes first when the CLI ends.
FORK_LOOK_EVERY = 1.0
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
        events = [proc_event(pid)]
        if lifeline is not None:
            events.append(
                select.kevent(lifeline, filter=select.KQ_FILTER_READ, flags=select.KQ_EV_ADD)
            )
        queue.control(events, 0, 0)
    except OSError:
        return None  # It has exited already: nothing more to watch.
    return queue


def proc_event(pid: int):
    """A process's forks and exit, as a kqueue event (macOS)."""
    if sys.platform != "darwin":
        raise OSError("macOS only")
    return select.kevent(
        pid,
        filter=select.KQ_FILTER_PROC,
        flags=select.KQ_EV_ADD,
        fflags=select.KQ_NOTE_FORK | select.KQ_NOTE_EXIT,
    )


def children(pid: int) -> list[int]:
    """macOS: the processes a process started that run now, asked of the kernel at once (a
    command's first child may live only for a moment)."""
    buffer = (ctypes.c_int * 256)()
    try:
        used = ctypes.CDLL(None, use_errno=True).proc_listpids(
            6,
            pid,
            buffer,
            ctypes.sizeof(buffer),  # PROC_PPID_ONLY
        )
    except (OSError, AttributeError):
        return []
    return [found for found in buffer[: max(used, 0) // ctypes.sizeof(ctypes.c_int)] if found]


def process_groups() -> tuple[dict[int, tuple[int, str]], dict[int, int]]:
    """Every process (as process_table has it) and its group: pid -> process group id. One
    `ps` for both."""
    try:
        listed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid=,command="],  # noqa: S607  (a fixed command)
            capture_output=True,
            text=True,
            errors="replace",
            check=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}, {}
    table, groups = {}, {}
    for line in listed.splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3 and all(part.isdigit() for part in parts[:3]):
            pid = int(parts[0])
            table[pid] = (int(parts[1]), parts[3] if len(parts) > 3 else "")
            groups[pid] = int(parts[2])
    return table, groups


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
        # macOS: the processes whose forks and exit wake it, and the process groups taken as
        # the agent's: those the tree was seen in, and those its processes may have made (each
        # pid seen; a group's id is its maker's pid). One is let go of once no process has it
        # (nor runs with that pid), so its number is never another's while it is kept.
        self.watched: set[int] = set()
        self.groups: set[int] = set()
        # macOS: the kqueue is read by a thread of its own, so a fork is followed the moment it
        # happens (not after a look in progress); it wakes the main loop.
        self.lock = threading.Lock()
        self.woken = threading.Event()
        self.watcher: threading.Thread | None = None

    def run(self) -> int:
        if sys.platform == "win32":
            raise RuntimeError("Another agent's workers need macOS or Linux for now.")
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, self.on_signal)
        self.adopting = adopt_orphans()
        self.process = subprocess.Popen(  # noqa: S603  (the agent's CLI, as configured)
            self.command, env=own_environment(), start_new_session=True
        )
        self.watch = fork_watch(self.process.pid, self.lifeline)
        if self.watch is not None:
            self.watched.add(self.process.pid)
            self.watcher = threading.Thread(target=self.watching, daemon=True)
            self.watcher.start()
        next_look, looked = 0.0, float("-inf")
        while (code := self.process.poll()) is None:
            if self.ending or self.gone():
                break
            now = time.monotonic()
            if now >= next_look or (self.forked and now - looked >= FORK_LOOK_EVERY):
                self.forked = False
                self.look()
                looked = time.monotonic()
                next_look = looked + LOOK_EVERY
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
        """Sleep, waking when the lifeline closes, or a process of the tree starts another or
        exits."""
        if self.watcher is not None:
            self.woken.wait(seconds)
            self.woken.clear()
            return
        if self.lifeline is None:
            time.sleep(seconds)
            return
        with suppress(OSError, InterruptedError):
            select.select([self.lifeline], [], [], seconds)

    def watching(self):
        """macOS: what the kqueue says, as it happens, until the supervisor ends."""
        if sys.platform != "darwin":
            return
        while self.watch is not None and not self.ending:
            try:
                events = self.watch.control(None, 64, 0.5)
            except InterruptedError:
                continue
            except OSError:
                return
            for event in events:
                if event.filter == select.KQ_FILTER_PROC and event.fflags & select.KQ_NOTE_FORK:
                    self.follow(event.ident)
                    self.forked = True
                self.woken.set()

    def follow(self, pid: int):
        """macOS: a process of the tree started another: it (and what that started already) is
        watched at once, before it can leave the tree (a daemon's first child, which makes a
        session of its own and lets its own child go)."""
        todo, own = [pid], os.getpid()
        while todo and len(self.watched) < 4096:
            for child in children(todo.pop()):
                if child != own and child not in self.watched:
                    with self.lock:
                        self.groups.add(child)
                    self.add_watch(child)
                    todo.append(child)

    def add_watch(self, pid: int):
        if self.watch is None or pid in self.watched or pid == os.getpid():
            return
        self.watched.add(pid)
        with suppress(OSError):
            self.watch.control([proc_event(pid)], 0, 0)

    def look(self):
        """What the CLI runs now, added to what it ran that still runs (left to another parent
        once what started it ended), and what those started since. (What it asks the system
        is asked outside its lock: a fork is followed meanwhile.)"""
        if not self.process:
            return
        with self.lock:
            known = set(self.groups)  # What was followed since may not be in the table yet.
        table, groups = process_groups() if self.watch is not None else (process_table(), {})
        if not table:
            return
        with self.lock:
            self.tree = {pid: c for pid, c in self.tree.items() if table.get(pid, (0, ""))[1] == c}
            # This process's own (the CLI's, and on Linux what they left), and each known
            # one's; not the `ps` that made the table.
            own = os.getpid()
            for root in {own, *self.tree}:
                self.tree |= {
                    pid: table[pid][1]
                    for pid in descendants(table, root)
                    if pid in table
                    and not (table[pid][0] == own and table[pid][1].startswith("ps "))
                }
            if groups:
                self.in_groups(table, groups, known)
        for pid in list(self.tree):
            self.add_watch(pid)
        self.reap()

    def in_groups(self, table: dict[int, tuple[int, str]], groups: dict[int, int], known: set[int]):
        """macOS: the tree's process groups (and those its processes may have made) and what
        runs in them, though no longer the tree's. A group let go of is one known before the
        table was made (`known`) that it does not show: one followed since may be newer than
        the table."""
        if sys.platform == "win32":
            return
        own = os.getpid()
        theirs = {os.getpgrp(), 0, 1}  # Chief of Staff's group (this one's), and the system's.
        self.groups |= {pid for pid in self.tree if pid != own}
        self.groups |= {groups[pid] for pid in self.tree if pid in groups and pid != own}
        self.groups = {
            g for g in self.groups - theirs if g not in known or g in table or g in groups.values()
        }
        self.tree |= {
            pid: table[pid][1]
            for pid, group in groups.items()
            if group in self.groups and pid in table and pid != own
        }

    def reap(self):
        """What came to this process (a subreaper) and ended is let go of (not a zombie)."""
        if sys.platform == "win32":
            return
        own = self.process.pid if self.process else 0
        for pid in list(self.tree):
            if pid not in {own, os.getpid()}:
                with suppress(ChildProcessError, OSError):
                    os.waitpid(pid, os.WNOHANG)

    def end(self):
        """The CLI asked to stop, then everything it ran ended (killed when it does not)."""
        if sys.platform == "win32":
            return
        # Its processes now, while they are still its own (or, on Linux, come to this one).
        self.look()
        self.ending = True  # Its kqueue's reader stops.
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
