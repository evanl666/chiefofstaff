"""Whether a newer Chief of Staff is out: PyPI's JSON for it read at most once a day, off the
hot path (a plain GET that sends nothing about this machine or its owner), unless Settings turns
it off; and the command that updates it the way it was installed."""

from __future__ import annotations

import functools
import json
import os
import re
import sys
import time
from importlib import metadata
from pathlib import Path

import httpx

from .send import throwaway
from .store import Store

PYPI_URL = "https://pypi.org/pypi/chiefofstaff/json"
CHECK_EVERY = 24 * 3600
# What was read last (kv): when, and the newest release then ("" when it could not be read).
LAST = "update_check"
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


@functools.cache
def upgrade_command(prefix: str = sys.prefix) -> str:
    """How the owner updates it, as it was installed: uvx runs a cached one unless told the
    latest; `uv tool` and pipx upgrade their own; else pip in its environment."""
    if throwaway(prefix):
        return "uvx chiefofstaff@latest"
    posix = Path(prefix).as_posix()
    tools = os.environ.get("UV_TOOL_DIR")
    if re.search(r"/uv/(?:data/)?tools/", posix) or (tools and Path(prefix).is_relative_to(tools)):
        return "uv tool upgrade chiefofstaff"
    if "/pipx/venvs/" in posix:
        return "pipx upgrade chiefofstaff"
    return "pip install --upgrade chiefofstaff"


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
    """What the page says, when a newer release is out and the notice is on."""
    if not store.settings().update_notice:
        return None
    try:
        latest = str(json.loads(store.get(LAST) or "{}").get("latest") or "")
    except (ValueError, AttributeError):
        return None
    if not newer(latest, installed()):
        return None
    return {"version": latest, "command": upgrade_command()}


def start_hint(store: Store) -> str:
    """Said when Copilot would not start at all: a newer Chief of Staff may run it (as 0.1.46 did
    a Copilot CLI that had updated itself past 0.1.45)."""
    command = upgrade_command()
    if found := notice(store):
        return f"Chief of Staff {found['version']} is out and may fix this: {command}"
    return f"A newer Chief of Staff may fix this: {command}"
