import asyncio
import json
import sys

import pytest
from conftest import Harness, call_tool, env, settle
from test_round13_ui import BOARD
from test_ui_text import run_js

from chiefofstaff.models import DraftDecision
from chiefofstaff.scripted import CallTool, Say, ScriptedBackend, Shell


async def spawn(runtime, router, title, **extra):
    extra.setdefault("instructions", title)
    result = await call_tool(runtime, "spawn_worker", router, title=title, **extra)
    return json.loads(result.text_result_for_llm)


def one_request(harness, *workers, request="9001"):
    """Workers' tasks made for one owner request (a Router turn's: Tasks.create stamps them)."""
    for worker in workers:
        harness.store.set(f"req:{worker['task_id']}", request)


async def test_workers_ask_one_decision_and_the_owner_changes_it_later(harness):
    runtime = await harness.connect()
    api = await spawn(runtime, harness.router_id, "Login API")
    form = await spawn(runtime, harness.router_id, "Login form", why_new="parallel")
    one_request(harness, api, form)
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

    # The same choice in other words, in the same request: joined by Chief of Staff (E159).
    again = await call_tool(
        runtime, "ask_decision", form["session_id"], question="Is there SMS login?"
    )
    assert again.result_type == "success" and card["id"] in again.text_result_for_llm
    assert "“Login API” asked the owner the same" in again.text_result_for_llm
    assert runtime.tasks.get(form["task_id"])["status"] == "waiting"
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
    # All confirmed, and their work changes (E123): the card stays for the owner to see.
    assert harness.store.draft(impact["id"])["status"] == "pending", "work changes"


async def test_a_closed_worker_stops_waiting_for_a_decision(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Migrate")
    await call_tool(runtime, "ask_decision", worker["session_id"], question="Add an index?")
    card = harness.store.drafts()[0]
    await runtime.workers.close(worker["session_id"])
    assert harness.store.draft(card["id"])["status"] == "expired"


async def test_a_decision_answered_in_chat_or_whose_task_ended_leaves_needs_you(harness):
    """Bug bash 3 (L4/G17): the card stayed after the owner answered in the worker's chat (and
    its task said it waited for an answer); a cancelled task's card stayed for good."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Format money")
    await call_tool(runtime, "ask_decision", worker["session_id"],
                    question="Symbol before or after?")  # fmt: skip
    [card] = harness.store.drafts()
    await runtime.owner_send(worker["session_id"], "Before by default; after is opt-in.")
    assert harness.store.draft(card["id"])["status"] == "answered"
    assert runtime.tasks.get(worker["task_id"])["status"] == "running", "it has its answer"
    sent = harness.session(worker["session_id"]).sent[-1]
    assert "This answers your decision card: Symbol before or after?" in sent

    other = await spawn(runtime, harness.router_id, "Add an index", why_new="separate")
    await call_tool(runtime, "ask_decision", other["session_id"], question="Add an index now?",
                    distinct=True)  # fmt: skip
    [card] = harness.store.drafts()
    await runtime.task_actions.cancel(other["task_id"])
    assert harness.store.draft(card["id"])["status"] == "expired"


async def test_a_question_back_in_chat_does_not_answer_a_decision_card(harness):
    """Bug bash 4 (R-s1): "before I pick: what does B change?" closed the decision card."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Format money")
    await call_tool(runtime, "ask_decision", worker["session_id"],
                    question="Symbol before or after?")  # fmt: skip
    [card] = harness.store.drafts()
    await runtime.owner_send(worker["session_id"], "Before I pick: what does “after” break?")
    assert harness.store.draft(card["id"])["status"] == "pending", "the card stays"
    assert runtime.tasks.get(worker["task_id"])["status"] == "waiting"
    sent = harness.session(worker["session_id"]).sent[-1]
    assert "what does “after” break?" in sent and "This answers your decision card" not in sent
    assert "stays open for their decision" in sent
    await runtime.owner_send(worker["session_id"], "OK, before then.")
    assert harness.store.draft(card["id"])["status"] == "answered"


async def test_claims_on_an_earlier_task_and_a_bad_decision_id(harness):
    runtime = await harness.connect()
    first = await spawn(runtime, harness.router_id, "API", done_when=["`make test` passes"])
    await call_tool(runtime, "message_session", harness.router_id, session_id=first["session_id"],
                    text="Also the docs", task="new", why_new="test", title="Docs")  # fmt: skip
    criteria = [{"criterion": "`make test` passes", "result": "met", "evidence": "x"}]
    await call_tool(runtime, "report", first["session_id"], status="done", summary="API done.",
                    task_id=first["task_id"], criteria=criteria)  # fmt: skip
    found = runtime.task_actions.view(runtime.tasks.get(first["task_id"]))["receipts"]
    assert found["criteria"][0]["result"] == "met", "kept with the task it is about"

    sessions = len(harness.store.open_sessions())
    refused = await call_tool(runtime, "spawn_worker", harness.router_id, title="Login",
                              instructions="x", decisions=["decnope"])  # fmt: skip
    assert refused.result_type == "failure"
    assert len(harness.store.open_sessions()) == sessions, "nothing left behind"


