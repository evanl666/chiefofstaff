"""A home of the tests' own (conftest sets HOME to it): never the owner's, so nothing a test
runs reads or writes the owner's git configuration (or anything else of theirs)."""

import os
import stat
import sys
import tempfile
from pathlib import Path

# The longest a home's path may be: a data directory a test makes in it holds a socket, whose
# path (`<home>/.cos-send-XXXXXXXX/data/send/socket`, 36 more) fits macOS's 104 bytes with
# its ending zero. (macOS's temporary folder, /private/var/folders/xx/…/T, is 56 long.)
MOST_PATH = 67


def owners_only(folder: Path) -> bool:
    """Whether no one but its owner (or root) can change a folder or one it is in: none of
    them another user's, or one its group or everyone may write (a shared /tmp)."""
    if sys.platform == "win32":
        return True  # No owner bits there; nothing in the tests' home is a Unix socket.
    me = os.geteuid()
    for part in (folder, *folder.parents):
        try:
            info = part.lstat()
        except OSError:
            return False
        if info.st_uid not in (me, 0) or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False
    return True


def account_home() -> str:
    """The account's own home, whatever HOME says (Windows: the user's profile)."""
    if sys.platform == "win32":
        return os.environ.get("USERPROFILE") or str(Path("~").expanduser())
    import pwd

    return pwd.getpwuid(os.getuid()).pw_dir


def home_for_tests() -> str:
    """A new folder for the tests' home, by its real path (macOS's temporary folder is under
    /var, a link; the app compares Path.home() with resolved paths), short, and in folders
    only its owner can change (the app keeps a socket out of others' reach): in the system's
    temporary folder where that is so (macOS's), else in XDG_RUNTIME_DIR (Linux's /tmp is
    everyone's), else in the account's own home (a container's root)."""
    bases = [tempfile.gettempdir(), os.environ.get("XDG_RUNTIME_DIR", ""),
             account_home()]  # fmt: skip
    for base in filter(None, bases):
        real = Path(os.path.realpath(base))
        if real.is_dir() and len(str(real)) + 10 <= MOST_PATH and owners_only(real):
            return os.path.realpath(tempfile.mkdtemp(prefix="h", dir=real))
    return os.path.realpath(tempfile.mkdtemp(prefix="h"))
