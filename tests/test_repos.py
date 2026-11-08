import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import Harness, call_tool, env, said, settle, task_of

from chiefofstaff import workspaces as workspaces_module
from chiefofstaff.models import Settings
from chiefofstaff.runtime import GITHUB_HOSTS
from chiefofstaff.store import Store
from chiefofstaff.task_actions import moved


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
    harness = Harness(tmp_path / "data", Settings(projects=[str(api), str(web)], worker_yolo=False))
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
        decision = await runtime.events.on_permission(request, {"session_id": row["id"]})
        return type(decision).__name__ == "PermissionDecisionApproveOnce"

    assert await may_write(checkouts[1]["path"] + "/src/a.ts")
    assert await may_write(str(workspace / "notes.md"))
    assert not await may_write(str(web / "a.ts")), "not the owner's own checkout"
    prompt = harness.session(row["id"]).options["system_message"]["content"]
    assert f"Your workspace: {workspace}" in prompt and checkouts[1]["path"] in prompt


async def test_add_repo_from_this_machine_or_a_remote(tmp_path):
    origin, api = published(tmp_path, "api")
    _, web = published(tmp_path, "web")
    _, docs = published(tmp_path, "docs")
    harness = Harness(
        tmp_path / "data", Settings(projects=[str(api), str(web), str(docs)], worker_sandbox=True)
    )
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
    assert "end this turn" in added["note"], "its sandbox can write that history next turn"
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    assert worker in harness.client.resumed, "reconnected with the new sandbox"
    assert "Go on with the task" in harness.session(worker).sent[-1]
    third = await call_tool(runtime, "add_repo", worker, repo=str(docs))
    assert "note" in json.loads(third.text_result_for_llm)
    sent = len(harness.session(worker).sent)
    await runtime.workers.interrupt(worker)  # The owner stops it before the turn ends.
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    assert len(harness.session(worker).sent) == sent, "stopped means stopped"
    again = await call_tool(runtime, "add_repo", worker, repo=str(web))
    assert "already have" in again.text_result_for_llm

    same = await call_tool(runtime, "add_repo", worker, repo=f"file://{origin}")
    assert "already have" in same.text_result_for_llm, "the project with that origin is used"
    lib, _ = published(tmp_path, "lib")  # Not a project: it is cloned.
    cloned = json.loads(
        (await call_tool(runtime, "add_repo", worker, repo=f"file://{lib}")).text_result_for_llm
    )
    assert Path(cloned["path"]).parent == Path(added["path"]).parent
    assert run("git", "branch", "--show-current", cwd=cloned["path"]).strip() == info["branch"]
    assert (Path(cloned["path"]) / "LATEST.md").exists()
    assert len(json.loads(harness.store.session(worker)["repos"])) == 4
    bad = await call_tool(runtime, "add_repo", worker, repo="not a repo")
    assert "git URL or GitHub owner/name" in bad.text_result_for_llm

    reader = (await spawn(runtime, harness.router_id, title="Answer Bob", instructions="y"))[
        "session_id"
    ]
    harness.store.execute("UPDATE sessions SET slack=1 WHERE id=?", (reader,))
    refused = await call_tool(runtime, "add_repo", reader, repo=f"file://{origin}")
    assert "only add configured projects" in refused.text_result_for_llm


async def test_yolo_skips_permission_checks_globally_or_per_worker(tmp_path):
    harness = Harness(tmp_path / "data", Settings(worker_sandbox=True, worker_yolo=False))
    runtime = await harness.connect()
    worker = (await spawn(runtime, harness.router_id, title="Ship", instructions="s"))["session_id"]
    push = SimpleNamespace(kind="shell", full_command_text="git push --force", possible_urls=[])

    async def allowed(session_id):
        decision = await runtime.events.on_permission(push, {"session_id": session_id})
        return type(decision).__name__ == "PermissionDecisionApproveOnce"

    assert not await allowed(worker)
    view = await runtime.set_session_yolo(worker, "on")
    assert view["yolo"] and view["yolo_choice"] == "on"
    assert await allowed(worker)
    prompt = harness.session(worker).options["system_message"]["content"]
    assert "(yolo)" not in prompt, "detached; the next use gets the new prompt"

    harness.store.save_settings(Settings(worker_yolo=True, worker_sandbox=True))
    other = (await spawn(runtime, harness.router_id, title="Other", instructions="o"))["session_id"]
    harness.store.execute("UPDATE sessions SET slack=1 WHERE id=?", (other,))
    assert not await allowed(other), "workers that read Slack keep the checks by default"
    await runtime.set_session_yolo(other, "on")
    assert await allowed(other), "unless chosen for that worker"
    assert (
        "Permission prompts are off for you (yolo); the sandbox still applies"
        in harness.session(other).options["system_message"]["content"]
    )
    await runtime.set_session_yolo(other, "off")
    assert not await allowed(other)
    harness.store.save_settings(Settings(worker_sandbox=True))
    third = (await spawn(runtime, harness.router_id, title="Third", instructions="t"))["session_id"]
    assert runtime.yolo(harness.store.session(third)), "on by default"
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
    assert "chief/" not in run("git", "branch", "--list", cwd=api)


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
            returncode=0, pid=1, communicate=lambda input=None: asyncio.sleep(0, (b"ok", b""))
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    assert await workspaces_module.git("fetch") == "ok"
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
    assert router.sent[-1] == said("hi"), "nothing to say the first time"
    harness.store.save_settings(Settings(projects=[str(app)]))
    await runtime.owner_send(harness.router_id, "work on app")
    assert router.sent[-1].endswith(
        env("system", "context", f"The configured projects are now:\n- {app.resolve()}")
    )
    await runtime.owner_send(harness.router_id, "again")
    assert "configured projects" not in router.sent[-1]


