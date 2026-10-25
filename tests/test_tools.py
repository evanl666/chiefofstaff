import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from work_buddy.agent import CopilotAgent
from work_buddy.engine import Engine
from work_buddy.models import AgentText, Settings
from work_buddy.store import Store
from work_buddy.tools import allowed_tools
from work_buddy.web import create_app


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / "data")
    engine = Engine(store)
    task = store.create_daily_brief("Help me with my Slack work and create agents as needed.")
    store.update_task(task, status="running", tool_driven=1)
    token, lease = engine.tools.grant(task)
    yield engine, task, token, lease
    store.close()


async def test_lead_spawns_more_than_one_fixed_batch_and_reuses_retries(runtime):
    engine, task_id, token, lease = runtime
    ids = []
    for index in range(6):
        result = await engine.tools.call(
            token,
            "spawn_agent",
            {
                "title": f"Investigation {index}",
                "instructions": f"Investigate area {index}.",
            },
        )
        ids.append(result["agent_id"])
    assert len(set(ids)) == 6
    repeated = await engine.tools.call(
        token,
        "spawn_agent",
        {
            "title": "Investigation 0",
            "instructions": "Investigate area 0.",
        },
    )
    assert repeated["agent_id"] == ids[0] and repeated["reused"]
    assert len(engine.store.children(task_id)) == 6
    assert all(child["role"] == "worker" for child in engine.store.children(task_id))


async def test_army_has_a_durable_total_limit(runtime):
    engine, task_id, token, _ = runtime
    for index in range(32):
        await engine.tools.call(
            token,
            "spawn_agent",
            {
                "title": f"Agent {index}",
                "instructions": f"Task {index}",
            },
        )
    with pytest.raises(ValueError, match="budget reached"):
        await engine.tools.call(token, "spawn_agent", {"title": "Extra", "instructions": "Extra"})
    assert len(engine.store.children(task_id)) == 32


async def test_scoped_tools_expire_and_workers_cannot_spawn_or_write(runtime):
    engine, task_id, token, lease = runtime
    child = engine.store.create_task("Worker", "Read", parent_id=task_id)
    engine.store.update_task(child, status="running")
    child_token, _ = engine.tools.grant(child)
    assert "spawn_agent" not in allowed_tools(engine.store.task(child))
    with pytest.raises(PermissionError):
        await engine.tools.call(child_token, "spawn_agent", {"title": "x", "instructions": "x"})
    with pytest.raises(PermissionError):
        await engine.tools.call(
            child_token,
            "slack_reply",
            {
                "channel": "C1",
                "thread_ts": "100",
                "text": "Unauthorized",
            },
        )
    engine.tools.revoke(token)
    with pytest.raises(PermissionError, match="expired or invalid"):
        await engine.tools.call(token, "buddy_status", {})


async def test_ownership_and_artifacts_preserve_code_whitespace(runtime):
    engine, task_id, token, lease = runtime
    child = engine.store.create_task("Worker", "Read", parent_id=task_id)
    engine.store.update_task(child, status="running")
    child_token, _ = engine.tools.grant(child)
    await engine.tools.call(
        child_token,
        "write_artifact",
        {
            "name": "draft.py",
            "content": "    indented\n\n",
        },
    )
    result = await engine.tools.call(
        token,
        "read_artifact",
        {
            "agent_id": child,
            "name": "draft.py",
        },
    )
    assert result["content"] == "    indented\n\n"
    other = engine.store.create_task("Another Lead", "Other")
    with pytest.raises(PermissionError, match="another task"):
        await engine.tools.call(token, "get_agent", {"agent_id": other})
    with pytest.raises(ValueError):
        await engine.tools.call(
            token,
            "read_artifact",
            {
                "agent_id": child,
                "name": "../private",
            },
        )


async def test_native_decision_is_idempotent_and_resumes_after_owner_choice(runtime):
    engine, task_id, token, lease = runtime
    params = {"question": "Which approach?", "options": ["A", "B"]}
    first = await engine.tools.call(token, "request_decision", params)
    repeated = await engine.tools.call(token, "request_decision", params)
    assert first["approval_id"] == repeated["approval_id"]
    assert first["executed"] is False
    await engine.finish_tool_run(task_id, AgentText(message="Waiting for your choice."), lease)
    assert engine.store.task(task_id)["status"] == "waiting_approval"
    await engine.resolve(first["approval_id"], "A", "Prefer compatibility.")
    assert engine.store.task(task_id)["status"] == "queued"
    assert engine.store.task(task_id)["needs_resume"] == 1
    answer = await engine.tools.call(token, "request_decision", params)
    assert answer["status"] == "approved" and answer["choice"] == "A"


