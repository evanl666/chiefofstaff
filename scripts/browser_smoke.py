"""Optional: uv run --with playwright python scripts/browser_smoke.py."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect


async def main():
    with tempfile.TemporaryDirectory(prefix="work-buddy-browser-") as directory:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "work_buddy",
            "--no-browser",
            "--port",
            "18765",
            "--data-dir",
            directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(20):
                assert process.stdout
                while True:
                    line = (await process.stdout.readline()).decode()
                    if "Private launch URL" in line:
                        launch_url = line.split(": ", 1)[1].strip()
                        break
                    if not line:
                        raise RuntimeError("Local service exited before startup.")
            async with async_playwright() as playwright:
                channel = os.environ.get("WORK_BUDDY_BROWSER_CHANNEL")
                browser = await playwright.chromium.launch(headless=True, channel=channel)
                try:
                    page = await browser.new_page(viewport={"width": 1440, "height": 1100})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(launch_url)
                    await expect(page.locator("#application")).to_be_visible()
                    await expect(page.locator("#daily-form textarea")).to_have_count(1)
                    await expect(page.locator("#daily-form input")).to_have_count(0)
                    await page.get_by_role("button", name="Settings", exact=True).click()
                    await page.locator("#advanced-settings > summary").click()
                    await page.locator("select[name=backend]").select_option("demo")
                    await page.get_by_role("button", name="Save settings", exact=True).click()
                    await expect(page.locator("#notice")).to_contain_text("Settings saved")

                    await page.get_by_role("button", name="Get to know me", exact=True).click()
                    await page.locator("textarea[name=material]").fill(
                        "I prefer concise replies that separate evidence from assumptions."
                    )
                    await page.get_by_role("button", name="Build my profile").click()
                    await expect(page.locator("#memory-approvals")).to_contain_text(
                        "Synthetic example",
                        timeout=15000,
                    )
                    await (
                        page.locator("#memory-approvals")
                        .get_by_role(
                            "button",
                            name="Approve",
                            exact=True,
                        )
                        .click()
                    )
                    await expect(page.locator("#memories .memory")).to_have_count(1)
                    await (
                        page.locator("#profile-dialog")
                        .get_by_role("button", name="Close", exact=True)
                        .click()
                    )
                    await expect(page.locator("#onboarding")).to_be_hidden()

                    await page.locator("#daily-form textarea").fill(
                        "Today: investigate caching and prepare design notes. "
                        "Context: preserve compatibility; ask about the final format."
                    )
                    await page.get_by_role("button", name="Hand over my day").click()
                    await expect(page.locator("#approvals")).to_contain_text(
                        "Which deliverable",
                        timeout=15000,
                    )
                    await page.get_by_role("button", name="Short summary", exact=True).click()
                    await expect(page.locator("#updates")).to_contain_text(
                        "Completed the simulated workflow",
                        timeout=15000,
                    )
                    await (
                        page.locator("#updates")
                        .get_by_role(
                            "button",
                            name="Read update / give feedback",
                        )
                        .first.click()
                    )
                    await expect(page.locator("#detail-result")).to_contain_text(
                        "Completed the simulated workflow",
                        timeout=15000,
                    )
                    await page.locator("#feedback-form textarea").fill(
                        "Include a concrete source rather than vague claims."
                    )
                    await page.get_by_role("button", name="Tell Buddy").click()
                    await (
                        page.locator("#task-dialog")
                        .get_by_role(
                            "button",
                            name="Close",
                            exact=True,
                        )
                        .click()
                    )
                    await expect(page.locator("#decision-panel")).to_be_hidden()
                    await page.get_by_role("button", name="What Buddy knows").click()
                    await expect(page.locator("#memory-approvals")).to_contain_text(
                        "Confirm this candidate memory",
                        timeout=15000,
                    )
                    await (
                        page.locator("#memory-approvals")
                        .get_by_role(
                            "button",
                            name="Approve",
                            exact=True,
                        )
                        .click()
                    )
                    await expect(page.locator("#memories .memory")).to_have_count(2)
                    await (
                        page.locator("#profile-dialog")
                        .get_by_role(
                            "button",
                            name="Close",
                            exact=True,
                        )
                        .click()
                    )
                    await expect(page.locator("#updates article")).to_have_count(1)
                    if screenshot := os.environ.get("WORK_BUDDY_SCREENSHOT"):
                        await page.evaluate("window.scrollTo(0, 0)")
                        await page.screenshot(path=screenshot, full_page=True)
                    assert not errors, errors
                    exported = Path(directory) / "memory/confirmed.json"
                    assert exported.exists()
                    assert not urlsplit(page.url).fragment, "Launch token remained in URL."
                    print("Daily browser flow passed: brief, profile, decisions, quiet learning.")
                finally:
                    await browser.close()
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), 15)
            except TimeoutError:
                process.kill()
                _, stderr = await process.communicate()
            if process.returncode not in {0, -15}:
                print(stderr.decode(errors="replace"), file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
