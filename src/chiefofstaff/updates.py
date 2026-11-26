"""Whether a newer Chief of Staff is out: PyPI's JSON for it read at most once a day, off the
hot path (a plain GET that sends nothing about this machine or its owner), unless Settings turns
it off; how it was launched, and so how it updates: started with uvx or installed with `uv tool`,
Chief of Staff updates itself and starts again in its own place (the same process, terminal and
private link: __main__.restart_into); otherwise the command is said."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import httpx

from .send import throwaway
from .store import Store

PYPI_URL = "https://pypi.org/pypi/chiefofstaff/json"
CHECK_EVERY = 24 * 3600
# What was read last (kv): when, and the newest release then ("" when it could not be read).
LAST = "update_check"
# In the data folder: what a restart to update could not do (__main__.restart_into), said at
# the next start (Runtime.after_update).
UPDATE_NOTE = "update-note"
# What a restart to update was for (kv): the version it ran and the one it was to run.
UPDATING = "updating_from"
RELEASE = re.compile(r"\d+(?:\.\d+){1,3}")


@functools.cache
def installed() -> str:
    try:
        return metadata.version("chiefofstaff")
    except metadata.PackageNotFoundError:
        return ""


def parts(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def newer(latest: str, current: str) -> bool:
    """Whether a release (only numbers: never a pre-release) is newer than this one."""
    if not RELEASE.fullmatch(latest or "") or not RELEASE.fullmatch(current or ""):
        return False
    return parts(latest) > parts(current)


@dataclass(frozen=True)
class Launch:
    """How this Chief of Staff was launched: `how` ("uvx", "tool", "pipx", "pip", "source": a
    checkout of its code), the command that updates it, and what runs in its place to update
    it (none: the owner runs the command)."""

    how: str
    command: str
    restart: tuple[str, ...] = ()

    def to(self, version: str) -> Launch:
        """What runs in its place to update to this version: with uvx, that very version (the
        one the owner was told of), not whatever is newest by then."""
        if self.how != "uvx" or not RELEASE.fullmatch(version):
            return self
        pinned = tuple(f"chiefofstaff@{version}" if word == "chiefofstaff@latest" else word
                       for word in self.restart)  # fmt: skip
        return Launch(self.how, self.command, pinned)


@functools.cache
def editable() -> bool:
    """Whether it runs from a checkout of its code (an editable install: `uv run` there)."""
    try:
        said = metadata.distribution("chiefofstaff").read_text("direct_url.json") or "{}"
        return bool(json.loads(said).get("dir_info", {}).get("editable"))
    except (metadata.PackageNotFoundError, ValueError, AttributeError):
        return True


def own_args(argv: tuple[str, ...] | None = None) -> list[str]:
    """Its arguments (sys.argv's), to start again with: the page it had reconnects (no other)."""
    args = list(sys.argv[1:] if argv is None else argv)
    return args if "--no-browser" in args else [*args, "--no-browser"]


def launched(prefix: str = sys.prefix, argv: tuple[str, ...] | None = None) -> Launch:
    """How it was launched (see Launch); `argv`: its own arguments (sys.argv's)."""
    args = own_args(argv)
    here = Path(prefix).resolve()
    if editable():
        return Launch("source", "git pull")
    windows = sys.platform == "win32"  # (No exec there: the owner restarts it.)
    if throwaway(prefix):
        uvx = shutil.which("uvx")
        restart = (uvx, "chiefofstaff@latest", *args) if uvx and not windows else ()
        return Launch("uvx", "uvx chiefofstaff@latest", restart)
    posix = here.as_posix()
    tools = os.environ.get("UV_TOOL_DIR")
    # Both sides resolved: a folder named through a link (macOS's /var) is still that folder.
    if re.search(r"/uv/(?:data/)?tools/", posix) or (
        tools and here.is_relative_to(Path(tools).resolve())
    ):
        entry = here / "bin" / "chiefofstaff"
        restart = (str(entry), *args) if shutil.which("uv") and not windows else ()
        return Launch("tool", "uv tool upgrade chiefofstaff", restart)
    if "/pipx/venvs/" in posix:
        return Launch("pipx", "pipx upgrade chiefofstaff")
    return Launch("pip", "pip install --upgrade chiefofstaff")


@functools.cache
def upgrade_command() -> str:
    return launched().command


async def fetch(launch: Launch) -> tuple[bool, str]:
    """What updating needs before Chief of Staff stops, so that it starts again at once: with
    uvx, the release it updates to downloaded into uv's cache (`uvx chiefofstaff@<version>
    --version`). (False, why) when it can't be."""
    if launch.how != "uvx":
        return True, ""

    def run() -> tuple[bool, str]:
        try:
            done = subprocess.run(  # noqa: S603  (uvx, which launched it, and its package)
                [*launch.restart[:2], "--version"], capture_output=True, text=True,
                timeout=600, check=False, stdin=subprocess.DEVNULL,
            )  # fmt: skip
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        said = (done.stderr or done.stdout).strip().splitlines()
        return done.returncode == 0, said[-1][:300] if said else ""

    return await asyncio.to_thread(run)


async def fetch_latest() -> str:
    """The newest release on PyPI ("" when it can't be read)."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            reply = await client.get(PYPI_URL, headers={"Accept": "application/json"})
            reply.raise_for_status()
            latest = reply.json()["info"]["version"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return ""
    return latest if isinstance(latest, str) and RELEASE.fullmatch(latest) else ""


async def check(store: Store, now: float | None = None, force: bool = False) -> str:
    """The newest release, read from PyPI when the last look is a day old (or `force`); ""
    when unknown. Never while Settings turns the notice off (unless `force`)."""
    if not force and not store.settings().update_notice:
        return ""
    now = time.time() if now is None else now
    try:
        last = json.loads(store.get(LAST) or "{}")
    except ValueError:
        last = {}
    last = last if isinstance(last, dict) else {}
    if not force and now - float(last.get("at") or 0) < CHECK_EVERY:
        return str(last.get("latest") or "")
    latest = await fetch_latest()
    store.set(LAST, json.dumps({"at": now, "latest": latest or last.get("latest") or ""}))
    return latest or str(last.get("latest") or "")


def notice(store: Store) -> dict | None:
    """What the page says, when a newer release is out and the notice is on: the version, the
    command, and whether Chief of Staff updates itself (`automatic`). None from a checkout of
    its code."""
    if not store.settings().update_notice:
        return None
    try:
        latest = str(json.loads(store.get(LAST) or "{}").get("latest") or "")
    except (ValueError, AttributeError):
        return None
    launch = launch_now()
    if not newer(latest, installed()) or launch.how == "source":
        return None
    return {"version": latest, "command": launch.command, "automatic": bool(launch.restart)}


@functools.cache
def launch_now() -> Launch:
    return launched()


def start_hint(store: Store) -> str:
    """Said when Copilot would not start at all: a newer Chief of Staff may run it (as 0.1.46 did
    a Copilot CLI that had updated itself past 0.1.45)."""
    command = upgrade_command()
    if found := notice(store):
        return f"Chief of Staff {found['version']} is out and may fix this: {command}"
    return f"A newer Chief of Staff may fix this: {command}"
