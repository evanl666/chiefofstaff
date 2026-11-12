"""Where workers work: a workspace folder each, with worktrees or clones of their repositories."""

import asyncio
import fnmatch
import hashlib
import itertools
import json
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from . import machine, receipts
from .models import Settings
from .policy import within
from .roles import WORKER
from .store import Store

# Workers' branches: chief/<task-slug>-<id>.
BRANCH_PREFIX = "chief/"


class TimedOutError(ValueError):
    """A command stopped for taking too long (not one that failed by itself)."""


async def command(
    *arguments: str, limit: float = 60, stdin_data: bytes | None = None, env: dict | None = None
) -> str:
    """Run a command without a terminal (it can never wait for a password prompt), and without
    Chief of Staff's own CHIEFOFSTAFF_* variables (a token one may hold): what it runs can run code
    a repository or a worker configured (a git filter)."""
    own = {key: value for key, value in os.environ.items() if not key.startswith("CHIEFOFSTAFF_")}
    env = {**own, **(env or {}), "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    # ssh asks for passphrases and host keys on the terminal, not stdin: make it fail instead.
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdin=asyncio.subprocess.DEVNULL if stdin_data is None else asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,  # No controlling terminal to prompt on.
    )
    try:
        async with asyncio.timeout(limit):
            stdout, stderr = await process.communicate(stdin_data)
    except TimeoutError as exc:
        machine.kill_process_tree(process.pid)
        await process.wait()
        raise TimedOutError(f"{arguments[0]} timed out.") from exc
    if process.returncode:
        error = stderr.decode(errors="replace").strip()[:500]
        raise ValueError(f"{arguments[0]} failed: {error}")
    return stdout.decode(errors="replace")


async def git(*arguments: str, limit: float = 60, stdin_data: bytes | None = None) -> str:
    return await command("git", *SAFE_GIT, *arguments, limit=limit, stdin_data=stdin_data)


# Chief of Staff's own git runs outside any sandbox: nothing in a repository's config may run
# code for it (a file-system monitor, hooks).
SAFE_GIT = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
if sys.platform == "win32":
    SAFE_GIT += ("-c", "core.longpaths=true")
# What running code leaves behind in a project without a .gitignore for it: not work to commit,
# review or count (`__pycache__/`, `.pytest_cache/`, `node_modules/`, `*.pyc`, and a `uv.lock`
# that `uv run` wrote where the project keeps none). Only new files: a tracked one that changed
# is work.
GENERATED = re.compile(
    r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.hypothesis|\.tox|\.nox|"
    r"\.venv|venv|node_modules|\.gradle|\.next|\.turbo|\.cache|\.coverage|htmlcov|"
    r"[^/]+\.egg-info)(/|$)|\.(pyc|pyo|class|o)$|(^|/)(\.DS_Store|uv\.lock)$"
)
# What a package manager writes when it runs (`uv run` a uv.lock): one the project did not have
# is new to it. One it has, updated, is normal.
LOCKFILES = re.compile(
    r"(^|/)(uv\.lock|poetry\.lock|Pipfile\.lock|pdm\.lock|package-lock\.json|yarn\.lock|"
    r"pnpm-lock\.yaml|bun\.lockb?|Cargo\.lock|Gemfile\.lock|composer\.lock)$"
)


def generated_paths(names: list[str]) -> list[str]:
    """Generated files as one names them: a folder of them once (`pkg/__pycache__/`)."""
    found: dict[str, None] = {}
    for name in names:
        match = GENERATED.search(name)
        folder = match and match.group(2) and match.group(3) == "/"
        found[name[: match.end(2)] + "/" if match and folder else name] = None
    return list(found)


def untracked_work(names: list[str]) -> list[str]:
    """New files that are work, not what running it left behind."""
    return [name for name in names if name and not GENERATED.search(name.rstrip("/"))]


def worth_committing(status: str) -> list[str]:
    """The lines of `git status --porcelain` that are work: changed files, and new ones that
    are not generated."""
    return [
        line
        for line in status.splitlines()
        if line.strip() and not (line.startswith("?? ") and not untracked_work([line[3:]]))
    ]


# Config that runs a program or sends git elsewhere: not allowed in a repository a worker can
# write the config of (a clone in its own folder), when Chief of Staff runs git there.
RUNS_CODE = re.compile(
    r"^(core\.(fsmonitor|hookspath|sshcommand|pager|editor|askpass|gitproxy|worktree|"
    r"alternaterefscommand)|credential\..*|filter\..*|diff\..*\.(command|textconv)|"
    r"merge\..*\.driver|include\..*|includeif\..*|url\..*|gpg\..*|sequence\.editor|"
    r"uploadpack\..*|receive\..*|remote\..*\.(uploadpack|receivepack|proxy|vcs))$",
    re.IGNORECASE,
)


def admin_dir(common: str, tree: str) -> str:
    """A linked worktree's own folder in its repository (.git/worktrees/<name>), found from the
    repository's side, not from the worktree's .git file (which its worker can rewrite)."""
    root = Path(common) / "worktrees"
    target = (Path(tree) / ".git").resolve()
    with suppress(OSError):
        for child in sorted(root.iterdir()):
            with suppress(OSError):
                if Path((child / "gitdir").read_text().strip()).resolve() == target:
                    return str(child)
    return ""


def linked_tree(common: str, path: Path) -> tuple[str, str] | None:
    """The linked worktree of this repository that holds `path` (the owner's project folder may
    be one), as (its admin folder, its root), read from the repository's side; None if `path`
    is in no linked worktree."""
    best: tuple[str, str] | None = None
    with suppress(OSError):
        for child in sorted((Path(common) / "worktrees").iterdir()):
            with suppress(OSError):
                root = Path((child / "gitdir").read_text().strip()).resolve().parent
                if path.is_relative_to(root) and (not best or len(str(root)) > len(best[1])):
                    best = (str(child), str(root))
    return best


def git_pins(checkout: dict, path: str) -> dict[str, str] | None:
    """Where git finds the repository of one of a worker's checkouts, fixed by the environment:
    the pointer files a sandboxed worker can rewrite (its worktree's .git, commondir) are not
    read, so they cannot send Chief of Staff's git to a config the worker wrote. None when the
    checkout's repository cannot be told apart safely."""
    common = checkout.get("git_dir") or (
        str(Path(checkout["repo"]) / ".git") if checkout.get("repo") else ""
    )
    # os.path.isdir, not Path.is_dir: False, not an error, for a folder it cannot look at.
    if not common or not os.path.isdir(common):  # noqa: PTH112
        return {}
    planted = Path(common) / "commondir"
    if Path(common).name == ".git" and planted.exists():
        # A repository's own .git never has one: a worker made it to send git elsewhere.
        logger.warning("Removed %s, which pointed git at another repository.", planted)
        with suppress(OSError):
            planted.unlink()
    tree = checkout.get("worktree")
    resolved = Path(path).resolve()
    if tree and os.path.isdir(tree) and resolved.is_relative_to(Path(tree).resolve()):  # noqa: PTH112
        admin = checkout.get("admin") or ""
        if not admin or not os.path.isdir(admin):  # noqa: PTH112
            admin = admin_dir(common, tree)  # Made again when the worker was reopened.
        if not admin:
            return None
        return {"GIT_DIR": admin, "GIT_COMMON_DIR": common, "GIT_WORK_TREE": tree}
    if found := linked_tree(common, resolved):
        # The owner's project folder is itself a linked worktree: its own HEAD, index, files.
        admin, root = found
        return {"GIT_DIR": admin, "GIT_COMMON_DIR": common, "GIT_WORK_TREE": root}
    top = Path(common).parent
    if Path(common).name == ".git" and resolved.is_relative_to(top.resolve()):
        return {"GIT_DIR": common, "GIT_COMMON_DIR": common, "GIT_WORK_TREE": str(top)}
    return {}


def config_runs_code(common: str) -> list[str]:
    """Keys in a repository's own config that would run code or send git elsewhere."""
    found = []
    with suppress(OSError):
        text = (Path(common) / "config").read_text(errors="replace")
        section = ""
        for line in text.splitlines():
            line = line.strip()
            if match := re.match(r'^\[\s*([\w.-]+)(?:\s+"([^"]*)")?\s*\]', line):
                section = match.group(1) + (f".{match.group(2)}" if match.group(2) else "")
            elif section and (key := re.match(r"^([\w-]+)\s*(=|$)", line)):
                name = f"{section}.{key.group(1)}"
                if RUNS_CODE.match(name):
                    found.append(name)
    return found


async def checkout_git(
    checkout: dict, path: str, *arguments: str, limit: float = 60, env: dict | None = None
) -> str:
    """git in one of a worker's checkouts, as Chief of Staff: the repository pinned (git_pins),
    nothing in its config run, and a clone in the worker's own folder (whose config it can
    write) refused if its config would run code."""
    pins = await asyncio.to_thread(git_pins, checkout, path)
    if pins is None:
        raise ValueError(f"Not reading {path}: its worktree cannot be found in its repository.")
    if checkout.get("remote") and (found := await asyncio.to_thread(
        config_runs_code, pins.get("GIT_COMMON_DIR") or str(Path(path) / ".git")
    )):  # fmt: skip
        raise ValueError(f"Not reading {path}: its git config was changed ({', '.join(found)}).")
    return await command(
        "git", *SAFE_GIT, "-C", path, *arguments, limit=limit, env={**pins, **(env or {})}
    )


# Repository -> when its default branch was last fetched; parallel workers fetch once.
FETCHED: dict[str, float] = {}
FETCH_LOCKS: dict[str, asyncio.Lock] = {}
FETCH_FRESH_SECONDS = 120
# A worker waits for its fetch at most this long, then starts from what this machine has: a
# terminal session starts from the checkout as it is, and a slow network or a huge repository
# must not keep a worker from starting.
FETCH_LIMIT_SECONDS = 30


async def default_branch(top: Path) -> str:
    """origin's default branch as this checkout knows it ("origin/main"), or ""."""
    with suppress(ValueError):
        ref = await git(
            "-C", str(top), "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"
        )
        if ref.strip():
            return ref.strip()
    for candidate in ("origin/main", "origin/master"):
        with suppress(ValueError):
            await git("-C", str(top), "rev-parse", "--verify", "--quiet", candidate)
            return candidate
    return ""


async def base_ref(top: Path) -> tuple[str, str]:
    """Where a new worktree starts: the default branch's latest commit when there is a remote.

    Only that branch is fetched (a large repository has many), and once for workers that start
    together."""
    if "origin" not in (await git("-C", str(top), "remote")).split():
        return "HEAD", "the HEAD of the local checkout (no origin remote)"
    note = ""
    async with FETCH_LOCKS.setdefault(str(top), asyncio.Lock()):
        ref = await default_branch(top)
        if time.monotonic() - FETCHED.get(str(top), -FETCH_FRESH_SECONDS) >= FETCH_FRESH_SECONDS:
            try:
                if ref:
                    await git(
                        "-C", str(top), "fetch", "--quiet", "origin", ref.removeprefix("origin/"),
                        limit=FETCH_LIMIT_SECONDS,
                    )  # fmt: skip
                else:
                    await git(
                        "-C", str(top), "fetch", "--quiet", "origin", limit=FETCH_LIMIT_SECONDS
                    )
                    ref = await default_branch(top)
                FETCHED[str(top)] = time.monotonic()
            except ValueError:
                note = " (fetch failed, so maybe not the latest)"
    if not ref:
        return "HEAD", "the HEAD of the local checkout (origin has no default branch)"
    return ref, ref + note


GIT_URL = re.compile(r"^(https?://|ssh://|git@|file://)\S+$")


GITHUB_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


logger = logging.getLogger(__name__)

# Workers' own folders, inside the code folder (hidden, so never taken for repositories).
TASK_FOLDERS = ".chief"
# Where repositories are looked for in a code folder, and what is never looked into.
CODE_FOLDER_DEPTH = 2
MAX_REPOSITORIES = 100
SKIPPED = {"node_modules", "venv", "__pycache__", "dist", "build", "target", "Library"}


def linked_worktree(path: Path) -> bool:
    """Whether a folder is a linked worktree's: its `.git` a file pointing at a git dir."""
    pointer = path / ".git"
    with suppress(OSError):
        if pointer.is_file() and not pointer.is_symlink():
            with pointer.open("rb") as handle:
                return handle.read(8) == b"gitdir: "
    return False


