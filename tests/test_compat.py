"""Copilot versions never stop Chief of Staff (compat.py, CopilotBackend.start): the protocol of
the owner's CLI told before it starts, protocol 4 spoken through the adapter, Copilot's fallback
set up ahead, the runtimes already on this machine when it can't be, one plain line when nothing
runs, a CLI that updates itself while Chief of Staff runs, a newer Copilot's mismatch errors and
events it can't read."""

import asyncio
import json
import logging
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import Harness, settle
from copilot._jsonrpc import JsonRpcClient
from copilot.generated.session_events import (
    SessionEventType,
    ShutdownType,
    session_event_from_dict,
)
from test_runtime import FALLBACK_PATH, fake_copilot

from chiefofstaff import backend as backend_module
from chiefofstaff import compat
from chiefofstaff.backend_claude import ClaudeWorker
from chiefofstaff.delivery import FreshStartError
from chiefofstaff.models import Settings
from chiefofstaff.runtime import CONTINUED_AFRESH, CONTINUED_FRESH

needs_sh = pytest.mark.skipif(sys.platform == "win32", reason="the stand-in CLI is a shell script")


def refusal(protocol: int) -> str:
    return (
        "SDK protocol version mismatch: SDK supports versions 3-3, but server reports version "
        f"{protocol}. Please update your SDK or server to ensure compatibility."
    )


def package(folder: Path, version: str, protocol: int) -> Path:
    """A Copilot CLI package of this version whose bundled SDK speaks this protocol."""
    (folder / "copilot-sdk").mkdir(parents=True, exist_ok=True)
    named = {"name": "@github/copilot", "version": version}
    (folder / "package.json").write_text(json.dumps(named))
    (folder / compat.PROTOCOL_FILE).write_text(
        f"export declare const SDK_PROTOCOL_VERSION = {protocol};\n"
    )
    return folder


def test_a_clis_protocol_is_known_before_it_starts(tmp_path, monkeypatch):
    """Without starting it: what its package says (beside its program, or where it updated
    itself to), else what its version is known to speak; nothing beyond that."""
    assert compat.protocol_by_version("1.0.105") == 3
    assert compat.protocol_by_version("1.0.112") == 3
    assert compat.protocol_by_version("1.0.113") == 4
    assert compat.protocol_by_version("1.0.115-1") == 4
    assert compat.protocol_by_version("1.0.116") is None
    assert compat.protocol_by_version("") is None
    roots = tmp_path / "pkg"
    monkeypatch.setattr(compat, "package_roots", lambda: [roots])
    program = tmp_path / "npm" / "copilot"
    program.parent.mkdir()
    program.write_text("")
    assert compat.cli_protocol(str(program), "1.0.130") is None, "only its handshake tells"
    package(roots / "darwin-arm64" / "1.0.130", "1.0.130", 5)
    assert compat.cli_protocol(str(program), "1.0.130") == 5, "where it updated itself to"
    package(program.parent, "1.0.131", 6)
    assert compat.cli_protocol(str(program), "1.0.131") == 6, "beside its program"
    package(roots / "darwin-arm64" / "1.0.112", "1.0.111", 9)
    assert compat.cli_protocol(str(program), "1.0.112") == 3, "another version's package is not"
    assert compat.cli_protocol(str(program), "") is None


@needs_sh
async def test_a_cli_whose_package_says_a_newer_protocol_is_not_started(tmp_path, monkeypatch):
    """Its package says protocol 5: Copilot FALLBACK_CLI runs at once, the owner's never
    started (a refused start took 2.5-4 s), and kept as refused."""
    starts = tmp_path / "starts"
    monkeypatch.setenv("FAKE_COPILOT_STARTS", str(starts))
    owner = tmp_path / "bin" / "copilot"
    fake_copilot(owner, "1.0.130", 5)
    package(owner.parent, "1.0.130", 5)
    ours = fake_copilot(tmp_path / "sdk" / "copilot-runtime", "1.0.112", 3)
    monkeypatch.setattr(backend_module, "fallback_runtime", lambda: ours)
    backend = backend_module.copilot_backend(str(owner))
    backend.refusals = tmp_path / backend_module.REFUSED_CLI
    await backend.start()
    try:
        assert backend.fallback == ("1.0.130", "1.0.112")
        assert starts.read_text().split() == ["1.0.112"]
        assert backend_module.read_refusal(backend.refusals)["key"]["version"] == "1.0.130"
    finally:
        await backend.stop()


