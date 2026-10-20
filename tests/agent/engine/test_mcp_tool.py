"""Tests for `physiclaw.agent.engine.mcp_tool` — MCP HTTP wrapper.

The MCP transport (`streamable_http_client`) and `ClientSession` are
both mocked with `AsyncMock`. Tests assert the public-surface
behavior: URL construction, session lifecycle, content normalization,
error mapping, and the process-level singleton + cache.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp import MCPError
from mcp.types import INTERNAL_ERROR, INVALID_REQUEST, ImageContent, TextContent

from physiclaw.agent.engine import mcp_tool
from physiclaw.agent.runtime.stopping import stop_event


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must start with the module-level singleton cleared."""
    monkeypatch.setattr(mcp_tool, "_stack", None)
    monkeypatch.setattr(mcp_tool, "_mcp", None)
    monkeypatch.setattr(mcp_tool, "_tools_cache", None)


@pytest.fixture(autouse=True)
def _no_real_server_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip PHYSICLAW_SERVER so the URL construction tests start clean."""
    monkeypatch.delenv("PHYSICLAW_SERVER", raising=False)


# ---------- URL construction ----------


def test_default_url_is_localhost_8048() -> None:
    c = mcp_tool.McpClient()

    assert c._url == "http://127.0.0.1:8048/mcp"


def test_url_uses_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHYSICLAW_SERVER", "http://other.host:9999")

    c = mcp_tool.McpClient()

    assert c._url == "http://other.host:9999/mcp"


def test_url_uses_constructor_base_url_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHYSICLAW_SERVER", "http://env.host")

    c = mcp_tool.McpClient(base_url="http://explicit.host:1234")

    assert c._url == "http://explicit.host:1234/mcp"


def test_url_strips_trailing_slash_from_base() -> None:
    c = mcp_tool.McpClient(base_url="http://host:1234/")

    assert c._url == "http://host:1234/mcp"


# ---------- McpClient lifecycle ----------


@pytest.fixture
def fake_session(mocker):
    """A mocked ClientSession with controllable list_tools / call_tool."""
    sess = mocker.MagicMock()
    sess.initialize = mocker.AsyncMock(
        return_value=SimpleNamespace(instructions="server says hi")
    )
    sess.list_tools = mocker.AsyncMock()
    sess.call_tool = mocker.AsyncMock()
    sess.aclose = mocker.AsyncMock()
    return sess


async def _no_probe(self) -> None:
    """Stand-in for the socket pre-flight when the transport is faked."""


@pytest.fixture
def patched_transport(mocker, fake_session):
    """Patch streamable_http_client + ClientSession so __aenter__ wires
    a synthetic read/write triple and our fake session. Records the
    httpx client handed to the transport on ``fake_session.captured``."""
    captured: dict = {}

    @asynccontextmanager
    async def fake_http(url: str, *, http_client=None):
        captured["http_client"] = http_client
        try:
            yield ("read", "write")
        finally:
            captured["transport_closed"] = True

    mocker.patch.object(mcp_tool, "streamable_http_client", fake_http)
    # The connect pre-flight opens a real socket; a faked transport means
    # there is nothing to reach, so stand it down with the rest.
    mocker.patch.object(mcp_tool.McpClient, "_probe", _no_probe)

    @asynccontextmanager
    async def fake_session_ctx(read, write):
        try:
            yield fake_session
        finally:
            captured["session_closed"] = True
            await fake_session.aclose()

    mocker.patch.object(mcp_tool, "ClientSession", side_effect=fake_session_ctx)
    fake_session.captured = captured
    return fake_session


@pytest.mark.asyncio
async def test_async_enter_hands_transport_a_trust_env_aware_client(
    patched_transport,
) -> None:
    # The transport must get our own httpx client whose trust_env follows the
    # per-platform proxy policy — otherwise httpx routes the localhost MCP
    # request through a configured system proxy (Windows) and initialize hangs.
    async with mcp_tool.McpClient():
        pass

    client = patched_transport.captured["http_client"]
    assert client is not None
    assert client.trust_env is mcp_tool.platform.TRUST_PROXY_ENV


@pytest.mark.asyncio
async def test_async_enter_calls_initialize_and_records_instructions(
    patched_transport,
) -> None:
    async with mcp_tool.McpClient() as c:
        assert c.server_instructions == "server says hi"

    patched_transport.initialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_enter_strips_trailing_whitespace_from_instructions(
    patched_transport, mocker
) -> None:
    patched_transport.initialize.return_value = SimpleNamespace(
        instructions="hello   \n"
    )

    async with mcp_tool.McpClient() as c:
        assert c.server_instructions == "hello"


@pytest.mark.asyncio
async def test_async_enter_handles_none_instructions(
    patched_transport,
) -> None:
    patched_transport.initialize.return_value = SimpleNamespace(instructions=None)

    async with mcp_tool.McpClient() as c:
        assert c.server_instructions == ""


# ---------- list_tools ----------


@pytest.mark.asyncio
async def test_list_tools_returns_normalized_dicts(patched_transport) -> None:
    patched_transport.list_tools.return_value = SimpleNamespace(
        tools=[
            SimpleNamespace(
                name="tap", description="Tap", input_schema={"type": "object"}
            ),
            SimpleNamespace(name="peek", description=None, input_schema={}),
        ]
    )

    async with mcp_tool.McpClient() as c:
        out = await c.list_tools()

    assert out == [
        {"name": "tap", "description": "Tap", "input_schema": {"type": "object"}},
        # None description normalized to empty string.
        {"name": "peek", "description": "", "input_schema": {}},
    ]


@pytest.mark.asyncio
async def test_list_tools_asserts_session_entered() -> None:
    c = mcp_tool.McpClient()

    with pytest.raises(AssertionError, match=r"^McpClient not entered$"):
        await c.list_tools()


# ---------- call_tool ----------


@pytest.mark.parametrize("now", [10.0, 11.0])
async def test_expired_call_does_not_send_to_mcp(
    patched_transport, monkeypatch, now
) -> None:
    monkeypatch.setattr(mcp_tool, "monotonic", lambda: now)

    async with mcp_tool.McpClient() as client:
        with pytest.raises(mcp_tool.ToolCallExpired):
            await client.call_tool("tap", expires_at=10.0)

    patched_transport.call_tool.assert_not_awaited()
    patched_transport.initialize.assert_awaited_once()


@pytest.mark.parametrize("pending_reconnect", [False, True])
async def test_expiry_during_reconnect_prevents_tool_send(
    patched_transport, monkeypatch, pending_reconnect
) -> None:
    clock = [10.0]
    monkeypatch.setattr(mcp_tool, "monotonic", lambda: clock[0])

    async def initialize():
        clock[0] = 11.0
        return SimpleNamespace(instructions="fixture")

    async with mcp_tool.McpClient() as client:
        client._reconnect_pending = pending_reconnect
        patched_transport.initialize.side_effect = initialize
        patched_transport.call_tool.side_effect = MCPError(
            INVALID_REQUEST, "Session not found"
        )

        with pytest.raises(mcp_tool.ToolCallExpired):
            await client.call_tool("tap", expires_at=11.0)

    assert patched_transport.initialize.await_count == 2
    assert patched_transport.call_tool.await_count == (0 if pending_reconnect else 1)


async def test_expiry_does_not_discard_a_started_tools_result(
    patched_transport, monkeypatch
) -> None:
    clock = [10.0]
    monkeypatch.setattr(mcp_tool, "monotonic", lambda: clock[0])

    async def complete(*args, **kwargs):
        clock[0] = 12.0
        await asyncio.sleep(0)
        return SimpleNamespace(
            content=[TextContent(type="text", text="tap completed")],
            is_error=False,
        )

    patched_transport.call_tool.side_effect = complete
    async with mcp_tool.McpClient() as client:
        result = await client.call_tool("tap", {"bbox": [0, 0, 1, 1]}, expires_at=11.0)

    assert result == [{"type": "text", "text": "tap completed"}]
    patched_transport.call_tool.assert_awaited_once_with("tap", {"bbox": [0, 0, 1, 1]})


@pytest.mark.asyncio
async def test_call_tool_normalizes_text_blocks(patched_transport) -> None:
    patched_transport.call_tool.return_value = SimpleNamespace(
        content=[
            TextContent(type="text", text="hello"),
        ],
        is_error=False,
    )

    async with mcp_tool.McpClient() as c:
        out = await c.call_tool("tap")

    assert out == [{"type": "text", "text": "hello"}]


@pytest.mark.asyncio
async def test_call_tool_normalizes_image_blocks(patched_transport) -> None:
    patched_transport.call_tool.return_value = SimpleNamespace(
        content=[
            ImageContent(type="image", mime_type="image/png", data="aGk="),
        ],
        is_error=False,
    )

    async with mcp_tool.McpClient() as c:
        out = await c.call_tool("peek")

    assert out == [{"type": "image", "mime_type": "image/png", "data": "aGk="}]


@pytest.mark.asyncio
async def test_call_tool_image_shaped_duck_falls_to_repr(
    patched_transport,
) -> None:
    # Normalization is type-driven (isinstance over mcp.types), not
    # duck-typed — an image-shaped impostor takes the stringify branch.
    class _ImgBlob:
        type = "image"
        data = "aGk="

        def __repr__(self) -> str:
            return "<img-blob>"

    patched_transport.call_tool.return_value = SimpleNamespace(
        content=[_ImgBlob()],
        is_error=False,
    )

    async with mcp_tool.McpClient() as c:
        out = await c.call_tool("p")

    assert out == [{"type": "text", "text": "<img-blob>"}]


@pytest.mark.asyncio
async def test_call_tool_unknown_block_type_stringified_as_text(
    patched_transport,
) -> None:
    class _Mystery:
        type = "future_resource"

        def __repr__(self) -> str:
            return "<future-thing>"

    patched_transport.call_tool.return_value = SimpleNamespace(
        content=[_Mystery()],
        is_error=False,
    )

    async with mcp_tool.McpClient() as c:
        out = await c.call_tool("p")

    assert out == [{"type": "text", "text": "<future-thing>"}]


@pytest.mark.asyncio
async def test_call_tool_passes_args_to_session(patched_transport) -> None:
    patched_transport.call_tool.return_value = SimpleNamespace(
        content=[],
        is_error=False,
    )

    async with mcp_tool.McpClient() as c:
        await c.call_tool("tap", {"bbox": [0.0, 0.0, 1.0, 1.0]})

    patched_transport.call_tool.assert_awaited_once_with(
        "tap", {"bbox": [0.0, 0.0, 1.0, 1.0]}
    )


@pytest.mark.asyncio
async def test_call_tool_uses_empty_dict_when_args_none(
    patched_transport,
) -> None:
    patched_transport.call_tool.return_value = SimpleNamespace(
        content=[],
        is_error=False,
    )

    async with mcp_tool.McpClient() as c:
        await c.call_tool("peek")

    args = patched_transport.call_tool.await_args.args
    assert args == ("peek", {})


@pytest.mark.asyncio
async def test_call_tool_raises_runtime_error_when_is_error_true(
    patched_transport,
) -> None:
    patched_transport.call_tool.return_value = SimpleNamespace(
        content=[
            TextContent(type="text", text="bad arg"),
            TextContent(type="text", text="bbox missing"),
        ],
        is_error=True,
    )

    async with mcp_tool.McpClient() as c:
        with pytest.raises(
            RuntimeError, match=r"^tool 'tap' failed: bad arg \| bbox missing$"
        ):
            await c.call_tool("tap")


@pytest.mark.asyncio
async def test_call_tool_asserts_session_entered() -> None:
    c = mcp_tool.McpClient()

    with pytest.raises(AssertionError, match=r"^McpClient not entered$"):
        await c.call_tool("any")


# ---------- get_mcp / list_tools_cached / close_mcp ----------


@pytest.mark.asyncio
async def test_get_mcp_creates_singleton_on_first_call(
    patched_transport,
) -> None:
    c1 = await mcp_tool.get_mcp()
    c2 = await mcp_tool.get_mcp()

    assert c1 is c2
    assert mcp_tool._mcp is c1
    assert mcp_tool._stack is not None


@pytest.mark.asyncio
async def test_get_mcp_cleans_up_stack_on_aenter_failure(mocker) -> None:
    @asynccontextmanager
    async def boom_http(url: str, *, http_client=None):
        raise RuntimeError("transport failed")
        yield None  # pragma: no cover

    mocker.patch.object(mcp_tool, "streamable_http_client", boom_http)
    mocker.patch.object(mcp_tool.McpClient, "_probe", _no_probe)

    with pytest.raises(ConnectionError, match="transport failed"):
        await mcp_tool.get_mcp()

    # Stack must NOT have been retained.
    assert mcp_tool._stack is None
    assert mcp_tool._mcp is None


@pytest.mark.asyncio
async def test_list_tools_cached_caches_first_result(
    patched_transport,
) -> None:
    patched_transport.list_tools.return_value = SimpleNamespace(
        tools=[
            SimpleNamespace(name="tap", description="Tap", input_schema={}),
        ]
    )

    a = await mcp_tool.list_tools_cached()
    b = await mcp_tool.list_tools_cached()

    assert a is b
    # Server only contacted once for the list.
    patched_transport.list_tools.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_mcp_no_op_when_singleton_unset() -> None:
    # No prior get_mcp call — close_mcp should be safe.
    await mcp_tool.close_mcp()


@pytest.mark.asyncio
async def test_close_mcp_clears_singleton_state(patched_transport) -> None:
    await mcp_tool.get_mcp()
    assert mcp_tool._stack is not None

    await mcp_tool.close_mcp()

    assert mcp_tool._stack is None
    assert mcp_tool._mcp is None
    assert mcp_tool._tools_cache is None


@pytest.mark.asyncio
async def test_close_mcp_logs_warning_on_aclose_exception(
    patched_transport, mocker, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    await mcp_tool.get_mcp()
    # Replace the stack with one that raises on aclose.
    bad_stack = mocker.MagicMock()
    bad_stack.aclose = mocker.AsyncMock(side_effect=RuntimeError("nope"))
    mcp_tool._stack = bad_stack

    with caplog.at_level(logging.WARNING, logger="physiclaw.agent.engine.mcp_tool"):
        await mcp_tool.close_mcp()

    assert any(
        r.getMessage().startswith("MCP client close failed") for r in caplog.records
    )


# ---------- connect failures are legible ----------


@pytest.mark.asyncio
async def test_probe_raises_connection_error_when_nothing_is_listening() -> None:
    # A refused connection inside `streamable_http_client` collapses its
    # anyio scope into a bare CancelledError — a BaseException no
    # `except Exception` can catch, which is why the CLI's "cannot reach the
    # server" message never fired and `dispatch` could break its "never
    # raises" contract. Answer the question before the transport does.
    client = mcp_tool.McpClient("http://127.0.0.1:1")

    with pytest.raises(ConnectionError, match="cannot reach the MCP server"):
        await client._probe()


@pytest.mark.asyncio
async def test_probe_passes_when_a_socket_is_listening() -> None:
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        await mcp_tool.McpClient(f"http://127.0.0.1:{port}")._probe()
    finally:
        server.close()
        await server.wait_closed()


def test_probe_targets_the_url_host_and_port() -> None:
    # A raw socket, not an HTTP request: `trust_env` is on, so a configured
    # system proxy answers an HTTP pre-flight itself and returns 502 for a
    # dead upstream — which reads as "reachable".
    assert mcp_tool.McpClient("http://127.0.0.1:8899")._host_port() == (
        "127.0.0.1",
        8899,
    )
    assert mcp_tool.McpClient("https://rig.local")._host_port() == ("rig.local", 443)
    assert mcp_tool.McpClient("http://rig.local")._host_port() == ("rig.local", 80)


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["Session not found", "Session terminated"])
async def test_call_tool_reconnects_once_when_the_server_lost_the_session(
    patched_transport,
    message,
) -> None:
    # A runtime that outlived its server keeps a session id the new
    # server never issued: the first call fails "Session not found". One
    # fresh handshake, then the same call — instead of every tool call
    # of the wake failing the same way.
    ok = SimpleNamespace(
        content=[TextContent(type="text", text="hello")], is_error=False
    )
    patched_transport.call_tool.side_effect = [MCPError(INVALID_REQUEST, message), ok]

    async with mcp_tool.McpClient() as c:
        out = await c.call_tool("peek")

    assert out == [{"type": "text", "text": "hello"}]
    assert patched_transport.initialize.await_count == 2  # the handshake ran again
    assert patched_transport.call_tool.await_count == 2


@pytest.mark.asyncio
async def test_call_tool_other_errors_are_not_retried(patched_transport) -> None:
    patched_transport.call_tool.side_effect = Exception("boom")

    async with mcp_tool.McpClient() as c:
        with pytest.raises(Exception, match="boom"):
            await c.call_tool("peek")

    assert patched_transport.call_tool.await_count == 1


@pytest.mark.asyncio
async def test_list_tools_reconnects_once_when_the_server_lost_the_session(
    patched_transport,
) -> None:
    # The wake's first session call is the tool listing; a lost session
    # shows there first, and the same one-handshake repair covers it.
    tools = SimpleNamespace(tools=[])
    patched_transport.list_tools.side_effect = [
        MCPError(INVALID_REQUEST, "Session not found"),
        tools,
    ]

    async with mcp_tool.McpClient() as c:
        out = await c.list_tools()

    assert out == []
    assert patched_transport.initialize.await_count == 2
    assert patched_transport.list_tools.await_count == 2


async def test_regression_cancelled_handshake_releases_all_opened_resources(
    patched_transport,
):
    client = mcp_tool.McpClient()

    async def cancel_initialize():
        asyncio.current_task().cancel("handshake cancelled")
        await asyncio.sleep(0)

    patched_transport.initialize.side_effect = cancel_initialize

    with pytest.raises(asyncio.CancelledError, match="handshake cancelled"):
        await asyncio.create_task(client.__aenter__())

    assert patched_transport.captured.get("session_closed") is True
    assert patched_transport.captured.get("transport_closed") is True
    assert patched_transport.captured["http_client"].is_closed


async def test_regression_failed_handshake_cannot_use_partially_initialized_session(
    patched_transport,
):
    client = mcp_tool.McpClient()
    patched_transport.initialize.side_effect = RuntimeError("bad handshake")

    with pytest.raises(ConnectionError, match="bad handshake"):
        await client.__aenter__()

    with pytest.raises(AssertionError, match="McpClient not entered"):
        await client.list_tools()
    patched_transport.list_tools.assert_not_awaited()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (RuntimeError("handshake failed"), ConnectionError),
        (asyncio.CancelledError("handshake failed"), asyncio.CancelledError),
    ],
)
@pytest.mark.parametrize(
    "close_error",
    [OSError("cleanup failed"), asyncio.CancelledError("cleanup cancelled")],
)
async def test_failed_connect_cleanup_error_preserves_original_failure(
    patched_transport,
    failure,
    expected,
    close_error,
):
    patched_transport.initialize.side_effect = failure
    patched_transport.aclose.side_effect = close_error

    with pytest.raises(expected, match="handshake failed"):
        await mcp_tool.get_mcp()

    assert patched_transport.captured["http_client"].is_closed
    assert patched_transport.captured["transport_closed"] is True
    assert mcp_tool._mcp is None
    assert mcp_tool._stack is None


async def test_reconnect_retries_rejected_request_only_once(patched_transport):
    patched_transport.call_tool.side_effect = MCPError(
        INVALID_REQUEST, "Session not found"
    )

    async with mcp_tool.McpClient() as client:
        with pytest.raises(MCPError, match="Session not found"):
            await client.call_tool("tap")

    assert patched_transport.initialize.await_count == 2
    assert patched_transport.call_tool.await_count == 2


async def test_reconnect_keeps_replacement_session_for_later_calls(patched_transport):
    ok = SimpleNamespace(content=[], is_error=False)
    patched_transport.call_tool.side_effect = [
        MCPError(INVALID_REQUEST, "Session not found"),
        ok,
        ok,
    ]

    async with mcp_tool.McpClient() as client:
        await client.call_tool("peek")
        await client.call_tool("peek")

    assert patched_transport.initialize.await_count == 2
    assert patched_transport.call_tool.await_count == 3


async def test_explicit_close_after_failed_reconnect_disables_automatic_reopen(
    patched_transport,
):
    patched_transport.call_tool.side_effect = MCPError(
        INVALID_REQUEST, "Session not found"
    )
    patched_transport.initialize.side_effect = [
        SimpleNamespace(instructions="hello"),
        OSError("offline"),
    ]

    async with mcp_tool.McpClient() as client:
        with pytest.raises(ConnectionError, match="offline"):
            await client.call_tool("peek")

    with pytest.raises(AssertionError, match="McpClient not entered"):
        await client.call_tool("peek")
    assert patched_transport.initialize.await_count == 2
    assert client.server_instructions == ""
    assert patched_transport.captured["http_client"].is_closed


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("Invalid structured content: Session not found"),
        MCPError(INTERNAL_ERROR, "Session not found"),
        MCPError(INVALID_REQUEST, "tool failed: Session not found"),
    ],
)
async def test_regression_tool_error_text_does_not_replay_an_executed_action(
    patched_transport,
    error,
):
    patched_transport.call_tool.side_effect = error

    async with mcp_tool.McpClient() as client:
        with pytest.raises(type(error)) as raised:
            await client.call_tool("tap")

    assert raised.value is error
    patched_transport.call_tool.assert_awaited_once()
    patched_transport.initialize.assert_awaited_once()


async def test_regression_next_call_recovers_after_reconnect_handshake_failed(
    patched_transport,
):
    ok = SimpleNamespace(
        content=[TextContent(type="text", text="ready")], is_error=False
    )
    patched_transport.call_tool.side_effect = [
        MCPError(INVALID_REQUEST, "Session not found"),
        ok,
    ]
    initialized = SimpleNamespace(instructions="fixture")
    patched_transport.initialize.side_effect = [
        initialized,
        OSError("server offline"),
        initialized,
    ]

    async with mcp_tool.McpClient() as client:
        with pytest.raises(ConnectionError, match="server offline"):
            await client.call_tool("peek")
        result = await client.call_tool("peek")

    assert result == [{"type": "text", "text": "ready"}]
    assert patched_transport.initialize.await_count == 3
    assert patched_transport.call_tool.await_count == 2


async def test_regression_cancellation_during_reconnect_close_does_not_replay(
    patched_transport,
):
    patched_transport.call_tool.side_effect = [
        MCPError(INVALID_REQUEST, "Session not found"),
        SimpleNamespace(content=[], is_error=False),
    ]

    async with mcp_tool.McpClient() as client:
        patched_transport.aclose.side_effect = asyncio.CancelledError("stop")
        with pytest.raises(asyncio.CancelledError, match="stop"):
            await client.call_tool("peek")
        patched_transport.aclose.side_effect = None

    patched_transport.initialize.assert_awaited_once()
    patched_transport.call_tool.assert_awaited_once()


@pytest.mark.parametrize(
    "error", [OSError("close failed"), asyncio.CancelledError("stop")]
)
async def test_regression_close_failure_clears_live_session(
    patched_transport,
    error,
):
    client = await mcp_tool.McpClient().__aenter__()
    patched_transport.aclose.side_effect = error

    with pytest.raises(type(error)):
        await client.__aexit__(None, None, None)

    with pytest.raises(AssertionError, match="McpClient not entered"):
        await client.list_tools()
    patched_transport.list_tools.assert_not_awaited()


async def test_stop_during_expired_session_does_not_reconnect_or_replay(
    patched_transport,
):
    stopping = asyncio.Event()

    async def rejected(*args, **kwargs):
        stopping.set()
        raise MCPError(INVALID_REQUEST, "Session not found")

    token = stop_event.set(stopping)
    try:
        async with mcp_tool.McpClient() as client:
            patched_transport.call_tool.side_effect = rejected
            with pytest.raises(asyncio.CancelledError, match="stop requested"):
                await client.call_tool("tap")
    finally:
        stop_event.reset(token)
    patched_transport.initialize.assert_awaited_once()
    patched_transport.call_tool.assert_awaited_once()


async def test_stop_during_pending_reconnect_does_not_send_tool(patched_transport):
    stopping = asyncio.Event()

    async def initialize():
        stopping.set()
        return SimpleNamespace(instructions="fixture")

    token = stop_event.set(stopping)
    try:
        async with mcp_tool.McpClient() as client:
            patched_transport.initialize.side_effect = initialize
            client._reconnect_pending = True
            with pytest.raises(asyncio.CancelledError, match="stop requested"):
                await client.call_tool("tap")
    finally:
        stop_event.reset(token)
    assert patched_transport.initialize.await_count == 2
    patched_transport.call_tool.assert_not_awaited()
