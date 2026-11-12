"""An MCP server for Codex's contract tests: a GitHub-like write tool and a read-only one. Each
call, and its start, leaves a marker file in MARKER_DIR. Run over stdio."""

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

NAME = os.environ.get("MARKER_NAME", "github")
server = FastMCP(NAME)
Path(os.environ["MARKER_DIR"], f"started-{NAME}").write_text("1")


@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
def create_pull_request_review(body: str = "") -> str:
    """Publish a review on a pull request."""
    Path(os.environ["MARKER_DIR"], "review-published").write_text(body)
    return "published"


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_me() -> str:
    """Who is signed in."""
    return "octocat"


server.run()
