"""Where workers work: a workspace folder each, with worktrees or clones of their repositories."""

import asyncio
import contextlib
import errno
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
import weakref
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from . import machine, policy, privategit, receipts, safefs, settings_guard
from .models import Settings
from .policy import CODEX_SETTINGS, REPOSITORY_SETTINGS, within
from .privategit import config_entries
from .roles import WORKER
from .settings_guard import read_regular
from .store import Store

# Workers' branches: chief/<task-slug>-<id>.
BRANCH_PREFIX = "chief/"


class TimedOutError(ValueError):
    """A command stopped for taking too long (not one that failed by itself)."""


# What Chief of Staff's own git never does in a worker's tree: look into its submodules' work
# trees. A submodule's git folder in a worktree of Chief of Staff's is in that worktree's own
# folder (.git/worktrees/<it>/modules/), which its worker can write: a filter or a file-system
# monitor configured there would run as the owner. Their commits still count (`dirty`).
INTO_SUBMODULES = {"status": "--ignore-submodules=dirty", "diff": "--ignore-submodules=dirty",
                   "fetch": "--no-recurse-submodules"}  # fmt: skip
# What every diff, show and log Chief of Staff runs is told, whatever the owner's or a
# repository's config says: no external diff program and no textconv one (as configured, they run
# on what a worker wrote), a submodule shown as the commit it is at (never its log or its diff,
# which open its repository: its config, which its worker can write).
PLAIN_DIFFS = {"diff", "show", "log"}
PLAIN_DIFF_OPTIONS = ("--no-ext-diff", "--no-textconv", "--submodule=short")


# git's own options before its command that take the next word as their value.
VALUED_GIT_OPTIONS = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--config-env"}


# git commands that reach another repository (over a transport): the only ones that may.
NETWORK_GIT = {"fetch", "push", "clone", "ls-remote", "pull"}
# The transports those may use (GIT_ALLOW_PROTOCOL: whatever a repository's config says). Not
# `ext::` (a command line), nor a remote helper (`<name>::`, a program by its name).
TRANSPORTS = "http:https:ssh:git:file"
# Those from a repository a worker can write (private_git): not a repository on this machine
# either, which a worker may have made (its hooks run as whoever pushes there).
REMOTE_TRANSPORTS = "http:https:ssh:git"


def git_command_at(words: list[str] | tuple[str, ...]) -> int:
    """Where a git command line's command is (after git's own options), len(words) for none."""
    at = 1
    while at < len(words) and words[at].startswith("-"):
        at += 2 if words[at] in VALUED_GIT_OPTIONS else 1
    return at


def git_command_of(arguments: tuple[str, ...]) -> str:
    at = git_command_at(arguments)
    return arguments[at] if at < len(arguments) else ""


def git_environment(
    arguments: tuple[str, ...], lazy: bool, local_remotes: bool = True
) -> dict[str, str]:
    """What every git Chief of Staff runs has in its environment, whatever the repository's
    config says: no transport but the plain ones (not `file`, a repository on this machine,
    unless `local_remotes`), and only for a command that reaches another repository or (`lazy`:
    one of the owner's, a partial clone's missing objects fetched as they are read) where that
    may happen. Elsewhere (a repository a worker can write): no transport at all and no object
    fetched as it is read."""
    # (No replace ref: refs/replace/ would change what a commit reads as.)
    environment = {"GIT_NO_REPLACE_OBJECTS": "1"} | ({} if lazy else {"GIT_NO_LAZY_FETCH": "1"})
    if lazy or git_command_of(arguments) in NETWORK_GIT:
        transports = TRANSPORTS if local_remotes else REMOTE_TRANSPORTS
        return environment | {"GIT_ALLOW_PROTOCOL": transports}
    return environment | {"GIT_ALLOW_PROTOCOL": ""}


# What git reaching another repository never does for Chief of Staff, whatever the owner's or a
# repository's config says (`submodule.recurse`, `push.recurseSubmodules`): go into submodules,
# whose own git folders (and so their config: a receivepack, an sshCommand) a worker can write.
NOT_INTO_SUBMODULES = ("-c", "submodule.recurse=false", "-c", "push.recurseSubmodules=no",
                       "-c", "fetch.recurseSubmodules=no")  # fmt: skip


def without_submodules(arguments: tuple[str, ...]) -> tuple[str, ...]:
    """A git command line as Chief of Staff runs it: `diff`, `show` and `log` with
    PLAIN_DIFF_OPTIONS, `status` and `diff` not looking into submodules' work trees (whatever
    else it asked for), `fetch` not into submodules."""
    words = list(arguments)
    at = git_command_at(words)
    if at < len(words) and words[at] in PLAIN_DIFFS:
        words[at + 1 : at + 1] = [o for o in PLAIN_DIFF_OPTIONS if o not in words[at + 1 :]]
        arguments = tuple(words)
    if at >= len(words) or not (flag := INTO_SUBMODULES.get(words[at])):
        return arguments
    name, rest = flag.split("=")[0], words[at + 1 :]
    paths = rest.index("--") if "--" in rest else len(rest)
    options = [w for w in rest[:paths] if w.split("=")[0] not in {name, "--recurse-submodules"}]
    return (*words[: at + 1], flag, *options, *rest[paths:])


