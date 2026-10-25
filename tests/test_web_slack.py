import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from work_buddy.engine import Engine
from work_buddy.models import Settings
from work_buddy.slack import SlackBridge
from work_buddy.store import Store
from work_buddy.web import create_app

ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data", "test-only-launch-token")
    app.state.store.save_settings(Settings(backend="demo"))
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as connection:
        yield connection


def login(client):
    result = client.post("/api/auth", json={"token": "test-only-launch-token"})
    assert result.status_code == 200
    return result


def test_local_auth_csrf_and_host(client):
    assert client.get("/api/health").json() == {"status": "ready"}
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/auth", json={"token": "wrong"}).status_code == 401
    response = login(client)
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert client.get("/api/state").status_code == 200
    assert (
        client.post(
            "/api/tasks",
            json={"title": "x", "instructions": "x"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert client.get("/api/state", headers={"Host": "evil.example"}).status_code == 400
    client.headers.pop("Origin")
    assert client.post("/api/tasks", json={"title": "x", "instructions": "x"}).status_code == 403


def test_static_assets_and_csp(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "Work Buddy" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_settings_and_source_restrictions(client, tmp_path):
    login(client)
    settings = client.get("/api/state").json()["settings"]
    settings["project_path"] = str(tmp_path)
    assert client.post("/api/settings", json=settings).status_code == 400
    project = tmp_path / "project"
    project.mkdir()
    settings["project_path"] = str(project)
    assert client.post("/api/settings", json=settings).status_code == 200
    assert (
        client.post(
            "/api/distill",
            json={
                "material": "I like evidence-backed work.",
                "source_files": ["../outside"],
            },
        ).status_code
        == 400
    )


def test_distillation_feedback_loop_through_api(client):
    login(client)
    response = client.post(
        "/api/distill",
        json={
            "material": "I like concise technical replies with specific evidence.",
        },
    )
    assert response.status_code == 200
    task_id = response.json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        data = client.get("/api/state").json()
        pending = [item for item in data["approvals"] if item["task_id"] == task_id]
        if pending:
            break
        time.sleep(0.03)
    assert pending
    assert data["memories"] == []
    result = client.post(
        f"/api/approvals/{pending[0]['id']}",
        json={
            "choice": "Approve",
            "reason": "Confirmed demo memory.",
        },
    )
    assert result.status_code == 200
    assert len(client.get("/api/state").json()["memories"]) == 1
    result = client.post(
        f"/api/tasks/{task_id}/feedback",
        json={
            "text": "This is too generic. Cite the particular example that supports a preference.",
        },
    )
    assert result.status_code == 200
    learning = client.get(f"/api/tasks/{result.json()['learning_task_id']}").json()
    assert learning["kind"] == "learn"
    assert "too generic" in learning["instructions"]


def test_memory_deletion_updates_export(client):
    login(client)
    client.post("/api/memories", json={"text": "Use explicit evidence.", "scope": "general"})
    memory = client.get("/api/state").json()["memories"][0]
    assert client.delete(f"/api/memories/{memory['id']}").status_code == 200
    assert client.get("/api/state").json()["memories"] == []
    from pathlib import Path

    export = Path(client.get("/api/state").json()["data_directory"]) / "memory/confirmed.json"
    assert json.loads(export.read_text())["memories"] == []


class FakeSlack:
    def __init__(self):
        self.messages = []
        self.views = []

    async def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)
        return {"ok": True}

    async def views_open(self, **kwargs):
        self.views.append(kwargs)
        return {"ok": True}

    async def conversations_replies(self, **kwargs):
        await asyncio.sleep(0)
        return {"messages": [{"user": "U2", "text": "Earlier relevant context"}]}


@pytest.fixture
def slack(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings(slack_owner="UOWNER", slack_channels=["CALLOWED"]))
    engine = Engine(store)
    bridge = SlackBridge(store, engine, ORIGIN)
    bridge.bot_id = "UBOT"
    bridge.client = FakeSlack()
    yield bridge
    store.close()


async def test_slack_inbox_authorization_dedup_and_threads(slack):
    event = {
        "user": "UOWNER",
        "channel": "DOWNER",
        "channel_type": "im",
        "text": "Investigate this",
        "ts": "123",
        "thread_ts": "100",
    }
    await slack.on_message({**event, "user": "UOTHER"}, {"event_id": "bad"})
    assert not slack.store.all("SELECT * FROM tasks")
    await slack.on_message(event, {"event_id": "one"})
    await slack.on_message(event, {"event_id": "one"})
    tasks = slack.store.all("SELECT * FROM tasks")
    assert len(tasks) == 1
    assert tasks[0]["status"] == "queued"
    assert "Earlier relevant context" in tasks[0]["instructions"]
    await slack.on_message({**event, "bot_id": "BSELF"}, {"event_id": "bot"})
    assert len(slack.store.all("SELECT * FROM tasks")) == 1


async def test_channel_requires_allowlist_and_trigger(slack):
    event = {
        "user": "UTEAM",
        "channel": "COTHER",
        "channel_type": "channel",
        "text": "<@UOWNER> is this compatible?",
        "ts": "200",
    }
    await slack.on_message(event, {"event_id": "blocked"})
    assert not slack.store.all("SELECT * FROM tasks")
    await slack.on_message({**event, "channel": "CALLOWED", "text": "noise"}, {"event_id": "noise"})
    assert not slack.store.all("SELECT * FROM tasks")
    await slack.on_message({**event, "channel": "CALLOWED"}, {"event_id": "valid"})
    assert len(slack.store.all("SELECT * FROM tasks")) == 1


async def test_slack_owner_only_decision_and_learning(slack):
    task_id = slack.store.create_task("Choose", "Goal")
    slack.store.update_task(task_id, status="waiting_approval")
    approval = slack.store.create_approval(task_id, "decision", "Tradeoff?", ["A", "B"], {})
    view = {
        "private_metadata": json.dumps({"id": approval, "choice": "A"}),
        "state": {"values": {"reason": {"value": {"value": "Compatibility matters here."}}}},
    }

    async def ack():
        return None

    await slack.on_view(ack, {"user": {"id": "UOTHER"}}, view)
    assert slack.store.approval(approval)["status"] == "pending"
    await slack.on_view(ack, {"user": {"id": "UOWNER"}}, view)
    assert slack.store.approval(approval)["status"] == "approved"
    assert slack.store.one("SELECT id FROM tasks WHERE kind='learn'")
    assert slack.store.all("SELECT * FROM memories") == []


async def test_slack_reply_is_exact_and_notified_separately(slack):
    task_id = slack.store.create_task(
        "Reply",
        "Goal",
        source={
            "channel": "CALLOWED",
            "thread_ts": "300",
        },
    )
    from work_buddy.models import AgentReply

    slack.engine.slack = slack
    await slack.engine.handle_reply(
        slack.store.task(task_id),
        AgentReply(
            action="complete",
            message="Draft ready.",
            slack_reply="The exact approved text.",
        ),
    )
    assert all(message["channel"] == "UOWNER" for message in slack.client.messages)
    approval = slack.store.one("SELECT id FROM approvals WHERE task_id=?", (task_id,))
    await slack.engine.resolve(approval["id"], "Approve", "")
    sent = [message for message in slack.client.messages if message["channel"] == "CALLOWED"]
    assert len(sent) == 1
    assert sent[0]["text"] == "The exact approved text."
    assert sent[0]["thread_ts"] == "300"


async def test_slack_feedback_enters_learning_loop(slack):
    task_id = slack.store.create_task("Result", "Write a response")
    slack.store.update_task(task_id, result="Overconfident initial reply", status="completed")
    view = {
        "private_metadata": task_id,
        "state": {"values": {"feedback": {"value": {"value": "Say when evidence is missing."}}}},
    }

    async def ack():
        return None

    await slack.on_feedback_view(ack, {"user": {"id": "UOTHER"}}, view)
    assert not slack.store.all("SELECT * FROM feedback")
    await slack.on_feedback_view(ack, {"user": {"id": "UOWNER"}}, view)
    assert len(slack.store.all("SELECT * FROM feedback")) == 1
    learned = slack.store.one("SELECT instructions FROM tasks WHERE kind='learn'")
    assert "Overconfident initial reply" in learned["instructions"]
    assert "Say when evidence is missing." in learned["instructions"]
    assert not slack.store.all("SELECT * FROM memories")
