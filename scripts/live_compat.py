"""Live test of sdk-compat: Copilot versions never stop Chief of Staff, on real Copilot.

  V1  Chief of Staff starts on the owner's own Copilot CLI (1.0.113 and later speak protocol 4,
      the SDK 3: spoken through compat.adapt; 0.1.46-0.1.48 ran Copilot 1.0.112 in its place),
      with no version line, and sets up Copilot 1.0.112 ahead, in the background, into a cache
      of the run's own. The Router starts a worker, which answers.
  V2  The owner's CLI "updates itself" past the SDK while Chief of Staff runs (Settings' Copilot
      command becomes a stand-in that says 1.0.130 and speaks protocol 5): Copilot connects
      again, on the 1.0.112 set up ahead (its program runs from that cache), one quiet line says
      so; the same worker, its session made by the owner's CLI, resumes there and answers.
  V3  Back on the owner's CLI: the line is gone; the worker, its session last run by 1.0.112,
      resumes on the owner's CLI and answers. No error line, no Copilot error text anywhere.

A Copilot home of its own; the owner's ~/.copilot is not used or changed. Signs in with
COPILOT_GITHUB_TOKEN, or the gh CLI's token when that is not set. Downloads Copilot 1.0.112 once
(about 100 MB, into the run's own folder), consumes a few premium requests (LIVE_COMPAT_MODEL:
claude-haiku-5.5 by default) and takes about 5 minutes. Its port is LIVE_COMPAT_PORT (21160).

    uv run python scripts/live_compat.py
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e

live_e2e.PORT = int(os.environ.get("LIVE_COMPAT_PORT", "21160"))
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from live_e2e import App, log, transcript, wait_for  # noqa: E402

MODEL = os.environ.get("LIVE_COMPAT_MODEL", "claude-haiku-5.5")
FALLBACK_LINE = (
    "Your Copilot CLI (1.0.130) is newer than Chief of Staff supports; using Copilot 1.0.112 "
    "for now."
)
RAW = ("JSON-RPC", "protocol version", "Traceback", "Unhandled method")
STAND_IN = Path(__file__).resolve().parent.parent / "tests" / "fake_copilot.py"


def updated_cli(folder: Path) -> Path:
    """The owner's CLI as if it had updated itself past the SDK: it says 1.0.130 and speaks
    protocol 5 to the SDK (the stand-in); anything else it runs as the real CLI does."""
    real = shutil.which("copilot")
    assert real, "the Copilot CLI is not installed"
    script = folder / "copilot"
    script.write_text(
        "#!/bin/sh\n"
        'case " $* " in\n'
        '  *" --version "*) echo "GitHub Copilot CLI 1.0.130."; exit 0 ;;\n'
        '  *" --headless "*) FAKE_COPILOT_VERSION=1.0.130 FAKE_COPILOT_PROTOCOL=5 '
        f'exec "{sys.executable}" "{STAND_IN}" "$@" ;;\n'
        "esac\n"
        f'exec "{real}" "$@"\n'
    )
    script.chmod(0o755)
    return script


def clean(messages: list[dict]):
    troubles = [m["text"] for m in messages if m["kind"] == "error"]
    troubles += [m["text"] for m in messages if any(word in m["text"] for word in RAW)]
    assert not troubles, troubles


def running_from(folder: Path) -> bool:
    listed = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
    return any(str(folder) in line for line in listed.splitlines())


async def answered(app: App, worker: str, word: str, mark: int):
    """The worker's own words (its reply or its report) after `mark` say `word`."""

    async def said():
        messages = await app.get(f"/api/sessions/{worker}/messages?after={mark}")
        found = [m["text"] for m in messages
                 if m["kind"] in {"assistant", "report"} and word in m["text"].lower()]  # fmt: skip
        return found[0] if found else None

    text = await wait_for(f"the worker answers {word}", said, 600, app)
    log(f"  it said: {text[:80]!r}")


async def last_id(app: App, session: str) -> int:
    return max((m["id"] for m in await app.get(f"/api/sessions/{session}/messages")), default=0)


