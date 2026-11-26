"""Live test of 0.1.58's composer against real Copilot (owner report "我输了回车 没有反应 又输了一
遍": Enter on Home showed nothing for seconds, so they sent it again and the Router got it twice).

K1  On Home, a message to the Router: its bubble is there at once, before the send is answered,
    then it is the stored message (an id) in the same place, once (never two bubbles, never
    none), and says where it stands as the Router takes it, ending Handled; the Router answers.
    Timed: Enter to the bubble, Enter to the send's answer, Enter to the message stored.
K2  The same words again while the first is on its way (its send held 3 s in the page, as a
    busy computer holds it): its bubble stands out, one send, one stored message, one answer.
K3  The page reloads while a send is on its way (it reached Chief of Staff, its answer was cut
    off): back, its words are no draft (no "Not sent" line), the message is there once, and the
    Router answers it once.

Uses your signed-in Copilot CLI (a Copilot home of its own, made from your config) and consumes a
few premium requests (LIVE_COMPOSER_MODEL: claude-haiku-5.5 by default); about 2 minutes. The
Copilot sessions it creates are deleted at the end. Set TMPDIR to a scratch folder of yours;
LIVE_COMPOSER_PORT moves its ports (the app's, and the stand-in Slack's 200 above it).

    LIVE_COMPOSER_PORT=18812 uv run --with playwright python scripts/live_composer.py [K1] [K2] [K3]
"""

import asyncio
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, expect

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e
import live_parity  # noqa: F401  (it sets its own port: this one is set after)

live_e2e.PORT = int(os.environ.get("LIVE_COMPOSER_PORT", "18812"))
live_e2e.SLACK_PORT = live_e2e.PORT + 200
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from live_e2e import App, cleanup, data_dir, log, transcript  # noqa: E402
from live_parity import setup  # noqa: E402

MODEL = os.environ.get("LIVE_COMPOSER_MODEL", "claude-haiku-5.5")
FIRST = "What is 17 + 25? Answer with the number only, and start no worker."
AGAIN = "And 17 + 26? The number only, start no worker."
RELOADED = "And 17 + 27? The number only, start no worker."
# In the page: when Enter went down; the owner's bubbles with a text at each change of the
# timeline (never none once shown, never two), when the first showed, and where it stood each
# time that changed.
WATCH = """(text) => {
  const bubbles = () => [...document.querySelectorAll('#timeline .msg.owner')]
    .filter((node) => node.querySelector('.text')?.textContent === text);
  window.seen = { counts: [bubbles().length], shownAt: null, enterAt: null, states: [] };
  window.watching?.disconnect();
  window.watching = new MutationObserver(() => {
    const found = bubbles();
    window.seen.counts.push(found.length);
    if (found.length && window.seen.shownAt === null) window.seen.shownAt = performance.now();
    const state = found[0]?.dataset.delivery || '';
    if (found.length && window.seen.states.at(-1) !== state) window.seen.states.push(state);
  });
  window.watching.observe(document.querySelector('#timeline'),
    { childList: true, subtree: true, attributes: true, attributeFilter: ['data-delivery'] });
  document.querySelector('#composer-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && window.seen.enterAt === null) {
      window.seen.enterAt = performance.now();
      window.seen.enterWall = Date.now();
    }
  }, { capture: true });
}"""
ANSWERED = """() => { const sends = performance.getEntriesByType('resource')
  .filter((entry) => entry.initiatorType === 'fetch' && /\\/messages$/.test(entry.name));
  return sends.length ? sends[sends.length - 1].responseEnd : null; }"""


class Run:
    def __init__(self, app: App, page):
        self.app = app
        self.page = page
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = ""):
        self.results.append((name, ok, detail))
        log(f"{'PASS' if ok else 'FAIL'} {name}{f': {detail}' if detail else ''}")

    def bubble(self, text: str):
        return self.page.locator("#timeline .msg.owner").filter(
            has=self.page.locator(".text", has_text=re.compile(f"^{re.escape(text)}$"))
        )

    async def stored(self, router: str, text: str) -> list[dict]:
        messages = await self.app.get(f"/api/sessions/{router}/messages")
        return [m for m in messages if m["kind"] == "owner" and m["text"] == text]

    async def answer_after(self, router: str, message_id: int, words: str) -> str:
        for _ in range(60):
            messages = await self.app.get(f"/api/sessions/{router}/messages?after={message_id}")
            said = [m["text"] for m in messages if m["kind"] == "assistant"]
            if any(words in text for text in said):
                return next(text for text in said if words in text)
            await asyncio.sleep(2)
        return ""


def held(counts: list[int]) -> bool:
    shown = counts[counts.index(1) :] if 1 in counts else []
    return bool(shown) and all(count == 1 for count in shown)


async def k1_shown_at_once_then_stored_in_place(run: Run, router: str):
    page = run.page
    await page.evaluate(WATCH, FIRST)
    box = page.locator("#composer-input")
    await box.fill(FIRST)
    async with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/messages")
    ) as answer:
        await box.press("Enter")
    sent = await (await answer.value).json()
    bubble = run.bubble(FIRST)
    await expect(bubble).to_have_attribute("data-id", re.compile(r"^\d+$"), timeout=60000)
    seen = await page.evaluate("window.seen")
    answered = await page.evaluate(ANSWERED)
    shown = seen["shownAt"] - seen["enterAt"]
    took = answered - seen["enterAt"] if answered else None
    stored_at = datetime.fromisoformat(sent["created_at"]).timestamp() * 1000
    run.check(
        "K1 the bubble is there before the send is answered",
        shown < 300 and took is not None and seen["shownAt"] < answered,
        f"bubble {shown:.0f} ms after Enter; the send answered after {took or 0:.0f} ms; "
        f"stored {stored_at - seen['enterWall']:.0f} ms after Enter",
    )
    run.check(
        "K1 the stored one in its place",
        await bubble.count() == 1 and await bubble.get_attribute("data-id") == str(sent["id"]),
    )
    await expect(bubble).to_have_attribute("data-delivery", "processed", timeout=180000)
    said = await run.answer_after(router, sent["id"], "42")
    stored = await run.stored(router, FIRST)
    run.check(
        "K1 Handled, answered, stored once with the page's id",
        bool(said)
        and len(stored) == 1
        and re.fullmatch(r"[0-9a-f]{32}", stored[0]["meta"].get("client_id", "")) is not None,
        f"answer {said[:60]!r}; stored {len(stored)}",
    )
    seen = await page.evaluate("window.seen")
    real = [state for state in seen["states"] if state != "sending"]
    after = seen["states"][seen["states"].index(real[0]) :] if real else []
    run.check(
        "K1 once all along (never two bubbles, never none), Sending… never after a real status",
        held(seen["counts"]) and "sending" not in after and seen["states"][-1] == "processed",
        f"states {seen['states']}",
    )


async def k2_the_same_again_is_not_sent_twice(run: Run, router: str):
    page = run.page
    posts: list[dict] = []

    async def slow(route):
        if route.request.method != "POST":
            await route.continue_()
            return
        posts.append(route.request.post_data_json)
        await asyncio.sleep(3)  # As a busy computer holds it.
        await route.continue_()

    await page.route(f"**/api/sessions/{router}/messages", slow)
    await page.evaluate(WATCH, AGAIN)
    box = page.locator("#composer-input")
    await box.fill(AGAIN)
    await box.press("Enter")
    bubble = run.bubble(AGAIN)
    await expect(bubble.locator(".delivery")).to_have_text("Sending…")
    await expect(bubble.locator(".delivery")).not_to_have_class(re.compile(r"\blater\b"))
    await box.fill(AGAIN)
    await box.press("Enter")
    await expect(bubble).to_have_class(re.compile(r"\bflash\b"))
    await expect(bubble).to_have_attribute("data-id", re.compile(r"^\d+$"), timeout=60000)
    await page.unroute(f"**/api/sessions/{router}/messages", slow)
    stored = await run.stored(router, AGAIN)
    said = await run.answer_after(router, stored[0]["id"], "43") if stored else ""
    await page.wait_for_timeout(3000)
    run.check(
        "K2 the same words while on its way: it stood out, one send, one message, one answer",
        len(posts) == 1 and len(stored) == 1 and bool(said) and await bubble.count() == 1,
        f"sends {len(posts)}, stored {len(stored)}, answer {said[:40]!r}",
    )
    run.check("K2 held: never two bubbles", held((await page.evaluate("window.seen"))["counts"]))