def owner_worktree_and_submodule(repo: Path, where: Path):
    """The owner's own worktree and a submodule: their configs run code when the owner uses git."""
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "mine", str(where)], check=True
    )
    (repo / ".git" / "modules" / "lib").mkdir(parents=True)


def own_git_dir(checkout: str) -> Path:
    return Path((Path(checkout) / ".git").read_text().removeprefix("gitdir:").strip()).resolve()


def resolved(paths) -> set[Path]:
    return {Path(path).resolve() for path in paths}


async def test_workers_run_sandboxed_in_their_own_folders(tmp_path, git_repo):
    harness = Harness(
        tmp_path / "data",
        Settings(projects=[str(git_repo)], slack_enabled=True, worker_sandbox=True),
    )
    runtime = await harness.connect()
    assert "sandbox_config" not in harness.session(harness.router_id).options
    owner_worktree_and_submodule(git_repo, tmp_path / "mine")
    info = await spawn(
        runtime,
        harness.router_id,
        title="Fix",
        instructions="fix",
        project=str(git_repo),
        isolate=True,
    )
    worker = harness.session(info["session_id"])
    policy = worker.options["sandbox_config"]
    git_dir = str(git_repo.resolve() / ".git")
    workspace = str(runtime.workspaces.workspace(harness.store.session(info["session_id"])))
    assert {info["cwd"], workspace, git_dir} <= set(policy.readwrite)
    protected = {f"{git_dir}/{name}" for name in ("hooks", "config", "modules", "worktrees/mine")}
    assert protected <= set(policy.readonly), "these run code outside the sandbox: never writable"
    assert own_git_dir(info["cwd"]) not in resolved(policy.readonly), "its own stays writable"
    assert policy.allowed_hosts == ()
    prompt = worker.options["system_message"]["content"]
    assert "OS sandbox" in prompt and "without -u" in prompt

    slack = await spawn(runtime, harness.router_id, title="Reply", instructions="r", slack=True)
    assert harness.session(slack["session_id"]).options["sandbox_config"].allowed_hosts == (
        GITHUB_HOSTS
    ), "what a Slack reader read can only go to GitHub"

    harness.store.save_settings(
        Settings(projects=[str(git_repo)], worker_sandbox=False, worker_yolo=True)
    )
    loose = await spawn(runtime, harness.router_id, title="Loose", instructions="l")
    options = harness.session(loose["session_id"]).options
    assert "sandbox_config" not in options
    assert "there is no sandbox" in options["system_message"]["content"]


