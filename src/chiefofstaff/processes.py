"""The machine's processes, as Chief of Staff tells them apart and ends them: light enough for
the supervisor of another agent's processes (agentrun.py) to import without the Copilot SDK."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress

from . import machine


def process_table() -> dict[int, tuple[int, str]]:
    """Every process: pid -> (parent pid, command line)."""
    if sys.platform == "win32":
        return {
            p["pid"]: (p["ppid"], subprocess.list2cmdline(p["cmdline"]))
            for p in machine.processes("ppid", "cmdline")
            if p["cmdline"]
        }
    try:
        table = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],  # noqa: S607  (a fixed command, found on PATH)
            capture_output=True,
            text=True,
            # Any program's command line, which need not be UTF-8.
            errors="replace",
            check=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in table.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            found[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return found


def descendants(table: dict[int, tuple[int, str]], root: int) -> set[int]:
    """`root` and every process it started, and they started (while `root` runs)."""
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    found, todo = set(), [root]
    while todo:
        pid = todo.pop()
        if pid not in found:
            found.add(pid)
            todo += children.get(pid, [])
    return found


def end_processes(tree: dict[int, str], wait: float = 3.0):
    """Stop a copilot_tree: asked to end, then killed if they do not. A process whose command
    changed since is another one, left alone."""

    def same(table: dict[int, tuple[int, str]]) -> list[int]:
        return [pid for pid, command in tree.items() if pid in table and table[pid][1] == command]

    # On Windows any signal os.kill sends is TerminateProcess, which cannot be ignored: both
    # passes force it, the second for any the first could not reach.
    for signum in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        left = same(process_table())
        for pid in left:
            with suppress(OSError):
                os.kill(pid, signum)
        deadline = time.monotonic() + wait
        while left and time.monotonic() < deadline:
            time.sleep(0.05)
            left = [pid for pid in left if running(pid)]
        if not left:
            return


def running(pid: int) -> bool:
    if sys.platform == "win32":
        import psutil

        return psutil.pid_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Someone else's process, but it exists.
    return True
