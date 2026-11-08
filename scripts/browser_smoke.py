"""Live browser test of the Chief of Staff UI against real Copilot sessions.

Drives the real UI with Playwright while Chief of Staff talks to your signed-in Copilot and to a
fake Slack MCP server. Saves screenshots to ./browser-shots (light and dark).

    uv run --with playwright python scripts/browser_smoke.py
"""

import asyncio
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright, expect

sys.path.insert(0, str(Path(__file__).parent))
from fake_slack_mcp import Workspace, serve_in_thread
from live_e2e import BASE, DASHBOARD, SLACK_PORT, SLACK_TOKEN, App, cleanup, git, log

SHOTS = Path("browser-shots")


async def run(page, app: App, project: Path, workspace: Workspace, errors: list[str]):
    # Home is the Router's chat, with Needs you and the task board beside it.
    home = page.locator(".home-item")
    await page.goto(f"{BASE}/#token={app.launch_token}")
    await expect(page.locator("#application")).to_be_visible()
    await expect(page.locator(".home-item.selected")).to_be_visible()
    await expect(page.locator("#home-welcome")).to_contain_text("Ask for anything")
    setup = page.locator("#home-setup")
    await expect(setup).to_contain_text("Where is your code?", timeout=60000)
    await page.screenshot(path=SHOTS / "1-empty.png")
    # Working without projects for now: the card goes, and stays gone after a reload.
    await setup.get_by_role("button", name="Not now").click()
    await expect(page.locator("#home-setup")).to_have_count(0)
    await page.reload()
    await expect(page.locator("#home-welcome")).to_be_visible()
    await expect(page.locator("#home-setup")).to_have_count(0)
    await page.evaluate("localStorage.removeItem('cos.noProjectsCard')")
    await page.reload()
    await expect(setup).to_contain_text("Where is your code?", timeout=60000)
    await setup.locator("input").fill(str(project.parent))
    await setup.get_by_role("button", name="Add").click()
    await expect(page.locator("#home-setup")).to_have_count(0)
    log("set the code folder from the first-run card")

    await page.get_by_role("button", name="Settings").click()
    form = page.locator("#settings-form")
    await expect(form.locator("input[name=code_folder]")).to_have_value(str(project.parent))
    await form.locator("input[name=mac_notifications]").uncheck()
    await page.locator("#settings-tabs [data-tab=connections]").click()
    await page.locator("#conn-slack summary").click()
    await form.locator("input[name=slack_enabled]").check()
    await form.locator("input[name=slack_token]").fill("xoxb-wrong")
    await page.locator("#slack-token-save").click()
    await expect(page.locator("#settings-error")).to_contain_text("not a Slack user token")
    await form.locator("input[name=slack_token]").fill(SLACK_TOKEN)
    await page.locator("#settings-tabs [data-tab=advanced]").click()
    await form.locator("input[name=slack_poll_seconds]").fill("20")
    await page.screenshot(path=SHOTS / "2-settings.png")
    await form.get_by_role("button", name="Save", exact=True).click()
    await expect(page.locator("#settings-dialog")).to_be_hidden()
    await page.wait_for_function(
        "() => fetch('/api/state', {headers: {'X-Chief-Key': localStorage.getItem('cos.key')}})"
        ".then((r) => r.json()).then((s) => (s.sessions || []).some((x) => x.role === 'slack'))",
        timeout=30000,
    )

    # Home's composer talks to the Router; the task shows on the board at once.
    composer = page.locator("#composer-input")
    await expect(composer).to_have_attribute("placeholder", re.compile("^Ask for anything…"))
    await composer.fill(
        f"In {project}, add notes.md with the line `notes from the UI test` and commit it "
        "with message 'Add notes'. Use an isolated worktree."
    )
    await composer.press("Enter")
    await expect(page.locator("#timeline .msg.owner")).to_contain_text("notes from the UI test")
    await expect(page.locator("#tasks .card.task").first).to_be_visible(timeout=120000)
    worker = page.locator("#session-list .item", has_text="notes")
    await expect(worker).to_be_visible(timeout=120000)
    log("a task card and its worker appeared")
    await page.screenshot(path=SHOTS / "3-dispatched.png")
    await worker.click()
    await expect(page.locator("#timeline .card.router")).to_be_visible()
    await expect(composer).to_have_attribute("placeholder", "Message this worker directly…")
    await expect(page.locator("#timeline .card.report.done")).to_be_visible(timeout=600000)
    await expect(page.locator("#timeline .card.report.done .chip")).to_have_text("Done")
    await page.screenshot(path=SHOTS / "4-worker.png")
    await page.locator("details.steps summary").first.click()
    await expect(page.locator("details.steps .step").first).to_be_visible()

    # The finished task waits for review, with the commit the worker made.
    await page.locator("#back-home").click()
    # Changes to accept: under Needs you (a report that only informs is with the finished).
    review = page.locator("#needs .card.task").first
    await expect(review).to_be_visible(timeout=60000)
    toggle = review.locator(".expand")
    if (await toggle.inner_text()).strip() == "Show report":
        await toggle.click()
    await expect(review.locator(".report .receipt", has_text="commit")).to_be_visible(timeout=60000)
    log("the result waits for review with its commit")
    await page.screenshot(path=SHOTS / "5-review.png")

    # In the chat, the worker's report is one line pointing at that card.
    await expect(page.locator("#timeline .event.done").first).to_contain_text(
        "finished", timeout=300000
    )
    log("the Home chat shows the report as one line")
    await page.screenshot(path=SHOTS / "5-update.png")

    workspace.post("C0DEPLOYS", "U0BOB001", f"Deploy dashboard lives here: {DASHBOARD} (pinned)")
    await asyncio.sleep(1)
    workspace.post(
        "D0ALICE01",
        "U0ALICE1",
        "Hey! Could you send me the deploy dashboard link? Bob posted it in #deploys.",
    )
    log("Alice sent a DM")
    draft = page.locator("#needs .card.draft.slack")
    await expect(draft).to_be_visible(timeout=600000)
    await expect(home.locator(".badge")).to_be_visible()
    assert DASHBOARD in await draft.locator("textarea").input_value()
    # Where it really goes, by Slack's id: not what the drafting session calls it.
    await expect(draft.locator(".label")).to_contain_text("Slack reply · direct message")
    destination = draft.locator(".label [title*='D0ALICE01']")
    await expect(destination).to_have_attribute("title", re.compile(r"direct message D0ALICE01"))
    await page.screenshot(path=SHOTS / "6-draft.png")
    edited = f"Here it is: {DASHBOARD}"
    await draft.locator("textarea").fill(edited)
    await draft.get_by_role("button", name="Send").click()
    await expect(draft).to_have_count(0, timeout=30000)
    assert [m["text"] for m in workspace.sent] == [edited], workspace.sent
    log("draft edited and sent from Needs you")

    # A worker asks the owner directly; the owner answers on the card.
    await composer.fill(
        "Start a worker that asks me directly (with ask_user) to pick red or blue, "
        "then reports my choice."
    )
    await composer.press("Enter")
    question = page.locator("#needs .card.request.question")
    await expect(question).to_be_visible(timeout=300000)
    await page.screenshot(path=SHOTS / "11-question.png")
    await question.get_by_role("button", name=re.compile("^blue$", re.I)).click()
    await expect(question).to_have_count(0)
    await expect(page.locator("#panel")).to_contain_text(re.compile("blue", re.I), timeout=300000)
    log("the owner answered a worker's question on its card")

    accepted = page.locator("#needs .card.task", has_text=re.compile("notes", re.I))
    if await accepted.count():
        await accepted.first.get_by_role("button", name="Accept").click()
        await expect(accepted).to_have_count(0)
        log("accepted the notes task")

    # The Slack session's own timeline: from Settings → Connections → Slack.
    await page.get_by_role("button", name="Settings").click()
    await page.locator("#settings-tabs [data-tab=connections]").click()
    if not await page.locator("#conn-slack").evaluate("section => section.open"):
        await page.locator("#conn-slack summary").click()
    await page.locator("#slack-activity").click()
    await expect(page.locator("#timeline")).to_contain_text("edited draft", timeout=30000)
    await page.locator("#external summary").click()
    await expect(page.locator("#external-list .item").first).to_be_visible(timeout=30000)
    await page.screenshot(path=SHOTS / "7-slack.png")

    # Close the finished worker (unless the Router already did), reopen it from the Closed
    # group, close it again, then reply to it.
    async def close_worker():
        await page.locator("#session-more-button").click()
        await page.locator("#session-menu-items").get_by_role("button", name="Close worker").click()

    async def shown(text):
        """A worker's item in the sidebar; one with nothing open is in a folded group."""
        item = page.locator("#session-list .item", has_text=text)
        fold = page.locator("#session-list details.idle-workers")
        if await item.count() and not await item.is_visible() and await fold.count():
            await fold.locator("summary").click()
        return item

    if await page.locator("#session-list .item", has_text="notes").count():
        await (await shown("notes")).click()
        await close_worker()
    await expect(page.locator("#session-list .item", has_text="notes")).to_have_count(0)
    await page.locator("#closed summary").click()
    await page.locator("#closed-list .item", has_text="notes").click()
    await expect(page.locator("#closed-list .item", has_text="notes")).to_be_visible()
    await page.locator("#session-actions").get_by_role("button", name="Reopen").click()
    await expect(page.locator("#session-list .item", has_text="notes")).to_have_count(1)
    await expect(await shown("notes")).to_be_visible()
    await expect(page.locator("#closed")).to_be_hidden()
    await close_worker()
    await page.locator("#closed-list .item", has_text="notes").click()
    await expect(composer).to_have_attribute("placeholder", "Message to reopen this worker…")
    await page.screenshot(path=SHOTS / "10-closed.png")
    await composer.fill("Which file did you add? Reply with only its name, in UPPERCASE.")
    await composer.press("Enter")
    await expect(page.locator("#session-list .item", has_text="notes")).to_have_count(1)
    await expect(page.locator("#timeline")).to_contain_text("NOTES.MD", timeout=300000)
    log("a reply reopened the closed worker, which still knew its task")

    await home.click()
    await page.locator("#session-more-button").click()
    picker = page.locator("#session-model")
    await expect(picker).to_be_visible()
    await picker.select_option("claude-sonnet-5.5")
    await expect(page.locator(".toast").last).to_contain_text("Applies from the next message")
    effort = page.locator("#session-effort")
    await expect(effort).to_be_visible()
    await effort.select_option("high")
    await expect(page.locator(".toast").last).to_contain_text("Thinking effort: high")
    await page.locator("#usage-button").click()
    await expect(page.locator("#usage-total strong")).to_contain_text("credits")
    await expect(page.locator("#usage-tables")).to_contain_text("By session")
    overflow = await page.evaluate(
        "() => [...document.querySelectorAll('#usage-dialog, #usage-dialog form')]"
        ".some((node) => node.scrollWidth > node.clientWidth)"
    )
    assert not overflow, "the usage dialog must not scroll sideways"
    await page.screenshot(path=SHOTS / "9-usage.png")
    await page.get_by_role("button", name="7 days").click()
    await expect(page.locator("#usage-dialog [data-range='7']")).to_have_class("active")
    await page.locator("#usage-dialog").get_by_role("button", name="Close").click()

    await page.emulate_media(color_scheme="dark")
    await home.click()
    await expect(page.locator("#tasks-recent, #needs").first).to_be_visible()
    await page.screenshot(path=SHOTS / "8-dark-inbox.png")
    await expect(page.locator("#timeline .event.done").first).to_be_visible()
    await expect(home).to_be_in_viewport()
    await page.screenshot(path=SHOTS / "8-dark.png")
    assert not errors, errors


