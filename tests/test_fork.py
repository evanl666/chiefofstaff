"""A new task for a busy worker goes to a second worker that starts at once with its context
(forks.py, docs/design/worker-fork.md); a free worker keeps it, as before."""

import asyncio
import json
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import Harness, call_tool, settle
from copilot.tools import ToolInvocation
from test_repos import commit, published

from chiefofstaff import prompts
from chiefofstaff.backend import fork_point
from chiefofstaff.models import Settings
from chiefofstaff.scripted import Say, ScriptedBackend, Wait


async def spawn(runtime, router, title, **extra):
    extra.setdefault("instructions", title)
    extra.setdefault("why_new", "test")
    result = await call_tool(runtime, "spawn_worker", router, title=title, **extra)
    return json.loads(result.text_result_for_llm)


async def at_work(runtime, harness, worker):
    """Its turn has begun (it holds a slot), as Copilot shows it."""
    session = harness.session(worker)
    session.emit("user.message", content=session.sent[-1])
    session.emit("assistant.turn_start")
    await settle(runtime)
    runtime.update_session(worker, status="working")


async def new_task(runtime, harness, worker, title, text, **extra):
    """message_session as the Router has it while the switch is on (with same_worker)."""
    extra.setdefault("why_new", "test")
    arguments = {"session_id": worker, "task": "new", "title": title, "text": text, **extra}
    result = await runtime.toolset.message_forks.handler(
        ToolInvocation(session_id=harness.router_id, tool_call_id="t1",
                       tool_name="message_session", arguments=arguments)
    )  # fmt: skip
    text = result if isinstance(result, str) else result.text_result_for_llm
    return json.loads(text) if text.startswith("{") else text


async def busy(runtime, harness, title="Fix login", **extra):
    """A worker at work with two tasks open: busy, as the owner put it ("many tasks")."""
    worker = (await spawn(runtime, harness.router_id, title, **extra))["session_id"]
    await at_work(runtime, harness, worker)
    told = await new_task(runtime, harness, worker, "Fix logout", "Fix logout too.", earlier="keep")
    assert told.startswith("Delivered"), "one task open: not busy, it gets the second"
    return worker


def branch_of(path: str) -> str:
    found = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path,
                           capture_output=True, text=True, check=True)  # fmt: skip
    return found.stdout.strip()


def tasks_of(harness, worker):
    return [t["title"] for t in harness.store.all(
        "SELECT title FROM tasks WHERE session_id=? ORDER BY created_at", (worker,))]  # fmt: skip


async def test_a_busy_workers_new_task_goes_to_a_second_worker_with_its_context(harness):
    runtime = await harness.connect()
    worker = await busy(runtime, harness)
    before = list(harness.session(worker).sent)
    result = await new_task(runtime, harness, worker, "Export CSV", "Add a CSV export.")
    second = result["session_id"]
    await settle(runtime)
    assert second != worker and harness.client.forks == [(worker, second)]
    assert "second worker" in result["note"] and worker[:8] in result["note"]
    assert tasks_of(harness, worker) == ["Fix login", "Fix logout"], "its own work stays"
    assert tasks_of(harness, second) == ["Export CSV"]
    assert harness.session(worker).sent == before, "the busy worker is not disturbed"
    assert second in harness.client.resumed and second not in harness.client.created, (
        "it runs on the fork of the busy worker's session: its conversation so far"
    )
    sent = harness.session(second).sent
    assert sent[: len(before)] == before and "Add a CSV export." in sent[-1]
    assert "You are a second worker" in sent[-1] and "“Fix logout”" in sent[-1], "the brief"
    row = harness.store.session(second)
    assert harness.store.session(worker)["title"] == "Fix login" and row["title"] == "Export CSV"
    name = runtime.display_title(harness.store.session(worker))
    notices = [text for kind, text in harness.kinds(harness.router_id) if kind == "notice"]
    line = f"Started a second worker for “Export CSV” (context from “{name}”)"
    assert notices[-1] == f"{line} · new because test", "why it is new, on hover"
    assert ("notice", "Its context went to a second worker for “Export CSV”") in harness.kinds(
        worker
    )
    assert harness.kinds(second)[0] == ("notice", f"Started with the context of “{name}”")
    assert runtime.view(row)["fork_of"] == {"id": worker, "title": name}
    assert "fork_of" not in runtime.view(harness.store.session(worker))