def find_repositories(folder: Path, skip: set[Path], worktrees: bool = False) -> list[str]:
    """Git repositories in a code folder, and in its folders up to CODE_FOLDER_DEPTH deep
    (`worktrees`: linked worktrees too, such as one a worker made in its folder).

    A repository's own subfolders are not searched (worktrees and submodules belong to it);
    hidden folders, dependencies and `skip` (Chief of Staff's own folders) are left out.
    """
    found: list[str] = []
    level = [folder]
    for depth in range(CODE_FOLDER_DEPTH + 1):
        deeper = []
        for path in level:
            if (path / ".git").is_dir() or (worktrees and linked_worktree(path)):
                found.append(str(path))
                continue
            if depth == CODE_FOLDER_DEPTH:
                continue
            with suppress(OSError):
                for entry in sorted(os.scandir(path), key=lambda entry: entry.name):
                    child = Path(entry.path)
                    if (
                        entry.is_dir(follow_symlinks=False)
                        and not entry.name.startswith(".")
                        and entry.name not in SKIPPED
                        and child not in skip
                    ):
                        deeper.append(child)
            if len(found) >= MAX_REPOSITORIES:
                return found
        level = deeper
    return found[:MAX_REPOSITORIES]


def clone_source(repo: str) -> tuple[str, str]:
    """The URL to clone a git URL or GitHub owner/name from, and the folder name for it."""
    if GITHUB_NAME.fullmatch(repo):
        url = f"https://github.com/{repo}.git"
    elif GIT_URL.fullmatch(repo):
        url = repo
    else:
        raise ValueError("Give a configured project folder, a git URL or GitHub owner/name.")
    return url, re.split(r"[/:\\]", url.rstrip("/\\").removesuffix(".git"))[-1]


def repository_key(url: str) -> str:
    """One name for the different URLs of a repository (https, ssh, owner/name, a local path)."""
    text = url.strip().rstrip("/").removesuffix(".git").lower()
    if match := re.search(r"github\.com[:/]([^/]+/[^/]+)$", text):
        return "github:" + match.group(1)
    if GITHUB_NAME.fullmatch(text):
        return "github:" + text
    return text.removeprefix("file://")


# Gitignored files a new worktree gets from the main checkout: the patterns in the repository's
# .worktreeinclude (as with Claude Code), else its .env files.
WORKTREE_INCLUDE = ".worktreeinclude"
DEFAULT_INCLUDE = (".env", ".env.*")
MAX_CARRIED = 200
MAX_CARRIED_BYTES = 50_000_000


async def carry_ignored_files(top: Path, tree: Path) -> list[str]:
    """Copy the main checkout's gitignored files that a worktree should have (like .env) into
    it; tracked files are there already. Returns their paths relative to the repository."""
    include = top / WORKTREE_INCLUDE
    if await asyncio.to_thread(include.is_file):
        patterns = ["--exclude-from", str(include)]
    else:
        patterns = [arg for pattern in DEFAULT_INCLUDE for arg in ("--exclude", pattern)]
    try:
        # In a huge checkout this walks every untracked folder: never wait long for it.
        listed = await git(
            "-C", str(top), "ls-files", "--others", "--ignored", "-z", *patterns, limit=15
        )  # fmt: skip
    except ValueError:
        logger.warning("Listing the files to carry into a worktree of %s took too long.", top)
        return []
    candidates = [path for path in listed.split("\0") if path][:MAX_CARRIED]
    if not candidates:
        return []
    try:  # Only what git ignores; check-ignore fails when it ignores none of them.
        ignored = await git(
            "-C", str(top), "check-ignore", "--stdin", "-z",
            stdin_data="\0".join(candidates).encode() + b"\0",
        )  # fmt: skip
    except ValueError:
        return []
    return await asyncio.to_thread(copy_files, top, tree, [p for p in ignored.split("\0") if p])


def copy_files(source: Path, target: Path, paths: list[str]) -> list[str]:
    copied, size = [], 0
    for relative in paths:
        origin, copy = source / relative, target / relative
        if not origin.is_file() or copy.exists():
            continue
        size += origin.stat().st_size
        if size > MAX_CARRIED_BYTES:
            break
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, copy)
        copied.append(relative)
    return copied


def free_child(parent: Path, name: str) -> Path:
    """A path under `parent` named `name` (or name-2, name-3, …) that does not exist yet."""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.") or "repo"
    path, number = parent / name, 2
    while path.exists():
        path, number = parent / f"{name}-{number}", number + 1
    return path


def slugify(title: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:30] or "task"


def resolve_project(project: str, projects: list[str]) -> Path:
    path = Path(project.strip()).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Project directory does not exist: {project}")
    if path == Path.home() or path == Path(path.anchor):
        raise ValueError("Choose a project directory, not your home or filesystem root.")
    if not any(within(str(path), item) for item in projects):
        configured = ", ".join(projects) or "none yet"
        raise ValueError(
            f"{path} is not inside a configured project ({configured}). Ask the owner with "
            "ask_workspace; their choice adds it."
        )
    return path


def package_name(root: Path) -> str:
    """The name its manifest gives the project (pyproject, package.json, Cargo, go.mod)."""
    patterns = [
        ("pyproject.toml", r'(?ms)^\[(?:project|tool\.poetry)\][^\[]*?^name\s*=\s*["\']([^"\']+)'),
        ("package.json", r'"name"\s*:\s*"([^"]+)"'),
        ("Cargo.toml", r'(?ms)^\[package\][^\[]*?^name\s*=\s*"([^"]+)"'),
        ("go.mod", r"(?m)^module\s+(\S+)"),
    ]
    for name, pattern in patterns:
        with suppress(OSError):
            text = (root / name).read_text(errors="replace")[:20000]
            if match := re.search(pattern, text):
                return match.group(1).rsplit("/", 1)[-1][:60]
    return ""


def readme_line(project: Path) -> str:
    """The first meaningful line of a project's README, as a hint of what the project is."""
    for name in ("README.md", "README.rst", "README.txt", "README"):
        path = project / name
        if path.is_file():
            with path.open(encoding="utf-8", errors="replace") as file:
                for line in itertools.islice(file, 40):
                    text = line.strip().lstrip("#").strip()
                    if text and not text.startswith(("[!", "<", "![", "=", "-")):
                        return text[:160]
    return ""


