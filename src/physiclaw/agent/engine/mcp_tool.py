"""MCP HTTP client wrapper for the engine.

Holds one MCP session open for the runtime process's lifetime — opening
a new session per agent wake would add ~100ms of handshake per cycle
and spam the log with "GET stream disconnected" on every close.
Module-level `get_mcp()` returns the singleton; `close_mcp()` tears it
down at process exit.
"""

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx2
from mcp import ClientSession, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import INVALID_REQUEST, ImageContent, TextContent

from physiclaw.agent.runtime.stopping import checkpoint
from physiclaw.common import platform
from physiclaw.common.config import server_url

log = logging.getLogger(__name__)


class McpClient:
    """Async-context wrapper around an MCP streamable-HTTP session.

        async with McpClient() as mcp:
            tools = await mcp.list_tools()
            blocks = await mcp.call_tool("tap", {"bbox": [0.4, 0.5, 0.5, 0.6]})

    `server_instructions` is the `instructions` field the server sent back
    in its initialize response — empty string if the server set nothing.
    Connect, reconnect and close in the same task: the SDK transport keeps
    its anyio cancellation scopes in the task that entered the context.
    """

    def __init__(self, base_url: str | None = None):
        base = base_url or server_url()
        self._url = base.rstrip("/") + "/mcp"
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None
        # A failed reconnect may resume; an explicitly closed client may not.
        self._reconnect_pending = False
        self.server_instructions: str = ""

    async def __aenter__(self) -> "McpClient":
        """Connect, or raise ConnectionError naming the URL. Unwind partial
        setup on every failure, including cancellation. Cancellation keeps
        its original type: the transport's anyio scopes can cancel the host
        task too, and that cannot be distinguished from an operator stop."""
        try:
            await self._connect()
            self._reconnect_pending = False
            return self
        except BaseException as e:
            await self._safe_close()
            if isinstance(e, Exception) and not isinstance(e, ConnectionError):
                raise ConnectionError(
                    f"cannot reach the MCP server at {self._url}: {e}"
                ) from e
            raise

    async def _probe(self) -> None:
        """Fail fast, and legibly, when nothing is listening.

        Inside `streamable_http_client` the handshake runs in an anyio task
        group: a refused connection there collapses the scope into a bare
        `CancelledError`, which no `except Exception` above us can catch and
        which is indistinguishable from a real shutdown. So we answer the
        question before handing over control.

        A raw TCP connect, not an HTTP request: the MCP server is this
        runtime's parent on localhost, but `trust_env` is on, so a configured
        system proxy answers an HTTP pre-flight itself — returning 502 for a
        dead upstream, which looks reachable. The socket cannot be fooled
        that way."""
        host, port = self._host_port()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=5.0
            )
        except (OSError, asyncio.TimeoutError) as e:
            raise ConnectionError(f"cannot reach the MCP server at {self._url}") from e
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    def _host_port(self) -> tuple[str, int]:
        parsed = urlparse(self._url)
        return parsed.hostname or "127.0.0.1", parsed.port or (
            443 if parsed.scheme == "https" else 80
        )

    async def _safe_close(self) -> None:
        """Unwind a half-open stack. A teardown error must not mask the
        connect error we are about to raise."""
        try:
            await self.__aexit__(None, None, None)
        except BaseException:
            log.debug("MCP teardown after failed connect", exc_info=True)

    async def _with_session(self, op: Callable[[], Awaitable[Any]]) -> Any:
        """Run one session call; when the server no longer knows our
        session (it restarted under a runtime that outlived it), one
        fresh handshake, then the same call — every later call would
        fail the same way, and the model cannot repair a transport from
        inside a turn."""
        checkpoint()
        if self._reconnect_pending:
            await self._reconnect()
        try:
            return await op()
        except MCPError as e:
            # These are the SDK's two expired-session responses, both sent
            # before tool execution. Similar text in an application error
            # is not permission to repeat a physical action.
            if e.code != INVALID_REQUEST or e.message not in {
                "Session not found",
                "Session terminated",
            }:
                raise
            checkpoint()
            log.warning("MCP session lost (%s) — reconnecting once", e)
            await self._reconnect()
            return await op()

    def _live(self) -> ClientSession:
        assert self._session is not None, "McpClient not entered"
        return self._session

    async def _reconnect(self) -> None:
        """Replace a lost session in its owning task. If the server is still
        down, the next call retries the handshake before sending its request.
        Cancellation during close propagates without starting new work."""
        try:
            await self.__aexit__(None, None, None)
            checkpoint()
            self._stack = AsyncExitStack()
            await self.__aenter__()
            checkpoint()
        finally:
            self._reconnect_pending = self._session is None

    async def _connect(self) -> "McpClient":
        # Hand the transport our own httpx2 client so `trust_env` follows the
        # same per-platform proxy policy as every other localhost client (the
        # status poll, phone-watch hook, doctor). The MCP server is this
        # runtime's parent on 127.0.0.1; with httpx2's default trust_env=True a
        # configured system proxy (common on Windows) hijacks that localhost
        # request and the initialize() handshake hangs forever. We own the
        # client, so our stack closes it — entered first so it outlives the
        # transport's terminate-on-close DELETE. Other kwargs match mcp's
        # create_mcp_http_client defaults.
        await self._probe()  # legible error before the transport swallows it
        checkpoint()
        http_client = await self._stack.enter_async_context(
            httpx2.AsyncClient(
                follow_redirects=True,
                timeout=httpx2.Timeout(30.0, read=300.0),
                trust_env=platform.TRUST_PROXY_ENV,
            )
        )
        read, write = await self._stack.enter_async_context(
            streamable_http_client(self._url, http_client=http_client)
        )
        self._session = await self._stack.enter_async_context(
            ClientSession(read, write)
        )
        checkpoint()
        init = await self._session.initialize()
        self.server_instructions = (init.instructions or "").rstrip()
        log.info("MCP client connected (%s)", self._url)
        return self

    async def __aexit__(self, *exc) -> None:
        self._reconnect_pending = False
        try:
            await self._stack.aclose()
        finally:
            self._session = None
            self.server_instructions = ""

    async def list_tools(self) -> list[dict]:
        """Return tool schemas as plain dicts: {name, description, input_schema}."""
        result = await self._with_session(lambda: self._live().list_tools())
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.input_schema,
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
        result = await self._with_session(
            lambda: self._live().call_tool(name, args or {})
        )
        blocks: list[dict] = []
        for c in result.content:
            if isinstance(c, TextContent):
                blocks.append({"type": "text", "text": c.text})
            elif isinstance(c, ImageContent):
                blocks.append(
                    {
                        "type": "image",
                        "mime_type": c.mime_type,
                        "data": c.data,
                    }
                )
                if c.meta:
                    blocks[-1]["_meta"] = c.meta
            else:
                # Unknown block type — stringify for debugging. MCP may grow
                # resource/embedded types later; we don't fail on those.
                blocks.append({"type": "text", "text": repr(c)})
        if getattr(result, "is_error", False):
            joined = " | ".join(
                b.get("text", "") for b in blocks if b["type"] == "text"
            )
            raise RuntimeError(f"tool {name!r} failed: {joined}")
        return blocks


