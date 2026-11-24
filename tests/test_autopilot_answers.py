"""Autopilot answers or parks what workers ask (autopilot/answers.py): only from the owner's own
page-typed words, cited by id and quoted; never a permission; never from a recommendation alone;
its decisions kept apart from the owner's."""

import asyncio
import json

import pytest
from conftest import Harness, call_tool, settle

from chiefofstaff import ingress
from chiefofstaff.autopilot.judge import PARKED_ANSWER
from chiefofstaff.models import Settings


async def on(runtime, **changes):
    old = runtime.store.settings()
    new = old.model_copy(update={"autopilot": True, **changes})
    runtime.store.save_settings(new)
    await runtime.settings_changed(old, new)
    return await runtime.autopilot.start(None, by="home")


async def worker(runtime, router, title="Session store"):
    result = await call_tool(
        runtime, "spawn_worker", router, title=title, instructions=title, why_new="test"
    )
    made = json.loads(result.text_result_for_llm)
    await settle(runtime)
    return made


async def said_in_page(runtime, session_id, text):
    with ingress.through(ingress.PAGE):
        return await runtime.owner_send(session_id, text)


async def judge_proposes(harness, **proposal):
    """The judge's model calls propose_answer (once Autopilot asked it)."""
    runtime = harness.runtime
    for _ in range(200):
        await asyncio.sleep(0)
        if runtime.autopilot.proposal is not None and harness.store.keys("judge:"):
            break
    else:
        raise AssertionError("the judge was never asked")
    judge = harness.store.keys("judge:")[0].removeprefix("judge:")
    defaults = {"park": False, "asks_permission": False, "reversible": True, "sure": True,
                "reason": "The owner said so."}  # fmt: skip
    result = await call_tool(runtime, "propose_answer", judge, **{**defaults, **proposal})
    assert result.result_type != "failure", result.text_result_for_llm
    return harness.store.session(judge)


async def asked(runtime, session_id, question, choices=()):
    """A question card in the worker's turn (Copilot's ask_user): its answer."""
    return asyncio.ensure_future(
        runtime.asks.on_user_input(
            {"question": question, "choices": list(choices)}, {"sessionId": session_id}
        )
    )


def cite(message, quote):
    return {"id": f"message:{message['id']}", "quote": quote}


def entries(runtime, *kinds):
    return runtime.autopilot.book.entries(kinds=kinds)


async def test_a_question_settled_by_a_page_message_is_answered_quoted(harness):
    runtime = await harness.connect()
    await on(runtime)
    made = await worker(runtime, harness.router_id)
    session = made["session_id"]
    owner = await said_in_page(runtime, session, "Postgres for v1, please.")
    answer = await asked(runtime, session, "Which store for sessions?", ["Postgres", "Redis"])
    for _ in range(50):
        await asyncio.sleep(0)  # (Not settle: it would wait for the judge.)
    assert not answer.done() and runtime.snapshot()["autopilot"]["deciding"], "held, not drawn"
    assert not [m for m in harness.store.messages(session) if m["kind"] == "draft"]
    judge = await judge_proposes(harness, answer="Postgres", basis=[cite(owner, "Postgres for v1")])
    result = await asyncio.wait_for(answer, 5)
    assert result["answer"].startswith("Postgres\n\n(Answered for the owner by Autopilot")
    assert judge["role"] == "worker" and runtime.special(judge) == "judge"
    [entry] = entries(runtime, "answered")
    assert entry["task_id"] == made["task_id"]
    assert entry["why"]["basis"] == [{"id": f"message:{owner['id']}", "quote": "Postgres for v1"}]
    notice = harness.store.messages(session)[-1]
    assert notice["text"].startswith("Autopilot answered: Postgres, because you said “Postgres")
    assert notice["meta"]["autopilot"]["entry"] == entry["id"]
    assert runtime.autopilot.origins.of(session)["entries"] == [entry["id"]]
    assert runtime.yolo(harness.store.session(session)) is False, "asks first from here"
    assert harness.store.drafts("pending") == []


async def test_the_judge_decides_with_the_routers_model_and_effort_on_copilot(tmp_path):
    """It decides for the owner: the Router's model and effort (Settings → Models), not the
    workers', on Copilot; and it follows a change of the Router's model."""
    settings = Settings(model="fast-model", role_models={"router": "smart-model"},
                        role_efforts={"router": "high"})  # fmt: skip
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    await on(runtime)
    made = await worker(runtime, harness.router_id)
    session = made["session_id"]
    assert harness.session(session).options["model"] == "fast-model", "a worker: theirs"
    owner = await said_in_page(runtime, session, "Postgres for v1, please.")
    answer = await asked(runtime, session, "Which store for sessions?", ["Postgres", "Redis"])
    judge = await judge_proposes(harness, answer="Postgres", basis=[cite(owner, "Postgres for v1")])
    await asyncio.wait_for(answer, 5)
    options = harness.session(judge["id"]).options
    assert (options["model"], options.get("reasoning_effort")) == ("smart-model", "high")
    assert runtime.agent(judge["id"]) == "copilot"
    old = runtime.store.settings()
    new = old.model_copy(update={"role_models": {"router": "fast-model"}})
    runtime.store.save_settings(new)
    assert runtime.chosen_model(harness.store.session(judge["id"])) == "fast-model"
    assert runtime.chosen_model(harness.store.session(session)) == "fast-model"