async def common_git_dir(path: Path) -> str:
    """The repository's own .git directory (shared by all its worktrees), or "" if none."""
    try:
        found = await git(
            "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
    except ValueError:
        return ""
    return str(Path(found.strip())) if found.strip() else ""


def git_dirs(checkouts: list[dict]) -> list[str]:
    """The .git directories a worker's commits write to, outside its workspace."""
    found = []
    for checkout in checkouts:
        if checkout.get("remote"):
            continue  # A clone in the workspace: its .git is inside already.
        git_dir = checkout.get("git_dir") or (
            str(Path(checkout["repo"]) / ".git") if checkout.get("repo") else ""
        )
        if git_dir and git_dir not in found:
            found.append(git_dir)
    return found


def protected_git_paths(git_dir: str, checkouts: list[dict]) -> list[str]:
    """Parts of a repository's .git that a worker must not change, though it commits there.

    Hooks and config run code when the owner uses the repository; so do the configs of its
    submodules (modules/) and of the owner's other worktrees, whose private folders also hold
    their HEAD and index. The worker's own worktrees stay writable.
    """
    root = Path(git_dir)
    own = set()
    shared = any(
        checkout.get("shared") and Path(checkout.get("git_dir", "")).resolve() == root.resolve()
        for checkout in checkouts
    )
    for checkout in checkouts:
        pointer = Path(checkout.get("path", "")) / ".git"
        with suppress(OSError, ValueError):
            if pointer.is_file() and (text := pointer.read_text()).startswith("gitdir:"):
                own.add(Path(text.removeprefix("gitdir:").strip()).resolve())
    # Pointers that tell git where a repository is: a worker that rewrites them sends git (the
    # owner's, or Chief of Staff's) to a config it wrote. A repository's own .git has no
    # commondir; a worker must not make one.
    paths = [root / "hooks", root / "config", root / "commondir"]
    for admin in sorted(own):
        paths += [admin / "commondir", admin / "gitdir"]
    for checkout in checkouts:
        pointer = Path(checkout.get("worktree") or "") / ".git"
        if checkout.get("worktree") and pointer.is_file():
            paths.append(pointer)
    if not shared:
        # The owner's own checkout: its branch and staged changes are theirs. (A worker that
        # works in the owner's checkout itself needs them.)
        paths += [root / name for name in ("HEAD", "index") if (root / name).exists()]
    if (root / "modules").is_dir():
        paths.append(root / "modules")
    if (root / "worktrees").is_dir():
        paths += sorted(
            path
            for path in (root / "worktrees").iterdir()
            if path.is_dir() and path.resolve() not in own
        )
    return [str(path) for path in paths]


def locate(checkout: dict) -> tuple[str, str, bool]:
    """Where to read a checkout's work: (folder, head, live). A closed worker's worktree may be
    gone (see Workspaces.release); its branch is still in the repository, with all its work."""
    tree = checkout.get("worktree")
    # os.path.isdir, not Path.is_dir: a worktree it cannot look at is gone, not an error.
    if tree and checkout.get("branch") and not os.path.isdir(tree):  # noqa: PTH112
        return checkout["repo"], f"refs/heads/{checkout['branch']}", False
    return checkout["path"], "HEAD", True


def base_of(checkout: dict) -> str:
    return checkout.get("base_ref") or ("" if checkout.get("shared") else "origin/HEAD")


async def version(checkout: dict) -> dict | None:
    """Which code a checkout holds now: its commit, and a digest of its uncommitted edits ("" for
    none). Evidence is about one version; when this changes, the evidence is out of date."""
    path, head, live = await asyncio.to_thread(locate, checkout)
    try:
        commit = (await checkout_git(checkout, path, "rev-parse", head)).strip()
    except ValueError:
        return None
    edits = ""
    if live:
        with suppress(ValueError):
            status = await checkout_git(checkout, path, "status", "--porcelain")
            edits = "\n".join(worth_committing(status))
            if edits.strip():
                edits += await checkout_git(
                    checkout, path, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "HEAD"
                )
                # New files show in the status by name only: their contents count too (not a
                # test run's caches, which change with every run).
                others = await checkout_git(
                    checkout, path, "ls-files", "--others", "--exclude-standard", "-z"
                )
                names = untracked_work(others.split("\0"))[:MAX_NEW_FILES]
                edits += await asyncio.to_thread(file_digest, Path(path), names)
    digest = (
        hashlib.sha1(edits.encode(), usedforsecurity=False).hexdigest()[:12]
        if edits.strip()
        else ""
    )
    return {"commit": commit, "edits": digest, "digest": DIGEST}


# How `edits` is computed: digests of different kinds are not compared (see same_version).
DIGEST = 3


def same_version(recorded: dict, current: dict) -> bool:
    """Whether a checkout still holds the code it held when it was checked."""
    if current["commit"] != recorded.get("commit"):
        return False
    if recorded.get("digest") != current.get("digest"):
        return True  # Recorded by an older Chief of Staff: only the commit can be compared.
    return current["edits"] == recorded.get("edits", "")


def file_digest(top: Path, names: list[str]) -> str:
    """The contents of new files, read here: one odd entry (a nested repository, a broken link)
    must not leave the others out."""
    found = hashlib.sha1(usedforsecurity=False)
    for name in names:
        path = top / name
        if name.endswith("/") or not path.is_file():
            continue
        with suppress(OSError), path.open("rb") as file:
            found.update(name.encode() + b"\0" + file.read(MAX_NEW_FILE))
    return found.hexdigest()


# Programs that write files they do not name on their command line (a script, a build, a
# git command that rewrites the tree): a file changed while one runs may be its work.
WRITES_UNNAMED = re.compile(
    r"^(python[\d.]*|py|node|deno|bun|ruby|perl|php|bash|sh|zsh|make|npm|npx|pnpm|yarn|uv|"
    r"poetry|pip|cargo|go|gradlew?|mvnw?|java|dotnet|xargs|find|rsync|tar|unzip|patch|"
    r"pytest|tox|nox|black|ruff|prettier|eslint|isort|gofmt|rustfmt|\./\S+|"
    r"git (apply|am|pull|merge|rebase|stash|cherry-pick|reset|checkout|switch|restore|revert))\b"
)


class CommandRun:
    """One of a task's shell commands, as far as files go: when it ran, the words that may name
    files (quotes respected: "release notes.txt"), and whether it may write files it does not
    name (a glob, a variable, a script)."""

    def __init__(self, command: str, start: float = 0, end: float = 0):
        self.start, self.end = start, end
        words: set[str] = set()
        unnamed = False
        for parts in receipts.simple_commands(command):
            program = receipts.program(parts)
            first = next((w for w in parts if not re.fullmatch(r"\w+=.*", w)), "")
            if WRITES_UNNAMED.match(program) or first.startswith(("./", "../", "/", "~/")):
                unnamed = True  # A tool, or a script of the project's (`./gen.sh`).
            for word in parts:
                if any(mark in word for mark in "*?$`[{"):
                    unnamed = True
                word = word.removeprefix("./").rstrip("/:")
                if word and not word.startswith("-"):
                    words.add(word)
                    if "/" in word:
                        words.add(word.rsplit("/", 1)[-1])
        self.words = frozenset(words)
        self.unnamed = unnamed

    def names(self, top: Path, name: str) -> bool:
        base = name.rpartition("/")[2]
        return (
            name in self.words
            or str(top / name) in self.words
            or (len(base) >= 3 and base in self.words)
        )


OPEN_RUN_SECONDS = 600
# How far a file's write time may lie outside a command's start and end (the times are those of
# its events; a file system may keep whole seconds).
RUN_SLACK_SECONDS = 1.0


def ran_then(run: CommandRun, when: float) -> bool:
    """Whether a command ran when a file was written (one that never ended, a server or a
    watcher, counts for a while, not for good)."""
    end = run.end or min(time.time(), run.start + OPEN_RUN_SECONDS)
    return run.start - RUN_SLACK_SECONDS <= when <= end + RUN_SLACK_SECONDS


def written_at(path: Path) -> float | None:
    """When a file was last written or moved (a rename, `tar`/`cp -p` restoring an old time
    still change its ctime); None when it is gone."""
    try:
        found = path.lstat()
    except OSError:
        return None
    return max(found.st_mtime, found.st_ctime)


def file_times(top: Path, names: list[str]) -> dict[str, float | None]:
    """When each file was last written (None: it is gone)."""
    return {name: written_at(top / name) for name in names}


def command_words(commands: str) -> frozenset[str]:
    """The words of shell commands that may name files (see CommandRun)."""
    return CommandRun(commands or "").words


class Scope:
    """One task's share of a checkout a worker kept using for several tasks: what it committed
    since `base` (the commit the checkout was at when the task was taken up), and of what is
    not committed, the files it changed: in a folder of the owner's (shared), those its own
    edits touched or its own shell commands named; elsewhere, those changed after `since` (when
    the task was taken up)."""

    def __init__(
        self,
        base: str = "",
        since: float = 0,
        touched: set[str] | None = None,
        start: str = "",
        end: dict | None = None,
        others: set[str] | None = None,
        head: str = "",
        large: set[str] | None = None,
        commands: str | list[CommandRun] = "",
        gaps: list[tuple[str, str, str, str, float]] | None = None,
        mtimes: dict[str, float | None] | None = None,
        earlier: dict[str, list[float]] | None = None,
        other_runs: list[CommandRun] | None = None,
    ):
        self.base, self.since, self.touched = base, since, touched or set()
        # The commit the checkout was on when it was taken up (`start`'s): what commits made
        # before then brought in since (a pull, another branch checked out) is not its work.
        self.head = head
        # New files too large for its start snapshot (left out of it): not its own later.
        self.large = large or set()
        # Its own shell commands (in a folder of the owner's): a file that changed while one
        # ran that names it, or one that writes files it does not name (a glob, a script), is
        # its work; not a file the owner edited while it slept or only read it.
        self.runs = (
            [CommandRun(commands)] if isinstance(commands, str) and commands else
            list(commands) if not isinstance(commands, str) else []
        )  # fmt: skip
        self.words = frozenset().union(*(run.words for run in self.runs))
        # When each file it changed was last written as of its result (a finished task's): a
        # later write by other work does not count against it.
        self.mtimes = mtimes or {}
        # When its earlier results found each file last written: a file that was its own then
        # stays its own when other work (the owner) writes it again later.
        self.earlier = earlier or {}
        # Other tasks' commands in the same folder of the owner's: a file one of them names,
        # changed as it ran, is not this task's for running a script then.
        self.other_runs = other_runs or []
        # Between a result and its follow-up, other work may have changed the checkout (a later
        # task): (tree at its result, tree when it was taken up again, their commits, and when
        # it was taken up again: a commit after that is its own, however late the snapshot).
        self.gaps = gaps or []
        # Snapshots (see snapshot): the files as they were when it was taken up (`start`, a
        # tree) and when it reported its result (`end`, {"commit", "tree"}; None while open).
        # Its share is what changed between them: shell edits too, and not an earlier or later
        # task's. `others`: files other tasks' edits wrote (in a folder of the owner's).
        self.start, self.end, self.others = start, end, others or set()

    def named(self, top: Path, name: str) -> bool:
        """Whether one of its own shell commands wrote this file, as far as can be told: the
        file changed while a command ran that names it or writes files it does not name. A run
        without times (a test) counts when it names it."""
        candidates = [run for run in self.runs if run.unnamed or run.names(top, name)]
        if not candidates:
            return False
        # None: deleted, by a command that names it, or one that may write unnamed ones.
        changed = self.mtimes[name] if name in self.mtimes else written_at(top / name)
        times = [changed, *self.earlier.get(name, [])]
        unnamed_only = False
        for run in candidates:
            if not run.start or changed is None:
                if run.names(top, name) or (changed is None and run.start):
                    return True
                continue
            if any(when is not None and ran_then(run, when) for when in times):
                if run.names(top, name):
                    return True
                unnamed_only = True
        if not unnamed_only:
            return False
        # Only a command that may write files it does not name ran then: not when another
        # task's command that names this file ran then too.
        return not any(
            other.names(top, name) and any(w is not None and ran_then(other, w) for w in times)
            for other in self.other_runs
        )

    def wrote(self, top: Path, name: str) -> bool:
        """Whether its own edits wrote this file."""
        return os.path.realpath(top / name) in self.touched

    def others_wrote(self, top: Path, name: str) -> bool:
        """Whether another task's edits wrote this file."""
        return os.path.realpath(top / name) in self.others

    def keeps(self, top: Path, name: str, shared: bool, committed: set[str]) -> bool:
        if name in committed:
            return True
        path = top / name
        if shared:
            return self.wrote(top, name)
        if not self.since:
            return True
        try:
            return path.lstat().st_mtime >= self.since - 1
        except OSError:
            return True  # Deleted: a change all the same.


MAX_SNAPSHOT_FILES = 5000
MAX_SNAPSHOT_FILE = 20_000_000
MAX_LARGE_LEFT_OUT = 50


def too_large(top: str, names: list[str]) -> list[str]:
    """Those of these files too large to copy into a snapshot."""
    found = []
    for name in names:
        with suppress(OSError):
            if (Path(top) / name).lstat().st_size > MAX_SNAPSHOT_FILE:
                found.append(name)
    return found


async def snapshot(checkout: dict) -> dict | None:
    """What a checkout holds now, all of it: its commit, and a tree of its files as they are
    (edited, new; not ignored ones). Written to the repository's objects only, through an index
    of its own: no branch, index or file of the checkout changes. New files too large to copy
    are left out, listed in `large`; with very many new files, its commit and no tree ("");
    None when it cannot be taken."""
    path, head, live = await asyncio.to_thread(locate, checkout)
    try:
        commit = (await checkout_git(checkout, path, "rev-parse", head)).strip()
        if not live:
            tree = await checkout_git(checkout, path, "rev-parse", f"{head}^{{tree}}")
            return {"commit": commit, "tree": tree.strip()}
        top = (await checkout_git(checkout, path, "rev-parse", "--show-toplevel")).strip()
        others = await checkout_git(
            checkout, top, "ls-files", "--others", "--exclude-standard", "-z"
        )
        names = [name for name in others.split("\0") if name]
        large = await asyncio.to_thread(too_large, top, names)
        if len(names) > MAX_SNAPSHOT_FILES or len(large) > MAX_LARGE_LEFT_OUT:
            return {"commit": commit, "tree": "", "large": []}  # Its commit all the same.
        index = (await checkout_git(checkout, top, "rev-parse", "--git-path", "index")).strip()
        index = str(Path(top) / index) if not Path(index).is_absolute() else index
        with tempfile.TemporaryDirectory(prefix="cos-snap-") as folder:
            own = str(Path(folder) / "index")
            with suppress(FileNotFoundError):
                await asyncio.to_thread(shutil.copyfile, index, own)
            env = {"GIT_INDEX_FILE": own}
            # New files too large to copy are left out (and listed: a task's large new file).
            left_out = [f":(exclude,literal){name}" for name in large]
            await checkout_git(checkout, top, "add", "-A", "--", ".", *left_out, limit=120, env=env)
            tree = (await checkout_git(checkout, top, "write-tree", env=env)).strip()
        return {"commit": commit, "tree": tree, "large": large}
    except (ValueError, OSError):
        return None


# A C-style escape in a path git quoted: \" \\ \t \n, or a byte in octal.
GIT_ESCAPE = re.compile(r"\\([0-7]{3}|.)")
GIT_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13}


def git_path(name: str) -> str:
    """A path as git printed it, as it is: git quotes one with a `"`, a `\\` or a control
    character ("docs/release \"v2\".md") even with core.quotePath off."""
    if len(name) < 2 or not (name.startswith('"') and name.endswith('"')):
        return name
    out = bytearray()
    at = 0
    body = name[1:-1]
    for match in GIT_ESCAPE.finditer(body):
        out += body[at : match.start()].encode()
        code = match.group(1)
        if len(code) == 3:
            out.append(int(code, 8))
        else:
            out.append(GIT_ESCAPES.get(code, ord(code)))
        at = match.end()
    out += body[at:].encode()
    return out.decode(errors="replace")


def status_name(line: str) -> str:
    """The file a `git status --porcelain` line is about (a rename's new name)."""
    return git_path(line[3:].split(" -> ", 1)[-1])


async def share_files(checkout: dict, scope: Scope) -> set[str]:
    """The files that are one task's share of a checkout (see Scope)."""
    path, head, live = await asyncio.to_thread(locate, checkout)
    share = await scope_files(checkout, path, head, live, bool(checkout.get("shared")), scope)
    return {*share.keep, *share.fresh, *share.large}


async def changes(checkout: dict, scope: Scope | None = None) -> dict | None:
    """What a worker changed in one checkout: commits on its branch and uncommitted edits, and
    the version (commit and edits) this describes. With a `scope`, one task's share of it."""
    path, head, live = await asyncio.to_thread(locate, checkout)
    try:
        await checkout_git(checkout, path, "rev-parse", "--git-dir")
    except ValueError:
        return None
    result = {"path": checkout["path"], "branch": checkout.get("branch", ""), "commits": 0}
    if live:
        # The branch it is on now: a PR it checked out to review is someone else's, not one
        # to open a PR from.
        with suppress(ValueError):
            current = (
                await checkout_git(checkout, path, "rev-parse", "--abbrev-ref", "HEAD")
            ).strip()
            if current and current != "HEAD":
                result["branch"] = current
    shared = bool(checkout.get("shared"))
    if shared:
        result["shared"] = True  # A folder of the owner's: no branch of its own to open a PR from.
    if scope is not None:
        result.update(await scoped_changes(checkout, path, head, live, shared, scope))
        result.update(await version(checkout) or {})
        return result
    # One count of what differs from where its branch started, committed or not (a file
    # changed in a commit and again since counts once), and its new files.
    total = {"files": 0, "insertions": 0, "deletions": 0}
    since = "HEAD" if live else ""
    if base := base_of(checkout):
        with suppress(ValueError):
            result["commits"] = int(
                await checkout_git(checkout, path, "rev-list", "--count", f"{base}..{head}")
            )
        with suppress(ValueError):
            since = (await checkout_git(checkout, path, "merge-base", base, head)).strip()
        if since and not live:
            since = f"{since}..{head}"
    if since:
        with suppress(ValueError):
            diffed = await checkout_git(checkout, path, "diff", "--shortstat", since)
            total = receipts.shortstat(diffed)
    if live:
        with suppress(ValueError):
            status = await checkout_git(checkout, path, "status", "--porcelain")
            result["uncommitted"] = len(worth_committing(status))
    if live and not shared:  # The owner's own folder has their files too.
        with suppress(ValueError):
            listed = await checkout_git(
                checkout, path, "ls-files", "--others", "--exclude-standard", "-z"
            )
            new = untracked_work(listed.split("\0"))
            total["files"] += len(new)
            total["insertions"] += await asyncio.to_thread(count_lines, Path(path), new)
    result.update(total)
    result.update(await version(checkout) or {})
    return result


