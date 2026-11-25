"""Live test of task relationships (E130) and Teams lite (teams-v2.md) on the real Copilot: a real
Router run makes the relations; the Router's team thread has them as in 0.1.56 (GET
/api/teams/<id>/thread); the owner's Teams (the state's `teams`, Home's Teams rows, a team's page,
a worker's Team or Notes tab) show them.

  R1  Two parts of one request, one passing a token to the other: one team, a fan of two parts
      that lands (2 of 2), each row's one relation its part; one Teams lite team of the two,
      each part with a name and an avatar of its own.
  R2  A task that needs another's result before it starts (`after`): while the other runs it
      waits on it (Home's Teams row: "1 working · 1 up next", or its newest change; the team's
      page: the plan's "Starts after …";
      its Team tab's lineage: "… waits on …"), the two workers one team though they never
      message, a chain (no fan of parts; neither told the other works at the same time); once
      the other is done the wait is cleared. Accepted, the team is Done (the calm Done row);
      seen, it stays until its close button takes it off Home.
  R3  Another worker does an open task instead (`replaces`), asked in a later message: an edge
      from the old task to the new; no Teams lite team (one part, the other request's work);
      the old task's Notes tab has the new one in its lineage.
  R4  In the browser: R1's team page (team-page-v4: its request as the goal, "Working · …" or
      "Done · …", the brief, a row a part on the plan, what they told each other in the notes
      folded at the bottom "A → B"), the part's Team tab with Open team.
  R5  One request in three parts, one of them a reused worker's new task (message_session): one
      Teams lite team of three, names of their own; what the workers and the Router were given
      as in 0.1.56 (no Teams lite id anywhere; the Router's team digest in its own words).
  R6  Two requests whose workers talk (one asks the other while it works): no Teams lite team;
      Nudge on that question (POST /api/questions/<id>/nudge) reaches the worker asked.

Uses your signed-in Copilot CLI (a scratch COPILOT_HOME, as live_wide does), consumes premium
requests (LIVE_COPILOT_MODEL, claude-sonnet-5.5 by default) and takes about 30 minutes. Ports:
LIVE_RELATIONS_PORT (21520) for the app, LIVE_RELATIONS_SLACK_PORT (21521) for the fake Slack.
Set TMPDIR to a scratch folder of yours: its temporary folders go there.

    TMPDIR=<scratch> uv run --extra dev --with playwright python scripts/live_relations.py
    TMPDIR=<scratch> uv run --extra dev --with playwright python scripts/live_relations.py R5 R6
"""

import asyncio
import contextlib
import json
import os
import re
import sqlite3
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
    prompts_of,
    router_given,
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
# A Teams lite team's id: the owner's, never in what a worker or the Router is given.
LITE_ID = re.compile(r"\bteam-\d+-t[0-9a-f]{7}\b")


async def thread_of(ctx: Ctx, worker: str) -> dict:
    """The Router's team thread a worker is in (0.1.56's), with its tasks and their relations."""
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


async def lite_team(ctx: Ctx, task_id: str) -> dict | None:
    """The owner's team (Teams lite) a task is a part of, as the state has it."""
    for team in (await state(ctx)).get("teams", []):
        if any(task_id == m["task"] or task_id in m["tasks"] for m in team["members"]):
            return team
    return None


def parts_named(team: dict) -> list[str]:
    """Its parts' names, each its own, each with an avatar of its own."""
    names = [member["name"] for member in team["members"]]
    assert all(names) and len(set(names)) == len(names), names
    assert len({m["avatar"] for m in team["members"]}) == len(names), team["members"]
    return names


async def page_of(ctx: Ctx, browser):
    page = await browser.new_page(viewport={"width": 1280, "height": 1000})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(f"{ctx.app_base}/#token={ctx.app.launch_token}")
    return page, errors


def home_row(page, team: str):
    return page.locator(f'#tasks-teams .team2-row[data-team="{team}"]')


async def worker_tab(page, worker: str, tab: str):
    """A worker's chat on one of its tabs ("Team" for a part of a team, else "Notes")."""
    await page.evaluate(f"import('/static/session.js').then((m) => m.select({json.dumps(worker)}))")
    await page.locator("#worker-tabs").get_by_role("tab", name=re.compile(f"^{tab}")).click()


