"""Live test of a busy worker's new task going to a second worker with its context
(docs/design/worker-fork.md), against real Copilot: the Router routes, Chief of Staff forks.

F1  a busy worker (at work, two tasks open) gets a related task: a second worker starts at once
    with its context (one line in the Router chat, linked to it), and does the task; the busy
    worker's own tasks finish undisturbed, in its own folder
F2  the same kind of task once that worker is free: it takes the task itself (no second worker)
F3  every worker slot taken: the task waits for the busy worker, as before (no second worker)
F4  the second worker answers what only the busy worker's conversation held (a codename it read
    from a file deleted since, and never told anyone)
F5  a worker at work on its only task (not busy): related work the owner wants done in parallel
    goes to a second worker with its context at once, not behind that turn
F6  the owner asks for a new worker for work only a related worker's conversation can do (a
    codename it read from a file deleted since): the Router starts it from that worker
    (spawn_worker fork_of), and it knows the codename; that worker, between turns, untouched

Uses your signed-in Copilot CLI and consumes premium requests; the Copilot sessions it creates
are deleted at the end. Its data folder has a space in its path, as macOS's own has. Set TMPDIR
to a scratch folder of yours (its temporary folders go there); LIVE_FORK_PORT moves its ports
(the app's, and the stand-in Slack's 200 above it). Scenarios by name run alone (F6: only it).

    uv run --with playwright python scripts/live_fork.py [F6]
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
from live_e2e import App, cleanup, data_dir, log, transcript, wait_for  # noqa: E402
from live_parity import setup  # noqa: E402

MODEL = os.environ.get("FORK_MODEL", "claude-sonnet-5.5")
CODENAME = "HERON-7"
VAULT = "OSPREY-3"
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

    async def keeper(self, title: str, codename: str = CODENAME) -> str:
        """A worker that read a secret file and said nothing of it: its conversation is the only
        place that holds it now (the file is gone)."""
        secret = self.root / f"secret-{title.split()[0].lower()}.txt"
        secret.write_text(f"The codename is {codename}.\n")
        mark = await self.say(
            f"Start a worker titled '{title}' (no project): read the file {secret} and remember "
            "what it says, but do not repeat it to anyone: reply only with the word 'read'."
        )
        started = await self.notice(mark, "Started")
        await self.finished(started["meta"]["task_id"], 600)
        secret.unlink()
        return started["meta"]["session_id"]

    async def at_work_alone(self, title: str, seconds: int) -> str:
        """A worker that read the secret file (and said nothing of it), now at work on a long
        command, its only task."""
        worker = await self.keeper(title)
        mark = await self.say(
            f"Give the '{title}' worker a new task (that same worker): run `sleep {seconds}` in "
            "its folder, then write 'slept' into slept.txt there, and report."
        )
        await self.notice(mark, "New task", "Started")
        await self.at_work(worker, f"sleep {seconds}")
        return worker

    async def busy_worker(self, title: str, seconds: int) -> str:
        """at_work_alone, with a second task waiting behind its long command: busy."""
        worker = await self.at_work_alone(title, seconds)
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
        return await self.notice(mark, SECOND, "New task", "Started", "Queued")


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


async def f5_work_wanted_in_parallel_goes_to_a_second_worker(run: Run):
    worker = await run.at_work_alone("Night Owl", 150)
    own = [t["id"] for t in await run.tasks(worker)]
    mark = await run.say(
        "Meanwhile, in parallel with what the Night Owl worker is doing: write the codename from "
        "the file it read earlier into codename.txt."
    )
    line = await run.notice(mark, SECOND, "New task", "Started")
    second = line["meta"]["session_id"]
    run.check("F5 wanted in parallel: a second worker took it", line["text"].startswith(SECOND),
              line["text"])  # fmt: skip
    if second == worker:
        for task_id in [*own, line["meta"]["task_id"]]:
            await run.finished(task_id, 1200)
        return
    row = await run.session(second)
    task = await run.finished(line["meta"]["task_id"])
    written = Path(row["cwd"]) / "codename.txt"
    said = written.read_text().strip() if written.exists() else ""
    run.check("F5 with its context", CODENAME in said, said[:80])
    run.check("F5 its task is done", task["status"] in FINISHED, task["status"])
    for task_id in own:  # Its own work goes on, and ends before F3 takes every slot but one.
        await run.finished(task_id)


async def f6_a_new_worker_the_owner_asks_for_starts_from_a_related_one(run: Run):
    keeper = await run.keeper("Vault Keeper", VAULT)
    before = len(await run.app.get(f"/api/sessions/{keeper}/messages"))
    mark = await run.say(
        "Start a new worker for this, not the Vault Keeper itself: it needs what the Vault "
        "Keeper worker read earlier. Have it write the codename from the file the Vault Keeper "
        "read into vault.txt in its own folder."
    )
    line = await run.notice(mark, SECOND, "Started", "New task", "Continued")
    new = line["meta"]["session_id"]
    run.check("F6 a new worker, from the Vault Keeper's conversation",
              line["text"].startswith(SECOND) and new != keeper, line["text"])  # fmt: skip
    if new == keeper:
        return
    row = await run.session(new)
    run.check("F6 linked to the Vault Keeper", (row.get("fork_of") or {}).get("id") == keeper)
    task = await run.finished(line["meta"]["task_id"])
    written = Path(row["cwd"]) / "vault.txt"
    said = written.read_text().strip() if written.exists() else ""
    run.check("F6 it knew what only that conversation held", VAULT in said, said[:80])
    run.check("F6 its task is done", task["status"] in FINISHED, task["status"])
    told = [m for m in (await run.app.get(f"/api/sessions/{keeper}/messages"))[before:]
            if m["kind"] in {"router", "user"}]  # fmt: skip
    run.check("F6 the Vault Keeper was given nothing", not told, str(told)[:120])


async def f3_no_free_slot_the_task_waits_for_the_busy_worker(run: Run):
    await run.app.post("/api/settings", {"max_workers": 1})
    worker = await run.busy_worker("Slot Keeper", 120)
    line = await run.codename("Slot Keeper")
    run.check("F3 every slot taken: no second worker", not line["text"].startswith(SECOND),
              line["text"])  # fmt: skip
    run.check("F3 the busy worker has it", line["meta"]["session_id"] == worker)
    await run.finished(line["meta"]["task_id"], 1200)


async def main():
    wanted = set(sys.argv[1:])
    with tempfile.TemporaryDirectory(prefix="cos-fork-") as temporary:
        root = Path(temporary).resolve()
        home, project, _ = setup(root)
        data = data_dir(root)
        data.parent.mkdir(parents=True, exist_ok=True)
        app = App(data, root / "log", {"COPILOT_HOME": str(home)})
        await app.start()
        run = Run(app, root)
        try:
            await app.post("/api/settings", {
                "projects": [str(project)], "model": MODEL, "max_workers": 4,
                "fork_busy": True, "mac_notifications": False, "digest_time": ""})  # fmt: skip
            if not wanted or wanted & {"F1", "F2", "F4"}:
                worker = await f1_f4_a_busy_worker_hands_a_related_task_to_a_second_worker(run)
                await f2_a_free_worker_takes_its_related_task_itself(run, worker)
            if not wanted or "F5" in wanted:
                await f5_work_wanted_in_parallel_goes_to_a_second_worker(run)
            if not wanted or "F6" in wanted:
                await f6_a_new_worker_the_owner_asks_for_starts_from_a_related_one(run)
            if not wanted or "F3" in wanted:  # (Last: it takes every slot but one.)
                await f3_no_free_slot_the_task_waits_for_the_busy_worker(run)
        except BaseException:
            router = (await app.get("/api/state"))["router_id"]
            print(transcript(await app.get(f"/api/sessions/{router}/messages")))
            raise
        finally:
            await app.stop()
            await cleanup(data)
    print("\n================ FORK ================")
    for name, ok, detail in run.results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    failed = [item for item in run.results if not item[1]]
    print(f"{len(run.results) - len(failed)}/{len(run.results)} as expected")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
