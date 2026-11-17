import asyncio
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeSession, Harness, call_tool, env, said, settle, task_of

from chiefofstaff import events as events_module
from chiefofstaff import policy as policy_module
from chiefofstaff import settings_guard
from chiefofstaff import workers as workers_module
from chiefofstaff import workspaces as workspaces_module
from chiefofstaff.models import Settings
from chiefofstaff.policy import REPOSITORY_SETTINGS, RUN_WRITE, settings_write
from chiefofstaff.runtime import GITHUB_HOSTS, home_shown
from chiefofstaff.store import Store
from chiefofstaff.task_actions import moved


def run(*command, cwd=None, env=None):
    done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, env=env)
    assert done.returncode == 0, f"{command}: {done.stderr}"
    return done.stdout


def commit(repo: Path, name: str, text: str):
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
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
    assert info["cwd"] == checkouts[0]["path"] and Path(info["checkouts"][1]).name == "web"
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
            returncode=0, pid=1, communicate=lambda data=None: asyncio.sleep(0, (b"ok", b""))
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
    protected = {Path(git_dir) / name for name in ("hooks", "config", "modules", "worktrees/mine")}
    assert protected <= {Path(path) for path in policy.readonly}, (
        "these run code outside the sandbox: never writable"
    )
    assert own_git_dir(info["cwd"]) not in resolved(policy.readonly), "its own stays writable"
    assert policy.allowed_hosts == ()
    prompt = worker.options["system_message"]["content"]
    assert "OS sandbox" in prompt and "without -u" in prompt

    slack = await spawn(runtime, harness.router_id, title="Reply", instructions="r", slack=True)
    assert harness.session(slack["session_id"]).options["sandbox_config"].allowed_hosts == (
        GITHUB_HOSTS
    ), "what a Slack reader read can only go to GitHub"
    said = harness.session(slack["session_id"]).options["system_message"]["content"]
    assert "You read Slack, so your sandbox is stricter" in said, "said why (bug bash 6, T4)"
    assert "You read Slack" not in prompt

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


# Agent settings a worker writes in its folder (policy.AGENT_SETTINGS): Copilot loads them in a
# trusted folder (a worktree of a trusted repository is one) from its next turn (an MCP server)
# or start (hooks, which can approve what Chief of Staff refuses), outside its sandbox. Chief of
# Staff keeps what they held when it handed the folder out (settings_guard), in its own store,
# and starts no turn while they differ, until the owner says to use them.

PLANTED = {
    ".github/hooks/p.json": '{"version": 1, "hooks": {"preToolUse": []}}',
    ".github/mcp.json": '{"mcpServers": {"p": {"command": "sh"}}}',
    ".mcp.json": '{"mcpServers": {"p": {"command": "sh"}}}',
    ".github/lsp.json": '{"lspServers": {"p": {"command": "sh", "fileExtensions": {".x": "x"}}}}',
    ".github/copilot/settings.json": '{"hooks": {}}',
    ".github/copilot/settings.local.json": '{"hooks": {}}',
    ".github/extensions/p/extension.mjs": "export {}",
    ".github/agents/p.agent.md": "---\nmcp-servers:\n  p:\n    command: sh\n---\nAn agent.\n",
    ".claude/settings.json": '{"hooks": {}}',
    ".claude/settings.local.json": '{"hooks": {}}',
    ".claude/agents/p.md": "---\nhooks: {}\n---\nAn agent.\n",
}


def trusting(tmp_path, monkeypatch, *folders) -> Path:
    home = tmp_path / "copilot-home"
    home.mkdir(exist_ok=True)
    (home / "config.json").write_text(
        "// managed by Copilot\n" + json.dumps({"trustedFolders": [str(f) for f in folders]})
    )
    monkeypatch.setenv("COPILOT_HOME", str(home))
    return home


def plant(folder: Path, name: str, text: str = ""):
    (folder / name).parent.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text or PLANTED.get(name, "{}"))


def refusal(changes: str) -> str:
    return (
        f"Copilot settings in this folder changed ({changes}); Chief of Staff won't start the "
        "worker there until you say to use them."
    )


def untracked(listed: str) -> str:
    return (
        f"Copilot settings Chief of Staff hasn't checked yet, in a checkout workers have had "
        f"({listed}); Chief of Staff won't start the worker there until you say to use them."
    )


def added_since(listed: str) -> str:
    return (
        f"Copilot settings added since workers had this checkout ({listed}); Chief of Staff "
        "won't start the worker there until you say to use them."
    )


def unused(listed: str, writable: bool = False) -> str:
    could = " but could write" if writable else ""
    return (
        f"Copilot settings in a folder of this checkout workers haven't used yet{could} "
        f"({listed}); look at them once. Chief of Staff won't start the worker there until you "
        "say to use them."
    )


async def asked(hook, call: dict, worker: str):
    """What Chief of Staff's pre-tool hook answers, as the SDK takes it (at once, or awaited)."""
    answer = hook(call, {"session_id": worker})
    return await answer if inspect.isawaitable(answer) else answer


def held(entries: dict) -> list[str]:
    """What a folder's settings run, as held: hooks' files, and servers' ("path@server")."""
    found = []
    for entry in entries:
        _, ran, server = settings_guard.ran_of(entry)
        if ran:
            found.append(f"{ran}@{server}" if server else ran)
    return sorted(found)


def settings_notices(harness, worker) -> list[dict]:
    return [m for m in harness.store.messages(worker) if m["meta"].get("agent_settings")]


async def use_them(runtime, harness, worker) -> str:
    """The owner's "Use these settings" on the latest line."""
    digest = settings_notices(harness, worker)[-1]["meta"]["agent_settings"]["digest"]
    said = await runtime.accept_settings(worker, digest)
    await settle(runtime)
    return said


def unsent(harness, worker) -> list[str]:
    mark = int(harness.store.get(f"sent:{worker}") or 0)
    return [m["text"] for m in harness.store.prompts(worker, after=mark)]


async def at_rest(runtime, harness, worker: str):
    """Its turn over (it reported), and let go of (as after a while idle)."""
    await call_tool(runtime, "report", worker, status="done", summary="Done.")
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    await runtime.detach(worker)


async def isolated(runtime, harness, project: Path, **more) -> tuple[str, Path]:
    info = await spawn(runtime, harness.router_id, title="Fix", instructions="f",
                       project=str(project), isolate=True, **more)  # fmt: skip
    return info["session_id"], Path(info["cwd"])


@pytest.mark.parametrize("name", PLANTED)
async def test_a_worker_with_agent_settings_it_wrote_starts_no_turn_until_the_owner_says(
    tmp_path, git_repo, monkeypatch, name
):
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await at_rest(runtime, harness, worker)
        plant(cwd, name)
        await runtime.owner_send(worker, "Go on")
        assert not runtime.states.is_live(worker), "not opened there"
        (notice,) = settings_notices(harness, worker)
        assert notice["text"] == refusal(f"added {name}")
        assert notice["meta"]["agent_settings"]["changes"] == f"added {name}"
        assert unsent(harness, worker) == ["Go on"], "it waits"
        await runtime.owner_send(worker, "Again")
        assert len(settings_notices(harness, worker)) == 1, "said once"
        assert harness.notifications, "the owner hears of it"
        said = await use_them(runtime, harness, worker)
        assert said == f"You let this worker use these settings (added {name})."
        assert runtime.states.is_live(worker)
        assert (
            "Go on" in harness.session(worker).sent[-1]
            and "Again" in harness.session(worker).sent[-1]
        )
        await runtime.owner_send(worker, "Next")
        assert unsent(harness, worker) == [] and len(settings_notices(harness, worker)) == 1
    finally:
        await runtime.stop()


async def test_agent_settings_a_live_worker_changes_stop_it_before_its_next_turn(
    tmp_path, git_repo, monkeypatch
):
    """Copilot starts a new MCP server of the repository's at its session's next turn: that
    turn is not sent; the worker is stopped and let go of, saying why. The owner's "Use these
    settings" goes on with it (no Go on to click after)."""
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        task = task_of(harness, worker)
        session = harness.session(worker)
        sent = len(session.sent)
        plant(cwd, ".github/mcp.json")
        await runtime.owner_send(worker, "Next")
        await settle(runtime)
        assert len(session.sent) == sent, "no turn"
        assert session.disconnected and not runtime.states.is_live(worker)
        assert unsent(harness, worker) == ["Next"]
        assert harness.store.get(f"stopped:{task}") == workers_module.SETTINGS_STOP
        stop = "Stopped: Copilot settings in this folder changed (added .github/mcp.json)."
        assert ("notice", stop) in harness.kinds(worker), "why, not just Stopped."
        await use_them(runtime, harness, worker)
        assert runtime.states.is_live(worker)
        assert harness.store.session(worker)["status"] == "working"
        assert runtime.tasks.get(task)["status"] == "running"
        assert not harness.store.get(f"stopped:{task}")
        went = harness.session(worker).sent[-1]
        assert "Go on where you stopped." in went and "Next" in went, "what waited goes too"
        assert unsent(harness, worker) == []
    finally:
        await runtime.stop()


async def test_agent_settings_are_checked_where_every_turn_starts(tmp_path, git_repo, monkeypatch):
    """Not only when a message comes: a send of what it has not received (a resume, a start
    from the queue, a take-back), a message sent again, and a turn Copilot starts by itself."""
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        session = harness.session(worker)
        await settle(runtime)
        # A turn Copilot starts from its own queue.
        plant(cwd, ".mcp.json")
        session.emit("assistant.turn_start", turn_id="t9")
        await settle(runtime)
        assert session.aborted and session.disconnected and not runtime.states.is_live(worker)
        (cwd / ".mcp.json").unlink()
        # What it has not received, sent (Delivery.flush: resume, Workers.start, take_back).
        await runtime.owner_send(worker, "Hello")
        await settle(runtime)
        session = harness.session(worker)
        assert runtime.states.is_live(worker)
        await call_tool(runtime, "report", worker, status="done", summary="Done.")
        session.emit("session.idle")
        await settle(runtime)
        sent = len(session.sent)
        runtime.add_message(worker, "owner", "More", {"prompt": "More"})
        plant(cwd, ".mcp.json")
        assert not await runtime.delivery.flush(worker, session)
        await settle(runtime)
        assert len(session.sent) == sent and not runtime.states.is_live(worker)
        (cwd / ".mcp.json").unlink()
        await runtime.delivery.resume(worker)
        await settle(runtime)
        session = harness.session(worker)
        assert runtime.states.is_live(worker) and "More" in session.sent[-1]
        # Sent again after its conversation never showed it.
        sent = len(session.sent)

        async def nothing():
            return []

        session.get_events = nothing
        runtime.states[worker].echoes = True  # (Its conversation showed what came before.)
        await runtime.owner_send(worker, "Lost")
        assert len(session.sent) == sent + 1
        (digest,) = runtime.states[worker].echo_wait
        plant(cwd, ".mcp.json")
        await runtime.delivery.check_echo(worker, session, digest)
        await settle(runtime)
        assert len(session.sent) == sent + 1, "not sent again"
    finally:
        await runtime.stop()


async def test_a_worker_at_work_when_chief_of_staff_stopped_waits_for_the_owner(
    tmp_path, git_repo, monkeypatch
):
    """Mid-turn at a restart, its folder changed meanwhile: it does not go on (nor fail); it
    waits at rest, and goes on once the owner says to use them."""
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await runtime.detach(worker)  # (As after a restart: nothing of it runs here.)
        assert harness.store.session(worker)["status"] == "working"
        plant(cwd, ".github/hooks/p.json")
        await runtime.resume_working()
        await settle(runtime)
        assert harness.store.session(worker)["status"] == "idle"
        assert not runtime.states.is_live(worker)
        assert settings_notices(harness, worker)[-1]["text"] == refusal(
            "added .github/hooks/p.json"
        )
        await use_them(runtime, harness, worker)
        assert runtime.states.is_live(worker)
        assert "Continue where you left off" in harness.session(worker).sent[-1]
    finally:
        await runtime.stop()


async def test_a_queued_worker_whose_folder_changed_waits_for_the_owner(
    tmp_path, git_repo, monkeypatch
):
    """Started from the queue with its session still open, it is not: it waits there, saying
    why, and is not tried again until the owner says to use them."""
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], max_workers=1))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await call_tool(runtime, "report", worker, status="done", summary="Done.")
        harness.session(worker).emit("session.idle")
        await settle(runtime)
        other, _ = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        await runtime.owner_send(worker, "One more thing")
        assert harness.store.session(worker)["status"] == "queued"
        plant(cwd, ".mcp.json")  # A command it left running writes it meanwhile.
        sent = len(harness.session(worker).sent)
        await call_tool(runtime, "report", other, status="done", summary="Done.")
        harness.session(other).emit("session.idle")
        await settle(runtime)
        assert len(harness.session(worker).sent) == sent, "no turn"
        assert not runtime.states.is_live(worker)
        row = harness.store.session(worker)
        assert row["status"] == "queued" and row["activity"] == workers_module.SETTINGS_WAIT
        await runtime.workers.pump()
        await settle(runtime)
        assert len(settings_notices(harness, worker)) == 1 and not runtime.states.is_live(worker)
        await use_them(runtime, harness, worker)
        assert runtime.states.is_live(worker)
        assert "One more thing" in harness.session(worker).sent[-1]
        assert harness.store.session(worker)["activity"] != workers_module.SETTINGS_WAIT
    finally:
        await runtime.stop()


async def test_committed_agent_settings_still_load(tmp_path, git_repo, monkeypatch):
    """A repository that ships its own hooks and MCP servers keeps them for its workers: as
    its worktree was made, a link committed as one included. One made before snapshots were
    kept is the owner's to look at once (no git runs to read its base)."""
    commit(git_repo, ".mcp.json", PLANTED[".mcp.json"])
    (git_repo / ".github" / "hooks").mkdir(parents=True)
    commit(git_repo, ".github/hooks/team.json", PLANTED[".github/hooks/p.json"])
    (git_repo / "config").mkdir()
    commit(git_repo, "config/agent.md", "An agent.\n")
    (git_repo / ".claude" / "agents").mkdir(parents=True)
    (git_repo / ".claude" / "agents" / "linked.md").symlink_to("../../config/agent.md")
    run("git", "add", ".", cwd=git_repo)
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "link", cwd=git_repo)
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        assert (cwd / ".claude" / "agents" / "linked.md").is_symlink()
        assert harness.session(worker).options["trust_working_directory"] == "user"
        await at_rest(runtime, harness, worker)
        await runtime.owner_send(worker, "Go on")
        assert runtime.states.is_live(worker)
        assert not settings_notices(harness, worker)
        # Made before snapshots were kept: the owner looks once.
        for key in [r["key"] for r in harness.store.all("SELECT key FROM kv")]:
            if key.startswith("settingssnap:"):
                harness.store.delete(key)
        await at_rest(runtime, harness, worker)
        await runtime.owner_send(worker, "Again")
        assert not runtime.states.is_live(worker)
        assert settings_notices(harness, worker)[-1]["text"] == untracked(
            ".claude/agents/linked.md, .github/hooks/team.json, .mcp.json"
        )
        await use_them(runtime, harness, worker)
        assert runtime.states.is_live(worker)
        await at_rest(runtime, harness, worker)
        plant(cwd, ".github/hooks/p.json")
        await runtime.owner_send(worker, "And")
        assert not runtime.states.is_live(worker)
        assert settings_notices(harness, worker)[-1]["text"] == refusal(
            "added .github/hooks/p.json"
        )
    finally:
        await runtime.stop()


async def test_agent_settings_a_worker_commits_or_merges_are_still_changed(
    tmp_path, git_repo, monkeypatch
):
    """What it was handed is what counts, not its git: committed in its worktree, or merged
    from upstream (a teammate's), a change waits for the owner's word."""
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await at_rest(runtime, harness, worker)
        commit(cwd, ".mcp.json", PLANTED[".mcp.json"])
        await runtime.owner_send(worker, "Go on")
        assert not runtime.states.is_live(worker)
        assert settings_notices(harness, worker)[-1]["text"] == refusal("added .mcp.json")
        await use_them(runtime, harness, worker)
        assert runtime.states.is_live(worker)
        await at_rest(runtime, harness, worker)
        commit(git_repo, ".mcp.json", '{"mcpServers": {"team": {"command": "node"}}}')
        upstream = run("git", "rev-parse", "HEAD", cwd=git_repo).strip()
        run("git", "-c", "user.email=t@t", "-c", "user.name=t", "merge", "-q", "-X", "theirs",
            "--no-edit", upstream, cwd=cwd)  # fmt: skip
        await runtime.owner_send(worker, "Merged")
        assert not runtime.states.is_live(worker)
        assert settings_notices(harness, worker)[-1]["text"] == refusal("changed .mcp.json")
        await use_them(runtime, harness, worker)
        assert runtime.states.is_live(worker) and "Merged" in harness.session(worker).sent[-1]
    finally:
        await runtime.stop()


async def test_agent_settings_in_the_owners_checkout_are_what_was_there_when_handed_out(
    tmp_path, git_repo, monkeypatch
):
    """The owner's checkout, handed out for the first time (no worker had it): its settings
    (their own local ones) are theirs, said once; a change since, committed or not, by a
    worker or by the owner, waits for their word, and a second worker there compares with the
    same."""
    trusting(tmp_path, monkeypatch, git_repo)
    plant(git_repo, ".claude/settings.local.json")
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    added = runtime.add_message
    threads = []

    def add_message(*arguments, **keywords):
        threads.append(threading.current_thread() is threading.main_thread())
        return added(*arguments, **keywords)

    monkeypatch.setattr(runtime, "add_message", add_message)
    try:
        info = await spawn(runtime, harness.router_id, title="Look", instructions="l",
                           project=str(git_repo), in_place=True)  # fmt: skip
        worker = info["session_id"]
        await settle(runtime)
        assert runtime.states.is_live(worker) and not settings_notices(harness, worker)
        assert threads and all(threads), "said on the event loop's thread, not a reading thread"
        notes = [t for k, t in harness.kinds(worker) if t.startswith("Workers in ")]
        assert len(notes) == 1 and notes[0].endswith(
            f"/{git_repo.name} use the agent settings there: .claude/settings.local.json."
        )
        await at_rest(runtime, harness, worker)
        # The owner's tool rewrites them ("don't ask again"): theirs to say.
        (git_repo / ".claude" / "settings.local.json").write_text('{"permissions": {}}')
        await runtime.owner_send(worker, "Go on")
        assert not runtime.states.is_live(worker)
        assert settings_notices(harness, worker)[-1]["text"] == refusal(
            "changed .claude/settings.local.json"
        )
        await use_them(runtime, harness, worker)
        assert runtime.states.is_live(worker)
        await at_rest(runtime, harness, worker)
        plant(git_repo, ".mcp.json")  # A worker's.
        await runtime.owner_send(worker, "Again")
        assert not runtime.states.is_live(worker)
        assert settings_notices(harness, worker)[-1]["text"] == refusal("added .mcp.json")
        info = await spawn(runtime, harness.router_id, title="Other", instructions="o",
                           project=str(git_repo), in_place=True)  # fmt: skip
        other = info["session_id"]
        await settle(runtime)
        assert not runtime.states.is_live(other), "a second worker there: the same snapshot"
        assert settings_notices(harness, other)[-1]["text"] == refusal("added .mcp.json")
    finally:
        await runtime.stop()


async def test_a_workers_commit_makes_no_baseline_in_the_owners_checkout(
    tmp_path, git_repo, monkeypatch
):
    """Its HEAD is the worker's to move: a plant it commits there is a plant still."""
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        info = await spawn(runtime, harness.router_id, title="Look", instructions="l",
                           project=str(git_repo), in_place=True)  # fmt: skip
        worker = info["session_id"]
        await at_rest(runtime, harness, worker)
        commit(git_repo, ".mcp.json", PLANTED[".mcp.json"])
        with suppress(RuntimeError):  # (An older version refused with an error.)
            await runtime.owner_send(worker, "Again")
        assert not runtime.states.is_live(worker)
        assert settings_notices(harness, worker)[-1]["text"] == refusal("added .mcp.json")
    finally:
        await runtime.stop()


async def test_agent_settings_in_a_checkout_handed_out_before_snapshots_were_kept(
    tmp_path, git_repo, monkeypatch
):
    """No snapshot yet (a worker given it by an older version): nothing there is taken by its
    date (a worker's `mv` keeps one); what is there is the owner's to look at once."""
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        info = await spawn(runtime, harness.router_id, title="Look", instructions="l",
                           project=str(git_repo), in_place=True)  # fmt: skip
        worker = info["session_id"]
        await at_rest(runtime, harness, worker)
        for key in [r["key"] for r in harness.store.all("SELECT key FROM kv")]:
            if key.startswith("settingssnap:"):
                harness.store.delete(key)
        plant(git_repo, ".github/hooks/p.json")
        os.utime(git_repo / ".github" / "hooks" / "p.json", (946684800, 946684800))
        await runtime.owner_send(worker, "Go on")
        assert not runtime.states.is_live(worker)
        assert settings_notices(harness, worker)[-1]["text"] == untracked(".github/hooks/p.json")
        await use_them(runtime, harness, worker)
        assert runtime.states.is_live(worker)
    finally:
        await runtime.stop()


async def test_the_owners_own_session_taken_up_keeps_their_local_settings(
    tmp_path, git_repo, monkeypatch
):
    """A session the owner started in a terminal in their repository, continued here, where
    they keep local settings of theirs (gitignored), no worker having had it: it opens, saying
    once what it uses."""
    commit(git_repo, ".gitignore", ".claude/settings.local.json\n")
    plant(git_repo, ".claude/settings.local.json")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    try:
        adopted = await runtime.workers.adopt(owners_session(harness, git_repo))
        worker = adopted["id"]
        assert runtime.states.is_live(worker) and not settings_notices(harness, worker)
        assert [t for k, t in harness.kinds(worker) if t.startswith("Workers in ")]
    finally:
        await runtime.stop()


def owners_session(harness, cwd: Path, said: tuple[str, ...] = ()) -> str:
    """A session the owner started in a terminal there, for Chief of Staff to take up."""
    harness.client.metas = [
        SimpleNamespace(
            session_id="ext-1", summary="Refactor parser", is_remote=False,
            modified_time=datetime(2026, 10, 1, tzinfo=UTC),
            context=SimpleNamespace(working_directory=str(cwd), repository="", branch=""),
        )
    ]  # fmt: skip
    harness.client.sessions["ext-1"] = FakeSession("ext-1", {})
    harness.client.sessions["ext-1"].raw_sent = list(said)
    return "ext-1"


