"""Live test of what the page runs for the owner instead of telling them a command (0.1.61), with
real installers and real CLIs, each in a scratch folder: never the owner's own tools, agents,
home or accounts.

  S1  Copilot not installed (Copilot off: nothing starts with an account): its row in Settings
      says Not installed, with Install; Install shows the official installer, word for word,
      runs it (the real `curl -fsSL https://gh.io/copilot-install | bash`, into the scratch home's
      ~/.local/bin, not on the PATH Chief of Staff started with), finds it there and says Ready.
  S2  A first start with only a Codex older than the versions tested (the real @openai/codex,
      installed with npm into a scratch prefix): Home's card offers Update; Update shows
      `codex update`, runs it (Codex updates itself with npm, in that prefix), and the card says
      what Codex needs next (Sign in), with no restart.
  S3  Chief of Staff installed with pip into a virtual environment of its own, a newer release
      out (a wheel of this checkout as 0.1.99, from a folder of its own: no index): Update and
      restart updates it with that environment's installer and starts the new version in its
      place (the same port and private link).
  S4  The same for a pipx install (pipx itself in a scratch environment, its home there).

Codex's account is never used (its home is the scratch one: signed out). Copilot is never started
(off, or not installed). Downloads Copilot CLI, Codex and Chief of Staff's dependencies once each;
takes 3 to 6 minutes. Ports: LIVE_SELF_HANDLE_PORT (18960) and the three after it. Scratch: a
folder in TMPDIR, removed at the end (--keep keeps it); screenshots go to LIVE_SELF_HANDLE_SHOTS
(browser-shots/self-handle-live).

    uv run --extra dev --with playwright python scripts/live_self_handle.py [S1 S2 S3 S4]
"""

import asyncio
import json
import os
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
PORT = int(os.environ.get("LIVE_SELF_HANDLE_PORT", "18960"))
SHOTS = Path(
    os.environ.get("LIVE_SELF_HANDLE_SHOTS") or ROOT / "browser-shots" / "self-handle-live"
)
UV = shutil.which("uv") or ""
# This checkout as a release PyPI does not have (so pip and pipx take it from its folder), and
# the newer one Update and restart goes to.
OURS, NEWER = "0.1.98", "0.1.99"
OLD_CODEX = "0.167.1"  # Older than the versions tested (backend_codex.OLDEST_CODEX).


def log(text: str):
    print(text, flush=True)


def changed_at(path: str | None) -> float:
    """When the owner's own copy of a tool last changed (0 when there is none): it must not."""
    return Path(path).resolve().stat().st_mtime if path else 0.0


