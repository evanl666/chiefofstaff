import json

from conftest import call_tool, settle

from chiefofstaff.models import DraftDecision


async def spawn(runtime, router, title, **extra):
    extra.setdefault("instructions", title)
    result = await call_tool(runtime, "spawn_worker", router, title=title, **extra)
    return json.loads(result.text_result_for_llm)


async def test_workers_ask_one_decision_and_the_owner_changes_it_later(harness):
    runtime = await harness.connect()
    api = await spawn(runtime, harness.router_id, "Login API")
    form = await spawn(runtime, harness.router_id, "Login form", why_new="parallel")
    asked = await call_tool(
        runtime,
        "ask_decision",
        api["session_id"],
        question="Do we support SMS login in v1?",
        why="SMS needs a paid provider.",
        options=[
            {"choice": "Email only", "effect": "Ships on time"},
            {"choice": "Email and SMS", "effect": "Two more days"},
        ],
        recommendation="Email only: on time",
    )
    card = harness.store.drafts()[0]
    assert card["kind"] == "decision" and card["id"] in asked.text_result_for_llm
    assert runtime.tasks.get(api["task_id"])["status"] == "waiting"

    again = await call_tool(
        runtime, "ask_decision", form["session_id"], question="Is there SMS login?"
    )
    assert again.result_type == "failure" and card["id"] in again.text_result_for_llm
    await call_tool(runtime, "ask_decision", form["session_id"], question="x", same_as=card["id"])
    card = harness.store.draft(card["id"])
    assert [a["title"] for a in card["target"]["askers"]] == ["Login API", "Login form"]
    assert len(harness.store.drafts()) == 1, "one card for one decision"

    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Email only"))
    await settle(runtime)
    [decision] = runtime.decisions.active()
    assert decision["text"] == "Do we support SMS login in v1? → Email only"
    assert sorted(decision["tasks"]) == sorted([api["task_id"], form["task_id"]])
    for worker in (api, form):
        sent = harness.session(worker["session_id"]).sent[-1]
        assert f'type="decision" decision="{decision["id"]}"' in sent
        assert "The owner decided: Email only" in sent
        assert runtime.tasks.get(worker["task_id"])["status"] == "running"

    await call_tool(runtime, "report", api["session_id"], status="done", summary="Email login.")
    await runtime.task_actions.collect_evidence(api["task_id"])
    changed = await call_tool(
        runtime,
        "record_decision",
        harness.router_id,
        decision="Login v1: email and SMS",
        replaces=[decision["id"]],
    )
    text = changed.text_result_for_llm
    assert "affects 2 task(s); 1 checked result(s) are out of date" in text
    [new] = runtime.decisions.active()
    api_task = runtime.task_actions.view(runtime.tasks.get(api["task_id"]))
    assert api_task["status"] == "running", "its finished result reopens"
    assert api_task["receipts"]["stale"].startswith("Out of date: the decision")
    for worker in (api, form):
        sent = harness.session(worker["session_id"]).sent[-1]
        assert 'type="decision-changed"' in sent and "Now: Login v1: email and SMS" in sent
    [impact] = [d for d in harness.store.drafts() if d["kind"] == "change"]
    assert [(t["title"], t["was"]) for t in impact["target"]["tasks"]] == [
        ("Login API", "review"),
        ("Login form", "running"),
    ]

    await call_tool(
        runtime, "ack_decision", api["session_id"], decision=new["id"], plan="Add SMS back"
    )
    impact = harness.store.draft(impact["id"])
    assert impact["status"] == "pending" and impact["target"]["tasks"][0]["ack"] == "Add SMS back"
    await call_tool(
        runtime, "ack_decision", form["session_id"], decision=new["id"], plan="Add a phone field"
    )
    assert harness.store.draft(impact["id"])["status"] == "answered", "all confirmed"


async def test_a_closed_worker_stops_waiting_for_a_decision(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Migrate")
    await call_tool(runtime, "ask_decision", worker["session_id"], question="Add an index?")
    card = harness.store.drafts()[0]
    await runtime.workers.close(worker["session_id"])
    assert harness.store.draft(card["id"])["status"] == "expired"
