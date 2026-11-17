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

import errno
import hashlib
import json
import os
import re
import shlex
import stat
import tomllib
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import suppress
from itertools import pairwise
from pathlib import Path
from typing import Any, NamedTuple, cast

from .policy import AGENT_SETTINGS

MOST_ENTRIES = 1000
MOST_BYTES = 16 * 1024 * 1024
KEY = "settingssnap:"
# An entry for a file a setting runs: "<setting's entry>" + RUN + "<that file>" for a hook's
# (held: a change waits for the owner), + SERVES + "<server>\0<that file>" for a server's (a
# change turns that server off for the worker: Runtime.servers_off).
RUN = "\0run:"
SERVES = "\0serves:"
# A kind never taken as unchanged: a link where workers can write, a special file.
REFUSED = "!"
# A program a setting runs through a link out of the folder: [PROGRAM, where it leads, "w" when
# that is where workers can write]. Like a program found on PATH, only the second kind counts.
PROGRAM = "p"


class SettingsChangedError(RuntimeError):
    """A Copilot worker's folder holds agent settings changed since Chief of Staff last saw
    them: it is not opened, nor sent a turn, there until the owner says to use them."""


class TooLargeError(Exception):
    """More, or larger, settings than a snapshot holds: they are never taken as unchanged."""


class SpecialFileError(OSError):
    """Where a file was to be read: a FIFO, a socket, a device (or, not following links, a
    link), whose read could wait forever."""


