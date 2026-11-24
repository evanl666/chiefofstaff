"""Task relationships (E130): how the tasks of workers that work together are related, read
from what is stored (relations.py), and how a team's view and Home's line say it."""

import json
import time
from datetime import UTC, datetime, timedelta

from conftest import call_tool, settle

from chiefofstaff.relations import LINEAGE_NODES, chip_of, lineage


async def spawn(runtime, router, title, **extra) -> tuple[str, str]:
    result = await call_tool(
        runtime, "spawn_worker", router, title=title, instructions=title, why_new="test", **extra
    )
    found = json.loads(result.text_result_for_llm)
    return found["session_id"], found["task_id"]


def edges(runtime, *kinds) -> set[tuple]:
    return {
        (edge["kind"], edge["from_task"], edge["to_task"], edge["state"])
        for edge in runtime.relations.graph().edges
        if not kinds or edge["kind"] in kinds
    }


async def done(runtime, worker, summary="Done."):
    await call_tool(runtime, "report", worker, status="done", summary=summary)
    await settle(runtime)


async def test_what_a_new_worker_builds_on_works_beside_and_replaces_is_kept(harness):
    """Kept as it was not before: the tasks a new worker was given to build on (context_from:
    only in its prompt), the tasks of the workers it was started beside (works_with: per worker
    only) and the task a new one supersedes in the same worker (only a cancel)."""
    runtime = await harness.connect()
    router = harness.router_id
    harness.store.save_settings(harness.store.settings().model_copy(update={"max_workers": 100}))
    _, research = await spawn(runtime, router, "Research caching")
    _, built = await spawn(runtime, router, "Implement caching", context_from=[research])
    assert json.loads(harness.store.get(f"contextfrom:{built}")) == [research]
    _, api = await spawn(runtime, router, "Token API")
    _, client = await spawn(runtime, router, "Token client", works_with=[api])
    assert json.loads(harness.store.get(f"parts:{client}")) == [api]
    worker, old = await spawn(runtime, router, "Fix the login")
    session = harness.session(worker)
    session.emit("user.message", content=session.sent[-1])  # At work on it.
    await settle(runtime)
    await call_tool(runtime, "message_session", router, session_id=worker, task="new",
                    why_new="test", earlier="superseded", title="Rewrite the login",
                    text="Rewrite it from scratch.")  # fmt: skip
    await settle(runtime)
    new = runtime.tasks.current(worker)["id"]
    assert harness.store.get(f"supersededby:{old}") == new


async def test_each_stored_relation_is_one_edge_from_the_earlier_task(harness):
    runtime = await harness.connect()
    router = harness.router_id
    scout, token = await spawn(runtime, router, "Token scout")
    writer, write = await spawn(runtime, router, "Token writer", after=[token])
    assert edges(runtime, "after") == {("after", token, write, "pending")}
    await done(runtime, scout)
    [edge] = runtime.relations.graph().edges
    assert (edge["state"], edge["created_by"], edge["verb"]) == ("resolved", "router", "waits")
    assert edge["resolved_at"] and edge["from_session"] == scout and edge["to_session"] == writer
    # Done instead by a new task (replaces): from the old one to the new.
    _, flaky = await spawn(runtime, router, "Fix the flaky test")
    _, again = await spawn(runtime, router, "Fix the flaky test again", replaces=[flaky])
    assert ("replaced", flaky, again, "resolved") in edges(runtime)
    # Built on (context_from), unless it replaces that task already.
    _, docs = await spawn(runtime, router, "Write the token docs", context_from=[token])
    assert ("context", token, docs, "resolved") in edges(runtime)
    # Split off a task, by the owner or the Router (splitfrom), or by its worker (splitoff).
    split = runtime.tasks.create(scout, "Rotate staging too", "Split off.", status="review")
    harness.store.set(f"splitfrom:{split['id']}", json.dumps({"task_id": token, "until": ""}))
    harness.store.set(f"titles:{split['id']}", json.dumps(
        [{"title": "split from “Token scout”", "at": "", "by": "you"}]))  # fmt: skip
    off = runtime.tasks.create(writer, "Write the README", "Split off.", status="running")
    harness.store.set(f"splitoff:{write}", off["id"])
    found = {e["id"]: e for e in runtime.relations.graph().edges}
    assert found[f"split:{token}:{split['id']}"]["created_by"] == "owner"
    assert found[f"split:{write}:{off['id']}"]["created_by"] == "worker"
    assert {e["verb"] for e in found.values() if e["kind"] == "split"} == {"branched"}
    # A draft replaced by another (the key drafts use too) is no task's relation.
    harness.store.set("replacedby:d1234567", "d7654321")
    assert not [e for e in runtime.relations.graph().edges if "d1234567" in e["id"]]


