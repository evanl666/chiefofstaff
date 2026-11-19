"""Chief of Staff's git in a repository a worker can write: a clone in its folder (one Chief of
Staff made there, or the worker's own), a worktree it made of one, a submodule checked out in
its worktree. git never reads that repository's own git folder, whose config, hooks and
attributes the worker could write (and change between a look at them and git's use of them):
it runs with a git folder of Chief of Staff's own (ROOT, in its data folder, out of every
worker's reach) made from what the repository holds as data. Its objects are taken through
`objects/info/alternates` (git reads an object by its hash, as from any remote); its HEAD, refs,
index, shallow commits and excludes are read here as regular files, checked and copied; its
config is written here: how the files are kept on this disk (as the repository says, checked),
and the remote Chief of Staff recorded itself. No hooks, no `info/attributes`, no filter, no
replace ref (`refs/replace/`: what an object reads as). A reftable's tables are copied as data
too. An index that names a path out of its tree (`..`, an absolute one, one in a `.git`) is not
used: one is made from HEAD's tree instead."""

import asyncio
import errno
import hashlib
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path

from .settings_guard import SpecialFileError, read_regular

logger = logging.getLogger(__name__)

# Where the git folders are (Workspaces sets it: in Chief of Staff's data folder).
ROOT: Path | None = None
# One git at a time in each (refs and index are copied in before each command).
LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
# Each git folder as last made: what its repository's files were then (their identity, size and
# time). Made again only when they changed.
MADE: dict[str, tuple] = {}

HASH = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
# A reftable's table, as git names them (tables.list lists them, oldest first).
TABLE = re.compile(r"0x[0-9a-f]{12}-0x[0-9a-f]{12}-[0-9a-f]{8}\.ref")
MOST_TABLES = 4096
# Refs that change what an object reads as: never copied (and never used: GIT_NO_REPLACE_OBJECTS).
REPLACE = "refs/replace/"
# A path in an index that leaves its tree, or is in a .git (as git's own check refuses one):
# ``..``, ``.``, an empty part (an absolute path), a `.git` part (any case, trailing dots or
# spaces: as Windows and macOS read one).
UNSAFE_PATH = re.compile(
    rb"(?:^|[\x00\t/])(?:\.\.?|(?i:\.git)[. ]*)(?:/|\x00|$)|(?:^|[\x00\t])/|//|/(?:\x00|$)"
)
# Each index's submodules as last listed (by its file's identity, size and time): {path from the
# top: the commit it records}. Listed as its copy is checked (prepare), or by workspaces'
# index_gitlinks (the same dict).
GITLINKS: dict[str, tuple[tuple[int, int, int], dict[str, str]]] = {}
INVALID_HEAD = b"ref: refs/heads/.invalid\n"
SHARED_INDEX = re.compile(r"sharedindex\.[0-9a-f]{40}(?:[0-9a-f]{24})?")
MOST_REFS = 200_000
MOST_PACKED_REFS = 256 * 1024 * 1024
MOST_INDEX = 1024 * 1024 * 1024
MOST_SMALL = 1024 * 1024
BOOLEAN = {"true", "false", "yes", "no", "on", "off", "1", "0"}
# What a repository's own config says of how its files are kept on this disk, kept when it is
# one of git's own words for it: the rest of that config is never used.
KEPT = {
    "core.filemode": BOOLEAN,
    "core.ignorecase": BOOLEAN,
    "core.precomposeunicode": BOOLEAN,
    "core.symlinks": BOOLEAN,
    "core.trustctime": BOOLEAN,
    "core.checkstat": {"default", "minimal"},
    "core.autocrlf": {"true", "false", "input"},
    "core.eol": {"lf", "crlf", "native"},
}


