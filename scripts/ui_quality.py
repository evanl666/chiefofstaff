"""Browser checks of how the UI looks and reads, with no accounts: a realistic Home (three
tasks, cards that need the owner) on a fake Copilot, at 800 to 1440 px, light and dark; and the
sidebar's Other Copilot sessions with 3, 12 and 40 of them, before and after where each stands
is shown (browser-shots/others/: a title keeps its width); Send to Chief's line in Settings and a
sent item's line on Home (browser-shots/send/); Monitoring's runs and Artifacts' folded runs
(browser-shots/qa-e41/); Settings' Coding agents tab and a worker on Codex
(browser-shots/agents-m2/); a task's life: its cards, a failed task out of Needs you, failing
Watch scans (browser-shots/qa7-life/); Your OK and Handled for you (browser-shots/qa8-approvals/).

Fails on: horizontal scrolling; elements overflowing their card or overlapping each other;
more than one main (filled) button per card or dialog footer; controls under 28 px tall or
without an accessible name; text under 12 px; more than three font sizes in a view; WCAG AA
violations found by axe-core (serious or critical); a crowded panel (more than 7 buttons at
rest with three tasks and two cards); more than three chat lines about one finished task; no
visible focus ring.

    uv run --with playwright python scripts/ui_quality.py
"""

# The checks run in the page are JavaScript, written on long lines.
# ruff: noqa: E501

import asyncio
import json
import os
import re
import sys
import tempfile
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from agent_paths import without_agents  # noqa: E402
from conftest import FakeClient, FakeSlackMcp, FakeTokens, call_tool, settle  # noqa: E402

from chiefofstaff.runtime import Runtime  # noqa: E402
from chiefofstaff.sources.slack import SlackSource  # noqa: E402
from chiefofstaff.web import create_app  # noqa: E402

PORT = int(os.environ.get("UI_QUALITY_PORT", "8796"))
TOKEN = "ui-quality-token"
BASE = f"http://127.0.0.1:{PORT}"
SHOTS = ROOT / "browser-shots"
AXE = "https://cdn.jsdelivr.net/npm/axe-core@4.10.2/axe.min.js"
WIDTHS = (800, 1024, 1280, 1440)
ASK = (
    "Fix the flaky login test in acme-api, then add rate limiting to login attempts. "
    "Also compare a few feature-flag services for us."
)
FIX, COMPARE, LIMIT = (
    "Fix the flaky login test",
    "Compare feature-flag services",
    "Rate-limit login attempts",
)


def log(text: str):
    print(f"  {text}", flush=True)


# What each check measures, in the page. Each returns a list of problems (strings).
CHECKS = r"""
(scope) => {
  const problems = [];
  const root = scope ? document.querySelector(scope) : document;
  const shown = (node) => {
    if (!node.isConnected) return false;
    // Inside a closed <details> (not its summary): not on screen, whatever its box says.
    const closed = node.closest("details:not([open])");
    if (closed && !closed.querySelector(":scope > summary")?.contains(node)) return false;
    const box = node.getBoundingClientRect();
    if (box.width < 1 || box.height < 1) return false;
    for (let at = node; at && at !== document; at = at.parentElement) {
      const style = getComputedStyle(at);
      if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
    }
    return true;
  };
  const name = (node) => {
    const what = node.tagName.toLowerCase() + (node.id ? `#${node.id}` : "") + (node.className && typeof node.className === "string" ? `.${node.className.trim().split(/\s+/).join(".")}` : "");
    const text = (node.textContent || node.value || "").trim().replace(/\s+/g, " ").slice(0, 30);
    return text ? `${what} "${text}"` : what;
  };
  const probe = document.createElement("span");
  probe.style.color = "var(--text)";
  document.body.append(probe);
  const filled = getComputedStyle(probe).color;
  probe.remove();
  // No sideways scrolling, anywhere.
  if (!scope && document.documentElement.scrollWidth > innerWidth + 1) problems.push(`page scrolls sideways (${document.documentElement.scrollWidth} > ${innerWidth})`);
  for (const box of root.querySelectorAll("#scroller, #panel, #settings-pane, #sidebar, .diff-body, #usage-tables, #file-body, .review-body")) {
    if (shown(box) && box.scrollWidth > box.clientWidth + 1) problems.push(`${name(box)} scrolls sideways (${box.scrollWidth} > ${box.clientWidth})`);
  }
  // Inside each card: nothing sticks out, and nothing in a row lies on top of another.
  const inside = (outer, inner) => inner.left >= outer.left - 1 && inner.right <= outer.right + 1 && inner.top >= outer.top - 1 && inner.bottom <= outer.bottom + 1;
  const overlap = (a, b) => Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left)) * Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
  for (const card of root.querySelectorAll(".card, .menu, dialog[open] footer, .event, .msg, .review-comment, .review-dialog > header")) {
    if (!shown(card)) continue;
    const box = card.getBoundingClientRect();
    for (const child of card.querySelectorAll("*")) {
      if (!shown(child) || child.closest(".menu") !== card.closest(".menu")) continue;
      if (getComputedStyle(child).position === "absolute" || child.closest(".folded, .clamp, .markdown table, pre")) continue;
      if (!inside(box, child.getBoundingClientRect())) problems.push(`${name(child)} sticks out of ${name(card)}`);
    }
    for (const row of [card, ...card.querySelectorAll(".head, .actions, .receipts, .choices, .options, summary, .learn-row, .row")]) {
      const items = [...row.children].filter((item) => shown(item) && getComputedStyle(item).position !== "absolute");
      for (let i = 0; i < items.length; i++) {
        for (let j = i + 1; j < items.length; j++) {
          if (overlap(items[i].getBoundingClientRect(), items[j].getBoundingClientRect()) > 4) problems.push(`${name(items[i])} overlaps ${name(items[j])}`);
        }
      }
    }
  }
  // Text running past its own box, inside a card or a dialog: a long path, a line of code.
  for (const box of root.querySelectorAll(".card *, dialog[open] *")) {
    if (!shown(box) || box.closest("pre, .folded, .clamp, .markdown table")) continue;
    const style = getComputedStyle(box);
    if (style.display.startsWith("inline") && style.display !== "inline-block" && style.display !== "inline-flex") continue;
    if (style.overflowX !== "visible" || style.textOverflow === "ellipsis" || box.matches("textarea, input, select")) continue;
    if (box.scrollWidth > box.clientWidth + 1) problems.push(`${name(box)} runs past its box (${box.scrollWidth} > ${box.clientWidth})`);
  }
  // Text that does not fit its control (unless it is cut on purpose, with an ellipsis).
  for (const node of root.querySelectorAll("button, a.button, .chip, .receipt, label > span")) {
    if (!shown(node) || getComputedStyle(node).textOverflow === "ellipsis") continue;
    if (node.scrollWidth > node.clientWidth + 1 && getComputedStyle(node).overflow !== "visible") problems.push(`${name(node)} text does not fit`);
  }
  // One main (filled) action per card and per dialog footer.
  const primary = (node) => shown(node) && getComputedStyle(node).backgroundColor === filled;
  for (const card of root.querySelectorAll(".card, dialog[open] footer")) {
    if (!shown(card)) continue;
    const main = [...card.querySelectorAll("button, a.button")].filter(primary);
    if (main.length > 1) problems.push(`${name(card)} has ${main.length} main buttons: ${main.map(name).join(", ")}`);
  }
  // Controls big enough to hit, and named for screen readers.
  for (const node of root.querySelectorAll("button, a[href], input:not([type=hidden]), select, textarea, summary, [role=button], [role=menuitem]")) {
    if (!shown(node) || node.closest(".markdown, .msg.owner .attachments")) continue;
    const checkbox = node.type === "checkbox" || node.type === "radio";
    const target = checkbox ? node.closest("label") || node : node;
    const height = target.getBoundingClientRect().height;
    if (height < 27.5) problems.push(`${name(target)} is ${height.toFixed(0)}px tall`);
    const label = node.getAttribute("aria-label") || node.getAttribute("aria-labelledby") || (node.labels && node.labels.length) || node.title || node.placeholder || (node.textContent || "").trim() || node.querySelector("img[alt]");
    if (!label) problems.push(`${name(node)} has no accessible name`);
  }
  // Text at 12px or more; few sizes per view.
  const sizes = new Map();
  const walker = document.createTreeWalker(root === document ? document.body : root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const text = walker.currentNode.textContent.trim();
    const parent = walker.currentNode.parentElement;
    if (!text || !parent || !shown(parent) || parent.closest("dialog:not([open]), option, select")) continue;
    if (!scope && parent.closest("dialog")) continue;
    const size = parseFloat(getComputedStyle(parent).fontSize);
    if (size < 11.95) problems.push(`${name(parent)} text is ${size}px`);
    if (/[\p{L}\p{N}]/u.test(text) && !parent.closest(".toast")) sizes.set(size, [...(sizes.get(size) || []), name(parent)]);
  }
  if (sizes.size > 3) problems.push(`${sizes.size} font sizes: ${[...sizes].map(([size, where]) => `${size}px (${where.slice(0, 2).join(", ")})`).join("; ")}`);
  return [...new Set(problems)];
}
"""

