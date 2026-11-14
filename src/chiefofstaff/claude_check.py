"""What Chief of Staff checks of a Claude Code version before its workers run, once per version
(ClaudeAgent.check, kept by backend_claude.check_key), on this machine and without a model call
off it: a stand-in for Anthropic's Messages API on 127.0.0.1 that the CLI talks to as it would to
Anthropic's, whose turns ask for each kind of action a worker takes, and Codex's stand-in MCP
server (`python -m chiefofstaff.codex_check --mcp FOLDER`), whose one tool writes a file.

The check never uses the owner's configuration folder, sign-in or provider: the CLI runs with an
allowlisted environment (HOME, PATH, USER, LOGNAME, SHELL, TMPDIR, LANG, and the Agent SDK's own
variables), a throwaway CLAUDE_CONFIG_DIR, a made-up API key, the stand-in as its base URL,
non-essential traffic off, and every proxy variable pointing at a listener of the check's own,
which records and drops any connection: one fails the check, saying where it went.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

if TYPE_CHECKING:
    from .backend_claude import ClaudeAgent, ClaudeWorker

# Raised whenever what the check does changes: every version is checked again then.
REVISION = 1
# The check's own tool of Chief of Staff's, and the stand-in MCP server's.
TOOL = "chiefofstaff_check"
SERVER = "chiefofstaff_check"
SERVER_TOOL = "write_marker"
# What each action would leave in the check's folder, had it run.
COMMAND_MARK = "command.marker"
SERVER_MARK = "server.marker"
RAISING_MARK = "raising.marker"
ALLOWED_FILE, REFUSED_FILE = "allowed.txt", "refused.txt"
INTERRUPT_PID = "interrupt.pid"
# The steps of the check's turns, each started by a message of Chief of Staff's.
STEP = "Chief of Staff's check, step "
STEER = "A note for the step in progress: reply with just 'steered'."
# The environment the check's CLI gets of Chief of Staff's (the rest is set by the check).
KEPT = (
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "LANG",
    # The Agent SDK's own.
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_CODE_SDK_READS_SESSION_STATE",
    "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS",
    "PWD",
)
PROXIES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")
# The fields of Claude's messages Chief of Staff reads (backend_claude): a version without one
# of them runs no worker, the check saying which.
CLAUDE_USES = {
    "initialize": (
        "hooks_applied",
        "hook_on_failure_supported",
        "claude_code_version",
        "models",
    ),
    "system/init": ("permissionMode", "mcp_servers", "session_id", "claude_code_version"),
    "user (echo)": ("uuid", "message"),
    "user (tool result)": ("tool_use_result", "tool_result_meta"),
    "assistant": ("message", "parent_tool_use_id"),
    "stream_event message_start": ("message.id", "message.usage"),
    "stream_event message_delta": ("usage", "delta.stop_reason"),
    "system/task_started": ("task_id", "tool_use_id", "is_backgrounded"),
    "system/task_notification": ("task_id", "status", "summary"),
    "result": ("modelUsage", "usage", "is_error"),
}
CHECK_SECONDS = 180.0


class Messages(ThreadingHTTPServer):
    """The stand-in Messages API, on a free port of 127.0.0.1, until closed: each request of the
    check's session is answered by its script (`answer`), others (a title, a summary) with
    words; every request is logged."""

    daemon_threads = True

    def __init__(self, work: Path):
        self.work = work
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.number = 0
        super().__init__(("127.0.0.1", 0), Answer)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def turn_requests(self) -> int:
        with self.lock:
            return sum(1 for request in self.requests if request["session"])

    def close(self):
        self.shutdown()
        self.server_close()


def texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    found = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            found.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_result":
            found += texts(block.get("content"))
    return found


def position(body: dict) -> tuple[str, int, str]:
    """Where the check's turn is: its latest step, how many of that step's calls were answered,
    and the text that came after the model's last words (a call's result, and a note steered
    into the turn with it: Claude gives that as a message of its own, 2.1.322)."""
    step, answered, after = "", 0, ""
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            after = ""
            continue
        content = message.get("content")
        joined = "\n".join(texts(content))
        blocks = content if isinstance(content, list) else []
        results = [
            block
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        if message.get("role") == "user" and STEP in joined and not results:
            step = joined.split(STEP, 1)[1].split()[0].strip(".:")
            answered, after = 0, ""
            continue
        answered += len(results)
        after += "\n" + joined
    return step, answered, after


def call(name: str, arguments: dict) -> dict:
    return {
        "type": "tool_use",
        "id": f"toolu_check_{uuid.uuid4().hex[:12]}",
        "name": name,
        "input": arguments,
    }


def bash(command: str, **more: Any) -> dict:
    return call("Bash", {"command": command, "description": "Chief of Staff's check", **more})


def script(work: Path, step: str, answered: int, after: str) -> tuple[list[dict], str]:
    """The stand-in model's answer: the step's next call, then its words."""
    steps = {
        "actions": [
            call(f"mcp__chief__{TOOL}", {"text": "check"}),
            bash(f"touch {work / COMMAND_MARK}"),
            call(f"mcp__{SERVER}__{SERVER_TOOL}", {}),
            call("Write", {"file_path": str(work / ALLOWED_FILE), "content": "x\n"}),
            call("Write", {"file_path": str(work / REFUSED_FILE), "content": "x\n"}),
            call(
                "Edit",
                {"file_path": str(work / ALLOWED_FILE), "old_string": "x", "new_string": "y"},
            ),
            bash(f"touch {work / RAISING_MARK}"),
        ],
        "steer": [bash("sleep 3")],
        "background": [bash("sleep 1; echo done", run_in_background=True)],
        "interrupt": [bash(f"echo $$ > {work / INTERRUPT_PID}; exec sleep 40")],
        "resumed": [],
    }
    planned = steps.get(step, [])
    if answered < len(planned):
        return [planned[answered]], "tool_use"
    if step == "steer":
        # Steered within the turn: the note came with the command's result.
        word = "steered" if STEER in after else "not steered"
        return [{"type": "text", "text": word}], "end_turn"
    return [{"type": "text", "text": f"{step or 'nothing'}: done"}], "end_turn"


def usage_of(number: int) -> dict:
    """Each response's usage, different each time, so what is counted can be told apart."""
    return {
        "input_tokens": 100 + number,
        "output_tokens": 7 + number % 5,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 11,
    }


