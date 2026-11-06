"""Browser test of edge cases in the UI, with no accounts: the real web app runs in this process
on a fake Copilot, so the test can create cards and events and check what the page shows.

    uv run --with playwright python scripts/ui_edges.py
"""

import asyncio
import base64
import itertools
import json
import re
import sys
import tempfile
from datetime import UTC, datetime
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
# A 1x1 PNG, for images sent with a message.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
# Every field Settings saves, as the page posts them: a tab layout must not lose one.
SETTINGS_FIELDS = {
    "projects", "code_folder", "scratch_directory", "max_workers", "worker_yolo",
    "worker_sandbox", "shared_caches", "sandbox_writable", "worker_idle_hours",
    "mac_notifications", "watchdog_enabled", "slack_enabled", "slack_poll_seconds",
    "slack_delegate_emoji", "github_enabled", "github_repos", "copilot_command", "model",
    "role_models", "role_efforts", "worker_credit_limit", "digest_time", "learn_from_work",
    "learn_checks", "paused",
}  # fmt: skip
SETTINGS_FIELDS |= {
    "skill_folders", "plugins", "mcp_servers", "watches", "watch_hours", "watch_minutes",
    "watch_after_hours_minutes", "away_notify", "away_minutes", "away_how",
}  # fmt: skip
FORM_NAMES = SETTINGS_FIELDS - {"role_models", "role_efforts", "watches", "watch_hours"} | {
    "slack_token", "profile", "setup_notes", "report_footer",
    *(f"{kind}_{role}" for kind in ("model", "effort")
      for role in ("router", "worker", "slack", "watchdog")),
}  # fmt: skip


def log(text: str):
    print(f"  {text}", flush=True)


# A path as long as some are (a Scala package six levels deep): it must not widen anything.
LONG_PATH = (
    "services/matching/src/main/scala/com/acme/openconnect/pysparkjobs/company_matching/"
    "normalizers/CompanyNameNormalizer.scala"
)
# Nothing in it may run past it: the elements that do, with how far.
OVERFLOWS = """root => [root, ...root.querySelectorAll("*")].filter((node) => {
  const box = node.getBoundingClientRect();
  if (!box.width || node.closest("pre")) return false;
  const style = getComputedStyle(node);
  const own = style.overflowX === "visible" && style.textOverflow !== "ellipsis"
    && !node.matches("textarea, input, select") && node.scrollWidth > node.clientWidth + 1;
  const outer = root.getBoundingClientRect();
  return own || box.right > outer.right + 1 || box.left < outer.left - 1;
}).map((node) => `${node.className || node.tagName}: ${node.scrollWidth}/${node.clientWidth}`)"""


def review(repo: str, comments: list[dict]) -> dict:
    target = {"repo": repo, "number": 7, "url": f"https://github.com/{repo}/pull/7",
              "via": "task", "head_sha": "c0ffee", "verdict": "comment",
              "pr": {"title": "Export every row", "author": "maya", "about":
                     "The exporter stopped at 10,000 rows. " * 6, "additions": 3,
                     "deletions": 1, "files": 1}}  # fmt: skip
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
        await section.locator("#settings-tabs [data-tab=you]").click()
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
    await dialog.locator("#settings-tabs [data-tab=you]").click()
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


