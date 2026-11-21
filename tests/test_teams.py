"""Team threads (E129, E130): what workers that work together tell and ask each other, folded
on Home, paired in one thread per team, and a digest of it for the Router."""

import json

from conftest import call_tool


async def spawn(runtime, router, title, **extra):
    result = await call_tool(
        runtime, "spawn_worker", router, title=title, instructions=title, why_new="test", **extra
    )
    return json.loads(result.text_result_for_llm)["session_id"]


async def team_of_two(harness):
    runtime = await harness.connect()
    router = harness.router_id
    scout = await spawn(runtime, router, "Token scout")
    task = runtime.tasks.current(scout)["id"]
    writer = await spawn(runtime, router, "Token writer", works_with=[task])
    return runtime, router, scout, writer


def peer_rows(harness, router):
    return [m for m in harness.store.messages(router) if m["kind"] == "peer"]


async def test_each_peer_row_says_what_it_is_and_which_question(harness):
    """Home folds the peer rows and says what waits: each row says whether one told, asked or
    answered the other, and of which question."""
    runtime, router, scout, writer = await team_of_two(harness)
    await call_tool(runtime, "tell_session", scout, session_id=writer, text="The token is 48d6.")
    asked = await call_tool(
        runtime, "ask_session", writer, session_id=scout, question="Which scope is it for?"
    )
    question = asked.text_result_for_llm.split("(")[1].split(")")[0]
    await call_tool(runtime, "answer_session", scout, question_id=question, answer="read:all")
    rows = peer_rows(harness, router)
    assert [(m["meta"]["peer_kind"], m["meta"].get("question_id")) for m in rows] == [
        ("told", None),
        ("asked", question),
        ("answered", question),
    ]
    assert [(m["meta"]["peer_from"], m["meta"]["peer_to"]) for m in rows] == [
        (scout, writer),
        (writer, scout),
        (scout, writer),
    ]


async def test_the_state_lists_the_questions_workers_wait_on_each_other_for(harness):
    """Open questions between workers (not to the Router, not answered) are in the state, so
    Home can say "1 waiting 9 min"."""
    runtime, router, scout, writer = await team_of_two(harness)
    asked = await call_tool(runtime, "ask_session", writer, session_id=scout, question="Scope?")
    question = asked.text_result_for_llm.split("(")[1].split(")")[0]
    await call_tool(runtime, "ask_session", scout, session_id=router, question="Who owns it?")
    waiting = runtime.snapshot()["peer_waiting"]
    assert [(q["id"], q["asker"], q["target"]) for q in waiting] == [(question, writer, scout)]
    assert waiting[0]["asked"].endswith("+00:00")
    await call_tool(runtime, "answer_session", scout, question_id=question, answer="read:all")
    assert runtime.snapshot()["peer_waiting"] == []
