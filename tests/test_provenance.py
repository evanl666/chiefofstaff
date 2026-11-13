"""Whose a Copilot session is when a worker or a script made it (provenance.py), and
`chiefofstaff tidy-sessions` (tidy.py).

QA on 0.1.39 to 0.1.42: a worker ran scripts that called `copilot -p` hundreds of times; each
call left a session made by github/cli, as the owner's terminal sessions are, and Other Copilot
sessions, the Router and learning took them all for the owner's. The logs here are made up in
the shape Copilot CLI 1.0.110 writes them (its `copilot -p` and its interactive sessions); the
words are invented."""

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import Harness, call_tool

from chiefofstaff import provenance, tidy
from chiefofstaff.backend import CopilotBackend
from chiefofstaff.habits import learned_from
from chiefofstaff.provenance import (
    BATCH,
    MADE_BY_WORKER,
    TALKED,
    UNREAD,
    OneShot,
    Provenance,
    Window,
    batches,
    decide,
    group_of,
    shape,
    whose,
)
from chiefofstaff.store import Store

T0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC).timestamp()
LABEL = (
    "You are labeling a B2B support ticket. Read the ticket below and answer with one of: "
    "billing, outage, feature, other."
)


def iso(seconds: float) -> str:
    """A time as the CLI writes it."""
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds")[:23] + "Z"


def stored(seconds: float) -> str:
    """A time as Chief of Staff stores it (store.now)."""
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def event(kind: str, at: float, agent: str = "", **data) -> str:
    line = {"type": kind, "data": data, "id": str(uuid4()), "timestamp": iso(at)}
    if agent:
        line["agentId"] = agent
    return json.dumps(line, separators=(",", ":"))


def prompt_mode(at: float, prompt: str, cwd: str = "/private/tmp") -> list[str]:
    """What `copilot -p "<prompt>" -s` writes (CLI 1.0.110): its model settled twice, the one
    message, one turn, then it shuts down, all in about two seconds."""
    return [
        event(
            "session.start", at, sessionId="s", version=1, producer="copilot-agent",
            copilotVersion="1.0.110", startTime=iso(at), context={"cwd": cwd},
            alreadyInUse=False,
        ),
        event("session.model_change", at, source="managed_settings", newModel="a-model"),
        event("session.model_change", at, source="sdk", newModel="a-model"),
        event("user.message", at + 0.5, content=prompt, delivery="idle", turnId="0",
              parentAgentTaskId=str(uuid4())),
        event("assistant.turn_start", at + 0.5, turnId="0"),
        event("assistant.message", at + 1.5, content="billing"),
        event("assistant.turn_end", at + 1.5, turnId="0"),
        event("session.usage_checkpoint", at + 1.6),
        event("session.shutdown", at + 1.7, shutdownType="routine"),
    ]  # fmt: skip


def memories(at: float) -> str:
    """What the CLI (1.0.105 on) puts first in a session of an account with memories: a
    user.message of its own, with no words of the owner's."""
    return event(
        "user.message", at, content="", source="memory",
        transformedContent="<memories>\nRecent memories from earlier sessions: …</memories>",
        interactionId=str(uuid4()), agentTaskId=str(uuid4()),
    )  # fmt: skip


def interactive(at: float, *said: str, exit_: bool = True) -> list[str]:
    """A session the owner typed in: its model resolved automatically, the CLI's memories, a
    turn per message."""
    lines = [
        event(
            "session.start", at, sessionId="s", version=1, producer="copilot-agent",
            copilotVersion="1.0.110", startTime=iso(at), context={"cwd": "/work/app"},
            alreadyInUse=False,
        ),
        event("session.model_change", at, source="automatic", newModel="a-model"),
        memories(at),
    ]  # fmt: skip
    for n, text in enumerate(said):
        moment = at + 60 * (n + 1)
        lines += [
            event("user.message", moment, content=text, delivery="idle", turnId=str(n)),
            event("assistant.turn_start", moment, turnId=str(n)),
            event("assistant.message", moment + 20, content="Done."),
            event("assistant.turn_end", moment + 20, turnId=str(n)),
        ]
    if exit_:
        lines.append(event("session.shutdown", at + 3600, shutdownType="routine"))
    return lines


def log(*lines: str) -> bytes:
    return "".join(line + "\n" for line in lines).encode()


# What a log says


def test_a_copilot_p_session_is_one_shot():
    found = shape(log(*prompt_mode(T0, LABEL)))
    assert found.one_shot and found.at == T0 and found.group == group_of(LABEL)


def test_what_is_not_one_shot():
    one = prompt_mode(T0, LABEL)
    assert not shape(log(*interactive(T0, "Fix the login test", "Now run all of them"))).one_shot
    assert not shape(log(*one[:-1])).one_shot, "not ended: it may run, or it was killed"
    assert not shape(log(*one)[:-5]).one_shot, "its last line is being written"
    assert not shape(log(*one) + b'{"type":"session.resume"').one_shot, "continued, being written"
    resumed = [*one, event("session.resume", T0 + 99), event("session.shutdown", T0 + 120)]
    assert not shape(log(*resumed)).one_shot, "continued in a terminal"
    twice = [*one[:4], one[3], *one[4:]]
    assert not shape(log(*twice)).one_shot, "two messages of its own"
    assert not shape(log(*one[1:])).one_shot, "no session.start first"
    start = json.loads(one[0])
    for changed in ({"producer": "another-agent"}, {"version": 2}):
        head = json.dumps({**start, "data": {**start["data"], **changed}})
        assert not shape(log(head, *one[1:])).one_shot, f"another format: {changed}"
    assert not shape(log(*one[:2], "{not json", *one[2:])).one_shot, "a line it cannot read"
    assert not shape(log(*interactive(T0, exit_=True))).one_shot, "no message at all"
    assert shape(b"") == UNREAD


def test_the_clis_memories_are_no_message_of_the_owners():
    """Review: since CLI 1.0.105 a session of an account with memories starts with a
    user.message the CLI writes (no words, source memory): one question asked is still one."""
    assert shape(log(*interactive(T0, "Why does the build fail on CI?"))).one_shot
    assert shape(log(*interactive(T0, "Fix it", "And test it"))) == TALKED
    one = prompt_mode(T0, LABEL)
    assert shape(log(*one[:3], memories(T0), *one[3:])).one_shot, "a script with memories"
    told = event("user.message", T0 + 1, content="The owner works on acme", source="memory")
    assert shape(log(*one[:4], told, *one[4:])).one_shot, "what the CLI tells, with words"
    wordless = event("user.message", T0 + 1, content="  ")
    assert shape(log(*one[:4], wordless, *one[4:])).one_shot, "a message with no words"
    from chiefofstaff.habits import talked_in

    assert not talked_in(said_in(interactive(T0, "Why does the build fail on CI?")))
    assert talked_in(said_in(interactive(T0, "Fix it", "And test it")))


def test_a_sub_agents_message_is_not_its_own_and_another_order_is_read():
    one = prompt_mode(T0, LABEL)
    agents = event("user.message", T0 + 1, agent="explore-1", content="Look at the ticket")
    found = shape(log(*one[:5], agents, *one[5:]))
    assert found.one_shot and found.group == group_of(LABEL)
    moved = json.dumps({"data": json.loads(one[3])["data"], "type": "user.message"})
    assert shape(log(*one[:3], moved, *one[4:])).one_shot, "type not first: parsed"


