import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from conftest import Harness, call_tool, env

from chiefofstaff import runtime as runtime_module
from chiefofstaff.models import Settings


def run(*command, cwd=None):
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True).stdout


def commit(repo: Path, name: str, text: str):
    (repo / name).write_text(text)
    run("git", "add", ".", cwd=repo)
    run(
        "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", f"add {name}", cwd=repo
    )


def published(tmp: Path, name: str) -> tuple[Path, Path]:
    """A repository with an origin, and a local clone that is one commit behind it."""
    origin = tmp / f"{name}.git"
    run("git", "init", "-q", "--bare", "-b", "main", str(origin))
    local = tmp / name
    run("git", "clone", "-q", str(origin), str(local))
    run("git", "checkout", "-q", "-b", "main", cwd=local)
    commit(local, "README.md", f"# {name}\n")
    run("git", "push", "-q", "origin", "main", cwd=local)
    other = tmp / f"{name}-other"
    run("git", "clone", "-q", str(origin), str(other))
    commit(other, "LATEST.md", "only on origin\n")
    run("git", "push", "-q", "origin", "main", cwd=other)
    return origin, local


def tracks_anything(path) -> bool:
    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "@{upstream}"], cwd=path, capture_output=True
    )
    return upstream.returncode == 0


async def spawn(runtime, router, **arguments):
    result = await call_tool(runtime, "spawn_worker", router, why_new="test", **arguments)
    return json.loads(result.text_result_for_llm)


async def test_one_workspace_with_a_worktree_per_repository(tmp_path, monkeypatch):
    _, api = published(tmp_path, "api")
    _, web = published(tmp_path, "web")
    harness = Harness(tmp_path / "data", Settings(projects=[str(api), str(web)]))
    runtime = await harness.connect()
    info = await spawn(
        runtime,
        harness.router_id,
        title="Rename user field",
        instructions="rename it in both",
        project=str(api),
        more_projects=[str(web), str(api)],
        isolate=True,
    )
    row = harness.store.session(info["session_id"])
    checkouts = json.loads(row["repos"])
    assert [Path(c["path"]).name for c in checkouts] == ["api", "web"], "one per repository"
    assert {c["branch"] for c in checkouts} == {info["branch"]}
    assert all(c["base"] == "origin/main" for c in checkouts)
    workspace = Path(harness.store.get(f"workspace:{row['id']}"))
    assert all(Path(c["path"]).parent == workspace for c in checkouts)
    assert info["cwd"] == checkouts[0]["path"] and info["checkouts"][1].endswith("/web")
    assert (Path(checkouts[0]["path"]) / "LATEST.md").exists(), "started from origin's latest"
    assert not (api / "LATEST.md").exists(), "the owner's checkout is untouched"
    assert not tracks_anything(checkouts[0]["path"]), "the branch must not track main"

    # These test folders live in the temp directory, where writing is always allowed.
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/nonexistent-temp")

    async def may_write(path):
        request = SimpleNamespace(kind="write", file_name="", resolved_path=path)
        decision = await runtime.on_permission(request, {"session_id": row["id"]})
        return type(decision).__name__ == "PermissionDecisionApproveOnce"

    assert await may_write(checkouts[1]["path"] + "/src/a.ts")
    assert await may_write(str(workspace / "notes.md"))
    assert not await may_write(str(web / "a.ts")), "not the owner's own checkout"
    prompt = harness.session(row["id"]).options["system_message"]["content"]
    assert f"Your workspace: {workspace}" in prompt and checkouts[1]["path"] in prompt


async def test_add_repo_from_this_machine_or_a_remote(tmp_path):
    origin, api = published(tmp_path, "api")
    _, web = published(tmp_path, "web")
    harness = Harness(tmp_path / "data", Settings(projects=[str(api), str(web)]))
    runtime = await harness.connect()
    info = await spawn(
        runtime,
        harness.router_id,
        title="Fix login",
        instructions="x",
        project=str(api),
        isolate=True,
    )
    worker = info["session_id"]
    added = json.loads(
        (await call_tool(runtime, "add_repo", worker, repo=str(web))).text_result_for_llm
    )
    assert added["branch"] == info["branch"] and Path(added["path"]).name == "web"
    again = await call_tool(runtime, "add_repo", worker, repo=str(web))
    assert "already have" in again.text_result_for_llm

    cloned = json.loads(
        (await call_tool(runtime, "add_repo", worker, repo=f"file://{origin}")).text_result_for_llm
    )
    assert Path(cloned["path"]).parent == Path(added["path"]).parent
    assert run("git", "branch", "--show-current", cwd=cloned["path"]).strip() == info["branch"]
    assert (Path(cloned["path"]) / "LATEST.md").exists()
    assert len(json.loads(harness.store.session(worker)["repos"])) == 3
    bad = await call_tool(runtime, "add_repo", worker, repo="not a repo")
    assert "git URL or GitHub owner/name" in bad.text_result_for_llm

    reader = (await spawn(runtime, harness.router_id, title="Answer Bob", instructions="y"))[
        "session_id"
    ]
    harness.store.execute("UPDATE sessions SET slack=1 WHERE id=?", (reader,))
    refused = await call_tool(runtime, "add_repo", reader, repo=f"file://{origin}")
    assert "only add configured projects" in refused.text_result_for_llm


