"""Codex workers (backend_codex.py): the adapter's translation of the app-server, on a stand-in
CLI (tests/fake_codex.py) under the real supervisor; and the runtime's paths for another agent's
workers that phase 1 adds: its options, its admission, its processes. What every backend shares
is in test_backend_contract.py, also on real Codex."""

import asyncio
import json
import os
import signal

import pytest
from conftest import Harness, call_tool, fake_codex
from copilot import Tool, define_tool
from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from pydantic import BaseModel

from chiefofstaff import agents, backend_codex, events
from chiefofstaff import runtime as runtime_module
from chiefofstaff.backend import process_table, running
from chiefofstaff.backend_codex import (
    REFUSED_NOTE,
    CodexBackend,
    github_server,
    inner_command,
    text_of,
    trust_levels,
    version_of,
)
from chiefofstaff.envelope import wrap
from chiefofstaff.models import Settings
from chiefofstaff.roles import WORKER
from chiefofstaff.runtime import AgentConnection


@pytest.fixture(autouse=True)
def promptly(monkeypatch):
    monkeypatch.setattr(events, "HELD_AFTER_IDLE", 0)
    monkeypatch.setattr(events, "HELD_AFTER_TURN", 0)
    monkeypatch.setattr(runtime_module, "PROCESS_CHECK", 0.05)


class Echo(BaseModel):
    text: str


def echo_tool(calls: list):
    def echo(params: Echo, invocation):
        calls.append((params.text, invocation.session_id))
        return f"echo: {params.text}"

    return define_tool("echo", description="Echo text back", params_type=Echo, handler=echo)


def approve(request, invocation):
    return PermissionDecisionApproveOnce()


class Turn:
    def __init__(self, session):
        self.events = []
        self.idle = asyncio.Event()
        session.on(self.on_event)

    def on_event(self, event):
        self.events.append(event)
        if event.type.value in {"session.idle", "session.error"}:
            self.idle.set()

    def kinds(self):
        return [event.type.value for event in self.events]

    def data(self, kind):
        return [event.data for event in self.events if event.type.value == kind]

    async def wait(self, seconds=20):
        await asyncio.wait_for(self.idle.wait(), seconds)
        self.idle.clear()


@pytest.fixture
async def codex(tmp_path, monkeypatch):
    backend = CodexBackend(fake_codex(tmp_path / "fake", monkeypatch))
    await backend.start()
    yield backend
    await backend.stop()


async def opened(backend, tmp_path, **options):
    options.setdefault("on_permission_request", approve)
    session = await backend.create_session(
        session_id="ignored", name="test", working_directory=str(tmp_path), **options
    )
    return session, Turn(session)


