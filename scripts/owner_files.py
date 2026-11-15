"""The owner's files a run against the real coding-agent CLIs must leave as they were (the contract
tests, live runs; more-agents.md 5.12.5), recorded before it and checked after it: each byte for
byte, but for the one change listed in ALLOWED_IGNORE_LINES, which Claude Code itself makes and
the guide and SECURITY.md say. Contents are never printed, only which file changed.

    python scripts/owner_files.py record FILE    # before the run
    python scripts/owner_files.py check FILE     # after it: exit 1 naming what changed

Credentials (Codex's auth.json, Claude's sign-in) are never read: they are not here.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

# What Claude Code (2.1.322) adds to the global git ignore file at its first sandboxed command
# in a git repository that does not ignore its staging folder: no setting turns it off.
ALLOWED_IGNORE_LINES = ("**/.claude/.cc-writes/",)


def claude_folder() -> Path:
    given = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(given).expanduser() if given else Path.home() / ".claude"


def codex_folder() -> Path:
    given = os.environ.get("CODEX_HOME")
    return Path(given).expanduser() if given else Path.home() / ".codex"


def global_ignore() -> Path:
    """The global git ignore file, found as Claude Code finds it: core.excludesFile, else
    $XDG_CONFIG_HOME/git/ignore, else ~/.config/git/ignore."""
    named = subprocess.run(
        ["git", "config", "--global", "--get", "core.excludesfile"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()  # fmt: skip
    if named == "~" or named.startswith("~/"):
        return Path.home() / named[2:]
    if named and Path(named).is_absolute():
        return Path(named)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base / "git" / "ignore"


def watched() -> dict[str, Path]:
    """Each file, by a name for the report; a folder's files each on their own."""
    claude, xdg = claude_folder(), os.environ.get("XDG_CONFIG_HOME")
    files = {
        "claude settings.json": claude / "settings.json",
        "claude settings.local.json": claude / "settings.local.json",
        "claude CLAUDE.md": claude / "CLAUDE.md",
        "claude plugins/known_marketplaces.json": claude / "plugins" / "known_marketplaces.json",
        "claude plugins/installed_plugins.json": claude / "plugins" / "installed_plugins.json",
        "codex config.toml": codex_folder() / "config.toml",
        "git ~/.gitconfig": Path.home() / ".gitconfig",
        "git config (XDG)": (Path(xdg) if xdg else Path.home() / ".config") / "git" / "config",
    }
    if not os.environ.get("CLAUDE_CONFIG_DIR"):
        files["claude ~/.claude.json"] = Path.home() / ".claude.json"
    for folder in ("agents", "skills"):
        for path in sorted((claude / folder).rglob("*")) if (claude / folder).is_dir() else []:
            if path.is_file():
                files[f"claude {path.relative_to(claude)}"] = path
    return files


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def record() -> dict:
    """Each file's digest; the global ignore file's lines', one by one (not the lines)."""
    ignore = global_ignore()
    found = lines(ignore)
    return {
        "files": {name: digest(path) for name, path in watched().items()},
        "ignore": {"path": str(ignore), "lines": None if found is None else hashed(found)},
    }


def lines(path: Path) -> list[str] | None:
    return path.read_text(errors="replace").splitlines() if path.is_file() else None


def hashed(found: list[str]) -> list[str]:
    return [hashlib.sha256(line.encode()).hexdigest() for line in found]


def changes(before: dict) -> list[str]:
    """What changed since `before` that ALLOWED_IGNORE_LINES does not allow, by file name."""
    after = record()
    found = [
        f"{name}: {'made' if was is None else 'removed' if now is None else 'changed'}"
        for name in sorted(set(before["files"]) | set(after["files"]))
        if (was := before["files"].get(name)) != (now := after["files"].get(name))
    ]
    where = Path(before["ignore"]["path"])
    old, new = before["ignore"]["lines"], lines(where)
    if old != (None if new is None else hashed(new)):
        kept = old or []
        if new is None or hashed(new)[: len(kept)] != kept:
            found.append(f"global git ignore ({where}): lines changed or removed")
        elif any(line not in ALLOWED_IGNORE_LINES for line in new[len(kept) :]):
            found.append(f"global git ignore ({where}): a line added that is not allowed")
    if str(where) != after["ignore"]["path"]:
        found.append("global git ignore: now another file (core.excludesFile changed)")
    return found


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in {"record", "check"}:
        print("Usage: python scripts/owner_files.py record|check FILE")
        return 2
    file = Path(sys.argv[2])
    if sys.argv[1] == "record":
        file.write_text(json.dumps(record()))
        return 0
    found = changes(json.loads(file.read_text()))
    for change in found:
        print(f"OWNER FILE CHANGED: {change}")
    if not found:
        print("owner's files unchanged (but for what ALLOWED_IGNORE_LINES allows)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
