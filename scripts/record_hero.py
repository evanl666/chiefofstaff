"""The README's pictures, taken from the real app in demo mode (docs/images/).

    uv run --extra dev --with playwright python scripts/record_hero.py [--out docs/images]

The hero: the demo (no autoplay, Slack quiet until the stills) driven as an owner uses it, with
a drawn cursor and a caption per step: one request becomes a team on Copilot, Codex and Claude,
one worker tells another what it changed, one decision comes to the owner, and the result is
accepted on its receipts. Chromium's own screencast keeps every painted frame with its time
(waits play faster); ffmpeg (on the PATH) makes hero.mp4 (H.264), hero.gif (1200 px wide) and
hero.png (its first frame). Then the tour's stills (tour-*.png, at twice the pixels) from the
same run. HERO_PORT (21710) is the app's port.
"""

import argparse
import asyncio
import base64
import bisect
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import uvicorn
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from chiefofstaff import demo  # noqa: E402
from chiefofstaff.web import create_app  # noqa: E402

PORT = int(os.environ.get("HERO_PORT", "21710"))
TOKEN = "hero-" + "x" * 27
WIDTH, HEIGHT = 1440, 900
REQUEST = (
    "Fix the flaky login test in acme-api, then have Codex add rate limiting to login attempts. "
    "Also ask Claude to compare a few feature-flag services."
)
# A cursor and a caption drawn over the page (headless Chromium paints no pointer). Both are
# popovers, so they stay above a modal dialog once raised again (heroTop).
OVERLAY = """
addEventListener("DOMContentLoaded", () => {
  const style = document.createElement("style");
  style.textContent = `
    #hero-cursor, #hero-caption, #hero-spot { margin: 0; padding: 0; border: 0; background: none;
      overflow: visible; inset: auto; pointer-events: none; }
    #hero-cursor { left: 0; top: 0; width: 24px; height: 24px; transform: translate(-60px, -60px); }
    #hero-cursor .ring { position: absolute; left: -13px; top: -13px; width: 30px; height: 30px;
      border-radius: 50%; background: rgba(37, 99, 235, .28); transform: scale(0); opacity: 0; }
    #hero-cursor .ring.on { animation: hero-ring .45s ease-out; }
    @keyframes hero-ring { from { transform: scale(.3); opacity: 1; }
      to { transform: scale(1.6); opacity: 0; } }
    #hero-caption { top: 12px; left: 50%; transform: translateX(-50%); padding: 9px 18px;
      border-radius: 999px; background: rgba(17, 17, 17, .9); color: #fff;
      font: 600 17px/1.3 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: .1px; white-space: nowrap; box-shadow: 0 6px 24px rgba(0, 0, 0, .18);
      transition: opacity .25s; }
    #hero-caption b { color: #93c5fd; margin-right: 8px; font-weight: 700; }
    #hero-spot { position: fixed; border-radius: 10px; box-shadow: 0 0 0 2px #2563eb,
      0 0 0 7px rgba(37, 99, 235, .16); opacity: 0; transition: opacity .3s; }
    #hero-spot.on { opacity: 1; }
    #hero-caption:empty, .hero-still #hero-caption, .hero-still #hero-cursor,
      .hero-still #hero-spot, .hero-still #toasts { opacity: 0; }`;
  document.head.append(style);
  const cursor = document.createElement("div");
  cursor.id = "hero-cursor";
  cursor.popover = "manual";
  cursor.innerHTML = `<div class="ring"></div><svg width="24" height="24" viewBox="0 0 24 24">
    <path d="M4 2.5l15 8.6-6.6 1.6-3.4 6.4z" fill="#111" stroke="#fff" stroke-width="1.7"
      stroke-linejoin="round"/></svg>`;
  const caption = document.createElement("div");
  caption.id = "hero-caption";
  caption.popover = "manual";
  const spot = document.createElement("div");
  spot.id = "hero-spot";
  spot.popover = "manual";
  document.body.append(spot, cursor, caption);
  window.heroSpot = (box) => {
    if (box) {
      Object.assign(spot.style, { left: `${box.x - 6}px`, top: `${box.y - 4}px`,
        width: `${box.width + 12}px`, height: `${box.height + 8}px` });
    }
    spot.classList.toggle("on", Boolean(box));
  };
  window.heroTop = () => {
    for (const element of [spot, caption, cursor]) {
      if (element.matches(":popover-open")) element.hidePopover();
      element.showPopover();
    }
  };
  window.heroCaption = (step, text) => {
    caption.innerHTML = step ? `<b>${step}</b>${text}` : "";
  };
  heroTop();
  addEventListener("mousemove", (event) => {
    cursor.style.transform = `translate(${event.clientX - 4}px, ${event.clientY - 2}px)`;
  }, true);
  addEventListener("mousedown", () => {
    const ring = cursor.querySelector(".ring");
    ring.classList.remove("on");
    void ring.offsetWidth;
    ring.classList.add("on");
  }, true);
});
"""