async def scenario_owners_cli(app: App, cache: Path) -> str:
    """V1: the owner's own CLI runs; the fallback is set up ahead; a worker answers."""
    state = await app.get("/api/state")
    assert state["runtime"]["state"] == "ready", state["runtime"]
    assert not state["runtime"].get("warning"), state["runtime"]
    router = state["router_id"]
    mark = await last_id(app, router)
    await app.post(
        f"/api/sessions/{router}/messages",
        {"text": "Start a worker for this, and tell it exactly: reply with just the word "
         "alpha, then report done. Nothing else."},
    )  # fmt: skip

    async def started():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        found = [m["meta"]["session_id"] for m in messages
                 if m["kind"] == "notice" and m["text"].startswith("Started")]  # fmt: skip
        return found[0] if found else None

    worker = await wait_for("the Router starts a worker", started, 300, app)
    await answered(app, worker, "alpha", 0)

    async def set_up():
        bundle = list(cache.glob("prebuilds/*/.hostless-runtime-assets-v2"))
        return bool(bundle)

    await wait_for("Copilot 1.0.112 is set up ahead", set_up, 600, app)
    clean(await app.get(f"/api/sessions/{worker}/messages"))
    log("V1: the owner's CLI runs (no line); 1.0.112 set up ahead; the worker answered")
    return worker


async def say(app: App, worker: str, text: str) -> int:
    mark = await last_id(app, worker)
    await app.post(f"/api/sessions/{worker}/messages", {"text": text})
    return mark


async def scenario_updated(app: App, worker: str, cache: Path, cli: Path):
    """V2: the CLI updates itself past the SDK: 1.0.112 runs at once, from the cache."""
    await app.post("/api/settings", {"copilot_command": str(cli)})

    async def fallen_back():
        runtime = (await app.get("/api/state"))["runtime"]
        return runtime["state"] == "ready" and runtime.get("warning") == FALLBACK_LINE

    await wait_for("Copilot 1.0.112 runs, said in one line", fallen_back, 120, app)
    assert running_from(cache), "the program set up ahead runs"
    mark = await say(app, worker, "Reply with just the word bravo.")
    await answered(app, worker, "bravo", mark)
    clean(await app.get(f"/api/sessions/{worker}/messages"))
    log("V2: past the SDK: 1.0.112 from the cache set up ahead, one line; the worker resumed")


async def scenario_back(app: App, worker: str):
    """V3: back on the owner's CLI: the line is gone, the worker resumes there."""
    await app.post("/api/settings", {"copilot_command": "copilot"})

    async def back():
        runtime = (await app.get("/api/state"))["runtime"]
        return runtime["state"] == "ready" and not runtime.get("warning")

    await wait_for("the owner's CLI runs again, no line", back, 120, app)
    mark = await say(app, worker, "Reply with just the word charlie.")
    await answered(app, worker, "charlie", mark)
    messages = await app.get(f"/api/sessions/{worker}/messages")
    clean(messages)
    fresh = [m for m in messages if "Continued in a fresh Copilot session" in m["text"]]
    assert not fresh, transcript(messages)
    log("V3: back on the owner's CLI; the worker's own session resumed each time")


async def main():
    token = (
        os.environ.get("COPILOT_GITHUB_TOKEN")
        or subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    with tempfile.TemporaryDirectory(prefix="cos-live-compat-") as temporary:
        root = Path(temporary).resolve()
        (root / "copilot-home").mkdir()
        os.environ["COPILOT_HOME"] = str(root / "copilot-home")
        cache = root / "runtime"
        cli = updated_cli(root)
        app = App(
            root / "data",
            root / "chiefofstaff.log",
            {"COPILOT_GITHUB_TOKEN": token, "COPILOT_CLI_EXTRACT_DIR": str(cache)},
        )
        failed = False
        try:
            await app.start()
            await app.post(
                "/api/settings",
                {"role_models": {"worker": MODEL}, "max_workers": 1,
                 "mac_notifications": False, "digest_time": ""},
            )  # fmt: skip
            worker = await scenario_owners_cli(app, cache)
            await scenario_updated(app, worker, cache, cli)
            await scenario_back(app, worker)
            log("COMPAT SCENARIOS PASSED")
        except Exception:
            failed = True
            raise
        finally:
            await app.stop()
            if failed:
                print((root / "chiefofstaff.log").read_text()[-6000:], file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