def test_an_interactive_session_asked_one_question_has_the_same_shape():
    """One question asked in a terminal, then /exit: a log cannot tell it from `copilot -p`, so
    shape alone never takes a session from the owner (see decide)."""
    assert shape(log(*interactive(T0, "Why does the build fail on CI?"))).one_shot


def test_first_messages_start_alike_by_their_first_64_characters():
    assert group_of(LABEL + " Ticket 1: refund") == group_of(LABEL + " Ticket 2: login")
    assert group_of("  You are LABELING\na B2B support ticket. " + LABEL[38:]) == group_of(LABEL)
    assert group_of(LABEL[:63] + "X" + LABEL[64:]) != group_of(LABEL)
    assert group_of(LABEL[:64] + "X") == group_of(LABEL)
    assert group_of("Reply OK") != group_of("Reply OK!"), "short ones: alike only when equal"


# Who made it


def shot(n: int, at: float, group: str = "g", cwd: str = "/private/tmp") -> OneShot:
    return OneShot(f"s{n}", at, group, cwd)


def test_three_alike_within_half_an_hour_of_each_other_are_a_batch():
    assert set(batches([shot(1, T0), shot(2, T0 + 60), shot(3, T0 + 120)])) == {"s1", "s2", "s3"}
    assert batches([shot(1, T0), shot(2, T0 + 60)]) == {}, "two are not a batch"
    gap = provenance.GAP
    assert batches([shot(1, T0), shot(2, T0 + 60), shot(3, T0 + 60 + gap + 1)]) == {}
    assert len(batches([shot(1, T0), shot(2, T0 + 60), shot(3, T0 + 60 + gap)])) == 3
    # Each within half an hour of the one before: one batch, however long it ran.
    chain = [shot(n, T0 + n * (gap - 60)) for n in range(6)]
    assert len(batches(chain)) == 6
    assert batches([shot(1, T0), shot(2, T0 + 60, "h"), shot(3, T0 + 120)]) == {}, "not alike"


def test_a_one_off_is_the_owners_and_alike_ones_a_batch():
    found = decide([shot(1, T0)], [], [])
    assert found == {}, "a single `copilot -p` the owner runs"
    found = decide([shot(n, T0 + n) for n in range(3)], [], [])
    assert {key: record["as"] for key, record in found.items()} == dict.fromkeys(
        ["s0", "s1", "s2"], BATCH
    )
    # One more of a batch decided before (its others are kept, not listed again).
    earlier = [shot(n, T0 + n) for n in range(3)]
    assert decide([shot(9, T0 + 600)], earlier, [])["s9"]["as"] == BATCH


def window(start: float, end: float, *, copilot: bool = False, folder: str = "", by="w1"):
    return Window(by, start, end, copilot, folder)


def test_a_worker_made_it_when_something_ties_it_to_the_worker(tmp_path):
    folder = tmp_path / "workspaces" / "label-tickets-w1"
    folder.mkdir(parents=True)
    real = os.path.realpath(folder)
    long_command = window(T0 - 7200, T0 + 7200)
    # In the worker's own folder, while it ran a command.
    found = decide(
        [shot(1, T0, cwd=str(folder / "out"))], [], [window(T0 - 5, T0 + 5, folder=real)]
    )
    assert found["s1"] | {"at": 0} == {
        "as": MADE_BY_WORKER, "by": "w1", "why": "folder", "group": "g", "at": 0,
    }  # fmt: skip
    # A command that names `copilot`.
    found = decide([shot(1, T0)], [], [window(T0 - 5, T0 + 5, copilot=True, by="w2")])
    assert (found["s1"]["as"], found["s1"]["by"], found["s1"]["why"]) == (
        MADE_BY_WORKER,
        "w2",
        "command",
    )
    # Only the time: the owner's `copilot -p` while a worker's dev server runs is theirs.
    assert decide([shot(1, T0)], [], [long_command]) == {}
    # Not during the command.
    assert decide([shot(1, T0)], [], [window(T0 + 1, T0 + 9, copilot=True)]) == {}
    # Its own folder first, over a command of another worker's that names copilot.
    tied = [window(T0 - 5, T0 + 5, copilot=True, by="w2"), window(T0 - 9, T0 + 9, folder=real)]
    assert decide([shot(1, T0, cwd=str(folder))], [], tied)["s1"]["by"] == "w1"


UP = [[T0 - 100, T0 + 60], [T0 + 3600, T0 + 3700]]  # Stopped from T0+60 to T0+3600.
LOOP = 'for t in tickets/*.txt; do copilot -p "$(cat $t)" -s; done'


def commanded(store: Store, started: float, ended: float | None, command: str = LOOP):
    store.execute(
        "INSERT INTO commands(session_id, call_id, command, started_at, ended_at) "
        "VALUES (?,?,?,?,?)",
        ("w1", f"c{started}", command, stored(started), stored(ended) if ended else None),
    )


@pytest.mark.parametrize("ended", [None, T0 + 3600], ids=["never seen to end", "ended at restart"])
async def test_a_command_window_never_spans_time_chief_of_staff_was_not_running(
    tmp_path, home, ended
):
    """Review: a command running when Chief of Staff stopped has no end, or the one its next
    start gives it (end_commands). What the owner runs meanwhile is theirs; what the command ran
    while Chief of Staff was running stays the worker's."""
    store = Store(tmp_path / "data")
    store.create_session("w1", "worker", "Label the tickets", "idle", cwd="/x")
    store.set("uptime", json.dumps(UP))
    commanded(store, T0 - 30, ended)
    workers = home.add(prompt_mode(T0 + 20, "Label: refund my order"), T0 + 20)
    owners = home.add(prompt_mode(T0 + 600, "Summarize the changelog"), T0 + 600)
    asked = home.add(interactive(T0 + 900, "Why does the build fail on CI?"), T0 + 900)
    made = await deciding(store, home).made(home.metas, {})
    assert made == {workers: MADE_BY_WORKER}, "the owner's, during the downtime, stay theirs"
    assert {owners, asked}.isdisjoint(store.items("made:"))


async def test_chief_of_staff_keeps_its_runs(tmp_path, monkeypatch):
    """Each run, from its start to when it was last known running: touched while it runs and
    when it stops; a restart begins another (what provenance.window_end clips commands to)."""
    from chiefofstaff import runtime as runtime_module
    from chiefofstaff.provenance import runs_of

    monkeypatch.setattr(runtime_module, "ALIVE_EVERY", 0.05)
    data = tmp_path / "data"
    harness = Harness(data)
    await harness.runtime.start()
    (first,) = runs_of(harness.store)
    await asyncio.sleep(0.2)
    (touched,) = runs_of(harness.store)
    assert touched[0] == first[0] and touched[1] > first[1], "touched while it runs"
    monkeypatch.setattr(runtime_module, "ALIVE_EVERY", 3600)
    await asyncio.sleep(0.1)  # Its last touch for a while.
    stopping = time.time()
    await harness.runtime.stop()
    (stopped,) = runs_of(harness.store)
    assert stopped[1] >= stopping, "and when it stops"
    assert harness.runtime.provenance.uptime()[-1] == stopped, "stopped: its run as kept"
    again = Harness(data)
    await again.runtime.start()
    await asyncio.sleep(0.01)
    runs = runs_of(again.store)
    assert len(runs) == 2 and runs[0] == stopped and runs[1][0] >= stopped[1]
    ending = again.runtime.provenance.uptime()[-1][1]
    assert ending > runs[1][1], "the running one runs until now"
    await again.runtime.stop()


