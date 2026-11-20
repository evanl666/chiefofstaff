"""The owner's files a run against the real coding-agent CLIs must leave as they were (the contract
tests, live runs; more-agents.md 5.12.5), recorded before it and checked after it: each byte for
byte, but for the one change listed in ALLOWED_IGNORE_LINES, which Claude Code itself makes and
the guide and SECURITY.md say. Contents are never printed, only which file changed.

    python scripts/owner_files.py record FILE    # before the run
    python scripts/owner_files.py check FILE     # after it: exit 1 naming what changed
    python scripts/owner_files.py check FILE --live ROOT [--allow KEY,…]
    python scripts/owner_files.py changed-keys FILE

A live run of Claude workers on the owner's own `claude` (scripts/live_claude.py) checks Claude's
state file (`.claude.json`, which every session of Claude's rewrites) key by key instead
(more-agents.md 5.12.5): new `projects` entries for folders under ROOT, none of them trusted;
top-level keys that a plain `claude -p` run changes on this machine (`changed-keys`, after one,
gives them: the run passes them with --allow) or STATE_KEYS; nothing else (no trust, no other
project, no MCP server, no account field). Only key names are printed, never a value. A live run
also allows SYNC_MARKS: the mark Claude writes in the owner's synced claude.ai skills at each
sync round (any session of the owner's signed-in Claude makes it; only hiding those skills from
workers would stop it).

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


# Of Claude's state file (.claude.json): what Claude Code itself rewrites as a session runs, kept
# whatever the run (first-run marks, counters, caches of its own; 2.1.322's names), and what must
# never change in a live run (the owner's MCP servers and account).
STATE_KEYS = {
    "firstStartTime", "firstStartVersion", "machineID", "migrationVersion", "userID",
    "numStartups", "seenNotifications", "pluginUsage", "skillUsage", "tipsHistory",
    "promptQueueUseCount", "lastReleaseNotesSeen", "changelogLastFetched", "cachedChangelog",
    "hasResetAutoModeOptInForDefaultOffer", "opusProMigrationComplete",
    "sonnet1m45MigrationComplete",
    # Experiment caches refresh independently of a plain calibration run: a later session may
    # update them even when calibration found none (live G7 on 2.1.322).
    "cachedExperimentData", "cachedExperimentFeatures", "cachedGrowthBookFeatures",
    "cachedGrowthBookFeaturesAt",
    # When Anthropic's server last confirmed the owner's own Consumer Terms answer: a cache of
    # an hour that any session of a signed-in Claude renews (2.1.322); never a notice marked as
    # seen (a session started without a terminal only says to run `claude` to review them).
    "groveChoiceSeenAt",
}  # fmt: skip
# Of the owner's synced claude.ai skills (~/.claude/skills/synced/<id>/): the mark Claude
# (2.1.322) writes at the end of each sync round, about every ten minutes a session runs.
SYNC_MARKS = (".last-complete-round",)
NEVER = {"mcpServers", "oauthAccount", "primaryApiKey", "customApiKeyResponses", "projects"}


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


def sync_mark(name: str) -> bool:
    """Whether a watched file is a mark of Claude's skill sync rounds (SYNC_MARKS)."""
    parts = name.removeprefix("claude ").split("/")
    return len(parts) == 4 and parts[:2] == ["skills", "synced"] and parts[3] in SYNC_MARKS


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def state_file() -> Path:
    given = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(given).expanduser() / ".claude.json" if given else Path.home() / ".claude.json"


