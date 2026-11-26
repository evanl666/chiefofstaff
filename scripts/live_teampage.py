"""Live test of a team's page (team-page-v4, phase 1 "Run the team") on the real Copilot: a real
Router run forms the team; the owner runs it from its page.

  T1  A team forms: one request in three parts (one at work, one asking the owner, one that
      starts after the first) is one team of exactly those three; its name and each part's
      are sensible (short, whole words, never the raw request, distinct).
  T2  Tell the team: written in the page's composer, it reaches the part at work now (its
      message, read), waits for the part not started (`teamnote:`), leaves one passive copy for
      the Router (no turn of its own), and the page says who got it.
  T3  The answer in the brief: the part's question is the brief's one need; the option chosen
      there reaches its worker.
  T4  Review in place: the first part's result reviewed and accepted on the plan; the part
      after it (started once that result was ready), was first given the owner's word told
      before it started.

Uses your signed-in Copilot CLI (a scratch COPILOT_HOME, as live_wide does), consumes premium
requests (LIVE_COPILOT_MODEL, claude-sonnet-5.5 by default) and takes about 15 minutes. Ports:
LIVE_TEAMPAGE_PORT (21540) for the app, LIVE_TEAMPAGE_SLACK_PORT (21541) for the fake Slack.
Set TMPDIR to a scratch folder of yours: its temporary folders go there.

    TMPDIR=<scratch> uv run --extra dev --with playwright python scripts/live_teampage.py
"""

import asyncio
import json
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
from live_relations import home_row, lite_team, page_of
from live_wide import MODEL, Ctx, file_of, messages, now, say, settings, state, titled

from chiefofstaff.team_names import columns

PORT = int(os.environ.get("LIVE_TEAMPAGE_PORT", "21540"))
SLACK_PORT = int(os.environ.get("LIVE_TEAMPAGE_SLACK_PORT", "21541"))
SHOTS = Path(__file__).resolve().parent.parent / "browser-shots" / "live-teampage"
REQUEST = (
    "A small glossary, in three parts, no project, one worker each, all started now. "
    "'Glossary Draft': first run `sleep 120` in your folder, then write glossary.md there with "
    "three wind names (zephyr, sirocco, mistral) and a one-line meaning each, and report done. "
    "'Glossary Check': it needs the draft, so it must not start before Glossary Draft is done; "
    "then it reads the draft and writes check.txt in its folder: 'ok' or the fixes, and reports "
    "done. 'Glossary Cover' works with 'Glossary Draft' on the same glossary, at the same time: "
    "before anything else, use ask_decision to ask me whether the cover line should be formal or "
    "casual (the two options), wait for my answer, then write cover.txt in your folder with one "
    "line in that tone naming the draft's three winds, and report done."
)
TOLD = "Team note TP-7: end every file you write with a last line that says exactly TP-7."
# Where a message is once its worker's conversation shows it (the page's "✓ read").
READ = {"received", "processed"}
# A word that may not end a name.
DANGLING = re.compile(r"\s(?:a|an|the|of|for|to|in|on|at|with|and|or|by|from)$", re.IGNORECASE)


def sensible(name: str, most: int, sources: list[str], raw: str = "") -> None:
    """A name as a person would write it: short, not the raw request, never ending on "the" or
    in the middle of a word (an ellipsis only after a whole word of what it was made from)."""
    assert name and name == name.strip() and "\n" not in name, repr(name)
    assert 2 <= columns(name) <= most, (name, columns(name), most)
    assert not raw or name != raw.strip(), name
    assert not DANGLING.search(name.rstrip("\u2026")), name
    if name.endswith("\u2026"):
        kept = name[:-1]
        after = [
            source[source.find(kept) + len(kept) :][:1] for source in sources if kept in source
        ]
        assert after, (name, sources)
        assert all(not char.isalnum() or ord(char) > 0x2E80 for char in after), (name, after)


async def team_data(ctx: Ctx, team: str) -> dict:
    return await ctx.app.get(f"/api/teams/{team}")


async def t1_a_team_forms(ctx: Ctx):
    since = now()
    await say(ctx, REQUEST)
    draft = await titled(ctx, since, "draft")
    check = await titled(ctx, since, "check")
    cover = await titled(ctx, since, "cover")
    tasks = {draft["id"], check["id"], cover["id"]}
    assert len(tasks) == 3, (draft, check, cover)

    async def formed():
        team = await lite_team(ctx, draft["id"])
        return team if team and {m["task"] for m in team["members"]} == tasks else None

    team = await wait_for("one team of the three parts", formed, 300, ctx.app)
    names = [member["name"] for member in team["members"]]
    log(f"T1: {team['goal']!r}: {names}")
    titles = [t["title"] for t in (draft, check, cover)]
    sensible(team["goal"], 48, [REQUEST, *titles], raw=REQUEST)
    assert len(set(names)) == len(names), names
    for member in team["members"]:
        title = next(t["title"] for t in (draft, check, cover) if t["id"] == member["task"])
        sensible(member["name"], 24, [title, REQUEST])
    assert len({m["avatar"] for m in team["members"]}) == len(names), team["members"]
    ctx.notes["team"] = {
        "id": team["id"],
        "draft": draft,
        "check": check,
        "cover": cover,
        "since": since,
    }
    log("T1 passed")


