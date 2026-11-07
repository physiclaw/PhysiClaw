"""Tests for hardware/assembly/cache.py — keys, layers, manifest integrity.

All directory constants are re-pointed at a per-test fake repo, so the
cache's hashing (which walks the procedure's project import closure
under ``REPO_ROOT``) and its store/restore run against tmp files only.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
from itertools import permutations
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
        "ANNOTATION_DIR": tmp_path / "hardware" / "output" / "annotations",
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
    procedure.write_text("import hardware.parts.helper\n", encoding="utf-8")
    helper = tmp_path / "hardware" / "parts" / "helper.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("SIZE = 1\n", encoding="utf-8")
    generator = procedure.parent.parent / "annotation" / "generator.py"
    generator.parent.mkdir()
    generator.with_name("__init__.py").write_text("", encoding="utf-8")
    generator.write_text("ENGINE = 1\n", encoding="utf-8")
    specs = generator.with_name("specs.py")
    specs.write_text("FIGURES = {}\n", encoding="utf-8")

    def expected_figures(stem):
        figures = ast.literal_eval(specs.read_text().split("=", 1)[1])
        return {name for name in figures if name.startswith(stem + "_")}

    monkeypatch.setattr(cache, "figure_names", expected_figures)
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "physiclaw"\nversion = "1.0"\n'
        'dependencies = ["anthropic>=1"]\n'
        '[dependency-groups]\ncad = ["build123d>=0.11"]\ndev = ["pytest"]\n',
        encoding="utf-8",
    )
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\n[[package]]\nname = "build123d"\nversion = "0.11"\n'
        'dependencies = [{name = "ocp"}]\n'
        '[[package]]\nname = "ocp"\nversion = "7.8"\n'
        '[[package]]\nname = "anthropic"\nversion = "1.0"\n',
        encoding="utf-8",
    )
    return SimpleNamespace(
        procedure=procedure,
        helper=helper,
        project=project,
        lock=lock,
        generator=generator,
        specs=specs,
        **dirs,
    )


def _set_figures(repo, *, present=True):
    name = f"{STEM}_exploded_cam0_annotated_screws"
    figures = {f"{name}.svg": {}} if present else {}
    repo.specs.write_text(f"FIGURES = {figures!r}  # edited\n", encoding="utf-8")
    if present:
        (repo.ANNOTATION_DIR / f"{name}.paths.svg").write_text(
            "paths", encoding="utf-8"
        )


def write_outputs(repo, *, bom: bool = True, cams: tuple[int, ...] = (0,)):
    """Materialize a complete source-layer output set for STEM."""
    for variant in ("exploded", "assembled"):
        (repo.STEP_DIR / f"{STEM}_{variant}.step").write_text(
            f"step {variant}", encoding="utf-8"
        )
        for cam in cams:
            (repo.SVG_DIR / f"{STEM}_{variant}_cam{cam}.svg").write_text(
                f"svg {variant} {cam}", encoding="utf-8"
            )
    if bom:
        (repo.BOM_DIR / f"{STEM}.md").write_text("| bom |", encoding="utf-8")


# ── keys ──────────────────────────────────────────────────────────────────────


def test_source_key_is_stable_across_calls(repo):
    assert cache.source_key(STEM) == cache.source_key(STEM)


def test_reference_illustrations_cannot_restore_manufacturing_cache(repo, monkeypatch):
    actual = cache.source_key(STEM)
    monkeypatch.setattr(cache, "MANUAL_ILLUSTRATIONS", True)
    assert cache.source_key(STEM) != actual


def test_source_key_changes_when_procedure_source_changes(repo):
    before = cache.source_key(STEM)

    repo.procedure.write_text(
        "import hardware.parts.helper  # edited\n", encoding="utf-8"
    )

    assert cache.source_key(STEM) != before


def test_source_key_changes_when_imported_dependency_changes(repo):
    before = cache.source_key(STEM)

    repo.helper.write_text("SIZE = 2\n", encoding="utf-8")

    assert cache.source_key(STEM) != before


def test_import_edits_with_unchanged_size_and_mtime_follow_current_dependencies(repo):
    cache.source_key(STEM)  # Populate the parsed-import lookup.
    second = repo.helper.with_name("second.py")
    second.write_text("SIZE = 1\n", encoding="utf-8")
    original = repo.procedure.stat()
    repo.procedure.write_text("import hardware.parts.second\n", encoding="utf-8")
    assert repo.procedure.stat().st_size == original.st_size
    os.utime(repo.procedure, ns=(original.st_atime_ns, original.st_mtime_ns))
    before = cache.source_key(STEM)

    original = second.stat()
    second.write_text("SIZE = 2\n", encoding="utf-8")
    os.utime(second, ns=(original.st_atime_ns, original.st_mtime_ns))
    after = cache.source_key(STEM)
    assert after != before

    repo.helper.write_text("SIZE = 3\n", encoding="utf-8")
    assert cache.source_key(STEM) == after  # The old dependency is no longer used.


def test_unchanged_imports_resolve_new_and_removed_modules_on_every_check(repo):
    repo.procedure.write_text("import hardware.parts.later\n", encoding="utf-8")
    before = cache.source_key(STEM)
    later = repo.helper.with_name("later.py")
    later.write_text("SIZE = 1\n", encoding="utf-8")
    assert cache.source_key(STEM) != before
    later.unlink()
    assert cache.source_key(STEM) == before


def test_source_key_changes_when_hardware_travel_changes(repo):
    travel = repo.PROCEDURES_DIR.parent / "travel_ranges.py"
    travel.write_text("X_TRAVEL = 110\nY_TRAVEL = 200\n", encoding="utf-8")
    repo.helper.write_text(
        "from __future__ import annotations\n"
        "from hardware.assembly.travel_ranges import X_TRAVEL\n",
        encoding="utf-8",
    )
    before = cache.source_key(STEM)

    travel.write_text("X_TRAVEL = 120\nY_TRAVEL = 200\n", encoding="utf-8")

    assert cache.source_key(STEM) != before


def test_illustration_key_changes_when_specs_change(repo):
    before = cache.illustration_key(STEM)

    _set_figures(repo, present=False)

    assert cache.illustration_key(STEM) != before


def test_illustration_key_changes_when_generator_changes(repo):
    before = cache.illustration_key(STEM)

    repo.generator.write_text("ENGINE = 2\n", encoding="utf-8")

    assert cache.illustration_key(STEM) != before


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

    assert (repo.STEP_DIR / f"{STEM}_exploded.step").read_text(
        encoding="utf-8"
    ) == "step exploded"


def test_restore_source_without_want_bom_skips_the_bom(repo):
    write_outputs(repo)
    cache.store_source(STEM)
    (repo.BOM_DIR / f"{STEM}.md").unlink()

    cache.restore_source(STEM, want_bom=False)

    assert not (repo.BOM_DIR / f"{STEM}.md").exists()


def test_source_cached_after_source_edit_is_false(repo):
    write_outputs(repo)
    cache.store_source(STEM)

    repo.procedure.write_text(
        "import hardware.parts.helper  # edited\n", encoding="utf-8"
    )

    assert not cache.source_cached(STEM, want_bom=True)


def test_source_cached_with_stored_file_deleted_is_false(repo):
    write_outputs(repo)
    cache.store_source(STEM)

    (cache.CACHE_DIR / STEM / f"{STEM}_exploded.step").unlink()

    assert not cache.source_cached(STEM, want_bom=True)


def test_source_cached_with_stored_file_corrupted_is_false(repo):
    write_outputs(repo)
    cache.store_source(STEM)

    (cache.CACHE_DIR / STEM / f"{STEM}_exploded.step").write_text(
        "tampered", encoding="utf-8"
    )

    assert not cache.source_cached(STEM, want_bom=True)


def test_source_cached_with_unreadable_manifest_is_false(repo):
    write_outputs(repo)
    cache.store_source(STEM)

    (cache.CACHE_DIR / STEM / "source.key").write_text("{broken", encoding="utf-8")

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


# ── annotation layer ────────────────────────────────────────────────────────────


def test_annotations_cached_after_store_is_true(repo):
    _set_figures(repo)
    (repo.SVG_DIR / f"{STEM}_exploded_cam0_annotated_screws.svg").write_text(
        "snap", encoding="utf-8"
    )

    cache.store_annotations(STEM)

    assert cache.annotations_cached(STEM)


def test_annotations_cached_with_no_annotations_is_trivially_true(repo):
    cache.store_annotations(STEM)

    assert cache.annotations_cached(STEM)


@pytest.mark.parametrize("damage", ["missing", "modified"])
def test_path_intermediate_is_required_and_verified_independently_of_geometry(
    repo, damage
):
    _set_figures(repo)
    write_outputs(repo)
    name = f"{STEM}_exploded_cam0_annotated_screws"
    (repo.SVG_DIR / f"{name}.svg").write_text("manual figure", encoding="utf-8")
    cache.store_source(STEM)
    cache.store_annotations(STEM)
    cached_paths = repo.CACHE_DIR / STEM / f"{name}.paths.svg"
    if damage == "missing":
        cached_paths.unlink()
    else:
        cached_paths.write_text("changed paths", encoding="utf-8")
    assert cache.source_cached(STEM, want_bom=True)
    assert not cache.annotations_cached(STEM)


def test_restore_annotations_restores_paths_to_their_own_directory(repo):
    _set_figures(repo)
    name = f"{STEM}_exploded_cam0_annotated_screws"
    final = repo.SVG_DIR / f"{name}.svg"
    paths = repo.ANNOTATION_DIR / f"{name}.paths.svg"
    final.write_text("manual figure", encoding="utf-8")
    cache.store_annotations(STEM)
    final.unlink()
    paths.unlink()
    cache.restore_annotations(STEM)
    assert final.read_text() == "manual figure"
    assert paths.read_text() == "paths"


def test_annotations_cached_after_declaration_edit_is_false(repo):
    cache.store_annotations(STEM)

    _set_figures(repo, present=False)

    assert not cache.annotations_cached(STEM)


def test_restore_annotations_reconstructs_deleted_annotation(repo):
    snap = repo.SVG_DIR / f"{STEM}_exploded_cam0_annotated_screws.svg"
    snap.write_text("snap", encoding="utf-8")
    cache.store_annotations(STEM)
    snap.unlink()

    cache.restore_annotations(STEM)

    assert snap.read_text(encoding="utf-8") == "snap"


# ── output clearing & pruning ─────────────────────────────────────────────────


def test_clear_outputs_removes_only_this_stems_files(repo):
    write_outputs(repo)
    (repo.SVG_DIR / f"{STEM}_exploded_cam0.tmp").write_text("partial", encoding="utf-8")
    other = repo.STEP_DIR / "belt_20_clamp_exploded.step"
    other.write_text("other", encoding="utf-8")

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
        json.dumps({"key": cache.source_key(STEM), "files": files, "inputs": {}}),
        encoding="utf-8",
    )
    assert not cache.source_cached(STEM, want_bom=False)


def test_unmanifested_cache_file_is_never_restored(repo):
    write_outputs(repo)
    cache.store_source(STEM)
    extra = f"{STEM}_exploded_cam99.svg"
    (cache.CACHE_DIR / STEM / extra).write_text("unexpected", encoding="utf-8")
    cache.restore_source(STEM)
    assert not (repo.SVG_DIR / extra).exists()


def test_missing_annotation_cannot_be_manifested_as_complete(repo):
    _set_figures(repo)
    cache.store_annotations(STEM)
    assert not cache.annotations_cached(STEM)


def test_added_relative_import_is_hashed_even_after_previous_lookup(repo):
    initial = cache.source_key(STEM)
    dependency = repo.helper.parent / "new.py"
    dependency.write_text("SIZE = 1", encoding="utf-8")
    repo.helper.write_text("from .new import SIZE", encoding="utf-8")
    changed = cache.source_key(STEM)
    assert changed != initial
    dependency.write_text("SIZE = 2", encoding="utf-8")
    assert cache.source_key(STEM) != changed


def replace_text(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text, f"Missing test input in {path}: {old!r}"
    path.write_text(text.replace(old, new), encoding="utf-8")


@pytest.mark.parametrize(
    "old,new", [("0.11", "0.12"), ("7.8", "7.9")], ids=["kernel", "transitive"]
)
def test_dependency_lock_changes_invalidate_source_cache(repo, old, new):
    previous = cache.source_key(STEM)
    replace_text(repo.lock, f'version = "{old}"', f'version = "{new}"')
    assert cache.source_key(STEM) != previous


@pytest.mark.parametrize(
    "file,old,new",
    [
        ("lock", 'version = "1.0"', 'version = "2.0"'),
        ("project", 'version = "1.0"', 'version = "2.0"'),
        ("project", "anthropic>=1", "anthropic>=2"),
        ("project", '["pytest"]', '["pytest", "ruff"]'),
    ],
    ids=["agent-lock", "app-version", "app-dependency", "dev"],
)
def test_unrelated_project_changes_preserve_cached_outputs(repo, file, old, new):
    write_outputs(repo)
    cache.store_source(STEM)
    cache.store_annotations(STEM)
    before = cache.source_key(STEM), cache.annotation_key(STEM)
    replace_text(getattr(repo, file), old, new)
    assert (cache.source_key(STEM), cache.annotation_key(STEM)) == before
    assert cache.source_cached(STEM, want_bom=True)
    assert cache.annotations_cached(STEM)


def test_cad_requirements_and_new_transitive_dependencies_invalidate(repo):
    before = cache.source_key(STEM)
    replace_text(repo.project, ">=0.11", ">=0.12")
    assert cache.source_key(STEM) != before
    before = cache.source_key(STEM)
    replace_text(
        repo.lock,
        'name = "ocp"\nversion = "7.8"',
        'name = "ocp"\nversion = "7.8"\ndependencies = [{name = "anthropic"}]',
    )
    assert cache.source_key(STEM) != before
    before = cache.source_key(STEM)
    replace_text(repo.lock, 'version = "1.0"', 'version = "2.0"')
    assert cache.source_key(STEM) != before


def test_annotation_only_edits_do_not_invalidate_geometry(repo):
    (repo.PROCEDURES_DIR.parent / "build_procedures.py").write_text(
        "from hardware.assembly.annotation.generator import generate\n",
        encoding="utf-8",
    )
    write_outputs(repo)
    cache.store_source(STEM)
    cache.store_annotations(STEM)
    repo.generator.write_text("ENGINE = 2\n", encoding="utf-8")
    assert cache.source_cached(STEM, want_bom=True)
    assert not cache.annotations_cached(STEM)


def test_annotation_generator_tracks_imported_helpers(repo):
    repo.generator.write_text("import hardware.parts.helper\n", encoding="utf-8")
    before = cache.annotation_key(STEM)
    repo.helper.write_text("SIZE = 2\n", encoding="utf-8")
    assert cache.annotation_key(STEM) != before


def test_direct_geometry_import_of_annotation_helper_is_still_tracked(repo):
    repo.procedure.write_text(
        "import hardware.assembly.annotation.generator\n", encoding="utf-8"
    )
    before = cache.source_key(STEM)
    repo.generator.write_text("ENGINE = 2\n", encoding="utf-8")
    assert cache.source_key(STEM) != before


def test_unimported_part_edit_preserves_cache(repo):
    other = repo.helper.with_name("other.py")
    other.write_text("SIZE = 1\n", encoding="utf-8")
    before = cache.source_key(STEM)
    other.write_text("SIZE = 2\n", encoding="utf-8")
    assert cache.source_key(STEM) == before


@pytest.mark.parametrize(
    "declaration",
    [
        'cad = [{include-group = "kernel"}]\nkernel = ["build123d>=0.11"]',
        'cad = ["  build123d>=0.11  "]',
    ],
    ids=["included-group", "whitespace"],
)
def test_requirement_forms_preserve_transitive_tracking(repo, declaration):
    replace_text(repo.project, 'cad = ["build123d>=0.11"]', declaration)
    before = cache.source_key(STEM)
    replace_text(repo.lock, 'version = "7.8"', 'version = "7.9"')
    assert cache.source_key(STEM) != before


@pytest.mark.parametrize("package,invalidates", [("ocp", True), ("anthropic", False)])
def test_only_cad_artifact_hash_changes_invalidate_cache(repo, package, invalidates):
    replace_text(
        repo.lock,
        f'name = "{package}"\nversion',
        f'name = "{package}"\nwheels = [{{hash = "sha256:old"}}]\nversion',
    )
    before = cache.source_key(STEM)
    replace_text(repo.lock, "sha256:old", "sha256:new")
    assert (cache.source_key(STEM) != before) is invalidates


def test_malformed_toolchain_cannot_silently_reuse_cache(repo):
    write_outputs(repo)
    cache.store_source(STEM)
    repo.lock.write_text("not valid TOML", encoding="utf-8")
    with pytest.raises(ValueError):
        cache.source_cached(STEM, want_bom=True)


@pytest.mark.parametrize("name", ["pyproject.toml", "uv.lock"])
def test_missing_toolchain_file_cannot_produce_a_cache_key(repo, tmp_path, name):
    (tmp_path / name).unlink()
    with pytest.raises(FileNotFoundError):
        cache.source_key(STEM)


@pytest.mark.parametrize("package", ["build123d", "ocp"])
def test_missing_cad_package_record_cannot_produce_a_cache_key(repo, package):
    replace_text(
        repo.lock, f'name = "{package}"\nversion', 'name = "unrelated"\nversion'
    )
    with pytest.raises(ValueError, match="Missing CAD lock record"):
        cache.source_key(STEM)


def test_missing_included_group_cannot_produce_a_cache_key(repo):
    repo.project.write_text(
        '[dependency-groups]\ncad = [{include-group = "missing"}]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Missing CAD dependency group"):
        cache.source_key(STEM)


def test_orchestration_geometry_helper_remains_a_dependency(repo):
    assembly = repo.PROCEDURES_DIR.parent
    (assembly / "build_procedures.py").write_text(
        "from .writer import export\n", encoding="utf-8"
    )
    (assembly / "writer.py").write_text(
        "from .geometry import SIZE\n", encoding="utf-8"
    )
    helper = repo.PROCEDURES_DIR.parent / "geometry.py"
    helper.write_text("SIZE = 1\n", encoding="utf-8")
    before = cache.source_key(STEM)
    helper.write_text("SIZE = 2\n", encoding="utf-8")
    assert cache.source_key(STEM) != before


def test_orchestration_dependencies_invalidate_source_cache(repo):
    assembly = repo.PROCEDURES_DIR.parent
    (assembly / "build_procedures.py").write_text(
        "from .writer import FORMAT\n", encoding="utf-8"
    )
    writer = assembly / "writer.py"
    writer.write_text("FORMAT = 1\n", encoding="utf-8")
    previous = cache.source_key(STEM)
    writer.write_text("FORMAT = 2\n", encoding="utf-8")
    assert cache.source_key(STEM) != previous


@pytest.mark.parametrize("change_sources", [False, True])
def test_interrupted_cache_update_cannot_pair_old_annotations_with_new_geometry(
    repo, change_sources
):
    write_outputs(repo)
    _set_figures(repo)
    annotation = repo.SVG_DIR / f"{STEM}_exploded_cam0_annotated_screws.svg"
    annotation.write_text("old annotated drawing", encoding="utf-8")
    cache.store_source(STEM)
    cache.store_annotations(STEM)
    assert cache.annotations_cached(STEM)

    if change_sources:
        repo.helper.write_text("SIZE = 2\n", encoding="utf-8")
    # A fresh render can differ even with identical source (e.g. after --no-cache).
    (repo.SVG_DIR / f"{STEM}_exploded_cam0.svg").write_text(
        "new raw drawing", encoding="utf-8"
    )
    cache.store_source(STEM)
    # Simulate a crash before store_annotations. The old annotation is intact,
    # but belongs to a different generation of the source layer.
    assert cache.source_cached(STEM, want_bom=True)
    assert not cache.annotations_cached(STEM)
    annotation.write_text("new annotated drawing", encoding="utf-8")
    cache.store_annotations(STEM)
    assert cache.annotations_cached(STEM)


def test_package_initialization_changes_invalidate_source_cache(repo):
    init = repo.helper.parent / "__init__.py"
    init.write_text("SIZE = 1\n", encoding="utf-8")
    before = cache.source_key(STEM)
    init.write_text("SIZE = 2\n", encoding="utf-8")
    assert cache.source_key(STEM) != before


def test_runtime_travel_changes_preserve_source_cache(repo, tmp_path):
    runtime = tmp_path / "src" / "physiclaw" / "core" / "hardware" / "arm.py"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("X_TRAVEL = 110\n", encoding="utf-8")
    before = cache.source_key(STEM)
    runtime.write_text("X_TRAVEL = 120\n", encoding="utf-8")
    assert cache.source_key(STEM) == before


@pytest.fixture
def lazy_package(repo, tmp_path):
    package = tmp_path / "hardware" / "lazy"
    package.mkdir(parents=True)
    shared = package / "arm_geometry.py"
    shared.write_text("X_TRAVEL = 110\n", encoding="utf-8")
    runtime = package / "runtime.py"
    runtime.write_text("PhysiClaw = 1\n", encoding="utf-8")
    eager = package / "eager.py"
    eager.write_text("SIZE = 1\n", encoding="utf-8")
    (package / "__init__.py").write_text(
        "from .eager import SIZE\n"
        "def __getattr__(name):\n"
        "    from .runtime import PhysiClaw\n"
        "    return PhysiClaw\n",
        encoding="utf-8",
    )
    repo.helper.write_text(
        "from hardware.lazy.arm_geometry import X_TRAVEL\n",
        encoding="utf-8",
    )
    return SimpleNamespace(shared=shared, runtime=runtime, eager=eager)


def test_unused_lazy_export_preserves_geometry_cache(repo, lazy_package):
    write_outputs(repo)
    cache.store_source(STEM)
    before = cache.source_key(STEM)

    lazy_package.runtime.write_text("PhysiClaw = 2\n", encoding="utf-8")

    assert cache.source_key(STEM) == before
    assert cache.source_cached(STEM, want_bom=True)

    lazy_package.shared.write_text("X_TRAVEL = 120\n", encoding="utf-8")
    assert not cache.source_cached(STEM, want_bom=True)


def test_implicit_initializer_eager_imports_remain_dependencies(repo, lazy_package):
    before = cache.source_key(STEM)
    lazy_package.eager.write_text("SIZE = 2\n", encoding="utf-8")
    assert cache.source_key(STEM) != before


@pytest.mark.parametrize(
    "imports",
    [
        "from hardware.lazy import PhysiClaw\n",
        "import hardware.lazy\n",
    ],
)
def test_explicit_package_import_tracks_lazy_exports(repo, lazy_package, imports):
    repo.helper.write_text(imports, encoding="utf-8")
    before = cache.source_key(STEM)
    lazy_package.runtime.write_text("PhysiClaw = 2\n", encoding="utf-8")
    assert cache.source_key(STEM) != before


@pytest.mark.parametrize("explicit", [False, True])
def test_cyclic_imports_preserve_lazy_boundary_in_every_visit_order(
    lazy_package, explicit
):
    lazy_package.shared.write_text("from .eager import SIZE\n", encoding="utf-8")
    lazy_package.eager.write_text(
        "from .arm_geometry import X_TRAVEL\n", encoding="utf-8"
    )
    init = lazy_package.shared.with_name("__init__.py")
    roots = [lazy_package.shared, lazy_package.eager] + ([init] if explicit else [])
    expected = {init, lazy_package.shared, lazy_package.eager}
    if explicit:
        expected.add(lazy_package.runtime)
    for paths in permutations(roots):
        assert cache._closure(*paths) == expected


def test_function_local_build_import_remains_a_dependency(repo):
    repo.procedure.write_text(
        "def build():\n    import hardware.parts.helper\n", encoding="utf-8"
    )
    before = cache.source_key(STEM)
    repo.helper.write_text("SIZE = 2\n", encoding="utf-8")
    assert cache.source_key(STEM) != before


def test_entry_copied_from_another_procedure_is_rejected(repo):
    write_outputs(repo)
    cache.store_source(STEM)
    other = "frame_20_other"
    (repo.PROCEDURES_DIR / f"{other}.py").write_text(
        repo.procedure.read_text(encoding="utf-8"), encoding="utf-8"
    )
    shutil.copytree(repo.CACHE_DIR / STEM, repo.CACHE_DIR / other)
    assert not cache.source_cached(other, want_bom=True)


def test_manual_only_edit_preserves_geometry_cache(repo, tmp_path):
    write_outputs(repo)
    cache.store_source(STEM)
    manual = tmp_path / "hardware" / "manual" / "content.json"
    manual.parent.mkdir()
    manual.write_text('{"title":"updated manual"}', encoding="utf-8")
    assert cache.source_cached(STEM, want_bom=True)


@pytest.mark.parametrize("layer", ["source", "annotations"])
@pytest.mark.parametrize("change", ["bytes", "key"])
def test_cache_changed_after_hit_cannot_be_restored(repo, layer, change):
    write_outputs(repo)
    _set_figures(repo)
    annotation = f"{STEM}_exploded_cam0_annotated_screws.svg"
    (repo.SVG_DIR / annotation).write_text("annotation", encoding="utf-8")
    cache.store_source(STEM)
    cache.store_annotations(STEM)
    assert cache.source_cached(STEM, want_bom=True)
    assert cache.annotations_cached(STEM)

    if change == "bytes":
        name = f"{STEM}_exploded.step" if layer == "source" else annotation
        (repo.CACHE_DIR / STEM / name).write_text("changed after hit", encoding="utf-8")
    elif layer == "source":
        repo.helper.write_text("SIZE = 2", encoding="utf-8")
    else:
        _set_figures(repo, present=False)
    restore = cache.restore_source if layer == "source" else cache.restore_annotations
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
        cache.shutil,
        "copy2",
        lambda src, dest: dest.write_text("bad copy", encoding="utf-8"),
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
    target.write_text("existing complete output", encoding="utf-8")

    def interrupt(src, dest):
        dest.write_text("partial copy", encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(cache.shutil, "copy2", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cache.restore_source(STEM)
    assert target.read_text(encoding="utf-8") == "existing complete output"
    assert not list(repo.STEP_DIR.glob("*.tmp"))


def test_cache_store_rejects_sources_edited_after_build_started(repo):
    write_outputs(repo)
    expected = cache.source_key(STEM)
    repo.helper.write_text("SIZE = 2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="inputs changed"):
        cache.store_source(STEM, expected_key=expected)
    assert not cache.source_cached(STEM, want_bom=True)


@pytest.mark.parametrize("layer", ["source", "annotations"])
def test_inputs_changed_during_copy_cannot_publish_a_cache_hit(
    repo, monkeypatch, layer
):
    write_outputs(repo)
    _set_figures(repo)
    (repo.SVG_DIR / f"{STEM}_exploded_cam0_annotated_screws.svg").write_text(
        "annotation", encoding="utf-8"
    )
    cache.store_source(STEM)
    cache.store_annotations(STEM)
    copy = cache.shutil.copy2

    def edit_inputs(src, dest):
        result = copy(src, dest)
        if layer == "source":
            repo.helper.write_text("SIZE = 2\n", encoding="utf-8")
        else:
            _set_figures(repo, present=False)
        return result

    monkeypatch.setattr(cache.shutil, "copy2", edit_inputs)
    store = cache.store_source if layer == "source" else cache.store_annotations
    with pytest.raises(RuntimeError, match="inputs changed"):
        store(STEM)
    if layer == "source":
        assert not cache.source_cached(STEM, want_bom=True)
    assert not cache.annotations_cached(STEM)


def test_annotation_store_rejects_declarations_edited_during_render(repo):
    write_outputs(repo)
    _set_figures(repo)
    (repo.SVG_DIR / f"{STEM}_exploded_cam0_annotated_screws.svg").write_text(
        "old annotation", encoding="utf-8"
    )
    cache.store_source(STEM)
    expected = cache.annotation_key(STEM)
    _set_figures(repo, present=False)
    with pytest.raises(RuntimeError, match="inputs changed"):
        cache.store_annotations(STEM, expected_annotation_key=expected)
    assert not cache.annotations_cached(STEM)


@pytest.mark.parametrize("layer", ["source", "annotations"])
def test_corrupt_store_copy_cannot_become_a_valid_cache_entry(repo, monkeypatch, layer):
    write_outputs(repo)
    _set_figures(repo)
    (repo.SVG_DIR / f"{STEM}_exploded_cam0_annotated_screws.svg").write_text(
        "annotation", encoding="utf-8"
    )
    cache.store_source(STEM)
    monkeypatch.setattr(
        cache.shutil,
        "copy2",
        lambda src, dest: dest.write_text("bad copy", encoding="utf-8"),
    )
    store = cache.store_source if layer == "source" else cache.store_annotations
    with pytest.raises(RuntimeError, match="Artifact changed while caching"):
        store(STEM)
    if layer == "source":
        assert not cache.source_cached(STEM, want_bom=True)
    assert not cache.annotations_cached(STEM)


@pytest.mark.parametrize(
    "want_bom,want_delta", [(False, False), (True, False), (False, True)]
)
def test_cache_store_excludes_unrequested_boms(repo, want_bom, want_delta):
    write_outputs(repo)
    (repo.BOM_DIR / f"{STEM}_delta.md").write_text("delta BOM", encoding="utf-8")
    cache.store_source(STEM, want_bom=want_bom, want_delta=want_delta)
    manifest = json.loads(
        (repo.CACHE_DIR / STEM / "source.key").read_text(encoding="utf-8")
    )["files"]
    assert (f"{STEM}.md" in manifest) is want_bom
    assert (f"{STEM}_delta.md" in manifest) is want_delta
    assert cache.source_cached(STEM, want_bom=want_bom, want_delta=want_delta)
    assert cache.source_cached(STEM, want_bom=True) is want_bom
    assert cache.source_cached(STEM, want_bom=False, want_delta=True) is want_delta


def test_cache_miss_names_changed_added_and_removed_source_inputs(repo):
    write_outputs(repo)
    cache.store_source(STEM)
    second = repo.helper.with_name("second.py")
    second.write_text("SIZE = 2\n", encoding="utf-8")
    repo.procedure.write_text("import hardware.parts.second\n", encoding="utf-8")
    reasons = []

    assert not cache.source_cached(STEM, want_bom=True, reasons=reasons)
    assert reasons == [
        f"input changed: hardware/assembly/procedures/{STEM}.py",
        "input removed: hardware/parts/helper.py",
        "input added: hardware/parts/second.py",
    ]


@pytest.mark.parametrize(
    "file,old,new,label",
    [
        ("project", ">=0.11", ">=0.12", "pyproject.toml (CAD requirements)"),
        ("lock", 'version = "7.8"', 'version = "7.9"', "uv.lock (CAD dependencies)"),
    ],
)
def test_cache_miss_names_relevant_toolchain_file(repo, file, old, new, label):
    write_outputs(repo)
    cache.store_source(STEM)
    replace_text(getattr(repo, file), old, new)
    reasons = []

    assert not cache.source_cached(STEM, want_bom=True, reasons=reasons)
    assert reasons == [f"input changed: {label}"]


def test_cache_miss_explains_python_version_change(repo, monkeypatch):
    write_outputs(repo)
    cache.store_source(STEM)
    monkeypatch.setattr(cache.platform, "python_version", lambda: "different Python")
    reasons = []

    assert not cache.source_cached(STEM, want_bom=True, reasons=reasons)
    assert reasons == ["input changed: Python version"]


@pytest.mark.parametrize("change", ["missing", "damaged", "empty"])
def test_cache_miss_names_damaged_or_missing_artifact(repo, change):
    write_outputs(repo)
    cache.store_source(STEM)
    artifact = repo.CACHE_DIR / STEM / f"{STEM}_assembled.step"
    if change == "missing":
        artifact.unlink()
    else:
        artifact.write_bytes(b"" if change == "empty" else b"damaged")
    reasons = []

    assert not cache.source_cached(STEM, want_bom=True, reasons=reasons)
    kind = "missing" if change == "missing" else "damaged"
    assert reasons == [
        f"cached artifact {kind}: {artifact.relative_to(cache.REPO_ROOT).as_posix()}"
    ]


@pytest.mark.parametrize("invalid", [False, True])
def test_cache_miss_explains_missing_or_invalid_manifest(repo, invalid):
    manifest = repo.CACHE_DIR / STEM / "source.key"
    if invalid:
        manifest.parent.mkdir()
        manifest.write_text("{partial", encoding="utf-8")
    reasons = []

    assert not cache.source_cached(STEM, want_bom=False, reasons=reasons)
    assert ("invalid or unreadable" if invalid else "missing") in reasons[0]
    assert manifest.relative_to(cache.REPO_ROOT).as_posix() in reasons[0]


def test_cache_miss_names_unrecorded_requested_output(repo):
    write_outputs(repo, bom=False)
    cache.store_source(STEM)
    reasons = []

    assert not cache.source_cached(STEM, want_bom=True, reasons=reasons)
    assert reasons == [f"cached output not recorded: {STEM}.md"]


@pytest.mark.parametrize("layer", ["source", "annotation"])
def test_cache_manifest_without_input_history_is_rejected(repo, layer):
    write_outputs(repo)
    cache.store_source(STEM)
    cache.store_annotations(STEM)
    path = repo.CACHE_DIR / STEM / f"{layer}.key"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    del manifest["inputs"]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    reasons = []
    if layer == "source":
        assert not cache.source_cached(STEM, want_bom=True, reasons=reasons)
    else:
        assert not cache.annotations_cached(STEM, reasons=reasons)
    assert "cache manifest invalid or unreadable" in reasons[0]


@pytest.mark.parametrize("kind", ["declarations", "generator", "render"])
def test_annotation_cache_miss_names_changed_file_and_preserves_geometry(repo, kind):
    write_outputs(repo)
    declarations = repo.specs
    _set_figures(repo, present=False)
    cache.store_source(STEM)
    cache.store_annotations(STEM)
    if kind == "declarations":
        changed = declarations
    elif kind == "generator":
        changed = repo.generator
    else:
        changed = repo.SVG_DIR / f"{STEM}_exploded_cam0.svg"
    changed.write_text(changed.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    if kind == "render":
        cache.store_source(STEM)
        changed = repo.CACHE_DIR / STEM / changed.name
    reasons = []

    assert cache.source_cached(STEM, want_bom=True)
    assert not cache.annotations_cached(STEM, reasons=reasons)
    assert reasons == [
        f"input changed: {changed.relative_to(cache.REPO_ROOT).as_posix()}"
    ]


def test_diagnostic_history_is_not_part_of_artifact_identity(repo):
    write_outputs(repo)
    cache.store_source(STEM)
    cache.store_annotations(STEM)
    path = repo.CACHE_DIR / STEM / "source.key"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["inputs"] = {"old diagnostic": "value"}
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert cache.source_cached(STEM, want_bom=True)
    assert cache.annotations_cached(STEM)
