"""The agent picker (the owner's decisions of 2026-11-14): only where another coding agent is
installed does anything of agents show; a first-run card for one installed and signed in; @codex
in the composer; who chooses the agent; a worker's work continued on another agent; a named
agent that cannot run, offered alternatives on a card (never moved by itself); a plan's limit
pausing its workers; how much of each plan's limits is used. Codex and Claude are the stand-ins
(tests/fake_codex.py, tests/fake_claude.py) on the PATH."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from conftest import Harness, settle
from test_agents import ran, router_options, saved, schema, spawn_on

from chiefofstaff import agents, backend_claude, backend_codex, prompts, task_actions
from chiefofstaff.backend_codex import CodexAgent
from chiefofstaff.roles import WORKER


async def until(check, seconds: float = 20):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.05)


async def started(tmp_path, settings=None):
    harness = Harness(tmp_path / "data", settings)
    return harness, await harness.connect()


async def test_with_copilot_alone_nothing_of_agents_is_in_the_page(tmp_path):
    harness, runtime = await started(tmp_path)
    try:
        await runtime.offer_agents()
        state = runtime.snapshot()
        assert not {
            "agents",
            "agents_found",
            "agents_status",
            "agent_offers",
            "agent_refusals",
        } & set(state)
        assert agents.stored(harness.store) == {}
    finally:
        await runtime.stop()


async def test_an_agent_installed_and_signed_in_is_offered_once_and_allowed(
    tmp_path, codex_installed
):
    _, runs = codex_installed
    harness, runtime = await started(tmp_path)
    try:
        await until(lambda: runtime.snapshot().get("agent_offers"))
        state = runtime.snapshot()
        assert state["agents_found"] == ["codex"]
        (offer,) = state["agent_offers"]
        assert (offer["name"], offer["label"]) == ("codex", "Codex")
        assert offer["state"] == "0.167.2 · signed in with ChatGPT"
        assert ran(runs) == ["--version", "login status"], "a glance: its version, its sign-in"
        await runtime.answer_offer("codex", True)
        assert agents.turned_on(harness.store) == ["codex"]
        assert agents.new_workers(harness.store) == "copilot", "new workers stay on Copilot"
        assert "agent_offers" not in runtime.snapshot()
        runtime.offers.clear()
        await runtime.offer_agents()
        assert not runtime.offers, "asked once"
    finally:
        await runtime.stop()


async def test_an_agent_turned_on_in_settings_is_never_offered_again(tmp_path, codex_installed):
    harness, runtime = await started(tmp_path)
    try:
        await until(lambda: runtime.snapshot().get("agent_offers"))
        await saved(runtime, {"codex": {"enabled": True}})
        assert "agent_offers" not in runtime.snapshot(), "decided on there"
        await saved(runtime, {"codex": {"enabled": False}})
        await runtime.offer_agents()
        assert "agent_offers" not in runtime.snapshot(), "nor once it is off again"
        assert harness.store.get(f"{agents.OFFERED}codex") == "settings"
    finally:
        await runtime.stop()


async def test_not_now_leaves_it_off_and_it_is_not_offered_again(tmp_path, codex_installed):
    _, runs = codex_installed
    harness, runtime = await started(tmp_path)
    try:
        await until(lambda: runtime.snapshot().get("agent_offers"))
        await runtime.answer_offer("codex", False)
        assert agents.turned_on(harness.store) == []
        assert agents.stored(harness.store) == {}, "nothing of agents stored"
        await runtime.stop()
        runtime = await harness.connect()
        await asyncio.sleep(0.3)
        assert "agent_offers" not in runtime.snapshot() and len(ran(runs)) == 2, (
            "never looked at again for its card"
        )
    finally:
        await runtime.stop()


@pytest.mark.parametrize("configured", [False, True], ids=["home", "CODEX_HOME"])
async def test_a_codex_never_set_up_is_not_offered_and_nothing_of_it_runs_or_is_made(
    tmp_path, codex_installed, monkeypatch, configured
):
    # Any run of Codex 0.167.2 makes its folder (~/.codex, its databases) where it was never set
    # up: it is not run then, and Settings says why.
    _, runs = codex_installed
    home = tmp_path / "home"
    (home / ".codex").rmdir()
    if configured:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    _, runtime = await started(tmp_path)
    try:
        await runtime.offer_agents()
        assert "agent_offers" not in runtime.snapshot()
        (entry,) = (await runtime.look_for_agents())["agents"]
        assert entry["problem"] == "Not set up: sign in to Codex from Settings → Agents & models."
        with pytest.raises(RuntimeError, match=r"^Not set up: sign in to Codex"):
            await CodexAgent("codex").start()
        assert not runs.exists(), "nothing of Codex ran"
        assert list(home.iterdir()) == [] and not (tmp_path / "codex-home").exists()
    finally:
        await runtime.stop()


async def test_settings_look_runs_codex_without_its_plugin_sync(tmp_path, codex_installed):
    # Each app-server syncs plugins as it starts, rewriting Codex's plugin cache (0.167.2): the
    # look Settings → Agents & models takes (its models, limits) turns that off for its own
    # process; workers keep it. Startup's first-run card runs no app-server at all.
    _, runs = codex_installed
    _, runtime = await started(tmp_path)
    try:
        await until(lambda: runtime.snapshot().get("agent_offers"))
        assert not [run for run in ran(runs) if run.startswith("app-server")]
        await runtime.look_for_agents()
        looked = [run for run in ran(runs) if run.startswith("app-server")]
        assert looked == [" ".join(['app-server -c approvals_reviewer="user"',
                                    *backend_codex.LOOK_SETTINGS])]  # fmt: skip
    finally:
        await runtime.stop()


async def test_claudes_first_run_card_runs_only_its_version_and_sign_in(
    tmp_path, claude_on_path, monkeypatch
):
    # Nothing else at startup: no check's key (a hash of the CLI), no models, no session.
    _, runs = claude_on_path

    def hashed(*_):
        raise AssertionError("its check's key is Settings' look's")

    monkeypatch.setattr(backend_claude, "check_key", hashed)
    _, runtime = await started(tmp_path)
    try:
        await until(lambda: runtime.snapshot().get("agent_offers"))
        (offer,) = runtime.snapshot()["agent_offers"]
        assert offer["name"] == "claude" and offer["state"].startswith("2.1.322 · signed in")
        assert [json.loads(line) for line in runs.read_text().splitlines()] == [
            ["--version"],
            ["auth", "status", "--json"],
        ]
    finally:
        await runtime.stop()


async def test_a_signed_out_agent_is_not_offered(tmp_path, codex_installed, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
    _, runtime = await started(tmp_path)
    try:
        await runtime.offer_agents()
        assert "agent_offers" not in runtime.snapshot()
        assert runtime.snapshot()["agents_found"] == ["codex"], "its tab says why, in Settings"
    finally:
        await runtime.stop()


def statuses(found: list[dict]) -> dict[str, dict]:
    return {entry["name"]: entry for entry in found}


async def test_each_agent_says_if_it_is_ready_who_pays_and_what_makes_it_ready(
    tmp_path, codex_installed, monkeypatch
):
    _, runtime = await started(tmp_path)
    try:
        found = statuses(await runtime.refresh_agent_status())
        assert list(found) == ["copilot", "codex", "claude"], "every agent, none hidden"
        assert (found["copilot"]["said"], found["copilot"]["pays"]) == (
            "Ready",
            "Uses your Copilot plan",
        )
        codex = found["codex"]
        assert (codex["status"], codex["pays"], codex["on"]) == (
            "ready",
            "Uses your ChatGPT sign-in",
            False,
        )
        claude = found["claude"]
        assert (claude["said"], claude["fix"]["command"]) == (
            "Not installed",
            "curl -fsSL https://claude.ai/install.sh | bash",
        )
        # Install runs that, word for word (agentsetup.py).
        assert claude["install"][0] == {"method": "script", "command": claude["fix"]["command"]}
        assert statuses(runtime.snapshot()["agents_status"])["codex"]["status"] == "ready"
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        found = statuses(await runtime.refresh_agent_status())
        assert found["codex"]["status"] == "ready", "glanced at lately: not run again at once"
        runtime.glanced.clear()
        codex = statuses(await runtime.refresh_agent_status())["codex"]
        assert (codex["said"], codex["fix"]) == (
            "Sign-in needed",
            {"command": "codex login", "then": ""},
        )
        assert "pays" not in codex
    finally:
        await runtime.stop()


async def test_signed_in_again_settings_says_ready_and_new_workers_start_there(
    tmp_path, codex_installed
):
    _, runtime = await started(tmp_path)
    try:
        runtime.agent_failed["codex"] = (
            "Codex is not signed in: sign in from Settings → Agents & models."
        )
        assert runtime.agent_status("codex")["said"] == "Sign-in needed"
        assert (await runtime.look_for_agent("codex"))["said"] == "Ready", "looked at again"
        assert "codex" not in runtime.agent_failed
    finally:
        await runtime.stop()


async def test_claude_on_an_api_key_says_whose_key_it_uses(tmp_path, claude_on_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", json.dumps({"authMethod": "api_key"}))
    _, runtime = await started(tmp_path)
    try:
        claude = statuses(await runtime.refresh_agent_status())["claude"]
        assert (claude["said"], claude["pays"]) == ("Ready", "Uses your Anthropic API key")
        monkeypatch.setenv("FAKE_CLAUDE_SIGNED_OUT", "1")
        runtime.glanced.clear()
        claude = statuses(await runtime.refresh_agent_status())["claude"]
        assert claude["fix"] == {"command": "claude auth login", "then": ""}
        assert (await runtime.look_for_agent("claude"))["said"] == "Sign-in needed"
    finally:
        await runtime.stop()


async def test_the_agent_chosen_in_the_composer_is_the_routers_to_use(tmp_path, codex_installed):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        message = await runtime.owner_send(harness.router_id, "Fix the login test", agent="codex")
        assert message["meta"]["agent"] == "codex"
        prompt = harness.store.messages(harness.router_id)[-1]["meta"]["prompt"]
        assert 'The owner chose Codex for this: spawn_worker\'s agent is "codex"' in prompt
        with pytest.raises(ValueError, match="Claude is turned off"):
            await runtime.owner_send(harness.router_id, "Thanks", agent="claude")
    finally:
        await runtime.stop()


@pytest.mark.parametrize("owner_named", [False, True], ids=["its-pick", "named"])
async def test_the_owners_chip_wins_over_the_agent_the_router_gives(
    tmp_path, codex_installed, owner_named
):
    """The owner chose Copilot in the composer: the Router's spawn runs there, whatever agent
    it gives (its own pick, with Let the Router choose on, or one it took the owner to name)."""
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        agents.save(harness.store, agents.planned(harness.store, {"router_chooses": True}))
        await runtime.owner_send(harness.router_id, "Fix the login test", agent="copilot")
        result = await spawn_on(harness, runtime, title="Fix it", instructions="Report done: x",
                                agent="codex", owner_named=owner_named)  # fmt: skip
        assert runtime.agent(result["session_id"]) == "copilot"
    finally:
        await runtime.stop()


async def test_last_used_starts_new_workers_on_the_agent_the_owner_last_chose(
    tmp_path, codex_installed
):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}, "new_workers": "last"})
        assert (await runtime.look_for_agents())["new_workers"] == "last"
        first = await spawn_on(harness, runtime, title="One", instructions="Report done: x")
        assert runtime.agent(first["session_id"]) == "copilot", "none chosen yet: Copilot"
        await runtime.owner_send(harness.router_id, "Fix the login test", agent="codex")
        named = await spawn_on(harness, runtime, title="Two", instructions="Report done: x")
        assert runtime.agent(named["session_id"]) == "codex"
        last = harness.store.messages(harness.router_id)[-1]["id"]
        harness.store.set(f"idle:{harness.router_id}", str(last))  # Its turn over: no chip.
        later = await spawn_on(harness, runtime, title="Three", instructions="Report done: x")
        assert runtime.agent(later["session_id"]) == "codex", "the one last chosen"
        await saved(runtime, {"codex": {"enabled": False}})
        assert agents.new_workers(harness.store) == "copilot", "off: Copilot"
    finally:
        await runtime.stop()


async def test_new_work_the_owner_chose_an_agent_for_is_not_given_to_a_worker_on_another(
    tmp_path, codex_installed
):
    """The owner's @codex (its chip) on new work: the Router may not give it as a task to a
    worker on another agent that knows the project (live, 0.1.55: @claude's went to a Codex
    worker); it starts it there with spawn_worker. Their worker on that agent may have it."""
    from conftest import call_tool

    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        other = await spawn_on(harness, runtime, title="One", instructions="Report done: x")
        await runtime.owner_send(harness.router_id, "Count the rows", agent="codex")
        refused = await call_tool(
            runtime, "message_session", harness.router_id, session_id=other["session_id"],
            text="Count the rows.", task="new", title="Count the rows · data", why_new="Another.",
        )  # fmt: skip
        assert refused.result_type == "failure"
        assert refused.error == (
            "Not sent: the owner chose Codex for this (their @codex in the message), and this "
            "worker runs on Copilot. Start it on Codex with spawn_worker."
        )
        assert [t["title"] for t in runtime.tasks.for_session(other["session_id"])] == ["One"]
        mine = await spawn_on(harness, runtime, title="Count it", instructions="Report done: 5")
        assert runtime.agent(mine["session_id"]) == "codex"
        await until(lambda: harness.store.session(mine["session_id"])["status"] == "done", 30)
        given = await call_tool(
            runtime, "message_session", harness.router_id, session_id=mine["session_id"],
            text="Also the columns.", task="new", title="Count the columns · data",
            why_new="Another.",
        )  # fmt: skip
        assert given.result_type != "failure", given.error
    finally:
        await runtime.stop()


async def test_the_page_names_each_model_as_its_agent_does_and_last_used(tmp_path, codex_installed):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        assert "model_names" not in runtime.agents_view(), "nothing started to list them"
        looked = await runtime.look_for_agents()
        assert looked["last_used"] == "copilot"
        assert runtime.agents_view()["model_names"] == {
            "codex": {"gpt-test": "GPT-TEST", "gpt-mini": "GPT-MINI"}
        }
        agents.used(harness.store, "codex")
        assert (await runtime.look_for_agents())["last_used"] == "codex"
    finally:
        await runtime.stop()


async def test_letting_the_router_choose_changes_its_rule_and_only_then(tmp_path, codex_installed):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        rule = router_options(harness, runtime)["system_message"]["content"]
        assert "set spawn_worker's agent only when the owner names one" in rule
        before = agents.configured(harness.store)
        chose = agents.router_chooses(harness.store)
        agents.save(harness.store, agents.planned(harness.store, {"router_chooses": True}))
        await runtime.agents_saved(before, chose)
        chooses = router_options(harness, runtime)["system_message"]["content"]
        assert "the owner lets you choose" in chooses and "Say which you chose and why" in chooses
        assert runtime.snapshot()["agents"]["router_chooses"] is True
        with pytest.raises(ValueError, match="Let the Router choose: on or off"):
            agents.planned(harness.store, {"router_chooses": "yes"})
    finally:
        await runtime.stop()


def test_the_routers_choosing_rule_never_runs_work_on_another_agent_unasked():
    rule = prompts.with_agents_rule("<chiefofstaff_role>\n</chiefofstaff_role>", ["Codex"], True)
    assert "never start it on another unless they say so" in rule


async def test_a_workers_work_continues_on_another_agent_in_its_folders(tmp_path, claude_on_path):
    from test_claude_workers import claude_worker

    harness, runtime, worker, _ = await claude_worker(tmp_path)
    try:
        await runtime.set_session_yolo(worker, "off")  # Ask first, chosen for this worker.
        with pytest.raises(ValueError, match="already runs on Claude"):
            await runtime.continue_on(worker, "claude")
        row = harness.store.session(worker)
        made = await runtime.continue_on(worker, "copilot")
        new = harness.store.session(made["session_id"])
        assert runtime.agent(new["id"]) == "copilot"
        assert (new["repos"], new["task"]) == (row["repos"], row["task"])
        assert new["cwd"] != row["cwd"], "a folder of its own"
        assert new["yolo"] == "off" and not runtime.yolo(new), "it asks first too"
        old = harness.store.session(worker)
        assert not old["closed_at"] and old["repos"] == row["repos"], "left open, as it was"
        assert await asyncio.to_thread(Path(row["cwd"]).is_dir)
        task = runtime.tasks.get(made["task_id"])
        assert task["goal"].startswith(
            "You continue the work of another worker, “Fix it”, which ran on Claude"
        )
        assert "say so in your first reply" in task["goal"]
        given = [m for m in harness.store.messages(new["id"]) if m["kind"] == "router"]
        assert given[0]["meta"]["prompt"].startswith('<message from="router" type="task"')
        assert (
            "notice",
            "Continued on Copilot by a new worker, “Fix it”; this one stays open.",
        ) in harness.kinds(worker)
    finally:
        await runtime.stop()


async def test_a_worker_that_starts_work_while_its_handover_is_written_is_not_handed_over(
    tmp_path, claude_on_path, monkeypatch
):
    from test_claude_workers import claude_worker

    harness, runtime, worker, _ = await claude_worker(tmp_path)
    try:
        repos = harness.store.session(worker)["repos"]
        summary = runtime.handoff_summary

        async def meanwhile(row, agent):
            written = await summary(row, agent)
            runtime.update_session(worker, status="working")  # A message it got meanwhile.
            return written

        monkeypatch.setattr(runtime, "handoff_summary", meanwhile)
        with pytest.raises(ValueError, match="It is at work"):
            await runtime.continue_on(worker, "copilot")
        assert [r["id"] for r in harness.store.open_sessions(WORKER)] == [worker], "none made"
        assert harness.store.session(worker)["repos"] == repos, "nothing handed over"
        assert not harness.store.get(f"continued:{worker}")
    finally:
        await runtime.stop()


async def test_a_continued_workers_process_ends_before_the_new_one_and_its_folder_stays(
    tmp_path, claude_on_path, monkeypatch
):
    """Never two writing in its folders: the worker it continues has closed and its process
    ended before the new worker is made; its workspace, now the new one's, stays (even empty)."""
    from test_claude_workers import claude_worker

    harness, runtime, worker, _ = await claude_worker(tmp_path)
    try:
        claude = runtime.agents["claude"].backend
        made = harness.store.create_session
        seen = []

        def creating(session_id, *arguments, **more):
            old = claude.workers.get(worker)
            seen.append(bool(old and old.ending_or_alive()))
            return made(session_id, *arguments, **more)

        monkeypatch.setattr(harness.store, "create_session", creating)
        # (One writing in the owner's folder: handed over, not copied.)
        monkeypatch.setattr(runtime, "stays_open", lambda checkouts: False)
        workspace = runtime.workspaces.workspace(harness.store.session(worker))
        done = await runtime.continue_on(worker, "copilot")
        assert seen == [False], "its process had ended"
        await runtime.workers.release(worker)  # As closing it does, in the background.
        assert await asyncio.to_thread(workspace.is_dir), "the new worker's now"
        assert runtime.workspaces.workspace(harness.store.session(done["session_id"])) == workspace
    finally:
        await runtime.stop()


