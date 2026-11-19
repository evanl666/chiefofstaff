"""The agent backend that runs Chief of Staff's sessions.

Chief of Staff uses a small part of what an agent SDK offers. This module names that part, so the
rest of the code depends on these Protocols rather than on the Copilot SDK. GitHub Copilot is the
backend that ships; the scripted backend (`scripted.py`) runs the demo mode and shows the runtime
other coding agents as their adapters would, and tests/test_backend_contract.py checks that they
all behave the way the runtime relies on.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import hashlib
import importlib.metadata
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from copilot import CopilotClient, RuntimeConnection
from copilot.client import SessionMetadata
from copilot.generated.rpc import (
    MCPOauthLoginRequest,
    QueueRemoveAtRequest,
    SandboxConfig,
    SandboxConfigAuth,
    SandboxConfigUserPolicy,
    SandboxConfigUserPolicyFilesystem,
    SandboxConfigUserPolicyNetwork,
    SandboxConfigUserPolicySeatbelt,
    SessionsForkRequest,
    SessionUpdateOptionsParams,
    ShellInitScript,
    ShellInitScriptShell,
    ShellOptions,
    TasksCancelRequest,
)

from . import compat, machine
from .compat import open_enum as open_enum

# The machine's processes (moved to their own module, which agentrun imports without the SDK).
from .processes import descendants as descendants
from .processes import end_processes as end_processes
from .processes import process_table as process_table
from .processes import running as running

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Capabilities:
    name: str
    # send(mode="immediate") reaches a turn in progress.
    steering: bool
    # A session can be resumed after a restart, with its conversation.
    resume: bool
    # Shell commands, file writes and MCP calls ask on_permission_request first.
    permission_callbacks: bool
    # MCP servers that need OAuth ask on_mcp_auth_request; sign-ins are kept by the backend.
    mcp_sign_in: bool
    # Shell commands can run in an OS sandbox (sandbox_config).
    sandbox: bool
    # A message sent shows as a user.message event once the session takes it up.
    echoes: bool = True
    # A session can be forked: its backend's fork_session(session_id, name) makes a new session
    # with its conversation up to its turn in progress, and delete_session(session_id) removes
    # one (see forks.py).
    fork: bool = False


SendMode = Literal["enqueue", "immediate"]


class AgentSession(Protocol):
    """A session may also answer running_background, take_queued, stop_background and
    queue_holds itself (async methods of those names, without arguments, answering as the
    helpers of those names do): another agent's adapter does, over its own queue and the commands
    it runs. Copilot's sessions do not; the helpers reach them through their RPCs."""

    session_id: str

    async def send(
        self, prompt: str, *, mode: SendMode = "enqueue", attachments: list[Any] | None = None
    ) -> Any:
        """Queue a message and return at once; the turn's progress arrives as events.
        `attachments`: files sent with it ({"type": "file", "path", "displayName"})."""
        ...

    async def abort(self) -> None:
        """Stop the turn in progress."""
        ...

    async def disconnect(self) -> None:
        """Stop receiving events; the session stays resumable."""
        ...

    async def set_model(
        self, model: str, *, reasoning_effort: str | None = None, context_tier: Any = None
    ) -> None: ...

    async def get_events(self) -> list[Any]:
        """The session's history; its user.message events show what it received."""
        ...

    def on(self, handler: Callable[[Any], None]) -> Any:
        """Receive every event: `event.type.value` names it, `event.data` holds its fields."""
        ...


class AgentBackend(Protocol):
    capabilities: Capabilities

    async def start(self) -> None: ...

    async def stop(self) -> Any: ...

    async def version(self) -> str: ...

    async def get_auth_status(self) -> Any:
        """Has `isAuthenticated`."""
        ...

    async def list_models(self) -> list[Any]: ...

    async def list_sessions(self) -> list[Any]: ...

    async def get_session_metadata(self, session_id: str) -> Any: ...

    async def create_session(self, *, session_id: str, name: str, **options: Any) -> AgentSession:
        """Options: system_message, tools, available_tools, working_directory, model and the
        callbacks on_permission_request, on_user_input_request, on_elicitation_request and
        on_mcp_auth_request (see Runtime.options)."""
        ...

    async def resume_session(self, session_id: str, **options: Any) -> AgentSession: ...

    async def mcp_sign_in_url(self, session: AgentSession, server: str) -> str:
        """Where the owner signs in to an MCP server; "" when the backend has credentials."""
        ...

    def sandbox(self, policy: SandboxPolicy) -> Any:
        """The session option (sandbox_config) that applies this policy."""
        ...

    async def gh_works_in_sandbox(self) -> bool | None:
        """Whether gh can reach GitHub from sandboxed commands; None when not applicable."""
        ...

    async def open_elsewhere(self) -> dict[str, int]:
        """Sessions another program has open right now (a terminal): session id -> its pid."""
        ...

    def processes(self) -> dict[int, str]:
        """The processes running the backend (Copilot's CLI and the program it starts), with
        their command lines; empty when nothing runs apart from this process. Blocking: call
        it in a thread."""
        ...

    def alive(self) -> bool:
        """False once the backend's process has exited by itself (crashed, killed)."""
        ...

    async def read_history(self, session_id: str, offset: int) -> tuple[list[dict], int, float]:
        """A session's events after `offset` (its latest ones when offset is 0) without
        attaching to it, the offset to read from next time, and when it last changed."""
        ...

    async def read_usage(self, session_id: str, offset: int) -> tuple[list[dict], int]:
        """The usage records in a session's log after `offset` (all of them from 0), and the
        offset to read from next time: its model calls (session.usage_record) and its
        compactions (session.compaction_complete), whose calls have no record of their own."""
        ...

    def session_log(self, session_id: str) -> Path | None:
        """Where a session's log is, for others.py to read without attaching to it (one JSON
        event a line, as the Copilot CLI writes it); None when the backend keeps none for it."""
        ...


@dataclass(frozen=True)
class SandboxPolicy:
    """What a sandboxed session's shell commands may touch."""

    readwrite: tuple[str, ...]
    readonly: tuple[str, ...] = ()
    # Empty: any host. Otherwise only these hosts are reachable.
    allowed_hosts: tuple[str, ...] = ()
    # Loopback: servers its tests start, a build tool's daemon (Gradle's), local services.
    local_network: bool = False
    # Neither readable nor writable.
    denied: tuple[str, ...] = ()


@dataclass(frozen=True)
class StepSandboxPolicy(SandboxPolicy):
    """A shepherd step's (shepherd.py): what a worker's sandbox has, and less."""

    # git and gh get no GitHub credentials through Copilot's proxy.
    credentials: bool = False
    # Any outbound traffic at all (the hosts above narrow it).
    outbound: bool = False
    # Never reachable, whatever else allows them.
    blocked_hosts: tuple[str, ...] = ()
    # macOS keychains: never.
    keychain: bool | None = False


