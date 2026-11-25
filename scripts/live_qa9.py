"""Live test of QA round 9's fixes against real Copilot (E139, E140).

W1  (E139) a worker told to wait for another worker's message ends its turn saying "Nothing is
    launched yet. I'll launch once <that worker's id> sends the snapshotId": its task stays
    running (never filed as done from those words), Home says it waits for that worker, and
    that worker's message wakes it: it does the work and reports it done with what it was sent.
W2  (E140) a worker in one long shell step whose output keeps growing (a line every 20 s for
    about 4 minutes) is never flagged by the Watchdog, whose stall threshold is 45 s here
    (CHIEFOFSTAFF_WATCHDOG_STALL; 15 minutes by default): a 25-minute step at the default, in
    short. Before 0.1.58 it was "no activity" within a minute.
W3  (E140) a worker in a silent step (`sleep`) is flagged once 45 s pass without output, and the
    Watchdog is told what it is at: "Now: in a turn since HH:MM (tool `…` running since HH:MM,
    no output yet)".

Uses your signed-in Copilot CLI (a Copilot home of its own, made from your config) and consumes
premium requests; the Copilot sessions it creates are deleted at the end. Set TMPDIR to a
scratch folder of yours; LIVE_QA9_PORT moves its ports (the app's, and the stand-in Slack's 200
above it, or LIVE_QA9_SLACK_PORT); QA9_MODEL picks the model. Scenarios by name run alone
(W2 and W3 run side by side).

    LIVE_QA9_PORT=18797 uv run --with playwright python scripts/live_qa9.py [W1] [W2] [W3]
"""

import asyncio
import contextlib
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e
import live_parity  # noqa: F401  (it sets its own port: this one is set after)

live_e2e.PORT = int(os.environ.get("LIVE_QA9_PORT", "18797"))
live_e2e.SLACK_PORT = int(os.environ.get("LIVE_QA9_SLACK_PORT", live_e2e.PORT + 200))
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from live_e2e import App, cleanup, data_dir, log, transcript, wait_for  # noqa: E402
from live_parity import setup  # noqa: E402

MODEL = os.environ.get("QA9_MODEL", "claude-sonnet-5.5")
STALL = "45"
SNAPSHOT = "S-4471"
FINISHED = {"review", "done"}
SCENARIOS = {"W1", "W2", "W3"}


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

    async def say(self, text: str):
        router = await self.router()
        await self.app.post(f"/api/sessions/{router}/messages", {"text": text})

    async def worker(self, title: str, timeout: float = 300) -> dict:
        """The worker the Router started for this title."""

        async def found():
            for row in (await self.app.get("/api/state"))["sessions"]:
                if row["role"] == "worker" and title.lower() in (row["title"] or "").lower():
                    return row
            return None

        return await wait_for(f"a worker for {title}", found, timeout, self.app)

    async def task_of(self, session_id: str) -> dict:
        tasks = (await self.app.get("/api/state"))["tasks"]
        return next(t for t in tasks if t["session_id"] == session_id)

    async def session(self, session_id: str) -> dict:
        return await self.app.get(f"/api/sessions/{session_id}")

    async def messages(self, session_id: str, prompts: bool = False) -> list[dict]:
        query = "?prompts=true" if prompts else ""
        return await self.app.get(f"/api/sessions/{session_id}/messages{query}")

    async def finished(self, task_id: str, timeout: float = 900) -> dict:
        async def done():
            task = await self.app.get(f"/api/tasks/{task_id}")
            return task if task["status"] in FINISHED else None

        return await wait_for(f"task {task_id} finished", done, timeout, self.app)

    async def reviews(self, session_id: str) -> list[dict]:
        """The Watchdog's reviews of a worker (the packets it was given)."""
        sessions = (await self.app.get("/api/state"))["sessions"]
        watchdog = next((s for s in sessions if s["role"] == "watchdog"), None)
        if not watchdog:
            return []
        return [
            m
            for m in await self.messages(watchdog["id"], prompts=True)
            if m["kind"] == "inbound" and m["meta"].get("review") == session_id
        ]

    def commands(self, session_id: str, word: str) -> list[tuple[str, str, str]]:
        """Its shell commands with this word, with when each started and ended (read only)."""
        database = data_dir(self.root) / "chiefofstaff.db"
        with contextlib.closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
            return db.execute(
                "SELECT command, started_at, coalesce(ended_at, '') FROM commands "
                "WHERE session_id=? AND command LIKE ? ORDER BY id",
                (session_id, f"%{word}%"),
            ).fetchall()


