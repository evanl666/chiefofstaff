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
from uuid import uuid4

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


async def unfold(card):
    """A row (Monitoring, Recently finished) shows its whole card: a click on it."""
    classes = (await card.get_attribute("class")).split()
    if "row" in classes and "unfolded" not in classes:
        await card.locator(".row-line").click()
        await expect(card).to_have_class(re.compile(r"\bunfolded\b"))


def take_up(runtime, session_id: str, title: str):
    """A worker at work sees a task sent meanwhile (after its turn) and takes it up."""
    task = next(t for t in runtime.tasks.for_session(session_id) if t["title"] == title)
    runtime.states.handle(session_id).emit(
        "user.message",
        content=f'<message from="router" type="task" task="{task["id"]}">\n{title}\n</message>',
    )
    return task


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
    pr = {"label": "acme/ranking-model#71",
          "url": "https://github.com/acme/ranking-model/pull/71",
          "state": "OPEN", "checks": "passing", "failing": [], "review": "APPROVED",
          "checks_started": "2026-11-05T10:00:00Z", "at": "2026-11-05T10:05:00+00:00",
          "own": True}  # fmt: skip
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
    # Its tests are a chip when they ran on its own work (CONTRACT receipts.tests.own), else
    # evidence in its report, saying where they ran.
    tests = runtime.task_actions.view(runtime.tasks.get(task["id"]))["receipts"]["tests"]
    if tests.get("own"):
        await expect(card.locator(".receipt.warn", has_text="✗ tests locally")).to_be_visible()
    else:
        await expect(card.locator(".chips .receipt", has_text="tests")).to_have_count(0)
        await expect(card.locator(".report .tests-elsewhere")).to_contain_text("Ran tests")
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
    if tests.get("own"):
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
    take_up(runtime, first, "Fix review comments on lms")  # Seen once its turn is over.
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
    # Monitoring: a row per task, its worker in the tooltip.
    queued_box = board.locator(f'#tasks-monitoring .card.task[data-session="{first}"]')
    await expect(running_box.locator(".card.task")).to_have_count(1, timeout=15000)
    await expect(running_box.locator(".card.task .head .title")).to_have_text(
        "Fix review comments on lms"
    )
    await expect(running_box.locator(".box-head .meta")).to_contain_text("also: Monitoring")
    await expect(queued_box.locator(".head .title")).to_have_text("Editor event paths")
    await expect(queued_box.locator(".row-line .why")).to_contain_text(
        "Paused: its worker is on “Fix review comments on lms” now"
    )
    # Said on its second line, not again beside the title (U-s3).
    await expect(queued_box.locator(".head .meta")).not_to_contain_text("Paused")
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
    await expect(queued_box.locator(".head .title")).to_have_text("Fix review comments on lms")
    # Its worker between turns, nothing registered to wait on: not running, and it says so.
    idle = board.locator("#tasks-monitoring .card.task", has_text="Buyer Group Graph 7-pager")
    await expect(idle.locator(".row-line .why.warn")).to_contain_text(
        "nothing registered to wait on"
    )
    # Overdue and quiet ones first.
    await expect(board.locator("#tasks-monitoring > .items > .card.task").first).to_have_class(
        re.compile(r"marker-(idle|overdue)")
    )
    await unfold(idle)
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
    take_up(runtime, busy, "CoS bug bash")
    long = ("**Status:** fixed `B16`; see [the B16 notes](https://docs.google.com/document/d/"
            "abc123) and https://github.com/acme/app/pull/592 — "
            + "more words here " * 120)  # fmt: skip
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
    waits = page.locator(f'#tasks-monitoring .card.task[data-session="{busy}"]')
    await expect(group.locator(".card.task")).to_have_count(1)
    await expect(group.locator(".card.task .head .title")).to_have_text("CoS bug bash")
    await expect(waits.locator(".head .title")).to_have_text("Research the Slack bot")
    for box, row in ((group, group.locator(".card.task").first), (waits, waits)):
        card_box = await box.bounding_box()
        row_box = await row.bounding_box()
        assert row_box["width"] <= card_box["width"], (row_box, card_box)
        meta_box = await row.locator(".head .meta").bounding_box()
        assert meta_box["x"] + meta_box["width"] <= card_box["x"] + card_box["width"] + 1
        if box == group:
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
    # A doc and a PR it only mentions (it did not say it made them) are references, folded
    # under "More links"; the doc named as the report linked it, its id on hover.
    await expect(chips_now.locator(".more-links summary")).to_have_text("More links: 2 referenced")
    await expect(chips_now.locator(".more-links .receipt", has_text="app#592")).to_have_count(1)
    doc_chip = chips_now.locator(".more-links .receipt.link-chip", has_text="the B16 notes")
    await expect(doc_chip.locator(".chip-link")).to_have_attribute(
        "title", re.compile("Google Doc abc123")
    )
    await expect(chips_now.locator(".receipt", has_text="Google Doc")).to_have_count(0)
    await expect(group.locator(".activity")).to_contain_text("Measure the row overflow")
    log("a worker's box per section: its task there, two clean lines, when apart, its links")

    asking_card = page.locator("#tasks .card.task", has_text="Answer Carol")
    if await asking_card.count():
        await expect(asking_card.locator(".receipt.needs")).to_be_visible()
    watching = page.locator("#tasks-monitoring .card.task", has_text="Watch CI on #12")
    await expect(watching).to_be_visible()
    await expect(watching.locator(".row-line .why")).to_have_text("Waiting on app#12")
    await unfold(watching)
    await expect(watching.locator(".receipt.watching")).to_have_text("Watching app#12")
    counts = await page.evaluate("""() => {
      const n = (id) => Number(document.querySelector(`#${id} .count`)?.textContent || 0);
      return [n("tasks"), n("tasks-running") + n("tasks-monitoring")];
    }""")
    assert counts[0] == counts[1], counts
    await expect(watching.locator(".task-body .reason")).to_contain_text("Waiting on app#12")
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
    # A row's title shows its card first; then it opens its worker.
    counted = recent.locator(".card.task", has_text="Count the orders")
    await counted.locator(".head .title").click()
    await expect(counted).to_have_class(re.compile(r"\bunfolded\b"))
    await counted.locator(".head .title").click()
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
        take_up(runtime, worker, title)
        await settle(runtime)
        return next(t for t in runtime.tasks.for_session(worker) if t["title"] == title)

    # C1/C2: to review, a PR with a long title, its changes, a Flyte run and what is under it.
    fixer = await start("Fix the Spark 3.5 dependency of the ICP flow")
    fix = runtime.tasks.current(fixer)
    label = "acme/ranking-data#594"
    store.set(f"prtitle:{label}", "Upgrade the ICP flow to Spark 3.5 and pin the shaded " * 3)
    pr = {"label": label, "url": "https://github.com/acme/ranking-data/pull/594",
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
    main = review.locator(".chips > .receipts > .receipt.link-chip > .chip-link")
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
    await expect(delivered.locator(".row-line .why")).to_contain_text(
        "Delivered; still following app#77"
    )
    await expect(delivered.locator(".head .meta")).not_to_contain_text("Delivered")
    # The owner's deliveries waiting on others: a group of their own, open.
    await expect(
        monitoring.locator("#tasks-monitoring-delivered[open] .card.task", has_text="Ship the")
    ).to_have_count(1)
    later = monitoring.locator(".card.task", has_text="Run the weekly embeddings")
    await expect(later.locator(".row-line .why")).to_contain_text("the Flyte run finishing")
    await expect(later.locator(".row-line .why")).to_contain_text("Checks back at")
    await expect(later.locator(".head .meta")).not_to_contain_text("Checks back")
    await expect(running.locator(".card.task", has_text="Answer Jingtao")).to_have_count(1)
    weekly_box = running.locator(f'.card.worker-box[data-session="{weekly}"]')
    await expect(weekly_box.locator(".box-head .meta")).to_contain_text("also: Monitoring")
    log("monitoring says what it waits on; a check-back is its task's while its worker works")

    cannot = page.locator(".card.task", has_text="Watch the example PR")
    await unfold(cannot)
    chip = cannot.locator(".receipt.failing")
    await expect(chip).to_contain_text("Can't read app#42")
    await expect(cannot.locator(".receipt.watching")).to_have_count(0)
    await chip.get_by_role("button", name="stop following").click()
    await expect(page.locator(".toast", has_text="Stopped following app#42.")).to_be_visible()
    assert "github:acme/app#42" not in store.follows(stuck)
    await expect(chip).to_have_count(0, timeout=10000)
    log("a thread it cannot read is said so, not waited on, and can be let go")

    spec_card = page.locator(".card.task", has_text="Draft the AI interviewer spec")
    await unfold(spec_card)
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
    pr_row = rows.filter(has_text="ranking-data#594")
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
    for listed in (made, monitoring, page.locator("#tasks-recent")):
        box = await listed.locator(":scope > .items").evaluate(
            "n => [getComputedStyle(n).maxHeight, getComputedStyle(n).overflowY]"
        )
        assert box == ["300px", "auto"], box  # min(40vh, 300px) at 900px high.
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
    # Kept with how it stood: should it go wrong later, it comes back on its own.
    assert json.loads(store.get("artifacts_hidden")) == [
        {"key": weekly_run.removeprefix("https://"), "tone": "active"}
    ], store.get("artifacts_hidden")
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
    # A row per task (no worker boxes: the worker is in the tooltip), each with a background
    # and a border, a few to the list's height; Show more at the end of the list.
    await expect(recent.locator(".card.worker-box")).to_have_count(0)
    looks = await cards.first.evaluate(
        "n => [getComputedStyle(n).borderTopWidth, getComputedStyle(n).backgroundColor,"
        " n.getBoundingClientRect().height]"
    )
    assert looks[0] == "1px" and looks[1] not in {"rgba(0, 0, 0, 0)", "transparent"}, looks
    assert looks[2] <= 60, ("a compact row", looks)
    await expect(recent.locator(":scope > .items > .show-more")).to_have_count(1)
    await more.click()
    await expect(cards).to_have_count(min(60, total))
    await in_order()
    log(f"recently finished: newest first across workers, 30 of {total}, then more")

    assert not errors, errors
    await context.close()


async def round_five(runtime, browser, failures: list[str]):
    """The bug bash's fixes in the browser: a stopped task waits for the owner to say go on; a
    worker's answer to the owner's @ on Home is on Home; View on a notice opens that task, not
    its worker's first; two Google docs are told apart; a table's long cell stays in its box;
    Recently finished is open; Artifacts' kinds line up."""
    store = runtime.store
    router = store.get("router_id")

    async def start(title):
        result = await call_tool(runtime, "spawn_worker", router, title=title,
                                 instructions=title, why_new="test")  # fmt: skip
        assert result.result_type == "success", result.text_result_for_llm
        await settle(runtime)
        return json.loads(result.text_result_for_llm)["session_id"]

    stopper = await start("Measure the cache hit rate")
    stopped_task = runtime.tasks.current(stopper)
    runtime.update_session(stopper, status="working", activity="Reading the logs")
    await runtime.workers.interrupt(stopper)
    await settle(runtime)
    assert runtime.tasks.get(stopped_task["id"])["status"] == "waiting"

    asked = await start("Count the tests")
    await call_tool(runtime, "report", asked, status="done", summary="There are 12 tests.")
    runtime.task_actions.accept(runtime.tasks.current(asked)["id"])
    await runtime.owner_send_to(router, asked, "and how many of them are slow?")
    answering = runtime.states.handle(asked)
    answering.emit("assistant.message", content="3 of them are slow: the e2e ones.")
    answering.emit("session.idle")

    planner = await start("Write the first plan")
    await call_tool(runtime, "message_session", router, session_id=planner, task="new",
                    title="Write the second plan", text="Write the second plan",
                    earlier="keep")  # fmt: skip
    second = take_up(runtime, planner, "Write the second plan")
    plans = ("Plans: [Plan A](https://docs.google.com/document/d/PLANAAAA1111/edit) and "
             "https://docs.google.com/document/d/PLANBBBB2222/edit")  # fmt: skip
    await call_tool(runtime, "report", planner, status="progress", summary=plans,
                    task_id=second["id"])  # fmt: skip
    # Plan A it says it made (no name given); plan B it only mentions.
    runtime.task_actions.declare_artifacts(
        second["id"],
        [{"url": "https://docs.google.com/document/d/PLANAAAA1111/edit", "status": "draft"}],
    )
    long_cell = "averyveryverylongidentifierwithoutanyspaces" * 4
    runtime.add_message(
        router, "assistant", f"| step | detail |\n|---|---|\n| 1 | {long_cell} |\n| 2 | ok |"
    )
    await settle(runtime)
    runtime.publish_state()

    context = await browser.new_context(viewport={"width": 1400, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")

    card = page.locator("#needs .card.task", has_text="Measure the cache hit rate")
    await expect(card.locator(".head .meta")).to_contain_text("Stopped", timeout=15000)
    await expect(card).to_contain_text("Stopped by you")
    await card.get_by_role("button", name="Go on", exact=True).click()
    await expect(page.locator(".toast", has_text="Sent to the worker.")).to_be_visible()
    await expect(card).to_have_count(0, timeout=10000)
    assert runtime.tasks.get(stopped_task["id"])["status"] == "running"
    log("a stopped task waits under Needs you, says who stopped it; Go on sends it back")

    timeline = page.locator("#timeline")
    answer = timeline.locator(".event", has_text="3 of them are slow")
    await expect(answer).to_contain_text("Count the tests: 3 of them are slow", timeout=10000)
    log("a worker's answer to the owner's @ from Home is on Home")

    told = timeline.locator(".event", has_text="Write the second plan").first
    await told.get_by_role("button", name="View", exact=True).click()
    flashed = page.locator(f'#panel .card.task.flash[data-id="{second["id"]}"]')
    await expect(flashed).to_have_count(1, timeout=5000)
    await expect(page.locator("#panel .card.task.flash")).to_have_count(1)
    # Named as the report linked them, else what they are; their ids on hover, not as names.
    plan_a_chip = flashed.locator(".artifacts > .receipts .receipt.link-chip", has_text="Plan A")
    await expect(plan_a_chip.locator(".chip-link")).to_have_text("Plan A · draft")
    await expect(plan_a_chip.locator(".chip-link")).to_have_attribute(
        "title", re.compile("Google Doc PLANAAAA1111")
    )
    plan_b = flashed.locator(".more-links .receipt.link-chip .chip-link")
    await expect(plan_b).to_have_text("Google Doc")
    await expect(plan_b).to_have_attribute("title", re.compile("PLANBBBB2222"))
    log("View on a notice opens that task; docs by their names, their ids on hover")

    table = timeline.locator(".markdown .table", has_text="averyvery")
    await expect(table).to_be_visible()
    bubble = await table.evaluate(
        "n => [n.getBoundingClientRect().right, n.closest('.message, .bubble, .event, #timeline')"
        ".getBoundingClientRect().right]"
    )
    assert bubble[0] <= bubble[1] + 1, bubble
    ok_cell = table.locator("td", has_text="ok")
    assert (await ok_cell.evaluate("n => n.getClientRects().length")) == 1
    await table.screenshot(path=SHOTS / "ui-edges-table.png")
    log("a table's long cell stays in its box; short cells are whole")

    recent = page.locator("#tasks-recent")
    await expect(recent).to_have_attribute("open", "")
    kinds = await page.locator("#tasks-artifacts .artifact .a-kind").evaluate_all(
        "ns => [...new Set(ns.map((n) => Math.round(n.getBoundingClientRect().width)))]"
    )
    assert len(kinds) <= 1, kinds
    log("Recently finished is open; Artifacts' kind column lines up")

    # Taken off by 0.1.25, under its address: still off; brought back, gone from the list.
    plan_a = "https://docs.google.com/document/d/PLANAAAA1111/edit"
    store.set("artifacts_hidden", json.dumps([plan_a]))
    runtime.publish_state()
    made = page.locator("#tasks-artifacts")
    await expect(made.locator(".artifact", has_text="Plan A")).to_have_count(0, timeout=10000)
    await made.get_by_role("button", name="Show 1 taken off").click()
    await (
        made.locator(".artifact", has_text="Plan A")
        .get_by_role("button", name="Bring back")
        .click()
    )
    await expect(made.locator(".artifact.hidden-artifact")).to_have_count(0)
    for _ in range(50):
        if json.loads(store.get("artifacts_hidden") or "[]") == []:
            break
        await asyncio.sleep(0.1)
    assert json.loads(store.get("artifacts_hidden")) == [], store.get("artifacts_hidden")
    log("one taken off by an earlier version stays off, and comes back")
    await context.close()


async def patch_state(page, patches: dict[str, dict], extra: dict | None = None) -> dict:
    """What the page reads from /api/state, with these fields on these tasks (by title): what
    the server says of them once it has them (the CONTRACT fields), so the page is checked on
    its own. Returns the tasks as last sent, by id."""
    sent: dict = {}

    async def handle(route):
        response = await route.fetch()
        data = await response.json()
        for task in data["tasks"]:
            patch = patches.get(task["title"], {})
            receipts = patch.get("receipts")
            task.update({key: value for key, value in patch.items() if key != "receipts"})
            if receipts:
                task["receipts"] = {**(task.get("receipts") or {}), **receipts}
            sent[task["id"]] = task
        for key, value in (extra or {}).items():
            merged = isinstance(value, dict) and isinstance(data.get(key), dict)
            data[key] = {**data[key], **value} if merged else value
        await route.fulfill(response=response, json=data)

    await page.route("**/api/state", handle)
    return sent


async def round_six(runtime, browser, failures: list[str]):
    """The v0.1.26 bug bash (E1-E10) and what other hunters found, in the browser: docs by their
    names; a row per task in Monitoring and Recently finished, in lists that scroll; every part
    of Home folds and is remembered; deliveries and reviewed PRs apart, a quiet one with the
    finished; PR chips only for PRs a task made or reviewed, in one state word; what it made
    (declared, said made, its files) apart from what it mentions, ✕ on a chip; tests run
    elsewhere as evidence; a report's Done, unread dot and files; moving a result to a new
    task with a preview and an undo; one request per click; empty replies refused; lessons
    read whole; Slack names; watches not searched; a short tooltip; a list that holds still
    under the mouse."""
    store = runtime.store
    router = store.get("router_id")
    store.save_settings(store.settings().model_copy(update={"max_workers": 60}))

    async def start(title):
        result = await call_tool(runtime, "spawn_worker", router, title=title,
                                 instructions=title, why_new="test")  # fmt: skip
        assert result.result_type == "success", result.text_result_for_llm
        await settle(runtime)
        return json.loads(result.text_result_for_llm)["session_id"]

    def finish(worker, status, result, evidence=None):
        task = runtime.tasks.current(worker)
        if evidence:
            run = store.one("SELECT id FROM runs WHERE task_id=?", (task["id"],))
            store.execute("UPDATE runs SET evidence=? WHERE id=?",
                          (json.dumps(evidence), run["id"]))  # fmt: skip
        runtime.tasks.move(task["id"], status, result=result)
        runtime.update_session(worker, status="idle", activity="")
        return task

    doc = "https://docs.google.com/document/d/1SPkOjC0xyz123/edit"
    reporter = await start("Eval SLM fine-tune report")
    report = finish(reporter, "review", (
        f"Results in [qwen3_8b SFT 11-05]({doc}). As discussed in https://docs.google.com/ "
        "and `https://docs.google.com/document/d/1InCodeOnly9/edit`."))  # fmt: skip
    shipper = await start("Ship app#594 export fix")
    finish(shipper, "done", "Opened acme/app#594.")
    reviewer = await start("Review checkout-service PR #1537")
    finish(reviewer, "done", "Reviewed #1537.")
    twin = runtime.tasks.create(reviewer, "Correct the #1537 review draft", "x", status="running")
    runtime.tasks.move(twin["id"], "done", result="Corrected.")
    flaky = await start("Investigate faro#1536 flake")
    finish(flaky, "done", "Reviewed faro#1536.")
    scheduler = await start("Schedule the weekly jobs")
    scheduled = finish(scheduler, "review", "Scheduled. Also: Qwen should use the 8B model.")
    mocker = await start("Mockup for the new Home")
    mockup = finish(mocker, "review", "The mockup is in mock5.html.")
    pr = lambda n, **extra: {"label": f"acme/app#{n}", "url": f"https://github.com/acme/app/pull/{n}",  # noqa: E731
                             "state": "OPEN", "checks": "passing", "failing": [], "review": "",
                             "at": datetime.now(UTC).isoformat(), **extra}  # fmt: skip
    checker = await start("Check the PR states")
    finish(checker, "review", "Looked at #9, #10 and #11.", {"checkouts": [], "prs": [
        pr(9, reviewed=True, own=False, review="CHANGES_REQUESTED", merge_state="CLEAN"),
        pr(10, own=False), pr(11, own=True, merge_state="CLEAN")]})  # fmt: skip
    asker = await start("Pick the cache size")
    await call_tool(runtime, "report", asker, status="question", summary="64 or 128 MB?")
    runtime.update_session(asker, status="waiting", activity="Waiting for you")
    latest = ("Channel: <#D0BOB0001> | From: <@U0BOB001> | ts: 1712345678.000100\n"
              "Text: lunch, <@U0OWNER1>?")  # fmt: skip
    slack = store.create_draft(
        asker,
        "slack",
        {"channel_id": "D0BOB0001", "to": "<@U0BOB001>", "latest": latest},
        "Sure, Friday.",
    )
    line = store.create_draft(asker, "github", {
        "repo": "acme/app", "number": 9, "url": "https://github.com/acme/app/pull/9",
        "via": "task", "in_reply_to": 31, "comment": {"author": "maya", "body": "Why 64?",
        "path": "cache.py", "line": 3, "url": "u"}}, "Measured: 64 is enough.")  # fmt: skip
    merge = store.create_draft(asker, "merge", {
        "label": "acme/app#11", "repo": "acme/app", "number": 11, "title": "Cache size",
        "url": "https://github.com/acme/app/pull/11", "head": "abc1234", "review": "",
        "checks": "none", "task_id": runtime.tasks.current(checker)["id"],
    }, "Cache size")  # fmt: skip
    long_lesson = "Before a full run, run a small smoke job on one partition and check it " * 3
    store.execute(
        "INSERT INTO lessons(id, text, scope, evidence, status, created_at, updated_at) "
        "VALUES ('llong', ?, '', '[]', 'proposed', 'now', 'now')",
        (long_lesson.strip(),),
    )
    lessons = store.create_draft(router, "lessons", {"lessons": [
        runtime.learning.card_item("llong")]}, "1 lesson")  # fmt: skip
    # Someone wrote there since the draft: not sent; the new message shown as new; or the
    # conversation could not be read.
    moved = store.create_draft(asker, "slack", {
        "channel_id": "C0DEPLOYS1", "latest": "Channel: <#C0DEPLOYS1> | From: <@U0BOB001> | "
        "ts: 1712345699.000300\nText: never mind, fixed it", "latest_ts": "1712345699.000300",
        "moved_on": {"who": "<@U0BOB001>", "text": "never mind, fixed it",
                     "ts": "1712345699.000300", "at": datetime.now(UTC).isoformat()},
    }, "I'll look now.")  # fmt: skip
    store.update_draft(moved["id"], outcome="Not sent: Bob wrote since: “never mind, fixed it”")
    unread = store.create_draft(asker, "slack", {
        "channel_id": "C0DEPLOYS2", "latest_error": "channel_not_found"}, "On it.")  # fmt: skip
    gone = store.create_draft(asker, "slack", {"channel_id": "C0DEPLOYS3"}, "Thanks!")
    runtime.add_message(
        router, "draft", "Thanks!", {"draft_id": gone["id"], "label": "Slack reply"}
    )
    for draft in (slack, line, merge, lessons, moved, unread, gone):
        runtime.bus.publish("draft", store.draft(draft["id"]))
    runtime.add_message(router, "inbound", "slack · Bob: is the cache fix in?", {"source": "watch"})
    runtime.add_message(router, "inbound", "Channel: <#D0BOB0001> | From: <@U0BOB001> | ts: "
                        "1712345690.000200\nText: ping", {"source": "slack"})  # fmt: skip
    store.set("watch_last", json.dumps({
        "at": datetime.now(UTC).isoformat(), "found": 0, "problems": "Glean is not connected",
        "incomplete": [{"watch": 1, "reason": "Glean is not connected"},
                       {"watch": 2, "reason": "Only the newest 3 DMs"}]}))  # fmt: skip
    await settle(runtime)
    runtime.publish_state()

    two_days = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    scratch = "/tmp/cos-scratch/eval"
    patches = {
        "Eval SLM fine-tune report": {
            "made_links": [doc], "read": False,
            "files": [{"path": f"{scratch}/mock5.html", "name": "mock5.html", "size": 5120}],
            "receipts": {"tests": {"own": False, "cwd": "/tmp/cos-scratch/source-v0.1.26",
                                   "passed": True, "exit_code": 0, "summary": "592 passed",
                                   "command": "uv run pytest -q", "runs": 2,
                                   "failed_before": False, "running": False}}},
        "Ship app#594 export fix": {"watching": ["github:acme/app#594"], "watch_kind": "own"},
        "Review checkout-service PR #1537": {"watching": ["github:acme/checkout#1537"],
                                           "watch_kind": "reviewed"},
        "Correct the #1537 review draft": {"watching": ["github:acme/checkout#1537"],
                                           "watch_kind": "reviewed"},
        "Investigate faro#1536 flake": {"watching": ["github:acme/faro#1536"],
                                        "watch_kind": "reviewed", "watch_changed_at": two_days},
        "Schedule the weekly jobs": {"splittable": True},
        "Mockup for the new Home": {"review_required": True},
    }  # fmt: skip
    names = {
        "slack_names": {"U0BOB001": "Bob", "D0BOB0001": "Bob", "U0OWNER1": "Evan"},
        # Not still at the scan an earlier round started (the fake connector never ends it).
        "connector": {"after_hours_reason": "weekend", "scanning": False},
    }

    context = await browser.new_context(viewport={"width": 1440, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    sent = await patch_state(page, patches, names)
    # Read here, not on the server: its answer would redraw the task without what was put in.
    reads: list[str] = []

    async def read(route):
        reads.append(route.request.url)
        await route.fulfill(json={"read": True})

    await page.route("**/api/tasks/*/read", read)
    await page.goto(f"{BASE}/#token={TOKEN}")
    needs, monitoring = page.locator("#needs"), page.locator("#tasks-monitoring")
    recent, made = page.locator("#tasks-recent"), page.locator("#tasks-artifacts")

    def card(title):
        return page.locator("#panel .card.task").filter(
            has=page.locator(".head .title", has_text=title)
        )

    # E1/E9/E10: a report: Report with Done, unread; the doc named as linked, its id on hover;
    # docs.google.com itself and a link in code are not docs; its file; tests run elsewhere.
    reported = card("Eval SLM fine-tune report")
    await expect(reported).to_have_class(re.compile(r"\brow\b"), timeout=15000)
    assert await recent.locator(".card.task", has_text="Eval SLM").count() == 1
    await expect(reported.locator(".head .meta")).to_contain_text("Report")
    await expect(reported.locator(".head .dot.unread")).to_have_count(1)
    await expect(reported.locator(".row-line .made")).to_have_text("qwen3_8b SFT 11-05")
    async with page.expect_request(f"**/api/tasks/{report['id']}/read"):
        await reported.locator(".row-line").click()
    await expect(reported).to_have_class(re.compile(r"\bunfolded\b"))
    await expect(reported.locator(".head .dot.unread")).to_have_count(0)
    chips = reported.locator(".chips > .receipts")
    await expect(chips.locator(".link-chip .chip-link")).to_have_text(
        ["qwen3_8b SFT 11-05", "file · mock5.html"]
    )
    await expect(chips.locator(".link-chip .chip-link").first).to_have_attribute(
        "title", re.compile("Google Doc 1SPkOjC0xyz123")
    )
    await expect(chips.locator(".receipt", has_text="tests")).to_have_count(0)
    await expect(reported.locator(".more-links summary")).to_have_text("More links: 1 referenced")
    await expect(reported.locator(".more-links .chip-link")).to_have_text(["docs.google.com"])
    done = reported.get_by_role("button", name="Done")
    await expect(done).to_be_visible()
    await expect(done).to_have_attribute(
        "title", "Only a report: nothing to accept. Done once read."
    )
    await reported.get_by_role("button", name="Show report").click()
    await expect(reported.locator(".report .tests-elsewhere")).to_have_text(
        "Ran tests in source-v0.1.26 · 592 passed"
    )
    await page.evaluate("document.querySelector('#panel').style.width = '352px'")
    await reported.screenshot(path=SHOTS / "ui-edges-report-352.png")
    found = await reported.evaluate(OVERFLOWS)
    assert not found, found
    await page.evaluate("document.querySelector('#panel').style.removeProperty('width')")
    await page.route("**/api/file?*", lambda route: route.fulfill(json={
        "name": "mock5.html", "path": f"{scratch}/mock5.html", "text": "<h1>Home</h1>",
        "truncated": False}))  # fmt: skip
    async with page.expect_request("**/api/file?*") as opened:
        await chips.get_by_role("button", name="file · mock5.html").click()
    assert "session=" in (await opened.value).url and "mock5.html" in (await opened.value).url
    await expect(page.locator("#file-dialog")).to_be_visible()
    await page.locator("#file-close").click()
    rows = made.locator(".artifact")
    doc_row = rows.filter(has_text="qwen3_8b SFT 11-05")
    await expect(doc_row.locator(".a-kind")).to_have_text("Doc")
    await expect(doc_row.locator(".a-status")).to_have_text("reported")
    await expect(rows.filter(has_text="Eval SLM fine-tune report")).to_have_count(1)
    await chips.get_by_role("button", name="Take qwen3_8b SFT 11-05 off").click()
    await expect(doc_row).to_have_count(0)
    await expect(reported.locator(".more-links summary")).to_have_text(
        "More links: 1 referenced · 1 taken off"
    )
    await made.get_by_role("button", name="Show 1 taken off").click()
    await doc_row.get_by_role("button", name="Bring back").click()
    await expect(chips.locator(".link-chip .chip-link").first).to_have_text("qwen3_8b SFT 11-05")
    log(
        "a report: Done in sight, unread until opened, the doc by its name (id on hover), its "
        "file, docs.google.com and code not docs, tests run elsewhere as evidence, ✕ and back"
    )

    # E6: deliveries waiting on others and PRs reviewed apart (the latter folded); one row for
    # one PR a worker follows; quiet for a day, with the finished ones, still followed.
    delivered = monitoring.locator("#tasks-monitoring-delivered")
    reviewed = monitoring.locator("#tasks-monitoring-reviewed")
    await expect(delivered).to_have_attribute("open", "")
    await expect(delivered.locator(".card.task", has_text="Ship app#594")).to_have_count(1)
    await expect(reviewed).not_to_have_attribute("open", "")
    await expect(reviewed.locator(":scope > summary .count")).to_have_text("1")
    await expect(reviewed.locator(".card.task")).to_have_count(1)
    await expect(recent.locator(".card.task.marker-still-following")).to_have_count(2)
    stale = recent.locator(".card.task", has_text="Investigate faro#1536")
    await expect(stale.locator(".head .meta")).not_to_contain_text("Still following")
    await expect(stale.locator(".row-line .why")).to_contain_text("nothing new there for 2d")
    await reviewed.locator(":scope > summary").click()
    await expect(reviewed).to_have_attribute("open", "")
    await page.wait_for_function(
        "() => localStorage.getItem('cos.section.tasks-monitoring-reviewed') === '1'"
    )
    log("monitoring: deliveries and reviewed PRs apart, one row a PR, a quiet one finished")

    # E7 and #1: PR chips for PRs it made or reviewed, one state word; the rest referenced.
    prs = card("Check the PR states")
    await expect(prs.locator(".chips > .receipts > .receipt.pr")).to_have_text(
        ["app#9 · ✓ CI · changes requested", "app#11 · ✓ CI · ready to merge"]
    )
    await expect(prs.locator(".more-links summary")).to_have_text("More links: 1 referenced")
    await expect(prs.locator(".more-links .receipt.pr")).to_contain_text("app#10")
    await expect(rows.filter(has_text="app#11").locator(".a-status")).to_have_text("ready to merge")
    log("PR chips: made or reviewed in sight, one word for where each stands; mentioned folded")

    # E10: the Router wants a look: to review, Accept; #5: a second click sends nothing.
    mock = needs.locator(".card.task", has_text="Mockup for the new Home")
    await expect(mock.locator(".head .meta")).to_contain_text("To review")
    accepted: list[str] = []

    async def accept(route):
        accepted.append(route.request.url)
        await asyncio.sleep(0.6)
        await route.fulfill(json={**sent[mockup["id"]], "status": "done"})

    await page.route(f"**/api/tasks/{mockup['id']}/accept", accept)
    button = mock.get_by_role("button", name="Accept")
    await button.evaluate("b => { b.click(); b.click(); }")
    await page.wait_for_timeout(1000)
    assert len(accepted) == 1, accepted
    log("a result the Router wants looked at is to review; two clicks accept once")

    # #3: a worker in a turn waiting on the owner waits for them; it is not idle.
    box = needs.locator(".card.worker-box", has_text="Pick the cache size")
    await expect(box.locator(".box-head .meta")).to_have_text("waits for you")

    # E3: move the latest result to a new task: what stays, what moves, a bad title refused,
    # then Undo split on the new task.
    split = card("Schedule the weekly jobs")
    await split.locator(".row-line").click()
    preview = {"allowed": True, "why": "", "keeps": {"result": "Scheduled the jobs."},
               "moves": {"result": "Qwen should use the 8B model.", "messages": [
                   {"id": 5978, "kind": "assistant", "text": "Also checked the 8B eval."},
               ]}}  # fmt: skip
    await page.route(f"**/api/tasks/{scheduled['id']}/split-preview",
                     lambda route: route.fulfill(json=preview))  # fmt: skip
    posted: list[dict] = []
    until = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    split_off = {
        **sent[scheduled["id"]],
        "id": "tmoved01",
        "title": "Answer Qwen: use the 8B model",
        "split_from": {"task_id": scheduled["id"], "until": until},
        "splittable": False,
    }

    async def splitting(route):
        posted.append(route.request.post_data_json)
        if len(posted) == 1:
            refused = "Say what it delivers: at least 8 characters."
            await route.fulfill(status=400, json={"detail": refused})
        else:
            await route.fulfill(json=split_off)

    await page.route(f"**/api/tasks/{scheduled['id']}/split", splitting)
    await split.locator(".more-button").click()
    await split.get_by_role("menuitem", name="Move the latest result to a new task…").click()
    form = split.locator(".split-preview")
    await expect(form.locator(".keeps")).to_have_text("Stays here: Scheduled the jobs.")
    await expect(form.locator(".moves")).to_have_text("Moves: Qwen should use the 8B model.")
    await expect(form.locator(".moved summary")).to_have_text("and 1 message after it")
    await split.screenshot(path=SHOTS / "ui-edges-split.png")
    title = split.get_by_label("Title of the new task")
    await title.fill("111")
    await split.get_by_role("button", name="Move it").click()
    await expect(split.locator(".actions .error")).to_have_text(
        "Say what it delivers: at least 8 characters."
    )
    await title.fill("Answer Qwen: use the 8B model")
    await split.get_by_role("button", name="Move it").click()
    assert [body["text"] for body in posted] == ["111", "Answer Qwen: use the 8B model"], posted
    new_card = card("Answer Qwen: use the 8B model")
    await unfold(new_card)
    undo = new_card.get_by_role("button", name="Undo split")
    await expect(undo).to_have_attribute(
        "title", re.compile("Back into “Schedule the weekly jobs”")
    )
    await page.route("**/api/tasks/tmoved01/merge-back",
                     lambda route: route.fulfill(json=sent[scheduled["id"]]))  # fmt: skip
    async with page.expect_request("**/api/tasks/tmoved01/merge-back"):
        await undo.click()
    log(
        "moving a result: a preview of what stays and moves, the server's word on a bad "
        "title, then Undo split on the new task"
    )
    await page.unroute("**/api/state")
    sent = await patch_state(page, patches, names)

    # Drafts: an empty reply is not sent; Slack names, not ids; no checks said so.
    slack_card = needs.locator(f'.card.draft[data-id="{slack["id"]}"]')
    await expect(slack_card.locator(".quoted")).to_contain_text("From: @Bob")
    await expect(slack_card.locator(".quoted")).not_to_contain_text("ts:")
    await expect(slack_card.locator(".quoted")).not_to_contain_text("U0OWNER1")
    await expect(slack_card.locator(".note")).to_contain_text("For @Bob")
    for draft in (slack, line):
        node = needs.locator(f'.card.draft[data-id="{draft["id"]}"]')
        await node.locator("textarea").fill("  ")
        await node.get_by_role("button", name="Send", exact=True).click()
        await expect(node.locator(".error")).to_have_text("The reply is empty.")
        assert store.draft(draft["id"])["status"] == "pending"
    merge_card = needs.locator(f'.card.draft[data-id="{merge["id"]}"]')
    await expect(merge_card).to_contain_text("No checks")
    moved_card = needs.locator(f'.card.draft[data-id="{moved["id"]}"]')
    await expect(moved_card.locator(".fresh-label")).to_contain_text("New since the draft: @Bob")
    await expect(moved_card.locator(".fresh-label")).to_contain_text("not sent yet")
    await expect(moved_card.locator(".quoted.fresh")).to_contain_text("never mind, fixed it")
    # What they wrote once (the quote), not again in a note; one action.
    await expect(moved_card.locator(".note", has_text="never mind")).to_have_count(0)
    await expect(moved_card.get_by_role("button", name="Send", exact=True)).to_have_count(0)
    await moved_card.screenshot(path=SHOTS / "ui-edges-slack-moved-on.png")
    posted_anyway: list[dict] = []

    async def anyway(route):
        posted_anyway.append(route.request.post_data_json)
        await route.fulfill(json={**store.draft(moved["id"]), "status": "sent"})

    await page.route(f"**/api/drafts/{moved['id']}", anyway)
    await moved_card.get_by_role("button", name="Send anyway").click()
    await expect(moved_card).to_have_count(0, timeout=5000)
    # Checked again on the way: held again only if someone wrote after the message shown.
    assert posted_anyway == [{"action": "send", "text": "I'll look now."}], posted_anyway
    unread_card = needs.locator(f'.card.draft[data-id="{unread["id"]}"]')
    await expect(unread_card).to_contain_text("Could not read the conversation: channel_not_found")
    # Withdrawn by its worker: off Needs you, and its line in the chat says so.
    store.update_draft(gone["id"], status="rejected", outcome="Withdrawn: the thread was solved")
    runtime.bus.publish("draft", store.draft(gone["id"]))
    await expect(needs.locator(f'.card.draft[data-id="{gone["id"]}"]')).to_have_count(0)
    record = page.locator(f'#timeline .draft-record[data-draft-id="{gone["id"]}"]')
    await expect(record.locator(".outcome")).to_have_text("Withdrawn: the thread was solved")
    log("drafts: an emptied reply is refused, not the draft sent; Slack names; no checks")

    # Lessons: a long one is read whole, at the narrowest panel too.
    await page.evaluate("document.querySelector('#panel').style.width = '352px'")
    lesson = needs.locator(".card.lessons textarea").first
    await expect(lesson).to_have_value(long_lesson.strip())
    await page.wait_for_timeout(200)
    sizes = await lesson.evaluate("n => [n.scrollHeight, n.clientHeight]")
    assert sizes[0] <= sizes[1] + 2 and sizes[1] > 40, sizes
    for part in (needs, page.locator("#tasks")):
        found = await part.evaluate(OVERFLOWS)
        assert not found, found
    await page.evaluate("document.querySelector('#panel').style.removeProperty('width')")
    log("a long lesson is read whole in a box that grows")

    # The chat: a watch's finding is the watch's; Slack's ids as names, no stamps.
    timeline = page.locator("#timeline")
    await expect(timeline.locator(".event", has_text="From a watch: slack · Bob")).to_have_count(1)
    slack_line = timeline.locator(".event", has_text="From Slack:")
    await expect(slack_line).to_contain_text("Channel: DM with Bob")
    await expect(slack_line).not_to_contain_text("D0BOB0001")
    # Watches: not searched is not "partly covered"; the reason once.
    status = page.locator("#status-line .bad", has_text="Watches:")
    await expect(status).to_contain_text("Watches: one not searched, one only partly covered")
    await page.locator("#settings-button").click()
    await page.locator('#settings-tabs [data-tab="connections"]').click()
    state_line = page.locator("#watch-state")
    await expect(state_line).to_contain_text("watch 1 not searched: Glean is not connected")
    await expect(state_line).to_contain_text("watch 2 only partly covered: Only the newest 3 DMs")
    assert (await state_line.text_content()).count("Glean is not connected") == 1
    await expect(page.locator("#watch-resting")).to_have_text(
        "Weekend: every 30 min (after-hours setting)"
    )
    await page.locator("#settings-cancel").click()
    log("the chat says a watch found it, Slack by names; a watch not searched is said so, once")

    # E5: every part of Home folds, its count in sight, remembered; Needs you opens again for
    # something new.
    parts = ["needs", "tasks-running", "tasks-monitoring", "tasks-artifacts", "tasks-recent"]
    parts = [part for part in parts if await page.locator(f"#{part}").is_visible()]
    assert len(parts) >= 4, parts  # Running has nothing now.
    for part in parts:
        node = page.locator(f"#{part}")
        await expect(node).to_have_attribute("open", "")
        count = await node.locator(":scope > summary .count").text_content()
        await node.locator(":scope > summary").click()
        await expect(node).not_to_have_attribute("open", "")
        await expect(node.locator(":scope > summary .count")).to_have_text(count)
        await page.wait_for_function(
            "part => localStorage.getItem(`cos.section.${part}`) === '0'", arg=part
        )
    await page.reload()
    await expect(page.locator("#tasks-monitoring")).to_be_attached(timeout=15000)
    for part in ("needs", "tasks-monitoring", "tasks-recent"):
        await expect(page.locator(f"#{part}")).not_to_have_attribute("open", "")
    fresh = store.create_draft(asker, "reply", {"to": "Al", "where": "email"}, "Noted.")
    runtime.bus.publish("draft", store.draft(fresh["id"]))
    await expect(page.locator("#needs")).to_have_attribute("open", "", timeout=10000)
    for part in parts[1:]:
        await page.locator(f"#{part} > summary").click()
        await expect(page.locator(f"#{part}")).to_have_attribute("open", "")
    log("every part of Home folds with its count in sight, remembered; Needs you reopens")

    # E2: a short tooltip of our own, gone when what it is for moves; the list holds still
    # under the mouse and moves on when the mouse leaves.
    first = recent.locator(".card.task").first
    await first.locator(".head .meta").hover()
    tip = page.locator("#tip")
    await expect(tip).to_be_visible()
    await expect(tip).to_contain_text("Task ")
    before = await first.get_attribute("data-task")
    # Finished meanwhile (by a worker with nothing else open: nothing else on Home moves).
    ended = runtime.tasks.create(reporter, "Finished while hovered", "x", status="done")
    runtime.tasks.update(ended["id"], result="Done meanwhile.")
    runtime.task_actions.publish(runtime.tasks.get(ended["id"]))
    await page.wait_for_timeout(1500)
    assert await recent.locator(".card.task").first.get_attribute("data-task") == before
    await page.mouse.move(700, 450)
    await expect(tip).to_be_hidden()
    await expect(recent.locator(".card.task").first).to_contain_text(
        "Finished while hovered", timeout=10000
    )
    log("a short tooltip, gone when the mouse leaves; the list waits for the mouse to leave")

    for width in (352, 376, 400):
        await page.evaluate("w => document.querySelector('#panel').style.width = `${w}px`", width)
        for part in (monitoring, recent, made):
            found = await part.evaluate(OVERFLOWS)
            assert not found, (width, found)
        if width in (352, 400):
            for part, name in ((monitoring, "monitoring"), (recent, "recent")):
                await part.screenshot(path=SHOTS / f"ui-edges-{name}-{width}.png")
    await page.evaluate("document.querySelector('#panel').style.removeProperty('width')")
    await page.screenshot(path=SHOTS / "ui-edges-home-1440.png")
    for draft in (slack, line, merge, lessons, unread, fresh):
        store.update_draft(draft["id"], status="rejected")
        runtime.bus.publish("draft", store.draft(draft["id"]))
    await context.close()


async def round_seven(runtime, browser, failures: list[str]):
    """The 0.1.27 bug bash, round 3 (UI): a title with " · " whole in its chat line; workers by
    the name they show in @ and the "To" chip; what Watches says after hours; a withdrawn
    draft's line after a reload; a worker that could not go on says why and opens; a file's name
    left in Artifacts; a redo needs a reason; rows fold from the keyboard; Rename on waiting and
    failed tasks; "Checks back" once on a row; a review GitHub refused says so (no error); one
    word for a PR; a line keeps the ")" after a link; a bare approval's wording; Create PR on a
    branch the owner named; a large new file said so."""
    store = runtime.store
    router = store.get("router_id")
    stamp = datetime.now(UTC).isoformat()

    async def start(title):
        result = await call_tool(runtime, "spawn_worker", router, title=title,
                                 instructions=title, why_new="test")  # fmt: skip
        assert result.result_type == "success", result.text_result_for_llm
        await settle(runtime)
        return json.loads(result.text_result_for_llm)["session_id"]

    def finish(worker, status, result):
        task = runtime.tasks.current(worker)
        runtime.tasks.move(task["id"], status, result=result)
        runtime.update_session(worker, status="idle", activity="")
        return task

    bench = await start("Benchmark words() · textkit")
    finish(bench, "done", "#44 (https://github.com/acme/cos-approve-test/pull/44) is open.")
    zebra = await start("Zebra cache question")
    runtime.task_actions.rename(runtime.tasks.current(zebra)["id"], "Cache size for app")
    runtime.update_session(zebra, status="idle", activity="")
    asker = await start("Pick the queue size")
    await call_tool(runtime, "report", asker, status="question", summary="10 or 20?")
    runtime.update_session(asker, status="waiting", activity="Waiting for you")
    failer = await start("Load the nightly data")
    finish(failer, "failed", "The bucket is gone.")
    exporter = await start("Fix the exporter PR")
    runtime.update_session(exporter, status="idle", activity="")
    named = await start("Add notes on a named branch")
    finish(named, "review", "Committed notes on bb3-ideas.")
    on_main = await start("Tidy the README on main")
    finish(on_main, "review", "Committed on main.")
    mocker = await start("Mock the Artifacts list")
    finish(mocker, "review", "The mockup is mock7.html.")
    # A worker that could not go on: why, in its line, and the whole report on a click.
    runtime.add_message(router, "inbound", "Can't tell Bob whether 0.3 is safe: you decide.\n\n"
                        "He needs a yes/no by 5pm.", {"from": asker, "title": "Slack",
                                                      "status": "blocked"})  # fmt: skip
    redo = store.create_draft(asker, "slack", {"channel_id": "C0REDO0001"}, "Sure.")
    # Withdrawn before the page loads: only the state it loads says so.
    gone = store.create_draft(asker, "slack", {"channel_id": "C0GONE0001"}, "Thanks!")
    runtime.add_message(router, "draft", "Thanks!", {"draft_id": gone["id"],
                                                     "label": "Slack reply"})  # fmt: skip
    store.update_draft(gone["id"], status="rejected", outcome="Withdrawn: Bob solved it",
                       resolved_at=stamp)  # fmt: skip
    refused = store.create_draft(asker, "github", {**review("acme/refused", []),
                                                   "verdict": "approve"}, "")  # fmt: skip
    for draft in (redo, refused):
        runtime.bus.publish("draft", store.draft(draft["id"]))
    await settle(runtime)
    runtime.publish_state()

    soon = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    pr21 = {"label": "acme/app#21", "url": "https://github.com/acme/app/pull/21",
            "state": "OPEN", "checks": "failing", "failing": ["lint"], "title": "Export all rows",
            "review": "CHANGES_REQUESTED", "own": True, "at": stamp}  # fmt: skip
    patches = {
        "Cache size for app": {"check_back": {"at": soon, "what": "the Flyte run"}},
        "Fix the exporter PR": {"watching": ["github:acme/app#21"], "receipts": {"prs": [pr21]}},
        "Add notes on a named branch": {"receipts": {
            "checkouts": [{"path": "/tmp/notes", "branch": "bb3-ideas", "commits": 1,
                           "files": 1, "insertions": 0, "deletions": 0, "large": 1}],
            "changed": {"files": 1, "insertions": 0, "deletions": 0, "commits": 1,
                        "uncommitted": 0, "large": 1}, "prs": []}},
        "Tidy the README on main": {"receipts": {
            "checkouts": [{"path": "/tmp/readme", "branch": "main", "commits": 1, "files": 1,
                           "insertions": 2, "deletions": 0}],
            "changed": {"files": 1, "insertions": 2, "deletions": 0, "commits": 1,
                        "not_counted": 2}, "prs": []}},
        "Mock the Artifacts list": {"artifacts": [{
            "url": "file:///tmp/cos-scratch/mock7.html", "path": "/tmp/cos-scratch/mock7.html",
            "name": "mock7.html", "status": "draft", "at": stamp}]},
    }  # fmt: skip

    context = await browser.new_context(viewport={"width": 1440, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await patch_state(page, patches)
    await page.goto(f"{BASE}/#token={TOKEN}")
    needs, monitoring = page.locator("#needs"), page.locator("#tasks-monitoring")
    recent, made = page.locator("#tasks-recent"), page.locator("#tasks-artifacts")
    timeline = page.locator("#timeline")

    def card(title):
        return page.locator("#panel .card.task").filter(
            has=page.locator(".head .title", has_text=title)
        )

    # U7: a title with " · " in it is whole; the why is on hover.
    started = timeline.locator(".event.started", has_text="Benchmark words()")
    await expect(started.locator(".what")).to_have_text(
        "Started “Benchmark words() · textkit”", timeout=15000
    )
    await expect(started.locator(".what")).to_have_attribute("title", re.compile("new because"))
    # U15: why it could not go on, and the report on a click.
    stuck = timeline.locator(".event", has_text="Slack couldn't go on")
    await expect(stuck.locator(".what")).to_have_text(
        "Slack couldn't go on: Can't tell Bob whether 0.3 is safe: you decide. He needs a yes/no "
        "by 5pm."
    )
    await stuck.locator("button.line").click()
    await expect(stuck.locator(".markdown")).to_contain_text("He needs a yes/no by 5pm.")
    # U11: withdrawn before the page loaded: its line says so all the same.
    record = timeline.locator(f'.draft-record[data-draft-id="{gone["id"]}"]')
    await expect(record.locator(".outcome")).to_have_text("Withdrawn: Bob solved it")
    log("chat: a title with “ · ” whole; why a worker could not go on; a withdrawn draft says so")

    # U8: a worker by the name it shows (the task it is on), found by that or its first title.
    composer = page.locator("#composer-input")
    for typed in ("@size", "@zebra"):
        await composer.fill("")
        await composer.press_sequentially(typed)
        await expect(page.locator("#mention-list li .name", has_text="Cache size for app")
                     ).to_have_count(1)  # fmt: skip
        await expect(page.locator("#mention-list")).not_to_contain_text("Zebra cache question")
    await page.locator("#mention-list li", has_text="Cache size for app").click()
    await expect(page.locator("#composer-to strong")).to_have_text("Cache size for app")
    await page.locator("#composer-to .clear").click()
    await composer.fill("")
    log("@: workers by the name they show, found by their first title too")

    # H10: a row's line keeps the ")" after a link. U-s1: a row folds from the keyboard.
    row = card("Benchmark words() · textkit")
    await expect(row.locator(".row-line .why")).to_have_text("#44 (cos-approve-test#44) is open.")
    title = row.locator(".head .title")
    await expect(title).to_have_attribute("aria-expanded", "false")
    await title.focus()
    await page.keyboard.press("Enter")
    await expect(row).to_have_class(re.compile(r"\bunfolded\b"))
    await expect(title).not_to_have_attribute("aria-expanded", "false")
    await page.keyboard.press("Escape")
    await expect(row).not_to_have_class(re.compile(r"\bunfolded\b"))
    await expect(title).to_be_focused()
    await page.keyboard.press("Enter")
    fold = row.get_by_role("button", name="Fold")
    await expect(fold).to_be_visible()
    await fold.press("Enter")
    await expect(row).not_to_have_class(re.compile(r"\bunfolded\b"))
    log("a row keeps the “)” after a link; Enter shows it whole, Escape or Fold folds it")

    # U-s2: Rename on a task waiting for an answer, and on a failed one.
    for title_text in ("Pick the queue size", "Load the nightly data"):
        waiting = needs.locator(".card.task", has_text=title_text)
        await waiting.locator(".actions .more-button").click()
        await expect(waiting.get_by_role("menuitem", name="Rename…")).to_be_visible()
        await page.keyboard.press("Escape")
    log("Rename… on a task waiting for an answer and on a failed one")

    # U-s3: "Checks back" once on a row, the title keeps its room at 352 px.
    later = card("Cache size for app")
    await expect(later.locator(".row-line .why")).to_contain_text("Checks back at")
    await expect(later.locator(".head .meta")).not_to_contain_text("Checks back")
    # H9: changes requested wins over failing CI, in every place it is said.
    waits = card("Fix the exporter PR")
    await expect(waits.locator(".row-line .why")).to_contain_text("(changes requested)")
    await expect(made.locator(".artifact", has_text="app#21").locator(".a-status")).to_have_text(
        "changes requested"
    )
    await unfold(waits)
    await expect(waits.locator(".receipt.pr")).to_have_text("app#21 · Export all rows · ✗ CI · "
                                                            "changes requested")  # fmt: skip
    log("Checks back said once on a row; changes requested wins everywhere, a word on the chip")

    # U16b: a new file too large to count is said so. H4b: Create PR on a branch the owner
    # named, not on main.
    noted = needs.locator(".card.task", has_text="Add notes on a named branch")
    await expect(noted.locator(".receipt.changes")).to_have_text("1 large new file")
    await noted.locator(".actions .more-button").click()
    await expect(noted.get_by_role("menuitem", name="Create PR")).to_be_visible()
    await page.keyboard.press("Escape")
    tidy = needs.locator(".card.task", has_text="Tidy the README on main")
    await expect(tidy.locator(".receipt.changes")).to_have_text(
        "+2 −0 in 1 file · not counted: 2 files"
    )
    await expect(tidy.locator(".receipt.changes")).to_have_attribute(
        "title", re.compile("changed there meanwhile by you or other work")
    )
    await tidy.locator(".actions .more-button").click()
    await expect(tidy.get_by_role("menuitem", name="Copy report")).to_be_visible()
    await expect(tidy.get_by_role("menuitem", name="Create PR")).to_have_count(0)
    await page.keyboard.press("Escape")
    log("a large new file said so; Create PR on a branch of its own, not on main")

    # U17: a file's name starts at the left of its row, as a link's does.
    file_name = made.locator(".artifact", has_text="mock7.html").locator(".a-name")
    gap = await file_name.evaluate(
        "n => { const r = document.createRange(); r.selectNodeContents(n);"
        " return r.getBoundingClientRect().left - n.getBoundingClientRect().left; }"
    )
    assert abs(gap) <= 1, gap
    heights = [
        await made.locator(".artifact", has_text=name).evaluate("n => n.offsetHeight")
        for name in ("mock7.html", "app#21")
    ]
    assert abs(heights[0] - heights[1]) <= 1, heights

    # U6b: Ask to redo needs a reason.
    redo_card = needs.locator(f'.card.draft[data-id="{redo["id"]}"]')
    asked: list[str] = []

    async def asking(route):
        asked.append(route.request.url)
        await route.fulfill(json=store.draft(redo["id"]))

    await page.route(f"**/api/drafts/{redo['id']}", asking)
    await redo_card.locator(".actions .more-button").click()
    await redo_card.get_by_role("menuitem", name="Ask to redo…").click()
    await redo_card.get_by_role("button", name="Send back").click()
    await expect(redo_card.locator(".actions .error")).to_have_text("Say what should change.")
    await expect(redo_card.get_by_label("What should change?")).to_be_focused()
    assert not asked, asked
    await redo_card.get_by_role("button", name="Cancel").click()
    log("a file's name on the left in Artifacts; Ask to redo needs what should change")

    # H14: a bare approval says so. H6: GitHub refuses it (the draft fails): said, no error.
    refused_card = needs.locator(f'.card.draft[data-id="{refused["id"]}"]')
    await expect(refused_card.locator(".summary-preview")).to_have_text(
        "No summary: the approval only."
    )
    await refused_card.get_by_role("button", name="Review…").click()
    view = page.locator(f'.review-dialog[data-draft="{refused["id"]}"]')
    await expect(view.get_by_label("Summary")).to_have_attribute(
        "placeholder", "No summary: approve without a comment"
    )
    await expect(view.locator(".review-body .note")).to_have_text("No line comments.")
    await view.screenshot(path=SHOTS / "ui-edges-bare-approval.png")

    async def refuse(route):
        store.update_draft(refused["id"], status="failed", resolved_at=stamp,
                           outcome="GitHub refused it: the PR moved on.")  # fmt: skip
        runtime.bus.publish("draft", store.draft(refused["id"]))
        await asyncio.sleep(0.6)
        await route.fulfill(status=409, json={"detail": "GitHub refused it: the PR moved on."})

    await page.route(f"**/api/drafts/{refused['id']}", refuse)
    errors = len(failures)
    await view.get_by_role("button", name="Approve & send").click()
    await expect(page.locator(".toast", has_text="GitHub refused it")).to_be_visible(timeout=5000)
    await expect(view).to_have_count(0)
    await expect(refused_card).to_have_count(0)
    await page.wait_for_timeout(300)
    assert len(failures) == errors, failures[errors:]
    log("a bare approval says so; refused by GitHub, a toast says why and nothing breaks")

    # U10: after hours, how often it scans (not "not scanning" while it scans or has no watch).
    resting = """async ([connector, settings]) => {
      const s = await import("/static/state.js");
      const c = await import("/static/settings.js");
      s.state.snapshot.connector = { ...s.state.snapshot.connector, ...connector };
      s.state.snapshot.settings = { ...s.state.snapshot.settings, ...settings };
      c.renderConnections();
      const line = document.querySelector("#watch-resting");
      return line.hidden ? null : line.textContent;
    }"""
    watch = [{"what": "Questions for me in #eng", "how": "", "enabled": True}]
    cases = [
        ({"after_hours_reason": "weekend", "scanning": False},
         {"watches": watch, "watch_after_hours_minutes": 30},
         "Weekend: every 30 min (after-hours setting)"),
        ({"after_hours_reason": "outside_hours", "scanning": False},
         {"watches": watch, "watch_after_hours_minutes": 15},
         "Outside work hours: every 15 min (after-hours setting)"),
        ({"after_hours_reason": "weekend", "scanning": False},
         {"watches": watch, "watch_after_hours_minutes": 0},
         "Not scanning now: weekend, and after hours is 0"),
        ({"after_hours_reason": "weekend", "scanning": True},
         {"watches": watch, "watch_after_hours_minutes": 30}, None),
        ({"after_hours_reason": "weekend", "scanning": False},
         {"watches": [], "watch_after_hours_minutes": 30}, None),
        ({"after_hours_reason": "", "scanning": False},
         {"watches": watch, "watch_after_hours_minutes": 30}, None),
    ]  # fmt: skip
    for connector, settings, said in cases:
        assert await page.evaluate(resting, [connector, settings]) == said, (connector, settings)
    log("Watches after hours: how often it scans; nothing while scanning or with no watch")

    for scheme in ("light", "dark"):
        await page.emulate_media(color_scheme=scheme)
        for width in (352, 400):
            await page.evaluate(
                "w => document.querySelector('#panel').style.width = `${w}px`", width
            )
            for part in (monitoring, recent, made, needs):
                found = await part.evaluate(OVERFLOWS)
                assert not found, (scheme, width, found)
            title_width = await later.locator(".head .title").evaluate(
                "n => n.getBoundingClientRect().width"
            )
            assert title_width > 150, (width, title_width)
            await monitoring.screenshot(path=SHOTS / f"ui-edges-r7-monitoring-{width}-{scheme}.png")
            await noted.screenshot(path=SHOTS / f"ui-edges-r7-large-{width}-{scheme}.png")
            await tidy.screenshot(path=SHOTS / f"ui-edges-r7-not-counted-{width}-{scheme}.png")
            await made.screenshot(path=SHOTS / f"ui-edges-r7-artifacts-{width}-{scheme}.png")
        await page.evaluate("document.querySelector('#panel').style.removeProperty('width')")
        await page.set_viewport_size({"width": 1280, "height": 900})
        await page.screenshot(path=SHOTS / f"ui-edges-r7-home-1280-{scheme}.png")
        await page.set_viewport_size({"width": 1440, "height": 900})
    await context.close()


async def round_eight(runtime, browser, failures: list[str]):
    """The 0.1.28 bug bash, round 4 (UI): notifications show while the page is open (during
    Focus only urgent ones), and the Router's notes that need the owner wait under Needs you
    until seen (D4); the Slack session's timeline, its question cards and its lines on Home read
    without Slack's ids and stamps (R7, D8); "@" finds a worker by words with spaces (D10); a
    message over the limit is refused with the limit in words and kept (E6)."""
    store = runtime.store
    router = store.get("router_id")
    stamp = datetime.now(UTC).isoformat()

    async def start(title):
        result = await call_tool(runtime, "spawn_worker", router, title=title,
                                 instructions=title, why_new="test")  # fmt: skip
        assert result.result_type == "success", result.text_result_for_llm
        await settle(runtime)
        return json.loads(result.text_result_for_llm)["session_id"]

    checkout = await start("Fix the checkout test")
    cache = await start("Fix the cache size")
    # The Slack session, as Slack's tools and the Router write to it: ids, stamps, thread keys.
    slack = store.create_session(str(uuid4()), "slack", "Slack", "idle")["id"]
    store.set("slacknames", json.dumps({"D0ALICE01": "DM with Alice", "U0ALICE1": "Alice Chen"}))
    owner_id = runtime.slack.status.get("user_id")
    runtime.slack.status["user_id"] = "U0OWNER1"  # No name read for it: "you".
    to_slack = ('Slack event (DM with Alice Chen, thread slack:D0ALICE01:1794141072.949527): "hey '
                '<@U0OWNER1>, can you ping <@U0BOB001> and me about the textkit release date?" '
                "(ts 1794141072.949527). Draft a reply.")  # fmt: skip
    runtime.add_message(slack, "router", to_slack)
    asked = ("Alice Chen (DM D0ALICE01, ts 1794141072.949527) asked the owner to ping her "
             "and Bob (U0BOB001) about the textkit release date. What should the reply "
             "say?")  # fmt: skip
    runtime.add_message(slack, "report", asked, {"status": "question", "task_id": None})
    runtime.add_message(slack, "assistant", "Asked the Router; <@U0ALICE1> waits in D0ALICE01.")
    store.set(f"ownerq:{slack}:-", json.dumps({"text": asked, "task_id": None, "at": stamp}))
    runtime.add_message(router, "inbound", "Can't reply to <@U0ALICE1> in D0ALICE01 "
                        "(ts 1794141072.949527): you decide.", {"from": slack, "title": "Slack",
                                                                "status": "blocked"})  # fmt: skip
    await settle(runtime)
    runtime.publish_state()

    context = await browser.new_context(viewport={"width": 1440, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    needs, timeline = page.locator("#needs"), page.locator("#timeline")
    composer = page.locator("#composer-input")
    await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)

    # R7/D8: the Slack session's question card and its line on Home, in names.
    question = needs.locator(".card.question-card", has_text="textkit release date")
    await expect(question.locator(".body")).to_have_text(
        "Alice Chen (DM with Alice) asked the owner to ping her and Bob about the textkit "
        "release date. What should the reply say?"
    )
    await expect(timeline.locator(".event", has_text="Can't reply to").locator(".what")
                 ).to_have_text("Slack couldn't go on: Can't reply to @Alice Chen in DM with "
                                "Alice: you decide.")  # fmt: skip
    # E6 (any form): over the limit, the server's answer is said in words.
    await question.get_by_role("button", name="Reply").click()
    await question.locator("textarea").fill("x" * 20001)
    await question.get_by_role("button", name="Send").click()
    await expect(question.locator(".actions .error")).to_have_text(
        "Too long: 20,001 characters, the limit is 20,000"
    )
    await question.get_by_role("button", name="Cancel").click()
    await question.locator(".head .title").click()
    await expect(page.locator("#session-title")).to_have_text("Slack")
    for said in ('thread in DM with Alice): "hey @you, can you ping @U0BOB001 and me',
                 "Alice Chen (DM with Alice) asked the owner to ping her and Bob about",
                 "Asked the Router; @Alice Chen waits in DM with Alice."):  # fmt: skip
        await expect(timeline).to_contain_text(said)
    shown = await timeline.inner_text()
    for raw in ("<@", "U0OWNER1", "ts 1794", "slack:D0ALICE01", "(U0BOB001)", "D0ALICE01"):
        assert raw not in shown, (raw, shown)
    for scheme in ("light", "dark"):
        await page.emulate_media(color_scheme=scheme)
        await page.set_viewport_size({"width": 1280, "height": 900})
        await page.wait_for_timeout(300)  # Colors fade in: shot once they are there.
        await page.screenshot(path=SHOTS / f"ui-edges-r8-slack-1280-{scheme}.png")
    await page.emulate_media(color_scheme="light")
    await page.set_viewport_size({"width": 1440, "height": 900})
    await page.locator("#back-home").click()
    log("Slack: its timeline, its question card and its line on Home in names, no stamps")

    # D4: what needs the owner shows as a toast (news only away from Home, which says it in its
    # chat); the Router's notes that need the owner wait under Needs you (urgent ones a toast
    # too, and the badge counts them); one that only informs does not.
    focused = await page.evaluate("document.hasFocus()")
    badge = page.locator(".home-item .badge")
    before = int(await badge.text_content() or 0)
    runtime.alert("“Fix the checkout test” failed: the bucket is gone.", session_id=checkout,
                  needs_action=True)  # fmt: skip
    failed = page.locator(".toast.attention", has_text="“Fix the checkout test” failed")
    await expect(failed).to_be_visible()
    runtime.alert("“Fix the cache size” finished", session_id=cache, needs_action=False)
    news = page.locator(".toast", has_text="“Fix the cache size” finished")
    await page.wait_for_timeout(300)
    await expect(news).to_have_count(0 if focused else 1)
    # Away from Home, news shows too; not what the session in view says itself.
    await page.locator("#session-list .item", has_text="Fix the cache size").click()
    await expect(page.locator("#back-home")).to_be_visible()
    runtime.alert("“Fix the cache size” has a question", session_id=cache, needs_action=True)
    runtime.alert("“Fix the checkout test” finished", session_id=checkout, needs_action=False)
    await expect(page.locator(".toast:not(.attention)", has_text="“Fix the checkout test” "
                              "finished")).to_be_visible()  # fmt: skip
    await page.locator(".toast", has_text="“Fix the checkout test” finished").click()
    await expect(page.locator("#session-title")).to_have_text("Fix the checkout test")
    await expect(page.locator(".toast", has_text="has a question")).to_have_count(
        0 if focused else 1
    )
    await page.locator("#back-home").click()
    await expect(page.locator(".home-item.selected")).to_be_visible()

    def note(text, **flags):
        meta = {"needs_action": False, "urgent": False, **flags}
        message = runtime.add_message(router, "update", text, meta, unread=True)
        runtime.alert(text, session_id=router, needs_action=meta["needs_action"],
                      urgent=meta["urgent"])  # fmt: skip
        return message

    asks = note("Alice (DM) asked you for the textkit release date.\nTell me the date, or say if "
                "I should draft a holding reply.", needs_action=True)  # fmt: skip
    down = note("The deploy pipeline is down: Bob is blocked on you today.", urgent=True)
    note("Two PRs merged overnight.")
    cards = needs.locator(".card.update-card")
    await expect(cards).to_have_count(2)
    first = cards.filter(has_text="textkit release date")
    await expect(first.locator(".head .title")).to_have_text("The Router")
    await expect(first.locator(".chip")).to_have_count(0)
    await expect(cards.filter(has_text="deploy pipeline").locator(".chip")).to_have_text("Urgent")
    await expect(badge).to_have_text(str(before + 2))
    await expect(page.locator(".toast.attention", has_text="deploy pipeline is down")
                 ).to_be_visible()  # fmt: skip
    # On Home its chat and Needs you show the Router's note already: no toast while looked at.
    for text in ("textkit release date", "Two PRs merged"):
        await expect(page.locator(".toast", has_text=text)).to_have_count(0 if focused else 1)
    await page.reload()
    await expect(cards).to_have_count(2, timeout=15000)
    for scheme in ("light", "dark"):
        await page.emulate_media(color_scheme=scheme)
        await page.wait_for_timeout(300)
        for width in (352, 400):
            await page.evaluate(
                "w => document.querySelector('#panel').style.width = `${w}px`", width
            )
            found = await needs.evaluate(OVERFLOWS)
            assert not found, (scheme, width, found)
            for index in range(2):
                await cards.nth(index).screenshot(
                    path=SHOTS / f"ui-edges-r8-note-{index}-{width}-{scheme}.png"
                )
        await page.evaluate("document.querySelector('#panel').style.removeProperty('width')")
        await page.set_viewport_size({"width": 1280, "height": 900})
        await page.screenshot(path=SHOTS / f"ui-edges-r8-home-1280-{scheme}.png")
        await page.set_viewport_size({"width": 1440, "height": 900})
    await page.emulate_media(color_scheme="light")
    await first.get_by_role("button", name="Got it").click()
    await expect(first).to_have_count(0)
    assert store.get(f"updateseen:{asks['id']}"), "seen on the server"
    urgent = cards.filter(has_text="deploy pipeline")
    await urgent.get_by_role("button", name="Open").click()
    await expect(urgent).to_have_count(0)
    line = timeline.locator(f'[data-id="{down["id"]}"]')
    await expect(line).to_have_class(re.compile(r"\bflash\b"))
    await expect(line).to_be_in_viewport()
    await page.reload()
    await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)
    await expect(cards).to_have_count(0)
    assert runtime.owner_updates() == []
    log("D4: notifications show; the Router's notes wait under Needs you until Got it or Open")

    # Narrow, Open shows the chat at the note's line.
    asked_repo = note("Which repo should the release notes go to?", needs_action=True)
    await page.set_viewport_size({"width": 700, "height": 800})
    await page.locator("#home-tabs [data-tab=needs]").click()
    await cards.first.get_by_role("button", name="Open").click()
    await expect(page.locator("#main")).to_have_class(re.compile(r"\btab-chat\b"))
    await expect(timeline.locator(f'[data-id="{asked_repo["id"]}"]')).to_be_in_viewport()
    await expect(cards).to_have_count(0)
    await page.set_viewport_size({"width": 1440, "height": 900})
    # Writing to the Router after its note answers it.
    note("Which branch should they go on?", needs_action=True)
    await expect(cards).to_have_count(1)
    await composer.fill("The textkit repo.")
    await composer.press("Enter")
    await expect(cards).to_have_count(0)
    assert runtime.owner_updates() == []
    # During Focus only urgent notifications show.
    await page.get_by_role("button", name="Focus").click()
    await page.get_by_role("menuitem", name="For 30 minutes").click()
    await expect(page.get_by_role("button", name="End focus")).to_be_visible()
    runtime.bus.publish("toast", {"text": "Held while focusing", "session_id": checkout,
                                  "needs_action": True, "urgent": False})  # fmt: skip
    runtime.alert("Prod is down", session_id=checkout, needs_action=True, urgent=True)
    await expect(page.locator(".toast", has_text="Prod is down")).to_be_visible()
    await expect(page.locator(".toast", has_text="Held while focusing")).to_have_count(0)
    await page.get_by_role("button", name="End focus").click()
    await expect(page.get_by_role("button", name="Focus")).to_be_visible()
    log("D4: writing to the Router answers its note; during Focus only urgent ones show")

    # D10: "@" with words and spaces; Enter picks, Escape closes for good.
    items = page.locator("#mention-list li")
    await composer.fill("")
    await composer.press_sequentially("@Fix ch")
    await expect(items.filter(has_text="Fix the checkout test")).to_have_count(1)
    await expect(items.filter(has_text="Fix the cache size")).to_have_count(1)
    await composer.press_sequentially("eck")
    await expect(items).to_have_count(1)
    await composer.press("Enter")
    await expect(page.locator("#composer-to strong")).to_have_text("Fix the checkout test")
    await expect(composer).to_have_value("")
    await page.locator("#composer-to .clear").click()
    await composer.press_sequentially("ask @Fix")
    await expect(page.locator("#mention-list")).to_be_visible()
    await composer.press("Escape")
    await expect(page.locator("#mention-list")).to_be_hidden()
    await composer.press_sequentially(" the cache")
    await expect(page.locator("#mention-list")).to_be_hidden()
    # Emptied as sending does (no input event): a fresh "@" opens the list again.
    await composer.evaluate("(node) => { node.value = ''; }")
    await composer.press_sequentially("@")
    await expect(page.locator("#mention-list")).to_be_visible()
    await composer.fill("")
    await composer.press_sequentially("@nothing like this")
    await expect(page.locator("#mention-list")).to_be_hidden()
    await composer.fill("")
    log("D10: @ finds “Fix the checkout test” by “Fix ch”; Enter picks, Escape closes for good")

    # E6: the count near the limit; over it, refused with the limit in words, the text kept.
    count = page.locator("#composer-count")
    await expect(count).to_be_hidden()
    await composer.fill("y" * 18500)
    await expect(count).to_have_text("18,500 of 20,000 characters")
    sent = store.value("SELECT count(*) FROM messages WHERE session_id=?", (router,))
    await composer.fill("z" * 20512)
    await expect(count).to_have_text("Too long: 20,512 characters, the limit is 20,000")
    await expect(count).to_have_class(re.compile(r"\bover\b"))
    for scheme in ("light", "dark"):
        await page.emulate_media(color_scheme=scheme)
        await page.evaluate("document.querySelectorAll('.toast').forEach((n) => n.remove())")
        for width in (1440, 700):
            await page.set_viewport_size({"width": width, "height": 900})
            await page.wait_for_timeout(300)
            found = await page.locator("#composer").evaluate(OVERFLOWS)
            assert not found, (scheme, width, found)
            await page.locator("#composer").screenshot(
                path=SHOTS / f"ui-edges-r8-limit-{width}-{scheme}.png"
            )
    await page.emulate_media(color_scheme="light")
    await page.set_viewport_size({"width": 1440, "height": 900})
    await composer.press("Enter")
    await expect(page.locator(".toast.error", has_text="Too long: 20,512 characters, the limit "
                              "is 20,000")).to_be_visible()  # fmt: skip
    assert await composer.input_value() == "z" * 20512, "the text stays"
    assert store.value("SELECT count(*) FROM messages WHERE session_id=?", (router,)) == sent
    await composer.fill("")
    await expect(count).to_be_hidden()
    log("E6: the count near 20,000; over it, “Too long: N characters, the limit is 20,000”")
    store.update_session(slack, closed_at=stamp)
    runtime.slack.status["user_id"] = owner_id
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
            task_id = runtime.tasks.current(worker)["id"]
            # A review that changed nothing informs: read, it is with the finished ones (no
            # Accept to click), marked a report.
            await expect(page.locator("#tasks-recent")).to_contain_text("Review the exports PR")
            await expect(reviewing.locator(".head .meta")).to_contain_text("Report")
            await expect(reviewing.get_by_role("button", name="Accept")).to_have_count(0)
            # Not read yet (CONTRACT task.read): a dot says so until the owner opens it.
            await expect(reviewing.locator(".head .dot.unread")).to_have_count(1)
            await expect(reviewing.locator(".head .meta")).to_have_attribute(
                "data-tip", re.compile("Only a report: nothing to accept. Done once read.")
            )
            # In the chat, the report is one line with its gist, pointing at the task's card.
            reported = page.locator("#timeline .event.done", has_text="Review the exports PR")
            await expect(reported).to_contain_text("finished: Review done.")
            async with page.expect_request(f"**/api/tasks/{task_id}/read") as read:
                await reported.get_by_role("button", name="View", exact=True).click()
            assert (await read.value).method == "POST"
            await expect(reviewing).to_have_class(re.compile(r"\bexpanded\b"))
            await expect(reviewing.locator(".report")).to_contain_text("Review done.")
            # Read now (once the server keeps it); Done, not Accept, ends it.
            await expect(reviewing.locator(".head .dot.unread")).to_have_count(0, timeout=5000)
            await expect(reviewing.get_by_role("button", name="Done")).to_have_attribute(
                "title", "Only a report: nothing to accept. Done once read."
            )
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
            await round_five(runtime, browser, failures)
            await round_six(runtime, browser, failures)
            await round_seven(runtime, browser, failures)
            await round_eight(runtime, browser, failures)
            await browser.close()
        server.should_exit = True
        await serving
        if failures:
            raise AssertionError(f"browser errors: {failures}")
    print("UI EDGE CASES PASSED")


if __name__ == "__main__":
    asyncio.run(main())