class World:
    """The scratch folder: a home, npm's prefix and cache, a Python environment with this
    checkout installed with pip, wheels of it as this version and as NEWER, the stand-in Codex."""

    def __init__(self, scratch: Path):
        self.s = scratch
        for name in ("dist-a", "dist-b", "home/.codex", "tmp", "pathbin", "npm", "npm-cache",
                     "pip-cache", "fakebin", "codex-home", "claude-config"):  # fmt: skip
            (scratch / name).mkdir(parents=True, exist_ok=True)
        # node and npm only (Homebrew's folder has the owner's own agents beside them).
        for tool in ("node", "npm"):
            if found := shutil.which(tool):
                (scratch / "pathbin" / tool).symlink_to(Path(found).resolve())
        stand_in = ROOT / "tests" / "fake_codex.py"
        (scratch / "fakebin" / "codex").write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{stand_in}" "$@"\n'
        )
        (scratch / "fakebin" / "codex").chmod(0o755)
        (scratch / "claude-config" / ".claude.json").write_text("{}\n")
        self.venv = scratch / "venv"

    def env(self, *path: str, **more) -> dict:
        s = self.s
        return {
            "HOME": str(s / "home"),
            "PATH": ":".join([*path, f"{s}/pathbin", "/usr/bin", "/bin"]),
            "TMPDIR": str(s / "tmp"),
            "NPM_CONFIG_PREFIX": str(s / "npm"),
            "NPM_CONFIG_CACHE": str(s / "npm-cache"),
            "PIP_CACHE_DIR": str(s / "pip-cache"),
            "UV_CACHE_DIR": str(s / "uv-cache"),
            "CODEX_HOME": str(s / "home" / ".codex"),
            "CLAUDE_CONFIG_DIR": str(s / "claude-config"),
            "COPILOT_HOME": str(s / "home" / ".copilot"),
            "CHIEFOFSTAFF_UPDATE_CHECK": "0",
            "LANG": "en_US.UTF-8",
            **more,
        }

    def run(self, *command: str, on_path: tuple[str, ...] = (), **more):
        done = subprocess.run(command, env=self.env(*on_path, **more), capture_output=True,
                              text=True, check=False)  # fmt: skip
        assert done.returncode == 0, (command, done.stdout[-2000:], done.stderr[-2000:])
        return done

    def build(self):
        """Wheels of this checkout (a copy of its files): as OURS (dist-a), and as NEWER
        (dist-b)."""
        cache = {**os.environ, "UV_CACHE_DIR": str(self.s / "uv-cache")}
        files = ["git", "ls-files", "-co", "--exclude-standard"]
        listed = subprocess.run(files, cwd=ROOT, capture_output=True, text=True, check=True)
        listed = listed.stdout.split()
        for version, out in ((OURS, "dist-a"), (NEWER, "dist-b")):
            copy = self.s / f"source-{version}"
            for name in listed:
                if (ROOT / name).is_file():
                    (copy / name).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(ROOT / name, copy / name)
            project = copy / "pyproject.toml"
            text = project.read_text()
            line = next(line for line in text.splitlines() if line.startswith("version = "))
            project.write_text(text.replace(line, f'version = "{version}"', 1))
            built = subprocess.run([UV, "build", "--wheel", "--out-dir", str(self.s / out)],
                                   cwd=copy, env=cache, capture_output=True, text=True,
                                   check=False)  # fmt: skip
            assert built.returncode == 0, built.stderr

    def venv_install(self):
        """A virtual environment with pip, this checkout installed in it with pip."""
        self.run(UV, "venv", "--seed", "--python", "3.12", str(self.venv))
        wheel = next((self.s / "dist-a").glob("*.whl"))
        self.run(
            str(self.venv / "bin" / "pip"), "install", "--disable-pip-version-check", str(wheel)
        )

    def version(self, python: Path) -> str:
        """The version installed there ("" while an install changes it)."""
        code = "from importlib import metadata; print(metadata.version('chiefofstaff'))"
        done = subprocess.run([str(python), "-c", code], env=self.env(), capture_output=True,
                              text=True, check=False)  # fmt: skip
        return done.stdout.strip() if done.returncode == 0 else ""

    def configure(self, python: Path, data: Path, part: dict, latest: str = ""):
        """The data folder's agents' settings (not a first start then), and a newer release
        known as read from PyPI a moment ago."""
        data.mkdir(parents=True, exist_ok=True)
        code = (
            "import json, sys, time; from pathlib import Path; "
            "from chiefofstaff.store import Store; from chiefofstaff import agents, updates; "
            "store = Store(Path(sys.argv[1])); part = json.loads(sys.argv[2]); "
            "part and agents.save(store, agents.planned(store, part)); "
            "sys.argv[3] and store.set(updates.LAST, json.dumps("
            "{'at': time.time(), 'latest': sys.argv[3]}))"
        )
        self.run(str(python), "-c", code, str(data), json.dumps(part), latest)

    def start(self, command: list[str], data: Path, port: int, *path: str, **more):
        """Chief of Staff, started as the owner would: its private link once it answers."""
        (data / "launch-url").unlink(missing_ok=True)
        output = (self.s / f"chief-{port}.log").open("a")
        process = subprocess.Popen(
            [*command, "--port", str(port), "--data-dir", str(data), "--no-browser"],
            env=self.env(*path, **more), stdout=output, stderr=subprocess.STDOUT,
            start_new_session=True,
        )  # fmt: skip
        return process, answered(process, data, port, self.s / f"chief-{port}.log")


def answered(process, data: Path, port: int, log_file: Path, seconds: float = 180) -> str:
    deadline = time.time() + seconds
    while time.time() < deadline:
        launch = data / "launch-url"
        if launch.exists():
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2):
                    return launch.read_text().strip()
            except OSError:
                pass
        assert process.poll() is None, log_file.read_text()[-3000:]
        time.sleep(0.2)
    raise AssertionError("Chief of Staff did not answer")


def stop(process: subprocess.Popen):
    try:
        os.killpg(process.pid, signal.SIGINT)
        process.wait(30)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    except (ProcessLookupError, PermissionError):
        pass


