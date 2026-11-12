"""What Chief of Staff checks of a Codex version before its workers run, once per version
(CodexBackend.check), on this machine and without a model call: a stand-in model that Codex
talks to as it would to OpenAI's (the Responses API, served on 127.0.0.1), whose one turn asks
for each kind of action a worker takes; and a stand-in MCP server
(`python -m chiefofstaff.codex_check --mcp FOLDER`) whose one tool writes a file.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# The names the check's model, tool and MCP server go by in Codex.
PROVIDER = "chiefofstaff_check"
MODEL = "chiefofstaff-check"
TOOL = "chiefofstaff_check"
SERVER = "chiefofstaff_check"
SERVER_TOOL = "write_marker"
# What each action would leave in the check's folder, had it run.
COMMAND_MARK = "command.marker"
SERVER_MARK = "server.marker"
# Codex lists an edit's files by path: the one refused (the last) comes last.
ADDED = ("added-1.marker", "added-2.marker")
MOVED_FROM, MOVED_TO = "from.txt", "moved.txt"
# What the turn asks for, as a model would (and as the tests' stand-in for Codex reads it).
PROMPT = (
    f"Call the {TOOL} tool with text 'check'. Run the shell command `touch {{work}}/"
    f"{COMMAND_MARK}`. Call the MCP tool {SERVER_TOOL} of {SERVER}. Write the file `{ADDED[0]}` "
    f"and the file `{ADDED[1]}`. Move the file `{MOVED_FROM}` to `{MOVED_TO}`. Then reply with "
    "just: checked"
)
# Its turn's last words: the check ran to its end only once they are said.
REPLY = "checked"
# Codex's tools for subagents (multi-agent v1's, in multi_agent_v1; v2's, in collaboration): a
# worker that asks first is offered none.
SUBAGENT_TOOLS = frozenset(
    {
        "spawn_agent", "send_input", "resume_agent", "wait_agent", "close_agent", "list_agents",
        "send_message", "followup_task", "interrupt_agent",
    }
)  # fmt: skip
SUBAGENT_NAMESPACES = ("multi_agent", "collaboration")


def subagent_tools(tools: Any) -> set[str]:
    """Which of a request's tools (Responses API, namespaces included) are Codex's subagents'."""
    found = set()
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            continue
        namespace = str(tool.get("name") or "") if tool.get("type") == "namespace" else ""
        for inner in (tool.get("tools") or [tool]) if namespace else [tool]:
            name = str(inner.get("name") or "") if isinstance(inner, dict) else ""
            if name in SUBAGENT_TOOLS or namespace.startswith(SUBAGENT_NAMESPACES):
                found.add(f"{namespace}::{name}" if namespace else name)
    return found


def catalog() -> dict:
    """The stand-in model as Codex's model catalog has one (model_catalog_json): its tools are
    a worker's (commands, edits as patches)."""
    return {
        "models": [
            {
                "slug": MODEL,
                "display_name": "Chief of Staff's check",
                "description": None,
                "base_instructions": "Do as asked.",
                "supported_reasoning_levels": [],
                "shell_type": "unified_exec",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 1,
                "upgrade": None,
                "model_messages": None,
                "default_reasoning_summary": "auto",
                "support_verbosity": False,
                "default_verbosity": None,
                "apply_patch_tool_type": "freeform",
                "truncation_policy": {"mode": "bytes", "limit": 10000},
                "supports_image_detail_original": False,
                "context_window": None,
                "auto_compact_token_limit": None,
                "effective_context_window_percent": 95,
                "experimental_supported_tools": [],
                # A model that asks for subagents (multi-agent v2, as OpenAI's do): the check's
                # thread, which asks first, is offered none all the same (agents.enabled).
                "multi_agent_version": "v2",
            }
        ]
    }


