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
import random
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
    ALIKE,
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


def alike(count: int, every: float = 20.0, start: float = T0, group: str = "g") -> list[OneShot]:
    return [shot(n, start + n * every, group) for n in range(count)]


def test_five_alike_each_within_two_minutes_of_the_one_before_are_alike():
    assert set(batches(alike(5))) == {f"s{n}" for n in range(5)}
    assert batches(alike(4)) == {}, "four are not"
    gap = provenance.GAP
    assert len(batches(alike(5, every=120))) == 5, "two minutes apart"
    assert batches(alike(5, every=121)) == {}
    split = [*alike(3), *[shot(n, T0 + 40 + gap + 1 + n, "g") for n in range(3, 6)]]
    assert batches(split) == {}, "three, then three more after a pause: neither five"
    # Each within two minutes of the one before: one, however long it ran (three hours).
    assert len(batches(alike(91, every=gap))) == 91
    assert batches([*alike(4), shot(9, T0 + 60, "h")]) == {}, "not alike"


def test_a_few_alike_are_the_owners_and_ten_a_scripts_batch():
    """E49: the owner asks alike questions too, a few at a time; a script runs its calls by the
    dozen. Alike, fewer than ten: the owner's still (`alike`: tidy-sessions names them)."""

    def kinds(found: dict) -> dict:
        return {key: record["as"] for key, record in found.items()}

    assert decide([shot(1, T0)], [], []) == {}, "a single `copilot -p` the owner runs"
    assert decide(alike(4), [], []) == {}
    assert kinds(decide(alike(5), [], [])) == dict.fromkeys([f"s{n}" for n in range(5)], ALIKE)
    assert set(kinds(decide(alike(9), [], [])).values()) == {ALIKE}
    ten = decide(alike(10), [], [])
    assert kinds(ten) == dict.fromkeys([f"s{n}" for n in range(10)], BATCH)
    assert ten["s0"] | {"at": 0} == {
        "as": BATCH, "by": "", "why": "batch", "group": "g", "at": 0, "rules": provenance.RULES,
    }  # fmt: skip
    # One more of a batch decided before (its others are kept, not listed again).
    assert decide([shot(99, T0 + 300)], alike(10), [])["s99"]["as"] == BATCH
    # Named a batch's: one is enough.
    assert kinds(decide([OneShot("n1", T0, "n", "/w", named=True)], [], [])) == {"n1": BATCH}


def window(start: float, end: float, *, copilot: bool = False, folder: str = "", by="w1",
           places: tuple[str, ...] = ()) -> Window:  # fmt: skip
    return Window(by, start, end, copilot, folder, places)


def test_a_worker_made_it_when_something_ties_it_to_the_worker(tmp_path, monkeypatch):
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
        "rules": provenance.RULES, "keeps_open": True,
    }  # fmt: skip
    # A command that runs `copilot -p`, in a temporary folder.
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
    # Its own folder first, over a command of another worker's that runs copilot.
    tied = [window(T0 - 5, T0 + 5, copilot=True, by="w2"), window(T0 - 9, T0 + 9, folder=real)]
    assert decide([shot(1, T0, cwd=str(folder))], [], tied)["s1"]["by"] == "w1"
    # E54: where the command ran, or a temporary folder ($TMPDIR too): not the owner's project.
    loop = [window(T0 - 5, T0 + 5, copilot=True, places=("/srv/w1", "/srv/labels"))]
    assert decide([shot(1, T0, cwd="/w/app")], [], loop) == {}
    assert decide([shot(1, T0, cwd="/srv/labels")], [], loop)["s1"]["why"] == "command"
    assert decide([shot(1, T0, cwd="/srv/w1")], [], loop)["s1"]["why"] == "command"
    assert decide([shot(1, T0, cwd="/srv/labels/x")], [], loop) == {}, "where it ran, only"
    monkeypatch.setenv("TMPDIR", str(tmp_path / "t"))
    monkeypatch.setattr(provenance.tempfile, "gettempdir", lambda: str(tmp_path / "t"))
    assert decide([shot(1, T0, cwd=str(tmp_path / "t" / "x1"))], [], loop)["s1"]["by"] == "w1"
    assert decide([shot(1, T0, cwd="/private/tmp/x2")], [], loop)["s1"]["by"] == "w1"


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
    """A detached command running copilot -p whose end Copilot never told: running for at most
    COMMAND_MOST, also while Chief of Staff runs (its run ends now); before uptime was kept
    (data of an older version), as long."""
    store = Store(tmp_path / "data")
    store.create_session("w1", "worker", "Label the tickets", "idle", cwd="/x")
    commanded(store, T0, None)
    early = home.add(prompt_mode(T0 + 60, "Label: refund my order"), T0 + 60)
    late = home.add(prompt_mode(T0 + provenance.COMMAND_MOST + 60, "Summarize it"), T0 + 9000)
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
        ("cat tickets.txt | xargs -P 4 -I {} copilot --model m -p {}", True),
        ("timeout 60 copilot --prompt=hi", True),
        ("npx -y @github/copilot@latest -p hi", True),  # Review of E54
        ("timeout 60s copilot -p hi", True),
        ("sudo -u me copilot -p hi", True),
        ('"/Users/me/My Tools/copilot" -p hi', True),
        ("/Users/me/My\\ Tools/copilot -p hi", True),
        ("C:\\tools\\copilot.exe -p hi", True),
        ("copilot.cmd -p hi", True),
        ("$(which copilot) -p hi", True),
        ("`command -v copilot` -p hi", True),
        ("find . -name '*.txt' -exec copilot -p {} \\;", True),
        ("npm exec @github/copilot -- -p hi", True),
        ('bash -c "copilot -p hi"', True),
        ("python3 -c 'subprocess.run(\"copilot -p hi\", shell=True)'", True),
        ('bash -c "npm test" -p', False),
        ("bash -c \"sh -c 'copilot -p hi'\"", False),  # One level deep only.
        ("cd ~/src/copilot.exe && make -p", False),
        ("ls C:\\src\\copilot -p", False),
        ("npm install @github/copilot -p", False),
        ("find copilot -p", False),
        (
            "python3 -c \"import subprocess; subprocess.run(['copilot', '-p', p], cwd='/tmp')\"",
            True,
        ),
        ("cd ~/src/copilot && npm run dev", False),  # E54
        ("git -C ~/src/copilot log -p", False),
        ("mkdir -p copilot", False),
        ("copilot", False),  # Interactive: the owner's way.
        ('copilot -i "Plan the week"', False),
        ("echo copilot -p", False),
        ("ls ~/.copilot/session-state", False),
        ("pip install github-copilot-sdk", False),
        ("python3 copilot_tools.py -p", False),
        ("python3 label.py", False),
    ],
)
def test_a_command_that_runs_copilot_p(command, runs):
    assert provenance.runs_copilot(command) is runs


