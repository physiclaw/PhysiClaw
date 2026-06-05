"""Build & render every procedure in hardware/assembly/procedures.

Procedures are grouped by family in dependency order and split into
``DEFAULT_BATCH_SIZE`` batches, each run in its own subprocess so the
OS reclaims all OCCT memory on worker exit. OCCT's allocator here is
native malloc, which never returns freed memory to the OS, so a single
all-in-one process balloons across all the procedures and gets
OOM-killed; per-batch subprocesses bound peak RSS.

Exact HLR rendering (``project_to_viewport`` → ``HLRBRep_Algo``)
intermittently SIGSEGVs — a nondeterministic OCCT hazard, not an OOM.
A crashed stem almost always succeeds when re-run in a fresh process,
so the dispatcher retries each incomplete stem solo (``MAX_STEM_RETRIES``).

After each variant renders, any patch JSON in
``hardware/assembly/patch/`` whose stem matches a rendered SVG is
replayed against it, so marker-tool snapshots stay in sync with the
underlying drawing.

Pass ``--bom`` / ``--bom-delta`` to also emit each procedure's BOM to
output/bom/ in the same pass — the build is already done, so collecting
the BOM is cheap off the warm leaf cache (one run yields STEP + SVG + BOM).

Run from the repo root:

    uv run --group cad python -m hardware.assembly.build_procedures
    uv run --group cad python -m hardware.assembly.build_procedures --bom
    uv run --group cad python -m hardware.assembly.build_procedures --bom --bom-delta

Worker mode (invoked internally by the dispatcher):

    uv run --group cad python -m hardware.assembly.build_procedures --stems frame_10_extrusion_tnut frame_20_SHCS ...
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import traceback
from functools import cache

from hardware.assembly.base import SVG_DIR, BaseAssembly, svg_path_for
from hardware.assembly.mark.patch import patch_path
from hardware.assembly.mark.replay import replay_one
# Procedure ordering & batching live in bom.py (next to list_procedures) so the
# BOM and render pipelines share one source of truth.
from hardware.assembly.dispatch import (
    DEFAULT_BATCH_SIZE,
    MAX_STEM_RETRIES,
    _batches,
    _family_of,
    load_class,
    retry_stems,
)
from hardware.bom.bom import BOM_DIR, write_bom  # only for the optional --bom feature
from hardware.parts.base import STEP_DIR


def _clear_outputs(clear_bom: bool = False) -> None:
    """Wipe stale .step / .svg artifacts (and .md BOMs when ``clear_bom``)
    so deleted procedures and renamed variants don't survive across runs.
    Only touches the file extensions we generate — user-placed files in the
    output dirs are left alone."""
    targets = [(STEP_DIR, "*.step"), (SVG_DIR, "*.svg")]
    if clear_bom:
        targets.append((BOM_DIR, "*.md"))
    cleared = 0
    for d, pattern in targets:
        if not d.exists():
            continue
        for f in d.glob(pattern):
            f.unlink()
            cleared += 1
    kinds = ".step / .svg" + (" / .md" if clear_bom else "")
    print(f"Cleared {cleared} stale {kinds} file(s)\n")


def _run_one(cls: type[BaseAssembly], exploded: bool) -> tuple[float, float, float]:
    asm = cls(exploded=exploded)
    t0 = time.monotonic(); asm.build();  t_build  = time.monotonic() - t0
    t0 = time.monotonic(); asm.export(); t_export = time.monotonic() - t0
    t0 = time.monotonic(); asm.render(); t_render = time.monotonic() - t0
    return t_build, t_export, t_render


def _replay_patches_for(stem: str, exploded: bool) -> int:
    """For every SVG just rendered for this procedure variant, replay
    any matching patch JSON in ``hardware/assembly/patch/``. Returns
    the total number of patch-leaf snapshots written (0 if none of the
    rendered SVGs has a patch). Per-source failures are logged but
    don't propagate — patches are user annotations, not build outputs."""
    variant = "exploded" if exploded else "assembled"
    written = 0
    for svg in sorted(SVG_DIR.glob(f"{stem}_{variant}_cam*.svg")):
        if not patch_path(svg).exists():
            continue
        try:
            written += len(replay_one(svg))
        except Exception as exc:  # malformed patch / I/O — keep building
            print(f"    WARN: patch replay failed for {svg.name}: "
                  f"{type(exc).__name__}: {exc}")
    return written