def requests_of(log: Path, method: str) -> list[dict]:
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    return [line["params"] for line in lines if line["method"] == method]


@needs_sh
async def test_a_protocol_4_cli_runs_through_the_adapter(tmp_path, monkeypatch):
    """Copilot 1.0.113 and later speak protocol 4, which this SDK (3) was refused for: the
    owner's CLI now runs, spoken to through the adapter, which sends what opens and updates a
    session as protocol 4 has it (grouped); one of protocol 3 gets them as before."""
    log = tmp_path / "requests"
    monkeypatch.setenv("FAKE_COPILOT_REQUESTS", str(log))
    monkeypatch.setattr(backend_module, "fallback_runtime", lambda: pytest.fail("not needed"))
    for version, protocol in (("1.0.114", 4), ("1.0.112", 3)):
        log.write_text("")
        owner = fake_copilot(tmp_path / version / "copilot", version, protocol)
        backend = backend_module.copilot_backend(owner)
        await backend.start()
        try:
            assert backend.fallback is None and backend.client is not backend.refused
            options = {
                "on_permission_request": lambda *_: None, "working_directory": str(tmp_path),
                "reasoning_effort": "high", "streaming": True, "available_tools": ["view"],
                "enable_config_discovery": True,
            }  # fmt: skip
            session = await backend.create_session(session_id="s1", name="x", **options)
            await backend_module.shell_init(session, ["/w/init.sh"])
            await backend.resume_session("s1", **options)
        finally:
            await backend.stop()
        created, resumed = requests_of(log, "session.create"), requests_of(log, "session.resume")
        updated = requests_of(log, "session.options.update")
        if protocol == 4:
            assert created[0]["inference"] == {"reasoningEffort": "high"}
            assert created[0]["events"]["streaming"] is True
            assert created[0]["tools"] == {"availableTools": ["view"]}
            assert created[0]["permissions"] == {"request": True}
            assert created[0]["enableConfigDiscovery"] is True, "at the root, as protocol 4 has it"
            assert "toolFilterPrecedence" not in created[0] and "streaming" not in created[0]
            assert resumed[0]["tools"] == {"availableTools": ["view"]}
            assert updated[0]["tools"]["shell"]["initScripts"][0]["path"] == "/w/init.sh"
        else:
            assert created[0]["reasoningEffort"] == "high" and created[0]["streaming"] is True
            assert created[0]["availableTools"] == ["view"] and "inference" not in created[0]
            assert updated[0]["shell"]["initScripts"][0]["path"] == "/w/init.sh"


@needs_sh
async def test_the_fallback_is_set_up_ahead_and_runs_at_once_when_needed(tmp_path, monkeypatch):
    """Copilot FALLBACK_CLI is set up in the background at a start on the owner's CLI (nothing
    said), so that when that CLI updates itself past this SDK the next start runs it at once:
    no 100 MB wait, nothing said about setting it up, set up once."""
    monkeypatch.setattr(backend_module, "PREFETCH", True)
    starts = tmp_path / "starts"
    monkeypatch.setenv("FAKE_COPILOT_STARTS", str(starts))
    ours = fake_copilot(tmp_path / "sdk" / "copilot-runtime", "1.0.112", 3)
    calls = []

    def fallback_runtime():
        calls.append(threading.current_thread().name)
        return ours

    monkeypatch.setattr(backend_module, "fallback_runtime", fallback_runtime)
    owner = tmp_path / "bin" / "copilot"
    fake_copilot(owner, "1.0.112", 3)
    notes = []
    backend = backend_module.copilot_backend(str(owner))
    backend.note = notes.append
    await backend.start()
    await asyncio.wrap_future(backend_module.PROVISIONED[backend_module.FALLBACK_CLI])
    await backend.stop()
    assert backend.fallback is None and calls == ["copilot-runtime"], "in a thread of its own"
    fake_copilot(owner, "1.0.130", 5)  # It updated itself.
    backend = backend_module.copilot_backend(str(owner))
    backend.note = notes.append
    await backend.start()
    try:
        assert backend.fallback == ("1.0.130", "1.0.112")
        assert starts.read_text().split() == ["1.0.112", "1.0.130", "1.0.112"]
        assert notes == [] and calls == ["copilot-runtime"], "set up once, ahead"
    finally:
        await backend.stop()


