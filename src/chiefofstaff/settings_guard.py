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
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any, NamedTuple, cast

from .policy import AGENT_SETTINGS

MOST_ENTRIES = 1000
MOST_BYTES = 16 * 1024 * 1024
KEY = "settingssnap:"
# An entry for a file a setting runs: "<setting's entry>" + RUN + "<that file>".
RUN = "\0run:"
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
            named = dot_git.read_text(errors="replace").strip()
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
    every tool a worker runs; a change means the full check (scan) runs."""
    seen: list = []

    def note(path: Path, depth: int):
        try:
            info = os.lstat(path)
        except OSError:
            return
        seen.append((str(path), info.st_mode, info.st_ino, info.st_size, info.st_mtime_ns,
                     info.st_ctime_ns))  # fmt: skip
        if stat.S_ISDIR(info.st_mode) and depth < 4 and len(seen) < MOST_ENTRIES:
            for entry in listed(path):
                note(path / entry.name, depth + 1)

    for folder in chain(top, cwd):
        note(folder, 4)
        for name in {name.split("/")[0] for name in names}:
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
        found: list = [name, info.st_mode, info.st_size]
        if stat.S_ISREG(info.st_mode) and info.st_size <= 4 * 1024 * 1024 and budget[1] > 0:
            budget[1] -= info.st_size
            with suppress(OSError):
                found.append(hashlib.sha256(path.read_bytes()).hexdigest())
        elif stat.S_ISLNK(info.st_mode):
            found.append(os.path.realpath(path))
        seen.append(found)
        budget[0] -= 1
        if stat.S_ISDIR(info.st_mode) and depth < 8 and budget[0] > 0:
            with suppress(OSError):
                for entry in listed(path):
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
        data = path.read_bytes()
    except OSError as exc:
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
# a custom agent's front matter (its mcp-servers), an extension's relative imports.
COMMAND_KEYS = {"command", "bash", "powershell", "pwsh", "sh", "cmd"}
SHELLS = {"bash", "sh", "zsh", "dash", "pwsh", "powershell"}
SESSION_DIR = re.compile(r"\$\{COPILOT_SESSION_CWD\}|\$COPILOT_SESSION_CWD\b")
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
    cwd: Path | None = None,
) -> dict[str, list]:
    """The RUN entries of one setting's file: what its commands run, held as files are (but
    for a file held as a setting itself, in `held_already`). A word naming what runs that is
    found nowhere is said in `code` (project code no entry holds), never waited for."""
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
    setting = f"{folder_key(top, folder)}/{entry}".removeprefix("./")
    for ref in references(real, text, code, setting):
        target = located(top, place, ref, cwd or top)
        if target is None:
            note = f"{setting} runs `{ref.said}`"
            if code is not None and ref.kind in {"script", "module"} and note not in code:
                code.append(note)
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
        key = f"{name}{RUN}{shown}"
        if key in held:
            continue
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


class Ref(NamedTuple):
    """A word of a setting's command that names what runs: `kind` "file" (the command, a
    script by its extension, a file a variable runs), "script" (an interpreter's: a folder is
    its entry point, `node server` is server.js), "module" (`python -m x`, `import x`),
    "folder" (a plugin marketplace's); `base` where it is looked up from (a hook's `cwd`, a
    `cd` before it); `said`: the command, for the owner, when it is found nowhere."""

    word: str
    kind: str
    base: str = ""
    said: str = ""
    runner: str = ""


def located(top: Path, place: Path, ref: Ref, cwd: Path) -> Path | None:
    """The file or folder a reference names, if there: from its base (a hook's cwd, a `cd`),
    the setting's folder or the top; an absolute one only inside the top; a module as Python
    finds it there; an interpreter's folder, by its entry point."""
    token = ref.word
    if token.startswith("-"):
        token = token.split("=", 1)[-1]
    token = expanded(token, top, cwd)
    if not token or token in {".", ".."} or "://" in token or token.startswith(("~", "-")):
        return None
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
        candidates = [Path(token)] if Path(token).is_relative_to(top) else []
    else:
        candidates = [base / name for base in bases for name in names]
    for candidate in candidates:
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


def expanded(word: str, top: Path, cwd: Path) -> str:
    """A word with the project's and the session's folders put in (`$CLAUDE_PROJECT_DIR`,
    `${COPILOT_SESSION_CWD}`, `$PWD`)."""
    return SESSION_DIR.sub(lambda _: str(cwd), PROJECT_DIR.sub(lambda _: str(top), word))


def entry_point(folder: Path, runner: str) -> Path | None:
    """What an interpreter runs for a folder: Python's `__main__.py`; Node's package `main`,
    else `index.js`."""
    if runner.startswith("python"):
        return folder / "__main__.py" if (folder / "__main__.py").is_file() else None
    if runner in NODE_LIKE:
        with suppress(OSError, ValueError, AttributeError):
            main = json.loads((folder / "package.json").read_text()).get("main")
            if isinstance(main, str) and (folder / main).is_file():
                return Path(os.path.normpath(folder / main))
        for name in ("index.js", "index.mjs", "index.cjs"):
            if (folder / name).is_file():
                return folder / name
    return None


def references(path: Path, text: str, code: list | None, setting: str) -> Iterator[Ref]:
    """What a setting's commands run (held_words), from its JSON, TOML, a custom agent's front
    matter (block or flow style), an extension's relative imports; commands that run project
    code by a name (`npm run`) go to `code`."""
    suffix = path.suffix.lower()
    if suffix in {".mjs", ".cjs", ".js"}:
        for found in IMPORTS.finditer(text):
            yield Ref(found[1], "file")
        return
    if suffix == ".md":
        data: Any = front_matter(text)
    else:
        try:
            data = tomllib.loads(text) if suffix == ".toml" else json.loads(text)
        except (ValueError, tomllib.TOMLDecodeError):
            return
    for command in walked(data):
        if command.folders:
            yield from (Ref(word, "folder") for word in command.arguments)
            continue
        base = command.cwd
        for word in command.files:
            yield Ref(word, "file", base)
        for segment in segments(split(command.line) + command.arguments):
            if segment[:1] == ["cd"] and len(segment) == 2:
                base = str(Path(base) / segment[1]) if base else segment[1]
                continue
            note = f"{setting} runs `{said}`" if (said := project_code(segment)) else ""
            if code is not None and note and note not in code:
                code.append(note)
            yield from (ref._replace(base=base) for ref in held_words(segment))


INTERPRETERS = {"python", "python2", "python3", "node", "bash", "sh", "zsh", "dash", "ruby",
                "perl", "php", "deno", "bun", "pwsh", "powershell", "tsx", "ts-node",
                "lua"}  # fmt: skip
NODE_LIKE = {"node", "deno", "bun", "tsx", "ts-node"}
# An interpreter's options whose value is not its script (python -W x, node --env-file .env).
VALUED = {"-W", "-X", "-Q", "-o", "-O", "-I", "-r", "--require", "--import", "--loader",
          "--env-file", "--inspect-port", "--title", "--config", "-C", "--cwd"}  # fmt: skip
# Options after which no script follows (python -m module, sh -c line, node -e code).
NO_SCRIPT = {"-m", "-c", "-e", "-p", "--eval", "--print", "-Command", "-EncodedCommand"}
REDIRECT = re.compile(r"^(?:[0-9&]?>>?|<)")
# Variables whose value is a file the shell or interpreter runs first.
RUN_VARIABLES = {"BASH_ENV", "ENV", "PYTHONSTARTUP"}
IMPORTED = re.compile(r"(?:^|[;\n])\s*(?:import\s+([\w.]+)|from\s+([\w.]+)\s+import)")


def held_words(words: list[str]) -> list[Ref]:
    """What one command runs: itself when it is a path, an interpreter's script (its first
    word that is no option: `python tools/x.py`, `node tools/server`, `uv run --script x.py`),
    the modules of `python -m x` or `python -c "import x"`, a file a variable runs
    (`BASH_ENV=x.sh`), and any word naming a script by its extension. Never where its output
    goes (`>> logs/x.log`, `tee`), nor an option's value that is no script (`--db ./dev.db`)."""
    refs: list[Ref] = []
    while words and "=" in words[0] and not words[0].startswith("-"):
        name, _, value = words[0].partition("=")
        if name in RUN_VARIABLES and value:
            refs.append(Ref(value, "file"))
        words = words[1:]  # Variables set for it.
    if words[:1] == ["env"]:
        return refs + held_words(words[1:])
    kept: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif REDIRECT.match(word):
            skip = word in {">", ">>", "1>", "1>>", "2>", "2>>", "&>", "&>>", "<"}
        else:
            kept.append(word)
    if not kept or Path(kept[0]).name == "tee":
        return refs
    if "/" in kept[0] or kept[0].startswith("."):
        refs.append(Ref(kept[0], "file"))
    runner = Path(kept[0]).name.removesuffix(".exe").lower()
    said = " ".join(kept)
    said = said if len(said) <= 60 else said[:59] + "…"
    refs += interpreted(runner, kept[1:], said)
    named = {ref.word for ref in refs}
    refs += [
        Ref(word, "file") for word in kept[1:]
        if Path(word.split("=", 1)[-1]).suffix.lower() in SCRIPTS and word not in named
    ]  # fmt: skip
    return refs


def interpreted(runner: str, rest: list[str], said: str) -> list[Ref]:
    """What an interpreter runs: its script (`node --import x server.js`: server.js), the
    module of `python -m x`, those `python -c "…"` imports."""
    if runner == "uv":
        if rest[:1] != ["run"]:
            return []
        rest = rest[1:]
        found = first_operand(rest)
        script = "--script" in rest or Path(found).suffix.lower() in SCRIPTS
        return [Ref(found, "script", said=said, runner="python")] if found and script else []
    if not (runner in INTERPRETERS or runner.startswith("python")):
        return []
    if runner in {"deno", "bun"} and rest[:1] == ["run"]:
        rest = rest[1:]
    if runner.startswith("python"):
        if "-m" in rest[:-1]:
            return [Ref(rest[rest.index("-m") + 1], "module", said=said, runner=runner)]
        if "-c" in rest[:-1]:
            line = rest[rest.index("-c") + 1]
            return [
                Ref(found[1] or found[2], "module", said=said, runner=runner)
                for found in IMPORTED.finditer(line)
            ]
    if any(word in NO_SCRIPT for word in rest[:4]):
        return []
    found = first_operand(rest)
    return [Ref(found, "script", said=said, runner=runner)] if found else []


def first_operand(words: list[str]) -> str:
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word.startswith("-"):
            skip = word in VALUED
        else:
            return word
    return ""


class Command(NamedTuple):
    line: str
    arguments: list[str]
    folders: bool = False
    cwd: str = ""
    files: tuple[str, ...] = ()


def walked(data: Any) -> Iterator[Command]:
    """The commands in a parsed setting: each command (a hook's `command`/`bash`, with its
    `cwd` and the files its `env` runs; a server's `command` with its `args`), each `path` (a
    plugin marketplace's own folder)."""
    if isinstance(data, dict):
        arguments = [a for a in data.get("args") or [] if isinstance(a, str)]
        cwd = str(data["cwd"]) if isinstance(data.get("cwd"), str) else ""
        environment: dict = data["env"] if isinstance(data.get("env"), dict) else {}
        files = tuple(str(v) for k, v in environment.items() if k in RUN_VARIABLES and v)
        for key in COMMAND_KEYS:
            if isinstance(data.get(key), str):
                yield Command(data[key], arguments, cwd=cwd, files=files)
        if isinstance(data.get("path"), str):
            yield Command("", [data["path"]], folders=True)
        for value in data.values():
            yield from walked(value)
    elif isinstance(data, list):
        for value in data:
            yield from walked(value)


def front_matter(text: str) -> Any:
    """A custom agent's front matter, as far as its commands go: YAML's flow style (`{s:
    {command: node, args: [x]}}`) read as such, block style line by line ({command, args})."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    found: list = []
    listing = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, colon, value = line.strip().partition(":")
        value = value.strip()
        if colon and value.startswith(("{", "[")):
            with suppress(ValueError):
                found.append(flow(value))
                listing = False
                continue
        value = value.strip("\"'")
        if colon and key.strip("- ") in COMMAND_KEYS and value:
            found.append({"command": value, "args": []})
            listing = False
        elif colon and key.strip("- ") == "args" and found and isinstance(found[-1], dict):
            listing = not value
            if value.startswith("["):
                with suppress(ValueError):
                    found[-1].setdefault("args", []).extend(map(str, flow(value)))
        elif listing and line.strip().startswith("- ") and isinstance(found[-1], dict):
            found[-1].setdefault("args", []).append(line.strip()[2:].strip().strip("\"'"))
        else:
            listing = False
    return found


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
        # (A Python module is looked for, as a file: said only when it isn't found, by runs.)
        if not (name == "deno" and rest[:1] == ["task"]):
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


def named(folders: dict[str, dict]) -> list[str]:
    """The settings a snapshot holds, by path from the top (not the files they run)."""
    return sorted(
        {shown_entry(folder, entry) for folder, entries in folders.items() for entry in entries
         if RUN not in entry}
    )  # fmt: skip


def changes(now: dict[str, dict], then: dict[str, dict]) -> list[tuple[str, str]]:
    """(what, path) for each entry added, changed, removed, or never taken as unchanged
    ("refused": a link out of the folder, a special file, said with why)."""
    found = []
    for folder in sorted(now.keys() | then.keys()):
        current, saved = now.get(folder, {}), then.get(folder, {})
        for entry in sorted(current.keys() | saved.keys()):
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
# there, not looked at by the owner yet]}, "unchecked": {what of settings too many to check
# the owner said to use as they were (unchecked_key: their names, those folders): glimpse},
# "code": digest of the project code lines said (code_line)}.


def unchecked_key(names: tuple[str, ...], folders: list[str]) -> str:
    return json.dumps([sorted(names), sorted(folders)])


def fresh() -> dict:
    return {"folders": {}, "known": {}, "first": {}, "unchecked": {}, "code": ""}


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
        for key in [key for key in held if key == entry or key.startswith(entry + RUN)]:
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
            setting, _, ran = entry.partition(RUN)
            path = ran or prefix + setting.split("\0")[0]
            if path not in wanted and not any(path.startswith(f"{c}/") for c in wanted):
                continue
            owned = (theirs or {}).get("folders", {}).get(folder, {}).get(entry)
            if owned is None and untouched:
                held: dict = {}
                with suppress(TooLargeError):
                    record(
                        owner, owner / (ran or prefix + setting.split("\0")[0]), "x", held, budget
                    )
                owned = held.get("x")
            if owned == entries[entry]:
                continue
            del entries[entry]
            name = next((n for n in AGENT_SETTINGS if under(setting, n)), setting)
            snapshot["first"][folder] = sorted({*snapshot["first"].get(folder, []), name})
            changed = True
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