async def test_a_hand_over_a_fork_and_a_compare_are_edges_of_the_tasks_then(harness):
    """Kept per worker (Continue with… `continues:`, a fork `forkof:`, Compare `compare:`): the
    new worker's first task and the task the other one was on then. `continued:` is a fresh
    Copilot session of the same worker, never a relation."""
    runtime = await harness.connect()
    router = harness.router_id
    old, first = await spawn(runtime, router, "Checkout API")
    runtime.tasks.create(old, "Checkout docs", "Later work.")  # Given after the hand-over.
    harness.store.execute(
        "UPDATE tasks SET created_at='2999-01-01T00:00:00+00:00' WHERE session_id=? AND id!=?",
        (old, first),
    )
    new, picked_up = await spawn(runtime, router, "Checkout API on Codex")
    harness.store.set(f"continues:{new}", old)
    harness.store.set(f"continued:{old}", "a-copilot-session-id")
    fork, forked = await spawn(runtime, router, "Checkout API metrics")
    harness.store.set(f"forkof:{fork}", json.dumps({"id": old, "title": "Checkout API"}))
    graph = {(e["kind"], e["from_task"], e["to_task"]) for e in runtime.relations.graph().edges}
    assert ("handoff", first, picked_up) in graph and ("fork", first, forked) in graph
    one, a = await spawn(runtime, router, "Fix the flaky test")
    two, b = await spawn(runtime, router, "Fix the flaky test (Codex)")
    group = json.dumps({"id": "c0ffee00", "title": "Fix the flaky test", "agents": ["a", "b"]})
    harness.store.set(f"compare:{one}", group)
    harness.store.set(f"compare:{two}", group)
    [compare] = [g for g in runtime.relations.graph().groups if g["kind"] == "compare"]
    assert (compare["tasks"], compare["state"], compare["picked"]) == ([a, b], "pending", "")
    runtime.tasks.move(b, "done")
    [compare] = [g for g in runtime.relations.graph().groups if g["kind"] == "compare"]
    assert (compare["state"], compare["picked"]) == ("resolved", b)
    # All of them join their workers into teams (none of them ever messaged).
    members = {frozenset(team) for team in runtime.teams.components()}
    assert frozenset({old, new, fork}) in members and frozenset({one, two}) in members


async def test_workers_a_relation_joins_are_one_team_a_shared_pr_alone_never(harness):
    runtime = await harness.connect()
    router = harness.router_id
    scout, token = await spawn(runtime, router, "Token scout")
    writer, _ = await spawn(runtime, router, "Token writer", after=[token])
    lone, solo = await spawn(runtime, router, "Billing report")
    other, also = await spawn(runtime, router, "Billing chart")
    for task in (solo, also):
        harness.store.set(f"prs:{task}", json.dumps(["https://github.com/acme/api/pull/482"]))
    assert runtime.peers.of(scout) == [] and runtime.peers.of(writer) == []
    # The Router's teams (teams.py, as before; the owner's are Teams lite's).
    teams = runtime.teams.view()
    assert [team["members"] for team in teams] == [[scout, writer]]
    assert teams[0]["cue"] == {"parts": None, "waiting": 1}
    views = {s["id"]: s for s in runtime.snapshot()["sessions"]}
    assert views[lone]["team"] == views[other]["team"] == ""
    # Two that share a PR, in one team for another reason: the PR is its anchor, a chip.
    await call_tool(runtime, "ask_session", other, session_id=lone, question="Which month?")
    thread = runtime.teams.thread_view(f"team-{lone[:8]}")
    assert [(a["kind"], a["label"], sorted(a["tasks"])) for a in thread["anchors"]] == [
        ("pr", "#482", sorted([solo, also]))
    ]
    assert thread["relations"] == []