# Buttons that look like buttons (filled or outlined), in Home's panel.
PANEL_BUTTONS = r"""
() => [...document.querySelectorAll("#panel button, #panel a.button")].filter((node) => {
  const box = node.getBoundingClientRect();
  if (box.width < 1 || box.height < 1 || node.closest("[hidden], .menu") || node.matches(".receipt")) return false;
  const style = getComputedStyle(node);
  const painted = style.backgroundColor !== "rgba(0, 0, 0, 0)" && style.backgroundColor !== "transparent";
  const outlined = parseFloat(style.borderTopWidth) > 0 && style.borderTopColor !== "rgba(0, 0, 0, 0)";
  return painted || outlined;
}).map((node) => (node.textContent || node.getAttribute("aria-label") || "").trim())
"""


async def seed(runtime, client, code: Path) -> dict:
    """Three tasks (one running, one queued, one to review), and a decision and a Slack reply
    that need the owner."""
    store = runtime.store
    settings = store.settings().model_copy(
        update={"code_folder": str(code), "max_workers": 2, "mac_notifications": False}
    )
    store.save_settings(settings)
    router = store.get("router_id")
    await runtime.owner_send(router, ASK)
    await settle(runtime)
    workers = {}
    for title in (FIX, COMPARE, LIMIT):
        result = await call_tool(
            runtime, "spawn_worker", router, title=title, instructions=title, why_new="test"
        )
        found = json.loads(result.text_result_for_llm)
        workers[title] = found
        await settle(runtime)
    session = lambda title: client.sessions[runtime.copilot_id(workers[title]["session_id"])]  # noqa: E731
    router_session = client.sessions[runtime.copilot_id(router)]
    router_session.emit(
        "assistant.message",
        content="Started three tasks: the flaky login test, rate limiting once the fix is in, "
        "and a feature-flag comparison.",
        tool_requests=None,
    )
    router_session.emit("session.idle")
    fix = session(FIX)
    for call, output in [
        ("c1", "F\n1 failed in 0.21s\n<exited with exit code 1>"),
        ("c2", "........\n8 passed in 0.29s\n<exited with exit code 0>"),
    ]:
        fix.emit("tool.execution_start", tool_name="bash",
                 arguments={"command": "uv run pytest -q"}, tool_call_id=call)  # fmt: skip
        fix.emit("tool.execution_complete", tool_call_id=call, success=True,
                 result=SimpleNamespace(content=output))  # fmt: skip
    await settle(runtime)
    await call_tool(
        runtime, "report", workers[FIX]["session_id"], status="done",
        summary="Fixed the flaky login test; all tests pass.\n- Cause: token expiry read the "
        "real clock, so slow runs failed\n- Fix: `app/login.py` takes the clock as a parameter\n"
        "- `uv run pytest -q`: 8 passed",
        criteria=[{"criterion": "`uv run pytest -q` passes", "result": "met",
                   "evidence": "`uv run pytest -q`: 8 passed"}],
    )  # fmt: skip
    compare = session(COMPARE)
    compare_task = workers[COMPARE]["task_id"]
    for number in range(1, 7):
        await call_tool(
            runtime,
            "report",
            workers[COMPARE]["session_id"],
            status="progress",
            summary=f"Comparison update {number}: checked service {number}.",
        )
    # Asked by the Router how far it is: its answer is an Answer/Update, its task goes on.
    await call_tool(runtime, "message_session", router, session_id=workers[COMPARE]["session_id"],
                    task="note", task_id=compare_task, text="How many services so far?")  # fmt: skip
    await call_tool(
        runtime,
        "report",
        workers[COMPARE]["session_id"],
        status="answer",
        task_id=compare_task,
        summary="The current comparison covers six services.",
    )
    compare.emit("assistant.message", content="Reading the pricing pages of three services.",
                 tool_requests=None)  # fmt: skip
    compare.emit("tool.execution_start", tool_name="bash", tool_call_id="p1",
                 arguments={"command": "curl -s https://example.com/pricing | head -40"})  # fmt: skip
    await settle(runtime)
    limit = workers[LIMIT]
    decision = store.create_draft(
        router, "decision",
        {"question": "Limit login attempts per IP address or per account?",
         "why": "Per IP misses attackers who rotate addresses; per account lets anyone lock a "
         "user out.",
         "options": [{"choice": "Per IP address", "effect": "Simple; offices share IPs"},
                     {"choice": "Per account", "effect": "Stops guessing one password"},
                     {"choice": "Both", "effect": "Safest; one more counter"}],
         "recommendation": "Per account: it protects each user directly",
         "askers": [{"task_id": limit["task_id"], "title": LIMIT,
                     "session_id": limit["session_id"]}]},
        "Limit login attempts per IP address or per account?",
    )  # fmt: skip
    slack = store.create_draft(
        router, "slack",
        {"channel_id": "D0DEMOMAYA", "thread_ts": "", "to": "Maya Chen",
         "latest": "hey! is the login fix going out today? support keeps asking"},
        "Yes! The flaky login test is fixed and waiting for review; rate limiting lands right "
        "after. I'll ping you when it ships.",
        note="Maya asks whether the login fix goes out today.",
    )  # fmt: skip
    for draft in (decision, slack):
        runtime.bus.publish("draft", store.draft(draft["id"]))
    runtime.bus.publish("state")
    return {"workers": workers, "router": router}


# A review with what the owner needs to review it: the PR, and the lines each comment is about.
# One path is long (120 characters): nothing may run past the card or the review view.
LONG_PATH = (
    "services/matching/src/main/scala/com/acme/openconnect/pysparkjobs/company_matching/"
    "normalizers/CompanyNameNormalizer.scala"
)
REVIEW = {
    "repo": "acme/api", "number": 42, "url": "https://github.com/acme/api/pull/42",
    "via": "task", "head_sha": "c0ffee", "verdict": "request_changes",
    "pr": {"title": "Match companies by normalized name", "author": "maya",
           "about": "Adds a normalizer for company names (case, punctuation, legal suffixes "
           "such as Inc. and GmbH) and uses it in the nightly matching job, so the same "
           "company spelled two ways is matched once.",
           "additions": 214, "deletions": 37, "files": 6},
    "comments": [
        {"path": LONG_PATH, "line": 48, "side": "RIGHT",
         "body": "This drops every non-Latin character, so 株式会社トヨタ normalizes to an empty "
         "string and matches every other empty name. Keep letters from all scripts.",
         "context": "     val cleaned = name.toLowerCase\n-    .replaceAll(\"[^a-z0-9 ]\", \"\")\n"
         "+    .replaceAll(\"[^a-z0-9 ]\", \"\").trim  // keeps only ASCII letters and digits"},
        {"path": "services/matching/jobs/nightly_match.py", "line": 112, "side": "RIGHT",
         "body": "Joining on domain alone fans out when two companies share one (gmail.com).",
         "context": "+    matches = companies.join(accounts, on=\"domain\")"},
        {"path": "services/matching/jobs/nightly_match.py", "start_line": 140, "line": 146,
         "side": "LEFT", "body": "Was removing the dedupe step on purpose?",
         "context": "-    matches = matches.dropDuplicates([\"company_id\"])"},
    ],
}  # fmt: skip
REVIEW_BODY = (
    "Good direction. Two things before this merges: the normalizer drops non-Latin names, and "
    "the join on domain can fan out."
)


def gallery(runtime, seeded: dict) -> list[dict]:
    """Every other kind of card: a question, a PR review with a verdict and line comments, a
    reply to send yourself, a sign-in, and the Router's notes (one urgent)."""
    store = runtime.store
    compare = seeded["workers"][COMPARE]["session_id"]
    fix = seeded["workers"][FIX]["session_id"]
    drafts = [
        store.create_draft(
            compare, "question",
            {"question": "Which seat count should the comparison price?",
             "choices": ["10 seats", "50 seats", "200 seats"], "allow_freeform": True},
            "Which seat count should the comparison price?",
        ),
        review := store.create_draft(fix, "github", REVIEW, REVIEW_BODY),
        store.create_draft(
            fix, "slack",
            {"channel_id": "C0DEPLOYS", "thread_ts": "1794157630.819840", "to": "Maya",
             "after_review": {"id": review["id"], "label": "your review of acme/app#7"}},
            "Reviewed: two small changes asked for.",
        ),
        store.create_draft(
            compare, "reply",
            {"to": "Ann Lee", "where": "email", "link": "https://mail.example.com/t/1"},
            "Hi Ann, the comparison is coming tomorrow morning.",
        ),
        store.create_draft(
            compare, "signin", {"url": "https://login.example.com/authorize", "server": "Jira"},
            "Jira needs you to sign in.",
        ),
        store.create_draft(
            seeded["router"], "form",
            {"schema": {"type": "object", "properties": {"days": {"type": "string",
                                                                  "title": "Which days"}}},
             "source": "Chief of Staff", "watch": "DMs from Ann"},
            "Watch DMs from Ann? Choose when.",
        ),
    ]  # fmt: skip
    for draft in drafts:
        runtime.bus.publish("draft", store.draft(draft["id"]))
    # The Router's notes that need the owner: one to answer, one urgent.
    router = seeded["router"]
    for text, urgent in [
        ("Maya asks whether the login fix ships today.\nTell me, or say if I should draft a "
         "holding reply.", False),
        ("The staging deploy is down: Ann is blocked on you today.", True),
    ]:  # fmt: skip
        runtime.add_message(router, "update", text, {"needs_action": not urgent, "urgent": urgent})
    # A message of the owner's to one worker, as Home shows it: its "→ worker" line above it.
    runtime.owner_told(store.session(compare), "Go on where you stopped.")
    return drafts


