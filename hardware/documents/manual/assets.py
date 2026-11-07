"""Image-asset strategies for the manual build.

Resolve an SVG name to an ``<img src>`` and emit whatever files the chosen
build mode needs. Images stay ``<img>`` (not inline ``<svg>``) so the figure
framing can rely on object-fit / object-position.
"""

from __future__ import annotations

import base64
import shutil
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Protocol

from hardware.assembly.travel_ranges import ILLUSTRATION_TRAVEL, X_TRAVEL, Y_TRAVEL
from hardware.documents import BuildError
from hardware.documents.assets import figure, icon
from hardware.scheme import OUTPUT_DIR, SVG_DIR, SVG_NAME_RE

# Never fall back to resized manufacturing renders: their crops and annotation
# coordinates no longer match the authored manual layout.
if (X_TRAVEL, Y_TRAVEL) != ILLUSTRATION_TRAVEL:
    SVG_DIR = OUTPUT_DIR / "manual-illustrations" / "svg"

ASSETS_SUBDIR = "assets"  # where external mode writes images, relative to the HTML.

# Authored figures copied into the manual's render pool at build time.
HAND_FIGURES = {"wire_splice.svg": lambda: figure.wire_splice_svg}


def _render_stem(basename: str) -> str:
    """The procedure stem behind a render filename, e.g.
    ``camera_40_frame_assembled_cam0_annotated_wiring_connections.svg`` -> ``camera_40_frame``.
    A name that isn't a render (e.g. a hand figure) passes through whole."""
    m = SVG_NAME_RE.match(basename)
    return m["stem"] if m else basename


def _require_renders(names) -> None:
    """Raise a clear ``BuildError`` if any referenced SVG render is absent from
    the selected render pool. Those files come from the assembly pipeline (not this
    package), so the message names exactly what's missing and the command that
    regenerates precisely those stems."""
    missing = sorted(n for n in names if not (SVG_DIR / n).exists())
    if not missing:
        return
    stems = sorted({_render_stem(n) for n in missing})
    listed = "\n".join(f"    {n}" for n in missing)
    raise BuildError(
        f"{len(missing)} referenced SVG render(s) missing from {SVG_DIR}:\n"
        f"{listed}\n\n"
        "These are produced by the assembly render pipeline.\n"
        "Regenerate them with:\n"
        "    uv run --group cad python -m hardware build"
        + (
            f"\nAffected stems: {' '.join(stems)}"
            if (X_TRAVEL, Y_TRAVEL) != ILLUSTRATION_TRAVEL
            else f" --stems {' '.join(stems)}"
        )
    )


@cache
def _read_svg(basename: str) -> str:
    """Read one SVG from the selected pool (cached — several are reused)."""
    _require_renders([basename])
    return (SVG_DIR / basename).read_text(encoding="utf-8")


def prepare_figures() -> None:
    """Start a new figure cache and copy the current authored SVGs into the pool."""
    _read_svg.cache_clear()
    SVG_DIR.mkdir(parents=True, exist_ok=True)
    for name, read_svg in HAND_FIGURES.items():
        (SVG_DIR / name).write_text(read_svg(), encoding="utf-8")


def _data_uri(svg_text: str) -> str:
    """Encode SVG markup as a base64 ``data:image/svg+xml`` URI."""
    b64 = base64.b64encode(svg_text.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


class Assets(Protocol):
    """Maps image references to ``<img src>`` values for a build mode."""

    def figure(self, basename: str) -> str: ...
    @property
    def crab(self) -> str: ...
    def preload(self, src: str) -> str: ...  # optional <head> preload for src
    def emit(self, out_dir: Path) -> None: ...  # write any external files


@dataclass
class InlineAssets:
    """Embed every image directly in the HTML as a ``data:`` URI."""

    def figure(self, basename: str) -> str:
        return _data_uri(_read_svg(basename))

    @property
    def crab(self) -> str:
        return _data_uri(icon.crab_svg)

    def preload(self, src: str) -> str:
        return ""  # nothing to preload — the bytes are already in the document.

    def emit(self, out_dir: Path) -> None:
        pass  # self-contained: no sidecar files.


@dataclass
class ExternalAssets:
    """Write images as sidecar files and reference them with relative URLs."""

    referenced: set[str] = field(default_factory=set)

    def figure(self, basename: str) -> str:
        self.referenced.add(basename)
        return f"{ASSETS_SUBDIR}/{basename}"

    @property
    def crab(self) -> str:
        return f"{ASSETS_SUBDIR}/crab.svg"

    def preload(self, src: str) -> str:
        return f'<link rel="preload" as="image" href="{src}">'

    def emit(self, out_dir: Path) -> None:
        assets_dir = out_dir / ASSETS_SUBDIR
        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / "crab.svg").write_text(icon.crab_svg, encoding="utf-8")
        _require_renders(self.referenced)  # all missing at once, with a fix hint
        for name in sorted(self.referenced):
            shutil.copyfile(SVG_DIR / name, assets_dir / name)