def _build_stems(stems: list[str], *, bom: bool = False, bom_delta: bool = False) -> int:
    classes = [(stem, load_class(stem)) for stem in stems]

    name_w   = max(len(name) for name, _ in classes)
    runs     = len(classes) * 2
    failures: list[tuple[str, str, BaseException]] = []
    sums     = {"build": 0.0, "export": 0.0, "render": 0.0}
    t_wall0  = time.monotonic()

    print(f"Building {len(classes)} procedures × 2 variants = {runs} runs\n")
    print(
        f"  {'procedure':<{name_w}}  {'variant':<9}  "
        f"{'build':>7}  {'export':>7}  {'render':>7}  {'total':>7}  status"
    )
    print(f"  {'-' * name_w}  {'-'*9}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*7}  ------")

    for short, cls in classes:
        for exploded in (True, False):
            variant = "exploded" if exploded else "assembled"
            try:
                tb, te, tr = _run_one(cls, exploded)
                sums["build"]  += tb
                sums["export"] += te
                sums["render"] += tr
                n_patches = _replay_patches_for(short, exploded)
                patch_tag = f"  +{n_patches} patch snap" if n_patches else ""
                print(
                    f"  {short:<{name_w}}  {variant:<9}  "
                    f"{tb:6.2f}s  {te:6.2f}s  {tr:6.2f}s  {tb+te+tr:6.2f}s  ok"
                    f"{patch_tag}"
                )
            except Exception as exc:
                failures.append((short, variant, exc))
                msg = f"FAIL {type(exc).__name__}: {exc}"
                print(
                    f"  {short:<{name_w}}  {variant:<9}  "
                    f"{'-':>7}  {'-':>7}  {'-':>7}  {'-':>7}  {msg}"
                )

        # BOM (cumulative / delta) from the same warm build — nearly free.
        if bom or bom_delta:
            try:
                write_bom(short, cumulative=bom, want_delta=bom_delta)
                kinds = " + ".join(k for k, on in (("cumulative", bom), ("delta", bom_delta)) if on)
                print(f"  {short:<{name_w}}  bom        wrote {kinds}")
            except Exception as exc:
                print(f"  {short:<{name_w}}  bom        FAIL {type(exc).__name__}: {exc}")

    wall = time.monotonic() - t_wall0
    cpu  = sum(sums.values())
    print(
        f"\n{runs - len(failures)}/{runs} OK   "
        f"wall {wall:.1f}s   "
        f"build {sums['build']:.1f}s  export {sums['export']:.1f}s  render {sums['render']:.1f}s   "
        f"(cpu/wall = {cpu/wall:.2f})"
    )

    if failures:
        print("\nTracebacks:")
        for short, variant, exc in failures:
            print(f"\n--- {short} ({variant}) ---")
            traceback.print_exception(type(exc), exc, exc.__traceback__)
        return 1
    return 0


def _run_subprocess(stems: list[str], bom_flags: tuple[str, ...] = ()) -> int:
    return subprocess.call([
        sys.executable, "-m", "hardware.assembly.build_procedures",
        "--stems", *stems, *bom_flags,
    ])


_VARIANTS = (("exploded", True), ("assembled", False))


@cache
def _camera_count(stem: str, exploded: bool) -> int:
    """How many cameras this stem renders for this variant — i.e. how many
    SVGs (``_cam0`` … ``_camN-1``) the variant should produce. Counted per
    variant because a procedure may set ``self.camera`` differently for
    exploded vs assembled (e.g. frame_10_extrusion_tnut). Reads the same
    ``BaseAssembly.cameras`` that ``render()`` iterates, so producer and
    verifier stay in lockstep."""
    return len(load_class(stem)(exploded=exploded).cameras)


