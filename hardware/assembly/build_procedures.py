"""Build & render every procedure in hardware/assembly/procedures.

Family-ordered batches run in separate processes so the OS reclaims OCCT
memory after each batch. Two batches run concurrently by default; ``--jobs``
allows 1–4 workers (about 1.2 GB each for heavy steps). The output lock prevents
two dispatchers from writing the same tree.

The dispatcher retries incomplete stems up to ``MAX_STEM_RETRIES`` after
native HLR crashes. ``--only-missing`` retains finished outputs, and exact
BREP checkpoints let unfinished views resume without rebuilding geometry.
See hardware/README.md for the upstream fault. Workers line-buffer progress
and write native stacks to ``output/crash.log``.

Both variants export STEP; only declared ``views`` render SVG. Matching
annotation patches are replayed after rendering. Optional ``--bom`` and
``--bom-delta`` outputs are written first, warming the leaf cache and surviving
render crashes.

Unchanged stems restore from ``output/.cache``; patch-only edits replay their
annotations without rebuilding geometry. ``--no-cache`` forces a fresh build
and refreshes the cache. See ``assembly/cache.py`` for invalidation rules.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.build_procedures
    uv run --group cad python -m hardware.assembly.build_procedures --bom
    uv run --group cad python -m hardware.assembly.build_procedures --bom --bom-delta

Worker mode (invoked internally by the dispatcher; crash retries add
``--only-missing`` to skip outputs already on disk):

    uv run --group cad python -m hardware.assembly.build_procedures --stems frame_10_extrusion_tnut frame_20_SHCS ...
"""

from __future__ import annotations

import argparse
import faulthandler
import os
import shutil
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path

from hardware.assembly import cache as stepcache
from hardware.assembly.base import BaseAssembly
from hardware.assembly.bom import write_bom  # only for the optional --bom feature

# Procedure ordering & batching live in dispatch.py so the BOM and render
# pipelines share one source of truth.
from hardware.assembly.dispatch import (
    DEFAULT_BATCH_SIZE,
    MAX_STEM_RETRIES,
    _batches,
    load_class,
    retry_stems,
)
from hardware.assembly.mark.patch import patch_path
from hardware.assembly.mark.replay import replay_one
from hardware.build_lock import inherited_fds, locked_main
from hardware.scheme import (
    BOM_DIR,
    OUTPUT_DIR,
    RENDER_WORK_DIR,
    REPO_ROOT,
    STEP_DIR,
    SVG_DIR,
    VARIANTS,
    family_of,
    step_path_for,
    svg_path_for,
)

# Worker faulthandler target. The OCCT HLR SIGSEGV is routine, and its full
# dump (thread stack + extension-module list) would bury the progress table
# on every crash — so workers write it here instead of stderr. Fresh per
# dispatch run (the dispatcher unlinks it up front).
CRASH_LOG = OUTPUT_DIR / "crash.log"


def _shown(path: Path) -> Path:
    return path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path


def _clear_outputs(clear_bom: bool = False) -> None:
    """Wipe stale .step / .svg artifacts (and .md BOMs when ``clear_bom``)
    so deleted procedures and renamed variants don't survive across runs.
    Also sweeps in-progress .tmp files a SIGSEGV'd render or export left
    behind.
    Only touches the file extensions we generate — user-placed files in the
    output dirs are left alone."""
    shutil.rmtree(RENDER_WORK_DIR, ignore_errors=True)
    targets = [
        (STEP_DIR, "*_exploded.step"),
        (STEP_DIR, "*_assembled.step"),
        (STEP_DIR, "*.tmp"),
        (SVG_DIR, "*.svg"),
        (SVG_DIR, "*.tmp"),
    ]
    if clear_bom:
        targets.append((BOM_DIR, "*.md"))
        targets.append((BOM_DIR, "*.tmp"))
    cleared = 0
    for d, pattern in targets:
        if not d.exists():
            continue
        for f in d.glob(pattern):
            f.unlink()
            cleared += 1
    kinds = ".step / .svg / .tmp" + (" / .md" if clear_bom else "")
    print(f"Cleared {cleared} stale {kinds} file(s)\n")


