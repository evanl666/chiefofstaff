"""Sign in and Install from the page (design/agent-smooth.md): Chief of Staff runs the agent's
own CLI for the owner (the stand-ins: tests/fake_codex.py, tests/fake_claude.py), shows what it
says (the page's link, a one-time code, a code to paste), sees it signed in by itself, and the
work it held goes on once, by itself, never twice and never what had already run."""

import asyncio
import json
import os
import re
import shlex
import sys
import time
from importlib import metadata
from pathlib import Path

import pytest
from conftest import Harness, call_tool
from test_agent_picker import started, until
from test_agents import ran, saved, spawn_on

from chiefofstaff import agents, agentsetup, backend_claude, claude_setup, task_actions
from chiefofstaff import runtime as runtime_module
from chiefofstaff.roles import WORKER
from chiefofstaff.store import Store


def approve(said: str = "ok", folder: Path | None = None):
    """The owner finishing a stand-in's sign-in in their browser (or failing to)."""
    home = folder or Path(os.environ["FAKE_CODEX_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "approve").write_text(said)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def quick(monkeypatch):
    """Sign-ins looked at often, and held work resumed soon, for tests."""
    monkeypatch.setattr(agentsetup, "POLL_SECONDS", 0.1)
    monkeypatch.setattr(runtime_module, "RESUME_SECONDS", 0.3)
    monkeypatch.setattr(runtime_module, "UNREADY_SECONDS", 0.0)


async def test_codex_signs_in_from_the_page_and_its_row_turns_ready_by_itself(
    tmp_path, codex_installed, monkeypatch, quick
):
    _, runs = codex_installed
    monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
    _, runtime = await started(tmp_path)
    try:
        await runtime.refresh_agent_status()
        status = runtime.agent_status("codex")
        assert status["status"] == agents.SIGN_IN
        assert [way["method"] for way in status["ways"]] == ["browser", "device", "key"]
        view = await runtime.setups.sign_in("codex")
        assert view["state"] == "running" and view["method"] == "browser"
        await until(lambda: runtime.setups.view()["codex"].get("url"))
        shown = runtime.snapshot()["agent_setups"]["codex"]
        # Its page's link as Codex printed it on stderr; not its local server's.
        assert shown["url"].startswith("https://auth.openai.com/oauth/authorize?")
        assert "Starting local login server on http://localhost:1455." in shown["tail"]
        assert runtime.agent_status("codex")["status"] == agents.SIGN_IN, "not yet"
        approve()
        await until(lambda: runtime.setups.view()["codex"]["state"] == "done")
        status = runtime.agent_status("codex")
        assert status["status"] == agents.READY
        assert status["pays"] == "Uses your ChatGPT sign-in"
        assert "login" in ran(runs), "its own CLI, unmodified"
    finally:
        await runtime.stop()


async def test_a_sign_in_runs_as_workers_run_the_agent(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/owner/codex")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/owner/claude")
    monkeypatch.setenv("CHIEFOFSTAFF_LAUNCH_KEY", "secret")
    codex, claude = agentsetup.environment("codex"), agentsetup.environment("claude")
    assert codex["CODEX_HOME"] == "/owner/codex" and claude["CLAUDE_CONFIG_DIR"] == "/owner/claude"
    assert not any(key.startswith("CHIEFOFSTAFF_") for key in {**codex, **claude})
    assert claude["CLAUDE_CODE_FULL_SIGN_IN_LINK"] == "1", "its whole link, readable"


async def test_a_failed_sign_in_says_why_and_a_cancelled_one_leaves_nothing_running(
    tmp_path, codex_installed, monkeypatch, quick
):
    monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
    _, runtime = await started(tmp_path)
    try:
        await runtime.setups.sign_in("codex")
        await until(lambda: runtime.setups.view()["codex"].get("url"))
        approve("access denied")
        await until(lambda: runtime.setups.view()["codex"]["state"] == "failed")
        assert runtime.setups.view()["codex"]["error"] == "Error logging in: access denied"
        assert runtime.agent_status("codex")["status"] == agents.SIGN_IN
        # Again, then Cancel: its process ends.
        await runtime.setups.sign_in("codex")
        run = runtime.setups.runs["codex"]
        await until(lambda: run.url)
        pid = run.process.pid
        view = await runtime.setups.cancel("codex")
        assert view["state"] == "cancelled"
        await until(lambda: not alive(pid), 5)
        await asyncio.sleep(0.3)
        assert runtime.setups.view()["codex"]["state"] == "cancelled", "not failed after"
    finally:
        await runtime.stop()


async def test_a_one_time_code_for_another_device_is_shown(
    tmp_path, codex_installed, monkeypatch, quick
):
    monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
    _, runtime = await started(tmp_path)
    try:
        await runtime.setups.sign_in("codex", "device")
        await until(lambda: runtime.setups.view()["codex"].get("code"))
        view = runtime.setups.view()["codex"]
        assert (view["url"], view["code"]) == ("https://auth.openai.com/codex/device", "FAKE-12345")
        assert not any("\x1b" in line for line in view["tail"]), "without its colours"
        approve()
        await until(lambda: runtime.setups.view()["codex"]["state"] == "done")
    finally:
        await runtime.stop()


async def test_an_api_key_goes_to_codex_on_its_stdin_and_is_kept_nowhere(
    tmp_path, codex_installed, monkeypatch, quick
):
    _, runs = codex_installed
    monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
    harness, runtime = await started(tmp_path)
    key = "stand-in-key-" + "x" * 12  # (Not a key: the stand-in keeps whatever it reads.)
    try:
        with pytest.raises(ValueError, match="Paste your API key"):
            await runtime.setups.sign_in("codex", "key")
        await runtime.setups.sign_in("codex", "key", key=key)
        await until(lambda: runtime.setups.view()["codex"]["state"] == "done")
        assert (Path(os.environ["FAKE_CODEX_HOME"]) / "api-key").read_text() == key, "its stdin"
        assert runtime.agent_status("codex")["pays"] == "Uses your OpenAI API key"
        assert key not in runs.read_text(), "never on its command line"
        assert key not in json.dumps(runtime.snapshot()), "nor in the page"
        dump = "\n".join(harness.store.db.iterdump())
        assert key not in dump, "nor kept"
    finally:
        await runtime.stop()


async def test_signing_in_again_asks_first_as_codex_signs_out_first(
    tmp_path, codex_installed, quick
):
    _, runs = codex_installed
    _, runtime = await started(tmp_path)
    try:
        await runtime.refresh_agent_status()
        assert runtime.agent_status("codex")["status"] == agents.READY
        with pytest.raises(agentsetup.SignInAgainError, match="signs you out of Codex first"):
            await runtime.setups.sign_in("codex")
        assert "login" not in ran(runs), "nothing run before the owner says so"
        await runtime.setups.sign_in("codex", confirm=True)
        await until(lambda: "login" in ran(runs))
        await runtime.setups.cancel("codex")
    finally:
        await runtime.stop()


async def test_claude_asks_for_a_pasted_code_and_takes_it_on_its_stdin(
    tmp_path, claude_on_path, monkeypatch, quick
):
    monkeypatch.setenv("FAKE_CLAUDE_SIGNED_OUT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_PASTE", "pasted-code-1")
    _, runtime = await started(tmp_path)
    try:
        await runtime.refresh_agent_status()
        status = runtime.agent_status("claude")
        assert status["status"] == agents.SIGN_IN
        assert [way["method"] for way in status["ways"]] == ["browser", "console"]
        await runtime.setups.sign_in("claude")
        await until(lambda: runtime.setups.view()["claude"].get("asks_code"))
        view = runtime.setups.view()["claude"]
        assert view["url"].startswith("https://claude.ai/oauth/authorize?")
        await runtime.setups.send("claude", "wrong")
        await until(lambda: runtime.setups.view()["claude"]["state"] == "failed")
        assert runtime.setups.view()["claude"]["error"] == "Login failed: Invalid code"
        await runtime.setups.sign_in("claude")
        await until(lambda: runtime.setups.view()["claude"].get("asks_code"))
        await runtime.setups.send("claude", "pasted-code-1")
        await until(lambda: runtime.setups.view()["claude"]["state"] == "done")
        status = runtime.agent_status("claude")
        assert (status["status"], status["pays"]) == (agents.READY, "Uses your Claude plan")
    finally:
        await runtime.stop()


async def test_claudes_sign_in_end_and_a_key_in_the_environment_are_said(
    tmp_path, claude_on_path, monkeypatch
):
    ends = time.time() + 3 * 86400
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", json.dumps({"loginExpiresAt": int(ends * 1000)}))
    _, runtime = await started(tmp_path)
    try:
        await runtime.refresh_agent_status()
        status = runtime.agent_status("claude")
        assert status["status"] == agents.READY and abs(status["expires"] - ends) < 1
        assert "key_env" not in status
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        await runtime.glance_agent("claude")
        status = runtime.agent_status("claude")
        assert status["pays"] == "Uses your Anthropic API key (ANTHROPIC_API_KEY)"
        assert status["key_env"] == "ANTHROPIC_API_KEY"
        assert "sk-ant-test" not in json.dumps(runtime.snapshot())
    finally:
        await runtime.stop()


async def test_an_access_token_codex_uses_over_its_sign_in_is_said_by_name_and_keys_it_leaves_not(
    tmp_path, codex_installed, monkeypatch, quick
):
    """Codex's app-server, as its workers run it, uses CODEX_ACCESS_TOKEN from the environment
    over the sign-in `codex login` keeps (0.167.2: login/src/auth/manager.rs, load_auth): its
    status says so by the variable's name, in its glance and in Settings' look, never its value.
    CODEX_API_KEY (read by `codex exec` alone) and OPENAI_API_KEY (no sign-in of Codex's own
    provider) change nothing it says."""
    secrets = {"CODEX_API_KEY": "sk-exec-only-1", "OPENAI_API_KEY": "sk-not-codex-2"}
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    _, runtime = await started(tmp_path)
    try:
        await runtime.refresh_agent_status()
        status = runtime.agent_status("codex")
        assert status["pays"] == "Uses your ChatGPT sign-in" and "key_env" not in status
        monkeypatch.setenv("CODEX_ACCESS_TOKEN", secrets.setdefault("token", "codex-pat-3"))
        await runtime.glance_agent("codex")
        status = runtime.agent_status("codex")
        assert status["pays"] == "Uses your access token (CODEX_ACCESS_TOKEN)", status
        assert status["key_env"] == "CODEX_ACCESS_TOKEN"
        found = await runtime.look_for_agents()
        (codex,) = [entry for entry in found["agents"] if entry["name"] == "codex"]
        assert codex["pays"] == "Uses your access token (CODEX_ACCESS_TOKEN)", "Settings' look"
        shown = json.dumps(runtime.snapshot()) + json.dumps(found)
        assert not [value for value in secrets.values() if value in shown], "by name only"
    finally:
        await runtime.stop()


async def test_signed_in_in_a_terminal_meanwhile_turns_ready_when_the_page_asks(
    tmp_path, codex_installed, monkeypatch, quick
):
    monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
    _, runtime = await started(tmp_path)
    try:
        await runtime.refresh_agent_status()
        assert runtime.agent_status("codex")["status"] == agents.SIGN_IN
        monkeypatch.delenv("FAKE_CODEX_SIGNED_OUT")
        found = {status["name"]: status for status in await runtime.refresh_agent_status()}
        assert found["codex"]["status"] == agents.READY, "at once: no 30 seconds' wait"
    finally:
        await runtime.stop()


async def test_install_runs_the_official_installer_finds_it_and_goes_on_to_sign_in(
    tmp_path, monkeypatch, quick
):
    """The installer puts `codex` in ~/.local/bin, which is not on the PATH Chief of Staff
    started with: found there, put on its PATH (after the owner's own: a `git` beside it is not
    the one workers run), kept for next time; signed out, it says Sign-in needed (no longer Not
    installed, as the start that held work found it) and Sign in follows at once; signed in,
    the work held goes on, once."""
    import shutil

    from conftest import fake_codex

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
    stand_in = fake_codex(tmp_path / "fake", monkeypatch)
    bin_dir = home / ".local" / "bin"
    script = (
        f'echo "Installing Codex…"; mkdir -p "{bin_dir}" && cp "{stand_in}" "{bin_dir}/codex"'
        f' && printf "#!/bin/sh\\n" > "{bin_dir}/git" && chmod +x "{bin_dir}/git"'
    )
    git = shutil.which("git")
    monkeypatch.setattr(agentsetup, "INSTALLERS", {"codex": (("script", script, ""),),
                                                   "claude": ()})  # fmt: skip
    monkeypatch.setattr(agentsetup, "known_dirs", lambda: [bin_dir])
    monkeypatch.setattr(agentsetup, "npm_bin", lambda: asyncio.sleep(0, None))
    harness, runtime = await started(tmp_path)
    try:
        status = runtime.agent_status("codex")
        assert status["status"] == agents.MISSING
        assert status["install"] == [{"method": "script", "command": script}], "word for word"
        await saved(runtime, {"codex": {"enabled": True}})
        refused = await spawn_on(harness, runtime, title="Count the rows · data",
                                 instructions="Report done: 42 rows.", agent="codex")  # fmt: skip
        assert "it starts there by itself" in refused.text_result_for_llm
        (card,) = [
            m for m in harness.store.messages(harness.router_id) if m["meta"].get("agent_refused")
        ]
        assert card["meta"]["status"] == agents.MISSING
        await runtime.setups.install("codex")
        await until(lambda: runtime.setups.runs["codex"].kind == "signin", 20)
        assert runtime.agent_status("codex")["status"] == agents.SIGN_IN
        assert str(bin_dir) in os.environ["PATH"].split(os.pathsep)
        assert shutil.which("git") == git, "the owner's own, first on their PATH"
        assert harness.store.get("agentpath:codex") == str(bin_dir)
        assert runtime.agents_found() == ["codex"]
        await until(lambda: runtime.setups.view()["codex"].get("url"))
        approve()
        await until(lambda: runtime.agent_status("codex")["status"] == agents.READY)
        await until(lambda: harness.store.open_sessions(WORKER), 10)
        assert harness.store.message(card["id"])["meta"]["resumed"] == "Resumed once installed"
        assert len(harness.store.open_sessions(WORKER)) == 1
    finally:
        await runtime.stop()


async def test_an_agent_turned_off_or_stopped_while_it_starts_is_not_left_running(
    tmp_path, codex_installed, monkeypatch, quick
):
    """Turned off in Settings while its backend starts (its check under way): that start ends
    stopped, never left running for an agent that is off (ui_edges' round_agent_smooth: Claude,
    off, still said Ready from it). So with its backend disconnected while it starts (Chief of
    Staff stopping it): a backend begun before what stopped it is not kept."""
    _, runtime = await started(tmp_path)
    checking, go, begun = asyncio.Event(), asyncio.Event(), []
    check = runtime.check_agent

    async def slow_check(agent, backend):
        begun.append(backend)
        checking.set()
        await go.wait()
        return await check(agent, backend)

    monkeypatch.setattr(runtime, "check_agent", slow_check)

    async def overtaken_by(stop) -> str:
        checking.clear()
        go.clear()
        starting = asyncio.ensure_future(runtime.connect_agent("codex"))
        await asyncio.wait_for(checking.wait(), 20)
        await stop()
        go.set()
        with pytest.raises(RuntimeError) as raised:
            await starting
        assert runtime.connection("codex")[0] is None
        await until(lambda: not begun[-1].alive(), 10)
        return str(raised.value)

    try:
        await saved(runtime, {"codex": {"enabled": True}})
        said = await overtaken_by(lambda: saved(runtime, {"codex": {"enabled": False}}))
        assert said == "Codex is turned off in Settings → Agents & models."
        await saved(runtime, {"codex": {"enabled": True}})
        said = await overtaken_by(lambda: runtime.disconnect_agent("codex"))
        assert said == "Codex was stopped while it started."
        go.set()
        assert await runtime.connect_agent("codex") is runtime.connection("codex")[0], "then on"
    finally:
        await runtime.stop()


async def test_a_sign_in_still_running_ends_as_chief_of_staff_stops(
    tmp_path, codex_installed, monkeypatch, quick
):
    """Chief of Staff stopped while a sign-in waits for the owner: its process ends with it (as
    Cancel ends it), never left waiting on its local port."""
    monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
    _, runtime = await started(tmp_path)
    await runtime.setups.sign_in("codex")
    await until(lambda: runtime.setups.view()["codex"].get("url"))
    process = runtime.setups.runs["codex"].process
    assert process is not None
    await runtime.stop()
    await until(lambda: not alive(process.pid), 10)
    assert runtime.setups.runs["codex"].state == "cancelled"


async def test_a_failed_install_says_its_last_words(tmp_path, monkeypatch, quick):
    script = "echo 'curl: (6) Could not resolve host: chatgpt.com' >&2; exit 6"
    monkeypatch.setattr(agentsetup, "INSTALLERS", {"codex": (("script", script, ""),),
                                                   "claude": ()})  # fmt: skip
    _, runtime = await started(tmp_path)
    try:
        await runtime.setups.install("codex")
        await until(lambda: runtime.setups.view()["codex"]["state"] == "failed")
        assert runtime.setups.view()["codex"]["error"] == (
            "curl: (6) Could not resolve host: chatgpt.com"
        )
        with pytest.raises(ValueError, match="not a way to install"):
            await runtime.setups.install("codex", "brew")
    finally:
        await runtime.stop()


async def test_work_held_for_a_sign_in_starts_by_itself_once_signed_in_and_only_once(
    tmp_path, codex_installed, monkeypatch, quick
):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        await runtime.disconnect_agent("codex")
        refused = await spawn_on(harness, runtime, title="Count the rows · data",
                                 instructions="Report done: 42 rows.", agent="codex")  # fmt: skip
        assert "it starts there by itself" in refused.text_result_for_llm
        (card,) = [
            m for m in harness.store.messages(harness.router_id) if m["meta"].get("agent_refused")
        ]
        assert card["text"] == (
            "Codex needs you to sign in. Your message is saved: “Count the rows · data” starts "
            "on Codex once you have."
        )
        assert card["meta"]["status"] == agents.SIGN_IN
        key = card["meta"]["agent_refused"]
        await runtime.setups.sign_in("codex")
        await until(lambda: runtime.setups.view()["codex"].get("url"))
        approve()
        # Resuming… (with Cancel) first, then it starts there.
        await until(lambda: harness.store.message(card["id"])["meta"].get("resuming"))
        assert harness.store.open_sessions(WORKER) == []
        await until(lambda: harness.store.open_sessions(WORKER), 10)
        (worker,) = harness.store.open_sessions(WORKER)
        assert runtime.agent(worker["id"]) == "codex" and worker["title"] == "Count the rows · data"
        meta = harness.store.message(card["id"])["meta"]
        assert meta["resumed"] == "Resumed after sign-in" and "resuming" not in meta
        assert "agent_refusals" not in runtime.snapshot()
        said = [
            m["text"].split(" · new because")[0]
            for m in harness.store.messages(harness.router_id)
            if m["kind"] == "notice" and m["text"].startswith("Started")
        ]
        assert said == ["Started “Count the rows · data” on Codex: resumed after sign-in"]
        await until(lambda: harness.store.all("SELECT id FROM handled"))
        (handled,) = harness.store.all("SELECT kind, text, session_id FROM handled")
        assert handled["kind"] == "resumed" and handled["session_id"] == worker["id"]
        assert handled["text"] == "Started “Count the rows · data” on Codex once you signed in"
        # Seen able to run again (a look, a restart): nothing more starts.
        runtime.ready_seen.clear()
        await runtime.glance_agent("codex")
        await asyncio.sleep(0.6)
        assert len(harness.store.open_sessions(WORKER)) == 1, "once"
        with pytest.raises(ValueError, match="answered already"):
            await runtime.answer_refusal(key, "now")
    finally:
        await runtime.stop()


async def test_work_held_for_a_check_that_could_not_run_goes_on_once_it_has(
    tmp_path, codex_installed, monkeypatch, quick
):
    """Work named for Codex while Chief of Staff's check of it could not run (a machine busy
    with other work) is held, not refused: its card says so, and that the check is tried again
    by itself; once it has run, the work goes on there, once, under Handled for you."""
    from chiefofstaff import backend_codex
    from chiefofstaff.models import Settings

    late = "Chief of Staff's check of Codex 0.167.2 could not finish (step 'turn' took too long)"
    checks = [0]
    original = backend_codex.CodexAgent.check

    async def check(self, folder):
        checks[0] += 1
        if checks[0] == 1:
            return {
                "ran": False,
                "unrun": late,
                "ask_first": f"{late}: workers that ask first do not run on it until it does.",
                "acting": "",
            }
        return await original(self, folder)

    monkeypatch.setattr(backend_codex.CodexAgent, "check", check)
    monkeypatch.setattr(runtime_module, "CHECK_AGAIN", (1.0,) * 5, raising=False)
    harness, runtime = await started(tmp_path, Settings(worker_yolo=False))
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        await runtime.connect_agent("codex")
        refused = await spawn_on(harness, runtime, title="Count the rows · data",
                                 instructions="Report done: 42 rows.", agent="codex")  # fmt: skip
        assert "once Chief of Staff's check of it has run" in refused.text_result_for_llm
        (card,) = [
            m for m in harness.store.messages(harness.router_id) if m["meta"].get("agent_refused")
        ]
        assert card["text"] == (
            f"{late}; it's tried again by itself. Your message is saved: “Count the rows · data” "
            "starts on Codex once it has run."
        )
        assert card["meta"]["status"] == agents.UNCHECKED
        assert harness.store.open_sessions(WORKER) == [], "held, nothing started"
        await until(lambda: harness.store.open_sessions(WORKER), 15)
        (worker,) = harness.store.open_sessions(WORKER)
        assert runtime.agent(worker["id"]) == "codex" and checks[0] == 2
        meta = harness.store.message(card["id"])["meta"]
        assert meta["resumed"] == "Resumed once Chief of Staff's check of it ran"
        await until(lambda: harness.store.all("SELECT id FROM handled"))
        (handled,) = harness.store.all("SELECT kind, text FROM handled")
        assert handled["text"] == (
            "Started “Count the rows · data” on Codex once Chief of Staff's check of it ran"
        )
        await asyncio.sleep(1.5)
        assert len(harness.store.open_sessions(WORKER)) == 1, "once"
    finally:
        await runtime.stop()


async def test_start_on_a_card_held_for_a_check_tries_the_check_now(
    tmp_path, codex_installed, monkeypatch, quick
):
    """The owner's Start on Codex, on work held for a check that could not run: the check is
    tried now (not a minute after the last try), and the work starts once it has run."""
    from chiefofstaff import backend_codex
    from chiefofstaff.models import Settings

    late = "Chief of Staff's check of Codex 0.167.2 could not finish (step 'turn' took too long)"
    checks = [0]
    original = backend_codex.CodexAgent.check

    async def check(self, folder):
        checks[0] += 1
        if checks[0] == 1:
            said = f"{late}: workers that ask first do not run on it until it does."
            return {"ran": False, "unrun": late, "ask_first": said, "acting": ""}
        return await original(self, folder)

    monkeypatch.setattr(backend_codex.CodexAgent, "check", check)
    harness, runtime = await started(tmp_path, Settings(worker_yolo=False))
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        await runtime.connect_agent("codex")
        await spawn_on(harness, runtime, title="Count the rows · data",
                       instructions="Report done: 42 rows.", agent="codex")  # fmt: skip
        (card,) = [
            m for m in harness.store.messages(harness.router_id) if m["meta"].get("agent_refused")
        ]
        await runtime.answer_refusal(card["meta"]["agent_refused"], "now")
        assert checks[0] == 2, "tried now"
        (worker,) = harness.store.open_sessions(WORKER)
        assert runtime.agent(worker["id"]) == "codex"
        assert harness.store.message(card["id"])["meta"]["resumed"] == "Started when you asked"
    finally:
        await runtime.stop()


async def test_cancel_while_resuming_keeps_it_for_the_owner_to_start(
    tmp_path, codex_installed, monkeypatch, quick
):
    monkeypatch.setattr(runtime_module, "RESUME_SECONDS", 2.0)
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        await runtime.disconnect_agent("codex")
        await spawn_on(harness, runtime, title="Count the rows · data",
                       instructions="Report done: 42 rows.", agent="codex")  # fmt: skip
        (card,) = [
            m for m in harness.store.messages(harness.router_id) if m["meta"].get("agent_refused")
        ]
        key = card["meta"]["agent_refused"]
        monkeypatch.delenv("FAKE_CODEX_SIGNED_OUT")  # Signed in in a terminal.
        await runtime.refresh_agent_status()
        await until(lambda: harness.store.message(card["id"])["meta"].get("resuming"))
        await runtime.answer_refusal(key, "keep")
        await asyncio.sleep(2.3)
        assert harness.store.open_sessions(WORKER) == [], "Cancel: it waits for the owner"
        assert harness.store.message(card["id"])["meta"]["kept"] is True
        assert runtime.snapshot()["agent_refusals"] == [key]
        runtime.ready_seen.clear()
        await runtime.glance_agent("codex")
        await asyncio.sleep(0.3)
        assert harness.store.open_sessions(WORKER) == [], "not by itself again"
        await runtime.answer_refusal(key, "now")
        (worker,) = harness.store.open_sessions(WORKER)
        assert runtime.agent(worker["id"]) == "codex"
    finally:
        await runtime.stop()


async def test_a_message_held_for_a_workers_sign_in_reaches_it_once_signed_in(
    tmp_path, codex_installed, monkeypatch, quick
):
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        made = await spawn_on(harness, runtime, title="Fix it", instructions="Report done: Fixed.",
                              agent="codex")  # fmt: skip
        worker = made["session_id"]
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        await runtime.disconnect_agent("codex")
        refused = await call_tool(
            runtime, "message_session", harness.router_id, session_id=worker,
            text="Also count the rows. Report done: 42 rows.", task="new",
            title="Count the rows · data", why_new="Another deliverable.",
        )  # fmt: skip
        assert "it gets this by itself" in refused.error
        (card,) = [
            m for m in harness.store.messages(harness.router_id) if m["meta"].get("agent_refused")
        ]
        assert card["text"] == (
            "Codex needs you to sign in. Your message is saved: “Fix it” gets it on Codex once "
            "you have."
        )
        monkeypatch.delenv("FAKE_CODEX_SIGNED_OUT")
        await runtime.refresh_agent_status()
        await until(lambda: len(runtime.tasks.for_session(worker)) == 2, 15)
        titles = [task["title"] for task in runtime.tasks.for_session(worker)]
        assert "Count the rows · data" in titles, "the same worker, as the Router gave it"
        assert harness.store.message(card["id"])["meta"]["resumed"] == "Resumed after sign-in"
        assert len(harness.store.open_sessions(WORKER)) == 1
    finally:
        await runtime.stop()


async def test_a_turn_that_lost_its_sign_in_after_running_tools_is_never_replayed(
    tmp_path, codex_installed, monkeypatch, quick
):
    """A worker's turn ran a command, then its sign-in ended: its agent says Sign-in needed at
    once, and nothing of that turn is sent again once signed in (it had run tools)."""
    harness, runtime = await started(tmp_path)
    marker = tmp_path / "ran.txt"
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        made = await spawn_on(
            harness, runtime, title="Count the rows · data", agent="codex",
            instructions=f"Run the shell command `echo once >> {shlex.quote(str(marker))}` then "
            "Lose the sign-in",
        )  # fmt: skip
        worker = made["session_id"]
        await until(lambda: runtime.agent_status("codex")["status"] == agents.SIGN_IN, 30)
        await until(lambda: harness.store.session(worker)["status"] not in {"working", "starting"})
        assert marker.read_text() == "once\n"
        assert "agent_refusals" not in runtime.snapshot(), "nothing held: it had started"
        approve()  # (A stand-in browser sign-in, finished at once.)
        await runtime.setups.sign_in("codex")
        await until(lambda: runtime.agent_status("codex")["status"] == agents.READY)
        await asyncio.sleep(0.8)
        assert marker.read_text() == "once\n", "never run again by itself"
        assert len(harness.store.open_sessions(WORKER)) == 1
    finally:
        await runtime.stop()


async def test_a_claude_turn_that_says_its_login_expired_shows_sign_in_needed(
    tmp_path, claude_on_path, quick
):
    from types import SimpleNamespace

    harness, runtime = await started(tmp_path)
    try:
        await runtime.refresh_agent_status()
        assert runtime.agent_status("claude")["status"] == agents.READY
        row = harness.store.create_session("w-claude", WORKER, "Fix it", "working")
        harness.store.set("agent:w-claude", "claude")
        event = SimpleNamespace(
            data=SimpleNamespace(message="Login expired · Please run /login", error_type="")
        )
        runtime.events.error(row, event)
        status = runtime.agent_status("claude")
        assert status["status"] == agents.SIGN_IN and status["ways"]
        await runtime.refresh_agent_status()
        assert runtime.agent_status("claude")["status"] == agents.READY, "a look says signed in"
    finally:
        await runtime.stop()


async def test_settings_lists_no_agent_not_installed_but_one_still_on_to_turn_off(
    tmp_path, codex_installed
):
    """Beside one found, an agent not installed has no row (nothing offers it), unless it is
    still on: then its row says Not installed, so it can be turned off."""
    from test_agents import saved

    _, runtime = await started(tmp_path)
    try:
        found = await runtime.look_for_agents()
        assert [entry["name"] for entry in found["agents"]] == ["codex"]
        assert "missing" not in found, found["missing"]
        await saved(runtime, {"claude": {"enabled": True}})
        found = await runtime.look_for_agents()
        assert [entry["name"] for entry in found["agents"]] == ["codex", "claude"]
        claude = found["agents"][1]
        assert (claude["found"], claude["enabled"], claude["said"]) == (
            False,
            True,
            "Not installed",
        )
        assert "missing" not in found
    finally:
        await runtime.stop()


async def test_signed_out_while_its_backend_runs_says_sign_in_needed_when_the_page_asks(
    tmp_path, claude_on_path, monkeypatch, quick
):
    """Claude's backend started (its workers each run a process of their own): signed out in a
    terminal meanwhile, the page's next ask says Sign-in needed (it said Ready while the
    backend ran, never glanced at); a start that runs again clears it."""
    from test_agents import saved

    monkeypatch.setattr(runtime_module, "STATUS_SECONDS", 0.0)
    monkeypatch.setattr(backend_claude.ClaudeAgent, "check", checked_ok)
    _, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        await runtime.connect_agent("claude")
        assert runtime.agent_status("claude")["status"] == agents.READY
        monkeypatch.setenv("FAKE_CLAUDE_SIGNED_OUT", "1")
        found = {status["name"]: status for status in await runtime.refresh_agent_status()}
        assert found["claude"]["status"] == agents.SIGN_IN, found["claude"]
        monkeypatch.delenv("FAKE_CLAUDE_SIGNED_OUT")
        found = {status["name"]: status for status in await runtime.refresh_agent_status()}
        assert found["claude"]["status"] == agents.READY
    finally:
        await runtime.stop()


async def checked_ok(self, folder):
    return {"ran": True, "acting": "", "ask_first": "", "steering": True, "usage": True}


async def test_signed_out_in_a_terminal_while_codex_runs_holds_new_work_before_it_goes(
    tmp_path, codex_installed, monkeypatch, quick
):
    """Codex's one app-server runs on for its workers after the owner signed out in a terminal:
    the page says Sign-in needed (its look), and what goes to Codex now is looked at again
    first, then held with its card, not started (nor delivered) to fail there."""
    monkeypatch.setattr(runtime_module, "STATUS_SECONDS", 0.0)
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        made = await spawn_on(harness, runtime, title="Fix it", instructions="Report done: Fixed.",
                              agent="codex")  # fmt: skip
        worker = made["session_id"]
        await until(lambda: harness.store.session(worker)["status"] == "done", 30)
        backend, _ = runtime.connection("codex")
        assert backend and backend.alive()
        monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
        await runtime.refresh_agent_status()
        assert runtime.agent_status("codex")["status"] == agents.SIGN_IN
        refused = await spawn_on(harness, runtime, title="Count the rows · data",
                                 instructions="Report done: 42 rows.", agent="codex")  # fmt: skip
        assert "it starts there by itself" in refused.text_result_for_llm
        assert [r["id"] for r in harness.store.open_sessions(WORKER)] == [worker], "not started"
        refused = await call_tool(
            runtime, "message_session", harness.router_id, session_id=worker,
            text="Also count the rows. Report done: 42 rows.", task="new",
            title="Count the rows · data", why_new="Another deliverable.",
        )  # fmt: skip
        assert "it gets this by itself" in refused.error
        assert len(runtime.tasks.for_session(worker)) == 1, "not delivered"
        cards = [
            m for m in harness.store.messages(harness.router_id) if m["meta"].get("agent_refused")
        ]
        assert [card["meta"]["status"] for card in cards] == [agents.SIGN_IN] * 2
        # Signed in again in the terminal: both go on, once each.
        monkeypatch.delenv("FAKE_CODEX_SIGNED_OUT")
        await runtime.refresh_agent_status()
        await until(lambda: len(harness.store.open_sessions(WORKER)) == 2, 15)
        await until(lambda: len(runtime.tasks.for_session(worker)) == 2, 15)
    finally:
        await runtime.stop()


async def test_the_pages_ask_never_waits_on_a_slow_look_and_hears_once_it_ends(
    tmp_path, codex_installed, monkeypatch, quick
):
    monkeypatch.setattr(runtime_module, "REFRESH_WAIT", 0.3, raising=False)
    monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
    _, runtime = await started(tmp_path)

    def looking() -> dict:
        return getattr(runtime, "glancing", {})

    try:
        await runtime.refresh_agent_status()
        await until(lambda: all(task.done() for task in looking().values()))
        assert runtime.agent_status("codex")["status"] == agents.SIGN_IN
        published = []
        monkeypatch.setattr(runtime, "publish_state", lambda: published.append(1))
        monkeypatch.delenv("FAKE_CODEX_SIGNED_OUT")  # Signed in in a terminal…
        monkeypatch.setenv("FAKE_CODEX_STATUS_DELAY", "1.5")  # … its status slow to say so.
        begun = time.monotonic()
        found = {status["name"]: status for status in await runtime.refresh_agent_status()}
        assert time.monotonic() - begun < 1.2, "not held up by the look"
        assert found["codex"]["status"] == agents.SIGN_IN, "as last known"
        look = looking()["codex"]
        again = asyncio.ensure_future(runtime.refresh_agent_status())
        await until(lambda: runtime.agent_status("codex")["status"] == agents.READY, 10)
        assert published, "the page is told"
        await again
        assert looking()["codex"] is look, "the same look, not a second one"
    finally:
        await runtime.stop()


async def test_a_look_that_fails_otherwise_is_asked_for_later_each_time(
    tmp_path, codex_installed, monkeypatch
):
    """A look that fails for another reason than a sign-in or an install (a Codex too old
    here; one that timed out) is looked at twice as late each time the page asks."""
    _, runs = codex_installed
    monkeypatch.setattr(runtime_module, "UNREADY_SECONDS", 1.0)
    monkeypatch.setenv("FAKE_CODEX_VERSION", "0.100.0")
    _, runtime = await started(tmp_path)

    def looks() -> int:
        return sum(1 for line in ran(runs) if line == "--version")

    try:
        await runtime.refresh_agent_status()
        status = runtime.agent_status("codex")
        assert status["status"] == agents.PROBLEM and "older" in status["said"]
        first = looks()
        await asyncio.sleep(1.3)
        await runtime.refresh_agent_status()
        assert looks() == first, "after 2 seconds, not 1"
        await asyncio.sleep(1.0)
        await runtime.refresh_agent_status()
        assert looks() == first + 1
        monkeypatch.delenv("FAKE_CODEX_VERSION")
        await asyncio.sleep(2.0)
        await runtime.refresh_agent_status()
        assert looks() == first + 1, "then after 4"
        await asyncio.sleep(2.3)
        await runtime.refresh_agent_status()
        assert runtime.agent_status("codex")["status"] == agents.READY
    finally:
        await runtime.stop()


def codex_limit(minutes: int, seconds: float) -> int:
    """The stand-in Codex's plan limit used up, its window (`minutes`) resetting in `seconds`,
    its rate limits not said before a turn fails (read them), as live."""
    resets = int(time.time() + seconds)
    home = Path(os.environ["FAKE_CODEX_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "limit.json").write_text(json.dumps({"minutes": minutes, "resets_at": resets}))
    return resets


async def test_a_codex_limit_it_did_not_say_first_is_read_and_said_until_when(
    tmp_path, codex_installed, monkeypatch, quick
):
    """Codex refused a worker's turn for its plan's limit without saying its rate limits first:
    they are read then; the worker's line, its paused task and the @ list say which window
    holds it and until when; the task goes on by itself then."""
    from test_agent_picker import limited

    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        resets = codex_limit(7 * 24 * 60, 2 * 86400)
        _worker, task, last = await limited(harness, runtime, "codex", "Report done: Fixed.")
        assert last["text"].startswith("Paused: Codex's weekly limit is used up · resets ")
        line = f"Codex's weekly limit is used up · resets {agents.reset_words(resets)}"
        assert last["text"] == f"Paused: {line}"
        view = runtime.task_actions.view(runtime.tasks.get(task))
        assert view["paused"]["line"] == line
        assert view["paused"]["go_on_at"] == resets, "it goes on by itself then"
        status = runtime.agent_status("codex")
        assert (status["status"], status["said"], status["resets_at"]) == (
            agents.LIMITED,
            "Weekly limit used up",
            resets,
        )
    finally:
        await runtime.stop()


async def test_a_codex_limit_used_up_again_after_its_reset_is_read_again(
    tmp_path, codex_installed, monkeypatch, quick
):
    """Its limit reset, then used up again: what Codex said of its rate limits before is past
    (its reset gone by), so they are read again, and the worker's line says the new reset."""
    from test_agent_picker import limited

    monkeypatch.setattr(task_actions, "GO_ON_MARGIN", 0.2)
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        codex_limit(300, 15)  # (Still ahead when its turn fails and its limits are read: CI.)
        worker, task, _ = await limited(harness, runtime, "codex", "Report done: Fixed.")
        await until(lambda: runtime.tasks.get(task)["status"] != "waiting", 30)  # Reset.
        await until(
            lambda: harness.store.session(worker)["status"] not in {"working", "queued"}, 30
        )
        resets = codex_limit(300, 3600)
        await runtime.owner_send(worker, "Report done: again.")

        def paused():
            last = harness.store.messages(worker)[-1]
            return last if last["text"].startswith("Paused: ") else None

        await until(paused, 30)
        line = f"Codex's 5-hour limit is used up · resets {agents.reset_words(resets)}"
        assert paused()["text"] == f"Paused: {line}"
    finally:
        await runtime.stop()


async def test_work_held_for_a_limit_goes_on_by_itself_once_it_resets_and_only_once(
    tmp_path, codex_installed, monkeypatch, quick
):
    """While Codex's plan limit is used up, work named for it is held (its card says until
    when, and that the message is saved), as a worker it paused waits; once the limit resets
    (its time, then Codex asked again), each goes on, once, under Handled for you."""
    from test_agent_picker import limited

    from chiefofstaff import task_actions

    monkeypatch.setattr(runtime_module, "LIMIT_MARGIN", 0.2, raising=False)
    monkeypatch.setattr(task_actions, "GO_ON_MARGIN", 0.2)
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        # (Long enough for the worker's turn to meet it on a slow machine.)
        resets = codex_limit(300, 20)
        paused, task, _ = await limited(harness, runtime, "codex", "Report done: Fixed.")
        refused = await spawn_on(harness, runtime, title="Count the rows · data",
                                 instructions="Report done: 42 rows.", agent="codex")  # fmt: skip
        cards = [
            m for m in harness.store.messages(harness.router_id) if m["meta"].get("agent_refused")
        ]
        assert len(cards) == 1, "held with its card, not started to be paused there"
        assert "once its limit resets" in refused.text_result_for_llm
        card = cards[0]
        assert card["text"] == (
            f"Codex's 5-hour limit is used up · resets {agents.reset_words(resets)}. Your message "
            "is saved: “Count the rows · data” starts on Codex once it resets."
        )
        assert card["meta"]["status"] == agents.LIMITED
        assert [r["id"] for r in harness.store.open_sessions(WORKER)] == [paused], "held"
        await until(lambda: len(harness.store.open_sessions(WORKER)) == 2, 60)
        (started_now,) = [r for r in harness.store.open_sessions(WORKER) if r["id"] != paused]
        assert runtime.agent(started_now["id"]) == "codex"
        assert (
            harness.store.message(card["id"])["meta"]["resumed"] == "Resumed once its limit reset"
        )
        await until(lambda: runtime.tasks.get(task)["status"] != "waiting", 30)
        await until(lambda: len(harness.store.all("SELECT id FROM handled")) == 2, 10)
        said = sorted(row["text"] for row in harness.store.all("SELECT text FROM handled"))
        assert said == [
            "Started “Count the rows · data” on Codex once its limit reset",
            "“Fix it” went on on Codex once its limit reset",
        ]
        assert runtime.agent_status("codex")["status"] == agents.READY
        # Said over again (a look, a restart): nothing goes on twice.
        await runtime.limit_over("codex")
        await asyncio.sleep(0.6)
        assert len(harness.store.open_sessions(WORKER)) == 2
        goes = [
            m for m in harness.store.messages(paused) if m["text"] == "Go on where you stopped."
        ]
        assert len(goes) == 1
    finally:
        await runtime.stop()


async def test_continue_with_copilot_on_a_limits_card_moves_the_work_over_now(
    tmp_path, codex_installed, monkeypatch, quick
):
    from test_agent_picker import limited

    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"codex": {"enabled": True}})
        codex_limit(300, 3600)
        await limited(harness, runtime, "codex", "Report done: Fixed.")
        await spawn_on(harness, runtime, title="Count the rows · data",
                       instructions="Report done: 42 rows.", agent="codex")  # fmt: skip
        (card,) = [
            m for m in harness.store.messages(harness.router_id) if m["meta"].get("agent_refused")
        ]
        await runtime.answer_refusal(card["meta"]["agent_refused"], "copilot")
        moved = [r for r in harness.store.open_sessions(WORKER) if r["title"].startswith("Count")]
        assert [runtime.agent(r["id"]) for r in moved] == ["copilot"]
        assert harness.store.message(card["id"])["meta"]["resumed"] == "Started on Copilot instead"
    finally:
        await runtime.stop()


async def test_a_claude_limit_without_a_reset_says_so_plainly_and_waits_for_the_owner(
    tmp_path, claude_on_path, monkeypatch
):
    from test_agent_picker import limited

    info = {"status": "rejected", "rateLimitType": "five_hour"}  # (No resetsAt.)
    monkeypatch.setenv("FAKE_CLAUDE_LIMIT", json.dumps(info))
    harness, runtime = await started(tmp_path)
    try:
        await saved(runtime, {"claude": {"enabled": True}})
        _worker, task, last = await limited(harness, runtime, "claude", "Hit the usage limit")
        line = "Claude's 5-hour limit is used up; it didn't say when it resets"
        assert last["text"] == f"Paused: {line}"
        view = runtime.task_actions.view(runtime.tasks.get(task))
        assert (view["paused"]["line"], view["paused"]["go_on_at"]) == (line, 0)
        assert view["attention"] == "stopped", "the owner's: Go on, or Continue with…"
    finally:
        await runtime.stop()


# Claude's Agent SDK set up by Chief of Staff itself (claude_setup.py): the owner runs no command.


class Installer:
    """A stand-in for the install's command (never a real install in the tests): what it was
    given, held until `gate` is set; it "installs" `installs` (sdk) where it succeeds, and into
    its folder (`--target`) as a real one would."""

    def __init__(self, sdk: dict, code: int = 0, output: str = "", installs: str = "0.2.189"):
        self.sdk, self.code, self.output, self.installs = sdk, code, output, installs
        self.calls: list[tuple[list[str], float]] = []
        self.held = ""  # The constraints it was given (`--constraint`).
        self.gate = asyncio.Event()
        self.gate.set()

    async def __call__(self, argv: list[str], seconds: float):
        self.calls.append((argv, seconds))
        if "--constraint" in argv:
            held = Path(argv[argv.index("--constraint") + 1])
            self.held = await asyncio.to_thread(held.read_text)
        await self.gate.wait()
        if self.code == 0:
            if "--target" in argv:
                target = Path(argv[argv.index("--target") + 1])
                (target / "claude_agent_sdk").mkdir(parents=True)
                (target / "claude_agent_sdk" / "__init__.py").write_text("")
            self.sdk["version"] = self.installs
        return self.code, self.output


@pytest.fixture
def sdk(monkeypatch):
    """The Agent SDK as installed here, to change ("": missing), and how it is installed:
    Chief of Staff's own environment, with uv."""
    found = {"version": ""}
    monkeypatch.setattr(backend_claude, "sdk_version", lambda: found["version"])
    monkeypatch.setattr(claude_setup, "CHECK_SECONDS", 0.0)
    monkeypatch.setattr(claude_setup, "own_environment", lambda: True)
    monkeypatch.setattr(claude_setup, "find_uv", lambda: "/owner/.local/bin/uv")
    monkeypatch.setattr(claude_setup, "has_pip", lambda: False)
    return found


def with_claude_on(directory: Path) -> Harness:
    """Chief of Staff with Claude on for workers, as it starts."""
    agents.configure(Store(directory), "claude", enabled=True)
    return Harness(directory)


async def test_claude_on_without_its_sdk_is_set_up_at_start_and_turns_ready_without_a_restart(
    tmp_path, claude_on_path, sdk, quick
):
    harness = with_claude_on(tmp_path / "data")
    runtime = harness.runtime
    installer = runtime.claude_setup.runner = Installer(sdk)
    installer.gate.clear()
    await runtime.start()
    try:
        await until(lambda: installer.calls)
        argv, seconds = installer.calls[0]
        wanted = claude_setup.requirement()
        assert argv == [
            "/owner/.local/bin/uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            wanted,
        ]
        assert re.fullmatch(r"claude-agent-sdk(?:[<>=,.0-9])+", wanted), "the extra's, nothing else"
        assert seconds == claude_setup.SETUP_SECONDS
        status = runtime.agent_status("claude")
        assert (status["status"], status["said"]) == (agents.SETTING_UP, "Setting up Claude…")
        assert "uv tool" not in json.dumps(runtime.snapshot()["agents_status"])
        installer.gate.set()
        await until(lambda: runtime.agent_status("claude")["status"] == agents.READY)
        assert len(installer.calls) == 1
    finally:
        await runtime.stop()


async def test_a_first_start_with_claude_alone_and_no_sdk_sets_it_up_and_starts_on_it(
    tmp_path, claude_on_path, sdk, quick, monkeypatch
):
    """agents-anywhere.md 12.3: a core install (no Claude extra), Claude Code installed and
    signed in, no Copilot: the first start turns Claude on (Copilot off), sets its Agent SDK up
    itself, says so calmly on Home at once (the page not held back by the wait), and runs on
    Claude once it is set up, with no restart: the Router, new workers and Watch for you."""
    harness = Harness(tmp_path / "data")
    runtime = harness.runtime
    runtime.backend_factory = lambda settings: (_ for _ in ()).throw(
        RuntimeError("Copilot CLI 'copilot' not found.")
    )
    installer = runtime.claude_setup.runner = Installer(sdk)
    installer.gate.clear()
    began = time.monotonic()
    await runtime.start()
    try:
        assert time.monotonic() - began < claude_setup.WAIT_SECONDS / 2
        assert runtime.status.get("settingup") and runtime.status["waits"]
        assert runtime.status["state"] == "error" and not runtime.ready()
        assert agents.enabled(harness.store, "claude") and not agents.copilot_on(harness.store)
        await until(lambda: installer.calls)
        installer.gate.set()
        await until(runtime.ready, 30)
        router = harness.store.get("router_id")
        assert runtime.agent(router) == "claude"
        said = [m["text"] for m in harness.store.messages(router) if m["kind"] == "notice"]
        assert said[0].startswith("Chief of Staff runs on Claude"), said
        assert len(installer.calls) == 1
        assert agents.default_agent(harness.store) == "claude"
        assert agents.role_agent(harness.store, "connector") == "claude"
    finally:
        await runtime.stop()


async def test_a_failed_setup_says_why_offers_retry_and_is_not_tried_again_by_itself(
    tmp_path, claude_on_path, sdk, quick
):
    data = tmp_path / "data"
    harness = with_claude_on(data)
    failing = (
        "error: Failed to fetch: `https://me:hunter2@pypi.acme.example/simple/claude-agent-sdk/`\n"
        "  Caused by: dns error: failed to lookup address information"
    )
    installer = harness.runtime.claude_setup.runner = Installer(sdk, code=2, output=failing)
    await harness.runtime.start()
    try:
        await until(lambda: harness.runtime.agent_status("claude").get("setup", {}).get("state")
                    == "failed")  # fmt: skip
        status = harness.runtime.agent_status("claude")
        assert status["said"] == "Couldn't set up Claude: no network"
        assert status["setup"]["command"] == claude_setup.manual_command()
        assert "hunter2" not in json.dumps(status), "an index's password is never shown"
        assert any("pypi.acme.example" in line for line in status["setup"]["output"])
    finally:
        await harness.runtime.stop()
    again = Harness(data)
    again.runtime.claude_setup.runner = installer
    await again.runtime.start()
    try:
        await asyncio.sleep(0.3)
        assert len(installer.calls) == 1, "not again by itself: once per version and reason"
        status = again.runtime.agent_status("claude")
        assert (status["said"], status["setup"]["state"]) == (
            "Couldn't set up Claude: no network",
            "failed",
        )
        # A worker for Claude meanwhile: why, in those words (the owner's Retry fixes it).
        agent, why = await again.runtime.new_worker_agent("", False, picked="claude")
        assert (agent, why) == (
            "copilot",
            "Started on Copilot: Claude could not start: Couldn't set up Claude: no network",
        )
        installer.code = 0
        shown = await again.runtime.retry_claude_setup("claude")
        assert [s["status"] for s in shown["agents_status"] if s["name"] == "claude"] == [
            agents.SETTING_UP
        ]
        await until(lambda: again.runtime.agent_status("claude")["status"] == agents.READY)
        assert len(installer.calls) == 2
    finally:
        await again.runtime.stop()


async def test_an_untested_sdk_is_replaced_and_each_reason_or_reinstall_is_tried_once(
    tmp_path, claude_on_path, sdk, quick, monkeypatch
):
    data = tmp_path / "data"
    sdk["version"] = "1.0.99"
    harness = with_claude_on(data)
    installer = harness.runtime.claude_setup.runner = Installer(sdk)

    async def restart(removed: bool = False) -> Harness:
        if removed:
            sdk["version"] = ""
        again = Harness(data)
        again.runtime.claude_setup.runner = installer
        await again.runtime.start()
        return again

    await harness.runtime.start()
    try:
        await until(lambda: harness.runtime.agent_status("claude")["status"] == agents.READY)
        ((argv, _),) = installer.calls
        assert argv[-1] == claude_setup.requirement(), "the tested range, in its place"
    finally:
        await harness.runtime.stop()
    # Removed since: another reason, tried again by itself.
    again = await restart(removed=True)
    try:
        await until(lambda: again.runtime.agent_status("claude")["status"] == agents.READY)
        assert len(installer.calls) == 2
    finally:
        await again.runtime.stop()
    # Removed again, the same way: not by itself (Set up is the owner's), never in a loop.
    again = await restart(removed=True)
    try:
        await asyncio.sleep(0.3)
        assert len(installer.calls) == 2
        status = again.runtime.agent_status("claude")
        assert (status["said"], status["setup"]["state"]) == ("Claude isn't set up yet", "needed")
    finally:
        await again.runtime.stop()
    # Chief of Staff installed again (`uv tool install --reinstall`, without the SDK): set up.
    monkeypatch.setattr(claude_setup, "made", lambda: "installed again")
    again = await restart(removed=True)
    try:
        await until(lambda: again.runtime.agent_status("claude")["status"] == agents.READY)
        assert len(installer.calls) == 3
    finally:
        await again.runtime.stop()


async def test_with_claude_off_nothing_is_installed_until_the_owner_turns_it_on(
    tmp_path, claude_on_path, sdk, quick
):
    harness = Harness(tmp_path / "data")
    installer = harness.runtime.claude_setup.runner = Installer(sdk)
    await harness.runtime.start()
    runtime = harness.runtime
    try:
        await asyncio.sleep(0.3)
        assert installer.calls == []
        status = runtime.agent_status("claude")
        assert (status["status"], status["said"]) == (agents.NOT_SET_UP, "Not set up yet")
        (claude,) = (await runtime.look_for_agents())["agents"]
        assert (claude["problem"], claude["status"]) == ("", agents.NOT_SET_UP), "not a reason"
        installer.gate.clear()
        await saved(runtime, {"claude": {"enabled": True}})
        await until(lambda: installer.calls)
        assert runtime.agent_status("claude")["status"] == agents.SETTING_UP
        # Turned off again before it was set up: the install stops, nothing more is installed.
        await saved(runtime, {"claude": {"enabled": False}})
        assert not runtime.claude_setup.running() and sdk["version"] == ""
        assert runtime.agent_status("claude")["status"] == agents.NOT_SET_UP
        installer.gate.set()
        await saved(runtime, {"claude": {"enabled": True}})
        await until(lambda: runtime.agent_status("claude")["status"] == agents.READY)
        assert len(installer.calls) == 2
    finally:
        await runtime.stop()


async def test_a_claude_worker_waits_a_little_for_a_setup_under_way_else_says_so(
    tmp_path, claude_on_path, sdk, quick, monkeypatch
):
    monkeypatch.setattr(claude_setup, "WAIT_SECONDS", 0.3)
    harness = with_claude_on(tmp_path / "data")
    runtime = harness.runtime
    installer = runtime.claude_setup.runner = Installer(sdk)
    installer.gate.clear()
    await runtime.start()
    try:
        await until(lambda: installer.calls)
        agent, why = await runtime.new_worker_agent("", False, picked="claude")
        assert (agent, why) == (
            "copilot",
            "Started on Copilot: Claude could not start: Claude is being set up",
        )
        with pytest.raises(ValueError, match=r"^Claude could not start: Claude is being set up$"):
            await runtime.new_worker_agent("claude", False)
        waiting = asyncio.ensure_future(runtime.new_worker_agent("", False, picked="claude"))
        await asyncio.sleep(0.05)
        installer.gate.set()  # Set up within its wait: it runs on Claude.
        assert await waiting == ("claude", "")
    finally:
        await runtime.stop()


async def test_started_with_uvx_the_sdk_goes_to_chief_of_staffs_own_folder_never_uvs_cache(
    tmp_path, claude_on_path, sdk, quick, monkeypatch
):
    """uvx's environment is in uv's cache (uv's alone to change): the SDK goes to a folder of
    Chief of Staff's own, last on sys.path, its requirements held to Chief of Staff's own
    packages; found there again at the next start."""
    monkeypatch.setattr(claude_setup, "own_environment", lambda: False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    data = tmp_path / "data"
    harness = with_claude_on(data)
    installer = harness.runtime.claude_setup.runner = Installer(sdk)
    await harness.runtime.start()
    side = claude_setup.side_folder(data)
    try:
        await until(lambda: harness.runtime.agent_status("claude")["status"] == agents.READY)
        ((argv, _),) = installer.calls
        staging = side.with_name(side.name + ".new")
        constraints = side.with_name(side.name + ".constraints.txt")
        assert argv == [
            "/owner/.local/bin/uv", "pip", "install", "--python", sys.executable,
            "--target", str(staging), "--constraint", str(constraints),
            claude_setup.requirement(),
        ]  # fmt: skip
        held = installer.held.split()
        assert "pydantic==" + metadata.version("pydantic") in held
        assert not [line for line in held if line.startswith("claude-agent-sdk")]
        assert (side / "claude_agent_sdk").is_dir()
        assert not staging.exists() and not constraints.exists(), "nothing left beside it"
        assert sys.path[-1] == str(side), "last: Chief of Staff's own packages come first"
    finally:
        await harness.runtime.stop()
    sys.path.remove(str(side))
    again = Harness(data)
    again.runtime.claude_setup.runner = installer
    await again.runtime.start()
    try:
        assert sys.path[-1] == str(side) and len(installer.calls) == 1
    finally:
        await again.runtime.stop()


def test_what_is_installed_is_the_claude_extras_own_requirement_and_how():
    sdk, extra = "claude-agent-sdk", "; extra == 'claude'"
    listed = ["fastapi<1; python_version > '3'", f"{sdk}<0.3,>=0.2.182{extra}"]
    assert claude_setup.requirement(listed) == f"{sdk}<0.3,>=0.2.182"
    assert claude_setup.requirement([f'{sdk} (>=0.2.190, <0.3) ; extra == "claude"']) == (
        f"{sdk}>=0.2.190,<0.3"
    )
    tested = f"{sdk}>=0.2.182,<0.3.0"
    assert claude_setup.fallback() == tested
    for odd in (
        f"{sdk} @ https://acme.example/sdk.whl{extra}",  # Never an address.
        f"evil-package>=1{extra}",
        f"{sdk}>=0.2; os.system('x'){extra}",
        f"{sdk}<0.3,>=0.2.182; extra == 'dev'",
    ):
        assert claude_setup.requirement([odd]) == tested, odd
    side = Path("/data/extras/claude-sdk")
    own_uv = claude_setup.plan(tested, side, own=True, uv="/u/uv", pip=True)
    assert own_uv.argv == ("/u/uv", "pip", "install", "--python", sys.executable, tested)
    own_pip = claude_setup.plan(tested, side, own=True, uv="", pip=True)
    assert own_pip.argv == (
        sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", tested,
    )  # fmt: skip
    assert claude_setup.plan(tested, side, own=True, uv="", pip=False).argv == ()
    # uvx's environment (in uv's cache) and a Python that is not a venv's: its own folder.
    assert not claude_setup.own_environment("/u/.cache/uv/archive-v0/Ab12/", "/py")
    assert not claude_setup.own_environment("/usr/local", "/usr/local")
    side_pip = claude_setup.plan(tested, side, own=False, uv="", pip=True)
    assert side_pip.argv[-5:] == (
        "--target", "/data/extras/claude-sdk.new",
        "--constraint", "/data/extras/claude-sdk.constraints.txt", tested,
    )  # fmt: skip
    assert claude_setup.reason_of("error: No space left on device (os error 28)") == (
        "the disk is full"
    )
    assert claude_setup.reason_of("error: Permission denied (os error 13)") == (
        "its folder can't be written"
    )
    offline = (  # uv 0.9, its network off and nothing cached.
        "  \u00d7 No solution found when resolving dependencies:\n  ╰─▶ Because claude-agent-sdk "
        "was not found in the cache and you require claude-agent-sdk>=0.2.182,<0.3, we can "
        "conclude that your requirements are unsatisfiable.\n      hint: Packages were unavailable "
        "because the network was disabled."
    )
    assert claude_setup.reason_of(offline) == "no network"
    assert claude_setup.reason_of("\u00d7 No solution found when resolving dependencies") == (
        "no tested version can be installed here"
    )
    assert claude_setup.reason_of("weird\nboom") == "the installer failed (boom)"
    # Here (a virtual environment, the SDK in it): what a `uv tool upgrade` must put back.
    assert claude_setup.kept_through_upgrade() == claude_setup.requirement()
    assert re.fullmatch(r"\d+-\d+", claude_setup.made()), "this install of it, told apart"


def test_uv_is_found_where_its_installers_put_it(tmp_path):
    home = tmp_path / "home"
    uv = home / ".local" / "bin" / "uv"
    uv.parent.mkdir(parents=True)
    uv.write_text("#!/bin/sh\n")
    uv.chmod(0o755)
    assert claude_setup.find_uv({"PATH": str(tmp_path / "nowhere")}, home) in {
        str(uv),
        "/opt/homebrew/bin/uv",
        "/usr/local/bin/uv",
    }
    assert claude_setup.find_uv({"PATH": "", "UV": str(uv)}, tmp_path) == str(uv)


async def test_no_real_install_runs_while_the_tests_run():
    with pytest.raises(RuntimeError, match=r"^not while Chief of Staff's own tests run$"):
        await claude_setup.run_command(["/bin/echo", "never"], 5)


# Copilot from the page, Update, the sandbox's certificate (the owner's commands, run for them) ---


def copilot_stand_in(directory: Path, monkeypatch) -> Path:
    """The stand-in Copilot CLI (tests/fake_copilot.py) as `copilot` on the PATH; its state, and
    each run of it, in `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    command = directory / "copilot"
    command.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{Path(__file__).parent / "fake_copilot.py"}" "$@"\n'
    )
    command.chmod(0o755)
    (directory / "home").mkdir(exist_ok=True)
    monkeypatch.setenv("FAKE_COPILOT_HOME", str(directory / "home"))
    monkeypatch.setenv("FAKE_COPILOT_RUNS", str(directory / "runs"))
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    return directory


def copilot_ran(directory: Path) -> list[str]:
    runs = directory / "runs"
    return runs.read_text().splitlines() if runs.exists() else []


async def test_copilot_signs_in_from_the_page_and_chief_of_staff_starts_by_itself(
    tmp_path, monkeypatch, quick
):
    """Copilot alone, signed out: Home's card and its row offer Sign in, which runs `copilot
    login` with its one-time code (said by Copilot whatever runs it); once the owner enters it,
    Chief of Staff starts by itself (no Retry, no terminal, no restart)."""
    from test_without_copilot import SignedOut

    bin_dir = copilot_stand_in(tmp_path / "bin", monkeypatch)
    await (await Harness(tmp_path / "data").connect()).stop()
    client = SignedOut()
    harness = Harness(tmp_path / "data", client=client)
    runtime = await harness.connect()
    try:
        assert runtime.status["error"] == "Copilot is not signed in."
        shown = {found["name"]: found for found in runtime.snapshot()["setup_agents"]}
        assert shown["copilot"]["status"] == agents.SIGN_IN, "for Home's card, Copilot alone"
        assert [way["method"] for way in shown["copilot"]["ways"]] == ["device", "browser"]
        view = await runtime.setups.sign_in("copilot")
        assert (view["kind"], view["method"]) == ("signin", "device")
        await until(lambda: runtime.setups.view()["copilot"].get("code"))
        run = runtime.setups.view()["copilot"]
        assert (run["url"], run["code"]) == ("https://github.com/login/device", "ACME-1234")
        assert not runtime.ready()
        client.signed_in = True  # (What Copilot says once the owner entered the code.)
        (bin_dir / "home" / "approve").write_text("ok")
        await until(lambda: runtime.setups.view()["copilot"]["state"] == "done", 30)
        assert runtime.copilot_ready() and harness.router_id
        assert copilot_ran(bin_dir) == ["login --device-code"]
        assert "setup_agents" not in runtime.snapshot()
    finally:
        await runtime.stop()


async def test_copilot_installs_from_the_page_and_its_sign_in_follows(tmp_path, monkeypatch, quick):
    """Not installed: Install runs Copilot's official installer, word for word; found where it
    put it (not on the PATH Chief of Staff started with), Copilot starts, says it is signed
    out, and Sign in follows at once."""
    from test_without_copilot import NOT_FOUND, SignedOut

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    from agent_paths import without_agents

    stand_in = copilot_stand_in(tmp_path / "stand-in", monkeypatch)
    hidden = frozenset({"claude", "codex", "copilot"})  # (Nor the owner's own Copilot CLI.)
    monkeypatch.setenv("PATH", without_agents(os.environ["PATH"], tmp_path / "path", hidden))
    bin_dir = home / ".local" / "bin"
    script = f'mkdir -p "{bin_dir}" && cp "{stand_in / "copilot"}" "{bin_dir}/copilot"'
    monkeypatch.setitem(agentsetup.INSTALLERS, "copilot", (("script", script, ""),))
    monkeypatch.setattr(agentsetup, "known_dirs", lambda: [bin_dir])
    monkeypatch.setattr(agentsetup, "npm_bin", lambda: asyncio.sleep(0, None))
    client = SignedOut()
    harness = Harness(tmp_path / "data", client=client)

    def factory(settings):
        if not (bin_dir / "copilot").exists():
            raise RuntimeError(NOT_FOUND)
        return client

    harness.runtime.backend_factory = factory
    runtime = await harness.connect()
    try:
        shown = {found["name"]: found for found in runtime.snapshot()["setup_agents"]}
        assert shown["copilot"]["status"] == agents.MISSING
        assert shown["copilot"]["install"][0] == {"method": "script", "command": script}
        await runtime.setups.install("copilot")
        await until(lambda: runtime.setups.runs["copilot"].kind == "signin", 30)
        assert harness.store.get("agentpath:copilot") == str(bin_dir)
        assert runtime.agent_status("copilot")["status"] == agents.SIGN_IN
        await until(lambda: runtime.setups.view()["copilot"].get("code"))
        client.signed_in = True
        (stand_in / "home" / "approve").write_text("ok")
        await until(lambda: runtime.copilot_ready(), 30)
    finally:
        await runtime.stop()


def test_copilot_off_and_not_installed_says_so_with_install(tmp_path, monkeypatch):
    """Copilot turned off is never started, so whether it is installed is looked for on the
    PATH (as Chief of Staff starts it): not there, its row offers Install, not Ready · off."""
    from agent_paths import without_agents

    hidden = frozenset({"claude", "codex", "copilot"})
    monkeypatch.setenv("PATH", without_agents(os.environ["PATH"], tmp_path / "path", hidden))
    harness = Harness(tmp_path / "data")
    harness.store.set(agents.CONFIG, json.dumps({"copilot": {"enabled": False}}))
    harness.runtime.backend_factory = runtime_module.default_backend
    status = harness.runtime.agent_status("copilot")
    assert (status["status"], status["on"]) == (agents.MISSING, False)
    assert status["install"][0] == {"method": "script", "command": agentsetup.INSTALLERS[
        "copilot"][0][1]}  # fmt: skip
    copilot_stand_in(tmp_path / "bin", monkeypatch)
    assert harness.runtime.agent_status("copilot")["status"] == agents.READY


async def test_copilot_older_than_tested_is_updated_from_the_page_once_asked(
    tmp_path, monkeypatch, quick
):
    """Older than the versions tested: its warning names no command, and Update runs its own
    updater (`copilot update`); once it is no longer older, nothing more is offered."""
    bin_dir = copilot_stand_in(tmp_path / "bin", monkeypatch)
    monkeypatch.setenv("FAKE_COPILOT_VERSION", "1.0.50")
    harness = Harness(tmp_path / "data")
    harness.client.cli_version = "1.0.50"
    runtime = await harness.connect()
    try:
        assert runtime.status["warning"] == (
            "Fake CLI 1.0.50 is older than the versions tested (1.0.100)."
        )
        (shown,) = runtime.snapshot()["setup_agents"]
        assert shown["update"] == {"command": "copilot update", "then": ""}
        view = await runtime.setups.update("copilot")
        assert (view["kind"], view["command"]) == ("update", "copilot update")
        await until(lambda: runtime.setups.view()["copilot"]["state"] != "running", 30)
        assert runtime.setups.view()["copilot"]["state"] == "done"
        assert copilot_ran(bin_dir) == ["update"]
        assert "warning" not in runtime.status and "setup_agents" not in runtime.snapshot()
    finally:
        await runtime.stop()


async def test_codex_older_than_tested_is_updated_and_a_failed_update_says_why_once(
    tmp_path, codex_installed, monkeypatch, quick
):
    """Codex refused as older than tested: its row offers Update (`codex update`, asked first:
    the page shows it), and turns Ready once updated, without a restart. A failed update says
    its own words, its command kept for Details, and is not run again by itself."""
    _, runs = codex_installed
    monkeypatch.setenv("FAKE_CODEX_VERSION", "0.160.0")
    monkeypatch.setenv("FAKE_CODEX_UPDATE_FAILS", "network unreachable")
    _, runtime = await started(tmp_path)
    try:
        await runtime.refresh_agent_status()
        status = runtime.agent_status("codex")
        assert status["status"] == agents.PROBLEM
        assert status["said"] == "Codex 0.160.0 is older than the versions tested (0.167.2)."
        assert status["update"] == {"command": "codex update", "then": ""}
        await runtime.setups.update("codex")
        await until(lambda: runtime.setups.view()["codex"]["state"] == "failed", 20)
        failed = runtime.setups.view()["codex"]
        assert failed["error"] == "Error: network unreachable"
        assert failed["command"] == "codex update", "to run yourself, under Details"
        await asyncio.sleep(0.5)
        assert ran(runs).count("update") == 1, "never again by itself"
        monkeypatch.delenv("FAKE_CODEX_UPDATE_FAILS")
        await runtime.setups.update("codex")  # Try again.
        await until(lambda: runtime.setups.view()["codex"]["state"] == "done", 20)
        assert runtime.agent_status("codex")["status"] == agents.READY
        assert "update" not in runtime.agent_status("codex")
    finally:
        await runtime.stop()


@pytest.mark.parametrize(
    ("ca", "steps"),
    [
        ("3", ["status", "create", "trust", "status"]),
        ("5", ["status", "rotate", "trust", "status"]),
        ("0", ["status"]),
    ],
)
async def test_the_sandboxs_certificate_is_trusted_in_one_click(
    tmp_path, monkeypatch, quick, ca, steps
):
    """Settings' Trust the sandbox's certificate: `copilot sandbox ca create`, then `trust`
    (one to rotate first, `rotate`; trusted already, nothing), its status again; gh works in
    the sandbox from then on."""
    bin_dir = copilot_stand_in(tmp_path / "bin", monkeypatch)
    monkeypatch.setenv("FAKE_COPILOT_CA", ca)
    monkeypatch.setattr(agentsetup, "keychain", lambda: True)
    _, runtime = await started(tmp_path)
    try:
        runtime.sandbox_gh = False
        view = await runtime.setups.trust_sandbox()
        assert (view["key"], view["kind"], view["command"]) == (
            "sandbox", "trust", "copilot sandbox ca create && copilot sandbox ca trust",
        )  # fmt: skip
        await until(lambda: runtime.setups.view()["sandbox"]["state"] != "running", 20)
        assert runtime.setups.view()["sandbox"]["state"] == "done"
        assert copilot_ran(bin_dir) == [f"sandbox ca {step}" for step in steps]
        assert runtime.snapshot()["sandbox"]["gh_trusted"] is True
    finally:
        await runtime.stop()


async def test_a_trust_the_owner_declines_says_why_and_is_not_tried_again(
    tmp_path, monkeypatch, quick
):
    bin_dir = copilot_stand_in(tmp_path / "bin", monkeypatch)
    monkeypatch.setenv("FAKE_COPILOT_TRUST_FAILS", "The authorization was canceled by the user.")
    monkeypatch.setattr(agentsetup, "keychain", lambda: True)
    _, runtime = await started(tmp_path)
    try:
        runtime.sandbox_gh = False
        await runtime.setups.trust_sandbox()
        await until(lambda: runtime.setups.view()["sandbox"]["state"] == "failed", 20)
        failed = runtime.setups.view()["sandbox"]
        assert failed["error"] == "Error: The authorization was canceled by the user."
        await asyncio.sleep(0.5)
        steps = ["status", "create", "trust"]
        assert copilot_ran(bin_dir) == [f"sandbox ca {step}" for step in steps], "not again"
        assert runtime.snapshot()["sandbox"]["gh_trusted"] is False
        monkeypatch.setattr(agentsetup, "keychain", lambda: False)
        with pytest.raises(ValueError, match="in a terminal on this system"):
            await runtime.setups.trust_sandbox()
    finally:
        await runtime.stop()