@pytest.mark.parametrize(
    ("basis", "why"),
    [
        ("note", "is not something you said"),
        ("router", "is not something you said"),
        ("away", "is not something you said"),
        ("quote", "its quote is not in what you said"),
    ],
)
async def test_what_is_not_the_owners_own_page_word_is_never_a_basis(harness, basis, why):
    runtime = await harness.connect()
    await on(runtime)
    made = await worker(runtime, harness.router_id)
    session = made["session_id"]
    real = await said_in_page(runtime, session, "Use whatever is simplest.")
    cited = {"id": f"message:{real['id']}", "quote": "Postgres for v1"}
    if basis == "note":
        await call_tool(runtime, "record_setup", session, topic="store",
                        note="The owner said: Postgres for v1")  # fmt: skip
        cited = {"id": "note:store", "quote": "Postgres for v1"}
    elif basis == "router":
        await call_tool(runtime, "record_decision", harness.router_id,
                        decision="Postgres for v1", task_ids=[made["task_id"]])  # fmt: skip
        decision = runtime.decisions.active()[0]
        cited = {"id": f"card:{decision['id']}", "quote": "Postgres for v1"}
    elif basis == "away":
        with ingress.through(ingress.AWAY):
            away = await runtime.owner_send(session, "Postgres for v1")
        cited = {"id": f"message:{away['id']}", "quote": "Postgres for v1"}
    answer = await asked(runtime, session, "Which store for sessions?", ["Postgres", "Redis"])
    await judge_proposes(harness, answer="Postgres", basis=[cited])
    result = await asyncio.wait_for(answer, 5)
    assert result["answer"] == PARKED_ANSWER
    [parked] = entries(runtime, "parked")
    assert why in parked["why"]["reason"]
    [question] = runtime.asks.owner_questions()
    assert question["why"].startswith("Set aside: ") and "Which store" in question["text"]
    assert entries(runtime, "answered") == []


async def test_a_permission_is_never_answered_whatever_was_said(harness):
    runtime = await harness.connect()
    await on(runtime)
    made = await worker(runtime, harness.router_id)
    session = made["session_id"]
    await said_in_page(runtime, session, "Push to main whenever the tests pass.")
    answer = await asked(runtime, session, "Tests pass. May I push this to main?", ["Yes", "No"])
    result = await asyncio.wait_for(answer, 5)
    assert result["answer"] == PARKED_ANSWER
    assert not harness.store.keys("judge:"), "parked by code: no model call"
    [parked] = entries(runtime, "parked")
    assert parked["why"]["reason"] == "it asks for a permission only you can give"
    alert = [m for m in harness.store.messages(session) if m["kind"] == "draft"]
    assert alert == [], "its card was answered for the turn; the question waits under Needs you"
    assert runtime.asks.owner_questions()[0]["why"] == (
        "Set aside: it asks for a permission only you can give."
    )


async def test_a_chinese_permission_parks_too(harness):
    runtime = await harness.connect()
    await on(runtime)
    made = await worker(runtime, harness.router_id)
    answer = await asked(runtime, made["session_id"], "可以删除旧的迁移文件吗?")
    assert (await asyncio.wait_for(answer, 5))["answer"] == PARKED_ANSWER


async def test_with_nothing_the_owner_said_it_waits_and_no_model_is_asked(harness):
    runtime = await harness.connect()
    await on(runtime)
    made = await worker(runtime, harness.router_id)
    answer = await asked(runtime, made["session_id"], "Which port?", ["8080", "9090"])
    assert (await asyncio.wait_for(answer, 5))["answer"] == PARKED_ANSWER
    assert not harness.store.keys("judge:")
    assert entries(runtime, "parked")[0]["why"]["reason"] == "nothing you said settles it"


