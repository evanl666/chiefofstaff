"""A stand-in for Anthropic's Messages API on 127.0.0.1, for the contract tests on the real Claude
Code CLI (CHIEFOFSTAFF_TEST_CLAUDE=1): a tiny scripted model that understands the prompts the
contract tests send, answering as the API streams (chiefofstaff.claude_check builds the events).
No sign-in and no model call off this machine: the CLI runs with a made-up key, a Claude
configuration folder of the test's own, and every proxy pointing at a listener that drops what it
gets (Catcher), which the test then finds empty."""

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from chiefofstaff.claude_check import events, texts, usage_of


def latest(body: dict) -> tuple[str, int, str]:
    """The latest prompt of Chief of Staff's (not a call's result), how many calls were answered
    since, and the text that came after it (a note steered into the turn)."""
    prompt, answered, after = "", 0, ""
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        joined = "\n".join(texts(content))
        # Claude's own reminders come in the prompt's message, before it.
        said = re.sub(r"<system-reminder>.*?</system-reminder>", "", joined, flags=re.DOTALL)
        if message.get("role") == "user" and not results and said.strip():
            prompt, answered, after = said.strip(), 0, ""
            continue
        if message.get("role") != "assistant":
            answered += len(results)
            after += "\n" + joined
    return prompt, answered, after


def remembered(body: dict) -> str:
    for message in body.get("messages") or []:
        for text in texts(message.get("content") if isinstance(message, dict) else None):
            if found := re.search(r"Remember the word '(\w+)'", text):
                return found.group(1)
    return ""


def call(name: str, data: dict) -> dict:
    return {"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:12]}", "name": name, "input": data}


def answer(body: dict) -> tuple[list[dict], str]:
    """The model's next step for the contract tests' prompts."""
    prompt, answered, after = latest(body)
    plan = []
    if found := re.search(r"Call the (\w+) tool with text '([^']*)'", prompt):
        plan.append(call(f"mcp__chief__{found.group(1)}", {"text": found.group(2)}))
    if found := re.search(r"Run the shell command `([^`]+)`", prompt):
        plan.append(call("Bash", {"command": found.group(1), "description": "a command"}))
    if found := re.search(r"Leave the command `([^`]+)` running for ([\d.]+) s", prompt):
        command = f"sleep {found.group(2)}; {found.group(1)}; exit 3"
        plan.append(
            call(
                "Bash",
                {"command": command, "description": "left running", "run_in_background": True},
            )
        )
    if found := re.search(r"Fill the form of (\w+)", prompt):
        plan.append(call(f"mcp__{found.group(1)}__form", {}))
    if answered < len(plan):
        return [plan[answered]], "tool_use"
    if found := re.search(r"reply with just: (\w+)", after, re.IGNORECASE):
        return [{"type": "text", "text": found.group(1)}], "end_turn"
    if "What word did I ask you to remember" in prompt:
        return [{"type": "text", "text": remembered(body)}], "end_turn"
    if prompt.startswith("<task-notification>"):
        return [{"type": "text", "text": "noted"}], "end_turn"
    if found := re.search(r"reply with just: (\w+)", prompt, re.IGNORECASE):
        return [{"type": "text", "text": found.group(1)}], "end_turn"
    return [{"type": "text", "text": "done"}], "end_turn"


class Model(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.lock = threading.Lock()
        self.number = 0
        self.bodies: list[dict] = []
        super().__init__(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def close(self):
        self.shutdown()
        self.server_close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return None

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        server = self.server
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        if "count_tokens" in self.path:
            data = json.dumps({"input_tokens": 10}).encode()
            kind = "application/json"
        else:
            with server.lock:
                server.number += 1
                number = server.number
                server.bodies.append(body)
            tools = [t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)]
            if "Bash" in tools:
                blocks, stop = answer(body)
            else:  # A side request (a title): words.
                blocks, stop = [{"type": "text", "text": "contract"}], "end_turn"
            model = str(body.get("model") or "contract")
            if body.get("stream"):
                data = "".join(
                    f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
                    for e in events(number, model, blocks, stop)
                ).encode()
                kind = "text/event-stream"
            else:
                message = {
                    "id": f"msg_{number}",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": blocks,
                    "stop_reason": stop,
                    "stop_sequence": None,
                    "usage": usage_of(number),
                }
                data, kind = json.dumps(message).encode(), "application/json"
        self.send_response(200)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
