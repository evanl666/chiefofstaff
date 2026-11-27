"""How each owner input came in (ingress.py): recorded on every owner message and each decision
card answered, from the page or an away answer; never in a prompt, never drawn."""

import json

from conftest import Harness, call_tool, said, settle
from test_web import ORIGIN, TOKEN, app_client, sign_in  # noqa: F401  (app_client: a fixture)

from chiefofstaff import ingress
from chiefofstaff.models import DraftDecision, Settings


def owners(store, session_id):
    return [m for m in store.messages(session_id) if m["kind"] == "owner"]


def test_what_the_owner_sends_in_the_page_is_marked_as_from_the_page(app_client):  # noqa: F811
    sign_in(app_client)
    router = app_client.get("/api/state").json()["router_id"]
    sent = app_client.post(
        f"/api/sessions/{router}/messages", json={"text": "Ship the release notes"}, headers=ORIGIN
    )
    assert sent.json()["meta"]["ingress"] == "page"
    assert app_client.fake.sessions[router].sent == [said("Ship the release notes")], (
        "the Router's prompt is as before"
    )
    store = app_client.app.state.store
    assert [m["meta"]["ingress"] for m in owners(store, router)] == ["page"]
    assert ingress.current() == "", "only for that request"


async def test_an_away_answer_is_marked_away_and_input_from_nowhere_has_none(tmp_path):
    harness = Harness(tmp_path / "data", Settings(away_notify=True))
    runtime = await harness.connect()
    connector = runtime.connector
    await runtime.owner_send(harness.router_id, "Write the port into config.txt")
    router = harness.session(harness.router_id)
    router.emit("assistant.message", content="Should it be 8080 or 9090?", tool_requests=None)
    router.emit("session.idle")
    await settle(runtime)
    [item] = connector.waiting_items()
    assert await connector.apply(item, "ABCDE 9090", "ABCDE") == "sent to the Router"
    first, answer = owners(harness.store, harness.router_id)
    assert "ingress" not in first["meta"], "not through any channel: data"
    assert answer["text"] == "9090" and answer["meta"]["ingress"] == "away"
    assert "ingress" not in router.sent[-1]


async def test_a_decision_cards_answer_records_how_it_came(tmp_path):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    options = [{"choice": "A", "effect": "a"}, {"choice": "B", "effect": "b"}]
    # Two workers' cards (a worker's own second ask on its task waits for its first: ledger.py).
    for title, question in (("Store", "Which store?"), ("Port", "Which port?")):
        result = await call_tool(
            runtime, "spawn_worker", harness.router_id, title=title, instructions=title,
            why_new="test",
        )  # fmt: skip
        worker = json.loads(result.text_result_for_llm)
        await call_tool(
            runtime, "ask_decision", worker["session_id"], question=question, options=options
        )
    first, second = (d for d in harness.store.drafts() if d["kind"] == "decision")
    with ingress.through(ingress.PAGE):
        await runtime.drafts.resolve(first["id"], DraftDecision(action="answer", text="A"))
    await runtime.drafts.resolve(second["id"], DraftDecision(action="answer", text="B"))
    assert harness.store.get(f"ingress:{first['id']}") == "page"
    assert harness.store.get(f"ingress:{second['id']}") is None
    assert harness.store.keys("ingress:") == [f"ingress:{first['id']}"]
