"""Slack through Slack's official MCP server (https://mcp.slack.com/mcp).

Work Buddy uses the owner's Slack *user* token in two places:
- the watcher searches for new DMs and mentions without involving a model, and only wakes the
  Router when something new arrived;
- sessions that handle Slack get the MCP server with read-only tools. Sending is never exposed
  to a model: drafts are approved by the owner and sent here, by the application.
"""

import asyncio
import hashlib
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import keyring
from keyring.errors import KeyringError
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .. import threads
from ..store import Store, now
from . import SourceHost

logger = logging.getLogger(__name__)

SLACK_MCP_URL = os.environ.get("WORK_BUDDY_SLACK_MCP_URL", "https://mcp.slack.com/mcp")
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
PERMALINK = re.compile(r"/archives/[A-Z0-9]+/p(\d{10})(\d{6})")
TIMESTAMP = re.compile(r"(?<![\d.])(\d{10}\.\d{6})(?![\d])")
THREAD_TS = re.compile(r"thread_ts\W{0,4}\d{10}\.\d{6}")
USER_ID = re.compile(r"user_id is (U[A-Z0-9]{6,})")


class SlackError(RuntimeError):
    pass


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


class SlackTokens:
    ENV = "WORK_BUDDY_SLACK_USER_TOKEN"

    def __init__(self, directory: Path):
        self.service = "work-buddy-" + hashlib.sha256(str(directory).encode()).hexdigest()[:16]

    def get(self) -> str:
        if token := os.environ.get(self.ENV):
            return token
        try:
            return keyring.get_password(self.service, "slack-user") or ""
        except KeyringError:
            logger.warning("OS keyring unavailable; set %s instead.", self.ENV)
            return ""

    def save(self, token: str):
        try:
            keyring.set_password(self.service, "slack-user", token)
        except KeyringError as exc:
            raise RuntimeError(f"OS keyring unavailable. Set {self.ENV} instead.") from exc

    def delete(self):
        try:
            keyring.delete_password(self.service, "slack-user")
        except KeyringError:
            pass


