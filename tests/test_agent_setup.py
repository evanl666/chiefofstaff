"""Sign in and Install from the page (design/agent-smooth.md): Chief of Staff runs the agent's
own CLI for the owner (the stand-ins: tests/fake_codex.py, tests/fake_claude.py), shows what it
says (the page's link, a one-time code, a code to paste), sees it signed in by itself, and the
work it held goes on once, by itself, never twice and never what had already run."""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from conftest import call_tool
from test_agent_picker import started, until
from test_agents import ran, saved, spawn_on

from chiefofstaff import agents, agentsetup, backend_claude
from chiefofstaff import runtime as runtime_module
from chiefofstaff.roles import WORKER


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
            instructions=f"Run the shell command `echo once >> {marker}` then Lose the sign-in",
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


async def test_settings_lists_an_agent_not_installed_beside_one_found_with_install(
    tmp_path, codex_installed
):
    _, runtime = await started(tmp_path)
    try:
        found = await runtime.look_for_agents()
        assert [entry["name"] for entry in found["agents"]] == ["codex"]
        (claude,) = found["missing"]
        assert (claude["name"], claude["said"]) == ("claude", "Not installed")
        assert claude["install"][0]["command"] == "curl -fsSL https://claude.ai/install.sh | bash"
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