class Quality:
    def __init__(self):
        self.failures: list[str] = []
        self.axe = urllib.request.urlopen(AXE, timeout=30).read().decode()

    async def check(self, page, where: str, scope: str = "", known: re.Pattern | None = None):
        """`known`: findings left as they are on purpose (see KNOWN_ROW_FINDINGS)."""
        found = await page.evaluate(CHECKS, scope)
        self.failures += [
            f"{where}: {problem}" for problem in found if not (known and known.match(problem))
        ]

    async def contrast(self, page, where: str, scope: str = ""):
        # Colors as they end up, not halfway through a fade.
        await page.evaluate(
            "() => Promise.all(document.getAnimations()"
            ".filter((a) => a.effect?.getComputedTiming().iterations !== Infinity)"
            ".map((a) => a.finished.catch(() => {})))"
        )
        if not await page.evaluate("() => Boolean(window.axe)"):
            await page.add_script_tag(content=self.axe)
        result = await page.evaluate(
            """async (scope) => {
            const found = await axe.run(scope || document, {
              runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] },
            });
            return found.violations.filter((v) => ["serious", "critical"].includes(v.impact))
              .map((v) => `${v.id}: ${v.nodes.slice(0, 4).map((n) => n.target.join(" ") +
                (n.any[0]?.data?.contrastRatio ? ` (${n.any[0].data.contrastRatio}:1)` : "")).join(", ")}${v.nodes.length > 4 ? ` +${v.nodes.length - 4}` : ""}`);
            }""",
            scope,
        )  # fmt: skip
        self.failures += [f"{where}: axe {problem}" for problem in result]


async def run(runtime, seeded, browser, quality: Quality, phase: str):
    for scheme in ("light", "dark"):
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900}, color_scheme=scheme, bypass_csp=True
        )
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))  # noqa: B023
        await page.goto(f"{BASE}/#token={TOKEN}")
        # Running and Monitoring: the comparison at work, rate limiting queued behind the fix.
        await expect(page.locator("#tasks .card.task")).to_have_count(2, timeout=15000)
        # The decision, the Slack reply and the fixed test's result to accept.
        await expect(page.locator("#needs > .items > .card")).to_have_count(3)
        await page.wait_for_timeout(400)
        for width in WIDTHS:
            await page.set_viewport_size({"width": width, "height": 900})
            await page.wait_for_timeout(150)
            where = f"home {width} {scheme}"
            await page.screenshot(path=SHOTS / f"quality-{phase}-home-{width}-{scheme}.png")
            await quality.check(page, where)
            if width >= 1100:
                panel = await page.evaluate(PANEL_BUTTONS)
                if len(panel) > 7:
                    quality.failures.append(f"{where}: {len(panel)} buttons in the panel: {panel}")
        await quality.contrast(page, f"home {scheme}")
        await page.set_viewport_size({"width": 1280, "height": 900})
        # Tasks only groups Home's four parts below Needs you (it is no box of its own).
        await page.locator("#tasks-running").scroll_into_view_if_needed()
        await page.locator("#panel").screenshot(path=SHOTS / f"quality-{phase}-tasks-{scheme}.png")
        # The fixed test's report waits for Accept under Needs you (a report that only informs
        # too): a row that opens its card in place, then its report.
        review = page.locator("#needs .card.task", has_text=FIX)
        await expect(review.get_by_role("button", name="Request changes")).to_be_visible()
        await review.locator(".head .title").click()
        await expect(review).to_have_class(re.compile(r"\bunfolded\b"))
        toggle = review.locator(".expand")
        if (await toggle.inner_text()).strip() == "Show report":
            await toggle.click()
        await expect(toggle).to_have_text("Hide report")
        await review.screenshot(path=SHOTS / f"quality-{phase}-review-open-{scheme}.png")
        await quality.check(page, f"review open {scheme}", "#panel")
        await toggle.click()
        await expect(toggle).to_have_text("Show report")
        # A finished task: at most its start, the Router's reply and its end in the chat.
        lines = await page.locator("#timeline > *", has_text=FIX).count()
        if lines > 3:
            quality.failures.append(f"chat {scheme}: {lines} lines about “{FIX}”")
        # A visible focus ring for keyboard users.
        await page.set_viewport_size({"width": 1280, "height": 900})
        await page.keyboard.press("Tab")
        ring = await page.evaluate(
            """() => {
            const button = document.querySelector("#panel .card .actions button:not(.more-button)");
            button.focus();
            const style = getComputedStyle(button);
            return button.matches(":focus-visible") && (style.outlineStyle !== "none" || style.boxShadow !== "none");
            }"""
        )  # fmt: skip
        if not ring:
            quality.failures.append(f"focus {scheme}: no visible focus ring on a card's button")

        # Its worker has nothing open: it is in the sidebar's folded group.
        worker = page.locator("#session-list .item", has_text=FIX)
        if not await worker.is_visible():
            await page.locator("#session-list .idle-workers > summary").click()
        await worker.click()
        await expect(page.locator("#back-home")).to_be_visible()
        await page.wait_for_timeout(300)
        for width in (1024, 800):
            await page.set_viewport_size({"width": width, "height": 900})
            await page.screenshot(path=SHOTS / f"quality-{phase}-worker-{width}-{scheme}.png")
            await quality.check(page, f"worker {width} {scheme}")
        await page.locator("#session-more-button").click()
        await page.wait_for_timeout(200)
        await page.screenshot(path=SHOTS / f"quality-{phase}-worker-menu-{scheme}.png")
        await quality.check(page, f"worker menu {scheme}", "#session-more")
        await quality.contrast(page, f"worker {scheme}")
        await page.keyboard.press("Escape")
        await page.locator("#back-home").click()

        await page.set_viewport_size({"width": 1280, "height": 900})
        await page.locator("#settings-button").click()
        for tab in ("general", "workers", "models", "connections", "you", "advanced"):
            await page.locator(f"#settings-tabs [data-tab={tab}]").click()
            if tab == "connections":
                for section in ("slack", "abilities", "watches", "away"):
                    details = page.locator(f"#conn-{section}")
                    if not await details.evaluate("section => section.open"):
                        await details.locator("summary").click()
            await page.wait_for_timeout(150)
            await page.screenshot(path=SHOTS / f"quality-{phase}-settings-{tab}-{scheme}.png")
            await quality.check(page, f"settings {tab} {scheme}", "#settings-dialog")
            await quality.contrast(page, f"settings {tab} {scheme}", "#settings-dialog")
        await page.set_viewport_size({"width": 800, "height": 900})
        await page.screenshot(path=SHOTS / f"quality-{phase}-settings-800-{scheme}.png")
        await quality.check(page, f"settings 800 {scheme}", "#settings-dialog")
        await page.locator("#settings-cancel").click()
        await page.set_viewport_size({"width": 1280, "height": 900})
        await page.locator("#usage-button").click()
        await page.wait_for_timeout(400)
        await page.screenshot(path=SHOTS / f"quality-{phase}-usage-{scheme}.png")
        await quality.check(page, f"usage {scheme}", "#usage-dialog")
        await quality.contrast(page, f"usage {scheme}", "#usage-dialog")
        await page.keyboard.press("Escape")
        quality.failures += [f"page error {scheme}: {error}" for error in errors]
        await context.close()


async def report_views(runtime, browser, quality: Quality):
    """E115's Answer/Update line and folded progress history at both compact widths."""
    for scheme in ("light", "dark"):
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900}, color_scheme=scheme
        )
        page = await context.new_page()
        await page.goto(f"{BASE}/#token={TOKEN}")
        await expect(page.locator("#application")).to_be_visible()
        await expect(
            page.locator("#timeline .event", has_text="Answer/Update from")
        ).to_contain_text("six services")
        for width in (800, 1280):
            await page.set_viewport_size({"width": width, "height": 900})
            if await page.locator("#home-tabs").is_visible():
                await page.locator("#home-tabs [data-tab=tasks]").click()
            card = page.locator("#tasks-running .card.task", has_text=COMPARE)
            await expect(card).to_be_visible()
            if "unfolded" not in (await card.get_attribute("class") or "").split():
                await card.locator(".head .title").click()
            await expect(card.locator(".progress-line .words")).to_have_text(
                "Comparison update 6: checked service 6."
            )
            await expect(card.locator(".earlier-updates summary")).to_have_text(
                "Earlier updates (4)"
            )
            await quality.check(page, f"report progress {width} {scheme}", "#tasks-running")
            await card.scroll_into_view_if_needed()
            await page.screenshot(path=SHOTS / f"quality-progress-{width}-{scheme}.png")
        await context.close()
    log("Answer/Update and five recent progress reports: 800/1280, light/dark")