def quiet(**more: str) -> dict[str, str]:
    """The environment of git Chief of Staff runs here on what it copied: no config but what it
    is given (not even the owner's), no transport, nothing fetched, no replace ref."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return environment | {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                          "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1",
                          "GIT_NO_REPLACE_OBJECTS": "1"} | more  # fmt: skip


def config_entries(file: Path) -> list[tuple[str, str]] | None:
    """(key, value) of each setting in a git config file, as git itself reads it (read here as a
    regular file, then given to `git config` to parse: nothing it names is followed or run);
    [] for none, None when it can't be read as one."""
    try:
        data = read_regular(file, MOST_SMALL, follow=True)
    except (FileNotFoundError, NotADirectoryError):  # (None there: a worktree's .git file.)
        return []
    except OSError:
        return None
    try:
        done = subprocess.run(
            ["git", "config", "--file", "-", "--no-includes", "--list", "-z"],  # noqa: S607
            input=data, capture_output=True, env=quiet(), cwd=os.sep, timeout=20, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode:
        return None
    entries = []
    for item in done.stdout.split(b"\0"):
        if item:
            key, _, value = item.decode(errors="replace").partition("\n")
            entries.append((key, value))
    return entries


def ref_name(name: str) -> bool:
    """Whether a name is one git takes for a ref (check-ref-format's rules, under refs/)."""
    if not name.startswith("refs/") or len(name) > 1024 or name.endswith(("/", ".", ".lock")):
        return False
    if any(ord(c) < 32 or c in " ~^:?*[\\\x7f" for c in name) or ".." in name or "@{" in name:
        return False
    return all(part and not part.startswith(".") and not part.endswith(".lock")
               for part in name.split("/"))  # fmt: skip


def root() -> Path:
    """Where the git folders are."""
    if ROOT is not None:
        return ROOT
    # (Only outside Chief of Staff: a script, a test of this alone.)
    return Path(tempfile.gettempdir()) / f"chiefofstaff-git-{getattr(os, 'getuid', int)()}"


def place(common: str, admin: str = "") -> Path:
    """The git folder Chief of Staff's git uses for a repository (and one of its worktrees)."""
    named = f"{os.path.realpath(common)}\0{admin and os.path.realpath(admin)}"
    return root() / hashlib.sha256(named.encode()).hexdigest()[:24]


def folders() -> list[tuple[Path, str]]:
    """Chief of Staff's git folders, each with the objects folder it reads (its alternates'; ""
    for none)."""
    found = []
    with suppress(OSError), os.scandir(root()) as listed:
        for entry in listed:
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                lines = (Path(entry.path) / "objects" / "info" / "alternates").read_text()
                found.append((Path(entry.path), lines.splitlines()[0].strip()))
            except (OSError, IndexError):
                found.append((Path(entry.path), ""))
    return found


def within(path: str, places: list[str]) -> bool:
    real = os.path.realpath(path)
    return any(Path(real).is_relative_to(os.path.realpath(place)) for place in places if place)


async def prune(places: list[str] | None = None) -> list[Path]:
    """Remove the git folders whose repository is gone (its objects folder), and (`places`)
    those of a repository in one of these folders (a released worker's): made again if it is
    read again. The ones removed."""
    removed = []
    for folder, objects in await asyncio.to_thread(folders):
        gone = not objects or not await asyncio.to_thread(os.path.isdir, objects)
        if gone or (places and within(objects, places)):
            async with lock(folder):
                await asyncio.to_thread(shutil.rmtree, folder, True)
                MADE.pop(str(folder), None)
            removed.append(folder)
    return removed


# A git folder this big (what Chief of Staff's own snapshots wrote there) is packed (gc).
GC_SIZE = 256 * 1024 * 1024
# git's gc there: its unreachable objects (snapshots: kept two weeks, as in any repository)
# packed too; never a worktree of its own pruned (a reftable's linked worktree has one).
GC = ("-c", "gc.cruftPacks=true", "-c", "gc.worktreePruneExpire=never", "gc", "--quiet")


def lock(folder: Path) -> asyncio.Lock:
    key = (id(asyncio.get_running_loop()), str(folder))
    if key not in LOCKS:
        LOCKS[key] = asyncio.Lock()
    return LOCKS[key]


def written(path: Path, data: bytes):
    """A file of Chief of Staff's git folder, written whole (a rename) when it changed."""
    there = path.is_file() and not path.is_symlink()
    if there and path.stat().st_size == len(data) and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def copied(source: Path, target: Path, limit: int) -> bool:
    """A regular file copied whole into Chief of Staff's git folder (opened without following a
    link or waiting on a FIFO, as read_regular); False, and none there, when it is not one."""
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0))  # fmt: skip
    try:
        if not getattr(os, "O_NOFOLLOW", 0) and source.is_symlink():
            raise SpecialFileError(errno.ELOOP, "a link, not a file", str(source))  # (Windows.)
        handle = os.open(source, flags)
    except OSError:
        target.unlink(missing_ok=True)
        return False
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            target.unlink(missing_ok=True)
            return False
        with os.fdopen(handle, "rb", closefd=False) as reading, temporary.open("wb") as out:
            shutil.copyfileobj(reading, out, 1024 * 1024)
            if out.tell() > limit:
                raise OSError(errno.EFBIG, "too large", str(source))
        # Its time too: git takes an index entry as racily clean (to read the file again) by
        # the index file's own time.
        os.utime(temporary, ns=(info.st_atime_ns, info.st_mtime_ns))
        temporary.replace(target)
        return True
    except OSError:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        return False
    finally:
        os.close(handle)