class Walk:
    """A cursor that glides and clicks, the caption over the page, and how fast each stretch
    of the recording plays."""

    def __init__(self, page):
        self.page = page
        self.x, self.y = 900.0, 500.0
        self.marks: list[tuple[float, str]] = []
        # From when each pace holds (time, how many times faster): waits play faster, and the
        # moments a still is taken do not show.
        self.paces: list[tuple[float, float]] = [(0.0, 1.0)]

    async def caption(self, step: str, text: str):
        self.marks.append((time.time(), f"{step} {text}"))
        await self.page.evaluate("([s, t]) => heroCaption(s, t)", [step, text])

    def pace(self, faster: float):
        self.paces.append((time.time(), faster))

    async def glide(self, x: float, y: float, ms: int = 600):
        steps = max(8, ms // 16)
        x0, y0 = self.x, self.y
        for index in range(1, steps + 1):
            part = index / steps
            eased = part * part * (3 - 2 * part)
            await self.page.mouse.move(x0 + (x - x0) * eased, y0 + (y - y0) * eased)
            await self.page.wait_for_timeout(14)
        self.x, self.y = x, y

    async def to(self, locator, fx: float = 0.5, fy: float = 0.5, ms: int = 600):
        await locator.wait_for(state="visible", timeout=30_000)
        box = await locator.bounding_box()
        await self.glide(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy, ms)

    async def click(self, locator, fx: float = 0.5, fy: float = 0.5, ms: int = 600):
        await self.to(locator, fx, fy, ms)
        await self.page.wait_for_timeout(160)
        await self.page.mouse.down()
        await self.page.wait_for_timeout(70)
        await self.page.mouse.up()

    async def spot(self, *around):
        """A ring around what these locators show (none: the ring goes)."""
        box = await union(around) if around else None
        await self.page.evaluate("(box) => heroSpot(box)", box)

    async def still(self, path: Path, *around, pad: int = 12):
        """A still of what these locators show (the page without any), cut from the video."""
        self.pace(1e6)
        await shoot(self.page, path, *around, pad=pad)
        self.pace(1.0)


async def union(around, pad: int = 0) -> dict:
    """The box around what these locators show, `pad` wider (within the page)."""
    boxes = [await locator.bounding_box() for locator in around]
    left = max(0, min(box["x"] for box in boxes) - pad)
    top = max(0, min(box["y"] for box in boxes) - pad)
    right = min(WIDTH, max(box["x"] + box["width"] for box in boxes) + pad)
    bottom = min(HEIGHT, max(box["y"] + box["height"] for box in boxes) + pad)
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


async def shoot(page, path: Path, *around, pad: int = 12):
    """A still of the page, or of the area around these locators, without the overlay."""
    await page.evaluate("document.body.classList.add('hero-still')")
    await page.wait_for_timeout(350)
    clip = await union(around, pad) if around else None
    await page.screenshot(path=str(path), clip=clip)
    await page.evaluate("document.body.classList.remove('hero-still')")


def card_of(page, title: str):
    """A task's card under Needs you (not another that names it)."""
    return page.locator(".card.task", has=page.get_by_text(title, exact=True)).first


async def walk_through(w: Walk, out: Path):
    page = w.page
    composer = page.locator("#composer-input")
    await w.caption("1", "Ask once, in plain words")
    await w.click(composer, 0.3, 0.5, 600)
    await page.keyboard.type(REQUEST, delay=7)
    await page.wait_for_timeout(300)
    await page.keyboard.press("Enter")

    await w.caption("2", "It forms a team: each part on its own agent")
    await page.get_by_text("Started three tasks").wait_for()
    await page.wait_for_timeout(300)
    fold = page.get_by_text("routing updates").first
    await w.click(fold, 0.4, 0.5, 600)
    chips = page.locator(".agent-chip")
    await chips.nth(2).wait_for()
    await w.spot(chips.nth(0), chips.nth(1), chips.nth(2))
    await w.to(chips.nth(1), 0.5, 0.5, 600)
    await page.wait_for_timeout(1300)
    await w.spot()

    w.pace(4.0)  # The fix reproduces the failure and runs the tests.
    told = page.get_by_text("told Rate-limit login").first
    await told.wait_for(state="visible", timeout=30_000)
    w.pace(1.0)
    await w.caption("3", "Its agents talk to each other")
    await w.spot(told)
    await w.to(told, 0.3, 0.5, 600)
    await page.wait_for_timeout(1400)
    await w.spot()
    reply = page.get_by_text("Started three tasks").first
    await w.still(out / "tour-team-talk.png", page.locator(".msg.owner").first, reply, told)

    decision = page.locator(".option.recommended").first
    await decision.wait_for(state="visible", timeout=30_000)
    await w.caption("4", "Only the decision comes to you")
    card = page.locator(".card", has=page.locator(".option.recommended")).first
    await w.spot(card)
    await w.to(card.locator(".option").first, 0.5, 0.5, 600)
    await page.wait_for_timeout(700)
    await w.still(out / "tour-decision.png", card)
    await w.click(decision, 0.3, 0.5, 500)
    await w.spot()
    await page.wait_for_timeout(900)

    await w.caption("5", "Followed to done, with receipts of what actually ran")
    fix = card_of(page, "Fix the flaky login test")
    await w.click(fix.get_by_text("View report"), 0.5, 0.5, 600)
    receipts = fix.locator(".chips .receipts").first
    await receipts.get_by_text("✓ Verified").wait_for()
    await page.wait_for_timeout(700)
    await w.spot(receipts)
    await w.to(receipts, 1.05, 1.6, 600)
    await page.wait_for_timeout(1500)
    await w.still(out / "tour-receipts.png", fix)
    await w.spot()
    await w.click(fix.get_by_role("button", name="Accept", exact=True), 0.5, 0.5, 600)
    await page.wait_for_timeout(1000)


async def stills(page, runtime, out: Path):
    """The tour's other stills, once the hero is recorded: the team's page, a share link, and a
    Slack reply drafted for the owner's OK."""
    await page.evaluate("heroCaption('', '')")
    await page.mouse.move(-50, -50)
    await page.locator(".team2-row").first.click()
    await page.wait_for_timeout(1500)
    await page.locator(".tp-talk summary").click()
    await page.wait_for_timeout(600)
    await shoot(page, out / "tour-team.png")
    await page.locator("#back-home").click()
    await page.wait_for_timeout(800)
    rate = card_of(page, "Rate-limit login attempts")
    await rate.get_by_role("button", name="Accept", exact=True).click()
    await page.wait_for_timeout(800)
    await page.get_by_text("Share…").first.click()
    await page.get_by_role("button", name="Create link").click()
    await page.get_by_text("Link ready").wait_for()
    await page.wait_for_timeout(700)
    await shoot(page, out / "tour-share.png", page.locator("dialog[open]").first, pad=0)
    await page.get_by_role("button", name="Done", exact=True).click()
    runtime.slack.mcp.ready_at = 0
    runtime.slack.wake()
    reply = page.locator(".card", has_text="Slack reply").first
    await reply.wait_for(timeout=30_000)
    await page.wait_for_timeout(800)
    await shoot(page, out / "tour-slack.png", reply)


async def record(url: str, frames_dir: Path, out: Path, runtime):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        # Twice the pixels: smoother text in the video, sharp stills. (The page's own policy
        # would refuse the overlay's style.)
        context = await browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
            device_scale_factor=2,
            color_scheme="light",
            bypass_csp=True,
        )
        await context.add_init_script(OVERLAY)
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.locator("#composer-input").wait_for()
        await page.get_by_text("Nothing needs you").wait_for()
        await page.wait_for_timeout(800)
        cdp = await context.new_cdp_session(page)
        frames: list[tuple[Path, float]] = []

        async def on_frame(params):
            path = frames_dir / f"f{len(frames):05d}.jpg"
            frames.append((path, params["metadata"]["timestamp"]))
            path.write_bytes(base64.b64decode(params["data"]))
            with contextlib.suppress(Exception):  # (The page closed.)
                await cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})

        cdp.on("Page.screencastFrame", lambda params: asyncio.ensure_future(on_frame(params)))
        await cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 95, "maxWidth": WIDTH, "maxHeight": HEIGHT},
        )
        walk = Walk(page)
        await page.mouse.move(walk.x, walk.y)
        await page.wait_for_timeout(700)
        try:
            await walk_through(walk, out)
        except Exception:
            await page.screenshot(path=str(out / "failed.png"))
            raise
        await cdp.send("Page.stopScreencast")
        await page.wait_for_timeout(300)
        await stills(page, runtime, out)
        await browser.close()
    if errors:
        raise SystemExit(f"Page errors: {errors}")
    return frames, walk.marks, walk.paces