async def r1_parts(ctx: Ctx):
    """Two parts of one request: one team, a fan of two that lands; one Teams lite team."""
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
    # The owner's team: the request's two parts (the Router named one in the other's
    # works_with), each named and drawn apart.
    team = await lite_team(ctx, scout["id"])
    assert team, f"no Teams lite team: {(await state(ctx)).get('teams')}"
    assert {m["task"] for m in team["members"]} == {scout["id"], writer["id"]}, team["members"]
    log(f"R1: Teams lite {team['goal']!r}: {parts_named(team)}")
    ctx.notes["r1"] = {
        "team": thread["team"]["id"],
        "scout": scout["id"],
        "lite": team["id"],
        "writer": writer["session_id"],
    }
    log("R1 passed")


async def r2_waits_on(ctx: Ctx):
    """A task that needs another's result first: it waits on it, then the wait is cleared;
    accepted, the team is Done, seen, and closed."""
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
    # A chain, not a request's parts (r81): no fan of the two, and neither hears the other
    # works at the same time (the builder may hear the note starts once it is done).
    assert not [g for g in thread["groups"] if note["id"] in g["tasks"]], thread["groups"]
    built, noted = [await messages(ctx, w["session_id"]) for w in (builder, note)]
    heard = [m["meta"].get("prompt") or m["text"] for m in built
             if m["kind"] == "notice" and "Release Note" in m["text"]]  # fmt: skip
    first = [m["meta"].get("prompt") or "" for m in noted if m["kind"] == "router"][:1]
    log(f"R2: the builder heard {heard}")
    assert not any("at the same time" in text for text in heard + first), (heard, first)
    # The owner's team: one request, the wait its line.
    lite = await lite_team(ctx, note["id"])
    assert lite and {m["task"] for m in lite["members"]} == {builder["id"], note["id"]}, lite
    assert [(e["kind"], e["state"]) for e in lite["edges"]] == [("after", "pending")], lite
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page, errors = await page_of(ctx, browser)
        home = home_row(page, lite["id"])
        await expect(home.locator(".status")).to_have_text(
            re.compile(r"^(?:1 working · 1 up next|.+ · (?:just now|.+ ago))$"), timeout=20000
        )
        await expect(home.locator(".segs i")).to_have_count(2)
        await page.locator("#tasks-teams").screenshot(path=SHOTS / "r2-home.png")
        await home.click()
        nodes = page.locator("#timeline .team-page .tp-plan .tp-part")
        await expect(nodes).to_have_count(2)
        await expect(nodes.nth(1).locator(".status")).to_have_text(re.compile(r"^Starts after "))
        await page.screenshot(path=SHOTS / "r2-team-page.png")
        await worker_tab(page, note["session_id"], "Team")
        lineage = page.locator("#timeline > .team-lineage .lineage")
        await expect(lineage.locator(".caption")).to_have_text(
            re.compile(r"waits on .*Builder"), timeout=20000
        )
        await page.screenshot(path=SHOTS / "r2-waiting.png")
        await finished(ctx, since, 2, "the builder, then the note", 900)
        await expect(lineage.locator(".llink.pending")).to_have_count(0, timeout=30000)
        await page.screenshot(path=SHOTS / "r2-cleared.png")
        # Accepted: Done, a calm row; in sight, it is seen (and stays); closed, it is off Home.
        for task in (builder, note):
            if (await ctx.app.get(f"/api/tasks/{task['id']}"))["status"] == "review":
                await ctx.app.post(f"/api/tasks/{task['id']}/accept")
        await page.locator("#back-home").click()
        await expect(home.locator(".status")).to_have_text(
            re.compile(r"^Done · 2 parts"), timeout=30000
        )
        await expect(home.locator(".frac")).to_have_text("")

        async def seen():
            team = await lite_team(ctx, note["id"])
            return team if team and team.get("seen") else None

        await wait_for("the done team seen", seen, 60, ctx.app)
        await expect(home).to_be_visible()
        await page.locator("#tasks-teams").screenshot(path=SHOTS / "r2-done.png")
        await page.locator(
            f'#tasks-teams .team2-wrap:has([data-team="{lite["id"]}"]) .dismiss'
        ).click()
        await expect(home).to_have_count(0)
        await expect(page.locator("#tasks-teams .past-teams summary")).to_have_text(
            re.compile(r"^Past teams · \d+$")
        )
        await page.reload()
        await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)
        await expect(home_row(page, lite["id"])).to_have_count(0)
        await browser.close()
        assert not errors, errors
    thread = await thread_of(ctx, note["session_id"])
    edge = relation(thread, {"after"}, builder["id"], note["id"])
    assert edge and edge["state"] == "resolved" and edge["resolved_at"], edge
    written = await file_of(ctx, since, "note.txt", "the note", 60)
    assert written.read_text().strip() == "after build"
    log("R2 passed")


