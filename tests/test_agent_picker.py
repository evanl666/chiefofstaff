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
from conftest import Harness
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
        assert not {"agents", "agents_found", "agent_offers", "agent_refusals"} & set(state)
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
        assert entry["problem"] == "Not set up: run `codex` in a terminal and sign in."
        with pytest.raises(RuntimeError, match=r"^Not set up: run `codex`"):
            await CodexAgent("codex").start()
        assert not runs.exists(), "nothing of Codex ran"
        assert list(home.iterdir()) == [] and not (tmp_path / "codex-home").exists()
    finally:
        await runtime.stop()


async def test_settings_look_runs_codex_without_its_plugin_sync(tmp_path, codex_installed):
    # Each app-server syncs plugins as it starts, rewriting Codex's plugin cache (0.167.2): the
    # look Settings → Coding agents takes (its models, limits) turns that off for its own
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
        row = harness.store.session(worker)
        made = await runtime.continue_on(worker, "copilot")
        new = harness.store.session(made["session_id"])
        assert runtime.agent(new["id"]) == "copilot"
        assert (new["cwd"], new["repos"], new["task"]) == (row["cwd"], row["repos"], row["task"])
        assert new["yolo"] == "off" and not runtime.yolo(new), "it asks first too"
        old = harness.store.session(worker)
        assert old["closed_at"] and json.loads(old["repos"] or "[]") == [], "handed over"
        assert await asyncio.to_thread(Path(row["cwd"]).is_dir), "its folder stays: the new one's"
        task = runtime.tasks.get(made["task_id"])
        assert task["goal"].startswith(
            "You continue the work of another worker, “Fix it”, which ran on Claude"
        )
        assert "say so in your first reply" in task["goal"]
        given = [m for m in harness.store.messages(new["id"]) if m["kind"] == "router"]
        assert given[0]["meta"]["prompt"].startswith('<message from="router" type="task"')
        assert ("notice", "Continued on Copilot by a new worker, “Fix it”, in its folders.") in (
            harness.kinds(worker)
        )
        assert runtime.view(old)["continued"] == new["id"]
        with pytest.raises(ValueError, match="Only an open worker"):
            await runtime.continue_on(worker, "claude")
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
        workspace = runtime.workspaces.workspace(harness.store.session(worker))
        done = await runtime.continue_on(worker, "copilot")
        assert seen == [False], "its process had ended"
        await runtime.workers.release(worker)  # As closing it does, in the background.
        assert await asyncio.to_thread(workspace.is_dir), "the new worker's now"
        assert runtime.workspaces.workspace(harness.store.session(done["session_id"])) == workspace
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
        assert card["text"].startswith("“Fix the login test” did not start on Codex:")
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
        with pytest.raises(ValueError, match="Codex is turned off in Settings → Coding agents"):
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
        line = f"Your Claude plan's 5-hour limit is reached; it resets at {agents.clock(resets)}."
        paused_as_by_stop(harness, runtime, worker, task, last, line)
        assert last["meta"]["full"].startswith("API Error: Request rejected (429)")
        assert json.loads(harness.store.get("agentlimit:claude"))["status"] == "rejected"
        # Go on before it resets: refused at once, saying so; still paused, nothing sent.
        with pytest.raises(Exception, match="5-hour limit is reached; it resets at"):
            await runtime.task_actions.follow_up(task, "Go on where you stopped.")
        assert runtime.tasks.get(task)["status"] == "waiting"
        assert harness.store.get(f"stopped:{task}") == agents.PLAN_STOP
    finally:
        await runtime.stop()


async def test_a_paused_task_says_its_reset_first_and_goes_on_by_itself_then_once_asked(
    tmp_path, claude_on_path, monkeypatch
):
    """Its row: "Claude limit reached · resets <time>" (Needs you). Go on at <time>: Monitoring
    until then, nothing sent before; once it resets, it goes on as the owner's Go on would."""
    resets = int(time.time()) + 3600
    info = {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": resets}
    monkeypatch.setenv("FAKE_CLAUDE_LIMIT", json.dumps(info))
    monkeypatch.setattr(task_actions, "GO_ON_MARGIN", 0.2)
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        worker, task, _ = await limited(harness, runtime, "claude", "Hit the usage limit")
        line = f"Claude limit reached · resets {agents.clock(resets)}"
        paused = {"agent": "claude", "resets_at": resets, "rate": False, "agent_name": "Claude"}
        view = runtime.task_actions.view(runtime.tasks.get(task))
        assert view["paused"] == {**paused, "line": line, "go_on_at": 0}
        assert (view["attention"], view["attention_why"]) == ("stopped", line)
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
        assert view["paused"]["line"] == "Claude rate limit reached", "no reset said: none shown"
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
        line = "Claude is refusing requests for a rate limit."
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
        line = f"Your Codex plan's 5-hour limit is reached; it resets at {agents.clock(resets)}."
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
            harness, runtime, worker, task, last, "Codex is refusing requests for a rate limit."
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
            "Your Claude plan's weekly limit is reached; it resets at"
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
