"""Live test of E123, smooth by default, on real Copilot: what is safe is handled for the owner
(under Handled for you, with Undo), the other approvals are one card, Your OK.

  A1  A worker answers a question in its report, changing no code: once the owner opened the
      report it is accepted for them (READ_SETTLE_MINUTES, short here), listed under Handled for
      you; Undo puts it back to accept, and it is not accepted again.
  A2  A worker commits a file in the configured project: read or not, its result keeps Accept.
  A3  The Router asks which folder a task works in, offering only the one configured project:
      picked for the owner, no card; the Router starts the worker there (or gives it to the
      worker it has there). Change brings the card back; choosing the same folder keeps it.
  A4  Your OK in the page: a lesson that is safe, one that is not, a learned check and a PR ready
      to merge are one card, a line each, counted once by the badge; Approve all safe keeps the
      safe lesson and uses the check, never the other lesson nor the merge.

Uses your signed-in Copilot CLI, consumes premium requests (LIVE_APPROVALS_MODEL, the workers'
model: claude-haiku-5.5 by default) and takes about 10 minutes. Its port is LIVE_APPROVALS_PORT
(21050). All Copilot sessions it creates are deleted at the end.

    uv run --with playwright python scripts/live_approvals.py           # A1-A4
    uv run --with playwright python scripts/live_approvals.py A3        # one of them
"""

import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e

live_e2e.PORT = int(os.environ.get("LIVE_APPROVALS_PORT", "21050"))
live_e2e.SLACK_PORT = live_e2e.PORT + 1
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from live_e2e import WATCHDOG_ENV, App, cleanup, git, log, transcript, wait_for  # noqa: E402

try:
    from playwright.async_api import async_playwright, expect
except ImportError:
    sys.exit("Run it with: uv run --with playwright python scripts/live_approvals.py")

MODEL = os.environ.get("LIVE_APPROVALS_MODEL", "claude-haiku-5.5")
# Read, a report only is accepted at the next round after this (the rounds come every 5 s here).
ENV = {**WATCHDOG_ENV, "CHIEFOFSTAFF_READ_SETTLE_MINUTES": "0.05"}


async def said_to_router(app: App, text: str) -> tuple[str, int]:
    router = (await app.get("/api/state"))["router_id"]
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    await app.post(f"/api/sessions/{router}/messages", {"text": text})
    return router, mark


async def started_worker(app: App, router: str, mark: int) -> tuple[str, str]:
    """The worker the Router gave the task to, and the task its line names: a worker it started,
    or one it had, as a new task (r70: A3's went to A2's worker, in the same project, rightly).
    """

    async def started():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        found = [(m["meta"]["session_id"], m["meta"]["task_id"]) for m in messages
                 if m["kind"] == "notice" and m["meta"].get("session_id")
                 and m["meta"].get("task_id")
                 and m["text"].startswith(("Started", "New task"))]  # fmt: skip
        return found[0] if found else None

    return await wait_for("the Router gives a worker the task", started, 300, app)


async def result_of(app: App, task_id: str) -> dict:
    """Its result, its checkouts read: that task's, not an older one of a worker the Router had
    (r72: live_edges E took another task's)."""

    async def finished():
        found = await task(app, task_id)
        checking = (found["receipts"] or {}).get("checking")
        return found if found["status"] == "review" and not checking else None

    return await wait_for("its result, its checkouts read", finished, 600, app)


async def task(app: App, task_id: str) -> dict:
    return await app.get(f"/api/tasks/{task_id}")


async def handled(app: App, kind: str) -> list[dict]:
    return [e for e in (await app.get("/api/state")).get("handled", []) if e["kind"] == kind]


async def scenario_report(app: App):
    """A1: a report only is accepted once read; Undo puts it back, for good."""
    router, mark = await said_to_router(
        app,
        "Start a worker, with no project, for this question: in two sentences, what does the "
        "HTTP status 418 mean? It answers in its report and changes no files.",
    )
    _, task_id = await started_worker(app, router, mark)
    result = await result_of(app, task_id)
    assert result["report_only"], f"a report only: {result['receipts']}"
    assert (await task(app, result["id"]))["status"] == "review", "unread: it waits"
    await app.post(f"/api/tasks/{result['id']}/read", {})

    async def accepted():
        return (await task(app, result["id"]))["status"] == "done"

    await wait_for("accepted for the owner once read", accepted, 120, app)
    [entry] = [e for e in await handled(app, "accepted") if e["ref"] == result["id"]]
    assert "after you read it" in entry["text"] and entry["undo"] == "Undo", entry
    said = (await app.post(f"/api/handled/{entry['id']}/undo"))["said"]
    assert "waits for your Accept again" in said, said
    await asyncio.sleep(20)  # Several rounds: not accepted again.
    assert (await task(app, result["id"]))["status"] == "review"
    await app.post(f"/api/tasks/{result['id']}/accept")
    log(f"A1: “{result['title']}” accepted once read, undone, accepted by the owner")


