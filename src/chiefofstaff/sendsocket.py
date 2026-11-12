"""Send to Chief's socket: `send/socket` in the data directory, served in Chief of Staff's own
event loop while the setting is on (docs/design/send-anywhere.md, 4.2), and what an item sent
there becomes for the Router (4.4). `chiefofstaff send` (send.py) is its client.

The socket is the way in, so it is opened only where no one else can reach it: the data directory
and every folder it is in writable by no one but the owner (or root), `send/` the owner's alone,
and the socket `0600`. A connection is accepted here, its peer's user id checked, and only then
read: by Chief of Staff's own user, or closed. One request per connection, at most 64 KB, read
within 5 s; two routes: POST /v1/send and POST /v1/hello. A request with an Origin is refused (no
browser can reach a Unix socket, and this keeps it so). There is no key: these checks are the
credential, and turning the setting off revokes it.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import re
import socket
import stat
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

import h11
from pydantic import ValidationError

from . import threads
from .envelope import UNTRUSTED, wrap
from .models import SentItem
from .send import (
    FOLDER,
    MAX_PATH,
    MAX_REQUEST,
    NAME,
    UNSUPPORTED,
    open_folder,
    peer_uid,
    remove_acl,
)

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

READ_TIMEOUT = 5.0
HEADLINE = 300
API = 1
# Who sent it, as the Router and Home name it.
CLIENT = "command line"
REASONS = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    413: "Content Too Large",
    429: "Too Many Requests",
    503: "Service Unavailable",
}
ROUTES = {b"/v1/send", b"/v1/hello"}
# Errors of accept() that pass: the connection is waited for again, or it went away.
ACCEPT_AGAIN = (BlockingIOError, InterruptedError, ConnectionAbortedError)
# Out of descriptors or memory: accepting rests a moment instead of spinning.
ACCEPT_LATER = {errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM}


class NotOpenedError(Exception):
    """Why the socket is not opened: said in Settings, in place of the command."""


class LimitedError(Exception):
    """More items than Chief of Staff takes in a while: nothing was stored."""


def prepare(directory: Path) -> tuple[socket.socket, tuple[int, int]]:
    """The send socket, bound and listening, and what identifies its file; under the data
    directory's lock (service.lock), which no other Chief of Staff can hold. NotOpenedError when
    the folders on the way are not the owner's alone, or something else is where it goes."""
    if sys.platform == "win32":
        raise NotOpenedError(UNSUPPORTED)
    me = os.geteuid()
    path = directory / FOLDER / NAME
    if len(os.fsencode(path)) > MAX_PATH:
        raise NotOpenedError("The data directory's path is too long for a socket")
    if problem := open_folder(directory):
        raise NotOpenedError(problem)
    folder = path.parent
    with suppress(FileExistsError):
        folder.mkdir(mode=0o700)
    info = os.lstat(folder)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != me:
        raise NotOpenedError(f"{folder} is not a folder of yours: it was left alone")
    folder.chmod(0o700)
    remove_acl(folder)
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        if not (stat.S_ISSOCK(found.st_mode) and found.st_uid == me):
            raise NotOpenedError(f"{path} is not a socket of yours: it was left alone")
        # One that stopped left it (no Chief of Staff can serve it while this one holds the
        # lock, and only the owner can write in send/).
        path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        path.chmod(0o600)
        remove_acl(path)
        bound = os.lstat(path)
        if not (stat.S_ISSOCK(bound.st_mode) and bound.st_uid == me):
            raise NotOpenedError(f"{path} is not the socket just opened")
        listener.listen(16)
        listener.setblocking(False)
    except BaseException:
        listener.close()
        raise
    return listener, (bound.st_dev, bound.st_ino)


