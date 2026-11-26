"""Live check of 0.1.59's hotfix on the real Copilot CLI: a worker whose Copilot session is very
large (one of the owner's logs was 808 MB). Copilot 1.0.115 and 1.0.116, asked for such a
session's whole history (session.getMessages), fail to write the answer (EINVAL on macOS) and
answer nothing more: 0.1.58 asked for it as it started, never became ready, and hung as it
stopped.

  B1  Chief of Staff starts on a data folder of its own with that worker, a message to it sent
      just before a stop (an interrupted send, which it checks at each start): its page answers
      within START_LIMIT seconds, Copilot is ready, and the worker's session is resumed with its
      send found in its log (it arrived), with no "transport error" in Copilot's log.
  B2  Chief of Staff stops within STOP_LIMIT seconds, and none of Copilot's processes are left.

No model call: nothing is sent to the session (its interrupted send arrived). The big session is
copied, never changed, from LIVE_BIG_SESSION (a folder of a Copilot session-state, its name the
session's id) into a Copilot home of the run's own, signed in with COPILOT_GITHUB_TOKEN; the run's
files are under LIVE_BIG_ROOT (a temporary folder by default), removed at the end. Its port is
LIVE_BIG_PORT (21310). LIVE_BIG_CODE runs another Chief of Staff's code (a checkout's src
folder): 0.1.58's fails B1.

    LIVE_BIG_SESSION=~/.copilot/session-state/<id> COPILOT_GITHUB_TOKEN=$(gh auth token) \\
        uv run python scripts/live_big_session.py
"""

import asyncio
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from chiefofstaff.backend import received_in_log
from chiefofstaff.delivery import digest
from chiefofstaff.processes import descendants, process_table, running
from chiefofstaff.store import Store

PORT = int(os.environ.get("LIVE_BIG_PORT", "21310"))
BASE = f"http://127.0.0.1:{PORT}"
START_LIMIT = 15.0
READY_LIMIT = 120.0
STOP_LIMIT = 20.0


def log(text: str):
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def last_user_message(events: Path) -> str:
    """The latest message the session received, from its log."""
    _, found = received_in_log(events, (0, []))
    if not found:
        raise SystemExit("The session has no message to check.")
    return found[-1]["data"]["content"]


def prepare(root: Path, source: Path) -> tuple[Path, Path, str, int]:
    """A Copilot home with the big session, and a data folder with a worker on it whose last
    send was cut short."""
    home = root / "copilot"
    session_id = source.name
    copy = home / "session-state" / session_id
    size = sum(each.stat().st_size for each in source.rglob("*") if each.is_file())
    log(f"Copying {source} ({size // 1_000_000} MB)…")
    shutil.copytree(source, copy, ignore=shutil.ignore_patterns("inuse.*.lock"))
    data = root / "data"
    data.mkdir(mode=0o700)
    work = root / "work"
    work.mkdir()
    store = Store(data)
    store.create_session(session_id, "worker", "A big session", "idle", cwd=str(work))
    store.set(f"created:{session_id}", "1")
    prompt = last_user_message(copy / "events.jsonl")
    message = store.add_message(session_id, "router", "Its last message", {"prompt": prompt})
    store.set(f"sent:{session_id}", str(message["id"] - 1))
    marker = {"digest": digest(prompt), "upto": message["id"]}
    store.set(f"sending:{session_id}", json.dumps(marker))
    store.close()
    return home, data, session_id, message["id"]


def kv(data: Path, key: str) -> str | None:
    database = next(data.glob("*.db"))
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