async def test_the_owners_own_session_waits_at_rest_when_its_settings_changed(
    tmp_path, git_repo, monkeypatch
):
    """Workers had the checkout before; the owner's settings there changed since (Claude Code
    rewrites settings.local.json): taken up, it waits at rest with its line and its button,
    not refused (nothing to click then, and every try refused again)."""
    commit(git_repo, ".gitignore", ".claude/settings.local.json\n")
    plant(git_repo, ".claude/settings.local.json")
    trusting(tmp_path, monkeypatch, git_repo)
    await asyncio.sleep(0.01)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        info = await spawn(runtime, harness.router_id, title="Look", instructions="l",
                           project=str(git_repo), in_place=True)  # fmt: skip
        await at_rest(runtime, harness, info["session_id"])
        plant(git_repo, ".claude/settings.local.json", '{"permissions": {"allow": []}}')
        worker = owners_session(harness, git_repo, said=("Refactor the parser",))
        adopted = await runtime.workers.adopt(worker)
        assert adopted["id"] == worker and not runtime.states.is_live(worker)
        assert [n["text"] for n in settings_notices(harness, worker)] == [
            refusal("changed .claude/settings.local.json")
        ]
        router = [t for k, t in harness.kinds(harness.router_id) if t.startswith("Adopted")]
        assert router == ["Adopted “Refactor parser”: it waits for you to look at its folder's "
                          "settings"]  # fmt: skip
        with pytest.raises(ValueError, match="already manages"):
            await runtime.workers.adopt(worker)
        await use_them(runtime, harness, worker)
        assert runtime.states.is_live(worker), "opened, as it would have been"
        assert ("owner", "Refactor the parser") in harness.kinds(worker), "its history, imported"
    finally:
        await runtime.stop()


async def test_agent_homes_are_never_a_projects_settings(tmp_path, monkeypatch):
    """A session in ~ (a quick question in a terminal): ~/.codex holds Codex's own sessions,
    thousands of files, and ~/.claude Claude Code's own settings. Neither is the folder's: a
    Copilot check reads only Copilot's settings, and never in an agent's home."""
    home = tmp_path / "home"
    sessions = home / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    for index in range(settings_guard.MOST_ENTRIES + 1):
        (sessions / f"rollout-{index}.jsonl").write_text("{}")
    plant(home, ".claude/settings.json")
    monkeypatch.setenv("HOME", str(home))
    trusting(tmp_path, monkeypatch, home)
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    try:
        adopted = await runtime.workers.adopt(owners_session(harness, home))
        assert runtime.states.is_live(adopted["id"])
        assert not settings_notices(harness, adopted["id"])
        assert not [t for k, t in harness.kinds(adopted["id"]) if t.startswith("Workers in ")]
    finally:
        await runtime.stop()


async def test_settings_too_many_to_check_are_the_owners_to_use_unchecked(
    tmp_path, git_repo, monkeypatch
):
    """More than a snapshot holds: the worker waits, and the owner may let it go on with them
    unchecked; once they can be checked again, they are checked from there."""
    trusting(tmp_path, monkeypatch, git_repo)
    monkeypatch.setattr(settings_guard, "MOST_ENTRIES", 3)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await at_rest(runtime, harness, worker)
        for index in range(4):
            plant(cwd, f".github/agents/a{index}.agent.md", "x")
        await runtime.owner_send(worker, "Next")
        await settle(runtime)
        assert not runtime.states.is_live(worker)
        line = settings_notices(harness, worker)[-1]["text"]
        assert line.startswith("Copilot settings in this folder can't be checked (")
        assert line.endswith(
            "Chief of Staff won't start the worker there until you say to use them."
        )
        said = await use_them(runtime, harness, worker)
        assert said.startswith("You let this worker use them unchecked")
        assert runtime.states.is_live(worker) and unsent(harness, worker) == []
        await at_rest(runtime, harness, worker)
        # What was said unchecked is what could be read of them then: one made executable (as
        # Copilot does a hook's script it runs) is the same; one changed waits again.
        (cwd / ".github" / "agents" / "a2.agent.md").chmod(0o755)
        await runtime.owner_send(worker, "Again")
        await settle(runtime)
        assert runtime.states.is_live(worker)
        await at_rest(runtime, harness, worker)
        plant(cwd, ".github/agents/a1.agent.md", "---\nmcp-servers: {p: {command: sh}}\n---\n")
        await runtime.owner_send(worker, "Still")
        await settle(runtime)
        assert not runtime.states.is_live(worker)
        await use_them(runtime, harness, worker)
        await at_rest(runtime, harness, worker)
        for index in range(3):
            (cwd / ".github" / "agents" / f"a{index}.agent.md").unlink()
        await runtime.owner_send(worker, "More")  # Checkable again: as they are now.
        await settle(runtime)
        assert runtime.states.is_live(worker)
        await at_rest(runtime, harness, worker)
        plant(cwd, ".mcp.json")
        await runtime.owner_send(worker, "And more")
        await settle(runtime)
        assert settings_notices(harness, worker)[-1]["text"] == refusal("added .mcp.json")
    finally:
        await runtime.stop()


async def test_a_second_folder_of_the_owners_checkout_is_the_owners_to_look_at_once(
    tmp_path, git_repo, monkeypatch
):
    """A monorepo: a worker in api/, then one in web/, whose own settings were committed long
    before. Its folder is seen for the first time: the owner looks once (a worker in api/ could
    have moved settings there, keeping their dates)."""
    for name in ("api", "web"):
        (git_repo / name).mkdir()
        commit(git_repo, f"{name}/README.md", name)
    (git_repo / "web" / ".claude").mkdir()
    commit(git_repo, "web/.claude/settings.json", '{"permissions": {}}')
    trusting(tmp_path, monkeypatch, git_repo)
    projects = [str(git_repo / "api"), str(git_repo / "web")]
    harness = Harness(tmp_path / "data", Settings(projects=projects))
    runtime = await harness.connect()
    try:
        api = await spawn(runtime, harness.router_id, title="Look api", instructions="l",
                          project=projects[0])  # fmt: skip
        await settle(runtime)
        assert runtime.states.is_live(api["session_id"])
        web = await spawn(runtime, harness.router_id, title="Look web", instructions="l",
                          project=projects[1])  # fmt: skip
        await settle(runtime)
        assert not runtime.states.is_live(web["session_id"])
        assert settings_notices(harness, web["session_id"])[-1]["text"] == unused(
            "web/.claude/settings.json"
        )
        await use_them(runtime, harness, web["session_id"])
        assert runtime.states.is_live(web["session_id"])
        # Made in web/ after that: a change.
        plant(git_repo / "web", ".mcp.json")
        await at_rest(runtime, harness, web["session_id"])
        await runtime.owner_send(web["session_id"], "Next")
        await settle(runtime)
        assert not runtime.states.is_live(web["session_id"])
        assert settings_notices(harness, web["session_id"])[-1]["text"] == refusal(
            "added web/.mcp.json"
        )
    finally:
        await runtime.stop()


async def test_a_worker_waiting_for_its_settings_says_so_to_the_router_and_the_owner(
    tmp_path, git_repo, monkeypatch
):
    """At rest when its folder changed, sent new work: it shows as waiting for the owner (not
    done), and the Router hears it, once."""
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await at_rest(runtime, harness, worker)
        plant(cwd, ".mcp.json")
        await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                        text="Add a FAQ.", task="new", title="Add a FAQ to the docs",
                        why_new="another deliverable")  # fmt: skip
        await settle(runtime)
        await call_tool(runtime, "message_session", harness.router_id, session_id=worker,
                        text="With examples.", task="note")  # fmt: skip
        await settle(runtime)
        assert harness.store.session(worker)["activity"] == workers_module.SETTINGS_WAIT
        heard = [
            m for m in harness.store.messages(harness.router_id)
            if m["meta"].get("from") == worker and m["meta"].get("status") == "waiting"
        ]  # fmt: skip
        assert len(heard) == 1 and "It waits for the owner" in heard[0]["text"]
        await use_them(runtime, harness, worker)
        assert harness.store.session(worker)["activity"] != workers_module.SETTINGS_WAIT
    finally:
        await runtime.stop()


async def test_a_waiting_worker_goes_on_once_its_settings_are_as_they_were(
    tmp_path, git_repo, monkeypatch
):
    """The owner removes what the worker wrote, rather than using it: the button (or the
    queue's next run) finds nothing changed, and every worker that waited goes on."""
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await at_rest(runtime, harness, worker)
        plant(cwd, ".mcp.json")
        await runtime.owner_send(worker, "Go on")
        await settle(runtime)
        assert not runtime.states.is_live(worker)
        (cwd / ".mcp.json").unlink()
        digest = settings_notices(harness, worker)[-1]["meta"]["agent_settings"]["digest"]
        assert await runtime.accept_settings(worker, digest) == (
            "They are as they were: nothing to change."
        )
        await settle(runtime)
        assert runtime.states.is_live(worker) and unsent(harness, worker) == []
        # Without the button: the queue's next run.
        await at_rest(runtime, harness, worker)
        plant(cwd, ".mcp.json")
        await runtime.owner_send(worker, "And this")
        await settle(runtime)
        assert not runtime.states.is_live(worker)
        (cwd / ".mcp.json").unlink()
        await runtime.workers.pump()
        await settle(runtime)
        assert runtime.states.is_live(worker) and unsent(harness, worker) == []
        assert harness.store.session(worker)["activity"] != workers_module.SETTINGS_WAIT
    finally:
        await runtime.stop()


async def test_a_reopened_worker_is_given_the_owners_carried_settings_again(
    tmp_path, git_repo, monkeypatch
):
    """Closed, its worktree removed; meanwhile the owner's own local settings changed. Made
    again, the worktree gets theirs as they are now, from Chief of Staff: not a change."""
    commit(git_repo, ".gitignore", ".claude/settings.local.json\n")
    commit(git_repo, ".worktreeinclude", ".claude/settings.local.json\n")
    plant(git_repo, ".claude/settings.local.json")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        await runtime.workers.close(worker)
        await settle(runtime)
        assert not cwd.exists()
        plant(git_repo, ".claude/settings.local.json", '{"permissions": {"allow": []}}')
        await runtime.workers.reopen(worker)
        await runtime.owner_send(worker, "One more change please")
        await settle(runtime)
        assert runtime.states.is_live(worker) and not settings_notices(harness, worker)
    finally:
        await runtime.stop()


async def test_agent_settings_carried_into_a_worktree_are_the_owners(
    tmp_path, git_repo, monkeypatch
):
    """A gitignored settings file the repository carries into worktrees (.worktreeinclude) is
    the owner's as carried; changed in the worktree, it is not."""
    commit(git_repo, ".gitignore", ".claude/settings.local.json\n")
    commit(git_repo, ".worktreeinclude", ".claude/settings.local.json\n")
    plant(git_repo, ".claude/settings.local.json")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        carried = cwd / ".claude" / "settings.local.json"
        assert carried.exists() and runtime.states.is_live(worker)
        await at_rest(runtime, harness, worker)
        (git_repo / ".claude" / "settings.local.json").write_text('{"owner": "edited theirs"}')
        await runtime.owner_send(worker, "Go on")
        assert runtime.states.is_live(worker), "as carried, whatever the owner's copy is now"
        await at_rest(runtime, harness, worker)
        carried.write_text('{"hooks": {"x": []}}')
        await runtime.owner_send(worker, "Go on")
        assert settings_notices(harness, worker)[-1]["text"] == refusal(
            "changed .claude/settings.local.json"
        )
    finally:
        await runtime.stop()


async def test_agent_settings_changed_between_the_top_and_its_folder_count(
    tmp_path, git_repo, monkeypatch
):
    sub = git_repo / "app"
    sub.mkdir()
    commit(git_repo, "app/README.md", "x")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(sub)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, sub)
        assert str(cwd).endswith("/app")
        await at_rest(runtime, harness, worker)
        plant(cwd.parent, ".mcp.json")
        plant(cwd, ".github/hooks/p.json")
        await runtime.owner_send(worker, "Go on")
        assert settings_notices(harness, worker)[-1]["text"] == refusal(
            "added .mcp.json, app/.github/hooks/p.json"
        )
    finally:
        await runtime.stop()


async def test_agent_settings_changed_in_an_untrusted_folder_wait_too(
    tmp_path, git_repo, monkeypatch
):
    """Whether Copilot trusts the folder is not taken on trust (a worktree of a trusted
    repository is trusted by Copilot itself): the same."""
    trusting(tmp_path, monkeypatch, tmp_path / "elsewhere")
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await at_rest(runtime, harness, worker)
        plant(cwd, ".mcp.json")
        await runtime.owner_send(worker, "Go on")
        assert not runtime.states.is_live(worker)
        assert settings_notices(harness, worker)[-1]["text"] == refusal("added .mcp.json")
    finally:
        await runtime.stop()


def booby_trap(folder: Path, marker: Path):
    """What a sandboxed worker's shell can do in its own folder: a git repository whose config
    runs a command when git needs an object it lacks (a partial clone's lazy fetch), or reads
    a remote over a transport of its own."""
    folder.mkdir(parents=True, exist_ok=True)
    trap = ["git", "-c", "init.defaultBranch=main"]
    run(*trap, "init", "-q", cwd=folder)
    for key, value in (
        ("core.repositoryformatversion", "1"),
        ("extensions.partialClone", "origin"),
        ("remote.origin.url", f"ext::sh -c touch% {marker}"),
        ("remote.origin.promisor", "true"),
        ("remote.origin.uploadpack", f"touch {marker}; false"),
        ("protocol.ext.allow", "always"),
        ("core.fsmonitor", f"touch {marker}; false"),
    ):
        run("git", "config", key, value, cwd=folder)
    (folder / ".git" / "refs" / "heads" / "main").write_text("1" * 40 + "\n")
    (folder / ".mcp.json").write_text("{}")


async def test_checking_agent_settings_runs_nothing_of_a_workers_repository(
    tmp_path, git_repo, monkeypatch
):
    """No git runs on a folder a worker can write (its config can run code as the owner): the
    check reads the files. A worker's own `git init`, commit and origin/HEAD make no baseline."""
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        info = await spawn(runtime, harness.router_id, title="Look", instructions="l")
        worker, cwd = info["session_id"], Path(info["cwd"])
        await at_rest(runtime, harness, worker)
        marker = tmp_path / "PWNED"
        booby_trap(cwd, marker)
        with suppress(RuntimeError):  # (An older version refused with an error.)
            await runtime.owner_send(worker, "Go on")
        await settle(runtime)
        assert not marker.exists()
        assert not runtime.states.is_live(worker)
        assert settings_notices(harness, worker)[-1]["text"] == refusal("added .mcp.json")
        # Its own repository, its plant committed, an origin/HEAD of its own: the same.
        (cwd / ".git" / "refs" / "heads" / "main").unlink()
        for key in ("extensions.partialClone", "core.fsmonitor"):
            run("git", "config", "--unset", key, cwd=cwd)
        run("git", "-c", "user.email=w@w", "-c", "user.name=w", "commit", "-qm", "x",
            "--allow-empty", cwd=cwd)  # fmt: skip
        run("git", "add", ".mcp.json", cwd=cwd)
        run("git", "-c", "user.email=w@w", "-c", "user.name=w", "commit", "-qm", "y", cwd=cwd)
        run("git", "update-ref", "refs/remotes/origin/HEAD", "HEAD", cwd=cwd)
        await runtime.owner_send(worker, "Again")
        assert not runtime.states.is_live(worker) and not marker.exists()
        # A clone of its own, and a folder of the owner's checkout with a .git planted in it.
        cloned, inner = tmp_path / "clone", git_repo / "src"
        cloned.mkdir()
        inner.mkdir()
        clone = harness.store.create_session("c1", "worker", "Clone", "idle", cwd=str(cloned))
        sub = harness.store.create_session("c2", "worker", "Sub", "idle", cwd=str(inner))
        await asyncio.sleep(0.01)
        booby_trap(cloned, marker)
        booby_trap(inner, marker)
        for row in (clone, sub):
            harness.store.set(f"sent:{row['id']}", "1")  # (They ran: not a first handout.)
            assert "hasn't checked yet, in a checkout workers have had (.mcp.json)" in (
                await runtime.settings_refusal(row)
            )
        assert not marker.exists()
    finally:
        await runtime.stop()


async def test_a_worker_folder_outside_git_is_checked(tmp_path, git_repo, monkeypatch):
    """Its own folder (no repository: a task's, a workspace) in a folder the owner trusts."""
    trusting(tmp_path, monkeypatch, tmp_path)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        info = await spawn(runtime, harness.router_id, title="Look", instructions="l")
        worker, cwd = info["session_id"], Path(info["cwd"])
        await at_rest(runtime, harness, worker)
        plant(cwd, ".github/copilot/settings.local.json")
        await runtime.owner_send(worker, "Go on")
        assert not runtime.states.is_live(worker)
        assert settings_notices(harness, worker)[-1]["text"] == refusal(
            "added .github/copilot/settings.local.json"
        )
    finally:
        await runtime.stop()


async def test_news_for_a_worker_whose_settings_changed_waits_for_it(
    tmp_path, git_repo, monkeypatch
):
    """It keeps following the PR; the news goes to it once the owner says to use them."""
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        key = "github:acme/api#7"
        runtime.follow(worker, [key], task_id=task_of(harness, worker))
        await at_rest(runtime, harness, worker)
        plant(cwd, ".mcp.json")
        took = await runtime.on_thread("github", key, ["github:issuecomment:1"], "bob: a test?")
        assert took and harness.store.follows(worker) == [key], "it still follows the PR"
        assert not runtime.states.is_live(worker)
        assert any("bob: a test?" in text for text in unsent(harness, worker))
        await use_them(runtime, harness, worker)
        assert "bob: a test?" in harness.session(worker).sent[-1]
    finally:
        await runtime.stop()


def test_a_snapshot_reads_the_files_every_time(tmp_path):
    """Content, not what a file system says of it (a same-size rewrite with its times put
    back); names as a case-insensitive file system finds them, NFC or NFD, said once; links
    by where they lead; too many or too large fail closed."""
    top = tmp_path / "top"
    sub = top / unicodedata.normalize("NFD", "café")
    (sub / ".github" / "hooks").mkdir(parents=True)
    hook = sub / ".github" / "hooks" / "a.json"
    hook.write_text('{"a": 1}')
    before = settings_guard.scan(top, sub)
    assert list(before) == [unicodedata.normalize("NFC", "café")]
    stat = hook.stat()
    hook.write_text('{"a": 2}')
    os.utime(hook, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    now = settings_guard.scan(top, sub)
    assert settings_guard.changes(now, before) == [("changed", "café/.github/hooks/a.json")]
    (top / ".MCP.json").write_text("{}")
    (top / ".Codex").mkdir()
    (top / ".Codex" / "config.toml").write_text("")
    found = settings_guard.changes(settings_guard.scan(top, sub), now)
    assert found == [("added", ".Codex/config.toml"), ("added", ".MCP.json")]
    (top / "elsewhere.json").write_text("{}")
    (top / ".MCP.json").unlink()
    (top / ".MCP.json").symlink_to(top / "elsewhere.json")
    linked = settings_guard.scan(top, sub)
    (top / "elsewhere.json").write_text('{"mcpServers": {}}')
    assert settings_guard.changes(settings_guard.scan(top, sub), linked) == [
        ("changed", ".MCP.json")
    ], "what a link inside leads to"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings_guard, "MOST_ENTRIES", 2)
        with pytest.raises(settings_guard.TooLargeError):
            settings_guard.scan(top, sub)
    (sub / ".github" / "hooks" / "b.json").mkdir()
    entries = settings_guard.scan(top, sub)[unicodedata.normalize("NFC", "café")]
    assert list(entries) == [".github/hooks/a.json"], "an empty folder holds no setting"


async def test_settings_a_worker_moves_into_place_are_the_owners_to_look_at(
    tmp_path, git_repo, monkeypatch
):
    """The repository's own example settings, moved into place by a worker (a rename keeps
    their dates): seen for the first time in a folder, never taken as long there. A monorepo's
    other folder, and Codex's settings where a Copilot worker was checked, alike."""
    for name in ("api", "web"):
        (git_repo / name).mkdir()
        commit(git_repo, f"{name}/README.md", name)
    demo = git_repo / "examples" / "demo"
    plant(demo, ".claude/settings.json", PLANTED[".github/hooks/p.json"])
    plant(demo, ".github/hooks/p.json")
    plant(demo, ".codex/rules/x.rules", 'prefix_rule(pattern=["git"], decision="allow")\n')
    run("git", "add", ".", cwd=git_repo)
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "demo", cwd=git_repo)
    trusting(tmp_path, monkeypatch, git_repo)
    projects = [str(git_repo / "api"), str(git_repo / "web")]
    harness = Harness(tmp_path / "data", Settings(projects=projects))
    runtime = await harness.connect()
    try:
        api = await spawn(runtime, harness.router_id, title="Look api", instructions="l",
                          project=projects[0], in_place=True)  # fmt: skip
        await settle(runtime)
        assert runtime.states.is_live(api["session_id"])
        # Its shell, in the owner's checkout (unsandboxed): renames, no file written.
        (demo / ".claude").rename(git_repo / "web" / ".claude")
        (demo / ".github").rename(git_repo / "web" / ".github")
        (demo / ".codex").rename(git_repo / ".codex")
        web = await spawn(runtime, harness.router_id, title="Look web", instructions="l",
                          project=projects[1], in_place=True)  # fmt: skip
        await settle(runtime)
        assert not runtime.states.is_live(web["session_id"])
        assert settings_notices(harness, web["session_id"])[-1]["text"] == unused(
            "web/.claude/settings.json, web/.github/hooks/p.json", writable=True
        )
        assert await runtime.codex_settings(harness.store.session(api["session_id"])) == [
            str(git_repo.resolve())
        ]
    finally:
        await runtime.stop()


async def test_a_git_folder_a_worker_makes_does_not_shorten_what_is_checked(
    tmp_path, git_repo, monkeypatch
):
    """A worker in a folder of the owner's checkout makes `app/.git` (no repository: Copilot and
    Codex look past it): settings planted at the checkout's top are still checked, and its
    commands still can't write them there."""
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "darwin")
    commit(git_repo, "app/README.md", "x")
    app = git_repo / "app"
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        info = await spawn(runtime, harness.router_id, title="Look", instructions="l",
                           project=str(app), in_place=True)  # fmt: skip
        worker = info["session_id"]
        await at_rest(runtime, harness, worker)
        (app / ".git").mkdir()
        row = harness.store.session(worker)
        policy = runtime.sandbox_policy(row)
        assert policy is not None
        top = git_repo.resolve()
        assert str(top / ".github" / "hooks") in policy.denied
        plant(git_repo, ".github/hooks/p.json")
        assert await runtime.settings_refusal(row) == refusal("added .github/hooks/p.json")
        plant(git_repo, ".codex/rules/x.rules", 'prefix_rule(pattern=["git"], decision="allow")\n')
        assert await runtime.codex_settings(row) == [str(top)]
    finally:
        await runtime.stop()


