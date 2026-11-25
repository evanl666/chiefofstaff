"""Live test of which task a worker's words belong to (QA round 7), on real Copilot sessions.

R1. The Router asks a worker about a task it finished (a note): the worker's answer (in its
    chat, or an Answer/Update) reaches the Router, the task keeps its result, and the worker is
    not asked to report (E85, E96, E115).
R2. A worker asks the owner a question; the owner tells the Router the answer, and the Router
    passes it on: the task goes on at once, it does not wait under Needs you (E97).
R3. The owner asks a worker a direct question; report(status="answer") reaches Home once and
    leaves the finished task unchanged.
R4. An older worker answers the owner's direct question with status="progress"; it is forwarded
    as an Answer/Update and leaves the finished task unchanged.
R5. The owner accepts a result, then asks its worker in its chat for a correction: the corrected
    result is a line in the Router's chat, once, on its task, and stays there while the worker
    follows up on the owner's next message (owner report 0.1.53, item 2).
R6. A worker asks a decision again after the owner answered it (seen_precedents=true): it gets
    the answer, no new card; asked to reconsider it on new evidence, its card names the earlier
    decision, and the owner's answer replaces it: one decision in force (item 1).

    uv run python scripts/live_report.py [R1 R2 R3 R4 R5 R6]

LIVE_REPORT_PORT sets its port (default 18777; Slack's fake on the port + 50).
"""

import asyncio
import os
import sqlite3
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
from live_e2e import App, cleanup, data_dir, log, transcript, wait_for  # noqa: E402
from live_parity import setup  # noqa: E402

MODEL = os.environ.get("REPORT_MODEL", os.environ.get("LIVE_COPILOT_MODEL", "claude-sonnet-5.5"))


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
        # Its turn is over (a report already says "done" while it writes its last words, which
        # may complete a one-line report: r83 R1 took the result before that).
        row = await app.get(f"/api/sessions/{worker}")
        return (row["status"] not in {"queued", "starting", "working"} and not row["slot"]) or None

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
    done = await task(app, done["id"])  # As its turn left it.
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
        # It reported instead of answering in its chat: its report is its answer, and that report
        # is what the Router has. An Answer/Update (as its prompt asks, E115) changes no task.
        statuses = [m["meta"]["status"] for m in reports]
        log(f"R1: the worker reported ({', '.join(statuses)}) instead of answering in its chat")
        results.append(("R1 the Router has its answer", True, "as a report"))
        if set(statuses) == {"answer"}:
            results.append(
                (
                    "R1 its Answer/Update leaves the finished task as it was",
                    (after["status"], after["result"]) == (done["status"], done["result"]),
                    f"{after['status']}: {after['result'][:60]!r}",
                )
            )
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


