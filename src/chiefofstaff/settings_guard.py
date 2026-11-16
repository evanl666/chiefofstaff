"""The agent settings a worker's folder may carry, held as Chief of Staff last saw them.

A repository can carry settings its agent loads from the next turn or start, outside Chief of
Staff's checks (policy.AGENT_SETTINGS: hooks that can approve what a check refuses, MCP and
language servers whose commands run outside the sandbox, custom agents with their own of both,
Codex's rules). A worker can write them in its own folder. So Chief of Staff keeps, for each
folder its workers use (by the folder's top: a worktree, a clone, the owner's checkout, a
workspace), what those files hold: a snapshot, in its own store, never in the folder. Before
every turn and start it reads them again (plain file reads, no git: a repository's git config
can run code) and compares; a difference is the worker's until the owner says to use them
(Runtime.accept_settings). A folder Chief of Staff makes (a worktree, a clone, a workspace) is
taken as it made it; any other, the first time it is seen, is the owner's to look at once.

A snapshot's folders: {folder (relative to the top, "." for it): {entry (relative to the
folder): [kind, digest]}} for every file, link or other entry under a setting's path (not
folders: an empty one loads nothing), found by its name compared as the file system may
(case-insensitively, NFC), links recorded with where they lead and what is there. A link out of
the top, or a special file (a FIFO, a socket, a device), is never taken as unchanged (REFUSED).
The files a setting runs (a hook's script, an MCP server's, a language server's), as far as its
commands name them, are held too (RUN entries); a command that runs project code by another
way (`uv run`, `npm run`, `python -m`) is noted, not held. It is stored from the first time a
folder is handed out or checked, empty or not. Agent homes (~/.codex, ~/.claude, ~/.copilot)
are never a project's settings.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import tomllib
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .policy import AGENT_SETTINGS

MOST_ENTRIES = 1000
MOST_BYTES = 16 * 1024 * 1024
KEY = "settingssnap:"
# An entry for a file a setting runs: "<setting's entry>" + RUN + "<that file>".
RUN = "\0run:"
# A kind never taken as unchanged: a link out of the top, a special file.
REFUSED = "!"


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


def checkout_top(checkout: dict) -> Path | None:
    """The top a worker's checkout reads settings up to, as Chief of Staff recorded it when it
    handed the folder out: a worktree's, a clone's, the owner's checkout's (`top`; for one
    recorded before that was kept, the folder of its repository's .git), never what markers in
    the folder say now; else the outermost top an agent would take."""
    if checkout.get("worktree"):
        return Path(os.path.realpath(checkout["worktree"]))
    if checkout.get("top"):
        return Path(os.path.realpath(checkout["top"]))
    if not (path := checkout.get("path")):
        return None
    real = Path(os.path.realpath(path))
    if not checkout.get("shared") and checkout.get("repo"):
        return Path(os.path.realpath(checkout["repo"]))  # A clone.
    git_dir = Path(checkout.get("git_dir") or "")
    if git_dir.name == ".git":
        owner = Path(os.path.realpath(git_dir.parent))
        if real == owner or real.is_relative_to(owner):
            return owner
    return outermost_top(real)


def outermost_top(folder: Path) -> Path:
    """For a folder with no recorded top: the outermost top an agent would read settings up to
    from there: git's (the nearest `.git` that is a repository: a folder with a HEAD, or a file
    naming one) or Codex's (the nearest `.git` file, or folder with a HEAD); else the folder."""
    found = [folder]
    for place in [folder, *folder.parents]:
        if repository(place / ".git"):
            found.append(place)
            break
    for place in [folder, *folder.parents]:
        marker = place / ".git"
        if marker.is_file() or (marker.is_dir() and (marker / "HEAD").exists()):
            found.append(place)
            break
    return min(found, key=lambda place: len(place.parts))


def repository(dot_git: Path) -> bool:
    """Whether a `.git` is a repository git would take: a folder with a HEAD, or a file naming
    one (`gitdir: …`)."""
    try:
        if dot_git.is_dir():
            return (dot_git / "HEAD").is_file()
        if dot_git.is_file():
            named = dot_git.read_text(errors="replace").strip()
            if named.startswith("gitdir:"):
                target = dot_git.parent / named.removeprefix("gitdir:").strip()
                return (target / "HEAD").is_file()
    except OSError:
        pass
    return False


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


