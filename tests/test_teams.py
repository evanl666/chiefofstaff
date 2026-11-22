"""Team threads (E129, E130): what workers that work together tell and ask each other, folded
on Home, paired in one thread per team, and a digest of it for the Router."""

import json

import pytest
from conftest import call_tool, settle


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


async def test_a_team_is_the_workers_that_work_together_each_with_a_short_name(harness):
    runtime, router, scout, writer = await team_of_two(harness)
    lone = await spawn(runtime, router, "Fix the flaky login test in the API: retries")
    [team] = runtime.snapshot()["teams"]
    assert team["id"] == f"team-{scout[:8]}"
    assert team["members"] == [scout, writer] and team["open"] == [scout, writer]
    assert team["name"] == "Token scout & Token writer"
    views = {s["id"]: s for s in runtime.snapshot()["sessions"]}
    assert (views[scout]["team"], views[writer]["team"], views[lone]["team"]) == (
        team["id"],
        team["id"],
        "",
    )
    # From its first task's title: the part before ":", cut at a word to 24 characters.
    assert views[lone]["label"] == "Fix the flaky login test"
    assert runtime.teams.set_label(lone, "  Login   fixer ") == "Login fixer"
    assert runtime.view(harness.store.session(lone))["label"] == "Login fixer"
    assert runtime.teams.set_label(lone, "") == "Fix the flaky login test"
    # A third that works with one of them joins their team; the team keeps its id.
    third = await spawn(
        runtime, router, "Token docs", works_with=[runtime.tasks.current(writer)["id"]]
    )
    [team] = runtime.snapshot()["teams"]
    assert team["members"] == [scout, writer, third] and team["id"] == f"team-{scout[:8]}"
    assert team["name"] == "Token scout, Token writer +1"


async def test_a_teams_thread_pairs_each_question_with_its_answer_and_says_what_waits(harness):
    runtime, _, scout, writer = await team_of_two(harness)
    await call_tool(runtime, "tell_session", scout, session_id=writer, text="Token 48d6.")
    asked = await call_tool(runtime, "ask_session", writer, session_id=scout, question="Scope?")
    first = asked.text_result_for_llm.split("(")[1].split(")")[0]
    await call_tool(runtime, "answer_session", scout, question_id=first, answer="read:all")
    asked = await call_tool(runtime, "ask_session", writer, session_id=scout, question="Staging?")
    second = asked.text_result_for_llm.split("(")[1].split(")")[0]
    harness.store.execute(
        "UPDATE questions SET asked=asked-540 WHERE id=?", (second,)
    )  # Asked 9 minutes ago.
    # It ends its turn to wait for the answer (its task is still open).
    harness.session(writer).emit("assistant.message", content="Waiting for Token scout.")
    harness.session(writer).emit("session.idle")
    await settle(runtime)
    team = f"team-{scout[:8]}"
    thread = runtime.teams.thread_view(team)
    assert thread["team"]["id"] == team
    items = thread["items"]
    # In time order: the open question was asked 9 minutes ago, before the others.
    assert [(i["kind"], i["from"], i["to"], i["text"]) for i in items] == [
        ("ask", writer, scout, "Staging?"),
        ("tell", scout, writer, "Token 48d6."),
        ("ask", writer, scout, "Scope?"),
    ]
    waiting, answered = items[0], items[2]
    assert (answered["status"], answered["answer"], answered["minutes"]) == (
        "answered",
        "read:all",
        0,
    )
    assert (waiting["status"], waiting["minutes"], waiting["id"]) == ("open", 9, second)
    # The writer is not at work and its task is open: the question holds it up.
    assert waiting["blocks"] is True
    [team_view] = runtime.snapshot()["teams"]
    assert (team_view["told"], team_view["asked"], team_view["answered"]) == (1, 2, 1)
    assert [(q["id"], q["minutes"], q["blocks"]) for q in team_view["waiting"]] == [
        (second, 9, True)
    ]
    # By one of its workers too; an unknown one is not a team.
    assert runtime.teams.thread_view(writer[:8])["team"]["id"] == team
    with pytest.raises(LookupError):
        runtime.teams.thread_view("team-nobody00")