async def test_approval_during_live_tool_turn_does_not_lose_resume(runtime):
    engine, task_id, token, lease = runtime
    engine.active[task_id] = asyncio.current_task()
    result = await engine.tools.call(
        token,
        "request_decision",
        {
            "question": "Pick?",
            "options": ["Yes", "No"],
        },
    )
    await engine.resolve(result["approval_id"], "Yes", "")
    assert engine.store.task(task_id)["status"] == "running"
    await engine.finish_tool_run(task_id, AgentText(message="Decision requested."), lease)
    assert engine.store.task(task_id)["status"] == "queued"
    engine.active.clear()


async def test_tool_driven_profile_is_saved_but_not_activated_without_confirmation(runtime):
    engine, _, _, _ = runtime
    task_id = engine.store.create_task("Profile", "Learn my work", kind="distill")
    engine.store.update_task(task_id, status="running", tool_driven=1)
    token, lease = engine.tools.grant(task_id)
    result = await engine.tools.call(
        token,
        "memory_candidate",
        {
            "text": "Prefer evidence-backed replies.",
            "category": "preference",
            "scope": "general",
            "evidence": "Owner explicitly requested citations.",
        },
    )
    assert not engine.store.all("SELECT * FROM memories")
    assert engine.store.approval(result["approval_id"])["payload"]["evidence"]
    await engine.finish_tool_run(task_id, AgentText(message="Profile prepared."), lease)
    await engine.resolve(result["approval_id"], "Approve", "")
    export = json.loads((engine.store.directory / "memory/confirmed.json").read_text())
    assert export["memories"][0]["text"] == "Prefer evidence-backed replies."
    assert engine.store.task(task_id)["status"] == "completed"


class SlackClient:
    async def conversations_history(self, **kwargs):
        return {"messages": [{"user": "UTEAM", "text": "Is it ready?", "ts": "100"}]}

    async def conversations_replies(self, **kwargs):
        return {"messages": [{"user": "UTEAM", "text": "Compatibility check pending", "ts": "100"}]}


async def test_slack_read_and_reply_are_agent_tools_with_scope_and_approval(runtime):
    engine, task_id, token, lease = runtime
    engine.store.save_settings(
        Settings(
            slack_enabled=True,
            slack_owner="UOWNER",
            slack_channels=["CALLOWED"],
        )
    )
    notifications = []

    async def notify(approval_id):
        notifications.append(approval_id)

    engine.slack = SimpleNamespace(client=SlackClient(), notify_approval=notify)
    with pytest.raises(PermissionError):
        await engine.tools.call(token, "slack_recent_messages", {"channel": "CPRIVATE"})
    with pytest.raises(ValueError, match="Read this thread"):
        await engine.tools.call(
            token,
            "slack_reply",
            {
                "channel": "CALLOWED",
                "thread_ts": "100",
                "text": "Reply",
            },
        )
    history = await engine.tools.call(token, "slack_recent_messages", {"channel": "CALLOWED"})
    assert history["messages"][0]["text"] == "Is it ready?"
    thread = await engine.tools.call(
        token,
        "slack_read_thread",
        {
            "channel": "CALLOWED",
            "thread_ts": "100",
        },
    )
    assert "pending" in thread["messages"][0]["text"]
    args = {"channel": "CALLOWED", "thread_ts": "100", "text": "The check is still pending."}
    reply = await engine.tools.call(token, "slack_reply", args)
    again = await engine.tools.call(token, "slack_reply", args)
    assert reply["executed"] is False and reply["status"] == "pending"
    assert len(notifications) == 1
    assert again["approval_id"] == reply["approval_id"]
    assert engine.store.approval(reply["approval_id"])["payload"] == args


async def test_children_finish_wakes_lead_and_unresolved_errors_are_not_success(runtime):
    engine, task_id, token, lease = runtime
    response = await engine.tools.call(
        token,
        "spawn_agent",
        {
            "title": "Research",
            "instructions": "Check evidence",
        },
    )
    child = response["agent_id"]
    await engine.finish_tool_run(task_id, AgentText(message="Delegated."), lease)
    assert engine.store.task(task_id)["status"] == "waiting_workers"
    engine.store.update_task(child, status="failed", error="Tool unavailable")
    settings = engine.store.settings()
    settings.paused = True
    engine.store.save_settings(settings)
    await engine.tick()
    assert engine.store.task(task_id)["status"] == "queued"
    with pytest.raises(PermissionError):
        await engine.tools.call(token, "settings", {})
    with pytest.raises(RuntimeError, match="Unresolved"):
        await engine.finish_tool_run(task_id, AgentText(message="Done."), lease)


