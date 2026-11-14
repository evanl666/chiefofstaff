"""Claude's sign-in is Claude's (more-agents.md 5.12.1): Chief of Staff never reads, stores or
forwards a credential of it. Its source names none, and its process, as it looks for Claude,
starts a worker and checks a version, opens no credentials file, asks the keychain for none of
Claude's, and starts no command that would."""

import asyncio
import sys
from pathlib import Path

import pytest
from conftest import fake_claude
from copilot.generated.rpc import PermissionDecisionApproveOnce

from chiefofstaff import backend_claude, claude_check

SOURCE = Path(backend_claude.__file__).parent
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Claude workers need macOS/Linux")


def test_chief_of_staffs_source_names_no_credential_of_claudes():
    for path in SOURCE.rglob("*.py"):
        text = path.read_text()
        assert ".credentials.json" not in text, path
        assert "Claude Code-credentials" not in text, path
        assert "find-generic-password" not in text, path
        if "keyring" in text:
            # Its own keychain entry (the Slack token), under its own service only.
            assert path == SOURCE / "sources" / "slack.py", path
            assert '"chiefofstaff-" + digest' in text


# What Chief of Staff's own process does, as Python's audit hooks see it. A hook cannot be removed
# once added: it records only while a test watches.
WATCHING: list[list[tuple[str, str]]] = []


def audit(event: str, arguments: tuple):
    if not WATCHING:
        return
    if event == "open" and arguments:
        WATCHING[-1].append(("open", str(arguments[0])))
    elif event in {"subprocess.Popen", "os.posix_spawn", "os.exec", "os.spawn"}:
        WATCHING[-1].append((event, repr(arguments)))


sys.addaudithook(audit)


@pytest.fixture
def watched(monkeypatch):
    seen: list[tuple[str, str]] = []
    keychain: list[tuple] = []
    import keyring

    for name in ("get_password", "set_password", "delete_password", "get_credential"):
        monkeypatch.setattr(keyring, name, lambda *args, _name=name: keychain.append((_name, args)))
    WATCHING.append(seen)
    yield seen, keychain
    WATCHING.remove(seen)


async def test_looking_starting_a_worker_and_checking_touch_no_claude_credential(
    tmp_path, monkeypatch, watched
):
    seen, keychain = watched
    command = fake_claude(tmp_path / "fake", monkeypatch)
    monkeypatch.setattr(
        claude_check, "KEPT", (*claude_check.KEPT, "FAKE_CLAUDE_HOME")
    )  # The stand-in's own sessions folder.
    await backend_claude.look(command)
    agent = backend_claude.claude_backend(command, folder=tmp_path / "agent")
    await agent.start()
    try:
        session = await agent.create_session(
            session_id="c" * 32,
            name="t",
            working_directory=str(tmp_path),
            folders=[str(tmp_path)],
            on_permission_request=lambda request, context: PermissionDecisionApproveOnce(),
        )
        done = []
        session.on(lambda event: done.append(event.type.value))
        await session.send("Run the shell command `true`, then reply with just: ok")
        for _ in range(200):
            if "session.idle" in done:
                break
            await asyncio.sleep(0.05)
        found = await agent.check(tmp_path / "check")
        assert found["ran"] is True
    finally:
        await agent.stop()
    opened = [path for kind, path in seen if kind == "open"]
    assert opened, "the hook saw Chief of Staff's process open files"
    assert not [path for path in opened if path.endswith(".credentials.json")]
    started = [arguments for kind, arguments in seen if kind != "open"]
    assert started, "the hook saw Chief of Staff's process start commands"
    keys = ("Claude Code-credentials", "find-generic-password", "dump-keychain")
    assert not [a for a in started if any(key in a for key in keys)]
    assert keychain == [], "nothing asked of the keychain"
