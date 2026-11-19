"""Claude workers in the runtime (more-agents.md 5.12): the Router's choice of Claude, a worker's
turn from task to report, its spend in its own ledger, what its gate admits, the Settings row,
and Claude turned off. Claude is the stand-in CLI (tests/fake_claude.py) on the PATH, under the
real supervisor; its version's check is the passing one (test_backend_claude.py runs the check
itself)."""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from by_hand import opened_by_hand, resumed_by_hand_is_let_go_and_said_closed_once
from conftest import Harness, call_tool, settle
from copilot.tools import ToolInvocation

from chiefofstaff import agents, backend_claude, prompts
from chiefofstaff.models import Settings
from chiefofstaff.settings_guard import SettingsChangedError
from chiefofstaff.workers import SETTINGS_WAIT

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

    def over() -> bool:  # Its report comes before its turn's end, and what that turn spent.
        session = runtime.states.handle(worker)
        return not (session and session.worker.turn) and harness.store.agent_spent(worker) > 0

    await until(over, 30)
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
        assert view["agent_notes"] == ["Its Claude memory is off"]
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
        assert stopped == ["Claude is turned off in Settings → Coding agents."]
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


async def test_a_claude_worker_that_reads_slack_is_never_given_slacks_token(
    tmp_path, claude_on_path
):
    """Slack's read tools reach a Claude worker from Chief of Staff's own process (read_proxy):
    its token is in none of the options its process starts with, and only a read tool is ever
    called for it."""
    from chiefofstaff.sources.slack import SlackError

    harness, runtime, worker, _ = await claude_worker(tmp_path)
    try:
        harness.store.save_settings(Settings(slack_enabled=True))
        row = {**harness.store.session(worker), "slack": 1}
        options = runtime.agent_options(row)
        assert "xoxp-test" not in repr(options)
        assert "slack" not in options.get("mcp_servers", {}) and options["slack_tools"] == []
        proxy = await runtime.slack.read_proxy()
        assert sorted(tool["name"] for tool in proxy["tools"]) == [
            "slack_read_thread",
            "slack_search_public_and_private",
        ]
        assert "xoxp-test" not in repr(proxy["tools"])
        await proxy["call"]("slack_read_thread", {"channel_id": "C1"})
        with pytest.raises(SlackError, match="not one of Slack's read tools"):
            await proxy["call"]("slack_send_message", {"text": "hi"})
        assert harness.mcp.calls == [("slack_read_thread", {"channel_id": "C1"})]
    finally:
        await runtime.stop()


async def test_no_worker_reads_chief_of_staffs_private_files_yolo_or_not(tmp_path, claude_on_path):
    """Chief of Staff's private folder for Claude's workers (their MCP configurations): refused
    to any worker's read, write or command (a Copilot worker's too, yolo or not), and denied in
    its sandbox; with Copilot alone there is none."""
    from types import SimpleNamespace

    harness, runtime = await started(tmp_path, Settings(worker_yolo=True))
    try:
        assert runtime.private_folders() == [], "Copilot alone: nothing of agents"
        await saved(runtime, {"claude": {"enabled": True}})
        private = str(runtime.agent_folder("claude") / "private")
        assert runtime.private_folders() == [private]
        result = await spawn_on(harness, runtime, title="Fix it", instructions="Report done: ok.")
        copilot = harness.store.session(result["session_id"])
        assert runtime.on_copilot(copilot["id"]) and runtime.yolo(copilot)
        invocation = {"session_id": runtime.copilot_id(copilot["id"])}
        for request in (
            SimpleNamespace(kind="read", path=f"{private}/1-2-mcp.json"),
            SimpleNamespace(kind="write", file_name=f"{private}/x", resolved_path=None),
            SimpleNamespace(kind="shell", full_command_text="cat ~/x/agents/claude/private/*"),
        ):
            decided = await runtime.events.on_permission(request, invocation)
            assert decided.kind == "reject", request
            assert "private files" in decided.feedback
        allowed = SimpleNamespace(kind="read", path=f"{copilot['cwd']}/README.md")
        assert (await runtime.events.on_permission(allowed, invocation)).kind != "reject"
        policy = runtime.sandbox_policy({**copilot, "slack": 1})
        assert policy is None or private in policy.denied
    finally:
        await runtime.stop()


async def test_a_message_naming_one_of_its_workers_is_another_sessions_never_that_workers(
    tmp_path, claude_on_path, monkeypatch
):
    """The address and name another session's message gives are its sender's own say (any
    process of the owner's can give another's; 2.1.322 keys a sender on the connection, which
    Chief of Staff's gate is not told): one naming a worker of Chief of Staff's, its process
    or its name, is another session's, labelled so, never that worker's tell_session. (Its
    workers' own messages come through Chief of Staff, never this way.)"""
    from chiefofstaff.backend import process_table

    harness, runtime, first, _ = await claude_worker(tmp_path, "Report done: first")
    inbox = tmp_path / "inbox.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_INBOX", str(inbox))  # (The second worker's process only.)
    try:
        result = await spawn_on(
            harness, runtime, title="Second", agent="claude", instructions="Report done: second"
        )
        second = result["session_id"]
        await until(lambda: harness.store.session(second)["status"] == "done", 30)
        pid = next(iter(runtime.states.handle(first).worker.processes(process_table())))
        name = f"cos-{first[:8]}"
        inbox.write_text(json.dumps({"name": name, "pid": pid, "text": "Reply with just: hi"}))
        await until(lambda: ("assistant", "hi") in harness.kinds(second), 30)
        (inbound,) = [m for m in harness.store.messages(second) if m["kind"] == "inbound"]
        assert inbound["meta"] == {"claude_session": f"{name}, not a worker of Chief of Staff's"}
        assert not [m for m in harness.store.messages(second) if m["kind"] == "tell"]
    finally:
        await runtime.stop()