async def test_completed_child_results_are_available_without_polling(runtime):
    engine, task_id, token, lease = runtime
    child = engine.store.create_task("Child", "Investigate", parent_id=task_id)
    engine.store.update_task(child, status="completed", result="Evidence found.")
    result = await engine.tools.call(token, "wait_for_agents", {"agent_ids": [child]})
    assert result["agents"][0]["result"] == "Evidence found."
    assert child in lease.seen_agents
    await engine.finish_tool_run(task_id, AgentText(message="Evidence checked."), lease)
    assert engine.store.task(task_id)["status"] == "completed"


async def test_lead_can_retry_a_failed_child_explicitly(runtime):
    engine, task_id, token, _ = runtime
    child = engine.store.create_task("Worker", "Read", parent_id=task_id)
    engine.store.update_task(child, status="failed", error="Temporary failure", tool_driven=1)
    result = await engine.tools.call(token, "retry_agent", {"agent_id": child})
    assert result["status"] == "queued"
    assert engine.store.task(child)["error"] == ""


def test_resumed_context_contains_exact_previous_reply(runtime):
    engine, task_id, _, _ = runtime
    payload = {"channel": "C1", "thread_ts": "100", "text": "Exact previously approved reply."}
    approval = engine.store.create_approval(
        task_id,
        "slack",
        "Send?",
        ["Approve", "Reject"],
        payload,
    )
    engine.store.execute("UPDATE approvals SET status='approved' WHERE id=?", (approval,))
    action = engine.context(engine.store.task(task_id))["previous_actions"][0]
    assert action["payload"] == payload and action["status"] == "approved"


def test_agent_tools_do_not_accept_browser_cookie_or_stale_capability(tmp_path):
    app = create_app(tmp_path / "data", "owner-launch-token")
    engine = app.state.engine
    engine.store.save_settings(Settings(backend="demo", paused=True))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/auth",
            json={"token": "owner-launch-token"},
            headers={"Origin": "http://127.0.0.1:8765"},
        )
        assert (
            client.post(
                "/internal/tools",
                json={
                    "name": "buddy_status",
                    "arguments": {},
                },
            ).status_code
            == 403
        )
        task = engine.store.create_task("Lead", "Work")
        engine.store.update_task(task, status="running")
        token, _ = engine.tools.grant(task)
        headers = {"Authorization": f"Bearer {token}"}
        request = {"name": "buddy_status", "arguments": {}}
        assert client.post("/internal/tools", json=request, headers=headers).status_code == 200
        assert (
            client.post(
                "/internal/tools",
                json=request,
                headers={
                    **headers,
                    "Origin": "http://127.0.0.1:8765",
                },
            ).status_code
            == 403
        )
        engine.tools.revoke(token)
        assert client.post("/internal/tools", json=request, headers=headers).status_code == 403


def test_native_cli_exposes_only_this_role_mcp_tools(monkeypatch):
    monkeypatch.setattr("work_buddy.agent.shutil.which", lambda name: "/fake/copilot")
    task = {"role": "worker", "kind": "work"}
    names = allowed_tools(task)
    command = CopilotAgent.command(Settings(), names)
    assert "--allow-tool=work-buddy" in command
    available = next(item for item in command if item.startswith("--available-tools="))
    assert "mcp:work-buddy-write_artifact" in available
    assert "mcp:work-buddy-spawn_agent" not in available


def test_artifacts_are_downloads_behind_owner_auth(tmp_path):
    app = create_app(tmp_path / "data", "launch-token")
    app.state.store.save_settings(Settings(backend="demo", paused=True))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        task = app.state.store.create_task("Draft", "Prepare code")
        folder = app.state.engine.tools.artifact_folder(task)
        folder.mkdir(parents=True)
        (folder / "draft.py").write_text("print('draft')\n")
        url = f"/api/tasks/{task}/artifacts/draft.py"
        assert client.get(url).status_code == 401
        client.post(
            "/api/auth", json={"token": "launch-token"}, headers={"Origin": "http://127.0.0.1:8765"}
        )
        response = client.get(url)
        assert response.status_code == 200 and response.text == "print('draft')\n"
        assert "attachment" in response.headers["content-disposition"]
        assert client.get(f"/api/tasks/{task}").json()["artifacts"][0]["name"] == "draft.py"