class SlackMcp:
    def __init__(self, token: Callable[[], str], url: str = SLACK_MCP_URL):
        self.token = token
        self.url = url

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[ClientSession]:
        token = self.token()
        if not token:
            raise SlackError("Slack is not connected. Add your Slack user token in Settings.")
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(30, read=60),
        ) as http:
            async with streamable_http_client(self.url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def list_tools(self) -> list:
        try:
            async with asyncio.timeout(60), self.connect() as session:
                return (await session.list_tools()).tools
        except SlackError:
            raise
        except Exception as exc:
            raise SlackError(f"Slack MCP is unreachable: {describe(exc)}"[:500]) from exc

    async def call(self, name: str, arguments: dict) -> str:
        try:
            async with asyncio.timeout(60), self.connect() as session:
                result = await session.call_tool(name, arguments)
        except SlackError:
            raise
        except Exception as exc:
            raise SlackError(f"Slack MCP call {name} failed: {describe(exc)}"[:500]) from exc
        text = "\n".join(item.text for item in result.content if getattr(item, "text", None))
        if result.isError:
            raise SlackError(f"Slack rejected {name}: {text[:400]}")
        return text


class SlackSource:
    name = "slack"

    def __init__(
        self,
        store: Store,
        host: SourceHost,
        *,
        tokens: SlackTokens | None = None,
        mcp: SlackMcp | None = None,
    ):
        self.store = store
        self.host = host
        self.tokens = tokens or SlackTokens(store.directory)
        self.mcp = mcp or SlackMcp(self.tokens.get)
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
            try:
                await asyncio.wait_for(
                    self.changed.wait(), timeout=self.store.settings().slack_poll_seconds
                )
            except TimeoutError:
                pass

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
            self.store.set("slack_cursor", str(started))
            self.status.update(connected=True, error="", last_checked=now())
            return
        await self.poll_followed()
        followed = {sub["thread"]: sub["since"] for sub in self.host.followed("slack:")}
        since = max(0, int(float(cursor)) - LOOKBACK_SECONDS)
        sections: list[str] = []
        shown: list[str] = []
        unseen: list[str] = []
        thread_keys: list[str] = []
        for_followers: dict[str, tuple[list[str], list[str]]] = {}
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
                found = message_keys(text)
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
                            bucket[0].append(key)
                            bucket[1].append(text[:MAX_RESULT_CHARS])
                        continue
                    fresh.append(key)
                    thread_keys += keys
                if fresh:
                    unseen += fresh
                    sections.append(f"Search `{query}`:\n{text[:MAX_RESULT_CHARS]}")
                    shown.append(text[:MAX_RESULT_CHARS])
                # A full page may hide newer matches: continue after the newest one returned.
                newest = int(max((float(key.removeprefix("slack:")) for key in found), default=0))
                if len(found) < PAGE_SIZE or newest <= after:
                    break
                after = newest
        for thread, (keys, texts) in for_followers.items():
            body = "\n\n".join(dict.fromkeys(texts))
            delivered = await self.host.on_thread(
                "slack",
                thread,
                keys,
                f"New messages in a Slack conversation you follow ({thread}):\n\n{body}",
                body,
            )
            if not delivered:  # Nobody follows it any more.
                unseen += [key for key in keys if not self.seen(key)]
                sections.append(body)
                shown.append(body)
        if unseen:
            keys_line = (
                f"Thread keys: {', '.join(dict.fromkeys(thread_keys))}\n" if thread_keys else ""
            )
            await self.host.on_trigger(
                "slack",
                unseen,
                f"{len(unseen)} new Slack message(s) to or mentioning the owner "
                f"(new ts: {', '.join(key.removeprefix('slack:') for key in unseen)}). "
                "Results may also contain messages handled earlier.\n"
                + keys_line
                + "\n"
                + "\n\n".join(sections),
                "\n\n".join(dict.fromkeys(shown)),
            )
        # Only advance after the Router accepted the events, so a failure is retried.
        self.store.set("slack_cursor", str(started))
        self.status.update(connected=True, error="", last_checked=now())

    def seen(self, key: str) -> bool:
        return bool(self.store.one("SELECT id FROM triggers WHERE key=?", (key,)))

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
            failures = int(self.store.get(f"slackfail:{thread}") or 0)
            # After repeated failures retry only every tenth poll (quiet follows expire anyway).
            if failures >= MAX_FOLLOW_FAILURES and failures % 10:
                self.store.set(f"slackfail:{thread}", str(failures + 1))
                continue
            try:
                text = await self.mcp.call(tool, {**arguments, "oldest": str(oldest), "limit": 50})
            except Exception as exc:
                # One unreadable thread must not stop the others or the DM/mention search.
                self.store.set(f"slackfail:{thread}", str(failures + 1))
                logger.warning("Could not read followed %s (%s): %s", thread, failures + 1, exc)
                continue
            self.store.set(f"slackfail:{thread}", "0")
            fresh = [
                key
                for key in message_keys(text)
                if float(key.removeprefix("slack:")) >= oldest and not self.seen(key)
            ]
            if fresh:
                newest = max(float(key.removeprefix("slack:")) for key in fresh)
                await self.host.on_thread(
                    "slack",
                    thread,
                    fresh,
                    f"New messages in a Slack {kind} you follow ({thread}; new ts: "
                    f"{', '.join(key.removeprefix('slack:') for key in fresh)}). Earlier "
                    f"messages may be included for context.\n\n{text[:MAX_RESULT_CHARS]}",
                    text[:MAX_RESULT_CHARS],
                )
                self.store.set(f"slackhw:{thread}", str(newest))

    async def send(self, target: dict, text: str) -> str:
        if self.fingerprint and not self.can_send:
            raise SlackError("This Slack token lacks chat:write; nothing was sent.")
        arguments = {"channel_id": target["channel_id"], "message": text}
        if target.get("thread_ts"):
            arguments["thread_ts"] = target["thread_ts"]
        return await self.mcp.call(SEND_TOOL, arguments)