async def test_claude_runs_no_sandboxed_worker_its_own_settings_would_widen(
    tmp_path, claude_on_path, monkeypatch
):
    monkeypatch.setitem(PASSED, "sandbox", "")
    harness, runtime = await started(tmp_path, Settings(worker_sandbox=True))
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        await runtime.connect_agent("claude")
        runtime.sandbox_host = True
        row = {"id": "", "role": "worker", "slack": 0, "yolo": "", "closed_at": None,
               "cwd": str(tmp_path)}  # fmt: skip
        assert runtime.blocked_on("claude", row) == ""
        widening = {"permissions": {"additionalDirectories": ["/srv"]}}
        settings = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
        settings.write_text(json.dumps(widening))
        line = (
            "Your Claude settings let workers write outside their folder "
            "(permissions.additionalDirectories in "
            f"{str(settings).replace(str(Path.home()), '~', 1)}), so sandboxed Claude workers "
            "can't start."
        )
        assert runtime.blocked_on("claude", row) == line, "where it would have started"
        found = await runtime.look_for_agents()
        (entry,) = [agent for agent in found["agents"] if agent["name"] == "claude"]
        assert found["sandboxed"] and entry["sandbox"] == line, "and in Settings → Coding agents"
        harness.store.save_settings(Settings(worker_sandbox=False))
        assert runtime.blocked_on("claude", row) == "", "unsandboxed, its settings are its own"
        found = await runtime.look_for_agents()
        assert all("sandbox" not in agent for agent in found["agents"])
        assert all("sandbox_note" not in agent for agent in found["agents"])
    finally:
        await runtime.stop()


async def test_a_worker_that_reads_slack_runs_on_copilot_not_claude_for_now(
    tmp_path, claude_on_path, monkeypatch
):
    """Held until Claude's Slack safeguards are reviewed: whatever its version's check showed,
    a worker that reads Slack is not run on Claude (it runs on Copilot, saying why)."""
    monkeypatch.setitem(PASSED, "sandbox", "")
    _, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"claude": {"enabled": True}, "new_workers": "claude"})
        await runtime.connect_agent("claude")
        row = {"id": "", "role": "worker", "slack": 1, "yolo": "", "closed_at": None}
        assert runtime.blocked_on("claude", row) == (
            "Workers that read Slack run on Copilot, not Claude: Chief of Staff does not run "
            "them on Claude yet."
        )
        assert runtime.blocked_on("claude", {**row, "slack": 0}) == ""
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
            "command": f"cd {row['cwd']} && claude --resume {backend_claude.dashed(worker)}",
            "agent": "claude",
        }
        assert not runtime.states.is_live(worker), "let go of first"
        assert await runtime.agents["claude"].backend.open_elsewhere() == {}, "not its own"
        # The owner runs it in a terminal: Chief of Staff writes nothing to it meanwhile.
        fake = tmp_path / "bin" / "claude"
        fake.parent.mkdir()
        fake.write_text("#!/bin/sh\nsleep 30\n")
        fake.chmod(0o755)
        terminal = await asyncio.create_subprocess_exec(
            str(fake), "--resume", backend_claude.dashed(worker)
        )
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


