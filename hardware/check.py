"""Static consistency check for the hardware pipeline — catches
cross-artifact drift in milliseconds instead of mid-way through a full
rebuild or a manual build.

What a rename or typo breaks quietly, this catches loudly:

  procedures/  stem naming + known family; exactly one assembly class per
               module; a well-formed class-level ``views`` declaration.
  annotations  every figure names a procedure stem and a declared view;
               its optional crop is valid.
  manual       every figure ``src``/``render`` in ``documents/manual/content/*.json``
               resolves to a declared view's raw render, one of its generated
               annotated figures, or a hand figure the manual builder
               generates itself (``HAND_FIGURES``).
  sourcing     Both vendor files’ part_ids match the BOM content
               rows one-to-one (no missing, stale, or duplicate ids).
  extrusions   every extrusion the model cuts has a BOM row whose spec
               states that length — the drawing builder's own refusal,
               surfaced here so a travel-range edit that skipped
               ``documents/bom/bom.json`` fails CI, not the release build.

Static only: everything is read via AST / JSON — no build123d import, no
``--group cad`` — so it runs anywhere (CI included) in under a second. The
one dynamic rule it cannot see is a camera index out of range for a
variant's camera *list* (``self.camera`` can be set per variant at runtime);
``BaseAssembly.view_indices`` still validates that at build time.

Run from the repo root::

    uv run python -m hardware.check      # or: python -m hardware check

Exit 0 when clean; exit 1 with one line per finding.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
import sys
from collections import Counter

from hardware import buildlog
from hardware.assembly.annotation.specs import FIGURES
from hardware.documents import BuildError
from hardware.documents.bom.loader import bom_row_findings, read_bom_rows
from hardware.documents.dimensions import resolve_dimensions
from hardware.documents.drawing.extrusion_parts import cut_list
from hardware.documents.manual.assets import HAND_FIGURES
from hardware.documents.manual.pages import CONTENT_DIR, load_chapter
from hardware.documents.sourcing import VENDOR_FILES
from hardware.scheme import (
    FAMILY_PRIORITY,
    PROCEDURES_DIR,
    STEM_CONVENTION,
    STEM_RE,
    SVG_NAME_RE,
    VARIANTS,
)

# A view tag name as written in a ``views`` declaration (EXPLODED_CAM1, …).
_TAG_RE = re.compile(rf"^({'|'.join(v.upper() for v in VARIANTS)})_CAM(\d+)$")

# Sentinel for "views not declared on this class" (inherit / render-all).
_UNDECLARED = object()


# ── procedures ────────────────────────────────────────────────────────────────


def _parse_views(node: ast.expr, where: str, findings: list[str]):
    """A ``views`` assignment value → set of (variant, cam) pairs, None for
    an explicit ``views = None`` (render every camera), or _UNDECLARED when
    the expression isn't statically readable (reported as a finding)."""
    if isinstance(node, ast.Constant) and node.value is None:
        return None
    if not isinstance(node, ast.List):
        findings.append(f"{where}: views is not a plain list literal")
        return _UNDECLARED
    views: list[tuple[str, int]] = []
    for elt in node.elts:
        if isinstance(elt, ast.Name) and (m := _TAG_RE.match(elt.id)):
            views.append((m[1].lower(), int(m[2])))
        elif isinstance(elt, ast.Tuple):
            try:
                variant, cam = ast.literal_eval(elt)
            except (ValueError, SyntaxError):
                findings.append(f"{where}: unreadable views entry")
                continue
            views.append((variant, cam))
        else:
            findings.append(f"{where}: unreadable views entry")
    for variant, cam in views:
        if variant not in VARIANTS or not isinstance(cam, int):
            findings.append(f"{where}: unknown view tag ({variant!r}, {cam!r})")
    if len(set(views)) != len(views):
        findings.append(f"{where}: duplicate view tags")
    return set(views)