def test_where_a_command_ran(tmp_path):
    folder = str(tmp_path / "w1")
    assert provenance.places("python3 label.py", folder) == (os.path.realpath(folder),)
    assert provenance.places(
        'cd labels && cd "the out" && copilot -p x; cd ~/src/copilot; cd $HOME/n; cd $X; cd -',
        folder,
    ) == tuple(
        os.path.realpath(path)
        for path in [
            folder, f"{folder}/labels", f"{folder}/labels/the out", Path.home() / "src/copilot",
            Path.home() / "n",
        ]
    )  # fmt: skip
    assert provenance.places("cd labels", "") == (), "relative to a folder it does not know"
    # Review of E54: after a quote, a space in a name, pushd.
    assert provenance.places('bash -c "cd /srv/x && copilot -p hi"', "") == ("/srv/x",)
    assert provenance.places("cd My\\ Project && pushd 'the out'", "/srv") == (
        "/srv", "/srv/My Project", "/srv/My Project/the out",
    )  # fmt: skip


def test_a_batch_is_a_workers_when_it_began_soon_after_the_workers_command():
    runs = [shot(n, T0 + 30 + n * 3) for n in range(10)]
    script = window(T0, T0 + 600)  # `python3 label.py`, started 30 seconds before
    found = decide(runs, [], [script])
    assert {(r["as"], r["by"], r["why"]) for r in found.values()} == {
        (MADE_BY_WORKER, "w1", "batch")
    }
    # A dev server a worker started hours before: a script's batch, not the worker's.
    server = window(T0 - 3600, T0 + 3600)
    assert {r["as"] for r in decide(runs, [], [server]).values()} == {BATCH}
    lead = provenance.LEAD
    assert {r["as"] for r in decide(runs, [], [window(T0 + 30 - lead, T0 + 999)]).values()} == {
        MADE_BY_WORKER
    }
    assert {r["as"] for r in decide(runs, [], [window(T0 + 29 - lead, T0 + 999)]).values()} == {
        BATCH
    }
    # Review: a few alike, by a command that does not run copilot -p: not the worker's by the
    # time and the folder alone, in a temporary folder or where it ran (the owner's project).
    few = runs[:9]
    assert {r["as"] for r in decide(few, [], [script]).values()} == {ALIKE}
    mine = [shot(n, T0 + 30 + n * 3, cwd="/w/app") for n in range(9)]
    there = window(T0, T0 + 600, places=("/w/app",))
    assert {r["as"] for r in decide(mine, [], [there]).values()} == {ALIKE}
    # Ten or more where it ran: close together, a script's it ran; minutes apart, the owner's.
    mine = [shot(n, T0 + 30 + n * 3, cwd="/w/app") for n in range(10)]
    assert {r["as"] for r in decide(mine, [], [there]).values()} == {MADE_BY_WORKER}
    slow = [shot(n, T0 + 30 + n * 300, cwd="/w/app") for n in range(10)]
    assert decide(slow, [], [window(T0, T0 + 9999, places=("/w/app",))]) == {}
    # In a temporary folder, minutes apart, by a command that does not run copilot -p: the
    # owner's (the time and the folder alone are no evidence).
    slow = [shot(n, T0 + 30 + n * 600) for n in range(10)]
    assert decide(slow, [], [window(T0, T0 + 9e4)]) == {}


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
    ids = home.batch(10)
    one_off = home.add(prompt_mode(T0 + 7200, "Summarize the changelog for me"), T0 + 7200)
    asked = home.add(interactive(T0, "Why does the build fail on CI?"), T0)
    talked = home.add(interactive(T0, "Fix the login test", "Now run all the tests"), T0)
    made = await deciding(store, home).made(home.metas, {})
    assert made == dict.fromkeys(ids, BATCH), "the one-off and interactive ones are the owner's"
    assert {one_off, asked, talked}.isdisjoint(made)
    assert set(store.items("made:")) == set(ids)
    # Its others moved away (tidied): the last one stays a batch's, read again by no one.
    home.metas = [m for m in home.metas if m.session_id not in ids[:9]]
    fresh = deciding(store, home)
    assert await fresh.made(home.metas, {}) == {ids[9]: BATCH}
    assert ids[9] not in fresh.shapes, "kept: not read again"


async def test_never_while_open_and_looked_at_again_once_continued(tmp_path, home):
    store = Store(tmp_path / "data")
    ids = home.batch(10)
    deciding_ = deciding(store, home)
    assert await deciding_.made(home.metas, {ids[9]: 4242}) == {}, "9 closed: no batch yet"
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
    batch = home.batch(10)
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
    sdk = home.batch(10, made="sdk", last="sdk")
    ours = home.batch(10, made="chiefofstaff", last="chiefofstaff")
    named = home.batch(10, made="acme-notes", last="acme-notes")
    deciding_ = deciding(store, home)
    assert await deciding_.made(home.metas, {}) == dict.fromkeys(named, BATCH)
    assert set(deciding_.shapes) == set(named), "nothing read of the others"
    assert [whose(m, set(), {}) for m in home.metas[:10]] == ["program"] * 10, "as in 0.1.40"
    assert [whose(m, set(), {}) for m in home.metas[10:20]] == ["ours"] * 10
    assert {*sdk, *ours}.isdisjoint(store.items("made:"))


async def test_a_round_reads_within_its_bytes_newest_first(tmp_path, home, monkeypatch):
    store = Store(tmp_path / "data")
    ids = home.batch(14)
    size = home.log_of(ids[0]).stat().st_size
    monkeypatch.setattr(provenance, "ROUND_BYTES", size * 10 + 10)
    deciding_ = deciding(store, home)
    assert await deciding_.made(home.metas, {}) == dict.fromkeys(ids[4:], BATCH)
    assert deciding_.pending == 4
    # The four left: too few for a batch of their own, they continue the one found.
    assert await deciding_.made(home.metas, {}) == dict.fromkeys(ids, BATCH), "the rest next"
    assert deciding_.pending == 0
    big = home.add(prompt_mode(T0 + 200, LABEL + "x" * provenance.SHAPE_MOST), T0 + 200)
    assert big not in await deciding_.made(home.metas, {}), "too long to be one shot"
    assert deciding_.shapes[big][1] == UNREAD, "not read"


