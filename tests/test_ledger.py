"""The asks ledger (QA10 E151, E152, E158, E159; decision ledger phase B): every ask of the owner
under its subject, one choice asked once and answered once."""

import asyncio
import json

import pytest
from conftest import call_tool, env, settle
from test_ui_text import STATIC, run_js

from chiefofstaff.digest import needs
from chiefofstaff.ledger import embedded_ask, likeness, related, still_waits
from chiefofstaff.models import DraftDecision


async def spawn(runtime, router, title, **extra):
    extra.setdefault("instructions", title)
    result = await call_tool(runtime, "spawn_worker", router, title=title, **extra)
    return json.loads(result.text_result_for_llm)


def one_request(harness, *workers, request="9001"):
    """Workers' tasks made for one owner request (a Router turn's: Tasks.create stamps them)."""
    for worker in workers:
        harness.store.set(f"req:{worker['task_id']}", request)


def held_for(harness, session_id: str) -> str:
    """What waits to ride along with a session's next message."""
    mark = int(harness.store.get(f"sent:{session_id}") or 0)
    return "\n".join(m["meta"]["prompt"] for m in harness.store.prompts(session_id, after=mark))


@pytest.mark.parametrize(
    ("one", "other", "level"),
    [
        ("Which k should the ranker serve?", "Serve k 1.12 instead? It scores better.", "same"),
        ("Do we support SMS login in v1?", "Is there SMS login?", "same"),
        (
            "Round invoices to cents or whole units?",
            "Invoice totals: whole units or cents?",
            "same",
        ),
        ("How should invoice totals be rounded?", "Round invoice totals to cents?", "same"),
        ("Serving k 1.12 from the ranker?", "Which k does the ranker serve?", "same"),
        ("夜港：金币要留到下一晚吗？", "金币留到下一晚吗", "same"),  # noqa: RUF001
        ("Limit login attempts per account?", "Limit signup attempts per account or IP?", "same"),
        ("Deploy the api to staging or prod?", "Which k should the api serve in prod?", "possibly"),
        ("Light the lighthouse at dusk?", "夜港：金币要留到下一晚吗？", ""),  # noqa: RUF001
        ("Which k to serve?", "Fix the flaky login test, then serve the docs with k8s.", ""),
    ],
)
def test_e159_chief_of_staff_tells_which_asks_are_one_choice(one, other, level):
    assert likeness(one, other)[0] == level
    assert likeness(other, one)[0] == level


def test_item1_a_question_relates_to_its_tasks_decision_only_by_its_words():
    assert related("Which tie rule, then?", "Rule A or rule B?")
    assert not related("Log every tie?", "Rule A or rule B?")
    assert not related("Light the lighthouse at dusk?", "夜港：金币要留到下一晚吗？")  # noqa: RUF001


@pytest.mark.parametrize(
    ("report", "ask", "waits"),
    [
        ("Shipped the parser.\n\nWant me to open a PR for it, or keep it local?", True, False),
        ("Done: both variants built.\n\nPlease choose which one we ship.", True, False),
        ("构建完成。\n\n用 A 方案还是 B 方案？", True, False),  # noqa: RUF001
        ("Built and uploaded. I'm blocked: I asked you for three things.", False, True),
        ("构建已上传。差你的三样东西就能提交审核。", False, True),
        ("Fixed the flaky test. All 42 tests pass.", False, False),
        ("Which tests were flaky? The two in auth.\n\nAll fixed now.", False, False),
    ],
)
def test_e152_a_done_report_that_ends_asking_the_owner(report, ask, waits):
    assert bool(embedded_ask(report)) == ask
    assert still_waits(report) == waits