def steps(work: Path) -> list[dict]:
    """The model's answers, one per request: each action in turn, then its reply."""
    patch = "".join(f"*** Add File: {work / name}\n+x\n" for name in ADDED)
    move = f"*** Update File: {work / MOVED_FROM}\n*** Move to: {work / MOVED_TO}\n@@\n-a\n+b\n"
    calls = [
        {"type": "function_call", "name": TOOL, "arguments": json.dumps({"text": "check"})},
        {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": f"touch {work / COMMAND_MARK}"}),
        },
        {
            "type": "function_call",
            "namespace": f"mcp__{SERVER}",
            "name": SERVER_TOOL,
            "arguments": "{}",
        },
        {"type": "custom_tool_call", "name": "apply_patch", "input": wrapped(patch)},
        {"type": "custom_tool_call", "name": "apply_patch", "input": wrapped(move)},
    ]
    for index, call in enumerate(calls):
        call["call_id"] = f"check-{index}"
    reply = {
        "type": "message",
        "role": "assistant",
        "id": "check-reply",
        "content": [{"type": "output_text", "text": REPLY}],
    }
    return [*calls, reply]


def wrapped(patch: str) -> str:
    return f"*** Begin Patch\n{patch}*** End Patch\n"


def answered(body: dict) -> int:
    """How many of its actions the turn has had answered (each answer is in what Codex sends)."""
    return sum(
        1
        for item in body.get("input") or []
        if isinstance(item, dict) and str(item.get("type", "")).endswith("_call_output")
    )


def stream(number: int, item: dict) -> bytes:
    """One response as Server-Sent Events, as the Responses API streams it."""
    response = f"check-response-{number}"
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    events = [
        {"type": "response.created", "response": {"id": response}},
        {"type": "response.output_item.done", "item": item},
        {"type": "response.completed", "response": {"id": response, "usage": usage}},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class Model(ThreadingHTTPServer):
    """The stand-in model, on a free port of 127.0.0.1, until closed."""

    daemon_threads = True

    def __init__(self, work: Path):
        self.answers = steps(work)
        self.requests = 0
        self.subagents: set[str] = set()  # Subagent tools the thread was offered.
        super().__init__(("127.0.0.1", 0), Answer)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def arguments(self, folder: Path) -> list[str]:
        """The app-server's `-c` settings that make the stand-in its model."""
        file = folder / "catalog.json"
        file.write_text(json.dumps(catalog()))
        url = f"http://127.0.0.1:{self.server_address[1]}/v1"
        provider = (
            f'{{name="Chief of Staff check", base_url={json.dumps(url)}, wire_api="responses"}}'
        )
        return [
            "-c",
            f"model_provider={json.dumps(PROVIDER)}",
            "-c",
            f"model={json.dumps(MODEL)}",
            "-c",
            f"model_catalog_json={json.dumps(str(file))}",
            "-c",
            f"model_providers.{PROVIDER}={provider}",
        ]

    def close(self):
        self.shutdown()
        self.server_close()


class Answer(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        except ValueError:
            body = {}
        model = self.server
        if not isinstance(model, Model):
            return
        model.requests += 1
        model.subagents |= subagent_tools(body.get("tools"))
        answers = model.answers
        data = stream(model.requests, answers[min(answered(body), len(answers) - 1)])
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve_mcp(folder: Path):
    """The stand-in MCP server (stdio, one JSON-RPC message a line): its one tool, a write,
    leaves a file in `folder`."""
    tool = {
        "name": SERVER_TOOL,
        "description": "Write a file.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": False},
    }
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue  # A notification.
        method, params = message.get("method"), message.get("params") or {}
        if method == "initialize":
            result: dict = {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER, "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [tool]}
        elif method == "tools/call":
            (folder / SERVER_MARK).write_text("written\n")
            result = {"content": [{"type": "text", "text": "written"}]}
        elif method == "ping":
            result = {}
        else:
            answer = {"code": -32601, "message": f"{method} is not supported"}
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": answer}))
            sys.stdout.write("\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}))
        sys.stdout.write("\n")
        sys.stdout.flush()


if __name__ == "__main__":
    if sys.argv[1:2] == ["--mcp"] and len(sys.argv) == 3:
        serve_mcp(Path(sys.argv[2]))
