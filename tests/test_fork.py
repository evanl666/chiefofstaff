"""A new task for a busy worker goes to a second worker that starts at once with its context
(forks.py, docs/design/worker-fork.md); a free worker keeps it, as before."""

import asyncio
import json
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeClient, Harness, call_tool, settle
from copilot.tools import ToolInvocation
from test_agents import saved, spawn_on
from test_backends import live, on, received, until
from test_repos import commit, published

from chiefofstaff import backend as backend_module
from chiefofstaff import prompts
from chiefofstaff.backend import fork_point
from chiefofstaff.models import Settings
from chiefofstaff.scripted import Say, ScriptedBackend, Wait
from chiefofstaff.store import Store


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


def tasks_of_ids(harness, worker):
    return [t["id"] for t in harness.store.all(
        "SELECT id FROM tasks WHERE session_id=? ORDER BY created_at", (worker,))]  # fmt: skip


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
    line = f"Started a second worker from “{name}” (it was busy) for “Export CSV”"
    assert notices[-1] == f"{line} · new because test", "why it is new, on hover"
    assert ("notice", "Its context went to a second worker for “Export CSV”") in harness.kinds(
        worker
    )
    assert harness.kinds(second)[0] == ("notice", f"Started with the context of “{name}”")
    # Why, for the sidebar's ⑂: "…, because <it> was busy".
    assert runtime.view(row)["fork_of"] == {"id": worker, "title": name, "busy": True}
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
    runtime.states[worker].busy_since = time.monotonic() - 21 * 60
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


