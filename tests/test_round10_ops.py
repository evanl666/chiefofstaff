"""QA round 10: the Copilot processes an instance stops are its own (E22), and what the way the
owner is reached away can do, as tried (Send, Delivery, Read replies, New commands), with reads
that covered only part of what there was never counted as complete."""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from contextlib import suppress

from conftest import Harness, call_tool, settle
from test_round6_drafts import away, entry, spawn
from test_round7_away import code_of

from chiefofstaff.backend import (
    copilot_orphans,
    copilot_ours,
    copilot_records,
    descendants,
    process_table,
    running,
    start_times,
)
from chiefofstaff.connector import DM_PAGE
from chiefofstaff.models import Settings, Watch
from chiefofstaff.sources.slack import SlackError
from chiefofstaff.store import Store

# E22: an instance on a copy of the data directory never stops the original's Copilot.


def sleeper(*command: str) -> subprocess.Popen:
    process = subprocess.Popen(list(command or ("sleep", "300")))
    deadline = time.monotonic() + 5
    while process.pid not in start_times():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return process


def end(*processes: subprocess.Popen | int):
    for process in processes:
        pid = process if isinstance(process, int) else process.pid
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        if isinstance(process, subprocess.Popen):
            process.wait()


def earlier(records: dict) -> dict:
    """Records as an earlier run of this data directory's Chief of Staff left them: it is gone
    (stopped abruptly), its Copilot still runs."""
    gone = subprocess.Popen(["true"])
    gone.wait()
    owner = {"pid": gone.pid, "started": "Thu Jan  1 00:00:00 1970"}
    return {
        pid: {**record, "instance": "earlier", "owner": owner} for pid, record in records.items()
    }


def orphan() -> int:
    """A process like the Copilot an earlier run left behind: its parent gone."""
    shell = subprocess.run(
        ["/bin/sh", "-c", "sleep 300 >/dev/null 2>&1 & echo $!"],
        capture_output=True,
        text=True,
        check=True,
    )
    pid = int(shell.stdout.strip())
    deadline = time.monotonic() + 5
    while pid not in start_times():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return pid


async def test_a_copy_of_the_data_directory_never_stops_the_originals_copilot(tmp_path):
    """E22: started on a copy of the data directory, Chief of Staff killed the Copilot processes
    of the instance still running on the original (same pids, same command lines)."""
    original, copy = tmp_path / "data", tmp_path / "copy"
    copilot = orphan()
    try:
        store = Store(original)
        records = earlier(copilot_records({copilot: process_table()[copilot][1]}, original))
        store.set("copilot_pids", json.dumps(records))
        store.db.close()
        shutil.copytree(original, copy)
        harness = Harness(copy)
        runtime = await harness.connect()
        assert runtime.ready()
        assert running(copilot), "the original's Copilot runs on"
        mine = copilot_records({copilot: process_table()[copilot][1]}, original)
        assert copilot_ours(mine, original) == {copilot}
        assert copilot_ours(earlier(mine), original) == set(), "an earlier run's: not running"
        assert copilot_ours(mine, copy) == set(), (
            "nor is it the copy's own: a session it has open is open elsewhere"
        )
        await runtime.stop()
        # On the data directory whose processes they are: an orphan of its earlier run.
        harness = Harness(original)
        runtime = await harness.connect()
        deadline = time.monotonic() + 5
        while running(copilot):
            assert time.monotonic() < deadline, "the orphan was not stopped"
            await asyncio.sleep(0.05)
        await runtime.stop()
    finally:
        end(copilot)


def test_a_process_that_has_a_recorded_pid_now_is_not_stopped(tmp_path):
    """A pid is reused: the process that has it now started after the one recorded."""
    copilot = sleeper()
    try:
        records = earlier(copilot_records({copilot.pid: "sleep 300"}, tmp_path))
        assert copilot_orphans(records, tmp_path) == {copilot.pid: "sleep 300"}
        reused = {
            pid: {**record, "started": "Thu Jan  1 00:00:00 1970"}
            for pid, record in records.items()
        }
        assert copilot_orphans(reused, tmp_path) == {}
        other = {
            pid: {**record, "command": "copilot --headless"} for pid, record in records.items()
        }
        assert copilot_orphans(other, tmp_path) == {}, "another command"
    finally:
        end(copilot)