def events(number: int, model: str, blocks: list[dict], stop: str) -> list[dict]:
    usage = usage_of(number)
    found: list[dict] = [
        {
            "type": "message_start",
            "message": {
                "id": f"msg_check_{number}",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {**usage, "output_tokens": 1},
            },
        }
    ]
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            found.append(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                }
            )
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            found.append({"type": "content_block_start", "index": index, "content_block": start})
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        found.append({"type": "content_block_delta", "index": index, "delta": delta})
        found.append({"type": "content_block_stop", "index": index})
    found.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": usage["output_tokens"]},
        }
    )
    found.append({"type": "message_stop"})
    return found


class Answer(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None

    def reply(self, kind: str, data: bytes):
        self.send_response(200)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        server = self.server
        if not isinstance(server, Messages):
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        except ValueError:
            body = {}
        if "count_tokens" in self.path:
            self.reply("application/json", json.dumps({"input_tokens": 10}).encode())
            return
        tools = [str(t.get("name")) for t in body.get("tools") or [] if isinstance(t, dict)]
        session = "Bash" in tools  # The worker's own loop (not a title, a summary).
        step, answered, after = position(body)
        with server.lock:
            server.number += 1
            number = server.number
            server.requests.append(
                {"number": number, "session": session, "tools": tools, "step": step}
            )
        if session:
            blocks, stop = script(server.work, step, answered, after)
        else:
            blocks, stop = [{"type": "text", "text": "check"}], "end_turn"
        model = str(body.get("model") or "chiefofstaff-check")
        if body.get("stream"):
            data = "".join(
                f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
                for e in events(number, model, blocks, stop)
            ).encode()
            self.reply("text/event-stream", data)
            return
        message = {
            "id": f"msg_check_{number}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": blocks,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": usage_of(number),
        }
        self.reply("application/json", json.dumps(message).encode())


class Catcher:
    """A listener on 127.0.0.1 that every proxy variable of the check's CLI points at: it
    records and drops each connection (a request meant for somewhere off this machine)."""

    def __init__(self):
        self.seen: list[str] = []
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(16)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.socket.getsockname()[1]}"

    def serve(self):
        while True:
            try:
                connection, _ = self.socket.accept()
            except OSError:
                return
            try:
                connection.settimeout(2)
                first = connection.recv(4096).split(b"\r\n", 1)[0]
                self.seen.append(first.decode(errors="replace")[:200] or "a connection")
            except OSError:
                self.seen.append("a connection")
            finally:
                connection.close()

    def close(self):
        with_suppressed(self.socket.close)


