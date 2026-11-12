"""Where the owner's other Copilot sessions stand (others.py, docs/design/adopt-sessions.md).

The logs here are made up in the shape the Copilot CLI writes them (1.0.91 to 1.0.109): each
line one event, its type first; a session.start line first; the agent's own events without an
agentId, its sub-agents' with one. The words are invented."""

import asyncio
import itertools
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import FakeClient, FakeSlackMcp, FakeTokens, Harness
from fastapi.testclient import TestClient

from chiefofstaff import others
from chiefofstaff.backend import CopilotBackend
from chiefofstaff.models import Settings
from chiefofstaff.others import NEUTRAL, Log, read_pass, status_of, where, whose
from chiefofstaff.runtime import Runtime
from chiefofstaff.scripted import ScriptedBackend
from chiefofstaff.sources.github import GitHubSource
from chiefofstaff.sources.slack import SlackSource
from chiefofstaff.web import create_app

START = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds")[:23] + "Z"


class Clocks:
    """The clock others.py counts elapsed time on and the wall clock, moved by the test."""

    def __init__(self, monkeypatch):
        self.now = 5000.0
        self.wall = (START + timedelta(hours=1)).timestamp()
        monkeypatch.setattr(others, "clock", lambda: self.now)
        monkeypatch.setattr(others, "wall", lambda: self.wall)

    def tick(self, seconds: float):
        self.now += seconds
        self.wall += seconds


@pytest.fixture
def clocks(monkeypatch):
    return Clocks(monkeypatch)


class Terminal:
    """A Copilot session run in a terminal, writing its log as the CLI does."""

    def __init__(self, home: Path, clocks: Clocks, session_id: str = "", head: dict | None = None):
        self.id = session_id or str(uuid4())
        self.clocks = clocks
        self.path = home / "session-state" / self.id / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.parent = None
        self.turns = itertools.count()
        self.turn = "0"
        self.write(
            "session.start",
            **(
                head
                if head is not None
                else {
                    "sessionId": self.id,
                    "version": 1,
                    "producer": "copilot-agent",
                    "copilotVersion": "1.0.109",
                    "startTime": iso(clocks.wall),
                    "context": {"cwd": "/work/app", "branch": "main"},
                    "alreadyInUse": False,
                }
            ),
        )

    def write(self, kind: str, *, agent: str = "", at: str | None = None, **data) -> str:
        event: dict = {"type": kind, "data": data, "id": str(uuid4())}
        event["timestamp"] = iso(self.clocks.wall) if at is None else at
        event["parentId"] = self.parent
        if agent:
            event["agentId"] = agent
        self.line(json.dumps(event, separators=(",", ":"), ensure_ascii=False))
        self.parent = event["id"]
        return event["id"]

    def line(self, text: str):
        # A lone surrogate (JSON can carry one) is written escaped, as the CLI writes it.
        with self.path.open("a", errors="backslashreplace") as log:
            log.write(text + "\n")
        os.utime(self.path, (self.clocks.wall, self.clocks.wall))  # Written now, by its clock.

    def usage(self):
        self.write("session.usage_record", usage={"model": "a-model", "inputTokens": 900})

    def prompt(self, text: str = "Make the login test pass"):
        self.turn = str(next(self.turns))
        self.write("user.message", content=text, interactionId="i-1", turnId=self.turn)
        self.write("assistant.turn_start", turnId=self.turn, interactionId="i-1")

    def call(self, tool: str = "bash", agent: str = "", **arguments) -> str:
        call = f"call_{uuid4().hex[:24]}"
        request = {"toolCallId": call, "name": tool, "arguments": arguments, "type": "function"}
        self.write("assistant.message", agent=agent, content="", toolRequests=[request])
        self.write(
            "tool.execution_start", agent=agent, toolCallId=call, toolName=tool, arguments=arguments
        )
        return call

    def done(self, call: str, agent: str = ""):
        """A step ends, and the model is called again with what it gave."""
        self.write("tool.execution_complete", agent=agent, toolCallId=call, success=True)
        self.usage()
        self.write("assistant.turn_end", turnId=self.turn)
        self.write("assistant.turn_start", turnId=self.turn, interactionId="i-1")

    def ask(self, question: str = "Which branch should the fix go to?") -> str:
        schema = {"properties": {"answer": {"type": "string"}}}
        return self.call("ask_user", message=question, requestedSchema=schema)

    def answered(self, call: str):
        self.write("human_response.recorded", toolCallId=call, response={"responseKind": "x"})
        self.done(call)

    def answer(self, text: str = "Done: the login test passes now."):
        self.write("assistant.message", content=text, interactionId="i-1", turnId=self.turn)
        self.usage()
        self.write("assistant.turn_end", turnId=self.turn)

    def exit(self):
        self.write("session.shutdown", shutdownType="routine", totalPremiumRequests=1)


def facts(*terminals: Terminal, startup: bool = True, cursor: str = "", logs=None):
    """One pass over these sessions' logs: what it found of each."""
    logs = logs if logs is not None else {t.id: Log(t.id, t.path) for t in terminals}
    _, found, _, _ = read_pass(list(logs.values()), startup, cursor, lambda: False)
    return found


class Follow:
    """The reader following some sessions, pass after pass."""

    def __init__(self, *terminals: Terminal):
        self.logs = {t.id: Log(t.id, t.path) for t in terminals}
        self.cursor = ""
        self.read = 0

    def add(self, terminal: Terminal):
        self.logs[terminal.id] = Log(terminal.id, terminal.path)

    def step(self, startup: bool = False) -> dict:
        self.cursor, found, self.sampled, self.read = read_pass(
            list(self.logs.values()), startup, self.cursor, lambda: False
        )
        self.found = found
        return found

    def status(self, terminal: Terminal, is_open: bool = True):
        """The status key, or why there is none."""
        found = self.where(terminal, is_open)
        return found if isinstance(found, str) else found[0]

    def shown(self, terminal: Terminal, is_open: bool = True) -> dict | str:
        found = self.where(terminal, is_open)
        return found if isinstance(found, str) else status_of(found, others.clock())

    def where(self, terminal: Terminal, is_open: bool):
        sampled = self.sampled[terminal.id]
        return where(self.found[terminal.id], sampled, "owner", is_open, others.clock())


@pytest.fixture
def home(tmp_path):
    return tmp_path / "copilot"


# Statuses (section 4.5)


