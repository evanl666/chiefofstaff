"""Local fake MCP server whose tool returns an account-connection error until authorized.

Used by scripts/live_auth_result.py and tests/test_backend_contract.py. It runs over stdio,
writes each call's outcome to the file ACME_AUTH_CALLS names (when set), and treats
ACME_AUTH_MARKER's existence as the browser step being complete.
"""

import asyncio
import os
from pathlib import Path

import mcp.server.stdio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

server = Server("acme-platform")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_acme",
            description="Search Acme Search for launch notes after the account is connected.",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
    marker = Path(os.environ["ACME_AUTH_MARKER"])
    authorized = marker.exists()
    if calls := os.environ.get("ACME_AUTH_CALLS"):
        with Path(calls).open("a") as file:
            file.write(("ok" if authorized else "blocked") + "\n")
    if not authorized:
        url = os.environ["ACME_AUTH_URL"]
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=(
                        "Authorization required for Acme Search. Connect your Acme Search "
                        f"account in a browser at {url}"
                    ),
                )
            ],
            isError=True,
        )
    query = str(arguments.get("query") or "")
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=f"Acme Search result for {query}: launch notes are ready.",
            )
        ]
    )


async def main():
    async with mcp.server.stdio.stdio_server() as streams:
        await server.run(
            *streams,
            InitializationOptions(
                server_name="acme-platform",
                server_version="1",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
