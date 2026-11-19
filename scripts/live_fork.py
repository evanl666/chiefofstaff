"""Live test of a busy worker's new task going to a second worker with its context
(docs/design/worker-fork.md), against real Copilot: the Router routes, Chief of Staff forks.

F1  a busy worker (at work, two tasks open) gets a related task: a second worker starts at once
    with its context (one line in the Router chat, linked to it), and does the task; the busy
    worker's own tasks finish undisturbed, in its own folder
F2  the same kind of task once that worker is free: it takes the task itself (no second worker)
F3  every worker slot taken: the task waits for the busy worker, as before (no second worker)
F4  the second worker answers what only the busy worker's conversation held (a codename it read
    from a file deleted since, and never told anyone)

Uses your signed-in Copilot CLI and consumes premium requests; the Copilot sessions it creates
are deleted at the end.

    uv run --with playwright python scripts/live_fork.py
"""

import asyncio
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e
import live_parity  # noqa: F401  (it sets its own port: this one is set after)

live_e2e.PORT = int(os.environ.get("LIVE_FORK_PORT", "18776"))
live_e2e.SLACK_PORT = live_e2e.PORT + 200
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from live_e2e import App, cleanup, log, transcript, wait_for  # noqa: E402
from live_parity import setup  # noqa: E402

MODEL = os.environ.get("FORK_MODEL", "claude-sonnet-5.5")
CODENAME = "HERON-7"
FINISHED = {"review", "done"}
SECOND = "Started a second worker for"


def stamp() -> str:
    return datetime.now(UTC).isoformat()


class Run:
    def __init__(self, app: App, root: Path):
        self.app = app
        self.root = root
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = ""):
        self.results.append((name, ok, detail))
        log(f"{'PASS' if ok else 'FAIL'} {name}{f': {detail}' if detail else ''}")

    async def router(self) -> str:
        return (await self.app.get("/api/state"))["router_id"]

    async def say(self, text: str) -> int:
        """The owner's message to the Router: the id of the last message before it."""
        router = await self.router()
        messages = await self.app.get(f"/api/sessions/{router}/messages")
        mark = max((m["id"] for m in messages), default=0)
        await self.app.post(f"/api/sessions/{router}/messages", {"text": text})
        return mark

    async def notice(self, mark: int, *prefixes: str, timeout: float = 300) -> dict:
        """The Router's first notice after `mark` that starts with one of these."""
        router = await self.router()

        async def found():
            for message in await self.app.get(f"/api/sessions/{router}/messages?after={mark}"):
                if message["kind"] == "notice" and message["text"].startswith(prefixes):
                    return message
            return None

        return await wait_for(f"the Router's line: {' / '.join(prefixes)}", found, timeout)

    async def notices(self, mark: int) -> list[str]:
        router = await self.router()
        return [
            m["text"]
            for m in await self.app.get(f"/api/sessions/{router}/messages?after={mark}")
            if m["kind"] == "notice"
        ]

    async def tasks(self, session_id: str) -> list[dict]:
        state = await self.app.get("/api/state")
        return [t for t in state["tasks"] if t["session_id"] == session_id]

    async def session(self, session_id: str) -> dict:
        return await self.app.get(f"/api/sessions/{session_id}")

    async def finished(self, task_id: str, timeout: float = 900) -> dict:
        async def done():
            task = await self.app.get(f"/api/tasks/{task_id}")
            return task if task["status"] in FINISHED else None

        return await wait_for(f"task {task_id} finished", done, timeout, self.app)

    async def at_work(self, session_id: str, command: str, timeout: float = 300):
        async def sleeping():
            row = await self.session(session_id)
            return row if command in (row.get("step_command") or "") else None

        await wait_for(f"it runs {command}", sleeping, timeout, self.app)

    async def busy_worker(self, title: str, seconds: int) -> str:
        """A worker that read the secret file (and said nothing of it), now at work on a long
        command with a second task waiting behind it: busy."""
        secret = self.root / f"secret-{title.split()[0].lower()}.txt"
        secret.write_text(f"The codename is {CODENAME}.\n")
        mark = await self.say(
            f"Start a worker titled '{title}' (no project): read the file {secret} and remember "
            "what it says, but do not repeat it to anyone: reply only with the word 'read'."
        )
        started = await self.notice(mark, "Started")
        worker = started["meta"]["session_id"]
        first = started["meta"]["task_id"]
        await self.finished(first, 600)
        secret.unlink()  # Its conversation is the only place that holds it now.
        mark = await self.say(
            f"Give the '{title}' worker a new task (that same worker): run `sleep {seconds}` in "
            "its folder, then write 'slept' into slept.txt there, and report."
        )
        await self.notice(mark, "New task", "Started")
        await self.at_work(worker, f"sleep {seconds}")
        mark = await self.say(
            f"Give the '{title}' worker one more task, for once it is free (that same worker): "
            "write 'two' into two.txt in its folder."
        )
        await self.notice(mark, "New task", "Started")
        return worker

    async def codename(self, title: str) -> dict:
        """A new task only the busy worker's conversation can do: the Router's line for it."""
        mark = await self.say(
            f"New task for the '{title}' worker: write the codename from the file it read "
            "earlier into codename.txt. I need it now: do not wait for what it is doing."
        )
        return await self.notice(mark, SECOND, "New task", "Started")