async def test_a_setting_linked_out_of_its_folder_or_special_is_never_taken(
    tmp_path, git_repo, monkeypatch
):
    """A link out of the folder (what it leads to can change with nothing there changing) and
    a FIFO (any content at each read): the worker waits until they are gone, with no button."""
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await at_rest(runtime, harness, worker)
        notes = cwd.parent / "notes"
        notes.mkdir()
        (notes / "p.json").write_text('{"version": 1, "hooks": {}}')
        (cwd / ".github").mkdir(exist_ok=True)
        (cwd / ".github" / "hooks").symlink_to(notes)
        await runtime.owner_send(worker, "Go on")
        assert not runtime.states.is_live(worker)
        line = harness.store.messages(worker)[-1]
        assert line["text"] == (
            "Copilot settings in this folder can't be used (.github/hooks leads to a folder "
            "workers can write); Chief of Staff won't start the worker there until that's removed."
        )
        assert not line["meta"].get("agent_settings"), "nothing to use"
        now = settings_guard.scan(cwd, cwd, REPOSITORY_SETTINGS, None, runtime.writable_roots)
        digest = settings_guard.digest(settings_guard.restricted(now, REPOSITORY_SETTINGS))
        with pytest.raises(ValueError, match="remove that first"):
            await runtime.accept_settings(worker, digest)
        (cwd / ".github" / "hooks").unlink()
        os.mkfifo(cwd / ".mcp.json")
        await runtime.owner_send(worker, "Again")
        assert not runtime.states.is_live(worker)
        assert "(.mcp.json is a special file)" in harness.store.messages(worker)[-1]["text"]
        (cwd / ".mcp.json").unlink()
        await runtime.workers.pump()
        await settle(runtime)
        assert runtime.states.is_live(worker)
    finally:
        await runtime.stop()


async def test_a_link_out_of_the_repository_is_held_by_where_it_leads(
    tmp_path, git_repo, monkeypatch
):
    """A settings file kept in a dotfiles repository, linked in: held by its content, the
    owner's to use as any. A virtualenv's python a server runs: like a program on PATH, nothing
    to it, unless it is made to lead where workers can write: then that server is off."""
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    (dotfiles / "claude.json").write_text('{"permissions": {}}')
    (git_repo / ".claude").mkdir()
    (git_repo / ".claude" / "settings.local.json").symlink_to(dotfiles / "claude.json")
    (git_repo / ".venv" / "bin").mkdir(parents=True)
    (git_repo / ".venv" / "bin" / "python").symlink_to(sys.executable)
    server = {"mcpServers": {"s": {"command": ".venv/bin/python", "args": ["server.py"]}}}
    commit(git_repo, ".mcp.json", json.dumps(server))
    commit(git_repo, "server.py", "print('serve')\n")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    workspaces = str((tmp_path / "data").resolve())
    monkeypatch.setattr(runtime, "writable_roots", lambda: (workspaces,))
    try:
        info = await spawn(runtime, harness.router_id, title="Look", instructions="l",
                           project=str(git_repo), in_place=True)  # fmt: skip
        worker = info["session_id"]
        await settle(runtime)
        assert runtime.states.is_live(worker) and not settings_notices(harness, worker)
        await at_rest(runtime, harness, worker)
        (dotfiles / "claude.json").write_text('{"permissions": {"allow": ["Bash"]}}')
        await runtime.owner_send(worker, "Next")
        assert settings_notices(harness, worker)[-1]["text"] == refusal(
            "changed .claude/settings.local.json"
        )
        await use_them(runtime, harness, worker)
        assert runtime.states.is_live(worker)
        await at_rest(runtime, harness, worker)
        # Its venv made again, its python another (an upgrade): as a program on PATH.
        (git_repo / ".venv" / "bin" / "python").unlink()
        (git_repo / ".venv" / "bin" / "python").symlink_to("/usr/bin/true")
        await runtime.owner_send(worker, "More")
        assert runtime.states.is_live(worker)
        await at_rest(runtime, harness, worker)
        # Led where workers can write: that one counts.
        planted = tmp_path / "data" / "evil.sh"
        planted.write_text("#!/bin/sh\n")
        (git_repo / ".venv" / "bin" / "python").unlink()
        (git_repo / ".venv" / "bin" / "python").symlink_to(planted)
        await runtime.owner_send(worker, "Again")
        assert runtime.states.is_live(worker)
        assert harness.session(worker).options["disabled_mcp_servers"] == ["s"]
        assert [t for k, t in harness.kinds(worker) if t.startswith("Its MCP")] == [
            "Its MCP server s changed in this worker's folder, so it runs without it."
        ]
    finally:
        await runtime.stop()


async def test_a_committed_link_out_of_the_repository_is_never_taken(
    tmp_path, git_repo, monkeypatch
):
    """Committed, pointing above the repository: out of the folder all the same."""
    (tmp_path / "shared.json").write_text("{}")
    (git_repo / ".mcp.json").symlink_to("../shared.json")
    run("git", "add", ".mcp.json", cwd=git_repo)
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "l", cwd=git_repo)
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        (tmp_path / "data" / "work" / "workspaces").mkdir(parents=True, exist_ok=True)
        worker, cwd = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        assert (cwd / ".mcp.json").is_symlink() and not runtime.states.is_live(worker)
        said = harness.store.messages(worker)[-1]["text"]
        assert "(.mcp.json leads to a folder workers can write)" in said
    finally:
        await runtime.stop()


HOOKED = {
    ".claude/settings.json": json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/check.sh"}]}]}}),
    ".claude/hooks/check.sh": "#!/bin/sh\nexit 0\n",
    ".mcp.json": json.dumps({"mcpServers": {"tool": {"command": "python3",
                                                     "args": ["tools/mcp_server.py"]},
                                            "app": {"command": "uv", "args": ["run", "server"]}}}),
    "tools/mcp_server.py": "print('server')\n",
    ".github/hooks/team.json": json.dumps({"version": 1, "hooks": {"preToolUse": [
        {"type": "command", "bash": "cd $PWD && ./scripts/check.sh --strict"}]}}),
    "scripts/check.sh": "#!/bin/sh\nexit 0\n",
    ".github/lsp.json": json.dumps({"lspServers": {"x": {"command": "node",
                                    "args": ["./lsp/server.js"], "fileExtensions": {".x": "x"}}}}),
    "lsp/server.js": "// a server\n",
    ".github/agents/a.agent.md": "---\nname: a\nmcp-servers:\n  t:\n    command: sh\n"
                                 "    args:\n      - tools/agent_server.sh\n---\nAn agent.\n",
    "tools/agent_server.sh": "#!/bin/sh\n",
}  # fmt: skip


async def test_the_files_settings_run_are_held_with_them(tmp_path, git_repo, monkeypatch):
    """A hook's script named by its settings: changed, the worker waits as for the settings
    themselves. What an MCP, language or agent's server runs, changed: that server is off for
    the worker from its next start, said once, never a wait. A command that runs project code
    otherwise (`uv run`) is said once: a change to that code isn't caught."""
    for name, text in HOOKED.items():
        commit(git_repo, name, text)
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await at_rest(runtime, harness, worker)
        assert runtime.states.is_live(worker) or not settings_notices(harness, worker)
        code = [t for k, t in harness.kinds(worker) if "run project code" in t]
        assert code == [
            f"Copilot settings in {home_shown(str(cwd.resolve()))} run project code Chief of "
            "Staff can't check (.mcp.json runs `uv run server`): a change to that code takes "
            "effect without asking you."
        ]
        # What its servers run (it develops them): no wait; off from its next start, said once.
        for name in ("tools/mcp_server.py", "lsp/server.js", "tools/agent_server.sh"):
            (cwd / name).write_text("built again\n")
        await runtime.owner_send(worker, "Next")
        await settle(runtime)
        assert runtime.states.is_live(worker) and not settings_notices(harness, worker)
        then = settings_guard.load(harness.store, cwd)["folders"]
        assert settings_guard.servers_changed(settings_guard.scan(cwd, cwd), then) == {
            "lsp/x": ["lsp/server.js (run by .github/lsp.json)"],
            "mcp/t@a": ["tools/agent_server.sh (run by .github/agents/a.agent.md)"],
            "mcp/tool": ["tools/mcp_server.py (run by .mcp.json)"],
        }
        line = [t for k, t in harness.kinds(worker) if t.startswith("Its ")]
        assert line == [
            "Its language server x, MCP server t (of the custom agent a) and MCP server tool "
            "changed in this worker's folder, so it runs without them."
        ]
        assert harness.session(worker).options["disabled_mcp_servers"] == ["t", "tool"]
        await at_rest(runtime, harness, worker)
        # What its hooks run: as the settings themselves, it waits.
        for name in (".claude/hooks/check.sh", "scripts/check.sh"):
            (cwd / name).write_text("planted\n")
        await runtime.owner_send(worker, "More")
        assert not runtime.states.is_live(worker)
        assert settings_notices(harness, worker)[-1]["text"] == refusal(
            "changed .claude/hooks/check.sh (run by .claude/settings.json), scripts/check.sh "
            "(run by .github/hooks/team.json)"
        )
        await use_them(runtime, harness, worker)
        assert runtime.states.is_live(worker)
    finally:
        await runtime.stop()


def test_what_settings_run_is_found_in_their_commands(tmp_path):
    """Best effort, by what their commands name: an extension's relative imports, a plugin
    marketplace's own folder, a Codex server's program, a shell's `-c` line. Folders an
    argument names are not what runs; commands that run project code by a name are noted."""
    top = tmp_path / "top"
    files = {
        ".github/extensions/e/extension.mjs": 'import { x } from "../../../src/helper.mjs";\n',
        "src/helper.mjs": "export const x = 1;\n",
        ".github/copilot/settings.json": json.dumps({"enabledPlugins": {"p@team": True},
            "extraKnownMarketplaces": {"team": {"source": {"source": "directory",
                                                           "path": "./plugins"}}}}),
        "plugins/p/hooks.json": "{}",
        ".claude/settings.json": json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "bash -c 'python3 -m tools.check && npx -y srv'"}]}]}}),
        ".codex/config.toml": '[mcp_servers.s]\ncommand = "./bin/server"\nargs = ["data"]\n',
        "bin/server": "#!/bin/sh\n",
        "data/big.bin": "x",
        ".mcp.json": "{ not json",
        ".github/hooks/h.json": json.dumps({"version": 1, "hooks": {"preToolUse": [
            {"type": "command", "bash": "./.github/hooks/run.sh"}]}}),
        ".github/hooks/run.sh": "#!/bin/sh\n",  # (Held as a setting itself.)
    }  # fmt: skip
    for name, text in files.items():
        plant(top, name, text)
    code: list[str] = []
    found = settings_guard.scan(top, top, code=code)["."]
    assert held(found) == ["bin/server@mcp/s", "plugins", "src/helper.mjs"]
    assert sorted(code) == [
        ".claude/settings.json runs `npx -y srv`",
        ".claude/settings.json runs `python3 -m tools.check`",
    ]
    before = settings_guard.scan(top, top)
    (top / "plugins" / "p" / "hooks.json").write_text('{"hooks": {}}')
    assert settings_guard.changes(settings_guard.scan(top, top), before) == [
        ("changed", "plugins (run by .github/copilot/settings.json)")
    ]


RUNS = {  # A setting, what it says, the file it runs (found, held with it).
    "bash -c": (".github/hooks/h.json", {"version": 1, "hooks": {"preToolUse": [
        {"type": "command", "bash": "bash -c 'python3 tools/guard.py'"}]}}, "tools/guard.py"),
    "sh -c in args": (".mcp.json", {"mcpServers": {"s": {"command": "sh",
        "args": ["-c", "node tools/server.js"]}}}, "tools/server.js"),
    "cd, then": (".github/hooks/h.json", {"version": 1, "hooks": {"preToolUse": [
        {"type": "command", "bash": "cd scripts && ./guard.sh"}]}}, "scripts/guard.sh"),
    "a hook's cwd": (".github/hooks/h.json", {"version": 1, "hooks": {"preToolUse": [
        {"type": "command", "bash": "./guard.sh", "cwd": "scripts"}]}}, "scripts/guard.sh"),
    "the session's folder": (".mcp.json", {"mcpServers": {"s": {"command": "node",
        "args": ["${COPILOT_SESSION_CWD}/tools/server.js"]}}}, "tools/server.js"),
    "node's folder": (".mcp.json", {"mcpServers": {"s": {"command": "node",
        "args": ["tools/server"]}}}, "tools/server/index.js"),
    "python's folder": (".mcp.json", {"mcpServers": {"s": {"command": "python3",
        "args": ["tools/srv"]}}}, "tools/srv/__main__.py"),
    "node server": (".mcp.json", {"mcpServers": {"s": {"command": "node", "args": ["server"]}}},
                    "server.js"),
    "python -c import": (".mcp.json", {"mcpServers": {"s": {"command": "python3",
        "args": ["-c", "import mcpserver; mcpserver.main()"]}}}, "mcpserver.py"),
    "python -m": (".mcp.json", {"mcpServers": {"s": {"command": "python3",
        "args": ["-m", "tools.serve"]}}}, "tools/serve.py"),
    "BASH_ENV": (".github/hooks/h.json", {"version": 1, "hooks": {"preToolUse": [
        {"type": "command", "bash": "true", "env": {"BASH_ENV": "scripts/env.sh"}}]}},
        "scripts/env.sh"),
    "flow-style agent": (".github/agents/a.agent.md", "---\nname: a\nmcp-servers: {s: {command: "
                         "node, args: [tools/server.js]}}\n---\nBody\n", "tools/server.js"),
}  # fmt: skip


@pytest.mark.parametrize("case", RUNS)
def test_the_file_a_setting_runs_is_found(tmp_path, case):
    setting, said, ran = RUNS[case]
    plant(tmp_path, setting, said if isinstance(said, str) else json.dumps(said))
    plant(tmp_path, ran, "print('ok')\n")
    code: list[str] = []
    entries = settings_guard.scan(tmp_path, tmp_path, code=code)["."]
    assert [path.split("@")[0] for path in held(entries)] == [ran]
    assert code == []


def hooked(bash: str, **more) -> tuple[str, dict]:
    return ".github/hooks/h.json", {"version": 1, "hooks": {"preToolUse": [
        {"type": "command", "bash": bash, **more}]}}  # fmt: skip


def served(command: str, args: list, **more) -> tuple[str, dict]:
    return ".mcp.json", {"mcpServers": {"s": {"command": command, "args": args, **more}}}


SHELL = {  # A setting, the file it runs, whether that is held, whether the command is said.
    "bash -lc": (*hooked("bash -lc 'python3 tools/guard.py'"), "tools/guard.py", True, False),
    "sh -ec": (*hooked("sh -ec 'node tools/guard.js'"), "tools/guard.js", True, False),
    "bash --noprofile -c": (*hooked("bash --noprofile -c 'python3 tools/guard.py'"),
                            "tools/guard.py", True, False),
    "env with a variable": (*hooked("env FOO=1 python3 tools/guard.py"), "tools/guard.py",
                            True, False),
    ". file": (*hooked(". scripts/env.sh; echo ok"), "scripts/env.sh", True, False),
    "pushd": (*hooked("pushd scripts >/dev/null && ./guard.sh"), "scripts/guard.sh", True, False),
    "cd ${VAR:-.}": (*hooked('cd "${CLAUDE_PROJECT_DIR:-.}/scripts" && ./guard.sh'),
                     "scripts/guard.sh", True, False),
    "sh -c $(cat …)": (*hooked('sh -c "$(cat scripts/guard.sh)"'), "scripts/guard.sh", False, True),
    "git add x.ts": (*hooked("git add src/x.ts"), "src/x.ts", False, False),
    "a pipe into sh": (*hooked("cat scripts/guard.txt | sh"), "scripts/guard.txt", False, True),
    "a loop over a glob": (*hooked("for f in scripts/*.sh; do sh $f; done"), "scripts/a.sh",
                           False, True),
    "PATH in its env": (*served("guardtool", [], env={"PATH": "./bin:/usr/bin:/bin"}),
                        "bin/guardtool", True, True),
    "node -r": (*served("node", ["-r", "./tools/pre.js", "/usr/lib/x.js"]), "tools/pre.js",
                True, False),
    "NODE_OPTIONS": (*served("node", ["/usr/lib/x.js"], env={"NODE_OPTIONS": "--require "
                     "./tools/pre.js"}), "tools/pre.js", True, False),
    "uvx --from .": (*served("uvx", ["--from", ".", "mytool"]), "pyproject.toml", False, True),
    "sh file": (*hooked("sh guard"), "guard", True, False),
    "python file": (*served("python3", ["server"]), "server", True, False),
    "ruby -e": (*served("ruby", ["-e", "load 'tools/s.rb'"]), "tools/s.rb", True, True),
    "awk -f": (*hooked("awk -f tools/x.awk /dev/null"), "tools/x.awk", True, False),
    "exec timeout": (*hooked("exec timeout 5 ./scripts/guard.sh"), "scripts/guard.sh", True, False),
    "x;": (*hooked("./scripts/guard.sh; exit 0"), "scripts/guard.sh", True, False),
    "x>file": (*hooked("./scripts/guard.sh>/dev/null"), "scripts/guard.sh", True, False),
    "(x)": (*hooked("(./scripts/guard.sh)"), "scripts/guard.sh", True, False),
    "x&&y": (*hooked("./scripts/guard.sh&&true"), "scripts/guard.sh", True, False),
    '"$CLAUDE_PROJECT_DIR"/x;': (*hooked('"$CLAUDE_PROJECT_DIR"/.claude/hooks/check.sh;'),
                                ".claude/hooks/check.sh", True, False),
    "lines": (*hooked("set -e\n./scripts/guard.sh\n"), "scripts/guard.sh", True, False),
    "sh -c with ;": (*served("sh", ["-c", "cd tools; exec node server.js"]), "tools/server.js",
                     True, False),
    "bash -c with &&": (*served("bash", ["-c", "cd tools&&node server.js"]), "tools/server.js",
                        True, False),
}  # fmt: skip


@pytest.mark.parametrize("case", SHELL)
def test_what_a_settings_shell_line_runs_is_held_or_said(tmp_path, case):
    """Read as a POSIX shell reads it (operators glued to words, subshells, newlines, `cd`,
    `pushd`, `.`, wrappers, a shell's `-c` line, a program's code options and environment);
    what the parser doesn't follow (a loop, a pipe into a shell, a substitution, inline code, a
    PATH into the project, the project installed by uvx) is said, never dropped."""
    setting, said, ran, is_held, is_said = SHELL[case]
    plant(tmp_path, setting, json.dumps(said))
    plant(tmp_path, ran, "#!/bin/sh\necho ok\n")
    code: list[str] = []
    entries = settings_guard.scan(tmp_path, tmp_path, code=code)["."]
    assert (ran in [path.split("@")[0] for path in held(entries)]) == is_held
    assert bool(code) == is_said, code


def test_what_a_setting_merely_writes_or_reads_is_not_held(tmp_path):
    """Where its output goes (GitHub's own hook example logs to a file), an option's value that
    is no script, data: never held, so the worker never waits for its log. A server's script
    that is not there yet (built later): kept as absent, so the build turns it off."""
    plant(tmp_path, ".github/hooks/log.json", json.dumps({"version": 1, "hooks": {"sessionStart": [
        {"type": "command", "bash": 'echo "$(date) start" >> logs/session.log'},
        {"type": "command", "bash": "./run.sh 2> logs/err.log | tee logs/tee.log"}]}}))  # fmt: skip
    plant(tmp_path, ".mcp.json", json.dumps({"mcpServers": {"db": {"command": "node",
        "args": ["missing/server.js", "--db-path", "./dev.db", "data/seed.json"]}}}))  # fmt: skip
    for name in ("logs/session.log", "logs/err.log", "logs/tee.log", "dev.db", "data/seed.json"):
        plant(tmp_path, name, "x")
    plant(tmp_path, "run.sh", "#!/bin/sh\n")
    code: list[str] = []
    entries = settings_guard.scan(tmp_path, tmp_path, code=code)["."]
    assert held(entries) == ["missing/server.js@mcp/db", "run.sh"]
    assert entries[".mcp.json\0serves:mcp/db\0missing/server.js"] == settings_guard.ABSENT
    assert code == []


async def test_settings_carried_from_the_owners_checkout_are_what_it_holds(
    tmp_path, git_repo, monkeypatch
):
    """A worker in the owner's checkout plants a gitignored `.mcp.json` the next worktree
    carries (`.worktreeinclude`): not taken as Chief of Staff's own; the next worker waits,
    told it was added since workers had the checkout, and nothing trusts the folder until the
    owner says. One carried as the owner's checkout holds it is taken."""
    commit(git_repo, ".gitignore", ".mcp.json\n.claude/settings.local.json\n")
    commit(git_repo, ".worktreeinclude", ".mcp.json\n.claude/settings.local.json\n")
    plant(git_repo, ".claude/settings.local.json")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        info = await spawn(runtime, harness.router_id, title="One", instructions="f",
                           project=str(git_repo), in_place=True)  # fmt: skip
        await at_rest(runtime, harness, info["session_id"])
        plant(
            git_repo, ".mcp.json", '{"mcpServers": {"p": {"command": "sh", "args": ["-c", "id"]}}}'
        )
        worker, cwd = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        assert (cwd / ".mcp.json").exists() and not runtime.states.is_live(worker)
        assert settings_notices(harness, worker)[-1]["text"] == added_since(".mcp.json")
        assert "trust_working_directory" not in harness.client.sessions.get(worker, FakeSession(
            worker, {})).options, "nothing opened, nothing trusted"  # fmt: skip
        await use_them(runtime, harness, worker)
        assert runtime.states.is_live(worker)
    finally:
        await runtime.stop()