async def cards(runtime, seeded, browser, quality: Quality, phase: str):
    """Every kind of card that needs the owner, at the panel's widths."""
    drafts = gallery(runtime, seeded)
    for scheme in ("light", "dark"):
        context = await browser.new_context(
            viewport={"width": 1440, "height": 2000}, color_scheme=scheme, bypass_csp=True
        )
        page = await context.new_page()
        await page.goto(f"{BASE}/#token={TOKEN}")
        # A card each (a result a draft is for is a card inside that draft's card).
        await expect(page.locator("#needs > .items > .card")).to_have_count(10, timeout=15000)
        # Bug bash 6 (N10): a Slack reply about a review says it waits for that review.
        await expect(page.locator("#needs .card .waits-review")).to_contain_text(
            "Waits for your review of acme/app#7 to be posted first"
        )
        # A watch the Router proposes says so (not "Form").
        await expect(page.locator("#needs .card.request.form .head .label")).to_have_text(
            "New watch"
        )
        # Each card says once what it is about: a decision names its task in its label only.
        decision = page.locator("#needs .card.decision")
        await expect(decision.locator(".head .label")).to_contain_text(f"Decision for {LIMIT}")
        if await decision.locator(".context").count():
            quality.failures.append(f"cards {scheme}: the decision names its task twice")
        for width in (1440, 1100, 800):
            await page.set_viewport_size({"width": width, "height": 2000})
            if width < 1100:
                await page.locator("#home-tabs [data-tab=needs]").click()
            await page.wait_for_timeout(150)
            await page.locator("#needs").screenshot(
                path=SHOTS / f"quality-{phase}-cards-{width}-{scheme}.png"
            )
            await quality.check(page, f"cards {width} {scheme}", "#panel")
        await quality.contrast(page, f"cards {scheme}", "#panel")
        # The review, opened: the PR, the summary, each comment with its code; at every width.
        review = page.locator("#needs .card.pr-review")
        await page.set_viewport_size({"width": 1280, "height": 900})
        await review.get_by_role("button", name="Review…").click()
        view = page.locator(".review-dialog")
        await expect(view.locator(".review-comment")).to_have_count(len(REVIEW["comments"]))
        await expect(view.locator(".review-comment .where").first).to_have_attribute(
            "title", f"{LONG_PATH}:48"
        )
        for width in WIDTHS:
            await page.set_viewport_size({"width": width, "height": 900})
            await page.wait_for_timeout(200)
            await page.screenshot(path=SHOTS / f"quality-{phase}-review-view-{width}-{scheme}.png")
            await quality.check(page, f"review view {width} {scheme}", ".review-dialog")
        await view.locator(".review-comment").last.get_by_role("button", name="✕ Remove").click()
        await view.get_by_role("button", name="Ask to redo…").click()
        await page.screenshot(path=SHOTS / f"quality-{phase}-review-view-redo-{scheme}.png")
        await quality.check(page, f"review view redo {scheme}", ".review-dialog")
        await quality.contrast(page, f"review view {scheme}", ".review-dialog")
        await page.keyboard.press("Escape")
        await page.keyboard.press("Escape")
        await expect(view).to_be_hidden()
        await page.set_viewport_size({"width": 1280, "height": 900})
        for index, card in enumerate(await page.locator("#needs > .items > .card").all()):
            await card.scroll_into_view_if_needed()
            await card.screenshot(path=SHOTS / f"quality-{phase}-card-{index}-{scheme}.png")
        await context.close()
    for draft in drafts:
        runtime.store.update_draft(draft["id"], status="rejected")
        runtime.bus.publish("draft", runtime.store.draft(draft["id"]))
    for update in runtime.owner_updates():
        runtime.update_seen(update["id"])


# Titles as owners write them: short, long, right to left.
TITLES = (
    "Fix the flaky login test",
    "Investigate why the nightly export to the analytics warehouse drops rows after midnight",
    "إصلاح اختبار تسجيل الدخول المتقطع في خدمة الحسابات",
    "Upgrade the payments SDK to version 5",
    "תקן את בדיקת ההתחברות שנכשלת לפעמים",
    "Profile the slow orders page",
    "Rate-limit login attempts per IP address and per account, with a clear message",
    "Write the API changelog",
    "Tidy the README",
    "Explain the cache misses",
)
# Eight that wait or work, then the rest.
KINDS = ("asks", "your_turn", "working", "no_progress", "asks", "working", "your_turn", "asks")
REST = ("idle", "finished", "unknown", "stopped", "old")
# Each title's box and text widths, by row.
LABELS = """() => Object.fromEntries([...document.querySelectorAll('#external-list button.item')]
  .map((row) => [row.querySelector('.label').textContent,
    [row.querySelector('.label').getBoundingClientRect().width,
     row.querySelector('.sub').getBoundingClientRect().width]]))"""


def other_sessions(client, count: int) -> list[str]:
    """`count` of the owner's other Copilot sessions, eight of them (at most) waiting or at work."""
    from ui_edges import answer, step, terminal, turn

    client.metas.clear()
    client.in_use.clear()
    ids = []
    for index in range(count):
        kind = KINDS[index] if index < len(KINDS) else REST[index % len(REST)]
        title = TITLES[index % len(TITLES)] + (
            f" {index // len(TITLES) + 1}" if index >= len(TITLES) else ""
        )
        minutes = 3 + index * 7
        events = {
            "asks": [
                *turn("Go", minutes + 2),
                *step(f"c{index}", "ask_user", minutes, message="Ship it?"),
            ],
            "your_turn": [*turn("Go", minutes + 2), *answer("Done: the tests pass.", minutes)],
            "working": [*turn("Go", minutes), *step(f"c{index}", "bash", 0.2, command="make test")],
            "no_progress": [*turn("Go", 50), *step(f"c{index}", "bash", 45, command="make build")],
            "idle": [*turn("Go", 130), *answer("Tidied.", 125)],
            "finished": [*turn("Go", 125), *answer("Drafted.", 121), ("session.shutdown", 120, {})],
            "unknown": turn("Go", minutes),
            "stopped": [*turn("Go", 70), *step(f"c{index}", "bash", 65, command="npm run e2e")],
            "old": [*turn("Go", 2000), *answer("Done.", 1990), ("session.shutdown", 1990, {})],
        }[kind]
        closed = kind in {"finished", "stopped", "old"}
        ids.append(terminal(client, title, events, is_open=not closed,
                            made="" if kind == "unknown" else "github/cli"))  # fmt: skip
    return ids


async def others(runtime, client, browser, quality: Quality):
    """The sidebar's Other Copilot sessions, where each stands: at 252 px in windows of 800 to
    1440 px, light and dark, with 3, 12 and 40 sessions (8 waiting or at work); the group folded
    and unfolded, its "N more" folded and unfolded; long and right-to-left titles keep the room
    they had without the setting (browser-shots/others/, before = off, after = on)."""
    from ui_edges import others_setting

    folder = SHOTS / "others"
    folder.mkdir(exist_ok=True)
    client.home = runtime.store.directory / "copilot-home"
    shrunk = {}
    try:
        for count in (3, 12, 40):
            ids = other_sessions(client, count)
            runtime.others.listed_at = float("-inf")  # Listed again at its next pass.
            await runtime.workers.refresh_elsewhere()
            widths = {}
            for when, on in (("before", False), ("after", True)):
                await others_setting(runtime, on)
                for scheme in ("light", "dark"):
                    context = await browser.new_context(
                        viewport={"width": 1440, "height": 900},
                        color_scheme=scheme,
                        bypass_csp=True,
                    )
                    page = await context.new_page()
                    await page.goto(f"{BASE}/#token={TOKEN}")
                    if not on:
                        await page.locator("#external > summary").click()
                    await expect(page.locator("#external-count")).not_to_have_text("")
                    if on:
                        waiting = page.locator(
                            "#external-list .dot.waiting, #external-list .dot.open.busy, #external-list .dot.stalled"
                        )
                        await expect(waiting).to_have_count(min(count, 8), timeout=15000)
                    more = page.locator("#external-list > details.idle-workers")
                    for width in WIDTHS:
                        await page.set_viewport_size({"width": width, "height": 900})
                        await page.wait_for_timeout(100)
                        name = f"{when}-{count}-{width}-{scheme}"
                        await page.locator("#external > summary").scroll_into_view_if_needed()
                        await page.locator("#sidebar").screenshot(path=folder / f"{name}.png")
                        await quality.check(page, f"others {name}", "#sidebar")
                        if await more.count():
                            await more.locator("> summary").click()
                            await page.locator("#sidebar").screenshot(
                                path=folder / f"{name}-more.png"
                            )
                            await quality.check(page, f"others {name} more", "#sidebar")
                            await more.locator("> summary").click()
                        await page.locator("#external > summary").click()
                        await page.locator("#sidebar").screenshot(
                            path=folder / f"{name}-folded.png"
                        )
                        await page.locator("#external > summary").click()
                    if await more.count():
                        await more.locator("> summary").click()
                    await page.set_viewport_size({"width": 1280, "height": 900})
                    await page.screenshot(path=folder / f"{when}-{count}-page-{scheme}.png")
                    widths[when] = await page.evaluate(LABELS)
                    await quality.contrast(page, f"others {when} {count} {scheme}", "#sidebar")
                    await context.close()
            # The title keeps its room: as wide as without the setting, row by row.
            assert set(widths["after"]) == set(widths["before"]), widths
            for title, (label, _) in widths["after"].items():
                before = widths["before"][title][0]
                if label < before - 0.5:
                    quality.failures.append(
                        f"others {count}: “{title}” {label:.1f}px, {before:.1f}px before"
                    )
            shrunk[count] = max(
                widths["before"][title][0] - label for title, (label, _) in widths["after"].items()
            )
            assert len(ids) == count
    finally:
        client.metas.clear()
        client.in_use.clear()
        await others_setting(runtime, True)
    log(
        f"other Copilot sessions: 3, 12 and 40 at 252 px, titles as wide as before (most lost: {shrunk} px); browser-shots/others/"
    )


