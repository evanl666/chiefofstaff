"""Writing where sandboxed workers write too, from Chief of Staff (which runs unsandboxed), never
through a link one of them made: workers' caches, their temporary folders, their own folders.

A worker can put a link (to the owner's ~/.zshenv, to their home) in place of any file or folder
it may write, even the folder it was given, between Chief of Staff's look at a path and its
write there. So no path under such a folder is ever used whole. Each folder is opened from one
workers cannot change (`under`, opened by its real path), one name at a time, never following a
link (O_NOFOLLOW): a link where a folder should be is an UnsafePathError. Everything else is done
relative to the folder opened: a file is made new (O_EXCL, O_NOFOLLOW) under another name, its
mode set on what was opened, then renamed into place (a rename replaces a link, never follows
it); links, unlinks, renames, deletions and modes by name in that folder.

Not on Windows, where nothing is sandboxed (and folders cannot be opened so): paths are used
as they are there.
"""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import stat
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

SAFE = os.name != "nt" and {
    os.open, os.mkdir, os.rename, os.unlink, os.symlink, os.stat, os.readlink, os.rmdir,
} <= os.supports_dir_fd  # fmt: skip
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
CLOEXEC = getattr(os, "O_CLOEXEC", 0)
BINARY = getattr(os, "O_BINARY", 0)  # Windows: bytes as they are, no newline translation.
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
FOLDER = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | NOFOLLOW | CLOEXEC
# A link where a folder is to be opened: ELOOP (macOS may say ENOTDIR or EMLINK), a file:
# ENOTDIR.
NOT_A_FOLDER = {errno.ELOOP, errno.ENOTDIR, errno.EMLINK}


class UnsafePathError(OSError):
    """A link, or something that is not a folder, where a folder was to be opened."""


class CannotCloneError(OSError):
    """This file system cannot clone (another volume, no reflinks)."""


