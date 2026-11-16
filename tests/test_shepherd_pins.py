"""Pins for Prepared for you (shepherd.py): every path it touches behaves exactly as today while
it is off, and while it is on with no PR in its scope. Each pin runs the same scenario with the
setting off and on, and compares what a session is given, what the owner sees and what is
stored, apart from what the feature declares (its settings, its tables)."""

import json

from conftest import Harness, settle

from chiefofstaff.models import Settings
from chiefofstaff.roles import WORKER

PR = "acme/web#7"
THREAD = f"github:{PR}"


async def scenario(tmp_path, on: bool) -> dict:
    """A worker following another's PR (not the owner's: never in scope), its news, a Stop,
    a merge card, an accept; what each path gives back."""
    harness = Harness(tmp_path / ("on" if on else "off") / "data", Settings(proactive=on))
    runtime = await harness.connect()
    if on:
        runtime.shepherd.control.ensure()
    store = runtime.store
    session_id = "aaaaaaaa-0000-4000-8000-000000000001"
    store.create_session(session_id, WORKER, "Review web#7", "idle", cwd=str(tmp_path))
    task = runtime.tasks.create(session_id, "Review web#7", "Review it", status="running")
    runtime.follow(session_id, [THREAD], task_id=task["id"], asked=[THREAD])
    row = store.session(session_id)
    found: dict = {
        "options": sorted(runtime.options(row)),
        "yolo": runtime.yolo(row),
        "tools": sorted(tool.name for tool in runtime.options(row)["tools"]),
        "router_tools": sorted(
            json.dumps(tool.parameters, sort_keys=True)
            for tool in runtime.options(store.session(runtime.router_id()))["tools"]
            if tool.name in {"spawn_worker", "message_session"}
        ),
    }
    delivered = await runtime.on_thread(
        "github", THREAD, ["github:ci:acme/web#7@abc:failed-test"], "CI on abc: failed: test", None
    )
    await settle(runtime)
    found["delivered"] = delivered
    found["sent"] = [m["kind"] for m in store.messages(session_id)]
    runtime.tasks.move(task["id"], "review", result="Reviewed.")
    view = runtime.task_actions.view(runtime.tasks.get(task["id"]))
    found["view"] = sorted(view)
    found["attention"] = runtime.task_actions.attention(runtime.tasks.get(task["id"]))
    runtime.task_actions.accept(task["id"])
    found["accepted"] = runtime.tasks.get(task["id"])["status"]
    found["claim"] = await runtime.workers.claim_slot(session_id)
    found["snapshot"] = sorted(key for key in runtime.snapshot() if key != "shepherd")
    found["tables"] = sorted(
        row["name"]
        for row in store.all("SELECT name FROM sqlite_master WHERE type='table'")
        if row["name"]
        not in {"pr_claims", "shepherd_steps", "actions", "ledger_events", "verdicts"}
    )
    return found


async def test_on_with_nothing_in_scope_is_today(tmp_path):
    off = await scenario(tmp_path, on=False)
    on = await scenario(tmp_path, on=True)
    router_off, router_on = off.pop("router_tools"), on.pop("router_tools")
    assert on == off
    # The Router's two tools take a PR to keep moving while on: the one difference declared.
    assert router_off != router_on
    assert all('"shepherd"' not in schema for schema in router_off)


async def test_off_is_today_on_an_owners_pr_too(tmp_path):
    """Off, the owner's own PR with failing CI reaches its worker as news, as today."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    store = runtime.store
    session_id = "aaaaaaaa-0000-4000-8000-000000000002"
    store.create_session(session_id, WORKER, "Fix web#7", "idle", cwd=str(tmp_path))
    task = runtime.tasks.create(session_id, "Fix web#7", "Fix it", status="running")
    runtime.follow(session_id, [THREAD], task_id=task["id"], asked=[THREAD])
    runtime.tasks.move(task["id"], "done", result="Fixed: acme/web#7.")
    assert await runtime.on_thread(
        "github", THREAD, ["github:ci:acme/web#7@abc:failed-test"], "CI on abc: failed: test", None
    )
    await settle(runtime)
    assert any(m["kind"] == "inbound" for m in store.messages(session_id))
    assert not runtime.shepherd.control.exists()
