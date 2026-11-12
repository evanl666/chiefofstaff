"""A stand-in model for the real Codex CLI in tests (CHIEFOFSTAFF_TEST_CODEX=1): Codex talks to
it as to OpenAI's Responses API, on 127.0.0.1, with a CODEX_HOME of the test's own, so the
contract tests run real Codex's app-server, approvals, commands, child agents and resumes
without a sign-in or a model call, and leave the owner's ~/.codex alone.

It understands the prompts the tests send (as contract_brain does for the scripted backends)
and answers each request from what Codex sent: the last message it was given, and the calls
answered since.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODEL, OTHER = "contract-model", "contract-mini"


def catalog() -> dict:
    efforts = [{"effort": e, "description": e} for e in ("low", "medium", "high")]
    return {
        "models": [
            {
                "slug": slug,
                "display_name": slug,
                "description": None,
                "base_instructions": "Do as asked.",
                "supported_reasoning_levels": efforts,
                "default_reasoning_level": "low",
                "shell_type": "unified_exec",
                "visibility": "list",
                "supported_in_api": True,
                "priority": priority,
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
            }
            for slug, priority in ((MODEL, 1), (OTHER, 2))
        ]
    }


def text_of(item: dict) -> str:
    return "\n".join(
        str(part.get("text") or "")
        for part in item.get("content") or []
        if isinstance(part, dict) and part.get("type") in {"input_text", "output_text"}
    )


def given(items: list[dict]) -> int:
    """Where the message it now works on is: the last user message that is not Codex's own
    context (those start with a tag) nor a note Chief of Staff steered in."""
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        mine = item.get("type") == "message" and item.get("role") == "user"
        if mine and not text_of(item).lstrip().startswith("<"):
            return index
    return -1


def call(kind: str, name: str, payload: Any, namespace: str = "") -> dict:
    item: dict[str, Any] = {"type": kind, "name": name}
    if kind == "custom_tool_call":
        item["input"] = payload
    else:
        item["arguments"] = json.dumps(payload)
    if namespace:
        item["namespace"] = namespace
    return item


def plan(prompt: str) -> list[dict]:
    """The calls a prompt asks for, in order."""
    steps = []
    for found in re.finditer(r"Call the (\w+) tool with text '([^']*)'", prompt):
        steps.append(call("function_call", found.group(1), {"text": found.group(2)}))
    for found in re.finditer(r"Run the shell command `([^`]+)`", prompt):
        steps.append(
            call("function_call", "exec_command", {"cmd": found.group(1), "yield_time_ms": 30000})
        )
    for found in re.finditer(r"Leave the command `([^`]+)` running", prompt):
        # Not waited for: Codex leaves it running as a terminal of its thread.
        steps.append(
            call("function_call", "exec_command", {"cmd": found.group(1), "yield_time_ms": 250})
        )
    for found in re.finditer(r"Write the file `([^`]+)` and the file `([^`]+)`", prompt):
        added = "".join(f"*** Add File: {path}\n+x\n" for path in found.groups())
        steps.append(
            call("custom_tool_call", "apply_patch", f"*** Begin Patch\n{added}*** End Patch\n")
        )
    for found in re.finditer(r"call the MCP tool (\w+) of (\w+(?:\.\w+)*)", prompt, re.IGNORECASE):
        # As Codex names a server's tools for the model: a dot in its name is not allowed.
        namespace = "mcp__" + found.group(2).replace(".", "_")
        steps.append(call("function_call", found.group(1), {}, namespace))
    for found in re.finditer(r"Send the text '([^']*)' to the agent ([0-9a-f-]+)", prompt):
        # Another thread, named by its id (as a worker could read it from Codex's files).
        message = {"target": found.group(2), "message": found.group(1)}
        steps.append(call("function_call", "send_input", message, "multi_agent_v1"))
    for found in re.finditer(r"Resume the agent ([0-9a-f-]+)", prompt):
        steps.append(
            call("function_call", "resume_agent", {"id": found.group(1)}, "multi_agent_v1")
        )
    if found := re.search(
        r"Have a child agent call the (\w+) tool with text '([^']*)'(?: and run `([^`]+)`)?", prompt
    ):
        task = f"Call the {found.group(1)} tool with text '{found.group(2)}'."
        if found.group(3):
            task += f" Run the shell command `{found.group(3)}`."
        steps.append(
            call(
                "function_call",
                "spawn_agent",
                {"message": f"{task} Then reply with just: done", "fork_context": True},
                "multi_agent_v1",
            )
        )
        steps.append({"wait": True})
    return steps


def reply(prompt: str, items: list[dict]) -> str:
    if "What word did I ask you to remember" in prompt:
        said = [re.search(r"Remember the word '(\w+)'", text_of(item)) for item in items]
        words = [found.group(1) for found in said if found]
        return words[-1] if words else "none"
    if "What colour is this image" in prompt:
        images = [
            part
            for item in items
            for part in item.get("content") or []
            if isinstance(part, dict) and part.get("type") == "input_image"
        ]
        return "red" if images else "unknown"
    if found := re.search(r"reply with just: (\w+)", prompt, re.IGNORECASE):
        return found.group(1)
    return "done"


def answer(body: dict) -> dict:
    """The next output item for this request."""
    items = [item for item in body.get("input") or [] if isinstance(item, dict)]
    if not body.get("tools"):
        return message("ok")  # Not a turn of its own (a title, a summary).
    start = given(items)
    prompt = text_of(items[start]) if start >= 0 else ""
    since = items[start + 1 :]
    made = [item for item in since if item.get("type") in {"function_call", "custom_tool_call"}]
    steps = plan(prompt)
    if len(made) < len(steps):
        step = dict(steps[len(made)])
        if step.get("wait"):
            spawned = next(
                (item for item in since if item.get("type") == "function_call_output"
                 and "agent_id" in str(item.get("output"))),
                {},
            )  # fmt: skip
            found = re.search(r'"agent_id"\s*:\s*"([^"]+)"', str(spawned.get("output")))
            step = call(
                "function_call",
                "wait_agent",
                {"targets": [found.group(1)] if found else [], "timeout_ms": 120000},
                "multi_agent_v1",
            )
        step["call_id"] = f"call-{start}-{len(made)}"
        return step
    return message(reply(prompt, items))


def message(text: str) -> dict:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


class Model(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.requests = 0
        self.bodies: list[dict] = []  # What Codex sent, in order.
        super().__init__(("127.0.0.1", 0), Answer)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def arguments(self, folder: Path) -> list[str]:
        """The app-server's `-c` settings that make this its model."""
        file = folder / "contract-models.json"
        file.write_text(json.dumps(catalog()))
        url = f"http://127.0.0.1:{self.server_address[1]}/v1"
        return [
            "-c",
            'model_provider="contract"',
            "-c",
            f'model="{MODEL}"',
            "-c",
            f"model_catalog_json={json.dumps(str(file))}",
            "-c",
            f'model_providers.contract={{name="contract", base_url={json.dumps(url)}, '
            'wire_api="responses"}',
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
        model.bodies.append(body)
        number = model.requests
        item = answer(body)
        if item.get("type") == "message":
            item["id"] = f"message-{number}"
        usage = {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 50},
            "output_tokens": 10,
            "output_tokens_details": None,
            "total_tokens": 110,
        }
        events = [
            {"type": "response.created", "response": {"id": f"response-{number}"}},
            {"type": "response.output_item.done", "item": item},
            {
                "type": "response.completed",
                "response": {"id": f"response-{number}", "usage": usage},
            },
        ]
        data = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