async def command(
    *arguments: str,
    limit: float = 60,
    stdin_data: bytes | None = None,
    env: dict | None = None,
    lazy: bool = False,
    local_remotes: bool = True,
) -> str:
    """Run a command without a terminal (it can never wait for a password prompt), and without
    Chief of Staff's own CHIEFOFSTAFF_* variables (a token one may hold): what it runs can run code
    a repository or a worker configured (a git filter). git never runs what a repository's
    config names (SAFE_GIT), never looks into submodules' work trees (without_submodules), and
    reaches other repositories only as git_environment lets it (`lazy`: one of the owner's
    repositories, which may fetch objects as they are read; `local_remotes`: not one a worker
    can write, which may reach a repository on this machine). Stopped, with what it started,
    when it takes longer than `limit` or whoever waits for it stops waiting."""
    fixed: dict[str, str] = {}
    finish = False
    if arguments and arguments[0] == "git":
        if "core.fsmonitor=false" not in arguments:
            arguments = ("git", *SAFE_GIT, *arguments[1:])
        arguments = without_submodules(arguments)
        if git_command_of(arguments) in NETWORK_GIT:
            arguments = ("git", *NOT_INTO_SUBMODULES, *arguments[1:])
        fixed = git_environment(arguments, lazy, local_remotes)
        # Stopped halfway, git making or removing a worktree leaves half a tree (files gone, its
        # `.git` gone, git's record of it left): it finishes, within its limit, even when
        # whoever waits for it stops waiting (Chief of Staff stopping).
        finish = git_command_of(arguments) == "worktree"
    elif arguments and arguments[0] == "gh":
        fixed = {"GIT_ALLOW_PROTOCOL": TRANSPORTS}  # (The git it runs: a clone.)
    own = {key: value for key, value in os.environ.items() if not key.startswith("CHIEFOFSTAFF_")}
    env = {**own, **(env or {}), **fixed, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
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
    deadline = asyncio.get_running_loop().time() + limit
    try:
        async with asyncio.timeout_at(deadline):
            stdout, stderr = await process.communicate(stdin_data)
    except TimeoutError as exc:
        machine.kill_process_tree(process.pid)
        await process.wait()
        raise TimedOutError(f"{arguments[0]} timed out.") from exc
    except asyncio.CancelledError:
        # Whoever waited stopped (Chief of Staff stopping, a look past its time): it ends too,
        # but for one that finishes (by its deadline, unless whoever waits insists).
        if finish:
            with suppress(TimeoutError, asyncio.CancelledError):
                async with asyncio.timeout_at(deadline):
                    await asyncio.shield(process.communicate())
        if process.returncode is None:
            machine.kill_process_tree(process.pid)
        raise
    if process.returncode:
        error = stderr.decode(errors="replace").strip()[:500]
        raise ValueError(f"{arguments[0]} failed: {error}")
    return stdout.decode(errors="replace")


async def git(*arguments: str, limit: float = 60, stdin_data: bytes | None = None) -> str:
    """Chief of Staff's git in the owner's repositories (making and removing its worktrees,
    fetching their branch) and in a clone it makes before any worker has it; in a worker's
    checkouts, checkout_git."""
    where = arguments[arguments.index("-C") + 1] if "-C" in arguments[:-1] else ""
    await guarded(where)
    return await command(
        "git", *SAFE_GIT, *arguments, limit=limit, stdin_data=stdin_data, lazy=True
    )


# What the runtime looks at before Chief of Staff runs git (Runtime.git_guard): on Linux, the
# files a worker must not make in a .git (a commondir would send that git elsewhere); anywhere,
# a .git (or a folder of one) a worker replaced. It refuses (ValueError) git there until the
# owner says so.
GIT_GUARD: weakref.WeakMethod | None = None


async def guarded(where: str = "", roots: tuple[str, ...] = ()):
    """Off the event loop (some lstat calls), in the runtime's own threads for it
    (Runtime.guard_thread: never behind its looks at workers' settings); ValueError where it
    may not run."""
    if GIT_GUARD and (guard := GIT_GUARD()):
        await guard.__self__.guard_thread(guard, where, roots)


# Chief of Staff's own git runs outside any sandbox: nothing in a repository's config may run
# code for it (a file-system monitor, hooks). Git for Windows can read /dev/null as a folder at
# the drive's root, which any user may make (as in CVE-2022-24765): there hooks are looked for
# in this file instead, which nothing can be put in. A submodule is shown as the commit it is
# at, whatever the owner's config says (`diff.submodule=log` or `diff`, a status summary): its
# own repository, whose config its worker can write, is never opened for its log or diff.
NO_HOOKS = Path(__file__).as_posix() if sys.platform == "win32" else "/dev/null"
SAFE_GIT = ("-c", "core.fsmonitor=false", "-c", f"core.hooksPath={NO_HOOKS}",
            "-c", "diff.submodule=short", "-c", "status.submoduleSummary=false")  # fmt: skip
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


# A remote's address git reaches with a plain transport (TRANSPORTS): a URL of one, a path, or
# scp-like `host:path`. Anything else (`ext::…`, `<helper>::…`, an unknown scheme) runs a program.
PLAIN_ADDRESS = re.compile(
    r"^((https?|ssh|git|file|git\+ssh|ssh\+git)://|[^:]*$|[^:/]+:(?!:)|[A-Za-z]:[\\/])",
    re.IGNORECASE,
)


def plain_address(url: str) -> bool:
    """Whether git reaches a remote's address with a plain transport, running no program."""
    url = url.strip()
    return bool(url) and "::" not in url and bool(PLAIN_ADDRESS.match(url))


def admin_dir(common: str, tree: str) -> str:
    """A linked worktree's own folder in its repository (.git/worktrees/<name>), found from the
    repository's side, not from the worktree's .git file (which its worker can rewrite)."""
    root = Path(common) / "worktrees"
    target = (Path(tree) / ".git").resolve()
    with suppress(OSError):
        for child in sorted(root.iterdir()):
            with suppress(OSError):
                named = read_regular(child / "gitdir", 65536, follow=True).decode()
                if Path(named.strip()).resolve() == target:
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
                named = read_regular(child / "gitdir", 65536, follow=True).decode()
                root = Path(named.strip()).resolve().parent
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


# git's commands that read a work tree's files: there, Chief of Staff's git leaves out each
# submodule's path (whose own git folder, config included, a worker can write: git would read
# it to tell the submodule's commit) and, in a repository a worker can write, each file a
# filter would have to read (git-lfs's: no filter runs there, see unfiltered).
READS_TREE = {"status", "add", "diff", "diff-files", "diff-index"}
# Of those, the ones that may also compare only commits or the index (then nothing is left out).
COMPARES = {"diff", "diff-index"}
# The git commands that read a checkout's files (where gitattributes could name a filter), a
# racily clean one's to tell it from its index entry too (write-tree), or write them.
READS_FILES = {"status", "add", "diff", "show", "log", "stash", "commit", "checkout", "restore",
               "reset", "merge", "rebase", "cherry-pick", "grep", "blame", "archive",
               "write-tree", "ls-files", "update-index", "worktree", "switch", "read-tree",
               "checkout-index", "apply", "hash-object", "diff-files", "diff-index"}  # fmt: skip
# Told when a clone in a worker's folder was made again where it was (the worker cloned it
# anew): Runtime.clone_remade, which records it and says so once on the worker's timeline.
CLONE_REMADE: weakref.WeakMethod | None = None


def reads_tree(arguments: tuple[str, ...]) -> bool:
    """Whether a git command line reads the work tree's files (not `diff A B`, `diff --cached`)."""
    at = git_command_at(arguments)
    if at >= len(arguments) or arguments[at] not in READS_TREE:
        return False
    if arguments[at] not in COMPARES:
        return True
    rest = list(arguments[at + 1 :])
    rest = rest[: rest.index("--")] if "--" in rest else rest
    if any(word in {"--cached", "--staged", "--no-index"} for word in rest):
        return False
    revisions = [word for word in rest if not word.startswith("-")]
    return len(revisions) <= 1 and not any(".." in word for word in revisions)


def left_out(arguments: tuple[str, ...], paths: list[str], filters: list[str]) -> tuple[str, ...]:
    """A git command line that reads the work tree with these paths (from the top) and the
    files these filters keep left out of it; as it was for any other."""
    specs = [f":(top,exclude,literal){path}" for path in paths]
    specs += [f":(top,exclude,attr:filter={name})" for name in filters]
    if not specs or not reads_tree(arguments):
        return arguments
    return (*arguments, *specs) if "--" in arguments else (*arguments, "--", *specs)


# Each index's submodules (by its file: (inode, size, mtime)): {path from the top: commit}. The
# same as privategit's, which lists a worker's clone's as it checks its copy.
GITLINKS = privategit.GITLINKS


async def index_gitlinks(path: str, env: dict[str, str], index: Path, lazy: bool) -> dict:
    """The submodules (and repositories added in it) an index holds: {path from the top: the
    commit it records}; read again only when the index changed."""
    try:
        info = await asyncio.to_thread(os.stat, index)
        key = (info.st_ino, info.st_size, info.st_mtime_ns)
    except OSError:
        return {}
    if (known := GITLINKS.get(str(index))) and known[0] == key:
        return known[1]
    listed = await command(
        "git", *SAFE_GIT, "-C", path, "ls-files", "--stage", "-z", "--full-name", "--", ":/",
        env=env, lazy=lazy,
    )  # fmt: skip
    found = {}
    for entry in listed.split("\0"):
        mode, _, name = entry.partition("\t")
        if mode.startswith("160000 ") and name:
            found[name] = mode.split()[1]
    if len(GITLINKS) > 256:
        GITLINKS.clear()
    GITLINKS[str(index)] = (key, found)
    return found


async def checkout_git(
    checkout: dict,
    path: str,
    *arguments: str,
    limit: float = 60,
    env: dict | None = None,
    every: bool = False,
    filtered: bool = False,
) -> str:
    """git in one of a worker's checkouts, as Chief of Staff: the repository pinned (git_pins),
    nothing in its config run, each submodule's path left out where git would read the work
    tree (unless `every`: what is left out, unread). A clone in a worker's folder (one Chief of
    Staff made there, or one of its own) is read through a git folder of Chief of Staff's own
    (clone_git): never through its own .git, whose config the worker can write. `filtered`:
    only about the files a filter keeps there (which are not read: private_git); "" for none,
    and anywhere else."""
    await guarded(path, (checkout.get("git_dir") or "", checkout.get("admin") or ""))
    if checkout.get("remote"):
        return await clone_git(checkout, path, arguments, limit, env, every, filtered)
    if filtered:
        return ""
    pins = await asyncio.to_thread(git_pins, checkout, path)
    if pins is None:
        raise ValueError(f"Not reading {path}: its worktree cannot be found in its repository.")
    pins = {**pins, **(env or {})}
    if not every and reads_tree(("git", *arguments)) and (own := pins.get("GIT_DIR")):
        index = Path(pins.get("GIT_INDEX_FILE") or Path(own) / "index")
        links = await index_gitlinks(path, pins, index, lazy=True)
        arguments = left_out(("git", *arguments), list(links), [])[1:]
    return await command(
        "git", *SAFE_GIT, "-C", path, *arguments, limit=limit, env=pins, lazy=True
    )  # fmt: skip


async def clone_git(
    checkout: dict, path: str, arguments: tuple[str, ...], limit: float, env: dict | None,
    every: bool, filtered: bool,
) -> str:  # fmt: skip
    """git in a clone in a worker's folder (or a worktree made of one): through its .git as
    found (clone_place), with a git folder of Chief of Staff's own (privategit)."""
    common, admin, tree, remade = await asyncio.to_thread(clone_place, checkout, path)
    if remade:
        checkout.update(remade)
        if CLONE_REMADE and (told := CLONE_REMADE()):
            told(checkout)
    return await private_git(common, admin, tree, origin_of(checkout), path, arguments,
                             limit=limit, env=env, every=every, filtered=filtered)  # fmt: skip


def blob_id(data: bytes, long: bool = False) -> str:
    """The id git gives a file's bytes (SHA-256 in a repository that uses it: `long`)."""
    found = hashlib.sha256() if long else hashlib.sha1(usedforsecurity=False)
    found.update(b"blob %d\0" % len(data) + data)
    return found.hexdigest()


class Committed:
    """The files of a commit fixed before any worker had a checkout (its base_ref), as Chief of
    Staff reads them in that checkout's repository: each object on the way hashed again here,
    from the commit's own id down, so a .git a worker can write (an object replaced in place, a
    pack planted, refs/replace) cannot make it say other bytes; nothing fetched. ValueError for
    what can't be read so."""

    def __init__(self, checkout: dict, path: str, commit: str):
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise ValueError("Not a commit's id.")
        self.checkout, self.path, self.commit = checkout, path, commit
        self.long = len(commit) == 64
        self.trees: dict[str, dict[str, tuple[str, str]]] = {}
        self.top = ""

    async def git(self, *arguments: str) -> bytes:
        said = await checkout_git(
            self.checkout, self.path, *arguments, env={"GIT_NO_LAZY_FETCH": "1"}, every=True
        )
        return said.encode()

    def hashed(self, kind: bytes, data: bytes) -> str:
        found = hashlib.sha256() if self.long else hashlib.sha1(usedforsecurity=False)
        found.update(kind + b" %d\0" % len(data) + data)
        return found.hexdigest()

    async def root(self) -> str:
        if not self.top:
            data = await self.git("cat-file", "commit", self.commit)
            if self.hashed(b"commit", data) != self.commit:
                raise ValueError("The commit read is not that commit.")
            first = data.split(b"\n", 1)[0].decode()
            if not first.startswith("tree "):
                raise ValueError("No tree in that commit.")
            self.top = first.removeprefix("tree ")
        return self.top

    async def tree(self, tree: str) -> dict[str, tuple[str, str]]:
        """{name: (mode, id)} of a tree, rebuilt as git stores it and hashed against its id."""
        if tree in self.trees:
            return self.trees[tree]
        listed = await self.git("ls-tree", "-z", "--full-tree", tree)
        raw, found = b"", {}
        for item in listed.split(b"\0")[:-1]:
            head, _, name = item.partition(b"\t")
            mode, kind, oid = head.split(b" ")
            stored = mode.lstrip(b"0") if kind == b"tree" else mode
            raw += stored + b" " + name + b"\0" + bytes.fromhex(oid.decode())
            found[name.decode()] = (mode.decode(), oid.decode())
        if self.hashed(b"tree", raw) != tree:
            raise ValueError("A tree read is not that tree.")
        self.trees[tree] = found
        return found

    async def find(self, path: str) -> tuple[str, str] | None:
        """(mode, id) of what is at a path from the top; None for nothing there."""
        mode, oid = "040000", await self.root()
        for part in [part for part in path.split("/") if part]:
            if mode != "040000":
                return None
            entry = (await self.tree(oid)).get(part)
            if entry is None:
                return None
            mode, oid = entry
        return mode, oid

    async def files(self, path: str) -> dict[str, tuple[str, str]]:
        """Every entry but a folder at or under a path, by its path from the top."""
        found = await self.find(path)
        if found is None:
            return {}
        if found[0] != "040000":
            return {path: found}
        out: dict[str, tuple[str, str]] = {}
        for name, (mode, oid) in (await self.tree(found[1])).items():
            if mode == "040000":
                out |= await self.files(f"{path}/{name}")
            else:
                out[f"{path}/{name}"] = (mode, oid)
        return out


class UnreadableError(ValueError):
    """Chief of Staff's own git folder for a repository a worker can write could not be made
    (privategit.prepare): "Not reading <path>: <why>." (`why` alone too)."""

    def __init__(self, path: str, why: str):
        super().__init__(f"Not reading {path}: {why}.")
        self.why = why


async def private_git(
    common: str,
    admin: str,
    tree: str,
    origin: str,
    path: str,
    arguments: tuple[str, ...],
    limit: float = 60,
    env: dict | None = None,
    every: bool = False,
    filtered: bool = False,
) -> str:
    """git in a repository a worker can write (its git folder `common`, `admin` for one of its
    linked worktrees; its work tree `tree`), run at `path` with Chief of Staff's own git folder
    for it (privategit.prepare): never the repository's config, hooks or info/attributes. Its
    files are read with no filter (unfiltered) and those a filter keeps left out (unless
    `every`; `filtered`: only those, "" for none), and no submodule is looked into; nothing is
    fetched as it is read. UnreadableError when that git folder can't be made."""
    folder = privategit.place(common, admin)
    async with privategit.lock(folder):
        try:
            pins = await asyncio.to_thread(privategit.prepare, common, admin, tree, origin)
        except ValueError as exc:
            raise UnreadableError(path, str(exc)) from exc
        except OSError as exc:
            raise UnreadableError(path, f"Chief of Staff's copy of its git folder can't be made "
                                        f"({exc.strerror or exc})") from exc  # fmt: skip
        pins |= env or {}
        line = ("git", *arguments)
        options: tuple[str, ...] = ("-c", "submodule.recurse=false")
        filters: list[str] = []
        if git_command_of(line) in READS_FILES or filtered:
            blanked, filters = await unfiltered(pins, path, common, admin)
            options += blanked
        if filtered:
            if not filters:
                return ""
            specs = [f":(top,attr:filter={name})" for name in filters]
            line = (*line, *specs) if "--" in line else (*line, "--", *specs)
        stdin = None
        if not filtered and not every and reads_tree(line):
            # (Known by the repository's own index: its copy here is made again when it changes.)
            index = Path(pins.get("GIT_INDEX_FILE") or Path(admin or common) / "index")
            links = list(await index_gitlinks(path, pins, index, lazy=False))
            if filters and git_command_of(line) == "add":
                # git before 2.43 (Debian 12's 2.39) takes no `attr:` pathspec in `add`: the
                # files those filters keep are listed (ls-files takes it) and left out by name,
                # all its paths given on its stdin (there may be many).
                kept: set[str] = set()
                specs = [f":(top,attr:filter={name})" for name in filters]
                for listing in ((), ("--others", "--exclude-standard")):
                    listed = await command(
                        "git", *SAFE_GIT, *options, "-C", path, "ls-files", "-z", "--full-name",
                        *listing, "--", *specs, limit=limit, env=pins, lazy=False,
                        local_remotes=False,
                    )  # fmt: skip
                    kept.update(name for name in listed.split("\0") if name)
                line = left_out(line, [*links, *sorted(kept)], [])
                if "--" in line:
                    at = line.index("--")
                    stdin = "\0".join(line[at + 1 :]).encode()
                    line = (*line[:at], "--pathspec-from-file=-", "--pathspec-file-nul")
            else:
                line = left_out(line, links, filters)
        return await command(
            "git", *SAFE_GIT, *options, "-C", path, *line[1:], limit=limit, env=pins, lazy=False,
            local_remotes=False, stdin_data=stdin,
        )  # fmt: skip


# The filters the owner's and the system's git config define, as `git config` last said them:
# with the environment it was read in and each file it came from (identity, size, time).
OUTSIDE_FILTERS: dict[tuple, tuple[float, tuple, set[str]]] = {}
CONFIG_ENVIRONMENT = ("HOME", "XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                      "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_PARAMETERS")  # fmt: skip


def owner_config_files(environment: dict[str, str]) -> list[str]:
    """Where the owner's own git config is, there or not yet."""
    home = environment.get("HOME") or str(Path.home())
    xdg = environment.get("XDG_CONFIG_HOME") or str(Path(home) / ".config")
    return [environment.get("GIT_CONFIG_GLOBAL") or str(Path(home) / ".gitconfig"),
            str(Path(xdg) / "git" / "config")]  # fmt: skip


async def outside_filters(pins: dict[str, str], path: str) -> set[str]:
    """The filters git (with Chief of Staff's own git folder) knows of: the owner's and the
    system's config's, with their includes; read again when one of their files changed (or
    appeared), and at least every minute."""
    environment = {**os.environ, **pins}
    variables = sorted(item for item in environment.items() if item[0].startswith("GIT_CONFIG"))
    key = (pins.get("GIT_DIR", ""), *(environment.get(name, "") for name in CONFIG_ENVIRONMENT),
           *variables)  # fmt: skip
    if (known := OUTSIDE_FILTERS.get(key)) and time.monotonic() - known[0] < 60:
        _, files, names = known
        now = await asyncio.to_thread(lambda: tuple(privategit.looked(Path(f)) for f, _ in files))
        if now == tuple(look for _, look in files):
            return names
    listed = await command(
        "git", *SAFE_GIT, "-C", path, "config", "--list", "--show-origin", "-z", env=pins,
        limit=20,
    )  # fmt: skip
    names, origins = set(), set()
    items = listed.split("\0")
    for origin, entry in zip(items[::2], items[1::2], strict=False):
        names |= filter_names([entry.split("\n", 1)[0]])
        if origin.startswith("file:"):
            origins.add(origin.removeprefix("file:"))
    origins |= set(owner_config_files(environment))
    files = await asyncio.to_thread(
        lambda: tuple((f, privategit.looked(Path(f))) for f in sorted(origins))
    )
    if len(OUTSIDE_FILTERS) > 64:
        OUTSIDE_FILTERS.clear()
    OUTSIDE_FILTERS[key] = (time.monotonic(), files, names)
    return names


async def unfiltered(
    pins: dict[str, str], path: str, common: str, admin: str = ""
) -> tuple[tuple[str, ...], list[str]]:
    """git's options that turn off every filter a repository a worker can write would otherwise
    run (git-lfs's: defined in the owner's or the system's config, which Chief of Staff's git
    keeps for how files are kept, autocrlf included), and their names: the files they keep are
    left out of what is read (left_out), their content not being what git would store. A filter
    only the repository's own config defines (which git never reads here) is none: the files it
    names are read as they are, never left out (a worker could name one to hide its changes).
    Gitattributes in its tree are read for git's own conversions (eol, text, encoding), which
    run no program."""
    names = await outside_filters(pins, path)
    options: list[str] = []
    for name in sorted(names):
        for setting in ("clean=", "smudge=", "process=", "required=false"):
            options += ["-c", f"filter.{name}.{setting}"]
    return tuple(options), sorted(names)


def filter_names(keys) -> set[str]:
    """The filters some config keys define (`filter.<name>.<setting>`)."""
    found = set()
    for key in keys:
        section, _, rest = key.partition(".")
        if section.lower() == "filter" and "." in rest:
            found.add(rest.rpartition(".")[0])
    return found


def clone_identity(dot_git: Path) -> dict:
    """What a clone's .git is as Chief of Staff takes it (it made the clone, or a worker's own
    is taken as a checkout): its folder, device and inode (clone_place)."""
    real = os.path.realpath(dot_git)
    info = Path(real).stat()
    return {"clone_git": real, "clone_id": [info.st_dev, info.st_ino]}


def clone_place(checkout: dict, path: str) -> tuple[str, str, str, dict]:
    """Where a clone in a worker's folder is: (its git folder, the admin folder of the linked
    worktree of it `path` is in or "", the work tree, and its identity when it is a new one).
    Its .git must be a folder there (not a `gitdir:` file, nor a link): one the worker made
    again where it was is read as the clone now (its new identity returned, to record); its
    config is never read (private_git). ValueError when it can't be found so."""
    repo = checkout["repo"]
    if checkout.get("git_dir") and os.path.realpath(checkout["git_dir"]) == os.path.realpath(repo):
        common, own = repo, False  # A bare repository (its own worktree's).
    else:
        common, own = str(Path(repo) / ".git"), True
    try:
        if Path(repo).is_symlink():
            raise ValueError(f"Not reading {path}: its folder is a link now.")
        info = os.lstat(common)
    except OSError:
        raise ValueError(f"Not reading {path}: its .git is gone.") from None
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Not reading {path}: its .git is no longer a folder.")
    common = os.path.realpath(common)
    identity = [info.st_dev, info.st_ino]
    remade = {}
    if (checkout.get("clone_git") and common != checkout["clone_git"]) or (
        checkout.get("clone_id") and identity != checkout["clone_id"]
    ):
        remade = {"clone_git": common, "clone_id": identity}
    resolved = Path(path).resolve()
    if own and resolved.is_relative_to(Path(repo).resolve()):
        return common, "", str(Path(repo)), remade
    if found := linked_tree(common, resolved):
        admin, root = found
        return common, admin, root, remade
    raise ValueError(f"Not reading {path}: its worktree cannot be found in its repository.")


def origin_of(checkout: dict) -> str:
    """Where a clone in a worker's folder is pushed to, as Chief of Staff recorded it (never as
    its config says now): the remote it cloned it from, or the one a worker's own clone had
    when it was taken; "" for none."""
    if found := checkout.get("origin"):
        return found if plain_address(found) else ""
    remote = checkout.get("remote") or ""
    if not remote or remote == checkout.get("repo"):
        return ""
    if GITHUB_NAME.fullmatch(remote):
        return clone_source(remote)[0]  # (Recorded before `origin` was.)
    return remote if plain_address(remote) else ""


def recorded_origin(git_folder: Path) -> str:
    """A clone's `origin` as its config says it when Chief of Staff takes it (read here as data,
    never by git): the address it is pushed to from then on; "" for none, or one that would run
    a program."""
    entries = dict(privategit.config_entries(git_folder / "config") or [])
    url = entries.get("remote.origin.url", "")
    return url if plain_address(url) else ""


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
            return read_regular(pointer, 8, whole=False) == b"gitdir: "
    return False


def nested_first(checkouts: list[dict]) -> list[dict]:
    """Checkouts, those inside another's folder first (a worktree another agent made in the
    worker's own: Claude's .claude/worktrees/…), the rest in their order: one is removed before
    the worktree that holds it."""

    def inside(checkout: dict) -> bool:
        tree = str(checkout.get("worktree") or checkout.get("path") or "")
        return any(
            other is not checkout
            and (around := str(other.get("worktree") or other.get("path") or ""))
            and tree.startswith(around.rstrip(os.sep) + os.sep)
            for other in checkouts
        )

    return sorted(checkouts, key=lambda checkout: not inside(checkout))


def tidy_claude_folder(tree: str):
    """A worktree removed from Claude's folder (<folder>/.claude/worktrees/<name>): the folders
    it leaves empty go too, so the worktree that held it is clean again."""
    path = Path(tree)
    if path.parent.name != "worktrees" or path.parent.parent.name != ".claude":
        return
    for folder in (path.parent, path.parent.parent):
        try:
            folder.rmdir()  # Only when empty.
        except OSError:
            return


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


async def carry_ignored_files(top: Path, tree: Path, under: Path | None = None) -> list[str]:
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
    paths = [p for p in ignored.split("\0") if p]
    return await asyncio.to_thread(copy_files, top, tree, paths, under)


def copy_files(
    source: Path, target: Path, paths: list[str], under: Path | None = None
) -> list[str]:
    """Copy these files of the owner's checkout `source` into the worktree `target` (made in
    `under`, which no worker can write: see place_tree), never through a link there."""
    copied, size = [], 0
    for relative in paths:
        origin, parts = source / relative, Path(relative).parts
        if not parts or ".." in parts or not origin.is_file():
            continue
        try:
            with safefs.open_folder(
                target.joinpath(*parts[:-1]), under or target.parent, create=True
            ) as folder:
                if folder.lstat(parts[-1]) is not None:
                    continue
                size += origin.stat().st_size
                if size > MAX_CARRIED_BYTES:
                    break
                folder.copy_in(origin, parts[-1])
        except OSError:
            continue
        copied.append(relative)
    return copied


async def carry_changes(top: Path, tree: Path, under: Path) -> tuple[list[str], list[str]]:
    """The owner's uncommitted work in their checkout `top`, copied as it is there into `tree`,
    a worktree of its commit (made in `under`, see place_tree): each file git says changed or is
    new (not an ignored one), and one deleted there deleted. What was copied, and what was left
    out ("name (why)")."""
    listed = await git(
        "--no-optional-locks", "-C", str(top), "status", "--porcelain", "-z",
        "--untracked-files=all", limit=60,
    )  # fmt: skip
    return await asyncio.to_thread(copy_changes, top, tree, status_paths(listed), under)


def status_paths(listed: str) -> list[str]:
    """The paths `git status --porcelain -z` names: a rename's old one too."""
    words, found, at = listed.split("\0"), [], 0
    while at < len(words):
        word, at = words[at], at + 1
        if len(word) > 3:
            found.append(word[3:])
            if "R" in word[:2] or "C" in word[:2]:
                found += [words[at]] if at < len(words) and words[at] else []
                at += 1
    return list(dict.fromkeys(found))


def copy_changes(
    source: Path, target: Path, paths: list[str], under: Path
) -> tuple[list[str], list[str]]:
    """These files of the owner's checkout `source` made in `target` as they are there (see
    carry_changes): one gone there (or a folder now) is deleted, first, then the others are
    copied: a regular file, or a link that leads to a place in the folder. Never through a link
    on either side, nor in a `.git`; at most MAX_SNAPSHOT_FILES, each up to MAX_SNAPSHOT_FILE and
    MAX_CARRIED_BYTES in all."""
    copied: list[str] = []
    left: list[str] = []
    present: list[tuple[str, tuple[str, ...]]] = []
    for path in paths:
        parts = Path(path.rstrip("/")).parts
        if not parts or ".." in parts or any(part.casefold() == ".git" for part in parts):
            continue
        if path.endswith("/"):
            left.append(f"{path.rstrip('/')} (a repository of its own)")
            continue
        try:
            with safefs.open_folder(source.joinpath(*parts[:-1]), source) as there:
                found = there.lstat(parts[-1])
        except FileNotFoundError:
            found = None
        except OSError:  # (A link, or a file, where a folder was.)
            left.append(f"{path} (could not be read)")
            continue
        if found is not None and not stat.S_ISDIR(found.st_mode):
            present.append((path, parts))
        elif dropped(target, parts, under):
            copied.append(path)  # (A folder where a file was: its files are named on their own.)
        elif found is not None:
            left.append(f"{path} (a repository of its own)")  # A submodule.
    size, more = 0, []
    for index, (path, parts) in enumerate(present):
        if len(copied) >= MAX_SNAPSHOT_FILES:
            more = [f"{len(present) - index} more files (over {MAX_SNAPSHOT_FILES})"]
            break
        try:
            with (
                safefs.open_folder(source.joinpath(*parts[:-1]), source) as there,
                safefs.open_folder(target.joinpath(*parts[:-1]), under, create=True) as folder,
            ):
                why, made = copy_one(there, folder, parts, size)
        except OSError:
            why, made = "could not be read", 0
        if why:
            left.append(f"{path} ({why})")
            continue
        size += made
        copied.append(path)
    return copied, [*sorted(left), *more]


def dropped(target: Path, parts: tuple[str, ...], under: Path) -> bool:
    """Whether a file (or link) at these names in `target` was deleted (not a folder)."""
    with suppress(OSError), safefs.open_folder(target.joinpath(*parts[:-1]), under) as folder:
        found = folder.lstat(parts[-1])
        if found is not None and not stat.S_ISDIR(found.st_mode):
            folder.delete(parts[-1])
            return True
    return False


def copy_one(
    there: safefs.Folder, folder: safefs.Folder, parts: tuple[str, ...], size: int
) -> tuple[str, int]:
    """One file of the owner's (or a link) copied from its folder `there` into `folder`, by its
    name, with `size` bytes copied before it: ("", its size), or (why not, 0)."""
    name = parts[-1]
    found = there.lstat(name)
    if found is None:
        return "gone meanwhile", 0
    if stat.S_ISLNK(found.st_mode):
        leads = there.readlink(name)
        inside = Path(os.path.normpath(Path(*parts[:-1], leads))).parts
        if (
            Path(leads).is_absolute()
            or inside[:1] == ("..",)
            or any(part.casefold() == ".git" for part in inside)
        ):
            return "a link out of the folder", 0
        folder.symlink(name, leads, replace=True)
        return "", 0
    if not stat.S_ISREG(found.st_mode):
        return "not a file", 0
    if found.st_size > MAX_SNAPSHOT_FILE:
        return f"over {MAX_SNAPSHOT_FILE // 1_000_000} MB", 0
    if size + found.st_size > MAX_CARRIED_BYTES:
        return f"past {MAX_CARRIED_BYTES // 1_000_000} MB in all", 0
    return "", folder.copy_from(there, name, MAX_SNAPSHOT_FILE)


def place_tree(staged: Path, workspace: Path, under: Path, name: str, exact: bool = False) -> Path:
    """Move a checkout made in Chief of Staff's own staging folder (no worker writes there:
    git, the copies and carried files are made out of every worker's reach) into a worker's
    workspace in one rename, from folders opened without following a link. Named `name`, or
    the first of name-2, name-3… that is free (`exact`: that name or nothing). Its path."""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.") or "repo"
    with (
        safefs.open_folder(staged.parent, staged.parent.parent) as source,
        safefs.open_folder(workspace, under, create=True) as into,
    ):
        for number in range(1, 200):
            candidate = name if number == 1 else f"{name}-{number}"
            if into.lstat(candidate) is None:
                try:
                    source.move(staged.name, candidate, into)
                    return workspace / candidate
                except OSError as exc:
                    if exact or exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR):
                        raise
            if exact:
                break
    raise FileExistsError(errno.EEXIST, f"{workspace / name} is taken")


def repoint(tree: Path, under: Path, admin: str, repository: Path) -> None:
    """A worktree moved into place (place_tree) and its repository told where it is, as git
    worktree move does, never through a link: its .git file and its folder's gitdir, with `/`
    between their folders as git writes them (Git for Windows doesn't find a worktree whose
    gitdir says `\\`: `git worktree remove` of it fails, and it lists it as `…\\.git`)."""
    with safefs.open_folder(tree, under) as folder:
        folder.write(".git", f"gitdir: {Path(admin).as_posix()}\n")
    with safefs.open_folder(Path(admin), repository) as folder:
        folder.write("gitdir", f"{(tree / '.git').as_posix()}\n")


def own_tree(checkouts: list[dict], top: str, kept: str) -> dict | None:
    """A worker's own worktree of the repository whose checkout is at `top` (real paths), with
    "at": where the owner's checkout `kept` (a folder in it) is in it; {} when that folder is
    not there (removed); None when it has none."""
    for checkout in checkouts:
        repo = checkout.get("repo") or ""
        if checkout.get("shared") or checkout.get("remote") or not repo:
            continue
        if os.path.realpath(repo) == top:
            place = Path(checkout.get("worktree") or checkout["path"]) / os.path.relpath(kept, top)
            return {**checkout, "at": str(place)} if place.is_dir() else {}
    return None


def place_in(copy: str, kept: str, folder: str) -> str:
    """Where a command run in `folder` runs in a copy (at `copy`) of the owner's checkout `kept`
    (its real path): the same folder there, when it ran in one of that checkout's and the copy
    has it; else the copy itself."""
    real = os.path.realpath(folder) if folder else ""
    if real and real != kept and within(real, kept):
        place = Path(copy) / os.path.relpath(real, kept)
        if place.is_dir():
            return str(place)
    return copy


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
            text = read_regular(root / name, 20000, follow=True, whole=False).decode(
                errors="replace"
            )
            if match := re.search(pattern, text):
                return match.group(1).rsplit("/", 1)[-1][:60]
    return ""


def readme_line(project: Path) -> str:
    """The first meaningful line of a project's README, as a hint of what the project is."""
    for name in ("README.md", "README.rst", "README.txt", "README"):
        path = project / name
        if path.is_file():
            try:
                data = read_regular(path, 65536, follow=True, whole=False)
            except OSError:
                continue
            for line in data.decode("utf-8", errors="replace").splitlines()[:40]:
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


def pointed(tree: Path) -> str:
    """Where a worktree's `.git` file points (its own folder in the repository), "" for none."""
    try:
        named = read_regular(tree / ".git", 65536, follow=True).decode(errors="replace").strip()
    except OSError:
        return ""
    return (
        os.path.realpath(named.removeprefix("gitdir:").strip())
        if named.startswith("gitdir:")
        else ""
    )


def watched_git_paths(
    repositories: list[str], checkouts: list[dict], modules: bool = True
) -> list[str]:
    """The files of a repository's .git that, made by a worker, would change what git runs or
    which repository it uses: a `commondir` (git then reads another .git), a `config.worktree`
    where the repository reads one (extensions.worktreeConfig), a `hooks` folder where it had
    none, and (`modules`: where the sandbox holds them, so no owner's own git is mistaken for a
    worker's) a submodules' folder, which the owner's `git submodule update` would reuse, in a
    repository with submodules. Not a `shallow`, alternates, `info/`: what objects git has, each
    checked by its hash."""
    found: list[str] = []
    admins = [c.get("admin") or "" for c in checkouts if c.get("worktree")]
    trees = [Path(c["path"]) for c in checkouts if c.get("path")]
    for common in repositories:
        root = Path(common)
        found += [str(root / "commondir"), str(root / "hooks")]
        if modules and any((tree / ".gitmodules").exists() for tree in [root.parent, *trees]):
            found.append(str(root / "modules"))
        if worktree_config(root):
            found.append(str(root / "config.worktree"))
            found += [str(Path(a) / "config.worktree") for a in admins if a.startswith(common)]
    return found


def worktree_config(root: Path) -> bool:
    """Whether a repository reads worktrees' own config (extensions.worktreeConfig)."""
    try:
        text = read_regular(root / "config", follow=True).decode(errors="replace")
    except OSError:
        return False
    section = ""
    for line in text.splitlines():
        line = line.split("#", 1)[0].split(";", 1)[0].strip()
        if line.startswith("["):
            section = line.strip("[]").strip().lower()
        elif section == "extensions" and "=" in line:
            key, _, value = line.partition("=")
            if key.strip().lower() == "worktreeconfig":
                return value.strip().lower() in {"true", "yes", "on", "1"}
    return False


def replaceable_git_paths(checkouts: list[dict], writable: list[str]) -> list[str]:
    """The folders of a repository's .git a worker's commands may write whose replacing would
    put what it wrote where git reads it. Copilot's sandbox holds paths, not folders: a folder
    moved into place brings what is in it past those paths (a `.git` of its own with a config
    that runs code, an `objects/info` with alternates, a worktree's private folder with its
    own commondir). Each is told by its identity (identity_of), looked at before each turn,
    after each, and before Chief of Staff runs git: the `.git` of an owner's checkout it works
    in itself, and in each repository it commits to, `objects`, `objects/info` and the private
    folders of the worktrees there."""
    found: dict[str, None] = {}
    reals = [os.path.realpath(path) for path in writable]
    for checkout in checkouts:
        common = checkout.get("git_dir") or ""
        if not common or checkout.get("remote"):
            continue
        if checkout.get("shared"):
            top = Path(checkout.get("top") or Path(common).parent)
            if any(os.path.realpath(top) == real for real in reals):
                found[str(top / ".git")] = None
        found[str(Path(common) / "objects")] = None
        found[str(Path(common) / "objects" / "info")] = None
        with suppress(OSError):
            found |= dict.fromkeys(
                str(entry) for entry in sorted((Path(common) / "worktrees").iterdir())
                if entry.is_dir()
            )  # fmt: skip
    return list(dict.fromkeys(os.path.realpath(path) for path in found))


def holds_alternates(path: str) -> bool:
    """Whether an `objects/info` holds what makes git take objects from elsewhere."""
    return any(os.path.lexists(Path(path) / n) for n in ("alternates", "http-alternates"))


def identity_of(path: str) -> list:
    """What is at a path now: [kind, device, inode] (a .git file's pointer, a link's target
    too); [] when nothing is. A folder moved into its place is another."""
    try:
        info = os.lstat(path)
    except OSError:
        return []
    if stat.S_ISDIR(info.st_mode):
        return ["d", info.st_dev, info.st_ino]
    if stat.S_ISLNK(info.st_mode):
        with suppress(OSError):
            return ["l", info.st_dev, info.st_ino, str(Path(path).readlink())]
    if stat.S_ISREG(info.st_mode) and info.st_size <= 4096:
        with suppress(OSError):
            return [
                "f",
                info.st_dev,
                info.st_ino,
                read_regular(path, 4096).decode(errors="replace"),
            ]
    return ["o", info.st_dev, info.st_ino]


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
            text = read_regular(pointer, 65536, follow=True).decode() if pointer.is_file() else ""
            if text.startswith("gitdir:"):
                own.add(Path(text.removeprefix("gitdir:").strip()).resolve())
    # Pointers that tell git where a repository is: a worker that rewrites them sends git (the
    # owner's, or Chief of Staff's) to a config it wrote. A repository's own .git has no
    # commondir; a worker must not make one. Nor the other files git reads for configuration
    # (config.worktree, with extensions.worktreeConfig), attributes (info/), which commits are
    # whole (shallow) or where else objects come from (alternates): absent ones are held too
    # (sandbox_paths).
    paths = [
        root / "hooks",
        root / "config",
        root / "commondir",
        root / "config.worktree",
        root / "info",
        root / "shallow",
        root / "objects" / "info" / "alternates",
        root / "objects" / "info" / "http-alternates",
    ]
    for admin in sorted(own):
        paths += [admin / "commondir", admin / "gitdir", admin / "config.worktree"]
    for checkout in checkouts:
        pointer = Path(checkout.get("worktree") or "") / ".git"
        if checkout.get("worktree") and pointer.is_file():
            paths.append(pointer)
    if not shared:
        # The owner's own checkout: its branch and staged changes are theirs. (A worker that
        # works in the owner's checkout itself needs them.)
        paths += [root / name for name in ("HEAD", "index") if (root / name).exists()]
    paths.append(root / "modules")  # Its submodules' git folders (a worktree's are its own).
    if (root / "worktrees").is_dir():
        paths += sorted(
            path
            for path in (root / "worktrees").iterdir()
            if path.is_dir() and path.resolve() not in own
        )
    return [str(path) for path in paths]


# Copilot's sandbox holds a listed path only if it exists when the session starts (CLI 1.0.112,
# checked on macOS: a command creates one that does not, in a read-only folder's place or not).
# Denied, an absent path is held on macOS, and nothing is left behind. On Linux the sandbox
# mounts an empty folder in its place and leaves it on the disk (which breaks git where it reads
# a file, .git/commondir): an absent path is not listed there; Chief of Staff looks for those it
# must not find after each turn (Runtime.git_watch_check, SECURITY.md).
SANDBOX_PLATFORM = sys.platform
# Copilot before this leaves an empty folder at a denied path that did not exist (1.0.105,
# checked on macOS; 1.0.107 and later do not), as Linux's sandbox does: one that breaks git,
# where it reads a file (.git/commondir). With such a Copilot (ABSENT_HELD, as the runtime found
# it connected), absent paths are looked for after each turn, as on Linux.
ABSENT_HELD_CLI = "1.0.107"
ABSENT_HELD = True


def sandbox_platform() -> str:
    """The platform whose sandbox the worker's paths are held as: macOS's holds a denied path
    that does not exist (with a Copilot that leaves nothing there), Linux's does not."""
    if SANDBOX_PLATFORM == "darwin" and not ABSENT_HELD:
        return "linux"
    return SANDBOX_PLATFORM


def sandbox_paths(paths: Sequence[str]) -> tuple[list[str], list[str]]:
    """(read-only, denied): the paths a worker's commands must not change, as Copilot's sandbox
    holds them here: those that exist read-only, absent ones denied on macOS (not held on
    Linux: absent_paths)."""
    readonly: list[str] = []
    denied: list[str] = []
    for path in dict.fromkeys(paths):
        if os.path.lexists(path):
            readonly.append(path)
        elif sandbox_platform() == "darwin":
            # (A link to nowhere: where it leads too.)
            denied += dict.fromkeys([path, os.path.realpath(path)])
    return readonly, denied


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
        with suppress(OSError):
            data = read_regular(path, MAX_NEW_FILE, follow=True, whole=False)
            found.update(name.encode() + b"\0" + data)
    return found.hexdigest()


# Programs that write files they do not name on their command line (a script, a build, a
# git command that rewrites the tree): a file changed while one runs may be its work.
WRITES_UNNAMED = re.compile(
    r"^(python[\d.]*|py|node|deno|bun|ruby|perl|php|bash|sh|zsh|make|npm|npx|pnpm|yarn|uv|"
    r"poetry|pip|cargo|go|gradlew?|mvnw?|java|dotnet|xargs|find|rsync|tar|unzip|patch|"
    r"pytest|tox|nox|black|ruff|prettier|eslint|isort|gofmt|rustfmt|\./\S+|"
    r"git (apply|am|pull|merge|rebase|stash|cherry-pick|reset|checkout|switch|restore|revert))\b"
)


# Programs that only read the files they name (policy's READERS, but those that may write what
# they name: git's checkout or restore, sed's or perl's in place, awk's script, a sourced one).
READS_NAMED = policy.READERS - {"git", "sed", "perl", "awk", "gawk", "source", "."}
GIT_READS = frozenset({"status", "diff", "show", "log", "blame", "grep", "ls-files", "ls-tree",
                       "cat-file", "rev-parse", "shortlog", "describe"})  # fmt: skip
# `sed -n '1,80p'`: a script that prints lines, nothing more (no `w` to write a file).
SED_PRINTS = re.compile(r"(\d+|\$)?(,(\d+|\$|\+\d+))?p")


def reads_only(program: str) -> bool:
    """Whether a simple command (receipts.program's) only reads the files it names: `cat`,
    `grep`, `git diff`, `sed -n 1,80p`; not one that may write one (`sed -i`, `git checkout`,
    `git diff --output=…`), nor one not known."""
    words = program.split()
    if not words:
        return False
    if words[0] == "git":
        return (
            len(words) > 1
            and words[1] in GIT_READS
            and not any(w.startswith(("--output", "-O", "--open-files-in-pager")) for w in words)
        )
    if words[0] == "sed":
        scripts, given, rest = [], False, words[1:]
        for at, word in enumerate(rest):
            if given:
                given = False
            elif word in {"-e", "--expression"}:
                scripts.append(rest[at + 1] if at + 1 < len(rest) else "")
                given = True
            elif word.startswith("--"):
                if word.startswith(("--in-place", "--file", "--expression=")):
                    return False
            elif word.startswith("-"):
                if any(flag in word[1:] for flag in "ief"):
                    return False  # (In place, or a script from a file or with -eSCRIPT.)
            elif not scripts:
                scripts.append(word)
        return bool(scripts) and all(SED_PRINTS.fullmatch(script) for script in scripts)
    return words[0] in READS_NAMED


def redirection(word: str) -> bool:
    """Whether a word is a redirection that writes (`>`, `>>`, `&>`, `>|`): the next word is the
    file it writes."""
    return ">" in word and not word.strip("<>&|")


class CommandRun:
    """One of a task's shell commands, as far as files go: when it ran, the words that may name
    files it writes (quotes respected: "release notes.txt"; not those of a program that only
    reads them, `cat` or `grep`, but where it redirects), and whether it may write files it does
    not name (a glob, a variable, a script)."""

    def __init__(self, command: str, start: float = 0, end: float = 0):
        self.start, self.end = start, end
        words: set[str] = set()
        unnamed = False
        for parts in receipts.simple_commands(command):
            program = receipts.program(parts)
            reads = reads_only(program)
            first = next((w for w in parts if not re.fullmatch(r"\w+=.*", w)), "")
            if not reads and (
                WRITES_UNNAMED.match(program) or first.startswith(("./", "../", "/", "~/"))
            ):
                unnamed = True  # A tool, or a script of the project's (`./gen.sh`).
            target = False
            for word in parts:
                written, target = target, redirection(word)
                if target or (reads and not written):
                    continue  # (A redirection's sign; a file it only reads.)
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


# A folder of Chief of Staff's own for its temporary files, where no worker writes (the
# system's temporary folder is every sandboxed worker's to write): set by Workspaces.
PRIVATE_TEMP: list[Path] = []


def private_temp() -> str | None:
    if not PRIVATE_TEMP:
        return None
    PRIVATE_TEMP[0].mkdir(parents=True, exist_ok=True, mode=0o700)
    return str(PRIVATE_TEMP[0])


async def gitlinks(checkout: dict, top: str) -> list[str]:
    """The submodules (and repositories added in it) a checkout's index holds, by path. `git
    add` looks into the work tree of each (its config there runs: the worker's to write, see
    INTO_SUBMODULES), so Chief of Staff's own adds leave them out (git_unlooked)."""
    staged = await checkout_git(checkout, top, "ls-files", "--stage", "-z")
    found = []
    for entry in staged.split("\0"):
        mode, _, path = entry.partition("\t")
        if mode.startswith("160000 ") and path:
            found.append(path)
    return found


def git_unlooked(paths: list[str]) -> list[str]:
    """Pathspecs that leave these paths out of a git command."""
    return [f":(exclude,literal){path}" for path in paths]


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
        with tempfile.TemporaryDirectory(prefix="cos-snap-", dir=private_temp()) as folder:
            own = str(Path(folder) / "index")
            with suppress(FileNotFoundError):
                await asyncio.to_thread(shutil.copyfile, index, own)
            env = {"GIT_INDEX_FILE": own}
            # New files too large to copy are left out (and listed: a task's large new file);
            # submodules too, as the index holds them (git_unlooked).
            left_out = git_unlooked([*large, *await gitlinks(checkout, top)])
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
    except UnreadableError:
        raise  # (Said in its receipts: see TaskActions.collected.)
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
    if live and checkout.get("remote") and (unchecked := await unchecked_files(checkout, path)):
        result["unchecked"] = unchecked
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


async def unchecked_files(checkout: dict, path: str) -> int:
    """In a clone in a worker's folder: the files a filter keeps (git-lfs's) that may have
    changed (their time or size no longer as its index says; read no further) or are new. Their
    content is not read there (private_git), so not counted either; 0 elsewhere."""
    found: set[str] = set()
    with suppress(ValueError):
        for arguments in (("diff-files", "--name-only", "-z"),
                          ("ls-files", "--others", "--exclude-standard", "-z")):  # fmt: skip
            listed = await checkout_git(checkout, path, *arguments, filtered=True)
            found.update(name for name in listed.split("\0") if name)
    return len(found)


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
            data = read_regular(file, MAX_NEW_FILE)
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
        data = read_regular(path, MAX_NEW_FILE + 1, whole=False)
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
STAGING = ".chiefofstaff-staging"  # Beside workers' folders: Workspaces.staging.
OLD_TRASH = ".trash"
TRASHED = re.compile(r"^[0-9a-f]{12}-")
# How long git may take to remove a worktree (a huge one, a slow disk).
REMOVE_SECONDS = 900
# A worktree whose removal started (kv, by its path) and has not ended: if Chief of Staff stopped
# meanwhile, what is left is no tree to resume in (restore() clears it, then makes it again).
REMOVING = "removing:"


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
# and submodules moved to other commits (diff.ignoreSubmodules, submodule.*.ignore). Never what
# is in a submodule's work tree (without_submodules): status_lines says each one there is.
ALL_STATUS = ("status", "--porcelain", "--untracked-files=normal")
# For git's own check before it removes a worktree (a `git status` it runs without -u).
SHOW_UNTRACKED = ("-c", "status.showUntrackedFiles=normal")


async def status_lines(checkout: dict, tree: str) -> list[str] | None:
    """A worktree's `git status --porcelain` lines, all of them (see ALL_STATUS), and each
    submodule checked out in it as changed (" M lib": what is in it is never read, and it may
    hold work); None when git cannot tell."""
    try:
        status = await checkout_git(checkout, tree, *ALL_STATUS, limit=REMOVE_SECONDS)
        lines = [line for line in status.splitlines() if line.strip()]
        named = {status_name(line) for line in lines}
        for path in await gitlinks(checkout, tree):
            if path not in named and await asyncio.to_thread(
                os.path.lexists, Path(tree) / path / ".git"
            ):
                lines.append(f" M {path}")
    except ValueError:
        return None
    return lines


async def submodule_work(checkout: dict, tree: str) -> tuple[list[str], list[tuple[str, str]]]:
    """The submodules checked out in a worktree that hold work not committed in it: a commit of
    their own its index doesn't record, or edited files, new ones that are work (what `git
    status` there shows); and those that can't be read so, with why: each by its path. As the
    owner's repository says to leave one out (`submodule.<name>.ignore` in its config; not in
    `.gitmodules`, which the worker can write). A submodule's git folder is in its worktree's
    own, which the worker can write: it is read through a git folder of Chief of Staff's own
    (private_git), never through its config."""
    changed: list[str] = []
    unread: list[tuple[str, str]] = []
    ignored = await submodules_ignored(checkout, tree)
    staged = await checkout_git(checkout, tree, "ls-files", "--stage", "-z")
    for entry in staged.split("\0"):
        mode, _, path = entry.partition("\t")
        if not mode.startswith("160000 ") or not path:
            continue
        place, ignore = Path(tree) / path, ignored.get(path, "")
        if ignore == "all" or not await asyncio.to_thread(os.path.lexists, place / ".git"):
            continue
        shown = "no" if ignore == "untracked" else "normal"
        try:
            common = await asyncio.to_thread(submodule_git_dir, place, tree)
            head = await private_git(common, "", str(place), "", str(place), ("rev-parse", "HEAD"))
            status = "" if ignore == "dirty" else await private_git(
                common, "", str(place), "", str(place),
                ("status", "--porcelain", f"--untracked-files={shown}"), limit=REMOVE_SECONDS,
            )  # fmt: skip
        except TimedOutError:
            unread.append((path, "reading it took too long"))
            continue
        except (ValueError, OSError) as exc:
            logger.info("Could not read the submodule %s of %s: %s", path, tree, exc)
            why = exc.why if isinstance(exc, UnreadableError) else str(exc)
            why = why.removeprefix("git failed: ")
            unread.append((path, why[:120] or "git could not read it"))
            continue
        if head.strip() != mode.split()[1] or worth_committing(status):
            changed.append(path)
    return changed, unread


def submodule_git_dir(place: Path, tree: str) -> str:
    """The git folder of a submodule checked out at `place` in the worktree `tree`: the one its
    `.git` names (or is), read here. ValueError when that is no plain repository of its own (or
    is reached through a link)."""
    if Path(os.path.realpath(place)) != Path(os.path.realpath(tree)) / place.relative_to(tree):
        raise ValueError("its folder is reached through a link")
    dot = place / ".git"
    info = dot.lstat()
    if stat.S_ISDIR(info.st_mode):
        git_dir = os.path.realpath(dot)
    elif stat.S_ISREG(info.st_mode):
        named = read_regular(dot, 65536).decode(errors="replace").strip()
        if not named.startswith("gitdir:"):
            raise ValueError("its .git names no git folder")
        git_dir = os.path.realpath(place / named.removeprefix("gitdir:").strip())
    else:
        raise ValueError("its .git is no file or folder")
    if not Path(git_dir).is_dir() or os.path.lexists(Path(git_dir) / "commondir"):
        raise ValueError("its git folder is no repository of its own")
    return git_dir


async def submodules_ignored(checkout: dict, tree: str) -> dict[str, str]:
    """What a worktree's repository says `git status` leaves out of each submodule (by path):
    `submodule.<name>.ignore` in its config only (the owner's: a worker's clone's own is never
    read), each named by its path in `.gitmodules`."""
    names: dict[str, str] = {}
    ignore: dict[str, str] = {}
    entries = await asyncio.to_thread(config_entries, Path(tree) / ".gitmodules") or []
    for key, value in entries:
        section, _, rest = key.partition(".")
        name, _, setting = rest.rpartition(".")
        if section.lower() == "submodule" and name and setting.lower() == "path":
            names[name] = value
    with suppress(ValueError):
        listed = await checkout_git(
            checkout, tree, "config", "-z", "--get-regexp", r"^submodule\..*\.ignore$"
        )
        for key, _, value in (e.partition("\n") for e in listed.split("\0") if e):
            ignore[key.partition(".")[2].rpartition(".")[0]] = value.strip().lower()
    return {path: ignore[name] for name, path in names.items() if name in ignore}


async def uncommitted(checkout: dict, tree: str) -> list[str] | None:
    """The files git would refuse to lose with a worktree: changed ones, and new ones it does not
    ignore; in a copy of the owner's checkout (make_checkout `copy`), not those the owner's
    checkout holds the same (theirs, copied in). None when git cannot tell (then nothing may be
    taken for clean)."""
    lines = await status_lines(checkout, tree)
    return None if lines is None else await its_work(checkout, tree, lines)


async def its_work(checkout: dict, tree: str, lines: list[str]) -> list[str]:
    """The files a worktree's status lines name; in a copy of the owner's checkout (make_checkout
    `copy`), not those the owner's checkout holds the same (theirs, copied in)."""
    names = [status_name(line) for line in lines]
    if checkout.get("copy") and names:
        return await asyncio.to_thread(not_owners, Path(tree), Path(checkout["repo"]), names)
    return names


def not_owners(tree: Path, repo: Path, names: list[str]) -> list[str]:
    """Of these files changed in a copy of the owner's checkout `repo` (or new folders: each
    file in them), those the owner's checkout does not hold the same, never read through a
    link."""

    def same(name: str) -> bool:
        mine, theirs = tree / name, repo / name
        try:
            one = os.lstat(mine) if os.path.lexists(mine) else None
            other = os.lstat(theirs) if os.path.lexists(theirs) else None
            if one is None or other is None:
                return one is None and other is None
            if stat.S_ISLNK(one.st_mode) and stat.S_ISLNK(other.st_mode):
                return mine.readlink() == theirs.readlink()
            if not (stat.S_ISREG(one.st_mode) and stat.S_ISREG(other.st_mode)):
                return False
            return one.st_size == other.st_size and read_regular(
                mine, MAX_SNAPSHOT_FILE
            ) == read_regular(theirs, MAX_SNAPSHOT_FILE)
        except OSError:
            return False

    kept = []
    for name in names:
        if not name.endswith("/"):
            if not same(name):
                kept.append(name)
            continue
        files = [
            str(Path(folder, file).relative_to(tree))
            for folder, _, found in itertools.islice(os.walk(tree / name), 200)
            for file in found
        ]
        if not files or len(files) > MAX_CARRIED or not all(map(same, files)):
            kept.append(name)
    return kept


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
    that leads out of it (each folder is opened from the workspaces folder without following a
    link: one a worker swaps in meanwhile is refused), never a repository of its own (its
    commits may be nowhere else). The folder's place in trash, True for a file removed, False
    for nothing done."""
    relative = entry.rstrip("/")
    parts = relative.split("/")
    if not relative or {"", ".", ".."} & set(parts):
        return False
    try:
        with safefs.open_folder(Path(tree, *parts[:-1]), trash.parent) as parent:
            found = parent.lstat(parts[-1])
            if found is None:
                return False
            if not stat.S_ISDIR(found.st_mode):
                parent.delete(parts[-1])
                return True
            with parent.child(parts[-1]) as folder:
                if folder.lstat(".git") is not None:
                    return False
            return trash_in(parent, parts[-1], trash) or False
    except (FileNotFoundError, NotADirectoryError, ValueError, safefs.UnsafePathError):
        return False
    except OSError as exc:
        logger.warning("Could not delete %s: %s", Path(tree, relative), exc)
        return False


def to_trash(path: Path, trash: Path) -> Path | None:
    """Move a folder into trash in one step (in a thread): only on the same disk, where that is
    one rename, from its folder opened without following a link. Its place there, or None (gone
    already, another disk, a link on the way, an error)."""
    try:
        with safefs.open_folder(path.parent, trash.parent) as parent:
            return trash_in(parent, path.name, trash)
    except (FileNotFoundError, NotADirectoryError, ValueError, safefs.UnsafePathError):
        return None
    except OSError as exc:
        logger.warning("Could not delete %s: %s", path, exc)
        return None


def trash_in(parent: safefs.Folder, name: str, trash: Path) -> Path | None:
    """What is at `name` in the opened folder `parent`, moved into trash (see to_trash)."""
    try:
        with safefs.open_folder(trash, trash.parent, create=True) as kept:
            found = parent.lstat(name)
            if found is None:
                return None
            if trash.stat().st_dev != found.st_dev:
                logger.warning("Not deleting %s: it is on another disk than %s.", name, trash)
                return None
            target = f"{uuid4().hex[:12]}-{name}"
            parent.move(name, target, kept)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Could not delete %s: %s", parent.path / name, exc)
        return None
    DELETING.add(trash / target)  # Its own cleanup deletes it: not a leftover for another.
    return trash / target


def untrash(moved: Path, place: Path) -> bool:
    """Put back what trash_in moved (when it changed meanwhile), if its place is still free."""
    DELETING.discard(moved)
    try:
        with (
            safefs.open_folder(moved.parent, moved.parent) as kept,
            safefs.open_folder(place.parent, moved.parent.parent) as parent,
        ):
            if parent.lstat(place.name) is not None:
                return False
            kept.move(moved.name, place.name, parent)
            return True
    except (OSError, ValueError) as exc:
        logger.warning("Could not put %s back in its place: %s", moved, exc)
        return False


# What is in trash because a cleanup running now moved it there (until it is deleted).
DELETING: set[Path] = set()


# Folders writable() is deleting again: each only once (never a loop).
RETRYING: set[str] = set()


def writable(function: Callable, path: str, _error: object, under: Path | None = None):
    """rmtree's handler: what it could not delete or open because it or its folder is read-only
    or unreadable (Go's module cache, a tool's locked cache, a folder at mode 000) is made
    writable and readable and tried once more: a file or an empty folder is removed again, a
    folder it could not open or list is deleted with all in it. All from folders opened from
    `under` (trash) without following a link. Never raises (that would stop the whole delete):
    one owned by another user stays, and what is left is measured after."""
    target = Path(path)
    anchor = Path(under) if under is not None else target.parent
    try:
        for each in {target.parent, target}:
            if each != anchor and each.is_relative_to(anchor):
                with safefs.open_folder(each.parent, anchor) as folder:
                    folder.add_mode(each.name, stat.S_IRWXU)
        with safefs.open_folder(target.parent, anchor) as folder:
            found = folder.lstat(target.name)
            if found is None:
                return
            if not stat.S_ISDIR(found.st_mode):
                folder.delete(target.name)
            elif function is os.rmdir:
                folder.delete_empty(target.name)
            elif path not in RETRYING:
                RETRYING.add(path)
                try:
                    rmtree_writable(target, anchor)
                finally:
                    RETRYING.discard(path)
    except Exception as exc:
        logger.debug("Could not delete %s: %s", path, exc)


def rmtree_writable(folder: Path, under: Path | None = None):
    """A folder deleted with all in it, never following a link (shutil.rmtree by folder
    descriptors), with writable() for what it cannot delete at first."""
    anchor = Path(under) if under is not None else folder.parent

    def retry(function: Callable, path: str, error: object):
        writable(function, str(folder.parent / path), error, anchor)

    with safefs.open_folder(folder.parent, anchor) as parent:
        parent.remove(folder.name, onexc=retry)


def delete_moved(item: Path) -> bool:
    """Delete a file or folder moved into trash (in a thread): whether it is all gone."""
    try:
        with safefs.open_folder(item.parent, item.parent) as kept:
            found = kept.lstat(item.name)
            if found is not None and not stat.S_ISDIR(found.st_mode):
                kept.delete(item.name)
            elif found is not None:
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
        limit = 1 << 40
        return read_regular(one, limit, follow=True) == read_regular(other, limit, follow=True)
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


# Package caches Copilot's dev-tool access opens read-write to sandboxed commands in a project
# of their kind (seen in its policy files, CLI 1.0.111), and the like, by their default place in
# home: what the owner's builds later run, unsandboxed. Workers that read Slack get them
# read-only; to the others they stay open until they get caches of their own.
OWNER_CACHES = (
    ".npm", ".yarn/berry", ".pnpm-store", ".bun/install/cache", ".cache/uv", ".cache/pre-commit",
    ".cache/node/corepack", ".local/share/virtualenv", ".m2/repository", ".m2/wrapper",
    ".gradle/caches", ".gradle/wrapper", ".gradle/native", "go/pkg/mod", ".cargo/registry",
    ".cargo/git", ".node-gyp", ".electron-gyp", ".nuget/packages",
)  # fmt: skip
# Where the others are on each system; held there only when they exist elsewhere.
PLATFORM_CACHES = {
    "darwin": (
        "Library/pnpm", "Library/Caches/pip", "Library/Caches/pypoetry", "Library/Caches/go-build",
        "Library/Caches/Yarn", "Library/Caches/ms-playwright", "Library/Caches/electron",
        "Library/Caches/node-gyp", "Library/Caches/ccache", "Library/Caches/Mozilla.sccache",
    ),
    "linux": (
        ".local/share/pnpm", ".cache/pip", ".cache/pypoetry", ".cache/go-build", ".cache/yarn",
        ".cache/ms-playwright", ".cache/electron", ".cache/node-gyp", ".cache/ccache",
        ".cache/sccache",
    ),
}  # fmt: skip
# The same caches moved by an environment variable (Copilot opens those too): the variable, and
# the folders under it (none: the variable names the cache itself).
MOVED_CACHES = {
    "npm_config_cache": (), "NPM_CONFIG_CACHE": (), "UV_CACHE_DIR": (), "PIP_CACHE_DIR": (),
    "POETRY_CACHE_DIR": (), "PRE_COMMIT_HOME": (), "GOMODCACHE": (), "GOCACHE": (),
    "PLAYWRIGHT_BROWSERS_PATH": (), "BUN_INSTALL_CACHE_DIR": (), "YARN_CACHE_FOLDER": (),
    "NUGET_PACKAGES": (), "COREPACK_HOME": (), "GOPATH": ("pkg/mod",),
    "CARGO_HOME": ("registry", "git"), "GRADLE_USER_HOME": ("caches", "wrapper", "native"),
    "XDG_CACHE_HOME": ("uv", "pip", "pypoetry", "pre-commit", "go-build", "ms-playwright", "yarn"),
}  # fmt: skip
# Files where registry and other credentials live, which Copilot's dev-tool access lets sandboxed
# commands read (registry configuration): unreadable for workers that read Slack, which must not
# carry a token anywhere.
REGISTRY_SECRETS = (
    ".npmrc", ".yarnrc", ".yarnrc.yml", ".bunfig.toml", ".m2/settings.xml",
    ".m2/settings-security.xml", ".gradle/gradle.properties", ".gradle/init.d", ".pypirc",
    ".cargo/credentials", ".cargo/credentials.toml", ".config/pip/pip.conf", ".pip/pip.conf",
    "Library/Application Support/pip/pip.conf", ".config/uv/uv.toml",
    ".config/pypoetry/auth.toml", "Library/Application Support/pypoetry/auth.toml", ".netrc",
    ".docker/config.json", ".gem/credentials", ".nuget/NuGet/NuGet.Config",
    ".config/NuGet/NuGet.Config", ".bundle/config", ".gemrc", ".config/go/env",
    "Library/Application Support/go/env", ".git-credentials", ".config/git/credentials",
    ".config/gh/hosts.yml", ".aws/credentials", ".aws/config", ".azure", ".kube/config",
    ".config/gcloud", ".config/mise/config.toml",
)  # fmt: skip
# The same, where an environment variable puts them: the variable, and the file under it (none:
# the variable names the file itself).
MOVED_SECRETS = {
    "NPM_CONFIG_USERCONFIG": (), "npm_config_userconfig": (), "PIP_CONFIG_FILE": (),
    "UV_CONFIG_FILE": (), "NETRC": (), "DOCKER_CONFIG": ("config.json",),
    "MISE_CONFIG_DIR": ("config.toml",), "MISE_GLOBAL_CONFIG_FILE": (),
    "GRADLE_USER_HOME": ("gradle.properties", "init.d"),
    "CARGO_HOME": ("credentials", "credentials.toml"), "POETRY_CONFIG_DIR": ("auth.toml",),
    "NPM_CONFIG_GLOBALCONFIG": (), "npm_config_globalconfig": (), "BUNDLE_USER_CONFIG": (),
    "GEMRC": (), "GOENV": (), "GH_CONFIG_DIR": ("hosts.yml",), "AWS_SHARED_CREDENTIALS_FILE": (),
    "AWS_CONFIG_FILE": (), "AZURE_CONFIG_DIR": (), "KUBECONFIG": (),
    "CLOUDSDK_CONFIG": (),
}  # fmt: skip


def named(home: Path, names: Sequence[str], moved: dict[str, tuple[str, ...]]) -> list[Path]:
    """These paths in home, and where the environment moved them."""
    found = [home / name for name in names]
    for variable, parts in moved.items():
        for value in os.environ.get(variable, "").split(os.pathsep):  # KUBECONFIG: a list.
            if value.strip() and (base := Path(value.strip()).expanduser()).is_absolute():
                found += [base / part for part in parts] if parts else [base]
    return list(dict.fromkeys(found))


def held_caches(home: Path | None = None) -> tuple[list[str], list[str]]:
    """(read-only, denied): the owner's package caches for a worker that reads Slack. One of
    this system's that does not exist yet is denied, so that it cannot make one the owner's
    builds would then use (Copilot leaves an empty folder there: an empty cache changes
    nothing); another system's is held only where it exists."""
    home = home or Path.home()
    system = "darwin" if sys.platform == "darwin" else "linux"
    ours = named(home, [*OWNER_CACHES, *PLATFORM_CACHES[system]], MOVED_CACHES)
    others = [
        home / name for kind, names in PLATFORM_CACHES.items() if kind != system for name in names
    ]
    readonly: list[str] = []
    denied: list[str] = []
    for path in [*ours, *others]:
        if too_broad(str(path)) or str(path) in readonly or str(path) in denied:
            continue
        if path.exists():
            readonly.append(str(path))
        elif path in ours:
            denied.append(str(path))
    return readonly, denied


def registry_secrets(home: Path | None = None) -> list[str]:
    """The owner's registry and credential files that exist, to deny a worker that reads Slack:
    in home, where the environment puts them, and npm's global configuration (beside the node
    on PATH). Not one that does not exist: the sandbox would leave an empty folder in its place,
    which breaks the tool that later looks for a file there."""
    found = named(home or Path.home(), REGISTRY_SECRETS, MOVED_SECRETS)
    if node := shutil.which("node"):
        for binary in (node, os.path.realpath(node)):
            found.append(Path(binary).parent.parent / "etc" / "npmrc")
    return [
        str(path)
        for path in dict.fromkeys(found)
        if os.path.lexists(path) and not too_broad(str(path))
    ]


def own_code(
    prefix: str = sys.prefix,
    base: str = sys.base_prefix,
    package: Path = Path(__file__).parent,
    launchers: Sequence[str] | None = None,
    paths: Sequence[str] | None = None,
) -> list[str]:
    """Chief of Staff's own code and the Python it runs on, read-only for every sandboxed
    worker, so that no worker's command changes what Chief of Staff (unsandboxed, with the
    owner's tokens) runs: its environment and Python, its package, every folder it imports from
    (sys.path, as real paths), its command (a uv tool's link to it, as found on PATH and as
    started), what a source checkout it runs from is built from (pyproject.toml, uv.lock), and
    where in uv's cache its packages come from (uv_sources). Not uv's link to uvx's environment
    (environments-v2/<python>/<hash>): the sandbox resolves links, so holding it holds its
    environment, which already is, and holding its folder would hold every environment uv makes
    for that Python (uvx, uv run --with). Slow on a big uv cache: looked up once a run, off the
    event loop (Runtime.connect)."""
    if launchers is None:
        started = sys.argv[0] if sys.argv and Path(sys.argv[0]).is_absolute() else ""
        launchers = [shutil.which("chiefofstaff") or "", started]
    found = [prefix, base, str(package)]
    for launcher in launchers:
        if launcher:
            found += [launcher, os.path.realpath(launcher)]
    found += [os.path.realpath(path) for path in (sys.path if paths is None else paths) if path]
    found += built_from(package)
    found += uv_sources(prefix)
    return [
        path
        for path in dict.fromkeys(path for path in found if path and Path(path).is_absolute())
        if os.path.lexists(path) and not too_broad(path)
    ]


def built_from(package: Path) -> list[str]:
    """The project files of the source checkout Chief of Staff runs from (an editable install,
    uv run), which uv syncs its environment from: none for an installed package."""
    for folder in (package.parent, package.parent.parent):
        project = folder / "pyproject.toml"
        with suppress(OSError, UnicodeDecodeError):
            if re.search(r'^name\s*=\s*"chiefofstaff"', project.read_text(), re.MULTILINE):
                return [str(project), str(folder / "uv.lock")]
    return []


def uv_sources(prefix: str) -> list[str]:
    """Where in uv's cache Chief of Staff's packages come from, which uv links into a new
    environment when it makes Chief of Staff's again: the unpacked wheels of the distributions
    installed in its environment (by name and version), and, where uv installed them by hard
    links (Linux), other environments holding the same files, which a write through them would
    change in Chief of Staff's too."""
    site = Path(sysconfig_purelib(prefix))
    try:
        installed = {entry.name for entry in site.iterdir() if entry.name.endswith(".dist-info")}
    except OSError:
        return []
    linked = hard_links(site, installed)
    real = os.path.realpath(prefix)
    found: list[str] = []
    for cache in uv_caches(prefix):
        for archive in sorted(Path(cache).glob("archive-v*")):
            with suppress(OSError):
                for entry in os.scandir(archive):
                    folder = Path(entry.path)
                    if entry.is_dir() and str(folder.resolve()) != real:
                        found += [entry.path] if holds(folder, installed, linked) else []
    return found


def hard_links(site: Path, installed: set[str]) -> dict[str, tuple[str, tuple[int, int]]]:
    """For each distribution installed by hard links: one of its files (as its RECORD names
    it, from site-packages) and that file's identity."""
    found = {}
    for name in installed:
        with suppress(OSError, UnicodeDecodeError):
            for line in (site / name / "RECORD").read_text().splitlines():
                relative = line.split(",", 1)[0]
                if not relative or relative.startswith(name):
                    continue
                status = (site / relative).stat()
                if status.st_nlink > 1:
                    found[name] = (relative, (status.st_dev, status.st_ino))
                break
    return found


def holds(entry: Path, installed: set[str], linked: dict[str, tuple[str, tuple[int, int]]]) -> bool:
    """Whether an entry of uv's archive is an unpacked wheel of one of these distributions (its
    .dist-info at the top), or an environment sharing files with them."""
    with suppress(OSError):
        names = {child.name for child in entry.iterdir()}
        if names & installed:
            return True
        if linked and "pyvenv.cfg" in names:
            for site in entry.glob("lib*/python*/site-packages"):
                for relative, identity in linked.values():
                    with suppress(OSError):
                        status = (site / relative).stat()
                        if (status.st_dev, status.st_ino) == identity:
                            return True
    return False


def sysconfig_purelib(prefix: str) -> str:
    """The site-packages folder of the environment at this prefix."""
    import sysconfig

    scheme = "nt" if os.name == "nt" else "posix_prefix"
    return sysconfig.get_path("purelib", scheme, vars={"base": prefix, "platbase": prefix})


def uv_caches(prefix: str) -> list[str]:
    """uv's cache folders that may hold this environment's sources: the one it runs from (uvx),
    and the one uv uses ($UV_CACHE_DIR, else $XDG_CACHE_HOME/uv, else ~/.cache/uv)."""
    found = []
    parts = Path(prefix).parts
    for index, part in enumerate(parts):
        if part.startswith("archive-v") and index:
            found.append(str(Path(*parts[:index])))
    if value := os.environ.get("UV_CACHE_DIR", "").strip():
        found.append(value)
    elif (xdg := os.environ.get("XDG_CACHE_HOME", "").strip()) and Path(xdg).is_absolute():
        found.append(str(Path(xdg) / "uv"))
    else:
        found.append(str(Path.home() / ".cache" / "uv"))
    return [path for path in dict.fromkeys(found) if Path(path).is_dir()]


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
        PRIVATE_TEMP[:] = [work_root / "tmp"]
        # Chief of Staff's own git folders for repositories workers can write (privategit): in
        # its data folder, out of every worker's reach.
        privategit.ROOT = work_root / "git"
        self.project_cache: dict[str, tuple[float, dict]] = {}
        self.known: tuple[float, tuple, list[str]] = (0.0, (), [])
        self.clone_lock = asyncio.Lock()
        # The token of the gh account that can see a repository ("" for gh's active one).
        self.github_token: Callable[[str], Awaitable[str]] | None = None
        # Told what release() removed and kept (with why), for the Storage view; and asked why
        # a closed worker's worktrees must stay as they are ("" when they need not).
        self.on_release: Callable[[dict, list[str], list[dict]], None] | None = None
        self.holds: Callable[[dict], str] | None = None
        # Whether a worker (one that reads Slack, or not) runs sandboxed, the one place its
        # folder's agent settings are checked (Runtime.settings_guarded); None: as if it does.
        self.guarding: Callable[[bool], bool] | None = None
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
        for checkout in nested_first(self.checkouts(row)):
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
                    await asyncio.to_thread(tidy_claude_folder, tree)
                    continue
                changes = await uncommitted(checkout, tree)
            elif found is not None and not unknown and changes == []:
                # Not in Chief of Staff's folders: git removes it, or it stays.
                self.store.set(REMOVING + tree, "1")
                with suppress(ValueError):
                    await checkout_git(
                        checkout, checkout["repo"], "worktree", "remove", tree,
                        limit=REMOVE_SECONDS,
                    )  # fmt: skip
                    removed.append(tree)
                self.store.delete(REMOVING + tree)
                if tree in removed:
                    await asyncio.to_thread(tidy_claude_folder, tree)
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
        if not self.store.get(f"continued:{row['id']}"):  # Not one handed to its successor.
            with suppress(OSError):
                # Only when nothing else is in it, like a worker's notes.
                await asyncio.to_thread(self.workspace(row).rmdir)
            # Chief of Staff's own git folders for its clones and submodules: made again if read
            # (a folder handed on keeps them for its successor).
            await privategit.prune(self.git_places(row, folder))
        if self.on_release:
            self.on_release(row, removed, kept)
        return removed

    def git_places(self, row: dict, folder: Path | None) -> list[str]:
        """Where the repositories of a worker's own git folders are (privategit): its folder
        (clones), and its worktrees' private folders in their repositories (submodules)."""
        admins = [checkout["admin"] for checkout in self.checkouts(row) if checkout.get("admin")]
        return [*([str(folder)] if folder else []), *admins]

    async def remove_tree(self, checkout: dict, tree: str, trash: Path) -> bool:
        """Remove a worktree git status found clean (nothing uncommitted, no ignored file that
        may matter). It goes out of its place into trash in one rename first (no worker can
        write there: what it holds stays as it is), is checked again there, and is deleted
        from there, never following a link; git then forgets it, so a reopened worker gets it
        back whole from its branch. Not git's own removal, which deletes in place by paths a
        worker's leftover program could redirect. False, and put back, when a file changed or
        appeared meanwhile. Noted as under way (REMOVING) until done: restore() finishes one
        stopped halfway."""
        self.store.set(REMOVING + tree, "1")
        removed = await self.removed_tree(checkout, tree, trash)
        self.store.delete(REMOVING + tree)
        return removed

    async def removed_tree(self, checkout: dict, tree: str, trash: Path) -> bool:
        moved = await asyncio.to_thread(to_trash, Path(tree), trash)
        if not moved:
            return False
        lines = await status_lines({**checkout, "worktree": str(moved)}, str(moved))
        if lines is None or await its_work(checkout, str(moved), lines):
            logger.warning("Not removing %s: files changed meanwhile.", tree)
            await asyncio.to_thread(untrash, moved, Path(tree))
            return False
        await asyncio.to_thread(delete_moved, moved)
        with suppress(ValueError):
            await checkout_git(checkout, checkout["repo"], "worktree", "prune")
        return not await asyncio.to_thread(os.path.lexists, tree)

    async def force_remove(self, checkout: dict, tree: str, trash: Path) -> bool:
        """Remove a worktree with whatever is in it: out of its place into trash in one rename,
        deleted from there (never following a link), and forgotten by git: never a half tree
        for a reopened worker to resume in (restore() makes it again from its branch, as it
        does when this itself was stopped halfway: noted as under way, REMOVING)."""
        self.store.set(REMOVING + tree, "1")
        if moved := await asyncio.to_thread(to_trash, Path(tree), trash):
            await asyncio.to_thread(delete_moved, moved)
        with suppress(ValueError):
            await checkout_git(checkout, checkout["repo"], "worktree", "prune")
        gone = not await asyncio.to_thread(os.path.lexists, tree)
        self.store.delete(REMOVING + tree)
        return gone

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
        """The task folder and worktrees release() removed, back on their branches; one whose
        removal stopped halfway (Chief of Staff stopped past its time, or ended at once) goes
        first, then comes back whole."""
        workspace = self.workspace(row)
        if row["cwd"] == str(workspace):  # A worker without a repository works in it directly.
            await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        for checkout in self.checkouts(row):
            tree = checkout.get("worktree")
            half = bool(tree and self.store.get(REMOVING + tree))
            if not tree or (await asyncio.to_thread(os.path.exists, tree) and not half):
                continue
            repo = checkout["repo"]
            if half and not await self.clear_half(row, tree):
                continue
            await git("-C", repo, "worktree", "prune")
            async with self.staging() as staging:
                staged = staging / Path(tree).name
                try:
                    await git("-C", repo, "worktree", "add", str(staged), checkout["branch"])
                except ValueError:
                    # The branch is gone (merged and deleted): start it again where it started.
                    await git(
                        "-C", repo, "worktree", "add", "-b", checkout["branch"], str(staged),
                        checkout.get("base_ref") or "HEAD",
                    )  # fmt: skip
                admin = (await git("-C", str(staged), "rev-parse", "--absolute-git-dir")).strip()
                # (Made now, by Chief of Staff.)
                self.store.delete(f"gitid:{await asyncio.to_thread(os.path.realpath, admin)}")
                carried = (
                    [] if row["slack"] else await carry_ignored_files(Path(repo), staged, staging)
                )
                try:
                    placed = await asyncio.to_thread(
                        place_tree, staged, Path(tree).parent, workspace.parent, Path(tree).name,
                        True,
                    )  # fmt: skip
                    common = checkout.get("git_dir") or str(Path(repo) / ".git")
                    await asyncio.to_thread(
                        repoint, placed, workspace.parent, admin, Path(common).parent
                    )
                except BaseException:
                    with suppress(ValueError):
                        await git("-C", repo, "worktree", "remove", "--force", str(staged))
                    raise
                checkout["admin"] = admin
            self.store.delete(REMOVING + tree)
            if not row["slack"] and self.keeps(slack=False):
                # What it carries is the owner's, as at its first making: kept as now, as far
                # as the owner's checkout holds it so (vet_carried).
                await asyncio.to_thread(settings_guard.refresh, self.store, Path(tree), carried)
                await asyncio.to_thread(self.vet_carried, Path(tree), Path(repo), carried)

    async def clear_half(self, row: dict, tree: str) -> bool:
        """What is left of a worktree whose removal stopped halfway (one found with nothing
        uncommitted, or one the owner said to delete): out of its place in one step and deleted
        from trash in Chief of Staff's own folders, deleted where it is elsewhere (git was
        deleting it there). Whether it is gone."""
        roots = self.roots()
        folder = await asyncio.to_thread(folder_of, self.workspace(row), roots)
        real = Path(await asyncio.to_thread(os.path.realpath, tree))
        trash = trash_of(folder, roots) if folder and real.is_relative_to(folder) else None
        logger.warning("Removing what is left of %s: its removal stopped halfway.", tree)
        if trash and (moved := await asyncio.to_thread(to_trash, Path(tree), trash)):
            await asyncio.to_thread(delete_moved, moved)
        elif await asyncio.to_thread(os.path.isdir, tree):
            await asyncio.to_thread(rmtree_writable, Path(tree))
        if gone := not await asyncio.to_thread(os.path.lexists, tree):
            self.store.delete(REMOVING + tree)
        return gone

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
        report's. Read through Chief of Staff's own git folder for each (private_git): its
        config, as it says where it was cloned from, is read once, here, as data."""
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
            ]

        made = []
        found = await asyncio.to_thread(unknown)
        for repo in found:
            if checkout := await self.as_checkout(row, repo, found):
                made.append(checkout)
        return made

    async def claimed_worktree(self, row: dict, tree: str) -> dict | None:
        """A worktree another agent made for a worker (Claude's EnterWorktree, a subagent's
        isolation: <folder>/.claude/worktrees/<name>, `folder` one it may write), as one of
        its checkouts: as own_clones takes one it made with `git worktree add`."""
        parts = Path(tree).parts
        folder = (
            str(Path(*parts[:-3]))
            if len(parts) > 3 and parts[-3:-1] == (".claude", "worktrees")
            else ""
        )
        writes = self.writes(row)

        def its_own() -> tuple[str, str] | None:
            # As each resolves: a link on its way (.claude, worktrees) must not lead elsewhere.
            real, real_tree = os.path.realpath(folder), os.path.realpath(tree)
            mine = any(within(real, os.path.realpath(path)) for path in writes)
            inside = real_tree != real and within(real_tree, real)
            if mine and inside and linked_worktree(Path(real_tree)):
                return real, real_tree
            return None

        found = await asyncio.to_thread(its_own) if folder else None
        if found is None:
            return None
        folder, tree = found
        checkout = await self.as_checkout(row, tree, [folder])
        if checkout is not None:
            # Released (and restored) with the worktrees Chief of Staff made for it: removed as
            # it closes when nothing in it is uncommitted, else kept, as theirs are. Its folder
            # in the repository, as found now (git_pins: read there, even once moved to trash).
            checkout["worktree"] = tree
            common = checkout.get("git_dir") or str(Path(checkout["repo"]) / ".git")
            if admin := await asyncio.to_thread(admin_dir, common, tree):
                checkout["admin"] = admin
        return checkout

    async def as_checkout(self, row: dict, repo: str, found: list[str]) -> dict | None:
        """A repository a worker made in its folder (a clone, or a linked worktree of one it
        works in: own_worktree), as a checkout: its remote, branch and base."""
        if await asyncio.to_thread(linked_worktree, Path(repo)):
            if not (checkout := await self.own_worktree(row, repo, found)):
                return None
        else:
            checkout = {"project": "", "repo": repo, "path": repo, "branch": "",
                        "base_ref": "", "base": "a clone it made itself",
                        "remote": repo}  # fmt: skip
            try:  # Its .git as taken now: the one Chief of Staff's git reads (clone_place).
                checkout |= await asyncio.to_thread(clone_identity, Path(repo) / ".git")
            except OSError:
                return None
        if checkout.get("remote"):
            # Where it is pushed to from now on: as its config says now, recorded.
            origin = await asyncio.to_thread(recorded_origin, Path(checkout["clone_git"]))
            checkout |= {"origin": origin, "remote": origin or checkout["remote"]}
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
        return checkout

    async def own_worktree(self, row: dict, tree: str, found: list[str]) -> dict | None:
        """A linked worktree a worker made itself in its folder, as a checkout: only of a
        repository it works in or a clone it made in its folder (read like a clone: through
        Chief of Staff's own git folder). Which one is read from the repository's side
        (admin_dir), not from the `.git` file the worker wrote."""
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
        checkout = {"project": "", "path": tree, "branch": "", "base_ref": "",
                    "base": "a worktree it made itself"}  # fmt: skip
        if Path(common).name == ".git":
            checkout["repo"] = str(Path(common).parent)
        else:
            checkout |= {"repo": common, "git_dir": common}
        if own:
            checkout["remote"] = checkout["repo"]
            try:  # The clone's .git (or the repository) as taken now (clone_place).
                checkout |= await asyncio.to_thread(clone_identity, Path(common))
            except OSError:
                return None
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
        copy: bool = False,
    ) -> dict | None:
        """A checkout of a configured project for a worker; None if it has that repo already.
        `carry`: copy the gitignored files the worktree needs (not for workers that read Slack:
        those files are often secrets). `copy`: a copy of the owner's checkout as it is now
        (Runtime.own_copy): from its commit, with its uncommitted work copied in
        (carry_changes); what could not be is in "left_out"."""
        path = resolve_project(project, self.projects())
        if not isolate:
            checkout = {"project": str(path), "path": str(path), "branch": "", "shared": True}
            if git_dir := await common_git_dir(path):
                checkout["git_dir"] = git_dir
                # Where its agent settings are read up to (settings_guard.checkout_top), as
                # git says now, before any worker had it.
                with suppress(ValueError):
                    checkout["top"] = top = (
                        await git("-C", str(path), "rev-parse", "--show-toplevel")
                    ).strip()
                    # The commit there before any worker had it: agent settings there as
                    # it has them are the owner's (Runtime.settings_proven).
                    checkout["settings_base"] = (await git("-C", top, "rev-parse", "HEAD")).strip()
            return checkout
        toplevel = await git("-C", str(path), "rev-parse", "--show-toplevel")
        top = await asyncio.to_thread(Path(toplevel.strip()).resolve)
        if str(top) in repos:
            return None
        ref, base = ("HEAD", "") if copy else await base_ref(top)
        branch = f"{BRANCH_PREFIX}{slug}"
        # --no-track: the branch must never push to the default branch it started from.
        start = (await git("-C", str(top), "rev-parse", ref)).strip()  # Fixed, unlike ref.
        if copy:
            base = (
                f"the owner's checkout as it was (commit {start[:12]}, with its uncommitted "
                "changes)"
            )
        common = await common_git_dir(top) or str(top / ".git")
        copied: list[str] = []
        left: list[str] = []
        async with self.staging() as staging:
            staged = staging / top.name
            await git(
                "-C", str(top), "worktree", "add", "--no-track", "-b", branch, str(staged), start
            )  # fmt: skip
            try:
                # Its own folder in the repository, read now, before a worker could change
                # where its .git points.
                admin = (await git("-C", str(staged), "rev-parse", "--absolute-git-dir")).strip()
                self.store.delete(f"gitid:{await asyncio.to_thread(os.path.realpath, admin)}")
                if copy:
                    copied, left = await carry_changes(top, staged, staging)
                carried = await carry_ignored_files(top, staged, staging) if carry else []
                tree = await asyncio.to_thread(
                    place_tree, staged, workspace, workspace.parent, top.name
                )
                await asyncio.to_thread(repoint, tree, workspace.parent, admin, Path(common).parent)
            except BaseException:
                with suppress(ValueError):
                    await git("-C", str(top), "worktree", "remove", "--force", str(staged))
                with suppress(ValueError):
                    await git("-C", str(top), "branch", "-D", branch)
                raise
        repos.add(str(top))
        checkout = {
            "project": str(path),
            "repo": str(top),
            "worktree": str(tree),
            "path": str(tree / path.relative_to(top)),
            "branch": branch,
            "base": base,
            "base_ref": start,
            "git_dir": common,
            "admin": admin,
            "carried": carried,
        }
        if self.keeps(slack=not carry):
            # The agent settings it is given, as Chief of Staff made it (settings_guard), what
            # it carried from the owner's checkout only as the owner's checkout holds it.
            await asyncio.to_thread(self.keep_settings, tree, Path(checkout["path"]))
            await asyncio.to_thread(self.vet_carried, tree, top, [*carried, *copied])
        if copy:
            checkout |= {"copy": start, "left_out": left}
        return checkout

    def keeps(self, slack: bool) -> bool:
        """Whether a folder made for a worker has its agent settings kept as it was made: only
        for one that runs sandboxed (guarding), where they are checked."""
        return self.guarding is None or self.guarding(slack)

    def staging(self) -> contextlib.AbstractAsyncContextManager[Path]:
        """A folder of Chief of Staff's own, beside workers' folders (the same disk, so one
        rename moves what it makes there into a workspace) where none of them can write: git
        checks out, clones and carried files are made there, out of every worker's reach, then
        moved into place (place_tree). Removed after (empty by then)."""
        root = self.scratch_root() / STAGING

        @contextlib.asynccontextmanager
        async def made():
            folder = root / uuid4().hex[:12]
            await asyncio.to_thread(folder.mkdir, parents=True, mode=0o700)
            try:
                yield folder
            finally:
                await asyncio.to_thread(rmtree_writable, folder, root)

        return made()

    @staticmethod
    async def remove_checkout(checkout: dict):
        """Undo a worktree made for a worker that could not be set up."""
        if not checkout.get("worktree"):
            return  # A shared project folder: nothing was created.
        with suppress(ValueError):
            await git("-C", checkout["repo"], "worktree", "remove", "--force", checkout["worktree"])
        with suppress(ValueError):
            await git("-C", checkout["repo"], "branch", "-D", checkout["branch"])

    def vet_carried(self, tree: Path, owner: Path, carried: list[str]):
        settings_guard.vet(self.store, tree, owner, carried, not self.touched(owner))

    def touched(self, top: Path, besides: str = "") -> bool:
        """Whether a worker other than `besides` could have written in this folder, or above
        it: one whose folders to write are there, above or below (writes: not a checkout it was
        given only to read), or the folders Settings lets sandboxed commands write; run or not
        (one handed out before Chief of Staff kept track of turns may have)."""
        extra = [os.path.realpath(path) for path in self.store.settings().sandbox_writable]
        if any(within(place, str(top)) or within(str(top), place) for place in extra):
            return True
        for other in self.store.all(
            "SELECT * FROM sessions WHERE role=? AND id!=?", (WORKER, besides)
        ):
            for place in map(os.path.realpath, self.writes(other)):
                if within(place, str(top)) or within(str(top), place):
                    return True
        return False

    def keep_settings(self, top: Path, cwd: Path):
        """A folder Chief of Staff just made for a worker: the agent settings in it are its own
        (settings_guard.keep), as they are now: each agent's."""
        settings_guard.keep(self.store, top, cwd, (REPOSITORY_SETTINGS, CODEX_SETTINGS))

    async def clone_checkout(
        self, workspace: Path, slug: str, repo: str, slack: bool = False
    ) -> dict:
        """Clone a repository that is not on this machine into a worker's workspace."""
        url, name = clone_source(repo)
        branch = f"{BRANCH_PREFIX}{slug}"
        async with self.staging() as staging:
            staged = staging / "clone"
            if GITHUB_NAME.fullmatch(repo) and shutil.which("gh"):
                # gh uses the owner's GitHub sign-in, so private repositories work too.
                await self.gh_clone(repo, staged)
            else:
                await git("clone", "--quiet", url, str(staged), limit=900)
            default = (await git("-C", str(staged), "rev-parse", "--abbrev-ref", "HEAD")).strip()
            start = (await git("-C", str(staged), "rev-parse", "HEAD")).strip()
            await git("-C", str(staged), "checkout", "--quiet", "-b", branch)
            # Where it came from, as Chief of Staff's own clone says before any worker has it:
            # where it is pushed to (privategit; the clone's config is never read again).
            url = ""
            with suppress(ValueError):
                url = await git("-C", str(staged), "config", "--get", "remote.origin.url")
            destination = await asyncio.to_thread(
                place_tree, staged, workspace, workspace.parent, name
            )
        if self.keeps(slack):
            await asyncio.to_thread(self.keep_settings, destination, destination)
        made = await asyncio.to_thread(clone_identity, destination / ".git")
        return {
            "project": "",
            "repo": str(destination),
            "path": str(destination),
            "branch": branch,
            "base": f"origin/{default} of a fresh clone of {repo}",
            "base_ref": start,
            "remote": repo,
            "origin": url.strip() if plain_address(url) else "",
            **made,
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