def git(*arguments, cwd):
    import subprocess

    found = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *arguments],
                           cwd=cwd, check=True, capture_output=True, text=True)  # fmt: skip
    return found.stdout.strip()


async def project_worker(tmp_path, *agents_on: str, **arguments):
    from test_round7_split import published

    from chiefofstaff.models import Settings

    app = published(tmp_path, "app")
    harness, runtime = await started(tmp_path, Settings(projects=[str(app)]))
    await saved(runtime, {name: {"enabled": True} for name in agents_on})
    result = await spawn_on(
        harness, runtime, title="Feature", project=str(app), isolate=True,
        instructions="Report done: x", **arguments,
    )  # fmt: skip
    return harness, runtime, result


async def test_continued_on_another_agent_it_stays_open_and_the_new_one_has_a_copy(
    tmp_path, codex_installed
):
    harness, runtime, result = await project_worker(tmp_path, "codex")
    worker = result["session_id"]
    try:
        runtime.update_session(worker, status="idle")
        (checkout,) = json.loads(harness.store.session(worker)["repos"])
        tree = Path(checkout["worktree"])
        (tree / "a.txt").write_text("one\n")
        git("add", "a.txt", cwd=tree)
        git("commit", "-qm", "a", cwd=tree)
        (tree / "a.txt").write_text("two\n")
        (tree / "new.txt").write_text("new\n")
        preview = await runtime.continue_preview(worker, "codex")
        assert preview["line"] == (
            f"Sends a summary ({preview['words']} words) and its changes (2 files, +1 −0) to a "
            "new Codex worker in a copy of its worktree; this one stays open."
        )
        made = await runtime.continue_on(worker, "codex")
        old = harness.store.session(worker)
        assert not old["closed_at"] and json.loads(old["repos"]) == [checkout], "as it was"
        new = harness.store.session(made["session_id"])
        assert runtime.agent(new["id"]) == "codex"
        (copy,) = json.loads(new["repos"])
        copied = Path(copy["worktree"])
        assert copied != tree and copy["branch"] != checkout["branch"]
        assert git("rev-parse", "HEAD", cwd=copied) == git("rev-parse", "HEAD", cwd=tree)
        assert (copied / "a.txt").read_text() == "two\n", "its uncommitted change"
        assert (copied / "new.txt").read_text() == "new\n", "its new file"
        assert copy["base_ref"] == checkout["base_ref"]
        assert (tree / "a.txt").read_text() == "two\n", "the first one's untouched"
        goal = runtime.tasks.get(made["task_id"])["goal"]
        assert str(copied) in goal and str(tree) not in goal, "told of its own folder"
    finally:
        await runtime.stop()