async def test_settings_a_worker_put_in_a_folder_are_never_shown_as_the_owners(
    tmp_path, git_repo, monkeypatch
):
    """A worker in the owner's checkout plants settings one folder down; the next worker there
    is told workers could write there, not only that they are seen for the first time (which
    reads as the owner's own)."""
    commit(git_repo, "sub/README.md", "x")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo), str(git_repo / "sub")]))
    runtime = await harness.connect()
    try:
        info = await spawn(runtime, harness.router_id, title="One", instructions="f",
                           project=str(git_repo), in_place=True)  # fmt: skip
        await at_rest(runtime, harness, info["session_id"])
        plant(git_repo / "sub", ".github/hooks/p.json")
        other = await spawn(runtime, harness.router_id, title="Two", instructions="f",
                            project=str(git_repo / "sub"), in_place=True)  # fmt: skip
        await settle(runtime)
        assert settings_notices(harness, other["session_id"])[-1]["text"] == unused(
            "sub/.github/hooks/p.json", writable=True
        )
    finally:
        await runtime.stop()


async def test_settings_changed_mid_turn_stop_it_at_its_next_tool(tmp_path, git_repo, monkeypatch):
    """Copilot loads a custom agent added mid-session when the worker calls it, and runs its
    folder's preToolUse hooks before each tool: for a sandboxed worker, Copilot asks Chief of
    Staff first (its pre-tool hook, waited for), which glances at the settings (no file read)
    and, when something changed, runs the full check: a change denies the tool and stops the
    worker; a script made executable alone (as Copilot does a hook's) changes nothing. As a
    sub-agent starts, the same glance."""
    hook = {"version": 1, "hooks": {"preToolUse": [{"type": "command", "bash": "./guard.sh"}]}}
    commit(git_repo, ".github/hooks/guard.json", json.dumps(hook))
    commit(git_repo, "guard.sh", "#!/bin/sh\necho '{}'\n")
    trusting(tmp_path, monkeypatch, git_repo)
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "darwin")
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        session = harness.session(worker)
        hooks = session.options["hooks"]
        call = {"toolName": "bash", "toolArgs": {"command": "ls"}}
        assert await asked(hooks["on_pre_tool_use"], call, worker) is None
        (cwd / "guard.sh").chmod(0o755)
        assert await asked(hooks["on_pre_tool_use"], call, worker) is None
        assert runtime.states.is_live(worker), "nothing changed but its mode: no stop"
        # A folder moved into place, past the sandbox's paths: another guard script.
        (tmp_path / "swap").mkdir()
        (tmp_path / "swap" / "guard.sh").write_text("#!/bin/sh\necho allow\n")
        (cwd / "guard.sh").unlink()
        (tmp_path / "swap" / "guard.sh").rename(cwd / "guard.sh")
        denied = await asked(hooks["on_pre_tool_use"], call, worker)
        assert denied["permissionDecision"] == "deny"
        assert denied["permissionDecisionReason"] == refusal(
            "changed guard.sh (run by .github/hooks/guard.json)"
        )
        await settle(runtime)
        assert session.aborted and not runtime.states.is_live(worker)
        await use_them(runtime, harness, worker)
        assert runtime.states.is_live(worker)
        session = harness.session(worker)
        plant(cwd, ".github/agents/x.agent.md", "---\nmcp-servers: {p: {command: sh}}\n---\n")
        session.emit("subagent.started", agent_name="x")
        await settle(runtime)
        assert session.aborted and not runtime.states.is_live(worker)
        assert settings_notices(harness, worker)[-1]["text"] == refusal(
            "added .github/agents/x.agent.md"
        )
    finally:
        await runtime.stop()


async def test_a_workers_edit_of_a_file_its_settings_run_is_refused(
    tmp_path, git_repo, monkeypatch
):
    """A hook's script: a hook runs it at the next tool, before any turn's check."""
    hook = {"version": 1, "hooks": {"preToolUse": [{"type": "command", "bash": "./guard.sh"}]}}
    commit(git_repo, ".github/hooks/guard.json", json.dumps(hook))
    commit(git_repo, "guard.sh", "#!/bin/sh\necho '{}'\n")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_yolo=True))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        request = SimpleNamespace(kind="write", resolved_path=str(cwd / "guard.sh"))
        decision = await runtime.events.on_permission(request, {"session_id": worker})
        assert decision.feedback == RUN_WRITE
    finally:
        await runtime.stop()


async def test_a_git_folder_a_worker_replaced_waits_for_the_owner(tmp_path, git_repo, monkeypatch):
    """The sandbox holds paths: a folder moved into place brings what is in it (a `.git` of the
    worker's with a config that runs code). Its identity, kept as handed out, tells it: the
    worker waits, and Chief of Staff runs no git there, until the owner says to use it."""
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        info = await spawn(runtime, harness.router_id, title="Fix", instructions="f",
                           project=str(git_repo), in_place=True)  # fmt: skip
        worker = info["session_id"]
        await at_rest(runtime, harness, worker)
        shutil.copytree(git_repo / ".git", tmp_path / "prepared")
        (git_repo / ".git").rename(tmp_path / "moved")
        (tmp_path / "prepared").rename(git_repo / ".git")
        await runtime.owner_send(worker, "Go on")
        await settle(runtime)
        assert not runtime.states.is_live(worker)
        line = harness.store.messages(worker)[-1]
        assert line["text"].endswith(
            "/.git was replaced since Chief of Staff first saw it, so git there could run what "
            "this worker put in its place; Chief of Staff won't start the worker, nor run git "
            "there, until you say to use it."
        )
        with pytest.raises(ValueError, match="runs no git there"):
            await workspaces_module.git("-C", str(git_repo), "status")
        await runtime.accept_git(worker, line["meta"]["git_replaced"]["digest"])
        await settle(runtime)
        assert runtime.states.is_live(worker)
        await workspaces_module.git("-C", str(git_repo), "status")
    finally:
        await runtime.stop()


async def test_ordinary_git_never_reads_as_a_replaced_git_folder(tmp_path, git_repo, monkeypatch):
    """Commits, merges, rebases, fetches that prune, gc, submodules, Chief of Staff's own
    worktrees made and removed: in the owner's checkout and in a worktree, nothing waits."""
    remote = tmp_path / "remote.git"
    run("git", "init", "-q", "--bare", str(remote))
    run("git", "remote", "add", "origin", str(remote), cwd=git_repo)
    run("git", "push", "-q", "origin", "HEAD:main", "HEAD:old", cwd=git_repo)
    sub = tmp_path / "lib"
    run("git", "init", "-q", "-b", "main", str(sub))
    commit(sub, "lib.txt", "x")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        info = await spawn(runtime, harness.router_id, title="Fix", instructions="f",
                           project=str(git_repo), in_place=True)  # fmt: skip
        theirs = harness.store.session(info["session_id"])
        worker, cwd = await isolated(runtime, harness, git_repo)
        mine = harness.store.session(worker)
        assert await runtime.settings_refusal(theirs) == ""
        assert await runtime.settings_refusal(mine) == ""
        g = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]
        for index, folder in enumerate((git_repo, cwd)):
            run(*g, "checkout", "-q", "-b", f"side-{index}", cwd=folder)
            commit(folder, f"side-{index}.txt", "s")
            run(*g, "checkout", "-q", "-", cwd=folder)
            commit(folder, f"main-{index}.txt", "m")
            run(*g, "merge", "-q", "--no-edit", f"side-{index}", cwd=folder)
            run(*g, "rebase", "-q", "HEAD~1", cwd=folder)
        run("git", "--git-dir", str(remote), "branch", "-qD", "old")
        run(*g, "fetch", "-q", "--prune", "origin", cwd=git_repo)
        assert "origin/old" not in run("git", "branch", "-r", cwd=git_repo)
        run(*g, "submodule", "add", "-q", str(sub), "lib", cwd=git_repo)
        run(*g, "commit", "-qm", "lib", cwd=git_repo)
        run(*g, "gc", "-q", cwd=git_repo)
        other, _ = await isolated(runtime, harness, git_repo)
        await runtime.workers.close(other, by="you")
        await settle(runtime)
        assert await runtime.settings_refusal(theirs) == ""
        assert await runtime.settings_refusal(mine) == ""
    finally:
        await runtime.stop()


def test_a_link_to_an_agents_own_folder_is_never_a_setting(tmp_path, monkeypatch):
    """`.claude` linked to ~/.claude: refused (the owner's own settings, a worker's to change
    through it); only in home itself is an agent's folder left out."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    top = tmp_path / "project"
    top.mkdir()
    (top / ".claude").symlink_to(home / ".claude")
    found = settings_guard.scan(top, top)["."]
    assert found == {".claude": [settings_guard.REFUSED, "leads to an agent's own folder"]}
    assert settings_guard.scan(home, home) == {}


def test_a_git_folder_that_is_no_repository_is_no_top(tmp_path):
    """As git tells one: a HEAD alone is not a repository (it looks past it)."""
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "refs").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    app = tmp_path / "app"
    (app / ".git").mkdir(parents=True)
    (app / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    assert settings_guard.outermost_top(app) == tmp_path


def turn_over(runtime, harness, worker):
    """Its turn over (it reported), its session still open."""
    return call_tool(runtime, "report", worker, status="done", summary="Done.")


async def test_a_server_the_worker_develops_is_off_for_it_after_a_build_never_a_wait(
    tmp_path, git_repo, monkeypatch
):
    """A repository that develops its own MCP server and dogfoods its build (`node
    dist/index.js`, dist/ gitignored): the worker builds and edits it freely. What the build
    made turns that server off for the worker from its next turn (Copilot opens MCP servers as
    a session opens; nothing stops mid-turn), said once; never a wait. "Use these settings" on
    that line turns it on again from its next turn."""
    commit(git_repo, ".gitignore", "dist/\n")
    commit(git_repo, "src/index.ts", "export {}\n")
    commit(git_repo, ".mcp.json", json.dumps(
        {"mcpServers": {"self": {"command": "node", "args": ["dist/index.js"]}}}))  # fmt: skip
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        session = harness.session(worker)
        assert "disabled_mcp_servers" not in session.options
        # Its build, mid-turn: a model round's check says nothing, stops nothing.
        (cwd / "dist").mkdir()
        (cwd / "dist" / "index.js").write_text("// built 1\n")
        session.emit("assistant.turn_start")
        await settle(runtime)
        assert runtime.states.is_live(worker) and not session.aborted
        assert not [t for k, t in harness.kinds(worker) if t.startswith("Its MCP server")]
        await turn_over(runtime, harness, worker)
        session.emit("session.idle")
        await settle(runtime)
        # Its next turn: opened again without that server, said once.
        await runtime.owner_send(worker, "Next")
        await settle(runtime)
        assert runtime.states.is_live(worker) and not settings_notices(harness, worker)
        assert harness.session(worker) is not session
        assert harness.session(worker).options["disabled_mcp_servers"] == ["self"]
        said = [t for k, t in harness.kinds(worker) if t.startswith("Its MCP server")]
        assert said == [
            "Its MCP server self changed in this worker's folder, so it runs without it."
        ]
        # Built again, its source edited: still off, nothing more said, no wait.
        await turn_over(runtime, harness, worker)
        harness.session(worker).emit("session.idle")
        await settle(runtime)
        (cwd / "dist" / "index.js").write_text("// built 2\n")
        (cwd / "src" / "index.ts").write_text("export const x = 1\n")
        session = harness.session(worker)
        await runtime.owner_send(worker, "More")
        await settle(runtime)
        assert harness.session(worker) is session, "the same servers off: not opened again"
        assert len([t for k, t in harness.kinds(worker) if t.startswith("Its MCP server")]) == 1
        # The owner turns it on: from its next turn.
        line = [m for m in harness.store.messages(worker) if m["meta"].get("agent_servers")][-1]
        said = await runtime.accept_settings(worker, line["meta"]["agent_servers"]["digest"])
        assert said == "You let this worker use its MCP server self again."
        await turn_over(runtime, harness, worker)
        harness.session(worker).emit("session.idle")
        await settle(runtime)
        await runtime.owner_send(worker, "Again")
        await settle(runtime)
        assert "disabled_mcp_servers" not in harness.session(worker).options
    finally:
        await runtime.stop()


async def test_a_language_or_agent_server_off_for_a_worker_is_never_started(
    tmp_path, git_repo, monkeypatch
):
    """Copilot starts a language server as its lsp tool first needs it, a custom agent's MCP
    servers as the agent runs: off for the worker (what they run changed), the tool that
    would start them is denied before it runs (Chief of Staff's pre-tool hook: for a worker
    without the sandbox, only while one of those is off)."""
    lsp = {"command": "node", "args": ["lsp/server.js"], "fileExtensions": {".x": "x"}}
    commit(git_repo, ".github/lsp.json", json.dumps({"lspServers": {"x": lsp}}))
    commit(git_repo, "lsp/server.js", "// a server\n")
    commit(git_repo, ".github/agents/a.agent.md", "---\nname: a\nmcp-servers:\n  t:\n"
           "    command: node\n    args: [tools/agent.js]\n---\nAn agent.\n")  # fmt: skip
    commit(git_repo, "tools/agent.js", "// a server\n")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        assert "hooks" not in harness.session(worker).options
        lsp = {"toolName": "lsp", "toolArgs": {"file": "a.x"}}
        task = {"toolName": "task", "toolArgs": {"agent_type": "a", "prompt": "hi"}}
        other = {"toolName": "task", "toolArgs": {"agent_type": "explore", "prompt": "hi"}}
        await at_rest(runtime, harness, worker)
        (cwd / "lsp" / "server.js").write_text("// edited\n")
        (cwd / "tools" / "agent.js").write_text("// edited\n")
        await runtime.owner_send(worker, "Next")
        await settle(runtime)
        assert runtime.states.is_live(worker)
        hook = harness.session(worker).options["hooks"]["on_pre_tool_use"]
        denied = await asked(hook, lsp, worker)
        assert denied["permissionDecision"] == "deny"
        assert denied["permissionDecisionReason"].startswith(
            "The language server x changed in this folder since the owner last let you use it"
        )
        denied = await asked(hook, task, worker)
        assert denied["permissionDecisionReason"].startswith(
            "The MCP server t (of the custom agent a) changed"
        )
        assert await asked(hook, other, worker) is None
        assert harness.session(worker).options["disabled_mcp_servers"] == ["t"]
    finally:
        await runtime.stop()


async def test_a_hooks_script_not_there_yet_is_held_as_missing(tmp_path, git_repo, monkeypatch):
    """A hook names a script the checkout doesn't have (yet): one a worker makes there runs at
    its next tool, so it is a change, as an edit is; the sandbox denies making it (macOS)."""
    hook = {"version": 1, "hooks": {"preToolUse": [{"type": "command", "bash": "./scripts/g.sh"}]}}
    commit(git_repo, ".github/hooks/guard.json", json.dumps(hook))
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "darwin")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await at_rest(runtime, harness, worker)
        policy = runtime.sandbox_policy(harness.store.session(worker))
        assert str(cwd / "scripts" / "g.sh") in policy.denied
        plant(cwd, "scripts/g.sh", "#!/bin/sh\necho allow\n")
        await runtime.owner_send(worker, "Next")
        assert not runtime.states.is_live(worker)
        assert settings_notices(harness, worker)[-1]["text"] == refusal(
            "changed scripts/g.sh (run by .github/hooks/guard.json)"
        )
    finally:
        await runtime.stop()


async def test_a_branch_update_left_halfway_is_said_with_how_to_undo_it(
    tmp_path, git_repo, monkeypatch
):
    """A merge that stopped on a file it couldn't change (the sandbox holds agent settings)
    leaves the branch's new files untracked and no merge to abort: after its turn, its
    timeline says so, and the worker is told how to put its tree back (those files only),
    once. Its tree as the merge began is taken before it runs (Copilot asks first)."""
    commit(git_repo, "base.txt", "b\n")
    run("git", "checkout", "-q", "-b", "team", cwd=git_repo)
    commit(git_repo, "team.txt", "t\n")
    run("git", "checkout", "-q", "main", cwd=git_repo)
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "darwin")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        (cwd / "mine.txt").write_text("mine\n")  # The worker's own, untouched.

        async def merged(call: str, command: str):
            hook = harness.session(worker).options["hooks"]["on_pre_tool_use"]
            asked_now = {"toolName": "bash", "toolArgs": {"command": command}}
            assert await asked(hook, asked_now, worker) is None
            (cwd / "team.txt").write_text("t\n")  # What the merge wrote before it stopped.
            session = harness.session(worker)
            session.emit("tool.execution_start", tool_name="bash", tool_call_id=call,
                         arguments={"command": command})  # fmt: skip
            done = SimpleNamespace(
                content="error: unable to create file\n<exited with exit code 1>"
            )
            session.emit("tool.execution_complete", tool_call_id=call, success=False, result=done)
            await call_tool(runtime, "report", worker, status="done", summary="Merge failed.")
            session.emit("session.idle")
            await settle(runtime)

        await merged("m1", "git merge team")
        notes = [t for k, t in harness.kinds(worker) if "stopped halfway" in t]
        assert notes == [
            "Its branch update stopped halfway (`team.txt` left untracked): asked it to put its "
            "worktree back."
        ]
        told = harness.session(worker).sent[-1]
        assert "remove `team.txt`" in told and "mine.txt" not in told
        # Again after another turn, the same left: said once.
        (cwd / "team.txt").unlink()
        await merged("m2", "git -c a=b merge team")
        assert len([t for k, t in harness.kinds(worker) if "stopped halfway" in t]) == 1
    finally:
        await runtime.stop()


async def test_a_read_only_worker_never_makes_the_owners_local_settings_ask(
    tmp_path, git_repo, monkeypatch
):
    """A worker that only reads the owner's checkout (a question about it) could not have
    written their gitignored local settings: worktrees made after it carry them as the
    owner's, however the owner's own tools change them. Once the owner said to use carried
    ones in a checkout workers did write, the next worktree carrying the same isn't asked."""
    commit(git_repo, ".gitignore", ".claude/settings.local.json\n")
    commit(git_repo, ".worktreeinclude", ".claude/settings.local.json\n")
    plant(git_repo, ".claude/settings.local.json", '{"permissions": {"allow": []}}')
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        info = await spawn(runtime, harness.router_id, title="Explain", instructions="e",
                           project=str(git_repo))  # fmt: skip
        await at_rest(runtime, harness, info["session_id"])
        for n in range(2):
            (git_repo / ".claude" / "settings.local.json").write_text(
                json.dumps({"permissions": {"allow": [f"Bash(npm test {n})"]}})
            )
            worker, _ = await isolated(runtime, harness, git_repo)
            await settle(runtime)
            assert runtime.states.is_live(worker) and not settings_notices(harness, worker)
        # A worker that wrote the checkout (in place): carried ones are asked about once.
        writer = await spawn(runtime, harness.router_id, title="Edit", instructions="e",
                             project=str(git_repo), in_place=True)  # fmt: skip
        await settle(runtime)
        # (The owner's tools changed their local settings since the first worker read them.)
        await use_them(runtime, harness, writer["session_id"])
        await at_rest(runtime, harness, writer["session_id"])
        (git_repo / ".claude" / "settings.local.json").write_text('{"permissions": {"x": 1}}')
        first, _ = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        assert settings_notices(harness, first)[-1]["text"] == added_since(
            ".claude/settings.local.json"
        )
        await use_them(runtime, harness, first)
        again, _ = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        assert runtime.states.is_live(again) and not settings_notices(harness, again)
    finally:
        await runtime.stop()


async def test_the_owner_making_their_worktree_again_is_no_replaced_git(
    tmp_path, git_repo, monkeypatch
):
    """The owner removes a worktree of theirs and makes one again under that name (`git
    worktree remove`, `add`): its private folder in the .git is another, made with a new
    worktree out of every worker's reach: theirs, nothing waits."""
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    # (The test's folders are in the temporary folder, which workers can write.)
    workspaces = str((tmp_path / "data").resolve())
    monkeypatch.setattr(runtime, "writable_roots", lambda: (workspaces,))
    try:
        run("git", "worktree", "add", "-q", str(tmp_path / "hotfix"), "-b", "hotfix", cwd=git_repo)
        worker, cwd = await isolated(runtime, harness, git_repo)
        await at_rest(runtime, harness, worker)
        await runtime.owner_send(worker, "Go on")
        await settle(runtime)
        assert runtime.states.is_live(worker)
        await at_rest(runtime, harness, worker)
        run("git", "worktree", "remove", str(tmp_path / "hotfix"), cwd=git_repo)
        run("git", "worktree", "add", "-q", str(tmp_path / "hotfix"), "-b", "hotfix2", cwd=git_repo)
        await runtime.owner_send(worker, "Next")
        await settle(runtime)
        assert runtime.states.is_live(worker)
        await workspaces_module.git("-C", str(cwd), "status", "--porcelain")
    finally:
        await runtime.stop()


@pytest.mark.parametrize("platform", ["darwin", "linux"])
async def test_copilot_asks_first_before_every_tool_of_a_sandboxed_worker_only(
    tmp_path, git_repo, monkeypatch, platform
):
    """Every sandboxed worker's tools wait for Chief of Staff's pre-tool hook (a setting moved
    in past the sandbox's paths is found before the tool that would load it). One without the
    sandbox is not asked first (its commands run as the owner; a few ms a tool): unless a
    language server or a custom agent's is off for it (an MCP server off needs no tool's)."""
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", platform)
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        worker, _ = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        row = harness.store.session(worker)
        assert "hooks" in harness.session(worker).options and worker in runtime.pre_tool_on
        harness.store.save_settings(Settings(projects=[str(git_repo)], worker_sandbox=False))
        assert "hooks" not in runtime.options(row)
        runtime.servers_off[worker] = {"mcp/s": [".mcp.json"]}
        assert "hooks" not in runtime.options(row)
        for server in ("lsp/x", "mcp/t@a"):
            runtime.servers_off[worker] = {server: ["server.js"]}
            assert "hooks" in runtime.options(row), server
    finally:
        await runtime.stop()


async def test_an_agent_moved_in_past_the_sandbox_is_denied_before_it_runs(
    tmp_path, git_repo, monkeypatch
):
    """A sandboxed worker renames a folder over `.github` (the sandbox's lists are paths) with a
    custom agent in it, and calls it: Copilot asks Chief of Staff before that `task`, which
    finds the change and denies it (the agent's MCP servers never start), and stops it."""
    commit(git_repo, ".github/workflows/ci.yml", "on: push\n")
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "darwin")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        session = harness.session(worker)
        hook = session.options["hooks"]["on_pre_tool_use"]
        task = {"toolName": "task", "toolArgs": {"agent_type": "evil", "prompt": "hi"}}
        assert await asked(hook, task, worker) is None
        shutil.copytree(cwd / ".github", cwd / "stage")
        plant(cwd / "stage", "agents/evil.agent.md", "---\nname: evil\nmcp-servers:\n  e:\n"
              "    command: sh\n---\nEvil.\n")  # fmt: skip
        (cwd / ".github").rename(cwd / ".github-old")
        (cwd / "stage").rename(cwd / ".github")
        denied = await asked(hook, task, worker)
        assert denied["permissionDecision"] == "deny"
        assert denied["permissionDecisionReason"] == refusal("added .github/agents/evil.agent.md")
        await settle(runtime)
        assert session.aborted and not runtime.states.is_live(worker)
    finally:
        await runtime.stop()