def check_procedures() -> tuple[list[str], dict[str, set | None]]:
    """Validate procedures/ and return (findings, declared views by stem).
    A stem mapping to ``None`` renders every camera (permissive downstream)."""
    findings: list[str] = []
    # Per stem: the module's classes as {name: (bases, views)}.
    modules: dict[str, dict[str, tuple[list[str], object]]] = {}

    for path in sorted(PROCEDURES_DIR.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        m = STEM_RE.match(path.stem)
        if not m or m["family"] not in FAMILY_PRIORITY:
            findings.append(
                f"procedures: {path.name} doesn't match "
                f"{STEM_CONVENTION}.py with a known family"
            )
            continue
        classes: dict[str, tuple[list[str], object]] = {}
        for node in ast.parse(path.read_bytes(), filename=str(path)).body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
            views: object = _UNDECLARED
            for stmt in node.body:
                targets = (
                    stmt.targets
                    if isinstance(stmt, ast.Assign)
                    else [stmt.target]
                    if isinstance(stmt, ast.AnnAssign)
                    else []
                )
                if any(isinstance(t, ast.Name) and t.id == "views" for t in targets):
                    views = _parse_views(
                        stmt.value, f"procedures: {path.name} {node.name}", findings
                    )
            classes[node.name] = (bases, views)
        modules[path.stem] = classes

    # Which classes are assemblies? Fixpoint over base names, since a
    # procedure may subclass another procedure's class rather than
    # BaseAssembly directly (e.g. ID20Ru(ID10Lu)).
    class_stem = {name: stem for stem, cls in modules.items() for name in cls}
    assembly = {"BaseAssembly"}
    changed = True
    while changed:
        changed = False
        for classes in modules.values():
            for name, (bases, _) in classes.items():
                if name not in assembly and assembly.intersection(bases):
                    assembly.add(name)
                    changed = True

    views_by_stem: dict[str, set | None] = {}
    for stem, classes in modules.items():
        asm = [n for n in classes if n in assembly]
        if len(asm) != 1:
            findings.append(
                f"procedures: {stem}.py defines {len(asm)} assembly classes "
                f"(need exactly 1)"
            )
            continue
        # Resolve an undeclared ``views`` up the inheritance chain: a
        # procedure subclassing another procedure's class (ID20Ru(ID10Lu))
        # inherits its declaration unless it overrides.
        name = asm[0]
        views = classes[name][1]
        seen = {name}
        while views is _UNDECLARED:
            bases, _ = modules[class_stem[name]][name]
            parent = next((b for b in bases if b in class_stem and b not in seen), None)
            if parent is None:
                views = None  # reached BaseAssembly: default renders all
                break
            seen.add(parent)
            name = parent
            views = modules[class_stem[name]][name][1]
        views_by_stem[stem] = views
    return findings, views_by_stem


def _view_declared(views: set | None, variant: str, cam: int) -> bool:
    return views is None or (variant, cam) in views


# ── generated annotations ─────────────────────────────────────────────────────


def check_annotations(views_by_stem):
    """Validate generated figures against the procedure views, without CAD."""
    findings = []
    generated_figures = set()
    for filename, figure in sorted(FIGURES.items()):
        view = figure.view
        m = SVG_NAME_RE.fullmatch(view + ".svg")
        if not m or m["annotation"] is not None:
            findings.append(f"annotation: {view!r} must name a procedure view")
            continue
        if filename != figure.filename or not SVG_NAME_RE.fullmatch(filename):
            findings.append(f"annotation: {filename!r} is not a valid figure filename")
            continue
        stem, variant, cam = m["stem"], m["variant"], int(m["cam"])
        if stem not in views_by_stem:
            findings.append(f"annotation: {view} names unknown procedure {stem!r}")
        elif not _view_declared(views_by_stem[stem], variant, cam):
            findings.append(
                f"annotation: {view} targets {variant}_cam{cam}, which {stem} doesn't declare in views"
            )
        if figure.crop is not None and (
            len(figure.crop) != 4
            or any(
                not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in figure.crop
            )
            or figure.crop[2] <= 0
            or figure.crop[3] <= 0
        ):
            findings.append(f"annotation: {view} has an invalid crop")
        generated_figures.add(filename)
    return findings, generated_figures


# ── manual content ────────────────────────────────────────────────────────────


def load_content() -> tuple[list[str], dict[str, list]]:
    """Parse every ``content/*.json`` once for both content checks. Returns
    (findings for unparseable files, pages keyed by filename)."""
    findings: list[str] = []
    pages_by_file: dict[str, list] = {}
    for path in sorted(CONTENT_DIR.glob("*.json")):
        try:
            pages_by_file[path.name] = load_chapter(path)
        except ValueError as exc:
            findings.append(f"manual: {path.name} is not valid JSON ({exc})")
        except (BuildError, OSError) as exc:
            findings.append(f"manual: {exc}")
    return findings, pages_by_file


def _figure_refs(pages: object) -> set[str]:
    """Every ``src``/``render`` string ending in .svg, recursively."""
    refs: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    key in ("src", "render")
                    and isinstance(child, str)
                    and child.endswith(".svg")
                ):
                    refs.add(child)
                else:
                    walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(pages)
    return refs