class SendSocket:
    """The socket's lifetime: opened when the setting is turned on and at each start while it is
    on, closed (its file removed) when it is turned off and at shutdown."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.path = runtime.store.directory / FOLDER / NAME
        self.listener: socket.socket | None = None
        self.identity: tuple[int, int] | None = None
        self.connections: set[asyncio.Transport] = set()
        self.accepting: set[asyncio.Future] = set()
        self.resting: asyncio.TimerHandle | None = None
        # Why it could not be opened, for Settings ("" while it is open, or off).
        self.problem = ""

    def switch(self, on: bool):
        if on:
            self.open()
        else:
            self.close()

    def open(self):
        if self.listener:
            return
        try:
            self.listener, self.identity = prepare(self.runtime.store.directory)
        except NotOpenedError as exc:
            self.problem = f"{exc}: the command stays off."
        except OSError as exc:
            self.problem = (
                f"Could not open {self.path} ({exc.strerror or exc}): the command stays off."
            )
        else:
            self.problem = ""
            asyncio.get_running_loop().add_reader(self.listener.fileno(), self.accept)
            return
        logger.warning("Send to Chief: %s", self.problem)

    def close(self):
        """No new connection from now on; those open end now, the file goes."""
        self.problem = ""
        listener, self.listener = self.listener, None
        if self.resting:
            self.resting.cancel()
            self.resting = None
        if listener:
            with suppress(ValueError, OSError):
                asyncio.get_running_loop().remove_reader(listener.fileno())
            listener.close()
        for future in list(self.accepting):
            future.cancel()
        for transport in list(self.connections):
            transport.abort()
        self.connections.clear()
        if self.identity:
            with suppress(OSError):
                info = os.lstat(self.path)
                if (info.st_dev, info.st_ino) == self.identity:
                    self.path.unlink()
            self.identity = None

    def accept(self):
        """A connection: its peer's user id is checked before a byte of it is read; another
        user's is closed at once."""
        if sys.platform == "win32" or not self.listener:  # Windows: prepare refused.
            return
        try:
            connection, _ = self.listener.accept()
        except ACCEPT_AGAIN:
            return
        except OSError as exc:
            if exc.errno in ACCEPT_LATER:
                self.rest()
            else:
                logger.warning("Send to Chief: could not accept a connection (%s).", exc.strerror)
            return
        try:
            peer = peer_uid(connection)
        except OSError:
            peer = None
        if peer != os.geteuid():
            connection.close()
            logger.warning("Send to Chief: closed a connection from another user (%s).", peer)
            return
        connection.setblocking(False)
        loop = asyncio.get_running_loop()
        future = asyncio.ensure_future(
            loop.connect_accepted_socket(lambda: Connection(self), connection)
        )
        self.accepting.add(future)
        future.add_done_callback(self.accepted)

    def accepted(self, future: asyncio.Future):
        self.accepting.discard(future)
        if not future.cancelled() and (failed := future.exception()):
            logger.warning("Send to Chief: a connection failed to start (%s).", failed)

    def rest(self):
        """Out of descriptors: no accepting for a second, then again."""
        if not self.listener or self.resting:
            return
        loop = asyncio.get_running_loop()
        loop.remove_reader(self.listener.fileno())

        def again():
            self.resting = None
            if self.listener:
                loop.add_reader(self.listener.fileno(), self.accept)

        self.resting = loop.call_later(1.0, again)

    async def take(self, raw: bytes) -> tuple[int, dict]:
        """A request to send an item: its receipt, or why it was not taken."""
        try:
            item = SentItem.model_validate(json.loads(raw))
        except ValidationError as exc:
            problem = exc.errors(include_url=False)[0]
            where = ".".join(str(part) for part in problem["loc"])
            said = problem["msg"].removeprefix("Value error, ")
            return 400, {"detail": f"{where}: {said}" if where else said}
        except (ValueError, RecursionError):  # Nested past Python's limit: not an item either.
            return 400, {"detail": "Send a JSON object."}
        try:
            return 200, await self.runtime.on_sent(item, CLIENT)
        except LimitedError as exc:
            return 429, {"detail": str(exc)}
        except Exception as exc:
            if not isinstance(exc, RuntimeError | ValueError):
                logger.exception("Send to Chief: an item could not be taken.")
            return 503, {"detail": str(exc)}


