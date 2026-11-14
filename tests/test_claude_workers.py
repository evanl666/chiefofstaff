"""Claude workers in the runtime (more-agents.md 5.12): the Router's choice of Claude, a worker's
turn from task to report, its spend in its own ledger, what its gate admits, the Settings row,
and Claude turned off. Claude is the stand-in CLI (tests/fake_claude.py) on the PATH, under the
real supervisor; its version's check is the passing one (test_backend_claude.py runs the check
itself)."""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from conftest import Harness, call_tool, settle
from copilot.tools import ToolInvocation

from chiefofstaff import agents, backend_claude, prompts

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Claude workers need macOS/Linux")

PASSED = {"ran": True, "acting": "", "ask_first": "", "steering": True, "usage": True}


@pytest.fixture(autouse=True)
def checked(monkeypatch):
    """Each version's check, as it finds one that keeps the contract; counted."""
    runs = []

    async def check(self, folder):
        runs.append(self.key)
        return dict(PASSED)

    monkeypatch.setattr(backend_claude.ClaudeAgent, "check", check)
    monkeypatch.setattr(backend_claude, "LISTED", {})
    return runs


async def started(tmp_path, settings=None):
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    return harness, runtime


async def saved(runtime, part: dict) -> list[str]:
    before = agents.configured(runtime.store)
    plan = agents.planned(runtime.store, part)
    runtime.check_agents(plan, runtime.store.settings())
    return await runtime.agents_saved(before) if agents.save(runtime.store, plan) else []


def router_options(harness, runtime) -> dict:
    return runtime.options(harness.store.session(harness.router_id))


async def spawn_on(harness, runtime, **arguments):
    tool = next(t for t in router_options(harness, runtime)["tools"] if t.name == "spawn_worker")
    result = await tool.handler(
        ToolInvocation(
            session_id=harness.router_id,
            tool_call_id="t1",
            tool_name="spawn_worker",
            arguments={"why_new": "t", **arguments},
        )
    )
    return result if result.result_type == "failure" else json.loads(result.text_result_for_llm)


def appended(path: Path, text: str):
    path.write_text(path.read_text() + text)


async def until(check, seconds: float = 20):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.05)


async def claude_worker(tmp_path, instructions="Report done: Fixed it.", named=True):
    harness, runtime = await started(tmp_path)
    await saved(runtime, {"claude": {"enabled": True}})
    result = await spawn_on(
        harness,
        runtime,
        title="Fix it",
        instructions=instructions,
        **({"agent": "claude"} if named else {}),
    )
    worker = result["session_id"]
    await until(lambda: harness.store.session(worker)["status"] == "done", 30)
    await until(lambda: harness.store.agent_spent(worker) > 0, 30)
    return harness, runtime, worker, result["task_id"]


async def test_the_router_offers_claude_while_it_is_on(tmp_path, claude_on_path):
    harness, runtime = await started(tmp_path)
    try:
        before = router_options(harness, runtime)
        await saved(runtime, {"claude": {"enabled": True}})
        on = router_options(harness, runtime)
        spawn = next(t for t in on["tools"] if t.name == "spawn_worker").parameters
        assert spawn["properties"]["agent"]["enum"] == ["", "copilot", "claude"]
        added = on["system_message"]["content"].replace(
            before["system_message"]["content"].rpartition("\n</chiefofstaff_role>")[0], ""
        )
        assert added.startswith("\n- Workers can run on Claude as well as Copilot")
        await saved(runtime, {"claude": {"enabled": False}})
        assert router_options(harness, runtime)["system_message"] == before["system_message"]
    finally:
        await runtime.stop()


