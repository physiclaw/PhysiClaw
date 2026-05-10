#!/usr/bin/env python3
"""Verify a build123d-generated STEP matches the original.

Two complementary checks are performed:

  1. Geometric fingerprint comparison — sorted multisets of edge lengths
     and face areas, plus volume and bounding box. This is robust to STEP
     round-trip artifacts and distinguishes parts decisively for simple
     prismatic/cylindrical geometry.

  2. Boolean diff — `original - draft` and `draft - original`. Useful when
     it works because the diff geometry itself shows what's wrong, but
     OCCT booleans can be fragile across separately-imported STEPs, so
     this is best-effort. A boolean failure does NOT mean the parts differ;
     fall back to the fingerprint result.

A part is considered to match if volume agrees to within tolerance AND
the edge-length and face-area multisets agree to within tolerance.

Usage:
    python verify_match.py original.step draft.step
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from build123d import import_step, export_step, Compound


# Tolerances. These are calibrated for typical mm-scale parts; very small or
# very large parts may need tuning, but the relative-volume threshold scales.
VOLUME_TOLERANCE_FRACTION = 0.001  # 0.1% of original volume
LENGTH_TOLERANCE = 1e-3            # mm — STEP round-trip is typically <1e-5
AREA_TOLERANCE = 1e-2              # mm² — areas accumulate more error


def verify(original_path: str, draft_path: str) -> dict:
    original = import_step(original_path)
    draft = import_step(draft_path)

    fingerprint = _fingerprint_compare(original, draft)
    bool_diff = _bool_diff(original, draft, draft_path)

    passed = fingerprint["passed"]

    return {
        "passed": passed,
        "fingerprint": fingerprint,
        "boolean_diff": bool_diff,
        "interpretation": _interpret(fingerprint, bool_diff),
    }


def _fingerprint_compare(original, draft) -> dict:
    """Compare parts by volume, bbox, edge-length multiset, face-area multiset."""
    o_vol = original.volume
    d_vol = draft.volume
    vol_tol = o_vol * VOLUME_TOLERANCE_FRACTION
    vol_match = abs(o_vol - d_vol) <= vol_tol

    o_bb = original.bounding_box()
    d_bb = draft.bounding_box()
    bb_match = (
        abs(o_bb.size.X - d_bb.size.X) < LENGTH_TOLERANCE
        and abs(o_bb.size.Y - d_bb.size.Y) < LENGTH_TOLERANCE
        and abs(o_bb.size.Z - d_bb.size.Z) < LENGTH_TOLERANCE
    )

    o_lengths = sorted(e.length for e in original.edges())
    d_lengths = sorted(e.length for e in draft.edges())
    edge_match = _multiset_match(o_lengths, d_lengths, LENGTH_TOLERANCE)

    o_areas = sorted(f.area for f in original.faces())
    d_areas = sorted(f.area for f in draft.faces())
    face_match = _multiset_match(o_areas, d_areas, AREA_TOLERANCE)

    passed = vol_match and bb_match and edge_match["match"] and face_match["match"]

    return {
        "passed": passed,
        "volume": {
            "original": round(o_vol, 4),
            "draft": round(d_vol, 4),
            "diff_percent": round(abs(o_vol - d_vol) / o_vol * 100, 4) if o_vol else None,
            "tolerance": round(vol_tol, 4),
            "match": vol_match,
        },
        "bounding_box": {
            "original_size": [round(o_bb.size.X, 4), round(o_bb.size.Y, 4), round(o_bb.size.Z, 4)],
            "draft_size": [round(d_bb.size.X, 4), round(d_bb.size.Y, 4), round(d_bb.size.Z, 4)],
            "match": bb_match,
        },
        "edges": {
            "original_count": len(o_lengths),
            "draft_count": len(d_lengths),
            **edge_match,
        },
        "faces": {
            "original_count": len(o_areas),
            "draft_count": len(d_areas),
            **face_match,
        },
    }


def _multiset_match(a: list[float], b: list[float], tol: float) -> dict:
    """Compare two sorted lists of floats as multisets within tolerance.

    Returns match status plus the largest mismatch found (most informative
    for debugging — tells you which values are off and by how much)."""
    if len(a) != len(b):
        return {
            "match": False,
            "reason": f"count differs: {len(a)} vs {len(b)}",
            "max_diff": None,
        }
    max_diff = 0.0
    max_diff_at = None
    for x, y in zip(a, b):
        diff = abs(x - y)
        if diff > max_diff:
            max_diff = diff
            max_diff_at = (round(x, 4), round(y, 4))
    return {
        "match": max_diff <= tol,
        "max_diff": round(max_diff, 6),
        "max_diff_at": max_diff_at,  # (original_value, draft_value) at worst mismatch
        "tolerance": tol,
    }


def _bool_diff(original, draft, draft_path) -> dict:
    """Best-effort boolean diff. Returns volumes and exported diff geometries
    when meaningful; reports failure cleanly otherwise."""
    orig_vol = original.volume
    draft_vol = draft.volume
    tol_vol = orig_vol * VOLUME_TOLERANCE_FRACTION

    result = {
        "missing_volume": None,
        "extra_volume": None,
        "missing_diff_step": None,
        "extra_diff_step": None,
        "errors": [],
        "reliable": True,
    }

    try:
        missing = original - draft
        m_vol = _safe_volume(missing)
        # Sanity check: if the diff equals the entire original, the boolean
        # didn't actually compute anything useful (OCCT sometimes returns the
        # left operand when the operation degenerates).
        if abs(m_vol - orig_vol) < tol_vol:
            result["reliable"] = False
            result["errors"].append(
                "original - draft returned a volume equal to original; "
                "boolean likely degenerated. Trust the fingerprint check."
            )
        else:
            result["missing_volume"] = round(m_vol, 4)
            if m_vol > tol_vol:
                path = str(Path(draft_path).with_name("diff_original_minus_draft.step"))
                _export_diff(missing, path)
                result["missing_diff_step"] = path
    except Exception as e:
        result["errors"].append(f"original - draft failed: {e}")
        result["reliable"] = False

    try:
        extra = draft - original
        e_vol = _safe_volume(extra)
        if abs(e_vol - draft_vol) < tol_vol:
            result["reliable"] = False
            result["errors"].append(
                "draft - original returned a volume equal to draft; "
                "boolean likely degenerated. Trust the fingerprint check."
            )
        else:
            result["extra_volume"] = round(e_vol, 4)
            if e_vol > tol_vol:
                path = str(Path(draft_path).with_name("diff_draft_minus_original.step"))
                _export_diff(extra, path)
                result["extra_diff_step"] = path
    except Exception as e:
        result["errors"].append(f"draft - original failed: {e}")
        result["reliable"] = False

    return result


def _safe_volume(diff_result) -> float:
    if diff_result is None:
        return 0.0
    if hasattr(diff_result, "__len__") and len(diff_result) == 0:
        return 0.0
    if hasattr(diff_result, "volume"):
        try:
            return float(diff_result.volume)
        except Exception:
            pass
    try:
        return sum(s.volume for s in diff_result)
    except Exception:
        return 0.0


def _export_diff(diff_result, path: str) -> None:
    if hasattr(diff_result, "wrapped") and diff_result.wrapped is not None:
        export_step(diff_result, path)
        return
    try:
        shapes = list(diff_result)
        if not shapes:
            return
        export_step(Compound(shapes), path)
    except Exception:
        export_step(diff_result, path)


def _interpret(fingerprint: dict, bool_diff: dict) -> str:
    if fingerprint["passed"]:
        return ("MATCH. Volume, bounding box, edge-length set, and face-area set "
                "all agree within tolerance.")

    reasons = []
    if not fingerprint["volume"]["match"]:
        diff_pct = fingerprint["volume"]["diff_percent"]
        reasons.append(f"volume differs by {diff_pct:.3f}%")
    if not fingerprint["bounding_box"]["match"]:
        reasons.append(f"bounding boxes differ "
                       f"(original {fingerprint['bounding_box']['original_size']} vs "
                       f"draft {fingerprint['bounding_box']['draft_size']})")
    if not fingerprint["edges"]["match"]:
        e = fingerprint["edges"]
        if e["original_count"] != e["draft_count"]:
            reasons.append(f"edge count differs ({e['original_count']} vs {e['draft_count']}) — "
                           "likely a missing or extra feature")
        elif e.get("max_diff_at"):
            o_val, d_val = e["max_diff_at"]
            reasons.append(f"edge length mismatch up to {e['max_diff']:.4f} mm "
                           f"(original {o_val} vs draft {d_val})")
    if not fingerprint["faces"]["match"]:
        f = fingerprint["faces"]
        if f["original_count"] != f["draft_count"]:
            reasons.append(f"face count differs ({f['original_count']} vs {f['draft_count']}) — "
                           "likely a missing or extra feature")
        elif f.get("max_diff_at"):
            o_val, d_val = f["max_diff_at"]
            reasons.append(f"face area mismatch up to {f['max_diff']:.4f} mm² "
                           f"(original {o_val} vs draft {d_val})")

    interp = "MISMATCH: " + "; ".join(reasons)

    if bool_diff["reliable"] and bool_diff.get("missing_diff_step"):
        interp += f". Missing-material diff exported to {bool_diff['missing_diff_step']}."
    if bool_diff["reliable"] and bool_diff.get("extra_diff_step"):
        interp += f". Extra-material diff exported to {bool_diff['extra_diff_step']}."
    if not bool_diff["reliable"]:
        interp += " Boolean diff was unreliable; rely on fingerprint values to localize the error."

    return interp


def main():
    if len(sys.argv) != 3:
        print("usage: python verify_match.py <original.step> <draft.step>", file=sys.stderr)
        sys.exit(2)

    original_path, draft_path = sys.argv[1], sys.argv[2]
    for p in (original_path, draft_path):
        if not Path(p).exists():
            print(f"file not found: {p}", file=sys.stderr)
            sys.exit(2)

    result = verify(original_path, draft_path)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
