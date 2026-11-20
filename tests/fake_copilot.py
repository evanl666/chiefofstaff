"""A stand-in for the Copilot CLI as the SDK starts it (`copilot --headless ... --stdio`), for
tests of how Chief of Staff starts it with the real SDK: its handshake and status, over the SDK's
JSON-RPC on stdio. Like the npm loader, it runs a program of its own (a child that lives as long
as it does). `copilot --version` says its version.

FAKE_COPILOT_* variables shape it: VERSION, PROTOCOL (the one `connect` reports), STARTS (a file
each start with --stdio appends a line to), REQUESTS (a file each request is appended to, as a
JSON line: method and params), REFUSE (JSON: method -> the error message it answers with).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

VERSION = os.environ.get("FAKE_COPILOT_VERSION", "1.0.112")
PROTOCOL = int(os.environ.get("FAKE_COPILOT_PROTOCOL", "3"))
REFUSE = json.loads(os.environ.get("FAKE_COPILOT_REFUSE") or "{}")


def read() -> dict | None:
    header = sys.stdin.buffer.readline()
    if not header.startswith(b"Content-Length:"):
        return None
    sys.stdin.buffer.readline()
    return json.loads(sys.stdin.buffer.read(int(header.split(b":")[1])))


def send(message: dict):
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
    sys.stdout.buffer.flush()


ANSWERS = {
    "connect": {"ok": True, "protocolVersion": PROTOCOL, "version": VERSION},
    "ping": {"message": "pong", "timestamp": 0, "protocolVersion": PROTOCOL},
    "status.get": {"version": VERSION, "protocolVersion": PROTOCOL},
    "auth.getStatus": {"isAuthenticated": True, "authType": "user", "login": "acme-dev"},
    "runtime.shutdown": {},
}


def main():
    if "--version" in sys.argv:
        sys.stdout.write(f"GitHub Copilot CLI {VERSION}.\n")
        return
    if starts := os.environ.get("FAKE_COPILOT_STARTS"):
        with Path(starts).open("a") as file:
            file.write(f"{VERSION}\n")
    # Its own program, as the npm loader's: ends when its stdin (this one's pipe) closes.
    program = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    requests = os.environ.get("FAKE_COPILOT_REQUESTS")
    while (message := read()) is not None:
        if "id" not in message:
            continue
        method, params = message.get("method", ""), message.get("params") or {}
        if requests:
            with Path(requests).open("a") as file:
                file.write(json.dumps({"method": method, "params": params}) + "\n")
        if method in REFUSE:
            error = {"code": -32603, "message": REFUSE[method]}
            send({"jsonrpc": "2.0", "id": message["id"], "error": error})
        elif method in ("session.create", "session.resume"):
            answer = {"sessionId": params.get("sessionId"), "workspacePath": None}
            send({"jsonrpc": "2.0", "id": message["id"], "result": answer})
        elif method in ("session.options.update", "session.detach"):
            send({"jsonrpc": "2.0", "id": message["id"], "result": {"success": True}})
        elif (answer := ANSWERS.get(method)) is not None:
            send({"jsonrpc": "2.0", "id": message["id"], "result": answer})
        else:
            error = {"code": -32601, "message": f"Unhandled method {message.get('method')}"}
            send({"jsonrpc": "2.0", "id": message["id"], "error": error})
    program.stdin.close()  # type: ignore[union-attr]
    program.wait()


if __name__ == "__main__":
    main()
