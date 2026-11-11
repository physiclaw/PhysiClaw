"""Setup-owned desktop and phone pages, including bridge URL substitution."""

from starlette.requests import Request
from starlette.responses import HTMLResponse

from physiclaw.common import assets, runtime_state
from physiclaw.common.text import read_text
from physiclaw.core.bridge.lan import bridge_base_urls

STATIC_DIR = assets.STATIC_DIR / "setup"


def render_phone_page_html(filename: str, port: int) -> str:
    """Substitute LAN bridge URLs using the page's control-listener port."""
    primary, fallback = bridge_base_urls(port)
    return (
        read_text(STATIC_DIR / filename)
        .replace("__PHONE_URL__", f"{primary}/bridge")
        .replace("__PHONE_URL_FALLBACK__", f"{fallback}/bridge")
    )


async def serve_bridge_page(request: Request) -> HTMLResponse:
    """Serve the phone UI without caching phases from an older build."""
    return HTMLResponse(
        read_text(STATIC_DIR / "bridge.html"),
        headers={"Cache-Control": "no-store"},
    )


async def serve_qr_page(request: Request) -> HTMLResponse:
    """Serve the phone connection page with its local shared QR script."""
    return HTMLResponse(
        render_phone_page_html("qr.html", request.url.port or 8048),
        headers={"Cache-Control": "no-store"},
    )


async def handle_setup_page(request: Request) -> HTMLResponse:
    """Serve the wizard driving the same server endpoints as the CLI."""
    state = runtime_state.read_live() or {}
    return HTMLResponse(
        render_phone_page_html("setup-hardware.html", request.url.port or 8048).replace(
            "__NO_RUNTIME__", "true" if state.get("no_runtime") else "false"
        ),
        headers={"Cache-Control": "no-store"},
    )
