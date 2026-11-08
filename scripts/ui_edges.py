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
from datetime import UTC, datetime, timedelta
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
    "worker_sandbox", "shared_caches", "sandbox_writable", "worker_idle_hours", "stall_hours",
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

    # Queued: not worked on now, it waits for a slot (Monitoring, marked so).
    queued = page.locator("#tasks-monitoring .card.task.marker-queued", has_text="Fix the parser")
    await expect(queued).to_contain_text("Waiting for a worker slot · 1 of 1 busy")
    await expect(queued.locator(".head .meta")).to_contain_text("Queued")
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
    running = page.locator("#tasks-running .card.task")

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
    # At work on news about its finished task (not the owner's follow-up): a card of its own,
    # named for that task, its result left where it is.
    card = running.filter(has_text="Review api PR #12")
    await expect(card).to_have_class(re.compile(r"\bfollowup\b"))
    await expect(card.locator(".head .meta")).to_have_text("Following up")
    await expect(card.locator(".activity")).to_have_text("reading the reply")
    worker = await spawn("Ship the docs")
    runtime.update_session(worker, status="working")
    card_draft = store.create_draft(worker, "reply", {"to": "Al", "where": "email"}, "Hi")
    runtime.bus.publish("draft", store.draft(card_draft["id"]))
    shown = running.filter(has_text="Ship the docs")
    await expect(shown).to_have_count(1)
    await expect(shown.locator(".receipt.needs", has_text="Needs you")).to_be_visible()
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
    pr = {"label": "linkedin-multiproduct/ad-targeting-model-pytorch#71",
          "url": "https://github.com/linkedin-multiproduct/ad-targeting-model-pytorch/pull/71",
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
        "ad-targeting-model-pytorch#71 · ✓ CI · approved"
    )
    title = await card.locator(".receipt.pr").get_attribute("title")
    assert "linkedin-multiproduct/ad-targeting-model-pytorch#71" in title and "Checks pass" in title
    await expect(card.locator(".receipt.warn", has_text="✗ tests locally")).to_be_visible()
    await expect(card.locator(".receipt.pass", has_text="✓ lint")).to_be_visible()
    await expect(card.locator(".flag")).to_contain_text(
        "The last local test run failed (exit 1), but the checks on "
        "linkedin-multiproduct/ad-targeting-model-pytorch#71 pass now."
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


async def round_two(runtime, browser, failures: list[str]):
    """A risky menu item is readable; a click around a dialog closes it (a selection dragged
    out of it does not); news on finished work waits under Needs you until seen; a
    worker with two tasks shows which it is on; replies say whom they answer and can resolve."""
    store = runtime.store
    router = store.get("router_id")

    async def start(title, **extra):
        result = await call_tool(runtime, "spawn_worker", router, title=title,
                                 instructions=title, why_new="test", **extra)  # fmt: skip
        return json.loads(result.text_result_for_llm)["session_id"]

    first = await start("Editor event paths")
    await settle(runtime)
    second = await call_tool(runtime, "message_session", router, session_id=first,
                             text="Also fix the review comments", task="new",
                             title="Fix review comments on lms", earlier="keep")  # fmt: skip
    assert second.result_type == "success", second.text_result_for_llm
    quiet = await start("Buyer Group Graph 7-pager")
    await settle(runtime)
    runtime.update_session(quiet, status="idle", activity="")  # Between turns, its task on.
    store.execute("UPDATE sessions SET updated_at=? WHERE id=?",
                  ("2026-11-05T09:00:00+00:00", quiet))  # fmt: skip
    finished = await start("Filter opt-outs before inference")
    await call_tool(runtime, "report", finished, status="done", summary="Opened acme/app#71.")
    runtime.task_actions.accept(runtime.tasks.current(finished)["id"])
    news = "PR 71 is approved and ready to merge: you need to click merge."
    await call_tool(runtime, "report", finished, status="progress", summary=news)
    reviewing = await start("Answer PR comments")
    for target, text in [
        ({"repo": "acme/model", "number": 1043, "url": "https://github.com/acme/model/pull/1043",
          "via": "task", "in_reply_to": 21, "resolve": True, "comment": {"author": "ygchen",
          "body": "Opt-out titles are not removed", "path": "seeds.py", "line": 117, "url": "u"}},
         "Fixed in f61047fa."),
        ({"repo": "acme/model", "number": 1043, "url": "https://github.com/acme/model/pull/1043",
          "via": "task", "reply_to_comment": 55, "comment": {"author": "ckrsek", "body":
          "Overall: please document the filter.", "url": "u"}}, "@ckrsek Documented in README."),
    ]:  # fmt: skip
        draft = store.create_draft(reviewing, "github", target, text)
        runtime.bus.publish("draft", draft)
    await settle(runtime)

    context = await browser.new_context(viewport={"width": 1400, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    board = page.locator("#tasks")

    def titled(title):
        return board.locator(".card.task").filter(has=page.locator(".head .title", has_text=title))

    # A worker with two tasks: the one it is on under Running, the other (taken up, then left
    # for it) under Monitoring, paused; the worker's box in each.
    running_box = board.locator(f'#tasks-running .card.worker-box[data-session="{first}"]')
    queued_box = board.locator(f'#tasks-monitoring .card.worker-box[data-session="{first}"]')
    await expect(running_box.locator(".card.task")).to_have_count(1, timeout=15000)
    await expect(running_box.locator(".card.task .head .title")).to_have_text(
        "Fix review comments on lms"
    )
    await expect(running_box.locator(".box-head .meta")).to_contain_text("also: Monitoring")
    await expect(queued_box.locator(".card.task .head .title")).to_have_text("Editor event paths")
    await expect(queued_box.locator(".reason")).to_contain_text(
        "Paused: its worker is on “Fix review comments on lms” now"
    )
    await expect(queued_box.locator(".card.task .head .meta")).to_contain_text("Paused")
    await expect(running_box.locator(".activity")).to_have_count(1)
    await running_box.screenshot(path=SHOTS / "ui-edges-worker-group.png")
    # Its conversation takes up the earlier task again: that one runs now, the newer waits.
    earlier = next(
        task for task in runtime.tasks.for_session(first) if task["title"] == "Editor event paths"
    )
    runtime.states.handle(first).emit(
        "user.message",
        content=f'<message from="owner" type="message" task="{earlier["id"]}">\nGo on\n</message>',
    )
    await settle(runtime)
    runtime.publish_session(store.session(first))
    await expect(running_box.locator(".card.task .head .title")).to_have_text(
        "Editor event paths", timeout=10000
    )
    await expect(queued_box.locator(".card.task .head .title")).to_have_text(
        "Fix review comments on lms"
    )
    # Its worker between turns, nothing registered to wait on: not running, and it says so.
    idle = board.locator("#tasks-monitoring .card.task", has_text="Buyer Group Graph 7-pager")
    await expect(idle.locator(".reason.warn")).to_contain_text("nothing registered to wait on")
    await expect(idle.get_by_role("button", name="Nudge")).to_be_visible()
    log("a worker with two tasks: the one it is on runs, the other paused; a quiet one says why")

    update = page.locator("#needs .card.task", has_text="Filter opt-outs before inference")
    await expect(update.locator(".news")).to_contain_text("approved and ready to merge")
    await expect(update.locator(".news .chip")).to_have_text("Update")
    await update.get_by_role("button", name="Got it").click()
    await expect(update).to_have_count(0, timeout=10000)
    await expect(
        page.locator("#tasks-recent .card.task", has_text="Filter opt-outs")
    ).to_have_count(1)
    log("news on finished work: under Needs you until Got it, then back with the finished")

    # The reply itself is in a text box: the cards are found by what they answer.
    line_reply = page.locator(".card.draft", has_text="Opt-out titles are not removed")
    resolve = line_reply.locator('label.resolve input[type="checkbox"]')
    await expect(resolve).to_be_checked()
    talk = page.locator(".card.draft", has_text="please document the filter")
    await expect(talk.locator(".quoted")).to_contain_text("ckrsek: Overall: please document")
    await expect(talk.locator("label.resolve")).to_have_count(0)
    await line_reply.screenshot(path=SHOTS / "ui-edges-reply-resolve.png")
    log("a reply on a line can resolve its conversation; one in the conversation names its author")

    card = running_box.locator(".card.task").first
    await card.locator(".more-button").click()
    danger = card.locator(".menu button.danger")
    await expect(danger).to_have_text("Cancel task…")
    colors = await danger.evaluate(
        "(b) => [getComputedStyle(b).color, getComputedStyle(b).backgroundColor]"
    )
    assert colors[0] != colors[1] and colors[1] in {"rgba(0, 0, 0, 0)", "transparent"}, colors
    await card.screenshot(path=SHOTS / "ui-edges-menu-danger.png")
    await page.keyboard.press("Escape")
    log("a risky item in a menu is red text on the menu, readable")

    await page.locator("#settings-button").click()
    dialog = page.locator("#settings-dialog")
    await expect(dialog).to_be_visible()
    box = await dialog.bounding_box()
    # A selection dragged from inside out of it keeps it open.
    await page.mouse.move(box["x"] + 200, box["y"] + 120)
    await page.mouse.down()
    await page.mouse.move(box["x"] - 30, box["y"] + 120, steps=4)
    await page.mouse.up()
    await expect(dialog).to_be_visible()
    await page.mouse.click(box["x"] - 30, box["y"] + 40)
    await expect(dialog).to_be_hidden()
    await page.locator("#usage-button").click()
    await expect(page.locator("#usage-dialog")).to_be_visible()
    await page.mouse.click(8, 8)
    await expect(page.locator("#usage-dialog")).to_be_hidden()
    log("a click around a dialog closes it; a selection dragged out of it does not")
    await context.close()


async def round_three(runtime, browser, failures: list[str]):
    """A worker's question stays under Needs you whatever became of its task, beside its drafts;
    a PR ready to merge is a card; sidebar dots say what Home says, with a legend; a worker's
    card lists its tasks with their progress and what they made, without overflowing; tasks
    waiting on others have their group and counts agree; a title opens the worker at that
    task; tables render; habits can be added; watches save before a scan; menus and the Slack
    problem line can be closed."""
    store = runtime.store
    router = store.get("router_id")

    async def start(title, **extra):
        result = await call_tool(runtime, "spawn_worker", router, title=title,
                                 instructions=title, why_new="test", **extra)  # fmt: skip
        assert result.result_type == "success", result.text_result_for_llm
        return json.loads(result.text_result_for_llm)["session_id"]

    # A question whose task was cancelled meanwhile.
    asker = await start("Pick the database")
    await call_tool(runtime, "report", asker, status="question", summary="Postgres or SQLite?")
    await runtime.task_actions.cancel(runtime.tasks.current(asker)["id"])
    # A task waiting for an answer, its worker with a draft too: both show.
    both = await start("Answer Carol")
    draft = store.create_draft(both, "reply", {"to": "Carol", "where": "email"}, "Friday works")
    runtime.bus.publish("draft", draft)
    await call_tool(runtime, "report", both, status="question", summary="Which Friday?")
    # A PR ready to merge.
    merger = await start("Ship the login fix")
    merge = store.create_draft(merger, "merge", {
        "label": "acme/app#7", "repo": "acme/app", "number": 7, "title": "Fix login",
        "url": "https://github.com/acme/app/pull/7", "head": "abc1234def", "review": "APPROVED",
        "task_id": runtime.tasks.current(merger)["id"],
    }, "Fix login")  # fmt: skip
    runtime.bus.publish("draft", merge)
    # A worker with two tasks, long markdown progress and links.
    busy = await start("Research the Slack bot")
    await call_tool(runtime, "message_session", router, session_id=busy, task="new",
                    title="CoS bug bash", text="Now the bug bash", earlier="keep")  # fmt: skip
    long = ("**Status:** fixed `B16`; see [the doc](https://docs.google.com/document/d/abc) and "
            "https://github.com/acme/app/pull/592 — " + "more words here " * 120)  # fmt: skip
    for task in runtime.tasks.for_session(busy):
        runtime.tasks.move(task["id"], "running")
        runtime.task_actions.publish(runtime.tasks.update(task["id"], progress=long))
    runtime.update_session(busy, status="working", activity="Measure the row overflow")
    # Running, between turns, watching a PR: waiting on others.
    watcher = await start("Watch CI on #12")
    runtime.follow(watcher, ["github:acme/app#12"])
    runtime.update_session(watcher, status="idle", activity="")
    # A finished task whose worker went on: its title opens it at the report.
    done = await start("Count the orders")
    done_task = runtime.tasks.current(done)
    await call_tool(runtime, "report", done, status="done", summary="There are 412 orders.")
    for number in range(40):
        runtime.add_message(done, "assistant", f"Later remark {number}. " * 6)
    # A table with a pipe in code, an empty row and a blank line inside.
    table = ("| name | value | note |\n|---|:-:|---:|\n| a | `x|y` | c\\|d |\n|  |  |  |\n\n"
             "| b | 2 | " + "long-cell-" * 30 + " |")  # fmt: skip
    runtime.add_message(router, "assistant", table)
    await settle(runtime)
    runtime.publish_state()

    context = await browser.new_context(viewport={"width": 1400, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    needs = page.locator("#needs")

    question = needs.locator(".card.question-card", has_text="Postgres or SQLite?")
    await expect(question).to_be_visible(timeout=15000)
    await expect(question.locator(".head .meta")).to_contain_text("task cancelled")
    await expect(needs.locator(".card.draft", has_text="Carol")).to_be_visible()
    await expect(needs.locator(".card.task", has_text="Which Friday?")).to_be_visible()
    log("a question stays under Needs you after its task is cancelled; a task and its draft both")

    merge_card = needs.locator(".card.draft.merge")
    await expect(merge_card).to_contain_text("Ready to merge · app#7")
    await expect(merge_card.get_by_role("button", name="Merge")).to_be_visible()
    await merge_card.locator(".more-button").click()
    await merge_card.get_by_role("menuitem", name="Not now").click()
    await expect(merge_card).to_have_count(0, timeout=10000)
    assert store.draft(merge["id"])["status"] == "rejected"
    log("a PR ready to merge is a card: Merge, or not now")

    # The sidebar says what Home says.
    def dot(title):
        return page.locator("#session-list .item", has_text=title).locator(".dot")

    await expect(dot("Pick the database")).to_have_class(re.compile(r"\bwaiting\b"))
    await expect(dot("Answer Carol")).to_have_class(re.compile(r"\bwaiting\b"))
    await expect(dot("Watch CI on #12")).to_have_class(re.compile(r"\bwatching\b"))
    # A report that only informs is over once delivered: its worker has nothing open, folded
    # away with the others like it.
    await expect(dot("Count the orders")).to_have_class(re.compile(r"\bidle\b"))
    idle_fold = page.locator("#session-list details.idle-workers")
    await expect(idle_fold.locator(".item", has_text="Count the orders")).to_have_count(1)
    await expect(idle_fold).not_to_have_attribute("open", "")
    await expect(dot("CoS bug bash")).to_have_class(re.compile(r"\bworking\b"))
    await page.locator("#session-list .legend-toggle").click()
    # Four places and a session open in a terminal: no more colors than that.
    await expect(page.locator("#session-list .legend .legend-row")).to_have_count(5)
    await page.locator("#session-list .legend-toggle").click()
    await expect(page.locator("#session-list .legend")).to_have_count(0)
    log("sidebar dots follow Home's four places, with a legend; idle workers fold away")

    group = page.locator(f'#tasks-running .card.worker-box[data-session="{busy}"]')
    waits = page.locator(f'#tasks-monitoring .card.worker-box[data-session="{busy}"]')
    await expect(group.locator(".card.task")).to_have_count(1)
    await expect(group.locator(".card.task .head .title")).to_have_text("CoS bug bash")
    await expect(waits.locator(".card.task .head .title")).to_have_text("Research the Slack bot")
    for box in (group, waits):
        card_box = await box.bounding_box()
        row = box.locator(".card.task").first
        row_box = await row.bounding_box()
        assert row_box["width"] <= card_box["width"], (row_box, card_box)
        meta_box = await row.locator(".head .meta").bounding_box()
        assert meta_box["x"] + meta_box["width"] <= card_box["x"] + card_box["width"] + 1
        menu_box = await row.locator(".more-button").bounding_box()
        assert menu_box["x"] + menu_box["width"] <= card_box["x"] + card_box["width"] + 1
        assert not await box.evaluate(OVERFLOWS), await box.evaluate(OVERFLOWS)
    progress = group.locator(".progress-line").first
    await expect(progress).not_to_contain_text("**")
    await expect(progress).to_contain_text("Status: fixed B16")
    await expect(progress).to_contain_text("app#592 — more words")
    height = await progress.evaluate("n => n.getBoundingClientRect().height")
    line = await progress.evaluate("n => parseFloat(getComputedStyle(n).lineHeight)")
    assert height <= line * 2.2, (height, line)
    # When it reported is on its own line, never cut off with the words.
    when = group.locator(".progress > .when").first
    await expect(when).to_contain_text("Reported")
    when_box, line_box = await when.bounding_box(), await progress.bounding_box()
    assert when_box["y"] >= line_box["y"] + line_box["height"] - 1, (when_box, line_box)
    await group.screenshot(path=SHOTS / "ui-edges-worker-card.png")
    await progress.click()
    assert await progress.evaluate("n => n.getBoundingClientRect().height") > height * 2
    # Redrawn (a new step), the report it opened stays open.
    runtime.update_session(busy, status="working", activity="Measure the row overflow again")
    await expect(group.locator(".activity")).to_contain_text("again")
    assert await progress.evaluate("n => n.classList.contains('open')")
    await progress.click()
    chips_now = group.locator(".card.task").first.locator(".artifacts")
    await expect(chips_now.locator(".receipt", has_text="Google Doc")).to_be_visible()
    # A PR it only mentions (not one it made) is a reference: folded under "More links".
    await expect(chips_now.locator(".more-links summary")).to_have_text("More links: 1 referenced")
    await expect(chips_now.locator(".more-links .receipt", has_text="app#592")).to_have_count(1)
    await expect(group.locator(".activity")).to_contain_text("Measure the row overflow")
    log("a worker's box per section: its task there, two clean lines, when apart, its links")

    asking_card = page.locator("#tasks .card.task", has_text="Answer Carol")
    if await asking_card.count():
        await expect(asking_card.locator(".receipt.needs")).to_be_visible()
    watching = page.locator("#tasks-monitoring .card.task", has_text="Watch CI on #12")
    await expect(watching).to_be_visible()
    await expect(watching.locator(".receipt.watching")).to_have_text("Watching app#12")
    counts = await page.evaluate("""() => {
      const n = (id) => Number(document.querySelector(`#${id} .count`)?.textContent || 0);
      return [n("tasks"), n("tasks-running") + n("tasks-monitoring")];
    }""")
    assert counts[0] == counts[1], counts
    await expect(watching.locator(".reason")).to_contain_text("Waiting on app#12")
    log("monitoring says what it waits on; the Tasks count is the sum of its groups")

    rendered = page.locator("#timeline .markdown .table").last
    await expect(rendered.locator("tbody tr")).to_have_count(2)
    await expect(rendered.locator("thead th")).to_have_count(3)
    await expect(rendered.locator("tbody tr").first.locator("td").nth(1)).to_have_text("x|y")
    await expect(rendered.locator("tbody tr").first.locator("td").nth(2)).to_have_text("c|d")
    message = rendered.locator("xpath=ancestor::*[contains(@class,'msg')][1]")
    table_box, message_box = await rendered.bounding_box(), await message.bounding_box()
    assert table_box["width"] <= message_box["width"] + 1, (table_box, message_box)
    log("a table: no blank row or column, a pipe in code stays, it fits its message")

    recent = page.locator("#tasks-recent")
    if not await recent.evaluate("n => n.open"):
        await recent.locator("summary").click()
    await recent.locator(".card.task", has_text="Count the orders").locator(".head .title").click()
    report = page.locator(f'#timeline [data-kind="report"][data-task="{done_task["id"]}"]')
    await expect(report).to_be_in_viewport(timeout=10000)
    await expect(page.locator("#timeline > *").last).not_to_be_in_viewport()
    log("a title opens its worker at that task's report, not at its latest message")

    await page.locator("#session-more-button").click()
    await expect(page.locator("#session-more")).to_be_visible()
    await page.mouse.click(700, 500)
    await expect(page.locator("#session-more")).to_be_hidden()
    await page.locator("#back-home").click()
    log("the session's ⋯ menu closes on a click elsewhere")

    await page.locator("#settings-button").click()
    await page.locator('#settings-tabs [data-tab="you"]').click()
    await page.locator("#lesson-text").fill("Run the linter before every commit")
    await page.locator("#lesson-text").press("Enter")
    await expect(page.locator("#lessons")).to_contain_text("Run the linter before every commit")
    await expect(page.locator("#settings-dialog")).to_be_visible()
    log("a habit added by hand shows at once; Enter does not submit Settings")

    await page.locator('#settings-tabs [data-tab="connections"]').click()
    watches = page.locator("#conn-watches")
    await watches.locator("summary").click()
    await expect(page.locator("#watches-summary")).to_have_text("None")
    await page.locator("#watch-add").click()
    await page.locator("#watch-list li .what").last.fill("Questions for me in #eng")
    await expect(page.locator("#watches-summary")).to_have_text("1 on · not saved yet")
    await page.locator("#watch-scan").click()
    await expect(page.locator(".toast", has_text="Scanning now.")).to_be_visible(timeout=10000)
    assert [w.what for w in store.settings().watches] == ["Questions for me in #eng"]
    await page.keyboard.press("Escape")
    log("Scan now saves the watch just written first; the summary counts what is written")

    store.save_settings(store.settings().model_copy(update={"slack_enabled": True}))
    runtime.slack.status["error"] = "Add your Slack user token in Settings."
    runtime.publish_state()
    line = page.locator("#status-line .bad", has_text="Slack:")
    await expect(line).to_be_visible(timeout=10000)
    await line.get_by_role("button", name="Turn off").click()
    await expect(line).to_have_count(0, timeout=10000)
    assert not store.settings().slack_enabled
    log("the Slack problem line can turn Slack watching off")
    await context.close()


async def round_four(runtime, browser, failures: list[str]):
    """The v0.1.24 bug bash. A result to review with a long PR title, its changes and runs fits
    at 352-400px, runs under a run folded (C1, C2); a worker following up on finished work is
    named for that work and fits (C13); workers are named by their work in the sidebar, the
    header and Closed (C14); a link that cannot be decoded does not stop Home, and a card that
    cannot be drawn does not stop the others (C10); a monitored task says what it waits on,
    a check-back is its own task's whatever its worker does (C3); a thread that cannot be read
    can be let go (C5); finished tasks newest first across workers, a page at a time (C11)."""
    store = runtime.store
    router = store.get("router_id")
    store.save_settings(store.settings().model_copy(update={"max_workers": 30}))

    async def start(title):
        result = await call_tool(runtime, "spawn_worker", router, title=title,
                                 instructions=title, why_new="test")  # fmt: skip
        assert result.result_type == "success", result.text_result_for_llm
        await settle(runtime)
        return json.loads(result.text_result_for_llm)["session_id"]

    async def another(worker, title):
        result = await call_tool(runtime, "message_session", router, session_id=worker,
                                 task="new", title=title, text=title, earlier="keep")  # fmt: skip
        assert result.result_type == "success", result.text_result_for_llm
        await settle(runtime)
        return next(t for t in runtime.tasks.for_session(worker) if t["title"] == title)

    # C1/C2: to review, a PR with a long title, its changes, a Flyte run and what is under it.
    fixer = await start("Fix the Spark 3.5 dependency of the ICP flow")
    fix = runtime.tasks.current(fixer)
    label = "acme/ad-targeting-model-data#594"
    store.set(f"prtitle:{label}", "Upgrade the ICP flow to Spark 3.5 and pin the shaded " * 3)
    pr = {"label": label, "url": "https://github.com/acme/ad-targeting-model-data/pull/594",
          "state": "OPEN", "checks": "passing", "failing": [], "review": "", "own": True,
          "about": True, "at": datetime.now(UTC).isoformat()}  # fmt: skip
    checkout = {"path": "/tmp/icp", "branch": "chief/icp", "commit": "b" * 40, "commits": 2,
                "files": 3, "insertions": 40, "deletions": 12}  # fmt: skip
    run = store.one("SELECT id FROM runs WHERE task_id=?", (fix["id"],))
    store.execute("UPDATE runs SET evidence=? WHERE id=?",
                  (json.dumps({"checkouts": [checkout], "prs": [pr]}), run["id"]))  # fmt: skip
    runtime.tasks.move(fix["id"], "review", result=(
        f"Opened {pr['url']}. The flow ran: "
        "https://flyte.example.com/console/projects/p/domains/d/executions/abc123 . Spark: "
        "https://shs.example.com/history/application_1700000000000_0042/jobs/ ; debug "
        "https://box-rdev-1.example.com/debug"))  # fmt: skip
    runtime.update_session(fixer, status="idle", activity="")

    # C13/C14: started for one thing, now on another, finished; at work on news about it.
    researcher = await start("Research Peter Huang's Slack-sending skill")
    await another(researcher, "Chief of Staff merge bug report")
    for item in runtime.tasks.for_session(researcher):
        runtime.tasks.move(item["id"], "done", result="Done.")

    # C3: delivered and still followed; a check-back while its worker is on another task.
    shipper = await start("Ship the exporter")
    shipped = runtime.tasks.current(shipper)
    runtime.tasks.move(shipped["id"], "done", result="Opened acme/app#77.")
    runtime.follow(shipper, ["github:acme/app#77"], task_id=shipped["id"])
    runtime.update_session(shipper, status="idle", activity="")
    weekly = await start("Run the weekly embeddings")
    first = runtime.tasks.current(weekly)
    at = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    runtime.task_actions.set_check_back(first, at, "the Flyte run finishing", "")
    weekly_run = "https://flyte.example.com/console/projects/p/domains/d/executions/weekly42"
    runtime.task_actions.declare_artifacts(
        first["id"], [{"url": weekly_run, "name": "Weekly embeddings run", "status": "running"}]
    )
    answer = await another(weekly, "Answer Jingtao about the two gets")
    runtime.tasks.move(first["id"], "running")
    runtime.tasks.move(answer["id"], "running")
    store.set(f"turntask:{weekly}", answer["id"])
    runtime.update_session(weekly, status="working", activity="Reading the client code")

    # C5: a thread it cannot read, for its task.
    stuck = await start("Watch the example PR")
    runtime.follow(stuck, ["github:acme/app#42"], task_id=runtime.tasks.current(stuck)["id"])
    store.set("ghfail:github:acme/app#42", "3")
    runtime.update_session(stuck, status="idle", activity="")

    # C10: links that cannot be decoded, with parentheses, ending in "_".
    spec = await start("Draft the AI interviewer spec")
    runtime.task_actions.publish(runtime.tasks.update(
        runtime.tasks.current(spec)["id"],
        progress="See https://example.com/report%zz and https://en.wikipedia.org/wiki/Foo_(bar) "
        "and https://example.com/run_ for the details.",
    ))  # fmt: skip

    # C14: a closed worker by its latest work.
    repopulate = await start("Repopulate buyer_advertiser_features_v1 via Darwin")
    recheck = await another(repopulate, "Recheck buyer_advertiser_features_v1 has data")
    for item in runtime.tasks.for_session(repopulate):
        runtime.tasks.move(item["id"], "done", result="Done.")
    await runtime.workers.close(repopulate)

    # C11: 33 finished tasks of two workers, ended a minute apart, the workers interleaved.
    batch = {name: await start(name) for name in ("Batch A", "Batch B")}
    stamp = datetime.now(UTC)
    for index in range(33):
        owner = batch["Batch B" if 10 <= index < 20 else "Batch A"]
        made = runtime.tasks.create(owner, f"Finished item {index:02d}", "x", status="running")
        runtime.tasks.move(made["id"], "done", result="ok")
        ended = (stamp - timedelta(minutes=index + 1)).isoformat()
        store.execute("UPDATE tasks SET ended_at=?, updated_at=? WHERE id=?",
                      (ended, ended, made["id"]))  # fmt: skip
    for worker in batch.values():
        for item in runtime.tasks.for_session(worker):
            if item["status"] != "done":
                runtime.tasks.move(item["id"], "done", result="ok")
        runtime.update_session(worker, status="idle", activity="")
    await settle(runtime)
    # At work on news about its finished tasks, its last instruction on neither.
    store.set(f"turntask:{researcher}", "elsewhere")
    runtime.update_session(researcher, status="working", activity="Reading the reply on #352")
    runtime.publish_state()

    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    needs, running = page.locator("#needs"), page.locator("#tasks-running")
    monitoring = page.locator("#tasks-monitoring")

    review = needs.locator(".card.task", has_text="Fix the Spark 3.5 dependency")
    await expect(review).to_be_visible(timeout=15000)
    await expect(review.locator(".head .meta")).to_contain_text("To review")
    await expect(review.locator(".receipt.pr .pr-state")).to_have_text(" · ✓ CI")
    await expect(review.locator(".receipt.changes")).to_have_text("+40 −12 in 3 files")
    main = review.locator(".chips > .receipts > .receipt.artifact")
    await expect(main).to_have_text(["Flyte · abc123"])
    folded = review.locator(".chips .more-links")
    await expect(folded.locator("summary")).to_have_text("More links: 2 for diagnosis")
    await expect(folded.locator(".receipt", has_text="Spark")).to_be_hidden()
    follow_up = running.locator(f'.card.task.followup[data-key="followup:{researcher}"]')
    await expect(follow_up.locator(".head .title")).to_have_text("Chief of Staff merge bug report")
    await expect(follow_up.locator(".head .meta")).to_have_text("Following up")
    for width in (352, 376, 400):
        await page.evaluate("w => document.querySelector('#panel').style.width = `${w}px`", width)
        for card in (review, follow_up):
            found = await card.evaluate(OVERFLOWS)
            assert not found, (width, found)
            outer = await card.bounding_box()
            for part in await card.locator(".receipt, .head > *").all():
                if not await part.is_visible():
                    continue
                inner = await part.bounding_box()
                assert inner["x"] + inner["width"] <= outer["x"] + outer["width"] + 1, (
                    width, await part.text_content(), inner, outer)  # fmt: skip
        if width == 352:
            await review.screenshot(path=SHOTS / "ui-edges-review-352.png")
            await follow_up.screenshot(path=SHOTS / "ui-edges-followup-352.png")
        title_box = await follow_up.locator(".head .title").bounding_box()
        assert title_box["width"] > 80, (width, title_box)
        state = await review.locator(".receipt.pr .pr-state").bounding_box()
        assert state["width"] > 20, ("where the PR stands stays in sight", width, state)
    await page.evaluate("document.querySelector('#panel').style.removeProperty('width')")
    await folded.locator("summary").click()
    await expect(folded.locator(".receipt", has_text="Spark · application_1700000000000_0042"))\
        .to_be_visible()  # fmt: skip
    log("to review: the PR, changes and the run fit at 352-400px; Spark and debug folded")
    log("a follow-up on finished work: named for it, a short meta, fits at 352-400px")

    item = page.locator(f'#session-list .item[data-id="{researcher}"]')
    await expect(item.locator(".label")).to_have_text("Chief of Staff merge bug report")
    await expect(item).to_have_attribute("title", re.compile("Started as “Research Peter"))
    await item.click()
    await expect(page.locator("#session-title")).to_have_text("Chief of Staff merge bug report")
    await expect(page.locator("#session-title")).to_have_attribute(
        "title", "Started as “Research Peter Huang's Slack-sending skill”"
    )
    await page.locator("#back-home").click()
    closed = page.locator("#closed-list")
    await expect(closed).to_contain_text("Recheck buyer_advertiser_features_v1 has data")
    await expect(closed).not_to_contain_text("Repopulate")
    assert recheck["title"] in (await closed.text_content())
    log("workers by their work: the sidebar, the header (started as… on hover), Closed")

    delivered = monitoring.locator(".card.task", has_text="Ship the exporter")
    await expect(delivered.locator(".reason")).to_contain_text("Delivered; still following app#77")
    await expect(delivered.locator(".head .meta")).to_contain_text("Delivered")
    later = monitoring.locator(".card.task", has_text="Run the weekly embeddings")
    await expect(later.locator(".reason")).to_contain_text("the Flyte run finishing")
    await expect(later.locator(".head .meta")).to_contain_text("Checks back")
    await expect(running.locator(".card.task", has_text="Answer Jingtao")).to_have_count(1)
    weekly_box = running.locator(f'.card.worker-box[data-session="{weekly}"]')
    await expect(weekly_box.locator(".box-head .meta")).to_contain_text("also: Monitoring")
    log("monitoring says what it waits on; a check-back is its task's while its worker works")

    cannot = page.locator(".card.task", has_text="Watch the example PR")
    chip = cannot.locator(".receipt.failing")
    await expect(chip).to_contain_text("Can't read app#42")
    await expect(cannot.locator(".receipt.watching")).to_have_count(0)
    await chip.get_by_role("button", name="stop following").click()
    await expect(page.locator(".toast", has_text="Stopped following app#42.")).to_be_visible()
    assert "github:acme/app#42" not in store.follows(stuck)
    await expect(chip).to_have_count(0, timeout=10000)
    log("a thread it cannot read is said so, not waited on, and can be let go")

    spec_card = page.locator(".card.task", has_text="Draft the AI interviewer spec")
    await expect(spec_card.locator(".more-links summary")).to_be_visible()
    hrefs = await spec_card.locator(".more-links a").evaluate_all("as => as.map((a) => a.href)")
    assert "https://example.com/report%zz" in hrefs, hrefs
    assert "https://en.wikipedia.org/wiki/Foo_(bar)" in hrefs, hrefs
    assert "https://example.com/run_" in hrefs, hrefs
    # Home goes on: a task added after it shows.
    await start("Proof Home still redraws")
    await expect(page.locator("#tasks", has_text="Proof Home still redraws")).to_have_count(
        1, timeout=10000
    )
    isolated = await page.evaluate("""async () => {
      const d = await import("/static/dom.js");
      const box = document.createElement("div");
      const quiet = console.error;
      console.error = () => {};
      try {
        d.reconcile(box, [
          { key: "bad", build: () => { throw new URIError("URI malformed"); } },
          { key: "good", build: () => d.el("div", { text: "drawn" }) },
        ]);
      } finally {
        console.error = quiet;
      }
      return [...box.children].map((n) => [n.className, n.textContent]);
    }""")
    assert isolated == [
        ["card broken", "This card could not be shown: URI malformed"],
        ["", "drawn"],
    ], isolated
    log("a malformed link does not stop Home; a card that cannot be drawn leaves the others")

    # Artifacts: every PR, doc and run in one list, where each stands, in a box that scrolls.
    made = page.locator("#tasks-artifacts")
    await expect(made).to_have_attribute("open", "")
    rows = made.locator(".artifact")
    pr_row = rows.filter(has_text="ad-targeting-model-data#594")
    await expect(pr_row.locator(".a-kind")).to_have_text("PR")
    await expect(pr_row.locator(".a-status")).to_have_text("in review")
    await expect(pr_row.locator(".a-task")).to_have_text(
        "Fix the Spark 3.5 dependency of the ICP flow"
    )
    run_row = rows.filter(has_text="Weekly embeddings run")
    await expect(run_row.locator(".a-kind")).to_have_text("Flyte")
    await expect(run_row.locator(".a-status")).to_have_text("running")
    await expect(run_row).to_have_class(re.compile(r"\btone-active\b"))
    told = rows.filter(has_text="abc123")  # Named only in a report: as its task stands.
    await expect(told.locator(".a-status")).to_have_text("to review")
    await expect(told.locator(".a-status.derived")).to_have_count(1)
    await expect(rows.filter(has_text="application_1700000000000_0042")).to_have_count(0)
    box = await made.locator(".items").evaluate(
        "n => [getComputedStyle(n).maxHeight, getComputedStyle(n).overflowY]"
    )
    assert box == ["280px", "auto"], box
    for width in (352, 400):
        await page.evaluate("w => document.querySelector('#panel').style.width = `${w}px`", width)
        found = await made.evaluate(OVERFLOWS)
        assert not found, (width, found)
    await made.screenshot(path=SHOTS / "ui-edges-artifacts-400.png")
    await page.evaluate("document.querySelector('#panel').style.removeProperty('width')")
    before = await rows.count()
    await run_row.get_by_role("button", name="Take Weekly embeddings run off this list").click()
    await expect(rows).to_have_count(before - 1)
    await expect(page.locator(".toast", has_text="Taken off Artifacts")).to_be_visible()
    assert json.loads(store.get("artifacts_hidden")) == [weekly_run]
    await made.get_by_role("button", name="Show 1 taken off").click()
    await expect(run_row).to_have_class(re.compile(r"\bhidden-artifact\b"))
    await run_row.get_by_role("button", name="Bring back").click()
    await expect(run_row).not_to_have_class(re.compile(r"\bhidden-artifact\b"))
    assert json.loads(store.get("artifacts_hidden")) == []
    # Where it stands changes: the row says so as it comes.
    runtime.task_actions.declare_artifacts(
        first["id"], [{"url": weekly_run, "name": "Weekly embeddings run", "status": "failed"}]
    )
    await expect(run_row.locator(".a-status")).to_have_text("failed", timeout=10000)
    await expect(run_row).to_have_class(re.compile(r"\btone-bad\b"))
    await expect(rows.first).to_contain_text("Weekly embeddings run")  # What needs a look first.
    await run_row.locator(".a-task").click()
    await expect(
        page.locator(".card.task.flash", has_text="Run the weekly embeddings")
    ).to_be_visible()
    log("artifacts: PRs, docs and runs in one scrolling list, live; one taken off and back")

    recent = page.locator("#tasks-recent")
    if not await recent.evaluate("n => n.open"):
        await recent.locator("summary").click()
    cards = recent.locator(".card.task")
    total = int(await page.evaluate(
        "() => document.querySelector('#tasks-recent .count').textContent.split(' of ')[1]"
    ))  # fmt: skip
    assert total >= 33, total
    await expect(cards).to_have_count(30)
    await expect(recent.locator("summary .count")).to_have_text(f"30 of {total}")
    more = recent.get_by_role("button", name=re.compile(r"^Show \d+ more$"))
    await expect(more).to_have_text(f"Show {min(30, total - 30)} more")

    async def in_order():
        ids = await cards.evaluate_all("cs => cs.map((c) => c.dataset.task)")
        ended = [store.one("SELECT ended_at, updated_at FROM tasks WHERE id=?", (i,))
                 for i in ids]  # fmt: skip
        stamps = [row["ended_at"] or row["updated_at"] for row in ended]
        assert stamps == sorted(stamps, reverse=True), list(zip(ids, stamps, strict=True))
        return ids

    shown = await in_order()
    items = [store.one("SELECT title FROM tasks WHERE id=?", (i,))["title"] for i in shown]
    batch_items = [title for title in items if title.startswith("Finished item")]
    assert batch_items == sorted(batch_items), batch_items
    # One worker's tasks next to each other share a box; another worker's in between part them.
    heads = await recent.locator(".card.worker-box").evaluate_all(
        "bs => bs.map((b) => [b.dataset.session, b.querySelectorAll('.card.task').length])"
    )
    assert all(a[0] != b[0] for a, b in itertools.pairwise(heads)), heads
    await more.click()
    await expect(cards).to_have_count(min(60, total))
    await in_order()
    log(f"recently finished: newest first across workers, 30 of {total}, then more")

    assert not errors, errors
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
            reviewing = page.locator("#panel .card.task", has_text="Review the exports PR")
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
            # A review that changed nothing informs: read, it is with the finished ones (no
            # Accept to click), marked a report.
            await expect(page.locator("#tasks-recent")).to_contain_text("Review the exports PR")
            await expect(reviewing.locator(".head .meta")).to_contain_text("Report")
            await expect(reviewing.get_by_role("button", name="Accept")).to_have_count(0)
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
            # It changed code now: a result to accept, under Needs you.
            await expect(
                page.locator("#needs .card.task", has_text="Review the exports")
            ).to_have_count(1, timeout=5000)
            await expect(reviewing.locator(".head .meta")).to_contain_text("To review")
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
            # Accepted from its card: it leaves Needs you for the finished ones.
            await reviewing.get_by_role("button", name="Accept").click()
            await expect(page.locator("#needs .card.task")).to_have_count(0, timeout=5000)
            await expect(page.locator("#tasks-recent")).to_contain_text("Review the exports PR")
            assert runtime.tasks.get(task["id"])["status"] == "done"
            await context.close()

            await home(runtime, browser, failures)
            await memory(runtime, browser, failures)
            await this_round(runtime, browser, failures)
            await round_two(runtime, browser, failures)
            await round_three(runtime, browser, failures)
            await round_four(runtime, browser, failures)
            await browser.close()
        server.should_exit = True
        await serving
        if failures:
            raise AssertionError(f"browser errors: {failures}")
    print("UI EDGE CASES PASSED")


if __name__ == "__main__":
    asyncio.run(main())
