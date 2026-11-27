"""Live: no message is lost to a restart (0.1.61). The owner sends the Router a message, and
Chief of Staff restarts 2 s later, as an update's restart does (stopped gently: SIGTERM, then
started again on the same data folder); the Router answers it once it is back, without the
owner sending it again.

On 0.1.60 the Router on Codex, restarted 3 s after the owner's message, waited out its whole
resume ("Resuming … took over 180 s: given up"), stayed "Thinking…" with no turn, and the
message was never answered.

  C1  the Router on Copilot (real): its turn cut short by the restart, it is given the message
      again once it is back (one line says so), and answers it; the owner's message handled,
      sent by the owner once; no error line; no "given up" in the log
  X1  the Router on Codex (the owner's own Codex and sign-in; one short question, twice at
      most: Codex's plan is shared): the same, its own Codex thread resumed (no fresh one), in
      seconds

A scenario whose answer came before the restart (a quick model) is tried again with another
question, restarting sooner (1 s, then 0.5 s). Copilot signs in with COPILOT_GITHUB_TOKEN, or the
gh CLI's token when that is not set, in a Copilot home of its own; the Router's model is
LIVE_RESTART_MODEL (LIVE_COPILOT_MODEL, else claude-haiku-5.5). Codex's model is its default, or
LIVE_CODEX_MODEL's; the owner's Codex files are recorded before and checked after
(scripts/owner_files.py). The stop ends the agents' processes Chief of Staff ran before it exits
(checked). Ports 21560-21561 (LIVE_RESTART_PORT moves them). Folders are made by `tempfile` (set
TMPDIR to keep them elsewhere). About 3 minutes a scenario.

    nice -n 15 uv run python scripts/live_restart.py [C1] [X1]
"""

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(1, str(Path(__file__).parent.parent / "tests"))
import live_e2e
from agent_paths import without_agents
from live_e2e import App, log, transcript, wait_for

from chiefofstaff.store import Store

BASE_PORT = int(os.environ.get("LIVE_RESTART_PORT", "21560"))
MODEL = os.environ.get(
    "LIVE_RESTART_MODEL", os.environ.get("LIVE_COPILOT_MODEL", "claude-haiku-5.5")
)
CODEX_MODEL = os.environ.get("LIVE_CODEX_MODEL", "")
OWNER_FILES = Path(__file__).parent / "owner_files.py"
# Questions the Router answers itself (no worker), long enough that its turn may still run 2 s
# after they are sent; each next try restarts sooner (a quick model answers in 2 s).
QUESTIONS = [
    (
        2.0,
        "Without starting a worker, in about 200 words: what does a chief of staff do for a "
        "busy engineer?",
    ),
    (1.0, "Without starting a worker, in about 200 words: why do teams write down decisions?"),
    (0.5, "Without starting a worker, in about 200 words: how should a team run a postmortem?"),
]
RESTARTED = "Chief of Staff restarted; asked the session to continue."


def restart_port(port: int):
    live_e2e.PORT = port
    live_e2e.BASE = f"http://127.0.0.1:{port}"


def agent_processes(root: int) -> dict[int, str]:
    """The agents' processes Chief of Staff (`root`) runs: each supervisor and what it runs."""
    listed = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, check=True
    ).stdout
    rows = {}
    for line in listed.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows[int(parts[0])] = (int(parts[1]), parts[2])
    found, frontier = set(), {root}
    while frontier:
        frontier = {pid for pid, (parent, _) in rows.items() if parent in frontier} - found
        found |= frontier
    agents = ("chiefofstaff.agentrun", "codex", "claude", "copilot")
    return {pid: rows[pid][1] for pid in found if any(word in rows[pid][1] for word in agents)}


def running(pids: dict[int, str]) -> list[int]:
    listed = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=True
    ).stdout
    now = {int(p): c for p, c in (line.split(None, 1) for line in listed.splitlines() if line)}
    return [pid for pid, command in pids.items() if now.get(pid) == command]


async def stop_gently(app: App) -> float:
    """Stopped as the owner's service manager (or an update) stops it: SIGTERM. Every agent's
    process it ran has ended by the time it has (else the next start may find its session
    held). How long it took."""
    began = time.monotonic()
    if app.http:
        await app.http.aclose()
    if app.process and app.process.poll() is None:
        theirs = agent_processes(app.process.pid)
        app.process.send_signal(signal.SIGTERM)
        await asyncio.to_thread(app.process.wait, 60)
        left = running(theirs)
        assert not left, f"agents' processes still running after the stop: {left}"
    return time.monotonic() - began


async def ready(app: App, name: str):
    async def up():
        return (await app.get("/api/state"))["runtime"]["state"] == "ready"

    await wait_for(f"{name}: Chief of Staff ready", up, 120, app)


async def router(app: App) -> dict:
    state = await app.get("/api/state")
    return next(s for s in state["sessions"] if s["id"] == state["router_id"])


async def messages(app: App, session: str, after: int = 0) -> list[dict]:
    return await app.get(f"/api/sessions/{session}/messages?after={after}")