async def test_before_its_first_step_a_worker_restarts_on_another_agent_with_its_task(
    tmp_path, codex_installed
):
    harness, runtime, result = await project_worker(tmp_path, "codex")
    worker, task = result["session_id"], result["task_id"]
    try:
        assert runtime.view(harness.store.session(worker))["untouched"]
        first = next(m for m in harness.store.messages(worker) if m["kind"] == "router")
        repos = harness.store.session(worker)["repos"]
        made = await runtime.restart_on(worker, "codex", model="", effort="high")
        new = harness.store.session(made["session_id"])
        assert harness.store.session(worker)["closed_at"], "the first one closed"
        assert runtime.agent(new["id"]) == "codex" and new["repos"] == repos, "its folders"
        assert made["task_id"] == task and runtime.tasks.get(task)["session_id"] == new["id"]
        assert runtime.tasks.get(task)["status"] == "queued", "its task goes on, not ended"
        given = next(m for m in harness.store.messages(new["id"]) if m["kind"] == "router")
        assert (given["text"], given["meta"]["prompt"]) == (first["text"], first["meta"]["prompt"])
        assert runtime.agent_choice(new["id"])["effort"] == "high"
        assert ("notice", "Restarted “Feature” on Codex") in harness.kinds(harness.router_id)
        assert agents.stored(harness.store) and harness.store.get(agents.LAST_USED) == "codex"
        # Once it did something, only Continue with….
        runtime.add_message(new["id"], "tool", "Ran `ls`")
        assert "untouched" not in runtime.view(harness.store.session(new["id"]))
        with pytest.raises(ValueError, match="use Continue with"):
            await runtime.restart_on(new["id"], "copilot")
    finally:
        await runtime.stop()