async def send_views(runtime, browser, quality: Quality):
    """Send to Chief (docs/design/send-anywhere.md): its line at the end of Settings →
    Connections off, unfolded to its consent, on, and with the socket closed for a reason; a sent
    item's line in the Router chat, folded and open; 800 to 1440 px, light and dark
    (browser-shots/send/)."""
    from ui_edges import send_switch, until

    from chiefofstaff import send

    folder = SHOTS / "send"
    folder.mkdir(exist_ok=True)
    data = runtime.store.directory
    note = "Fix the flaky login test in acme/api, it blocks Friday's release"
    passed = data.parent / "passed.txt"
    passed.write_text(
        "Steps to reproduce:\n1. Throttle the network to 3G\n2. Log in\n"
        "Expected: signed in. Actual: `TimeoutError` after 30 s"
    )
    copied = data.parent / "copied.txt"
    copied.write_text(
        "From the incident channel: logins time out for users on mobile networks\nStarted after Tuesday's deploy"
    )
    try:
        await send_switch(runtime, True)
        for arguments in (
            [note, "--url", "https://github.com/acme/api/issues/117",
             "--title", "Login times out on slow networks · Issue #117 · acme/api",
             "--text-from", str(passed)],
            ["--text-from", str(copied)],
        ):  # fmt: skip
            code = await asyncio.to_thread(send.main, [*arguments, "--data-dir", str(data)])
            assert code == send.SENT
        await send_switch(runtime, False)
        for scheme in ("light", "dark"):
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900}, color_scheme=scheme, bypass_csp=True
            )
            page = await context.new_page()
            await page.goto(f"{BASE}/#token={TOKEN}")
            await expect(page.locator("#application")).to_be_visible()
            switch = page.locator("#settings-form input[name=send_to_chief]")
            for state in ("off", "consent", "on", "problem"):
                await page.locator("#settings-button").click()
                await page.locator("#settings-tabs [data-tab=connections]").click()
                if state == "consent":
                    await switch.check()
                elif state in {"on", "problem"}:
                    if state == "problem":
                        data.chmod(0o770)
                    await switch.check()
                    await page.locator("#send-on").click()
                    shown = "#send-problem" if state == "problem" else "#send-how"
                    await expect(page.locator(shown)).to_be_visible()
                    data.chmod(0o700)
                await page.locator("#send-line").scroll_into_view_if_needed()
                for width in WIDTHS:
                    await page.set_viewport_size({"width": width, "height": 900})
                    await page.wait_for_timeout(100)
                    name = f"settings-{state}-{width}-{scheme}"
                    await page.screenshot(path=folder / f"{name}.png")
                    await quality.check(page, f"send {name}", "#settings-dialog")
                await quality.contrast(page, f"send settings {state} {scheme}", "#settings-dialog")
                await page.set_viewport_size({"width": 1280, "height": 900})
                if state in {"on", "problem"}:
                    await switch.uncheck()
                    await until(lambda: not runtime.store.settings().send_to_chief)
                await page.locator("#settings-cancel").click()
            entry = page.locator("#timeline .event.opens", has_text=note)
            text = page.locator("#timeline .event.opens", has_text="From the incident channel")
            await expect(entry).to_have_count(1, timeout=10000)
            await expect(text).to_have_count(1)
            for view in ("line", "open", "text"):
                if view == "open":
                    await entry.locator("button.line").click()
                elif view == "text":
                    await entry.locator("button.line").click()
                    await text.locator("button.line").click()
                for width in WIDTHS:
                    await page.set_viewport_size({"width": width, "height": 900})
                    if width < 1100:
                        await page.locator("#home-tabs [data-tab=chat]").click()
                    await (text if view == "text" else entry).scroll_into_view_if_needed()
                    await page.wait_for_timeout(150)
                    name = f"home-{view}-{width}-{scheme}"
                    await page.screenshot(path=folder / f"{name}.png")
                    await quality.check(page, f"send {name}")
            await quality.contrast(page, f"send home {scheme}")
            await context.close()
    finally:
        data.chmod(0o700)
        await send_switch(runtime, False)
    log(
        "Send to Chief: its Settings line off, unfolded, on and broken; a sent line; browser-shots/send/"
    )


# What the rows of Monitoring and Artifacts were made with before these checks first looked at
# them (QA round 12): links and chips 18-19 px tall on a row's lines, and Artifacts' 10 px kind
# label. A change of their own; anything else found there fails.
KNOWN_ROW_FINDINGS = re.compile(
    r"^(?:a\.object|a\.chip-link|button\.chip-off|a\.a-name|button\.a-task\.link|"
    r"button\.a-hide|span\.a-kind) .*(?: is 1[89]px tall| text is 10px)$"
)


async def runs_views(runtime, browser, quality: Quality):
    """QA E41/E43, round 13: a finished task with only a run of unknown status (under Needs
    you, its two answers on its row), one in Monitoring with another run still waited for (its
    menu open), a running task's unknown run, and Artifacts with tasks' runs under their line
    (a waiting run in sight, one marked ended, one no longer tracked), folded and unfolded; 800
    to 1440 px, light and dark (browser-shots/qa-e41/)."""
    from ui_edges import FLYTE, runs_seeded, thirteen_seeded

    folder = SHOTS / "qa-e41"
    folder.mkdir(exist_ok=True)
    await runs_seeded(runtime)
    ship = (await thirteen_seeded(runtime))["E58 ship the approvals"]["id"]
    runtime.task_actions.end_runs(ship, [FLYTE + "ap07"], "untracked")  # E53: not tracked.
    for scheme in ("light", "dark"):
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1000}, color_scheme=scheme, bypass_csp=True
        )
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))  # noqa: B023
        await page.goto(f"{BASE}/#token={TOKEN}")
        monitoring, made = page.locator("#tasks-monitoring"), page.locator("#tasks-artifacts")
        needs = page.locator("#needs")
        await expect(needs.locator(".card.task", has_text="E41 backfill")).to_be_visible(
            timeout=15000
        )
        row = monitoring.locator(".card.task", has_text="E41 rebuild the search index")
        await expect(row).to_be_visible(timeout=15000)
        for width in WIDTHS:
            await page.set_viewport_size({"width": width, "height": 1000})
            if width < 1100:
                await page.locator("#home-tabs [data-tab=needs]").click()
            await page.wait_for_timeout(150)
            name = f"quality-{width}-{scheme}"
            await needs.screenshot(path=folder / f"{name}-needs.png")
            await quality.check(
                page, f"needs unknown runs {width} {scheme}", "#needs", KNOWN_ROW_FINDINGS
            )
            if width < 1100:
                await page.locator("#home-tabs [data-tab=tasks]").click()
                await page.wait_for_timeout(150)
            await monitoring.screenshot(path=folder / f"{name}-monitoring.png")
            await quality.check(
                page, f"monitoring {width} {scheme}", "#tasks-monitoring", KNOWN_ROW_FINDINGS
            )
            await row.get_by_role("button", name=re.compile("More actions")).click()
            await page.wait_for_timeout(150)
            await quality.check(
                page, f"run menu {width} {scheme}", "#tasks-monitoring", KNOWN_ROW_FINDINGS
            )
            await monitoring.screenshot(path=folder / f"{name}-menu.png")
            await page.keyboard.press("Escape")
            for fold in await made.locator(".a-fold").all():
                for state in ("false", "true"):
                    await expect(fold).to_have_attribute("aria-expanded", state)
                    await made.locator(":scope > .items").evaluate("n => n.scrollTop = 0")
                    task = await fold.evaluate("n => n.closest('.artifact').dataset.task")
                    shown = "open" if state == "true" else "folded"
                    await made.screenshot(path=folder / f"{name}-artifacts-{task}-{shown}.png")
                    await quality.check(
                        page, f"artifacts {width} {scheme} {task} {state}", "#tasks-artifacts",
                        KNOWN_ROW_FINDINGS,
                    )  # fmt: skip
                    await fold.click()
        await quality.contrast(page, f"needs {scheme}", "#needs")
        await quality.contrast(page, f"monitoring {scheme}", "#tasks-monitoring")
        await quality.contrast(page, f"artifacts {scheme}", "#tasks-artifacts")
        quality.failures += [f"page error {scheme}: {error}" for error in errors]
        await context.close()


