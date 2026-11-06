"""Setup and the independent AssistiveTouch service share one offline QR asset."""

import httpx
from starlette.applications import Starlette
from starlette.routing import Route

from physiclaw.cli._assistive_touch_http import QR_FILE
from physiclaw.common.assets import QR_CODE_JS
from physiclaw.core.server import assets
from physiclaw.core.setup.pages import STATIC_DIR


async def test_qr_asset_route_serves_the_shared_vendored_bytes(fake_mcp):
    assets.register(fake_mcp)
    app = Starlette(
        routes=[
            Route(path, endpoint=handler, methods=methods)
            for path, methods, handler in fake_mcp.routes
        ]
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/assets/qrcode-1.4.4.min.js")

    assert QR_FILE == QR_CODE_JS
    assert response.content == QR_CODE_JS.read_bytes()
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_setup_pages_use_the_local_shared_qr_url():
    wizard = (STATIC_DIR / "setup-hardware.html").read_text(encoding="utf-8")
    qr = (STATIC_DIR / "qr.html").read_text(encoding="utf-8")

    assert '<script src="/assets/qrcode-1.4.4.min.js"></script>' in wizard
    assert '<script src="/assets/qrcode-1.4.4.min.js"></script>' in qr
    assert "cdn.jsdelivr.net" not in wizard + qr