def with_suppressed(action: Any):
    try:
        action()
    except OSError:
        return


def environment(folder: Path, messages: Messages, catcher: Catcher) -> Path:
    """The check's environment file (agentrun --environment): what it keeps of Chief of Staff's
    environment, and what it sets."""
    config = folder / "config"
    config.mkdir(parents=True, exist_ok=True)
    chosen: dict[str, str] = {
        "CLAUDE_CONFIG_DIR": str(config),
        "ANTHROPIC_API_KEY": "sk-ant-api03-chiefofstaff-check-not-a-key",
        "ANTHROPIC_BASE_URL": messages.url,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name in PROXIES:
        chosen[name] = chosen[name.lower()] = catcher.url
    file = folder / "environment.json"
    file.write_text(json.dumps({"keep": list(KEPT), "set": chosen}))
    return file


class Facts:
    """What the check saw."""

    def __init__(self):
        self.asked: list[str] = []
        self.tool_calls = 0
        self.raised = False
        self.said: list[str] = []
        self.errors: list[str] = []
        self.seen: dict[str, set[str]] = {}
        self.results: list[dict] = []
        self.responses: dict[str, dict] = {}
        self.gate_seen = False
        self.wakeup_requests = -1
        self.interrupt_pid = 0
        self.interrupt_ended = False
        self.restored: dict | None = None
        self.resumed_ok = False
        self.counted: dict[str, int] = {}
        self.usage_matches = True
        self.last_response = ""
        self.background_code: int | None = None
        self.tasks_seen = False
        self.offered: list[str] = []

    def saw(self, kind: str, message: dict, *paths: str):
        """Which of the fields Chief of Staff reads this message had."""
        found = self.seen.setdefault(kind, set())
        for path in paths:
            value: Any = message
            for part in path.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            if value is not None or (isinstance(message, dict) and path in message):
                found.add(path)


def observe(facts: Facts, message: dict):
    """Each of Claude's messages, as the check's worker reads it."""
    kind, subtype = message.get("type"), message.get("subtype")
    if kind == "system" and subtype == "init":
        facts.saw("system/init", message, *CLAUDE_USES["system/init"])
    elif kind == "system" and subtype == "task_started":
        facts.tasks_seen = True
        facts.saw("system/task_started", message, *CLAUDE_USES["system/task_started"])
    elif kind == "system" and subtype == "task_notification":
        facts.saw("system/task_notification", message, *CLAUDE_USES["system/task_notification"])
        summary = str(message.get("summary") or "")
        if "exit code" in summary:
            found = summary.rsplit("exit code", 1)[1].strip(" )")
            facts.background_code = int(found) if found.lstrip("-").isdigit() else None
    elif kind == "user":
        content = (message.get("message") or {}).get("content")
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            facts.saw("user (tool result)", message, *CLAUDE_USES["user (tool result)"])
        elif message.get("isReplay"):
            facts.saw("user (echo)", message, *CLAUDE_USES["user (echo)"])
    elif kind == "assistant":
        facts.saw("assistant", message, *CLAUDE_USES["assistant"])
    elif kind == "stream_event":
        streamed = message.get("event") or {}
        if streamed.get("type") == "message_start":
            facts.saw("stream_event message_start", streamed, "message.id", "message.usage")
            response = streamed.get("message") or {}
            facts.last_response = str(response.get("id"))
            facts.responses.setdefault(facts.last_response, {})["start"] = response.get("usage")
        elif streamed.get("type") == "message_delta":
            facts.saw("stream_event message_delta", streamed, "usage", "delta.stop_reason")
            facts.responses.setdefault(facts.last_response, {})["delta"] = streamed.get("usage")
    elif kind == "result":
        facts.saw("result", message, *CLAUDE_USES["result"])
        facts.results.append(message)
        # What its responses said as they came, summed, must be what the turn's result says.
        from .backend_claude import added, counts

        counted = added(
            counts({}),
            *(
                counts({**(found.get("start") or {}), **(found.get("delta") or {})})
                for key, found in facts.responses.items()
                if key not in facts.counted
            ),
        )
        facts.counted |= dict.fromkeys(facts.responses, 1)
        if counted != counts(message.get("usage")):
            facts.usage_matches = False


async def check(agent: ClaudeAgent, folder: Path) -> dict:
    """What Chief of Staff's checks rely on, on this version, without a model call off this
    machine: a worker that asks first (its process as each worker's is, but in the check's
    environment), whose turns call one of Chief of Staff's tools, run a command, call an MCP
    tool, write two files and edit one, and run a command through a hook that fails (each must
    reach Chief of Staff, which refuses all but one write; nothing refused may happen); steer a
    note into a turn; leave a command running whose end must reach the gate before any model
    request (blocked, it costs none); are stopped during a command, which must end; and resume,
    whose restored totals its next turn must count on. `ran`: False when the check itself could
    not run (then it is not remembered)."""
    from .backend_claude import ClaudeAgent, ClaudeWorker, write_launcher

    facts = Facts()
    work = folder / "work"
    messages = catcher = None
    checker = ClaudeAgent(agent.command, folder)
    checker.cli_version = agent.cli_version
    checker.running = True
    try:
        await asyncio.to_thread(shutil.rmtree, folder, True)
        await asyncio.to_thread(work.mkdir, parents=True)
        messages = await asyncio.to_thread(Messages, work)
        catcher = await asyncio.to_thread(Catcher)
        chosen = await asyncio.to_thread(environment, folder, messages, catcher)
        launcher = await asyncio.to_thread(write_launcher, folder, agent.command, chosen)

        async def admit(session: Any, woken: bool = False) -> str:
            if woken:
                facts.gate_seen = True
                facts.wakeup_requests = messages.turn_requests() if messages else -1
                return "Chief of Staff's check: not admitted."
            return ""

        checker.admit = admit
        session_id = str(uuid.uuid4())
        options = check_options(work, facts)
        worker = ClaudeWorker(
            checker, "check", session_id, options, resume=False, launcher=launcher, checking=True
        )
        await asyncio.wait_for(checking(checker, worker, work, facts, messages), CHECK_SECONDS)
        happened = [
            name
            for name in (COMMAND_MARK, SERVER_MARK, RAISING_MARK, REFUSED_FILE)
            if (work / name).exists()
        ]
        if (work / ALLOWED_FILE).exists() and "y" in (work / ALLOWED_FILE).read_text():
            happened.append(f"{ALLOWED_FILE} edited")
        offered = messages.requests[0]["tools"] if messages.requests else []
        facts.offered = sorted({*offered})
        reached = list(catcher.seen)
    except Exception as exc:  # Whatever stops the check, it says why.
        why = (
            f"Chief of Staff's check of Claude Code {agent.cli_version} could not run ("
            + ((str(exc) or type(exc).__name__)[:200])
        )
        from .backend_claude import ask_first_tested

        return {
            "ran": False,
            "ask_first": f"{why}): workers that ask first do not run on it until it does.",
            "acting": ""
            if ask_first_tested(agent.cli_version)
            else f"{why}): no worker runs on this newer version until it does.",
        }
    finally:
        await checker.stop()
        if messages:
            await asyncio.to_thread(messages.close)
        if catcher:
            await asyncio.to_thread(catcher.close)
        await asyncio.to_thread(shutil.rmtree, folder, True)
    return check_verdict(agent.cli_version, facts, happened, reached)


def check_options(work: Path, facts: Facts) -> dict:
    """The check's worker: one that asks first, with Chief of Staff's own tool and the stand-in
    MCP server; Chief of Staff refuses each action but the first write and the check's commands,
    and fails (raises) on the one that must block."""

    def tool(invocation: Any) -> str:
        facts.tool_calls += 1
        return "checked"

    def decide(request: Any, context: dict) -> Any:
        kind = getattr(request, "kind", "")
        if kind == "shell":
            command = getattr(request, "full_command_text", "")
            if RAISING_MARK in command:
                facts.raised = True
                raise RuntimeError("Chief of Staff's check: a hook that fails.")
            facts.asked.append("shell")
            if COMMAND_MARK in command:
                return PermissionDecisionReject(feedback="Chief of Staff's check.")
            return PermissionDecisionApproveOnce()
        if kind == "write":
            name = Path(getattr(request, "resolved_path", "")).name
            asked = f"{getattr(request, 'tool_name', 'write')} {name}"
            facts.asked.append(asked)
            if asked == f"Write {ALLOWED_FILE}":
                return PermissionDecisionApproveOnce()  # The first write; its edit is refused.
            return PermissionDecisionReject(feedback="Chief of Staff's check.")
        facts.asked.append(kind)
        if kind == "custom-tool":
            return PermissionDecisionApproveOnce()
        return PermissionDecisionReject(feedback="Chief of Staff's check.")

    return {
        "system_message": {"content": "Chief of Staff's check. Do as asked."},
        "tools": [
            SimpleNamespace(
                name=TOOL,
                description="Chief of Staff's check.",
                parameters={"type": "object", "properties": {"text": {"type": "string"}}},
                handler=tool,
            )
        ],
        "working_directory": str(work),
        "folders": [str(work)],
        "ask_first": True,
        "on_permission_request": decide,
        "on_message": lambda message: observe(facts, message),
        "on_accounting": lambda kind, data: None,
        "checking_servers": {
            SERVER: {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "chiefofstaff.codex_check", "--mcp", str(work)],
            }
        },
    }


