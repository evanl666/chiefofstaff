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
REACTIONS_TOOL = "slack_get_reactions"
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
# Also as a JSON answer escapes them ("\/archives\/C0DEPLOYS\/p…").
PERMALINK = re.compile(r"\\?/archives\\?/[A-Z0-9]+\\?/p(\d{10})(\d{6})")
PERMALINK_CHANNEL = re.compile(r"\\?/archives\\?/([A-Z0-9]+)\\?/p\d")
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
    text = THREAD_TS.sub("", plain(text))
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
    # Within its line: an empty "Display Name: " is not followed by the next line's text.
    r"^[ \t*\-]*(real[ _]name|full[ _]name|display[ _]name|name)\W{0,3}[:=][ \t]*"
    r"\"?([^\"\n]{1,80})",
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
    if inner := unwrapped(profile):
        profile = inner[0]
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


# Since November 2026 Slack's MCP server answers in JSON: a listing in one field ("messages": a
# thread or a channel's history, "results": a search, "result": a profile or reactions) and
# what else it says in others ("pagination_info"). Before, it answered in lines.
LISTING_FIELDS = ("messages", "results", "result")
EMAIL = re.compile(r" ?<[^<>\s@|]+@[^<>\s|]+>")


def unwrapped(listed: str) -> tuple[str, str] | None:
    """The listing in what Slack's MCP server answered as JSON, and the field it was in; None
    when the answer is not such JSON (Slack's earlier format, an error)."""
    text = (listed or "").strip()
    if not text.startswith("{"):
        return None
    try:
        found = json.loads(text)
    except ValueError:
        return None
    if isinstance(found, dict):
        for field in LISTING_FIELDS:
            if isinstance(found.get(field), str):
                return found[field], field
    return None


def plain(listed: str) -> str:
    """What Slack answered, as text to look for links and ids in: a JSON answer's values one
    per line (its links unescaped: "https:\\/\\/…" is "https://…"), else as it is."""
    text = (listed or "").strip()
    if not text.startswith(("{", "[")):
        return listed or ""
    try:
        found = json.loads(text)
    except ValueError:
        return listed

    def lines(value, key: str = "") -> list[str]:
        if isinstance(value, dict):
            return [line for name, item in value.items() for line in lines(item, str(name))]
        if isinstance(value, list):
            return [line for item in value for line in lines(item, key)]
        if key in LISTING_FIELDS:
            return [str(value)]
        return [f"{key}: {value}" if key else str(value)]

    return "\n".join(lines(found))


def scrubbed(text: str) -> str:
    """Slack's text without the email addresses its lines give people ("Bob <bob@acme.com>")."""
    return EMAIL.sub("", text or "")


PERSON = r"(?P<name>[^\n]*?)(?: <(?P<email>[^<>\n]*)>)? \((?:ID: )?(?P<user>[UWB][A-Z0-9]{2,})\)"
FROM_LINE = re.compile(rf"From: {PERSON} ?")
TIME_LINE = re.compile(r"Time: \S[^\n]*")
TS_LINE = re.compile(r"Message[ _]TS: (?P<ts>\d{10}\.\d{6}) ?", re.IGNORECASE)
PARENT_LINE = re.compile(r"=== THREAD PARENT MESSAGE ===")
REPLIES_LINE = re.compile(r"=== THREAD REPLIES \((?P<total>\d+)\b[^()\n]*\) ===")
REPLY_LINE = re.compile(r"--- Reply (?P<number>\d+) of (?P<total>\d+) ---")
NO_REPLIES = re.compile(r"No thread mes+ages?\.?", re.IGNORECASE)
CHANNEL_LINE = re.compile(
    r"Channel: (?P<where>[^\n]*?) \((?:ID: )?(?P<channel>[CDG][A-Z0-9]{2,})\) ?"
)
CHANNEL_MESSAGE = re.compile(rf"=== Message from {PERSON} at (?P<time>[^\n]+?) === ?")
SEARCH_LINE = re.compile(r"# Search Results for:[^\n]*")
SEARCH_MESSAGES = re.compile(r"## Messages \((?P<total>\d+) results?\)")
SEARCH_SECTION = re.compile(r"## \w[\w ]* \(\d+ results?\)")
RESULT_LINE = re.compile(r"### Result (?P<number>\d+) of (?P<total>\d+)")
PARTICIPANTS_LINE = re.compile(r"Participants: (?P<people>[^\n]*)")
PARTICIPANT = re.compile(r"([^,()\n][^()\n]*?) \((?:ID: )?([UWB][A-Z0-9]{2,})\)")
REPLY_COUNT_LINE = re.compile(r"Reply count: \d+")
PERMALINK_FIELD = re.compile(r"Permalink: (?:\[[^\]\n]*\]\((?P<url>[^)\s]+)\)|(?P<bare>\S+))")
TEXT_LINE = re.compile(r"Text: ?(?P<first>[^\n]*)")
REACTIONS_LINE = re.compile(r"Reactions: (?P<reactions>[^\n]*)")
THREAD_INFO_LINE = re.compile(r"Thread: \d+ repl(?:y|ies)\b[^\n]*")
REACTION = re.compile(r"([\w+\-']+(?:::[\w\-]+)?) \((\d+)\)")
# A line in a message's text written to look like one of Slack's lines around a message.
SECTION_LOOKALIKE = re.compile(
    r"^\s*(?:=== THREAD |=== Message from |--- Reply \d|### Result \d|## Messages \(|"
    r"Message[ _]TS: |From: [^\n]*\((?:ID: )?[UWB][A-Z0-9]{2,}\))",
    re.MULTILINE | re.IGNORECASE,
)


