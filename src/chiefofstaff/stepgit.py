"""A shepherd step's own git folder (shepherd.py): its HEAD, index and refs, its objects its own
with the repository's borrowed read-only (alternates), so that nothing a step runs can change
the repository its worktree shares with the owner and other workers (their branches, tags,
objects). While the step runs, its worktree's `.git` file points here; the repository's folder
is read-only to it. Once it ends, Chief of Staff, outside any sandbox, takes the commit it made
into the repository (its objects checked as they come in) and, where the step worked in its
worker's own worktree, moves the worker's branch to it, as the step's commit would have.

Blocking functions: call them in a thread. Chief of Staff never runs git with this folder as its
repository once the step ran: it reads its HEAD as a file and its objects through the
repository's own git (borrowed())."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from contextlib import suppress
from pathlib import Path

from .settings_guard import read_regular
from .workspaces import SAFE_GIT

# What a step's git folder takes of its repository, as links (held read-only in its sandbox):
# the hooks and info/ git runs and reads there, which commits are whole (shallow), submodules'
# folders and git-lfs's objects, as a worktree of the repository has them.
SHARED = ("hooks", "info", "shallow", "modules", "lfs")
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
REF = re.compile(r"refs/[A-Za-z0-9._/-]{1,250}")
# Most of a step's commit taken into the repository (its new objects, as a pack).
MOST_BYTES = 512 * 1024 * 1024
# git with what a step wrote borrowed: nothing it planted that git would trust over the objects
# themselves (a commit graph, a multi-pack index, bitmaps), nothing fetched.
BORROWING = (*SAFE_GIT, "-c", "core.commitGraph=false", "-c", "core.multiPackIndex=false",
             "-c", "pack.useBitmaps=false", "-c", "fetch.writeCommitGraph=false")  # fmt: skip


def environment(**more: str) -> dict[str, str]:
    """This process's environment without Chief of Staff's own variables (a token may be one),
    git not prompting, not fetching objects it lacks, and not replacing any."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("CHIEFOFSTAFF_")}
    for name in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):  # fmt: skip
        env.pop(name, None)
    return env | {"GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1",
                  "GIT_NO_REPLACE_OBJECTS": "1", **more}  # fmt: skip


def run(*arguments: str, cwd: str, env: dict[str, str], data: bytes | None = None) -> bytes:
    found = subprocess.run(  # noqa: S603
        ["git", *arguments],  # noqa: S607  (the owner's git, as Chief of Staff runs it)
        cwd=cwd, env=env, input=data, capture_output=True, timeout=300, check=False,
    )  # fmt: skip
    if found.returncode:
        said = found.stderr.decode(errors="replace").strip().splitlines()
        raise ValueError(said[-1][:300] if said else f"git {arguments[0]} failed")
    return found.stdout


def folder_of(step_folder: str) -> Path:
    """A step's git folder: beside its folder, in a folder (.shepherd) the step can't write, so
    neither the folder nor what it holds read-only can be moved aside and made again."""
    return Path(step_folder).with_name(Path(step_folder).name + ".git")


