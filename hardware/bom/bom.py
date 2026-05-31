"""BOM (bill of materials) for assembly steps — library + CLI.

Each ``BasePart`` instance pushes one entry (``bom_key``, ``qty``,
``category``) into a process-wide ``BOM_REGISTRY`` (in
``hardware.parts.base``) the first time it builds — memo-guarded, so
re-builds of the same instance don't double-push. To get the BOM for a
procedure step, ``collect()`` clears the registry, drives the step's
build (which transitively builds every prior step in the chain), then
aggregates the registry by ``bom_key``.

Predecessor for ``--delta`` is found by convention: same family prefix
(``belt_`` / ``frame_`` / …), next-lower ``NN`` index in the filename.

Outputs Markdown (primary, for docs) and JSON (machine-readable).

CLI usage:

    uv run --group cad python -m hardware.bom.bom belt_20_clamp
    uv run --group cad python -m hardware.bom.bom belt_20_clamp --delta
    uv run --group cad python -m hardware.bom.bom belt_20_clamp --json
    uv run --group cad python -m hardware.bom.bom belt_20_clamp --from belt_10_motor_a --delta
    uv run --group cad python -m hardware.bom.bom --all --write

Aggregating a late step transitively builds its whole dependency chain,
so doing every procedure in one process accumulates many GB of OCCT
geometry and gets OOM-killed. Like ``hardware.assembly.build_procedures``,
``--all`` dispatches family-clustered batches to subprocesses, returning
memory to the OS between batches (the in-batch geometry cache still warms).

Worker mode (invoked internally by ``--all``):

    uv run --group cad python -m hardware.bom.bom --stems belt_10_motor_a belt_20_clamp --write
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import subprocess
import sys
import traceback
from collections import defaultdict
from dataclasses import dataclass
from itertools import groupby

from hardware.assembly.base import BaseAssembly
from hardware.parts.base import BOM_REGISTRY, REPO_ROOT, BomCategory

PROCEDURES_DIR = REPO_ROOT / "hardware" / "assembly" / "procedures"
BOM_DIR = REPO_ROOT / "hardware" / "output" / "bom"

# Filename convention: <family>_<NN>_<descriptor>.py (e.g. belt_20_clamp).
_STEM_RE = re.compile(r"^(?P<family>[a-z]+)_(?P<nn>\d+)_(?P<descriptor>.+)$")


@dataclass(frozen=True, order=True)
class BomEntry:
    category: BomCategory
    key: tuple
    qty: int
    custom_display: str | None = None

    @property
    def display(self) -> str:
        """e.g. ('Screw', 'SHCS', 'M6', 16) -> 'Screw SHCS M6×16'. If the
        part class provided a ``bom_display`` override, use that instead."""
        if self.custom_display is not None:
            return self.custom_display
        head, *rest = self.key
        out = head
        for item in rest:
            out += f"×{item:g}" if isinstance(item, (int, float)) else f" {item}"
        return out


# ── Collection ────────────────────────────────────────────────────────────────

def collect(step: BaseAssembly) -> list[BomEntry]:
    """Build the step and aggregate its BOM. Returns a sorted list.

    Clears the registry up front (and the step's memo) so a re-collect
    rebuilds cleanly. Every ``BasePart.build()`` in the chain pushes one
    entry; we just aggregate by ``bom_key``."""
    BOM_REGISTRY.clear()
    if hasattr(step, "_built"):
        delattr(step, "_built")
    step.build()

    sums: dict[tuple, int] = defaultdict(int)
    cats: dict[tuple, str] = {}
    displays: dict[tuple, str | None] = {}
    for key, qty, category, display in BOM_REGISTRY:
        sums[key] += qty
        cats[key] = category
        displays[key] = display

    entries = [BomEntry(cats[k], k, q, displays[k]) for k, q in sums.items()]
    entries.sort()
    return entries


# ── Delta against a predecessor step ──────────────────────────────────────────

def delta(current: list[BomEntry], previous: list[BomEntry]) -> list[BomEntry]:
    """Entries added at the current step that weren't at the previous
    step. Quantity is the cumulative delta (current − previous, omitting
    zero/negative deltas). Categories follow the current step."""
    prev_qty = {e.key: e.qty for e in previous}
    out: list[BomEntry] = []
    for e in current:
        diff = e.qty - prev_qty.get(e.key, 0)
        if diff > 0:
            out.append(BomEntry(e.category, e.key, diff, e.custom_display))
    out.sort()
    return out


# ── Predecessor discovery ────────────────────────────────────────────────────

def predecessor_module(module_name: str) -> str | None:
    """Return the predecessor procedure's module name (e.g.
    'hardware.assembly.procedures.belt_10_motor_a') or None if this is
    the first step in its family.

    Convention: predecessor = same family prefix, largest NN strictly
    less than this file's NN. Cross-family chains aren't auto-resolved;
    pass --from explicitly for those."""
    stem = module_name.rsplit(".", 1)[-1]
    m = _STEM_RE.match(stem)
    if not m:
        return None
    family, nn = m["family"], int(m["nn"])
    candidates: list[tuple[int, str]] = []
    for path in PROCEDURES_DIR.glob(f"{family}_*.py"):
        cm = _STEM_RE.match(path.stem)
        if not cm:
            continue
        cnn = int(cm["nn"])
        if cnn < nn:
            candidates.append((cnn, path.stem))
    if not candidates:
        return None
    candidates.sort()
    prior_stem = candidates[-1][1]
    return f"hardware.assembly.procedures.{prior_stem}"


# ── Step loading ─────────────────────────────────────────────────────────────

def load_step(module_name: str) -> BaseAssembly:
    """Import a procedure module and instantiate its BaseAssembly
    subclass. The procedure files each define exactly one such class."""
    if "." not in module_name:
        module_name = f"hardware.assembly.procedures.{module_name}"
    mod = importlib.import_module(module_name)
    for obj in vars(mod).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, BaseAssembly)
            and obj is not BaseAssembly
            and obj.__module__ == mod.__name__
        ):
            return obj(exploded=False)
    raise LookupError(f"No BaseAssembly subclass in {module_name}")


def list_procedures() -> list[str]:
    """All procedure module names, sorted by family then NN."""
    keyed = []
    for path in PROCEDURES_DIR.glob("*.py"):
        m = _STEM_RE.match(path.stem)
        if not m or path.stem.startswith("_"):
            continue
        keyed.append((m["family"], int(m["nn"]), path.stem))
    keyed.sort()
    return [stem for _, _, stem in keyed]


# ── Procedure ordering & subprocess batching ─────────────────────────────────
# Single source of truth (here, next to list_procedures);
# hardware.assembly.build_procedures imports these to batch its renders in the
# same order. Subprocess batching bounds OCCT memory — a batch's geometry is
# freed when its worker exits — while same-family clustering keeps the in-batch
# geometry cache warm on the shared dependency chain.
_FAMILIES = ("frame", "idler", "motor", "linear", "belt", "tapz")
_FAMILY_PRIORITY = {name: i for i, name in enumerate(_FAMILIES)}
DEFAULT_BATCH_SIZE = 5


def _family_of(stem: str) -> str:
    return stem.split("_", 1)[0]


def _ordered_stems() -> list[str]:
    """Procedure stems in dependency-order family, then NN. list_procedures
    already sorts by (family, NN), but alphabetical family order isn't the
    dependency/build order — so re-sort by _FAMILY_PRIORITY."""
    fallback = len(_FAMILY_PRIORITY)
    return sorted(
        list_procedures(),
        key=lambda s: (_FAMILY_PRIORITY.get(_family_of(s), fallback), s),
    )


def _batches(batch_size: int) -> list[list[str]]:
    """Family-clustered chunks of up to ``batch_size``, dependency order."""
    out: list[list[str]] = []
    for _, group in groupby(_ordered_stems(), key=_family_of):
        stems = list(group)
        for i in range(0, len(stems), batch_size):
            out.append(stems[i:i + batch_size])
    return out


# ── Writers ──────────────────────────────────────────────────────────────────

def to_markdown(entries: list[BomEntry], *, title: str) -> str:
    standard = [e for e in entries if e.category == "standard"]
    custom = [e for e in entries if e.category == "custom"]
    lines = [f"# {title}", ""]
    lines += _section("Standard parts", standard)
    lines += _section("Custom parts", custom)
    return "\n".join(lines).rstrip() + "\n"


def _section(heading: str, entries: list[BomEntry]) -> list[str]:
    if not entries:
        return []
    out = [f"## {heading}", "", "| Qty | Part |", "|----:|------|"]
    for e in entries:
        out.append(f"| {e.qty} | {e.display} |")
    out.append("")
    return out


def to_json(entries: list[BomEntry], *, step: str) -> str:
    return json.dumps(
        {
            "step": step,
            "entries": [
                {"category": e.category, "key": list(e.key), "qty": e.qty}
                for e in entries
            ],
        },
        indent=2,
    )


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bom", description=__doc__.splitlines()[0])
    p.add_argument(
        "step",
        nargs="?",
        help="procedure module (e.g. belt_20_clamp). Omit with --all.",
    )
    p.add_argument(
        "--delta",
        action="store_true",
        help="emit only entries added vs. the predecessor step",
    )
    p.add_argument(
        "--from",
        dest="from_step",
        metavar="MODULE",
        help="override the predecessor for --delta (otherwise: same family, prior NN)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of Markdown",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="process every procedure in batched subprocesses (use with --write)",
    )
    p.add_argument(
        "--write",
        action="store_true",
        help="write to hardware/output/bom/ instead of stdout",
    )
    p.add_argument(
        "--stems",
        nargs="+",
        help="worker mode: process these stems in-process (used internally by --all)",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"max procedures per subprocess for --all (default {DEFAULT_BATCH_SIZE})",
    )
    args = p.parse_args(argv)

    if args.stems:
        if args.step or args.all:
            p.error("--stems is a worker mode; don't combine it with STEP or --all")
        rc = 0
        for step_name in args.stems:
            try:
                _emit_one(step_name, args, write_only=True)
            except Exception:
                traceback.print_exc()
                rc = 1
        return rc

    if args.all:
        if args.step:
            p.error("--all is incompatible with a positional STEP")
        if not args.write:
            p.error("--all without --write would dump every BOM to stdout; add --write")
        return _dispatch_all(args)

    if not args.step:
        p.error("STEP is required (or pass --all --write)")

    _emit_one(args.step, args)
    return 0


def _dispatch_all(args) -> int:
    """Run every procedure's BOM in family-clustered batches, each in its
    own subprocess so OCCT geometry is freed between batches. Passes the
    output flags (--write/--json/--delta/--from) through to the workers."""
    passthrough = ["--write"]
    if args.json:
        passthrough.append("--json")
    if args.delta:
        passthrough.append("--delta")
    if args.from_step:
        passthrough += ["--from", args.from_step]

    batches = _batches(args.batch_size)
    total = sum(len(b) for b in batches)
    done = 0
    rc = 0
    for batch in batches:
        start = done + 1
        done += len(batch)
        span = f"{start}/{total}" if len(batch) == 1 else f"{start}-{done}/{total}"
        print(f"\n=== [{span}] {_family_of(batch[0])}: {batch[0]} … {batch[-1]} ===")
        ret = subprocess.call([
            sys.executable, "-m", "hardware.bom.bom", "--stems", *batch, *passthrough,
        ])
        if ret != 0:
            rc = 1
            print(f"  batch exited {ret}")

    if rc:
        print("\nOne or more batches failed.")
    else:
        print(f"\nAll {len(batches)} batches OK — {total} BOMs written to {BOM_DIR}")
    return rc


def _emit_one(step_name: str, args, *, write_only: bool = False) -> None:
    step = load_step(step_name)
    current = collect(step)

    if args.delta:
        prev_name = args.from_step or predecessor_module(step_name)
        if prev_name is None:
            print(
                f"# {step_name}: no predecessor in the same family — "
                "pass --from MODULE for a cross-family delta, or drop --delta",
                file=sys.stderr,
            )
            sys.exit(1)
        previous = collect(load_step(prev_name))
        entries = delta(current, previous)
        title = f"{step_name} — delta vs. {prev_name.rsplit('.', 1)[-1]}"
        suffix = "_delta"
    else:
        entries = current
        title = f"{step_name} — cumulative BOM"
        suffix = ""

    if args.json:
        text = to_json(entries, step=step_name)
        ext = "json"
    else:
        text = to_markdown(entries, title=title)
        ext = "md"

    if args.write or write_only:
        BOM_DIR.mkdir(parents=True, exist_ok=True)
        path = BOM_DIR / f"{step_name}{suffix}.{ext}"
        path.write_text(text)
        print(f"  wrote {path}")
    else:
        print(text, end="")


if __name__ == "__main__":
    raise SystemExit(main())