def warped(paces: list[tuple[float, float]], start: float):
    """The video's time of a moment of the recording: each stretch at its pace."""
    marks = [max(at, start) for at, _ in paces]

    def at(moment: float) -> float:
        moment = max(moment, start)
        total, index = 0.0, bisect.bisect_right(marks, moment) - 1
        for (since, pace), until in zip(paces[:index], marks[1 : index + 1], strict=True):
            total += (until - max(since, start)) / pace
        return total + (moment - marks[index]) / paces[index][1]

    return at


def encode(frames: list, paces: list, frames_dir: Path, out: Path, fps: int = 30, hold=2.0):
    """The frames at a steady rate (each tick shows the last frame painted by then)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg is needed on the PATH.")
    at = warped(paces, frames[0][1])
    times = [at(moment) for _, moment in frames]
    sequence = frames_dir / "steady"
    sequence.mkdir()
    ticks = int((times[-1] + hold) * fps)
    for tick in range(ticks):
        index = max(0, bisect.bisect_right(times, tick / fps) - 1)
        (sequence / f"{tick:05d}.jpg").symlink_to(frames[index][0])
    steady = [ffmpeg, "-y", "-loglevel", "error", "-framerate", str(fps),
              "-i", str(sequence / "%05d.jpg")]  # fmt: skip
    subprocess.run(
        [*steady, "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "slow",
         "-crf", "24", "-movflags", "+faststart", str(out / "hero.mp4")],
        check=True,
    )  # fmt: skip
    # The GIF's palette is made from the whole clip: from a lossless copy of one size (a
    # frame of another size would start the palette over).
    lossless = frames_dir / "lossless.mkv"
    subprocess.run(
        [*steady, "-vf", "fps=15,scale=1200:750:flags=lanczos", "-c:v", "ffv1", str(lossless)],
        check=True,
    )
    palette = (
        "split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];"
        "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle"
    )
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(lossless), "-vf", palette,
         str(out / "hero.gif")],
        check=True,
    )  # fmt: skip
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(frames[0][0]),
         "-vf", "scale=1200:-1:flags=lanczos", str(out / "hero.png")],
        check=True,
    )  # fmt: skip
    return ticks / fps


async def main(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cos-hero-") as temporary:
        root = Path(temporary)
        project = demo.make_project(root / "projects")
        made = []

        def factory(store, bus):
            made.append(demo.DemoRuntime(store, bus, project, autoplay=False, slack_delay=3600))
            return made[0]

        app = create_app(root / "data", TOKEN, PORT, runtime_factory=factory)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
        )
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        frames_dir = root / "frames"
        frames_dir.mkdir()
        try:
            url = f"http://127.0.0.1:{PORT}/#token={TOKEN}"
            frames, marks, paces = await record(url, frames_dir, out, made[0])
        finally:
            server.should_exit = True
            await serving
        at = warped(paces, frames[0][1])
        for moment, text in marks:
            print(f"  {at(moment):5.1f}s  {text}")
        seconds = encode(frames, paces, frames_dir, out)
        print(f"{len(frames)} frames, {seconds:.1f} s")
    for path in sorted(out.glob("hero.*")) + sorted(out.glob("tour-*.png")):
        print(f"{path.name}: {path.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "images")
    asyncio.run(main(parser.parse_args().out))