def _run_one(
    cls: type[BaseAssembly], exploded: bool, *, only_missing: bool = False
) -> tuple[float, float, float] | None:
    """Build, export and render one variant; return timings or None if complete.

    ``only_missing`` skips atomic outputs already on disk. Render recovery
    uses its checkpoint when available, otherwise rebuilds the geometry.
    """
    asm = cls(exploded=exploded)
    svgs = [asm.svg_path(index=i) for i in asm.view_indices]
    step_done = only_missing and asm.output_path().exists()
    if step_done and all(p.exists() for p in svgs):
        return None
    t_build = 0.0
    t_export = 0.0
    if not step_done:
        t0 = time.monotonic()
        asm.build()
        t_build = time.monotonic() - t0
        t0 = time.monotonic()
        asm.export()
        t_export = time.monotonic() - t0
    t0 = time.monotonic()
    asm.render(only_missing=only_missing)
    t_render = time.monotonic() - t0
    return t_build, t_export, t_render


def _missing_boms(stem: str, *, bom: bool, bom_delta: bool) -> bool:
    """Any requested BOM .md for ``stem`` absent from output/bom/? Used by
    the ``--only-missing`` retry path: BOM presence is trustworthy there
    because the dispatcher cleared the stem's outputs (BOMs included)
    before its first attempt, so a file on disk is from this build cycle."""
    wanted = [BOM_DIR / f"{stem}.md"] if bom else []
    if bom_delta:
        wanted.append(BOM_DIR / f"{stem}_delta.md")
    return any(not p.exists() for p in wanted)


def _replay_patches_for(stem: str, exploded: bool) -> int:
    """For every SVG just rendered for this procedure variant, replay
    any matching patch JSON in ``hardware/assembly/patch/``. Returns
    the total number of patch-leaf snapshots written (0 if none of the
    rendered SVGs has a patch). Annotations are required manual inputs;
    a failed replay must fail the build."""
    variant = "exploded" if exploded else "assembled"
    written = 0
    for svg in sorted(SVG_DIR.glob(f"{stem}_{variant}_cam*.svg")):
        if not patch_path(svg).exists():
            continue
        written += len(replay_one(svg))
    return written


def _build_stems(
    stems: list[str],
    *,
    bom: bool = False,
    bom_delta: bool = False,
    only_missing: bool = False,
) -> int:
    classes = [(stem, load_class(stem)) for stem in stems]

    name_w = max(len(name) for name, _ in classes)
    runs = len(classes) * 2
    failures: list[tuple[str, str, BaseException]] = []
    sums = {"build": 0.0, "export": 0.0, "render": 0.0}
    t_wall0 = time.monotonic()

    print(f"Building {len(classes)} procedures × 2 variants = {runs} runs\n")
    print(
        f"  {'procedure':<{name_w}}  {'variant':<9}  "
        f"{'build':>7}  {'export':>7}  {'render':>7}  {'total':>7}  status"
    )
    print(
        f"  {'-' * name_w}  {'-' * 9}  {'-' * 7}  {'-' * 7}  {'-' * 7}  {'-' * 7}  ------"
    )

    for short, cls in classes:
        # Save BOMs before entering the crash-prone renderer. This also warms
        # the leaf cache; a render retry can then use its BREP checkpoint and
        # existing BOM without reconstructing the assembly in a new process.
        if bom or bom_delta:
            if only_missing and not _missing_boms(short, bom=bom, bom_delta=bom_delta):
                print(f"  {short:<{name_w}}  bom        skip (on disk)")
            else:
                try:
                    write_bom(short, cumulative=bom, want_delta=bom_delta)
                    kinds = " + ".join(
                        k for k, on in (("cumulative", bom), ("delta", bom_delta)) if on
                    )
                    print(f"  {short:<{name_w}}  bom        wrote {kinds}")
                except Exception as exc:
                    failures.append((short, "bom", exc))
                    print(
                        f"  {short:<{name_w}}  bom        FAIL {type(exc).__name__}: {exc}"
                    )
        for variant, exploded in _VARIANTS:
            try:
                timings = _run_one(cls, exploded, only_missing=only_missing)
                if timings is None:
                    status = f"{'-':>7}  {'-':>7}  {'-':>7}  {'-':>7}  skip (on disk)"
                else:
                    tb, te, tr = timings
                    sums["build"] += tb
                    sums["export"] += te
                    sums["render"] += tr
                    status = (
                        f"{tb:6.2f}s  {te:6.2f}s  {tr:6.2f}s  {tb + te + tr:6.2f}s  ok"
                    )
                # Replay even on a skip: a crash may have landed the last SVG
                # before its annotations. Replay is idempotent and geometry-free.
                n_patches = _replay_patches_for(short, exploded)
                patch_tag = f"  +{n_patches} patch snap" if n_patches else ""
                print(f"  {short:<{name_w}}  {variant:<9}  {status}{patch_tag}")
            except Exception as exc:
                failures.append((short, variant, exc))
                msg = f"FAIL {type(exc).__name__}: {exc}"
                print(
                    f"  {short:<{name_w}}  {variant:<9}  "
                    f"{'-':>7}  {'-':>7}  {'-':>7}  {'-':>7}  {msg}"
                )

    wall = time.monotonic() - t_wall0
    cpu = sum(sums.values())
    print(
        f"\n{runs - len(failures)}/{runs} OK   "
        f"wall {wall:.1f}s   "
        f"build {sums['build']:.1f}s  export {sums['export']:.1f}s  render {sums['render']:.1f}s   "
        f"(cpu/wall = {cpu / wall:.2f})"
    )

    if failures:
        print("\nTracebacks:")
        for short, variant, exc in failures:
            print(f"\n--- {short} ({variant}) ---")
            traceback.print_exception(type(exc), exc, exc.__traceback__)
        return 1
    return 0


