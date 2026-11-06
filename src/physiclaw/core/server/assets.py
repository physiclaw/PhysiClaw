"""Serve explicitly bundled shared assets on the control listener."""

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import FileResponse

from physiclaw.common.assets import QR_CODE_JS

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer


def register(mcp: "MCPServer") -> None:
    @mcp.custom_route("/assets/qrcode-1.4.4.min.js", methods=["GET"])
    async def qrcode(request: Request) -> FileResponse:
        return FileResponse(
            QR_CODE_JS,
            media_type="text/javascript",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )
