"""`chiefofstaff send`: hand the running Chief of Staff a note, and a link or text, from anywhere
(docs/design/send-anywhere.md). It goes through `send/socket` in the data directory, which Chief
serves while Send to Chief is on (sendsocket.py) and only the owner's user can use: this command
checks the folders on the way and the process answering before it writes a byte, and Chief makes
the same checks of the folders before it opens the socket.

Only the standard library: the command starts in a few hundredths of a second, where Chief's own
modules (FastAPI, the runtime) take most of a second.
"""

import argparse
import errno
import json
import os
import socket
import stat
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

FOLDER, NAME = "send", "socket"
# A Unix socket's path is at most 104 bytes on macOS with its closing NUL (103 without), 108 on
# Linux.
MAX_PATH = 103 if sys.platform == "darwin" else 108
# An item's limits (Chief checks them too: models.SentItem), and a request's: what is sent fits
# in it with room for its head.
MAX_NOTE = MAX_TEXT = 20000
MAX_URL, MAX_TITLE = 2048, 300
MAX_REQUEST = 64 * 1024
MAX_BODY = MAX_REQUEST - 1024
# Its answer after the request was written, and the tries after the first that went unanswered.
TIMEOUT = 10.0
RETRIES = (0.5, 1.0)
SENT, USAGE, ABSENT, LIMITED, REFUSED, UNCONFIRMED = 0, 2, 3, 4, 5, 6
# Windows has no peer credentials to check who connects (Unix sockets there have none).
UNSUPPORTED = "Send to Chief is not supported on Windows"


class NotServingError(Exception):
    """Nothing serves the socket: Chief of Staff is not running, or Send to Chief is off."""


class UnsafeError(Exception):
    """Something there is not the owner's Chief of Staff, or could be replaced by someone else's:
    nothing is written to it."""


# Who may change a folder: its owner, root, and no one else (a folder of root's holds the
# owner's home). macOS keeps more in access control lists, read with acl_get_link_np: these rights
# let another user put, rename or remove what is in a folder, or take the folder over.
WRITES = frozenset(
    "write append add_file add_subdirectory delete delete_child writeattr writeextattr "
    "writesecurity chown".split()
)
ACL_TYPE_EXTENDED = 0x00000100


def _libc():
    import ctypes  # Only on macOS, only when a folder is checked.

    libc = ctypes.CDLL(None, use_errno=True)
    for name in ("acl_get_link_np", "acl_init", "acl_to_text"):
        getattr(libc, name).restype = ctypes.c_void_p
    libc.acl_get_link_np.argtypes = [ctypes.c_char_p, ctypes.c_int]
    libc.acl_set_link_np.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    libc.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
    libc.acl_free.argtypes = [ctypes.c_void_p]
    libc.acl_init.argtypes = [ctypes.c_int]
    return ctypes, libc


def acl_entries(path: Path) -> list[str]:
    """The entries of a file's access control list on macOS, as `ls -le` reads them
    ("group:<uuid>:everyone:12:deny:delete"); none elsewhere. Not followed through a link."""
    if sys.platform != "darwin":
        return []
    ctypes, libc = _libc()
    acl = libc.acl_get_link_np(os.fsencode(path), ACL_TYPE_EXTENDED)
    if not acl:
        failed = ctypes.get_errno()
        if failed in (errno.ENOENT, errno.EOPNOTSUPP, errno.ENOTSUP):
            return []  # None, or none possible on that volume.
        raise OSError(failed, os.strerror(failed), str(path))
    try:
        size = ctypes.c_ssize_t()
        text = libc.acl_to_text(acl, ctypes.byref(size))
        if not text:
            failed = ctypes.get_errno()
            raise OSError(failed, os.strerror(failed), str(path))
        try:
            written = ctypes.string_at(text, size.value).decode("utf-8", "replace")
        finally:
            libc.acl_free(text)
    finally:
        libc.acl_free(acl)
    return [line for line in written.splitlines() if line and not line.startswith("!#acl")]


def acl_opens(path: Path) -> bool:
    """Whether an entry of a file's access control list lets another user, or a group, change it
    or what is in it. Deny entries, and those only for what is made in it later, grant nothing."""
    if sys.platform != "darwin":
        return False  # No access control lists (acl_entries).
    mine = {str(os.geteuid()), "0"}
    for entry in acl_entries(path):
        try:
            head, kinds, rights = entry.rsplit(":", 2)
            tag, _uuid, rest = head.split(":", 2)
            ident = rest.rsplit(":", 1)[1]
        except (ValueError, IndexError):
            return True  # Not read as expected: taken as open.
        flags = kinds.split(",")
        if flags[0] != "allow" or "only_inherit" in flags:
            continue
        if tag == "user" and ident in mine:
            continue
        if WRITES & set(rights.split(",")):
            return True
    return False


