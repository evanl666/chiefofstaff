"""Reliability: what happens when starts are interrupted, connections drop and things fail,
and the boundaries that keep Slack content and the owner's checkout safe."""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import Harness, call_tool, settle

from chiefofstaff.models import DraftDecision, Settings
from chiefofstaff.workspaces import changes, checkout_git, protected_git_paths


async def spawn(runtime, router, title, **extra):
    extra.setdefault("instructions", title)
    extra.setdefault("why_new", "test")
    result = await call_tool(runtime, "spawn_worker", router, title=title, **extra)
    return json.loads(result.text_result_for_llm)


def gate(harness):
    """Session creation waits until the returned event is set."""
    opened = asyncio.Event()
    create = harness.client.create_session

    async def slow_create(**options):
        await opened.wait()
        return await create(**options)

    harness.client.create_session = slow_create
    return opened


async def test_a_task_cancelled_while_its_worker_starts_stays_cancelled(harness):
    runtime = await harness.connect()
    opened = gate(harness)
    spawning = asyncio.ensure_future(spawn(runtime, harness.router_id, "Build it"))
    await asyncio.sleep(0.05)
    [row] = [r for r in harness.store.open_sessions() if r["role"] == "worker"]
    assert row["status"] == "starting"
    await runtime.task_actions.cancel(runtime.tasks.current(row["id"])["id"], "not needed")
    opened.set()
    await spawning
    await settle(runtime)
    row = harness.store.session(row["id"])
    task = runtime.tasks.current(row["id"])
    assert row["status"] == "idle" and task["status"] == "cancelled"
    assert not [run for run in runtime.tasks.runs(task["id"]) if not run["ended_at"]]
    assert harness.session(row["id"]).sent == [], "nothing sent for a cancelled task"
    assert not runtime.states[row["id"]].busy
    assert not runtime.delivery.has_unsent(row["id"]), "a restart does not start it either"
    await runtime.reconnect()  # Copilot did not keep the session: it never had a message.
    await runtime.delivery.deliver(row["id"], "Do this instead", kind="owner")
    assert harness.client.created.count(row["id"]) == 2, "made again, under the same id"
    assert "Do this instead" in harness.session(row["id"]).sent[-1]


async def test_a_reconnect_while_a_worker_starts_puts_it_back_in_the_queue(harness):
    runtime = await harness.connect()
    opened = gate(harness)
    spawning = asyncio.ensure_future(spawn(runtime, harness.router_id, "Build it"))
    await asyncio.sleep(0.05)
    [row] = [r for r in harness.store.open_sessions() if r["role"] == "worker"]
    await runtime.disconnect()
    opened.set()
    await spawning
    assert harness.store.session(row["id"])["status"] == "queued", "not failed"
    assert not runtime.states[row["id"]].busy and not runtime.states.handle(row["id"])
    await runtime.connect()
    await settle(runtime)
    assert harness.store.session(row["id"])["status"] == "working"
    assert runtime.tasks.current(row["id"])["status"] == "running"
    assert len(harness.session(row["id"]).sent) == 1, "its task, once"