def test_processes_another_running_chief_of_staff_owns_are_left_alone(tmp_path):
    """Its records name this data directory, yet another Chief of Staff runs them: the one that
    started them still runs, or their parents lead to one."""
    owner, copilot = sleeper(), sleeper()
    service = tmp_path / "bin" / "chiefofstaff"
    service.parent.mkdir()
    service.write_text("#!/bin/sh\nsleep 300 &\nwait\n")
    service.chmod(0o755)
    other = sleeper(str(service))
    try:
        records = copilot_records({copilot.pid: "sleep 300"}, tmp_path)
        alive = {"pid": owner.pid, "started": start_times()[owner.pid]}
        started_by_other = {
            pid: {**record, "instance": "other", "owner": alive} for pid, record in records.items()
        }
        assert copilot_orphans(started_by_other, tmp_path) == {}
        assert copilot_orphans(earlier(records), tmp_path) == {copilot.pid: "sleep 300"}
        deadline = time.monotonic() + 5
        while not (children := descendants(process_table(), other.pid) - {other.pid}):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        [child] = children
        under_other = earlier(copilot_records({child: process_table()[child][1]}, tmp_path))
        assert copilot_orphans(under_other, tmp_path) == {}, "its parent is a Chief of Staff"
    finally:
        tree = descendants(process_table(), other.pid)
        end(owner, copilot, other, *tree)


async def test_processes_recorded_by_an_earlier_version_are_left_running(tmp_path, caplog):
    """Records without what tells them from another instance's: never stopped, said once."""
    copilot = sleeper()
    try:
        harness = Harness(tmp_path / "data")
        command = process_table()[copilot.pid][1]
        harness.store.set("copilot_pids", json.dumps({copilot.pid: command}))
        with caplog.at_level(logging.WARNING):
            assert harness.runtime.recorded_processes() == {}
            assert harness.runtime.recorded_processes() == {}
        said = [r for r in caplog.records if "recorded by an earlier version" in r.getMessage()]
        assert len(said) == 1, said
        harness.store.set("copilot_pids", json.dumps({copilot.pid: command}))
        runtime = await harness.connect()
        assert runtime.ready() and copilot.poll() is None
        await runtime.stop()
    finally:
        end(copilot)


# What the way the owner is reached away can do, as tried.

DM = "D0SELF001"
OWNER = "U0OWNER1"


def stamp(after: float) -> str:
    return f"{time.time() + after:.6f}"


def can(runtime) -> dict:
    return runtime.connector.view()["away"]["can"]


def states(runtime) -> dict[str, str]:
    found = can(runtime)
    return {what: found[what]["state"] for what in ("send", "delivery", "replies", "commands")}


async def own_dm_harness(tmp_path):
    settings = Settings(away_notify=True, away_how="my own Slack DM", slack_enabled=True)
    harness = Harness(tmp_path / "data", settings)
    return harness, await away(harness)


async def send_to_own_dm(runtime, harness, version="A", listed=True):
    """The messenger sends what waits to the owner's own DM, as the send tool tells it to
    report. `listed`: reading the DM back finds it."""
    await settle(runtime)
    messenger = runtime.toolset.last_special("messenger")["id"]
    sent = stamp(1)
    harness.mcp.sent_link = f"https://acme.slack.com/archives/{DM}/p{sent.replace('.', '')}"
    if listed:
        harness.mcp.reads[DM] = entry(DM, OWNER, "Me", sent, "Chief of Staff")
    said = await call_tool(runtime, "send_owner_slack_dm", messenger, version=version)
    assert said.result_type == "success", said.text_result_for_llm
    reads = 'replies="read_owner_slack_dm"' in said.text_result_for_llm
    await call_tool(runtime, "report_notified", messenger, ok=True, where="your own Slack DM",
                    method="send_owner_slack_dm",
                    replies="read_owner_slack_dm" if reads else "")  # fmt: skip
    harness.session(messenger).emit("session.idle")
    await settle(runtime)