def read_regular(
    path: Path | str, limit: int = MOST_BYTES, follow: bool = False, whole: bool = True
) -> bytes:
    """A regular file's bytes, for every file the checks read: opened without waiting (a FIFO
    opened to read would wait for a writer) and, unless `follow`, without following a link;
    what was opened is what is read (no look first and read after, which a swap could come
    between). SpecialFileError for anything but a regular file; past `limit` bytes, an
    OSError (EFBIG), or (not `whole`) its first `limit` bytes."""
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | (0 if follow else os.O_NOFOLLOW)
    try:
        handle = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP and not follow:
            raise SpecialFileError(errno.ELOOP, "a link, not a file", str(path)) from exc
        raise
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            raise SpecialFileError(errno.EINVAL, "not a regular file", str(path))
        if whole and info.st_size > limit:
            raise OSError(errno.EFBIG, "too large", str(path))
        chunks, total = [], 0
        while chunk := os.read(handle, min(1024 * 1024, limit + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                if whole:
                    raise OSError(errno.EFBIG, "too large", str(path))
                break
        return b"".join(chunks)[:limit]
    finally:
        os.close(handle)


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
            return git_folder(dot_git)
        if dot_git.is_file():
            named = read_regular(dot_git, 4096, follow=True).decode(errors="replace").strip()
            if named.startswith("gitdir:"):
                return git_folder(dot_git.parent / named.removeprefix("gitdir:").strip())
    except OSError:
        pass
    return False


def git_folder(folder: Path) -> bool:
    """As git tells a repository's folder: a HEAD with objects and refs (or a worktree's own
    folder, which names the repository's: commondir)."""
    if not (folder / "HEAD").is_file():
        return False
    return (folder / "commondir").is_file() or (
        (folder / "objects").is_dir() and (folder / "refs").is_dir()
    )


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
    top: Path,
    cwd: Path,
    names: tuple[str, ...] = AGENT_SETTINGS,
    code: list | None = None,
    writable: Callable[[], tuple[str, ...]] | tuple[str, ...] = (),
) -> dict[str, dict]:
    """What the settings named (one agent's, or policy.AGENT_SETTINGS) hold now in each folder
    of the chain, with the files they run; the agents' own folders left out (agent_homes).
    `code`: where to note the commands that run project code no entry holds. `writable`: the
    folders workers can write (real paths; or what says them, asked only for a link out)."""
    budget: list = [MOST_ENTRIES, MOST_BYTES, {}, writable]
    found: dict[str, dict] = {}
    wanted = [name.split("/") for name in names]
    homes = agent_homes()
    for folder in chain(top, cwd):
        entries: dict[str, list] = {}
        seek(top, folder, wanted, "", entries, budget, homes)
        for entry in list(entries):
            entries |= runs(top, folder, entry, entries[entry], budget, code, entries, cwd)
        if entries:
            found[folder_key(top, folder)] = entries
    return found


def signature(top: Path, cwd: Path, names: tuple[str, ...], also: list[str]) -> str:
    """What the file system says of these settings (each entry's kind, inode, size and change
    times, no content read) and of `also` (the files they run): cheap enough to look at before
    every tool a worker runs; a change means the full check (scan) runs. Each setting's own
    path wholly (up to the caps), and the folders on its way (a folder renamed over `.github`
    is another one): what fills a folder beside it (`.github/workflows`) neither costs the look
    nor uses up its caps. Never raises: what can't be looked at (a folder that can't be listed)
    is noted as such, a change the full check then finds can't be checked."""
    seen: list = []
    noted: set[Path] = set()

    def note(path: Path, depth: int):
        if path in noted:
            return
        noted.add(path)
        try:
            info = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return
        except OSError as exc:
            seen.append((str(path), "unseen", exc.errno))
            return
        target: tuple = ()
        if stat.S_ISLNK(info.st_mode):
            # Where it leads, and what is there (a link to nothing whose target is made later).
            with suppress(OSError):
                target = (str(path.readlink()),)
            with suppress(OSError):
                led = path.stat()
                target += (led.st_mode, led.st_ino, led.st_size, led.st_mtime_ns, led.st_ctime_ns)
        seen.append((str(path), info.st_mode, info.st_ino, info.st_size, info.st_mtime_ns,
                     info.st_ctime_ns, target))  # fmt: skip
        if stat.S_ISDIR(info.st_mode) and depth < 4 and len(seen) < MOST_ENTRIES:
            try:
                entries = listed(path)
            except TooLargeError as exc:
                cause = exc.__cause__
                seen.append((str(path), "unlisted", getattr(cause, "errno", None)))
                return
            for entry in entries:
                note(path / entry.name, depth + 1)

    for folder in chain(top, cwd):
        note(folder, 4)
        for name in names:
            parts = name.split("/")
            for end in range(1, len(parts)):
                note(folder.joinpath(*parts[:end]), 4)
            note(folder / name, 0)
    for path in also:
        note(Path(path), 4)
    return hashlib.sha256(repr(seen).encode()).hexdigest()[:16]


def glimpse(top: Path, cwd: Path, names: tuple[str, ...]) -> str:
    """Of settings too many or too large to check (TooLargeError), what can be read: each
    entry's name and size, and the content of each file up to a few MB; what the owner said to
    use unchecked is that, in those folders, and nothing else."""
    seen: list = []
    budget = [MOST_ENTRIES * 20, MOST_BYTES * 4]

    def note(path: Path, name: str, depth: int):
        try:
            info = os.lstat(path)
        except OSError:
            return
        # Its kind, not its permissions: Copilot makes a hook's script executable as it runs it.
        found: list = [name, stat.S_IFMT(info.st_mode), info.st_size]
        if stat.S_ISREG(info.st_mode) and info.st_size <= 4 * 1024 * 1024 and budget[1] > 0:
            budget[1] -= info.st_size
            with suppress(OSError):
                found.append(hashlib.sha256(read_regular(path, 4 * 1024 * 1024)).hexdigest())
        elif stat.S_ISLNK(info.st_mode):
            found.append(os.path.realpath(path))
        seen.append(found)
        budget[0] -= 1
        if stat.S_ISDIR(info.st_mode) and depth < 8 and budget[0] > 0:
            try:
                entries = listed(path)
            except TooLargeError:
                found.append("unlisted")
                return
            for entry in entries:
                note(path / entry.name, f"{name}/{nfc(entry.name)}", depth + 1)

    for folder in chain(top, cwd):
        for name in names:
            note(folder / name, f"{folder_key(top, folder)}/{name}", 0)
    return hashlib.sha256(repr(seen).encode()).hexdigest()[:16]


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
            if entry.is_symlink() and os.path.realpath(folder) != os.path.realpath(Path.home()):
                entries[name] = [REFUSED, "leads to an agent's own folder"]
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


def workers_write(path: Path, budget: list) -> bool:
    """Whether a path is in a folder some worker can write (Runtime.writable_roots)."""
    if len(budget) < 4:
        return False
    roots: Any = budget[3]
    if callable(roots):
        roots = budget[3] = tuple(cast(Callable[[], tuple[str, ...]], roots)())
    return any(path == Path(root) or path.is_relative_to(root) for root in roots)


def content(path: Path, size: int, budget: list) -> str:
    budget[1] -= size
    if budget[1] < 0:
        raise TooLargeError("too large")
    try:
        data = read_regular(path, budget[1] + size)
    except OSError as exc:
        if exc.errno == errno.EFBIG:
            raise TooLargeError("too large") from exc
        raise TooLargeError(f"{path.name} could not be read") from exc
    if len(budget) > 2 and path.name.lower().endswith(PARSED):
        budget[2][path] = data  # (Read once: what it runs is read from it next.)
    return hashlib.sha256(data).hexdigest()


def inside(top: Path, link: Path, budget: list) -> tuple[str, str]:
    """("l", what a link leads to: a file's content, a folder's, wherever it is: a settings file
    the owner keeps in a dotfiles repository), or (REFUSED, why) when it leads where workers
    can write (what is there could change with nothing here changing) or to a special file."""
    target = Path(os.path.realpath(link))
    if not target.is_relative_to(os.path.realpath(top)) and workers_write(target, budget):
        return REFUSED, "leads to a folder workers can write"
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
# a custom agent's front matter (its mcp-servers and hooks), an extension's relative imports.
# Two kinds (Ref.serves): what a hook runs (or a plugin, an extension: in the same turn, on
# every tool) is held as the setting is (RUN entries: a change waits for the owner); what a
# server runs (MCP, language, a custom agent's) is compared too, but a change only turns that
# server off for the worker (SERVES entries, Runtime.servers_off), never a wait. A command the
# parser does not follow (a substitution, a loop, a pipe into a shell, a variable it can't
# expand) is said in `code`, never dropped.
COMMAND_KEYS = {"command", "bash", "powershell", "pwsh", "sh", "cmd"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "pwsh", "powershell"}
# Where a setting lists servers by name: {key, case-insensitive: the servers' kind}.
SERVER_KEYS = {
    "mcpservers": "mcp", "servers": "mcp", "mcp-servers": "mcp", "mcp_servers": "mcp",
    "lspservers": "lsp", "lsp-servers": "lsp", "lsp_servers": "lsp",
}  # fmt: skip
SESSION_DIR = re.compile(r"\$\{COPILOT_SESSION_CWD\}|\$COPILOT_SESSION_CWD\b")
PROJECT_DIR = re.compile(
    r"\$\{(?:CLAUDE_PROJECT_DIR|workspaceFolder|workspaceRoot|PWD)\}|\$(?:CLAUDE_PROJECT_DIR|PWD)\b"
)
# `${VAR:-default}`, `${VAR-default}`: the variable's value where it is one of those above.
DEFAULTED = re.compile(r"\$\{(\w+):?-[^}]*\}")
KNOWN_VARIABLES = {"CLAUDE_PROJECT_DIR", "workspaceFolder", "workspaceRoot", "PWD",
                   "COPILOT_SESSION_CWD"}  # fmt: skip
SCRIPTS = {".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts", ".mts", ".rb", ".pl",
           ".ps1", ".php", ".lua", ".jar", ".awk", ".sed", ".jq", ".tcl", ".fish"}  # fmt: skip
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
# A file a setting expects that is not there (yet): compared as such, so one made later (a
# build's output, a planted script) is a change.
ABSENT = ["-", ""]


def runs(
    top: Path,
    folder: Path,
    entry: str,
    value: list,
    budget: list,
    code: list | None,
    held_already: dict | None = None,
    cwd: Path | None = None,
) -> dict[str, list]:
    """The RUN and SERVES entries of one setting's file: what its commands run, held as files
    are (but for a file held as a setting itself, in `held_already`). One it names by a path
    that is not there is kept as absent; a word naming what runs that is found nowhere else
    (a module, a program) is said in `code`, never waited for."""
    name = entry.split("\0")[0]
    if value[0] not in {"f", "l"} or RUN in entry or SERVES in entry:
        return {}
    if not name.lower().endswith(PARSED):
        return {}
    path = folder / name
    real = path if value[0] == "f" else Path(os.path.realpath(path))
    try:
        data = budget[2].get(real) if len(budget) > 2 else None
        text = (data if data is not None else read_regular(real)).decode(errors="replace")
    except FileNotFoundError:
        return {}  # (A link to nothing: nothing runs.)
    except OSError as exc:
        raise TooLargeError(f"{name} could not be read") from exc
    held: dict[str, list] = {}
    extension = nfc(entry).casefold().startswith(".github/extensions/")
    place = real.parent if extension else folder
    setting = f"{folder_key(top, folder)}/{entry}".removeprefix("./")

    def say(said: str):
        note = f"{setting} runs `{shortened(said)}`"
        if code is not None and note not in code:
            code.append(note)

    for ref in references(real, text, say):
        target = located(top, place, ref, cwd or top)
        if target is None:
            if ref.kind in {"maybe", "program"}:
                continue
            if (missing := expected(top, place, ref, cwd or top)) is None:
                if ref.kind in {"script", "module"} and not elsewhere(top, ref.word, cwd or top):
                    say(ref.said or ref.word)
                continue
            key = run_key(name, ref.serves, nfc(missing.relative_to(top).as_posix()))
            held.setdefault(key, list(ABSENT))
            continue
        if ref.kind == "maybe":
            say(ref.said or ref.word)  # A program's file it doesn't follow: said.
            continue
        if target == Path(top) or Path(top).is_relative_to(target):
            continue
        if target.is_relative_to(folder) and nfc(target.relative_to(folder).as_posix()) in (
            held_already or {}
        ):
            continue  # A setting itself (a hook's script in `.github/hooks/`).
        shown = (
            nfc(target.relative_to(top).as_posix()) if target.is_relative_to(top) else str(target)
        )
        key = run_key(name, ref.serves, shown)
        if key in held and held[key] != ABSENT:
            continue
        held.pop(key, None)
        if target.is_dir() and not target.is_symlink():
            nested: dict[str, list] = {}
            for child in sorted(item.name for item in target.iterdir()):
                record(top, target / child, nfc(child), nested, budget)
            refused = next((v for kind, v in nested.values() if kind == REFUSED), "")
            held[key] = (
                [REFUSED, refused]
                if refused
                else ["d", hashlib.sha256(json.dumps(nested, sort_keys=True).encode()).hexdigest()]
            )
        elif target.is_symlink() and not (real := Path(os.path.realpath(target))).is_relative_to(
            os.path.realpath(top)
        ):
            # A program out of the folder (a virtualenv's python): as one found on PATH, what
            # it leads to is noted, never what is there; held only where workers can write.
            held[key] = [PROGRAM, str(real), "w" if workers_write(real, budget) else ""]
        else:
            record(top, target, key, held, budget)
    return held


def elsewhere(top: Path, word: str, cwd: Path) -> bool:
    """Whether a word names a path out of the project (a system program, one in home)."""
    token = expanded(word.split("=", 1)[-1] if word.startswith("-") else word, top, cwd)
    path = Path(token)
    return token.startswith("~") or (path.is_absolute() and not path.is_relative_to(top))


def run_key(setting: str, serves: str, path: str) -> str:
    """A RUN entry's key (a hook's file: held), or a SERVES one's (a server's: "mcp/name")."""
    return f"{setting}{SERVES}{serves}\0{path}" if serves else f"{setting}{RUN}{path}"


def ran_of(entry: str) -> tuple[str, str, str]:
    """(setting, file it runs, server) of a RUN or SERVES entry; ("entry", "", "") otherwise."""
    if SERVES in entry:
        setting, _, rest = entry.partition(SERVES)
        server, _, path = rest.partition("\0")
        return setting, path, server
    setting, _, path = entry.partition(RUN)
    return setting, path, ""


def shortened(said: str) -> str:
    said = " ".join(said.split())
    return said if len(said) <= 60 else said[:59] + "…"


class Ref(NamedTuple):
    """A word of a setting's command that names what runs: `kind` "file" (the command, a
    script by its extension, a file a variable runs), "script" (an interpreter's: a folder is
    its entry point, `node server` is server.js), "module" (`python -m x`, `import x`),
    "folder" (a plugin marketplace's), "maybe" (a path an unknown program is given: said when
    it is there, as code the parser doesn't follow); `base` where it is looked up from (a
    hook's `cwd`, a `cd` before it); `said`: the command, for the owner; `serves`: the server
    it is part of ("mcp/name", "lsp/name", "mcp/name@agent"), "" for a hook's."""

    word: str
    kind: str
    base: str = ""
    said: str = ""
    runner: str = ""
    serves: str = ""


def located(top: Path, place: Path, ref: Ref, cwd: Path) -> Path | None:
    """The file or folder a reference names, if there: from its base (a hook's cwd, a `cd`),
    the setting's folder or the top; an absolute one only inside the top; a module as Python
    finds it there; an interpreter's folder, by its entry point."""
    for candidate in candidates(top, place, ref, cwd):
        if not os.path.lexists(candidate):
            continue
        found = Path(os.path.normpath(candidate))
        if found.is_dir() and not found.is_symlink() and ref.kind != "folder":
            entry = entry_point(found, ref.runner)
            if entry is None:
                continue  # A folder an argument names (a root, data): not what runs.
            found = entry
        return found
    return None


def expected(top: Path, place: Path, ref: Ref, cwd: Path) -> Path | None:
    """Where a file a setting names by a path would be, inside the top, when it is nowhere
    (a build's output, not built yet): kept as absent. None for a module or a program found
    by name (on PATH)."""
    if ref.kind not in {"file", "script"}:
        return None
    token = expanded(ref.word.split("=", 1)[-1] if ref.word.startswith("-") else ref.word, top, cwd)
    if ref.kind == "file" and "/" not in token:
        return None  # A program, found on PATH.
    found = next(iter(candidates(top, place, ref, cwd)), None)
    if found is None:
        return None
    found = Path(os.path.normpath(found))
    return found if found.is_relative_to(top) and found != top else None


def candidates(top: Path, place: Path, ref: Ref, cwd: Path) -> list[Path]:
    token = ref.word
    if token.startswith("-"):
        token = token.split("=", 1)[-1]
    token = expanded(token, top, cwd)
    if not token or token in {".", ".."} or "://" in token or token.startswith(("~", "-")):
        return []
    if "$" in token or any(char in token for char in "*?["):
        return []
    base = expanded(ref.base, top, cwd)
    if base and Path(base).is_absolute():
        bases = [Path(base)] if Path(base).is_relative_to(top) else []
    else:
        bases = [place / base, top / base] if base else [place, top]
    if ref.kind == "module":
        stem = token.replace(".", "/")
        names = [f"{stem}.py", f"{stem}/__init__.py", f"{stem}/__main__.py"]
    else:
        names = [token]
        if ref.kind == "script" and not Path(token).suffix and ref.runner in NODE_LIKE:
            names += [f"{token}{ext}" for ext in (".js", ".mjs", ".cjs", ".ts")]
    if Path(token).is_absolute():
        return [Path(token)] if Path(token).is_relative_to(top) else []
    return [base / name for base in bases for name in names]


def expanded(word: str, top: Path, cwd: Path) -> str:
    """A word with the project's and the session's folders put in (`$CLAUDE_PROJECT_DIR`,
    `${CLAUDE_PROJECT_DIR:-.}`, `${COPILOT_SESSION_CWD}`, `$PWD`)."""
    word = DEFAULTED.sub(
        lambda found: f"${{{found[1]}}}" if found[1] in KNOWN_VARIABLES else found[0], word
    )
    return SESSION_DIR.sub(lambda _: str(cwd), PROJECT_DIR.sub(lambda _: str(top), word))


def entry_point(folder: Path, runner: str) -> Path | None:
    """What an interpreter runs for a folder: Python's `__main__.py`; Node's package `main`,
    else `index.js`."""
    if runner.startswith("python"):
        return folder / "__main__.py" if (folder / "__main__.py").is_file() else None
    if runner in NODE_LIKE:
        with suppress(OSError, ValueError, AttributeError):
            package = read_regular(folder / "package.json", 1024 * 1024, follow=True)
            main = json.loads(package).get("main")
            if isinstance(main, str) and (folder / main).is_file():
                return Path(os.path.normpath(folder / main))
        for name in ("index.js", "index.mjs", "index.cjs"):
            if (folder / name).is_file():
                return folder / name
    return None


def references(path: Path, text: str, say: Callable[[str], None]) -> Iterator[Ref]:
    """What a setting's commands run (held_words), from its JSON, TOML, a custom agent's front
    matter (block or flow style), an extension's relative imports; commands that run project
    code by a name (`npm run`), or that the parser does not follow, go to `say`."""
    suffix = path.suffix.lower()
    if suffix in {".mjs", ".cjs", ".js"}:
        for found in IMPORTS.finditer(text):
            yield Ref(found[1], "file")
        return
    agent = ""
    if suffix == ".md":
        data: Any = front_matter(text)
        if data is None:
            # Read loosely (every command it names held as a hook's), and said.
            say("its front matter, which Chief of Staff can't read as YAML")
            data = loose_front_matter(text)
        else:
            agent = agent_name(path.name, data)
    else:
        try:
            data = tomllib.loads(text) if suffix == ".toml" else json.loads(text)
        except (ValueError, tomllib.TOMLDecodeError):
            return
    for command in walked(data, "", agent):
        if command.folders:
            yield from (Ref(word, "folder", serves=command.serves) for word in command.arguments)
            continue
        yield from (ref._replace(serves=command.serves) for ref in command_refs(command, say))


def agent_name(file: str, data: Any) -> str:
    """The name a custom agent is called by (the task tool's agent_type, as Copilot matches it:
    exactly): its front matter's `name`, else its file's name without `.agent.md` (`.md`)."""
    named = data.get("name") if isinstance(data, dict) else None
    if named not in (None, ""):
        return str(named)
    for suffix in (".agent.md", ".md"):
        if file.lower().endswith(suffix):
            return file[: -len(suffix)]
    return file


def command_refs(command: Command, say: Callable[[str], None]) -> Iterator[Ref]:
    """What one setting's command runs: the files its environment runs (`BASH_ENV`,
    `NODE_OPTIONS=--require x`, `LD_PRELOAD`), a program its PATH finds in the project, and
    each simple command of its line and arguments, from its `cwd`."""
    environment = command.environment
    for name, value in environment.items():
        if name in RUN_VARIABLES:
            yield Ref(value, "file", command.cwd)
        elif name == "NODE_OPTIONS":
            words = split(value) or []
            for index, word in enumerate(words):
                if word in NODE_CODE and index + 1 < len(words):
                    yield Ref(words[index + 1], "file", command.cwd)
                elif "=" in word and word.split("=", 1)[0] in NODE_CODE:
                    yield Ref(word.split("=", 1)[1], "file", command.cwd)
        elif name in {"LD_PRELOAD", "DYLD_INSERT_LIBRARIES"}:
            yield from (Ref(part, "file", command.cwd) for part in value.split(":") if part)
        elif name in {"PATH", "PYTHONPATH", "NODE_PATH", "RUBYLIB", "PERL5LIB"} and any(
            not part.startswith("/") or "$" in part for part in value.split(":") if part
        ):
            # A folder of the project put where programs or modules are looked up first: said;
            # a program it finds there (PATH=./bin, the command `tool`), held.
            say(f"{command.line or ' '.join(command.arguments)} (with {name}={value})")
            program = command.line.strip()
            if name == "PATH" and program and "/" not in program and " " not in program:
                for part in value.split(":"):
                    if part and not part.startswith("/") and "$" not in part:
                        yield Ref(f"{part.rstrip('/')}/{program}", "program", command.cwd)
    line = command.line
    if command.program:
        # A server's program and its arguments (not a shell line): as the shell would be given
        # them, quoted.
        line = " ".join(map(shlex.quote, [line, *command.arguments]))
    for words, base in simple_commands(line, command.cwd, say):
        if said := project_code(words):
            say(said)
        yield from (ref._replace(base=base) for ref in held_words(words, say))


INTERPRETERS = {"python", "python2", "python3", "node", "bash", "sh", "zsh", "dash", "ksh",
                "fish", "ruby", "perl", "php", "deno", "bun", "pwsh", "powershell", "tsx",
                "ts-node", "lua", "rscript", "julia", "tclsh", "osascript"}  # fmt: skip
NODE_LIKE = {"node", "deno", "bun", "tsx", "ts-node"}
# An interpreter's options whose value is not its script (python -W x, node --env-file .env).
VALUED = {"-W", "-X", "-Q", "-o", "-O", "-I", "--env-file", "--inspect-port", "--title",
          "--config", "-C", "--cwd"}  # fmt: skip
# Node's options whose value is code it loads first (node -r ./pre.js server.js).
NODE_CODE = {"-r", "--require", "--import", "--loader", "--experimental-loader"}
# An interpreter's options whose value is code in the line itself (node -e, ruby -e, php -r).
INLINE = {"-e", "-p", "--eval", "--print", "-Command", "-EncodedCommand", "-E"}
# Programs that read a program from a file an option names (awk -f x.awk).
FILE_OPTIONS = {"awk": {"-f"}, "gawk": {"-f"}, "mawk": {"-f"}, "nawk": {"-f"}, "sed": {"-f"},
                "jq": {"-f", "--from-file"}}  # fmt: skip
# Words that run what follows them (`exec timeout 5 ./x.sh`): left out, with their options
# and as many arguments as given here.
WRAPPERS = {"exec": 0, "command": 0, "builtin": 0, "nohup": 0, "time": 0, "nice": 0,
            "timeout": 1, "stdbuf": 0, "caffeinate": 0, "chronic": 0}  # fmt: skip
# Keywords before a command (`then ./x.sh`): left out.
LEADING = {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "(", "time"}
# What the parser does not follow (a loop over files, code run later): said.
UNFOLLOWED = {"for", "case", "select", "function", "eval", "trap", "alias", "xargs", "coproc",
              "parallel", "watch", "find"}  # fmt: skip
# Programs whose arguments are data, never code (nothing of theirs is held or said).
DATA = {"true", "false", ":", "exit", "return", "echo", "printf", "test", "[", "[[", "set",
        "shift", "read", "wait", "sleep", "unset", "export", "local", "declare", "readonly",
        "fi", "done", "esac", "}", "]]", "]", ")", "cat", "ls", "mkdir", "rm", "rmdir", "touch",
        "cp", "mv", "date", "pwd", "git", "gh", "grep", "egrep", "rg", "wc", "head", "tail",
        "sort", "uniq", "cut", "tr", "diff", "cmp", "basename", "dirname", "realpath",
        "readlink", "stat", "chmod", "chown", "ln", "which", "type", "file", "du", "df", "tar",
        "zip", "unzip", "gzip", "open", "xdg-open", "curl", "wget", "notify-send",
        "terminal-notifier", "say", "afplay", "logger", "yq", "sha256sum", "shasum", "md5",
        "md5sum", "base64", "column", "tee", "jq"}  # fmt: skip
# Variables whose value is a file the shell or interpreter runs first.
RUN_VARIABLES = {"BASH_ENV", "ENV", "PYTHONSTARTUP", "ZDOTDIR"}
IMPORTED = re.compile(r"(?:^|[;\n])\s*(?:import\s+([\w.]+)|from\s+([\w.]+)\s+import)")
# A script a line of inline code names (ruby -e "load 'tools/s.rb'").
NAMED_SCRIPT = re.compile(r"""["']([\w./-]+\.(?:py|rb|js|mjs|cjs|ts|pl|php|lua|sh))["']""")
OPERATOR_CHARS = "();<>|&\n"
# A word that names files by a pattern (`scripts/*.sh`, `*.py`), not data with a `*` in it.
GLOB = re.compile(r"[*?](?=[^/]*$)(?:.*/|.*\.\w+$)|/.*[*?]|^\.?[*?]")


def tokens(line: str) -> list[str] | None:
    """A shell line's words and operators (`;`, `&&`, `|`, `>`, `(`, a newline), as a POSIX
    shell reads them; None when it can't be read (an unclosed quote)."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=OPERATOR_CHARS)
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    try:
        return list(lexer)
    except ValueError:
        return None


def pieces(words: list[str]) -> tuple[list[tuple[list[str], bool]], bool, list[str]]:
    """A line's simple commands (each with whether a pipe feeds it), redirections and their
    targets left out; whether it holds what the parser does not follow (a here-document, a
    command substitution whose output is more than data: `echo "$(date)"` is data, `sh -c
    "$(cat x)"` is code); the lines of its command substitutions, read as commands too."""
    found: list[tuple[list[str], bool]] = []
    inner: list[str] = []
    unfollowed = False
    current: list[str] = []
    piped = skip = False
    for word in [*words, ";"]:
        if skip:
            skip = False
            continue
        if word and all(char in OPERATOR_CHARS for char in word):
            op = word.replace("\n", "")
            if "<" in op or ">" in op:
                if current and current[-1].isdigit():
                    current.pop()  # A file descriptor (2>).
                unfollowed = unfollowed or op.startswith("<<")  # A here-document.
                skip = True
                continue
            if current and current[-1] == "$" and "(" in op:
                current.pop()  # $(…) unquoted: its commands are read as commands of the line.
                unfollowed = unfollowed or not current or Path(current[0]).name not in DATA
            if current:
                found.append((current, piped))
            piped = "|" in op and "||" not in op
            current = []
            continue
        if "$(" in word or "`" in word:
            unfollowed = unfollowed or not current or Path(current[0]).name not in DATA
            inner += [a or b for a, b in re.findall(r"\$\(([^()]*)\)|`([^`]*)`", word)]
        current.append(word)
    return found, unfollowed, inner


def simple_commands(
    line: str, base: str, say: Callable[[str], None], depth: int = 0
) -> Iterator[tuple[list[str], str]]:
    """The simple commands of a shell line, each with the folder it runs in (a hook's `cwd`,
    then `cd`, `pushd`, `popd`): split at its operators, subshells opened, redirections left
    out, a shell's `-c` line and command substitutions read too. What the parser does not
    follow is said (`say`, the whole line), never dropped; its commands are still read."""
    words = tokens(line)
    if words is None or depth > 4:
        say(line)
        return
    commands, unfollowed, inner = pieces(words)
    if "`" in line:
        unfollowed = True  # Backquotes: what they give, the parser does not follow.
        inner += [found for found in re.findall(r"`([^`]*)`", line) if found not in inner]
    folders = [base]
    for text in inner:
        yield from simple_commands(text, folders[-1], say, depth + 1)
    for found, piped in commands:
        words = unwrapped(found)
        if not words:
            continue
        name = Path(words[0]).name
        if name in {"cd", "pushd", "popd"}:
            unfollowed = not moved(words, folders) or unfollowed
            continue
        if name in SHELLS and (inline := shell_line(words)) is not None:
            if inline:
                yield from simple_commands(inline, folders[-1], say, depth + 1)
            else:
                unfollowed = True
            continue
        if not followed(words, piped):
            unfollowed = True
        yield words, folders[-1]
    if unfollowed:
        say(line)


def unwrapped(words: list[str]) -> list[str]:
    """A command without the words before it that only run it (`then`, `exec`, `timeout 5`)."""
    while words:
        if words[0] in LEADING:
            words = words[1:]
        elif words[0] in WRAPPERS:
            count = WRAPPERS[words[0]]
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[2:] if words[0] in {"-n", "-s", "-k", "--signal"} else words[1:]
            words = words[count:]
        else:
            return words
    return words


def moved(words: list[str], folders: list[str]) -> bool:
    """A `cd`, `pushd` or `popd`, followed (folders: the stack, the current last); False when
    where it goes can't be said (a variable it doesn't know)."""
    name = Path(words[0]).name
    if name == "popd":
        if len(folders) > 1:
            folders.pop()
        return True
    target = next((word for word in words[1:] if not word.startswith("-")), "")
    if not target or "$" in known_removed(target) or target.startswith("~"):
        return False  # Home, the folder before, one a variable names: not followed.
    here = folders[-1]
    where = (
        target
        if Path(target).is_absolute() or target.startswith("$") or not here
        else (str(Path(here) / target))
    )
    if name == "pushd":
        folders.append(where)
    else:
        folders[-1] = where
    return True


def known_removed(word: str) -> str:
    """A word without the variables the parser knows (the project's and the session's folder)."""
    return PROJECT_DIR.sub("", SESSION_DIR.sub("", DEFAULTED.sub(
        lambda found: "" if found[1] in KNOWN_VARIABLES else found[0], word
    )))  # fmt: skip


def shell_line(words: list[str]) -> str | None:
    """A shell's own line (`bash -lc 'x'`, `sh -ec`, `bash --noprofile -c`): that line, ""
    when it gives none; None when the shell runs a script (or reads its input) instead."""
    options = []
    rest = list(words[1:])
    while rest and rest[0].startswith("-") and rest[0] != "-":
        options.append(rest.pop(0))
    if any(
        (option.startswith("-") and not option.startswith("--") and "c" in option)
        or option in {"-Command", "-command", "-EncodedCommand"}
        for option in options
    ):
        return rest[0] if rest else ""
    return None


def followed(words: list[str], piped: bool) -> bool:
    """Whether the parser can say what a command runs: not a loop or code run later, not a
    command named by a variable it doesn't know, not a glob over files, not an interpreter fed
    its program through a pipe."""
    name = Path(words[0]).name.lower()
    if name in UNFOLLOWED or "$" in known_removed(words[0]):
        return False
    if any(GLOB.search(word) for word in words[1:] if not word.startswith("-")):
        return name in DATA  # A glob: which files, the parser can't say (but as data).
    if piped and (name in SHELLS or name in INTERPRETERS or name.startswith("python")):
        operands = [word for word in words[1:] if not word.startswith("-")]
        return bool(operands) and operands[0] != "-"
    return True


def held_words(words: list[str], say: Callable[[str], None] | None = None) -> list[Ref]:
    """What one simple command runs: itself when it is a path, an interpreter's script (its
    first word that is no option: `python tools/x.py`, `node tools/server`, `uv run --script
    x.py`), code an option loads (`node -r ./pre.js`, `awk -f x.awk`), the modules of `python
    -m x` or `python -c "import x"`, a file a variable runs (`BASH_ENV=x.sh`), a file it
    sources, any word naming a script by its extension; a path an unknown program is given
    ("maybe": said if it is there). Never where its output goes (`tee`), nor an option's value
    that is no code (`--db ./dev.db`)."""
    say = say or (lambda said: None)
    refs: list[Ref] = []
    while words and "=" in words[0] and not words[0].startswith(("-", "=")):
        name, _, value = words[0].partition("=")
        if name in RUN_VARIABLES and value:
            refs.append(Ref(value, "file"))
        words = words[1:]  # Variables set for it.
    if not words:
        return refs
    command = Path(words[0]).name.removesuffix(".exe").lower()
    if command == "env":
        rest = words[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in {"-u", "-C", "-S", "--unset", "--chdir"} else rest[1:]
        return refs + held_words(rest, say)
    if command in {"source", "."}:
        return refs + ([Ref(words[1], "script", runner="sh")] if len(words) > 1 else [])
    if "/" in words[0] or words[0].startswith("."):
        refs.append(Ref(words[0], "file"))
    said = shortened(" ".join(words))
    refs += interpreted(command, words[1:], said, say)
    if command in FILE_OPTIONS:
        wanted = FILE_OPTIONS[command]
        refs += [
            Ref(words[i + 1], "file") for i, word in enumerate(words[1:-1], 1) if word in wanted
        ]
    named = {ref.word for ref in refs}
    for word in words[1:]:
        value = word.split("=", 1)[-1] if word.startswith("-") else word
        if word in named or value in named or command in DATA:
            continue  # (What `cat`, `git`, `tee`… are given is data.)
        if Path(value).suffix.lower() in SCRIPTS:
            refs.append(Ref(value, "file"))
            named.add(value)
        elif (
            command not in DATA
            and command not in INTERPRETERS
            and not command.startswith("python")
            and command not in RUNNERS
            and ("/" in value or value.startswith("."))
            and not word.startswith("-")
        ):
            refs.append(Ref(value, "maybe", said=said))  # An unknown program's: said if there.
    return refs


def interpreted(runner: str, rest: list[str], said: str, say: Callable[[str], None]) -> list[Ref]:
    """What an interpreter runs: its script (`node --import x server.js`: server.js, and x),
    the module of `python -m x`, those `python -c "…"` imports; inline code (`ruby -e`), said,
    with the scripts it names."""
    if runner == "uv":
        if rest[:1] != ["run"]:
            return []
        rest = rest[1:]
        found = first_operand(rest)
        if found and (Path(found).name in INTERPRETERS or Path(found).name.startswith("python")):
            index = rest.index(found)
            return interpreted(Path(found).name, rest[index + 1 :], said, say)
        script = "--script" in rest or Path(found).suffix.lower() in SCRIPTS
        return [Ref(found, "script", said=said, runner="python")] if found and script else []
    if runner == "uvx":
        if any(
            word in {"--from", "--with", "--with-editable"} and value.startswith((".", "/"))
            for word, value in pairwise(rest)
        ):
            say(said)  # The project itself, installed and run.
        return []
    if not (runner in INTERPRETERS or runner.startswith("python")):
        return []
    if runner in {"deno", "bun"} and rest[:1] == ["run"]:
        rest = rest[1:]
    refs: list[Ref] = []
    if runner in NODE_LIKE:
        refs += [Ref(rest[i + 1], "file") for i, word in enumerate(rest[:-1]) if word in NODE_CODE]
        refs += [
            Ref(word.split("=", 1)[1], "file") for word in rest
            if "=" in word and word.split("=", 1)[0] in NODE_CODE
        ]  # fmt: skip
    if runner.startswith("python"):
        if "-m" in rest[:-1]:
            return [Ref(rest[rest.index("-m") + 1], "module", said=said, runner=runner)]
        if "-c" in rest[:-1]:
            line = rest[rest.index("-c") + 1]
            refs += [Ref(found, "file") for found in NAMED_SCRIPT.findall(line)]
            return refs + [
                Ref(found[1] or found[2], "module", said=said, runner=runner)
                for found in IMPORTED.finditer(line)
            ]
    inline = next((i for i, word in enumerate(rest[:-1]) if word in INLINE), None)
    if inline is not None or (runner == "php" and "-r" in rest[:-1]):
        line = rest[(inline if inline is not None else rest.index("-r")) + 1]
        say(said)  # Code in the line itself: what it loads, the parser doesn't follow.
        return refs + [Ref(found, "file") for found in NAMED_SCRIPT.findall(line)]
    found = first_operand(rest, NODE_CODE if runner in NODE_LIKE else set())
    return refs + ([Ref(found, "script", said=said, runner=runner)] if found else [])


def first_operand(words: list[str], valued: set[str] | frozenset = frozenset()) -> str:
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word.startswith("-"):
            skip = word in VALUED or word in valued
        else:
            return word
    return ""


class Command(NamedTuple):
    line: str
    arguments: list[str]
    folders: bool = False
    cwd: str = ""
    environment: dict = {}  # noqa: RUF012  (Never changed: read only.)
    serves: str = ""
    program: bool = False


def walked(data: Any, serves: str = "", agent: str = "") -> Iterator[Command]:
    """The commands in a parsed setting: each command (a hook's `command`/`bash`, with its
    `cwd` and `env`; a server's `command` with its `args`), each `path` (a plugin
    marketplace's own folder); `serves`, the server one is part of (under `mcpServers`,
    `lspServers`, a custom agent's `mcp-servers`: "mcp/name", "lsp/name", "mcp/name@agent")."""
    if isinstance(data, dict):
        arguments = [str(a) for a in data.get("args") or [] if isinstance(a, (str, int, float))]
        cwd = str(data["cwd"]) if isinstance(data.get("cwd"), str) else ""
        environment = {
            str(k): str(v) for k, v in (data.get("env") or {}).items()
            if isinstance(data.get("env"), dict) and isinstance(v, (str, int, float))
        }  # fmt: skip
        program = isinstance(data.get("args"), list)
        for key in COMMAND_KEYS:
            if isinstance(data.get(key), str):
                yield Command(data[key], arguments, cwd=cwd, environment=environment,
                              serves=serves, program=program)  # fmt: skip
        if isinstance(data.get("path"), str):
            yield Command("", [data["path"]], folders=True, serves=serves)
        notify = data.get("notify")
        if isinstance(notify, list) and notify and all(isinstance(w, str) for w in notify):
            yield Command(notify[0], notify[1:], serves=serves, program=True)  # Codex's.
        for key, value in data.items():
            kind = SERVER_KEYS.get(str(key).casefold())
            if kind and isinstance(value, dict) and not serves:
                for name, server in value.items():
                    yield from walked(server, f"{kind}/{name}" + (f"@{agent}" if agent else ""))
            elif key not in COMMAND_KEYS:
                yield from walked(value, serves, agent)
    elif isinstance(data, list):
        for value in data:
            yield from walked(value, serves, agent)


def front_matter(text: str) -> Any:
    """A custom agent's front matter, as values (its mcp-servers, hooks, name): YAML block
    style as far as front matter goes (mappings, lists, scalars), flow style (`{s: {command:
    node, args: [x]}}`) read as such. {} with none; None when it can't be read."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    body: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.strip() and not line.lstrip().startswith("#"):
            body.append(line.rstrip())
    else:
        return None
    try:
        value, rest = block(body, 0, 0)
    except (ValueError, IndexError):
        return None
    return value if rest == len(body) else None


def block(lines: list[str], start: int, indent: int) -> tuple[Any, int]:
    """The YAML block at `start` (lines indented `indent`): a mapping, a list, or nothing."""
    if start >= len(lines) or indentation(lines[start]) < indent:
        return None, start
    indent = indentation(lines[start])
    if lines[start].strip().startswith("- ") or lines[start].strip() == "-":
        items: list = []
        index = start
        while (
            index < len(lines)
            and indentation(lines[index]) == indent
            and (lines[index].strip().startswith("-"))
        ):
            item = lines[index].strip()[1:].strip()
            if ":" in item and not item.startswith(("'", '"', "{", "[")):
                # A mapping in the list: its first key on the dash's line, the rest under it.
                inner = [" " * (indent + 2) + item, *lines[index + 1 :]]
                value, used = block(inner, 0, indent + 2)
                items.append(value)
                index += used
            elif item:
                items.append(scalar(item))
                index += 1
            else:
                value, index = block(lines, index + 1, indent + 1)
                items.append(value)
        return items, index
    found: dict = {}
    index = start
    while index < len(lines) and indentation(lines[index]) == indent:
        key, colon, value = lines[index].strip().partition(":")
        if not colon:
            raise ValueError(lines[index])
        key = key.strip().strip("\"'")
        value = value.strip()
        index += 1
        if value and value[0] in "|>" and set(value[1:]) <= set("+-0123456789"):
            text = []  # A block scalar (`description: |`): its lines, under it.
            while index < len(lines) and indentation(lines[index]) > indent:
                text.append(lines[index].strip())
                index += 1
            found[key] = "\n".join(text)
        elif value:
            found[key] = scalar(value)
            while index < len(lines) and indentation(lines[index]) > indent:
                index += 1  # A plain scalar's next lines (a long description).
        elif (
            index < len(lines)
            and indentation(lines[index]) == indent
            and (lines[index].strip().startswith("-"))
        ):
            found[key], index = block(lines, index, indent)  # A list at the key's indent.
        else:
            found[key], index = block(lines, index, indent + 1)
    return found, index


def loose_front_matter(text: str) -> list:
    """Of a front matter the YAML reader can't read: each `command:` it says, with the `args`
    under it, line by line."""
    found: list = []
    listing = False
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            break
        key, colon, value = line.strip().partition(":")
        value = value.strip()
        if colon and key.strip("- ") in COMMAND_KEYS and value:
            found.append({"command": value.strip("\"'"), "args": []})
            listing = False
        elif colon and key.strip("- ") == "args" and found:
            listing = not value
            with suppress(ValueError):
                if value.startswith("["):
                    found[-1]["args"].extend(map(str, flow(value)))
        elif listing and line.strip().startswith("- "):
            found[-1]["args"].append(line.strip()[2:].strip().strip("\"'"))
        else:
            listing = False
    return found


def indentation(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def scalar(value: str) -> Any:
    """A YAML value on one line: a flow collection, a quoted or plain scalar."""
    if value.startswith(("{", "[")):
        return flow(value)
    if value[:1] in {"'", '"'} and value.endswith(value[0]) and len(value) > 1:
        return value[1:-1]
    return value.split(" #", 1)[0].strip()


def flow(text: str) -> Any:
    """A YAML flow collection (`{a: [b, "c d"]}`), as Python values; ValueError when it isn't
    one this reads."""
    value, rest = flow_value(text.strip())
    if rest.strip():
        raise ValueError(rest)
    return value


def flow_value(text: str) -> tuple[Any, str]:
    text = text.lstrip()
    if text.startswith("{"):
        found: dict = {}
        text = text[1:].lstrip()
        while not text.startswith("}"):
            key, text = flow_value(text)
            text = text.lstrip()
            if not text.startswith(":"):
                raise ValueError(text)
            found[str(key)], text = flow_value(text[1:])
            text = text.lstrip()
            if text.startswith(","):
                text = text[1:].lstrip()
            elif not text.startswith("}"):
                raise ValueError(text)
        return found, text[1:]
    if text.startswith("["):
        items: list = []
        text = text[1:].lstrip()
        while not text.startswith("]"):
            item, text = flow_value(text)
            items.append(item)
            text = text.lstrip()
            if text.startswith(","):
                text = text[1:].lstrip()
            elif not text.startswith("]"):
                raise ValueError(text)
        return items, text[1:]
    if text[:1] in {"'", '"'}:
        end = text.find(text[0], 1)
        if end < 0:
            raise ValueError(text)
        return text[1:end], text[end + 1 :]
    match = re.match(r"[^,:{}\[\]]*(?::(?=\S)[^,:{}\[\]]*)*", text)
    word = match[0] if match else ""
    if not word.strip():
        raise ValueError(text)
    return word.strip(), text[len(word) :]


def split(line: str) -> list[str] | None:
    try:
        return shlex.split(line)
    except ValueError:
        return None


def project_code(words: list[str]) -> str:
    """The command, shortened, when it runs project code no word of it names as a file."""
    while words and "=" in words[0] and not words[0].startswith("-"):
        words = words[1:]  # Variables set for it.
    if not words:
        return ""
    name = Path(words[0]).name.removesuffix(".exe").removesuffix(".cmd")
    rest = [word for word in words[1:] if not word.startswith("-")]
    if name.startswith("python") or name in {"node", "deno", "bun"}:
        # (A Python module is looked for, as a file: said only when it isn't found, by runs.)
        if not (name == "deno" and rest[:1] == ["task"]):
            return ""
    elif name not in RUNNERS or (
        (wanted := RUNNERS[name]) is not None and not (rest and rest[0] in wanted)
    ):
        return ""
    return shortened(" ".join(words))


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
    setting, ran, _ = ran_of(entry)
    if ran:
        return f"{ran} (run by {prefix}{setting})"
    return prefix + setting.split("\0")[0]


def named(folders: dict[str, dict]) -> list[str]:
    """The settings a snapshot holds, by path from the top (not the files they run)."""
    return sorted(
        {shown_entry(folder, entry) for folder, entries in folders.items() for entry in entries
         if RUN not in entry and SERVES not in entry}
    )  # fmt: skip


def changes(
    now: dict[str, dict], then: dict[str, dict], servers: bool = False
) -> list[tuple[str, str]]:
    """(what, path) for each entry added, changed, removed, or never taken as unchanged
    ("refused": a link out of the folder, a special file, said with why); what a server runs
    only with `servers` (Codex's: a Copilot server's turns it off instead, servers_changed)."""
    found = []
    for folder in sorted(now.keys() | then.keys()):
        current, saved = now.get(folder, {}), then.get(folder, {})
        for entry in sorted(current.keys() | saved.keys()):
            if SERVES in entry and not servers:
                continue
            shown = shown_entry(folder, entry)
            value = current.get(entry) or saved[entry]
            if value[0] == PROGRAM and (entry not in current or not current[entry][2]):
                continue  # (Out of every worker's reach: as a program on PATH.)
            if entry in current and current[entry][0] == REFUSED:
                found.append(("refused", f"{shown} {current[entry][1]}"))
            elif entry not in saved:
                found.append(("added", shown))
            elif entry not in current:
                found.append(("removed", shown))
            elif current[entry] != saved[entry]:
                found.append(("changed", shown))
    return found


def servers_changed(now: dict[str, dict], then: dict[str, dict]) -> dict[str, list[str]]:
    """{server ("mcp/name", "lsp/name", "mcp/name@agent"): the files it runs that differ from
    `then`, shown}: a build's output made since, a server's source edited."""
    found: dict[str, list[str]] = {}
    for folder in sorted(now.keys() | then.keys()):
        current, saved = now.get(folder, {}), then.get(folder, {})
        for entry in sorted(current.keys() | saved.keys()):
            if SERVES not in entry or current.get(entry) == saved.get(entry):
                continue
            value = current.get(entry) or saved[entry]
            if value[0] == PROGRAM and (entry not in current or not current[entry][2]):
                continue
            server = ran_of(entry)[2]
            found.setdefault(server, []).append(shown_entry(folder, entry))
    return found


def taken_servers(snapshot: dict, now: dict[str, dict], servers: set[str]):
    """What these servers run, as `now` holds it, becomes the snapshot's (the owner said to
    use them as they are)."""
    for folder in now.keys() | snapshot["folders"].keys():
        kept = snapshot["folders"].get(folder, {})
        for entry in [e for e in kept if SERVES in e and ran_of(e)[2] in servers]:
            del kept[entry]
        kept |= {
            entry: value for entry, value in now.get(folder, {}).items()
            if SERVES in entry and ran_of(entry)[2] in servers
        }  # fmt: skip
        if kept:
            snapshot["folders"][folder] = kept


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
# there, not looked at by the owner yet]}, "carried": {folder: [setting names carried there
# from the owner's checkout as it doesn't hold them]}, "unchecked": {what of settings too many
# to check the owner said to use as they were (unchecked_key: their names, those folders):
# glimpse}, "code": digest of the project code lines said (code_line)}.


def unchecked_key(names: tuple[str, ...], folders: list[str]) -> str:
    return json.dumps([sorted(names), sorted(folders)])


def fresh() -> dict:
    return {"folders": {}, "known": {}, "first": {}, "carried": {}, "unchecked": {}, "code": ""}


def without_servers(snapshot: dict[str, dict]) -> dict[str, dict]:
    """Of a snapshot's folders, all but what servers run (their own digest: a build between a
    line and its click does not change what the owner is asked)."""
    return {
        folder: {entry: value for entry, value in entries.items() if SERVES not in entry}
        for folder, entries in snapshot.items()
    }


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
    if not isinstance(found.get("unchecked"), dict):
        found["unchecked"] = {}  # (Of an earlier build: names alone.)
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
        for marks in ("first", "carried"):
            marked = snapshot.setdefault(marks, {})
            if left := [n for n in marked.get(folder, []) if n not in names]:
                marked[folder] = left
            else:
                marked.pop(folder, None)


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


def first_seen(
    snapshot: dict,
    now: dict[str, dict],
    then: dict[str, dict],
    marks: str = "first",
    servers: bool = False,
) -> bool:
    """Whether what differs is only what is in settings seen for the first time there
    (`marks` "first"), or carried there from the owner's checkout as it doesn't hold them
    ("carried")."""
    found = False
    for folder in now.keys() | then.keys():
        current, saved = now.get(folder, {}), then.get(folder, {})
        first = snapshot.get(marks, {}).get(folder, [])
        for entry in current.keys() | saved.keys():
            if SERVES in entry and not servers:
                continue
            value = current.get(entry)
            if (value or saved[entry])[0] == PROGRAM and not (value and value[2]):
                continue
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
    knows them; vetted as they were at first (vet)."""
    snapshot = load(store, top)
    if snapshot is None or not paths:
        return
    budget: list = [MOST_ENTRIES, MOST_BYTES, {}]
    for path in paths:
        folder, setting = split_setting(path)
        if setting == path and not any(under(path, name) for name in AGENT_SETTINGS):
            continue  # Not a setting.
        if folder not in snapshot["known"]:
            continue
        entry = path if folder == "." else path[len(folder) + 1 :]
        place = top if folder == "." else top / folder
        held = snapshot["folders"].setdefault(folder, {})
        for key in [
            key for key in held
            if key == entry or key.startswith((entry + RUN, entry + SERVES))
        ]:  # fmt: skip
            del held[key]
        try:
            record(top, top / path, entry, held, budget)
            if entry in held:
                held |= runs(top, place, entry, held[entry], budget, None)
        except TooLargeError:
            continue
    save(store, top, snapshot)