def check_manual(
    views_by_stem: dict[str, set | None],
    generated_figures: set[str],
    pages_by_file: dict[str, list],
) -> list[str]:
    findings: list[str] = []
    hand = set(HAND_FIGURES)  # SVGs the manual builder writes itself
    for filename, pages in pages_by_file.items():
        for ref in sorted(_figure_refs(pages)):
            if ref in hand:
                continue
            m = SVG_NAME_RE.match(ref)
            if not m or m["stem"] not in views_by_stem:
                findings.append(
                    f"manual: {filename} references {ref!r}, which is neither "
                    f"a procedure render nor a hand figure"
                )
                continue
            stem, variant, cam = (
                m["stem"],
                m["variant"],
                int(m["cam"]),
            )
            if not _view_declared(views_by_stem[stem], variant, cam):
                findings.append(
                    f"manual: {filename} references {ref!r}, but {stem} "
                    f"doesn't declare {variant}_cam{cam} in views"
                )
                continue
            if m["annotation"] is None:
                continue
            base = f"{stem}_{variant}_cam{cam}.svg"
            if ref not in generated_figures:
                findings.append(
                    f"manual: {filename} references annotated figure {ref!r}, "
                    f"but it has no declared annotation for {base}"
                )
    return findings


# ── sourcing ──────────────────────────────────────────────────────────────────


def _duplicates(ids: list) -> list[str]:
    """Non-empty ids appearing more than once, sorted."""
    return sorted(i for i, n in Counter(filter(None, ids)).items() if n > 1)


def load_purchasing_bom() -> tuple[list[str], list]:
    """Check the purchasing list independently of the manual's page structure."""
    try:
        return [], read_bom_rows()
    except BuildError as exc:
        return [f"bom: {exc}"], []


def check_sourcing(rows: list) -> list[str]:
    """The shared purchasing rows and each vendor file must join one-to-one
    on part_id — the same invariants the sourcing builder enforces at build
    time (missing/duplicate row ids) plus the sync drift it only reports
    (stale / missing vendor entries)."""
    findings = [f"sourcing: {finding}" for finding in bom_row_findings(rows)]
    row_set = {
        row["part_id"]
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("part_id"), str)
        and row["part_id"]
    }
    for lang, vendor_file in VENDOR_FILES.items():
        if not vendor_file.exists():
            findings.append(f"sourcing: {vendor_file.name} not found")
            continue
        try:
            entries = json.loads(vendor_file.read_text(encoding="utf-8"))
        except ValueError as exc:
            findings.append(f"sourcing: {vendor_file.name} is not valid JSON ({exc})")
            continue
        try:
            entries = resolve_dimensions(entries, source=str(vendor_file))
        except BuildError as exc:
            findings.append(f"sourcing: {exc}")
            continue
        vendor_ids = [e.get("part_id") for e in entries if isinstance(e, dict)]
        if dupes := _duplicates(vendor_ids):
            findings.append(
                f"sourcing: duplicate part_id(s) in {vendor_file.name}: {', '.join(dupes)}"
            )
        vendor_set = set(filter(None, vendor_ids))
        if stale := sorted(vendor_set - row_set):
            findings.append(
                f"sourcing: {vendor_file.name} has stale part_id(s) with no BOM "
                f"row: {', '.join(stale)}"
            )
        if absent := sorted(row_set - vendor_set):
            findings.append(
                f"sourcing: BOM part_id(s) missing from {vendor_file.name}: "
                f"{', '.join(absent)} (run `python -m hardware sourcing --lang {lang} --scaffold`)"
            )
    return findings


# ── extrusions ────────────────────────────────────────────────────────────────


def check_extrusions(rows: list) -> list[str]:
    """Each extrusion the model cuts (``drawing.extrusion_parts.SPECS``,
    lengths from ``travel_ranges``) needs a BOM row stating that length in
    both languages — the join the drawing builder makes; its refusal is the
    finding."""
    try:
        cut_list(
            [
                row
                for row in rows
                if isinstance(row, dict) and isinstance(row.get("part_id"), str)
            ]
        )
    except BuildError as exc:
        return [f"extrusions: {exc}"]
    return []


# ── entry point ───────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    buildlog.add_arguments(parser)
    args = parser.parse_args(argv)
    with buildlog.command("Hardware check", args, name="check") as run:
        with buildlog.step("Check consistency"):
            proc_findings, views_by_stem = check_procedures()
            annotation_findings, generated_figures = check_annotations(views_by_stem)
            content_findings, pages_by_file = load_content()
            bom_findings, rows = load_purchasing_bom()
            findings = (
                proc_findings
                + annotation_findings
                + content_findings
                + bom_findings
                + check_manual(views_by_stem, generated_figures, pages_by_file)
                + check_sourcing(rows)
                + check_extrusions(rows)
            )
        if findings:
            for finding in findings:
                buildlog.event("consistency_error", finding, level="error")
            run.exit_code = 1
            run.summary = f"{len(findings)} consistency findings"
        else:
            n_figures = len(FIGURES)
            run.summary = "All checks passed"
            run.details.append(
                f"{len(views_by_stem)} procedures, {n_figures} annotated figures; manual, sourcing and extrusions consistent"
            )
        return run.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