class Connection(asyncio.Protocol):
    """One request, read whole (h11) within READ_TIMEOUT, then answered and closed."""

    def __init__(self, owner: SendSocket):
        self.owner = owner
        self.http = h11.Connection(h11.SERVER, max_incomplete_event_size=MAX_REQUEST)
        self.transport: asyncio.Transport | None = None
        self.timer: asyncio.TimerHandle | None = None
        self.received = 0
        self.route = b""
        self.body = bytearray()
        self.answered = self.handling = False
        self.started = time.monotonic()

    def connection_made(self, transport: asyncio.BaseTransport):
        self.transport = cast(asyncio.Transport, transport)
        self.owner.connections.add(self.transport)
        self.timer = asyncio.get_running_loop().call_later(READ_TIMEOUT, self.timed_out)

    def connection_lost(self, exc: Exception | None):
        if self.timer:
            self.timer.cancel()
        if self.transport:
            self.owner.connections.discard(self.transport)

    def timed_out(self):
        self.respond(408, {"detail": f"Send the request within {READ_TIMEOUT:.0f} s."})

    def data_received(self, data: bytes):
        if self.answered or self.handling:
            return
        self.received += len(data)
        if self.received > MAX_REQUEST:
            self.respond(413, {"detail": f"A request holds at most {MAX_REQUEST // 1024} KB."})
            return
        self.read(data)

    def eof_received(self) -> bool:
        if not (self.answered or self.handling):
            self.read(b"")
        return self.handling  # Answered once handled: kept open for it.

    def read(self, data: bytes):
        try:
            self.http.receive_data(data)
            while not self.answered:
                event = self.http.next_event()
                if event is h11.NEED_DATA:
                    return
                if isinstance(event, h11.Request):
                    self.request(event)
                elif isinstance(event, h11.Data):
                    self.body += event.data
                elif isinstance(event, h11.EndOfMessage):
                    self.handle()
                    return
                else:
                    self.respond(400, {"detail": "The request ended before it was whole."})
        except h11.RemoteProtocolError:
            self.respond(400, {"detail": "Not an HTTP request."})

    def request(self, event: h11.Request):
        headers = {name.lower() for name, _ in event.headers}
        if b"origin" in headers:
            self.respond(403, {"detail": "Not from a browser."})
        elif event.target not in ROUTES:
            self.respond(404, {"detail": "POST /v1/send or /v1/hello."})
        elif event.method != b"POST":
            self.respond(405, {"detail": "POST only."})
        else:
            self.route = event.target

    def handle(self):
        """The request is whole: answered once handled (reading stops here)."""
        self.handling = True
        if self.timer:
            self.timer.cancel()
        if self.transport:
            self.transport.pause_reading()
        self.owner.runtime.background(self.answer())

    async def answer(self):
        if self.route == b"/v1/hello":
            status, body = 200, {"api": API}
        else:
            status, body = await self.owner.take(bytes(self.body))
        self.respond(status, body)

    def respond(self, status: int, body: dict[str, Any]):
        took = (time.monotonic() - self.started) * 1000
        # What happened and how long it took; never what was sent.
        logger.info("Send to Chief: %s %s in %.0f ms", self.route.decode() or "-", status, took)
        if self.answered or not self.transport or self.transport.is_closing():
            self.answered = True
            return
        self.answered = True
        if self.timer:
            self.timer.cancel()
        payload = json.dumps(body).encode()
        head = (
            f"HTTP/1.1 {status} {REASONS[status]}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\nCache-Control: no-store\r\nConnection: close\r\n"
            "\r\n"
        )
        self.transport.write(head.encode() + payload)
        self.transport.close()


# What a sent item becomes (4.4).