# The agent a session runs on unless its row names another (Runtime.agent): every session today.
COPILOT_AGENT = "copilot"

COPILOT = Capabilities(
    name="GitHub Copilot",
    steering=True,
    resume=True,
    permission_callbacks=True,
    mcp_sign_in=True,
    sandbox=True,
    fork=True,
)
# The Copilot CLI versions this release was tested with (live end-to-end and contract tests).
TESTED_CLI = "1.0.107"
OLDEST_CLI = "1.0.100"
# The SDK refusing a Copilot CLI that speaks a newer protocol than it does (the CLI updates
# itself; the SDK supporting it comes later): "SDK supports versions 3-3, but server reports
# version 4".
NEWER_PROTOCOL = re.compile(r"SDK supports versions (\d+)-(\d+), but server reports version (\d+)")
CLI_VERSION = re.compile(r"\b(\d+\.\d+\.\d+(?:-\d+)?)\b")
# The Copilot that runs instead then: the newest that speaks this SDK's protocol, the one this
# release was tested with (the SDK's own pinned runtime, 1.0.105, lacks what Chief of Staff
# reads of later ones, such as which client last ran a session).
FALLBACK_CLI = "1.0.112"


def fallback_runtime() -> str:
    """The Copilot runtime FALLBACK_CLI, as the SDK provisions its own (from the CLI's GitHub
    release, its checksum checked, into the SDK's cache: downloaded once); "" when this SDK
    can't provision another version (then its own pinned one runs)."""
    try:
        from copilot._cli_download import ensure_runtime_wrapper
    except ImportError:
        return ""
    return ensure_runtime_wrapper(FALLBACK_CLI)


# FALLBACK_CLI set up ahead (provision), in a thread of its own: at each start, before it is
# needed, so that a Copilot CLI updating itself past this SDK never costs a 100 MB wait or a
# failed start. Once a process, again after a failure. Off in tests (conftest), but for theirs.
PREFETCH = True
PROVISIONED: dict[str, concurrent.futures.Future] = {}
PROVISIONING = threading.Lock()


def provision() -> concurrent.futures.Future:
    """FALLBACK_CLI's path once set up (fallback_runtime), from the one set-up running or done
    in this process; another starts when none ran, the last one failed, or what it set up is
    gone (a cache cleaned)."""
    with PROVISIONING:
        future = PROVISIONED.get(FALLBACK_CLI)
        if future is None or (
            future.done()
            and (future.exception() is not None or not Path(future.result() or ".").exists())
        ):
            future = PROVISIONED[FALLBACK_CLI] = concurrent.futures.Future()
            # A thread of its own, never waited for at exit (the SDK downloads into a staging
            # folder it renames once complete): not the loop's executor, which is.
            threading.Thread(
                target=provide, args=(future,), name="copilot-runtime", daemon=True
            ).start()
    return future


def provide(future: concurrent.futures.Future) -> None:
    try:
        future.set_result(fallback_runtime())
    except BaseException as exc:  # (Said where it is awaited.)
        future.set_exception(exc)


def prefetch() -> None:
    """FALLBACK_CLI set up in the background, said nowhere unless it is used."""
    if PREFETCH:
        provision().add_done_callback(prefetched)


def prefetched(future: concurrent.futures.Future) -> None:
    if (error := future.exception()) is not None:
        logger.debug("Copilot %s could not be set up ahead: %s", FALLBACK_CLI, error)


def newer_protocol(error: BaseException | str) -> bool:
    """Whether the SDK refused the CLI for speaking a newer protocol than it supports."""
    found = NEWER_PROTOCOL.search(str(error))
    return bool(found) and int(found.group(3)) > int(found.group(2))


# Copilot's own runtime (the SDK's: FALLBACK_CLI) runs no extension by itself: a session that
# asks for them must bring the JavaScript SDK they import and a launcher for them, and this SDK
# has neither. A session asks for them with config discovery (every worker) once the owner's
# Copilot is in experimental mode ("experimental": true in its settings.json, as
# `/experimental on` leaves it): then each create and resume failed ("Failed to initialize
# standalone session extensions: extensionSdkPath is required when standalone extensions are
# requested"). The owner's own CLI brings both, and runs them.
EXTENSIONS_REFUSED = compat.EXTENSIONS_REFUSED


def extensions_refused(error: BaseException | str) -> bool:
    """Whether Copilot refused to open a session for the extensions it could not run."""
    return bool(EXTENSIONS_REFUSED.search(str(error)))


def adapted_protocol(error: BaseException | str) -> int | None:
    """The protocol of a CLI the SDK refused that it speaks through compat.adapt, else None."""
    found = NEWER_PROTOCOL.search(str(error))
    return int(found.group(3)) if found and int(found.group(3)) in compat.ADAPTED else None


def unavailable(theirs: str, error: BaseException) -> str:
    """One line for the owner when no Copilot this SDK speaks could be set up: why, and the
    fix."""
    said = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
    if "Checksum mismatch" in said:
        why, fix = "its download did not match its checksum", "Restart Chief of Staff to try again."
    elif "downloads are disabled" in said:
        why = "downloads are turned off (COPILOT_SKIP_CLI_DOWNLOAD)"
        fix = "Unset COPILOT_SKIP_CLI_DOWNLOAD, then restart Chief of Staff."
    elif "Failed to download" in said or "urlopen error" in said:
        why = "it could not be downloaded"
        fix = "Connect to the internet, then restart Chief of Staff (a download of about 100 MB)."
    else:
        why, fix = said[:200], ""
    return (
        f"Your Copilot CLI{f' ({theirs})' if theirs else ''} is newer than Chief of Staff "
        f"supports, and Copilot {FALLBACK_CLI}, which it works with, could not be set up: {why}."
        + (f" {fix}" if fix else "")
    )


class UnavailableError(RuntimeError):
    """No Copilot this SDK speaks could be started: said in one line (unavailable), no
    traceback."""


class ExpectedRefusal(logging.Filter):
    """The SDK's warning, with its traceback, when it refuses a CLI that speaks a newer protocol:
    expected (FALLBACK_CLI runs then), and said in one line instead (CopilotBackend.start)."""

    def filter(self, record: logging.LogRecord) -> bool:
        error = record.exc_info[1] if record.exc_info else None
        return error is None or not newer_protocol(error)


logging.getLogger("copilot.client").addFilter(ExpectedRefusal())


# What macOS's malloc writes on stderr in a process that turns off its stack logging without
# having had it on (cf. copilot-cli#4375): a Copilot runtime, Codex, Claude Code, an MCP server
# or a Python one of them starts, each time one starts. Harmless: dropped wherever Chief of
# Staff reads its children's stderr, never in the terminal nor taken for their last words.
MALLOC_NOTICE = re.compile(
    r"(?:\[CLI\] )?[^\n]{1,200}?\(\d+\) MallocStackLogging: can't turn off malloc stack "
    r"logging because it was not enabled\.?"
)