async def test_two_agents_chosen_run_the_task_on_each_side_by_side(tmp_path, codex_installed):
    from test_round7_split import published

    from chiefofstaff.models import Settings

    app = published(tmp_path, "app")
    harness, runtime = await started(tmp_path, Settings(projects=[str(app)]))
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        message = await runtime.owner_send(
            harness.router_id, "Fix the login test", compare=["copilot", "codex"]
        )
        assert message["meta"]["agents"] == ["copilot", "codex"]
        found = await spawn_on(harness, runtime, title="Fix the login test",
                               instructions="Report done: x", project=str(app))  # fmt: skip
        assert found["compare"] == "Fix the login test" and "side by side" in found["note"]
        made = [worker["session_id"] for worker in found["workers"]]
        assert [runtime.agent(made_id) for made_id in made] == ["copilot", "codex"]
        trees = [json.loads(harness.store.session(i)["repos"])[0]["worktree"] for i in made]
        assert len(set(trees)) == 2, "each in a worktree of its own"
        groups = [runtime.view(harness.store.session(i))["compare"] for i in made]
        assert groups[0] == groups[1] and groups[0]["title"] == "Fix the login test"
        assert groups[0]["agents"] == ["copilot", "codex"]
    finally:
        await runtime.stop()


async def test_the_router_compares_when_asked_in_words(tmp_path, codex_installed):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        assert "compare_with" in schema(router_options(harness, runtime))["properties"]
        found = await spawn_on(harness, runtime, title="Fix it", instructions="Report done: x",
                               agent="codex", compare_with=["copilot"])  # fmt: skip
        assert not hasattr(found, "result_type"), found.text_result_for_llm
        assert [w["agent"] for w in found["workers"]] == ["codex", "copilot"]
    finally:
        await runtime.stop()


async def test_a_worker_at_work_is_not_continued_elsewhere(tmp_path, claude_on_path):
    from test_claude_workers import claude_worker

    _, runtime, worker, _ = await claude_worker(tmp_path)
    try:
        runtime.update_session(worker, status="working")
        with pytest.raises(ValueError, match="It is at work"):
            await runtime.continue_on(worker, "copilot")
        runtime.update_session(worker, status="idle")
        with pytest.raises(ValueError, match="already runs on Claude"):
            await runtime.continue_on(worker, "claude")
    finally:
        await runtime.stop()


async def test_a_named_agent_that_cannot_run_offers_choices_and_starts_nothing_else(
    tmp_path, codex_installed, monkeypatch
):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        await runtime.disconnect_agent("codex")
        result = await spawn_on(
            harness, runtime, title="Fix the login test", instructions="Fix it", agent="codex"
        )
        assert result.result_type == "failure"
        # The card says why: the Router hears so, and does not say it again.
        assert "on a card in this chat" in result.text_result_for_llm
        assert "do not say it again" in result.text_result_for_llm
        assert harness.store.open_sessions(WORKER) == [], "never started on another"
        (card,) = [
            m for m in harness.store.messages(harness.router_id) if m["meta"].get("agent_refused")
        ]
        key = card["meta"]["agent_refused"]
        assert card["text"] == (
            "Codex needs you to sign in. Your message is saved: “Fix the login test” starts on "
            "Codex once you have."
        )
        # The command that makes it ready, for the owner to run; Copilot offered instead.
        assert card["meta"]["fix"] == {"command": "codex login", "then": ""}
        assert card["meta"]["instead"] == "copilot"
        assert runtime.snapshot()["agent_refusals"] == [key]
        await runtime.answer_refusal(key, "copilot")
        (started_on,) = harness.store.open_sessions(WORKER)
        assert runtime.agent(started_on["id"]) == "copilot"
        assert started_on["title"] == "Fix the login test"
        said = [
            m["text"].split(" · ")[0]  # (Why it is new, on hover.)
            for m in harness.store.messages(harness.router_id)
            if m["kind"] == "notice" and m["text"].startswith("Started")
        ]
        assert said == ["Started “Fix the login test” on Copilot instead"], "one line, where"
        assert "agent_refusals" not in runtime.snapshot()
        with pytest.raises(ValueError, match="answered already"):
            await runtime.answer_refusal(key, "wait")
    finally:
        await runtime.stop()