async def until(check, seconds: float = 20):
    for _ in range(int(seconds * 100)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


# What the adapter reads from Codex


def test_a_command_line_without_the_shell_codex_runs_it_in():
    assert inner_command("/bin/zsh -lc 'npm test -- --watch=false'") == (
        "npm test -- --watch=false"
    )
    assert inner_command("bash -c 'echo hi'") == "echo hi"
    assert inner_command("git status") == "git status", "no shell around it"
    assert inner_command("/bin/zsh -lc 'unclosed") == "/bin/zsh -lc 'unclosed"
    assert inner_command("python -c 'print(1)'") == "python -c 'print(1)'", "not a shell"


def test_what_is_read_from_its_answers():
    assert text_of([{"type": "text", "text": "a"}, {"type": "localImage"}, {"type": "text"}]) == (
        "a\n"
    )
    assert text_of(None) == ""
    assert version_of("chiefofstaff/0.167.2 (Mac OS 27.0.1; arm64) Terminal (x; 0.1.39)") == (
        "0.167.2"
    )
    assert version_of("") == ""
    assert github_server("github", None)
    assert github_server("gh", {"command": "docker", "args": ["ghcr.io/github/github-mcp-server"]})
    assert github_server("work", {"url": "https://api.githubcopilot.com/mcp/"})
    assert not github_server("notes", {"command": "notes-mcp", "args": ["--port", 1]})
    assert not github_server("notes", None)
    assert wrap("system", "refused", "x").split("\n")[0] == REFUSED_NOTE


def test_each_folder_is_trusted_as_the_owner_trusts_its_repository(tmp_path):
    owners = {
        str(tmp_path / "trusted"): {"trust_level": "trusted"},
        str(tmp_path / "other"): {"trust_level": "untrusted"},
        str(tmp_path / "odd"): "trusted",
    }
    worktree, repo = tmp_path / "work", tmp_path / "trusted" / "repo"
    levels = trust_levels(owners, [(str(worktree), str(repo)), (str(tmp_path / "x"), "/x")])
    assert levels[str(worktree)] == levels[str(repo)] == {"trust_level": "trusted"}
    assert levels[str(tmp_path / "x")] == {"trust_level": "untrusted"}


# The adapter on the stand-in


async def test_a_turn_shows_as_copilots_does(codex, tmp_path):
    calls = []
    session, turn = await opened(codex, tmp_path, tools=[echo_tool(calls)])
    await session.send(
        "Call the echo tool with text 'hi'. Run the shell command `echo out; exit 3`. "
        "Then reply with just: done"
    )
    await turn.wait()
    kinds = turn.kinds()
    assert kinds.index("user.message") < kinds.index("assistant.turn_start"), "as Copilot's"
    assert calls == [("hi", session.session_id)]
    narration, answer = turn.data("assistant.message")
    assert narration.tool_requests and answer.tool_requests is None and answer.content == "done"
    starts = {d.tool_name: d for d in turn.data("tool.execution_start")}
    assert starts["bash"].arguments == {"command": "echo out; exit 3"}
    done = {d.tool_call_id: d for d in turn.data("tool.execution_complete")}
    shell = done[starts["bash"].tool_call_id]
    assert (shell.success, shell.result.content, shell.shell_execution.exit_code) == (
        False,
        "out\n",
        3,
    )
    assert done[starts["echo"].tool_call_id].result.content == "echo: hi"
    usage = turn.data("agent.usage")
    assert usage and usage[-1].total["inputTokens"] >= usage[0].total["inputTokens"]
    assert kinds[-1] == "session.idle"


async def test_its_other_steps_show_as_copilots_tools_do(codex, tmp_path):
    session, turn = await opened(codex, tmp_path)
    await session.send(
        "Search the web for cats. View the image red.png. Enter review mode. "
        "Then reply with just: done"
    )
    await turn.wait()
    starts = [(d.tool_name, d.arguments) for d in turn.data("tool.execution_start")]
    assert starts == [("web_search", {"query": "cats"}), ("view", {"path": "red.png"})]
    assert len(turn.data("tool.execution_complete")) == 2, "a step it does not show: nothing"


async def test_a_turn_codex_starts_by_itself_shows_as_it_goes(codex, tmp_path):
    session, turn = await opened(codex, tmp_path)
    await session.send("Start a turn by yourself after this one. Reply with just: first")
    await until(lambda: turn.kinds().count("session.idle") == 2)
    second = turn.kinds()[turn.kinds().index("session.idle") + 1 :]
    assert second == ["assistant.turn_start", "assistant.message", "agent.usage", "session.idle"]


async def test_what_cannot_be_understood_or_answered_is_said_and_skipped(
    codex, tmp_path, monkeypatch
):
    failing = []

    def broken(event):
        if event.type.value == "assistant.message":
            failing.append(event.data.content)
            raise RuntimeError("a handler that fails")

    def check(request, invocation):
        raise RuntimeError("a check that fails")

    session, turn = await opened(codex, tmp_path, on_permission_request=check)
    session.on(broken)
    await session.send("Write garbage. Ask about a stranger. Reply with just: still")
    await turn.wait()
    assert failing == ["Stranger: Unknown thread.", "still"], "answered with an error; it goes on"
    await session.send("Run the shell command `mkdir nope`.")
    await turn.wait()
    assert turn.data("session.error")[0].message == "a check that fails"
    assert not (tmp_path / "nope").exists()
    monkeypatch.setattr(backend_codex, "REQUEST_TIMEOUT", 0.2)
    with pytest.raises(backend_codex.CodexError, match="did not answer test/hang in time"):
        await codex.request("test/hang", {})


async def test_a_failing_tool_and_an_unknown_one_answer_as_copilots_do(codex, tmp_path):
    def broken(params: Echo, invocation):
        raise RuntimeError("down")

    def raw(invocation):
        raise RuntimeError("down")

    tools = [
        define_tool("echo", description="Echo", params_type=Echo, handler=broken),
        Tool(name="raw", description="Raw", handler=raw),
    ]
    session, _ = await opened(codex, tmp_path, tools=tools)
    answer = await session.tool_call({"tool": "echo", "arguments": {"text": "x"}, "callId": "c"})
    assert answer["success"] is False, "as the Copilot SDK reports its tools' failures"
    answer = await session.tool_call({"tool": "raw", "arguments": {}, "callId": "c"})
    assert answer == {
        "contentItems": [{"type": "inputText", "text": "The tool failed: down"}],
        "success": False,
    }
    answer = await session.tool_call({"tool": "nothing", "arguments": {}, "callId": "c"})
    assert answer == {
        "contentItems": [{"type": "inputText", "text": "Tool nothing is not available."}],
        "success": False,
    }


async def test_with_ask_first_every_server_it_can_name_asks(tmp_path, monkeypatch):
    servers = {
        "notes": {"command": "notes", "tools": {"write": {"approval_mode": "approve"}, "a.b": {}}},
        "dotted.name": {"command": "x"},
        "odd": "not a table",
    }
    monkeypatch.setenv("FAKE_CODEX_CONFIG", json.dumps({"mcp_servers": servers}))
    codex = CodexBackend(fake_codex(tmp_path / "fake", monkeypatch))
    await codex.start()
    try:
        config = await codex.thread_config({"ask_first": True}, str(tmp_path))
    finally:
        await codex.stop()
    assert {key: value for key, value in config.items() if key != "projects"} == {
        "mcp_servers.notes.default_tools_approval_mode": "prompt",
        "mcp_servers.notes.tools.write.approval_mode": "prompt",
    }


async def test_a_refusal_reaches_the_model_and_is_no_message_of_chief_of_staff(codex, tmp_path):
    def deny(request, invocation):
        return PermissionDecisionReject(feedback="Not here: add_repo it.")

    session, turn = await opened(codex, tmp_path, on_permission_request=deny)
    steered = []
    request = codex.request

    async def spying(method, params):
        if method == "turn/steer":
            steered.append(params["input"][0]["text"])
        return await request(method, params)

    codex.request = spying  # type: ignore[method-assign]
    await session.send("Run the shell command `mkdir nope`. Wait 1 s. Then reply with just: ok")
    await turn.wait()
    assert not (tmp_path / "nope").exists()
    shell = turn.data("tool.execution_complete")[0]
    assert (shell.success, shell.error.message, shell.shell_execution) == (False, "Refused.", None)
    assert steered == [
        wrap("system", "refused", "Chief of Staff refused that: Not here: add_repo it.")
    ], "the model hears why"
    assert len(turn.data("user.message")) == 1, "its note is not a message Chief of Staff sent"
    history = [e.data.content for e in await session.get_events() if e.type.value == "user.message"]
    assert len(history) == 1 and not history[0].startswith(REFUSED_NOTE)


async def test_every_kind_of_request_is_answered(codex, tmp_path):
    asked, answered = [], []

    def check(request, invocation):
        asked.append(request)
        return PermissionDecisionReject(feedback="No network.")

    async def form(context):
        answered.append(("form", context))
        return {"action": "accept", "content": {"env": "staging"}}

    async def question(request, context):
        answered.append(("question", request))
        return {"answer": "Blue", "wasFreeform": False}

    session, turn = await opened(
        codex,
        tmp_path,
        on_permission_request=check,
        on_elicitation_request=form,
        on_user_input_request=question,
    )
    await session.send(
        "Fetch https://example.com now. Fill the form of deploy. Ask me red or blue? "
        "Ask for more permissions. Ask something odd. Then reply with just: done"
    )
    await turn.wait()
    assert [(r.kind, r.url) for r in asked] == [("url", "https://example.com")]
    said = [d.content for d in turn.data("assistant.message")]
    assert 'Form: {"action": "accept", "content": {"env": "staging"}}' in said
    assert 'Answered: {"q1": {"answers": ["Blue"]}}' in said, "a secret is never asked"
    assert 'Granted: {"permissions": {}}' in said
    assert any(text.startswith("Odd: item/somethingNew is not supported") for text in said)
    context = next(context for kind, context in answered if kind == "form")
    assert (context["mode"], context["elicitationSource"], context["session_id"]) == (
        "form",
        "deploy",
        session.session_id,
    )
    assert next(request for kind, request in answered if kind == "question") == {
        "question": "Choice red or blue?",
        "choices": ["Red", "Blue"],
        "allowFreeform": True,
    }


async def test_without_callbacks_nothing_is_allowed_or_asked(codex, tmp_path):
    session = await codex.create_session(session_id="x", name="", working_directory=str(tmp_path))
    turn = Turn(session)
    await session.send(
        "Run the shell command `mkdir nope`. Fill the form of deploy. Ask me red or blue? "
        "Call the nothing tool with text 'x'. Then reply with just: done"
    )
    await turn.wait()
    assert not (tmp_path / "nope").exists()
    said = [d.content for d in turn.data("assistant.message")]
    assert 'Form: {"action": "decline"}' in said and "Answered: {}" in said


async def test_a_message_sent_during_a_turn_waits_for_its_end_and_its_admission(codex, tmp_path):
    session, turn = await opened(codex, tmp_path)
    refused = []

    async def admit(asking):
        refused.append(asking.session_id)
        return "Not now."

    await session.send("Wait 1 s. Then reply with just: first")
    await until(lambda: session.turn)
    await session.send("Reply with just: second")
    await session.send("Reply with just: third")
    assert await session.queue_holds() and await session.take_queued() == 2
    await session.send("Reply with just: fourth")
    codex.admit = admit
    await turn.wait()
    assert refused == [session.session_id], "asked once its turn ended: refused, given back"
    assert not await session.queue_holds()
    assert [d.content for d in turn.data("assistant.message")] == ["first"]
    with pytest.raises(RuntimeError, match=r"Not now\."):
        await session.send("Reply with just: fifth")

    codex.admit = None
    codex.begins = lambda asking: False  # Admitted, then a change before it began.
    with pytest.raises(RuntimeError, match="settings changed"):
        await session.send("Reply with just: sixth")
    codex.begins = None
    await session.send("Wait 0.5 s. Then reply with just: seventh")
    await until(lambda: session.turn)
    await session.send("Reply with just: eighth")
    await turn.wait()
    assert [d.content for d in turn.data("assistant.message")][-2:] == ["seventh", "eighth"]
    assert turn.kinds().count("session.idle") == 2, "one turn after the other, then idle"


async def test_a_steer_into_a_turn_that_just_ended_waits_for_that_end(codex, tmp_path):
    session, turn = await opened(codex, tmp_path)
    handle, release = session.handle, asyncio.Event()

    async def held(method, params, thread):
        if method == "turn/completed":
            await release.wait()  # Its end is on its way, not handled yet.
        await handle(method, params, thread)

    session.handle = held  # type: ignore[method-assign]
    await session.send("Reply with just: first")
    await until(lambda: [d.content for d in turn.data("assistant.message")] == ["first"])
    await asyncio.sleep(0.2)
    await session.send("Reply with just: second", mode="immediate")
    assert await session.queue_holds(), "no turn to steer into: it waits for that turn's end"
    release.set()
    await turn.wait()
    assert [d.content for d in turn.data("assistant.message")] == ["first", "second"]
    assert not [d for d in turn.data("user.message") if d.delivery], "a turn of its own"


async def test_a_message_steered_into_its_turn_says_so(codex, tmp_path):
    session, turn = await opened(codex, tmp_path)
    await session.send("Wait 5 s. Then reply with just: first")
    await until(lambda: session.turn and turn.data("user.message"))
    await session.send("Reply with just: steered", mode="immediate")
    await turn.wait()
    first, steered = turn.data("user.message")
    assert (first.delivery, steered.delivery.value) == (None, "steering")
    assert turn.kinds().count("assistant.turn_start") == 1, "one turn"


async def test_a_child_agent_at_work_after_its_turn_holds_it(codex, tmp_path):
    calls = []
    session, turn = await opened(codex, tmp_path, tools=[echo_tool(calls)])
    await session.send(
        "Have a detached child agent call the echo tool with text 'later'. "
        "Then reply with just: started"
    )
    await until(lambda: "assistant.idle" in turn.kinds())
    assert "session.idle" not in turn.kinds() and await session.running_background() == 1
    await turn.wait()
    assert calls == [("later", session.session_id)]
    assert await session.running_background() == 0


async def test_stopping_a_child_left_at_work_ends_it(codex, tmp_path):
    session, turn = await opened(codex, tmp_path)
    await session.send(
        "Have a detached child agent call the echo tool with text 'x' and run `sleep 30`. "
        "Then reply with just: started"
    )
    await until(lambda: "assistant.idle" in turn.kinds())
    assert await session.stop_background() == 1
    await turn.wait()
    task = next(
        d for d in turn.data("tool.execution_complete") if d.tool_call_id.startswith("child-")
    )
    assert task.success is False, "stopped, not finished"


async def test_a_turns_start_read_after_its_end_leaves_no_turn_behind(codex, tmp_path):
    session, turn = await opened(codex, tmp_path)
    answer = codex.answer

    async def late(method, future):
        result = await answer(method, future)
        if method == "turn/start":
            await until(lambda: "session.idle" in turn.kinds())  # Its end handled first.
        return result

    codex.answer = late  # type: ignore[method-assign]
    await session.send("Reply with just: quick")
    assert session.turn is None, "that turn is over"
    codex.answer = answer  # type: ignore[method-assign]
    await session.send("Reply with just: next")
    await until(lambda: turn.kinds().count("session.idle") == 2)


async def test_a_stop_while_its_turn_starts_stops_that_turn(codex, tmp_path):
    session, turn = await opened(codex, tmp_path)
    handle, release = session.handle, asyncio.Event()

    async def held(method, params, thread):
        if method == "turn/started":
            await release.wait()  # Its start not seen yet.
        await handle(method, params, thread)

    session.handle = held  # type: ignore[method-assign]
    answer = codex.answer

    async def slow(method, future):
        await asyncio.sleep(0.3)
        return await answer(method, future)

    codex.answer = slow  # type: ignore[method-assign]
    sending = asyncio.ensure_future(session.send("Wait 5 s. Then reply with just: done"))
    await until(lambda: session.starting)
    await session.abort()
    await sending
    release.set()
    await turn.wait()
    assert "abort" in turn.kinds() and "done" not in [
        d.content for d in turn.data("assistant.message")
    ]


async def test_a_failed_turn_is_an_error_and_what_waited_is_given_back(codex, tmp_path):
    session, turn = await opened(codex, tmp_path)
    await session.send("Wait 0.5 s. Then Fail this turn.")
    await until(lambda: session.turn)
    await session.send("Reply with just: next")
    await turn.wait()
    assert turn.data("session.error")[0].message == "The model is overloaded."
    assert not await session.queue_holds()


async def test_stop_drops_what_waited_and_ends_the_turn_and_then_its_command(codex, tmp_path):
    """Codex keeps an interrupted turn's command running (0.167.2): it holds the turn's end,
    as one left running does, until the stop that follows ends it."""
    session, turn = await opened(codex, tmp_path)
    await session.send("Run the shell command `sleep 30`. Then reply with just: slept")
    await until(lambda: "tool.execution_start" in turn.kinds())
    await asyncio.sleep(0.3)
    await session.send("Reply with just: later")
    await session.abort()
    await until(lambda: "assistant.idle" in turn.kinds())
    assert turn.kinds()[-2:] == ["abort", "assistant.idle"]
    assert not await session.queue_holds()
    assert await session.stop_background() == 1
    await turn.wait()
    ended = turn.data("system.notification")[0].kind
    assert ended.exit_code is None, "stopped: no result"
    assert turn.kinds()[-1] == "session.idle"
    await session.abort()  # Nothing to stop: nothing happens.
    assert await session.stop_background() == 0


async def test_a_turn_codex_interrupted_by_itself_drops_what_waited(codex, tmp_path):
    session, turn = await opened(codex, tmp_path)
    await session.send("Wait 5 s. Then reply with just: first")
    await until(lambda: session.turn)
    await session.send("Reply with just: second")
    await codex.request("turn/interrupt", {"threadId": session.session_id, "turnId": session.turn})
    await turn.wait()
    assert turn.kinds()[-2:] == ["abort", "session.idle"]
    assert not await session.queue_holds(), "dropped, as Copilot drops what a stopped turn queued"


async def test_an_edit_whose_files_are_not_known_is_refused(codex, tmp_path):
    session, _ = await opened(codex, tmp_path)
    answer = await session.change_approval({"itemId": "unseen"}, {"session_id": "x"})
    assert answer == {"decision": "decline"}


async def test_a_child_agent_known_only_by_the_turn_that_led_to_it_is_its_workers(codex, tmp_path):
    calls = []
    session, turn = await opened(codex, tmp_path, tools=[echo_tool(calls)])
    await session.send(
        "Have a quiet child agent call the echo tool with text 'deep' and run `echo child`. "
        "Then reply with just: done"
    )
    await turn.wait()
    assert calls == [("deep", session.session_id)]
    task = next(d for d in turn.data("tool.execution_start") if d.tool_name == "task")
    inner = [d for d in turn.data("tool.execution_start") if d.tool_name != "task"]
    assert {d.parent_tool_call_id for d in inner} == {task.tool_call_id}
    ended = next(
        d for d in turn.data("tool.execution_complete") if d.tool_call_id == task.tool_call_id
    )
    assert ended.success


async def test_what_it_left_running_is_followed_across_a_detach(codex, tmp_path):
    session, turn = await opened(codex, tmp_path)
    await session.send("Leave the command `sleep 1; exit 4` running. Then reply with just: up")
    await until(lambda: "assistant.idle" in turn.kinds())
    await session.disconnect()  # Detached while it runs: still followed.
    assert codex.sessions[session.session_id] is session
    resumed = await codex.resume_session(session.session_id, working_directory=str(tmp_path))
    again = Turn(resumed)
    assert await resumed.running_background() == 1
    await again.wait()
    assert again.data("system.notification")[0].kind.exit_code == 4
    await resumed.disconnect()  # Nothing runs: let go of.
    assert session.session_id not in codex.sessions


async def test_its_model_list_and_threads_as_codex_has_them(codex, tmp_path):
    models = await codex.list_models()
    assert [(m.id, m.is_default, m.supported_reasoning_efforts) for m in models] == [
        ("gpt-test", True, ["low", "medium", "high"]),
        ("gpt-mini", False, ["low", "medium", "high"]),
    ], "every page, hidden ones left out"
    assert await codex.get_session_metadata("no-such-thread") is None
    session, turn = await opened(codex, tmp_path)
    assert (await codex.get_session_metadata(session.session_id)).session_id == session.session_id
    await session.set_model("gpt-mini", reasoning_effort="high")
    await session.send("Reroute to gpt-other and reply with just: ok")
    await turn.wait()
    assert session.current_model == "gpt-other"
    assert (await codex.list_sessions(), await codex.open_elsewhere()) == ([], {})
    assert (codex.sandbox(None), await codex.gh_works_in_sandbox()) == (None, None)
    assert (await codex.read_history("x", 3), await codex.read_usage("x", 3)) == (
        ([], 3, 0.0),
        ([], 3),
    )
    assert (codex.session_log("x"), await codex.mcp_sign_in_url(session, "s")) == (None, "")


async def test_ask_first_runs_only_on_a_tested_version(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_VERSION", "0.168.0")
    newer = CodexBackend(fake_codex(tmp_path / "fake", monkeypatch))
    await newer.start()
    try:
        assert "has not passed Chief of Staff's checks for Ask first" in newer.ask_first_refused
        with pytest.raises(RuntimeError, match="checks for Ask first"):
            await opened(newer, tmp_path, ask_first=True)
        session, turn = await opened(newer, tmp_path)  # Acting on its own: it runs.
        await session.send("Reply with just: ok")
        await turn.wait()
    finally:
        await newer.stop()


@pytest.mark.parametrize(
    ("env", "why"),
    [
        ({"FAKE_CODEX_VERSION": "0.167.1"}, "older than the versions tested"),
        ({"FAKE_CODEX_SIGNED_OUT": "1"}, "not signed in: run `codex login`"),
    ],
)
async def test_a_codex_it_cannot_run_on_says_why(tmp_path, monkeypatch, env, why):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    backend = CodexBackend(fake_codex(tmp_path / "fake", monkeypatch))
    with pytest.raises(RuntimeError, match=why):
        await backend.start()
    assert not backend.alive()


async def test_without_the_cli_it_says_so(tmp_path):
    with pytest.raises(RuntimeError, match=r"not found\. Install it and sign in"):
        await CodexBackend(str(tmp_path / "no-codex")).start()


async def test_when_its_process_dies_what_waited_on_it_fails_and_nothing_is_left(codex, tmp_path):
    session, turn = await opened(codex, tmp_path)
    await session.send("Leave the command `sleep 120` running. Then reply with just: up")
    await until(lambda: "assistant.idle" in turn.kinds())
    tree = codex.processes()
    os.kill(codex.process.pid, signal.SIGKILL)  # The supervisor itself, killed.
    await until(lambda: not codex.alive())
    with pytest.raises(ConnectionResetError):
        await session.send("Reply with just: ok")
    await codex.stop()  # As watch_agent does: what it left (the CLI, its command) ends.
    with pytest.raises(ConnectionResetError):
        await codex.request("model/list", {})
    await asyncio.sleep(0.5)
    table = await asyncio.to_thread(process_table)
    assert not [pid for pid, command in tree.items() if table.get(pid, (0, ""))[1] == command]


# The runtime with a Codex worker


async def brief(tmp_path, monkeypatch, settings: Settings | None = None, **env):
    """A runtime with Codex turned on (the stand-in), whose new workers run on it."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    command = fake_codex(tmp_path / "fake", monkeypatch)
    harness = Harness(tmp_path / "data", settings)
    agents.configure(harness.store, "codex", enabled=True)
    runtime = await harness.connect()
    runtime.agents["codex"] = AgentConnection(
        lambda: CodexBackend(command), enabled=lambda: agents.enabled(harness.store, "codex")
    )
    create = harness.store.create_session

    def creating(session_id, role, *args, **kwargs):
        row = create(session_id, role, *args, **kwargs)
        if role == WORKER:
            harness.store.set(f"agent:{session_id}", "codex")
        return row

    harness.store.create_session = creating  # type: ignore[method-assign]
    return harness, runtime


async def spawn(runtime, harness, instructions: str) -> str:
    result = await call_tool(
        runtime,
        "spawn_worker",
        harness.router_id,
        title="Fix it",
        instructions=instructions,
        why_new="t",
    )
    return json.loads(result.text_result_for_llm)["session_id"]


def commands(harness, worker) -> list[tuple]:
    rows = harness.store.all(
        "SELECT command, exit_code, ended_at IS NOT NULL AS ended FROM commands "
        "WHERE session_id=? ORDER BY id",
        (worker,),
    )
    return [(row["command"], row["exit_code"], bool(row["ended"])) for row in rows]


async def test_a_codex_worker_works_reports_and_has_receipts(tmp_path, monkeypatch):
    harness, runtime = await brief(tmp_path, monkeypatch)
    try:
        worker = await spawn(
            runtime,
            harness,
            "Run the shell command `echo tested; exit 0`. Report done: Fixed it.",
        )
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        thread = harness.store.get(f"copilot:{worker}")
        assert thread and harness.store.get(f"alias:{thread}") == worker
        assert harness.store.get(f"created:{worker}") and not harness.store.get(f"fresh:{thread}")
        assert commands(harness, worker) == [("echo tested; exit 0", 0, True)]
        task = runtime.tasks.for_session(worker)[-1]
        assert task["result"] == "Fixed it." and task["status"] not in {"running", "queued"}
        records = json.loads(harness.store.get("agentpids:codex"))
        assert records and all(record["data_dir"] for record in records.values())
        options = runtime.agent_options(harness.store.session(worker))
        copilot = runtime.options(harness.store.session(worker))
        assert options["system_message"]["content"] == copilot["system_message"]["content"]
        assert [t.name for t in options["tools"]] == [t.name for t in copilot["tools"]]
        assert options["ask_first"] is False and "model" not in options
        asking = {**harness.store.session(worker), "yolo": "off"}
        assert runtime.agent_options(asking)["ask_first"] is True
        assert (worker_cwd := harness.store.session(worker)["cwd"]) in {
            folder for folder, _ in options["trust"]
        }
        assert worker_cwd
        assert not runtime.agent_block(harness.store.session(harness.router_id))
    finally:
        await runtime.stop()


async def test_a_codex_worker_runs_only_where_it_may(tmp_path, monkeypatch):
    harness, runtime = await brief(tmp_path, monkeypatch)
    try:
        runtime.sandbox_host = True
        harness.store.save_settings(
            harness.store.settings().model_copy(update={"worker_sandbox": True})
        )
        worker = await spawn(runtime, harness, "Report done: Fixed it.")
        await until(lambda: harness.store.session(worker)["status"] == "failed", 30)
        assert "can't run while workers are sandboxed" in harness.kinds(worker)[-1][1]
        assert runtime.connection("codex") == (None, 0), "its CLI never started"

        harness.store.save_settings(
            harness.store.settings().model_copy(update={"worker_sandbox": False})
        )
        agents.configure(harness.store, "codex", enabled=False)
        row = harness.store.session(worker)
        assert runtime.agent_block(row) == "Codex is turned off in Settings → Models."
        with pytest.raises(RuntimeError, match="turned off"):
            await runtime.connect_agent("codex")
        agents.configure(harness.store, "codex", enabled=True)
        harness.store.set(f"learner:{worker}", "1")
        assert runtime.agent_block(row) == "Only your own workers run on Codex."
        harness.store.delete(f"learner:{worker}")
        assert runtime.agent_block({**row, "slack": 1}).startswith("Workers that read Slack")
        harness.store.set(f"agent:{worker}", "elsewhere")
        assert "not available" in runtime.agent_block(row)
    finally:
        await runtime.stop()


async def test_a_settings_save_stops_codex_workers_it_makes_ineligible(tmp_path, monkeypatch):
    harness, runtime = await brief(tmp_path, monkeypatch)
    try:
        worker = await spawn(runtime, harness, "Wait 30 s. Then reply with just: done")
        await until(lambda: (s := runtime.states.handle(worker)) and s.turn, 30)
        session = runtime.states.handle(worker)
        generation = runtime.admission_generation
        runtime.sandbox_host = True
        old = harness.store.settings()
        new = old.model_copy(update={"worker_sandbox": True})
        harness.store.save_settings(new)
        await runtime.settings_changed(old, new)
        assert runtime.admission_generation == generation + 1
        await until(lambda: session.turn is None)
        assert ("notice", "Stopped by Settings.") in harness.kinds(worker)
        task = runtime.tasks.for_session(worker)[-1]
        assert task["status"] == "waiting"
        with pytest.raises(RuntimeError, match="sandboxed"):
            await runtime.owner_send(worker, "Go on")
        assert runtime.begins_turn(session) is False, "admitted before: never begins after"
    finally:
        await runtime.stop()


async def test_a_turn_admitted_before_a_change_never_begins_after_it(tmp_path, monkeypatch):
    harness, runtime = await brief(tmp_path, monkeypatch)
    try:
        worker = await spawn(runtime, harness, "Reply with just: first")
        await until(lambda: harness.store.session(worker)["status"] != "working", 30)
        backend, _ = runtime.connection("codex")
        admit = backend.admit

        async def admitted_then_changed(session):
            why = await admit(session)
            async with runtime.admission:
                runtime.admission_generation += 1  # A Settings save, just after.
            return why

        backend.admit = admitted_then_changed
        with pytest.raises(RuntimeError, match="settings changed"):
            await runtime.owner_send(worker, "Reply with just: second")
        said = [text for kind, text in harness.kinds(worker) if kind == "assistant"]
        assert "second" not in said
    finally:
        await runtime.stop()


async def test_a_turn_queued_in_codex_never_begins_once_its_worker_may_not_run(
    tmp_path, monkeypatch
):
    """A message waiting in the adapter for its worker's turn to end, and the agent turned off
    meanwhile: its turn is refused at the door (Runtime.admit_turn) and never begins."""
    harness, runtime = await brief(tmp_path, monkeypatch)
    try:
        worker = await spawn(runtime, harness, "Wait 2 s. Then reply with just: first")
        await until(lambda: (s := runtime.states.handle(worker)) and s.turn, 30)
        session = runtime.states.handle(worker)
        await runtime.delivery.deliver(
            worker, wrap("router", "message", "Reply with just: second"), kind="router"
        )
        assert await session.queue_holds(), "waits in Codex's adapter for the turn's end"
        agents.configure(harness.store, "codex", enabled=False)
        await until(lambda: session.turn is None and not session.starting, 30)
        await asyncio.sleep(0.3)
        assert not await session.queue_holds(), "given back"
        said = [text for kind, text in harness.kinds(worker) if kind == "assistant"]
        assert "second" not in said
    finally:
        await runtime.stop()


async def test_a_blocked_codex_worker_never_starts_codex(tmp_path, monkeypatch):
    harness, runtime = await brief(tmp_path, monkeypatch)
    try:
        worker = await spawn(runtime, harness, "Reply with just: done")
        await until(lambda: harness.store.session(worker)["status"] != "working", 30)
        await runtime.disconnect_agent("codex")
        harness.store.save_settings(
            harness.store.settings().model_copy(update={"worker_sandbox": True})
        )
        with pytest.raises(RuntimeError, match="sandboxed"):
            await runtime.owner_send(worker, "Once more")
        assert runtime.connection("codex")[0] is None, "its CLI was not started for it"
    finally:
        await runtime.stop()


async def test_copilots_model_is_never_a_codex_workers(tmp_path, monkeypatch):
    """Copilot's model chosen in Settings (one without efforts, switched to on a resume for a
    Copilot worker) is not given to a Codex worker, created or resumed."""
    harness, runtime = await brief(tmp_path, monkeypatch, Settings(model="fast-model"))
    try:
        await runtime.available_models()
        worker = await spawn(runtime, harness, "Reply with just: done")
        await until(lambda: harness.store.session(worker)["status"] != "working", 30)
        assert runtime.states.handle(worker).model == ""
        await runtime.detach(worker)
        resumed = await runtime.session_for(worker)
        assert resumed.model == "" and resumed.session_id == harness.store.get(f"copilot:{worker}")
    finally:
        await runtime.stop()


async def test_ask_first_on_an_untested_codex_is_refused_with_why(tmp_path, monkeypatch):
    harness, runtime = await brief(
        tmp_path, monkeypatch, Settings(worker_yolo=False), FAKE_CODEX_VERSION="0.168.1"
    )
    try:
        worker = await spawn(runtime, harness, "Report done: Fixed it.")
        await until(lambda: harness.store.session(worker)["status"] == "failed", 30)
        assert "checks for Ask first" in harness.kinds(worker)[-1][1]
        assert "checks for Ask first" in runtime.agent_block(harness.store.session(worker))
        harness.store.update_session(worker, yolo="on")
        assert not runtime.agent_block(harness.store.session(worker))
        await runtime.connect_agent("codex")
        with pytest.raises(ValueError, match="checks for Ask first"):
            await runtime.set_session_yolo(worker, "off")
        assert harness.store.session(worker)["yolo"] == "on"
        await runtime.set_session_yolo(worker, "on")
    finally:
        await runtime.stop()


async def test_what_codex_left_after_an_abrupt_stop_is_ended_at_its_next_start(
    tmp_path, monkeypatch
):
    harness, runtime = await brief(tmp_path, monkeypatch)
    sleeper = await asyncio.create_subprocess_exec("sleep", "300")
    try:
        from chiefofstaff.backend import copilot_records

        records = await asyncio.to_thread(
            copilot_records, {sleeper.pid: "sleep 300"}, harness.store.directory
        )
        harness.store.set("agentpids:codex", json.dumps(records))
        await runtime.connect_agent("codex")
        await asyncio.wait_for(sleeper.wait(), 15)
        assert not running(sleeper.pid)
    finally:
        if sleeper.returncode is None:
            sleeper.kill()
        await runtime.stop()


async def test_a_codex_worker_codex_no_longer_has_cannot_be_reopened(tmp_path, monkeypatch):
    harness, runtime = await brief(tmp_path, monkeypatch)
    try:
        worker = await spawn(runtime, harness, "Reply with just: done")
        await until(lambda: harness.store.session(worker)["status"] != "working", 30)
        await runtime.workers.close(worker, by="you")
        backend, _ = runtime.connection("codex")
        await backend.archive(harness.store.get(f"copilot:{worker}"))
        with pytest.raises(ValueError, match=r"^Codex no longer has that conversation"):
            await runtime.workers.reopen(worker)
    finally:
        await runtime.stop()


async def test_a_thread_made_while_codex_starts_again_is_let_go(tmp_path, monkeypatch):
    harness, runtime = await brief(tmp_path, monkeypatch)
    try:
        backend = await runtime.connect_agent("codex")
        create = backend.create_session

        async def restarting(**options):
            session = await create(**options)
            runtime.agents["codex"].connections += 1  # Codex started again meanwhile.
            return session

        backend.create_session = restarting  # type: ignore[method-assign]
        harness.store.create_session("w1", WORKER, "W", "starting", cwd=str(tmp_path))
        harness.store.set("agent:w1", "codex")
        with pytest.raises(RuntimeError, match=r"^Codex started again meanwhile; try again"):
            await runtime.open_live(harness.store.session("w1"), create=True)
    finally:
        await runtime.stop()


async def test_nothing_of_codex_is_stored_or_started_until_it_is_turned_on(harness):
    runtime = await harness.connect()
    assert runtime.agents == {} and agents.configured(harness.store) == {}
    harness.store.set(agents.CONFIG, "not json")
    assert agents.configured(harness.store) == {}
    harness.store.set(agents.CONFIG, "[]")
    assert agents.configured(harness.store) == {}
    with pytest.raises(ValueError, match="Unknown agent"):
        agents.configure(harness.store, "gemini", enabled=True)
    with pytest.raises(RuntimeError, match="does not run"):
        agents.backend("gemini")
    assert agents.display("codex") == "Codex" and agents.display("other") == "other"
    await runtime.stop()
