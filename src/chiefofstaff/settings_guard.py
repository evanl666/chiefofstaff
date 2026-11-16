"""The agent settings a worker's folder may carry, held as Chief of Staff last saw them.

A repository can carry settings its agent loads from the next turn or start, outside Chief of
Staff's checks (policy.AGENT_SETTINGS: hooks that can approve what a check refuses, MCP servers
whose commands run outside the sandbox, custom agents with their own of both, Codex's rules).
A worker can write them in its own folder. So Chief of Staff keeps, for each folder it hands a
worker (by the folder's top: a worktree, a clone, the owner's checkout, a workspace), what those
files held then: a snapshot, in its own store, never in the folder. Before every turn and start
it reads them again (plain file reads, no git: a repository's git config can run code) and
compares; a difference is the worker's until the owner says to use them
(Runtime.accept_settings).

A snapshot's folders: {folder (relative to the top, "." for it): {entry (relative to the
folder): [kind, digest]}} for every file, link or other entry under a setting's path (not
folders: an empty one loads nothing), found by its name compared as the file system may
(case-insensitively, NFC), links recorded with where they lead (and what is there, inside the
folder). It is stored from the first time a folder is handed out or checked, empty or not: a
folder with none is one Chief of Staff knows (see `fresh` for the rest of what it holds). Agent
homes (~/.codex, ~/.claude, ~/.copilot) are never a project's settings.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from .policy import AGENT_SETTINGS

MOST_ENTRIES = 1000
MOST_BYTES = 16 * 1024 * 1024
KEY = "settingssnap:"


class SettingsChangedError(RuntimeError):
    """A Copilot worker's folder holds agent settings changed since Chief of Staff last saw
    them: it is not opened, nor sent a turn, there until the owner says to use them."""


class TooLargeError(Exception):
    """More, or larger, settings than a snapshot holds: they are never taken as unchanged."""


def nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def same_name(found: str, wanted: str) -> bool:
    """As a case-insensitive file system (macOS's, Windows') resolves a name, NFC or NFD."""
    return nfc(found).casefold() == nfc(wanted).casefold()


def chain(top: Path, cwd: Path) -> list[Path]:
    """The folders an agent reads settings from for `cwd`: it and those above it up to the top."""
    if cwd != top and not cwd.is_relative_to(top):
        return [cwd]
    return [cwd, *[parent for parent in cwd.parents if parent.is_relative_to(top)]]


def folder_key(top: Path, folder: Path) -> str:
    return "." if folder == top else nfc(folder.relative_to(top).as_posix())


def agent_homes() -> set[str]:
    """The agents' own folders (Codex's, Claude's, Copilot's: their sessions, logs and the
    owner's own settings), as real paths: never a folder's settings, though a session in home
    finds one by such a name (~/.codex)."""
    home = Path.home()
    found = {home / ".codex", home / ".claude", home / ".copilot"}
    for variable in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "COPILOT_HOME"):
        if value := os.environ.get(variable):
            found.add(Path(value).expanduser())
    return {os.path.realpath(path) for path in found}


def scan(top: Path, cwd: Path, names: tuple[str, ...] = AGENT_SETTINGS) -> dict[str, dict]:
    """What the settings named (one agent's, or policy.AGENT_SETTINGS) hold now in each folder
    of the chain; the agents' own folders left out (agent_homes)."""
    budget = [MOST_ENTRIES, MOST_BYTES]
    found: dict[str, dict] = {}
    wanted = [name.split("/") for name in names]
    homes = agent_homes()
    for folder in chain(top, cwd):
        entries: dict[str, list] = {}
        seek(top, folder, wanted, "", entries, budget, homes)
        if entries:
            found[folder_key(top, folder)] = entries
    return found


def listed(folder: Path) -> list[os.DirEntry]:
    try:
        with os.scandir(folder) as entries:
            return sorted(entries, key=lambda entry: entry.name)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise TooLargeError(f"{folder} could not be read") from exc


def seek(
    top: Path,
    folder: Path,
    wanted: list[list[str]],
    prefix: str,
    entries: dict,
    budget: list,
    homes: set[str],
):
    """Each entry named by one of `wanted` (a setting's path, by its names) under `folder`,
    the folder read once; a link on the way is recorded, not followed; an agent's own folder
    (`homes`) skipped."""
    by_first: dict[str, list[list[str]]] = {}
    for parts in wanted:
        by_first.setdefault(nfc(parts[0]).casefold(), []).append(parts)
    for entry in listed(folder):
        group = by_first.get(nfc(entry.name).casefold())
        if not group:
            continue
        name = f"{prefix}{nfc(entry.name)}"
        path = folder / entry.name
        if os.path.realpath(path) in homes:
            continue
        rest = [parts[1:] for parts in group if len(parts) > 1]
        if len(rest) < len(group) or entry.is_symlink():
            record(top, path, name, entries, budget)
        elif entry.is_dir(follow_symlinks=False):
            seek(top, path, rest, f"{name}/", entries, budget, homes)


def record(top: Path, path: Path, name: str, entries: dict, budget: list):
    budget[0] -= 1
    if budget[0] < 0:
        raise TooLargeError("too many files")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise TooLargeError(f"{name} could not be read") from exc
    key = name if name not in entries else f"{name}\0{path.name}"
    if stat.S_ISLNK(info.st_mode):
        target = str(path.readlink())
        entries[key] = ["l", f"{target}\0{inside(top, path, budget)}"]
    elif stat.S_ISDIR(info.st_mode):
        # What it holds, not the folder: an empty one (a sandbox's placeholder) loads nothing.
        try:
            children = sorted(child.name for child in path.iterdir())
        except OSError as exc:
            raise TooLargeError(f"{name} could not be read") from exc
        for child in children:
            record(top, path / child, f"{name}/{nfc(child)}", entries, budget)
    elif stat.S_ISREG(info.st_mode):
        entries[key] = ["f", content(path, info.st_size, budget)]
    else:
        entries[key] = ["o", str(stat.S_IFMT(info.st_mode))]


def content(path: Path, size: int, budget: list) -> str:
    budget[1] -= size
    if budget[1] < 0:
        raise TooLargeError("too large")
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise TooLargeError(f"{path.name} could not be read") from exc


def inside(top: Path, link: Path, budget: list) -> str:
    """What a link leads to when that is in the folder (its content, a folder's listing);
    "outside" when it leads out of the folder (only where it leads is held then)."""
    target = Path(os.path.realpath(link))
    if not target.is_relative_to(os.path.realpath(top)):
        return "outside"
    if target.is_file():
        return content(target, target.stat().st_size, budget)
    if target.is_dir():
        nested: dict[str, list] = {}
        for child in sorted(entry.name for entry in target.iterdir()):
            record(top, target / child, nfc(child), nested, budget)
        return "d:" + hashlib.sha256(json.dumps(nested, sort_keys=True).encode()).hexdigest()
    return "missing"


def restricted(snapshot: dict[str, dict], names: tuple[str, ...]) -> dict[str, dict]:
    """Of a snapshot, the entries under these settings' names (one agent's, say)."""
    out: dict[str, dict] = {}
    for folder, entries in snapshot.items():
        kept = {
            entry: value
            for entry, value in entries.items()
            if any(under(entry, name) for name in names)
        }
        if kept:
            out[folder] = kept
    return out


def under(entry: str, name: str) -> bool:
    parts, wanted = entry.split("\0")[0].split("/"), name.split("/")
    return len(parts) >= len(wanted) and all(
        same_name(part, want) for part, want in zip(parts, wanted, strict=False)
    )


def changes(now: dict[str, dict], then: dict[str, dict]) -> list[tuple[str, str]]:
    """(what, path) for each entry added, changed or removed, path from the top."""
    found = []
    for folder in sorted(now.keys() | then.keys()):
        current, saved = now.get(folder, {}), then.get(folder, {})
        prefix = "" if folder == "." else f"{folder}/"
        for entry in sorted(current.keys() | saved.keys()):
            shown = prefix + entry.split("\0")[0]
            if entry not in saved:
                found.append(("added", shown))
            elif entry not in current:
                found.append(("removed", shown))
            elif current[entry] != saved[entry]:
                found.append(("changed", shown))
    return found


def described(found: list[tuple[str, str]]) -> str:
    """One line: "added .mcp.json; changed .github/hooks/x.json"."""
    kinds: dict[str, list[str]] = {}
    for what, path in found:
        kinds.setdefault(what, []).append(path)
    parts = []
    for what in ("added", "changed", "removed"):
        if paths := kinds.get(what):
            listed_paths = ", ".join(paths[:3]) + (
                f" and {len(paths) - 3} more" if len(paths) > 3 else ""
            )
            parts.append(f"{what} {listed_paths}")
    return "; ".join(parts)


def digest(snapshot: dict[str, dict]) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()[:16]


def top_key(top: Path) -> str:
    return KEY + nfc(os.path.realpath(top))


# A snapshot, as kept: {"since": when Chief of Staff first handed the folder (its top) out,
# "folders": {folder: {entry: [kind, digest]}}, "known": {folder: [setting names it holds what
# was there of]}, "unchecked": [setting names the owner said to use unchecked (too many)]}.


def fresh(since: float) -> dict:
    return {"since": since, "folders": {}, "known": {}, "unchecked": []}


def load(store: Any, top: Path) -> dict | None:
    """Its snapshot; None before Chief of Staff first handed it out or checked it (a folder
    a worker of an older version had)."""
    raw = store.get(top_key(top))
    try:
        found = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(found, dict) or not isinstance(found.get("folders"), dict):
        return None
    return {**fresh(float(found.get("since") or 0)), **found}


def save(store: Any, top: Path, snapshot: dict):
    store.set(top_key(top), json.dumps(snapshot, sort_keys=True))


def taken(snapshot: dict, now: dict[str, dict], folders: list[str], names: tuple[str, ...]):
    """What `now` holds of these settings in these folders becomes the snapshot's, known."""
    for folder in folders:
        kept = {
            entry: value
            for entry, value in snapshot["folders"].get(folder, {}).items()
            if not any(under(entry, name) for name in names)
        }
        kept |= restricted({folder: now.get(folder, {})}, names).get(folder, {})
        snapshot["folders"][folder] = kept
        snapshot["known"][folder] = sorted({*snapshot["known"].get(folder, []), *names})


def keep(store: Any, top: Path, cwd: Path, since: float):
    """A folder Chief of Staff just made for a worker (a worktree, a clone): the agent settings
    in it are its own, as they are now. Too many or too large ones: nothing kept (checked as they
    are found)."""
    snapshot = load(store, top) or fresh(since)
    snapshot["since"] = since
    try:
        now = scan(top, cwd)
    except TooLargeError:
        now = None
    if now is not None:
        taken(
            snapshot, now, [folder_key(top, folder) for folder in chain(top, cwd)], AGENT_SETTINGS
        )
    save(store, top, snapshot)


def refresh(store: Any, top: Path, paths: list[str]):
    """Files Chief of Staff itself put in a folder again (carried from the owner's checkout as
    a worktree is made again): kept as they are now, where its snapshot knows them."""
    snapshot = load(store, top)
    if snapshot is None or not paths:
        return
    for path in paths:
        folder, setting = split_setting(path)
        if setting == path and not any(under(path, name) for name in AGENT_SETTINGS):
            continue  # Not a setting.
        if folder not in snapshot["known"]:
            continue
        entry = path if folder == "." else path[len(folder) + 1 :]
        where = top / path
        try:
            info = os.lstat(where)
        except OSError:
            snapshot["folders"].get(folder, {}).pop(entry, None)
            continue
        if stat.S_ISREG(info.st_mode):
            budget = [MOST_ENTRIES, MOST_BYTES]
            snapshot["folders"].setdefault(folder, {})[entry] = [
                "f",
                content(where, info.st_size, budget),
            ]
    save(store, top, snapshot)


# Windows' st_ctime is a file's creation time, which its owner can set back.
CHANGE_TIME_KNOWN = sys.platform != "win32"


def older_than(path: Path, moment: float) -> bool:
    """Whether an entry last changed before `moment` (its inode's change time, which no worker
    can set back): it was there before Chief of Staff first handed the folder out. Unknown on
    Windows (CHANGE_TIME_KNOWN): never then."""
    if not CHANGE_TIME_KNOWN:
        return False
    try:
        return os.lstat(path).st_ctime < moment
    except OSError:
        return False


def epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


# A worktree made before snapshots were kept: what it was given is its base commit's content,
# read once with a git that runs nothing of the repository's (no lazy fetch, no transport, no
# file-system monitor, no hooks, no system or global configuration, no replaced objects), on the
# owner's repository's own .git only.
HARDENED = (
    "-c", "protocol.allow=never", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
    "--no-replace-objects", "--literal-pathspecs",
)  # fmt: skip


def base_files(common: Path, base: str, paths: list[str]) -> dict[str, tuple[str, str]] | None:
    """path -> (mode, blob id) of what `base` holds under `paths`; None when git cannot say."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "CHIEFOFSTAFF_"))
    } | {
        "GIT_DIR": str(common),
        "GIT_COMMON_DIR": str(common),
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }
    try:
        done = subprocess.run(  # noqa: S603  (git, as above)
            ["git", *HARDENED, "ls-tree", "-r", "-z", "--full-tree", base, "--", *paths],  # noqa: S607
            env=environment,
            cwd=str(common),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, NotADirectoryError):
        return None
    if done.returncode:
        return None
    files = {}
    for line in done.stdout.decode(errors="surrogateescape").split("\0"):
        info, tab, name = line.partition("\t")
        if tab:
            mode, _, rest = info.partition(" ")
            files[nfc(name)] = (mode, rest.partition(" ")[2])
    return files


def split_setting(path: str) -> tuple[str, str]:
    """(folder, setting) of a path from the top: "sub/.github/hooks/x.json" -> ("sub",
    ".github/hooks")."""
    parts = path.split("/")
    for index in range(len(parts)):
        for name in AGENT_SETTINGS:
            wanted = name.split("/")
            here = parts[index : index + len(wanted)]
            if len(here) == len(wanted) and all(map(same_name, here, wanted)):
                return "/".join(parts[:index]) or ".", "/".join(here)
    return ".", path


def committed(
    top: Path, now: dict[str, dict], common: Path, base: str, owner: Path, carried: list[str]
) -> dict[str, dict]:
    """Of the settings on the disk, those a worktree was given: as its base commit holds them,
    or carried from the owner's checkout and as they are there."""
    paths = []
    for folder in now:
        prefix = "" if folder == "." else f"{folder}/"
        paths += [prefix + name for name in AGENT_SETTINGS]
    files = base_files(common, base, paths)
    if files is None:
        return {}
    kept: dict[str, dict] = {}
    for folder, entries in now.items():
        prefix = "" if folder == "." else f"{folder}/"
        for entry, (kind, value) in entries.items():
            name = prefix + entry.split("\0")[0]
            path = top / name
            if name in carried and kind == "f":
                try:
                    theirs = hashlib.sha256((owner / name).read_bytes()).hexdigest()
                except OSError:
                    theirs = ""
                given = theirs == value
            else:
                mode, oid = files.get(name, ("", ""))
                try:
                    data = str(path.readlink()).encode() if kind == "l" else path.read_bytes()
                except OSError:
                    data = None
                wanted = {"l": {"120000"}, "f": {"100644", "100755"}}.get(kind, set())
                given = data is not None and mode in wanted and oid in blob_id(data, len(oid))
            if given:
                kept.setdefault(folder, {})[entry] = [kind, value]
    return kept


def shown(snapshot: dict[str, dict]) -> list[str]:
    """The settings a snapshot holds, by name ("sub/.mcp.json", ".github/hooks")."""
    names = set()
    for folder, entries in snapshot.items():
        for entry in entries:
            where, setting = split_setting(entry.split("\0")[0])
            names.add("/".join(p for p in (folder, where, setting) if p and p != "."))
    return sorted(names)


def blob_id(data: bytes, length: int) -> set[str]:
    algorithm = hashlib.sha1 if length == 40 else hashlib.sha256
    return {
        algorithm(b"blob %d\0" % len(variant) + variant, usedforsecurity=False).hexdigest()
        for variant in {data, data.replace(b"\r\n", b"\n")}
    }
