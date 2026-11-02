import asyncio
import json
import time

import pytest
from conftest import call_tool, env, settle

from chiefofstaff.asks import form_values
from chiefofstaff.models import DraftDecision, Settings
from chiefofstaff.store import Store


async def spawn(runtime, router, title, **extra):
    result = await call_tool(
        runtime, "spawn_worker", router, title=title, instructions=title, why_new="test", **extra
    )
    return json.loads(result.text_result_for_llm)["session_id"]


async def pending(harness, kind):
    for _ in range(20):
        await asyncio.sleep(0)
        drafts = [d for d in harness.store.drafts() if d["kind"] == kind]
        if drafts:
            return drafts[-1]
    raise AssertionError(f"no pending {kind}")


async def test_a_worker_asks_the_owner_and_waits_for_the_answer(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Pick a color")
    request = {"question": "Red or blue?", "choices": ["red", "blue"], "allowFreeform": True}
    answer = asyncio.create_task(runtime.asks.on_user_input(request, {"sessionId": worker}))
    draft = await pending(harness, "question")
    assert draft["target"]["choices"] == ["red", "blue"] and draft["session_id"] == worker
    assert runtime.view(harness.store.session(worker))["status"] == "waiting"
    assert harness.store.messages(worker)[-1]["meta"]["label"] == "Question"

    with pytest.raises(ValueError, match="Write an answer"):
        await runtime.drafts.resolve(draft["id"], DraftDecision(action="answer", text=" "))
    await runtime.drafts.resolve(draft["id"], DraftDecision(action="answer", text="blue"))
    assert await answer == {"answer": "blue", "wasFreeform": False}
    assert runtime.view(harness.store.session(worker))["status"] == "working"
    assert harness.store.draft(draft["id"])["status"] == "answered"


async def test_forms_and_links_from_mcp_tools(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "File a ticket")
    schema = {
        "type": "object",
        "properties": {"key": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["key"],
    }
    context = {
        "session_id": worker,
        "message": "Which project?",
        "requestedSchema": schema,
        "mode": "form",
        "elicitationSource": "jira",
    }
    form = asyncio.create_task(runtime.asks.on_elicitation(context))
    draft = await pending(harness, "form")
    assert draft["text"] == "jira: Which project?"
    with pytest.raises(ValueError, match="key is required"):
        await runtime.drafts.resolve(draft["id"], DraftDecision(action="answer", content={}))
    decision = DraftDecision(action="answer", content={"key": "ABC", "count": "3"})
    await runtime.drafts.resolve(draft["id"], decision)
    assert await form == {"action": "accept", "content": {"key": "ABC", "count": 3}}
    assert harness.store.draft(draft["id"])["outcome"] == "Submitted", "values stay private"

    bad = {"session_id": worker, "message": "Go", "mode": "url", "url": "javascript:alert(1)"}
    assert await runtime.asks.on_elicitation(bad) == {"action": "decline"}
    link = {**bad, "url": "https://example.com/consent"}
    opened = asyncio.create_task(runtime.asks.on_elicitation(link))
    draft = await pending(harness, "link")
    await runtime.drafts.resolve(draft["id"], DraftDecision(action="reject"))
    assert await opened == {"action": "decline"}


async def test_mcp_sign_in_asks_once_and_closes_by_itself(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Read the launch doc")
    session = harness.session(worker)
    request = {"requestId": "r1", "serverName": "docs", "serverUrl": "https://docs.example.com"}
    signing = asyncio.create_task(runtime.asks.on_mcp_auth(request, {"sessionId": worker}))
    draft = await pending(harness, "signin")
    assert draft["target"] == {"server": "docs", "url": session.login_url}
    assert session.sent[-1].startswith(env("system", "system", "docs needs sign-in")[:-11])
    assert session.modes[-1] == "immediate", "a working worker hears it in its current turn"
    session.emit("session.mcp_server_tools_loaded", server_name="docs", tool_count=1)
    assert await signing == {"kind": "cancelled"}
    await settle(runtime)
    assert harness.store.draft(draft["id"])["status"] == "answered"
    assert session.sent[-1] == env(
        "system", "system", "You are signed in to docs; its tools are available now."
    )

    session.login_url = ""  # Copilot kept the credentials: nothing to ask.
    assert await runtime.asks.on_mcp_auth(request, {"sessionId": worker}) == {"kind": "cancelled"}
    assert not [d for d in harness.store.drafts() if d["kind"] == "signin"]
    router = harness.session(harness.router_id)
    assert await runtime.asks.on_mcp_auth(request, {"sessionId": harness.router_id})
    assert router.logins == [], "only workers sign in"


async def test_requests_do_not_survive_a_restart(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Ask")
    asking = asyncio.create_task(
        runtime.asks.on_user_input({"question": "Ok?"}, {"sessionId": worker})
    )
    draft = await pending(harness, "question")
    runtime.asks.requests.clear()  # As after a restart: nobody waits for this answer any more.
    runtime.asks.expire_requests()
    assert harness.store.draft(draft["id"])["status"] == "expired"
    with pytest.raises(ValueError, match="already handled"):
        await runtime.drafts.resolve(draft["id"], DraftDecision(action="answer", text="yes"))
    asking.cancel()


async def test_environment_notes_are_shared(harness):
    runtime = await harness.connect()
    router = harness.router_id
    setup = await spawn(runtime, router, "Set up docs access")
    note = "company MCP server 'docs' (read_doc); signed in via its card on Nov 1"
    await call_tool(runtime, "record_setup", setup, topic="Google Docs", note=note)
    assert runtime.asks.setup_notes()["Google Docs"]["note"] == note
    assert harness.store.messages(router)[-1]["text"].startswith("Environment note “Google Docs”")
    later = await spawn(runtime, router, "Summarize the plan")
    prompt = harness.session(later).options["system_message"]["content"]
    assert "<environment_notes>" in prompt and f"- Google Docs: {note}" in prompt

    reader = await spawn(runtime, router, "Answer Alice", slack=True)
    harness.store.execute("UPDATE sessions SET slack=1 WHERE id=?", (reader,))
    refused = await call_tool(runtime, "record_setup", reader, topic="x", note="y")
    assert "cannot record" in refused.text_result_for_llm
    shown = f"Google Docs: {note}"
    await call_tool(runtime, "record_setup", setup, topic="Figma", note="desktop app only")
    await runtime.asks.edit_setup_notes(
        "Google Docs: use the docs MCP\nnot a note\nJira: via jira CLI", shown
    )
    notes = runtime.asks.setup_notes()
    assert notes["Google Docs"]["note"] == "use the docs MCP" and notes["Jira"]["by"] == "owner"
    assert "Figma" in notes, "a note recorded while the owner edited is kept"
    await runtime.asks.edit_setup_notes(
        "Jira: via jira CLI", "Google Docs: use the docs MCP\nJira: via jira CLI"
    )
    assert sorted(runtime.asks.setup_notes()) == ["Figma", "Jira"]


async def test_sessions_ask_each_other(harness):
    runtime = await harness.connect()
    router = harness.router_id
    expert = await spawn(runtime, router, "Billing research")
    asker = await spawn(runtime, router, "Invoice bug")
    found = json.loads(
        (await call_tool(runtime, "find_sessions", asker, query="billing")).text_result_for_llm
    )
    assert [m["session_id"] for m in found["matches"]] == [expert]
    assert found["router_id"] == router

    result = await call_tool(
        runtime, "ask_session", asker, session_id=expert, question="Which table holds invoices?"
    )
    question_id = result.text_result_for_llm.split("(")[1].split(")")[0]
    assert harness.session(expert).sent[-1] == env(
        f"worker:{asker[:8]}",
        "question",
        "Which table holds invoices?",
        question=question_id,
        title="Invoice bug",
    )
    assert harness.store.messages(asker)[-1]["kind"] == "ask"
    wrong = await call_tool(runtime, "answer_session", asker, question_id=question_id, answer="x")
    assert "No such question" in wrong.text_result_for_llm
    await call_tool(
        runtime, "answer_session", expert, question_id=question_id, answer="billing.inv"
    )
    assert harness.session(asker).sent[-1] == env(
        f"worker:{expert[:8]}",
        "answer",
        "billing.inv",
        question=question_id,
        title="Billing research",
    )
    again = await call_tool(runtime, "answer_session", expert, question_id=question_id, answer="y")
    assert "already" in again.text_result_for_llm

    await call_tool(runtime, "ask_session", asker, session_id=router, question="Who owns billing?")
    assert (
        harness.session(router)
        .sent[-1]
        .startswith(f'<message from="worker:{asker[:8]}" type="question"')
    )
    reader = await spawn(runtime, router, "Slack reader", slack=True)
    harness.store.execute("UPDATE sessions SET slack=1 WHERE id=?", (reader,))
    refused = await call_tool(runtime, "ask_session", asker, session_id=reader, question="?")
    assert "Slack access differs" in refused.text_result_for_llm


async def test_messages_during_a_card_do_not_leave_the_worker_waiting(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Deploy")
    session = harness.session(worker)
    session.emit("session.idle")
    await settle(runtime)
    asking = asyncio.create_task(
        runtime.asks.on_user_input({"question": "Ship?"}, {"sessionId": worker})
    )
    draft = await pending(harness, "question")
    await runtime.owner_send(worker, "Also update the changelog")
    await runtime.drafts.resolve(draft["id"], DraftDecision(action="answer", text="yes"))
    await asking
    session.emit("session.idle")
    await settle(runtime)
    assert runtime.view(harness.store.session(worker))["status"] != "waiting"
    assert await runtime.watchdog.check() is not None


async def test_a_worker_resumed_during_startup_can_sign_in(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Read the launch doc")
    runtime.status["state"] = "starting"  # Workers resume before the runtime is ready.
    request = {"requestId": "r1", "serverName": "docs", "serverUrl": "https://docs.example.com"}
    signing = asyncio.create_task(runtime.asks.on_mcp_auth(request, {"sessionId": worker}))
    draft = await pending(harness, "signin")
    assert draft["target"]["url"] == harness.session(worker).login_url
    signing.cancel()


async def test_closing_a_worker_ends_its_cards_and_frees_the_sign_in(harness):
    runtime = await harness.connect()
    first = await spawn(runtime, harness.router_id, "Read docs one")
    second = await spawn(runtime, harness.router_id, "Read docs two")
    request = {"requestId": "r", "serverName": "docs", "serverUrl": "https://docs.example.com"}
    one = asyncio.create_task(runtime.asks.on_mcp_auth(request, {"sessionId": first}))
    card = await pending(harness, "signin")
    two = asyncio.create_task(runtime.asks.on_mcp_auth(request, {"sessionId": second}))
    await asyncio.sleep(0)
    assert harness.session(second).logins == [], "one card per server"
    await runtime.workers.close(first)
    assert await one == {"kind": "cancelled"}
    assert harness.store.draft(card["id"])["status"] == "expired"
    card = await pending(harness, "signin")
    assert card["session_id"] == second, "closing is not 'not now': the next session asks"
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer"))
    assert await two == {"kind": "cancelled"}
    await settle(runtime)
    assert "You are signed in to docs" in harness.session(second).sent[-1]

    third = await spawn(runtime, harness.router_id, "Read docs three")
    asking = asyncio.create_task(runtime.asks.on_mcp_auth(request, {"sessionId": third}))
    card = await pending(harness, "signin")
    await runtime.drafts.resolve(card["id"], DraftDecision(action="reject"))
    assert await asking == {"kind": "cancelled"}
    fourth = await spawn(runtime, harness.router_id, "Read docs four")
    assert await runtime.asks.on_mcp_auth(request, {"sessionId": fourth}) == {"kind": "cancelled"}
    assert harness.session(fourth).logins == [], "after 'not now', others do not ask for a while"


async def test_a_failed_question_does_not_use_up_a_slot(harness, tmp_path):
    runtime = await harness.connect()
    asker = await spawn(runtime, harness.router_id, "Invoice bug")
    gone = await spawn(runtime, harness.router_id, "Old research")
    await runtime.workers.close(gone)
    harness.store.update_session(gone, cwd=str(tmp_path / "deleted"))
    for _ in range(6):
        failed = await call_tool(runtime, "ask_session", asker, session_id=gone, question="?")
        assert "directory is gone" in failed.text_result_for_llm
    assert runtime.asks.open_questions(asker) == []
    assert not harness.store.all("SELECT key FROM kv WHERE key LIKE 'question:%'")


async def test_a_broken_scratch_folder_is_refused_and_does_not_break_the_router(harness, tmp_path):
    runtime = await harness.connect()
    file = tmp_path / "a-file"
    file.write_text("x")
    with pytest.raises(ValueError, match="cannot be used"):
        runtime.workspaces.check_scratch(Settings(scratch_directory=str(file)))
    harness.store.save_settings(Settings(scratch_directory=str(file / "inside")))
    await runtime.owner_send(harness.router_id, "What is running?")
    refused = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="Research", instructions="r"
    )
    assert "Fix the workspaces folder in Settings" in refused.text_result_for_llm


def test_form_choices_and_lists():
    schema = {
        "properties": {
            "tags": {
                "type": "array",
                "items": {"anyOf": [{"const": "a", "title": "A"}, {"const": "b", "title": "B"}]},
            },
            "size": {"type": "string", "oneOf": [{"const": "s"}, {"const": "m"}]},
        }
    }
    assert form_values(schema, {"tags": "a", "size": "m"}) == {"tags": ["a"], "size": "m"}
    with pytest.raises(ValueError, match="must be one of"):
        form_values(schema, {"tags": ["a", "z"]})


async def test_questions_expire_or_end_with_their_session(harness):
    runtime = await harness.connect()
    router = harness.router_id
    asker = await spawn(runtime, router, "Invoice bug")
    billing = await spawn(runtime, router, "Billing research")
    audit = await spawn(runtime, router, "Audit research")

    async def ask(target, text):
        result = await call_tool(runtime, "ask_session", asker, session_id=target, question=text)
        return result.text_result_for_llm.split("(")[1].split(")")[0]

    first = await ask(billing, "Which table?")
    second = await ask(audit, "Which log?")
    assert [q["id"] for q in runtime.asks.open_questions(asker)] == [first, second]
    await runtime.workers.close(billing)
    assert runtime.asks.question(first)["status"] == "cancelled"
    assert "will not be answered: that session closed" in harness.session(asker).sent[-1]
    assert await runtime.asks.expire_questions(time.time() + 3 * 3600) == [second]
    assert runtime.asks.question(second)["status"] == "expired"
    assert "got no answer in 2 hours" in harness.session(asker).sent[-1]
    assert runtime.asks.open_questions(asker) == []


def test_old_questions_move_into_their_table(tmp_path):
    store = Store(tmp_path)
    store.set("question:qold", json.dumps({"asker": "a", "target": "b", "text": "Which?"}))
    store.set("asking:a", "1")
    moved = Store(tmp_path)
    assert moved.one("SELECT status, text FROM questions WHERE id='qold'") == {
        "status": "open",
        "text": "Which?",
    }
    assert not moved.all("SELECT key FROM kv WHERE key LIKE 'question:%' OR key LIKE 'asking:%'")