async def test_the_router_gets_who_told_whom_never_the_words(harness):
    """E129: the Router's next turn gets a digest of what workers told each other (who, how
    often, what still waits), never a copy of each message: it cannot pass a value on (E3)."""
    runtime, router, scout, writer = await team_of_two(harness)
    await call_tool(runtime, "tell_session", scout, session_id=writer, text="Token 48d6e521.")
    asked = await call_tool(runtime, "ask_session", writer, session_id=scout, question="Scope?")
    question = asked.text_result_for_llm.split("(")[1].split(")")[0]
    rows = peer_rows(harness, router)
    assert len(rows) == 2 and not any(row["meta"].get("prompt") for row in rows), "lines only"
    await runtime.owner_send(router, "How is the token going?")
    await settle(runtime)
    sent = harness.session(router).sent[-1]
    assert "48d6e521" not in sent and "Scope?" not in sent
    assert sent.startswith('<message from="system" type="team-digest">'), sent[:200]
    assert (
        f"- team-{scout[:8]} (worker:{scout[:8]} “Token scout”, worker:{writer[:8]} “Token "
        f"writer”): 1 asked, 1 told. Open: “Token writer” waits on “Token scout” for 0 min "
        f"({question})" in sent
    )
    # Nothing new between them: the next turn has no digest.
    await runtime.owner_send(router, "And the docs?")
    await settle(runtime)
    assert "team-digest" not in harness.session(router).sent[-1]
    # The words, when the Router needs them; reading them changes nothing (no write at all).
    writes = harness.store.db.total_changes
    read = await call_tool(runtime, "read_team_thread", router, team=f"worker:{writer[:8]}")
    assert harness.store.db.total_changes == writes, "read-only"
    text = read.text_result_for_llm
    assert "told “Token writer”" in text and "Token 48d6e521." in text
    assert f"({question}): Scope?\n  Not answered yet (0 min)." in text
    assert "never pass it on yourself" in text


async def test_what_workers_tell_each_other_reaches_the_router_only_as_their_own_reports(harness):
    """Live E1 (r81): the Router gets its workers' reports as they wrote them, and one may name
    a value it handed on; what one worker told, asked or answered another it is given nowhere
    (a turn, the team digest, a fresh Router's handover, the records its search shows)."""
    from chiefofstaff.envelope import ENVELOPE

    runtime, router, scout, writer = await team_of_two(harness)
    await call_tool(runtime, "tell_session", scout, session_id=writer,
                    text="Token 4a7c19e2, rotate at 18:00.")  # fmt: skip
    asked = await call_tool(runtime, "ask_session", writer, session_id=scout,
                            question="Staging too, or prod only?")  # fmt: skip
    question = asked.text_result_for_llm.split("(")[1].split(")")[0]
    await call_tool(runtime, "answer_session", scout, question_id=question, answer="Prod only.")
    task = runtime.tasks.current(scout)["id"]
    await call_tool(runtime, "report", scout, status="done", task_id=task,
                    summary="Generated token 4a7c19e2 and sent it to the writer.")  # fmt: skip
    await runtime.owner_send(router, "How is the token going?")
    await settle(runtime)
    sent = "\n".join(harness.session(router).sent)
    reports = [body for head, body in ENVELOPE.findall(sent) if 'type="report"' in head]
    assert any("4a7c19e2" in body for body in reports), "its own report, as it wrote it"
    given = [
        sent,
        runtime.handover(),
        json.dumps(runtime.workers.find("token rotate staging prod"), ensure_ascii=False),
    ]
    for words in ("rotate at 18:00", "Staging too, or prod only?", "Prod only."):
        assert not any(words in text for text in given), words


