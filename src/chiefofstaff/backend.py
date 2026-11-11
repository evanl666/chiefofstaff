"""The agent backend that runs Chief of Staff's sessions.

Chief of Staff uses a small part of what an agent SDK offers. This module names that part, so the
rest of the code depends on these Protocols rather than on the Copilot SDK. GitHub Copilot is the
backend that ships; the scripted backend (`scripted.py`) runs the demo mode, and
tests/test_backend_contract.py checks that both behave the way the runtime relies on.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
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
    TasksCancelRequest,
)


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


SendMode = Literal["enqueue", "immediate"]


class AgentSession(Protocol):
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
        offset to read from next time."""
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


COPILOT = Capabilities(
    name="GitHub Copilot",
    steering=True,
    resume=True,
    permission_callbacks=True,
    mcp_sign_in=True,
    sandbox=True,
)
# The Copilot CLI versions this release was tested with (live end-to-end and contract tests).
TESTED_CLI = "1.0.107"
OLDEST_CLI = "1.0.100"


# What Chief of Staff calls itself to Copilot (work-buddy: its earlier name), which the CLI keeps
# with each session as the client that made it and the one that last resumed it.
OWN_CLIENTS = frozenset({"chiefofstaff", "work-buddy"})


def made_by_us(item: dict) -> bool:
    """A session Chief of Staff made, under any data directory, that no other client (a terminal)
    has resumed since."""
    made = item.get("clientName") or ""
    return made in OWN_CLIENTS and (item.get("lastClientName") or made) in OWN_CLIENTS


class CopilotBackend:
    """GitHub Copilot through its Python SDK: one CLI process runs every session."""

    capabilities = COPILOT

    def __init__(self, client: CopilotClient, command: str = "copilot"):
        self.client = client
        self.command = command
        # The CLI's process and those it started (the npm loader runs Copilot's own program,
        # which outlives the loader when only that is killed), as last seen: pid -> command.
        self.started: dict[int, str] = {}

    async def start(self) -> None:
        await self.client.start()

    def cli_process(self) -> subprocess.Popen | None:
        return getattr(self.client, "_cli_process", None)

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
        return (await self.client.get_status()).version

    async def get_auth_status(self) -> Any:
        return await self.client.get_auth_status()

    async def list_models(self) -> list[Any]:
        return await self.client.list_models()

    async def list_sessions(self) -> list[Any]:
        """Every session, each marked `made_by_us` (see made_by_us), with the client that made
        it (`client_name`) and the one that last ran a turn in it (`last_client_name`, "" when
        none is recorded); `clients_listed` says these were read. The SDK's list leaves out
        which client made a session, the CLI's answer has it. Unmarked when that cannot be read."""
        try:
            answer = await self.client._client.request("session.list", {})  # type: ignore[union-attr]
            listed = []
            for item in answer.get("sessions", []):
                meta = SessionMetadata.from_dict(item)
                meta.made_by_us = made_by_us(item)  # type: ignore[attr-defined]
                vars(meta).update(
                    client_name=str(item.get("clientName") or ""),
                    last_client_name=str(item.get("lastClientName") or ""),
                    clients_listed=True,
                )
                listed.append(meta)
            return listed
        except Exception:
            return await self.client.list_sessions()

    async def get_session_metadata(self, session_id: str) -> Any:
        return await self.client.get_session_metadata(session_id)

    async def create_session(self, *, session_id: str, name: str, **options: Any) -> AgentSession:
        return await self.client.create_session(session_id=session_id, name=name, **options)

    async def resume_session(self, session_id: str, **options: Any) -> AgentSession:
        return await self.client.resume_session(session_id, **options)

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
        return await asyncio.to_thread(scan_log, log, offset, USAGE_RECORD)

    def session_log(self, session_id: str) -> Path | None:
        if not SESSION_ID.fullmatch(session_id):
            return None
        return copilot_home() / "session-state" / session_id / "events.jsonl"

    def sandbox(self, policy: SandboxPolicy) -> SandboxConfig:
        return SandboxConfig(
            enabled=True,
            # Its folders are listed in the policy: a working directory such as home is not
            # added just because the session started there.
            add_current_working_directory=False,
            allow_bypass=False,
            allow_dev_tool_access=True,
            # MCP servers are what the owner or their company configured, not what an agent
            # wrote: they run as they do in Copilot, with their own sign-ins and network.
            sandbox_mcp_servers=False,
            # git and gh get the owner's GitHub credentials through Copilot's local proxy;
            # commands only ever see stand-in values.
            auth=SandboxConfigAuth(gh=True, git=True),
            user_policy=SandboxConfigUserPolicy(
                filesystem=SandboxConfigUserPolicyFilesystem(
                    readwrite_paths=list(policy.readwrite),
                    readonly_paths=list(policy.readonly),
                    denied_paths=list(policy.denied) or None,
                ),
                network=SandboxConfigUserPolicyNetwork(
                    allow_outbound=True,
                    allow_local_network=policy.local_network,
                    allowed_hosts=list(policy.allowed_hosts) or None,
                ),
            ),
        )


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
    return CopilotBackend(client, path)


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