def header_at(lines: list[str], start: int, *patterns: re.Pattern) -> list[re.Match] | None:
    """These of Slack's lines, in this order, from line `start` on; None unless all match."""
    found = []
    for offset, pattern in enumerate(patterns):
        line = lines[start + offset] if start + offset < len(lines) else None
        if line is None or not (match := pattern.fullmatch(line)):
            return None
        found.append(match)
    return found


def body_of(lines: list[str], start: int, stop: int) -> tuple[str, list[str], bool]:
    """A message's text between its header and the next one's (or the end), without the blank
    lines around it; its reactions ("robot_face"), and whether Slack says it starts a thread.
    Slack writes "Reactions: …" (and in a channel "Thread: 2 replies …") after the text."""
    kept = lines[start:stop]
    while kept and not kept[-1].strip():
        kept.pop()
    reactions: list[str] = []
    replied = False
    while kept and (
        (found := REACTIONS_LINE.fullmatch(kept[-1])) or THREAD_INFO_LINE.fullmatch(kept[-1])
    ):
        if found:
            reactions = [name for name, _count in REACTION.findall(found["reactions"])]
        else:
            replied = True
        kept.pop()
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept), reactions, replied


def section_entry(
    person: re.Match,
    stamp: str,
    text: str,
    *,
    channel: str = "",
    where: str = "",
    thread_ts: str = "",
    link: str = "",
    reactions: list[str] | None = None,
    people: list[tuple[str, str]] | None = None,
) -> dict:
    """One message of Slack's JSON answers, in the fields `parse` gives (`head`: Slack's
    line on who wrote it where, as its earlier format wrote it; never an email address)."""
    name = " ".join((person["name"] or "").split())
    where = where.strip().lstrip("#")
    where = "" if where.upper() in {"DM", "GROUP DM"} else where
    shown = f"<#{channel}>" + (f" ({where})" if where else "") if channel else ""
    head = " | ".join(
        part
        for part in (
            f"Channel: {shown}" if shown else "",
            f"From: <@{person['user']}>" + (f" ({name})" if name else ""),
            f"ts: {stamp}",
            f"thread_ts: {thread_ts}" if thread_ts else "",
        )
        if part
    )
    return {
        "channel": channel,
        "where": where,
        "user": person["user"],
        "name": name,
        "ts": stamp,
        "thread_ts": thread_ts,
        "text": entities(" ".join(text.split())),
        "said": entities("\n".join(line.rstrip() for line in text.splitlines())),
        "link": link,
        "head": head,
        "raw": text,
        "reactions": reactions or [],
        "people": people or [],
        "sections": True,  # Read from Slack's JSON answers.
    }


def numbered(numbers: dict[int, int]) -> dict[int, int]:
    """Where each numbered header block of Slack's is ("--- Reply 2 of 3 ---" and the lines
    after it; line -> its number): number -> line, for the numbers only one block claims."""
    places: dict[int, list[int]] = {}
    for index, number in numbers.items():
        places.setdefault(number, []).append(index)
    return {number: where[0] for number, where in places.items() if len(where) == 1}


