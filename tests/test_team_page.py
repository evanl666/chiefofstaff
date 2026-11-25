"""The team page (team-page-v4, phase 1) on the server: Since you looked kept on the server,
what a team decided, the views of parts Home does not list, Home's newest exchange of a team,
and Tell the team: the owner's word to the parts they choose, each as its task stands now (at
work, waiting on them, its result ready: now; not started: with its start; done: a follow-up
that reopens it), one passive copy for the Router and no turn of its own."""

import json
import re

import pytest
from conftest import call_tool, settle
from test_teams_lite import owner_turn, spawn, teams_of

from chiefofstaff.models import DraftDecision

REQUEST = (
    "Refunds for acme-shop: the refund API, then the order page and the email side by side, "
    "then end-to-end tests."
)


async def refund_team(harness, runtime) -> dict:
    """The prototype's request: API done and accepted; the order page waiting on the owner's
    decision; the email at work; the end-to-end tests not started (after both)."""
    store = harness.store
    store.save_settings(store.settings().model_copy(update={"max_workers": 100}))
    router = harness.router_id
    await owner_turn(harness, runtime, REQUEST)
    api = await spawn(runtime, router, "Refund API: POST /v2/orders/{id}/refunds")
    page = await spawn(
        runtime, router, "Order page: the refund button and dialog", after=[api["task_id"]]
    )
    mail = await spawn(runtime, router, "Refund email", works_with=[api["task_id"]])
    e2e = await spawn(
        runtime, router, "End-to-end tests: refunds", after=[page["task_id"], mail["task_id"]]
    )
    await settle(runtime)
    await call_tool(runtime, "report", api["session_id"], status="done", summary="API merged.")
    runtime.task_actions.accept(api["task_id"])
    await settle(runtime)
    assert runtime.tasks.get(page["task_id"])["status"] == "running", "it starts after the API"
    await call_tool(
        runtime,
        "ask_decision",
        page["session_id"],
        question="Partial refunds too?",
        why="The API takes an amount.",
        options=[
            {"choice": "Full refunds only", "effect": "Ships today"},
            {"choice": "Partial refunds too", "effect": "About 30 minutes more"},
        ],
        recommendation="Partial refunds too: support's most common ask",
    )
    await settle(runtime)
    [team] = teams_of(runtime)
    return {"api": api, "page": page, "mail": mail, "e2e": e2e, "team": team}


def status(runtime, part) -> str:
    return runtime.tasks.get(part["task_id"])["status"]


def owner_messages(harness, session_id: str) -> list[dict]:
    return [m for m in harness.store.messages(session_id) if m["kind"] == "owner"]


def router_copies(harness) -> list[dict]:
    return [m for m in owner_messages(harness, harness.router_id) if m["meta"].get("to")]


async def test_pin_a_message_to_one_worker_gives_the_router_one_copy_and_no_turn(harness):
    """Today's behaviour, kept: the owner's message to one worker reaches it, and the Router
    gets one passive copy with its next message (no turn of its own)."""
    runtime = await harness.connect()
    found = await refund_team(harness, runtime)
    router = harness.session(harness.router_id)
    turns = len(router.sent)
    await runtime.owner_send(found["mail"]["session_id"], "Use the order email's layout.")
    await settle(runtime)
    [copy] = router_copies(harness)
    assert copy["meta"]["to"] == found["mail"]["session_id"] and copy["meta"]["deferred"]
    assert 'type="to-worker"' in copy["meta"]["prompt"]
    assert len(router.sent) == turns, "no Router turn"


