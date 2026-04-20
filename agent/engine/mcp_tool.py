"""MCP HTTP client wrapper for the engine.

Holds a single MCP session open for the duration of a turn loop — opening a
new session per tool call would add ~100ms of handshake per step.
"""
import logging
import os
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

log = logging.getLogger(__name__)


class McpClient:
    """Async-context wrapper around an MCP streamable-HTTP session.

        async with McpClient() as mcp:
            tools = await mcp.list_tools()
            blocks = await mcp.call_tool("tap", {"bbox": [0.4, 0.5, 0.5, 0.6]})

    `server_instructions` is the `instructions` field the server sent back
    in its initialize response — empty string if the server set nothing.
    Caller injects it into the system prompt.
    """

    def __init__(self, base_url: str | None = None):
        base = base_url or os.environ.get("PHYSICLAW_SERVER", "http://127.0.0.1:8048")
        self._url = base.rstrip("/") + "/mcp"
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None
        self.server_instructions: str = ""

    async def __aenter__(self) -> "McpClient":
        read, write, _ = await self._stack.enter_async_context(
            streamable_http_client(self._url)
        )
        self._session = await self._stack.enter_async_context(
            ClientSession(read, write)
        )
        init = await self._session.initialize()
        self.server_instructions = (init.instructions or "").rstrip()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()
        self._session = None

    async def list_tools(self) -> list[dict]:
        """Return tool schemas as plain dicts: {name, description, input_schema}."""
        assert self._session is not None, "McpClient not entered"
        result = await self._session.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.inputSchema,
            }
            for t in result.tools
        ]

    async def call_tool(
        self, name: str, args: dict[str, Any] | None = None
    ) -> list[dict]:
        """Call an MCP tool and return its content as a list of normalized blocks.

        Each block is either:
          {"type": "text", "text": str}
          {"type": "image", "mime_type": str, "data": <base64 str>}
        """
        assert self._session is not None, "McpClient not entered"
        result = await self._session.call_tool(name, args or {})
        blocks: list[dict] = []
        for c in result.content:
            ctype = getattr(c, "type", None)
            if ctype == "text":
                blocks.append({"type": "text", "text": c.text})
            elif ctype == "image":
                blocks.append({
                    "type": "image",
                    "mime_type": getattr(c, "mimeType", "image/jpeg"),
                    "data": c.data,
                })
            else:
                # Unknown block type — stringify for debugging. MCP may grow
                # resource/embedded types later; we don't fail on those.
                blocks.append({"type": "text", "text": repr(c)})
        if getattr(result, "isError", False):
            joined = " | ".join(b.get("text", "") for b in blocks if b["type"] == "text")
            raise RuntimeError(f"tool {name!r} failed: {joined}")
        return blocks
