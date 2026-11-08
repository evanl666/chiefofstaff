"""Browser test of the demo: Home through a whole scenario, with no accounts.

    uv run --with playwright python scripts/demo_smoke.py

Starts `chiefofstaff --demo`, then drives the UI like an owner: watches tasks start and wait
on each other, answers a worker's question, approves a Slack reply, checks the receipts on the
finished work, copies a report, follows up on a task and focuses for a while. Screenshots go to
browser-shots/ (light and dark).
"""

import asyncio
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "browser-shots"
PORT = int(os.environ.get("DEMO_SMOKE_PORT", "8797"))


def log(text: str):
    print(f"  {text}", flush=True)


async def start() -> tuple[subprocess.Popen, str]:
    process = subprocess.Popen(
        [sys.executable, "-m", "chiefofstaff", "--demo", "--no-browser", "--port", str(PORT)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for _ in range(200):
        line = await asyncio.to_thread(process.stdout.readline)
        if match := re.search(r"(http://127\.0\.0\.1:\d+/#token=\S+)", line):
            # The server's log, shown if the test fails.
            process.output = []  # type: ignore[attr-defined]
            threading.Thread(
                target=lambda: process.output.extend(process.stdout), daemon=True
            ).start()
            return process, match.group(1)
    process.kill()
    raise RuntimeError("The demo did not start.")


async def run(scheme: str):
    process, url = await start()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context(
                viewport={"width": 1280, "height": 860}, color_scheme=scheme
            )
            await context.grant_permissions(["clipboard-read", "clipboard-write"])
            page = await context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(url)

            # Home: the chat with the Router, and beside it Needs you and the task board.
            await expect(page.locator("#conversation")).to_be_visible()
            await expect(page.locator("#panel")).to_be_visible()
            await expect(page.locator("#session-list .item", has_text="Chat")).to_have_count(0)
            board = page.locator("#tasks-running .card.task, #tasks-monitoring .card.task")
            await expect(board).to_have_count(3, timeout=20000)
            queued = page.locator("#tasks-monitoring .card.task.marker-queued")
            await expect(queued).to_contain_text("Waiting for “Fix the flaky login test”")
            started = page.locator(
                "#timeline .event", has_text="Started “Fix the flaky login test”"
            )
            await expect(started).to_be_visible()
            log("three tasks on the board as they start, a line each in the chat; one waits")
            dots = page.locator("#session-list .item .dot")
            assert all([await dot.get_attribute("title") for dot in await dots.all()]), "dot help"
            await page.screenshot(path=SHOTS / f"demo-{scheme}-1-running.png")

            # A half-typed note survives updates of its card as the worker moves on.
            research = page.locator("#tasks .card.task", has_text="Compare feature-flag services")
            await research.locator(".more-button").click()
            await research.get_by_role("menuitem", name="Add a note…").click()
            await research.locator("textarea").press_sequentially("Include pricing")
            state = "n => n.className + (n.querySelector('.activity')?.textContent || '')"
            before = await research.evaluate(state)
            for _ in range(60):
                if await research.evaluate(state) != before:
                    break
                await asyncio.sleep(0.25)
            else:
                raise AssertionError("the card never changed; the check proves nothing")
            await expect(research.locator("textarea")).to_have_value("Include pricing")
            await expect(research.locator("textarea")).to_be_focused()
            await research.get_by_role("button", name="Cancel", exact=True).click()
            now = await research.evaluate(state)
            log(f"a half-typed note survived an update of its card ({now[:60]})")

            question = page.locator("#needs .card.decision")
            await expect(question).to_be_visible(timeout=30000)
            await expect(question.locator(".option.recommended")).to_contain_text("Per account")
            await expect(question).to_contain_text("lets anyone lock a user out")
            await expect(page.locator(".home-item .badge")).to_be_visible()
            await expect(page.locator("#sidebar .badge")).to_have_count(1)
            await page.screenshot(path=SHOTS / f"demo-{scheme}-2-question.png")
            await question.locator(".option", has_text="Per account").click()
            await expect(question).to_have_count(0)
            await expect(page.locator(".toast", has_text="Decided.")).to_be_visible()
            log("decided from a card with the options, their effects and a recommendation")

            slack = page.locator("#needs .card.draft.slack")
            await expect(slack).to_be_visible(timeout=60000)
            await expect(slack).to_contain_text("Maya Chen")
            await page.screenshot(path=SHOTS / f"demo-{scheme}-3-approve.png")
            await slack.get_by_role("button", name="Send", exact=True).click()
            await expect(slack).to_have_count(0)
            await expect(page.locator(".toast", has_text="Sent on Slack.")).to_be_visible()
            log("approved a Slack reply drafted in the owner's voice")

            # A review drafted for a teammate's PR: the PR in a line, each comment with its
            # code; one comment left out, the rest posted.
            review = page.locator("#needs .card.pr-review")
            await expect(review).to_be_visible(timeout=60000)
            await expect(review.locator(".pr-line")).to_contain_text("by @maya")
            await review.get_by_role("button", name="Review…").click()
            view = page.locator(".review-dialog")
            rows = view.locator(".review-comment")
            await expect(rows).to_have_count(3)
            await expect(rows.first.locator(".file")).to_have_text("CompanyNameNormalizer.scala:48")
            wide = await view.evaluate(
                "v => [...v.querySelectorAll('*')].some((n) => !n.closest('pre') && "
                "n.getBoundingClientRect().right > v.getBoundingClientRect().right + 1)"
            )
            assert not wide, "nothing runs past the review view"
            await page.screenshot(path=SHOTS / f"demo-{scheme}-3-review.png")
            await rows.last.get_by_role("button", name="✕ Remove").click()
            await view.locator("footer button.send").click()
            await expect(view).to_have_count(0)
            await expect(review).to_have_count(0)
            await expect(page.locator(".toast", has_text="Posted on GitHub.")).to_be_visible()
            log("reviewed the review: one line comment left out, the rest posted")

            # Results: those that changed code under Needs you to accept; a report that only
            # informs with the finished ones, read rather than accepted.
            review = page.locator("#panel .card.task.review")
            await expect(review).to_have_count(3, timeout=60000)
            recent = page.locator("#tasks-recent")
            if not await recent.evaluate("n => n.open"):
                await recent.locator("summary").click()
            fix = review.filter(has_text="Fix the flaky login test")
            await expect(fix.locator(".receipt.pass", has_text="tests")).to_be_visible()
            limited = review.filter(has_text="Rate-limit")
            await expect(limited.locator(".outcome")).to_contain_text("per account")
            await expect(fix.locator(".receipt.verdict")).to_have_text("✓ Verified")
            await expect(fix.locator(".receipt", has_text="✗→✓ tests")).to_be_visible()
            await expect(limited.locator(".receipt.verdict")).to_have_text("Partly verified · 1/2")
            await expect(limited.locator(".criteria li.warn")).to_contain_text("sixth attempt")
            # Compact until opened: the whole report, with its commit.
            await expect(fix.locator(".report")).to_be_hidden()
            await fix.get_by_role("button", name="Show report").click()
            await expect(fix.locator(".report .receipt", has_text="commit")).to_be_visible(
                timeout=10000
            )
            # The chat says it finished in one line, pointing at this card.
            await expect(
                page.locator("#timeline .event.done", has_text="Fix the flaky login test")
            ).to_contain_text("finished")
            log("results wait for review: compact, verified criteria apart from claimed ones")
            await page.screenshot(path=SHOTS / f"demo-{scheme}-4-review.png")

            await fix.locator(".actions .more-button").click()
            await fix.get_by_role("menuitem", name="Copy report").click()
            await expect(page.locator(".toast", has_text="Report copied")).to_be_visible()
            copied = await page.evaluate("navigator.clipboard.readText()")
            assert copied.startswith("## Fix the flaky login test"), repr(copied[:300])
            assert "**Observed**" in copied, repr(copied)
            log("copied the task report")

            await fix.locator(".actions .more-button").click()
            await fix.get_by_role("menuitem", name="Create PR").click()
            await expect(fix.locator(".error")).to_contain_text("has no GitHub remote")
            log("Create PR explains why the demo project cannot have one")

            await fix.locator("button.receipt.changes").click()
            changes = page.locator(".diff-dialog")
            await expect(changes.locator(".diff-file summary")).to_contain_text("app/login.py")
            await changes.locator(".diff-line.add").first.click()
            await changes.locator(".diff-compose textarea").fill("Call the clock parameter now.")
            await changes.get_by_role("button", name="Comment").click()
            await expect(changes.locator("footer")).to_contain_text("1 comment")
            await page.screenshot(path=SHOTS / f"demo-{scheme}-5-changes.png")
            await changes.get_by_role("button", name="Send to the worker").click()
            await expect(changes).to_have_count(0)
            await expect(fix).to_contain_text("Review comments on your changes", timeout=30000)
            log("commented on lines of the diff; the worker got the comments")

            research = review.filter(has_text="Compare feature-flag services")
            if await research.locator(".row-line").count():
                await research.locator(".row-line").click()  # A row: its card opens in place.
            await research.locator(".actions .more-button").click()
            await research.get_by_role("menuitem", name="Follow up…").click()
            await research.locator("textarea").fill("Add pricing for 50 seats")
            await research.get_by_role("button", name="Send").click()
            await expect(page.locator(".toast", has_text="Sent to the worker.")).to_be_visible()
            await expect(
                page.locator("#panel .card.task.review", has_text="Compare feature-flag services")
            ).to_be_visible(timeout=30000)
            log("followed up on a finished task; it came back for review")

            await fix.get_by_role("button", name="Accept").click()
            await expect(page.locator("#panel .card.task.review")).to_have_count(2)
            await expect(page.locator(".toast", has_text="Accepted.")).to_be_visible()
            log("accepted a result")

            # Its result accepted, its worker has nothing open: folded away with the others.
            idle = page.locator("#session-list details.idle-workers")
            await expect(idle.locator(".item", has_text="Fix the flaky login")).to_have_count(1)
            await idle.locator("summary").click()
            await page.locator("#session-list .item", has_text="Fix the flaky login").click()
            work = page.locator("#timeline details.steps")
            await expect(work.locator(".said").first).to_contain_text("Passing the clock in")
            folded = work.locator(".narration", has_text="Reproducing the flaky test first.")
            await expect(folded).to_have_count(1)
            narrated = page.locator("#timeline > .msg.agent", has_text="Reproducing the flaky")
            await expect(narrated).to_have_count(0)
            log("what a worker says on the way is folded into its steps, not shown as answers")
            await page.locator("#back-home").click()

            await page.fill(
                "#composer-input", "Change of plan: limit login attempts per IP address only"
            )
            await page.press("#composer-input", "Enter")
            limited = page.locator("#panel .card.task.review", has_text="per IP address only")
            await expect(limited).to_be_visible(timeout=30000)
            await expect(page.locator("#timeline")).to_contain_text(
                "works to the new decision", timeout=10000
            )
            log("changed a decision: the affected result was redone, and its worker confirmed")

            # Learn now: "Learning…" at once in the open dialog, then how it went.
            await page.click("#settings-button")
            await page.click("#settings-tabs [data-tab=you]")
            await expect(page.locator("#learn-status")).to_contain_text("new to learn from")
            learn = page.locator("#learn-now")
            await learn.click()
            await expect(learn).to_have_text("Learning…")
            await expect(learn).to_be_disabled()
            await expect(page.locator("#settings-dialog")).to_be_visible()
            await expect(learn).to_have_text("Learn now", timeout=60000)
            await expect(page.locator("#learn-state")).not_to_be_empty()
            await expect(page.locator("#learn-last-run")).to_be_visible()
            await page.click("#settings-cancel")
            # Chief of Staff's own learning worker is not one of the owner's.
            for place in ("#session-list", "#tasks", "#closed-list"):
                await expect(page.locator(place)).not_to_contain_text("Learn how you work")
            lessons = page.locator("#needs .card.lessons")
            await expect(lessons).to_be_visible(timeout=30000)
            await expect(lessons).to_contain_text("because you said")
            await page.screenshot(path=SHOTS / f"demo-{scheme}-5-lessons.png")
            rows = lessons.locator("li")
            texts = await rows.evaluate_all(
                "rs => rs.map((r) => (r.querySelector('textarea, input[type=text]') || {}).value"
                " || '')"
            )
            dropped = rows.nth(
                next(i for i, text in enumerate(texts) if text.startswith("Rate-limit"))
            )
            await dropped.locator("input[type=checkbox]").uncheck()
            await lessons.get_by_role("button", name="Keep ticked").click()
            await expect(lessons).to_have_count(0)
            await page.click("#settings-button")
            await expect(page.locator("#settings-tabs [data-tab=you]")).to_have_attribute(
                "aria-selected", "true"
            )
            await expect(page.locator("#lessons")).to_contain_text("pricing for a team of 50")
            await expect(page.locator("#lessons")).not_to_contain_text("Rate-limit")
            await expect(page.locator("#agreement")).to_contain_text("recommendations you took")
            await page.screenshot(path=SHOTS / f"demo-{scheme}-5-learned.png")
            await page.click("#settings-cancel")
            log("learned from the owner's feedback; the owner kept one lesson of two")

            await page.locator("#session-actions .menu-button", has_text="Focus").click()
            await page.get_by_role("menuitem", name="For 1 hour").click()
            await expect(page.locator("#session-meta")).to_contain_text("Focusing until")
            await page.click("#session-actions .focus-on")
            await expect(page.locator("#focus-summary")).to_contain_text("While you focused")
            log("focused, then got one summary")

            # A session open in the owner's terminal: shown read-only, never taken over.
            await page.locator("#external summary").click()
            await page.locator("#external-list .item", has_text="Profile the slow orders").click()
            await expect(page.locator("#panel")).to_be_hidden()
            await expect(page.locator("#back-home")).to_be_visible()
            await expect(page.locator(".external-history")).to_contain_text("ten times faster")
            await expect(page.locator("#session-meta")).to_contain_text("Open in a terminal")
            await expect(page.locator("#composer-note")).to_contain_text("exit it there (/exit)")
            await expect(page.locator("#composer-input")).to_be_hidden()
            await page.screenshot(path=SHOTS / f"demo-{scheme}-6-terminal.png")
            log("opened a session that is open in a terminal, read-only")

            # An older session of the owner's: a message continues it here.
            await page.locator("#external-list .item", has_text="Write the API changelog").click()
            history = page.locator(".external-history")
            await expect(history).to_contain_text("CHANGELOG.md, not committed yet")
            await expect(history.locator(".steps .said")).to_contain_text("Reading the recent")
            await page.screenshot(path=SHOTS / f"demo-{scheme}-6-other-session.png")
            await page.fill("#composer-input", "Commit it")
            await page.press("#composer-input", "Enter")
            continued = page.locator("#session-list .item", has_text="Write the API changelog")
            await expect(continued).to_be_visible(timeout=15000)
            await expect(page.locator("#timeline")).to_contain_text("Draft a changelog entry")
            await expect(page.locator("#timeline .msg.owner", has_text="Commit it")).to_be_visible()
            await expect(page.locator("#timeline")).to_contain_text(
                "Done: Commit it", timeout=20000
            )
            log("messaged an older Copilot session: it continues here, with its conversation")

            await page.locator(".home-item").click()
            await expect(page.locator("#timeline")).to_contain_text("Started three tasks")
            await page.screenshot(path=SHOTS / f"demo-{scheme}-7-chat.png")
            assert not errors, errors
            await browser.close()
    except BaseException:
        print("--- demo server log (last 60 lines)")
        print("".join(process.output[-60:]), flush=True)
        raise
    finally:
        process.terminate()


async def main():
    SHOTS.mkdir(exist_ok=True)
    for scheme in ("light", "dark"):
        print(f"{scheme}:", flush=True)
        await run(scheme)
    print("DEMO BROWSER TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())