async def reported_answer(app: App, status: str) -> list[tuple[str, bool, str]]:
    label = "answer" if status == "answer" else "progress compatibility"
    mark = await say(
        app,
        "Start a new worker (no project): write the number 42 into answer.txt in its folder, "
        "then report done.",
    )
    done = await task_of(app, mark, {"review", "done"}, f"the {label} task is reported")
    worker = done["session_id"]
    await idle(app, worker, "its first turn ends")
    before = await task(app, done["id"])
    router = await router_id(app)
    router_mark = max(
        (m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0
    )
    worker_mark = max(
        (m["id"] for m in await app.get(f"/api/sessions/{worker}/messages")), default=0
    )
    wording = (
        "Use report(status='answer'"
        if status == "answer"
        else "For this backward-compatibility check, use report(status='progress'"
    )
    await app.post(
        f"/api/sessions/{worker}/messages",
        {
            "text": "Read answer.txt and tell me which number it contains. "
            f"{wording}, task_id='{done['id']}') with the answer; do not report done."
        },
    )

    async def reached():
        messages = await app.get(f"/api/sessions/{router}/messages?after={router_mark}")
        answers = [
            message
            for message in messages
            if message["kind"] == "inbound"
            and message["meta"].get("from") == worker
            and message["meta"].get("status") == "answer"
        ]
        return answers or None

    await wait_for(f"the {label} reaches Home", reached, 600, app)
    await idle(app, worker, f"its {label} turn ends")
    answers = await reached() or []  # All of them, once its turn is over.
    after = await task(app, done["id"])
    worker_messages = await app.get(f"/api/sessions/{worker}/messages?after={worker_mark}")
    reports = [
        message
        for message in worker_messages
        if message["kind"] == "report" and "42" in message["text"]
    ]
    prefix = "R3" if status == "answer" else "R4"
    return [
        (
            f"{prefix} its answer reaches Home once",
            len(answers) == 1,
            f"{len(answers)} Answer/Update(s)",
        ),
        (
            f"{prefix} its task association is retained",
            answers[0]["meta"].get("task_id") == done["id"],
            answers[0]["meta"].get("task_id") or "",
        ),
        (
            f"{prefix} its task state and result stay unchanged",
            (after["status"], after["result"]) == (before["status"], before["result"]),
            after["status"],
        ),
        (
            f"{prefix} its report is marked told",
            bool(reports and reports[-1]["meta"].get("told")),
            str(reports[-1]["meta"])[:120] if reports else "no report",
        ),
    ]


async def r3_answer_status(app: App) -> list[tuple[str, bool, str]]:
    return await reported_answer(app, "answer")


async def r4_progress_answer_compatibility(app: App) -> list[tuple[str, bool, str]]:
    return await reported_answer(app, "progress")


async def last_id(app: App, session: str) -> int:
    return max((m["id"] for m in await app.get(f"/api/sessions/{session}/messages")), default=0)


async def r5_a_correction_stays_on_home(app: App) -> list[tuple[str, bool, str]]:
    mark = await say(
        app,
        "Start a new worker (no project): write the number 42 into number.txt in its folder, "
        "then report done.",
    )
    done = await task_of(app, mark, {"review", "done"}, "the number task is reported")
    worker = done["session_id"]
    await idle(app, worker, "its first turn ends")
    await app.post(f"/api/tasks/{done['id']}/accept")
    router = await router_id(app)
    router_mark = await last_id(app, router)
    await app.post(
        f"/api/sessions/{worker}/messages",
        {
            "text": "That number is wrong: number.txt must contain 57. Correct the file, then "
            "report done with the corrected result."
        },
    )

    async def corrected():
        messages = await app.get(f"/api/sessions/{router}/messages?after={router_mark}")
        found = [
            m
            for m in messages
            if m["kind"] == "inbound"
            and m["meta"].get("from") == worker
            and m["meta"].get("status") in {"done", "answer"}
            and "57" in m["text"]
        ]
        return found or None

    await wait_for("the correction is a line in the Router's chat", corrected, 600, app)
    await idle(app, worker, "its correction turn ends")
    lines = await corrected() or []
    upto = max(m["id"] for m in lines)
    worker_mark = await last_id(app, worker)
    await app.post(
        f"/api/sessions/{worker}/messages",
        {"text": "One more check on it: run `sleep 15; wc -c number.txt` and tell me the count."},
    )

    async def following_up():
        row = await app.get(f"/api/sessions/{worker}")
        return row["status"] == "working" or None

    await wait_for("its worker follows up", following_up, 300, app)
    meanwhile = [m for m in await corrected() or [] if m["id"] <= upto]
    await idle(app, worker, "its follow-up ends")
    after = [m for m in await corrected() or [] if m["id"] <= upto]
    said = await app.get(f"/api/sessions/{worker}/messages?after={worker_mark}")
    tasks = [t for t in (await app.get("/api/state"))["tasks"] if t["session_id"] == worker]
    now = await task(app, done["id"])
    log(f"R5: {[(m['meta']['status'], m['text'][:80]) for m in lines]}; result {now['result']!r}")
    return [
        ("R5 the correction is a line in the Router's chat, once", len(lines) == 1,
         f"{len(lines)} line(s)"),
        ("R5 it is on its task", bool(lines) and lines[0]["meta"].get("task_id") == done["id"],
         lines[0]["meta"].get("task_id", "") if lines else ""),
        ("R5 it is there while its worker follows up, and after", len(meanwhile) == len(after) == 1,
         f"{len(meanwhile)} then {len(after)}"),
        ("R5 a correction keeps its task", [t["id"] for t in tasks] == [done["id"]],
         ", ".join(t["title"] for t in tasks)),
        ("R5 its worker answered the follow-up", any(m["kind"] in {"assistant", "report"}
                                                      for m in said), f"{len(said)} message(s)"),
    ]  # fmt: skip


GREETING = "Should greet.txt say hi or hello?"


async def decision_card(app: App, worker: str | None = None, after: str = ""):
    """A decision card waiting for the owner (asked by this worker, made after `after`)."""
    drafts = (await app.get("/api/state"))["drafts"]
    found = [
        d
        for d in drafts
        if d["kind"] == "decision"
        and d["status"] == "pending"
        and (worker is None or d["session_id"] == worker)
        and d["created_at"] > after
    ]
    return found[-1] if found else None


def decisions(app: App) -> list[tuple[str, str, str, str]]:
    """Every decision kept: id, status, what replaced it, its text."""
    with sqlite3.connect(app.data / "chiefofstaff.db") as db:
        return db.execute(
            "SELECT id, status, coalesce(replaced_by, ''), text FROM decisions ORDER BY created_at"
        ).fetchall()


async def r6_a_decision_is_not_asked_twice(app: App) -> list[tuple[str, bool, str]]:
    await say(
        app,
        "Start a new worker (no project) for this: before anything else it asks me with "
        f"ask_decision, the question exactly “{GREETING}”, options hi and hello. Once I decide, "
        "it writes that word into greet.txt in its folder and reports done.",
    )
    card = await wait_for("its decision card", lambda: decision_card(app), 600, app)
    worker = card["session_id"]
    await app.post(f"/api/drafts/{card['id']}", {"action": "answer", "text": "hello"})

    async def reported():
        found = [t for t in (await app.get("/api/state"))["tasks"] if t["session_id"] == worker]
        return next((t for t in found if t["status"] in {"review", "done"}), None)

    done = await wait_for("it writes hello and reports", reported, 600, app)
    await idle(app, worker, "its turn ends")
    first = next(row[0] for row in decisions(app) if GREETING in row[3])
    mark = await last_id(app, worker)
    await app.post(
        f"/api/sessions/{worker}/messages",
        {
            "text": f"Call ask_decision again, the question exactly “{GREETING}”, with "
            "seen_precedents=true, then tell me in one line what it answered. Change no files."
        },
    )

    async def answered():
        said = await app.get(f"/api/sessions/{worker}/messages?after={mark}")
        return [m for m in said if m["kind"] in {"assistant", "report"}] or None

    await wait_for("it says what ask_decision answered", answered, 600, app)
    await idle(app, worker, "its turn ends")
    again = await decision_card(app, worker)
    said = " ".join(m["text"] for m in await answered() or [])
    now = await task(app, done["id"])
    results = [
        ("R6 asked again: no new card", again is None, again["id"] if again else ""),
        ("R6 asked again: it has the owner's answer", "hello" in said.lower(), said[:100]),
        ("R6 its task did not wait again", now["status"] in {"review", "done"}, now["status"]),
    ]
    await app.post(
        f"/api/sessions/{worker}/messages",
        {
            # (0.1.58, E154: only a picked option decides; words naming none are a note.)
            "text": "New evidence: greet.txt is shown to Spanish speakers. Ask me to reconsider "
            "that decision with ask_decision (replaces=its id, why: what changed, and the "
            "options hola and hello). Once I decide, write that word into greet.txt and report "
            "done."
        },
    )
    revisit = await wait_for(
        "its reconsideration card", lambda: decision_card(app, worker, card["created_at"]), 600, app
    )
    target = revisit["target"]
    previous, why = target.get("previous", ""), target.get("why", "")
    results.append((
        "R6 the reconsideration names the earlier decision, and why",
        target.get("replaces") == first and "hello" in previous and bool(why),
        f"{target.get('replaces')}: {previous[:60]} / {why[:60]}",
    ))  # fmt: skip
    await app.post(f"/api/drafts/{revisit['id']}", {"action": "answer", "text": "hola"})
    await wait_for("it reports again", lambda: _finished(app, done["id"]), 600, app)
    await idle(app, worker, "its turn ends")
    kept = decisions(app)
    active = [row for row in kept if row[1] == "active"]
    old = next(row for row in kept if row[0] == first)
    log(f"R6: decisions {kept}")
    results += [
        ("R6 one decision in force", len(active) == 1 and "hola" in active[0][3],
         "; ".join(row[3] for row in active)),
        ("R6 the earlier one is replaced by it", bool(active) and old[2] == active[0][0],
         f"{old[1]} → {old[2]}"),
    ]  # fmt: skip
    return results


SCENARIOS = {
    "R1": r1_answer_about_finished_work,
    "R2": r2_the_routers_answer_moves_the_task_on,
    "R3": r3_answer_status,
    "R4": r4_progress_answer_compatibility,
    "R5": r5_a_correction_stays_on_home,
    "R6": r6_a_decision_is_not_asked_twice,
}


async def main():
    chosen = [name for name in sys.argv[1:] if name in SCENARIOS] or list(SCENARIOS)
    every: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="cos-report-") as temporary:
        root = Path(temporary).resolve()
        home, project, _ = setup(root)
        # Its data folder's path has a space, as macOS's own has ("Application Support").
        app = App(data_dir(root), root / "log", {"COPILOT_HOME": str(home)})
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
            await cleanup(data_dir(root))
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