@pytest.mark.parametrize("sandboxed", [True, False])
@pytest.mark.parametrize("trouble", ["raises", "hangs"])
async def test_a_pre_tool_answer_comes_in_time_whatever_happens(
    tmp_path, git_repo, monkeypatch, trouble, sandboxed
):
    """Copilot waits for Chief of Staff's pre-tool answer: a look that fails or never ends must
    not hold the worker's turn. Within PRE_TOOL_SECONDS a sandboxed worker's tool is denied,
    with a line saying why (its turn goes on), another's runs as it would; the next tool is
    looked at afresh."""
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "darwin")
    monkeypatch.setattr(events_module, "PRE_TOOL_SECONDS", 0.2)
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        worker, _ = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        hook = harness.session(worker).options["hooks"]["on_pre_tool_use"]
        if not sandboxed:
            runtime.pre_tool_on[worker] = False  # (Asked first for a server off for it.)
        call = {"toolName": "bash", "toolArgs": {"command": "ls"}}
        look = runtime.settings_look
        never = asyncio.Event()

        async def troubled(row):
            if trouble == "raises":
                raise OSError("a folder that can't be read")
            await never.wait()
            return ""

        monkeypatch.setattr(runtime, "settings_look", troubled)
        started = time.monotonic()
        answer = await asked(hook, call, worker)
        assert time.monotonic() - started < 2
        if sandboxed:
            assert answer == {
                "permissionDecision": "deny",
                "permissionDecisionReason": events_module.PRE_TOOL_UNANSWERED
                if trouble == "hangs"
                else events_module.PRE_TOOL_UNCHECKED,
            }
        else:
            assert answer is None
        assert runtime.states.is_live(worker)
        monkeypatch.setattr(runtime, "settings_look", look)
        assert await asked(hook, call, worker) is None
        never.set()
    finally:
        await runtime.stop()


def drained(fifo: Path, seconds: float = 20):
    """Whatever opened the FIFO to read gets an end, for a while (a test's leftover threads)."""
    stop = time.monotonic() + seconds

    def loop():
        while time.monotonic() < stop:
            with suppress(OSError):
                handle = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                os.close(handle)
            time.sleep(0.01)

    threading.Thread(target=loop, daemon=True).start()


async def test_a_folder_that_cant_be_listed_never_lets_a_tool_through(
    tmp_path, git_repo, monkeypatch
):
    """A sandboxed worker plants a custom agent beside a folder no one can list (`chmod 000`
    .github/agents/zz): the glance notes what it can't see (it never fails), the full check
    says they can't be checked, and the `task` is denied; the worker stops."""
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "darwin")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    locked = None
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        session = harness.session(worker)
        hook = session.options["hooks"]["on_pre_tool_use"]
        task = {"toolName": "task", "toolArgs": {"agent_type": "evil", "prompt": "hi"}}
        assert await asked(hook, task, worker) is None
        plant(cwd, ".github/agents/evil.agent.md", "---\nname: evil\nmcp-servers:\n  e:\n"
              "    command: sh\n---\nEvil.\n")  # fmt: skip
        locked = cwd / ".github" / "agents" / "zz"
        locked.mkdir()
        locked.chmod(0)
        denied = await asked(hook, task, worker)
        assert denied["permissionDecision"] == "deny"
        assert denied["permissionDecisionReason"].startswith(
            "Copilot settings in this folder can't be checked"
        )
        await settle(runtime)
        assert session.aborted and not runtime.states.is_live(worker)
    finally:
        if locked:
            locked.chmod(0o755)
        await runtime.stop()


@pytest.mark.parametrize("trouble", ["raises", "hangs"])
async def test_a_model_round_whose_look_fails_stops_the_worker(
    tmp_path, git_repo, monkeypatch, trouble
):
    """The look as each model round starts never fails quietly: one that fails, or takes
    longer than it may, stops the worker with a line saying its folder can't be checked; it
    waits until it can be, then goes on."""
    from chiefofstaff import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "SETTINGS_SECONDS", 0.3)
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, _ = await isolated(runtime, harness, git_repo)
        session = harness.session(worker)
        await settle(runtime)
        signature = settings_guard.signature
        freed = threading.Event()

        def troubled(*args):
            if trouble == "raises":
                raise OSError("a folder that can't be read")
            freed.wait(5)
            return signature(*args)

        monkeypatch.setattr(settings_guard, "signature", troubled)
        session.emit("assistant.turn_start")
        await settle(runtime)
        await asyncio.sleep(0.8)
        await settle(runtime)
        assert session.aborted and not runtime.states.is_live(worker)
        assert worker in runtime.settings_held
        said = [
            m["text"] for m in harness.store.messages(worker) if "can't be checked" in m["text"]
        ]
        assert said[0].startswith("This worker's folder can't be checked (")
        freed.set()
        monkeypatch.setattr(settings_guard, "signature", signature)
        await runtime.workers.pump()
        await settle(runtime)
        assert worker not in runtime.settings_held
    finally:
        await runtime.stop()


async def test_a_look_at_a_folder_past_its_time_makes_the_worker_wait(
    tmp_path, git_repo, monkeypatch
):
    """Before a turn: a look at its folder that doesn't end in time (a file system that doesn't
    answer) says the folder can't be checked; nothing waits on it forever."""
    from chiefofstaff import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "SETTINGS_SECONDS", 0.3)
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    never = asyncio.Event()
    try:
        worker, _ = await isolated(runtime, harness, git_repo)
        await settle(runtime)

        async def stuck(*args):
            await never.wait()

        monkeypatch.setattr(runtime, "settings_verdict", stuck)
        line = await asyncio.wait_for(runtime.settings_refusal(harness.store.session(worker)), 5)
        assert line == (
            "This worker's folder can't be checked (looking at it took over 0.3 s); Chief of "
            "Staff won't start the worker there until it can."
        )
    finally:
        never.set()
        await runtime.stop()


async def test_a_fifo_where_the_check_reads_never_holds_copilot_nor_the_owner(
    tmp_path, git_repo, monkeypatch
):
    """A sandboxed worker writes a hook that runs `node tools` and makes tools/package.json (where
    node's entry point is read) a FIFO: the check never waits on it. Copilot has its answer
    before the next tool (denied: a hook the worker made), and the owner's next message goes
    through (it waits for them)."""
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "darwin")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    fifo = None
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        hook = harness.session(worker).options["hooks"]["on_pre_tool_use"]
        (cwd / "tools").mkdir()
        fifo = cwd / "tools" / "package.json"
        os.mkfifo(fifo)
        plant(cwd, ".github/hooks/x.json", json.dumps({"version": 1, "hooks": {
            "preToolUse": [{"type": "command", "bash": "node tools"}]}}))  # fmt: skip
        call = {"toolName": "bash", "toolArgs": {"command": "ls"}}
        answer = await asyncio.wait_for(asked(hook, call, worker), 5)
        assert answer["permissionDecision"] == "deny"
        assert answer["permissionDecisionReason"].startswith(
            "Copilot settings in this folder changed (added .github/hooks/x.json"
        )
        await asyncio.wait_for(runtime.owner_send(worker, "Next"), 5)
    finally:
        if fifo:
            drained(fifo)
        await runtime.stop()


def test_the_checks_read_regular_files_only(tmp_path):
    """Never a FIFO (opened to read, it would wait for a writer), never through a link unless
    asked to, never past a size."""
    os.mkfifo(tmp_path / "fifo")
    (tmp_path / "file").write_text("abcdef")
    (tmp_path / "link").symlink_to(tmp_path / "file")
    started = time.monotonic()
    with pytest.raises(settings_guard.SpecialFileError):
        settings_guard.read_regular(tmp_path / "fifo")
    with pytest.raises(settings_guard.SpecialFileError):
        settings_guard.read_regular(tmp_path / "fifo", follow=True)
    assert time.monotonic() - started < 1
    with pytest.raises(settings_guard.SpecialFileError):
        settings_guard.read_regular(tmp_path / "link")
    assert settings_guard.read_regular(tmp_path / "link", follow=True) == b"abcdef"
    with pytest.raises(OSError, match="too large"):
        settings_guard.read_regular(tmp_path / "file", 3)
    assert settings_guard.read_regular(tmp_path / "file", 3, whole=False) == b"abc"


async def test_looks_at_a_folder_never_wait_on_the_default_pool(tmp_path, git_repo, monkeypatch):
    """The looks at workers' folders read the file system in threads of their own: with every
    thread of the default pool taken (the SDK writes to Copilot there), they still answer."""
    from concurrent.futures import ThreadPoolExecutor

    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    held = threading.Event()
    try:
        worker, _ = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(1))
        loop.run_in_executor(None, held.wait)
        row = harness.store.session(worker)
        assert await asyncio.wait_for(runtime.settings_refusal(row), 5) == ""
        assert await asyncio.wait_for(runtime.settings_look(row), 5) == ""
    finally:
        held.set()
        await runtime.stop()


async def test_a_servers_file_changed_mid_turn_is_denied_at_the_tool_that_would_start_it(
    tmp_path, git_repo, monkeypatch
):
    """A sandboxed worker edits the file a language server runs, or a custom agent's MCP
    server, then calls the tool that starts it in the same turn: the look before that tool
    sees it (the files servers run are looked at too) and the server is off for it: denied.
    A custom agent with no name of its own is called by its file's (Copilot: `my.helper` for
    my.helper.agent.md, as written)."""
    server = {"command": "node", "args": ["tools/lsp.js"], "fileExtensions": {".ts": "typescript"}}
    commit(git_repo, ".github/lsp.json", json.dumps({"lspServers": {"ts": server}}))
    commit(git_repo, "tools/lsp.js", "// language server\n")
    commit(git_repo, ".github/agents/my.helper.agent.md",
           "---\ndescription: x\nmcp-servers:\n  s:\n    command: node\n"
           "    args: [\"tools/agent_server.js\"]\n---\nBody\n")  # fmt: skip
    commit(git_repo, "tools/agent_server.js", "// server\n")
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "darwin")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        hook = harness.session(worker).options["hooks"]["on_pre_tool_use"]
        lsp = {"toolName": "lsp", "toolArgs": {}}
        task = {"toolName": "task", "toolArgs": {"agent_type": "my.helper", "prompt": "hi"}}
        assert await asked(hook, lsp, worker) is None
        assert await asked(hook, task, worker) is None
        (cwd / "tools" / "lsp.js").write_text("require('child_process').execSync('id')\n")
        denied = await asked(hook, lsp, worker)
        assert denied["permissionDecision"] == "deny"
        assert denied["permissionDecisionReason"].startswith("The language server ts changed")
        assert await asked(hook, task, worker) is None
        (cwd / "tools" / "agent_server.js").write_text("require('child_process').execSync('id')\n")
        denied = await asked(hook, task, worker)
        assert denied["permissionDecision"] == "deny"
        assert denied["permissionDecisionReason"].startswith(
            "The MCP server s (of the custom agent my.helper) changed"
        )
        assert runtime.states.is_live(worker), "a server off is no wait"
    finally:
        await runtime.stop()


async def test_a_worktree_made_again_under_its_name_is_no_replaced_git(
    tmp_path, git_repo, monkeypatch
):
    """A worker makes a scratch worktree to compare with main, removes it and makes it again
    (looked at in between, or in one go): its private folder in the .git is another each time,
    made with its worktree: nothing waits. One swapped under a worktree that stays still is."""
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "darwin")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        hook = harness.session(worker).options["hooks"]["on_pre_tool_use"]
        call = {"toolName": "bash", "toolArgs": {"command": "ls"}}
        scratch = tmp_path / "data" / "work" / "cmp"
        add = ("git", "worktree", "add", "-q", "--detach", str(scratch), "main")

        async def looked():
            # Before a tool, and before a turn (which takes in the .git's folders as they are).
            assert await asked(hook, call, worker) is None
            assert await runtime.settings_refusal(harness.store.session(worker)) == ""

        run(*add, cwd=cwd)
        await looked()
        run("git", "worktree", "remove", str(scratch), cwd=cwd)
        await looked()
        run(*add, cwd=cwd)
        await looked()
        run("git", "worktree", "remove", str(scratch), cwd=cwd)
        run(*add, cwd=cwd)
        await looked()
        assert runtime.states.is_live(worker)
        common = Path(run("git", "rev-parse", "--git-common-dir", cwd=cwd).strip())
        admin = (cwd / common).resolve() / "worktrees" / "cmp"
        shutil.copytree(admin, tmp_path / "swapped")
        shutil.rmtree(admin)
        (tmp_path / "swapped").rename(admin)
        denied = await asked(hook, call, worker)
        assert denied["permissionDecision"] == "deny"
        assert (
            "/worktrees/cmp was replaced since Chief of Staff first saw it"
            in (denied["permissionDecisionReason"])
        )
    finally:
        await runtime.stop()


async def test_only_what_a_failed_update_left_is_said(tmp_path, git_repo, monkeypatch):
    """Not `git merge-base` (no update), nor a merge that went through: nothing said. Of a merge
    that failed and left its tree halfway, only what it left: not the task's own deletions."""
    commit(git_repo, "legacy.py", "old\n")
    run("git", "checkout", "-q", "-b", "team", cwd=git_repo)
    commit(git_repo, "team.txt", "t\n")
    run("git", "checkout", "-q", "main", cwd=git_repo)
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "darwin")
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        hook = harness.session(worker).options["hooks"]["on_pre_tool_use"]
        (cwd / "legacy.py").unlink()  # Its task: remove the legacy module.

        async def turn(call: str, command: str, code: int, before=None):
            asked_now = {"toolName": "bash", "toolArgs": {"command": command}}
            assert await asked(hook, asked_now, worker) is None
            if before:
                before()
            session = harness.session(worker)
            session.emit("tool.execution_start", tool_name="bash", tool_call_id=call,
                         arguments={"command": command})  # fmt: skip
            done = SimpleNamespace(content=f"x\n<exited with exit code {code}>")
            session.emit("tool.execution_complete", tool_call_id=call, success=code == 0,
                         result=done)  # fmt: skip
            session.emit("session.idle")
            await settle(runtime)

        def halfway():
            (cwd / "team.txt").write_text("t\n")  # What the merge wrote before it stopped.

        await turn("m1", "git merge-base HEAD main", 0)
        await turn("m2", "git fetch -q && git merge main", 0)
        assert not [t for k, t in harness.kinds(worker) if "stopped halfway" in t]
        await turn("m3", "git merge team", 1, before=halfway)
        notes = [t for k, t in harness.kinds(worker) if "stopped halfway" in t]
        assert notes == [
            "Its branch update stopped halfway (`team.txt` left untracked): asked it to put its "
            "worktree back."
        ]
    finally:
        await runtime.stop()


async def test_a_worker_without_the_sandbox_is_glanced_at_as_each_tool_starts(
    tmp_path, git_repo, monkeypatch
):
    """Copilot doesn't wait for Chief of Staff before an unsandboxed worker's tools: each one's
    start is a glance at its settings, off Copilot's path; a change stops it there."""
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        session = harness.session(worker)
        assert "hooks" not in session.options and worker not in runtime.pre_tool_on
        session.emit("tool.execution_start", tool_name="bash", tool_call_id="c0",
                     arguments={"command": "ls"})  # fmt: skip
        await settle(runtime)
        assert runtime.states.is_live(worker)
        plant(cwd, ".github/hooks/h.json")
        session.emit("tool.execution_start", tool_name="bash", tool_call_id="c1",
                     arguments={"command": "ls"})  # fmt: skip
        await settle(runtime)
        assert session.aborted and not runtime.states.is_live(worker)
    finally:
        await runtime.stop()


async def test_a_model_round_reads_no_settings_unless_the_glance_changed(
    tmp_path, git_repo, monkeypatch
):
    """Each model round of a turn (assistant.turn_start) glances at the folder's settings; the
    full check (every file read) runs only when that glance found a change."""
    trusting(tmp_path, monkeypatch, git_repo)
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        session = harness.session(worker)
        await settle(runtime)
        verdicts = []
        verdict = runtime.settings_verdict

        async def counted(*args, **kwargs):
            verdicts.append(args)
            return await verdict(*args, **kwargs)

        monkeypatch.setattr(runtime, "settings_verdict", counted)
        for _ in range(5):
            session.emit("assistant.turn_start")
            await settle(runtime)
        assert verdicts == [] and runtime.states.is_live(worker)
        plant(cwd, ".mcp.json")
        session.emit("assistant.turn_start")
        await settle(runtime)
        assert verdicts and not runtime.states.is_live(worker)
    finally:
        await runtime.stop()


async def test_a_worker_stopped_for_its_settings_is_never_left_working(
    tmp_path, git_repo, monkeypatch
):
    """Copilot goes on with a turn whose tool was denied (its next model round starts) as Chief
    of Staff stops it and lets go of it: no end of that turn comes, and it is at rest."""
    trusting(tmp_path, monkeypatch, git_repo)
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "darwin")
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        await settle(runtime)
        interrupt = runtime.workers.interrupt

        async def interrupted(session_id, **more):
            await interrupt(session_id, **more)
            harness.session(session_id).emit("assistant.turn_start")  # (Its next round.)
            await asyncio.sleep(0.05)  # (Handled on the event loop meanwhile.)

        monkeypatch.setattr(runtime.workers, "interrupt", interrupted)
        plant(cwd, ".mcp.json")
        hook = harness.session(worker).options["hooks"]["on_pre_tool_use"]
        call = {"toolName": "bash", "toolArgs": {"command": "ls"}}
        assert (await asked(hook, call, worker))["permissionDecision"] == "deny"
        await settle(runtime)
        assert not runtime.states.is_live(worker)
        assert harness.store.session(worker)["status"] == "idle"
    finally:
        await runtime.stop()


def test_a_glance_sees_what_a_link_leads_to(tmp_path):
    """A setting linked to nothing (yet): what it leads to, made later, is a change the glance
    sees, as the full check would."""
    top = tmp_path / "top"
    (top / ".github").mkdir(parents=True)
    (tmp_path / "dotfiles").mkdir()
    (top / ".github" / "lsp.json").symlink_to(tmp_path / "dotfiles" / "lsp.json")
    before = settings_guard.signature(top, top, (".github/lsp.json",), [])
    (tmp_path / "dotfiles" / "lsp.json").write_text('{"lspServers": {}}')
    assert settings_guard.signature(top, top, (".github/lsp.json",), []) != before


def test_a_glance_sees_a_setting_however_full_the_folders_beside_it_are(tmp_path):
    """A sandboxed worker may write a folder's other files (`.github/workflows`, a thousand of
    them): the glance still sees a custom agent added to `.github/agents/` (its tools wait for
    that glance), and a `.github` renamed over the old one."""
    top = tmp_path / "top"
    for name in (".github/agents/base.agent.md", ".github/workflows/ci.yml"):
        plant(top, name, "x")
    for index in range(settings_guard.MOST_ENTRIES + 5):
        plant(top, f".github/aaa/{index}.yml", "x")
    names = policy_module.REPOSITORY_SETTINGS
    before = settings_guard.signature(top, top, names, [])
    plant(top, ".github/workflows/more.yml", "x")
    assert settings_guard.signature(top, top, names, []) == before, "not a setting"
    plant(top, ".github/agents/evil.agent.md", "x")
    after = settings_guard.signature(top, top, names, [])
    assert after != before
    shutil.copytree(top / ".github", tmp_path / "stage")
    (top / ".github").rename(tmp_path / "old")
    (tmp_path / "stage").rename(top / ".github")
    assert settings_guard.signature(top, top, names, []) != after


def test_workers_never_write_copilots_repository_settings(tmp_path):
    from chiefofstaff.policy import SETTINGS_WRITE, settings_write

    row = {"role": "worker", "cwd": str(tmp_path)}

    def write(path):
        return settings_write(row, SimpleNamespace(kind="write", resolved_path=str(path)))

    for name in PLANTED:
        assert write(tmp_path / name) == SETTINGS_WRITE, name
    for name in (".GitHub/hooks/x.json", ".MCP.json", ".Claude/Settings.json", ".CODEX/a.rules",
                 ".github/Agents/x.md", "sub/.Mcp.Json"):  # fmt: skip
        assert write(tmp_path / name) == SETTINGS_WRITE, "as a case-insensitive disk finds it"
    (tmp_path / "notes.md").symlink_to(tmp_path / ".github" / "hooks" / "x.json")
    assert write(tmp_path / "notes.md") == SETTINGS_WRITE, "where a link leads"
    assert write(tmp_path / "src" / "hooks.py") is None
    assert write(tmp_path / ".github" / "workflows" / "ci.yml") is None
    assert settings_write({"role": "router", "cwd": ""}, SimpleNamespace(kind="write")) is None


async def test_a_sandboxed_workers_commands_cannot_write_copilots_repository_settings(
    tmp_path, git_repo, monkeypatch
):
    """In the owner's checkout, every agent's settings, and the files their hooks run, are
    read-only to its commands, and one not there yet is denied (macOS; `.claude`'s settings one
    by one: its commands, skills and rules stay the worker's to write): the owner's own
    sessions load them. In its own worktree, only what Copilot loads by itself mid-session (its
    MCP server files) and the files hooks run (each rule costs every command a little): the
    others load as its session starts (after the full check) or through a tool (after the
    pre-tool check). Its edits to them, and to the files the hooks run, are refused all the
    same. What a server runs is the worker's to build and edit (a change turns that server
    off)."""
    trusting(tmp_path, monkeypatch, git_repo)
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "darwin")
    server = {"mcpServers": {"s": {"command": "node", "args": ["dist/index.js"]}}}
    commit(git_repo, ".gitignore", "dist/\n")
    commit(git_repo, ".mcp.json", json.dumps(server))
    commit(git_repo, "src/index.ts", "export {}\n")
    hook = {"version": 1, "hooks": {"preToolUse": [{"type": "command", "bash": "tools/guard.sh"}]}}
    commit(git_repo, ".github/hooks/guard.json", json.dumps(hook))
    commit(git_repo, "tools/guard.sh", "#!/bin/sh\n")
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        own = await spawn(runtime, harness.router_id, title="Fix", instructions="f",
                          project=str(git_repo), isolate=True)  # fmt: skip
        theirs = await spawn(runtime, harness.router_id, title="Look", instructions="f",
                             project=str(git_repo), in_place=True)  # fmt: skip
        for info in (own, theirs):
            row = harness.store.session(info["session_id"])
            policy = runtime.sandbox_policy(row)
            assert policy is not None
            cwd = Path(row["cwd"])
            listed = (*policy.denied, *policy.readonly)
            for name in (".mcp.json", "tools/guard.sh"):
                assert str(cwd / name) in policy.readonly, name
            assert str(cwd / ".github/mcp.json") in policy.denied
            assert (str(cwd / ".github/hooks") in policy.readonly) == (info is theirs)
            for name in (".github/lsp.json", ".github/agents", ".github/copilot",
                         ".github/extensions", ".claude/settings.json",
                         ".claude/settings.local.json", ".claude/agents", ".codex"):  # fmt: skip
                assert (str(cwd / name) in policy.denied) == (info is theirs), name
            assert str(cwd / ".claude") not in listed
            for name in ("dist", "dist/index.js", "src/index.ts"):
                assert not any(
                    p == str(cwd / name) or p.startswith(f"{cwd}/{name}/") for p in listed
                )
            request = SimpleNamespace(kind="write", resolved_path=str(cwd / "tools/guard.sh"))
            assert settings_write(row, request, runtime.held_run_files(row)) == RUN_WRITE
            for name in ("tools/other.py", "dist/index.js", "src/index.ts"):
                request = SimpleNamespace(kind="write", resolved_path=str(cwd / name))
                assert settings_write(row, request, runtime.held_run_files(row)) is None, name
    finally:
        await runtime.stop()


