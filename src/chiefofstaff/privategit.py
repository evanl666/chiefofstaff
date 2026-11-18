"""Chief of Staff's git in a repository a worker can write: a clone in its folder (one Chief of
Staff made there, or the worker's own), a worktree it made of one, a submodule checked out in
its worktree. git never reads that repository's own git folder, whose config, hooks and
attributes the worker could write (and change between a look at them and git's use of them):
it runs with a git folder of Chief of Staff's own (ROOT, in its data folder, out of every
worker's reach) made from what the repository holds as data. Its objects are taken through
`objects/info/alternates` (git reads an object by its hash, as from any remote); its HEAD, refs,
index, shallow commits and excludes are read here as regular files, checked and copied; its
config is written here: how the files are kept on this disk (as the repository says, checked),
and the remote Chief of Staff recorded itself. No hooks, no `info/attributes`, no filter."""

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
from pathlib import Path

from .settings_guard import SpecialFileError, read_regular

logger = logging.getLogger(__name__)

# Where the git folders are (Workspaces sets it: in Chief of Staff's data folder).
ROOT: Path | None = None
# One git at a time in each (refs and index are copied in before each command).
LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
# Each git folder as last made: what its repository's files were then (their identity, size and
# time), and the filters that repository's own config names. Made again only when they changed.
MADE: dict[str, tuple[tuple, list[str]]] = {}

HASH = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
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
    quiet = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    quiet |= {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
              "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1"}  # fmt: skip
    try:
        done = subprocess.run(
            ["git", "config", "--file", "-", "--no-includes", "--list", "-z"],  # noqa: S607
            input=data, capture_output=True, env=quiet, cwd=os.sep, timeout=20, check=False,
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
    """Where Chief of Staff's git folders are (ROOT)."""
    if ROOT is None:  # (Only outside Chief of Staff: a script, a test of this alone.)
        return Path(tempfile.gettempdir()) / f"chiefofstaff-git-{getattr(os, 'getuid', int)()}"
    return ROOT


def place(common: str, admin: str = "") -> Path:
    """The git folder Chief of Staff's git uses for a repository (and one of its worktrees)."""
    named = f"{os.path.realpath(common)}\0{admin and os.path.realpath(admin)}"
    return root() / hashlib.sha256(named.encode()).hexdigest()[:24]


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
        if HASH.fullmatch(value) and ref_name(name):
            direct[name] = value
    count = 0
    for folder, _, files in os.walk(common / "refs"):  # (Not into a linked folder.)
        for file in files:
            count += 1
            if count > MOST_REFS:
                raise ValueError("it has too many refs")
            path = Path(folder) / file
            name = path.relative_to(common).as_posix()
            if not ref_name(name):
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
    files = [source / name for name in ("config", "packed-refs", "shallow", "info/exclude")]
    files += [own / "HEAD", own / "index"]
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


def filters_of(common: str, admin: str = "") -> list[str]:
    """The filters a repository's own config names, as last read (prepare); never used by git."""
    return MADE.get(str(place(common, admin)), ((), []))[1]


def prepare(common: str, admin: str, tree: str, origin: str = "") -> dict[str, str]:
    """Chief of Staff's git folder for a repository whose own (`common`; `admin`, one of its
    linked worktrees') a worker can write, made from what that one holds now (see above), when
    that changed since: the environment git runs with there. `origin`: the remote Chief of
    Staff recorded (a plain address). ValueError when it can't be read so (a HEAD that is no
    ref, a reftable)."""
    source, own = Path(common), Path(admin or common)
    folder = place(common, admin)
    now = (state(source, own), origin)
    environment = {"GIT_DIR": str(folder), "GIT_WORK_TREE": tree}
    if (made := MADE.get(str(folder))) and made[0] == now and (folder / "HEAD").is_file():
        return environment
    MADE.pop(str(folder), None)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    entries = config_entries(source / "config") or []
    said = {key.lower(): value.strip().lower() for key, value in entries}
    kind = said.get("extensions.objectformat", "sha1")
    if kind not in {"sha1", "sha256"}:
        raise ValueError(f"its objects are kept as {kind}, which git here doesn't read")
    if said.get("extensions.refstorage", "files") != "files":
        raise ValueError("its refs are kept in a reftable, which Chief of Staff doesn't read")
    version = 0 if kind == "sha1" else 1
    lines = ["[core]", f"\trepositoryformatversion = {version}", "\tbare = false"]
    lines += [f"\t{key.split('.')[1]} = {said[key]}" for key, allowed in KEPT.items()
              if said.get(key) in allowed]  # fmt: skip
    if kind != "sha1":
        lines += ["[extensions]", f"\tobjectformat = {kind}"]
    if origin and not any(c in origin for c in '\n\r"\\'):
        lines += ['[remote "origin"]', f"\turl = {origin}"]
    written(folder / "config", ("\n".join(lines) + "\n").encode())
    objects = Path(os.path.realpath(source / "objects"))
    written(folder / "objects" / "info" / "alternates", f"{objects}\n".encode())
    try:
        head = read_regular(own / "HEAD", 4096).decode(errors="replace").strip()
    except OSError as exc:
        raise ValueError(f"its HEAD can't be read ({exc.strerror or exc})") from exc
    if not (HASH.fullmatch(head) or (head.startswith("ref:") and ref_name(head[4:].strip()))):
        raise ValueError("its HEAD names no commit or branch")
    written(folder / "HEAD", f"{head}\n".encode())
    direct, symbolic = refs_of(source)
    shutil.rmtree(folder / "refs", ignore_errors=True)
    for name in ("refs/heads", "refs/tags"):
        (folder / name).mkdir(parents=True, exist_ok=True)
    for name, target in symbolic.items():
        written(folder / name, f"ref: {target}\n".encode())
    names = sorted(direct, key=lambda name: name.encode())
    packed = "# pack-refs with: sorted \n" + "".join(f"{direct[n]} {n}\n" for n in names)
    written(folder / "packed-refs", packed.encode())
    copied(own / "index", folder / "index", MOST_INDEX)
    for item in folder.glob("sharedindex.*"):
        item.unlink(missing_ok=True)
    if (folder / "index").exists():
        with os.scandir(own) as listed:  # (A split index's shared part, by its name.)
            for entry in listed:
                if SHARED_INDEX.fullmatch(entry.name):
                    copied(own / entry.name, folder / entry.name, MOST_INDEX)
    try:
        shallow = read_regular(source / "shallow", MOST_SMALL).decode(errors="replace").split()
        kept = "".join(f"{commit}\n" for commit in shallow if HASH.fullmatch(commit))
        written(folder / "shallow", kept.encode())
    except OSError:
        (folder / "shallow").unlink(missing_ok=True)
    copied(source / "info" / "exclude", folder / "info" / "exclude", MOST_SMALL)
    names = set()
    for key, _ in entries:
        section, _, rest = key.partition(".")
        if section.lower() == "filter" and "." in rest:
            names.add(rest.rpartition(".")[0])
    MADE[str(folder)] = (now, sorted(names))
    return environment
