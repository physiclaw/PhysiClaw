"""Tests for hardware/assembly/build_procedures.py — dispatcher/worker glue.

The real module drives CAD geometry (build123d / OCCT) in worker
subprocesses; these tests monkeypatch the heavy pieces (``load_class``,
``_run_one``, the ``stepcache`` module) and pin the wiring around them:
BOM failure accounting, the ``_incomplete`` completeness predicate, and
the patch-replay branch's stale-snapshot clearing.
"""

from __future__ import annotations

from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

# The module under test imports the CAD stack at module level; that's
# the optional `cad` extra (uv sync --extra cad), absent on CI runners.
pytest.importorskip("build123d", reason="CAD extra not installed")

from hardware.assembly import build_procedures as bp  # noqa: E402

STEM = "frame_10_base"


# ── _build_stems: BOM failure accounting (worker mode) ────────────────────────


@pytest.fixture
def worker(monkeypatch):
    """_build_stems with the CAD-heavy pieces stubbed to instant successes."""
    monkeypatch.setattr(bp, "load_class", lambda stem: object)
    monkeypatch.setattr(
        bp,
        "_run_one",
        lambda cls, exploded, *, only_missing=False: (0.0, 0.0, 0.0),
    )
    monkeypatch.setattr(bp, "_replay_patches_for", lambda stem, exploded: 0)


def test_bom_write_failure_is_recorded_so_the_build_returns_nonzero(
    worker, monkeypatch
):
    # A swallowed BOM failure would return 0 and let `make hw-release`
    # package output with BOM pages silently missing.
    def boom(stem, *, cumulative=False, want_delta=False):
        raise RuntimeError("disk full")

    monkeypatch.setattr(bp, "write_bom", boom)

    assert bp._build_stems([STEM], bom=True) == 1


def test_bom_write_success_keeps_the_build_green(worker, monkeypatch, tmp_path, capsys):
    import json

    monkeypatch.setattr(
        bp, "write_bom", lambda stem, *, cumulative=False, want_delta=False: None
    )

    with bp.buildlog.session(tmp_path) as log:
        assert bp._build_stems([STEM], bom=True) == 0
    output = capsys.readouterr().out
    assert "Building assembly" not in output
    assert "Done in" not in output
    assert len(output.splitlines()) == 1
    records = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert next(r for r in records if r["event"] == "step_started")["level"] == "debug"
    assert (
        next(r for r in records if r["event"] == "step_finished")["status"] == "success"
    )
    finished = next(r for r in records if r["event"] == "step_finished")
    assert abs(float(finished["message"][:-1]) - finished["duration_s"]) < 0.051


def test_annotation_exception_fails_worker(worker, monkeypatch):
    def fail(*args):
        raise ValueError("invalid annotation")

    monkeypatch.setattr(bp, "_replay_patches_for", fail)
    assert bp._build_stems([STEM]) == 1


def test_first_step_delta_bom_is_written(tmp_path, monkeypatch):
    from hardware.assembly import bom

    monkeypatch.setattr(bom, "BOM_DIR", tmp_path)
    monkeypatch.setattr(bom, "load_step", lambda name: None)
    monkeypatch.setattr(
        bom, "collect", lambda step: [bom.BomEntry("standard", ("Screw", "M3"), 2)]
    )
    monkeypatch.setattr(bom, "predecessor_module", lambda name: None)
    bom.write_bom(STEM, cumulative=False, want_delta=True)
    assert "Screw M3" in (tmp_path / f"{STEM}_delta.md").read_text()