async def test_yolo_skips_permission_checks_globally_or_per_worker(tmp_path):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    worker = (await spawn(runtime, harness.router_id, title="Ship", instructions="s"))["session_id"]
    push = SimpleNamespace(kind="shell", full_command_text="git push --force", possible_urls=[])

    async def allowed(session_id):
        decision = await runtime.on_permission(push, {"session_id": session_id})
        return type(decision).__name__ == "PermissionDecisionApproveOnce"

    assert not await allowed(worker)
    view = await runtime.set_session_yolo(worker, "on")
    assert view["yolo"] and view["yolo_choice"] == "on"
    assert await allowed(worker)
    prompt = harness.session(worker).options["system_message"]["content"]
    assert "Permission checks are off" not in prompt, "detached; the next use gets the new prompt"

    harness.store.save_settings(Settings(worker_yolo=True))
    other = (await spawn(runtime, harness.router_id, title="Other", instructions="o"))["session_id"]
    harness.store.execute("UPDATE sessions SET slack=1 WHERE id=?", (other,))
    assert await allowed(other), "yolo covers workers that read Slack too"
    assert (
        "Permission checks are off" in harness.session(other).options["system_message"]["content"]
    )
    await runtime.set_session_yolo(other, "off")
    assert not await allowed(other)
    assert not runtime.yolo(harness.store.session(harness.router_id)), "only workers"


async def test_a_failed_setup_leaves_no_worktree_or_branch(tmp_path):
    _, api = published(tmp_path, "api")
    (api / "pkg").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    harness = Harness(tmp_path / "data", Settings(projects=[str(api)]))
    runtime = await harness.connect()
    for isolate in (True, False):
        result = await call_tool(
            runtime,
            "spawn_worker",
            harness.router_id,
            title="Two repos",
            instructions="x",
            project=str(api / "pkg"),
            more_projects=[str(elsewhere)],
            isolate=isolate,
            why_new="test",
        )
        assert "not inside a configured project" in result.text_result_for_llm
    assert run("git", "worktree", "list", cwd=api).count("\n") == 1, "only the owner's checkout"
    assert "wb/" not in run("git", "branch", "--list", cwd=api)


async def test_parallel_add_repo_calls_keep_every_checkout(tmp_path):
    _, api = published(tmp_path, "api")
    _, web = published(tmp_path, "web")
    _, docs = published(tmp_path, "docs")
    harness = Harness(tmp_path / "data", Settings(projects=[str(api), str(web), str(docs)]))
    runtime = await harness.connect()
    worker = (
        await spawn(
            runtime,
            harness.router_id,
            title="Wide",
            instructions="x",
            project=str(api),
            isolate=True,
        )
    )["session_id"]
    await asyncio.gather(
        call_tool(runtime, "add_repo", worker, repo=str(web)),
        call_tool(runtime, "add_repo", worker, repo=str(docs)),
    )
    names = [Path(c["path"]).name for c in json.loads(harness.store.session(worker)["repos"])]
    assert sorted(names) == ["api", "docs", "web"]


async def test_commands_never_wait_on_a_terminal(monkeypatch):
    seen = {}

    async def fake_exec(*arguments, **options):
        seen.update(options)
        return SimpleNamespace(
            returncode=0, pid=1, communicate=lambda: asyncio.sleep(0, (b"ok", b""))
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    assert await runtime_module.git("fetch") == "ok"
    assert seen["start_new_session"] and seen["stdin"] == asyncio.subprocess.DEVNULL
    assert seen["env"]["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


async def test_the_router_hears_when_projects_change(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    router = harness.session(harness.router_id)
    await runtime.owner_send(harness.router_id, "hi")
    assert router.sent[-1] == env("owner", "message", "hi"), "nothing to say the first time"
    harness.store.save_settings(Settings(projects=[str(app)]))
    await runtime.owner_send(harness.router_id, "work on app")
    assert router.sent[-1].endswith(
        env("system", "context", f"The configured projects are now:\n- {app.resolve()}")
    )
    await runtime.owner_send(harness.router_id, "again")
    assert "configured projects" not in router.sent[-1]
