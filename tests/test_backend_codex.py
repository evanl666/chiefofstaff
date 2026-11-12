"""Codex workers (backend_codex.py): the adapter's translation of the app-server, on a stand-in
CLI (tests/fake_codex.py) under the real supervisor; and the runtime's paths for another agent's
workers that phase 1 adds: its options, its admission, its processes. What every backend shares
is in test_backend_contract.py, also on real Codex."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

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
from chiefofstaff.policy import GITHUB_MCP
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


def git_repository(path, *more):
    """A repository with one commit (and a `.codex/` folder), and its worktrees at `more`."""
    import subprocess

    (path / ".codex").mkdir(parents=True)
    (path / "sub").mkdir()
    (path / "sub" / "file").write_text("x")
    git = ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / ".codex" / "config.toml").write_text("")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "start"], check=True)
    for worktree in more:
        subprocess.run([*git, "worktree", "add", "-q", str(worktree)], check=True)
    return path


def level(levels, path):
    return levels[str(path)]["trust_level"]


def test_a_folder_is_trusted_only_as_codex_itself_would_trust_it(tmp_path):
    """Codex 0.167.2 trusts a folder by the entry for that folder, its project root or its
    repository's root (a linked worktree's main checkout): never by an ancestor's."""
    code = tmp_path / "code"
    main = git_repository(code / "main", tmp_path / "scratch" / "wt")
    clone = git_repository(code / ".chief" / "clone")  # CoS's clone, under a trusted folder.
    elsewhere = git_repository(tmp_path / "elsewhere")
    owners = {
        str(code): {"trust_level": "trusted"},
        str(main): {"trust_level": "trusted"},
        str(main / "sub"): {"trust_level": "untrusted"},
        str(tmp_path / "odd"): "trusted",
        str(tmp_path / "plain"): {"trust_level": "trusted"},
    }
    worktree = tmp_path / "scratch" / "wt"
    folders = [str(p) for p in (main, main / "sub", worktree, worktree / "sub", clone, elsewhere)]
    levels = trust_levels(owners, [*folders, str(tmp_path / "plain"), str(tmp_path / "none")])
    assert level(levels, main) == "trusted"  # Exactly the owner's.
    assert level(levels, main / "sub") == "untrusted"  # Its own entry comes first.
    assert level(levels, worktree) == "trusted"  # A worktree of a repository trusted.
    assert level(levels, worktree / "sub") == "trusted"  # By its main checkout, as Codex does.
    assert level(levels, clone) == "untrusted"  # Under a trusted folder: not trusted by it.
    assert level(levels, elsewhere) == "untrusted"
    assert level(levels, tmp_path / "plain") == "trusted"  # Not a repository: its own entry.
    assert level(levels, tmp_path / "none") == "untrusted"
    # Not even the folder a trusted one is in, nor any above the project root.
    assert str(code) not in levels and str(tmp_path / "scratch") not in levels


def test_trust_follows_links_and_the_project_root(tmp_path):
    main = git_repository(tmp_path / "real" / "main", tmp_path / "wt")
    (tmp_path / "link").symlink_to(main)
    (tmp_path / "linkwt").symlink_to(tmp_path / "wt")
    owners = {str(main): {"trust_level": "trusted"}}
    levels = trust_levels(owners, [str(tmp_path / "link" / "sub"), str(tmp_path / "linkwt")])
    # Each folder up to its project root, under its own spelling and its real one.
    for folder in ("link", "link/sub", "linkwt"):
        assert level(levels, tmp_path / folder) == "trusted"
    assert level(levels, main / "sub") == "trusted"
    assert str(tmp_path) not in levels
    # Trusted only by a link's spelling: Codex looks up the real path first, then the link.
    by_link = trust_levels({str(tmp_path / "link"): {"trust_level": "trusted"}}, [str(main)])
    assert level(by_link, main) == "untrusted"
    linked = trust_levels(
        {str(tmp_path / "link"): {"trust_level": "trusted"}}, [str(tmp_path / "link")]
    )
    assert level(linked, tmp_path / "link") == level(linked, main) == "trusted"
    # The owner's own project root markers (none here: each folder is its own project root, so
    # no folder above it is read); and what Codex itself said of a folder wins.
    marked = trust_levels({str(main): {"trust_level": "trusted"}}, [str(main / "sub")], [])
    assert level(marked, main / "sub") == "trusted" and str(main) not in marked
    said = {str(main): "untrusted"}
    assert level(trust_levels(owners, [str(main)], None, said), main) == "untrusted"


