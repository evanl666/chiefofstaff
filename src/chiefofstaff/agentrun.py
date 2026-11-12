"""The supervisor every process of another coding agent runs under (`python -m
chiefofstaff.agentrun`).

    python -m chiefofstaff.agentrun [--lifeline FD] -- codex app-server ...

It starts the agent's CLI in a process group of its own, with the owner's environment without
Chief of Staff's variables (CHIEFOFSTAFF_*, the Slack token among them), and the CLI talks to
Chief of Staff over the stdin and stdout it inherits. When the CLI exits, or when Chief of Staff
goes away however it stopped (its end of the lifeline pipe closes, or this process is orphaned),
it ends the CLI's whole process tree, commands left running in the background included, as
backend.end_processes does for Copilot's; then it exits with the CLI's code. It never writes to
stdout: that is the CLI's.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import suppress

from .backend import descendants, end_processes, process_table

# How often the CLI's process tree is looked at (seconds): what it started is ended with it,
# also once the CLI itself is gone and they were left to another parent. What a CLI that exits
# by itself (crashes) started in its last moments is not seen (it ends its own on a clean exit);
# when the supervisor ends the CLI, it looks first.
LOOK_EVERY = float(os.environ.get("CHIEFOFSTAFF_AGENTRUN_LOOK", "2"))


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

    def run(self) -> int:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, self.on_signal)
        self.process = subprocess.Popen(  # noqa: S603  (the agent's CLI, as configured)
            self.command, env=own_environment(), start_new_session=True
        )
        next_look = 0.0
        while (code := self.process.poll()) is None:
            if self.ending or self.gone():
                break
            if time.monotonic() >= next_look:
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
        """Sleep, waking when the lifeline closes."""
        if self.lifeline is None:
            time.sleep(seconds)
            return
        with suppress(OSError, InterruptedError):
            select.select([self.lifeline], [], [], seconds)

    def look(self):
        """What the CLI runs now, added to what it ran that still runs (left to another parent
        once what started it ended)."""
        if not self.process:
            return
        table = process_table()
        if not table:
            return
        found = descendants(table, self.process.pid) if self.process.pid in table else set()
        self.tree = {pid: c for pid, c in self.tree.items() if table.get(pid, (0, ""))[1] == c}
        self.tree |= {pid: table[pid][1] for pid in found if pid in table}

    def end(self):
        """The CLI asked to stop, then everything it ran ended (killed when it does not)."""
        if self.process and self.process.poll() is None:
            self.look()  # Its children now, while they are still its own.
            with suppress(OSError):
                os.killpg(self.process.pid, signal.SIGTERM)
            with suppress(subprocess.TimeoutExpired):
                self.process.wait(3)
            if self.process.poll() is None:
                with suppress(OSError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                with suppress(subprocess.TimeoutExpired):
                    self.process.wait(3)
        if self.process:
            self.tree.pop(self.process.pid, None)
        self.tree.pop(os.getpid(), None)
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
