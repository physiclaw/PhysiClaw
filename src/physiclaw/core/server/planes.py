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

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.routing import Route

from physiclaw.common.control_gate import ControlGate as ControlGate

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


def build_control_app(mcp: "MCPServer", host: str = "127.0.0.1") -> ControlGate:
    """The loopback ASGI app: MCPServer's Starlette app (``/mcp`` + every
    ``custom_route``) behind the ControlGate."""
    return ControlGate(mcp.streamable_http_app(host=host), host)


def build_bridge_app(phone_app: PhoneApp) -> Starlette:
    """The LAN ASGI app: just the collected phone routes."""
    return phone_app.build()