def refs_of(common: Path) -> tuple[dict[str, str], dict[str, str]]:
    """A repository's refs, read as files: ({name: hash}, {name: the ref it names}). What is
    not a ref git would take (a bad name, a link, not a hash) is left out."""
    direct: dict[str, str] = {}
    symbolic: dict[str, str] = {}
    try:
        packed = read_regular(common / "packed-refs", MOST_PACKED_REFS).decode(errors="replace")
    except FileNotFoundError:
        packed = ""
    except OSError as exc:
        raise ValueError(f"its packed refs can't be read ({exc.strerror or exc})") from exc
    for line in packed.splitlines():
        if not line or line[0] in "#^":
            continue
        value, _, name = line.partition(" ")
        if HASH.fullmatch(value) and ref_name(name) and not name.startswith(REPLACE):
            direct[name] = value
    count = 0
    for folder, _, files in os.walk(common / "refs"):  # (Not into a linked folder.)
        for file in files:
            count += 1
            if count > MOST_REFS:
                raise ValueError("it has too many refs")
            path = Path(folder) / file
            name = path.relative_to(common).as_posix()
            if not ref_name(name) or name.startswith(REPLACE):
                continue
            try:
                text = read_regular(path, 1024).decode(errors="replace").strip()
            except OSError:
                continue
            if HASH.fullmatch(text):
                direct[name] = text
                symbolic.pop(name, None)
            elif text.startswith("ref:") and ref_name(target := text[4:].strip()):
                symbolic[name] = target
                direct.pop(name, None)
    return direct, symbolic


def looked(path: Path) -> tuple | None:
    """A file's identity, size and time as it is now (not following a link); None for none."""
    try:
        info = path.lstat()
    except OSError:
        return None
    return (info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)


def state(source: Path, own: Path) -> tuple:
    """What git's folder of a repository holds that Chief of Staff copies, as it is now: each
    file's identity, size and time, and each loose ref's (a ref git writes is a new file)."""
    files = [source / name for name in ("config", "packed-refs", "shallow", "info/exclude",
                                        "reftable/tables.list")]  # fmt: skip
    files += [own / "HEAD", own / "index", own / "reftable" / "tables.list"]
    found: list = [looked(path) for path in files]
    count = 0
    for folder, folders, names in os.walk(source / "refs"):
        found.append((folder, looked(Path(folder))))
        for name in sorted(names):
            count += 1
            if count > MOST_REFS:
                raise ValueError("it has too many refs")
            found.append((name, looked(Path(folder) / name)))
        folders.sort()
    return tuple(found)


def copy_reftable(source: Path, target: Path):
    """A reftable (its tables.list and the tables it names) copied as data: each table by the
    name git gives it, each a regular file. ValueError when it can't be read so."""
    try:
        listed = read_regular(source / "tables.list", MOST_SMALL).decode(errors="replace").split()
    except OSError as exc:
        raise ValueError(f"its reftable can't be read ({exc.strerror or exc})") from exc
    if len(listed) > MOST_TABLES or not all(TABLE.fullmatch(name) for name in listed):
        raise ValueError("its reftable lists tables git wouldn't name so")
    target.mkdir(parents=True, exist_ok=True)
    for name in listed:
        if not (target / name).is_file() and not copied(source / name, target / name,
                                                         MOST_PACKED_REFS):  # fmt: skip
            raise ValueError(f"its reftable's {name} can't be read")
    for stale in target.iterdir():
        if stale.name not in listed and stale.name != "tables.list":
            stale.unlink(missing_ok=True)
    written(target / "tables.list", "".join(f"{name}\n" for name in listed).encode())


