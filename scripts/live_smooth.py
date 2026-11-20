"""Live: folder settings on real Copilot sessions, smooth (qa8: E121, E122, E124).

A. Without the sandbox (the default), a hook put in a worker's folder holds nothing: what the
   owner writes it goes out, and no line asks about its settings.
B. With the sandbox on, the same hook holds it; the owner's message shows that line's choices at
   once; "use these settings" in its own chat uses them (one line says so) and the message goes.
C. With the sandbox on, a settings file whose only change runs nothing (`model`) holds nothing.
D. With the sandbox on, a held worker whose task is cancelled closes; what the owner wrote it is
   dropped, never sent, and Home says so.

    uv run python scripts/live_smooth.py [A B C D]
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e
import live_parity  # noqa: F401  (it sets its own port: this one is set after)

live_e2e.PORT = 18779
live_e2e.SLACK_PORT = 18829
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from live_e2e import App, cleanup, log, transcript, wait_for  # noqa: E402
from live_parity import setup  # noqa: E402

MODEL = os.environ.get("SMOOTH_MODEL", "claude-sonnet-5.5")
HOOK = ".github/hooks/smooth.json"
START = (
    "Start one worker for exactly this and nothing more, in {project} with isolate=true: run `ls` "
    "in the shell, then report done with what it lists. Do not start another worker for it."
)


async def say(app: App, text: str) -> int:
    router = (await app.get("/api/state"))["router_id"]
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    await app.post(f"/api/sessions/{router}/messages", {"text": text})
    return mark


async def worker_done(app: App, project: Path) -> tuple[str, dict]:
    """A worker of the project at rest, its task reported."""
    router = (await app.get("/api/state"))["router_id"]
    mark = await say(app, START.format(project=project))

    async def found():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        return next(
            (m["meta"]["session_id"] for m in messages
             if m["kind"] == "notice" and m["text"].startswith(("Started", "New task"))),
            None,
        )  # fmt: skip

    worker = await wait_for("the Router starts the worker", found, 300)

    async def reported():
        tasks = [t for t in (await app.get("/api/state"))["tasks"] if t["session_id"] == worker]
        return tasks[-1] if tasks and tasks[-1]["status"] in {"review", "done"} else None

    task = await wait_for("its task is reported", reported, 600, app)
    return worker, task


async def folder(app: App, worker: str) -> Path:
    return Path((await app.get(f"/api/sessions/{worker}"))["cwd"])


async def settings_lines(app: App, worker: str) -> list[dict]:
    return [
        m for m in await app.get(f"/api/sessions/{worker}/messages")
        if m["kind"] == "notice" and m["meta"].get("agent_settings")
    ]  # fmt: skip


async def answered_after(app: App, worker: str, mark: int, what: str) -> dict:
    async def check():
        messages = await app.get(f"/api/sessions/{worker}/messages?after={mark}")
        return next((m for m in messages if m["kind"] in {"assistant", "report"}), None)

    return await wait_for(what, check, 600, app)


def plant(where: Path, name: str, text: str):
    (where / name).parent.mkdir(parents=True, exist_ok=True)
    (where / name).write_text(text)


async def scenario_a_no_sandbox_no_hold(app: App, project: Path):
    worker, _ = await worker_done(app, project)
    plant(await folder(app, worker), HOOK, json.dumps({"version": 1, "hooks": {}}))
    sent = await app.post(
        f"/api/sessions/{worker}/messages", {"text": "Now run `ls textkit` and report done."}
    )
    assert "settings_hold" not in sent["meta"], sent["meta"]
    await answered_after(app, worker, sent["id"], "it answers, nothing held")
    assert not await settings_lines(app, worker), "no line about its folder's settings"
    log("A: without the sandbox a planted hook holds nothing; the message went out")


async def scenario_b_typed_word(app: App, project: Path):
    worker, _ = await worker_done(app, project)
    plant(await folder(app, worker), HOOK, json.dumps({"version": 1, "hooks": {}}))
    sent = await app.post(
        f"/api/sessions/{worker}/messages", {"text": "Now run `ls textkit` and report done."}
    )
    hold = sent["meta"].get("settings_hold")
    assert hold and hold["changes"] == f"added {HOOK}", sent["meta"]
    assert sent["delivery"] == "queued", sent["delivery"]
    log("B: held; the message shows that line's choices at once")
    state = await app.get("/api/state")
    shown = next(s for s in state["sessions"] if s["id"] == worker)["settings_hold"]["digest"]
    said = await app.post(
        f"/api/sessions/{worker}/messages",
        {"text": "use these settings", "settings_digest": shown},
    )
    assert said["meta"].get("settings_typed") == shown, said["meta"]
    lines = [
        m["text"] for m in await app.get(f"/api/sessions/{worker}/messages")
        if m["meta"].get("settings_undo")
    ]  # fmt: skip
    assert lines == [f"Using these settings (added {HOOK}) because you said so."], lines
    await answered_after(app, worker, said["id"], "the held message goes, and it answers")
    log("B: the owner's plain word in its chat used the settings shown; the message went")


async def scenario_c_inert_key(app: App, project: Path):
    worker, _ = await worker_done(app, project)
    plant(await folder(app, worker), ".claude/settings.local.json", json.dumps({"model": MODEL}))
    sent = await app.post(
        f"/api/sessions/{worker}/messages", {"text": "Now run `ls tests` and report done."}
    )
    assert "settings_hold" not in sent["meta"], sent["meta"]
    await answered_after(app, worker, sent["id"], "it answers, nothing held")
    assert not await settings_lines(app, worker)
    log("C: a settings file changed only in a key that runs nothing holds nothing")


async def scenario_d_cancelled_while_held(app: App, project: Path):
    router = (await app.get("/api/state"))["router_id"]
    worker, task = await worker_done(app, project)
    plant(await folder(app, worker), HOOK, json.dumps({"version": 1, "hooks": {}}))
    sent = await app.post(
        f"/api/sessions/{worker}/messages", {"text": "Also count the files in textkit."}
    )
    assert sent["meta"].get("settings_hold"), sent["meta"]
    await app.post(f"/api/tasks/{task['id']}/cancel", {"text": "Not needed after all."})

    async def closed():
        return (await app.get(f"/api/sessions/{worker}"))["closed_at"]

    await wait_for("the held worker closes", closed, 120, app)
    message = next(
        m for m in await app.get(f"/api/sessions/{worker}/messages") if m["id"] == sent["id"]
    )
    assert message["meta"].get("dropped") and not message.get("delivery"), message
    notices = [
        m["text"] for m in await app.get(f"/api/sessions/{router}/messages")
        if m["kind"] == "notice" and "was not sent: its task is over" in m["text"]
    ]  # fmt: skip
    assert notices, "Home says what was dropped"
    log("D: cancelled while held: it closed; the message was dropped and Home says so")


async def main():
    wanted = set(sys.argv[1:]) or set("ABCD")
    with tempfile.TemporaryDirectory(prefix="cos-smooth-") as temporary:
        root = Path(temporary).resolve()
        home, project, _ = setup(root)
        app = App(root / "data", root / "chiefofstaff.log", {"COPILOT_HOME": str(home)})
        await app.start()
        try:
            await app.post(
                "/api/settings",
                {"projects": [str(project)], "model": MODEL, "max_workers": 3,
                 "mac_notifications": False, "digest_time": "", "worker_sandbox": False},
            )  # fmt: skip
            if "A" in wanted:
                await scenario_a_no_sandbox_no_hold(app, project)
            if wanted & set("BCD"):
                await app.post("/api/settings", {"worker_sandbox": True})
            if "B" in wanted:
                await scenario_b_typed_word(app, project)
            if "C" in wanted:
                await scenario_c_inert_key(app, project)
            if "D" in wanted:
                await scenario_d_cancelled_while_held(app, project)
            log("LIVE SMOOTH PASSED")
        except BaseException:
            state = await app.get("/api/state")
            for session in state["sessions"]:
                print(f"--- {session['role']} {session['title']} [{session['status']}]")
                print(transcript(await app.get(f"/api/sessions/{session['id']}/messages")))
            print("\n".join((root / "chiefofstaff.log").read_text().splitlines()[-40:]))
            raise
        finally:
            await app.stop()
            await cleanup(root / "data")


if __name__ == "__main__":
    asyncio.run(main())
