"""Live: a task's life on real Copilot sessions (QA round 7 on 0.1.46).

A. A task that fails, retried, then cancelled, is cancelled (not Failed again, E83); one that
   fails is dismissed without its worker being reopened (E89); the Router's cancel_task works on
   a failed task too.
B. Focus ended early: the Router hears it with its next message (E110).

    uv run python scripts/live_life.py [A B]
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e
import live_parity  # noqa: F401  (it sets its own port: this one is set after)

live_e2e.PORT = 18775
live_e2e.SLACK_PORT = 18825
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from live_e2e import App, cleanup, log, transcript, wait_for  # noqa: E402
from live_parity import setup  # noqa: E402

MODEL = os.environ.get("LIFE_MODEL", "claude-sonnet-5.5")


async def say(app: App, text: str) -> int:
    router = (await app.get("/api/state"))["router_id"]
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    await app.post(f"/api/sessions/{router}/messages", {"text": text})
    return mark


async def assigned(app: App, mark: int) -> str:
    router = (await app.get("/api/state"))["router_id"]

    async def found():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        return next(
            (m["meta"]["session_id"] for m in messages
             if m["kind"] == "notice" and m["text"].startswith(("Started", "New task"))),
            None,
        )  # fmt: skip

    return await wait_for("the Router assigns it", found, 300)


async def task_of(app: App, worker: str) -> dict | None:
    tasks = [t for t in (await app.get("/api/state"))["tasks"] if t["session_id"] == worker]
    return tasks[-1] if tasks else None


async def ended(app: App, worker: str, after: str = "") -> dict:
    async def check():
        task = await task_of(app, worker)
        done = task and task["status"] in {"failed", "review", "done"}
        return task if done and task["updated_at"] > after else None

    return await wait_for("its task ends", check, 600, app)


FAIL = (
    "Start one worker for exactly this and nothing more: in {project}, run `ls "
    "/nonexistent-qa7-life` in the shell. It fails: then call report with status failed and the "
    "error. Do not try anything else, and do not start another worker for it."
)


async def scenario_a_cancel_and_dismiss(app: App, project: Path):
    worker = await assigned(app, await say(app, FAIL.format(project=project)))
    task = await ended(app, worker)
    assert task["status"] == "failed", (task["status"], task.get("result"))
    log("A: the task failed")
    await app.post(f"/api/tasks/{task['id']}/retry", {"text": "Try exactly once more."})

    async def running():
        found = await task_of(app, worker)
        return found if found and found["status"] == "running" else None

    await wait_for("it runs again", running, 120, app)
    view = await app.post(f"/api/tasks/{task['id']}/cancel", {"text": "Not needed after all."})
    assert view["status"] == "cancelled", view["status"]
    await asyncio.sleep(10)  # Its stopped turn ends: nothing brings it back as Failed.
    assert (await task_of(app, worker))["status"] == "cancelled"
    log("A: retried, then cancelled: cancelled, not Failed again (E83)")

    other = await assigned(app, await say(app, FAIL.format(project=project)))
    failed = await ended(app, other)
    assert failed["status"] == "failed", failed["status"]
    await app.post(f"/api/sessions/{other}/close")
    view = await app.post(f"/api/tasks/{failed['id']}/cancel", {"text": ""})
    assert view["status"] == "cancelled", view["status"]
    row = await app.get(f"/api/sessions/{other}")
    assert row["closed_at"], "dismissed without its worker being reopened"
    log("A: a failed task is dismissed; its closed worker stays closed (E89)")


async def scenario_b_focus_ends_early(app: App):
    router = (await app.get("/api/state"))["router_id"]
    await app.post("/api/focus", {"minutes": 30})
    await app.post("/api/focus", {"minutes": 0})
    mark = await say(app, "Is my focus time still on? Answer in one line.")

    async def answered():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        return next((m for m in messages if m["kind"] == "assistant"), None)

    await wait_for("the Router answers", answered, 300, app)
    notices = [
        m for m in await app.get(f"/api/sessions/{router}/messages")
        if m["kind"] == "notice" and m["text"].startswith("Focus ended at")
    ]  # fmt: skip
    assert notices and notices[-1]["id"] < mark + 1, "held for its next message, before it"
    log("B: focus ended early: the Router has it with its next message (E110)")


async def main():
    wanted = set(sys.argv[1:]) or set("AB")
    with tempfile.TemporaryDirectory(prefix="cos-life-") as temporary:
        root = Path(temporary).resolve()
        home, project, _ = setup(root)
        app = App(root / "data", root / "chiefofstaff.log", {"COPILOT_HOME": str(home)})
        await app.start()
        try:
            await app.post(
                "/api/settings",
                {"projects": [str(project)], "model": MODEL, "max_workers": 3,
                 "mac_notifications": False, "digest_time": ""},
            )  # fmt: skip
            if "A" in wanted:
                await scenario_a_cancel_and_dismiss(app, project)
            if "B" in wanted:
                await scenario_b_focus_ends_early(app)
            log("LIVE LIFE PASSED")
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
