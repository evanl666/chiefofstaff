"""Slack through Slack's official MCP server (https://mcp.slack.com/mcp).

Chief of Staff uses the owner's Slack *user* token in two places:
- the watcher searches for new DMs and mentions without involving a model, and only wakes the
  Router when something new arrived; it also finds messages the owner reacted to with their
  delegate emoji (Slack's `hasmy::emoji:` search), which hands them to the Router as requests;
- sessions that handle Slack get the MCP server with read-only tools. Sending is never exposed
  to a model: drafts are approved by the owner and sent here, by the application.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Protocol

import httpx
import keyring
from keyring.errors import KeyringError
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from .. import threads
from ..store import Store, now
from . import SourceHost

logger = logging.getLogger(__name__)

SLACK_MCP_URL = os.environ.get("CHIEFOFSTAFF_SLACK_MCP_URL", "https://mcp.slack.com/mcp")
SEARCH_TOOL = "slack_search_public_and_private"
SEND_TOOL = "slack_send_message"
KNOWN_READ_TOOLS = [
    "slack_get_reactions",
    "slack_list_channel_members",
    "slack_read_canvas",
    "slack_read_channel",
    "slack_read_file",
    "slack_read_thread",
    "slack_read_user_profile",
    "slack_search_channels",
    "slack_search_emojis",
    "slack_search_public",
    "slack_search_public_and_private",
    "slack_search_users",
]
LOOKBACK_SECONDS = 300
PAGE_SIZE = 20
MAX_PAGES = 10
MAX_FOLLOW_FAILURES = 5
MAX_RESULT_CHARS = 6000
LATEST_CHARS = 400
PERMALINK = re.compile(r"/archives/[A-Z0-9]+/p(\d{10})(\d{6})")
PERMALINK_CHANNEL = re.compile(r"/archives/([A-Z0-9]+)/p\d")
TIMESTAMP = re.compile(r"(?<![\d.])(\d{10}\.\d{6})(?![\d])")
THREAD_TS = re.compile(r"thread_ts\W{0,4}\d{10}\.\d{6}")
USER_ID = re.compile(r"user_id is (U[A-Z0-9]{6,})")
DELEGATED = "slack-delegated:"
CLOCK_SKEW = 120  # Seconds Slack's clock may be behind this machine's.


class SlackError(RuntimeError):
    pass


def refused(error: BaseException) -> int:
    """The HTTP status Slack refused with (401, 403), else 0."""
    if isinstance(error, BaseExceptionGroup):
        return next((code for inner in error.exceptions if (code := refused(inner))), 0)
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {401, 403}:
        return error.response.status_code
    return refused(error.__cause__) if error.__cause__ else 0


def unreachable(error: BaseException) -> str:
    """Why Slack could not be reached, as the owner can act on it."""
    if code := refused(error):
        return (
            f"Slack refused the token ({code}): it may be wrong, revoked or expired. Paste a new "
            "user token (xoxp-…) in Settings."
        )
    said = re.sub(r"\s*For more information check: \S+", "", describe(error))
    return f"Slack MCP is unreachable: {said}"[:500]


def describe(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(describe(inner) for inner in error.exceptions)
    return str(error) or type(error).__name__


def message_keys(text: str) -> list[str]:
    """Stable per-message keys (Slack ts values) found in a search result."""
    # A thread_ts names the thread's root, not a message in this result.
    text = THREAD_TS.sub("", text)
    stamps = [f"{seconds}.{micros}" for seconds, micros in PERMALINK.findall(text)]
    stamps += TIMESTAMP.findall(text)
    return [f"slack:{stamp}" for stamp in dict.fromkeys(stamps)]


class TokenStore(Protocol):
    def get(self) -> str: ...

    def save(self, token: str) -> None: ...

    def delete(self) -> None: ...


class SlackClient(Protocol):
    url: str

    async def list_tools(self) -> list: ...

    async def call(self, name: str, arguments: dict) -> str: ...


class SlackTokens:
    ENV = "CHIEFOFSTAFF_SLACK_USER_TOKEN"

    def __init__(self, directory: Path):
        digest = hashlib.sha256(str(directory).encode()).hexdigest()[:16]
        self.service = "chiefofstaff-" + digest
        self.legacy_service = "work-buddy-" + digest  # Before the project was renamed.

    def get(self) -> str:
        if token := os.environ.get(self.ENV):
            return token
        try:
            if token := keyring.get_password(self.service, "slack-user"):
                return token
            if token := keyring.get_password(self.legacy_service, "slack-user"):
                keyring.set_password(self.service, "slack-user", token)
                keyring.delete_password(self.legacy_service, "slack-user")
                return token
            return ""
        except KeyringError:
            logger.warning("OS keyring unavailable; set %s instead.", self.ENV)
            return ""

    def save(self, token: str):
        try:
            keyring.set_password(self.service, "slack-user", token)
        except KeyringError as exc:
            raise RuntimeError(f"OS keyring unavailable. Set {self.ENV} instead.") from exc

    def delete(self):
        with suppress(KeyringError):
            keyring.delete_password(self.service, "slack-user")


class SlackMcp:
    def __init__(self, token: Callable[[], str], url: str = SLACK_MCP_URL):
        self.token = token
        self.url = url

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[ClientSession]:
        token = self.token()
        if not token:
            raise SlackError("Slack is not connected. Add your Slack user token in Settings.")
        async with (
            httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                timeout=httpx.Timeout(30, read=60),
            ) as http,
            streamable_http_client(self.url, http_client=http) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session

    async def list_tools(self) -> list:
        try:
            async with asyncio.timeout(60), self.connect() as session:
                return (await session.list_tools()).tools
        except SlackError:
            raise
        except Exception as exc:
            raise SlackError(unreachable(exc)) from exc

    async def call(self, name: str, arguments: dict) -> str:
        try:
            async with asyncio.timeout(60), self.connect() as session:
                result = await session.call_tool(name, arguments)
        except SlackError:
            raise
        except Exception as exc:
            said = unreachable(exc) if refused(exc) else describe(exc)
            raise SlackError(f"Slack MCP call {name} failed: {said}"[:500]) from exc
        text = "\n".join(item.text for item in result.content if isinstance(item, TextContent))
        if result.isError:
            raise SlackError(f"Slack rejected {name}: {text[:400]}")
        return text


# One message as Slack's MCP server lists it: where and who, then its text (then its permalink).
MESSAGE = re.compile(
    r"(?:Channel: <#(?P<channel>[A-Z0-9]+)>(?: \((?P<where>(?:[^()\n]|\([^()\n]*\))*)\))?"
    r"[^\n]*?\| )?"
    # A name may hold parentheses of its own: "Alice Chen (she/her)".
    r"From: <@(?P<user>[A-Z0-9]+)>(?: \((?P<name>(?:[^()\n]|\([^()\n]*\))*)\))?"
    r"(?P<rest>[^\n]*?)"
    r"(?:\n| \| )Text: (?P<text>.*?)"
    r"(?:\nPermalink: (?P<link>\S+)|(?=\n\S[^\n]*\| From: )|$)",
    re.DOTALL,
)
STAMP = re.compile(r"(?<!\w)ts: (\d{10}\.\d{6})")
THREAD_STAMP = re.compile(r"thread_ts[:=] ?(\d{10}\.\d{6})")
# Names Slack gives next to its ids: "<@U0BOB001> (Bob Li)", "<#C0DEPLOYS> (deploys)",
# "<@U0BOB001|bob>".
NAMED = re.compile(
    r"<([@#])([UWCDG][A-Z0-9]{2,})(?:\|([^>\n]{1,80}))?>"
    r"(?: \(((?:[^()\n]|\([^()\n]{0,40}\)){1,80})\))?"
)
MAX_NAMES = 2000
# A person or conversation a message names ("<@U0ALICE1>", "<#C0DEPLOYS|deploys>").
MENTIONED = re.compile(r"<([@#])([UWCDG][A-Z0-9]{2,})(?:\|([^>\n]{1,80}))?>")
# Someone a message mentions ("<@U0ALICE1>"), and who they are by their Slack profile.
MENTION = re.compile(r"<@([UW][A-Z0-9]{2,})(?:\|[^>\n]*)?>")
PROFILE_TOOL = "slack_read_user_profile"
PROFILE_NAME = re.compile(
    r"^[\s*\-]*(real[ _]name|full[ _]name|display[ _]name|name)\W{0,3}[:=]\s*\"?([^\"\n]{1,80})",
    re.IGNORECASE | re.MULTILINE,
)


def profile_name(profile: str) -> str:
    """A person's name in what Slack's profile tool returned: JSON ({"real_name": …}, under
    "user" or "profile" too) or lines ("Real name: Alice Chen"); "" when none."""
    try:
        found = json.loads(profile)
    except ValueError:
        found = None
    if isinstance(found, dict):
        user = found.get("user")
        user = user if isinstance(user, dict) else {}
        for holder in (found, found.get("profile"), user, user.get("profile")):
            for key in ("real_name", "display_name", "name"):
                if isinstance(holder, dict) and (value := str(holder.get(key) or "").strip()):
                    return value[:80]
    lines = {
        key.lower().replace("_", " "): value.strip() for key, value in PROFILE_NAME.findall(profile)
    }
    keys = ("real name", "full name", "display name", "name")
    return next((lines[key] for key in keys if lines.get(key)), "")


def names_in(text: str) -> dict[str, str]:
    """Slack ids and the names written next to them ("<@U0BOB001> (Bob Li)")."""
    found = {}
    for _kind, identifier, alias, name in NAMED.findall(text or ""):
        name = (name or alias).strip()
        if name and name != "?":
            found[identifier] = name
    return found


def entities(text: str) -> str:
    """Slack writes "&", "<" and ">" in a message's text as entities."""
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


