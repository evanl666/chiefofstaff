"""Live test of task relationships in team threads (E130), on the real Copilot: a real Router run
makes the relations, a team's thread has them (GET /api/teams/<id>/thread), the page shows them.

  R1  Two parts of one request, one passing a token to the other: one team, a fan of two parts
      that lands (2 of 2), each row's one relation its part.
  R2  A task that needs another's result before it starts (`after`): while the other runs its
      row waits on it (Home's line: "1 waiting"; the view: "waits on …"), the two workers one
      team though they never message; once the other is done the wait is cleared.
  R3  Another worker does an open task instead (`replaces`): an edge from the old task to the
      new, the old row "handed to …" in the page.
  R4  In the browser: R1's team (its parts, a lineage with its caption).

Uses your signed-in Copilot CLI (a scratch COPILOT_HOME, as live_wide does), consumes premium
requests (LIVE_COPILOT_MODEL, claude-sonnet-5.5 by default) and takes about 15 minutes. Ports:
LIVE_RELATIONS_PORT (21520) for the app, LIVE_RELATIONS_SLACK_PORT (21521) for the fake Slack.
Set TMPDIR to a scratch folder of yours: its temporary folders go there.

    TMPDIR=<scratch> uv run --extra dev --with playwright python scripts/live_relations.py
    TMPDIR=<scratch> uv run --extra dev --with playwright python scripts/live_relations.py R2 R3
"""

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e
from fake_slack_mcp import Workspace, serve_in_thread
from live_e2e import SLACK_TOKEN, App, cleanup, git, log, transcript, wait_for
from live_parity import setup
from live_wide import (
    MODEL,
    Ctx,
    file_of,
    finished,
    messages,
    now,
    say,
    session,
    settings,
    state,
    task_status,
    titled,
)

PORT = int(os.environ.get("LIVE_RELATIONS_PORT", "21520"))
SLACK_PORT = int(os.environ.get("LIVE_RELATIONS_SLACK_PORT", "21521"))
SHOTS = Path(__file__).resolve().parent.parent / "browser-shots" / "live-relations"


async def thread_of(ctx: Ctx, worker: str) -> dict:
    """The team's thread a worker is in, with its tasks and their relations."""
    team = (await session(ctx, worker))["team"]
    assert team, f"{worker} is in no team"
    return await ctx.app.get(f"/api/teams/{team}/thread")


def relation(thread: dict, kinds: set[str], source: str, target: str = "") -> dict | None:
    return next(
        (
            edge
            for edge in thread["relations"]
            if edge["kind"] in kinds
            and edge["from_task"] == source
            and (not target or edge["to_task"] == target)
        ),
        None,
    )


def row(thread: dict, task_id: str) -> dict:
    return next(item for item in thread["tasks"] if item["id"] == task_id)


async def page_of(ctx: Ctx, browser):
    page = await browser.new_page(viewport={"width": 1280, "height": 1000})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(f"{ctx.app_base}/#token={ctx.app.launch_token}")
    return page, errors


async def r1_parts(ctx: Ctx):
    """Two parts of one request: one team, a fan of two that lands."""
    since = now()
    await say(
        ctx,
        "Two tasks in parallel, no project, one worker each, started now. 'Token Scout': run "
        '`python3 -c "import secrets; print(secrets.token_hex(4))"` once, write its output '
        "into token.txt in your folder, and pass the token on to the 'Token Writer' worker "
        "yourself. 'Token Writer': do not make up a token; wait until the Token Scout's token "
        "reaches you, then write exactly it into result.txt in your folder and report done.",
    )
    await finished(ctx, since, 2, "both parts", 900)
    scout, writer = await titled(ctx, since, "scout"), await titled(ctx, since, "writer")
    thread = await thread_of(ctx, scout["session_id"])
    [group] = [g for g in thread["groups"] if scout["id"] in g["tasks"]]
    log(f"R1: group {group}")
    assert group["kind"] == "parts" and set(group["tasks"]) == {scout["id"], writer["id"]}
    assert (group["landed"], group["total"], group["state"]) == (2, 2, "resolved")
    assert group["landed_at"], "when the last part landed"
    for task in (scout, writer):
        assert row(thread, task["id"])["relation"]["kind"] == "parts", row(thread, task["id"])
    ctx.notes["r1"] = {"team": thread["team"]["id"], "scout": scout["id"]}
    log("R1 passed")


