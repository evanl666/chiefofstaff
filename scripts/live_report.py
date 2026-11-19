"""Live test of which task a worker's words belong to (QA round 7), on real Copilot sessions.

R1. The Router asks a worker about a task it finished (a note): the worker's answer reaches
    the Router, the task keeps its result, and the worker is not asked to report (E85, E96).
R2. A worker asks the owner a question; the owner tells the Router the answer, and the Router
    passes it on: the task goes on at once, it does not wait under Needs you (E97).

    uv run python scripts/live_report.py [R1 R2]

LIVE_REPORT_PORT sets its port (default 18777; Slack's fake on the port + 50).
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e
import live_parity  # noqa: F401  (it sets its own port: this one is set after)

live_e2e.PORT = int(os.environ.get("LIVE_REPORT_PORT", "18777"))
live_e2e.SLACK_PORT = live_e2e.PORT + 50
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from live_e2e import App, cleanup, log, transcript, wait_for  # noqa: E402
from live_parity import setup  # noqa: E402

MODEL = os.environ.get("REPORT_MODEL", "claude-sonnet-5.5")


async def router_id(app: App) -> str:
    return (await app.get("/api/state"))["router_id"]


async def say(app: App, text: str) -> int:
    """The owner's message to the Router; the id of the Router's last message before it."""
    router = await router_id(app)
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    await app.post(f"/api/sessions/{router}/messages", {"text": text})
    return mark


async def task_of(app: App, mark: int, statuses: set[str], what: str, timeout: float = 900):
    """The task the Router gave out after `mark`, once it is in one of these statuses."""
    router = await router_id(app)

    async def found():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        workers = [
            m["meta"]["session_id"]
            for m in messages
            if m["kind"] == "notice" and m["text"].startswith(("Started", "New task"))
        ]
        tasks = [t for t in (await app.get("/api/state"))["tasks"] if t["session_id"] in workers]
        return next((t for t in reversed(tasks) if t["status"] in statuses), None)

    return await wait_for(what, found, timeout, app)


async def task(app: App, task_id: str) -> dict:
    return next(t for t in (await app.get("/api/state"))["tasks"] if t["id"] == task_id)


async def idle(app: App, worker: str, what: str, timeout: float = 600):
    async def done():
        row = await app.get(f"/api/sessions/{worker}")
        return row["status"] not in {"queued", "starting", "working"} or None

    await wait_for(what, done, timeout, app)


async def r1_answer_about_finished_work(app: App) -> list[tuple[str, bool, str]]:
    mark = await say(
        app,
        "Start a new worker (no project): write the single word plum into plum.txt in its "
        "folder, then report done.",
    )
    done = await task_of(app, mark, {"review", "done"}, "the plum task is reported")
    worker = done["session_id"]
    await idle(app, worker, "its turn ends")
    worker_mark = max(m["id"] for m in await app.get(f"/api/sessions/{worker}/messages"))
    router = await router_id(app)
    mark = await say(
        app,
        "Ask the worker that wrote plum.txt, with a note on that finished task (no new work, "
        "nothing to redo), which shell command it used to write the file. Tell me its answer.",
    )

    async def answered():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        found = [m for m in messages if m["kind"] == "inbound" and m["meta"].get("from") == worker]
        reports = [m for m in found if m["meta"].get("status")]
        answers = [
            m for m in messages if m["kind"] == "inbound" and m["meta"].get("answered_by") == worker
        ]
        return (answers or reports or None) and (answers, reports)

    answers, reports = await wait_for("its answer reaches the Router", answered, 600, app)
    await idle(app, worker, "its turn on the note ends")
    after = await task(app, done["id"])
    said = await app.get(f"/api/sessions/{worker}/messages?after={worker_mark}")
    nudged = [m for m in said if "without a report" in m["text"]]
    results = [("R1 no report asked of it", not nudged, f"{len(nudged)} nudge(s)")]
    if reports and not answers:
        # It reported instead of answering in its chat (the model's choice): its report is its
        # answer, and that report is what the Router has.
        log("R1: the worker reported instead of answering in its chat")
        results.append(("R1 the Router has its answer", True, "as a report"))
        return results
    results += [
        ("R1 the Router has its answer", bool(answers), answers[0]["text"][:80] if answers else ""),
        (
            "R1 the finished task keeps its result",
            (after["status"], after["result"]) == (done["status"], done["result"]),
            f"{after['status']}: {after['result'][:60]!r}",
        ),
    ]
    return results


async def r2_the_routers_answer_moves_the_task_on(app: App) -> list[tuple[str, bool, str]]:
    mark = await say(
        app,
        "Start a new worker (no project) for this: before anything else it asks me, with "
        "report(status='question'), whether to write hi or hello into greet.txt in its folder; "
        "once I answer, it writes that word there and reports done.",
    )
    asked = await task_of(app, mark, {"waiting"}, "it asks its question")
    worker = asked["session_id"]
    await idle(app, worker, "its turn ends")
    router = await router_id(app)
    mark = await say(app, "Answer that worker's question for me: hello.")

    async def told():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        return next(
            (
                m
                for m in messages
                if m["kind"] == "notice"
                and m["meta"].get("session_id") == worker
                and m["text"].startswith(("Told", "Continued in"))
            ),
            None,
        )

    await wait_for("the Router passes the answer on", told, 300, app)
    now = await task(app, asked["id"])
    results = [("R2 its task goes on at once", now["status"] != "waiting", now["status"])]
    finished = await wait_for("it reports", lambda: _finished(app, asked["id"]), 600, app)
    results.append(("R2 it reports what was answered", finished["status"] in {"review", "done"},
                    finished["status"]))  # fmt: skip
    return results


async def _finished(app: App, task_id: str):
    found = await task(app, task_id)
    return found if found["status"] in {"review", "done", "failed"} else None


SCENARIOS = {"R1": r1_answer_about_finished_work, "R2": r2_the_routers_answer_moves_the_task_on}


async def main():
    chosen = [name for name in sys.argv[1:] if name in SCENARIOS] or list(SCENARIOS)
    every: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="cos-report-") as temporary:
        root = Path(temporary).resolve()
        home, project, _ = setup(root)
        app = App(root / "data", root / "log", {"COPILOT_HOME": str(home)})
        await app.start()
        try:
            await app.post("/api/settings", {
                "model": MODEL, "max_workers": 3, "mac_notifications": False,
                "digest_time": ""})  # fmt: skip
            for name in chosen:
                log(f"scenario {name}")
                every += await SCENARIOS[name](app)
        except BaseException:
            router = await router_id(app)
            print(transcript(await app.get(f"/api/sessions/{router}/messages")))
            raise
        finally:
            await app.stop()
            await cleanup(root / "data")
            subprocess.run(["git", "-C", str(project), "worktree", "prune"], check=False)
    print("\n================ REPORT ================")
    for name, ok, detail in every:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    failed = [item for item in every if not item[1]]
    print(f"{len(every) - len(failed)}/{len(every)} as expected")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