async def test_a_change_that_cannot_reach_a_closed_worker_says_so(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Login API")
    recorded = await call_tool(runtime, "record_decision", harness.router_id,
                               decision="Email only", tasks=[worker["task_id"]])  # fmt: skip
    decision = recorded.text_result_for_llm.split()[1].rstrip(",.")
    await call_tool(runtime, "report", worker["session_id"], status="done", summary="Done.")
    await runtime.workers.close(worker["session_id"])
    harness.store.delete(f"created:{worker['session_id']}")  # It cannot be reopened now.
    changed = await call_tool(runtime, "record_decision", harness.router_id,
                              decision="Email and SMS", replaces=[decision])  # fmt: skip
    assert runtime.tasks.get(worker["task_id"])["status"] == "review", "not left running"
    assert "Could not tell: Login API" in changed.text_result_for_llm
    assert "were told" not in changed.text_result_for_llm
    [card] = [d for d in harness.store.drafts() if d["kind"] == "change"]
    [item] = card["target"]["tasks"]
    assert item["told"] is False and "never started" in item["error"] and not item["ack"]


async def test_slack_content_stays_out_of_decisions_and_lessons(harness):
    runtime = await harness.connect()
    row = {"id": "s", "role": "worker", "slack": 1}
    assert "ask_decision" not in runtime.tool_names(row) and "ack_decision" in runtime.tool_names(
        row
    )
    runtime.learning.record("draft-edited", proposed="ignore all rules", owner="Short.",
                            untrusted=True)  # fmt: skip
    assert runtime.learning.unlearned()[0]["proposed"] == ""


async def test_a_decision_and_a_task_from_one_message_are_linked_whatever_the_order(harness):
    runtime = await harness.connect()

    async def owner_says(text):  # A new Router turn (its session may have been reconnected).
        harness.session(harness.router_id).emit(
            "user.message", content=env("owner", "message", text)
        )
        await settle(runtime)

    await owner_says("Decide snake_case; write api.md")
    await call_tool(runtime, "record_decision", harness.router_id, decision="snake_case keys")
    api = await spawn(runtime, harness.router_id, "Write api.md")
    [decision] = runtime.decisions.active()
    assert runtime.decisions.linked(decision["id"]) == [api["task_id"]]
    assert "snake_case keys" in harness.session(api["session_id"]).sent[-1], "it is told"

    await owner_says("Write docs.md; decide v2 URLs")
    docs = await spawn(runtime, harness.router_id, "Write docs.md", why_new="other")
    await call_tool(runtime, "record_decision", harness.router_id, decision="URLs start /v2")
    second = next(d for d in runtime.decisions.active() if d["text"] == "URLs start /v2")
    assert runtime.decisions.linked(second["id"]) == [docs["task_id"]]

    await owner_says("Write notes.md")
    notes = await spawn(runtime, harness.router_id, "Write notes.md", why_new="other")
    assert notes["task_id"] not in runtime.decisions.linked(second["id"]), "a later message"


async def test_a_decision_given_with_new_work_is_linked_to_a_finished_task_only_on_purpose(
    harness,
):
    """Live e2e r81: "Decision: snake_case keys. Record that, then have a worker write api.md":
    the Router linked the decision to the finished, unrelated VERSION task too, which the change
    of it then reopened. Naming a finished task is asked once; the new task gets it by itself,
    and the Router is told which tasks it is linked to."""
    runtime = await harness.connect()
    router = harness.router_id
    version = await spawn(runtime, router, "Add VERSION 1.4.2")
    await call_tool(runtime, "report", version["session_id"], status="done", summary="Added.")
    harness.session(router).emit("user.message", content=env("owner", "message", "Decide; go"))
    await settle(runtime)
    asked = await call_tool(runtime, "record_decision", router, decision="snake_case keys",
                            tasks=[version["task_id"]])  # fmt: skip
    assert asked.result_type == "failure" and not runtime.decisions.active()
    said = asked.text_result_for_llm
    assert version["task_id"] in said and "finished" in said, said
    recorded = await call_tool(runtime, "record_decision", router, decision="snake_case keys")
    api = await spawn(runtime, router, "Write api.md", why_new="other")
    [decision] = runtime.decisions.active()
    assert runtime.decisions.linked(decision["id"]) == [api["task_id"]]
    assert "linked to 0 task(s)." in recorded.text_result_for_llm
    again = await call_tool(runtime, "record_decision", router, decision="URLs start /v2",
                            tasks=[version["task_id"]])  # fmt: skip
    assert again.result_type == "failure"
    linked = await call_tool(runtime, "record_decision", router, decision="URLs start /v2",
                             tasks=[version["task_id"]])  # fmt: skip
    assert f"{version['task_id']} “Add VERSION 1.4.2”" in linked.text_result_for_llm, "on purpose"


async def test_a_changed_decision_that_changes_nothing_gives_the_result_back(harness):
    runtime = await harness.connect()
    api = await spawn(runtime, harness.router_id, "Login API")
    await call_tool(runtime, "ask_decision", api["session_id"], question="SMS login in v1?")
    [card] = harness.store.drafts()
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Email only"))
    await settle(runtime)
    [first] = runtime.decisions.active()
    await call_tool(runtime, "report", api["session_id"], status="done", summary="Email login.")
    await call_tool(
        runtime, "record_decision", harness.router_id, decision="Login v1: email, no SSO",
        replaces=[first["id"]],
    )  # fmt: skip
    assert runtime.tasks.get(api["task_id"])["status"] == "running", "reopened to check"
    [new] = runtime.decisions.active()
    await call_tool(
        runtime, "ack_decision", api["session_id"], decision=new["id"],
        plan="Nothing to change: there was no SSO", changes=False,
    )  # fmt: skip
    task = runtime.tasks.get(api["task_id"])
    assert task["status"] == "review" and task["result"] == "Email login."
    assert not (runtime.task_actions.receipts(task) or {}).get("stale")


async def test_a_decision_recorded_while_the_task_relying_on_it_starts_is_linked(
    harness, monkeypatch
):
    """Live e2e (f1): the Router recorded the owner's decision and started the task relying on
    it in one step (parallel calls): recorded while the task's folder was being made, the
    decision was linked to no task, so a change of it reached no worker."""
    runtime = await harness.connect()
    router = harness.router_id
    making, go = asyncio.Event(), asyncio.Event()
    prepare = runtime.workspaces.prepare_directory

    async def slow(*args, **kwargs):
        making.set()
        await go.wait()
        return await prepare(*args, **kwargs)

    monkeypatch.setattr(runtime.workspaces, "prepare_directory", slow)
    spawning = asyncio.ensure_future(
        call_tool(runtime, "spawn_worker", router, title="Write api.md",
                  instructions="Write api.md with a GET /users example.", why_new="t")
    )  # fmt: skip
    await making.wait()
    said = await call_tool(runtime, "record_decision", router, decision="The API uses snake_case")
    go.set()
    task_id = json.loads((await spawning).text_result_for_llm)["task_id"]
    linked = harness.store.all("SELECT decision_id FROM task_decisions WHERE task_id=?", (task_id,))
    assert linked, said.text_result_for_llm


async def test_a_decision_asked_again_on_its_task_gets_its_answer_not_a_card(harness):
    """Owner report (0.1.53) item 1: answered, the same question asked again on its task (with
    seen_precedents=true) was a new card, and the task waited again. Asked again, it gets the
    card it waits on; answered (on the card or in its chat), the owner's answer."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Night Harbor")
    sid, task = worker["session_id"], worker["task_id"]
    question = "夜港：金币要留到下一晚吗？"  # noqa: RUF001
    await call_tool(runtime, "ask_decision", sid, question=question)
    [card] = harness.store.drafts()
    waits = await call_tool(runtime, "ask_decision", sid, question=question, seen_precedents=True)
    assert card["id"] in waits.text_result_for_llm and len(harness.store.drafts()) == 1
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="留到下一晚"))
    await settle(runtime)
    [decision] = runtime.decisions.active()
    retry = await call_tool(runtime, "ask_decision", sid, question=question[:-1] + "?",
                            seen_precedents=True)  # fmt: skip
    said = retry.text_result_for_llm
    assert retry.result_type == "success" and "留到下一晚" in said and decision["id"] in said
    assert not harness.store.drafts() and runtime.tasks.get(task)["status"] == "running"

    # Another question on that task is asked as such (owner report 0.1.58: "already decided").
    lighthouse = "Light the lighthouse at dusk?"
    shown = await call_tool(runtime, "ask_decision", sid, question=lighthouse)
    assert "Asked the owner" in shown.text_result_for_llm and len(harness.store.drafts()) == 1
    await runtime.owner_send(sid, "Yes, at dusk.")
    again = await call_tool(runtime, "ask_decision", sid, question=lighthouse,
                            seen_precedents=True)  # fmt: skip
    assert "Yes, at dusk." in again.text_result_for_llm and not harness.store.drafts()


async def test_new_evidence_reconsiders_a_decision_and_one_stays_in_force(harness):
    """Item 1's field case: the second question was a genuine revision (simulations had shown
    the first choice wrong), and both answers stayed in force. A reconsideration names the
    decision it revisits and why: its card says both, and its answer replaces that decision for
    every task that relied on it."""
    runtime = await harness.connect()
    harbor = await spawn(runtime, harness.router_id, "Night Harbor")
    town = await spawn(runtime, harness.router_id, "Sly Town", why_new="separate")
    sid = harbor["session_id"]
    await call_tool(runtime, "ask_decision", sid, question="Carry the gold over to the next night?")
    [card] = harness.store.drafts()
    await call_tool(runtime, "ask_decision", town["session_id"], question="x", same_as=card["id"])
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Carry it over"))
    await settle(runtime)
    [old] = runtime.decisions.active()
    revised = "Carry only half of the gold over to the next night?"
    listed = await call_tool(runtime, "ask_decision", sid, question=revised)
    assert listed.result_type == "success" and old["id"] in listed.text_result_for_llm
    assert "replaces=" in listed.text_result_for_llm and not harness.store.drafts()
    unsaid = await call_tool(runtime, "ask_decision", sid, question=revised, replaces=old["id"])
    assert unsaid.result_type == "failure" and not harness.store.drafts(), "it says why"
    why = "Simulations: with all of it carried over, every night after the third is unwinnable."
    await call_tool(runtime, "ask_decision", sid, question=revised, replaces=old["id"], why=why)
    [card] = harness.store.drafts()
    assert card["target"]["previous"] == old["text"] and card["target"]["why"] == why
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Half"))
    await settle(runtime)
    [new] = runtime.decisions.active()
    assert new["text"] == f"{revised} → Half"
    assert runtime.decisions.get(old["id"])["replaced_by"] == new["id"]
    assert sorted(new["tasks"]) == sorted([harbor["task_id"], town["task_id"]])
    assert "The owner decided: Half" in harness.session(sid).sent[-1]
    assert not any('type="decision-changed"' in s for s in harness.session(sid).sent), (
        "it asked: the answer is all it needs"
    )
    assert 'type="decision-changed"' in harness.session(town["session_id"]).sent[-1]


async def test_a_reconsideration_decided_meanwhile_closes_and_its_task_goes_on(harness):
    """The owner changed the decision another way (told the Router) while a reconsideration of
    it waited: its card has nothing left to decide, and its task goes on under the new one."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Night Harbor")
    sid = worker["session_id"]
    await call_tool(runtime, "ask_decision", sid, question="Carry the gold over?")
    [card] = harness.store.drafts()
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Yes"))
    [old] = runtime.decisions.active()
    await call_tool(runtime, "ask_decision", sid, question="Carry half of it over?",
                    replaces=old["id"], why="Late nights are unwinnable.")  # fmt: skip
    [card] = harness.store.drafts()
    assert runtime.tasks.get(worker["task_id"])["status"] == "waiting"
    await call_tool(runtime, "record_decision", harness.router_id, decision="Gold: carry half",
                    replaces=[old["id"]])  # fmt: skip
    assert harness.store.draft(card["id"])["status"] != "pending"
    assert runtime.tasks.get(worker["task_id"])["status"] == "running"
    [new] = runtime.decisions.active()
    assert new["text"] == "Gold: carry half" and worker["task_id"] in new["tasks"]
    assert "Now: Gold: carry half" in harness.session(sid).sent[-1]