def malloc_notice(line: str) -> bool:
    """Whether a line a child process wrote on stderr is macOS's malloc notice, and only that."""
    return bool(MALLOC_NOTICE.fullmatch(line.strip()))


class MallocNotice(logging.Filter):
    """The Copilot runtime's stderr, which the SDK logs line by line as warnings ("[CLI] …": in
    the terminal Chief of Staff runs in), less macOS's malloc notice. All else it says stays."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not malloc_notice(record.getMessage())


logging.getLogger("copilot._jsonrpc").addFilter(MallocNotice())


# A newer Copilot's events this SDK can't read are taken as unknown ones (compat.open_enum: a
# session.shutdown's "destroy", which Copilot 1.0.112 sends when a session is deleted, failed with
# a traceback in the log and the event was dropped).
compat.tolerate_newer_events()

# Kept in Chief of Staff's data folder (CopilotBackend.refusals): the Copilot CLI this SDK
# refused for speaking a newer protocol, by its program's real path, the version it says it is
# and the SDK's. While those stay the same, a start goes straight to FALLBACK_CLI: the refused
# start took 2.5-4 s.
REFUSED_CLI = "copilot-refused.json"


def refusal_of(error: object) -> str:
    """What a refusal said, in short: "SDK supports versions 3-3, but server reports version 4"."""
    found = NEWER_PROTOCOL.search(str(error or ""))
    return found.group(0) if found else "a newer protocol"


@functools.cache
def sdk_version() -> str:
    try:
        return importlib.metadata.version("github-copilot-sdk")
    except importlib.metadata.PackageNotFoundError:
        return ""


def refused_key(command: str, version: str) -> dict[str, str]:
    """What a refusal is kept for: the CLI (its real path and the version it says it is, which
    changes when it updates itself behind the same path), this SDK and the protocols spoken over
    it (compat.SPOKEN: one refused before the adapter spoke it is tried again)."""
    return {"cli": os.path.realpath(command), "version": version, "sdk": sdk_version(),
            "speaks": ",".join(map(str, sorted(compat.SPOKEN)))}  # fmt: skip


def read_refusal(file: Path) -> dict:
    try:
        kept = json.loads(file.read_text())
    except (OSError, ValueError):
        return {}
    return kept if isinstance(kept, dict) and isinstance(kept.get("key"), dict) else {}


def keep_refusal(file: Path, key: dict[str, str], refusal: str) -> None:
    with suppress(OSError):
        temporary = file.with_name(f".{file.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps({"key": key, "refusal": refusal}))
        temporary.replace(file)


async def cli_version(command: str) -> str:
    """The version a Copilot CLI says it is (`copilot --version`), "" when it doesn't say."""
    try:
        process = await asyncio.create_subprocess_exec(
            command, "--version", stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
    except OSError:
        return ""
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 15)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        return ""
    except asyncio.CancelledError:
        with suppress(ProcessLookupError):
            process.kill()
        raise
    found = CLI_VERSION.search(output.decode(errors="replace"))
    return found.group(1) if found else ""


# What Chief of Staff calls itself to Copilot, which the CLI keeps with each session as the
# client that made it and the one that last resumed it. Copilot also sends it as the User-Agent
# of the remote MCP servers a session calls, and a server that keeps each user's connections per
# app takes its part before the slash for the app (E137): as "chiefofstaff" its workers were an
# app of their own, with none of the connections the owner made for Copilot (Glean said "not
# connected" in every one). It is a Copilot CLI client; the rest tells its sessions apart from
# the owner's own (github/cli).
CLIENT_NAME = "copilot-cli/chiefofstaff"
# Its names, this one and those its sessions were made with before (chiefofstaff; work-buddy,
# its earlier name). Only these exactly: never anything else that starts copilot-cli/.
OWN_CLIENTS = frozenset({CLIENT_NAME, "chiefofstaff", "work-buddy"})
# What the CLI records as the maker of a session a program made through Copilot's SDK without
# naming itself: a script's (a batch job, an eval), often one of many.
SDK_CLIENT = "sdk"


def made_by_us(item: dict) -> bool:
    """A session Chief of Staff made, under any data directory, that no other client (a terminal)
    has resumed since."""
    made = item.get("clientName") or ""
    return made in OWN_CLIENTS and (item.get("lastClientName") or made) in OWN_CLIENTS


def made_by_a_program(item: dict) -> bool:
    """A session a program made through Copilot's SDK without naming itself, that no other client
    (a terminal) has run a turn in since: not the owner's own."""
    made = item.get("clientName") or ""
    return made == SDK_CLIENT and (item.get("lastClientName") or made) == SDK_CLIENT


class NotStartedError(ConnectionError):
    """Copilot did not start (the CLI it was given was refused, and the one that runs instead
    could not be set up): what was to be sent waits until it runs."""