async def test_e159_the_same_choice_in_one_request_joins_one_card_by_itself(harness):
    """E159: whether two asks are one choice was the asking model's call (same_as, distinct):
    reworded, a second card. Within one request Chief of Staff joins them; across requests the
    card says "possibly the same", and Merge makes them one."""
    runtime = await harness.connect()
    api = await spawn(runtime, harness.router_id, "Login API")
    form = await spawn(runtime, harness.router_id, "Login form", why_new="parallel")
    other = await spawn(runtime, harness.router_id, "Admin login", why_new="other request")
    one_request(harness, api, form)
    one_request(harness, other, request="9002")
    options = [{"choice": "Email only"}, {"choice": "Email and SMS"}]
    await call_tool(runtime, "ask_decision", api["session_id"],
                    question="Do we support SMS login in v1?", options=options)  # fmt: skip
    [card] = harness.store.drafts()
    joined = await call_tool(runtime, "ask_decision", form["session_id"],
                             question="Is there SMS login?")  # fmt: skip
    assert joined.result_type == "success" and card["id"] in joined.text_result_for_llm
    assert len(harness.store.drafts()) == 1, "joined by Chief of Staff, not the model"
    assert runtime.tasks.get(form["task_id"])["status"] == "waiting"

    apart = await call_tool(runtime, "ask_decision", other["session_id"],
                            question="Is SMS login supported?", options=options)  # fmt: skip
    assert "Asked the owner" in apart.text_result_for_llm
    cards = harness.store.drafts()
    assert len(cards) == 2, "another request: never joined by itself"
    subjects = {s["id"]: s for s in runtime.snapshot()["subjects"]}
    shared = next(s for s in subjects.values() if len(s["asks"]) == 2)
    assert shared["asked_by"] == "asked by 2 workers"
    lone = next(s for s in subjects.values() if len(s["asks"]) == 1)
    assert [p["id"] for p in lone["possibly"]] == [shared["id"]], "possibly the same: Merge"

    await runtime.ledger.merge(lone["id"], shared["id"])
    assert needs(runtime).count() == 1, "merged: one item under Needs you"
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Email only"))
    await settle(runtime)
    assert not harness.store.drafts(), "one answer closes the other card too"
    for worker in (api, form, other):
        assert "Email only" in harness.session(worker["session_id"]).sent[-1]
        assert runtime.tasks.get(worker["task_id"])["status"] == "running"
    [decision] = runtime.decisions.active()
    assert sorted(runtime.decisions.linked(decision["id"])) == sorted(
        [api["task_id"], form["task_id"], other["task_id"]]
    )


async def test_e151_an_answer_on_a_question_is_the_decision_and_reaches_every_task(harness):
    """E151, the real case: the owner picked k on one worker's card, then another value on
    another worker's question; the decision in force stayed the first, and the workers relying
    on it never heard. Any answer settles its subject: a pick replaces the decision on it, and
    every task relying on it hears, each worker once."""
    runtime = await harness.connect()
    first = await spawn(runtime, harness.router_id, "Serve the ranker")
    second = await spawn(runtime, harness.router_id, "Tune the ranker", why_new="parallel")
    board = await spawn(runtime, harness.router_id, "Ranker dashboard", why_new="parallel")
    one_request(harness, first, second, board)
    await call_tool(runtime, "ask_decision", first["session_id"],
                    question="Which k should the ranker serve?",
                    options=[{"choice": "1.1"}, {"choice": "1.11"}])  # fmt: skip
    [card] = harness.store.drafts()
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="1.1"))
    await settle(runtime)
    [old] = runtime.decisions.active()
    harness.store.execute(
        "INSERT INTO task_decisions(task_id, decision_id) VALUES (?,?)",
        (board["task_id"], old["id"]),
    )

    asked = "Serve k 1.12 instead? The ranker scores better with it."
    held = await call_tool(runtime, "report", second["session_id"], status="question",
                           summary=asked, options=["1.1", "1.12"])  # fmt: skip
    assert held.result_type == "failure" and old["id"] in held.text_result_for_llm
    await call_tool(runtime, "report", second["session_id"], status="question", summary=asked,
                    options=["1.1", "1.12"])  # fmt: skip
    [question] = runtime.asks.session_questions(second["session_id"])
    [subject] = runtime.snapshot()["subjects"]
    assert subject["decided"]["value"] == "1.1", "Needs you: decided before"

    await runtime.task_actions.answer_question(question["id"], "1.12")
    await settle(runtime)
    [now] = runtime.decisions.active()
    assert now["text"] == "Which k should the ranker serve? → 1.12"
    assert runtime.decisions.get(old["id"])["replaced_by"] == now["id"]
    for worker in (first, board):
        sent = harness.session(worker["session_id"]).sent[-1]
        assert (
            'type="decision-changed"' in sent
            and "Now: Which k should the ranker serve? → 1.12" in sent
        )
    sent = harness.session(second["session_id"]).sent
    assert "1.12" in sent[-1] and not any("decision-changed" in text for text in sent), "once"


