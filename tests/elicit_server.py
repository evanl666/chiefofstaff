"""A stdio MCP server whose one tool, `form`, asks its client for a form (MCP elicitation) and
returns what came back: the contract tests' stand-in for a server that needs the owner."""

from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel

server = FastMCP("asker")


class Pick(BaseModel):
    color: str


@server.tool()
async def form(ctx: Context) -> str:
    """Asks which color."""
    result = await ctx.elicit(message="Which color?", schema=Pick)
    data = getattr(result, "data", None)
    return f"action={result.action} color={getattr(data, 'color', '')}"


if __name__ == "__main__":
    server.run()