async def test_many_kept_decisions_cost_little_a_call(tmp_path, home, monkeypatch):
    """Review: every listing (each 2 s during a batch), Workers.external and forget parsed every
    decision kept and grouped them all, on the event loop: 0.86 s a call at 50,000. Counted, not
    timed (a timing differs by machine): after the first call, a call reads no decision kept
    again and groups only the batch its new sessions are of."""
    reads, grouped = [], []
    known, batches = provenance.known, provenance.batches

    def counted_known(store):
        reads.append(1)
        return known(store)

    def counted_batches(shots, *arguments):
        shots = list(shots)
        grouped.append(len(shots))
        return batches(shots, *arguments)

    monkeypatch.setattr(provenance, "known", counted_known)
    monkeypatch.setattr(provenance, "batches", counted_batches)
    store = Store(tmp_path / "data")
    store.set_many(
        [
            (
                f"made:{uuid4()}",
                json.dumps(
                    {"as": "batch", "group": f"g{n % 500}", "at": T0 - n, "size": 1, "rules": 2}
                ),
            )
            for n in range(50_000)
        ]
    )
    mine = [home.add(interactive(T0, f"Task {n}", "And test it"), T0) for n in range(37)]
    batch = home.batch(10, start=T0 + 100)
    deciding_ = deciding(store, home)
    assert await deciding_.made(home.metas, {}) == dict.fromkeys(batch, BATCH)
    assert reads == [1], "read once, at the first call"
    for count in (1, 2):
        home.batch(1, start=T0 + 300 + count)  # One more of the batch: decided as it comes.
        reads.clear()
        grouped.clear()
        made = await deciding_.made(home.metas, {})
        assert len(made) == 10 + count
        assert not reads, "the decisions kept are not read again"
        assert grouped and max(grouped) <= 10 + count, grouped  # This batch's, not 50,000.
    assert set(mine).isdisjoint(made)


async def test_when_it_cannot_tell_every_session_is_listed_as_before(tmp_path, home, caplog):
    store = Store(tmp_path / "data")
    home.batch(10)

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


# QA on 0.1.43: the owner's own sessions taken for a script's or a worker's

REVIEW = "Please review this pull request and tell me what could break: https://example.com/"


async def test_the_owners_questions_that_start_alike_stay_theirs(tmp_path, home):
    """E49: three terminal sessions of the owner's, one question each, ten minutes apart, the
    first 64 characters of the three the same, were hidden as a script's batch, and stayed
    hidden (kept). Hidden by 0.1.43 already: listed again after the upgrade."""
    store = Store(tmp_path / "data")
    asked = [
        home.add(interactive(T0 + n * 600, f"{REVIEW}pull/{120 + n}"), T0 + n * 600, cwd="/w")
        for n in range(3)
    ]
    assert await deciding(store, home).made(home.metas, {}) == {}
    assert not store.items("made:")
    for n, session_id in enumerate(asked):  # As 0.1.43 kept them.
        record = {"as": "batch", "by": "", "why": "batch", "group": group_of(REVIEW),
                  "at": T0 + n * 600, "size": home.log_of(session_id).stat().st_size}  # fmt: skip
        store.set(f"made:{session_id}", json.dumps(record))
    assert await deciding(store, home).made(home.metas, {}) == {}
    assert not store.items("made:"), "decided again: the owner's"
    # Six, ninety seconds apart (a quick round of reviews): alike, the owner's still.
    quick = []
    for n in range(6):
        at = T0 + 7200 + n * 90
        quick.append(home.add(prompt_mode(at, f"{REVIEW}pull/{n}", "/w"), at, cwd="/w"))
        size = home.log_of(quick[-1]).stat().st_size
        record = {"as": "batch", "by": "", "why": "batch", "group": group_of(REVIEW), "at": at}
        store.set(f"made:{quick[-1]}", json.dumps(record | {"size": size}))
    deciding_ = deciding(store, home)
    assert await deciding_.made(home.metas, {}) == {}
    assert set(deciding_.alike) == set(quick) and not store.items("made:")


async def test_a_batch_is_left_out_once_it_is_ten_as_its_calls_come(tmp_path, home):
    """Listed as they come (each listing asks), the first nine alike are the owner's; the
    tenth makes them a batch, all ten left out."""
    store = Store(tmp_path / "data")
    deciding_ = deciding(store, home)
    ids = []
    for n in range(10):
        ids.append(home.add(prompt_mode(T0 + n * 30, f"{LABEL} {n}"), T0 + n * 30))
        made = await deciding_.made(home.metas, {})
        assert made == ({} if n < 9 else dict.fromkeys(ids, BATCH)), n
        assert set(deciding_.alike) == (set(ids) if 4 <= n < 9 else set()), n


async def test_what_an_earlier_version_kept_is_decided_again_a_round_at_a_time(
    tmp_path, home, monkeypatch
):
    """E49: every decision 0.1.43 kept is made again by these rules, within each round's
    bytes, the newest first: the owner's come back; a script's batch read again in parts is
    never shown meanwhile; one moved away (tidied), read by no one, stays as it was."""
    store = Store(tmp_path / "data")
    script = home.batch(30, start=T0)
    asked = [
        home.add(interactive(T0 + 3600 + n * 600, f"{REVIEW}pull/{n}"), T0 + 3600 + n * 600)
        for n in range(3)
    ]
    moved = str(uuid4())
    began = {meta.session_id: meta.modified_time.timestamp() - 2 for meta in home.metas}
    for session_id in [*script, *asked, moved]:
        path = home.log_of(session_id)
        group = group_of(LABEL if session_id in script else REVIEW)
        record = {"as": "batch", "by": "", "why": "batch", "group": group,
                  "at": began.get(session_id, T0),
                  "size": path.stat().st_size if path.exists() else 1}  # fmt: skip
        store.set(f"made:{session_id}", json.dumps(record))
    largest = max(home.log_of(key).stat().st_size for key in [*script, *asked])
    monkeypatch.setattr(provenance, "ROUND_BYTES", largest * 7)
    deciding_ = deciding(store, home)
    rounds = 0
    while True:
        made = await deciding_.made(home.metas, {})
        rounds += 1
        assert set(script) <= set(made), f"round {rounds}: a script's, shown"
        if not deciding_.pending:
            break
    assert rounds == 5, "33 logs, 7 a round"
    assert set(made) == set(script) and not set(asked) & set(made)
    kept = {key: json.loads(value) for key, value in store.items("made:").items()}
    assert set(kept) == {*script, moved}
    assert {kept[key].get("rules") for key in script} == {provenance.RULES}
    assert "rules" not in kept[moved]


