"""Build & render every procedure in hardware/assembly/procedures.

Walks the procedures namespace via `hardware.bom.bom.list_procedures`,
imports each module, picks out the BaseAssembly subclass it defines,
and invokes .export() (STEP) and .render() (SVG) for both exploded
and assembled variants. Per-run timings are reported so the assembly-
state cache's effect is visible.

Modules are visited in dependency order — lower-level layers
(frame → idler → motor → linear → belt → tapz) first, so each higher
layer hits a warm `_BUILD_CACHE` when it embeds its predecessors.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.build_procedures
"""

from __future__ import annotations

import sys
import time
import traceback

from hardware.assembly.base import SVG_DIR, BaseAssembly
from hardware.bom.bom import list_procedures, load_step
from hardware.parts.base import STEP_DIR

# Family priority — lower index = built first.
_FAMILY_PRIORITY = {
    name: i for i, name in enumerate(
        ("frame", "idler", "motor", "linear", "belt", "tapz")
    )
}


def _ordered_stems() -> list[str]:
    """All procedure stems, sorted by dependency-order family then alphabetical."""
    fallback = len(_FAMILY_PRIORITY)
    return sorted(
        list_procedures(),
        key=lambda s: (_FAMILY_PRIORITY.get(s.split("_", 1)[0], fallback), s),
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


def main() -> int:
    stems = _ordered_stems()
    if not stems:
        print("No procedure modules found.")
        return 1
    _clear_outputs()
    # load_step returns an instance; we use type(...) to recover the
    # class so we can iterate both exploded values without a second
    # import. Cheap — load_step doesn't call .build().
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
                print(
                    f"  {short:<{name_w}}  {variant:<9}  "
                    f"{tb:6.2f}s  {te:6.2f}s  {tr:6.2f}s  {tb+te+tr:6.2f}s  ok"
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


if __name__ == "__main__":
    sys.exit(main())
