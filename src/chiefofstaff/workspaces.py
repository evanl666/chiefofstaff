"""Where workers work: a workspace folder each, with worktrees or clones of their repositories."""

import asyncio
import hashlib
import itertools
import json
import logging
import os
import re
import shutil
import signal
import time
import unicodedata
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

from . import receipts
from .models import Settings
from .policy import within
from .roles import WORKER
from .store import Store

# Workers' branches: chief/<task-slug>-<id>.
BRANCH_PREFIX = "chief/"


async def command(
    *arguments: str, limit: float = 60, input: bytes | None = None, env: dict | None = None
) -> str:
    """Run a command without a terminal (it can never wait for a password prompt)."""
    env = {**os.environ, **(env or {}), "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    # ssh asks for passphrases and host keys on the terminal, not stdin: make it fail instead.
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdin=asyncio.subprocess.DEVNULL if input is None else asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,  # No controlling terminal to prompt on.
    )
    try:
        async with asyncio.timeout(limit):
            stdout, stderr = await process.communicate(input)
    except TimeoutError as exc:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)  # Also ssh and other children.
        await process.wait()
        raise ValueError(f"{arguments[0]} timed out.") from exc
    if process.returncode:
        error = stderr.decode(errors="replace").strip()[:500]
        raise ValueError(f"{arguments[0]} failed: {error}")
    return stdout.decode(errors="replace")


async def git(*arguments: str, limit: float = 60, input: bytes | None = None) -> str:
    return await command("git", *SAFE_GIT, *arguments, limit=limit, input=input)


# Chief of Staff's own git runs outside any sandbox: nothing in a repository's config may run
# code for it (a file-system monitor, hooks).
SAFE_GIT = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
# What running code leaves behind in a project without a .gitignore for it: not work to commit,
# review or count (`__pycache__/`, `.pytest_cache/`, `node_modules/`, `*.pyc`, and a `uv.lock`
# that `uv run` wrote where the project keeps none). Only new files: a tracked one that changed
# is work.
GENERATED = re.compile(
    r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.hypothesis|\.tox|\.nox|"
    r"\.venv|venv|node_modules|\.gradle|\.next|\.turbo|\.cache|\.coverage|htmlcov|"
    r"[^/]+\.egg-info)(/|$)|\.(pyc|pyo|class|o)$|(^|/)(\.DS_Store|uv\.lock)$"
)


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
    if not common or not os.path.isdir(common):
        return {}
    planted = Path(common) / "commondir"
    if Path(common).name == ".git" and planted.exists():
        # A repository's own .git never has one: a worker made it to send git elsewhere.
        logger.warning("Removed %s, which pointed git at another repository.", planted)
        with suppress(OSError):
            planted.unlink()
    tree = checkout.get("worktree")
    resolved = Path(path).resolve()
    if tree and os.path.isdir(tree) and resolved.is_relative_to(Path(tree).resolve()):
        admin = checkout.get("admin") or ""
        if not admin or not os.path.isdir(admin):
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


async def checkout_git(checkout: dict, path: str, *arguments: str, limit: float = 60) -> str:
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
    return await command("git", *SAFE_GIT, "-C", path, *arguments, limit=limit, env=pins)


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


def find_repositories(folder: Path, skip: set[Path]) -> list[str]:
    """Git repositories in a code folder, and in its folders up to CODE_FOLDER_DEPTH deep.

    A repository's own subfolders are not searched (worktrees and submodules belong to it);
    hidden folders, dependencies and `skip` (Chief of Staff's own folders) are left out.
    """
    found: list[str] = []
    level = [folder]
    for depth in range(CODE_FOLDER_DEPTH + 1):
        deeper = []
        for path in level:
            if (path / ".git").is_dir():
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
    return url, re.split(r"[/:]", url.rstrip("/").removesuffix(".git"))[-1]


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
            input="\0".join(candidates).encode() + b"\0",
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
    return found.strip()


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
    if tree and checkout.get("branch") and not os.path.isdir(tree):
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
    digest = hashlib.sha1(edits.encode()).hexdigest()[:12] if edits.strip() else ""
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
    found = hashlib.sha1()
    for name in names:
        path = top / name
        if name.endswith("/") or not path.is_file():
            continue
        with suppress(OSError), path.open("rb") as file:
            found.update(name.encode() + b"\0" + file.read(MAX_NEW_FILE))
    return found.hexdigest()


