"""Live test of Claude's self-setup (claude_setup.py): Chief of Staff installs Claude's Agent SDK by
itself, with a real uv and PyPI, into a `uv tool` install of this checkout made without the
Claude extra (in a scratch folder: never the owner's tools, Claude or Copilot).

  C0  A first start (a data folder Chief of Staff never ran in) with Claude Code alone ready
      and the SDK missing: Claude is turned on and set up by itself, Home says "Setting up
      Claude Code" meanwhile, then Chief of Staff runs on Claude, said once in its chat; Copilot,
      not ready, is turned off (agents-anywhere: the first agent ready).
  C1  Claude off, the SDK missing, not a first start (the agents' settings there, Claude off:
      a first start would turn Claude on, C0): its row says "Not set up yet" (it can be turned
      on); turned on in Settings, "Setting up Claude…", then Ready, in the same process (no
      restart).
  C2  Chief of Staff installed again without the extra (`uv tool install --reinstall`, the same
      version), Claude on: set up as it starts, "Setting up Claude…" then Ready.
  C3  No network (uv offline, nothing cached): "Couldn't set up Claude: no network.", Retry, and
      under Details what uv said and the command; Retry tries again; nothing installed.
  C4  Started with uvx (its environment in uv's cache): the SDK goes to a folder of Chief of
      Staff's own in its data folder; the environment in uv's cache gets nothing.

Claude Code is the tests' stand-in (tests/fake_claude.py): no Claude account or plan is used.
Copilot is not on the PATH (its row says Can't start). Downloads the SDK (about 160 MB) once per
scenario but C3, and takes 2 to 5 minutes. Ports: LIVE_CLAUDE_SETUP_PORT (18990) and the three
after it. Scratch: a folder in TMPDIR, removed at the end (--keep keeps it); screenshots go to
LIVE_CLAUDE_SETUP_SHOTS (browser-shots/claude-setup).

    uv run --extra dev --with playwright python scripts/live_claude_setup.py [C0 C1 C2 C3 C4]
"""

import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("LIVE_CLAUDE_SETUP_PORT", "18990"))
SHOTS = Path(os.environ.get("LIVE_CLAUDE_SETUP_SHOTS") or ROOT / "browser-shots" / "claude-setup")
UV = shutil.which("uv") or ""


def log(text: str):
    print(text, flush=True)


class World:
    """The scratch folder: a wheel of this checkout installed as a `uv tool` without the extra,
    the stand-in Claude Code, a home and caches of its own."""

    def __init__(self, scratch: Path):
        self.s = scratch
        for name in ("dist", "tools", "bin", "home", "tmp", "pathbin", "fakebin/stand-in",
                     "claude-home", "claude-config"):  # fmt: skip
            (scratch / name).mkdir(parents=True, exist_ok=True)
        (scratch / "pathbin" / "uv").symlink_to(UV)
        (scratch / "pathbin" / "uvx").symlink_to(shutil.which("uvx") or Path(UV).with_name("uvx"))
        stand_in = scratch / "fakebin" / "stand-in" / "claude"
        stand_in.symlink_to(ROOT / "tests" / "fake_claude.py")
        claude = scratch / "fakebin" / "claude"
        claude.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stand_in}" "$@"\n')
        claude.chmod(0o755)
        (scratch / "claude-config" / ".claude.json").write_text("{}\n")
        self.python = scratch / "tools" / "chiefofstaff" / "bin" / "python"

    def env(self, cache: str = "cache", **more) -> dict:
        s = self.s
        return {
            "HOME": str(s / "home"),
            "PATH": f"{s}/pathbin:{s}/fakebin:/usr/bin:/bin",
            "CLAUDE_CONFIG_DIR": str(s / "claude-config"),
            "FAKE_CLAUDE_HOME": str(s / "claude-home"),
            "TMPDIR": str(s / "tmp"),
            "UV_CACHE_DIR": str(s / cache),
            "UV_TOOL_DIR": str(s / "tools"),
            "UV_TOOL_BIN_DIR": str(s / "bin"),
            "CHIEFOFSTAFF_UPDATE_CHECK": "0",
            "LANG": "en_US.UTF-8",
            **more,
        }

    def run(self, *command: str, **more) -> subprocess.CompletedProcess:
        return subprocess.run(command, env=self.env(**more), capture_output=True, text=True,
                              check=False)  # fmt: skip

    def install(self, again: bool = False):
        """This checkout, as a `uv tool` without the Claude extra."""
        if not again:
            built = subprocess.run(
                [UV, "build", "--wheel", "--out-dir", str(self.s / "dist")], cwd=ROOT,
                env={**os.environ, "UV_CACHE_DIR": str(self.s / "cache")},
                capture_output=True, text=True, check=False,
            )  # fmt: skip
            assert built.returncode == 0, built.stderr
        wheel = str(next((self.s / "dist").glob("*.whl")))
        done = self.run("uv", "tool", "install", *(["--reinstall"] if again else []), wheel)
        assert done.returncode == 0, done.stderr
        assert self.sdk(self.python) == "", "installed without the extra"

    @property
    def wheel(self) -> str:
        return str(next((self.s / "dist").glob("*.whl")))

    def sdk(self, python: Path) -> str:
        code = (
            "from importlib import metadata\ntry: print(metadata.version('claude-agent-sdk'))\n"
            "except metadata.PackageNotFoundError: print('')"
        )
        return self.run(str(python), "-c", code).stdout.strip()

    def no_sdk(self):
        """The SDK not in Chief of Staff's own environment (an earlier scenario set it up)."""
        self.run("uv", "pip", "uninstall", "--python", str(self.python), "claude-agent-sdk")
        assert self.sdk(self.python) == ""

    def claude_on(self, data: Path, on: bool = True):
        """Claude turned on (or off) in the data folder's agents' settings: then not a first
        start, whatever else is there."""
        data.mkdir(parents=True, exist_ok=True)
        code = (
            "import sys; from pathlib import Path; from chiefofstaff.store import Store; "
            "from chiefofstaff import agents; agents.configure(Store(Path(sys.argv[1])), "
            "'claude', enabled=sys.argv[2] == '1')"
        )
        done = self.run(str(self.python), "-c", code, str(data), "1" if on else "0")
        assert done.returncode == 0, done.stderr

    def start(self, command: list[str], data: Path, port: int, cache: str, **more):
        """Chief of Staff, started as the owner would: its private link once it answers."""
        (data / "launch-url").unlink(missing_ok=True)
        output = (self.s / f"chief-{port}.log").open("w")
        process = subprocess.Popen(
            [*command, "--port", str(port), "--data-dir", str(data), "--no-browser"],
            env=self.env(cache, **more), stdout=output, stderr=subprocess.STDOUT,
            start_new_session=True,
        )  # fmt: skip
        deadline = time.time() + 180
        while time.time() < deadline:
            launch = data / "launch-url"
            if launch.exists():
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2):
                        return process, launch.read_text().strip()
                except OSError:
                    pass
            assert process.poll() is None, (self.s / f"chief-{port}.log").read_text()[-2000:]
            time.sleep(0.2)
        raise AssertionError("Chief of Staff did not answer")