async def test_two_reconsiderations_of_a_decision_are_one_card_and_one_answer(harness):
    """Two workers that found the same decision wrong ask once; answered in one's chat, the
    other's wait ends too, under the one decision now in force."""
    runtime = await harness.connect()
    harbor = await spawn(runtime, harness.router_id, "Night Harbor")
    town = await spawn(runtime, harness.router_id, "Sly Town", why_new="separate")
    await call_tool(runtime, "ask_decision", harbor["session_id"], question="Carry the gold over?")
    [card] = harness.store.drafts()
    await call_tool(runtime, "ask_decision", town["session_id"], question="x", same_as=card["id"])
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Yes"))
    [old] = runtime.decisions.active()
    for worker in (harbor, town):
        await call_tool(runtime, "ask_decision", worker["session_id"], question="Carry half?",
                        replaces=old["id"], why="Late nights are unwinnable.")  # fmt: skip
    [card] = harness.store.drafts()
    assert [a["title"] for a in card["target"]["askers"]] == ["Night Harbor", "Sly Town"]
    await runtime.owner_send(harbor["session_id"], "Half, then.")
    assert harness.store.draft(card["id"])["status"] != "pending"
    [new] = runtime.decisions.active()
    assert new["text"] == "Carry half? → Half, then." and new["replaces"] == f'["{old["id"]}"]'
    assert runtime.tasks.get(town["task_id"])["status"] == "running"
    assert "Now: Carry half? → Half, then." in harness.session(town["session_id"]).sent[-1]


