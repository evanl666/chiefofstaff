"""Usage as Copilot counts it (E131-E136): premium requests only for a call a user message
started, compactions' calls, the prompt cache's tokens apart, what the spend went to, by task."""

import json
import sqlite3
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from conftest import call_tool, settle

from chiefofstaff.store import Store

# A real Copilot session (CLI 1.0.112, claude-haiku-5.5): a message whose turn called a tool
# twice, then another message; then a compaction. Its calls as the CLI sent them, and its last
# checkpoint's totals (session.usage_checkpoint).
CALLS = [  # initiator, input, output, cache read, cache write, nano-AIU
    ("user", 22611, 47, 16027, 6580, 100_667_000),
    ("agent", 22668, 46, 22607, 59, 25_664_500),
    ("agent", 22723, 3, 22666, 55, 23_523_500),
    ("user", 22771, 4, 22721, 46, 23_536_000),
]
COMPACTION = (24446, 462, 22767, 1675, 66_844_500)
CHECKPOINT = SimpleNamespace(total_nano_aiu=240_235_500, _total_premium_requests=2.0)


def compacted(model="claude-haiku-5.5"):
    tokens_in, tokens_out, read, written, nano = COMPACTION
    used = SimpleNamespace(
        model=model, input_tokens=tokens_in, output_tokens=tokens_out, cache_read_tokens=read,
        cache_write_tokens=written, request_count=1,
        _copilot_usage=SimpleNamespace(total_nano_aiu=nano),
    )  # fmt: skip
    return {"success": True, "model": model, "compaction_tokens_used": used}


async def spawn(runtime, router, title):
    result = await call_tool(runtime, "spawn_worker", router, title=title, instructions=title)
    return json.loads(result.text_result_for_llm)


async def test_premium_requests_and_credits_add_up_as_copilots_checkpoint_says(harness):
    """Every call was counted a premium request at its model's multiplier, the agent's own next
    calls too (56,255 a day against Copilot's 8,460); a compaction's calls were not counted at
    all. As Copilot counts them: its checkpoint's premium requests and credits."""
    runtime = await harness.connect()
    session = harness.session(harness.router_id)
    for initiator, tokens_in, tokens_out, read, written, nano in CALLS:
        session.emit(
            "assistant.usage", model="claude-haiku-5.5", initiator=initiator, cost=1.0,
            input_tokens=tokens_in, output_tokens=tokens_out, cache_read_tokens=read,
            cache_write_tokens=written, copilot_usage=SimpleNamespace(total_nano_aiu=nano),
        )  # fmt: skip
    session.emit("session.compaction_complete", **compacted())
    session.emit("session.usage_checkpoint", **vars(CHECKPOINT))
    await settle(runtime)
    total = harness.store.usage(0, time.time() + 1)["total"]
    assert total["premium"] == CHECKPOINT._total_premium_requests
    assert round(total["credits"] * 1e9) == CHECKPOINT.total_nano_aiu
    assert (total["calls"], total["compactions"], total["premium_unread"]) == (5, 1, 0)
    # The tokens: Copilot's input count holds the cache's part.
    rows = [call[1:5] for call in CALLS] + [COMPACTION[:4]]
    assert total["cache_read_tokens"] == sum(row[2] for row in rows)
    assert total["cache_write_tokens"] == sum(row[3] for row in rows)
    # 16, as Copilot's own count at the session's end (its accountingSnapshot's input).
    assert total["fresh_input_tokens"] == sum(row[0] - row[2] - row[3] for row in rows) == 16
    assert total["output_tokens"] == sum(row[1] for row in rows)
    assert total["context_per_call"] == round(sum(call[1] for call in CALLS) / 4)


async def test_a_call_copilot_leaves_unmarked_is_a_users_and_a_failed_compaction_is_none(
    harness,
):
    runtime = await harness.connect()
    session = harness.session(harness.router_id)
    session.emit("assistant.usage", model="m", cost=0.33, input_tokens=10, output_tokens=1)
    session.emit("assistant.usage", model="m", cost=0.33, initiator="sub-agent")
    session.emit("session.compaction_complete", success=False, compaction_tokens_used=None)
    await settle(runtime)
    total = harness.store.usage(0, time.time() + 1)["total"]
    assert (total["premium"], total["calls"]) == (pytest.approx(0.33), 2)


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def logged_call(initiator, tokens_in, tokens_out, read, written, nano, when):
    usage = {"model": "m", "inputTokens": tokens_in, "outputTokens": tokens_out, "cost": 1.0,
             "cacheReadTokens": read, "cacheWriteTokens": written, "initiator": initiator,
             "copilotUsage": {"totalNanoAiu": nano}}  # fmt: skip
    return {"type": "session.usage_record", "data": {"usage": usage}, "timestamp": iso(when)}


