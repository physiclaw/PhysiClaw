"""Host and browser-origin boundary for local ASGI control applications.

Native clients may omit browser headers. Browser requests must use the same
origin, and literal loopback Host names prevent DNS rebinding by foreign pages.
"""

import ipaddress
import re

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from physiclaw.common.config import WILDCARD_HOSTS
from physiclaw.common.loopback import LOOPBACK_HOST_HEADERS


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
