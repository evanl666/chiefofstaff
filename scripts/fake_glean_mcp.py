"""A company search MCP server, like Glean, for live tests: it searches a JSON file of Slack
messages and document comments (GLEAN_DATA). Run over stdio."""

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

server = FastMCP("glean")


@server.tool()
def glean_search(query: str, since: str = "") -> str:
    """Search the company's Slack messages, documents and document comments. Returns every
    matching item, newest first, with its source, where it is, its author (the signed-in
    user's own items are marked "(you)"), time, URL and text. The signed-in user is Evan Liu."""
    items = json.loads(Path(os.environ["GLEAN_DATA"]).read_text())
    items.sort(key=lambda item: item["time"], reverse=True)
    return json.dumps({"query": query, "results": items}, ensure_ascii=False, indent=1)


server.run()
