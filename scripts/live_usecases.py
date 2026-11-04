"""Live test of the owner's everyday flows on finished and running work:

U1 revise a finished task (it comes back for review with the change), then accept it
U2 cancel a running task, then retry it with a note
U3 a worker asks the owner a question mid-task; the answer is used
U4 Create PR from a worker's branch on GitHub; the worker then follows the PR
U5 a long answer (a 500-row table) reaches the owner whole
U6 three requests in a row, sent while the Router is busy: none is lost
U7 a credit limit under Copilot's minimum is raised to it, and workers still run

    uv run python scripts/live_usecases.py [U1 U2 U3 U4 U5]
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e
import live_parity  # noqa: F401  (it sets its own port: this one is set after)

live_e2e.PORT = 18771
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from live_e2e import App, cleanup, git, log, transcript, wait_for  # noqa: E402
from live_parity import setup  # noqa: E402

MODEL = os.environ.get("USECASES_MODEL", "claude-sonnet-5.5")
PR_REPO = os.environ.get("CHIEFOFSTAFF_E2E_PR_REPO", "evanl666/cos-pr-e2e-1793727958")

HELPER_NOTICE = re.compile(r"“(Learn how you work|Learn checks from mistakes)”")


async def ask(app: App, text: str) -> tuple[str, dict]:
    router = (await app.get("/api/state"))["router_id"]
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    await app.post(f"/api/sessions/{router}/messages", {"text": text})

    async def assigned():
        found = [
            m["meta"]["session_id"]
            for m in await app.get(f"/api/sessions/{router}/messages?after={mark}")
            if m["kind"] == "notice"
            and m["text"].startswith(("Started", "New task", "Continued"))
            and not HELPER_NOTICE.search(m["text"])  # Its own learning workers.
        ]
        return found[-1] if found else None

    await wait_for(f"assigned: {text[:50]}", assigned, 300)
    await asyncio.sleep(4)  # The Router may route it in more than one step.
    worker = await assigned()
    return worker, await latest_task(app, worker)


async def latest_task(app: App, worker: str) -> dict:
    tasks = [t for t in (await app.get("/api/state"))["tasks"] if t["session_id"] == worker]
    return max(tasks, key=lambda t: t["created_at"])


async def reaches(app: App, task_id: str, statuses: set[str], since: str = "", timeout=900):
    async def check():
        task = await app.get(f"/api/tasks/{task_id}")
        return task if task["status"] in statuses and task["updated_at"] > since else None

    return await wait_for(f"task {task_id} is {'/'.join(sorted(statuses))}", check, timeout, app)


def now() -> str:
    return datetime.now(UTC).isoformat()


async def u1_revise_then_accept(app: App, project: Path):
    worker, task = await ask(
        app,
        f"In {project}, in an isolated worktree: add a function "
        "shout(text) to textkit/__init__.py that upper-cases text, with a "
        "test, and commit.",
    )
    task = await reaches(app, task["id"], {"review"})
    runs = task["runs"]
    since = now()
    await app.post(
        f"/api/tasks/{task['id']}/follow-up",
        {"text": "Also make shout() add an exclamation mark at the end, with a test."},
    )
    task = await reaches(app, task["id"], {"review"}, since)
    tree = Path((await app.get(f"/api/sessions/{worker}"))["cwd"])
    source = (tree / "textkit" / "__init__.py").read_text()
    assert "!" in source and "def shout" in source, source[-400:]
    assert task["runs"] > runs, "the revision is a new run of the same task"
    log(f"U1: revised task came back for review ({task['runs']} runs)")
    accepted = await app.post(f"/api/tasks/{task['id']}/accept")
    assert accepted["status"] == "done"
    log("U1: accepted: done")


async def u2_cancel_then_retry(app: App, project: Path):
    worker, task = await ask(
        app,
        f"In {project}: run `sleep 120 && echo late > late.txt` in "
        "the shell (exactly that), then report.",
    )

    async def sleeping():
        row = await app.get(f"/api/sessions/{worker}")
        return row if "sleep 120" in (row.get("step") or "") else None

    await wait_for("the slow command runs", sleeping, 300, app)
    cancelled = await app.post(f"/api/tasks/{task['id']}/cancel", {"text": "too slow"})
    assert cancelled["status"] == "cancelled"
    await asyncio.sleep(10)
    assert (await app.get(f"/api/tasks/{task['id']}"))["status"] == "cancelled", "stays cancelled"
    since = now()
    await app.post(
        f"/api/tasks/{task['id']}/retry",
        {"text": "Use `sleep 3 && echo quick > quick.txt` instead."},
    )
    task = await reaches(app, task["id"], {"review", "done"}, since)
    assert (
        "quick" in (task.get("result") or "").lower()
        or (project / "quick.txt").exists()
        or (Path((await app.get(f"/api/sessions/{worker}"))["cwd"]) / "quick.txt").exists()
    ), task.get("result")
    log("U2: cancelled while running, stayed cancelled; the retry with a note finished")


async def u3_question_mid_task(app: App, project: Path):
    worker, task = await ask(
        app,
        f"In {project}, in an isolated worktree: write greeting.txt "
        "with a greeting. Before writing it, ask me which language I want "
        "(report a question and wait for my answer).",
    )

    async def asked():
        """A question card (Copilot's ask_user) or a report(question): either is asking."""
        for draft in (await app.get("/api/state"))["drafts"]:
            if draft["session_id"] == worker and draft["kind"] in {"question", "form"}:
                return ("card", draft)
        current = await app.get(f"/api/tasks/{task['id']}")
        return ("report", current) if current["status"] == "waiting" else None

    how, item = await wait_for("the worker asks which language", asked, 600, app)
    log(f"U3: it asks ({how}): {(item.get('text') or item.get('progress') or '')[:120]!r}")
    since = now()
    if how == "card":
        await app.post(f"/api/drafts/{item['id']}", {"action": "answer", "text": "French, please."})
    else:
        await app.post(f"/api/sessions/{worker}/messages", {"text": "French, please."})
    await reaches(app, task["id"], {"review", "done"}, since)
    tree = Path((await app.get(f"/api/sessions/{worker}"))["cwd"])
    greeting = (tree / "greeting.txt").read_text()
    assert "bonjour" in greeting.lower() or "salut" in greeting.lower(), greeting
    log(f"U3: the answer was used: {greeting.strip()!r}")


async def u4_create_pr(app: App, root: Path):
    clone = root / "pr-project"
    subprocess.run(["gh", "repo", "clone", PR_REPO, str(clone), "--", "-q"], check=True)
    state = await app.get("/api/state")
    projects = [*state["settings"]["projects"], str(clone)]
    await app.post("/api/settings", {**state["settings"], "projects": projects})
    stamp = datetime.now(UTC).strftime("%H%M%S")
    worker, task = await ask(
        app,
        f"In {clone}, in an isolated worktree: add a file "
        f"notes/e2e-{stamp}.md with the line `hello {stamp}` and commit it. "
        "Do not push.",
    )
    task = await reaches(app, task["id"], {"review"})
    opened = await app.post(f"/api/tasks/{task['id']}/pr")
    urls = [link["url"] for link in (opened.get("receipts") or {}).get("prs", [])] or [
        u for u in json.dumps(opened).split('"') if "/pull/" in u
    ]
    url = next(u for u in urls if PR_REPO in u)
    files = subprocess.run(
        ["gh", "pr", "view", url, "--json", "files", "-q", ".files[].path"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"notes/e2e-{stamp}.md" in files, files
    number = url.rsplit("/", 1)[1]
    follows = (await app.get(f"/api/sessions/{worker}"))["follows"]
    assert f"github:{PR_REPO}#{number}" in follows, f"its PR comes back to it: {follows}"
    log(f"U4: Create PR opened {url} with the worker's commit; the worker follows it")
    subprocess.run(["gh", "pr", "close", url, "--delete-branch"], capture_output=True)
    log("U4: closed the test PR and deleted its branch")


async def u5_long_answer(app: App, project: Path):
    _, task = await ask(
        app,
        f"In {project}: list every row of data/sales.csv with its "
        "revenue (units * unit_price) as one markdown table, sorted by "
        "revenue descending. All rows, no summary. Do not change files.",
    )
    task = await reaches(app, task["id"], {"review", "done"})
    result = task.get("result") or ""
    rows = [line for line in result.splitlines() if line.startswith("|")]
    log(f"U5: report has {len(rows)} table lines, {len(result)} chars")
    assert len(rows) >= 500, "all 540 rows reach the owner, in the report"


async def u6_rapid_requests(app: App, project: Path):
    router = (await app.get("/api/state"))["router_id"]
    before = {t["id"] for t in (await app.get("/api/state"))["tasks"]}
    asks = [
        f"In {project}: how many lines does textkit/__init__.py have? Do not change files.",
        f"In {project}: how many rows does data/sales.csv have? Do not change files.",
        f"In {project}: what does AGENTS.md say? Do not change files.",
    ]
    for text in asks:
        await app.post(f"/api/sessions/{router}/messages", {"text": text})
        await asyncio.sleep(1)

    async def all_answered():
        tasks = [t for t in (await app.get("/api/state"))["tasks"] if t["id"] not in before]
        done = [t for t in tasks if t["status"] in {"review", "done"}]
        results = " ".join((t.get("result") or "") for t in done)
        replies = " ".join(
            m["text"]
            for m in await app.get(f"/api/sessions/{router}/messages")
            if m["kind"] in {"assistant", "inbound"}
        )
        found = results + " " + replies
        answered = [
            "__init__" in found or "lines" in found,
            "540" in found,
            "AGENTS" in found or "pytest" in found,
        ]
        return answered if all(answered) else None

    await wait_for("all three requests are answered", all_answered, 900, app)
    log("U6: three requests in three seconds: all three answered")


async def u7_credit_limit(app: App, project: Path):
    """A limit under Copilot's minimum (30 credits a session) is raised to it: before, every
    worker failed to start ("minimum session limit is 30 ai credits")."""
    state = await app.get("/api/state")
    saved = await app.post("/api/settings", {**state["settings"], "worker_credit_limit": 0.01})
    assert saved["settings"]["worker_credit_limit"] == 30, saved["settings"]
    worker, task = await ask(
        app, f"In {project}: how many Python files are in the repository? Do not change files."
    )
    current = await reaches(app, task["id"], {"review", "done"}, timeout=600)
    row = await app.get(f"/api/sessions/{worker}")
    assert row["status"] != "failed", row
    log(f"U7: a 0.01 limit was saved as 30; the worker ran: {current['result'][:80]!r}")
    await app.post(
        "/api/settings", {**(await app.get("/api/state"))["settings"], "worker_credit_limit": 0}
    )


async def main():
    wanted = set(sys.argv[1:]) or {"U1", "U2", "U3", "U4", "U5", "U6", "U7"}
    with tempfile.TemporaryDirectory(prefix="cos-usecases-") as temporary:
        root = Path(temporary).resolve()
        home, project, _ = setup(root)
        app = App(root / "data", root / "log", {"COPILOT_HOME": str(home)})
        await app.start()
        try:
            await app.post(
                "/api/settings",
                {
                    "projects": [str(project)],
                    "model": MODEL,
                    "max_workers": 4,
                    "mac_notifications": False,
                    "digest_time": "",
                },
            )
            if "U1" in wanted:
                await u1_revise_then_accept(app, project)
            if "U2" in wanted:
                await u2_cancel_then_retry(app, project)
            if "U3" in wanted:
                await u3_question_mid_task(app, project)
            if "U4" in wanted:
                await u4_create_pr(app, root)
            if "U5" in wanted:
                await u5_long_answer(app, project)
            if "U6" in wanted:
                await u6_rapid_requests(app, project)
            if "U7" in wanted:
                await u7_credit_limit(app, project)
            log("USE CASES PASSED")
        except BaseException:
            for session in (await app.get("/api/state"))["sessions"]:
                print(f"--- {session['role']} {session['title']} [{session['status']}]")
                print(transcript(await app.get(f"/api/sessions/{session['id']}/messages")))
            raise
        finally:
            await app.stop()
            await cleanup(root / "data")
            git(project, "worktree", "prune")


if __name__ == "__main__":
    asyncio.run(main())
