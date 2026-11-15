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
as each turn's first request is answered); GATE_DELAY (seconds a prompt waits before its gate);
NO_STEER (notes sent during a turn are taken up after it); EXIT_DELAY (seconds it takes to end
once its input closes, its sessions/<pid>.json kept meanwhile). With ANTHROPIC_BASE_URL set
(Chief of Staff's check), its turns come from that stand-in Messages API, as Claude Code's would.
A prompt saying "Hit the usage limit" (or "rate limit") is refused as for a rate limit (a 429),
as 2.1.322 says it."""

import asyncio
import atexit
import contextlib
import json
import os
import re
import shlex
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
        self.matchers: dict[str, str] = {}  # A hook callback's matcher (none: every tool).
        self.mode = "default"
        self.schedules: dict[str, dict] = {}
        self.entered: list[str] = []
        self.plan_file = ""
        self.agents: dict[str, asyncio.Future] = {}
        self.session_id = str(flags.get("resume") or flags.get("session-id") or uuid.uuid4())
        self.cwd = str(Path.cwd())
        self.model = str(flags.get("model") or "claude-fake-1")
        self.effort = str(flags.get("effort") or "")
        self.name = str(flags.get("name") or "")
        # Each --mcp-config (JSON, or a file's path, as Claude reads them), and the system prompt
        # appended (as text, or from a file).
        self.servers: dict = {}
        configs = flags.get("mcp-config") or []
        for config in configs if isinstance(configs, list) else [configs]:
            text = config if config.lstrip().startswith("{") else Path(config).read_text()
            self.servers |= json.loads(text).get("mcpServers") or {}
        # Claude's sandbox for its commands, as --settings says (emulated: sandboxed).
        settings = json.loads(flags["settings"]) if isinstance(flags.get("settings"), str) else {}
        self.sandbox = (settings.get("sandbox") or {}) if isinstance(settings, dict) else {}
        appended = flags.get("append-system-prompt-file")
        self.appended = Path(appended).read_text() if appended else ""
        self.appended = self.appended or str(flags.get("append-system-prompt") or "")
        note(
            "FAKE_CLAUDE_STARTS",
            {
                "cwd": self.cwd,
                "pwd": os.environ.get("PWD"),
                "own": sorted(k for k in os.environ if k.startswith("CHIEFOFSTAFF_")),
                "servers": sorted(self.servers),
                "appended": self.appended,
                "plugins": flags.get("plugin-dir"),
            },
        )
        self.state = self.load() if flags.get("resume") else self.fresh()
        self.announce()
        self.prompts: asyncio.Queue = asyncio.Queue()
        self.steering: list[dict] = []
        self.turning = False
        self.interrupted = False
        self.running: asyncio.subprocess.Process | None = None
        self.background: dict[str, dict] = {}
        self.responses = 0
        self.mcp_ready = False
        self.chief_tools: list[str] = []
        self.sdk_ready: set[str] = set()
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

    def announce(self):
        """As Claude 2.1.x: `sessions/<pid>.json` in its configuration folder while it runs (an
        SDK's process says so: entrypoint sdk-py), gone as it ends."""
        given = os.environ.get("CLAUDE_CONFIG_DIR")
        folder = (Path(given) if given else Path.home() / ".claude") / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        live = folder / f"{os.getpid()}.json"
        said = {"pid": os.getpid(), "sessionId": self.session_id, "cwd": self.cwd}
        said |= {"kind": "interactive", "entrypoint": "sdk-py", "status": "idle"}
        live.write_text(json.dumps(said))
        atexit.register(lambda: live.unlink(missing_ok=True))

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
                    "entrypoint": "sdk-py",
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
            pattern = self.matchers.get(callback)
            if pattern and not re.fullmatch(pattern, str(data.get("tool_name") or "")):
                continue
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
                    ids = matcher.get("hookCallbackIds") or []
                    self.hooks.setdefault(event, []).extend(ids)
                    for callback in ids:
                        if matcher.get("matcher"):
                            self.matchers[callback] = matcher["matcher"]
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
        elif subtype == "apply_flag_settings" and os.environ.get("FAKE_CLAUDE_FIXED_EFFORT"):
            # As a Claude that takes a thinking effort only as it starts.
            self.send({"type": "control_response", "response": {
                "subtype": "error", "request_id": message.get("request_id"),
                "error": "apply_flag_settings: unsupported_key"}})  # fmt: skip
            return
        elif subtype == "apply_flag_settings":
            settings = request.get("settings") or {}
            if "effortLevel" in settings:
                self.effort = str(settings["effortLevel"] or "")
            note("FAKE_CLAUDE_EFFORTS", {"effort": self.effort, "pid": os.getpid()})
        elif subtype == "stop_task" and request.get("task_id") in self.agents:
            self.agents.pop(request["task_id"]).cancel()
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

    async def mcp(self, method: str, params: dict | None = None, server: str = "chief") -> dict:
        reply = await self.ask(
            {
                "subtype": "mcp_message",
                "server_name": server,
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
        **more,
    ):
        self.send(
            more
            | {
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
        note("FAKE_CLAUDE_EFFORTS", {"turn": text[:60], "effort": self.effort, "pid": os.getpid()})
        if delay := os.environ.get("FAKE_CLAUDE_GATE_DELAY"):
            await asyncio.sleep(float(delay))  # A prompt waiting its turn in Claude's queue.
        if not entry.get("ungated"):
            answer = await self.hook(
                "UserPromptSubmit", {"prompt": text, "prompt_id": str(uuid.uuid4())}
            )
            added = ((answer or {}).get("hookSpecificOutput") or {}).get("additionalContext")
            if added:
                note("FAKE_CLAUDE_CONTEXT", {"prompt": text, "context": added})
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
            "permissionMode": self.mode,
            "claude_code_version": VERSION,
            "mcp_servers": [{"name": name, "status": "connected"} for name in names],
            "agents": ["claude", "Explore", "general-purpose", "Plan", "statusline-setup"],
            "plugins": [],
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
        if limit := os.environ.get("FAKE_CLAUDE_LIMIT"):
            # What its first request's answer said of the plan's limits (as Claude says it).
            self.send(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": json.loads(limit),
                    "uuid": str(uuid.uuid4()),
                    "session_id": self.session_id,
                }
            )
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
        if "Hit the usage limit" in text or "Hit the rate limit" in text:
            # As 2.1.322 says a request refused for a rate limit (a 429; a plan's has its
            # rate_limit_event, FAKE_CLAUDE_LIMIT, before): Claude's words, and a failed turn.
            said = "API Error: Request rejected (429) · You've hit your limit"
            self.send(
                {
                    "type": "assistant",
                    "error": "rate_limit",
                    "message": {
                        "id": str(uuid.uuid4()),
                        "role": "assistant",
                        "model": "<synthetic>",
                        "content": [{"type": "text", "text": said}],
                    },
                    "parent_tool_use_id": None,
                    "session_id": self.session_id,
                    "uuid": str(uuid.uuid4()),
                }
            )
            self.result(
                turns=1,
                spent=dict.fromkeys(usage(0), 0),
                error=said,
                reason="api_error",
                api_error_status=429,
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
            for steered in await self.steered():
                # As Claude gives a note sent mid-turn: a message of its own, with the result.
                conversation.append(
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "text",
                                "text": f"{STEERED}\n{steered}",
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
        if found := re.search(r"Fill the form of (\w+)", text):
            call(f"mcp__{found.group(1)}__form", {})
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
        if found := re.search(r"Enter a worktree named (\w+)", text):
            call("EnterWorktree", {"name": found.group(1)})
        if "Leave the worktree and remove it" in text:
            call("ExitWorktree", {"action": "remove", "discard_changes": True})
        if found := re.search(r"Schedule '([^']+)' every minute( durably)?", text):
            schedule = {"cron": "* * * * *", "prompt": found.group(1), "recurring": True}
            call("CronCreate", schedule | ({"durable": True} if found.group(2) else {}))
        if found := re.search(r"Wake up later to '([^']+)'", text):
            wake = {"delaySeconds": 60, "reason": "later", "prompt": found.group(1)}
            call("ScheduleWakeup", wake)
        if found := re.search(r"Message (\S+): '([^']+)'", text):
            message = {"to": found.group(1), "summary": "a note", "message": found.group(2)}
            call("SendMessage", message)
        if found := re.search(r"Plan it: '([^']+)'", text):
            plans = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "plans"
            plans.mkdir(parents=True, exist_ok=True)
            call("EnterPlanMode", {})
            call("Write", {"file_path": str(plans / "a-plan.md"), "content": found.group(1)})
            call("ExitPlanMode", {})
        if found := re.search(
            r"Have a (background )?subagent( in a worktree| in the cloud)?(?: of type (\S+))? "
            r"run `([^`]+)`",
            text,
        ):
            isolation = {" in a worktree": "worktree", " in the cloud": "remote"}.get(
                found.group(2) or ""
            )
            call(
                "Agent",
                {
                    "description": "a subagent",
                    "prompt": f"Run the shell command `{found.group(4)}`",
                    "subagent_type": found.group(3) or "general-purpose",
                    "run_in_background": bool(found.group(1)),
                    **({"isolation": isolation} if isolation else {}),
                },
            )
        if found := re.search(r"Notify me: '([^']+)'", text):
            call("PushNotification", {"message": found.group(1)})
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

    async def run(self, block: dict, agent: dict | None = None) -> dict:
        """One tool call: asked about first (PreToolUse, then can_use_tool when it asks), then
        run; its result as Claude gives it. `agent`: the subagent that makes it (its id, its
        folder, the call that started it)."""
        name, data, call = block["name"], block["input"], block["id"]
        decision, reason = "allow", ""
        if name not in UNASKED:
            asked = {"tool_name": name, "tool_input": data, "tool_use_id": call,
                     "agent_id": None, "permission_mode": self.mode}  # fmt: skip
            if agent:
                asked |= {k: agent[k] for k in ("agent_id", "agent_type", "cwd")}
            answer = await self.hook("PreToolUse", asked, call)
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
        parent = (agent or {}).get("parent")
        if decision != "allow":
            return self.tool_result(
                call,
                f"PreToolUse:{name} hook error: {reason}",
                True,
                f"Error: PreToolUse:{name} hook error: {reason}",
                {"non_execution_kind": "permission-rule"},
                parent=parent,
            )
        if (done := await self.capability(name, call, data, agent)) is not None:
            return done
        if name == "Bash":
            return await self.bash(call, data, (agent or {}).get("cwd"), parent)
        if name == "Write" and self.mode == "plan":
            self.plan_file = data["file_path"]
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
        sdk, _, tool = name[5:].partition("__") if name.startswith("mcp__") else ("", "", "")
        if sdk and sdk != "chief" and (self.servers.get(sdk) or {}).get("type") == "sdk":
            # Another server of the SDK's own process (as chief), through the same requests,
            # once initialized (as Claude initializes each server).
            if sdk not in self.sdk_ready:
                hello = {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "fake-claude", "version": VERSION}}  # fmt: skip
                await self.mcp("initialize", hello, sdk)
                self.sdk_ready.add(sdk)
            found = await self.mcp("tools/call", {"name": tool, "arguments": data}, sdk)
            text = "\n".join(str(part.get("text") or "") for part in found.get("content") or [])
            return self.tool_result(call, text, bool(found.get("isError")), found)
        if name.startswith("mcp__") and name.endswith("__form"):
            # The server asks for a form (elicitation): Claude asks the SDK's host (2.1.322).
            reply = await self.ask(
                {"subtype": "elicitation", "mcp_server_name": name[5:].partition("__")[0],
                 "message": "Which color?", "mode": "form", "property_order": ["color"],
                 "requested_schema": {"type": "object", "properties": {"color": {"type": "string"}},
                                      "required": ["color"]}}
            )  # fmt: skip
            answered = reply.get("response") or {"action": "cancel"}
            return self.tool_result(call, f"Form: {json.dumps(answered)}", False, answered)
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

    # Claude's own capabilities (2.1.322's, as Chief of Staff ties them in: more-agents.md 5.12.4)

    async def capability(self, name: str, call: str, data: dict, agent: dict | None) -> dict | None:
        """A capability tool's run, as 2.1.322 answers it (PostToolUse after it, for those that
        report what they made); None for any other tool."""
        structured: dict | None = None
        text = "ok"
        if name == "EnterWorktree":
            top = await self.git("rev-parse", "--show-toplevel")
            label = data.get("name") or f"w{uuid.uuid4().hex[:6]}"
            path = str(Path(top) / ".claude" / "worktrees" / label)
            await self.git("worktree", "add", "-q", "-b", f"worktree-{label}", path)
            self.entered.append(self.cwd)
            self.cwd = path
            structured = {"worktreePath": path, "worktreeBranch": f"worktree-{label}",
                          "message": f"Created worktree at {path}"}  # fmt: skip
            text = structured["message"]
        elif name == "ExitWorktree":
            path, self.cwd = self.cwd, self.entered.pop() if self.entered else self.cwd
            if data.get("action") == "remove":
                await self.git("worktree", "remove", "--force", path)
            structured = {"action": data.get("action"), "originalCwd": self.cwd,
                          "worktreePath": path, "restoredCwd": self.cwd}  # fmt: skip
        elif name in {"CronCreate", "ScheduleWakeup"}:
            key = uuid.uuid4().hex[:8]
            recurring = name == "CronCreate" and bool(data.get("recurring", True))
            self.schedules[key] = {"prompt": data["prompt"], "recurring": recurring}
            task = asyncio.ensure_future(self.fire(key))
            self.tasks.add(task)
            if name == "CronCreate":
                structured = {
                    "id": key,
                    "humanSchedule": "Every minute",
                    "recurring": recurring,
                    "durable": False,
                    "nextFireAt": int(time.time() * 1000) + 60000,
                }
            else:
                structured = {"scheduledFor": int(time.time() * 1000) + 60000,
                              "clampedDelaySeconds": 60, "wasClamped": False}  # fmt: skip
        elif name == "CronDelete":
            self.schedules.pop(str(data.get("id") or ""), None)
            structured = {"id": data.get("id")}
        elif name == "SendMessage":
            text = f"Message sent to {data.get('to')}."
            note("FAKE_CLAUDE_SENT", {"to": data.get("to"), "message": data.get("message")})
        elif name == "EnterPlanMode":
            self.mode = "plan"
            text = "Entered plan mode."
        elif name == "ExitPlanMode":
            self.mode = "default"
            structured = {"plan": None, "isAgent": False, "filePath": self.plan_file}
            text = "User has approved exiting plan mode."
        elif name in {"Agent", "Task"}:
            return await self.subagent(call, data)
        elif name == "PushNotification":
            text = "Notification sent."
        else:
            return None
        result = self.tool_result(call, text, False, structured or {"ok": True},
                                  parent=(agent or {}).get("parent"))  # fmt: skip
        await self.hook(
            "PostToolUse",
            {"tool_name": name, "tool_input": data, "tool_response": structured or {},
             "tool_use_id": call},
            call,
        )  # fmt: skip
        return result

    async def git(self, *arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git", *arguments, cwd=self.cwd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        output, _ = await process.communicate()
        return output.decode().strip()

    async def fire(self, key: str):
        """A schedule fires (FAKE_CLAUDE_FIRE_SECONDS apart, for a test; Claude's are a minute
        at least): its prompt, as it is, through the gate; a recurring one up to 3 times."""
        for _ in range(3):
            await asyncio.sleep(float(os.environ.get("FAKE_CLAUDE_FIRE_SECONDS") or 1))
            entry = self.schedules.get(key)
            if entry is None:
                return
            # As Claude: a firing waits for its session to be idle (taken after the turn).
            self.system("command_lifecycle", state="started", wakeup_source="schedule_wakeup")
            self.prompts.put_nowait({"content": entry["prompt"], "origin": None, "uuid": None})
            if not entry["recurring"]:
                self.schedules.pop(key, None)
                return

    async def inbox(self):
        """Other sessions' messages (FAKE_CLAUDE_INBOX: a file, one JSON line each, {name, pid,
        text}), as Claude gives them to the gate."""
        path = Path(os.environ.get("FAKE_CLAUDE_INBOX") or "")
        read = 0
        while path.name:
            await asyncio.sleep(0.1)
            lines = await asyncio.to_thread(
                lambda: path.read_text().splitlines() if path.is_file() else []
            )
            for line in lines[read:]:
                said = json.loads(line)
                prompt = (
                    f'<cross-session-message from="uds:/tmp/cc-socks/{said["pid"]}.sock" '
                    f'from-name="{said["name"]}" from-mode="prompting">\n{said["text"]}\n'
                    "</cross-session-message>"
                )
                entry = {"content": prompt, "origin": None, "uuid": None}
                if self.turning:
                    self.steering.append(entry)
                else:
                    self.prompts.put_nowait(entry)
            read = len(lines)

    async def subagent(self, call: str, data: dict) -> dict:
        """A subagent (Agent): in Anthropic's cloud (isolation remote), else here, in a worktree
        of its own (isolation worktree) or the worker's folder; told what SubagentStart
        answers; its calls asked about (PreToolUse, with its id and folder) and run; in the
        background when asked (its end then wakes the worker)."""
        kind = str(data.get("subagent_type") or "general-purpose")
        if data.get("isolation") == "remote":
            note("FAKE_CLAUDE_SUBAGENTS", {"kind": kind, "remote": True})
            structured = {"status": "remote_launched", "taskId": "r1"}
            return self.tool_result(call, "Remote agent launched.", False, structured)
        agent_id = f"a{uuid.uuid4().hex[:16]}"
        cwd = self.cwd
        if data.get("isolation") == "worktree":
            top = await self.git("rev-parse", "--show-toplevel")
            cwd = str(Path(top) / ".claude" / "worktrees" / f"agent-{agent_id[:8]}")
            await self.git("worktree", "add", "-q", "-b", f"worktree-agent-{agent_id[:8]}", cwd)
        background = bool(data.get("run_in_background"))
        self.system(
            "task_started", task_id=agent_id, tool_use_id=call, description=data.get("description"),
            subagent_type=kind, is_backgrounded=background, task_type="local_agent",
            prompt=data.get("prompt"),
        )  # fmt: skip
        answer = await self.hook(
            "SubagentStart", {"agent_id": agent_id, "agent_type": kind, "cwd": cwd}
        )
        told = ((answer or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""
        note("FAKE_CLAUDE_SUBAGENTS", {"kind": kind, "cwd": cwd, "told": told})
        context = {"agent_id": agent_id, "agent_type": kind, "cwd": cwd, "parent": call}

        async def work() -> str:
            said = "done"
            for block in self.brain(str(data.get("prompt") or "")):
                if block.get("type") != "tool_use":
                    said = block.get("text") or said
                    continue
                self.send(
                    {"type": "assistant", "parent_tool_use_id": call,
                     "session_id": self.session_id, "uuid": str(uuid.uuid4()),
                     "message": {"id": f"msg_sub_{uuid.uuid4().hex[:8]}", "role": "assistant",
                                 "model": self.model, "content": [block],
                                 "usage": {"input_tokens": 30, "output_tokens": 1}}}
                )  # fmt: skip
                await self.run(block, context)
                self.system(
                    "task_progress", task_id=agent_id, tool_use_id=call,
                    usage={"total_tokens": 120, "tool_uses": 1, "duration_ms": 5},
                )  # fmt: skip
            return said

        async def ending(said: str, status: str = "completed"):
            self.system(
                "task_notification", task_id=agent_id, tool_use_id=call, status=status,
                output_file="", summary=said, usage={"total_tokens": 150},
            )  # fmt: skip

        if not background:
            said = await work()
            await ending(said)
            structured = {"status": "completed", "agentId": agent_id, "content": said}
            return self.tool_result(call, said, False, structured)

        async def behind():
            try:
                said = await work()
            except asyncio.CancelledError:
                self.system("task_updated", task_id=agent_id, patch={"status": "killed"})
                return
            await ending(said)
            prompt = (
                f"<task-notification>\n<task-id>{agent_id}</task-id>\n<tool-use-id>{call}"
                f"</tool-use-id>\n<status>completed</status>\n<summary>{said}</summary>\n"
                "</task-notification>"
            )
            entry = {"content": prompt, "origin": {"kind": "task-notification"}, "uuid": None}
            if self.turning:
                self.steering.append(entry)
            else:
                self.prompts.put_nowait(entry)

        self.agents[agent_id] = asyncio.ensure_future(behind())
        structured = {"status": "async_launched", "agentId": agent_id}
        return self.tool_result(call, f"Async agent launched: {agent_id}", False, structured)

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
        self, call: str, text: str, error: bool, structured, meta: dict | None = None,
        parent: str | None = None,
    ) -> dict:  # fmt: skip
        block = {"type": "tool_result", "tool_use_id": call, "content": text, "is_error": error}
        self.send(
            {
                "type": "user",
                "message": {"role": "user", "content": [block]},
                "tool_use_result": structured,
                "tool_result_meta": [{"id": call, **(meta or {})}],
                "parent_tool_use_id": parent,
                "session_id": self.session_id,
                "uuid": str(uuid.uuid4()),
            }
        )
        return block

    async def sandboxed(self, call: str, command: str) -> dict | None:
        """Claude's sandbox, as 2.1.322's behaves for the commands Chief of Staff's check runs
        (a write outside its folders, a read of a file kept unreadable, a host asked about):
        the result where it refuses one, else None (the command runs)."""
        if not self.sandbox.get("enabled"):
            return None
        files = self.sandbox.get("filesystem") or {}
        network = self.sandbox.get("network") or {}
        words = shlex.split(command) if command else []

        def under(path: str, roots: list) -> bool:
            path = os.path.normpath(Path(self.cwd) / path)
            return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)

        refused = None
        if words[:1] == ["touch"] and not under(words[1], [self.cwd, *files.get("allowWrite", [])]):
            refused = f"touch: {words[1]}: Operation not permitted"
        if words[:1] == ["cat"] and under(words[1], files.get("denyRead", [])):
            refused = f"cat: {words[1]}: Operation not permitted"
        if words[:1] == ["curl"]:
            url = next(w for w in words if "://" in w)
            host = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
            allowed = network.get("allowedDomains")
            listed = allowed is None or any(host == d or host.endswith("." + d) for d in allowed)
            if not listed:
                reply = await self.ask(
                    {"subtype": "can_use_tool", "tool_name": "SandboxNetworkAccess",
                     "input": {"host": host}, "tool_use_id": call, "permission_suggestions": []}
                )  # fmt: skip
                listed = (reply.get("response") or {}).get("behavior") == "allow"
            if not listed:
                refused = f"curl: (56) CONNECT tunnel failed, response 403 ({host} blocked)"
            elif proxy := os.environ.get("HTTPS_PROXY"):
                # Through its proxy, to the one it was given: the check's catcher hears of it.
                address = proxy.split("://", 1)[1].rstrip("/")
                with contextlib.suppress(OSError):
                    reader, writer = await asyncio.open_connection(*address.rsplit(":", 1))
                    writer.write(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
                    await writer.drain()
                    await asyncio.wait_for(reader.read(100), 5)
                    writer.close()
                refused = "curl: (56) CONNECT tunnel failed, response 502"
        if refused is None:
            return None
        failed = f"Exit code 1\n{refused}"
        return self.tool_result(call, failed, True, f"Error: {failed}")

    async def bash(
        self, call: str, data: dict, cwd: str | None = None, parent: str | None = None
    ) -> dict:
        command = data.get("command") or ""
        if (refused := await self.sandboxed(call, command)) is not None:
            return refused
        if parent:  # A subagent's: run where it works, its result its own.
            process = await asyncio.create_subprocess_exec(
                "/bin/sh", "-c", command, cwd=cwd or self.cwd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )  # fmt: skip
            output, _ = await process.communicate()
            return self.tool_result(call, output.decode().strip() or "(no output)", False,
                                    {"stdout": output.decode()}, parent=parent)  # fmt: skip
        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            command,
            cwd=cwd or self.cwd,
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
        if delay := os.environ.get("FAKE_CLAUDE_EXIT_DELAY"):
            await asyncio.sleep(float(delay))
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
    inbox = asyncio.ensure_future(fake.inbox())
    fake.tasks.add(inbox)
    await fake.read()
    turns.cancel()


def main():
    argv = sys.argv[1:]
    note("FAKE_CLAUDE_RUNS", argv)
    note("FAKE_CLAUDE_CWDS", str(Path.cwd()))
    if argv[:1] == ["--version"]:
        sys.stdout.write(f"{VERSION} (Claude Code)\n")
        return
    if argv[:2] == ["auth", "status"]:
        # As 2.1.322 does where Claude was never set up: its state file and a backups folder.
        given = os.environ.get("CLAUDE_CONFIG_DIR")
        config = Path(given) if given else Path.home() / ".claude"
        state = config / ".claude.json" if given else Path.home() / ".claude.json"
        if not state.exists():
            (config / "backups").mkdir(parents=True, exist_ok=True)
            state.write_text("{}\n")
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