# Which lines of a JSON answer are Slack's own, and which a message's text, when anyone can
# write lines like Slack's in a message ("Message TS: …", "=== THREAD PARENT MESSAGE ===", a
# whole "--- Reply 2 of 3 ---" block)? Messages are told apart by Slack's section structure
# only, never by a line alone:
# - the listing is the JSON field's string, so nothing in a message reaches past it (the
#   pagination note is another field);
# - a message starts only at Slack's full header block (its separator line, then From, Time
#   and Message TS lines, in that order, after a blank line), and only the block expected next:
#   the thread's first message at the very top, replies and search results by their numbers
#   ("Reply k of N": k counts up from 1, N is the total Slack gave first), a channel's
#   messages newest first (each block an older ts than the one before). Past the last number,
#   nothing starts a message: what follows is the last message's text;
# - a number (or in a channel a ts) that two full blocks claim is nobody's: one of them was
#   written in a message's text, and which one cannot be told, so neither starts a message
#   (the text stays with the message before them). So in a thread or a search, text never
#   makes a message by someone else: a forged block either is not the one expected next, or
#   claims a number Slack's own block claims too; at worst what one person wrote after it
#   stays in their message. A channel's history has no numbers: there a whole block with an
#   older ts than the message it is written in, and a ts of its own, cannot be told from
#   Slack's (it can only sit below the newest message, so the newest is always Slack's).
# - a thread Slack closes with "No thread messsages" has no replies: blocks in its first
#   message's text are its text, whatever they claim (nothing Slack wrote can compete there).
# Any full block left in a message's text is that message's sender's text: shown as theirs,
# said to look like other messages, never a message of whoever it names. When a real message
# may be in another's text (a number two blocks claim, fewer messages than Slack counts), who
# wrote what is unsure: callers say so plainly (the third value; `parse`'s second).
def thread_listing(lines: list[str], channel: str) -> tuple[list[dict], bool, bool] | None:
    top = header_at(lines, 0, PARENT_LINE, FROM_LINE, TIME_LINE, TS_LINE)
    if not top:
        return None
    _, person, _, stamp = top
    root = stamp["ts"]

    def reply_block(index: int) -> list[re.Match] | None:
        if index < 1 or lines[index - 1].strip():
            return None
        return header_at(lines, index, REPLY_LINE, FROM_LINE, TIME_LINE, TS_LINE)

    headers = {index: found for index in range(len(lines)) if (found := reply_block(index))}
    numbers = {index: int(found[0]["number"]) for index, found in headers.items()}
    unique = numbered(numbers)
    claimed_twice = len(numbers) != len(set(numbers.values()))
    closing = [line for line in lines if line.strip()][-1:]
    alone = bool(closing) and NO_REPLIES.fullmatch(closing[0].strip()) is not None
    heading = (
        None
        if alone
        else next(
            (
                (index, int(found["total"]))
                for index, line in enumerate(lines)
                if index > 4
                and not lines[index - 1].strip()
                and (found := REPLIES_LINE.fullmatch(line))
                and (first := unique.get(1)) is not None
                and first > index
                and not any(item.strip() for item in lines[index + 1 : first])
                and int(headers[first][0]["total"]) == int(found["total"])
            ),
            None,
        )
    )
    accepted: list[int] = []  # Where each reply's header block is.
    if heading:
        index, total = heading
        previous, place_before = float(root), index
        for number in range(1, total + 1):
            place = unique.get(number)
            header = headers.get(place) if place is not None else None
            if (
                place is None
                or header is None
                or place <= place_before
                or int(header[0]["total"]) != total
                or float(header[3]["ts"]) <= previous
            ):
                break  # Not the one expected next: what follows is the message before's text.
            accepted.append(place)
            previous, place_before = float(header[3]["ts"]), place
    # The first message runs to the replies' heading, else to the end, where "No thread
    # messsages" closes the listing, not its text.
    tail = lines[4 : heading[0]] if heading and accepted else lines[4:]
    if not accepted:
        while tail and not tail[-1].strip():
            tail.pop()
        if len(tail) >= 2 and NO_REPLIES.fullmatch(tail[-1].strip()) and not tail[-2].strip():
            tail = tail[:-1]
    text, reactions, _ = body_of(tail, 0, len(tail))
    found = [
        section_entry(
            person,
            root,
            text,
            channel=channel,
            thread_ts=root if accepted else "",
            reactions=reactions,
        )
    ]
    for position, place in enumerate(accepted):
        header = headers[place]
        stop = accepted[position + 1] if position + 1 < len(accepted) else len(lines)
        text, reactions, _ = body_of(lines, place + 4, stop)
        found.append(
            section_entry(
                header[1],
                header[3]["ts"],
                text,
                channel=channel,
                thread_ts=root,
                reactions=reactions,
            )
        )
    parents = [
        index
        for index in range(1, len(lines))
        if header_at(lines, index, PARENT_LINE, FROM_LINE, TIME_LINE, TS_LINE)
    ]
    forged = bool(set(headers) - set(accepted)) or bool(parents)
    # A real reply may be in another message's text: blocks Slack's numbers cannot tell apart.
    unsure = bool(set(headers) - set(accepted)) and (
        alone or claimed_twice or not heading or len(accepted) < heading[1]
    )
    return found, forged, unsure


def channel_listing(lines: list[str]) -> tuple[list[dict], bool, bool] | None:
    top = CHANNEL_LINE.fullmatch(lines[0]) if lines else None
    if not top:
        return None
    channel, where = top["channel"], top["where"]

    def block(index: int) -> list[re.Match] | None:
        if index < 1 or lines[index - 1].strip():
            return None
        return header_at(lines, index, CHANNEL_MESSAGE, TS_LINE)

    headers = {index: found for index in range(1, len(lines)) if (found := block(index))}
    claimed: dict[str, int] = {}
    for header in headers.values():
        claimed[header[1]["ts"]] = claimed.get(header[1]["ts"], 0) + 1
    accepted: list[int] = []
    newer = float("inf")
    first = next((index for index, line in enumerate(lines) if index and line.strip()), None)
    for index, header in headers.items():
        stamp = float(header[1]["ts"])
        if not accepted and index != first:
            break  # The first message comes right after the channel's line.
        if claimed[header[1]["ts"]] > 1 or stamp >= newer:
            continue  # Not one of Slack's: the text of the message it is in.
        accepted.append(index)
        newer = stamp
    found = []
    for position, place in enumerate(accepted):
        header = headers[place]
        stop = accepted[position + 1] if position + 1 < len(accepted) else len(lines)
        text, reactions, replied = body_of(lines, place + 2, stop)
        stamp = header[1]["ts"]
        found.append(
            section_entry(
                header[0],
                stamp,
                text,
                channel=channel,
                where=where,
                thread_ts=stamp if replied else "",
                reactions=reactions,
            )
        )
    forged = bool(set(headers) - set(accepted))
    # Unnumbered: a block left out may be Slack's own, left in another message's text.
    return found, forged, forged