class CopilotBackend:
    """GitHub Copilot through its Python SDK: one CLI process runs every session."""

    capabilities = COPILOT

    def __init__(
        self, client: CopilotClient, command: str = "copilot", env: dict[str, str] | None = None
    ):
        self.client = client
        self.command = command
        self.env = env
        # The CLI's process and those it started (the npm loader runs Copilot's own program,
        # which outlives the loader when only that is killed), as last seen: pid -> command.
        self.started: dict[int, str] = {}
        # Told what the start is doing when it takes a while (Runtime.backend_note).
        self.note: Callable[[str], None] | None = None
        # When the owner's CLI speaks a newer protocol than this SDK: (its version, the version
        # of the SDK's own Copilot runtime used instead).
        self.fallback: tuple[str, str] | None = None
        # Done once start() is over (it ran, or failed); a client Copilot's SDK refused, which
        # is never started again (the SDK starts a client on its first use).
        self.starting: asyncio.Future | None = None
        self.refused: CopilotClient | None = None
        # Where a refusal of the owner's CLI is kept (REFUSED_CLI, in Chief of Staff's data
        # folder: Runtime sets it); None: not kept, the owner's is tried at each start.
        self.refusals: Path | None = None
        # Its sessions say they run no extension (EXTENSIONS_REFUSED): on Copilot's own runtime,
        # or once Copilot refused one for them.
        self.no_extensions = False

    async def start(self) -> None:
        self.starting = asyncio.get_running_loop().create_future()
        try:
            await self.start_one()
        finally:
            self.starting.set_result(None)
        prefetch()  # (After the start: never in its way.)

    async def start_one(self) -> None:
        """The owner's CLI, when this SDK speaks its protocol (itself, or through compat.adapt);
        or, when it speaks a newer one (its package says so, its handshake did now, or at an
        earlier start of the same CLI and SDK, kept in `refusals`), Copilot FALLBACK_CLI."""
        theirs = await cli_version(self.command)
        protocol = await asyncio.to_thread(compat.cli_protocol, self.command, theirs)
        known = await asyncio.to_thread(read_refusal, self.refusals) if self.refusals else {}
        if known and theirs and known["key"] == refused_key(self.command, theirs):
            logger.info("Copilot CLI %s speaks a newer protocol than this SDK (%s, at an "
                        "earlier start): Copilot %s runs instead.",
                        theirs, refusal_of(known.get("refusal")), FALLBACK_CLI)  # fmt: skip
            self.refused = self.client  # (Never started: nor on a first use.)
            await self.start_fallback(str(known.get("refusal") or ""), theirs, tried=False)
            return
        if protocol is not None and protocol > max(compat.SPOKEN):
            # Its package says so: not started only to be refused (2.5-4 s).
            refusal = (f"SDK supports versions {compat.NATIVE}-{max(compat.SPOKEN)}, but "
                       f"server reports version {protocol}")  # fmt: skip
            logger.info("Copilot CLI %s speaks a newer protocol than this SDK (%s): Copilot %s "
                        "runs instead.", theirs, refusal, FALLBACK_CLI)  # fmt: skip
            await self.refuse(theirs, refusal)
            self.refused = self.client
            await self.start_fallback(refusal, theirs, tried=False)
            return
        self.watch_start(self.client)
        try:
            await self.client.start()
        except RuntimeError as exc:
            if not newer_protocol(exc):
                raise
            self.refused = self.client
            logger.info("Copilot CLI %s speaks a newer protocol than this SDK (%s): Copilot %s "
                        "runs instead.", theirs or self.command, refusal_of(exc),
                        FALLBACK_CLI)  # fmt: skip
            await self.refuse(theirs, str(exc))
            await self.start_fallback(str(exc), theirs)
            return
        if known and self.refusals:
            self.refusals.unlink(missing_ok=True)  # (It changed, and this SDK speaks it now.)

    async def refuse(self, theirs: str, refusal: str) -> None:
        """The owner's CLI kept as refused (`refusals`), for the next start to skip."""
        if self.refusals and theirs:
            key = refused_key(self.command, theirs)
            await asyncio.to_thread(keep_refusal, self.refusals, key, refusal)

    async def ready_client(self) -> CopilotClient:
        """The client, once start() is over: a session opened, or a message sent, while Copilot
        starts (the owner's CLI refused, the version it works with being set up) waits for it,
        and never reaches a client that was refused."""
        if self.starting is not None and not self.starting.done():
            await asyncio.shield(self.starting)
        if self.client is self.refused:
            raise NotStartedError("Copilot is not running: it could not start.")
        return self.client

    def watch_start(self, client: CopilotClient) -> None:
        """A client's handshake: a CLI of a protocol this SDK speaks through compat.adapt is
        spoken to through it; one it refuses has its processes taken (processes): the SDK then
        stops the npm loader only, and Copilot's own program would run on."""
        verify = getattr(client, "_verify_protocol_version", None)
        if verify is None:
            return

        def taken() -> None:
            if process := self.cli_process():
                table = process_table()
                tree = descendants(table, process.pid)
                self.started |= {child: table[child][1] for child in tree if child in table}

        async def verifying() -> Any:
            try:
                return await verify()
            except Exception as exc:
                if (protocol := adapted_protocol(exc)) is not None:
                    compat.adapt(client, protocol)
                    return None
                # (It answered: its program runs now, the loader not stopped yet.)
                with suppress(Exception):
                    await asyncio.to_thread(taken)
                raise

        client._verify_protocol_version = verifying  # type: ignore[method-assign]

    async def start_fallback(self, refused: str, theirs: str = "", tried: bool = True) -> None:
        """The owner's Copilot CLI updated itself past what this SDK speaks: Copilot
        FALLBACK_CLI runs instead (set up once, ahead, into the SDK's own cache: prefetch; the
        same sign-in, settings and sessions, in ~/.copilot), until a release supports the new
        one. When it can't be set up (offline, a checksum that does not match), the newest
        Copilot already on this machine that this SDK speaks; else UnavailableError, in one
        line. `tried`: the owner's was started (and refused) first."""
        if tried:
            with suppress(Exception):
                await self.stop()  # (What the refused CLI left running.)
        self.started = {}
        theirs = theirs or await cli_version(self.command)
        setting_up = provision()
        if self.note and not setting_up.done():
            self.note(
                f"Your Copilot CLI{f' ({theirs})' if theirs else ''} is newer than Chief of "
                "Staff supports: setting up the Copilot version it works with (a download of "
                "about 100 MB, the first time)…"
            )
        # The SDK's own: not one COPILOT_CLI_PATH names (a Copilot CLI sets it for what it
        # runs, as when Chief of Staff was started from one).
        env = {key: value for key, value in (self.env or os.environ).items()
               if key != "COPILOT_CLI_PATH"}  # fmt: skip
        failed: BaseException | None = None
        try:
            paths: list[str | None] = [await asyncio.wrap_future(setting_up) or None]
        except Exception as exc:
            failed, paths = exc, []
            logger.info("Copilot %s could not be set up (%s): trying those on this machine.",
                        FALLBACK_CLI, exc)  # fmt: skip
        if paths:
            if (error := await self.run_first(paths, env, theirs)) is None:
                return
            failed = error
        found = await asyncio.to_thread(compat.installed_runtimes)
        others = [each.path for each in found if each.version != theirs and each.path not in paths]
        if others and await self.run_first(others, env, theirs) is None:
            return
        if self.refused is not None:
            self.client = self.refused  # (Nothing reaches a client that did not start.)
        error = failed or RuntimeError("no Copilot runtime")
        raise UnavailableError(unavailable(theirs, error)) from error

    async def run_first(
        self, paths: Sequence[str | None], env: dict[str, str], theirs: str
    ) -> BaseException | None:
        """The first of these Copilot runtimes (None: the SDK's own) that starts runs instead of
        the owner's (`fallback`); the last one's error when none does."""
        failed: BaseException | None = None
        for path in paths:
            try:
                self.client = await asyncio.to_thread(
                    CopilotClient, connection=RuntimeConnection.for_stdio(path=path), env=env
                )
                self.watch_start(self.client)
                await self.client.start()
                ours = (await self.client.get_status()).version
            except Exception as exc:
                failed = exc
                logger.info("Copilot at %s did not start: %s", path or "the SDK's own", exc)
                with suppress(Exception):
                    await self.stop()
                self.started = {}
                continue
            self.fallback = (theirs, ours)
            self.no_extensions = True
            return None
        return failed

    def cli_process(self) -> subprocess.Popen | None:
        """The CLI's process, wherever this SDK keeps it: `_cli_process` (SDK 1.0), `_process`
        (0.x; in 1.0 the same process, or the socket of a TCP connection: not a process), or
        its JSON-RPC client's `process`."""
        client = self.client
        for holder, name in ((client, "_cli_process"), (client, "_process"),
                             (getattr(client, "_client", None), "process")):  # fmt: skip
            found = getattr(holder, name, None)
            if isinstance(getattr(found, "pid", None), int) and callable(
                getattr(found, "poll", None)
            ):
                return found
        return None

    def processes(self) -> dict[int, str]:
        if process := self.cli_process():
            table = process_table()
            self.started |= {
                pid: table[pid][1] for pid in descendants(table, process.pid) if pid in table
            }
        return dict(self.started)

    def alive(self) -> bool:
        process = self.cli_process()
        return process is None or process.poll() is None

    async def stop(self) -> Any:
        tree = await asyncio.to_thread(lambda: copilot_tree(self.processes()))
        if not self.alive():
            # The loader is gone, but the program it ran still holds the pipes: it goes first.
            await asyncio.to_thread(end_processes, tree)
        try:
            return await self.client.stop()
        finally:
            # What Copilot left running (a shell's server), or Copilot itself if it hangs.
            await asyncio.to_thread(end_processes, tree)

    async def version(self) -> str:
        return (await (await self.ready_client()).get_status()).version

    async def get_auth_status(self) -> Any:
        return await (await self.ready_client()).get_auth_status()

    async def list_models(self) -> list[Any]:
        return await (await self.ready_client()).list_models()

    async def list_sessions(self) -> list[Any]:
        """Every session, each marked `made_by_us` (see made_by_us) and `made_by_program` (see
        made_by_a_program), with the client that made it (`client_name`), the one that last ran
        a turn in it (`last_client_name`, "" when none is recorded) and its `name` ("" when it
        has none); `clients_listed` says these were read. The SDK's list leaves out which
        client made a session, the CLI's answer has it. Unmarked when that cannot be read."""
        client = await self.ready_client()
        try:
            answer = await client._client.request("session.list", {})  # type: ignore[union-attr]
            listed = []
            unread = 0
            for item in answer.get("sessions", []):
                try:
                    meta = SessionMetadata.from_dict(item)
                except (AssertionError, ValueError, TypeError, AttributeError):
                    # One the SDK can't read (a field missing, a time it doesn't parse) is left
                    # out, not every session with it: the sidebar showed none of the owner's.
                    unread += 1
                    continue
                meta.made_by_us = made_by_us(item)  # type: ignore[attr-defined]
                meta.made_by_program = made_by_a_program(item)  # type: ignore[attr-defined]
                vars(meta).update(
                    client_name=str(item.get("clientName") or ""),
                    last_client_name=str(item.get("lastClientName") or ""),
                    # The name it was given (`copilot --name`): see provenance.named.
                    name=str(item.get("name") or ""),
                    clients_listed=True,
                )
                listed.append(meta)
            if unread:
                logger.warning("Left out %d Copilot session(s) the SDK can't read.", unread)
            return listed
        except Exception:
            return await client.list_sessions()

    async def get_session_metadata(self, session_id: str) -> Any:
        return await (await self.ready_client()).get_session_metadata(session_id)

    async def create_session(self, *, session_id: str, name: str, **options: Any) -> AgentSession:
        client = await self.ready_client()
        return await self.opening(
            lambda tuned: client.create_session(session_id=session_id, name=name, **tuned), options
        )

    async def resume_session(self, session_id: str, **options: Any) -> AgentSession:
        client = await self.ready_client()
        return await self.opening(lambda tuned: client.resume_session(session_id, **tuned), options)

    async def opening(self, open_with: Callable[[dict], Any], options: dict) -> AgentSession:
        """A session created or resumed without the extensions Copilot cannot run for it
        (EXTENSIONS_REFUSED): said so on Copilot's own runtime; with another, once it refused
        one, then for every session after it."""
        if "request_extensions" in options:
            return await open_with(options)
        try:
            return await open_with(
                {**options, "request_extensions": False} if self.no_extensions else options
            )
        except Exception as exc:
            if self.no_extensions or not extensions_refused(exc):
                raise
        logger.info("This Copilot cannot run extensions for Chief of Staff's sessions: they open "
                    "without them.")  # fmt: skip
        self.no_extensions = True
        return await open_with({**options, "request_extensions": False})

    async def fork_session(self, session_id: str, name: str) -> str:
        """A new session with this one's conversation up to its turn in progress (not that
        turn: the new session would take it for its own work, interrupted), named `name`: its
        id. Copilot's sessions.fork, from the session's log as written."""
        if not SESSION_ID.fullmatch(session_id):
            raise ValueError("Not a Copilot session id.")
        log = copilot_home() / "session-state" / session_id / "events.jsonl"
        events, _, _ = await asyncio.to_thread(read_log, log, 0)
        request = SessionsForkRequest(
            session_id=session_id, name=name, to_event_id=fork_point(events) or None
        )
        return (await (await self.ready_client()).rpc.sessions.fork(request)).session_id

    async def delete_session(self, session_id: str) -> None:
        await (await self.ready_client()).delete_session(session_id)

    async def mcp_sign_in_url(self, session: Any, server: str) -> str:
        login = await session.rpc.mcp.oauth.login(MCPOauthLoginRequest(server_name=server))
        return getattr(login, "authorization_url", None) or ""

    async def gh_works_in_sandbox(self) -> bool | None:
        return await gh_trusts_sandbox(self.command)

    async def open_elsewhere(self) -> dict[str, int]:
        return await asyncio.to_thread(sessions_in_use, copilot_home())

    async def read_history(self, session_id: str, offset: int) -> tuple[list[dict], int, float]:
        if not SESSION_ID.fullmatch(session_id):
            raise ValueError("Not a Copilot session id.")
        log = copilot_home() / "session-state" / session_id / "events.jsonl"
        return await asyncio.to_thread(read_log, log, offset)

    async def read_usage(self, session_id: str, offset: int) -> tuple[list[dict], int]:
        if not SESSION_ID.fullmatch(session_id):
            raise ValueError("Not a Copilot session id.")
        log = copilot_home() / "session-state" / session_id / "events.jsonl"
        return await asyncio.to_thread(scan_log, log, offset, USAGE_RECORD, COMPACTION_RECORD)

    def session_log(self, session_id: str) -> Path | None:
        if not SESSION_ID.fullmatch(session_id):
            return None
        return copilot_home() / "session-state" / session_id / "events.jsonl"

    def sandbox(self, policy: SandboxPolicy) -> SandboxConfig:
        # A shepherd step's policy has no credentials, and may have no outbound traffic.
        credentials = getattr(policy, "credentials", True)
        keychain = getattr(policy, "keychain", None)
        return SandboxConfig(
            enabled=True,
            # Its folders are listed in the policy: a working directory such as home is not
            # added just because the session started there.
            add_current_working_directory=False,
            allow_bypass=False,
            # Not Copilot's own grants to the owner's caches (read-write) and tools: the policy
            # names what a worker reads and writes (caches.py).
            allow_dev_tool_access=False,
            # MCP servers are what the owner or their company configured, not what an agent
            # wrote: they run as they do in Copilot, with their own sign-ins and network.
            sandbox_mcp_servers=False,
            # git and gh get the owner's GitHub credentials through Copilot's local proxy;
            # commands only ever see stand-in values.
            auth=SandboxConfigAuth(gh=credentials, git=credentials),
            user_policy=SandboxConfigUserPolicy(
                filesystem=SandboxConfigUserPolicyFilesystem(
                    readwrite_paths=list(policy.readwrite),
                    readonly_paths=list(policy.readonly),
                    denied_paths=list(policy.denied) or None,
                ),
                network=SandboxConfigUserPolicyNetwork(
                    allow_outbound=getattr(policy, "outbound", True),
                    allow_local_network=policy.local_network,
                    allowed_hosts=list(policy.allowed_hosts) or None,
                    blocked_hosts=list(getattr(policy, "blocked_hosts", ())) or None,
                ),
                seatbelt=None
                if keychain is None
                else SandboxConfigUserPolicySeatbelt(keychain_access=keychain),
            ),
        )