def make(
    common: str, admin: str, tree: str, step_folder: str, head: str, branch: str, own: bool
) -> dict:
    """The step's git folder for its worktree `tree` (a linked worktree of `common`, `admin`
    its own folder there), at `head` (on `branch`, a ref name, when the worktree is on one).
    `own`: the worker's own worktree, whose HEAD Chief of Staff moves to the step's commit once
    it ends (release_git). Returns its record, the worktree's `.git` not yet pointed at it
    (point())."""
    common = os.path.realpath(common)
    place = folder_of(step_folder)
    if place.parent.is_symlink() or place.is_symlink():
        raise ValueError(f"{place.parent} is a link, not its own folder")
    if place.exists():
        shutil.rmtree(place)
    for part in ("objects/info", "objects/pack", "refs/heads", "refs/tags"):
        (place / part).mkdir(parents=True, mode=0o700)
    (place / "objects" / "info" / "alternates").write_text(f"{common}/objects\n")
    env = environment()
    listed = run("--git-dir", common, "for-each-ref", "--format=%(objectname) %(refname)",
                 cwd=common, env=env).decode(errors="replace")  # fmt: skip
    refs = sorted((line for line in listed.splitlines() if SHA.match(line)),
                  key=lambda line: line.partition(" ")[2].encode())  # fmt: skip
    (place / "packed-refs").write_text("".join(f"{line}\n" for line in ["# pack-refs with: sorted",
                                                                         *refs]))  # fmt: skip
    if branch:
        (place / branch).parent.mkdir(parents=True, exist_ok=True)
        (place / branch).write_text(f"{head}\n")
    (place / "HEAD").write_text(f"ref: {branch}\n" if branch else f"{head}\n")
    (place / "config").write_text(config_text(common))
    for name in SHARED:
        if os.path.lexists(Path(common) / name):
            (place / name).symlink_to(Path(common) / name)
    copied = False
    with suppress(OSError):
        shutil.copyfile(Path(admin) / "index", place / "index")
        for shared in Path(admin).glob("sharedindex.*"):
            shutil.copyfile(shared, place / shared.name)
        copied = True
    # Its index: the worktree's, when that says what is at `head` (it does: clean, or just made).
    pins = env | {"GIT_DIR": str(place), "GIT_WORK_TREE": tree}
    try:
        if not copied:
            raise ValueError("no index")
        run(*SAFE_GIT, "diff-index", "--cached", "--quiet", head, "--", cwd=tree, env=pins)
    except ValueError:
        run(*SAFE_GIT, "read-tree", head, cwd=tree, env=pins)
    if newest(tree, pins) >= int(time.time()):
        # Files written this second (a worktree just made): an index written in it too
        # leaves each "racily clean", hashed again by every git command of the step, and
        # `git commit -a` then writes each again into the step's folder (it can't freshen the
        # repository's copy, read-only to it). A second's tick first, at most.
        time.sleep(1.01 - time.time() % 1)
    with suppress(ValueError):
        run(*SAFE_GIT, "update-index", "-q", "--refresh", "--force-write-index", cwd=tree,
            env=pins)  # fmt: skip
    pointer = Path(tree) / ".git"
    return {"git": str(place), "pointer": str(pointer), "was": pointer.read_text(),
            "admin": admin, "common": common, "branch": branch, "h0": head,
            "own": own}  # fmt: skip


def newest(tree: str, pins: dict[str, str]) -> int:
    """The second the newest of a worktree's tracked files was written in."""
    found = 0
    with suppress(ValueError):
        listed = run(*SAFE_GIT, "ls-files", "-z", cwd=tree, env=pins)
        for name in filter(None, listed.split(b"\0")):
            with suppress(OSError):
                found = max(found, int((Path(tree) / os.fsdecode(name)).lstat().st_mtime))
    return found


def config_text(common: str) -> str:
    """The step's git folder's configuration: the repository's own, included (its filters,
    remotes, settings), with its format and extensions (but how it stores refs: here, as
    files)."""
    lines = ["[core]", "\trepositoryformatversion = 0"]
    extensions = []
    with suppress(ValueError):
        found = run("config", "--file", f"{common}/config", "--get-regexp",
                    r"^(core\.repositoryformatversion|extensions\.)", cwd=common,
                    env=environment()).decode(errors="replace")  # fmt: skip
        for line in found.splitlines():
            key, _, value = line.partition(" ")
            if key == "core.repositoryformatversion" and value.strip().isdigit():
                lines[1] = f"\trepositoryformatversion = {value.strip()}"
            elif key.startswith("extensions.") and key != "extensions.refstorage":
                extensions.append(f"\t{key.removeprefix('extensions.')} = {value.strip()}")
    if extensions:
        lines[1] = "\trepositoryformatversion = 1"
        lines += ["[extensions]", *extensions]
    quoted = common.replace("\\", "\\\\").replace('"', '\\"')
    lines += ["[include]", f'\tpath = "{quoted}/config"']
    return "\n".join(lines) + "\n"