async def scope_base(checkout: dict, path: str, head: str, scope: Scope) -> tuple[str, str]:
    """Where this task's share starts, and the commit to count its files from when the start
    snapshot no longer applies (else ""): its own base when that commit is in the history;
    amended since, where it and the history meet (its snapshot still applies); rebased, the
    rewritten start commit; on another line of history (a branch made from elsewhere), the
    newest commit made before it was taken up. Else where the branch started, or where the
    default branch now meets it when the branch was rebased onto a newer one."""
    start = scope.base or (scope.head if checkout.get("shared") else "")
    if start:
        with suppress(ValueError):
            await checkout_git(checkout, path, "merge-base", "--is-ancestor", start, head)
            # Rebased onto a newer main since: from where it meets main now (its snapshot was
            # of the older one).
            newer = await newer_base(checkout, path, head, start)
            return (newer, newer) if newer != start else (start, "")
        with suppress(ValueError):
            if met := (await checkout_git(checkout, path, "merge-base", start, head)).strip():
                again = await rewritten(checkout, path, start, met, head)
                if again and await parent_of(checkout, path, again) == await parent_of(
                    checkout, path, start
                ):
                    return met, ""  # Amended: its start snapshot is still what came before.
                if again:
                    return again, again  # Rebased: from the start commit as it is now.
                if scope.since:
                    since = f"--before=@{int(scope.since)}"
                    listed = await checkout_git(
                        checkout, path, "rev-list", "-1", "--first-parent", since, head
                    )
                    if before := listed.strip():
                        return before, before
                return met, met
    if base := base_of(checkout):
        with suppress(ValueError):
            met = (await checkout_git(checkout, path, "merge-base", base, head)).strip()
            return await newer_base(checkout, path, head, met), ""
    return "", ""


async def rewritten(checkout: dict, path: str, commit: str, met: str, head: str) -> str:
    """The commit in met..head that is `commit` rewritten (amended, rebased: the same author
    time and subject), or ""."""
    with suppress(ValueError):
        own = (await checkout_git(checkout, path, "log", "-1", "--format=%at %s", commit)).strip()
        listed = await checkout_git(
            checkout, path, "log", "--format=%H %at %s", "-n", "500", f"{met}..{head}"
        )
        for line in listed.splitlines():
            sha, _, rest = line.partition(" ")
            if own and rest.strip() == own:
                return sha
    return ""


async def parent_of(checkout: dict, path: str, commit: str) -> str:
    with suppress(ValueError):
        return (await checkout_git(checkout, path, "rev-parse", f"{commit}^")).strip()
    return ""


async def local_default(checkout: dict, path: str) -> str:
    """Without an origin: the branch the owner's own checkout of the repository is on (their
    main), when it is not the one this checkout is on."""
    with suppress(ValueError):
        owner = checkout.get("repo") or path
        branch = (await checkout_git(checkout, owner, "symbolic-ref", "--short", "HEAD")).strip()
        here = (await checkout_git(checkout, path, "symbolic-ref", "--short", "HEAD")).strip()
        if branch and branch != here:
            return f"refs/heads/{branch}"
    return ""


async def newer_base(checkout: dict, path: str, head: str, base: str) -> str:
    """Where a branch starts now: rebased onto a newer default branch, where they meet now
    (that branch's newer commits are others' work); else `base`. Not when the default branch
    has the branch's own work in it (merged): then that work is still the branch's."""
    if not base:
        return base
    with suppress(ValueError):
        top = (await checkout_git(checkout, path, "rev-parse", "--show-toplevel")).strip()
        default = await default_branch(Path(top)) or await local_default(checkout, path)
        if not default:
            return base
        try:
            await checkout_git(checkout, path, "merge-base", "--is-ancestor", head, default)
            return base  # Its work is in the default branch already.
        except ValueError:
            pass
        met = (await checkout_git(checkout, path, "merge-base", default, head)).strip()
        if met and met != base:
            await checkout_git(checkout, path, "merge-base", "--is-ancestor", base, met)
            return met
    return base


class Share:
    """One task's share of a checkout (see Scope)."""

    def __init__(self, base: str, top: Path):
        self.base, self.top = base, top
        self.own: dict[str, list[str]] = {}  # Its commits, newest first, and their files.
        # Whether base..head is its commits alone (no merge brought others' in, none left out):
        # then one diff from its base is its share; else its commits' own patches are.
        self.exact = True
        self.committed: set[str] = set()
        self.keep: list[str] = []  # Changed files it keeps, committed or not.
        self.fresh: list[str] = []  # Its new untracked files.
        # From snapshots: the trees its share lies between, the commit at its end, and which
        # kept files were not committed then.
        self.start, self.end, self.end_commit = "", "", ""
        self.uncommitted: set[str] = set()
        self.large: list[str] = []  # Its new files too large to count or show.
        # In a folder of the owner's: what changed there meanwhile that is not its own.
        self.not_counted: list[str] = []
        # Kept files that other work changed between its result and a follow-up: counted and
        # shown over its own stretches only (start to result, taken up again to end).
        self.split: set[str] = set()
        self.segments: list[tuple[str, str]] = []
        self.added: set[str] = set()  # Files that were not there at its start.


async def own_commits(checkout: dict, path: str, head: str, shared: bool, share: Share, scope):
    """The task's own commits since its base and the files each changed: those on its branch's
    own line (not ones a merge brought in from where its branch comes from), made since it was
    taken up (not another task's its branch fast-forwarded onto, nor ones pulled); in a folder
    of the owner's, only those touching files its edits wrote (the owner's own commits are
    theirs)."""
    try:
        listed = await checkout_git(
            checkout, path, "-c", "core.quotePath=false", "log", "--first-parent",
            "--format=%x00%H %ct %P", "--name-only", f"{share.base}..{head}",
        )  # fmt: skip
    except ValueError:
        return
    for entry in listed.split("\0"):
        line, _, names = entry.strip().partition("\n")
        if not line:
            continue
        sha, made, *parents = line.split()
        if len(parents) > 1:
            share.exact = False  # A merge: what it brought in is not this task's.
            continue
        if scope.since and int(made) < scope.since - 1:
            # Made before it was taken up (another task's branch it built on, a pull): there
            # already, not its work.
            share.exact = False
            continue
        files = [git_path(name) for name in names.splitlines() if name]
        if shared and not any(scope.wrote(share.top, name) for name in files):
            share.exact = False  # The owner's own commit (or one they pulled).
            continue
        share.own[sha] = files


async def tree_names(checkout: dict, top: Path, old: str, new: str) -> dict[str, str]:
    """The files that differ between two trees (or commits): name -> A, M or D."""
    listed = await checkout_git(
        checkout, str(top), "-c", "core.quotePath=false", "diff", "--name-status", "--no-renames",
        old, new,
    )  # fmt: skip
    found = {}
    for line in listed.splitlines():
        status, _, name = line.partition("\t")
        if name:
            found[git_path(name)] = status[:1]
    return found


async def checkout_top(checkout: dict) -> Path:
    """The top folder of a checkout's repository."""
    path, _head, _live = await asyncio.to_thread(locate, checkout)
    return Path((await checkout_git(checkout, path, "rev-parse", "--show-toplevel")).strip())


async def brought_in(checkout: dict, top: Path, scope: Scope, head: str) -> set[str]:
    """Files that commits made before the task was taken up brought into the checkout since
    (the owner pulled, or checked out another branch): not its work. Commits made since may be
    its own."""
    if not scope.head or not head or scope.head == head or not scope.since:
        return set()
    listed = await checkout_git(
        checkout, str(top), "-c", "core.quotePath=false", "log", "--format=%x00%ct",
        "--name-only", "--no-renames", f"{scope.head}...{head}",
    )  # fmt: skip
    older: set[str] = set()
    newer: set[str] = set()
    for block in listed.split("\0")[1:]:
        when, _, names = block.partition("\n")
        files = {git_path(name) for name in names.splitlines() if name}
        made = int(when.strip() or 0)
        (older if made < scope.since - 1 else newer).update(files)
    return older - newer


async def tree_share(
    checkout: dict, path: str, shared: bool, scope: Scope, share: Share, start: str, end: dict
) -> Share:
    """Its share between two snapshots: what changed from when it was taken up to its result
    (or now), less what was not its own: in a folder of the owner's, files other tasks' edits
    wrote; on a branch, what a merge brought in."""
    top = share.top
    uncommitted = await tree_names(checkout, top, end["commit"], end["tree"])
    # Not what running it left behind (`__pycache__/`, a `uv.lock` it wrote): files never
    # committed that only running code makes. Committed, they are its work (a PR carries them).
    window = {
        name: status
        for name, status in (await tree_names(checkout, top, start, end["tree"])).items()
        if uncommitted.get(name) != "A" or untracked_work([name])
    }
    if scope.gaps:
        # Reopened after other work: what changed only in between is not its own.
        between: set[str] = set()
        own: set[str] = set()
        edges = [start]
        for before, after, *_ in scope.gaps:
            between |= set(await tree_names(checkout, top, before, after))
            edges += [before, after]
        edges.append(end["tree"])
        for first, last in zip(edges[::2], edges[1::2], strict=True):
            own |= set(await tree_names(checkout, top, first, last))
            share.segments.append((first, last))
        window = {
            name: status for name, status in window.items() if name in own or name not in between
        }
        share.exact = share.exact and not (between & own)
        share.split = set(window) & between & own
    share.added = {name for name, status in window.items() if status == "A"}
    share.uncommitted = set(uncommitted)
    if share.base:
        await own_commits(checkout, path, end["commit"], False, share, scope)
        for _before, _after, done, again, restarted in scope.gaps:
            if not (done and again and done != again):
                continue
            # Commits made in between (another task's): not its own.
            with suppress(ValueError):
                listed = await checkout_git(
                    checkout, path, "rev-list", "--format=%H %ct", "--no-commit-header",
                    f"{done}..{again}",
                )  # fmt: skip
                pairs = listed.split()
                made = {
                    sha
                    for sha, when in zip(pairs[::2], pairs[1::2], strict=False)
                    if not restarted or int(when) <= restarted
                }
                if made & set(share.own):
                    share.own = {sha: names for sha, names in share.own.items() if sha not in made}
                    share.exact = False
        share.committed = {name for names in share.own.values() for name in names}
    if shared:
        # In a folder of the owner's, others work too (the owner, other tasks): its files are
        # those its own edits wrote or its own commands named; not what a pull brought in.
        brought = await brought_in(checkout, top, scope, end["commit"])
        keep = {
            name
            for name in window
            if scope.wrote(top, name)
            or (scope.named(top, name) and not (name in brought and name not in uncommitted))
        }
        share.not_counted = sorted(set(window) - keep)
        mixed = {sha for sha, names in share.own.items() if not set(names) & keep}
        if mixed:
            share.exact = False  # The owner's own commits (or pulled ones) in between.
            share.own = {sha: names for sha, names in share.own.items() if sha not in mixed}
            share.committed = {name for names in share.own.values() for name in names}
    elif share.exact:
        keep = set(window)
    else:
        keep = {name for name in window if name in share.committed or name in share.uncommitted}
    share.keep = sorted(keep)
    share.committed &= keep
    share.uncommitted &= keep
    share.split &= keep
    share.start, share.end, share.end_commit = start, end["tree"], end["commit"]
    share.large = [
        name
        for name in end.get("large") or []
        if name not in scope.large
        and (not shared or scope.wrote(top, name) or not scope.others_wrote(top, name))
    ]
    return share


async def scope_files(
    checkout: dict, path: str, head: str, live: bool, shared: bool, scope: Scope
) -> Share:
    """This task's share of a checkout."""
    top = Path((await checkout_git(checkout, path, "rev-parse", "--show-toplevel")).strip())
    base, moved = await scope_base(checkout, path, head, scope)
    share = Share(base, top)
    start = moved or scope.start or share.base
    end = scope.end or (await snapshot(checkout) if start else None)
    if start and end and end["tree"]:
        with suppress(ValueError):
            return await tree_share(checkout, path, shared, scope, share, start, end)
        share = Share(share.base, top)
    if share.base:
        await own_commits(checkout, path, head, shared, share, scope)
        committed = {name for names in share.own.values() for name in names}
        if shared:
            if any(not scope.wrote(top, name) for name in committed):
                share.exact = False
            committed = {name for name in committed if scope.wrote(top, name)}
        share.committed = committed
    changed = set(share.committed)
    new: list[str] = []
    if live:
        with suppress(ValueError):
            # One line per new file (not one per new folder).
            status = await checkout_git(
                checkout, path, "-c", "core.quotePath=false", "status", "--porcelain",
                "--untracked-files=all",
            )  # fmt: skip
            for line in worth_committing(status):
                name = status_name(line)
                if line.startswith("?? "):
                    new.append(name)
                else:
                    changed.add(name)
    share.keep = [
        name for name in sorted(changed) if scope.keeps(top, name, shared, share.committed)
    ]
    share.fresh = [
        name for name in untracked_work(new) if scope.keeps(top, name, shared, share.committed)
    ]
    return share


MAX_OWN_COMMITS = 200