def test_a_worktree_is_trusted_by_its_main_checkout_only_when_git_says_they_belong(tmp_path):
    main = git_repository(tmp_path / "main", tmp_path / "wt")
    owners = {str(main): {"trust_level": "trusted"}}
    assert level(trust_levels(owners, [str(tmp_path / "wt")]), tmp_path / "wt") == "trusted"
    # A `.git` file pointing at another repository's worktree folder does not borrow its trust.
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    (stranger / ".git").write_text((tmp_path / "wt" / ".git").read_text())
    assert level(trust_levels(owners, [str(stranger)]), stranger) == "untrusted"
    # Nor one that is a link, nor one naming a folder that is not git's worktrees folder.
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").symlink_to(tmp_path / "wt" / ".git")
    assert level(trust_levels(owners, [str(linked)]), linked) == "untrusted"
    odd = tmp_path / "odd"
    odd.mkdir()
    (odd / ".git").write_text(f"gitdir: {main / '.git'}\n")
    assert level(trust_levels(owners, [str(odd)]), odd) == "untrusted"
    # Git's own files saying so, but not in its worktrees folder: not one of its worktrees.
    admin = main / ".git" / "elsewhere" / "fake"
    admin.mkdir(parents=True)
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / ".git").write_text(f"gitdir: {admin}\n")
    (admin / "gitdir").write_text(f"{fake / '.git'}\n")
    (admin / "commondir").write_text("../..\n")
    assert level(trust_levels(owners, [str(fake)]), fake) == "untrusted"
    # A worktree whose `.git` is a link (to its own, moved): as Codex, no longer its repository's.
    git_repository_worktree(main, tmp_path / "wt2")
    moved = tmp_path / "moved-git"
    (tmp_path / "wt2" / ".git").rename(moved)
    (tmp_path / "wt2" / ".git").symlink_to(moved)
    assert level(trust_levels(owners, [str(tmp_path / "wt2")]), tmp_path / "wt2") == "untrusted"


def git_repository_worktree(main, worktree):
    import subprocess

    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(worktree)], check=True)


def test_a_folder_named_git_that_is_no_repository_is_no_project_root(tmp_path):
    main = git_repository(tmp_path / "main")
    (main / "inner" / ".git").mkdir(parents=True)  # No HEAD in it.
    levels = trust_levels({str(main): {"trust_level": "trusted"}}, [str(main / "inner")])
    assert level(levels, main / "inner") == level(levels, main) == "trusted"


async def test_what_codex_says_of_a_folders_trust_is_asked_for_and_wins(tmp_path, monkeypatch):
    """Prefer Codex's own answer (config/read with its layers): a `.codex/` folder it found and
    does not load is untrusted, whatever the owner's entries look like to Chief of Staff."""
    main = git_repository(tmp_path / "main")
    config = {"projects": {str(main): {"trust_level": "trusted"}}}
    layers = [
        {"name": {"type": "user"}, "disabledReason": None},
        {
            "name": {"type": "project", "dotCodexFolder": str(main / ".codex")},
            "disabledReason": "no",
        },
    ]
    monkeypatch.setenv("FAKE_CODEX_CONFIG", json.dumps(config))
    monkeypatch.setenv("FAKE_CODEX_LAYERS", json.dumps(layers))
    codex = CodexBackend(fake_codex(tmp_path / "fake", monkeypatch))
    await codex.start()
    try:
        told = await codex.thread_config({"folders": [str(main)]}, str(main))
    finally:
        await codex.stop()
    assert told["projects"][str(main)] == {"trust_level": "untrusted"}
    assert backend_codex.trust_answered([*layers, "odd", {"name": "odd"}]) == {
        str(main): "untrusted"
    }


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
    assert second == [
        "assistant.turn_start",
        "assistant.message",
        "agent.usage",
        "agent.settled",
        "session.idle",
    ]


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
        "features.apps": False,
        "mcp_servers.notes.default_tools_approval_mode": "prompt",
        "mcp_servers.notes.tools.write.approval_mode": "prompt",
    }


async def test_chatgpts_apps_are_off_for_a_worker_that_asks_first_as_read_back(
    tmp_path, monkeypatch
):
    """Review: an app's call (GitHub's, say) takes approval from Codex's own `apps` settings,
    not Chief of Staff's: a worker that asks first has none; one acting on its own keeps the
    owner's."""
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    codex = CodexBackend(fake_codex(tmp_path / "fake", monkeypatch))
    await codex.start()
    try:
        asking, _ = await opened(codex, tmp_path, ask_first=True)
        acting, _ = await opened(codex, tmp_path)
        features = {}
        for session in (asking, acting):
            listed = await codex.request(
                "experimentalFeature/list", {"threadId": session.session_id}
            )
            features[session] = {f["name"]: f["enabled"] for f in listed["data"]}["apps"]
        assert (features[asking], features[acting]) == (False, True)
        resumed = await codex.resume_session(asking.session_id, ask_first=True)
        assert resumed.session_id == asking.session_id
    finally:
        await codex.stop()
    # Kept on by an administrator: such a thread is not used (nor kept), and says why.
    monkeypatch.setenv("FAKE_CODEX_FEATURES", json.dumps({"apps": True}))
    codex = CodexBackend(fake_codex(tmp_path / "fake", monkeypatch))
    await codex.start()
    try:
        with pytest.raises(RuntimeError, match="did not turn ChatGPT's apps off"):
            await opened(codex, tmp_path, ask_first=True)
        with pytest.raises(RuntimeError, match="did not turn ChatGPT's apps off"):
            await codex.resume_session(asking.session_id, ask_first=True)
        assert (await opened(codex, tmp_path))[0].session_id, "acting on its own, it runs"
    finally:
        await codex.stop()
    methods = [json.loads(line)["method"] for line in log.read_text().splitlines()]
    assert methods.count("thread/archive") == 1 and "thread/unsubscribe" in methods