def process_table() -> dict[int, tuple[int, str]]:
    """Every process: pid -> (parent pid, command line)."""
    try:
        table = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],  # noqa: S607  (a fixed command, found on PATH)
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in table.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            found[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return found


def descendants(table: dict[int, tuple[int, str]], root: int) -> set[int]:
    """`root` and every process it started, and they started (while `root` runs)."""
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    found, todo = set(), [root]
    while todo:
        pid = todo.pop()
        if pid not in found:
            found.add(pid)
            todo += children.get(pid, [])
    return found


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


def end_processes(tree: dict[int, str], wait: float = 3.0):
    """Stop a copilot_tree: asked to end, then killed if they do not. A process whose command
    changed since is another one, left alone."""

    def same(table: dict[int, tuple[int, str]]) -> list[int]:
        return [pid for pid, command in tree.items() if pid in table and table[pid][1] == command]

    for signum in (signal.SIGTERM, signal.SIGKILL):
        left = same(process_table())
        for pid in left:
            with suppress(OSError):
                os.kill(pid, signum)
        deadline = time.monotonic() + wait
        while left and time.monotonic() < deadline:
            time.sleep(0.05)
            left = [pid for pid in left if running(pid)]
        if not left:
            return


def running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Someone else's process, but it exists.
    return True


# This running Chief of Staff, as its records of the Copilot processes it started name it.
INSTANCE = secrets.token_hex(8)
# A Chief of Staff service in a process's command line (its script, or `python -m`).
SERVICE = re.compile(r"(?:^|[\s/])chiefofstaff(?:\s|$)")


def start_times() -> dict[int, str]:
    """When each process started: pid -> a time to compare, not to show. A pid is reused, and
    the process that has it now started after the one recorded."""
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


def copilot_records(processes: dict[int, str], directory: Path) -> dict[str, dict]:
    """What is kept of the processes this instance's Copilot CLI runs (pid -> command line), so
    that a later start can tell they are still those, and its own to stop (copilot_orphans):
    this instance, its data directory, when each started, its command and its parents."""
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
            "command": command,
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
            and table[pid][1] == record.get("command")
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


def scan_log(log: Path, offset: int, marker: bytes) -> tuple[list[dict], int]:
    """The events in a log after `offset` whose line holds `marker`, and where the complete
    lines end. Lines are only parsed when they match, so a long log is quick to go through."""
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
                if marker in line:
                    with suppress(ValueError):
                        if isinstance(event := json.loads(line), dict):
                            found.append(event)
    return found, position


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


def older(version: str, than: str) -> bool:
    def parts(text: str) -> tuple[int, ...]:
        return tuple(int(part) for part in text.split("-")[0].split(".") if part.isdigit())

    try:
        mine = parts(version)
        return bool(mine) and mine < parts(than)
    except ValueError:
        return False