async def home(runtime, browser, failures: list[str]):
    """Home: the Router's chat beside Needs you and Tasks; one badge; why a task is queued;
    Chief of Staff's own helpers kept out of the owner's lists; long messages fold; images go
    along with a message; Settings in tabs keeps every field; Learn now answers at once."""
    store = runtime.store
    router = store.get("router_id")
    before = store.settings()
    store.save_settings(before.model_copy(update={"max_workers": 1}))
    started = []
    for title in ("Write the docs", "Fix the parser"):
        spawned = await call_tool(
            runtime, "spawn_worker", router, title=title, instructions=title, why_new="test"
        )
        started.append(json.loads(spawned.text_result_for_llm)["session_id"])
        await settle(runtime)
    await runtime.toolset.start_special("learner", "Learn how you work", "Learn from this.")
    await settle(runtime)
    question = store.create_draft(
        started[0], "question", {"question": "Which docs?", "choices": ["API", "Guide"]},
        "Which docs?",
    )  # fmt: skip
    runtime.bus.publish("draft", store.draft(question["id"]))
    long_text = "\n".join(f"Line {n}: more context for the task." for n in range(1, 25))
    runtime.add_message(router, "owner", long_text)
    runtime.add_message(
        router, "notice", "Learned how you write: Slack DMs (v2).", {"settings": "you"}
    )

    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    await context.grant_permissions(["clipboard-read", "clipboard-write"])
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    page.on("console", lambda m: failures.append(m.text) if m.type == "error" else None)
    # Where an earlier version left the owner: its inbox is Home now.
    await page.add_init_script("localStorage.getItem('cos.migrated') || "
                               "(localStorage.setItem('cos.selected', 'inbox'), "
                               "localStorage.setItem('cos.migrated', '1'))")  # fmt: skip
    await page.goto(f"{BASE}/#token={TOKEN}")
    await expect(page.locator(".home-item.selected")).to_be_visible()
    chat, panel = page.locator("#conversation"), page.locator("#panel")
    await expect(chat).to_be_visible()
    await expect(panel).to_be_visible()
    await expect(page.locator("#home-tabs")).to_be_hidden()
    chat_box, panel_box = await chat.bounding_box(), await panel.bounding_box()
    assert panel_box["x"] >= chat_box["x"] + chat_box["width"] - 1, (chat_box, panel_box)
    assert await page.evaluate("localStorage.getItem('cos.selected')") == "home"
    await expect(page.locator("#session-list .item", has_text="Chat")).to_have_count(0)
    log("Home: the Router's chat with Needs you and Tasks beside it; the old inbox opens it")

    # One count in the sidebar: on Home, what needs the owner.
    needs = page.locator("#needs .items > *")
    await expect(needs).to_have_count(1)
    await expect(page.locator("#sidebar .badge")).to_have_count(1)
    await expect(page.locator(".home-item .badge")).to_have_text("1")
    assert (await page.title()).startswith("(1) "), await page.title()

    queued = page.locator("#tasks-queued .card.task", has_text="Fix the parser")
    await expect(queued).to_contain_text("Waiting for a worker slot · 1 of 1 busy")
    for hidden in (page.locator("#session-list"), page.locator("#tasks")):
        await expect(hidden).not_to_contain_text("Learn how you work")
    log("one badge; the queued task says why; the learning helper is not in the lists")

    owner = page.locator("#timeline .msg.owner", has_text="Line 1:")
    await expect(owner.locator(".text")).to_have_class("text folded")
    await owner.get_by_role("button", name="Show more").click()
    await expect(owner.locator(".text")).to_have_class("text")
    await expect(owner.get_by_role("button", name="Show less")).to_be_visible()
    notice = page.locator("#timeline .event", has_text="Learned how you write")
    await notice.get_by_role("button", name="Open Settings").click()
    dialog = page.locator("#settings-dialog")
    await expect(dialog.locator("#settings-tabs [data-tab=you]")).to_have_attribute(
        "aria-selected", "true"
    )
    await expect(dialog.locator("#learn-last-run")).to_be_visible()
    await expect(dialog.locator("#learn-state")).to_have_text("Learning now…")
    await page.locator("#settings-cancel").click()
    log("a long message folds; a notice opens Settings where the learning helper shows")

    # Narrow: one part at a time, picked at the top.
    await page.set_viewport_size({"width": 800, "height": 900})
    await expect(page.locator("#home-tabs")).to_be_visible()
    await expect(panel).to_be_hidden()
    await page.locator("#home-tabs [data-tab=tasks]").click()
    await expect(page.locator("#tasks")).to_be_visible()
    await expect(page.locator("#needs")).to_be_hidden()
    await expect(chat).to_be_hidden()
    await expect(page.locator("#home-tabs [data-tab=needs] .count")).to_have_text("1")
    await page.screenshot(path=SHOTS / "ui-edges-home-narrow.png")
    await page.locator("#home-tabs [data-tab=chat]").click()
    await expect(page.locator("#composer-input")).to_be_focused()
    await page.set_viewport_size({"width": 1280, "height": 900})
    log("narrow: Chat, Needs you and Tasks one at a time")

    # Settings in tabs: every field there, and saved as before.
    await page.locator("#settings-button").click()
    names = set(await dialog.evaluate(
        "d => [...d.querySelectorAll('[name]')].map(n => n.name)"
    ))  # fmt: skip
    assert names == FORM_NAMES, names ^ FORM_NAMES
    for tab in ("general", "workers", "models", "connections", "you", "advanced"):
        await dialog.locator(f"#settings-tabs [data-tab={tab}]").click()
        await expect(dialog.locator(f".tab[data-tab={tab}]")).to_be_visible()
        await expect(page.locator("#settings-form footer button[type=submit]")).to_be_visible()
    await expect(dialog.locator("#connections-extra")).to_have_count(1)
    settings = store.settings().model_dump()
    async with page.expect_request("**/api/settings") as request:
        await page.locator("#settings-form footer button[type=submit]").click()
    body = (await request.value).post_data_json
    # What the form shows, and every other setting as it was: a save resets nothing.
    assert SETTINGS_FIELDS <= set(body) == set(settings), set(body) ^ set(settings)
    for key in set(settings) - {"role_models", "role_efforts", "slack_delegate_emoji"}:
        assert body[key] == settings[key], (key, body[key], settings[key])
    assert body["slack_delegate_emoji"].strip(":") == settings["slack_delegate_emoji"]
    for key in ("role_models", "role_efforts"):
        assert body[key] == {k: v for k, v in settings[key].items() if v}, (key, body[key])
    await expect(dialog).to_be_hidden()
    log("settings: six tabs, every field kept; saving posts what it did before")

    # Connections: a watch added, MCP servers checked as JSON, a skill uploaded and removed.
    await page.locator("#settings-button").click()
    await dialog.locator("#settings-tabs [data-tab=connections]").click()
    await dialog.locator("#conn-abilities summary").click()
    mcp = dialog.locator("textarea[name=mcp_servers]")
    await mcp.fill('{"jira": {"command": "npx"')
    await expect(dialog.locator("#mcp-error")).to_contain_text("not valid JSON")
    await page.locator("#settings-form footer button[type=submit]").click()
    await expect(dialog.locator("#settings-error")).to_contain_text("not valid JSON")
    await mcp.fill('{"jira": {"command": "npx", "args": ["-y", "jira-mcp"]}}')
    await expect(dialog.locator("#mcp-error")).to_be_hidden()
    skill = "---\nname: release-notes\ndescription: How we write release notes\n---\nShort.\n"
    await dialog.locator("#skill-file").set_input_files(
        files=[{"name": "SKILL.md", "mimeType": "text/markdown", "buffer": skill.encode()}]
    )
    await expect(dialog.locator("#skills")).to_contain_text("release-notes")
    await expect(dialog.locator("#abilities-summary")).to_contain_text("1 skill")
    await dialog.locator("#conn-watches summary").click()
    await dialog.locator("#watch-add").click()
    row = dialog.locator("#watch-list li").last
    await row.locator(".what").fill("Questions about the release in #eng")
    await row.locator(".how").fill("Slack MCP")
    await page.locator("#settings-form footer button[type=submit]").click()
    await expect(dialog).to_be_hidden()
    saved = store.settings()
    assert [w.model_dump() for w in saved.watches] == [
        {"what": "Questions about the release in #eng", "how": "Slack MCP", "enabled": True}
    ], saved.watches
    assert saved.mcp_servers["jira"]["command"] == "npx", saved.mcp_servers
    await page.locator("#settings-button").click()
    # Sections stay as the owner left them.
    await expect(dialog.locator("#conn-abilities")).to_have_attribute("open", "")
    await (
        dialog.locator("#skills li", has_text="release-notes")
        .get_by_role("button", name="Remove")
        .click()
    )
    await expect(dialog.locator("#skills")).not_to_contain_text("release-notes")
    await expect(dialog.locator("#watches-summary")).to_contain_text("1 on")
    await page.locator("#settings-cancel").click()
    log("connections: a watch saved, bad JSON caught before saving, a skill added and removed")

    # Learn now: at once "Learning…", the dialog stays, and the outcome shows when it ends.
    async def learning(route):
        await asyncio.sleep(0.8)
        snapshot = runtime.snapshot()
        snapshot["habits"] = {**snapshot["habits"], "running": True}
        await route.fulfill(json=snapshot)

    # Not while Chief of Staff's learning worker is still at it.
    for session_id in [row["id"] for row in store.open_sessions("worker")]:
        if store.get(f"learner:{session_id}"):
            await runtime.workers.close(session_id)
    await page.route("**/api/lessons/learn", learning)
    await page.locator("#settings-button").click()
    await dialog.locator("#settings-tabs [data-tab=you]").click()
    learn = dialog.locator("#learn-now")
    await learn.click()
    await expect(learn).to_be_disabled()
    await expect(learn).to_have_text("Learning…")
    await page.wait_for_timeout(1200)
    await expect(dialog).to_be_visible()
    await expect(learn).to_have_text("Learning…")
    at = datetime.now(UTC).isoformat()
    store.set("habits_last", json.dumps({"at": at, "text": "Nothing new learned.", "error": ""}))
    runtime.bus.publish("state")
    await expect(learn).to_be_enabled(timeout=10000)
    await expect(learn).to_have_text("Learn now")
    await expect(dialog.locator("#learn-state")).to_contain_text("Nothing new learned.")
    await page.unroute("**/api/lessons/learn")
    await page.locator("#settings-cancel").click()
    log("Learn now: Learning… at once, in the open dialog, then how it went")

    # Images with a message: picked, pasted or dropped; small before sending; in the bubble.
    composer = page.locator("#composer")
    await page.locator("#attach-input").set_input_files(
        files=[{"name": "shot.png", "mimeType": "image/png", "buffer": PNG}]
    )
    thumbs = page.locator("#attachments .thumb")
    await expect(thumbs).to_have_count(1)
    png = base64.b64encode(PNG).decode()
    await page.evaluate(
        """png => {
        const file = (name) => new File([Uint8Array.from(atob(png), (c) => c.charCodeAt(0))],
          name, { type: "image/png" });
        const pasted = new DataTransfer();
        pasted.items.add(file("pasted.png"));
        document.querySelector("#composer-input").dispatchEvent(
          new ClipboardEvent("paste", { clipboardData: pasted, bubbles: true, cancelable: true }));
        const dropped = new DataTransfer();
        dropped.items.add(file("dropped.png"));
        for (const type of ["dragover", "drop"]) {
          document.querySelector("#composer").dispatchEvent(
            new DragEvent(type, { dataTransfer: dropped, bubbles: true, cancelable: true }));
        }
      }""",
        png,
    )
    await expect(thumbs).to_have_count(3)
    await thumbs.nth(1).get_by_role("button").click()
    await expect(thumbs).to_have_count(2)
    await composer.screenshot(path=SHOTS / "ui-edges-attachments.png")
    async with page.expect_request(f"**/api/sessions/{router}/messages") as request:
        await page.fill("#composer-input", "What is wrong here?")
        await page.press("#composer-input", "Enter")
    sent = (await request.value).post_data_json
    bubble = page.locator("#timeline .msg.owner", has_text="What is wrong here?")
    await expect(bubble.locator(".attachments img")).to_have_count(2)
    await expect(thumbs).to_have_count(0)
    names = [item["name"] for item in sent["attachments"]]
    assert names == ["shot.png", "dropped.png"], names
    assert all(item["type"] == "image/png" and item["data"] == png
               for item in sent["attachments"]), sent  # fmt: skip
    image = await bubble.locator(".attachments img").first.element_handle()
    # Served back by the server: it loads.
    await page.wait_for_function("img => img.complete && img.naturalWidth === 1", arg=image)
    log("images: picked, pasted and dropped, one removed; sent along; shown in the bubble")

    # A reply only the owner can send (mail, say): copied, and marked sent, in one click.
    reply = store.create_draft(
        started[0], "reply", {"to": "Ann", "where": "email", "link": "https://mail.example.com/t/1"},
        "Sure, Friday works.",
    )  # fmt: skip
    runtime.bus.publish("draft", store.draft(reply["id"]))
    card = page.locator(f'#needs .card.draft[data-id="{reply["id"]}"]')
    # Where it goes is the link to the conversation, to paste it there.
    await expect(card.locator(".label")).to_have_text("Reply to Ann · email ↗")
    await expect(card.locator(".label a")).to_have_attribute("href", "https://mail.example.com/t/1")
    await card.locator("textarea").fill("Sure, Friday at 10 works.")
    await card.get_by_role("button", name="Copy & mark sent").click()
    await expect(card).to_have_count(0, timeout=5000)
    assert await page.evaluate("navigator.clipboard.readText()") == "Sure, Friday at 10 works."
    assert store.draft(reply["id"])["status"] == "sent"
    log("a reply to send yourself: edited, copied and marked sent from its card")

    # Not like this: back to the worker that drafted it, with what should change.
    again = store.create_draft(
        started[0], "reply", {"to": "Bo", "where": "email", "link": ""}, "Sure."
    )
    runtime.bus.publish("draft", store.draft(again["id"]))
    card = page.locator(f'#needs .card.draft[data-id="{again["id"]}"]')
    await card.locator(".actions .more-button").click()
    await card.get_by_role("menuitem", name="Ask to redo…").click()
    await card.get_by_label("What should change?").fill("Warmer, and say when")
    async with page.expect_request(f"**/api/drafts/{again['id']}") as request:
        await card.get_by_role("button", name="Send back").click()
    sent = (await request.value).post_data_json
    assert sent == {"action": "revise", "reason": "Warmer, and say when"}, sent
    await expect(card).to_have_count(0, timeout=5000)
    await expect(page.locator(".toast", has_text="Sent back to redo.")).to_be_visible()
    assert store.draft(again["id"])["outcome"] == "Redo: Warmer, and say when"
    log("a reply sent back to redo, with what should change")

    # Every worker at work shows under Running: one whose task is finished, handling news on
    # a PR it follows; and one at work whose card waits for the owner, marked so.
    running = page.locator("#tasks-running .card")

    async def spawn(title):
        made = await call_tool(
            runtime, "spawn_worker", router, title=title, instructions=title, why_new="test"
        )
        await settle(runtime)
        return json.loads(made.text_result_for_llm)["session_id"]

    store.save_settings(store.settings().model_copy(update={"max_workers": 6}))
    follow_up = await spawn("Review api PR #12")
    await call_tool(runtime, "report", follow_up, status="done", summary="Reviewed.")
    runtime.update_session(follow_up, status="working", activity="reading the reply")
    card = running.filter(has_text="Review api PR #12")
    await expect(card).to_contain_text("following up on “Review api PR #12”")
    worker = await spawn("Ship the docs")
    runtime.update_session(worker, status="working")
    card_draft = store.create_draft(worker, "reply", {"to": "Al", "where": "email"}, "Hi")
    runtime.bus.publish("draft", store.draft(card_draft["id"]))
    shown = running.filter(has_text="Ship the docs")
    await expect(shown).to_have_count(1)
    await expect(shown.locator(".chip", has_text="Needs you")).to_be_visible()
    log("Running shows every worker at work: a follow-up on finished work, one waiting on a card")
    await context.close()

    store.update_draft(question["id"], status="rejected")
    runtime.bus.publish("draft", store.draft(question["id"]))
    store.save_settings(before)
    runtime.bus.publish("state")


