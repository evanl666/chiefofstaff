"""A company bot MCP server for live tests: it direct-messages the signed-in user (which
notifies their phone) and reads their replies. Messages go to BOT_OUTBOX (JSON lines);
replies come from BOT_REPLIES (a JSON list). Run over stdio."""

import json
import os
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

server = FastMCP("acmebot")


@server.tool()
def send_dm(text: str) -> str:
    """Send a direct message from acmebot to the signed-in user (Evan Liu) on Slack. It
    notifies them on their phone."""
    with Path(os.environ["BOT_OUTBOX"]).open("a") as outbox:
        outbox.write(json.dumps({"at": time.time(), "text": text}) + "\n")
    return "Sent to Evan Liu as a Slack DM from acmebot."


@server.tool()
def read_dm_replies() -> str:
    """The replies Evan Liu wrote to acmebot's direct messages, oldest first."""
    path = Path(os.environ["BOT_REPLIES"])
    replies = json.loads(path.read_text()) if path.exists() else []
    return json.dumps(replies, ensure_ascii=False) if replies else "No replies yet."


server.run()