async def test_a_command_never_seen_to_end_holds_for_two_hours_at_most(tmp_path, home):
    """A detached command naming copilot whose end Copilot never told: running for at most
    OPEN_MOST, also while Chief of Staff runs (its run ends now); before uptime was kept (data
    of an older version), as long."""
    store = Store(tmp_path / "data")
    store.create_session("w1", "worker", "Label the tickets", "idle", cwd="/x")
    commanded(store, T0, None)
    early = home.add(prompt_mode(T0 + 60, "Label: refund my order"), T0 + 60)
    late = home.add(prompt_mode(T0 + provenance.OPEN_MOST + 60, "Summarize it"), T0 + 9000)
    running = Provenance(store, home.log_of, lambda row: None, uptime=lambda: [(T0 - 9, 2e10)])
    assert await running.made(home.metas, {}) == {early: MADE_BY_WORKER}
    store.execute("DELETE FROM kv WHERE key LIKE 'made:%'")
    assert await deciding(store, home).made(home.metas, {}) == {early: MADE_BY_WORKER}, "no uptime"
    assert late not in store.items("made:")
    # One that began while Chief of Staff was not running is none of its commands.
    store.execute("DELETE FROM kv WHERE key LIKE 'made:%'")
    store.set("uptime", json.dumps([[T0 - 9000, T0 - 8000], [T0 + 9000, T0 + 9999]]))
    assert await deciding(store, home).made(home.metas, {}) == {}


async def test_tidy_sessions_with_chief_of_staff_stopped_leaves_the_owners_alone(
    tmp_path, home, monkeypatch, capsys
):
    monkeypatch.setenv("COPILOT_HOME", str(home.root))
    store = Store(tmp_path / "data")
    store.create_session("w1", "worker", "Label the tickets", "idle", cwd="/x")
    store.set("uptime", json.dumps(UP[:1]))  # Stopped since T0+60.
    commanded(store, T0 - 30, None)
    workers = home.add(prompt_mode(T0 + 20, "Label: refund my order"), T0 + 20)
    owners = home.add(prompt_mode(T0 + 600, "Summarize the changelog"), T0 + 600)
    store.close()
    assert await tidy.run(tmp_path / "data", apply=False, backend=Listing(home)) == 0
    said = capsys.readouterr().out
    assert "1 session a Chief of Staff worker made" in said and "Summarize" not in said
    assert {workers, owners} <= {m.session_id for m in home.metas}


@pytest.mark.parametrize(
    ("command", "runs"),
    [
        ('copilot -p "Label it" -s', True),
        ('for t in *.txt; do /opt/homebrew/bin/copilot -p "$(cat $t)"; done', True),
        ("COPILOT_HOME=$PWD/.copilot-home npx @github/copilot -p hi", True),
        ("ls ~/.copilot/session-state", False),
        ("pip install github-copilot-sdk", False),
        ("python3 copilot_tools.py", False),
        ("python3 label.py", False),
    ],
)
def test_a_command_that_runs_copilot(command, runs):
    assert bool(provenance.RUNS_COPILOT.search(command)) is runs


def test_a_batch_is_a_workers_when_it_began_soon_after_the_workers_command():
    runs = [shot(n, T0 + 30 + n * 3) for n in range(4)]
    script = window(T0, T0 + 600)  # `python3 label.py`, started 30 seconds before
    found = decide(runs, [], [script])
    assert {(r["as"], r["by"], r["why"]) for r in found.values()} == {
        (MADE_BY_WORKER, "w1", "batch")
    }
    # A dev server a worker started hours before: the owner's script's batch, not the worker's.
    server = window(T0 - 3 * 3600, T0 + 3600)
    assert {r["as"] for r in decide(runs, [], [server]).values()} == {BATCH}
    lead = provenance.LEAD
    assert {r["as"] for r in decide(runs, [], [window(T0 + 30 - lead, T0 + 999)]).values()} == {
        MADE_BY_WORKER
    }
    assert {r["as"] for r in decide(runs, [], [window(T0 + 29 - lead, T0 + 999)]).values()} == {
        BATCH
    }


# What a Chief of Staff decides, kept


def listed(session_id: str, at: float, *, cwd: str = "/private/tmp", made: str = "github/cli",
           last: str = "github/cli", summary: str = LABEL) -> SimpleNamespace:  # fmt: skip
    return SimpleNamespace(
        session_id=session_id,
        summary=summary[:80],
        is_remote=False,
        modified_time=datetime.fromtimestamp(at + 2, UTC),
        context=SimpleNamespace(working_directory=cwd, repository="", branch=""),
        client_name=made,
        last_client_name=last,
        clients_listed=True,
    )


class Home:
    """A Copilot home with sessions, and Copilot's listing of them."""

    def __init__(self, root: Path):
        self.root = root
        self.metas: list[SimpleNamespace] = []
        self.open: dict[str, int] = {}

    def log_of(self, session_id: str) -> Path:
        return self.root / "session-state" / session_id / "events.jsonl"

    def add(self, lines: list[str], at: float, **meta) -> str:
        session_id = str(uuid4())
        path = self.log_of(session_id)
        path.parent.mkdir(parents=True)
        path.write_bytes(log(*lines))
        self.metas.append(listed(session_id, at, **meta))
        return session_id

    def batch(self, count: int, start: float = T0, every: float = 20.0, **meta) -> list[str]:
        return [
            self.add(
                prompt_mode(start + n * every, f"{LABEL} Ticket {n}"), start + n * every, **meta
            )
            for n in range(count)
        ]


@pytest.fixture
def home(tmp_path):
    return Home(tmp_path / "copilot")


def deciding(store: Store, home: Home, keep: bool = True) -> Provenance:
    return Provenance(store, home.log_of, lambda row: None, keep=keep)


async def test_decided_once_and_kept_as_its_batch_goes(tmp_path, home):
    store = Store(tmp_path / "data")
    ids = home.batch(4)
    one_off = home.add(prompt_mode(T0 + 7200, "Summarize the changelog for me"), T0 + 7200)
    asked = home.add(interactive(T0, "Why does the build fail on CI?"), T0)
    talked = home.add(interactive(T0, "Fix the login test", "Now run all the tests"), T0)
    made = await deciding(store, home).made(home.metas, {})
    assert made == dict.fromkeys(ids, BATCH), "the one-off and interactive ones are the owner's"
    assert {one_off, asked, talked}.isdisjoint(made)
    assert set(store.items("made:")) == set(ids)
    # Its others moved away (tidied): the last one stays a batch's, read again by no one.
    home.metas = [m for m in home.metas if m.session_id not in ids[:3]]
    fresh = deciding(store, home)
    assert await fresh.made(home.metas, {}) == {ids[3]: BATCH}
    assert ids[3] not in fresh.shapes, "kept: not read again"


