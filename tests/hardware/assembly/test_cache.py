"""Tests for hardware/assembly/cache.py — keys, layers, manifest integrity.

All directory constants are re-pointed at a per-test fake repo, so the
cache's hashing (which walks the procedure's project import closure
under ``REPO_ROOT``) and its store/restore run against tmp files only.
"""

from __future__ import annotations

import json
import os
import shutil
from types import SimpleNamespace

import pytest

from hardware.assembly import cache

STEM = "frame_10_base"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fake repo tree: one procedure importing one helper, plus every
    output/cache directory the module writes to."""
    dirs = {
        "PROCEDURES_DIR": tmp_path / "hardware" / "assembly" / "procedures",
        "PATCH_DIR": tmp_path / "hardware" / "assembly" / "patch",
        "MARK_DIR": tmp_path / "hardware" / "assembly" / "mark",
        "STEP_DIR": tmp_path / "hardware" / "output" / "step",
        "SVG_DIR": tmp_path / "hardware" / "output" / "svg",
        "BOM_DIR": tmp_path / "hardware" / "output" / "bom",
        "CACHE_DIR": tmp_path / "hardware" / "output" / ".cache",
        "RENDER_WORK_DIR": tmp_path / "hardware" / "output" / ".render-work",
    }
    monkeypatch.setattr(cache, "REPO_ROOT", tmp_path)
    for name, d in dirs.items():
        d.mkdir(parents=True)
        monkeypatch.setattr(cache, name, d)

    procedure = dirs["PROCEDURES_DIR"] / f"{STEM}.py"
    procedure.write_text("import hardware.parts.helper\n")
    helper = tmp_path / "hardware" / "parts" / "helper.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("SIZE = 1\n")
    (dirs["MARK_DIR"] / "replay.py").write_text("ENGINE = 1\n")
    return SimpleNamespace(procedure=procedure, helper=helper, **dirs)


def write_outputs(repo, *, bom: bool = True, cams: tuple[int, ...] = (0,)):
    """Materialize a complete source-layer output set for STEM."""
    for variant in ("exploded", "assembled"):
        (repo.STEP_DIR / f"{STEM}_{variant}.step").write_text(f"step {variant}")
        for cam in cams:
            (repo.SVG_DIR / f"{STEM}_{variant}_cam{cam}.svg").write_text(
                f"svg {variant} {cam}"
            )
    if bom:
        (repo.BOM_DIR / f"{STEM}.md").write_text("| bom |")


# ── keys ──────────────────────────────────────────────────────────────────────


def test_source_key_is_stable_across_calls(repo):
    assert cache.source_key(STEM) == cache.source_key(STEM)


def test_source_key_changes_when_procedure_source_changes(repo):
    before = cache.source_key(STEM)

    repo.procedure.write_text("import hardware.parts.helper  # edited\n")

    assert cache.source_key(STEM) != before


def test_source_key_changes_when_imported_dependency_changes(repo):
    before = cache.source_key(STEM)

    repo.helper.write_text("SIZE = 2\n")

    assert cache.source_key(STEM) != before


def test_import_edits_with_unchanged_size_and_mtime_follow_current_dependencies(repo):
    cache.source_key(STEM)  # Populate the parsed-import lookup.
    second = repo.helper.with_name("second.py")
    second.write_text("SIZE = 1\n")
    original = repo.procedure.stat()
    repo.procedure.write_text("import hardware.parts.second\n")
    assert repo.procedure.stat().st_size == original.st_size
    os.utime(repo.procedure, ns=(original.st_atime_ns, original.st_mtime_ns))
    before = cache.source_key(STEM)

    original = second.stat()
    second.write_text("SIZE = 2\n")
    os.utime(second, ns=(original.st_atime_ns, original.st_mtime_ns))
    after = cache.source_key(STEM)
    assert after != before

    repo.helper.write_text("SIZE = 3\n")
    assert cache.source_key(STEM) == after  # The old dependency is no longer used.


def test_unchanged_imports_resolve_new_and_removed_modules_on_every_check(repo):
    repo.procedure.write_text("import hardware.parts.later\n")
    before = cache.source_key(STEM)
    later = repo.helper.with_name("later.py")
    later.write_text("SIZE = 1\n")
    assert cache.source_key(STEM) != before
    later.unlink()
    assert cache.source_key(STEM) == before


def test_source_key_changes_when_shared_arm_travel_changes(repo, tmp_path):
    shared = tmp_path / "src" / "physiclaw" / "core" / "arm_geometry.py"
    shared.parent.mkdir(parents=True)
    shared.write_text("X_TRAVEL = 110\nY_TRAVEL = 200\n")
    repo.helper.write_text(
        "from __future__ import annotations\n"
        "from src.physiclaw.core.arm_geometry import X_TRAVEL\n"
    )
    before = cache.source_key(STEM)

    shared.write_text("X_TRAVEL = 120\nY_TRAVEL = 200\n")

    assert cache.source_key(STEM) != before


def test_patch_key_changes_when_patch_json_changes(repo):
    before = cache.patch_key(STEM)

    (repo.PATCH_DIR / f"{STEM}_exploded_cam0.json").write_text("[]")

    assert cache.patch_key(STEM) != before


def test_patch_key_changes_when_replay_engine_changes(repo):
    before = cache.patch_key(STEM)

    (repo.MARK_DIR / "replay.py").write_text("ENGINE = 2\n")

    assert cache.patch_key(STEM) != before


# ── source layer: store / hit / restore ───────────────────────────────────────


def test_source_cached_after_store_is_true(repo):
    write_outputs(repo)

    cache.store_source(STEM)

    assert cache.source_cached(STEM, want_bom=True)


def test_source_cached_without_any_store_is_false(repo):
    assert not cache.source_cached(STEM, want_bom=False)


def test_restore_source_reconstructs_deleted_outputs(repo):
    write_outputs(repo)
    cache.store_source(STEM)
    (repo.STEP_DIR / f"{STEM}_exploded.step").unlink()
    (repo.SVG_DIR / f"{STEM}_exploded_cam0.svg").unlink()

    cache.restore_source(STEM)

    assert (repo.STEP_DIR / f"{STEM}_exploded.step").read_text() == "step exploded"


def test_restore_source_without_want_bom_skips_the_bom(repo):
    write_outputs(repo)
    cache.store_source(STEM)
    (repo.BOM_DIR / f"{STEM}.md").unlink()

    cache.restore_source(STEM, want_bom=False)

    assert not (repo.BOM_DIR / f"{STEM}.md").exists()


def test_source_cached_after_source_edit_is_false(repo):
    write_outputs(repo)
    cache.store_source(STEM)

    repo.procedure.write_text("import hardware.parts.helper  # edited\n")

    assert not cache.source_cached(STEM, want_bom=True)


def test_source_cached_with_stored_file_deleted_is_false(repo):
    write_outputs(repo)
    cache.store_source(STEM)

    (cache.CACHE_DIR / STEM / f"{STEM}_exploded.step").unlink()

    assert not cache.source_cached(STEM, want_bom=True)


def test_source_cached_with_stored_file_corrupted_is_false(repo):
    write_outputs(repo)
    cache.store_source(STEM)

    (cache.CACHE_DIR / STEM / f"{STEM}_exploded.step").write_text("tampered")

    assert not cache.source_cached(STEM, want_bom=True)


def test_source_cached_with_unreadable_manifest_is_false(repo):
    write_outputs(repo)
    cache.store_source(STEM)

    (cache.CACHE_DIR / STEM / "source.key").write_text("{broken")

    assert not cache.source_cached(STEM, want_bom=True)


def test_source_cached_wanting_a_bom_the_entry_lacks_is_false(repo):
    write_outputs(repo, bom=False)

    cache.store_source(STEM)

    assert not cache.source_cached(STEM, want_bom=True)


def test_source_cached_not_wanting_the_missing_bom_is_true(repo):
    write_outputs(repo, bom=False)

    cache.store_source(STEM)

    assert cache.source_cached(STEM, want_bom=False)


def test_store_source_replaces_the_layer_wholesale(repo):
    write_outputs(repo, cams=(0, 1))
    cache.store_source(STEM)
    (repo.SVG_DIR / f"{STEM}_exploded_cam1.svg").unlink()
    (repo.SVG_DIR / f"{STEM}_assembled_cam1.svg").unlink()

    cache.store_source(STEM)

    assert not (cache.CACHE_DIR / STEM / f"{STEM}_exploded_cam1.svg").exists()


# ── snapshot layer ────────────────────────────────────────────────────────────


def test_snapshots_cached_after_store_is_true(repo):
    (repo.PATCH_DIR / f"{STEM}_exploded_cam0.json").write_text(
        '[{"id":"abcd","preop":"orig"}]'
    )
    (repo.SVG_DIR / f"{STEM}_exploded_cam0_abcd.svg").write_text("snap")

    cache.store_snapshots(STEM)

    assert cache.snapshots_cached(STEM)


def test_snapshots_cached_with_no_snapshots_is_trivially_true(repo):
    cache.store_snapshots(STEM)

    assert cache.snapshots_cached(STEM)


def test_snapshots_cached_after_patch_edit_is_false(repo):
    cache.store_snapshots(STEM)

    (repo.PATCH_DIR / f"{STEM}_exploded_cam0.json").write_text("[]")

    assert not cache.snapshots_cached(STEM)


def test_clear_snapshots_drops_snapshots_but_keeps_source(repo):
    # A patch re-replay must drop the old snapshot layer (a leaf op the
    # edit removed) without touching the cached .step / raw .svg.
    raw = repo.SVG_DIR / f"{STEM}_exploded_cam0.svg"
    raw.write_text("render")
    step = repo.STEP_DIR / f"{STEM}_exploded.step"
    step.write_text("geom")
    stale = repo.SVG_DIR / f"{STEM}_exploded_cam0_gone.svg"
    stale.write_text("stale snapshot")

    cache.clear_snapshots(STEM)

    assert not stale.exists()
    assert raw.read_text() == "render"
    assert step.read_text() == "geom"


def test_restore_snapshots_reconstructs_deleted_snapshot(repo):
    snap = repo.SVG_DIR / f"{STEM}_exploded_cam0_abcd.svg"
    snap.write_text("snap")
    cache.store_snapshots(STEM)
    snap.unlink()

    cache.restore_snapshots(STEM)

    assert snap.read_text() == "snap"


# ── output clearing & pruning ─────────────────────────────────────────────────


def test_clear_outputs_removes_only_this_stems_files(repo):
    write_outputs(repo)
    (repo.SVG_DIR / f"{STEM}_exploded_cam0.tmp").write_text("partial")
    other = repo.STEP_DIR / "belt_20_clamp_exploded.step"
    other.write_text("other")

    cache.clear_outputs(STEM)

    assert other.exists() and not list(repo.SVG_DIR.iterdir())


def test_prune_drops_entries_for_unknown_stems(repo):
    write_outputs(repo)
    cache.store_source(STEM)
    (cache.CACHE_DIR / "ghost_10_gone").mkdir()

    removed = cache.prune({STEM})

    assert (removed, (cache.CACHE_DIR / STEM).is_dir()) == (1, True)


def test_cumulative_cache_does_not_satisfy_delta_request(repo):
    write_outputs(repo)
    cache.store_source(STEM)
    assert not cache.source_cached(STEM, want_bom=True, want_delta=True)


def test_partial_camera_manifest_is_not_a_hit(repo):
    write_outputs(repo)
    cache.store_source(STEM)
    assert not cache.source_cached(
        STEM, want_bom=False, expected_svgs={f"{STEM}_exploded_cam1.svg"}
    )


def test_both_step_variants_are_required(repo):
    write_outputs(repo)
    (repo.STEP_DIR / f"{STEM}_assembled.step").unlink()
    cache.store_source(STEM)
    assert not cache.source_cached(STEM, want_bom=False)


@pytest.mark.parametrize(
    "files", [[], "bad", {"../escape.step": "a" * 64}, {"x.step": None}]
)
def test_malformed_manifest_is_a_miss(repo, files):
    write_outputs(repo)
    cache.store_source(STEM)
    (cache.CACHE_DIR / STEM / "source.key").write_text(
        json.dumps({"key": cache.source_key(STEM), "files": files})
    )
    assert not cache.source_cached(STEM, want_bom=False)


def test_unmanifested_cache_file_is_never_restored(repo):
    write_outputs(repo)
    cache.store_source(STEM)
    extra = f"{STEM}_exploded_cam99.svg"
    (cache.CACHE_DIR / STEM / extra).write_text("unexpected")
    cache.restore_source(STEM)
    assert not (repo.SVG_DIR / extra).exists()


def test_missing_snapshot_cannot_be_manifested_as_complete(repo):
    (repo.PATCH_DIR / f"{STEM}_exploded_cam0.json").write_text(
        '[{"id":"abcd","preop":"orig"}]'
    )
    cache.store_snapshots(STEM)
    assert not cache.snapshots_cached(STEM)


def test_added_relative_import_is_hashed_even_after_previous_lookup(repo):
    initial = cache.source_key(STEM)
    dependency = repo.helper.parent / "new.py"
    dependency.write_text("SIZE = 1")
    repo.helper.write_text("from .new import SIZE")
    changed = cache.source_key(STEM)
    assert changed != initial
    dependency.write_text("SIZE = 2")
    assert cache.source_key(STEM) != changed


def test_dependency_lock_changes_invalidate_source_cache(repo, tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_text("old kernel")
    previous = cache.source_key(STEM)
    lock.write_text("new kernel")
    assert cache.source_key(STEM) != previous


def test_orchestration_dependencies_invalidate_source_cache(repo):
    assembly = repo.PROCEDURES_DIR.parent
    (assembly / "build_procedures.py").write_text("from .writer import FORMAT\n")
    writer = assembly / "writer.py"
    writer.write_text("FORMAT = 1\n")
    previous = cache.source_key(STEM)
    writer.write_text("FORMAT = 2\n")
    assert cache.source_key(STEM) != previous


@pytest.mark.parametrize("change_sources", [False, True])
def test_interrupted_cache_update_cannot_pair_old_snapshots_with_new_geometry(
    repo, change_sources
):
    write_outputs(repo)
    (repo.PATCH_DIR / f"{STEM}_exploded_cam0.json").write_text(
        '[{"id":"abcd","preop":"orig"}]'
    )
    snapshot = repo.SVG_DIR / f"{STEM}_exploded_cam0_abcd.svg"
    snapshot.write_text("old annotated drawing")
    cache.store_source(STEM)
    cache.store_snapshots(STEM)
    assert cache.snapshots_cached(STEM)

    if change_sources:
        repo.helper.write_text("SIZE = 2\n")
    # A fresh render can differ even with identical source (e.g. after --no-cache).
    (repo.SVG_DIR / f"{STEM}_exploded_cam0.svg").write_text("new raw drawing")
    cache.store_source(STEM)
    # Simulate a crash before store_snapshots. The old snapshot is intact,
    # but belongs to a different generation of the source layer.
    assert cache.source_cached(STEM, want_bom=True)
    assert not cache.snapshots_cached(STEM)
    snapshot.write_text("new annotated drawing")
    cache.store_snapshots(STEM)
    assert cache.snapshots_cached(STEM)


def test_package_initialization_changes_invalidate_source_cache(repo):
    init = repo.helper.parent / "__init__.py"
    init.write_text("SIZE = 1\n")
    before = cache.source_key(STEM)
    init.write_text("SIZE = 2\n")
    assert cache.source_key(STEM) != before


def test_installed_project_import_changes_invalidate_source_cache(repo, tmp_path):
    shared = tmp_path / "src" / "physiclaw" / "core" / "arm_geometry.py"
    shared.parent.mkdir(parents=True)
    shared.write_text("X_TRAVEL = 110\n")
    repo.helper.write_text("from physiclaw.core.arm_geometry import X_TRAVEL\n")
    before = cache.source_key(STEM)
    shared.write_text("X_TRAVEL = 120\n")
    assert cache.source_key(STEM) != before


def test_entry_copied_from_another_procedure_is_rejected(repo):
    write_outputs(repo)
    cache.store_source(STEM)
    other = "frame_20_other"
    (repo.PROCEDURES_DIR / f"{other}.py").write_text(repo.procedure.read_text())
    shutil.copytree(repo.CACHE_DIR / STEM, repo.CACHE_DIR / other)
    assert not cache.source_cached(other, want_bom=True)


def test_manual_only_edit_preserves_geometry_cache(repo, tmp_path):
    write_outputs(repo)
    cache.store_source(STEM)
    manual = tmp_path / "hardware" / "manual" / "content.json"
    manual.parent.mkdir()
    manual.write_text('{"title":"updated manual"}')
    assert cache.source_cached(STEM, want_bom=True)


@pytest.mark.parametrize("layer", ["source", "snapshots"])
@pytest.mark.parametrize("change", ["bytes", "key"])
def test_cache_changed_after_hit_cannot_be_restored(repo, layer, change):
    write_outputs(repo)
    (repo.PATCH_DIR / f"{STEM}_exploded_cam0.json").write_text(
        '[{"id":"abcd","preop":"orig"}]'
    )
    snapshot = f"{STEM}_exploded_cam0_abcd.svg"
    (repo.SVG_DIR / snapshot).write_text("snapshot")
    cache.store_source(STEM)
    cache.store_snapshots(STEM)
    assert cache.source_cached(STEM, want_bom=True)
    assert cache.snapshots_cached(STEM)

    if change == "bytes":
        name = f"{STEM}_exploded.step" if layer == "source" else snapshot
        (repo.CACHE_DIR / STEM / name).write_text("changed after hit")
    elif layer == "source":
        repo.helper.write_text("SIZE = 2")
    else:
        (repo.PATCH_DIR / f"{STEM}_exploded_cam0.json").write_text("[]")
    restore = cache.restore_source if layer == "source" else cache.restore_snapshots
    with pytest.raises(RuntimeError, match="changed"):
        restore(STEM)


def test_corrupt_copy_never_succeeds(repo, monkeypatch):
    write_outputs(repo)
    cache.store_source(STEM)
    before = {
        path: path.read_bytes()
        for directory in (repo.STEP_DIR, repo.SVG_DIR, repo.BOM_DIR)
        for path in directory.iterdir()
    }
    monkeypatch.setattr(
        cache.shutil, "copy2", lambda src, dest: dest.write_text("bad copy")
    )
    with pytest.raises(RuntimeError, match="Cached artifact changed"):
        cache.restore_source(STEM)
    assert all(path.read_bytes() == data for path, data in before.items())
    assert not [
        path
        for directory in (repo.STEP_DIR, repo.SVG_DIR, repo.BOM_DIR)
        for path in directory.glob("*.tmp")
    ]


def test_interrupted_restore_preserves_complete_output_and_removes_partial_copy(
    repo, monkeypatch
):
    write_outputs(repo)
    cache.store_source(STEM)
    target = repo.STEP_DIR / f"{STEM}_exploded.step"
    target.write_text("existing complete output")

    def interrupt(src, dest):
        dest.write_text("partial copy")
        raise KeyboardInterrupt

    monkeypatch.setattr(cache.shutil, "copy2", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cache.restore_source(STEM)
    assert target.read_text() == "existing complete output"
    assert not list(repo.STEP_DIR.glob("*.tmp"))


def test_cache_store_rejects_sources_edited_after_build_started(repo):
    write_outputs(repo)
    expected = cache.source_key(STEM)
    repo.helper.write_text("SIZE = 2\n")
    with pytest.raises(RuntimeError, match="inputs changed"):
        cache.store_source(STEM, expected_key=expected)
    assert not cache.source_cached(STEM, want_bom=True)


@pytest.mark.parametrize("layer", ["source", "snapshots"])
def test_inputs_changed_during_copy_cannot_publish_a_cache_hit(
    repo, monkeypatch, layer
):
    write_outputs(repo)
    patch = repo.PATCH_DIR / f"{STEM}_exploded_cam0.json"
    patch.write_text('[{"id":"abcd","preop":"orig"}]')
    (repo.SVG_DIR / f"{STEM}_exploded_cam0_abcd.svg").write_text("snapshot")
    cache.store_source(STEM)
    cache.store_snapshots(STEM)
    copy = cache.shutil.copy2

    def edit_inputs(src, dest):
        result = copy(src, dest)
        if layer == "source":
            repo.helper.write_text("SIZE = 2\n")
        else:
            patch.write_text("[]")
        return result

    monkeypatch.setattr(cache.shutil, "copy2", edit_inputs)
    store = cache.store_source if layer == "source" else cache.store_snapshots
    with pytest.raises(RuntimeError, match="inputs changed"):
        store(STEM)
    if layer == "source":
        assert not cache.source_cached(STEM, want_bom=True)
    assert not cache.snapshots_cached(STEM)


def test_snapshot_store_rejects_patches_edited_during_render(repo):
    write_outputs(repo)
    patch = repo.PATCH_DIR / f"{STEM}_exploded_cam0.json"
    patch.write_text('[{"id":"abcd","preop":"orig"}]')
    (repo.SVG_DIR / f"{STEM}_exploded_cam0_abcd.svg").write_text("old snapshot")
    cache.store_source(STEM)
    expected = cache.annotation_key(STEM)
    patch.write_text("[]")
    with pytest.raises(RuntimeError, match="inputs changed"):
        cache.store_snapshots(STEM, expected_annotation_key=expected)
    assert not cache.snapshots_cached(STEM)


@pytest.mark.parametrize("layer", ["source", "snapshots"])
def test_corrupt_store_copy_cannot_become_a_valid_cache_entry(repo, monkeypatch, layer):
    write_outputs(repo)
    patch = repo.PATCH_DIR / f"{STEM}_exploded_cam0.json"
    patch.write_text('[{"id":"abcd","preop":"orig"}]')
    (repo.SVG_DIR / f"{STEM}_exploded_cam0_abcd.svg").write_text("snapshot")
    cache.store_source(STEM)
    monkeypatch.setattr(
        cache.shutil, "copy2", lambda src, dest: dest.write_text("bad copy")
    )
    store = cache.store_source if layer == "source" else cache.store_snapshots
    with pytest.raises(RuntimeError, match="Artifact changed while caching"):
        store(STEM)
    if layer == "source":
        assert not cache.source_cached(STEM, want_bom=True)
    assert not cache.snapshots_cached(STEM)


@pytest.mark.parametrize(
    "want_bom,want_delta", [(False, False), (True, False), (False, True)]
)
def test_cache_store_excludes_unrequested_boms(repo, want_bom, want_delta):
    write_outputs(repo)
    (repo.BOM_DIR / f"{STEM}_delta.md").write_text("delta BOM")
    cache.store_source(STEM, want_bom=want_bom, want_delta=want_delta)
    manifest = json.loads((repo.CACHE_DIR / STEM / "source.key").read_text())["files"]
    assert (f"{STEM}.md" in manifest) is want_bom
    assert (f"{STEM}_delta.md" in manifest) is want_delta
    assert cache.source_cached(STEM, want_bom=want_bom, want_delta=want_delta)
    assert cache.source_cached(STEM, want_bom=True) is want_bom
    assert cache.source_cached(STEM, want_bom=False, want_delta=True) is want_delta