def state() -> dict | None:
    """Claude's state file, key by key: each top-level key's value's digest, and each project's
    (with whether it is trusted); None when it is not there or not an object."""
    try:
        data = json.loads(state_file().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    def value_digest(value) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    return {
        "keys": {key: value_digest(value) for key, value in data.items()},
        "projects": {
            path: {
                "digest": value_digest(entry),
                "trusted": isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True,
            }
            for path, entry in projects.items()
        },
    }


def record() -> dict:
    """Each file's digest; the global ignore file's lines', one by one (not the lines); Claude's
    state file's keys (state)."""
    ignore = global_ignore()
    found = lines(ignore)
    return {
        "files": {name: digest(path) for name, path in watched().items()},
        "ignore": {"path": str(ignore), "lines": None if found is None else hashed(found)},
        "state": state(),
    }


def changed_keys(before: dict) -> list[str]:
    """The top-level keys of Claude's state file that changed since `before` (not projects)."""
    was, now = before.get("state") or {"keys": {}}, state() or {"keys": {}}
    return sorted(
        key for key in set(was["keys"]) | set(now["keys"])
        if key != "projects" and was["keys"].get(key) != now["keys"].get(key)
    )  # fmt: skip


def state_changes(before: dict, roots: list[str], allowed: set[str]) -> list[str]:
    """What changed in Claude's state file since `before` that a live run may not change."""
    was, now = before.get("state"), state()
    if was is None or now is None:
        return [] if was == now else ["claude .claude.json: made or removed"]
    found = [
        f"claude .claude.json: {key} changed"
        for key in changed_keys(before)
        if key in NEVER or key not in STATE_KEYS | allowed
    ]
    real = [os.path.realpath(root) for root in roots]
    for path in sorted(set(was["projects"]) | set(now["projects"])):
        old, new = was["projects"].get(path), now["projects"].get(path)
        if old == new:
            continue
        ours = any(
            os.path.realpath(path) == root or os.path.realpath(path).startswith(root + os.sep)
            for root in real
        )
        if new is None:
            found.append(f"claude .claude.json: a project removed ({'ours' if ours else 'theirs'})")
        elif new["trusted"] and not (old and old["trusted"]):
            found.append("claude .claude.json: a project trusted")
        elif not ours:
            found.append("claude .claude.json: another project changed")
    return found


def lines(path: Path) -> list[str] | None:
    return path.read_text(errors="replace").splitlines() if path.is_file() else None


def hashed(found: list[str]) -> list[str]:
    return [hashlib.sha256(line.encode()).hexdigest() for line in found]


def changes(
    before: dict, roots: list[str] | None = None, allowed: set[str] = frozenset()
) -> list[str]:
    """What changed since `before` that ALLOWED_IGNORE_LINES does not allow, by file name (with
    `roots`: a live run's, Claude's state file key by key, state_changes)."""
    after = record()
    found = [
        f"{name}: {'made' if was is None else 'removed' if now is None else 'changed'}"
        for name in sorted(set(before["files"]) | set(after["files"]))
        if (was := before["files"].get(name)) != (now := after["files"].get(name))
        and not (roots is not None and (name == "claude ~/.claude.json" or sync_mark(name)))
    ]
    if roots is not None:
        found += state_changes(before, roots, set(allowed))
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
    if len(sys.argv) < 3 or sys.argv[1] not in {"record", "check", "changed-keys"}:
        print("Usage: python scripts/owner_files.py record|check|changed-keys FILE [--live ROOT]"
              " [--allow KEY,…]")  # fmt: skip
        return 2
    file = Path(sys.argv[2])
    if sys.argv[1] == "record":
        file.write_text(json.dumps(record()))
        return 0
    if sys.argv[1] == "changed-keys":
        print(",".join(changed_keys(json.loads(file.read_text()))))
        return 0
    options = {sys.argv[i]: sys.argv[i + 1] for i in range(3, len(sys.argv) - 1, 2)}
    roots = [options["--live"]] if "--live" in options else None
    allowed = set(filter(None, options.get("--allow", "").split(",")))
    found = changes(json.loads(file.read_text()), roots, allowed)
    for change in found:
        print(f"OWNER FILE CHANGED: {change}")
    if not found:
        print("owner's files unchanged (but for what ALLOWED_IGNORE_LINES allows)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