def owners_session(fake: Path, project: Path) -> str:
    """A session of the owner's own Claude (made in their terminal), as Claude keeps it (its
    transcript) and as the stand-in resumes it."""
    from claude_agent_sdk._internal.sessions import _sanitize_path

    session = str(uuid.uuid4())
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    said = [
        {"type": "user", "uuid": first,
         "message": {"role": "user", "content": "Remember the word 'teal'."}},
        {"type": "assistant", "uuid": second,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Noted: teal."}]}},
    ]  # fmt: skip
    (fake / "home" / "sessions").mkdir(parents=True, exist_ok=True)
    (fake / "home" / "sessions" / f"{session}.json").write_text(
        json.dumps({"memory": "teal", "totals": {}, "transcript": said, "cwd": str(project)})
    )
    folder = fake / "config" / "projects" / _sanitize_path(str(project))
    folder.mkdir(parents=True)
    lines, parent = [], None
    for entry in said:
        line = {**entry, "parentUuid": parent, "entrypoint": "cli", "sessionId": session,
                "cwd": str(project), "timestamp": "2026-11-14T10:00:00Z"}  # fmt: skip
        lines.append(json.dumps(line, separators=(",", ":")))
        parent = entry["uuid"]
    (folder / f"{session}.jsonl").write_text("\n".join(lines) + "\n")
    return session


async def test_the_owners_claude_sessions_are_listed_and_one_adopted_goes_on_as_itself(
    tmp_path, claude_on_path
):
    """Parity with adopting a Copilot session: the owner's own Claude sessions are listed with
    theirs (made in their terminal; never a worker's), each shown read-only; one taken over goes
    on as itself, its conversation in its timeline, refused while a Claude process has it open
    (two would write it)."""
    fake = tmp_path / "fake-claude"
    project = tmp_path / "project"
    project.mkdir()
    session = owners_session(fake, project)
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        listed = [i for i in await runtime.workers.external(True, True) if i.get("agent")]
        assert [(i["id"], i["agent"], i["agent_name"], i["cwd"], i["open"]) for i in listed] == [
            (session, "claude", "Claude", str(project), False)
        ]
        assert listed[0]["resume"] == f"cd {project} && claude --resume {session}"
        history = await runtime.workers.history(session)
        assert [(e["kind"], e["text"]) for e in history["entries"]] == [
            ("owner", "Remember the word 'teal'."),
            ("assistant", "Noted: teal."),
        ]
        # The owner has it open in their terminal (begun there, so no command line names it).
        program = tmp_path / "bin" / "claude"
        program.parent.mkdir()
        program.write_text("#!/bin/sh\nsleep 30\n")
        program.chmod(0o755)
        terminal = await asyncio.create_subprocess_exec(str(program))
        live = fake / "config" / "sessions" / f"{terminal.pid}.json"
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_text(json.dumps({"pid": terminal.pid, "sessionId": session}))
        try:
            await until(lambda: terminal.pid in backend_claude.process_table())
            assert (await runtime.workers.history(session))["open"]
            with pytest.raises(ValueError, match="open in another Claude window"):
                await runtime.workers.adopt(session)
            assert not harness.store.find_session(session), "nothing of it made"
        finally:
            terminal.kill()
            await terminal.wait()
            live.unlink()

        row = await runtime.workers.adopt(session)
        assert row["id"] == session and runtime.agent(session) == "claude"
        assert runtime.copilot_id(session) == session, "the same session"
        kinds = harness.kinds(session)
        assert ("notice", "Its conversation in Claude before Chief of Staff managed it") in kinds
        assert ("owner", "Remember the word 'teal'.") in kinds
        assert not [i for i in await runtime.workers.external(True, True) if i.get("agent")]
        await runtime.owner_send(session, "What word did I ask you to remember?")
        await until(lambda: ("assistant", "teal") in harness.kinds(session), 30)
    finally:
        await runtime.stop()


async def test_a_worker_let_go_is_its_own_until_its_process_has_ended(
    tmp_path, claude_on_path, monkeypatch
):
    """Let go of (to continue in a terminal), its process takes a moment to end: until it has,
    its live file and its `--resume` are the worker's own, never a terminal's (the owner's next
    message would be held for a window that is not there, and miss what was done there)."""
    monkeypatch.setenv("FAKE_CLAUDE_EXIT_DELAY", "1.5")
    # Seen as Claude by its command line (python …/claude …), as the real one is.
    named = tmp_path / "named" / "claude"
    named.parent.mkdir()
    script = Path(__file__).parent / "fake_claude.py"
    named.write_text(
        f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(script)!r}, run_name='__main__')\n"
    )
    named.chmod(0o755)
    monkeypatch.setenv("PATH", f"{named.parent}:{os.environ['PATH']}")
    harness, runtime, worker, _ = await claude_worker(tmp_path)
    try:
        backend = runtime.agents["claude"].backend
        assert any(backend_claude.claude_program(c) for c in backend.processes().values())
        await runtime.terminal_command(worker)
        assert await backend.open_elsewhere() == {}
        # Not opened in a terminal after all: the owner's next message, at once, goes as usual
        # (once its old process has ended: never two writing to one conversation).
        await runtime.owner_send(worker, "Reply with just: back")
        await until(lambda: ("assistant", "back") in harness.kinds(worker), 30)
        assert not [text for kind, text in harness.kinds(worker) if "another Claude" in text]
        assert await backend.open_elsewhere() == {}, "nor what the old one left once it ended"
    finally:
        await runtime.stop()


async def test_what_a_claude_worker_did_in_a_terminal_shows_before_its_next_message(
    tmp_path, claude_on_path
):
    """Parity with a Copilot worker's catch-up: continued in a terminal, then messaged here
    again, what it said and did there comes first in its timeline (from Claude's transcript)."""
    harness, runtime, worker, _ = await claude_worker(tmp_path)
    try:
        await runtime.terminal_command(worker)
        # The owner's turn in the terminal, as Claude appends it to the transcript: once Chief
        # of Staff's process has ended (the stand-in rewrites it as it ends).
        await until(lambda: not runtime.agents["claude"].backend.processes(), 15)
        path = backend_claude.transcript_of(backend_claude.dashed(worker))

        def written() -> bool:
            text = path.read_text()
            try:
                return text.endswith("\n") and bool(json.loads(text.splitlines()[-1]))
            except ValueError:
                return False

        await until(written, 10)
        lines = path.read_text().splitlines()
        parent = json.loads(lines[-1])["uuid"]
        now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 2))
        common = {"sessionId": backend_claude.dashed(worker), "entrypoint": "cli", "timestamp": now}
        asked, answered = str(uuid.uuid4()), str(uuid.uuid4())
        lines += [
            json.dumps({"type": "user", "uuid": asked, "parentUuid": parent, **common,
                        "message": {"role": "user", "content": "Also tidy the README"}}),
            json.dumps({"type": "assistant", "uuid": answered, "parentUuid": asked, **common,
                        "message": {"role": "assistant", "content": [
                            {"type": "tool_use", "id": "t9", "name": "Edit",
                             "input": {"file_path": "README.md"}},
                            {"type": "text", "text": "Tidied the README."}]}}),
        ]  # fmt: skip
        path.write_text("\n".join(lines) + "\n")
        await runtime.owner_send(worker, "Reply with just: thanks")
        await until(lambda: ("assistant", "thanks") in harness.kinds(worker), 30)
        kinds = harness.kinds(worker)
        meanwhile = kinds.index(("notice", "Meanwhile, in a terminal"))
        assert kinds[meanwhile + 1 : meanwhile + 4] == [
            ("owner", "Also tidy the README"),
            ("tool", "edit README.md"),
            ("assistant", "Tidied the README."),
        ]
        assert meanwhile < kinds.index(("owner", "Reply with just: thanks"))
    finally:
        await runtime.stop()


