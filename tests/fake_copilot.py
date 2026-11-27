"""A stand-in for the Copilot CLI as the SDK starts it (`copilot --headless ... --stdio`), for
tests of how Chief of Staff starts it with the real SDK: its handshake and status, over the SDK's
JSON-RPC on stdio. Like the npm loader, it runs a program of its own (a child that lives as long
as it does). `copilot --version` says its version.

FAKE_COPILOT_* variables shape it: VERSION, PROTOCOL (the one `connect` reports), STARTS (a file
each start with --stdio appends a line to), REQUESTS (a file each request is appended to, as a
JSON line: method and params), REFUSE (JSON: method -> the error message it answers with), DEAF
(a method: once asked it, it answers nothing more and runs on, as Copilot 1.0.116 did after a write
of an answer too large for its pipe failed), STUBBORN (1: it ignores SIGTERM).

Its own commands Chief of Staff runs for the owner (agentsetup.py), its state in FAKE_COPILOT_HOME:
`copilot login --device-code` (its page and one-time code; signed in once the owner approves:
HOME/approve), `copilot update` (to UPDATE_TO; UPDATE_FAILS: its error), `copilot sandbox ca
status|create|trust|rotate` (CA: the status's exit code to start with; TRUST_FAILS: trust's error).
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("FAKE_COPILOT_HOME") or ".")
VERSION = (
    (HOME / "updated").read_text().strip()
    if (HOME / "updated").is_file()
    else os.environ.get("FAKE_COPILOT_VERSION", "1.0.112")
)
PROTOCOL = int(os.environ.get("FAKE_COPILOT_PROTOCOL", "3"))
REFUSE = json.loads(os.environ.get("FAKE_COPILOT_REFUSE") or "{}")
DEAF = os.environ.get("FAKE_COPILOT_DEAF", "")


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


def runs(arguments: list[str]):
    if ran := os.environ.get("FAKE_COPILOT_RUNS"):
        with Path(ran).open("a") as file:
            file.write(" ".join(arguments) + "\n")


def login():
    sys.stdout.write(
        "To authenticate, visit https://github.com/login/device and enter code ACME-1234\n"
        "Waiting for authorization...\n"
    )
    sys.stdout.flush()
    approve = HOME / "approve"
    while not approve.exists():
        time.sleep(0.05)
    said = approve.read_text().strip() or "ok"
    approve.unlink()
    if said != "ok":
        sys.stderr.write(f"Error: {said}\n")
        sys.exit(1)
    (HOME / "signed-in").write_text("acme")
    sys.stdout.write("Signed in successfully as acme.\n")


def update():
    if failed := os.environ.get("FAKE_COPILOT_UPDATE_FAILS"):
        sys.stderr.write(f"Error: {failed}\n")
        sys.exit(1)
    newest = os.environ.get("FAKE_COPILOT_UPDATE_TO", "1.0.116")
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "updated").write_text(newest)
    sys.stdout.write(f"Updated GitHub Copilot CLI to {newest}.\n")


def sandbox_ca(command: str):
    """`copilot sandbox ca …` as 1.0.116 says it: status's exit code (0 trusted, 3 not trusted,
    5 rotate first), create (safe to repeat), trust, rotate."""
    state = HOME / "ca"
    found = state.read_text().strip() if state.is_file() else os.environ.get("FAKE_COPILOT_CA", "3")
    if command == "status":
        sys.stdout.write("Certificate trusted.\n" if found == "0" else "Certificate not trusted.\n")
        sys.exit(int(found))
    HOME.mkdir(parents=True, exist_ok=True)
    if command in {"create", "rotate"}:
        sys.stdout.write(f"Certificate written to {HOME / 'ca.pem'}\n")
        state.write_text("3")
    elif command == "trust":
        if failed := os.environ.get("FAKE_COPILOT_TRUST_FAILS"):
            sys.stderr.write(f"Error: {failed}\n")
            sys.exit(1)
        if found == "5":
            sys.stderr.write("Error: rotate the certificate first.\n")
            sys.exit(5)
        state.write_text("0")
        sys.stdout.write("Certificate trusted in your login keychain.\n")


def main():
    if "--version" in sys.argv:
        sys.stdout.write(f"GitHub Copilot CLI {VERSION}.\n")
        return
    runs(sys.argv[1:])
    if sys.argv[1:2] == ["login"]:
        login()
        return
    if sys.argv[1:2] == ["update"]:
        update()
        return
    if sys.argv[1:3] == ["sandbox", "ca"]:
        sandbox_ca(sys.argv[3])
        return
    if os.environ.get("FAKE_COPILOT_STUBBORN") == "1":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if starts := os.environ.get("FAKE_COPILOT_STARTS"):
        with Path(starts).open("a") as file:
            file.write(f"{VERSION}\n")
    # Its own program, as the npm loader's: ends when its stdin (this one's pipe) closes.
    program = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    requests = os.environ.get("FAKE_COPILOT_REQUESTS")
    deaf = False
    while (message := read()) is not None:
        if "id" not in message:
            continue
        method, params = message.get("method", ""), message.get("params") or {}
        if requests:
            with Path(requests).open("a") as file:
                file.write(json.dumps({"method": method, "params": params}) + "\n")
        deaf = deaf or (bool(DEAF) and method == DEAF)
        if deaf:
            continue
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