@pytest.mark.parametrize("ask_first", [True, False])
async def test_a_call_of_chatgpts_apps_is_checked_as_its_server_would_be(
    codex, tmp_path, ask_first
):
    asked = []

    def check(request, invocation):
        asked.append((request.server_name, request.read_only))
        return PermissionDecisionApproveOnce()

    session, _ = await opened(codex, tmp_path, on_permission_request=check, ask_first=ask_first)

    async def call(read_only=False, **meta):
        params = {
            "serverName": "codex_apps",
            "mode": "form",
            "_meta": {
                "codex_approval_kind": "mcp_tool_call",
                "tool_annotations": {"readOnlyHint": read_only},
                **meta,
            },
        }
        return (await session.elicitation(params, {"session_id": "x"}))["action"]

    # GitHub's, by its connector or its tool: as GitHub's MCP server (its writes go through
    # draft_github_review).
    assert await call(connector_id="connector_github", tool_name="add_comment") == "accept"
    assert await call(connector_name="GitHub", tool_name="add_comment") == "accept"
    assert await call(tool_name="github_create_review") == "accept"
    assert asked == [(GITHUB_MCP, False)] * 3
    asked.clear()
    # Another app: read-only, as any server's; a write, refused outright when asking first
    # (apps are off for it, so one that still asks is not let act).
    assert await call(True, connector_name="Calendar", tool_name="list_events") == "accept"
    expected = "decline" if ask_first else "accept"
    assert await call(connector_name="Calendar", tool_name="create_event") == expected
    assert asked == [("codex_apps", True)] + ([] if ask_first else [("codex_apps", False)])


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


async def test_a_child_codex_closed_keeps_nothing_else_running_after_a_stop(codex, tmp_path):
    """Review: a closed child's thread, asked first, made the stop end nothing (Codex has it no
    more), leaving the worker's own dev server running in about half the runs."""
    calls = []
    session, turn = await opened(codex, tmp_path, tools=[echo_tool(calls)])
    await session.send(
        "Leave the command `sleep 37` running. Have a child agent call the echo tool with text "
        "'x' then close it quietly. Then reply with just: started"
    )
    await until(lambda: "assistant.idle" in turn.kinds())
    assert len(session.child_threads) == 1
    assert await session.stop_background() == 1, "its own command, ended"
    assert not session.child_threads, "let go of: Codex has it no more"
    await turn.wait()
    await until(lambda: not any("sleep 37" in line for _, line in process_table().values()))
    assert await session.stop_background() == 0
    # Said closed, it is let go of at once.
    await session.send(
        "Have a child agent call the echo tool with text 'y' then close it. "
        "Then reply with just: done"
    )
    await turn.wait()
    assert not session.child_threads
    assert calls == [("x", session.session_id), ("y", session.session_id)]


@pytest.mark.parametrize("closed", ["a-closed", "z-closed"])
async def test_a_stop_ends_what_every_thread_it_can_ask_runs(tmp_path, closed):
    """Whichever order its threads come in, one Codex no longer has is skipped and let go of;
    the count is what was ended."""
    session = backend_codex.CodexSession(CodexBackend("codex"), "own", {})
    session.child_threads |= {closed, "m-live"}
    session.left_running["cmd"] = closed
    cleaned, notes = [], []
    session.on(notes.append)

    async def request(method, params):
        thread = params["threadId"]
        if thread == closed:
            raise backend_codex.CodexError(f"thread not found: {thread}")
        if method.endswith("list"):
            return {"data": [{"itemId": f"{thread}-1"}]}
        cleaned.append(thread)
        return {}

    session.backend.request = request  # type: ignore[method-assign]
    assert await session.stop_background() == 2
    assert cleaned == ["own", "m-live"]
    # One that cannot be ended now is not counted as ended.
    session.child_threads.add(closed)
    refusing = request

    async def request_refusing_clean(method, params):
        if method.endswith("clean") and params["threadId"] == "m-live":
            raise backend_codex.CodexError("busy")
        return await refusing(method, params)

    session.backend.request = request_refusing_clean  # type: ignore[method-assign]
    assert await session.stop_background() == 1
    assert session.child_threads == {"m-live"} and not session.left_running
    assert [n.data.kind.shell_id for n in notes if n.type.value == "system.notification"] == ["cmd"]


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


async def test_every_file_an_edit_writes_is_asked_about_a_moves_destination_too(codex, tmp_path):
    """Review: apply_patch's `*** Move to:` writes a file Chief of Staff was never asked about;
    refused, nothing of the edit is written."""
    asked = []
    theirs = tmp_path / "theirs"

    def check(request, invocation):
        asked.append(request.resolved_path)
        if request.resolved_path.startswith(str(theirs)):
            return PermissionDecisionReject(feedback="Not there.")
        return PermissionDecisionApproveOnce()

    work = tmp_path / "work"
    work.mkdir()
    for name in ("a.py", "b.py", "gone.py"):
        (work / name).write_text(name)
    session, turn = await opened(codex, work, on_permission_request=check)
    await session.send(
        f"Write the file `n1` and the file `n2`. Move the file `a.py` to `{theirs / 'a.py'}`. "
        "Move the file `b.py` to `c.py`. Delete the file `gone.py`. Then reply with just: done"
    )
    await turn.wait()
    assert asked == [
        *(str(work / name) for name in ("n1", "n2", "a.py")),
        str(theirs / "a.py"),
        *(str(work / name) for name in ("b.py", "c.py", "gone.py")),
    ]
    assert (work / "a.py").exists() and not theirs.exists(), "refused: not moved"
    assert (work / "c.py").read_text() == "b.py" and not (work / "b.py").exists()
    assert not (work / "gone.py").exists() and (work / "n1").exists()