def _missing_variants(stem: str) -> list[str]:
    """Variant names ("exploded" / "assembled") with any SVG missing on disk
    for this stem. ``_run_one`` writes STEP first then the SVGs, and a
    multi-camera assembly emits one per camera (``_cam0`` … ``_camN``), so a
    variant is complete only when ALL its cameras' SVGs exist. Checking just
    ``_cam0`` would miss a crash *between* cameras — exactly the OCCT HLR
    SIGSEGV that the retry exists to recover from."""
    return [name for name, exploded in _VARIANTS
            if any(not svg_path_for(stem, exploded, index=i).exists()
                   for i in range(_camera_count(stem, exploded)))]


def _dispatch(batch_size: int, bom_flags: tuple[str, ...] = ()) -> int:
    """Run each batch in its own subprocess; on failure, solo-retry
    only the stems missing outputs. ``bom_flags`` (--bom / --bom-delta)
    are passed through so workers also emit BOMs."""
    _clear_outputs(clear_bom=bool(bom_flags))

    batches = _batches(batch_size)
    position = {s: i + 1 for i, s in enumerate(s for b in batches for s in b)}
    total = len(position)
    t_wall0 = time.monotonic()

    def header(stems: list[str]) -> str:
        start, end = position[stems[0]], position[stems[-1]]
        progress = f"{start}/{total}" if start == end else f"{start}-{end}/{total}"
        tail = stems[0] if start == end else f"{stems[0]} … {stems[-1]}"
        return f"\n=== [{progress}] {_family_of(stems[0])}: {tail} ==="

    for batch in batches:
        print(header(batch))
        rc = _run_subprocess(batch, bom_flags)
        if rc == 0:
            continue
        # Nondeterministic HLR SIGSEGV: re-run each incomplete stem in a
        # fresh process (new heap layout) until its outputs land or we
        # exhaust the retries.
        incomplete = [s for s in batch if _missing_variants(s)]
        print(f"\n--- batch exit {rc}; retrying {len(incomplete)}/{len(batch)} incomplete stem(s) ---")
        retry_stems(
            incomplete,
            run=lambda s: _run_subprocess([s], bom_flags),
            done=lambda s: not _missing_variants(s),
            log=lambda s, a: print(header([s]) + f"  (retry {a}/{MAX_STEM_RETRIES})"),
        )

    wall = time.monotonic() - t_wall0
    failed_variants = [(s, v) for s in position for v in _missing_variants(s)]
    ok_assemblies = sum(1 for s in position if not _missing_variants(s))
    n_step = len(list(STEP_DIR.glob("*.step")))
    n_svg  = len(list(SVG_DIR.glob("*.svg")))
    tally  = (
        f"{ok_assemblies}/{total} assemblies   "
        f"wrote {n_step} .step / {n_svg} .svg   "
        f"total wall {wall:.1f}s"
    )
    if failed_variants:
        print(f"\nFAILED variants ({len(failed_variants)}):")
        for stem, variant in failed_variants:
            print(f"  {stem} {variant}")
        print(tally)
        return 1
    print(f"\nAll {len(batches)} batches OK   {tally}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stems",
        nargs="+",
        help="procedure stems to build (worker mode); omit to dispatch all batches as subprocesses",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"max procedures per subprocess in dispatcher mode (default {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--bom",
        action="store_true",
        help="also write each procedure's cumulative BOM to output/bom/",
    )
    parser.add_argument(
        "--bom-delta",
        action="store_true",
        help="also write each procedure's delta-vs-predecessor BOM to output/bom/",
    )
    args = parser.parse_args(argv)

    if args.stems:
        return _build_stems(args.stems, bom=args.bom, bom_delta=args.bom_delta)

    bom_flags = tuple(
        f for f, on in (("--bom", args.bom), ("--bom-delta", args.bom_delta)) if on
    )
    return _dispatch(args.batch_size, bom_flags)


if __name__ == "__main__":
    sys.exit(main())