async def main() -> int:
    source = Path(os.environ.get("LIVE_BIG_SESSION", "")).expanduser()
    if not (source / "events.jsonl").is_file():
        raise SystemExit("Set LIVE_BIG_SESSION to a Copilot session's folder (one with a big log).")
    if not os.environ.get("COPILOT_GITHUB_TOKEN"):
        raise SystemExit("Set COPILOT_GITHUB_TOKEN (gh auth token).")
    made = not os.environ.get("LIVE_BIG_ROOT")
    root = Path(os.environ.get("LIVE_BIG_ROOT") or tempfile.mkdtemp(prefix="live-big-"))
    root.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    process = None
    try:
        home, data, session_id, upto = prepare(root, source)
        env = {**os.environ, "COPILOT_HOME": str(home), "TMPDIR": str(root)}
        if code := os.environ.get("LIVE_BIG_CODE"):
            env["PYTHONPATH"] = code
        launched = time.monotonic()
        process = subprocess.Popen(
            [sys.executable, "-m", "chiefofstaff", "--no-browser", "--port", str(PORT),
             "--data-dir", str(data)],
            stdout=subprocess.PIPE, stderr=(root / "chiefofstaff.log").open("a"), env=env,
            text=True,
        )  # fmt: skip
        token = ""
        while not token and process.poll() is None:
            line = await asyncio.to_thread(process.stdout.readline)  # type: ignore[union-attr]
            if "#token=" in line:
                token = line.split("#token=", 1)[1].strip()
        async with httpx.AsyncClient(base_url=BASE, headers={"origin": BASE}, timeout=5) as http:
            answered = None
            while time.monotonic() - launched < 60:
                try:
                    if (await http.get("/api/health")).status_code == 200:
                        answered = time.monotonic() - launched
                        break
                except httpx.TransportError:
                    await asyncio.sleep(0.2)
            log(f"B1 page answered after {answered:.1f} s" if answered else "B1 page: no answer")
            if answered is None or answered > START_LIMIT:
                failures.append(f"B1: the page did not answer within {START_LIMIT:.0f} s")
            else:
                await http.post("/api/auth", json={"token": token})
                http.headers["X-Chief-Key"] = token
                state: dict = {}
                while time.monotonic() - launched < READY_LIMIT:
                    state = (await http.get("/api/state")).json()
                    if (state.get("runtime") or {}).get("state") == "ready":
                        break
                    await asyncio.sleep(1)
                status = state.get("runtime") or {}
                log(f"B1 Copilot {status.get('state')} after {time.monotonic() - launched:.1f} s")
                if status.get("state") != "ready":
                    failures.append(f"B1: Copilot not ready ({status})")
        cli = []
        if process.poll() is None:
            table = process_table()
            cli = sorted(descendants(table, process.pid) - {process.pid})
            log(f"B2 stopping (Copilot's processes: {cli})")
            stopped = time.monotonic()
            process.send_signal(signal.SIGINT)
            try:
                await asyncio.to_thread(process.wait, STOP_LIMIT + 30)
            except subprocess.TimeoutExpired:
                process.kill()
            took = time.monotonic() - stopped
            log(f"B2 stopped in {took:.1f} s")
            if took > STOP_LIMIT:
                failures.append(f"B2: stopping took {took:.0f} s")
            if left := [pid for pid in cli if running(pid)]:
                failures.append(f"B2: Copilot's processes left: {left}")
                for pid in left:
                    os.kill(pid, signal.SIGKILL)
        if process.poll() is not None:
            checked = kv(data, f"sending:{session_id}") is None
            arrived = kv(data, f"sent:{session_id}") == str(upto)
            log(f"B1 its interrupted send: {'checked' if checked else 'not checked'}, "
                f"{'arrived' if arrived else 'not found'}")  # fmt: skip
            if not (checked and arrived):
                failures.append("B1: its interrupted send was not found in its session")
        logs = list((home / "logs").glob("*.log"))
        broken = any("JSON-RPC transport error" in f.read_text(errors="replace") for f in logs)
        log(f"Copilot's log: {'a transport error' if broken else 'no transport error'}")
        if broken:
            failures.append("B1: Copilot's log has a transport error")
    finally:
        if process and process.poll() is None:
            process.kill()
        if made:
            shutil.rmtree(root, ignore_errors=True)
    for failure in failures:
        log(f"FAIL {failure}")
    log("PASS" if not failures else f"{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
