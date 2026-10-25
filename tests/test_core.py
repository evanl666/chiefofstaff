import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from work_buddy.agent import CopilotAgent, DemoAgent, parse_reply
from work_buddy.engine import Engine
from work_buddy.files import apply_files, prepare_files, safe_path
from work_buddy.models import AgentReply, ProposedFile, Settings
from work_buddy.store import Store


@pytest.fixture
def store(tmp_path):
    database = Store(tmp_path / "data")
    yield database
    database.close()


async def wait_status(store, task_id, target):
    async with asyncio.timeout(5):
        while store.task(task_id)["status"] != target:  # noqa: ASYNC110 -- inspect persisted state
            await asyncio.sleep(0.02)


def test_default_is_real_agent():
    assert Settings().backend == "copilot"


def test_file_content_is_not_stripped():
    proposal = ProposedFile(path="code.py", content="    indented\n\n")
    assert proposal.content == "    indented\n\n"


def test_reply_validation():
    assert parse_reply('```json\n{"action":"complete","message":"ok"}\n```').message == "ok"
    with pytest.raises(ValidationError):
        parse_reply('{"action":"delegate","message":"go"}')
    with pytest.raises(ValidationError):
        AgentReply(action="decision", message="Choose", options=["same", "same"])
    with pytest.raises(ValidationError):
        AgentReply(action="decision", message="Choose", options=["A", "B"], slack_reply="oops")


@pytest.mark.parametrize("path", ["../escape", "/absolute", ".git/config", ".env", ".ssh/key"])
def test_file_proposals_reject_unsafe_paths(tmp_path, path):
    with pytest.raises(ValueError):
        safe_path(tmp_path, path)


def test_symlink_rejected(tmp_path):
    (tmp_path / "link").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="Symlink"):
        safe_path(tmp_path, "link")


def test_exact_file_approval_and_stale_check(tmp_path):
    path = tmp_path / "code.py"
    path.write_text("old\n")
    payload = prepare_files(str(tmp_path), [ProposedFile(path="code.py", content="new\n")])
    assert "-old" in payload["files"][0]["diff"]
    assert path.read_text() == "old\n"
    path.write_text("another human edit\n")
    with pytest.raises(ValueError, match="changed"):
        apply_files(payload)
    assert path.read_text() == "another human edit\n"
    fresh = prepare_files(str(tmp_path), [ProposedFile(path="code.py", content="new\n")])
    apply_files(fresh)
    assert path.read_text() == "new\n"


def test_batch_preflight_prevents_partial_stale_write(tmp_path):
    (tmp_path / "b").write_text("before")
    payload = prepare_files(
        str(tmp_path),
        [
            ProposedFile(path="a", content="created"),
            ProposedFile(path="b", content="replaced"),
        ],
    )
    (tmp_path / "b").write_text("changed")
    with pytest.raises(ValueError):
        apply_files(payload)
    assert not (tmp_path / "a").exists()


def test_memory_export_and_project_scope(store, tmp_path):
    store.remember("Use citations.", "project", "/project-a")
    store.remember("Keep it concise.", "general", "")
    payload = json.loads((store.directory / "memory/confirmed.json").read_text())
    assert len(payload["memories"]) == 2
    task_id = store.create_task("Other project", "Investigate.", project_path="/project-b")
    memories = Engine(store).context(store.task(task_id))["memories"]
    assert [memory["text"] for memory in memories] == ["Keep it concise."]


def test_demo_memories_do_not_enter_real_agent_context(store):
    demo = store.create_task("Demo profile", "Synthetic", backend="demo")
    store.remember("Synthetic preference", "general", "", task_id=demo)
    real = store.create_task("Real task", "Work", backend="copilot")
    assert Engine(store).context(store.task(real))["memories"] == []
    assert len(Engine(store).context(store.task(demo))["memories"]) == 1