async def test_the_owners_answer_reaches_a_worker_still_in_the_turn_that_asked(harness):
    """Owner report (0.1.57, R1): a worker asked, went on in the same turn (its simulations
    ran), and the owner's answer waited behind that turn: 43 minutes later it read it, after a
    second card built without it. Steered, it reaches that turn; a worker whose turn is over
    gets it as its next turn, once."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Tie breaker")
    sid = worker["session_id"]
    session = harness.session(sid)
    await call_tool(runtime, "ask_decision", sid, question="Add rule A or rule B?",
                    options=[{"choice": "Rule A"}, {"choice": "Rule B"}])  # fmt: skip
    [card] = harness.store.drafts()
    assert harness.store.session(sid)["status"] == "waiting" and runtime.states[sid].busy
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Rule A"))
    await settle(runtime)
    [decision] = runtime.decisions.active()
    assert session.modes[-1] == "immediate", "into the turn it asked in"
    assert "The owner decided: Rule A" in session.sent[-1]
    told = [m for m in harness.store.messages(sid) if m["kind"] == "owner"][-1]
    assert told["meta"]["answers"] == card["id"] and told["meta"]["decision"] == decision["id"]
    session.emit("session.idle")
    await settle(runtime)
    assert sum("The owner decided: Rule A" in s for s in session.sent) == 1, "not sent again"

    other = await spawn(runtime, harness.router_id, "Tie log", why_new="separate")
    waiting = harness.session(other["session_id"])
    await call_tool(runtime, "ask_decision", other["session_id"], question="Log the ties?")
    [card] = harness.store.drafts()
    waiting.emit("session.idle")  # It ended its turn to wait.
    await settle(runtime)
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Yes"))
    await settle(runtime)
    assert waiting.modes[-1] == "enqueue", "its turn was over: the answer starts the next"
    assert sum("The owner decided: Yes" in s for s in waiting.sent) == 1


def r1_brain(gate: asyncio.Event):
    """The owner's case R1, in made-up words: a worker asks rule A or B and keeps at it in the
    same turn (a simulation it started before the answer); what that simulation found, it asks
    again unless the owner's answer reached it first."""

    async def brain(session, prompt):
        if 'type="task"' not in prompt or "Tie breaker" not in prompt:
            yield Say("Noted.")
            return
        yield CallTool("ask_decision", {
            "question": "Players always tie. Add rule A, rule B, or change nothing?",
            "options": [{"choice": "Rule A"}, {"choice": "Rule B"}, {"choice": "No change"}],
            "recommendation": "Rule A: fewer ties"})  # fmt: skip
        await asyncio.wait_for(gate.wait(), 10)  # The owner answers while it simulates.
        yield Shell("python simulate.py --rules A,B", output="A and B: ties 61%", seconds=0.05)
        heard = [text for who, text in session.history if who == "user" and "Rule A" in text]
        if not heard:
            options = [{"choice": "C+D"}, {"choice": "C only"}, {"choice": "None"}]
            yield CallTool("ask_decision", {
                "question": "(Update) A and B both fail in simulation: C+D, C only, or none?",
                "why": "The two options from the last question failed.", "seen_precedents": True,
                "options": options})  # fmt: skip
            return
        yield CallTool("report", {"status": "done", "summary": "Rule A in; ties at 12%."})

    return brain


async def test_r1_the_answer_given_while_the_asker_simulates_reaches_it_and_no_card_follows(
    tmp_path,
):
    gate = asyncio.Event()
    harness = Harness(tmp_path / "data", client=ScriptedBackend(r1_brain(gate)))
    runtime = await harness.connect()
    try:
        worker = await spawn(runtime, harness.router_id, "Tie breaker")
        sid = worker["session_id"]
        for _ in range(500):
            if harness.store.drafts():
                break
            await asyncio.sleep(0.01)
        [card] = harness.store.drafts()
        await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Rule A"))
        gate.set()
        for _ in range(500):
            if runtime.tasks.get(worker["task_id"])["status"] in {"review", "done"}:
                break
            await asyncio.sleep(0.01)
        cards = harness.store.all("SELECT id FROM drafts WHERE kind='decision'")
        assert len(cards) == 1, "no second card built on what it found before the answer"
        [decision] = runtime.decisions.active()
        assert decision["text"].endswith("→ Rule A")
        assert runtime.tasks.get(worker["task_id"])["result"] == "Rule A in; ties at 12%."
        received = [m for m in harness.store.messages(sid) if m["kind"] == "owner"]
        assert len(received) == 1 and received[0]["meta"]["answers"] == card["id"]
    finally:
        await runtime.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-ins run their commands in sh")
@pytest.mark.parametrize("agent", ["codex", "claude"])
async def test_each_agent_takes_the_answer_into_the_turn_that_asked(
    tmp_path, monkeypatch, request, agent
):
    """Steering parity: Codex's (turn/steer) and Claude's (a message while a tool runs) take
    the owner's answer within the turn that asked, as Copilot's does, once."""
    from test_agents import saved, spawn_on

    from chiefofstaff import backend_claude

    if agent == "codex":
        request.getfixturevalue("codex_installed")
    else:
        request.getfixturevalue("claude_on_path")
        passed = {"ran": True, "acting": "", "ask_first": "", "steering": True, "usage": True}

        async def check(self, folder):
            return dict(passed)

        monkeypatch.setattr(backend_claude.ClaudeAgent, "check", check)
        monkeypatch.setattr(backend_claude, "LISTED", {})
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    taken: list[tuple[str, str]] = []
    took = runtime.events.user_message

    def spy(row, event):
        content = getattr(event.data, "content", "") or ""
        delivery = getattr(getattr(event.data, "delivery", None), "value", "")
        taken.append((content, delivery))
        took(row, event)

    monkeypatch.setitem(runtime.events.handlers, "user.message", spy)
    try:
        await saved(runtime, {agent: {"enabled": True}})
        asked = json.dumps({"question": "Rule A or rule B?"})
        made = await spawn_on(
            harness, runtime, title="Tie breaker", agent=agent,
            instructions=f"Call the ask_decision tool with arguments {asked}\n"
            "Run the shell command `sleep 4`. Then reply with just: simulated",
        )  # fmt: skip
        sid = made["session_id"]
        for _ in range(600):
            if harness.store.drafts() and runtime.states[sid].steps:
                break
            await asyncio.sleep(0.05)
        [card] = harness.store.drafts()
        assert runtime.states[sid].busy, "its turn goes on (the command runs)"
        await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Rule A"))
        for _ in range(600):
            if not runtime.states[sid].busy:
                break
            await asyncio.sleep(0.05)
        answers = [d for text, d in taken if "The owner decided: Rule A" in text]
        assert answers == ["steering"], f"taken into its turn, once: {taken}"
    finally:
        await runtime.stop()


async def asked_in_turn(runtime, harness, title, question):
    """A worker at work (its conversation shows its task) that asks a decision card and goes
    on in that turn."""
    worker = await spawn(runtime, harness.router_id, title)
    session = harness.session(worker["session_id"])
    session.emit("user.message", content=session.sent[-1])
    await settle(runtime)
    await call_tool(runtime, "ask_decision", worker["session_id"], question=question)
    [card] = harness.store.drafts()
    return worker, session, card


