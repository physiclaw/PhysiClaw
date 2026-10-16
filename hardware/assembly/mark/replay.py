"""Replay patch JSONs against freshly-regenerated source SVGs.

For each patch file at ``hardware/assembly/patch/<stem>.json``, walks the
op chain and writes one snapshot per **leaf** op — an op that no other
op references as its ``preop``. Intermediate ops are applied in memory
to build each leaf's chain but not written to disk: in a chain
``A on orig → B on A``, only ``B`` lands on disk.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.mark.replay
    uv run --group cad python -m hardware.assembly.mark.replay <patch.json>
    uv run --group cad python -m hardware.assembly.mark.replay <source.svg>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List

from hardware import buildlog
from hardware.assembly.mark.patch import (
    ORIG_SENTINEL,
    load_patch,
    patch_path,
    snapshot_path,
    source_for_patch,
)
from hardware.assembly.mark.svg import build_shapes_svg
from hardware.scheme import PATCH_DIR


def find_leaves(entries: list[dict]) -> list[dict]:
    """Ops that no other op references as its ``preop``."""
    referenced = {e["preop"] for e in entries}
    return [e for e in entries if e["id"] not in referenced]


def chain_to(entries: list[dict], leaf_id: str) -> list[dict]:
    """Ops from the original down to ``leaf_id`` (inclusive), in order
    of application. Raises on a broken chain or a cycle."""
    by_id = {e["id"]: e for e in entries}
    chain: list[dict] = []
    seen: set[str] = set()
    cur = leaf_id
    while cur != ORIG_SENTINEL:
        if cur in seen:
            raise ValueError(f"cycle detected at op {cur!r}")
        seen.add(cur)
        op = by_id.get(cur)
        if op is None:
            raise ValueError(f"unknown op id {cur!r} (broken preop chain)")
        chain.append(op)
        cur = op["preop"]
    chain.reverse()
    return chain


def apply_chain(src_bytes: bytes, chain: list[dict]) -> bytes:
    """Fold each op's shapes + viewBox onto ``src_bytes`` in order.
    Each shape carries its own ``color`` + ``outlined`` so
    ``build_shapes_svg`` needs no extra arg."""
    out = src_bytes
    for op in chain:
        out = build_shapes_svg(out, op["shapes"], viewbox=op["viewBox"])
    return out


def apply_upto(entries: list[dict], src_bytes: bytes, upto: str) -> bytes:
    """Composite ``src_bytes`` up to (and including) op ``upto``;
    ``upto == "orig"`` returns the source unchanged."""
    if upto == ORIG_SENTINEL:
        return src_bytes
    return apply_chain(src_bytes, chain_to(entries, upto))


def replay_one(src: Path) -> List[Path]:
    """Walk ``patch_path(src)``'s ops, write a snapshot for each leaf."""
    entries = load_patch(src)
    src_bytes = src.read_bytes()
    written: List[Path] = []
    for leaf in find_leaves(entries):
        chain = chain_to(entries, leaf["id"])
        out_bytes = apply_chain(src_bytes, chain)
        out = snapshot_path(src, leaf["id"])
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(out_bytes)
        tmp.replace(out)
        written.append(out)
    return written


def _resolve_targets(argv: list[str]) -> list[tuple[Path, Path]]:
    """Return ``(source_svg, patch_json)`` pairs to replay."""
    if len(argv) == 1:
        patches = sorted(PATCH_DIR.glob("*.json"))
    else:
        p = Path(argv[1]).expanduser().resolve()
        if p.suffix == ".svg":
            patches = [patch_path(p)]
        elif p.suffix == ".json":
            patches = [p]
        else:
            raise SystemExit(f"unsupported: {p}")

    pairs: list[tuple[Path, Path]] = []
    for pj in patches:
        pairs.append((source_for_patch(pj), pj))
    return pairs


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", nargs="?")
    buildlog.add_arguments(parser)
    args = parser.parse_args(argv[1:])
    with buildlog.command("Hardware annotation replay", args, name="replay") as run:
        pairs = _resolve_targets([argv[0], *([args.file] if args.file else [])])
        if not pairs:
            raise ValueError("No patch files found")
        written_all = []
        for src, pj in pairs:
            if not pj.exists() or not src.exists():
                buildlog.event(
                    "replay_failed",
                    f"Missing patch: {pj}"
                    if not pj.exists()
                    else f"Missing source: {src}",
                    level="error",
                    patch=str(pj),
                    source=str(src),
                )
                run.exit_code = 1
                continue
            with buildlog.step(pj.stem):
                written = replay_one(src)
            written_all.extend(written)
        run.artifacts(written_all)
        run.summary = f"{len(written_all)} snapshots"
        return run.exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
