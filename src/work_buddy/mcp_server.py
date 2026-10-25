"""Task-scoped stdio MCP tools for a Copilot process; no Slack credentials live here."""

import asyncio
import json
import os

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .tools import TOOL_SPECS


async def main():
    endpoint = os.environ["WORK_BUDDY_TOOL_ENDPOINT"]
    token = os.environ["WORK_BUDDY_AGENT_CAPABILITY"]
    enabled = json.loads(os.environ["WORK_BUDDY_TOOL_NAMES"])
    server = Server("work-buddy")

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name=name,
                description=TOOL_SPECS[name][1],
                inputSchema=TOOL_SPECS[name][0].model_json_schema(),
            )
            for name in enabled
        ]

    async with httpx.AsyncClient(
        timeout=25,
        trust_env=False,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:

        @server.call_tool()
        async def call_tool(name: str, arguments: dict):
            if name not in enabled:
                raise ValueError("Tool is not available to this agent.")
            response = await client.post(endpoint, json={"name": name, "arguments": arguments})
            if not response.is_success:
                raise RuntimeError(response.json().get("detail", "Tool request failed."))
            return [types.TextContent(type="text", text=json.dumps(response.json()))]

        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
