import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from work_buddy.engine import Engine
from work_buddy.github import GitHubInbox
from work_buddy.models import AgentReply, ProposedFile, Settings
from work_buddy.store import Store, today
from work_buddy.web import create_app


@pytest.fixture
def store(tmp_path):
    database = Store(tmp_path / "data")
    yield database
    database.close()


def test_daily_brief_preserves_plain_text_and_local_day(store):
    brief = "Today:\n- Review caching.\n- Write design notes.\nContext: preserve compatibility."
    task_id = store.create_daily_brief(brief)
    task = store.task(task_id)
    assert task["kind"] == "daily"
    assert task["instructions"] == brief
    assert store.daily_briefs("")[0]["day"] == today()
    side_id = store.create_task("A message", "Question", kind="inbox")
    context = Engine(store).context(store.task(side_id))
    assert context["daily_briefs"][0]["text"] == brief
    assert context["ongoing_work"][0]["id"] == task_id


def test_daily_context_does_not_cross_project_or_date(store):
    task_id = store.create_daily_brief("Today's actual priorities")
    store.execute("UPDATE daily_briefs SET day='2000-01-01' WHERE task_id=?", (task_id,))
    assert store.daily_briefs("") == []
    other = store.create_task("Other project", "Work", project_path="/other")
    assert Engine(store).context(store.task(other))["daily_briefs"] == []


def test_daily_endpoint_needs_only_one_text_field(tmp_path):
    app = create_app(tmp_path / "data", "test-token")
    app.state.store.save_settings(Settings(backend="demo", paused=True))
    with TestClient(
        app, base_url="http://127.0.0.1:8765", headers={"Origin": "http://127.0.0.1:8765"}
    ) as client:
        client.post("/api/auth", json={"token": "test-token"})
        result = client.post("/api/daily", json={"text": "Review the PR and update the docs."})
        assert result.status_code == 200
        state = client.get("/api/state").json()
        assert state["daily_briefs"][0]["task_id"] == result.json()["id"]
        assert state["updates"][0]["kind"] == "daily"
        assert client.post("/api/daily", json={"text": "  "}).status_code == 422