async def test_tell_the_team_reaches_each_part_as_it_stands_and_the_router_once(harness):
    runtime = await harness.connect()
    found = await refund_team(harness, runtime)
    team, page, mail, e2e = found["team"], found["page"], found["mail"], found["e2e"]
    router = harness.session(harness.router_id)
    turns = len(router.sent)
    before = {
        part: len(harness.session(found[part]["session_id"]).sent) for part in ("page", "mail")
    }
    [card] = [d for d in harness.store.drafts() if d["kind"] == "decision"]
    text = "Say in the dialog and the email: partial refunds arrive in 3 business days."
    told = await runtime.teams_lite.tell(
        team["id"], text, [page["task_id"], mail["task_id"], e2e["task_id"]], "k1"
    )
    await settle(runtime)
    assert [(item["part"], item["how"]) for item in told["to"]] == [
        (page["task_id"], "now"),
        (mail["task_id"], "now"),
        (e2e["task_id"], "start"),
    ]
    line = (
        f"To the parts of \u201c{team['goal']}\u201d (you are Order page; also sent to "
        "Refund email, End-to-end tests (when it starts)):"
    )
    for part in ("page", "mail"):
        session = harness.session(found[part]["session_id"])
        assert len(session.sent) == before[part] + 1, "one message each, now"
        assert text in session.sent[-1] and "To the parts of" in session.sent[-1]
        [mine] = [
            m for m in owner_messages(harness, found[part]["session_id"]) if text in m["text"]
        ]
        assert mine["meta"]["team"] == team["id"] and mine["meta"]["to_parts"] == [
            "Order page",
            "Refund email",
            "End-to-end tests",
        ]
    assert line in harness.session(page["session_id"]).sent[-1]
    # Waiting on the owner's decision: it reads it, and the card still waits for them.
    assert harness.store.draft(card["id"])["status"] == "pending"
    assert status(runtime, page) == "waiting"
    assert status(runtime, mail) == "running"
    # Not started: nothing sent, nothing spent; kept for its start.
    assert status(runtime, e2e) == "queued"
    assert e2e["session_id"] not in harness.client.sessions, "not even made yet"
    [note] = json.loads(harness.store.get(f"teamnote:{e2e['task_id']}"))
    assert note["text"].endswith(text) and note["team"] == team["id"]
    # One passive copy for the Router, naming the team and who got it; no turn of its own.
    [copy] = router_copies(harness)
    assert copy["text"] == text and copy["meta"]["to"] == f"team:{team['id']}"
    assert copy["meta"]["deferred"] and copy["meta"]["to_parts"] == [
        "Order page",
        "Refund email",
        "End-to-end tests",
    ]
    assert 'type="to-worker"' in copy["meta"]["prompt"]
    assert (
        "End-to-end tests (worker " in copy["meta"]["prompt"]
        and "when its task starts" in copy["meta"]["prompt"]
    )
    assert len(router.sent) == turns, "no Router turn"
    # It hears of it with its next message, once.
    await runtime.owner_send(harness.router_id, "How is it going?")
    assert router.sent[-1].count(text) == 1
    # The same send again (a second click): nothing more.
    again = await runtime.teams_lite.tell(team["id"], text, [mail["task_id"]], "k1")
    assert again["key"] == "k1" and len(again["to"]) == 3
    assert len(router_copies(harness)) == 1
    assert len(harness.session(mail["session_id"]).sent) == before["mail"] + 1


async def test_a_part_not_started_reads_it_in_its_first_prompt(harness):
    runtime = await harness.connect()
    found = await refund_team(harness, runtime)
    team, page, mail, e2e = found["team"], found["page"], found["mail"], found["e2e"]
    text = "Cover a refund of a 31-day-old order: it must be refused."
    await runtime.teams_lite.tell(team["id"], text, [e2e["task_id"]], "k2")
    view = runtime.teams_lite.page(team["id"])["tells"][-1]
    assert [item["delivery"] for item in view["to"]] == ["start"]
    await runtime.drafts.resolve(
        next(d for d in harness.store.drafts() if d["kind"] == "decision")["id"],
        DraftDecision(action="answer", text="Partial refunds too"),
    )
    await settle(runtime)
    for part in (page, mail):
        await call_tool(runtime, "report", part["session_id"], status="done", summary="Done.")
        runtime.task_actions.accept(part["task_id"])
        await settle(runtime)
    assert status(runtime, e2e) == "running"
    first = harness.session(e2e["session_id"]).sent[0]
    assert text in first and "(you are End-to-end tests):" in first, (
        "with its start, in its first prompt"
    )
    assert first.index("End-to-end tests: refunds") < first.index(text), "after its instructions"
    assert harness.store.get(f"teamnote:{e2e['task_id']}") is None
    [mine] = [m for m in owner_messages(harness, e2e["session_id"]) if text in m["text"]]
    assert mine["meta"]["team"] == team["id"]
    view = runtime.teams_lite.page(team["id"])["tells"][-1]
    assert view["to"][0]["message"] == mine["id"] and view["to"][0]["delivery"] != "start"


async def test_a_done_part_only_when_chosen_and_then_it_reopens(harness):
    runtime = await harness.connect()
    found = await refund_team(harness, runtime)
    team, api, mail = found["team"], found["api"], found["mail"]
    sent = len(harness.session(api["session_id"]).sent)
    await runtime.teams_lite.tell(team["id"], "Check the email copy.", [mail["task_id"]], "k3")
    assert len(harness.session(api["session_id"]).sent) == sent, "not chosen: nothing"
    assert status(runtime, api) == "done"
    told = await runtime.teams_lite.tell(
        team["id"], "Return 409 for a refund over the amount paid.", [api["task_id"]], "k4"
    )
    await settle(runtime)
    assert told["to"][0]["how"] == "reopen"
    assert status(runtime, api) == "running", "a follow-up that reopens it"
    assert "409 for a refund over the amount paid" in harness.session(api["session_id"]).sent[-1]
    assert len(router_copies(harness)) == 2, "one copy per send, none per part"