def search_listing(lines: list[str]) -> tuple[list[dict], bool, bool] | None:
    if not lines or not SEARCH_LINE.fullmatch(lines[0]):
        return None
    heading = next(
        (
            (index, int(found["total"]))
            for index, line in enumerate(lines[:6])
            if (found := SEARCH_MESSAGES.fullmatch(line))
        ),
        None,
    )
    if not heading:
        return [], False, False  # "No results found."

    def fields(index: int) -> dict | None:
        """A result's header block from its "### Result k of N" line: its matches by field,
        and the line its text starts on."""
        numbered_as = RESULT_LINE.fullmatch(lines[index])
        if index + 1 >= len(lines) or not numbered_as:
            return None
        before = lines[index - 1] if index else ""
        if not (
            SEARCH_MESSAGES.fullmatch(before)
            or (not before.strip() and index >= 3 and lines[index - 2].strip() == "---")
        ):
            return None
        place = index + 1
        got: dict = {"number": int(numbered_as["number"]), "total": int(numbered_as["total"])}
        order = [
            ("channel", CHANNEL_LINE, True),
            ("people", PARTICIPANTS_LINE, False),
            ("from", FROM_LINE, True),
            ("time", TIME_LINE, True),
            ("ts", TS_LINE, True),
            ("count", REPLY_COUNT_LINE, False),
            ("link", PERMALINK_FIELD, False),
            ("text", TEXT_LINE, True),
        ]
        for name, pattern, required in order:
            match = pattern.fullmatch(lines[place]) if place < len(lines) else None
            if match:
                got[name] = match
                place += 1
            elif required:
                return None
        got["start"] = place
        return got

    headers = {index: got for index in range(len(lines)) if (got := fields(index))}
    unique = numbered({index: int(got["number"]) for index, got in headers.items()})
    start, total = heading
    accepted: list[int] = []
    previous = start
    for number in range(1, total + 1):
        place = unique.get(number)
        if place is None or place <= previous or headers[place]["total"] != total:
            break
        accepted.append(place)
        previous = place
    found = []
    for position, place in enumerate(accepted):
        got = headers[place]
        last = position + 1 == len(accepted)
        stop = accepted[position + 1] if not last else len(lines)
        body = lines[got["start"] : stop]
        if last:
            # After the last result: other sections ("## Files (2 results)") are not its text.
            cut = next(
                (
                    index
                    for index, line in enumerate(body)
                    if SEARCH_SECTION.fullmatch(line) and index and not body[index - 1].strip()
                ),
                None,
            )
            body = body[:cut] if cut is not None else body
        while body and not body[-1].strip():
            body.pop()
        if body and body[-1].strip() == "---":
            body.pop()  # The line Slack ends each result with.
        first = got["text"]["first"].strip()
        body = ([first] if first else []) + body
        text, reactions, _ = body_of(body, 0, len(body))
        link = (got["link"]["url"] or got["link"]["bare"]) if "link" in got else ""
        root = threads.PERMALINK.search(link or "")
        stamp = got["ts"]["ts"]
        thread_ts = root.group(4) if root and root.group(4) else ""
        if not thread_ts and "count" in got:
            thread_ts = stamp  # A thread's first message.
        people = PARTICIPANT.findall(got["people"]["people"]) if "people" in got else []
        found.append(
            section_entry(
                got["from"],
                stamp,
                text,
                channel=got["channel"]["channel"],
                where=got["channel"]["where"],
                thread_ts=thread_ts,
                link=link,
                reactions=reactions,
                people=[(user, " ".join(name.split())) for name, user in people],
            )
        )
    numbers = [got["number"] for got in headers.values()]
    unsure = len(numbers) != len(set(numbers)) or len(accepted) < total
    return found, bool(set(headers) - set(accepted)), unsure


def sectioned(listed: str, channel: str = "") -> tuple[list[dict], bool, bool] | None:
    """The messages of one of Slack's JSON answers (a thread, a channel's history, a search),
    whether text in them was made to look like Slack's own lines, and whether a real message
    may be in another's text (who wrote what is unsure); None when it is not one.

    Slack's lines around a message in its JSON answers. A thread:
      === THREAD PARENT MESSAGE ===
      From: Bob Li <bob@acme.com> (U0BOB001)
      Time: 2026-11-21 09:16:40 PST
      Message TS: 1795201000.100100
      <text>
      Reactions: robot_face (1)

      === THREAD REPLIES (2 total) ===            (or: "No thread messsages", Slack's spelling)

      --- Reply 1 of 2 ---
      From: … / Time: … / Message TS: … / <text>
    A channel's history (newest first):
      Channel: #deploys (C0DEPLOYS)

      === Message from Bob Li <bob@acme.com> (U0BOB001) at 2026-11-21 09:16:40 PST ===
      Message TS: 1795201000.100100
      <text>
      Thread: 2 replies (latest: 2026-11-21 09:33:20 PST)
      Reactions: robot_face (1)
    A search:
      # Search Results for: <query>

      ## Messages (2 results)
      ### Result 1 of 2
      Channel: DM (ID: D0BOB0001)                  (or "#deploys (ID: C0DEPLOYS)")
      Participants: Bob Li (ID: U0BOB001), Owner (ID: U0OWNER1)
      From: Bob Li <bob@acme.com> (ID: U0BOB001)
      Time: 2026-11-20 18:03:20 PST
      Message_ts: 1795199000.400400
      Reply count: 1                                (a thread's first message)
      Permalink: [link](https://acme.slack.com/archives/D0BOB0001/p1795199000400400)
      Text:
      <text>

      ---

      ### Result 2 of 2 …
    """
    found = unwrapped(listed)
    if not found:
        return None
    lines = found[0].replace("\r\n", "\n").split("\n")
    return thread_listing(lines, channel) or channel_listing(lines) or search_listing(lines)