async def own_lines(checkout: dict, share: Share, total: dict, edits_from: str = ""):
    """Lines its own commits changed (not a merged-in teammate's in the same file), then its
    edits since the last commit (from the end snapshot's commit to its files, or from HEAD to
    the folder as it is)."""
    top = str(share.top)
    # Files others changed between its stretches are counted by stretch (split_lines), their
    # committed part too: not again by commit.
    committed = share.committed - share.split
    if share.own and committed:
        shas = list(share.own)[:MAX_OWN_COMMITS]
        with suppress(ValueError):
            counted = await checkout_git(
                checkout, top, "show", "--numstat", "--format=", *shas, "--",
                *sorted(committed),
            )  # fmt: skip
            for line in counted.splitlines():
                added, removed, *_ = [*line.split("\t"), "", ""]
                if added.isdigit() and removed.isdigit():
                    total["insertions"] += int(added)
                    total["deletions"] += int(removed)
    edited = sorted(share.uncommitted - share.split) if share.end else share.keep
    against = [share.end_commit, share.end] if share.end else [edits_from] if edits_from else []
    if edited and against:
        with suppress(ValueError):
            edits = receipts.shortstat(
                await checkout_git(checkout, top, "diff", "--shortstat", *against, "--", *edited)
            )
            total["insertions"] += edits.get("insertions", 0)
            total["deletions"] += edits.get("deletions", 0)
    if share.end:
        await split_lines(checkout, share, total, share.split)


async def split_lines(checkout: dict, share: Share, total: dict, names: set[str]):
    """Lines it changed in files others changed between its stretches: each stretch's own."""
    for first, last in share.segments if names else []:
        with suppress(ValueError):
            listed = await checkout_git(
                checkout, str(share.top), "diff", "--shortstat", first, last, "--", *sorted(names)
            )
            edits = receipts.shortstat(listed)
            total["insertions"] += edits.get("insertions", 0)
            total["deletions"] += edits.get("deletions", 0)


MAX_NAMED = 50


def added_files(share: Share) -> dict:
    """Of the new files it keeps, those generated (committed: new ones it did not commit are
    not counted) and lockfiles the project did not have: counted all the same, and said."""
    added = sorted(share.added & set(share.keep))
    lockfiles = [name for name in added if LOCKFILES.search(name)]
    generated = [name for name in added if GENERATED.search(name) and name not in lockfiles]
    found: dict = {"generated": generated_paths(generated)[:MAX_NAMED]} if generated else {}
    return found | ({"lockfiles": lockfiles[:MAX_NAMED]} if lockfiles else {})


async def scoped_changes(
    checkout: dict, path: str, head: str, live: bool, shared: bool, scope: Scope
) -> dict:
    share = await scope_files(checkout, path, head, live, shared, scope)
    top, keep = str(share.top), share.keep
    total = {"files": 0, "insertions": 0, "deletions": 0, "commits": len(share.own)}
    total["uncommitted"] = 0
    if share.end:
        total |= {"files": len(keep), "uncommitted": len(share.uncommitted)}
        if share.large:
            total["large"] = len(share.large)
        if share.not_counted:
            total["not_counted"] = len(share.not_counted)
        if keep and share.exact:
            whole = [name for name in keep if name not in share.split]
            with suppress(ValueError):
                if whole:
                    span = (share.start, share.end)
                    listed = await checkout_git(
                        checkout, top, "diff", "--shortstat", *span, "--", *whole
                    )
                    total |= receipts.shortstat(listed)
                await split_lines(checkout, share, total, share.split)
                total["files"] = len(keep)
        elif keep:
            await own_lines(checkout, share, total)
        if keep:
            # A file moved is one file changed, as git and GitHub count it (not two).
            with suppress(ValueError):
                listed = await checkout_git(
                    checkout, top, "diff", "-M", "--name-status", share.start, share.end, "--",
                    *keep,
                )  # fmt: skip
                total["files"] -= sum(1 for line in listed.splitlines() if line[:1] == "R")
        total["files"] += len(share.large)
        total["uncommitted"] += len(share.large)
        return total | added_files(share)
    if share.exact and keep and (share.base or live):
        against = share.base if live else f"{share.base}..{head}"
        with suppress(ValueError):
            diffed = await checkout_git(
                checkout, top, "diff", "--shortstat", against or "HEAD", "--", *keep
            )
            total |= receipts.shortstat(diffed)
    elif keep:
        total["files"] = len(keep)
        await own_lines(checkout, share, total, "HEAD" if live else "")
    if live:
        total["uncommitted"] = len([name for name in keep if name not in share.committed]) + len(
            share.fresh
        )
        total["files"] += len(share.fresh)
        total["insertions"] += await asyncio.to_thread(count_lines, share.top, share.fresh)
        if large := await asyncio.to_thread(too_large_to_count, share.top, share.fresh):
            total["large"] = large
    return total


def too_large_to_count(root: Path, names: list[str]) -> int:
    """How many of these new files are too large to count lines in."""
    found = 0
    for name in names[:MAX_NEW_FILES]:
        with suppress(OSError):
            file = root / name
            if file.is_file() and not file.is_symlink() and file.stat().st_size > MAX_NEW_FILE:
                found += 1
    return found


def count_lines(root: Path, names: list[str]) -> int:
    """The lines of new (untracked) text files, as git would count them added."""
    lines = 0
    for name in names[:MAX_NEW_FILES]:
        with suppress(OSError):
            file = root / name
            if file.is_symlink() or not file.is_file() or file.stat().st_size > MAX_NEW_FILE:
                continue
            data = file.read_bytes()
            if b"\0" not in data[:8000]:
                lines += data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    return lines


MAX_DIFF = 2_000_000
MAX_NEW_FILES = 100
MAX_NEW_FILE = 200_000
# The same diff whatever the owner's git config says (prefixes, colors, external tools).
PLAIN_DIFF = (
    "-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff", "--no-textconv",
    "--src-prefix=a/", "--dst-prefix=b/",
)  # fmt: skip
PLAIN_SHOW = (
    "-c", "core.quotePath=false", "show", "--format=", "--no-color", "--no-ext-diff",
    "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/",
)  # fmt: skip


def new_file_diff(top: Path, name: str) -> str:
    """A diff that adds an untracked file, like git's own for a new file."""
    header = f"diff --git a/{name} b/{name}\nnew file mode 100644\n"
    try:
        path = top / name
        if path.is_symlink() or not path.is_file():
            return ""
        data = path.read_bytes()[: MAX_NEW_FILE + 1]
    except OSError:
        return ""
    if b"\0" in data[:8000]:
        return f"{header}Binary files /dev/null and b/{name} differ\n"
    lines = data[:MAX_NEW_FILE].decode(errors="replace").splitlines() or [""]
    body = "".join(f"+{line}\n" for line in lines)
    return f"{header}--- /dev/null\n+++ b/{name}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


async def diff(checkout: dict, scope: Scope | None = None) -> dict:
    """The full diff of a worker's work in one checkout, from where it started: commits,
    uncommitted edits and new files (for a shared project folder: its uncommitted edits). With a
    `scope`, one task's share of it (see Scope)."""
    path, head, live = await asyncio.to_thread(locate, checkout)
    top = (await checkout_git(checkout, path, "rev-parse", "--show-toplevel")).strip()
    if scope is not None:
        share = await scope_files(checkout, path, head, live, bool(checkout.get("shared")), scope)
        text = ""
        if share.end and share.keep:
            if share.exact:
                whole = [name for name in share.keep if name not in share.split]
                if whole:
                    text = await checkout_git(
                        checkout, top, *PLAIN_DIFF, share.start, share.end, "--", *whole, limit=60
                    )
                text += await split_diff(checkout, share, share.split)
            else:
                if share.own and share.committed - share.split:
                    shas = list(reversed(list(share.own)[:MAX_OWN_COMMITS]))
                    text = await checkout_git(
                        checkout, top, *PLAIN_SHOW, *shas, "--",
                        *sorted(share.committed - share.split), limit=60,
                    )  # fmt: skip
                if share.uncommitted - share.split:
                    text += await checkout_git(
                        checkout, top, *PLAIN_DIFF, share.end_commit, share.end, "--",
                        *sorted(share.uncommitted - share.split), limit=60,
                    )  # fmt: skip
                text += await split_diff(checkout, share, share.split)
        elif share.exact and share.keep:
            against = (share.base or "HEAD") if live else f"{share.base}...{head}"
            text = await checkout_git(
                checkout, top, *PLAIN_DIFF, against, "--", *share.keep, limit=60
            )
        elif share.keep:
            # Its commits' own patches, oldest first, then its edits since the last commit.
            if share.own and share.committed:
                shas = list(reversed(list(share.own)[:MAX_OWN_COMMITS]))
                text = await checkout_git(
                    checkout, top, *PLAIN_SHOW, *shas, "--", *sorted(share.committed), limit=60
                )
            if live:
                text += await checkout_git(
                    checkout, top, *PLAIN_DIFF, "HEAD", "--", *share.keep, limit=60
                )
        for name in share.fresh[:MAX_NEW_FILES]:
            text += await asyncio.to_thread(new_file_diff, share.top, name)
        large = await asyncio.to_thread(file_sizes, share.top, share.large)
    else:
        large = []
        base = base_of(checkout) or "HEAD"
        if live:
            text = await checkout_git(checkout, top, *PLAIN_DIFF, base, limit=60)
            others = await checkout_git(
                checkout, top, "ls-files", "--others", "--exclude-standard", "-z"
            )
            names = [name for name in others.split("\0") if name][:MAX_NEW_FILES]
            for name in names:
                text += await asyncio.to_thread(new_file_diff, Path(top), name)
        else:
            text = await checkout_git(checkout, top, *PLAIN_DIFF, f"{base}...{head}", limit=60)
    return {
        "name": Path(checkout.get("repo") or top).name,
        "path": checkout["path"],
        "branch": checkout.get("branch", ""),
        "diff": text[:MAX_DIFF],
        "truncated": len(text) > MAX_DIFF,
        # Its new files too large to show (left out of the diff): by name and size.
        "large": large,
    }


async def split_diff(checkout: dict, share: Share, names: set[str]) -> str:
    """The diff of files others changed between its stretches: each stretch's own part."""
    text = ""
    for first, last in share.segments if names else []:
        text += await checkout_git(
            checkout, str(share.top), *PLAIN_DIFF, first, last, "--", *sorted(names), limit=60
        )
    return text


def file_sizes(top: Path, names: list[str]) -> list[dict]:
    """These files by name and size in bytes (0 when it cannot be read)."""
    found = []
    for name in names[:MAX_NEW_FILES]:
        size = 0
        with suppress(OSError):
            size = (top / name).stat().st_size
        found.append({"name": name, "size": size})
    return found


# Ignored folders and files that a build or an install makes again (build output, dependencies,
# caches), by name anywhere in a worktree: no reason to keep a worktree, and deleted to free
# space from one that stays. Not logs (`logs/`, `*.log`): the owner decides about those.
BUILD_OUTPUT = {
    *SKIPPED, ".venv", ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".hypothesis", ".cache", ".next", ".nuxt", ".turbo", ".parcel-cache", ".gradle", ".kotlin",
    ".cxx", ".externalNativeBuild", ".dart_tool", ".angular", ".svelte-kit", ".expo", ".output",
    ".vite", ".webpack", ".docusaurus", "storybook-static", ".nyc_output", ".jest-cache",
    ".eslintcache", ".stylelintcache", ".ipynb_checkpoints", ".sass-cache", "elm-stuff",
    ".stack-work", "_build", "DerivedData", ".build", "Pods", "CMakeFiles", ".terraform",
    "__generated__", "coverage", "htmlcov", ".coverage", "out-tsc",
}  # fmt: skip
BUILD_PATTERNS = ("*.egg-info", "*.pyc", "*.pyo", "cmake-build-*", "bazel-*")
# What tools keep to go faster next time (test, type-check and lint caches, coverage, bytecode),
# not what a worker's next run needs installed or built: all that is cleaned from a worker that
# still follows a task (a PR's checks, a run under way).
CACHES = {
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis", ".cache", ".parcel-cache",
    ".jest-cache", ".eslintcache", ".stylelintcache", ".nyc_output", ".ipynb_checkpoints",
    ".sass-cache", ".turbo", "__pycache__", "coverage", "htmlcov", ".coverage",
}  # fmt: skip
CACHE_PATTERNS = ("*.pyc", "*.pyo")
# Editor settings and Finder's notes: no reason to keep a worktree, nor worth deleting from one
# that stays.
EDITOR_FILES = {".idea", ".vscode", ".DS_Store"}
REGENERABLE = BUILD_OUTPUT | EDITOR_FILES
# At most this many ignored entries and changed files are recorded for a worktree that stays.
MAX_KEPT_LISTED = 50
# A build folder is looked through for files the owner never wants cleaned up to this many
# entries; one bigger is kept whole.
MAX_LOOKED_INSIDE = 500_000
# Where a folder being deleted is moved first, in Chief of Staff's workspaces folder (never in a
# worktree): a turn that starts meanwhile finds it whole or gone, never half deleted. A name only
# Chief of Staff uses (the workspaces folder may be one the owner chose); what an earlier version
# called it, of which only its own entries are taken; and the names to_trash gives those.
TRASH = ".chiefofstaff-trash"
OLD_TRASH = ".trash"
TRASHED = re.compile(r"^[0-9a-f]{12}-")
# How long git may take to remove a worktree (a huge one, a slow disk).
REMOVE_SECONDS = 900