def remove_acl(path: Path):
    """As `chmod -N` does: no access control list on it at all (macOS; nothing elsewhere, where
    the mode's group bits mask any POSIX ACL)."""
    if sys.platform != "darwin":
        return
    ctypes, libc = _libc()
    empty = libc.acl_init(0)
    if not empty:
        raise OSError(ctypes.get_errno(), "acl_init failed", str(path))
    try:
        if libc.acl_set_link_np(os.fsencode(path), ACL_TYPE_EXTENDED, empty) != 0:
            failed = ctypes.get_errno()
            raise OSError(failed, os.strerror(failed), str(path))
    finally:
        libc.acl_free(empty)


def open_folder(directory: Path) -> str:
    """The first of a data directory and the folders it is in, up to /, that another user could
    change (and so swap what is inside): a folder owned by someone else than the owner or root, one
    its group or everyone may write (a shared folder with the sticky bit too), or one with an
    access control list that lets others write. "" when there is none. `directory` is resolved:
    a link on the way is not a folder."""
    if sys.platform == "win32":
        return UNSUPPORTED
    me = os.geteuid()
    for folder in (directory, *directory.parents):
        try:
            info = os.lstat(folder)
        except OSError as exc:
            return f"{folder} cannot be read ({exc.strerror})"
        if not stat.S_ISDIR(info.st_mode):
            return f"{folder} is not a folder"
        if info.st_uid not in (me, 0):
            return f"{folder} belongs to another user"
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return f"{folder} is writable by others"
        try:
            opened = acl_opens(folder)
        except OSError as exc:
            return f"{folder} cannot be checked ({exc.strerror})"
        if opened:
            return f"{folder} is writable by others (an access control list)"
    return ""


def peer_uid(connection: socket.socket) -> int:
    """The user id of the process at the other end of a connected Unix socket, as the kernel
    recorded it when it connected."""
    if sys.platform == "darwin":
        # struct xucred: cr_version (XUCRED_VERSION, 0), then cr_uid; level 0 is SOL_LOCAL.
        raw = connection.getsockopt(0, socket.LOCAL_PEERCRED, 76)
        version, uid = struct.unpack_from("=II", raw)
        if version != 0:
            raise OSError(errno.EPROTO, "Unknown peer credentials")
        return uid
    if sys.platform == "linux":
        # struct ucred: pid, uid, gid.
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        return struct.unpack("=iII", raw)[1]
    raise OSError(errno.ENOTSUP, "Peer credentials are not supported here")


def default_data_dir() -> Path:
    """Where Chief of Staff keeps its data unless told (__main__.default_data_dir, which asks
    platformdirs: its rule, without loading it): $XDG_DATA_HOME when it is an absolute path,
    else ~/Library/Application Support on macOS and ~/.local/share elsewhere."""
    named = os.environ.get("XDG_DATA_HOME", "").strip()
    if Path(named).is_absolute():
        base = Path(named)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".local" / "share"
    current, legacy = base / "chiefofstaff", base / "work-buddy"
    return legacy if legacy.is_dir() and not current.exists() else current


def socket_path(directory: Path) -> Path:
    """The send socket of a data directory, checked before connecting to it: the folders on the
    way only the owner can change, `send/` a folder of theirs and the socket theirs (not a link
    to one). Raises NotServingError when nothing is there, UnsafeError when something else is."""
    if sys.platform == "win32":
        raise UnsafeError(UNSUPPORTED)
    directory = directory.expanduser().resolve()
    if not directory.exists():
        raise NotServingError()
    if problem := open_folder(directory):
        raise UnsafeError(f"{problem}, so what is in it cannot be trusted")
    path = directory / FOLDER / NAME
    for part, check in ((path.parent, stat.S_ISDIR), (path, stat.S_ISSOCK)):
        try:
            info = os.lstat(part)
        except FileNotFoundError:
            raise NotServingError() from None
        if not check(info.st_mode) or info.st_uid != os.geteuid():
            raise UnsafeError(f"Something other than your Chief of Staff is at {part}")
    return path