async def restart_mid_turn(app: App, name: str, agent: str) -> str:
    """The owner's message, a restart 2 s later, and its answer after it; returns the answer."""
    tries = QUESTIONS[:2] if agent == "codex" else QUESTIONS  # (Codex's plan is shared.)
    for attempt, (delay, question) in enumerate(tries):
        before = await router(app)
        session = before["id"]
        mark = max((m["id"] for m in await messages(app, session)), default=0)
        await app.post(f"/api/sessions/{session}/messages", {"text": question})
        await asyncio.sleep(delay)
        said = [m for m in await messages(app, session, mark) if m["kind"] == "assistant"]
        took = await stop_gently(app)
        log(f"{name}: stopped in {took:.1f} s ({delay:g} s after the message), and its agents")
        if said:
            log(f"{name}: answered before the restart (a quick model): again, sooner")
            await app.start()
            await ready(app, name)
            if attempt == len(tries) - 1:
                raise AssertionError("the Router answered before every restart")
            continue
        began = time.monotonic()
        await app.start()
        await ready(app, name)
        shown = await router(app)
        assert shown["status"] != "working" or shown["live"], (
            "never working while it is not resumed",
            shown["status"],
        )

        async def answered(session=session, mark=mark):
            found = await messages(app, session, mark)
            answers = [m for m in found if m["kind"] == "assistant"]
            return found if answers and (await router(app))["status"] == "idle" else None

        found = await wait_for(f"{name}: the Router answers after the restart", answered, 300, app)
        took = time.monotonic() - began
        assert took < 150, f"resumed in seconds, not after a resume given up: {took:.0f} s"
        asked = [m for m in found if m["kind"] == "owner"]
        assert [m["text"] for m in asked] == [question], transcript(found)
        assert asked[0]["delivery"] == "processed", asked[0]["delivery"]
        notes = [m["text"] for m in found if m["kind"] == "notice" and m["text"] == RESTARTED]
        assert len(notes) == 1, transcript(found)
        errors = [m["text"] for m in found if m["kind"] == "error"]
        assert not errors, errors
        log_text = app.logfile.read_text(errors="replace")
        assert "given up" not in log_text, "no resume was given up"
        fresh = [m["text"] for m in found if "could not be resumed" in m["text"]]
        assert not fresh, f"its own session resumed, no fresh one: {fresh}"
        answer = "\n".join(m["text"] for m in found if m["kind"] == "assistant")
        log(f"{name}: answered {took:.0f} s after the restart: {answer[:120]!r}")
        return answer
    raise AssertionError("not tried")


def quiet(data: Path):
    """No desktop notification from this instance, from its first moment (never on the owner's
    screen): saved before it starts."""
    store = Store(data)
    try:
        settings = store.settings().model_copy(update={"mac_notifications": False})
        store.save_settings(settings.model_copy(update={"digest_time": ""}))
    finally:
        store.close()


def owner_files(action: str, record: Path):
    done = subprocess.run(
        [sys.executable, str(OWNER_FILES), action, str(record)], capture_output=True, text=True
    )
    if done.returncode:
        raise AssertionError(f"owner files: {done.stdout}{done.stderr}")


async def scenario(name: str, root: Path, token: str) -> str:
    agent = "codex" if name.startswith("X") else "copilot"
    restart_port(BASE_PORT + (1 if agent == "codex" else 0))
    home = root / name / "copilot-home"
    home.mkdir(parents=True)
    os.environ["COPILOT_HOME"] = str(home)  # (Chief of Staff's own Copilot, signed in by token.)
    data = live_e2e.data_dir(root / name)
    env = {"COPILOT_GITHUB_TOKEN": token}
    if agent == "codex":
        # The owner's Codex on the PATH, and not Claude Code (never run here).
        hidden = frozenset({"claude"})
        env["PATH"] = without_agents(os.environ.get("PATH", ""), root / name / "path", hidden)
    app = App(data, root / f"{name}.log", env, agents=agent != "copilot")
    quiet(data)
    try:
        await app.start()
        await ready(app, name)
        state = await app.get("/api/state")
        body = {**state["settings"], "role_models": {"router": MODEL, "worker": MODEL}}
        body |= {"mac_notifications": False, "digest_time": ""}
        if agent == "codex":
            on = {"enabled": True, **({"model": CODEX_MODEL} if CODEX_MODEL else {})}
            body["agents"] = {"codex": on, "roles": {"router": "codex"}}
        await app.post("/api/settings", body)
        if agent == "codex":

            async def on_codex():
                return (await router(app)).get("agent") == "codex"

            await wait_for(f"{name}: the Router on Codex", on_codex, 120, app)
        return await restart_mid_turn(app, name, agent)
    finally:
        await app.stop()


async def main():
    wanted = [name for name in sys.argv[1:] if name in {"C1", "X1"}] or ["C1", "X1"]
    token = (
        os.environ.get("COPILOT_GITHUB_TOKEN")
        or subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    results: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="cos-live-restart-") as temporary:
        root = Path(temporary).resolve()
        record = root / "owner-files.json"
        if "X1" in wanted:
            owner_files("record", record)
        for name in wanted:
            try:
                await scenario(name, root, token)
                results[name] = "passed"
            except Exception as exc:
                results[name] = f"FAILED: {exc}"
                print((root / f"{name}.log").read_text(errors="replace")[-6000:], file=sys.stderr)
        if "X1" in wanted:
            owner_files("check", record)
    for name, result in results.items():
        log(f"{name}: {result}")
    if all(result == "passed" for result in results.values()):
        log("RESTART SCENARIOS PASSED")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