async def test_never_while_open_and_looked_at_again_once_continued(tmp_path, home):
    store = Store(tmp_path / "data")
    ids = home.batch(3)
    deciding_ = deciding(store, home)
    assert await deciding_.made(home.metas, {ids[2]: 4242}) == {}, "2 closed: no batch yet"
    assert await deciding_.made(home.metas, {}) == dict.fromkeys(ids, BATCH)
    # Open again in a terminal: the owner's while open.
    assert ids[0] not in await deciding_.made(home.metas, {ids[0]: 4242})
    # Continued there (resumed, a second message): theirs from then on.
    with home.log_of(ids[0]).open("a") as file:
        file.write(event("session.resume", T0 + 999) + "\n")
        file.write(event("user.message", T0 + 1000, content="And the next one?") + "\n")
        file.write(event("session.shutdown", T0 + 1100) + "\n")
    made = await deciding_.made(home.metas, {})
    assert made == dict.fromkeys(ids[1:], BATCH)
    assert set(store.items("made:")) == set(ids[1:])


async def test_what_a_worker_ran_is_the_workers(tmp_path, home):
    harness = Harness(tmp_path / "data")
    store = harness.store
    row = store.create_session("w1", "worker", "Label the support tickets", "idle", cwd="/x")
    folder = harness.runtime.workspaces.own_folder(row)
    assert folder
    store.execute(
        "INSERT INTO commands(session_id, call_id, command, started_at, ended_at) "
        "VALUES (?,?,?,?,?)",
        ("w1", "c1", "python3 label.py --all", stored(T0 - 30), stored(T0 + 600)),
    )
    store.execute(
        "INSERT INTO commands(session_id, call_id, command, started_at, ended_at) "
        "VALUES (?,?,?,?,?)",
        ("w1", "c2", 'copilot -p "Summarize notes.md" -s', stored(T0 + 7000), None),
    )
    batch = home.batch(3)
    alone = home.add(prompt_mode(T0 + 7200, "Summarize notes.md"), T0 + 7200)
    inside = home.add(
        prompt_mode(T0 + 300, "Write the release notes"), T0 + 300, cwd=str(folder / "out")
    )
    owners = home.add(prompt_mode(T0 + 900, "Explain this stack trace"), T0 + 900, cwd="/work")
    deciding_ = Provenance(store, home.log_of, harness.runtime.workspaces.own_folder)
    assert await deciding_.made(home.metas, {}) == dict.fromkeys(
        [*batch, alone, inside], MADE_BY_WORKER
    )
    records = {key: json.loads(value) for key, value in store.items("made:").items()}
    assert {key: (r["by"], r["why"]) for key, r in records.items()} == {
        **dict.fromkeys(batch, ("w1", "batch")),
        alone: ("w1", "command"),
        inside: ("w1", "folder"),
    }
    assert owners not in records, "no command ran then"


async def test_a_session_named_as_a_batchs_is_one_whatever_their_number(tmp_path, home):
    """Workers name each `copilot -p` of a batch `--name "batch: <task>"`: one is enough. Never
    while open, nor once someone talked in it."""
    harness = Harness(tmp_path / "data")
    store = harness.store
    store.create_session("w1", "worker", "Label the support tickets", "idle", cwd="/x")
    store.execute(
        "INSERT INTO commands(session_id, call_id, command, started_at, ended_at) "
        "VALUES (?,?,?,?,?)",
        ("w1", "c1", "python3 label.py", stored(T0 + 7000), stored(T0 + 7400)),
    )
    alone = home.add(prompt_mode(T0, "Label this ticket: refund"), T0)
    home.metas[-1].name = "batch: label the support tickets"
    shouted = home.add(prompt_mode(T0 + 60, "Label this one: where is it"), T0 + 60)
    home.metas[-1].name = "  BATCH:   label the support tickets"
    worker = home.add(prompt_mode(T0 + 7030, "Label this ticket: outage"), T0 + 7030)
    home.metas[-1].name = "batch: Label the support tickets"
    later = home.add(prompt_mode(T0 + 7200, "Label one more ticket"), T0 + 7200)
    home.metas[-1].name = "batch: Label the support tickets"
    opened = home.add(prompt_mode(T0 + 120, "Label it")[:4], T0 + 120)
    home.metas[-1].name = "batch: label the support tickets"
    home.open[opened] = 4242
    continued = home.add(
        [*prompt_mode(T0 + 180, "Label it"), event("session.resume", T0 + 999)], T0 + 180
    )
    home.metas[-1].name = "batch: label the support tickets"
    plain = home.add(prompt_mode(T0 + 240, "Label this ticket: refund"), T0 + 240)
    home.metas[-1].name = "batching notes"
    deciding_ = Provenance(store, home.log_of, harness.runtime.workspaces.own_folder)
    made = await deciding_.made(home.metas, home.open)
    assert made == {alone: BATCH, shouted: BATCH, worker: MADE_BY_WORKER, later: MADE_BY_WORKER}
    records = {key: json.loads(value) for key, value in store.items("made:").items()}
    assert {key: (r["why"], r["by"], r.get("named")) for key, r in records.items()} == {
        alone: ("named", "", True),
        shouted: ("named", "", True),
        worker: ("named", "w1", True),
        later: ("named", "w1", True),
    }
    assert records[alone]["group"] == records[shouted]["group"] != group_of("batch: label")
    # One more of the same name, decided alone later: one of its batch still.
    one_more = home.add(prompt_mode(T0 + 600, "Label the last one"), T0 + 600)
    home.metas[-1].name = "batch: label the support tickets"
    assert (await Provenance(store, home.log_of, lambda row: None).made(home.metas, {})).get(
        one_more
    ) == BATCH
    assert not {opened, continued, plain} & set(made)


async def test_the_cli_says_what_each_session_was_named():
    times = {"startTime": "2026-11-13T06:07:53Z", "modifiedTime": "2026-11-13T06:07:53Z"}
    answer = {"sessions": [
        {"sessionId": "a", **times, "isRemote": False, "clientName": "github/cli",
         "lastClientName": "github/cli",
         "name": "batch: Label the support tickets",
         "summary": "batch: Label the support tickets"},
        {"sessionId": "b", **times, "isRemote": False, "clientName": "github/cli"},
    ]}  # fmt: skip

    class Rpc:
        async def request(self, method, payload):
            return answer

    client = SimpleNamespace(_client=Rpc())
    listed = await CopilotBackend(client).list_sessions()  # type: ignore[arg-type]
    assert [provenance.named(meta) for meta in listed] == ["batch: Label the support tickets", ""]


async def test_only_what_would_be_the_owners_is_looked_at(tmp_path, home):
    store = Store(tmp_path / "data")
    sdk = home.batch(3, made="sdk", last="sdk")
    ours = home.batch(3, made="chiefofstaff", last="chiefofstaff")
    named = home.batch(3, made="acme-notes", last="acme-notes")
    deciding_ = deciding(store, home)
    assert await deciding_.made(home.metas, {}) == dict.fromkeys(named, BATCH)
    assert set(deciding_.shapes) == set(named), "nothing read of the others"
    assert [whose(m, set(), {}) for m in home.metas[:3]] == ["program"] * 3, "as in 0.1.40"
    assert [whose(m, set(), {}) for m in home.metas[3:6]] == ["ours"] * 3
    assert {*sdk, *ours}.isdisjoint(store.items("made:"))