def logged_compaction(when):
    tokens_in, tokens_out, read, written, nano = COMPACTION
    used = {"model": "m", "inputTokens": tokens_in, "outputTokens": tokens_out,
            "cacheReadTokens": read, "cacheWriteTokens": written, "requestCount": 1,
            "copilotUsage": {"totalNanoAiu": nano}}  # fmt: skip
    data = {"success": True, "compactionTokensUsed": used}
    return {"type": "session.compaction_complete", "data": data, "timestamp": iso(when)}


async def test_calls_kept_before_are_read_back_from_their_logs_once(harness):
    """What an earlier version kept: what started each call and its cache tokens, read back
    from the session's logs (a Router's earlier conversation too), and the compactions it never
    counted, with the run they were in. Calls whose log is gone stay apart, said."""
    runtime = await harness.connect()
    store, router = harness.store, harness.router_id
    worker = (await spawn(runtime, router, "Fix login"))["session_id"]
    await settle(runtime)
    run = runtime.tasks.run(worker)
    started = time.time()
    for _, tokens_in, tokens_out, _, _, nano in CALLS:
        store.record_usage(worker, "worker", "m", nano / 1e9, 1.0, tokens_in, tokens_out,
                           run_id=run["id"])  # fmt: skip
    store.record_usage(router, "router", "m", 0.5, 1.0, 900, 9)
    store.record_usage("gone", "worker", "m", 0.5, 1.0, 800, 8)
    store.execute("UPDATE usage SET initiator=''")  # As an earlier version kept them.
    harness.client.logs[worker] = [logged_call(*call, started) for call in CALLS] + [
        logged_compaction(started + 1)
    ]
    store.set("alias:old-router", router)  # The Router's conversation before it rotated.
    harness.client.logs["old-router"] = [logged_call("agent", 900, 9, 800, 90, 5e8, started)]
    store.create_session("gone", "worker", "Old work", "idle", cwd="/tmp")
    assert store.usage(0, time.time() + 1)["total"]["premium"] == 0, "none read yet"

    await runtime.workers.read_back()
    await runtime.workers.read_back()  # Once: nothing new the second time.
    total = store.usage(0, time.time() + 1)["total"]
    assert (total["premium"], total["premium_unread"], total["compactions"]) == (2, 1, 1)
    assert total["cache_read_tokens"] == sum(call[3] for call in CALLS) + 800 + COMPACTION[2]
    compaction = store.one("SELECT * FROM usage WHERE kind='compaction'")
    assert compaction["run_id"] == run["id"] and compaction["session_id"] == worker
    assert compaction["credits"] == pytest.approx(COMPACTION[4] / 1e9)


async def test_a_terminal_turns_calls_and_compactions_count_as_copilot_counts_them(harness):
    runtime = await harness.connect()
    harness.store.create_session("term-9", "worker", "Research", "idle", cwd="/tmp")
    harness.store.execute(
        "UPDATE sessions SET created_at=? WHERE id='term-9'",
        (datetime.fromtimestamp(time.time() - 3600, UTC).isoformat(),),
    )
    harness.client.logs["term-9"] = [logged_call(*call, time.time()) for call in CALLS] + [
        logged_compaction(time.time())
    ]
    await runtime.workers.sync_all_usage()
    await runtime.workers.sync_all_usage()
    total = harness.store.usage(0, time.time() + 1)["total"]
    assert (total["premium"], total["calls"], total["compactions"]) == (2, 5, 1)
    assert round(total["credits"] * 1e9) == CHECKPOINT.total_nano_aiu


def test_an_earlier_versions_usage_gains_what_started_each_call(tmp_path):
    with sqlite3.connect(tmp_path / "chiefofstaff.db") as old:
        old.execute(
            """CREATE TABLE usage (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
            role TEXT NOT NULL, model TEXT NOT NULL, credits REAL NOT NULL,
            premium REAL NOT NULL, input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL, at REAL NOT NULL)"""
        )
        old.execute("INSERT INTO usage VALUES (1, 's', 'router', 'm', 2.0, 1.0, 10, 1, 5)")
    store = Store(tmp_path)
    row = store.one("SELECT * FROM usage")
    assert (row["initiator"], row["kind"], row["calls"], row["cache_read_tokens"]) == (
        "",
        "call",
        1,
        0,
    )
    total = store.usage(0, 10)["total"]
    assert (total["premium"], total["premium_unread"], total["credits"]) == (0, 1.0, 2.0)