async def test_a_requests_parts_are_one_fan_ready_only_when_every_part_landed(harness):
    runtime = await harness.connect()
    router = harness.router_id
    api, a = await spawn(runtime, router, "Checkout API")
    ui, b = await spawn(runtime, router, "Checkout UI", works_with=[a])
    tests, c = await spawn(runtime, router, "Checkout tests", works_with=[a, b])
    [group] = runtime.relations.graph().groups
    assert (group["kind"], group["tasks"], group["total"]) == ("parts", [a, b, c], 3)
    assert (group["landed"], group["state"]) == (0, "pending")
    assert runtime.teams.view()[0]["cue"] == {"parts": [0, 3], "waiting": 0}
    await done(runtime, api)
    await done(runtime, ui)
    [group] = runtime.relations.graph().groups
    assert (group["landed"], group["state"], group["landed_at"]) == (2, "pending", "")
    # A part that failed never lands: the fan is never ready, never looks done.
    harness.session(tests).emit("session.error", message="Execution failed: boom")
    await settle(runtime)
    [group] = runtime.relations.graph().groups
    assert (group["landed"], group["failed"], group["state"]) == (2, 1, "pending")
    # Done instead by another task, that one is the part.
    _, d = await spawn(runtime, router, "Checkout tests again", replaces=[c])
    [group] = runtime.relations.graph().groups
    assert group["tasks"] == [a, b, d] and group["failed"] == 0
    harness.store.execute("UPDATE tasks SET status='review' WHERE id=?", (d,))
    [group] = runtime.relations.graph().groups
    assert (group["landed"], group["state"]) == (3, "resolved") and group["landed_at"]
    assert runtime.teams.view()[0]["cue"]["parts"] is None, "landed: nothing to say"


def task(task_id, created="2026-11-21T10:00:00+00:00", status="running"):
    return {"id": task_id, "created_at": created, "status": status, "session_id": "w" + task_id}


def edge(kind, source, target, state="resolved", at="2026-11-21T10:00:00+00:00", resolved=""):
    return {"id": f"{kind}:{source}:{target}", "kind": kind, "from_task": source,
            "to_task": target, "state": state, "at": at, "resolved_at": resolved}  # fmt: skip


NOW = datetime(2026, 11, 21, 10, 30, tzinfo=UTC)