def check_name(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\0" in name:
        raise ValueError(f"not a name in a folder: {name!r}")
    return name


class Folder:
    """A folder opened without following any link, and what is done in it by name."""

    def __init__(self, fd: int | None, path: Path):
        self.fd = fd
        self.path = path

    def __enter__(self) -> Folder:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self.fd is not None and self.fd >= 0:
            os.close(self.fd)
        self.fd = -1

    def at(self, name: str) -> dict:
        """The keyword arguments that make an os call relative to this folder."""
        return {"dir_fd": self.fd} if SAFE else {}

    def where(self, name: str) -> str:
        """The name to give an os call: the name itself, or its whole path without dir_fd."""
        check_name(name)
        return name if SAFE else str(self.path / name)

    def child(self, name: str, create: bool = False, mode: int = 0o755, relink: bool = False):
        """The folder `name` in this one (made if `create`). `relink`: a link or a file there
        (a worker's) is removed, never followed, and a folder made in its place."""
        check_name(name)
        if not SAFE:
            path = self.path / name
            if relink and (path.is_symlink() or path.is_file()):
                path.unlink()
            if create:
                path.mkdir(mode=mode, exist_ok=True)
            if not path.is_dir():
                raise UnsafePathError(errno.ENOTDIR, f"not a folder: {path}")
            return Folder(None, path)
        fd = step(self.descriptor(), name, create, mode, relink, self.path / name)
        return Folder(fd, self.path / name)

    def descriptor(self) -> int:
        if self.fd is None or self.fd < 0:
            raise ValueError(f"not open: {self.path}")
        return self.fd

    def descend(self, parts: tuple[str, ...] | list[str], create: bool = False) -> Folder:
        """The folder these names down from this one (a new Folder; this one stays open)."""
        folder = self.child(parts[0], create) if parts else open_same(self)
        for part in parts[1:]:
            with folder:
                folder = folder.child(part, create)
        return folder

    def lstat(self, name: str) -> os.stat_result | None:
        """What is at `name` (a link as a link), None when nothing is."""
        with suppress(FileNotFoundError, NotADirectoryError):
            return os.stat(self.where(name), follow_symlinks=False, **self.at(name))
        return None

    def is_folder(self, name: str) -> bool:
        found = self.lstat(name)
        return bool(found and stat.S_ISDIR(found.st_mode))

    def names(self) -> list[str]:
        return sorted(os.listdir(self.fd if SAFE else self.path))

    def readlink(self, name: str) -> str:
        return os.readlink(self.where(name), **self.at(name))

    def read(self, name: str) -> bytes | None:
        """A file's bytes (never through a link), None when there is no such file."""
        try:
            flags = os.O_RDONLY | NOFOLLOW | CLOEXEC | BINARY | NONBLOCK
            fd = os.open(self.where(name), flags, **self.at(name))
        except OSError:
            return None
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                return None
            return file.read()

    def write(self, name: str, data: bytes | str, mode: int = 0o644) -> None:
        """A file at `name` with this content and mode: made new under another name, then
        renamed into place, over whatever a worker put there (a link is replaced, not
        followed)."""
        check_name(name)
        content = data.encode() if isinstance(data, str) else data
        temporary = self.temporary_name(name)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | CLOEXEC | BINARY
        fd = os.open(self.where(temporary), flags, mode, **self.at(temporary))
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(content)
                if hasattr(os, "fchmod"):
                    os.fchmod(file.fileno(), mode)
            self.move(temporary, name)
        except BaseException:
            with suppress(OSError):
                self.delete(temporary)
            raise

    def copy_in(self, source: Path, name: str) -> None:
        """A copy of the owner's file `source` (its mode and times) at `name`, made as write()
        makes a file."""
        check_name(name)
        found = os.stat(source)
        temporary = self.temporary_name(name)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | CLOEXEC | BINARY
        fd = os.open(self.where(temporary), flags, 0o600, **self.at(temporary))
        times = (found.st_atime_ns, found.st_mtime_ns)
        try:
            with open(source, "rb") as origin, os.fdopen(fd, "wb") as copy:
                shutil.copyfileobj(origin, copy, 1 << 20)
                copy.flush()
                if hasattr(os, "fchmod"):
                    os.fchmod(copy.fileno(), stat.S_IMODE(found.st_mode))
                if os.utime in os.supports_fd:
                    os.utime(copy.fileno(), ns=times)
            if os.utime not in os.supports_fd:  # Windows: by its path (no worker writes there).
                os.utime(self.where(temporary), ns=times)
            self.move(temporary, name)
        except BaseException:
            with suppress(OSError):
                self.delete(temporary)
            raise

    def delete_empty(self, name: str) -> None:
        """An empty folder deleted."""
        os.rmdir(self.where(name), **self.at(name))

    def temporary_name(self, name: str) -> str:
        return f".cos-{uuid4().hex[:8]}-{name}"[:255]

    def symlink(self, name: str, target: str | Path, replace: bool = False) -> None:
        """A link at `name` to `target`; FileExistsError when something is there, unless
        `replace` (then made under another name and renamed over it)."""
        if not replace:
            os.symlink(str(target), self.where(name), **self.at(name))
            return
        temporary = self.temporary_name(name)
        os.symlink(str(target), self.where(temporary), **self.at(temporary))
        try:
            self.move(temporary, name)
        except BaseException:
            with suppress(OSError):
                self.delete(temporary)
            raise

    def make(self, name: str, mode: int = 0o755) -> None:
        """A new folder at `name`."""
        os.mkdir(self.where(name), mode, **self.at(name))

    def delete(self, name: str) -> None:
        """A file or link deleted (a link itself, never what it leads to)."""
        os.unlink(self.where(name), **self.at(name))

    def move(self, name: str, to: str, into: Folder | None = None) -> None:
        """`name` renamed `to`, in this folder or `into` another opened one (a link is moved
        as a link; one at `to` is replaced, not followed)."""
        into = into or self
        if SAFE:
            os.rename(check_name(name), check_name(to), src_dir_fd=self.fd, dst_dir_fd=into.fd)
        else:  # os.replace: Windows' os.rename refuses a name that exists.
            os.replace(self.path / check_name(name), into.path / check_name(to))

    def remove(self, name: str, onexc: Callable | None = None) -> None:
        """A file, link or folder (with all in it) removed, never following a link."""
        found = self.lstat(name)
        if found is None:
            return
        if not stat.S_ISDIR(found.st_mode):
            self.delete(name)
        elif SAFE:
            rmtree(name, onexc, dir_fd=self.fd)
        else:
            rmtree(str(self.path / name), onexc)

    def add_mode(self, name: str, bits: int) -> None:
        """These permission bits added to a file or folder (not to a link: nothing then)."""
        found = self.lstat(name)
        if found is None or stat.S_ISLNK(found.st_mode):
            return
        chmod(self, name, stat.S_IMODE(found.st_mode) | bits)

    def copy_tree(self, source: Path, name: str) -> bool:
        """A plain copy of the owner's `source` (a file, a link, or a folder with all in it) at
        `name`, where this file system cannot clone it."""
        try:
            return copy_into(source, self, name)
        except BaseException:
            with suppress(OSError):
                self.remove(name)
            raise

    def clone(self, source: Path, name: str) -> bool:
        """A copy-on-write copy of the owner's `source` (a file or folder) at `name`, which
        must not exist: APFS clonefile on macOS (a folder whole), reflinks file by file on
        Linux. False when it could not be made (nothing is left at `name` then);
        CannotCloneError when this file system cannot clone at all."""
        check_name(name)
        if sys.platform == "darwin":
            return clone_mac(source, self, name)
        try:
            return reflink(source, self, name)
        except BaseException:
            with suppress(OSError):
                self.remove(name)
            raise


def open_same(folder: Folder) -> Folder:
    return Folder(os.dup(folder.fd) if SAFE and folder.fd is not None else None, folder.path)


def step(fd: int, name: str, create: bool, mode: int, relink: bool, path: Path) -> int:
    """Open folder `name` in the folder `fd`, never through a link."""
    for _ in range(5):
        try:
            return os.open(name, FOLDER, dir_fd=fd)
        except FileNotFoundError:
            if not create:
                raise
            with suppress(FileExistsError):
                os.mkdir(name, mode, dir_fd=fd)
        except OSError as exc:
            if exc.errno not in NOT_A_FOLDER:
                raise
            if not relink:
                raise UnsafePathError(
                    exc.errno, f"a link or a file, not a folder: {path}"
                ) from None
            os.unlink(name, dir_fd=fd)  # A worker's link or file: removed, never followed.
    raise UnsafePathError(errno.EAGAIN, f"kept changing: {path}")


def open_folder(
    path: Path | str, under: Path | str, create: bool = False, mode: int = 0o755,
    relink: bool = False,
) -> Folder:  # fmt: skip
    """The folder at `path`, opened from `under` (a folder no worker can change, opened by its
    real path; made if `create`), each name below it without following a link. `relink`
    applies to the last name only."""
    path, under = Path(path), Path(under)
    parts = relative_parts(path, under)
    if create:
        Path(under).mkdir(parents=True, exist_ok=True)
    if not SAFE:
        folder = Folder(None, Path(under))
        for index, part in enumerate(parts):
            folder = folder.child(part, create, mode, relink and index == len(parts) - 1)
        return folder
    fd = os.open(os.path.realpath(under), FOLDER)
    try:
        for index, part in enumerate(parts):
            check_name(part)
            last = relink and index == len(parts) - 1
            following = step(fd, part, create, mode, last, path)
            os.close(fd)
            fd = following
    except BaseException:
        os.close(fd)
        raise
    return Folder(fd, path)


def relative_parts(path: Path, under: Path) -> tuple[str, ...]:
    """The names from `under` down to `path`, which may name `under` by another path (through
    a link above it, as the owner configured it). Only folders down to `under` are resolved."""
    with suppress(ValueError):
        return path.relative_to(under).parts
    real = os.path.realpath(under)
    with suppress(ValueError):
        return path.relative_to(real).parts
    parts = path.parts
    for index in range(1, len(parts) + 1):
        if os.path.realpath(Path(*parts[:index])) == real:
            return parts[index:]
    raise ValueError(f"{path} is not in {under}")


def parent_of(path: Path | str, under: Path | str, create: bool = False) -> tuple[Folder, str]:
    """The folder holding `path` (opened as open_folder does) and its name there."""
    path = Path(path)
    return open_folder(path.parent, under, create), check_name(path.name)


def rmtree(name: str, onexc: Callable | None = None, dir_fd: int | None = None) -> None:
    """shutil.rmtree, which deletes by folder descriptors, never following a link."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(name, onexc=onexc, dir_fd=dir_fd)
    elif onexc is not None:
        shutil.rmtree(name, onerror=lambda f, p, e: onexc(f, p, e[1]), dir_fd=dir_fd)
    else:
        shutil.rmtree(name, dir_fd=dir_fd)


def chmod(folder: Folder, name: str, mode: int) -> None:
    """Set the mode of what is at `name` (never through a link), even one it cannot open."""
    if not SAFE:  # Windows: by its path, as everything there.
        os.chmod(folder.where(name), mode)
        return
    flags = os.O_RDONLY | NOFOLLOW | CLOEXEC | NONBLOCK
    try:
        fd = os.open(folder.where(name), flags, **folder.at(name))
    except PermissionError:
        if sys.platform == "darwin":
            os.chmod(folder.where(name), mode, follow_symlinks=False, **folder.at(name))
            return
        # Linux (no lchmod): what O_PATH opened, through its descriptor's own path.
        fd = os.open(name, getattr(os, "O_PATH", 0) | NOFOLLOW | CLOEXEC, dir_fd=folder.fd)
        try:
            if not stat.S_ISLNK(os.fstat(fd).st_mode):
                os.chmod(f"/proc/self/fd/{fd}", mode)
        finally:
            os.close(fd)
        return
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


# macOS: clonefileat(2) clones a file or a whole folder; CLONE_NOFOLLOW for the source.
AT_FDCWD = -2
CLONE_NOFOLLOW = 0x0001
_libc = None


def clone_mac(source: Path, folder: Folder, name: str) -> bool:
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL("libc.dylib", use_errno=True)
    if SAFE:
        result = _libc.clonefileat(
            AT_FDCWD, os.fsencode(source), folder.fd, os.fsencode(name), CLONE_NOFOLLOW
        )
    else:
        target = os.fsencode(folder.path / name)
        result = _libc.clonefile(os.fsencode(source), target, CLONE_NOFOLLOW)
    if result == 0:
        return True
    code = ctypes.get_errno()
    if code in (errno.EXDEV, errno.ENOTSUP):
        raise CannotCloneError(code, os.strerror(code))
    return False


def copy_into(source: Path, folder: Folder, name: str) -> bool:
    found = os.lstat(source)
    if stat.S_ISLNK(found.st_mode):
        folder.symlink(name, os.readlink(source))
    elif stat.S_ISDIR(found.st_mode):
        folder.make(name, 0o700)
        with folder.child(name) as inside:
            for child in sorted(os.listdir(source)):
                copy_into(source / child, inside, child)
            if SAFE:
                os.chmod(inside.descriptor(), stat.S_IMODE(found.st_mode))
    elif stat.S_ISREG(found.st_mode):
        folder.copy_in(source, name)
    return True


# Linux: the FICLONE ioctl, file by file, where the file system has reflinks (Btrfs, XFS).
FICLONE = 0x40049409


def reflink(source: Path, folder: Folder, name: str) -> bool:
    import fcntl  # Not on Windows (where nothing is sandboxed).

    found = os.lstat(source)
    if stat.S_ISLNK(found.st_mode):
        folder.symlink(name, os.readlink(source))
        return True
    if stat.S_ISDIR(found.st_mode):
        folder.make(name, 0o700)
        with folder.child(name) as inside:
            for child in sorted(os.listdir(source)):
                if not reflink(source / child, inside, child):
                    return False
            if SAFE:
                os.chmod(inside.descriptor(), stat.S_IMODE(found.st_mode))
        return True
    if not stat.S_ISREG(found.st_mode):
        return True  # A socket or a pipe: nothing to copy.
    src = os.open(source, os.O_RDONLY | NOFOLLOW | CLOEXEC)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | CLOEXEC
        dst = os.open(folder.where(name), flags, 0o600, **folder.at(name))
        try:
            fcntl.ioctl(dst, FICLONE, src)
            os.fchmod(dst, stat.S_IMODE(found.st_mode))
            os.utime(dst, ns=(found.st_atime_ns, found.st_mtime_ns))
        except OSError as exc:
            with suppress(OSError):
                folder.delete(name)
            if exc.errno in (errno.EXDEV, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOTTY):
                raise CannotCloneError(exc.errno, os.strerror(exc.errno)) from None
            return False
        finally:
            os.close(dst)
    finally:
        os.close(src)
    return True