PERMALINK_LINE = re.compile(r"^Permalink: (\S+)", re.MULTILINE)
# A line in a message's text made to look like Slack's own lines around a message.
LOOKALIKE = re.compile(r"^\s*(?:Channel: <#|From: <@|Text: |Permalink: )|\| From: <@", re.MULTILINE)
LOOKALIKES = (LOOKALIKE, SECTION_LOOKALIKE)
UNREADABLE = "New in Slack, in a form Chief of Staff could not read here: open Slack to see it."
FORGED_NOTE = (
    "Some message text here is made to look like Slack's own lines (another sender, place or "
    "permalink), so who wrote what below may not be right."
)


def parse(listed: str, channel: str = "") -> tuple[list[dict], bool]:
    """The messages of what Slack listed (a thread, a channel's history, a search), in the
    listing's order (channel, where (its name), user, name, ts, thread_ts, text: on one line,
    said: as written, link, head: Slack's own line(s) before the text, raw: the text as
    listed, reactions: their names, people: a DM's participants as (id, name)), and whether
    text in it was made to look like Slack's own lines in a way that can mislead (a permalink
    line that ends no message, a message closed by another's permalink, two messages with one
    ts; in Slack's JSON answers, a header block of Slack's left in a message's text).

    Slack's JSON answers are read by their sections (see thread_listing); `channel`: where a
    thread read there is (Slack's thread listing does not say). In its earlier line format,
    when the listing gives a message its permalink, the message goes on to it: a "From: …" or
    "Channel: …" line before that is its text (anyone can write one), not another message."""
    if (sections := sectioned(listed, channel)) is not None:
        return sections[0], sections[2]
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
            "reactions": [],
            "people": [],
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


def messages(listed: str, channel: str = "") -> list[dict]:
    """The messages of what Slack listed, in the listing's order (see `parse`)."""
    return parse(listed, channel)[0]


def known(listed: str) -> bool:
    """Whether Slack's answer is a listing in a form read here (Slack's JSON answers by their
    sections; its earlier lines when they give a message), even one with no message in it."""
    return sectioned(listed) is not None or bool(parse(listed)[0])


def listing_keys(listed: str) -> list[str]:
    """Keys ("slack:<ts>") of the messages Slack listed: the messages read in it when each has
    a ts (a ts in a message's text is not a message), else every ts in it (in a form not read
    here; never in Slack's JSON answers, which are read by their sections)."""
    if (sections := sectioned(listed)) is not None:
        # Slack's own messages only: a ts written in a message's text is that message's text
        # (it reaches its readers there, as its sender's), never a message of its own.
        return [f"slack:{stamp}" for stamp in dict.fromkeys(e["ts"] for e in sections[0])]
    found = parse(listed)[0]
    if found and all(entry["ts"] for entry in found):
        # Also when who wrote which is unsure: a message's header with its own permalink is
        # always read as one.
        return [f"slack:{stamp}" for stamp in dict.fromkeys(entry["ts"] for entry in found)]
    return message_keys(listed)