def test_worker_timeout_returns_failure(tmp_path, monkeypatch):
    import subprocess
    import sys

    popen = subprocess.Popen

    def slow_worker(*args, **kwargs):
        return popen([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)

    monkeypatch.setattr(bp.subprocess, "Popen", slow_worker)
    monkeypatch.setattr(bp, "WORKER_TIMEOUT", 0.05)
    with bp.buildlog.session(tmp_path, log_format="json") as log:
        assert bp._run_subprocess([STEM]) == 124
    import json

    records = [json.loads(line) for line in log.path.read_text().splitlines()]
    timeout = next(r for r in records if r["event"] == "worker_timeout")
    assert timeout["timeout_s"] == 0.05
    assert records[-1]["event"] == "worker_exited"
    assert records[-1]["returncode"] == 124


def test_external_output_path_is_safe_to_log(tmp_path):
    assert bp._shown(tmp_path / "crash.log") == tmp_path / "crash.log"


@pytest.mark.parametrize("verification_exit", [0, 1])
def test_crashed_batch_with_complete_files_still_requires_successful_verification(
    tmp_path, monkeypatch, verification_exit
):
    stems = [STEM, "frame_20_next"]
    calls = []
    stored = []

    def run(stems, bom_flags, *, only_missing=False):
        calls.append((stems, only_missing))
        return verification_exit if only_missing else -11

    monkeypatch.setattr(bp.stepcache, "source_key", lambda stem: f"source:{stem}")
    monkeypatch.setattr(
        bp.stepcache, "annotation_key", lambda stem: f"annotations:{stem}"
    )
    monkeypatch.setattr(bp, "CRASH_LOG", tmp_path / "crash.log")
    monkeypatch.setattr(bp, "STEP_DIR", tmp_path / "step")
    monkeypatch.setattr(bp, "SVG_DIR", tmp_path / "svg")
    monkeypatch.setattr(bp, "_clear_outputs", lambda **kwargs: None)
    monkeypatch.setattr(bp, "_batches", lambda size: [stems])
    monkeypatch.setattr(bp, "_incomplete", lambda stem, flags: [])
    monkeypatch.setattr(bp, "_run_subprocess", run)
    monkeypatch.setattr(
        bp.stepcache, "store_source", lambda stem, **kwargs: stored.append(stem)
    )
    monkeypatch.setattr(bp.stepcache, "store_snapshots", lambda stem, **kwargs: None)

    import json

    with bp.buildlog.session(tmp_path, log_format="json") as log:
        assert bp._dispatch(2, use_cache=False) == verification_exit
    records = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert records[-1]["event"] == "build_summary"
    assert records[-1]["status"] == ("failed" if verification_exit else "success")
    assert records[-1]["failed_batches"] == verification_exit
    assert calls == [(stems, False), (stems, True)]
    assert stored == ([] if verification_exit else stems)


def test_main_logs_failure_and_final_status(tmp_path, monkeypatch):
    import json

    def fail(args):
        raise ValueError("invalid input\nsecond line")

    monkeypatch.setattr(bp, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr("hardware.build_lock.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(bp, "_execute", fail)
    assert bp.main(["--log-format", "json"]) == 1
    records = [
        json.loads(line)
        for line in next((tmp_path / "logs").glob("*.jsonl")).read_text().splitlines()
    ]
    assert records[-1]["event"] == "build_finished"
    assert records[-1]["status"] == "failed"
    assert records[-1]["exit_code"] == 1
    error = next(r for r in records if r["event"] == "error")
    assert "ValueError: invalid input" in error["traceback"]


def test_worker_stream_keeps_native_output_out_of_json(tmp_path, monkeypatch, capsys):
    import json
    import subprocess
    import sys

    popen = subprocess.Popen
    script = (
        "import sys\n"
        "from hardware import buildlog\n"
        "with buildlog.session(None, worker=True):\n"
        " buildlog.event('step_started', 'Building', stem='frame_10_base')\n"
        " print('native diagnostic', file=sys.stderr)\n"
        " buildlog.event('step_finished', 'Done', stem='frame_10_base', status='success')\n"
    )
    monkeypatch.setattr(
        bp.subprocess,
        "Popen",
        lambda *args, **kwargs: popen([sys.executable, "-c", script], **kwargs),
    )
    with bp.buildlog.session(tmp_path, log_format="json") as log:
        assert bp._run_subprocess([STEM]) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["event"] for r in records] == [
        "step_started",
        "worker_output",
        "step_finished",
        "worker_exited",
    ]
    assert records[1]["message"] == "native diagnostic"
    assert all(r["run_id"] == log.run_id for r in records)
    assert all(r["worker_pid"] != bp.os.getpid() for r in records)


def test_worker_collector_failure_records_exit_before_raising(tmp_path, monkeypatch):
    import json
    import subprocess
    import sys

    popen = subprocess.Popen
    monkeypatch.setattr(
        bp.subprocess,
        "Popen",
        lambda *args, **kwargs: popen(
            [
                sys.executable,
                "-u",
                "-c",
                "import time; print('diagnostic'); time.sleep(30)",
            ],
            **kwargs,
        ),
    )

    def fail(*args, **kwargs):
        raise OSError("Cannot collect CAD output")

    monkeypatch.setattr(bp.buildlog, "worker_line", fail)
    with (
        bp.buildlog.session(tmp_path) as log,
        pytest.raises(OSError, match="Cannot collect CAD output"),
    ):
        bp._run_subprocess([STEM])
    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert events[-1]["event"] == "worker_exited"
    assert events[-1]["returncode"] == events[-1]["process_returncode"]
    assert events[-1]["process_returncode"] != 0


def test_worker_warnings_and_native_dumps_are_saved_per_run(
    tmp_path, monkeypatch, capsys
):
    import json
    import subprocess
    import sys

    popen = subprocess.Popen
    script = (
        "import faulthandler, os, warnings\n"
        "assert faulthandler.is_enabled()\n"
        "warnings.warn('kernel diagnostic', RuntimeWarning)\n"
        "with open(os.environ['PHYSICLAW_HW_CRASH_LOG'], 'a') as dump:\n"
        " faulthandler.dump_traceback(file=dump)\n"
        "raise SystemExit(7)\n"
    )
    monkeypatch.setattr(
        bp.subprocess,
        "Popen",
        lambda *args, **kwargs: popen([sys.executable, "-u", "-c", script], **kwargs),
    )
    paths = []
    for _ in range(2):
        with bp.buildlog.session(tmp_path) as log:
            assert bp._run_subprocess([STEM]) == 7
            assert bp._crash_log() == log.crash_path
        saved = [json.loads(line) for line in log.path.read_text().splitlines()]
        assert any("RuntimeWarning: kernel diagnostic" in r["message"] for r in saved)
        exited = saved[-1]
        assert exited["returncode"] == 7
        assert exited["process_returncode"] == 7
        assert exited["crash_log"] == str(log.crash_path)
        paths.append(log.crash_path)
    assert paths[0] != paths[1]
    assert all('File "<string>"' in path.read_text() for path in paths)
    assert capsys.readouterr().out == ""


def test_parallel_batches_are_bounded_and_awaited(tmp_path, monkeypatch):
    stems = [STEM, "frame_20_next", "frame_30_last", "frame_40_end"]
    barrier = Barrier(2, timeout=5)
    lock = Lock()
    active = peak = 0
    completed = []

    def run(batch, flags, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait()
        with lock:
            active -= 1
            completed.extend(batch)
        return 0

    monkeypatch.setattr(bp.stepcache, "source_key", lambda stem: f"source:{stem}")
    monkeypatch.setattr(
        bp.stepcache, "annotation_key", lambda stem: f"annotations:{stem}"
    )
    monkeypatch.setattr(bp, "CRASH_LOG", tmp_path / "crash.log")
    monkeypatch.setattr(bp, "STEP_DIR", tmp_path / "step")
    monkeypatch.setattr(bp, "SVG_DIR", tmp_path / "svg")
    monkeypatch.setattr(bp, "_clear_outputs", lambda **kwargs: None)
    monkeypatch.setattr(bp, "_batches", lambda size: [[s] for s in stems])
    monkeypatch.setattr(bp, "_incomplete", lambda stem, flags: [])
    monkeypatch.setattr(bp, "_run_subprocess", run)
    monkeypatch.setattr(bp.stepcache, "store_source", lambda stem, **kwargs: None)
    monkeypatch.setattr(bp.stepcache, "store_snapshots", lambda stem, **kwargs: None)

    assert bp._dispatch(1, use_cache=False, jobs=2) == 0
    assert sorted(completed) == stems
    assert active == 0
    assert peak == 2


def test_cancelled_batch_caches_only_verified_successes_and_resumes_them(
    cache_pipeline, outputs, tmp_path, monkeypatch
):
    import json

    stems = [
        STEM,
        "frame_20_failed",
        "frame_30_unreported",
        "frame_40_restarted",
        "frame_50_partial",
    ]
    cache_pipeline.stems[:] = stems

    def write(stem, variants=("exploded", "assembled")):
        for variant in variants:
            (outputs.step / f"{stem}_{variant}.step").write_text("step")
            (outputs.svg / f"{stem}_{variant}_cam0.svg").write_text("svg")

    def interrupted(batch, flags):
        for stem in stems[:-1]:
            write(stem)
        write(stems[-1], ("exploded",))
        for stem, status in (
            (STEM, "success"),
            (stems[1], "failed"),
            (stems[3], "success"),
            (stems[4], "success"),
        ):
            bp.buildlog.event("step_finished", "Finished", stem=stem, status=status)
        bp.buildlog.event("step_started", "Restarting", level="debug", stem=stems[3])
        bp._CANCELLED.set()
        raise KeyboardInterrupt

    monkeypatch.setattr(bp, "_run_subprocess", interrupted)
    with bp.buildlog.session(tmp_path) as first:
        with pytest.raises(KeyboardInterrupt), bp.cancel_on_interrupt(bp._CANCELLED):
            bp._dispatch(5, use_cache=False)
    saved = [json.loads(line) for line in first.path.read_text().splitlines()]
    assert [event["stem"] for event in saved if event["event"] == "cache_saved"] == [
        STEM
    ]
    assert bp.stepcache.source_cached(STEM, want_bom=False)
    assert all(
        not bp.stepcache.source_cached(stem, want_bom=False) for stem in stems[1:]
    )

    rebuilt = []

    def resume(batch, flags):
        rebuilt.extend(batch)
        for stem in batch:
            write(stem)
            bp.buildlog.event("step_finished", "Finished", stem=stem, status="success")
        return 0

    monkeypatch.setattr(bp, "_run_subprocess", resume)
    with bp.buildlog.session(tmp_path) as second:
        assert bp._dispatch(5) == 0
    saved = [json.loads(line) for line in second.path.read_text().splitlines()]
    assert (
        next(event for event in saved if event["event"] == "cache_checked")["cached"]
        == 1
    )
    assert rebuilt == stems[1:]


# ── _incomplete: STEP/SVG variants + requested BOMs ───────────────────────────


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    """A fake output tree that _missing_variants / _missing_boms resolve
    against, with the CAD-loading view-index lookup pinned to one camera."""
    dirs = SimpleNamespace(
        step=tmp_path / "step", svg=tmp_path / "svg", bom=tmp_path / "bom"
    )
    for d in vars(dirs).values():
        d.mkdir()
    monkeypatch.setattr(bp, "BOM_DIR", dirs.bom)
    monkeypatch.setattr(
        bp,
        "step_path_for",
        lambda stem, exploded: dirs.step / f"{stem}{suffix(exploded)}.step",
    )
    monkeypatch.setattr(
        bp,
        "svg_path_for",
        lambda stem, exploded, index=0: (
            dirs.svg / f"{stem}{suffix(exploded)}_cam{index}.svg"
        ),
    )
    monkeypatch.setattr(bp, "_view_indices", lambda stem, exploded: (0,))
    return dirs


@pytest.fixture
def cache_pipeline(outputs, tmp_path, monkeypatch):
    """Real cache store/restore and completeness checks with small generated files."""
    stems, source_versions, annotation_versions = [STEM], {}, {}
    for name, path in (
        ("STEP_DIR", outputs.step),
        ("SVG_DIR", outputs.svg),
        ("BOM_DIR", outputs.bom),
    ):
        monkeypatch.setattr(bp, name, path)
        monkeypatch.setattr(bp.stepcache, name, path)
    for name, path in (
        ("CACHE_DIR", tmp_path / "cache"),
        ("PATCH_DIR", tmp_path / "patches"),
        ("RENDER_WORK_DIR", tmp_path / "render"),
    ):
        monkeypatch.setattr(bp.stepcache, name, path)
    monkeypatch.setattr(bp, "RENDER_WORK_DIR", tmp_path / "render")
    monkeypatch.setattr("hardware.assembly.mark.patch.PATCH_DIR", tmp_path / "patches")
    monkeypatch.setattr(
        bp.stepcache,
        "source_key",
        lambda stem: f"{stem}:{source_versions.get(stem, 1)}",
    )
    monkeypatch.setattr(
        bp.stepcache,
        "annotation_key",
        lambda stem: f"{stem}:{annotation_versions.get(stem, 1)}",
    )
    monkeypatch.setattr(bp, "_batches", lambda size: [stems])

    def write(stem, flags=()):
        for variant in ("exploded", "assembled"):
            (outputs.step / f"{stem}_{variant}.step").write_text("geometry")
            (outputs.svg / f"{stem}_{variant}_cam0.svg").write_text("drawing")
        for flag, suffix in (("--bom", ""), ("--bom-delta", "_delta")):
            if flag in flags:
                (outputs.bom / f"{stem}{suffix}.md").write_text("fresh BOM")
        bp.buildlog.event("step_finished", "Finished", stem=stem, status="success")

    return SimpleNamespace(
        stems=stems,
        sources=source_versions,
        annotations=annotation_versions,
        write=write,
    )


@pytest.mark.parametrize(
    "flags", [(), ("--bom",), ("--bom-delta",), ("--bom", "--bom-delta")]
)
def test_forced_build_does_not_cache_unrequested_stale_boms(
    cache_pipeline, outputs, tmp_path, monkeypatch, flags
):
    import json

    for suffix in ("", "_delta"):
        (outputs.bom / f"{STEM}{suffix}.md").write_text("stale BOM")
    builds = []

    def run(batch, bom_flags):
        builds.append(batch)
        for stem in batch:
            cache_pipeline.write(stem, bom_flags)
        return 0

    monkeypatch.setattr(bp, "_run_subprocess", run)
    with bp.buildlog.session(tmp_path):
        assert bp._dispatch(1, flags, use_cache=False) == 0
    manifest = json.loads((bp.stepcache.CACHE_DIR / STEM / "source.key").read_text())[
        "files"
    ]
    assert (f"{STEM}.md" in manifest) == ("--bom" in flags)
    assert (f"{STEM}_delta.md" in manifest) == ("--bom-delta" in flags)
    for name in manifest:
        if name.endswith(".md"):
            assert (bp.stepcache.CACHE_DIR / STEM / name).read_text() == "fresh BOM"
    # A later request for both BOM kinds must rebuild if either was omitted.
    with bp.buildlog.session(tmp_path):
        assert bp._dispatch(1, ("--bom", "--bom-delta")) == 0
    assert len(builds) == (1 if len(flags) == 2 else 2)


def test_rebuild_bypasses_existing_cache_and_next_build_restores_fresh_outputs(
    cache_pipeline, outputs, tmp_path, monkeypatch
):
    builds = []

    def run(batch, flags):
        builds.append(batch)
        for stem in batch:
            cache_pipeline.write(stem, flags)
            (outputs.step / f"{stem}_assembled.step").write_text(f"build {len(builds)}")
        return 0

    monkeypatch.setattr(bp, "_run_subprocess", run)
    with bp.buildlog.session(tmp_path):
        assert bp._dispatch(5) == 0
    assert len(builds) == 1

    with monkeypatch.context() as forced:
        forced.setattr(
            bp.stepcache,
            "source_cached",
            lambda *args, **kwargs: pytest.fail("rebuild must not read the CAD cache"),
        )
        with bp.buildlog.session(tmp_path):
            assert bp._dispatch(5, use_cache=False) == 0
    assert len(builds) == 2

    target = outputs.step / f"{STEM}_assembled.step"
    target.unlink()
    with bp.buildlog.session(tmp_path):
        assert bp._dispatch(5) == 0
    assert len(builds) == 2  # A normal build always reuses the valid entry.
    assert target.read_text() == "build 2"


@pytest.mark.parametrize("edited", ["sources", "annotations"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_inputs_edited_during_build_are_not_cached_as_current(
    cache_pipeline, tmp_path, monkeypatch, edited, cancelled
):
    import json

    def run(batch, flags):
        cache_pipeline.write(STEM, flags)
        getattr(cache_pipeline, edited)[STEM] = 2
        if cancelled:
            bp._CANCELLED.set()
            raise KeyboardInterrupt
        return 0

    monkeypatch.setattr(bp, "_run_subprocess", run)
    with bp.buildlog.session(tmp_path) as log:
        with (
            pytest.raises(KeyboardInterrupt if cancelled else RuntimeError),
            bp.cancel_on_interrupt(bp._CANCELLED),
        ):
            bp._dispatch(1, use_cache=False)
    assert not bp.stepcache.snapshots_cached(STEM)
    assert bp.stepcache.source_cached(STEM, want_bom=False) == (edited == "annotations")
    if cancelled:
        events = [json.loads(line) for line in log.path.read_text().splitlines()]
        assert not any(event["event"] == "cache_saved" for event in events)
        assert any(
            event["event"] == "cache_store_failed"
            and "inputs changed" in event["message"]
            for event in events
        )


@pytest.mark.parametrize("edited", ["sources", "annotations"])
def test_edit_to_restored_step_prevents_success_of_mixed_build(
    cache_pipeline, tmp_path, monkeypatch, edited
):
    cache_pipeline.write(STEM)
    bp.stepcache.store_source(STEM)
    bp.stepcache.store_snapshots(STEM)
    pending = "frame_20_pending"
    cache_pipeline.stems.append(pending)

    def run(batch, flags):
        assert batch == [pending]
        cache_pipeline.write(pending, flags)
        getattr(cache_pipeline, edited)[STEM] = 2
        return 0

    monkeypatch.setattr(bp, "_run_subprocess", run)
    with (
        bp.buildlog.session(tmp_path),
        pytest.raises(RuntimeError, match="inputs changed during the run"),
    ):
        bp._dispatch(1)


def suffix(exploded: bool) -> str:
    return "_exploded" if exploded else "_assembled"


def write_variants(outputs) -> None:
    """Materialize a complete STEP + SVG set for both variants of STEM."""
    for variant in ("exploded", "assembled"):
        (outputs.step / f"{STEM}_{variant}.step").write_text("step")
        (outputs.svg / f"{STEM}_{variant}_cam0.svg").write_text("svg")


def test_incomplete_flags_a_missing_requested_bom(outputs):
    # STEP + SVG landed but the BOM never did (a write_bom-phase SIGSEGV):
    # the stem still owes its BOM, so the retry machinery must see it.
    write_variants(outputs)

    assert bp._incomplete(STEM, ("--bom",)) == ["bom"]


def test_incomplete_without_a_bom_request_ignores_the_absent_bom(outputs):
    write_variants(outputs)

    assert bp._incomplete(STEM, ()) == []


def test_incomplete_with_every_output_present_is_empty(outputs):
    write_variants(outputs)
    (outputs.bom / f"{STEM}.md").write_text("| bom |")

    assert bp._incomplete(STEM, ("--bom",)) == []


def test_incomplete_flags_a_missing_delta_bom(outputs):
    write_variants(outputs)
    (outputs.bom / f"{STEM}.md").write_text("| bom |")

    assert bp._incomplete(STEM, ("--bom", "--bom-delta")) == ["bom"]


def test_incomplete_still_reports_a_missing_variant(outputs):
    write_variants(outputs)
    (outputs.bom / f"{STEM}.md").write_text("| bom |")
    (outputs.step / f"{STEM}_exploded.step").unlink()

    assert bp._incomplete(STEM, ("--bom",)) == ["exploded"]


# ── _dispatch: the cached-geometry patch-replay branch ────────────────────────


def test_patch_replay_branch_clears_stale_snapshots_before_replaying(
    tmp_path, monkeypatch
):
    # A leaf op the patch edit removed/renamed leaves its `_<opid>.svg` on
    # disk; clear_outputs must remove it before restoring source and replaying.
    events: list[tuple[str, str]] = []

    def record(name):
        return lambda stem, **kwargs: events.append((name, stem))

    fake_cache = SimpleNamespace(
        CACHE_DIR=tmp_path / "hardware" / "output" / ".cache",
        prune=lambda stems: 0,
        source_key=lambda stem: f"source:{stem}",
        annotation_key=lambda stem: f"annotations:{stem}",
        source_cached=lambda stem, **kwargs: True,
        snapshots_cached=lambda stem: False,  # patch edited → re-replay
        restore_source=record("restore_source"),
        restore_snapshots=record("restore_snapshots"),
        store_snapshots=record("store_snapshots"),
        clear_outputs=record("clear_outputs"),
        store_source=record("store_source"),
    )
    monkeypatch.setattr(bp, "stepcache", fake_cache)
    monkeypatch.setattr(bp, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(bp, "CRASH_LOG", tmp_path / "crash.log")
    monkeypatch.setattr(bp, "STEP_DIR", tmp_path / "step")
    monkeypatch.setattr(bp, "SVG_DIR", tmp_path / "svg")
    monkeypatch.setattr(bp, "_batches", lambda batch_size: [[STEM]])
    monkeypatch.setattr(bp, "_incomplete", lambda stem, bom_flags: [])
    monkeypatch.setattr(bp, "_view_indices", lambda stem, exploded: (0,))
    monkeypatch.setattr(
        bp,
        "_replay_patches_for",
        lambda stem, exploded: events.append(("replay", stem)),
    )

    rc = bp._dispatch(1, (), use_cache=True)

    assert rc == 0
    assert [name for name, _ in events] == [
        "clear_outputs",  # stale snapshots removed before replay
        "restore_source",
        "replay",  # … which rewrites both variants …
        "replay",
        "store_snapshots",  # … and only then is the fresh layer stored.
    ]