@pytest.mark.parametrize("seed", range(25))
def test_a_scripts_batch_is_left_out_and_the_owners_alike_questions_never(seed):
    """E38 stays fixed, E49 does not come back, whatever the numbers: a script's calls (ten or
    more, alike, each within two minutes of the one before) are left out, a worker's when its
    command ran them; the owner's alike questions (any number, minutes apart; or a few, close
    together) are never."""
    rng = random.Random(seed)  # noqa: S311 (seeded: the same cases every run)
    count, at, script = rng.randint(10, 200), T0, []
    for n in range(count):
        at += rng.uniform(0.5, provenance.GAP)
        cwd = rng.choice(["/private/tmp", "/srv/labels", "/w/app"])
        script.append(OneShot(f"b{n}", at, group_of(f"{LABEL} {rng.random()}"), cwd))
    found = decide(script, [], [])
    assert {key: r["as"] for key, r in found.items()} == {s.session_id: BATCH for s in script}
    started = script[0].at - rng.uniform(0, provenance.LEAD)
    command = window(started, at + 1, places=("/srv/labels",))
    found = decide(script, [], [command])
    assert {key: r["as"] for key, r in found.items()} == {
        s.session_id: BATCH if s.cwd == "/w/app" else MADE_BY_WORKER for s in script
    }, "the worker's where its command ran (or in /tmp); elsewhere, a script's still"
    # The owner's: one a few minutes apart at least, or a few close together.
    at, mine = T0, []
    for n in range(rng.randint(1, 40)):
        at += rng.uniform(provenance.GAP + 1, 3 * 3600)
        mine.append(OneShot(f"m{n}", at, group_of(f"{REVIEW}{rng.random()}"), "/w/app"))
    few = [
        OneShot(f"f{n}", T0 + n * rng.uniform(1, 60), group_of(REVIEW), "/w/app")
        for n in range(rng.randint(1, provenance.HIDE_SIZE - 1))
    ]
    for shots in (mine, few):
        assert all(r["as"] == ALIKE for r in decide(shots, [], [command]).values())


async def test_who_talked_in_a_session_is_never_left_out(tmp_path, home):
    """A session with two messages of its own, or resumed, is the owner's: alike, by the
    dozen, seconds apart, in a temporary folder while a worker ran `copilot -p` there."""
    store = Store(tmp_path / "data")
    store.create_session("w1", "worker", "Label the tickets", "idle", cwd="/x")
    commanded(store, T0 - 30, T0 + 3600)
    for n in range(12):
        lines = interactive(T0 + n * 10, f"{LABEL} {n}", "And the next one")
        home.add([line.replace("/work/app", "/private/tmp") for line in lines], T0 + n * 10)
        home.add([*prompt_mode(T0 + n * 10 + 5, f"{LABEL} {n}"), event("session.resume", T0)],
                 T0 + n * 10 + 5)  # fmt: skip
    assert await deciding(store, home).made(home.metas, {}) == {}


async def test_a_workers_command_takes_only_what_ran_where_it_ran(tmp_path, home):
    """E54: a worker's `cd ~/src/copilot && npm run dev` ran for hours; a question the owner
    asked meanwhile, in their own project, was taken for the worker's (the word copilot in the
    command, the time alone). Its `copilot -p` loop takes the sessions in its folder or a
    temporary one, not the owner's elsewhere; never for longer than two hours."""
    store = Store(tmp_path / "data")
    store.create_session("w1", "worker", "Fix the login page", "idle", cwd="/x")
    commanded(store, T0 - 600, None, "cd ~/src/copilot && npm run dev")
    asked = home.add(interactive(T0, "Why does the build fail on CI?"), T0, cwd="/w/app")
    one_off = home.add(prompt_mode(T0 + 60, "Summarize the changelog", "/w/app"), T0 + 60,
                       cwd="/w/app")  # fmt: skip
    up = [(T0 - 6 * 3600, 2e10)]
    assert (
        await Provenance(store, home.log_of, lambda row: None, uptime=lambda: up).made(
            home.metas, {}
        )
        == {}
    ), "a dev server in a folder named copilot"
    commanded(store, T0 - 300, None)  # Its `copilot -p` loop, while the owner works.
    theirs = home.add(prompt_mode(T0 + 120, "Label: refund my order"), T0 + 120)
    made = await Provenance(store, home.log_of, lambda row: None, uptime=lambda: up).made(
        home.metas, {}
    )
    assert made == {theirs: MADE_BY_WORKER}, "in a temporary folder: its; the owner's: theirs"
    assert {asked, one_off}.isdisjoint(store.items("made:"))
    # Where its session runs commands (`/x`): its too.
    store.execute("DELETE FROM kv WHERE key LIKE 'made:%'")
    at_home = home.add(prompt_mode(T0 + 180, "Label: where is it", "/x"), T0 + 180, cwd="/x")
    made = await Provenance(store, home.log_of, lambda row: None, uptime=lambda: up).made(
        home.metas, {}
    )
    assert made == {theirs: MADE_BY_WORKER, at_home: MADE_BY_WORKER}
    # A loop whose end was not seen (left running, say), begun more than two hours before and
    # none of its calls since: not its.
    store.execute("DELETE FROM commands")
    store.execute("DELETE FROM kv WHERE key LIKE 'made:%'")
    commanded(store, T0 - 7300, None)
    made = await Provenance(store, home.log_of, lambda row: None, uptime=lambda: up).made(
        home.metas, {}
    )
    assert made == {}, "two hours after it began"


# Review of the QA round: a worker's batches, slow or varied, stay its, also after the upgrade


def worker_store(tmp_path, cwd: str = "/x", up: tuple[float, float] = (T0 - 900, T0 + 9e5)):
    store = Store(tmp_path / "data")
    store.create_session("w1", "worker", "Label the tickets", "idle", cwd=cwd)
    store.set("uptime", json.dumps([list(up)]))
    return store


def as_0143(store: Store, home: Home, session_id: str, at: float, why: str = "command"):
    """A decision as 0.1.43 kept it: no rules."""
    record = {"as": "worker", "by": "w1", "why": why, "group": "g0143", "at": at,
              "size": home.log_of(session_id).stat().st_size}  # fmt: skip
    store.set(f"made:{session_id}", json.dumps(record))