async def test_a_worker_that_asks_again_before_reading_the_answer_gets_it_not_a_card(harness):
    """R1's second card: asked from what it found before it read the owner's answer, it was a
    card of its own, both answers in force. Asked while the answer is unread, it gets the answer
    (once); the owner sees that in Handled."""
    runtime = await harness.connect()
    worker, session, card = await asked_in_turn(
        runtime, harness, "Tie breaker", "Add rule A, rule B, or change nothing?"
    )
    sid = worker["session_id"]
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Rule A"))
    await settle(runtime)
    [decision] = runtime.decisions.active()
    again = await call_tool(runtime, "ask_decision", sid, seen_precedents=True,
                            question="A and B both fail in simulation: C+D, C only, or none?",
                            why="The two options from the last question failed.")  # fmt: skip
    said = again.text_result_for_llm
    assert "already answered" in said and "Rule A" in said and decision["id"] in said, said
    assert f'replaces="{decision["id"]}"' in said
    assert not harness.store.drafts() and len(runtime.decisions.active()) == 1
    [handled] = runtime.handled.view()
    assert handled["kind"] == "held-ask" and "before reading your" in handled["text"]

    session.emit("user.message", content=session.sent[-1])  # It reads the answer now.
    await settle(runtime)
    assert not runtime.delivery.unread(sid, worker["task_id"])
    why = "Simulation: with rule A players still tie 61% of the time."
    await call_tool(runtime, "ask_decision", sid, question="C+D, C only, or none?",
                    replaces=decision["id"], why=why)  # fmt: skip
    [card] = harness.store.drafts()
    assert card["target"]["replaces"] == decision["id"], "asked as what it is: a reconsideration"


async def test_e156_a_question_or_blocked_report_waits_for_what_it_has_not_read(harness):
    """E156: a question (or blocked) report while the owner's answer to it, or the Router's newer
    word on its task, was unread became a card all the same. The answer is given instead; any
    other instruction holds it back once; news that asks nothing holds nothing."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Port picker")
    sid, task = worker["session_id"], worker["task_id"]
    session = harness.session(sid)
    session.emit("user.message", content=session.sent[-1])
    await settle(runtime)
    await call_tool(runtime, "report", sid, status="question", summary="Port 8080 or 9090?")
    [question] = runtime.asks.session_questions(sid)
    await runtime.task_actions.answer_question(question["id"], "9090")
    await settle(runtime)
    assert session.modes[-1] == "immediate", "into the turn that asked"
    blocked = await call_tool(runtime, "report", sid, status="blocked",
                              summary="Which port: 8080 or 9090?")  # fmt: skip
    said = blocked.text_result_for_llm
    assert blocked.result_type == "failure" and "already answered" in said and "9090" in said
    assert not runtime.asks.session_questions(sid), "no card built without it"
    assert runtime.tasks.get(task)["status"] == "running"

    session.emit("user.message", content=session.sent[-1])
    await settle(runtime)
    await call_tool(runtime, "message_session", harness.router_id, session_id=sid,
                    task="note", text="Use the staging host.")  # fmt: skip
    held = await call_tool(runtime, "report", sid, status="question", summary="Which host?")
    assert held.result_type == "failure" and "Use the staging host." in held.text_result_for_llm
    assert not runtime.asks.session_questions(sid)
    await call_tool(runtime, "report", sid, status="question", summary="Which host?")
    assert len(runtime.asks.session_questions(sid)) == 1, "held once, then asked"
    told = [m for m in harness.store.messages(harness.router_id) if m["kind"] == "inbound"]
    assert "asked before its worker read a newer message" in told[-1]["meta"]["prompt"]


async def test_a_question_card_asked_before_the_answer_was_read_gets_the_answer(harness):
    """Copilot's ask_user (a question card) asked while the owner's answer to its decision card
    is unread: the answer, no card."""
    runtime = await harness.connect()
    worker, _session, card = await asked_in_turn(runtime, harness, "Tie breaker", "Rule A or B?")
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Rule A"))
    await settle(runtime)
    answer = await asyncio.wait_for(
        runtime.asks.on_user_input(
            {"question": "Rule A or rule B?", "choices": ["A", "B"]},
            {"sessionId": runtime.copilot_id(worker["session_id"])},
        ),
        5,
    )
    assert "already answered" in answer["answer"] and "Rule A" in answer["answer"]
    assert not harness.store.drafts()


async def test_e151_the_router_hears_what_the_owner_answered(harness):
    """E151 (interim): the owner answered "which k" on one worker's question card and on another's
    question under Needs you; the Router only heard that a question closed, and the decision in
    force still said the old value. It hears what they said, and that it may record it."""
    runtime = await harness.connect()
    router = harness.router_id

    def told() -> str:
        notes = [
            m["meta"]["prompt"]
            for m in harness.store.messages(router)
            if 'type="question-closed"' in m["meta"].get("prompt", "")
        ]
        return notes[-1]

    serve = await spawn(runtime, router, "Serve the model")
    await call_tool(runtime, "report", serve["session_id"], status="question",
                    summary="Serve k=1.11 or k=1.12?")  # fmt: skip
    [question] = runtime.asks.session_questions(serve["session_id"])
    await runtime.task_actions.answer_question(question["id"], "k=1.11")
    assert "The owner answered “Serve k=1.11 or k=1.12?” (Serve the model" in told()
    assert ": k=1.11." in told() and "record_decision" in told()

    tune = await spawn(runtime, router, "Tune the model", why_new="separate")
    asking = asyncio.ensure_future(runtime.asks.on_user_input(
        {"question": "Which k for the tuning run?", "choices": ["1.11", "1.12"]},
        {"sessionId": runtime.copilot_id(tune["session_id"])},
    ))  # fmt: skip
    for _ in range(100):
        if harness.store.drafts():
            break
        await asyncio.sleep(0.01)
    [card] = harness.store.drafts()
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="1.12"))
    assert (await asking)["answer"] == "1.12"
    assert "The owner answered “Which k for the tuning run?”" in told() and ": 1.12." in told()

    chat = await spawn(runtime, router, "Report the k", why_new="separate")
    await call_tool(runtime, "report", chat["session_id"], status="question",
                    summary="Report with k=1.11?")  # fmt: skip
    await runtime.owner_send(chat["session_id"], "Use 1.12 everywhere.")
    assert "The owner answered “Report with k=1.11?”" in told()
    assert "Use 1.12 everywhere." in told()


async def decided(runtime, harness, title, question, choice, options=("Rule A", "Rule B")):
    """A worker whose card the owner answered: its task relies on that decision."""
    worker = await spawn(runtime, harness.router_id, title)
    await call_tool(runtime, "ask_decision", worker["session_id"], question=question,
                    options=[{"choice": option} for option in options])  # fmt: skip
    [card] = harness.store.drafts()
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text=choice))
    await settle(runtime)
    [decision] = runtime.decisions.active()
    return worker, decision


async def test_a_reask_on_a_decided_task_is_its_decision_unless_it_reconsiders(harness):
    """R1: a reworded re-ask on a decided task went through with seen_precedents=true and left
    two decisions in force. On a decided task a new card is a reconsideration saying why, or a
    decision it says is another one once it has seen the one in force."""
    runtime = await harness.connect()
    worker, decision = await decided(runtime, harness, "Tie breaker", "Rule A or rule B?",
                                     "Rule A")  # fmt: skip
    sid = worker["session_id"]
    for flags in ({"distinct": True}, {"seen_precedents": True}):
        again = await call_tool(runtime, "ask_decision", sid, question="Which tie rule, then?",
                                **flags)  # fmt: skip
        said = again.text_result_for_llm
        assert decision["id"] in said and "→ Rule A" in said and "replaces=" in said, said
        assert not harness.store.drafts(), f"no card with {flags}"
    other = await call_tool(runtime, "ask_decision", sid, question="Log every tie?", distinct=True)
    assert "Asked the owner" in other.text_result_for_llm, "another decision, shown the first"
    [card] = harness.store.drafts()
    assert card["target"]["precedents"] == [decision["text"]], "the card shows the one in force"
    await runtime.drafts.resolve(card["id"], DraftDecision(action="reject"))

    why = "Simulated 2,000 games: with rule A players still tie 61% of the time."
    await call_tool(runtime, "ask_decision", sid, question="C and D, or C only?",
                    options=[{"choice": "C and D"}, {"choice": "C only"}],
                    replaces=decision["id"], why=why)  # fmt: skip
    [card] = harness.store.drafts()
    target = card["target"]
    assert (target["replaces"], target["previous_at"]) == (decision["id"], decision["created_at"])
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="C and D"))
    [now] = runtime.decisions.active()
    assert now["text"] == "C and D, or C only? → C and D", "one decision in force"


async def test_e155_its_own_card_asked_again_is_that_card_updated(harness):
    """E155 (and R1 before the answer): a worker asked again in other words while its card was
    open: a second card of its own. It is told the card waits; with what it found since
    (same_as), that card is updated: the same id, saying when."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Tie breaker")
    sid = worker["session_id"]
    await call_tool(runtime, "ask_decision", sid, question="Rule A or rule B?",
                    options=[{"choice": "Rule A"}, {"choice": "Rule B"}])  # fmt: skip
    [card] = harness.store.drafts()
    again = await call_tool(runtime, "ask_decision", sid, question="Which rule against ties?")
    assert f'same_as="{card["id"]}"' in again.text_result_for_llm
    assert len(harness.store.drafts()) == 1
    three = [{"choice": "Rule A"}, {"choice": "Rule B"}, {"choice": "Rule C"}]
    await call_tool(runtime, "ask_decision", sid, question="Rule A, B or C?", same_as=card["id"],
                    why="C came out best in a first run.", options=three)  # fmt: skip
    [updated] = harness.store.drafts()
    assert updated["id"] == card["id"] and updated["target"]["updated_at"]
    assert updated["target"]["question"] == "Rule A, B or C?"
    assert [o["choice"] for o in updated["target"]["options"]] == ["Rule A", "Rule B", "Rule C"]
    await call_tool(runtime, "ask_decision", sid, question="Database?", distinct=True)
    assert len(harness.store.drafts()) == 2, "another decision is a card of its own"