def test_a_rows_one_chip_is_the_most_telling_and_the_rest_a_count():
    parts = {"id": "parts-t1", "kind": "parts", "tasks": ["t1", "t2", "t3"], "state": "pending",
             "landed": 1, "total": 3, "failed": 0, "picked": "",
             "started_at": "2026-11-21T09:00:00+00:00"}  # fmt: skip
    waits = edge("after", "t0", "t2", "pending", at="2026-11-21T10:21:00+00:00")
    split = edge("split", "t9", "t2")
    chip, more = chip_of(task("t2"), [waits, split], [parts], None, {}, NOW)
    assert (chip["kind"], chip["other"], chip["minutes"], more) == ("after", "t0", 9, 2)
    # Cleared a minute ago: still its chip ("Ada delivered · unblocked"); later, its part.
    cleared = {**waits, "state": "resolved", "resolved_at": "2026-11-21T10:29:00+00:00"}
    chip, _ = chip_of(task("t2"), [cleared, split], [parts], None, {}, NOW)
    assert (chip["kind"], chip.get("just")) == ("after", True)
    cleared["resolved_at"] = "2026-11-21T10:20:00+00:00"
    chip, more = chip_of(task("t2"), [cleared, split], [parts], None, {}, NOW)
    assert (chip["kind"], chip["landed"], chip["total"], more) == ("parts", 1, 3, 2)
    # Done instead by another: its row is over (above a part).
    chip, _ = chip_of(task("t2"), [edge("replaced", "t2", "t5"), split], [parts], None, {}, NOW)
    assert (chip["kind"], chip["side"], chip["other"]) == ("replaced", "from", "t5")
    # A teammate's question its task waits on is its wait, the one chip (never both).
    question = {"id": "q1", "target": "wB", "minutes": 34, "asked": "2026-11-21T09:56:00+00:00"}
    chip, more = chip_of(task("t2"), [split], [parts], question, {}, NOW)
    assert (chip["kind"], chip["minutes"], more) == ("question", 34, 2)
    chip, more = chip_of(task("t2"), [waits, split], [], question, {}, NOW)
    assert (chip["kind"], more) == ("question", 2), "the longer wait; the other a count"
    # Branched from another, then what it continues; follow-ups a count, last.
    chip, _ = chip_of(task("t2"), [edge("context", "t7", "t2"), split], [], None, {}, NOW)
    assert (chip["kind"], chip["side"]) == ("split", "to")
    chip, more = chip_of(task("t4"), [], [], None, {"t4": 2}, NOW)
    assert (chip, more) == ({"kind": "followups", "count": 2}, 0)
    assert chip_of(task("t4"), [], [], None, {}, NOW) == (None, 0)


def test_a_lineage_is_two_steps_each_way_at_most_five_tasks_and_never_crosses():
    chain = [edge("replaced", f"t{n}", f"t{n + 1}", at=f"2026-11-21T1{n}:00:00+00:00")
             for n in range(7)]  # fmt: skip
    strip = lineage("t3", chain, [])
    assert strip["columns"] == [["t1"], ["t2"], ["t3"], ["t4"], ["t5"]]
    assert (strip["at"], strip["earlier"], strip["later"]) == (2, 1, 2)
    # Its parts under it, in its own column; the nearest step each way first.
    parts = {"id": "parts-t3", "kind": "parts", "tasks": ["t3", "p1", "p2", "p3"]}
    strip = lineage("t3", chain, [parts])
    assert strip["columns"] == [["t2"], ["t3", "p1", "p2"], ["t4"]]
    assert (strip["siblings"], strip["earlier"], strip["later"]) == (1, 2, 3)
    assert sum(len(column) for column in strip["columns"]) == LINEAGE_NODES
    # Two it waits on: one column (a fan in), the nearest first; a step beyond joins the top.
    waits = [edge("after", "a", "t", at="2026-11-21T09:00:00+00:00"),
             edge("after", "b", "t", at="2026-11-21T10:00:00+00:00"),
             edge("context", "z", "b"), edge("context", "y", "a")]  # fmt: skip
    strip = lineage("t", waits, [])
    assert strip["columns"] == [["z"], ["b", "a"], ["t"]]
    assert lineage("t9", chain, []) is None


async def test_a_teams_thread_has_its_tasks_and_how_they_are_related(harness):
    from chiefofstaff.teams import Teams

    runtime = await harness.connect()
    router = harness.router_id
    scout, token = await spawn(runtime, router, "Token scout")
    writer, write = await spawn(runtime, router, "Token writer", works_with=[token])
    docs, document = await spawn(runtime, router, "Token docs", after=[write])
    thread = runtime.teams.thread_view(f"team-{scout[:8]}")
    assert thread["team"]["members"] == [scout, writer, docs]
    rows = {row["id"]: row for row in thread["tasks"]}
    assert [row["id"] for row in thread["tasks"]] == [token, write, document]
    assert rows[document]["relation"]["kind"] == "after" and rows[document]["more"] == 0
    assert rows[token]["relation"]["kind"] == "parts" and rows[write]["more"] == 1
    assert {e["kind"] for e in thread["relations"]} == {"after", "part"}
    assert [g["kind"] for g in thread["groups"]] == ["parts"]
    assert rows[write]["lineage"]["columns"] == [[write, token], [document]]
    assert thread["summary"]["waiting"] == 1 and thread["summary"]["parts"][0]["total"] == 2
    assert thread["earlier"] == 0 and thread["anchors"] == []
    assert isinstance(Teams.team_id(thread["team"]["members"]), str)