async def test_a_round_reads_within_its_bytes_newest_first(tmp_path, home, monkeypatch):
    store = Store(tmp_path / "data")
    ids = home.batch(6)
    size = home.log_of(ids[0]).stat().st_size
    monkeypatch.setattr(provenance, "ROUND_BYTES", size * 4 + 10)
    deciding_ = deciding(store, home)
    assert await deciding_.made(home.metas, {}) == dict.fromkeys(ids[2:], BATCH)
    # The two left: too few for a batch of their own, they continue the one found.
    assert await deciding_.made(home.metas, {}) == dict.fromkeys(ids, BATCH), "the rest next"
    big = home.add(prompt_mode(T0 + 200, LABEL + "x" * provenance.SHAPE_MOST), T0 + 200)
    assert big not in await deciding_.made(home.metas, {}), "too long to be one shot"
    assert deciding_.shapes[big][1] == UNREAD, "not read"


async def test_many_kept_decisions_cost_little_a_call(tmp_path, home):
    """Review: every listing (each 2 s during a batch), Workers.external and forget parsed every
    decision kept and grouped them all, on the event loop: 0.86 s a call at 50,000."""
    store = Store(tmp_path / "data")
    store.set_many(
        [
            (
                f"made:{uuid4()}",
                json.dumps({"as": "batch", "group": f"g{n % 500}", "at": T0 - n, "size": 1}),
            )
            for n in range(50_000)
        ]
    )
    mine = [home.add(interactive(T0, f"Task {n}", "And test it"), T0) for n in range(37)]
    batch = home.batch(3, start=T0 + 100)
    deciding_ = deciding(store, home)
    assert await deciding_.made(home.metas, {}) == dict.fromkeys(batch, BATCH)
    for count in (1, 2):
        home.batch(1, start=T0 + 160 + count)  # One more of the batch: decided as it comes.
        began = time.perf_counter()
        made = await deciding_.made(home.metas, {})
        took = time.perf_counter() - began
        assert len(made) == 3 + count and took < 0.03, took  # About 4 ms here.
    assert set(mine).isdisjoint(made)


async def test_when_it_cannot_tell_every_session_is_listed_as_before(tmp_path, home, caplog):
    store = Store(tmp_path / "data")
    home.batch(3)

    def broken(session_id):
        raise OSError("disk gone")

    with caplog.at_level(logging.WARNING):
        assert await Provenance(store, broken, lambda row: None).made(home.metas, {}) == {}
    assert "Could not tell which Copilot sessions a worker or a script made: OSError" in caplog.text
    assert LABEL[:20] not in caplog.text


async def test_who_talked_in_a_session_as_far_as_its_log_says(tmp_path, home, monkeypatch):
    """For learning: two messages of its own or a resume (True), one at most (False); a long
    log is read from its start only, where a second message usually is (unknown without)."""
    store = Store(tmp_path / "data")
    monkeypatch.setattr(provenance, "SHAPE_MOST", 4000)
    long = [event("assistant.message", T0 + n, content="x" * 400) for n in range(20)]
    ids = {
        "talked": home.add(interactive(T0, "Fix it", "And test it"), T0),
        "resumed": home.add([*prompt_mode(T0, "Fix it"), event("session.resume", T0 + 9)], T0),
        "one-off": home.add(prompt_mode(T0, "Fix it"), T0),
        "asked": home.add(interactive(T0, "Why?", exit_=False), T0),
        "long, talked first": home.add([*interactive(T0, "Fix it", "Test it"), *long], T0),
        "long, one message": home.add([*interactive(T0, "Fix it"), *long], T0),
        "another format": home.add(prompt_mode(T0, "Fix it")[1:], T0),
    }
    talks = await deciding(store, home).talks([*ids.values(), "no-log"])
    assert {name: talks.get(key) for name, key in ids.items()} == {
        "talked": True, "resumed": True, "one-off": False, "asked": False,
        "long, talked first": True, "long, one message": None, "another format": None,
    }  # fmt: skip


# The sidebar, the Router and learning, alike


async def connected(tmp_path, home: Home) -> Harness:
    harness = Harness(tmp_path / "data")
    harness.client.home = home.root
    harness.client.metas = home.metas
    harness.client.in_use = home.open
    await harness.connect()
    return harness


async def test_a_scripts_batch_takes_no_room_in_the_list_nor_reaches_the_router(tmp_path, home):
    """QA: 38 of the 40 rows were a script's. 500 of them, newer than the owner's 40 sessions:
    the list still has the owner's 40, the Router finds none of the script's."""
    mine = [
        home.add(
            interactive(T0 - 86400 + n * 60, f"Task {n}: fix it", "And test it"),
            T0 - 1000,
            summary=f"Task {n}",
        )
        for n in range(39)
    ]
    one_off = home.add(prompt_mode(T0 - 500, "Summarize the changelog"), T0 - 500)
    script = home.batch(500, start=T0, every=3)
    harness = await connected(tmp_path, home)
    items = await harness.runtime.workers.external(refresh=True)
    assert len(items) == 40 and {item["id"] for item in items} == {*mine, one_off}
    with pytest.raises(ValueError, match="No session"):
        await harness.runtime.toolset.read_terminal_session(
            SimpleNamespace(session_id=script[0], items=5)
        )
    reply = await call_tool(
        harness.runtime, "message_session", harness.router_id, session_id=script[-1], text="Go on"
    )
    assert "No session" in reply.text_result_for_llm
    await harness.runtime.stop()


async def test_the_index_and_the_list_agree(tmp_path, home, monkeypatch):
    from chiefofstaff import others

    monkeypatch.setattr(others, "wall", lambda: T0 + 3600)
    batch = home.batch(3)
    running = home.add(prompt_mode(T0 + 100, f"{LABEL} Ticket 9")[:5], T0 + 100)
    home.open[running] = 4242
    # A worker's call named as its batch's, still running: no row for it meanwhile.
    named_running = home.add(prompt_mode(T0 + 110, "Label this ticket")[:5], T0 + 110)
    home.metas[-1].name = "batch: Label the support tickets"
    home.open[named_running] = 4343
    asked = home.add(interactive(T0, "Why does the build fail on CI?"), T0)
    routers = home.add(interactive(T0, "Plan the week", "And the next"), T0 + 50)
    harness = await connected(tmp_path, home)
    runtime = harness.runtime
    # The Copilot session its Router runs on now: Chief of Staff's own, for both.
    harness.store.set(f"copilot:{harness.router_id}", routers)
    runtime.bus.queues.add(asyncio.Queue())
    runtime.store.save_settings(
        runtime.store.settings().model_copy(update={"other_sessions": True})
    )
    await runtime.workers.refresh_elsewhere()
    await runtime.others.step(runtime.others.generation, runtime.others.stopping)
    rows = [row["id"] for row in runtime.others.view()["rows"]]
    items = [item["id"] for item in await runtime.workers.external(refresh=True)]
    assert rows == items == [running, asked], "open (still running) first; never the batch"
    for session_id in [*batch, named_running]:
        with pytest.raises(LookupError):
            await runtime.others.find(session_id)
    # One of them continued in a terminal: the owner's while open, before the next listing.
    home.open[batch[0]] = 4343
    await runtime.workers.refresh_elsewhere()
    found = await runtime.others.find(batch[0])
    assert (found["open"], found["reason"]) == (True, "reading")
    runtime.others.track()
    assert [row["id"] for row in runtime.others.view()["rows"]] == [running, batch[0], asked]
    assert (await runtime.others.find(batch[0]))["open"] is True
    await runtime.stop()