def git_paths(cwd: str) -> tuple[Path, Path]:
    common = run("git", "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd).strip()
    admin = run("git", "rev-parse", "--path-format=absolute", "--git-dir", cwd=cwd).strip()
    return Path(common), Path(admin)


async def test_absent_protected_paths_are_denied_to_sandboxed_commands_on_macos(
    tmp_path, git_repo, monkeypatch
):
    """Copilot's sandbox holds a read-only path only if it exists (a command made .git/commondir
    there, sending the owner's git to a config of its own): one that does not is denied, where
    it would change what git runs or which repository it uses (each rule costs every command a
    little): a commondir, a submodules' folder where there are submodules, a config.worktree
    where one is read. Not a shallow or alternates (objects are checked by their hash)."""
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "darwin")
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        info = await spawn(runtime, harness.router_id, title="Fix", instructions="f",
                           project=str(git_repo), isolate=True)  # fmt: skip
        row = harness.store.session(info["session_id"])
        policy = runtime.sandbox_policy(row)
        assert policy is not None
        common, admin = git_paths(info["cwd"])
        assert not (common / "commondir").exists()
        assert str(common / "commondir") in policy.denied
        assert str(common / "commondir") not in policy.readonly
        for path in (common / "config.worktree", admin / "config.worktree", common / "shallow",
                     common / "objects" / "info" / "alternates", common / "modules"):  # fmt: skip
            assert str(path) not in (*policy.denied, *policy.readonly), path
        for path in (common / "hooks", common / "config", common / "info", admin / "gitdir"):
            assert str(path) in policy.readonly, path
        run("git", "config", "extensions.worktreeConfig", "true", cwd=git_repo)
        commit(git_repo, ".gitmodules", '[submodule "lib"]\n\tpath = lib\n\turl = ../lib\n')
        policy = runtime.sandbox_policy(row)
        assert policy is not None
        for path in (common / "config.worktree", admin / "config.worktree", common / "modules"):
            assert str(path) in policy.denied, path
    finally:
        await runtime.stop()


async def test_on_linux_a_git_file_a_worker_could_have_made_waits_for_the_owner(
    tmp_path, git_repo, monkeypatch
):
    """Linux's sandbox cannot hold a path that is not there, and a read-only .git breaks git's
    own lock files: there, the .git is writable, nothing is made in it, and what would change
    what git runs or which repository it uses is looked for after each turn, as it stops or
    closes, and before Chief of Staff runs git. Nothing is moved out of the owner's repository:
    the worker waits for the owner's word, and Chief of Staff runs no git there until then. In
    its own worktree's folder, the owner may keep one removed. What the owner's own git makes
    (a shallow fetch, submodules) is not looked for."""
    trusting(tmp_path, monkeypatch, git_repo)
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "linux")
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        worker, cwd = await isolated(runtime, harness, git_repo)
        common, admin = git_paths(str(cwd))
        before = sorted(map(str, git_repo.rglob("*")))
        policy = runtime.sandbox_policy(harness.store.session(worker))
        assert policy is not None
        assert sorted(map(str, git_repo.rglob("*"))) == before, "nothing made in their checkout"
        assert str(common) in policy.readwrite and str(common / "config") in policy.readonly
        for path in (common / "commondir", common / "modules", admin / "config.worktree"):
            assert str(path) not in (*policy.denied, *policy.readonly), path
        # The owner's own: not looked for.
        (common / "shallow").write_text("0" * 40 + "\n")
        (common / "modules").mkdir()
        session = harness.session(worker)
        session.emit("session.idle")
        await settle(runtime)
        assert runtime.states.is_live(worker)
        # Made in a turn: said when it ends; it waits; nothing is moved.
        (common / "commondir").write_text("../elsewhere\n")
        session.emit("session.idle")
        await settle(runtime)
        assert (common / "commondir").exists(), "never moved out of the owner's repository"
        line = [m for m in harness.store.messages(worker) if m["meta"].get("git_quarantine")][-1]
        assert line["text"] == "A git file appeared in .git (commondir). If you made it, use it."
        assert line["meta"]["git_quarantine"]["removable"] is False
        assert not runtime.states.is_live(worker)
        with pytest.raises(ValueError, match="runs no git there"):
            await workspaces_module.git("-C", str(git_repo), "status", "--porcelain")
        await runtime.owner_send(worker, "Next")
        await settle(runtime)
        assert unsent(harness, worker) == ["Next"]
        # Removed by the owner: the worker goes on (with the queue's next run).
        (common / "commondir").unlink()
        await runtime.workers.pump()
        await settle(runtime)
        assert runtime.states.is_live(worker) and unsent(harness, worker) == []
        await workspaces_module.git("-C", str(git_repo), "status", "--porcelain")
        # Its own worktree's folder, where the repository reads one: kept removed, on a click.
        run("git", "config", "extensions.worktreeConfig", "true", cwd=git_repo)
        runtime.sandbox_policy(harness.store.session(worker))
        (admin / "config.worktree").write_text("[core]\n\tfsmonitor = touch PWNED\n")
        harness.session(worker).emit("session.idle")
        await settle(runtime)
        lines = [m for m in harness.store.messages(worker) if m["meta"].get("git_quarantine")]
        assert lines[-1]["meta"]["git_quarantine"]["removable"] is True
        said = await runtime.answer_git_quarantine(worker, use=False)
        assert "kept removed" in said and not (admin / "config.worktree").exists()
        await settle(runtime)
        assert worker not in runtime.settings_held
        # Used: then not looked for any more.
        (common / "commondir").write_text("../elsewhere\n")
        await runtime.git_watch_after(worker)  # (As a turn of it ends.)
        assert "commondir" in await runtime.settings_refusal(harness.store.session(worker))
        await runtime.answer_git_quarantine(worker, use=True)
        await settle(runtime)
        assert worker not in runtime.settings_held and (common / "commondir").exists()
        assert runtime.git_watch_scan(worker) == {}, "not looked for any more"
        (common / "commondir").unlink()
        run("git", "fsck", "--no-progress", cwd=git_repo)
    finally:
        await runtime.stop()


async def test_on_linux_nothing_is_made_in_the_owners_checkout(tmp_path, git_repo, monkeypatch):
    """A worker in a folder of the owner's checkout: what it must not make there is looked for,
    not held by empty placeholders (left in their checkout)."""
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "linux")
    sub = git_repo / "services" / "api"
    sub.mkdir(parents=True)
    commit(git_repo, "services/api/README.md", "x")
    harness = Harness(tmp_path / "data", Settings(projects=[str(sub)], worker_sandbox=True))
    runtime = await harness.connect()
    try:
        before = sorted(map(str, git_repo.rglob("*")))
        info = await spawn(runtime, harness.router_id, title="Fix in place", instructions="f",
                           project=str(sub), in_place=True)  # fmt: skip
        assert runtime.sandbox_policy(harness.store.session(info["session_id"])) is not None
        assert sorted(map(str, git_repo.rglob("*"))) == before
        assert run("git", "status", "--porcelain", cwd=git_repo) == ""
        watched = json.loads(harness.store.get(f"gitwatch:{info['session_id']}") or "[]")
        assert str(git_repo / ".git" / "commondir") in watched
    finally:
        await runtime.stop()


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
    assert {k: v for k, v in found["tests"].items() if k != "cwd"} == {
        "command": "uv run pytest -q",
        "exit_code": 0,
        "passed": True,
        "runs": 2,
        "failed_before": True,
        "running": False,
        "own": True,  # Run in its own checkout: its tests.
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
    assert json.dumps(str(lib.resolve())) in projects.text_result_for_llm


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


async def test_create_pr_uses_the_branch_the_worker_is_on(tmp_path):
    """Bug bash: asked to commit on a branch the owner named, Create PR said nothing was
    committed (it counted the branch the worker was given)."""
    origin, app = published(tmp_path, "app")
    run("git", "config", "remote.origin.url", "https://github.com/acme/app.git", cwd=app)
    run("git", "config", f"url.{origin}.insteadOf", "https://github.com/acme/app.git", cwd=app)
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    calls = []

    async def gh(*arguments, payload=None):
        calls.append(arguments)
        if arguments[:2] == ("pr", "list"):
            return "[]"
        if arguments[:2] == ("pr", "create"):
            return "https://github.com/acme/app/pull/3\n"
        return json.dumps({"url": "https://github.com/acme/app/pull/3", "state": "OPEN"})

    runtime.github.gh = gh
    worker = await spawn(runtime, harness.router_id, title="Ideas", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree = Path(worker["cwd"])
    run("git", "checkout", "-q", "-b", "bb3-ideas", cwd=tree)
    commit(tree, "ideas.md", "ideas\n")
    await call_tool(runtime, "report", worker["session_id"], status="done", summary="Ideas.")
    await settle(runtime)
    await runtime.task_actions.create_pr(worker["task_id"])
    created = next(call for call in calls if call[:2] == ("pr", "create"))
    assert created[created.index("--head") + 1] == "bb3-ideas"


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
    assert "+++ b/notes.md\n@@ -0,0 +1 @@\n+draft" in found["diff"], "and new files"
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
    assert created[created.index("--title") + 1] == "add login.py", "what its commit says"
    body = created[created.index("--body") + 1]
    assert "Fixed the login." in body and "<!--" not in body, "nothing hidden in it (E37)"
    assert "credits" not in body and "**Goal**" not in body, "not the worker's instructions"
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
    assert runtime.tasks.get(task_id)["status"] == "review", "merged is not accepted (E28)"
    await call_tool(runtime, "report", worker_id, status="done", summary="Merged, all good.")
    assert runtime.tasks.get(task_id)["status"] == "review", "the answer is news on it"
    runtime.task_actions.accept(task_id)
    await call_tool(runtime, "report", worker_id, status="done", summary="Still all good.")
    assert runtime.tasks.get(task_id)["status"] == "done", "news does not reopen it"


async def test_editing_a_new_file_changes_the_version(tmp_path, git_repo):
    checkout = {"path": str(git_repo)}
    (git_repo / "new_test.py").write_text("assert True\n")
    (git_repo / "nested").mkdir()
    run("git", "init", "-q", cwd=git_repo / "nested")  # Listed as "nested/".
    try:
        (git_repo / "gone").symlink_to(git_repo / "missing")  # A broken link.
    except OSError as exc:
        if sys.platform != "win32" or exc.winerror != 1314:
            raise
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


def slow_reads(monkeypatch) -> dict[str, str]:
    """Reading a worker's worktree (git's snapshot, its changes) takes a while, and lsof sees
    the git doing it run in it. The worktree read now -> by which."""
    from chiefofstaff import machine
    from chiefofstaff import task_actions as task_actions_module

    reading: dict[str, str] = {}

    def slow(name, real):
        async def read(checkout, *args):
            tree = checkout.get("worktree") or checkout["path"]
            if not exists(tree):
                return await real(checkout, *args)  # Removed: read from its branch.
            reading[tree] = name
            try:
                await asyncio.sleep(0.05)
                assert exists(tree), f"removed while {name} read it"
                return await real(checkout, *args)
            finally:
                reading.pop(tree, None)

        return read

    for name in ("snapshot", "changes"):
        monkeypatch.setattr(
            task_actions_module, name, slow(name, getattr(task_actions_module, name))
        )

    async def cwds(limit=30):
        return set(reading)

    monkeypatch.setattr(machine, "process_cwds", cwds)
    return reading


async def until_read(reading: dict[str, str], by: str):
    for _ in range(400):
        if by in reading.values():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"its worktree was not read ({by})")


@pytest.mark.parametrize("closed_during", ["snapshot", "changes"])
async def test_a_worker_closed_as_its_result_is_read_has_its_worktree_removed_after(
    tmp_path, monkeypatch, closed_during
):
    """CI on 0.1.39: a worker closed right after it reported done had its worktree looked at for
    removal while its result was still read from it (record_end's snapshot, then its changes).
    The git reading it ran in it, so it stayed, as one a program runs in; or it went from under
    the read. Its removal waits for the read."""
    reading = slow_reads(monkeypatch)
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Fix", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    commit(Path(worker["cwd"]), "fix.py", "fixed = True\n")
    await call_tool(runtime, "report", worker["session_id"], status="done", summary="Fixed.")
    await until_read(reading, closed_during)
    await runtime.workers.close(worker["session_id"])
    await settle(runtime)
    assert not reading
    assert not exists(Path(worker["cwd"])), "its clean worktree was removed once read"
    task_id = task_of(harness, worker["session_id"])
    changed = runtime.task_actions.view(runtime.tasks.get(task_id))["receipts"]["changed"]
    assert changed["files"] == 1 and changed["commits"] == 1


async def test_a_worker_closed_after_its_turn_has_its_worktree_removed_after_it_is_read(
    tmp_path, monkeypatch
):
    """A worker closed as its turn ends (its PR merged): whether its finished task's files
    changed is read from its worktree then too (check_fresh). Its removal waits for that."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Fix", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    session_id = worker["session_id"]
    commit(Path(worker["cwd"]), "fix.py", "fixed = True\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Fixed.")
    await settle(runtime)
    runtime.task_actions.accept(task_of(harness, session_id))  # Nothing of it is open.
    reading = slow_reads(monkeypatch)
    runtime.states[session_id].close_after_turn = True
    harness.session(session_id).emit("session.idle")
    await until_read(reading, "snapshot")
    await settle(runtime)
    assert harness.store.session(session_id)["closed_at"]
    assert not reading
    assert not exists(Path(worker["cwd"])), "its clean worktree was removed once read"


async def restarted(runtime, task_id: str):
    """Until a finished task taken up again was checked for work done in between (as it is
    before its worker's first step)."""
    for _ in range(100):
        if runtime.store.get(f"restarted:{task_id}"):
            return
        await asyncio.sleep(0.03)
    raise AssertionError(f"{task_id} was not checked when taken up again")


def shell(harness, session_id: str, call: str, command: str, output: str = ""):
    """A worker's shell command, as Copilot reports it (run by the test itself)."""
    session = harness.session(session_id)
    session.emit("tool.execution_start", tool_name="bash", tool_call_id=call,
                 arguments={"command": command})  # fmt: skip
    done = SimpleNamespace(content=f"{output}\n<exited with exit code 0>")
    session.emit("tool.execution_complete", tool_call_id=call, success=True, result=done)


async def receipts_of(runtime, task_id, ready=lambda found: bool(found["version"])):
    for _ in range(100):
        found = runtime.task_actions.view(runtime.tasks.get(task_id))["receipts"]
        if found and ready(found):
            return found
        await asyncio.sleep(0.03)
    raise AssertionError(f"No receipts for {task_id}: {found}")


async def test_a_result_reported_while_its_start_is_recorded_still_gets_its_end(
    tmp_path, git_repo, monkeypatch
):
    """Its start (record_base) is recorded in the background; a report that comes before that
    is done waits for it, or its result would have no share (with slow git, as on Windows)."""
    harness = Harness(tmp_path / "data", Settings(projects=[str(git_repo)]))
    runtime = await harness.connect()
    actions = runtime.task_actions
    adopt = actions.adopt_clones
    calls = []

    async def slow_first(row):
        calls.append(row["id"])
        if len(calls) == 1:  # record_base's: still at it when the report comes.
            await asyncio.sleep(0.3)
        return await adopt(row)

    monkeypatch.setattr(actions, "adopt_clones", slow_first)
    info = await spawn(runtime, harness.router_id, title="Fix", instructions="fix",
                       project=str(git_repo), isolate=True)  # fmt: skip
    for _ in range(100):
        if info["task_id"] in actions.basing:
            break
        await asyncio.sleep(0.01)
    assert info["task_id"] in actions.basing, "its start is being recorded"
    commit(Path(info["cwd"]), "fix.py", "fixed = True\n")
    await call_tool(runtime, "report", info["session_id"], status="done", summary="Fixed.")
    await settle(runtime)
    ended = json.loads(harness.store.get(f"taskend:{info['task_id']}") or "{}")
    head = run("git", "rev-parse", "HEAD", cwd=info["cwd"]).strip()
    assert ended and ended[info["cwd"]]["commit"] == head


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

    # Another file changed (a later task's, say): its own code is as it was checked.
    (tree / "other.py").write_text("x = 1\n")
    worker.emit("session.idle")
    await settle(runtime)
    await asyncio.sleep(0.3)
    assert not (await receipts_of(runtime, info["task_id"]))["stale"], "not its own file"
    (tree / "orders.py").write_text("fixed = False\n")  # Edited after it was checked.
    worker.emit("session.idle")
    found = await receipts_of(runtime, info["task_id"], lambda found: bool(found["stale"]))
    assert found["stale"] == "Out of date: its files changed since it was checked (orders.py)."
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
                    task="new", why_new="test", title="Fix chunk", text="Fix chunk")  # fmt: skip
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
    first = runtime.tasks.for_session(session_id)[0]["id"]
    [found] = await runtime.task_actions.diff(first)
    assert "+doc = True" in found["diff"] and "chunk" not in found["diff"], (
        "the first task's result is what it delivered: not the later task's commit"
    )
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    task="new", title="Count tests", text="How many tests?",
                    why_new="test")  # fmt: skip
    question = task_of(harness, session_id)
    await settle(runtime)
    for _ in range(50):
        if runtime.store.get(f"taskbase:{question}"):
            break
        await asyncio.sleep(0.05)
    await call_tool(runtime, "report", session_id, status="done", summary="2 tests.")
    changed = (await receipts_of(runtime, question))["changed"]
    assert not (changed["commits"] or changed["files"] or changed["uncommitted"]), changed


async def test_in_the_owners_folder_a_task_has_what_it_changed_not_another_tasks(tmp_path):
    """Bug bash: in a folder of the owner's, a file another worker's edits wrote is that task's;
    a file this one changed with a shell command (`printf >> file`) is this task's; and of a
    file two tasks of one worker changed in turn, each has its own lines."""
    _, app = published(tmp_path, "app")
    (app / "kit.py").write_text("def base():\n    return 1\n")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()

    async def started(title):
        worker = await spawn(runtime, harness.router_id, title=title, instructions="x",
                             project=str(app))  # fmt: skip
        await settle(runtime)
        for _ in range(50):
            if runtime.store.get(f"taskbase:{task_of(harness, worker['session_id'])}"):
                break
            await asyncio.sleep(0.05)
        return worker["session_id"]

    mine, theirs = await started("Note"), await started("Their note")
    (app / "theirs.txt").write_text("another worker's file\n")
    harness.session(theirs).emit(
        "tool.execution_start", tool_call_id="t1", tool_name="create",
        arguments={"path": str(app / "theirs.txt"), "file_text": "x"},
    )  # fmt: skip
    with (app / "kit.py").open("a") as file:
        file.write("\n\ndef shout(text):\n    return text.upper()\n")
    shell(harness, mine, "s1", "printf 'def shout(text): ...' >> kit.py")  # It wrote that.
    await settle(runtime)
    await call_tool(runtime, "report", mine, status="done", summary="Added shout.")
    shout = task_of(harness, mine)
    changed = (await receipts_of(runtime, shout))["changed"]
    assert changed["files"] == 1 and changed["uncommitted"] == 1, changed
    [found] = await runtime.task_actions.diff(shout)
    assert "+def shout(text):" in found["diff"] and "theirs.txt" not in found["diff"]

    await call_tool(runtime, "message_session", harness.router_id, session_id=mine,
                    task="new", title="Add whisper", text="Add whisper",
                    why_new="test")  # fmt: skip
    whisper = task_of(harness, mine)
    await settle(runtime)
    for _ in range(50):
        if runtime.store.get(f"taskbase:{whisper}"):
            break
        await asyncio.sleep(0.05)
    with (app / "kit.py").open("a") as file:
        file.write("\n\ndef whisper(text):\n    return text.lower()\n")
    shell(harness, mine, "s2", "cat >> kit.py <<'EOF'\ndef whisper(text): ...\nEOF")
    await settle(runtime)
    (app / "__pycache__").mkdir(exist_ok=True)
    (app / "__pycache__" / "kit.cpython-313.pyc").write_bytes(b"\0compiled")  # By running it.
    await call_tool(runtime, "report", mine, status="done", summary="Added whisper.")
    for _ in range(50):
        if runtime.store.get(f"taskend:{whisper}"):
            break
        await asyncio.sleep(0.05)
    later = time.time() + 300  # Written again later (by other work): not when shout wrote it.
    os.utime(app / "kit.py", (later, later))
    [found] = await runtime.task_actions.diff(whisper)
    assert "+def whisper(text):" in found["diff"] and "+def shout" not in found["diff"]
    assert "__pycache__" not in found["diff"]
    [found] = await runtime.task_actions.diff(shout)
    assert "+def shout(text):" in found["diff"] and "whisper" not in found["diff"], (
        "its result as it delivered it, not with the later task's lines"
    )