async def test_workers_load_what_the_owners_own_sessions_load(tmp_path, git_repo, monkeypatch):
    """Plugins, the repository's MCP servers, skills, agents and past sessions, as in a session
    the owner starts in a terminal; no sandbox of Chief of Staff's unless the worker reads
    Slack. A worktree of a repository the owner trusts in Copilot is trusted too (Copilot only
    loads a repository's .mcp.json in a trusted folder)."""
    home = tmp_path / "copilot-home"
    home.mkdir()
    (home / "config.json").write_text(
        "// managed by Copilot\n" + json.dumps({"trustedFolders": [str(git_repo)]})
    )
    monkeypatch.setenv("COPILOT_HOME", str(home))
    plain = Harness(tmp_path / "plain", Settings(projects=[str(git_repo)]))
    runtime = await plain.connect()
    info = await spawn(runtime, plain.router_id, title="Fix", instructions="f",
                       project=str(git_repo), isolate=True)  # fmt: skip
    assert "trust_working_directory" not in plain.session(info["session_id"]).options, (
        "no MCP servers or hooks of its own: nothing to trust it for"
    )
    commit(git_repo, ".mcp.json", '{"mcpServers": {}}')
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], slack_enabled=True))
    runtime = await harness.connect()
    info = await spawn(
        runtime,
        harness.router_id,
        title="Fix",
        instructions="f",
        project=str(git_repo),
        isolate=True,
    )
    options = harness.session(info["session_id"]).options
    assert "sandbox_config" not in options
    assert options["enable_config_discovery"] and options["enable_session_store"]
    assert options["trust_working_directory"] == "user", "its project is trusted"
    router = harness.session(harness.router_id).options
    assert "enable_config_discovery" not in router and "trust_working_directory" not in router

    await runtime.detach(info["session_id"])
    await runtime.session_for(info["session_id"])
    assert "trust_working_directory" not in harness.session(info["session_id"]).options, (
        "a resume keeps the trust Copilot stored"
    )

    reader = await spawn(runtime, harness.router_id, title="Reply", instructions="r", slack=True)
    options = harness.session(reader["session_id"]).options
    assert options["sandbox_config"].allowed_hosts == GITHUB_HOSTS, "Slack readers stay sandboxed"
    assert options["enable_config_discovery"] and not options["enable_session_store"]

    (home / "config.json").write_text(json.dumps({"trustedFolders": [str(tmp_path / "other")]}))
    untrusted = await spawn(
        runtime,
        harness.router_id,
        title="Two",
        instructions="t",
        project=str(git_repo),
        isolate=True,
    )
    assert "trust_working_directory" not in harness.session(untrusted["session_id"]).options


def test_existing_installs_switch_to_unsandboxed_workers_once(tmp_path):
    away = tmp_path / "on-a-drive"
    away.mkdir()
    store = Store(tmp_path / "data")
    store.save_settings(Settings(worker_sandbox=True, projects=[str(away)]))
    store.delete("migrated:worker-parity")  # As saved by 0.1.12, when it was the default.
    store.close()
    away.rmdir()  # A drive not mounted at the first start after the upgrade.
    store = Store(tmp_path / "data")
    assert not store.settings().worker_sandbox
    away.mkdir()
    assert store.settings().projects == [str(away)], "nothing else of the settings changes"
    store.save_settings(Settings(worker_sandbox=True))  # The owner turns it back on.
    store.close()
    assert Store(tmp_path / "data").settings().worker_sandbox


async def test_receipts_show_what_was_observed_next_to_the_report(tmp_path, git_repo):
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    info = await spawn(
        runtime,
        harness.router_id,
        title="Fix",
        instructions="fix",
        project=str(git_repo),
        isolate=True,
    )
    worker, tree = harness.session(info["session_id"]), Path(info["cwd"])
    (tree / "fix.py").write_text("print('fixed')\n")
    run("git", "add", ".", cwd=tree)
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "Fix", cwd=tree)
    for call, code in (("c1", 1), ("c2", 0)):
        worker.emit(
            "tool.execution_start",
            tool_call_id=call,
            tool_name="bash",
            arguments={"command": "uv run pytest -q"},
        )
        worker.emit(
            "tool.execution_complete",
            tool_call_id=call,
            success=code == 0,
            result=SimpleNamespace(content=f"...\n<shellId: 1 completed with exit code {code}>"),
        )
    harness.gh_replies["pr view https://github.com/acme/app/pull/7"] = json.dumps(
        {
            "number": 7,
            "url": "https://github.com/acme/app/pull/7",
            "state": "OPEN",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "reviewDecision": "",
        }
    )
    await call_tool(
        runtime,
        "report",
        info["session_id"],
        status="done",
        summary="Fixed it; all tests pass. https://github.com/acme/app/pull/7",
    )
    await asyncio.sleep(0.3)  # Evidence is collected in the background (git, gh).
    for _ in range(50):
        view = runtime.task_actions.view(runtime.tasks.get(info["task_id"]))
        if view["receipts"] and view["receipts"]["checkouts"]:
            break
        await asyncio.sleep(0.05)
    found = view["receipts"]
    assert found["tests"] == {
        "command": "uv run pytest -q",
        "exit_code": 0,
        "passed": True,
        "runs": 2,
        "failed_before": True,
        "running": False,
    }
    assert found["changed"]["files"] == 1 and found["changed"]["commits"] == 1
    assert found["prs"][0]["checks"] == "passing" and found["flags"] == []
    report = runtime.task_actions.report_markdown(info["task_id"])
    assert "- Tests: `uv run pytest -q` passed" in report
    assert "[acme/app#7](https://github.com/acme/app/pull/7): checks passing" in report
    assert "Orchestrated with [Chief of Staff]" in report

    claim = await spawn(runtime, harness.router_id, title="Claim", instructions="c")
    await call_tool(
        runtime, "report", claim["session_id"], status="done", summary="All tests pass."
    )
    worker = harness.session(claim["session_id"])
    worker.emit(
        "tool.execution_start", tool_call_id="x", tool_name="bash", arguments={"command": "ls"}
    )
    worker.emit("tool.execution_complete", tool_call_id="x", success=True, result=None)
    await settle(runtime)
    flags = runtime.task_actions.view(runtime.tasks.get(claim["task_id"]))["receipts"]["flags"]
    assert flags == ["Says tests pass, but no test run was observed."]


