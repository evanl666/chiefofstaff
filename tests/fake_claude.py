"""A stand-in for Claude Code (2.1.322's stream-json control protocol: the part the Agent SDK and
Chief of Staff use), for tests: run by the Agent SDK as the CLI, with a tiny scripted model that
understands the prompts the tests send.

Run as `fake_claude.py --version`, `fake_claude.py auth status --json`, or as the SDK runs the CLI
(`--output-format stream-json --input-format stream-json ...`). Its sessions are kept in
FAKE_CLAUDE_HOME (one JSON file each, and a transcript where the SDK reads Claude's), so another
process resumes them. FAKE_CLAUDE_* variables shape it: VERSION; SIGNED_OUT; AUTH (JSON: what
`auth status` says); INIT (JSON merged into the answer to initialize: hooks not applied, say);
SYSTEM (JSON merged into each turn's system/init: another permission mode); UNASKED (tools it
runs without asking PreToolUse, comma-separated); RUNS (a file each run's arguments are appended
to); LOG (a file each message from the SDK is appended to); ON_ITS_OWN (a turn it starts after
each one, past the gate: one no gate admitted); LIMIT (JSON: a rate_limit_event's info, sent
before each turn); GATE_DELAY (seconds a prompt waits before its gate); NO_STEER (notes sent
during a turn are taken up after it). With ANTHROPIC_BASE_URL set (Chief of Staff's check), its
turns come from that stand-in Messages API, as Claude Code's would.
"""

import asyncio
import contextlib
import json
import os
import re
import signal
import sys
import time
import uuid
from pathlib import Path

HOME = Path(os.environ.get("FAKE_CLAUDE_HOME") or ".")
# A stand-in Messages API to take its turns from (Chief of Staff's check sets it); else its brain.
REMOTE = os.environ.get("ANTHROPIC_BASE_URL", "")
# How Claude gives the model a note sent while a tool ran (2.1.322: a message of its own).
STEERED = "The user sent a new message while you were working:"
VERSION = os.environ.get("FAKE_CLAUDE_VERSION", "2.1.322")
UNASKED = set(filter(None, os.environ.get("FAKE_CLAUDE_UNASKED", "").split(",")))
MODELS = [
    {
        "value": "default",
        "displayName": "Default (recommended)",
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high"],
    },
    {
        "value": "sonnet",
        "displayName": "Sonnet",
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high"],
    },
    {"value": "haiku", "displayName": "Haiku", "supportsEffort": False},
]


def note(name: str, entry):
    if path := os.environ.get(name):
        with Path(path).open("a") as file:
            file.write(json.dumps(entry) + "\n")


def arguments(argv: list[str]) -> dict:
    """The flags the SDK passes, by name (a flag alone is True)."""
    found: dict = {}
    index = 0
    while index < len(argv):
        word = argv[index]
        if word.startswith("--") and "=" in word:
            name, value = word[2:].split("=", 1)
            found.setdefault(name, []).append(value)
            index += 1
            continue
        if word.startswith("--"):
            name = word[2:]
            following = argv[index + 1] if index + 1 < len(argv) else None
            if following is not None and not following.startswith("--"):
                found.setdefault(name, []).append(following)
                index += 2
                continue
            found.setdefault(name, []).append(True)
        index += 1
    return {name: values[-1] if len(values) == 1 else values for name, values in found.items()}


def edit(path: Path, old: str, new: str):
    path.write_text(path.read_text().replace(old, new, 1))


def usage(number: int) -> dict:
    return {
        "input_tokens": 50 + number,
        "output_tokens": 5,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 10,
    }