async def f1_f4_a_busy_worker_hands_a_related_task_to_a_second_worker(run: Run) -> str:
    worker = await run.busy_worker("Secret Keeper", 200)
    before = {t["id"] for t in await run.tasks(worker)}
    line = await run.codename("Secret Keeper")
    second = line["meta"]["session_id"]
    run.check("F1 a second worker took it", line["text"].startswith(SECOND), line["text"])
    if second == worker:
        return worker
    row = await run.session(second)
    run.check("F1 linked to the busy worker", (row.get("fork_of") or {}).get("id") == worker)
    task = await run.finished(line["meta"]["task_id"])
    written = Path(row["cwd"]) / "codename.txt"
    said = written.read_text().strip() if written.exists() else ""
    run.check("F4 it knew what only that conversation held", CODENAME in said, said[:80])
    run.check("F1 its task is done", task["status"] in FINISHED, task["status"])
    for task_id in before:
        task = await run.finished(task_id)
        run.check(f"F1 the busy worker's own task finished: {task['title']}", True)
    theirs = Path((await run.session(worker))["cwd"])
    run.check(
        "F1 undisturbed: its files in its own folder, none of the second worker's",
        (theirs / "slept.txt").exists() and not (theirs / "codename.txt").exists(),
        str(sorted(p.name for p in theirs.iterdir()) if theirs.exists() else "no folder"),
    )
    return worker


async def f2_a_free_worker_takes_its_related_task_itself(run: Run, worker: str):
    mark = await run.say(
        "Have the Secret Keeper worker also write 'free' into free.txt in its own folder."
    )
    line = await run.notice(mark, SECOND, "New task", "Started", "Continued")
    run.check("F2 no second worker for a free worker", not line["text"].startswith(SECOND),
              line["text"])  # fmt: skip
    await run.finished(line["meta"]["task_id"])


async def f3_no_free_slot_the_task_waits_for_the_busy_worker(run: Run):
    await run.app.post("/api/settings", {"max_workers": 1})
    worker = await run.busy_worker("Slot Keeper", 120)
    line = await run.codename("Slot Keeper")
    run.check("F3 every slot taken: no second worker", not line["text"].startswith(SECOND),
              line["text"])  # fmt: skip
    run.check("F3 the busy worker has it", line["meta"]["session_id"] == worker)
    await run.finished(line["meta"]["task_id"], 1200)


async def main():
    with tempfile.TemporaryDirectory(prefix="cos-fork-") as temporary:
        root = Path(temporary).resolve()
        home, project, _ = setup(root)
        app = App(root / "data", root / "log", {"COPILOT_HOME": str(home)})
        await app.start()
        run = Run(app, root)
        try:
            await app.post("/api/settings", {
                "projects": [str(project)], "model": MODEL, "max_workers": 4,
                "fork_busy": True, "mac_notifications": False, "digest_time": ""})  # fmt: skip
            worker = await f1_f4_a_busy_worker_hands_a_related_task_to_a_second_worker(run)
            await f2_a_free_worker_takes_its_related_task_itself(run, worker)
            await f3_no_free_slot_the_task_waits_for_the_busy_worker(run)
        except BaseException:
            router = (await app.get("/api/state"))["router_id"]
            print(transcript(await app.get(f"/api/sessions/{router}/messages")))
            raise
        finally:
            await app.stop()
            await cleanup(root / "data")
    print("\n================ FORK ================")
    for name, ok, detail in run.results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    failed = [item for item in run.results if not item[1]]
    print(f"{len(run.results) - len(failed)}/{len(run.results)} as expected")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