async def k3_a_reload_mid_send_leaves_no_draft(run: Run, router: str):
    page = run.page
    reached = asyncio.Event()

    async def cut_off(route):
        if route.request.method != "POST":
            await route.continue_()
            return
        await route.fetch()  # Chief of Staff has it.
        reached.set()
        await route.abort()  # Its answer never reaches the page.

    url = f"**/api/sessions/{router}/messages"
    await page.route(url, cut_off)
    box = page.locator("#composer-input")
    await box.fill(RELOADED)
    await box.press("Enter")
    await asyncio.wait_for(reached.wait(), 60)
    await page.reload()
    await page.unroute(url, cut_off)
    await expect(page.locator("#application")).to_be_visible()
    bubble = run.bubble(RELOADED)
    await expect(bubble).to_have_count(1, timeout=30000)
    await page.wait_for_function("() => !localStorage.getItem('cos.unsent')", timeout=30000)
    stored = await run.stored(router, RELOADED)
    said = await run.answer_after(router, stored[0]["id"], "44") if stored else ""
    value = await box.input_value()
    draft = await page.evaluate("localStorage.getItem('cos.draft.home')")
    run.check(
        "K3 reloaded mid-send: no draft of it, no Not sent line, the message once, one answer",
        not value
        and draft is None
        and await page.locator("#composer-unsent").is_hidden()
        and len(stored) == 1
        and await bubble.count() == 1
        and bool(said),
        f"box {value!r}, draft {draft!r}, stored {len(stored)}, answer {said[:40]!r}",
    )


async def main():
    wanted = {name.upper() for name in sys.argv[1:]}
    with tempfile.TemporaryDirectory(prefix="cos-composer-") as temporary:
        root = Path(temporary).resolve()
        home, _, _ = setup(root)
        data = data_dir(root)
        data.parent.mkdir(parents=True, exist_ok=True)
        app = App(data, root / "log", {"COPILOT_HOME": str(home)})
        await app.start()
        run = None
        try:
            await app.post("/api/settings", {
                "model": MODEL, "mac_notifications": False, "digest_time": ""})  # fmt: skip
            router = (await app.get("/api/state"))["router_id"]
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(f"{live_e2e.BASE}/#token={app.launch_token}")
                await expect(page.locator("#application")).to_be_visible()
                await expect(page.locator("#composer-input")).to_be_enabled()
                run = Run(app, page)
                if not wanted or "K1" in wanted:
                    await k1_shown_at_once_then_stored_in_place(run, router)
                if not wanted or "K2" in wanted:
                    await k2_the_same_again_is_not_sent_twice(run, router)
                if not wanted or "K3" in wanted:
                    await k3_a_reload_mid_send_leaves_no_draft(run, router)
                run.check("no page errors", not errors, "; ".join(errors)[:200])
                await browser.close()
        except BaseException:
            router = (await app.get("/api/state"))["router_id"]
            print(transcript(await app.get(f"/api/sessions/{router}/messages")))
            raise
        finally:
            await app.stop()
            await cleanup(data)
    results = run.results if run else []
    print("\n================ COMPOSER ================")
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    failed = [item for item in results if not item[1]]
    print(f"{len(results) - len(failed)}/{len(results)} as expected")
    if failed or not results:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
