"""Two ASGI planes, one process — the bind split that isolates the arm.

The control plane (``/mcp`` + every setup/calibrate/watch route) binds
``127.0.0.1`` by default, keeping arm control off the LAN. The phone
bridge (the handful of routes Safari and the iOS
Shortcuts touch) binds ``0.0.0.0`` on the next port up.

Neither plane uses credentials. The phone plane assumes a trusted LAN:
its clipboard and calibration state are available to connected clients.
``PHONE_PLANE_PATHS`` keeps arm-control routes off that listener. Upload
and phase guards reject unsolicited or stale data; they do not authenticate
the sender. An explicit wildcard control bind also exposes control to LAN
clients, while retaining the browser same-origin check.

MCPServer's own ``transport_security`` covers only the ``/mcp`` route —
``@mcp.custom_route`` routes are explicitly exempt from SDK auth.
``ControlGate`` therefore protects every control route: the Host allowlist
blocks DNS rebinding, and same-origin checks block browser requests from
foreign pages. Native CLI/MCP clients may omit browser origin headers.
"""

import ipaddress
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from physiclaw.common.config import WILDCARD_HOSTS
from physiclaw.common.loopback import LOOPBACK_HOST_HEADERS

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

log = logging.getLogger(__name__)


# The complete set of LAN-exposed paths — the phone page's XHRs and the
# two iOS Shortcut endpoints, nothing else. ``PhoneApp.custom_route``
# REFUSES any path not listed here, so a copy-paste error that would
# put a control route (arm-driving, setup, calibrate) on the LAN plane
# fails at assembly time instead of silently exposing it. Adding a
# phone route is a deliberate two-step: register it AND list it here.
PHONE_PLANE_PATHS = frozenset(
    {
        "/bridge",
        "/api/bridge/state",
        "/api/bridge/tapped",
        "/api/bridge/screen-dimension",
        "/api/bridge/screenshot",
        "/api/bridge/clipboard",
        "/api/bridge/touch",
    }
)


class PhoneApp:
    """Route collector with MCPServer's ``custom_route`` decorator shape.

    Lets ``register_phone`` in each feature module read identically to its
    control-plane sibling; ``build()`` turns the collected routes into the
    LAN-facing Starlette app. Registration is gated by
    ``PHONE_PLANE_PATHS`` — see that constant for why.
    """

    def __init__(self) -> None:
        self._routes: list[Route] = []

    def custom_route(self, path: str, methods: list[str]) -> Callable:
        if path not in PHONE_PLANE_PATHS:
            raise RuntimeError(
                f"refusing to expose {path!r} on the LAN phone plane — "
                "phone routes must be listed in planes.PHONE_PLANE_PATHS"
            )

        def deco(fn: Callable) -> Callable:
            self._routes.append(Route(path, endpoint=fn, methods=methods))
            return fn

        return deco

    def build(self) -> Starlette:
        return Starlette(routes=self._routes)


def _normalize_host(host: str) -> str:
    """Compare literal IPs canonically and DNS names without case sensitivity."""
    try:
        return str(ipaddress.ip_address(host.removeprefix("[").removesuffix("]")))
    except ValueError:
        return host.lower()


def _authority(value: str, scheme: str) -> tuple[str, str, int] | None:
    """Parse an HTTP authority into a comparable (scheme, host, port).

    Host and Origin share this parser. Reject credentials, paths, ambiguous
    IPv6, and malformed ports instead of stripping untrusted suffixes.
    """
    if scheme not in ("http", "https"):
        return None
    match = re.fullmatch(
        r"(\[[0-9a-fA-F:.]+\]|[a-zA-Z0-9.-]+)(?::([0-9]{1,5}))?", value
    )
    if match is None:
        return None
    host, port_text = match.groups()
    if host.startswith("["):
        try:
            host = str(ipaddress.IPv6Address(host[1:-1]))
        except ValueError:
            return None
    port = (
        int(port_text) if port_text is not None else (443 if scheme == "https" else 80)
    )
    if not 0 < port <= 65535:
        return None
    return scheme, _normalize_host(host), port


class ControlGate:
    """Control-plane Host allowlist and browser same-origin boundary.

    ``host`` is the bind address — admitted as a Host value so a user
    exposing the control plane via a non-default ``--host`` still passes.
    """

    def __init__(self, app: ASGIApp, host: str = "127.0.0.1") -> None:
        self.app = app
        # A wildcard bind is the user's explicit opt-in to LAN exposure:
        # clients then send Host: <whichever interface IP they dialed>,
        # which can't be enumerated here. Skip the Host allowlist for this
        # bind, while still checking authority syntax and browser origin.
        self._wildcard = host in WILDCARD_HOSTS
        self._allowed = {_normalize_host(h) for h in LOOPBACK_HOST_HEADERS | {host}}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        target = (
            _authority(hosts[0], scope.get("scheme", "http"))
            if len(hosts) == 1
            else None
        )
        if target is None or (not self._wildcard and target[1] not in self._allowed):
            reject = JSONResponse({"error": "unrecognized Host"}, status_code=421)
            return await reject(scope, receive, send)
        origins = headers.getlist("origin")
        same_origin = True
        if origins:
            scheme, _, authority = origins[0].partition("://")
            same_origin = len(origins) == 1 and _authority(authority, scheme) == target
        # Browser GETs can omit Origin. They still must not invoke control
        # routes from a foreign page; native clients omit Fetch Metadata too.
        if not same_origin or headers.get("sec-fetch-site", "none") not in (
            "same-origin",
            "none",
        ):
            reject = JSONResponse({"error": "cross-origin request"}, status_code=403)
            return await reject(scope, receive, send)
        return await self.app(scope, receive, send)


def build_control_app(mcp: "MCPServer", host: str = "127.0.0.1") -> ControlGate:
    """The loopback ASGI app: MCPServer's Starlette app (``/mcp`` + every
    ``custom_route``) behind the ControlGate."""
    return ControlGate(mcp.streamable_http_app(host=host), host)


def build_bridge_app(phone_app: PhoneApp) -> Starlette:
    """The LAN ASGI app: just the collected phone routes."""
    return phone_app.build()