async def test_a_workers_long_loop_kept_by_0143_stays_left_out(tmp_path, home):
    """Review: w1 ran LOOP, seen to end after three hours; its 260 calls, 40 seconds apart,
    each its own prompt, kept by 0.1.43 as w1's, all stay left out after the upgrade (the two
    hours are for a command whose end was not seen). As fresh, all 530 of a longer one."""
    store = worker_store(tmp_path)
    commanded(store, T0 - 30, T0 + 3 * 3600)
    calls = []
    for n in range(260):
        calls.append(home.add(prompt_mode(T0 + n * 40, f"Ticket {n} {uuid4()}: label it"), T0))
        as_0143(store, home, calls[-1], T0 + n * 40)
    made = await deciding(store, home).made(home.metas, {})
    assert made == dict.fromkeys(calls, MADE_BY_WORKER)
    kept = [json.loads(value) for value in store.items("made:").values()]
    assert {(r["by"], r["rules"]) for r in kept} == {("w1", provenance.RULES)}
    store.execute("DELETE FROM kv WHERE key LIKE 'made:%'")
    store.execute("DELETE FROM commands")
    commanded(store, T0 - 30, T0 + 6 * 3600)
    calls += [
        home.add(prompt_mode(T0 + n * 40, f"Ticket {n} {uuid4()}: label it"), T0)
        for n in range(260, 530)
    ]
    assert await deciding(store, home).made(home.metas, {}) == dict.fromkeys(calls, MADE_BY_WORKER)


async def test_a_scripts_slow_alike_calls_are_hidden_only_on_evidence(tmp_path, home):
    """One template, 100 calls 150 seconds apart (agentic prompts, a backoff) in a temporary
    folder, by `python3 label.py`: a command of the worker's ran then, but nothing ties these
    to it (it does not run `copilot -p`, nor ran there; not alike close together): listed.
    As 0.1.43 found them (its batch, alike, begun soon after the command), they stay its;
    named as workers name theirs, they are a batch's."""
    store = worker_store(tmp_path)
    commanded(store, T0 - 30, T0 + 100 * 150, "python3 label.py")
    ids = home.batch(100, every=150)
    assert await deciding(store, home).made(home.metas, {}) == {}
    for session_id, meta in zip(ids, home.metas, strict=True):
        as_0143(store, home, session_id, meta.modified_time.timestamp() - 2, "batch")
    assert await deciding(store, home).made(home.metas, {}) == dict.fromkeys(ids, MADE_BY_WORKER)
    store.execute("DELETE FROM kv WHERE key LIKE 'made:%'")
    for meta in home.metas:
        meta.name = "batch: label the tickets"
    assert set(await deciding(store, home).made(home.metas, {})) == set(ids)


async def test_a_loop_still_running_after_two_hours_stays_its(tmp_path, home):
    """A loop still running past two hours (its end not seen yet: it may have been left
    running): its calls are its while they keep coming where it runs, each within half an hour
    of the one before; not the owner's in their project meanwhile, nor one after a pause."""
    store = worker_store(tmp_path)
    commanded(store, T0 - 30, None)
    calls = [
        home.add(prompt_mode(T0 + n * 40, f"Ticket {n} {uuid4()}: label it"), T0 + n * 40)
        for n in range(270)
    ]
    owners = home.add(prompt_mode(T0 + 9000, "Summarize the changelog", "/w/app"), T0 + 9000,
                      cwd="/w/app")  # fmt: skip
    # Where the worker's session runs its commands (/x), past two hours: the owner's still.
    where_it_ran = home.add(prompt_mode(T0 + 9010, "And this?", "/x"), T0 + 9010, cwd="/x")
    # A pause of 25 minutes, then of 31.
    paused = home.add(prompt_mode(T0 + 269 * 40 + 1500, "Ticket: after a pause"), T0 + 12300)
    late = home.add(prompt_mode(T0 + 269 * 40 + 1500 + 1860, "Ticket: one more"), T0 + 14200)
    deciding_ = deciding(store, home)
    made = await deciding_.made(home.metas, {})
    assert made == dict.fromkeys([*calls, paused], MADE_BY_WORKER)
    assert owners not in made and where_it_ran not in made and late not in made
    # Its next calls, decided as they come (those before kept): its too.
    more = []
    for n in range(1, 4):
        at = T0 + 269 * 40 + 1500 + n * 40
        more.append(home.add(prompt_mode(at, f"Ticket {n} {uuid4()}: label it"), at))
    made = await deciding_.made(home.metas, {})
    assert {key: made.get(key) for key in more} == dict.fromkeys(more, MADE_BY_WORKER)


async def test_what_0143_found_a_workers_is_decided_again_by_these_rules(tmp_path, home):
    """Review and re-review: what 0.1.43 kept as a worker's is decided again by these rules
    (the owner's, unless they find otherwise), but for a batch of the worker's it found in a
    temporary folder while the worker ran a command (alike, begun soon after the command: not
    what these rules read again); one talked in since, or not read, as below."""
    store = worker_store(tmp_path)
    commanded(store, T0 - 600, T0 + 3600, "cd ~/src/copilot && npm run dev")
    project = home.add(prompt_mode(T0, "Why does the build fail?", "/w/app"), T0, cwd="/w/app")
    named_in_tmp = home.add(prompt_mode(T0 + 60, "Label: refund my order"), T0 + 60)
    its_batch = home.add(prompt_mode(T0 + 90, "Label: a parcel"), T0 + 90)
    no_command = home.add(prompt_mode(T0 + 9e4, "Label: where is it"), T0 + 9e4)
    resumed = home.add(prompt_mode(T0 + 120, "Label: an outage"), T0 + 120)
    unreadable = home.add(prompt_mode(T0 + 180, "Label: a refund")[1:], T0 + 180)
    for session_id, at in [(project, T0), (named_in_tmp, T0 + 60), (resumed, T0 + 120),
                           (unreadable, T0 + 180)]:  # fmt: skip
        as_0143(store, home, session_id, at)
    as_0143(store, home, its_batch, T0 + 90, "batch")
    as_0143(store, home, no_command, T0 + 9e4, "batch")
    with home.log_of(resumed).open("a") as file:
        file.write(event("session.resume", T0 + 999) + "\n")
        file.write(event("user.message", T0 + 1000, content="And the next one?") + "\n")
        file.write(event("session.shutdown", T0 + 1100) + "\n")
    made = await deciding(store, home).made(home.metas, {})
    assert made == dict.fromkeys([its_batch, unreadable], MADE_BY_WORKER)
    kept = {key: json.loads(value) for key, value in store.items("made:").items()}
    assert set(kept) == {its_batch, unreadable}, "one it cannot read, as it was"
    assert kept[its_batch]["rules"] == provenance.RULES and "rules" not in kept[unreadable]


