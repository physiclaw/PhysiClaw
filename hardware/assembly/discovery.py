"""Procedure metadata without importing CAD models.

Owns discovery, family ordering, batching and delta-BOM predecessors. Both
execution and cache dependency tracking use these rules; dynamic predecessor
selection must not live only inside the BOM writer.
"""

from itertools import groupby
from pathlib import Path

from hardware.scheme import (
    FAMILY_PRIORITY,
    PROCEDURES_DIR,
    STEM_CONVENTION,
    STEM_RE,
    family_of,
)


def list_procedures() -> list[str]:
    """All procedure module stems, in build order: family dependency
    order (``FAMILIES``), then NN within the family.

    Fails fast on a ``.py`` that doesn't match the stem convention or
    names an unknown family — either would otherwise be silently skipped
    (a typo'd stem never builds) or silently ordered last (an unlisted
    family). Underscore-prefixed files are non-procedures by convention
    and stay exempt."""
    keyed = []
    bad: list[str] = []
    for path in PROCEDURES_DIR.glob("*.py"):
        if path.stem.startswith("_"):
            continue
        m = STEM_RE.match(path.stem)
        if not m or m["family"] not in FAMILY_PRIORITY:
            bad.append(path.name)
            continue
        keyed.append((FAMILY_PRIORITY[m["family"]], int(m["nn"]), path.stem))
    if bad:
        raise ValueError(
            f"procedure file(s) {sorted(bad)} don't match "
            f"{STEM_CONVENTION}.py with a family from "
            f"hardware.scheme.FAMILIES; rename them (or prefix with '_' "
            f"to exclude)"
        )
    keyed.sort()
    return [stem for *_, stem in keyed]


def _batches(batch_size: int) -> list[list[str]]:
    """Family-clustered chunks of up to ``batch_size``, dependency order."""
    out: list[list[str]] = []
    for _, group in groupby(list_procedures(), key=family_of):
        stems = list(group)
        for i in range(0, len(stems), batch_size):
            out.append(stems[i : i + batch_size])
    return out


def predecessor_module(
    module_name: str, *, directory: Path | None = None
) -> str | None:
    """Return the predecessor procedure's module name (e.g.
    'hardware.assembly.procedures.belt_10_motor_a') or None if this is
    the first step in its family.

    Convention: predecessor = same family prefix, largest NN strictly
    less than this file's NN. Cross-family chains aren't auto-resolved."""
    stem = module_name.rsplit(".", 1)[-1]
    m = STEM_RE.match(stem)
    if not m:
        return None
    family, nn = m["family"], int(m["nn"])
    candidates: list[tuple[int, str]] = []
    directory = PROCEDURES_DIR if directory is None else directory
    for path in directory.glob(f"{family}_*.py"):
        cm = STEM_RE.match(path.stem)
        if not cm:
            continue
        cnn = int(cm["nn"])
        if cnn < nn:
            candidates.append((cnn, path.stem))
    if not candidates:
        return None
    prior_stem = max(candidates)[1]
    return f"hardware.assembly.procedures.{prior_stem}"
