"""Teams lite with a large history (teams-v2.md §11.5): 40 workers, 10 current teams of 6 tasks
with 200 exchanges each, reused workers with 250 unrelated exchanges each, 5,000 past requests,
20,000 tasks and 60,000 messages. The first snapshot reads only the requests it shows (Past
teams' count comes from a read of the rest in the background, a short chunk at a time); then the
snapshot's teams cost what 0.1.56's did (unchanged, and after one task changes); a team's page
and a page of past teams are quick; every read is by a key or an index, whatever a reused
worker's other exchanges."""

import json
import statistics
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from conftest import runner_pace, settle

from chiefofstaff.store import now

WORKERS, CURRENT, PARTS, EXCHANGES = 40, 10, 6, 200
PAST, PAST_PARTS, UNRELATED, TASKS, MESSAGES = 5000, 3, 250, 20_000, 60_000


def timed(work, rounds: int = 7) -> float:
    """Its median time in ms."""
    found = []
    for _ in range(rounds):
        start = time.perf_counter()
        work()
        found.append((time.perf_counter() - start) * 1000)
    return statistics.median(found)


def history(harness) -> dict:
    store = harness.store
    router = harness.router_id
    workers = [str(uuid4()) for _ in range(WORKERS)]
    for number, worker in enumerate(workers):
        store.create_session(worker, "worker", f"Worker {number}", "idle")
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    message_id = (store.value("SELECT max(id) FROM messages") or 0) + 1
    messages, tasks, kv = [], [], {}
    serial = iter(range(10**7))

    def task(worker, title, status, at, ended=None) -> str:
        identifier = f"t{next(serial):07x}"
        tasks.append((identifier, worker, title, status, at, at, ended))
        return identifier

    def message(session, kind, text, meta, at) -> int:
        nonlocal message_id
        messages.append((message_id, session, kind, text, json.dumps(meta), at))
        message_id += 1
        return message_id - 1

    def request(text, at, made):
        identifier = str(message(router, "owner", text, {}, at))
        kv[f"reqtasks:{identifier}"] = json.dumps(made)
        for index, made_task in enumerate(made):
            kv[f"req:{made_task}"] = identifier
            if index:
                kv[f"with:{made_task}"] = json.dumps([made[index - 1]])
        return identifier

    def listed(task_id, message_id):
        key = f"xchg:{task_id}"
        kv[key] = json.dumps([*json.loads(kv.get(key, "[]")), message_id][-500:])

    for number in range(PAST):
        made = [
            task(
                workers[(number + part) % WORKERS],
                f"Old job {number} part {part}",
                "done",
                old,
                old,
            )
            for part in range(PAST_PARTS)
        ]
        request(f"Old job {number}: in parts.", old, made)
    current = []
    for number in range(CURRENT):
        made = [
            task(
                workers[(number * 3 + part) % WORKERS],
                f"Job {number} part {part}",
                "running",
                recent,
            )
            for part in range(PARTS)
        ]
        current.append((request(f"Job {number}: in six parts.", recent, made), made))
        for index in range(EXCHANGES):
            one, other = made[index % PARTS], made[(index + 1) % PARTS]
            stamped = {"target": "w", "from_task": one, "to_task": other}
            sent = message(workers[0], "tell", f"Note {index} of job {number}.", stamped, recent)
            listed(one, sent)
            listed(other, sent)
    # Reused workers' exchanges about other work (their past tasks): never a team's.
    for worker_number, worker in enumerate(workers):
        mine = [t[0] for t in tasks if t[1] == worker and t[3] == "done"][:3]
        for index in range(UNRELATED):
            stamped = {"target": "w", "from_task": mine[index % len(mine)], "to_task": mine[0]}
            sent = message(worker, "tell", f"Unrelated {worker_number}/{index}.", stamped, old)
            listed(mine[index % len(mine)], sent)
    while len(tasks) < TASKS:
        task(workers[len(tasks) % WORKERS], "A task of its own", "done", old, old)
    while len(messages) < MESSAGES:
        message(workers[len(messages) % WORKERS], "tool", "bash pytest -q", {}, old)
    with store.db:
        store.db.executemany(
            "INSERT INTO tasks(id, session_id, title, status, created_at, updated_at, ended_at) "
            "VALUES (?,?,?,?,?,?,?)",
            tasks,
        )
        store.db.executemany(
            "INSERT INTO messages(id, session_id, kind, text, meta, created_at) VALUES "
            "(?,?,?,?,?,?)",
            messages,
        )
        store.db.executemany(
            "INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value",
            list(kv.items()),
        )
    return {"current": current, "workers": workers}