async def test_the_away_test_tries_sending_delivery_and_reading_replies(tmp_path):
    """Requirement 4: Send, Delivery and Read replies as tried, each with when; New commands
    no, by design."""
    harness, runtime = await own_dm_harness(tmp_path)
    assert states(runtime) == {
        "send": "untested", "delivery": "untested", "replies": "untested", "commands": "no",
    }  # fmt: skip
    await runtime.connector.notify_away(test=True)
    await send_to_own_dm(runtime, harness)
    found = can(runtime)
    assert states(runtime) == {
        "send": "yes", "delivery": "yes", "replies": "yes", "commands": "no",
    }, found  # fmt: skip
    assert all(found[what]["at"] for what in ("send", "delivery", "replies"))
    assert not found["send_only"]
    read = [args for tool, args in harness.mcp.calls if tool == "slack_read_thread"]
    assert read, "its thread was read back too"


async def test_a_message_not_found_when_read_back_is_not_confirmed(tmp_path):
    harness, runtime = await own_dm_harness(tmp_path)
    await runtime.connector.notify_away(test=True)
    await send_to_own_dm(runtime, harness, listed=False)
    found = can(runtime)
    assert found["delivery"]["state"] == "no" and "not found" in found["delivery"]["detail"]
    assert found["replies"]["state"] == "yes"
    # A later read of the DM finds it there.
    dm = json.loads(harness.store.get("away_slack_dm") or "{}")
    harness.mcp.reads[DM] = entry(DM, OWNER, "Me", dm["threads"][-1], "Chief of Staff")
    await runtime.connector.owner_dm_news()
    assert can(runtime)["delivery"]["state"] == "yes"


async def test_when_replies_cannot_be_read_notifications_are_send_only(tmp_path):
    """Requirement 5: only Send works: Settings says Send only, and what is sent asks for no
    reply there and points back to Chief of Staff."""
    harness, runtime = await own_dm_harness(tmp_path)
    worker, _task = await spawn(runtime, harness, "Answer the team")
    await call_tool(runtime, "draft_reply", worker, text="Friday works.", to="Bob", where="email")
    reading = harness.mcp.call

    async def call(name, arguments):
        if name in {"slack_read_channel", "slack_read_thread"}:
            raise SlackError("Slack rejected it: missing_scope")
        return await reading(name, arguments)

    harness.mcp.call = call
    await runtime.connector.notify_away()
    await send_to_own_dm(runtime, harness)
    sent = [args["message"] for tool, args in harness.mcp.calls if tool == "slack_send_message"]
    assert "Reply: `" in sent[0], "it was not known yet: sent with codes"
    assert sent[1] == "Replies here cannot be read now: answer in Chief of Staff on your computer."
    found = can(runtime)
    assert found["send_only"] and found["replies"]["state"] == "no"
    assert found["delivery"]["state"] == "no" and "could not read" in found["delivery"]["detail"]
    assert not harness.store.get("away_replies"), "no reads: nothing waits for a reply"
    await call_tool(runtime, "draft_reply", worker, text="Monday then.", to="Bob", where="email")
    await runtime.connector.notify_away()
    await settle(runtime)
    messenger = runtime.toolset.last_special("messenger")["id"]
    job = harness.session(messenger).sent[-1]
    assert "A: " not in job and "B: " in job and "version B" in job, job
    await send_to_own_dm(runtime, harness)  # It asks for codes: it gets none.
    plain = [args["message"] for tool, args in harness.mcp.calls if tool == "slack_send_message"]
    assert "Monday then" in plain[-1], plain[-1]
    assert "Reply" not in plain[-1] and "[" not in plain[-1]
    assert "Answer in Chief of Staff on your computer" in plain[-1]