async def test_a_copilot_session_a_claude_worker_made_is_its_not_the_owners(
    tmp_path, claude_on_path, monkeypatch
):
    """The owner's Other sessions leave out a Copilot session a worker's `copilot -p` made: a
    Claude worker's commands are kept as a Copilot worker's are, so its `copilot -p` is found."""
    from chiefofstaff import provenance

    stand_in = tmp_path / "copilot-bin" / "copilot"  # Never the owner's Copilot.
    stand_in.parent.mkdir()
    stand_in.write_text("#!/bin/sh\necho made\n")
    stand_in.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stand_in.parent}:{os.environ['PATH']}")
    started_at = time.time()
    harness, runtime, worker, _ = await claude_worker(
        tmp_path, "Run the shell command `copilot -p 'label it' -s`. Report done: labelled"
    )
    try:
        ran = harness.store.all("SELECT * FROM commands WHERE session_id=?", (worker,))
        assert [(row["command"], bool(row["ended_at"])) for row in ran] == [
            ("copilot -p 'label it' -s", True)
        ]
        found = provenance.windows(
            harness.store, started_at, time.time(), runtime.provenance.folder_of,
            [(started_at - 1, time.time() + 1)],
        )  # fmt: skip
        assert [(window.by, window.copilot) for window in found] == [(worker, True)]
    finally:
        await runtime.stop()


# Claude's own capabilities, tied into Chief of Staff (more-agents.md 5.12.4)


async def test_a_worktree_a_claude_worker_makes_is_one_of_its_checkouts(tmp_path, claude_on_path):
    """Claude makes a worktree in its repository's main checkout (2.1.322), the owner's own for
    a worker in a worktree Chief of Staff made: refused there, it makes one in its own folder
    and enters it. Its receipts, Storage and folders count it as they do a checkout Chief of
    Staff made; removed by Claude, it is no longer one."""
    from test_round7_split import published

    app = published(tmp_path, "app")
    harness, runtime = await started(tmp_path, Settings(projects=[str(app)]))
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        result = await spawn_on(
            harness, runtime, title="Feature", agent="claude", project=str(app), isolate=True,
            instructions="Enter a worktree named feat. Report done: tried",
        )  # fmt: skip
        worker = result["session_id"]
        await until(lambda: ("tool", "EnterWorktree") in harness.kinds(worker), 30)
        await until(lambda: runtime.states[worker].busy is False, 30)
        assert not (app / ".claude").exists(), "refused: nothing made in the owner's checkout"
        assert len(json.loads(harness.store.session(worker)["repos"])) == 1
        await runtime.owner_send(
            worker,
            "Run the shell command `git worktree add -q -b worktree-feat .claude/worktrees/feat`. "
            "Enter the worktree at .claude/worktrees/feat. Report done: made",
        )
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        tree = str(Path(harness.store.session(worker)["cwd"]) / ".claude" / "worktrees" / "feat")
        made = f"Made the worktree {tree} on worktree-feat: one of its checkouts now."
        await until(lambda: ("notice", made) in harness.kinds(worker))
        row = harness.store.session(worker)
        found = [c for c in json.loads(row["repos"]) if c["path"] == tree]
        assert [(c["branch"], c["base"]) for c in found] == [
            ("worktree-feat", "a worktree it made itself")
        ]
        assert tree in runtime.workspaces.writes(row), "a folder of its own to write"
        await runtime.owner_send(worker, "Leave the worktree and remove it. Report done: gone")
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        await until(
            lambda: (
                tree not in [c["path"] for c in json.loads(harness.store.session(worker)["repos"])]
            )
        )
    finally:
        await runtime.stop()


async def test_a_claude_worker_resumed_by_hand_is_let_go_and_its_closing_said_once(
    tmp_path, claude_on_path
):
    harness, runtime, worker, _ = await claude_worker(tmp_path)
    try:
        assert runtime.states.is_live(worker)
        terminal = await opened_by_hand(
            tmp_path, "claude", "--resume", backend_claude.dashed(worker)
        )
        try:
            await resumed_by_hand_is_let_go_and_said_closed_once(harness, runtime, worker, terminal)
        finally:
            if terminal.returncode is None:
                terminal.kill()
                await terminal.wait()
    finally:
        await runtime.stop()


async def test_a_worktree_is_its_only_where_it_resolves_inside_its_folder(tmp_path, claude_on_path):
    """A worktree Claude names under the worker's folder (.claude/worktrees/…) that resolves
    elsewhere (a link to another repository's worktree): neither one of its checkouts nor a
    folder a subagent may work in; one that resolves there, both."""
    from test_round7_split import published

    app, other = published(tmp_path, "app"), published(tmp_path, "other")
    harness, runtime = await started(tmp_path, Settings(projects=[str(app)]))
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        result = await spawn_on(
            harness, runtime, title="Feature", agent="claude", project=str(app), isolate=True,
            instructions="Report done: ready",
        )  # fmt: skip
        worker = result["session_id"]
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        cwd = Path(harness.store.session(worker)["cwd"])
        trees = cwd / ".claude" / "worktrees"
        trees.mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        git = ["git", "worktree", "add", "-q", "-b"]
        await asyncio.to_thread(subprocess.run, [*git, "w1", str(elsewhere)], cwd=other, check=True)
        (trees / "away").symlink_to(elsewhere, target_is_directory=True)
        await asyncio.to_thread(
            subprocess.run, [*git, "w2", str(trees / "good")], cwd=cwd, check=True
        )
        before = harness.store.session(worker)["repos"]
        await runtime.agent_worktree(worker, str(trees / "away"), removed=False)
        assert harness.store.session(worker)["repos"] == before
        assert any("not one Chief of Staff can keep" in t for _, t in harness.kinds(worker))
        await runtime.agent_worktree(worker, str(trees / "good"), removed=False)
        repos = json.loads(harness.store.session(worker)["repos"])
        assert [c["branch"] for c in repos if c["path"].endswith("good")] == ["w2"]
        handle = runtime.states.handle(worker) or await runtime.session_for(worker)
        claude = handle.worker
        for name, folder in (("a1", trees / "away"), ("a2", trees / "good")):
            await claude.subagent_start({"agent_id": name, "cwd": str(folder)}, None, None)
        assert claude.subagents["a1"].get("refused") and not claude.subagents["a2"].get("refused")
    finally:
        await runtime.stop()