@pytest.mark.parametrize("yolo", [True, False])
async def test_a_codex_worker_moves_no_file_where_it_may_not_write(tmp_path, monkeypatch, yolo):
    """Review: a move's destination escaped the owner's checkout (always kept, also when the
    worker acts on its own) and, with Ask first, the worker's folders."""
    from test_round7_split import published

    app = published(tmp_path, "app")
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "a.py").write_text("a")
    harness, runtime = await brief(
        tmp_path, monkeypatch, Settings(projects=[str(app)], worker_yolo=yolo)
    )
    try:
        if yolo:  # Its own checkout of the owner's project is the owner's: never written.
            source, target = loose / "a.py", app / "a.py"
            project, made = {"project": str(app)}, ""
        else:  # Asking first, it writes only in its own folders (tests run in the temp folder).
            monkeypatch.setattr("tempfile.gettempdir", lambda: "/no-temp-here")
            source, target, project = "a.py", loose / "moved.py", {}
            made = "Write the file `a.py` and the file `b.py`. "
        result = await call_tool(
            runtime,
            "spawn_worker",
            harness.router_id,
            title="Move it",
            instructions=f"{made}Move the file `{source}` to `{target}`. Report done: moved.",
            why_new="t",
            **project,
        )
        worker = json.loads(result.text_result_for_llm)["session_id"]
        why = "own checkout" if yolo else "outside"

        def refused():
            return [m for m in harness.store.messages(worker) if why in m["text"]]

        await until(lambda: refused() and harness.store.session(worker)["status"] == "done", 30)
        assert refused()[0]["kind"] == "notice"
        kept = Path(harness.store.session(worker)["cwd"]) / source
        assert not target.exists() and kept.read_text() in {"a", "x\n"}, "not moved"
        assert str(target) not in json.loads(harness.store.session(worker)["files"])
    finally:
        await runtime.stop()


async def test_a_codex_worker_records_where_it_moved_a_file(tmp_path, monkeypatch):
    harness, runtime = await brief(tmp_path, monkeypatch)
    try:
        worker = await spawn(
            runtime,
            harness,
            "Write the file `old.py` and the file `keep.py`. Move the file `old.py` to "
            "`new/name.py`. Report done: moved.",
        )
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        cwd = Path(harness.store.session(worker)["cwd"])
        assert (cwd / "new" / "name.py").exists() and not (cwd / "old.py").exists()
        assert {"old.py", "new/name.py"} <= set(json.loads(harness.store.session(worker)["files"]))
    finally:
        await runtime.stop()


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
    ("version", "runs", "asks_first"),
    [
        ("0.167.1", False, False),
        ("0.167.2-alpha.1", False, False),  # Before the release tested.
        ("0.167.2", True, True),
        ("0.167.9", True, True),
        ("0.168.0-alpha.1", True, False),
        ("0.168.0", True, False),
        ("1.0.0", True, False),
        ("", False, False),
        ("dev", False, False),
    ],
)
def test_which_codex_runs_and_which_runs_workers_that_ask_first(version, runs, asks_first):
    """The owner's decision: nothing older than tested; Ask first only before 0.168; newer
    versions run workers that act on their own; one whose version cannot be told, none."""
    assert (backend_codex.codex_refused(version) == "") is runs
    assert backend_codex.ask_first_tested(version) is asks_first
    if not runs:
        assert ("older than" if version.startswith("0.") else "could not tell") in (
            backend_codex.codex_refused(version)
        )