async def test_no_kind_of_notification_suggests_replying_when_replies_are_not_read(tmp_path):
    """Requirement 5, every kind: without codes, no code, reply hint, reason that says what to
    answer, or note asking for a reply; a pointer back to Chief of Staff instead."""
    harness = Harness(tmp_path / "data", Settings(away_notify=True))
    runtime = await harness.connect()
    connector = runtime.connector
    harness.store.set("stopped:T2", "1")
    draft = lambda kind, target, text="": {"kind": kind, "target": target, "text": text}  # noqa: E731
    items = [
        {"test": True},
        {"ask": {"text": "Which port should the service use?", "meta": {}}},
        {"draft": draft("github", {"repo": "acme/app", "number": 7, "verdict": "approve",
                                   "pr": {"title": "Fix login"}}, "Looks good.")},
        {"draft": draft("github", {"repo": "acme/app", "number": 7, "in_reply_to": 1,
                                   "comment": {"author": "bob"}}, "Thanks Bob.")},
        {"draft": draft("slack", {"to": "Bob"}, "Friday works.")},
        {"draft": draft("reply", {"to": "Carol"}, "Sure, [yes/no] ___")},
        {"draft": draft("decision", {"question": "Which database?",
                                     "options": [{"choice": "SQLite"}, {"choice": "Postgres"}]})},
        {"draft": draft("merge", {"label": "acme/app#7", "title": "Fix login",
                                  "checks": "passing"})},
        {"draft": draft("workspace", {"title": "Fix login", "options": [{"path": "/src/app"}]})},
        {"draft": draft("question", {}, "Which region?")},
        {"question": {"task_title": "Pick a port", "worker": "w1", "text": "8080 or 9090?"}},
        {"task": {"id": "T1", "status": "failed", "title": "Build", "progress": "It broke."}},
        {"task": {"id": "T2", "status": "waiting", "title": "Deploy", "progress": ""}},
        {"task": {"id": "T3", "status": "waiting", "title": "Port", "progress": "Which one?"}},
    ]  # fmt: skip
    for number, item in enumerate(items):
        item |= {"id": f"item{number}", "code": f"COD{chr(65 + number)}2"}
    harness.store.set("awayreask:item4", 'not sent: answer "send" when it should go, or "no"')
    await connector.tell_away("Which one? Reply with its code: CODA2, CODB2", asks=True)
    await connector.tell_away("ZZZZ7 was answered already at 10:00: this reply changes nothing.")
    notes = connector.away_notes()
    with_codes, _ = connector.render(items, 2, True, notes)
    assert "Reply: `" in with_codes and "Which one?" in with_codes
    assert "when it should go" in with_codes, "with codes: the reason, and what to answer"
    plain, blocks = connector.render(items, 2, False, notes)
    for said in (plain, json.dumps(blocks, ensure_ascii=False)):
        # Not "Reply: `CODE send · no`", "Reply in this conversation", "Reply with its code".
        assert not re.search(r"Reply(?::| in | with )|`", said), said
        assert not any(item["code"] in said for item in items), said
        assert "when it should go" not in said
        assert "Answer in Chief of Staff on your computer" in said
    assert "Friday works." in plain and "Not sent yet" in plain, "the item itself stays"
    assert "this reply changes nothing" in plain, "a note that asks nothing stays"


async def test_a_notification_thread_that_cannot_be_read_back_means_replies_are_not_read(
    tmp_path,
):
    harness, runtime = await own_dm_harness(tmp_path)
    reading = harness.mcp.call

    async def call(name, arguments):
        if name == "slack_read_thread":
            raise SlackError("Slack rejected slack_read_thread: missing_scope")
        return await reading(name, arguments)

    harness.mcp.call = call
    await runtime.connector.notify_away(test=True)
    await send_to_own_dm(runtime, harness)
    found = can(runtime)
    assert found["replies"]["state"] == "no" and "thread" in found["replies"]["detail"]
    assert found["delivery"]["state"] == "yes" and found["send_only"]
    messenger = runtime.toolset.last_special("messenger")["id"]
    await call_tool(runtime, "read_owner_slack_dm", messenger)
    assert can(runtime)["coverage"]["detail"] == "could not read 1 of 1 notification threads"
    assert can(runtime)["replies"]["state"] == "no", "a read of part of it is no read of replies"


# Requirement 6: a read that covered only part of the owner's DM is not counted as complete.