async def test_a_claude_workers_schedule_is_kept_holds_its_process_and_fires_as_its_task_run(
    tmp_path, claude_on_path, monkeypatch
):
    from chiefofstaff import workers as workers_module

    monkeypatch.setenv("FAKE_CLAUDE_FIRE_SECONDS", "1.5")
    harness, runtime, worker, _ = await claude_worker(
        tmp_path, "Report done: set. Schedule 'Run the shell command `echo tick`' every minute."
    )
    try:
        # (It reports first, then schedules: its report alone does not say the schedule is made.)
        await until(lambda: harness.store.get(f"agentschedules:{worker}"), 10)
        kept = json.loads(harness.store.get(f"agentschedules:{worker}") or "[]")
        assert [(e["prompt"], e["recurring"]) for e in kept] == [
            ("Run the shell command `echo tick`", True)
        ]
        assert any(t.startswith("Scheduled (Every minute)") for k, t in harness.kinds(worker))
        later = time.time() + workers_module.RELEASE_IDLE + 1
        assert await runtime.workers.release_idle(later) == [], "a schedule needs its process"
        fired = ("notice", "Its schedule fired (Every minute).")
        await until(lambda: fired in harness.kinds(worker), 30)
        await until(lambda: harness.kinds(worker)[-2:] == [("tool", "bash echo tick"),
                                                          ("assistant", "done")])  # fmt: skip
    finally:
        await runtime.stop()


async def test_a_claude_workers_schedule_shows_it_working_and_stops_with_stop_and_cancel(
    tmp_path, claude_on_path, monkeypatch
):
    """A firing is a turn at work (its row says so); after the owner's Stop no firing runs until
    they tell it to go on; once its task is cancelled none runs, and nothing keeps its process
    (it is let go of when idle, as any worker)."""
    from chiefofstaff import workers as workers_module

    monkeypatch.setenv("FAKE_CLAUDE_FIRE_SECONDS", "1")
    harness, runtime, worker, task = await claude_worker(
        tmp_path, "Report done: set. Schedule 'Run the shell command `echo tick`' every minute."
    )
    statuses = []
    update = runtime.update_session

    def record(session_id, **fields):
        if session_id == worker and "status" in fields:
            statuses.append(fields["status"])
        return update(session_id, **fields)

    monkeypatch.setattr(runtime, "update_session", record)
    try:
        await until(lambda: harness.store.get(f"agentschedules:{worker}"), 10)
        fired = ("notice", "Its schedule fired (Every minute).")
        await until(lambda: fired in harness.kinds(worker), 30)
        await until(lambda: "working" in statuses, 10)
        await runtime.workers.interrupt(worker)
        held = len([k for k in harness.kinds(worker) if k == fired])
        await until(lambda: any("did not run" in t for _, t in harness.kinds(worker)), 15)
        assert len([k for k in harness.kinds(worker) if k == fired]) == held
        with pytest.raises(ValueError, match="cancel again to set it aside"):
            await runtime.task_actions.cancel(task)
        await runtime.task_actions.cancel(task)  # Set aside: nothing of it open now.
        assert harness.store.get(f"agentschedules:{worker}") is None
        ran = len([k for k in harness.kinds(worker) if k == fired])
        await asyncio.sleep(2.5)  # (Firings every second: refused, without a word.)
        assert len([k for k in harness.kinds(worker) if k == fired]) == ran
        later = time.time() + workers_module.RELEASE_IDLE + 1
        await until(lambda: runtime.store.session(worker)["status"] != "working", 15)
        assert await runtime.workers.release_idle(later) == [worker], "nothing keeps it now"
    finally:
        await runtime.stop()


async def test_a_claude_worker_with_schedules_is_not_handed_to_a_terminal_and_none_is_durable(
    tmp_path, claude_on_path, monkeypatch
):
    """Claude brings a worker's schedules back with its conversation (2.1.322, also one it calls
    session-only): in a terminal they would run outside Chief of Staff, so it is not handed
    over with any. A durable one (written into its folder) is never made."""
    monkeypatch.setenv("FAKE_CLAUDE_FIRE_SECONDS", "600")
    harness, runtime, worker, _ = await claude_worker(
        tmp_path, "Report done: set. Schedule 'Run the shell command `echo tick`' every minute."
    )
    try:
        await until(lambda: harness.store.get(f"agentschedules:{worker}"), 10)
        with pytest.raises(ValueError, match=r"It has schedules \(Every minute\) that would go"):
            await runtime.terminal_command(worker)
        assert runtime.states.is_live(worker), "not let go of"
        await runtime.owner_send(
            worker, "Schedule 'Run the shell command `echo later`' every minute durably"
        )
        await until(lambda: harness.kinds(worker)[-1][0] == "assistant", 30)
        assert ("tool", "CronCreate") in harness.kinds(worker)[-3:], "asked for, refused"
        kept = json.loads(harness.store.get(f"agentschedules:{worker}"))
        assert [entry["prompt"] for entry in kept] == ["Run the shell command `echo tick`"]
    finally:
        await runtime.stop()