async def main():
    SHOTS.mkdir(exist_ok=True)
    workspace = Workspace()
    serve_in_thread(workspace, SLACK_TOKEN, SLACK_PORT)
    with tempfile.TemporaryDirectory(prefix="wb-browser-") as temporary:
        root = Path(temporary).resolve()
        project = root / "code" / "project"  # A repository in the owner's code folder.
        project.mkdir(parents=True)
        git(project, "init", "-q", "-b", "main")
        (project / "README.md").write_text("# Demo\n")
        git(project, "add", ".")
        git(project, "-c", "user.email=e2e@test", "-c", "user.name=e2e", "commit", "-qm", "init")
        app = App(root / "data", root / "chiefofstaff.log")
        await app.start()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1280, "height": 820})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def console(message):
                    # The 422 is the deliberately rejected bot token.
                    if message.type == "error" and "status of 422" not in message.text:
                        errors.append(message.text)

                page.on("console", console)
                try:
                    await run(page, app, project, workspace, errors)
                    log("BROWSER TEST PASSED")
                except BaseException:
                    await page.screenshot(path=SHOTS / "failure.png")
                    raise
                finally:
                    await browser.close()
        finally:
            await app.stop()
            if (root / "chiefofstaff.log").exists():
                print("\n".join((root / "chiefofstaff.log").read_text().splitlines()[-30:]))
            await cleanup(root / "data")
    subprocess.run(["ls", str(SHOTS)])


if __name__ == "__main__":
    asyncio.run(main())