async def test_keep_and_not_now_on_a_reconsideration_leave_the_decision_in_force(harness):
    """Item 4: Keep answers a reconsideration with the decision in force. Item 5: Not now on one
    told the worker to go on with its recommendation, which there is the change: it keeps the
    decision for now."""
    runtime = await harness.connect()
    worker, decision = await decided(runtime, harness, "Tie breaker", "Rule A or rule B?",
                                     "Rule A")  # fmt: skip
    sid = worker["session_id"]
    session = harness.session(sid)
    for how, said in (
        (DraftDecision(action="choose", choice="keep"), "The owner keeps their decision:"),
        (DraftDecision(action="reject"), "The owner keeps their decision for now:"),
    ):
        await call_tool(runtime, "ask_decision", sid, question="Switch to rule C?",
                        replaces=decision["id"], why="Rule A still ties 61%.",
                        recommendation="Rule C", options=[{"choice": "Rule C"}])  # fmt: skip
        [card] = harness.store.drafts()
        assert runtime.tasks.get(worker["task_id"])["status"] == "waiting"
        card = await runtime.drafts.resolve(card["id"], how)
        await settle(runtime)
        assert card["outcome"].startswith("Kept") and card["outcome"].endswith(": Rule A")
        assert not harness.store.drafts()
        assert said in session.sent[-1] and "Rule C" not in session.sent[-1].split(said)[1]
        assert "recommendation" not in session.sent[-1]
        assert [d["id"] for d in runtime.decisions.active()] == [decision["id"]]
        assert runtime.tasks.get(worker["task_id"])["status"] == "running"


async def test_e154_words_that_pick_no_option_are_a_note_and_the_decision_stays(harness):
    """E154: "try more variants with a lower weight first" on a reconsideration card replaced the
    decision in force, and every task relying on it reopened. Only a picked option (or words
    that name one) is a decision; other words reach the workers that asked, as the owner's."""
    runtime = await harness.connect()
    worker, decision = await decided(runtime, harness, "Weights", "Weight 1.0 or 0.5?", "1.0",
                                     options=("1.0", "0.5"))  # fmt: skip
    sid = worker["session_id"]
    await call_tool(runtime, "ask_decision", sid, question="Weight 0.5 after all, or 0.25?",
                    replaces=decision["id"], why="1.0 overfits.",
                    options=[{"choice": "0.5"}, {"choice": "0.25"}])  # fmt: skip
    [card] = harness.store.drafts()
    note = "Try more variants with a lower weight first."
    card = await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text=note))
    await settle(runtime)
    assert card["outcome"] == "Kept your decision; sent your note"
    assert [d["id"] for d in runtime.decisions.active()] == [decision["id"]]
    assert note in harness.session(sid).sent[-1]
    assert not [d for d in harness.store.drafts() if d["kind"] == "change"]

    await call_tool(runtime, "ask_decision", sid, question="0.5 or 0.25, then?",
                    replaces=decision["id"], why="Variants: 0.25 overfits least.",
                    options=[{"choice": "0.5"}, {"choice": "0.25"}])  # fmt: skip
    [card] = harness.store.drafts()
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="0.25, please"))
    [now] = runtime.decisions.active()
    assert now["text"] == "0.5 or 0.25, then? → 0.25, please", "words that name one decide"