def ignored_kind(entry: str, extra: Sequence[str] = (), never: Sequence[str] = ()) -> str:
    """What an ignored file or folder of a worktree is, by its path there: "never" (one the owner
    keeps, `never`), "build" (a build or an install makes it again; `extra` names more), "editor"
    (editor settings), or "" (unknown: it may matter)."""
    path = entry.rstrip("/")
    parts = path.split("/")

    def matches(patterns: Sequence[str]) -> bool:
        return any(
            fnmatch.fnmatchcase(name, pattern) for pattern in patterns for name in (path, *parts)
        )

    if matches(never):
        return "never"
    if any(part in BUILD_OUTPUT for part in parts) or matches(BUILD_PATTERNS) or matches(extra):
        return "build"
    return "editor" if any(part in EDITOR_FILES for part in parts) else ""


def holding_never(
    tree: str, folders: list[str], never: Sequence[str], limit: int = MAX_LOOKED_INSIDE
) -> set[str]:
    """Those of these ignored folders of a worktree that hold something the owner never wants
    cleaned (a never-clean pattern matches a name or a path in them; git lists a folder it
    ignores whole as one entry), or that are too big to look through: kept whole. In a thread;
    links in them are not followed (git lists a link itself as a file, never as a folder)."""
    found = set()
    root = Path(tree)
    for folder in folders:
        top = root / folder.rstrip("/")
        seen = 0
        for path, dirs, files in os.walk(top):
            relative = Path(path).relative_to(root).as_posix()
            names = [*dirs, *files]
            seen += len(names)
            if seen > limit or any(
                fnmatch.fnmatchcase(name, pattern)
                or fnmatch.fnmatchcase(f"{relative}/{name}", pattern)
                for name in names
                for pattern in never
            ):
                found.add(folder)
                break
    return found


def is_cache(entry: str) -> bool:
    """Whether an ignored entry a build makes again is only a tool's cache (see CACHES)."""
    parts = entry.rstrip("/").split("/")
    return any(
        part in CACHES or any(fnmatch.fnmatchcase(part, p) for p in CACHE_PATTERNS)
        for part in parts
    )


async def ignored_listing(checkout: dict, tree: str) -> list[str] | None:
    """Every ignored file and folder of a worktree (or clone) as git lists them, sorted, before
    any is sorted out; None when git cannot list them."""
    try:
        listed = await checkout_git(
            checkout, tree, "ls-files", "--others", "--ignored", "--exclude-standard",
            "--directory", "-z", limit=120,
        )  # fmt: skip
    except ValueError:
        return None
    return sorted(set(filter(None, listed.split("\0"))))


async def sort_ignored(
    checkout: dict,
    tree: str,
    extra: Sequence[str] = (),
    never: Sequence[str] = (),
    look_inside: bool = True,
    listing: list[str] | None = None,
) -> tuple[list[str], list[str]] | None:
    """The ignored files of a worker's worktree (or clone), as git lists them (`listing`, when
    read already: see ignored_listing): those a build or an install makes again (safe to
    delete), and those that may matter (git would delete them with the worktree): anything else,
    copied-in files it changed (an .env), and a build folder with something in it the owner
    never wants cleaned (`look_inside`: looked for, in a thread). Never a tracked file, nor a new
    one git does not ignore. None when git cannot list them."""
    entries = listing if listing is not None else await ignored_listing(checkout, tree)
    if entries is None:
        return None
    # git also lists a new folder that holds only ignored files ("pkg/"), with what is in it
    # ("pkg/__pycache__/"): what is in it counts, not the folder.
    entries = [
        entry
        for at, entry in enumerate(entries)
        if not (entry.endswith("/") and at + 1 < len(entries) and entries[at + 1].startswith(entry))
    ]
    carried = set(checkout.get("carried") or [])
    cleanable, kept = [], []
    for entry in entries:
        kind = ignored_kind(entry, extra, never)
        if kind == "build":
            cleanable.append(entry)
        elif kind == "editor" or (
            not kind
            and checkout.get("worktree")
            and await asyncio.to_thread(
                only_carried, Path(tree), entry, carried, Path(checkout["repo"])
            )
        ):
            continue
        else:
            kept.append(entry)
    folders = [entry for entry in cleanable if entry.endswith("/")]
    if never and look_inside and folders:
        held = await asyncio.to_thread(holding_never, tree, folders, never)
        cleanable = [entry for entry in cleanable if entry not in held]
        kept = sorted([*kept, *held])
    return cleanable, kept


# git status of a worktree whatever its config hides: new files (status.showUntrackedFiles=no)
# and changed submodules (diff.ignoreSubmodules, submodule.*.ignore).
ALL_STATUS = ("status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=none")
# For git's own check before it removes a worktree (a `git status` it runs without -u).
SHOW_UNTRACKED = ("-c", "status.showUntrackedFiles=normal")


async def status_lines(checkout: dict, tree: str) -> list[str] | None:
    """A worktree's `git status --porcelain` lines, all of them (see ALL_STATUS); None when git
    cannot tell."""
    try:
        status = await checkout_git(checkout, tree, *ALL_STATUS, limit=REMOVE_SECONDS)
    except ValueError:
        return None
    return [line for line in status.splitlines() if line.strip()]


async def uncommitted(checkout: dict, tree: str) -> list[str] | None:
    """The files git would refuse to lose with a worktree: changed ones, and new ones it does not
    ignore. None when git cannot tell (then nothing may be taken for clean)."""
    lines = await status_lines(checkout, tree)
    return None if lines is None else [status_name(line) for line in lines]


def trash_of(folder: Path | None, roots: list[Path]) -> Path | None:
    """Where folders deleted from a worker's worktrees go first: TRASH in the workspaces folder
    the worker's own folder is in (never in a repository: see Settings.validate_paths)."""
    if not folder:
        return None
    for root in roots:
        real = Path(os.path.realpath(root))
        if folder.is_relative_to(real) and folder != real:
            return real / TRASH
    return None


def move_ignored(tree: str, entry: str, trash: Path) -> Path | bool:
    """Take one ignored file or folder out of a worktree (in a thread): a file or a link is
    removed (a link is never followed); a folder is moved into `trash` in one step (on the same
    disk only), to be deleted from there. Only inside the worktree, never through a linked folder
    that leads out of it, never a repository of its own (its commits may be nowhere else). The
    folder's place in trash, True for a file removed, False for nothing done."""
    root = Path(tree).resolve()
    relative = entry.rstrip("/")
    if not relative or ".." in relative.split("/"):
        return False
    path = root / relative
    if not path.parent.resolve().is_relative_to(root):
        return False
    try:
        if path.is_symlink() or not path.is_dir():
            path.unlink()
            return True
        if (path / ".git").exists() or (path / ".git").is_symlink():
            return False
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Could not delete %s: %s", path, exc)
        return False
    return to_trash(path, trash) or False


def to_trash(path: Path, trash: Path) -> Path | None:
    """Move a folder into trash in one step (in a thread): only on the same disk, where that is
    one rename. Its place there, or None (gone already, another disk, an error)."""
    try:
        trash.mkdir(exist_ok=True)
        if trash.stat().st_dev != path.lstat().st_dev:
            logger.warning("Not deleting %s: it is on another disk than %s.", path, trash)
            return None
        target = trash / f"{uuid4().hex[:12]}-{path.name}"
        path.rename(target)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Could not delete %s: %s", path, exc)
        return None
    DELETING.add(target)  # Its own cleanup deletes it: not a leftover for another to take.
    return target


# What is in trash because a cleanup running now moved it there (until it is deleted).
DELETING: set[Path] = set()


# Folders writable() is deleting again: each only once (never a loop).
RETRYING: set[str] = set()