def seconds(started: str, ended: str) -> float:
    if not started or not ended:
        return 0.0
    return (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()


async def w1_a_worker_waiting_for_a_peer_stays_at_work_and_is_woken(run: Run):
    await run.say(
        "Start two workers that work together, both with no project: 'Release Builder' and "
        "'Model Trainer'. Release Builder: run `sleep 90` in the shell, then tell Model Trainer "
        f"with tell_session exactly 'Release r42 is out: snapshotId {SNAPSHOT}', then report "
        "done. Model Trainer: it trains once Release Builder sends the release's snapshotId, and "
        "nothing is launched before that. It does not wait in a shell or poll: it ends its turn "
        "at once with exactly 'Nothing is launched yet. I'll launch once <Release Builder's "
        "worker id> sends that release's snapshotId.' (the id as worker:… from its peers). Once "
        "the id arrives, it writes it into trained.txt in its folder and reports done with it."
    )
    builder = await run.worker("Release Builder")
    trainer = await run.worker("Model Trainer")
    task = await run.task_of(trainer["id"])
    waited, early, filed = "", "", []

    async def told():
        nonlocal waited, early
        row = await run.session(trainer["id"])
        if (row.get("activity") or "").startswith("waiting for “"):
            waited = row["activity"]
        rows = await run.messages(trainer["id"])
        if any(m["meta"].get("told_by") == builder["id"] for m in rows):
            return True
        now = await run.app.get(f"/api/tasks/{task['id']}")
        if now["status"] != "running" or now.get("result"):
            early = f"{now['status']}: {now.get('result')!r}"
        filed.extend(
            m["text"] for m in rows if m["kind"] == "notice" and "filed as one" in m["text"]
        )
        return None

    await wait_for("Release Builder's message to Model Trainer", told, 900, run.app)
    run.check("W1 its task stays running until the peer's message", not early, early)
    run.check("W1 never filed as done from its words", not filed, "; ".join(filed[:2]))
    run.check("W1 Home says whom it waits for", "Release Builder" in waited, waited or "none")
    done = await run.finished(task["id"], 900)
    run.check("W1 woken by the message, it reports the id", SNAPSHOT in (done.get("result") or ""),
              (done.get("result") or "")[:200])  # fmt: skip
    folder = Path((await run.session(trainer["id"]))["cwd"])
    written = (folder / "trained.txt").read_text() if (folder / "trained.txt").exists() else ""
    run.check("W1 it did the work then", SNAPSHOT in written, written[:80])


async def long_step(run: Run, title: str, script: Path) -> tuple[dict, dict]:
    await run.say(
        f"Start a worker titled '{title}' (no project): run {script} in the foreground as one "
        "command and wait for it to finish (about 4 minutes); give the shell command a timeout "
        "of 600 seconds, and do not run it in the background, in parts or with read_bash. Then "
        "report done with its last line of output."
    )
    worker = await run.worker(title)
    task = await run.task_of(worker["id"])
    return worker, task


async def w2_a_long_step_with_growing_output_is_not_flagged(run: Run):
    script = run.root / "train_epochs.sh"
    script.write_text(
        '#!/bin/sh\nfor i in $(seq 1 12); do echo "epoch $i/12 loss 0.$((90 - i))"; '
        "sleep 20; done\necho trained\n"
    )
    script.chmod(0o755)
    worker, task = await long_step(run, "Epoch Trainer", script)
    done = await run.finished(task["id"], 1200)
    ran = run.commands(worker["id"], "train_epochs")
    longest = max((seconds(started, ended) for _, started, ended in ran), default=0.0)
    run.check("W2 one long step", longest >= 150, f"{len(ran)} command(s), longest {longest:.0f}s")
    reviews = await run.reviews(worker["id"])
    run.check("W2 never flagged while its output grew", not reviews,
              "; ".join(m["text"] for m in reviews[:2]))  # fmt: skip
    run.check("W2 it finished", "trained" in (done.get("result") or ""), done["status"])


async def w3_a_silent_step_is_flagged_and_the_packet_says_what_it_is_at(run: Run):
    script = run.root / "quiet_export.sh"
    script.write_text("#!/bin/sh\nsleep 200\necho exported\n")
    script.chmod(0o755)
    worker, task = await long_step(run, "Quiet Exporter", script)
    started = time.monotonic()

    async def flagged():
        found = await run.reviews(worker["id"])
        return found or None

    try:
        found = await wait_for("the Watchdog's review of the silent step", flagged, 400, run.app)
    except Exception as error:
        run.check("W3 a silent step is flagged", False, str(error)[:200])
        return
    packet = found[0]["meta"].get("prompt") or ""
    run.check("W3 a silent step is flagged", True, f"after {time.monotonic() - started:.0f}s")
    now = next((line for line in packet.splitlines() if line.startswith("Now:")), "")
    said = now.startswith("Now: in a turn since ") and "quiet_export" in now
    run.check("W3 the packet says what it is at", said and "running since" in now, now)
    await run.finished(task["id"], 1200)


async def main():
    wanted = {name.upper() for name in sys.argv[1:]} or SCENARIOS
    with tempfile.TemporaryDirectory(prefix="cos-qa9-") as temporary:
        root = Path(temporary).resolve()
        home, project, _ = setup(root)
        data = data_dir(root)
        data.parent.mkdir(parents=True, exist_ok=True)
        env = {"COPILOT_HOME": str(home), "CHIEFOFSTAFF_WATCHDOG_STALL": STALL}
        os.environ["COPILOT_HOME"] = str(home)  # Its sessions are deleted there (cleanup).
        app = App(data, root / "log", env)
        await app.start()
        run = Run(app, root)
        try:
            await app.post("/api/settings", {
                "projects": [str(project)], "model": MODEL, "max_workers": 4,
                "mac_notifications": False, "digest_time": ""})  # fmt: skip
            if "W1" in wanted:
                await w1_a_worker_waiting_for_a_peer_stays_at_work_and_is_woken(run)
            parts = []
            if "W2" in wanted:
                parts.append(w2_a_long_step_with_growing_output_is_not_flagged(run))
            if "W3" in wanted:
                parts.append(w3_a_silent_step_is_flagged_and_the_packet_says_what_it_is_at(run))
            await asyncio.gather(*parts)
        except BaseException:
            router = (await app.get("/api/state"))["router_id"]
            print(transcript(await app.get(f"/api/sessions/{router}/messages")))
            raise
        finally:
            await app.stop()
            await cleanup(data)
    print("\n================ QA9 ================")
    for name, ok, detail in run.results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    failed = [item for item in run.results if not item[1]]
    print(f"{len(run.results) - len(failed)}/{len(run.results)} as expected")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
