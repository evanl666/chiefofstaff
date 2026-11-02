"""A local stand-in for Slack's official MCP server, for live tests and demos.

It exposes the same tool names, parameters and read-only annotations as
https://mcp.slack.com/mcp, backed by an in-memory workspace, and requires
`Authorization: Bearer <token>` like the real server.

    uv run python scripts/fake_slack_mcp.py --port 18900 --token xoxp-demo
    CHIEFOFSTAFF_SLACK_MCP_URL=http://127.0.0.1:18900/mcp \\
    CHIEFOFSTAFF_SLACK_USER_TOKEN=xoxp-demo uv run python -m chiefofstaff
"""

import argparse
import threading
import time

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

OWNER = "U0OWNER1"
NAMES = {OWNER: "Owner", "U0ALICE1": "Alice Chen", "U0BOB001": "Bob Li"}
CHANNELS = {"C0DEPLOYS": "deploys", "D0ALICE01": "DM with Alice", "D0BOB0001": "DM with Bob"}
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)


class Workspace:
    def __init__(self):
        self.messages: list[dict] = []
        self.sent: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.unauthorized = 0
        self.lock = threading.Lock()

    def post(self, channel: str, user: str, text: str, thread_ts: str = "") -> dict:
        with self.lock:
            stamp = f"{time.time():.6f}"
            while any(m["ts"] == stamp for m in self.messages):
                stamp = f"{float(stamp) + 0.000001:.6f}"
            message = {
                "channel": channel,
                "ts": stamp,
                "user": user,
                "text": text,
                "thread_ts": thread_ts,
            }
            self.messages.append(message)
            return message

    @staticmethod
    def permalink(message: dict) -> str:
        stamp = message["ts"].replace(".", "")
        link = f"https://acme.slack.com/archives/{message['channel']}/p{stamp}"
        if message["thread_ts"] and message["thread_ts"] != message["ts"]:
            link += f"?thread_ts={message['thread_ts']}&cid={message['channel']}"
        return link

    def render(self, message: dict) -> str:
        channel = message["channel"]
        where = f"<#{channel}> ({CHANNELS.get(channel, channel)})"
        thread = f" | thread_ts: {message['thread_ts']}" if message["thread_ts"] else ""
        return (
            f"Channel: {where} | From: <@{message['user']}> ({NAMES.get(message['user'], '?')})"
            f" | ts: {message['ts']}{thread}\nText: {message['text']}\n"
            f"Permalink: {self.permalink(message)}"
        )


def matches(message: dict, query: str) -> bool:
    """Slack-like search: every term must match; supports to:me, in:, from: and @mentions."""
    channel, user, text = message["channel"], message["user"], message["text"].lower()
    for term in query.split():
        term = term.strip('"')
        lowered = term.lower()
        if lowered == "to:me":
            if not (channel.startswith("D") and user != OWNER):
                return False
        elif lowered.startswith("in:"):
            target = term[3:].strip("<>#").split("|")[0]
            if target not in (channel, CHANNELS.get(channel, "")):
                return False
        elif lowered.startswith("from:"):
            target = term[5:].strip("<>@")
            if target != user and target.lower() not in NAMES.get(user, "").lower():
                return False
        elif lowered.startswith(("after:", "before:", "on:", "during:", "is:", "has:")):
            continue
        elif lowered not in text:
            return False
    return True