def test_recover_requires_retry_and_never_replays_side_effect(store):
    task_id = store.create_task("Work", "Work")
    store.update_task(task_id, status="running")
    approval_id = store.create_approval(task_id, "slack", "Send?", ["Approve", "Reject"], {})
    store.execute("UPDATE approvals SET status='executing' WHERE id=?", (approval_id,))
    store.recover()
    assert store.task(task_id)["status"] == "interrupted"
    assert store.approval(approval_id)["status"] == "uncertain"
    with pytest.raises(ValueError, match="uncertain"):
        Engine(store).retry(task_id)


async def test_demo_delegation_decision_completion(store):
    store.save_settings(Settings(backend="demo"))
    engine = Engine(store)
    await engine.start()
    try:
        task_id = store.create_task("Main goal", "Investigate and report.")
        await wait_status(store, task_id, "waiting_approval")
        assert len(store.children(task_id)) == 2
        assert all(child["status"] == "completed" for child in store.children(task_id))
        approval = store.one("SELECT id FROM approvals WHERE task_id=?", (task_id,))
        await engine.resolve(approval["id"], "Short summary", "I need a quick update.")
        await wait_status(store, task_id, "completed")
        assert "Short summary" in store.task(task_id)["result"]
        assert store.one("SELECT id FROM tasks WHERE kind='learn'")
        assert list((store.directory / "artifacts").glob("*.json"))
    finally:
        await engine.stop()


async def test_distillation_requires_confirmation_and_persists(store):
    store.save_settings(Settings(backend="demo"))
    engine = Engine(store)
    await engine.start()
    try:
        task_id = store.create_task("Profile", "I prefer evidence.", kind="distill")
        await wait_status(store, task_id, "waiting_approval")
        assert store.all("SELECT * FROM memories") == []
        approval = store.one("SELECT id FROM approvals WHERE task_id=?", (task_id,))
        await engine.resolve(approval["id"], "Approve", "")
        assert store.task(task_id)["status"] == "completed"
        assert len(store.all("SELECT * FROM memories")) == 1
        assert (store.directory / "memory/confirmed.json").exists()
        with pytest.raises(ValueError, match="no longer pending"):
            await engine.resolve(approval["id"], "Approve", "")
    finally:
        await engine.stop()


async def test_worker_cannot_delegate(store):
    parent = store.create_task("Parent", "Goal")
    child = store.create_task("Worker", "Task", parent_id=parent)
    engine = Engine(store)
    with pytest.raises(ValueError, match="Only a lead"):
        await engine.handle_reply(
            store.task(child),
            AgentReply(
                action="delegate",
                message="more",
                assignments=[{"title": "Illegal nested task", "instructions": "do more"}],
            ),
        )
    assert store.children(child) == []


async def test_invalid_reply_does_not_leave_pending_memory(store):
    task_id = store.create_task("Bad target", "Task")
    with pytest.raises(ValueError, match="Slack reply target"):
        await Engine(store).handle_reply(
            store.task(task_id),
            AgentReply(
                action="complete",
                message="done",
                slack_reply="No target",
                memories=[
                    {
                        "text": "Test",
                        "category": "preference",
                        "evidence": "test",
                        "scope": "project",
                    }
                ],
            ),
        )
    assert store.all("SELECT * FROM approvals") == []


