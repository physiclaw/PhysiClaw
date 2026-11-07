"""Join CAD extrusion specifications to the authored purchasing list.

This standard-library module owns cut-list validation, shared by hw-check and
the drawing renderer. It does not import CAD geometry or document rendering.
"""

from dataclasses import dataclass

from hardware.assembly.travel_ranges import (
    PHONE_BED_BEAM_LENGTH,
    X_BEAM_LENGTH,
    X_EXTRUSION_LENGTH,
    Y_EXTRUSION_LENGTH,
)
from hardware.documents import BuildError
from hardware.documents.bom.loader import load_bom_rows
from hardware.documents.common import loc
from hardware.parts.standard import extrusion_spec as spec

# --------------------------------------------------------------------------- #
# The cut list — what the supplier machines, joined to the BOM rows
# --------------------------------------------------------------------------- #
# Machining kinds; each has one table sentence and one drawing row.
CB, TAP, PLAIN, HOLE = "cb", "tap", "plain", "hole"


@dataclass(frozen=True)
class Profile:
    """A standard profile as the sheet presents it: dimensioned by the
    nominal size its designation means (the 1020 is modelled at its
    measured 19.8 × 9.9, but sold as 20 × 10) and ordered with a finish."""

    nominal: tuple[float, float]  # width × height
    size: tuple[float, float]  # the model's section, width × height
    finish: dict | str  # as the BOM orders it; "—" when unspecified

    @property
    def heading(self) -> str:
        return f"{fmt(self.nominal[0])} × {fmt(self.nominal[1])}"


PROFILES = {
    "2040": Profile(
        spec.nominal_2040,
        (2 * (spec.cell_offset + spec.leg), 2 * spec.leg),
        {"en": "Black anodized", "zh": "黑色阳极氧化"},
    ),
    "1020": Profile(spec.nominal_1020, (2 * spec.half_x_1020, spec.half_x_1020), "—"),
}


@dataclass(frozen=True)
class CutSpec:
    """One extrusion specification as the model cuts it. ``part_id`` names
    the BOM row that carries the quantity and the application."""

    part_id: str
    profile: str  # a PROFILES key
    length: float
    machining: str


SPECS: tuple[CutSpec, ...] = (
    CutSpec("ext-2040-y", "2040", Y_EXTRUSION_LENGTH, CB),
    CutSpec("ext-2040-x", "2040", X_EXTRUSION_LENGTH, TAP),
    CutSpec("ext-1020-x-beam", "1020", X_BEAM_LENGTH, PLAIN),
    CutSpec("ext-1020-phone-support", "1020", PHONE_BED_BEAM_LENGTH, HOLE),
)


@dataclass(frozen=True)
class CutItem:
    """A cut-list line: the spec plus what its BOM row says about it."""

    spec: CutSpec
    qty: int
    application: dict | str


def fmt(value: float) -> str:
    """A millimetre value as drawn: no trailing zeros (345, 5.5, 19.8)."""
    return f"{value:g}"


def cut_list(rows: list[dict] | None = None) -> list[CutItem]:
    """The four specifications joined to their BOM rows, in SPECS order.

    Raises ``BuildError`` when a spec's BOM row is missing, or when the
    row's spec text (either language) no longer states the length the model
    cuts — the BOM is hand-written, so this is where a travel-range edit
    that was not carried into the manual surfaces."""
    rows = load_bom_rows() if rows is None else rows
    by_id = {r.get("part_id"): r for r in rows}
    items: list[CutItem] = []
    for cut in SPECS:
        row = by_id.get(cut.part_id)
        if row is None:
            raise BuildError(f"no BOM row with part_id {cut.part_id!r}")
        stated = f"{fmt(cut.length)} mm"
        for lang in ("en", "zh"):
            if stated not in loc(row.get("spec", ""), lang):
                raise BuildError(
                    f"BOM row {cut.part_id!r} spec [{lang}] does not state "
                    f"{stated!r} — the model cuts {stated} "
                    f"(hardware/assembly/travel_ranges.py); update hardware/documents/bom/bom.json"
                )
        try:
            qty = int(row.get("qty", ""))
        except ValueError:
            raise BuildError(
                f"BOM row {cut.part_id!r} qty {row.get('qty')!r} is not a number"
            ) from None
        items.append(CutItem(cut, qty, row.get("desc", "")))
    return items