async def test_a_task_runs_on_claude_when_the_owner_names_it(tmp_path, claude_on_path, checked):
    harness, runtime, worker, task_id = await claude_worker(tmp_path)
    try:
        assert runtime.agent(worker) == "claude"
        assert backend_claude.dashed(worker) == worker, "Claude takes the row's own id"
        assert harness.store.get(f"copilot:{worker}") == worker, "made: resumed from now on"
        assert harness.store.get(f"alias:{worker}") is None and runtime.made(worker)
        assert harness.store.get(f"agentcheck:claude:{checked[0]}") == json.dumps(PASSED)
        assert runtime.tasks.get(task_id)["status"] == "review", "its report: for the owner"
        options = runtime.agent_options(harness.store.session(worker))
        assert options["system_message"]["content"].startswith(
            prompts.worker_on("claude", prompts.WORKER)
        )
        view = runtime.view(harness.store.session(worker))
        assert view["agent"] == "claude" and view["agent_name"] == "Claude"
        assert view["tokens"] == harness.store.agent_spent(worker) > 0
        assert view["agent_notes"] == ["Its subagents, worktrees and schedules are off for now"]
        listed = json.loads(
            (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
        )
        assert listed["agents"]["models"] == {"claude": ["default", "sonnet", "haiku"]}
    finally:
        await runtime.stop()


async def test_a_claude_workers_words_are_claudes_and_a_copilot_workers_its_own():
    for copilot in prompts.AGENT_WORDS["claude"]:
        assert prompts.WORKER.count(copilot) == 1, copilot
    said = prompts.worker_on("claude", prompts.WORKER)
    for word in ("Copilot", 'mode="async"', "detach: true"):
        assert word not in said, word
    assert "AskUserQuestion" in said and "claude mcp login" in said
    assert "run_in_background" in said and "a Claude Code session" in said
    assert prompts.worker_on("copilot", prompts.WORKER) == prompts.WORKER


async def test_a_wakeup_for_work_stopped_or_cancelled_is_not_admitted(tmp_path, claude_on_path):
    harness, runtime, worker, task_id = await claude_worker(tmp_path)
    try:
        session = runtime.states.handle(worker) or await runtime.session_for(worker)
        assert await runtime.admit_turn(session, woken=True) == ""
        # Stopped, as Stop leaves what it was on (Workers.interrupt): waiting, marked stopped.
        harness.store.set(f"stopped:{task_id}", "you")
        runtime.tasks.move(task_id, "waiting", progress="Stopped by you.")
        assert await runtime.admit_turn(session, woken=True) == (
            "What it was on is stopped or cancelled."
        )
        assert await runtime.admit_turn(session) == "", "what the owner sends still goes in"
    finally:
        await runtime.stop()


async def test_claudes_row_says_how_it_is_signed_in_and_the_plans_line(tmp_path, claude_on_path):
    harness, runtime = await started(tmp_path)
    try:
        entry = await runtime.look_for_agent("claude")
        assert entry["state"] == "2.1.322 · signed in with your Claude plan"
        assert entry["note"] == (
            "On a Claude plan, Claude workers count against your plan's usage limits, under "
            "Anthropic's terms."
        )
        assert "limit_line" not in entry
        resets = int(time.time()) + 3600
        limit = {"status": "rejected", "window": "five_hour", "resets_at": resets}
        harness.store.set("agentlimit:claude", json.dumps(limit))
        entry = await runtime.look_for_agent("claude")
        assert entry["limit_line"] == (
            f"Your Claude plan's 5-hour limit is reached; it resets at {agents.clock(resets)}."
        )
        harness.store.set("agentlimit:claude", json.dumps(limit | {"resets_at": 1}))
        assert "limit_line" not in await runtime.look_for_agent("claude"), "past its reset"
    finally:
        await runtime.stop()


async def test_a_claude_signed_in_with_a_key_has_no_plan_line(
    tmp_path, claude_on_path, monkeypatch
):
    monkeypatch.setenv(
        "FAKE_CLAUDE_AUTH",
        json.dumps({"authMethod": "api_key", "apiKeySource": "ANTHROPIC_API_KEY"}),
    )
    _, runtime = await started(tmp_path)
    try:
        entry = await runtime.look_for_agent("claude")
        assert entry["state"] == "2.1.322 · signed in with an API key" and "note" not in entry
    finally:
        await runtime.stop()


async def test_a_plan_limit_a_worker_hears_is_kept_for_settings_and_stops_new_turns(
    tmp_path, claude_on_path, monkeypatch
):
    resets = int(time.time()) + 3600
    info = {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": resets}
    monkeypatch.setenv("FAKE_CLAUDE_LIMIT", json.dumps(info))
    harness, runtime, worker, _ = await claude_worker(tmp_path)
    try:
        kept = json.loads(harness.store.get("agentlimit:claude"))
        assert kept == {"status": "rejected", "window": "five_hour", "resets_at": resets}
        why = runtime.agent_block(harness.store.session(worker))
        assert why.startswith("Your Claude plan's 5-hour limit is reached")
    finally:
        await runtime.stop()


async def test_a_turn_its_gate_did_not_admit_marks_its_version_for_good(
    tmp_path, claude_on_path, checked, monkeypatch
):
    monkeypatch.setenv("FAKE_CLAUDE_ON_ITS_OWN", "1")
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        result = await spawn_on(harness, runtime, title="Fix it", instructions="Report done: x.",
                                agent="claude")  # fmt: skip
        worker = result["session_id"]
        key = f"agentcheck:claude:{checked[0]}"
        await until(lambda: "did not admit" in (harness.store.get(key) or ""), 30)
        await settle(runtime)
        refused = json.loads(harness.store.get("agentrefused:claude"))
        assert "did not admit" in refused["acting"]
        assert "did not admit" in runtime.agent_block(harness.store.session(worker))
    finally:
        await runtime.stop()


async def test_the_check_is_kept_by_the_binary_it_checked(tmp_path, claude_on_path, checked):
    command, _ = claude_on_path
    _, runtime, _, _ = await claude_worker(tmp_path)
    try:
        assert len(checked) == 1
        await runtime.disconnect_agent("claude")
        await runtime.connect_agent("claude")
        assert len(checked) == 1, "the same binary: not checked again"
        await runtime.disconnect_agent("claude")
        await asyncio.to_thread(appended, Path(command), "# changed\n")
        await runtime.connect_agent("claude")
        assert len(checked) == 2 and checked[0] != checked[1], "a changed binary: checked again"
    finally:
        await runtime.stop()


async def test_turning_claude_off_drains_its_workers_and_processes(tmp_path, claude_on_path):
    harness, runtime = await started(tmp_path)
    try:
        plain = router_options(harness, runtime)
        await saved(runtime, {"claude": {"enabled": True}, "new_workers": "claude"})
        worker = (
            await spawn_on(
                harness,
                runtime,
                title="Serve it",
                instructions="Leave the command `sleep 300` running. Report done: Serving.",
            )
        )["session_id"]
        assert runtime.agent(worker) == "claude"
        await until(lambda: runtime.held(worker), 30)
        backend = runtime.agents["claude"].backend
        processes = backend.processes()
        assert any("sleep 300" in command for command in processes.values())
        stopped = await saved(runtime, {"claude": {"enabled": False}})
        await settle(runtime)
        assert stopped == ["Claude is turned off in Settings → Models."]
        assert runtime.connection("claude") == (None, 1) and not backend.alive()

        def gone():
            table = backend_claude.process_table()
            return not any(table.get(pid, (0, ""))[1] == c for pid, c in processes.items())

        await until(gone, 10)
        off = router_options(harness, runtime)
        assert off["system_message"]["content"] == plain["system_message"]["content"]
        (ended,) = harness.store.all("SELECT ended_at FROM commands WHERE session_id=?", (worker,))
        assert ended["ended_at"], "its command ended with its process"
    finally:
        await runtime.stop()


async def test_private_files_a_killed_chief_of_staff_left_go_at_start_and_at_turning_off(
    tmp_path, claude_on_path
):
    private = tmp_path / "data" / "agents" / "claude" / "private"
    private.mkdir(parents=True)
    (private / "999999999-0a1b-mcp.json").write_text('{"mcpServers": {}}')
    _, runtime = await started(tmp_path)
    try:
        assert list(private.iterdir()) == [], "gone at start, before any Claude worker runs"
        await saved(runtime, {"claude": {"enabled": True}, "new_workers": "claude"})
        (private / "999999999-2c3d-prompt.md").write_text("A worker.")
        await saved(runtime, {"claude": {"enabled": False}})
        assert list(private.iterdir()) == [], "and once it is turned off"
    finally:
        await runtime.stop()


@pytest.mark.parametrize("proven", [True, False])
async def test_claude_workers_run_sandboxed_only_on_a_version_whose_sandbox_held(
    tmp_path, claude_on_path, monkeypatch, proven
):
    monkeypatch.setitem(PASSED, "sandbox", "" if proven else "its sandbox let a command out")
    harness, runtime = await started(tmp_path)
    try:
        harness.store.save_settings(
            harness.store.settings().model_copy(update={"worker_sandbox": True})
        )
        await saved(runtime, {"claude": {"enabled": True}})
        await runtime.connect_agent("claude")
        row = {"id": "", "role": "worker", "slack": 0, "yolo": "", "closed_at": None}
        why = runtime.blocked_on("claude", row)
        if not proven:
            assert why == (
                "Claude can't run while workers are sandboxed: its sandbox let a command out. "
                "Turn the sandbox off in Settings → Workers to go on."
            )
            return
        assert why == ""
        result = await spawn_on(harness, runtime, title="Fix it", instructions="Report done: Ok.",
                                agent="claude")  # fmt: skip
        worker = harness.store.session(result["session_id"])
        policy = runtime.agent_options(worker)["sandbox"]
        assert worker["cwd"] in policy.readwrite and policy.allowed_hosts == ()
        await until(lambda: harness.store.session(worker["id"])["status"] == "done", 30)
    finally:
        await runtime.stop()


@pytest.mark.parametrize(
    ("sandbox", "ask_first", "yolo", "expected"),
    [
        ("", "", "", ""),
        ("its sandbox let a command out", "", "", "its sandbox let a command out"),
        ("", "it did not ask first for a command", "", "it did not ask first for a command"),
        ("", "", "on", "it acts on its own"),
    ],
)
async def test_a_worker_that_reads_slack_runs_on_claude_only_with_its_guarantees(
    tmp_path, claude_on_path, monkeypatch, sandbox, ask_first, yolo, expected
):
    monkeypatch.setitem(PASSED, "sandbox", sandbox)
    monkeypatch.setitem(PASSED, "ask_first", ask_first)
    _, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        await runtime.connect_agent("claude")
        row = {"id": "", "role": "worker", "slack": 1, "yolo": yolo, "closed_at": None}
        why = runtime.blocked_on("claude", row)
        assert why == (
            f"Workers that read Slack run on Copilot, not Claude: {expected}." if expected else ""
        )
        runtime.sandbox_host = False
        assert "this computer can't sandbox workers" in runtime.blocked_on("claude", row)
    finally:
        await runtime.stop()


async def test_a_claude_worker_continues_in_a_terminal_and_is_left_alone_there(
    tmp_path, claude_on_path
):
    from chiefofstaff.runtime import OpenElsewhereError

    harness, runtime, worker, _ = await claude_worker(tmp_path)
    try:
        row = harness.store.session(worker)
        given = await runtime.terminal_command(worker)
        assert given == {
            "command": f"cd {row['cwd']} && claude --resume {worker}",
            "agent": "claude",
        }
        assert not runtime.states.is_live(worker), "let go of first"
        assert await runtime.agents["claude"].backend.open_elsewhere() == {}, "not its own"
        # The owner runs it in a terminal: Chief of Staff writes nothing to it meanwhile.
        fake = tmp_path / "bin" / "claude"
        fake.parent.mkdir()
        fake.write_text("#!/bin/sh\nsleep 30\n")
        fake.chmod(0o755)
        terminal = await asyncio.create_subprocess_exec(str(fake), "--resume", worker)
        try:
            await until(lambda: True)
            with pytest.raises(OpenElsewhereError, match="open in another Claude window"):
                await runtime.refuse_if_open_elsewhere(row)
            assert runtime.view(harness.store.session(worker))["elsewhere"]
        finally:
            terminal.kill()
            await terminal.wait()
        await runtime.refuse_if_open_elsewhere(row)  # Closed there: its own again.
        harness.store.update_session(worker, status="working")
        with pytest.raises(ValueError, match="It is at work"):
            await runtime.terminal_command(worker)
    finally:
        await runtime.stop()