async def r3_replaced(ctx: Ctx):
    """Another worker does an open task instead: an edge from the old task to the new; asked
    in a later message, no Teams lite team."""
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
    # Two of the owner's messages: no team of theirs.
    for task in (counter["id"], new["id"]):
        assert not await lite_team(ctx, task), await lite_team(ctx, task)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page, errors = await page_of(ctx, browser)
        await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)
        await worker_tab(page, counter["session_id"], "Notes")
        lineage = page.locator("#timeline > .team-lineage .lineage")
        await expect(lineage.locator(f'.lnode[data-task="{new["id"]}"]')).to_have_count(
            1, timeout=20000
        )
        await page.screenshot(path=SHOTS / "r3-handed.png")
        await browser.close()
        assert not errors, errors
    log("R3 passed")


async def r4_parts_in_the_page(ctx: Ctx):
    """R1's team page: its request, a node a part, their line, what they handed each other;
    the part's Team tab with Open team."""
    from playwright.async_api import async_playwright, expect

    lite, writer = ctx.notes["r1"]["lite"], ctx.notes["r1"]["writer"]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page, errors = await page_of(ctx, browser)
        await home_row(page, lite).click()
        await expect(page.locator("#session-meta")).to_have_text(
            re.compile(r"^(Working|Waiting on you|Done)\b")
        )
        team_page = page.locator("#timeline .team-page")
        await expect(team_page.locator(".tp-brief")).to_be_visible()
        await expect(team_page.locator(".tp-plan .tp-part")).to_have_count(2)
        notes = team_page.locator(".tp-talk.notes")
        await expect(notes.locator("summary")).to_contain_text("What the parts told each other")
        await notes.locator("summary").click()
        await expect(notes.locator(".who").first).to_have_text(re.compile(r"^.+→.+$"))
        await page.screenshot(path=SHOTS / "r4-team-page.png")
        await worker_tab(page, writer, "Team")
        link = page.locator("#worker-tabs .team-link")
        await expect(link).to_have_text("Open team")
        await link.click()
        await expect(page.locator("#timeline .team-page")).to_be_visible()
        await browser.close()
        assert not errors, errors
    log("R4 passed")