def scan(
    top: Path, cwd: Path, names: tuple[str, ...] = AGENT_SETTINGS, code: list | None = None
) -> dict[str, dict]:
    """What the settings named (one agent's, or policy.AGENT_SETTINGS) hold now in each folder
    of the chain, with the files they run; the agents' own folders left out (agent_homes).
    `code`: where to note the commands that run project code no entry holds."""
    budget: list = [MOST_ENTRIES, MOST_BYTES, {}]
    found: dict[str, dict] = {}
    wanted = [name.split("/") for name in names]
    homes = agent_homes()
    for folder in chain(top, cwd):
        entries: dict[str, list] = {}
        seek(top, folder, wanted, "", entries, budget, homes)
        for entry in list(entries):
            entries |= runs(top, folder, entry, entries[entry], budget, code, entries)
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
        kind, value = inside(top, path, budget)
        entries[key] = [kind, f"{path.readlink()}\0{value}" if kind == "l" else value]
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
        entries[key] = [REFUSED, "is a special file"]


def content(path: Path, size: int, budget: list) -> str:
    budget[1] -= size
    if budget[1] < 0:
        raise TooLargeError("too large")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TooLargeError(f"{path.name} could not be read") from exc
    if len(budget) > 2 and path.name.lower().endswith(PARSED):
        budget[2][path] = data  # (Read once: what it runs is read from it next.)
    return hashlib.sha256(data).hexdigest()


def inside(top: Path, link: Path, budget: list) -> tuple[str, str]:
    """("l", what a link leads to: a file's content, a folder's), or (REFUSED, why) when it
    leads out of the top (what is there can change with nothing in the folder changing) or to
    a special file."""
    target = Path(os.path.realpath(link))
    if not target.is_relative_to(os.path.realpath(top)):
        return REFUSED, "leads out of the folder"
    try:
        info = target.stat()
    except FileNotFoundError:
        return "l", "missing"
    except OSError as exc:
        raise TooLargeError(f"{link.name} could not be read") from exc
    if stat.S_ISREG(info.st_mode):
        return "l", content(target, info.st_size, budget)
    if not stat.S_ISDIR(info.st_mode):
        return REFUSED, "is a special file"
    nested: dict[str, list] = {}
    for child in sorted(entry.name for entry in target.iterdir()):
        record(top, target / child, nfc(child), nested, budget)
    if refused := next((value for kind, value in nested.values() if kind == REFUSED), ""):
        return REFUSED, refused
    return "l", "d:" + hashlib.sha256(json.dumps(nested, sort_keys=True).encode()).hexdigest()


# The files a setting runs, as far as its commands name them (best effort): a hook's script, an
# MCP or language server's, a plugin marketplace's folder. Read from the JSON and TOML settings,
# a custom agent's front matter (its mcp-servers), an extension's relative imports.
COMMAND_KEYS = {"command", "bash", "powershell", "pwsh", "sh", "cmd"}
SHELLS = {"bash", "sh", "zsh", "dash", "pwsh", "powershell"}
PROJECT_DIR = re.compile(
    r"\$\{(?:CLAUDE_PROJECT_DIR|workspaceFolder|workspaceRoot|PWD)\}|\$(?:CLAUDE_PROJECT_DIR|PWD)\b"
)
SCRIPTS = {".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts", ".mts", ".rb", ".pl",
           ".ps1", ".php", ".lua", ".jar"}  # fmt: skip