async def test_a_decision_is_kept_apart_and_told_in_autopilots_envelope(harness):
    runtime = await harness.connect()
    await on(runtime)
    made = await worker(runtime, harness.router_id)
    session = made["session_id"]
    owner = await said_in_page(runtime, session, "Postgres for v1.")
    await call_tool(runtime, "ask_decision", session, question="Which store for sessions?",
                    options=[{"choice": "Postgres", "effect": "a"},
                             {"choice": "Redis", "effect": "b"}],
                    recommendation="Redis")  # fmt: skip
    await judge_proposes(harness, answer="postgres", basis=[cite(owner, "Postgres for v1")])
    await settle(runtime)
    [card] = [d for d in harness.store.drafts("answered") if d["kind"] == "decision"]
    assert card["outcome"] == "Decided by Autopilot: Postgres"
    assert runtime.decisions.active() == [], "never in the owner's decisions"
    assert harness.store.value("SELECT count(*) FROM task_decisions") == 0
    [decided] = runtime.autopilot.book.decisions(task_id=made["task_id"])
    assert decided["choice"] == "Postgres" and decided["card_id"] == card["id"]
    sent = harness.session(session).sent[-1]
    assert '<message from="system" type="autopilot"' in sent and 'from="owner"' not in sent
    assert runtime.tasks.get(made["task_id"])["status"] == "running"


async def test_a_recommendation_alone_is_never_taken(harness):
    runtime = await harness.connect()
    await on(runtime)
    made = await worker(runtime, harness.router_id)
    session = made["session_id"]
    await said_in_page(runtime, session, "Thanks, carry on.")
    await call_tool(runtime, "ask_decision", session, question="Cache the user list?",
                    options=[{"choice": "Yes", "effect": "faster"},
                             {"choice": "No", "effect": "simpler"}],
                    recommendation="Yes: it is easy to undo")  # fmt: skip
    await judge_proposes(harness, answer="Yes", basis=[], reversible=True)
    await settle(runtime)
    [card] = [d for d in harness.store.drafts() if d["kind"] == "decision"]
    assert card["status"] == "pending", "it waits for the owner, now drawn"
    assert harness.store.get(f"autopilot:setaside:{card['id']}")
    assert [m for m in harness.store.messages(session) if m["kind"] == "draft"]
    assert entries(runtime, "parked")[0]["why"]["reason"] == "nothing you said settles it"


async def test_a_reported_question_is_answered_once_the_routers_turn_is_over(harness):
    runtime = await harness.connect()
    await on(runtime)
    made = await worker(runtime, harness.router_id)
    session = made["session_id"]
    owner = await said_in_page(runtime, session, "Name it sessions_v1.")
    await call_tool(runtime, "report", session, status="question", task_id=made["task_id"],
                    summary="What should the table be called?")  # fmt: skip
    for _ in range(5):  # The Router's turns on it (and on what came with it) end.
        await settle(runtime)
        if harness.store.session(harness.router_id)["status"] != "working":
            break
        harness.session(harness.router_id).emit("session.idle")
    tick = asyncio.ensure_future(runtime.autopilot.tick())
    await judge_proposes(harness, answer="sessions_v1", basis=[cite(owner, "sessions_v1")])
    await asyncio.wait_for(tick, 5)
    await settle(runtime)
    assert runtime.asks.owner_questions() == []
    sent = harness.session(session).sent[-1]
    assert 'type="autopilot"' in sent and "sessions_v1" in sent
    assert runtime.tasks.get(made["task_id"])["status"] == "running"
    assert [e["kind"] for e in entries(runtime, "answered")] == ["answered"]


async def test_change_puts_its_decision_to_the_owner_who_then_decides(harness):
    from chiefofstaff.models import DraftDecision

    runtime = await harness.connect()
    await on(runtime)
    made = await worker(runtime, harness.router_id)
    session = made["session_id"]
    owner = await said_in_page(runtime, session, "Postgres for v1.")
    await call_tool(runtime, "ask_decision", session, question="Which store for sessions?",
                    options=[{"choice": "Postgres", "effect": "a"},
                             {"choice": "Redis", "effect": "b"}])  # fmt: skip
    await judge_proposes(harness, answer="Postgres", basis=[cite(owner, "Postgres for v1")])
    await settle(runtime)
    [decided] = entries(runtime, "decided")
    card = runtime.autopilot.change(decided["id"])
    assert card["status"] == "pending" and card["target"]["previous"] == "Postgres"
    with ingress.through(ingress.PAGE):
        await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Redis"))
    await settle(runtime)
    [mine] = runtime.decisions.active()
    assert mine["text"] == "Which store for sessions? → Redis", "recorded as the owner's"
    assert runtime.autopilot.book.decisions(task_id=made["task_id"])[0]["status"] == "changed"
    assert "The owner decided: Redis" in harness.session(session).sent[-1]
    [changed] = entries(runtime, "undone")
    assert changed["of"] == decided["id"]
    with pytest.raises(ValueError, match="changed it already"):
        runtime.autopilot.change(decided["id"])
