"""Live test of the 0.1.50 fix: workers when the owner's Copilot is in experimental mode, on the
Copilot that runs when the owner's CLI is newer than Chief of Staff supports (1.0.112 for a
1.0.114 CLI).

Experimental mode ("experimental": true in Copilot's settings.json, as `/experimental on` leaves
it) turns on Copilot's extensions, which every worker asks for (config discovery). Copilot
1.0.112 runs none itself, and 0.1.48 had each worker's create and resume refused: "Could not
deliver the message: JSON-RPC Error -32603: Request session.resume failed with message: Failed
to initialize standalone session extensions: extensionSdkPath is required when standalone
extensions are requested", under the owner's answer to a worker's question.

  X1  The Router starts a worker, which asks the owner a question and waits (0.1.48: it never
      started).
  X2  Chief of Staff starts again; the owner answers the question on its card: the worker's own
      Copilot session resumes (no fresh one needed), it gets the answer once, and reports done.
      No error line, no Copilot error text anywhere.

A Copilot home of its own (experimental mode on, a user extension in it); the owner's
~/.copilot is not used or changed. Signs in with COPILOT_GITHUB_TOKEN, or the gh CLI's token
when that is not set. Consumes a few premium requests (LIVE_RESUME_MODEL, the workers' model:
LIVE_COPILOT_MODEL, else claude-haiku-5.5) and takes about 5 minutes. Its port is
LIVE_RESUME_PORT (20860). LIVE_RESUME_CLI names the Copilot to run instead of the installed one
(Settings → Copilot command): Copilot's own 1.0.112 runtime, say, where the installed CLI is not
refused.

    uv run python scripts/live_resume.py
    LIVE_RESUME_CLI=~/Library/Caches/github-copilot-sdk/cli/1.0.112/prebuilds/darwin-arm64/\
copilot-runtime uv run python scripts/live_resume.py
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e

live_e2e.PORT = int(os.environ.get("LIVE_RESUME_PORT", "20860"))
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from live_e2e import App, log, transcript, wait_for  # noqa: E402

MODEL = os.environ.get(
    "LIVE_RESUME_MODEL", os.environ.get("LIVE_COPILOT_MODEL", "claude-haiku-5.5")
)
CLI = str(Path(given).expanduser()) if (given := os.environ.get("LIVE_RESUME_CLI")) else ""
EXTENSION = 'import { joinSession } from "@github/copilot-sdk/extension";\nawait joinSession({});\n'
# What 0.1.48 showed, and what no line may show now.
RAW = ("JSON-RPC", "extensionSdkPath", "standalone")


def copilot_home(root: Path) -> Path:
    home = root / "copilot-home"
    (home / "extensions" / "probe").mkdir(parents=True)
    (home / "extensions" / "probe" / "extension.mjs").write_text(EXTENSION)
    (home / "settings.json").write_text('{"experimental": true}\n')
    return home


def clean(messages: list[dict]):
    troubles = [m["text"] for m in messages if m["kind"] == "error"]
    troubles += [m["text"] for m in messages if any(word in m["text"] for word in RAW)]
    assert not troubles, troubles


async def scenario_ask(app: App) -> tuple[str, dict]:
    """X1: a worker starts in experimental mode, asks the owner, and waits (or, the model
    skipping the question, reports: it ran all the same)."""
    router = (await app.get("/api/state"))["router_id"]
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": "Start a worker for this, and tell it exactly: ask me, the owner, with "
            "report status question: 'Retry the probe access now?', then end your turn and "
            "wait for my answer. When I answer yes, report done with the word retried. "
            "Nothing else."
        },
    )

    async def started():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        found = [m["meta"]["session_id"] for m in messages
                 if m["kind"] == "notice" and m["text"].startswith("Started")]  # fmt: skip
        return found[0] if found else None

    worker = await wait_for("the Router starts a worker", started, 300, app)

    async def asked():
        tasks = [t for t in (await app.get("/api/state"))["tasks"] if t["session_id"] == worker]
        return next((t for t in tasks if t["status"] in {"waiting", "review", "done"}), None)

    task = await wait_for("it asks the owner and waits", asked, 600, app)
    clean(await app.get(f"/api/sessions/{worker}/messages"))
    log(f"X1: the worker started: {task['title']!r} ({task['status']})")
    return worker, task


async def scenario_answer(app: App, worker: str, task: dict):
    """X2: after a restart, the owner's answer reaches the worker's own session once."""
    await app.stop()
    await app.start()
    state = await app.get("/api/state")
    assert state["runtime"]["state"] == "ready", state["runtime"]
    before = len([m for m in await app.get(f"/api/sessions/{worker}/messages")
                  if m["kind"] == "report"])  # fmt: skip
    if task["status"] == "waiting":  # Its question's card.
        await app.post(f"/api/questions/{worker}/answer", {"text": "Yes, retry it."})
    else:  # A follow-up on its card: the same resume.
        await app.post(f"/api/tasks/{task['id']}/follow-up", {"text": "Yes, retry it."})

    async def reported():
        tasks = [t for t in (await app.get("/api/state"))["tasks"] if t["id"] == task["id"]]
        reports = [m for m in await app.get(f"/api/sessions/{worker}/messages")
                   if m["kind"] == "report"]  # fmt: skip
        done = tasks and tasks[0]["status"] in {"review", "done"}
        return tasks[0] if done and len(reports) > before else None

    await wait_for("it gets the answer and reports done", reported, 600, app)
    messages = await app.get(f"/api/sessions/{worker}/messages")
    clean(messages)
    answers = [m for m in messages if m["kind"] == "owner" and "Yes, retry it." in m["text"]]
    assert len(answers) == 1, transcript(messages)
    fresh = [m for m in messages if "Continued in a fresh Copilot session" in m["text"]]
    assert not fresh, "its own session resumed: no fresh one needed"
    log("X2: resumed after a restart, the answer arrived once, done; no error")


async def main():
    token = (
        os.environ.get("COPILOT_GITHUB_TOKEN")
        or subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    with tempfile.TemporaryDirectory(prefix="cos-live-resume-") as temporary:
        root = Path(temporary).resolve()
        home = copilot_home(root)
        os.environ["COPILOT_HOME"] = str(home)  # (This process's: Chief of Staff's and cleanup's.)
        data = live_e2e.data_dir(root)
        app = App(data, root / "chiefofstaff.log", {"COPILOT_GITHUB_TOKEN": token})
        failed = False
        try:
            await app.start()
            state = await app.get("/api/state")
            assert state["runtime"]["state"] == "ready", state["runtime"]
            log(f"connected: {state['runtime'].get('warning') or 'the owner CLI'}")
            settings = {
                "role_models": {"worker": MODEL},
                "max_workers": 1,
                "mac_notifications": False,
                "digest_time": "",
            }
            await app.post("/api/settings", settings | ({"copilot_command": CLI} if CLI else {}))
            if CLI:
                await app.stop()
                await app.start()
                state = await app.get("/api/state")
                assert state["runtime"]["state"] == "ready", state["runtime"]
                log(f"connected to {CLI}: {state['runtime'].get('warning') or 'as given'}")
            worker, task = await scenario_ask(app)
            await scenario_answer(app, worker, task)
            log("RESUME SCENARIOS PASSED")
        except Exception:
            failed = True
            raise
        finally:
            await app.stop()
            if failed:
                print((root / "chiefofstaff.log").read_text()[-6000:], file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