async def test_e154_on_a_first_card_the_owners_own_words_are_the_decision(harness):
    """The owner's decision (2026-11-25): only a reconsideration keeps its decision against
    words that name no option. On a first card, nothing in force, their words decide, as before
    (fewer clicks: no "Decide or just a note")."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Docs")
    sid = worker["session_id"]
    await call_tool(runtime, "ask_decision", sid, question="Docs in English or in both languages?",
                    options=[{"choice": "English"}, {"choice": "Both"}])  # fmt: skip
    [card] = harness.store.drafts()
    said = "Spanish only, for now"
    card = await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text=said))
    await settle(runtime)
    [decision] = runtime.decisions.active()
    assert card["outcome"] == said
    assert decision["text"] == f"Docs in English or in both languages? → {said}"
    assert f"The owner decided: {said}" in harness.session(sid).sent[-1]

    other = await spawn(runtime, harness.router_id, "Docs site", why_new="separate")
    await call_tool(runtime, "ask_decision", other["session_id"], question="Light or dark theme?",
                    options=[{"choice": "Light"}, {"choice": "Dark"}])  # fmt: skip
    await runtime.owner_send(other["session_id"], "Follow the system setting.")
    assert any(d["text"] == "Light or dark theme? → Follow the system setting."
               for d in runtime.decisions.active()), "in its chat too"  # fmt: skip


async def test_e154_a_change_tells_each_worker_once_and_reopens_only_on_a_new_value(harness):
    """E154: a change reached four workers six times (once per task), reopened six finished
    tasks, and an hour later a second change made a second card. One message per worker; the
    same answer in other words reopens nothing; changes within two hours are one card."""
    runtime = await harness.connect()
    router = harness.router_id
    worker = await spawn(runtime, router, "Serve the model")
    sid = worker["session_id"]
    await call_tool(runtime, "message_session", router, session_id=sid, task="new",
                    why_new="test", earlier="keep", title="Tune the model",
                    text="Tune it.")  # fmt: skip
    tasks = [task["id"] for task in runtime.tasks.for_session(sid)]
    recorded = await call_tool(runtime, "record_decision", router, decision="Which k? → 1.11",
                               tasks=tasks)  # fmt: skip
    first = recorded.text_result_for_llm.split()[1].rstrip(",.")
    await call_tool(runtime, "report", sid, status="done", summary="Served with 1.11.",
                    task_id=tasks[0])  # fmt: skip
    before = len(harness.session(sid).sent)
    same = await call_tool(runtime, "record_decision", router, decision="Which k to serve? → 1.11",
                           replaces=[first])  # fmt: skip
    assert "nothing changed" in same.text_result_for_llm
    assert runtime.tasks.get(tasks[0])["status"] == "review", "the same value: nothing reopens"
    assert len(harness.session(sid).sent) == before
    second = runtime.decisions.active()[0]["id"]

    await call_tool(runtime, "record_decision", router, decision="Which k? → 1.12",
                    replaces=[second])  # fmt: skip
    told = [s for s in harness.session(sid).sent[before:] if 'type="decision-changed"' in s]
    assert len(told) == 1 and all(task in told[0] for task in tasks), "one message, both tasks"
    assert runtime.tasks.get(tasks[0])["status"] == "running", "a new value: it reopens"
    third = runtime.decisions.active()[0]["id"]
    await call_tool(runtime, "record_decision", router, decision="Which k? → 1.13",
                    replaces=[third])  # fmt: skip
    [card] = [d for d in harness.store.drafts() if d["kind"] == "change"]
    assert card["target"]["decision"]["text"] == "Which k? → 1.13"
    assert card["target"]["before"] == ["Which k to serve? → 1.11"], "from what it was first"


async def test_e153_a_new_decision_reaches_the_workers_whose_tasks_rely_on_it(harness):
    """E153: the Router recorded a decision linked to a worker's task, without replaces: nobody
    told it, and 75 s later it reported blocked, waiting for that decision. Each worker hears it
    once; one that waits on the owner hears it at once. Its question stays the owner's until the
    Router says the decision answers it (it may ask something else: owner report, 0.1.58)."""
    runtime = await harness.connect()
    router = harness.router_id
    waiting = await spawn(runtime, router, "Serve the model")
    busy = await spawn(runtime, router, "Tune the model", why_new="separate")
    await call_tool(runtime, "report", waiting["session_id"], status="blocked",
                    summary="Which k should I serve?")  # fmt: skip
    assert runtime.asks.session_questions(waiting["session_id"])
    said = await call_tool(runtime, "record_decision", router, decision="Serve and tune k=1.12",
                           tasks=[waiting["task_id"], busy["task_id"]])  # fmt: skip
    assert "2 of their worker(s) were told" in said.text_result_for_llm
    [decision] = runtime.decisions.active()
    for worker, mode in ((waiting, "immediate"), (busy, "enqueue")):
        session = harness.session(worker["session_id"])
        [note] = [s for s in session.sent if 'type="decision"' in s]
        assert decision["id"] in note and "Serve and tune k=1.12" in note
        assert session.modes[-1] == mode
    assert len(runtime.asks.session_questions(waiting["session_id"])) == 1, "still the owner's"
    assert runtime.tasks.get(waiting["task_id"])["status"] == "waiting", "under Needs you still"
    assert "stays open under Needs you" in harness.session(waiting["session_id"]).sent[-1]
    assert "Which k should I serve?" in said.text_result_for_llm, "the Router hears it is open"
    assert "answers_question=true" in said.text_result_for_llm
    await call_tool(runtime, "message_session", router, session_id=waiting["session_id"],
                    task="note", task_id=waiting["task_id"], answers_question=True,
                    text=f"The owner decided this: {decision['text']}.")  # fmt: skip
    assert not runtime.asks.session_questions(waiting["session_id"]), "its question settled"
    assert runtime.tasks.get(waiting["task_id"])["status"] == "running"


async def test_e160_confirmations_are_kept_once_the_owner_saw_the_change(harness):
    """E160: Got it on a change card, then a worker's confirmation: lost."""
    runtime = await harness.connect()
    router = harness.router_id
    worker = await spawn(runtime, router, "Login API")
    recorded = await call_tool(runtime, "record_decision", router, decision="Email only",
                               tasks=[worker["task_id"]])  # fmt: skip
    first = recorded.text_result_for_llm.split()[1].rstrip(",.")
    await call_tool(runtime, "record_decision", router, decision="Email and SMS", replaces=[first])
    [card] = [d for d in harness.store.drafts() if d["kind"] == "change"]
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer"))
    [new] = runtime.decisions.active()
    await call_tool(runtime, "ack_decision", worker["session_id"], decision=new["id"],
                    plan="Add the SMS form")  # fmt: skip
    card = harness.store.draft(card["id"])
    assert card["status"] == "answered" and card["target"]["tasks"][0]["ack"] == "Add the SMS form"
    assert runtime.decisions.for_task(worker["task_id"])[-1]["ack"] == "Add the SMS form"