def for_session(
    listed: str, stamps: set[str], limit: int = MAX_RESULT_CHARS, channel: str = ""
) -> str:
    """Only these messages ("1712345678.000100") of what Slack listed, for a session to read:
    Slack's line on who wrote it where (with ids and ts), its permalink, and its text quoted
    ("> ", so a line in it cannot pass for Slack's) and shortened to its share of `limit` (a
    cut says how much is left). Its lines that look like another message stay its sender's,
    said so; when who wrote what is unsure, that is said first. Slack's JSON answers are never
    given as they are (a line written in a message there reads as Slack's own): what is not
    read here is said to be so. Slack's earlier line format, when it does not show each of
    these messages in a form known here: what Slack listed (cut at `limit`, saying so; only the
    entries ending in their permalinks when it has those)."""
    found, forged = parse(listed, channel)
    kept = [entry for entry in found if entry["ts"] in stamps]
    missing = stamps - {entry["ts"] for entry in kept}
    if unwrapped(listed) is not None and (missing or not kept):
        unread = (
            f"({len(missing)} of these Slack messages ({', '.join(sorted(missing))}) came in a "
            "form Chief of Staff cannot read: nothing of them is quoted here. Have them read with "
            "Slack's tools to see who wrote what.)"
        )
        if not kept:
            return unread
        return f"{for_session(listed, stamps - missing, limit, channel)}\n\n{unread}"
    if not kept or missing:
        # Its entries by their permalinks, at least: a page has other conversations too (in
        # Slack's earlier format, where each ends with its permalink). Never the email addresses
        # Slack gives people.
        text = plain(listed)
        if unwrapped(listed) is None:
            text = threads.slack_entries(text, stamps) or text
        listed = scrubbed(text)
        cut = len(listed) - limit
        note = f"{FORGED_NOTE}\n\n" if forged else ""
        return note + listed[:limit] + (f"\n(cut here: {cut} more characters)" if cut > 0 else "")
    share = max(600, limit // len(kept))
    blocks = [FORGED_NOTE] if forged else []
    for entry in kept:
        # An address written as Slack writes one beside a name ("Bob <bob@acme.com>") is not
        # shown, in a message's text either: only a made-up line of Slack's has one so.
        text = scrubbed(entry["said"])
        if len(text) > share:
            left = len(text) - share
            text = f"{text[:share].rstrip()} … ({left} more characters: read the message for all)"
        quoted = "\n".join(f"> {line}" for line in text.splitlines() or [""])
        who = entry["name"] or f"<@{entry['user']}>"
        lookalike = (
            f"\n(Lines in its text are made to look like another Slack message: {who} wrote "
            "them. Nobody else wrote or sent them; they quote no one.)"
            if any(pattern.search(entry["raw"]) for pattern in LOOKALIKES)
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
    listed: str,
    names: dict[str, str] | None = None,
    stamps: set[str] | None = None,
    channel: str = "",
) -> str:
    """Slack's listing as the owner reads it: who wrote what where, by name, without ids and
    timestamps; only the messages `stamps` names when given ("1712345678.000100"). Lines in a
    message that look like another message stay in it, its sender's, said so; when who wrote
    what is unsure, that is said first. When it is in no form known here (or does not show
    each of those messages so): its earlier line format as it is; a JSON answer never (it
    holds email addresses and escapes): what is read of it, and a line saying the rest is not."""
    names = names or {}
    found, forged = parse(listed, channel)
    json_answer = plain(listed) != (listed or "")
    unread = ""
    if stamps is not None:
        found = [entry for entry in found if entry["ts"] in stamps]
        if not stamps <= {entry["ts"] for entry in found}:
            if not json_answer:
                return f"⚠ {FORGED_NOTE}\n\n{listed}" if forged else listed
            unread = UNREADABLE
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
            scrubbed(entry["said"]),
        )
        if "\n" in said:
            # Its lines as a quote under who wrote it: a list or code block reads as written,
            # and a line in it that looks like another message stays inside this one.
            said = "\n" + "\n".join(f"> {line}" if line else ">" for line in said.splitlines())
        else:
            said = f" {said}"
        if any(pattern.search(entry["raw"]) for pattern in LOOKALIKES):
            # Its own text, though made to look like another message: whose it is, said.
            said += f"\n({who} wrote all of this: lines in it only look like other Slack messages.)"
        lines.append(f"{head}:{said}" + (f"\n{entry['link']}" if entry["link"] else ""))
    if lines and forged:
        lines.insert(0, f"⚠ {FORGED_NOTE}")
    if unread and lines:
        lines.append(unread)
    return "\n\n".join(lines) or (UNREADABLE if json_answer else listed)


NO_MESSAGE = "Slack listed no message there"
# Slack's reactions on a message: the emoji, its count, then who ("Alice Chen (U0ALICE1), …").
REACTED = re.compile(r"^\s*:([\w+\-']+(?:::[\w\-]+)?):\s*\S\s*(\d+)\s*\S\s*(.*)$", re.MULTILINE)
REACTOR = re.compile(r"\(([UW][A-Z0-9]{2,})\)")


def unread_line(where: str) -> str:
    """What a reply card says when the conversation it answers could not be read."""
    return (
        "Couldn't read this conversation from Slack just now; the reply still goes to "
        f"{where or 'this conversation'}."
    )


