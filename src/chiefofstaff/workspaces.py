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
    return await command("git", *arguments, limit=limit, input=input)


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
    paths = [root / "hooks", root / "config"]
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
        commit = (await git("-C", path, "rev-parse", head)).strip()
    except ValueError:
        return None
    edits = ""
    if live:
        with suppress(ValueError):
            edits = await git("-C", path, "status", "--porcelain")
            if edits.strip():
                edits += await git("-C", path, "diff", "--no-color", "--no-ext-diff", "HEAD")
                # New files show in the status by name only: their contents count too.
                others = await git("-C", path, "ls-files", "--others", "--exclude-standard", "-z")
                names = [name for name in others.split("\0") if name][:MAX_NEW_FILES]
                edits += await asyncio.to_thread(file_digest, Path(path), names)
    digest = hashlib.sha1(edits.encode()).hexdigest()[:12] if edits.strip() else ""
    return {"commit": commit, "edits": digest, "digest": DIGEST}


# How `edits` is computed: digests of different kinds are not compared (see same_version).
DIGEST = 2


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


async def changes(checkout: dict) -> dict | None:
    """What a worker changed in one checkout: commits on its branch and uncommitted edits, and
    the version (commit and edits) this describes."""
    path, head, live = await asyncio.to_thread(locate, checkout)
    try:
        await git("-C", path, "rev-parse", "--git-dir")
    except ValueError:
        return None
    result = {"path": checkout["path"], "branch": checkout.get("branch", ""), "commits": 0}
    committed = {"files": 0, "insertions": 0, "deletions": 0}
    if base := base_of(checkout):
        with suppress(ValueError):
            committed = receipts.shortstat(
                await git("-C", path, "diff", "--shortstat", f"{base}...{head}")
            )
            result["commits"] = int(await git("-C", path, "rev-list", "--count", f"{base}..{head}"))
    uncommitted = {"files": 0, "insertions": 0, "deletions": 0}
    if live:
        with suppress(ValueError):
            uncommitted = receipts.shortstat(await git("-C", path, "diff", "--shortstat", "HEAD"))
        with suppress(ValueError):
            status = await git("-C", path, "status", "--porcelain")
            result["uncommitted"] = len([line for line in status.splitlines() if line.strip()])
    for key in ("files", "insertions", "deletions"):
        result[key] = committed[key] + uncommitted[key]
    result.update(await version(checkout) or {})
    return result


MAX_DIFF = 2_000_000
MAX_NEW_FILES = 100
MAX_NEW_FILE = 200_000
# The same diff whatever the owner's git config says (prefixes, colors, external tools).
PLAIN_DIFF = (
    "-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff", "--no-textconv",
    "--src-prefix=a/", "--dst-prefix=b/",
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


async def diff(checkout: dict) -> dict:
    """The full diff of a worker's work in one checkout, from where it started: commits,
    uncommitted edits and new files (for a shared project folder: its uncommitted edits)."""
    path, head, live = await asyncio.to_thread(locate, checkout)
    top = (await git("-C", path, "rev-parse", "--show-toplevel")).strip()
    base = base_of(checkout) or "HEAD"
    if live:
        text = await git("-C", top, *PLAIN_DIFF, base, limit=60)
        others = await git("-C", top, "ls-files", "--others", "--exclude-standard", "-z")
        names = [name for name in others.split("\0") if name][:MAX_NEW_FILES]
        for name in names:
            text += await asyncio.to_thread(new_file_diff, Path(top), name)
    else:
        text = await git("-C", top, *PLAIN_DIFF, f"{base}...{head}", limit=60)
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
        listed = await git(
            "-C", str(tree), "ls-files", "--others", "--ignored", "--exclude-standard",
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
                await git("-C", checkout["repo"], "worktree", "remove", checkout["worktree"])
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
            self.project_cache[project] = (time.monotonic(), card)
            cards.append(card)
        return cards