async def t2_t3_t4_run_the_team(ctx: Ctx):
    from playwright.async_api import async_playwright, expect

    found = ctx.notes["team"]
    team, draft, check, cover = found["id"], found["draft"], found["check"], found["cover"]
    router = (await state(ctx))["router_id"]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page, errors = await page_of(ctx, browser)
        await expect(home_row(page, team)).to_be_visible(timeout=60000)
        await home_row(page, team).click()
        await expect(page.locator("#timeline .tp-plan .tp-part")).to_have_count(3, timeout=20000)
        await page.screenshot(path=SHOTS / "t1-team-page.png")

        # T2: the draft at work, the check not started: told from the composer.
        async def standing():
            draft_now = await ctx.app.get(f"/api/tasks/{draft['id']}")
            check_now = await ctx.app.get(f"/api/tasks/{check['id']}")
            if draft_now["status"] == "running" and check_now["status"] == "queued":
                return True
            assert draft_now["status"] in {"queued", "running"}, draft_now["status"]
            return None

        await wait_for("the draft at work, the check not started", standing, 300, ctx.app)
        to = page.locator("#composer-to")
        await expect(to).to_contain_text("(when it starts)")
        await page.locator("#composer-input").fill(TOLD)
        await page.locator("#composer-input").press("Enter")
        sent = page.locator("#timeline .tp-sent")
        await expect(sent).to_contain_text("You told the team at", timeout=20000)
        await page.screenshot(path=SHOTS / "t2-told.png")
        told = [
            m for m in await messages(ctx, draft["session_id"])
            if m["kind"] == "owner" and TOLD in m["text"]
        ]  # fmt: skip
        assert len(told) == 1 and told[0]["meta"].get("team") == team, told
        [entry] = [e for e in (await team_data(ctx, team))["tells"] if e["text"] == TOLD]
        how = {item["task"]: item["how"] for item in entry["to"]}
        assert how.get(check["id"]) == "start", entry
        copies = [
            m for m in await messages(ctx, router)
            if m["kind"] == "owner" and m["meta"].get("to") == f"team:{team}"
        ]  # fmt: skip
        assert len(copies) == 1 and copies[0]["meta"].get("deferred"), copies

        async def read():
            data = await team_data(ctx, team)
            [entry] = [e for e in data["tells"] if e["text"] == TOLD]
            state_ = {item["task"]: item["delivery"] for item in entry["to"]}
            return state_ if state_.get(draft["id"]) in READ else None

        log(f"T2: delivered {await wait_for('the draft reading it', read, 600, ctx.app)}")
        log("T2 passed (the part not started: at its start, T4)")

        # T3: the cover's question, the brief's one need, answered there.
        brief = page.locator("#timeline .tp-brief")
        await expect(brief.locator(".say")).to_contain_text("asks you", timeout=600000)
        await page.screenshot(path=SHOTS / "t3-asked.png")
        option = brief.locator(".option", has_text=re.compile("casual", re.IGNORECASE))
        if await option.count():
            await option.first.click()
        else:
            await brief.locator("input.own").fill("casual")
            await brief.locator("input.own").press("Enter")

        async def answered():
            got = [
                m for m in await messages(ctx, cover["session_id"])
                if m["kind"] in {"owner", "answer", "notice"} and "casual" in m["text"].lower()
            ]  # fmt: skip
            return got or None

        await wait_for("the cover hearing the answer", answered, 120, ctx.app)
        written = await file_of(ctx, found["since"], "cover.txt", "the cover", 900)
        log(f"T3: cover.txt {written.read_text()[:200]!r}")
        log("T3 passed")

        # T4: the draft's result reviewed and accepted in place; the check starts with the
        # owner's word in what it is first given.
        review = page.locator(f'.tp-part[data-part="{draft["id"]}"] .act button', has_text="Review")
        await expect(review).to_be_visible(timeout=900000)
        await review.click()
        drawer = page.locator("#timeline .tp-review")
        await expect(drawer.locator("button").first).to_have_text("Accept")
        await page.screenshot(path=SHOTS / "t4-review.png")
        await drawer.locator("button", has_text="Accept").click()

        async def accepted():
            task = await ctx.app.get(f"/api/tasks/{draft['id']}")
            return task if task["status"] == "done" else None

        await wait_for("the draft accepted", accepted, 60, ctx.app)

        async def started():
            given = [
                m.get("meta", {}).get("prompt") or m["text"]
                for m in await messages(ctx, check["session_id"])
                if m["kind"] in {"router", "owner"}
            ]
            return given if any(TOLD in text for text in given) else None

        await wait_for("the check given the owner's word at its start", started, 600, ctx.app)
        await page.screenshot(path=SHOTS / "t4-accepted.png")
        await browser.close()
        assert not errors, errors
    log("T4 passed")


SCENARIOS = [t1_a_team_forms, t2_t3_t4_run_the_team]


async def main():
    live_e2e.PORT, live_e2e.SLACK_PORT = PORT, SLACK_PORT
    live_e2e.BASE = f"http://127.0.0.1:{PORT}"
    SHOTS.mkdir(parents=True, exist_ok=True)
    workspace = Workspace()
    serve_in_thread(workspace, SLACK_TOKEN, SLACK_PORT)
    with tempfile.TemporaryDirectory(prefix="cos-teampage-") as temporary:
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
                log(f"=== {scenario.__name__}")
                await scenario(ctx)
                passed.append(scenario.__name__)
            log(f"LIVE TEAM PAGE PASSED: {' '.join(passed)}")
        except BaseException:
            log(f"passed before the failure: {' '.join(passed) or 'none'}")
            for found in (await state(ctx))["sessions"]:
                print(f"--- {found['role']} {found['title']} [{found['status']}]")
                print(transcript(await messages(ctx, found["id"]))[-4000:])
            print(json.dumps((await state(ctx)).get("teams", []), ensure_ascii=False)[:4000])
            raise
        finally:
            await app.stop()
            SHOTS.joinpath("app.log").write_text((root / "log").read_text())
            await cleanup(live_e2e.data_dir(root))
            git(project, "worktree", "prune")


if __name__ == "__main__":
    asyncio.run(main())