def write_pointer(path: Path, text: str):
    """A worktree's `.git` file written, never through a link or into a folder put there."""
    if os.path.lexists(path) and (path.is_symlink() or not path.is_file()):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o644), "w") as file:
        file.write(text)


def point(record: dict):
    """The worktree's `.git` at the step's git folder."""
    write_pointer(Path(record["pointer"]), f"gitdir: {record['git']}\n")


def restore(record: dict):
    """The worktree's `.git` back at its own folder in the repository, as it was."""
    pointer = Path(record["pointer"])
    with suppress(OSError):
        if read_regular(pointer, 65536).decode(errors="replace") == record["was"]:
            return
    write_pointer(pointer, record["was"])


def remove(record: dict):
    shutil.rmtree(record["git"], ignore_errors=True)


def head_of(record: dict) -> str:
    """The commit the step's HEAD is at, read as files (never through its git): "" when it
    can't be told."""
    place = Path(record["git"])
    try:
        text = read_regular(place / "HEAD", 4096).decode(errors="replace").strip()
    except OSError:
        return ""
    if SHA.fullmatch(text):
        return text
    name = text.removeprefix("ref:").strip()
    if not text.startswith("ref:") or not REF.fullmatch(name) or ".." in name:
        return ""
    with suppress(OSError):
        found = read_regular(place / name, 4096).decode(errors="replace").strip()
        return found if SHA.fullmatch(found) else ""
    with suppress(OSError):
        packed = read_regular(place / "packed-refs").decode(errors="replace")
        for line in packed.splitlines():
            sha, _, ref = line.partition(" ")
            if ref == name and SHA.fullmatch(sha):
                return sha
    return ""


def resolve(record: dict, commit: str) -> str:
    """A commit the step names (in full or in part), as the repository's git finds it with the
    step's objects borrowed; ValueError if it is none."""
    env = environment(GIT_ALTERNATE_OBJECT_DIRECTORIES=f"{record['git']}/objects")
    found = run(*BORROWING, "--git-dir", record["common"], "rev-parse", "--verify", "--quiet",
                f"{commit}^{{commit}}", cwd=record["common"], env=env)  # fmt: skip
    return found.decode().strip()


def take(record: dict, tip: str, base: str) -> None:
    """The commits from `base` to `tip` (a descendant of it) into the repository: their objects
    as a pack its own git checks as it stores them. ValueError if `tip` is not on top of
    `base`, or its objects can't be taken."""
    common = record["common"]
    borrowed = environment(GIT_ALTERNATE_OBJECT_DIRECTORIES=f"{record['git']}/objects")
    run(*BORROWING, "--git-dir", common, "merge-base", "--is-ancestor", base, tip, cwd=common,
        env=borrowed)  # fmt: skip
    plain = environment()
    with suppress(ValueError):
        run(*SAFE_GIT, "--git-dir", common, "cat-file", "-e", f"{tip}^{{commit}}", cwd=common,
            env=plain)  # fmt: skip
        return  # Already in it (taken before).
    pack = run(*BORROWING, "--git-dir", common, "pack-objects", "--revs", "--stdout", "-q",
               cwd=common, env=borrowed, data=f"{tip}\n^{base}\n".encode())  # fmt: skip
    if len(pack) > MOST_BYTES:
        raise ValueError("its commit is too big to take")
    run(*SAFE_GIT, "--git-dir", common, "index-pack", "--stdin", "--strict", cwd=common,
        env=plain, data=pack)  # fmt: skip


def move_branch(record: dict, tip: str) -> None:
    """The worker's branch from where the step began to `tip` (taken already), only if it is
    still there."""
    common = record["common"]
    run(*SAFE_GIT, "--git-dir", common, "update-ref", "-m", "Prepared for you: the step's commit",
        record["branch"], tip, record["h0"], cwd=common, env=environment())  # fmt: skip