async def test_work_for_a_worker_whose_agent_is_signed_out_continues_on_copilot_in_one_click(
    tmp_path, codex_installed, monkeypatch
):
    """The owner names Codex and the Router gives the work to a Codex worker they have, but
    Codex is signed out now: one line with the fix and Continue with Copilot (a new worker
    there, from that worker's summary, with what it was given), as for a new worker."""
    from conftest import call_tool

    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        made = await spawn_on(harness, runtime, title="Fix it", instructions="Report done: Fixed.",
                              agent="codex")  # fmt: skip
        worker = made["session_id"]
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        tasks = [task["id"] for task in runtime.tasks.for_session(worker)]
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        await runtime.disconnect_agent("codex")
        refused = await call_tool(
            runtime, "message_session", harness.router_id, session_id=worker,
            text="Also count the rows.", task="new", title="Count the rows · data",
            why_new="Another deliverable.",
        )  # fmt: skip
        assert refused.error.startswith("Not sent: Codex is not signed in"), refused
        assert "on a card in this chat" in refused.error and "do not say it again" in refused.error
        assert [task["id"] for task in runtime.tasks.for_session(worker)] == tasks, "none given"
        (card,) = [
            m for m in harness.store.messages(harness.router_id) if m["meta"].get("agent_refused")
        ]
        assert card["text"] == (
            "Codex needs you to sign in. Your message is saved: “Fix it” gets it on Codex once you "
            "have."
        )
        assert card["meta"]["worker"] == worker and card["meta"]["instead"] == "copilot"
        assert card["meta"]["fix"] == {"command": "codex login", "then": ""}
        moved = await runtime.answer_refusal(card["meta"]["agent_refused"], "copilot")
        new = harness.store.session(moved["session_id"])
        assert runtime.agent(new["id"]) == "copilot"
        goal = runtime.tasks.get(moved["task_id"])["goal"]
        assert "Also count the rows." in goal and "did not reach it" in goal, goal
        assert not harness.store.session(worker)["closed_at"], "the first one stays open"
        assert ("notice", "Continued “Fix it” on Copilot") in harness.kinds(harness.router_id)
        assert "agent_refusals" not in runtime.snapshot()
    finally:
        await runtime.stop()


@pytest.mark.parametrize("held", [False, True])
async def test_a_refusal_card_is_answered_once(tmp_path, codex_installed, monkeypatch, held):
    """Two answers at once on an agent refusal's card (a double click, two tabs): one is
    taken, the other finds the card answered; nothing is made twice. For a worker's card:
    Continue with Copilot; for new work held for a sign-in: Run on Copilot instead."""
    from conftest import call_tool

    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        made = await spawn_on(harness, runtime, title="Fix it", instructions="Report done: Fixed.",
                              agent="codex")  # fmt: skip
        worker = made["session_id"]
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        await runtime.disconnect_agent("codex")
        if held:
            await spawn_on(harness, runtime, title="Count the rows · data",
                           instructions="Report done: 42 rows.", agent="codex")  # fmt: skip
        else:
            await call_tool(
                runtime, "message_session", harness.router_id, session_id=worker,
                text="Also count the rows.", task="new", title="Count the rows · data",
                why_new="Another deliverable.",
            )  # fmt: skip
        (card,) = [
            m for m in harness.store.messages(harness.router_id) if m["meta"].get("agent_refused")
        ]
        before = {r["id"] for r in harness.store.open_sessions(WORKER)}
        key = card["meta"]["agent_refused"]
        answers = await asyncio.gather(
            runtime.answer_refusal(key, "copilot"),
            runtime.answer_refusal(key, "copilot"),
            return_exceptions=True,
        )
        failed = [a for a in answers if isinstance(a, Exception)]
        assert len(failed) == 1 and "answered already" in str(failed[0]), answers
        await until(lambda: len(harness.store.open_sessions(WORKER)) > len(before), 10)
        await settle(runtime)
        new = {r["id"] for r in harness.store.open_sessions(WORKER)} - before
        assert len(new) == 1, "one worker made"
        assert "agent_refusals" not in runtime.snapshot()
    finally:
        await runtime.stop()


async def test_a_card_for_a_worker_continued_already_sends_its_message_to_the_new_one(
    tmp_path, codex_installed, monkeypatch
):
    """A worker of the owner's on Codex, continued on Copilot already (its ⋯), then given more
    while Codex is signed out: Continue with Copilot on its card sends that to the worker it
    went to, makes no other."""
    from conftest import call_tool

    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        made = await spawn_on(harness, runtime, title="Fix it", instructions="Report done: Fixed.",
                              agent="codex")  # fmt: skip
        worker = made["session_id"]
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        first = await runtime.continue_on(worker, "copilot")
        await until(lambda: harness.store.session(first["session_id"])["status"] != "queued", 30)
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        await runtime.disconnect_agent("codex")
        await call_tool(
            runtime, "message_session", harness.router_id, session_id=worker,
            text="Also count the rows.", task="new", title="Count the rows · data",
            why_new="Another deliverable.",
        )  # fmt: skip
        (card,) = [
            m for m in harness.store.messages(harness.router_id) if m["meta"].get("agent_refused")
        ]
        before = {r["id"] for r in harness.store.open_sessions(WORKER)}
        sent = await runtime.answer_refusal(card["meta"]["agent_refused"], "copilot")
        assert sent["session_id"] == first["session_id"]
        assert {r["id"] for r in harness.store.open_sessions(WORKER)} == before, "none made"
        tasks = runtime.tasks.for_session(first["session_id"])
        assert [t["title"] for t in tasks][-1] == "Count the rows · data", "given to it"
        assert "Also count the rows." in tasks[-1]["goal"]
        meta = harness.store.message(card["id"])["meta"]
        assert meta["resumed"] == "Sent to “Fix it” on Copilot"
        assert "agent_refusals" not in runtime.snapshot()
    finally:
        await runtime.stop()