# Commands that run project code no file of theirs names (a package's scripts, a module, a
# build file, a local package's bin): noted, not held.
RUNNERS: dict[str, set[str] | None] = {
    "npm": {"run", "run-script", "exec", "x", "start", "test", "restart"},
    "npx": None, "pnpm": None, "pnpx": None, "yarn": None, "bun": {"run", "x"}, "bunx": None,
    "deno": {"task"}, "uv": {"run"}, "poetry": {"run"}, "pipenv": {"run"}, "pdm": {"run"},
    "hatch": {"run"}, "rye": {"run"}, "make": None, "just": None, "task": None, "rake": None,
    "invoke": None, "nox": None, "tox": None, "cargo": {"run"}, "go": {"run"},
    "dotnet": {"run"}, "gradle": None, "gradlew": None, "mvn": None, "mvnw": None,
    "bundle": {"exec"}, "mix": None, "sbt": None,
}  # fmt: skip
PARSED = (".json", ".toml", ".md", ".mjs", ".cjs", ".js")
IMPORTS = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)["'](\.{1,2}/[^"']+)["']"""
)


def runs(
    top: Path,
    folder: Path,
    entry: str,
    value: list,
    budget: list,
    code: list | None,
    held_already: dict | None = None,
) -> dict[str, list]:
    """The RUN entries of one setting's file: what its commands run, held as files are (but
    for a file held as a setting itself, in `held_already`)."""
    name = entry.split("\0")[0]
    if value[0] not in {"f", "l"} or RUN in entry or not name.lower().endswith(PARSED):
        return {}
    path = folder / name
    real = path if value[0] == "f" else Path(os.path.realpath(path))
    try:
        data = budget[2].get(real) if len(budget) > 2 else None
        text = (data if data is not None else real.read_bytes()).decode(errors="replace")
    except OSError:
        return {}
    held: dict[str, list] = {}
    extension = nfc(entry).casefold().startswith(".github/extensions/")
    place = real.parent if extension else folder
    for token, folders in references(real, text, top, code, f"{folder_key(top, folder)}/{entry}"):
        target = located(top, place, token)
        if target is None or target == Path(top) or Path(top).is_relative_to(target):
            continue
        if target.is_relative_to(folder) and nfc(target.relative_to(folder).as_posix()) in (
            held_already or {}
        ):
            continue  # A setting itself (a hook's script in `.github/hooks/`).
        shown = (
            nfc(target.relative_to(top).as_posix()) if target.is_relative_to(top) else str(target)
        )
        key = f"{entry.split(chr(0))[0]}{RUN}{shown}"
        if key in held:
            continue
        if target.is_dir() and not target.is_symlink():
            if not folders:
                continue  # A folder an argument names (a root, data): not what runs.
            nested: dict[str, list] = {}
            for child in sorted(item.name for item in target.iterdir()):
                record(top, target / child, nfc(child), nested, budget)
            refused = next((v for kind, v in nested.values() if kind == REFUSED), "")
            held[key] = (
                [REFUSED, refused]
                if refused
                else ["d", hashlib.sha256(json.dumps(nested, sort_keys=True).encode()).hexdigest()]
            )
        else:
            record(top, target, key, held, budget)
    return held


def located(top: Path, place: Path, token: str) -> Path | None:
    """The file or folder a command's word names, if it names one there: relative to the
    setting's folder or the top; an absolute one only inside the top."""
    token = PROJECT_DIR.sub(
        lambda _: str(top), token.split("=", 1)[-1] if token.startswith("-") else token
    )
    if not token or token in {".", ".."} or "://" in token or token.startswith(("~", "-")):
        return None
    if Path(token).is_absolute():
        candidates = [Path(token)] if Path(token).is_relative_to(top) else []
    else:
        candidates = [place / token, top / token]
    for candidate in candidates:
        if os.path.lexists(candidate):
            return Path(os.path.normpath(candidate))
    return None


def references(
    path: Path, text: str, top: Path, code: list | None, setting: str
) -> Iterator[tuple[str, bool]]:
    """(word, whether a folder it names is what runs) for each word of a setting's commands
    that may name a file; commands that run project code otherwise go to `code`."""
    suffix = path.suffix.lower()
    if suffix in {".mjs", ".cjs", ".js"}:
        for found in IMPORTS.finditer(text):
            yield found[1], False
        return
    if suffix == ".md":
        commands = front_matter(text)
    else:
        try:
            data = tomllib.loads(text) if suffix == ".toml" else json.loads(text)
        except (ValueError, tomllib.TOMLDecodeError):
            return
        commands = list(walked(data))
    for line, arguments, folders in commands:
        words = split(line) + arguments
        for segment in segments(words):
            if code is not None and (said := project_code(segment)):
                code.append(f"{setting.removeprefix('./')} runs `{said}`")
        for word in words:
            if (
                folders
                or "/" in word
                or word.startswith(".")
                or Path(word).suffix.lower() in SCRIPTS
            ):
                yield word, folders


def walked(data: Any) -> Iterator[tuple[str, list[str], bool]]:
    """(command line, its arguments, whether folders count) in a parsed setting: each command
    (a hook's `command`/`bash`, a server's `command` with its `args`), each `path` (a plugin
    marketplace's own folder)."""
    if isinstance(data, dict):
        arguments = [a for a in data.get("args") or [] if isinstance(a, str)]
        lines = [data[k] for k in COMMAND_KEYS if isinstance(data.get(k), str)]
        for line in lines:
            yield line, arguments, False
        if arguments and not lines:
            yield "", arguments, False
        if isinstance(data.get("path"), str):
            yield "", [data["path"]], True
        for value in data.values():
            yield from walked(value)
    elif isinstance(data, list):
        for value in data:
            yield from walked(value)