def said_in(lines: list[str]) -> list[dict]:
    """A log's events, as Copilot's history gives them to habits."""
    return [json.loads(line) for line in lines]


async def test_learning_takes_words_only_from_sessions_the_owner_talked_in(tmp_path, home):
    """QA on 0.1.42: after a restart the learner took in more of a script's prompts as the
    owner's instructions. Words are read only from sessions the owner talked in (two messages
    or more): never a script's, a worker's, nor a one-shot one of the owner's own (a
    `copilot -p`, one question asked and left), which stay listed."""
    logs = {
        "talked": interactive(T0 - 900, "Fix the flaky login test please", "Now run it 20 times"),
        "asked": interactive(T0 - 800, "Why does the build fail on CI today?"),
        "one-off": prompt_mode(T0 - 700, "Summarize the changelog for the release notes"),
        "running": interactive(T0 - 600, "Profile the slow orders page now", exit_=False),
        "named": prompt_mode(T0 - 500, "Label this ticket: refund my order"),
    }
    logs["named, running"] = interactive(T0 - 400, "Label this ticket", "And this one", exit_=False)
    ids = {name: home.add(lines, T0) for name, lines in logs.items()}
    home.metas[-2].name = home.metas[-1].name = "batch: Label the tickets"
    home.open[ids["running"]] = 4242
    home.open[ids["named, running"]] = 4343
    batch = home.batch(3)
    harness = await connected(tmp_path, home)
    for name, session_id in ids.items():
        harness.client.logs[session_id] = said_in(logs[name])
    for session_id in batch:
        harness.client.logs[session_id] = said_in(prompt_mode(T0, f"{LABEL} Ticket"))
    listed = {item["id"] for item in await harness.runtime.workers.external(refresh=True)}
    assert listed == {ids[name] for name in ("talked", "asked", "one-off", "running")}
    await harness.runtime.habits.collect_sessions()
    words = harness.store.all("SELECT ref, text FROM observations ORDER BY id")
    assert [(row["ref"].split(":")[1], row["text"]) for row in words] == [
        (ids["talked"], "Fix the flaky login test please"),
        (ids["talked"], "Now run it 20 times"),
    ]
    # Talked in since (a second message): read from then on, its first message too.
    path = home.log_of(ids["running"])
    more = event("user.message", T0, content="And batch the queries too")
    path.write_bytes(path.read_bytes() + log(more))
    harness.client.logs[ids["running"]].append(json.loads(more))
    await harness.runtime.habits.collect_sessions()
    assert len(harness.store.all("SELECT 1 FROM observations")) == 4
    await harness.runtime.stop()


async def test_without_a_log_of_its_own_what_is_read_says_whether_someone_talked(tmp_path):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    harness.client.metas = [listed(name, T0) for name in ("once", "twice")]
    harness.client.logs = {
        name: said_in(lines)
        for name, lines in {
            "once": prompt_mode(T0, "Summarize the changelog for the release"),
            "twice": interactive(T0, "Fix the flaky login test", "Then run it 20 times"),
        }.items()
    }
    assert harness.client.session_log("once") is None
    await runtime.habits.collect_sessions()
    rows = harness.store.all("SELECT ref FROM observations ORDER BY id")
    assert [row["ref"].split(":")[1] for row in rows] == ["twice", "twice"]
    await runtime.stop()


@pytest.mark.parametrize("why", ["the listing failed", "the listing does not say whose"])
async def test_a_learning_run_that_could_not_look_leaves_copilot_words_out(
    tmp_path, monkeypatch, why
):
    """Review: when what was queued could not be looked at (a reconnect, a fallback listing),
    a learning run still took it: 0.1.42's queued batch prompts could reach the first run
    after the upgrade. They wait for a run that can look; the owner's other words go."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    habits, store = runtime.habits, harness.store
    habits.add("copilot", "instruction", "copilot:s1:e1", "You label support tickets for acme")
    habits.add("chief", "instruction", "chief:1", "Answer me briefly, the conclusion first")
    if why == "the listing failed":

        async def failing():
            raise RuntimeError("Copilot is reconnecting")

        monkeypatch.setattr(harness.client, "list_sessions", failing)
    else:
        harness.client.metas = [listed("s1", T0, made="github/cli")]
        harness.client.metas[0].clients_listed = False
    await runtime.learning.learn()
    given = json.loads(store.get("learning_observations") or "[]")
    rows = store.all("SELECT id, ref FROM observations")
    assert [row["ref"] for row in rows if row["id"] in given] == ["chief:1"]
    habits.mark_learned(store.all("SELECT id, kind FROM observations WHERE ref='chief:1'"))
    assert store.one("SELECT learned FROM observations WHERE ref='copilot:s1:e1'")["learned"] == 0
    await runtime.stop()


async def test_what_was_queued_is_dropped_before_learning_and_once_at_start(
    tmp_path, home, monkeypatch
):
    """QA: 17 of a script's prompts waited to be learned from. They and the owner's one-shot
    prompts go before any learning run (one a piece of feedback starts too) and once after a
    start; what was learned stays and is named; the owner's own words stay."""
    from chiefofstaff import habits as habits_module

    batch = home.batch(3)
    asked = home.add(interactive(T0, "Why does the build fail on CI?"), T0)
    one_off = home.add(prompt_mode(T0 + 7200, "Summarize the changelog"), T0 + 7200)
    talked = home.add(interactive(T0, "Fix the login test", "Now run all of them"), T0)
    running = home.add(interactive(T0, "Profile the orders page", exit_=False), T0)
    home.open[running] = 4242
    named_running = home.add(prompt_mode(T0, "Label this ticket")[:5], T0)
    home.metas[-1].name = "batch: Label the support tickets"
    home.open[named_running] = 4343
    harness = await connected(tmp_path, home)
    habits, store = harness.runtime.habits, harness.store

    # A script's session moved away (tidied): what was read from it is not the owner's either.
    moved = str(uuid4())
    store.set(f"made:{moved}", json.dumps({"as": "batch", "group": "g", "at": T0, "size": 1}))

    def queue():
        for n, session_id in enumerate(
            [*batch, asked, one_off, talked, running, named_running, moved]
        ):
            for e in ("e1", "e2"):
                habits.add("copilot", "instruction", f"copilot:{session_id}:{e}", f"Words {n}{e}")

    def queued() -> set[str]:
        rows = store.all("SELECT ref FROM observations WHERE learned=0")
        return {row["ref"].split(":")[1] for row in rows}

    queue()
    learned = [batch[0], batch[1]]
    for session_id in learned:
        store.execute(
            "UPDATE observations SET learned=1 WHERE ref LIKE ?", (f"copilot:{session_id}:%",)
        )
    ids = [r["id"] for r in store.all("SELECT id FROM observations WHERE learned=1")]
    mine = store.one("SELECT id FROM observations WHERE ref=?", (f"copilot:{talked}:e1",))["id"]
    store.execute(
        "INSERT INTO style_versions(section, text, evidence, status, created_at) VALUES "
        "('chat', 'Short answers.', ?, 'active', 'now')",
        (json.dumps([*ids[:3], mine]),),
    )
    store.execute(
        "INSERT INTO lessons(id, text, scope, evidence, observed, status, created_at, updated_at)"
        " VALUES ('l1', 'Label in one word', '', '[]', ?, 'proposed', 'now', 'now')",
        (json.dumps(ids[3:]),),
    )
    # A learning run (one a piece of feedback starts, say): what it is given.
    await harness.runtime.learning.learn()
    given = json.loads(store.get("learning_observations") or "[]")
    rows = store.all("SELECT id, ref FROM observations")
    sessions = {row["ref"].split(":")[1] for row in rows if row["id"] in given}
    assert sessions == {talked, running}, "the owner's own words only"
    assert queued() == {talked, running}, "open: looked at again later"
    assert learned_from(store, batch) == [
        {"kind": "style", "name": "Answers and reports to you", "version": 1, "from_them": 3,
         "examples": 4},
        {"kind": "habit", "name": "Label in one word", "status": "proposed", "from_them": 1,
         "examples": 1},
    ]  # fmt: skip
    assert learned_from(store, [talked]) == [], "learned from the owner's own: nothing to name"
    assert store.one("SELECT status FROM style_versions")["status"] == "active", "nothing undone"
    assert len(store.all("SELECT 1 FROM observations WHERE learned=1")) == 4, "learned stays"

    # Queued again (by 0.1.42, say), then a restart: dropped once Copilot is connected.
    store.execute("DELETE FROM observations WHERE learned=0")
    queue()
    monkeypatch.setattr(habits_module, "FIRST_FORGET_DELAY", 0)
    monkeypatch.setattr(habits_module, "FIRST_COLLECT_DELAY", 3600)
    looked = asyncio.Event()
    forget_queued = habits.forget_queued

    async def tracked():
        await forget_queued()
        looked.set()

    monkeypatch.setattr(habits, "forget_queued", tracked)

    async def started():
        looked.clear()
        await habits.start()
        await asyncio.wait_for(looked.wait(), 10)
        await habits.stop()

    store.save_settings(store.settings().model_copy(update={"learn_from_work": False}))
    await started()
    everyone = {batch[2], asked, one_off, talked, running, named_running, moved}
    assert queued() == everyone, "learning off: not looked at"
    store.save_settings(store.settings().model_copy(update={"learn_from_work": True}))
    await started()
    assert queued() == {talked, running}
    await harness.runtime.stop()