async def test_the_routers_own_pick_that_cannot_run_starts_on_copilot_an_owners_does_not(
    tmp_path, codex_installed, monkeypatch
):
    """Letting the Router choose: it hears which agents can run now (and why another can't),
    and says whether the owner named its pick: one it chose itself that cannot run starts on
    Copilot, saying why; one the owner named is never replaced."""
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}, "router_chooses": True})
        assert "owner_named" in schema(router_options(harness, runtime))["properties"]
        assert (await runtime.toolset.agents_listed(["codex"]))["available"] == ["codex"]
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        await runtime.disconnect_agent("codex")
        chose = await spawn_on(
            harness, runtime, title="Lint it", instructions="Lint", agent="codex"
        )
        assert runtime.agent(chose["session_id"]) == "copilot"
        assert runtime.view(harness.store.session(chose["session_id"]))[
            "agent_fallback"
        ].startswith("Started on Copilot: Codex could not start: Codex is not signed in")
        listed = await runtime.toolset.agents_listed(["codex"])
        assert listed["available"] == [] and "not signed in" in listed["not_now"]["codex"]
        named = await spawn_on(harness, runtime, title="Fix it", instructions="Fix",
                               agent="codex", owner_named=True)  # fmt: skip
        assert named.result_type == "failure", "the owner named it: never replaced"
    finally:
        await runtime.stop()


async def test_an_agent_chosen_in_the_composer_is_kept_or_refused_never_dropped(
    tmp_path, codex_installed
):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        await runtime.owner_send(harness.router_id, "Port the parser", agent="codex")
        # The Router leaves agent empty for that turn: the owner's choice holds.
        made = await spawn_on(harness, runtime, title="Port it", instructions="Port")
        assert runtime.agent(made["session_id"]) == "codex"
        await saved(runtime, {"codex": {"enabled": False}})
        before = len(harness.store.messages(harness.router_id))
        with pytest.raises(ValueError, match="Codex is turned off in Settings → Agents & models"):
            await runtime.owner_send(harness.router_id, "And the lexer", agent="codex")
        assert len(harness.store.messages(harness.router_id)) == before, "nothing sent"
    finally:
        await runtime.stop()


async def test_the_default_agent_unable_to_run_starts_it_on_copilot_and_says_why(
    tmp_path, codex_installed, monkeypatch
):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}, "new_workers": "codex"})
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        await runtime.disconnect_agent("codex")
        made = await spawn_on(harness, runtime, title="Fix the login test", instructions="Fix it")
        worker = made["session_id"]
        assert runtime.agent(worker) == "copilot"
        view = runtime.view(harness.store.session(worker))
        assert view["agent_fallback"].startswith("Started on Copilot: ")
    finally:
        await runtime.stop()


async def limited(harness, runtime, agent: str, instructions: str):
    """A task on `agent` whose turn a limit ends: once it is paused, its worker, task and what
    its timeline said last (its notice, and the notice's tooltip)."""
    result = await spawn_on(harness, runtime, title="Fix it", instructions=instructions,
                            agent=agent)  # fmt: skip
    worker, task = result["session_id"], result["task_id"]
    await until(lambda: runtime.tasks.get(task)["status"] == "waiting", 30)
    await until(lambda: harness.store.session(worker)["status"] == "idle")
    last = harness.store.messages(worker)[-1]
    return worker, task, last


def paused_as_by_stop(harness, runtime, worker, task, last, line: str):
    assert harness.store.get(f"stopped:{task}") == agents.PLAN_STOP
    assert last["kind"] == "notice" and last["text"] == f"Paused: {line}", last["text"]
    assert runtime.tasks.get(task)["progress"] == last["text"]
    kinds = [kind for kind, _ in harness.kinds(worker)]
    assert "error" not in kinds, "paused, not failed"
    router = " ".join(text for _, text in harness.kinds(harness.router_id))
    assert "failed" not in router, "nor told the Router as a failure"