async def test_in_the_owners_folder_what_the_owner_pulled_is_not_the_tasks_work(tmp_path):
    """Code review: in a folder of the owner's, files a `git pull` brought in while a worker
    worked there counted as that task's changes."""
    _, app = published(tmp_path, "app")
    run("git", "pull", "-q", "--ff-only", cwd=app)
    commit(app, "kit.py", "def base():\n    return 1\n")
    run("git", "push", "-q", "origin", "main", cwd=app)
    other = tmp_path / "app-other"
    run("git", "pull", "-q", "--ff-only", cwd=other)
    (other / "team.py").write_text("team = 1\n")
    run("git", "add", ".", cwd=other)
    old = {**os.environ, "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z",
           "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"}  # fmt: skip
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "team", cwd=other,
        env=old)  # fmt: skip
    run("git", "push", "-q", "origin", "main", cwd=other)
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Shout", instructions="x",
                         project=str(app))  # fmt: skip
    session_id = worker["session_id"]
    task = task_of(harness, session_id)
    await settle(runtime)
    for _ in range(50):
        if runtime.store.get(f"taskbase:{task}"):
            break
        await asyncio.sleep(0.05)
    run("git", "pull", "-q", "--ff-only", cwd=app)  # The owner pulls meanwhile.
    with (app / "kit.py").open("a") as file:
        file.write("\n\ndef shout(text):\n    return text.upper()\n")
    shell(harness, session_id, "s1", "echo 'def shout(text): ...' >> kit.py")
    await settle(runtime)
    await call_tool(runtime, "report", session_id, status="done", summary="Added shout.")
    changed = (await receipts_of(runtime, task))["changed"]
    assert changed["files"] == 1 and changed["uncommitted"] == 1, changed
    [found] = await runtime.task_actions.diff(task)
    assert "+def shout(text):" in found["diff"] and "team.py" not in found["diff"]


async def test_in_the_owners_folder_the_owners_edits_and_another_tasks_are_not_its_own(
    tmp_path,
):
    """Bug bash: in a folder of the owner's, what the owner edited while a worker ran a long
    command, and what another task's shell commands wrote at the same time, counted as its."""
    _, app = published(tmp_path, "app")
    commit(app, "money.py", "def money():\n    return 1\n")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()

    async def started(title):
        worker = await spawn(runtime, harness.router_id, title=title, instructions="x",
                             project=str(app))  # fmt: skip
        await settle(runtime)
        for _ in range(50):
            if runtime.store.get(f"taskbase:{task_of(harness, worker['session_id'])}"):
                break
            await asyncio.sleep(0.05)
        return worker["session_id"]

    x, y = await started("X marks"), await started("Y notes")
    # The owner edits while X sleeps; Y writes its notes and a line in money.py.
    (app / "OWNER_TODO.md").write_text("mine\n")
    (app / "y_notes.txt").write_text("y\n")
    shell(harness, y, "y1", "echo y > y_notes.txt && echo '# Y was here' >> money.py")
    with (app / "money.py").open("a") as file:
        file.write("# Y was here\n# X end\n")
    shell(harness, x, "x1", "sleep 75; echo '# X end' >> money.py")
    await settle(runtime)
    await call_tool(runtime, "report", x, status="done", summary="Marked.")
    await call_tool(runtime, "report", y, status="done", summary="Noted.")
    changed = (await receipts_of(runtime, task_of(harness, x)))["changed"]
    assert changed["files"] == 1 and changed["not_counted"] == 2, changed
    [found] = await runtime.task_actions.diff(task_of(harness, x))
    assert "OWNER_TODO.md" not in found["diff"] and "y_notes.txt" not in found["diff"]
    changed = (await receipts_of(runtime, task_of(harness, y)))["changed"]
    assert changed["files"] == 2, changed


async def test_a_question_in_chat_is_news_though_others_changed_the_folder(tmp_path):
    """Bug bash: asked about finished work in chat, the worker changed nothing; edits by the
    owner (or another task) in the folder made its answer a new result to review."""
    _, app = published(tmp_path, "app")
    commit(app, "money.py", "def money():\n    return 1\n")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Money", instructions="x",
                         project=str(app))  # fmt: skip
    session_id, task = worker["session_id"], worker["task_id"]
    await settle(runtime)
    with (app / "money.py").open("a") as file:
        file.write("# cents\n")
    shell(harness, session_id, "m1", "echo '# cents' >> money.py")
    await settle(runtime)
    await call_tool(runtime, "report", session_id, status="done", summary="Cents added.")
    await receipts_of(runtime, task)
    for _ in range(50):
        if runtime.store.get(f"taskend:{task}"):
            break
        await asyncio.sleep(0.05)
    await runtime.owner_send(session_id, "How many lines does money.py have? Change nothing.")
    (app / "AGENTS.md").write_text("the owner's own notes\n")  # The owner, meanwhile.
    shell(harness, session_id, "m2", "wc -l money.py", "3 money.py")
    await settle(runtime)
    await call_tool(runtime, "report", session_id, status="done", summary="3 lines.")
    shown = runtime.tasks.get(task)
    assert shown["result"] == "Cents added.", "an answer: news, not a new result"


async def test_a_change_in_chat_without_a_report_is_asked_to_be_reported(tmp_path):
    """Bug bash: asked in chat to change accepted work, the worker committed and only replied;
    the task stayed done with its old result."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Notes", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    (tree / "NOTES.md").write_text("notes\n")
    await call_tool(runtime, "report", session_id, status="done", summary="NOTES.md written.")
    await receipts_of(runtime, task)
    runtime.task_actions.accept(task)
    await runtime.owner_send(session_id, "Make it say 'words: 9' and commit")
    commit(tree, "NOTES.md", "words: 9\n")
    harness.session(session_id).emit("assistant.message", content="Done, committed.",
                                     tool_requests=None)  # fmt: skip
    harness.session(session_id).emit("session.idle")
    await settle(runtime)
    for _ in range(50):
        asked = [m for m in harness.store.messages(session_id)
                 if m["meta"].get("report_nudge") and m["meta"].get("task_id") == task]  # fmt: skip
        if asked:
            break
        await asyncio.sleep(0.05)
    assert asked and "new result" in asked[-1]["meta"]["prompt"]


async def test_a_split_gives_each_result_its_own_changes_and_undo_puts_them_back(tmp_path):
    """Bug bash: the task split off had no receipts (shown as a report, not to accept) and
    the task it came from kept the moved result's changes."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Currency", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    commit(tree, "currency.py", "DEFAULT = 'USD'\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Default currency.")
    await receipts_of(runtime, task)
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    text="Also add textkit/units.py", task_id=task)  # fmt: skip
    await restarted(runtime, task)
    commit(tree, "units.py", "UNITS = {}\n")
    shell(harness, session_id, "u1", "uv run pytest -q", "1 passed in 0.01s")
    await settle(runtime)
    await call_tool(runtime, "report", session_id, status="done", summary="Units added.")
    await receipts_of(runtime, task, lambda found: found["changed"]["commits"] == 2)
    made = runtime.task_actions.split(task, "Add the units module")

    def files(task_id):
        return runtime.task_actions.view(runtime.tasks.get(task_id))["receipts"]

    for _ in range(100):
        new, old = files(made["id"]), files(task)
        if new and new["changed"]["commits"] == 1 and old["changed"]["commits"] == 1:
            break
        await asyncio.sleep(0.03)
    assert new["changed"]["commits"] == 1 and new["changed"]["files"] == 1, new["changed"]
    assert new["tests"] and new["tests"]["passed"], "its test run moved with it"
    assert old["changed"]["commits"] == 1 and not old["tests"], old
    [diff] = await runtime.task_actions.diff(made["id"])
    assert "units.py" in diff["diff"] and "currency.py" not in diff["diff"]
    assert runtime.task_actions.needs_acceptance(runtime.tasks.get(made["id"]))
    runtime.task_actions.merge_back(made["id"])
    for _ in range(100):
        old = files(task)
        if old["changed"]["commits"] == 2:
            break
        await asyncio.sleep(0.03)
    assert old["changed"]["commits"] == 2 and old["tests"], old


async def test_a_follow_up_after_a_later_task_does_not_take_in_its_changes(tmp_path):
    """Bug bash: task A followed up after task B on the same branch showed B's changes."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Slugify", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, first = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    (tree / "slug.py").write_text("def slugify(): ...\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Slugify.")
    await receipts_of(runtime, first)
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    task="new", why_new="test", title="Truncate", text="Add truncate")  # fmt: skip
    second = task_of(harness, session_id)
    await settle(runtime)
    for _ in range(50):
        if runtime.store.get(f"taskbase:{second}"):
            break
        await asyncio.sleep(0.05)
    (tree / "trunc.py").write_text("def truncate(): ...\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Truncate.")
    await receipts_of(runtime, second)
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    text="Underscores become dashes too", task_id=first)  # fmt: skip
    await restarted(runtime, first)
    (tree / "slug.py").write_text("def slugify(): ...  # dashes\n")
    checked = runtime.store.value(
        "SELECT json_extract(evidence, '$.at') FROM runs WHERE task_id=? AND evidence!='' "
        "ORDER BY started_at DESC LIMIT 1",
        (first,),
    )
    await call_tool(runtime, "report", session_id, status="done", summary="Dashes.",
                    task_id=first)  # fmt: skip

    def rechecked(found):
        at = runtime.store.value(
            "SELECT json_extract(evidence, '$.at') FROM runs WHERE task_id=? AND "
            "evidence!='' ORDER BY started_at DESC LIMIT 1",
            (first,),
        )
        return at != checked

    found = await receipts_of(runtime, first, rechecked)
    assert found["changed"]["files"] == 1, found["changed"]
    [diff] = await runtime.task_actions.diff(first)
    assert "slug.py" in diff["diff"] and "trunc.py" not in diff["diff"]


async def test_a_follow_up_after_a_later_tasks_commit_on_the_same_file(tmp_path):
    """Live M4: task A followed up after task B committed on the same file (same branch): A's
    diff showed B's commit."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Currency", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, first = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    commit(tree, "kit.py", "DEFAULT_CURRENCY = 'USD'\n\n\ndef words(): ...\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Currency.")
    await receipts_of(runtime, first)
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    task="new", why_new="test", title="Truncate", text="Add truncate")  # fmt: skip
    second = task_of(harness, session_id)
    await settle(runtime)
    for _ in range(50):
        if runtime.store.get(f"taskbase:{second}"):
            break
        await asyncio.sleep(0.05)
    commit(
        tree, "kit.py", "DEFAULT_CURRENCY = 'USD'\n\n\ndef words(): ...\n\n\ndef truncate(): ...\n"
    )
    await call_tool(runtime, "report", session_id, status="done", summary="Truncate.")
    await receipts_of(runtime, second)
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    text="Rename it to BASE_CURRENCY", task_id=first)  # fmt: skip
    await restarted(runtime, first)
    commit(tree, "kit.py", "BASE_CURRENCY = 'USD'\n\n\ndef words(): ...\n\n\ndef truncate(): ...\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Renamed.",
                    task_id=first)  # fmt: skip
    await settle(runtime)
    for _ in range(100):
        found = runtime.task_actions.view(runtime.tasks.get(first))["receipts"]
        if found and found["changed"]["commits"] == 2:
            break
        await asyncio.sleep(0.03)
    assert found["changed"]["commits"] == 2, found["changed"]
    [diff] = await runtime.task_actions.diff(first)
    assert "BASE_CURRENCY" in diff["diff"] and "def truncate" not in diff["diff"], diff["diff"]


async def test_a_follow_ups_own_edits_stay_its_own_after_more_messages(tmp_path):
    """Code review: a second message on a follow-up (a nudge to report) recorded the
    follow-up's own edits as work done "in between", and they left its receipts."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Readme", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    (tree / "NOTES.md").write_text("notes\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Notes.")
    await receipts_of(runtime, task)
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    text="Add a README too", task_id=task)  # fmt: skip
    await restarted(runtime, task)
    (tree / "README.md").write_text("# readme\n")
    harness.session(session_id).emit("session.idle")  # Its turn ends without a report.
    await settle(runtime)
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    text="Report it now", task_id=task)  # fmt: skip
    await settle(runtime)
    await asyncio.sleep(0.2)
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    text="And report the README", task_id=task)  # fmt: skip
    await asyncio.sleep(0.2)
    await call_tool(runtime, "report", session_id, status="done", summary="README added.",
                    task_id=task)  # fmt: skip
    await settle(runtime)
    for _ in range(100):
        found = runtime.task_actions.view(runtime.tasks.get(task))["receipts"]
        if found and found["changed"]["files"] == 2:
            break
        await asyncio.sleep(0.03)
    assert found["changed"]["files"] == 2, found["changed"]


async def test_a_change_asked_in_chat_counts_after_a_second_chat_message(tmp_path):
    """Code review: a second chat message reset the base, so a change made for the first one
    was news on the old result, not a new result."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Notes", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    (tree / "NOTES.md").write_text("notes\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Notes written.")
    await receipts_of(runtime, task)
    for _ in range(50):
        if runtime.store.get(f"taskend:{task}"):
            break
        await asyncio.sleep(0.05)
    await runtime.owner_send(session_id, "Rename notes to memo inside")
    (tree / "NOTES.md").write_text("memo\n")
    await runtime.owner_send(session_id, "Thanks, and explain why")
    await call_tool(runtime, "report", session_id, status="done", summary="Renamed to memo.")
    shown = runtime.tasks.get(task)
    assert shown["status"] == "review" and shown["result"] == "Renamed to memo."


def test_a_command_names_a_file_by_its_path_or_its_name():
    scope = workspaces_module.Scope(
        commands="cat >> textkit/__init__.py <<'EOF'\nx\nEOF\n"
        "echo y>>./notes.txt; sed -i s/a/b/ kit.py"
    )
    top = Path("/w/app")
    assert scope.named(top, "textkit/__init__.py") and scope.named(top, "notes.txt")
    assert scope.named(top, "lib/kit.py"), "by its name"
    assert not scope.named(top, "money.py") and not scope.named(top, "it.py")
    runs = [workspaces_module.CommandRun(line) for line in (
        "for f in textkit/*.py; do echo x >> $f; done", 'echo hi > "release notes.txt"',
        "python3 fix.py", "sleep 50", "cat money.py | grep x", "./scripts/gen.sh out",
    )]  # fmt: skip
    assert [run.unnamed for run in runs] == [True, False, True, False, False, True]
    assert "release notes.txt" in runs[1].words and "money.py" in runs[4].words


async def test_in_the_owners_folder_glob_and_script_edits_count_and_reads_do_not(
    tmp_path, monkeypatch
):
    """Bug bash 4: edits made with a glob loop, a script or into a quoted name with spaces were
    "not counted"; a file the task only read (cat) and the owner edited while it slept was
    counted as its change."""
    # Times set by the test (os.utime): their write times (a real edit's ctime is its time).
    monkeypatch.setattr(
        workspaces_module,
        "written_at",
        lambda path: path.lstat().st_mtime if path.exists() else None,
    )
    _, app = published(tmp_path, "app")
    (app / "textkit").mkdir()
    for name in ("a.py", "b.py", "money.py"):
        (app / "textkit" / name).write_text(f"# {name}\n")
    run("git", "add", ".", cwd=app)
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "kit", cwd=app)
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Review stamps", instructions="x",
                         project=str(app))  # fmt: skip
    session_id, task = worker["session_id"], worker["task_id"]
    await settle(runtime)
    for _ in range(50):
        if runtime.store.get(f"taskbase:{task}"):
            break
        await asyncio.sleep(0.05)
    # Minutes ago: it read money.py, then slept; the owner edited money.py meanwhile.
    shell(harness, session_id, "r1", "cat textkit/money.py")
    shell(harness, session_id, "s1", "sleep 50")
    await settle(runtime)
    ago = lambda seconds: datetime.fromtimestamp(time.time() - seconds, UTC).isoformat()  # noqa: E731
    for call, start, end in (("r1", 200, 199), ("s1", 199, 140)):
        harness.store.execute(
            "UPDATE commands SET started_at=?, ended_at=? WHERE call_id=?",
            (ago(start), ago(end), call),
        )
    owners = app / "textkit" / "money.py"
    with owners.open("a") as file:
        file.write("# the owner's own line\n")
    os.utime(owners, (time.time() - 170, time.time() - 170))
    # Now: a script stamps the two files (naming neither); a name in quotes, with a space.
    for name in ("a.py", "b.py"):
        with (app / "textkit" / name).open("a") as file:
            file.write("# reviewed\n")
    shell(harness, session_id, "g1", "python3 stamp.py")
    (app / "release notes.txt").write_text("hello\n")
    shell(harness, session_id, "q1", 'echo hello > "release notes.txt"')
    await settle(runtime)
    await call_tool(runtime, "report", session_id, status="done", summary="Stamped.")
    changed = (await receipts_of(runtime, task))["changed"]
    [diff] = await runtime.task_actions.diff(task)
    assert "release notes.txt" in diff["diff"] and "textkit/a.py" in diff["diff"], diff["diff"]
    assert "the owner's own line" not in diff["diff"], "it only read money.py"
    assert changed["files"] == 3 and changed["not_counted"] == 1, changed


async def test_a_later_task_keeps_its_start_when_its_files_are_too_big_to_copy(
    tmp_path, monkeypatch
):
    """Code review / bug bash: with a large new file (no snapshot taken), a later task had no
    start and counted an earlier task's commits (and lines) as its own. Now the snapshot leaves
    the large file out and lists it: a task's own large new file is counted as one."""
    monkeypatch.setattr(workspaces_module, "MAX_SNAPSHOT_FILE", 10)
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="First", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id = Path(worker["cwd"]), worker["session_id"]
    commit(tree, "first.py", "first = True\n")
    (tree / "data.bin").write_bytes(b"0" * 100)  # Too big to copy (here).
    await call_tool(runtime, "report", session_id, status="done", summary="First.")
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    task="new", why_new="test", title="Second", text="Add second")  # fmt: skip
    second = task_of(harness, session_id)
    await settle(runtime)
    for _ in range(50):
        if runtime.store.get(f"taskbase:{second}"):
            break
        await asyncio.sleep(0.05)
    base = json.loads(runtime.store.get(f"taskbase:{second}"))
    assert base["commits"].get(str(tree)) and base["trees"].get(str(tree)), base
    assert base["large"][str(tree)] == ["data.bin"], base
    (tree / "second.py").write_text("second = True\n")
    run("git", "add", "second.py", cwd=tree)
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "second", cwd=tree)
    (tree / "more.bin").write_bytes(b"1" * 100)  # Its own large new file (not committed).
    await call_tool(runtime, "report", session_id, status="done", summary="Second.")
    [found] = await runtime.task_actions.diff(second)
    assert "second.py" in found["diff"] and "first.py" not in found["diff"]
    assert "data.bin" not in found["diff"]
    assert found["large"] == [{"name": "more.bin", "size": 100}], "said, though not shown"
    changed = (await receipts_of(runtime, second))["changed"]
    assert changed["large"] == 1 and changed["files"] == 2 and changed["commits"] == 1, changed


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
                    task="new", title="Feature", text="Add the feature",
                    why_new="test")  # fmt: skip
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


async def test_a_change_asked_for_in_chat_is_a_new_result_not_news(tmp_path):
    """Bug bash: asked in chat to change a finished result, the worker edited the file; the
    change came as news on the old result, which still showed its old diff."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Notes", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id = Path(worker["cwd"]), worker["session_id"]
    (tree / "NOTES.md").write_text("notes\n")
    await call_tool(runtime, "report", session_id, status="done", summary="NOTES.md written.")
    task = task_of(harness, session_id)
    await receipts_of(runtime, task)
    for _ in range(50):
        if runtime.store.get(f"taskend:{task}"):
            break
        await asyncio.sleep(0.05)
    await runtime.owner_send(session_id, "What does it say?")
    # Running it to answer leaves files behind: not a change.
    (tree / "__pycache__").mkdir()
    (tree / "__pycache__" / "notes.cpython-313.pyc").write_bytes(b"\0compiled")
    (tree / ".coverage").write_bytes(b"coverage")
    await call_tool(runtime, "report", session_id, status="done", summary="It says notes.")
    assert runtime.tasks.get(task)["result"] == "NOTES.md written.", "a question: news"
    await runtime.owner_send(session_id, "Add a line more")
    (tree / "NOTES.md").write_text("notes\nmore\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Added the line more.")
    shown = runtime.tasks.get(task)
    assert shown["status"] == "review" and shown["result"] == "Added the line more."


async def test_a_tasks_start_commit_amended_away_does_not_count_earlier_commits(tmp_path):
    """Bug bash 4: after its start commit was amended, a task counted every commit on its
    branch (an earlier task's too)."""
    _, app = published(tmp_path, "app")
    run("git", "pull", "-q", "--ff-only", cwd=app)
    run("git", "checkout", "-q", "-b", "chief/work", cwd=app)
    commit(app, "slug.py", "slug = 1\n")  # An earlier task's.
    commit(app, "chunk.py", "chunk = 1\n")  # Where this task started.
    start = run("git", "rev-parse", "HEAD", cwd=app).strip()
    (app / "chunk.py").write_text("chunk = 2\n")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qa", "--amend",
        "--no-edit", cwd=app)  # fmt: skip
    commit(app, "more.py", "more = 1\n")
    checkout = {"path": str(app), "branch": "chief/work", "base_ref": "origin/main"}
    found = await workspaces_module.changes(checkout, workspaces_module.Scope(start))
    assert found and found["commits"] == 2, found


@pytest.mark.skipif(sys.platform == "win32", reason="Windows forbids quotes in file names")
async def test_files_named_with_quotes_and_backslashes_are_in_a_tasks_changes(tmp_path):
    """Bug bash 4: git quotes a name with `"` even with core.quotePath off; such files were
    left out of a task's changes ("+0 −0", an empty diff)."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Notes file", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    (tree / "docs").mkdir()
    (tree / "docs" / 'release notes "v2" \u8aaa\u660e.md').write_text("v2\nnotes\n")
    commit(tree, 'notes "x" \u00fc.txt', "x\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Notes.")
    changed = (await receipts_of(runtime, task))["changed"]
    assert changed["files"] == 2 and changed["insertions"] == 3, changed
    [diff] = await runtime.task_actions.diff(task)
    assert "+v2" in diff["diff"] and "+x" in diff["diff"], diff["diff"]


async def test_a_worktree_removed_under_a_worker_is_back_before_its_next_message(tmp_path):
    """Bug bash 4: its worktree deleted, the worker said "recreate the worktree" to every
    message; only Close and Reopen fixed it."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Sleepy", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree = Path(worker["cwd"])
    shutil.rmtree(tree)
    await runtime.owner_send(worker["session_id"], "Try again")
    assert (tree / "README.md").exists(), "restored on its branch"