def connect(path: Path) -> socket.socket:
    """A connection to Chief of Staff's socket, once the process answering is known to run as
    this user: before anything is written."""
    if sys.platform == "win32":
        raise UnsafeError(UNSUPPORTED)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(TIMEOUT)
    try:
        connection.connect(str(path))
        if peer_uid(connection) != os.geteuid():
            raise UnsafeError(f"Something other than your Chief of Staff is at {path}")
    except (FileNotFoundError, ConnectionRefusedError):
        connection.close()
        raise NotServingError() from None  # A socket left by a Chief of Staff that stopped.
    except OSError as exc:
        connection.close()
        raise UnsafeError(f"{path} did not let this user in ({exc.strerror or exc})") from None
    except BaseException:
        connection.close()
        raise
    return connection


class UnansweredError(Exception):
    """Written, and no answer read: it may have been taken."""


def post(path: Path, route: str, body: dict) -> tuple[int, dict]:
    """One request to Chief of Staff's socket: its status and what it answered. NotServingError
    or UnsafeError when nothing was written; UnansweredError when something was and no whole
    answer came back."""
    payload = encoded(body)
    head = (
        f"POST {route} HTTP/1.1\r\nHost: chief\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
    ).encode()
    connection = connect(path)
    try:
        connection.sendall(head + payload)
        deadline = time.monotonic() + TIMEOUT
        received = b""
        while chunk := connection.recv(65536):
            received += chunk
            connection.settimeout(max(0.01, deadline - time.monotonic()))
    except OSError as exc:
        raise UnansweredError(str(exc)) from None
    finally:
        connection.close()
    return answer(received)


def answer(received: bytes) -> tuple[int, dict]:
    """An HTTP answer read to its end: its status and JSON body."""
    head, _, rest = received.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    try:
        status = int(lines[0].split(" ", 2)[1])
        fields = dict(line.split(":", 1) for line in lines[1:])
        length = int(next(v for k, v in fields.items() if k.strip().lower() == "content-length"))
        if len(rest) < length:
            raise ValueError("cut short")
        body = json.loads(rest[:length]) if length else {}
    except (IndexError, ValueError, StopIteration) as exc:
        raise UnansweredError(f"no answer read ({exc})") from None
    return status, body if isinstance(body, dict) else {}


def encoded(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False).encode()


def fitted(body: dict) -> dict:
    """The item as it fits in a request: its text cut where it does not (20,000 characters of
    some scripts are more than a request holds)."""
    text = body.get("text", "")
    while text and (over := len(encoded(body)) - MAX_BODY) > 0:
        size = len(encoded({"text": text})) / len(text)  # Bytes a character, as encoded.
        text = text[: max(0, len(text) - int(over / size) - 1)].rstrip()
        body = {**body, "text": text} if text else {k: v for k, v in body.items() if k != "text"}
    return body


def item_id(text: str) -> str:
    if len(text) != 32 or any(c not in "0123456789abcdef" for c in text.lower()):
        raise argparse.ArgumentTypeError("an item id is 32 hexadecimal characters")
    return text.lower()


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="chiefofstaff send",
        description="Send Chief of Staff a note, and a link or text, as one request for its "
        "Router. Chief of Staff must be running, with Send to Chief on (Settings → Connections).",
        epilog="Exit codes: 0 sent (or already taken), 2 usage, 3 not running, off or not "
        "yours (nothing was sent), 4 too many sent lately, 5 not taken while Chief of Staff "
        "reconnects to Copilot, 6 delivery unconfirmed (send again with --item).",
    )
    command.add_argument(
        "note", nargs="*", metavar="NOTE", help="your words: what to do (the request)"
    )
    command.add_argument("--url", default="", help="a link to pass along (http or https)")
    command.add_argument("--title", default="", help="its title")
    command.add_argument(
        "--text-from",
        metavar="FILE|-",
        help="text to pass along, from a file or - (standard input)",
    )
    command.add_argument(
        "--item", type=item_id, metavar="ID", help="send again an item that went unconfirmed"
    )
    command.add_argument(
        "--data-dir", type=Path, metavar="DIR", help="Chief of Staff's data directory"
    )
    return command


def read_text(source: str | None) -> str:
    """Text passed along, read as UTF-8 whatever the locale (a launcher runs the command with
    none): what is not UTF-8 reads as "\ufffd", it does not stop the send."""
    if source is None:
        return ""
    if source == "-":
        return sys.stdin.buffer.read().decode("utf-8", "replace") if sys.stdin else ""
    return Path(source).expanduser().read_text(encoding="utf-8", errors="replace")