def add_task(store, task, session, status, title=""):
    store.execute(
        "INSERT INTO tasks(id, session_id, title, status, created_at, updated_at) "
        "VALUES (?,?,?,?,'now','now')",
        (task, session, title or task, status),
    )
    store.execute(
        "INSERT INTO runs(id, task_id, session_id, status, started_at) VALUES (?,?,?,'done','now')",
        ("r" + task, task, session),
    )


async def test_the_spend_by_what_it_went_to(harness):
    """Per accepted result divided running tasks' spend by finished tasks and left out what
    no task spent (40%). Now: accepted (each), in progress (so far), failed or cancelled, and
    what went to no task, by whom; each task with spend in the range, once."""
    await harness.connect()
    store = harness.store
    for task, session, status, credits_used in [
        ("t1", "w1", "done", 4.0),
        ("t2", "w2", "done", 2.0),
        ("t3", "w1", "running", 3.0),
        ("t4", "w3", "review", 1.0),
        ("t5", "w3", "failed", 0.5),
        ("t6", "watch", "done", 9.0),  # The Watch's scan: no task of the owner's.
    ]:
        add_task(store, task, session, status)
        store.record_usage(session, "worker", "m", credits_used, 0, 1, 1, run_id="r" + task)
    store.record_usage("w1", "worker", "m", 1.0, 0, 1, 1, run_id="rt1")
    store.record_usage(harness.router_id, "router", "m", 12.0, 0, 1, 1)
    store.record_usage("w2", "worker", "m", 0.25, 0, 1, 1)  # Between tasks.
    store.create_session("watch", "worker", "Watch for you", "idle", cwd="/tmp")
    store.set("connector:watch", "1")
    spend = store.usage(0, time.time() + 1)["spend"]
    assert spend["accepted"] == {"tasks": 2, "credits": 7.0, "each": 3.5}
    assert spend["in_progress"] == {"tasks": 2, "credits": 4.0}
    assert spend["stopped"] == {"tasks": 1, "credits": 0.5}
    assert spend["untasked"]["credits"] == 21.25
    assert [part["who"] for part in spend["untasked"]["by"]] == [
        "Router",
        "Watch for you",
        "Workers between tasks",
    ]


async def test_spend_by_task_and_each_workers_tasks(harness):
    """By session named a worker's whole spend after its current task. Now each worker has its
    tasks under it, and By task has each task with its worker, and what no task spent."""
    runtime = await harness.connect()
    router = harness.router_id

    def spent(session_id, nano):
        harness.session(session_id).emit(
            "assistant.usage", model="m", cost=1.0, input_tokens=1, output_tokens=1,
            copilot_usage=SimpleNamespace(total_nano_aiu=nano),
        )  # fmt: skip

    worker = (await spawn(runtime, router, "Fix login"))["session_id"]
    await settle(runtime)
    first = runtime.tasks.current(worker)
    spent(worker, 3e9)
    await settle(runtime)
    await call_tool(runtime, "report", worker, status="done", summary="Fixed.")
    await call_tool(runtime, "message_session", router, session_id=worker, task="new",
                    title="Write the docs", text="Write them", why_new="test")  # fmt: skip
    await settle(runtime)
    spent(worker, 1e9)
    spent(router, 2e9)
    await settle(runtime)
    usage = runtime.usage(0, time.time() + 1)
    tasks = [(row["title"], row.get("worker"), row["credits"]) for row in usage["by_task"]]
    assert tasks == [
        ("Fix login", "Write the docs", 3.0),
        ("Write the docs", "Write the docs", 1.0),
        ("", None, 2.0),
    ], "each task with its worker's name; last, what no task spent"
    assert usage["by_task"][0]["task_id"] == first["id"]
    rows = {row["session_id"]: row for row in usage["by_session"]}
    assert rows[worker]["worker"] and rows[worker]["credits"] == 4.0
    assert [(t["title"], t["credits"]) for t in rows[worker]["tasks"]] == [
        ("Fix login", 3.0),
        ("Write the docs", 1.0),
    ]
    assert "tasks" not in rows[router]