def front_matter(text: str) -> list[tuple[str, list[str], bool]]:
    """A custom agent's commands (its mcp-servers), read from its front matter's lines."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    found: list[tuple[str, list[str], bool]] = []
    listing = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, colon, value = line.strip().partition(":")
        value = value.strip().strip("\"'")
        if colon and key.strip("- ") in COMMAND_KEYS and value:
            found.append((value, [], False))
            listing = False
        elif colon and key.strip("- ") == "args":
            listing = not value
            if value.startswith("["):
                items = [item.strip().strip("\"'") for item in value.strip("[]").split(",")]
                found.append(("", [item for item in items if item], False))
        elif listing and line.strip().startswith("- "):
            found.append(("", [line.strip()[2:].strip().strip("\"'")], False))
        else:
            listing = False
    return found


def split(line: str) -> list[str]:
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


def segments(words: list[str]) -> Iterator[list[str]]:
    """The commands of a line, split at its shell operators; a shell's `-c` line read too."""
    current: list[str] = []
    for word in [*words, ";"]:
        if word in {"&&", "||", ";", "|", "&"}:
            if current:
                name = Path(current[0]).name
                if name in SHELLS and "-c" in current[:-1]:
                    yield from segments(split(current[current.index("-c") + 1]))
                else:
                    yield current
            current = []
        else:
            current.append(word)


def project_code(words: list[str]) -> str:
    """The command, shortened, when it runs project code no word of it names as a file."""
    while words and "=" in words[0] and not words[0].startswith("-"):
        words = words[1:]  # Variables set for it.
    if not words:
        return ""
    name = Path(words[0]).name.removesuffix(".exe").removesuffix(".cmd")
    rest = [word for word in words[1:] if not word.startswith("-")]
    if name.startswith("python") or name in {"node", "deno", "bun"}:
        runs_module = "-m" in words[1:] or (name == "deno" and rest[:1] == ["task"])
        if not runs_module:
            return ""
    elif name not in RUNNERS or (
        (wanted := RUNNERS[name]) is not None and not (rest and rest[0] in wanted)
    ):
        return ""
    line = " ".join(words)
    return line if len(line) <= 60 else line[:59] + "…"


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


def shown_entry(folder: str, entry: str) -> str:
    """An entry, as the owner reads it: its path from the top; a file a setting runs, with the
    setting ("tools/server.py (run by .mcp.json)")."""
    prefix = "" if folder == "." else f"{folder}/"
    setting, _, ran = entry.partition(RUN)
    if ran:
        return f"{ran} (run by {prefix}{setting})"
    return prefix + setting.split("\0")[0]


def changes(now: dict[str, dict], then: dict[str, dict]) -> list[tuple[str, str]]:
    """(what, path) for each entry added, changed, removed, or never taken as unchanged
    ("refused": a link out of the folder, a special file, said with why)."""
    found = []
    for folder in sorted(now.keys() | then.keys()):
        current, saved = now.get(folder, {}), then.get(folder, {})
        for entry in sorted(current.keys() | saved.keys()):
            shown = shown_entry(folder, entry)
            if entry in current and current[entry][0] == REFUSED:
                found.append(("refused", f"{shown} {current[entry][1]}"))
            elif entry not in saved:
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
    for what in ("refused", "added", "changed", "removed"):
        if paths := kinds.get(what):
            listed_paths = ", ".join(paths[:3]) + (
                f" and {len(paths) - 3} more" if len(paths) > 3 else ""
            )
            parts.append(listed_paths if what == "refused" else f"{what} {listed_paths}")
    return "; ".join(parts)


def listed_entries(found: list[tuple[str, str]]) -> str:
    """The paths alone ("first time" lines): ".mcp.json, .github/hooks/x.json and 2 more"."""
    paths = [path for _, path in found]
    return ", ".join(paths[:3]) + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")


def digest(snapshot: dict[str, dict]) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()[:16]


def top_key(top: Path) -> str:
    return KEY + nfc(os.path.realpath(top))


# A snapshot, as kept: {"folders": {folder: {entry: [kind, digest]}}, "known": {folder:
# [setting names it holds what was there of]}, "first": {folder: [setting names first seen
# there, not looked at by the owner yet]}, "unchecked": [setting names the owner said to use
# unchecked (too many)], "code": digest of the project code lines said (code_line)}.


