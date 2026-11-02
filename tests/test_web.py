import pytest
from conftest import FakeClient, FakeSlackMcp, FakeTokens, env
from fastapi.testclient import TestClient

from chiefofstaff.runtime import Runtime
from chiefofstaff.sources.slack import SlackSource
from chiefofstaff.web import create_app

TOKEN = "launch-token"
ORIGIN = {"origin": "http://127.0.0.1:8765"}


@pytest.fixture
def app_client(tmp_path):
    client = FakeClient()

    def factory(store, bus):
        async def notifier(title, text):
            pass

        runtime = Runtime(store, bus, client_factory=lambda settings: client, notifier=notifier)
        runtime.slack = SlackSource(store, runtime, tokens=FakeTokens(""), mcp=FakeSlackMcp())
        return runtime

    app = create_app(tmp_path / "data", TOKEN, 8765, runtime_factory=factory)
    with TestClient(app, base_url="http://127.0.0.1:8765") as http:
        http.fake = client
        yield http


def sign_in(http):
    assert http.post("/api/auth", json={"token": TOKEN}, headers=ORIGIN).status_code == 200


def test_api_needs_the_launch_cookie_and_a_trusted_origin(app_client):
    assert app_client.get("/api/state").status_code == 401
    assert app_client.post("/api/auth", json={"token": "nope"}, headers=ORIGIN).status_code == 401
    sign_in(app_client)
    state = app_client.get("/api/state").json()
    assert state["runtime"]["state"] == "ready"
    assert [s["role"] for s in state["sessions"]] == ["router"]
    router = state["router_id"]
    response = app_client.post(f"/api/sessions/{router}/messages", json={"text": "hi"})
    assert response.status_code == 403, "writes without a same-origin header are refused"


def test_owner_talks_to_the_router(app_client):
    sign_in(app_client)
    router = app_client.get("/api/state").json()["router_id"]
    sent = app_client.post(
        f"/api/sessions/{router}/messages", json={"text": "Ship the release notes"}, headers=ORIGIN
    )
    assert sent.status_code == 200 and sent.json()["kind"] == "owner"
    assert app_client.fake.sessions[router].sent == [
        env("owner", "message", "Ship the release notes")
    ]
    messages = app_client.get(f"/api/sessions/{router}/messages").json()
    assert [m["text"] for m in messages] == ["Ship the release notes"]
    after = app_client.get(f"/api/sessions/{router}/messages?after={messages[-1]['id']}").json()
    assert after == []
    empty = app_client.post(f"/api/sessions/{router}/messages", json={"text": ""}, headers=ORIGIN)
    assert empty.status_code == 422
    closing = app_client.post(f"/api/sessions/{router}/close", headers=ORIGIN)
    assert closing.status_code == 400
    assert app_client.get(f"/api/sessions/{router}").json()["role"] == "router"


def test_settings_are_validated(app_client, tmp_path):
    sign_in(app_client)
    project = tmp_path / "project"
    project.mkdir()
    saved = app_client.post(
        "/api/settings", json={"projects": [str(project)], "max_workers": 3}, headers=ORIGIN
    )
    assert saved.status_code == 200
    assert saved.json()["settings"]["projects"] == [str(project.resolve())]
    containing_data = app_client.post(
        "/api/settings", json={"projects": [str(tmp_path)]}, headers=ORIGIN
    )
    assert "private data directory" in containing_data.json()["detail"]
    missing = app_client.post(
        "/api/settings", json={"projects": [str(tmp_path / "nope")]}, headers=ORIGIN
    )
    assert missing.status_code == 422


def test_slack_token_must_be_a_user_token(app_client):
    sign_in(app_client)
    bot = app_client.post("/api/slack/token", json={"token": "xoxb-123"}, headers=ORIGIN)
    assert bot.status_code == 422
    saved = app_client.post("/api/slack/token", json={"token": "xoxp-1-abc"}, headers=ORIGIN)
    assert saved.status_code == 200 and saved.json()["slack"]["token_saved"] is True
    removed = app_client.delete("/api/slack/token", headers=ORIGIN)
    assert removed.json()["slack"]["token_saved"] is False


def test_unknown_draft_and_session_are_404(app_client):
    sign_in(app_client)
    assert app_client.get("/api/sessions/nope/messages").status_code == 404
    response = app_client.post("/api/drafts/dnope", json={"action": "send"}, headers=ORIGIN)
    assert response.status_code == 404


async def test_a_stalled_event_stream_is_closed_so_the_browser_resyncs():
    import asyncio

    from chiefofstaff.bus import Bus

    bus = Bus()
    bus.bind(asyncio.get_running_loop())
    async with bus.subscribe() as queue:
        for index in range(1002):
            bus.publish("message", index)
        items = [queue.get_nowait() for _ in range(queue.qsize())]
    assert items[-1] is None
    assert not bus.queues


def test_models_usage_and_profile_endpoints(app_client):
    sign_in(app_client)
    router = app_client.get("/api/state").json()["router_id"]
    assert [m["id"] for m in app_client.get("/api/models").json()] == [
        "auto",
        "fast-model",
        "smart-model",
    ]
    switched = app_client.post(
        f"/api/sessions/{router}/model", json={"model": "fast-model"}, headers=ORIGIN
    )
    assert switched.json()["model_choice"] == "fast-model"
    bad = app_client.post(f"/api/sessions/{router}/model", json={"model": "nope"}, headers=ORIGIN)
    assert bad.status_code == 400
    assert app_client.get("/api/usage?since=0").json()["total"]["calls"] == 0
    saved = app_client.post("/api/profile", json={"text": "- likes short replies"}, headers=ORIGIN)
    assert saved.json()["profile"] == "- likes short replies"
    asked = app_client.post("/api/profile/distill", headers=ORIGIN)
    assert asked.json()["text"].startswith("Build my profile")