def _run_subprocess(
    stems: list[str],
    bom_flags: tuple[str, ...] = (),
    *,
    only_missing: bool = False,
) -> int:
    try:
        return subprocess.call(
            [
                sys.executable,
                "-m",
                "hardware.assembly.build_procedures",
                "--stems",
                *stems,
                *bom_flags,
                *(("--only-missing",) if only_missing else ()),
            ],
            pass_fds=inherited_fds(),
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        print(f"Worker timed out after 900s: {', '.join(stems)}")
        return 124


_VARIANTS = tuple((name, name == "exploded") for name in VARIANTS)


@cache
def _view_indices(stem: str, exploded: bool) -> tuple[int, ...]:
    """Camera indices this stem renders for this variant — i.e. which
    ``_cam<i>`` SVGs the variant should produce. Read per variant because
    a procedure may set ``self.camera`` differently for exploded vs
    assembled (e.g. frame_10_extrusion_tnut) and ``views`` declares each
    variant separately. Reads the same ``BaseAssembly.view_indices`` that
    ``render()`` iterates, so producer and verifier stay in lockstep."""
    return tuple(load_class(stem)(exploded=exploded).view_indices)


def _missing_variants(stem: str) -> list[str]:
    """Variant names ("exploded" / "assembled") with the STEP or any
    declared-view SVG missing on disk for this stem. A variant is complete
    only when ALL its outputs exist: checking just ``_cam0`` would miss a
    crash *between* cameras — exactly the OCCT HLR SIGSEGV the retry
    exists to recover from — and a variant that declares no views still
    owes its STEP. Both writers are atomic, so presence means complete."""
    return [
        name
        for name, exploded in _VARIANTS
        if not step_path_for(stem, exploded).exists()
        or any(
            not svg_path_for(stem, exploded, index=i).exists()
            for i in _view_indices(stem, exploded)
        )
    ]


def _incomplete(stem: str, bom_flags: tuple[str, ...]) -> list[str]:
    """Missing STEP/SVG variants, requested BOMs, and annotation snapshots.

    Presence is sufficient here: writers are atomic, and the dispatcher
    clears each miss stem's outputs before its first build attempt.
    """
    missing = list(_missing_variants(stem))
    if _missing_boms(
        stem, bom="--bom" in bom_flags, bom_delta="--bom-delta" in bom_flags
    ):
        missing.append("bom")
    from hardware.assembly.mark.patch import load_patch, snapshot_path
    from hardware.assembly.mark.replay import find_leaves

    for variant, exploded in _VARIANTS:
        for index in _view_indices(stem, exploded):
            svg = svg_path_for(stem, exploded, index=index)
            if patch_path(svg).exists():
                if any(
                    not snapshot_path(svg, leaf["id"]).is_file()
                    for leaf in find_leaves(load_patch(svg))
                ):
                    missing.append(f"{variant} annotations")
    return missing


def _dispatch(
    batch_size: int,
    bom_flags: tuple[str, ...] = (),
    use_cache: bool = True,
    *,
    jobs: int = 1,
) -> int:
    """Build every batch, skipping steps the cache already covers.

    Per stem (see ``assembly/cache.py``): if the *source* layer is fresh
    (.step / raw .svg / BOM unchanged) restore it instead of rebuilding,
    then either restore the patch snapshots (patch unchanged) or just
    re-run the cheap annotation replay (patch edited — geometry stays
    cached). Stems with no cache hit go to a worker subprocess in family
    batches; crashed stems solo-retry. ``bom_flags`` (--bom / --bom-delta)
    pass through so workers also emit BOMs."""
    want_bom = bool(bom_flags)
    CRASH_LOG.unlink(missing_ok=True)  # this run's workers append fresh
    # With the cache on, don't wipe everything up front (it would just be
    # restored again) — the cache owns each stem's freshness, and only the
    # miss stems are cleared, right before they rebuild. A --no-cache run
    # still wipes all stale outputs so deleted/renamed steps don't survive.
    if not use_cache:
        _clear_outputs(clear_bom=want_bom)

    batches = _batches(batch_size)
    position = {s: i + 1 for i, s in enumerate(s for b in batches for s in b)}
    total = len(position)

    # Classify each stem once (source_cached verifies the entry's hashes — not
    # something to run twice); the loop below reuses this map.
    cached: dict[str, bool] = {}
    if use_cache:
        pruned = stepcache.prune(position)
        cached = {
            s: stepcache.source_cached(
                s,
                want_bom="--bom" in bom_flags,
                want_delta="--bom-delta" in bom_flags,
                expected_svgs={
                    svg_path_for(s, exploded, index=i).name
                    for _, exploded in _VARIANTS
                    for i in _view_indices(s, exploded)
                },
            )
            for s in position
        }
        n_cached = sum(cached.values())
        print(
            f"cache: {_shown(stepcache.CACHE_DIR)} — "
            f"{n_cached}/{total} cached, {total - n_cached} to build"
            + (f"   (pruned {pruned} stale)" if pruned else "")
        )
    t_wall0 = time.monotonic()

    def header(stems: list[str]) -> str:
        start, end = position[stems[0]], position[stems[-1]]
        progress = f"{start}/{total}" if start == end else f"{start}-{end}/{total}"
        tail = stems[0] if start == end else f"{stems[0]} … {stems[-1]}"
        return f"\n=== [{progress}] {family_of(stems[0])}: {tail} ==="

    n_fresh = n_replay = 0
    pending = []
    for batch in batches:
        misses = []
        for s in batch:
            if not cached.get(s):
                misses.append(s)
                continue
            stepcache.clear_outputs(s)
            stepcache.restore_source(s, want_bom=want_bom)
            if stepcache.snapshots_cached(s):
                stepcache.restore_snapshots(s)
                n_fresh += 1
            else:
                # Patch edited but geometry/render unchanged: keep the cached
                # .step + raw .svg, just re-apply the (build123d-free) replay.
                # clear_outputs already removed the old snapshots, so removed
                # or renamed patch operations cannot survive in the new layer.
                print(f"  [patch] {s}: geometry cached, re-replaying annotations")
                for exploded in (True, False):
                    _replay_patches_for(s, exploded)
                stepcache.store_snapshots(s)
                n_replay += 1

        if not misses:
            continue
        if use_cache:
            # Clear each miss stem's own outputs so a rebuild that now renders
            # fewer cameras/variants leaves no stale file behind.
            for s in misses:
                stepcache.clear_outputs(s)
        pending.append(misses)

    def build_batch(misses: list[str]) -> bool:
        print(header(misses))
        rc = _run_subprocess(misses, bom_flags)
        if rc != 0:
            # Nondeterministic HLR SIGSEGV: re-run each incomplete stem in a
            # fresh process (new heap layout) until its outputs land or we
            # exhaust the retries.
            incomplete = [s for s in misses if _incomplete(s, bom_flags)]
            print(
                f"\n--- batch exit {rc}; retrying {len(incomplete)}/{len(misses)} "
                f"incomplete stem(s)  [stacks → {_shown(CRASH_LOG)}] ---"
            )
            retry_stems(
                incomplete,
                run=lambda s: _run_subprocess([s], bom_flags, only_missing=True),
                done=lambda s: not _incomplete(s, bom_flags),
                log=lambda s, a: print(
                    header([s]) + f"  (retry {a}/{MAX_STEM_RETRIES})"
                ),
            )
            # Exceptions after the last output landed (e.g. annotation
            # replay) must never be converted to success by file presence.
            # Re-run a cheap only-missing pass and require a successful exit.
            completed = [s for s in misses if not _incomplete(s, bom_flags)]
            if completed and _run_subprocess(completed, bom_flags, only_missing=True):
                return False
        # Store every stem that completed — also on --no-cache runs: the
        # flag bypasses cache READS, but the fresh rebuild is exactly what
        # is worth storing; skipping the store would make the next cached
        # run rebuild all steps a second time.
        for s in misses:
            if not _incomplete(s, bom_flags):
                stepcache.store_source(s)
                stepcache.store_snapshots(s)
        return True

    # Threads only supervise independent child processes; OCCT never runs
    # concurrently within one interpreter. Each batch owns distinct stems.
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        results = list(executor.map(build_batch, pending))
    if not all(results):
        return 1

    wall = time.monotonic() - t_wall0
    failed_variants = [(s, v) for s in position for v in _incomplete(s, bom_flags)]
    ok_assemblies = total - len({s for s, _ in failed_variants})
    n_step = len(list(STEP_DIR.glob("*.step")))
    n_svg = len(list(SVG_DIR.glob("*.svg")))
    cache_note = (
        f"   cache: {n_fresh} hit + {n_replay} patch-replay" if use_cache else ""
    )
    tally = (
        f"{ok_assemblies}/{total} assemblies   "
        f"wrote {n_step} .step / {n_svg} .svg{cache_note}   "
        f"total wall {wall:.1f}s"
    )
    if failed_variants:
        print(f"\nFAILED variants ({len(failed_variants)}):")
        for stem, variant in failed_variants:
            print(f"  {stem} {variant}")
        print(tally)
        return 1
    print(f"\nAll OK   {tally}")
    return 0


@locked_main
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
        "--jobs",
        type=int,
        choices=range(1, 5),
        default=min(2, os.cpu_count() or 1),
        help="concurrent CAD batch processes (1–4; default 2, use 1 for low memory)",
    )
    parser.add_argument(
        "--bom-delta",
        action="store_true",
        help="also write each procedure's delta-vs-predecessor BOM to output/bom/",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="don't read the persistent step cache — rebuild & re-render every "
        "step (the rebuilt outputs still refresh the cache)",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="skip variants/cameras whose outputs are already on disk "
        "(worker mode; the dispatcher's crash retries pass this)",
    )
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    if args.only_missing and not args.stems:
        parser.error("--only-missing requires --stems")

    # Line-buffer stdout: under a pipe (make … | tee, CI) the default is
    # block buffering, which holds the dispatcher's progress until exit and
    # loses a crashed worker's entire report with the SIGSEGV.
    sys.stdout.reconfigure(line_buffering=True)

    if args.stems:
        # Route crash forensics to CRASH_LOG instead of stderr; the header
        # line ties each dumped stack to the worker's stems. Deliberately
        # not a ``with`` block: faulthandler writes to this fd at signal
        # time, so it must stay open for the life of the process.
        CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        crash_log = CRASH_LOG.open("a")
        crash_log.write(
            f"=== {time.strftime('%H:%M:%S')} worker --stems {' '.join(args.stems)}\n"
        )
        crash_log.flush()
        faulthandler.enable(file=crash_log)
        return _build_stems(
            args.stems,
            bom=args.bom,
            bom_delta=args.bom_delta,
            only_missing=args.only_missing,
        )

    bom_flags = tuple(
        f for f, on in (("--bom", args.bom), ("--bom-delta", args.bom_delta)) if on
    )
    return _dispatch(
        args.batch_size, bom_flags, use_cache=not args.no_cache, jobs=args.jobs
    )


if __name__ == "__main__":
    sys.exit(main())