async def test_a_follow_up_after_a_split_and_another_task_leaves_out_that_tasks_work(tmp_path):
    """Live M4: after a split, a later task's commit showed in a follow-up on the task it was
    split from (its "checked for work in between" mark was from before the split)."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Currency", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    commit(tree, "kit.py", "DEFAULT = 'USD'\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Currency.")
    await receipts_of(runtime, task)
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    text="Also add units.py", task_id=task)  # fmt: skip
    await restarted(runtime, task)
    commit(tree, "units.py", "UNITS = {}\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Units.")
    await receipts_of(runtime, task, lambda found: found["changed"]["commits"] == 2)
    runtime.task_actions.split(task, "Add the units module")
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    task="new", why_new="test", title="Truncate", text="Add truncate")  # fmt: skip
    other = task_of(harness, session_id)
    await settle(runtime)
    for _ in range(50):
        if runtime.store.get(f"taskbase:{other}"):
            break
        await asyncio.sleep(0.05)
    commit(tree, "trunc.py", "def truncate(): ...\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Truncate.")
    await receipts_of(runtime, other)
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    text="Rename DEFAULT to BASE", task_id=task)  # fmt: skip
    await restarted(runtime, task)
    for _ in range(50):
        if runtime.store.get(f"taskgaps:{task}"):
            break
        await asyncio.sleep(0.05)
    commit(tree, "kit.py", "BASE = 'USD'\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Renamed.",
                    task_id=task)  # fmt: skip
    await settle(runtime)
    [diff] = await runtime.task_actions.diff(task)
    assert "BASE" in diff["diff"] and "truncate" not in diff["diff"], diff["diff"]


def dated(repo: Path, name: str, text: str, at: int, message: str = ""):
    """A commit made at a given time (author and committer)."""
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text)
    run("git", "add", "-A", cwd=repo)
    stamp = f"@{at} +0000"
    environment = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm",
        message or f"add {name}", cwd=repo, env=environment)  # fmt: skip


async def test_a_task_on_a_branch_made_from_elsewhere_counts_only_its_own_commits(tmp_path):
    """Bug bash 5 (L2): one worker, tasks on separate branches: a task that made a branch from
    main after the previous task's branch counted that task's lines as removed; one that
    branched from the previous task's branch counted its commits too."""
    _, app = published(tmp_path, "app")
    run("git", "pull", "-q", "--ff-only", cwd=app)
    t0 = int(time.time()) - 1000
    run("git", "checkout", "-q", "-b", "chief/slug", cwd=app)
    dated(app, "kit.py", "def slug():\n    return 1\n" * 8, t0 + 10)  # The first task's.
    slug_head = run("git", "rev-parse", "HEAD", cwd=app).strip()
    tree = run("git", "rev-parse", "HEAD^{tree}", cwd=app).strip()
    # The second task, taken up on chief/slug, makes its branch from main.
    run("git", "checkout", "-q", "-b", "chief/camel", "main", cwd=app)
    dated(app, "kit.py", "def camel():\n    return 2\n", t0 + 200)
    checkout = {"path": str(app), "branch": "chief/camel", "base_ref": "origin/main"}
    scope = workspaces_module.Scope(slug_head, t0 + 100, start=tree, head=slug_head)
    found = await workspaces_module.changes(checkout, scope)
    assert found and found["commits"] == 1, found
    assert (found["insertions"], found["deletions"]) == (2, 0), found
    # A third, taken up on chief/camel, branches from chief/slug: not slug's commit.
    camel_head = run("git", "rev-parse", "HEAD", cwd=app).strip()
    camel_tree = run("git", "rev-parse", "HEAD^{tree}", cwd=app).strip()
    run("git", "checkout", "-q", "-b", "chief/bench", "chief/slug", cwd=app)
    dated(app, "bench.py", "import kit\n" * 5, t0 + 400)
    checkout["branch"] = "chief/bench"
    scope = workspaces_module.Scope(camel_head, t0 + 300, start=camel_tree, head=camel_head)
    found = await workspaces_module.changes(checkout, scope)
    assert found and found["commits"] == 1 and found["files"] == 1, found
    assert (found["insertions"], found["deletions"]) == (5, 0), found


async def test_rebased_onto_a_newer_main_a_task_does_not_count_its_commits(tmp_path):
    """Bug bash 5 (S3): rebased onto the latest origin/main, a task's changes took in what was
    merged there meanwhile (another PR's files)."""
    _, app = published(tmp_path, "app")
    run("git", "pull", "-q", "--ff-only", cwd=app)
    base = run("git", "rev-parse", "HEAD", cwd=app).strip()
    run("git", "checkout", "-q", "-b", "chief/notes", cwd=app)
    commit(app, "notes.md", "notes\n")
    # Another PR merged into main meanwhile.
    other = tmp_path / "app-other"
    run("git", "pull", "-q", "--ff-only", cwd=other)
    commit(other, "calc.py", "def pct(): ...\n")
    commit(other, "test_calc.py", "def test_pct(): ...\n")
    run("git", "push", "-q", "origin", "main", cwd=other)
    run("git", "fetch", "-q", "origin", cwd=app)
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "rebase", "-q", "origin/main",
        cwd=app)  # fmt: skip
    checkout = {"path": str(app), "branch": "chief/notes", "base_ref": base}
    found = await workspaces_module.changes(checkout, workspaces_module.Scope())
    assert found and found["commits"] == 1 and found["files"] == 1, found
    # A later task whose start commit was rebased with it: from that commit as it is now.
    start = run("git", "rev-parse", "HEAD", cwd=app).strip()
    run("git", "checkout", "-q", "-b", "chief/older", base, cwd=app)
    dated(app, "notes2.md", "older\n", int(time.time()) - 50, "older start")
    older = run("git", "rev-parse", "HEAD", cwd=app).strip()
    commit(app, "mine.md", "mine\n")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "rebase", "-q", start, cwd=app)
    checkout = {"path": str(app), "branch": "chief/older", "base_ref": base}
    found = await workspaces_module.changes(
        checkout, workspaces_module.Scope(older, time.time() - 40)
    )
    assert found and found["commits"] == 1 and found["files"] == 1, found


async def test_generated_files_a_task_committed_are_in_its_changes(tmp_path):
    """Bug bash 5 (S7): a commit of `.pyc` files and a uv.lock showed as "2 files": a PR would
    carry all 5."""
    _, app = published(tmp_path, "app")
    run("git", "pull", "-q", "--ff-only", cwd=app)
    run("git", "checkout", "-q", "-b", "chief/kit", cwd=app)
    start = run("git", "rev-parse", "HEAD", cwd=app).strip()
    tree = run("git", "rev-parse", "HEAD^{tree}", cwd=app).strip()
    (app / "__pycache__").mkdir()
    (app / "__pycache__" / "kit.cpython-313.pyc").write_bytes(b"\0compiled")
    (app / "uv.lock").write_text("version = 1\n")
    commit(app, "kit.py", "kit = 1\n")
    (app / "__pycache__" / "new.cpython-313.pyc").write_bytes(b"\0not committed")
    checkout = {"path": str(app), "branch": "chief/kit", "base_ref": "origin/main"}
    found = await workspaces_module.changes(
        checkout, workspaces_module.Scope(start, time.time() - 60, start=tree, head=start)
    )
    assert found and found["files"] == 3 and found["commits"] == 1, found


def test_a_file_stays_a_tasks_own_when_other_work_writes_it_later():
    """Bug bash 5 (S1, S2): in a folder of the owner's, the task's own file left its changes
    once the owner wrote to it later; a script running as another task's command wrote a file
    that command names took that file in."""
    now = time.time()
    top = Path("/nowhere")
    loop = workspaces_module.CommandRun("for f in tests/*.py; do echo x >> $f; done", now - 100,
                                        now - 99)  # fmt: skip
    nap = workspaces_module.CommandRun("sleep 60", now - 98, now - 38)
    scope = workspaces_module.Scope(commands=[loop, nap], mtimes={"tests/t.py": now - 50})
    assert not scope.named(top, "tests/t.py"), "written last by the owner, as it slept"
    scope.earlier = {"tests/t.py": [now - 99.5]}
    assert scope.named(top, "tests/t.py"), "its own at its earlier result"
    # Two tasks at once: its script ran as another task's command wrote test_shout.py.
    script = workspaces_module.CommandRun("python3 -c 'stamp()'", now - 30, now - 29.9)
    shout = "printf 'x' > tests/test_shout.py && uv run pytest -q"
    theirs = workspaces_module.CommandRun(shout, now - 30, now - 29)
    scope = workspaces_module.Scope(commands=[script], mtimes={"tests/test_shout.py": now - 29.5})
    assert scope.named(top, "tests/test_shout.py"), "nothing else names it"
    scope.other_runs = [theirs]
    assert not scope.named(top, "tests/test_shout.py"), "the other task's command names it"
    # Seconds apart are apart: a file written 1.4 s before a command started is not its.
    scope = workspaces_module.Scope(commands=[theirs], mtimes={"CHANGES.md": now - 31.4})
    assert not scope.named(top, "CHANGES.md")


async def test_in_the_owners_folder_a_tasks_commit_counts_and_another_worker_dates_it(tmp_path):
    """Bug bash 5 (S14, S15): in a folder of the owner's, a task's commit counted as 0 commits;
    and a finished task there never became out of date when another worker changed its file."""
    _, app = published(tmp_path, "app")
    (app / "kit.py").write_text("def base():\n    return 1\n")
    run("git", "add", ".", cwd=app)
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "kit", cwd=app)
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()

    async def started(title):
        worker = await spawn(runtime, harness.router_id, title=title, instructions="x",
                             project=str(app))  # fmt: skip
        await settle(runtime)
        for _ in range(50):
            if runtime.store.get(f"taskbase:{task_of(harness, worker['session_id'])}"):
                break
            await asyncio.sleep(0.05)
        return worker["session_id"]

    mine = await started("Add shout")
    with (app / "kit.py").open("a") as file:
        file.write("\n\ndef shout(text):\n    return text.upper()\n")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "shout", cwd=app)
    shell(harness, mine, "c1", "printf 'def shout' >> kit.py && git commit -qam shout")
    await settle(runtime)
    await call_tool(runtime, "report", mine, status="done", summary="Added shout, committed.")
    shout = task_of(harness, mine)
    changed = (await receipts_of(runtime, shout))["changed"]
    assert changed["commits"] == 1 and changed["files"] == 1, changed
    # Another worker in the same folder changes that file: its result is out of date.
    theirs = await started("Add whisper")
    with (app / "kit.py").open("a") as file:
        file.write("\n\ndef whisper(text):\n    return text.lower()\n")
    shell(harness, theirs, "w1", "printf 'def whisper' >> kit.py")
    await settle(runtime)
    await runtime.task_actions.check_fresh(theirs)
    found = await receipts_of(runtime, shout, lambda found: bool(found["stale"]))
    assert found["stale"] == "Out of date: its files changed since it was checked (kit.py)."


async def test_a_file_a_task_moved_counts_once(tmp_path):
    """Bug bash 5 (S10): a rename counted as 2 files (git and GitHub count 1)."""
    _, app = published(tmp_path, "app")
    run("git", "pull", "-q", "--ff-only", cwd=app)
    commit(app, "notes.md", "".join(f"line {n}\n" for n in range(40)))
    run("git", "checkout", "-q", "-b", "chief/move", cwd=app)
    start = run("git", "rev-parse", "HEAD", cwd=app).strip()
    tree = run("git", "rev-parse", "HEAD^{tree}", cwd=app).strip()
    (app / "docs").mkdir()
    run("git", "mv", "notes.md", "docs/notes.md", cwd=app)
    commit(app, "docs/index.md", "# Docs\n")
    checkout = {"path": str(app), "branch": "chief/move", "base_ref": "origin/main"}
    found = await workspaces_module.changes(
        checkout, workspaces_module.Scope(start, time.time() - 60, start=tree, head=start)
    )
    assert found and found["files"] == 2, found


async def test_work_in_a_clone_the_worker_made_itself_is_its_tasks_change(tmp_path):
    """Bug bash 5 (L11): a worker that cloned a repository in its shell and pushed a fix there
    showed a "Report" with no files: that clone was no checkout of its."""
    origin, _ = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings())
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Fix the docs", instructions="x")
    session_id, task = worker["session_id"], worker["task_id"]
    await settle(runtime)
    for _ in range(50):
        if runtime.store.get(f"taskbase:{task}"):
            break
        await asyncio.sleep(0.05)
    folder = runtime.workspaces.workspace(harness.store.session(session_id))
    folder.mkdir(parents=True, exist_ok=True)
    run("git", "clone", "-q", str(origin), str(folder / "app"))
    run("git", "checkout", "-q", "-b", "fix-docs", cwd=folder / "app")
    commit(folder / "app", "docs.md", "fixed\n")
    shell(harness, session_id, "c1", f"git clone {origin} app && cd app && git commit -am fix")
    await settle(runtime)
    await call_tool(runtime, "report", session_id, status="done", summary="Fixed the docs.")
    changed = (await receipts_of(runtime, task))["changed"]
    assert changed["commits"] == 1 and changed["files"] == 1, changed
    [found] = await runtime.task_actions.diff(task)
    assert "+fixed" in found["diff"]


async def test_finished_work_is_checked_once_at_a_time_and_closed_workers_are_left_out(tmp_path):
    """Code review (round 5): every turn end in a folder of the owner's re-checked every worker
    that ever used it, closed ones too, and checks piled up."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    actions = runtime.task_actions
    first = await spawn(runtime, harness.router_id, title="One", instructions="x", project=str(app))
    second = await spawn(runtime, harness.router_id, title="Two", instructions="x",
                         project=str(app))  # fmt: skip
    await settle(runtime)
    assert set(actions.sharing(str(app))) == {first["session_id"], second["session_id"]}
    await runtime.workers.close(second["session_id"])
    assert actions.sharing(str(app)) == [first["session_id"]], "not a closed worker"
    assert second["session_id"] in actions.sharing(str(app), closed=True)
    calls = []

    async def slow(row):
        calls.append(row["id"])
        await asyncio.sleep(0.05)

    actions.check_own = slow
    await asyncio.gather(*(actions.check_fresh(first["session_id"]) for _ in range(5)))
    assert calls == [first["session_id"]] * 2, "one at a time, once more for what came after"


async def test_rebased_onto_the_owners_newer_main_without_an_origin(tmp_path):
    """Bug bash 6 (G3): a worktree of a repository with no origin, rebased onto the owner's
    newer main, counted main's new commit as the task's."""
    repo = tmp_path / "kit"
    repo.mkdir()
    run("git", "init", "-q", "-b", "main", cwd=repo)
    commit(repo, "kit.py", "kit = 1\n")
    start = run("git", "rev-parse", "HEAD", cwd=repo).strip()
    tree = tmp_path / "work"
    run("git", "worktree", "add", "-q", "-b", "chief/notes", str(tree), start, cwd=repo)
    commit(tree, "notes.py", "notes = 1\n")
    commit(tree, "more.py", "more = 1\n")
    commit(repo, "NOTES.md", "the owner's\n")  # The owner's commit on main.
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "rebase", "-q", "main", cwd=tree)
    checkout = {"path": str(tree), "repo": str(repo), "worktree": str(tree),
                "branch": "chief/notes", "base_ref": start}  # fmt: skip
    found = await workspaces_module.changes(checkout, workspaces_module.Scope())
    assert found and found["commits"] == 2 and found["files"] == 2, found
    # Its start recorded at main's commit then (a task taken up there), still in the history.
    first_tree = run("git", "rev-parse", f"{start}^{{tree}}", cwd=repo).strip()
    scope = workspaces_module.Scope(start, time.time() - 60, start=first_tree, head=start)
    found = await workspaces_module.changes(checkout, scope)
    assert found and found["commits"] == 2 and found["files"] == 2, found


async def test_a_follow_up_counts_only_its_own_edit_of_a_file_another_task_made(tmp_path):
    """Bug bash 6 (G4): in a folder of the owner's, a follow-up that changed a file another
    task had made (and the owner had added to) counted that whole file as its new file."""
    _, app = published(tmp_path, "app")
    run("git", "pull", "-q", "--ff-only", cwd=app)
    checkout = {"path": str(app), "branch": "", "shared": True}
    head = run("git", "rev-parse", "HEAD", cwd=app).strip()
    start = await workspaces_module.snapshot(checkout)
    (app / "beta.py").write_text("def beta():\n    return 2\n")
    first = await workspaces_module.snapshot(checkout)  # Its result.
    # Between: another task makes alpha.py; the owner adds a line to it.
    (app / "alpha.py").write_text('def alpha():\n    """Alpha."""\n    return 1\n# owner note\n')
    again = await workspaces_module.snapshot(checkout)  # Taken up again.
    (app / "alpha.py").write_text(
        'def alpha():\n    """Return one."""\n    return 1\n# owner note\n'
    )
    end = await workspaces_module.snapshot(checkout)
    assert start and first and again and end
    touched = {str((app / name).resolve()) for name in ("beta.py", "alpha.py")}
    scope = workspaces_module.Scope(
        "", time.time() - 60, touched, start=start["tree"], end=end, head=head,
        gaps=[(first["tree"], again["tree"], first["commit"], again["commit"], time.time())],
    )  # fmt: skip
    found = await workspaces_module.changes(checkout, scope)
    assert found and found["files"] == 2, found
    assert (found["insertions"], found["deletions"]) == (3, 1), found
    shown = (await workspaces_module.diff(checkout, scope))["diff"]
    assert '+    """Return one."""' in shown and "+# owner note" not in shown, shown


async def test_a_result_is_not_out_of_date_because_its_worker_moved_to_another_branch(tmp_path):
    """Bug bash 5 (L2), left over: a worker's next task on a branch made from main marked the
    earlier result "Out of date: its files changed", though its commit is untouched."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Slugify", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, first = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    for _ in range(50):
        if runtime.store.get(f"taskbase:{first}"):
            break
        await asyncio.sleep(0.05)
    commit(tree, "slug.py", "def slugify(): ...\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Slugify.")
    await receipts_of(runtime, first)
    run("git", "checkout", "-q", "-b", "chief/camel", "origin/main", cwd=tree)
    commit(tree, "camel.py", "def camel(): ...\n")
    await runtime.task_actions.check_fresh(session_id)
    assert not (await receipts_of(runtime, first))["stale"]
    # Its own branch moved (its file changed there): out of date.
    run("git", "checkout", "-q", worker["branch"], cwd=tree)
    commit(tree, "slug.py", "def slugify(): return 1\n")
    await runtime.task_actions.check_fresh(session_id)
    found = await receipts_of(runtime, first, lambda found: bool(found["stale"]))
    assert "slug.py" in found["stale"]


async def test_a_file_it_committed_then_changed_after_others_counts_once(tmp_path):
    """Code review (round 6): a file a task committed, that other work changed before its
    follow-up changed it again, was counted (and shown) twice."""
    _, app = published(tmp_path, "app")
    run("git", "pull", "-q", "--ff-only", cwd=app)
    (app / "kit.py").write_text("a = 1\n")
    run("git", "add", ".", cwd=app)
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "kit", cwd=app)
    checkout = {"path": str(app), "branch": "", "shared": True}
    head = run("git", "rev-parse", "HEAD", cwd=app).strip()
    start = await workspaces_module.snapshot(checkout)
    (app / "kit.py").write_text("a = 1\nb = 2\nc = 3\n")  # Its own: two lines, committed.
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "b c", cwd=app)
    first = await workspaces_module.snapshot(checkout)
    with (app / "kit.py").open("a") as file:
        file.write("# someone else's\n")
    again = await workspaces_module.snapshot(checkout)
    with (app / "kit.py").open("a") as file:
        file.write("d = 4\n")  # Its follow-up: one more line, not committed.
    end = await workspaces_module.snapshot(checkout)
    assert start and first and again and end
    scope = workspaces_module.Scope(
        head, time.time() - 60, {str((app / "kit.py").resolve())}, start=start["tree"],
        end=end, head=head,
        gaps=[(first["tree"], again["tree"], first["commit"], again["commit"], time.time())],
    )  # fmt: skip
    found = await workspaces_module.changes(checkout, scope)
    assert found and found["files"] == 1, found
    assert (found["insertions"], found["deletions"]) == (3, 0), found
    shown = (await workspaces_module.diff(checkout, scope))["diff"]
    assert shown.count("+b = 2") == 1 and "+d = 4" in shown and "+# someone else" not in shown


async def test_a_task_built_on_another_tasks_branch_counts_only_its_own_commits(tmp_path):
    """Bug bash 7 (D2): a task whose worker fast-forwarded onto another task's branch counted
    that task's commits as its own ("+66 −0 in 3 files" for a 13-line README section) and was
    out of date when that task's files changed."""
    _, app = published(tmp_path, "textkit")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    first = await spawn(runtime, harness.router_id, title="Truncate", instructions="x",
                        project=str(app), isolate=True)  # fmt: skip
    tree = Path(first["cwd"])
    (tree / "textkit").mkdir()
    (tree / "tests").mkdir()
    (tree / "textkit" / "__init__.py").write_text("def truncate(text, n):\n    return text[:n]\n")
    (tree / "tests" / "test_truncate.py").write_text("def test_truncate(): ...\n")
    (tree / "README.md").write_text("# textkit\n\nHas truncate.\n")
    run("git", "add", "-A", cwd=tree)
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "truncate",
        cwd=tree)  # fmt: skip
    await call_tool(runtime, "report", first["session_id"], status="done", summary="Truncate.")
    await receipts_of(runtime, first["task_id"])
    await asyncio.sleep(1.1)  # Its commit is from before the next task was taken up.
    worker = await spawn(runtime, harness.router_id, title="README section", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    for _ in range(50):
        if runtime.store.get(f"taskbase:{task}"):
            break
        await asyncio.sleep(0.05)
    run("git", "merge", "-q", "--ff-only", first["branch"], cwd=tree)
    with (tree / "README.md").open("a") as file:
        file.write("\n## Truncate\n\n`truncate(text, n)` keeps the first n characters.\n")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "README",
        cwd=tree)  # fmt: skip
    await call_tool(runtime, "report", session_id, status="done", summary="README section.")
    changed = (await receipts_of(runtime, task))["changed"]
    assert changed["commits"] == 1 and changed["files"] == 1, changed
    assert (changed["insertions"], changed["deletions"]) == (4, 0), changed
    [found] = await runtime.task_actions.diff(task)
    assert "+## Truncate" in found["diff"], found["diff"]
    assert "+Has truncate" not in found["diff"] and "test_truncate" not in found["diff"]
    # The other task's files change in its checkout: not its own.
    (tree / "tests" / "test_truncate.py").write_text("def test_truncate(): assert True\n")
    await runtime.task_actions.check_fresh(session_id)
    assert not (await receipts_of(runtime, task))["stale"]
    (tree / "README.md").write_text("# textkit\n")
    await runtime.task_actions.check_fresh(session_id)
    found = await receipts_of(runtime, task, lambda found: bool(found["stale"]))
    assert found["stale"] == "Out of date: its files changed since it was checked (README.md)."
