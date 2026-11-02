"""Where workers work: a workspace folder each, with worktrees or clones of their repositories."""

import asyncio
import itertools
import json
import os
import re
import shutil
import signal
import time
import unicodedata
from contextlib import suppress
from pathlib import Path

from .models import Settings
from .policy import within
from .roles import WORKER
from .store import Store

# Workers' branches: chief/<task-slug>-<id>.
BRANCH_PREFIX = "chief/"


async def command(*arguments: str, limit: float = 60) -> str:
    """Run a command without a terminal (it can never wait for a password prompt)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    # ssh asks for passphrases and host keys on the terminal, not stdin: make it fail instead.
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,  # No controlling terminal to prompt on.
    )
    try:
        async with asyncio.timeout(limit):
            stdout, stderr = await process.communicate()
    except TimeoutError as exc:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)  # Also ssh and other children.
        await process.wait()
        raise ValueError(f"{arguments[0]} timed out.") from exc
    if process.returncode:
        error = stderr.decode(errors="replace").strip()[:500]
        raise ValueError(f"{arguments[0]} failed: {error}")
    return stdout.decode(errors="replace")


async def git(*arguments: str, limit: float = 60) -> str:
    return await command("git", *arguments, limit=limit)


async def base_ref(top: Path) -> tuple[str, str]:
    """Where a new worktree starts: the default branch's latest commit when there is a remote."""
    if "origin" not in (await git("-C", str(top), "remote")).split():
        return "HEAD", "the HEAD of the local checkout (no origin remote)"
    note = ""
    try:
        await git("-C", str(top), "fetch", "--quiet", "origin", limit=180)
    except ValueError:
        note = " (fetch failed, so maybe not the latest)"
    ref = ""
    with suppress(ValueError):
        ref = (
            await git(
                "-C", str(top), "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"
            )
        ).strip()
    for candidate in ("origin/main", "origin/master"):
        if ref:
            break
        with suppress(ValueError):
            await git("-C", str(top), "rev-parse", "--verify", "--quiet", candidate)
            ref = candidate
    if not ref:
        return "HEAD", "the HEAD of the local checkout (origin has no default branch)"
    return ref, ref + note


GIT_URL = re.compile(r"^(https?://|ssh://|git@|file://)\S+$")


GITHUB_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


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


class Workspaces:
    def __init__(self, store: Store, work_root: Path):
        self.store = store
        self.work_root = work_root
        self.project_cache: dict[str, tuple[float, dict]] = {}

    def scratch_root(self) -> Path:
        """Where each worker's own workspace folder is created."""
        configured = self.store.settings().scratch_directory
        return Path(configured) if configured else self.work_root / "workspaces"

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
        return [row["cwd"], str(self.workspace(row)), *(c["path"] for c in self.checkouts(row))]

    async def make_checkout(
        self, workspace: Path, slug: str, project: str, isolate: bool, repos: set[str]
    ) -> dict | None:
        """A checkout of a configured project for a worker; None if it has that repo already."""
        path = resolve_project(project, self.store.settings().projects)
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
        await git("-C", str(top), "worktree", "add", "--no-track", "-b", branch, str(tree), ref)
        repos.add(str(top))
        return {
            "project": str(path),
            "repo": str(top),
            "worktree": str(tree),
            "path": str(tree / path.relative_to(top)),
            "branch": branch,
            "base": base,
            "git_dir": await common_git_dir(top) or str(top / ".git"),
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
        if GITHUB_NAME.fullmatch(repo):
            url = f"https://github.com/{repo}.git"
        elif GIT_URL.fullmatch(repo):
            url = repo
        else:
            raise ValueError("Give a configured project folder, a git URL or GitHub owner/name.")
        name = re.split(r"[/:]", url.rstrip("/").removesuffix(".git"))[-1]
        destination = free_child(workspace, name)
        if GITHUB_NAME.fullmatch(repo) and shutil.which("gh"):
            # gh uses the owner's GitHub sign-in, so private repositories work too.
            await command("gh", "repo", "clone", repo, str(destination), limit=900)
        else:
            await git("clone", "--quiet", url, str(destination), limit=900)
        default = (await git("-C", str(destination), "rev-parse", "--abbrev-ref", "HEAD")).strip()
        branch = f"{BRANCH_PREFIX}{slug}"
        await git("-C", str(destination), "checkout", "--quiet", "-b", branch)
        return {
            "project": "",
            "repo": str(destination),
            "path": str(destination),
            "branch": branch,
            "base": f"origin/{default} of a fresh clone of {repo}",
            "remote": repo,
        }

    async def prepare_directory(
        self, session_id: str, title: str, projects: list[str], isolate: bool
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
                if checkout := await self.make_checkout(workspace, slug, project, isolate, repos):
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
        for project in self.store.settings().projects:
            cached = self.project_cache.get(project)
            if cached and time.monotonic() - cached[0] < 600:
                cards.append(cached[1])
                continue
            card = {"path": project, "name": Path(project).name, "remote": "", "about": ""}
            with suppress(Exception):
                card["remote"] = await git("-C", project, "config", "--get", "remote.origin.url")
            card["about"] = await asyncio.to_thread(readme_line, Path(project))
            self.project_cache[project] = (time.monotonic(), card)
            cards.append(card)
        return cards
