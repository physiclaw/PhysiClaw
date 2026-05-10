"""Re-exports of FreeCAD's `App`, `Part`, and `Constraint` for the parts
library — centralises the IDE-unresolvable imports and their lint
suppressions, and turns "No module named 'FreeCAD'" into an actionable
error pointing at the right invocation."""

try:
    import FreeCAD as App  # type: ignore[import-not-found]
    import Part  # type: ignore[import-not-found]
    from Sketcher import Constraint  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover — only happens outside FreeCAD
    raise ImportError(
        "FreeCAD APIs are not available — these scripts must run inside "
        "FreeCAD's embedded interpreter. Try:\n"
        "    /Applications/FreeCAD.app/Contents/MacOS/FreeCAD -c "
        "hardware/scripts/build_all_fc.py"
    ) from exc

__all__ = ["App", "Part", "Constraint"]