async def test_stopping_the_router_keeps_the_events_waiting_for_it(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Build it")
    router = harness.session(harness.router_id)
    await runtime.delivery.deliver(harness.router_id, "Busy with this", kind="owner")
    before = len(router.sent)
    await call_tool(runtime, "report", worker["session_id"], status="done", summary="Built.")
    await settle(runtime)
    await runtime.workers.interrupt(harness.router_id)
    router.emit("session.idle")
    await settle(runtime)
    assert len(router.sent) == before + 1 and "Built." in router.sent[-1]


async def test_a_reconnect_drops_a_go_on_note_for_a_turn_that_ended(harness):
    runtime = await harness.connect()
    worker = (await spawn(runtime, harness.router_id, "Build it"))["session_id"]
    runtime.states[worker].stale = True
    runtime.states[worker].after_refresh = "Go on with the task."
    await runtime.reconnect()
    await settle(runtime)
    runtime.states[worker].stale = True  # Settings changed during some later turn.
    await runtime.session_for(worker)
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    assert not any("Go on" in prompt for prompt in harness.session(worker).sent)


async def test_a_dropped_connection_or_a_failure_frees_worker_slots(tmp_path):
    harness = Harness(tmp_path / "data", Settings(max_workers=1))
    runtime = await harness.connect()
    first = await spawn(runtime, harness.router_id, "First")
    second = await spawn(runtime, harness.router_id, "Second")
    assert harness.store.session(second["session_id"])["status"] == "queued"
    runtime.fail(harness.store.session(first["session_id"]), "It broke.")
    await settle(runtime)
    assert harness.store.session(second["session_id"])["status"] == "working", "next in line"
    await runtime.disconnect()
    assert not any(runtime.states[s["session_id"]].busy for s in (first, second))


async def test_a_slack_session_copilot_lost_is_started_anew(tmp_path):
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    [old] = harness.store.open_sessions("slack")
    harness.store.delete(f"fresh:{old['id']}")  # It has had a conversation.
    await runtime.disconnect()
    harness.client.sessions.pop(old["id"], None)  # Copilot cleaned up its session state.
    await runtime.connect()
    assert runtime.status["state"] == "ready"
    [new] = harness.store.open_sessions("slack")
    assert new["id"] != old["id"] and harness.store.session(old["id"])["closed_at"]


async def test_slack_threads_reach_only_sessions_that_may_read_slack(harness):
    runtime = await harness.connect()
    sessions = len(harness.store.open_sessions())
    refused = await call_tool(runtime, "spawn_worker", harness.router_id, title="X",
                              instructions="x", follow=["slack:D0ALICE01"])  # fmt: skip
    assert refused.result_type == "failure" and len(harness.store.open_sessions()) == sessions
    worker = await spawn(runtime, harness.router_id, "Code")
    followed = await call_tool(runtime, "follow_thread", worker["session_id"],
                               thread="slack:C0SECRET1:1712345678.000100")  # fmt: skip
    assert followed.result_type == "failure"
    harness.store.follow("slack:D0ALICE01", worker["session_id"])  # However it got there.
    delivered = await runtime.on_thread("slack", "slack:D0ALICE01", ["k1"], "the password is x")
    assert not delivered and not harness.session(worker["session_id"]).sent[1:]


async def test_only_send_publishes_a_draft(harness):
    runtime = await harness.connect()
    target = {"repo": "acme/app", "number": 7, "head_sha": "abc", "url": "u"}
    draft = harness.store.create_draft(harness.router_id, "github", target, "Looks good")
    with pytest.raises(ValueError, match="Send this draft"):
        await runtime.drafts.resolve(draft["id"], DraftDecision(action="mark_sent"))
    assert harness.store.draft(draft["id"])["status"] == "pending"
    assert not [call for call in harness.gh_calls if call[0][0] != "auth"], "nothing published"


def test_a_code_folder_that_became_a_repository_does_not_break_settings(tmp_path):
    from chiefofstaff.store import Store

    code = tmp_path / "code"
    code.mkdir()
    store = Store(tmp_path / "data")
    store.save_settings(
        Settings(code_folder=str(code), max_workers=3, github_enabled=True, github_repos=["a/b"])
    )
    subprocess.run(["git", "init", "-q", str(code)], check=True)
    settings = store.settings()
    assert settings.code_folder in ("", None) and settings.max_workers == 3
    assert settings.github_enabled and settings.github_repos == ["a/b"], "valid together"


def test_the_owners_checkout_head_and_index_are_protected(tmp_path):
    repo = tmp_path / "app"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".git" / "index").write_bytes(b"")
    git_dir = str(repo / ".git")
    protected = protected_git_paths(git_dir, [])
    assert f"{git_dir}/HEAD" in protected and f"{git_dir}/index" in protected
    shared = [{"path": str(repo), "shared": True, "git_dir": git_dir}]
    assert f"{git_dir}/HEAD" not in protected_git_paths(git_dir, shared), "it works right there"


async def test_a_worker_cannot_point_chief_of_staffs_git_at_its_own_config(tmp_path):
    """A sandboxed worker can write its worktree's .git file and its commondir: Chief of Staff's
    own git (unsandboxed) must not follow them to a config the worker wrote."""

    def run(*args, cwd=tmp_path):
        return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout

    repo, tree = tmp_path / "app", tmp_path / "work" / "app"
    run("git", "init", "-q", "-b", "main", str(repo))
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty",
        "-m", "init", cwd=repo)  # fmt: skip
    run("git", "worktree", "add", "-q", "-b", "chief/x", str(tree), cwd=repo)
    checkout = {
        "repo": str(repo), "worktree": str(tree), "path": str(tree), "branch": "chief/x",
        "git_dir": str(repo / ".git"), "admin": str(repo / ".git" / "worktrees" / "app"),
        "base_ref": "HEAD",
    }  # fmt: skip
    evil = tmp_path / "evil"
    run("git", "init", "-q", str(evil))
    run("git", "config", "probe.owner", "attacker", cwd=evil)
    fake = tmp_path / "fake-admin"
    shutil.copytree(repo / ".git" / "worktrees" / "app", fake)
    (fake / "commondir").write_text(str(evil / ".git"))
    (tree / ".git").write_text(f"gitdir: {fake}\n")  # What the worker rewrote.
    (repo / ".git" / "commondir").write_text(str(evil / ".git"))  # And planted here.
    assert run("git", "config", "--get", "probe.owner", cwd=tree).strip() == "attacker"

    # Not found: the worker's config is not read.
    with pytest.raises(ValueError, match="git failed"):
        await checkout_git(checkout, str(tree), "config", "--get", "probe.owner")
    with pytest.raises(ValueError, match="git failed"):
        await checkout_git(checkout, str(repo), "config", "--get", "probe.owner")
    assert not (repo / ".git" / "commondir").exists(), "the planted pointer is removed"
    assert (await changes(checkout))["branch"] == "chief/x", "its work is still read"
    paths = protected_git_paths(str(repo / ".git"), [checkout])
    assert str(repo / ".git" / "commondir") in paths