async def test_relations_cost_a_snapshot_a_few_queries_whatever_the_teams(harness):
    """40 workers in 10 teams, each with parts, a wait and a replacement: the relations are a
    fixed number of queries for the whole snapshot (read once, again when anything changed),
    and their share of it a few milliseconds."""
    runtime = await harness.connect()
    router = harness.router_id
    harness.store.save_settings(harness.store.settings().model_copy(update={"max_workers": 100}))
    for team in range(10):
        _, first = await spawn(runtime, router, f"Team {team} API")
        _, second = await spawn(runtime, router, f"Team {team} UI", works_with=[first])
        _, third = await spawn(runtime, router, f"Team {team} tests", after=[second])
        await spawn(runtime, router, f"Team {team} tests again", replaces=[third])
    statements: list[str] = []
    harness.store.db.set_trace_callback(statements.append)
    try:
        runtime.relations.memo = None
        snapshot = runtime.snapshot()
    finally:
        harness.store.db.set_trace_callback(None)
    assert snapshot["sessions"] and len(runtime.teams.view()) == 10
    read = [s for s in statements if "task_deps" in s and "JOIN" not in s]
    assert len(read) <= 2, "read once (again only after a change), not per worker or team"
    runtime.relations.memo = None
    started = time.perf_counter()
    for _ in range(20):
        runtime.relations.memo = None
        runtime.relations.graph()
        for members in runtime.teams.components():
            runtime.relations.cue(members)
    each = (time.perf_counter() - started) / 20
    assert each < 0.025, f"{each * 1000:.1f} ms"


def test_a_wait_cleared_long_ago_settles(harness):
    """(Times as written: a wait cleared is "just" for JUST_CLEARED seconds.)"""
    waits = edge("after", "t0", "t2", "resolved",
                 resolved=(NOW - timedelta(seconds=30)).isoformat())  # fmt: skip
    assert chip_of(task("t2"), [waits], [], None, {}, NOW)[0].get("just") is True
    waits["resolved_at"] = (NOW - timedelta(minutes=5)).isoformat()
    assert "just" not in chip_of(task("t2"), [waits], [], None, {}, NOW)[0]


async def test_a_compare_is_one_team_its_sides_side_by_side_the_one_accepted_picked(
    tmp_path, codex_installed
):
    """The owner's "do this with Copilot and Codex" (0.1.53's Compare): its workers are one team
    (they never message), its sides one group, pending until the owner accepts one: picked."""
    from test_agents import saved, spawn_on, started

    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        found = await spawn_on(harness, runtime, title="Fix the login test",
                               instructions="Report done: x", agent="codex",
                               compare_with=["copilot"])  # fmt: skip
        made = [worker["session_id"] for worker in found["workers"]]
        tasks = [worker["task_id"] for worker in found["workers"]]
        [team] = [members for members in runtime.teams.components() if made[0] in members]
        assert sorted(team) == sorted(made)
        # Its two workers have one name: said once, with how many.
        [view] = [t for t in runtime.teams.view() if made[0] in t["members"]]
        assert view["name"] == "Fix the login test \u00d72"
        [group] = [g for g in runtime.relations.graph().groups if g["kind"] == "compare"]
        assert sorted(group["tasks"]) == sorted(tasks) and group["state"] == "pending"
        thread = runtime.teams.thread_view(runtime.teams.team_of(made[0]))
        rows = {row["id"]: row for row in thread["tasks"]}
        assert {rows[task]["relation"]["kind"] for task in tasks} == {"compare"}
        assert thread["summary"]["compare"] == 1
        runtime.tasks.move(tasks[1], "done")
        [group] = [g for g in runtime.relations.graph().groups if g["kind"] == "compare"]
        assert (group["state"], group["picked"]) == ("resolved", tasks[1])
    finally:
        await runtime.stop()


