"""Shared bundled assets; HTTP adapters choose how to serve the same files."""

from pathlib import Path

QR_CODE_JS = Path(__file__).with_name("static") / "vendor" / "qrcode-1.4.4.min.js"