def test_a_session_that_asks_the_owner_says_so_with_its_question(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    term.done(term.call(command="npm test"))
    clocks.tick(30)
    term.ask("Should the fix go to main or to the release branch?")
    reader = Follow(term)
    reader.step(startup=True)
    clocks.tick(600)
    assert reader.status(term) == "reading", "a sample this old is not shown"
    reader.step()
    shown = reader.shown(term)
    assert shown == {
        "key": "asks",
        "elapsed": 600.0,
        "since": iso(clocks.wall - 600),
        "before": False,
        "words": "Should the fix go to main or to the release branch?",
    }
    assert reader.status(term, is_open=False) == "stopped", "closed while it asked"


def test_an_approval_asked_for_is_a_question_to_the_owner(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    call = term.call(command="rm -rf build")
    term.write(
        "permission.requested",
        requestId="r-1",
        permissionRequest={"kind": "shell", "toolCallId": call, "fullCommandText": "rm -rf build"},
    )
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.shown(term)["key"] == "asks"
    assert reader.shown(term)["words"] == "Permission for rm -rf build"
    term.write("permission.completed", requestId="r-1", toolCallId=call, result={"kind": "ok"})
    reader.step()
    assert reader.status(term) == "working"
    assert reader.shown(term)["words"] == "bash rm -rf build", "the step it runs"


def test_a_turn_ends_as_the_owners_turn_then_idle(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    term.done(term.call(command="pytest tests/test_login.py"))
    term.answer("Fixed: the token expiry used the client's clock.")
    reader = Follow(term)
    reader.step(startup=True)
    shown = reader.shown(term)
    assert shown["key"] == "your_turn" and shown["elapsed"] == 0
    assert shown["words"] == "Fixed: the token expiry used the client's clock."
    for _ in range(10):  # An hour, pass after pass (its sample renewed every pass).
        clocks.tick(360)
        reader.step()
    assert reader.status(term) == "idle"
    assert reader.status(term, is_open=False) == "finished"


def test_stopped_by_the_owner_or_failed_is_said(home, clocks):
    stopped, failed = Terminal(home, clocks), Terminal(home, clocks)
    for term in (stopped, failed):
        term.prompt()
        term.call(command="make build")
    stopped.write("abort", reason="user_initiated")
    failed.write("session.error", errorType="query", message="The model is not available.")
    reader = Follow(stopped, failed)
    reader.step(startup=True)
    assert reader.shown(stopped)["words"] == "Stopped by you"
    assert reader.shown(failed)["words"] == "The model is not available."
    assert {reader.status(stopped), reader.status(failed)} == {"your_turn"}
    for term in (stopped, failed):
        term.exit()
    reader.step()
    assert reader.status(stopped, is_open=False) == "stopped"
    assert reader.shown(failed, is_open=False)["words"] == "The model is not available."


def test_a_session_at_work_then_without_progress(home, clocks):
    term = Terminal(home, clocks)
    term.prompt("Run the integration suite")
    clocks.tick(20)
    term.call(command="make integration")
    reader = Follow(term)
    reader.step(startup=True)
    shown = reader.shown(term)
    assert shown["key"] == "working" and shown["before"] is False
    assert shown["since"] == iso(clocks.wall - 20) and shown["elapsed"] == 20
    assert shown["words"] == "bash make integration"
    clocks.tick(60)
    term.usage()  # Its log grows: still at work.
    reader.step()
    for _ in range(15):
        clocks.tick(60)
        reader.step()
    shown = reader.shown(term)
    assert shown["key"] == "no_progress" and shown["words"] == "bash make integration"
    assert 15 * 60 <= shown["elapsed"] < 16 * 60, "since it last grew"
    term.usage()
    reader.step()
    assert reader.status(term) == "working"


def test_a_session_closed_in_the_middle_of_a_turn_stopped(home, clocks):
    killed, exited = Terminal(home, clocks), Terminal(home, clocks)
    for term in (killed, exited):
        term.prompt()
        term.call(command="npm run e2e")
    exited.exit()
    reader = Follow(killed, exited)
    reader.step(startup=True)
    assert reader.shown(killed, is_open=False)["key"] == "stopped"
    assert reader.shown(killed, is_open=False)["words"] == "bash npm run e2e", "its last step"
    assert reader.status(exited, is_open=False) == "stopped"
    assert reader.status(exited) == "reading", "open again before its log says so"


def test_a_session_that_has_not_started_anything_and_one_closed_long_ago(home, clocks):
    new, old = Terminal(home, clocks), Terminal(home, clocks)
    old.prompt()
    old.answer()
    old.exit()
    clocks.tick(25 * 3600)
    reader = Follow(new, old)
    reader.step(startup=True)
    assert reader.status(new) == "fresh" and reader.status(new, is_open=False) == "fresh"
    assert reader.status(old, is_open=False) == "closed"


def test_what_its_sub_agents_do_is_not_where_it_stands(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    task = term.call("task", description="Look through the logs")
    helper = term.call("ask_user", agent="sub-1", message="A helper's question")
    term.write("abort", agent="sub-1", reason="user_initiated")
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.shown(term)["key"] == "working"
    assert reader.shown(term)["words"] == "task Look through the logs"
    term.done(helper, agent="sub-1")
    term.done(task)
    term.answer()
    reader.step()
    assert reader.status(term) == "your_turn"


def test_words_are_kept_short_and_cut_whole(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    term.answer("é" * 3000)
    found = facts(term)[term.id]
    assert found.words == "é" * 512, "1 KB, cut at a character's boundary"
    status = status_of(where(found, clocks.now, "owner", True, clocks.now), clocks.now)
    assert status["words"] == "é" * 200


# The reducer's confidence (section 4.4)


@pytest.fixture
def capped(monkeypatch):
    """A small line cap, so a line over it is quick to write."""
    monkeypatch.setattr(others, "LINE_CAP", 4096)
    return "x" * 5000


@pytest.mark.parametrize(
    "case",
    ["asked", "approval", "abort", "error", "answer", "shutdown", "rewind", "resume"],
)
def test_after_a_gap_no_status_is_guessed(home, clocks, capped, case):
    """A line it does not read (over the cap) right before what would change the status: the
    status is not shown until the next model call starts, whatever came after the gap."""
    term = Terminal(home, clocks)
    term.prompt()
    asked = term.ask()
    term.write(
        "permission.requested",
        requestId="r-1",
        permissionRequest={"kind": "shell", "fullCommandText": "make deploy"},
    )
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "asks"
    term.write("assistant.message", content=capped, toolRequests=[{"name": "bash"}])
    if case == "asked":
        term.write("human_response.recorded", toolCallId=asked, response={"responseKind": "x"})
        term.write("tool.execution_complete", toolCallId=asked, success=True)
    elif case == "approval":
        term.write("permission.completed", requestId="r-1", result={"kind": "approved"})
    elif case == "abort":
        term.write("abort", reason="user_initiated")
    elif case == "error":
        term.write("session.error", errorType="query", message="Rate limited.")
    elif case == "answer":
        term.answer()
    elif case == "shutdown":
        term.exit()
    elif case == "rewind":
        term.write("session.snapshot_rewind", eventsRemoved=3, upToEventId=asked)
    elif case == "resume":
        term.write("session.resume", sessionWasActive=False, eventCount=12)
    reader.step()
    assert reader.status(term) == "partial" and reader.status(term, is_open=False) == "partial"
    term.prompt("Go on")
    reader.step()
    assert reader.status(term) == "working", "complete again from the next model call"
    began = reader.shown(term)["before"] is False
    assert began is (case in {"abort", "error"}), "a message begins a turn only after a known end"


def test_a_neutral_line_however_long_is_passed_over_not_a_gap(home, clocks, capped):
    term = Terminal(home, clocks)
    term.prompt()
    asked = term.ask()
    term.write("session.binary_asset", assetId="a-1", mimeType="image/png", data=capped * 3)
    term.answered(asked)
    term.answer()
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "your_turn"


def test_a_resume_is_no_fresh_start_with_or_without_pending_work(home, clocks):
    killed, idle = Terminal(home, clocks), Terminal(home, clocks)
    killed.prompt()
    killed.ask()  # Killed while it asked: no end in its log.
    killed.write("session.resume", sessionWasActive=True, continuePendingWork=True)
    idle.prompt()
    idle.answer()
    idle.exit()
    idle.write("session.resume", sessionWasActive=False)
    reader = Follow(killed, idle)
    reader.step(startup=True)
    assert reader.status(killed) == "partial", "not still asking: it may have gone on, or not"
    assert reader.status(idle) == "partial"
    idle.prompt("Now the docs")
    reader.step()
    assert reader.status(idle) == "working"


def test_a_completion_it_does_not_hold_shows_a_wrong_reset(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "working"
    term.write("tool.execution_complete", toolCallId="call_never_started", success=True)
    reader.step()
    assert reader.status(term) == "partial"
    term.write("permission.completed", requestId="r-never")
    term.prompt()
    reader.step()
    assert reader.status(term) == "working"
    term.write("permission.completed", requestId="r-never")
    reader.step()
    assert reader.status(term) == "partial", "an approval it did not hold either"


def test_a_step_the_owner_ran_themselves_is_held_until_it_ends(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    term.write(
        "tool.user_requested", toolCallId="u-1", toolName="bash", arguments={"command": "ls"}
    )
    term.write("tool.execution_complete", toolCallId="u-1", success=True)
    term.answer()
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "your_turn"


@pytest.mark.parametrize("kind", [*sorted(NEUTRAL), "model.call_failure", "subagent.started"])
def test_neutral_events_change_nothing(home, clocks, kind):
    term = Terminal(home, clocks)
    term.prompt()
    asked = term.ask()
    term.write(kind, content="made up")
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "asks"
    term.answered(asked)
    term.write(kind, content="made up")
    term.answer()
    reader.step()
    assert reader.status(term) == "your_turn"


@pytest.mark.parametrize(
    "line",
    [
        '{"type":"session.idle","data":{}}',  # Known to the SDK, never seen in a log.
        '{"type":"user_input.requested","data":{"question":"x"}}',
        '{"type":"tool.execution_start","data":{"toolName":"bash"}}',  # No toolCallId.
        '{"type":"tool.execution_complete","data":{}}',
        '{"type":"permission.requested","data":{"permissionRequest":{}}}',  # No requestId.
        '{"type":"permission.completed","data":{"requestId":7}}',
        '{"type":"assistant.message","data":"not an object"}',
        '{"type":"assistant.message","data":{"content":"cut',  # Not JSON.
        '{"type":"tool.execution_start","data":{"toolCallId":"' + "x" * 200 + '"}}',
        '{"type":"user_input.requested","data":{},"agentId":"sub-1"}',  # A sub-agent's: still.
        "not a line of JSON at all, long enough to be read whole" + "." * 100,
    ],
)
def test_an_event_it_does_not_know_or_cannot_read_makes_it_partial(home, clocks, line):
    term = Terminal(home, clocks)
    term.prompt()
    term.line(line)
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "partial"
    term.prompt()
    reader.step()
    assert reader.status(term) == "working"


def test_a_line_in_another_order_is_still_read(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    term.line(json.dumps({"data": {"content": "Done."}, "type": "assistant.message"}))
    term.write("assistant.turn_end", turnId="0")
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.shown(term)["words"] == "Done."


def test_more_than_sixteen_pending_steps_is_partial(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    for n in range(16):
        term.call(command=f"sleep {n}")
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "working"
    term.call(command="sleep 17")
    reader.step()
    assert reader.status(term) == "partial"


@pytest.mark.parametrize(
    "head",
    [
        {"producer": "another-agent", "version": 1},
        {"producer": "copilot-agent", "version": 2},
        {"producer": "copilot-agent", "version": True},
        {"producer": "copilot-agent"},
    ],
)
def test_a_log_in_another_format_is_not_read(home, clocks, head):
    term = Terminal(home, clocks, head=head)
    term.prompt()
    term.answer()
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "format"
    assert reader.logs[term.id].read_to == term.path.stat().st_size, "and not parsed on"


def test_a_log_that_does_not_start_with_a_session_start_is_not_read(home, clocks):
    term = Terminal(home, clocks)
    term.path.write_text('{"type":"user.message","data":{"content":"hi"}}\n')
    term.prompt()
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "format"


# First looks (section 4.4)


@pytest.fixture
def small(monkeypatch):
    """Logs read whole up to 4 KB, else from windows of 2 KB, 4 KB, 8 KB and 16 KB."""
    monkeypatch.setattr(others, "WHOLE", 4096)
    monkeypatch.setattr(others, "WINDOWS", (2048, 4096, 8192, 16384))


def padding(term: Terminal, size: int):
    """Neutral lines up to about `size` bytes."""
    while term.path.stat().st_size < size:
        term.usage()


@pytest.mark.parametrize(
    ("after", "window"), [(0, 2048), (2500, 4096), (6000, 8192), (12000, 16384)]
)
def test_a_long_log_is_read_from_the_smallest_window_with_a_model_call(
    home, clocks, small, after, window
):
    term = Terminal(home, clocks)
    term.prompt("A long session")
    early = term.call(command="make lint")  # Begun before the window, ended in it.
    padding(term, 30000)
    clocks.tick(5)
    start = term.path.stat().st_size
    term.write("tool.execution_complete", toolCallId=early, success=True)
    read_from = iso(clocks.wall)
    clocks.tick(5)
    term.write("assistant.turn_start", turnId="5")
    padding(term, start + after)
    term.write("assistant.turn_start", agent="sub-1", turnId="0")  # A sub-agent's: not its own.
    term.call(command="cargo build")
    size = term.path.stat().st_size
    assert min(w for w in others.WINDOWS if size - w <= start) == window
    assert all(size - w > start + 400 for w in others.WINDOWS if w < window)
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.logs[term.id].low == size - window, "it looked no further back"
    shown = reader.shown(term)
    assert shown["key"] == "working" and shown["words"] == "bash cargo build"
    assert shown["before"] is True and shown["since"] == read_from, "since before what it read"


def test_a_long_log_with_no_model_call_at_its_end_has_no_status_until_one(home, clocks, small):
    term = Terminal(home, clocks)
    term.prompt()
    padding(term, 30000)
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "no_start"
    assert reader.read <= 16384 + 2 * 65536, "the end and the first line only"
    term.prompt("Next")
    reader.step()
    assert reader.status(term) == "working"


def test_a_short_log_is_read_whole_from_its_start(home, clocks, small):
    term = Terminal(home, clocks)
    term.prompt("Short")
    term.answer()
    padding(term, 4000)
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "your_turn"
    assert reader.read == term.path.stat().st_size


def test_a_log_rewritten_replaced_or_gone_is_looked_at_again(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    term.answer()
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "your_turn"
    term.path.unlink()
    reader.step()
    assert reader.status(term) == "missing"
    term = Terminal(home, clocks, session_id=term.id)
    term.prompt()
    reader.step()
    assert reader.status(term) == "working"
    # Another file in its place, whose lines are not where the old one's were.
    first, *rest = term.path.read_text().splitlines(keepends=True)
    head = json.loads(first)
    head["data"]["context"]["cwd"] = "/work/" + "app" * 100
    replaced = term.path.with_name("new")
    replaced.write_text(json.dumps(head, separators=(",", ":")) + "\n" + "".join(rest))
    term.path, kept = replaced, term.path
    term.write("abort", reason="user_initiated")
    term.path = kept
    replaced.replace(term.path)
    reader.step()
    assert reader.shown(term)["words"] == "Stopped by you"
    term.path.write_text(term.path.read_text().splitlines(keepends=True)[0])  # Rewritten.
    reader.step()
    assert reader.status(term) == "fresh"


# Files it reads (section 5)


def test_a_link_or_a_pipe_is_never_read(home, clocks, tmp_path):
    elsewhere = Terminal(tmp_path / "elsewhere", clocks)
    elsewhere.prompt()
    linked = home / "session-state" / str(uuid4()) / "events.jsonl"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(elsewhere.path)
    folder = home / "session-state" / str(uuid4())
    folder.symlink_to(elsewhere.path.parent)
    piped = home / "session-state" / str(uuid4()) / "events.jsonl"
    piped.parent.mkdir(parents=True)
    os.mkfifo(piped)
    logs = {path: Log(str(path), path) for path in (linked, folder / "events.jsonl", piped)}
    _, found, _, read = read_pass(list(logs.values()), True, "", lambda: False)
    assert {f.reason for f in found.values()} == {"unreadable"} and read == 0


def test_a_pipe_put_in_its_place_after_it_was_sampled_does_not_block(home, clocks):
    term = Terminal(home, clocks)
    log = Log(term.id, term.path)
    log.sample(clocks.now, clocks.wall)
    term.path.unlink()
    os.mkfifo(term.path)
    assert log.open() is None and log.problem == "unreadable"


def test_without_a_log_there_is_nothing_to_read(clocks):
    _, found, _, read = read_pass([Log(str(uuid4()), None)], True, "", lambda: False)
    assert [f.reason for f in found.values()] == ["missing"] and read == 0


# Time (section 4.5)


def test_moving_the_wall_clock_changes_no_elapsed_time(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    reader = Follow(term)
    reader.step(startup=True)
    term.answer()
    clocks.wall += 3600  # Moved an hour ahead by hand, before the reader saw the answer.
    reader.step()  # Seen come: dated by the pass that first saw it, not by its own time.
    clocks.tick(60)
    reader.step()
    assert reader.shown(term)["key"] == "your_turn" and reader.shown(term)["elapsed"] == 60
    clocks.wall -= 7200  # And back two.
    clocks.tick(60)
    reader.step()
    assert reader.shown(term)["elapsed"] == 120


@pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")), reason="a clock that counts sleep"
)
@pytest.mark.parametrize("working", [False, True])
def test_a_night_asleep_counts_as_time_gone_by(home, monkeypatch, working):
    """While a Mac sleeps time.monotonic stands still; the clock elapsed time is counted on
    does not (CLOCK_MONOTONIC there, CLOCK_BOOTTIME on Linux)."""
    counted = {"now": 1000.0}
    monkeypatch.setattr(time, "clock_gettime", lambda which: counted["now"])
    clocks = SimpleNamespace(wall=(START + timedelta(hours=1)).timestamp())
    monkeypatch.setattr(others, "wall", lambda: clocks.wall)
    term = Terminal(home, clocks)  # type: ignore[arg-type]
    term.prompt()
    if working:
        term.call(command="make test")
    else:
        term.answer()
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == ("working" if working else "your_turn")
    counted["now"] += 10 * 3600  # Asleep for the night.
    clocks.wall += 10 * 3600
    reader.step()
    assert reader.status(term) == ("no_progress" if working else "idle")
    assert reader.shown(term)["elapsed"] >= 10 * 3600


@pytest.mark.parametrize(
    "stamp",
    ["2026-11-10T10:03:00.000Z", "yesterday", "2026-11-10T10:00:00", "2026-11-10T08:00:00.000Z"],
    ids=["future", "malformed", "no zone", "before its start"],
)
def test_an_event_time_not_to_trust_is_not_shown_or_counted(home, clocks, stamp, utc):
    term = Terminal(home, clocks)  # Started at 10:00; the wall clock says 10:00.
    term.prompt()
    term.write("assistant.message", content="Done.", at=stamp)
    term.write("assistant.turn_end", at=stamp)
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "waiting", "is it the owner's turn, or idle? not known yet"
    for _ in range(10):
        clocks.tick(360)
        reader.step()
    shown = reader.shown(term)
    assert shown["key"] == "idle" and shown["since"] is None and shown["elapsed"] is None


@pytest.fixture
def utc(monkeypatch):
    """The machine's time zone is UTC: a time without its zone would read as the right one."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_times_out_of_order_are_shown_as_written(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    clocks.tick(120)
    term.write("assistant.message", content="Done.", at=iso(clocks.wall - 60))
    term.write("assistant.turn_end", at=iso(clocks.wall - 90))
    reader = Follow(term)
    reader.step(startup=True)
    shown = reader.shown(term)
    assert shown["since"] == iso(clocks.wall - 90) and shown["elapsed"] == 90


def test_what_came_before_the_reader_looked_is_dated_by_its_own_time(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    term.answer()
    clocks.tick(1800)
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.shown(term)["elapsed"] == 1800
    clocks.tick(1800)
    reader.step()
    assert reader.status(term) == "idle"


def test_growth_before_the_reader_looked_is_dated_by_the_file(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    term.call(command="make")
    os.utime(term.path, (clocks.wall - 20 * 60, clocks.wall - 20 * 60))
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "no_progress"


# The reader's caps and fair share (section 4.6)


def busy(home: Path, clocks: Clocks, count: int, size: int) -> list[Terminal]:
    sessions = []
    for _ in range(count):
        term = Terminal(home, clocks)
        term.prompt()
        padding(term, size)
        sessions.append(term)
    return sessions


def test_a_start_reads_in_order_open_ones_first_within_its_cap(home, clocks, monkeypatch):
    monkeypatch.setattr(others, "STARTUP_BYTES", 20_000)
    sessions = busy(home, clocks, 4, 8_000)
    reader = Follow(*sessions)
    reader.step(startup=True)
    assert reader.read <= 20_000
    assert [reader.status(term) for term in sessions] == [
        "working",
        "working",
        "reading",
        "reading",
    ]
    reader.step(startup=True)
    assert [reader.status(term) for term in sessions] == ["working"] * 4


def test_steady_passes_share_their_bytes_round_robin(home, clocks, monkeypatch):
    monkeypatch.setattr(others, "STEADY_BYTES", 64 << 10)
    monkeypatch.setattr(others, "SHARE", 4 << 10)
    sessions = busy(home, clocks, 20, 1000)
    reader = Follow(*sessions)
    reader.step(startup=True)
    for term in sessions:
        padding(term, 11_000)  # 10 KB each to read: 200 KB in all.
    seen: dict[str, list[int]] = {term.id: [] for term in sessions}
    for number in range(1, 6):
        before = {key: log.read_to for key, log in reader.logs.items()}
        reader.step()
        assert reader.read <= 64 << 10, "at most its cap"
        for key, log in reader.logs.items():
            if log.read_to > before[key]:
                assert log.read_to - before[key] <= 4096 + 4096, "about its share"
                seen[key].append(number)
    for passes in seen.values():
        gaps = [b - a for a, b in itertools.pairwise([0, *passes])]
        assert max(gaps) <= 2, "each read at least every ceil(20 / 16) passes"
    statuses = {reader.status(term) for term in sessions}
    assert statuses <= {"working", "reading"}
    while any(log.unread() for log in reader.logs.values()):
        reader.step()
    assert {reader.status(term) for term in sessions} == {"working"}, "caught up, shown"


def test_a_session_not_caught_up_shows_no_status(home, clocks, monkeypatch):
    monkeypatch.setattr(others, "STEADY_BYTES", 8 << 10)
    monkeypatch.setattr(others, "SHARE", 8 << 10)
    term = Terminal(home, clocks)
    term.prompt()
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "working"
    asked = term.ask()
    padding(term, term.path.stat().st_size + 20_000)
    reader.step()
    assert reader.status(term) == "reading", "its question is not shown before the rest is read"
    while reader.logs[term.id].unread():
        reader.step()
    assert reader.status(term) == "asks"
    term.answered(asked)
    reader.step()
    assert reader.status(term) == "working"


def test_a_pass_stops_at_its_time_cap(home, clocks, monkeypatch):
    monkeypatch.setattr(others, "CHUNK", 1024)
    monkeypatch.setattr(others, "PASS_SECONDS", 0.05)
    reads = []

    def slow(fd, size, position):
        reads.append(size)
        threading.Event().wait(0.02)
        return os.pread(fd, size, position)

    monkeypatch.setattr(others, "read_chunk", slow)
    sessions = busy(home, clocks, 2, 30_000)
    reader = Follow(*sessions)
    reader.step(startup=True)
    assert 1 <= len(reads) <= 4


def test_a_session_far_behind_jumps_to_its_end(home, clocks, monkeypatch, small):
    monkeypatch.setattr(others, "JUMP", 32_000)
    term = Terminal(home, clocks)
    term.prompt()
    reader = Follow(term)
    reader.step(startup=True)
    term.call(command="make")
    padding(term, 100_000)
    term.prompt("Then the docs")
    term.call(command="mkdocs build")
    reader.step()
    assert reader.read < 20_000, "not what it jumped over"
    shown = reader.shown(term)
    assert shown["key"] == "working" and shown["words"] == "bash mkdocs build"
    assert shown["before"] is True, "after a gap, a message may not have begun the turn"


def test_a_stop_ends_a_pass_between_reads(home, clocks, monkeypatch):
    monkeypatch.setattr(others, "CHUNK", 1024)
    reads = []
    stop = threading.Event()

    def counted(fd, size, position):
        reads.append(position)
        if len(reads) == 2:
            stop.set()
        return os.pread(fd, size, position)

    monkeypatch.setattr(others, "read_chunk", counted)
    sessions = busy(home, clocks, 2, 30_000)
    read_pass([Log(t.id, t.path) for t in sessions], True, "", stop.is_set)
    assert len(reads) == 2


# Whose session it is (section 4.2)


def listed(
    session_id: str = "",
    made: str = "github/cli",
    last: str = "",
    *,
    clients: bool = True,
    remote: bool = False,
    hours: float = 1,
    summary: str = "Fix the login test",
) -> SimpleNamespace:
    meta = SimpleNamespace(
        session_id=session_id or str(uuid4()),
        summary=summary,
        is_remote=remote,
        modified_time=START + timedelta(hours=1) - timedelta(hours=hours),
        context=SimpleNamespace(working_directory="/work/app", repository="o/app", branch="main"),
    )
    if clients:
        meta.client_name, meta.last_client_name, meta.clients_listed = made, last, True
    return meta


@pytest.mark.parametrize(
    ("made", "last", "expected"),
    [
        ("github/cli", "", "owner"),
        ("github/cli", "github/cli", "owner"),
        ("sdk-app", "github/cli", "owner"),
        ("chiefofstaff", "github/cli", "owner"),  # Taken over in a terminal.
        ("github/cli", "chiefofstaff", "another"),  # Another Chief of Staff ran it last.
        ("github/cli", "work-buddy", "another"),
        ("chiefofstaff", "chiefofstaff", "ours"),  # Made by one under another data directory.
        ("chiefofstaff", "", "ours"),
        ("work-buddy", "chiefofstaff", "ours"),
        ("", "github/cli", "unknown"),
    ],
)
def test_whose_session_it_is(made, last, expected):
    assert whose(listed("s", made, last), set()) == expected


def test_what_is_ours_here_or_not_known_is_never_the_owners():
    assert whose(listed("s", "github/cli", "github/cli"), {"s"}) == "ours", "rule 1 first"
    assert whose(listed("s", remote=True), set()) == "remote"
    assert whose(listed("s", clients=False), set()) == "unlisted", "the fallback listing"
    flagged = listed("s", "github/cli", "github/cli")
    flagged.made_by_us = True
    assert whose(flagged, set()) == "ours"


def test_a_session_changing_hands_changes_whose_it_is():
    here = {"adopted"}  # Continued in this Chief of Staff: rule 1 whoever ran it last.
    for last in ("github/cli", "chiefofstaff", "github/cli"):
        assert whose(listed("adopted", "github/cli", last), here) == "ours"
    # The same session seen from a second data directory.
    seen = [whose(listed("s", "github/cli", last), set()) for last in ("", "chiefofstaff", "x")]
    assert seen == ["owner", "another", "owner"]
    # Made by Chief of Staff elsewhere, taken over in a terminal, run by it again.
    seen = [
        whose(listed("s", "chiefofstaff", last), set()) for last in ("", "github/cli", "work-buddy")
    ]
    assert seen == ["ours", "owner", "ours"]


async def test_the_cli_says_who_made_and_last_ran_each_session():
    times = {"startTime": "2026-11-01T00:00:00Z", "modifiedTime": "2026-11-01T00:00:00Z"}
    answer = {"sessions": [
        {"sessionId": "a", **times, "isRemote": False, "clientName": "github/cli",
         "lastClientName": "chiefofstaff"},
        {"sessionId": "b", **times, "isRemote": False},
    ]}  # fmt: skip

    class Rpc:
        async def request(self, method, payload):
            return answer

    class Client:
        _client = Rpc()

    listed = await CopilotBackend(Client()).list_sessions()  # type: ignore[arg-type]
    assert [(m.client_name, m.last_client_name, m.clients_listed) for m in listed] == [
        ("github/cli", "chiefofstaff", True),
        ("", "", True),
    ]
    assert [whose(m, set()) for m in listed] == ["another", "unknown"]
    assert CopilotBackend(Client()).session_log("not-an-id") is None  # type: ignore[arg-type]


# The index: what it follows, and what the tabs hear


class Index:
    """A connected runtime whose index runs pass by pass, with a tab open."""

    def __init__(self, harness: Harness, runtime: Runtime, home: Path, clocks: Clocks):
        self.harness, self.runtime, self.home, self.clocks = harness, runtime, home, clocks
        self.others = runtime.others
        self.tab: asyncio.Queue = asyncio.Queue()
        runtime.bus.queues.add(self.tab)

    def terminal(self, *, made="github/cli", last="", open_=True, **meta) -> Terminal:
        term = Terminal(self.home, self.clocks)
        self.harness.client.metas.append(listed(term.id, made, last, **meta))
        if open_:
            self.harness.client.in_use[term.id] = 4242
        return term

    async def step(self):
        await self.runtime.workers.refresh_elsewhere()
        await self.others.step(self.others.generation, self.others.stopping)

    def heard(self) -> list[dict]:
        events = []
        while not self.tab.empty():
            if (event := json.loads(self.tab.get_nowait()))["type"] == "others":
                events.append(event["data"])
        return events


@pytest.fixture
async def index(tmp_path, clocks):
    harness = Harness(tmp_path / "data")
    harness.client.home = tmp_path / "copilot"
    runtime = await harness.connect()
    return Index(harness, runtime, tmp_path / "copilot", clocks)


async def test_the_tabs_hear_where_the_owners_sessions_stand(index):
    asking = index.terminal()
    asking.prompt()
    asking.ask("Deploy to staging first?")
    done = index.terminal(open_=False, summary="Write the changelog")
    done.prompt()
    done.answer("The changelog is written.")
    done.exit()
    another = index.terminal(last="chiefofstaff")
    unknown = index.terminal(made="")
    ours = index.terminal(made="chiefofstaff")
    old = index.terminal(open_=False, hours=30)
    managed = index.terminal()
    index.harness.store.create_session(managed.id, "worker", "Managed", "idle", cwd="/tmp")
    remote = index.terminal(remote=True)
    await index.step()
    (event,) = index.heard()
    rows = {row["id"]: row for row in event["rows"]}
    assert list(rows) == [asking.id, another.id, unknown.id, done.id], "open ones first"
    assert rows[asking.id]["status"]["key"] == "asks" and rows[asking.id]["open"] is True
    assert rows[asking.id]["status"]["words"] == "Deploy to staging first?"
    assert rows[done.id]["status"]["key"] == "finished"
    assert rows[done.id]["summary"] == "Write the changelog" and rows[done.id]["branch"] == "main"
    assert (rows[another.id]["status"], rows[another.id]["reason"]) == (None, "another")
    assert rows[unknown.id]["why"] == "Copilot did not say which app started it."
    assert set(event["leases"]) == {asking.id, done.id} and event["leases"][asking.id] == 15
    assert event["pass"] == 1 and event["gone"] == [] and event["run"] == index.others.run
    for left_out in (ours, old, managed, remote):
        assert left_out.id not in rows

    view = index.others.view()
    assert [row["id"] for row in view["rows"]] == list(rows)
    assert view["rows"][0]["valid_for"] == 15 and view["rows"][1]["valid_for"] is None
    assert (await index.others.find(old.id))["reason"] == "closed"
    assert (await index.others.find(asking.id))["status"]["key"] == "asks"
    for hidden in (ours.id, managed.id, remote.id, str(uuid4())):
        with pytest.raises(LookupError):
            await index.others.find(hidden)


async def test_every_pass_renews_the_leases_of_what_holds(index):
    term = index.terminal()
    term.prompt()
    term.answer()
    behind = index.terminal()
    behind.prompt()
    await index.step()
    first = index.heard()[-1]
    index.clocks.tick(2)
    await index.step()
    (renewal,) = index.heard()
    assert renewal["rows"] == [] and renewal["pass"] == first["pass"] + 1, "unchanged: renewed"
    assert renewal["leases"] == {term.id: 15, behind.id: 15}
    index.clocks.tick(16)  # No pass for a while: nothing holds.
    assert all(row["status"] is None for row in index.others.view()["rows"])
    index.others.logs[behind.id].path = None  # Its log cannot be read any more.
    await index.step()
    (event,) = index.heard()
    assert set(event["leases"]) == {term.id}, "none for a status withdrawn"
    assert [(row["id"], row["status"], row["reason"]) for row in event["rows"]] == [
        (behind.id, None, "reading")
    ]


async def test_what_opens_and_closes_in_a_terminal_is_found(index, monkeypatch):
    calls = []
    listing = index.harness.client.list_sessions

    async def counted():
        calls.append(index.clocks.now)
        return await listing()

    monkeypatch.setattr(index.harness.client, "list_sessions", counted)
    term = index.terminal()
    term.prompt()
    await index.step()
    assert len(calls) == 1
    for _ in range(5):
        index.clocks.tick(2)
        await index.step()
    assert len(calls) == 1, "at most once a minute while nothing changes"
    later = index.terminal()  # Opened in a terminal.
    later.prompt()
    await index.step()
    assert len(calls) == 2 and index.heard()[-1]["rows"][0]["id"] == later.id
    del index.harness.client.in_use[later.id]  # Closed there.
    await index.step()
    assert len(calls) == 2, "not again within two seconds"
    index.clocks.tick(2)
    await index.step()
    assert len(calls) == 3
    index.harness.client.metas.remove(
        next(m for m in index.harness.client.metas if m.session_id == later.id)
    )
    index.clocks.tick(60)
    await index.step()
    assert len(calls) == 4 and index.heard()[-1]["gone"] == [later.id]


async def test_a_session_open_before_copilot_lists_it_is_found_once_it_writes(index, monkeypatch):
    """Copilot lists a session from its first message on, seconds after its lock: one found
    open meanwhile is listed again as soon as its log grows, not a minute later."""
    calls = []
    listing = index.harness.client.list_sessions

    async def counted():
        calls.append(index.clocks.now)
        return await listing()

    monkeypatch.setattr(index.harness.client, "list_sessions", counted)
    await index.step()
    term = Terminal(index.home, index.clocks)  # Its lock and its log's first line, not listed.
    index.harness.client.in_use[term.id] = 4242
    index.clocks.tick(2)
    await index.step()
    assert len(calls) == 2 and index.others.view()["rows"] == []
    index.clocks.tick(2)
    await index.step()
    assert len(calls) == 2, "not again while its log does not grow"
    index.harness.client.metas.append(listed(term.id))  # Its first message: listed from now on.
    term.prompt()
    index.clocks.tick(2)
    await index.step()
    assert len(calls) == 3
    assert [(row["id"], row["status"]["key"]) for row in index.others.view()["rows"]] == [
        (term.id, "working")
    ]
    term.answer()
    index.clocks.tick(2)
    await index.step()
    assert len(calls) == 3, "listed: no more listings for it"


async def test_the_rows_for_a_listing_without_who_made_them_say_why(index):
    term = index.terminal(clients=False)
    term.prompt()
    await index.step()
    view = index.others.view()
    assert view["why"] == "Copilot did not say which sessions are yours."
    assert [row["reason"] for row in view["rows"]] == ["unlisted"]
    assert not index.others.logs, "nothing read for it"


async def test_sessions_of_this_chief_of_staff_are_never_listed(index):
    router = index.harness.router_id
    rotated = index.terminal()  # The Router's earlier Copilot session, as the listing has it.
    index.harness.store.set(f"alias:{rotated.id}", router)
    fresh = index.terminal()
    index.harness.store.set(f"copilot:{router}", fresh.id)
    await index.step()
    assert index.others.view()["rows"] == []


async def test_a_session_continued_here_leaves_the_rows_at_the_next_pass(index, monkeypatch):
    """Continued here (Workers.adopt), it is this Chief of Staff's (rule 1): its own CLI opens
    it, which no lock scan reports, so no listing comes before the next minute's."""
    calls = []
    listing = index.harness.client.list_sessions

    async def counted():
        calls.append(index.clocks.now)
        return await listing()

    monkeypatch.setattr(index.harness.client, "list_sessions", counted)
    term = index.terminal(open_=False)
    term.prompt()
    term.answer()
    term.exit()
    await index.step()
    assert [row["id"] for row in index.others.view()["rows"]] == [term.id]
    index.heard()
    index.harness.store.create_session(term.id, "worker", "Continued", "idle", cwd="/tmp")
    index.clocks.tick(2)
    await index.step()
    assert len(calls) == 1, "without a listing"
    assert index.others.view()["rows"] == [] and index.heard()[-1]["gone"] == [term.id]
    with pytest.raises(LookupError):
        await index.others.find(term.id)


async def test_at_most_two_hundred_sessions_are_followed(index, monkeypatch):
    monkeypatch.setattr(others, "MOST", 3)
    terms = [index.terminal(open_=n == 4, hours=n) for n in range(5)]
    await index.step()
    assert [row["id"] for row in index.others.view()["rows"]] == [
        terms[4].id,
        terms[0].id,
        terms[1].id,
    ]


async def test_nothing_is_read_or_told_without_a_tab_or_copilot(index, monkeypatch):
    term = index.terminal()
    term.prompt()
    index.runtime.bus.queues.clear()
    assert not index.others.eligible()
    index.runtime.bus.queues.add(index.tab)
    index.runtime.status = {"state": "error", "error": "Copilot stopped."}
    assert not index.others.eligible()
    index.runtime.status = {"state": "ready", "error": ""}
    assert index.others.eligible()


# Running with the app: on, off, and off in the middle of a pass


async def until(check, seconds: float = 5):
    for _ in range(int(seconds / 0.01)):
        if result := check():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


class Blocked:
    """read_chunk, held at its second read until released."""

    def __init__(self, monkeypatch):
        self.reads = 0
        self.inside = threading.Event()
        self.release = threading.Event()
        monkeypatch.setattr(others, "read_chunk", self)

    def __call__(self, fd, size, position):
        self.reads += 1
        if self.reads == 2:
            self.inside.set()
            self.release.wait(10)
        return os.pread(fd, size, position)


async def save(runtime: Runtime, **changes):
    """As Settings saves: stored, then applied."""
    old = runtime.store.settings()
    new = old.model_copy(update=changes)
    runtime.store.save_settings(new)
    await runtime.settings_changed(old, new)


async def test_turned_off_in_the_middle_of_a_pass_nothing_more_is_told(index, monkeypatch):
    monkeypatch.setattr(others, "CHUNK", 1024)
    blocked = Blocked(monkeypatch)
    term = index.terminal()
    term.prompt()
    padding(term, 50_000)
    await index.runtime.workers.refresh_elsewhere()
    index.others.start()
    index.runtime.bus.on_subscribe()
    await until(blocked.inside.is_set)
    reading = index.others.reading
    await save(index.runtime, other_sessions=False)
    reads = blocked.reads
    blocked.release.set()
    assert reading
    await asyncio.wait_for(asyncio.shield(reading), 5)
    assert blocked.reads == reads, "the thread stopped after the read it was in"
    await asyncio.sleep(0.05)
    assert index.heard() == [], "nothing told after the save returned"
    assert index.others.view()["rows"] == [] and index.others.job is None
    assert index.runtime.bus.on_subscribe is None
    with pytest.raises(LookupError):
        await index.others.find(term.id)

    # On again: reading starts afresh, one pass at a time.
    await save(index.runtime, other_sessions=True)
    event = await until(lambda: next(iter(index.heard()), None))
    assert event["rows"][0]["status"]["key"] == "working"
    await index.others.stop()
    assert index.others.job is None


async def test_a_long_pass_has_what_got_too_old_withdrawn_meanwhile(index, monkeypatch):
    monkeypatch.setattr(others, "WATCH", 0.01)
    term = index.terminal()
    term.prompt()
    await index.step()
    assert index.heard()[-1]["leases"] == {term.id: 15}
    monkeypatch.setattr(others, "CHUNK", 1024)
    blocked = Blocked(monkeypatch)
    padding(term, 50_000)
    stepping = asyncio.ensure_future(index.step())
    await until(blocked.inside.is_set)
    index.clocks.tick(16)
    withdrawn = await until(lambda: next(iter(index.heard()), None))
    assert withdrawn["rows"][0]["status"] is None and withdrawn["leases"] == {}
    blocked.release.set()
    await stepping


async def test_the_index_runs_with_the_app_and_starts_reading_when_a_tab_connects(tmp_path, clocks):
    harness = Harness(tmp_path / "data")
    harness.client.home = tmp_path / "copilot"
    term = Terminal(tmp_path / "copilot", clocks)
    term.prompt()
    term.ask("Merge it now?")
    harness.client.metas = [listed(term.id, "github/cli")]
    harness.client.in_use = {term.id: 4242}
    runtime = harness.runtime
    await runtime.start()
    try:
        assert runtime.others.job and runtime.bus.on_subscribe == runtime.others.wake
        await runtime.workers.refresh_elsewhere()
        await asyncio.sleep(0.05)
        assert runtime.others.passes == 0, "nothing read without a tab"
        async with runtime.bus.subscribe() as tab:
            payload = await asyncio.wait_for(tab.get(), 5)
            while json.loads(payload)["type"] != "others":
                payload = await asyncio.wait_for(tab.get(), 5)
            (row,) = json.loads(payload)["data"]["rows"]
            assert row["status"]["key"] == "asks" and row["status"]["words"] == "Merge it now?"
    finally:
        await runtime.stop()
    assert runtime.others.job is None and runtime.bus.on_subscribe is None


# Its routes


@pytest.fixture
def served(tmp_path, clocks):
    client = FakeClient()
    client.home = tmp_path / "copilot"
    term = Terminal(client.home, clocks)
    term.prompt()
    term.answer("All green.")
    client.metas = [listed(term.id, "github/cli")]
    client.in_use = {term.id: 4242}

    def factory(store, bus):
        async def notifier(title, text):
            pass

        async def gh(*arguments, payload=None, token=""):
            raise RuntimeError("gh is not run here")

        runtime = Runtime(store, bus, backend_factory=lambda settings: client, notifier=notifier)
        runtime.slack = SlackSource(store, runtime, tokens=FakeTokens(""), mcp=FakeSlackMcp())
        runtime.github = GitHubSource(store, runtime, gh_command=gh)
        return runtime

    app = create_app(tmp_path / "data", "key", 8765, runtime_factory=factory)
    with TestClient(app, base_url="http://127.0.0.1:8765") as http:
        http.term = term
        yield http


def test_its_routes_need_the_launch_key_and_say_nothing_when_off(served):
    term = served.term
    assert served.get("/api/others").status_code == 401
    assert served.get(f"/api/others/{term.id}").status_code == 401
    served.headers["X-Chief-Key"] = "key"
    runtime = served.app.state.runtime
    served.portal.call(runtime.workers.refresh_elsewhere)
    found = served.get(f"/api/others/{term.id}").json()
    assert found["id"] == term.id and found["open"] is True
    assert served.get(f"/api/others/{uuid4()}").status_code == 404
    served.portal.call(runtime.others.step, runtime.others.generation, runtime.others.stopping)
    view = served.get("/api/others").json()
    assert [row["status"]["key"] for row in view["rows"]] == ["your_turn"]
    off = served.post(
        "/api/settings", json={"other_sessions": False}, headers={"origin": "http://127.0.0.1:8765"}
    )
    assert off.status_code == 200 and off.json()["settings"]["other_sessions"] is False
    assert served.get("/api/others").json()["rows"] == []
    assert served.get(f"/api/others/{term.id}").status_code == 404


# The scripted backend's sessions have logs too (tests, the demo)


async def test_the_scripted_backend_writes_its_sessions_logs_where_it_says(tmp_path):
    session_id = str(uuid4())
    log = [
        {"type": "session.start", "data": {"producer": "copilot-agent", "version": 1}},
        {"type": "user.message", "data": {"content": "Profile the orders page"}},
        {"type": "assistant.turn_start", "data": {"turnId": "0"}},
        {"type": "tool.execution_start", "data": {"toolCallId": "c1", "toolName": "bash",
                                                   "arguments": {"command": "make profile"}}},
    ]  # fmt: skip
    meta = listed(session_id, "github/cli")
    backend = ScriptedBackend(others=[{"meta": meta, "open": True, "log": log}], home=tmp_path)
    await backend.start()
    path = backend.session_log(session_id)
    assert path and [json.loads(line) for line in path.read_text().splitlines()] == log
    assert backend.session_log("not-an-id") is None
    assert ScriptedBackend(others=[{"meta": meta, "log": log}]).session_log(session_id) is None
    found = facts(logs={session_id: Log(session_id, path)})[session_id]
    assert where(found, others.clock(), "owner", True, others.clock())[0] == "working"
    assert (await backend.read_history(session_id, 0))[0] == log, "its view reads as before"


# Pins: with the setting off, nothing of this runs

OPENED: list[str] | None = None


def audit(event: str, arguments: tuple):
    if OPENED is not None and event == "open" and isinstance(arguments[0], (str, bytes, Path)):
        OPENED.append(os.fsdecode(arguments[0]))


@pytest.fixture
def opened():
    """Every file opened meanwhile, by any thread (an audit hook stays once added: it records
    only while a test asks)."""
    global OPENED
    if not getattr(sys, "_others_audit", False):
        sys.addaudithook(audit)
        sys._others_audit = True  # type: ignore[attr-defined]
    OPENED = []
    yield OPENED
    OPENED = None


@pytest.mark.parametrize("on", [False, True])
async def test_off_no_log_is_opened_nor_anything_told(tmp_path, clocks, opened, on):
    harness = Harness(tmp_path / "data", Settings(other_sessions=on))
    home = tmp_path / "copilot"
    harness.client.home = home
    sessions = [Terminal(home, clocks) for _ in range(2)]
    for term in sessions:
        term.prompt()
    harness.client.metas = [listed(term.id, "github/cli") for term in sessions]
    harness.client.in_use = {term.id: 4242 for term in sessions}
    lists = []
    listing = harness.client.list_sessions

    async def counted():
        lists.append(1)
        return await listing()

    harness.client.list_sessions = counted
    runtime = harness.runtime
    opened.clear()  # From here on: the app running, a tab connected.
    await runtime.start()
    told = []
    try:
        await runtime.workers.refresh_elsewhere()
        async with runtime.bus.subscribe() as tab:
            await asyncio.sleep(0.3)
            while not tab.empty():
                told.append(json.loads(tab.get_nowait())["type"])
        view = runtime.others.view()
    finally:
        await runtime.stop()
    read = sorted({path for path in opened if path.startswith(str(home))})
    if on:
        assert read == sorted(str(term.path) for term in sessions), "only their logs"
        assert "others" in told and len(lists) == 1 and view["rows"]
    else:
        assert read == [] and "others" not in told and lists == [] and view["rows"] == []


# More of the reducer's edges


def test_a_turn_it_starts_by_itself_is_at_work_from_its_start(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    term.answer()
    clocks.tick(60)
    term.write("system.notification", content="A background command ended.")
    term.write("assistant.turn_start", turnId="1")
    reader = Follow(term)
    reader.step(startup=True)
    shown = reader.shown(term)
    assert (shown["key"], shown["before"], shown["since"]) == ("working", False, iso(clocks.wall))


def test_a_turn_with_more_to_do_does_not_end_with_its_answer(home, clocks):
    queued, background, approval = (Terminal(home, clocks) for _ in range(3))
    for term in (queued, background, approval):
        term.prompt()
    background.call("task", description="Watch the deploy", mode="background")
    approval.write("permission.requested", requestId="r-1", permissionRequest={"kind": "url"})
    for term in (queued, background, approval):
        term.write("assistant.message", content="Here it is.")
    queued.write("user.message", content="Also the docs")
    for term in (queued, background, approval):
        term.write("assistant.turn_end", turnId="0")
    reader = Follow(queued, background, approval)
    reader.step(startup=True)
    assert [reader.status(t) for t in (queued, background, approval)] == [
        "working",
        "working",
        "asks",
    ]


def test_a_rewind_makes_it_partial(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    asked = term.ask()
    term.write("session.snapshot_rewind", eventsRemoved=2, upToEventId=asked)
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "partial"


def test_a_log_dated_in_the_future_is_not_counted_from(home, clocks):
    term = Terminal(home, clocks)
    term.prompt()
    os.utime(term.path, (clocks.wall + 3600, clocks.wall + 3600))
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "waiting", "at work, or no progress? not known yet"
    for _ in range(16):
        clocks.tick(60)
        reader.step()
    assert reader.status(term) == "no_progress", "watched for long enough without growing"


def test_lines_across_reads_are_read_whole_or_passed_over(home, clocks, monkeypatch, capped):
    monkeypatch.setattr(others, "CHUNK", 1024)
    term = Terminal(home, clocks)
    term.prompt()
    term.write("assistant.message", content="Looking. " + "y" * 3000, toolRequests=[{}])
    term.write("session.binary_asset", data="z" * 10_000)
    # A long neutral line whose type is cut at the end of a read.
    size = term.path.stat().st_size
    filler = (
        1024 * (size // 1024 + 2) - 20 - size - len('{"type":"session.info","data":{"x":""}}\n')
    )
    term.line('{"type":"session.info","data":{"x":"' + "f" * filler + '"}}')
    cut_at = term.path.stat().st_size
    assert cut_at % 1024 == 1024 - 20
    term.write("session.binary_asset", data=capped * 2)
    term.answer("All " + "w" * 2000)
    reads = []

    def read(fd, size, position):
        reads.append(position)
        return os.pread(fd, size, position)

    monkeypatch.setattr(others, "read_chunk", read)
    reader = Follow(term)
    reader.step(startup=True)
    assert cut_at in reads, "read again from the start of the line whose type was cut"
    shown = reader.shown(term)
    assert shown["key"] == "your_turn" and shown["words"] == ("All " + "w" * 2000)[:200]
    term.prompt()
    term.write("assistant.message", content=capped, toolRequests=[{}])
    reader.step()
    assert reader.status(term) == "partial", "a line over the cap, across reads, is a gap"


def test_a_session_cut_short_by_a_full_pass_comes_first_in_the_next(home, clocks, monkeypatch):
    monkeypatch.setattr(others, "STEADY_BYTES", 10_000)
    monkeypatch.setattr(others, "SHARE", 4_000)
    sessions = busy(home, clocks, 3, 1000)
    reader = Follow(*sessions)
    reader.step(startup=True)
    for term in sessions:
        padding(term, 9_000)
    before = [reader.logs[t.id].read_to for t in sessions]
    reader.step()
    first = [reader.logs[t.id].read_to - b for t, b in zip(sessions, before, strict=True)]
    assert first[0] >= 4000 and first[1] >= 4000 and 0 < first[2] < 4000, "the third cut short"
    order = []
    read = Log.read

    def reading(log, *arguments):
        order.append(log.session_id)
        return read(log, *arguments)

    monkeypatch.setattr(Log, "read", reading)
    reader.step()
    assert order[0] == sessions[2].id, "it comes first in the next pass"


async def test_passes_run_back_to_back_until_every_session_had_its_first_look(index, monkeypatch):
    monkeypatch.setattr(others, "STARTUP_BYTES", 6_000)
    terms = [index.terminal() for _ in range(2)]
    for term in terms:
        term.prompt()
        padding(term, 5_000)
    await index.step()
    assert index.others.fresh, "one still to look at"
    await index.step()
    assert not index.others.fresh


async def test_off_in_its_settings_it_answers_nothing_whatever_it_holds(index):
    term = index.terminal()
    term.prompt()
    await index.step()
    assert index.others.view()["rows"]
    index.harness.store.save_settings(Settings(other_sessions=False))
    assert index.others.view()["rows"] == []
    with pytest.raises(LookupError):
        await index.others.find(term.id)
    index.others.switch(True)
    assert index.others.job is None, "not before the app runs"


def test_a_turn_runs_from_the_owners_message_across_model_calls(home, clocks):
    term = Terminal(home, clocks)
    term.prompt("Upgrade the parser")
    asked = iso(clocks.wall)
    clocks.tick(30)
    term.done(term.call(command="npm outdated"))  # One model call ends, the next begins.
    clocks.tick(30)
    term.call(command="npm install parser@4")
    reader = Follow(term)
    reader.step(startup=True)
    shown = reader.shown(term)
    assert (shown["key"], shown["since"], shown["elapsed"]) == ("working", asked, 60)


def test_a_long_log_in_another_format_is_known_from_its_first_line(home, clocks, small):
    term = Terminal(home, clocks, head={"producer": "copilot-agent", "version": 2})
    term.prompt()
    padding(term, 30_000)
    reader = Follow(term)
    reader.step(startup=True)
    assert reader.status(term) == "format"
    reader.step()
    assert reader.read == 0, "and not read again"


def test_a_first_look_cut_short_by_its_pass_goes_on_in_the_next(home, clocks, small, monkeypatch):
    monkeypatch.setattr(others, "CHUNK", 512)
    monkeypatch.setattr(others, "HEAD", 1024)
    monkeypatch.setattr(others, "STARTUP_BYTES", 3_000)
    term = Terminal(home, clocks)
    term.prompt("A long one")
    padding(term, 30_000)
    term.prompt("The latest")
    padding(term, term.path.stat().st_size + 6_000)
    reads = []

    def read(fd, size, position):
        reads.append(position)
        return os.pread(fd, size, position)

    monkeypatch.setattr(others, "read_chunk", read)
    reader = Follow(term)
    reader.step(startup=True)
    passes = 1
    while reader.logs[term.id].unread():
        reader.step(startup=True)
        assert reader.read <= 3_000, "within each pass's cap"
        passes += 1
    assert passes > 3 and reader.status(term) == "working"
    floor = term.path.stat().st_size - 8192
    assert min(position for position in reads if position) == floor - 1, "no further back"


def test_a_file_put_in_its_place_after_it_was_sampled_is_looked_at_afresh(home, clocks):
    term = Terminal(home, clocks)
    log = Log(term.id, term.path)
    log.sample(clocks.now, clocks.wall)
    replaced = term.path.with_name("new")
    replaced.write_text(term.path.read_text())
    replaced.replace(term.path)
    assert log.open() is None and log.ident is None and log.problem == ""


async def test_when_copilot_cannot_list_its_sessions_the_last_list_stays(index, caplog):
    term = index.terminal()
    term.prompt()
    await index.step()

    async def failing():
        raise RuntimeError(f"no answer for {term.id}: Make the login test pass")

    index.harness.client.list_sessions = failing
    index.clocks.tick(60)
    await index.step()
    assert [row["id"] for row in index.others.view()["rows"]] == [term.id]
    assert "Could not list Copilot's sessions: RuntimeError at" in caplog.text
    assert "login test" not in caplog.text, "what a session said is never logged"


async def test_a_pass_that_fails_is_logged_without_what_sessions_said(index, caplog, monkeypatch):
    def failing(*arguments):
        raise ValueError("Make the login test pass")

    monkeypatch.setattr(others, "read_pass", failing)
    monkeypatch.setattr(others, "STEADY", 0.01)
    index.others.start()
    index.runtime.bus.on_subscribe()
    await until(lambda: "Reading other Copilot sessions failed" in caplog.text)
    await index.others.stop()
    assert "ValueError at test_others.py" in caplog.text and "login test" not in caplog.text


def test_a_first_look_with_too_little_left_of_its_pass_waits_for_the_next(home, clocks, small):
    term = Terminal(home, clocks)
    term.prompt()
    padding(term, 30_000)
    log = Log(term.id, term.path)
    log.sample(clocks.now, clocks.wall)
    fd = log.open()
    assert fd is not None
    try:
        short = others.Budget(100, lambda: False)  # Less than the first line.
        assert log.look(fd, short) is False and log.head is None and short.used == 0
        log.head = True
        nearly = others.Budget(1000, lambda: False)
        nearly.used = 990  # Less than a model call's start: not even a part of one is read.
        assert log.search(fd, nearly) is None and nearly.used == 990
    finally:
        os.close(fd)


@pytest.mark.parametrize("ended", ["abort", "error"])
@pytest.mark.parametrize("began", ["asked", "by itself"])
def test_a_session_closed_in_a_later_turn_shows_that_turns_step(home, clocks, ended, began):
    term = Terminal(home, clocks)
    term.prompt()
    term.call(command="make build")
    if ended == "abort":
        term.write("abort", reason="user_initiated")
    else:
        term.write("session.error", errorType="query", message="The model is not available.")
    reader = Follow(term)
    if began == "asked":
        term.write("user.message", content="Try again")
        found = reader.step(startup=True)[term.id]
        assert (found.turn, found.ended, found.ended_at) == (True, "", None), "a new turn"
    else:
        term.write("system.notification", content="A background command ended.")
    term.write("assistant.turn_start", turnId="1")
    term.call(command="npm run build")
    term.write("assistant.message", content="Building it now.", toolRequests=[{"name": "bash"}])
    found = reader.step(startup=True)[term.id]
    assert (found.ended, found.ended_at) == ("", None), "the turn before ended; this one runs"
    term.exit()  # Its terminal closed in the middle of the turn.
    reader.step()
    shown = reader.shown(term, is_open=False)
    assert (shown["key"], shown["words"]) == ("stopped", "bash npm run build")


async def test_text_utf8_cannot_hold_is_sent_as_text_it_can(index):
    """JSON can carry a lone surrogate (an emoji cut in half); UTF-8 cannot, and one sent would
    end the event stream of every tab."""
    answered = index.terminal(summary="Fix the \ud83d build")
    answered.prompt()
    answered.answer("Done \ud83d")
    failed = index.terminal()
    failed.prompt()
    failed.write("session.error", errorType="query", message="Broke at \udc00 here")
    running = index.terminal()
    running.prompt()
    running.call(command="echo \ud800")
    await index.step()
    payloads = []
    while not index.tab.empty():
        payloads.append(index.tab.get_nowait())
    for payload in payloads:
        payload.encode("utf-8")
    rows = {row["id"]: row for row in index.others.view()["rows"]}
    json.dumps(rows, ensure_ascii=False).encode("utf-8")  # As the route answers.
    assert rows[answered.id]["summary"] == "Fix the ? build"
    assert rows[answered.id]["status"]["words"] == "Done ?"
    assert rows[failed.id]["status"]["words"] == "Broke at ? here"
    assert rows[running.id]["status"]["words"] == "bash echo ?"


def test_the_route_answers_with_text_utf8_cannot_hold(served):
    client = served.app.state.runtime.backend
    term = Terminal(client.home, served.term.clocks)
    term.prompt()
    term.answer("Done \ud83d")
    meta = listed(term.id, "github/cli", summary="Half an emoji \udc00")
    meta.context = SimpleNamespace(
        working_directory="/work/\ud83d", repository="o/\udc00", branch="fix-\ud800"
    )
    client.metas += [meta, listed("odd-\ud83d", "github/cli")]
    client.in_use[term.id] = 4242
    served.headers["X-Chief-Key"] = "key"
    runtime = served.app.state.runtime
    served.portal.call(runtime.workers.refresh_elsewhere)
    served.portal.call(runtime.others.step, runtime.others.generation, runtime.others.stopping)
    answer = served.get("/api/others")
    assert answer.status_code == 200
    row = next(row for row in answer.json()["rows"] if row["id"] == term.id)
    assert (row["summary"], row["status"]["words"]) == ("Half an emoji ?", "Done ?")
    assert (row["cwd"], row["repository"], row["branch"]) == ("/work/?", "o/?", "fix-?")
    assert "odd-?" in [row["id"] for row in answer.json()["rows"]]
