"""A stand-in MCP server with as many tools as several real ones together (MANY_TOOLS, 250 by
default), each about the size of a real server's tool (a paragraph of description, a schema of
documented parameters): enough that loading them all inline exceeds a model's usable context.
Its `ping` tool answers "pong"; each call's tool name is added to the file MANY_TOOLS_CALLS
names, if one does. Run over stdio (scripts/live_hotfix.py, tests/test_backend_contract.py)."""

import asyncio
import os
from pathlib import Path

import mcp.server.stdio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

COUNT = int(os.environ.get("MANY_TOOLS", "250"))
AREAS = ["tickets", "pages", "boards", "builds", "alerts", "contacts", "invoices", "files"]
VERBS = ["list", "get", "search", "create", "update", "archive", "export", "link"]

server = Server("acme-tools")


def schema(area: str) -> dict:
    """A parameter schema as large as a real server's: documented fields, enums, nesting."""
    fields = {
        "workspace_id": "The workspace the request runs in, as shown in the workspace's URL.",
        "query": f"Free text matched against the {area}' titles, bodies and comments.",
        "owner": "Only items owned by this person (an email address or a user id).",
        "labels": "Only items carrying every one of these labels (exact names, case-sensitive).",
        "updated_after": "Only items changed after this time (ISO 8601, with a time zone).",
        "updated_before": "Only items changed before this time (ISO 8601, with a time zone).",
        "page_token": "The token a previous call returned, to read the next page of results.",
        "page_size": "How many results a page holds (1 to 200; the server's default is 50).",
        "project_id": f"Only {area} in this project (its id, as the project's settings show it).",
        "assignee": "Only items assigned to this person (an email address or a user id).",
        "status": "Only items in this status (open, in progress, blocked, done or archived).",
        "format": "How each result is returned: 'summary' (the default) or 'full', with history.",
    }
    properties: dict = {name: {"type": "string", "description": text}
                        for name, text in fields.items()}  # fmt: skip
    properties["labels"] = {"type": "array", "items": {"type": "string"},
                            "description": fields["labels"]}  # fmt: skip
    properties["page_size"] = {"type": "integer", "minimum": 1, "maximum": 200,
                               "description": fields["page_size"]}  # fmt: skip
    properties["sort"] = {
        "type": "object",
        "description": "How results are ordered.",
        "properties": {
            "field": {
                "type": "string",
                "enum": ["created", "updated", "priority", "title"],
                "description": "The field results are ordered by.",
            },
            "direction": {
                "type": "string",
                "enum": ["asc", "desc"],
                "description": "Ascending or descending order.",
            },
        },
    }
    return {"type": "object", "properties": properties, "required": ["workspace_id"]}


def make_tools() -> list[types.Tool]:
    tools = [
        types.Tool(
            name="ping",
            description="Answers pong: checks that the acme tools are reachable.",
            inputSchema={"type": "object", "properties": {}},
        )
    ]
    for index in range(COUNT - 1):
        area, verb = AREAS[index % len(AREAS)], VERBS[(index // len(AREAS)) % len(VERBS)]
        name = f"{verb}_{area}_{index}"
        description = (
            f"{verb.title()} {area} in an acme workspace (variant {index}). Use this when the "
            f"user asks about {area} by title, owner, label or date. Results are paged: pass the "
            "page token a call returns to read the next page, and keep the page size small when "
            "only the first few results matter. Times are ISO 8601 with a time zone. Items the "
            "signed-in user cannot see are left out without an error; an empty result means "
            "nothing matched, not that the workspace is empty. Rate limits apply per workspace: "
            "a 429 answer says how many seconds to wait before trying again."
        )
        tools.append(types.Tool(name=name, description=description, inputSchema=schema(area)))
    return tools


TOOLS = make_tools()


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if calls := os.environ.get("MANY_TOOLS_CALLS"):
        with Path(calls).open("a") as file:
            file.write(name + "\n")
    text = "pong" if name == "ping" else f"No {name.split('_')[1]} matched."
    return [types.TextContent(type="text", text=text)]


async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="acme-tools",
                server_version="1.0.0",
                capabilities=server.get_capabilities(NotificationOptions(), {}),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
