"""Build & render every procedure in hardware/assembly/procedures.

Procedures are grouped by family (frame → idler → motor → linear →
belt → tapz) in dependency order. Each family is split into batches
of up to five procedures and each batch runs in its own subprocess —
OCCT memory accumulated during a batch is returned to the OS before
the next batch starts. A single all-in-one process accumulates many
GB of geometry across 42 procedures and gets SIGKILL'd around the
linear stages; batching at five-per-subprocess keeps every batch
well under the OOM threshold while still letting the in-batch cache
warm.

After each variant renders, any patch JSON in
``hardware/assembly/patch/`` whose stem matches a rendered SVG is
replayed against it, so marker-tool snapshots stay in sync with the
underlying drawing.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.build_procedures

Worker mode (invoked internally by the dispatcher):

    uv run --group cad python -m hardware.assembly.build_procedures --stems frame_10_extrusion_tnut frame_20_SHCS ...
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import traceback
from itertools import groupby

from hardware.assembly.base import SVG_DIR, BaseAssembly, svg_path_for
from hardware.assembly.mark.patch import patch_path
from hardware.assembly.mark.replay import replay_one
from hardware.bom.bom import list_procedures, load_step
from hardware.parts.base import STEP_DIR

# Family priority — lower index = built first.
_FAMILIES = ("frame", "idler", "motor", "linear", "belt", "tapz")
_FAMILY_PRIORITY = {name: i for i, name in enumerate(_FAMILIES)}
DEFAULT_BATCH_SIZE = 5


def _family_of(stem: str) -> str:
    return stem.split("_", 1)[0]


def _ordered_stems() -> list[str]:
    """All procedure stems, sorted by dependency-order family then alphabetical."""
    fallback = len(_FAMILY_PRIORITY)
    return sorted(
        list_procedures(),
        key=lambda s: (_FAMILY_PRIORITY.get(_family_of(s), fallback), s),
    )


def _clear_outputs() -> None:
    """Wipe stale .step / .svg artifacts so deleted procedures and
    renamed variants don't survive across runs. Only touches the file
    extensions we generate — user-placed files in the output dirs are
    left alone."""
    cleared = 0
    for d, pattern in ((STEP_DIR, "*.step"), (SVG_DIR, "*.svg")):
        if not d.exists():
            continue
        for f in d.glob(pattern):
            f.unlink()
            cleared += 1
    print(f"Cleared {cleared} stale .step / .svg file(s)\n")


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


def _build_stems(stems: list[str]) -> int:
    classes = [(stem, type(load_step(stem))) for stem in stems]

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


def _batches(batch_size: int) -> list[list[str]]:
    """Family-clustered chunks of up to ``batch_size``, dependency order."""
    out: list[list[str]] = []
    for _, group in groupby(_ordered_stems(), key=_family_of):
        stems = list(group)
        for i in range(0, len(stems), batch_size):
            out.append(stems[i:i + batch_size])
    return out


def _run_subprocess(stems: list[str]) -> int:
    return subprocess.call([
        sys.executable, "-m", "hardware.assembly.build_procedures",
        "--stems", *stems,
    ])


_VARIANTS = (("exploded", True), ("assembled", False))


def _missing_variants(stem: str) -> list[str]:
    """Variant names ("exploded" / "assembled") whose SVG isn't on disk
    for this stem. ``_run_one`` writes STEP first then SVG, so SVG
    presence implies the full build/export/render finished for that
    variant."""
    return [name for name, exploded in _VARIANTS
            if not svg_path_for(stem, exploded).exists()]


def _dispatch(batch_size: int) -> int:
    """Run each batch in its own subprocess; on failure, solo-retry
    only the stems missing outputs."""
    _clear_outputs()

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
        rc = _run_subprocess(batch)
        if rc == 0 or len(batch) == 1:
            continue
        incomplete = [s for s in batch if _missing_variants(s)]
        print(f"\n--- batch exit {rc}; solo-retrying {len(incomplete)}/{len(batch)} incomplete stem(s) ---")
        for stem in incomplete:
            print(header([stem]))
            _run_subprocess([stem])

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
    args = parser.parse_args(argv)

    if args.stems:
        return _build_stems(args.stems)

    return _dispatch(args.batch_size)


if __name__ == "__main__":
    sys.exit(main())
