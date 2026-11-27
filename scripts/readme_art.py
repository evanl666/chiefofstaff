"""The README's drawn pictures: how it works (docs/images/how-it-works.png) and, with --social,
the repository's 1280 x 640 social preview.

    uv run --extra dev --with playwright python scripts/readme_art.py [--social <file.png>]

Plain HTML drawn by Chromium at twice the pixels. The screenshots and the hero clip come from
the app itself (record_hero.py).
"""

import argparse
import asyncio
import base64
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
IMAGES = ROOT / "docs" / "images"

STYLE = """
* { box-sizing: border-box; margin: 0; }
body { font: 15px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  color: #1c1c1e; background: #fff; }
.copilot { --c: #6e56cf; } .codex { --c: #0f8a6c; } .claude { --c: #c2603e; }
.mark { display: inline-block; width: 9px; height: 9px; border-radius: 2px;
  transform: rotate(45deg); background: var(--c); margin: 0 7px 1px 2px; }
"""

HOW = """
<div class="frame">
  <div class="col">
    <div class="head">Comes to you</div>
    <div class="in">Your words, in one chat</div>
    <div class="in">Slack: DMs, mentions, a 🤖 reaction</div>
    <div class="in">GitHub: reviews for you, your PRs</div>
    <div class="in">Watches: docs, mail, your search</div>
    <div class="in">A hotkey from any app</div>
  </div>
  <div class="arrow">→</div>
  <div class="mid">
    <div class="router"><b>Router</b>
      <span>splits the request, starts the workers, keeps the decisions</span></div>
    <div class="down">↓</div>
    <div class="team">
      <div class="label">A team, one worker per part, each in its own worktree</div>
      <div class="worker copilot"><i class="mark"></i><b>Copilot</b> Fix the flaky test</div>
      <div class="talk">↕ tell and ask each other</div>
      <div class="worker codex"><i class="mark"></i><b>Codex</b> Rate-limit logins</div>
      <div class="worker claude"><i class="mark"></i><b>Claude</b> Compare flag services</div>
    </div>
  </div>
  <div class="arrow">→</div>
  <div class="col">
    <div class="head">Comes back to you</div>
    <div class="out strong">Only the decisions: one card each</div>
    <div class="out">Drafts in your voice, sent on your OK</div>
    <div class="out">Results with receipts of what ran</div>
    <div class="out">A share link that expires</div>
  </div>
  <div class="foot">
    <div><b>Sees it through</b> PRs to merged · CI and reviews · check-backs · Watchdog</div>
    <div><b>Learns you</b> how you write · habits you approve · checks from fixed mistakes</div>
  </div>
</div>
"""

HOW_STYLE = """
.frame { width: 1200px; padding: 28px 30px 24px; display: grid;
  grid-template-columns: 310px 36px 1fr 36px 300px; align-items: center; gap: 0;
  background: #fcfcfb; border: 1px solid #e7e7e4; border-radius: 18px; }
.head { font-size: 13px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
  color: #626267; margin-bottom: 10px; }
.in, .out { padding: 9px 13px; margin-bottom: 8px; background: #fff; border: 1px solid #e7e7e4;
  border-radius: 10px; }
.out.strong { border-color: #b45309; box-shadow: 0 0 0 3px rgba(180, 83, 9, .12);
  font-weight: 600; }
.arrow, .down { text-align: center; font-size: 26px; color: #9a9aa0; }
.down { margin: 4px 0; font-size: 22px; }
.router { padding: 13px 16px; border-radius: 12px; background: #1c1c1e; color: #fff;
  text-align: center; }
.router b { display: block; font-size: 18px; }
.router span { color: #c9c9ce; font-size: 13.5px; }
.team { padding: 14px 16px 10px; border: 1.5px dashed #c8c8cc; border-radius: 14px;
  background: #fff; }
.label { font-size: 13px; color: #626267; margin-bottom: 10px; text-align: center; }
.worker { padding: 8px 12px; margin-bottom: 8px; border: 1px solid #e7e7e4; border-radius: 999px;
  background: #fff; white-space: nowrap; }
.worker b { margin-right: 6px; }
.talk { text-align: center; color: #1d5bd0; font-size: 13px; margin: -2px 0 6px; }
.foot { grid-column: 1 / -1; display: grid; grid-template-columns: 1fr 1fr; gap: 14px;
  margin-top: 18px; }
.foot div { padding: 10px 14px; border-radius: 10px; background: #f2f2f0; color: #3a3a3e;
  font-size: 14px; }
.foot b { color: #1c1c1e; margin-right: 8px; }
"""

SOCIAL = """
<div class="social">
  <div class="text">
    <img class="duck" src="{duck}">
    <h1>Chief of Staff</h1>
    <p class="tag">Your engineering chief of staff.<br>The more it works for you,<br>the more it
      works like you.</p>
    <p class="agents"><span class="copilot"><i class="mark"></i>Copilot</span>
      <span class="codex"><i class="mark"></i>Codex</span>
      <span class="claude"><i class="mark"></i>Claude</span> as one team</p>
    <code>uvx chiefofstaff --demo</code>
  </div>
  <img class="shot" src="{shot}">
</div>
"""

SOCIAL_STYLE = """
.social { width: 1280px; height: 640px; display: flex; align-items: center; gap: 40px;
  padding: 0 0 0 64px; background: linear-gradient(135deg, #fcfcfb 0%, #f1efe9 100%);
  overflow: hidden; }
.text { flex: none; width: 470px; }
.duck { width: 76px; height: 76px; object-fit: contain; margin-bottom: 12px; }
h1 { font-size: 54px; letter-spacing: -1px; line-height: 1.05; }
.tag { margin-top: 18px; font-size: 27px; line-height: 1.3; color: #2c2c30; }
.agents { margin-top: 22px; font-size: 19px; color: #626267; }
.agents span { color: #1c1c1e; font-weight: 600; margin-right: 10px; }
code { display: inline-block; margin-top: 24px; padding: 10px 16px; border-radius: 10px;
  background: #1c1c1e; color: #fff; font: 600 19px/1 ui-monospace, Menlo, monospace; }
.shot { flex: none; width: 820px; border-radius: 14px; border: 1px solid #ddd;
  box-shadow: 0 24px 60px rgba(0, 0, 0, .16); }
"""


def inline(path: Path, kind: str) -> str:
    """An image the page holds itself (a page of its own reads no files)."""
    return f"data:image/{kind};base64,{base64.b64encode(path.read_bytes()).decode()}"


async def draw(html: str, style: str, path: Path, selector: str, scale: int = 2):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(device_scale_factor=scale)
        await page.set_content(f"<style>{STYLE}{style}</style>{html}")
        await page.wait_for_load_state("networkidle")
        await page.locator(selector).screenshot(path=str(path))
        await browser.close()


async def main(social: Path | None):
    await draw(HOW, HOW_STYLE, IMAGES / "how-it-works.png", ".frame")
    print(IMAGES / "how-it-works.png")
    if social:
        html = SOCIAL.format(
            duck=inline(ROOT / "src" / "chiefofstaff" / "static" / "favicon.svg", "svg+xml"),
            shot=inline(IMAGES / "tour-team.png", "png"),
        )
        await draw(html, SOCIAL_STYLE, social, ".social", scale=1)
        print(social)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--social", type=Path, help="where the social preview goes")
    asyncio.run(main(parser.parse_args().social))