def test_distillation_can_start_from_connected_project_without_manual_profile(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("Project overview")
    app = create_app(tmp_path / "data", "test-token")
    app.state.store.save_settings(
        Settings(
            backend="demo",
            paused=True,
            project_path=str(project),
        )
    )
    with TestClient(
        app, base_url="http://127.0.0.1:8765", headers={"Origin": "http://127.0.0.1:8765"}
    ) as client:
        client.post("/api/auth", json={"token": "test-token"})
        result = client.post("/api/distill", json={})
        assert result.status_code == 200
        task = client.get("/api/tasks/" + result.json()["id"]).json()
        assert task["kind"] == "distill"
        assert "owner authorizes project discovery" in task["instructions"]


async def test_lead_can_ignore_side_noise_but_not_daily_priorities(store):
    engine = Engine(store)
    side = store.create_task("FYI", "No response needed", kind="inbox")
    await engine.handle_reply(
        store.task(side), AgentReply(action="ignore", message="No action needed.")
    )
    assert store.task(side)["status"] == "ignored"
    assert store.backlog_count() == 0
    assert engine.snapshot()["updates"] == []
    daily = store.create_daily_brief("Please finish my priorities.")
    with pytest.raises(ValueError, match="Only discovered inbox"):
        await engine.handle_reply(
            store.task(daily),
            AgentReply(action="ignore", message="Not interesting."),
        )


async def test_choices_without_reasons_are_still_learning_evidence(store):
    engine = Engine(store)
    task = store.create_daily_brief("Prepare the proposal")
    store.update_task(task, status="waiting_approval")
    approval = store.create_approval(task, "decision", "Which format?", ["Short", "Long"], {})
    await engine.resolve(approval, "Short", "")
    lesson = store.one("SELECT instructions FROM tasks WHERE kind='learn'")
    assert lesson and "not provided; do not invent one" in lesson["instructions"]
    assert store.all("SELECT * FROM memories") == []


def test_old_settings_and_existing_data_survive_upgrade(tmp_path):
    database = Store(tmp_path / "data")
    database.execute(
        "INSERT INTO settings VALUES(1,?)",
        (json.dumps({"backend": "demo", "profile": "old"}),),
    )
    database.remember("Keep citations.", "general", "")
    database.close()
    reopened = Store(tmp_path / "data")
    try:
        assert reopened.settings().github_enabled is False
        assert reopened.settings().profile == "old"
        assert reopened.all("SELECT text FROM memories")[0]["text"] == "Keep citations."
        reopened.create_daily_brief("A new day")
    finally:
        reopened.close()


def test_review_repository_validation():
    with pytest.raises(ValidationError):
        Settings(github_enabled=True)
    with pytest.raises(ValidationError):
        Settings(github_repos=["org/repo/../../other"])


class FakeGitHub:
    def __init__(self):
        self.head = "a" * 40
        self.calls = []
        self.posts = []
        self.title = "Keep old clients working"

    async def __call__(self, *args, payload=None):
        self.calls.append(args)
        if args[0] == "search":
            return json.dumps(
                [
                    {
                        "number": 7,
                        "repository": {"nameWithOwner": "example/project"},
                        "title": self.title,
                        "url": "https://github.com/example/project/pull/7",
                    }
                ]
            )
        if payload is not None:
            self.posts.append(payload)
            return '{"id": 123}'
        if "-H" in args:
            return "diff --git a/cache.py b/cache.py\n@@ -1 +1 @@\n-old\n+new\n"
        return json.dumps(
            {
                "state": "open",
                "draft": False,
                "title": self.title,
                "body": "Compatibility fix",
                "head": {"sha": self.head},
                "base": {"sha": "b" * 40},
            }
        )


async def test_review_requests_are_captured_automatically_and_deduplicated(store, monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr("work_buddy.github.gh", fake)
    store.save_settings(Settings(github_enabled=True, github_repos=["example/project"]))
    watcher = GitHubInbox(store)
    store.create_daily_brief("Focus on compatibility today.")
    await watcher.poll_once()
    await watcher.poll_once()
    captured = store.all("SELECT id FROM tasks WHERE kind='inbox'")
    assert len(captured) == 1
    task = store.task(captured[0]["id"])
    assert task["source"]["head_sha"] == fake.head
    assert task["source"]["read_project"] is False
    assert "cache.py" in task["instructions"]
    assert Engine(store).context(task)["daily_briefs"]
    assert "--review-requested=@me" in fake.calls[0]
    assert "--repo=example/project" in fake.calls[0]
    fake.head = "c" * 40
    await watcher.poll_once()
    assert len(store.all("SELECT id FROM tasks WHERE kind='inbox'")) == 2
    assert fake.posts == []


async def test_disabled_review_connection_never_uses_github(store, monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr("work_buddy.github.gh", fake)
    await GitHubInbox(store).poll_once()
    assert fake.calls == []


async def test_review_publishes_only_after_approval_and_checks_head(store, monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr("work_buddy.github.gh", fake)
    store.save_settings(Settings(github_enabled=True, github_repos=["example/project"]))
    watcher = GitHubInbox(store)
    engine = Engine(store)
    engine.github = watcher
    await watcher.poll_once()
    task = store.task(store.one("SELECT id FROM tasks WHERE kind='inbox'")["id"])
    await engine.handle_reply(
        task,
        AgentReply(
            action="complete",
            message="Review draft ready.",
            github_review="cache.py:1: check clients.",
        ),
    )
    approval = store.one("SELECT id FROM approvals WHERE kind='review'")["id"]
    assert not fake.posts
    await engine.resolve(approval, "Approve", "")
    assert fake.posts == [
        {
            "event": "COMMENT",
            "commit_id": fake.head,
            "body": "cache.py:1: check clients.",
        }
    ]
    with pytest.raises(ValueError, match="no longer pending"):
        await engine.resolve(approval, "Approve", "")
    fake.head = "d" * 40
    with pytest.raises(ValueError, match="PR changed"):
        await watcher.publish_review(
            {
                "repo": "example/project",
                "number": 7,
                "head_sha": "a" * 40,
                "body": "stale",
            }
        )
    assert len(fake.posts) == 1


async def test_remote_review_cannot_write_local_files_and_workers_keep_isolation(store):
    task_id = store.create_task(
        "Remote PR",
        "Review a remote diff",
        kind="inbox",
        source={"type": "github", "read_project": False},
    )
    engine = Engine(store)
    with pytest.raises(ValueError, match="Remote review tasks"):
        await engine.handle_reply(
            store.task(task_id),
            AgentReply(
                action="complete",
                message="Oops",
                files=[ProposedFile(path="local.py", content="x")],
            ),
        )
    await engine.handle_reply(
        store.task(task_id),
        AgentReply(
            action="delegate",
            message="Investigate independent aspects",
            assignments=[{"title": "Read diff", "instructions": "Inspect compatibility"}],
        ),
    )
    worker = store.task(store.children(task_id)[0]["id"])
    assert worker["source"]["read_project"] is False