def fresh() -> dict:
    return {"folders": {}, "known": {}, "first": {}, "unchecked": [], "code": ""}


def load(store: Any, top: Path) -> dict | None:
    """Its snapshot; None before Chief of Staff first handed it out or checked it."""
    raw = store.get(top_key(top))
    try:
        found = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(found, dict) or not isinstance(found.get("folders"), dict):
        return None
    found.pop("since", None)  # (Of an earlier build: a change time, no longer used.)
    return {**fresh(), **found}


def save(store: Any, top: Path, snapshot: dict):
    store.set(top_key(top), json.dumps(snapshot, sort_keys=True))


def taken(snapshot: dict, now: dict[str, dict], folders: list[str], names: tuple[str, ...]):
    """What `now` holds of these settings in these folders becomes the snapshot's, known (and
    looked at)."""
    for folder in folders:
        kept = {
            entry: value
            for entry, value in snapshot["folders"].get(folder, {}).items()
            if not any(under(entry, name) for name in names)
        }
        kept |= restricted({folder: now.get(folder, {})}, names).get(folder, {})
        snapshot["folders"][folder] = kept
        snapshot["known"][folder] = sorted({*snapshot["known"].get(folder, []), *names})
        if first := [n for n in snapshot["first"].get(folder, []) if n not in names]:
            snapshot["first"][folder] = first
        else:
            snapshot["first"].pop(folder, None)


def unseen(snapshot: dict, now: dict[str, dict], missing: dict[str, tuple[str, ...]]):
    """Folders (and settings) seen for the first time, not made by Chief of Staff: known, with
    nothing in them taken as unchanged; what is there now (`now`) is the owner's to look at
    once ("first"); one with nothing there is known as empty."""
    for folder, names in missing.items():
        there = now.get(folder, {})
        found = [name for name in names if any(under(entry, name) for entry in there)]
        taken(snapshot, {}, [folder], names)
        if found:
            snapshot["first"][folder] = sorted({*snapshot["first"].get(folder, []), *found})


def first_seen(snapshot: dict, now: dict[str, dict], then: dict[str, dict]) -> bool:
    """Whether what differs is only what is in settings seen for the first time, there."""
    found = False
    for folder in now.keys() | then.keys():
        current, saved = now.get(folder, {}), then.get(folder, {})
        first = snapshot["first"].get(folder, [])
        for entry in current.keys() | saved.keys():
            value = current.get(entry)
            if value is None or value[0] == REFUSED:
                return False
            if entry in saved:
                if value == saved[entry]:
                    continue
                return False
            if not any(under(entry, name) for name in first):
                return False
            found = True
    return found


def keep(store: Any, top: Path, cwd: Path, names_by_agent: tuple[tuple[str, ...], ...]):
    """A folder Chief of Staff just made for a worker (a worktree, a clone, a workspace): the
    agent settings in it are its own, as they are now (each agent's: too many or too large
    ones of one agent are checked as they are found, not kept)."""
    snapshot = load(store, top) or fresh()
    folders = [folder_key(top, folder) for folder in chain(top, cwd)]
    for names in names_by_agent:
        try:
            now = scan(top, cwd, names)
        except TooLargeError:
            continue
        taken(snapshot, now, folders, names)
    save(store, top, snapshot)


def refresh(store: Any, top: Path, paths: list[str]):
    """Files Chief of Staff itself put in a folder again (carried from the owner's checkout as
    a worktree is made again): kept as they are now, with what they run, where its snapshot
    knows them."""
    snapshot = load(store, top)
    if snapshot is None or not paths:
        return
    budget = [MOST_ENTRIES, MOST_BYTES]
    for path in paths:
        folder, setting = split_setting(path)
        if setting == path and not any(under(path, name) for name in AGENT_SETTINGS):
            continue  # Not a setting.
        if folder not in snapshot["known"]:
            continue
        entry = path if folder == "." else path[len(folder) + 1 :]
        place = top if folder == "." else top / folder
        held = snapshot["folders"].setdefault(folder, {})
        for key in [key for key in held if key == entry or key.startswith(entry + RUN)]:
            del held[key]
        try:
            record(top, top / path, entry, held, budget)
            if entry in held:
                held |= runs(top, place, entry, held[entry], budget, None)
        except TooLargeError:
            continue
    save(store, top, snapshot)


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