def bundle(root: Path, version: str, program: str = "", complete: bool = True) -> Path:
    """A Copilot runtime in the SDK's cache (its layout: <version>/prebuilds/<platform>)."""
    from copilot._cli_version import get_runtime_platform

    folder = root / version / "prebuilds" / get_runtime_platform()
    folder.mkdir(parents=True)
    wrapper = folder / ("copilot-runtime.exe" if sys.platform == "win32" else "copilot-runtime")
    if program:
        fake_copilot(wrapper, version, compat.protocol_by_version(version) or 3)
    else:
        wrapper.write_text("x")
    (folder / "runtime.node").write_text("x")
    if complete:
        (folder / ".hostless-runtime-assets-v2").write_text("1\n")
    return wrapper


def test_the_runtimes_on_this_machine_are_those_this_sdk_speaks_best_first(tmp_path, monkeypatch):
    """The SDK's complete bundles (newest first), then the owner's CLI's own versions (run with
    node), of a protocol spoken here; not an incomplete bundle, nor an unknown or newer one."""
    cache, roots = tmp_path / "sdk", tmp_path / "pkg"
    monkeypatch.setattr(compat, "sdk_cache_root", lambda: cache)
    monkeypatch.setattr(compat, "package_roots", lambda: [roots])
    monkeypatch.setattr(compat.shutil, "which", lambda name: "/usr/bin/node")
    old = bundle(cache, "1.0.105")
    newer = bundle(cache, "1.0.114")
    bundle(cache, "1.0.111", complete=False)
    bundle(cache, "1.0.140")  # Beyond what is known here.
    (cache / "stray").mkdir()
    packaged = package(roots / "darwin-arm64" / "1.0.110", "1.0.110", 3)
    (packaged / "index.js").write_text("x")
    package(roots / "darwin-arm64" / "1.0.150", "1.0.150", 7).joinpath("index.js").write_text("x")
    found = compat.installed_runtimes()
    assert [(each.version, each.path, each.protocol) for each in found] == [
        ("1.0.114", str(newer), 4),
        ("1.0.105", str(old), 3),
        ("1.0.110", str(packaged / "index.js"), 3),
    ]
    monkeypatch.setattr(compat.shutil, "which", lambda name: None)
    assert [each.version for each in compat.installed_runtimes()] == ["1.0.114", "1.0.105"]
    assert compat.installed_runtimes(frozenset({3})) == [found[1]]


@needs_sh
@pytest.mark.parametrize(
    "failure",
    [
        "Failed to download from https://example.invalid/SUMS.txt: <urlopen error [Errno 8]>",
        "Checksum mismatch for github-copilot-1.0.112-darwin-arm64.tgz:\n  expected: 00",
    ],
)
async def test_an_unreachable_fallback_runs_the_best_copilot_on_this_machine(
    tmp_path, monkeypatch, failure
):
    """Copilot FALLBACK_CLI can't be set up (offline; a download that does not match its
    checksum): the best Copilot this SDK speaks already on this machine runs instead."""
    cache = tmp_path / "sdk"
    monkeypatch.setattr(compat, "sdk_cache_root", lambda: cache)

    def fallback_runtime():
        raise RuntimeError(failure)

    monkeypatch.setattr(backend_module, "fallback_runtime", fallback_runtime)
    bundle(cache, "1.0.108", program="yes")
    bundle(cache, "1.0.109")  # Not a program: it does not start.
    owner = fake_copilot(tmp_path / "bin" / "copilot", "1.0.130", 5)
    backend = backend_module.copilot_backend(owner)
    await backend.start()
    try:
        assert backend.fallback == ("1.0.130", "1.0.108")
    finally:
        await backend.stop()