def suspect(listed: bytes) -> bool:
    """Whether `git ls-files --stage -z` lists a path UNSAFE_PATH may find: quick looks for what
    such a path holds (the regular expression alone takes 60 ms on 40,000 files)."""
    if b"\t/" in listed or b"//" in listed or b"/\x00" in listed:
        return True
    if b"./" in listed or b".\x00" in listed:
        return True
    lowered = listed.lower()
    return any(part in lowered for part in (b".git/", b".git\x00", b".git.", b".git "))


def unsafe_index(git_dir: Path, index: Path, source: Path, key: tuple | None) -> bytes:
    """A path an index names out of its tree or in a .git (UNSAFE_PATH), as git lists them;
    b"" for none. Its submodules are kept as listed (GITLINKS), for `source`, the repository's
    own index, as it was (`key`) when copied. ValueError when git can't read it."""
    try:
        done = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "ls-files", "--stage", "-z"],  # noqa: S607
            capture_output=True, cwd=os.sep, timeout=60, check=False,
            env=quiet(GIT_DIR=str(git_dir), GIT_INDEX_FILE=str(index)),
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"its index can't be read ({exc})") from exc
    if done.returncode:
        raise ValueError("its index can't be read")
    listed = done.stdout
    if suspect(listed) and (found := UNSAFE_PATH.search(listed)):
        start = max(listed.rfind(b"\0", 0, found.start() + 1), listed.rfind(b"\t", 0, found.end()))
        end = listed.find(b"\0", found.start() + 1)
        return listed[start + 1 : end if end >= 0 else None] or b"/"
    links = {}
    for entry in listed.split(b"\0"):
        if entry.startswith(b"160000 "):
            mode, _, name = entry.decode(errors="surrogateescape").partition("\t")
            links[name] = mode.split()[1]
    if key:
        if len(GITLINKS) > 256:
            GITLINKS.clear()
        GITLINKS[str(source)] = (key, links)
    return b""