async def shell_init(session: Any, scripts: list[str]) -> bool:
    """Have Copilot source these scripts in the session's shell before each command it runs
    (session init scripts, set again at every open: Copilot does not keep them). False when
    the session cannot take them."""
    options = getattr(getattr(session, "rpc", None), "options", None)
    if options is None:
        return False
    sourced = [ShellInitScript(path, ShellInitScriptShell.BASH) for path in scripts]
    await options.update(SessionUpdateOptionsParams(shell=ShellOptions(init_scripts=sourced)))
    return True


async def switch_model(session: Any, model: str, **tuning: Any) -> None:
    """Switch a session's model. `reasoning_effort=None`: the model takes no thinking effort.
    Copilot keeps the effort a session had (after a resume too) and refuses such a model with
    it ("Reasoning effort 'low' is not supported for model …"), and the SDK leaves a None out:
    Copilot is sent null, which clears it."""
    client = getattr(session, "_client", None)
    if "reasoning_effort" not in tuning or tuning["reasoning_effort"] is not None or not client:
        await session.set_model(model, **tuning)
        return
    params = {"sessionId": session.session_id, "modelId": model, "reasoningEffort": None}
    if tuning.get("context_tier"):
        params["contextTier"] = tuning["context_tier"]
    await client.request("session.model.switchTo", params)


