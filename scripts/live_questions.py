"""Live test of a worker's question nobody answers in time (0.1.57), against real Copilot: the
worker asks on a question card (Copilot's ask_user), its wait is short here
(CHIEFOFSTAFF_REQUEST_TIMEOUT), and nobody answers.

Q1  the worker goes on without the answer (it is not held longer than the wait) and finishes;
    the question stays under Needs you, saying it went on without it; answered there, the
    worker hears which question the answer is to, on its task, and redoes the work with it
Q2  a question it went on without leaves Needs you once its task is accepted

Uses your signed-in Copilot CLI and consumes premium requests; the Copilot sessions it creates
are deleted at the end. Its data folder has a space in its path, as macOS's own has. Set TMPDIR
to a scratch folder of yours (its temporary folders go there); LIVE_QUESTIONS_PORT moves its
ports (the app's, and the stand-in Slack's 200 above it). Scenarios by name run alone.

    uv run python scripts/live_questions.py [Q1] [Q2]
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e
import live_parity  # noqa: F401  (it sets its own port: this one is set after)

live_e2e.PORT = int(os.environ.get("LIVE_QUESTIONS_PORT", "18791"))
live_e2e.SLACK_PORT = live_e2e.PORT + 200
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from live_e2e import App, cleanup, data_dir, log, transcript, wait_for  # noqa: E402
from live_parity import setup  # noqa: E402

MODEL = os.environ.get("LIVE_COPILOT_MODEL", "claude-sonnet-5.5")
# How long a worker waits on its question card here (an hour in use).
WAIT = 45
FINISHED = {"review", "done"}


class Run:
    def __init__(self, app: App, root: Path):
        self.app = app
        self.root = root
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = ""):
        self.results.append((name, ok, detail))
        log(f"{'PASS' if ok else 'FAIL'} {name}{f': {detail}' if detail else ''}")

    async def say(self, text: str):
        router = (await self.app.get("/api/state"))["router_id"]
        await self.app.post(f"/api/sessions/{router}/messages", {"text": text})

    async def card(self, words: str) -> dict:
        """The question card a worker shows the owner, by its words."""

        async def found():
            drafts = (await self.app.get("/api/state"))["drafts"]
            return next(
                (d for d in drafts if d["kind"] == "question" and words in d["text"].lower()), None
            )

        return await wait_for(f"the question card ({words})", found, 300, self.app)

    async def kept(self, session_id: str) -> dict:
        """Its question, under Needs you once its card had no answer in time."""

        async def found():
            questions = (await self.app.get("/api/state"))["questions"]
            return next(
                (q for q in questions if q["session_id"] == session_id and q.get("went_on")), None
            )

        return await wait_for("the question kept under Needs you", found, WAIT + 120, self.app)

    async def task(self, session_id: str, *statuses: str, timeout: float = 600) -> dict:
        async def found():
            tasks = (await self.app.get("/api/state"))["tasks"]
            mine = [t for t in tasks if t["session_id"] == session_id]
            return next((t for t in reversed(mine) if t["status"] in statuses), None)

        return await wait_for(f"its task {'/'.join(statuses)}", found, timeout, self.app)


def written(worker: dict, name: str) -> str:
    path = Path(worker["cwd"]) / name
    return path.read_text().strip() if path.exists() else ""


async def q1_the_question_stays_and_its_answer_reaches_the_worker(run: Run):
    app = run.app
    await run.say(
        "Start a worker titled 'Port Picker' (no project). Tell it: use your ask_user tool to ask "
        "me whether the API should listen on port 8080 or 9090 (those two as choices); if no "
        "answer comes, go on with 8080. Write the port alone into port.txt in your own folder, "
        "then report done, saying which port and why."
    )
    card = await run.card("8080")
    worker_id = card["session_id"]
    asked = asyncio.get_running_loop().time()
    question = await run.kept(worker_id)
    waited = asyncio.get_running_loop().time() - asked
    state = await app.get("/api/state")
    run.check(
        "Q1 the card left after its wait, the question stayed under Needs you",
        not any(d["id"] == card["id"] for d in state["drafts"])
        and "8080" in question["text"]
        and sorted(question["options"]) == ["8080", "9090"],
        f"after {waited:.0f}s: {question['text'][:80]!r}, choices {question['options']}",
    )
    done = await run.task(worker_id, *FINISHED)
    worker = await app.get(f"/api/sessions/{worker_id}")
    run.check(
        "Q1 the worker went on without the answer and finished",
        written(worker, "port.txt") == "8080",
        f"port.txt {written(worker, 'port.txt')!r}, task {done['status']}",
    )
    still = [q for q in (await app.get("/api/state"))["questions"] if q["id"] == question["id"]]
    run.check(
        "Q1 its question is still the owner's once it reported",
        bool(still) and still[0]["task_id"] == done["id"],
        f"{len(still)} question(s) on {done['id']}",
    )
    await app.post(f"/api/questions/{question['id']}/answer", {"text": "9090"})

    async def answered():
        worker = await app.get(f"/api/sessions/{worker_id}")
        return worker if written(worker, "port.txt") == "9090" else None

    worker = await wait_for("the answer used", answered, 600, app)
    # The prompt a message was sent as comes only with ?prompts=1.
    messages = await app.get(f"/api/sessions/{worker_id}/messages?prompts=1")
    told = [
        m for m in messages if m["kind"] == "owner" and "earlier" in m["meta"].get("prompt", "")
    ]
    questions = (await app.get("/api/state"))["questions"]
    run.check(
        "Q1 the answer reached the worker on its task, saying which question it answers",
        bool(told) and told[-1]["meta"].get("task_id") == done["id"],
        f"{len(told)} such message(s); port.txt {written(worker, 'port.txt')!r}",
    )
    run.check(
        "Q1 answered, it left Needs you",
        not any(q["id"] == question["id"] for q in questions),
        f"{len(questions)} question(s) left",
    )


async def q2_accepted_its_task_takes_the_question_away(run: Run):
    app = run.app
    await run.say(
        "Start a worker titled 'Color Picker' (no project). Tell it: use your ask_user tool to "
        "ask me whether the banner should be red or blue (those two as choices); if no answer "
        "comes, go on with red. Write the color alone into color.txt in your own folder, then "
        "report done."
    )
    card = await run.card("red")
    worker_id = card["session_id"]
    question = await run.kept(worker_id)
    done = await run.task(worker_id, *FINISHED)
    run.check(
        "Q2 the question stayed while its result waited for acceptance",
        any(q["id"] == question["id"] for q in (await app.get("/api/state"))["questions"]),
        f"task {done['status']}",
    )
    await app.post(f"/api/tasks/{done['id']}/accept", {})
    questions = (await app.get("/api/state"))["questions"]
    run.check(
        "Q2 accepted, its task took the question away",
        not any(q["id"] == question["id"] for q in questions),
        f"{len(questions)} question(s) left",
    )


async def main():
    wanted = {name.upper() for name in sys.argv[1:]}
    with tempfile.TemporaryDirectory(prefix="cos-questions-") as temporary:
        root = Path(temporary).resolve()
        home, project, _ = setup(root)
        data = data_dir(root)
        data.parent.mkdir(parents=True, exist_ok=True)
        env = {"COPILOT_HOME": str(home), "CHIEFOFSTAFF_REQUEST_TIMEOUT": str(WAIT)}
        app = App(data, root / "log", env)
        await app.start()
        run = Run(app, root)
        try:
            await app.post("/api/settings", {
                "projects": [str(project)], "model": MODEL, "max_workers": 3,
                "mac_notifications": False, "digest_time": ""})  # fmt: skip
            if not wanted or "Q1" in wanted:
                await q1_the_question_stays_and_its_answer_reaches_the_worker(run)
            if not wanted or "Q2" in wanted:
                await q2_accepted_its_task_takes_the_question_away(run)
        except BaseException:
            router = (await app.get("/api/state"))["router_id"]
            print(transcript(await app.get(f"/api/sessions/{router}/messages")))
            raise
        finally:
            await app.stop()
            await cleanup(data)
    results = run.results
    print("\n================ QUESTIONS ================")
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    failed = [item for item in results if not item[1]]
    print(f"{len(results) - len(failed)}/{len(results)} as expected")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