async def scenario_code(app: App, project: Path):
    """A2: a result that commits code keeps its Accept."""
    router, mark = await said_to_router(
        app,
        f"In the project {project} (an isolated worktree), add a file hello.txt with the line "
        "`hi` and commit it with the message 'Add hello'.",
    )
    _, task_id = await started_worker(app, router, mark)
    result = await result_of(app, task_id)
    assert not result["report_only"], result["receipts"]
    assert ((result["receipts"] or {}).get("changed") or {}).get("commits"), result["receipts"]
    await app.post(f"/api/tasks/{result['id']}/read", {})
    await asyncio.sleep(20)
    assert (await task(app, result["id"]))["status"] == "review", "code keeps its Accept"
    assert not [e for e in await handled(app, "accepted") if e["ref"] == result["id"]]
    await app.post(f"/api/tasks/{result['id']}/accept")
    log(f"A2: “{result['title']}” committed code: it waited for Accept")


async def scenario_folder(app: App, project: Path):
    """A3: the one configured project offered is picked, said with Change."""
    before = {e["id"] for e in await handled(app, "workspace")}
    router, mark = await said_to_router(
        app,
        "Before you start anything, call ask_workspace for a task titled 'List the top files' "
        f"(request: list the files at the top of the project), offering only {project} as the "
        "candidate; then do what its answer says.",
    )

    async def picked():
        found = [e for e in await handled(app, "workspace") if e["id"] not in before]
        return found[0] if found else None

    entry = await wait_for("the folder picked for the owner", picked, 300, app)
    assert entry["undo"] == "Change" and project.name in entry["text"], entry
    drafts = (await app.get("/api/state"))["drafts"]
    assert not [d for d in drafts if d["kind"] == "workspace"], "no card"
    worker, _ = await started_worker(app, router, mark)
    row = await app.get(f"/api/sessions/{worker}")
    folders = [row["cwd"], *(c["path"] for c in row.get("checkouts") or [])]
    assert row.get("project") == str(project) or any(
        project.name in folder for folder in filter(None, folders)
    ), row
    messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
    lines = [m for m in messages if m["kind"] == "notice" and "the only project" in m["text"]]
    assert lines and lines[0]["meta"]["handled"]["undo"] == "Change", transcript(messages)
    await app.post(f"/api/handled/{entry['id']}/undo")
    drafts = (await app.get("/api/state"))["drafts"]
    [card] = [d for d in drafts if d["kind"] == "workspace"]
    assert card["target"]["picked"] == str(project), card
    await app.post(f"/api/drafts/{card['id']}", {"action": "choose", "choice": str(project)})
    log(f"A3: {project.name} picked for the owner; Change brought the card back; kept")


