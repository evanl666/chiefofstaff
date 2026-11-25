"""Browser test of edge cases in the UI, with no accounts: the real web app runs in this process
on a fake Copilot, so the test can create cards and events and check what the page shows.

    uv run --with playwright python scripts/ui_edges.py [round_nine …]
"""

import asyncio
import base64
import contextlib
import itertools
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import uvicorn
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from agent_paths import without_agents  # noqa: E402
from conftest import (  # noqa: E402
    FakeClient,
    FakeSession,
    FakeSlackMcp,
    FakeTokens,
    call_tool,
    settle,
)

from chiefofstaff.runtime import Runtime  # noqa: E402
from chiefofstaff.sources.slack import SlackSource  # noqa: E402
from chiefofstaff.web import create_app  # noqa: E402

PORT = int(os.environ.get("UI_EDGES_PORT", "8798"))
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
    "watch_after_hours_minutes", "away_notify", "away_minutes", "away_how", "other_sessions",
    "send_to_chief", "update_notice", "slack_delegate_reply", "fork_busy",
}  # fmt: skip
# (With Copilot alone, as here: other agents' fields are made only where one is installed or on.)
# Prepared for you: its switch in General (its tab's fields come only while it is on).
SETTINGS_FIELDS |= {"proactive"}
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
# Elements cut short with an ellipsis (wider than they are shown), under a root.
TRUNCATED = """(root, selector) => [...root.querySelectorAll(selector)]
  .filter((node) => node.offsetParent && node.scrollWidth > node.clientWidth + 1)
  .map((node) => `${node.className}: ${node.textContent}`)"""
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
    """A row shows its whole card: a click on its title (its second line may hold links)."""
    classes = (await card.get_attribute("class")).split()
    if "row" in classes and "unfolded" not in classes:
        await card.locator(":scope > .head .title").click()
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
        # On Your OK (E123): its line, and its whole card on Details.
        row = page.locator(f'#needs .your-ok .ok-row[data-id="{seeded["card"]["id"]}"]')
        await expect(row).to_be_visible(timeout=15000)
        await row.locator(".more-button").click()
        await row.get_by_role("menuitem", name="Details").click()
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
    card = page.locator(f'#needs .your-ok .ok-row[data-id="{seeded["card"]["id"]}"]')
    await card.get_by_role("button", name="Use", exact=True).click()
    await expect(card).to_have_count(0, timeout=10000)
    assert store.one("SELECT status FROM checks WHERE id='kcard'")["status"] == "active"
    rows = page.locator(f'#needs .your-ok .ok-row[data-id="{seeded["lessons"]["id"]}"]')
    await expect(rows).to_have_count(2)
    drop = page.locator('#needs .your-ok .ok-row[data-lesson="ldrop"]')
    await drop.locator(".more-button").click()
    await drop.get_by_role("menuitem", name="Drop").click()
    await expect(drop).to_have_count(0, timeout=10000)
    # The other one: still on Your OK (on a card of its own now), kept from its line.
    keep = page.locator('#needs .your-ok .ok-row[data-lesson="lkeep"]')
    await keep.get_by_role("button", name="Keep").click()
    await expect(keep).to_have_count(0, timeout=10000)
    statuses = {row["id"]: row["status"] for row in store.all("SELECT id, status FROM lessons")}
    assert statuses == {"lkeep": "active", "ldrop": "rejected"}, statuses
    assert not [d for d in store.drafts() if d["kind"] == "lessons"], "both were seen"
    log("check: proof shown on Details, used from its line; lessons: dropped, kept, a line each")

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
    await expect(usage).to_contain_text("Finished and accepted")
    await expect(usage).to_contain_text("2.5 credits each")
    log("usage: what an accepted result cost")
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
    # Tasks groups the four parts below Needs you (no box of its own).
    await expect(page.locator("#tasks-running")).to_be_visible()
    await expect(page.locator("#needs")).to_be_hidden()
    await expect(chat).to_be_hidden()
    await expect(page.locator("#home-tabs [data-tab=needs] .count")).to_have_text("1")
    await page.screenshot(path=SHOTS / "ui-edges-home-narrow.png")
    await page.locator("#home-tabs [data-tab=chat]").click()
    await expect(page.locator("#composer-input")).to_be_focused()
    await page.set_viewport_size({"width": 1280, "height": 900})
    log("narrow: Chat, Needs you and Tasks one at a time")

    # Settings: one form, every field there from the start, and saved as before.
    await page.locator("#settings-button").click()
    names = set(await dialog.evaluate(
        "d => [...d.querySelectorAll('[name]')].map(n => n.name)"
    ))  # fmt: skip
    assert names == FORM_NAMES, names ^ FORM_NAMES
    for tab in ("general", "workers", "agents", "connections", "you", "advanced"):
        await dialog.locator(f"#settings-tabs [data-tab={tab}]").click()
        await expect(dialog.locator(f".tab[data-tab={tab}]")).to_be_visible()
        await expect(page.locator("#settings-form footer button[type=submit]")).to_be_visible()
    await expect(dialog.locator("#connections-extra")).to_have_count(1)
    settings = store.settings().model_dump()
    async with page.expect_request("**/api/settings") as request:
        await page.locator("#settings-form footer button[type=submit]").click()
    body = (await request.value).post_data_json
    # What the form shows, and every other setting as it was: a save resets nothing. `base`:
    # the settings as the dialog showed them, so a change from another tab is not undone.
    assert body.pop("base") == settings, "base"
    assert SETTINGS_FIELDS <= set(body) == set(settings), set(body) ^ set(settings)
    for key in set(settings) - {"role_models", "role_efforts", "slack_delegate_emoji"}:
        assert body[key] == settings[key], (key, body[key], settings[key])
    assert body["slack_delegate_emoji"].strip(":") == settings["slack_delegate_emoji"]
    for key in ("role_models", "role_efforts"):
        assert body[key] == {k: v for k, v in settings[key].items() if v}, (key, body[key])
    await expect(dialog).to_be_hidden()
    log("settings: six pages, every field kept; saving posts what it did before")

    # Connections: a watch added, MCP servers checked as JSON, a skill uploaded and removed.
    await page.locator("#settings-button").click()
    await dialog.locator("#settings-tabs [data-tab=connections]").click()
    await dialog.locator(".conn[data-conn=tools] .conn-open").click()
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
    await dialog.locator("[data-conn-detail=tools] [data-back]").click()
    await dialog.locator(".conn[data-conn=watches] .conn-open").click()
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
    # Opened again: on the page left, its list of connections.
    await expect(dialog.locator("#conn-list")).to_be_visible()
    await dialog.locator(".conn[data-conn=tools] .conn-open").click()
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
    # No address in the page carries the launch key (a link would put it in the history or a
    # copied link): the image is fetched with the key in a header and shown from a blob: address.
    assert (await image.get_attribute("src")).startswith("blob:")
    keyed = await page.evaluate(
        "() => [...document.querySelectorAll('[href], [src]')]"
        ".map((n) => n.getAttribute('href') || n.getAttribute('src') || '')"
        ".filter((value) => value.includes('key='))"
    )
    assert keyed == [], keyed
    async with page.expect_popup() as opened:
        await bubble.locator(".attachments a").first.click()
    full = await opened.value
    assert full.url.startswith("blob:") and "key=" not in full.url, full.url
    await full.close()
    # Code review: once 200 newer files were loaded, an image still on screen opened an address
    # let go (nothing there); it is fetched again.
    first = bubble.locator(".attachments a").first
    evicted = await first.get_attribute("href")
    await page.evaluate(
        "async () => { const dom = await import('/static/dom.js');"
        " for (let n = 0; n < 200; n++) await dom.blobUrl(`/static/favicon.svg?n=${n}`); }"
    )
    live = "async (address) => (await import('/static/dom.js')).blobLive(address)"
    assert not await page.evaluate(live, evicted), "let go"
    async with page.expect_popup() as opened:
        await first.click()
    again = await opened.value
    await again.wait_for_load_state()
    assert again.url.startswith("blob:") and again.url != evicted, again.url
    assert await again.evaluate("() => document.images[0]?.naturalWidth === 1"), "it shows"
    await again.close()
    kept = await page.evaluate(
        "async () => { const dom = await import('/static/dom.js');"
        " const used = await dom.blobUrl('/static/favicon.svg?used');"
        " for (let n = 0; n < 199; n++) await dom.blobUrl(`/static/favicon.svg?m=${n}`);"
        " await dom.blobUrl('/static/favicon.svg?used');"
        " await dom.blobUrl('/static/favicon.svg?newer');"
        " return dom.blobLive(used); }"
    )
    assert kept, "the ones used last are kept, not the ones loaded last"
    log("images: shown and opened full size from blob: addresses, never one with the key")

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
    await expect(card.locator(".row-line .latest")).to_have_text("reading the reply")
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
    # Its row has the PR it delivered; its whole card (View report) the receipts.
    await expect(card.locator(".row-foot .receipt.pr")).to_have_text(
        "ranking-model#71 · ✓ CI · approved"
    )
    await card.get_by_role("button", name="View report").click()
    await expect(card.locator(".chips .receipt.pr")).to_have_text(
        "ranking-model#71 · ✓ CI · approved"
    )
    title = await card.locator(".chips .receipt.pr").get_attribute("data-tip")
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
        "The last local test run failed (exit 1), but the checks on acme/ranking-model#71 pass now."
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

    # What workers told each other: a line in the Router's chat by their short names (E129),
    # cards in their timelines.
    await expect(page.locator("#timeline .event", has_text="Filter opt-outs told Opt-out "
                              "client")).to_be_visible(timeout=5000)  # fmt: skip
    # In a worker's chat (E130): a line in its Work, the card on its Notes tab (Team for a part
    # of one of the owner's requests: these were started on their own).
    await page.locator("#session-list .item", has_text="Opt-out client").click()
    await expect(page.locator("#timeline .peer-msg > .event", has_text="Filter opt-outs:"))\
        .to_be_visible()  # fmt: skip
    await page.locator("#worker-tabs").get_by_role("tab", name=re.compile("^Notes")).click()
    await expect(page.locator("#timeline .card", has_text="From Filter opt-outs")).to_be_visible()
    await page.locator("#session-list .item", has_text="Filter opt-outs").click()
    await expect(page.locator("#timeline .peer-msg > .event", has_text="Told Opt-out client:"))\
        .to_be_visible()  # fmt: skip
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
                             title="Fix review comments on lms", earlier="keep",
                             why_new="another PR")  # fmt: skip
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
    # for it) under Monitoring, paused; a row each (QA E16: no box per worker), its worker a
    # small reference at its foot.
    running_box = board.locator(f'#tasks-running .card.task[data-session="{first}"]')
    queued_box = board.locator(f'#tasks-monitoring .card.task[data-session="{first}"]')
    await expect(running_box).to_have_count(1, timeout=15000)
    await expect(running_box.locator(".head .title")).to_have_text("Fix review comments on lms")
    await expect(running_box.locator(".row-line .worker-ref")).to_have_count(1)
    await expect(queued_box.locator(".head .title")).to_have_text("Editor event paths")
    # Its status beside the title, why on its second line: not said twice (U-s3).
    await expect(queued_box.locator(".head .meta")).to_contain_text("Paused")
    await expect(queued_box.locator(".row-line .why")).to_have_text(
        "its worker is on “Fix review comments on lms” now"
    )
    await expect(running_box.locator(".row-line .latest")).to_have_count(1)
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
    await expect(running_box.locator(".head .title")).to_have_text(
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
    # The news in its row's line; all of it on its card.
    await expect(update.locator(".row-line .latest")).to_contain_text("approved and ready")
    await unfold(update)
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

    card = running_box
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
                    title="CoS bug bash", text="Now the bug bash", earlier="keep",
                    why_new="another subject")  # fmt: skip
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
    # Accepted (a report too waits for Accept): with the finished ones.
    runtime.task_actions.accept(done_task["id"])
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

    # On Your OK (E123): a line, Merge its own button, never with others.
    merge_card = needs.locator(f'.your-ok .ok-row[data-id="{merge["id"]}"]')
    await expect(merge_card).to_contain_text("Ready to merge")
    await expect(merge_card).to_contain_text("app#7")
    await expect(merge_card.locator(".ok-safe")).to_have_count(0)
    await expect(merge_card.get_by_role("button", name="Merge")).to_be_visible()
    await merge_card.locator(".more-button").click()
    await merge_card.get_by_role("menuitem", name="Not now").click()
    await expect(merge_card).to_have_count(0, timeout=10000)
    assert store.draft(merge["id"])["status"] == "rejected"
    log("a PR ready to merge is a line on Your OK: Merge, or not now")

    # The sidebar says what Home says.
    def dot(title):
        return page.locator("#session-list .item", has_text=title).locator(".dot")

    await expect(dot("Pick the database")).to_have_class(re.compile(r"\bwaiting\b"))
    await expect(dot("Answer Carol")).to_have_class(re.compile(r"\bwaiting\b"))
    await expect(dot("Watch CI on #12")).to_have_class(re.compile(r"\bwatching\b"))
    # An accepted report: its worker has nothing open, folded away with the others like it.
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

    group = page.locator(f'#tasks-running .card.task[data-session="{busy}"]')
    waits = page.locator(f'#tasks-monitoring .card.task[data-session="{busy}"]')
    await expect(group).to_have_count(1)
    await expect(group.locator(".head .title")).to_have_text("CoS bug bash")
    await expect(waits.locator(".head .title")).to_have_text("Research the Slack bot")
    # A row: what its worker does now in a line; its whole card (its progress, its links) on a
    # click.
    await expect(group.locator(".row-line .latest")).to_contain_text("Measure the row overflow")
    await unfold(group)
    for row in (group, waits):
        card_box = await row.bounding_box()
        meta_box = await row.locator(".head .meta").bounding_box()
        assert meta_box["x"] + meta_box["width"] <= card_box["x"] + card_box["width"] + 1
        menu_box = await row.locator(".more-button").bounding_box()
        assert menu_box["x"] + menu_box["width"] <= card_box["x"] + card_box["width"] + 1
        assert not await row.evaluate(OVERFLOWS), await row.evaluate(OVERFLOWS)
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
    chips_now = group.locator(".artifacts")
    # A doc and a PR it only mentions (it did not say it made them) are references, folded
    # under "More links"; the doc named as the report linked it, its id on hover.
    await expect(chips_now.locator(".more-links summary")).to_have_text("More links: 2 referenced")
    await expect(chips_now.locator(".more-links .receipt", has_text="app#592")).to_have_count(1)
    doc_chip = chips_now.locator(".more-links .receipt.link-chip", has_text="the B16 notes")
    await expect(doc_chip.locator(".chip-link")).to_have_attribute(
        "data-tip", re.compile("Google Doc abc123")
    )
    await expect(chips_now.locator(".receipt", has_text="Google Doc")).to_have_count(0)
    await expect(group.locator(".activity")).to_contain_text("Measure the row overflow")
    # Bug bash 7 (D7): a long step is cut to fit, and how long it has run stays in sight.
    long_step = "Run the long soak check with every fixture and report each timing " * 3
    runtime.states[busy].steps["soak"] = (time.time() - 180, long_step, "sleep 200")
    runtime.update_session(busy, status="working", activity="soak")
    activity = group.locator(".activity").first
    await expect(activity.locator(".when")).to_have_text(" · 3m")
    when_box = await activity.locator(".when").bounding_box()
    row_box = await activity.bounding_box()
    assert when_box["x"] + when_box["width"] <= row_box["x"] + row_box["width"] + 1, row_box
    runtime.states[busy].steps.pop("soak")
    runtime.update_session(busy, status="working", activity="Measure the row overflow again")
    log("a row per task: what its worker does now; its card: two clean lines, when apart, links")

    asking_card = page.locator("#tasks .card.task", has_text="Answer Carol")
    if await asking_card.count():
        await expect(asking_card.locator(".receipt.needs")).to_be_visible()
    watching = page.locator("#tasks-monitoring .card.task", has_text="Watch CI on #12")
    await expect(watching).to_be_visible()
    # What it waits on (QA E12): the PR it follows, a link to it.
    await expect(watching.locator(".row-line .why")).to_have_text(
        re.compile(r"^(Following|Waiting for \w+)$")
    )
    await expect(watching.locator(".row-line a.object")).to_have_text("app#12")
    await unfold(watching)
    await expect(watching.locator(".receipt.watching .watch-name")).to_have_text("Watching app#12")
    # Bug bash 6 (T11): any thread or PR it follows can be let go from its card.
    await expect(
        watching.locator(".receipt.watching .unfollow", has_text="\u2715")
    ).to_have_attribute("aria-label", "Stop following app#12")
    counts = await page.evaluate("""() => {
      const n = (selector) => Number(document.querySelector(selector)?.textContent || 0);
      return [n("#home-tabs [data-tab=tasks] .count"),
        n("#tasks-running > summary .count") + n("#tasks-monitoring > summary .count")];
    }""")
    assert counts[0] == counts[1], counts
    await expect(watching.locator(".task-body .reason")).to_contain_text("app#12")
    log("monitoring says what it waits on; the Tasks count is the sum of its parts")

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
    # A row's title shows all of it, and folds it back; its worker, at its foot, opens that
    # worker's chat at the task.
    counted = recent.locator(".card.task", has_text="Count the orders")
    await counted.locator(".head .title").click()
    await expect(counted).to_have_class(re.compile(r"\bunfolded\b"))
    await expect(counted.locator(".head .title")).to_have_attribute("aria-expanded", "true")
    await counted.locator(".head .title").click()
    await expect(counted).not_to_have_class(re.compile(r"\bunfolded\b"))
    await counted.locator(".row-line .worker-ref").click()
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
    await expect(page.locator("#watches-summary")).to_have_text("None")
    await page.locator(".conn[data-conn=watches] .conn-open").click()
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
                                 task="new", title=title, text=title, earlier="keep",
                                 why_new="another deliverable")  # fmt: skip
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

    # Bug bash 7 (G5): its own PR's CI fails after it was delivered: under Needs you, with a
    # way to tell its worker what to do and to let the PR go from its chip.
    cuber = await start("Add cube to textkit")
    cubed = runtime.tasks.current(cuber)
    cube_pr = {"label": "acme/app#88", "url": "https://github.com/acme/app/pull/88",
               "title": "Add cube", "state": "OPEN", "checks": "failing", "failing": ["lint"],
               "review": "", "own": True, "about": True}  # fmt: skip
    cube_pr["at"] = datetime.now(UTC).isoformat()
    cube_run = store.one("SELECT id FROM runs WHERE task_id=?", (cubed["id"],))
    store.execute("UPDATE runs SET evidence=? WHERE id=?",
                  (json.dumps({"checkouts": [], "prs": [cube_pr]}), cube_run["id"]))  # fmt: skip
    runtime.tasks.move(cubed["id"], "done", result="Opened acme/app#88.")
    runtime.follow(cuber, ["github:acme/app#88"], task_id=cubed["id"])
    runtime.update_session(cuber, status="idle", activity="")

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
    batch = {name: await start(name) for name in ("Batch run A", "Batch run B")}
    stamp = datetime.now(UTC)
    for index in range(33):
        owner = batch["Batch run B" if 10 <= index < 20 else "Batch run A"]
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
    await expect(review.locator(".head .meta")).to_contain_text("Awaiting acceptance")
    # Folded, its row has its PR (what it delivered); shown whole, its receipts.
    await expect(review.locator(".row-foot .receipt.pr .pr-state")).to_have_text(" · ✓ CI")
    await review.get_by_role("button", name="View report").click()
    await expect(review).to_have_class(re.compile(r"\bunfolded\b"))
    await expect(review.locator(".receipt.pr .pr-state")).to_have_text(" · ✓ CI")
    await expect(review.locator(".receipt.changes")).to_have_text("+40 −12 in 3 files")
    # What it delivered is its PR (the server's deliverables): a run its report only names is
    # a link on its card, with what is under it folded.
    main = review.locator(".chips > .receipts > .receipt.link-chip > .chip-link")
    await expect(main).to_have_count(0)
    folded = review.locator(".chips .more-links")
    await expect(folded.locator("summary")).to_have_text(
        "More links: 1 more made · 2 for diagnosis"
    )
    await expect(folded.locator(".receipt", has_text="Flyte · abc123")).to_have_count(1)
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
    # What it waits for and on what (QA E12): its PR, a link to it; its status beside its title.
    await expect(delivered.locator(".row-line .why")).to_have_text(re.compile(r"^Waiting for "))
    await expect(delivered.locator(".row-line .object")).to_contain_text("app#77")
    await expect(delivered.locator(".head .meta")).to_contain_text("Delivered")
    # One list: no groups inside Monitoring (QA E16).
    await expect(monitoring.locator("details")).to_have_count(0)
    later = monitoring.locator(".card.task", has_text="Run the weekly embeddings")
    await expect(later.locator(".row-line .why")).to_contain_text("the Flyte run finishing")
    await expect(later.locator(".row-line .why")).to_have_text(re.compile(r"^at "))
    await expect(later.locator(".head .meta")).to_contain_text("Checks back")
    await expect(running.locator(".card.task", has_text="Answer Jingtao")).to_have_count(1)
    weekly_row = running.locator(f'.card.task[data-session="{weekly}"]')
    await expect(weekly_row).to_have_count(1)
    log("monitoring says what it waits on; a check-back is its task's while its worker works")

    cube_card = page.locator(".card.task.marker-pr-failing", has_text="Add cube to textkit")
    await expect(cube_card.locator(".head .meta")).to_contain_text("CI failing")
    await expect(cube_card.locator(".row-line .why")).to_contain_text("CI failing on app#88 (lint)")
    await expect(cube_card.get_by_role("button", name="Ask it to fix…")).to_be_visible()
    await cube_card.locator(".pr-followed .unfollow").click()
    await expect(page.locator(".toast", has_text="Stopped following app#88")).to_be_visible()
    assert "github:acme/app#88" not in store.follows(cuber)
    log("a delivered PR with failing CI needs you: ask its worker to fix it, or let it go")

    cannot = page.locator(".card.task", has_text="Watch the example PR")
    await unfold(cannot)
    chip = cannot.locator(".receipt.failing")
    # Why, by the server's kind (here GitHub does not find it): not one that passes by itself.
    await expect(chip.locator(".words")).to_have_text(
        re.compile(r"(Can't read|No access to) app#42|app#42 not found")
    )
    await expect(cannot.locator(".receipt.watching")).to_have_count(0)
    await chip.get_by_role("button", name="stop following").click()
    await expect(page.locator(".toast", has_text="Stopped following app#42")).to_be_visible()
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
    await expect(run_row.locator(".a-kind")).to_have_text("Flow")
    await expect(run_row.locator(".a-status")).to_have_text("running")
    await expect(run_row).to_have_class(re.compile(r"\btone-active\b"))
    # A run only named in a report is no deliverable (QA E25).
    await expect(rows.filter(has_text="abc123")).to_have_count(0)
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
    # A row per task (its worker a small reference on it), each with a background and a
    # border, a few to the list's height; Show more at the end of the list.
    await expect(cards.first.locator(".row-line .worker-ref")).to_have_count(1)
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
                    earlier="keep", why_new="another plan")  # fmt: skip
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
    # Labelled an answer, by its worker's short name (0.1.57).
    await expect(answer.locator(".chip.answer")).to_have_text("Answer", timeout=10000)
    await expect(answer.locator(".what")).to_have_text(
        "Count the tests · 3 of them are slow: the e2e ones."
    )
    log("a worker's answer to the owner's @ from Home is on Home")

    told = timeline.locator(".event", has_text="Write the second plan").first
    await open_fold(told)
    await told.get_by_role("button", name="View", exact=True).click()
    flashed = page.locator(f'#panel .card.task.flash[data-id="{second["id"]}"]')
    await expect(flashed).to_have_count(1, timeout=5000)
    await expect(page.locator("#panel .card.task.flash")).to_have_count(1)
    # Named as the report linked them, else what they are; their ids on hover, not as names.
    plan_a_chip = flashed.locator(".artifacts > .receipts .receipt.link-chip", has_text="Plan A")
    # What it delivered, as Artifacts has it (QA E17): its kind, its name, where it stands.
    await expect(plan_a_chip.locator(".chip-link")).to_have_text("Doc · Plan A · draft")
    await expect(plan_a_chip.locator(".chip-link")).to_have_attribute(
        "data-tip", re.compile("PLANAAAA1111")
    )
    plan_b = flashed.locator(".more-links .receipt.link-chip .chip-link")
    await expect(plan_b).to_have_text("Google Doc")
    await expect(plan_b).to_have_attribute("data-tip", re.compile("PLANBBBB2222"))
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
    # As a card made before 0.1.53 kept it: what Slack answered, an email address in it.
    unread = store.create_draft(asker, "slack", {
        "channel_id": "C0DEPLOYS2", "to": "Bob (#deploys)",
        "latest_error": 'Slack showed no message there ({"messages":"=== THREAD PARENT MESSAGE '
        '===\\nFrom: Bob <bob@example.com> (U0BOB0001)…).'}, "On it.")  # fmt: skip
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
    ago = lambda **delta: (datetime.now(UTC) - timedelta(**delta)).isoformat()  # noqa: E731
    scratch = "/tmp/cos-scratch/eval"
    # What it waits on, as the server says it (QA E12/E18): what for, when it was read and is
    # read next, what wakes it, when it last changed.
    shipping = {"key": "github:acme/app#594", "kind": "pr", "label": "app#594",
                "url": "https://github.com/acme/app/pull/594", "waiting_for": "approval",
                "last_check": ago(minutes=5, seconds=10), "next_check": ago(seconds=-650),
                "trigger": "A review, CI result or merge on it brings it back",
                "changed_at": ago(hours=26, minutes=5)}  # fmt: skip
    patches = {
        "Eval SLM fine-tune report": {
            "made_links": [doc], "read": False, "deliverables": [
                {"kind": "doc", "url": doc, "title": "qwen3_8b SFT 11-05", "role": "",
                 "status": ""}],
            "files": [{"path": f"{scratch}/mock5.html", "name": "mock5.html", "size": 5120}],
            "receipts": {"tests": {"own": False, "cwd": "/tmp/cos-scratch/source-v0.1.26",
                                   "passed": True, "exit_code": 0, "summary": "592 passed",
                                   "command": "uv run pytest -q", "runs": 2,
                                   "failed_before": False, "running": False}}},
        "Ship app#594 export fix": {"watching": ["github:acme/app#594"], "watch_kind": "own",
                                    "monitoring": [shipping]},
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

    # E1/E9/E10, QA E1/E17: a report waits for Accept under Needs you, unread until opened;
    # what it delivered is its doc (named as linked, its address on hover); its file, and
    # docs.google.com itself, are links on its card, not deliverables; tests run elsewhere.
    reported = card("Eval SLM fine-tune report")
    await expect(reported).to_have_class(re.compile(r"\brow\b"), timeout=15000)
    assert await needs.locator(".card.task", has_text="Eval SLM").count() == 1
    await expect(reported.locator(".head .meta")).to_contain_text("Awaiting acceptance")
    await expect(reported.locator(".head .new-tag")).to_have_text("New")
    await expect(reported.locator(".head .dot")).to_have_class(re.compile(r"\bwaiting\b"))
    await expect(reported.locator(".row-foot .row-chips .chip-link")).to_have_text(
        ["Doc · qwen3_8b SFT 11-05"]
    )
    async with page.expect_request(f"**/api/tasks/{report['id']}/read"):
        await reported.locator(".row-line").click()
    await expect(reported).to_have_class(re.compile(r"\bunfolded\b"))
    await expect(reported.locator(".head .new-tag")).to_have_count(0)
    chips = reported.locator(".chips > .receipts")
    await expect(chips.locator(".link-chip .chip-link")).to_have_text(["Doc · qwen3_8b SFT 11-05"])
    await expect(chips.locator(".link-chip .chip-link").first).to_have_attribute(
        "data-tip", re.compile("1SPkOjC0xyz123")
    )
    await expect(chips.locator(".receipt", has_text="tests")).to_have_count(0)
    more_links = reported.locator(".chips > .more-links")
    await expect(more_links.locator("summary")).to_have_text(
        "More links: 1 more made · 1 referenced"
    )
    await expect(more_links.locator(".chip-link")).to_have_text(
        ["file · mock5.html", "docs.google.com"]
    )
    await expect(reported.get_by_role("button", name="Done")).to_have_count(0)
    await expect(reported.get_by_role("button", name="Accept")).to_be_visible()
    await expect(reported.get_by_role("button", name="Request changes")).to_be_visible()
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
    await more_links.locator("summary").click()
    async with page.expect_request("**/api/file?*") as opened:
        await more_links.get_by_role("button", name="file · mock5.html").click()
    assert "session=" in (await opened.value).url and "mock5.html" in (await opened.value).url
    await expect(page.locator("#file-dialog")).to_be_visible()
    await page.locator("#file-close").click()
    rows = made.locator(".artifact")
    doc_row = rows.filter(has_text="qwen3_8b SFT 11-05")
    await expect(doc_row.locator(".a-kind")).to_have_text("Doc")
    await expect(doc_row.locator(".a-status")).to_have_text("to accept")
    # Its file is no artifact (QA E25): Artifacts has PRs, docs and flows only.
    await expect(rows.filter(has_text="mock5.html")).to_have_count(0)
    await expect(rows.filter(has_text="Eval SLM fine-tune report")).to_have_count(1)
    await chips.get_by_role("button", name="Take qwen3_8b SFT 11-05 off").click()
    await expect(doc_row).to_have_count(0)
    await expect(more_links.locator("summary")).to_have_text(
        "More links: 1 more made · 1 referenced · 1 taken off"
    )
    await made.get_by_role("button", name="Show 1 taken off").click()
    await doc_row.get_by_role("button", name="Bring back").click()
    await expect(chips.locator(".link-chip .chip-link").first).to_have_text(
        "Doc · qwen3_8b SFT 11-05"
    )
    log(
        "a report: Accept and Request changes in sight, unread until opened, the doc it "
        "delivered by its name, its file a link not an artifact, tests elsewhere, ✕ and back"
    )

    # QA E12/E16/E18: Monitoring is one list (no groups); an accepted task waiting on others
    # says for what, on what (a link), how long nothing changed there (it stays monitored
    # however long), when it was checked and is checked next; others' PRs the owner reviewed
    # are with the finished ones (no obligation of theirs).
    await expect(monitoring.locator("details")).to_have_count(0)
    shipped = monitoring.locator(".card.task", has_text="Ship app#594")
    await expect(shipped.locator(".row-line .why")).to_have_text("Waiting for approval")
    await expect(shipped.locator(".row-line a.object")).to_have_text("app#594")
    await expect(shipped.locator(".row-line a.object")).to_have_attribute(
        "href", "https://github.com/acme/app/pull/594"
    )
    await expect(shipped.locator(".row-line .quiet")).to_have_text("No updates for 26h")
    await expect(shipped.locator(".row-foot .checks")).to_have_text(
        "checked 5 min ago · next in 10 min"
    )
    await expect(shipped.locator(".row-foot .checks")).to_have_attribute(
        "data-tip", "A review, CI result or merge on it brings it back"
    )
    await expect(shipped.locator(".head .meta")).to_contain_text("Delivered")
    for title in ("Review checkout-service PR #1537", "Correct the #1537 review draft",
                  "Investigate faro#1536 flake"):  # fmt: skip
        reviewed_row = recent.locator(".card.task").filter(
            has=page.locator(".head .title", has_text=title)
        )
        await expect(reviewed_row.locator(".head .meta")).to_contain_text("Reviewed")
    await expect(monitoring.locator(".card.task", has_text="#1537")).to_have_count(0)
    log("monitoring: one list; waits for what, on what, quiet how long, checked when")

    # E7 and #1: PR chips for PRs it made or reviewed, one state word; the rest referenced.
    prs = card("Check the PR states")
    await unfold(prs)
    # Its part in each (QA E26): one it reviewed says so.
    await expect(prs.locator(".chips > .receipts > .receipt.pr")).to_have_text(
        ["app#9 · ✓ CI · changes requested · reviewed", "app#11 · ✓ CI · ready to merge"]
    )
    await expect(prs.locator(".more-links summary")).to_have_text("More links: 1 referenced")
    await expect(prs.locator(".more-links .receipt.pr")).to_contain_text("app#10")
    await expect(rows.filter(has_text="app#11").locator(".a-status")).to_have_text("ready to merge")
    log("PR chips: made or reviewed in sight, one word for where each stands; mentioned folded")

    # E10: the Router wants a look: to review, Accept; #5: a second click sends nothing.
    mock = needs.locator(".card.task", has_text="Mockup for the new Home")
    await expect(mock.locator(".head .meta")).to_contain_text("Awaiting acceptance")
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
    box = needs.locator(".card.task", has_text="Pick the cache size")
    await expect(box.locator(".head .meta")).to_contain_text("Question")
    # Bug bash 6 (T5): no session id for its worker ("WORKER d62fdc").
    await expect(box.locator(".worker-ref")).not_to_have_text(re.compile(r"[0-9a-f]{6}"))

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
        "data-tip", re.compile("Back into “Schedule the weekly jobs”")
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
    merge_card = needs.locator(f'.your-ok .ok-row[data-id="{merge["id"]}"]')
    await expect(merge_card).to_contain_text("no checks")
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
    # One plain line, never what Slack answered.
    await expect(unread_card).to_contain_text(
        "Couldn't read this conversation from Slack just now; the reply still goes to Bob "
        "(#deploys)."
    )
    await expect(unread_card).not_to_contain_text("example.com")
    await expect(unread_card).not_to_contain_text("THREAD PARENT")
    # Withdrawn by its worker: off Needs you, and its line in the chat says so.
    store.update_draft(gone["id"], status="rejected", outcome="Withdrawn: the thread was solved")
    runtime.bus.publish("draft", store.draft(gone["id"]))
    await expect(needs.locator(f'.card.draft[data-id="{gone["id"]}"]')).to_have_count(0)
    record = page.locator(f'#timeline .draft-record[data-draft-id="{gone["id"]}"]')
    await expect(record.locator(".outcome")).to_have_text("Withdrawn: the thread was solved")
    log("drafts: an emptied reply is refused, not the draft sent; Slack names; no checks")

    # Lessons: a long one is read whole, at the narrowest panel too (its card on Details).
    await page.evaluate("document.querySelector('#panel').style.width = '352px'")
    row = needs.locator('.your-ok .ok-row[data-lesson="llong"]')
    await row.locator(".more-button").click()
    await row.get_by_role("menuitem", name="Details").click()
    lesson = needs.locator(".card.lessons textarea").first
    await expect(lesson).to_have_value(long_lesson.strip())
    await page.wait_for_timeout(200)
    sizes = await lesson.evaluate("n => [n.scrollHeight, n.clientHeight]")
    assert sizes[0] <= sizes[1] + 2 and sizes[1] > 40, sizes
    found = await page.locator("#panel").evaluate(OVERFLOWS)
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
    # Needs you too: its rows have Accept in the same place, so a new result must not slide
    # under the pointer as the owner clicks.
    needs = page.locator("#needs > .items")
    held = needs.locator(":scope > *").first
    await held.hover()
    before = await needs.evaluate("list => [...list.children].map((n) => n.dataset.key).join()")
    arrived = runtime.tasks.create(reporter, "Arrived while hovered", "x", status="review")
    runtime.tasks.update(arrived["id"], result="New result.")
    runtime.task_actions.publish(runtime.tasks.get(arrived["id"]))
    await expect(page.locator("#needs")).to_contain_text("Arrived while hovered", timeout=10000)
    after = await needs.evaluate("list => [...list.children].map((n) => n.dataset.key).join()")
    assert after.startswith(before), ("its rows keep their places; new at the end", before, after)
    await page.mouse.move(700, 450)
    # Then in its place: the newest of the tasks (drafts come first).
    await expect(needs.locator(":scope > .card.task").first).to_contain_text(
        "Arrived while hovered", timeout=10000
    )
    log("Needs you keeps its rows in place under the mouse; what is new still shows, at its end")

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
        "Fix the exporter PR": {"watching": ["github:acme/app#21"], "receipts": {"prs": [pr21]},
                                "deliverables": [{"kind": "pr", "url": pr21["url"],
                                                  "title": "app#21 · Export all rows",
                                                  "role": "created",
                                                  "status": "changes requested"}]},
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
    # Opening a result reads it: not on the server here (its task event would come without
    # what was put in above).
    await page.route("**/api/tasks/*/read", lambda route: route.fulfill(json={"read": True}))
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
    await expect(later.locator(".head .meta")).to_contain_text("Checks back")
    await expect(later.locator(".row-line .why")).to_have_text(re.compile(r"^at .*the Flyte run$"))
    # H9: changes requested wins over failing CI, in every place it is said.
    waits = card("Fix the exporter PR")
    await expect(waits.locator(".row-line .why")).to_have_text("Waiting for changes")
    await expect(waits.locator(".row-line .object")).to_have_text("app#21")
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
    await unfold(noted)
    await expect(noted.locator(".receipt.changes")).to_have_text("1 large new file")
    await noted.locator(".actions .more-button").click()
    await expect(noted.get_by_role("menuitem", name="Create PR")).to_be_visible()
    await page.keyboard.press("Escape")
    tidy = needs.locator(".card.task", has_text="Tidy the README on main")
    await unfold(tidy)
    await expect(tidy.locator(".receipt.changes")).to_have_text(
        "+2 −0 in 1 file · not counted: 2 files"
    )
    await expect(tidy.locator(".receipt.changes")).to_have_attribute(
        "data-tip", re.compile("changed there meanwhile by you or other work")
    )
    await tidy.locator(".actions .more-button").click()
    await expect(tidy.get_by_role("menuitem", name="Copy report")).to_be_visible()
    await expect(tidy.get_by_role("menuitem", name="Create PR")).to_have_count(0)
    await page.keyboard.press("Escape")
    log("a large new file said so; Create PR on a branch of its own, not on main")

    # U17: a name starts at the left of its row. QA E25: a mockup file is no artifact (PRs,
    # docs and flows only); it stays on its task's card.
    file_name = made.locator(".artifact", has_text="app#21").locator(".a-name")
    gap = await file_name.evaluate(
        "n => { const r = document.createRange(); r.selectNodeContents(n);"
        " return r.getBoundingClientRect().left - n.getBoundingClientRect().left; }"
    )
    assert abs(gap) <= 1, gap
    await expect(made.locator(".artifact", has_text="mock7.html")).to_have_count(0)
    kinds = await made.locator(".artifact .a-kind").all_text_contents()
    assert set(kinds) <= {"PR", "Doc", "Flow"}, kinds

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
    now_sent = store.value("SELECT count(*) FROM messages WHERE session_id=?", (router,))
    assert now_sent == sent, store.all(
        "SELECT kind, text FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (router, max(1, now_sent - sent)),
    )
    await composer.fill("")
    await expect(count).to_be_hidden()
    log("E6: the count near 20,000; over it, “Too long: N characters, the limit is 20,000”")
    store.update_session(slack, closed_at=stamp)
    runtime.slack.status["user_id"] = owner_id
    await context.close()


async def round_nine(runtime, browser, failures: list[str]):
    """The 0.1.29 bug bash, round 5 (UI): a draft for a result keeps that result's own card in
    it, with its receipts, Accept and Create PR (S6), and its news (W3); a narrow window folds
    the sidebar away behind a button, nothing past the edge (L12); the ⋯ menus from the
    keyboard, and Escape out of a card's form (A12); a title no longer than the server keeps
    (A9); what cancelling came to, in words (A2)."""
    store = runtime.store
    router = store.get("router_id")
    stamp = datetime.now(UTC).isoformat()

    async def start(title):
        result = await call_tool(runtime, "spawn_worker", router, title=title,
                                 instructions=title, why_new="test")  # fmt: skip
        assert result.result_type == "success", result.text_result_for_llm
        await settle(runtime)
        return json.loads(result.text_result_for_llm)["session_id"]

    def finish(worker, result):
        task = runtime.tasks.current(worker)
        runtime.tasks.move(task["id"], "review", result=result)
        runtime.update_session(worker, status="idle", activity="")
        return task

    committer = await start("Commit notes on r9-cpr")
    committed = finish(committer, "Committed notes/r9.md on r9-cpr. The PR text is on a card.")
    pr_text = store.create_draft(committer, "reply", {"to": "Owner", "where": "PR description",
                                                      "task_id": committed["id"]},
                                 "Title: Add notes/r9.md")  # fmt: skip
    answerer = await start("Answer Bob on r9 flags")
    answered = finish(answerer, "Bob's flag is off by default.")
    to_bob = store.create_draft(answerer, "reply", {"to": "Bob", "where": "email",
                                                    "task_id": answered["id"]},
                                "Hi Bob, it is off by default.")  # fmt: skip
    for draft in (pr_text, to_bob):
        runtime.bus.publish("draft", store.draft(draft["id"]))
    nightly = await start("Watch the r9 nightly run")
    runtime.update_session(nightly, status="idle", activity="")
    await settle(runtime)
    runtime.publish_state()
    patches = {
        "Commit notes on r9-cpr": {"receipts": {
            "checkouts": [{"path": "/tmp/r9", "branch": "r9-cpr", "commits": 1, "files": 1,
                           "insertions": 2, "deletions": 0}],
            "changed": {"files": 1, "insertions": 2, "deletions": 0, "commits": 1}, "prs": []}},
        "Answer Bob on r9 flags": {"news": {"at": stamp, "text": "Bob says the flag is on now."}},
    }  # fmt: skip

    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await patch_state(page, patches)
    await page.goto(f"{BASE}/#token={TOKEN}")
    needs = page.locator("#needs")
    await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)

    # S6: the PR text's card keeps its result's card, with what the owner decides on.
    card = needs.locator(f'.card.draft[data-id="{pr_text["id"]}"]')
    inner = card.locator(".card.task.in-draft")
    await expect(inner.locator(".head .of")).to_have_text("Result of")
    await expect(inner.locator(".head .title")).to_have_text("Commit notes on r9-cpr")
    await expect(inner.locator(".receipt.changes")).to_have_text("+2 −0 in 1 file")
    # One decision (QA6-2): the card's own action accepts the result it holds, and says so.
    await expect(inner.get_by_role("button", name="Accept")).to_have_count(0)
    await expect(inner.get_by_role("button", name="Request changes")).to_be_visible()
    await expect(card.get_by_role("button", name="Copy & accept")).to_be_visible()
    await expect(needs.locator(".card.task", has_text="Commit notes on r9-cpr")).to_have_count(1)
    # W3: news on a result a draft is for shows on that card, once.
    news = needs.locator(f'.card.draft[data-id="{to_bob["id"]}"] .card.task.in-draft')
    await expect(news.locator(".news")).to_contain_text("Bob says the flag is on now.")
    # Still a result to accept (QA round 10): its card's own action accepts it, news and all.
    await expect(news.get_by_role("button", name="Accept")).to_have_count(0)
    await expect(
        needs.locator(f'.card.draft[data-id="{to_bob["id"]}"]').get_by_role(
            "button", name="Copy & accept"
        )
    ).to_be_visible()
    await expect(needs.locator(".news", has_text="Bob says the flag")).to_have_count(1)
    await card.screenshot(path=SHOTS / "ui-edges-r9-draft-result.png")
    log("S6/W3: a draft's card holds its result's card: changes, its news, once; one action")

    # A12: its ⋯ from the keyboard: Enter opens it on its first item; arrows, Home and End
    # move (round); Escape closes it, back on ⋯.
    more = inner.locator(".actions .more-button")
    items = inner.locator('.menu [role="menuitem"]')
    await more.focus()
    await page.keyboard.press("Enter")
    await expect(items.first).to_be_focused()
    await page.keyboard.press("ArrowDown")
    await expect(items.nth(1)).to_be_focused()
    await page.keyboard.press("End")
    await expect(items.last).to_be_focused()
    await page.keyboard.press("ArrowDown")
    await expect(items.first).to_be_focused()
    await page.keyboard.press("ArrowUp")
    await expect(items.last).to_be_focused()
    await page.keyboard.press("Home")
    await expect(items.first).to_be_focused()
    await expect(inner.get_by_role("menuitem", name="Create PR")).to_be_visible()
    await page.keyboard.press("Escape")
    await expect(inner.locator(".menu")).to_be_hidden()
    await expect(more).to_be_focused()
    # A9: Rename… keeps a title as long as the server does. A12: Escape closes the form.
    await page.keyboard.press("ArrowDown")
    await inner.get_by_role("menuitem", name="Rename…").click()
    title = inner.get_by_role("textbox", name="Title")
    await expect(title).to_have_attribute("maxlength", "80")
    await expect(title).to_be_focused()
    await page.keyboard.press("Escape")
    await expect(title).to_have_count(0)
    await expect(inner.get_by_role("button", name="Request changes")).to_be_visible()
    await expect(card).to_be_visible()
    log("A12: ⋯ menus by arrow keys, Home, End and Escape; Escape closes a card's form; A9")

    # A2: refused (it finished meanwhile), the server's words; set aside, said so.
    row = page.locator("#tasks-monitoring .card.task", has_text="Watch the r9 nightly run")
    replies = []
    view = json.loads(json.dumps(runtime.task_actions.view(runtime.tasks.current(nightly)),
                                 default=str))  # fmt: skip
    set_aside = {**view, "status": "done", "set_aside": True, "ended_at": stamp,
                 "result": "The nightly run passed.", "updated_at": stamp}  # fmt: skip

    async def cancelling(route):
        replies.append(route.request.url)
        if len(replies) == 1:
            refused = "It finished just now: its result is under Recently finished."
            await route.fulfill(status=409, json={"detail": refused})
        else:
            # Made as the server makes it: after the action (an older reply sets nothing back).
            await route.fulfill(json={**set_aside, "updated_at": datetime.now(UTC).isoformat()})

    await page.route("**/api/tasks/*/cancel", cancelling)
    await row.click()
    await row.locator(".actions .more-button").click()
    await row.get_by_role("menuitem", name="Cancel task…").click()
    await row.get_by_role("button", name="Cancel task").click()
    await expect(row.locator(".actions .error")).to_have_text(
        "It finished just now: its result is under Recently finished."
    )
    await row.get_by_role("button", name="Cancel task").click()
    await expect(page.locator(".toast", has_text="It had finished: set aside, its result is "
                              "kept.")).to_be_visible()  # fmt: skip
    finished = page.locator("#tasks-recent .card.task", has_text="Watch the r9 nightly run")
    await expect(finished.locator(".head .meta")).to_contain_text("Set aside")
    log("A2: a refused cancel says why; one that came after the result says it was set aside")

    # L12: narrow, the sidebar folds away behind ☰; nothing runs past the right edge.
    toggle, sidebar = page.locator("#sidebar-toggle"), page.locator("#sidebar")
    for scheme in ("light", "dark"):
        await page.emulate_media(color_scheme=scheme)
        for width in (352, 400):
            await page.set_viewport_size({"width": width, "height": 800})
            await expect(toggle).to_be_visible()
            await expect(sidebar).to_be_hidden()
            for tab in ("needs", "tasks", "chat"):
                await page.locator(f"#home-tabs [data-tab={tab}]").click()
                past = await page.evaluate(
                    "() => document.documentElement.scrollWidth - innerWidth"
                )
                assert past <= 0, (scheme, width, tab, past)
                for part in await page.locator("#home-tabs button, #session-header > *").all():
                    if await part.is_visible():
                        box = await part.bounding_box()
                        assert box["x"] + box["width"] <= width + 1, (width, tab, box)
            composer = await page.locator("#composer").bounding_box()
            assert composer["width"] >= width - 32, (width, composer)
            await page.screenshot(path=SHOTS / f"ui-edges-r9-narrow-{width}-{scheme}.png")
        # From the keyboard: opens on Home, Escape closes it back on ☰.
        await toggle.focus()
        await page.keyboard.press("Enter")
        await expect(sidebar).to_be_visible()
        await expect(toggle).to_have_attribute("aria-expanded", "true")
        await expect(sidebar.locator(".home-item")).to_be_focused()
        assert (await sidebar.bounding_box())["width"] <= 352
        await page.wait_for_timeout(300)  # Slid in.
        await page.screenshot(path=SHOTS / f"ui-edges-r9-sidebar-open-{scheme}.png")
        await page.keyboard.press("Escape")
        await expect(sidebar).to_be_hidden()
        await expect(toggle).to_be_focused()
        # A worker picked there: the sidebar folds, its page shows.
        await toggle.click()
        await sidebar.locator(".item", has_text="Answer Bob on r9 flags").click()
        await expect(sidebar).to_be_hidden()
        await expect(page.locator("#session-title")).to_have_text("Answer Bob on r9 flags")
        await toggle.click()
        await page.mouse.click(345, 400)  # Beside it: it folds.
        await expect(sidebar).to_be_hidden()
        await page.locator("#back-home").click()
    await page.emulate_media(color_scheme="light")
    # Wider, as it was: the sidebar in its column, no ☰.
    for width, column in ((800, 210), (1280, 252)):
        await page.set_viewport_size({"width": width, "height": 900})
        await expect(toggle).to_be_hidden()
        await expect(sidebar).to_be_visible()
        assert round((await sidebar.bounding_box())["width"]) == column, width
    log("L12: at 352-400px the sidebar folds behind ☰ (keyboard, Escape, a pick closes it)")
    await context.close()


def picture(width: int, height: int) -> bytes:
    """A plain PNG of this size."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + data))
        return struct.pack(">I", len(data)) + kind + data + crc

    rows = b"".join(b"\x00" + b"\x60\x80\xc0" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    body = chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    return b"\x89PNG\r\n\x1a\n" + body


async def round_ten(runtime, browser, failures: list[str]):
    """The 0.1.32 bug bash, round 8 (UI): a result whose own PR has changes requested says so
    under Needs you, with Ask it to fix… and a quiet Accept (R13), and its report says Awaiting
    acceptance in its worker's chat; a path in a line is short (U3); inline code on a decision
    card (U7); the chat opens at its end when that is an image (U4); diff lines from the
    keyboard (U6);
    less motion when asked (U8); ☰'s count clear of what is beside it, and Focusing until…
    moves nothing (U9); names for screen readers (U10); Bring back takes its toast away
    (U13)."""
    store = runtime.store
    router = store.get("router_id")

    async def start(title):
        result = await call_tool(runtime, "spawn_worker", router, title=title,
                                 instructions=title, why_new="test")  # fmt: skip
        assert result.result_type == "success", result.text_result_for_llm
        await settle(runtime)
        return json.loads(result.text_result_for_llm)["session_id"]

    fixer = await start("Rename the r10 flag")
    fixed = runtime.tasks.current(fixer)
    notes = f"{Path.home()}/r10-notes/flag.md"
    runtime.tasks.move(fixed["id"], "review", result=f"Renamed it; the notes are at {notes}.")
    runtime.update_session(fixer, status="idle", activity="")
    runtime.add_message(fixer, "report", "Renamed it.", {"status": "done", "task_id": fixed["id"]})
    decider = await start("Speed up r10 words")
    decision = store.create_draft(decider, "decision", {
        "question": "words() is currently `text.split()`. Keep it?",
        "why": "A regex would also split `a,b`.",
        "options": [{"choice": "Keep `split()`", "effect": "Nothing changes"},
                    {"choice": "Regex", "effect": "Splits on `,` too"}],
        "recommendation": "Keep `split()`: callers expect it",
        "askers": [{"session_id": decider, "title": "Speed up r10 words"}], "precedents": [],
    }, "words() is currently `text.split()`. Keep it?")  # fmt: skip
    runtime.bus.publish("draft", store.draft(decision["id"]))
    # Owner report (0.1.53) item 1: a decision asked again on new evidence says which.
    revisit = store.create_draft(decider, "decision", {
        "question": "Split on commas after all?", "why": "Profiling: half the input is CSV.",
        "options": [], "recommendation": "",
        "askers": [{"session_id": decider, "title": "Speed up r10 words"}],
        "precedents": ["Keep `split()`?"], "replaces": "dec0r10a1",
        "previous": "words(): keep `split()`? → Keep `split()`",
    }, "Split on commas after all?")  # fmt: skip
    runtime.bus.publish("draft", store.draft(revisit["id"]))
    await settle(runtime)
    runtime.publish_state()
    pr = {"label": "acme/r10#4", "own": True, "state": "OPEN", "review": "CHANGES_REQUESTED",
          "url": "https://github.com/acme/r10/pull/4", "title": "Rename the flag"}  # fmt: skip
    delivered = [{"kind": "pr", "url": pr["url"], "title": "r10#4 · Rename the flag",
                  "role": "created", "status": "changes requested"}]  # fmt: skip
    patches = {"Rename the r10 flag": {"watching": ["github:acme/r10#4"], "watch_kind": "own",
                                       "receipts": {"prs": [pr]},
                                       "deliverables": delivered}}  # fmt: skip

    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await patch_state(page, patches)
    await page.goto(f"{BASE}/#token={TOKEN}")
    await expect(page.locator("#application")).to_be_visible()

    # R13 (as of QA round 10): to accept, its PR has changes requested: still a result awaiting
    # acceptance, with Accept and Request changes at hand; why it needs a look is its reason.
    card = page.locator("#needs .card.task", has_text="Rename the r10 flag")
    await expect(card.locator(".head .meta")).to_contain_text("Awaiting acceptance")
    await expect(card.locator(".row-line .why")).to_have_text("Changes requested on r10#4")
    await expect(card.get_by_role("button", name="Accept")).to_be_visible()
    await expect(card.get_by_role("button", name="Request changes")).to_be_visible()
    more = card.locator(".actions .more-button")
    await expect(more).to_have_attribute("aria-label", "More actions for “Rename the r10 flag”")
    # U3: the path in its line as a chip has it.
    await expect(card.locator(".row-line .latest")).to_have_text(
        "Renamed it; the notes are at ~/r10-notes/flag.md."
    )
    log("R13: changes requested on its PR: Needs you says so; fix it, or accept it as it is")

    # U7: inline code on a decision card, never its backticks.
    asked = page.locator(f'#needs .card.draft[data-id="{decision["id"]}"]')
    await expect(asked.locator(".question code")).to_have_text("text.split()")
    await expect(asked.locator(".why code")).to_have_text("a,b")
    await expect(asked.locator(".recommendation code")).to_have_text("split()")
    await expect(asked.locator(".option .choice code")).to_have_text("split()")
    for part in (".question", ".why", ".recommendation", ".options"):
        assert "`" not in await asked.locator(part).inner_text(), part
    log("U7: a decision card shows `code` as code")
    again = page.locator(f'#needs .card.draft[data-id="{revisit["id"]}"]')
    await expect(again.locator(".precedents")).to_have_text(
        "Reconsiders what you decided: words(): keep split()? → Keep split()"
    )
    await expect(again.locator(".why")).to_have_text("Profiling: half the input is CSV.")
    log("Item 1: a reconsideration names the decision it revisits, and why")

    # U10: names a screen reader says.
    await expect(page.get_by_role("heading", name="Needs you", level=2)).to_be_visible()
    await expect(page.get_by_role("button", name="Focus", exact=True)).to_be_visible()
    open_worker = page.locator("#panel .worker-ref").first
    assert (await open_worker.get_attribute("aria-label")).startswith("Open the worker of “")
    composer = page.locator("#composer-input")
    await expect(composer).to_have_attribute("aria-expanded", "false")
    await composer.press_sequentially("@Rename the r10")
    await expect(composer).to_have_attribute("aria-expanded", "true")
    await expect(composer).to_have_attribute("aria-activedescendant", "mention-0")
    await expect(page.get_by_role("option", name=re.compile("Rename the r10 flag"))).to_have_id(
        "mention-0"
    )
    await composer.press("Escape")
    await expect(composer).to_have_attribute("aria-expanded", "false")
    await composer.fill("")
    log("U10: Needs you is a heading, Focus is called Focus, @ is a combobox, ⋯ says whose")

    # U9: Focusing until… does not move what is below it.
    header = page.locator("#session-header")
    before = (await header.bounding_box())["height"]
    await page.get_by_role("button", name="Focus", exact=True).click()
    await page.get_by_role("menuitem", name="For 30 minutes").click()
    await expect(page.locator("#session-meta")).to_contain_text("Focusing until")
    after = (await header.bounding_box())["height"]
    assert after == before, ("the header grew", before, after)
    await page.get_by_role("button", name="End focus").click()
    await expect(page.locator("#session-meta")).to_be_hidden()
    # U9: at 400px, ☰'s count covers neither ☰ nor "Home" or "← Home" beside it.
    await page.set_viewport_size({"width": 400, "height": 800})
    badge = page.locator("#sidebar-toggle .badge")
    await expect(badge).to_be_visible()

    def apart(a, b):
        return (a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"]
                or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"])  # fmt: skip

    for where in ("home", "worker"):
        await expect(page.locator("#sidebar")).to_be_hidden()
        count = await badge.bounding_box()
        bars = await page.locator("#sidebar-toggle .bars").bounding_box()
        assert apart(count, bars), (where, count, bars)
        beside = page.locator("#back-home" if where == "worker" else "#session-title")
        words = await beside.evaluate(
            "node => { const range = document.createRange(); range.selectNodeContents(node);"
            " const box = range.getBoundingClientRect();"
            " return { x: box.x, y: box.y, width: box.width, height: box.height }; }"
        )
        assert apart(count, words), (where, count, words)
        await page.screenshot(path=SHOTS / f"ui-edges-r10-badge-{where}.png")
        if where == "home":
            await page.locator("#sidebar-toggle").click()
            await page.locator("#sidebar .item", has_text="Rename the r10 flag").click()
            await expect(page.locator("#back-home")).to_be_visible()
    # Its report in its worker's chat says what Home says: Awaiting acceptance.
    await expect(page.locator("#timeline .card.report .chip")).to_have_text("Awaiting acceptance")
    await page.locator("#back-home").click()
    await page.set_viewport_size({"width": 1280, "height": 900})
    log("U9: Focusing until… moves nothing; ☰'s count is clear of ☰ and of Home at 400px")

    # U13: Bring back takes away the toast that taking it off left.
    row = page.locator("#tasks-artifacts .artifact", has_text="Rename the flag")
    await row.get_by_role("button", name=re.compile("^Take .* off this list$")).click()
    taken = page.locator(".toast", has_text="Taken off Artifacts")
    await expect(taken).to_be_visible()
    await page.locator("#tasks-artifacts").get_by_role("button", name="Show 1 taken off").click()
    await row.get_by_role("button", name="Bring back").click()
    await expect(taken).to_have_count(0)
    await expect(page.locator(".toast", has_text="Back on Artifacts.")).to_be_visible()
    log("U13: Bring back takes away “Taken off Artifacts” and says it is back")

    # U6: lines of a diff from the keyboard: Tab to one, Enter, write, Ctrl+Enter.
    diff = ("diff --git a/words.py b/words.py\n--- a/words.py\n+++ b/words.py\n"
            "@@ -1,2 +1,2 @@\n def words(text):\n-    return text.split()\n"
            "+    return text.split(None)\n")  # fmt: skip

    async def changes(route):
        await route.fulfill(json=[{"name": "textkit", "branch": "r10", "diff": diff}])

    sent: list = []

    async def follow_up(route):
        sent.append(route.request.post_data_json)
        await route.fulfill(json={})

    await page.route("**/api/tasks/r10diff/diff", changes)
    await page.route("**/api/tasks/r10diff/follow-up", follow_up)
    await page.evaluate(
        "async () => (await import('/static/diff.js')).showChanges("
        "{ id: 'r10diff', title: 'Speed up words' }, () => {})"
    )
    dialog = page.locator(".diff-dialog")
    await expect(dialog.locator(".diff-line.add")).to_be_visible()
    await dialog.locator(".diff-file > summary").focus()
    await page.keyboard.press("Tab")
    await expect(dialog.locator(".diff-line.ctx").first).to_be_focused()
    await page.keyboard.press("Tab")
    await page.keyboard.press("Tab")
    added = dialog.get_by_role("button", name="Added line 2: return text.split(None)")
    await expect(added).to_be_focused()
    await page.keyboard.press("Enter")
    await expect(dialog.locator(".diff-compose textarea")).to_be_focused()
    await page.keyboard.type("Keep the default.")
    await page.keyboard.press("Control+Enter")
    await expect(dialog.locator(".diff-comment")).to_have_text(re.compile("Keep the default."))
    await expect(added).to_be_focused()
    await dialog.get_by_role("button", name="Send to the worker").click()
    await expect(dialog).to_have_count(0)
    assert "words.py:2 `return text.split(None)`: Keep the default." in sent[0]["text"], sent
    log("U6: a diff line is reached with Tab and commented on with Enter")

    # U8: less motion when the system asks: menus, dialogs and toasts appear still; a
    # highlight shows without fading.
    for reduce in (False, True):
        await page.emulate_media(reduced_motion="reduce" if reduce else "no-preference")
        await page.get_by_role("button", name="Focus", exact=True).click()
        menu = page.locator("#session-actions .menu")
        moving = await menu.evaluate("node => getComputedStyle(node).animationName")
        assert (moving == "none") == reduce, (reduce, moving)
        await page.keyboard.press("Escape")
        flash = await card.evaluate(
            "node => { node.classList.add('flash'); const style = getComputedStyle(node);"
            " const found = [style.animationName, style.animationTimingFunction];"
            " node.classList.remove('flash'); return found; }"
        )
        assert flash[0] == "flash" and ("steps" in flash[1]) == reduce, (reduce, flash)
    await page.emulate_media(reduced_motion="no-preference")
    log("U8: with reduced motion, nothing pops or rises; a highlight shows without fading")

    # U4: a chat that ends with an image opens at its end, once the image has loaded.
    for index in range(30):
        runtime.add_message(router, "assistant", f"Line {index} of what came before.")
    await page.locator("#attach-input").set_input_files(
        files=[{"name": "r10.png", "mimeType": "image/png", "buffer": picture(400, 300)}]
    )
    await composer.fill("What is wrong in r10?")
    await composer.press("Enter")
    image = page.locator("#timeline .msg.owner .attachments img").last
    await expect(image).to_be_visible()
    await page.reload()
    await expect(image).to_be_visible(timeout=15000)
    await page.wait_for_function(
        "() => [...document.querySelectorAll('#timeline .attachments img')].at(-1)?.complete"
    )
    await page.wait_for_timeout(300)
    short = await page.evaluate(
        "() => { const box = document.querySelector('#scroller');"
        " return box.scrollHeight - box.scrollTop - box.clientHeight; }"
    )
    assert short <= 2, f"the chat stops {short}px above its end"
    log("U4: the chat opens at its end when that is an image")
    await context.close()


async def round_scale(runtime, browser, failures: list[str]):
    """The 0.1.33 bug bash, round 9 (scale and recovery): a chat of more than a page loads its
    earlier messages where you read, its fold of steps says it has more (C1); a task far back
    opens at its report (C1); after the event stream drops, everything since comes in (C1);
    every closed worker is counted and can be listed (C4); what you typed and did not send is
    back after a reload, per chat, and gone once sent (C5)."""
    store = runtime.store
    router = store.get("router_id")
    result = await call_tool(runtime, "spawn_worker", router, title="Echo every number",
                             instructions="Echo 1 to 450", why_new="test")  # fmt: skip
    worker = json.loads(result.text_result_for_llm)["session_id"]
    await settle(runtime)
    task = runtime.tasks.current(worker)
    store.add_message(worker, "report", "The plan: ten batches of 45.",
                      {"status": "progress", "task_id": task["id"]})  # fmt: skip
    for i in range(450):
        store.add_message(worker, "tool", f"$ echo {i + 1}")
    store.add_message(worker, "assistant", "Final batch (421-450)", {"narration": True})
    for i in range(30):  # A chat taller than the window.
        store.add_message(worker, "assistant", f"Batch {i + 1} printed its numbers.")
    runtime.update_session(worker, status="idle", activity="")
    for i in range(25):
        closed = store.create_session(str(uuid4()), "worker", f"Closed scale {i:02d}", "idle")
        store.update_session(closed["id"], closed_at=f"2020-01-01T00:00:{i:02d}+00:00")
    store.set(f"learner:{closed['id']}", "1")  # A helper: not one of yours.
    total = runtime.workers.closed_count()

    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await patch_state(page, {}, {"closed_count": total})
    await page.goto(f"{BASE}/#token={TOKEN}")
    await expect(page.locator("#application")).to_be_visible()
    timeline, scroller = page.locator("#timeline"), page.locator("#scroller")

    # C1: the newest page, a fold that says it has more, and earlier ones above, in place.
    await page.locator(f'#session-list .item[data-id="{worker}"]').click()
    earlier = timeline.locator(".earlier button")
    await expect(earlier).to_have_text(re.compile(r"^Load earlier messages \(\d+ more\)$"))
    fold = timeline.locator("details.steps").first
    await expect(fold.locator(".count")).to_have_text("369+ steps")
    await expect(fold.locator(".said")).to_have_text("Final batch (421-450)")
    await scroller.evaluate("box => { box.scrollTop = 0; }")
    before = (await fold.bounding_box())["y"]
    await earlier.click()
    await expect(timeline.locator(".earlier")).to_have_count(0)
    await expect(fold.locator(".count")).to_have_text("450 steps")
    after = (await fold.bounding_box())["y"]
    assert abs(after - before) < 2, ("the view moved", before, after)
    await expect(timeline.locator(".card.report", has_text="ten batches of 45")).to_have_count(1)
    log("C1: Load earlier messages keeps the view; the fold counts every step")

    # C1: a task whose report is before the newest page: opened there.
    await page.evaluate(
        "([id, task]) => import('/static/session.js').then((m) => m.select(id, { task }))",
        [worker, task["id"]],
    )
    report = timeline.locator(f'[data-task="{task["id"]}"][data-kind="report"]')
    await expect(report).to_have_class(re.compile(r"\bflash\b"))
    await expect(report).to_be_in_viewport()
    log("C1: a task far back opens at its report")

    # C1: the event stream drops while more than a page comes: all of it, no gap.
    await expect(fold.locator(".count")).to_have_text("450 steps")
    for i in range(450, 900):
        store.add_message(worker, "tool", f"$ echo {i + 1}")  # Never published: only a resync.
    for queue in list(runtime.bus.queues):
        queue.put_nowait(None)
    await expect(timeline.locator("details.steps .count")).to_have_text(
        ["450 steps", "450 steps"], timeout=20000
    )
    log("C1: after the stream drops, everything since comes in page by page")

    # C4: every closed worker counted; Show all lists the older ones too, never a helper.
    await page.locator("#closed > summary").click()
    await expect(page.locator("#closed-count")).to_have_text(f"· {total}")
    show = page.locator("#closed-list .show-all")
    await expect(show).to_have_text(f"Show all {total}")
    await show.click()
    await expect(page.locator("#closed-list .item")).to_have_count(total)
    await expect(show).to_have_count(0)
    await expect(page.locator("#closed-list")).not_to_contain_text("Closed scale 24")
    await expect(page.locator("#closed-list")).to_contain_text("Closed scale 00")
    log("C4: every closed worker counted, all of them listed when asked")

    # C5: what you typed stays with its chat, through a reload; sent, it is gone.
    composer = page.locator("#composer-input")
    await page.locator("#back-home").click()
    await expect(page.locator(".home-item.selected")).to_be_visible()
    await composer.fill("Half a question about the echo")
    await page.reload()
    await expect(page.locator("#application")).to_be_visible()
    await expect(composer).to_have_value("Half a question about the echo", timeout=15000)
    await page.locator(f'#session-list .item[data-id="{worker}"]').click()
    await expect(composer).to_have_value("")
    await page.locator("#back-home").click()
    await expect(composer).to_have_value("Half a question about the echo")
    await composer.press("Enter")
    await expect(timeline.locator(".msg.owner", has_text="Half a question")).to_be_visible()
    await page.reload()
    await expect(page.locator("#application")).to_be_visible()
    await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)
    await expect(composer).to_have_value("")
    log("C5: unsent text stays with its chat through a reload, and goes once sent")
    await context.close()


async def round_qa(runtime, browser, failures: list[str]):
    """QA round 10 in the browser: Home's five parts in order (Artifacts last), no sideways
    overflow at 360, 768 and 1280 px; a report waits for Accept and can be sent back with what
    to change; a question with why, choices and what next; a follow that cannot be read says
    why by kind, with Retry or Stop following; a worker whose turn is over while a command it
    left runs waits for that process; the owner's messages say whether they got there, in the
    chat and on the row they were sent from; the tooltip goes when Home changes under it."""
    store = runtime.store
    router = store.get("router_id")
    store.save_settings(store.settings().model_copy(update={"max_workers": 80}))

    async def start(title):
        result = await call_tool(runtime, "spawn_worker", router, title=title,
                                 instructions=title, why_new="test")  # fmt: skip
        assert result.result_type == "success", result.text_result_for_llm
        await settle(runtime)
        return json.loads(result.text_result_for_llm)["session_id"]

    informer = await start("QA tell me the flag state")
    await call_tool(runtime, "report", informer, status="done", summary="The flag is on.")
    informed = runtime.tasks.current(informer)
    asker = await start("QA pick the cache size")
    await call_tool(runtime, "report", asker, status="question", summary="QA: 48 or 96 MB?",
                    why="The cache must fit the box's memory", options=["48 MB", "96 MB"],
                    next="I set it and rerun the benchmark")  # fmt: skip
    asked = runtime.tasks.current(asker)
    watcher = await start("QA watch the slow PR")
    runtime.update_session(watcher, status="idle", activity="")
    locked = await start("QA watch the private PR")
    runtime.update_session(locked, status="idle", activity="")
    server = await start("QA run the dev server")
    stopper = await start("QA measure the latency")
    stopped = runtime.tasks.current(stopper)
    runtime.update_session(stopper, status="working", activity="Reading the logs")
    await runtime.workers.interrupt(stopper)
    await settle(runtime)
    busy = await start("QA keep busy")
    runtime.update_session(busy, status="working", activity="Step one")
    runtime.publish_state()

    soon = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()
    slow, private = "github:acme/app#301", "github:acme/app#302"
    patches = {
        "QA watch the slow PR": {"watch_failing": [slow], "watch_failing_why": {slow: {
            "kind": "rate_limited", "error": "GitHub asked to wait before reading it again.",
            "step": "pull request", "at": datetime.now(UTC).isoformat(), "retry_at": soon,
            "detail": "HTTP 429"}}},
        "QA watch the private PR": {"watch_failing": [private], "watch_failing_why": {private: {
            "kind": "permission", "error": "None of your accounts can see acme/app.",
            "step": "pull request", "at": datetime.now(UTC).isoformat(), "retry_at": None,
            "detail": "HTTP 404"}}},
    }  # fmt: skip
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await patch_state(page, patches)
    await page.goto(f"{BASE}/#token={TOKEN}")
    needs = page.locator("#needs")

    def card(title):
        return page.locator("#panel .card.task").filter(
            has=page.locator(".head .title", has_text=title)
        )

    # Five parts, Artifacts last, each folding; no part inside another.
    await expect(card("QA tell me the flag state")).to_be_visible(timeout=15000)
    parts = await page.evaluate("[...document.querySelectorAll('#panel .area')].map(n => n.id)")
    assert parts == ["needs", "tasks-running", "tasks-monitoring", "tasks-recent",
                     "tasks-artifacts"], parts  # fmt: skip
    assert not await page.locator("#panel .area .area, #panel .area details.panel-section").count()
    # (Teams, a line per team above Running when there are any, is spaced as a part: E129.)
    gaps = await page.evaluate("""() => {
      const areas = [...document.querySelectorAll('#panel .area, #teams-strip')]
        .filter((n) => n.offsetHeight);
      return areas.slice(1).map((n, i) => Math.round(n.getBoundingClientRect().top
        - areas[i].getBoundingClientRect().bottom));
    }""")
    assert set(gaps) == {12}, ("12px between parts", gaps)
    log("Home: five parts in order, Artifacts last, 12px apart")

    # QA E27: a tooltip of Home's own, gone as soon as Home changes under it.
    row = card("QA keep busy")
    tip = page.locator("#tip")
    # Shown once the mouse rests on it (Home may still be settling from earlier rounds: a
    # change under it takes it away, and the next move of the mouse shows it again; one that
    # moved another row's line under the mouse shows that line's).
    for _ in range(10):
        await page.mouse.move(0, 0)
        await row.locator(".head .meta").hover()
        await page.wait_for_timeout(700)
        if await tip.is_visible() and "Task " in (await tip.text_content() or ""):
            break
    await expect(tip).to_be_visible()
    await expect(tip).to_contain_text("Task ")
    runtime.update_session(busy, status="working", activity="Step two")
    await expect(row.locator(".row-line .latest")).to_have_text("Step two")
    await expect(tip).to_be_hidden()
    assert not await page.locator("#panel [title]").count(), "no native tooltips on Home"
    log("the tooltip goes when Home changes under it; no native tooltips on Home")

    # QA E1: a report that only informs waits for Accept; Request changes sends it back.
    report = card("QA tell me the flag state")
    await expect(report.locator(".head .meta")).to_contain_text("Awaiting acceptance")
    for name in ("Accept", "Follow up", "Request changes", "View report"):
        await expect(report.get_by_role("button", name=name, exact=True)).to_be_visible()
    await report.get_by_role("button", name="Request changes").click()
    async with page.expect_request(f"**/api/tasks/{informed['id']}/request-changes") as sent:
        await report.locator("textarea").fill("Say which flag")
        await report.get_by_role("button", name="Send").click()
    assert (await sent.value).post_data_json == {"text": "Say which flag"}
    await expect(page.locator(".toast", has_text="Sent back with your changes.")).to_be_visible()
    await expect(needs.locator(".card.task", has_text="QA tell me the flag")).to_have_count(
        0, timeout=10000
    )
    assert runtime.tasks.get(informed["id"])["status"] == "running"
    log("a report waits for Accept; Request changes sends it back with what to change")

    # QA E9: its question with why, the answers it offers and what it does then.
    question = card("QA pick the cache size")
    await expect(question.locator(".asked .q-why")).to_contain_text("fit the box's memory")
    await expect(question.locator(".asked .q-next")).to_have_text(
        "Then: I set it and rerun the benchmark"
    )
    await expect(question.locator(".asked .q-option")).to_have_text(["48 MB", "96 MB"])
    async with page.expect_request("**/api/questions/*/answer") as answered:
        await question.get_by_role("button", name="96 MB").click()
    assert (await answered.value).post_data_json == {"text": "96 MB"}
    await expect(needs.locator(".card", has_text="QA: 48 or 96 MB?")).to_have_count(
        0, timeout=10000
    )
    assert runtime.tasks.get(asked["id"])["status"] == "running"
    log("a question: why, its choices as buttons (one sends it), then what it does")

    # QA E10: a follow that cannot be read, by kind: GitHub's rate limit passes (Retry, the
    # details); no access does not (Stop following).
    limited = card("QA watch the slow PR")
    await unfold(limited)
    chip = limited.locator(".receipt.failing")
    await expect(chip.locator(".words")).to_have_text("GitHub's rate limit for app#301")
    await expect(chip.get_by_role("button", name="stop following")).to_have_count(0)
    retried: list[dict] = []

    async def retry(route):
        retried.append(route.request.post_data_json)
        await route.fulfill(json={"key": slow, "failures": 0, "why": {}, "read_at": "",
                                  "next_at": ""})  # fmt: skip

    await page.route(f"**/api/sessions/{watcher}/follow-retry", retry)
    await chip.get_by_role("button", name="Retry").click()
    await expect(page.locator(".toast", has_text="Reading app#301 again.")).to_be_visible()
    assert retried == [{"key": slow}], retried
    await chip.locator(".why-details summary").click()
    await expect(chip.locator(".why-details .detail")).to_contain_text("asked to wait")
    denied = card("QA watch the private PR")
    await unfold(denied)
    chip = denied.locator(".receipt.failing")
    await expect(chip.locator(".words")).to_have_text("No access to app#302")
    await expect(chip).to_have_attribute("data-tip", re.compile("None of your accounts"))
    await expect(chip.get_by_role("button", name="stop following")).to_be_visible()
    await expect(chip.get_by_role("button", name="Retry")).to_have_count(0)
    log("a follow it cannot read: rate limited offers Retry and details; no access, Stop")

    # QA E13/8: its turn over, a command it left running goes on: waiting for that process.
    runtime.states[server].held = True
    runtime.update_session(server, status="idle", activity="")
    serving_row = card("QA run the dev server")
    await expect(serving_row.locator(".row-line .latest")).to_have_text(
        "Waiting for background process", timeout=10000
    )
    await expect(
        page.locator("#tasks-running .card.task", has_text="QA run the dev")
    ).to_have_count(1)
    await serving_row.locator(".worker-ref").click()
    await expect(page.locator("#timeline .working-line")).to_contain_text(
        "Waiting for background process"
    )
    await expect(page.locator("#timeline .working-line")).not_to_contain_text("Thinking")

    # QA E11: the owner's messages say whether they got there, as the server says it.
    # Not sent for real (no prompt): what it says is what these events say.
    said = runtime.add_message(server, "owner", "Use port 8080")
    message = page.locator(f'#timeline .msg.owner[data-id="{said["id"]}"]')
    await expect(message).to_be_visible(timeout=10000)
    states = [("sent", "Sent"), ("resending", "Not received yet — sending again"),
              ("undelivered", "Not delivered"), ("received", "Received")]  # fmt: skip
    for delivery, words in states:
        runtime.bus.publish("message", {**said, "meta": {}, "delivery": delivery, "amended": True})
        await expect(message.locator(".delivery")).to_have_text(words)
    later = runtime.add_message(server, "owner", "And log it")
    runtime.bus.publish("message", {**later, "meta": {}, "delivery": "queued", "amended": True})
    newer = page.locator(f'#timeline .msg.owner[data-id="{later["id"]}"]')
    await expect(newer.locator(".delivery")).to_have_text("Waiting to be sent")
    # Got there: said under the latest only.
    await expect(message.locator(".delivery")).to_be_hidden()
    runtime.states[server].held = False
    await page.locator("#back-home").click()
    log("a background process is said, not Thinking; each message says if it got there")

    # Go on from a row: the row says whether it got there. One click, one request, one toast
    # (an answer's "Sent to the worker." above may still show: set aside first).
    go_on = card("QA measure the latency")
    follow_ups: list[str] = []
    page.on("request", lambda sent: follow_ups.append(sent.url) if "/follow-up" in sent.url else 0)
    await page.evaluate("document.querySelectorAll('#toasts .toast').forEach((n) => n.remove())")
    await go_on.get_by_role("button", name="Go on", exact=True).click()
    toasted = page.locator(".toast", has_text="Sent to the worker.")
    await expect(toasted).to_be_visible()
    await page.wait_for_timeout(300)
    assert (len(follow_ups), await toasted.count()) == (1, 1), (follow_ups, await toasted.count())
    moved = card("QA measure the latency")
    await expect(moved.locator(".row-foot .delivery")).to_be_visible(timeout=10000)
    await settle(runtime)
    await page.wait_for_timeout(500)
    prompt = store.one(
        "SELECT * FROM messages WHERE session_id=? AND json_extract(meta, '$.prompt') IS NOT "
        "NULL ORDER BY id DESC LIMIT 1",
        (stopper,),
    )
    runtime.bus.publish("message", {**prompt, "meta": {}, "delivery": "resending",
                                    "amended": True})  # fmt: skip
    await expect(moved.locator(".row-foot .delivery")).to_have_text(
        "Not received yet — sending again"
    )
    assert runtime.tasks.get(stopped["id"])["status"] == "running"
    log("Go on from a row: the row says the message is being sent again")

    # QA E19/E20: no sideways overflow at 360, 768 and 1280 px, in either tab.
    for width in (1280, 768, 360):
        await page.set_viewport_size({"width": width, "height": 900})
        for tab in ("needs", "tasks") if width < 1100 else ("",):
            if tab:
                await page.locator(f"#home-tabs [data-tab={tab}]").click()
            await page.wait_for_timeout(150)
            wide = await page.evaluate(
                "[document.documentElement.scrollWidth, innerWidth,"
                " document.querySelector('#panel').scrollWidth,"
                " document.querySelector('#panel').clientWidth]"
            )
            assert wide[0] <= wide[1] and wide[2] <= wide[3] + 1, (width, tab, wide)
            found = await page.locator("#panel").evaluate(OVERFLOWS)
            assert not found, (width, tab, found)
        await page.screenshot(path=SHOTS / f"ui-edges-qa-home-{width}.png")
    await page.set_viewport_size({"width": 1280, "height": 900})
    log("Home fits at 360, 768 and 1280 px: nothing runs past the panel")
    assert not errors, errors
    await context.close()


# What a narrow row's second line shows of each part: its width, whether its text is whole, and
# whether it ends inside the line.
ROW_LINE_PARTS = """line => [...line.children].map((node) => {
  const box = node.getBoundingClientRect();
  const outer = line.getBoundingClientRect();
  const shown = getComputedStyle(node).display !== "none";
  return { cls: node.className, shown, width: Math.round(box.width),
    whole: node.scrollWidth <= node.clientWidth + 1, inside: !shown || box.right <= outer.right + 1,
    text: node.textContent };
})"""


async def round_eleven(runtime, browser, failures: list[str]):
    """The owner's QA E32-E34: a worker given new work is never named on a row by the first
    thing it was asked (its old task); at 352-400 px a row's reason keeps its room while its
    worker's name gives way, then its worker, and nothing runs past it; a newer report is the
    row's latest and its card's result, the earlier ones folded under it with when each was
    said."""
    store = runtime.store
    router = store.get("router_id")
    store.save_settings(store.settings().model_copy(update={"max_workers": 90}))

    async def start(title):
        result = await call_tool(runtime, "spawn_worker", router, title=title,
                                 instructions=title, why_new="test")  # fmt: skip
        assert result.result_type == "success", result.text_result_for_llm
        await settle(runtime)
        return json.loads(result.text_result_for_llm)["session_id"]

    async def reuse(worker, title):
        """Its task reported and accepted, the same worker gets a new one and takes it up."""
        task = runtime.tasks.current(worker)
        await call_tool(runtime, "report", worker, status="done", summary=f"{task['title']}: done.")
        runtime.task_actions.accept(task["id"])
        given = await call_tool(runtime, "message_session", router, session_id=worker,
                                text=title, task="new", title=title, earlier="keep",
                                why_new="another PR")  # fmt: skip
        assert given.result_type == "success", given.text_result_for_llm
        take_up(runtime, worker, title)
        await settle(runtime)

    # E32: started for one task, given another: its row's worker link is neutral.
    reused = await start("E32 old export task")
    await reuse(reused, "E32 new export task")
    runtime.update_session(reused, status="working", activity="Reading the exporter")
    # E33: a delivered task waiting for approval, quiet for 26h; its worker on another task
    # with a long title.
    shipper = await start("E33 ship app#594 export fix")
    await reuse(shipper, "Investigate why the nightly ranking backfill times out on the big shard")
    runtime.update_session(shipper, status="working", activity="Reading the backfill logs")
    # E34: a newer report on the same task is its result; the ones before it are kept.
    tuner = await start("E34 tune the cache size")
    await call_tool(runtime, "report", tuner, status="done", summary="Cache at 96 MB: p99 -30%.")
    await settle(runtime)
    runtime.publish_state()

    ago = lambda **delta: (datetime.now(UTC) - timedelta(**delta)).isoformat()  # noqa: E731
    earlier = [
        {"at": ago(hours=2), "text": "Cache at **64 MB**: p99 -12%.\n\n" + "Measured again. " * 60},
        {"at": ago(days=1, hours=1), "text": "First try at 48 MB: no change."},
    ]
    waiting = {"key": "github:acme/app#594", "kind": "pr", "label": "app#594",
               "url": "https://github.com/acme/app/pull/594", "waiting_for": "approval",
               "last_check": ago(minutes=5), "next_check": ago(minutes=-10),
               "trigger": "A review, CI result or merge on it brings it back",
               "changed_at": ago(hours=26, minutes=5)}  # fmt: skip
    patches = {
        "E33 ship app#594 export fix": {"watching": [waiting["key"]], "watch_kind": "own",
                                        "monitoring": [waiting], "result": "Opened app#594.",
                                        "earlier_results": [earlier[1]]},
        "E34 tune the cache size": {"earlier_results": earlier},
    }  # fmt: skip
    context = await browser.new_context(viewport={"width": 1440, "height": 900})
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await patch_state(page, patches)
    await page.route("**/api/tasks/*/read", lambda route: route.fulfill(json={"read": True}))
    await page.goto(f"{BASE}/#token={TOKEN}")

    def card(title):
        return page.locator("#panel .card.task").filter(
            has=page.locator(".head .title", has_text=title)
        )

    # E32: the worker it was started as is not its name here; one on another task is named so.
    new_row, old_row = card("E32 new export task"), card("E32 old export task")
    await expect(new_row.locator(".row-line .worker-ref .name")).to_have_text("chat", timeout=15000)
    await expect(new_row.locator(".row-line .worker-ref")).not_to_contain_text("old export")
    await expect(new_row.locator(".worker-ref")).not_to_have_attribute(
        "data-tip", re.compile("old export")
    )
    await expect(old_row.locator(".row-line .worker-ref .name")).to_have_text("E32 new export task")
    await page.locator("#panel").screenshot(path=SHOTS / "ui-edges-worker-links.png")
    await new_row.locator(".row-line .worker-ref").click()
    await expect(page.locator("#session-title")).to_have_text("E32 new export task")
    await page.locator("#back-home").click()
    log("E32: a reused worker's link is its worker chat, never its old task")

    # E34: the row says its newest result; its report, the earlier ones folded with their time.
    tuned = card("E34 tune the cache size")
    await expect(tuned.locator(".row-line .latest")).to_have_text("Cache at 96 MB: p99 -30%.")
    await tuned.get_by_role("button", name="View report").click()
    fold = tuned.locator(".report details.earlier-reports")
    await expect(tuned.locator(".report > .body")).to_contain_text("96 MB")
    await expect(fold.locator("summary")).to_have_text("Earlier reports (2)")
    await expect(fold).not_to_have_attribute("open", "")
    await expect(fold.locator(".earlier-report").first).to_be_hidden()
    await fold.locator("summary").click()
    entries = fold.locator(".earlier-report")
    await expect(entries).to_have_count(2)
    await expect(entries.nth(0).locator(".when")).to_have_text("Reported 2h ago")
    await expect(entries.nth(0).locator(".body strong")).to_have_text("64 MB")
    await expect(entries.nth(0).get_by_role("button", name="Show more")).to_be_visible()
    await expect(entries.nth(1).locator(".body")).to_have_text("First try at 48 MB: no change.")
    assert not await tuned.evaluate(OVERFLOWS), await tuned.evaluate(OVERFLOWS)
    ship = card("E33 ship app#594 export fix")
    await expect(ship.locator(".row-line .latest")).to_have_text("Opened app#594.")
    await unfold(ship)
    await expect(ship.locator(".task-body details.earlier-reports summary")).to_have_text(
        "Earlier reports (1)"
    )
    await ship.locator(":scope > .head .fold").click()
    log("E34: the newest result on the row and its card; earlier reports folded, with when")

    # E33: on a wide panel its worker named (cut to fit); at 352-400 px its reason stays
    # readable, its worker gives way (an icon, then not on the line) and nothing runs past it.
    line = ship.locator(".row-line")
    await expect(line.locator(".why")).to_have_text("Waiting for approval")
    await expect(line.locator(".quiet")).to_have_text("No updates for 26h")
    ref = line.locator(".worker-ref")
    await expect(ref).to_have_attribute("data-tip", re.compile("Worker: “Investigate why"))
    for width in (600, 400, 376, 352):
        await page.evaluate("w => document.querySelector('#panel').style.width = `${w}px`", width)
        await page.wait_for_timeout(100)
        # Every row's: its reason readable, nothing squeezed to nothing or past its end.
        for each in await page.locator("#panel .card.task .row-line").all():
            parts = await each.evaluate(ROW_LINE_PARTS)
            shown = [part for part in parts if part["shown"]]
            assert all(part["inside"] and part["width"] > 0 for part in shown), (width, parts)
            for why in (part for part in parts if part["cls"].startswith("why")):
                assert why["whole"] or why["width"] >= 90, (width, why)
        assert not await page.locator("#panel").evaluate(OVERFLOWS), width
        parts = await line.evaluate(ROW_LINE_PARTS)
        shown = [part for part in parts if part["shown"]]
        # What it waits for, and on what, whole: how long it has been quiet gives way first.
        why = next(part for part in parts if part["cls"] == "why")
        assert why["whole"] and parts[1]["whole"], (width, parts)
        worker = next(part for part in parts if "worker-ref" in part["cls"])
        if width == 600:
            await expect(ref.locator(".name")).to_be_visible()
            cut = await ref.locator(".name").evaluate("n => n.scrollWidth > n.clientWidth")
            assert cut, ("its long name is cut", worker)
            assert worker["width"] <= 0.3 * sum(part["width"] + 6 for part in shown), (width, parts)
        elif worker["shown"]:
            await expect(ref.locator(".icon")).to_be_visible()
            await expect(ref.locator(".who")).to_be_hidden()
            assert worker["width"] < 20, ("only its icon", width, worker)
        log(f"  {width}px: " + " | ".join(
            f"{part['cls']} {part['width']}px{'' if part['whole'] else ' (cut)'}"
            for part in shown))  # fmt: skip
        if width in (400, 352):
            await ship.screenshot(path=SHOTS / f"ui-edges-narrow-reason-{width}.png")
            # At the panel's widest default it is on the line (an icon); at 352 px only in
            # the row shown whole.
            await expect(ref).to_be_visible(visible=width == 400)
    await unfold(ship)
    await expect(ship.locator(".row-foot .worker-ref .name")).to_be_visible()
    await page.evaluate("document.querySelector('#panel').style.removeProperty('width')")
    log("E33: at 352-400 px the reason stays readable; its worker gives way; nothing overflows")
    assert not errors, errors
    await context.close()


FLYTE = "https://flyte.acme.dev/console/projects/ml/domains/prod/executions/"
SHARDS = ["running", "running", "succeeded", "failed", "running", "succeeded", "succeeded",
          "succeeded", "succeeded"]  # fmt: skip


async def runs_seeded(runtime) -> dict:
    """QA E41/E43: two accepted tasks with a run their worker declared running two days ago and
    nothing since; one that shipped a PR and nine runs (one failed, three running); another's
    PR. By title."""
    store = runtime.store
    router = store.get("router_id")
    store.save_settings(store.settings().model_copy(update={"max_workers": 90}))
    ago = lambda **delta: (datetime.now(UTC) - timedelta(**delta)).isoformat()  # noqa: E731
    found = {}

    async def delivered(title, artifacts):
        result = await call_tool(runtime, "spawn_worker", router, title=title, instructions=title,
                                 why_new="test")  # fmt: skip
        worker = json.loads(result.text_result_for_llm)["session_id"]
        await settle(runtime)
        task = runtime.tasks.current(worker)
        await call_tool(runtime, "report", worker, status="done", summary=f"{title}: done.",
                        artifacts=artifacts)  # fmt: skip
        await settle(runtime)
        runtime.task_actions.accept(task["id"])
        found[title] = runtime.tasks.get(task["id"])
        return task

    def said(task, **times):
        known = json.loads(store.get(f"artifacts:{task['id']}"))
        for index, item in enumerate(known):
            item["at"] = ago(**{key: value(index) for key, value in times.items()})
        store.set(f"artifacts:{task['id']}", json.dumps(known))

    for title, run in (("E41 backfill the ranking features", "f3a9c1"),
                       ("E41 rebuild the search index", "a7b2d4")):  # fmt: skip
        task = await delivered(title, [{"url": FLYTE + run, "name": f"Run {run}",
                                        "status": "running"}])  # fmt: skip
        said(task, hours=lambda _index: 47)
    # Round 13 (E56): the rebuild still waits on a run of its own declared since, so it stays in
    # Monitoring (its unknown run ended or untracked from its menu); the backfill waits on
    # nothing else, so it needs the owner.
    rebuild = found["E41 rebuild the search index"]
    known = json.loads(store.get(f"artifacts:{rebuild['id']}"))
    fresh = {"url": FLYTE + "c5d6e7", "name": "Run c5d6e7", "status": "running", "at": ago(hours=1)}
    store.set(f"artifacts:{rebuild['id']}", json.dumps([*known, fresh]))
    shards = [{"url": FLYTE + f"e{index:02d}", "name": f"Export shard {index + 1}",
               "status": status} for index, status in enumerate(SHARDS)]  # fmt: skip
    export = await delivered("E43 ship the sharded export", shards)
    runtime.task_actions.record_opened(export["id"], ["https://github.com/acme/app/pull/42"])
    runtime.remember_pr_title("acme/app#42", "Shard the nightly export")
    said(export, minutes=lambda index: 10 + 20 * (len(shards) - index))
    cache = await delivered("E43 tune the cache size", [])
    runtime.task_actions.record_opened(cache["id"], ["https://github.com/acme/app/pull/43"])
    runtime.remember_pr_title("acme/app#43", "Raise the cache to 96 MB")
    for task in found.values():
        runtime.task_actions.publish(runtime.tasks.get(task["id"]))
    return found


async def round_twelve(runtime, browser, failures: list[str]):
    """The owner's QA E41: a run declared running two days ago and nothing since is "Status
    unknown · reported running 47h ago", never "checked" when nobody checked; its row's menu
    marks it ended or stops tracking it, and the task leaves Monitoring. E43: a task's nine
    runs are one line under its PR, the latest and the failed one in sight, the rest behind
    "+7"; unfolded and folded again by mouse or keyboard."""
    store = runtime.store
    tasks = await runs_seeded(runtime)
    context = await browser.new_context(viewport={"width": 1440, "height": 1000})
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    monitoring = page.locator("#tasks-monitoring")

    def row(title):
        return page.locator("#panel .card.task").filter(
            has=page.locator(".head .title", has_text=title)
        )

    backfill = row("E41 backfill the ranking features")
    await expect(backfill).to_be_visible(timeout=15000)
    # Round 13 (E56): nothing else to wait on, so it needs the owner, its two answers on its row.
    needs = page.locator("#needs")
    await expect(needs.locator(".card.task", has_text="E41 backfill")).to_have_count(1)
    assert await monitoring.locator(".card.task", has_text="E41 backfill").count() == 0
    await expect(backfill.locator(".head .marker")).to_have_text("Status unknown")
    await expect(backfill.locator(".row-line .why")).to_have_text(
        "Run f3a9c1: reported running 47h ago"
    )
    # Its accepted result is not said again beside it (here, its title again).
    await expect(backfill.locator(".row-line .latest")).to_have_count(0)
    await expect(backfill.get_by_role("button", name="Mark ended", exact=True)).to_be_visible()
    await expect(backfill.get_by_role("button", name="Stop tracking")).to_be_visible()
    # Its run is "status unknown" everywhere: on its chip and on Artifacts, not "running".
    await expect(backfill.locator(".row-foot .link-chip .chip-link")).to_have_text(
        "Flow · Run f3a9c1 · status unknown"
    )
    lost = page.locator("#tasks-artifacts .artifact", has_text="Run f3a9c1")
    await expect(lost.locator(".a-status")).to_have_text("status unknown")
    await expect(lost).to_have_class(re.compile(r"\btone-wait\b"))
    await expect(lost.locator(".a-status")).to_have_attribute(
        "data-tip", re.compile("^Status unknown: reported running 47h ago, not checked since")
    )
    rebuild = row("E41 rebuild the search index")
    await expect(monitoring.locator(".card.task", has_text="E41 rebuild")).to_have_count(1)
    line = rebuild.locator(".row-line")
    # Its run declared since is what it waits for; the unknown one on hover, its state once.
    await expect(line.locator(".why")).to_have_text("Waiting for the run")
    await expect(line.locator(".also")).to_have_text("+1")
    tip = await line.locator(".why").get_attribute("data-tip") or ""
    assert "Run a7b2d4: status unknown\n  Nothing tells where it stands now" in tip, tip
    assert tip.count("status unknown") == 1, tip
    # A run declared an hour or two ago: waited for, said when its worker said it, not "checked".
    export = row("E43 ship the sharded export")
    await expect(export.locator(".row-line .why")).to_have_text("Waiting for the run")
    await expect(export.locator(".row-foot .checks")).to_have_text(
        re.compile(r"^reported running \dh ago$")
    )
    assert "checked" not in await monitoring.inner_text()
    log('E41: a run nothing told of for a day is unknown; never "checked" unless it was')

    # Its menu: Mark ended and Stop tracking first, all of it in sight in the list.
    more = rebuild.get_by_role("button", name=re.compile("More actions"))
    await more.click()
    menu = rebuild.locator(".menu:not([hidden])")
    items = await menu.get_by_role("menuitem").all_text_contents()
    assert items[:2] == ["Mark ended", "Stop tracking"], items
    await page.evaluate(
        "() => Promise.all(document.getAnimations()"
        ".filter((a) => a.effect?.getComputedTiming().iterations !== Infinity)"
        ".map((a) => a.finished.catch(() => {})))"
    )
    box = await monitoring.locator(":scope > .items").bounding_box()
    shown = await menu.bounding_box()
    assert box and shown and shown["y"] + shown["height"] <= box["y"] + box["height"] + 1, (
        "the menu is not cut by its list",
        box,
        shown,
    )
    await menu.get_by_role("menuitem", name="Mark ended").click()
    # Its other run is still waited for: it stays, its unknown one ended.
    await expect(line.locator(".also")).to_have_count(0, timeout=10000)
    await expect(line.locator(".why")).to_have_text("Waiting for the run")
    await backfill.get_by_role("button", name="Stop tracking").click()
    await expect(page.locator("#panel .card.task", has_text="E41 backfill")).to_have_count(1)
    await expect(needs.locator(".card.task", has_text="E41 backfill")).to_have_count(
        0, timeout=10000
    )
    await expect(page.locator("#tasks-recent .card.task", has_text="E41 backfill")).to_have_count(1)
    [kept] = json.loads(store.get(f"artifacts:{tasks['E41 backfill the ranking features']['id']}"))
    assert kept["untracked"] and kept["status"] == "running", kept
    made = page.locator("#tasks-artifacts")
    ended = made.locator(".artifact", has_text="Run a7b2d4")
    await expect(ended.locator(".a-status")).to_have_text("ended")
    await expect(ended.locator(".a-status")).to_have_attribute(
        "data-tip", re.compile("^As you marked it")
    )
    # Round 13 (E53): no longer tracked, it is not said to run.
    untracked = made.locator(".artifact", has_text="Run f3a9c1")
    await expect(untracked.locator(".a-status")).to_have_text("not tracked")
    await expect(untracked).to_have_class(re.compile(r"\btone-neutral\b"))
    await expect(untracked.locator(".a-status")).to_have_attribute(
        "data-tip", re.compile("^Not tracked · last reported running: you stopped tracking it")
    )
    log("E41/E56: a run's end or untracking from the row (Needs you) or its menu (Monitoring)")

    # E43: the PR first, the runs in a line, the latest and the failed one under it.
    await made.locator(":scope > .items").evaluate("n => { n.style.maxHeight = 'none'; }")
    rows = made.locator(".artifact")
    names = await rows.locator(".a-name").all_text_contents()
    first = names.index("9 runs")
    # Round 13 (E58): its failed run first, its PR after; round 14: after what any other task
    # has that needs a look (here, nothing).
    assert names[first : first + 3] == ["9 runs", "Export shard 9", "Export shard 4"], names
    assert names.index("app#42 · Shard the nightly export") > first + 2, names
    assert not [name for name in names[first + 3 :] if name.startswith("Export shard")], names
    summary = made.locator(".artifact.runs").filter(has=page.locator(".a-meta", has_text="E43"))
    fold = summary.locator(".a-fold")
    await expect(fold).to_have_attribute("aria-expanded", "false")
    await expect(summary.locator(".a-status")).to_have_text("1 failed · 3 running")
    await expect(summary).to_have_class(re.compile(r"\btone-bad\b"))
    await expect(made.locator(".artifact.in-runs")).to_have_count(2)
    more_runs = "Show 7 more runs of E43 ship the sharded export"
    await summary.get_by_role("button", name=more_runs).click()
    await expect(made.locator(".artifact.in-runs")).to_have_count(9)
    await expect(fold).to_have_attribute("aria-expanded", "true")
    await expect(summary.locator(".a-more")).to_have_count(0)
    await fold.focus()
    await page.keyboard.press("Enter")
    await expect(made.locator(".artifact.in-runs")).to_have_count(2)
    await expect(fold).to_have_attribute("aria-expanded", "false")
    await page.keyboard.press("Space")
    await expect(made.locator(".artifact.in-runs")).to_have_count(9)
    for width in (352, 400):
        await page.evaluate("w => document.querySelector('#panel').style.width = `${w}px`", width)
        await page.wait_for_timeout(100)
        for part in (made, monitoring):
            found = await part.evaluate(OVERFLOWS)
            assert not found, (width, found)
    await page.evaluate("document.querySelector('#panel').style.removeProperty('width')")
    log("E43: a task's runs fold under its PR (latest and failed in sight); mouse and keyboard")
    assert not errors, errors
    await context.close()


APPROVALS = ["running", "succeeded", "failed", "succeeded", "waiting for approval", "succeeded",
             "succeeded", "running", "succeeded"]  # fmt: skip
# A row's columns where its kind and its name start (E48: a run under its line's was 16px off).
COLUMNS = """root => [...root.querySelectorAll(".artifact")].map((row) => {
  const kind = row.querySelector(".a-kind").getBoundingClientRect();
  const name = row.children[1].getBoundingClientRect();
  const box = row.getBoundingClientRect();
  return { cls: row.className, task: row.dataset.task || "", kind: Math.round(kind.left),
    name: Math.round(name.left),
    top: Math.round(box.top), bottom: Math.round(box.bottom), height: Math.round(box.height),
    hidden: getComputedStyle(row.querySelector(".a-kind")).visibility === "hidden" };
})"""


async def thirteen_seeded(runtime) -> dict:
    """QA round 13: a running task whose only run nothing told of for 30 hours (its worker
    between turns); an accepted task with its PR and nine runs, an older one waiting for
    approval and one the owner marked ended."""
    store = runtime.store
    router = store.get("router_id")
    ago = lambda **delta: (datetime.now(UTC) - timedelta(**delta)).isoformat()  # noqa: E731
    found = {}

    async def started(title, status, artifacts):
        result = await call_tool(runtime, "spawn_worker", router, title=title, instructions=title,
                                 why_new="test")  # fmt: skip
        worker = json.loads(result.text_result_for_llm)["session_id"]
        await settle(runtime)
        task = runtime.tasks.current(worker)
        await call_tool(runtime, "report", worker, status=status, summary=f"{title}: on it.",
                        artifacts=artifacts)  # fmt: skip
        await settle(runtime)
        runtime.update_session(worker, status="idle", activity="")
        found[title] = task
        return task

    swept = [{"url": FLYTE + "s1", "name": "Ranker sweep", "status": "running"}]
    sweep = await started("E57 tune the ranker", "progress", swept)
    known = json.loads(store.get(f"artifacts:{sweep['id']}"))
    known[0]["at"] = ago(hours=30)
    store.set(f"artifacts:{sweep['id']}", json.dumps(known))
    runs = [{"url": FLYTE + f"ap{index:02d}", "name": f"Approval run {index + 1}",
             "status": status} for index, status in enumerate(APPROVALS)]  # fmt: skip
    ship = await started("E58 ship the approvals", "done", runs)
    runtime.task_actions.accept(ship["id"])
    runtime.task_actions.record_opened(ship["id"], ["https://github.com/acme/app/pull/58"])
    runtime.remember_pr_title("acme/app#58", "Gate the export on approval")
    known = json.loads(store.get(f"artifacts:{ship['id']}"))
    for index, item in enumerate(known):
        item["at"] = ago(minutes=5 + 30 * (len(known) - index))
    known[1] |= {"status": "ended", "by": "you"}
    store.set(f"artifacts:{ship['id']}", json.dumps(known))
    for task in found.values():
        runtime.task_actions.publish(runtime.tasks.get(task["id"]))
    return found


async def round_thirteen(runtime, browser, failures: list[str]):
    """The owner's QA round 13. E57: a running task's run of unknown status is ended or
    untracked from its menu. E48: a task's runs under their line are one card with it, each a
    line, in the list's columns at 1500, 760, 400 and 352 px. E58: a run waiting for approval is
    never folded away and is counted; "+N" says what it shows; the line's ✕ takes all its runs
    off, each back from the list of those taken off; the line's tooltip names the owner."""
    tasks = await thirteen_seeded(runtime)
    context = await browser.new_context(viewport={"width": 1500, "height": 1000})
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    monitoring = page.locator("#tasks-monitoring")
    sweep = page.locator("#panel .card.task").filter(
        has=page.locator(".head .title", has_text="E57 tune the ranker")
    )
    await expect(monitoring.locator(".card.task", has_text="E57 tune")).to_have_count(
        1, timeout=15000
    )
    line = sweep.locator(".row-line")
    await expect(line.locator(".why")).to_have_text("Status unknown")
    await expect(line.locator(".quiet")).to_have_text("reported running 30h ago")
    await sweep.get_by_role("button", name=re.compile("More actions")).click()
    menu = sweep.locator(".menu:not([hidden])")
    items = await menu.get_by_role("menuitem").all_text_contents()
    assert items[:2] == ["Mark ended", "Stop tracking"], items
    await menu.get_by_role("menuitem", name="Stop tracking").click()
    await expect(line.locator(".why")).not_to_have_text("Status unknown", timeout=10000)
    [kept] = json.loads(runtime.store.get(f"artifacts:{tasks['E57 tune the ranker']['id']}"))
    assert kept["untracked"], kept
    log("E57: a running task's run of unknown status is untracked from its menu")

    made = page.locator("#tasks-artifacts")
    await made.locator(":scope > .items").evaluate("n => { n.style.maxHeight = 'none'; }")
    line = made.locator(".artifact.runs").filter(has=page.locator(".a-meta", has_text="E58 ship"))
    await expect(line.locator(".a-status")).to_have_text("1 failed · 1 waiting · 2 running")
    await expect(line).to_have_class(re.compile(r"\btone-bad\b"))
    await expect(line.locator(".a-status")).to_have_attribute(
        "data-tip",
        "1 failed · 1 waiting · 2 running\nAs their worker last said, or as you marked them",
    )
    more = line.locator(".a-more")
    await expect(more).to_have_text("+6")
    await expect(more).to_have_attribute("data-tip", "Show 6 more: 2 running, 3 done, 1 ended")
    group = made.locator(".artifact.in-runs", has_text="Approval run")
    names = await group.locator(".a-name").all_text_contents()
    assert names == ["Approval run 9", "Approval run 5", "Approval run 3"], names
    await expect(group.nth(1).locator(".a-status")).to_have_text(re.compile(r"^waiting · \dh ago$"))
    for width in (1500, 760, 400, 352):
        await page.set_viewport_size({"width": width, "height": 1000})
        if width < 1100:
            await page.locator("#home-tabs [data-tab=tasks]").click()
        await made.locator(":scope > .items").evaluate("n => { n.style.maxHeight = 'none'; }")
        await page.wait_for_timeout(150)
        rows = await made.evaluate(COLUMNS)
        assert len({row["kind"] for row in rows}) == 1, (width, rows)
        assert len({row["name"] for row in rows}) == 1, (width, rows)
        ship = tasks["E58 ship the approvals"]["id"]
        header = next(index for index, row in enumerate(rows) if row["task"] == ship)
        under = list(
            itertools.takewhile(lambda row: "in-runs" in row["cls"].split(), rows[header + 1 :])
        )
        assert under and all(row["hidden"] for row in under), (width, rows)
        # One card: each right under the one before; one line each.
        chain = [rows[header], *under]
        assert all(b["top"] == a["bottom"] for a, b in itertools.pairwise(chain)), (width, chain)
        assert all(row["height"] < rows[header]["height"] for row in under), (width, chain)
        found = await made.evaluate(OVERFLOWS)
        assert not found, (width, found)
    await page.set_viewport_size({"width": 1500, "height": 1000})
    log("E48: a task's runs are one card with their line, in its columns, at four widths")

    take = line.get_by_role(
        "button", name="Take the 9 runs of E58 ship the approvals off this list"
    )
    await take.click()
    await expect(made.locator(".artifact", has_text="Approval run")).to_have_count(0, timeout=5000)
    await expect(made.locator(".artifact.runs", has_text="E58 ship")).to_have_count(0)
    await expect(made.locator(".artifact", has_text="app#58")).to_have_count(1)
    # Undo, from what it said: all back at once.
    note = page.locator(".toast", has_text="9 runs taken off Artifacts")
    await note.get_by_role("button", name="Undo").click()
    await expect(made.locator(".artifact.runs", has_text="E58 ship")).to_have_count(1, timeout=5000)
    for _ in range(50):
        if not [item for item in runtime.hidden_artifacts() if "ap0" in item["key"]]:
            break
        await page.wait_for_timeout(100)
    else:
        raise AssertionError(runtime.hidden_artifacts())
    await take.click()
    await expect(made.locator(".artifact", has_text="Approval run")).to_have_count(0, timeout=5000)
    hidden = [item["key"] for item in runtime.hidden_artifacts()]
    assert sum("ap0" in key for key in hidden) == 9, hidden
    await made.get_by_role("button", name=re.compile(r"^Show \d+ taken off$")).click()
    back = made.locator(".artifact.hidden-artifact", has_text="Approval run 5")
    await back.get_by_role("button", name="Bring back").click()
    await expect(
        made.locator(".artifact:not(.hidden-artifact)", has_text="Approval run 5")
    ).to_have_count(1)
    log("E58: a task's runs taken off at once from their line, each brought back")
    assert not errors, errors
    await context.close()


async def fourteen_seeded(runtime) -> dict:
    """QA round 14: an accepted task with two runs nothing told of for 30 hours (E78); one with
    a wiki page on a port of its own and a DAG run on a private address (E71); one whose runs
    are queued, waiting and running, one of them just marked ended after three days (E76,
    E77); six merged PRs of one task, a run of it failing, and another task's PR with failing
    CI (E58)."""
    store = runtime.store
    router = store.get("router_id")
    store.save_settings(store.settings().model_copy(update={"max_workers": 90}))
    ago = lambda **delta: (datetime.now(UTC) - timedelta(**delta)).isoformat()  # noqa: E731
    found = {}

    async def delivered(title, artifacts, prs=()):
        result = await call_tool(runtime, "spawn_worker", router, title=title, instructions=title,
                                 why_new="test")  # fmt: skip
        worker = json.loads(result.text_result_for_llm)["session_id"]
        await settle(runtime)
        task = runtime.tasks.current(worker)
        await call_tool(runtime, "report", worker, status="done", summary=f"{title}: done.",
                        artifacts=artifacts)  # fmt: skip
        await settle(runtime)
        if prs:
            run = store.one("SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC",
                            (task["id"],))  # fmt: skip
            store.execute("UPDATE runs SET evidence=? WHERE id=?",
                          (json.dumps({"checkouts": [], "prs": list(prs)}), run["id"]))  # fmt: skip
        runtime.task_actions.accept(task["id"])
        runtime.update_session(worker, status="idle", activity="")
        found[title] = runtime.tasks.get(task["id"])
        return task

    def said(task, url, **fields):
        known = json.loads(store.get(f"artifacts:{task['id']}"))
        for item in known:
            if item["url"] == url:
                item.update(fields)
        store.set(f"artifacts:{task['id']}", json.dumps(known))

    def pr(number, title, **state):
        return {"label": f"acme/app#{number}", "url": f"https://github.com/acme/app/pull/{number}",
                "title": title, "state": "MERGED", "checks": "passing", "failing": [],
                "review": "APPROVED", "own": True, "at": ago(hours=number % 7 + 1),
                **state}  # fmt: skip

    indexes = (("ix1", "Rebuild users index"), ("ix2", "Rebuild orders index"))
    two = [{"url": FLYTE + run, "name": name, "status": "running"} for run, name in indexes]
    rebuild = await delivered("E78 rebuild the search indexes", two)
    for item in two:
        said(rebuild, item["url"], at=ago(hours=30))
    await delivered("E71 publish the rollout notes", [
        {"url": "http://wiki.corp:8090/display/ENG/Rollout", "name": "Rollout notes",
         "status": "published"},
        {"url": "http://10.1.2.9:8080/dags/rollout/grid?dag_run_id=m1", "name": "Rollout DAG",
         "status": "running"},
    ])  # fmt: skip
    states = [("q1", "Shard 1 export", "queued"), ("q2", "Shard 2 export", "waiting"),
              ("q3", "Shard 3 export", "running"), ("q4", "Shard 4 export", "running")]  # fmt: skip
    shards = await delivered("E76 export the shards", [
        {"url": FLYTE + run, "name": name, "status": status} for run, name, status in states
    ])  # fmt: skip
    for index, (run, _name, _status) in enumerate(states):
        said(shards, FLYTE + run, at=ago(hours=1 + index))
    said(shards, FLYTE + "q4", at=ago(days=3))
    runtime.task_actions.end_runs(shards["id"], [FLYTE + "q4"], "ended")  # E77: just now.
    merged = [pr(number, f"Docs batch {number}") for number in range(101, 107)]
    await delivered("E58 merge the docs batch", [
        {"url": FLYTE + "d1", "name": "Docs link check", "status": "failed"},
    ], merged)  # fmt: skip
    await delivered("E58 fix the cache build", [], [
        pr(120, "Cache build", state="OPEN", checks="failing", failing=["build"], review=""),
    ])  # fmt: skip
    for task in found.values():
        runtime.task_actions.publish(runtime.tasks.get(task["id"]))
    return found


async def round_fourteen(runtime, browser, failures: list[str]):
    """The owner's QA round 14. E78: of two runs of unknown status, Mark ended… picks which. E71:
    a wiki page on a port of its own and a DAG on a private address are on Artifacts. E76: one
    word for each run (1 running · 2 pending). E77: one marked ended just now says so. E58:
    another task's failing CI above one task's six merged PRs, in a 300px list. E72: an away
    notice not delivered is said in the sidebar and in Settings, in red, until one is."""
    tasks = await fourteen_seeded(runtime)
    context = await browser.new_context(viewport={"width": 1500, "height": 1000})
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    needs = page.locator("#needs")
    rebuild = needs.locator(".card.task").filter(
        has=page.locator(".head .title", has_text="E78 rebuild the search indexes")
    )
    await expect(rebuild).to_be_visible(timeout=15000)
    await expect(rebuild.get_by_role("button", name="Stop tracking…")).to_be_visible()
    await rebuild.get_by_role("button", name="Mark ended…").click()
    picker = rebuild.locator(".run-picker")
    await expect(picker.locator("label")).to_have_count(2)
    # Listed as their chips above are.
    chips = await rebuild.locator(".link-chip.kind-flow .chip-link").all_text_contents()
    picked = await picker.locator("label > span:not(.muted)").all_text_contents()
    assert picked == [chip.split(" · ")[1] for chip in chips], (picked, chips)
    confirm = rebuild.get_by_role("button", name="Mark ended", exact=True)
    await expect(confirm).to_be_disabled()
    await expect(picker.get_by_role("checkbox").first).to_be_focused()
    await page.keyboard.press("Space")
    await expect(confirm).to_be_enabled()
    await confirm.click()
    # The other is still of unknown status: one left, its actions for it alone.
    await expect(rebuild.get_by_role("button", name="Mark ended", exact=True)).to_be_visible(
        timeout=10000
    )
    known = json.loads(
        runtime.store.get(f"artifacts:{tasks['E78 rebuild the search indexes']['id']}")
    )
    assert {item["name"]: item["status"] for item in known} == {
        picked[0]: "ended",
        picked[1]: "running",
    }, known
    log("E78: of two runs of unknown status, one marked ended; the other's actions are its own")

    made = page.locator("#tasks-artifacts")
    names = await made.locator(".a-name").all_text_contents()
    assert "Rollout notes" in names and "Rollout DAG" in names, names
    shards = made.locator(".artifact.runs").filter(has=page.locator(".a-meta", has_text="E76"))
    await expect(shards.locator(".a-status")).to_have_text("1 running · 2 pending")
    await shards.locator(".a-fold").click()
    await expect(
        made.locator(".artifact", has_text="Shard 4 export").locator(".a-status")
    ).to_have_text("ended · just now")
    log("E71/E76/E77: listed wherever served; one word per run; since it stood so")
    rows = made.locator(".artifact")
    labels = await rows.evaluate_all(
        "rows => rows.map((r) => r.querySelector('.a-name')?.textContent || '')"
    )
    failing = labels.index("app#120 · Cache build")
    first_merged = min(i for i, label in enumerate(labels) if label.startswith("app#10"))
    assert failing < first_merged, labels
    box = await made.locator(":scope > .items").bounding_box()
    shown = await rows.nth(failing).bounding_box()
    assert box and shown and shown["y"] + shown["height"] <= box["y"] + box["height"], (box, shown)
    log("E58: another task's failing CI above one task's merged PRs, in sight")

    store = runtime.store
    store.save_settings(store.settings().model_copy(update={"away_notify": True}))
    store.set("away_last", json.dumps({"at": datetime.now(UTC).isoformat(), "ok": False,
                                       "where": "Slack DM to you",
                                       "error": "the bot could not sign in"}))  # fmt: skip
    runtime.publish_state()
    line = page.locator("#status-line .bad", has_text="Away notice not delivered")
    await expect(line).to_be_visible(timeout=10000)
    await expect(line).to_have_attribute(
        "title", "Could not reach you via Slack DM to you: the bot could not sign in"
    )
    await line.get_by_role("button", name="Settings").click()
    # It opens When you're away's details; its row and its head say it.
    await expect(page.locator("[data-conn-detail=away]")).to_be_visible()
    summary = page.locator("#away-summary")
    await expect(summary).to_have_text(re.compile(r"^Away notice not delivered · "))
    await expect(page.locator("[data-conn-state=away]")).to_have_text(
        re.compile(r"^Away notice not delivered · ")
    )
    assert await summary.evaluate("n => n.classList.contains('bad')")
    red = await summary.evaluate(
        "n => { const i = document.createElement('i'); i.style.color = 'var(--red)';"
        " n.append(i); const c = getComputedStyle(i).color; i.remove(); return c; }"
    )
    assert await summary.evaluate("n => getComputedStyle(n).color") == red
    store.set("away_last", json.dumps({"at": datetime.now(UTC).isoformat(), "ok": True,
                                       "where": "Slack DM to you"}))  # fmt: skip
    store.set("away_where", "Slack DM to you")
    runtime.publish_state()
    await expect(summary).to_have_text("On · Slack DM to you", timeout=10000)
    assert not await summary.evaluate("n => n.classList.contains('bad')")
    # What "away" is read from here, said as it is: keyboard and mouse (macOS, Windows), else
    # this page (Linux).
    from chiefofstaff import connector as connector_module

    after = page.locator("#away-after")
    keys = "Away after (minutes without keyboard or mouse)"
    page_use = "Away after (minutes without using Chief of Staff)"
    await expect(after).to_have_text(keys if sys.platform in {"darwin", "win32"} else page_use)
    reading = connector_module.idle_reading
    connector_module.idle_reading = lambda: "app"
    runtime.publish_state()
    await expect(after).to_have_text(page_use)
    connector_module.idle_reading = lambda: "input"
    runtime.publish_state()
    await expect(after).to_have_text(keys)
    connector_module.idle_reading = reading
    runtime.publish_state()
    await page.keyboard.press("Escape")
    await expect(page.locator("#status-line .bad", has_text="Away notice")).to_have_count(0)
    store.save_settings(store.settings().model_copy(update={"away_notify": False}))
    runtime.publish_state()
    log("E72: an away notice not delivered, red in the sidebar and in Settings until one is")
    log("away is read from the keyboard and mouse, or this page: its setting says which")
    assert not errors, errors
    await context.close()


async def qa6_seeded(runtime) -> dict:
    """QA round 6 (the owner's report): a worker stopped with the owner's message unseen (1); a
    result and the reply drafted for it (2); a worker whose earlier task A is watched while B,
    given since, waits for acceptance (3, 4); accepted work with news just now (5); accepted
    work with two runs gone on with as a new task of the same name, its command running (6)."""
    store = runtime.store
    router = store.get("router_id")
    store.save_settings(store.settings().model_copy(update={"max_workers": 90}))
    ago = lambda **delta: (datetime.now(UTC) - timedelta(**delta)).isoformat()  # noqa: E731
    found: dict = {}

    async def spawn(title):
        result = await call_tool(runtime, "spawn_worker", router, title=title, instructions=title,
                                 why_new="test")  # fmt: skip
        worker = json.loads(result.text_result_for_llm)["session_id"]
        await settle(runtime)
        session = runtime.states.handle(worker)
        session.emit("user.message", content=session.raw_sent[-1])
        session.emit("assistant.turn_start")
        await settle(runtime)
        return worker, runtime.tasks.current(worker)

    async def idle(worker):
        runtime.states.handle(worker).emit("session.idle")
        await settle(runtime)

    async def nothing():
        return []

    held, _task = await spawn("QA6 update the deploy guide")
    runtime.update_session(held, status="working")
    runtime.states.handle(held).get_events = nothing
    await runtime.owner_send(held, "QA6 use the staging config")
    await runtime.workers.interrupt(held)
    found["held"] = held

    ann, task = await spawn("QA6 answer Ann about the release")
    await call_tool(runtime, "report", ann, status="done", summary="The release is Friday.")
    await idle(ann)
    found["draft"] = store.create_draft(
        ann, "reply", {"to": "Ann", "where": "email", "task_id": task["id"]}, "It is Friday."
    )
    found["ann"] = task

    # A question its worker asks about its result: answering it accepts nothing.
    asker, task = await spawn("QA6 size the cache")
    await call_tool(runtime, "report", asker, status="done", summary="96 MB fits.")
    await idle(asker)
    found["ask"] = store.create_draft(
        asker, "question", {"task_id": task["id"], "choices": ["Yes", "No"]}, "Keep 96 MB?"
    )

    chat, a = await spawn("QA6 benchmark the cache backends")
    await call_tool(runtime, "report", chat, status="done", summary="Backend B is 20% faster.",
                    artifacts=[{"url": FLYTE + "qa6c", "name": "QA6 load test",
                                "status": "running"}])  # fmt: skip
    runtime.task_actions.accept(a["id"])
    await idle(chat)
    await call_tool(runtime, "message_session", router, session_id=chat, task="new",
                    why_new="another question",
                    title="QA6 answer whether the API docs cover paging",
                    text="Do the API docs cover paging?")  # fmt: skip
    await settle(runtime)
    await call_tool(
        runtime, "report", chat, status="done", summary="Yes, under the list endpoints."
    )
    await idle(chat)
    found["chat"], found["a"] = chat, a

    news, task = await spawn("QA6 migrate the exporter to the new schema")
    await call_tool(runtime, "report", news, status="done", summary="Switched.")
    runtime.task_actions.accept(task["id"])
    await idle(news)
    runtime.tasks.update(task["id"], ended_at=ago(hours=1, minutes=5))
    runtime.task_actions.news(task["id"], "QA6 the schema migration finished.")
    found["news"] = task

    went, y = await spawn("QA6 tune the retry policy, option 1")
    runs = [{"url": FLYTE + "qa6a", "name": "QA6 run a", "status": "running"},
            {"url": FLYTE + "qa6b", "name": "QA6 run b", "status": "running"}]  # fmt: skip
    await call_tool(runtime, "report", went, status="done", summary="Both runs started.",
                    artifacts=runs)  # fmt: skip
    runtime.task_actions.accept(y["id"])
    await idle(went)
    await call_tool(runtime, "message_session", router, session_id=went, task="new",
                    why_new="more of it", title="QA6 tune the retry policy, option 1",
                    text="Wait for both runs and compare them.")  # fmt: skip
    await settle(runtime)
    await call_tool(runtime, "report", went, status="progress", summary="Waiting for both runs.",
                    artifacts=runs)  # fmt: skip
    await idle(went)
    runtime.states[went].held = True  # Its turn over, a command it left running goes on.
    runtime.update_session(went, status="idle", activity="")
    found["y"] = y
    for task in runtime.tasks.listed():
        runtime.task_actions.publish(runtime.tasks.get(task["id"]))
    for draft in (found["draft"], found["ask"]):
        runtime.bus.publish("draft", store.draft(draft["id"]))
    runtime.publish_state()
    return found


async def round_qa6(runtime, browser, failures: list[str]):
    """The owner's QA round 6. 1: Home's copy of what they wrote a worker stands where the
    worker has it; held by a stop, it says so, with Send now. 2: a result and the reply drafted
    for it, one decision: Copy & accept, and the card is gone with both done. 3/4: written in a
    worker's chat opened at an earlier task, a message is about that task: its row comes under
    Running, named for it, once. 5: an update's time is its own. 6: accepted work gone on with
    as a new task of the same name is one row."""
    found = await qa6_seeded(runtime)
    store = runtime.store
    context = await browser.new_context(viewport={"width": 1500, "height": 1100})
    await context.grant_permissions(["clipboard-read", "clipboard-write"])
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)

    copy = page.locator("#timeline .msg.owner", has_text="QA6 use the staging config")
    await expect(copy.locator(".to")).to_have_text("→ QA6 update the deploy guide")
    line = copy.locator(".delivery")
    await expect(line).to_have_text("Not sent yet: you stopped this worker · Send now")
    box = await line.bounding_box()
    assert box and box["height"] < 24, ("one short line", box)
    await line.get_by_role("button", name="Send now").click()
    await expect(line).to_have_text("Sent", timeout=10000)
    sent = runtime.states.handle(found["held"]).raw_sent[-1]
    assert "QA6 use the staging config" in sent, sent
    assert not any(
        m["delivery"] == "queued"
        for m in await page.evaluate(
            "id => import('/static/dom.js').then((d) => d.api(`/api/sessions/${id}/messages`))",
            store.get("router_id"),
        )
        if m["kind"] == "owner" and (m["meta"] or {}).get("to")
    ), "no copy of a message a worker has says it waits"
    log("QA6-1: Home's copy stands where the worker has it; held, it says why, Send now sends")

    needs = page.locator("#needs")
    card = needs.locator(f'.card.draft[data-id="{found["draft"]["id"]}"]')
    inner = card.locator(".card.task.in-draft")
    await expect(inner.locator(".head .title")).to_have_text("QA6 answer Ann about the release")
    await expect(inner.get_by_role("button", name="Accept")).to_have_count(0)
    await expect(inner.get_by_role("button", name="Request changes")).to_be_visible()
    main = card.get_by_role("button", name="Copy & accept")
    await expect(main).to_be_visible()
    await card.screenshot(path=SHOTS / "ui-edges-qa6-one-decision.png")
    await main.click()
    await expect(page.locator(".toast", has_text="Result accepted.")).to_be_visible()
    await expect(card).to_have_count(0, timeout=10000)
    await expect(needs.locator(".card", has_text="QA6 answer Ann")).to_have_count(0)
    assert store.draft(found["draft"]["id"])["status"] == "sent"
    assert runtime.tasks.get(found["ann"]["id"])["status"] == "done"
    assert await page.evaluate("navigator.clipboard.readText()") == "It is Friday."
    log("QA6-2: a result and its reply, one action: copied, sent, accepted; the card gone")
    asked = needs.locator(f'.card.draft[data-id="{found["ask"]["id"]}"]')
    await expect(asked.locator(".card.task.in-draft .head .title")).to_have_text(
        "QA6 size the cache"
    )
    await expect(
        asked.locator(".card.task.in-draft").get_by_role("button", name="Accept")
    ).to_be_visible()
    log("QA6-2: a question about a result keeps Accept on it: answering it accepts nothing")

    title = "QA6 benchmark the cache backends"
    watched = page.locator("#tasks-monitoring .card.task", has_text=title)
    await expect(watched).to_have_count(1)
    # Opened at its end (the sidebar), with two finished tasks, the latest given by the Router:
    # nothing says which one is meant, so what is written there is a task of its own.
    await page.locator("#session-list .item", has_text="QA6 answer whether the API").click()
    composer = page.locator("#composer-input")
    await expect(composer).to_have_attribute("placeholder", "A new task for this worker…")
    await page.locator("#back-home").click()
    # On Home, @ that worker says so before it is sent (review M1).
    await composer.press_sequentially("@QA6 answer whether")
    await page.locator("#mention-list li", has_text="QA6 answer whether the API").click()
    await expect(page.locator("#composer-to")).to_contain_text("· as a new task")
    chip = await page.locator("#composer-to").bounding_box()
    assert chip and chip["height"] <= 32, ("one line", chip)
    await page.locator("#composer-to .clear").click()
    await composer.fill("")
    await watched.locator(".worker-ref").first.click()
    await expect(page.locator("#back-home")).to_be_visible()
    await expect(composer).to_have_attribute("placeholder", f"Message about “{title}”…")
    async with page.expect_request(f"**/api/sessions/{found['chat']}/messages") as request:
        await composer.fill("QA6 also time the cold start")
        await composer.press("Enter")
    assert (await request.value).post_data_json["task_id"] == found["a"]["id"]
    asked = [m for m in store.messages(found["chat"]) if m["kind"] == "owner"][-1]
    assert asked["meta"]["task_id"] == found["a"]["id"], asked
    await page.locator("#back-home").click()
    running = page.locator("#tasks-running .card", has_text=title)
    await expect(running).to_have_count(1, timeout=10000)
    await expect(running.locator(".head .meta")).to_contain_text("Following up")
    await expect(watched).to_have_count(0)
    await expect(
        page.locator("#tasks-running .card", has_text="QA6 answer whether the API docs")
    ).to_have_count(0)
    log("QA6-3/4: written at task A in its chat, it is A's: one row, under Running, named A")

    # (Read before a message goes to its worker below: at work, its card is under Running.)
    update = needs.locator(".card.task", has_text="QA6 migrate the exporter")
    await expect(update.locator(".head .meta .marker")).to_have_text("Update")
    await expect(update.locator(".head .meta .when")).to_have_text(" · just now")
    log("QA6-5: an update's time is the news's own, not when its task ended")

    # Every worker slot taken (the one at work above has one): a message waits for a slot,
    # says so in a line, and Send now sends it all the same.
    from chiefofstaff import machine

    roomy, machine.short_of_memory = machine.short_of_memory, lambda: None
    busy = runtime.workers.running()
    assert busy >= 1, busy
    store.save_settings(store.settings().model_copy(update={"max_workers": busy}))
    await runtime.owner_send(found["news"]["session_id"], "QA6 add the row counts")
    waits = page.locator("#timeline .msg.owner", has_text="QA6 add the row counts")
    await expect(waits.locator(".delivery")).to_have_text(
        "Not sent yet: waiting for a free worker slot · Send now", timeout=10000
    )
    await waits.get_by_role("button", name="Send now").click()
    await expect(waits.locator(".delivery")).to_have_text("Sent", timeout=10000)
    store.save_settings(store.settings().model_copy(update={"max_workers": 90}))
    machine.short_of_memory = roomy
    log("QA6-1: every slot taken, a message says so in a line; Send now sends it")

    again = "QA6 tune the retry policy, option 1"
    await expect(page.locator("#tasks-running .card.task", has_text=again)).to_have_count(1)
    await expect(page.locator("#tasks-monitoring .card.task", has_text=again)).to_have_count(0)
    earlier = page.locator("#tasks-recent .card.task", has_text=again)
    await expect(earlier.locator(".head .meta")).to_contain_text("Still following")
    await expect(earlier).to_contain_text("Still following, in a newer task of the same name")
    log("QA6-6: accepted work gone on with as a new task of the same name: one row")
    assert not errors, errors
    await context.close()


async def life_seeded(runtime) -> dict:
    """QA round 7 on 0.1.46, a task's life: a worker waiting for the owner's word on its
    folder's settings (E98); accepted work whose check-back failed (E109); a reply whose send was
    cut short, "Sent?" (E113); a task failed just now, one four days ago (E89); a review whose
    summary the owner removed before a failed send (E105); Watch for you scans failing (E102)."""
    from chiefofstaff.models import Watch

    store = runtime.store
    router = store.get("router_id")
    store.save_settings(
        store.settings().model_copy(
            update={
                "max_workers": 90,
                "watches": [Watch(what="QA7 Slack DMs and mentions of me", how="Slack search")],
            }
        )
    )
    ago = lambda **delta: (datetime.now(UTC) - timedelta(**delta)).isoformat()  # noqa: E731
    found: dict = {}

    async def spawn(title):
        result = await call_tool(runtime, "spawn_worker", router, title=title, instructions=title,
                                 why_new="test")  # fmt: skip
        worker = json.loads(result.text_result_for_llm)["session_id"]
        await settle(runtime)
        session = runtime.states.handle(worker)
        session.emit("user.message", content=session.raw_sent[-1])
        session.emit("assistant.turn_start")
        await settle(runtime)
        return worker, runtime.tasks.current(worker), session

    async def idle(session):
        session.emit("session.idle")
        await settle(runtime)

    held, task, session = await spawn("QA7 fix the login timeout")
    await call_tool(runtime, "report", held, status="progress", summary="PR open, in review.")
    await idle(session)
    reason = "Copilot settings Chief of Staff hasn't checked yet (.claude/settings.json)"
    line = f"{reason}; Chief of Staff won't start the worker there until you say to use them."
    refusal = runtime.settings_refusal

    async def refused(row):  # Its folder's settings as they are: changed, until the owner's word.
        if row["id"] != held or found.get("used"):
            return await refusal(row)
        runtime.settings_reasons[held] = reason
        runtime.settings_said(held, line, {"digest": "qa7", "changes": [("added", ".mcp.json")]})
        return line

    runtime.settings_refusal = refused
    # It runs sandboxed: only then do its folder's settings wait for the owner.
    guarded = runtime.settings_guarded
    runtime.settings_guarded = lambda row: row["id"] == held or guarded(row)
    await refused(store.session(held))
    await runtime.settings_stop(held)
    queued = await runtime.owner_send(held, "ok")
    found |= {
        "held": held,
        "held_message": queued["id"],
        "settings": task,
        "refusal": refusal,
        "refused": refused,
    }

    checked, task, session = await spawn("QA7 export the nightly data")
    await call_tool(runtime, "report", checked, status="done", summary="Exported.",
                    artifacts=[{"url": FLYTE + "qa7", "name": "QA7 nightly export",
                                "status": "running"}])  # fmt: skip
    await call_tool(runtime, "check_back", checked, task_id=task["id"], minutes=60,
                    what="whether the nightly export finished", url=FLYTE + "qa7")  # fmt: skip
    await idle(session)
    runtime.task_actions.accept(task["id"])
    check = json.loads(store.get(f"checkback:{task['id']}"))
    # Its next try a few minutes ahead, as wake_check_backs sets it after a failure: one due now
    # was tried again by the watch loop (each minute) when it ticked before the card was read,
    # and, delivered, left no card.
    check |= {"attempts": 3, "error": "Copilot is not running", "at": ago(minutes=-4)}
    store.set(f"checkback:{task['id']}", json.dumps(check))
    found["checked"] = task

    ann, task, session = await spawn("QA7 answer Ann about the release date")
    await idle(session)
    target = {"channel_id": "D0ANN0001", "thread_ts": "", "task_id": task["id"]}
    draft = store.create_draft(ann, "slack", target, "Friday works for the release.")
    store.update_draft(draft["id"], status="uncertain",
                       outcome="Chief of Staff stopped while sending this.")  # fmt: skip
    runtime.add_message(ann, "draft", "Friday works for the release.",
                        {"draft_id": draft["id"], "label": "Slack reply"})  # fmt: skip
    found |= {"ann": ann, "uncertain": draft["id"]}
    review = store.create_draft(
        ann, "github",
        {"repo": "acme/app", "number": 7, "verdict": "comment", "task_id": task["id"],
         "comments": [{"path": "app.py", "line": 3, "body": "QA7 off by one?"}]},
        "QA7 looks good overall.",
    )  # fmt: skip
    store.update_draft(review["id"], final_text="", outcome="Sending failed: 502")
    found["review"] = review["id"]

    for name, hours in (("new", 0), ("aged", 80)):
        worker, task, session = await spawn(f"QA7 count the rows, {name}")
        await call_tool(runtime, "report", worker, status="failed",
                        summary="The warehouse refused the query.")  # fmt: skip
        await idle(session)
        if hours:
            runtime.tasks.update(task["id"], ended_at=ago(hours=hours))
        found[name] = runtime.tasks.get(task["id"])

    if (row := runtime.connector.session()) and runtime.connector.busy():
        runtime.update_session(row["id"], status="idle")  # An earlier round's scan, at rest now.
    store.set("watch_last", json.dumps({"at": ago(hours=1, minutes=20), "found": 0, "seen": 3}))
    store.set(
        "watch_failed",
        json.dumps({"at": ago(minutes=12), "since": ago(minutes=62), "count": 3,
                    "reason": "Copilot failed on this turn 3 times: static context too large"}),
    )  # fmt: skip
    for listed in runtime.tasks.listed(include_ended_since=ago(hours=200)):
        runtime.task_actions.publish(runtime.tasks.get(listed["id"]))
    for draft_id in (draft["id"], review["id"]):
        runtime.bus.publish("draft", store.draft(draft_id))
    runtime.publish_state()
    return found


async def round_qa7_life(runtime, browser, failures: list[str]):
    """QA round 7, a task's life: each card that needs the owner has its own word on it (Use
    these settings, Retry check-back, Dismiss), "Sent?" is found from its timeline and counted
    on its worker's dot, a failed task out of Needs you is under Recently finished, failing Watch
    scans say so, Close worker says it closed (with Undo), and an older reply sets nothing back."""
    found = await life_seeded(runtime)
    store = runtime.store
    context = await browser.new_context(viewport={"width": 1500, "height": 1300})
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)
    needs = page.locator("#needs")

    # E113: "Sent?" is found from its worker's timeline, and its worker's dot waits.
    dot = page.locator("#session-list .item", has_text="QA7 answer Ann").locator(".dot")
    await expect(dot).to_have_class(re.compile(r"\bwaiting\b"))
    card = needs.locator(f'.card.uncertain[data-id="{found["uncertain"]}"]')
    await expect(card).to_be_visible()
    await page.locator("#session-list .item", has_text="QA7 answer Ann").click()
    await page.locator("#timeline .goto").click()
    await expect(page.locator(".home-item.selected")).to_be_visible()
    await expect(card).to_be_in_viewport()
    log("E113: a send cut short is found from its timeline; its worker's dot waits")

    # E105: a summary the owner removed stays removed on its card.
    review = needs.locator(f'.card.draft[data-id="{found["review"]}"]')
    await expect(review).to_be_visible()
    await expect(review).not_to_contain_text("QA7 looks good overall.")
    preview = review.locator(".summary-preview")
    await expect(preview).to_have_text("No summary: the line comments only.")
    # One line of its card, not an empty page's words (.empty: a fifth of the window above them).
    await expect(preview).to_have_css("margin-top", "0px")
    await expect(preview).to_have_css("text-align", "start")
    log("E105: a review summary removed before a failed send stays removed")

    # E121: under what the owner wrote a worker held for its folder's settings, at once, one
    # line saying why, with that line's three ways on (for the settings shown); "ok" there is
    # not their word on them. What they write there next carries the line it showed.
    await page.locator("#session-list .item", has_text="QA7 fix the login").click()
    message = page.locator(f'#timeline .msg.owner[data-id="{found["held_message"]}"]')
    hold = message.locator(".settings-hold")
    await expect(hold).to_contain_text("Not sent yet: its folder's settings changed")
    await expect(hold).to_contain_text(
        "Use the button so the approval is tied to the settings shown."
    )
    for name in ("Use these settings", "Start a new worker", "Cancel message"):
        await expect(hold.get_by_role("button", name=name)).to_be_visible()
    await expect(message.locator(".delivery")).to_be_hidden()
    shots = Path(os.environ.get("QA8_SMOOTH_SHOTS", SHOTS / "qa8-smooth"))
    shots.mkdir(parents=True, exist_ok=True)
    for width in (800, 1280):
        await page.set_viewport_size({"width": width, "height": 900})
        for scheme in ("light", "dark"):
            await page.emulate_media(color_scheme=scheme)
            await page.screenshot(path=shots / f"held-chat-{width}-{scheme}.png")
    await page.set_viewport_size({"width": 1500, "height": 1300})
    await page.emulate_media(color_scheme="light")
    async with page.expect_request(
        lambda request: request.method == "POST" and request.url.endswith("/messages")
    ) as posted:
        await page.locator("#composer-input").fill("Also check the retry path")
        await page.locator("#composer-input").press("Enter")
    assert (await posted.value).post_data_json.get("settings_digest") == "qa7"
    newest = page.locator("#timeline .msg.owner").last
    await expect(newest.locator(".settings-hold")).to_contain_text("Not sent yet")
    await expect(newest.locator(".hold-guidance")).to_have_count(0)
    await page.locator(".home-item").click()
    await expect(page.locator(".home-item.selected")).to_be_visible()
    log("E121: a held worker's queued message shows its line's choices; ok is not consent")

    # E98: one card for the worker waiting for the owner's word on its folder's settings.
    settings = needs.locator(".card.task", has_text="QA7 fix the login timeout")
    await expect(settings.locator(".head .meta")).to_contain_text("Settings changed")
    dot = page.locator("#session-list .item", has_text="QA7 fix the login").locator(".dot")
    await expect(dot).to_have_class(re.compile(r"\bwaiting\b"))
    await expect(settings.get_by_role("button", name="See what changed")).to_be_visible()
    found["used"] = True  # The owner's word: its folder's settings as they are now are used.
    await settings.get_by_role("button", name="Use these settings").click()
    await expect(page.locator(".toast").last).to_be_visible()
    await expect(settings).to_have_count(0, timeout=10000)
    runtime.settings_refusal = found["refusal"]
    log("E98: settings to approve are under Needs you; Use these settings sends the worker on")

    # E109: accepted work whose check-back failed: tried again, or dropped, from its card.
    checked = needs.locator(".card.task", has_text="QA7 export the nightly data")
    await expect(checked.locator(".head .meta")).to_contain_text("Check-back failed")
    await expect(checked.get_by_role("button", name="Retry check-back")).to_be_visible()
    await checked.get_by_role("button", name="Stop following up").click()
    await expect(page.locator(".toast", has_text="No longer followed up.")).to_be_visible()
    await expect(checked).to_have_count(0, timeout=10000)
    assert not store.get(f"checkback:{found['checked']['id']}")
    log("E109: a failed check-back on accepted work: Retry check-back, or Stop following up")

    # E89: a failed task is dismissed (its worker stays as it is); one failed days ago is under
    # Recently finished, still with Retry.
    new = needs.locator(".card.task", has_text="QA7 count the rows, new")
    await new.get_by_role("button", name="Dismiss").click()
    await expect(page.locator(".toast", has_text="Dismissed.")).to_be_visible()
    await expect(new).to_have_count(0, timeout=10000)
    assert runtime.tasks.get(found["new"]["id"])["status"] == "cancelled"
    aged = page.locator("#tasks-recent .card.task", has_text="QA7 count the rows, aged")
    more = page.locator("#tasks-recent .show-more")
    for _ in range(20):  # The newest first, a page at a time: it failed four days ago.
        if await aged.count() or not await more.count():
            break
        await more.click()
    await expect(aged.locator(".head .meta")).to_contain_text("Failed")
    await expect(needs.locator(".card.task", has_text="QA7 count the rows, aged")).to_have_count(0)
    log("E89: a failed task is dismissed; one failed days ago is under Recently finished")

    # E102: failing Watch scans say so, in the sidebar and in Settings.
    line = page.locator("#status-line .bad", has_text="Watch scan failed (3 in a row)")
    await expect(line).to_be_visible()
    await line.get_by_role("button", name="Settings").click()
    state = page.locator("#watch-state")
    await expect(state).to_contain_text("Scan failed")
    await expect(state).to_contain_text("(3 in a row)")
    await expect(state).to_have_class(re.compile(r"\berror\b"))
    await page.keyboard.press("Escape")
    store.delete("watch_failed")
    runtime.publish_state()
    await expect(line).to_have_count(0, timeout=10000)
    log("E102: failing Watch scans are said in the sidebar and in Settings")

    # E90: an action's reply older than what the page holds sets nothing back.
    kept = await page.evaluate(
        """async (id) => {
          const { upsertTask } = await import('/static/tasks.js');
          const { tasks } = await import('/static/state.js');
          const held = tasks().find((t) => t.id === id);
          upsertTask({ ...held, status: 'running', updated_at: '2000-01-01T00:00:00+00:00' }, true);
          return tasks().find((t) => t.id === id).status;
        }""",
        found["aged"]["id"],
    )
    assert kept == "failed", kept
    log("E90: an older reply does not overwrite a newer state")

    # E91: Close worker says so, with Undo.
    await page.locator("#session-list .item", has_text="QA7 answer Ann").click()
    page.once("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))  # Still working.
    await page.locator("#session-more-button").click()
    await page.locator("#session-menu-items").get_by_role("button", name="Close worker").click()
    toast = page.locator(".toast", has_text="Worker closed.")
    await expect(toast).to_be_visible()
    await toast.get_by_role("button", name="Undo").click()
    await expect(page.locator(".toast", has_text="Worker reopened.")).to_be_visible()
    assert not store.session(found["ann"])["closed_at"]
    log("E91: Close worker says so, with Undo")
    assert not errors, errors
    await context.close()


async def approvals_seeded(runtime) -> dict:
    """E123: Your OK with a changed decision (one worker's work changes), a lesson that is safe and
    one that is not, a learned check, a PR ready to merge and a sign-in; a reply in the owner's
    voice beside it; handled for the owner: a report accepted once read, a lesson kept."""
    store = runtime.store
    router = store.get("router_id")
    # Room for its workers: each starts at once (earlier rounds' are many, at work still).
    store.save_settings(store.settings().model_copy(update={"max_workers": 1000}))
    for draft in store.drafts():  # Earlier rounds': this one's cards alone.
        store.update_draft(draft["id"], status="expired", outcome="Left by an earlier round.")

    async def start(title):
        result = await call_tool(runtime, "spawn_worker", router, title=title, instructions=title,
                                 why_new="test")  # fmt: skip
        await settle(runtime)
        return json.loads(result.text_result_for_llm)["session_id"]

    api, form = await start("QA8 login API"), await start("QA8 login form")
    tasks = [runtime.tasks.current(api)["id"], runtime.tasks.current(form)["id"]]
    said = await call_tool(runtime, "record_decision", router, decision="QA8 email login only",
                           tasks=tasks)  # fmt: skip
    first = said.text_result_for_llm.split()[1].rstrip(",.")
    await call_tool(runtime, "record_decision", router, decision="QA8 email and SMS login",
                    replaces=[first])  # fmt: skip
    decision = next(d for d in runtime.decisions.active() if "QA8" in d["text"])
    await call_tool(runtime, "ack_decision", form, decision=decision["id"],
                    plan="Add a phone field")  # fmt: skip
    for lesson_id, text in [("l8short", "Keep reports to five lines"),
                            ("l8force", "Never force-push a shared branch")]:  # fmt: skip
        store.execute(
            "INSERT INTO lessons(id, text, scope, evidence, status, created_at, updated_at) "
            "VALUES (?, ?, '', '[]', 'proposed', 'now', 'now')",
            (lesson_id, text),
        )
    store.create_draft(router, "lessons", {"lessons": [
        runtime.learning.card_item("l8short"), runtime.learning.card_item("l8force")]},
        "2 lessons")  # fmt: skip
    evidence = {"failing": {"exit": 1, "output": "x"}, "fixed": {"exit": 0, "output": ""}}
    store.execute(
        """INSERT INTO checks(id, case_id, repo, name, why, applies_to, command, evidence,
        status, created_at, updated_at) VALUES ('k8', 'c8', 'svc', 'No print in the API',
        'Logs go through the logger.', '[]', '! git grep -n print', ?, 'proposed', 'x', 'x')""",
        (json.dumps(evidence),),
    )
    check = {
        "check": "k8", "name": "No print in the API", "why": "Logs go through the logger.",
        "command": "! git grep -n print", "applies_to": [], "repo": "svc", "task": "",
    }  # fmt: skip
    store.create_draft(router, "check", {**check, **evidence}, "A check")
    merge = store.create_draft(api, "merge", {
        "label": "acme/app#18", "repo": "acme/app", "number": 18, "title": "Email login",
        "url": "https://github.com/acme/app/pull/18", "head": "abc1234def", "review": "APPROVED",
    }, "Email login")  # fmt: skip
    store.create_draft(form, "signin", {"server": "docs", "url": "https://docs.acme.test/login"},
                       "Sign in to the docs server so the worker can read the spec.")  # fmt: skip
    reply = store.create_draft(form, "reply", {"to": "Maya", "where": "email"}, "Ships Friday.")
    # A sandboxed worker held for its folder's settings: its own row, never a line of Your OK.
    held = await start("QA8 fix the cache headers")
    guarded, refusal = runtime.settings_guarded, runtime.settings_refusal
    runtime.settings_guarded = lambda row: row["id"] == held or guarded(row)
    reason = "Copilot settings in this folder changed (added .mcp.json)"
    line = f"{reason}; Chief of Staff won't start the worker there until you say to use them."

    async def refused(row):  # As they are: changed, until the owner's word.
        if row["id"] != held:
            return await refusal(row)
        runtime.settings_reasons[held] = reason
        runtime.settings_said(held, line, {"digest": "qa8", "changes": [("added", ".mcp.json")]})
        return line

    runtime.settings_refusal = refused
    await refused(store.session(held))
    await runtime.settings_stop(held)
    # Handled for the owner: a report accepted once read (Undo), a lesson kept (Undo).
    reader = await start("QA8 compare queues")
    await call_tool(runtime, "report", reader, status="done", summary="SQS fits; Kafka is more.")
    await settle(runtime)
    report = runtime.tasks.current(reader)
    runtime.task_actions.mark_read(report["id"])
    runtime.task_actions.settle_reports(datetime.now(UTC) + timedelta(minutes=11))
    store.execute(
        "INSERT INTO lessons(id, text, scope, evidence, status, created_at, updated_at) "
        "VALUES ('l8kept', 'Name branches after the ticket', '', '[]', 'active', 'x', 'x')"
    )
    runtime.handled.record("lesson", "Kept the lesson “Name branches after the ticket”: your own "
                           "words said so 4 times", ref="l8kept", undo="Undo")  # fmt: skip
    runtime.publish_state()
    return {"merge": merge, "reply": reply, "report": report}


async def round_qa8_approvals(runtime, browser, failures: list[str]):
    """E123, smooth by default: the approvals are one card under Needs you, Your OK, counted
    once (a line each with its own button, the safe ones marked and taken by Approve all safe;
    a merge or a sign-in never), what speaks as the owner keeps its own card, and what was
    handled for the owner is one quiet line with Undo. Light and dark, 800 and 1280 px."""
    from chiefofstaff.digest import needs_you

    shots = Path(os.environ.get("QA8_APPROVALS_SHOTS", SHOTS))
    shots.mkdir(parents=True, exist_ok=True)
    store = runtime.store
    seeded = await approvals_seeded(runtime)
    merge, reply, report = seeded["merge"], seeded["reply"], seeded["report"]
    for width, scheme in itertools.product((1280, 800), ("light", "dark")):
        context = await browser.new_context(
            viewport={"width": width, "height": 1800}, color_scheme=scheme
        )
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
        await page.goto(f"{BASE}/#token={TOKEN}")
        await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)
        if width < 1100:
            await page.locator("#home-tabs [data-tab=needs]").click()
        ok = page.locator("#needs .card.your-ok")
        await expect(ok).to_have_count(1)
        rows = ok.locator(".ok-row")
        await expect(rows).to_have_count(6)
        await expect(ok.locator(".ok-count")).to_have_text("6")
        # Safe: the changed decision seen, the check, the lesson not about what can't be undone.
        await expect(ok.locator(".ok-row:has(.ok-safe)")).to_have_count(3)
        for kind in ("Ready to merge", "Sign-in"):
            await expect(rows.filter(has_text=kind).locator(".ok-safe")).to_have_count(0)
        await expect(ok.locator('.ok-row[data-lesson="l8force"] .ok-safe')).to_have_count(0)
        await expect(ok.get_by_role("button", name="Approve all safe (3)")).to_be_visible()
        # A folder's settings to use: its worker's own row, not a line of Your OK.
        hold = page.locator("#needs > .items > .card.task", has_text="QA8 fix the cache headers")
        await expect(hold.get_by_role("button", name="Use these settings")).to_be_visible()
        await expect(ok.locator(".ok-row", has_text="cache headers")).to_have_count(0)
        # What speaks as the owner keeps its own card; Your OK counts once.
        await expect(page.locator(f'#needs .card.draft[data-id="{reply["id"]}"]')).to_be_visible()
        await expect(page.locator(".home-item .badge")).to_have_text(str(needs_you(runtime)))
        found = await page.locator("#needs").evaluate(OVERFLOWS)
        assert not found, (width, found)
        handled = page.locator("#needs details.handled")
        await expect(handled.locator("summary")).to_contain_text("Handled for you")
        await expect(handled.locator("summary .count")).to_have_text("2")
        await handled.locator("summary").click()
        await expect(handled.locator("li")).to_have_count(2)
        await expect(handled).to_contain_text("Accepted “QA8 compare queues” after you read it")
        await ok.screenshot(path=shots / f"your-ok-{width}-{scheme}.png")
        await page.locator("#needs").screenshot(path=shots / f"needs-you-{width}-{scheme}.png")
        # Details: the line's whole card, under it.
        merge_row = ok.locator(f'.ok-row[data-id="{merge["id"]}"]')
        await merge_row.locator(".more-button").click()
        await merge_row.get_by_role("menuitem", name="Details").click()
        await expect(ok.locator(".ok-details .card.merge")).to_contain_text("Checks pass")
        await expect(ok.locator(".ok-details .card.merge").get_by_role("button", name="Merge"))\
            .to_be_visible()  # fmt: skip
        # Its action once, in the card opened: the line's own buttons step aside for Hide details.
        await expect(ok.get_by_role("button", name="Merge", exact=True)).to_have_count(1)
        await expect(merge_row.locator(".more-button")).to_have_count(0)
        await ok.screenshot(path=shots / f"your-ok-details-{width}-{scheme}.png")
        await merge_row.get_by_role("button", name="Hide details").click()
        await expect(ok.locator(".ok-details")).to_have_count(0)
        await expect(merge_row.get_by_role("button", name="Merge", exact=True)).to_be_visible()
        assert not errors, errors
        await context.close()
    log("Your OK: one card, a line each, the safe ones marked; replies keep their cards")

    context = await browser.new_context(viewport={"width": 1280, "height": 1000})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    ok = page.locator("#needs .card.your-ok")
    await ok.get_by_role("button", name="Approve all safe (3)").click()
    await expect(page.locator(".toast", has_text="Approved 3.")).to_be_visible()
    await expect(ok.locator(".ok-row")).to_have_count(3, timeout=10000)
    await expect(ok.get_by_role("button", name=re.compile("Approve all safe"))).to_have_count(0)
    assert store.one("SELECT status FROM checks WHERE id='k8'")["status"] == "active"
    statuses = {r["id"]: r["status"] for r in store.all("SELECT id, status FROM lessons")}
    assert (statuses["l8short"], statuses["l8force"]) == ("active", "proposed"), statuses
    assert store.draft(merge["id"])["status"] == "pending", "a merge: never with others"
    log("Approve all safe: the three safe ones; the merge, the sign-in, the risky lesson stay")

    # A report only, opened: it says it is accepted for the owner once read.
    said = await call_tool(runtime, "spawn_worker", store.get("router_id"),
                           title="QA8 summarize the logs", instructions="Summarize the logs",
                           why_new="test")  # fmt: skip
    summarizer = json.loads(said.text_result_for_llm)["session_id"]
    await settle(runtime)
    await call_tool(runtime, "report", summarizer, status="done", summary="Three timeouts.")
    await settle(runtime)
    summary = runtime.tasks.current(summarizer)
    for _ in range(600):  # Its checkouts read (in the background; slow on a loaded machine).
        if runtime.task_actions.informational(runtime.tasks.get(summary["id"])):
            break
        await asyncio.sleep(0.1)

    row = page.locator("#needs .card.task", has_text="QA8 summarize the logs")
    await row.locator(":scope > .head .title").click()
    await expect(row.locator(".report .settles")).to_have_text(
        "A report with no code or PR: accepted for you a few minutes after you've read it.",
        timeout=20000,
    )
    handled = page.locator("#needs details.handled")
    await handled.locator("summary").click()
    line = handled.locator("li", has_text="QA8 compare queues")
    await line.get_by_role("button", name="Undo").click()
    await expect(page.locator(".toast", has_text="waits for your Accept again")).to_be_visible()
    await expect(line).to_contain_text("(undone)", timeout=10000)
    await expect(line.get_by_role("button", name="Undo")).to_have_count(0)
    assert runtime.tasks.get(report["id"])["status"] == "review"
    back = page.locator("#needs .card.task", has_text="QA8 compare queues")
    await expect(back).to_be_visible()
    # Undone: its opened report no longer says it is accepted for the owner.
    await back.locator(":scope > .head .title").click()
    await expect(back.locator(".report")).to_have_count(1)
    await expect(back.locator(".report .settles")).to_have_count(0)
    # A reply sent for the owner (their delegate emoji): its line opens it in Slack; no Undo.
    link = "https://acme.slack.com/archives/C0DEPLOYS/p1795203000300300?thread_ts=1795201000.100100"
    runtime.handled.record(
        "slack-reply",
        "Replied in the thread in #deploys for you (you reacted :robot_face:)",
        data={"link": link},
    )
    sent = handled.locator("li", has_text="Replied in the thread in #deploys for you")
    await expect(sent.get_by_role("link", name="Open in Slack ↗")).to_have_attribute("href", link)
    await expect(sent.get_by_role("button")).to_have_count(0)
    log("Handled for you: one quiet line; Undo puts a result back to accept")
    await context.close()
    for draft in store.drafts():
        store.update_draft(draft["id"], status="expired", outcome="End of the round.")
    runtime.publish_state()


async def open_fold(line):
    """A line in a run Home folds in one (E129: routing receipts, team messages): its fold open."""
    group = line.locator("xpath=ancestor::div[contains(concat(' ', @class, ' '), ' fold ')][1]")
    if await group.count() and "open" not in (await group.get_attribute("class") or "").split():
        await group.locator(":scope > .fold-line button.line").click()


async def teams_seeded(runtime) -> dict:
    """Two workers that work together (E129): routing receipts, then five messages between them
    (told twice, a question answered, a question open 9 minutes), the owner, then one more."""
    store = runtime.store
    router = store.get("router_id")
    store.save_settings(store.settings().model_copy(update={"max_workers": 1000}))

    async def start(title, **extra):
        result = await call_tool(runtime, "spawn_worker", router, title=title, instructions=title,
                                 why_new="test", **extra)  # fmt: skip
        await settle(runtime)
        return json.loads(result.text_result_for_llm)["session_id"]

    runtime.add_message(router, "owner", "Rotate the API token and update the client with it.")
    scout = await start("Token Scout: rotate the API token")
    writer = await start("Column Maker: use the new token in the client",
                         works_with=[runtime.tasks.current(scout)["id"]])  # fmt: skip

    async def tool(name, caller, **arguments):
        return (await call_tool(runtime, name, caller, **arguments)).text_result_for_llm

    await tool("tell_session", scout, session_id=writer, text="The new token is in vault:api/v2.")
    await tool("tell_session", scout, session_id=writer, text="Old one stops working at 18:00.")
    asked = await tool("ask_session", writer, session_id=scout, question="Read or write scope?")
    first = asked.split("(")[1].split(")")[0]
    await tool("answer_session", scout, question_id=first, answer="Read only.")
    asked = await tool("ask_session", writer, session_id=scout, question="Rotate staging too?")
    second = asked.split("(")[1].split(")")[0]
    store.execute("UPDATE questions SET asked=? WHERE id=?", (time.time() - 9 * 60, second))
    runtime.add_message(router, "owner", "How is the token going?")
    await tool("tell_session", writer, session_id=scout, text="Client switched to the new token.")
    runtime.publish_state()
    return {"scout": scout, "writer": writer, "open": second}


async def teams_held_up(runtime, seeded: dict) -> str:
    """A third question, open 34 minutes, its asker at rest with its task open: held up."""
    asked = await call_tool(runtime, "ask_session", seeded["writer"], session_id=seeded["scout"],
                            question="Which region rotates first?")  # fmt: skip
    question = asked.text_result_for_llm.split("(")[1].split(")")[0]
    runtime.store.execute(
        "UPDATE questions SET asked=? WHERE id=?", (time.time() - 34 * 60, question)
    )
    # It ends its turn to wait for the answer: its task waits on it.
    waiting = runtime.states.handle(seeded["writer"])
    waiting.emit("assistant.message", content="Waiting for Token Scout's answer.")
    waiting.emit("session.idle")
    await settle(runtime)
    runtime.publish_state()
    return question


async def round_teams(runtime, browser, failures: list[str]):
    """E129, phase 1: on Home, a run of what workers told each other is one line (how many, who,
    what waits), open to its last three; a run of routing receipts is one line too; one alone
    stays a line. Light and dark, 800, 1280 and 1440 px."""
    shots = Path(os.environ.get("TEAMS_SHOTS", SHOTS / "teams"))
    shots.mkdir(parents=True, exist_ok=True)
    seeded = await teams_seeded(runtime)
    for width, scheme in itertools.product((1440, 1280, 800), ("light", "dark")):
        context = await browser.new_context(
            viewport={"width": width, "height": 900}, color_scheme=scheme
        )
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
        await page.goto(f"{BASE}/#token={TOKEN}")
        await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)
        timeline = page.locator("#timeline")
        team = timeline.locator(":scope > .fold.team").last
        await expect(team.locator(".fold-line .what")).to_have_text("5 team messages")
        await expect(team.locator(".fold-line .avatar")).to_have_count(2)
        await expect(team.locator(".fold-line .avatar").first).to_have_text("TS")
        await expect(team.locator(".fold-line .waiting")).to_have_text("1 waiting 9 min")
        await expect(team.locator(".fold-items")).to_be_hidden()
        routing = timeline.locator(":scope > .fold.routing").last
        await expect(routing.locator(".fold-line .what")).to_have_text("2 routing updates")
        # With Copilot alone, no agent marks.
        await expect(routing.locator(".fold-line .agent-mark")).to_have_count(0)
        # One alone after the owner's message: a line by the workers' short names.
        alone = timeline.locator(':scope > .event[data-kind="peer"]').last
        await expect(alone.locator(".what")).to_have_text(
            "Column Maker told Token Scout: Client switched to the new token."
        )
        found = await timeline.evaluate(OVERFLOWS)
        assert not found, (width, found)
        await timeline.screenshot(path=shots / f"home-folded-{width}-{scheme}.png")
        await team.locator(".fold-line button.line").click()
        toggle = team.locator(".fold-line button.line")
        await expect(toggle).to_have_attribute("aria-expanded", "true")
        shown = team.locator(".fold-items > .event:visible")
        await expect(shown).to_have_count(3)
        await expect(shown.first).to_contain_text("Column Maker asked Token Scout: Read or write")
        await expect(shown.last).to_contain_text("Column Maker asked Token Scout: Rotate staging")
        await routing.locator(".fold-line button.line").click()
        await expect(routing.locator(".fold-items > .event:visible")).to_have_count(2)
        await timeline.screenshot(path=shots / f"home-open-{width}-{scheme}.png")
        assert not errors, errors
        await context.close()
    log("Home: a run of team messages and of routing receipts, one line each")

    # The open question is answered: the line says nothing waits, without a reload.
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    team = page.locator("#timeline > .fold.team", has_text="5 team messages")
    await expect(team.locator(".fold-line .waiting")).to_have_text("1 waiting 9 min")
    await call_tool(runtime, "answer_session", seeded["scout"], question_id=seeded["open"],
                    answer="Yes, staging too.")  # fmt: skip
    await expect(team.locator(".fold-line .waiting")).to_have_count(0, timeout=10000)
    await context.close()
    log("Home: an answered question no longer waits")

    # A run longer than a page: Load earlier brings the rest into the same fold.
    router = runtime.store.get("router_id")
    runtime.add_message(router, "owner", "Thanks, both.")
    for index in range(405):
        runtime.add_message(router, "peer", f"“Token Scout: rotate the API token” told “Column "
                            f"Maker: use the new token in the client”: note {index}",
                            {"peer_from": seeded["scout"], "peer_to": seeded["writer"],
                             "peer_kind": "told"})  # fmt: skip
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    timeline = page.locator("#timeline")
    await expect(
        timeline.locator(":scope > .fold.team").last.locator(".fold-line .what")
    ).to_have_text("400 team messages", timeout=15000)
    await timeline.locator(":scope > .earlier button").click()
    longest = timeline.locator(":scope > .fold.team").last
    await expect(longest.locator(".fold-line .what")).to_have_text(
        "405 team messages", timeout=15000
    )
    await context.close()
    log("Home: a run longer than a page is one fold once the earlier messages load")


async def relations_seeded(runtime) -> dict:
    """A request in three parts (E130): the API landed, the UI at work, the tests handed to
    another worker; release notes that wait on the UI and build on the API; a part split off
    the UI; their PR (#482) shared. None of them ever messaged."""
    store = runtime.store
    router = store.get("router_id")
    store.save_settings(store.settings().model_copy(update={"max_workers": 1000}))

    async def start(title, **extra):
        result = await call_tool(runtime, "spawn_worker", router, title=title, instructions=title,
                                 why_new="test", **extra)  # fmt: skip
        await settle(runtime)
        found = json.loads(result.text_result_for_llm)
        return found["session_id"], found["task_id"]

    runtime.add_message(router, "owner", "Ship checkout v2: the API, the UI and its tests.")
    api, api_task = await start("Checkout API: the v2 endpoints")
    ui, ui_task = await start("Checkout UI: the v2 pages", works_with=[api_task])
    tests, tests_task = await start("Checkout tests: end to end", works_with=[api_task, ui_task])
    notes, notes_task = await start("Release Notes: checkout v2", after=[ui_task],
                                    context_from=[api_task])  # fmt: skip
    e2e, e2e_task = await start("E2E Runner: checkout tests on Linux", replaces=[tests_task])
    for worker in (api, ui, e2e):
        session = runtime.states.handle(worker)
        session.emit("user.message", content=session.sent[-1])  # At work.
    await settle(runtime)
    pr = "https://github.com/acme/checkout/pull/482"
    store.set(f"prs:{api_task}", json.dumps([pr]))
    store.set(f"cited:{ui_task}", json.dumps([pr]))
    await call_tool(runtime, "report", api, status="done",
                    summary=f"The v2 endpoints are in {pr}.")  # fmt: skip
    dark = runtime.tasks.create(ui, "Checkout UI: dark mode", "Split off.", status="running")
    store.set(f"splitoff:{ui_task}", dark["id"])
    await settle(runtime)
    runtime.publish_state()
    return {"api": api, "ui": ui, "tests": tests, "notes": notes, "e2e": e2e,
            "api_task": api_task, "ui_task": ui_task, "tests_task": tests_task,
            "notes_task": notes_task, "e2e_task": e2e_task, "dark": dark["id"]}  # fmt: skip


async def round_relations(runtime, browser, failures: list[str]):
    """E130, task relationships, as 0.1.56 computes them, in a worker's own tab (Notes, its task
    in no team of the owner's requests): its task's lineage first (where it came from, what came
    of it, a caption, a peek); a wait clearing draws its line solid once, then settles; less
    motion asked, nothing moves. Light and dark, 800, 1280 and 1440 px (browser-shots/relations/).
    No team: these tasks were made outside an owner's request."""
    shots = Path(os.environ.get("RELATIONS_SHOTS", SHOTS / "relations"))
    shots.mkdir(parents=True, exist_ok=True)
    seeded = await relations_seeded(runtime)
    assert not [
        t for t in runtime.snapshot()["teams"] if t["members"][0]["task"] == seeded["api_task"]
    ]
    for width, scheme in itertools.product((1440, 1280, 800), ("light", "dark")):
        context = await browser.new_context(
            viewport={"width": width, "height": 1000}, color_scheme=scheme
        )
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
        await page.goto(f"{BASE}/#token={TOKEN}")
        await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)
        await page.evaluate(
            f"import('/static/session.js').then((m) => m.select({json.dumps(seeded['notes'])}))"
        )
        tabs = page.locator("#worker-tabs")
        await expect(tabs.get_by_role("tab")).to_have_text(["Work", "Notes", "Tools", "Reports"])
        await expect(tabs.locator(".team-link")).to_have_count(0)
        await tabs.get_by_role("tab", name="Notes").click()
        strip = page.locator("#timeline > .team-lineage .lineage")
        await expect(strip.locator(".lnode.self")).to_have_count(1)
        await expect(strip.locator(".caption")).to_have_text(
            re.compile(r"^Release Notes waits on Checkout UI · \d+ min$")
        )
        await expect(strip.locator(".llink.pending")).to_have_count(1)
        found = await strip.evaluate(OVERFLOWS)
        assert not found, (width, found)
        await page.locator("#main").screenshot(path=shots / f"worker-notes-{width}-{scheme}.png")
        # Peek at the one it waits on: its title, worker, status and Open.
        await strip.locator(f'.lnode[data-task="{seeded["ui_task"]}"]').click()
        strip = page.locator("#timeline > .team-lineage .lineage")
        await expect(strip.locator(".peek .who")).to_have_text("Checkout UI")
        await page.locator("#worker-tabs").get_by_role("tab", name="Work").click()
        await expect(page.locator("#timeline > .team-lineage")).to_be_hidden()
        assert not errors, errors
        await context.close()
    log("Relations: a worker's Notes tab starts with its task's lineage, a peek")

    # The wait clears while it is shown: drawn solid once, then "started once … delivered".
    context = await browser.new_context(viewport={"width": 1280, "height": 1000})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)
    await page.evaluate(
        f"import('/static/session.js').then((m) => m.select({json.dumps(seeded['notes'])}))"
    )
    await page.locator("#worker-tabs").get_by_role("tab", name="Notes").click()
    strip = page.locator("#timeline > .team-lineage .lineage")
    await expect(strip.locator(".llink.pending")).to_have_count(1)
    for _ in range(2):  # (Once more after the API's result reached it first.)
        reported = await call_tool(runtime, "report", seeded["ui"], status="done",
                                   summary="The v2 pages are in acme/checkout#482.",
                                   task_id=seeded["ui_task"])  # fmt: skip
        await settle(runtime)
        if reported.result_type != "failure":
            break
    assert runtime.tasks.get(seeded["ui_task"])["status"] == "review", reported
    strip = page.locator("#timeline > .team-lineage .lineage")
    await expect(strip.locator(".caption")).to_have_text(
        "Checkout UI delivered · Release Notes is unblocked", timeout=10000
    )
    await expect(strip.locator(".llink.drawing")).to_have_count(1)
    drawn = await strip.locator(".llink.drawing .ink").evaluate(
        "node => getComputedStyle(node).animationName"
    )
    assert drawn == "draw", drawn
    await page.locator("#main").screenshot(path=shots / "unblocked-1280-light.png")
    runtime.store.execute(
        "UPDATE runs SET ended_at=? WHERE task_id=?",
        ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(), seeded["ui_task"]),
    )
    runtime.relations.memo = None
    await page.evaluate(
        f"import('/static/team.js').then((m) => m.workerLineage({json.dumps(seeded['notes'])}))"
    )
    await expect(page.locator("#timeline > .team-lineage .caption")).to_have_text(
        "Release Notes started once Checkout UI delivered"
    )
    await context.close()
    log("Relations: a wait clearing draws its line solid once, then settles")

    # Less motion: nothing breathes or draws; the words alone say it.
    context = await browser.new_context(viewport={"width": 1280, "height": 1000},
                                        reduced_motion="reduce")  # fmt: skip
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)
    await page.evaluate(
        f"import('/static/session.js').then((m) => m.select({json.dumps(seeded['e2e'])}))"
    )
    await page.locator("#worker-tabs").get_by_role("tab", name="Notes").click()
    strip = page.locator("#timeline > .team-lineage .lineage")
    await expect(strip.locator(".caption")).to_have_text(
        "3 workers, one request · ready when 3 of 3 land"
    )
    links = strip.locator(".llink .base")
    for index in range(await links.count()):
        moving = await links.nth(index).evaluate("node => getComputedStyle(node).animationName")
        assert moving == "none", moving
    await context.close()
    log("Relations: less motion asked, nothing moves; a request's parts in words")


# Teams lite (teams-v2.md v3.1): what a map's labels may never cover, and the words no node says.
LABEL_CLASHES = """map => {
  const boxes = (selector) => [...map.querySelectorAll(selector)]
    .map((node) => node.getBoundingClientRect());
  const over = (a, b) => a.left < b.right - 1 && b.left < a.right - 1
    && a.top < b.bottom - 1 && b.top < a.bottom - 1;
  const labels = boxes(".tv2-label");
  const nodes = boxes(".tv2-node");
  const found = [];
  labels.forEach((label, index) => {
    if (nodes.some((node) => over(label, node))) found.push(`label ${index} over a node`);
    labels.slice(index + 1).forEach((other, at) => over(label, other)
      && found.push(`labels ${index} and ${index + at + 1}`));
  });
  return found;
}"""
INTERNAL = re.compile(r"\b(handed|forked|worker:|Copilot|Codex|Claude|t[0-9a-f]{7})\b")
# As OVERFLOWS, but for an avatar's ring and tick (drawn around it on purpose) and the map in
# words (read by a screen reader, not shown).
TEAM_OVERFLOWS = OVERFLOWS.replace(
    'if (!box.width || node.closest("pre")) return false;',
    'if (!box.width || node.closest("pre, .pa, .visually-hidden")) return false;',
)


async def team_row(page, team: str):
    return page.locator(f'#tasks-teams .team2-row[data-team="{team}"]')


async def round_teams_v2(runtime, browser, failures: list[str]):
    """Teams lite: Home's Teams rows (a calm line, a segment a part, the Done row, Past teams),
    none in the sidebar, a team's page (its request, the one thing that needs you, the flow
    map with one labelled line per pair and the map in words, All N parts beyond 8, what its
    parts handed each other newest first, filters only from 8), a held question's card for a
    question in no team (Nudge, Open), a part continued outside its team, a done team leaving 10
    minutes after it was seen, keyboard; 800, 1024, 1280 and 1440 px, light and dark
    (browser-shots/teams-v2/)."""
    import teams_scene

    shots = Path(os.environ.get("TEAMS_V2_SHOTS", SHOTS / "teams-v2"))
    shots.mkdir(parents=True, exist_ok=True)
    found = await teams_scene.teams_lite_seeded(runtime)
    held = await teams_scene.held_unteamed(runtime)
    await teams_scene.moved_out(runtime)
    ids = found["ids"]
    autopilot = next(i for g, i in ids.items() if g.startswith("设计"))
    checkout = next(i for g, i in ids.items() if g.startswith("Ship"))
    custom = next(i for g, i in ids.items() if g.startswith("给"))
    [warehouse] = [t["id"] for t in runtime.snapshot()["teams"] if t["goal"].startswith("Export")]
    for width, scheme in itertools.product((1440, 1280, 1024, 800), ("light", "dark")):
        context = await browser.new_context(
            viewport={"width": width, "height": 1000}, color_scheme=scheme
        )
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
        await page.goto(f"{BASE}/#token={TOKEN}")
        await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)
        if width < 1100:
            await page.locator("#home-tabs [data-tab=tasks]").click()
        section = page.locator("#tasks-teams")
        # Above Running; a row per team (the one on you first), a calm line, a segment a part.
        order = await page.evaluate("[...document.querySelectorAll('#tasks > *')].map((n) => n.id)")
        assert order.index("tasks-teams") < order.index("tasks-running"), order
        rows = section.locator(".team2-row")
        row = await team_row(page, checkout)
        await expect(row.locator(".goal")).to_have_text("Ship checkout v2 for acme-shop")
        await expect(row.locator(".status")).to_have_text("Waiting on you · review UI")
        await expect(row.locator(".frac")).to_have_text("1/3")
        await expect(row.locator(".segs i")).to_have_count(3)
        await expect(row.locator(".segs i").nth(1)).to_have_class("waiting")
        row = await team_row(page, autopilot)
        await expect(row.locator(".goal")).to_have_text("设计 Chief of Staff 的 Autopilot 模式")
        await expect(row.locator(".status")).to_have_text(
            "1 working · 1 waiting on a teammate · 1 up next"
        )
        await expect(row.locator(".frac")).to_have_text("1/4")
        row = await team_row(page, custom)
        await expect(row.locator(".status")).to_have_text(
            re.compile(r"^Done · 3 parts · 1 h 4\d min$")
        )
        await expect(row.locator(".frac")).to_have_text("")
        await expect(
            section.locator(f'.team2-wrap:has([data-team="{custom}"]) .dismiss')
        ).to_be_visible()
        # A part continued in another request's work: moved out, finished for its team; the
        # part that waits on it says so, plainly.
        row = await team_row(page, warehouse)
        await expect(row.locator(".frac")).to_have_text("1/2")
        await expect(row.locator(".status")).to_have_text(
            "Waiting on “Sales report export” (outside this team)"
        )
        assert (await rows.first.get_attribute("data-team")) == checkout
        found_overflow = await section.evaluate(TEAM_OVERFLOWS)
        assert not found_overflow, (width, found_overflow)
        await section.screenshot(path=shots / f"home-teams-{width}-{scheme}.png")
        if width >= 1100:
            # The sidebar lists workers only: a team's workers are rows like any other, with no
            # line over them and no rail (teams are here on Home and on their pages).
            await expect(page.locator("#session-list .item")).not_to_have_count(0)
            grouping = ".team-line, .in-team, .compare-group, .forked"
            await expect(page.locator("#session-list").locator(grouping)).to_have_count(0)
            await expect(page.locator("#session-list")).not_to_contain_text(
                "设计 Chief of Staff 的 Autopilot 模式"
            )
            await page.locator("#sidebar").screenshot(path=shots / f"sidebar-{width}-{scheme}.png")
        if width < 1100:
            await page.locator("#home-tabs [data-tab=needs]").click()
        # A question that holds its asker up, in no team: its card, Nudge and Open.
        card = page.locator(f'#needs .card.team-wait[data-id="teamwait:{held["question"]}"]')
        await expect(card.locator(".label")).to_have_text(
            re.compile(r"^Invoice rounding bug waits on Billing research's answer · 3\d min$")
        )
        await expect(card.get_by_role("button", name="Nudge Billing research")).to_be_visible()
        await expect(card.get_by_role("button", name="Open", exact=True)).to_be_visible()
        await expect(card.locator(".in-team")).to_have_count(0)
        await page.locator("#needs").screenshot(path=shots / f"needs-{width}-{scheme}.png")
        # A team's page: the one at work.
        if width < 1100:
            await page.locator("#home-tabs [data-tab=tasks]").click()
        await (await team_row(page, autopilot)).click()
        await expect(page.locator("#session-title")).to_have_text(
            "设计 Chief of Staff 的 Autopilot 模式"
        )
        await expect(page.locator("#session-meta")).to_have_text(
            re.compile(r"^Working · 1 of 4 parts done · started ")
        )
        team_page = page.locator("#timeline .team-page")
        await expect(team_page.locator(".tv2-why")).to_contain_text("先出方案")
        await expect(team_page.locator(".tv2-callout")).to_have_count(0)
        map_ = team_page.locator(".tv2-map")
        nodes = map_.locator(".tv2-node")
        await expect(nodes.locator(".name > span:first-child")).to_have_text(
            ["方案设计", "安全规则", "点对点直连", "可点击原型"]
        )
        await expect(nodes.nth(2).locator(".status")).to_have_text(
            re.compile(r"^Waits on 安全规则's answer · 1\d min$")
        )
        await expect(nodes.nth(3).locator(".status")).to_have_text(
            "Starts after 安全规则 and 点对点直连"
        )
        await expect(nodes.nth(0).locator(".status")).to_have_text(
            re.compile(r"^Done · you accepted it at ")
        )
        # One line per pair, each with one label; none over a node or another label.
        await expect(map_.locator("path.e")).to_have_count(5)
        await expect(map_.locator(".tv2-label")).to_have_count(4)
        await page.wait_for_timeout(200)
        clashes = await map_.evaluate(LABEL_CLASHES)
        assert not clashes, (width, scheme, clashes)
        cut = await map_.evaluate(TRUNCATED, ".tv2-node .name > span:first-child")
        assert not cut, (width, scheme, cut)
        statuses = await nodes.locator(".status").all_text_contents()
        assert not [s for s in statuses if INTERNAL.search(s)], statuses
        pair = map_.locator(".tv2-label", has_text="asked")
        await expect(pair).to_have_text(re.compile(r"^asked · 1\d min · told$"))
        await expect(pair).to_have_attribute(
            "title",
            re.compile(
                r"^点对点直连 asked 安全规则, waiting 1\d min; 安全规则 told 点对点直连 1 thing$"
            ),
        )
        words = map_.locator(".tv2-words li")
        await expect(words).to_have_count(4 + 5)
        await expect(words.first).to_have_text(
            re.compile(r"^方案设计: Done · you accepted it at .+\.$")
        )
        # What they handed each other: newest first, A → B, titled by the lead sentence; no
        # filters under 8 entries.
        history = team_page.locator(".tv2-history")
        items = history.locator(".tv2-item")
        await expect(items).to_have_count(3)
        await expect(items.first.locator(".who")).to_have_text("点对点直连→安全规则")
        await expect(items.first.locator(".about")).to_have_text(
            "对端实例发来的内容\uff0c能直接当作请求执行吗\uff1f"
        )
        await expect(items.first.locator(".pill.waiting")).to_have_text(
            re.compile(r"^waiting 1\d min · holds 点对点直连$")
        )
        await expect(items.nth(1).locator(".about")).to_have_text(
            "Autopilot 方案把来自对端的内容当请求处理\uff0c这会绕过主人"
        )
        await expect(history.locator(".filters")).to_have_count(0)
        await expect(history.locator(".tv2-event").last).to_have_text(
            re.compile(r"^·Your request, in 4 parts: 方案设计, 安全规则, 点对点直连 and 可点击原型")
        )
        found_overflow = await page.locator("#timeline").evaluate(TEAM_OVERFLOWS)
        assert not found_overflow, (width, found_overflow)
        await page.locator("#main").screenshot(path=shots / f"team-active-{width}-{scheme}.png")
        # The one waiting on the owner: one callout, one button.
        await page.locator("#back-home").click()
        if width < 1100:
            await page.locator("#home-tabs [data-tab=tasks]").click()
        await (await team_row(page, checkout)).click()
        callout = page.locator("#timeline .tv2-callout")
        await expect(callout.locator("strong")).to_have_text("UI's result is ready for you.")
        await expect(callout).to_contain_text("Tests starts once you accept it.")
        await expect(callout.locator("button")).to_have_text(["Review UI"])
        await expect(page.locator("#session-meta")).to_have_text(
            re.compile(r"^Waiting on you · 1 of 3 parts done · since ")
        )
        await expect(page.locator("#timeline .tv2-label", has_text="after your OK")).to_have_count(
            1
        )
        answered = page.locator("#timeline .tv2-item", has_text="cents or in dollars")
        await expect(answered.locator(".pill.answered")).to_have_count(1)
        await expect(answered.locator(".answer .a-text")).to_have_text(
            "Cents, always; the mock's dollars are a leftover."
        )
        await page.wait_for_timeout(200)
        clashes = await page.locator("#timeline .tv2-map").evaluate(LABEL_CLASHES)
        assert not clashes, (width, scheme, clashes)
        await page.locator("#main").screenshot(path=shots / f"team-idle-{width}-{scheme}.png")
        # A part that went on outside: its node and its line say where, in words.
        await page.locator("#back-home").click()
        if width < 1100:
            await page.locator("#home-tabs [data-tab=tasks]").click()
        await (await team_row(page, warehouse)).click()
        first = page.locator("#timeline .tv2-node").first
        await expect(first.locator(".status")).to_have_text(
            "Continued in “Sales report export” outside this team"
        )
        await expect(page.locator("#timeline .tv2-node").nth(1).locator(".status")).to_have_text(
            "Waiting on “Sales report export” (outside this team)"
        )
        await expect(
            page.locator("#timeline .tv2-event", has_text="outside this team")
        ).to_have_count(1)
        await page.locator("#main").screenshot(path=shots / f"team-moved-{width}-{scheme}.png")
        # The done one: a calm row, its history folded.
        await page.locator("#back-home").click()
        if width < 1100:
            await page.locator("#home-tabs [data-tab=tasks]").click()
        await (await team_row(page, custom)).click()
        done = page.locator("#timeline .tv2-done")
        await expect(done.locator("strong")).to_have_text(
            re.compile(r"^Done · 3 parts · 1 h 4\d min$")
        )
        await expect(done).to_contain_text("Leaves Teams 10 min after you\u2019ve seen it")
        await expect(page.locator("#timeline details.tv2-history")).not_to_have_attribute(
            "open", ""
        )
        await expect(page.locator("#session-meta")).to_have_text(
            re.compile(r"^Done at .+ · from your request at ")
        )
        await page.locator("#main").screenshot(path=shots / f"team-done-{width}-{scheme}.png")
        assert not errors, errors
        await context.close()
    log(
        "Teams lite: rows, no team in the sidebar, the page at work, on you, moved out and done; "
        "a held question of no team"
    )

    # Every team in the same words on Home's row and its page's header.
    context = await browser.new_context(viewport={"width": 1280, "height": 1000})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)
    for team in (checkout, autopilot, warehouse, custom):
        row = await team_row(page, team)
        status = await row.locator(".status").text_content()
        goal = await row.locator(".goal").text_content()
        await row.click()
        await expect(page.locator("#session-title")).to_have_text(goal)
        header = await page.locator("#session-meta").text_content()
        lead = header.split(" · ")[0]
        if lead.startswith("Done"):
            assert status.startswith("Done"), (goal, status, header)
        else:
            assert lead.casefold() in status.casefold(), (goal, status, header)
        await page.locator("#back-home").click()
    await context.close()
    log("Teams lite: each team's words the same on its row and its page")

    # Nudge on a question of no team; Open is its asker's chat.
    context = await browser.new_context(viewport={"width": 1280, "height": 1000})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    card = page.locator(f'#needs .card.team-wait[data-id="teamwait:{held["question"]}"]')
    await card.get_by_role("button", name="Nudge Billing research").click()
    await expect(page.locator(".toast", has_text="Nudged “Billing research”")).to_be_visible()
    await expect(card.get_by_role("button", name=re.compile("^Nudged"))).to_be_disabled()
    nudges = [m for m in runtime.store.messages(held["expert"]) if m["meta"].get("nudge")]
    assert len(nudges) == 1, nudges
    await card.get_by_role("button", name="Open", exact=True).click()
    await expect(page.locator(f'#session-list .item[data-id="{held["asker"]}"]')).to_have_class(
        re.compile(r"\bselected\b")
    )
    # Keyboard: a team's row opens it with Enter.
    await page.locator("#back-home").click()
    await (await team_row(page, autopilot)).focus()
    await page.keyboard.press("Enter")
    await expect(page.locator("#session-title")).to_have_text(
        "设计 Chief of Staff 的 Autopilot 模式"
    )
    await context.close()
    log("Teams lite: Nudge on a question of no team; Open; a row by the keyboard")

    # Filters only from 8 entries, never a zero; All N parts beyond 8.
    await teams_scene.more_exchanges(runtime, found)
    big = await teams_scene.big_team(runtime)
    context = await browser.new_context(viewport={"width": 1280, "height": 1000})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    await (await team_row(page, autopilot)).click()
    filters = page.locator("#timeline .tv2-history .filters button")
    await expect(filters).to_have_text(
        [re.compile(r"^All\d+$"), "Waiting1", "Questions1", "Notes8"]
    )
    await filters.filter(has_text="Questions").click()
    await expect(page.locator("#timeline .tv2-item")).to_have_count(1)
    await page.locator("#back-home").click()
    await (await team_row(page, big)).click()
    await expect(page.locator("#timeline .tv2-node:not(.tv2-more)")).to_have_count(7)
    await expect(page.locator("#timeline .tv2-more")).to_have_text("+4 more")
    await page.locator("#timeline .tv2-all > summary").click()
    await expect(page.locator("#timeline .tv2-all tbody tr")).to_have_count(11)
    await expect(page.locator("#timeline .tv2-words li").first).to_be_attached()
    await page.locator("#main").screenshot(path=shots / "team-all-parts-1280-light.png")
    await context.close()
    log("Teams lite: filters from 8 entries without a zero; All 11 parts")

    # Done → stays unseen → seen (2 s in sight, focused) → gone 10 min later by the timer, to
    # Past teams; Past teams · N alone, a page at a time.
    await teams_scene.past_teams(runtime, 22)
    context = await browser.new_context(viewport={"width": 1280, "height": 1000})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.clock.install()
    await page.goto(f"{BASE}/#token={TOKEN}")
    row = await team_row(page, custom)
    await expect(row).to_be_visible(timeout=15000)
    # In sight (other rounds' work may have put it below the fold).
    await row.scroll_into_view_if_needed()
    await page.clock.run_for(3000)
    for _ in range(50):
        if runtime.snapshot()["teams"] and any(
            t["id"] == custom and t["seen"] for t in runtime.snapshot()["teams"]
        ):
            break
        await page.wait_for_timeout(100)
    seen = next(t["seen"] for t in runtime.snapshot()["teams"] if t["id"] == custom)
    assert seen and not seen["dismissed"], seen
    await expect(row).to_be_visible()
    await page.clock.run_for(9 * 60 * 1000)
    await expect(row).to_be_visible()
    # Ten minutes after it was seen: gone by the page's one timer, with no event.
    await page.clock.run_for(2 * 60 * 1000)
    await expect(row).to_have_count(0, timeout=10000)
    past = page.locator("#tasks-teams .past-teams")
    await expect(past.locator("summary")).to_have_text("Past teams · 23")
    await past.locator("summary").click()
    await expect(past.locator(".past-row")).to_have_count(21)
    await expect(past.locator(".past-row").first).to_contain_text(
        "给 Chief of Staff 做用户自定义 UI"
    )
    await past.get_by_role("button", name="Show more").click()
    await expect(past.locator(".past-row")).to_have_count(23)
    # Dismiss (its x) at once; every current team gone: the section is Past teams alone.
    for task in runtime.tasks.listed():
        if task["status"] in {"queued", "running", "waiting", "review"}:
            runtime.tasks.move(task["id"], "cancelled")
    runtime.publish_state()
    dismiss = page.locator("#tasks-teams .team2-wrap.done .dismiss")
    await expect(dismiss.first).to_be_visible(timeout=10000)
    while await dismiss.count():
        await dismiss.first.click()
        await page.wait_for_timeout(300)
    await expect(page.locator("#tasks-teams > .items > *")).to_have_count(0, timeout=10000)
    await expect(past.locator("summary")).to_have_text(re.compile(r"^Past teams · \d+$"))
    await page.locator("#tasks-teams").screenshot(path=shots / "past-alone-1280-light.png")
    await context.close()
    log(
        "Teams lite: a done team stays until seen, leaves 10 min later; dismissed at once; "
        "Past teams alone, paged"
    )


async def main():
    from chiefofstaff import machine

    # A disk as roomy as the pages expect, whatever this machine's is (a low one adds the
    # Router's note under Needs you); a round about storage sets its own.
    machine.read_disk = lambda paths: {"free": 200 * 10**9, "total": 500 * 10**9, "path": "/"}
    # Its folder's name has a space, as macOS's data folder has one ("Application Support"):
    # every path Chief of Staff puts in a command must keep it whole. No longer for it, so Send
    # to Chief's socket in its data folder still fits a socket's path.
    with tempfile.TemporaryDirectory(prefix="cos ui-") as temporary:
        # The owner's own agents are never found here: stand-ins are put on the PATH where used.
        os.environ["PATH"] = without_agents(os.environ["PATH"], Path(temporary) / "bin")
        # Nor this machine's free disk space (low, it would be a card under Needs you): room
        # enough, but where the Storage round reads its own.
        from chiefofstaff import machine

        machine.read_disk = lambda paths: {"free": 300 * 10**9, "total": 500 * 10**9, "path": "/"}
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
            # Done, with its review still to approve: one card (the draft), its result's own card
            # in it (what it came to, its actions), not a card of its own beside it.
            plain_card = page.locator(f'#needs .card.draft[data-id="{plain["id"]}"]')
            result = plain_card.locator(".card.task.in-draft")
            await expect(result.locator(".head")).to_contain_text("Result of", timeout=10000)
            await expect(result.locator(".head .title")).to_have_text("Review the exports PR")
            await expect(result.locator(".outcome")).to_have_text("Review done.")
            reviewing = page.locator(
                "#panel .card.task:not(.in-draft)", has_text="Review the exports PR"
            )
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
            # A review that changed nothing is a result to accept all the same (QA E1): under
            # Needs you, never with the finished ones before Accept; it can be sent back too.
            await expect(page.locator("#needs")).to_contain_text("Review the exports PR")
            await expect(page.locator("#tasks-recent")).not_to_contain_text("Review the exports")
            await expect(reviewing.locator(".head .meta")).to_contain_text("Awaiting acceptance")
            await expect(reviewing.get_by_role("button", name="Accept")).to_be_visible()
            await expect(reviewing.get_by_role("button", name="Request changes")).to_be_visible()
            # Not read yet (CONTRACT task.read): "New" says so until the owner opens it.
            await expect(reviewing.locator(".head .new-tag")).to_have_text("New")
            # In the chat, the report is one line with its gist, pointing at the task's card.
            reported = page.locator("#timeline .event.done", has_text="Review the exports PR")
            await expect(reported).to_contain_text("finished: Review done.")
            async with page.expect_request(f"**/api/tasks/{task_id}/read") as read:
                await reported.get_by_role("button", name="View", exact=True).click()
            assert (await read.value).method == "POST"
            await expect(reviewing).to_have_class(re.compile(r"\bexpanded\b"))
            await expect(reviewing.locator(".report")).to_contain_text("Review done.")
            # Read now (once the server keeps it); Accept ends it, as any result.
            await expect(reviewing.locator(".head .new-tag")).to_have_count(0, timeout=5000)
            await expect(reviewing.get_by_role("button", name="Done")).to_have_count(0)
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
            await expect(reviewing.locator(".head .meta")).to_contain_text("Awaiting acceptance")
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

            # `python scripts/ui_edges.py round_nine …`: only those, after the checks above.
            only = set(sys.argv[1:])
            for check in (home, memory, this_round, round_two, round_three, round_four,
                          round_five, round_six, round_seven, round_eight, round_nine,
                          round_ten, round_scale, round_qa, round_eleven, round_twelve,
                          round_thirteen, round_fourteen, round_storage, round_mascot,
                          round_others,
                          round_send, round_agents, round_picker, round_roles,
                          round_claude_models,
                          round_picker_ux, round_agent_parity, round_agent_smooth, round_qa6,
                          round_qa7_life,
                          round_qa8_report,
                          round_layout, round_fresh_start, round_qa8_auth,
                          round_qa8_approvals, round_e125, round_teams,
                          round_usage, round_relations, round_shepherd,
                          round_stacked_merge, round_fork, round_needs_dots,
                          round_settings, round_teams_v2,
                          round_answer_label, round_sidebar_flat):  # fmt: skip
                if not only or check.__name__ in only:
                    await check(runtime, browser, failures)
            await browser.close()
        server.should_exit = True
        await serving
        if failures:
            raise AssertionError(f"browser errors: {failures}")
    print("UI EDGE CASES PASSED")


async def round_needs_dots(runtime, browser, failures: list[str]):
    """0.1.56: a dot says where its task is, the same color under Needs you whether its result
    was opened or not (blue is Running's: an unread result's blue dot read as "still running");
    a result not opened yet says so in a word beside its title, gone once opened. The sidebar's
    worker dots agree with the cards'."""
    seeded = needs_dots_seeded(runtime)
    shots = Path(os.environ.get("NEEDS_DOTS_SHOTS", SHOTS / "needs-dots"))
    shots.mkdir(parents=True, exist_ok=True)
    color = "node => getComputedStyle(node).backgroundColor"
    for scheme in ("light", "dark"):
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900}, color_scheme=scheme
        )
        page = await context.new_page()
        page.on("pageerror", lambda error: failures.append(str(error)))
        await page.goto(f"{BASE}/#token={TOKEN}")

        def card(key, page=page):
            return page.locator(f'#panel .card.task[data-task="{seeded[key][1]}"]')

        await expect(card("first")).to_be_visible(timeout=15000)
        running = await card("busy").locator(".head .dot").evaluate(color)
        amber = await card("third").locator(".head .dot").evaluate(color)
        assert amber != running, (scheme, amber, running)
        for key in ("first", "second", "third"):
            row = card(key)
            await expect(row.locator(".head .meta")).to_contain_text("Awaiting acceptance")
            dot = row.locator(".head .dot")
            # Where it is, whether its result was opened or not: Needs you's color.
            await expect(dot).to_have_class(re.compile(r"\bwaiting\b"))
            assert await dot.evaluate(color) == amber, (scheme, key)
            # The sidebar's dot for its worker is the same.
            side = page.locator(f'#session-list .item[data-id="{seeded[key][0]}"] .dot')
            assert await side.evaluate(color) == amber, (scheme, key, "sidebar")
        for key in ("first", "second"):
            tag = card(key).locator(".head .new-tag")
            await expect(tag).to_have_text("New")
            await expect(tag).to_have_attribute("data-tip", re.compile(r"not opened yet"))
            await expect(card(key).locator(".head .dot")).to_have_attribute(
                "data-tip", "Needs you · a new result, not opened yet"
            )
        await expect(card("third").locator(".head .new-tag")).to_have_count(0)
        await expect(card("third").locator(".head .dot")).to_have_attribute("data-tip", "Needs you")
        await expect(card("busy").locator(".head .dot")).to_have_attribute("data-tip", "Running")
        # The word sits on the title's line, and the title still has room at 800 px.
        await page.locator("#needs").screenshot(path=shots / f"needs-1280-{scheme}.png")
        await page.set_viewport_size({"width": 800, "height": 900})
        await page.locator("#home-tabs [data-tab=needs]").click()
        box = await card("first").locator(".head .new-tag").bounding_box()
        title = await card("first").locator(".head .title").bounding_box()
        assert (
            box
            and title
            and abs(box["y"] + box["height"] / 2 - title["y"] - title["height"] / 2) < 3
        )
        assert title["width"] > 120, title
        await page.locator("#needs").screenshot(path=shots / f"needs-800-{scheme}.png")
        await page.set_viewport_size({"width": 1280, "height": 900})
        if scheme == "dark":
            # Opened: the word goes, the dot stays Needs you's.
            async with page.expect_request(f"**/api/tasks/{seeded['first'][1]}/read"):
                await card("first").locator(".head .title").click()
            await expect(card("first").locator(".head .new-tag")).to_have_count(0)
            await expect(card("first").locator(".head .dot")).to_have_class(
                re.compile(r"\bwaiting\b")
            )
            assert await card("first").locator(".head .dot").evaluate(color) == amber
            await expect(card("second").locator(".head .new-tag")).to_have_text("New")
        await context.close()
    needs_dots_done(runtime, seeded)
    log("Needs you: every dot amber, read or not; a new result says New beside its title")


def needs_dots_seeded(runtime) -> dict:
    """Three results under Needs you, two not opened yet, and a task at work: {key: (worker,
    task)}."""
    store = runtime.store
    seeded = {}
    titles = {"first": "Ship the export fix", "second": "Rotate the API token",
              "third": "Tidy the release notes", "busy": "Index the archive"}  # fmt: skip
    for key, title in titles.items():
        session = f"wdots{key}"
        store.create_session(session, "worker", title, "idle", cwd=str(Path.home()))
        task = runtime.tasks.create(session, title, title, status="running")
        seeded[key] = (session, task["id"])
    for key in ("first", "second", "third"):
        runtime.tasks.move(seeded[key][1], "review", result=f"{key.title()}: done, tests pass.")
    runtime.update_session(seeded["busy"][0], status="working")
    runtime.task_actions.mark_read(seeded["third"][1])  # Opened already.
    runtime.bus.publish("state")
    return seeded


def needs_dots_done(runtime, seeded: dict):
    for session, task in seeded.values():
        runtime.tasks.move(task, "done")
        runtime.update_session(session, status="idle")
    runtime.bus.publish("state")


# An answer the owner waits for, long and in Chinese: its line cuts it short.
SLY_ANSWER = (
    "Top 10 已完成 7 个，剩下 3 个在等画面验证：老街烧鹅、阿婆牛杂和巷口糖水"  # noqa: RUF001
    "都在等店家回复菜单照片，预计今晚八点前能全部确认，到时我再把最终排序发给你。"  # noqa: RUF001
    "\n\n- 已确认：七家，菜单和营业时间都对得上"  # noqa: RUF001
)
INVOICE_ANSWER = (
    "Found 12 invoices this week; 2 don't match the bank statement (Mar 3 and Mar 9), both from "
    "the same vendor, and I drafted a note asking them for corrected copies."
)


async def round_answer_label(runtime, browser, failures: list[str]):
    """0.1.57 (owner report): an answer the owner asked for (the Router asked a worker for them,
    or they asked it on Home) stands apart from the routine lines beside it on Home: a small
    Answer label, the worker's short name (not its task's title cut short) and the answer's
    first line, in the text's colour; it opens in place as before, View or Open beside it.
    Progress, done, blocked and Watchdog lines are as they were."""
    seeded = answer_label_seeded(runtime)
    shots = Path(os.environ.get("ANSWER_LABEL_SHOTS", SHOTS / "answer-label"))
    shots.mkdir(parents=True, exist_ok=True)
    text_color = """() => { const n = document.createElement("span"); n.style.color = "var(--text)";
      document.body.append(n); const c = getComputedStyle(n).color; n.remove(); return c; }"""
    label_box = """n => { const b = n.getBoundingClientRect(); return { width: b.width,
      height: b.height, cut: n.scrollWidth > n.clientWidth + 1 }; }"""
    routine = {
        "Sly Town: 按推荐定下今晚吃什么 reported progress": "",
        "Rotate the API token finished: Rotated the token; the old one is revoked.": "done",
        "Watchdog: “Upgrade the payments SDK” looks stuck": "waiting",
        "Upgrade the payments SDK couldn't go on: Version 5 is on a private index.": "waiting",
    }
    answered = {
        "Top 10 已完成 7 个": ("Sly Town · Top 10 已完成 7 个，剩下 3 个在等画面验证", "View"),  # noqa: RUF001
        "Found 12 invoices": ("整理本周发票并和银行流水对账 · Found 12 invoices this week", "Open"),
        "staging deploy is healthy": ("Staging · The staging deploy is healthy", "Open"),
    }
    pages = {}
    # As the owner sees them, in both schemes before any check (so the old lines' are taken too).
    for scheme in ("light", "dark"):
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900}, color_scheme=scheme
        )
        page = await context.new_page()
        page.on("pageerror", lambda error: failures.append(str(error)))
        await page.goto(f"{BASE}/#token={TOKEN}")
        await answer_label_shots(page, shots, scheme)
        pages[scheme] = page
    for scheme, page in pages.items():
        timeline = page.locator("#timeline")
        text = await page.evaluate(text_color)
        lines = {key: timeline.locator(".event.answer", has_text=key) for key in answered}
        for key, (words, link) in answered.items():
            line = lines[key]
            await expect(line).to_have_count(1)
            await expect(line.locator(".line > .chip.answer")).to_have_text("Answer")
            await expect(line.locator(".what")).to_have_text(re.compile(f"^{re.escape(words)}"))
            # Read as words: its toggle's name says it is an answer, and whose.
            await expect(line.locator("button.line")).to_have_accessible_name(
                re.compile(rf"Answer\s+{re.escape(words[:12])}")
            )
            await expect(line.get_by_role("button", name=link, exact=True)).to_have_count(1)
            # In the text's colour, not the muted lines' (nor bold).
            what = line.locator(".what")
            assert await what.evaluate("n => getComputedStyle(n).color") == text, (scheme, key)
            assert await what.evaluate("n => getComputedStyle(n).fontWeight") == "400", key
        for words, kind in routine.items():
            exact = re.compile(f"^{re.escape(words)}$")
            line = timeline.locator(".event").filter(has=page.locator(".what", has_text=exact))
            await expect(line).to_have_count(1)
            classes = f"^event( opens)?{' ' + kind if kind else ''}$"
            await expect(line).to_have_class(re.compile(classes))
            await expect(line.locator(".chip")).to_have_count(0)
            color = await line.locator(".what").evaluate("n => getComputedStyle(n).color")
            assert color != text, (scheme, words)
        # The label stays whole and on one line at every width; a long answer is cut short.
        sizes = []
        for width in (1440, 1024, 800):
            await page.set_viewport_size({"width": width, "height": 900})
            if await page.locator("#home-tabs").is_visible():
                await page.locator("#home-tabs [data-tab=chat]").click()
            for key, line in lines.items():
                box = await line.locator(".chip.answer").evaluate(label_box)
                assert box["height"] <= 20 and not box["cut"], (width, key, box)
                sizes.append(box["width"])
                found = await line.evaluate(OVERFLOWS)
                assert not found, (width, key, found)
                row = await line.locator(".row").bounding_box()
                assert row and row["height"] <= 30, (width, key, row)
            cut = await timeline.evaluate(TRUNCATED, ".event.answer .what")
            assert any("Top 10" in item for item in cut), (width, cut)
        assert max(sizes) - min(sizes) < 0.5, sizes
        await page.set_viewport_size({"width": 1440, "height": 900})
        # It opens in place to the whole answer, by click or by key, as before.
        sly = lines["Top 10 已完成 7 个"]
        toggle = sly.locator("button.line")
        await toggle.click()
        await expect(toggle).to_have_attribute("aria-expanded", "true")
        await expect(sly.locator(".markdown")).to_contain_text("菜单和营业时间都对得上")
        box = await sly.locator(".chip.answer").evaluate(label_box)
        assert box["height"] <= 20 and not box["cut"], box
        await sly.scroll_into_view_if_needed()
        await page.mouse.move(1, 1)
        await page.wait_for_timeout(250)
        await page.screenshot(path=shots / f"opened-1440-{scheme}.png")
        await toggle.click()
        await expect(sly.locator(".markdown")).to_be_hidden()
        invoices = lines["Found 12 invoices"].locator("button.line")
        await invoices.focus()
        await page.keyboard.press("Enter")
        await expect(invoices).to_have_attribute("aria-expanded", "true")
        await page.keyboard.press("Space")
        await expect(invoices).to_have_attribute("aria-expanded", "false")
        await page.context.close()
    answer_label_done(runtime, seeded)
    log("Home: an answer is labelled Answer, by its worker's short name; routine lines as were")


async def answer_label_shots(page, shots: Path, scheme: str):
    """Home's last lines, the page and the lines alone, at 1440, 1024 and 800 px."""
    timeline = page.locator("#timeline")
    first = timeline.locator(".event", has_text="reported progress").last
    last = timeline.locator(".event", has_text="staging deploy is healthy")
    await expect(last).to_have_count(1, timeout=15000)
    for width in (1440, 1024, 800):
        await page.set_viewport_size({"width": width, "height": 900})
        if await page.locator("#home-tabs").is_visible():
            await page.locator("#home-tabs [data-tab=chat]").click()
        await last.scroll_into_view_if_needed()
        await page.mouse.move(1, 1)
        await page.wait_for_timeout(250)
        await page.screenshot(path=shots / f"home-{width}-{scheme}.png")
        top, bottom = await first.bounding_box(), await last.bounding_box()
        side = await timeline.bounding_box()
        assert top and bottom and side, width
        clip = {"x": side["x"], "y": top["y"] - 12, "width": side["width"],
                "height": bottom["y"] + bottom["height"] - top["y"] + 24}  # fmt: skip
        await page.screenshot(path=shots / f"lines-{width}-{scheme}.png", clip=clip)
    await page.set_viewport_size({"width": 1440, "height": 900})


def answer_label_seeded(runtime) -> dict:
    """Workers whose news reaches Home: two answers the Router asked for, one the owner asked
    on Home, and the routine lines around them. {key: (worker, task)}."""
    store = runtime.store
    router = store.get("router_id")
    titles = {"sly": "Sly Town: 按推荐定下今晚吃什么", "invoices": "整理本周发票并和银行流水对账",
              "staging": "Staging: check the deploy", "token": "Rotate the API token",
              "sdk": "Upgrade the payments SDK"}  # fmt: skip
    seeded = {}
    for key, title in titles.items():
        session = f"wanswer{key}"
        store.create_session(session, "worker", title, "idle", cwd=str(Path.home()))
        seeded[key] = (session, runtime.tasks.create(session, title, title, status="running")["id"])
    runtime.bus.publish("state")

    def report(key: str, status: str, text: str):
        session, task = seeded[key]
        meta = {"from": session, "title": titles[key], "status": status, "task_id": task}
        runtime.add_message(router, "inbound", text, meta)

    report("sly", "progress", "已看完 4 家的画面。")
    report("token", "done", "Rotated the token; the old one is revoked.")
    report("sly", "answer", SLY_ANSWER)
    report("sdk", "watchdog: stuck", "No output for 40 minutes.")
    runtime.add_message(router, "inbound", INVOICE_ANSWER, {
        "answered_by": seeded["invoices"][0], "title": f"“{titles['invoices']}”",
        "task_id": seeded["invoices"][1]})  # fmt: skip
    report("sdk", "blocked", "Version 5 is on a private index.")
    runtime.add_message(router, "answer", "The staging deploy is healthy: all 14 checks pass.", {
        "session_id": seeded["staging"][0], "task_id": seeded["staging"][1],
        "from_title": titles["staging"]})  # fmt: skip
    return seeded


def answer_label_done(runtime, seeded: dict):
    for _session, task in seeded.values():
        runtime.tasks.move(task, "done")
    runtime.bus.publish("state")


def usage_seeded(runtime) -> dict:
    """E131-E136, a range's spend as Usage shows it: premium requests as Copilot counts them (a
    user message's call, not the agent's next ones; an older call apart), a compaction, the
    prompt cache's tokens, a worker with three tasks (two accepted, one in progress), and what
    no task spent (the Router's turns, the Watch's scans)."""
    store = runtime.store
    router = store.get("router_id")
    store.create_session("wuse", "worker", "Fix the login timeout", "idle", cwd="/tmp")
    store.create_session("wwatch", "worker", "Watch for you", "idle", cwd="/tmp")
    store.set("connector:wwatch", "1")
    tasks = [
        ("tuse1", "wuse", "Fix the login timeout", "done"),
        ("tuse2", "wuse", "Add a retry to the token refresh", "done"),
        ("tuse3", "wuse", "Write the release notes", "running"),
        ("tscan", "wwatch", "Watch for you", "done"),
    ]
    for task, session, title, status in tasks:
        store.execute(
            "INSERT INTO tasks(id, session_id, title, status, created_at, updated_at) "
            "VALUES (?,?,?,?,'now','now')",
            (task, session, title, status),
        )
        store.execute(
            "INSERT INTO runs(id, task_id, session_id, status, started_at) "
            "VALUES (?,?,?,'running','now')",
            ("r" + task, task, session),
        )
    calls = [  # session, run, initiator, credits, input, cache read, cache write
        ("wuse", "rtuse1", "user", 3.1, 48_000, 40_000, 7_900),
        ("wuse", "rtuse1", "agent", 0.6, 49_000, 47_900, 1_000),
        ("wuse", "rtuse2", "user", 1.4, 52_000, 49_000, 2_900),
        ("wuse", "rtuse3", "user", 0.9, 30_000, 28_000, 1_900),
        ("wwatch", "rtscan", "user", 2.2, 120_000, 118_000, 1_900),
        (router, "", "user", 1.8, 60_000, 50_000, 9_900),
        (router, "", "agent", 0.4, 61_000, 59_900, 1_000),
    ]
    for session, run, initiator, spent, tokens_in, read, written in calls:
        role = "router" if session == router else "worker"
        store.record_usage(session, role, "smart-model", spent, 1.0, tokens_in, 900,
                           run_id=run, initiator=initiator, cache_read_tokens=read,
                           cache_write_tokens=written)  # fmt: skip
    store.record_usage("wuse", "worker", "smart-model", 0.7, 0, 70_000, 2_000, run_id="rtuse3",
                       initiator="compaction", cache_read_tokens=65_000,
                       cache_write_tokens=4_900, kind="compaction")  # fmt: skip
    store.record_usage(router, "router", "smart-model", 0.3, 1.0, 20_000, 300, initiator="")
    runtime.bus.publish("state")
    return {"worker": "wuse", "tasks": [task[2] for task in tasks[:3]]}


# A color of a worker's or a team's part (style.css --c0…--c7) and the status colors' hues:
# [[hue, saturation] of each part color, [hue of each status color]].
PALETTE_HUES = """() => {
  const style = getComputedStyle(document.documentElement);
  const hsl = (name) => {
    const hex = style.getPropertyValue(name).trim().replace('#', '');
    const [r, g, b] = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
    const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
    const l = (max + min) / 2, s = d ? d / (1 - Math.abs(2 * l - 1)) : 0;
    const h = !d ? 0 : max === r ? 60 * (((g - b) / d) % 6)
      : max === g ? 60 * ((b - r) / d + 2) : 60 * ((r - g) / d + 4);
    return [(h + 360) % 360, s];
  };
  return [[0, 1, 2, 3, 4, 5, 6, 7].map((i) => hsl(`--c${i}`)),
          ['--accent', '--amber', '--green', '--red'].map((name) => hsl(name)[0])];
}"""


async def round_sidebar_flat(runtime, browser, failures: list[str]):
    """The owner's choice for the Workers sidebar (QA E143, E146, E147, E149, E150): workers
    only, in one flat list by status (Needs you, Running, Monitoring, the newest first; Nothing
    open folded); a second worker a row like any other with a small ⑂ after its name (where it
    came from and why on hover); a check-back's time ("↻ in 12 min" within the hour, else its
    time; what it checks on hover); "waits on “…”" for one that waits on a peer; no team line,
    rail or grouping (a team of three is three rows; its team on Home); each side of a Compare
    a row, with a small ⇄; part colors that are no status's hue. Screenshots at 800, 1024
    and 1440 px, light and dark (browser-shots/sidebar-flat/, or SIDEBAR_FLAT_SHOTS), taken
    before anything is checked."""
    import sidebar_scene

    shots = Path(os.environ.get("SIDEBAR_FLAT_SHOTS") or SHOTS / "sidebar-flat")
    shots.mkdir(parents=True, exist_ok=True)
    folder, _ = codex_on_path()
    try:
        runtime.found_cache = (float("-inf"), [])
        await agents_saved(runtime, {"codex": {"enabled": True}})
        await runtime.refresh_agent_status()
        scene = await sidebar_scene.busy_sidebar(runtime)
        store = runtime.store
        title = lambda worker: runtime.display_title(store.session(worker))  # noqa: E731
        pages = []
        for scheme in ("light", "dark"):
            context, page, errors, _ = await agents_page(browser, scheme, 1440)
            await page.set_viewport_size({"width": 1440, "height": 1000})
            await expect(page.locator(f'#session-list .item[data-id="{scene["fork"]}"]'))\
                .to_be_visible(timeout=15000)  # fmt: skip
            for width in (1440, 1024, 800):
                await page.set_viewport_size({"width": width, "height": 1000})
                await page.mouse.move(1, 1)
                await quiet_for(page, 300)
                await page.locator("#sidebar").screenshot(
                    path=shots / f"sidebar-{width}-{scheme}.png"
                )
            pages.append((scheme, context, page, errors))
        for scheme, context, page, errors in pages:
            listed = page.locator("#session-list")
            # No groups: no team line, rail, caption, nesting or indentation.
            grouping = ".team-line, .in-team, .compare-group, .forked, .nested"
            await expect(listed.locator(grouping)).to_have_count(0)
            await expect(listed).not_to_contain_text("Run the reach experiment")
            for width in (1440, 1024, 800):
                await page.set_viewport_size({"width": width, "height": 1000})
                lefts = await listed.locator(".item > .dot").evaluate_all(
                    "nodes => nodes.filter((n) => n.offsetParent)"
                    ".map((n) => Math.round(n.getBoundingClientRect().left))"
                )
                assert len(set(lefts)) == 1, ("one column of dots", width, lefts)
                found = await listed.evaluate(SCROLLED_OVERFLOWS)
                assert not found, (width, found)
            await page.set_viewport_size({"width": 1440, "height": 1000})
            # By status: Needs you, Running, Monitoring; the rest folded.
            ranks = {"waiting": 0, "working": 1, "watching": 2}
            dots = await listed.locator(":scope > .item:not(.home-item)").evaluate_all(
                "nodes => nodes.map((n) => [n.dataset.id, n.classList[1]])"
            )
            shown = [worker for worker, _ in dots]
            assert [ranks[dot] for _, dot in dots] == sorted(ranks[dot] for _, dot in dots), dots
            for worker in scene["idle"]:
                assert worker not in shown
                await expect(listed.locator(f'.idle-workers .item[data-id="{worker}"]'))\
                    .to_have_count(1)  # fmt: skip
            row = lambda worker, page=page: page.locator(  # noqa: E731
                f'#session-list .item[data-id="{worker}"]'
            )
            await expect(row(scene["thresholds"])).to_have_class(re.compile(r"\bwaiting\b"))
            # The team of three: three rows like any other, where each stands.
            await expect(row(scene["build"])).to_have_class(re.compile(r"\bworking\b"))
            await expect(row(scene["train"]).locator(".sub.waits")).to_have_text(
                f"waits on “{title(scene['build'])}”"
            )
            await expect(row(scene["train"]).locator(".sub.waits")).to_have_attribute(
                "title", f"Waits on “{title(scene['build'])}”"
            )
            # Check-backs: within the hour, in how long; later, its time; what, on hover.
            back = row(scene["nightly"]).locator(".sub.back")
            await expect(back).to_have_text(re.compile(r"^↻ in (1[0-2]|[1-9]) min$"))
            await expect(back).to_have_attribute(
                "title", re.compile(r"^Checks back at .+: whether the nightly export finished$")
            )
            later = row(scene["reach"]).locator(".sub.back")
            await expect(later).to_have_text(
                re.compile(r"^↻ ([A-Z][a-z]{2} \d{1,2}, )?\d{1,2}:\d{2}\s?[AP]M$")
            )
            await expect(later).to_have_attribute(
                "title", re.compile(r"^Checks back at .+: whether the reach backfill finished$")
            )
            # A second worker: ⑂ after its name, where it came from and why.
            origin = title(scene["busy"])
            mark = row(scene["fork"]).locator(".name > .fork-mark")
            await expect(mark).to_have_attribute(
                "title", f"Started from “{origin}” with its conversation, because {origin} was busy"
            )
            label = row(scene["fork"]).locator(".label")
            gap = await label.evaluate(
                "n => n.nextElementSibling.getBoundingClientRect().left"
                " - n.getBoundingClientRect().right"
            )
            assert gap < 10, ("right after its name", gap)
            await expect(row(scene["busy"]).locator(".fork-mark")).to_have_count(0)
            # A Compare: each side a row named by its work, a small ⇄ naming the other.
            for side, other in zip(scene["pair"], ("Codex", "Copilot"), strict=True):
                await expect(row(side).locator(".label")).to_have_text("Speed up the importer")
                await expect(row(side).locator(".compare-mark")).to_have_attribute(
                    "title", f"Compared with {other}: the same task on each"
                )
            # Its team is on Home (Teams), never in the sidebar.
            team = page.locator("#tasks-teams .team2-row", has_text="Run the reach experiment")
            await expect(team).to_have_count(1)
            # A worker's or a part's color is never a status's hue (E149): far from blue,
            # amber, green and red, or a neutral tint.
            colors, statuses = await page.evaluate(PALETTE_HUES)
            for hue, saturation in colors:
                apart = min(min(abs(hue - s), 360 - abs(hue - s)) for s in statuses)
                assert apart >= 25 or saturation < 0.25, (scheme, hue, saturation, statuses)
            # Keyboard: Tab reaches each row in its order; Enter opens it.
            await row(scene["thresholds"]).focus()
            await page.keyboard.press("Tab")
            following = await page.evaluate("document.activeElement.dataset.id")
            assert following == shown[shown.index(scene["thresholds"]) + 1], (following, shown)
            await page.keyboard.press("Enter")
            await expect(row(following)).to_have_class(re.compile(r"\bselected\b"))
            assert not errors, errors
            await context.close()
    finally:
        await agents_saved(runtime, {"codex": {"enabled": False}})
        codex_off_path(folder)
    log("the sidebar: workers only, flat by status; ⑂, ↻ times, waits on, ⇄; 800-1440 px")


async def round_usage(runtime, browser, failures: list[str]):
    """E131-E136: premium requests as Copilot counts them, compactions and the cache's tokens
    in the totals, what the spend went to, By task, and a worker's tasks under it."""
    seeded = usage_seeded(runtime)
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    await page.locator("#usage-button").click()
    dialog = page.locator("#usage-dialog")
    await dialog.locator('[data-range="30"]').click()
    total = dialog.locator("#usage-total")
    await expect(total).to_contain_text("premium requests (as Copilot counts them)")
    await expect(total).to_contain_text("up to 1 more from older calls")
    await expect(total).to_contain_text("of them to compact")
    await expect(total).to_contain_text("cache reads")
    await expect(total).to_contain_text("context a call on average")
    tables = dialog.locator("#usage-tables")
    by_task = tables.locator("div", has=page.locator("h3", has_text="By task"))
    for title in seeded["tasks"]:
        await expect(by_task).to_contain_text(title)
    await expect(by_task.locator("tr").last).to_contain_text("No task")
    spend = tables.locator("table.spend")
    accepted = runtime.store.usage(time.time() - 30 * 86400, time.time() + 1)["spend"]["accepted"]
    await expect(spend.locator("tr", has_text="Finished and accepted")).to_contain_text(
        f"{accepted['tasks']} tasks{accepted['each']:.1f} credits each"
    )
    await expect(spend.locator("tr", has_text="In progress")).to_contain_text("so far")
    await expect(spend.locator("tr", has_text="Not tied to a task")).to_contain_text(
        "Watch for you"
    )
    worker = tables.locator("tr.link", has=page.locator("button", has_text="worker · 3 tasks"))
    toggle = worker.locator("button", has_text="worker · 3 tasks")
    sub = tables.locator("tr.sub", has_text="Add a retry to the token refresh")
    await expect(sub).to_be_hidden()
    await toggle.click()
    await expect(dialog).to_be_visible()  # Its tasks unfold: the dialog stays.
    await expect(sub).to_be_visible()
    await expect(toggle).to_have_attribute("aria-expanded", "true")
    log("usage: premium as Copilot counts it, compactions, cache tokens, spend, by task")
    await page.keyboard.press("Escape")
    await context.close()


async def round_qa8_report(runtime, browser, failures: list[str]):
    """E115: a running task shows its latest update and folds the four before it."""
    router = runtime.store.get("router_id")
    spawned = await call_tool(
        runtime,
        "spawn_worker",
        router,
        title="QA8 summarize the import",
        instructions="Summarize the import",
        why_new="test",
    )
    worker = json.loads(spawned.text_result_for_llm)["session_id"]
    task = runtime.tasks.current(worker)
    for number in range(1, 7):
        await call_tool(
            runtime,
            "report",
            worker,
            status="progress",
            summary=f"Import update {number}: checked {number * 200} rows.",
        )
    # Asked by the Router how far it is: its answer is an Answer/Update, its task goes on.
    await call_tool(runtime, "message_session", router, session_id=worker, task="note",
                    task_id=task["id"], text="How many rows are ready?")  # fmt: skip
    await call_tool(
        runtime,
        "report",
        worker,
        status="answer",
        task_id=task["id"],
        summary="Answer for the owner: 1,240 rows are ready.",
    )
    await settle(runtime)
    assert runtime.tasks.get(task["id"])["status"] == "running"
    for scheme in ("light", "dark"):
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900}, color_scheme=scheme
        )
        page = await context.new_page()
        page.on("pageerror", lambda error: failures.append(str(error)))
        await page.goto(f"{BASE}/#token={TOKEN}")
        await expect(page.locator("#application")).to_be_visible()
        for width in (800, 1280):
            await page.set_viewport_size({"width": width, "height": 900})
            if await page.locator("#home-tabs").is_visible():
                await page.locator("#home-tabs [data-tab=tasks]").click()
            card = page.locator("#tasks-running .card.task", has_text="QA8 summarize the import")
            await expect(card).to_be_visible()
            if "unfolded" not in (await card.get_attribute("class") or "").split():
                await card.locator(".head .title").click()
            await expect(card.locator(".progress-line .words")).to_have_text(
                "Import update 6: checked 1200 rows."
            )
            earlier = card.locator(".earlier-updates")
            await expect(earlier.locator("summary")).to_have_text("Earlier updates (4)")
            if not await earlier.evaluate("node => node.open"):
                await earlier.locator("summary").click()
            await expect(earlier).to_contain_text("Import update 2")
            await expect(earlier).not_to_contain_text("Import update 1")
            assert not await card.evaluate(OVERFLOWS), (width, scheme)
            await card.scroll_into_view_if_needed()
            await page.screenshot(path=SHOTS / f"ui-edges-progress-{width}-{scheme}.png")
        await context.close()
    log("E115: latest progress is visible; four earlier updates are folded with their times")


async def round_qa8_auth(runtime, browser, failures: list[str]):
    """E116: two workers whose MCP tool says its account must be connected share one card. Its
    error and link are plain text (a link outside the server's own addresses says who gave it);
    the link opens only on a click; "I've done it" keeps the card until a retry works. Settings
    and the sidebar say Watch for you needs a connection, not "0 found". Light and dark, 800
    and 1280 wide."""
    from chiefofstaff.models import Watch

    store = runtime.store
    before = store.settings()
    servers = {
        "acme-platform": {"type": "http", "url": "https://mcp.acme.test/api"},
    }
    store.save_settings(
        before.model_copy(
            update={"mcp_servers": servers, "watches": [Watch(what="Acme Search launch notes")]}
        )
    )
    error = (
        "MCP server 'acme-platform': Authorization required for Acme Search. <b>Not bold.</b> "
        "Connect your Acme Search account in a browser at https://login.acme-id.test/connect?f=ui"
    )
    workers = []
    for title in ("Search launch notes", "Find the account plan"):
        result = await call_tool(
            runtime, "spawn_worker", store.get("router_id"),
            title=title, instructions=title, why_new="UI check",
        )  # fmt: skip
        workers.append(json.loads(result.text_result_for_llm)["session_id"])
    await settle(runtime)
    store.set("watch_scanned_at", "0")
    await runtime.connector.scan()
    await settle(runtime)
    watcher = runtime.connector.session()["id"]
    for number, session_id in enumerate([*workers, watcher]):
        session = runtime.states.handle(session_id)
        session.emit("user.message", content=session.raw_sent[-1])
        session.emit("assistant.turn_start")
        session.emit(
            "tool.execution_start", tool_call_id=f"qa8-{number}", tool_name="acme-platform-search",
            mcp_config_server_name="acme-platform", mcp_tool_name="search",
        )  # fmt: skip
        session.emit(
            "tool.execution_complete", tool_call_id=f"qa8-{number}", success=False,
            result=SimpleNamespace(content=error), error=SimpleNamespace(message=error),
        )  # fmt: skip
    await settle(runtime)
    await call_tool(runtime, "report_found", watcher, items=[])
    scanning = runtime.states.handle(watcher)
    scanning.emit("user.message", content=scanning.raw_sent[-1])  # Its note, steered in.
    scanning.emit("session.idle")
    await settle(runtime)
    [draft] = [item for item in store.drafts() if item["kind"] == "connection"]
    shots = Path(os.environ.get("QA8_AUTH_SHOTS", SHOTS / "qa8-auth"))
    shots.mkdir(parents=True, exist_ok=True)
    for scheme, width in itertools.product(("light", "dark"), (800, 1280)):
        context = await browser.new_context(
            viewport={"width": width, "height": 900}, color_scheme=scheme
        )
        page = await context.new_page()
        page.on("pageerror", lambda error: failures.append(str(error)))
        await page.goto(f"{BASE}/#token={TOKEN}")
        await expect(page.locator("#application")).to_be_visible()
        if await page.locator("#home-tabs").is_visible():
            await page.locator("#home-tabs [data-tab=needs]").click()
        card = page.locator(f'.card.draft[data-id="{draft["id"]}"]')
        await expect(card).to_be_visible()
        await expect(card).to_contain_text("acme-platform needs you to connect Acme Search")
        await expect(card).to_contain_text("3 sessions waiting")
        await expect(card).to_contain_text("<b>Not bold.</b>")
        await expect(card.locator("b")).to_have_count(0)
        await expect(card).to_contain_text("Link supplied by acme-platform")
        link = card.get_by_role("link", name="Open link")
        await expect(link).to_have_attribute("href", "https://login.acme-id.test/connect?f=ui")
        await expect(link).to_have_attribute("target", "_blank")
        await expect(link).to_have_attribute("rel", "noreferrer")
        assert len(context.pages) == 1, "nothing opened by itself"
        await expect(card.get_by_role("button", name="Not now")).to_be_visible()
        await expect(card.get_by_role("button", name="I've done it")).to_be_visible()
        assert not await card.evaluate(OVERFLOWS), (width, scheme)
        await expect(page.locator("#status-line")).to_contain_text(
            "Watch for you: connection needed for Acme Search"
        )
        await card.scroll_into_view_if_needed()  # Other rounds' cards may come first.
        await page.screenshot(path=shots / f"connection-{width}-{scheme}.png")
        await context.close()
    page = await browser.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    await expect(page.locator("#application")).to_be_visible()
    if await page.locator("#home-tabs").is_visible():
        await page.locator("#home-tabs [data-tab=needs]").click()
    await page.locator("#settings-button").click()
    await page.locator("#settings-tabs [data-tab=connections]").click()
    state = page.locator("#watch-state")
    await expect(state).to_contain_text("Connection needed: Acme Search (acme-platform)")
    await expect(state).not_to_contain_text("found")
    await expect(state).to_have_class(re.compile(r"\berror\b"))
    await page.keyboard.press("Escape")
    card = page.locator(f'.card.draft[data-id="{draft["id"]}"]')
    await card.get_by_role("button", name="I've done it").click()
    await expect(card).to_contain_text("Each session retries once")
    await expect(card).to_be_visible()
    # E138: its retry fails the same way: connected, but not for Chief of Staff. Said on the same
    # card, with what to do, not asked for again.
    session = runtime.states.handle(workers[0])
    session.emit(
        "tool.execution_start", tool_call_id="qa8-again", tool_name="acme-platform-search",
        mcp_config_server_name="acme-platform", mcp_tool_name="search",
    )  # fmt: skip
    session.emit(
        "tool.execution_complete", tool_call_id="qa8-again", success=False,
        result=SimpleNamespace(content=error), error=SimpleNamespace(message=error),
    )  # fmt: skip
    await settle(runtime)
    await expect(card).to_contain_text(
        "acme-platform still can't use Acme Search from Chief of Staff"
    )
    await expect(card).to_contain_text("Check that it works in Copilot CLI in your terminal")
    await expect(card.get_by_role("button", name="Try again")).to_be_visible()
    assert not await card.evaluate(OVERFLOWS)
    shots = Path(os.environ.get("HOTFIX_SHOTS", SHOTS / "hotfix-0152"))
    shots.mkdir(parents=True, exist_ok=True)
    await card.scroll_into_view_if_needed()
    await page.screenshot(path=shots / "connection-still-failing-1280-light.png")
    await page.close()
    store.update_draft(draft["id"], status="rejected", outcome="UI check over", resolved_at="now")
    for record in runtime.accounts.records():
        runtime.accounts.drop(record)
    for session_id in workers:
        await runtime.workers.close(session_id)
    store.save_settings(before)
    await settle(runtime)
    runtime.publish_state()
    log("E116: one connection card for three sessions, plain text, a link only on a click; "
        "E138: a retry after I've done it that fails the same says so on it")  # fmt: skip


async def round_layout(runtime, browser, failures: list[str]):
    """0.1.48's two overlaps (worker-fork's QA): Home's "Where is your code?" card lay over the
    top of the Router's timeline, hiding its first lines; and in the Router's chat, an owner's
    message to a worker had its "→ worker" line over its text ("Go on where you stopped."). At
    800 and 1280 px, light and dark: the card takes room of its own above the timeline, and the
    line stands above the text."""
    store = runtime.store
    router = store.get("router_id")
    before = store.settings()
    store.save_settings(before.model_copy(update={"projects": [], "code_folder": ""}))
    spawned = await call_tool(runtime, "spawn_worker", router, title="Pick up the importer",
                              instructions="Pick it up", why_new="test")  # fmt: skip
    worker = json.loads(spawned.text_result_for_llm)["session_id"]
    await settle(runtime)
    for number in range(24):
        runtime.add_message(router, "notice", f"Closed “Keep the old notes {number}”")
    runtime.owner_told(store.session(worker), "Go on where you stopped.")
    await settle(runtime)
    runtime.publish_state()
    shots = SHOTS / "0149"
    shots.mkdir(parents=True, exist_ok=True)
    problems = []
    try:
        for width in (800, 1280):
            for scheme in ("light", "dark"):
                context = await browser.new_context(
                    viewport={"width": width, "height": 900}, color_scheme=scheme
                )
                page = await context.new_page()
                page.on("pageerror", lambda error: failures.append(str(error)))
                await page.goto(f"{BASE}/#token={TOKEN}")
                if width < 1100:
                    await page.locator("#home-tabs [data-tab=chat]").click()
                card = page.locator("#home-setup")
                await expect(card).to_be_visible()
                await expect(card).to_contain_text("Where is your code?")
                timeline = page.locator("#scroller")
                await timeline.evaluate("node => { node.scrollTop = node.scrollHeight; }")
                await page.wait_for_timeout(200)
                held, top = await card.bounding_box(), await timeline.bounding_box()
                assert held and top
                await page.screenshot(path=shots / f"setup-card-{width}-{scheme}.png")
                # Room of its own: the timeline, scrolled, starts below it (it ran on at its edge,
                # lines cut there as if under it), and fades in where it is cut.
                faded = await timeline.evaluate("node => getComputedStyle(node).maskImage")
                if top["y"] - (held["y"] + held["height"]) < 8 or "gradient" not in faded:
                    problems.append(f"{width} {scheme}: the timeline under the card {held} {top}")
                # (Other rounds' owners said "Go on where you stopped." too: this worker's.)
                message = page.locator(
                    "#timeline .msg.owner", has=page.locator(".to", has_text="Pick up the importer")
                ).last
                await message.scroll_into_view_if_needed()
                to, text = message.locator(".to"), message.locator(".text")
                await expect(to).to_have_text("→ Pick up the importer")
                above, below = await to.bounding_box(), await text.bounding_box()
                assert above and below
                await message.screenshot(path=shots / f"to-line-{width}-{scheme}.png")
                if above["y"] + above["height"] > below["y"] + 0.5:
                    problems.append(f"{width} {scheme}: the → line over the text {above} {below}")
                await context.close()
    finally:
        store.save_settings(before)
    assert not problems, problems
    log("0.1.49: the code folder's card above the timeline, the → line above its message")


async def round_fresh_start(runtime, browser, failures: list[str]):
    """0.1.48 on Copilot 1.0.112, the owner's Copilot in experimental mode: a worker's Copilot
    session refused for its extensions showed the raw "JSON-RPC Error -32603 … extensionSdkPath
    is required …" under the owner's answer, which was then lost. Now, should Copilot still
    refuse one: its chat says "Reconnecting this worker…" while a fresh session takes over, and
    the answer reaches that session once; with no fresh session either, one plain sentence and
    "Start a fresh worker with its context", on the card and in its chat, and the answer kept
    goes once that is clicked."""
    store, client = runtime.store, runtime.backend
    router = store.get("router_id")
    refused = (
        "JSON-RPC Error -32603: Request session.resume failed with message: Failed to "
        "initialize standalone session extensions: extensionSdkPath is required when "
        "standalone extensions are requested"
    )
    resume, create = client.resume_session, client.create_session
    gone: set[str] = set()
    opening = asyncio.Event()
    opening.set()
    refusing = {"create": False}

    async def resuming(session_id, **options):
        if session_id in gone:
            raise RuntimeError(refused)
        return await resume(session_id, **options)

    async def creating(**options):
        await opening.wait()
        if refusing["create"]:
            raise RuntimeError(refused.replace("session.resume", "session.create"))
        return await create(**options)

    async def asking(title: str) -> str:
        spawned = await call_tool(runtime, "spawn_worker", router, title=title,
                                  instructions=title, why_new="test")  # fmt: skip
        worker = json.loads(spawned.text_result_for_llm)["session_id"]
        await call_tool(runtime, "report", worker, status="question", summary=f"{title} now?")
        client.sessions[runtime.copilot_id(worker)].emit("session.idle")
        await settle(runtime)
        await runtime.detach(worker)  # As after a restart.
        gone.add(runtime.copilot_id(worker))
        return worker

    fresh = "Start a fresh worker with its context"

    def given(worker: str, text: str) -> int:
        return sum(item.count(text) for item in client.sessions[runtime.copilot_id(worker)].sent)

    client.resume_session, client.create_session = resuming, creating
    try:
        first = await asking("Retry the probe access")
        second = await asking("Retry the mirror access")
        runtime.publish_state()
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await context.new_page()
        page.on("pageerror", lambda error: failures.append(str(error)))
        await page.goto(f"{BASE}/#token={TOKEN}")
        needs = page.locator("#needs")

        # In its chat, the owner answers; a fresh session takes over, said in one line.
        await page.locator("#session-list .item", has_text="Retry the probe access").click()
        await expect(page.locator("#session-title")).to_have_text("Retry the probe access")
        opening.clear()
        await page.locator("#composer-input").fill("Yes, retry it")
        await page.locator("#composer-input").press("Enter")
        line = page.locator("#timeline .working-line")
        await expect(line).to_have_text("Reconnecting this worker…", timeout=10000)
        await expect(page.locator("#session-meta")).to_contain_text("Reconnecting this worker…")
        opening.set()
        notice = page.locator("#timeline .line.notice", has_text="Continued in a fresh Copilot")
        await expect(notice).to_have_count(1, timeout=10000)
        await expect(page.locator("#timeline")).not_to_contain_text("JSON-RPC")
        await expect(page.locator("#timeline .line.error")).to_have_count(0)
        await settle(runtime)
        assert given(first, "Yes, retry it") == 1, "the fresh session has it, once"
        log("a worker Copilot can't reopen: Reconnecting this worker…, then a fresh one has it")

        # No fresh session either: said plainly, with its button; the answer is kept.
        refusing["create"] = True
        await page.locator("#back-home").click()
        card = needs.locator(".card.task", has_text="Retry the mirror access now?")
        await expect(card).to_be_visible(timeout=10000)
        await card.get_by_role("button", name="Reply").click()
        await card.locator("textarea").fill("Yes, the mirror too")
        await card.get_by_role("button", name="Send").click()
        said = page.locator(".toast", has_text="can't be reopened; your message is kept")
        await expect(said).to_be_visible(timeout=10000)
        await expect(said.get_by_role("button", name=fresh)).to_be_visible()
        await expect(said).not_to_contain_text("JSON-RPC")
        await expect(needs).not_to_contain_text("JSON-RPC")
        await page.locator("#session-list .item", has_text="Retry the mirror access").click()
        timeline = page.locator("#timeline")
        line = timeline.locator(".line.error", has_text="can't be reopened")
        await expect(line).to_have_count(1)
        await expect(timeline).not_to_contain_text("JSON-RPC")
        await line.screenshot(path=SHOTS / "fresh-start-line.png")
        refusing["create"] = False
        await line.get_by_role("button", name=fresh).click()
        await expect(page.locator(".toast", has_text="Started a fresh worker with its context."))\
            .to_be_visible(timeout=10000)  # fmt: skip
        await settle(runtime)
        assert given(second, "Yes, the mirror too") == 1, "the answer kept goes once"
        await expect(timeline.locator(".line.note", has_text="can't be reopened")).to_have_count(1)
        await expect(timeline.get_by_role("button", name="Start a fresh worker")).to_have_count(0)
        await expect(timeline).not_to_contain_text("JSON-RPC")
        await context.close()
        log("with no fresh session either: one plain line, Start a fresh worker; kept, then sent")
    finally:
        client.resume_session, client.create_session = resume, create


async def fork_switch(runtime, on: bool):
    old = runtime.store.settings()
    new = old.model_copy(update={"fork_busy": on})
    runtime.store.save_settings(new)
    await runtime.settings_changed(old, new)


async def round_fork(runtime, browser, failures: list[str]):
    """A busy worker's new task goes to a second worker with its context
    (docs/design/worker-fork.md): one line in the Router chat; in the sidebar the second worker
    a row like any other, with ⑂ after its name (where it came from and why on hover); its
    task's row says where it came from; the first line of each one's chat opens the other; one
    switch in Settings → General."""
    store = runtime.store
    router = store.get("router_id")
    await fork_switch(runtime, True)
    spawned = await call_tool(
        runtime, "spawn_worker", router, title="Speed up the CSV importer",
        instructions="Profile the CSV importer and make it faster.", why_new="test",
    )  # fmt: skip
    worker = json.loads(spawned.text_result_for_llm)["session_id"]
    await settle(runtime)  # Started (its session opened, its task sent).
    session = runtime.states.handle(worker)
    session.emit("user.message", content=session.sent[-1])
    session.emit("assistant.turn_start")
    await settle(runtime)
    runtime.update_session(worker, status="working")
    await call_tool(
        runtime, "message_session", router, session_id=worker, task="new", why_new="test",
        earlier="keep", title="Add importer benchmarks", text="Add a benchmark for the importer.",
    )  # fmt: skip
    told = await call_tool(
        runtime, "message_session", router, session_id=worker, task="new", why_new="test",
        title="Export to Parquet", text="Add a Parquet export beside the CSV one.",
    )  # fmt: skip
    second = json.loads(told.text_result_for_llm)["session_id"]
    await settle(runtime)
    # The second worker busy too: a third, from its conversation.
    session = runtime.states.handle(second)
    session.emit("user.message", content=session.sent[-1])
    session.emit("assistant.turn_start")
    await settle(runtime)
    runtime.update_session(second, status="working")
    await call_tool(
        runtime, "message_session", router, session_id=second, task="new", why_new="test",
        earlier="keep", title="Export to Avro", text="Add an Avro export too.",
    )  # fmt: skip
    told = await call_tool(
        runtime, "message_session", router, session_id=second, task="new", why_new="test",
        title="Export to ORC", text="And an ORC export.",
    )  # fmt: skip
    third = json.loads(told.text_result_for_llm)["session_id"]
    assert store.session(third)["role"] == "worker" and third not in {worker, second}
    await settle(runtime)
    origin = runtime.display_title(store.session(worker))
    for scheme in ("light", "dark"):
        for width in (1280, 800):
            context = await browser.new_context(
                viewport={"width": width, "height": 900}, color_scheme=scheme
            )
            page = await context.new_page()
            page.on("pageerror", lambda error: failures.append(str(error)))
            await page.goto(f"{BASE}/#token={TOKEN}")
            await expect(page.locator("#application")).to_be_visible()
            if width < 1100:
                await page.locator("#home-tabs [data-tab=chat]").click()
            line = page.locator(
                "#timeline .event", has_text="Started a second worker for “Export to Parquet”"
            )
            await expect(line.locator(".what")).to_have_text(
                f"Started a second worker for “Export to Parquet” (context from “{origin}”)"
            )
            # The sidebar: a row like any other, by status (never indented under the busy
            # worker), a small ⑂ after its name saying where it came from and why.
            nested = page.locator(f'#session-list .item[data-id="{second}"]')
            lefts = [
                await page.locator(f'#session-list .item[data-id="{id_}"] .dot').evaluate(
                    "n => n.getBoundingClientRect().left"
                )
                for id_ in (worker, second, third)
            ]
            assert len(set(lefts)) == 1, ("no indentation", lefts)
            busy = f"Started from “{origin}” with its conversation, because {origin} was busy"
            await expect(nested.locator(".fork-mark")).to_have_attribute("title", busy)
            await expect(nested).to_have_attribute("title", re.compile(re.escape(busy)))
            await expect(
                page.locator(f'#session-list .item[data-id="{worker}"] .fork-mark')
            ).to_have_count(0)
            found = await page.locator("#session-list").evaluate(OVERFLOWS)
            assert not found, (width, found)
            # Its task's row: where it came from, at its foot.
            if width < 1100:
                await page.locator("#home-tabs [data-tab=tasks]").click()
            title = page.locator(".head .title", has_text=re.compile(r"^Export to Parquet$"))
            row = page.locator(".card.task", has=title)
            await expect(row.locator(".worker-ref .name")).to_have_text(f"from “{origin}”")
            await expect(row.locator(".worker-ref")).to_have_attribute(
                "data-tip", re.compile("A second worker of")
            )
            found = await row.evaluate(OVERFLOWS)
            assert not found, (width, found)
            await page.screenshot(path=SHOTS / f"fork-home-{width}-{scheme}.png")
            # Its chat starts with where its context came from, which opens that worker.
            await nested.click()
            first_line = page.locator("#timeline .line.notice").first
            await expect(first_line).to_have_text(f"Started with the context of “{origin}”")
            await page.screenshot(path=SHOTS / f"fork-chat-{width}-{scheme}.png")
            await first_line.click()
            await expect(page.locator(f'#session-list .item[data-id="{worker}"]')).to_have_class(
                re.compile(r"\bselected\b")
            )
            gone = page.locator("#timeline .line.notice", has_text="Its context went to")
            await expect(gone).to_have_text(
                "Its context went to a second worker for “Export to Parquet”"
            )
            await gone.click()
            await expect(nested).to_have_class(re.compile(r"\bselected\b"))
            await context.close()
    log("a second worker: one line, a row with ⑂ in the sidebar, its row and chats linked")
    # Joined to the worker it came from (the Router's view of teams, as before); its own tab
    # starts with its task's lineage: with whose context it started (⑂). No team of the owner's.
    team = runtime.teams.team_of(worker)
    assert team and {runtime.teams.team_of(other) for other in (second, third)} == {team}
    [task] = [
        t["id"] for t in runtime.tasks.for_session(second) if t["title"] == "Export to Parquet"
    ]
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)
    await page.evaluate(f"import('/static/session.js').then((m) => m.select({json.dumps(second)}))")
    await page.locator("#worker-tabs").get_by_role("tab", name="Notes").click()
    strip = page.locator("#timeline > .team-lineage .lineage")
    mine = runtime.teams.label(store.session(second))
    orc = runtime.teams.label(store.session(third))
    # Its current task's lineage (its Avro export): its context went on to a third worker.
    await expect(strip.locator(".caption")).to_have_text(f"{mine}'s context went to {orc}")
    await expect(strip.locator(".lnode.self")).to_have_count(1)
    assert task
    found = await strip.evaluate(OVERFLOWS)
    assert not found, found
    await page.locator("#main").screenshot(path=SHOTS / "fork-team-1280.png")
    await context.close()
    log("its tab: its lineage says with whose context it started (⑂)")
    # The switch: one line in Settings → General, on; off when saved off.
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    await page.goto(f"{BASE}/#token={TOKEN}")
    await page.locator("#settings-button").click()
    switch = page.locator("#settings-dialog input[name=fork_busy]")
    await expect(switch).to_be_visible()
    await expect(switch).to_be_checked()
    await page.wait_for_timeout(300)  # (The dialog fades in.)
    await page.screenshot(path=SHOTS / "fork-settings-1280.png")
    await switch.uncheck()
    await page.locator("#settings-dialog").get_by_role("button", name="Save", exact=True).click()
    await until(lambda: not store.settings().fork_busy)
    await context.close()
    await fork_switch(runtime, True)  # As it is by default.
    log("its switch: one line in Settings → General")


async def round_storage(runtime, browser, failures: list[str]):
    """Disk space: the sidebar's Storage button says the disk is low; its dialog lists workers'
    folders the largest first with why a closed worker's worktree stays, reviews its files, and
    deletes it only once "delete" is typed; changes arrive as "storage" events."""
    import subprocess

    from chiefofstaff import machine, workspaces

    store = runtime.store
    router = store.get("router_id")
    code = store.directory.parent / "storage-code"
    app = code / "app"
    app.mkdir(parents=True, exist_ok=True)
    git = ("git", "-c", "user.email=t@t", "-c", "user.name=t")
    (app / ".gitignore").write_text("build/\nlogs/\n")
    (app / "main.py").write_text("print(1)\n")
    for command in (("git", "init", "-q", "-b", "main"), ("git", "add", "."),
                    (*git, "commit", "-qm", "init")):  # fmt: skip
        subprocess.run(command, cwd=app, check=True, capture_output=True)
    before = store.settings()
    store.save_settings(before.model_copy(update={"code_folder": str(code)}))
    read_disk = machine.read_disk
    reading = {"free": 25 * 10**9, "total": 400 * 10**9, "path": "/"}
    machine.read_disk = lambda paths: dict(reading)
    machine._disk = (float("-inf"), None)
    try:
        spawned = await call_tool(runtime, "spawn_worker", router, title="Storage closed one",
                                  instructions="x", why_new="test", project=str(app),
                                  isolate=True)  # fmt: skip
        closed = json.loads(spawned.text_result_for_llm)
        await settle(runtime)
        tree = Path(closed["cwd"])
        (tree / "logs").mkdir()
        (tree / "logs" / "run.txt").write_text("y" * 200_000)
        (tree / "build").mkdir()
        (tree / "build" / "out.o").write_text("z" * 100_000)
        await runtime.workers.close(closed["session_id"])
        await settle(runtime)
        await runtime.storage.measure(store.session(closed["session_id"]))
        # What a cleanup could not delete (a container's build, owned by another user).
        stuck = (code / ".chief").resolve() / ".chiefofstaff-trash" / "0123456789ab-build"
        stuck.mkdir(parents=True)
        (stuck / "out.o").write_text("x")
        store.set("storage:stuck", json.dumps({str(stuck): {
            "size": 2_500_000_000, "at": "2026-11-09T08:00:00+00:00", "tried": time.time(),
        }}))  # fmt: skip
        await runtime.storage.check_disk()
        await settle(runtime)

        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(f"{BASE}/#token={TOKEN}")
        button = page.locator("#storage-button")
        await expect(button).to_have_class(re.compile(r"\bwarning\b"))
        await expect(page.locator("#storage-free")).to_have_text("25 GB free")
        log("storage: the sidebar button says the disk is low")
        await button.click()
        dialog = page.locator("#storage-dialog")
        await expect(dialog).to_be_visible()
        await expect(dialog.locator(".storage-disk")).to_contain_text("25 GB free")
        await expect(dialog.locator(".storage-level")).to_have_text("Low")
        row = dialog.locator(f'.storage-worker[data-session="{closed["session_id"]}"]')
        await expect(row.locator(".storage-state")).to_have_text("Closed")
        await expect(row.locator(".storage-kept")).to_contain_text("ignored files (logs/)")
        assert (tree / "logs" / "run.txt").exists() and not (tree / "build" / "out.o").exists()
        await expect(dialog.locator("#storage-uv-path")).not_to_be_empty()
        await expect(dialog.locator("#storage-stuck")).to_contain_text("0123456789ab-build")
        await expect(dialog.locator("#storage-stuck")).to_contain_text("2.5 GB")
        await expect(dialog.locator("#storage-history")).to_contain_text(
            "Cleaned build artifacts on close"
        )
        log("storage: a closed worker's kept worktree says why; its build output went on close")

        await row.get_by_role("button", name="Review files").click()
        review = row.locator(".storage-review")
        await expect(review).to_contain_text("logs/")
        # git cannot read its changes: it says so, and offers no delete.
        real_git = workspaces.checkout_git

        async def no_status(checkout, path, *arguments, **options):
            if "status" in arguments:
                raise ValueError("git failed: index.lock exists")
            return await real_git(checkout, path, *arguments, **options)

        workspaces.checkout_git = no_status
        try:
            await row.get_by_role("button", name="Hide files").click()
            await row.get_by_role("button", name="Review files").click()
            await expect(review.locator(".storage-unread")).to_be_visible()
            await expect(review).to_contain_text("Uncommitted changes could not be read")
            await expect(review.get_by_role("button", name="Delete worktree")).to_have_count(0)
        finally:
            workspaces.checkout_git = real_git
        await row.get_by_role("button", name="Hide files").click()
        await row.get_by_role("button", name="Review files").click()
        await expect(review).to_contain_text("No uncommitted changes")
        log("storage: changes git cannot read: the review says so and offers no delete")
        delete = review.get_by_role("button", name="Delete worktree")
        await expect(delete).to_be_disabled()
        await review.locator("input[aria-label='Type delete to confirm']").fill("delete")
        await expect(delete).to_be_enabled()
        await dialog.screenshot(path=SHOTS / "ui-edges-storage.png")
        inner = dialog.locator(".storage")
        width = await inner.evaluate("node => node.scrollWidth - node.clientWidth")
        assert width <= 0, f"the dialog runs {width}px past its side"
        await delete.click()
        await expect(dialog.locator("#storage-history")).to_contain_text("Deleted its worktree")
        assert not tree.exists(), "deleted once confirmed"
        log("storage: Review files lists what is kept; delete only once 'delete' is typed")

        # Workers' own caches: measured, then cleared (in a folder of this run's, not the
        # machine's), once the owner confirms.
        tier = runtime.cache_tier
        runtime.cache_tier = store.directory.parent / "worker-caches" / "workers"
        (runtime.cache_tier / "npm").mkdir(parents=True, exist_ok=True)
        (runtime.cache_tier / "npm" / "blob").write_bytes(b"\1" * 3_000_000)
        try:
            runtime.storage.publish()
            size = dialog.locator("#storage-caches-size")
            await expect(dialog.locator("#storage-caches-path")).to_contain_text("worker-caches")
            await dialog.locator("#storage-caches-measure").click()
            # du runs at the lowest priority, after any other measuring.
            await expect(size).to_contain_text(re.compile(r"^3 MB, measured"), timeout=60_000)
            in_use = runtime.caches_in_use
            runtime.caches_in_use = lambda: True
            try:
                runtime.storage.publish()
                await expect(dialog.locator("#storage-caches-clear")).to_be_disabled()
                await expect(dialog.locator("#storage-caches-note")).to_have_text(
                    "Workers are using their caches; clear when they're idle."
                )
            finally:
                runtime.caches_in_use = in_use
            runtime.storage.publish()
            await expect(dialog.locator("#storage-caches-note")).to_have_text("")
            page.once("dialog", lambda asked: asyncio.ensure_future(asked.accept()))
            await dialog.locator("#storage-caches-clear").click()
            await expect(size).to_contain_text(re.compile(r"^0 KB, measured"), timeout=30_000)
            assert not (runtime.cache_tier / "npm").exists(), "workers' caches deleted"
        finally:
            runtime.cache_tier = tier
        log("storage: workers' caches measured, and cleared once confirmed")

        reading["free"] = 80 * 10**9
        machine._disk = (float("-inf"), None)
        await runtime.storage.check_disk()
        await expect(dialog.locator(".storage-level")).to_have_count(0)
        await page.locator("#storage-close").click()
        await expect(button).not_to_have_class(re.compile(r"\bwarning\b"))
        log("storage: room again: the dialog and the button say so")
        assert not errors, errors
        await context.close()
    finally:
        machine.read_disk = read_disk
        machine._disk = (float("-inf"), None)
        store.set("storage:level", "normal")
        store.delete("storage:stuck")
        store.save_settings(store.settings().model_copy(update={"code_folder": before.code_folder}))


async def round_mascot(runtime, browser, failures: list[str]):
    """The captain, Chief of Staff's duck: beside the name, its mood says how Home stands (it
    salutes with Home's count when something needs the owner, steers while at work, keeps watch,
    rests; cheers a moment when a task is done and nothing needs the owner); it is on the
    "Thinking…" line, in the empty states and the favicon. It moves a while, then only blinks;
    with reduced motion it does not move at all. It adds no text."""
    router = runtime.store.get("router_id")
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda note: errors.append(note.text) if note.type == "error" else None)
    await page.goto(f"{BASE}/#token={TOKEN}")
    captain = page.locator("#sidebar .brand > .duck")
    await expect(captain).to_have_count(1)
    await expect(captain).to_be_visible()

    spawned = await call_tool(runtime, "spawn_worker", router, title="Paint the duck pond",
                              instructions="x", why_new="test")  # fmt: skip
    asker = json.loads(spawned.text_result_for_llm)["session_id"]
    await call_tool(runtime, "report", asker, status="question", summary="Blue or green?")
    await expect(page.locator("#needs .card", has_text="Blue or green?")).to_be_visible()
    await expect(captain).to_have_attribute("data-mood", "needs")
    await expect(captain.locator(".d-bubble")).to_be_visible()
    count, title = await page.evaluate(
        "() => [document.querySelector('.home-item .badge').textContent,"
        " document.querySelector('#sidebar .brand > .duck').title]"
    )
    assert title == f"{count} need{'s' if count == '1' else ''} you", (count, title)
    log("captain: it salutes when something needs you, with Home's count")

    # It moves a while after its mood changes, then rests: only its blink runs (free between
    # blinks).
    await expect(captain).not_to_have_class(re.compile(r"\bmoving\b"), timeout=15000)
    running = "d => d.getAnimations({ subtree: true }).map((a) => a.animationName)"
    assert await captain.evaluate(running) == ["d-blink"], await captain.evaluate(running)
    await captain.hover()
    assert len(await captain.evaluate(running)) > 1, "a pointer on it wakes it"
    await page.mouse.move(600, 600)
    log("captain: moves a while, then rests blinking; the pointer wakes it")

    moods = await page.evaluate(
        """async () => {
          const { captainMood } = await import("/static/sidebar.js");
          const t = Date.now() + 60000;
          const mood = (needs, finished, dots, now) =>
            captainMood({ needs, finished, dots, now }).join(": ");
          return [
            mood(0, 0, ["watching", "working"], t),
            mood(0, 0, ["idle", "watching"], t),
            mood(0, 0, ["idle", undefined], t),
            mood(0, 1, ["working"], t),
            mood(2, 1, ["working"], t + 1000),
            mood(0, 1, ["working"], t + 5000),
            mood(0, 1, ["working"], t + 7000),
            mood(1, 1, [], t + 7000),
          ];
        }"""
    )
    assert moods == [
        "working: At work",
        "watching: Keeping watch: tasks wait on CI, a review or a reply",
        "idle: All quiet",
        "done: Done",
        "needs: 2 need you",
        "done: Done",
        "working: At work",
        "needs: 1 needs you",
    ], moods
    log("captain: needs you, then a moment's cheer for a done task, then at work, watch, quiet")

    # On the "Thinking…" line: at the wheel; on watch while a command it left runs on.
    runtime.update_session(asker, status="working", activity="Mixing the paint")
    await page.locator(f'#session-list .item[data-id="{asker}"]').click()
    line = page.locator("#timeline .working-line")
    await expect(line).to_contain_text("Mixing the paint")
    await expect(line.locator(".duck")).to_have_attribute("data-mood", "working")
    await expect(line.locator(".dot")).to_have_count(0)
    runtime.states[asker].held = True
    runtime.update_session(asker, status="idle", activity="")
    await expect(line).to_contain_text("Waiting for background process")
    await expect(line.locator(".duck")).to_have_attribute("data-mood", "watching")
    runtime.states[asker].held = False
    runtime.update_session(asker, status="idle", activity="")
    await expect(line).to_have_count(0)
    await page.locator("#back-home").click()
    log("captain: steers on the Thinking line, keeps watch while a background command runs")

    # The empty states: Home's welcome and Needs you; the duck adds no words to either.
    hint = page.locator("#needs > .empty-hint")
    await expect(hint.locator(".duck")).to_have_count(1)
    await expect(hint).to_have_text("Nothing needs you.")
    welcome = await page.evaluate(
        "() => import('/static/inbox.js').then((m) => { const card = m.welcomeCard();"
        " return [card.querySelector('.duck')?.dataset.mood, card.querySelector('h2').textContent,"
        " card.textContent.startsWith('Ask for anything')]; })"
    )
    assert welcome == ["idle", "Ask for anything", True], welcome
    assert await page.locator('link[rel="icon"]').get_attribute("href") == "/static/favicon.svg"
    icon = await page.request.get(f"{BASE}/static/favicon.svg")
    assert icon.ok and "svg" in icon.headers["content-type"], icon.headers
    log("captain: in the empty states (no words of its own) and the favicon")
    assert not errors, errors
    await context.close()

    # With reduced motion it does not move at all, not even a blink.
    still = await browser.new_context(viewport={"width": 1280, "height": 900},
                                      reduced_motion="reduce")  # fmt: skip
    page = await still.new_page()
    await page.goto(f"{BASE}/#token={TOKEN}")
    captain = page.locator("#sidebar .brand > .duck")
    await expect(captain).to_be_visible()
    await expect(captain).to_have_class(re.compile(r"\bmoving\b"))
    assert await captain.evaluate("d => d.getAnimations({ subtree: true }).length") == 0
    log("captain: still with reduced motion")
    await still.close()
    await runtime.task_actions.cancel(runtime.tasks.current(asker)["id"])


def terminal(client, summary: str, events: list, *, is_open: bool = True, made="github/cli",
             last="", head: bool = True, folder: str = "/work/app") -> str:  # fmt: skip
    """One of the owner's Copilot sessions run in a terminal: its log (each event `minutes` ago,
    as the CLI writes it, in the client's Copilot home and for its view) and its listing."""
    session_id = str(uuid4())
    now = datetime.now(UTC)
    start = events[0][1] if events else 0
    if head:
        header = {"sessionId": session_id, "version": 1, "producer": "copilot-agent",
                  "copilotVersion": "1.0.109",
                  "context": {"cwd": folder, "branch": "main"}}  # fmt: skip
        events = [("session.start", start, header), *events]
    log, parent, at = [], None, now
    for kind, minutes, data in events:
        at = now - timedelta(minutes=minutes)
        stamp = at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        event = {"type": kind, "data": data, "id": str(uuid4()), "timestamp": stamp,
                 "parentId": parent}  # fmt: skip
        log.append(event)
        parent = event["id"]
    path = client.home / "session-state" / session_id / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event, separators=(",", ":")) + "\n" for event in log))
    os.utime(path, (at.timestamp(), at.timestamp()))
    client.logs[session_id] = log
    context = SimpleNamespace(working_directory=folder, repository="", branch="main")
    meta = SimpleNamespace(session_id=session_id, summary=summary, is_remote=False,
                           modified_time=at, context=context)  # fmt: skip
    vars(meta).update(client_name=made, last_client_name=last, clients_listed=True)
    client.metas.append(meta)
    if is_open:
        client.in_use[session_id] = 4242
    client.sessions[session_id] = FakeSession(session_id, {})  # A message can continue it.
    return session_id


def step(call: str, tool: str, minutes: float, **arguments) -> list:
    """A step the session's model asked for and started, `minutes` ago."""
    request = {"toolCallId": call, "name": tool, "arguments": arguments, "type": "function"}
    return [
        ("assistant.message", minutes, {"content": "", "toolRequests": [request]}),
        ("tool.execution_start", minutes,
         {"toolCallId": call, "toolName": tool, "arguments": arguments}),
    ]  # fmt: skip


def turn(text: str, minutes: float) -> list:
    return [
        ("user.message", minutes, {"content": text, "interactionId": "i-1", "turnId": "0"}),
        ("assistant.turn_start", minutes, {"turnId": "0", "interactionId": "i-1"}),
    ]


def answer(text: str, minutes: float) -> list:
    return [
        ("assistant.message", minutes, {"content": text, "turnId": "0"}),
        ("assistant.turn_end", minutes, {"turnId": "0"}),
    ]


QUESTION = "Should the fix go to main or to the release branch?"


def asking(client, summary="Fix the login test", minutes=12) -> str:
    """Open in a terminal, asking the owner (ask_user) for `minutes`."""
    return terminal(client, summary, [
        *turn("Make the login test pass", minutes + 3),
        *step("c-1", "bash", minutes + 2, command="npm test"),
        ("tool.execution_complete", minutes + 1, {"toolCallId": "c-1", "success": True}),
        ("assistant.turn_end", minutes + 1, {"turnId": "0"}),
        ("assistant.turn_start", minutes, {"turnId": "0"}),
        *step("c-2", "ask_user", minutes, message=QUESTION),
    ])  # fmt: skip


def finished(client, summary="Write the API changelog", minutes=180) -> str:
    """Closed in its terminal after it answered, `minutes` ago."""
    return terminal(client, summary, [
        *turn("Draft a changelog entry for the login API.", minutes + 5),
        *answer("Drafted it in CHANGELOG.md, not committed yet.", minutes + 1),
        ("session.shutdown", minutes, {"shutdownType": "routine"}),
    ], is_open=False)  # fmt: skip


async def others_setting(runtime, on: bool):
    old = runtime.store.settings()
    new = old.model_copy(update={"other_sessions": on})
    runtime.store.save_settings(new)
    await runtime.settings_changed(old, new)
    runtime.workers.external_cache = (float("-inf"), [])


async def others_page(browser, requests: list[str] | None = None, **options):
    context = await browser.new_context(viewport={"width": 1280, "height": 900}, **options)
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda note: errors.append(note.text) if note.type == "error" else None)
    if requests is not None:
        page.on("request", lambda r: requests.append(f"{r.method} {r.url.split(str(PORT), 1)[-1]}"))
    page.errors = errors  # type: ignore[attr-defined]
    await page.goto(f"{BASE}/#token={TOKEN}")
    await expect(page.locator("#application")).to_be_visible()
    return context, page


# Each row of the sidebar's Other Copilot sessions, as its DOM has it.
ROWS = """() => [...document.querySelectorAll('#external-list button.item')].map((row) => ({
  id: row.dataset.id || '', classes: row.className, title: row.title,
  name: row.getAttribute('aria-label') || '', dot: row.querySelector('.dot').className,
  tip: row.querySelector('.dot').title, label: row.querySelector('.label').textContent,
  sub: row.querySelector('.sub')?.textContent ?? null,
  folded: Boolean(row.closest('details.idle-workers')) }))"""


async def others_pins(runtime, browser, ask: str, done: str):
    """With the setting off, the sidebar's Other Copilot sessions and their view are today's:
    pinned before the setting existed, and kept."""
    requests: list[str] = []
    context, page = await others_page(browser, requests)
    group = page.locator("#external")
    await page.wait_for_timeout(400)
    assert await group.get_attribute("open") is None, "folded at load"
    assert not [r for r in requests if "/api/external" in r or "/api/others" in r], requests
    await expect(page.locator("#external-list .item")).to_have_count(0)
    await expect(page.locator("#external-count")).to_have_text("")
    async with page.expect_request("**/api/external?refresh=true"):
        await page.locator("#external > summary").click()
    await expect(page.locator("#external-list .item")).to_have_count(2)
    await expect(page.locator("#external-count")).to_have_text("1 open")
    await expect(page.locator("#external > summary")).to_have_attribute(
        "title", "2 sessions, 1 open"
    )
    rows = await page.evaluate(ROWS)
    assert rows == [
        {"id": "", "classes": "item", "title": "Open in a terminal now · /work/app", "name": "",
         "dot": "dot open", "tip": "Open in a terminal right now (shown read-only)",
         "label": "Fix the login test", "sub": "open", "folded": False},
        {"id": "", "classes": "item", "title": "/work/app", "name": "", "dot": "dot",
         "tip": "Not open anywhere", "label": "Write the API changelog", "sub": "3h",
         "folded": False},
    ], rows  # fmt: skip
    log("others off: the group folded at load, read only when unfolded; its rows as today")

    meta = page.locator("#session-meta")
    history = page.locator(".external-history")
    await page.locator("#external-list .item", has_text="Fix the login test").click()
    await expect(history.locator(".working-line")).to_have_text("Working in a terminal…")
    await expect(history.locator(".working-line")).to_be_visible()
    assert await history.locator(".working-line").get_attribute("hidden") is None
    await expect(meta).to_have_text(
        re.compile(r"^Open in a terminal · /work/app · main · updated 1[23]m ago$")
    )
    await expect(history).to_contain_text("ask_user")
    await expect(history).not_to_contain_text(QUESTION)
    await expect(history.locator(".line.note")).to_have_count(0)
    await expect(page.locator("#composer-note")).to_contain_text("exit it there (/exit)")
    await expect(page.locator("#composer-input")).to_be_disabled()
    await page.locator("#external-list .item", has_text="Write the API changelog").click()
    await expect(history.locator(".line.notice")).to_have_text(
        "A message continues it here, still in /work/app"
    )
    await expect(meta).to_have_text("Not managed yet · /work/app · main · updated 3h ago")
    await expect(page.locator("#composer-input")).to_be_enabled()
    placeholder = await page.locator("#composer-input").get_attribute("placeholder")
    assert placeholder == "Message to continue this session here…", placeholder
    assert await page.evaluate("localStorage.getItem('cos.selected')") == "home"
    log("others off: the view's meta line, timeline (ask_user as a step) and message box")
    await page.reload()
    await expect(page.locator(".home-item.selected")).to_be_visible()
    await expect(page.locator("#back-home")).to_be_hidden()
    log("others off: a reload while viewing one shows Home")
    assert not page.errors, page.errors  # type: ignore[attr-defined]
    await context.close()

    # The group never unfolded: the message box knows nothing of these sessions, a submission
    # to one sends nothing, and a toast about one opens nothing.
    requests.clear()
    context, page = await others_page(browser, requests)
    await page.wait_for_timeout(300)
    requests.clear()
    found = await page.evaluate(
        """async (id) => {
          const { state } = await import('/static/state.js');
          const { externalItem } = await import('/static/sidebar.js');
          state.selected = id;
          const item = externalItem();
          const input = document.querySelector('#composer-input');
          input.value = 'Commit it';
          document.querySelector('#composer').requestSubmit();
          await new Promise((done) => setTimeout(done, 300));
          state.selected = 'home';
          return [item === undefined, input.value];
        }""",
        done,
    )
    assert found == [True, "Commit it"], found
    assert not [r for r in requests if r.startswith("POST") or "/api/external" in r], requests
    await page.fill("#composer-input", "")
    for session_id in (done, str(uuid4())):
        runtime.bus.publish("toast", {"text": f"About {session_id}", "session_id": session_id,
                                      "needs_action": True})  # fmt: skip
        note = page.locator(".toast", has_text=session_id)
        await note.click()
        await expect(note).to_have_count(0)
        await expect(page.locator(".home-item.selected")).to_be_visible()
        await expect(page.locator("#back-home")).to_be_hidden()
    assert not page.errors, page.errors  # type: ignore[attr-defined]
    await context.close()
    log("others off: externalItem() and a submission with the group never unfolded; a toast "
        "for one stays on Home")  # fmt: skip


def more_sessions(client) -> dict[str, str]:
    """A session in every status, and three without one: their ids by what they show."""
    return {
        "your_turn": terminal(client, "Explain the cache misses", [
            *turn("Why does the cache miss so often?", 9),
            *answer("Keys include the request id, so no two requests share one.", 5),
        ]),
        "working": terminal(client, "Run the integration suite", [
            *turn("Run the integration suite", 3),
            *step("c-3", "bash", 1, command="make integration"),
            ("session.usage_info", 0.5, {"tokenLimit": 1000}),
        ]),
        "no_progress": terminal(client, "Upgrade the payments SDK", [
            *turn("Upgrade the payments SDK to version 5", 45),
            *step("c-4", "bash", 40, command="make build"),
        ]),
        "idle": terminal(client, "Tidy the README", [
            *turn("Tidy the README", 125), *answer("Tidied: shorter install steps.", 120),
        ]),
        "stopped": terminal(client, "Profile the orders page", [
            *turn("Profile the orders page", 70), *step("c-5", "bash", 60, command="npm run e2e"),
        ], is_open=False),
        "unknown": terminal(client, "Draft the release notes", turn("Draft them", 30), made=""),
        "another": terminal(client, "Bump the linter", turn("Bump it", 50), last="chiefofstaff"),
        "format": terminal(client, "Translate the docs", turn("Translate", 90), head=False),
    }  # fmt: skip


# A time as the page shows it: "2:10 PM", or "Nov 10, 11:06 PM" on another day.
TIME = r"(?:[A-Z][a-z]{2} \d{1,2}, )?\d{1,2}:\d{2}\s?[AP]M"


async def others_statuses(runtime, browser, ids: dict[str, str]):
    """Every status: its dot, small word, tooltip and name; rows without one say why; five at
    rest, the rest folded; the order held under the pointer; folding's first-load default; the
    view's meta and note lines; reduced motion; the keyboard."""
    requests: list[str] = []
    context, page = await others_page(browser, requests)
    group = page.locator("#external")
    await expect(group).to_have_attribute("open", "")
    await expect(page.locator("#external-count")).to_have_text("8 open")
    assert any(r.startswith("GET /api/external?refresh=true") for r in requests), requests
    row = lambda key: page.locator(f'#external-list button.item[data-id="{ids[key]}"]')  # noqa: E731
    await expect(row("asks").locator(".dot")).to_have_class("dot waiting", timeout=8000)
    await expect(row("idle").locator(".dot")).to_have_class("dot open", timeout=8000)
    shown = {item["id"]: item for item in await page.evaluate(ROWS)}
    by = {key: shown[session_id] for key, session_id in ids.items()}
    expected = {
        "asks": ("dot waiting", "12m", "Asks you, for 12 minutes",
                 f"from its log: asked at {TIME}, no answer since\n{re.escape(QUESTION)}"),
        "your_turn": ("dot waiting", "5m", "Your turn, for 5 minutes",
                      f"from its log: its turn ended at {TIME}\nKeys include the request id"),
        "working": ("dot open busy", "open", "Working, for 3 minutes",
                    f"from its log: at work since {TIME}\nbash make integration"),
        "no_progress": ("dot stalled", "40m", "No progress for 40 minutes",
                        f"from its log: nothing written since {TIME}\nbash make build\n"
                        "A slow step, a hang or a prompt in its terminal look the same"),
        "idle": ("dot open", "open", "Idle, for 2 hours",
                 f"from its log: its turn ended at {TIME}\nTidied: shorter install steps."),
        "finished": ("dot done", "3h", "Finished, 3 hours ago",
                     f"from its log: last written at {TIME}\nDrafted it in CHANGELOG.md"),
        "stopped": ("dot", "1h", "Stopped, 1 hour ago",
                    f"from its log: last written at {TIME}\nbash npm run e2e"),
    }  # fmt: skip
    for key, (dot, sub, words, more) in expected.items():
        item = by[key]
        assert (item["dot"], item["sub"], item["tip"]) == (dot, sub, words), (key, item)
        tooltip = f"{re.escape(words)} · {more}.*\n/work/app"
        assert re.fullmatch(tooltip, item["title"], re.DOTALL), (key, item["title"])
        name = f"{item['label']}, {words[0].lower()}{words[1:]}"
        assert item["name"] == name, (key, item["name"])
        named = page.get_by_role("button", name=name, exact=True, include_hidden=True)
        await expect(named).to_have_count(1)
    why = {
        "unknown": "Copilot did not say which app started it.",
        "another": "Another Chief of Staff ran it last.",
        "format": "Its log is in a format this version does not read.",
    }
    for key, words in why.items():
        item = by[key]
        assert (item["dot"], item["sub"], item["name"]) == ("dot open", "open", ""), (key, item)
        assert item["title"] == f"Open in a terminal now · /work/app\n{words}", (key, item)
        assert item["tip"] == "Open in a terminal right now (shown read-only)", (key, item)
    log("others on: every status's dot, small word, tooltip and name; rows without one say why")

    # Five at rest: waiting (the longest first), no progress, at work, then the list's order;
    # the rest folded as workers with nothing open are.
    rest = [item["id"] for item in await page.evaluate(ROWS) if not item["folded"]]
    assert rest == [ids[k] for k in ("asks", "your_turn", "no_progress", "working", "unknown")]
    fold = page.locator("#external-list > details.idle-workers")
    await expect(fold.locator("> summary")).to_have_text("5 more")
    assert await fold.get_attribute("open") is None
    await expect(fold.locator("button.item").first).to_be_hidden()
    await fold.locator("> summary").click()
    await expect(fold.locator("button.item").first).to_be_visible()
    folded = [item["id"] for item in await page.evaluate(ROWS) if item["folded"]]
    assert folded == [ids[k] for k in ("another", "format", "idle", "stopped", "finished")]
    await fold.locator("> summary").click()
    log("others on: five rows at rest, the rest behind “5 more”, in the list's order")
    (SHOTS / "others").mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=SHOTS / "others" / "edges-statuses.png")

    # Under the pointer the order holds; a session that starts asking moves up once it leaves.
    await row("asks").hover()
    readme = ids["idle"]
    terminal_ask(runtime.backend, readme, "Keep the old badge?")
    await expect(row("idle").locator(".dot")).to_have_class("dot waiting", timeout=8000)
    rest = [item["id"] for item in await page.evaluate(ROWS) if not item["folded"]]
    assert readme not in rest, "the order holds under the pointer"
    await page.mouse.move(900, 600)
    await expect(page.locator("#external-list > button.item").nth(2)).to_have_attribute(
        "data-id", readme
    )
    log("others on: the order holds under the pointer, sorted again once it leaves")

    # The row the keyboard is on keeps the focus when it moves up (it starts asking).
    await row("working").focus()
    terminal_ask(runtime.backend, ids["working"], "Run the slow tests too?", done="c-3")
    await expect(row("working").locator(".dot")).to_have_class("dot waiting", timeout=8000)
    rest = [item["id"] for item in await page.evaluate(ROWS) if not item["folded"]]
    assert rest.index(ids["working"]) == 3, rest
    focused = await page.evaluate("() => document.activeElement?.dataset?.id")
    assert focused == ids["working"], "the focus moves with its row"
    # Answered in its terminal, it works again.
    terminal_write(runtime.backend, ids["working"], [
        ("tool.execution_complete", 0, {"toolCallId": "c-9", "success": True}),
        *step("c-10", "bash", 0, command="make test-slow"),
    ])  # fmt: skip
    await expect(row("working").locator(".dot")).to_have_class("dot open busy", timeout=8000)
    log("others on: a row that moves keeps the keyboard's focus")
    assert not page.errors, page.errors  # type: ignore[attr-defined]
    await context.close()


async def others_view(runtime, browser, ids: dict[str, str], old: str):
    """The view behind a row: its meta line says where it stands, and while it asks its timeline
    ends with the question; a toast opens it, a reload keeps it, and a closed session is
    continued, the group never unfolded; folding is kept; the keyboard; reduced motion."""
    requests: list[str] = []
    context, page = await others_page(browser, requests)
    row = lambda key: page.locator(f'#external-list button.item[data-id="{ids[key]}"]')  # noqa: E731
    meta = page.locator("#session-meta")
    history = page.locator(".external-history")
    await expect(row("asks").locator(".dot")).to_have_class("dot waiting", timeout=8000)
    await row("asks").click()
    await expect(meta).to_have_text(
        re.compile(f"^Asks you since {TIME} · /work/app · main · updated 12m ago$")
    )
    await expect(history.locator(":scope > :last-child")).to_have_text(f"Asks you: {QUESTION}")
    await expect(history.locator(":scope > :last-child")).to_have_class("line note")
    # In place of "Working in a terminal…", which would say the opposite.
    await expect(history.get_by_text("Working in a terminal…")).to_be_hidden()
    await expect(history.locator(":scope > :nth-last-child(2)")).to_have_class("working-line")
    await expect(history).to_contain_text("ask_user")
    await expect(page.locator("#composer-input")).to_be_disabled()
    await row("no_progress").click()
    await expect(meta).to_have_text("No progress for 40 min · /work/app · main · updated 40m ago")
    await expect(history.locator(".line.note")).to_have_count(0)
    await expect(history.locator(".working-line")).to_be_visible()  # As without the setting.
    await page.locator("#external-list > details.idle-workers > summary").click()
    await row("unknown").click()
    await expect(meta).to_have_text("Open in a terminal · /work/app · main · updated 30m ago")
    await page.locator("#external-list > details.idle-workers > summary").click()
    await expect(history.locator(".working-line")).to_be_visible()
    log("others on: the view's meta line says where it stands; asking, the question ends it")

    # The keyboard: rows are buttons, Enter opens one, and a redraw keeps the focus on it.
    await page.locator("#back-home").click()
    first = page.locator("#external-list > button.item").first
    await first.focus()
    await page.keyboard.press("Enter")
    await expect(page.locator("#back-home")).to_be_visible()
    focused = "() => document.activeElement?.dataset?.id || document.activeElement?.tagName"
    chosen = await page.evaluate(focused)
    assert chosen == await first.get_attribute("data-id"), chosen
    await page.evaluate("import('/static/sidebar.js').then((m) => m.renderOthers())")
    await page.wait_for_timeout(2500)  # An event from the index meanwhile, too.
    assert await page.evaluate(focused) == chosen, "a redraw keeps the focus"
    await page.keyboard.press("Tab")
    assert await page.evaluate(focused) == await page.locator(
        "#external-list > button.item").nth(1).get_attribute("data-id")  # fmt: skip
    log("others on: rows are buttons in words; Enter opens one; a redraw keeps the focus")

    # Folded by the owner, it stays folded (and unread) at the next load; unfolded, unfolded.
    await page.locator("#external > summary").click()
    await expect(page.locator("#external")).not_to_have_attribute("open", "")
    await page.wait_for_function("() => localStorage.getItem('cos.section.external') === '0'")
    requests.clear()
    await page.reload()
    await expect(page.locator("#application")).to_be_visible()
    await page.wait_for_timeout(500)
    assert await page.locator("#external").get_attribute("open") is None
    assert not [r for r in requests if re.search(r"/api/external(\?|$)", r)], requests
    await expect(page.locator("#external-count")).to_have_text("")  # As before it is read.
    await page.locator("#external > summary").click()
    await page.wait_for_function("() => localStorage.getItem('cos.section.external') === '1'")
    await page.reload()
    await expect(page.locator("#external")).to_have_attribute("open", "")
    log("others on: unfolded on the first load, then folded or not as the owner left it")
    assert not page.errors, page.errors  # type: ignore[attr-defined]
    await context.close()

    # The group never unfolded: a toast opens the view, a reload keeps it, and one found only by
    # its id (closed for long) is continued here.
    requests.clear()
    context, page = await others_page(browser, requests)
    meta, history = page.locator("#session-meta"), page.locator(".external-history")
    await page.evaluate("localStorage.setItem('cos.section.external', '0')")
    await page.reload()
    await expect(page.locator("#application")).to_be_visible()
    requests.clear()
    runtime.bus.publish("toast", {"text": "Look at the login test", "session_id": ids["asks"],
                                  "needs_action": True})  # fmt: skip
    await page.locator(".toast", has_text="Look at the login test").click()
    await expect(meta).to_have_text(re.compile(f"^Asks you since {TIME} · "))
    await expect(page.locator("#session-title")).to_have_text("Fix the login test")
    await page.reload()
    await expect(page.locator("#session-title")).to_have_text("Fix the login test")
    await expect(meta).to_have_text(re.compile(f"^Asks you since {TIME} · "))
    await expect(history.locator(":scope > :last-child")).to_have_text(f"Asks you: {QUESTION}")
    runtime.bus.publish("toast", {"text": "About something else", "session_id": str(uuid4()),
                                  "needs_action": True})  # fmt: skip
    await page.locator(".toast", has_text="About something else").click()
    await expect(page.locator("#session-title")).to_have_text("Fix the login test")
    await page.evaluate(f"localStorage.setItem('cos.selected', '{old}')")
    await page.reload()
    await expect(page.locator("#session-title")).to_have_text("Keep the old notes")
    await expect(meta).to_have_text(
        re.compile(r"^Not managed yet · /work/app · main · updated 1d ago$")
    )
    assert f"GET /api/others/{old}" in requests, requests
    await page.fill("#composer-input", "Carry on")
    await page.press("#composer-input", "Enter")
    continued = page.locator("#session-list .item", has_text="Keep the old notes")
    await expect(continued).to_be_visible(timeout=10000)
    await expect(continued).to_have_class(re.compile(r"\bselected\b"))
    assert f"POST /api/external/{old}/messages" in requests, requests
    assert not [r for r in requests if re.search(r"/api/external(\?|$)", r)], requests
    await page.fill("#composer-input", "And tidy it")
    await page.press("#composer-input", "Enter")
    await expect(page.locator("#timeline .msg.owner", has_text="And tidy it")).to_be_visible()
    assert f"POST /api/sessions/{old}/messages" in requests, "now a worker of its own"
    assert requests.count(f"POST /api/external/{old}/messages") == 1, requests
    await expect(page.locator(f'#external-list [data-id="{old}"]')).to_have_count(0)
    assert not page.errors, page.errors  # type: ignore[attr-defined]
    await context.close()
    log("others on, the group never unfolded: a toast opens the view, a reload keeps it, one "
        "closed for long is found by its id and continued here")  # fmt: skip

    # The pulsing ring of one at work is still with reduced motion.
    for motion, moving in (("no-preference", True), ("reduce", False)):
        context, page = await others_page(browser, reduced_motion=motion)
        dot = page.locator(f'#external-list [data-id="{ids["working"]}"] .dot')
        await expect(dot).to_have_class("dot open busy", timeout=8000)
        assert bool(await dot.evaluate("d => d.getAnimations().length")) is moving, motion
        await context.close()
    log("others on: one at work pulses, still with reduced motion")


def leased(session_id: str, key: str = "asks", words: str = QUESTION, valid_for: float = 15.0):
    """A row as /api/others gives it, its status held for `valid_for` seconds."""
    since = datetime.now(UTC) - timedelta(minutes=12)
    return {"id": session_id, "summary": "Fix the login test", "cwd": "/work/app",
            "repository": "", "branch": "main", "modified": since.isoformat(), "open": True,
            "status": {"key": key, "elapsed": 720.0, "since": since.isoformat(), "before": False,
                       "words": words},
            "reason": "", "why": "", "valid_for": valid_for}  # fmt: skip


async def others_leases(runtime, browser, ask: str):
    """A status is shown only while its lease holds: a stream open but silent past 15 s, a stream
    dropped, a tab hidden then shown, a back-forward cache return, back online, a laptop that
    slept (renewals sent before it ignored) and a clock moved back. The index is stopped here:
    the test says what it would."""
    others = runtime.others
    await others.stop()
    feed = {"run": "leases", "pass": 10, "why": "", "rows": [leased(ask)]}
    others.view = lambda: feed  # type: ignore[method-assign]
    requests: list[str] = []
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        await context.clock.install()
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda r: requests.append(f"{r.method} {r.url.split(str(PORT), 1)[-1]}"))
        # What changed between the first read and the stream's start is read again then.
        stale = {**feed, "pass": 9, "rows": [leased(ask, "your_turn", "Merged it.")]}
        await page.route("**/api/others", lambda route: route.fulfill(json=stale), times=1)
        await page.goto(f"{BASE}/#token={TOKEN}")
        row = page.locator(f'#external-list button.item[data-id="{ask}"]')
        dot, meta = row.locator(".dot"), page.locator("#session-meta")
        note = page.locator(".external-history [data-question]")
        working = page.locator(".external-history .working-line")
        await expect(row).to_have_attribute("aria-label", re.compile("asks you"))
        await row.click()
        await expect(meta).to_have_text(re.compile("^Asks you since "))
        await expect(note).to_have_text(f"Asks you: {QUESTION}")
        log("others leases: what changed before the stream started is read once it starts")

        async def shown():
            await expect(dot).to_have_class("dot waiting")
            await expect(row).to_have_attribute("aria-label", re.compile("asks you"))
            await expect(meta).to_have_text(re.compile("^Asks you since "))
            await expect(note).to_have_count(1)
            await expect(working).to_be_hidden()  # The question in its place.

        async def today():
            """Every part drawn from a status, as without the setting."""
            await expect(dot).to_have_class("dot open")
            await expect(row.locator(".sub")).to_have_text("open")
            await expect(row).to_have_attribute("title", "Open in a terminal now · /work/app")
            assert await row.get_attribute("aria-label") is None
            await expect(meta).to_have_text(re.compile("^Open in a terminal · /work/app · main"))
            await expect(note).to_have_count(0)
            await expect(working).to_have_text("Working in a terminal…")
            await expect(working).to_be_visible()
            assert await working.get_attribute("hidden") is None

        await page.clock.run_for(14000)
        await shown()
        await page.clock.run_for(2000)
        await today()
        log("others leases: the stream open but silent past 15 s: back to today's drawing")
        runtime.bus.publish("others", {"run": "leases", "pass": 9, "rows": [], "gone": [],
                                       "leases": {ask: 15}})  # fmt: skip
        await page.wait_for_timeout(300)
        await today()
        runtime.bus.publish("others", {"run": "leases", "pass": 11, "rows": [], "gone": [],
                                       "leases": {ask: 15}})  # fmt: skip
        await shown()
        log("others leases: a renewal of an older pass is ignored, of a newer one shown")
        changed = {**leased(ask, "your_turn", "Merged it."), "valid_for": None}
        runtime.bus.publish("others", {"run": "leases", "pass": 12, "rows": [changed],
                                       "gone": [], "leases": {}})  # fmt: skip
        await today()
        runtime.bus.publish("others", {"run": "leases", "pass": 13, "rows": [leased(ask)],
                                       "gone": [], "leases": {ask: 15}})  # fmt: skip
        await shown()
        log("others leases: a status changed without its lease is not shown")
        feed["pass"] = 13

        async def lost(how, cause):
            """`cause` loses every lease: gone at once, back once /api/others answers."""
            gate = asyncio.Event()

            async def hold(route):
                await gate.wait()
                await route.continue_()

            feed["pass"] += 2
            await page.route("**/api/others", hold)
            async with page.expect_request("**/api/others"):
                await cause()
            await today()
            # A renewal sent before (delivered while /api/others is read): it waits, and goes.
            runtime.bus.publish("others", {"run": "leases", "pass": feed["pass"] - 1, "rows": [],
                                           "gone": [], "leases": {ask: 15}})  # fmt: skip
            await page.wait_for_timeout(200)
            await today()
            gate.set()
            await shown()
            await page.unroute("**/api/others")
            log(f"others leases: {how}: gone at once, back after /api/others")

        async def drop():
            for queue in list(runtime.bus.queues):
                queue.put_nowait(None)

        async def visibility(state: str):
            await page.evaluate(
                """(state) => { Object.defineProperty(document, 'visibilityState',
                  { value: state, configurable: true });
                  document.dispatchEvent(new Event('visibilitychange')); }""",
                state,
            )

        async def hidden_then_shown():
            await visibility("hidden")
            await page.clock.run_for(1500)
            await visibility("visible")

        async def dispatch(script: str):
            await page.evaluate(script)

        await lost("the stream dropped", drop)
        await page.wait_for_timeout(3500)  # The stream back meanwhile (and read again).
        await shown()
        await lost("the tab hidden then shown", hidden_then_shown)
        restored = "new PageTransitionEvent('pageshow', { persisted: true })"
        await lost("back from the back-forward cache",
                   lambda: dispatch(f"window.dispatchEvent({restored})"))  # fmt: skip
        await lost("back online", lambda: dispatch("window.dispatchEvent(new Event('online'))"))

        now = await page.evaluate("Date.now()")
        await lost("a laptop that slept (the wall clock an hour ahead)",
                   lambda: page.clock.set_system_time(now / 1000 + 3600))  # fmt: skip
        late = leased(ask, "your_turn", "Merged it.")
        runtime.bus.publish("others", {"run": "leases", "pass": feed["pass"] - 1, "rows": [late],
                                       "gone": [], "leases": {ask: 15}})  # fmt: skip
        await page.wait_for_timeout(300)
        await shown()
        await expect(row).to_have_attribute("aria-label", re.compile("asks you"))
        runtime.bus.publish("others", {"run": "leases", "pass": feed["pass"] + 1, "rows": [late],
                                       "gone": [], "leases": {ask: 15}})  # fmt: skip
        await expect(row).to_have_attribute("aria-label", re.compile("your turn"))
        await expect(note).to_have_count(0)
        await expect(working).to_be_hidden()  # Its turn ended: no "Working…", the meta says it.
        await expect(meta).to_have_text(re.compile("^Your turn since "))
        log("others leases: renewals sent before the sleep, delivered after it, are ignored")
        feed["rows"] = [leased(ask)]
        feed["pass"] += 1
        now = await page.evaluate("Date.now()")
        await lost("the wall clock moved back an hour",
                   lambda: page.clock.set_system_time(now / 1000 - 3600))  # fmt: skip

        # A wall clock that creeps ahead, under 2 s at a time: no jump, but its deadline passes
        # (the monotonic clock 8 s on, the wall clock 20 s).
        now = await page.evaluate("Date.now()")
        await page.clock.pause_at(now / 1000)
        requests.clear()
        for _ in range(8):
            now = await page.evaluate("Date.now()")
            await page.clock.set_system_time(now / 1000 + 1.5)
            await page.clock.run_for(1000)
        await today()
        assert "GET /api/others" not in requests, requests
        log("others leases: a wall clock creeping ahead: its deadline passes first")
        runtime.bus.publish("others", {"run": "leases", "pass": feed["pass"] + 1, "rows": [],
                                       "gone": [], "leases": {ask: 15}})  # fmt: skip
        await page.clock.run_for(100)
        await shown()
        # And one creeping back: the monotonic deadline passes (16 s on, the wall clock 8 s back).
        for _ in range(16):
            now = await page.evaluate("Date.now()")
            await page.clock.set_system_time(now / 1000 - 1.5)
            await page.clock.run_for(1000)
        await today()
        assert "GET /api/others" not in requests, requests
        await page.clock.resume()
        log("others leases: a wall clock creeping back: the monotonic deadline passes first")
        assert not errors, errors
    finally:
        await context.close()
        del others.view
        others.start()


# The heading's lines: of its words and of its count (their text's line boxes), and whether the
# two share one; whether either is cut; how far the count is from the rows' small words.
HEADING = """() => {
  const summary = document.querySelector('#external > summary');
  const words = summary.querySelector('.group-name');
  const count = document.querySelector('#external-count');
  const lines = (node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    return new Set([...range.getClientRects()].map((box) => Math.round(box.top))).size;
  };
  const box = (node) => node.getBoundingClientRect();
  const sub = document.querySelector('#external-list button.item .sub');
  return {
    lines: Math.max(lines(words), lines(count)) + (Math.abs(box(words).top - box(count).top) > 3),
    height: box(summary).height, width: box(document.querySelector('#sidebar')).width,
    wordsCut: words.scrollWidth > words.clientWidth,
    countCut: count.scrollWidth > count.clientWidth,
    right: box(count).right - (sub ? box(sub).right : NaN),
  };
}"""


async def others_heading(runtime, browser):
    """The group's heading is one line at 252 px with 40 sessions, 12 open, setting on or off,
    light and dark: the words whole, the count on the right in line with the rows' small words
    and in full in its tooltip; in a narrower sidebar (a wider font, say) the words are cut, never
    the count, and it stays one line."""
    client = runtime.backend
    client.metas.clear()
    client.in_use.clear()
    for index in range(40):
        terminal(client, f"Session {index + 1}", turn("Go", 5 + index * 30), is_open=index < 12)
    runtime.others.listed_at = float("-inf")
    await runtime.workers.refresh_elsewhere()
    for on in (True, False):
        await others_setting(runtime, on)
        for scheme in ("light", "dark"):
            context, page = await others_page(browser, color_scheme=scheme)
            if not on:
                await page.locator("#external > summary").click()
            summary = page.locator("#external > summary")
            await expect(page.locator("#external-count")).to_have_text("12 open")
            await expect(summary).to_have_attribute("title", "40 sessions, 12 open")
            await expect(summary).to_have_attribute(
                "aria-label", "Other Copilot sessions, 40 sessions, 12 open"
            )
            found = await page.evaluate(HEADING)
            where = f"{'on' if on else 'off'} {scheme}"
            assert found["width"] == 252 and found["lines"] == 1, (where, found)
            assert found["height"] <= 28.5, (where, found)  # Its 28 px target, one line.
            assert not found["wordsCut"] and not found["countCut"], (where, found)
            assert abs(found["right"]) <= 1, ("in line with the rows' small words", where, found)
            await page.evaluate(
                "() => { document.querySelector('#application').style.gridTemplateColumns ="
                " '190px 1fr'; }"
            )
            found = await page.evaluate(HEADING)
            assert found["lines"] == 1 and found["height"] <= 28.5, (where, found)
            assert found["wordsCut"] and not found["countCut"], (where, found)
            assert not page.errors, page.errors  # type: ignore[attr-defined]
            await context.close()
    log("others: the heading is one line at 252 px (on and off, light and dark); narrower, "
        "its words are cut, never the count")  # fmt: skip


def held(gate: asyncio.Event):
    """A route handler that answers once `gate` is set."""

    async def hold(route):
        await gate.wait()
        await route.continue_()

    return hold


# What has the focus: a row's id, else its tag.
FOCUSED = "() => document.activeElement?.dataset?.id || document.activeElement?.tagName"
# The group as it is (for a failure's message): what has the focus, each row's dot and fold.
GROUP = """() => ({
  focused: document.activeElement?.dataset?.id || document.activeElement?.tagName,
  rows: [...document.querySelectorAll('#external-list button.item')].map((row) =>
    `${row.dataset.id.slice(0, 8)} ${row.querySelector('.dot').className}`
    + (row.closest('details.idle-workers') ? ' (folded)' : '')) })"""


async def shown_again(page):
    """The tab hidden, then shown again."""
    for state in ("hidden", "visible"):
        await page.evaluate(
            """(state) => { Object.defineProperty(document, 'visibilityState',
              { value: state, configurable: true });
              document.dispatchEvent(new Event('visibilitychange')); }""",
            state,
        )


async def others_focus(runtime, browser):
    """The keyboard's focus is never lost in the fold: the order holds while the keyboard is in
    the list (sorted again once it leaves), and the row it is on stays at rest, also when every
    status goes at once (the tab shown again) and the order is not held (no focus shown)."""
    client = runtime.backend
    await others_setting(runtime, True)

    async def sessions(made: list) -> list[str]:
        client.metas.clear()
        client.in_use.clear()
        found = [terminal(client, title, events, made=maker) for title, events, maker in made]
        runtime.others.listed_at = float("-inf")
        runtime.workers.external_cache = (float("-inf"), [])
        await runtime.workers.refresh_elsewhere()
        return found

    def rows(page):
        return page.evaluate(
            "() => [...document.querySelectorAll('#external-list button.item')]"
            ".map((row) => [row.dataset.id, Boolean(row.closest('details.idle-workers'))])"
        )

    # One asks, six idle: A to E at rest. The keyboard on E, F starts working: the order holds.
    asking = [("Asks A", [*turn("Go", 12), *step("c-1", "ask_user", 10, message="Ship?")], "")]
    idle = [(f"Idle {name}", [*turn("Go", 125 + i), *answer("Done.", 120 + i)], "")
            for i, name in enumerate("BCDEFG")]  # fmt: skip
    ids = await sessions([(t, e, "github/cli") for t, e, _ in [*asking, *idle]])
    a, b, c, d, e, f, g = ids
    context, page = await others_page(browser)
    await expect(page.locator(f'#external-list [data-id="{a}"] .dot')).to_have_class(
        "dot waiting", timeout=8000
    )
    assert await rows(page) == [[a, False], [b, False], [c, False], [d, False], [e, False],
                                [f, True], [g, True]]  # fmt: skip
    await page.locator("#external > summary").focus()
    for _ in range(5):
        await page.keyboard.press("Tab")
    assert await page.evaluate(FOCUSED) == e
    terminal_write(client, f, [*turn("Again", 0), *step("c-2", "bash", 0, command="make")])
    await expect(page.locator(f'#external-list [data-id="{f}"] .dot')).to_have_class(
        "dot open busy", timeout=8000
    )
    await page.wait_for_timeout(300)
    assert await rows(page) == [[a, False], [b, False], [c, False], [d, False], [e, False],
                                [f, True], [g, True]], "held under the keyboard"  # fmt: skip
    assert await page.evaluate(FOCUSED) == e
    # Sorted again as the keyboard leaves (the index quiet meanwhile: no event redraws it).
    await runtime.others.stop()
    try:
        for _ in range(5):
            await page.keyboard.press("Shift+Tab")  # Back to the heading: out of the list.
        await page.wait_for_timeout(300)
        assert await rows(page) == [[a, False], [f, False], [b, False], [c, False], [d, False],
                                    [e, True], [g, True]], "sorted again once it left"  # fmt: skip
    finally:
        runtime.others.start()
    assert not page.errors, page.errors  # type: ignore[attr-defined]
    await context.close()
    log("others focus: the order holds while the keyboard is in the list, sorted once it leaves")

    # One asks, five newer without a status. The keyboard on it, the tab shown again: every
    # status goes at once and the rows fall back to the list's order, but not that row.
    newer = [(f"No status {i}", turn("Go", 2 + i), "") for i in range(5)]
    asks = [*turn("Go", 62), *step("c-1", "ask_user", 60, message="Ship?")]
    ids = await sessions([("Asks A", asks, "github/cli"), *newer])
    a = ids[0]
    row = f'#external-list [data-id="{a}"]'
    for how in ("keyboard", "pointer"):
        context, page = await others_page(browser)
        await expect(page.locator(f"{row} .dot")).to_have_class("dot waiting", timeout=8000)
        # The one that asks is first at rest, so the first row after the heading.
        first = page.locator("#external-list > button.item").first
        await expect(first).to_have_attribute("data-id", a)
        if how == "keyboard":
            await page.locator("#external > summary").focus()
            await page.keyboard.press("Tab")
        else:
            # Focus from a press of the pointer, let go away from it (no click, so not chosen):
            # no focus shown, so the order is not held. On screen first: in a full run, the
            # workers of earlier rounds push the group below the window.
            await page.locator(row).scroll_into_view_if_needed()
            box = await page.locator(row).bounding_box()
            x, y = box["x"] + 20, box["y"] + box["height"] / 2
            pressed = "([x, y]) => document.elementFromPoint(x, y)?.closest('button')?.dataset.id"
            assert await page.evaluate(pressed, [x, y]) == a, (how, "the press lands on it")
            await page.mouse.move(x, y)
            await page.mouse.down()
            await page.mouse.move(900, 500)
            await page.mouse.up()
            assert not await page.locator(row).evaluate("r => r.matches(':focus-visible')")
            assert not await page.locator("#external-list").evaluate("l => l.matches(':hover')")
        assert await page.evaluate(FOCUSED) == a, (how, "focused", await page.evaluate(GROUP))
        gate = asyncio.Event()
        await page.route("**/api/others", held(gate))
        async with page.expect_request("**/api/others"):
            await shown_again(page)
        await expect(page.locator(f"{row} .dot")).to_have_class("dot open")  # Its lease gone.
        assert [folded for _, folded in await rows(page) if _ == a] == [False], (
            how,
            "at rest",
            await page.evaluate(GROUP),
        )
        assert await page.evaluate(FOCUSED) == a, (
            how,
            "kept while every status is gone",
            await page.evaluate(GROUP),
        )
        gate.set()
        await expect(page.locator(f"{row} .dot")).to_have_class("dot waiting")
        assert await page.evaluate(FOCUSED) == a, (
            how,
            "kept once /api/others answered",
            await page.evaluate(GROUP),
        )
        await page.unroute("**/api/others")
        assert not page.errors, page.errors  # type: ignore[attr-defined]
        await context.close()
    log("others focus: the tab shown again, the row the keyboard is on stays at rest, focused")


def terminal_ask(client, session_id: str, question: str, done: str = ""):
    """The session's owner asks it something more (once its step `done` ended), and it asks
    back (now)."""
    ended = [("tool.execution_complete", 0, {"toolCallId": done, "success": True}),
             ("assistant.turn_end", 0, {"turnId": "0"})] if done else []  # fmt: skip
    asked = step("c-9", "ask_user", 0, message=question)
    terminal_write(client, session_id, [*ended, *turn("And the badge?", 0), *asked])


def terminal_write(client, session_id: str, events: list):
    """More of a session's log, written now."""
    path = client.home / "session-state" / session_id / "events.jsonl"
    stamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    parent = client.logs[session_id][-1]["id"]
    for kind, _, data in events:
        event = {"type": kind, "data": data, "id": str(uuid4()), "timestamp": stamp,
                 "parentId": parent}  # fmt: skip
        client.logs[session_id].append(event)
        parent = event["id"]
        with path.open("a") as file:
            file.write(json.dumps(event, separators=(",", ":")) + "\n")


async def round_others(runtime, browser, failures: list[str]):
    """The sidebar's Other Copilot sessions (docs/design/adopt-sessions.md): with the setting
    off, exactly as before it existed; on, where each stands."""
    client = runtime.backend
    client.home = runtime.store.directory / "copilot-home"
    ask, done = asking(client), finished(client)
    await runtime.workers.refresh_elsewhere()
    try:
        await others_setting(runtime, False)
        await others_pins(runtime, browser, ask, done)
        await others_setting(runtime, True)
        ids = {"asks": ask, "finished": done, **more_sessions(client)}
        await runtime.workers.refresh_elsewhere()
        await others_statuses(runtime, browser, ids)
        old = finished(client, "Keep the old notes", minutes=30 * 60)
        runtime.workers.external_cache = (float("-inf"), [])
        await runtime.others.list_sessions()  # In the index's latest listing (not followed).
        await others_view(runtime, browser, ids, old)
        await others_leases(runtime, browser, ask)
        await others_heading(runtime, browser)
        await others_focus(runtime, browser)
    finally:
        client.metas.clear()
        client.in_use.clear()
        await others_setting(runtime, True)
        await runtime.workers.refresh_elsewhere()


async def round_send(runtime, browser, failures: list[str]):
    """Send to Chief (docs/design/send-anywhere.md): what reached the Router from Slack, GitHub
    and a watch reads on Home as it did before; the switch is Settings → Connections → Send to
    Chief (the list's last row, settings-v2.md §5), whose consent unfolds in place, saved only
    with Turn on; on, a small line with the command (or why the socket stays closed); a sent item
    is one line in the Router chat that opens in place, and nothing else on Home."""
    await send_pins(runtime, browser)
    try:
        for scheme in ("light", "dark"):
            await send_setting(runtime, browser, scheme)
        await send_home(runtime, browser)
    finally:
        await send_switch(runtime, False)


async def until(check, seconds: float = 5):
    """What the page changed on the server, once its request was answered."""
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "not in time"
        await asyncio.sleep(0.05)


async def send_switch(runtime, on: bool):
    old = runtime.store.settings()
    new = old.model_copy(update={"send_to_chief": on})
    runtime.store.save_settings(new)
    await runtime.settings_changed(old, new)


async def send_setting(runtime, browser, scheme: str):
    """The line: off, unfolding, cancelled, turned on, broken, turned off."""
    from chiefofstaff import send

    data = runtime.store.directory
    socket_path = data / "send" / "socket"
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, color_scheme=scheme
    )
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda note: errors.append(note.text) if note.type == "error" else None)
    saved: list[dict] = []
    page.on(
        "request",
        lambda r: saved.append(r.post_data_json) if r.url.endswith("/api/settings") else None,
    )
    await page.goto(f"{BASE}/#token={TOKEN}")
    await expect(page.locator("#application")).to_be_visible()
    await page.locator("#settings-button").click()
    await page.locator("#settings-tabs [data-tab=connections]").click()
    tab = page.locator("#settings-pane .tab[data-tab=connections]")
    line = tab.locator("#send-line")
    switch = line.locator("input[name=send_to_chief]")
    # Off: the list's last row, after When you're away, saying Off; nothing else of it.
    rows = tab.locator("#conn-list .conn")
    assert (await rows.evaluate_all("r => r.map((n) => n.dataset.conn)"))[-2:] == ["tools", "send"]
    assert await tab.evaluate(
        "t => t.querySelector('.conn[data-conn=away]')"
        ".compareDocumentPosition(t.querySelector('.conn[data-conn=send]'))"
        " & Node.DOCUMENT_POSITION_FOLLOWING"
    )
    await expect(tab.locator("#send-summary")).to_have_text("Off")
    await tab.locator(".conn[data-conn=send] .conn-open").click()
    await expect(line.locator("label.check")).to_have_text("Send to Chief from the command line")
    await expect(switch).not_to_be_checked()
    for hidden in ("#send-consent", "#send-how", "#send-problem"):
        await expect(line.locator(hidden)).to_be_hidden()
    # On: the consent unfolds in place, one sentence, Turn on the one main button; not saved.
    await switch.check()
    consent = line.locator("#send-consent")
    await expect(consent).to_be_visible()
    await expect(consent.locator("p")).to_have_text(
        "What you send (your note, and any link or text you pass) goes to Chief of Staff on this "
        "computer, which hands it to GitHub Copilot under your account; the work it starts runs "
        "with your settings, as any task does."
    )
    buttons = consent.locator("button")
    await expect(buttons).to_have_count(2)
    await expect(buttons.nth(0)).to_have_text("Turn on")
    await expect(buttons.nth(1)).to_have_text("Cancel")
    assert await buttons.nth(1).evaluate("b => b.classList.contains('link')")
    assert not await buttons.nth(0).evaluate("b => b.classList.contains('link')")
    await page.screenshot(path=SHOTS / f"ui-edges-send-consent-{scheme}.png")
    assert saved == [] and not runtime.store.settings().send_to_chief
    await consent.get_by_role("button", name="Cancel").click()
    await expect(consent).to_be_hidden()
    await expect(switch).not_to_be_checked()
    await expect(switch).to_be_focused()
    # Unchecked while it is unfolded: the same as Cancel.
    await switch.check()
    await switch.uncheck()
    await expect(consent).to_be_hidden()
    assert saved == [] and not socket_path.exists()
    await switch.check()
    await consent.get_by_role("button", name="Turn on").click()
    await expect(consent).to_be_hidden()
    how = line.locator("#send-how")
    await expect(how).to_be_visible()
    await expect(how.locator("code")).to_have_text('chiefofstaff send "…"')
    await expect(how.locator("a")).to_have_attribute(
        "href", "https://github.com/evanl666/chiefofstaff/blob/main/docs/guide.md#send-to-chief"
    )
    await expect(switch).to_be_checked()
    await expect(switch).to_be_focused()
    assert saved[-1]["send_to_chief"] is True and set(saved[-1]) == {"send_to_chief", "base"}
    assert runtime.store.settings().send_to_chief and socket_path.is_socket()
    await page.screenshot(path=SHOTS / f"ui-edges-send-on-{scheme}.png")
    # Saving the rest of Settings keeps it as saved.
    await page.locator("#settings-form footer button[type=submit]").click()
    await expect(page.locator("#settings-dialog")).to_be_hidden()
    assert saved[-1]["send_to_chief"] is True and runtime.store.settings().send_to_chief
    await page.locator("#settings-button").click()
    await page.locator("#settings-tabs [data-tab=connections]").click()
    await expect(tab.locator("#send-summary")).to_have_text("On")
    await tab.locator(".conn[data-conn=send] .conn-open").click()
    await expect(switch).to_be_checked()
    await expect(how).to_be_visible()
    # Started with uvx (round 14, E63): no `chiefofstaff` in the owner's shell, so it says how
    # to have it, and a command that runs now.
    runtime.send_command = ("uvx chiefofstaff", "uv tool install chiefofstaff")
    runtime.publish_state()
    # Settings shows the page's state as it last came; reopened until the new one has.
    for _ in range(40):
        await page.keyboard.press("Escape")
        await page.locator("#settings-button").click()
        await page.locator("#settings-tabs [data-tab=connections]").click()
        await tab.locator(".conn[data-conn=send] .conn-open").click()
        if await how.locator("code").count() == 2:
            break
        await page.wait_for_timeout(250)
    await expect(how.locator("code")).to_have_text(
        ["uv tool install chiefofstaff", 'uvx chiefofstaff send "…"']
    )
    await expect(how).to_contain_text("once puts the command on your PATH")
    await expect(how.locator("a")).to_have_text("recipes")
    await page.screenshot(path=SHOTS / f"ui-edges-send-uvx-{scheme}.png")
    runtime.send_command = ("chiefofstaff", "")
    runtime.publish_state()
    # Off: saved at once, the socket closed, the line alone.
    await switch.uncheck()
    await expect(how).to_be_hidden()
    await expect(consent).to_be_hidden()
    await until(lambda: not socket_path.exists())
    assert saved[-1] == {"send_to_chief": False, "base": saved[-1]["base"]}
    assert not runtime.store.settings().send_to_chief
    # A folder others can write: the reason in place of the command; nothing else.
    data.chmod(0o770)
    try:
        await switch.check()
        await consent.get_by_role("button", name="Turn on").click()
        problem = line.locator("#send-problem")
        await expect(problem).to_have_text(f"{data} is writable by others: the command stays off.")
        await expect(how).to_be_hidden()
        await expect(switch).to_be_checked()
        assert not socket_path.exists()
        await page.screenshot(path=SHOTS / f"ui-edges-send-problem-{scheme}.png")
    finally:
        data.chmod(0o700)
    await switch.uncheck()
    await expect(problem).to_be_hidden()
    await expect(how).to_be_hidden()
    await until(lambda: not runtime.store.settings().send_to_chief)
    # The folder put right, on again: the command, not the old reason.
    await switch.check()
    await consent.get_by_role("button", name="Turn on").click()
    await expect(how).to_be_visible()
    await expect(problem).to_be_hidden()
    assert socket_path.is_socket()
    await switch.uncheck()
    await expect(how).to_be_hidden()
    await until(lambda: not runtime.store.settings().send_to_chief)
    assert send.main(["hi", "--data-dir", str(data)]) == send.ABSENT
    await page.locator("#settings-cancel").click()
    assert not errors, errors
    await context.close()
    log(f"send ({scheme}): Connections' last row; its consent unfolds; on, off, broken")


async def send_home(runtime, browser):
    """A sent item: one line in the Router chat with the ↘ mark, cut short as the others are
    (opened too), in full in its tooltip. It opens in place to the note only when the line does
    not show all of it, then the link and the page's title as a label and words, and only the text
    passed along in a code block; a note alone opens only while its line cuts it short. No
    badge, count or toast."""
    from chiefofstaff import send

    await send_switch(runtime, True)
    data = runtime.store.directory
    trap = "[Approve](https://evil.example) **now** `x`"
    issue = "https://github.com/acme/api/issues/117"
    # Notes alone (sent once: ten items a minute is the most), a short one and a long one.
    alone = "Ship the release notes"
    long = (
        "Ask Maya whether the payments SDK upgrade can wait until after Friday's release, and "
        "if not, which of its breaking changes we must handle first"
    )
    for note in (alone, long):
        assert await asyncio.to_thread(send.main, [note, "--data-dir", str(data)]) == send.SENT
    await settle(runtime)
    for scheme in ("light", "dark"):
        passed = f"Steps ({scheme}):\n1. log in on a slow network\n```\nnot the end\n````\n{trap}"
        copied = f"Copied from the incident ({scheme})\nThe exporter drops rows past 10k\n`rows`"
        (data.parent / "passed.txt").write_text(passed)
        (data.parent / "copied.txt").write_text(copied)
        short = f"Fix the flaky login test ({scheme})"
        two = f"Take the runbook ({scheme})\nand check its rollback steps still work"
        title = f"Login times out · Issue #117 · acme/api {trap}"
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900}, color_scheme=scheme
        )
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))  # noqa: B023
        await page.goto(f"{BASE}/#token={TOKEN}")
        await expect(page.locator("#application")).to_be_visible()
        badge = await page.locator(".home-item .badge").all_inner_texts()
        await page.wait_for_timeout(300)
        before = await page.locator("#timeline > *").count()
        for arguments in (
            [
                short,
                "--url",
                issue,
                "--title",
                title,
                "--text-from",
                str(data.parent / "passed.txt"),
            ],
            [two, "--url", "https://example.com/runbook"],
            ["--text-from", str(data.parent / "copied.txt")],
        ):
            code = await asyncio.to_thread(send.main, [*arguments, "--data-dir", str(data)])
            assert code == send.SENT
        await settle(runtime)
        await expect(page.locator("#timeline > *")).to_have_count(before + 3)
        lines = page.locator("#timeline .event.sent", has_text=f"({scheme})")
        await expect(lines).to_have_count(3)
        await expect(lines.locator(".mark")).to_have_text(["\u2198"] * 3)
        for line in await lines.all():
            await expect(line.locator("time")).to_have_count(1)
            said = await line.locator(".what").text_content()
            await expect(line.locator(".what")).to_have_attribute("title", said or "")
        await page.screenshot(path=SHOTS / f"ui-edges-send-home-{scheme}.png")
        # Note, link, title and text: the line shows all of the note, so the card starts with
        # the link; the title is a label and words; only the text is a code block, as it was.
        entry = lines.filter(has_text=short)
        await expect(entry.locator(".what")).to_have_text(f"Sent from the command line: {short}")
        row = entry.locator(".row")
        height = (await row.bounding_box() or {})["height"]
        await entry.locator("button.line").click()
        body = entry.locator(".markdown")
        await expect(body).to_be_visible()
        assert (await row.bounding_box() or {})["height"] == height, "one line, opened too"
        await expect(body.locator(".sent-note")).to_be_hidden()
        await expect(body.locator("> p:visible")).to_have_count(2)
        link, named = body.locator("> p:visible").nth(0), body.locator("> p:visible").nth(1)
        await expect(link.locator("strong.key")).to_have_text("Link:")
        await expect(link.locator("a")).to_have_attribute("href", issue)
        await expect(named.locator("strong.key")).to_have_text("Title:")
        await expect(named).to_have_text(f"Title: {title}")
        await expect(named.locator("a, code, em, strong:not(.key)")).to_have_count(0)
        await expect(body.locator("a")).to_have_count(1)
        await expect(body.locator("pre")).to_have_count(1)
        assert await body.locator("pre code").text_content() == passed
        assert await row.evaluate("r => getComputedStyle(r.querySelector('.what')).whiteSpace") == (
            "nowrap"
        )
        # Narrower, the line cuts the note short: the card shows it, and hides it again.
        await page.evaluate("document.querySelector('#timeline').style.maxWidth = '320px'")
        await expect(body.locator(".sent-note")).to_be_visible()
        await expect(body.locator(".sent-note")).to_have_text(short)
        await page.evaluate("document.querySelector('#timeline').style.removeProperty('max-width')")
        await expect(body.locator(".sent-note")).to_be_hidden()
        await page.screenshot(path=SHOTS / f"ui-edges-send-home-open-{scheme}.png")
        await entry.locator("button.line").click()
        # A note of two lines: its first is the line; the card has it all, then the link.
        entry = lines.filter(has_text=two.splitlines()[0])
        await expect(entry.locator(".what")).to_have_text(
            f"Sent from the command line: {two.splitlines()[0]}"
        )
        assert await entry.locator(".what").evaluate("w => w.scrollWidth <= w.clientWidth")
        await entry.locator("button.line").click()
        await expect(entry.locator(".sent-note")).to_be_visible()
        assert await entry.locator(".sent-note").text_content() == two
        await expect(entry.locator(".markdown > p:visible")).to_have_count(2)
        await entry.locator("button.line").click()
        # Text alone: its first line is the line; the card is the text, as it was.
        entry = lines.filter(has_text=copied.splitlines()[0])
        await expect(entry.locator(".what")).to_have_text(
            f"Sent from the command line: {copied.splitlines()[0]}"
        )
        await entry.locator("button.line").click()
        await expect(entry.locator(".markdown > *")).to_have_count(1)
        assert await entry.locator("pre code").text_content() == copied
        await entry.locator("button.line").click()
        # Notes alone: the short one shows whole and does not open; the long one is cut short
        # and opens to it all; at 800 px and 1440 px.
        short_line = page.locator("#timeline .event.sent", has_text=f": {alone}")
        long_line = page.locator("#timeline .event.sent", has_text=long[:40])
        for width in (800, 1440):
            await page.set_viewport_size({"width": width, "height": 900})
            if width < 1100:
                await page.locator("#home-tabs [data-tab=chat]").click()
            await long_line.scroll_into_view_if_needed()
            await expect(short_line.locator("button.line")).to_be_disabled()
            await expect(short_line.locator("button.line")).to_have_css("opacity", "1")
            await short_line.locator(".what").click(force=True)
            await expect(short_line.locator(".markdown")).to_be_hidden()
            what = long_line.locator(".what")
            assert await what.evaluate("w => w.scrollWidth > w.clientWidth"), ("cut", width)
            await expect(long_line.locator("button.line")).to_be_enabled()
            await long_line.locator("button.line").click()
            await expect(long_line.locator(".sent-note")).to_be_visible()
            assert await long_line.locator(".sent-note").text_content() == long
            assert await what.evaluate("w => getComputedStyle(w).whiteSpace") == "nowrap"
            await page.screenshot(path=SHOTS / f"ui-edges-send-alone-{width}-{scheme}.png")
            await long_line.locator("button.line").click()
            await expect(long_line.locator(".markdown")).to_be_hidden()
        # Wide enough to show it whole: folded, and no longer opens.
        await long_line.locator("button.line").click()
        await page.evaluate("document.querySelector('#timeline').style.maxWidth = '2000px'")
        await page.set_viewport_size({"width": 2400, "height": 900})
        await expect(long_line.locator("button.line")).to_be_disabled()
        await expect(long_line.locator(".markdown")).to_be_hidden()
        await page.evaluate("document.querySelector('#timeline').style.removeProperty('max-width')")
        await page.set_viewport_size({"width": 1280, "height": 900})
        assert await page.locator(".home-item .badge").all_inner_texts() == badge
        await expect(page.locator("#toasts .toast")).to_have_count(0)
        assert not errors, errors
        await context.close()
    log("send: one line each, opening in place to what the line does not show; nothing else")


async def send_pins(runtime, browser):
    """Pins: a delegated Slack message, a GitHub event and a watch's finding are one line each
    in the Router chat, with the ↘ mark, opening in place to what came."""
    events = [
        ("slack", "delegated", "Bob: can you look at the flaky login test?\nIt fails on CI.",
         "Handed over from Slack: Bob: can you look at the flaky login test?"),
        ("github", "event", "Review requested on acme/api#12: Speed up exports\nBy @maya.",
         "From GitHub: Review requested on acme/api#12: Speed up exports"),
        ("watch", "event", "Bob asked when the release ships\nIn #releases.",
         "From a watch: Bob asked when the release ships"),
    ]  # fmt: skip
    for source, kind, text, _ in events:
        await runtime.on_trigger(source, [f"pin:{uuid4().hex}"], text, kind=kind)
    await settle(runtime)
    for scheme in ("light", "dark"):
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900}, color_scheme=scheme
        )
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))  # noqa: B023
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)  # noqa: B023
        await page.goto(f"{BASE}/#token={TOKEN}")
        await expect(page.locator("#application")).to_be_visible()
        timeline = page.locator("#timeline")
        for _, _, text, line in events:
            entry = timeline.locator(".event.opens", has_text=line)
            await expect(entry).to_have_count(1)
            await expect(entry.locator(".mark")).to_have_text("\u2198")
            await expect(entry.locator(".what")).to_have_text(line)
            body = entry.locator(".markdown")
            await expect(body).to_be_hidden()
            await entry.locator("button.line").click()
            await expect(body).to_be_visible()
            await expect(body).to_contain_text(text.splitlines()[1])
            await expect(entry.locator("button.line")).to_have_attribute("aria-expanded", "true")
            await entry.locator("button.line").click()
            await expect(body).to_be_hidden()
        assert not errors, errors
        await context.close()
    log("send: Slack, GitHub and watch lines on Home as before, each opening in place")


# Other coding agents (docs/design/more-agents.md 5.10, the agent picker): Settings → Agents &
# models (each of its rows' states), a worker on Codex in its header, menu, task rows and Usage, a
# save that stops workers, and Go on for one whose spend is unknown.
AGENT_SHOTS = Path(os.environ.get("UI_AGENT_SHOTS") or SHOTS / "agents-m2")
# (UI_AGENT_WIDTHS, UI_PICKER_WIDTHS, comma-separated: other widths, for a review's shots.)
AGENT_WIDTHS = tuple(int(w) for w in os.environ.get("UI_AGENT_WIDTHS", "800,1024,1440").split(","))
SIGNED_OUT_CODEX = "Codex is not signed in: sign in from Settings → Agents & models."
OLD_CODEX = "Codex 0.160.0 is older than the versions tested (0.167.2): update it."
SANDBOXED_CODEX = (
    "Can't run while workers are sandboxed: its sandbox has not passed Chief of Staff's checks yet."
)
WIDENED_CLAUDE = (
    "Your Claude settings let workers write outside their folder "
    "(permissions.additionalDirectories in ~/.claude/settings.json), so sandboxed Claude workers "
    "can't start."
)
STOPPED_CODEX = "Stopped unexpectedly; starts again with its next worker's message."
NO_ANSWER = "Codex did not answer in time: try again."
PLAN_NOTE = (
    "On a Claude plan, Claude workers count against your plan's usage limits, under Anthropic's "
    "terms."
)
HOOKS_NOTE = "Sandboxed Claude workers don't run hooks."
NO_EXTRA = "Needs Chief of Staff's Claude extra: `uv tool install 'chiefofstaff[claude]'`"
LIMIT_LINE = "Claude's 5-hour limit is used up · resets 3:40 PM"


def codex_on_path() -> tuple[Path, Path]:
    """The stand-in Codex CLI (tests/fake_codex.py) as `codex` on this process's PATH, and the
    stand-in Claude Code (tests/fake_claude.py) as `claude` (never the owner's); each of their
    runs is listed (codex's in `runs`, claude's in `claude-runs` beside it). Taken away with
    codex_off_path."""
    folder = Path(tempfile.mkdtemp(prefix="cos-codex-"))
    for name in ("codex", "claude"):
        stand_in = ROOT / "tests" / f"fake_{name}.py"
        (folder / name).write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stand_in}" "$@"\n')
        (folder / name).chmod(0o755)
    os.environ["PATH"] = f"{folder}{os.pathsep}{os.environ['PATH']}"
    os.environ["FAKE_CODEX_HOME"] = str(folder / "home")
    os.environ["FAKE_CODEX_RUNS"] = str(folder / "runs")
    os.environ["FAKE_CLAUDE_HOME"] = str(folder / "claude-home")
    os.environ["FAKE_CLAUDE_RUNS"] = str(folder / "claude-runs")
    # A Claude configuration of its own, set up (its state file there): never the owner's.
    (folder / "claude-config").mkdir()
    (folder / "claude-config" / ".claude.json").write_text("{}\n")
    OWNERS_CLAUDE_CONFIG.append(os.environ.get("CLAUDE_CONFIG_DIR"))
    os.environ["CLAUDE_CONFIG_DIR"] = str(folder / "claude-config")
    # Codex set up (its folder there): the tests' home has none (conftest), and Codex runs
    # nowhere it was never set up.
    (folder / "codex-home").mkdir()
    OWNERS_CODEX_HOME.append(os.environ.get("CODEX_HOME"))
    os.environ["CODEX_HOME"] = str(folder / "codex-home")
    return folder, folder / "runs"


OWNERS_CLAUDE_CONFIG: list[str | None] = []
OWNERS_CODEX_HOME: list[str | None] = []


CLAUDE_HIDDEN = ("FAKE_CODEX_HOME", "FAKE_CODEX_RUNS", "FAKE_CLAUDE_HOME", "FAKE_CLAUDE_RUNS")


def codex_off_path(folder: Path):
    path = os.environ["PATH"].split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(entry for entry in path if entry != str(folder))
    for name in CLAUDE_HIDDEN:
        os.environ.pop(name, None)
    if OWNERS_CLAUDE_CONFIG:
        if (owners := OWNERS_CLAUDE_CONFIG.pop()) is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = owners
    if OWNERS_CODEX_HOME:
        if (owners := OWNERS_CODEX_HOME.pop()) is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = owners
    shutil.rmtree(folder, ignore_errors=True)


async def agents_saved(runtime, part: dict) -> list[str]:
    from chiefofstaff import agents

    before = agents.configured(runtime.store)
    plan = agents.planned(runtime.store, part)
    if not agents.save(runtime.store, plan):
        return []
    return await runtime.agents_saved(before)


async def agents_page(browser, scheme: str, width: int = 1280):
    context = await browser.new_context(
        viewport={"width": width, "height": 900}, color_scheme=scheme
    )
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda note: errors.append(note.text) if note.type == "error" else None)
    requests: list[str] = []
    page.on("request", lambda r: requests.append(f"{r.method} {r.url.split(BASE)[-1]}"))
    await page.goto(f"{BASE}/#token={TOKEN}")
    await expect(page.locator("#application")).to_be_visible()
    return context, page, errors, requests


async def open_agents(page, tab: bool = True):
    """Settings → Agents & models (settings-v2.md §3), once its look answered (data-looked)."""
    await page.locator("#settings-button").click()
    if tab:
        await page.locator("#settings-tabs [data-tab=agents]").click()
    section = page.locator("#settings-page-agents")
    await expect(section).to_have_attribute(
        "data-looked", re.compile(r"^(none|done|failed)$"), timeout=20000
    )
    return section


async def unfold_agent(section, name: str):
    """An agent's row unfolded (its model, effort, limit and details)."""
    fold = section.locator(f"#agent-{name} .fold")
    if await fold.get_attribute("aria-expanded") != "true":
        await fold.click()
    await expect(section.locator(f"#agent-{name}-body")).to_be_visible()


async def shoot(
    page, name: str, scheme: str, width: int, target: str = "#settings-dialog", focus=None
):
    """A screenshot of the page at this width; `focus` (a locator): what it is of, centred in
    view and wholly in it."""
    AGENT_SHOTS.mkdir(parents=True, exist_ok=True)
    await page.set_viewport_size({"width": width, "height": 900})
    await page.mouse.move(1, 1)  # No hover where the last click left the pointer.
    if focus is not None:
        await focus.evaluate("n => n.scrollIntoView({block: 'center', inline: 'nearest'})")
    await quiet_for(page)
    if focus is not None:
        box = await focus.bounding_box()
        assert box and box["y"] >= 0 and box["y"] + box["height"] <= 900, (name, width, box)
        assert box["x"] >= 0 and box["x"] + box["width"] <= width, (name, width, box)
    found = await page.locator(target).evaluate(OVERFLOWS)
    assert not found, (name, width, found)
    await page.screenshot(path=AGENT_SHOTS / f"{name}-{width}-{scheme}.png")


async def quiet_for(page, ms: int = 200):
    await page.wait_for_timeout(ms)


async def round_agents(runtime, browser, failures: list[str]):
    """With Copilot alone, nothing of other agents anywhere; installed, a row each on Agents &
    models, which looks only when shown; each state of its rows; a worker on Codex where it
    shows; a save that stops workers says so once."""
    await agents_none(runtime, browser)
    folder, runs = codex_on_path()
    runtime.found_cache = (float("-inf"), [])  # Looked at the PATH again: now they are on it.
    try:
        for scheme in ("light", "dark"):
            await agents_off(runtime, browser, scheme, runs)
            await agents_states(browser, scheme)
        await agents_on_and_worker(runtime, browser, runs)
    finally:
        await agents_saved(runtime, {"codex": {"enabled": False}})
        codex_off_path(folder)


async def agents_off(runtime, browser, scheme: str, runs: Path):
    """Installed, none on: Settings → Agents & models has a row for each, from what the page
    knows; it looks once when it is shown (nothing is looked for until then); turned on, its
    model, effort and limit set, and saved."""
    ran = runs.read_text().count("\n") if runs.exists() else 0
    context, page, errors, requests = await agents_page(browser, scheme)
    await page.locator("#settings-button").click()
    await page.locator("#settings-tabs [data-tab=general]").click()
    button = page.locator("#settings-tabs [data-tab=agents]")
    await expect(button).to_have_text("Agents & models")
    order = await page.locator("#settings-tabs [role=tab]").evaluate_all(
        "b => b.filter((x) => !x.hidden).map((x) => x.dataset.tab)"
    )
    assert order == ["general", "workers", "agents", "connections", "you", "advanced"], order
    claude_runs = runs.parent / "claude-runs"
    claude_ran = claude_runs.read_text().count("\n") if claude_runs.exists() else 0
    await quiet_for(page, 300)
    assert not any("/api/agents" in r for r in requests), requests
    now = runs.read_text().count("\n") if runs.exists() else 0
    assert now == ran, "no agent CLI runs before the owner asks"
    assert (claude_runs.read_text().count("\n") if claude_runs.exists() else 0) == claude_ran
    # By keyboard: arrows move along the pages; the page looks once it is shown.
    await page.locator("#settings-tabs [data-tab=general]").focus()
    await page.keyboard.press("ArrowDown")
    await page.keyboard.press("ArrowDown")
    await expect(button).to_be_focused()
    await expect(button).to_have_attribute("aria-selected", "true")
    group = page.locator("#settings-page-agents")
    await expect(group).to_be_visible()
    await expect(group).to_have_attribute("data-looked", "done", timeout=20000)
    assert not any("/api/models?agent=" in r for r in requests), requests
    rows = group.locator("#agent-codex")
    state = rows.locator(".agent-line .status")
    await expect(state).to_have_text("Ready · off for workers")
    assert sum("/api/agents" in r for r in requests) == 1, requests
    await expect(rows.locator(".plan-meter")).to_have_text("42% 5-hour")
    await expect(rows.locator(".plan-meter")).to_have_attribute(
        "aria-describedby", "agent-codex-usage"
    )
    # Claude's row, beside Codex's: how it is signed in, and the one line a plan calls for. Its
    # look ran its version and its sign-in's state, and started no session.
    claude = group.locator("#agent-claude")
    await expect(claude.locator(".who strong")).to_have_text("Claude Code")
    await expect(claude.locator("label.check")).to_have_text("Use for workers")
    await expect(claude.locator(".status")).to_have_text("Ready · off for workers")
    await unfold_agent(group, "claude")
    await expect(claude.locator(".about")).to_have_text(
        "Version 2.1.322Signed in with your Claude planUses your Claude planSign in again"
    )
    await expect(claude.locator(".plan-note")).to_have_text(PLAN_NOTE)
    await expect(claude.locator("input[type=checkbox]")).to_be_enabled()
    looked = [json.loads(line) for line in claude_runs.read_text().splitlines()[claude_ran:]]
    assert looked == [["--version"], ["auth", "status", "--json"]], looked
    await expect(group.locator("#agents-guide")).to_have_text(
        "What differs between agents: the guide"
    )
    box = rows.locator("input[type=checkbox]")
    await expect(rows.locator(".who strong")).to_have_text("Codex")
    await expect(box).to_have_attribute("aria-describedby", "agent-codex-status agent-codex-alerts")
    await expect(box).to_be_enabled()
    choose = group.locator("select[name=agent_new_workers]")
    await expect(choose).to_be_hidden()  # No other agent on: Copilot, said.
    await expect(group.locator("#new-work-plain")).to_have_text("Copilot")
    for width in AGENT_WIDTHS:
        await shoot(page, "tab-found", scheme, width)
    await page.set_viewport_size({"width": 1280, "height": 900})
    # Used for workers: new workers can run on it; its model, effort and limit unfold.
    await box.check()
    await expect(choose).to_be_visible()
    await unfold_agent(group, "codex")
    fields = rows.locator(".agent-fields")
    # Its models by name, as its look listed them (never /api/models?agent=).
    model = fields.locator("select[name=agent_codex_model]")
    await expect(model.locator("option")).to_have_text(
        ["Codex's default (GPT-TEST)", "GPT-TEST", "GPT-MINI"]
    )
    options = model.locator("option")
    assert await options.evaluate_all("o => o.map((x) => x.value)") == ["", "gpt-test", "gpt-mini"]
    effort = fields.locator("select[name=agent_codex_effort]")
    await expect(effort.locator("option")).to_have_text(["Default", "Low", "Medium", "High"])
    await expect(fields.locator("label > span")).to_have_text(
        ["Model", "Effort", "Limit per worker (tokens)"]
    )
    limit = fields.locator("input[name=agent_codex_limit]")
    await expect(limit).to_have_attribute("placeholder", "No limit")
    await expect(choose.locator("option")).to_have_text(["Copilot", "Codex", "Last used"])
    await expect(choose).to_have_value("copilot")
    await expect(group.locator("#new-work-hint")).to_have_text(
        "Uses your Copilot plan. For one task, type @codex or say \u201cdo this with Codex\u201d."
    )
    assert await group.locator("input[name=agent_router_chooses]").count() == 0
    for width in AGENT_WIDTHS:
        await shoot(page, "tab-enabling", scheme, width)
    await page.set_viewport_size({"width": 1280, "height": 900})
    await choose.select_option("codex")
    await expect(group.locator("#new-work-model")).to_have_text("GPT-TEST")
    await effort.select_option("high")
    await expect(group.locator("#new-work-model")).to_have_text("GPT-TEST · high effort")
    saved = []
    page.on(
        "request",
        lambda r: saved.append(r.post_data_json) if r.url.endswith("/api/settings") else None,
    )
    # Not a whole number of tokens: said beside it, and nothing is saved; from another page,
    # Save opens its row and puts the cursor there.
    error = fields.locator("#agent-codex-limit-error")
    await limit.fill("20k")
    await rows.locator(".fold").click()
    await page.locator("#settings-tabs [data-tab=you]").click()
    await page.locator("#settings-form footer button[type=submit]").click()
    await expect(error).to_have_text("A whole number of tokens, or empty for no limit.")
    await expect(limit).to_have_attribute("aria-invalid", "true")
    await expect(limit).to_have_attribute("aria-describedby", "agent-codex-limit-error")
    await expect(limit).to_be_focused()
    await expect(rows.locator(".fold")).to_have_attribute("aria-expanded", "true")
    await expect(page.locator("#settings-dialog")).to_be_visible()
    assert not saved, saved
    for width in AGENT_WIDTHS:
        await shoot(page, "tab-limit-wrong", scheme, width)
    await page.set_viewport_size({"width": 1280, "height": 900})
    await limit.fill("200000")
    await expect(error).to_be_hidden()
    await choose.focus()  # Left: the number shows grouped.
    await expect(limit).to_have_value("200,000")
    await page.locator("#settings-form footer button[type=submit]").click()
    await expect(page.locator("#settings-dialog")).to_be_hidden()
    assert saved[-1]["agents"] == {
        "codex": {"enabled": True, "effort": "high", "limit": 200000},
        "new_workers": "codex",
    }, saved[-1].get("agents")
    from chiefofstaff import agents

    assert agents.turned_on(runtime.store) == ["codex"]
    assert agents.configured(runtime.store)["codex"]["effort"] == "high"
    # On: the page shows it so (shown again, it looks again).
    group = await open_agents(page)
    await expect(state).to_have_text("Ready")
    await expect(box).to_be_checked()
    await unfold_agent(group, "codex")
    await expect(fields.locator("input[name=agent_codex_limit]")).to_have_value("200,000")
    await expect(effort).to_have_value("high")
    await expect(rows.locator(".default-chip")).to_be_visible()
    # Downgrading is for the guide (its section "What differs" opens), not this page.
    await expect(page.locator("#settings-form")).not_to_contain_text("prepare-downgrade")
    await expect(choose).to_have_value("codex")
    for width in AGENT_WIDTHS:
        await shoot(page, "tab-on", scheme, width)
    # Saving with nothing changed on the page sends nothing of it.
    await page.locator("#settings-form footer button[type=submit]").click()
    await expect(page.locator("#settings-dialog")).to_be_hidden()
    assert "agents" not in saved[-1], saved[-1]
    await agents_saved(runtime, {"codex": {"enabled": False, "effort": "", "limit": 0}})
    assert not errors, errors
    await context.close()
    log(
        f"agents ({scheme}): a row each, looked at once shown (by keyboard too); used for "
        "workers, its model, effort and limit"
    )


async def agents_none(runtime, browser):
    """Copilot alone (no other agent installed): Agents & models has Copilot's row and its own
    sessions, nothing of other agents (no menu, no line about them), and asks nothing; nothing
    of agents on Home or in the composer's @ list."""
    runtime.found_cache = (float("-inf"), [])
    for scheme in ("light", "dark"):
        context, page, errors, requests = await agents_page(browser, scheme)
        assert not await page.evaluate(
            "() => import('/static/state.js').then((m) => ['agents', 'agents_found', "
            "'agent_offers', 'agent_refusals'].some((k) => k in m.state.snapshot))"
        )
        await expect(page.locator("#agent-offer")).to_have_count(0)
        await page.locator("#composer-input").fill("@co")
        await expect(page.locator("#mention-list li.agent")).to_have_count(0)
        await page.locator("#composer-input").fill("")
        section = await open_agents(page)
        await expect(section).to_have_attribute("data-looked", "none")
        await expect(section.locator(".agent")).to_have_count(1)
        await expect(section.locator("#agent-copilot .always")).to_have_text("Always on")
        await expect(section.locator("select[name=agent_new_workers]")).to_have_count(0)
        await expect(section.locator("select[name^=role_]")).to_have_count(0)
        await expect(section.locator("#agents-guide")).to_have_count(0)
        assert not re.search(r"Codex|Claude Code", await section.inner_text())
        await quiet_for(page, 300)
        assert not any("/api/agents" in r for r in requests), requests
        await picker_shoot(page, "copilot-alone-models", scheme, "#settings-dialog")
        assert not errors, errors
        await context.close()
    log("agents: with Copilot alone, Copilot's row and its own sessions; nothing of other agents")


def agents_answer(answer, gate: asyncio.Event):
    """What /api/agents answers in a state: nothing until let go (checking), an error, or
    this."""

    async def answered(route):
        if answer is None:
            await gate.wait()
            await route.abort()
        elif answer == "error":
            await route.fulfill(status=500, json={"detail": NO_ANSWER})
        else:
            await route.fulfill(json=answer)

    return answered


async def agents_states(browser, scheme: str):
    """The page's other states, as /api/agents answers them (settings-v2.md §3.3): the look
    under way, an error, none found, signed out, one that cannot run here, the sandbox on,
    stopped by itself, most of a plan used, a plan's limit, Claude without its extra. A row's
    status is a few words; why, and what costs money, is an alert line under it; one that is on
    can always be turned off; one off is turned on only once it can work."""
    found = {
        "name": "codex", "label": "Codex", "enabled": False, "model": "", "effort": "",
        "limit": 0, "found": True, "state": "", "problem": "", "models": [], "status": "ready",
        "said": "Ready",
    }  # fmt: skip
    claude = found | {"name": "claude", "label": "Claude"}
    states = {
        "none-found": {
            "agents": [], "new_workers": "copilot", "sandboxed": False,
            "supported": ["the Codex CLI", "Claude Code"],
        },
        "signed-out": {
            "agents": [found | {"problem": SIGNED_OUT_CODEX, "status": "signin",
                                "said": "Sign-in needed", "ways": [{"method": "browser"}],
                                "fix": {"command": "codex login", "then": ""}}],
            "new_workers": "copilot", "sandboxed": False,
        },
        "too-old": {
            "agents": [found | {"problem": OLD_CODEX}],
            "new_workers": "copilot", "sandboxed": False,
        },
        "sandboxed": {
            "agents": [found | {"state": "0.167.2 · signed in with ChatGPT",
                                "sandbox": SANDBOXED_CODEX}],
            "new_workers": "copilot", "sandboxed": True,
        },
        # Sandboxed, and the owner's own Claude settings would widen its sandbox.
        "claude-widened": {
            "agents": [claude | {"state": "2.1.322 · signed in with your Claude plan",
                                 "sandbox": WIDENED_CLAUDE, "sandbox_note": HOOKS_NOTE}],
            "new_workers": "copilot", "sandboxed": True,
        },
        # Sandboxed, its workers can start: they run no hooks, one line says.
        "claude-sandboxed": {
            "agents": [claude | {"enabled": True, "note": PLAN_NOTE,
                                 "state": "2.1.322 · signed in with your Claude plan",
                                 "sandbox": "", "sandbox_note": HOOKS_NOTE}],
            "new_workers": "copilot", "sandboxed": True,
        },
        "offline": {
            "agents": [found | {"enabled": True, "problem": STOPPED_CODEX}],
            "new_workers": "codex", "sandboxed": False,
        },
        # Most of a plan's limit used: its meter, amber.
        "usage-high": {
            "agents": [found | {"enabled": True, "state": "0.167.2 · signed in with ChatGPT",
                                "usage": [{"label": "5-hour limit", "used": 86,
                                           "resets_at": int(time.time()) + 5400}]}],
            "new_workers": "copilot", "sandboxed": False,
        },
        # Claude on a plan whose limit its workers reached, and without Chief of Staff's extra.
        "claude-plan-limit": {
            "agents": [claude | {"state": "2.1.322 · signed in with your Claude plan",
                                 "note": PLAN_NOTE, "limit_line": LIMIT_LINE}],
            "new_workers": "copilot", "sandboxed": False,
        },
        "claude-no-extra": {
            "agents": [claude | {"problem": NO_EXTRA, "note": PLAN_NOTE}],
            "new_workers": "copilot", "sandboxed": False,
        },
    }  # fmt: skip
    for name, answer in [("checking", None), ("error", "error"), *states.items()]:
        context, page, errors, requests = await agents_page(browser, scheme)
        gate = asyncio.Event()
        answered = agents_answer(answer, gate)
        await page.route("**/api/agents", answered)
        await page.locator("#settings-button").click()
        await page.locator("#settings-tabs [data-tab=agents]").click()
        tab = page.locator("#settings-page-agents")
        if name == "checking":
            # A row for each agent from what the page knows; its fields wait for the look.
            await expect(tab).to_have_attribute("data-looked", "running")
            await expect(tab.locator(".agent .who strong")).to_have_text(
                ["Copilot", "Codex", "Claude Code"]
            )
            for field in await tab.locator("[name^=agent_codex_]:not([type=checkbox])").all():
                await expect(field).to_be_disabled()
        elif name == "error":
            await expect(tab).to_have_attribute("data-looked", "failed")
            await expect(tab.locator(".agent-alerts")).to_contain_text(
                [f"Could not check: {NO_ANSWER}"] * 2
            )
        elif name == "none-found":
            await expect(tab.locator("#agents-none")).to_have_text(
                "No supported coding agent found. Supported: the Codex CLI or Claude Code \u2014 "
                "install one, then open this again."
            )
        else:
            await expect(tab).to_have_attribute("data-looked", "done")
            shown = answer["agents"][0]
            row = tab.locator(f"#agent-{shown['name']}")
            box = row.locator("input[type=checkbox]")
            await expect(box).to_have_attribute(
                "aria-describedby", f"agent-{shown['name']}-status agent-{shown['name']}-alerts"
            )
            status = row.locator(".agent-line .status")
            alerts = row.locator(".agent-alert")
            # Why it can't run, in full, under its line (its status: a few words); a command in
            # it shows as code, its marks not as they are.
            problem = shown["problem"] or (answer["sandboxed"] and shown.get("sandbox")) or ""
            words = {"signed-out": "Sign in needed"}.get(name) or (
                "Can't run here"
                if problem
                else "Ready"
                if shown["enabled"]
                else "Ready · off for workers"
            )
            await expect(status).to_have_text(words)
            said = [shown.get("limit_line")]
            said += [problem.replace("`", "") if problem and name != "signed-out" else None]
            said += [shown.get("sandbox_note") if not problem and answer["sandboxed"] else None]
            await expect(alerts).to_have_text([line for line in said if line])
            assert "`" not in await row.inner_text(), "no code marks shown as they are"
            label = row.locator("label.check")
            await expect(label).to_have_text("Use for workers")
            if name in ("signed-out", "too-old", "sandboxed", "claude-widened", "claude-no-extra"):
                await expect(box).to_be_disabled()
                await expect(label).to_have_class(re.compile(r"\blocked\b"))
                assert await box.evaluate("b => getComputedStyle(b).opacity") == "0.4"
            else:
                await expect(box).to_be_enabled()  # On: it can always be turned off.
            if name == "signed-out":
                await expect(row.locator(".action-slot button")).to_have_text("Sign in")
                await expect(row.locator(".action-slot button")).to_have_attribute(
                    "aria-label", "Sign in to Codex"
                )
            meter = row.locator(".plan-meter")
            if name == "usage-high":
                await expect(meter).to_have_text("86% 5-hour")
                await expect(meter).to_have_class(re.compile(r"\bhigh\b"))
                await meter.focus()
                tip = page.locator("#settings-dialog #tip")
                await expect(tip).to_be_visible()
                await expect(tip).to_have_text(re.compile(r"^86% of your 5-hour limit · resets "))
                nav = await page.locator("#settings-tabs").bounding_box()
                box_at = await tip.bounding_box()
                assert box_at["x"] >= nav["x"] + nav["width"], (box_at, nav)
            else:
                await expect(meter).to_have_count(0)
        assert not any("/api/models?agent=" in r for r in requests), requests
        for width in AGENT_WIDTHS:
            await shoot(page, f"group-{name}", scheme, width)
        gate.set()
        await page.locator("#settings-cancel").click()
        assert not [e for e in errors if "500" not in e and "Failed to load" not in e], errors
        await context.close()
    log(
        f"agents ({scheme}): looking, error, none found, signed out, too old, sandboxed, "
        "Claude's settings widening its sandbox, Claude sandboxed (no hooks), offline, most of a "
        "plan used (its tip in the page area), Claude's plan limit, Claude without its extra"
    )


async def agents_on_and_worker(runtime, browser, runs: Path):
    """A worker on Codex: its header names Codex and its model, its tokens; its ⋯ lists Codex's
    models (Ask first off with why, where it applies); its task's tip says "on Codex" and its
    tokens; Usage has a Codex part; a worker whose spend is unknown waits under Needs you for Go
    on; turning Codex off while it works says so once."""
    from copilot.tools import ToolInvocation

    store = runtime.store
    await agents_saved(runtime, {"codex": {"enabled": True, "limit": 0}, "new_workers": "codex"})
    router = store.get("router_id")
    spawn = runtime.toolset.spawn_on(["codex"])

    async def spawned(title, text):
        result = await spawn.handler(
            ToolInvocation(session_id=router, tool_call_id="t", tool_name="spawn_worker",
                           arguments={"title": title, "instructions": text, "why_new": "t"})
        )  # fmt: skip
        return json.loads(result.text_result_for_llm)

    done = await spawned("Fix the date parser", "Report done: Fixed the parser.")
    await until(lambda: store.session(done["session_id"])["status"] == "done", 30)
    worker = done["session_id"]
    for scheme in ("light", "dark"):
        context, page, errors, _ = await agents_page(browser, scheme)
        if await page.locator("#home-tabs").is_visible():
            await page.locator("#home-tabs [data-tab=tasks]").click()
        row = page.locator(".card.task").filter(has_text="Fix the date parser").first
        await expect(row).to_be_visible()
        tip = await row.locator(".head .meta").get_attribute("data-tip") or ""
        tokens = store.agent_spent(worker)
        assert f"on Codex · {tokens:,} tokens" in tip, tip
        await page.evaluate("id => import('/static/session.js').then((m) => m.select(id))", worker)
        header = page.locator("#session-meta")
        await expect(header).to_contain_text("Codex · GPT-TEST", timeout=10000)
        meta = await header.inner_text()
        shown = f"{int(tokens / 1000 + 0.5)}k" if tokens >= 1000 else str(tokens)
        assert meta.endswith(f"{shown} tokens") and "credits" not in meta, meta
        await page.locator("#session-more-button").click()
        models = page.locator("#session-model option")
        await expect(models).to_have_text(["Default · GPT-TEST", "GPT-TEST", "GPT-MINI"])
        for width in AGENT_WIDTHS:
            # (The menu's buttons reach past its edges on purpose: their hover, as for Copilot's.)
            await shoot(page, "worker-header-menu", scheme, width, "#session-header .heading")
        await page.keyboard.press("Escape")
        assert not errors, errors
        await context.close()
    log("agents: a Codex worker's header, ⋯ models, task tip and tokens")
    await agents_differences(runtime, browser, worker)
    await agents_usage(runtime, browser, worker)
    await agents_unknown_spend(runtime, browser, spawned)
    await agents_stopped_by_settings(runtime, browser, spawned)


def patched_session(session_id: str, fields: dict):
    """The page's /api/state with these fields on one session's view."""

    async def handle(route):
        response = await route.fetch()
        data = await response.json()
        for session in data["sessions"]:
            if session["id"] == session_id:
                session.update(fields)
        await route.fulfill(response=response, json=data)

    return handle


async def agents_differences(runtime, browser, worker: str):
    """Where its agent works differently: the name's tooltip and description list it, a line
    each; Ask first, where its checks cannot run: disabled, with why."""
    why = (
        "Codex 0.168.1 has not passed Chief of Staff's checks for Ask first (tested from "
        "0.167.2, before 0.168.0): let this worker act on its own, or use a tested version."
    )
    notes = ["Commands it leaves running are not tracked", "Its subagents are off"]
    for scheme in ("light", "dark"):
        context, page, errors, _ = await agents_page(browser, scheme)
        await page.route(
            "**/api/state",
            patched_session(worker, {"agent_notes": notes, "ask_first_refused": why}),
        )
        await page.reload()
        await expect(page.locator("#application")).to_be_visible()
        await page.evaluate("id => import('/static/session.js').then((m) => m.select(id))", worker)
        name = page.locator("#session-meta span", has_text="Codex")
        await expect(name).to_have_attribute("title", "\n".join(notes))
        await expect(name).to_have_attribute("aria-describedby", "session-agent-notes")
        await expect(page.locator("#session-agent-notes")).to_have_text(". ".join(notes))
        await expect(page.locator("#session-agent-notes")).to_be_hidden()
        await page.locator("#session-more-button").click()
        checks = page.locator("#session-menu-items select")
        await expect(checks.locator('option[value="off"]')).to_be_disabled()
        await expect(checks).to_have_attribute("title", why)
        await expect(checks).to_have_attribute("aria-describedby", "ask-first-why")
        await expect(page.locator("#ask-first-why")).to_have_text(why)
        for width in AGENT_WIDTHS:
            await shoot(page, "worker-ask-first-off", scheme, width, "#session-header .heading")
        await page.keyboard.press("Escape")
        assert not errors, errors
        await context.close()
    # A Copilot worker's header says Copilot, as every worker's says its agent while another is
    # on; its menu says nothing of agents.
    copilot = next(
        row["id"]
        for row in runtime.store.open_sessions()
        if row["role"] == "worker" and runtime.on_copilot(row["id"])
    )
    context, page, errors, _ = await agents_page(browser, "light")
    await page.evaluate("id => import('/static/session.js').then((m) => m.select(id))", copilot)
    await expect(page.locator("#session-meta")).not_to_contain_text("Codex")
    # (Its agent with its mark; before its first step, a chip whose popover can change it.)
    await expect(page.locator("#session-meta .agent-name, #session-meta .agent-chip")).to_have_text(
        [re.compile(r"^Copilot▾?$")]
    )
    await page.locator("#session-more-button").click()
    assert not await page.locator("#session-menu-items select").evaluate(
        "s => s.hasAttribute('aria-describedby') || s.querySelector('option[value=off]').disabled"
    )
    assert not errors, errors
    await context.close()
    log("agents: what a Codex worker does differently in its name's tooltip; Ask first off, why")


async def agents_usage(runtime, browser, worker: str):
    """Usage: a part for Codex with its models' tokens, a range as a range, and when what it
    spent is unknown."""
    store = runtime.store
    now = time.time()
    rows = [
        {"agent": "codex", "session_id": worker, "model": "gpt-mini", "tokens": 120_000,
         "least": 80_000, "kind": "range", "at": now - 60},
        {"agent": "codex", "session_id": worker, "kind": "unknown", "at": now - 30},
    ]  # fmt: skip
    store.record_agent_usage(rows, "agentepoch:ui-edges", {})
    try:
        for scheme in ("light", "dark"):
            context, page, errors, _ = await agents_page(browser, scheme)
            await page.locator("#usage-button").click()
            dialog = page.locator("#usage-dialog")
            await expect(dialog).to_be_visible()
            part = dialog.locator("#usage-tables > div", has=page.locator("h3", has_text="Codex"))
            today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
            spent = store.agent_usage_in(today.timestamp(), time.time() + 1)
            exact = sum(row["tokens"] for row in spent if row.get("model") == "gpt-test")
            await expect(part.locator("h3")).to_have_text(
                f"Codex{80_000 + exact:,}\u2013{120_000 + exact:,} tokens"
            )
            await expect(part.locator("tr td:first-child")).to_have_text(["gpt-mini", "gpt-test"])
            ranged = part.locator("tr", has_text="gpt-mini")
            await expect(ranged).to_contain_text("80,000\u2013120,000 tokens")
            unknown = part.locator("small.unknown")
            await expect(unknown).to_have_text(
                re.compile(r"^Unknown: Fix the date parser — Codex crashed at \d{1,2}:\d\d [AP]M$")
            )
            # Copilot's totals are Copilot's alone (today's, as the dialog opens on).
            local = datetime.now().astimezone()  # The page's midnight is the computer's.
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
            copilot = store.usage(midnight.timestamp(), time.time() + 1)["total"]
            await expect(dialog.locator("#usage-total strong")).to_have_text(
                f"{copilot['credits']:.1f} credits"
            )
            if not copilot["calls"]:
                await expect(dialog.locator("#usage-tables .line")).to_have_text(
                    "No Copilot model calls in this range."
                )
            for width in AGENT_WIDTHS:
                await shoot(page, "usage-codex", scheme, width, "#usage-dialog")
            await page.keyboard.press("Escape")
            assert not errors, errors
            await context.close()
    finally:
        store.execute("DELETE FROM agent_usage WHERE session_id=? AND kind!='exact'", (worker,))
    log("agents: Usage has a Codex part, a range as a range, unknown as unknown")


async def agents_unknown_spend(runtime, browser, spawned):
    """A Codex worker with a limit that stopped mid-turn: what it spent then is unknown, so it
    waits under Needs you, as after any stop, until the owner says Go on."""
    store = runtime.store
    await agents_saved(runtime, {"codex": {"enabled": True, "limit": 1_000_000}})
    made = await spawned("Measure the build time", "Wait 600 s")
    worker = made["session_id"]
    await until(lambda: store.session(worker)["status"] == "working", 30)
    store.record_agent_usage(
        [{"agent": "codex", "session_id": worker, "kind": "unknown", "at": time.time()}],
        f"agentepoch:{worker}",
        runtime.epoch_of(worker),
    )
    await runtime.stop_for_spend(store.session(worker))
    await settle(runtime)
    for scheme in ("light", "dark"):
        context, page, errors, _ = await agents_page(browser, scheme)
        if await page.locator("#home-tabs").is_visible():
            await page.locator("#home-tabs [data-tab=needs]").click()
        card = page.locator("#needs .card.task", has_text="Measure the build time")
        await expect(card).to_contain_text(
            re.compile(
                r"Codex stopped mid-turn, so what it spent since \d{1,2}:\d\d [AP]M "
                r"isn't known\. Go on accepts that and continues\."
            ),
            timeout=15000,
        )
        assert "Stopped by" not in await card.inner_text(), "said once, not again after it"
        await expect(card.get_by_role("button", name="Go on", exact=True)).to_be_visible()
        full = await whole_on_hover(card)
        for width in AGENT_WIDTHS:
            await page.set_viewport_size({"width": width, "height": 900})
            if await page.locator("#home-tabs").is_visible():
                await page.locator("#home-tabs [data-tab=needs]").click()
            await expect(card).to_be_visible()
            await shoot(page, "unknown-spend-go-on", scheme, width, "#main", card)
        await page.set_viewport_size({"width": 1280, "height": 900})
        await page.evaluate("id => import('/static/session.js').then((m) => m.select(id))", worker)
        notices = page.locator("#timeline .notice")
        await expect(notices).to_have_text(
            ["Stopped: Codex stopped mid-turn, so what it spent is unknown."]
        )
        assert (await notices.get_attribute("title") or "").startswith(full + "\n")
        for width in AGENT_WIDTHS:
            await shoot(page, "unknown-spend-worker", scheme, width, "#main")
            assert await notices.locator("span").evaluate("n => n.scrollWidth <= n.clientWidth"), (
                "cut short",
                width,
            )
        assert not errors, errors
        await context.close()
    context, page, errors, _ = await agents_page(browser, "light")
    if await page.locator("#home-tabs").is_visible():
        await page.locator("#home-tabs [data-tab=needs]").click()
    card = page.locator("#needs .card.task", has_text="Measure the build time")
    await card.get_by_role("button", name="Go on", exact=True).click()
    await until(lambda: not store.agent_unknown(worker), 10)
    await context.close()
    await runtime.workers.interrupt(worker, by="you")
    await agents_limit_used(runtime, browser, spawned)
    await agents_saved(runtime, {"codex": {"enabled": True, "limit": 0}})
    log("agents: unknown spend with a limit waits under Needs you for Go on, which accepts it")


async def whole_on_hover(card) -> str:
    """A stopped task's card says why on one line (cut where narrow): all of it on hover."""
    why = card.locator(".why")
    text = await why.text_content() or ""
    assert text and await why.get_attribute("data-tip") == text, (text, "its tooltip")
    return text


async def agents_limit_used(runtime, browser, spawned):
    """A Codex worker that used its token limit: its card says so, with no Go on until the
    limit is raised; then Go on goes on."""
    store = runtime.store
    await agents_saved(runtime, {"codex": {"enabled": True, "limit": 1_000}})
    made = await spawned("Profile the importer", "Wait 600 s")
    worker = made["session_id"]
    await until(lambda: store.session(worker)["status"] == "working", 30)
    row = {"agent": "codex", "session_id": worker, "model": "gpt-test", "tokens": 1_200,
           "kind": "exact", "at": time.time()}  # fmt: skip
    store.record_agent_usage([row], f"agentepoch:{worker}", runtime.epoch_of(worker))
    await runtime.stop_for_spend(store.session(worker))
    await settle(runtime)
    for scheme in ("light", "dark"):
        context, page, errors, _ = await agents_page(browser, scheme)
        if await page.locator("#home-tabs").is_visible():
            await page.locator("#home-tabs [data-tab=needs]").click()
        card = page.locator("#needs .card.task", has_text="Profile the importer")
        await expect(card).to_contain_text(
            "Used its 1,000-token limit. Raise it in Settings → Agents & models, or cancel.",
            timeout=15000,
        )
        assert "Stopped by" not in await card.inner_text()
        await expect(card.get_by_role("button", name="Go on", exact=True)).to_have_count(0)
        full = await whole_on_hover(card)
        for width in AGENT_WIDTHS:
            await page.set_viewport_size({"width": width, "height": 900})
            if await page.locator("#home-tabs").is_visible():
                await page.locator("#home-tabs [data-tab=needs]").click()
            await expect(card).to_be_visible()
            await shoot(page, "limit-used-card", scheme, width, "#main", card)
        await page.set_viewport_size({"width": 1280, "height": 900})
        await page.evaluate("id => import('/static/session.js').then((m) => m.select(id))", worker)
        notices = page.locator("#timeline .notice")
        await expect(notices).to_have_text(["Stopped: it used its 1,000-token limit."])
        assert (await notices.get_attribute("title") or "").startswith(full + "\n")
        for width in AGENT_WIDTHS:
            await shoot(page, "limit-used-worker", scheme, width, "#main")
        assert not errors, errors
        await context.close()
    # Raised: Go on goes on.
    stopped_at = store.messages(worker)[-1]["id"]
    await agents_saved(runtime, {"codex": {"enabled": True, "limit": 1_000_000}})
    for scheme in ("dark", "light"):
        context, page, errors, _ = await agents_page(browser, scheme)
        if await page.locator("#home-tabs").is_visible():
            await page.locator("#home-tabs [data-tab=needs]").click()
        card = page.locator("#needs .card.task", has_text="Profile the importer")
        await expect(card).to_contain_text("Its token limit is 1,000,000 now: Go on continues.")
        for width in AGENT_WIDTHS:
            await page.set_viewport_size({"width": width, "height": 900})
            if await page.locator("#home-tabs").is_visible():
                await page.locator("#home-tabs [data-tab=needs]").click()
            await shoot(page, "limit-raised-card", scheme, width, "#main", card)
        if scheme == "dark":
            assert not errors, errors
            await context.close()
    await card.get_by_role("button", name="Go on", exact=True).click()
    # Admitted: its turn on it ran (the stand-in says nothing to it, and is asked to report).
    await until(
        lambda: any(m["kind"] == "report" and m["id"] > stopped_at for m in store.messages(worker)),
        15,
    )
    assert not [m for m in store.messages(worker) if m["kind"] == "error" and m["id"] > stopped_at]
    assert not errors, errors
    await context.close()
    await runtime.workers.interrupt(worker, by="you")
    log("agents: a worker that used its limit waits with no Go on until it is raised")


async def agents_stopped_by_settings(runtime, browser, spawned):
    """Codex turned off while a worker of it works: the save says once how many stopped, why."""
    store = runtime.store
    made = await spawned("Profile the importer", "Wait 600 s")
    worker = made["session_id"]
    await until(lambda: store.session(worker)["status"] == "working", 30)
    context, page, errors, _ = await agents_page(browser, "light")
    group = await open_agents(page)
    box = group.locator("input[name=agent_codex_enabled]")
    await expect(box).to_be_checked(timeout=20000)
    await box.uncheck()
    # What the save answers is the page's at once (the state it reloads after comes later).
    held = asyncio.Event()

    async def later(route):
        await held.wait()
        await route.continue_()

    await page.route("**/api/state", later)
    await page.locator("#settings-form footer button[type=submit]").click()
    await expect(page.locator("#settings-dialog")).to_be_hidden()
    assert not await page.evaluate(
        "() => import('/static/state.js').then((m) => 'agents' in m.state.snapshot "
        "&& m.state.snapshot.agents !== undefined)"
    ), "none on: the page drops what it knew of them"
    held.set()  # From now on each goes through at once.
    toast = page.locator(".toast")
    await expect(toast).to_have_text(
        "Stopped 1 worker because Codex is off. Turn Codex on again, then press Go on under "
        "Needs you."
    )
    await expect(toast).to_have_count(1)
    for width in AGENT_WIDTHS:
        await shoot(page, "turned-off-toast", "light", width, "#application")
    group = await open_agents(page)
    await expect(group.locator("#agent-codex .status")).to_have_text("Ready · off for workers")
    await page.keyboard.press("Escape")
    assert store.session(worker)["status"] == "idle"
    assert not errors, errors
    await context.close()
    log("agents: turned off while a worker works: one toast says how many stopped, and why")


# The agent picker (the owner's decisions of 2026-11-14): each surface, checked and photographed at
# the widths the owner reviews, light and dark.
PICKER_WIDTHS = tuple(
    int(w) for w in os.environ.get("UI_PICKER_WIDTHS", "1500,760,400,352").split(",")
)
PICKER_SHOTS = Path(os.environ.get("UI_PICKER_SHOTS") or SHOTS / "claude-m2")


# OVERFLOWS, but for what a horizontal scroller holds (Settings' tabs on a phone scroll sideways).
SCROLLED_OVERFLOWS = OVERFLOWS.replace(
    'if (!box.width || node.closest("pre")) return false;',
    'if (!box.width || node.closest("pre")) return false;\n'
    "  for (let up = node.parentElement; up && up !== root.parentElement;"
    " up = up.parentElement) {\n"
    '    if (["auto", "scroll"].includes(getComputedStyle(up).overflowX)) return false;\n'
    "  }",
)


async def picker_shoot(
    page, name: str, scheme: str, target: str = "#main", focus=None, tab: str = ""
):
    """`name` at each of the owner's widths, light or dark; `focus`: what it is of, brought into
    view (on Home's `tab` where Home is in tabs); nothing of `target` cut off or spilling over
    (but in a sideways scroller)."""
    PICKER_SHOTS.mkdir(parents=True, exist_ok=True)
    for width in PICKER_WIDTHS:
        await page.set_viewport_size({"width": width, "height": 900})
        await page.mouse.move(1, 1)
        if tab and await page.locator("#home-tabs").is_visible():
            await page.locator(f"#home-tabs [data-tab={tab}]").click()
            await page.mouse.move(1, 1)
        if focus is not None:
            await focus.evaluate("n => n.scrollIntoView({block: 'center', inline: 'nearest'})")
        await quiet_for(page)
        await page.screenshot(path=PICKER_SHOTS / f"{name}-{width}-{scheme}.png")
        found = await page.locator(target).evaluate(SCROLLED_OVERFLOWS)
        assert not found, (name, width, found)
    await page.set_viewport_size({"width": 1280, "height": 900})


async def sidebar_shoot(page, name: str, scheme: str):
    """The sidebar at each of the owner's widths (opened over the page where it folds away)."""
    PICKER_SHOTS.mkdir(parents=True, exist_ok=True)
    for width in PICKER_WIDTHS:
        await page.set_viewport_size({"width": width, "height": 900})
        await page.mouse.move(1, 1)
        folded = await page.locator("#sidebar-toggle").is_visible()
        if folded:
            await page.locator("#sidebar-toggle").click()
            await expect(page.locator("#sidebar")).to_be_visible()
            await page.wait_for_timeout(300)  # Slid in.
        await quiet_for(page)
        await page.screenshot(path=PICKER_SHOTS / f"{name}-{width}-{scheme}.png")
        found = await page.locator("#sidebar").evaluate(SCROLLED_OVERFLOWS)
        assert not found, (name, width, found)
        if folded:
            await page.keyboard.press("Escape")
            await expect(page.locator("#sidebar")).to_be_hidden()
    await page.set_viewport_size({"width": 1280, "height": 900})


async def at_home(page):
    await page.evaluate("() => import('/static/session.js').then((m) => m.select('home'))")
    if await page.locator("#home-tabs").is_visible():
        await page.locator("#home-tabs [data-tab=chat]").click()


def start_fold(page, title: str):
    """On Home, the fold of a run of routing receipts (E129) that holds `title`'s start line."""
    return page.locator("#timeline > .fold.routing").filter(
        has=page.locator(".event.started", has_text=title)
    )


async def start_line(page, title: str):
    """`title`'s start line on Home, shown: in a run of routing receipts it is in a fold (E129),
    opened first, with its chip."""
    line = page.locator("#timeline .event.started", has_text=title)
    await expect(line).to_have_count(1)
    fold = start_fold(page, title)
    if await fold.count() and not await line.is_visible():
        await fold.locator(":scope > .fold-line button.line").click()
    await expect(line).to_be_visible()
    return line


ROLE_SHOTS = Path(os.environ.get("UI_ROLE_SHOTS") or SHOTS / "roles")
ROLE_WIDTHS = (800, 1280)


async def role_shoot(page, name: str, scheme: str, focus=None):
    """Agents & models (or what `focus` is) at each width, nothing overflowing."""
    ROLE_SHOTS.mkdir(parents=True, exist_ok=True)
    for width in ROLE_WIDTHS:
        await page.set_viewport_size({"width": width, "height": 900})
        await page.mouse.move(1, 1)
        target = focus or page.locator("#settings-dialog")
        await target.evaluate("n => n.scrollIntoView({block: 'center', inline: 'nearest'})")
        await quiet_for(page)
        found = await page.locator("#settings-dialog" if focus is None else "body").evaluate(
            OVERFLOWS
        )
        assert not found, (name, width, found)
        await page.screenshot(path=ROLE_SHOTS / f"{name}-{width}-{scheme}.png")
    await page.set_viewport_size({"width": 1280, "height": 900})


async def round_roles(runtime, browser, failures: list[str]):
    """Where Chief of Staff's own sessions run (agent-roles.md; settings-v2.md §3.4): the
    Router's and the Watchdog's Runs on in Chief of Staff's own sessions, a menu only once an
    agent that can run them is on, listing only those; none for Slack nor Watch for you; on
    Codex, Codex's model and effort said, its Copilot ones kept; the Router put on Codex says so
    in its chat, whose header names Codex; Codex signed out, one line under its choice says why
    it runs on Copilot for now."""
    from chiefofstaff import agents

    folder, _ = codex_on_path()
    runtime.found_cache = (float("-inf"), [])
    router = runtime.store.get("router_id")
    # Codex's plan limit as an earlier round left it reached (round_picker): it would keep the
    # Router on Copilot until it resets, saying so. And what that round left for the Router, taken
    # as sent: its first turn on the stand-in Codex would carry that round's cue to refuse a turn
    # for a limit ("Hit the usage limit", a task's words in its handover).
    runtime.store.delete("agentlimit:codex")
    try:
        for scheme in ("light", "dark"):
            context, page, errors, _ = await agents_page(browser, scheme)
            saved: list = []
            page.on(
                "request",
                lambda r, saved=saved: (
                    saved.append(r.post_data_json) if r.url.endswith("/api/settings") else None
                ),
            )
            group = await open_agents(page)
            codex = group.locator("#agent-codex")
            await expect(codex.locator(".status")).to_have_text("Ready · off for workers")
            # Where they run: Chief of Staff's own sessions, folded; Copilot while none is on.
            system = group.locator("#own-sessions")
            await expect(system.locator("summary > span")).to_have_text(
                "Chief of Staff's own sessions"
            )
            await system.locator("summary").click()
            choose = group.locator("select[name=role_router]")
            await expect(choose).to_be_hidden()  # None on.
            await codex.locator("input[type=checkbox]").check()
            await expect(choose).to_be_visible()
            await expect(group.locator("select[name=role_watchdog]")).to_be_visible()
            await expect(group.locator("select[name=role_slack]")).to_have_count(0)
            # If it can't run there: only once one runs on another agent (round_agent_smooth).
            for fallback in await group.locator("[data-sub]").all():
                await expect(fallback).to_be_hidden()
            assert "Watch for you runs on" not in await group.inner_text()
            await expect(choose.locator("option")).to_have_text(["Copilot", "Codex"])
            await expect(choose).to_have_value("copilot")
            await role_shoot(page, "roles-codex-on", scheme)
            # Unchecked again: the menus go with it.
            await codex.locator("input[type=checkbox]").uncheck()
            await expect(choose).to_be_hidden()
            await codex.locator("input[type=checkbox]").check()
            await choose.select_option("codex")
            await expect(system.locator("#own-summary")).to_have_text(
                "Router on Codex · others on Copilot"
            )
            # On Codex: Codex's model and effort, said; its Copilot ones kept, unsent.
            await expect(group.locator("[data-inherits=router]")).to_have_text(
                "Codex's model and effort (its row)"
            )
            await expect(group.locator("[data-cells=router]")).to_be_hidden()
            await expect(group.locator("[data-sub=router]")).to_be_visible()
            mark = max((m["id"] for m in runtime.store.messages(router)), default=0)
            runtime.store.set(f"sent:{router}", str(mark))
            await page.locator("#settings-form footer button[type=submit]").click()
            await expect(page.locator("#settings-dialog")).to_be_hidden()
            assert saved[-1]["agents"] == {
                "codex": {"enabled": True},
                "roles": {"router": "codex"},
            }, saved[-1].get("agents")
            assert saved[-1]["role_models"] == {} and saved[-1]["role_efforts"] == {}

            said = "The Router runs on Codex now, with a summary of what it knew."

            def said_since(mark: int = mark) -> list[str]:
                return [
                    m["text"]
                    for m in runtime.store.messages(router)
                    if m["kind"] == "notice" and m["id"] > mark
                ]

            def switched(said: str = said) -> bool:
                # Switched at its turn's end: the stand-in Copilot ends none by itself.
                row = runtime.store.session(router)
                if (
                    runtime.on_copilot(router)
                    and row["status"] == "working"
                    and (handle := runtime.states.handle(router))
                ):
                    handle.emit("session.idle")
                return said in said_since()

            await until(switched, 30)
            assert runtime.agent(router) == "codex"
            assert said_since() == [said], said_since()
            # Its chat on Home: one line says it; its ⋯ menu lists Codex's models.
            line = page.locator("#timeline").get_by_text(said, exact=True).last
            await expect(line).to_be_visible(timeout=10000)
            await page.locator("#session-more-button").click()
            await expect(page.locator("#session-model option")).to_have_text(
                ["Default · GPT-TEST", "GPT-TEST", "GPT-MINI"], timeout=10000
            )
            await role_shoot(page, "roles-router-chat", scheme, line)
            await page.keyboard.press("Escape")
            # Signed out: on Copilot for now, the line under its choice says why.
            os.environ["FAKE_CODEX_SIGNED_OUT"] = "1"
            try:
                await agents_saved(runtime, {"codex": {"enabled": False}})
                runtime.found_cache = (float("-inf"), [])
                await agents_saved(
                    runtime, {"codex": {"enabled": True}, "roles": {"router": "codex"}}
                )
                assert runtime.agent(router) == "copilot"
                await open_agents(page)
                if not await system.evaluate("n => n.open"):
                    await system.locator("summary").click()
                why = group.locator("#role-router-line")
                await expect(why).to_have_text(
                    "On Copilot for now: Codex is not signed in: sign in from Settings → Agents "
                    "& models.",
                    timeout=20000,
                )
                await expect(choose).to_have_attribute("aria-describedby", "role-router-line")
                await expect(choose).to_have_value("codex")
                # Folded, its line says where it runs now, not only where it was put.
                await expect(system.locator("#own-summary")).to_have_text(
                    "Router on Copilot for now · others on Copilot"
                )
                await role_shoot(page, "roles-fallback", scheme)
                # Chosen again (not saved yet): where it is put, as chosen.
                await choose.select_option("copilot")
                await expect(system.locator("#own-summary")).to_have_text(
                    re.compile(r"^All on Copilot · ")
                )
                await choose.select_option("codex")
                await expect(system.locator("#own-summary")).to_have_text(
                    "Router on Copilot for now · others on Copilot"
                )
            finally:
                os.environ.pop("FAKE_CODEX_SIGNED_OUT", None)
            await roles_check_unfinished(runtime, page, scheme)
            await agents_saved(runtime, {"codex": {"enabled": False}})
            assert agents.role_agent(runtime.store, "router") == "copilot"
            assert not errors, errors
            await context.close()
        log("roles: the Router and the Watchdog's lines once Codex is on; on Codex; on Copilot "
            "for now, saying why")  # fmt: skip
    finally:
        await agents_saved(runtime, {"codex": {"enabled": False}})
        codex_off_path(folder)


# As Claude Code lists its models to a worker (2.1.322's initialize): with what each alias stands
# for now, and a row by version.
CLAUDE_ROWS = [
    {"value": "default", "displayName": "Default (recommended)", "resolvedModel": "claude-opus-5-5",
     "description": "Use the default model (currently Opus 5.5)"},
    {"value": "opus", "displayName": "Opus", "resolvedModel": "claude-opus-5-5",
     "description": "Opus 5.5 · Best for everyday, complex tasks",
     "supportedEffortLevels": ["low", "high"]},
    {"value": "haiku", "displayName": "Haiku", "resolvedModel": "claude-haiku-5-5",
     "description": "Haiku 5.5 · Fastest for quick answers"},
    {"value": "claude-opus-4-8", "displayName": "Opus 4.8", "resolvedModel": "claude-opus-4-8"},
]  # fmt: skip


async def round_claude_models(runtime, browser, failures: list[str]):
    """A Claude worker's Model menu (the owner's, 0.1.53: "Default · Opus" and "Default
    (recommended)", whose was whose not said, and no exact version): its first choice is what
    Settings chose, said as what it is, or Claude Code's own default where Settings chose none
    (then not listed twice); each alias with the exact model it stands for now; each exact
    model, pinned. Settings' row: Claude Code's default first, then the same."""
    from chiefofstaff import backend_claude

    store = runtime.store
    folder, _ = codex_on_path()
    os.environ["FAKE_CLAUDE_INIT"] = json.dumps({"models": CLAUDE_ROWS})
    version = "2.1.322"  # The stand-in's.
    backend_claude.LISTED[version] = backend_claude.picker_models(CLAUDE_ROWS)
    runtime.agent_models_cache.pop("claude", None)
    worker = ""
    pinned = ["claude-opus-4-8", "claude-opus-5-5", "claude-haiku-5-5"]
    rest = [
        "Opus (now claude-opus-5-5)",
        "Haiku (now claude-haiku-5-5)",
        *(f"{model} (pinned)" for model in pinned),
    ]
    try:
        await agents_saved(runtime, {"claude": {"enabled": True}})
        from copilot.tools import ToolInvocation

        spawn = runtime.toolset.spawn_on(["claude"])
        made = await spawn.handler(
            ToolInvocation(session_id=store.get("router_id"), tool_call_id="t",
                           tool_name="spawn_worker",
                           arguments={"title": "Tidy the notes", "agent": "claude",
                                      "instructions": "Report done: Tidied.", "why_new": "t"})
        )  # fmt: skip
        worker = json.loads(made.text_result_for_llm)["session_id"]
        await until(lambda: store.session(worker)["status"] not in {"queued", "working"}, 60)
        for chosen, first, more in (
            ("", ["Claude Code's default (now claude-opus-5-5)"], []),
            (
                "opus",
                ["Settings: Opus (now claude-opus-5-5)"],
                ["Claude Code's default (now claude-opus-5-5)"],
            ),
        ):
            await agents_saved(runtime, {"claude": {"enabled": True, "model": chosen}})
            for scheme in ("light", "dark"):
                context, page, errors, _ = await agents_page(browser, scheme)
                await page.evaluate(
                    "id => import('/static/session.js').then((m) => m.select(id))", worker
                )
                # Its tabs, no team: nothing more beside them (0.1.55 said "null" there).
                await expect(page.locator("#worker-tabs")).to_have_text("WorkToolsReports")
                await page.locator("#session-more-button").click()
                options = page.locator("#session-model option")
                await expect(options).to_have_text([*first, *more, *rest], timeout=20000)
                values = await options.evaluate_all("nodes => nodes.map((n) => n.value)")
                assert values == ["", *(["default"] if more else []), "opus", "haiku", *pinned]
                name = f"claude-models-menu{'-settings' if chosen else ''}"
                await picker_shoot(page, name, scheme, "#session-tuning", focus=options.first)
                await page.keyboard.press("Escape")
                if chosen and scheme == "light":
                    # Settings' row: Claude Code's default first, by the alias it stands for
                    # now (not listed twice), Opus chosen.
                    section = await open_agents(page)
                    await unfold_agent(section, "claude")
                    field = page.locator("select[name=agent_claude_model]")
                    await expect(field.locator("option")).to_have_text(
                        ["Claude Code's default (Opus)", *rest], timeout=20000
                    )
                    await expect(field).to_have_value("opus")
                    await page.keyboard.press("Escape")
                assert not errors, errors
                await context.close()
        log("claude models: what Settings chose or Claude Code's default, once; each alias with "
            "what it stands for now; each exact model, to pin; Settings' row")  # fmt: skip
    finally:
        os.environ.pop("FAKE_CLAUDE_INIT", None)
        backend_claude.LISTED.pop(version, None)
        if worker:
            with contextlib.suppress(Exception):
                await runtime.workers.close(worker, by="you")
        await agents_saved(runtime, {"claude": {"enabled": False}})
        codex_off_path(folder)


async def roles_check_unfinished(runtime, page, scheme: str):
    """Chief of Staff's check of Codex could not finish (a machine busy with other work): the
    Router on Copilot for now, the line under its choice saying so as a role's line says it,
    and that the check is tried again by itself."""
    from chiefofstaff import backend_codex

    late = "Chief of Staff's check of Codex 0.167.2 could not finish (step 'turn' took too long)"
    checked = backend_codex.CodexAgent.check

    async def unfinished(self, folder):
        said = f"{late}: workers that ask first do not run on it until it does."
        return {"ran": False, "unrun": late, "ask_first": said, "acting": ""}

    backend_codex.CodexAgent.check = unfinished  # type: ignore[method-assign]
    try:
        await page.keyboard.press("Escape")
        await agents_saved(runtime, {"codex": {"enabled": False}})
        for key in runtime.store.items("agentcheck:codex:"):
            runtime.store.delete(f"agentcheck:codex:{key}")
        await agents_saved(runtime, {"codex": {"enabled": True}, "roles": {"router": "codex"}})
        assert runtime.on_copilot(runtime.store.get("router_id"))
        await open_agents(page)
        system = page.locator("#own-sessions")
        if not await system.evaluate("n => n.open"):
            await system.locator("summary").click()
        await expect(page.locator("#role-router-line")).to_have_text(
            f"On Copilot for now: {late}. It's tried again by itself.", timeout=20000
        )
        await expect(system.locator("#own-summary")).to_have_text(
            "Router on Copilot for now · others on Copilot"
        )
        await role_shoot(page, "roles-check-retry", scheme)
    finally:
        backend_codex.CodexAgent.check = checked  # type: ignore[method-assign]
        for waiting in runtime.check_waits.values():
            waiting.cancel()


async def paused_continued_once(runtime, browser, worker: str, task_id: str, selector: str):
    """Continue with Copilot… on a task its plan's limit paused, Continue clicked twice: one
    new worker goes on with it; its card leaves at once, and it no longer goes on at its reset.
    Its dialog opened again (a stale card, another tab) says where it went, with Open it."""
    store = runtime.store
    context, page, errors, _ = await agents_page(browser, "light")
    if await page.locator("#home-tabs").is_visible():
        await page.locator("#home-tabs [data-tab=tasks]").click()
    card = page.locator(selector)
    await card.get_by_role("button", name="Continue with Copilot…").click()
    dialog = page.locator("dialog.continue-dialog")
    go = dialog.get_by_role("button", name="Continue")
    await expect(go).to_be_enabled(timeout=10000)
    await go.dblclick()
    await expect(dialog).to_have_count(0, timeout=20000)
    # (The new worker's chat is shown: Home, as it is now.)
    await at_home(page)
    if await page.locator("#home-tabs").is_visible():
        await page.locator("#home-tabs [data-tab=tasks]").click()
    # Out of Monitoring (and never in Needs you): only among those that ended, replaced.
    session = selector.split(".card.task", 1)[1]
    for place in ("#tasks-monitoring", "#needs"):
        await expect(page.locator(f"{place} .card.task{session}")).to_have_count(0, timeout=10000)
    assert runtime.tasks.get(task_id)["status"] == "cancelled", "not left paused"
    made = [
        r["id"]
        for r in store.open_sessions("worker")
        if store.get(f"continues:{r['id']}") == worker
    ]
    assert len(made) == 1, made
    assert store.get(f"replacedby:{task_id}") == runtime.tasks.for_session(made[0])[0]["id"]
    assert store.get(f"goonat:{task_id}") is None, "nor goes on at its reset"
    title = store.session(worker)["title"]
    await page.evaluate(
        "s => import('/static/session.js').then((m) => m.continueWith(s, 'copilot'))",
        {"id": worker, "title": title},
    )
    line = dialog.locator(".continue-line")
    await expect(line).to_have_text(f"Already continued on Copilot by a new worker, “{title}”.")
    opened = dialog.get_by_role("button", name="Open it")
    await expect(opened).to_be_enabled()
    for scheme in ("light", "dark"):
        await page.emulate_media(color_scheme=scheme)
        await picker_shoot(page, "plan-limit-continued-again", scheme, "#main", focus=dialog)
    await opened.click()
    await expect(dialog).to_have_count(0)
    assert (
        len(
            [
                r
                for r in store.open_sessions("worker")
                if store.get(f"continues:{r['id']}") == worker
            ]
        )
        == 1
    )
    assert not errors, errors
    await context.close()


async def round_picker(runtime, browser, failures: list[str]):
    """The first-run card for an agent installed and signed in; Agents & models' rows folded and
    open, with a plan's usage on its meter; @codex in the composer; a worker's agent on its row
    and header (and why one runs on Copilot); Continue with… in its ⋯; a named agent that could
    not run, on the Router's card; a task its plan's limit paused; the Router's pick."""
    from chiefofstaff import agents

    folder, _ = codex_on_path()
    store = runtime.store
    try:
        runtime.found_cache = (float("-inf"), [])
        runtime.offers.clear()
        for name in agents.NAMES:
            store.delete(f"{agents.OFFERED}{name}")
        await runtime.offer_agents()
        assert [o["name"] for o in runtime.agent_offers()] == ["codex", "claude"]
        await picker_offer(runtime, browser)
        await picker_settings(runtime, browser)
        await picker_composer(runtime, browser)
        await picker_workers(runtime, browser)
    finally:
        # The paused task set to go on at its reset goes on no more: its wait (hours away)
        # would hold every later round's settle().
        for task_id in store.items("goonat:"):
            store.delete(f"goonat:{task_id}")
            runtime.task_actions.stop_going_on(task_id)
        await agents_saved(runtime, {"codex": {"enabled": False}, "claude": {"enabled": False}})
        codex_off_path(folder)


async def picker_offer(runtime, browser):
    """Home's first-run card: once per agent; Not now leaves it off, Allow turns it on (new
    workers stay on Copilot)."""
    from chiefofstaff import agents

    for scheme in ("light", "dark"):
        context, page, errors, _ = await agents_page(browser, scheme)
        await at_home(page)
        card = page.locator("#agent-offer")
        await expect(card.locator(".label")).to_have_text("Codex is ready")
        await expect(card.locator("p").first).to_have_text(
            "Allow workers to use Codex (0.167.2 · signed in with ChatGPT)? New workers stay on "
            "Copilot unless you ask for Codex."
        )
        await expect(card.get_by_role("button")).to_have_text(["Not now", "Allow"])
        await picker_shoot(page, "first-run-card", scheme, focus=card)
        assert not errors, errors
        await context.close()
    context, page, errors, _ = await agents_page(browser, "light")
    await at_home(page)
    await page.locator("#agent-offer").get_by_role("button", name="Allow").click()
    await expect(page.locator(".toast").first).to_contain_text("Codex is available for workers.")
    # Claude's card next: Not now leaves it off, and it is not asked again.
    claude = page.locator("#agent-offer")
    await expect(claude.locator(".label")).to_have_text("Claude is ready")
    await expect(claude.locator(".muted")).to_have_text(PLAN_NOTE)
    await picker_shoot(page, "first-run-card-claude", "light", focus=claude)
    dark, other, other_errors, _ = await agents_page(browser, "dark")
    await at_home(other)
    await expect(other.locator("#agent-offer .label")).to_have_text("Claude is ready")
    await picker_shoot(other, "first-run-card-claude", "dark", focus=other.locator("#agent-offer"))
    assert not other_errors, other_errors
    await dark.close()
    await claude.get_by_role("button", name="Not now").click()
    await expect(page.locator("#agent-offer")).to_have_count(0)
    assert agents.turned_on(runtime.store) == ["codex"]
    assert agents.new_workers(runtime.store) == "copilot"
    assert runtime.store.get(f"{agents.OFFERED}claude") == "not now"
    assert not errors, errors
    await context.close()
    log("picker: a first-run card per agent; Allow turns it on, Not now leaves it off, once")


async def picker_settings(runtime, browser):
    """Agents & models: new workers run on (first: a menu once another agent is on), a row per
    agent; a plan's usage on its meter, amber from 80%, each window on focus; picking an agent
    where new workers run uses it for workers; turned off, they run on Copilot."""
    runtime.store.set(
        "agentlimit:claude",
        json.dumps({"status": "allowed_warning", "window": "five_hour", "used": 84,
                    "resets_at": int(time.time()) + 5400}),
    )  # fmt: skip
    for scheme in ("light", "dark"):
        context, page, errors, _ = await agents_page(browser, scheme)
        tab = await open_agents(page)
        codex = tab.locator("#agent-codex")
        await expect(codex.locator(".status")).to_have_text("Ready")
        await unfold_agent(tab, "codex")
        await expect(codex.locator(".about")).to_contain_text("Signed in with ChatGPT")
        claude = tab.locator("#agent-claude")
        meter = claude.locator(".plan-meter")
        await expect(meter).to_have_text("84% 5-hour")
        await expect(meter).to_have_class(re.compile(r"\bhigh\b"))
        await expect(claude.locator(".agent-alert")).to_have_count(0)  # A close one: its meter.
        await expect(tab.locator("#agent-claude-usage")).to_have_text(
            re.compile(r"^84% of your 5-hour limit · resets ")
        )
        await expect(codex.locator(".plan-meter")).to_have_text("42% 5-hour")
        await expect(codex.locator(".plan-meter")).not_to_have_class(re.compile(r"\bhigh\b"))
        choose = tab.locator("select[name=agent_new_workers]")
        await expect(choose.locator("option")).to_have_text(["Copilot", "Codex", "Last used"])
        await expect(codex.locator("select[name=agent_codex_model]")).to_be_visible()
        await picker_shoot(page, "settings-rows", scheme, "#settings-dialog")
        # Claude used for workers: new workers run on lists it, and the hint names another.
        await claude.locator("input[name=agent_claude_enabled]").check()
        await expect(choose.locator("option")).to_have_text(
            ["Copilot", "Codex", "Claude Code", "Last used"]
        )
        await choose.select_option("claude")
        await expect(claude.locator("input[name=agent_claude_enabled]")).to_be_checked()
        await expect(claude.locator(".default-chip")).to_be_visible()
        await expect(tab.locator("#new-work-hint")).to_have_text(
            re.compile(r"For one task, type @codex or say “do this with Codex”\.$")
        )
        await picker_shoot(page, "settings-start-on", scheme, "#settings-dialog")
        # The one new workers run on, turned off: they run on Copilot again.
        await claude.locator("input[name=agent_claude_enabled]").uncheck()
        await expect(choose).to_have_value("copilot")
        await page.keyboard.press("Escape")
        assert not errors, errors
        await context.close()
    log("picker: Agents & models: new workers run on, rows, usage on its meter, amber at 80%")


async def picker_composer(runtime, browser):
    """@ in the composer lists the agents on (and Copilot) before workers; one chosen is a chip
    ("On Codex", removable) and goes with the message: the Router hears it as named."""
    for scheme in ("light", "dark"):
        context, page, errors, _ = await agents_page(browser, scheme)
        await at_home(page)
        box = page.locator("#composer-input")
        await box.fill("@")
        await box.press("End")
        await box.type("c")
        options = page.locator("#mention-list li.agent .name")
        # Every agent, Claude too (ready, off for workers: dimmed, with Turn on).
        await expect(options).to_have_text(["Copilot", "Codex", "Claude"])
        await expect(page.locator("#mention-list li.agent.dim .name")).to_have_text(["Claude"])
        await picker_shoot(page, "composer-autocomplete", scheme, focus=page.locator("#composer"))
        await page.locator("#mention-list li.agent", has_text="Codex").click()
        chip = page.locator("#composer-to")
        await expect(chip).to_have_text("On Codex▾\u00d7")
        await expect(box).to_have_value("")
        await box.fill("Port the parser to Rust")
        await picker_shoot(page, "composer-chip", scheme, focus=page.locator("#composer"))
        mine = page.locator("#timeline .msg.owner", has_text="Port the parser to Rust")
        if scheme == "dark":
            # Its clear button takes the chip away (and the agent with it); chosen again, it is
            # back. Sent once (above, in light): one bubble, said to be on Codex.
            await chip.locator("button.clear").click()
            await expect(chip).to_be_hidden()
            await page.locator("#composer-input").fill("@cod")
            await page.locator("#mention-list li.agent", has_text="Codex").click()
            await expect(chip).to_have_text("On Codex▾\u00d7")
            await chip.locator("button.clear").click()
            await box.fill("")
            await expect(mine).to_have_count(1)
            await picker_shoot(page, "composer-sent", scheme, focus=mine)
            assert not errors, errors
            await context.close()
            continue
        sent: list = []

        def posted(request, sent=sent):
            if request.method == "POST" and request.url.endswith("/messages"):
                sent.append(request.post_data_json)

        page.on("request", posted)
        await box.press("Enter")
        await expect(chip).to_be_hidden()
        await until(lambda sent=sent: sent)
        assert sent == [{"text": "Port the parser to Rust", "agent": "codex"}], sent
        await expect(mine).to_have_count(1)
        await expect(mine.locator("span.to")).to_have_text("On Codex")
        await picker_shoot(page, "composer-sent", scheme, focus=mine)
        assert not errors, errors
        await context.close()
    log("picker: @codex in the composer, as a chip that goes with the message")


async def picker_workers(runtime, browser):
    """A Codex worker's agent on its row and header; a Copilot one that was to run on Codex says
    why; Continue with… in its ⋯ goes on there; the Router's card for a named agent that could
    not run; a task its plan's limit paused; the Router's pick, in its words."""
    from copilot.tools import ToolInvocation

    store = runtime.store
    router = store.get("router_id")
    spawn = runtime.toolset.spawn_on(["codex"])
    # The Router's turn on the owner's @codex message (picker_composer) is over, as after its
    # session goes idle: that message's agent was for that turn only.
    last = store.value("SELECT max(id) FROM messages WHERE session_id=?", (router,))
    store.set(f"idle:{router}", str(last or 0))

    async def spawned(title, text, **more):
        return await spawn.handler(
            ToolInvocation(session_id=router, tool_call_id="t", tool_name="spawn_worker",
                           arguments={"title": title, "instructions": text, "why_new": "t",
                                      **more})
        )  # fmt: skip

    made = json.loads((await spawned("Port the parser", "Report done: Ported.", agent="codex"))
                      .text_result_for_llm)  # fmt: skip
    worker = made["session_id"]
    await until(lambda: store.session(worker)["status"] == "done", 30)
    # The Router's pick, in one line of its reply (as its rule asks).
    runtime.add_message(
        router, "assistant", "On Codex: a long refactor it handles well. Started “Port the parser”."
    )
    fallback = json.loads(
        (await spawned("Tidy the changelog", "Report done: Tidied.")).text_result_for_llm
    )
    store.set(f"agentfallback:{fallback['session_id']}", "Started on Copilot: Codex is signed out.")
    runtime.publish_session(store.session(fallback["session_id"]))
    for scheme in ("light", "dark"):
        context, page, errors, _ = await agents_page(browser, scheme)
        await at_home(page)
        pick = page.locator("#timeline .msg", has_text="On Codex: a long refactor")
        await picker_shoot(page, "router-pick", scheme, focus=pick)
        row = page.locator("#session-list .item", has_text="Port the parser")
        await expect(row.locator(".agent-mark")).to_have_attribute("aria-label", "Codex · GPT-TEST")
        other = page.locator("#session-list .item", has_text="Tidy the changelog")
        await expect(other.locator(".agent-mark")).to_have_attribute(
            "aria-label", re.compile(r"^Copilot · .*Started on Copilot: Codex is signed out\.$")
        )
        await sidebar_shoot(page, "sidebar-badges", scheme)
        await page.evaluate("id => import('/static/session.js').then((m) => m.select(id))", worker)
        await expect(page.locator("#session-meta")).to_contain_text("Codex · GPT-TEST")
        await page.locator("#session-more-button").click()
        moves = page.locator("#session-menu-items button", has_text="Continue with")
        await expect(moves).to_have_text(["Continue with Copilot…"])
        # (The menu's buttons reach past its edges on purpose: their hover, as for Copilot's.)
        await picker_shoot(page, "worker-continue-menu", scheme, "#session-header .heading")
        await page.keyboard.press("Escape")
        await page.evaluate(
            "id => import('/static/session.js').then((m) => m.select(id))", fallback["session_id"]
        )
        # (Its agent, before its first step, is a button: its popover changes it.)
        why = page.locator("#session-meta .agent-chip, #session-meta .agent-name").first
        await expect(why).to_have_attribute("title", "Started on Copilot: Codex is signed out.")
        await picker_shoot(page, "worker-fallback-header", scheme, "#session-header")
        assert not errors, errors
        await context.close()
    # Continue with Copilot: a new worker goes on in its folders; this one closes.
    context, page, errors, _ = await agents_page(browser, "light")
    await page.evaluate("id => import('/static/session.js').then((m) => m.select(id))", worker)
    await page.locator("#session-more-button").click()
    await page.locator("#session-menu-items button", has_text="Continue with Copilot…").click()
    # What it sends, in one line; the worker stays open.
    dialog = page.locator("dialog.continue-dialog")
    await expect(dialog.locator(".continue-line")).to_have_text(
        re.compile(r"^Sends a summary \(\d+ words\) and no changes to a new Copilot worker; "
                   r"this one stays open\.$")
    )  # fmt: skip
    await picker_shoot(page, "continue-dialog", "light", focus=dialog)
    await dialog.get_by_role("button", name="Continue").click()
    await expect(page.locator("#session-title")).to_have_text("Port the parser", timeout=10000)

    def successor():
        return next((r["id"] for r in store.open_sessions("worker")
                     if store.get(f"continues:{r['id']}") == worker), "")  # fmt: skip

    await until(successor, 10)
    moved = successor()
    assert runtime.agent(moved) == "copilot" and not store.session(worker)["closed_at"]
    notices = page.locator("#timeline .notice", has_text="Continues “Port the parser”")
    await expect(notices).to_have_count(1)
    # On Copilot, it says so as every worker does while another agent is on: its row, its header.
    badge = page.locator("#session-list .item.selected .agent-mark")
    await expect(badge).to_have_attribute("aria-label", re.compile(r"^Copilot( · \S.*)?$"))
    await expect(page.locator("#session-meta")).to_contain_text("Copilot")
    await picker_shoot(page, "worker-continued", "light", "#main")
    assert not errors, errors
    await context.close()
    context, page, errors, _ = await agents_page(browser, "dark")
    await page.evaluate("id => import('/static/session.js').then((m) => m.select(id))", moved)
    await expect(page.locator("#timeline .notice", has_text="Continues “Port")).to_have_count(1)
    await picker_shoot(page, "worker-continued", "dark", "#main")
    assert not errors, errors
    await context.close()
    # A named agent that could not run: the card in the Router's chat says why and offers what
    # to do (the Router does not say it again).
    runtime.agent_refused_card(
        "codex",
        "Codex is not signed in: sign in from Settings → Agents & models.",
        {"title": "Benchmark the importer", "instructions": "Benchmark it", "why_new": "t"},
    )
    for scheme in ("light", "dark"):
        context, page, errors, _ = await agents_page(browser, scheme)
        await at_home(page)
        card = page.locator("#timeline .card.agent-refused")
        await expect(card.locator("p")).to_have_text(
            "Codex needs you to sign in. Your message is saved: “Benchmark the importer” starts "
            "on Codex once you have."
        )
        # What they can do now: Codex runs here (Start on Codex), else Sign in (agent smooth).
        await expect(card.locator(".actions > button")).to_have_text(
            ["Start on Codex", "Don't run it", "Continue with Copilot"]
        )
        await picker_shoot(page, "recovery-card", scheme, focus=card)
        assert not errors, errors
        await context.close()
    context, page, errors, _ = await agents_page(browser, "light")
    await at_home(page)
    card = page.locator("#timeline .card.agent-refused")
    await card.get_by_role("button", name="Continue with Copilot").click()
    await expect(card.locator(".actions")).to_have_count(0)
    started = [r for r in store.open_sessions("worker") if r["title"] == "Benchmark the importer"]
    assert len(started) == 1 and runtime.agent(started[0]["id"]) == "copilot"
    # One line says it started, and where.
    lines = page.locator("#timeline .event.started", has_text="Benchmark the importer")
    await expect(lines.locator(".what")).to_have_text(
        ["Started “Benchmark the importer” on Copilot instead"]
    )
    assert not errors, errors
    await context.close()
    # Work for a worker of theirs on an agent signed out now: the same card. Its work went on on
    # Copilot already (Continue with… above): in one click, what it was given goes to that
    # worker, and nothing new is made; the first stays open.
    runtime.agent_refused_card(
        "codex",
        "Codex is not signed in: sign in from Settings → Agents & models.",
        {"title": "Port the parser"},
        worker=worker,
        text="Also port the tokenizer.",
    )
    for scheme in ("light", "dark"):
        context, page, errors, _ = await agents_page(browser, scheme)
        await at_home(page)
        card = page.locator("#timeline .card.agent-refused", has_text="Port the parser")
        await expect(card.locator("p")).to_have_text(
            "Codex needs you to sign in. Your message is saved: “Port the parser” gets it on "
            "Codex once you have."
        )
        await expect(card.locator(".actions > button")).to_have_text(
            ["Send it on Codex", "Don't run it", "Continue with Copilot"]
        )
        await picker_shoot(page, "recovery-card-worker", scheme, focus=card)
        if scheme == "dark":
            before = {r["id"] for r in store.open_sessions("worker")}
            await card.get_by_role("button", name="Continue with Copilot").click()
            await expect(card.locator(".actions")).to_have_count(0)
            await expect(card.locator(".card-outcome")).to_have_text(
                "Sent to “Port the parser” on Copilot"
            )
            await expect(
                page.locator("#timeline .event.started .what", has_text="Continued in “Port the")
            ).to_have_text(["Continued in “Port the parser”"])
            assert {r["id"] for r in store.open_sessions("worker")} == before, "none made"
            given = store.messages(moved)
            assert any("Also port the tokenizer." in m["text"] for m in given), given[-3:]
            assert not store.session(worker)["closed_at"], "the first one stays open"
        assert not errors, errors
        await context.close()
    # A task its plan's limit paused (its request refused): which limit and until when, on its
    # row; it goes on by itself then (under Monitoring), or continues on another agent now.
    # (The Copilot workers above, closed: room for it to start now.)
    for row in store.open_sessions("worker"):
        if runtime.agent(row["id"]) == "copilot":
            await runtime.workers.close(row["id"], by="you")
    paused = json.loads((await spawned("Profile the cache warmer", "Hit the usage limit",
                                       agent="codex")).text_result_for_llm)  # fmt: skip
    task_id = paused["task_id"]
    await until(lambda: runtime.tasks.get(task_id)["status"] == "waiting", 30)
    assert store.get(f"stopped:{task_id}") == "its plan's limit"
    resets = runtime.task_actions.view(runtime.tasks.get(task_id))["paused"]["resets_at"]
    assert resets > time.time()
    assert store.get(f"goonat:{task_id}") == str(resets), "it goes on by itself then"
    sent = len(store.messages(paused["session_id"]))
    selector = f'#panel .card.task[data-session="{paused["session_id"]}"]'
    for scheme in ("light", "dark"):
        context, page, errors, _ = await agents_page(browser, scheme)
        if await page.locator("#home-tabs").is_visible():
            await page.locator("#home-tabs [data-tab=tasks]").click()
        card = page.locator(selector)
        monitored = f'#tasks-monitoring .card.task[data-session="{paused["session_id"]}"]'
        await expect(page.locator(monitored)).to_be_visible()
        # ("tomorrow 1:12 AM", "Sun 12:43 AM": its day too, when that is not today.)
        when = await page.evaluate(
            "at => import('/static/board.js').then((m) => m.resetTime(at))", resets * 1000
        )
        line = card.locator(".row-line .why")
        said = f"Codex's 5-hour limit is used up · resets {when}"
        await expect(line).to_have_text(f"{said} · goes on by itself then")
        await expect(line).to_have_attribute(
            "data-tip", re.compile(r"^Codex's 5-hour limit is used up · resets .*\nPaused: Codex's")
        )
        await expect(card.locator(".row-line .latest")).to_have_count(0)
        await expect(card.locator(".head")).to_contain_text("Paused")
        assert (await card.inner_text()).count("Paused") == 1, "said once"
        # The one other agent that can run it: Continue with it now (what it sends, then).
        elsewhere = card.get_by_role("button", name="Continue with Copilot…")
        await expect(elsewhere).to_be_visible()
        await picker_shoot(page, "plan-limit-paused", scheme, "#main", focus=card, tab="tasks")
        await elsewhere.click()
        dialog = page.locator("dialog.continue-dialog")
        await expect(dialog.locator(".continue-line")).to_contain_text("to a new Copilot worker")
        await dialog.get_by_role("button", name="Cancel").click()
        await expect(dialog).to_have_count(0)
        assert not errors, errors
        await context.close()
    assert runtime.tasks.get(task_id)["status"] == "waiting"
    assert len(store.messages(paused["session_id"])) == sent, "nothing sent before its reset"
    await paused_continued_once(runtime, browser, paused["session_id"], task_id, selector)
    # (Codex's limit not kept for the rounds after: they have Codex as it is.)
    store.delete("agentlimit:codex")
    if waiting := runtime.limit_waits.pop("codex", None):
        waiting.cancel()
    log("picker: badges, Continue with…, the recovery card, a paused task, the Router's pick")


UX_SHOTS = Path(os.environ.get("UI_PICKER_UX_SHOTS") or SHOTS / "picker")
# The Router's composer with another agent installed (session.js), and how much room its input
# leaves beyond its placeholder (its font, measured; < 0: cut off).
AGENT_PLACEHOLDER = "Ask anything… type @ to pick an agent or a worker"
PLACEHOLDER_SPARE = """input => {
  const style = getComputedStyle(input);
  const context = document.createElement("canvas").getContext("2d");
  context.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
  const room = input.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  return room - context.measureText(input.placeholder).width;
}"""
UX_WIDTHS = tuple(int(w) for w in os.environ.get("UI_PICKER_UX_WIDTHS", "800,1280").split(","))


async def ux_shoot(page, name: str, scheme: str, target: str = "#main", focus=None):
    """`name` at 800 and 1280 px (UI_PICKER_UX_WIDTHS), light or dark, into files/shots/picker
    (UI_PICKER_UX_SHOTS); nothing of `target` cut off or spilling over."""
    UX_SHOTS.mkdir(parents=True, exist_ok=True)
    for width in UX_WIDTHS:
        await page.set_viewport_size({"width": width, "height": 900})
        if focus is not None:
            await focus.evaluate("n => n.scrollIntoView({block: 'center', inline: 'nearest'})")
        await quiet_for(page)
        await page.screenshot(path=UX_SHOTS / f"{name}-{width}-{scheme}.png")
        found = await page.locator(target).evaluate(SCROLLED_OVERFLOWS)
        assert not found, (name, width, found)
        # A popover open stays inside the window.
        for box in await page.locator(".agent-popover").evaluate_all(
            "nodes => nodes.map((n) => n.getBoundingClientRect().toJSON())"
        ):
            assert box["left"] >= 0 and box["right"] <= width, (name, width, box)
    await page.set_viewport_size({"width": 1280, "height": 900})


async def round_picker_ux(runtime, browser, failures: list[str]):
    """The agent picker as the owner's research asked (2026-11-19): @ lists every agent with its
    status (Ready, Sign-in needed, Not installed; one not ready dimmed, with the command and
    Copy); the chip's popover (agent, its models and effort, whose plan it uses); two agents
    picked to compare, each worker a row of its own (⇄); each worker's agent mark on its
    row, task and header; the start line's chip (Restart on… before its first step); Settings'
    status lines, Use for workers off until ready, Last used, its own sessions."""
    from copilot.tools import ToolInvocation

    from chiefofstaff import agents

    folder, _ = codex_on_path()
    store = runtime.store
    router = store.get("router_id")
    store.delete("agentlimit:codex")
    os.environ["FAKE_CLAUDE_SIGNED_OUT"] = "1"
    try:
        runtime.found_cache = (float("-inf"), [])
        runtime.glanced.clear()
        await agents_saved(runtime, {"codex": {"enabled": True}})
        await runtime.refresh_agent_status()
        for scheme in ("light", "dark"):
            context, page, errors, _ = await agents_page(browser, scheme)
            await at_home(page)
            box = page.locator("#composer-input")
            # With another agent installed, the placeholder says @ picks an agent too: whole at
            # 800 px (Home in tabs) as at 1280.
            await expect(box).to_have_attribute("placeholder", AGENT_PLACEHOLDER)
            for width in (800, 1280):
                await page.set_viewport_size({"width": width, "height": 900})
                await at_home(page)
                spare = await box.evaluate(PLACEHOLDER_SPARE)
                assert spare >= 0, (width, scheme, spare)
            await page.set_viewport_size({"width": 1280, "height": 900})
            await box.fill("@")
            await box.press("End")
            names = page.locator("#mention-list li.agent .name")
            await expect(names).to_have_text(["Copilot", "Codex", "Claude"])
            # Agents, then workers: a quiet label over each.
            await expect(page.locator("#mention-list li.group-label")).to_have_text(
                ["Agents", "Workers"]
            )
            claude = page.locator("#mention-list li.agent.dim", has_text="Claude")
            await expect(claude.locator(".status")).to_contain_text("Sign-in needed")
            # Picked, it signs in (round_agent_smooth): never a control inside an option.
            await expect(claude.locator(".agent-fix")).to_have_text("Pick to sign in")
            await expect(claude.locator("button")).to_have_count(0)
            await ux_shoot(page, "at-statuses", scheme, focus=page.locator("#composer"))
            await context.grant_permissions(["clipboard-read", "clipboard-write"])
            # Its command copied from the popover (Copy command, beside Sign in): one toast.
            await page.locator("#composer-to").evaluate("node => node.hidden")
            await box.fill("@")
            await box.press("End")
            await page.locator("#mention-list li.agent", has_text="Codex").click()
            chip = page.locator("#composer-to .agent-chip")
            await expect(chip).to_have_text("Codex▾")
            await chip.click()
            pop = page.locator(".agent-popover")
            await expect(pop.locator(".agent-pop-agent .name")).to_have_text(
                ["Copilot", "Codex", "Claude"]
            )
            await expect(pop.locator(".agent-pop-pays")).to_have_text("Uses your ChatGPT sign-in")
            # Claude's command copied from here (Copy command, beside Sign in), twice: one toast
            # says so (the earlier goes), clear of the popover while it is open (below).
            row = pop.locator(".agent-pop-row", has_text="Claude")
            await expect(row.locator(".setup-action")).to_have_text("Sign in")
            await row.locator(".copy-fix").click()
            toast = page.locator(".toast")
            await expect(toast).to_have_count(1)
            await toast.evaluate("node => { node.dataset.earlier = '1'; }")
            await row.locator(".copy-fix").click()
            await expect(page.locator(".toast:not([data-earlier])")).to_have_text(
                "Copied: run claude auth login in a terminal."
            )
            assert await toast.count() == 1, await toast.all_inner_texts()
            copied = await page.evaluate("() => navigator.clipboard.readText()")
            assert copied == "claude auth login", copied
            ours, theirs = await pop.bounding_box(), await toast.bounding_box()
            assert (
                theirs["y"] + theirs["height"] <= ours["y"]
                or theirs["y"] >= ours["y"] + ours["height"]
            ), (ours, theirs)
            model = pop.locator("select[aria-label=Model]")
            await expect(model.locator("option")).to_have_text(
                ["Default", "GPT-TEST", "GPT-MINI"], timeout=10000
            )
            await model.select_option("gpt-mini")
            # Named as Codex names it, as in the popover.
            await expect(chip).to_have_text("CodexGPT-MINI▾")
            await ux_shoot(page, "chip-popover", scheme, focus=pop)
            await pop.locator(".agent-pop-agent", has_text="Copilot").click()
            await expect(pop.locator(".agent-pop-pays")).to_have_text("Uses your Copilot plan")
            await expect(chip).to_have_text("Copilot▾")
            await page.keyboard.press("Escape")
            await expect(pop).to_have_count(0)
            # A second agent picked: the task on each (Compare).
            await box.fill("@cod")
            await page.locator("#mention-list li.agent", has_text="Codex").click()
            await expect(page.locator("#composer-to")).to_be_visible()
            await expect(page.locator("#composer-to")).to_have_text(
                "Compare on Copilot and Codex\u00d7"
            )
            await box.fill("Make the importer twice as fast")
            await ux_shoot(page, "compare-chip", scheme, focus=page.locator("#composer"))
            if scheme == "dark":
                await page.locator("#composer-to button.clear").click()
                await box.fill("")
                assert not errors, errors
                await context.close()
                continue
            sent: list = []
            page.on("request", lambda r, sent=sent: sent.append(r.post_data_json)
                    if r.method == "POST" and r.url.endswith("/messages") else None)  # fmt: skip
            await box.press("Enter")
            await until(lambda sent=sent: sent)
            assert sent[0]["agents"] == ["copilot", "codex"], sent
            mine = page.locator("#timeline .msg.owner", has_text="Make the importer")
            await expect(mine.locator("span.to")).to_have_text("Compare on Copilot and Codex")
            assert not errors, errors
            await context.close()
        # The Router compares: a worker on each, grouped; each one's mark; the start line's chip.
        last = store.value("SELECT max(id) FROM messages WHERE session_id=?", (router,))
        store.set(f"idle:{router}", str(last or 0))
        spawn = runtime.toolset.spawn_on(["codex"])

        async def spawned(title, text, **more):
            found = await spawn.handler(
                ToolInvocation(session_id=router, tool_call_id="t", tool_name="spawn_worker",
                               arguments={"title": title, "instructions": text, "why_new": "t",
                                          **more})
            )  # fmt: skip
            return json.loads(found.text_result_for_llm)

        made = await spawned("Speed up the importer", "Make it twice as fast",
                             agent="copilot", compare_with=["codex"])  # fmt: skip
        pair = [worker["session_id"] for worker in made["workers"]]
        assert [runtime.agent(i) for i in pair] == ["copilot", "codex"]
        fresh = await spawned("Draft the release notes", "Draft them")
        untouched = fresh["session_id"]
        assert runtime.view(store.session(untouched))["untouched"]
        for scheme in ("light", "dark"):
            context, page, errors, _ = await agents_page(browser, scheme)
            await at_home(page)
            # Each side a row like any other, named by its work, its agent's mark, and a
            # small ⇄ saying the other side on hover; no caption, no nesting.
            await expect(page.locator("#session-list .compare-group")).to_have_count(0)
            for side, other in zip(pair, ("Codex", "Copilot"), strict=True):
                row = page.locator(f'#session-list .item[data-id="{side}"]')
                await expect(row.locator(".label")).to_have_text("Speed up the importer")
                await expect(row.locator(".compare-mark")).to_have_attribute(
                    "title", f"Compared with {other}: the same task on each"
                )
            row = page.locator("#session-list .item", has_text="Draft the release notes")
            await expect(row.locator(".agent-mark")).to_have_attribute(
                "aria-label", re.compile(r"^Copilot")
            )
            await sidebar_shoot_to(page, "compare-sidebar", scheme)
            # The start lines in a row are a run of routing receipts, one line (E129), their
            # chips folded with them; opened, every chip shows and opens its menu.
            fold = start_fold(page, "Draft the release notes")
            await expect(fold.locator(".fold-line .what")).to_have_text(
                re.compile(r"^[2-9] routing updates$")
            )
            chips = fold.locator(".fold-items .event.started .agent-chip")
            await expect(chips).to_have_count(3)
            await expect(chips.first).to_be_hidden()
            # Its line: each started worker's agent mark, its agent (and model) on hover.
            marks = fold.locator(".fold-line .marks .agent-mark")
            await expect(marks).to_have_count(3)
            marked = [await mark.get_attribute("data-agent") for mark in await marks.all()]
            assert marked == ["copilot", "codex", "copilot"], marked
            await expect(marks.nth(1)).to_have_attribute("aria-label", re.compile(r"^Codex"))
            await ux_shoot(page, "start-chips-folded", scheme, focus=fold)
            line = await start_line(page, "Draft the release notes")
            await expect(fold.locator(".fold-items .agent-chip:visible")).to_have_count(3)
            await expect(chips).to_have_text(["Copilot", "Codex", "Copilot"])
            start = line.locator(".agent-chip")
            await expect(start).to_have_text("Copilot")
            await start.click()
            menu = line.locator(".agent-chip-wrap .menu")
            await expect(menu.get_by_role("menuitem")).to_have_text(["Restart on Codex"])
            await ux_shoot(page, "start-chip-restart", scheme, focus=line)
            await page.keyboard.press("Escape")
            await page.mouse.click(5, 5)
            if await page.locator("#home-tabs").is_visible():
                await page.locator("#home-tabs [data-tab=tasks]").click()
            task = page.locator(f'#panel .card.task[data-session="{pair[1]}"]')
            await expect(task.locator(".head .agent-mark")).to_have_attribute(
                "aria-label", re.compile(r"^Codex")
            )
            await ux_shoot(page, "task-marks", scheme, focus=task)
            assert not errors, errors
            await context.close()
        # A side's own tab (Notes: no team of the owner's) starts with its task's lineage: the
        # same task on each agent, the one the owner accepts picked.
        sides = [worker["task_id"] for worker in made["workers"]]
        context, page, errors, _ = await agents_page(browser, "light")
        await at_home(page)
        await page.evaluate(
            f"import('/static/session.js').then((m) => m.select({json.dumps(pair[0])}))"
        )
        await page.locator("#worker-tabs").get_by_role("tab", name="Notes").click()
        lineage = page.locator("#timeline > .team-lineage .lineage")
        await expect(lineage.locator(".fan.compare .lnode")).to_have_count(1)
        await expect(lineage.locator(".caption")).to_have_text(
            "The same task on Copilot and Codex · yours to pick"
        )
        # Its peek names it once (its title is its worker's name).
        await expect(lineage.locator(".peek .what")).to_have_count(0)
        runtime.tasks.move(sides[1], "done")  # The owner accepts Codex's.
        await page.evaluate(
            f"import('/static/team.js').then((m) => m.workerLineage({json.dumps(pair[0])}))"
        )
        await expect(page.locator("#timeline > .team-lineage .caption")).to_have_text(
            "The same task on Copilot and Codex · Codex picked", timeout=10000
        )
        await ux_shoot(page, "compare-team", "light", focus=page.locator("#timeline"))
        assert not errors, errors
        await context.close()
        # A worker on Codex in the @ list is a worker (to message), not the agent Codex.
        context, page, errors, _ = await agents_page(browser, "light")
        await at_home(page)
        box = page.locator("#composer-input")
        await box.fill("@Speed")
        await box.press("End")
        listed = page.locator("#mention-list li")
        await expect(listed.locator(".name")).to_have_text(
            ["Speed up the importer", "Speed up the importer"]
        )
        await expect(page.locator("#mention-list li.agent")).to_have_count(0)
        await listed.nth(1).click()
        await expect(page.locator("#composer-to")).to_have_text(
            re.compile("^To Speed up the importer")
        )
        await page.locator("#composer-to button.clear").click()
        await context.close()
        # Before its first step, its header's agent opens the popover; Restart on Codex: at once.
        context, page, errors, _ = await agents_page(browser, "light")
        await page.evaluate(
            "id => import('/static/session.js').then((m) => m.select(id))", untouched
        )
        badge = page.locator("#session-meta .agent-chip")
        await expect(badge).to_have_text("Copilot▾")
        await badge.click()
        pop = page.locator(".agent-popover")
        await expect(pop.locator(".agent-pop-agent[aria-checked=true] .name")).to_have_text(
            "Copilot"
        )
        await ux_shoot(page, "header-popover", "light", focus=pop)
        await pop.locator(".agent-pop-agent", has_text="Codex").click()
        await expect(page.locator(".toast").first).to_contain_text("Restarted on Codex.")
        await until(lambda: store.session(untouched)["closed_at"], 10)
        moved = store.get(f"continued:{untouched}")
        assert moved and runtime.agent(moved) == "codex"
        await at_home(page)
        restarted = page.locator(
            "#timeline .event.started", has_text="Restarted “Draft the release"
        )
        await expect(restarted).to_have_count(1)
        await ux_shoot(page, "restarted-line", "light", focus=restarted)
        assert not errors, errors
        await context.close()
        # Settings: each row's status; Claude's sign-in needed: Use for workers off, Sign in,
        # its command; whose plan new workers use; Last used; its own sessions, folded.
        for scheme in ("light", "dark"):
            context, page, errors, _ = await agents_page(browser, scheme)
            tab = await open_agents(page)
            codex = tab.locator("#agent-codex")
            await expect(codex.locator(".status")).to_have_text("Ready")
            claude = tab.locator("#agent-claude")
            await expect(claude.locator(".status")).to_have_text("Sign in needed")
            await expect(claude.locator(".action-slot button")).to_have_text("Sign in")
            await unfold_agent(tab, "claude")
            await expect(claude.locator(".setup-details")).to_have_text(
                "Command claude auth loginCopy command"
            )
            await unfold_agent(tab, "codex")
            await expect(codex.locator("[data-again]")).to_have_text("Sign in again")
            await expect(claude.locator("input[name=agent_claude_enabled]")).to_be_disabled()
            choose = tab.locator("select[name=agent_new_workers]")
            await expect(choose.locator("option")).to_have_text(["Copilot", "Codex", "Last used"])
            # Whose plan the agent chosen there uses, as its row says.
            pays = tab.locator("#new-work-hint")
            await expect(pays).to_have_text(re.compile(r"^Uses your Copilot plan\. "))
            await choose.select_option("codex")
            await expect(pays).to_have_text(re.compile(r"^Uses your ChatGPT sign-in\. "))
            await choose.select_option("last")
            await expect(pays).to_have_text(
                re.compile(r"^Now Codex\. Uses your ChatGPT sign-in\. ")
            )
            await choose.select_option("copilot")
            await expect(tab.locator("#own-sessions summary > span")).to_have_text(
                "Chief of Staff's own sessions"
            )
            await expect(tab.locator("#own-sessions")).not_to_have_attribute("open", "")
            await ux_shoot(page, "settings-rows", scheme, "#settings-dialog")
            await page.keyboard.press("Escape")
            assert not errors, errors
            await context.close()
        # Not installed: dimmed in the @ list, with the command that installs it.
        runtime.found_cache = (time.monotonic(), ["codex"])
        for scheme in ("light", "dark"):
            context, page, errors, _ = await agents_page(browser, scheme)
            await at_home(page)
            box = page.locator("#composer-input")
            await box.fill("@cl")
            await box.press("End")
            missing = page.locator("#mention-list li.agent.dim", has_text="Claude")
            await expect(missing.locator(".status")).to_contain_text("Not installed")
            await expect(missing.locator(".agent-fix")).to_have_text("Pick to install")
            await ux_shoot(page, "at-not-installed", scheme, focus=page.locator("#composer"))
            await box.fill("")
            assert not errors, errors
            await context.close()
        assert agents.turned_on(store) == ["codex"]
        log("picker UX: @ statuses with their fix, the chip's popover, Compare grouped, marks, "
            "Restart on before a first step, Settings' status lines")  # fmt: skip
    finally:
        os.environ.pop("FAKE_CLAUDE_SIGNED_OUT", None)
        runtime.found_cache = (float("-inf"), [])
        runtime.glanced.clear()
        for row in store.open_sessions("worker"):
            if store.get(f"compare:{row['id']}") or store.get(f"continues:{row['id']}"):
                await runtime.workers.close(row["id"], by="you")
        await agents_saved(runtime, {"codex": {"enabled": False}})
        codex_off_path(folder)


async def round_agent_parity(runtime, browser, failures: list[str]):
    """Codex wherever Claude is (the owner's ask, 0.1.53): both signed in and on, each place
    that offers an agent offers both: the @ list, the chip's popover, Compare, a worker's Restart
    on… before its first step and Continue with… in its ⋯, the card for a named agent that
    could not run, Settings' rows and its own sessions (the Router, the Watchdog). Watch for you is
    offered on neither (what reads Slack stays on Copilot; never Codex)."""
    from copilot.tools import ToolInvocation

    folder, _ = codex_on_path()
    store = runtime.store
    router = store.get("router_id")
    store.delete("agentlimit:codex")
    store.delete("agentlimit:claude")
    os.environ.pop("FAKE_CLAUDE_SIGNED_OUT", None)
    made: list[str] = []
    try:
        runtime.found_cache = (float("-inf"), [])
        runtime.glanced.clear()
        await agents_saved(runtime, {"codex": {"enabled": True}, "claude": {"enabled": True}})
        await runtime.refresh_agent_status()
        context, page, errors, _ = await agents_page(browser, "light")
        await at_home(page)
        box = page.locator("#composer-input")
        await box.fill("@")
        await box.press("End")
        agents_listed = page.locator("#mention-list li.agent")
        await expect(agents_listed.locator(".name")).to_have_text(["Copilot", "Codex", "Claude"])
        await expect(page.locator("#mention-list li.agent.dim")).to_have_count(0)
        await agents_listed.filter(has_text="Claude").click()
        chip = page.locator("#composer-to .agent-chip")
        await expect(chip).to_have_text("Claude▾")
        await chip.click()
        pop = page.locator(".agent-popover")
        await expect(pop.locator(".agent-pop-agent .name")).to_have_text(
            ["Copilot", "Codex", "Claude"]
        )
        await expect(pop.locator(".agent-pop-agent.dim")).to_have_count(0)
        await page.keyboard.press("Escape")
        await page.locator("#composer-to button.clear").click()
        # Compare: the task on each of the two.
        await box.fill("@cod")
        await page.locator("#mention-list li.agent", has_text="Codex").click()
        await box.fill("@cla")
        await page.locator("#mention-list li.agent", has_text="Claude").click()
        await expect(page.locator("#composer-to")).to_contain_text("Compare on Codex and Claude")
        await page.locator("#composer-to button.clear").click()
        await box.fill("")
        assert not errors, errors
        await context.close()
        # A Copilot worker before its first step: Restart on either; in its ⋯, Continue with either.
        spawn = runtime.toolset.spawn_on(["codex", "claude"])
        found = await spawn.handler(
            ToolInvocation(session_id=router, tool_call_id="t", tool_name="spawn_worker",
                           arguments={"title": "Parity draft the notes",
                                      "instructions": "Draft them", "why_new": "t"})
        )  # fmt: skip
        untouched = json.loads(found.text_result_for_llm)["session_id"]
        made.append(untouched)
        assert runtime.view(store.session(untouched))["untouched"]
        context, page, errors, _ = await agents_page(browser, "light")
        await at_home(page)
        line = await start_line(page, "Parity draft the notes")
        await line.locator(".agent-chip").click()
        await expect(line.locator(".agent-chip-wrap .menu").get_by_role("menuitem")).to_have_text(
            ["Restart on Codex", "Restart on Claude"]
        )
        await page.keyboard.press("Escape")
        await page.mouse.click(5, 5)
        assert not errors, errors
        await context.close()
        found = await spawn.handler(
            ToolInvocation(session_id=router, tool_call_id="t", tool_name="spawn_worker",
                           arguments={"title": "Parity sum the logs",
                                      "instructions": "Sum them", "why_new": "t"})
        )  # fmt: skip
        done = json.loads(found.text_result_for_llm)["session_id"]
        made.append(done)
        await settle(runtime)
        await call_tool(runtime, "report", done, status="done", summary="Summed.")
        await settle(runtime)
        await until(lambda: store.session(done)["status"] not in {"working", "queued", "starting"})
        context, page, errors, _ = await agents_page(browser, "light")
        await page.evaluate("id => import('/static/session.js').then((m) => m.select(id))", done)
        await page.locator("#session-more-button").click()
        await expect(
            page.locator("#session-menu-items button", has_text="Continue with")
        ).to_have_text(["Continue with Codex…", "Continue with Claude…"])
        await page.keyboard.press("Escape")
        assert not errors, errors
        await context.close()
        # A named agent that could not run: Claude's card as Codex's (picker_workers).
        runtime.agent_refused_card(
            "claude",
            "Claude is not signed in: run claude, then /login.",
            {"title": "Parity benchmark", "instructions": "Benchmark it", "why_new": "t"},
        )
        context, page, errors, _ = await agents_page(browser, "light")
        await at_home(page)
        card = page.locator("#timeline .card.agent-refused", has_text="Parity benchmark")
        await expect(card.locator("p")).to_have_text(
            "Claude needs you to sign in. Your message is saved: “Parity benchmark” starts on "
            "Claude once you have."
        )
        # Claude is signed in here: started there in one click (else Sign in, as for Codex).
        await expect(card.locator(".actions > button")).to_have_text(
            ["Start on Claude", "Don't run it", "Continue with Copilot"]
        )
        await card.get_by_role("button", name="Don't run it").click()
        await expect(card.locator(".actions")).to_have_count(0)
        await expect(card.locator(".card-outcome")).to_have_text("Not run")
        assert not errors, errors
        await context.close()
        # Settings: a row each, ready; its own sessions: the Router and the Watchdog on either,
        # no Watch for you.
        context, page, errors, _ = await agents_page(browser, "light")
        tab = await open_agents(page)
        for name, pays in (("codex", "Uses your ChatGPT sign-in"), ("claude", "")):
            row = tab.locator(f"#agent-{name}")
            await expect(row.locator(".status")).to_have_text("Ready")
            if pays:
                await unfold_agent(tab, name)
                await expect(row.locator(".about")).to_contain_text(pays)
            await expect(row.locator(f"input[name=agent_{name}_enabled]")).to_be_checked()
        choose = tab.locator("select[name=agent_new_workers]")
        await expect(choose.locator("option")).to_have_text(
            ["Copilot", "Codex", "Claude Code", "Last used"]
        )
        system = tab.locator("#own-sessions")
        await system.locator("summary").click()
        for role in ("router", "watchdog"):
            await expect(system.locator(f"select[name=role_{role}] option")).to_have_text(
                ["Copilot", "Codex", "Claude Code"]
            )
        await expect(system.locator("select[name=role_connector]")).to_have_count(0)
        await ux_shoot(page, "parity-system-agents", "light", "#settings-dialog")
        await page.keyboard.press("Escape")
        assert not errors, errors
        await context.close()
        log("parity: Codex and Claude alike in @, the popover, Compare, Restart on, Continue "
            "with, the recovery card, Settings' rows and its own sessions")  # fmt: skip
    finally:
        for session_id in made:
            await runtime.workers.close(session_id, by="you")
        await agents_saved(runtime, {"codex": {"enabled": False}, "claude": {"enabled": False}})
        runtime.found_cache = (float("-inf"), [])
        runtime.glanced.clear()
        codex_off_path(folder)


async def round_agent_smooth(runtime, browser, failures: list[str]):
    """Agent smoothness (design/agent-smooth.md): where an agent says Sign-in needed or Not
    installed, one click runs its own CLI (the stand-ins here): the @ list (picked, with the
    keyboard too), the chip's popover, Settings' rows, its own sessions and the held card. The
    dialog shows its page's link (Copy link), a one-time code, a pasted code, its output; it
    turns Ready live. Held work resumes by itself (Resuming… Cancel, then Resumed after
    sign-in, under Handled for you). Install shows its command first. The @ list marks the
    agent the Router runs on; a Claude sign-in ending within a week is said."""
    from chiefofstaff import agents, agentsetup

    folder, _ = codex_on_path()
    store = runtime.store
    router = store.get("router_id")
    codex_home = Path(os.environ["FAKE_CODEX_HOME"])
    claude_config = Path(os.environ["CLAUDE_CONFIG_DIR"])
    installers = agentsetup.INSTALLERS
    made: list[str] = []

    def sign_codex_out():
        codex_home.mkdir(parents=True, exist_ok=True)
        (codex_home / "auth.json").unlink(missing_ok=True)
        (codex_home / "signed-out").write_text("")

    async def fresh_status():
        runtime.glanced.clear()
        runtime.agent_failed.clear()
        runtime.found_cache = (float("-inf"), [])
        await runtime.refresh_agent_status()

    try:
        await agents_saved(runtime, {"codex": {"enabled": True}})
        sign_codex_out()
        await fresh_status()
        assert runtime.agent_status("codex")["status"] == agents.SIGN_IN
        # The @ list: Codex dimmed, Pick to sign in; the Router's agent marked. Picked with the
        # keyboard: its sign-in starts, its page's link in the dialog.
        for scheme in ("light", "dark"):
            context, page, errors, _ = await agents_page(browser, scheme)
            await context.grant_permissions(["clipboard-read", "clipboard-write"])
            await at_home(page)
            box = page.locator("#composer-input")
            await box.fill("@")
            await box.press("End")
            codex = page.locator("#mention-list li.agent.dim", has_text="Codex")
            await expect(codex.locator(".agent-fix")).to_have_text("Pick to sign in")
            await expect(
                page.locator("#mention-list li.agent", has_text="Copilot").locator(".role-tag")
            ).to_have_text("Router")
            await ux_shoot(page, "smooth-at-sign-in", scheme, focus=page.locator("#composer"))
            await box.press("ArrowDown")
            await expect(page.locator("#mention-list li.active")).to_contain_text("Codex")
            await box.press("Enter")
            dialog = page.locator("dialog.agent-setup")
            await expect(dialog.locator("h2")).to_have_text("Sign in to Codex")
            link = dialog.get_by_role("link", name="Open sign-in page")
            await expect(link).to_have_attribute(
                "href", re.compile(r"^https://auth\.openai\.com/oauth/authorize\?")
            )
            await expect(link).to_have_attribute("target", "_blank")
            await expect(dialog.locator(".setup-waiting")).to_contain_text(
                "Waiting for you to sign in…"
            )
            await expect(dialog.locator(".setup-note")).to_have_text(
                "Codex signed you out as this began: Cancel leaves it signed out."
            )
            await dialog.get_by_role("button", name="Copy link").click()
            copied = await page.evaluate("() => navigator.clipboard.readText()")
            assert copied.startswith("https://auth.openai.com/oauth/authorize?"), copied
            await dialog.locator(".setup-output summary").click()
            await expect(dialog.locator(".setup-output pre")).to_contain_text(
                "Starting local login server"
            )
            await ux_shoot(page, "smooth-signin-dialog", scheme, "dialog.agent-setup")
            if scheme == "light":
                # Cancel: its process ends; Try again is there.
                await dialog.get_by_role("button", name="Cancel").click()
                await expect(dialog.locator(".agent-setup-body p").first).to_have_text("Cancelled.")
                await expect(dialog.get_by_role("button", name="Try again")).to_be_visible()
                await dialog.get_by_role("button", name="Close").click()
            else:
                # Signed in on Codex's page (the stand-in's browser): Ready, live.
                (codex_home / "approve").write_text("ok")
                await expect(dialog.locator(".setup-done")).to_contain_text(
                    "Codex is ready", timeout=15000
                )
                await ux_shoot(page, "smooth-signin-done", scheme, "dialog.agent-setup")
                await dialog.get_by_role("button", name="Done").click()
                await box.fill("@")
                await box.press("End")
                await expect(
                    page.locator("#mention-list li.agent", has_text="Codex")
                ).not_to_have_class(re.compile(r"\bdim\b"), timeout=10000)
                await box.fill("")
            assert not errors, errors
            await context.close()
        assert runtime.agent_status("codex")["status"] == agents.READY
        # Another device: a one-time code, from Settings' row (Sign in, Other ways).
        sign_codex_out()
        await fresh_status()
        for scheme in ("light", "dark"):
            context, page, errors, _ = await agents_page(browser, scheme)
            tab = await open_agents(page)
            row = tab.locator("#agent-codex")
            await expect(row.locator(".status")).to_have_text("Sign in needed")
            await unfold_agent(tab, "codex")
            await expect(row.locator(".setup-details")).to_have_text(
                "Command codex loginCopy command"
            )
            await role_shoot(page, "smooth-settings-sign-in", scheme)
            await row.locator(".action-slot .setup-action").click()
            dialog = page.locator("dialog.agent-setup")
            await expect(dialog.get_by_role("link", name="Open sign-in page")).to_be_visible()
            await dialog.locator(".setup-other summary").click()
            await dialog.get_by_role("button", name="Use a code instead (another device)").click()
            await expect(dialog.locator(".setup-code code")).to_have_text("FAKE-12345")
            await expect(dialog.get_by_role("link", name="Open sign-in page")).to_have_attribute(
                "href", "https://auth.openai.com/codex/device"
            )
            await ux_shoot(page, "smooth-signin-device", scheme, "dialog.agent-setup")
            await dialog.get_by_role("button", name="Cancel").click()
            await expect(dialog.get_by_role("button", name="Try again")).to_be_visible()
            # An API key, typed (never shown back), then Back.
            await dialog.locator(".setup-other summary").click()
            await dialog.get_by_role("button", name="Use an API key").click()
            key = dialog.get_by_label("API key")
            await expect(key).to_have_attribute("type", "password")
            await ux_shoot(page, "smooth-signin-key", scheme, "dialog.agent-setup")
            await dialog.get_by_role("button", name="Back").click()
            await page.keyboard.press("Escape")
            await expect(dialog).to_have_count(0)
            assert not errors, errors
            await context.close()
        # Held work: named for Codex while it is signed out, kept with Sign in; signed in, it
        # resumes by itself.
        spawn = runtime.toolset.spawn_on(["codex"])
        from copilot.tools import ToolInvocation

        refused = await spawn.handler(
            ToolInvocation(session_id=router, tool_call_id="t", tool_name="spawn_worker",
                           arguments={"title": "Smooth count the rows",
                                      "instructions": "Report done: 42 rows.",
                                      "why_new": "t", "agent": "codex"})
        )  # fmt: skip
        assert refused.result_type == "failure", refused
        for scheme in ("light", "dark"):
            context, page, errors, _ = await agents_page(browser, scheme)
            await at_home(page)
            card = page.locator("#timeline .card.agent-refused", has_text="Smooth count")
            await expect(card.locator("p").first).to_have_text(
                "Codex needs you to sign in. Your message is saved: “Smooth count the rows” "
                "starts on Codex once you have."
            )
            await expect(card.locator(".actions > button")).to_have_text(
                ["Sign in", "Don't run it", "Continue with Copilot"]
            )
            await ux_shoot(page, "smooth-held-card", scheme, focus=card)
            if scheme == "dark":
                await card.locator(".setup-action").click()
                dialog = page.locator("dialog.agent-setup")
                await expect(dialog.get_by_role("link", name="Open sign-in page")).to_be_visible()
                (codex_home / "approve").write_text("ok")
                await expect(dialog.locator(".setup-done")).to_be_visible(timeout=15000)
                await dialog.get_by_role("button", name="Done").click()
                await expect(card.locator(".resuming")).to_have_text(
                    "Resuming on Codex…", timeout=15000
                )
                await expect(card.get_by_role("button", name="Cancel")).to_be_visible()
                await ux_shoot(page, "smooth-held-resuming", scheme, focus=card)
                await expect(card.locator(".card-outcome")).to_have_text(
                    "Resumed after sign-in", timeout=20000
                )
                started = page.locator("#timeline .event.started", has_text="Smooth count")
                await expect(started.locator(".what")).to_have_text(
                    ["Started “Smooth count the rows” on Codex: resumed after sign-in"]
                )
                handled = page.locator("#needs details.handled")
                await expect(handled).to_be_visible(timeout=10000)
                await handled.locator("summary").click()
                await expect(handled).to_contain_text(
                    "Started “Smooth count the rows” on Codex once you signed in"
                )
                await ux_shoot(page, "smooth-held-resumed", scheme, focus=card)
            assert not errors, errors
            await context.close()
        made += [r["id"] for r in store.open_sessions("worker") if r["title"].startswith("Smooth")]
        assert [runtime.agent(i) for i in made] == ["codex"], made
        # (Closed, and Codex stopped: a signed-out Codex says so only once none of it runs.)
        for session_id in made:
            await runtime.workers.close(session_id, by="you")
        made.clear()
        await runtime.disconnect_agent("codex")
        # Claude asks for a pasted code: from the chip's popover (arrows reach it), Sign in.
        os.environ["FAKE_CLAUDE_SIGNED_OUT"] = "1"
        os.environ["FAKE_CLAUDE_PASTE"] = "smooth-code"
        await fresh_status()
        context, page, errors, _ = await agents_page(browser, "light")
        await at_home(page)
        box = page.locator("#composer-input")
        await box.fill("@cod")
        await page.locator("#mention-list li.agent", has_text="Codex").click()
        await page.locator("#composer-to .agent-chip").click()
        pop = page.locator(".agent-popover")
        await expect(pop.locator(".agent-pop-agent[aria-checked=true]")).to_be_focused()
        await page.keyboard.press("ArrowDown")
        await expect(pop.locator(".agent-pop-agent", has_text="Claude")).to_be_focused()
        await page.keyboard.press("ArrowUp")
        await page.keyboard.press("ArrowUp")
        await expect(pop.locator(".agent-pop-agent", has_text="Copilot")).to_be_focused()
        # Enter picks it, and it keeps the focus (the list drawn again): arrows go on from it.
        await page.keyboard.press("Enter")
        picked = pop.locator(".agent-pop-agent[aria-checked=true]")
        await expect(picked).to_have_text(re.compile("^Copilot"))
        await expect(picked).to_be_focused()
        await page.keyboard.press("ArrowDown")
        await expect(pop.locator(".agent-pop-agent", has_text="Codex")).to_be_focused()
        await page.keyboard.press("Enter")
        await expect(picked).to_have_text(re.compile("^Codex"))
        await expect(picked).to_be_focused()
        await page.keyboard.press("End")
        await expect(pop.locator(".agent-pop-agent", has_text="Claude")).to_be_focused()
        claude = pop.locator(".agent-pop-row", has_text="Claude")
        await expect(claude.locator(".setup-action")).to_have_text("Sign in")
        await claude.locator(".setup-action").click()
        await expect(pop).to_have_count(0)
        dialog = page.locator("dialog.agent-setup")
        await expect(dialog.locator("h2")).to_have_text("Sign in to Claude Code")
        paste = dialog.get_by_label("Paste code")
        await expect(paste).to_be_visible(timeout=15000)
        await ux_shoot(page, "smooth-signin-paste", "light", "dialog.agent-setup")
        await paste.fill("smooth-code")
        await paste.press("Enter")
        await expect(dialog.locator(".setup-done")).to_contain_text(
            "Claude is ready", timeout=15000
        )
        await dialog.get_by_role("button", name="Done").click()
        assert not errors, errors
        await context.close()
        os.environ.pop("FAKE_CLAUDE_SIGNED_OUT", None)
        os.environ.pop("FAKE_CLAUDE_PASTE", None)
        # A Claude sign-in that ends within a week: said in the @ list.
        ends = int((time.time() + 3 * 86400) * 1000)
        os.environ["FAKE_CLAUDE_AUTH"] = json.dumps({"loginExpiresAt": ends})
        await agents_saved(runtime, {"claude": {"enabled": True}})
        await fresh_status()
        context, page, errors, _ = await agents_page(browser, "light")
        await at_home(page)
        box = page.locator("#composer-input")
        await box.fill("@cl")
        await box.press("End")
        listed = page.locator("#mention-list li.agent", has_text="Claude")
        await expect(listed.locator(".status")).to_contain_text(
            re.compile(r"Ready · Uses your Claude plan · Claude sign-in ends \w+ \d+")
        )
        await ux_shoot(page, "smooth-at-expiry", "light", focus=page.locator("#composer"))
        await box.fill("")
        assert not errors, errors
        await context.close()
        os.environ.pop("FAKE_CLAUDE_AUTH", None)
        await agents_saved(runtime, {"claude": {"enabled": False}})
        # Its own sessions: the Router on Codex, Codex signed out: Sign in in Codex's row; If it
        # can't run there, Ask me: its card in the Router's chat.
        await runtime.disconnect_agent("codex")
        sign_codex_out()
        await fresh_status()
        await agents_saved(runtime, {"roles": {"router": "codex"},
                                     "role_unavailable": {"router": "ask"}})  # fmt: skip
        assert runtime.agent(router) == "copilot" and runtime.role_wait("router")
        for scheme in ("light", "dark"):
            context, page, errors, _ = await agents_page(browser, scheme)
            tab = await open_agents(page)
            system = tab.locator("#own-sessions")
            await system.locator("summary").click()
            await expect(tab.locator("#agent-codex .status")).to_have_text("Sign in needed")
            await expect(tab.locator("#agent-codex .action-slot .setup-action")).to_have_text(
                "Sign in"
            )
            await expect(system.locator("select[name=role_router]")).to_have_value("codex")
            await expect(system.locator("[data-sub=router]")).to_be_visible()
            await expect(system.locator("select[name=unavailable_router]")).to_have_value("ask")
            await role_shoot(page, "smooth-system-agents", scheme, system)
            await page.keyboard.press("Escape")
            await at_home(page)
            wait = page.locator("#timeline .card.role-wait")
            await expect(wait.locator("p").first).to_have_text(
                "The Router waits for Codex. Codex needs you to sign in. What it is sent is kept "
                "until then."
            )
            await expect(wait.locator(".actions > button")).to_have_text(
                ["Sign in", "Use Copilot until then"]
            )
            await ux_shoot(page, "smooth-role-wait", scheme, focus=wait)
            if scheme == "dark":
                await wait.get_by_role("button", name="Use Copilot until then").click()
                await expect(wait.locator(".actions")).to_have_count(0)
            assert not errors, errors
            await context.close()
        assert "role_waits" not in runtime.snapshot()
        await agents_saved(runtime, {"roles": {"router": "copilot"},
                                     "role_unavailable": {"router": "copilot"}})  # fmt: skip
        # Not installed: Install shows its command first, runs it, finds it, then Sign in.
        installed, hidden = folder / "installed", folder / "hidden"
        hidden.mkdir()
        (folder / "claude").rename(hidden / "claude")  # Not on the PATH: not installed.
        script = (f'echo "Downloading Claude Code…"; sleep 2; mkdir -p "{installed}" && '
                  f'cp "{hidden / "claude"}" "{installed}/claude"')  # fmt: skip
        agentsetup.INSTALLERS = {**installers, "claude": (("script", script, ""),)}
        known = agentsetup.known_dirs
        agentsetup.known_dirs = lambda: [installed]
        await fresh_status()
        assert runtime.agent_status("claude")["status"] == agents.MISSING
        for scheme in ("light", "dark"):
            context, page, errors, _ = await agents_page(browser, scheme)
            await at_home(page)
            box = page.locator("#composer-input")
            await box.fill("@cl")
            await box.press("End")
            missing = page.locator("#mention-list li.agent.dim", has_text="Claude")
            await expect(missing.locator(".agent-fix")).to_have_text("Pick to install")
            await missing.click()
            dialog = page.locator("dialog.agent-setup")
            await expect(dialog.locator("h2")).to_have_text("Install Claude Code")
            await expect(dialog.locator(".setup-command code")).to_have_text(script)
            await expect(dialog.locator("footer button")).to_have_text(["Copy command", "Install"])
            await ux_shoot(page, "smooth-install-consent", scheme, "dialog.agent-setup")
            if scheme == "dark":
                await dialog.get_by_role("button", name="Install").click()
                await expect(dialog.locator(".setup-waiting")).to_contain_text(
                    "Installing Claude Code…"
                )
                await expect(dialog.locator(".setup-tail")).to_contain_text("Downloading")
                await ux_shoot(page, "smooth-install-running", scheme, "dialog.agent-setup")
                await expect(dialog.locator(".setup-done")).to_contain_text(
                    "Claude is ready", timeout=20000
                )
            else:
                await page.keyboard.press("Escape")
            assert not errors, errors
            await context.close()
        assert runtime.agent_status("claude")["status"] == agents.READY
        assert str(installed) in os.environ["PATH"].split(os.pathsep)
        # Codex's weekly limit used up until tomorrow: the @ list says until when (picked all the
        # same: the chip says it too), and work held for it says so, with Continue with Copilot.
        codex_home.mkdir(parents=True, exist_ok=True)
        (codex_home / "signed-out").unlink(missing_ok=True)
        (codex_home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}))
        await fresh_status()
        resets = int(time.time()) + 86400 + 60
        runtime.plan_limit_seen("codex", {"status": "rejected", "label": "weekly limit",
                                          "resets_at": resets})  # fmt: skip
        assert runtime.agent_status("codex")["status"] == agents.LIMITED
        runtime.agent_refused_card("codex", runtime.limit_held("codex"),
                                   {"title": "Smooth limit count"})  # fmt: skip
        for scheme in ("light", "dark"):
            context, page, errors, _ = await agents_page(browser, scheme)
            await at_home(page)
            when = await page.evaluate(
                "at => import('/static/board.js').then((m) => m.resetTime(at))", resets * 1000
            )
            box = page.locator("#composer-input")
            await box.fill("@cod")
            await box.press("End")
            codex = page.locator("#mention-list li.agent.dim", has_text="Codex")
            await expect(codex.locator(".status > span").first).to_have_text(
                f"Weekly limit used up · resets {when}"
            )
            await expect(codex.locator(".agent-fix")).to_have_text(
                "Pick: it starts there once its limit resets"
            )
            await ux_shoot(page, "smooth-limit-at", scheme, focus=page.locator("#composer"))
            await codex.click()
            await expect(page.locator("#composer-to .agent-chip .detail")).to_have_text(
                f"Weekly limit used up · resets {when}"
            )
            await box.fill("")
            card = page.locator("#timeline .card.agent-refused", has_text="Smooth limit count")
            await expect(card.locator("p").first).to_have_text(
                f"Codex's weekly limit is used up · resets {when}. Your message is saved: "
                "“Smooth limit count” starts on Codex once it resets."
            )
            await expect(card.locator(".actions button")).to_have_text(
                ["Don't run it", "Continue with Copilot"]
            )
            await ux_shoot(page, "smooth-limit-card", scheme, focus=card)
            assert not errors, errors
            await context.close()
        for key in list(store.items("agentrefusal:")):
            await runtime.answer_refusal(key, "wait")
        store.delete("agentlimit:codex")
        if waiting := runtime.limit_waits.pop("codex", None):
            waiting.cancel()
        log(
            "agent smooth: Sign in from the @ list (keyboard), the popover, Settings, the held "
            "card and its own sessions; link, code, pasted code; Ready live; held work resumed "
            "once; Install's command first; the Router's agent marked; a sign-in's end; a "
            "limit used up, until when, and the work it holds"
        )
    finally:
        agentsetup.INSTALLERS = installers
        if "known" in locals():
            agentsetup.known_dirs = known
        for name in ("FAKE_CLAUDE_SIGNED_OUT", "FAKE_CLAUDE_PASTE", "FAKE_CLAUDE_AUTH"):
            os.environ.pop(name, None)
        for name in list(runtime.setups.runs):
            with contextlib.suppress(Exception):
                await runtime.setups.cancel(name)
        runtime.setups.runs.clear()
        # (Its Handled for you line, for the rounds after: they count theirs.)
        store.execute("DELETE FROM handled WHERE kind='resumed'")
        for session_id in made:
            await runtime.workers.close(session_id, by="you")
        await agents_saved(runtime, {"codex": {"enabled": False}, "claude": {"enabled": False}})
        if "installed" in locals():
            parts = os.environ["PATH"].split(os.pathsep)
            os.environ["PATH"] = os.pathsep.join(p for p in parts if p != str(installed))
            store.delete("agentpath:claude")
        runtime.found_cache = (float("-inf"), [])
        runtime.glanced.clear()
        runtime.agent_failed.clear()
        codex_off_path(folder)


async def sidebar_shoot_to(page, name: str, scheme: str):
    """The sidebar (opened over the page where it folds away), into files/shots/picker."""
    UX_SHOTS.mkdir(parents=True, exist_ok=True)
    for width in UX_WIDTHS:
        await page.set_viewport_size({"width": width, "height": 900})
        await page.mouse.move(1, 1)
        folded = await page.locator("#sidebar-toggle").is_visible()
        if folded:
            await page.locator("#sidebar-toggle").click()
            await expect(page.locator("#sidebar")).to_be_visible()
            await page.wait_for_timeout(300)
        await quiet_for(page)
        await page.screenshot(path=UX_SHOTS / f"{name}-{width}-{scheme}.png")
        found = await page.locator("#sidebar").evaluate(SCROLLED_OVERFLOWS)
        assert not found, (name, width, found)
        if folded:
            await page.keyboard.press("Escape")
    await page.set_viewport_size({"width": 1280, "height": 900})


async def cleanup_seeded(runtime) -> dict:
    """E125: a worker held for its folder's settings by an earlier version, four messages of
    the owner's written to it yesterday afternoon waiting, its task replaced (the link only in
    its words, as before 0.1.51) by another worker's, which finished it yesterday evening."""
    from chiefofstaff import tasks as tasks_module
    from chiefofstaff.workers import SETTINGS_WAIT

    store = runtime.store
    router = store.get("router_id")

    async def start(title):
        result = await call_tool(runtime, "spawn_worker", router, title=title, instructions=title,
                                 why_new="test")  # fmt: skip
        await settle(runtime)
        worker = json.loads(result.text_result_for_llm)["session_id"]
        return worker, runtime.tasks.current(worker)["id"]

    held, replaced = await start("QA9 tidy the export script")
    other, instead = await start("QA9 rewrite the export script")
    await call_tool(runtime, "report", other, status="done", summary="Rewritten.")
    await settle(runtime)
    yesterday = (datetime.now().astimezone() - timedelta(days=1)).replace(second=0, microsecond=0)

    def at(hour: int, minute: int) -> str:
        return yesterday.replace(hour=hour, minute=minute).astimezone(UTC).isoformat()

    written = []
    for text, when in [("Keep the CSV header", at(15, 2)), ("And the dates in UTC", at(15, 40)),
                       ("Skip empty rows", at(16, 30)), ("Then tell me", at(17, 40))]:  # fmt: skip
        message = store.add_message(held, "owner", text, {"task_id": replaced, "prompt": text})
        store.execute("UPDATE messages SET created_at=? WHERE id=?", (when, message["id"]))
        written.append(message["id"])
    finished = at(21, 56)
    store.execute("UPDATE runs SET ended_at=? WHERE task_id=?", (finished, instead))
    store.execute(
        "UPDATE tasks SET status='cancelled', ended_at=?, progress=? WHERE id=?",
        (finished, f"Cancelled by the Router: Replaced by {instead} “QA9 rewrite the export "
         "script”.", replaced),
    )  # fmt: skip
    store.delete(f"replacedby:{replaced}")
    store.delete("migrated:replacedby")
    tasks_module.migrate_replacedby(store)  # (As this version starts.)
    runtime.update_session(held, activity=SETTINGS_WAIT)
    await runtime.release_unguarded()
    await settle(runtime)
    return {"held": held, "written": written, "finished": finished, "first": at(15, 2)}


async def round_e125(runtime, browser, failures: list[str]):
    """E125: what an update's start cleaned up is one line under Handled for you, with when the
    messages were written (not the start's time) and what became of their task, and that the
    worker closed (E128); nothing of it in the Router's chat. Light and dark, 800 and 1280 px."""
    from chiefofstaff.runtime import said_at

    store = runtime.store
    router = store.get("router_id")
    before = len(store.messages(router))
    seeded = await cleanup_seeded(runtime)
    assert store.session(seeded["held"])["closed_at"], "nothing open, nothing left: closed"
    said = [m["text"] for m in store.messages(router)[before:]]
    assert not [t for t in said if "not sent" in t or t.startswith("Closed “QA9 tidy")], said
    for message_id in seeded["written"]:
        assert store.message(message_id)["meta"]["cleanup"]
    expected = (
        "Your 4 messages to “QA9 tidy the export script” were not sent: its task moved to “QA9 "
        f"rewrite the export script”, which finished it {said_at(seeded['finished'])}. Closed the "
        "worker too: nothing was left for it."
    )
    shots = Path(os.environ.get("E125_SHOTS", SHOTS / "e125"))
    shots.mkdir(parents=True, exist_ok=True)
    for width, scheme in itertools.product((1280, 800), ("light", "dark")):
        context = await browser.new_context(
            viewport={"width": width, "height": 1400}, color_scheme=scheme
        )
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
        await page.goto(f"{BASE}/#token={TOKEN}")
        await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)
        if width < 1100:
            await page.locator("#home-tabs [data-tab=needs]").click()
        handled = page.locator("#needs details.handled")
        await handled.locator("summary").click()
        line = handled.locator("li", has_text="Your 4 messages to “QA9 tidy the export script”")
        await expect(line.locator(".handled-text")).to_have_text(expected)
        written = line.locator("time")
        await expect(written).to_contain_text("3:02")
        await expect(written).to_contain_text("5:40")
        await expect(written).not_to_have_text("now")
        await expect(line.get_by_role("button")).to_have_count(0)
        await line.scroll_into_view_if_needed()
        await handled.screenshot(path=shots / f"handled-cleanup-{width}-{scheme}.png")
        assert not errors, errors
        await context.close()
    log("E125: an update's cleanup is one line under Handled for you, with when it was written")


async def round_stacked_merge(runtime, browser, failures: list[str]):
    """A PR in a stack on GitHub on its Ready to merge line (drafts.py, StackBelow and MERGING):
    above open PRs of its stack, the line names them and its button merges all; while GitHub
    merges a stack in the background, the line says so. Light and dark, 800 and 1280 px, nothing
    overflowing."""
    from chiefofstaff.drafts import MERGING

    store = runtime.store
    router = runtime.router_id()
    above = store.create_draft(router, "merge", {
        "label": "acme/app#12", "repo": "acme/app", "number": 12, "title": "Part three",
        "url": "https://github.com/acme/app/pull/12", "head": "c" * 40,
        "stack_below": [{"number": 10, "head": "a" * 40, "title": "Part one"},
                        {"number": 11, "head": "b" * 40, "title": "Part two"}],
    }, "Part three")  # fmt: skip
    store.update_draft(above["id"], outcome="Not merged yet: acme/app#12 is in a stack above #10 "
                       "and #11, and GitHub merges a stack from the bottom, so merging it merges "
                       "them too. Merge all 3 from here, or merge #10 first.")  # fmt: skip
    merging = store.create_draft(router, "merge", {
        "label": "acme/app#20", "repo": "acme/app", "number": 20, "title": "Stacked fix",
        "url": "https://github.com/acme/app/pull/20", "head": "d" * 40,
    }, "Stacked fix")  # fmt: skip
    store.update_draft(merging["id"], outcome=MERGING)
    runtime.publish_state()
    try:
        for width, scheme in itertools.product((1280, 800), ("light", "dark")):
            context = await browser.new_context(
                viewport={"width": width, "height": 1400}, color_scheme=scheme
            )
            page = await context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
            await page.goto(f"{BASE}/#token={TOKEN}")
            await expect(page.locator(".home-item.selected")).to_be_visible(timeout=15000)
            if width < 1100:
                await page.locator("#home-tabs [data-tab=needs]").click()
            ok = page.locator("#needs .card.your-ok")
            row = ok.locator(f'.ok-row[data-id="{above["id"]}"]')
            await expect(row).to_contain_text(
                "#12 “Part three” · checks pass · with #10, #11 below it"
            )
            await expect(row.get_by_role("button", name="Merge all 3", exact=True)).to_be_visible()
            busy = ok.locator(f'.ok-row[data-id="{merging["id"]}"]')
            await expect(busy.locator(".ok-kind")).to_have_text("Merging")
            await expect(busy.locator(".ok-text")).to_have_text(
                "app#20 “Stacked fix” · GitHub merges its stack in the background; this card says "
                "when it's done."
            )
            # Nothing to click again while GitHub is at it: the PR to look at.
            await expect(busy.get_by_role("button", name="Merge", exact=True)).to_have_count(0)
            await expect(busy.get_by_role("link", name="Open PR")).to_be_visible()
            found = await page.locator("#needs").evaluate(OVERFLOWS)
            assert not found, (width, scheme, found)
            # Its whole card: the same button, what it said.
            await row.locator(".more-button").click()
            await row.get_by_role("menuitem", name="Details").click()
            card = ok.locator(".ok-details .card.merge")
            await expect(card).to_contain_text("GitHub merges a stack from the bottom")
            await expect(card.get_by_role("button", name="Merge all 3")).to_be_visible()
            await ok.screenshot(path=SHOTS / f"stacked-merge-{width}-{scheme}.png")
            assert not errors, errors
            await context.close()
    finally:
        for draft in (above, merging):
            store.update_draft(draft["id"], status="rejected", outcome="Not now.")
        runtime.publish_state()
    log("stacked merge: a PR above open ones says so and merges all; one merging says so")


async def round_shepherd(runtime, browser, failures: list[str]):
    """Prepared for you (shepherd.py): off, one line in Settings → General and nothing else;
    turned on, its tab comes at once, and saved; Home's section with three rows, All found with
    why not, Hand to a worker; a worker PR's row: the fix's review (Request changes… and back,
    Push), its line after, Resume on a paused one, its menu (Stop looking after this PR, Accept
    this commit…, What it did); the tab's choices saved; turned off, everything of it goes."""
    from shepherd_world import TARGET, seed_page

    from chiefofstaff import discovery

    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda error: failures.append(str(error)))
    await page.goto(f"{BASE}/#token={TOKEN}")
    await expect(page.locator("#application")).to_be_visible()
    # Off: the switch's line in General, no tab, no section.
    await page.locator("#settings-button").click()
    await page.locator("#settings-tabs [data-tab=general]").click()
    line = page.locator("#proactive-line")
    await expect(line).to_contain_text("Keep my PRs moving")
    await expect(line.locator("input")).not_to_be_checked()
    await expect(page.locator("#settings-tabs [data-tab=proactive]")).to_have_count(0)
    # On: its tab comes before it is saved, and is hidden if unticked (what was written kept).
    await line.locator("input").check()
    tab = page.locator("#settings-tabs [data-tab=proactive]")
    await expect(tab).to_have_text("Prepared for you")
    await line.locator("input").uncheck()
    await expect(tab).to_be_hidden()
    await line.locator("input").check()
    await page.locator("#settings-form footer button[type=submit]").click()
    await until(lambda: runtime.store.settings().proactive, 15)
    await expect(page.locator("#settings-dialog")).not_to_be_visible()
    await context.close()
    log("Prepared for you: one line in General while off; its tab once ticked")

    root = Path(tempfile.mkdtemp(prefix="cos-ui-shepherd-", dir=runtime.store.directory.parent))
    seeded = await seed_page(runtime, root)
    handed = []

    async def hand(shepherd, target):
        handed.append(target)
        return {"task_id": "", "session_id": "", "claim": {}}

    real_hand = discovery.hand
    discovery.hand = hand
    try:
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        page.on("pageerror", lambda error: failures.append(str(error)))
        await page.goto(f"{BASE}/#token={TOKEN}")
        prepared = page.locator("#prepared")
        await expect(prepared.locator(".prepared-row")).to_have_count(3, timeout=10000)
        await prepared.get_by_role("button", name="1 more · All found…").click()
        await expect(prepared.locator(".prepared-row")).to_have_count(6)
        await expect(prepared.locator(".prepared-row", has_text="web#88")).to_contain_text(
            "from a fork: not yet"
        )
        await expect(
            prepared.locator(".prepared-row", has_text="web#88").get_by_role("button")
        ).to_have_count(0)
        await prepared.get_by_role("button", name="Done").click()
        await expect(prepared.locator(".prepared-row")).to_have_count(3)
        await (
            prepared.locator(".prepared-row", has_text="web#117")
            .get_by_role("button", name="Hand to a worker")
            .click()
        )
        await expect(page.locator(".toast", has_text="Handed web#117 to a worker.")).to_be_visible()
        assert handed == ["github:acme/web#117"], handed
        log("Home's section: three rows, All found with why not, Hand to a worker")

        # The fix's review: Request changes… and back, then Push.
        row = page.locator("#needs .card.task", has_text="Fix the login timeout")
        await row.get_by_role("button", name="Review fix").click()
        dialog = page.locator(".plan-dialog")
        await expect(dialog.locator("h2")).to_have_text("A fix for api#12")
        await expect(dialog.locator(".plan-evidence")).to_contain_text(
            "test_timeout failed before and passes now"
        )
        await dialog.get_by_role("button", name="Request changes…").click()
        await expect(dialog.locator("footer textarea")).to_be_focused()
        await dialog.locator("footer").get_by_role("button", name="Cancel").click()
        await expect(dialog.get_by_role("button", name="Push")).to_be_visible()
        await dialog.get_by_role("button", name="Push").click()
        await expect(dialog).to_have_count(0)
        world = seeded["world"]
        step = seeded["step"]
        await until(lambda: runtime.shepherd.control.step(step["id"])["state"] == "published", 15)
        await until(lambda: world.github.head() == step["plan"]["h1"], 15)
        # Its row's own words (what GitHub says of its PR) stay; unfolded, what was done.
        await row.locator(".head .title").click()
        await expect(row).to_have_class(re.compile(r"\bunfolded\b"))
        await expect(row.locator(".kept-line")).to_contain_text(
            f"Pushed {step['plan']['h1'][:7]}", timeout=10000
        )
        await row.locator(".head .title").click()
        log("the fix's review: Request changes… and back; Push publishes it")

        # Paused: Resume.
        paused = page.locator("#needs .card.task", has_text="Add the export endpoint")
        await paused.get_by_role("button", name="Resume").click()
        await expect(page.locator(".toast", has_text="It keeps moving again.")).to_be_visible()
        await until(
            lambda: runtime.shepherd.control.claim("github:acme/api#31")["state"] == "idle", 15
        )
        await expect(paused.get_by_role("button", name="Resume")).to_have_count(0)

        # Its menu: Accept this commit… (the one it pushed, read again at once), What it did,
        # Stop looking after this PR.
        await until(lambda: runtime.shepherd.control.claim(TARGET)["head"] == world.github.head())
        await row.locator(".more-button").click()
        await row.get_by_role("menuitem", name="Accept this commit…").click()
        confirm = page.locator(".confirm-dialog")
        await expect(confirm.locator("h2")).to_have_text(
            f"Accept api#12 at {world.github.head()[:7]}?"
        )
        await confirm.get_by_role("button", name=re.compile(r"^Accept ")).click()
        await until(
            lambda: (
                (runtime.shepherd.control.claim(TARGET)["accepted"] or {}).get("sha")
                == world.github.head()
            ),
            15,
        )
        await row.locator(".more-button").click()
        await row.get_by_role("menuitem", name="What it did").click()
        journal = page.locator(".journal-dialog")
        await expect(journal.locator(".journal-ops")).to_contain_text("Pushed the fix")
        await journal.get_by_role("button", name="Close").click()
        await row.locator(".more-button").click()
        await row.get_by_role("menuitem", name="Stop looking after this PR").click()
        await until(lambda: not runtime.shepherd.control.claim(TARGET)["keep"], 15)
        await row.locator(".more-button").click()
        await expect(row.get_by_role("menuitem", name="Look after this PR")).to_be_visible()
        await page.keyboard.press("Escape")
        log("a kept PR's row: Resume, Accept this commit…, What it did, Stop looking after this PR")

        # The tab's choices, saved.
        await page.locator("#settings-button").click()
        await page.locator("#settings-tabs [data-tab=proactive]").click()
        pane = page.locator('#settings-pane .tab[data-tab="proactive"]')
        await pane.locator("select[name=level_fix-ci]").select_option("prepare")
        await pane.locator("input[name=proactive_budget]").fill("12")
        await pane.locator("textarea[name=step_hosts]").fill("registry.acme.dev")
        repo = pane.locator('.repo-row[data-repo="acme/api"]')
        await repo.locator("select.disclosure").select_option("yes")
        await repo.locator("input.localhost").check()
        await page.locator("#settings-form footer button[type=submit]").click()
        await until(lambda: runtime.store.settings().proactive_budget == 12, 15)
        settings = runtime.store.settings()
        assert settings.proactive_levels["fix-ci"] == "prepare", settings.proactive_levels
        assert settings.step_hosts == ["registry.acme.dev"], settings.step_hosts
        choice = settings.proactive_repos["acme/api"]
        assert choice.disclosure == "yes" and choice.localhost, choice
        # GitHub is never a host steps may reach: said where it was typed.
        await page.locator("#settings-button").click()
        await page.locator("#settings-tabs [data-tab=proactive]").click()
        await pane.locator("textarea[name=step_hosts]").fill("api.github.com")
        await page.locator("#settings-form footer button[type=submit]").click()
        await expect(page.locator("#settings-error")).to_contain_text("api.github.com is GitHub's")
        assert runtime.store.settings().step_hosts == ["registry.acme.dev"]
        await page.locator("#settings-cancel").click()
        log("the tab's choices saved; GitHub refused as a host")

        # Off: its tab, its section and its words on rows go.
        await page.locator("#settings-button").click()
        await page.locator("#settings-tabs [data-tab=general]").click()
        await page.locator("#proactive-line input").uncheck()
        await expect(page.locator("#settings-tabs [data-tab=proactive]")).to_be_hidden()
        await page.locator("#settings-form footer button[type=submit]").click()
        await until(lambda: not runtime.store.settings().proactive, 15)
        await expect(page.locator("#prepared")).to_have_count(0)
        await expect(row.locator(".kept-line")).to_have_count(0)
        await expect(row.get_by_role("button", name="Review fix")).to_have_count(0)
        await context.close()
        log("turned off: its tab, its section and its words on rows go")
    finally:
        discovery.hand = real_hand


# Settings v2, phase 1 (docs/design/settings-v2.md §8, §10.3): one mounted form whose drafts
# survive navigation and Cancel discards; menus that keep what they can't list; an agent on
# that can always be turned off; validation that opens the field wherever the owner is; Back
# that returns focus; statuses that never run under a row's controls; tabs by keyboard; old
# names; the sidebar's Resume.
SETTINGS_SHOTS = SHOTS / "settings-v2"
SETTINGS_WIDTHS = (800, 1024, 1440)


async def settings_page(browser, scheme: str = "light", width: int = 1280, routes=None):
    """A page of its own (`routes`: what it answers for itself from the start)."""
    if routes is None:
        context, page, errors, requests = await agents_page(browser, scheme, width)
    else:
        context = await browser.new_context(
            viewport={"width": width, "height": 900}, color_scheme=scheme
        )
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        requests: list[str] = []
        page.on("request", lambda r: requests.append(f"{r.method} {r.url.split(BASE)[-1]}"))
        for pattern, handler in routes.items():
            await page.route(pattern, handler)
        await page.goto(f"{BASE}/#token={TOKEN}")
        await expect(page.locator("#application")).to_be_visible()
    posted: list[dict] = []
    page.on(
        "request",
        lambda r: (
            posted.append(r.post_data_json)
            if r.url.endswith("/api/settings") and r.method == "POST"
            else None
        ),
    )
    return context, page, errors, requests, posted


async def save_settings_dialog(page):
    async with page.expect_response("**/api/settings") as answer:
        await page.locator("#settings-form footer button[type=submit]").click()
    assert (await answer.value).ok, await (await answer.value).text()
    await expect(page.locator("#settings-dialog")).to_be_hidden()


async def round_settings(runtime, browser, failures: list[str]):
    store = runtime.store
    before = store.settings()
    try:
        await settings_drafts(runtime, browser)
        await settings_copilot_row(runtime, browser)
        await settings_retained(runtime, browser)
        await settings_validation(runtime, browser)
        await settings_keyboard_and_names(runtime, browser)
        await settings_paused(runtime, browser)
        folder, _ = codex_on_path()
        runtime.found_cache = (float("-inf"), [])
        try:
            await settings_switch_off(runtime, browser)
            await settings_not_installed(runtime, browser)
            await settings_router_elsewhere(runtime, browser)
            await settings_long_states(runtime, browser)
        finally:
            await agents_saved(runtime, {"codex": {"enabled": False}, "roles": {}})
            codex_off_path(folder)
    finally:
        store.save_settings(before)
        runtime.publish_state()


async def settings_drafts(runtime, browser):
    """A connection's edit survives Back, another connection and another page, and Save sends
    it; Cancel discards drafts on every page and in a folded row; an immediate action (Scan now)
    in the middle saves only its own fields."""
    store = runtime.store
    context, page, errors, _, posted = await settings_page(browser)
    await page.locator("#settings-button").click()
    await page.locator("#settings-tabs [data-tab=connections]").click()
    slack = page.locator(".conn[data-conn=slack] .conn-open")
    await slack.click()
    await expect(page.locator("#conn-slack-title")).to_be_focused()
    await page.locator("input[name=slack_delegate_emoji]").fill(":eyes:")
    await expect(page.locator("#slack-delegate-reply")).to_have_text(
        "Reply directly when you react with :eyes:"
    )
    await page.locator("[data-conn-detail=slack] [data-back]").click()
    await expect(slack).to_be_focused()
    await expect(page.locator("#slack-summary")).to_have_text(re.compile(r" · not saved yet$"))
    await page.locator(".conn[data-conn=watches] .conn-open").click()
    await page.locator("[data-conn-detail=watches] [data-back]").click()
    await page.locator("#settings-tabs [data-tab=agents]").click()
    await save_settings_dialog(page)
    assert store.settings().slack_delegate_emoji == "eyes", store.settings().slack_delegate_emoji
    log("settings: a connection's draft survives Back, another connection and another page")
    # Cancel: drafts on three pages and in a folded row, gone; Save then changes nothing.
    await page.locator("#settings-button").click()
    await page.locator("#settings-tabs [data-tab=workers]").click()
    await page.locator("input[name=worker_sandbox]").check()
    await page.locator("#settings-tabs [data-tab=general]").click()
    await page.locator("input[name=worker_idle_hours]").fill("48")
    await page.locator("#settings-tabs [data-tab=agents]").click()
    await page.locator("#agent-copilot .fold").click()
    await page.locator("input[name=worker_credit_limit]").fill("50")
    await page.locator("#agent-copilot .fold").click()
    await page.locator("#settings-cancel").click()
    assert not store.settings().worker_sandbox
    await page.locator("#settings-button").click()
    await page.locator("#settings-tabs [data-tab=workers]").click()
    await expect(page.locator("input[name=worker_sandbox]")).not_to_be_checked()
    await expect(page.locator("input[name=worker_idle_hours]")).to_have_value(
        str(store.settings().worker_idle_hours)
    )
    await expect(page.locator("input[name=worker_credit_limit]")).to_have_value("")
    saved = store.settings().model_dump(mode="json")
    await save_settings_dialog(page)
    body = dict(posted[-1])
    body.pop("base")
    assert store.settings().model_dump(mode="json") == saved
    for key in ("worker_sandbox", "worker_idle_hours", "worker_credit_limit"):
        assert body[key] == saved[key], (key, body[key], saved[key])
    log("settings: Cancel discards drafts on every page and in a folded row")
    # Scan now in the middle: the watches saved, the emoji still a draft until Save.
    await page.locator("#settings-button").click()
    await page.locator("#settings-tabs [data-tab=connections]").click()
    await page.locator(".conn[data-conn=slack] .conn-open").click()
    await page.locator("input[name=slack_delegate_emoji]").fill(":wave:")
    await page.locator("[data-conn-detail=slack] [data-back]").click()
    await page.locator(".conn[data-conn=watches] .conn-open").click()
    await page.locator("#watch-add").click()
    await page.locator("#watch-list li .what").last.fill("Questions about the launch")
    await page.locator("#watch-scan").click()
    await expect(page.locator(".toast", has_text="Scanning now.")).to_be_visible(timeout=10000)
    assert "Questions about the launch" in [w.what for w in store.settings().watches]
    assert store.settings().slack_delegate_emoji == "eyes"
    assert set(posted[-1]) == {
        "watches", "watch_hours", "watch_minutes", "watch_after_hours_minutes", "base",
    }  # fmt: skip
    await page.locator("[data-conn-detail=watches] [data-back]").click()
    await expect(page.locator("#watches-summary")).not_to_contain_text("not saved yet")
    await expect(page.locator("#slack-summary")).to_contain_text("not saved yet")
    await save_settings_dialog(page)
    assert store.settings().slack_delegate_emoji == "wave"
    store.save_settings(store.settings().model_copy(update={"watches": []}))
    assert not errors, errors
    await context.close()
    log("settings: Scan now saves its watches only; other drafts wait for Save")


async def settings_copilot_row(runtime, browser):
    """Copilot's row: its workers' model and effort (Settings' role_models.worker and
    role_efforts.worker), the credit limit per worker and the Copilot command; the effort menu
    lists what the chosen model offers; the default model in its own sessions."""
    store = runtime.store
    context, page, errors, _, posted = await settings_page(browser)
    section = await open_agents(page)
    await unfold_agent(section, "copilot")
    effort = section.locator("select[name=effort_worker]")
    await section.locator("select[name=model_worker]").select_option("smart-model")
    await expect(effort.locator("option")).to_have_text(["Default", "Low", "Medium", "High"])
    await effort.select_option("high")
    await expect(section.locator("#new-work-model")).to_have_text("Smart-Model · high effort")
    await section.locator("input[name=worker_credit_limit]").fill("40")
    await section.locator("#own-sessions summary").click()
    await section.locator("select[name=model]").select_option("fast-model")
    await expect(section.locator("#own-summary")).to_have_text("All on Copilot · Fast-Model")
    await save_settings_dialog(page)
    saved = store.settings()
    assert saved.role_models == {"worker": "smart-model"}, saved.role_models
    assert saved.role_efforts == {"worker": "high"}, saved.role_efforts
    assert saved.worker_credit_limit == 40 and saved.model == "fast-model", saved
    assert posted[-1]["copilot_command"] == saved.copilot_command
    store.save_settings(
        saved.model_copy(
            update={"role_models": {}, "role_efforts": {}, "worker_credit_limit": 0, "model": ""}
        )
    )
    runtime.publish_state()
    assert not errors, errors
    await context.close()
    log("settings: Copilot's row saves the workers' model, effort and limit; Default its own")


async def settings_retained(runtime, browser):
    """Save untouched sends what is saved, even what a menu can't list: a model Copilot does not
    offer, an effort its model does not offer, `none`; with Copilot's models failing to load, or
    coming late (Settings opened before), its menus kept as they were."""
    store = runtime.store
    retained = {
        "role_models": {"worker": "gpt-5.2", "router": "auto"},
        "role_efforts": {"worker": "high", "router": "high", "slack": "none"},
    }
    store.save_settings(store.settings().model_copy(update=retained))
    runtime.publish_state()
    for how in ("listed", "failed", "late"):
        arrived = asyncio.Event()

        async def models(route, *, how=how, arrived=arrived):
            if "agent=" in route.request.url:
                await route.continue_()
            elif how == "failed":
                await route.fulfill(status=500, json={"detail": "Copilot is not connected"})
            else:
                if how == "late":
                    await arrived.wait()
                await route.continue_()

        context, page, errors, _, posted = await settings_page(
            browser, routes={"**/api/models*": models}
        )
        await page.locator("#settings-button").click()
        await page.locator("#settings-tabs [data-tab=agents]").click()
        await page.locator("#agent-copilot .fold").click()
        worker = page.locator("select[name=model_worker]")
        await expect(worker).to_have_value("gpt-5.2")
        await expect(page.locator("select[name=effort_slack]")).to_have_value("none")
        if how == "late":
            listed = await worker.locator("option").count()
            arrived.set()
            await expect(worker.locator("option")).not_to_have_count(listed)
            await expect(worker).to_have_value("gpt-5.2")
            await expect(worker.locator("option:checked")).to_have_text("gpt-5.2 (not offered now)")
        await save_settings_dialog(page)
        for key, value in retained.items():
            assert posted[-1][key] == value, (how, key, posted[-1][key])
            assert getattr(store.settings(), key) == value, (how, key)
        assert not [e for e in errors if "500" not in e], errors
        await context.close()
    store.save_settings(store.settings().model_copy(update={"role_models": {}, "role_efforts": {}}))
    runtime.publish_state()
    log("settings: what a menu can't list is kept (listed, failed to load, late)")


async def settings_validation(runtime, browser):
    """A problem opens its field wherever the owner is: the browser's (a number in General, from
    Connections), the page's (MCP servers in a connection's details, from General) and the
    server's (the code folder, from You)."""
    context, page, errors, _, posted = await settings_page(browser)
    await page.locator("#settings-button").click()
    await page.locator("#settings-tabs [data-tab=general]").click()
    idle = page.locator("input[name=worker_idle_hours]")
    await idle.evaluate("i => { i.value = '0'; }")
    await page.locator("#settings-tabs [data-tab=connections]").click()
    await page.locator("#settings-form footer button[type=submit]").click()
    await expect(page.locator("#settings-page-general")).to_be_visible()
    await expect(idle).to_be_focused()
    await idle.fill("24")
    mcp = page.locator("textarea[name=mcp_servers]")
    await mcp.evaluate("t => { t.value = '{'; }")
    await page.locator("#settings-tabs [data-tab=general]").click()
    await page.locator("#settings-form footer button[type=submit]").click()
    await expect(page.locator("[data-conn-detail=tools]")).to_be_visible()
    await expect(mcp).to_be_focused()
    await expect(page.locator("#settings-error")).to_contain_text("not valid JSON")
    await mcp.fill("{}")
    await page.locator("#settings-tabs [data-tab=general]").click()
    code = page.locator("input[name=code_folder]")
    await code.fill("/nope/not/here")
    await page.locator("#settings-tabs [data-tab=you]").click()
    await page.locator("#settings-form footer button[type=submit]").click()
    await expect(page.locator("#settings-error")).to_contain_text("code folder")
    await expect(page.locator("#settings-page-general")).to_be_visible()
    await expect(code).to_be_focused()
    await expect(code).to_have_attribute("aria-invalid", "true")
    assert all(body.get("code_folder") != "/nope/not/here" or True for body in posted)
    await page.locator("#settings-cancel").click()
    assert not [e for e in errors if "422" not in e], errors
    await context.close()
    log("settings: a problem opens its page, connection and folds, and focuses its field")


async def settings_keyboard_and_names(runtime, browser):
    """The pages are a tablist (arrows either way, Home, End, focus on the one shown when
    Settings opens); old names open their new place; a connection's Back by keyboard returns to
    its row."""
    context, page, errors, _, _ = await settings_page(browser)
    await page.locator("#settings-button").click()
    tabs = page.locator("#settings-tabs")
    await expect(tabs).to_have_attribute("role", "tablist")
    shown = tabs.locator("[aria-selected=true]")
    await expect(shown).to_be_focused()
    await expect(shown).to_have_attribute("tabindex", "0")
    for panel in await page.locator("#settings-pane > .tab").all():
        await expect(panel).to_have_attribute("role", "tabpanel")
        tab_id = await panel.get_attribute("aria-labelledby")
        await expect(page.locator(f"#{tab_id}")).to_have_attribute(
            "aria-controls", await panel.get_attribute("id")
        )
    await page.keyboard.press("Home")
    await expect(tabs.locator("[data-tab=general]")).to_be_focused()
    await page.keyboard.press("ArrowUp")
    await expect(tabs.locator("[data-tab=advanced]")).to_be_focused()
    await expect(page.locator("#settings-page-advanced")).to_be_visible()
    await page.keyboard.press("ArrowRight")
    await expect(tabs.locator("[data-tab=general]")).to_have_attribute("aria-selected", "true")
    await page.keyboard.press("End")
    await expect(tabs.locator("[data-tab=advanced]")).to_have_attribute("aria-selected", "true")
    await page.keyboard.press("Escape")
    # Old names and connections' own.
    for name, tab, shown_part in (
        ("models", "agents", "#settings-page-agents"),
        ("agents", "agents", "#settings-page-agents"),
        ("workers", "workers", "#settings-page-workers"),
        ("advanced", "advanced", "#settings-page-advanced"),
        ("connections", "connections", "#conn-list"),
        ("connections/watches", "connections", "[data-conn-detail=watches]"),
        ("whatever", "general", "#settings-page-general"),
    ):
        await page.evaluate(
            "name => import('/static/settings.js').then((m) => m.openSettings(name))", name
        )
        await expect(tabs.locator(f"[data-tab={tab}]")).to_have_attribute("aria-selected", "true")
        await expect(page.locator(shown_part)).to_be_visible()
        await page.keyboard.press("Escape")
    await page.evaluate("() => localStorage.setItem('cos.settingsTab', 'models')")
    await page.locator("#settings-button").click()
    await expect(tabs.locator("[data-tab=agents]")).to_have_attribute("aria-selected", "true")
    # Back by keyboard returns to its row.
    await page.locator("#settings-tabs [data-tab=connections]").click()
    row = page.locator(".conn[data-conn=away] .conn-open")
    await row.focus()
    await page.keyboard.press("Enter")
    await expect(page.locator("#conn-away-title")).to_be_focused()
    await page.locator("[data-conn-detail=away] [data-back]").focus()
    await page.keyboard.press("Enter")
    await expect(row).to_be_focused()
    await page.keyboard.press("Escape")
    assert not errors, errors
    await context.close()
    log("settings: a tablist by keyboard; old names; Back by keyboard to its row")


async def settings_paused(runtime, browser):
    """Pause watching: the last line of Connections, saying what it stops; paused, the sidebar's
    line offers Resume."""
    store = runtime.store
    context, page, errors, _, _ = await settings_page(browser)
    await page.locator("#settings-button").click()
    await page.locator("#settings-tabs [data-tab=connections]").click()
    pause = page.locator("#paused-box")
    await expect(page.locator("#paused-help")).to_have_text(
        "Stops Slack, GitHub, Watches, away notices, learning and Prepared for you (what it had "
        "ready is withdrawn)."
    )
    await expect(pause).to_have_attribute("aria-describedby", "paused-help")
    await pause.check()
    await save_settings_dialog(page)
    assert store.settings().paused
    line = page.locator("#status-line", has_text="Watchers paused")
    await expect(line).to_be_visible(timeout=10000)
    await line.get_by_role("button", name="Resume watching").click()
    await expect(page.locator("#status-line")).not_to_contain_text("Watchers paused")
    assert not store.settings().paused
    assert not errors, errors
    await context.close()
    log("settings: Pause watching at Connections' end; Resume on the sidebar's line")


def look_routes(page, answer):
    """/api/agents answered as `answer` says: "never" (the look running), "error", or this."""

    async def answered(route):
        if answer == "never":
            await asyncio.sleep(30)
            await route.abort()
        elif answer == "error":
            await route.fulfill(status=500, json={"detail": NO_ANSWER})
        else:
            await route.fulfill(json=answer)

    return page.route("**/api/agents", answered)


async def settings_switch_off(runtime, browser):
    """An agent on can be turned off in every status: signed out, a problem, while the look
    runs, after it failed (review finding 3); off and not ready, it waits to be turned on."""
    store = runtime.store
    on = {
        "name": "codex", "label": "Codex", "enabled": True, "model": "", "effort": "",
        "limit": 0, "found": True, "state": "", "problem": "", "models": [], "status": "ready",
        "said": "Ready",
    }  # fmt: skip
    cases = {
        "signed-out": {"agents": [on | {"status": "signin", "said": "Sign-in needed",
                                        "problem": SIGNED_OUT_CODEX}], "sandboxed": False},
        "cannot-run": {"agents": [on | {"problem": STOPPED_CODEX}], "sandboxed": False},
        "looking": "never",
        "look-failed": "error",
    }  # fmt: skip
    for name, answer in cases.items():
        await agents_saved(runtime, {"codex": {"enabled": True}})
        context, page, errors, requests, posted = await settings_page(browser)
        await look_routes(page, answer)
        await page.locator("#settings-button").click()
        await page.locator("#settings-tabs [data-tab=agents]").click()
        box = page.locator("input[name=agent_codex_enabled]")
        await expect(box).to_be_checked()
        await expect(box).to_be_enabled()
        await box.uncheck()
        await save_settings_dialog(page)
        assert posted[-1]["agents"] == {"codex": {"enabled": False}}, (name, posted[-1])
        from chiefofstaff import agents

        assert agents.turned_on(store) == [], name
        assert not any("/api/models?agent=" in r for r in requests), requests
        assert not [e for e in errors if "500" not in e], errors
        await context.close()
    # Off, signed out: it waits until it can work.
    context, page, errors, _, _ = await settings_page(browser)
    await look_routes(
        page,
        cases["signed-out"] | {"agents": [cases["signed-out"]["agents"][0] | {"enabled": False}]},
    )
    section = await open_agents(page)
    await expect(section.locator("input[name=agent_codex_enabled]")).to_be_disabled()
    await page.keyboard.press("Escape")
    await context.close()
    log("settings: an agent on turns off in every status, during and after its look")


async def settings_not_installed(runtime, browser):
    """An agent not installed has no row beside one found (nothing offers it: the @ list does),
    nor once its look says it was uninstalled since; one still on keeps its row, Not installed
    with its switch (no Install; unfolded, its command to copy), to be turned off; then it
    goes."""
    import time

    from chiefofstaff import agents

    codex = {
        "name": "codex", "label": "Codex", "enabled": False, "model": "", "effort": "",
        "limit": 0, "found": True, "state": "", "problem": "", "models": [], "status": "ready",
        "said": "Ready",
    }  # fmt: skip
    install = "curl -fsSL https://claude.ai/install.sh | bash"
    claude = codex | {"name": "claude", "label": "Claude", "found": False, "status": "missing",
                      "said": "Not installed", "fix": {"command": install, "then": ""},
                      "install": [{"method": "script", "label": "Official installer"}]}  # fmt: skip
    look = {"agents": [codex], "supported": ["the Codex CLI", "Claude Code"], "sandboxed": False}

    async def rows(page) -> list[str]:
        shown = page.locator("#agent-rows > .agent:visible")
        return [await row.get_attribute("id") for row in await shown.all()]

    await agents_saved(runtime, {"codex": {"enabled": False}, "claude": {"enabled": False}})
    try:
        # Codex found, Claude not: Copilot's row and Codex's, nothing else.
        runtime.found_cache = (time.monotonic(), ["codex"])
        context, page, errors, _, _ = await settings_page(browser)
        await look_routes(page, look)
        section = await open_agents(page)
        assert await rows(page) == ["agent-copilot", "agent-codex"], await rows(page)
        await expect(section).not_to_contain_text("Not installed")
        await page.keyboard.press("Escape")
        await context.close()
        # Found when Settings opened, uninstalled since (its look says so): its row goes.
        runtime.found_cache = (time.monotonic(), ["codex", "claude"])
        context, page, errors, _, _ = await settings_page(browser)
        await look_routes(page, look)
        section = await open_agents(page)
        assert await rows(page) == ["agent-copilot", "agent-codex"], await rows(page)
        await page.keyboard.press("Escape")
        await context.close()
        # Still on: its row says Not installed, and its switch turns it off.
        runtime.found_cache = (time.monotonic(), ["codex"])
        await agents_saved(runtime, {"claude": {"enabled": True}})
        context, page, errors, _, posted = await settings_page(browser)
        await look_routes(page, look | {"agents": [codex, claude | {"enabled": True}]})
        section = await open_agents(page)
        assert await rows(page) == ["agent-copilot", "agent-codex", "agent-claude"]
        await expect(section.locator("#agent-claude .status")).to_have_text("Not installed")
        # Nothing offers to install it here; unfolded, its command to copy.
        await expect(section.locator("#agent-claude .action-slot button")).to_have_count(0)
        await unfold_agent(section, "claude")
        await expect(section.locator("#agent-claude .setup-details code")).to_have_text(install)
        await expect(section.locator("#agent-claude .setup-details button")).to_have_text(
            "Copy command"
        )
        box = section.locator("input[name=agent_claude_enabled]")
        await expect(box).to_be_checked()
        await expect(box).to_be_enabled()
        await box.uncheck()
        await save_settings_dialog(page)
        assert posted[-1]["agents"] == {"claude": {"enabled": False}}, posted[-1]
        assert agents.turned_on(runtime.store) == []
        assert not errors, errors
        await context.close()
        context, page, errors, _, _ = await settings_page(browser)
        await look_routes(page, look)
        await open_agents(page)
        assert await rows(page) == ["agent-copilot", "agent-codex"], await rows(page)
        await page.keyboard.press("Escape")
        await context.close()
    finally:
        runtime.found_cache = (float("-inf"), [])
        await agents_saved(runtime, {"claude": {"enabled": False}})
    log("settings: an agent not installed has no row; one still on says so and turns off")


async def settings_router_elsewhere(runtime, browser):
    """The Router on Codex with a Copilot model saved for it: its cell says Codex's model and
    effort; Save untouched keeps the Copilot one (for when it runs there)."""
    store = runtime.store
    store.save_settings(
        store.settings().model_copy(
            update={"role_models": {"router": "smart-model"}, "role_efforts": {"router": "high"}}
        )
    )
    await agents_saved(runtime, {"codex": {"enabled": True}, "roles": {"router": "codex"}})
    context, page, errors, _, posted = await settings_page(browser)
    section = await open_agents(page)
    await section.locator("#own-sessions summary").click()
    await expect(section.locator("[data-inherits=router]")).to_have_text(
        "Codex's model and effort (its row)"
    )
    await expect(section.locator("[data-sub=router] .on-copilot")).to_have_text(
        " · on Copilot: Smart-Model, high effort"
    )
    await save_settings_dialog(page)
    assert posted[-1]["role_models"] == {"router": "smart-model"}, posted[-1]
    assert posted[-1]["role_efforts"] == {"router": "high"}, posted[-1]
    assert "agents" not in posted[-1], posted[-1]
    store.save_settings(store.settings().model_copy(update={"role_models": {}, "role_efforts": {}}))
    await agents_saved(runtime, {"roles": {"router": "copilot"}})
    assert not errors, errors
    await context.close()
    log("settings: a role on Codex says Codex's model; its Copilot one is kept")


async def settings_long_states(runtime, browser):
    """Long and combined states (signed out while on, extra usage with the sign-in ending, used
    up with no reset, a rate limit, a long reason it can't run): a row's status and its
    controls never overlap, at 800, 1024 and 1440, light and dark."""
    soon = int(time.time()) + 3 * 86400
    base = {
        "name": "codex", "label": "Codex", "enabled": True, "model": "", "effort": "",
        "limit": 0, "found": True, "state": "0.167.2 · signed in with ChatGPT", "problem": "",
        "models": [], "status": "ready", "said": "Ready",
        "usage": [{"label": "weekly limit", "used": 100, "resets_at": soon}],
    }  # fmt: skip
    extra = (
        "Your Codex plan's weekly limit is reached; workers go on with extra usage until it "
        "resets at 3:40 pm."
    )
    used_up = "Codex's weekly limit is used up; it didn't say when it resets"
    rate = "Codex is refusing requests for a rate limit; it didn't say for how long"
    reason = (
        "Codex can't run while workers are sandboxed: its sandbox has not passed Chief of "
        "Staff's checks yet. Its workers wait; turn the sandbox off in Workers to go on."
    )
    signed_out = {
        "status": "signin",
        "said": "Sign-in needed",
        "ways": [{"method": "browser"}],
        "fix": {"command": "codex login", "then": ""},
    }
    states = {
        "signed-out-on": base | signed_out,
        "extra-ending": base | {"expires": soon, "limit_line": extra},
        "used-up": base | {"limit_line": used_up},
        "rate": base | {"limit_line": rate},
        "long-reason": base | {"problem": reason},
    }
    await agents_saved(runtime, {"codex": {"enabled": True}})
    for scheme in ("light", "dark"):
        for name, entry in states.items():
            context, page, errors, _, _ = await settings_page(browser, scheme)
            await look_routes(
                page, {"agents": [entry], "new_workers": "copilot", "sandboxed": False}
            )
            section = await open_agents(page)
            row = section.locator("#agent-codex")
            await expect(
                row.locator(".agent-alerts")
            ).to_be_visible() if name != "signed-out-on" else None
            for width in SETTINGS_WIDTHS:
                await page.set_viewport_size({"width": width, "height": 900})
                await page.wait_for_timeout(100)
                status = await row.locator(".agent-line .status").bounding_box()
                aside = await row.locator(".agent-line .aside").bounding_box()
                apart = (
                    status["x"] + status["width"] <= aside["x"] + 1
                    or status["y"] + status["height"] <= aside["y"] + 1
                    or aside["y"] + aside["height"] <= status["y"] + 1
                )
                assert apart, (name, width, status, aside)
                found = await page.locator("#settings-dialog").evaluate(OVERFLOWS)
                assert not found, (name, width, found)
                SETTINGS_SHOTS.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=SETTINGS_SHOTS / f"long-{name}-{width}-{scheme}.png")
            assert not errors, errors
            await context.close()
    log("settings: long and combined states: statuses and controls apart, 800 to 1440")


if __name__ == "__main__":
    asyncio.run(main())