async def this_round(runtime, browser, failures: list[str]):
    """The Habits list scrolls in a compact box; the border beside the panel drags (and is
    kept, and resets); more than 100 parallel workers; a test run in progress, a local failure
    the PR's checks passed after, the PR in a few words; what workers told each other."""
    store = runtime.store
    router = store.get("router_id")
    store.save_settings(store.settings().model_copy(update={"max_workers": 8}))
    for index in range(25):
        store.execute(
            "INSERT INTO lessons(id, text, scope, evidence, status, created_at, updated_at) "
            "VALUES (?, ?, ?, '[]', 'active', 'now', 'now')",
            (f"lh{index}", f"Habit {index}: put explanations in the reply itself. " * 3,
             "" if index % 3 else "project:/work/acme/svc"),
        )  # fmt: skip
    spawned = await call_tool(
        runtime, "spawn_worker", router, title="Filter opt-outs", instructions="Filter opt-outs",
        why_new="test",
    )  # fmt: skip
    worker = json.loads(spawned.text_result_for_llm)["session_id"]
    peer_spawn = await call_tool(
        runtime, "spawn_worker", router, title="Opt-out client", instructions="Use the filter",
        why_new="test", works_with=[worker],
    )  # fmt: skip
    peer = json.loads(peer_spawn.text_result_for_llm)["session_id"]
    await runtime.workers.pump()
    await settle(runtime)
    await call_tool(runtime, "tell_session", worker, session_id=peer,
                    text="The filter is in optout.py: is_opted_out(member).")  # fmt: skip
    task = runtime.tasks.current(worker)
    run = store.one("SELECT id FROM runs WHERE task_id=?", (task["id"],))
    for call, command, code, ended in [
        ("u1", "uv run pytest -q tests/test_filter.py", 1, "2026-11-05T09:00:00+00:00"),
        ("u2", "uv run ruff check .", 0, "2026-11-05T09:01:00+00:00"),
    ]:
        store.execute(
            "INSERT INTO commands(session_id, run_id, call_id, command, kind, exit_code, "
            "started_at, ended_at) VALUES (?,?,?,?,?,?,?,?)",
            (worker, run["id"], call, command, "test" if "pytest" in command else "check", code,
             ended, ended),
        )  # fmt: skip
    pr = {"label": "acme/ranking-model#71",
          "url": "https://github.com/acme/ranking-model/pull/71",
          "state": "OPEN", "checks": "passing", "failing": [], "review": "APPROVED",
          "checks_started": "2026-11-05T10:00:00Z", "at": "2026-11-05T10:05:00+00:00"}  # fmt: skip
    store.execute(
        "UPDATE runs SET evidence=? WHERE id=?",
        (json.dumps({"checkouts": [], "prs": [pr]}), run["id"]),
    )
    runtime.tasks.move(task["id"], "review", result="Filtered LEATCH and 3rd party opt-outs.")
    await settle(runtime)

    context = await browser.new_context(viewport={"width": 1400, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    card = page.locator(".card.task.review", has_text="Filter opt-outs")
    await expect(card).to_be_visible(timeout=15000)
    await expect(card.locator(".receipt.pr")).to_have_text(
        "ranking-model#71 · ✓ CI · approved"
    )
    title = await card.locator(".receipt.pr").get_attribute("title")
    assert "acme/ranking-model#71" in title and "Checks pass" in title
    await expect(card.locator(".receipt.warn", has_text="✗ tests locally")).to_be_visible()
    await expect(card.locator(".receipt.pass", has_text="✓ lint")).to_be_visible()
    await expect(card.locator(".flag")).to_contain_text(
        "The last local test run failed (exit 1), but the checks on "
        "acme/ranking-model#71 pass now."
    )
    found = await card.evaluate(OVERFLOWS)
    assert not found, found
    store.execute(
        "INSERT INTO commands(session_id, run_id, call_id, command, kind, exit_code, "
        "started_at, ended_at, shell) VALUES (?,?,?,?,?,NULL,'now',NULL,'7')",
        (worker, run["id"], "u3", "uv run pytest -q tests/", "test"),
    )
    runtime.task_actions.publish(runtime.tasks.get(task["id"]))
    await expect(card.locator(".receipt", has_text="running again")).to_be_visible(timeout=5000)
    await card.screenshot(path=SHOTS / "ui-edges-receipts-now.png")
    log("a task card: the PR in a few words, a local failure CI passed after, a run in progress")

    # What workers told each other: a line in the Router's chat, cards in their timelines.
    await expect(page.locator("#timeline .event", has_text="“Filter opt-outs” told “Opt-out "
                              "client”")).to_be_visible(timeout=5000)  # fmt: skip
    await page.locator("#session-list .item", has_text="Opt-out client").click()
    await expect(page.locator("#timeline .card", has_text="From “Filter opt-outs”")).to_be_visible()
    await page.locator("#session-list .item", has_text="Filter opt-outs").click()
    await expect(page.locator("#timeline .card", has_text="Told “Opt-out client”")).to_be_visible()
    await page.locator("#back-home").click()
    log("workers that work together: who told whom, in the chat and in their timelines")

    # The border beside the panel: dragged, kept after a reload, reset by a double click.
    panel, handle = page.locator("#panel"), page.locator("#panel-resizer")
    await expect(handle).to_be_visible()
    before = (await panel.bounding_box())["width"]
    box = await handle.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + 200
    await page.mouse.move(x, y)
    await page.mouse.down()
    await page.mouse.move(x - 120, y, steps=6)
    await page.mouse.move(x - 240, y, steps=6)
    await page.mouse.up()
    wider = (await panel.bounding_box())["width"]
    assert abs(wider - (before + 240)) <= 6, (before, wider)
    await page.reload()
    await expect(panel).to_be_visible(timeout=15000)
    kept = (await panel.bounding_box())["width"]
    assert abs(kept - wider) <= 2, (wider, kept)
    await handle.focus()
    await page.keyboard.press("ArrowRight")
    assert abs((await panel.bounding_box())["width"] - (kept - 32)) <= 2
    await page.mouse.move(x, y)
    await page.mouse.down()
    await page.mouse.move(40, y, steps=8)
    await page.mouse.up()
    widest = (await panel.bounding_box())["width"]
    chat = (await page.locator("#conversation").bounding_box())["width"]
    assert chat >= 359, ("the chat keeps its room", chat, widest)
    await handle.dblclick()
    assert abs((await panel.bounding_box())["width"] - before) <= 2, "reset"
    await page.set_viewport_size({"width": 1000, "height": 900})
    await expect(handle).to_be_hidden()
    await page.set_viewport_size({"width": 1400, "height": 900})
    log("the panel's border drags, is kept, stops where the chat needs its room, resets")

    # Settings: more than 100 parallel workers; the Habits list scrolls in a compact box.
    await page.locator("#settings-button").click()
    dialog = page.locator("#settings-dialog")
    await dialog.locator("#settings-tabs [data-tab=general]").click()
    await dialog.locator('input[name="max_workers"]').fill("150")
    await dialog.get_by_role("button", name="Save", exact=True).click()
    await expect(dialog).to_be_hidden(timeout=10000)
    assert store.settings().max_workers == 150
    await page.locator("#settings-button").click()
    await dialog.locator("#settings-tabs [data-tab=general]").click()
    await expect(dialog.locator('input[name="max_workers"]')).to_have_value("150")
    await dialog.locator("#settings-tabs [data-tab=you]").click()
    habits = dialog.locator("#lessons")
    await expect(habits.locator("li")).to_have_count(26)  # With "Run a smoke test first".
    await expect(habits.locator("xpath=preceding-sibling::h4[1]")).to_have_attribute(
        "data-count", "26"
    )
    sizes = await habits.evaluate(
        "(list) => ({client: list.clientHeight, scroll: list.scrollHeight, "
        "rows: [...list.children].map((row) => row.getBoundingClientRect().height)})"
    )
    assert sizes["client"] <= 240 and sizes["scroll"] > sizes["client"], sizes
    assert max(sizes["rows"]) <= 40, ("one line a habit", sizes["rows"])
    await habits.evaluate("(list) => list.scrollTo(0, list.scrollHeight)")
    await expect(habits.locator("li").last).to_be_in_viewport()
    row = habits.locator("li", has_text="Habit 1:")
    await row.get_by_role("button", name="more").click()
    await expect(row.locator(".clamp.open")).to_be_visible()
    await habits.screenshot(path=SHOTS / "ui-edges-habits.png")
    await page.locator("#settings-cancel").click()
    log("150 parallel workers saved; 26 habits in a box of a few one-line rows that scrolls")
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
            {"path": f"src/module_{i}.py", "line": 10 + i, "side": "RIGHT", "body": f"Note {i}.",
             "context": f"+    rows = fetch(cursor, limit={i})"}
            for i in range(17)
        ]  # fmt: skip
        many.append({"path": LONG_PATH, "line": 48, "side": "RIGHT", "body": "Keep all scripts.",
                     "context": "-    .replaceAll(\"[^a-z ]\", \"\")\n+    .replaceAll(\"[^a-z0-9 ]"
                     "\", \"\").trim  // " + "only ASCII letters are kept " * 4})  # fmt: skip
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
                plain_card = page.locator(f'#needs .card.draft[data-id="{plain["id"]}"]')
                busy_card = page.locator(f'#needs .card.draft[data-id="{busy["id"]}"]')
                await expect(plain_card).to_be_visible(timeout=15000)
                # The card says which task it is about, and what the PR is, in a line.
                await expect(plain_card.locator(".context")).to_contain_text(
                    "Review the exports PR"
                )
                await expect(plain_card.locator(".pr-line")).to_have_text(
                    "“Export every row” by @maya · +3 −1 in 1 file"
                )
                await expect(plain_card.locator(".comments-note")).to_have_text("No line comments")
                # Compact: the comments are read and changed in the review view.
                await expect(busy_card.locator(".comments-note")).to_have_text("20 line comments")
                await expect(busy_card.locator("textarea")).to_have_count(0)
                await expect(busy_card.get_by_role("button", name="Review…")).to_be_visible()
                for width in (1280, 1100, 800):
                    await page.set_viewport_size({"width": width, "height": 900})
                    if width < 1100:
                        await page.locator("#home-tabs [data-tab=needs]").click()
                    for card in (plain_card, busy_card):
                        found = await card.evaluate(OVERFLOWS)
                        assert not found, (width, found)
                await page.set_viewport_size({"width": 1280, "height": 900})
                await busy_card.get_by_role("button", name="Review…").click()
                view = page.locator(".review-dialog")
                rows = view.locator(".review-comment")
                await expect(rows).to_have_count(20)
                long_row = rows.filter(has_text="CompanyNameNormalizer.scala:48")
                await expect(long_row.locator(".where")).to_have_attribute(
                    "title", f"{LONG_PATH}:48"
                )
                await expect(rows.filter(has_text="old.py:4")).to_contain_text("old")
                await expect(rows.filter(has_text="range.py:140\u2013146")).to_have_count(1)
                await expect(long_row.locator(".diff-context .del")).to_have_count(1)
                await expect(long_row.locator(".diff-context .add")).to_have_count(1)
                for width in (1440, 1024, 800):
                    await page.set_viewport_size({"width": width, "height": 900})
                    await long_row.scroll_into_view_if_needed()
                    for root in (view.locator(".review-body"), long_row, view.locator("footer")):
                        found = await root.evaluate(OVERFLOWS)
                        assert not found, (width, found)
                    page_wide = await page.evaluate(
                        "document.documentElement.scrollWidth > innerWidth"
                    )
                    assert not page_wide, width
                # A long note grows its box (up to ten lines, then scrolls): no overlap.
                boxes = await rows.evaluate_all(
                    "rows => rows.map((row) => [...row.children].map((n) => {"
                    "const b = n.getBoundingClientRect(); return [b.top, b.bottom]; }))"
                )
                for row in boxes:
                    for upper, lower in itertools.pairwise(row):
                        assert upper[1] <= lower[0] + 1, ("overlap", row)
                await page.set_viewport_size({"width": 1280, "height": 900})
                await page.screenshot(path=SHOTS / f"ui-edges-review-{scheme}.png")
                await view.get_by_role("button", name="Close").click()
                await context.close()
            log("review cards: compact, the PR in a line; the view: twenty comments, a long path")

            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            page = await context.new_page()
            page.on("pageerror", lambda error: failures.append(str(error)))
            await page.goto(f"{BASE}/#token={TOKEN}")
            busy_card = page.locator(f'.card.draft[data-id="{busy["id"]}"]')
            # A comment removed, the summary cleared: what is posted says so; the server's 400
            # shows in the view, which comes back as the owner left it.
            await busy_card.get_by_role("button", name="Review…").click()
            view = page.locator(".review-dialog")
            await (
                view.locator(".review-comment")
                .nth(1)
                .get_by_role("button", name="✕ Remove")
                .click()
            )
            await expect(view.locator(".review-comment.removed")).to_have_count(1)
            await (
                view.locator(".review-comment")
                .nth(2)
                .get_by_role("button", name="✕ Remove")
                .click()
            )
            await view.locator(".review-comment").nth(2).get_by_role("button", name="Undo").click()
            await expect(view.locator(".review-comment.removed")).to_have_count(1)
            await view.get_by_label("Summary").fill("")
            await expect(busy_card.locator(".summary-preview")).to_have_text(
                "No summary: the line comments only."
            )
            await expect(busy_card.locator(".comments-note")).to_have_text("19 of 20 line comments")
            refused: list[dict] = []

            async def refuse(route):
                refused.append(route.request.post_data_json)
                await route.fulfill(
                    status=400,
                    json={"detail": "Nothing to post: write a summary or keep a line comment."},
                )

            await page.route(f"**/api/drafts/{busy['id']}", refuse)
            await view.locator("footer button.send").click()
            await expect(view.locator(".error")).to_have_text(
                "Nothing to post: write a summary or keep a line comment.", timeout=5000
            )
            await expect(view).to_be_visible()
            await expect(busy_card).to_be_visible()
            content = refused[0]["content"]
            assert refused[0]["action"] == "send" and content["body"] == "", refused
            assert content["comment:1"] == "" and content["comment:2"] == "Note 2.", content
            assert content["verdict"] == "comment", content
            await expect(view.locator(".review-comment.removed")).to_have_count(1)
            await page.unroute(f"**/api/drafts/{busy['id']}")
            # Sent back to its worker to draft again, with what should change.
            await view.get_by_role("button", name="Ask to redo…").click()
            async with page.expect_request(f"**/api/drafts/{busy['id']}") as request:
                await view.get_by_label("What should change?").fill("Fewer nits; only bugs")
                await view.get_by_role("button", name="Send back").click()
            assert (await request.value).post_data_json == {
                "action": "revise",
                "reason": "Fewer nits; only bugs",
            }
            await expect(view).to_be_hidden()
            await expect(busy_card).to_have_count(0, timeout=10000)
            redone = store.draft(busy["id"])
            assert redone["status"] == "rejected" and redone["outcome"].startswith("Redo"), redone
            log("review view: a comment removed, the summary cleared, the 400 inline; redo")
            store.update_draft(busy["id"], status="pending", outcome="")
            runtime.bus.publish("draft", store.draft(busy["id"]))
            await expect(busy_card).to_be_visible(timeout=10000)
            # Rejecting is the card's other action, behind "⋯".
            await busy_card.locator(".actions .more-button").click()
            await busy_card.get_by_role("menuitem", name="Reject…").click()
            await busy_card.locator(".actions input").fill("Too many nits")
            await busy_card.get_by_role("button", name="Reject", exact=True).click()
            await expect(page.locator(".toast", has_text="Dropped.")).to_be_visible()
            await expect(busy_card).to_have_count(0, timeout=10000)
            assert store.draft(busy["id"])["status"] == "rejected"
            log("rejecting a review with comments removes its card and posts nothing")

            await call_tool(runtime, "report", worker, status="done", summary="Review done.")
            await settle(runtime)
            # Done, with its review still to approve: one card (the draft), not two.
            plain_card = page.locator(f'#needs .card.draft[data-id="{plain["id"]}"]')
            await expect(plain_card.locator(".context")).to_contain_text(
                "Result of Review the exports PR", timeout=10000
            )
            await plain_card.locator(".context summary").click()
            await expect(plain_card.locator(".context .markdown")).to_contain_text("Review done.")
            reviewing = page.locator("#tasks .card.task", has_text="Review the exports PR")
            await expect(reviewing).to_have_count(0)
            # Its verdict can change before it is sent, in the review view.
            await plain_card.get_by_role("button", name="Review…").click()
            view = page.locator(".review-dialog")
            send = view.locator("footer button.send")
            await expect(send).to_have_text("Send review")
            await view.locator("select.verdict").select_option("approve")
            await expect(send).to_have_text("Approve & send")
            await expect(plain_card.locator(".head .chip")).to_have_text("Approve")
            await expect(plain_card.locator(".label")).to_contain_text("Review of acme/plain#7")
            # Acting on a card takes it away at once; if that fails, it is back with why.
            posted: list[dict] = []
            answers = [
                {"status": 409, "json": {"detail": "Sending failed: network down."}},
                {"json": {**store.draft(plain["id"]), "status": "sent"}},
            ]

            async def sending(route):
                posted.append(route.request.post_data_json)
                await asyncio.sleep(0.8)
                await route.fulfill(**answers[len(posted) - 1])

            badge = page.locator(".home-item .badge")
            await expect(badge).to_have_text("1")
            await page.route(f"**/api/drafts/{plain['id']}", sending)
            await send.click()
            # The view closes and the card goes at once; not posted, both come back, with why.
            await expect(view).to_be_hidden()
            await expect(plain_card).to_be_hidden()
            await expect(badge).to_have_count(0)
            await expect(view.locator(".error")).to_contain_text("network down", timeout=5000)
            await expect(plain_card).to_be_visible()
            await expect(badge).to_have_text("1")
            await send.click()
            await expect(plain_card).to_be_hidden()
            await expect(view).to_have_count(0, timeout=5000)
            # Gone for good once the answer comes: the page does not wait for an event.
            await expect(plain_card).to_have_count(0, timeout=5000)
            await expect(page.locator(".toast", has_text="Posted on GitHub.")).to_be_visible()
            assert [body["action"] for body in posted] == ["send", "send"], posted
            assert posted[1]["content"] == {"body": "LGTM.", "verdict": "approve"}, posted
            await page.unroute(f"**/api/drafts/{plain['id']}")
            store.update_draft(plain["id"], status="rejected")
            runtime.bus.publish("draft", store.draft(plain["id"]))
            await expect(reviewing).to_have_count(1, timeout=10000)
            await expect(page.locator("#tasks-review")).to_contain_text("Review the exports PR")
            # In the chat, the report is one line pointing at the task's card.
            reported = page.locator("#timeline .event.done", has_text="Review the exports PR")
            await expect(reported).to_contain_text("finished")
            await expect(page.locator("#timeline")).not_to_contain_text("Review done.")
            await reported.get_by_role("button", name="View").click()
            await expect(reviewing).to_have_class(re.compile(r"\bexpanded\b"))
            await expect(reviewing.locator(".report")).to_contain_text("Review done.")
            # New receipts redraw the card (a commit seen: Create PR comes): the report the
            # owner opened stays open.
            task = runtime.tasks.current(worker)
            run = store.one(
                "SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC", (task["id"],)
            )
            checkout = {"path": "/tmp/exports", "branch": "chief/exports", "commit": "a" * 40,
                        "commits": 1, "files": 1, "insertions": 2, "deletions": 0}  # fmt: skip
            store.execute(
                "UPDATE runs SET evidence=? WHERE id=?",
                (json.dumps({"checkouts": [checkout], "prs": []}), run["id"]),
            )
            runtime.task_actions.publish(runtime.tasks.get(task["id"]))
            await expect(reviewing.locator(".receipt.changes")).to_be_visible(timeout=5000)
            await expect(reviewing).to_have_class(re.compile(r"\bexpanded\b"))
            await expect(reviewing.locator(".expand")).to_have_text("Hide report")
            await expect(reviewing.locator(".report")).to_be_visible()
            log(
                "a done task with a draft: one card; verdict; gone at once, back on error; "
                "its report a line in the chat"
            )

            await page.locator("#session-list .item", has_text="Review the exports PR").click()
            await expect(page.locator("#back-home")).to_be_visible()
            timeline = page.locator("#timeline")
            await expect(timeline).to_contain_text("Review done.")
            answer = "The exporter drops rows past 10k because the cursor resets. " * 6
            runtime.add_message(worker, "assistant", answer)
            runtime.workers.complete_report(worker)
            report = timeline.locator(".card.report")
            await expect(report).to_contain_text("the cursor resets", timeout=10000)
            await expect(report).to_have_count(1)  # Completed in place, not repeated.
            log("a one-line report gets the answer in place in the timeline")
            await page.locator("#back-home").click()
            await expect(page.locator(".home-item.selected")).to_be_visible()
            await context.close()

            await home(runtime, browser, failures)
            await memory(runtime, browser, failures)
            await this_round(runtime, browser, failures)
            await browser.close()
        server.should_exit = True
        await serving
        if failures:
            raise AssertionError(f"browser errors: {failures}")
    print("UI EDGE CASES PASSED")


if __name__ == "__main__":
    asyncio.run(main())
