"""Sourcing document sources and rendering."""

from pathlib import Path

# Markets are distinct from presentation languages; preserve the current mapping.
_SOURCE_DIR = Path(__file__).resolve().parent
VENDOR_FILES = {
    "zh": _SOURCE_DIR / "vendors" / "cn.json",
    "en": _SOURCE_DIR / "vendors" / "global.json",
}