async def test_the_code_folder_holds_projects_task_folders_and_clones(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    api_origin, _ = published(remote, "api")
    lib_origin, _ = published(remote, "lib")
    code = tmp_path / "code"
    run("git", "clone", "-q", str(api_origin), str(code / "group" / "api"))
    for where in (".hidden/old", "node_modules/dep", "group/api/vendor/inner"):
        (code / where).mkdir(parents=True)
        run("git", "init", "-q", str(code / where))
    (code / "notes").mkdir()
    harness = Harness(tmp_path / "data", Settings(code_folder=str(code)))
    runtime = await harness.connect()
    api = str((code / "group" / "api").resolve())
    assert runtime.workspaces.projects() == [api], "two levels deep; not hidden, deps or nested"

    first = await spawn(
        runtime, harness.router_id, title="Fix", instructions="x", project=api, isolate=True
    )
    assert Path(first["cwd"]).is_relative_to(code.resolve() / ".chief"), "its own task folder"
    added = json.loads(
        (
            await call_tool(runtime, "add_repo", first["session_id"], repo=f"file://{lib_origin}")
        ).text_result_for_llm
    )
    lib = code / "lib"
    assert (lib / ".git").is_dir(), "a repository not on this machine is cloned into the folder"
    assert Path(added["path"]).parent != lib and added["branch"] == first["branch"]
    assert str(lib.resolve()) in runtime.workspaces.projects()

    second = await spawn(runtime, harness.router_id, title="Docs", instructions="y")
    await call_tool(runtime, "add_repo", second["session_id"], repo=f"file://{lib_origin}")
    assert sorted(path.name for path in code.iterdir()) == [
        ".chief",
        ".hidden",
        "group",
        "lib",
        "node_modules",
        "notes",
    ], "the second task reuses that clone"
    projects = await call_tool(runtime, "list_sessions", harness.router_id)
    assert str(lib.resolve()) in projects.text_result_for_llm


def exists(path) -> bool:
    return Path(path).exists()


async def test_closing_a_worker_removes_its_clean_worktrees_and_reopening_restores_them(
    tmp_path, git_repo
):
    code = tmp_path / "code"
    run("git", "clone", "-q", str(git_repo), str(code / "app"))
    harness = Harness(tmp_path / "data", Settings(code_folder=str(code)))
    runtime = await harness.connect()
    app = str((code / "app").resolve())
    clean, busy = [
        await spawn(
            runtime, harness.router_id, title=title, instructions=title, project=app, isolate=True
        )
        for title in ("Clean", "Busy")
    ]
    await settle(runtime)
    (Path(busy["cwd"]) / "draft.txt").write_text("not committed yet")
    for worker in (clean, busy):
        await runtime.workers.close(worker["session_id"])
    await settle(runtime)
    assert not exists(clean["cwd"]), "a clean worktree goes"
    assert not exists(Path(clean["cwd"]).parent), "and its empty task folder"
    assert exists(Path(busy["cwd"]) / "draft.txt"), "uncommitted work is never removed"
    branches = run("git", "branch", "--format=%(refname:short)", cwd=app).split()
    assert clean["branch"] in branches, "the branch stays"

    await runtime.workers.reopen(clean["session_id"])
    assert run("git", "branch", "--show-current", cwd=clean["cwd"]).strip() == clean["branch"]


async def failure(coroutine) -> Exception | None:
    try:
        await coroutine
    except Exception as exc:
        return exc
    return None


async def test_the_inbox_adds_a_repository_or_a_folder_of_them(tmp_path, git_repo):
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    folder = tmp_path / "code"
    run("git", "clone", "-q", str(git_repo), str(folder / "app"))
    assert "own data" in str(await failure(runtime.add_folder(str(tmp_path))))
    added = await runtime.add_folder(str(folder))
    assert added == {"path": str(folder.resolve()), "kind": "code_folder", "repositories": 1}
    assert harness.store.settings().code_folder == str(folder.resolve())
    with pytest.raises(ValueError, match="is a repository"):
        Settings(code_folder=str(folder / "app"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert "Your code folder is" in str(await failure(runtime.add_folder(str(elsewhere))))
    other = tmp_path / "other"
    other.mkdir()
    run("git", "init", "-q", str(other))
    assert (await runtime.add_folder(str(other)))["kind"] == "project"


async def test_a_worker_reopened_right_after_closing_keeps_its_folders(tmp_path, git_repo):
    code = tmp_path / "code"
    run("git", "clone", "-q", str(git_repo), str(code / "app"))
    harness = Harness(tmp_path / "data", Settings(code_folder=str(code)))
    runtime = await harness.connect()
    app = str((code / "app").resolve())
    workers = [
        await spawn(runtime, harness.router_id, title="Notes", instructions="n"),
        await spawn(runtime, harness.router_id, title="Fix", instructions="f", project=app,
                    isolate=True),
    ]  # fmt: skip
    await settle(runtime)
    for worker in workers:
        await runtime.workers.close(worker["session_id"])
        # The removal started by closing is still pending when the worker is reopened.
        await runtime.workers.reopen(worker["session_id"])
    await settle(runtime)
    for worker in workers:
        assert exists(worker["cwd"]), "the removal saw that it was reopened"
        assert not harness.store.session(worker["session_id"])["closed_at"]


async def test_new_worktrees_get_the_gitignored_files_they_need(tmp_path, git_repo):
    code = tmp_path / "code"
    app = code / "app"
    (git_repo / ".gitignore").write_text(".env*\nsecret.json\n!.env.example\n")
    (git_repo / ".env.example").write_text("KEY=\n")
    run("git", "add", ".", cwd=git_repo)
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "ignore", cwd=git_repo)
    run("git", "clone", "-q", str(git_repo), str(app))
    (app / ".env").write_text("KEY=1\n")
    (app / "web").mkdir()
    (app / "web" / ".env.local").write_text("PORT=1\n")
    (app / "secret.json").write_text("{}")
    (app / "notes.txt").write_text("untracked, not ignored")
    harness = Harness(tmp_path / "data", Settings(code_folder=str(code), slack_enabled=True))
    runtime = await harness.connect()
    project = str(app.resolve())

    first = await spawn(runtime, harness.router_id, title="A", instructions="a", project=project,
                        isolate=True)  # fmt: skip
    tree = Path(first["cwd"])
    assert exists(tree / ".env") and exists(tree / "web" / ".env.local"), ".env files by default"
    assert not exists(tree / "secret.json") and not exists(tree / "notes.txt")
    prompt = harness.session(first["session_id"]).options["system_message"]["content"]
    assert "(gitignored): .env, web/.env.local" in prompt and "fresh checkout" in prompt

    (app / ".worktreeinclude").write_text("secret.json\nnotes.txt\n")
    second = await spawn(runtime, harness.router_id, title="B", instructions="b", project=project,
                         isolate=True)  # fmt: skip
    tree = Path(second["cwd"])
    assert exists(tree / "secret.json"), "what .worktreeinclude lists"
    assert not exists(tree / ".env"), ".worktreeinclude replaces the default"
    assert not exists(tree / "notes.txt"), "only gitignored files: never ones git would track"

    reader = await spawn(runtime, harness.router_id, title="C", instructions="c", project=project,
                         isolate=True, slack=True)  # fmt: skip
    assert not exists(Path(reader["cwd"]) / "secret.json"), "workers that read Slack get none"

    await runtime.workers.close(second["session_id"])
    await settle(runtime)
    (Path(second["cwd"]) / "secret.json").unlink(missing_ok=True)
    await runtime.workers.reopen(second["session_id"])
    assert exists(Path(second["cwd"]) / "secret.json"), "copied again when the worktree returns"


async def test_create_pr_pushes_the_branch_and_the_worker_follows_the_pr(tmp_path):
    origin, app = published(tmp_path, "app")
    # The repository is acme/app on GitHub to Chief of Staff; git really talks to `origin`.
    run("git", "config", "remote.origin.url", "https://github.com/acme/app.git", cwd=app)
    run("git", "config", f"url.{origin}.insteadOf", "https://github.com/acme/app.git", cwd=app)
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    pr = "https://github.com/acme/app/pull/9"
    calls, open_prs = [], []

    async def gh(*arguments, payload=None):
        calls.append(arguments)
        if arguments[:2] == ("pr", "list"):
            return json.dumps([{"url": url} for url in open_prs])
        if arguments[:2] == ("pr", "create"):
            return f"Creating pull request\n{pr}\n"
        assert arguments[:2] == ("pr", "view")
        return json.dumps({"url": pr, "state": "OPEN", "statusCheckRollup": []})

    runtime.github.gh = gh
    worker = await spawn(runtime, harness.router_id, title="Fix login", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    worker_id, tree = worker["session_id"], Path(worker["cwd"])
    commit(tree, "login.py", "def login():\n    return True\n")
    (tree / "notes.md").write_text("draft\n")
    await call_tool(runtime, "report", worker_id, status="done", summary="Fixed the login.")
    await settle(runtime)
    task_id = task_of(harness, worker_id)

    [found] = await runtime.task_actions.diff(task_id)
    assert "+    return True" in found["diff"], "its commits"
    assert "+++ b/notes.md\n@@ -0,0 +1,1 @@\n+draft" in found["diff"], "and new files"
    with pytest.raises(ValueError, match="uncommitted changes"):
        await runtime.task_actions.create_pr(task_id)
    assert not calls, "nothing is pushed or opened while work is uncommitted"

    (tree / "notes.md").unlink()
    task = await runtime.task_actions.create_pr(task_id)
    branch = run("git", "branch", "--show-current", cwd=tree).strip()
    pushed = run("git", f"--git-dir={origin}", "rev-parse", f"refs/heads/{branch}")
    assert pushed == run("git", "rev-parse", "HEAD", cwd=tree)
    created = next(call for call in calls if call[:2] == ("pr", "create"))
    assert created[created.index("--repo") + 1] == "acme/app"
    assert created[created.index("--head") + 1] == branch
    assert created[created.index("--title") + 1] == "Fix login"
    assert "Fixed the login." in created[created.index("--body") + 1]
    assert harness.store.follows(worker_id) == ["github:acme/app#9"], "its PR comes to it"
    stale = runtime.task_actions.view(task)["receipts"]["stale"]
    assert stale.startswith("Out of date"), "checked with notes.md, opened without it"
    assert [p["url"] for p in runtime.task_actions.view(task)["receipts"]["prs"]] == [pr]

    calls.clear()
    open_prs.append(pr)
    await runtime.task_actions.create_pr(task_id)
    assert not any(call[:2] == ("pr", "create") for call in calls), "an open PR is not doubled"

    await runtime.owner_send(worker_id, "Is CI green?")  # A question, before the merge.
    runtime.unfollow("github:acme/app#9", finished=True, merged=True)
    assert runtime.tasks.get(task_id)["status"] == "done", "merged, its task is done"
    await call_tool(runtime, "report", worker_id, status="done", summary="Merged, all good.")
    assert runtime.tasks.get(task_id)["status"] == "done", "the question does not reopen it"


async def test_editing_a_new_file_changes_the_version(tmp_path, git_repo):
    checkout = {"path": str(git_repo)}
    (git_repo / "new_test.py").write_text("assert True\n")
    (git_repo / "nested").mkdir()
    run("git", "init", "-q", cwd=git_repo / "nested")  # Listed as "nested/".
    (git_repo / "gone").symlink_to(git_repo / "missing")  # A broken link.
    first = await workspaces_module.version(checkout)
    (git_repo / "new_test.py").write_text("assert False\n")
    assert (await workspaces_module.version(checkout))["edits"] != first["edits"]
    older = {"path": "p", "commit": "abc", "edits": "from-0.1.9"}  # No digest kind.
    assert not moved([older], [{"path": "p", "commit": "abc", "edits": "x", "digest": 2}]), (
        "an older digest is not compared: no false out-of-date after upgrading"
    )
    assert moved(
        [{**older, "digest": 2}], [{"path": "p", "commit": "abc", "edits": "x", "digest": 2}]
    )


async def test_a_closed_workers_changes_are_still_shown(tmp_path):
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Fix", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    commit(Path(worker["cwd"]), "fix.py", "fixed = True\n")
    await call_tool(runtime, "report", worker["session_id"], status="done", summary="Fixed.")
    await runtime.workers.close(worker["session_id"])
    await settle(runtime)
    assert not exists(Path(worker["cwd"])), "its clean worktree was removed"
    task_id = task_of(harness, worker["session_id"])
    [found] = await runtime.task_actions.diff(task_id)
    assert "+fixed = True" in found["diff"] and "README" not in found["diff"]
    await runtime.task_actions.collect_evidence(task_id)
    changed = runtime.task_actions.view(runtime.tasks.get(task_id))["receipts"]["changed"]
    assert changed["files"] == 1 and changed["commits"] == 1


async def receipts_of(runtime, task_id, ready=lambda found: bool(found["version"])):
    for _ in range(100):
        found = runtime.task_actions.view(runtime.tasks.get(task_id))["receipts"]
        if found and ready(found):
            return found
        await asyncio.sleep(0.03)
    raise AssertionError(f"No receipts for {task_id}: {found}")


async def test_evidence_is_about_one_version_of_the_code(tmp_path, git_repo):
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    info = await spawn(runtime, harness.router_id, title="Fix orders", instructions="fix",
                       project=str(git_repo), isolate=True,
                       done_when=["`pytest tests/test_orders.py` passes"])  # fmt: skip
    worker, tree = harness.session(info["session_id"]), Path(info["cwd"])
    assert "Done when (check each yourself" in worker.sent[0]
    assert "- `pytest tests/test_orders.py` passes" in worker.sent[0]
    commit(tree, "orders.py", "fixed = True\n")
    worker.emit("tool.execution_start", tool_call_id="c1", tool_name="bash",
                arguments={"command": "pytest tests/test_orders.py"})  # fmt: skip
    worker.emit("tool.execution_complete", tool_call_id="c1", success=True,
                result=SimpleNamespace(content="6 passed\n<exited with exit code 0>"))  # fmt: skip
    criteria = [{"criterion": "`pytest tests/test_orders.py` passes", "result": "met",
                 "evidence": "`pytest tests/test_orders.py`: 6 passed"}]  # fmt: skip
    await call_tool(runtime, "report", info["session_id"], status="done", summary="Fixed.",
                    criteria=criteria)  # fmt: skip
    found = await receipts_of(runtime, info["task_id"])
    head = run("git", "rev-parse", "HEAD", cwd=tree).strip()
    assert found["verdict"] == "verified" and found["stale"] == ""
    assert found["version"] == [{"branch": info["branch"], "commit": head[:12], "edited": False}]
    report = runtime.task_actions.report_markdown(info["task_id"])
    assert (
        "- ✅ `pytest tests/test_orders.py` passes (verified: `pytest tests/test_orders.py`)"
        in (report)
    )

    (tree / "orders.py").write_text("fixed = False\n")  # Edited after it was checked.
    worker.emit("session.idle")
    found = await receipts_of(runtime, info["task_id"], lambda found: bool(found["stale"]))
    assert found["stale"] == "Out of date: the code was edited since it was checked."
    assert found["flags"][0] == found["stale"]


async def test_a_worker_in_the_owners_folder_gets_a_worktree_to_change_it(tmp_path, git_repo):
    """A worker that analysed a project in its folder is the one to change it next: it adds
    the project and works in a worktree of its own, never in the owner's checkout."""
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    info = await spawn(runtime, harness.router_id, title="Analyse", instructions="a",
                       project=str(git_repo))  # fmt: skip
    assert info["cwd"] == str(git_repo) and not info.get("branch")
    prompt = harness.session(info["session_id"]).options["system_message"]["content"]
    assert "change nothing there" in prompt and "first add_repo this folder" in prompt
    added = await call_tool(runtime, "add_repo", info["session_id"], repo=str(git_repo))
    result = json.loads(added.text_result_for_llm)
    assert result["branch"].startswith("chief/") and result["path"] != str(git_repo)
    assert await asyncio.to_thread(lambda: (Path(result["path"]) / ".git").exists())
    head = await asyncio.to_thread(
        subprocess.run, ["git", "-C", str(git_repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )  # fmt: skip
    assert head.stdout.strip() == "main", "the owner's checkout stays on its branch"


async def test_receipts_name_the_branch_a_worker_is_on_now(git_repo):
    run("git", "branch", "chief/fix", cwd=git_repo)
    run("git", "checkout", "-q", "-b", "their-feature", cwd=git_repo)  # A PR checked out to review.
    found = await workspaces_module.changes({"path": str(git_repo), "branch": "chief/fix"})
    assert found and found["branch"] == "their-feature", "not one of its own to open a PR from"


async def test_a_later_task_on_the_same_branch_shows_only_its_own_changes(tmp_path):
    """Bug bash #3: a worker's second task on its branch claimed the first one's commit; a
    read-only question after them showed both and asked to be accepted."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Docstring", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id = Path(worker["cwd"]), worker["session_id"]
    commit(tree, "doc.py", "doc = True\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Docstring added.")
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    task="new", title="Fix chunk", text="Fix chunk")  # fmt: skip
    chunk = task_of(harness, session_id)
    await settle(runtime)
    for _ in range(50):
        if runtime.store.get(f"taskbase:{chunk}"):
            break
        await asyncio.sleep(0.05)
    commit(tree, "chunk.py", "chunk = True\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Fixed chunk.")
    changed = (await receipts_of(runtime, chunk))["changed"]
    assert changed["commits"] == 1 and changed["files"] == 1, changed
    [found] = await runtime.task_actions.diff(chunk)
    assert "+chunk = True" in found["diff"] and "doc = True" not in found["diff"]
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    task="new", title="Count tests", text="How many tests?")  # fmt: skip
    question = task_of(harness, session_id)
    await settle(runtime)
    for _ in range(50):
        if runtime.store.get(f"taskbase:{question}"):
            break
        await asyncio.sleep(0.05)
    await call_tool(runtime, "report", session_id, status="done", summary="2 tests.")
    changed = (await receipts_of(runtime, question))["changed"]
    assert not (changed["commits"] or changed["files"] or changed["uncommitted"]), changed


async def test_in_the_owners_folder_only_files_its_edits_touched_are_its_changes(tmp_path):
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Note", instructions="x",
                         project=str(app))  # fmt: skip
    session_id = worker["session_id"]
    await settle(runtime)
    for _ in range(50):
        if runtime.store.get(f"taskbase:{task_of(harness, session_id)}"):
            break
        await asyncio.sleep(0.05)
    (app / "theirs.txt").write_text("another worker's file\n")
    (app / "mine.txt").write_text("this task's file\n")
    harness.session(session_id).emit(
        "tool.execution_start", tool_call_id="e1", tool_name="create",
        arguments={"path": str(app / "mine.txt"), "file_text": "x"},
    )  # fmt: skip
    await call_tool(runtime, "report", session_id, status="done", summary="Wrote mine.txt.")
    task = task_of(harness, session_id)
    changed = (await receipts_of(runtime, task))["changed"]
    assert changed["files"] == 1 and changed["uncommitted"] == 1, changed
    [found] = await runtime.task_actions.diff(task)
    assert "mine.txt" in found["diff"] and "theirs.txt" not in found["diff"]


async def test_a_project_is_also_known_by_its_package_name(tmp_path):
    """Bug bash #35: asked about "textkit", the Router asked which folder (it was "project")."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text('[build-system]\nx = 1\n[project]\nname = "textkit"\n')
    same = tmp_path / "web"
    same.mkdir()
    (same / "package.json").write_text('{"name": "@acme/web", "version": "1"}')
    plain = tmp_path / "notes"
    plain.mkdir()
    harness = Harness(tmp_path / "data", Settings(projects=[str(project), str(same), str(plain)]))
    runtime = await harness.connect()
    cards = {card["name"]: card for card in await runtime.workspaces.project_cards()}
    assert cards["project"]["package"] == "textkit"
    assert "package" not in cards["web"] and "package" not in cards["notes"]


async def test_a_tasks_new_folder_and_merged_upstream_work_are_counted_right(tmp_path):
    """Code review: a new folder's files were missing from a task's diff; commits merged in
    from origin counted as the task's own, and so did a teammate's lines merged into a file the
    task changed too."""
    origin, app = published(tmp_path, "app")
    teammate = tmp_path / "teammate"
    run("git", "clone", "-q", str(origin), str(teammate))
    lines = [f"setting_{n} = {n}" for n in range(12)]
    commit(teammate, "config.py", "\n".join(lines) + "\n")
    run("git", "push", "-q", "origin", "main", cwd=teammate)
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="First", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id = Path(worker["cwd"]), worker["session_id"]
    assert (tree / "config.py").exists()
    commit(tree, "first.py", "first = True\n")
    await call_tool(runtime, "report", session_id, status="done", summary="First.")
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    task="new", title="Feature", text="Add the feature")  # fmt: skip
    feature = task_of(harness, session_id)
    await settle(runtime)
    for _ in range(50):
        if runtime.store.get(f"taskbase:{feature}"):
            break
        await asyncio.sleep(0.05)
    for n in range(3):
        commit(teammate, f"team{n}.py", f"team = {n}\n")
    commit(teammate, "config.py", "\n".join(["setting_0 = 'team'", *lines[1:]]) + "\n")
    run("git", "push", "-q", "origin", "main", cwd=teammate)
    commit(tree, "config.py", "\n".join([*lines[:-1], "setting_11 = 'mine'"]) + "\n")
    run("git", "fetch", "-q", "origin", cwd=tree)
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "merge", "-q", "--no-edit",
        "origin/main", cwd=tree)  # fmt: skip
    assert "setting_0 = 'team'" in (tree / "config.py").read_text()
    commit(tree, "feature.py", "feature = True\n")
    (tree / "newpkg").mkdir()
    (tree / "newpkg" / "__init__.py").write_text("")
    (tree / "newpkg" / "core.py").write_text("core = 1\nmore = 2\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Feature added.")
    changed = (await receipts_of(runtime, feature))["changed"]
    assert changed["commits"] == 2, changed
    assert changed["files"] == 4 and changed["uncommitted"] == 2, changed
    assert changed["insertions"] == 4 and changed["deletions"] == 1, changed
    [found] = await runtime.task_actions.diff(feature)
    diff = found["diff"]
    assert "+feature = True" in diff and "+core = 1" in diff and "newpkg/__init__.py" in diff
    assert "+setting_11 = 'mine'" in diff and "setting_0 = 'team'" not in diff
    assert "team0.py" not in diff and "first.py" not in diff
