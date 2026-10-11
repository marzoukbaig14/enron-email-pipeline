"""List the tools a Gmail MCP server offers, with each tool's input schema.

Why this exists: the live notification sender (notify.McpSender) calls the
server's send_email tool, and the argument names must come from the server,
not from memory. This script connects the same way the sender does (stdio
transport, one session), calls list_tools(), and prints every tool's name and
JSON input schema. Listing tools sends nothing.

Usage:
    .venv/bin/python scripts/list_mcp_tools.py                 # every tool
    .venv/bin/python scripts/list_mcp_tools.py --tool send_email
    .venv/bin/python scripts/list_mcp_tools.py --command npx --args -y @gongrzhe/server-gmail-autoauth-mcp

Needs Node (npx) and the server's OAuth files in ~/.gmail-mcp/ (see
mcp_config.json.example). Nothing here reads or prints credentials.
"""
import argparse
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

DEFAULT_COMMAND = "npx"
DEFAULT_ARGS = ["-y", "@gongrzhe/server-gmail-autoauth-mcp"]
CONNECT_TIMEOUT_SECONDS = 120  # the first npx run may have to download the package


async def list_tools(command: str, args: list[str]) -> list:
    """Connect over stdio, run the MCP handshake, and return every tool."""
    params = StdioServerParameters(command=command, args=args)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = []
            result = await session.list_tools()
            tools.extend(result.tools)
            cursor = getattr(result, "next_cursor", None)
            while cursor:  # servers may split a long tool list into pages
                from mcp.types import PaginatedRequestParams

                result = await session.list_tools(params=PaginatedRequestParams(cursor=cursor))
                tools.extend(result.tools)
                cursor = getattr(result, "next_cursor", None)
            return tools


def input_schema_of(tool) -> dict:
    # The attribute is inputSchema in the 1.x SDK and input_schema in 2.x.
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
    return schema or {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--command", default=DEFAULT_COMMAND)
    parser.add_argument("--args", nargs="*", default=DEFAULT_ARGS, help="arguments for the command")
    parser.add_argument("--tool", help="print only this tool")
    options = parser.parse_args()

    try:
        tools = asyncio.run(
            asyncio.wait_for(list_tools(options.command, options.args), CONNECT_TIMEOUT_SECONDS)
        )
    except Exception as error:  # a clear one-line failure beats a traceback here
        print(f"error: could not list tools: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    shown = [t for t in tools if options.tool in (None, t.name)]
    if not shown:
        names = ", ".join(t.name for t in tools)
        print(f"error: no tool named {options.tool!r}. Tools offered: {names}", file=sys.stderr)
        return 1
    print(f"{len(tools)} tool(s) offered: {', '.join(t.name for t in tools)}\n")
    for tool in shown:
        print(f"=== {tool.name}")
        if tool.description:
            print(f"description: {' '.join(tool.description.split())}")
        print("input schema:")
        print(json.dumps(input_schema_of(tool), indent=2))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