# tidy-sessions


class Listing:
    """Copilot's listing as tidy-sessions asks it (backend.list_sessions, open_elsewhere)."""

    def __init__(self, home: Home, clients: bool = True):
        self.home, self.clients = home, clients

    async def list_sessions(self):
        return [
            SimpleNamespace(**{**vars(m), "clients_listed": self.clients}) for m in self.home.metas
        ]

    async def open_elsewhere(self):
        return dict(self.home.open)


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every file under a folder: its size and change time (not SQLite's -wal and -shm files,
    which a reader of a database in WAL mode makes)."""
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(("-wal", "-shm"))
    }


@pytest.fixture
def tidying(tmp_path, home, monkeypatch):
    """A Chief of Staff's data (one worker, one command of its), its owner's Copilot home:
    a worker's batch, a script's batch, the owner's sessions, all old enough to move."""
    monkeypatch.setenv("COPILOT_HOME", str(home.root))
    for name in tidy.AGENT_SHELL:
        monkeypatch.delenv(name, raising=False)
    store = Store(tmp_path / "data")
    store.create_session("w1", "worker", "Label the support tickets", "idle", cwd="/x")
    store.execute(
        "INSERT INTO commands(session_id, call_id, command, started_at, ended_at) "
        "VALUES (?,?,?,?,?)",
        ("w1", "c1", "python3 label.py", stored(T0 - 30), stored(T0 + 600)),
    )
    worker = home.batch(3)
    translate = "Translate the text below into French. Keep its tone and its length. Text"
    script = [
        home.add(
            prompt_mode(T0 + 7200 + n, f"{translate} {n}"),
            T0 + 7200 + n,
            summary=f"Translate into French {n}",
        )
        for n in range(3)
    ]
    mine = home.add(interactive(T0, "Why does the build fail on CI?"), T0, summary="Build")
    one_off = home.add(prompt_mode(T0 + 9000, "Summarize the changelog"), T0 + 9000)
    sdk = home.batch(3, start=T0 + 9999, made="sdk", last="sdk")
    for session_id in [*worker, *script]:
        store.execute(
            "INSERT INTO observations(source, kind, ref, text, created_at) VALUES "
            "('copilot', 'instruction', ?, 'A prompt of theirs', 'now')",
            (f"copilot:{session_id}:e1",),
        )
    store.execute(
        "INSERT INTO observations(source, kind, ref, text, learned, created_at) VALUES "
        "('copilot', 'instruction', ?, 'Learned before', 1, 'now')",
        (f"copilot:{worker[0]}:e2",),
    )
    learned = store.value("SELECT max(id) FROM observations")
    store.execute(
        "INSERT INTO observations(source, kind, ref, text, created_at) VALUES "
        "('copilot', 'instruction', ?, 'Mine', 'now')",
        (f"copilot:{mine}:e1",),
    )
    store.execute(
        "INSERT INTO style_versions(section, text, evidence, status, created_at) VALUES "
        "('chat', 'Short answers.', ?, 'active', 'now')",
        (json.dumps([learned]),),
    )
    store.close()
    old = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    for path in home.root.rglob("*"):
        os.utime(path, (old, old))
    return SimpleNamespace(
        data=tmp_path / "data", home=home, worker=worker, script=script, mine=mine,
        one_off=one_off, sdk=sdk,
    )  # fmt: skip


def unlearned_refs(data: Path) -> set[str]:
    with sqlite3.connect(data / "chiefofstaff.db") as db:
        return {ref for (ref,) in db.execute("SELECT ref FROM observations WHERE learned=0")}


async def test_tidy_sessions_shows_what_it_would_move_and_changes_nothing(tidying, capsys):
    home = tidying.home
    before, data = snapshot(home.root), snapshot(tidying.data)
    unlearned = unlearned_refs(tidying.data)
    database = tidying.data / "chiefofstaff.db"
    database.chmod(0o444)  # Read only: it is not written to.
    try:
        assert await tidy.run(tidying.data, apply=False, backend=Listing(home)) == 0
    finally:
        database.chmod(0o600)
    said = capsys.readouterr().out
    assert snapshot(home.root) == before and snapshot(tidying.data) == data, "nothing changed"
    assert unlearned_refs(tidying.data) == unlearned and len(unlearned) == 7
    with sqlite3.connect(tidying.data / "chiefofstaff.db") as db:
        assert db.execute("SELECT count(*) FROM kv WHERE key LIKE 'made:%'").fetchone() == (0,)
    assert "3 sessions a Chief of Staff worker made (“Label the support tickets”)" in said
    assert "3 sessions of a script's batch (one-shot, alike)" in said
    assert "e.g. “Translate into French 0”" in said and ", in /private/tmp\n" in said
    assert said.count(f"e.g. “{LABEL[:71]}…”") == 1, "a batch's start the same: shown once"
    assert "6 prompts of theirs, read as yours and not learned from yet, would be dropped" in said
    assert "How you write, Answers and reports to you (version 1): 1 of its 1 example" in said
    assert "Nothing was changed. To move 6 sessions" in said
    assert "chiefofstaff tidy-sessions --apply" in said
    assert tidying.mine not in said and LABEL[:30] in said


async def test_tidy_sessions_moves_them_where_they_can_be_had_again(tidying, capsys):
    home = tidying.home
    kept = {tidying.mine, tidying.one_off, *tidying.sdk}
    before = snapshot(home.root / "session-state" / tidying.worker[0])
    # One open in a terminal now, one changed a minute ago: they stay.
    busy, recent = tidying.script[0], tidying.script[1]
    (home.root / "session-state" / busy / f"inuse.{os.getpid()}.lock").write_text("")
    os.utime(home.log_of(recent))
    assert await tidy.run(tidying.data, apply=True, backend=Listing(home)) == 0
    said = capsys.readouterr().out
    state = home.root / "session-state"
    moved = list((home.root / tidy.MOVED).iterdir())
    assert len(moved) == 1 and "Moved 4 sessions" in said and str(moved[0]) in said
    manifest = json.loads((moved[0] / "manifest.json").read_text())
    entries = {entry["id"]: entry for entry in manifest["sessions"]}
    assert set(entries) == {*tidying.worker, tidying.script[2]}
    assert entries[tidying.worker[0]] | {"began": "", "bytes": 0} == {
        "id": tidying.worker[0], "made_by": "worker", "worker": "Label the support tickets",
        "worker_session": "w1", "why": "batch", "summary": LABEL[:80], "cwd": "/private/tmp",
        "began": "", "bytes": 0,
    }  # fmt: skip
    assert manifest["from"] == str(state)
    for session_id in kept | {busy, recent}:
        assert (state / session_id / "events.jsonl").exists(), "left where it was"
    for session_id in entries:
        assert not (state / session_id).exists() and (moved[0] / session_id).is_dir()
    assert "Left where they are: 1 session open now, 1 session changed in the last hour." in said
    # Their prompts not learned from yet are dropped; the owner's and what was learned stay.
    assert unlearned_refs(tidying.data) == {f"copilot:{tidying.mine}:e1"}
    assert "Dropped 6 prompts of theirs not learned from yet." in said
    with sqlite3.connect(tidying.data / "chiefofstaff.db") as db:
        assert db.execute("SELECT count(*) FROM observations WHERE learned=1").fetchone() == (1,)
        assert db.execute("SELECT status FROM style_versions").fetchone() == ("active",)
    # Who made them is kept: what was learned from them stays named once they are gone.
    with sqlite3.connect(tidying.data / "chiefofstaff.db") as db:
        kept = dict(db.execute("SELECT key, value FROM kv WHERE key LIKE 'made:%'").fetchall())
    assert {key.removeprefix("made:") for key in kept} == {*tidying.worker, *tidying.script}
    assert json.loads(kept[f"made:{tidying.worker[0]}"])["by"] == "w1"
    capsys.readouterr()
    for session_id in entries:
        home.metas = [m for m in home.metas if m.session_id != session_id]
    assert await tidy.run(tidying.data, apply=False, backend=Listing(home)) == 0
    said = capsys.readouterr().out
    assert "2 sessions of a script's batch" in said, "the two left, still a batch's"
    assert "How you write, Answers and reports to you (version 1): 1 of its 1 example" in said
    home.metas = [m for m in home.metas if m.session_id not in tidying.script]
    assert await tidy.run(tidying.data, apply=True, backend=Listing(home)) == 0
    said = capsys.readouterr().out
    assert "No Copilot sessions a worker or a script made in" in said and "Moved" not in said
    assert "How you write, Answers and reports to you (version 1): 1 of its 1 example" in said
    # Moved back, a session is as it was.
    (moved[0] / tidying.worker[0]).rename(state / tidying.worker[0])
    assert snapshot(state / tidying.worker[0]) == before


async def test_tidy_sessions_leaves_one_that_changed_since_it_looked(tidying, monkeypatch):
    home, looked = tidying.home, tidy.survey

    async def then_continued(*arguments):
        found = await looked(*arguments)
        with home.log_of(tidying.script[0]).open("a") as file:
            file.write(event("session.resume", T0) + "\n")
        os.utime(home.log_of(tidying.script[0]), (T0, T0))
        return found

    monkeypatch.setattr(tidy, "survey", then_continued)
    assert await tidy.run(tidying.data, apply=True, backend=Listing(home)) == 0
    assert (home.root / "session-state" / tidying.script[0]).is_dir()
    assert not (home.root / "session-state" / tidying.script[1]).exists()


async def test_tidy_sessions_changes_nothing_when_it_cannot_tell(tidying, tmp_path, capsys):
    home = tidying.home
    before = snapshot(home.root)
    assert await tidy.run(tidying.data, apply=True, backend=Listing(home, clients=False)) == 1
    assert "cannot be told apart. Nothing was changed." in capsys.readouterr().err
    assert await tidy.run(tmp_path / "nowhere", apply=True, backend=Listing(home)) == 2
    assert "No Chief of Staff data in" in capsys.readouterr().err
    assert snapshot(home.root) == before


async def test_tidy_sessions_works_with_the_data_of_an_older_chief_of_staff(tidying, capsys):
    """Data last used by a Chief of Staff from before it learned from the owner's words: no
    observations to drop or name, and nothing in it updated."""
    with sqlite3.connect(tidying.data / "chiefofstaff.db") as db:
        for table in ("observations", "style_versions", "lessons"):
            db.execute(f"DROP TABLE {table}")
    assert await tidy.run(tidying.data, apply=False, backend=Listing(tidying.home)) == 0
    said = capsys.readouterr().out
    assert "3 sessions of a script's batch" in said and "prompt" not in said
    assert await tidy.run(tidying.data, apply=True, backend=Listing(tidying.home)) == 0
    assert "Moved 6 sessions" in capsys.readouterr().out


def test_tidy_sessions_moves_nothing_from_a_copilot_sessions_shell(tidying, monkeypatch, capsys):
    """A worker's commands run in a Copilot session's shell: moving the owner's sessions takes
    the owner, in their own terminal."""
    monkeypatch.setenv("COPILOT_CLI", "1")
    before = snapshot(tidying.home.root)
    assert tidy.main(["--apply", "--data-dir", str(tidying.data)]) == 2
    assert "Run it in your own terminal" in capsys.readouterr().err
    assert snapshot(tidying.home.root) == before


def test_the_command_runs_it(monkeypatch):
    seen = []
    monkeypatch.setattr(tidy, "main", lambda arguments: seen.append(arguments) or 0)
    monkeypatch.setattr("sys.argv", ["chiefofstaff", "tidy-sessions", "--apply"])
    from chiefofstaff import cli

    assert cli.main() == 0 and seen == [["--apply"]]


def test_tidy_sessions_says_which_were_named_as_a_batchs(tmp_path):
    item = tidy.Found(
        "s1", BATCH, {"as": BATCH, "why": "named", "named": True, "group": "n", "at": T0},
        "batch: Label the tickets", str(Path.home() / "code" / "app"), tmp_path, size=2048,
    )  # fmt: skip
    lines = tidy.report(tidy.Survey(tmp_path, found=[item]))
    assert lines[2:5] == [
        "  1 session named as a batch's, 2.0 KB",
        f"    {tidy.when(T0)}, in ~/code/app",
        "    e.g. “batch: Label the tickets”",
    ]


def test_what_a_session_said_is_printed_safely():
    assert tidy.shown("Do it\x1b[2J\x1b]0;owned\x07 now\u202e!") == "Do it [2J ]0;owned now !"
    assert tidy.shown("x" * 100, 10) == "xxxxxxxxx…"