def stop(process: subprocess.Popen):
    os.killpg(process.pid, signal.SIGINT)
    try:
        process.wait(30)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


async def agents_page(browser, url: str):
    page = await browser.new_page(viewport={"width": 1100, "height": 900})
    await page.goto(url)
    await expect(page.locator("#application")).to_be_visible()
    await open_agents(page)
    return page, page.locator("#agent-claude .agent-line .status")


async def open_agents(page):
    await page.locator("#settings-button").click()
    await page.locator("#settings-tabs [data-tab=agents]").click()
    await expect(page.locator("#settings-page-agents")).to_have_attribute(
        "data-looked", "done", timeout=60000
    )


def shot_path(name: str) -> Path:
    SHOTS.mkdir(parents=True, exist_ok=True)
    return SHOTS / f"{name}.png"


async def shot(page, name: str):
    await page.locator("#settings-dialog").screenshot(path=shot_path(name))


READY_ROUTER = """async () => {
    const headers = {"X-Chief-Key": localStorage.getItem("cos.key") || ""};
    const state = await (await fetch("/api/state", {headers, credentials: "omit"})).json();
    return state.runtime?.state === "ready" && Boolean(state.router_id);
}"""


async def c0(world: World, browser, port: int):
    world.no_sdk()
    data = world.s / "data-first"
    process, url = world.start([str(world.s / "bin" / "chiefofstaff")], data, port, "cache-c0")
    try:
        page = await browser.new_page(viewport={"width": 1100, "height": 900})
        await page.goto(url)
        await expect(page.locator("#application")).to_be_visible()
        card = page.locator("#home-setup .label")
        note = page.get_by_text(re.compile(r"^Chief of Staff runs on Claude\b"))
        seen = await card.or_(note).first.inner_text(timeout=60000)
        if seen == "Setting up Claude Code":
            await page.screenshot(path=shot_path("c0-setting-up-home"))
        else:
            assert seen.startswith("Chief of Staff runs on Claude"), seen
        await expect(page.locator("#home-setup")).to_have_count(0, timeout=240000)
        # Running on Claude once set up (no restart): its Router there, the note said once. (A
        # page open from before the Router existed shows its chat once reloaded.)
        deadline = time.time() + 120
        while not await page.evaluate(READY_ROUTER):
            assert time.time() < deadline, "no Router running"
            await asyncio.sleep(1)
        await page.reload()
        await expect(note).to_have_count(1, timeout=30000)
        await page.screenshot(path=shot_path("c0-runs-on-claude"))
        await open_agents(page)
        await expect(page.locator("#agent-claude .agent-line .status")).to_have_text("Ready")
        await expect(page.locator("input[name=agent_claude_enabled]")).to_be_checked()
        await expect(page.locator("input[name=agent_copilot_enabled]")).not_to_be_checked()
        await shot(page, "c0-ready")
        assert process.poll() is None, "the same process: no restart"
    finally:
        stop(process)
    assert world.sdk(world.python), "in Chief of Staff's own environment"
    log("C0 ok: a first start with Claude alone: turned on, set up, runs on it, said once")