async def test_a_thread_that_could_not_be_read_limits_coverage_until_a_full_read(tmp_path):
    from test_round9_away import own_dm

    harness, runtime, messenger, body, sent, _threads = await own_dm(tmp_path, "Friday works.")
    code = code_of(body, "Friday works")
    dm = json.loads(harness.store.get("away_slack_dm") or "{}")
    others = [f"{float(sent) - n:.6f}" for n in (40, 30, 20, 10)]
    harness.store.set("away_slack_dm", json.dumps({**dm, "threads": [*others, sent]}))
    failing: set[str] = set()
    reading = harness.mcp.call

    async def call(name, arguments):
        if name == "slack_read_thread" and arguments["message_ts"] in failing:
            raise SlackError("Slack rejected slack_read_thread: ratelimited")
        return await reading(name, arguments)

    harness.mcp.call = call
    harness.mcp.reads[DM] += "\n\n" + entry(DM, OWNER, "Me", stamp(10), f"{code} send")
    seen = harness.store.get("away_dm_seen")
    await call_tool(runtime, "read_owner_slack_dm", messenger)  # All of it: seen once reported.
    failing.update(others[:2])  # Then, before it reports, read again: only part of it.
    read = (await call_tool(runtime, "read_owner_slack_dm", messenger)).text_result_for_llm
    assert "Coverage limited: could not read 2 of 5 notification threads" in read, read
    coverage = can(runtime)["coverage"]
    assert coverage["limited"] and coverage["detail"] == (
        "could not read 2 of 5 notification threads"
    )
    said = await call_tool(
        runtime,
        "report_owner_replies",
        messenger,
        replies=[{"code": code, "text": f"{code} send", "intent": "send"}],
    )
    assert said.result_type == "success", said.text_result_for_llm  # fmt: skip
    assert harness.store.get("away_dm_seen") == seen, "a partial read is not all checked"
    assert await runtime.connector.owner_dm_news() is None, "read again later, not at once"
    failing.clear()
    assert await runtime.connector.owner_dm_news() is True, "now it can: all of it, at once"
    await call_tool(runtime, "read_owner_slack_dm", messenger)
    assert not can(runtime)["coverage"]["limited"], "a full read clears it"
    await call_tool(runtime, "report_owner_replies", messenger,
                    replies=[{"code": code, "text": f"{code} send", "intent": "send"}])  # fmt: skip
    assert float(harness.store.get("away_dm_seen") or 0) > float(seen or 0)


async def test_a_dm_listing_cut_short_limits_coverage(tmp_path):
    from test_round9_away import own_dm

    harness, runtime, messenger, _body, _sent, _threads = await own_dm(tmp_path, "Friday works.")
    harness.mcp.reads[DM] = "\n\n".join(
        entry(DM, OWNER, "Me", stamp(10 + n), f"note {n}") for n in range(DM_PAGE)
    )
    read = (await call_tool(runtime, "read_owner_slack_dm", messenger)).text_result_for_llm
    assert f"the DM listing stopped at {DM_PAGE} messages" in read, read
    assert can(runtime)["coverage"]["limited"]
    assert not harness.store.get("away_dm_seen"), "nothing counts as seen"


# Requirement 7: a watch partly covered in any report of a scan keeps its window.


async def test_a_watch_partly_covered_in_a_later_report_of_the_scan_keeps_its_window(tmp_path):
    settings = Settings(
        watches=[Watch(what="DMs to me"), Watch(what="PR mentions")], watch_after_hours_minutes=0
    )
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    connector = runtime.connector
    harness.store.set("watch_done_at", str(time.time() - 3 * 3600))
    before = [connector.watch_since(watch.what) for watch in settings.watches]
    await connector.scan()
    await settle(runtime)
    row = (connector.session() or {})["id"]
    await call_tool(runtime, "report_found", row, items=[])
    partly = [{"watch": 1, "reason": "the DM search returns only the newest 3 threads"}]
    await call_tool(runtime, "report_found", row, items=[], incomplete=partly)
    await call_tool(runtime, "report_found", row, items=[])  # It leaves it out: still partial.
    after = [connector.watch_since(watch.what) for watch in settings.watches]
    assert after[0] == before[0], "partly covered: read again from where it was"
    assert after[1] > before[1], "fully covered: reads on"
    assert connector.view()["last"]["incomplete"] == [{"watch": 1, "reason": partly[0]["reason"]}]
    harness.session(row).emit("session.idle")
    await settle(runtime)
    await connector.scan()  # The next scan covers it in full.
    await settle(runtime)
    await call_tool(runtime, "report_found", (connector.session() or {})["id"], items=[])
    assert connector.watch_since(settings.watches[0].what) > before[0]
    assert connector.view()["last"]["incomplete"] == []