async def test_a_task_that_waits_on_another_of_its_request_is_a_chain_not_a_part(harness):
    """Live R2 (r81): "Slow Builder, and Release Note, which must not start before it is done":
    the note waits on the builder (after) and was started beside it (works_with). They are a
    chain: no fan of two parts, Home's line says one waits; the builder hears the note starts
    once it is done, never that it works at the same time, and the note that it starts after."""
    runtime = await harness.connect()
    router = harness.router_id
    builder, build = await spawn(runtime, router, "Slow Builder")
    note, write = await spawn(runtime, router, "Release Note", after=[build], works_with=[build])
    assert json.loads(harness.store.get(f"parts:{write}") or "[]") == []
    assert [g for g in runtime.relations.graph().groups if g["kind"] == "parts"] == []
    [team] = runtime.teams.view()
    assert team["cue"] == {"parts": None, "waiting": 1}
    rows = {row["id"]: row for row in runtime.teams.thread_view(team["id"])["tasks"]}
    assert (rows[write]["relation"]["kind"], rows[write]["relation"]["state"]) == (
        "after",
        "pending",
    )
    told = harness.session(builder).sent[-1]
    assert "starts once you're done" in told and "at the same time" not in told, told
    first = harness.session(note).sent[0] if harness.client.sessions.get(note) else ""
    first = first or harness.store.messages(note)[0]["meta"]["prompt"]
    assert "You start once" in first and "work at the same time" not in first, first


async def test_of_a_requests_parts_only_those_at_work_side_by_side_are_its_fan(harness):
    """API, UI and a check that waits on the UI: the fan is the API and the UI; the check
    follows the UI. Also as an earlier version kept it (the check among the parts)."""
    runtime = await harness.connect()
    router = harness.router_id
    harness.store.save_settings(harness.store.settings().model_copy(update={"max_workers": 100}))
    _, api = await spawn(runtime, router, "Checkout API")
    _, ui = await spawn(runtime, router, "Checkout UI", works_with=[api])
    _, check = await spawn(runtime, router, "Checkout check", after=[ui], works_with=[api, ui])
    [group] = [g for g in runtime.relations.graph().groups if g["kind"] == "parts"]
    assert group["tasks"] == [api, ui]
    harness.store.set(f"parts:{check}", json.dumps([api, ui]))
    runtime.relations.memo = None
    [group] = [g for g in runtime.relations.graph().groups if g["kind"] == "parts"]
    assert group["tasks"] == [api, ui]


async def test_a_wait_moves_to_the_task_done_instead_of_the_one_it_waited_on(harness):
    """A task that waits on another's result waits on the one that does it instead: replaced by
    a new worker's task (replaces), or superseded by a new task of its own worker; never
    blocked on the one that is over."""
    runtime = await harness.connect()
    router = harness.router_id
    harness.store.save_settings(harness.store.settings().model_copy(update={"max_workers": 100}))
    _, build = await spawn(runtime, router, "Slow Builder")
    _, note = await spawn(runtime, router, "Release Note", after=[build])
    _, again = await spawn(runtime, router, "Fast Builder", replaces=[build])
    assert [dep["id"] for dep in runtime.tasks.deps(note)] == [again]
    assert edges(runtime, "after") == {("after", again, note, "pending")}
    # Superseded in its own worker (message_session, task="new", earlier="superseded").
    worker = runtime.tasks.get(again)["session_id"]
    session = harness.session(worker)
    session.emit("user.message", content=session.sent[-1])  # At work on it.
    await settle(runtime)
    await call_tool(runtime, "message_session", router, session_id=worker, task="new",
                    why_new="test", earlier="superseded", title="Faster Builder",
                    text="Build it with the cache on.")  # fmt: skip
    await settle(runtime)
    faster = runtime.tasks.current(worker)["id"]
    assert faster != again and runtime.tasks.get(again)["status"] == "cancelled"
    assert [dep["id"] for dep in runtime.tasks.deps(note)] == [faster]
    assert not runtime.tasks.broken(runtime.tasks.get(note)), "never blocked on the one over"