async def c1(world: World, browser, port: int):
    world.no_sdk()
    data = world.s / "data"
    world.claude_on(data, False)  # Not a first start: Claude stays off (C0 is one).
    process, url = world.start([str(world.s / "bin" / "chiefofstaff")], data, port, "cache-c1")
    try:
        page, status = await agents_page(browser, url)
        await expect(status).to_have_text("Not set up yet")
        await expect(page.locator("input[name=agent_claude_enabled]")).to_be_enabled()
        await shot(page, "c1-not-set-up-yet")
        await page.locator("input[name=agent_claude_enabled]").check()
        await page.locator("#settings-save").click()
        await expect(page.locator("#settings-dialog")).to_be_hidden()
        await open_agents(page)
        seen = await status.inner_text()
        assert seen in {"Setting up Claude…", "Ready"}, seen
        if seen != "Ready":
            await shot(page, "c1-setting-up")
        await expect(status).to_have_text("Ready", timeout=240000)
        await shot(page, "c1-ready")
        assert process.poll() is None, "the same process: no restart"
        assert "uv tool install" not in await page.locator("#agent-claude").inner_text()
    finally:
        stop(process)
    assert world.sdk(world.python), "in Chief of Staff's own environment"
    log("C1 ok: Not set up yet; turned on, Setting up Claude…, then Ready, no restart")


async def c2(world: World, browser, port: int):
    world.install(again=True)
    data = world.s / "data"
    world.claude_on(data)
    process, url = world.start([str(world.s / "bin" / "chiefofstaff")], data, port, "cache-c2")
    try:
        page, status = await agents_page(browser, url)
        seen = await status.inner_text()
        assert seen in {"Setting up Claude…", "Ready"}, seen
        if seen != "Ready":
            await shot(page, "c2-setting-up-at-start")
        await expect(status).to_have_text("Ready", timeout=240000)
        await shot(page, "c2-ready")
    finally:
        stop(process)
    assert world.sdk(world.python)
    log("C2 ok: installed again without the extra, set up as it starts, Ready")


async def c3(world: World, browser, port: int):
    world.run("uv", "pip", "uninstall", "--python", str(world.python), "claude-agent-sdk")
    data = world.s / "data-offline"
    world.claude_on(data)
    process, url = world.start([str(world.s / "bin" / "chiefofstaff")], data, port, "cache-c3",
                               UV_OFFLINE="1")  # fmt: skip
    try:
        page, status = await agents_page(browser, url)
        await expect(status).to_have_text("Not set up", timeout=120000)
        row = page.locator("#agent-claude")
        await expect(row.locator(".agent-alert")).to_have_text(
            ["Couldn't set up Claude: no network."]
        )
        await expect(row.locator(".action-slot button")).to_have_text("Retry")
        assert "uv tool install" not in await row.inner_text(), "the command: under Details only"
        await shot(page, "c3-failed")
        await row.locator("details.setup-fold summary").click()
        await expect(row.locator("details.setup-fold code")).to_have_text(
            "uv tool install 'chiefofstaff[claude]'"
        )
        await expect(row.locator("details.setup-fold pre")).to_contain_text("network")
        await shot(page, "c3-failed-details")
        await row.locator(".action-slot button").click()
        await expect(status).to_have_text("Not set up", timeout=120000)
    finally:
        stop(process)
    assert not world.sdk(world.python)
    log("C3 ok: no network said in a few words, Retry, the command under Details")


async def c4(world: World, browser, port: int):
    data = world.s / "data-uvx"
    world.claude_on(data)
    command = ["uvx", "--isolated", "--from", world.wheel, "chiefofstaff"]
    process, url = world.start(command, data, port, "cache-c4")
    try:
        page, status = await agents_page(browser, url)
        await expect(status).to_have_text("Ready", timeout=240000)
        await shot(page, "c4-uvx-ready")
    finally:
        stop(process)
    cached = [p for p in (world.s / "cache-c4" / "archive-v0").iterdir() if (p / "bin").is_dir()]
    assert cached and not any(world.sdk(p / "bin" / "python") for p in cached), "uv's cache"
    own = [p.name for p in (data / "extras").iterdir()]
    assert len(own) == 1 and own[0].startswith("claude-sdk-"), own
    assert (data / "extras" / own[0] / "claude_agent_sdk").is_dir()
    log("C4 ok: started with uvx, the SDK in Chief of Staff's own folder, uv's cache unchanged")


async def main():
    assert UV, "uv is needed"
    wanted = [arg.upper() for arg in sys.argv[1:] if not arg.startswith("--")] or [
        "C0", "C1", "C2", "C3", "C4"
    ]  # fmt: skip
    scratch = Path(tempfile.mkdtemp(prefix="cos-claude-setup-"))
    try:
        world = World(scratch)
        world.install()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            for index, name in enumerate(wanted):
                await globals()[name.lower()](world, browser, PORT + index)
            await browser.close()
    finally:
        if "--keep" in sys.argv:
            log(f"kept: {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)
    log(f"LIVE CLAUDE SETUP PASSED ({' '.join(wanted)})")


if __name__ == "__main__":
    asyncio.run(main())