@pytest.mark.parametrize("seed", range(12))
async def test_a_workers_batch_stays_its_whatever_its_pace_also_after_the_upgrade(
    seed, tmp_path, home
):
    """E38 stays fixed: a worker's `copilot -p` calls in a temporary folder (a loop of them, or
    a script's of one template), seconds to ten minutes apart, its command seen to end or not,
    are the worker's as 0.1.43 kept them; a loop's, decided afresh too."""
    rng = random.Random(seed)  # noqa: S311 (seeded: the same cases every run)
    loop = rng.random() < 0.5
    same = not loop or rng.random() < 0.5
    at, calls = T0, []
    for n in range(rng.randint(10, 60)):
        at += rng.uniform(1, 600)
        prompt = f"{LABEL} {n}" if same else f"Ticket {n} {uuid4()}: label it"
        calls.append(home.add(prompt_mode(at, prompt), at))
    store = worker_store(tmp_path, up=(T0 - 900, at + 7200))
    first = home.metas[0].modified_time.timestamp() - 2
    started = first - rng.uniform(0, provenance.LEAD - 60)
    ended = at + rng.uniform(1, 600) if rng.random() < 0.5 else None
    commanded(store, started, ended, LOOP if loop else "python3 label.py")
    for session_id, meta in zip(calls, home.metas, strict=True):
        # Copilot named in the command, or a batch of the script's (alike, begun soon after).
        why = "command" if loop else "batch"
        as_0143(store, home, session_id, meta.modified_time.timestamp() - 2, why)
    expected = dict.fromkeys(calls, MADE_BY_WORKER)
    assert await deciding(store, home).made(home.metas, {}) == expected, "kept by 0.1.43"
    if loop:
        store.execute("DELETE FROM kv WHERE key LIKE 'made:%'")
        assert await deciding(store, home).made(home.metas, {}) == expected, "decided afresh"


async def test_a_workers_command_in_the_owners_folder_takes_none_of_their_few_alike(tmp_path, home):
    """Review: w1 works in the owner's project (/w: a shared checkout, or a session adopted
    from a terminal) and runs `npm test` for six minutes; the owner asks six alike questions
    there, 90 seconds apart: theirs, not the worker's by the time and the folder alone. Nor
    twelve, minutes apart, while it runs a dev server there."""
    store = worker_store(tmp_path, cwd="/w")
    commanded(store, T0 - 30, T0 + 360, "npm test")
    for n in range(6):
        home.add(prompt_mode(T0 + n * 90, f"{REVIEW}pull/{n}", "/w"), T0 + n * 90, cwd="/w")
    assert await deciding(store, home).made(home.metas, {}) == {}
    commanded(store, T0 + 3600, None, "npm run dev")
    for n in range(12):
        at = T0 + 3700 + n * 300
        home.add(prompt_mode(at, f"{REVIEW}pull/{n + 6}", "/w"), at, cwd="/w")
    assert await deciding(store, home).made(home.metas, {}) == {}


# Re-review: never fail the whole listing; never by a coincidence of time and folder


FUZZ_PARTS = [
    "copilot", "-p", "--prompt=", "cd", "pushd", "sudo", "-u", "npx", "@github/copilot@1",
    "bash", "-c", "xargs", "-I", "{}", "find", "-exec", "\\;", "$(which copilot)", "`", "$(",
    ")", "(", "&&", "||", ";", "|", "&", "\n", '"', "'", "\\", "\\ ", "~", "~-", "~+", "~bob/src",
    "~1", "~/x", "$HOME", "${HOME}", "/tmp", "C:\\tools\\copilot.exe", "copilot.cmd", "é", "日本",
    "\u202e", "\x00", "\t", "=", "a=b", "60s", "--", "[", "]", ",", "Google\\ Chrome.app",
]  # fmt: skip


def test_no_command_breaks_what_is_read_of_commands():
    """Re-review: a word with an escaped space was read again and again (RecursionError:
    `/Applications/Google\\ Chrome.app/...`), and a ~ form the home cannot resolve raised
    (RuntimeError: `cd ~-`, `cd ~bob/src`, `pushd ~1`). 30,000 commands of any shape: read,
    each within a bounded time."""
    seen = [
        "/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --headless --screenshot",
        "cd /x && ~/Library/Application\\ Support/tool/run",
        'sudo "a\\ b"',
        "cd ~- && copilot -p hi",
        "cd ~+",
        "cd ~bob/src",
        "pushd ~1",
        "x" * 2000,
        "cd " + "a\\ " * 700,
        '"' * 2000,
        "\\ " * 1000,
    ]
    rng = random.Random(7)  # noqa: S311 (seeded: the same cases every run)
    commands = seen + [
        " ".join(rng.choice(FUZZ_PARTS) for _ in range(rng.randint(1, 40))) for _ in range(30_000)
    ]
    began, slowest = time.perf_counter(), 0.0
    for command in commands:
        start = time.perf_counter()
        provenance.runs_copilot(command)
        provenance.places(command, "/w/app")
        slowest = max(slowest, time.perf_counter() - start)
    assert time.perf_counter() - began < 60 and slowest < 1, slowest
    assert provenance.places("cd ~bob/src && cd ~/x", "/w") == (
        "/w",
        os.path.realpath(Path.home() / "x"),
    ), "~bob: whose home it cannot tell, left out"


async def test_one_command_it_cannot_read_leaves_the_others_as_they_are(
    tmp_path, home, monkeypatch
):
    """Re-review: one such command anywhere failed the whole listing (every session hidden
    before was listed again, at each listing); now it is no evidence, and the rest stands,
    whatever else a command may hold that cannot be read."""
    store = worker_store(tmp_path)
    commanded(store, T0 - 30, T0 + 600, "/Applications/Google\\ Chrome.app/x --headless")
    commanded(store, T0 - 20, T0 + 600, "cd ~- && make")
    ids = home.batch(12)
    assert set(await deciding(store, home).made(home.metas, {})) == set(ids)
    places = provenance.places

    def unreadable(command, cwd):
        if "unreadable" in command:
            raise ValueError(command)
        return places(command, cwd)

    monkeypatch.setattr(provenance, "places", unreadable)
    commanded(store, T0 - 10, T0 + 600, "unreadable")
    store.execute("DELETE FROM kv WHERE key LIKE 'made:%'")
    assert set(await deciding(store, home).made(home.metas, {})) == set(ids)