async def test_file_changes_only_after_approval(store, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    task_id = store.create_task("Write draft", "Task", project_path=str(project))
    engine = Engine(store)
    await engine.handle_reply(
        store.task(task_id),
        AgentReply(
            action="complete",
            message="Proposed a doc.",
            files=[ProposedFile(path="draft.txt", content="draft\n")],
        ),
    )
    assert not (project / "draft.txt").exists()
    approval = store.one("SELECT id FROM approvals WHERE task_id=?", (task_id,))
    await engine.resolve(approval["id"], "Approve", "")
    assert (project / "draft.txt").read_text() == "draft\n"


async def test_expired_approval_is_not_executed(store):
    task_id = store.create_task("Decision", "Task")
    store.update_task(task_id, status="waiting_approval")
    approval = store.create_approval(task_id, "decision", "Which?", ["A", "B"], {})
    store.execute(
        "UPDATE approvals SET created_at=? WHERE id=?",
        ((datetime.now(UTC) - timedelta(days=2)).isoformat(), approval),
    )
    with pytest.raises(ValueError, match="expired"):
        await Engine(store).resolve(approval, "A", "")
    assert store.approval(approval)["status"] == "expired"
    assert store.task(task_id)["status"] == "interrupted"


async def test_cancellation_cancels_children_and_approvals(store):
    parent = store.create_task("Main", "Work")
    child = store.create_task("Child", "Work", parent_id=parent)
    approval = store.create_approval(child, "decision", "?", ["A", "B"], {})
    await Engine(store).cancel(parent)
    assert store.task(parent)["status"] == "cancelled"
    assert store.task(child)["status"] == "cancelled"
    assert store.approval(approval)["status"] == "cancelled"


class BlockingAgent:
    def __init__(self):
        self.started = []
        self.released = asyncio.Event()
        self.cancelled = []

    async def run(self, task, context, settings, report):
        self.started.append(task["id"])
        try:
            await self.released.wait()
        except asyncio.CancelledError:
            self.cancelled.append(task["id"])
            raise
        return AgentReply(action="complete", message="done")


async def test_worker_limit_pause_and_stop(store):
    agent = BlockingAgent()
    store.save_settings(Settings(max_workers=2))
    engine = Engine(store, agent)
    parent = store.create_task("Parent", "Main")
    store.update_task(parent, status="waiting_workers")
    for index in range(4):
        store.create_task(f"Worker {index}", "read", parent_id=parent)
    await engine.tick()
    await asyncio.sleep(0.01)
    assert len(agent.started) == 2
    settings = store.settings()
    settings.paused = True
    store.save_settings(settings)
    await engine.tick()
    assert len(agent.started) == 2
    await engine.stop()
    assert len(agent.cancelled) == 2


class FailingAgent:
    async def run(self, task, context, settings, report):
        raise RuntimeError("Authentication failed.")


async def test_real_failure_is_never_demo_success(store):
    engine = Engine(store, FailingAgent())
    task_id = store.create_task("Real", "Work")
    store.update_task(task_id, status="running")
    await engine.run_task(task_id)
    assert store.task(task_id)["status"] == "failed"
    assert "Authentication failed" in store.task(task_id)["error"]
    assert not store.task(task_id)["result"]


def test_cli_tool_boundary(tmp_path):
    executable = tmp_path / "copilot"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    command = CopilotAgent.command(Settings(copilot_command=str(executable)))
    assert "--available-tools=view,glob,rg" in command
    assert "--deny-tool=shell" in command
    assert "--deny-tool=write" in command
    assert "--disallow-temp-dir" in command
    assert "--allow-all" not in command
    assert "--allow-all-paths" not in command


async def test_cli_jsonl_stdin_and_environment(tmp_path, monkeypatch):
    import sys

    executable = tmp_path / "fake-copilot"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "prompt = sys.stdin.read()\n"
        "assert 'WORK_BUDDY_SLACK_BOT_TOKEN' not in os.environ\n"
        "assert 'COPILOT_ALLOW_ALL' not in os.environ\n"
        "assert 'schema' in prompt\n"
        "print(json.dumps({'type':'assistant.message','data':{'content':"
        "json.dumps({'action':'complete','message':'Read the prompt successfully.'})}}))\n"
        "print(json.dumps({'type':'result','exitCode':0}))\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("WORK_BUDDY_SLACK_BOT_TOKEN", "fake-not-a-real-token")
    monkeypatch.setenv("COPILOT_ALLOW_ALL", "true")
    reply = await CopilotAgent(tmp_path / "empty").run(
        {"project_path": "", "instructions": "hello"},
        {},
        Settings(copilot_command=str(executable)),
        lambda message: None,
    )
    assert reply.message == "Read the prompt successfully."


async def test_demo_explicitly_labels_learning():
    reply = await DemoAgent().run(
        {"kind": "learn"},
        {},
        Settings(),
        lambda message: None,
    )
    assert "[DEMO]" in reply.memories[0].evidence