async def r2_waits_on(ctx: Ctx):
    """A task that needs another's result first: it waits on it, then the wait is cleared."""
    from playwright.async_api import async_playwright, expect

    since = now()
    await say(
        ctx,
        "Two tasks, no project, one worker each, started now. 'Slow Builder': run "
        "`sleep 75 && echo built > build.txt` in your folder, then report done. 'Release Note': "
        "it needs the Slow Builder's result, so it must not start before the Slow Builder is "
        "done; then it writes note.txt in its folder containing exactly 'after build' and "
        "reports done.",
    )
    builder, note = await titled(ctx, since, "builder"), await titled(ctx, since, "note")

    async def waiting():
        thread = await thread_of(ctx, note["session_id"])
        edge = relation(thread, {"after"}, builder["id"], note["id"])
        return thread if edge and edge["state"] == "pending" else None

    thread = await wait_for("the note waiting on the builder", waiting, 300, ctx.app)
    log(f"R2: {[(e['kind'], e['state']) for e in thread['relations']]}")
    assert set(thread["team"]["members"]) >= {builder["session_id"], note["session_id"]}
    told = [m for m in await messages(ctx, note["session_id"]) if m["kind"] == "tell"]
    assert not told, "one team by the wait alone"
    chip = row(thread, note["id"])["relation"]
    assert (chip["kind"], chip["side"], chip["state"]) == ("after", "to", "pending"), chip
    team = thread["team"]["id"]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page, errors = await page_of(ctx, browser)
        strip = page.locator(f'#teams-strip .team-row[data-team="{team}"]')
        await expect(strip.locator(".cue")).to_have_text("1 waiting", timeout=20000)
        await strip.click()
        line = page.locator(f'#timeline .trow[data-task="{note["id"]}"] .rel')
        await expect(line).to_have_text(re.compile(r"^waits on Slow Builder"), timeout=20000)
        await page.screenshot(path=SHOTS / "r2-waiting.png")
        await finished(ctx, since, 2, "the builder, then the note", 900)
        await expect(line).to_have_text(
            re.compile(r"^(Slow Builder delivered · unblocked|after Slow Builder)$"), timeout=30000
        )
        await page.screenshot(path=SHOTS / "r2-cleared.png")
        await browser.close()
        assert not errors, errors
    thread = await thread_of(ctx, note["session_id"])
    edge = relation(thread, {"after"}, builder["id"], note["id"])
    assert edge and edge["state"] == "resolved" and edge["resolved_at"], edge
    written = await file_of(ctx, since, "note.txt", "the note", 60)
    assert written.read_text().strip() == "after build"
    log("R2 passed")


async def r3_replaced(ctx: Ctx):
    """Another worker does an open task instead: an edge from the old task to the new."""
    from playwright.async_api import async_playwright, expect

    since = now()
    await say(
        ctx,
        "Start one task now, no project: 'Counter Script': write count.sh in your folder that "
        "prints 1 to 3, run it, then run `sleep 240` before you report done.",
    )
    counter = await titled(ctx, since, "counter")
    await task_status(ctx, counter["id"], {"running"}, "the counter at work", 300)
    await asyncio.sleep(20)
    await say(
        ctx,
        "Change of plan for the 'Counter Script' task: have a new worker do it instead, in "
        "Python this time (count.py printing 1 to 3, run once), and report; the current "
        "worker must not go on with it.",
    )

    async def replaced():
        row_ = await session(ctx, counter["session_id"])
        if not row_.get("team"):
            return None
        thread = await thread_of(ctx, counter["session_id"])
        return thread if relation(thread, {"replaced", "superseded"}, counter["id"]) else None

    thread = await wait_for("the counter's task done instead by another", replaced, 300, ctx.app)
    edge = relation(thread, {"replaced", "superseded"}, counter["id"])
    log(f"R3: {edge}")
    new = row(thread, edge["to_task"])
    assert new["session_id"] != counter["session_id"], "another worker"
    old = row(thread, counter["id"])
    assert (old["relation"]["kind"], old["relation"]["side"]) == (edge["kind"], "from"), old
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page, errors = await page_of(ctx, browser)
        await page.locator(f'#teams-strip .team-row[data-team="{thread["team"]["id"]}"]').click()
        line = page.locator(f'#timeline .trow[data-task="{counter["id"]}"]')
        await expect(line.locator(".rel")).to_have_text(re.compile(r"^handed to "), timeout=20000)
        await expect(line).to_have_class(re.compile(r"\bover\b"))
        await page.screenshot(path=SHOTS / "r3-handed.png")
        await browser.close()
        assert not errors, errors
    log("R3 passed")