class Scope:
    """One task's share of a checkout a worker kept using for several tasks: what it committed
    since `base` (the commit the checkout was at when the task was taken up), and of what is
    not committed, the files it changed: in a folder of the owner's (shared), those its own
    edits touched; elsewhere, those changed after `since` (when the task was taken up)."""

    def __init__(self, base: str = "", since: float = 0, touched: set[str] | None = None):
        self.base, self.since, self.touched = base, since, touched or set()

    def wrote(self, top: Path, name: str) -> bool:
        """Whether its own edits wrote this file."""
        return os.path.realpath(top / name) in self.touched

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


def status_name(line: str) -> str:
    """The file a `git status --porcelain` line is about (a rename's new name)."""
    name = line[3:]
    return name.split(" -> ", 1)[-1].strip('"')


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


async def scope_base(checkout: dict, path: str, head: str, scope: Scope) -> str:
    """Where this task's share starts: its own base when that commit is in the history, else
    where the branch started."""
    if scope.base:
        with suppress(ValueError):
            await checkout_git(checkout, path, "merge-base", "--is-ancestor", scope.base, head)
            return scope.base
    if base := base_of(checkout):
        with suppress(ValueError):
            return (await checkout_git(checkout, path, "merge-base", base, head)).strip()
    return ""


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


async def own_commits(checkout: dict, path: str, head: str, shared: bool, share: Share, scope):
    """The task's own commits since its base and the files each changed: those on its branch's
    own line (not ones a merge brought in from where its branch comes from); in a folder of the
    owner's, only those touching files its edits wrote (the owner's own commits are theirs)."""
    try:
        listed = await checkout_git(
            checkout, path, "-c", "core.quotePath=false", "log", "--first-parent",
            "--format=%x00%H %P", "--name-only", f"{share.base}..{head}",
        )  # fmt: skip
    except ValueError:
        return
    for entry in listed.split("\0"):
        line, _, names = entry.strip().partition("\n")
        if not line:
            continue
        sha, *parents = line.split()
        if len(parents) > 1:
            share.exact = False  # A merge: what it brought in is not this task's.
            continue
        files = [name for name in names.splitlines() if name]
        if shared and not any(scope.wrote(share.top, name) for name in files):
            share.exact = False  # The owner's own commit (or one they pulled).
            continue
        share.own[sha] = files


async def scope_files(
    checkout: dict, path: str, head: str, live: bool, shared: bool, scope: Scope
) -> Share:
    """This task's share of a checkout."""
    top = Path((await checkout_git(checkout, path, "rev-parse", "--show-toplevel")).strip())
    share = Share(await scope_base(checkout, path, head, scope), top)
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


async def scoped_changes(
    checkout: dict, path: str, head: str, live: bool, shared: bool, scope: Scope
) -> dict:
    share = await scope_files(checkout, path, head, live, shared, scope)
    top, keep = str(share.top), share.keep
    total = {"files": 0, "insertions": 0, "deletions": 0, "commits": len(share.own)}
    total["uncommitted"] = 0
    if share.exact and keep and (share.base or live):
        against = share.base if live else f"{share.base}..{head}"
        with suppress(ValueError):
            diffed = await checkout_git(
                checkout, top, "diff", "--shortstat", against or "HEAD", "--", *keep
            )
            total |= receipts.shortstat(diffed)
    elif keep:
        # Its commits' own lines (not a merged-in teammate's in the same file), then its edits
        # since the last commit.
        total["files"] = len(keep)
        if share.own and share.committed:
            shas = list(share.own)[:MAX_OWN_COMMITS]
            with suppress(ValueError):
                counted = await checkout_git(
                    checkout, top, "show", "--numstat", "--format=", *shas, "--",
                    *sorted(share.committed),
                )  # fmt: skip
                for line in counted.splitlines():
                    added, removed, *_ = [*line.split("\t"), "", ""]
                    if added.isdigit() and removed.isdigit():
                        total["insertions"] += int(added)
                        total["deletions"] += int(removed)
        if live:
            with suppress(ValueError):
                edits = receipts.shortstat(
                    await checkout_git(checkout, top, "diff", "--shortstat", "HEAD", "--", *keep)
                )
                total["insertions"] += edits.get("insertions", 0)
                total["deletions"] += edits.get("deletions", 0)
    if live:
        total["uncommitted"] = len([name for name in keep if name not in share.committed]) + len(
            share.fresh
        )
        total["files"] += len(share.fresh)
        total["insertions"] += await asyncio.to_thread(count_lines, share.top, share.fresh)
    return total


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
        if share.exact and share.keep:
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
    else:
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
    }