async def test_e155_a_progress_report_keeps_the_open_question_and_a_reask_updates_it(harness):
    """E155: a progress report withdrew the worker's open question (and its task left Needs
    you), and its next question came back as new. It stays until answered, asked again (the
    same question, updated) or the task ends."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Port picker")
    sid, task = worker["session_id"], worker["task_id"]
    await call_tool(runtime, "report", sid, status="question", summary="Port 8080 or 9090?",
                    options=["8080", "9090"])  # fmt: skip
    [question] = runtime.asks.session_questions(sid)
    await call_tool(runtime, "report", sid, status="progress", summary="Load-testing both.")
    assert [q["id"] for q in runtime.asks.session_questions(sid)] == [question["id"]]
    assert runtime.tasks.get(task)["status"] == "waiting", "it still waits for the owner"
    await call_tool(runtime, "report", sid, status="question", options=["8080", "9090"],
                    summary="Port 8080 or 9090? 9090 held 20% more load.")  # fmt: skip
    [again] = runtime.asks.session_questions(sid)
    assert again["id"] == question["id"] and again["at"] == question["at"]
    assert again["updated_at"] and "20% more" in again["text"]
    view = runtime.task_actions.view(runtime.tasks.get(task))
    assert view["question"]["updated_at"] == again["updated_at"]
    await call_tool(runtime, "report", sid, status="done", summary="Went with 9090.")
    assert not runtime.asks.session_questions(sid), "its task finished without it"


async def test_e157_blocked_on_another_worker_waits_under_monitoring_not_needs_you(harness):
    """E157: a blocked report that waited on another worker went under Needs you, and the
    owner's acknowledgement raised it again. It waits on that worker, under Monitoring."""
    runtime = await harness.connect()
    router = harness.router_id
    api = await spawn(runtime, router, "Build the schema API")
    await spawn(runtime, router, "Build the schema", why_new="parallel")  # (Named by the other.)
    form = await spawn(runtime, router, "Build the signup form", why_new="parallel")
    waits = "Waiting for “Build the schema API” to publish the field names."
    await call_tool(runtime, "report", form["session_id"], status="blocked", summary=waits)
    assert not runtime.asks.session_questions(form["session_id"]), "nothing for the owner"
    view = runtime.task_actions.view(runtime.tasks.get(form["task_id"]))
    assert view["status"] == "running" and view["waits_on"] == "“Build the schema API”"
    assert harness.store.session(form["session_id"])["status"] != "waiting"
    told = [m for m in harness.store.messages(router) if m["kind"] == "inbound"][-1]
    assert "not on the owner" in told["meta"]["prompt"]
    found = run_js(
        BOARD + f"const task = {json.dumps(view)};\n"
        "const worker = { status: 'idle' };\n"
        "const phase = b.taskPhase(task, worker, b.workerContext(worker, [task]));\n"
        "return [phase.section, phase.why];"
    )
    assert found == ["monitoring", "Waits on “Build the schema API”"]
    await call_tool(runtime, "report", form["session_id"], status="blocked",
                    summary="I need the owner's Stripe key.")  # fmt: skip
    assert runtime.asks.session_questions(form["session_id"]), "the owner's, under Needs you"
    assert not runtime.task_actions.view(runtime.tasks.get(form["task_id"]))["waits_on"]
    assert api["task_id"]


@pytest.mark.parametrize(
    ("options", "said", "picked"),
    [
        (("Rule A", "Rule B"), "rule a, please", "Rule A"),
        (("Rule A", "Rule B"), "Rule A, not rule B", ""),
        (("C", "C only"), "C only", "C only"),
        (("C and D", "C only"), "C", ""),
        (("Spanish: hola", "English: hello"), "hola", "Spanish: hola"),
        (("'hi'", "'hello'"), "hi", "'hi'"),
        (("A", "B"), "Add a test first", ""),
        (("留到下一晚", "清零"), "留到下一晚吧", "留到下一晚"),
    ],
)
def test_e154_which_words_name_an_option(options, said, picked):
    """E154: only a picked option, or words that clearly name one, decide."""
    from chiefofstaff.decisions import Decisions

    assert Decisions.named({"options": [{"choice": o} for o in options]}, said) == picked


async def test_a_routers_side_question_on_the_same_task_leaves_the_blocked_ask_open(harness):
    """Owner report (0.1.58): a worker reported blocked on three things only the owner can give;
    10 s later the Router relayed a separate question of the owner's to the same worker on the
    same task, and that closed the blocked ask (and the task went back to running): it never
    stayed under Needs you. A Router message answers it only when it says so."""
    runtime = await harness.connect()
    router = harness.router_id
    worker = await spawn(runtime, router, "Ship the iPhone build to TestFlight")
    sid, task = worker["session_id"], worker["task_id"]
    session = harness.session(sid)
    blocked = "Three things from you and I can submit: the Apple ID, the team id, the icon."
    await call_tool(runtime, "report", sid, status="blocked", summary=blocked)
    [question] = runtime.asks.session_questions(sid)
    await call_tool(runtime, "message_session", router, session_id=sid, task_id=task,
                    text="Separately, answer with report(status='answer'): whose GitHub account "
                    "is Rusty?")  # fmt: skip
    told = session.sent[-1]
    await call_tool(runtime, "report", sid, status="answer", summary="Rusty is the CI bot.")
    assert [q["id"] for q in runtime.asks.session_questions(sid)] == [question["id"]]
    assert runtime.tasks.get(task)["status"] == "waiting", "it still waits for the owner"
    assert "stays open under Needs you" in told and blocked[:40] in told, "it is told so"
    assert any(q["id"] == question["id"] for q in runtime.snapshot()["questions"]) or (
        runtime.task_actions.view(runtime.tasks.get(task))["question"]["id"] == question["id"]
    ), "under Needs you"

    await call_tool(runtime, "message_session", router, session_id=sid, task_id=task,
                    task="note", answers_question=True,
                    text="The owner says: use the company Apple ID; the rest follows.")  # fmt: skip
    assert not runtime.asks.session_questions(sid), "answered: it leaves Needs you"
    assert runtime.tasks.get(task)["status"] == "running"
    assert "stays open" not in session.sent[-1]