async def checking(
    checker: ClaudeAgent, worker: ClaudeWorker, work: Path, facts: Facts, messages: Messages
):
    """The check's turns, one after the other, on one worker's process, then on another that
    resumes it."""
    checker.workers["check"] = worker
    await worker.open()
    facts.saw("initialize", worker.initialized, *CLAUDE_USES["initialize"])
    session = worker.attach(worker.options)
    turn = Turn(session, facts)

    await turn.send(f"{STEP}actions")
    await turn.ended()
    await turn.send(f"{STEP}steer")
    await until(lambda: bool(worker.running_calls), 30)
    await asyncio.sleep(0.5)
    await session.send(STEER, mode="immediate")
    await turn.ended()
    await turn.send(f"{STEP}background")
    await turn.ended()
    # Its command ends, and wakes it: the gate is asked first (and refuses).
    await until(lambda: facts.gate_seen and not worker.background, 30)
    await asyncio.sleep(1.5)
    after = messages.turn_requests()
    facts.wakeup_requests = after - facts.wakeup_requests if facts.wakeup_requests >= 0 else -1
    await turn.send(f"{STEP}interrupt")
    await until(lambda: (work / INTERRUPT_PID).exists() and bool(worker.running_calls), 30)
    await asyncio.sleep(0.5)
    facts.interrupt_pid = int((work / INTERRUPT_PID).read_text().strip() or 0)
    await session.abort()
    await turn.ended()
    await asyncio.sleep(1.0)
    facts.interrupt_ended = not running(facts.interrupt_pid)
    await session.disconnect()
    await worker.stop()

    resumed = type(worker)(
        checker,
        "check",
        worker.session_id,
        worker.options,
        resume=True,
        launcher=worker.launcher,
        checking=True,
    )
    checker.workers["check"] = resumed
    await resumed.open()
    facts.restored = resumed.restored
    session = resumed.attach(resumed.options)
    turn = Turn(session, facts)
    before, asked = len(facts.results), len(messages.requests)
    await turn.send(f"{STEP}resumed")
    await turn.ended()
    last = facts.results[before:]
    if last and facts.restored is not None:
        from .backend_claude import added, counts, model_totals

        # Its result's totals: what it restored on, and what it spent since (each request
        # made after the resume, as the stand-in answered it).
        spent = [counts(usage_of(r["number"])) for r in messages.requests[asked:]]
        expected = added(facts.restored, *spent)
        facts.resumed_ok = model_totals(last[-1].get("modelUsage")) == expected
    await session.disconnect()
    await resumed.stop()