def _word(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


async def running_background(session: Any) -> int:
    """How many things a session left running in the background that hold its end of turn
    (shells attached to it, sub-agents), not a call its turn waits for nor a detached
    process; 0 when it cannot say."""
    if own := getattr(session, "running_background", None):
        try:
            return await own()
        except Exception:
            return 0
    tasks = getattr(getattr(session, "rpc", None), "tasks", None)
    if tasks is None:
        return 0
    try:
        listed = await tasks.list()
    except Exception:
        return 0
    return sum(
        _word(getattr(task, "status", None)) == "running"
        and _word(getattr(task, "execution_mode", None)) != "sync"
        and _word(getattr(task, "attachment_mode", None)) != "detached"
        for task in getattr(listed, "tasks", None) or []
    )


async def take_queued(session: Any) -> int | None:
    """Take back what Copilot queued for a session and has not started on (sent while it
    worked): how many, None when it cannot say."""
    if own := getattr(session, "take_queued", None):
        try:
            return await own()
        except Exception:
            return None
    try:
        queue = session.rpc.queue
        listed = await queue.pending_items()
        taken = 0
        for item in listed.items or []:
            taken += bool((await queue.remove_at(QueueRemoveAtRequest(id=item.id))).removed)
    except Exception:
        return None
    return taken


async def stop_background(session: Any) -> int:
    """Cancel what a session left running in the background (a command its turn went on from,
    a sub-agent): after a stop, a background command would end in a turn of its own, and until
    then the session takes no queued message. How many were cancelled."""
    if own := getattr(session, "stop_background", None):
        try:
            return await own()
        except Exception:
            return 0
    tasks = getattr(getattr(session, "rpc", None), "tasks", None)
    if tasks is None:
        return 0
    try:
        listed = await tasks.list()
    except Exception:
        return 0
    cancelled = 0
    for task in getattr(listed, "tasks", None) or []:
        status = getattr(getattr(task, "status", None), "value", getattr(task, "status", ""))
        if status != "running":
            continue
        with suppress(Exception):
            if (await tasks.cancel(TasksCancelRequest(id=task.id))).cancelled:
                cancelled += 1
    return cancelled


def copilot_backend(command: str) -> CopilotBackend:
    path = shutil.which(command) if command else None
    if not path:
        raise RuntimeError(
            f"Copilot CLI '{command}' not found. Install it and sign in, or fix the command in "
            "Settings."
        )
    # The CLI and every worker shell inherit this environment; keep our own settings out.
    env = {key: value for key, value in os.environ.items() if not key.startswith("CHIEFOFSTAFF_")}
    client = CopilotClient(connection=RuntimeConnection.for_stdio(path=path), env=env)
    return CopilotBackend(client, path, env)


def copilot_home() -> Path:
    """Where the Copilot CLI keeps its state."""
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")


def copilot_trusts(path: str) -> bool:
    """Whether the owner trusts this folder in Copilot (its trusted folders, or one above it):
    Copilot then loads the folder's own MCP servers."""
    try:
        text = (copilot_home() / "config.json").read_text()
        # Copilot writes comment lines at the top of it.
        config = json.loads(
            "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
        )
        folders = [Path(folder).resolve() for folder in config.get("trustedFolders") or []]
        target = Path(path).resolve()
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return any(target.is_relative_to(folder) for folder in folders)


def owner_tool_search() -> bool | None:
    """The owner's own choice of Copilot's tool search (`toolSearch` in its settings.json, only
    read), None when they made none. Copilot applies theirs over a session's."""
    try:
        text = (copilot_home() / "settings.json").read_text()
        settings = json.loads(
            "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
        )
    except (OSError, ValueError):
        return None
    chosen = settings.get("toolSearch") if isinstance(settings, dict) else None
    return chosen if isinstance(chosen, bool) else None


def own_processes() -> set[int]:
    """This process and its descendants (the Copilot CLI it runs)."""
    table = process_table()
    return descendants(table, os.getpid()) if table else {os.getpid()}


def copilot_tree(known: dict[int, str]) -> dict[int, str]:
    """Of these processes a Copilot CLI this app started ran (pid -> command line), those that
    still run the same command (a pid can be reused), wherever they are now (orphaned when
    their loader was killed), and everything they started (shells, MCP servers)."""
    table = process_table()
    tops = [
        pid
        for pid, command in known.items()
        if pid in table and pid != os.getpid() and table[pid][1] == command
    ]
    tree = {pid: table[pid][1] for pid in tops}
    for pid in tops:
        tree |= {child: table[child][1] for child in descendants(table, pid) if child in table}
    tree.pop(os.getpid(), None)
    return tree


# This running Chief of Staff, as its records of the Copilot processes it started name it.
INSTANCE = secrets.token_hex(8)
# A Chief of Staff service in a process's command line (its script, or `python -m`).
SERVICE = re.compile(r"(?:^|[\s/])chiefofstaff(?:\s|$)")


def start_times() -> dict[int, str]:
    """When each process started: pid -> a time to compare, not to show. A pid is reused, and
    the process that has it now started after the one recorded."""
    if sys.platform == "win32":
        return {p["pid"]: str(p["create_time"]) for p in machine.processes("create_time")}
    try:
        table = subprocess.run(
            ["ps", "-axo", "pid=,lstart="],  # noqa: S607  (a fixed command, found on PATH)
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
            # The same words whatever language the owner's terminal has: compared across runs.
            env={**os.environ, "LC_ALL": "C"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in table.splitlines():
        pid, _, started = line.strip().partition(" ")
        if pid.isdigit() and started.strip():
            found[int(pid)] = " ".join(started.split())
    return found


def data_identity(directory: Path) -> dict:
    """Which data directory this is: its real path, and the file system's own id for it. A copy
    has another id, even when it is moved to the original's path."""
    real = directory.resolve()
    try:
        info = real.stat()
    except OSError:
        return {"path": str(real)}
    return {"path": str(real), "device": info.st_dev, "inode": info.st_ino}


# What an agent's model takes as an image (the owner's attachments are these: Runtime
# .save_attachments); any other file attached reaches it as a marked line with its path.
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
# The first line of what Chief of Staff adds to a message for the files attached to it (another
# agent's adapter leaves it out of the message as shown: it is not the message's text).
ATTACHED = "Attached to this message (files to read, by path):"


def attachment_paths(attachments: list[Any] | None) -> list[tuple[str, str]]:
    """Each attachment's path and name (a session's send: {"type": "file", "path", ...})."""
    found = []
    for attachment in attachments or []:
        if isinstance(attachment, dict) and isinstance(attachment.get("path"), str):
            path = attachment["path"]
            found.append((path, str(attachment.get("displayName") or Path(path).name)))
    return found


def image_type(path: str) -> str:
    """The image type an agent's model takes this file as ("" for another file)."""
    return IMAGE_TYPES.get(Path(path).suffix.lower(), "")


def attached_text(files: list[tuple[str, str]], unreadable: bool = False) -> str:
    """The marked lines for files attached to a message, each by path (and its name when that
    differs), every tag escaped."""
    from .envelope import inert

    lines = [ATTACHED]
    for path, name in files:
        named = f" ({inert(name)})" if name and name != Path(path).name else ""
        lines.append(f"- {inert(path)}{named}")
    if unreadable:
        lines.append("(Some could not be read as images: open them by path.)")
    return "\n".join(lines)


RESUMED = re.compile(r"(?:^|\s)(?:--resume[= ]|-r |resume )([0-9a-f][0-9a-f-]{7,63})(?:\s|$)")


def resumed_elsewhere(
    program: str, ours: set[int], table: dict[int, tuple[int, str]] | None = None
) -> dict[str, int]:
    """Sessions of an agent's CLI the owner resumed in a terminal (`claude --resume <id>`,
    `codex resume <id>`): session id -> pid. Never one of Chief of Staff's own processes."""
    found: dict[str, int] = {}
    for pid, (_, command) in (process_table() if table is None else table).items():
        words = command.split()
        if pid in ours or not words:
            continue
        named = [Path(word).name for word in words[:3]]
        if program not in named and f"{program}.js" not in named:
            continue
        if match := RESUMED.search(command):
            found.setdefault(match.group(1), pid)
    return found


def isolated(module: str, *arguments: str) -> list[str]:
    """How Chief of Staff runs a module of its own as a program (another agent's supervisor,
    agentrun; a check's MCP server): this Python, isolated (-I), so that the folder it starts in
    (a repository) is not on its import path, nor PYTHONPATH, nor the user's site folder."""
    return [sys.executable, "-I", "-m", module, *arguments]


def hashed_command(command: str) -> str:
    """A command line as another agent's process records keep it: its SHA-256, never its words
    (what a worker's commands were given, an MCP server's)."""
    return "sha256:" + hashlib.sha256(command.encode(errors="replace")).hexdigest()


def same_command(command: str, recorded: Any) -> bool:
    """Whether a process's command line is the one recorded (as is, or as hashed_command)."""
    return isinstance(recorded, str) and recorded in {command, hashed_command(command)}


def copilot_records(
    processes: dict[int, str], directory: Path, *, hashed: bool = False
) -> dict[str, dict]:
    """What is kept of the processes this instance's Copilot CLI runs (pid -> command line), so
    that a later start can tell they are still those, and its own to stop (copilot_orphans):
    this instance, its data directory, when each started, its command and its parents.
    `hashed`: each command kept as its hash (another agent's records)."""
    if not processes:
        return {}
    starts, table = start_times(), process_table()
    me = os.getpid()
    owner = {"pid": me, "started": starts.get(me, "")}
    data = data_identity(directory)
    records = {}
    for pid, command in processes.items():
        parents, at = [], table.get(pid, (0, ""))[0]
        while at > 1 and at not in parents and len(parents) < 20:
            parents.append(at)
            if at == me:
                break
            at = table.get(at, (0, ""))[0]
        records[str(pid)] = {
            "command": hashed_command(command) if hashed else command,
            "started": starts.get(pid, ""),
            "parents": parents,
            "instance": INSTANCE,
            "owner": owner,
            "data_dir": data,
        }
    return records


def started_elsewhere(
    pid: int, record: dict, starts: dict[int, str], table: dict[int, tuple[int, str]]
) -> bool:
    """Whether another running Chief of Staff owns a recorded process: the one that started it
    still runs, or its parents now lead to one (never this one, or what this one runs under)."""
    me = os.getpid()
    owner = record.get("owner")
    owner = owner if isinstance(owner, dict) else {}
    owner_pid, owner_started = owner.get("pid"), owner.get("started")
    # The one that started it still runs (a start it cannot compare: as good as the same one).
    if (
        isinstance(owner_pid, int)
        and owner_pid != me
        and owner_pid in starts
        and (not owner_started or starts[owner_pid] == owner_started)
    ):
        return True
    mine, at = set(), me
    while at > 1 and at not in mine:
        mine.add(at)
        at = table.get(at, (0, ""))[0]
    seen, at = set(), table.get(pid, (0, ""))[0]
    while at > 1 and at not in mine and at not in seen:
        seen.add(at)
        if SERVICE.search(table.get(at, (0, ""))[1]):
            return True
        at = table.get(at, (0, ""))[0]
    return False


def copilot_orphans(records: Any, directory: Path) -> dict[int, str]:
    """Of the processes recorded (copilot_records), those this instance may stop: pid ->
    command line. Only this data directory's (an instance on a copy of it never stops the
    original's, which its copied records name), each only while it is the process recorded (the
    same start and command: a pid is reused), and only when no other running Chief of Staff
    owns it."""
    if not isinstance(records, dict) or not records:
        return {}
    here = data_identity(directory)
    starts, table = start_times(), process_table()
    found = {}
    for key, record in records.items():
        if not (isinstance(record, dict) and str(key).isdigit()):
            continue  # Recorded by an earlier version: not told from another instance's.
        pid = int(key)
        if (
            record.get("data_dir") == here
            and pid != os.getpid()
            and record.get("started")
            and starts.get(pid) == record["started"]
            and pid in table
            and same_command(table[pid][1], record.get("command"))
            and not started_elsewhere(pid, record, starts, table)
        ):
            found[pid] = table[pid][1]
    return found


def copilot_ours(records: Any, directory: Path) -> set[int]:
    """The recorded processes this running instance's Copilot CLI has (its last connection's),
    with this data directory: never another program, such as the owner's terminal."""
    if not isinstance(records, dict):
        return set()
    here = data_identity(directory)
    return {
        int(key)
        for key, record in records.items()
        if str(key).isdigit()
        and isinstance(record, dict)
        and record.get("instance") == INSTANCE
        and record.get("data_dir") == here
    }


def sessions_in_use(home: Path) -> dict[str, int]:
    """Sessions that another running program has open.

    The Copilot CLI keeps an ``inuse.<pid>.lock`` file in the folder of each session it has open;
    a lock whose process is gone is stale. Sessions this process's own CLI has open do not count.
    """
    locks = []
    for lock in (home / "session-state").glob("*/inuse.*.lock"):
        pid = lock.name.split(".")[1]
        if pid.isdigit() and running(int(pid)):
            locks.append((lock.parent.name, int(pid)))
    if not locks:
        return {}
    own = own_processes()
    return {session_id: pid for session_id, pid in locks if pid not in own}


SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The latest part of a session's log that is read when nothing was read before.
LOG_TAIL = 12_000_000
LOG_CHUNK = 8_000_000


USAGE_RECORD = b'"session.usage_record"'
COMPACTION_RECORD = b'"session.compaction_complete"'


def scan_log(log: Path, offset: int, *markers: bytes) -> tuple[list[dict], int]:
    """The events in a log after `offset` whose line holds one of `markers`, and where the
    complete lines end. Lines are only parsed when they match, so a long log is quick to go
    through."""
    try:
        size = log.stat().st_size
    except OSError:
        return [], offset
    if offset > size:
        offset = 0  # The log was rewritten.
    found = []
    position = offset
    with log.open("rb") as file:
        file.seek(offset)
        rest = b""
        while chunk := file.read(LOG_CHUNK):
            lines = (rest + chunk).split(b"\n")
            rest = lines.pop()  # Unfinished: read with the next chunk, or next time.
            for line in lines:
                position += len(line) + 1
                if any(marker in line for marker in markers):
                    with suppress(ValueError):
                        if isinstance(event := json.loads(line), dict):
                            found.append(event)
    return found, position


# What a session's log shows at the end of a turn (each of its model calls ends with
# assistant.turn_end; the turn, with the checkpoint of its usage).
LOGGED_TURN_ENDS = {"session.usage_checkpoint", "abort", "session.shutdown", "session.error"}


def fork_point(events: list[dict]) -> str:
    """The id of the event a fork of a session stops before: the message that began its turn
    in progress (messages steered into that turn come after it); "" when none is in progress.
    Its own events only, not its sub-agents'. A log that shows no turn's end at all (another
    format): its latest message."""
    cut, ended, latest = "", False, ""
    for event in events:
        if event.get("agentId") or (event.get("data") or {}).get("parentToolCallId"):
            continue
        kind = event.get("type")
        if kind in LOGGED_TURN_ENDS:
            cut, ended = "", True
        elif kind == "user.message":
            latest = str(event.get("id") or "")
            cut = cut or latest
    return cut if ended or cut == latest else latest


def read_log(log: Path, offset: int) -> tuple[list[dict], int, float]:
    """Events appended to a Copilot session log (JSON lines) since `offset`.

    The CLI appends one event per line while it runs, so reading is safe while a terminal has
    the session open; a line still being written is left for the next read.
    """
    try:
        stat = log.stat()
    except OSError:
        return [], offset, 0.0
    if offset > stat.st_size:
        offset = 0  # The log was rewritten.
    start = offset or max(0, stat.st_size - LOG_TAIL)
    # A first look reads its whole tail, the latest events; later reads catch up in chunks.
    size = min(stat.st_size - start, LOG_CHUNK) if offset else stat.st_size - start
    with log.open("rb") as file:
        file.seek(start)
        data = file.read(size)
    end = data.rfind(b"\n") + 1
    lines = data[:end].split(b"\n")
    if start and not offset:
        lines = lines[1:]  # Started mid-line.
    events = []
    for line in lines:
        with suppress(ValueError):
            if line.strip() and isinstance(event := json.loads(line), dict):
                events.append(event)
    return events, start + end, stat.st_mtime


def sandbox_supported() -> bool:
    """Whether this machine can run Copilot's command sandbox."""
    if sys.platform == "darwin":
        return bool(shutil.which("sandbox-exec"))
    return sys.platform.startswith("linux") and bool(shutil.which("bwrap"))


async def gh_trusts_sandbox(command: str = "copilot") -> bool | None:
    """Whether gh can reach GitHub from inside the sandbox (macOS needs Copilot's CA trusted)."""
    if sys.platform != "darwin" or not shutil.which(command):
        return None
    process = await asyncio.create_subprocess_exec(
        command,
        "sandbox",
        "ca",
        "status",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(process.wait(), 15) == 0
    except TimeoutError:
        process.kill()
        return None
    except asyncio.CancelledError:
        with suppress(ProcessLookupError):
            process.kill()
        raise


def older(version: str, than: str) -> bool:
    def parts(text: str) -> tuple[int, ...]:
        return tuple(int(part) for part in text.split("-")[0].split(".") if part.isdigit())

    try:
        mine = parts(version)
        return bool(mine) and mine < parts(than)
    except ValueError:
        return False