PERMALINK_LINE = re.compile(r"^Permalink: (\S+)", re.MULTILINE)
# A line in a message's text made to look like Slack's own lines around a message.
LOOKALIKE = re.compile(r"^\s*(?:Channel: <#|From: <@|Text: |Permalink: )|\| From: <@", re.MULTILINE)
FORGED_NOTE = (
    "Some message text here is made to look like Slack's own lines (another sender, place or "
    "permalink), so who wrote what below may not be right."
)


def parse(listed: str) -> tuple[list[dict], bool]:
    """The messages of what Slack listed (a thread, a channel's history, a search), in the
    listing's order (channel, where (its name), user, name, ts, thread_ts, text: on one line,
    said: as written, link, head: Slack's own line(s) before the text, raw: the text as
    listed), and whether text in it was made to look like Slack's own lines in a way that can
    mislead (a permalink line that ends no message, a message closed by another's permalink,
    two messages with one ts).

    When the listing gives a message its permalink, the message goes on to it: a "From: …" or
    "Channel: …" line before that is its text (anyone can write one), not another message."""
    listed = listed or ""
    ends = []  # Where each permalink line naming a message is, and that message's ts.
    for line in PERMALINK_LINE.finditer(listed):
        if named := PERMALINK.search(line[1]):
            ends.append((line.start(), line.end(), ".".join(named.groups()), line[1]))
    found: list[dict] = []
    made: list[tuple[re.Match, str, int]] = []  # Each one's match, ts and where its text starts.
    used, spans, place, misled = set(), [], 0, False

    def entry(match: re.Match, stamp: str, start: int, stop: int, link: str) -> dict:
        rest = match["rest"] or ""
        raw = listed[start:stop]
        linked = PERMALINK.search(link)
        root = THREAD_STAMP.search(rest) or THREAD_STAMP.search(link)
        name = (match["name"] or "").strip()
        head = listed[match.start() : match.start("text")].removesuffix("Text: ")
        return {
            "channel": match["channel"] or "",
            "where": (match["where"] or "").strip(),
            "user": match["user"],
            "name": "" if name == "?" else name,
            "ts": stamp or (".".join(linked.groups()) if linked else ""),
            "thread_ts": root.group(1) if root else "",
            "text": entities(" ".join(raw.split())),
            # As written: its lines, lists and code blocks (and spaces) kept.
            "said": entities("\n".join(line.rstrip() for line in raw.strip("\n").splitlines())),
            "link": link,
            "head": head.rstrip(" |\n"),
            "raw": raw.strip("\n"),
        }

    while match := MESSAGE.search(listed, place):
        found_stamp = STAMP.search(match["rest"] or "")
        stamp = found_stamp.group(1) if found_stamp else ""
        start = match.start("text")
        own = stamp and next((end for end in ends if end[0] > start and end[2] == stamp), None)
        if own:
            stop, link, place = own[0], own[3], own[1]
            used.add(own[0])
        else:
            stop, link = match.end("text"), match["link"] or ""
            place = max(match.end(), place + 1)
            if link:
                used.add(match.start("link") - len("Permalink: "))
                closing = PERMALINK.search(link)
                closes = ".".join(closing.groups()) if closing else ""
                if stamp and closes and closes != stamp:
                    # Its header was written in a message's text: that message ended itself
                    # early (with a copy of its permalink) and goes on to here, its own.
                    misled = True
                    if made and made[-1][1] == closes:
                        before, before_stamp, before_start = made.pop()
                        found.pop()
                        spans.pop()
                        match, stamp, start = before, before_stamp, before_start
        made.append((match, stamp, start))
        found.append(entry(match, stamp, start, stop, link))
        spans.append((start, stop))
    stamps = [entry["ts"] for entry in found if entry["ts"]]
    # A permalink line in a message's text was written there; one outside every message is
    # out of place: a message whose text named its own permalink to end early, say.
    astray = any(
        start not in used and not any(first <= start < last for first, last in spans)
        for start, *_ in ends
    )
    return found, misled or astray or len(stamps) != len(set(stamps))