async def claude_in_worktree(tmp_path, settings=None, instructions="Report done: made"):
    """A Claude worker in a worktree Chief of Staff made of a project, done with its task."""
    from test_round7_split import published

    app = published(tmp_path, "app")
    harness, runtime = await started(
        tmp_path, (settings or Settings()).model_copy(update={"projects": [str(app)]})
    )
    await saved(runtime, {"claude": {"enabled": True}})
    result = await spawn_on(
        harness, runtime, title="Feature", agent="claude", project=str(app), isolate=True,
        instructions=instructions,
    )  # fmt: skip
    worker = result["session_id"]
    await until(lambda: harness.store.session(worker)["status"] == "done", 30)
    return harness, runtime, worker, Path(harness.store.session(worker)["cwd"])


async def test_claude_settings_changed_in_a_workers_folder_wait_for_the_owner_to_use_them(
    tmp_path, claude_on_path
):
    """Claude loads a folder's own settings (hooks, apiKeyHelper, MCP servers, subagents) as
    its process starts there, a worker's resumed or the owner's own in a terminal: one a
    command wrote (its own folder's are not held by its sandbox) waits, as a Copilot worker's
    does, until the owner clicks Use these settings on the line its timeline shows; nor is it
    handed to a terminal meanwhile."""
    harness, runtime, worker, cwd = await claude_in_worktree(tmp_path)
    try:
        (cwd / ".mcp.json").write_text('{"mcpServers": {"x": {"command": "true"}}}')
        reason = "Claude settings in this folder changed (added .mcp.json)"
        handover = (
            f"{reason}: Claude would load them in your terminal. Look at them, then Use these "
            "settings here first."
        )
        with pytest.raises(ValueError, match=re.escape(handover)):
            await runtime.terminal_command(worker)
        (task,) = runtime.tasks.for_session(worker)
        await runtime.task_actions.follow_up(
            task["id"], "Run the shell command `echo next`. Report done: next"
        )
        line = f"{reason}; Chief of Staff won't start the worker there until you say to use them."
        await until(lambda: ("notice", line) in harness.kinds(worker), 10)
        assert harness.store.session(worker)["activity"] == SETTINGS_WAIT
        assert not runtime.states.is_live(worker), "let go of: its next process would load them"
        (said,) = [m for m in harness.store.messages(worker) if m["text"] == line]
        digest = said["meta"]["agent_settings"]["digest"]
        # One card under Needs you, with Use these settings, as a Copilot worker's (0.1.48).
        shown = runtime.task_actions.view(runtime.tasks.get(task["id"]))
        assert (shown["attention"], shown["settings"]["digest"]) == ("settings", digest)
        with pytest.raises(SettingsChangedError, match=re.escape(reason)):
            await runtime.session_for(worker)
        assert await runtime.accept_settings(worker, digest) == (
            "You let this worker use these settings (added .mcp.json)."
        )
        await until(lambda: ("report", "next") in harness.kinds(worker), 30)
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        assert (await runtime.terminal_command(worker))["agent"] == "claude"
    finally:
        await runtime.stop()


async def test_a_claude_worker_whose_subagent_settings_changed_mid_turn_is_stopped_first(
    tmp_path, claude_on_path
):
    """A subagent's definition (.claude/agents) loads as the subagent starts: its folder's
    settings are looked at before Claude runs one, and a change stops the worker there."""
    harness, runtime, worker, cwd = await claude_in_worktree(tmp_path)
    try:
        (cwd / ".claude" / "agents").mkdir(parents=True)
        (cwd / ".claude" / "agents" / "helper.md").write_text("---\nname: helper\n---\nHi\n")
        row = harness.store.session(worker)
        why = await runtime.agent_subagent(worker, "Agent", {"subagent_type": "helper"})
        assert why.startswith("Claude settings in this folder changed (added .claude/agents")
        await until(lambda: worker in runtime.settings_held, 10)
        assert row["id"] == worker
    finally:
        await runtime.stop()


async def test_a_claude_workers_changed_mcp_server_is_off_for_it_not_a_wait(
    tmp_path, claude_on_path, monkeypatch
):
    """As for a Copilot worker: what its folder's MCP server runs (a repository developing its
    own server) stays the worker's to build and edit; changed, that server is off for it from
    its next process (Claude's disabledMcpjsonServers), said once, never a wait."""
    from test_round7_split import published

    starts = tmp_path / "starts.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_STARTS", str(starts))
    app = published(tmp_path, "app")
    (app / "tools").mkdir()
    (app / "tools" / "serve.py").write_text("print('serving')\n")
    servers = {"mcpServers": {"x": {"command": "python3", "args": ["tools/serve.py"]}}}
    (app / ".mcp.json").write_text(json.dumps(servers))
    named = ["-c", "user.email=t@t", "-c", "user.name=t"]  # (Linux runners have none.)
    for arguments in (
        ["add", "-A"],
        [*named, "commit", "-qm", "server"],
        ["push", "-q", "origin", "main"],
    ):
        await asyncio.to_thread(subprocess.run, ["git", *arguments], cwd=app, check=True)
    harness, runtime = await started(tmp_path, Settings(projects=[str(app)]))
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        result = await spawn_on(
            harness, runtime, title="Server", agent="claude", project=str(app), isolate=True,
            instructions="Report done: made",
        )  # fmt: skip
        worker = result["session_id"]
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        cwd = Path(harness.store.session(worker)["cwd"])
        (cwd / "tools" / "serve.py").write_text("print('serving better')\n")
        await runtime.owner_send(worker, "Report done: again")
        await until(lambda: ("report", "again") in harness.kinds(worker), 30)
        opened = [json.loads(line) for line in starts.read_text().splitlines()]
        assert [entry["disabled_servers"] for entry in opened] == [None, ["x"]]
        line = "Its MCP server x changed in this worker's folder, so it runs without it."
        assert ("notice", line) in harness.kinds(worker)
        assert worker not in runtime.settings_held, "never a wait"
    finally:
        await runtime.stop()