class Turn:
    """The check's view of its session: its words, and when each turn ended."""

    def __init__(self, session: Any, facts: Facts):
        self.session = session
        self.facts = facts
        self.done = asyncio.Event()
        session.on(self.on_event)

    def on_event(self, event: Any):
        kind = event.type.value
        if kind == "assistant.message":
            self.facts.said.append(str(getattr(event.data, "content", "") or ""))
        elif kind == "session.error":
            self.facts.errors.append(str(getattr(event.data, "message", "") or ""))
        if kind in {"session.idle", "assistant.idle", "session.error"}:
            self.done.set()

    async def send(self, text: str):
        self.done.clear()
        await self.session.send(text)

    async def ended(self, seconds: float = 60):
        await asyncio.wait_for(self.done.wait(), seconds)
        self.done.clear()


async def until(check: Any, seconds: float):
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise RuntimeError("a step of the check did not happen in time")
        await asyncio.sleep(0.05)


def running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def check_verdict(version: str, facts: Facts, happened: list[str], reached: list[str]) -> dict:
    """What the check showed: why no worker runs on this version (`acting`: an edit not asked
    about, a refused one done, Stop not ending a command, the gate missing a wakeup or a
    blocked one costing a request, a field Chief of Staff reads missing, a connection off this
    machine), why workers that ask first do not (`ask_first`: an action not asked about, one
    done though refused, a hook that failed and did not block), and what it can do (`steering`,
    `usage`)."""
    said = f"Chief of Staff's check of Claude Code {version}"
    edits = {
        f"Write {ALLOWED_FILE}": "a new file",
        f"Write {REFUSED_FILE}": "a second new file",
        f"Edit {ALLOWED_FILE}": "an edit",
    }
    wanted = {
        "custom-tool": "a call of Chief of Staff's own tools",
        "shell": "a command",
        "mcp": "an MCP tool's call",
        **edits,
    }
    missing = [what for key, what in wanted.items() if key not in facts.asked]
    written = any(name in happened for name in (REFUSED_FILE, f"{ALLOWED_FILE} edited"))
    acting: list[str] = []
    if written or any(key not in facts.asked for key in edits):
        acting.append("its edits do not all ask Chief of Staff first")
    if not facts.interrupt_ended:
        acting.append("stopping a turn does not end its command")
    if not facts.gate_seen or facts.wakeup_requests != 0:
        acting.append("its gate before the model does not see (or cannot hold) a wakeup")
    if not facts.resumed_ok:
        acting.append("a resumed worker's spend cannot be counted")
    absent = [
        f"{kind} {field}"
        for kind, fields in CLAUDE_USES.items()
        for field in fields
        if field not in facts.seen.get(kind, set())
    ]
    if absent:
        acting.append(
            "it lacks what Chief of Staff reads (" + ", ".join(absent[:4])
            + (", …" if len(absent) > 4 else "") + ")"
        )  # fmt: skip
    if reached:
        acting.append("it connected off this machine (" + ", ".join(reached[:2]) + ")")
    if facts.tool_calls != 1:
        acting.append("Chief of Staff's own tools did not work")
    ask_first: list[str] = []
    if missing:
        ask_first.append("did not ask first for " + ", ".join(missing))
    done = [name for name in happened if name in {COMMAND_MARK, SERVER_MARK}]
    if done:
        ask_first.append("did what was refused (" + ", ".join(done) + ")")
    if not facts.raised or RAISING_MARK in happened:
        ask_first.append("ran a command though Chief of Staff's check of it failed")
    usage_ok = (
        bool(facts.responses)
        and facts.usage_matches
        and all("start" in found and "delta" in found for found in facts.responses.values())
    )
    return {
        "ran": True,
        "acting": f"{said} found that {' and '.join(acting)}: no worker runs on it."
        if acting
        else "",
        "ask_first": f"{said} found that it {' and '.join(ask_first)}: workers that ask first "
        "do not run on it."
        if ask_first
        else "",
        "steering": "steered" in facts.said,
        "usage": usage_ok,
        "background_code": facts.background_code,
        # Chief of Staff's tools given to the model upfront (alwaysLoad), not behind its search.
        "tools_upfront": f"mcp__chief__{TOOL}" in facts.offered,
    }