@pytest.mark.parametrize(
    ("env", "why"),
    [
        ({"FAKE_CODEX_VERSION": "0.167.1"}, "older than the versions tested"),
        ({"FAKE_CODEX_VERSION": "unknown"}, "could not tell which version of Codex"),
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


async def test_once_nothing_reads_codex_a_request_fails_at_once(codex, tmp_path):
    """Not after a minute (REQUEST_TIMEOUT): nothing would read its answer (Codex's output
    ended, or its reading was stopped while Codex still runs)."""
    codex.reader.cancel()
    await until(lambda: codex.reader.done())
    assert codex.process.returncode is None, "Codex itself still runs"
    started = asyncio.get_running_loop().time()
    with pytest.raises(ConnectionResetError):
        await codex.request("thread/backgroundTerminals/list", {"threadId": "t"})
    assert asyncio.get_running_loop().time() - started < 1


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
        worker_cwd = harness.store.session(worker)["cwd"]
        assert worker_cwd and options["folders"][0] == worker_cwd
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


def logged(log) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


async def test_each_codex_version_is_checked_once_before_its_workers_run(tmp_path, monkeypatch):
    """The owner's decision: the first time a Codex version starts, Chief of Staff checks what
    its checks rely on, offline (a stand-in model; Codex's own approvals), and remembers it."""
    log = tmp_path / "log.jsonl"
    servers = {"notes": {"command": "notes"}}
    harness, runtime = await brief(
        tmp_path,
        monkeypatch,
        FAKE_CODEX_LOG=str(log),
        FAKE_CODEX_CONFIG=json.dumps({"mcp_servers": servers}),
    )
    try:
        await runtime.connect_agent("codex")
        found = json.loads(harness.store.get("agentcheck:codex:0.167.2") or "{}")
        assert found == {"ran": True, "ask_first": "", "acting": ""}
        await runtime.disconnect_agent("codex")
        await runtime.connect_agent("codex")  # Remembered: not checked again.
        worker = await spawn(runtime, harness, "Report done: Fixed it.")
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
    finally:
        await runtime.stop()
    starts = [m["params"] for m in logged(log) if m["method"] == "thread/start"]
    (check,) = [p for p in starts if p.get("ephemeral")]
    assert len(starts) == 2, "the check's thread, then the worker's"
    config = check["config"]
    assert check["approvalPolicy"] == "untrusted" and config["features.apps"] is False
    assert config["mcp_servers.notes.enabled"] is False, "the owner's servers do not start"
    assert config["mcp_servers.chiefofstaff_check.default_tools_approval_mode"] == "prompt"
    assert "mcp_servers.notes.enabled" not in starts[1]["config"]
    assert not (tmp_path / "data" / "work" / "codex-check").exists(), "nothing of it is left"


@pytest.mark.parametrize(
    ("unasked", "missing", "acts"),
    [
        ("shell", "a command", True),
        ("mcp", "an MCP tool's call", True),
        ("patch", "a new file", False),
    ],
)
async def test_what_a_codex_check_finds_keeps_its_workers_from_running(
    tmp_path, monkeypatch, unasked, missing, acts
):
    harness, runtime = await brief(tmp_path, monkeypatch, FAKE_CODEX_UNASKED=unasked)
    try:
        await runtime.connect_agent("codex")
        found = json.loads(harness.store.get("agentcheck:codex:0.167.2") or "{}")
        assert f"did not ask first for {missing}" in found["ask_first"]
        # What it did unasked (a command, an edit) happened; the stand-in's MCP call leaves none.
        assert ("did what was refused" in found["ask_first"]) is (unasked != "mcp")
        assert bool(found["acting"]) is not acts
        row = harness.store.create_session("w1", WORKER, "Fix", "/tmp")
        asking = runtime.agent_block({**row, "yolo": "off"})
        assert "workers that ask first do not run on it" in asking or "no worker runs" in asking
        assert (runtime.agent_block({**row, "yolo": "on"}) == "") is acts
        if not acts:
            assert "keeps your checkouts unchanged" in runtime.agent_block({**row, "yolo": "on"})
    finally:
        await runtime.stop()


async def test_a_check_that_could_not_run_holds_only_ask_first_and_runs_again(
    tmp_path, monkeypatch
):
    def broken(work):
        raise OSError("no port free")

    model = backend_codex.codex_check.Model
    monkeypatch.setattr(backend_codex.codex_check, "Model", broken)
    harness, runtime = await brief(tmp_path, monkeypatch)
    try:
        await runtime.connect_agent("codex")
        assert harness.store.get("agentcheck:codex:0.167.2") is None, "tried again next time"
        assert not (tmp_path / "data" / "work" / "codex-check").exists()
        row = harness.store.create_session("w1", WORKER, "Fix", "/tmp")
        assert "could not run (no port free)" in runtime.agent_block({**row, "yolo": "off"})
        assert runtime.agent_block({**row, "yolo": "on"}) == ""
        monkeypatch.setattr(backend_codex.codex_check, "Model", model)
        await runtime.disconnect_agent("codex")
        await runtime.connect_agent("codex")
        assert json.loads(harness.store.get("agentcheck:codex:0.167.2") or "{}")["ran"] is True
        assert runtime.agent_block({**row, "yolo": "off"}) == ""
    finally:
        await runtime.stop()


def test_what_a_check_found_is_said_plainly():
    verdict = backend_codex.check_verdict
    every = ["tool", "shell", "mcp", "write added-1.marker", "write added-2.marker"]
    moves = ["write from.txt", "write moved.txt"]
    assert verdict("0.167.2", every + moves, []) == {"ran": True, "ask_first": "", "acting": ""}
    unmoved = verdict("0.167.2", every + moves[:1], ["moved.txt"])
    assert unmoved["ask_first"] == (
        "Chief of Staff's check of Codex 0.167.2 found that it did not ask first for a file "
        "moved (where to) and did what was refused (moved.txt): workers that ask first do not "
        "run on it."
    )
    assert "no worker runs on it" in unmoved["acting"]
    quiet = verdict("0.167.2", ["shell", "mcp", *every[3:], *moves], [])
    assert "did not ask first for a call of Chief of Staff's own tools" in quiet["ask_first"]
    assert quiet["acting"] == ""
    done = verdict("0.167.2", every + moves, ["server.marker"])
    assert done["acting"] == "" and "did what was refused (server.marker)" in done["ask_first"]


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

    def tables():
        return {r["name"] for r in harness.store.all("SELECT name FROM sqlite_master")}

    assert "agent_usage" not in tables(), "the release before's database, as it was"
    agents.configure(harness.store, "codex", model="gpt-test")
    assert "agent_usage" not in tables()
    agents.configure(harness.store, "codex", enabled=True)
    assert "agent_usage" in tables(), "its usage's own table, made when first turned on"
    assert agents.limit(harness.store, "codex") == 0
    for limit, kept in ((5000, 5000), (-1, 0), ("9", 0), (True, 0)):
        agents.configure(harness.store, "codex", limit=limit)
        assert agents.limit(harness.store, "codex") == kept
    await runtime.stop()


# Its usage: a ledger of its own, per accounting epoch, and the spend limit (more-agents.md 5.8)


def tokens(given: int, said: int = 0) -> dict:
    return {
        "inputTokens": given,
        "cachedInputTokens": given // 2,
        "cacheWriteInputTokens": 0,
        "outputTokens": said,
        "totalTokens": given + said,
    }


def update(runtime, row, epoch, total, thread="t", turn="u1", restored=None):
    data = SimpleNamespace(
        thread_id=thread,
        turn_id=turn,
        total=total,
        last={},
        model="gpt-test",
        child=thread != "t",
        epoch=epoch,
        restored=restored,
    )
    runtime.record_agent_usage(row, data)


def ledger(harness, session_id) -> list[tuple]:
    return [
        (r["kind"], r["tokens"], r["least"], r["input_tokens"], r["output_tokens"])
        for r in harness.store.all(
            "SELECT * FROM agent_usage WHERE session_id=? ORDER BY id", (session_id,)
        )
    ]


async def codex_worker(tmp_path, monkeypatch, **env):
    harness, runtime = await brief(tmp_path, monkeypatch, **env)
    harness.store.create_session("w1", WORKER, "Fix", "idle", cwd=str(tmp_path))
    harness.store.set("agent:w1", "codex")
    return harness, runtime, harness.store.session("w1")


async def test_codex_usage_counts_each_update_once_per_epoch(tmp_path, monkeypatch):
    """Each process of a session is an epoch: an update counts its difference from the one
    before; repeated and missed updates count once; a clean restart knows its offset, a crash
    leaves what was spent then unknown and its next first update a range."""
    harness, runtime, row = await codex_worker(tmp_path, monkeypatch)
    try:
        update(runtime, row, "p1", tokens(100, 10))
        update(runtime, row, "p1", tokens(100, 10))  # Said twice: once.
        update(runtime, row, "p1", tokens(300, 30))  # One missed: still counted.
        update(runtime, row, "p1", tokens(50, 5), thread="child")  # A child agent's: its own.
        assert ledger(harness, "w1") == [
            ("exact", 110, None, 100, 10),
            ("exact", 220, None, 200, 20),
            ("exact", 55, None, 50, 5),
        ]
        # A clean end (nothing spending): the next process's offset is what was last seen.
        await runtime.end_epochs("codex", SimpleNamespace(epoch="p2"))
        update(runtime, row, "p2", tokens(350, 30))
        # Or what the agent said it restored, when it says so.
        await runtime.end_epochs("codex", SimpleNamespace(epoch="p3"))
        update(runtime, row, "p3", tokens(400, 30), restored=tokens(340, 30))
        assert ledger(harness, "w1")[3:] == [("exact", 50, None, 50, 0), ("exact", 60, None, 60, 0)]
        # A crash in the middle of a turn: what it spent then is unknown, and the next first
        # update without an offset is a range (at most its totals).
        runtime.spending("w1", True)
        await runtime.end_epochs("codex", SimpleNamespace(epoch="p4"))
        update(runtime, row, "p4", tokens(420, 40))
        # Totals that go down within a process: as a crash.
        update(runtime, row, "p4", tokens(100, 0))
        assert ledger(harness, "w1")[5:] == [
            ("unknown", 0, None, 0, 0),
            ("range", 460, 30, 420, 40),
            ("unknown", 0, None, 0, 0),
            ("range", 100, 0, 100, 0),
        ]
        assert harness.store.agent_spent("w1") == 110 + 220 + 55 + 50 + 60 + 460 + 100
        assert harness.store.agent_unknown("w1")
        assert not harness.store.all("SELECT * FROM usage"), "never Copilot's usage"
        assert not harness.store.get("copilot_default_model")
    finally:
        await runtime.stop()


async def test_a_stop_between_a_ledger_row_and_its_epoch_neither_loses_nor_doubles(
    tmp_path, monkeypatch
):
    harness, runtime, row = await codex_worker(tmp_path, monkeypatch)
    try:
        update(runtime, row, "p1", tokens(100))
        dumps = json.dumps

        def stopped(value, *args, **kwargs):
            raise OSError("stopped here")

        monkeypatch.setattr("chiefofstaff.store.json.dumps", stopped)
        with pytest.raises(OSError, match="stopped here"):
            update(runtime, row, "p1", tokens(250))
        monkeypatch.setattr("chiefofstaff.store.json.dumps", dumps)
        assert ledger(harness, "w1") == [("exact", 100, None, 100, 0)], "its row went too"
        update(runtime, row, "p1", tokens(250))
        assert harness.store.agent_spent("w1") == 250
    finally:
        await runtime.stop()


async def test_codex_usage_counts_for_the_run_its_turn_is_on_also_after_its_report(
    tmp_path, monkeypatch
):
    harness, runtime = await brief(tmp_path, monkeypatch)
    try:
        worker = await spawn(
            runtime,
            harness,
            "Have a child agent call the report tool with text 'x'. Report done: Fixed it.",
        )
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        (run,) = runtime.tasks.runs(runtime.tasks.for_session(worker)[-1]["id"])
        assert run["ended_at"], "its report ended the run"
        rows = harness.store.all("SELECT * FROM agent_usage WHERE session_id=?", (worker,))
        # The report's own call spent after the report: still that run's.
        assert len(rows) == 2 and {r["run_id"] for r in rows} == {run["id"]}
        assert {r["agent"] for r in rows} == {"codex"} and {r["model"] for r in rows} == {
            "gpt-test"
        }
        assert not harness.store.all("SELECT * FROM usage WHERE session_id=?", (worker,))
        assert json.loads(harness.store.get(f"agentepoch:{worker}"))["busy"] is False
    finally:
        await runtime.stop()


async def test_a_codex_worker_stops_at_its_token_limit(tmp_path, monkeypatch):
    harness, runtime = await brief(tmp_path, monkeypatch)
    try:
        agents.configure(harness.store, "codex", limit=150)
        worker = await spawn(
            runtime,
            harness,
            "Run the shell command `echo 1`. Run the shell command `echo 2`. Wait 20 s. "
            "Then reply with just: done",
        )
        await until(lambda: "Stopped by its token limit." in str(harness.kinds(worker)), 30)
        notices = [text for kind, text in harness.kinds(worker) if kind == "notice"]
        assert "This worker used its Codex token limit (220 of 150)." in notices
        task = runtime.tasks.for_session(worker)[-1]
        assert task["status"] == "waiting" and harness.store.get(f"stopped:{task['id']}")
        assert harness.store.session(worker)["status"] == "idle"
        with pytest.raises(RuntimeError, match="token limit"):
            await runtime.start_agent_of(worker)
        agents.configure(harness.store, "codex", limit=10_000)
        assert runtime.agent_block(harness.store.session(worker)) == ""
    finally:
        await runtime.stop()


@pytest.mark.parametrize("limited", [True, False])
async def test_a_crash_mid_turn_leaves_its_spend_unknown_until_the_owner_goes_on(
    tmp_path, monkeypatch, limited
):
    harness, runtime = await brief(tmp_path, monkeypatch)
    try:
        if limited:
            agents.configure(harness.store, "codex", limit=1_000_000)
        worker = await spawn(
            runtime, harness, "Run the shell command `echo 1`. Wait 30 s. Then reply with just: on"
        )
        await until(lambda: harness.store.all("SELECT 1 FROM agent_usage"), 30)
        backend, _ = runtime.connection("codex")
        os.kill(backend.process.pid, signal.SIGKILL)  # Codex crashes mid-turn.
        await until(lambda: runtime.connection("codex")[0] not in {None, backend}, 30)
        assert ledger(harness, worker)[-1][0] == "unknown"
        task = runtime.tasks.for_session(worker)[-1]
        if not limited:
            await until(lambda: harness.store.session(worker)["status"] == "working", 30)
            assert runtime.agent_block(harness.store.session(worker)) == ""
            return
        await until(lambda: "Stopped by its token limit." in str(harness.kinds(worker)), 30)
        why = runtime.agent_block(harness.store.session(worker))
        assert "what this worker spent then is not known" in why and "Go on" in why
        assert any(why == text for _, text in harness.kinds(worker))
        await runtime.task_actions.follow_up(task["id"], "Go on where you stopped.")
        assert runtime.agent_block(harness.store.session(worker)) == ""
        await until(lambda: harness.store.session(worker)["status"] == "working", 30)
    finally:
        await runtime.stop()


async def test_a_turn_waits_for_what_the_last_one_spent(tmp_path, monkeypatch):
    harness, runtime, _ = await codex_worker(tmp_path, monkeypatch)
    try:
        harness.store.set("copilot:w1", "thread-w1")
        harness.store.set("alias:thread-w1", "w1")
        pending = [True, True, False]
        late = SimpleNamespace(session_id="w1", usage_pending=lambda: pending.pop(0))
        assert await runtime.admit_turn(late) == ""
        assert pending == [], "waited until it was said"
        assert json.loads(harness.store.get("agentepoch:w1"))["busy"] is True
        monkeypatch.setattr(runtime_module, "USAGE_SETTLES", 0.3)
        agents.configure(harness.store, "codex", limit=1000)
        never = SimpleNamespace(session_id="w1", usage_pending=lambda: True)
        assert "not known" in await runtime.admit_turn(never), "a minute without it: unknown"
        assert ledger(harness, "w1") == [("unknown", 0, None, 0, 0)]
    finally:
        await runtime.stop()


@pytest.mark.parametrize("less", ["0", "50"])
async def test_a_clean_restart_counts_on_from_what_codex_restored(tmp_path, monkeypatch, less):
    """Codex restores a thread's totals when it resumes it in a new process (said before the
    resume's answer); less than was spent too: the new process counts from what it restored."""
    harness, runtime = await brief(tmp_path, monkeypatch, FAKE_CODEX_RESTORES_LESS=less)
    try:
        worker = await spawn(runtime, harness, "Run the shell command `echo 1`. Report done: one.")
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        await until(lambda: json.loads(harness.store.get(f"agentepoch:{worker}"))["busy"] is False)
        await runtime.disconnect_agent("codex")
        await runtime.connect_agent("codex")
        task = runtime.tasks.for_session(worker)[-1]
        await runtime.task_actions.follow_up(
            task["id"], "Run the shell command `echo 2`. Report done: two."
        )
        await until(lambda: len(ledger(harness, worker)) == 4, 30)
        assert ledger(harness, worker) == [("exact", 110, None, 100, 10)] * 4
        assert not harness.store.agent_unknown(worker)
    finally:
        await runtime.stop()


async def test_codex_workers_need_macos_or_linux_for_now(tmp_path, monkeypatch):
    """Windows (experimental since 0.1.42) runs Copilot's workers only, for now: nothing of
    Codex starts there, and its workers say why."""
    harness, runtime, row = await codex_worker(tmp_path, monkeypatch)
    try:
        why = "Codex workers need macOS or Linux for now."
        with monkeypatch.context() as windows:
            windows.setattr(sys, "platform", "win32")
            assert runtime.agent_block(row) == why
            with pytest.raises(RuntimeError, match=why):
                await runtime.connect_agent("codex")
            with pytest.raises(RuntimeError, match=why):
                await runtime.start_agent_of("w1")
            assert runtime.agent_block(harness.store.session(harness.router_id)) == ""
        assert runtime.connection("codex") == (None, 0), "never started"
        assert runtime.agent_block(row) == ""
    finally:
        await runtime.stop()


async def test_what_codex_said_before_a_turn_is_admitted_is_counted_first(tmp_path, monkeypatch):
    """Its events reach the runtime a moment after the adapter has them (Runtime.open_live's
    forward): the usage Codex said before the turn's end is recorded before the next turn's
    admission."""
    harness, runtime, _ = await codex_worker(tmp_path, monkeypatch)
    try:
        agents.configure(harness.store, "codex", limit=100)
        harness.store.set("copilot:w1", "thread-w1")
        harness.store.set("alias:thread-w1", "w1")
        data = SimpleNamespace(
            thread_id="t", turn_id="u", total=tokens(150), model="m", epoch="p1", restored=None
        )
        event = SimpleNamespace(type=SimpleNamespace(value="agent.usage"), data=data)
        asyncio.get_running_loop().call_soon(runtime.events.on_event, "w1", event)
        why = await runtime.admit_turn(SimpleNamespace(session_id="thread-w1"))
        assert why == "This worker used its Codex token limit (150 of 100)."
    finally:
        await runtime.stop()


def test_the_checks_stand_in_model_answers_each_step_in_turn(tmp_path):
    """What real Codex talks to in the check (the fake CLI never does): one answer a request,
    the next action once the one before was answered, then the reply."""
    import urllib.request

    from chiefofstaff import codex_check

    model = codex_check.Model(tmp_path)
    try:
        arguments = model.arguments(tmp_path)
        assert json.loads((tmp_path / "catalog.json").read_text())["models"][0]["slug"] == (
            codex_check.MODEL
        )
        assert f'model="{codex_check.MODEL}"' in arguments

        def ask(outputs: int) -> dict:
            body = {"input": [{"type": "function_call_output"}] * outputs + ["odd"]}
            url = f"http://127.0.0.1:{model.server_address[1]}/v1/responses"
            request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
            with urllib.request.urlopen(request, timeout=10) as answer:  # noqa: S310
                events = [
                    json.loads(line.removeprefix("data: "))
                    for line in answer.read().decode().splitlines()
                    if line.startswith("data: ")
                ]
            assert [e["type"] for e in events] == [
                "response.created",
                "response.output_item.done",
                "response.completed",
            ]
            return events[1]["item"]

        names = [ask(n).get("name") for n in range(5)]
        assert (
            names
            == [codex_check.TOOL, "exec_command", codex_check.SERVER_TOOL] + ["apply_patch"] * 2
        )
        assert "*** Move to:" in ask(4)["input"] and ask(5)["type"] == "message"
        assert ask(9)["type"] == "message", "past its steps: its reply"
    finally:
        model.close()


def test_the_checks_stand_in_mcp_server_writes_only_when_its_tool_is_called(tmp_path):
    import subprocess

    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "x"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
    ]
    feed = "\n".join(json.dumps(line) for line in lines) + "\nnot json\n"
    ran = subprocess.run(
        [sys.executable, "-m", "chiefofstaff.codex_check", "--mcp", str(tmp_path)],
        input=feed,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    answers = [json.loads(line) for line in ran.stdout.splitlines()]
    assert [a["id"] for a in answers] == [1, 2, 3, 4]
    assert answers[0]["result"]["protocolVersion"] == "x"
    assert answers[1]["result"]["tools"][0]["annotations"] == {"readOnlyHint": False}
    assert answers[3]["error"]["code"] == -32601
    assert not (tmp_path / "server.marker").exists()
    called = json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {}})
    subprocess.run(
        [sys.executable, "-m", "chiefofstaff.codex_check", "--mcp", str(tmp_path)],
        input=called + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert (tmp_path / "server.marker").exists()