async def test_a_replaced_git_folder_keeps_a_claude_worker_from_a_terminal(
    tmp_path, claude_on_path
):
    """Its worktree's folder in the repository replaced since Chief of Staff first saw it (git
    there could run what the worker put in its place): not handed to the owner's terminal,
    whose Claude would run git there; said with Use this .git."""
    harness, runtime, worker, _ = await claude_in_worktree(tmp_path)
    try:
        await runtime.detach(worker)
        (checkout,) = runtime.workspaces.checkouts(harness.store.session(worker))
        admin = Path(checkout["admin"])
        shutil.copytree(admin, tmp_path / "swapped")
        shutil.rmtree(admin)
        (tmp_path / "swapped").rename(admin)
        with pytest.raises(ValueError, match=r"was replaced since Chief of Staff first saw it"):
            await runtime.terminal_command(worker)
    finally:
        await runtime.stop()


async def test_a_sandboxed_claude_workers_commands_use_its_own_caches(
    tmp_path, claude_on_path, monkeypatch
):
    """Its tools pointed at workers' own caches, as a Copilot worker's are: the same init
    script, which Claude sources before each of its commands (CLAUDE_ENV_FILE)."""
    monkeypatch.setitem(PASSED, "sandbox", "")
    starts = tmp_path / "starts.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_STARTS", str(starts))
    harness, runtime = await started(tmp_path, Settings(worker_sandbox=True))
    runtime.sandbox_host = True
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        await runtime.connect_agent("claude")  # (Its version's check: its sandbox holds.)
        result = await spawn_on(
            harness, runtime, title="Cache", agent="claude",
            instructions="Run the shell command `echo $npm_config_cache > seen`. Report done: x",
        )  # fmt: skip
        worker = result["session_id"]
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        (start,) = [json.loads(line) for line in starts.read_text().splitlines()]
        script = Path(start["env_file"])
        assert script == runtime.cache_scripts / worker / "init.sh"
        seen = Path(harness.store.session(worker)["cwd"]) / "seen"
        assert seen.read_text().strip() == str(runtime.cache_tier / "npm")
        # Nor does it run hooks, which Claude runs outside its sandbox: its tooltip and Claude's
        # row in Settings say so.
        assert start["hooks_off"]
        notes = runtime.view(harness.store.session(worker))["agent_notes"]
        assert "Your Claude hooks are off for it" in notes
        found = await runtime.look_for_agents()
        (entry,) = [agent for agent in found["agents"] if agent["name"] == "claude"]
        assert entry["sandbox_note"] == "Sandboxed Claude workers don't run hooks."
    finally:
        await runtime.stop()


async def test_a_claude_worker_resuming_is_never_another_claude_window(
    tmp_path, claude_on_path, monkeypatch
):
    """Its own process resuming its session (`claude --resume <id>`, its sessions/ file) is
    Chief of Staff's from its start, before it is ready: never "open in another Claude window"
    (that would show it so, keep it from being let go of, and refuse what it is sent)."""
    from chiefofstaff.backend import process_table

    harness, runtime, worker, _ = await claude_worker(tmp_path, "Report done: ready")
    try:
        await runtime.detach(worker)
        monkeypatch.setenv("FAKE_CLAUDE_INIT_DELAY", "3")
        opening = asyncio.ensure_future(runtime.session_for(worker))
        session = backend_claude.dashed(runtime.copilot_id(worker))

        def resuming() -> bool:  # (Its process resuming: `claude … --resume <id>`.)
            return any(
                backend_claude.claude_program(command)
                and "--resume" in command
                and session in command
                for _, command in process_table().values()
            )

        await until(resuming, 15)
        backend, _ = runtime.connection("claude")
        assert not {session, worker.replace("-", "")} & set(await backend.open_elsewhere())
        await runtime.workers.refresh_elsewhere()
        assert not runtime.is_elsewhere(harness.store.session(worker))
        assert await opening
    finally:
        await runtime.stop()


async def set_aside(runtime, task: str):
    """A finished task the owner cancels: the first cancel asks again, the second sets it aside."""
    with pytest.raises(ValueError, match="cancel again to set it aside"):
        await runtime.task_actions.cancel(task)
    await runtime.task_actions.cancel(task)


def kept_crons(worker: str) -> dict:
    """The crons the stand-in keeps with a worker's conversation: what a resume brings back."""
    home = Path(os.environ["FAKE_CLAUDE_HOME"])
    return json.loads((home / "sessions" / f"{worker}.json").read_text()).get("crons") or {}


@pytest.mark.parametrize("live", [True, False], ids=["live", "let-go-of"])
async def test_schedules_that_end_with_a_task_are_deleted_in_claude_before_a_terminal_gets_it(
    tmp_path, claude_on_path, monkeypatch, live
):
    """Claude brings a session's crons back with its conversation unless a CronDelete named
    them (2.1.322), so a resume, or `claude --resume` in the owner's terminal, would run them
    outside Chief of Staff. Once its task is set aside they are deleted in Claude itself, in its
    process (opened for that, and let go of after, where none runs); only then is it handed to a
    terminal."""
    from chiefofstaff import workers as workers_module

    monkeypatch.setenv("FAKE_CLAUDE_FIRE_SECONDS", "600")
    harness, runtime, worker, task = await claude_worker(
        tmp_path, "Report done: set. Schedule 'Run the shell command `echo tick`' every minute."
    )
    try:
        await until(lambda: harness.store.get(f"agentschedules:{worker}"), 10)
        await until(lambda: len(kept_crons(worker)) == 1, 10)
        if not live:
            later = time.time() + workers_module.RELEASE_IDLE + 1
            runtime.store.set(f"scheduleshold:{worker}", "1")  # (Else they keep its process.)
            assert await runtime.workers.release_idle(later) == [worker]
            runtime.store.delete(f"scheduleshold:{worker}")
        await set_aside(runtime, task)
        deleted = ("notice", "Its schedules are deleted in Claude: none comes back.")
        await until(lambda: deleted in harness.kinds(worker), 30)
        assert kept_crons(worker) == {}, "nothing for a resume to bring back"
        assert not harness.store.get(f"schedulesdelete:{worker}")
        assert runtime.states.is_live(worker) is live, "opened for this only, then let go of"
        assert (await runtime.terminal_command(worker))["agent"] == "claude"
    finally:
        await runtime.stop()


