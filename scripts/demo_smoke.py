"""Browser test of the demo: the inbox through a whole scenario, with no accounts.

    uv run --with playwright python scripts/demo_smoke.py

Starts `chiefofstaff --demo`, then drives the UI like an owner: watches tasks start and wait
on each other, answers a worker's question, approves a Slack reply, checks the receipts on the
finished work, copies a report, follows up on a task and focuses for a while. Screenshots go to
browser-shots/ (light and dark).
"""

import asyncio
import re
import subprocess
import sys
import threading
from pathlib import Path

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "browser-shots"
PORT = 8797


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

            inbox = page.locator("#inbox")
            await expect(inbox).to_be_visible()
            await expect(page.locator("#inbox-running .card.task")).to_have_count(3, timeout=20000)
            await expect(page.locator(".card.task.queued")).to_contain_text(
                "waiting for “Fix the flaky login test”"
            )
            log("three tasks; one waits for another")
            dots = page.locator("#session-list .item .dot")
            assert all([await dot.get_attribute("title") for dot in await dots.all()]), "dot help"
            await page.screenshot(path=SHOTS / f"demo-{scheme}-1-running.png")

            # A half-typed note survives updates of its card as the worker moves on.
            research = page.locator("#inbox .card.task", has_text="Compare feature-flag services")
            await research.hover()
            await research.get_by_role("button", name="Add a note").click()
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

            question = page.locator(".card.decision")
            await expect(question).to_be_visible(timeout=30000)
            await expect(question.locator(".option.recommended")).to_contain_text("Per account")
            await expect(question).to_contain_text("lets anyone lock a user out")
            await page.screenshot(path=SHOTS / f"demo-{scheme}-2-question.png")
            await question.locator(".option", has_text="Per account").click()
            await expect(question).to_have_count(0)
            log("decided from a card with the options, their effects and a recommendation")

            slack = page.locator("#inbox-needs .card.draft:not(.request)")
            await expect(slack).to_be_visible(timeout=60000)
            await expect(slack).to_contain_text("Maya Chen")
            await page.screenshot(path=SHOTS / f"demo-{scheme}-3-approve.png")
            await slack.get_by_role("button", name="Send").click()
            await expect(slack).to_have_count(0)
            log("approved a Slack reply drafted in the owner's voice")

            review = page.locator("#inbox-review .card.task")
            await expect(review).to_have_count(3, timeout=60000)
            fix = review.filter(has_text="Fix the flaky login test")
            await expect(fix.locator(".receipt.pass", has_text="tests")).to_be_visible()
            await expect(fix.locator(".receipt", has_text="commit")).to_be_visible(timeout=10000)
            limited = review.filter(has_text="Rate-limit")
            await expect(limited).to_contain_text("per account")
            await expect(fix.locator(".receipt.verdict")).to_have_text("✓ Verified")
            await expect(fix.locator(".receipt", has_text="✗→✓ tests")).to_be_visible()
            await expect(limited.locator(".receipt.verdict")).to_have_text("Partly verified · 1/2")
            await expect(limited.locator(".criteria li.warn")).to_contain_text("sixth attempt")
            log("results wait for review: verified criteria apart from claimed ones")
            await page.screenshot(path=SHOTS / f"demo-{scheme}-4-review.png", full_page=True)

            await fix.get_by_role("button", name="Copy report").click()
            await expect(page.locator(".toast", has_text="Report copied")).to_be_visible()
            copied = await page.evaluate("navigator.clipboard.readText()")
            assert copied.startswith("## Fix the flaky login test"), repr(copied[:300])
            assert "**Observed**" in copied, repr(copied)
            log("copied the task report")

            await fix.get_by_role("button", name="Create PR").click()
            await expect(fix.locator(".error")).to_contain_text("no GitHub origin")
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
            await research.get_by_role("button", name="Follow up").click()
            await research.locator("textarea").fill("Add pricing for 50 seats")
            await research.get_by_role("button", name="Send").click()
            await expect(
                page.locator("#inbox-review .card.task", has_text="Compare feature-flag services")
            ).to_be_visible(timeout=30000)
            log("followed up on a finished task; it came back for review")

            await fix.get_by_role("button", name="Accept").click()
            await expect(page.locator("#inbox-review .card.task")).to_have_count(2)
            log("accepted a result")

            await page.locator("#session-list .item", has_text="Fix the flaky login").click()
            work = page.locator("#timeline details.steps")
            await expect(work.locator(".said").first).to_contain_text("Passing the clock in")
            folded = work.locator(".narration", has_text="Reproducing the flaky test first.")
            await expect(folded).to_have_count(1)
            narrated = page.locator("#timeline > .msg.agent", has_text="Reproducing the flaky")
            await expect(narrated).to_have_count(0)
            log("what a worker says on the way is folded into its steps, not shown as answers")
            await page.locator(".item", has_text="Inbox").first.click()

            await page.fill(
                "#composer-input", "Change of plan: limit login attempts per IP address only"
            )
            await page.press("#composer-input", "Enter")
            limited = page.locator("#inbox-review .card.task", has_text="per IP address only")
            await expect(limited).to_be_visible(timeout=30000)
            await page.locator(".item", has_text="Chat").click()
            await expect(page.locator("#timeline")).to_contain_text(
                "works to the new decision", timeout=10000
            )
            await page.locator(".item", has_text="Inbox").first.click()
            log("changed a decision: the affected result was redone, and its worker confirmed")

            await page.click("#settings-button")
            await expect(page.locator("#learn-status")).to_contain_text("new piece")
            await page.click("#learn-now")
            lessons = page.locator("#inbox .card.lessons")
            await expect(lessons).to_be_visible(timeout=30000)
            await expect(lessons).to_contain_text("because you said")
            await page.screenshot(path=SHOTS / f"demo-{scheme}-5-lessons.png")
            dropped = lessons.locator("li", has=page.locator("input[value^='Rate-limit']"))
            await dropped.locator("input[type=checkbox]").uncheck()
            await lessons.get_by_role("button", name="Keep ticked").click()
            await expect(lessons).to_have_count(0)
            await page.click("#settings-button")
            await expect(page.locator("#lessons")).to_contain_text("pricing for a team of 50")
            await expect(page.locator("#lessons")).not_to_contain_text("Rate-limit")
            await expect(page.locator("#agreement")).to_contain_text("recommendations you took")
            await page.screenshot(path=SHOTS / f"demo-{scheme}-5-learned.png")
            await page.click("#settings-cancel")
            log("learned from the owner's feedback; the owner kept one lesson of two")

            await page.select_option("#session-actions select", "60")
            await expect(page.locator("#session-meta")).to_contain_text("Focusing until")
            await page.click("#session-actions .focus-on")
            await expect(page.locator("#focus-summary")).to_contain_text("While you focused")
            log("focused, then got one summary")

            # A session open in the owner's terminal: shown read-only, never taken over.
            await page.locator("#external summary").click()
            await page.locator("#external-list .item", has_text="Profile the slow orders").click()
            await expect(page.locator("#inbox")).to_be_hidden()
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

            await page.locator(".item", has_text="Chat").click()
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