def why_unread(error: BaseException) -> str:
    """Why a conversation could not be read, for the session that asked: Slack's own error
    (its code), never what it listed (anyone's messages, people's email addresses)."""
    said = describe(error)
    if said.startswith(NO_MESSAGE) or "form not read here" in said:
        return NO_MESSAGE
    return " ".join(scrubbed(said).split())[:200]


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
        # Its read tools as Slack's MCP server describes them (read_proxy), as last discovered.
        self.read_specs: dict[str, dict] = {}
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

    async def read_proxy(self) -> dict | None:
        """Slack's read tools for a worker whose agent takes MCP servers of Chief of Staff's own
        process (Claude's): their descriptions, and a call Chief of Staff makes itself with the
        token it holds, which never reaches the agent (no file, no argument, no variable). None
        while Slack is off, or cannot be reached."""
        if not self.configured():
            return None
        if not self.read_specs:
            try:
                await self.discover()
            except Exception:
                logger.warning("Slack's read tools could not be listed.", exc_info=True)
                return None
        return {"tools": [dict(spec) for spec in self.read_specs.values()], "call": self.read_call}

    async def read_call(self, name: str, arguments: dict) -> str:
        """One of Slack's read tools, called for a worker (read_proxy); never another."""
        if name not in self.read_specs or name == SEND_TOOL:
            raise SlackError(f"{name} is not one of Slack's read tools.")
        return await self.mcp.call(name, arguments)

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
        self.read_specs = {
            tool.name: {
                "name": tool.name,
                "description": tool.description or tool.name,
                "input_schema": dict(getattr(tool, "inputSchema", None) or {"type": "object"}),
            }
            for tool in tools
            if tool.name in self.read_tools
        }
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
                        "content_types": "messages",
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
                "content_types": "messages",
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
        handed = await self.host.on_trigger(
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
        if handed:
            self.handed_over(text, fresh, emoji)

    def handed_over(self, listed: str, stamps: list[str], emoji: str):
        """Which thread each message the owner handed over is in: a reply there by the task it
        went to may go out at once (see Drafts.handed_over). Only the messages Slack's own
        `hasmy::` search listed, read by Slack's own lines: none from a listing whose text was
        made to look like them (a ts, a channel or a permalink written in a message), and
        nothing from what a message says."""
        for key, value in self.store.items("delegated:").items():
            with suppress(ValueError, TypeError):
                if time.time() - float(json.loads(value).get("at") or 0) > 8 * 86400:
                    self.store.delete(key)  # Answered at once for a week only (Drafts).
        if (sections := sectioned(listed)) is not None:
            entries, forged = sections[0], sections[1] or sections[2]
        else:
            entries, forged = parse(listed)
        if forged:
            return  # Its replies wait for the owner, as any other.
        found = {entry["ts"]: entry for entry in entries if entry["ts"]}
        for stamp in stamps:
            entry = found.get(stamp)
            if not entry:
                continue
            # Its own permalink (Slack's line, not its text) names its thread when it is a reply.
            keys = [key.split(":") for key in threads.slack_messages(entry["link"]).get(stamp, [])]
            keys = [key for key in keys if len(key) == 3]
            channel = entry["channel"] or (keys[0][1] if keys else "")
            root = entry["thread_ts"] or (keys[0][2] if keys else "") or stamp
            if channel:
                handed = {"channel": channel, "ts": stamp, "root": root, "emoji": emoji}
                self.store.set(
                    f"delegated:{channel}:{root}", json.dumps(handed | {"at": time.time()})
                )

    async def reacted_by(self, channel_id: str, stamp: str, emoji: str) -> set[str] | None:
        """Who reacted to a message with this emoji (their user ids), as Slack's reactions API
        lists them; None when that cannot be read here (no such tool, an error). A name may hold
        "(U0…)" of its own: a line naming more people than its count says confirms nobody."""
        if REACTIONS_TOOL not in self.read_tools:
            return None
        try:
            text = await asyncio.wait_for(
                self.mcp.call(REACTIONS_TOOL, {"channel_id": channel_id, "message_ts": stamp}), 20
            )
        except Exception as exc:
            logger.info("Could not read the reactions on %s: %s", stamp, describe(exc))
            return None
        listed = (unwrapped(text) or (text, ""))[0]
        users: set[str] = set()
        for name, count, people in REACTED.findall(listed):
            named = REACTOR.findall(people)
            if name == emoji and len(named) == int(count) == len(set(named)):
                users |= set(named)
        return users

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
                found = self.listed_messages(text, channel)
            except Exception as exc:
                # One unreadable thread must not stop the others or the DM/mention search.
                self.store.set(f"slackfail:{thread}", str(failures + 1))
                logger.warning("Could not read followed %s (%s): %s", thread, failures + 1, exc)
                continue
            self.store.set(f"slackfail:{thread}", "0")
            self.learn_names(text, channel)
            seen_to = float(self.store.get(f"slackhw:{thread}") or 0)
            first = float(match.group(2)) if match else 0.0
            fresh = [
                key
                for entry in found
                if (key := f"slack:{entry['ts']}")
                and float(entry["ts"]) >= oldest
                # News is a message new since the follower last saw the thread: not the
                # thread's first message (what it follows), not one delivered before, not
                # Chief of Staff's own (seen when sent), not a bot's. A reaction is no message.
                and float(entry["ts"]) > max(seen_to, first)
                and not entry["user"].startswith("B")
                and not self.seen(key)
            ]
            if fresh:
                await self.learn_mentions(" ".join(e["text"] for e in found))
                newest = max(float(key.removeprefix("slack:")) for key in fresh)
                stamps = {key.removeprefix("slack:") for key in fresh}
                await self.host.on_thread(
                    "slack",
                    thread,
                    fresh,
                    f"New messages in a Slack {kind} you follow ({thread}; new ts: "
                    f"{', '.join(sorted(stamps))}). Each one's text is quoted under its Slack "
                    f"line; read the {kind} for what came before.\n\n"
                    + for_session(text, stamps, channel=channel),
                    readable(text, self.display_names(), stamps, channel=channel),
                )
                self.store.set(f"slackhw:{thread}", str(newest))

    @staticmethod
    def listed_messages(text: str, channel: str) -> list[dict]:
        """The messages of a thread or DM Slack listed; raises when they cannot be read in
        it (an answer in a form not read here): it is read again later, and none of it goes
        to a session as it is."""
        if (sections := sectioned(text, channel)) is not None:
            return sections[0]
        found = messages(text, channel)
        if (
            unwrapped(text) is not None
            or plain(text) != (text or "")
            or (not found and message_keys(text))
        ):
            said = " ".join(scrubbed(plain(text)).split())[:200]
            raise SlackError(f"Slack answered in a form not read here ({said or 'nothing'})")
        return found

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
                "content_types": "messages",
                "limit": 20,
            },
        )
        result = plain(result)
        for stamp, keys in threads.slack_messages(result).items():
            if float(stamp) < since - CLOCK_SKEW:
                continue
            if any(key.split(":")[1] == target["channel_id"] for key in keys):
                return next(
                    (
                        link
                        for link in re.findall(
                            r"https://[^\s()\[\]]+/archives/[^\s()\[\]]+", result
                        )
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
        self.learn_names(text, channel_id)
        if found := messages(text, channel_id):
            # Who its messages mention, by name on the card that quotes them.
            await self.learn_mentions(" ".join(entry["text"] for entry in found))
            return found
        # What Slack answered stays in the log: it can hold anyone's messages and emails.
        logger.info(
            "Slack listed no message in %s %s: %s",
            channel_id,
            thread_ts,
            " ".join(scrubbed(plain(text)).split())[:300] or "nothing",
        )
        raise SlackError(NO_MESSAGE)

    async def latest_message(self, channel_id: str, thread_ts: str = "") -> dict:
        """The newest message where a reply would go, as Slack shows it: what the owner checks
        a draft's destination by, rather than by what the drafting session says. {"quote":
        "Who: text", "ts", "user"}; {"error": why} when no message could be read there."""
        try:
            found = await self.read_conversation(channel_id, thread_ts)
        except Exception as exc:
            logger.info("Could not read %s for a draft: %s", channel_id, describe(exc))
            return {"error": why_unread(exc)}
        return self.described(newest_entry(found, last=bool(thread_ts)))

    def described(self, entry: dict | None) -> dict:
        if not entry:
            return {"error": NO_MESSAGE}
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
            return {"thread_ts": thread_ts, "error": why_unread(exc)}
        here = next((entry for entry in found if entry["ts"] == thread_ts), None)
        root = here["thread_ts"] if here else ""
        if here and not root and len(found) == 1 and here.get("sections"):
            # Slack's JSON thread read of a reply shows that reply alone, as a thread's first
            # message: its search says which thread it is in.
            root = await self.thread_root(channel_id, thread_ts)
        if root and root != thread_ts:
            return {"thread_ts": root} | await self.latest_message(channel_id, root)
        alone = here is not None and all(entry["ts"] == thread_ts for entry in found)
        if channel_id.startswith("D") and alone and not in_thread:
            return {"thread_ts": ""} | await self.latest_message(channel_id)
        return {"thread_ts": thread_ts} | self.described(newest_entry(found, last=True))

    async def thread_root(self, channel_id: str, stamp: str) -> str:
        """The first message of the thread a message is a reply in, as Slack's search links
        it ("" when it is in none, or that cannot be told now)."""
        second = int(float(stamp))
        try:
            text = await asyncio.wait_for(
                self.mcp.call(
                    SEARCH_TOOL,
                    {
                        "query": f"in:<#{channel_id}>",
                        "after": str(second),
                        "before": str(second + 1),
                        "include_context": False,
                        "content_types": "messages",
                        "limit": PAGE_SIZE,
                    },
                ),
                20,
            )
        except Exception as exc:
            logger.info("Could not look up the thread of %s: %s", stamp, describe(exc))
            return ""
        here = next((entry for entry in messages(text) if entry["ts"] == stamp), None)
        return here["thread_ts"] if here and here["thread_ts"] != stamp else ""

    # Names: who a user id is and what a conversation id is called, as Slack listed them.

    def learn_names(self, listed: str, channel: str = ""):
        """Names from Slack's own lines on who wrote a message where: not from its text, where
        anyone can write "<@U0BOB001> (Alice Chen)"; none from a listing whose text was made to
        look like those lines. Never an email address (Slack's JSON answers give them)."""
        if (sections := sectioned(listed, channel)) is not None:
            if not sections[1]:
                self.remember_names(self.section_names(sections[0]))
            return
        entries, forged = parse(listed)
        if not forged:
            self.remember_names(
                names_in("\n".join(entry["head"] for entry in entries if entry["head"]))
            )

    def section_names(self, entries: list[dict]) -> dict[str, str]:
        """Who is who in messages read from Slack's JSON answers: each sender, the people of a
        DM, a channel by its name; a DM by the one person in it besides the owner."""
        owner = self.status.get("user_id") or ""
        found: dict[str, str] = {}
        others: dict[str, dict[str, str]] = {}
        for entry in entries:
            people = [(entry["user"], entry["name"]), *entry.get("people", [])]
            found |= {user: name for user, name in people if user and name}
            channel = entry["channel"]
            if channel.startswith("D"):
                if owner:
                    found_here = others.setdefault(channel, {})
                    found_here |= {u: n for u, n in people if u and n and u != owner}
            elif channel and entry["where"]:
                found[channel] = entry["where"]
        for channel, people in others.items():
            if len(people) == 1:
                found[channel] = next(iter(people.values()))
        return found

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