def build_app(workspace: Workspace, token: str):
    server = FastMCP("fake-slack", stateless_http=True, json_response=True, log_level="WARNING")

    def record(name: str, arguments: dict):
        with workspace.lock:
            workspace.calls.append((name, arguments))

    @server.tool(
        name="slack_search_public_and_private",
        description=(
            "Searches for messages, files in ALL Slack channels, including public channels, "
            f"private channels, DMs, and group DMs. Current logged in user's user_id is {OWNER}."
        ),
        annotations=READ,
    )
    def search(
        query: str,
        after: str = "",
        before: str = "",
        sort: str = "score",
        sort_dir: str = "desc",
        include_context: bool = True,
        limit: int = 20,
        channel_types: str = "",
        content_types: str = "",
        response_format: str = "detailed",
        cursor: str = "",
        include_bots: bool = False,
        max_context_length: int = 0,
        context_channel_id: str = "",
    ) -> str:
        record("slack_search_public_and_private", {"query": query, "after": after})
        found = [
            message
            for message in workspace.messages
            if not (after and float(message["ts"]) < float(after)) and matches(message, query)
        ]
        if sort_dir == "desc":
            found.reverse()
        return "\n\n".join(workspace.render(m) for m in found[:limit]) or "No results found."

    @server.tool(
        name="slack_search_channels",
        description="Search for channels by name or description. Returns channel IDs.",
        annotations=READ,
    )
    def search_channels(query: str, limit: int = 20, response_format: str = "detailed") -> str:
        record("slack_search_channels", {"query": query})
        needle = query.lower().lstrip("#")
        found = [
            f"{channel_id} #{name}"
            for channel_id, name in CHANNELS.items()
            if channel_id.startswith("C") and needle in name.lower()
        ]
        return "\n".join(found) or "No channels found."

    @server.tool(
        name="slack_read_thread",
        description="Reads messages from a specific Slack thread (parent message + all replies).",
        annotations=READ,
    )
    def read_thread(
        channel_id: str,
        message_ts: str,
        limit: int = 50,
        cursor: str = "",
        latest: str = "",
        oldest: str = "",
        response_format: str = "detailed",
    ) -> str:
        record("slack_read_thread", {"channel_id": channel_id, "message_ts": message_ts})
        thread = [
            m
            for m in workspace.messages
            if m["channel"] == channel_id
            and message_ts in (m["ts"], m["thread_ts"])
            and (not oldest or m["ts"] == message_ts or float(m["ts"]) >= float(oldest))
        ]
        return "\n\n".join(workspace.render(m) for m in thread) or "Thread not found."

    @server.tool(
        name="slack_read_channel",
        description="Reads messages from a Slack channel in reverse chronological order.",
        annotations=READ,
    )
    def read_channel(
        channel_id: str,
        limit: int = 20,
        cursor: str = "",
        latest: str = "",
        oldest: str = "",
        response_format: str = "detailed",
    ) -> str:
        record("slack_read_channel", {"channel_id": channel_id})
        found = [
            m
            for m in workspace.messages
            if m["channel"] == channel_id
            and not m["thread_ts"]
            and (not oldest or float(m["ts"]) >= float(oldest))
        ][-limit:]
        return "\n\n".join(workspace.render(m) for m in reversed(found)) or "No messages."

    @server.tool(
        name="slack_read_user_profile",
        description="Retrieves profile information for a Slack user.",
        annotations=READ,
    )
    def read_profile(
        user_id: str = OWNER, include_locale: bool = False, response_format: str = "detailed"
    ) -> str:
        record("slack_read_user_profile", {"user_id": user_id})
        return f"User {user_id}: {NAMES.get(user_id, 'Unknown')}, timezone America/Los_Angeles"

    @server.tool(
        name="slack_send_message",
        description="Sends a message to a Slack channel or user. Return the message link.",
        annotations=WRITE,
    )
    def send_message(
        channel_id: str,
        message: str,
        thread_ts: str = "",
        reply_broadcast: bool = False,
        draft_id: str = "",
    ) -> str:
        record("slack_send_message", {"channel_id": channel_id, "thread_ts": thread_ts})
        posted = workspace.post(channel_id, OWNER, message, thread_ts)
        with workspace.lock:
            workspace.sent.append(posted)
        return f"Message sent: {workspace.permalink(posted)}"

    @server.tool(
        name="slack_schedule_message",
        description="Schedules a message for future delivery.",
        annotations=WRITE,
    )
    def schedule_message(channel_id: str, message: str, post_at: int, thread_ts: str = "") -> str:
        record("slack_schedule_message", {"channel_id": channel_id})
        return "Scheduled."

    class RequireToken(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.headers.get("authorization") != f"Bearer {token}":
                workspace.unauthorized += 1
                return JSONResponse({"error": "invalid_auth"}, status_code=401)
            return await call_next(request)

    app = server.streamable_http_app()
    app.add_middleware(RequireToken)
    return app


def serve_in_thread(workspace: Workspace, token: str, port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        build_app(workspace, token), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("Fake Slack MCP did not start.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=18900)
    parser.add_argument("--token", default="xoxp-demo")
    args = parser.parse_args()
    workspace = Workspace()
    serve_in_thread(workspace, args.token, args.port)
    print(f"Fake Slack MCP on http://127.0.0.1:{args.port}/mcp. Type a DM from Alice:")
    try:
        while True:
            text = input("> ").strip()
            if text:
                message = workspace.post("D0ALICE01", "U0ALICE1", text)
                print(f"posted ts={message['ts']}")
            for sent in workspace.sent:
                print(f"[sent by owner] {sent['channel']}: {sent['text']}")
            workspace.sent.clear()
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    main()