def shot_path(name: str) -> Path:
    SHOTS.mkdir(parents=True, exist_ok=True)
    return SHOTS / f"{name}.png"


async def opened(browser, url: str):
    page = await browser.new_page(viewport={"width": 1100, "height": 900})
    await page.goto(url)
    await expect(page.locator("#application")).to_be_visible()
    return page


async def open_agents(page):
    await page.locator("#settings-button").click()
    await page.locator("#settings-tabs [data-tab=agents]").click()
    await expect(page.locator("#settings-page-agents")).to_have_attribute(
        "data-looked", "done", timeout=60000
    )


async def s1(world: World, browser, port: int):
    owners = changed_at(shutil.which("copilot"))
    data = world.s / "data-s1"
    python = world.venv / "bin" / "python"
    world.configure(python, data, {"copilot": {"enabled": False}, "codex": {"enabled": True}})
    fake = {"FAKE_CODEX_HOME": str(world.s / "codex-home")}
    process, url = world.start([str(world.venv / "bin" / "chiefofstaff")], data, port,
                               str(world.s / "fakebin"), **fake)  # fmt: skip
    try:
        page = await opened(browser, url)
        await open_agents(page)
        row = page.locator("#agent-copilot")
        await expect(row.locator(".status")).to_have_text("Not installed", timeout=30000)
        await row.locator(".action-slot .setup-action").click()
        dialog = page.locator("dialog.agent-setup")
        await expect(dialog.locator(".setup-command").first).to_have_text(
            "curl -fsSL https://gh.io/copilot-install | bash"
        )
        await dialog.screenshot(path=shot_path("s1-install-consent"))
        await dialog.get_by_role("button", name="Install", exact=True).click()
        await expect(dialog.locator(".setup-waiting")).to_contain_text("Installing Copilot")
        await dialog.screenshot(path=shot_path("s1-installing"))
        await expect(dialog.locator(".setup-done")).to_contain_text(
            "Copilot is ready", timeout=300000
        )
        await dialog.screenshot(path=shot_path("s1-installed"))
        await dialog.get_by_role("button", name="Done").click()
        await expect(row.locator(".status")).to_have_text("Ready · off", timeout=30000)
        await page.locator("#settings-dialog").screenshot(path=shot_path("s1-row-ready-off"))
        assert process.poll() is None, "the same process: no restart"
    finally:
        stop(process)
    installed = world.s / "home" / ".local" / "bin" / "copilot"
    said = world.run(str(installed), "--version").stdout
    assert "Copilot" in said or said.strip(), said
    assert changed_at(shutil.which("copilot")) == owners, "the owner's own Copilot CLI"
    log(f"S1 ok: Copilot installed from its row by its official installer ({said.strip()})")


async def s2(world: World, browser, port: int):
    owners = changed_at(shutil.which("codex"))
    world.run("npm", "install", "-g", f"@openai/codex@{OLD_CODEX}")
    codex = world.s / "npm" / "bin" / "codex"
    assert OLD_CODEX in world.run(str(codex), "--version").stdout
    data = world.s / "data-s2"
    npm_bin = str(world.s / "npm" / "bin")
    process, url = world.start([str(world.venv / "bin" / "chiefofstaff")], data, port, npm_bin)
    try:
        page = await opened(browser, url)
        card = page.locator("#home-setup")
        await expect(card.locator(".label")).to_have_text("Set up a coding agent", timeout=60000)
        update = card.get_by_role("button", name="Update Codex")
        await expect(update).to_be_visible(timeout=60000)
        await card.screenshot(path=shot_path("s2-home-update"))
        await update.click()
        dialog = page.locator("dialog.agent-setup")
        await expect(dialog.locator(".setup-command").first).to_have_text("codex update")
        await dialog.screenshot(path=shot_path("s2-update-consent"))
        await dialog.get_by_role("button", name="Update", exact=True).click()
        await expect(dialog.locator(".setup-done")).to_contain_text(
            "Codex is up to date", timeout=300000
        )
        await dialog.screenshot(path=shot_path("s2-updated"))
        await dialog.get_by_role("button", name="Done").click()
        await expect(card.get_by_role("button", name="Sign in to Codex")).to_be_visible(
            timeout=30000
        )
        await card.screenshot(path=shot_path("s2-home-sign-in-next"))
        assert process.poll() is None, "the same process: no restart"
    finally:
        stop(process)
    said = world.run(str(codex), "--version").stdout.strip()
    assert OLD_CODEX not in said, said
    assert changed_at(shutil.which("codex")) == owners, "the owner's own Codex"
    log(f"S2 ok: an old Codex updated from Home's card by `codex update` ({said})")