def sendable(text: str) -> str:
    """Text as UTF-8 can hold it (as envelope.sendable): bytes of an argument that were not UTF-8
    reach Python as lone surrogates, which become "?"."""
    return text.encode("utf-8", "replace").decode("utf-8")


def clock(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")
    except (TypeError, ValueError):
        return "earlier"


def said(receipt: dict, retried: bool) -> str:
    """What a receipt tells the owner: taken already, as it says, unless by this command's own
    try that went unanswered."""
    if not receipt.get("duplicate") or (retried and not receipt.get("earlier")):
        return "Sent to Chief of Staff."
    at = clock(receipt.get("taken_at", ""))
    if earlier := receipt.get("earlier"):
        return f"Already sent at {at} (item {earlier}): Chief of Staff took it once."
    return f"Already sent: Chief of Staff took this item at {at}."


def main(arguments: list[str] | None = None) -> int:
    command = parser()
    options = command.parse_intermixed_args(arguments)
    note = sendable(" ".join(options.note).strip())
    url, title = sendable(options.url.strip()), sendable(" ".join(options.title.split()))
    try:
        text = read_text(options.text_from).strip()
    except (OSError, UnicodeError) as exc:
        command.error(f"--text-from: {exc}")
    if not (note or url or title or text):
        command.error("send a note, a link (--url) or text (--text-from)")
    if len(note) > MAX_NOTE:
        command.error(f"the note is longer than {MAX_NOTE:,} characters")
    if url and (not url.lower().startswith(("http://", "https://")) or len(url) > MAX_URL):
        command.error(f"--url: an http or https link of at most {MAX_URL:,} characters")
    if len(title) > MAX_TITLE:
        title = title[: MAX_TITLE - 1].rstrip() + "…"
    item = options.item or os.urandom(16).hex()
    given = {"item": item, "note": note, "url": url, "title": title, "text": text[:MAX_TEXT]}
    body = fitted({key: value for key, value in given.items() if value})
    if len(encoded(body)) > MAX_BODY:
        command.error(f"the note is longer than one item holds ({MAX_BODY // 1024} KB)")
    if (kept := len(body.get("text", ""))) < len(text):
        cut = f"The text was cut to its first {kept:,} characters: one item holds no more."
        print(cut, file=sys.stderr)

    def failed(message: str, code: int) -> int:
        print(message, file=sys.stderr)
        if note:
            print(f"Your note: {note}", file=sys.stderr)
        elif url:
            print(f"Your link: {url}", file=sys.stderr)
        return code

    directory = options.data_dir or default_data_dir()
    written = retried = False
    problem = ""
    for wait in (0.0, *RETRIES):
        time.sleep(wait)
        try:
            status, reply = post(socket_path(directory), "/v1/send", body)
        except NotServingError:
            if not written:
                return failed(
                    "Chief of Staff isn't running, or Send to Chief is off: nothing was sent.",
                    ABSENT,
                )
            continue
        except UnsafeError as exc:
            if not written:
                return failed(f"{exc}: nothing was sent.", ABSENT)
            problem = f"{exc}: not sent there."
            break
        except UnansweredError:
            written = retried = True
            continue
        written = True
        if status == 200 and reply.get("item") == item:
            print(said(reply, retried))
            return SENT
        if status == 200:
            retried = True  # A receipt for another item is none.
            continue
        detail = str(reply.get("detail") or "")
        if status == 429:
            return failed(detail or "Too many sent lately: not taken. Try again later.", LIMITED)
        if status == 503 and "not connected" in detail:
            return failed(
                "Chief of Staff is reconnecting to Copilot: not taken. Try again in a moment.",
                REFUSED,
            )
        if status == 503:
            why = detail.rstrip(".") or "it could not deliver it"
            return failed(
                f"Chief of Staff did not take it ({why}). Try again in a moment.", REFUSED
            )
        if status == 400:
            return failed(f"Chief of Staff refused it: {detail or 'not a valid item'}", USAGE)
        return failed(f"Chief of Staff did not take it ({detail.rstrip('.') or status}).", REFUSED)
    unconfirmed = (
        f"Delivery unconfirmed (item {item}): run the same command with --item {item} to retry; "
        "Chief of Staff will not take it twice."
    )
    return failed(f"{problem}\n{unconfirmed}" if problem else unconfirmed, UNCONFIRMED)