def writable(function: Callable, path: str, _error: object):
    """rmtree's handler: what it could not delete or open because it or its folder is read-only
    or unreadable (Go's module cache, a tool's locked cache, a folder at mode 000) is made
    writable and readable and tried once more: a file or an empty folder is removed again, a
    folder it could not open or list is deleted with all in it. Never raises (that would stop
    the whole delete): one owned by another user stays, and what is left is measured after."""
    target = Path(path)
    try:
        for each in {target.parent, target}:
            if not each.is_symlink():
                each.chmod(each.lstat().st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        if function in {os.unlink, os.remove, os.rmdir}:
            function(path)
        elif path not in RETRYING and target.is_dir() and not target.is_symlink():
            RETRYING.add(path)
            try:
                rmtree_writable(target)
            finally:
                RETRYING.discard(path)
    except Exception as exc:
        logger.debug("Could not delete %s: %s", path, exc)


def rmtree_writable(folder: Path):
    """shutil.rmtree with writable() for what it cannot delete at first."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(folder, onexc=writable)
    else:
        shutil.rmtree(folder, onerror=writable)


def delete_moved(item: Path) -> bool:
    """Delete a file or folder moved into trash (in a thread): whether it is all gone."""
    try:
        if item.is_symlink() or not item.is_dir():
            item.unlink(missing_ok=True)
        else:
            rmtree_writable(item)
    except OSError as exc:
        logger.warning("Could not delete all of %s: %s", item, exc)
    finally:
        DELETING.discard(item)
    if gone := not (item.exists() or item.is_symlink()):
        return gone
    logger.warning("Could not delete all of %s (owned by another user?).", item)
    return False


def leftovers(trash: Path) -> list[Path]:
    """What a cleanup left in trash: Chief of Staff stopped while deleting it, or it could not
    delete all of it. Only what to_trash put there (anything else in a folder of that name is
    not Chief of Staff's), not what a cleanup running now moved there."""
    with suppress(OSError):
        return [
            item
            for item in sorted(trash.iterdir())
            if TRASHED.match(item.name) and item not in DELETING
        ]
    return []


async def clean_tree(
    tree: str, entries: list[str], trash: Path, stop: Callable[[], str] | None = None
) -> tuple[list[str], int]:
    """Delete these ignored entries of a worktree: those deleted and the bytes they took
    (measured with du just before). Each folder is first moved out into trash in one step, all
    of them before any is deleted (in a thread, one at a time): a turn that starts meanwhile
    finds each whole or gone. `stop` says why to stop before the next one (its worker started a
    turn), or "" to go on."""
    paths = {entry: str(Path(tree, entry.rstrip("/"))) for entry in entries}
    sizes = await machine.disk_used(list(paths.values())) or {}
    removed, freed = [], 0
    moved: list[tuple[Path, int]] = []
    left: list[tuple[Path, int]] = []
    try:
        for entry in entries:
            if stop and (why := stop()):
                logger.info("Stopped cleaning %s: %s.", tree, why)
                break
            if done := await asyncio.to_thread(move_ignored, tree, entry, trash):
                removed.append(entry)
                freed += sizes.get(paths[entry], 0)
                if isinstance(done, Path):
                    moved.append((done, sizes.get(paths[entry], 0)))
        left = [
            (item, size) for item, size in moved if not await asyncio.to_thread(delete_moved, item)
        ]
    finally:
        DELETING.difference_update(item for item, _ in moved)  # Stopped: leftovers now.
    if left:
        # Only what went counts: what could not be deleted is measured (a leftover the Storage
        # view shows; see Storage.empty_leftovers).
        remaining = await machine.disk_used([str(item) for item, _ in left]) or {}
        freed -= sum(min(size, remaining.get(str(item), size)) for item, size in left)
    return removed, freed


def only_carried(tree: Path, entry: str, carried: set[str], repo: Path) -> bool:
    """Whether an ignored file (or folder, as git lists one that holds only ignored files) is
    nothing but copies made when the worktree was, unchanged."""
    path = tree / entry
    if not entry.endswith("/"):
        return entry in carried and same_file(path, repo / entry)
    names = [
        str(Path(folder, name).relative_to(tree))
        for folder, _, files in itertools.islice(os.walk(path), 200)
        for name in files
    ]
    return bool(names) and all(
        name in carried and same_file(tree / name, repo / name) for name in names
    )


def same_file(one: Path, other: Path) -> bool:
    try:
        return one.read_bytes() == other.read_bytes()
    except OSError:
        return False


# Package stores whose contents the tools check against the project's lockfile before use
# (npm's content-addressed store, Yarn Berry's checksummed archives): a worker writing there
# cannot change what the owner's builds run. Not npm's _npx or Yarn classic's unpacked folders,
# which run as they are. Copilot itself shares pip's, uv's and poetry's (and, in a Node project,
# npm's whole folder). Caches whose contents run as they are (Gradle and Maven jars, Gradle
# distributions, pre-commit hooks, shiv apps) are never shared; Gradle's dependency cache is
# read, not written (see prompts.worker_context).
CACHE_FOLDERS = (".npm/_cacache", ".yarn/berry/cache")
# Files that let a program talk to the owner's local services as the owner: the tokens of their
# Gradle daemons and Jupyter servers. Unreadable for workers that may use localhost.
SERVICE_SECRETS = (".gradle/daemon", "Library/Jupyter/runtime", ".local/share/jupyter/runtime")


def shared_caches(home: Path | None = None) -> list[str]:
    """The owner's verified package caches that exist on this machine."""
    home = home or Path.home()
    return [str(home / name) for name in CACHE_FOLDERS if (home / name).is_dir()]


def service_secrets(home: Path | None = None) -> list[str]:
    """Denied whether they exist yet or not (Copilot then makes an empty folder): a daemon the
    owner starts later must not be reachable either."""
    home = home or Path.home()
    found = [str(home / name) for name in SERVICE_SECRETS]
    if gradle_home := os.environ.get("GRADLE_USER_HOME"):
        found.append(str(Path(gradle_home).expanduser() / "daemon"))
    return found


def too_broad(path: str) -> bool:
    """The filesystem root, home, or a folder above home."""
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, ValueError):
        return True
    return resolved == Path(resolved.anchor) or Path.home().resolve().is_relative_to(resolved)


def folder_of(workspace: Path, roots: list[Path]) -> Path | None:
    """A worker's own folder (its real path) when it is in one of Chief of Staff's roots for
    them (not a root itself): the only kind it measures and cleans. None for any other, like the
    folder of a session continued from a terminal."""
    folder = Path(os.path.realpath(workspace))
    for root in roots:
        real = Path(os.path.realpath(root))
        if folder != real and folder.is_relative_to(real):
            return folder
    return None


def own_trees(folder: Path | None, checkouts: list[dict]) -> list[tuple[dict, str]]:
    """A worker's worktrees and clones in its own folder (`folder`, see folder_of) that are on
    disk: never a project folder of the owner's it works in."""
    if not folder:
        return []
    found = []
    for checkout in checkouts:
        tree = checkout.get("worktree") or (checkout.get("remote") and checkout.get("repo"))
        if checkout.get("shared") or not tree or not os.path.isdir(tree):  # noqa: PTH112
            continue
        if Path(os.path.realpath(tree)).is_relative_to(folder):
            found.append((checkout, tree))
    return found


def overlapping(tree: str, others: list[tuple[str, list[str]]]) -> str:
    """The first of others (name, the folders it works in) that works in this one: it, or a
    folder in it (not one around it, like a session continued from a terminal in the code
    folder)."""
    real = Path(os.path.realpath(tree))
    for name, paths in others:
        if any(Path(os.path.realpath(path)).is_relative_to(real) for path in paths if path):
            return name
    return ""


class Workspaces:
    def __init__(self, store: Store, work_root: Path):
        self.store = store
        self.work_root = work_root
        self.project_cache: dict[str, tuple[float, dict]] = {}
        self.known: tuple[float, tuple, list[str]] = (0.0, (), [])
        self.clone_lock = asyncio.Lock()
        # The token of the gh account that can see a repository ("" for gh's active one).
        self.github_token: Callable[[str], Awaitable[str]] | None = None
        # Told what release() removed and kept (with why), for the Storage view; and asked why
        # a closed worker's worktrees must stay as they are ("" when they need not).
        self.on_release: Callable[[dict, list[str], list[dict]], None] | None = None
        self.holds: Callable[[dict], str] | None = None
        # The folders programs run in, read once for a whole cleanup pass (see storage.py).
        self.known_cwds: set[str] | None = None

    async def gh_clone(self, repo: str, destination: Path):
        token = await self.github_token(repo) if self.github_token else ""
        await command(
            "gh", "repo", "clone", repo, str(destination), limit=900,
            env={"GH_TOKEN": token} if token else None,
        )  # fmt: skip

    def projects(self) -> list[str]:
        """Configured projects, then the repositories in the code folders (looked up again
        every half minute, so repositories cloned or deleted meanwhile are noticed)."""
        settings = self.store.settings()
        key = (tuple(settings.projects), settings.code_folder)
        found_at, known_key, known = self.known
        if known_key == key and time.monotonic() - found_at < 30:
            return known
        found = list(settings.projects)
        skip = {self.store.directory.resolve(), self.scratch_root().resolve()}
        if settings.code_folder:
            for repo in find_repositories(Path(settings.code_folder), skip):
                if not any(within(repo, project) for project in found):
                    found.append(repo)
        self.known = (time.monotonic(), key, found)
        return found

    def forget_projects(self):
        self.known = (0.0, (), [])

    async def local_copy(self, repo: str) -> Path | None:
        """The owner's copy of a repository (a git URL or GitHub owner/name): a project whose
        origin it is, else a fresh clone in the code folder. None when there is neither."""
        url, name = clone_source(repo)
        wanted = repository_key(url)
        for card in await self.project_cards():
            if card["remote"] and repository_key(card["remote"]) == wanted:
                return Path(card["path"])
        folder = self.store.settings().code_folder
        if not folder:
            return None
        async with self.clone_lock:  # Two workers asking for the same repository clone it once.
            self.forget_projects()
            for card in await self.project_cards():
                if card["remote"] and repository_key(card["remote"]) == wanted:
                    return Path(card["path"])
            destination = free_child(Path(folder), name)
            # Hidden while it downloads, so a half clone is never taken for a project.
            partial = destination.with_name(f".{destination.name}.cloning")
            await asyncio.to_thread(shutil.rmtree, partial, True)
            try:
                if GITHUB_NAME.fullmatch(repo) and shutil.which("gh"):
                    await self.gh_clone(repo, partial)
                else:
                    await git("clone", "--quiet", url, str(partial), limit=900)
                await asyncio.to_thread(partial.rename, destination)
            finally:
                await asyncio.to_thread(shutil.rmtree, partial, True)
            self.forget_projects()
            return destination

    def scratch_root(self) -> Path:
        """Where each worker's own folder is created: .chief/ in the code folder by default."""
        settings = self.store.settings()
        if settings.scratch_directory:
            return Path(settings.scratch_directory)
        if settings.code_folder:
            return Path(settings.code_folder) / TASK_FOLDERS
        return self.work_root / "workspaces"

    async def release(self, row: dict) -> list[str]:
        """A closed worker's worktrees, removed when they hold nothing uncommitted and no ignored
        file that may matter.

        The branches stay in their repositories, and restore() brings the worktrees back when
        the worker is reopened. A worktree with changes stays (git status says which; git
        refuses to remove one anyway), as does one with ignored files no build makes again (git
        would delete them with it), and one a program runs in or another open worker uses: it
        is looked at again later. From one that stays, what a build or an install makes again
        goes (often most of its size); why it stays is recorded (on_release).
        """
        settings = self.store.settings()
        removed: list[str] = []
        kept: list[dict] = []
        cwds: set[str] | None = None
        looked = False
        hold = self.holds(row) if self.holds else ""
        roots = self.roots()
        folder = await asyncio.to_thread(folder_of, self.workspace(row), roots)
        # Only in Chief of Staff's own folders is anything deleted in place.
        ours = {tree for _, tree in await asyncio.to_thread(own_trees, folder, self.checkouts(row))}
        trash = trash_of(folder, roots)
        others = self.others(row)
        for checkout in self.checkouts(row):
            tree = checkout.get("worktree")
            if not tree or not await asyncio.to_thread(os.path.isdir, tree):
                continue
            stays = {"path": tree, "repo": checkout["repo"], "branch": checkout.get("branch") or "",
                     "items": [], "changes": [], "cleaned": [], "freed": 0}  # fmt: skip
            held = hold
            if not held:
                if not looked:
                    cwds = self.known_cwds
                    cwds, looked = cwds if cwds is not None else await machine.process_cwds(), True
                if cwds is None:
                    held = "which programs run in it could not be read"
                elif machine.in_use(tree, cwds):
                    held = "a program runs in it"
                elif user := await asyncio.to_thread(overlapping, tree, others):
                    held = f"“{user}” uses it"
            if held:
                # As it is, until it is looked at again (see Storage.unreleased).
                kept.append({**stays, "held": held, "in_use": True})
                continue
            found = await sort_ignored(
                checkout, tree, settings.regenerable_patterns, settings.never_clean_patterns
            )
            cleanable, unknown = found or ([], [])
            cleanable = cleanable if tree in ours and trash else []
            changes = await uncommitted(checkout, tree)
            if found is not None and not unknown and changes == [] and tree in ours and trash:
                # Its build output goes first, without a time limit (git's removal has one),
                # even with automatic cleanup off: removing the worktree deletes it anyway.
                if cleanable:
                    stays["cleaned"], stays["freed"] = await clean_tree(tree, cleanable, trash)
                    cleanable = []
                if await self.remove_tree(checkout, tree, trash):
                    removed.append(tree)
                    continue
                changes = await uncommitted(checkout, tree)
            elif found is not None and not unknown and changes == []:
                # Not in Chief of Staff's folders: git removes it, or it stays.
                with suppress(ValueError):
                    await checkout_git(
                        checkout, checkout["repo"], "worktree", "remove", tree,
                        limit=REMOVE_SECONDS,
                    )  # fmt: skip
                    removed.append(tree)
                    continue
            stays |= {
                "items": unknown[:MAX_KEPT_LISTED],
                "changes": (changes or [])[:MAX_KEPT_LISTED],
                "changes_unread": changes is None,
                "unlisted": found is None,
            }
            if cleanable and not settings.auto_cleanup:
                stays["pending"] = True  # Cleaned if the owner turns automatic cleanup on.
            elif cleanable and trash:
                stays["cleaned"], stays["freed"] = await clean_tree(tree, cleanable, trash)
            kept.append(stays)
            why = [
                *(["it has uncommitted changes"] if stays["changes"] else []),
                *(["its changes could not be read"] if changes is None else []),
                *(["it has ignored files: " + ", ".join(unknown[:5])] if unknown else []),
                *(["its ignored files could not be listed"] if found is None else []),
            ]
            logger.info(
                "Kept %s: %s. Deleted %d build and cache folders in it (%s).",
                tree,
                "; ".join(why) or "git would not remove it",
                len(stays["cleaned"]),
                machine.size_text(stays["freed"]),
            )
        with suppress(OSError):
            # Only when nothing else is in it, like a worker's notes.
            await asyncio.to_thread(self.workspace(row).rmdir)
        if self.on_release:
            self.on_release(row, removed, kept)
        return removed

    async def remove_tree(self, checkout: dict, tree: str, trash: Path) -> bool:
        """Remove a worktree git status found clean (nothing uncommitted, no ignored file that
        may matter), by git. git stopped halfway (a huge tree, a slow disk) is finished, not
        taken for changes: what is left goes too, out of its place in one step, and git forgets
        it, so a reopened worker gets it back whole from its branch. False when git refuses
        (changes made meanwhile)."""
        repo = checkout["repo"]
        try:
            await checkout_git(
                checkout, repo, *SHOW_UNTRACKED, "worktree", "remove", tree, limit=REMOVE_SECONDS
            )
            return True
        except TimedOutError:
            logger.warning("Removing %s took too long: finishing it.", tree)
        except ValueError:
            return False
        # Only what git itself deleted may be missing: a file that appeared or changed since
        # it was found clean is kept (the worktree stays, half removed, for the owner to see).
        lines = await status_lines(checkout, tree)
        if lines is None or any(not line.startswith(" D ") for line in lines):
            logger.warning("Not finishing the removal of %s: files changed meanwhile.", tree)
            return False
        return await self.force_remove(checkout, tree, trash)

    @staticmethod
    async def force_remove(checkout: dict, tree: str, trash: Path) -> bool:
        """Remove a worktree with whatever is in it (`git worktree remove --force`). What git
        leaves (stopped for taking too long) goes out of its place in one step and is deleted
        from trash, and git forgets the worktree: never a half tree for a reopened worker to
        resume in (restore() makes it again from its branch)."""
        repo = checkout["repo"]
        with suppress(ValueError):
            await checkout_git(
                checkout, repo, "worktree", "remove", "--force", tree, limit=REMOVE_SECONDS
            )
        if moved := await asyncio.to_thread(to_trash, Path(tree), trash):
            await asyncio.to_thread(delete_moved, moved)
        with suppress(ValueError):
            await checkout_git(checkout, repo, "worktree", "prune")
        return not await asyncio.to_thread(os.path.lexists, tree)

    def others(self, row: dict) -> list[tuple[str, list[str]]]:
        """The other open workers, each with the folders it works in (its own, its checkouts)."""
        found = []
        for other in self.store.open_sessions(WORKER):
            if other["id"] != row["id"]:
                paths = [str(self.workspace(other)), other["cwd"]]
                for checkout in self.checkouts(other):
                    paths += [checkout.get(key) or "" for key in ("path", "worktree", "repo")]
                found.append((other["title"], paths))
        return found

    async def restore(self, row: dict):
        """The task folder and worktrees release() removed, back on their branches."""
        workspace = self.workspace(row)
        if row["cwd"] == str(workspace):  # A worker without a repository works in it directly.
            await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        for checkout in self.checkouts(row):
            tree = checkout.get("worktree")
            if not tree or await asyncio.to_thread(os.path.exists, tree):
                continue
            repo = checkout["repo"]
            await git("-C", repo, "worktree", "prune")
            try:
                await git("-C", repo, "worktree", "add", tree, checkout["branch"])
            except ValueError:
                # The branch is gone (merged and deleted): start it again where it started.
                await git(
                    "-C", repo, "worktree", "add", "-b", checkout["branch"], tree,
                    checkout.get("base_ref") or "HEAD",
                )  # fmt: skip
            if not row["slack"]:
                await carry_ignored_files(Path(repo), Path(tree))

    def roots(self) -> list[Path]:
        """The folders Chief of Staff makes workers' own folders in: the configured one, and
        those it used by default."""
        found = [self.scratch_root(), self.work_root / "workspaces"]
        if code := self.store.settings().code_folder:
            found.append(Path(code) / TASK_FOLDERS)
        return list(dict.fromkeys(found))

    def own_folder(self, row: dict) -> Path | None:
        """A worker's own folder when Chief of Staff made it (see folder_of)."""
        return folder_of(self.workspace(row), self.roots())

    def workspace(self, row: dict) -> Path:
        if stored := self.store.get(f"workspace:{row['id']}"):
            return Path(stored)
        return self.scratch_root() / f"{slugify(row['title'])}-{row['id'][:6]}"

    def checkouts(self, row: dict) -> list[dict]:
        """The repositories a worker works in: worktrees, clones, or shared project folders."""
        if repos := json.loads(row.get("repos") or "[]"):
            return repos
        if row["role"] == WORKER and row.get("project"):
            return [{"project": row["project"], "path": row["cwd"], "branch": row["branch"]}]
        return []

    async def own_clones(self, row: dict) -> list[dict]:
        """Repositories a worker cloned itself into its folder (`git clone` in its shell), and
        worktrees it made there (`git worktree add`, see own_worktree), not yet among its
        checkouts: what it does there is counted and shown like a checkout's, not lost as a
        report's. Not one whose git config would run code."""
        workspace = self.workspace(row)
        keys = ("path", "repo", "worktree")
        paths = [c[key] for c in self.checkouts(row) for key in keys if c.get(key)]

        def unknown() -> list[str]:
            if not workspace.is_dir():
                return []
            known = {os.path.realpath(path) for path in paths}
            return [
                repo
                for repo in find_repositories(workspace, set(), worktrees=True)
                if os.path.realpath(repo) not in known
                and not config_runs_code(str(Path(repo) / ".git"))
            ]

        made = []
        found = await asyncio.to_thread(unknown)
        for repo in found:
            if await asyncio.to_thread(linked_worktree, Path(repo)):
                if not (checkout := await self.own_worktree(row, repo, found)):
                    continue
            else:
                checkout = {"project": "", "repo": repo, "path": repo, "branch": "",
                            "base_ref": "", "base": "a clone it made itself",
                            "remote": repo}  # fmt: skip
            if checkout.get("remote"):
                with suppress(ValueError):
                    url = await checkout_git(checkout, repo, "remote", "get-url", "origin")
                    checkout["remote"] = url.strip() or repo
            with suppress(ValueError):
                branch = await checkout_git(checkout, repo, "rev-parse", "--abbrev-ref", "HEAD")
                checkout["branch"] = branch.strip().removesuffix("HEAD")
            for ref in ("origin/HEAD", "origin/main", "origin/master"):
                with suppress(ValueError):
                    await checkout_git(checkout, repo, "rev-parse", "--verify", "--quiet", ref)
                    checkout["base_ref"] = ref
                    break
            if not checkout["base_ref"] and checkout["repo"] != repo:
                # A worktree of a repository without an origin: from the owner's own branch.
                checkout["base_ref"] = await local_default(checkout, repo)
            made.append(checkout)
        return made

    async def own_worktree(self, row: dict, tree: str, found: list[str]) -> dict | None:
        """A linked worktree a worker made itself in its folder, as a checkout: only of a
        repository it works in or a clone it made in its folder (whose config it can write:
        checked like a clone's). Which one is read from the repository's side (admin_dir), not
        from the `.git` file the worker wrote."""
        workspace = str(self.workspace(row))
        commons = [
            checkout.get("git_dir") or str(Path(checkout["repo"]) / ".git")
            for checkout in self.checkouts(row)
            if checkout.get("git_dir") or checkout.get("repo")
        ]
        commons += [str(Path(path) / ".git") for path in found]

        def repository() -> str:
            seen = set()
            for common in commons:
                real = os.path.realpath(common)
                if real not in seen and os.path.isdir(real) and admin_dir(real, tree):  # noqa: PTH112
                    return real
                seen.add(real)
            return ""

        common = await asyncio.to_thread(repository)
        if not common:
            return None
        own = within(common, workspace)
        if own and await asyncio.to_thread(config_runs_code, common):
            return None
        checkout = {"project": "", "path": tree, "branch": "", "base_ref": "",
                    "base": "a worktree it made itself"}  # fmt: skip
        if Path(common).name == ".git":
            checkout["repo"] = str(Path(common).parent)
        else:
            checkout |= {"repo": common, "git_dir": common}
        if own:
            checkout["remote"] = checkout["repo"]
        return checkout

    def writes(self, row: dict) -> list[str]:
        """Where a worker may write (its edits and, sandboxed, its commands): its folders, but
        not a git folder of the owner's it was given only to read (a task that changes no code,
        see make_checkout). Their checkouts are never touched: an install or a test run there
        would leave .venv, a lockfile or caches behind. Its own worktree is for those. One the
        owner asked it to change in place (spawn_worker in_place) is its to write."""
        kept = {
            os.path.realpath(checkout["path"])
            for checkout in self.checkouts(row)
            if checkout.get("shared") and checkout.get("git_dir") and not checkout.get("in_place")
        }
        return [path for path in self.writable(row) if os.path.realpath(path) not in kept]

    def writable(self, row: dict) -> list[str]:
        if row["role"] != WORKER:
            return [row["cwd"]]
        own = [str(self.workspace(row)), *(c["path"] for c in self.checkouts(row))]
        # A session continued from a terminal may have been started in home: that stays the
        # owner's, never a worker's to write (nor to read through Chief of Staff).
        return own if too_broad(row["cwd"]) else [row["cwd"], *own]

    async def make_checkout(
        self,
        workspace: Path,
        slug: str,
        project: str,
        isolate: bool,
        repos: set[str],
        carry: bool = True,
    ) -> dict | None:
        """A checkout of a configured project for a worker; None if it has that repo already.
        `carry`: copy the gitignored files the worktree needs (not for workers that read Slack:
        those files are often secrets)."""
        path = resolve_project(project, self.projects())
        if not isolate:
            checkout = {"project": str(path), "path": str(path), "branch": "", "shared": True}
            if git_dir := await common_git_dir(path):
                checkout["git_dir"] = git_dir
            return checkout
        toplevel = await git("-C", str(path), "rev-parse", "--show-toplevel")
        top = await asyncio.to_thread(Path(toplevel.strip()).resolve)
        if str(top) in repos:
            return None
        ref, base = await base_ref(top)
        tree = free_child(workspace, top.name)
        branch = f"{BRANCH_PREFIX}{slug}"
        # --no-track: the branch must never push to the default branch it started from.
        start = (await git("-C", str(top), "rev-parse", ref)).strip()  # Fixed, unlike ref.
        await git("-C", str(top), "worktree", "add", "--no-track", "-b", branch, str(tree), start)
        # Its own folder in the repository, read now, before a worker could change where its
        # .git points.
        admin = (await git("-C", str(tree), "rev-parse", "--absolute-git-dir")).strip()
        repos.add(str(top))
        return {
            "project": str(path),
            "repo": str(top),
            "worktree": str(tree),
            "path": str(tree / path.relative_to(top)),
            "branch": branch,
            "base": base,
            "base_ref": start,
            "git_dir": await common_git_dir(top) or str(top / ".git"),
            "admin": admin,
            "carried": await carry_ignored_files(top, tree) if carry else [],
        }

    @staticmethod
    async def remove_checkout(checkout: dict):
        """Undo a worktree made for a worker that could not be set up."""
        if not checkout.get("worktree"):
            return  # A shared project folder: nothing was created.
        with suppress(ValueError):
            await git("-C", checkout["repo"], "worktree", "remove", "--force", checkout["worktree"])
        with suppress(ValueError):
            await git("-C", checkout["repo"], "branch", "-D", checkout["branch"])

    async def clone_checkout(self, workspace: Path, slug: str, repo: str) -> dict:
        """Clone a repository that is not on this machine into a worker's workspace."""
        url, name = clone_source(repo)
        destination = free_child(workspace, name)
        if GITHUB_NAME.fullmatch(repo) and shutil.which("gh"):
            # gh uses the owner's GitHub sign-in, so private repositories work too.
            await self.gh_clone(repo, destination)
        else:
            await git("clone", "--quiet", url, str(destination), limit=900)
        default = (await git("-C", str(destination), "rev-parse", "--abbrev-ref", "HEAD")).strip()
        start = (await git("-C", str(destination), "rev-parse", "HEAD")).strip()
        branch = f"{BRANCH_PREFIX}{slug}"
        await git("-C", str(destination), "checkout", "--quiet", "-b", branch)
        return {
            "project": "",
            "repo": str(destination),
            "path": str(destination),
            "branch": branch,
            "base": f"origin/{default} of a fresh clone of {repo}",
            "base_ref": start,
            "remote": repo,
        }

    async def prepare_directory(
        self, session_id: str, title: str, projects: list[str], isolate: bool, carry: bool = True
    ) -> tuple[Path, list[dict]]:
        """The worker's own workspace folder, with a checkout of each project it works on."""
        slug = f"{slugify(title)}-{session_id[:6]}"
        projects = [project for project in projects if project]
        if isolate and not projects:
            raise ValueError("isolate=true needs a project that is a git repository.")
        workspace = self.scratch_root() / slug
        try:
            await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(
                f"Cannot create a workspace in {workspace.parent}: {exc.strerror or exc}. "
                "Fix the workspaces folder in Settings."
            ) from exc
        checkouts: list[dict] = []
        repos: set[str] = set()
        try:
            for project in projects:
                checkout = await self.make_checkout(workspace, slug, project, isolate, repos, carry)
                if checkout:
                    checkouts.append(checkout)
        except Exception:
            for checkout in checkouts:  # Leave nothing half made.
                await self.remove_checkout(checkout)
            raise
        return workspace, checkouts

    @staticmethod
    def check_scratch(settings: Settings):
        """Task folders must be usable before they are saved."""
        if not settings.scratch_directory:
            return
        try:
            Path(settings.scratch_directory).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(
                f"Task folders: {settings.scratch_directory} cannot be used: {exc.strerror or exc}"
            ) from exc

    async def project_cards(self) -> list[dict]:
        """What the Router needs to match a request to a project."""
        cards = []
        for project in self.projects()[:60]:
            if not await asyncio.to_thread(Path(project).is_dir):
                # Still configured: its folder went away (moved, deleted, a drive not mounted).
                cards.append(
                    {
                        "path": project,
                        "name": Path(project).name,
                        "remote": "",
                        "about": "",
                        "missing": "configured, but its folder is not on disk (moved or "
                        "deleted?): tell the owner; they restore it or remove it in Settings",
                    }
                )
                continue
            cached = self.project_cache.get(project)
            if cached and time.monotonic() - cached[0] < 600:
                cards.append(cached[1])
                continue
            card = {"path": project, "name": Path(project).name, "remote": "", "about": ""}
            with suppress(Exception):
                remote = await git("-C", project, "config", "--get", "remote.origin.url")
                card["remote"] = remote.strip()
            card["about"] = await asyncio.to_thread(readme_line, Path(project))
            # What the owner may call it: its package's name ("textkit" in a folder "project").
            package = await asyncio.to_thread(package_name, Path(project))
            if package and package != card["name"]:
                card["package"] = package
            self.project_cache[project] = (time.monotonic(), card)
            cards.append(card)
        return cards