async def test_a_clone_whose_config_would_run_code_is_not_read(tmp_path):
    clone = tmp_path / "clone"

    def made():
        subprocess.run(["git", "init", "-q", str(clone)], check=True)
        subprocess.run(["git", "config", "filter.x.clean", "touch pwned"], cwd=clone, check=True)

    await asyncio.to_thread(made)
    checkout = {"repo": str(clone), "path": str(clone), "remote": "acme/x", "branch": "b"}
    with pytest.raises(ValueError, match=r"filter\.x\.clean"):
        await checkout_git(checkout, str(clone), "status", "--porcelain")


async def test_closing_keeps_a_worktree_with_ignored_work(tmp_path):
    code = tmp_path / "code"
    app = code / "app"
    app.mkdir(parents=True)
    run = lambda *args: subprocess.run(args, cwd=app, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    (app / ".gitignore").write_text(".env\nnode_modules/\nreport.txt\n")
    run("git", "add", ".")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    (app / ".env").write_text("KEY=1\n")
    harness = Harness(tmp_path / "data", Settings(code_folder=str(code)))
    runtime = await harness.connect()

    async def close_after(change) -> Path:
        info = await spawn(runtime, harness.router_id, change.__name__, project=str(app),
                           isolate=True)  # fmt: skip
        tree = Path(info["cwd"])
        change(tree)
        await runtime.workers.close(info["session_id"])
        await settle(runtime)
        return tree

    def installed(tree):
        (tree / "node_modules").mkdir()
        (tree / "node_modules" / "x.js").write_text("x")

    def reported(tree):
        (tree / "report.txt").write_text("results")

    def edited(tree):
        (tree / ".env").write_text("KEY=2\n")

    assert not (await close_after(installed)).exists(), "dependencies come back with an install"
    assert (await close_after(reported)).exists(), "an ignored report is not deleted"
    assert (await close_after(edited)).exists(), "an edited .env is not replaced by the owner's"


async def test_changes_count_each_file_once_and_new_files(tmp_path):
    def run(*args, cwd=tmp_path):
        return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout

    repo, tree = tmp_path / "app", tmp_path / "work" / "app"
    run("git", "init", "-q", "-b", "main", str(repo))
    (repo / "a.py").write_text("one\n")
    run("git", "add", ".", cwd=repo)
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init",
        cwd=repo)  # fmt: skip
    run("git", "worktree", "add", "-q", "-b", "chief/x", str(tree), cwd=repo)
    (tree / "a.py").write_text("one\ntwo\n")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "two",
        cwd=tree)  # fmt: skip
    (tree / "a.py").write_text("one\ntwo\nthree\n")  # Changed again, not committed.
    (tree / "new.py").write_text("x = 1\ny = 2\n")  # New, never added.
    (tree / "__pycache__").mkdir()
    (tree / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"\0junk")  # Left by a test run.
    (tree / "uv.lock").write_text("version = 1\n")  # Written by `uv run`.
    checkout = {
        "repo": str(repo), "worktree": str(tree), "path": str(tree), "branch": "chief/x",
        "git_dir": str(repo / ".git"), "admin": str(repo / ".git" / "worktrees" / "app"),
        "base_ref": "main",
    }  # fmt: skip
    found = await changes(checkout)
    assert found["commits"] == 1
    assert (found["files"], found["insertions"]) == (2, 4), "a.py once (+2), new.py (+2)"
    assert found["uncommitted"] == 2, "a.py and new.py; not the cache"
    (tree / "new.py").unlink()
    run("git", "checkout", "--", "a.py", cwd=tree)
    assert (await changes(checkout))["uncommitted"] == 0, "only the cache is left: nothing"


async def test_a_project_folder_that_is_a_linked_worktree_is_read_as_itself(tmp_path):
    def run(*args, cwd=tmp_path):
        return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout

    repo, feature = tmp_path / "app", tmp_path / "app-feature"
    run("git", "init", "-q", "-b", "main", str(repo))
    (repo / "a.py").write_text("one\n")
    run("git", "add", ".", cwd=repo)
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init",
        cwd=repo)  # fmt: skip
    run("git", "worktree", "add", "-q", "-b", "feature", str(feature), cwd=repo)
    (repo / "main-only.txt").write_text("x\n")  # An edit in the main checkout, not this one.
    (feature / "a.py").write_text("one\ntwo\n")
    shared = {"project": str(feature), "path": str(feature), "branch": "", "shared": True,
              "git_dir": str(repo / ".git")}  # fmt: skip
    status = await checkout_git(shared, str(feature), "status", "--porcelain")
    assert status.strip() == "M a.py", status
    top = await checkout_git(shared, str(feature), "rev-parse", "--abbrev-ref", "HEAD")
    assert top.strip() == "feature"
    main = {**shared, "project": str(repo), "path": str(repo)}
    assert "main-only.txt" in await checkout_git(main, str(repo), "status", "--porcelain")