async def r5_a_reused_worker_in_a_team(ctx: Ctx):
    """One request in three parts, one of them a reused worker's new task: one team of three;
    what workers and the Router were given has no Teams lite in it."""
    since = now()
    await say(
        ctx,
        "Start a worker titled 'Word Smith' (no project): write the words 'alpha beta gamma "
        "delta' into words.txt in its folder and report done.",
    )
    smith = await titled(ctx, since, "smith")
    await finished(ctx, since, 1, "the word smith", 600)
    later = now()
    await say(
        ctx,
        "Three parts, all started now. (1) Give the existing 'Word Smith' worker a new task "
        "(that same worker, not a new one): 'Word Count': count the words in its words.txt and "
        "pass the number itself to the 'Count Checker' worker. (2) A new worker, 'Count "
        "Checker', working with Word Count: it waits for Word Count's number, then writes it "
        "into checked.txt in its folder. (3) A new worker, 'Count Report': it needs both "
        "results, so it must not start before Word Count and Count Checker are done; then it "
        "writes 'reported' into report.txt in its folder.",
    )
    await finished(ctx, later, 3, "the three parts", 1200)
    count = await titled(ctx, later, "count")
    checker, report = await titled(ctx, later, "checker"), await titled(ctx, later, "report")
    assert count["session_id"] == smith["session_id"], "the Word Smith, reused"
    team = await lite_team(ctx, count["id"])
    assert team, f"no Teams lite team: {(await state(ctx)).get('teams')}"
    members = {m["task"] for m in team["members"]}
    assert members == {count["id"], checker["id"], report["id"]}, team["members"]
    assert smith["id"] not in {t for m in team["members"] for t in m["tasks"]}, "its other task"
    log(f"R5: Teams lite {team['goal']!r}: {parts_named(team)}")
    # What the workers and the Router were given is as 0.1.56 gives it.
    router = (await state(ctx))["router_id"]
    given = router_given(ctx, router, await messages(ctx, router))
    for worker in {smith["session_id"], checker["session_id"], report["session_id"]}:
        rows = await messages(ctx, worker)
        assert not LITE_ID.search(prompts_of(rows)), f"a Teams lite id given to {worker}"
    assert not LITE_ID.search(given), "a Teams lite id given to the Router"
    digests = re.findall(
        r'<message from="system" type="team-digest">(.*?)</message>', given, re.DOTALL
    )
    log(f"R5: {len(digests)} team digest(s)")
    for digest in digests:
        for line in digest.strip().splitlines()[1:]:
            assert re.match(r"^(- team-[0-9a-f]{8} \(|  )", line), line
    log("R5 passed")


async def r6_two_requests_talk(ctx: Ctx):
    """Workers of two requests: one asks the other while it works; no team; Nudge on that
    question reaches the one asked."""
    since = now()
    await say(
        ctx,
        "Start a worker titled 'Glossary Keeper' (no project): remember that the term "
        "'zephyr' means 'west wind'; then run `sleep 180` in its folder before you report done.",
    )
    keeper = await titled(ctx, since, "keeper")

    async def sleeping():
        row_ = await session(ctx, keeper["session_id"])
        return row_ if "sleep 180" in (row_.get("step_command") or "") else None

    await wait_for("the keeper at work", sleeping, 300, ctx.app)
    later = now()
    await say(
        ctx,
        "Now start a separate new worker titled 'Glossary Asker', not the Keeper: it asks the "
        "Glossary Keeper with ask_session what 'zephyr' means and writes the answer into "
        "term.txt in its own folder. Do not tell it the answer yourself.",
    )
    asker = await titled(ctx, later, "asker")

    def open_question() -> str:
        database = ctx.app.data / "chiefofstaff.db"
        with contextlib.closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
            found = db.execute(
                "SELECT id FROM questions WHERE asker=? AND status='open'", (asker["session_id"],)
            ).fetchone()
        return found[0] if found else ""

    async def asked():
        return open_question() or None

    question = await wait_for("the asker's question", asked, 600, ctx.app)
    said = await ctx.app.post(f"/api/questions/{question}/nudge")
    log(f"R6: nudged {question}: {said['said'][:120]!r}")
    nudged = [m for m in await messages(ctx, keeper["session_id"]) if m["meta"].get("nudge")]
    assert nudged and nudged[0]["meta"]["nudge"] == question, nudged
    written = await file_of(ctx, later, "term.txt", "the asker's answer", 900)
    assert "west wind" in written.read_text().lower(), written.read_text()
    for task in (keeper["id"], asker["id"]):
        assert not await lite_team(ctx, task), await lite_team(ctx, task)
    log("R6 passed")


SCENARIOS = [
    r1_parts, r2_waits_on, r3_replaced, r4_parts_in_the_page, r5_a_reused_worker_in_a_team,
    r6_two_requests_talk,
]  # fmt: skip


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
        app = App(live_e2e.data_dir(root), root / "log", {"COPILOT_HOME": str(home)})
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
            await cleanup(live_e2e.data_dir(root))
            git(project, "worktree", "prune")


if __name__ == "__main__":
    asyncio.run(main())