async def test_a_second_worker_that_cannot_be_made_leaves_the_task_with_the_busy_one(
    harness, monkeypatch
):
    runtime = await harness.connect()
    worker = await busy(runtime, harness)

    async def refused(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(runtime.workspaces, "prepare_directory", refused)
    told = await new_task(runtime, harness, worker, "Export CSV", "Add a CSV export.")
    assert told.startswith("Delivered") and "Export CSV" in tasks_of(harness, worker)
    assert len(harness.store.open_sessions("worker")) == 1, "nothing of a second worker is left"
    made = [forked for _, forked in harness.client.forks]
    assert made and harness.client.deleted == made, "a fork no worker runs on is deleted"


@pytest.mark.parametrize("failing", ["fork", "unknown", "not forked"])
async def test_a_busy_worker_whose_conversation_cannot_be_copied_hands_on_a_brief_of_its_work(
    harness, monkeypatch, failing
):
    """Where its agent's sessions are not forked, or the fork fails, the second worker still
    takes the task at once: with a brief of the busy worker's work (its tasks, what its
    finished ones found, its folders and files) instead of its conversation."""
    runtime = await harness.connect()
    worker = (await spawn(runtime, harness.router_id, "Fix login"))["session_id"]
    runtime.tasks.update(tasks_of_ids(harness, worker)[0], status="review",
                         result="The form posts twice: fixed.")  # fmt: skip
    await new_task(runtime, harness, worker, "Fix logout", "Fix logout.", why_new="another")
    await at_work(runtime, harness, worker)
    await new_task(runtime, harness, worker, "Fix signup", "Fix signup.", earlier="keep")

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
        monkeypatch.setattr(harness.client, "capabilities", replace(FakeClient.capabilities,
                                                                    fork=False))  # fmt: skip
    before = list(harness.session(worker).sent)
    result = await new_task(runtime, harness, worker, "Export CSV", "Add a CSV export.")
    await settle(runtime)
    second = result["session_id"]
    assert second != worker and tasks_of(harness, second) == ["Export CSV"]
    assert "Export CSV" not in tasks_of(harness, worker)
    assert harness.session(worker).sent == before, "the busy worker is not disturbed"
    assert second in harness.client.created and second not in harness.client.resumed, (
        "a session of its own: nothing of the conversation"
    )
    sent = harness.session(second).sent
    assert len(sent) == 1 and "Add a CSV export." in sent[0]
    assert "its conversation is not copied to you" in sent[0], "the brief says so"
    assert "“Fix signup”" in sent[0] and "The form posts twice: fixed." in sent[0], (
        "its open tasks, and what its finished ones found"
    )
    assert "a brief of its work" in result["note"]
    made = [forked for _, forked in harness.client.forks]
    assert harness.client.deleted == made, "a fork no worker runs on is deleted"
    name = runtime.display_title(harness.store.session(worker))
    assert runtime.view(harness.store.session(second))["fork_of"]["id"] == worker
    notices = [text for kind, text in harness.kinds(harness.router_id) if kind == "notice"]
    assert notices[-1].startswith(
        f"Started a second worker from “{name}” (it was busy) for “Export CSV”"
    )


async def test_a_busy_worker_on_another_agent_gets_a_second_worker_there_with_a_brief(harness):
    """An agent whose sessions are not forked (a Codex-like one): the second worker runs on that
    agent too, at once, with a brief of the busy worker's work; while that agent cannot run
    one, the task waits for the busy worker, as before."""
    runtime = await harness.connect()
    on(harness, "codex-like")
    worker = (await spawn(runtime, harness.router_id, "Ponder at length"))["session_id"]
    await until(lambda: runtime.forks.in_turn(harness.store.session(worker)))
    told = await new_task(runtime, harness, worker, "Fix logout", "Fix logout.", earlier="keep")
    assert told.startswith("Delivered"), told
    chosen = json.dumps({"model": "codex-like-large", "effort": "high"})
    harness.store.set(f"agentmodel:{worker}", chosen)
    result = await new_task(runtime, harness, worker, "Export CSV", "Add a CSV export.")
    second = result["session_id"]
    assert second != worker and runtime.agent(second) == "codex-like"
    assert harness.store.get(f"agentmodel:{second}") == chosen, "its model and effort there"
    assert tasks_of(harness, second) == ["Export CSV"] and not harness.client.forks
    await until(lambda: received(live(runtime, second)))
    given = received(live(runtime, second))[0]
    assert "Add a CSV export." in given and "its conversation is not copied to you" in given
    assert "“Fix logout”" in given

    async def down(agent):
        return "Codex-like is signed out."

    runtime.worker_agent_down = down  # type: ignore[method-assign]
    told = await new_task(runtime, harness, worker, "Export TSV", "Add a TSV export.")
    assert "Not sent: Codex-like is signed out." in told, told
    assert len(harness.store.open_sessions("worker")) == 2, "no second worker it could not run"


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
        spawn = next(t for t in options["tools"] if t.name == "spawn_worker")
        # Never behind Copilot's tool search, as Chief of Staff's own tools are (0.1.47).
        assert tool.defer == "never" and spawn.defer == "never"
        fields = (
            "same_worker" in json.dumps(tool.parameters),
            "fork_of" in json.dumps(spawn.parameters),
        )
        return fields, options["system_message"]["content"]

    fields, prompt = router()
    assert fields == (True, True) and prompts.FORK_RULE in prompt
    # What a second worker gets, said as it is: on Codex or Claude Code, a brief.
    options = runtime.options(harness.store.session(harness.router_id))
    told = {t.name: t.parameters["properties"] for t in options["tools"]}
    said = [
        told["message_session"]["same_worker"]["description"],
        told["message_session"]["parallel"]["description"],
        told["spawn_worker"]["fork_of"]["description"],
        " ".join(prompts.FORK_RULE.split()),
    ]
    brief = "its conversation so far, or a brief of it where the conversation can't be copied"
    assert all(brief in text for text in said), said
    old, new = harness.store.settings(), Settings(fork_busy=False)
    harness.store.save_settings(new)
    await runtime.settings_changed(old, new)
    fields, prompt = router()
    assert fields == (False, False) and prompts.FORK_RULE not in prompt


async def test_the_owners_new_worker_can_start_from_a_related_ones_conversation_said_so(harness):
    """0.1.51: the owner's "a new worker" wins over reuse (the prompt's rule, the related
    workers' refusal). While second workers are on, both say how it starts from the related
    worker's conversation (fork_of); off, they are as they were."""
    runtime = await harness.connect()
    await finished(runtime, harness, "Fix the login form", project="")
    rule = " ".join(prompts.FORK_RULE.split())
    assert "When the owner asks for a new worker (see above)" in rule
    assert "spawn_worker with fork_of=<that worker's session_id>" in rule

    async def refusal():
        refused = await call_tool(runtime, "spawn_worker", harness.router_id,
                                  title="Fix the login copy",
                                  instructions="Fix the login form's copy.")  # fmt: skip
        return " ".join(refused.text_result_for_llm.split())

    said = await refusal()
    assert "(with fork_of the session_id of the one that knows the subject" in said, said
    old, new = harness.store.settings(), Settings(fork_busy=False)
    harness.store.save_settings(new)
    await runtime.settings_changed(old, new)
    said = await refusal()
    assert "fork_of" not in said and "quoting their words, and give it to none of them" in said


async def test_a_fork_never_reaches_a_copilot_that_could_not_start(tmp_path, monkeypatch):
    """As every call to Copilot since 0.1.47 (ready_client): not the client the SDK refused."""
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot"))
    calls = []
    refused = SimpleNamespace(
        rpc=SimpleNamespace(sessions=SimpleNamespace(fork=lambda request: calls.append(request))),
        delete_session=lambda session_id: calls.append(session_id),
    )
    backend = backend_module.copilot_backend("git")
    backend.client = backend.refused = refused
    session = "0b6f3f8e-1c1d-4d3e-9f6a-2b7c8d9e0f1a"
    with pytest.raises(backend_module.NotStartedError):
        await backend.fork_session(session, "Second")
    with pytest.raises(backend_module.NotStartedError):
        await backend.delete_session(session)
    assert calls == []


@pytest.mark.parametrize("case", ["in a turn", "between turns", "no slot"])
async def test_work_wanted_in_parallel_with_a_worker_at_work_goes_to_a_second_worker(
    tmp_path, case
):
    """Live routing (f3, f4): the Router gave work the owner wanted "in parallel" to the worker
    at work on a test run (one task, seconds in: not busy) with message_session, taking it for
    busy, and it waited behind that turn. Said so (parallel=true), a second worker with its
    context takes it at once; a worker between turns takes it itself; with no second worker
    possible, the Router hears so (to start one of its own) and nothing is sent."""
    harness = Harness(tmp_path / "data", Settings(max_workers=1 if case == "no slot" else 4))
    runtime = await harness.connect()
    worker = (await spawn(runtime, harness.router_id, "Run the tests"))["session_id"]
    await at_work(runtime, harness, worker)
    if case == "between turns":
        runtime.states[worker].busy = False
        runtime.update_session(worker, status="idle")
    before = list(harness.session(worker).sent)
    result = await new_task(runtime, harness, worker, "Count the lines", "Count the lines.",
                            earlier="keep", parallel=True)  # fmt: skip
    if case == "in a turn":
        second = result["session_id"]
        assert second != worker and harness.client.forks == [(worker, second)]
        assert tasks_of(harness, second) == ["Count the lines"]
        assert tasks_of(harness, worker) == ["Run the tests"]
        assert harness.session(worker).sent == before, "the worker at work is not disturbed"
        assert "busy" not in runtime.forks.origin(second), "wanted in parallel, not busy"
    elif case == "between turns":
        assert result.startswith("Delivered"), result
        assert tasks_of(harness, worker) == ["Run the tests", "Count the lines"]
        assert not harness.client.forks
    else:
        assert result.startswith("Error: Not sent: no second worker can take it"), result
        # A new worker would wait for a slot too (live fork F3): not one for it, it waits here.
        assert "every worker slot is taken" in result and "call again without parallel" in result
        assert "A new worker would wait as well, so do not start one" in result, result
        assert tasks_of(harness, worker) == ["Run the tests"] and not harness.client.forks
        assert harness.session(worker).sent == before
    rule = " ".join(prompts.FORK_RULE.split())
    assert "is message_session with parallel=true" in rule


async def spawn_from(runtime, harness, title, **extra):
    """spawn_worker as the Router has it (with fork_of while the switch is on)."""
    extra.setdefault("instructions", title)
    extra.setdefault("why_new", "test")
    router = harness.store.session(harness.router_id)
    tool = next(t for t in runtime.session_tools(router) if t.name == "spawn_worker")
    result = await tool.handler(
        ToolInvocation(session_id=harness.router_id, tool_call_id="t1",
                       tool_name="spawn_worker", arguments={"title": title, **extra})
    )  # fmt: skip
    text = result if isinstance(result, str) else result.text_result_for_llm
    return json.loads(text) if text.startswith("{") else text


async def finished(runtime, harness, title, **extra) -> str:
    """A worker that did its task (a result to review) and is between turns."""
    worker = (await spawn(runtime, harness.router_id, title, **extra))["session_id"]
    runtime.tasks.update(tasks_of_ids(harness, worker)[0], status="review",
                         result="The codename is HERON-7.")  # fmt: skip
    runtime.states[worker].busy = False
    runtime.update_session(worker, status="idle")
    return worker


async def test_a_new_worker_the_owner_asks_for_starts_from_a_related_workers_conversation(
    tmp_path,
):
    """The owner's "a new worker for this" wins over reuse (0.1.51); with fork_of the Router
    starts it from the related worker's conversation: a second worker of it, in a worktree of
    its own from the project's base, the related worker untouched."""
    _, api = published(tmp_path, "api")
    harness = Harness(tmp_path / "data", Settings(projects=[str(api)]))
    runtime = await harness.connect()
    source = await finished(runtime, harness, "Read the spec", project=str(api), isolate=True)
    theirs = harness.store.session(source)
    before = list(harness.session(source).sent)
    result = await spawn_from(runtime, harness, "Write the codename file",
                              instructions="Write the codename into codename.txt.",
                              why_new="“a new worker for this”", fork_of=source[:8])  # fmt: skip
    second = result["session_id"]
    assert second != source and harness.client.forks == [(source, second)]
    assert second in harness.client.resumed and second not in harness.client.created
    sent = harness.session(second).sent
    assert sent[: len(before)] == before and "Write the codename into codename.txt." in sent[-1]
    assert harness.session(source).sent == before, "the related worker is not disturbed"
    mine = harness.store.session(second)
    assert mine["cwd"] != theirs["cwd"] and mine["branch"] not in {"", theirs["branch"]}
    assert (Path(mine["cwd"]) / "LATEST.md").exists() and branch_of(mine["cwd"]) == mine["branch"]
    name = runtime.display_title(theirs)
    assert runtime.view(mine)["fork_of"] == {"id": source, "title": name}
    notices = [text for kind, text in harness.kinds(harness.router_id) if kind == "notice"]
    assert notices[-1].startswith(f"Started a second worker from “{name}” for “Write the "
                                  "codename file”")  # fmt: skip
    edges = [e for e in runtime.relations.graph().edges if e["kind"] == "fork"]
    assert [(e["from_session"], e["to_session"]) for e in edges] == [(source, second)]


@pytest.mark.parametrize("case", ["not forked", "fork fails", "agent down"])
async def test_a_new_worker_from_a_related_one_that_cannot_be_forked_gets_its_work(harness, case):
    """spawn_worker(fork_of) where that worker's conversation cannot be copied: a second worker
    with a brief of its work; where its agent cannot run a worker now, a new worker given its
    task as context_from."""
    runtime = await harness.connect()
    started = on(harness, "codex-like") if case == "agent down" else []
    source = await finished(runtime, harness, "Read the spec")
    if started:
        harness.store.create_session = Store.create_session.__get__(harness.store)

        async def down(agent):
            return "Codex-like is signed out."

        runtime.worker_agent_down = down  # type: ignore[method-assign]
    if case == "not forked":
        harness.client.capabilities = replace(FakeClient.capabilities, fork=False)
    elif case == "fork fails":

        async def refused(*args, **kwargs):
            raise RuntimeError("no")

        harness.client.fork_session = refused
    result = await spawn_from(runtime, harness, "Write the codename file",
                              instructions="Write the codename into codename.txt.",
                              why_new="“a new worker for this”", fork_of=source)  # fmt: skip
    second = result["session_id"]
    task = tasks_of_ids(harness, second)[0]
    assert (second != source and not harness.client.forks) or case == "fork fails"
    if case in {"not forked", "fork fails"}:
        assert runtime.forks.origin(second)["id"] == source
        given = harness.session(second).sent[-1]
        assert "its conversation is not copied to you" in given and "HERON-7" in given
        assert not harness.store.get(f"contextfrom:{task}")
    else:
        assert runtime.forks.origin(second) is None and runtime.agent(second) == "copilot"
        assert json.loads(harness.store.get(f"contextfrom:{task}")) == tasks_of_ids(harness, source)


async def test_a_new_worker_from_a_related_one_on_another_agent_the_owner_named_gets_its_work(
    tmp_path, codex_installed
):
    """The owner named another agent than the related worker's: a new worker there, given that
    worker's task as context_from (a conversation is not carried across agents)."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    try:
        source = await finished(runtime, harness, "Read the spec")
        await saved(runtime, {"codex": {"enabled": True}})
        result = await spawn_on(harness, runtime, title="Write the codename file",
                                instructions="Report done: x", fork_of=source,
                                agent="codex")  # fmt: skip
        second = result["session_id"]
        assert runtime.agent(second) == "codex" and not harness.client.forks
        assert runtime.forks.origin(second) is None
        task = tasks_of_ids(harness, second)[0]
        assert json.loads(harness.store.get(f"contextfrom:{task}")) == tasks_of_ids(harness, source)
    finally:
        await runtime.stop()


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