async def life_views(runtime, browser, quality: Quality):
    """QA round 7, a task's life: the cards that need the owner for it (settings to approve,
    a failed check-back on accepted work, a send cut short, a failed task with Dismiss), a
    failed task under Recently finished, failing Watch scans in the sidebar, and Other Copilot
    sessions with none; 800 to 1440 px, light and dark (browser-shots/qa7-life/)."""
    from ui_edges import life_seeded

    folder = SHOTS / "qa7-life"
    folder.mkdir(exist_ok=True)
    found = await life_seeded(runtime)
    for scheme in ("light", "dark"):
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1400}, color_scheme=scheme, bypass_csp=True
        )
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))  # noqa: B023
        await page.goto(f"{BASE}/#token={TOKEN}")
        needs = page.locator("#needs")
        await expect(
            needs.locator(".card.task", has_text="QA7 fix the login timeout")
        ).to_be_visible(timeout=15000)
        for width in WIDTHS:
            await page.set_viewport_size({"width": width, "height": 1400})
            if width < 1100:
                await page.locator("#home-tabs [data-tab=needs]").click()
            await page.wait_for_timeout(150)
            name = f"quality-{width}-{scheme}"
            await needs.screenshot(path=folder / f"{name}-needs.png")
            await quality.check(page, f"life needs {width} {scheme}", "#needs", KNOWN_ROW_FINDINGS)
            if width < 1100:
                await page.locator("#home-tabs [data-tab=tasks]").click()
                await page.wait_for_timeout(150)
            recent = page.locator("#tasks-recent")
            await recent.screenshot(path=folder / f"{name}-recent.png")
            await quality.check(
                page, f"life recent {width} {scheme}", "#tasks-recent", KNOWN_ROW_FINDINGS
            )
            await page.locator("#sidebar").screenshot(path=folder / f"{name}-sidebar.png")
            await quality.check(page, f"life sidebar {width} {scheme}", "#sidebar")
        await quality.contrast(page, f"life needs {scheme}", "#needs")
        await quality.contrast(page, f"life sidebar {scheme}", "#sidebar")
        quality.failures += [f"page error {scheme}: {error}" for error in errors]
        await context.close()
    runtime.settings_refusal = found["refusal"]
    runtime.store.delete("watch_failed")


async def approvals_views(runtime, browser, quality: Quality):
    """E123: Your OK (a line per approval, the safe ones marked, a card opened on Details) and
    Handled for you, open; 800 to 1440 px, light and dark (browser-shots/qa8-approvals/)."""
    from ui_edges import approvals_seeded

    folder = SHOTS / "qa8-approvals"
    folder.mkdir(exist_ok=True)
    found = await approvals_seeded(runtime)
    for scheme in ("light", "dark"):
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1400}, color_scheme=scheme, bypass_csp=True
        )
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))  # noqa: B023
        await page.goto(f"{BASE}/#token={TOKEN}")
        needs = page.locator("#needs")
        ok = needs.locator(".card.your-ok")
        await expect(ok.locator(".ok-row")).to_have_count(6, timeout=15000)
        await needs.locator("details.handled > summary").click()
        merge = ok.locator(f'.ok-row[data-id="{found["merge"]["id"]}"]')
        await merge.locator(".more-button").click()
        await merge.get_by_role("menuitem", name="Details").click()
        await expect(ok.locator(".ok-details .card.merge")).to_be_visible()
        for width in WIDTHS:
            await page.set_viewport_size({"width": width, "height": 1400})
            if width < 1100:
                await page.locator("#home-tabs [data-tab=needs]").click()
            await page.wait_for_timeout(150)
            await needs.screenshot(path=folder / f"quality-{width}-{scheme}-needs.png")
            await quality.check(page, f"approvals {width} {scheme}", "#needs", KNOWN_ROW_FINDINGS)
        await quality.contrast(page, f"approvals {scheme}", "#needs")
        quality.failures += [f"page error {scheme}: {error}" for error in errors]
        await context.close()
    for draft in runtime.store.drafts():
        runtime.store.update_draft(draft["id"], status="expired")
    runtime.publish_state()


async def teams_views(runtime, browser, quality: Quality):
    """E129, E130: Home's chat with what workers told each other folded (one line: how many,
    who, what waits) and the routing receipts folded, open; the Teams strip and a question held
    up under Needs you; a team's view; a worker's Team tab; 800 to 1440 px, light and dark
    (browser-shots/teams/)."""
    from ui_edges import teams_held_up, teams_seeded

    folder = SHOTS / "teams"
    folder.mkdir(exist_ok=True)
    seeded = await teams_seeded(runtime)
    await teams_held_up(runtime, seeded)
    team = f"team-{seeded['scout'][:8]}"
    for scheme in ("light", "dark"):
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1000}, color_scheme=scheme, bypass_csp=True
        )
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))  # noqa: B023
        await page.goto(f"{BASE}/#token={TOKEN}")
        timeline = page.locator("#timeline")
        fold = timeline.locator(":scope > .fold.team", has_text="5 team messages")
        await expect(fold).to_be_visible(timeout=15000)
        await fold.locator(".fold-line button.line").click()
        routing = timeline.locator(":scope > .fold.routing").last
        await routing.locator(".fold-line button.line").click()
        for width in WIDTHS:
            await page.set_viewport_size({"width": width, "height": 1000})
            await page.wait_for_timeout(150)
            await timeline.screenshot(path=folder / f"quality-{width}-{scheme}-chat.png")
            await quality.check(page, f"teams chat {width} {scheme}", "#timeline")
        await quality.contrast(page, f"teams chat {scheme}", "#timeline")
        await expect(page.locator(f'#teams-strip .team-row[data-team="{team}"]')).to_be_visible()
        for width in WIDTHS:
            await page.set_viewport_size({"width": width, "height": 1000})
            await page.wait_for_timeout(150)
            for tab, part in (("needs", "#needs"), ("tasks", "#teams-strip")):
                if width < 1100:
                    await page.locator(f"#home-tabs [data-tab={tab}]").click()
                    await page.wait_for_timeout(150)
                await page.locator(part).screenshot(
                    path=folder / f"quality-{width}-{scheme}-{tab}.png"
                )
                await quality.check(page, f"teams {tab} {width} {scheme}", part, KNOWN_ROW_FINDINGS)
        await quality.contrast(page, f"teams needs {scheme}", "#needs")
        await quality.contrast(page, f"teams strip {scheme}", "#teams-strip")
        await page.locator(f'#teams-strip .team-row[data-team="{team}"]').click()
        await expect(page.locator("#timeline .team-view .team-item")).to_have_count(6)
        for width in WIDTHS:
            await page.set_viewport_size({"width": width, "height": 1000})
            await page.wait_for_timeout(150)
            await page.locator("#main").screenshot(
                path=folder / f"quality-{width}-{scheme}-team.png"
            )
            await quality.check(page, f"team view {width} {scheme}", "#main")
        await quality.contrast(page, f"team view {scheme}", "#main")
        await page.locator(".team-members .member", has_text="Column Maker").click()
        await page.locator("#worker-tabs").get_by_role("tab", name=re.compile("^Team")).click()
        for width in WIDTHS:
            await page.set_viewport_size({"width": width, "height": 1000})
            await page.wait_for_timeout(150)
            await page.locator("#main").screenshot(
                path=folder / f"quality-{width}-{scheme}-worker-team.png"
            )
            await quality.check(page, f"worker team tab {width} {scheme}", "#main")
        await quality.contrast(page, f"worker team tab {scheme}", "#main")
        quality.failures += [f"page error {scheme}: {error}" for error in errors]
        await context.close()


async def relations_views(runtime, browser, quality: Quality):
    """E130: a team its tasks' relations make: Home's line with its cue, the team's view (the
    counts, a row per task with one relation) and a task's lineage open, a worker's Team tab
    with its task's lineage; 800 to 1440 px, light and dark (browser-shots/relations/)."""
    from ui_edges import relations_seeded

    folder = SHOTS / "relations"
    folder.mkdir(exist_ok=True)
    seeded = await relations_seeded(runtime)
    team = f"team-{seeded['api'][:8]}"
    for scheme in ("light", "dark"):
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1000}, color_scheme=scheme, bypass_csp=True
        )
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))  # noqa: B023
        await page.goto(f"{BASE}/#token={TOKEN}")
        row = page.locator(f'#teams-strip .team-row[data-team="{team}"]')
        await expect(row).to_be_visible(timeout=15000)
        for width in WIDTHS:
            await page.set_viewport_size({"width": width, "height": 1000})
            await page.wait_for_timeout(150)
            if width < 1100:
                await page.locator("#home-tabs [data-tab=tasks]").click()
                await page.wait_for_timeout(150)
            await page.locator("#teams-strip").screenshot(
                path=folder / f"quality-{width}-{scheme}-strip.png"
            )
            await quality.check(page, f"relations strip {width} {scheme}", "#teams-strip")
        await quality.contrast(page, f"relations strip {scheme}", "#teams-strip")
        await row.click()
        view = page.locator("#timeline .team-view")
        await expect(view.locator(".team-tasks .trow")).to_have_count(6)
        await view.locator(f'.trow[data-task="{seeded["ui_task"]}"] .main').click()
        await expect(view.locator(".lineage .lnode.self")).to_have_count(1)
        for width in WIDTHS:
            await page.set_viewport_size({"width": width, "height": 1000})
            await page.wait_for_timeout(150)
            await page.locator("#main").screenshot(
                path=folder / f"quality-{width}-{scheme}-team.png"
            )
            await quality.check(page, f"relations team view {width} {scheme}", "#main")
        await quality.contrast(page, f"relations team view {scheme}", "#main")
        await page.locator("#session-list .item", has_text="Release Notes").first.click()
        await page.locator("#worker-tabs").get_by_role("tab", name=re.compile("^Team")).click()
        await expect(page.locator("#timeline > .team-lineage .lnode.self")).to_be_visible()
        for width in WIDTHS:
            await page.set_viewport_size({"width": width, "height": 1000})
            await page.wait_for_timeout(150)
            await page.locator("#main").screenshot(
                path=folder / f"quality-{width}-{scheme}-worker-team.png"
            )
            await quality.check(page, f"relations worker team tab {width} {scheme}", "#main")
        await quality.contrast(page, f"relations worker team tab {scheme}", "#main")
        quality.failures += [f"page error {scheme}: {error}" for error in errors]
        await context.close()


