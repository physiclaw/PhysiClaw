"""The hardware pipeline's shared naming scheme — output directories,
variant names, procedure-stem convention, and the output-filename builders
with their matching regexes.

Single source of truth: every stage (parts export, assembly render, build
cache, dispatcher, annotation generator) imports its paths and filename logic from
here, so a producer (``svg_path_for``) and its verifiers (``raw_svg_re``,
the cache's layer membership) cannot drift apart.

Deliberately stdlib-only — no build123d import — so the cache/dispatch
layer can decide hit/miss and classify filenames without loading the CAD
kernel.
"""

from __future__ import annotations

import functools
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_HW = REPO_ROOT / "hardware"
# Release preparation gives every subprocess a fresh, private output tree.
# Interactive builds retain the usual location.
OUTPUT_DIR = Path(os.environ.get("PHYSICLAW_HW_OUTPUT", _HW / "output")).resolve()
STEP_DIR = OUTPUT_DIR / "step"
SVG_DIR = OUTPUT_DIR / "svg"
ANNOTATION_DIR = OUTPUT_DIR / "annotations"
BOM_DIR = OUTPUT_DIR / "bom"
CACHE_DIR = OUTPUT_DIR / ".cache"
RENDER_WORK_DIR = OUTPUT_DIR / ".render-work"
PROCEDURES_DIR = _HW / "assembly" / "procedures"

# ── procedure-stem convention ─────────────────────────────────────────────────

# Filename convention: <family>_<NN>_<descriptor>.py (e.g. belt_20_clamp),
# NN ordering steps within a family in gaps of 10.
STEM_RE = re.compile(r"^(?P<family>[a-z]+)_(?P<nn>\d+)_(?P<descriptor>.+)$")
# Human-readable form of STEM_RE, for error messages.
STEM_CONVENTION = "<family>_<NN>_<descriptor>"

# Dependency-order families — lower index built first. Clustering each batch
# within a family lets the in-batch geometry cache reuse the shared chain.
FAMILIES = (
    "fastener",
    "frame",
    "idler",
    "motor",
    "linear",
    "belt",
    "tapz",
    "phone",
    "board",
    "camera",
    "wire",
)
FAMILY_PRIORITY = {name: i for i, name in enumerate(FAMILIES)}


def family_of(stem: str) -> str:
    return stem.split("_", 1)[0]


# ── variants & output filenames ───────────────────────────────────────────────

VARIANTS = ("exploded", "assembled")


def variant_suffix(exploded: bool) -> str:
    return "_exploded" if exploded else "_assembled"


def step_path_for(stem: str, exploded: bool) -> Path:
    """Output path for an assembly-variant STEP export."""
    return STEP_DIR / f"{stem}{variant_suffix(exploded)}.step"


def svg_path_for(stem: str, exploded: bool, index: int | None = None) -> Path:
    """Output path for a rendered SVG. Single-camera assemblies use
    ``_cam0`` so the filename scheme is uniform with multi-camera ones."""
    return SVG_DIR / f"{stem}{variant_suffix(exploded)}_cam{index or 0}.svg"


# Regex counterparts of the builders above. Two shapes for two questions:
# the stem-anchored regexes below answer "does this name belong to stem X";
# the component-capturing parsers answer "what is this file". An annotated
# figure adds _annotated_<name> to the raw render's filename.
_ANNOTATION_NAME = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
_SVG_BODY = rf"_(?:{'|'.join(VARIANTS)})_cam\d+"
_NAME_BODY = rf"(?P<stem>.+?)_(?P<variant>{'|'.join(VARIANTS)})_cam(?P<cam>\d+)"

# The annotation group is None for a raw render.
SVG_NAME_RE = re.compile(
    rf"^{_NAME_BODY}(?:_annotated_(?P<annotation>{_ANNOTATION_NAME}))?\.svg$"
)


@functools.cache
def raw_svg_re(stem: str) -> re.Pattern:
    """``<stem>_<variant>_cam<i>.svg`` — a raw render output."""
    return re.compile(rf"^{re.escape(stem)}{_SVG_BODY}\.svg$")


@functools.cache
def annotated_svg_re(stem: str) -> re.Pattern:
    """``<stem>_<variant>_cam<i>_annotated_<name>.svg`` — a manual figure."""
    return re.compile(
        rf"^{re.escape(stem)}{_SVG_BODY}_annotated_{_ANNOTATION_NAME}\.svg$"
    )