GITHUB_ITEM = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(pull|issues)/(\d+)(?:[/?#]|$)"
)
# Characters a link may hold that the page would read as the end of a link, or as markup.
NOT_IN_LINK = re.compile(r"""[^\x21-\x7e]|[<>\[\]{}`'"*|]""")


def reference(url: str) -> tuple[str, str, list[str]]:
    """What a link is, for the Router: its kind and reference ("GitHub issue", "acme/api#117")
    and the thread keys a session can follow it by (a pull request, a Slack message)."""
    if found := GITHUB_ITEM.match(url):
        repo, kind, number = found.groups()
        if kind == "pull":
            return "GitHub pull request", f"{repo}#{number}", threads.pull_requests(url)
        return "GitHub issue", f"{repo}#{number}", []  # Issues are not followed.
    host = (urlsplit(url).hostname or "").removesuffix(".")
    if host == "slack.com" or host.endswith(".slack.com"):
        keys = [key for found in threads.slack_messages(url).values() for key in found]
        return "", "", list(dict.fromkeys(keys))
    return "", "", []


def captured(item: SentItem, client: str) -> str:
    """The capture, as the Router reads it: what it is and where it came from, never the
    owner's words."""
    if not (item.url or item.title or item.text):
        return ""
    lines = [f"Sent with Send to Chief from the {client}."]
    if item.url:
        lines.append(f"Link: {item.url}")
    if item.title:
        lines.append(f"Title: {item.title}")
    kind, ref, keys = reference(item.url)
    if ref:
        lines.append(f"{kind}: {ref}")
    if keys:
        lines.append(f"Thread keys: {', '.join(keys)}")
    if item.text:
        lines += ["Text:", item.text]
    return "\n".join(lines)


def prompt(item: SentItem, client: str, reply_in: str) -> str:
    """The item for the Router: the note as the owner's message, the capture as untrusted
    material, each in an envelope Chief of Staff writes (the bodies escaped)."""
    short = item.item[:8]
    parts = []
    if item.note:
        parts.append(wrap("owner", "message", item.note, via=client, item=short, reply_in=reply_in))
    if capture := captured(item, client):
        parts.append(
            wrap(
                "capture",
                "delegated",
                capture,
                trust=UNTRUSTED,
                item=short,
                reply_in="" if item.note else reply_in,
            )
        )
    return "\n\n".join(parts)


def fenced(text: str) -> str:
    """Text as a code block whose fence is longer than any run of backticks in it: nothing in it
    can end the block, or show as formatting or a link. Without the bidirectional controls the
    page drops before it reads Markdown (static/dom.js withoutBidi): there, "``\u202e``" is four
    backticks."""
    text = BIDI_CONTROLS.sub("", text)
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


# As static/dom.js BIDI_CONTROLS.
BIDI_CONTROLS = re.compile("[\u202a-\u202e\u2066-\u2069]")


def parts(item: SentItem) -> dict[str, str]:
    """What Home's line opens to, field by field (static/session.js sentEntry draws each as
    words, never as Markdown): the note, the link, its title, the text passed along."""
    found = {"note": item.note, "link": item.url, "title": item.title, "text": item.text}
    return {key: value for key, value in found.items() if value}


def shown(item: SentItem) -> str:
    """The item as Markdown, inert, for a view that draws the message's text (an earlier
    version, after a downgrade): the note as the owner wrote it, the link as a link to exactly
    that address, and the page's title and text as they were, in a code block."""
    parts = [item.note] if item.note else []
    if item.url:
        parts.append(f"Link: {linked(item.url)}")
    page = [f"Title: {item.title}"] if item.title else []
    if item.text:
        page += ["Text:", item.text] if item.title else [item.text]
    if page:
        parts.append(fenced("\n".join(page)))
    return "\n\n".join(parts)


def linked(url: str) -> str:
    """A link the page shows as one link to that address, whole: what would end it or read as
    markup there is percent-encoded, as is punctuation at its end (else read as the sentence's)."""
    body, end = re.fullmatch(r"(.*?)([.,;:!?)]*)", url, re.DOTALL).groups()  # type: ignore[union-attr]
    return NOT_IN_LINK.sub(lambda found: escaped(found.group()), body) + escaped(end)


def escaped(characters: str) -> str:
    return "".join(f"%{byte:02X}" for byte in characters.encode())


def headline(item: SentItem) -> str:
    """The item in a line: the note's first line; without one, the GitHub reference and the
    title, else the title, else the link, else the text's first line."""
    first = next((line.strip() for line in item.note.splitlines() if line.strip()), "")
    _, ref, _ = reference(item.url)
    text = next((line.strip() for line in item.text.splitlines() if line.strip()), "")
    line = first or (f"{ref} · {item.title}" if ref and item.title else "")
    line = line or item.title or ref or item.url or text
    return line if len(line) <= HEADLINE else line[: HEADLINE - 1].rstrip() + "…"