class Fake:
    def __init__(self, flags: dict):
        self.flags = flags
        self.out = sys.stdout
        self.ids = 0
        self.pending: dict[str, asyncio.Future] = {}
        self.hooks: dict[str, list[str]] = {}
        self.session_id = str(flags.get("resume") or flags.get("session-id") or uuid.uuid4())
        self.cwd = str(Path.cwd())
        self.model = str(flags.get("model") or "claude-fake-1")
        self.name = str(flags.get("name") or "")
        self.servers = json.loads(flags.get("mcp-config") or "{}").get("mcpServers") or {}
        self.state = self.load() if flags.get("resume") else self.fresh()
        self.prompts: asyncio.Queue = asyncio.Queue()
        self.steering: list[dict] = []
        self.turning = False
        self.interrupted = False
        self.running: asyncio.subprocess.Process | None = None
        self.background: dict[str, dict] = {}
        self.responses = 0
        self.mcp_ready = False
        self.chief_tools: list[str] = []
        self.stdio: dict[str, asyncio.subprocess.Process] = {}
        self.plan: list[dict] | None = None
        self.final: dict | None = None
        self.tasks: set[asyncio.Task] = set()

    # Its sessions, kept for another process

    def fresh(self) -> dict:
        if (HOME / "sessions" / f"{self.session_id}.json").exists():
            sys.stderr.write(f"Error: Session ID {self.session_id} is already in use.\n")
            sys.stderr.flush()
            sys.exit(1)
        return {"memory": "", "totals": {}, "transcript": [], "cwd": self.cwd}

    def load(self) -> dict:
        try:
            return json.loads((HOME / "sessions" / f"{self.session_id}.json").read_text())
        except (OSError, ValueError):
            sys.stderr.write(f"No conversation found with session ID: {self.session_id}\n")
            sys.stderr.flush()
            sys.exit(1)

    def save(self):
        (HOME / "sessions").mkdir(parents=True, exist_ok=True)
        (HOME / "sessions" / f"{self.session_id}.json").write_text(json.dumps(self.state))
        with contextlib.suppress(Exception):
            from claude_agent_sdk._internal.sessions import _get_project_dir

            folder = _get_project_dir(self.cwd)
            folder.mkdir(parents=True, exist_ok=True)
            lines, parent = [], None
            for entry in self.state["transcript"]:
                line = {
                    **entry,
                    "parentUuid": parent,
                    "sessionId": self.session_id,
                    "cwd": self.cwd,
                }
                lines.append(json.dumps(line))
                parent = entry["uuid"]
            (folder / f"{self.session_id}.jsonl").write_text("\n".join(lines) + "\n")

    def record(self, kind: str, content):
        self.state["transcript"].append(
            {"type": kind, "uuid": str(uuid.uuid4()), "message": {"role": kind, "content": content}}
        )

    # The wire

    def send(self, message: dict):
        self.out.write(json.dumps(message) + "\n")
        self.out.flush()

    def system(self, subtype: str, **fields):
        self.send(
            {
                "type": "system",
                "subtype": subtype,
                "uuid": str(uuid.uuid4()),
                "session_id": self.session_id,
                **fields,
            }
        )

    async def ask(self, request: dict) -> dict:
        """A request of the CLI's to the SDK (a hook, a permission question, an MCP message)."""
        self.ids += 1
        request_id = f"fake_{self.ids}"
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self.send({"type": "control_request", "request_id": request_id, "request": request})
        return await future

    async def hook(self, event: str, data: dict, tool_use_id: str | None = None) -> dict | None:
        """Each callback of a hook event; None when one failed (it blocks: on_failure)."""
        answer: dict = {}
        for callback in self.hooks.get(event, []):
            reply = await self.ask(
                {
                    "subtype": "hook_callback",
                    "callback_id": callback,
                    "input": {
                        "hook_event_name": event,
                        "session_id": self.session_id,
                        "cwd": self.cwd,
                        **data,
                    },
                    "tool_use_id": tool_use_id,
                }
            )
            if reply.get("error"):
                return None
            answer |= reply.get("response") or {}
        return answer

    async def read(self):
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        while line := await reader.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            note("FAKE_CLAUDE_LOG", message)
            kind = message.get("type")
            if kind == "control_response":
                response = message.get("response") or {}
                future = self.pending.pop(response.get("request_id"), None)
                if future and not future.done():
                    future.set_result(response)
            elif kind == "control_request":
                task = asyncio.ensure_future(self.control(message))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            elif kind == "user":
                content = (message.get("message") or {}).get("content")
                entry = {"content": content, "uuid": message.get("uuid")}
                if self.turning:
                    self.steering.append(entry)
                else:
                    self.prompts.put_nowait(entry)
        await self.end()

    async def control(self, message: dict):
        request = message.get("request") or {}
        subtype = request.get("subtype")
        answer: dict = {}
        if subtype == "initialize":
            for event, matchers in (request.get("hooks") or {}).items():
                for matcher in matchers or []:
                    self.hooks.setdefault(event, []).extend(matcher.get("hookCallbackIds") or [])
            answer = {
                "commands": [],
                "models": MODELS,
                "account": {"apiKeySource": "ANTHROPIC_API_KEY", "apiProvider": "firstParty"},
                "hooks_applied": True,
                "hook_on_failure_supported": True,
                "claude_code_version": VERSION,
                "current_permission_mode": "default",
                "pid": os.getpid(),
                **json.loads(os.environ.get("FAKE_CLAUDE_INIT") or "{}"),
            }
        elif subtype == "interrupt":
            self.interrupted = True
            if self.running and self.running.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.running.pid, signal.SIGKILL)
        elif subtype == "set_model":
            self.model = request.get("model") or "claude-fake-1"
        elif subtype == "stop_task":
            task = self.background.get(request.get("task_id") or "")
            if not task:
                self.send(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "error",
                            "request_id": message.get("request_id"),
                            "error": "Task not found",
                        },
                    }
                )
                return
            task["stopped"] = True
            with contextlib.suppress(ProcessLookupError):
                os.killpg(task["process"].pid, signal.SIGKILL)
        elif subtype == "mcp_status":
            answer = {"mcpServers": self.mcp_status()}
        self.send(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": message.get("request_id"),
                    "response": answer,
                },
            }
        )

    def mcp_status(self) -> list[dict]:
        found = [{"name": "chief", "status": "connected", "scope": "dynamic", "tools": []}]
        for name, config in self.servers.items():
            if name == "chief":
                continue
            entry = {
                "name": name,
                "status": "connected",
                "config": config,
                "scope": "dynamic",
                "tools": [
                    {"name": "write_marker", "annotations": {"readOnly": False}},
                    {"name": "read_marker", "annotations": {"readOnly": True}},
                ],
            }
            found.append(entry)
        return found

    async def mcp(self, method: str, params: dict | None = None) -> dict:
        reply = await self.ask(
            {
                "subtype": "mcp_message",
                "server_name": "chief",
                "message": {
                    "jsonrpc": "2.0",
                    "id": self.ids + 1,
                    "method": method,
                    "params": params or {},
                },
            }
        )
        return ((reply.get("response") or {}).get("mcp_response") or {}).get("result") or {}

    async def chief_ready(self):
        if self.mcp_ready or "chief" not in self.servers:
            return
        await self.mcp(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "fake-claude", "version": VERSION},
            },
        )
        listed = await self.mcp("tools/list")
        self.chief_tools = [str(tool.get("name")) for tool in listed.get("tools") or []]
        self.mcp_ready = True

    # Turns

    async def turns(self):
        while True:
            entry = await self.prompts.get()
            self.turning = True
            try:
                await self.turn(entry)
            finally:
                self.turning = False
            for pending in self.steering:
                self.prompts.put_nowait(pending)  # Not taken up within the turn.
            self.steering = []
            if os.environ.get("FAKE_CLAUDE_ON_ITS_OWN") and not entry.get("own"):
                # A turn no gate saw (a version whose gate misses a prompt).
                self.prompts.put_nowait({"content": "on its own", "own": True, "ungated": True})

    def text(self, content) -> str:
        if isinstance(content, str):
            return content
        return "\n".join(
            str(block.get("text") or "")
            for block in content or []
            if isinstance(block, dict) and block.get("type") == "text"
        )

    def result(
        self,
        *,
        turns: int,
        spent: dict,
        text: str = "",
        error: str = "",
        origin=None,
        subtype: str = "success",
        reason: str = "completed",
    ):
        self.send(
            {
                "type": "result",
                "subtype": subtype,
                "is_error": bool(error) or subtype != "success",
                "num_turns": turns,
                "result": error or text,
                "session_id": self.session_id,
                "usage": spent,
                "modelUsage": self.state["totals"],
                "total_cost_usd": 0.0,
                "terminal_reason": reason,
                "origin": origin,
                "uuid": str(uuid.uuid4()),
            }
        )
        self.save()

    async def turn(self, entry: dict):
        text = self.text(entry["content"])
        if limit := os.environ.get("FAKE_CLAUDE_LIMIT"):
            self.send(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": json.loads(limit),
                    "uuid": str(uuid.uuid4()),
                    "session_id": self.session_id,
                }
            )
        if delay := os.environ.get("FAKE_CLAUDE_GATE_DELAY"):
            await asyncio.sleep(float(delay))  # A prompt waiting its turn in Claude's queue.
        if not entry.get("ungated"):
            answer = await self.hook(
                "UserPromptSubmit", {"prompt": text, "prompt_id": str(uuid.uuid4())}
            )
            if answer is None or answer.get("decision") == "block":
                why = (answer or {}).get("reason") or "hook failed"
                self.system(
                    "informational",
                    content=f"UserPromptSubmit operation blocked by hook:\n{why}",
                    prompt_submit_outcome={"kind": "blocked_by_hook", "prompt_kept": False},
                )
                self.result(turns=0, spent=dict.fromkeys(usage(0), 0), origin=entry.get("origin"))
                return
        names = dict.fromkeys(["chief", *self.servers])
        init = {
            "cwd": self.cwd,
            "session_id": self.session_id,
            "model": self.model,
            "permissionMode": "default",
            "claude_code_version": VERSION,
            "mcp_servers": [{"name": name, "status": "connected"} for name in names],
            "tools": ["Bash", "Write", "Edit", "Read"],
        }
        self.system("init", **(init | json.loads(os.environ.get("FAKE_CLAUDE_SYSTEM") or "{}")))
        if entry.get("uuid") and "replay-user-messages" in self.flags:
            self.send(
                {
                    "type": "user",
                    "message": {"role": "user", "content": entry["content"]},
                    "uuid": entry["uuid"],
                    "isReplay": True,
                    "parent_tool_use_id": None,
                    "session_id": self.session_id,
                }
            )
        self.record("user", entry["content"])
        conversation = self.state.setdefault("messages", [])
        conversation.append({"role": "user", "content": entry["content"]})
        await self.chief_ready()
        if "Fail this turn" in text:
            self.send(
                {
                    "type": "assistant",
                    "error": "server_error",
                    "message": {
                        "id": f"msg_{uuid.uuid4().hex[:8]}",
                        "role": "assistant",
                        "model": "<synthetic>",
                        "content": [{"type": "text", "text": "API Error: 529 Overloaded"}],
                    },
                    "parent_tool_use_id": None,
                    "session_id": self.session_id,
                    "uuid": str(uuid.uuid4()),
                }
            )
            self.result(
                turns=1, spent=dict.fromkeys(usage(0), 0), error="API Error: 529 Overloaded"
            )
            return
        spent: dict = {}
        turns = 0
        self.plan = None if REMOTE else self.brain(text)
        self.final = None
        while True:
            turns += 1
            blocks, stop, given = await self.think(conversation)
            self.respond(blocks, stop, spent, given)
            conversation.append({"role": "assistant", "content": blocks})
            if stop != "tool_use":
                self.record("assistant", blocks)
                said = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                self.result(turns=turns, spent=spent, text=said, origin=entry.get("origin"))
                return
            answered = []
            for block in blocks:
                if block.get("type") == "tool_use":
                    answered.append(await self.run(block))
                    if self.interrupted:
                        break
            if self.interrupted:
                self.interrupted = False
                self.send(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "[Request interrupted by user for tool use]",
                                }
                            ],
                        },
                        "uuid": str(uuid.uuid4()),
                        "parent_tool_use_id": None,
                        "session_id": self.session_id,
                    }
                )
                conversation.append({"role": "user", "content": answered})
                self.result(
                    turns=turns,
                    spent=spent,
                    subtype="error_during_execution",
                    reason="aborted_tools",
                    origin=entry.get("origin"),
                )
                return
            conversation.append({"role": "user", "content": answered})
            if os.environ.get("FAKE_CLAUDE_NO_STEER"):
                continue  # A version that takes notes up only after the turn.
            for note in await self.steered():
                # As Claude gives a note sent mid-turn: a message of its own, with the result.
                conversation.append(
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "text",
                                "text": f"{STEERED}\n{note}",
                            }
                        ],
                    }
                )

    async def think(self, conversation: list[dict]) -> tuple[list[dict], str, dict]:
        """The model's next response: from the stand-in Messages API this CLI was pointed at
        (ANTHROPIC_BASE_URL), else from its own scripted brain."""
        if REMOTE:
            return await asyncio.to_thread(self.ask_model, conversation)
        self.responses += 1
        given = usage(len(self.state["transcript"]) * 10 + self.responses)
        if self.plan and self.plan[0]["type"] == "tool_use":
            return [self.plan.pop(0)], "tool_use", given
        final = self.final or (self.plan[-1] if self.plan else {"type": "text", "text": "done"})
        return [final], "end_turn", given

    def ask_model(self, conversation: list[dict]) -> tuple[list[dict], str, dict]:
        import urllib.request

        tools = ["Agent", "AskUserQuestion", "Bash", "Edit", "Read", "Write", "ToolSearch"]
        tools += [f"mcp__chief__{name}" for name in self.chief_tools]
        body = {
            "model": self.model,
            "max_tokens": 1024,
            "stream": False,
            "messages": conversation,
            "tools": [{"name": name} for name in tools],
        }
        request = urllib.request.Request(  # noqa: S310 (the stand-in's http://127.0.0.1)
            REMOTE.rstrip("/") + "/v1/messages",
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json", "x-api-key": "fake"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            message = json.loads(response.read())
        return message["content"], message.get("stop_reason") or "end_turn", message["usage"]

    async def steered(self) -> list[str]:
        """Messages that came while a tool ran: each through the gate, into this turn."""
        notes = []
        while self.steering:
            entry = self.steering.pop(0)
            text = self.text(entry["content"])
            answer = await self.hook(
                "UserPromptSubmit", {"prompt": text, "prompt_id": str(uuid.uuid4())}
            )
            if answer is None or answer.get("decision") == "block":
                continue
            if entry.get("uuid"):
                self.send(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": entry["content"]},
                        "uuid": entry["uuid"],
                        "isReplay": True,
                        "parent_tool_use_id": None,
                        "session_id": self.session_id,
                    }
                )
            self.record("user", entry["content"])
            notes.append(text)
            if found := re.search(r"reply with just: (\w+)", text, re.IGNORECASE):
                self.final = {"type": "text", "text": found.group(1)}
        return notes

    def respond(self, blocks: list[dict], stop: str, spent: dict, given: dict):
        """One model response: its stream events, then each block as an assistant message."""
        message_id = f"msg_fake_{uuid.uuid4().hex[:12]}"
        self.send(
            {
                "type": "stream_event",
                "event": {
                    "type": "message_start",
                    "message": {
                        "id": message_id,
                        "model": self.model,
                        "usage": {**given, "output_tokens": 1},
                    },
                },
                "parent_tool_use_id": None,
                "session_id": self.session_id,
                "uuid": str(uuid.uuid4()),
            }
        )
        for block in blocks:
            self.send(
                {
                    "type": "assistant",
                    "message": {
                        "id": message_id,
                        "model": self.model,
                        "role": "assistant",
                        "content": [block],
                        "usage": {**given, "output_tokens": 1},
                    },
                    "parent_tool_use_id": None,
                    "session_id": self.session_id,
                    "uuid": str(uuid.uuid4()),
                }
            )
        self.send(
            {
                "type": "stream_event",
                "event": {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop},
                    "usage": {"output_tokens": given["output_tokens"]},
                },
                "parent_tool_use_id": None,
                "session_id": self.session_id,
                "uuid": str(uuid.uuid4()),
            }
        )
        for name, value in given.items():
            spent[name] = spent.get(name, 0) + value
        totals = self.state["totals"].setdefault(self.model, {})
        for name, key in (
            ("input_tokens", "inputTokens"),
            ("output_tokens", "outputTokens"),
            ("cache_read_input_tokens", "cacheReadInputTokens"),
            ("cache_creation_input_tokens", "cacheCreationInputTokens"),
        ):
            totals[key] = totals.get(key, 0) + given[name]

    def brain(self, text: str) -> list[dict]:
        """The calls a prompt asks for, then the reply."""
        plan: list[dict] = []

        def call(name: str, data: dict):
            plan.append(
                {
                    "type": "tool_use",
                    "id": f"toolu_{uuid.uuid4().hex[:12]}",
                    "name": name,
                    "input": data,
                }
            )

        if found := re.search(r"Call the (\w+) tool with text '([^']*)'", text):
            call(f"mcp__chief__{found.group(1)}", {"text": found.group(2)})
        if found := re.search(r"Run the shell command `([^`]+)`", text):
            call("Bash", {"command": found.group(1), "description": "a command"})
        if found := re.search(r"Leave the command `([^`]+)` running for ([\d.]+) s", text):
            command = f"sleep {found.group(2)}; {found.group(1)}; exit 3"
            call(
                "Bash",
                {"command": command, "description": "left running", "run_in_background": True},
            )
        elif found := re.search(r"Leave the command `([^`]+)` running", text):
            call(
                "Bash",
                {
                    "command": found.group(1),
                    "description": "left running",
                    "run_in_background": True,
                },
            )
        if found := re.search(r"Report done: ([^\n<]+)", text):
            call("mcp__chief__report", {"status": "done", "summary": found.group(1).strip()})
        if found := re.search(r"Write the file `([^`]+)`", text):
            call("Write", {"file_path": str(Path(self.cwd) / found.group(1)), "content": "x\n"})
        if found := re.search(r"Use Claude's (\w+) tool", text):
            call(found.group(1), {})
        if found := re.search(r"Call the MCP tool (\w+) of (\w+)", text):
            call(f"mcp__{found.group(2)}__{found.group(1)}", {})
        if re.search(r"Ask the owner which color", text):
            call(
                "AskUserQuestion",
                {
                    "questions": [
                        {
                            "question": "Which color?",
                            "header": "Color",
                            "multiSelect": False,
                            "options": [
                                {"label": "red", "description": "warm"},
                                {"label": "blue", "description": "cool"},
                            ],
                        }
                    ]
                },
            )
        if found := re.search(r"Remember the word '(\w+)'", text):
            self.state["memory"] = found.group(1)
        if "What word did I ask you to remember" in text:
            plan.append({"type": "text", "text": self.state["memory"]})
            return plan
        if text.startswith("<task-notification>"):
            plan.append({"type": "text", "text": "noted"})
            return plan
        if found := re.search(r"reply with just: (\w+)", text, re.IGNORECASE):
            plan.append({"type": "text", "text": found.group(1)})
        return plan

    async def run(self, block: dict) -> dict:
        """One tool call: asked about first (PreToolUse, then can_use_tool when it asks), then
        run; its result as Claude gives it."""
        name, data, call = block["name"], block["input"], block["id"]
        decision, reason = "allow", ""
        if name not in UNASKED:
            answer = await self.hook(
                "PreToolUse",
                {"tool_name": name, "tool_input": data, "tool_use_id": call, "agent_id": None},
                call,
            )
            if answer is None:
                self.system(
                    "informational",
                    tag="hook_callback_failed",
                    content="PreToolUse: Hook callback failed",
                )
                decision, reason = (
                    "deny",
                    '[callback]: failed; blocking because onFailure is "block"',
                )
            else:
                specific = answer.get("hookSpecificOutput") or {}
                decision = specific.get("permissionDecision") or "ask"
                reason = specific.get("permissionDecisionReason") or ""
                data = specific.get("updatedInput") or data
        if decision == "ask":
            reply = await self.ask(
                {
                    "subtype": "can_use_tool",
                    "tool_name": name,
                    "input": data,
                    "tool_use_id": call,
                    "permission_suggestions": [],
                }
            )
            answer = reply.get("response") or {}
            if answer.get("behavior") == "allow":
                decision, data = "allow", answer.get("updatedInput") or data
            else:
                decision, reason = "deny", answer.get("message") or "denied"
        if decision != "allow":
            return self.tool_result(
                call,
                f"PreToolUse:{name} hook error: {reason}",
                True,
                f"Error: PreToolUse:{name} hook error: {reason}",
                {"non_execution_kind": "permission-rule"},
            )
        if name == "Bash":
            return await self.bash(call, data)
        if name == "Write":
            await asyncio.to_thread(Path(data["file_path"]).write_text, data.get("content") or "")
            return self.tool_result(
                call,
                f"File created successfully at: {data['file_path']}",
                False,
                {"type": "create"},
            )
        if name == "Edit":
            path = Path(data["file_path"])
            await asyncio.to_thread(
                edit, path, data.get("old_string", ""), data.get("new_string", "")
            )
            return self.tool_result(
                call, f"The file {path} has been updated.", False, {"filePath": str(path)}
            )
        if name.startswith("mcp__chief__"):
            found = await self.mcp(
                "tools/call", {"name": name.removeprefix("mcp__chief__"), "arguments": data}
            )
            text = "\n".join(str(part.get("text") or "") for part in found.get("content") or [])
            return self.tool_result(call, text, bool(found.get("isError")), found)
        if name.startswith("mcp__"):
            server, _, tool = name[5:].partition("__")
            text = await self.stdio_call(server, tool, data)
            return self.tool_result(call, text, False, {"ok": True})
        if name == "AskUserQuestion":
            answers = data.get("answers") or {}
            said = ", ".join(f'"{q}"="{a}"' for q, a in answers.items())
            return self.tool_result(
                call, f"Your questions have been answered: {said}.", False, data
            )
        return self.tool_result(call, "ok", False, {"ok": True})

    async def stdio_call(self, server: str, tool: str, data: dict) -> str:
        """A tool of a stdio MCP server it was given (started on first use)."""
        config = self.servers.get(server) or {}
        if not config.get("command"):
            return "ok"
        if server not in self.stdio:
            self.stdio[server] = await asyncio.create_subprocess_exec(
                config["command"],
                *(config.get("args") or []),
                cwd=self.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
            with contextlib.suppress(OSError, ValueError):
                await self.stdio_request(server, "initialize", {"protocolVersion": "2025-06-18"})
        try:
            found = await self.stdio_request(
                server, "tools/call", {"name": tool, "arguments": data}
            )
        except (OSError, ValueError):
            return "ok"  # Not a server that answers (a test's `true`).
        return "\n".join(str(part.get("text") or "") for part in found.get("content") or [])

    async def stdio_request(self, server: str, method: str, params: dict) -> dict:
        process = self.stdio[server]
        self.ids += 1
        message = {"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params}
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()
        line = await process.stdout.readline()
        return (json.loads(line) if line else {}).get("result") or {}

    def tool_result(
        self, call: str, text: str, error: bool, structured, meta: dict | None = None
    ) -> dict:
        block = {"type": "tool_result", "tool_use_id": call, "content": text, "is_error": error}
        self.send(
            {
                "type": "user",
                "message": {"role": "user", "content": [block]},
                "tool_use_result": structured,
                "tool_result_meta": [{"id": call, **(meta or {})}],
                "parent_tool_use_id": None,
                "session_id": self.session_id,
                "uuid": str(uuid.uuid4()),
            }
        )
        return block

    async def bash(self, call: str, data: dict) -> dict:
        command = data.get("command") or ""
        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            command,
            cwd=self.cwd,
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        task_id = f"b{uuid.uuid4().hex[:8]}"
        if data.get("run_in_background"):
            self.background[task_id] = {
                "process": process,
                "call": call,
                "description": data.get("description") or "",
            }
            self.system(
                "task_started",
                task_id=task_id,
                tool_use_id=call,
                description=data.get("description"),
                is_backgrounded=True,
                task_type="local_bash",
            )
            task = asyncio.ensure_future(self.background_ended(task_id))
            self.tasks.add(task)
            return self.tool_result(
                call,
                f"Command running in background with ID: {task_id}. Output is being written "
                f"to: /tmp/{task_id}.output",
                False,
                {"stdout": "", "stderr": "", "interrupted": False, "backgroundTaskId": task_id},
            )
        self.running = process
        self.system(
            "task_started",
            task_id=task_id,
            tool_use_id=call,
            description=data.get("description"),
            is_backgrounded=False,
            task_type="local_bash",
        )
        output, _ = await process.communicate()
        self.running = None
        text = output.decode(errors="replace").rstrip("\n")
        if self.interrupted:
            self.system(
                "task_notification",
                task_id=task_id,
                tool_use_id=call,
                status="stopped",
                output_file="",
                summary="",
            )
            return self.tool_result(
                call,
                "The user doesn't want to proceed with this tool use.",
                True,
                "User rejected tool use",
                {"non_execution_kind": "user-rejected"},
            )
        self.system(
            "task_notification",
            task_id=task_id,
            tool_use_id=call,
            status="completed",
            output_file="",
            summary="",
        )
        if process.returncode:
            said = f"Exit code {process.returncode}\n{text}".rstrip("\n")
            return self.tool_result(call, said, True, f"Error: {said}")
        return self.tool_result(
            call,
            text or "(Bash completed with no output)",
            False,
            {"stdout": text, "stderr": "", "interrupted": False, "isImage": False},
        )

    async def background_ended(self, task_id: str):
        task = self.background[task_id]
        await task["process"].wait()
        code = task["process"].returncode
        if task.get("stopped"):
            self.system(
                "task_updated",
                task_id=task_id,
                patch={"status": "killed", "end_time": int(time.time() * 1000)},
            )
            self.background.pop(task_id, None)
            return
        status = "completed" if code == 0 else "failed"
        self.system(
            "task_updated",
            task_id=task_id,
            patch={"status": status, "end_time": int(time.time() * 1000)},
        )
        summary = f'Background command "{task["description"]}" {status} (exit code {code})'
        self.system(
            "task_notification",
            task_id=task_id,
            tool_use_id=task["call"],
            status=status,
            output_file=f"/tmp/{task_id}.output",
            summary=summary,
        )
        self.background.pop(task_id, None)
        prompt = (
            f"<task-notification>\n<task-id>{task_id}</task-id>\n<tool-use-id>{task['call']}</tool-use-id>\n"
            f"<status>{status}</status>\n<summary>{summary}</summary>\n</task-notification>"
        )
        entry = {"content": prompt, "origin": {"kind": "task-notification"}}
        if self.turning:
            self.steering.append({**entry, "uuid": None})
        else:
            self.prompts.put_nowait(entry)

    async def end(self):
        for process in self.stdio.values():
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        for task in self.background.values():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(task["process"].pid, signal.SIGKILL)
        self.save()
        os._exit(0)


async def serve(flags: dict):
    fake = Fake(flags)
    fake.save()
    turns = asyncio.ensure_future(fake.turns())
    await fake.read()
    turns.cancel()


def main():
    argv = sys.argv[1:]
    note("FAKE_CLAUDE_RUNS", argv)
    if argv[:1] == ["--version"]:
        sys.stdout.write(f"{VERSION} (Claude Code)\n")
        return
    if argv[:2] == ["auth", "status"]:
        if os.environ.get("FAKE_CLAUDE_SIGNED_OUT"):
            out = {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}
            sys.stdout.write(json.dumps(out) + "\n")
            sys.exit(1)
        said = {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "apiKeySource": None,
            "email": "someone@example.com",
            "orgName": "Example",
        }
        said |= json.loads(os.environ.get("FAKE_CLAUDE_AUTH") or "{}")
        sys.stdout.write(json.dumps(said) + "\n")
        return
    asyncio.run(serve(arguments(argv)))


if __name__ == "__main__":
    main()