async def test_teams_cost_a_snapshot_what_0156s_did_with_a_large_history(harness):
    runtime = await harness.connect()
    made = history(harness)
    pace = runner_pace()
    lite = runtime.teams_lite
    listed = runtime.tasks.listed(include_ended_since=now())
    assert len(listed) == CURRENT * PARTS
    chunks: list[float] = []
    read_all = lite.read_all

    def timed_chunk(requests):
        start = time.perf_counter()
        read_all(requests)
        chunks.append((time.perf_counter() - start) * 1000)

    lite.read_all = timed_chunk
    started = time.perf_counter()
    snapshot = lite.snapshot(listed)
    cold = (time.perf_counter() - started) * 1000
    assert len(snapshot["teams"]) == CURRENT and snapshot["past_teams"] is None
    assert cold < 50 * pace, f"the first snapshot: {cold:.0f} ms"
    # The rest, in the background: Past teams' count once it is read, never long at a time.
    published = runtime.publish_state
    runtime.publish_state = lambda *args, **kwargs: None
    await settle(runtime)
    runtime.publish_state = published
    assert lite.loaded and len(chunks) >= PAST // 250, chunks
    assert max(chunks) < 60 * pace, f"a chunk of the background read: {max(chunks):.0f} ms"
    snapshot = lite.snapshot(listed)
    assert len(snapshot["teams"]) == CURRENT and snapshot["past_teams"] == PAST
    # Unchanged: what the snapshot had in 0.1.56 (Teams.view), and Teams lite's now.
    old = timed(runtime.teams.view)
    new = timed(lambda: lite.snapshot(listed))
    assert new <= old + 1.0 * pace, (new, old)
    # After one task changes (the change itself not counted).
    _, team_tasks = made["current"][0]

    def after_change(work, rounds: int = 9) -> float:
        found = []
        for number in range(rounds):
            runtime.tasks.update(team_tasks[0], progress=f"step {number} {time.perf_counter()}")
            start = time.perf_counter()
            work()
            found.append((time.perf_counter() - start) * 1000)
        return statistics.median(found)

    old = after_change(runtime.teams.view)
    new = after_change(lambda: lite.snapshot(listed))
    assert new <= old + 3.0 * pace, (new, old)
    # A team's page: its first page, whatever its reused workers' other exchanges.
    team = snapshot["teams"][0]["id"]
    page = timed(lambda: lite.page(team), rounds=5)
    first = lite.page(team)
    assert len(first["items"]) == 50 and first["next"]
    assert page <= 20.0 * pace, f"a team's page: {page:.1f} ms"
    # A page of past teams.
    past = timed(lambda: lite.past(limit=20), rounds=5)
    assert len(lite.past(limit=20)["teams"]) == 20
    assert past <= 20.0 * pace, f"a page of past teams: {past:.1f} ms"


def test_every_read_is_by_a_key_or_an_index(harness):
    """EXPLAIN QUERY PLAN for each of Teams lite's reads: a key or an index, no scan."""
    reads = [
        "SELECT key, value FROM kv WHERE key IN (?,?)",
        "SELECT id, session_id, title, status, created_at, updated_at, ended_at FROM tasks "
        "WHERE id IN (?,?)",
        "SELECT task_id, depends_on, needs FROM task_deps WHERE task_id IN (?,?)",
        "SELECT id, text, created_at FROM messages WHERE id IN (?,?)",
        "SELECT id, session_id, kind, text, meta, created_at FROM messages WHERE id IN (?,?)",
        "SELECT * FROM questions WHERE id IN (?,?)",
        "SELECT id, meta FROM messages WHERE session_id=? AND kind='owner' ORDER BY id DESC "
        "LIMIT ?",
        "SELECT id FROM messages WHERE session_id=? AND kind='owner' AND id<? ORDER BY id DESC "
        "LIMIT ?",
        "SELECT created_at FROM messages WHERE id=?",
        "SELECT key FROM kv WHERE key >= ? AND key < ?",
        "SELECT status FROM tasks WHERE id IN (?,?)",
    ]
    for sql in reads:
        plan = " | ".join(
            row[-1]
            for row in harness.store.db.execute(
                f"EXPLAIN QUERY PLAN {sql}", tuple("x" for _ in range(sql.count("?")))
            )
        )
        assert "SCAN" not in plan or "USING" in plan, (sql, plan)
        assert "USING" in plan or "PRIMARY KEY" in plan or "INTEGER PRIMARY KEY" in plan, (
            sql,
            plan,
        )
