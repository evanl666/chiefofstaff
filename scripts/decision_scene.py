"""A reconsideration card under Needs you, photographed: a worker asked rule A or rule B, the
owner picked A, and the worker asks again with what it found since (replaces=… and why). Made
through the real tools on a fake Copilot, so it shows what the version in use makes of it; the
card at 800 and 1440 px wide, light and dark, into --out.

    uv run --extra dev --with playwright python scripts/decision_scene.py --out <folder>

DECISION_SCENE_PORT (20710) is the app's port."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import uvicorn
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from conftest import FakeClient, FakeSlackMcp, FakeTokens, call_tool, settle  # noqa: E402

from chiefofstaff.models import DraftDecision  # noqa: E402
from chiefofstaff.runtime import Runtime  # noqa: E402
from chiefofstaff.sources.slack import SlackSource  # noqa: E402
from chiefofstaff.web import create_app  # noqa: E402

PORT = int(os.environ.get("DECISION_SCENE_PORT", "20710"))
TOKEN = "decision-scene-token"


async def reconsideration(runtime) -> str:
    """The card: asked, answered, asked again on new evidence. Its id."""
    store = runtime.store
    router = store.get("router_id")
    made = await call_tool(runtime, "spawn_worker", router, title="Tie breaker",
                           instructions="Stop players from always tying.",
                           why_new="scene")  # fmt: skip
    worker = json.loads(made.text_result_for_llm)["session_id"]
    await settle(runtime)
    await call_tool(runtime, "ask_decision", worker,
                    question="Players always tie. Add rule A, rule B, or change nothing?",
                    why="Nine of ten test games end in a tie.",
                    options=[{"choice": "Rule A", "effect": "A tie-break round"},
                             {"choice": "Rule B", "effect": "Points for the first move"},
                             {"choice": "No change"}],
                    recommendation="Rule A: fewer ties")  # fmt: skip
    [card] = [d for d in store.drafts() if d["kind"] == "decision"]
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", text="Rule A"))
    await settle(runtime)
    decision = next(d for d in runtime.decisions.active() if d["text"].endswith("Rule A"))
    await call_tool(runtime, "ask_decision", worker,
                    question="Rule A doesn't stop the ties. Switch to rules C and D together, or "
                    "C only?",
                    why="Simulated 2,000 games: with rule A players still tie 61% of the time.",
                    options=[{"choice": "C and D", "effect": "Ties drop to 12%; one more rule"},
                             {"choice": "C only", "effect": "Ties drop to 30%"}],
                    recommendation="C and D: the fewest ties", replaces=decision["id"])  # fmt: skip
    [card] = [d for d in store.drafts() if d["kind"] == "decision"]
    runtime.publish_state()
    return card["id"]


async def photograph(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cos-decision-scene-") as temporary:
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
        card = await reconsideration(runtime)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            for scheme in ("light", "dark"):
                for width in (800, 1440):
                    context = await browser.new_context(
                        viewport={"width": width, "height": 1000}, color_scheme=scheme
                    )
                    page = await context.new_page()
                    await page.goto(f"http://127.0.0.1:{PORT}/#token={TOKEN}")
                    found = page.locator(f'#needs .card.draft[data-id="{card}"]')
                    tab = page.locator("#home-tabs [data-tab='needs']")
                    if width < 1000 and await tab.count():
                        await tab.click()
                    await found.wait_for()
                    await found.screenshot(path=out / f"reconsideration-{width}-{scheme}.png")
                    await context.close()
            await browser.close()
        server.should_exit = True
        await serving


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    asyncio.run(photograph(parser.parse_args().out))


if __name__ == "__main__":
    main()