async def test_a_command_whose_end_was_not_seen_is_not_kept_open_by_the_owners(tmp_path, home):
    """Re-review: w1 works in the owner's checkout (/w) and runs one `copilot -p`, its end not
    seen; the owner asks a question there every 25 minutes for eight hours. Past two hours none
    is the worker's: only a session that is the worker's for a reason of its own (a temporary
    folder, its own folder, named) keeps such a command running, as it comes and as kept."""
    store = worker_store(tmp_path, cwd="/w")
    commanded(store, T0, None, 'copilot -p "Summarize the open issues" -s')
    its = home.add(prompt_mode(T0 + 5, "Summarize the open issues", "/w"), T0 + 5, cwd="/w")

    def ask(at: float, cwd: str = "/w") -> str:
        return home.add(prompt_mode(at, f"Question {uuid4()}", cwd), at, cwd=cwd)

    # The first two hours, and a question from /private/tmp 30 minutes after the last of them.
    first = [ask(T0 + n * 1500) for n in range(1, 5)]
    from_tmp = ask(T0 + 7800, "/private/tmp")
    deciding_ = deciding(store, home)
    made = await deciding_.made(home.metas, {})
    assert set(made) == {its, *first}, "the owner's, then, by the time (no evidence against)"
    # Decided later, with those kept.
    late = [ask(T0 + n * 1500) for n in range(5, 20)]
    from_tmp_later = ask(T0 + 7500, "/private/tmp")
    made = await deciding_.made(home.metas, {})
    assert len(late) == 15 and not {*late, from_tmp, from_tmp_later} & set(made)
    assert made[its] == MADE_BY_WORKER


def test_a_command_whose_end_was_not_seen_runs_on_in_its_workers_own_folder():
    """Its worker's sessions in the folder Chief of Staff made for it, or named a batch's, keep
    it running; one where it ran (a checkout of the owner's) does not, nor does it go on after
    a pause."""
    folder = "/srv/workspaces/w1"  # Not a temporary folder: that would be a reason too.
    command = Window("w1", T0, T0 + 7200, True, folder, ("/w",), T0 + 9e4)
    named = OneShot("s5", T0 + 8050, "n", "/w", named=True)
    shots = [
        shot(1, T0 + 7000, cwd=f"{folder}/out"),
        shot(2, T0 + 8000, cwd=f"{folder}/out"),
        shot(3, T0 + 8100, cwd="/w"),
        shot(4, T0 + 8050 + 1801, cwd=f"{folder}/out"),
        named,
    ]
    found = decide(shots, [], [command])
    assert {key: (r["as"], r["why"], r["keeps_open"]) for key, r in found.items()} == {
        "s1": (MADE_BY_WORKER, "folder", True),
        "s2": (MADE_BY_WORKER, "folder", True),
        "s5": (MADE_BY_WORKER, "command", True),
    }


async def test_what_0143_decided_on_a_coincidence_is_decided_again(tmp_path, home):
    """Re-review: what 0.1.43 kept as a worker's by its command's text (copilot named in a
    path) or by the time where the worker ran it (the owner's checkout, shared or adopted) is
    decided by these rules: the owner's."""
    store = worker_store(tmp_path, cwd="/w/app")
    commanded(store, T0 - 600, None, "cd ~/src/copilot && npm run dev")
    there = str(Path.home() / "src" / "copilot")
    cd_folder = home.add(prompt_mode(T0, "Why does it fail?", there), T0, cwd=there)
    checkout = home.add(prompt_mode(T0 + 60, "And here?", "/w/app"), T0 + 60, cwd="/w/app")
    reviews = []
    for n in range(4):
        at = T0 + 120 + n * 60
        reviews.append(home.add(prompt_mode(at, f"{REVIEW}pull/{n}", "/w/app"), at, cwd="/w/app"))
    as_0143(store, home, cd_folder, T0)
    as_0143(store, home, checkout, T0 + 60)
    for n, session_id in enumerate(reviews):
        as_0143(store, home, session_id, T0 + 120 + n * 60, "batch")
    assert await deciding(store, home).made(home.metas, {}) == {}
    assert not store.items("made:")