@needs_sh
async def test_nothing_to_run_is_said_in_one_line_with_its_fix(tmp_path, monkeypatch, caplog):
    """No Copilot this SDK speaks can be set up or found: Chief of Staff says so in one line,
    with what to do, and logs no traceback; nothing reaches a client that did not start."""

    def offline():
        raise RuntimeError("Failed to download from https://example.invalid: <urlopen error>")

    monkeypatch.setattr(backend_module, "fallback_runtime", offline)
    owner = fake_copilot(tmp_path / "bin" / "copilot", "1.0.130", 5)
    harness = Harness(tmp_path / "data", Settings())
    caplog.set_level(logging.INFO)
    runtime = await harness.connect()
    runtime.backend_factory = lambda settings: backend_module.copilot_backend(owner)
    try:
        await runtime.reconnect()
        line = (
            "Your Copilot CLI (1.0.130) is newer than Chief of Staff supports, and Copilot "
            "1.0.112, which it works with, could not be set up: it could not be downloaded. "
            "Connect to the internet, then restart Chief of Staff (a download of about 100 MB)."
        )
        assert runtime.status["state"] == "error"
        assert runtime.status["error"].startswith(line + " A newer Chief of Staff may fix this")
        assert not [r for r in caplog.records if r.exc_info], "no traceback"
        with pytest.raises(backend_module.NotStartedError):
            await runtime.backend.list_models()  # type: ignore[union-attr]
    finally:
        await runtime.stop()


def test_why_a_fallback_could_not_be_set_up_is_said_with_its_fix():
    def said(error: str) -> str:
        return backend_module.unavailable("1.0.130", RuntimeError(error)).split(": ", 1)[1]

    assert said("Checksum mismatch for x.tgz:\n expected: 0") == (
        "its download did not match its checksum. Restart Chief of Staff to try again."
    )
    assert said("Copilot runtime bundle is not cached in /x and automatic downloads are "
                "disabled.") == (
        "downloads are turned off (COPILOT_SKIP_CLI_DOWNLOAD). Unset COPILOT_SKIP_CLI_DOWNLOAD, "
        "then restart Chief of Staff."
    )  # fmt: skip
    assert said("disk full") == "disk full."