def vet(store: Any, tree: Path, owner: Path, carried: list[str], untouched: bool):
    """Of the settings (and files they run) Chief of Staff carried into a worktree it made
    from the owner's checkout (their gitignored local settings), those the owner's checkout
    holds as its snapshot holds them, or as they are (`untouched`: no worker could have
    written that checkout) stay taken; any other is left out of the worktree's snapshot, so
    the worker waits for the owner ("added since workers had this checkout"): a worker in the
    owner's checkout could have planted it."""
    snapshot = load(store, tree)
    if snapshot is None or not carried:
        return
    theirs = load(store, owner)
    wanted = set(carried)
    budget: list = [MOST_ENTRIES, MOST_BYTES, {}]
    changed = False
    for folder, entries in snapshot["folders"].items():
        prefix = "" if folder == "." else f"{folder}/"
        for entry in list(entries):
            setting, ran, _ = ran_of(entry)
            path = ran or prefix + setting.split("\0")[0]
            if path not in wanted and not any(path.startswith(f"{c}/") for c in wanted):
                continue
            # No worker could have written the owner's checkout: what is there now is theirs
            # (their own tools change their local settings); else as its snapshot holds it.
            owned = (
                None if untouched else (theirs or {}).get("folders", {}).get(folder, {}).get(entry)
            )
            if untouched:
                held: dict = {}
                with suppress(TooLargeError):
                    record(
                        owner, owner / (ran or prefix + setting.split("\0")[0]), "x", held, budget
                    )
                owned = held.get("x")
            if owned == entries[entry]:
                continue
            del entries[entry]
            changed = True
            if SERVES in entry:
                continue  # A server's: that server is off for the worker until the owner says.
            name = next((n for n in AGENT_SETTINGS if under(setting, n)), setting)
            marked = snapshot.setdefault("carried", {})
            marked[folder] = sorted({*marked.get(folder, []), name})
    if changed:
        save(store, tree, snapshot)


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