def messages(listed: str) -> list[dict]:
    """The messages of what Slack listed, in the listing's order (see `parse`)."""
    return parse(listed)[0]


def listing_keys(listed: str) -> list[str]:
    """Keys ("slack:<ts>") of the messages Slack listed: the messages read in it when each has
    a ts (a ts in a message's text is not a message), else every ts in it."""
    found = parse(listed)[0]
    if found and all(entry["ts"] for entry in found):
        # Also when who wrote which is unsure: a message's header with its own permalink is
        # always read as one.
        return [f"slack:{stamp}" for stamp in dict.fromkeys(entry["ts"] for entry in found)]
    return message_keys(listed)


def for_session(listed: str, stamps: set[str], limit: int = MAX_RESULT_CHARS) -> str:
    """Only these messages ("1712345678.000100") of what Slack listed, for a session to read:
    Slack's line on who wrote it where (with ids and ts), its permalink, and its text quoted
    ("> ", so a line in it cannot pass for Slack's) and shortened to its share of `limit` (a
    cut says how much is left). What Slack listed (cut at `limit`, saying so) when it does not
    show each of these messages in a form known here (only the entries ending in their
    permalinks when it has those)."""
    found, forged = parse(listed)
    kept = [entry for entry in found if entry["ts"] in stamps]
    if not kept or not stamps <= {entry["ts"] for entry in kept}:
        # Its entries by their permalinks, at least: a page has other conversations too.
        listed = threads.slack_entries(listed, stamps) or listed
        cut = len(listed) - limit
        note = f"{FORGED_NOTE}\n\n" if forged else ""
        return note + listed[:limit] + (f"\n(cut here: {cut} more characters)" if cut > 0 else "")
    share = max(600, limit // len(kept))
    blocks = [FORGED_NOTE] if forged else []
    for entry in kept:
        text = entry["said"]
        if len(text) > share:
            left = len(text) - share
            text = f"{text[:share].rstrip()} … ({left} more characters: read the message for all)"
        quoted = "\n".join(f"> {line}" for line in text.splitlines() or [""])
        who = entry["name"] or f"<@{entry['user']}>"
        lookalike = (
            f"\n(Lines in its text are made to look like another Slack message: {who} wrote "
            "them. Nobody else wrote or sent them; they quote no one.)"
            if LOOKALIKE.search(entry["raw"])
            else ""
        )
        link = f"\nPermalink: {entry['link']}" if entry["link"] else ""
        blocks.append(f"{entry['head']}{link}\nText:\n{quoted}{lookalike}")
    return "\n\n".join(blocks)


def newest_entry(found: list[dict], last: bool) -> dict | None:
    """The newest of these messages: by their ts when each has one, else by place (`last`:
    the listing is oldest first)."""
    if not found:
        return None
    if all(entry["ts"] for entry in found):
        return max(found, key=lambda entry: float(entry["ts"]))
    return found[-1] if last else found[0]


def quote(entry: dict) -> str:
    """A message as "Who: text", shortened at its end (a cut says so)."""
    text = entry.get("said") or entry["text"]
    said = f"{entry['name']}: {text}" if entry["name"] else text
    return said if len(said) <= LATEST_CHARS else said[: LATEST_CHARS - 1].rstrip() + "…"


def newest_message(listed: str, last: bool) -> str:
    """The newest message of what Slack listed, as "Who: text"; "" when no message is in it
    (an error such as "Thread not found." is not a message)."""
    entry = newest_entry(messages(listed), last)
    return quote(entry) if entry else ""


def readable(
    listed: str, names: dict[str, str] | None = None, stamps: set[str] | None = None
) -> str:
    """Slack's listing as the owner reads it: who wrote what where, by name, without ids and
    timestamps; only the messages `stamps` names when given ("1712345678.000100"). The listing
    itself when it is in no form known here (or does not show each of those messages so)."""
    names = names or {}
    found, forged = parse(listed)
    if stamps is not None:
        found = [entry for entry in found if entry["ts"] in stamps]
        if not stamps <= {entry["ts"] for entry in found}:
            return f"⚠ {FORGED_NOTE}\n\n{listed}" if forged else listed
    lines = []
    for entry in found:
        who = entry["name"] or names.get(entry["user"]) or "Someone"
        channel = entry["channel"] or (PERMALINK_CHANNEL.findall(entry["link"]) or [""])[0]
        if channel.startswith("D"):
            where = "in a DM"
        elif title := names.get(channel) or entry["where"]:
            where = f"in #{title.lstrip('#')}"
        else:
            where = "in a channel" if channel else ""
        if entry["thread_ts"] and entry["thread_ts"] != entry["ts"]:
            where += ", in a thread" if where else "in a thread"
        head = f"{who} {where}".strip()
        said = MENTIONED.sub(
            lambda found: (
                f"{'@' if found[1] == '@' else '#'}{name}"
                if (name := names.get(found[2]) or found[3])
                else found[0]
            ),
            entry["said"],
        )
        if "\n" in said:
            # Its lines as a quote under who wrote it: a list or code block reads as written,
            # and a line in it that looks like another message stays inside this one.
            said = "\n" + "\n".join(f"> {line}" if line else ">" for line in said.splitlines())
        else:
            said = f" {said}"
        lines.append(f"{head}:{said}" + (f"\n{entry['link']}" if entry["link"] else ""))
    if lines and forged:
        lines.insert(0, f"⚠ {FORGED_NOTE}")
    return "\n\n".join(lines) or listed


class SlackSource:
    name = "slack"

    def __init__(
        self,
        store: Store,
        host: SourceHost,
        *,
        tokens: TokenStore | None = None,
        mcp: SlackClient | None = None,
    ):
        self.store = store
        self.host = host
        self.tokens: TokenStore = tokens or SlackTokens(store.directory)
        self.mcp: SlackClient = mcp or SlackMcp(self.tokens.get)
        self.read_tools: list[str] = []
        self.can_send = False
        self.fingerprint = ""
        self.changed = asyncio.Event()
        self.job: asyncio.Task | None = None
        self.status = {
            "enabled": False,
            "connected": False,
            "error": "",
            "user_id": "",
            "last_checked": None,
            "can_send": False,
        }

    def configured(self) -> bool:
        return self.store.settings().slack_enabled and bool(self.tokens.get())

    def token_source(self) -> str:
        """Where its token comes from: "env" (that variable wins over a saved one: Remove and
        Save do nothing then), "keychain", or "" (none)."""
        if os.environ.get(SlackTokens.ENV):
            return "env"
        return "keychain" if self.tokens.get() else ""

    def session_mcp_config(self) -> dict | None:
        """MCP config for model sessions: read-only tools only, never send/write tools."""
        token = self.tokens.get()
        if not (self.store.settings().slack_enabled and token):
            return None
        tools = [name for name in (self.read_tools or KNOWN_READ_TOOLS) if name != SEND_TOOL]
        return {
            "type": "http",
            "url": self.mcp.url,
            "headers": {"Authorization": f"Bearer {token}"},
            "tools": tools,
        }

    async def start(self):
        self.job = asyncio.create_task(self.loop())

    async def stop(self):
        if self.job:
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)

    def wake(self):
        self.changed.set()

    async def loop(self):
        while True:
            self.changed.clear()
            try:
                await self.poll_once()
            except Exception as exc:
                message = describe(exc)[:500]
                if self.status["error"] != message:
                    logger.warning("Slack watcher failed: %s", message)
                self.status.update(connected=False, error=message)
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self.changed.wait(), timeout=self.store.settings().slack_poll_seconds
                )

    async def discover(self):
        tools = await self.mcp.list_tools()
        names = {tool.name for tool in tools}
        if SEARCH_TOOL not in names:
            raise SlackError(
                "This Slack token cannot search. Add the search:read.* user scopes to your "
                "Slack app, reinstall it and paste the new token."
            )
        self.read_tools = sorted(
            tool.name
            for tool in tools
            if tool.annotations and tool.annotations.readOnlyHint and tool.name != SEND_TOOL
        ) or sorted(names & set(KNOWN_READ_TOOLS))
        self.can_send = SEND_TOOL in names
        user_id = next(
            (m.group(1) for tool in tools if (m := USER_ID.search(tool.description or ""))), ""
        )
        self.status.update(user_id=user_id, can_send=self.can_send)

    def queries(self) -> list[str]:
        queries = ["to:me"]
        if self.status["user_id"]:
            queries.append(f"<@{self.status['user_id']}>")
        return queries

    async def poll_once(self):
        settings = self.store.settings()
        self.status["enabled"] = settings.slack_enabled
        if not settings.slack_enabled:
            self.status.update(connected=False, error="")
            return
        if settings.paused:
            return
        token = self.tokens.get()
        if not token:
            self.status.update(connected=False, error="Add your Slack user token in Settings.")
            return
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        if fingerprint != self.fingerprint:
            await self.discover()
            self.fingerprint = fingerprint
        started = time.time()
        cursor = self.store.get("slack_cursor")
        if cursor is None:
            # Start from now: enabling Slack must not flood the Router with old history.
            if settings.slack_delegate_emoji:
                await self.poll_delegated(settings.slack_delegate_emoji)
            self.store.set("slack_cursor", str(started))
            self.status.update(connected=True, error="", last_checked=now())
            return
        await self.poll_followed()
        followed = {sub["thread"]: sub["since"] for sub in self.host.followed("slack:")}
        # After a poll that stopped at its page limit, go on exactly from there: looking back
        # again could fill every page with what was read already.
        lookback = 0 if self.store.get("slack_cursor_exact") else LOOKBACK_SECONDS
        since = max(0, int(float(cursor)) - lookback)
        sections: list[str] = []
        shown: list[str] = []
        unseen: list[str] = []
        thread_keys: list[str] = []
        for_followers: dict[str, tuple[list[str], list[tuple[str, str]]]] = {}
        crowded: list[tuple[str, str]] = []  # Notes on matches past a page within one second.
        # How far this poll got: short of now when a query had more pages than it reads.
        reached = started
        for query in self.queries():
            after = since
            for _ in range(MAX_PAGES):
                text = await self.mcp.call(
                    SEARCH_TOOL,
                    {
                        "query": query,
                        "after": str(after),
                        "sort": "timestamp",
                        "sort_dir": "asc",
                        "include_context": False,
                        "limit": PAGE_SIZE,
                    },
                )
                self.learn_names(text)
                found = listing_keys(text)
                links = threads.slack_messages(text)
                fresh = []
                for key in found:
                    keys = links.get(key.removeprefix("slack:"), [])
                    if key in unseen or self.seen(key):
                        continue
                    stamp = float(key.removeprefix("slack:"))
                    hit = next((t for t in keys if t in followed and stamp >= followed[t]), None)
                    if hit:
                        # Belongs to a followed thread that reading it did not return (a DM
                        # thread reply, say): it goes to the followers, not the Router.
                        bucket = for_followers.setdefault(hit, ([], []))
                        if key not in bucket[0]:
                            # Only its own messages: the page has other conversations too.
                            bucket[0].append(key)
                            bucket[1].append((text, key.removeprefix("slack:")))
                        continue
                    fresh.append(key)
                    thread_keys += keys
                if fresh:
                    unseen += fresh
                    stamps = {key.removeprefix("slack:") for key in fresh}
                    # Only the new ones, each with its share: none goes past the Router unread.
                    sections.append(f"Search `{query}`:\n{for_session(text, stamps)}")
                    shown.append(readable(text, self.display_names(), stamps))
                # A full page may hide newer matches: continue after the newest one returned.
                newest = int(max((float(key.removeprefix("slack:")) for key in found), default=0))
                if len(found) < PAGE_SIZE or newest < after:
                    break  # All of it, or nothing past where it asked from.
                if newest == after:
                    # A page of matches within one second: past it, a search by time can only
                    # go on from the next second. Said, so the rest is looked for.
                    logger.warning(
                        "More than %s Slack matches at %s for %s", PAGE_SIZE, after, query
                    )
                    where = sorted({entry["channel"] for entry in parse(text)[0]} - {""})
                    crowded.append(
                        (
                            f"slack-crowded:{query}:{after}",
                            f"More than {PAGE_SIZE} messages for `{query}` came within one "
                            f"second ({after}): the ones past these {PAGE_SIZE} are not listed "
                            "here. Read those conversations to find them"
                            + (f" ({', '.join(where)})." if where else "."),
                        )
                    )
                    newest = after + 1
                after = newest
            else:
                # More than MAX_PAGES pages: the rest comes with the next poll, from here.
                reached = min(reached, after)
        for thread, (keys, listed) in for_followers.items():
            body = "\n\n".join(dict.fromkeys(for_session(text, {stamp}) for text, stamp in listed))
            said = "\n\n".join(
                dict.fromkeys(
                    readable(text, self.display_names(), {stamp}) for text, stamp in listed
                )
            )
            delivered = await self.host.on_thread(
                "slack",
                thread,
                keys,
                f"New messages in a Slack conversation you follow ({thread}):\n\n{body}",
                said,
            )
            if not delivered:  # Nobody follows it any more.
                unseen += [key for key in keys if not self.seen(key)]
                sections.append(body)
                shown.append(said)
        crowded = [(key, note) for key, note in crowded if not self.seen(key)]  # Said once.
        if crowded and not unseen:
            # Nothing else new to carry it (they were handled, or went to followers): said on
            # its own, once, so the ones past that page are looked for.
            await self.host.on_trigger(
                "slack",
                [key for key, _ in crowded],
                "\n".join(note for _, note in crowded),
                "More Slack messages came at once than one search lists; the Router looks for "
                "the rest.",
            )
        sections += [note for _, note in crowded]
        if unseen:
            keys_line = (
                f"Thread keys: {', '.join(dict.fromkeys(thread_keys))}\n" if thread_keys else ""
            )
            await self.host.on_trigger(
                "slack",
                unseen,
                f"{len(unseen)} new Slack message(s) to or mentioning the owner "
                f"(new ts: {', '.join(key.removeprefix('slack:') for key in unseen)}). "
                "Each one's text is quoted under its Slack line.\n"
                + keys_line
                + "\n"
                + "\n\n".join(sections),
                "\n\n".join(dict.fromkeys(shown)),
            )
            for key, note in crowded:  # Said with them: not again on its own next time.
                self.store.add_trigger("slack", key, note)
        if settings.slack_delegate_emoji:
            await self.poll_delegated(settings.slack_delegate_emoji)
        else:
            self.store.delete("slack_delegate_emoji")  # Turning it back on starts from then.
        # Only advance after the Router accepted the events, so a failure is retried.
        self.store.set("slack_cursor", str(reached))
        if reached < started:
            self.store.set("slack_cursor_exact", "1")
        else:
            self.store.delete("slack_cursor_exact")
        self.status.update(connected=True, error="", last_checked=now())

    def seen(self, key: str) -> bool:
        return bool(self.store.one("SELECT id FROM triggers WHERE key=?", (key,)))

    async def poll_delegated(self, emoji: str):
        """Messages the owner reacted to with the delegate emoji go to the Router as requests.

        Reactions are not dated in search, so this looks at the latest messages with the owner's
        reaction (by message time) and hands over the ones not handed over before.
        """
        text = await self.mcp.call(
            SEARCH_TOOL,
            {
                "query": f"hasmy::{emoji}:",
                "sort": "timestamp",
                "sort_dir": "desc",
                "include_context": False,
                "limit": PAGE_SIZE,
            },
        )
        self.learn_names(text)
        stamps = [key.removeprefix("slack:") for key in listing_keys(text)]
        keys = [f"{DELEGATED}{stamp}" for stamp in stamps]
        if self.store.get("slack_delegate_emoji") != emoji:
            # Reactions from before the feature was on (or with another emoji) are history.
            for key in keys:
                self.store.add_trigger("slack", key, "")
            self.store.set("slack_delegate_emoji", emoji)
            return
        fresh = [stamp for stamp in stamps if not self.seen(f"{DELEGATED}{stamp}")]
        if not fresh:
            return
        links = threads.slack_messages(text)
        thread_keys = list(dict.fromkeys(key for stamp in fresh for key in links.get(stamp, [])))
        keys_line = f"Thread keys: {', '.join(thread_keys)}\n" if thread_keys else ""
        await self.host.on_trigger(
            "slack",
            [f"{DELEGATED}{stamp}" for stamp in fresh],
            f"The owner reacted with :{emoji}: to {len(fresh)} Slack message(s), handing them to "
            f"you (new ts: {', '.join(fresh)}). Each one's text is quoted under its Slack line.\n"
            + keys_line
            + "\n"
            + for_session(text, set(fresh)),
            readable(text, self.display_names(), set(fresh)),
            kind="delegated",
        )

    async def poll_followed(self):
        """Read every followed thread and DM; new messages go straight to their followers."""
        for sub in self.host.followed("slack:"):
            thread = sub["thread"]
            # Read from the newest message already delivered, so long threads stay readable.
            oldest = max(int(sub["since"]), int(float(self.store.get(f"slackhw:{thread}") or 0)))
            if match := threads.SLACK_THREAD.fullmatch(thread):
                channel, root = match.groups()
                tool, arguments = "slack_read_thread", {"channel_id": channel, "message_ts": root}
                kind = "thread"
            else:
                channel = thread.removeprefix("slack:")
                tool, arguments = "slack_read_channel", {"channel_id": channel}
                kind = "DM"
            # Failures in a row count real reads only; after a few, it is read only every
            # tenth poll (quiet follows expire anyway).
            failures = int(self.store.get(f"slackfail:{thread}") or 0)
            if failures >= MAX_FOLLOW_FAILURES:
                skipped = int(self.store.get(f"slackskip:{thread}") or 0) + 1
                if skipped < 10:
                    self.store.set(f"slackskip:{thread}", str(skipped))
                    continue
                self.store.set(f"slackskip:{thread}", "0")
            try:
                text = await self.mcp.call(tool, {**arguments, "oldest": str(oldest), "limit": 50})
            except Exception as exc:
                # One unreadable thread must not stop the others or the DM/mention search.
                self.store.set(f"slackfail:{thread}", str(failures + 1))
                logger.warning("Could not read followed %s (%s): %s", thread, failures + 1, exc)
                continue
            self.store.set(f"slackfail:{thread}", "0")
            self.learn_names(text)
            fresh = [
                key
                for key in listing_keys(text)
                if float(key.removeprefix("slack:")) >= oldest and not self.seen(key)
            ]
            if fresh:
                await self.learn_mentions(text)
                newest = max(float(key.removeprefix("slack:")) for key in fresh)
                stamps = {key.removeprefix("slack:") for key in fresh}
                await self.host.on_thread(
                    "slack",
                    thread,
                    fresh,
                    f"New messages in a Slack {kind} you follow ({thread}; new ts: "
                    f"{', '.join(sorted(stamps))}). Each one's text is quoted under its Slack "
                    f"line; read the {kind} for what came before.\n\n" + for_session(text, stamps),
                    readable(text, self.display_names(), stamps),
                )
                self.store.set(f"slackhw:{thread}", str(newest))

    async def find_sent(self, target: dict, text: str, since: float) -> str:
        """A permalink to the owner's message with this text in the target conversation, sent
        after `since` (when the draft was written), or "". An older "Thanks!" is not this one."""
        words = " ".join(text.replace('"', " ").split()[:12])
        if not words:
            return ""
        result = await self.mcp.call(
            SEARCH_TOOL,
            {
                "query": f'"{words}" from:me',
                "after": str(int(since) - 86400),
                "sort": "timestamp",
                "sort_dir": "desc",
                "include_context": False,
                "limit": 20,
            },
        )
        for stamp, keys in threads.slack_messages(result).items():
            if float(stamp) < since - CLOCK_SKEW:
                continue
            if any(key.split(":")[1] == target["channel_id"] for key in keys):
                return next(
                    (
                        link
                        for link in re.findall(r"https://\S+/archives/\S+", result)
                        if stamp.replace(".", "") in link
                    ),
                    f"slack:{stamp}",
                )
        return ""

    async def read_conversation(self, channel_id: str, thread_ts: str = "") -> list[dict]:
        """The messages where a reply would go: a thread's (oldest first), else the newest of
        the conversation. Raises when Slack shows no message there ("Thread not found.")."""
        if thread_ts:
            tool, arguments = (
                "slack_read_thread",
                {"channel_id": channel_id, "message_ts": thread_ts},
            )
        else:
            tool, arguments = "slack_read_channel", {"channel_id": channel_id, "limit": 1}
        text = await asyncio.wait_for(self.mcp.call(tool, arguments), 20)
        self.learn_names(text)
        if found := messages(text):
            # Who its messages mention, by name on the card that quotes them.
            await self.learn_mentions(" ".join(entry["text"] for entry in found))
            return found
        said = " ".join((text or "").split())[:200]
        raise SlackError(f"Slack showed no message there ({said or 'nothing'}).")

    async def latest_message(self, channel_id: str, thread_ts: str = "") -> dict:
        """The newest message where a reply would go, as Slack shows it: what the owner checks
        a draft's destination by, rather than by what the drafting session says. {"quote":
        "Who: text", "ts", "user"}; {"error": why} when no message could be read there."""
        try:
            found = await self.read_conversation(channel_id, thread_ts)
        except Exception as exc:
            logger.info("Could not read %s for a draft: %s", channel_id, describe(exc))
            return {"error": describe(exc)[:300]}
        return self.described(newest_entry(found, last=bool(thread_ts)))

    def described(self, entry: dict | None) -> dict:
        if not entry:
            return {"error": "Slack showed no message there."}
        name = entry["name"] or self.display_names().get(entry["user"], "")
        return {
            "quote": quote({**entry, "name": name}),
            "ts": entry["ts"],
            "user": entry["user"],
            "name": name,
            "text": entry["text"],
        }

    async def latest(self, channel_id: str, thread_ts: str = "") -> str:
        """The newest message's quote ("" when none could be read)."""
        return (await self.latest_message(channel_id, thread_ts)).get("quote", "")

    async def reply_place(self, channel_id: str, thread_ts: str, in_thread: bool = False) -> dict:
        """Where a drafted reply goes ({"thread_ts"}) and the newest message there (as
        latest_message). A reply to a DM message that is in no thread goes into the DM, not
        into a new thread under it, unless `in_thread`; a reply to a message inside a thread
        goes into that thread (under its first message, as Slack threads)."""
        if not thread_ts:
            return {"thread_ts": ""} | await self.latest_message(channel_id)
        try:
            found = await self.read_conversation(channel_id, thread_ts)
        except Exception as exc:
            logger.info("Could not read %s for a draft: %s", channel_id, describe(exc))
            return {"thread_ts": thread_ts, "error": describe(exc)[:300]}
        here = next((entry for entry in found if entry["ts"] == thread_ts), None)
        root = here["thread_ts"] if here else ""
        if root and root != thread_ts:
            return {"thread_ts": root} | await self.latest_message(channel_id, root)
        alone = here is not None and all(entry["ts"] == thread_ts for entry in found)
        if channel_id.startswith("D") and alone and not in_thread:
            return {"thread_ts": ""} | await self.latest_message(channel_id)
        return {"thread_ts": thread_ts} | self.described(newest_entry(found, last=True))

    # Names: who a user id is and what a conversation id is called, as Slack listed them.

    def learn_names(self, listed: str):
        """Names from Slack's own lines on who wrote a message where: not from its text, where
        anyone can write "<@U0BOB001> (Alice Chen)"; none from a listing whose text was made to
        look like those lines."""
        entries, forged = parse(listed)
        if not forged:
            self.remember_names(
                names_in("\n".join(entry["head"] for entry in entries if entry["head"]))
            )

    def remember_names(self, found: dict[str, str]):
        known = self.display_names()
        if all(known.get(identifier) == name for identifier, name in found.items()):
            return
        known.update(found)
        kept = dict(list(known.items())[-MAX_NAMES:])
        self.store.set("slacknames", json.dumps(kept, ensure_ascii=False))

    async def learn_mentions(self, text: str, limit: int = 3):
        """Names for the people a text mentions ("<@U0ALICE1>") that no listing named yet (only
        senders are named there): from their Slack profile, asked at most once a day each."""
        if PROFILE_TOOL not in self.read_tools:
            return
        known = self.display_names()
        wanted = [user for user in dict.fromkeys(MENTION.findall(text or "")) if user not in known]
        for user in wanted[:limit]:
            if time.time() - float(self.store.get(f"slackwho:{user}") or 0) < 86400:
                continue
            self.store.set(f"slackwho:{user}", str(time.time()))
            try:
                profile = await asyncio.wait_for(self.mcp.call(PROFILE_TOOL, {"user_id": user}), 10)
            except Exception as exc:
                logger.info("Could not read the Slack profile of %s: %s", user, describe(exc))
                continue
            named = names_in(profile).get(user) or profile_name(profile)
            if named:
                self.remember_names({user: named})

    def display_names(self) -> dict[str, str]:
        """Slack ids -> names, as read in what Slack listed: users (U…, W…) and conversations
        (C…, G…, D…)."""
        try:
            known = json.loads(self.store.get("slacknames") or "{}")
        except ValueError:
            return {}
        return known if isinstance(known, dict) else {}

    async def send(self, target: dict, text: str) -> str:
        if self.fingerprint and not self.can_send:
            raise SlackError("This Slack token lacks chat:write; nothing was sent.")
        arguments = {"channel_id": target["channel_id"], "message": text}
        if target.get("thread_ts"):
            arguments["thread_ts"] = target["thread_ts"]
        return await self.mcp.call(SEND_TOOL, arguments)
