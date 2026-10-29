"""Build & render every procedure in hardware/assembly/procedures.

Family-ordered batches run in separate processes so the OS reclaims OCCT
memory after each batch. Two batches run concurrently by default; ``--jobs``
allows 1–4 workers (about 1.2 GB each for heavy steps). The output lock prevents
two dispatchers from writing the same tree.

The dispatcher retries incomplete stems up to ``MAX_STEM_RETRIES`` after
native HLR crashes. ``--only-missing`` retains finished outputs, and exact
BREP checkpoints let unfinished views resume without rebuilding geometry.
See hardware/README.md for the upstream fault. Workers line-buffer progress
and write native stacks to a per-run ``output/logs/build-*.crash.log``.

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
import signal
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from functools import cache
from pathlib import Path
from threading import Thread

from hardware import buildlog
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
from hardware.build_lock import locked_main
from hardware.port.processes import (
    CancellationEvent,
    cancel_on_interrupt,
    is_interrupted,
    owned_process,
)
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

# Fallback for internal worker invocations without a run session. Normal CLI
# builds inherit a unique crash-log path from the collector, preserved per run.
CRASH_LOG = OUTPUT_DIR / "crash.log"
WORKER_TIMEOUT = 900
WORKER_HEARTBEAT = 30
_CANCELLED = CancellationEvent()


def _check_cancelled() -> None:
    if _CANCELLED.is_set():
        raise KeyboardInterrupt


def _crash_log() -> Path:
    return Path(os.environ.get(buildlog.CRASH_LOG_ENV, CRASH_LOG))


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
    buildlog.event(
        "outputs_cleared", f"Removed {cleared} stale generated files", count=cleared
    )


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
    failures = 0
    for short in stems:
        t_step = time.monotonic()
        step_failures = failures
        buildlog.event(
            "step_started",
            "Building assembly",
            level="debug",
            stem=short,
            only_missing=only_missing,
        )
        cls = load_class(short)
        # Save BOMs before entering the crash-prone renderer. This also warms
        # the leaf cache; a render retry can then use its BREP checkpoint and
        # existing BOM without reconstructing the assembly in a new process.
        if bom or bom_delta:
            if only_missing and not _missing_boms(short, bom=bom, bom_delta=bom_delta):
                buildlog.event(
                    "bom_finished",
                    "Retained completed bill of materials",
                    level="debug",
                    stem=short,
                    status="retained",
                )
            else:
                try:
                    write_bom(short, cumulative=bom, want_delta=bom_delta)
                    kinds = " + ".join(
                        k for k, on in (("cumulative", bom), ("delta", bom_delta)) if on
                    )
                    buildlog.event(
                        "bom_finished",
                        f"Wrote {kinds} bill of materials",
                        level="debug",
                        stem=short,
                        status="written",
                    )
                except Exception as exc:
                    failures += 1
                    _log_exception(exc, stem=short, variant="bom")
        for variant, exploded in _VARIANTS:
            try:
                buildlog.event(
                    "variant_started",
                    "Building and rendering variant",
                    level="debug",
                    stem=short,
                    variant=variant,
                )
                timings = _run_one(cls, exploded, only_missing=only_missing)
                # Replay even on a skip: a crash may have landed the last SVG
                # before its annotations. Replay is idempotent and geometry-free.
                n_patches = _replay_patches_for(short, exploded)
                tb, te, tr = timings or (0.0, 0.0, 0.0)
                buildlog.event(
                    "variant_finished",
                    f"{'Retained completed outputs' if timings is None else 'Built variant'}; "
                    f"build {tb:.2f}s, export {te:.2f}s, render {tr:.2f}s; {n_patches} annotation snapshots",
                    level="debug",
                    stem=short,
                    variant=variant,
                    status="retained" if timings is None else "built",
                    build_s=round(tb, 3),
                    export_s=round(te, 3),
                    render_s=round(tr, 3),
                    snapshots=n_patches,
                )
            except Exception as exc:
                failures += 1
                _log_exception(exc, stem=short, variant=variant)
        duration = time.monotonic() - t_step
        failed = failures != step_failures
        buildlog.event(
            "step_finished",
            f"Failed after {duration:.1f}s" if failed else f"{duration:.1f}s",
            level="error" if failed else "info",
            stem=short,
            status="failed" if failed else "success",
            duration_s=round(duration, 3),
        )
    return int(bool(failures))


def _log_exception(exc: Exception, **fields) -> None:
    buildlog.event(
        "error",
        f"{type(exc).__name__}: {exc}",
        level="error",
        error_type=type(exc).__name__,
        traceback="".join(traceback.format_exception(exc)),
        **fields,
    )


def _run_subprocess(
    stems: list[str],
    bom_flags: tuple[str, ...] = (),
    *,
    only_missing: bool = False,
) -> int:
    _check_cancelled()
    with owned_process(
        [
            sys.executable,
            "-u",
            "-m",
            "hardware.assembly.build_procedures",
            "--worker-events",
            "--stems",
            *stems,
            *bom_flags,
            *(("--only-missing",) if only_missing else ()),
        ],
        env={**os.environ, "PYTHONFAULTHANDLER": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    ) as owner:
        process = owner.process
        reader_errors = []

        def collect():
            try:
                for line in process.stdout:
                    buildlog.worker_line(line, stems=stems, worker_pid=process.pid)
            except Exception as exc:
                reader_errors.append(exc)
                process.kill()

        reader = Thread(target=collect, daemon=True)
        deadline = time.monotonic() + WORKER_TIMEOUT
        next_heartbeat = time.monotonic() + WORKER_HEARTBEAT
        rc = None
        try:
            reader.start()
            while True:
                if _CANCELLED.is_set():
                    rc = 130
                    break
                try:
                    rc = process.wait(
                        timeout=min(0.1, max(0, deadline - time.monotonic()))
                    )
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        process.kill()
                        process.wait()
                        buildlog.event(
                            "worker_timeout",
                            f"Worker exceeded {WORKER_TIMEOUT}s; retaining completed outputs for recovery",
                            level="warning",
                            stems=stems,
                            timeout_s=WORKER_TIMEOUT,
                            worker_pid=process.pid,
                        )
                        rc = 124
                        break
                    if time.monotonic() >= next_heartbeat:
                        buildlog.event(
                            "worker_running",
                            f"CAD worker still running: {', '.join(stems)}",
                            stems=stems,
                            worker_pid=process.pid,
                        )
                        next_heartbeat = time.monotonic() + WORKER_HEARTBEAT
        except KeyboardInterrupt:
            _CANCELLED.set()
            rc = 130
        finally:
            owner.stop(cooperative_cancel=True)
            if reader.ident is not None:
                reader.join()
            exit_code = rc if rc is not None else process.returncode
            buildlog.event(
                "worker_exited",
                f"Worker exited with return code {exit_code}",
                level="debug",
                stems=stems,
                worker_pid=process.pid,
                returncode=exit_code,
                process_returncode=process.returncode,
                crash_log=str(_crash_log()),
            )
            if reader_errors:
                raise reader_errors[0]
        if is_interrupted(rc):
            _CANCELLED.set()
        _check_cancelled()
        return rc


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
    want_bom = "--bom" in bom_flags
    want_delta = "--bom-delta" in bom_flags
    want_any_bom = want_bom or want_delta
    # With the cache on, don't wipe everything up front (it would just be
    # restored again) — the cache owns each stem's freshness, and only the
    # miss stems are cleared, right before they rebuild. A --no-cache run
    # still wipes all stale outputs so deleted/renamed steps don't survive.
    if not use_cache:
        _clear_outputs(clear_bom=want_any_bom)

    t_wall0 = time.monotonic()
    _check_cancelled()
    batches = _batches(batch_size)
    stems = dict.fromkeys(s for batch in batches for s in batch)
    total = len(stems)
    buildlog.event(
        "build_plan",
        f"{total} assemblies, 2 variants each; {jobs} CAD workers; cache {'enabled' if use_cache else 'bypassed'}",
        total=total,
        jobs=jobs,
        use_cache=use_cache,
        batch_size=batch_size,
    )

    inputs = {}
    for stem in stems:
        _check_cancelled()
        inputs[stem] = (stepcache.source_key(stem), stepcache.annotation_key(stem))

    # Classify each stem once (source_cached verifies the entry's hashes — not
    # something to run twice); the loop below reuses this map.
    cached: dict[str, bool] = {}
    if use_cache:
        pruned = stepcache.prune(stems)
        for s in stems:
            _check_cancelled()
            cached[s] = stepcache.source_cached(
                s,
                expected_key=inputs[s][0],
                want_bom=want_bom,
                want_delta=want_delta,
                expected_svgs={
                    svg_path_for(s, exploded, index=i).name
                    for _, exploded in _VARIANTS
                    for i in _view_indices(s, exploded)
                },
            )
        n_cached = sum(cached.values())
        buildlog.event(
            "cache_checked",
            f"Cache: {n_cached}/{total} reusable, {total - n_cached} to build; {_shown(stepcache.CACHE_DIR.resolve())}",
            cached=n_cached,
            to_build=total - n_cached,
            pruned=pruned,
            cache_dir=str(stepcache.CACHE_DIR.resolve()),
        )

    n_fresh = n_replay = 0
    pending = []
    for batch in batches:
        _check_cancelled()
        misses = []
        for s in batch:
            _check_cancelled()
            if not cached.get(s):
                misses.append(s)
                continue
            stepcache.clear_outputs(s)
            stepcache.restore_source(s, want_bom=want_any_bom)
            if stepcache.snapshots_cached(s):
                stepcache.restore_snapshots(s)
                n_fresh += 1
                buildlog.event(
                    "step_cached",
                    "Restored validated cached outputs",
                    level="debug",
                    stem=s,
                )
            else:
                # Patch edited but geometry/render unchanged: keep the cached
                # .step + raw .svg, just re-apply the (build123d-free) replay.
                # clear_outputs already removed the old snapshots, so removed
                # or renamed patch operations cannot survive in the new layer.
                for exploded in (True, False):
                    _replay_patches_for(s, exploded)
                stepcache.store_snapshots(s, expected_annotation_key=inputs[s][1])
                n_replay += 1
                buildlog.event(
                    "annotations_replayed",
                    "Reapplied annotations to cached geometry",
                    stem=s,
                )

        if not misses:
            continue
        if use_cache:
            # Clear each miss stem's own outputs so a rebuild that now renders
            # fewer cameras/variants leaves no stale file behind.
            for s in misses:
                stepcache.clear_outputs(s)
        pending.append(misses)

    retry_attempts: list[tuple[str, int]] = []
    stored = set()

    def store_completed(stem: str) -> None:
        source_key, annotation_key = inputs[stem]
        stepcache.store_source(
            stem,
            expected_key=source_key,
            want_bom=want_bom,
            want_delta=want_delta,
        )
        stepcache.store_snapshots(stem, expected_annotation_key=annotation_key)
        stored.add(stem)

    def log_retry(stem: str, attempt: int) -> None:
        _check_cancelled()
        retry_attempts.append((stem, attempt))
        buildlog.event(
            "step_retry",
            f"Recovery attempt {attempt}/{MAX_STEM_RETRIES}; resume unfinished outputs",
            level="warning",
            stem=stem,
            attempt=attempt,
            max_attempts=MAX_STEM_RETRIES,
        )

    def build_batch(misses: list[str]) -> bool:
        _check_cancelled()
        buildlog.event(
            "batch_started",
            f"Building {family_of(misses[0])} batch ({len(misses)} assemblies)",
            level="debug",
            stems=misses,
            family=family_of(misses[0]),
        )
        rc = _run_subprocess(misses, bom_flags)
        if is_interrupted(rc):
            _CANCELLED.set()
        _check_cancelled()
        if rc != 0:
            # Nondeterministic HLR SIGSEGV: re-run each incomplete stem in a
            # fresh process (new heap layout) until its outputs land or we
            # exhaust the retries.
            incomplete = [s for s in misses if _incomplete(s, bom_flags)]
            reason = signal.Signals(-rc).name if rc < 0 else f"exit {rc}"
            buildlog.event(
                "worker_failed",
                f"Worker stopped ({reason}); {len(incomplete)} incomplete assemblies to retry; completed outputs retained",
                level="warning",
                returncode=rc,
                reason=reason,
                stems=misses,
                incomplete=incomplete,
                crash_log=str(_crash_log()),
            )
            retry_stems(
                incomplete,
                run=lambda s: _run_subprocess([s], bom_flags, only_missing=True),
                done=lambda s: not _incomplete(s, bom_flags),
                log=log_retry,
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
            _check_cancelled()
            if not _incomplete(s, bom_flags):
                store_completed(s)
        return True

    # Threads only supervise independent child processes; OCCT never runs
    # concurrently within one interpreter. Each batch owns distinct stems.
    try:
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            results = list(executor.map(build_batch, pending))
        _check_cancelled()
    except KeyboardInterrupt:
        # The executor has joined every worker and drained its events. Keep
        # successful steps from unfinished batches, not just whole batches.
        successful = buildlog.successful_stems() - stored
        for stem in (s for batch in pending for s in batch if s in successful):
            try:
                if not _incomplete(stem, bom_flags):
                    store_completed(stem)
                    buildlog.event(
                        "cache_saved",
                        "Saved completed outputs after cancellation",
                        level="debug",
                        stem=stem,
                    )
            except Exception as exc:
                buildlog.event(
                    "cache_store_failed",
                    str(exc),
                    level="warning",
                    stem=stem,
                    traceback=traceback.format_exc(),
                )
        raise
    for stem, expected in inputs.items():
        _check_cancelled()
        if (stepcache.source_key(stem), stepcache.annotation_key(stem)) != expected:
            raise RuntimeError(f"Build inputs changed during the run; rebuild {stem}")
    wall = time.monotonic() - t_wall0
    failed_variants = [(s, v) for s in stems for v in _incomplete(s, bom_flags)]
    ok_assemblies = total - len({s for s, _ in failed_variants})
    n_step = len(list(STEP_DIR.glob("*.step")))
    n_svg = len(list(SVG_DIR.glob("*.svg")))
    failed = bool(failed_variants) or not all(results)
    for stem, variant in failed_variants:
        buildlog.event(
            "output_missing",
            "Required output missing after recovery",
            level="error",
            stem=stem,
            variant=variant,
        )
    if not all(results):
        buildlog.event(
            "verification_failed",
            "Worker verification failed even though output files may exist; inspect the error events",
            level="error",
        )
    buildlog.event(
        "build_summary",
        f"{ok_assemblies}/{total} assemblies have complete outputs; {n_fresh} cached, {n_replay} reannotated, "
        f"{ok_assemblies - n_fresh - n_replay} rebuilt; {len(retry_attempts)} retries; "
        f"{n_step} STEP and {n_svg} SVG files present",
        level="error" if failed else "info",
        status="failed" if failed else "success",
        total=total,
        complete=ok_assemblies,
        cached=n_fresh,
        reannotated=n_replay,
        rebuilt=ok_assemblies - n_fresh - n_replay,
        retries=len(retry_attempts),
        failed_outputs=len(failed_variants),
        failed_batches=results.count(False),
        step_files=n_step,
        svg_files=n_svg,
        duration_s=round(wall, 3),
    )
    return int(failed)


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
    buildlog.add_arguments(
        parser,
        format_help="terminal output format; json emits every event as JSONL",
        verbose_help="show per-variant timings and worker diagnostics (always saved in the JSONL log)",
    )
    parser.add_argument("--worker-events", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    if args.only_missing and not args.stems:
        parser.error("--only-missing requires --stems")
    if args.worker_events and not args.stems:
        parser.error("--worker-events requires --stems")

    # Line-buffer stdout: under a pipe (make … | tee, CI) the default is
    # block buffering, which holds the dispatcher's progress until exit and
    # loses a crashed worker's entire report with the SIGSEGV.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    # The CLI can forward another SIGINT after workers have stopped. Keep the
    # handler active until the final result is written and the log is closed.
    with (
        nullcontext() if args.worker_events else cancel_on_interrupt(_CANCELLED),
        buildlog.session(
            OUTPUT_DIR,
            log_format=args.log_format,
            verbose=args.verbose,
            worker=args.worker_events,
        ) as log,
    ):
        level = "debug" if args.worker_events else "info"
        buildlog.event(
            "worker_started" if args.worker_events else "build_started",
            "Hardware assembly build started",
            level=level,
            stems=args.stems,
            total=len(args.stems) if args.stems else None,
            output_dir=str(OUTPUT_DIR),
            crash_log=str(_crash_log()),
            argv=list(sys.argv[1:] if argv is None else argv),
        )
        if log.path:
            buildlog.event(
                "log_opened",
                f"Detailed event log: {_shown(log.path)}",
                log_path=str(log.path),
                crash_log=str(log.crash_path),
            )
        try:
            rc = _execute(args)
            if not args.worker_events:
                _check_cancelled()
        except KeyboardInterrupt:
            buildlog.event(
                "build_interrupted",
                "Build interrupted by user",
                level="warning",
                traceback=traceback.format_exc(),
            )
            rc = 130
        except Exception as exc:
            _log_exception(exc)
            rc = 1
        if rc < 0:
            buildlog.event(
                "worker_failed",
                f"Worker stopped ({signal.Signals(-rc).name})",
                level="error",
                returncode=rc,
            )
            rc = 128 - rc
        status = "success" if rc == 0 else "cancelled" if rc == 130 else "failed"
        subject = "Worker" if args.worker_events else "Build"
        message = f"{subject} {status.upper()}; outputs: {_shown(OUTPUT_DIR)}"
        if rc and log.path:
            message += f"; inspect errors in {_shown(log.path)}"
        crash_log = _crash_log()
        if rc and crash_log.exists() and crash_log.stat().st_size:
            message += f"; native diagnostics: {_shown(crash_log)}"
        buildlog.event(
            "worker_finished" if args.worker_events else "build_finished",
            message,
            level="debug"
            if args.worker_events
            else "error"
            if rc not in (0, 130)
            else "info",
            status=status,
            exit_code=rc,
            duration_s=round(time.monotonic() - log.started, 3),
            output_dir=str(OUTPUT_DIR),
            log_path=str(log.path) if log.path else None,
            crash_log=str(crash_log) if crash_log.exists() else None,
        )
        return rc


def _execute(args: argparse.Namespace) -> int:
    bom_flags = tuple(
        f for f, on in (("--bom", args.bom), ("--bom-delta", args.bom_delta)) if on
    )
    if args.stems and not args.worker_events:
        # Use the same collector for single-step builds: native output must
        # not leak into a JSON stdout stream either.
        return _run_subprocess(args.stems, bom_flags, only_missing=args.only_missing)
    if args.stems:
        # Route crash forensics to this run's log instead of stderr; the header
        # line ties each dumped stack to the worker's stems.
        crash_path = _crash_log()
        crash_path.parent.mkdir(parents=True, exist_ok=True)
        with crash_path.open("a", encoding="utf-8") as crash_log:
            crash_log.write(
                f"=== {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} pid={os.getpid()} worker --stems {' '.join(args.stems)}\n"
            )
            crash_log.flush()
            faulthandler.enable(file=crash_log)
            try:
                return _build_stems(
                    args.stems,
                    bom=args.bom,
                    bom_delta=args.bom_delta,
                    only_missing=args.only_missing,
                )
            finally:
                faulthandler.disable()

    return _dispatch(
        args.batch_size, bom_flags, use_cache=not args.no_cache, jobs=args.jobs
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