async def usage_views(runtime, browser, quality: Quality):
    """E131-E136, Usage: the totals as Copilot counts them, By session with a worker's tasks
    unfolded, By task and what the spend went to; 800 and 1280 px, light and dark
    (browser-shots/usage/)."""
    from ui_edges import usage_seeded

    folder = SHOTS / "usage"
    folder.mkdir(exist_ok=True)
    usage_seeded(runtime)
    for scheme in ("light", "dark"):
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900}, color_scheme=scheme, bypass_csp=True
        )
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))  # noqa: B023
        await page.goto(f"{BASE}/#token={TOKEN}")
        await page.locator("#usage-button").click()
        dialog = page.locator("#usage-dialog")
        await dialog.locator('[data-range="30"]').click()
        toggle = dialog.locator("button", has_text="worker · 3 tasks")
        await toggle.click()
        await expect(toggle).to_have_attribute("aria-expanded", "true")
        # The dialog scrolls (its form, in some layouts): its top, then its end.
        scroll = (
            "(dialog, top) => { for (const box of [dialog, dialog.querySelector('form')]) "
            "box.scrollTo(0, top ? 0 : box.scrollHeight); }"
        )
        for width in (1280, 800):
            await page.set_viewport_size({"width": width, "height": 900})
            await dialog.evaluate(scroll, True)
            await page.wait_for_timeout(150)
            await page.screenshot(path=folder / f"quality-{width}-{scheme}.png")
            await dialog.evaluate(scroll, False)
            await page.wait_for_timeout(150)
            await page.screenshot(path=folder / f"quality-{width}-{scheme}-end.png")
            await quality.check(page, f"usage {width} {scheme}", "#usage-dialog")
        await quality.contrast(page, f"usage {scheme}", "#usage-dialog")
        quality.failures += [f"page error {scheme}: {error}" for error in errors]
        await context.close()


async def signed_out(route):
    codex = {
        "name": "codex", "label": "Codex", "enabled": False, "model": "", "limit": 0,
        "found": True, "state": "", "models": [],
        "problem": "Codex is not signed in: sign in from Settings → Coding agents.",
    }  # fmt: skip
    claude = codex | {
        "name": "claude", "label": "Claude",
        "problem": "Not signed in to Claude Code: sign in from Settings → Coding agents.",
    }  # fmt: skip
    answer = {"agents": [codex, claude], "new_workers": "copilot", "sandboxed": False}
    await route.fulfill(json=answer)


async def agents_views(runtime, browser, quality: Quality):
    """Other coding agents (docs/design/more-agents.md 5.10, the agent picker): Settings' Coding
    agents tab (Codex's and Claude's rows), signed out, found, being turned on, on (where new
    workers start, its Model and Limit); a worker on Codex, its header and ⋯; 800 to 1440 px, light and dark
    (browser-shots/agents-m2/quality-*)."""
    from copilot.tools import ToolInvocation
    from ui_edges import agents_saved, codex_off_path, codex_on_path, until

    folder = SHOTS / "agents-m2"
    folder.mkdir(exist_ok=True)
    codex, _ = codex_on_path()
    runtime.found_cache = (float("-inf"), [])  # Looked at the PATH again: now they are on it.
    try:
        for scheme in ("light", "dark"):
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900}, color_scheme=scheme, bypass_csp=True
            )
            page = await context.new_page()
            await page.goto(f"{BASE}/#token={TOKEN}")
            await expect(page.locator("#application")).to_be_visible()
            for state in ("signed-out", "found", "enabling", "on-open"):
                if state == "signed-out":
                    # One that cannot be turned on: it looks it, in both themes.
                    await page.route("**/api/agents", signed_out)
                await page.locator("#settings-button").click()
                await page.locator("#settings-tabs [data-tab=agents]").click()
                group = page.locator("#settings-pane .tab[data-tab=agents]")
                if state == "signed-out":
                    await expect(group.locator(".agent-row.disabled .agent-problem")).to_have_count(
                        2
                    )
                if state in {"found", "enabling"}:
                    await expect(group.locator(".agent-row small[role=status]")).to_have_text(
                        [
                            "Ready · Uses your ChatGPT sign-in",
                            "Ready · Uses your Claude plan",
                        ],
                        timeout=20000,
                    )
                if state == "enabling":
                    await group.locator("input[name=agent_codex_enabled]").check()
                if state == "on-open":
                    await expect(group.locator("input[name=agent_codex_enabled]")).to_be_checked(
                        timeout=20000
                    )
                    await expect(group.locator("#agents-choice")).to_be_visible()
                for width in WIDTHS:
                    await page.set_viewport_size({"width": width, "height": 900})
                    await page.wait_for_timeout(100)
                    name = f"quality-settings-{state}-{width}-{scheme}"
                    await page.screenshot(path=folder / f"{name}.png")
                    await quality.check(page, f"agents {name}", "#settings-dialog")
                await quality.contrast(
                    page, f"agents settings {state} {scheme}", "#settings-dialog"
                )
                await page.set_viewport_size({"width": 1280, "height": 900})
                if state == "enabling":
                    await page.locator("#settings-form footer button[type=submit]").click()
                    await expect(page.locator("#settings-dialog")).to_be_hidden()
                    await until(lambda: bool(runtime.agents_on()), 10)
                else:
                    await page.locator("#settings-cancel").click()
                if state == "signed-out":
                    await page.unroute("**/api/agents", signed_out)
            # A worker on Codex: its header and its ⋯.
            router = runtime.store.get("router_id")
            result = await runtime.toolset.spawn_on(["codex"]).handler(
                ToolInvocation(session_id=router, tool_call_id="t", tool_name="spawn_worker",
                               arguments={"title": f"Check the cache ({scheme})", "why_new": "t",
                                          "instructions": "Report done: Checked.", "agent": "codex"})
            )  # fmt: skip
            worker = json.loads(result.text_result_for_llm)["session_id"]  # Queued, or at work.
            await page.evaluate(
                "id => import('/static/session.js').then((m) => m.select(id))", worker
            )
            await expect(page.locator("#session-meta")).to_contain_text("Codex", timeout=10000)
            for view in ("header", "menu"):
                if view == "menu":
                    await page.locator("#session-more-button").click()
                    await expect(page.locator("#session-model option").nth(1)).to_have_text(
                        "GPT-TEST"
                    )
                for width in WIDTHS:
                    await page.set_viewport_size({"width": width, "height": 900})
                    await page.wait_for_timeout(100)
                    name = f"quality-worker-{view}-{width}-{scheme}"
                    await page.screenshot(path=folder / f"{name}.png")
                    await quality.check(page, f"agents {name}", "#session-header")
                await quality.contrast(page, f"agents worker {view} {scheme}", "#session-header")
                await page.set_viewport_size({"width": 1280, "height": 900})
            await page.keyboard.press("Escape")
            # The agent picker: @'s list (every agent, its status), the chip's popover.
            await page.evaluate("() => import('/static/session.js').then((m) => m.select('home'))")
            if await page.locator("#home-tabs").is_visible():
                await page.locator("#home-tabs [data-tab=chat]").click()
            box = page.locator("#composer-input")
            await box.fill("@")
            await box.press("End")
            await expect(page.locator("#mention-list li.agent")).to_have_count(3)
            for view in ("list", "popover"):
                if view == "popover":
                    await page.locator("#mention-list li.agent", has_text="Codex").click()
                    await page.locator("#composer-to .agent-chip").click()
                    await expect(page.locator(".agent-popover select").first).to_be_visible()
                scope = "#composer" if view == "list" else ".agent-popover"
                for width in WIDTHS:
                    await page.set_viewport_size({"width": width, "height": 900})
                    await page.wait_for_timeout(100)
                    name = f"quality-picker-{view}-{width}-{scheme}"
                    await page.screenshot(path=folder / f"{name}.png")
                    await quality.check(page, f"agents {name}", scope)
                await quality.contrast(page, f"agents picker {view} {scheme}", scope)
                await page.set_viewport_size({"width": 1280, "height": 900})
            await page.keyboard.press("Escape")
            await page.locator("#composer-to button.clear").click()
            await agents_saved(runtime, {"codex": {"enabled": False}})
            await context.close()
    finally:
        await agents_saved(runtime, {"codex": {"enabled": False}})
        codex_off_path(codex)
    log("Coding agents: the tab's states, a Codex worker, the picker; browser-shots/agents-m2/")


# On Home with worker PRs, what today's rows were made with (a PR's receipt link, 18 px tall)
# and Artifacts' known findings (KNOWN_ROW_FINDINGS, its 10 px kind label among the sizes): not
# Prepared for you's; anything else found there fails.
KNOWN_HOME_FINDINGS = re.compile(
    rf"{KNOWN_ROW_FINDINGS.pattern}|^a\.receipt\.pr\S* .* is 18px tall$"
    r'|^4 font sizes: .*10px \(span\.a-kind "[^"]+"(?:, span\.a-kind "[^"]+")*\)$'
)