# ---------- process-level singleton ----------

_stack: AsyncExitStack | None = None
_mcp: "McpClient | None" = None
_tools_cache: list[dict] | None = None


async def get_mcp() -> McpClient:
    """Return the process-level McpClient, opening it on first call.

    Persists across agent wakes so the SSE channel and initialize
    handshake are paid once per runtime process, not once per session.
    """
    global _stack, _mcp
    checkpoint()
    if _mcp is None:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            _mcp = await stack.enter_async_context(McpClient())
        except BaseException:
            # Unwind anything entered before the failure so the stack
            # doesn't leak transports / sessions on a half-open client.
            await stack.aclose()
            raise
        _stack = stack
    return _mcp


async def list_tools_cached() -> list[dict]:
    """Cached MCP tool schemas. Stable for the runtime's lifetime — the
    MCP server is the runtime's parent process, so the tool surface can't
    change without restarting both."""
    global _tools_cache
    if _tools_cache is None:
        mcp = await get_mcp()
        _tools_cache = await mcp.list_tools()
    return _tools_cache


async def close_mcp() -> None:
    """Close the singleton if it's open. Safe to call when it isn't."""
    global _stack, _mcp, _tools_cache
    if _stack is None:
        return
    stack, _stack, _mcp, _tools_cache = _stack, None, None, None
    try:
        await stack.aclose()
        log.info("MCP client closed", extra={"event": "runtime.mcp_closed"})
    except Exception:
        log.warning("MCP client close failed", exc_info=True)