async def test_a_cli_updating_itself_while_chief_of_staff_runs_is_chosen_again(
    tmp_path, monkeypatch
):
    """The owner's CLI updates itself while Chief of Staff runs; when Copilot connects again it
    is probed again and chosen again: past this SDK, Copilot FALLBACK_CLI (and the owner told in
    one line); back to one it speaks, the owner's again, its kept refusal gone."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    installed = {"version": "1.0.112", "protocol": 3}
    starts: list[str] = []

    class Client:
        def __init__(self, connection, env):
            self.path = connection.path

        async def start(self):
            ran = "fallback" if self.path == FALLBACK_PATH else installed["version"]
            starts.append(ran)
            if self.path != FALLBACK_PATH and installed["protocol"] not in compat.SPOKEN:
                raise RuntimeError(refusal(installed["protocol"]))

        async def stop(self):
            return []

        async def get_status(self):
            return SimpleNamespace(version="1.0.112")

        def __getattr__(self, name):
            return getattr(harness.client, name)

    async def cli_version(command):
        return installed["version"]

    monkeypatch.setattr(backend_module, "CopilotClient", Client)
    monkeypatch.setattr(backend_module, "cli_version", cli_version)
    monkeypatch.setattr(backend_module, "fallback_runtime", lambda: FALLBACK_PATH)
    # What its package says, read again at each start.
    monkeypatch.setattr(compat, "cli_protocol", lambda command, version: installed["protocol"])
    runtime.backend_factory = lambda settings: backend_module.copilot_backend("git")
    refusals = harness.store.directory / backend_module.REFUSED_CLI
    try:
        await runtime.reconnect()
        assert runtime.ready() and runtime.backend.fallback is None  # type: ignore[union-attr]
        installed.update(version="1.0.130", protocol=5)  # It updated itself meanwhile.
        await runtime.reconnect()
        await settle(runtime)
        assert runtime.ready()
        assert runtime.status["warning"] == (
            "Your Copilot CLI (1.0.130) is newer than Chief of Staff supports; using Copilot "
            "1.0.112 for now."
        )
        assert backend_module.read_refusal(refusals)["key"]["version"] == "1.0.130"
        assert starts == ["1.0.112", "fallback"], "not started only to be refused"
        await runtime.reconnect()
        assert starts[-1:] == ["fallback"], "kept refused: straight to the fallback"
        installed.update(version="1.0.131", protocol=4)  # Again: one spoken through the adapter.
        await runtime.reconnect()
        assert runtime.ready() and runtime.backend.fallback is None  # type: ignore[union-attr]
        assert starts == ["1.0.112", "fallback", "fallback", "1.0.131"]
        assert not refusals.exists() and not runtime.status.get("warning")
    finally:
        await runtime.stop()


MISMATCHED = [
    ("protocol", refusal(5)),
    ("protocol", "SDK supports versions 3-3, but server does not report a protocol version."),
    ("unknown method", "Request session.resume failed: Unhandled method session.resume"),
    ("unknown method", "unknown method: session.fork"),
    ("unknown method", "JSON-RPC Error -32601: Method not found"),
    ("missing parameter", "extensionSdkPath is required when standalone extensions are requested"),
    ("missing parameter", "Invalid params: missing field `workspaceRoot`"),
    ("extensions", "session.resume failed: Failed to initialize standalone session extensions: x"),
    ("extensions", "Failed to initialize standalone session extensions: No extension launch "
                   "provider is registered for standalone extensions"),
    ("session format", "Legacy session import events are no longer supported; this session cannot "
                       "be resumed without losing imported legacy state"),
    ("session format", "unsupported checkpoint schema version"),
]  # fmt: skip


@pytest.mark.parametrize(("kind", "said"), MISMATCHED)
def test_a_newer_copilots_mismatch_errors_are_known_by_kind(kind, said):
    assert compat.mismatch(RuntimeError(said)) == kind


@pytest.mark.parametrize(
    "said",
    [
        "Session not found: 1234",
        "Invalid params: unknown field `x`",
        "rate limited: try again later",
        "Model 'gpt-x' is not available",
        "Cannot resume a session with a different MCP OAuth client identity.",
        "SDK supports versions 3-4",
    ],
)
def test_other_errors_are_the_sessions_own(said):
    assert compat.mismatch(said) == ""


def refused_with(harness, said: str, *, ids: tuple[str, ...] = (), creating: bool = False):
    """Copilot refusing to resume these sessions (and, `creating`, to create any) with `said`."""
    resume, create = harness.client.resume_session, harness.client.create_session

    async def resuming(session_id, **options):
        if session_id in ids:
            raise RuntimeError(f"JSON-RPC Error -32603: Request session.resume failed: {said}")
        return await resume(session_id, **options)

    async def refusing(**options):
        raise RuntimeError(f"JSON-RPC Error -32603: Request session.create failed: {said}")

    harness.client.resume_session = resuming
    if creating:
        harness.client.create_session = refusing
    return create


@pytest.mark.parametrize(("kind", "said"), [m for m in MISMATCHED if m[0] != "extensions"])
async def test_a_session_a_newer_copilot_cant_resume_goes_on_fresh(tmp_path, kind, said):
    """Any version mismatch resuming a session (not only its extensions: resume-compat's) is
    met as one: a fresh Copilot session goes on for it, its timeline saying so in one line."""
    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    router = harness.router_id
    await runtime.detach(router)
    old = runtime.copilot_id(router)
    harness.store.delete(f"fresh:{old}")
    refused_with(harness, said, ids=(old,))
    try:
        fresh = await runtime.session_for(router)
        assert fresh.session_id != old and runtime.copilot_id(router) == fresh.session_id
        notices = [m["text"] for m in harness.store.messages(router) if m["kind"] == "notice"]
        assert CONTINUED_AFRESH in notices and CONTINUED_FRESH not in notices
        assert not [m for m in harness.store.messages(router) if m["kind"] == "error"]
    finally:
        await runtime.stop()


async def test_a_worker_no_session_of_this_copilot_opens_for_is_said_plainly(tmp_path):
    """Resumed and created alike refused for a version mismatch: the owner's message is kept,
    one plain line (no JSON-RPC) offers Start fresh, as for the extensions resume-compat met."""
    from test_progress import spawn

    harness = Harness(tmp_path / "data")
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Retry the probe access")
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    await runtime.detach(worker)
    old = runtime.copilot_id(worker)
    refused_with(harness, "Unhandled method session.resume", ids=(old,), creating=True)
    try:
        with pytest.raises(FreshStartError) as raised:
            await runtime.delivery.deliver(worker, "Yes, retry it", kind="owner", steer=True)
        said = str(raised.value)
        assert raised.value.fresh == worker and "JSON-RPC" not in said
        assert "Unhandled" not in said and "kept" in said
    finally:
        await runtime.stop()


def test_a_newer_copilots_events_are_taken_and_said_once(caplog):
    """A type this SDK does not know, a value it has no member for, a field it can't read: the
    event is taken (the value as it is; the last as an event of unknown type), never failing,
    and each said once in the log, at debug."""
    caplog.set_level(logging.DEBUG, logger="chiefofstaff.compat")
    compat._said.clear()
    base = {"id": "487eb139-0676-4770-8020-ef48129cc1d3", "timestamp": "2026-11-19T06:05:04Z"}
    new = session_event_from_dict({**base, "type": "session.hologram", "data": {"x": 1}})
    assert new.type == SessionEventType.UNKNOWN and new.raw_type == "session.hologram"
    session_event_from_dict({**base, "type": "session.hologram", "data": {"x": 2}})
    broken = session_event_from_dict({**base, "type": "session.idle", "data": None})
    broken_again = session_event_from_dict({**base, "type": "session.idle", "data": "x"})
    assert broken.type == SessionEventType.UNKNOWN and broken.raw_type == "session.idle"
    assert broken_again.data.raw == "x"  # type: ignore[union-attr]
    assert ShutdownType("teleport").value == "teleport"
    assert ShutdownType("teleport") is ShutdownType("teleport")
    said = [r.getMessage() for r in caplog.records if r.name == "chiefofstaff.compat"]
    assert len(said) == 3 and all(r.levelno == logging.DEBUG for r in caplog.records)
    assert "session.hologram" in said[0] and "session.idle" in said[1] and "teleport" in said[2]


MALLOC = "can't turn off malloc stack logging because it was not enabled."


def test_macos_malloc_notice_from_children_is_dropped_and_their_errors_kept(caplog):
    """The owner's terminal: a line of macOS's malloc (cf. copilot-cli#4375) each time a Python
    of an MCP server started, as the SDK logs each line of the Copilot runtime's stderr as a
    warning; Claude's stderr took it for its last words. Dropped, and only it: an error stays."""
    notice = f"python3(12345) MallocStackLogging: {MALLOC}"
    error = "Error: MCP server acme could not start: spawn uvx ENOENT"
    written = f"copilot(4242) MallocStackLogging: {MALLOC}\n{notice}\n{error}\n"
    process = subprocess.Popen(
        [sys.executable, "-c", f"import sys; sys.stderr.write({written!r})"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )  # fmt: skip
    caplog.set_level(logging.WARNING, logger="copilot._jsonrpc")
    client = JsonRpcClient(process)
    client._running = True  # As its thread reads the runtime's stderr once started.
    client._stderr_loop()
    process.wait()
    assert [r.getMessage() for r in caplog.records] == [f"[CLI] {error}"]
    assert error in client.get_stderr_output(), "a runtime that fails to start says why"
    claude = SimpleNamespace(stderr=[], worker="w1")
    for line in (error, notice):
        ClaudeWorker.on_stderr(claude, line)  # type: ignore[arg-type]
    assert claude.stderr == [error]
    assert not backend_module.malloc_notice(f"python3(1) MallocStackLogging: {MALLOC} {error}")


async def test_a_sessions_history_with_an_event_it_cant_read_is_read(tmp_path):
    """A session's history (session.getMessages) failed whole for one event this SDK can't
    read: now that one is of unknown type, the others as they were."""
    from copilot.session import CopilotSession

    base = {"id": "487eb139-0676-4770-8020-ef48129cc1d3", "timestamp": "2026-11-19T06:05:04Z"}

    class Rpc:
        async def request(self, method, params):
            return {"events": [{**base, "type": "session.idle", "data": None},
                               {**base, "type": "session.idle", "data": {}}]}  # fmt: skip

    session = CopilotSession.__new__(CopilotSession)
    session._client = Rpc()  # type: ignore[attr-defined]
    session.session_id = "s1"
    events = await session.get_events()
    assert [event.type for event in events] == [
        SessionEventType.UNKNOWN,
        SessionEventType.SESSION_IDLE,
    ]