async def test_schedules_claude_did_not_delete_keep_a_worker_from_a_terminal(
    tmp_path, claude_on_path, monkeypatch
):
    monkeypatch.setenv("FAKE_CLAUDE_FIRE_SECONDS", "600")
    monkeypatch.setenv("FAKE_CLAUDE_KEEPS_SCHEDULES", "1")
    harness, runtime, worker, task = await claude_worker(
        tmp_path, "Report done: set. Schedule 'Run the shell command `echo tick`' every minute."
    )
    try:
        await until(lambda: harness.store.get(f"agentschedules:{worker}"), 10)
        await set_aside(runtime, task)
        await until(
            lambda: any(
                t.startswith("Its schedules are not deleted") for _, t in harness.kinds(worker)
            ),
            30,
        )
        assert any("Claude still lists 1 of them." in t for _, t in harness.kinds(worker))
        assert len(kept_crons(worker)) == 1
        # Handed to a terminal: why not, and what lets it.
        with pytest.raises(ValueError, match="Deleting them there failed") as refused:
            await runtime.terminal_command(worker)
        assert str(refused.value) == (
            "Its schedules ended with its task but are still in its Claude conversation, where "
            "they would run in your terminal. Deleting them there failed: Claude still lists 1 "
            "of them. Chief of Staff asks it again as you try again; or tell it in its chat to "
            "delete its schedules (CronDelete), then try again."
        )
    finally:
        await runtime.stop()


async def test_a_claude_workers_message_to_another_worker_is_its_tell_session(
    tmp_path, claude_on_path
):
    harness, runtime, first, _ = await claude_worker(tmp_path, "Report done: first")
    try:
        result = await spawn_on(
            harness, runtime, title="Second", agent="claude",
            instructions=f"Message cos-{first[:8]}: 'the API moved'. Report done: told",
        )  # fmt: skip
        second = result["session_id"]
        await until(lambda: harness.store.session(second)["status"] == "done", 30)
        # (Its report comes first, then its message: the report does not say it is sent.)
        await until(lambda: any(m["kind"] == "tell" for m in harness.store.messages(second)))
        told = [m for m in harness.store.messages(second) if m["kind"] == "tell"]
        assert [(m["text"], m["meta"]["target"]) for m in told] == [("the API moved", first)]
        router = [m["text"] for m in harness.store.messages(harness.router_id)]
        assert not any("the API moved" in text and "Claude session" in text for text in router)
    finally:
        await runtime.stop()


async def test_another_claude_sessions_message_to_a_worker_is_shown_as_its_and_admitted(
    tmp_path, claude_on_path, monkeypatch
):
    inbox = tmp_path / "inbox.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_INBOX", str(inbox))
    harness, runtime, worker, _ = await claude_worker(tmp_path, "Report done: ready")
    try:
        inbox.write_text(json.dumps({"name": "alice", "pid": 4242, "text": "Reply with just: hi"}))
        await until(lambda: ("assistant", "hi") in harness.kinds(worker), 30)
        (inbound,) = [m for m in harness.store.messages(worker) if m["kind"] == "inbound"]
        assert (inbound["text"], inbound["meta"]) == (
            "Reply with just: hi",
            {"claude_session": "alice"},
        )
    finally:
        await runtime.stop()


async def test_a_claude_workers_notification_is_its_own_in_chief_of_staff(tmp_path, claude_on_path):
    harness, runtime, worker, _ = await claude_worker(
        tmp_path, "Notify me: 'Build is green'. Report done: notified"
    )
    try:
        await until(lambda: ("notice", "Notified you: Build is green") in harness.kinds(worker))
    finally:
        await runtime.stop()


async def test_a_claude_workers_worktrees_go_with_its_checkouts_when_it_closes(
    tmp_path, claude_on_path
):
    from test_round7_split import published

    app = published(tmp_path, "app")
    harness, runtime = await started(tmp_path, Settings(projects=[str(app)]))
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        result = await spawn_on(
            harness, runtime, title="Feature", agent="claude", project=str(app), isolate=True,
            instructions="Run the shell command `git worktree add -q -b worktree-feat "
            ".claude/worktrees/feat`. Enter the worktree at .claude/worktrees/feat. Report "
            "done: made",
        )  # fmt: skip
        worker = result["session_id"]
        cwd = Path(harness.store.session(worker)["cwd"])
        tree = cwd / ".claude" / "worktrees" / "feat"
        await until(lambda: str(tree) in harness.store.session(worker)["repos"], 30)
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        await call_tool(runtime, "close_worker", harness.router_id, session_id=worker)
        await until(lambda: not tree.exists() and not cwd.exists(), 30)

        def listed() -> str:
            return subprocess.run(
                ["git", "worktree", "list"], cwd=app, capture_output=True, text=True, check=True
            ).stdout

        # Out of its place first, then deleted, and git told (0.1.46's removal, in the trash).
        await until(lambda: "feat" not in listed() and str(cwd) not in listed(), 30)
    finally:
        await runtime.stop()