async def test_a_claude_plans_limit_mid_task_pauses_it_with_its_reset_not_failing_it(
    tmp_path, claude_on_path, monkeypatch
):
    resets = int(time.time()) + 5400
    info = {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": resets}
    monkeypatch.setenv("FAKE_CLAUDE_LIMIT", json.dumps(info))
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        worker, task, last = await limited(harness, runtime, "claude", "Hit the usage limit")
        line = f"Claude's 5-hour limit is used up · resets {agents.reset_words(resets)}"
        paused_as_by_stop(harness, runtime, worker, task, last, line)
        assert last["meta"]["full"].startswith("API Error: Request rejected (429)")
        assert json.loads(harness.store.get("agentlimit:claude"))["status"] == "rejected"
        # Go on before it resets: refused at once, saying so; still paused, nothing sent.
        with pytest.raises(Exception, match="5-hour limit is used up · resets "):
            await runtime.task_actions.follow_up(task, "Go on where you stopped.")
        assert runtime.tasks.get(task)["status"] == "waiting"
        assert harness.store.get(f"stopped:{task}") == agents.PLAN_STOP
    finally:
        await runtime.stop()


async def test_a_paused_task_says_its_reset_first_and_goes_on_by_itself_then(
    tmp_path, claude_on_path, monkeypatch
):
    """Its row: "Claude's 5-hour limit is used up · resets <time>", under Monitoring: it goes on
    by itself then, unasked (Go on at <time> again for a reset said later); nothing sent
    before; once it resets, it goes on as the owner's Go on would."""
    resets = int(time.time()) + 3600
    info = {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": resets}
    monkeypatch.setenv("FAKE_CLAUDE_LIMIT", json.dumps(info))
    monkeypatch.setattr(task_actions, "GO_ON_MARGIN", 0.2)
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        worker, task, _ = await limited(harness, runtime, "claude", "Hit the usage limit")
        line = f"Claude's 5-hour limit is used up · resets {agents.reset_words(resets)}"
        paused = {"agent": "claude", "resets_at": resets, "rate": False, "agent_name": "Claude",
                  "window": "5-hour limit"}  # fmt: skip
        view = runtime.task_actions.view(runtime.tasks.get(task))
        assert view["paused"] == {**paused, "line": line, "go_on_at": resets}
        assert view["attention"] == "", "it goes on by itself: not the owner's to do now"
        # Its reset near now (as its stream would say it later on).
        resets = int(time.time()) + 3
        runtime.agents["claude"].backend.limit["resets_at"] = resets
        runtime.record_pause(worker, "claude", runtime.agents["claude"].backend.limit)
        sent = len(harness.store.messages(worker))
        await runtime.task_actions.go_on_at(task)
        view = runtime.task_actions.view(runtime.tasks.get(task))
        assert view["paused"]["go_on_at"] == resets
        assert view["attention"] == "", "not the owner's to do now"
        await asyncio.sleep(1)
        assert runtime.tasks.get(task)["status"] == "waiting", "nothing before its reset"
        assert len(harness.store.messages(worker)) == sent
        await until(lambda: runtime.tasks.get(task)["status"] != "waiting", 30)
        assert harness.store.get(f"goonat:{task}") is None
        assert harness.store.get(f"stopped:{task}") is None
        assert "Go on where you stopped." in [m["text"] for m in harness.store.messages(worker)]
    finally:
        await runtime.stop()


async def test_work_a_limit_paused_goes_on_elsewhere_once_and_its_card_leaves_needs_you(
    tmp_path, claude_on_path, monkeypatch
):
    """The owner's (0.1.53): Continue on another agent… on a task Claude's plan limit paused
    made the new worker, but the paused card stayed in Needs you, still to go on at the reset,
    and a second click made a second worker. Now the task is replaced by the new one's (what
    waited for it waits for that), its card goes, nothing goes on at the reset; asked again (a
    double click, a stale card, another tab), nothing new is made: the same continuation."""
    resets = int(time.time()) + 3600
    info = {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": resets}
    monkeypatch.setenv("FAKE_CLAUDE_LIMIT", json.dumps(info))
    monkeypatch.setattr(task_actions, "GO_ON_MARGIN", 0)
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        worker, task, _ = await limited(harness, runtime, "claude", "Hit the usage limit")
        assert harness.store.get(f"goonat:{task}") == str(resets), "to go on at its reset"
        other = harness.store.create_session("w-next", WORKER, "Use it", str(tmp_path))
        waiting = runtime.tasks.create(other["id"], "Use it", "Use what it made.", after=[task])
        first, again = await asyncio.gather(
            runtime.continue_on(worker, "copilot"), runtime.continue_on(worker, "copilot")
        )
        assert again == {**first, "already": True, "agent": "copilot"}, "one continuation"
        later = await runtime.continue_on(worker, "copilot")  # A stale card.
        assert later["session_id"] == first["session_id"] and later["already"]
        preview = await runtime.continue_preview(worker, "copilot")
        assert preview == {
            "line": "Already continued on Copilot by a new worker, “Fix it”.",
            "already": first["session_id"],
        }
        mine = [r["id"] for r in harness.store.open_sessions(WORKER) if r["id"] != other["id"]]
        assert sorted(mine) == sorted([worker, first["session_id"]]), "one new worker"
        old = runtime.tasks.get(task)
        assert old["status"] == "cancelled", "not left paused in Needs you"
        assert old["progress"] == (
            f"Cancelled by you: Continued on Copilot by a new worker, as {first['task_id']} "
            "“Fix it”."
        )
        assert harness.store.get(f"replacedby:{task}") == first["task_id"]
        assert {d["id"] for d in runtime.tasks.deps(waiting["id"])} == {first["task_id"]}
        assert harness.store.get(f"goonat:{task}") is None
        await asyncio.sleep(0.05)
        assert not [job for job in runtime.jobs if "going_on_at" in repr(job)]
        assert not harness.store.session(worker)["closed_at"], "left open, for reference"
        said = "It was continued on Copilot by a new worker, as"
        with pytest.raises(ValueError, match=said):
            await runtime.task_actions.go_on_at(task)  # Its stale card's Go on at…
        with pytest.raises(ValueError, match=said):
            await runtime.task_actions.follow_up(task, "Go on where you stopped.")
        sent = len(harness.store.messages(worker))
        await runtime.task_actions.going_on_at(task, resets - 7200)  # Its reset come: nothing.
        assert len(harness.store.messages(worker)) == sent
    finally:
        await runtime.stop()


@pytest.mark.parametrize("said_by", ["owner", "worker"])
async def test_a_task_set_to_go_on_at_its_reset_does_not_once_told_something_else(
    tmp_path, claude_on_path, monkeypatch, said_by
):
    """What the owner (in its chat) or another worker (tell_session) says to its worker, once
    taken, takes the place of going on by itself at its reset."""
    from chiefofstaff.peers import TellSession

    resets = int(time.time()) + 3600
    info = {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": resets}
    monkeypatch.setenv("FAKE_CLAUDE_LIMIT", json.dumps(info))
    monkeypatch.setattr(task_actions, "GO_ON_MARGIN", 0)
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        worker, task, _ = await limited(harness, runtime, "claude", "Hit the usage limit")
        await runtime.task_actions.go_on_at(task)
        assert harness.store.get(f"goonat:{task}") == str(resets)
        # Its limit reset early (as its stream would say it): what it is told now is taken.
        runtime.agents["claude"].backend.limit = {"status": "allowed", "resets_at": 0}
        harness.store.delete("agentlimit:claude")
        if said_by == "owner":
            await runtime.owner_send(worker, "Do the other part instead.")
        else:
            other = await spawn_on(harness, runtime, title="Other", instructions="Report done: o.")
            await runtime.peers.t_tell_session(
                TellSession(session_id=worker, text="Do the other part instead."),
                other["session_id"],
            )
        assert harness.store.get(f"goonat:{task}") is None
        assert runtime.task_actions.view(runtime.tasks.get(task))["paused"]["go_on_at"] == 0
        # Its wait for the reset ended too (not left asleep for an hour among the jobs).
        await asyncio.sleep(0.05)
        assert not [job for job in runtime.jobs if "going_on_at" in repr(job)]
        await runtime.task_actions.going_on_at(task, resets - 7200)  # Its time come: nothing.
        said = [m["text"] for m in harness.store.messages(worker)]
        assert "Go on where you stopped." not in said
    finally:
        await runtime.stop()


async def test_a_task_asked_to_go_on_at_its_reset_still_does_after_a_restart(
    tmp_path, claude_on_path, monkeypatch
):
    """Asked to go on at its reset, and Chief of Staff was not running then: it goes on at the
    next start; anything the owner says to it first takes the place of that."""
    info = {"status": "limited"}
    monkeypatch.setenv("FAKE_CLAUDE_LIMIT", json.dumps(info))
    monkeypatch.setattr(task_actions, "GO_ON_MARGIN", 0)
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        _worker, task, _ = await limited(harness, runtime, "claude", "Hit the usage limit")
        view = runtime.task_actions.view(runtime.tasks.get(task))
        assert view["paused"]["line"] == (
            "Claude is refusing requests for a rate limit; it didn't say for how long"
        ), "no reset said: said plainly"
        # As one that was to go on while Chief of Staff was not running.
        harness.store.set(f"goonat:{task}", str(int(time.time()) - 60))
        runtime.task_actions.arm_scheduled()
        await until(lambda: runtime.tasks.get(task)["status"] != "waiting", 30)
        assert harness.store.get(f"goonat:{task}") is None
    finally:
        await runtime.stop()


async def test_a_rate_limit_with_an_api_key_pauses_a_claude_worker_and_is_not_retried(
    tmp_path, claude_on_path
):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        worker, task, last = await limited(harness, runtime, "claude", "Hit the usage limit")
        line = "Claude is refusing requests for a rate limit; it didn't say for how long"
        paused_as_by_stop(harness, runtime, worker, task, last, line)
        await asyncio.sleep(0.5)  # Nothing tried again: one turn, the one refused.
        assert harness.store.get(f"retries:{worker}") is None
        assert harness.store.get("agentlimit:claude") is None, "no plan's limit known"
    finally:
        await runtime.stop()


async def test_a_codex_plans_usage_limit_mid_task_pauses_it_with_its_reset(
    tmp_path, codex_installed
):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        worker, task, last = await limited(harness, runtime, "codex", "Hit the usage limit")
        resets = json.loads(harness.store.get("agentlimit:codex"))["resets_at"]
        assert resets > time.time()
        line = f"Codex's 5-hour limit is used up · resets {agents.reset_words(resets)}"
        paused_as_by_stop(harness, runtime, worker, task, last, line)
        assert last["meta"]["full"].startswith("You've hit your usage limit.")
        # Kept for Settings, in Codex's name.
        assert agents.limit_line(agents.live_limit(harness.store, "codex"), name="codex") == line
    finally:
        await runtime.stop()


async def test_a_rate_limit_codex_gave_up_on_pauses_its_worker_and_is_not_retried(
    tmp_path, codex_installed
):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        worker, task, last = await limited(harness, runtime, "codex", "Hit the rate limit")
        paused_as_by_stop(
            harness,
            runtime,
            worker,
            task,
            last,
            "Codex is refusing requests for a rate limit; it didn't say for how long",
        )
        assert harness.store.get(f"retries:{worker}") is None
        assert harness.store.get("agentlimit:codex") is None
    finally:
        await runtime.stop()


async def test_a_plans_limit_with_extra_usage_on_pauses_nothing_and_refuses_nothing(
    tmp_path, claude_on_path, monkeypatch
):
    resets = int(time.time()) + 5400
    info = {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": resets,
            "overageStatus": "allowed", "isUsingOverage": True}  # fmt: skip
    monkeypatch.setenv("FAKE_CLAUDE_LIMIT", json.dumps(info))
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        result = await spawn_on(harness, runtime, title="Fix it", agent="claude",
                                instructions="Report done: Fixed it.")  # fmt: skip
        worker, task = result["session_id"], result["task_id"]
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        assert runtime.tasks.get(task)["status"] == "review"
        assert harness.store.get(f"stopped:{task}") is None
        line = (await runtime.look_for_agent("claude"))["limit_line"]
        assert line == (
            "Your Claude plan's 5-hour limit is reached; workers go on with extra usage until it "
            f"resets at {agents.clock(resets)}."
        )
    finally:
        await runtime.stop()


def test_a_codex_failure_is_a_limit_only_for_its_limits():
    from chiefofstaff.backend_codex import limit_failure

    windows = {
        "primary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 9},
        "secondary": {"usedPercent": 30, "windowDurationMins": 300, "resetsAt": 5},
    }
    plan = limit_failure({"codexErrorInfo": "usageLimitExceeded"}, windows)
    assert plan == {"status": "rejected", "label": "weekly limit", "resets_at": 9}
    assert limit_failure({"codexErrorInfo": "usageLimitExceeded"}, {}) == {
        "status": "rejected",
        "resets_at": 0,
    }
    too_many = {"codexErrorInfo": {"responseTooManyFailedAttempts": {"httpStatusCode": 429}}}
    assert limit_failure(too_many, {})["status"] == "limited"
    for other in ("serverOverloaded", "contextWindowExceeded", None,
                  {"httpConnectionFailed": {"httpStatusCode": 502}}):  # fmt: skip
        assert limit_failure({"codexErrorInfo": other}, windows) is None, other


async def test_each_plans_usage_is_in_its_row(tmp_path, codex_installed, claude_on_path):
    harness, runtime = await started(tmp_path)
    try:
        codex = await runtime.look_for_agent("codex")
        assert codex["usage"][0]["label"] == "5-hour limit" and codex["usage"][0]["used"] == 42
        harness.store.set(
            "agentlimit:claude",
            json.dumps({"status": "allowed_warning", "window": "seven_day", "used": 85,
                        "resets_at": int(time.time()) + 86400}),
        )  # fmt: skip
        claude = await runtime.look_for_agent("claude")
        assert [(u["label"], u["used"]) for u in claude["usage"]] == [("weekly limit", 85)]
        assert "limit_line" not in claude, "said once: its usage line says it"
        reached = {"status": "rejected", "window": "seven_day", "resets_at": int(time.time()) + 60}
        harness.store.set("agentlimit:claude", json.dumps(reached))
        claude = await runtime.look_for_agent("claude")
        assert "usage" not in claude and claude["limit_line"].startswith(
            "Claude's weekly limit is used up · resets "
        ), "no percent known: in words"
    finally:
        await runtime.stop()


async def test_a_workers_tokens_show_against_its_limit(tmp_path, claude_on_path):
    from test_claude_workers import claude_worker

    harness, runtime, worker, _ = await claude_worker(tmp_path)
    try:
        agents.configure(harness.store, "claude", limit=50000)
        view = runtime.view(harness.store.session(worker))
        assert view["token_limit"] == 50000 and view["tokens"] > 0
    finally:
        await runtime.stop()


async def test_a_question_open_when_its_work_continues_on_another_agent_moves_with_it(
    tmp_path, claude_on_path
):
    """QA 0.1.61 Q6: a worker's open question stayed with it when its work went on to another
    agent (Continue with…), and the owner's answer went to the closed worker. The question
    moves with the work: one item under Needs you, its answer to the worker doing it now."""
    from chiefofstaff.digest import needs

    asking = "Call the report tool with arguments " + json.dumps(
        {"status": "question", "summary": "Port 8080 or 9090?", "options": ["8080", "9090"]}
    )
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        made = await spawn_on(harness, runtime, title="Ports", instructions=asking, agent="claude")
        worker = made["session_id"]
        await until(lambda: runtime.asks.session_questions(worker), 30)
        await until(lambda: harness.store.session(worker)["status"] != "working", 30)
        moved = await runtime.continue_on(worker, "copilot")
        await settle(runtime)
        now = moved["session_id"]
        assert not runtime.asks.session_questions(worker), "never answered to a closed worker"
        [question] = runtime.asks.session_questions(now)
        assert needs(runtime).count() == 1
        before = len(harness.session(now).sent)
        await runtime.task_actions.answer_question(question["id"], "9090")
        await settle(runtime)
        assert any("9090" in sent for sent in harness.session(now).sent[before:])
        assert not runtime.asks.session_questions(now) and needs(runtime).count() == 0
    finally:
        await runtime.stop()