async def r4_parts_in_the_page(ctx: Ctx):
    """R1's team in the page: its parts counted, a part's lineage with its caption."""
    from playwright.async_api import async_playwright, expect

    team, scout = ctx.notes["r1"]["team"], ctx.notes["r1"]["scout"]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page, errors = await page_of(ctx, browser)
        await page.locator(f'#teams-strip .team-row[data-team="{team}"]').click()
        view = page.locator("#timeline .team-view")
        await expect(view.locator(".rel-summary .rel-chip").first).to_have_text("2 parts")
        part = view.locator(f'.trow[data-task="{scout}"]')
        await expect(part.locator(".rel")).to_have_text("2 of 2 parts")
        await part.locator(".main").click()
        await expect(view.locator(".lineage .caption")).to_have_text(
            re.compile(r"^All 2 parts landed( · \d+ (min|h))?$")
        )
        await page.screenshot(path=SHOTS / "r4-parts.png")
        await browser.close()
        assert not errors, errors
    log("R4 passed")


SCENARIOS = [r1_parts, r2_waits_on, r3_replaced, r4_parts_in_the_page]


async def main():
    wanted = {name.upper() for name in sys.argv[1:]}
    live_e2e.PORT, live_e2e.SLACK_PORT = PORT, SLACK_PORT
    live_e2e.BASE = f"http://127.0.0.1:{PORT}"
    SHOTS.mkdir(parents=True, exist_ok=True)
    workspace = Workspace()
    serve_in_thread(workspace, SLACK_TOKEN, SLACK_PORT)
    with tempfile.TemporaryDirectory(prefix="cos-relations-") as temporary:
        root = Path(temporary).resolve()
        home, project, truth = setup(root)
        app = App(root / "data", root / "log", {"COPILOT_HOME": str(home)})
        ctx = Ctx(app, root, home, project, truth, workspace)
        ctx.app_base = live_e2e.BASE
        await app.start()
        passed: list[str] = []
        try:
            await settings(ctx, projects=[str(project)], model=MODEL, max_workers=4,
                           mac_notifications=False, digest_time="")  # fmt: skip
            for scenario in SCENARIOS:
                name = scenario.__name__.split("_", 1)[0].upper()
                if wanted and name not in wanted and not (name == "R4" and "R1" in wanted):
                    continue
                log(f"=== {name} {scenario.__name__}")
                await scenario(ctx)
                passed.append(name)
            log(f"LIVE RELATIONS PASSED: {' '.join(passed)}")
        except BaseException:
            log(f"passed before the failure: {' '.join(passed) or 'none'}")
            for found in (await state(ctx))["sessions"]:
                print(f"--- {found['role']} {found['title']} [{found['status']}]")
                print(transcript(await messages(ctx, found["id"]))[-4000:])
            raise
        finally:
            await app.stop()
            SHOTS.joinpath("app.log").write_text((root / "log").read_text())
            await cleanup(root / "data")
            git(project, "worktree", "prune")


if __name__ == "__main__":
    asyncio.run(main())