async def restarted_updated(world: World, browser, command: list[str], python: Path, data: Path,
                            port: int, name: str, *path: str, **more):  # fmt: skip
    """A newer release known: Update and restart updates it with its own installer (offline,
    from dist-b) and starts the new version in this one's place."""
    before = world.version(python)
    assert before == OURS, before
    world.configure(python, data, {"copilot": {"enabled": False}, "codex": {"enabled": True}},
                    latest=NEWER)  # fmt: skip
    offline = {
        "PIP_FIND_LINKS": str(world.s / "dist-b"), "PIP_NO_INDEX": "1",
        "UV_FIND_LINKS": str(world.s / "dist-b"), "UV_NO_INDEX": "1",
        "FAKE_CODEX_HOME": str(world.s / "codex-home"),
        **more,
    }  # fmt: skip
    process, url = world.start(command, data, port, str(world.s / "fakebin"), *path, **offline)
    try:
        page = await opened(browser, url)
        line = page.locator("#status-line")
        await expect(line).to_contain_text(NEWER, timeout=30000)
        button = line.get_by_role("button", name="Update and restart")
        await expect(button).to_be_visible()
        await line.screenshot(path=shot_path(f"{name}-update-line"))
        pid = process.pid
        await button.click()
        deadline = time.time() + 300
        while world.version(python) != NEWER:
            assert time.time() < deadline, (world.s / f"chief-{port}.log").read_text()[-3000:]
            assert process.poll() is None, (world.s / f"chief-{port}.log").read_text()[-3000:]
            await asyncio.sleep(1)
        answered(process, data, port, world.s / f"chief-{port}.log", 120)
        assert process.pid == pid and process.poll() is None, "in its place"
        # (The page it had reloads by itself once the new version answers: a page of its own.)
        await page.close()
        page = await opened(browser, url)
        line = page.locator("#status-line")
        await expect(line).not_to_contain_text("Update and restart", timeout=30000)
        await page.screenshot(path=shot_path(f"{name}-updated"))
    finally:
        stop(process)
    log(f"{name.upper()} ok: {before} updated to {NEWER} by Update and restart, in its place")


async def s3(world: World, browser, port: int):
    python = world.venv / "bin" / "python"
    await restarted_updated(world, browser, [str(world.venv / "bin" / "chiefofstaff")], python,
                            world.s / "data-s3", port, "s3")  # fmt: skip


async def s4(world: World, browser, port: int):
    tools = world.s / "pipx-tool"
    world.run(UV, "venv", "--seed", "--python", "3.12", str(tools))
    world.run(str(tools / "bin" / "pip"), "install", "--disable-pip-version-check", "pipx")
    pipx = {"PIPX_HOME": str(world.s / "pipx"), "PIPX_BIN_DIR": str(world.s / "pipx-bin")}
    # By its name, as the owner installs it (OURS: from dist-a, PyPI has none that new).
    world.run(str(tools / "bin" / "pipx"), "install", "chiefofstaff",
              PIP_FIND_LINKS=str(world.s / "dist-a"), **pipx)  # fmt: skip
    python = world.s / "pipx" / "venvs" / "chiefofstaff" / "bin" / "python"
    command = [str(world.s / "pipx-bin" / "chiefofstaff")]
    await restarted_updated(world, browser, command, python, world.s / "data-s4", port, "s4",
                            str(tools / "bin"), **pipx)  # fmt: skip


async def main():
    assert UV, "uv is needed"
    wanted = [arg.upper() for arg in sys.argv[1:] if not arg.startswith("--")] or [
        "S1", "S2", "S3", "S4"
    ]  # fmt: skip
    scratch = Path(tempfile.mkdtemp(prefix="cos-self-handle-"))
    try:
        world = World(scratch)
        world.build()
        world.venv_install()
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
    log(f"LIVE SELF HANDLE PASSED ({' '.join(wanted)})")


if __name__ == "__main__":
    asyncio.run(main())
