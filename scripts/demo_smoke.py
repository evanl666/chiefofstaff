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
            await page.screenshot(path=SHOTS / f"demo-{scheme}-1-running.png")

            question = page.locator(".card.request.question")
            await expect(question).to_be_visible(timeout=30000)
            await page.screenshot(path=SHOTS / f"demo-{scheme}-2-question.png")
            await question.get_by_role("button", name="Per account").click()
            await expect(question).to_have_count(0)
            log("answered the worker's question from its card")

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
            await expect(fix.locator(".receipt.pass")).to_contain_text("tests")
            await expect(fix.locator(".receipt", has_text="commit")).to_be_visible(timeout=10000)
            await expect(review.filter(has_text="Rate-limit")).to_contain_text("per account")
            log("results wait for review with what was observed")
            await page.screenshot(path=SHOTS / f"demo-{scheme}-4-review.png", full_page=True)

            await fix.get_by_role("button", name="Copy report").click()
            await expect(page.locator(".toast", has_text="Report copied")).to_be_visible()
            copied = await page.evaluate("navigator.clipboard.readText()")
            assert copied.startswith("## Fix the flaky login test"), repr(copied[:300])
            assert "**Observed**" in copied, repr(copied)
            log("copied the task report")

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

            await page.select_option("#session-actions select", "60")
            await expect(page.locator("#session-meta")).to_contain_text("Focusing until")
            await page.click("#session-actions .focus-on")
            await expect(page.locator("#focus-summary")).to_contain_text("While you focused")
            log("focused, then got one summary")

            await page.locator(".item", has_text="Chat").click()
            await expect(page.locator("#timeline")).to_contain_text("Started three tasks")
            await page.screenshot(path=SHOTS / f"demo-{scheme}-5-chat.png")
            assert not errors, errors
            await browser.close()
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
