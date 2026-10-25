"""Tests for `physiclaw.core.server.planes` — the two-plane split.

The gate is a plain ASGI wrapper, so tests drive it with hand-built
scopes around a dummy inner app — no sockets, no Starlette routing.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from physiclaw.core.server.planes import ControlGate, PhoneApp, build_bridge_app


async def _inner_ok(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _call(
    app,
    path: str = "/api/x",
    *,
    headers: dict[str, str] | list[tuple[str, str]] | None = None,
    client: tuple[str, int] | None = ("192.168.1.5", 40000),
    method: str = "GET",
    scheme: str = "http",
) -> int:
    sent: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            sent["status"] = msg["status"]

    header_items = headers.items() if isinstance(headers, dict) else headers or []
    scope = {
        "type": "http",
        "method": method,
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in header_items],
        "client": client,
        "server": ("127.0.0.1", 8048),
    }
    await app(scope, receive, send)
    return sent["status"]


# ---------- ControlGate (loopback plane, Host allowlist) ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["localhost:8048", "127.0.0.1:8048", "[::1]:8048"])
async def test_control_gate_allows_local_hosts(host: str) -> None:
    gate = ControlGate(_inner_ok)

    assert await _call(gate, headers={"host": host}) == 200


@pytest.mark.asyncio
async def test_control_gate_rejects_foreign_host() -> None:
    # DNS rebinding: the attacker's page reaches 127.0.0.1 but its Host
    # header still says evil.com — the gate must refuse before routing.
    gate = ControlGate(_inner_ok)

    assert await _call(gate, headers={"host": "evil.example:8048"}) == 421


@pytest.mark.asyncio
async def test_control_gate_rejects_missing_host() -> None:
    gate = ControlGate(_inner_ok)

    assert await _call(gate) == 421


@pytest.mark.asyncio
async def test_control_gate_admits_configured_bind_host() -> None:
    gate = ControlGate(_inner_ok, host="10.0.0.9")

    assert await _call(gate, headers={"host": "10.0.0.9:8048"}) == 200


@pytest.mark.parametrize(
    ("bind", "header"),
    [("2001:db8::1", "[2001:db8::1]:8048"), ("device.local", "DEVICE.LOCAL:8048")],
)
async def test_regression_control_gate_accepts_configured_host_spelling(bind, header):
    gate = ControlGate(_inner_ok, host=bind)

    result = await _call(gate, headers={"host": header})

    assert result == 200


@settings(max_examples=25, derandomize=True)
@given(address=st.ip_addresses(v=6))
async def test_control_gate_equivalent_ipv6_spellings_are_accepted(address):
    gate = ControlGate(_inner_ok, host=address.exploded)

    result = await _call(gate, headers={"host": f"[{address.compressed}]:8048"})

    assert result == 200


@pytest.mark.asyncio
async def test_control_gate_concrete_bind_still_rejects_foreign_host() -> None:
    gate = ControlGate(_inner_ok, host="10.0.0.9")

    assert await _call(gate, headers={"host": "evil.example:8048"}) == 421


@pytest.mark.asyncio
@pytest.mark.parametrize("bind", ["0.0.0.0", "::"])
async def test_control_gate_stands_down_on_wildcard_bind(bind: str) -> None:
    # A wildcard bind is the user's explicit exposure opt-in; clients send
    # Host: <interface-ip>, which can't be allowlisted — gating would 421
    # every real client.
    gate = ControlGate(_inner_ok, host=bind)

    assert await _call(gate, headers={"host": "192.168.1.7:8048"}) == 200


@pytest.mark.parametrize("bind", ["127.0.0.1", "0.0.0.0", "::"])
@pytest.mark.parametrize("method", ["GET", "POST", "OPTIONS"])
async def test_control_gate_blocks_cross_origin_before_routing(bind, method):
    gate = ControlGate(_inner_ok, host=bind)

    assert (
        await _call(
            gate,
            path="/api/connect-arm",
            method=method,
            headers={"host": "127.0.0.1:8048", "origin": "https://attacker.example"},
        )
        == 403
    )


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "",
        "http://127.0.0.1:8049",
        "https://127.0.0.1:8048",
        "http://127.0.0.1:8048/",
        "http://127.0.0.1:8048?x",
        "http://127.0.0.1:8048#x",
        "http://user@127.0.0.1:8048",
        "http://127.0.0.1:8048 https://attacker.example",
    ],
)
async def test_control_gate_rejects_invalid_or_different_origin(origin):
    assert (
        await _call(
            ControlGate(_inner_ok),
            headers={"host": "127.0.0.1:8048", "origin": origin},
        )
        == 403
    )


@pytest.mark.parametrize(
    ("host", "origin", "scheme"),
    [
        ("LOCALHOST:8048", "http://localhost:8048", "http"),
        ("[::1]:8048", "http://[0:0:0:0:0:0:0:1]:8048", "http"),
        ("127.0.0.1", "http://127.0.0.1:80", "http"),
        ("localhost:443", "https://localhost", "https"),
    ],
)
async def test_control_gate_accepts_same_origin(host, origin, scheme):
    assert (
        await _call(
            ControlGate(_inner_ok),
            method="POST",
            scheme=scheme,
            headers={"host": host, "origin": origin, "sec-fetch-site": "same-origin"},
        )
        == 200
    )


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
async def test_control_gate_blocks_foreign_browser_get_without_origin(site):
    assert (
        await _call(
            ControlGate(_inner_ok),
            headers={"host": "127.0.0.1:8048", "sec-fetch-site": site},
        )
        == 403
    )


@pytest.mark.parametrize(
    "host",
    [
        "[::1]evil:8048",
        "[::1",
        "localhost:",
        "localhost:99999",
        "localhost:0",
        "localhost:abc",
        "user@localhost",
        "localhost/path",
    ],
)
async def test_control_gate_rejects_malformed_authorities(host):
    assert await _call(ControlGate(_inner_ok), headers={"host": host}) == 421


@pytest.mark.parametrize(
    ("name", "value", "status"),
    [("host", "localhost:8048", 421), ("origin", "http://localhost:8048", 403)],
)
async def test_control_gate_rejects_duplicate_boundary_headers(name, value, status):
    headers = [
        ("host", "localhost:8048"),
        ("origin", "http://localhost:8048"),
        (name, value),
    ]
    assert await _call(ControlGate(_inner_ok), headers=headers) == status


# ---------- PhoneApp ----------


def test_phone_app_refuses_non_phone_route() -> None:
    """A control route (arm-driving, setup, calibrate) registered on the
    LAN plane must fail at assembly time, not silently expose."""
    app = PhoneApp()

    with pytest.raises(RuntimeError, match="refusing to expose"):
        app.custom_route("/api/calibrate/arm", methods=["POST"])


def test_phone_app_collects_routes_and_builds_starlette() -> None:
    app = PhoneApp()

    @app.custom_route("/bridge", methods=["GET"])
    async def _page(request):  # noqa: ARG001
        raise NotImplementedError

    built = app.build()
    assert [r.path for r in built.routes] == ["/bridge"]


def test_build_bridge_app_is_the_plain_phone_app() -> None:
    # The LAN plane retains its trusted-network model; upload guards
    # live in the handler (see planes.py module doc).
    from starlette.applications import Starlette

    assert isinstance(build_bridge_app(PhoneApp()), Starlette)


def test_build_control_app_hands_the_bind_host_to_the_sdk() -> None:
    """`streamable_http_app(host=...)` decides the SDK's own Host
    allowlist on `/mcp`: a loopback host installs a loopback-only list,
    anything else installs none. Left at the SDK default (127.0.0.1), a
    `--host 0.0.0.0` server would 421 every LAN client on `/mcp` while
    the ControlGate in front of it stood down."""
    from unittest.mock import MagicMock

    from physiclaw.core.server.planes import build_control_app

    mcp = MagicMock()
    gate = build_control_app(mcp, host="0.0.0.0")

    mcp.streamable_http_app.assert_called_once_with(host="0.0.0.0")
    assert isinstance(gate, ControlGate)
    assert gate.app is mcp.streamable_http_app.return_value