def index_from_head(git_dir: Path, index: Path) -> bool:
    """An index made from HEAD's tree (git read-tree: no file is read or written); False when
    there is none to make (no commit yet)."""
    index.unlink(missing_ok=True)
    try:
        done = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "read-tree", "HEAD"],  # noqa: S607
            capture_output=True, cwd=os.sep, timeout=120, check=False,
            env=quiet(GIT_DIR=str(git_dir), GIT_INDEX_FILE=str(index)),
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def prepare(common: str, admin: str, tree: str, origin: str = "") -> dict[str, str]:
    """Chief of Staff's git folder for a repository whose own (`common`; `admin`, one of its
    linked worktrees') a worker can write, made from what that one holds now (see above), when
    that changed since: the environment git runs with there. `origin`: the remote Chief of
    Staff recorded (a plain address). ValueError when it can't be read so (a HEAD that is no
    ref, a reftable that is not one)."""
    source, own = Path(common), Path(admin or common)
    folder = place(common, admin)
    now = (state(source, own), origin)
    entries = None
    if (made := MADE.get(str(folder))) and made == now:
        git_dir = (
            folder / "worktrees" / "own" if (folder / "worktrees" / "own").is_dir() else folder
        )
        if (git_dir / "HEAD").is_file():
            return {"GIT_DIR": str(git_dir), "GIT_WORK_TREE": tree}
    MADE.pop(str(folder), None)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    entries = config_entries(source / "config") or []
    said = {key.lower(): value.strip().lower() for key, value in entries}
    kind = said.get("extensions.objectformat", "sha1")
    if kind not in {"sha1", "sha256"}:
        raise ValueError(f"its objects are kept as {kind}, which git here doesn't read")
    refs = said.get("extensions.refstorage", "files")
    if refs not in {"files", "reftable"}:
        raise ValueError(f"its refs are kept as {refs}, which git here doesn't read")
    version = 0 if kind == "sha1" and refs == "files" else 1
    lines = ["[core]", f"\trepositoryformatversion = {version}", "\tbare = false"]
    lines += [f"\t{key.split('.')[1]} = {said[key]}" for key, allowed in KEPT.items()
              if said.get(key) in allowed]  # fmt: skip
    if version:
        lines += ["[extensions]"]
        lines += [f"\tobjectformat = {kind}"] if kind != "sha1" else []
        lines += ["\trefstorage = reftable"] if refs == "reftable" else []
    if origin and not any(c in origin for c in "\n\r\0"):
        # Quoted, as git reads a value: a path on Windows has `\\` in it.
        quoted = origin.replace("\\", "\\\\").replace('"', '\\"')
        lines += ['[remote "origin"]', f'\turl = "{quoted}"']
    written(folder / "config", ("\n".join(lines) + "\n").encode())
    objects = Path(os.path.realpath(source / "objects"))
    written(folder / "objects" / "info" / "alternates", f"{objects.as_posix()}\n".encode())
    shutil.rmtree(folder / "refs", ignore_errors=True)
    shutil.rmtree(folder / "worktrees", ignore_errors=True)
    if refs == "reftable":
        # Its refs, HEAD too, are in its tables (HEAD and refs/ only say so to git); a linked
        # worktree's own (its HEAD) in its own tables, read here as git reads a worktree's.
        (folder / "refs").mkdir()
        written(folder / "HEAD", INVALID_HEAD)
        copy_reftable(source / "reftable", folder / "reftable")
        git_dir = folder
        if admin:
            git_dir = folder / "worktrees" / "own"
            (git_dir / "refs").mkdir(parents=True)
            written(git_dir / "HEAD", INVALID_HEAD)
            written(git_dir / "commondir", b"../..\n")
            copy_reftable(own / "reftable", git_dir / "reftable")
    else:
        git_dir = folder
        shutil.rmtree(folder / "reftable", ignore_errors=True)
        try:
            head = read_regular(own / "HEAD", 4096).decode(errors="replace").strip()
        except OSError as exc:
            raise ValueError(f"its HEAD can't be read ({exc.strerror or exc})") from exc
        if not (HASH.fullmatch(head) or (head.startswith("ref:") and ref_name(head[4:].strip()))):
            raise ValueError("its HEAD names no commit or branch")
        written(folder / "HEAD", f"{head}\n".encode())
        direct, symbolic = refs_of(source)
        for name in ("refs/heads", "refs/tags"):
            (folder / name).mkdir(parents=True, exist_ok=True)
        for name, target in symbolic.items():
            written(folder / name, f"ref: {target}\n".encode())
        names = sorted(direct, key=lambda name: name.encode())
        packed = "# pack-refs with: sorted \n" + "".join(f"{direct[n]} {n}\n" for n in names)
        written(folder / "packed-refs", packed.encode())
    index = git_dir / "index"
    if git_dir != folder:
        (folder / "index").unlink(missing_ok=True)
    try:
        info = (own / "index").stat()
        key: tuple | None = (info.st_ino, info.st_size, info.st_mtime_ns)
    except OSError:
        key = None
    copied(own / "index", index, MOST_INDEX)
    for item in git_dir.glob("sharedindex.*"):
        item.unlink(missing_ok=True)
    if index.exists():
        with os.scandir(own) as listed:  # (A split index's shared part, by its name.)
            for entry in listed:
                if SHARED_INDEX.fullmatch(entry.name):
                    copied(own / entry.name, git_dir / entry.name, MOST_INDEX)
        if bad := unsafe_index(git_dir, index, own / "index", key):
            # git would write there, out of the tree or into a .git, as it checks out (a stash,
            # a restore): what its commit holds instead, as if nothing were staged.
            made = index_from_head(git_dir, index)
            logger.warning(
                "Not using the index of %s: it names %r, out of its folder or in a .git. %s",
                tree, bad.decode(errors="replace")[:200],
                "Read from its last commit instead." if made else "Read without one.",
            )  # fmt: skip
    try:
        shallow = read_regular(source / "shallow", MOST_SMALL).decode(errors="replace").split()
        kept = "".join(f"{commit}\n" for commit in shallow if HASH.fullmatch(commit))
        written(folder / "shallow", kept.encode())
    except OSError:
        (folder / "shallow").unlink(missing_ok=True)
    copied(source / "info" / "exclude", folder / "info" / "exclude", MOST_SMALL)
    MADE[str(folder)] = now
    return {"GIT_DIR": str(git_dir), "GIT_WORK_TREE": tree}