# Prepared for you's lines on Home that are cut off (an ellipsis hides part of them).
CUT_LINES = r"""
() => [...document.querySelectorAll("#prepared .prepared-title, #prepared .prepared-line > *, #needs .card.task.marker-kept-paused .row-line .why, #needs .card.task.marker-prepared .row-line .why, #needs .card.task.marker-review .row-line .why")]
  .filter((node) => node.getBoundingClientRect().width > 0 && node.scrollWidth > node.clientWidth + 1)
  .map((node) => node.textContent.trim())
"""


async def shepherd_views(runtime, browser, quality: Quality):
    """Prepared for you (shepherd.py): Home with its section (three rows, then All found), a
    worker PR's row with a fix ready (its line, Review fix), one paused, one that can't run
    steps here; the fix's review (checked on frozen commits, its diff); the switch in Settings →
    General and its tab; 800 to 1440 px, light and dark (browser-shots/shepherd/)."""
    from shepherd_world import seed_page

    folder = SHOTS / "shepherd"
    folder.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cos-shepherd-") as root:
        seeded = await seed_page(runtime, Path(root))
        for scheme in ("light", "dark"):
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900}, color_scheme=scheme, bypass_csp=True
            )
            page = await context.new_page()
            await page.goto(f"{BASE}/#token={TOKEN}")
            await expect(page.locator("#application")).to_be_visible()
            prepared = page.locator("#prepared")
            await expect(prepared.locator(".prepared-row")).to_have_count(3, timeout=10000)
            await expect(prepared.locator(".prepared-foot button")).to_have_text(
                "1 more · All found…"
            )
            row = page.locator("#needs .card.task", has_text="Fix the login timeout")
            await expect(row.get_by_role("button", name="Review fix")).to_be_visible()
            for width in WIDTHS:
                await page.set_viewport_size({"width": width, "height": 900})
                if width < 1100:
                    await page.locator("#home-tabs [data-tab=needs]").click()
                await page.wait_for_timeout(150)
                if width == 800:
                    # Its lines are whole at 800 px: nothing of their meaning cut off.
                    cut = await page.evaluate(CUT_LINES)
                    quality.failures += [f"shepherd 800 {scheme}: cut off: {text}" for text in cut]
                    # Its rows, as the owner reads them there.
                    rows = page.locator("#needs .card.task.marker-kept-paused")
                    await rows.scroll_into_view_if_needed()
                    await page.screenshot(path=folder / f"rows-800-{scheme}.png")
                    await page.locator("#prepared").scroll_into_view_if_needed()
                    await page.screenshot(path=folder / f"prepared-800-{scheme}.png")
                    await page.locator("#needs").scroll_into_view_if_needed()
                name = f"home-{width}-{scheme}"
                await page.screenshot(path=folder / f"{name}.png")
                await quality.check(page, f"shepherd {name}", known=KNOWN_HOME_FINDINGS)
            await quality.contrast(page, f"shepherd home {scheme}")
            await page.set_viewport_size({"width": 1280, "height": 900})
            await page.locator("#needs").screenshot(path=folder / f"needs-1280-{scheme}.png")
            await prepared.screenshot(path=folder / f"prepared-1280-{scheme}.png")
            await prepared.locator(".prepared-foot button").click()
            await expect(prepared.locator(".prepared-row.found")).to_have_count(6)
            await prepared.scroll_into_view_if_needed()
            await page.screenshot(path=folder / f"all-found-1280-{scheme}.png")
            await quality.check(page, "shepherd all found", known=KNOWN_HOME_FINDINGS)
            await prepared.get_by_role("button", name="Done").click()
            await row.get_by_role("button", name="Review fix").click()
            dialog = page.locator(".plan-dialog")
            await expect(dialog.locator(".plan-evidence")).to_contain_text("test_timeout")
            for width in (800, 1280):
                await page.set_viewport_size({"width": width, "height": 900})
                await page.wait_for_timeout(150)
                await page.screenshot(path=folder / f"plan-{width}-{scheme}.png")
                await quality.check(page, f"shepherd plan {width} {scheme}", ".plan-dialog")
            await quality.contrast(page, f"shepherd plan {scheme}", ".plan-dialog")
            await dialog.get_by_role("button", name="Close").click()
            await page.set_viewport_size({"width": 1280, "height": 900})
            await row.locator(".more-button").click()
            menu = row.locator(".menu")
            await expect(menu).to_be_visible()
            for item in ("Stop looking after this PR", "Merge when ready", "What it did"):
                await expect(menu.get_by_role("menuitem", name=item)).to_be_visible()
            await page.wait_for_timeout(150)
            await page.screenshot(path=folder / f"row-menu-1280-{scheme}.png")
            await quality.check(page, f"shepherd row menu {scheme}", "#needs", KNOWN_HOME_FINDINGS)
            await page.keyboard.press("Escape")
            await row.locator(".more-button").click()
            await row.get_by_role("menuitem", name="Accept this commit…").click()
            confirm = page.locator(".confirm-dialog")
            await expect(confirm).to_be_visible()
            await page.wait_for_timeout(250)
            await page.screenshot(path=folder / f"accept-commit-1280-{scheme}.png")
            await quality.check(page, f"shepherd accept {scheme}", ".confirm-dialog")
            await quality.contrast(page, f"shepherd accept {scheme}", ".confirm-dialog")
            await confirm.get_by_role("button", name="Cancel").click()
            await page.locator("#settings-button").click()
            await page.locator("#settings-tabs [data-tab=general]").click()
            await page.screenshot(path=folder / f"settings-general-1280-{scheme}.png")
            await page.locator("#settings-tabs [data-tab=proactive]").click()
            for width in (800, 1280):
                await page.set_viewport_size({"width": width, "height": 900})
                await page.wait_for_timeout(100)
                await page.screenshot(path=folder / f"settings-tab-{width}-{scheme}.png")
                await quality.check(page, f"shepherd settings {width} {scheme}", "#settings-dialog")
                await page.locator("#settings-pane .repo-list").scroll_into_view_if_needed()
                await page.screenshot(path=folder / f"settings-repos-{width}-{scheme}.png")
                await quality.check(page, f"shepherd repos {width} {scheme}", "#settings-dialog")
            await quality.contrast(page, f"shepherd settings {scheme}", "#settings-dialog")
            await page.locator("#settings-cancel").click()
            await context.close()
        assert seeded["step"]
    log("Prepared for you: Home, a fix's review, its Settings tab (browser-shots/shepherd/)")


async def main():
    from chiefofstaff import machine

    # A disk as roomy as the pages expect, whatever this machine's is (a low one adds the
    # Router's note under Needs you); a round about storage sets its own.
    machine.read_disk = lambda paths: {"free": 200 * 10**9, "total": 500 * 10**9, "path": "/"}
    phase = sys.argv[1] if len(sys.argv) > 1 else "check"
    SHOTS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cos-quality-") as temporary:
        # The owner's own agents are never found here: stand-ins are put on the PATH where used.
        os.environ["PATH"] = without_agents(os.environ["PATH"], Path(temporary) / "bin")
        holder: dict = {}

        def factory(store, bus):
            async def notifier(title, text):
                pass

            client = FakeClient()
            runtime = Runtime(
                store, bus, backend_factory=lambda settings: client, notifier=notifier
            )
            runtime.slack = SlackSource(store, runtime, tokens=FakeTokens(""), mcp=FakeSlackMcp())
            holder.update(runtime=runtime, client=client)
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
        code = Path(temporary) / "code"
        code.mkdir()
        seeded = await seed(runtime, holder["client"], code)
        quality = Quality()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            # `UI_QUALITY_ONLY=shepherd`: only Prepared for you's views.
            if os.environ.get("UI_QUALITY_ONLY") != "shepherd":
                await report_views(runtime, browser, quality)
                await run(runtime, seeded, browser, quality, phase)
                log("Home, a worker, Settings and Usage: 800 to 1440 px, light and dark")
                await cards(runtime, seeded, browser, quality, phase)
                log("every kind of card that needs the owner")
                await others(runtime, holder["client"], browser, quality)
                await send_views(runtime, browser, quality)
                await runs_views(runtime, browser, quality)
                log("runs nothing told of for a day, a task's runs under its PR (qa-e41/)")
                await agents_views(runtime, browser, quality)
                await life_views(runtime, browser, quality)
                log(
                    "a task's life: its cards, a failed task out of Needs you, failing scans "
                    "(qa7-life/)"
                )
                await approvals_views(runtime, browser, quality)
                log("Your OK and Handled for you (qa8-approvals/)")
                await teams_views(runtime, browser, quality)
                log("what workers told each other, folded on Home (teams/)")
                await usage_views(runtime, browser, quality)
                log("Usage: as Copilot counts it, by task, a worker's tasks (usage/)")
                await relations_views(runtime, browser, quality)
                log("how a team's tasks are related (relations/)")
            await shepherd_views(runtime, browser, quality)
            await browser.close()
        server.should_exit = True
        await serving
    if quality.failures:
        print(f"{len(quality.failures)} problems:")
        for failure in quality.failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("UI QUALITY PASSED")


if __name__ == "__main__":
    asyncio.run(main())