async def test_e152_every_channel_is_one_ask_and_one_answer_reaches_them_all(harness):
    """E152: a question report, a question card (ask_user) and a Router note on one choice were
    three asks, answered separately. They are one subject: one item under Needs you, the
    Router's note joins it, and one answer reaches every asker where it asked."""
    runtime = await harness.connect()
    api = await spawn(runtime, harness.router_id, "Billing API")
    page = await spawn(runtime, harness.router_id, "Billing page", why_new="parallel")
    one_request(harness, api, page)
    await call_tool(runtime, "report", api["session_id"], status="question",
                    summary="Should invoices round to cents or to whole units?",
                    options=["Cents", "Whole units"])  # fmt: skip
    asking = asyncio.ensure_future(
        runtime.asks.on_user_input(
            {"question": "Round invoice totals to whole units or cents?",
             "choices": ["Cents", "Whole units"]},
            {"sessionId": runtime.copilot_id(page["session_id"])},
        )
    )  # fmt: skip
    await settle(runtime)
    [card] = [d for d in harness.store.drafts() if d["kind"] == "question"]
    note = "Need your call: round invoices to cents or whole units?"
    noted = await call_tool(
        runtime, "notify_owner", harness.router_id, needs_action=True, text=note
    )
    said = noted.text_result_for_llm
    assert said.startswith("Not shown") and "“Billing API”" in said, said
    [subject] = runtime.snapshot()["subjects"]
    assert subject["asked_by"] == "asked by 2 workers + the Router"
    assert needs(runtime).count() == 1, "one item for one choice"
    lines = (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
    assert "asked_by_several" in lines and "one answer reaches them all" in lines

    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Cents"))
    await settle(runtime)
    assert (await asyncio.wait_for(asking, 5))["answer"] == "Cents"
    assert not runtime.asks.session_questions(api["session_id"]), "its question answered too"
    assert "Cents" in harness.session(api["session_id"]).sent[-1]
    [decision] = runtime.decisions.active()
    assert decision["text"].endswith("→ Cents"), "a choice answered: their decision"
    assert sorted(runtime.decisions.linked(decision["id"])) == sorted(
        [api["task_id"], page["task_id"]]
    )
    told = held_for(harness, harness.router_id)
    assert told.count("Cents") == 1, f"the Router hears it once: {told}"
    assert "do not record it again" in told


async def test_e152_a_done_report_that_asks_again_is_held_and_its_ask_kept(harness):
    """Live D6 and E152: a done report that said it was blocked on the owner withdrew the open
    blocked ask; one ending in a choice for the owner lost the ask once accepted. Held once
    with how to ask; reported again, the open ask stays, and one at its end is kept."""
    runtime = await harness.connect()
    app = await spawn(runtime, harness.router_id, "Ship the iPhone build")
    sid = app["session_id"]
    await call_tool(runtime, "report", sid, status="blocked",
                    summary="Need three things from you to submit: the Apple ID, the team id, "
                    "and the review notes.")  # fmt: skip
    done = "Built and uploaded the build. I'm blocked: I asked you for three things."
    held = await call_tool(runtime, "report", sid, status="done", summary=done)
    assert (
        held.result_type == "failure" and "still open under Needs you" in held.text_result_for_llm
    )
    await call_tool(runtime, "report", sid, status="done", summary=done)
    [question] = runtime.asks.session_questions(sid)
    assert question["kind"] == "blocked", "its blocked ask stays under Needs you"

    parser = await spawn(runtime, harness.router_id, "Parser", why_new="other")
    psid = parser["session_id"]
    done = "Shipped the parser.\n\nWant me to open a PR for it, or keep it local?"
    held = await call_tool(runtime, "report", psid, status="done", summary=done)
    assert held.result_type == "failure" and "ask_decision" in held.text_result_for_llm
    await call_tool(runtime, "report", psid, status="done", summary=done)
    [kept] = runtime.asks.session_questions(psid)
    assert kept["text"] == "Want me to open a PR for it, or keep it local?"
    assert runtime.tasks.get(parser["task_id"])["status"] == "review"


async def test_item1_another_question_on_a_decided_task_or_a_new_one_is_asked(harness):
    """Owner reports since 0.1.58: on a task linked to any decision (one recorded in the Router
    turn that gave it out, too), an unrelated question came back "already decided". The guard
    is the question's subject: an unrelated one is a card; the same one, in other words, is
    the decision."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Tie breaker")
    sid = worker["session_id"]
    await call_tool(runtime, "record_decision", harness.router_id, decision="Ties: rule A",
                    tasks=[worker["task_id"]])  # fmt: skip
    [decision] = runtime.decisions.active()
    other = await call_tool(runtime, "ask_decision", sid, question="Log every game to a file?")
    assert "Asked the owner" in other.text_result_for_llm, other.text_result_for_llm
    [card] = harness.store.drafts()
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Yes"))
    await settle(runtime)
    same = await call_tool(runtime, "ask_decision", sid, question="Which tie rule, rule A?")
    assert decision["id"] in same.text_result_for_llm and not harness.store.drafts()


async def test_item2_a_task_given_in_the_turn_of_a_decision_asks_another_question(harness):
    """ui_edges: a decision recorded in a Router turn was inherited by the task it gave out in
    that turn (it came with the owner's message), and its first unrelated question came back
    "already decided". It still relies on that decision; another question is a card."""
    runtime = await harness.connect()
    harness.session(harness.router_id).emit(
        "user.message", content=env("owner", "message", "Ties use rule A. Build the scoreboard.")
    )
    await settle(runtime)
    await call_tool(runtime, "record_decision", harness.router_id, decision="Ties: rule A")
    board = await spawn(runtime, harness.router_id, "Scoreboard")
    [decision] = runtime.decisions.active()
    assert runtime.decisions.linked(decision["id"]) == [board["task_id"]], "it relies on it"
    asked = await call_tool(runtime, "ask_decision", board["session_id"],
                            question="Show the scoreboard in dark mode?")  # fmt: skip
    assert "Asked the owner" in asked.text_result_for_llm, asked.text_result_for_llm


async def test_the_routers_answer_for_the_owner_reaches_every_worker_that_asked(harness):
    """The Router relays the owner's answer to one worker (answers_question): the others that
    asked the same in its request hear it too, their questions close."""
    runtime = await harness.connect()
    one = await spawn(runtime, harness.router_id, "Docs site")
    two = await spawn(runtime, harness.router_id, "Docs search", why_new="parallel")
    one_request(harness, one, two)
    await call_tool(runtime, "report", one["session_id"], status="question",
                    summary="Which docs domain: docs.example.org or example.org/docs?")  # fmt: skip
    blocked = "Blocked: which docs domain, docs.example.org or example.org/docs?"
    await call_tool(runtime, "report", two["session_id"], status="blocked", summary=blocked)
    assert needs(runtime).count() == 1
    await call_tool(runtime, "message_session", harness.router_id, session_id=one["session_id"],
                    task="note", text="The owner said docs.example.org.",
                    answers_question=True)  # fmt: skip
    await settle(runtime)
    assert not runtime.asks.session_questions(two["session_id"])
    assert "The Router answered for the owner" in harness.session(two["session_id"]).sent[-1]
    assert runtime.tasks.get(two["task_id"])["status"] == "running"


async def test_a_worker_gets_the_decisions_in_force_it_was_not_given_once(harness):
    """Workers keep up: the decisions its open tasks rely on that it was not told ride with its
    next message, once; none (or one its message names): no line."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Billing API")
    sid = worker["session_id"]
    plain = "Go on with the invoices."
    await call_tool(runtime, "message_session", harness.router_id, session_id=sid, task="note",
                    text=plain)  # fmt: skip
    assert "Decisions in force" not in harness.session(sid).sent[-1]
    decision = await runtime.decisions.record("Round to cents", [worker["task_id"]], [], "owner")
    await call_tool(runtime, "message_session", harness.router_id, session_id=sid, task="note",
                    text="And the totals.")  # fmt: skip
    sent = harness.session(sid).sent[-1]
    assert "Decisions in force for your tasks" in sent and decision["decision"]["id"] in sent
    await call_tool(runtime, "message_session", harness.router_id, session_id=sid, task="note",
                    text="And the tax line.")  # fmt: skip
    assert "Decisions in force" not in harness.session(sid).sent[-1], "once"


def test_e158_home_folds_a_subjects_asks_into_one_item():
    """Home's Needs you (inbox.js) folds as the server counts (Ledger.folded): the richest ask
    shown, the others in it."""
    subjects = [
        {
            "id": "sub1",
            "asked_by": "asked by 2 workers + the Router",
            "asks": [
                {"id": "a1", "channel": "question", "keys": ["task:t1", "question:s1:t1"]},
                {"id": "a2", "channel": "ask_user", "keys": ["draft:d2"]},
                {"id": "a3", "channel": "notify", "keys": ["update:7"]},
            ],
        }
    ]
    present = ["task:t1", "draft:d2", "update:7", "draft:other"]
    found = run_js(
        f"const m = await import({json.dumps((STATIC / 'subjects.js').as_uri())});\n"
        f"const {{ folded, shown }} = m.folding({json.dumps(subjects)}, "
        f"new Set({json.dumps(present)}));\n"
        "const others = [...shown.values()][0].others.map((a) => a.id);\n"
        "return { folded: [...folded.entries()], shown: [...shown.keys()], others };"
    )
    assert found == {
        "folded": [["task:t1", "draft:d2"], ["update:7", "draft:d2"]],
        "shown": ["draft:d2"],
        "others": ["a1", "a3"],
    }


async def test_a_store_from_before_files_its_open_asks_and_decisions_once(harness):
    """Opened by this version, a store from before has its decisions in force and its open
    cards and questions under subjects, once; a worker asking one of them joins it."""
    runtime = await harness.connect()
    api = await spawn(runtime, harness.router_id, "Login API")
    page = await spawn(runtime, harness.router_id, "Login page", why_new="parallel")
    one_request(harness, api, page)
    await call_tool(runtime, "record_decision", harness.router_id, decision="Login v1: email only",
                    tasks=[api["task_id"]])  # fmt: skip
    await call_tool(runtime, "report", page["session_id"], status="question",
                    summary="Which font for the login page: Inter or system?")  # fmt: skip
    for table in ("asks", "subjects"):
        harness.store.execute(f"DELETE FROM {table}")  # noqa: S608  (as before the ledger)
    harness.store.delete("migrated:ledger")
    runtime.ledger.migrate()
    runtime.ledger.migrate()
    assert len(harness.store.all("SELECT * FROM subjects")) == 2
    assert len(harness.store.all("SELECT * FROM asks")) == 1
    await call_tool(runtime, "report", api["session_id"], status="question",
                    summary="Inter or the system font on the login page?")  # fmt: skip
    [subject] = runtime.snapshot()["subjects"]
    assert subject["asked_by"] == "asked by 2 workers"