async def test_tell_the_team_refuses_what_is_not_a_part_now(harness):
    runtime = await harness.connect()
    found = await refund_team(harness, runtime)
    team, mail = found["team"], found["mail"]
    await owner_turn(harness, runtime, "Unrelated: billing research.")
    other = await spawn(runtime, harness.router_id, "Billing research")
    sent = len(harness.session(mail["session_id"]).sent)
    for to, words in (
        ([mail["task_id"], other["task_id"]], "Not a part of this team now"),
        ([], "Choose who gets it"),
    ):
        with pytest.raises(ValueError, match=words):
            await runtime.teams_lite.tell(team["id"], "Hello.", to, "k5")
    with pytest.raises(ValueError, match="Write what"):
        await runtime.teams_lite.tell(team["id"], "  ", [mail["task_id"]], "k6")
    with pytest.raises(LookupError):
        await runtime.teams_lite.tell("team-1-tdeadbe", "Hello.", [mail["task_id"]], "k7")
    runtime.tasks.move(found["e2e"]["task_id"], "cancelled")
    with pytest.raises(ValueError, match="was cancelled"):
        await runtime.teams_lite.tell(team["id"], "Hi.", [found["e2e"]["task_id"]], "k8")
    assert len(harness.session(mail["session_id"]).sent) == sent, "refused as a whole"
    assert router_copies(harness) == []


async def test_since_you_looked_is_kept_on_the_server_for_every_tab(harness):
    runtime = await harness.connect()
    found = await refund_team(harness, runtime)
    team = found["team"]
    assert runtime.teams_lite.page(team["id"])["team"]["looked"] == ""
    assert teams_of(runtime)[0]["looked"] == ""
    looked = runtime.teams_lite.look(team["id"])["looked"]
    # Another tab (or device) reads the same look.
    assert runtime.teams_lite.page(team["id"])["team"]["looked"] == looked
    assert teams_of(runtime)[0]["looked"] == looked
    assert harness.store.get(f"teamlook:{team['id']}") == looked
    with pytest.raises(ValueError, match="No such team"):
        runtime.teams_lite.look("team-x")


async def test_decided_holds_decisions_and_answers_never_a_note(harness):
    runtime = await harness.connect()
    found = await refund_team(harness, runtime)
    team, page, mail = found["team"], found["page"], found["mail"]
    await call_tool(
        runtime,
        "tell_session",
        mail["session_id"],
        session_id=page["session_id"],
        text="The email's title is Refund accepted.",
    )
    asked = await call_tool(
        runtime,
        "ask_session",
        page["session_id"],
        session_id=mail["session_id"],
        question="Is the amount in cents or in dollars?",
    )
    question = re.search(r"\((q\w+)\)", asked.text_result_for_llm).group(1)
    await call_tool(
        runtime,
        "answer_session",
        mail["session_id"],
        question_id=question,
        answer="Cents, always, as the order API. The mock's dollars are a leftover.",
    )
    card = next(d for d in harness.store.drafts() if d["kind"] == "decision")
    await runtime.drafts.resolve(
        card["id"], DraftDecision(action="answer", text="Partial refunds too")
    )
    await settle(runtime)
    decided = runtime.teams_lite.page(team["id"])["decided"]
    assert [(e["kind"], e["text"]) for e in decided] == [
        ("between", "Cents, always, as the order API"),
        ("yours", "Partial refunds too"),
    ]
    between, yours = decided
    assert (between["from"], between["to"]) == (mail["task_id"], page["task_id"])
    assert between["question"] == "Is the amount in cents or in dollars?"
    assert yours["question"] == "Partial refunds too?" and yours["parts"] == [page["task_id"]]
    assert yours["unconfirmed"] == [] and not yours["changed"]
    assert not any("Refund accepted" in e["text"] for e in decided), "a note is never there"
    # Home's row: the team's newest exchange between its parts.
    news = teams_of(runtime)[0]["news"]
    assert (news["kind"], news["from"], news["to"]) == (
        "answered",
        mail["task_id"],
        page["task_id"],
    )


async def test_the_page_gives_views_of_the_parts_home_does_not_list(harness):
    runtime = await harness.connect()
    found = await refund_team(harness, runtime)
    team, api, mail = found["team"], found["api"], found["mail"]
    data = runtime.teams_lite.page(team["id"], need=[api["task_id"], "t0000000"])
    assert [view["id"] for view in data["views"]] == [api["task_id"]]
    assert "deliverables" in data["views"][0] and "decisions" in data["views"][0]
    rows = {row["id"]: row for row in data["tasks"]}
    assert rows[api["task_id"]]["lead"] == "API merged" and rows[api["task_id"]]["started_at"]
    assert "result" not in rows[mail["task_id"]], "only its lead sentence"
    assert "pairs" not in data, "the flow map is gone"