async def test_a_task_queued_to_finish_after_long_work_goes_to_a_second_worker(harness):
    """QA E87: the Router queued separate work behind a worker's long task (earlier=finish),
    and it waited, then failed with it. Busy, its new task goes to a second worker."""
    runtime = await harness.connect()
    worker = await busy(runtime, harness)
    result = await new_task(
        runtime, harness, worker, "Plan the rollout", "Plan the rollout.", earlier="finish"
    )
    assert result["session_id"] != worker and tasks_of(harness, worker) == [
        "Fix login",
        "Fix logout",
    ]


async def test_the_router_sees_how_loaded_each_worker_is(tmp_path):
    """QA E87: nothing showed the Router how loaded a worker was. Its load is in list_sessions,
    find_sessions and the related workers spawn_worker lists; none of it while the switch is
    off (as before)."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    worker = await busy(runtime, harness, project="", instructions="Fix the login form.")
    runtime.states[worker].busy_since = time.monotonic() - 3 * 60
    listed = json.loads(
        (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
    )
    entry = next(item for item in listed["sessions"] if item["session_id"] == worker)
    assert entry["load"] == {
        "in_turn": True, "turn_minutes": 3, "open_tasks": 2, "messages_waiting": 1,
        "busy": "2 open tasks",
    }, entry["load"]  # fmt: skip
    found = json.loads(
        (
            await call_tool(runtime, "find_sessions", harness.router_id, query="login")
        ).text_result_for_llm
    )
    assert found["matches"][0]["load"]["busy"] == "2 open tasks"
    other = (await spawn(runtime, harness.router_id, "Docs", project=""))["session_id"]
    peers = json.loads(
        (await call_tool(runtime, "find_sessions", other, query="login")).text_result_for_llm
    )
    assert peers["matches"][0]["session_id"] == worker
    assert "load" not in peers["matches"][0], "a worker's view of the others is as before"
    refused = await call_tool(runtime, "spawn_worker", harness.router_id, title="Fix login copy",
                              instructions="Fix the login form's copy.")  # fmt: skip
    said = refused.text_result_for_llm
    assert "A busy one too" in said and '"busy": "2 open tasks"' in said, said
    old, new = harness.store.settings(), Settings(fork_busy=False)
    harness.store.save_settings(new)
    await runtime.settings_changed(old, new)
    listed = json.loads(
        (await call_tool(runtime, "list_sessions", harness.router_id)).text_result_for_llm
    )
    assert all("load" not in item for item in listed["sessions"])


async def test_a_worker_long_at_work_on_one_task_is_busy_too(harness):
    runtime = await harness.connect()
    worker = (await spawn(runtime, harness.router_id, "Run the migration"))["session_id"]
    await at_work(runtime, harness, worker)
    runtime.states[worker].busy_since = time.monotonic() - 11 * 60
    result = await new_task(runtime, harness, worker, "Check the docs", "Check the docs.")
    assert result["session_id"] != worker and harness.client.forks


@pytest.mark.parametrize("case", ["idle", "one task", "same_worker", "superseded", "slots", "off"])
async def test_otherwise_the_worker_gets_its_new_task_as_before(tmp_path, case):
    settings = Settings(max_workers=2 if case == "slots" else 4, fork_busy=case != "off")
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    if case == "idle":
        worker = (await spawn(runtime, harness.router_id, "Fix login"))["session_id"]
        await new_task(runtime, harness, worker, "Fix logout", "Fix logout.", earlier="keep")
        runtime.states[worker].busy = False  # Between turns, with two tasks open.
        runtime.update_session(worker, status="idle")
    elif case == "one task":
        worker = (await spawn(runtime, harness.router_id, "Fix login"))["session_id"]
        await at_work(runtime, harness, worker)
    else:
        worker = await busy(runtime, harness)
    if case == "slots":  # Another worker takes the only other slot.
        other = (await spawn(runtime, harness.router_id, "Other work"))["session_id"]
        await at_work(runtime, harness, other)
    extra = {"same_worker": True} if case == "same_worker" else {}
    extra["earlier"] = "superseded" if case == "superseded" else "keep"
    told = await new_task(runtime, harness, worker, "Export CSV", "Add a CSV export.", **extra)
    assert told.startswith("Delivered"), told
    assert "Export CSV" in tasks_of(harness, worker) and not harness.client.forks
    # (Superseded: the task it replaces is cancelled once the worker has the new one.)
    assert any("Add a CSV export." in text for text in harness.session(worker).sent[-2:])


@pytest.mark.parametrize("failing", ["fork", "unknown", "workspace"])
async def test_a_second_worker_that_cannot_be_made_leaves_the_task_with_the_busy_one(
    harness, monkeypatch, failing
):
    runtime = await harness.connect()
    worker = await busy(runtime, harness)

    async def refused(*args, **kwargs):
        raise RuntimeError("no")

    async def unknown(session_id, name):
        harness.client.forks.append((session_id, "0f0f0f0f-0000-4000-8000-000000000000"))
        return "0f0f0f0f-0000-4000-8000-000000000000"  # An answer of another shape, say.

    if failing == "fork":
        monkeypatch.setattr(harness.client, "fork_session", refused)
    elif failing == "unknown":
        monkeypatch.setattr(harness.client, "fork_session", unknown)
    else:
        monkeypatch.setattr(runtime.workspaces, "prepare_directory", refused)
    told = await new_task(runtime, harness, worker, "Export CSV", "Add a CSV export.")
    assert told.startswith("Delivered") and "Export CSV" in tasks_of(harness, worker)
    assert len(harness.store.open_sessions("worker")) == 1, "nothing of a second worker is left"
    made = [forked for _, forked in harness.client.forks]
    assert harness.client.deleted == made, "a fork no worker runs on is deleted"


async def test_a_slack_readers_second_worker_reads_slack_too(harness):
    runtime = await harness.connect()
    worker = await busy(runtime, harness)
    harness.store.execute("UPDATE sessions SET slack=1, yolo='off' WHERE id=?", (worker,))
    second = await new_task(runtime, harness, worker, "Summarize", "Sum up the thread.")
    row = harness.store.session(second["session_id"])
    assert row["slack"] and row["yolo"] == "off", "as careful as the one it came from"


async def test_its_own_worktree_from_the_base_never_the_busy_workers(tmp_path, monkeypatch):
    _, api = published(tmp_path, "api")
    harness = Harness(tmp_path / "data", Settings(projects=[str(api)]))
    runtime = await harness.connect()
    worker = await busy(runtime, harness, project=str(api), isolate=True)
    theirs = harness.store.session(worker)
    commit(Path(theirs["cwd"]), "half.py", "x = 1\n")
    (Path(theirs["cwd"]) / "wip.py").write_text("unsaved")
    second = await new_task(runtime, harness, worker, "Export CSV", "Add a CSV export.")
    mine = harness.store.session(second["session_id"])
    assert mine["cwd"] != theirs["cwd"] and mine["branch"] not in {"", theirs["branch"]}
    assert (Path(mine["cwd"]) / "LATEST.md").exists(), "from the project's base"
    assert (
        not (Path(mine["cwd"]) / "half.py").exists() and not (Path(mine["cwd"]) / "wip.py").exists()
    )
    assert branch_of(mine["cwd"]) == mine["branch"]
    brief = harness.session(mine["id"]).sent[-1]
    assert f"Its folder: {theirs['cwd']} (branch {theirs['branch']})" in brief
    assert f"Your folder: {mine['cwd']} (branch {mine['branch']}" in brief

    async def may_write(path):
        request = SimpleNamespace(kind="write", file_name="", resolved_path=path)
        decision = await runtime.events.on_permission(request, {"session_id": mine["id"]})
        return type(decision).__name__ == "PermissionDecisionApproveOnce"

    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/nonexistent-temp")
    assert await may_write(mine["cwd"] + "/export.py")
    assert not await may_write(theirs["cwd"] + "/export.py"), "the busy worker's checkout"


async def test_what_its_conversation_copied_is_neither_shown_nor_counted(harness):
    runtime = await harness.connect()
    worker = await busy(runtime, harness)
    harness.client.logs[worker] = [
        {"type": "user.message", "id": "e1", "timestamp": "2020-01-01T00:00:00Z",
         "data": {"content": "Fix login."}},
        {"type": "assistant.message", "id": "e2", "timestamp": "2020-01-01T00:00:01Z",
         "data": {"content": "Looking at the login form."}},
        {"type": "session.usage_record", "id": "e3", "timestamp": "2020-01-01T00:00:02Z",
         "data": {"usage": {"model": "fast-model", "inputTokens": 9, "outputTokens": 9,
                            "copilotUsage": {"totalNanoAiu": 5e9}}}},
    ]  # fmt: skip
    second = (await new_task(runtime, harness, worker, "Export CSV", "Add CSV."))["session_id"]
    await settle(runtime)
    # It let go of its session (idle), and Chief of Staff started again since.
    await runtime.drop_handle(second)
    runtime.workers.read_to.clear()
    await runtime.workers.sync_usage(harness.store.session(second))
    await new_task(runtime, harness, second, "Export TSV", "And TSV.", same_worker=True)
    said = [text for _, text in harness.kinds(second)]
    assert "Looking at the login form." not in said and "Meanwhile, in a terminal" not in said
    assert runtime.view(harness.store.session(second))["credits"] == 0


async def test_the_router_has_same_worker_and_its_rule_only_while_it_is_on(harness):
    runtime = await harness.connect()

    def router():
        options = runtime.options(harness.store.session(harness.router_id))
        tool = next(t for t in options["tools"] if t.name == "message_session")
        return "same_worker" in json.dumps(tool.parameters), options["system_message"]["content"]

    field, prompt = router()
    assert field and prompts.FORK_RULE in prompt
    old, new = harness.store.settings(), Settings(fork_busy=False)
    harness.store.save_settings(new)
    await runtime.settings_changed(old, new)
    field, prompt = router()
    assert not field and prompts.FORK_RULE not in prompt


@pytest.mark.parametrize(
    ("kinds", "cut"),
    [
        # Idle: nothing in progress, all of it is copied.
        (["user.message", "assistant.turn_end", "session.usage_checkpoint"], ""),
        # In a turn, with a message steered into it: cut where that turn began.
        (["user.message", "session.usage_checkpoint", "user.message", "assistant.turn_start",
          "user.message", "tool.execution_start"], "e2"),
        # Its first turn, still going.
        (["user.message", "assistant.turn_start"], "e0"),
        # A log that never shows a turn's end: its latest message.
        (["user.message", "assistant.turn_end", "user.message"], "e2"),
    ],
)  # fmt: skip
def test_a_fork_stops_before_the_turn_in_progress(kinds, cut):
    events = [{"type": kind, "id": f"e{n}", "data": {}} for n, kind in enumerate(kinds)]
    events.append({"type": "user.message", "id": "sub", "data": {"parentToolCallId": "c1"}})
    assert fork_point(events) == cut


async def test_the_scripted_backends_fork_has_what_came_before_the_turn_in_progress():
    async def brain(session, prompt):
        if "slow" in prompt:
            yield Wait(5)
        yield Say("ok")

    backend = ScriptedBackend(brain)
    session = await backend.create_session(session_id="s1", name="n")
    await session.send("remember BLUEBIRD")
    await session.turn
    await session.send("slow work")
    await asyncio.sleep(0.05)
    forked = await backend.fork_session("s1", "fork")
    assert backend.histories[forked] == [("user", "remember BLUEBIRD"), ("assistant", "ok")]
    await session.disconnect()
