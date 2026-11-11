"""Bundled browser assets, resolved relative to the installed package.

Pages live in feature directories under ``physiclaw/static``; shared libraries
and their licenses live in ``static/vendor``. HTTP adapters own their routes.
"""

from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
QR_CODE_JS = STATIC_DIR / "vendor" / "qrcode-1.4.4.min.js"