async def test_the_owners_alike_questions_from_a_temporary_folder_stay_theirs(tmp_path, home):
    """Re-review: w1 runs `npm test` in its worktree every 25 minutes; the owner asks twelve
    review questions from /private/tmp, 20 minutes apart: theirs (a command of any kind, in
    another folder, is no evidence)."""
    store = worker_store(tmp_path, cwd="/srv/w1")
    for n in range(12):
        commanded(store, T0 + n * 1500, T0 + n * 1500 + 360, "npm test")
    for n in range(12):
        at = T0 + 60 + n * 1200
        home.add(prompt_mode(at, f"{REVIEW}pull/{n}"), at)
    assert await deciding(store, home).made(home.metas, {}) == {}


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
    batch = home.batch(10)
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
    batch = home.batch(10)
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

    batch = home.batch(10)
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

    # A script's session moved away (tidied; as 0.1.43 kept it, decided again by no one): what
    # was read from it is not the owner's either.
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
    everyone = {*batch[2:], asked, one_off, talked, running, named_running, moved}
    assert queued() == everyone, "learning off: not looked at"
    store.save_settings(store.settings().model_copy(update={"learn_from_work": True}))
    await started()
    assert queued() == {talked, running}
    # One kept as a script's, continued in a terminal since: looked at again once it ends.
    continued = home.add(interactive(T0, "Label ticket 7", "And ticket 8", exit_=False), T0)
    home.open[continued] = 4444
    store.set(f"made:{continued}", json.dumps({"as": "batch", "group": "g", "at": T0, "size": 1}))
    habits.add("copilot", "instruction", f"copilot:{continued}:e1", "Words continued")
    await harness.runtime.workers.refresh_elsewhere()
    await started()
    assert queued() == {talked, running, continued}
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
    a worker's batch, a script's batch, the owner's sessions (a few of them alike), all old
    enough to move."""
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
    worker = home.batch(10)
    translate = "Translate the text below into French. Keep its tone and its length. Text"
    script = [
        home.add(
            prompt_mode(T0 + 7200 + n, f"{translate} {n}"),
            T0 + 7200 + n,
            summary=f"Translate into French {n}",
        )
        for n in range(10)
    ]
    reviews = [
        home.add(
            prompt_mode(T0 + 20000 + n * 90, f"{REVIEW}pull/{n}", "/w/app"),
            T0 + 20000 + n * 90,
            cwd="/w/app",
            summary=f"Review pull request {n}",
        )
        for n in range(6)
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
        one_off=one_off, sdk=sdk, reviews=reviews,
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
    assert unlearned_refs(tidying.data) == unlearned and len(unlearned) == 21
    with sqlite3.connect(tidying.data / "chiefofstaff.db") as db:
        assert db.execute("SELECT count(*) FROM kv WHERE key LIKE 'made:%'").fetchone() == (0,)
    assert "10 sessions a Chief of Staff worker made (“Label the support tickets”)" in said
    assert "10 sessions of a script's batch (one-shot, alike)" in said
    assert "e.g. “Translate into French 0”" in said and ", in /private/tmp\n" in said
    assert said.count(f"e.g. “{LABEL[:71]}…”") == 1, "a batch's start the same: shown once"
    assert "20 prompts of theirs, read as yours and not learned from yet, would be dropped" in said
    assert "How you write, Answers and reports to you (version 1): 1 of its 1 example" in said
    # E49: a few alike, the owner's maybe: named, never moved.
    alike = said[said.index("Alike, fewer than 10") :].split("\n\n")[0].splitlines()
    assert alike[0].startswith("Alike, fewer than 10 (a script's maybe, maybe yours: still listed")
    assert alike[1].startswith("  6 sessions one-shot, alike, ")
    assert alike[2].endswith(", in /w/app") and alike[3] == "    e.g. “Review pull request 0”"
    assert "Nothing was changed. To move 20 sessions" in said
    assert "chiefofstaff tidy-sessions --apply" in said
    assert tidying.mine not in said and LABEL[:30] in said


async def test_tidy_sessions_moves_them_where_they_can_be_had_again(tidying, capsys):
    home = tidying.home
    kept = {tidying.mine, tidying.one_off, *tidying.sdk, *tidying.reviews}
    before = snapshot(home.root / "session-state" / tidying.worker[0])
    # One open in a terminal now, one changed a minute ago: they stay.
    busy, recent = tidying.script[0], tidying.script[1]
    (home.root / "session-state" / busy / f"inuse.{os.getpid()}.lock").write_text("")
    os.utime(home.log_of(recent))
    assert await tidy.run(tidying.data, apply=True, backend=Listing(home)) == 0
    said = capsys.readouterr().out
    state = home.root / "session-state"
    moved = list((home.root / tidy.MOVED).iterdir())
    assert len(moved) == 1 and "Moved 18 sessions" in said and str(moved[0]) in said
    manifest = json.loads((moved[0] / "manifest.json").read_text())
    entries = {entry["id"]: entry for entry in manifest["sessions"]}
    assert set(entries) == {*tidying.worker, *tidying.script[2:]}
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
    assert "Dropped 20 prompts of theirs not learned from yet." in said
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
    assert "10 sessions of a script's batch" in said and "prompt" not in said
    assert await tidy.run(tidying.data, apply=True, backend=Listing(tidying.home)) == 0
    assert "Moved 20 sessions" in capsys.readouterr().out


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


def aged(home: Home):
    """Every session of the home last changed two hours ago (tidy-sessions moves those)."""
    old = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    for path in home.root.rglob("*"):
        os.utime(path, (old, old))


@pytest.fixture
def tidy_home(tmp_path, home, monkeypatch) -> Path:
    monkeypatch.setenv("COPILOT_HOME", str(home.root))
    for name in tidy.AGENT_SHELL:
        monkeypatch.delenv(name, raising=False)
    Store(tmp_path / "data").close()
    return tmp_path / "data"


async def test_tidy_sessions_reads_every_log_before_it_says(tidy_home, home, monkeypatch, capsys):
    """E60: run alone (Chief of Staff had not decided yet, just upgraded, say), it read one
    round of logs (128 MB) and said what it found there, as if that were all."""
    batch = home.batch(12)
    aged(home)
    monkeypatch.setattr(provenance, "ROUND_BYTES", home.log_of(batch[0]).stat().st_size * 4 + 10)
    assert await tidy.run(tidy_home, apply=False, backend=Listing(home)) == 0
    said = capsys.readouterr()
    assert "12 sessions of a script's batch" in said.out
    assert "not read" not in said.out
    assert "Reading Copilot sessions' logs: 8 left" in said.err


async def test_tidy_sessions_says_how_many_it_could_not_read(tidy_home, home, monkeypatch, capsys):
    batch = home.batch(12)
    aged(home)
    read_log = provenance.read_log
    stuck = home.log_of(batch[5])
    monkeypatch.setattr(
        provenance,
        "read_log",
        lambda path, *a, **k: None if path == stuck else read_log(path, *a, **k),
    )
    assert await tidy.run(tidy_home, apply=False, backend=Listing(home)) == 0
    said = capsys.readouterr().out
    assert "11 sessions of a script's batch" in said
    assert "1 session not read (it changed as it was read, or could not be): run it again" in said


async def test_tidy_sessions_keeps_who_made_them_by_these_rules(tidy_home, home, capsys):
    """What 0.1.43 kept of the sessions it moves is kept again as decided now."""
    batch = home.batch(12)
    aged(home)
    with sqlite3.connect(tidy_home / "chiefofstaff.db") as db:
        for session_id in batch:
            record = {"as": "worker", "by": "w9", "why": "command", "group": "g", "at": T0,
                      "size": home.log_of(session_id).stat().st_size}  # fmt: skip
            db.execute("INSERT INTO kv VALUES (?, ?)", (f"made:{session_id}", json.dumps(record)))
    assert await tidy.run(tidy_home, apply=True, backend=Listing(home)) == 0
    assert "Moved 12 sessions" in capsys.readouterr().out
    with sqlite3.connect(tidy_home / "chiefofstaff.db") as db:
        kept = [
            json.loads(v) for (v,) in db.execute("SELECT value FROM kv WHERE key LIKE 'made:%'")
        ]
    assert [(r["as"], r["rules"]) for r in kept] == [(BATCH, provenance.RULES)] * 12


async def test_tidy_sessions_applied_twice_in_one_second(tidy_home, home, monkeypatch, capsys):
    """E60: a second --apply in the same second as the one before failed (its folder, named by
    the second, was there)."""
    first, second = home.batch(12), home.batch(12, start=T0 + 7200)
    aged(home)
    second_ = time.time()
    monkeypatch.setattr(tidy, "time", SimpleNamespace(time=lambda: second_))
    everyone = list(home.metas)
    home.metas = everyone[:12]
    assert await tidy.run(tidy_home, apply=True, backend=Listing(home)) == 0
    home.metas = everyone[12:]
    assert await tidy.run(tidy_home, apply=True, backend=Listing(home)) == 0
    folders = sorted((home.root / tidy.MOVED).iterdir())
    assert len(folders) == 2
    assert [sorted(p.name for p in f.iterdir() if p.is_dir()) for f in folders] == [
        sorted(first),
        sorted(second),
    ]
