"""What the runtime knows about each session beyond the database.

The database keeps what must survive a restart: status, messages and the delivery marks. This
module keeps what belongs to the running process: the Copilot connection, the turn in progress
and what to do when it ends. It is lost on restart by design.

Everything runs on one asyncio loop, yet coroutines interleave at every await, so state that is
read, awaited on and then written needs a lock. When more than one is needed they are taken in
this order, and none is held while waiting for the owner:

1. ``Runtime.rotate_lock``: Router rotation.
2. ``SessionState.lifecycle``: connecting, resuming or replacing one session's Copilot session.
3. ``SessionState.sending``: one send at a time per session, together with its ``sent:`` mark.
4. ``Workers.pump_lock``: worker slots; held only around reads and writes, never across a
   session lock.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionState:
    # The live Copilot session, or None while disconnected.
    handle: Any = None
    # A worker turn is running or starting; this is what max_workers counts.
    busy: bool = False
    # A delayed flush of held messages is scheduled.
    flush_scheduled: bool = False
    # The model changed during this turn: messages wait for the next turn, which uses it.
    retuned: bool = False
    # Reconnect once this turn ends, so new instructions apply.
    stale: bool = False
    # Said to it in a new turn once it is reattached (after its sandbox changed).
    after_refresh: str = ""
    # Close once this turn ends (its pull request was merged or closed).
    close_after_turn: bool = False
    # The status a done or waiting worker returns to after a follow-up turn without a report.
    resting: str = ""
    # The Router's current turn has no message from the owner.
    event_turn: bool = False
    # The next turn resumes one the owner started (after a restart).
    owner_turn: bool = False
    # The Router's current turn answers the owner: its reply is what the owner reads.
    owner_reply: bool = False
    # Decisions the Router recorded and tasks it gave out in its current turn: what one owner
    # message decided and asked for together belongs together.
    turn_decisions: list[str] = field(default_factory=list)
    turn_tasks: list[str] = field(default_factory=list)
    # Tasks a worker set a check-back on in its current turn: it waits for it, not in this turn.
    turn_check_backs: set[str] = field(default_factory=set)
    # Tasks it reported done in its current turn: another done report on one is an amendment.
    turn_reported: set[str] = field(default_factory=set)
    # Open cards for the owner: draft id -> text.
    asking: dict[str, str] = field(default_factory=dict)
    # Messages sent to it that its conversation has not shown it yet (sent while it was
    # finishing a turn, say): digest of what was sent -> the newest message's text, the tasks
    # they are about ("" for a message about none) and the first message's id (to send them
    # again if its turn is stopped before it saw them).
    unread: dict[str, tuple[str, tuple[str, ...], int]] = field(default_factory=dict)
    # Its connection has shown a message it received (so unread ones can be told apart).
    echoes: bool = False
    # Every message sent to it that its conversation has not shown yet, even one that starts a
    # turn: digest -> the first message's id. A stop before it saw them sends them again.
    unseen: dict[str, int] = field(default_factory=dict)
    # A message sent to start a turn, until its conversation shows one (backends that echo):
    # its digest and the sent mark before it. Copilot can take a message and never start (it
    # gave up on the session's queue): see Delivery.check_started.
    awaiting: tuple[str, int] | None = None
    # Its turn is over, but a command it left running in the background (an attached shell:
    # a dev server) holds Copilot's session.idle until it ends, and with it every queued
    # message: messages go in at once ("immediate"), which starts a turn (see Events.held_idle).
    held: bool = False
    # Counts its turns' starts and ends: a check scheduled at a turn's end sees whether another
    # began or ended since.
    turn_mark: int = 0
    # Copilot told it something ended (a command it left running, a sub-agent) since its last
    # turn and no message came: a turn starting now is one it started on its own.
    woken: bool = False
    # What was sent to it that its conversation has not shown yet (backends that echo): digest
    # -> the first and last message ids it carried, the text and attachments as sent and how
    # many times it was sent again. See Delivery.check_echo.
    echo_wait: dict[str, tuple[int, int, str, list, int]] = field(default_factory=dict)
    # It was stopped and its turn aborted: the next message steers (until a turn ends or
    # starts, Copilot may still hold the session busy). Its first steps after the stop are not
    # work on the stopped task until a new message started a turn.
    after_abort: bool = False
    # Tools it called in its current turn: a turn that failed after acting is not sent again.
    turn_tools: int = 0
    # Its failed turn is sent again shortly (not a turn that ended without a report).
    retrying: bool = False
    # Its current turn brings news from a thread or PR it follows: the owner hears of it.
    thread_turn: bool = False
    thread_nudged: bool = False
    # The tail of the latest tool output, for the Watchdog.
    last_output: str = ""
    # The steps (tool calls) it is in now, by call id: when each started, what it is for and
    # what it runs.
    steps: dict[str, tuple[float, str, str]] = field(default_factory=dict)
    # Its calls that read, type into or stop a shell, by call id: (tool, shell id).
    shell_calls: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Calls of a tool that opens a PR (a GitHub MCP server's create_pull_request): its result
    # names the PR, which is then the task's own.
    pr_calls: set[str] = field(default_factory=set)
    lifecycle: asyncio.Lock = field(default_factory=asyncio.Lock)
    sending: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serializes add_repo for one worker.
    checkouts: asyncio.Lock = field(default_factory=asyncio.Lock)

    def step(self) -> tuple[float, str]:
        """The longest-running step it is in now: when it started and what it is for; (0, "")
        between steps. Several run at once when it calls tools in parallel."""
        since, described, _ = self.current_step()
        return since, described

    def current_step(self) -> tuple[float, str, str]:
        """The same, with what it runs (the command)."""
        return min(self.steps.values()) if self.steps else (0.0, "", "")

    def pop_resting(self, default: str) -> str:
        status, self.resting = self.resting or default, ""
        return status


class SessionStates:
    """Session id -> SessionState, created on first use."""

    def __init__(self):
        self._states: dict[str, SessionState] = {}

    def __getitem__(self, session_id: str) -> SessionState:
        return self._states.setdefault(session_id, SessionState())

    def handle(self, session_id: str) -> Any:
        state = self._states.get(session_id)
        return state.handle if state else None

    def is_live(self, session_id: str) -> bool:
        return self.handle(session_id) is not None

    def take_handle(self, session_id: str) -> Any:
        """The live Copilot session, now detached from this state."""
        state = self._states.get(session_id)
        if not state:
            return None
        handle, state.handle = state.handle, None
        return handle

    def live_ids(self) -> list[str]:
        return [key for key, state in self._states.items() if state.handle is not None]

    def take_all_handles(self) -> list[Any]:
        return [handle for key in self.live_ids() if (handle := self.take_handle(key))]

    def end_turns(self):
        """Every session is disconnected: no turn is running, none holds a worker slot."""
        for state in self._states.values():
            state.busy = False
            state.retuned = False
            state.stale = False
            state.after_refresh = ""
            state.awaiting = None
            state.held = False
            state.echo_wait.clear()
            state.steps.clear()