# Ignored folders that are made again by an install or a build: no reason to keep a worktree.
REGENERABLE = {
    *SKIPPED, ".venv", ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".cache", ".next", ".nuxt", ".turbo", ".parcel-cache", ".gradle", ".idea", ".vscode",
    "coverage", "htmlcov", ".coverage", ".DS_Store", "out-tsc",
}  # fmt: skip


async def kept_ignored(checkout: dict) -> list[str]:
    """Ignored files in a worker's worktree that may matter (git removes them with it): any
    outside folders an install or a build makes, and copied-in files it changed (an .env)."""
    tree = Path(checkout["worktree"])
    try:
        listed = await checkout_git(
            checkout, str(tree), "ls-files", "--others", "--ignored", "--exclude-standard",
            "--directory", "-z",
        )  # fmt: skip
    except ValueError:
        return []
    carried = set(checkout.get("carried") or [])
    kept = []
    for entry in filter(None, listed.split("\0")):
        parts = entry.rstrip("/").split("/")
        if any(part in REGENERABLE or part.endswith((".egg-info", ".pyc")) for part in parts):
            continue
        if await asyncio.to_thread(only_carried, tree, entry, carried, Path(checkout["repo"])):
            continue
        kept.append(entry)
    return kept


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


class Workspaces:
    def __init__(self, store: Store, work_root: Path):
        self.store = store
        self.work_root = work_root
        self.project_cache: dict[str, tuple[float, dict]] = {}
        self.known: tuple[float, tuple, list[str]] = (0.0, (), [])
        self.clone_lock = asyncio.Lock()
        # The token of the gh account that can see a repository ("" for gh's active one).
        self.github_token: Callable[[str], Awaitable[str]] | None = None

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
        """A closed worker's worktrees, removed when they hold nothing uncommitted.

        The branches stay in their repositories, and restore() brings the worktrees back when
        the worker is reopened. git refuses to remove a worktree with changes, so nothing
        unsaved is lost; that worktree just stays.
        """
        removed = []
        for checkout in self.checkouts(row):
            tree = checkout.get("worktree")
            if not tree or not await asyncio.to_thread(os.path.isdir, tree):
                continue
            if kept := await kept_ignored(checkout):
                # git would delete them with the worktree: it stays instead.
                logger.info("Kept %s: it has ignored files: %s", tree, ", ".join(kept[:5]))
                continue
            try:
                await checkout_git(
                    checkout, checkout["repo"], "worktree", "remove", checkout["worktree"]
                )
                removed.append(checkout["worktree"])
            except ValueError:
                logger.info("Kept %s: it has uncommitted changes.", checkout["worktree"])
        with suppress(OSError):
            # Only when nothing else is in it, like a worker's notes.
            await asyncio.to_thread(self.workspace(row).rmdir)
        return removed

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
        """A scratch folder must be usable before it is saved."""
        if not settings.scratch_directory:
            return
        try:
            Path(settings.scratch_directory).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"The scratch folder cannot be used: {exc.strerror or exc}") from exc

    async def project_cards(self) -> list[dict]:
        """What the Router needs to match a request to a project."""
        cards = []
        for project in self.projects()[:60]:
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