def seed_your_ok(data: Path):
    """Two lessons (one about what can't be taken back), a learned check and a PR ready to
    merge, as their cards come: written in the store while the app runs."""
    database = sqlite3.connect(data / "chiefofstaff.db")
    now = "2026-01-01T00:00:00+00:00"
    with database:
        router = database.execute("SELECT value FROM kv WHERE key='router_id'").fetchone()[0]
        items = []
        for lesson_id, text, sensitive in [
            ("lA4safe", "Keep reports to five lines", False),
            ("lA4force", "Never force-push a shared branch", True),
        ]:
            database.execute(
                "INSERT INTO lessons(id, text, scope, evidence, status, created_at, updated_at) "
                "VALUES (?, ?, '', '[]', 'proposed', ?, ?)",
                (lesson_id, text, now, now),
            )
            items.append({"id": lesson_id, "text": text, "scope": "", "because": [],
                          "sensitive": sensitive})  # fmt: skip
        evidence = {"failing": {"exit": 1, "output": "x"}, "fixed": {"exit": 0, "output": ""}}
        database.execute(
            """INSERT INTO checks(id, case_id, repo, name, why, applies_to, command, evidence,
            status, created_at, updated_at) VALUES ('kA4', 'cA4', 'app', 'No print', 'Logs.',
            '[]', '! git grep -n print', ?, 'proposed', ?, ?)""",
            (json.dumps(evidence), now, now),
        )
        cards = [
            ("dA4lessons", "lessons", {"lessons": items}, "2 lessons"),
            ("dA4check", "check", {"check": "kA4", "name": "No print", "why": "Logs.",
                                   "command": "! git grep -n print", "applies_to": [],
                                   "repo": "app", "task": "", **evidence}, "A check"),
            ("dA4merge", "merge", {"label": "acme/app#18", "repo": "acme/app", "number": 18,
                                   "title": "Email login", "head": "abc1234def",
                                   "url": "https://github.com/acme/app/pull/18",
                                   "review": "APPROVED"}, "Email login"),
        ]  # fmt: skip
        for draft_id, kind, target, text in cards:
            database.execute(
                "INSERT INTO drafts(id, session_id, kind, target, text, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (draft_id, router, kind, json.dumps(target), text, now),
            )
    database.close()


async def scenario_your_ok(app: App):
    """A4: Your OK in the page, counted once; Approve all safe takes only the safe."""
    seed_your_ok(app.data)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(f"{live_e2e.BASE}/#token={app.launch_token}")
        ok = page.locator("#needs .card.your-ok")
        await expect(ok).to_have_count(1, timeout=30000)
        await expect(ok.locator(".ok-row")).to_have_count(4)
        await expect(ok.locator(".ok-row:has(.ok-safe)")).to_have_count(2)
        items = await page.locator("#needs .items > *").count()
        await expect(page.locator(".home-item .badge")).to_have_text(str(items))
        await ok.get_by_role("button", name="Approve all safe (2)").click()
        await expect(ok.locator(".ok-row")).to_have_count(2, timeout=30000)
        await browser.close()
    assert not errors, errors
    database = sqlite3.connect(f"file:{app.data / 'chiefofstaff.db'}?mode=ro", uri=True)
    lessons = dict(database.execute("SELECT id, status FROM lessons WHERE id LIKE 'lA4%'"))
    check = database.execute("SELECT status FROM checks WHERE id='kA4'").fetchone()[0]
    merge = database.execute("SELECT status FROM drafts WHERE id='dA4merge'").fetchone()[0]
    database.close()
    assert lessons == {"lA4safe": "active", "lA4force": "proposed"}, lessons
    assert (check, merge) == ("active", "pending"), (check, merge)
    for draft in (await app.get("/api/state"))["drafts"]:
        if draft["kind"] in {"lessons", "merge"}:
            await app.post(f"/api/drafts/{draft['id']}", {"action": "reject"})
    log("A4: Your OK, one card counted once; Approve all safe kept the safe two, not the merge")


async def main():
    chosen = sys.argv[1:] or ["A1", "A2", "A3", "A4"]
    unknown = [name for name in chosen if not re.fullmatch(r"A[1-4]", name)]
    if unknown:
        sys.exit(f"unknown scenario: {unknown}")
    with tempfile.TemporaryDirectory(prefix="cos-live-approvals-") as temporary:
        root = Path(temporary).resolve()
        data, project = live_e2e.data_dir(root), root / "app"
        project.mkdir()
        git(project, "init", "-q", "-b", "main")
        (project / "README.md").write_text("# App\n")
        git(project, "add", ".")
        git(project, "-c", "user.email=live@test", "-c", "user.name=live", "commit", "-qm", "init")
        app = App(data, root / "chiefofstaff.log", extra_env=ENV)
        failed = False
        try:
            await app.start()
            await app.post(
                "/api/settings",
                {
                    "projects": [str(project)],
                    "role_models": {"worker": MODEL},
                    "mac_notifications": False,
                    "digest_time": "",
                },
            )
            if "A1" in chosen:
                await scenario_report(app)
            if "A2" in chosen:
                await scenario_code(app, project)
            if "A3" in chosen:
                await scenario_folder(app, project)
            if "A4" in chosen:
                await scenario_your_ok(app)
            log("APPROVALS SCENARIOS PASSED")
        except Exception:
            failed = True
            raise
        finally:
            await app.stop()
            await cleanup(data)
            if failed:
                print((root / "chiefofstaff.log").read_text()[-6000:], file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
