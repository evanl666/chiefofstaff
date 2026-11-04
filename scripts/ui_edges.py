"""Browser test of edge cases in the UI, with no accounts: the real web app runs in this process
on a fake Copilot, so the test can create cards and events and check what the page shows.

    uv run --with playwright python scripts/ui_edges.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import uvicorn
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from conftest import FakeClient, FakeSlackMcp, FakeTokens, call_tool, settle  # noqa: E402

from chiefofstaff.runtime import Runtime  # noqa: E402
from chiefofstaff.sources.slack import SlackSource  # noqa: E402
from chiefofstaff.web import create_app  # noqa: E402

PORT = 8798
TOKEN = "ui-edges-token"
BASE = f"http://127.0.0.1:{PORT}"
SHOTS = ROOT / "browser-shots"


def log(text: str):
    print(f"  {text}", flush=True)


def review(repo: str, comments: list[dict]) -> dict:
    target = {"repo": repo, "number": 7, "url": f"https://github.com/{repo}/pull/7",
              "via": "task", "head_sha": "c0ffee"}  # fmt: skip
    if comments:
        target["comments"] = comments
    return target


def seed_memory(runtime) -> dict:
    """How the owner writes, a check in use, cards to review and an accepted result."""
    store = runtime.store
    for section, text in [
        ("slack_dm", "Very short, lowercase, ends with 🙏. E.g. “on it 🙏”."),
        ("pr_review", "Starts with a verdict, then numbered nits; asks for tests. " * 6),
    ]:
        store.execute(
            "INSERT INTO style_versions(section, text, evidence, status, created_at) "
            "VALUES (?,?,'[1,2,3]','active','2026-11-04T10:00:00+00:00')",
            (section, text),
        )
    for index in range(4):
        runtime.habits.add("slack", "slack_dm", f"s{index}", f"ok {index} 🙏")
    evidence = {"failing": {"exit": 1, "output": "svc/poll.py:3:    time.sleep(1)\n"},
                "fixed": {"exit": 0, "output": ""}}  # fmt: skip
    for check_id, status in [("kactive", "active"), ("kcard", "proposed")]:
        store.execute(
            """INSERT INTO checks(id, case_id, repo, name, why, applies_to, command, evidence,
            status, runs, caught, created_at, updated_at)
            VALUES (?, 'c1', 'github.com/acme/svc', ?, 'time.sleep blocks the event loop.',
            '["svc/**/*.py"]', ?, ?, ?, 3, 1, 'now', 'now')""",
            (check_id, f"No time.sleep in async code ({status})",
             "! git grep -nE '^[[:space:]]+time[.]sleep' -- 'svc/*.py'", json.dumps(evidence),
             status),
        )  # fmt: skip
    router = store.get("router_id")
    card = store.create_draft(
        router, "check",
        {"check": "kcard", "name": "No time.sleep in async code", "why":
         "time.sleep blocks the event loop.", "command":
         "! git grep -nE '^[[:space:]]+time[.]sleep' -- 'svc/*.py'", "applies_to":
         ["svc/**/*.py"], "repo": "svc", "task": "Make fetch async", **evidence},
        "Check learned from a mistake",
    )  # fmt: skip
    for lesson_id, text in [("lkeep", "Run a smoke test first"), ("ldrop", "Use tabs")]:
        store.execute(
            "INSERT INTO lessons(id, text, scope, evidence, status, created_at, updated_at) "
            "VALUES (?, ?, '', '[]', 'proposed', 'now', 'now')",
            (lesson_id, text),
        )
    lessons = store.create_draft(
        router, "lessons",
        {"lessons": [runtime.learning.card_item("lkeep"), runtime.learning.card_item("ldrop")]},
        "2 lessons",
    )  # fmt: skip
    store.execute(
        "INSERT INTO tasks(id, session_id, title, status, created_at, updated_at) "
        "VALUES ('tacc', 'wacc', 'Accepted', 'done', 'now', 'now')"
    )
    store.execute(
        "INSERT INTO runs(id, task_id, session_id, status, started_at) "
        "VALUES ('racc', 'tacc', 'wacc', 'done', 'now')"
    )
    store.record_usage("wacc", "worker", "smart-model", 2.5, 1, 10, 10, run_id="racc")
    for draft in (card, lessons):
        runtime.bus.publish("draft", store.draft(draft["id"]))
    runtime.bus.publish("state")
    return {"card": card, "lessons": lessons}


async def memory(runtime, browser, failures: list[str]):
    """The check card, the lessons card, Settings → How you work, and Usage."""
    store = runtime.store
    seeded = seed_memory(runtime)
    for scheme in ("light", "dark"):
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900}, color_scheme=scheme
        )
        page = await context.new_page()
        page.on("pageerror", lambda error: failures.append(str(error)))
        page.on("console", lambda m: failures.append(m.text) if m.type == "error" else None)
        await page.goto(f"{BASE}/#token={TOKEN}")
        card = page.locator(f'.card.draft[data-id="{seeded["card"]["id"]}"]')
        await expect(card).to_be_visible(timeout=15000)
        await expect(card.locator("pre")).to_contain_text("git grep")
        await expect(card).to_contain_text("Fails on the code that had the mistake (exit 1)")
        await expect(card).to_contain_text("Passes on the fixed code (exit 0)")
        await card.scroll_into_view_if_needed()
        await card.screenshot(path=SHOTS / f"ui-edges-check-{scheme}.png")
        await page.locator("#settings-button").click()
        section = page.locator("#settings-dialog")
        await expect(section.locator("#styles li")).to_have_count(2)
        await expect(section.locator("#checks li")).to_have_count(1)
        await expect(section.locator("#learn-status")).to_contain_text("4 Slack DMs")
        await section.locator("#styles").scroll_into_view_if_needed()
        await section.screenshot(path=SHOTS / f"ui-edges-how-you-work-{scheme}.png")
        await context.close()

    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    card = page.locator(f'.card.draft[data-id="{seeded["card"]["id"]}"]')
    await card.get_by_role("button", name="Use this check").click()
    await expect(card).to_have_count(0, timeout=10000)
    assert store.one("SELECT status FROM checks WHERE id='kcard'")["status"] == "active"
    lessons = page.locator(f'.card.draft[data-id="{seeded["lessons"]["id"]}"]')
    await lessons.locator('input[type="checkbox"]').last.uncheck()
    await lessons.get_by_role("button", name="Keep ticked").click()
    await expect(lessons).to_have_count(0, timeout=10000)
    statuses = {row["id"]: row["status"] for row in store.all("SELECT id, status FROM lessons")}
    assert statuses == {"lkeep": "active", "ldrop": "rejected"}, statuses
    assert not [d for d in store.drafts() if d["kind"] == "lessons"], "both were seen"
    log("check card: proof shown, used; lessons card: unticked is dropped, not re-asked")

    await page.locator("#settings-button").click()
    dialog = page.locator("#settings-dialog")
    await expect(dialog.locator("#checks li")).to_have_count(2)
    first = dialog.locator("#checks li").first
    await first.get_by_role("button", name="Turn off").click()
    await expect(first).to_have_class("off")
    await expect(first.get_by_role("button", name="Turn on")).to_be_visible()
    await (
        dialog.locator("#styles li", has_text="Slack direct messages")
        .get_by_role("button", name="Undo")
        .click()
    )
    await expect(dialog.locator("#styles li")).to_have_count(1)
    assert "slack_dm" not in runtime.habits.styles()
    await page.locator("#settings-cancel").click()
    log("settings: a check turned off, a style undone")

    await page.locator("#usage-button").click()
    await page.locator('#usage-dialog [data-range="30"]').click()
    usage = page.locator("#usage-tables")
    await expect(usage).to_contain_text("Per accepted result")
    await expect(usage).to_contain_text("2.5 credits each")
    log("usage: what an accepted result cost, by model")
    await context.close()


async def main():
    with tempfile.TemporaryDirectory(prefix="cos-ui-") as temporary:
        holder: dict = {}

        def factory(store, bus):
            async def notifier(title, text):
                pass

            runtime = Runtime(
                store, bus, backend_factory=lambda settings: FakeClient(), notifier=notifier
            )
            runtime.slack = SlackSource(store, runtime, tokens=FakeTokens(""), mcp=FakeSlackMcp())
            holder["runtime"] = runtime
            return runtime

        app = create_app(Path(temporary) / "data", TOKEN, PORT, runtime_factory=factory)
        server = uvicorn.Server(uvicorn.Config(app, port=PORT, log_level="warning"))
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        runtime = holder["runtime"]
        for _ in range(100):
            if runtime.ready():
                break
            await asyncio.sleep(0.1)
        store = runtime.store
        router = store.get("router_id")
        spawned = await call_tool(
            runtime, "spawn_worker", router, title="Review the exports PR",
            instructions="Review acme/exports#7", why_new="test",
        )  # fmt: skip
        worker = json.loads(spawned.text_result_for_llm)["session_id"]
        await settle(runtime)

        plain = store.create_draft(worker, "github", review("acme/plain", []), "LGTM.")
        many = [
            {"path": f"src/module_{i}.py", "line": 10 + i, "side": "RIGHT", "body": f"Note {i}."}
            for i in range(18)
        ]
        many.append(
            {"path": "src/old.py", "line": 4, "side": "LEFT", "body": "Removed on purpose?"}
        )
        many.append({"path": "src/range.py", "start_line": 140, "line": 146, "side": "RIGHT",
                     "body": "Long note. " * 200})  # fmt: skip
        busy = store.create_draft(worker, "github", review("acme/busy", many), "Twenty notes.")
        for draft in (plain, busy):
            runtime.bus.publish("draft", store.draft(draft["id"]))

        failures: list[str] = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            for scheme in ("light", "dark"):
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 900}, color_scheme=scheme
                )
                page = await context.new_page()
                page.on("pageerror", lambda error: failures.append(str(error)))
                page.on(
                    "console",
                    lambda m: failures.append(m.text) if m.type == "error" else None,
                )
                await page.goto(f"{BASE}/#token={TOKEN}")
                await expect(page.locator("#application")).to_be_visible()
                plain_card = page.locator(f'.card.draft[data-id="{plain["id"]}"]')
                busy_card = page.locator(f'.card.draft[data-id="{busy["id"]}"]')
                await expect(plain_card).to_be_visible(timeout=15000)
                assert await plain_card.locator(".inline-comment").count() == 0
                assert "line comment" not in (await plain_card.inner_text())
                await expect(busy_card.locator(".inline-comment")).to_have_count(20)
                text = await busy_card.inner_text()
                assert "src/old.py:4 (old)" in text, text[:400]
                assert "src/range.py:140-146" in text, text[:400]
                wide = await busy_card.evaluate("node => node.scrollWidth > node.clientWidth")
                assert not wide, "a long comment does not widen the card"
                boxes = busy_card.locator(".inline-comment textarea")
                assert await boxes.last.get_attribute("rows") == "6", "a long comment scrolls"
                assert await boxes.first.get_attribute("rows") == "1", "a short one is one line"
                await busy_card.scroll_into_view_if_needed()
                await page.screenshot(path=SHOTS / f"ui-edges-review-{scheme}.png")
                await context.close()
            log("review cards: no comments, twenty, an old-side one, a range, a long one")

            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            page = await context.new_page()
            page.on("pageerror", lambda error: failures.append(str(error)))
            await page.goto(f"{BASE}/#token={TOKEN}")
            busy_card = page.locator(f'.card.draft[data-id="{busy["id"]}"]')
            await busy_card.get_by_role("button", name="Reject").click()
            await busy_card.locator("input").fill("Too many nits")
            await busy_card.get_by_role("button", name="Reject").click()
            await expect(busy_card).to_have_count(0, timeout=10000)
            assert store.draft(busy["id"])["status"] == "rejected"
            log("rejecting a review with comments removes its card and posts nothing")

            await call_tool(runtime, "report", worker, status="done", summary="Review done.")
            await settle(runtime)
            await page.locator("#session-list .item", has_text="Review the exports PR").click()
            timeline = page.locator("#timeline")
            await expect(timeline).to_contain_text("Review done.")
            answer = "The exporter drops rows past 10k because the cursor resets. " * 6
            runtime.add_message(worker, "assistant", answer)
            runtime.workers.complete_report(worker)
            report = timeline.locator(".card.report")
            await expect(report).to_contain_text("the cursor resets", timeout=10000)
            await expect(report).to_have_count(1)  # Completed in place, not repeated.
            log("a one-line report gets the answer in place in the timeline")
            await context.close()

            await memory(runtime, browser, failures)
            await browser.close()
        server.should_exit = True
        await serving
        if failures:
            raise AssertionError(f"browser errors: {failures}")
    print("UI EDGE CASES PASSED")


if __name__ == "__main__":
    asyncio.run(main())