async def test_peer_rows_from_before_ride_along_themselves_not_in_a_digest(harness):
    runtime, router, scout, writer = await team_of_two(harness)
    runtime.delivery.hold(
        router,
        '<message from="system" type="peer-message">old copy</message>',
        kind="peer",
        display="“Token scout” told “Token writer”: old copy",
        meta={"peer_from": scout, "peer_to": writer},
    )
    await runtime.owner_send(router, "Status?")
    await settle(runtime)
    sent = harness.session(router).sent[-1]
    assert "old copy" in sent and "team-digest" not in sent


async def test_a_question_holding_a_task_up_half_an_hour_needs_the_owner_once_per_team(harness):
    """E129, phase 4: a question its asker's task waits on, open 30 minutes or more, is one line
    under Needs you per team; Nudge tells the one asked (at most every half hour)."""
    from chiefofstaff.digest import needs, needs_you, waiting_parts

    runtime, _, scout, writer = await team_of_two(harness)
    asked = await call_tool(runtime, "ask_session", writer, session_id=scout, question="Scope?")
    question = asked.text_result_for_llm.split("(")[1].split(")")[0]
    other = await call_tool(runtime, "ask_session", writer, session_id=scout, question="Staging?")
    later = other.text_result_for_llm.split("(")[1].split(")")[0]
    harness.session(writer).emit("assistant.message", content="Waiting for Token scout.")
    harness.session(writer).emit("session.idle")
    await settle(runtime)
    before = needs_you(runtime)
    harness.store.execute("UPDATE questions SET asked=asked-29*60 WHERE id=?", (question,))
    assert not needs(runtime).of("team"), "29 minutes: not yet"
    harness.store.execute("UPDATE questions SET asked=asked-40*60 WHERE id IN (?, ?)",
                          (question, later))  # fmt: skip
    [held] = needs(runtime).of("team")
    assert (held["question"]["id"], held["question"]["minutes"]) == (question, 69)
    assert needs_you(runtime) == before + 1, "one line for the team, not one per question"
    assert "1 worker waiting on another one's answer" in waiting_parts(needs(runtime))

    said = await runtime.teams.nudge(f"team-{scout[:8]}", question)
    assert said == "Nudged “Token scout”: “Token writer” waits for its answer."
    nudge = harness.session(scout).sent[-1]
    assert nudge.startswith('<message from="system" type="peer-nudge"')
    assert f'answer_session(question_id="{question}")' in nudge and "Scope?" in nudge
    [waiting] = [q for q in runtime.snapshot()["teams"][0]["waiting"] if q["id"] == question]
    assert waiting["nudged"]
    with pytest.raises(ValueError, match="Nudged already"):
        await runtime.teams.nudge(f"team-{scout[:8]}", question)
    with pytest.raises(LookupError):
        await runtime.teams.nudge("team-nobody00", question)
    await call_tool(runtime, "answer_session", scout, question_id=question, answer="read:all")
    await call_tool(runtime, "answer_session", scout, question_id=later, answer="no")
    assert not needs(runtime).of("team")
    with pytest.raises(ValueError, match="answered already"):
        await runtime.teams.nudge(f"team-{scout[:8]}", question)


async def test_workers_that_talk_unasked_to_work_together_are_a_team_and_nothing_more(harness):
    """A worker that found another (find_sessions) and asked it: they talk, so their exchange
    has a team and a thread; they are not made to work together (no result of one reaches the
    other)."""
    runtime = await harness.connect()
    router = harness.router_id
    expert = await spawn(runtime, router, "Billing research")
    asker = await spawn(runtime, router, "Invoice bug")
    assert runtime.snapshot()["teams"] == []
    await call_tool(runtime, "ask_session", asker, session_id=expert, question="Which table?")
    [team] = runtime.snapshot()["teams"]
    assert team["members"] == [expert, asker] and team["asked"] == 1
    assert runtime.peers.of(asker) == [] and runtime.peers.of(expert) == []
