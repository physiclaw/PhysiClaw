"""Persistent per-step build cache for the procedure pipeline.

Three output kinds, two invalidation layers (their "subtly different logic"):

  * ``.step`` (geometry) and the raw ``.svg`` (render) share one **source
    key** — a content hash of the procedure's transitive hardware
    import closure plus
    the CAD dependency group's locked transitive toolchain and platform.
    Unrelated application metadata and agent/dev dependencies are excluded.
    Source tracking is conservative at file granularity. The cumulative /
    delta BOM ``.md`` is a pure function of the same parts. Change a part →
    source key changes → rebuild geometry, re-render, re-aggregate BOM.
  * the patch-snapshot ``.svg`` (the raw render with the mark-tool
    annotations composited in) carries an extra **patch key** on top: the
    stem's patch JSON(s), the replay engine's own source, and the exact
    source-layer manifest. Edit only a
    patch and the source key still matches — so we keep the cached ``.step``
    and raw ``.svg`` and just re-run the replay — no geometry rebuild, though
    the replay itself still imports build123d (via ``assembly.base``).

No stale cache, three ways:
  * stale HIT — the keys fold in every input file (import closure, kernel
    version, patch JSON, replay engine), so any change that could alter an
    output changes that output's key and forces a rebuild;
  * stale FILE — each stem keeps exactly one entry; ``store_source`` /
    ``store_snapshots`` replace their layer's files wholesale, and
    ``prune()`` drops entries for stems that no longer exist;
  * damaged ENTRY — each layer's key file is a manifest (key + each stored
    file's sha256), written last; a hit requires the key to match AND every
    listed file to be present with its recorded hash, so a partial entry
    (store killed mid-copy), a deleted file, or a corrupted/edited one all
    fall back to a rebuild instead of restoring a bad set. Restored copies are
    hashed again before use. (Output files are
    reconstructed from the entry on every hit, so tampering there self-heals
    on the next build.)

The cache lives at ``hardware/output/.cache`` (git-ignored with the rest of
``output/``) and needs no build123d import — only the stdlib — so the
dispatcher can decide hit/miss before spawning a worker.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import json
import platform
import re
import shutil
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from hardware.scheme import (
    BOM_DIR,
    CACHE_DIR,
    MANUAL_ILLUSTRATIONS,
    MARK_DIR,
    PATCH_DIR,
    PROCEDURES_DIR,
    RENDER_WORK_DIR,
    REPO_ROOT,
    STEP_DIR,
    SVG_DIR,
    VARIANTS,
    raw_svg_re,
    snapshot_svg_re,
)

_SOURCE_KEY = "source.key"
_PATCH_KEY = "patch.key"


# ── content hashing ──────────────────────────────────────────────────────────


def _module_to_path(mod: str) -> Path | None:
    """Resolve hardware source modules within this checkout."""
    if mod != "hardware" and not mod.startswith("hardware."):
        return None
    p = REPO_ROOT / Path(*mod.split("."))
    if p.with_suffix(".py").is_file():
        return p.with_suffix(".py")
    init = p / "__init__.py"
    return init if init.is_file() else None


def _imports(path: Path, *, include_lazy: bool = True) -> frozenset[str]:
    """Read current bytes on every check, including edits that preserve mtime."""
    return _parsed_imports(path, path.read_bytes(), REPO_ROOT, include_lazy)


@functools.lru_cache(maxsize=256)
def _parsed_imports(
    path: Path, source: bytes, repo_root: Path, include_lazy: bool
) -> frozenset[str]:
    """Cache parsing only; dependency resolution and hashing remain live."""
    mods: set[str] = set()
    tree = ast.parse(source, filename=str(path))
    if not include_lazy:
        # Loading a child module executes its parents' initializers, but does
        # not request their lazy exports. Keep all other imports conservative,
        # including function-local imports that ordinary build code may call.
        tree.body = [
            node
            for node in tree.body
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            or node.name != "__getattr__"
        ]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module
            if node.level:
                package = list(path.relative_to(repo_root).parts[:-1])
                package = package[: len(package) - node.level + 1]
                module = ".".join(package + ([module] if module else []))
            if module:
                mods.add(module)
                # `from hardware.parts import custom` names the SUBMODULE
                # hardware/parts/custom.py — record the package-qualified
                # name per alias too. Non-module names (functions, classes)
                # resolve to no file in _module_to_path and drop out.
                mods.update(f"{module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
    return frozenset(mods)


def _closure(
    *paths: Path, skip_imports: frozenset[tuple[Path, Path]] = frozenset()
) -> frozenset[Path]:
    """All project source files reachable from ``paths`` through imports."""
    seen: dict[Path, bool] = {}
    stack = [(p, True) for p in paths]
    while stack:
        p, include_lazy = stack.pop()
        if p in seen and (seen[p] or not include_lazy):
            continue
        # An explicit import may revisit an implicitly loaded initializer.
        # Upgrade it to include lazy exports regardless of traversal order.
        seen[p] = include_lazy
        # Python executes package initializers before importing their modules.
        for directory in p.parents:
            if directory == REPO_ROOT:
                break
            init = directory / "__init__.py"
            if init.is_file() and init not in seen:
                stack.append((init, False))
        for mod in _imports(p, include_lazy=include_lazy):
            dep = _module_to_path(mod)
            if (
                dep is not None
                and (p, dep) not in skip_imports
                and not seen.get(dep, False)
            ):
                stack.append((dep, True))
    return frozenset(seen)


@functools.cache
def _kernel_version() -> str:
    try:
        return version("build123d")
    except PackageNotFoundError:
        return "unknown"


def _hash_files(h: "hashlib._Hash", files) -> None:
    for f in sorted(files, key=lambda p: str(p)):
        h.update(str(f.relative_to(REPO_ROOT)).encode())
        h.update(b"\0")
        h.update(f.read_bytes() if f.is_file() else b"")
        h.update(b"\0")


def _cad_toolchain() -> bytes:
    """Only CAD requirements and their locked transitive package records.

    Application/dev dependencies and project metadata do not affect CAD.
    Keep every platform variant of reachable packages, including artifact
    hashes and dependency edges, so kernel and transitive upgrades invalidate.
    Parse current bytes on each check to detect edits during a build.
    """
    project_path = REPO_ROOT / "pyproject.toml"
    lock_path = REPO_ROOT / "uv.lock"
    return _parsed_cad_toolchain(project_path.read_bytes(), lock_path.read_bytes())


@functools.lru_cache(maxsize=8)
def _parsed_cad_toolchain(project_source: bytes, lock_source: bytes) -> bytes:
    """Reuse TOML parsing only for identical contents, never mtime or size."""
    project = tomllib.loads(project_source.decode("utf-8"))
    groups = project.get("dependency-groups", {})

    def requirements(group: str, visiting: frozenset[str] = frozenset()) -> list[str]:
        if group in visiting:
            raise ValueError(f"Cyclic dependency group: {group}")
        if group not in groups:
            raise ValueError(f"Missing CAD dependency group: {group}")
        result = []
        for item in groups[group]:
            if isinstance(item, str):
                result.append(item.strip())
            else:
                result.extend(requirements(item["include-group"], visiting | {group}))
        return result

    cad = sorted(requirements("cad"))
    lock = tomllib.loads(lock_source.decode("utf-8"))
    packages: dict[str, list[dict]] = {}
    for package in lock.get("package", []):
        packages.setdefault(package["name"], []).append(package)
    pending = [re.split(r"[\s\[<>=!~;@]", req, maxsplit=1)[0] for req in cad]
    selected = {}
    while pending:
        name = re.sub(r"[-_.]+", "-", pending.pop()).lower()
        if name in selected:
            continue
        if name not in packages:
            raise ValueError(f"Missing CAD lock record: {name}")
        records = packages[name]
        selected[name] = records
        for record in records:
            pending.extend(dep["name"] for dep in record.get("dependencies", []))
            # Extras can add CAD dependencies (e.g. an alternate exporter).
            for deps in record.get("optional-dependencies", {}).values():
                pending.extend(dep["name"] for dep in deps)
    return json.dumps({"cad": cad, "packages": selected}, sort_keys=True).encode()


def source_key(stem: str) -> str:
    """Content hash of ``stem``'s import closure, CAD toolchain and platform.
    Governs the ``.step``, the raw ``.svg``, and the BOM ``.md``."""
    h = hashlib.sha256()
    h.update(b"hardware-cache-v3\n")
    h.update(f"manual-illustrations={MANUAL_ILLUSTRATIONS}\n".encode())
    h.update(b"build123d=" + _kernel_version().encode() + b"\n")
    h.update(platform.python_version().encode())
    h.update(platform.platform().encode())
    h.update(_cad_toolchain())
    # Export orchestration and BOM code aren't necessarily imported by the
    # procedure. Only these known replay imports belong to the other layer.
    # Follow mark helpers reached by any other importer, including exporters
    # and BOM code: their location alone cannot prove they are annotation-only.
    assembly = REPO_ROOT / "hardware/assembly"
    inputs = {
        assembly / name
        for name in ("build_procedures.py", "bom.py", "cache.py")
        if (assembly / name).is_file()
    }
    annotation_imports = frozenset(
        (assembly / caller, MARK_DIR / module)
        for caller in ("build_procedures.py", "cache.py")
        for module in ("patch.py", "replay.py")
    )
    sources = _closure(
        PROCEDURES_DIR / f"{stem}.py", *inputs, skip_imports=annotation_imports
    )
    _hash_files(h, sources)
    return h.hexdigest()


def _patch_files(stem: str) -> list[Path]:
    out: list[Path] = []
    for v in VARIANTS:
        out += PATCH_DIR.glob(f"{stem}_{v}_cam*.json")
    return out


def annotation_key(stem: str) -> str:
    """Inputs to annotation replay, independent of the cached geometry generation."""
    h = hashlib.sha256()
    _hash_files(h, _closure(MARK_DIR / "replay.py"))
    _hash_files(h, _patch_files(stem))
    return h.hexdigest()


def patch_key(stem: str, *, inputs_key: str | None = None) -> str:
    """Bind annotated drawings to the exact source layer, patches and replay code."""
    h = hashlib.sha256()
    # A crash between the two stores must not pair new geometry with old
    # snapshots. File hashes also cover rerenders with unchanged source keys.
    h.update(json.dumps(_read_layer(stem, _SOURCE_KEY), sort_keys=True).encode())
    h.update((annotation_key(stem) if inputs_key is None else inputs_key).encode())
    return h.hexdigest()


# ── store / restore / prune ──────────────────────────────────────────────────


def _entry(stem: str) -> Path:
    return CACHE_DIR / stem


def _dest_dir(name: str) -> Path:
    return {".step": STEP_DIR, ".svg": SVG_DIR, ".md": BOM_DIR}[Path(name).suffix]


# Layer membership — one predicate per layer, shared by store (which old
# files to drop), restore (which to copy back), and the completeness checks.
def _is_source(stem: str, name: str, *, want_bom: bool = True) -> bool:
    """``name`` belongs to ``stem``'s source layer (.step / raw .svg / BOM)."""
    return (
        name in {f"{stem}_{v}.step" for v in VARIANTS}
        or bool(raw_svg_re(stem).match(name))
        or (want_bom and name in {f"{stem}.md", f"{stem}_delta.md"})
    )


def _is_snapshot(stem: str, name: str) -> bool:
    """``name`` belongs to ``stem``'s patch-snapshot layer."""
    return bool(snapshot_svg_re(stem).match(name))


def _svgs(stem: str, pat: re.Pattern) -> list[Path]:
    return [p for p in SVG_DIR.glob(f"{stem}_*_cam*.svg") if pat.match(p.name)]


def _source_outputs(
    stem: str, *, want_bom: bool = True, want_delta: bool = True
) -> list[Path]:
    """``.step`` + raw ``.svg`` + BOM ``.md`` for ``stem`` currently on disk."""
    files = [p for v in VARIANTS for p in STEP_DIR.glob(f"{stem}_{v}.step")]
    files += _svgs(stem, raw_svg_re(stem))
    files += [
        p
        for p, wanted in (
            (BOM_DIR / f"{stem}.md", want_bom),
            (BOM_DIR / f"{stem}_delta.md", want_delta),
        )
        if wanted and p.exists()
    ]
    return files


def _snapshot_outputs(stem: str) -> list[Path]:
    return _svgs(stem, snapshot_svg_re(stem))


def clear_outputs(stem: str) -> None:
    """Remove ``stem``'s output files (.step / .svg / .md), so a rebuild that
    now emits fewer cameras/variants doesn't leave a stale file behind.
    Sweeps the stem's in-progress .tmp files too (a SIGSEGV'd render or
    export can leave one)."""
    for variant in VARIANTS:
        shutil.rmtree(RENDER_WORK_DIR / f"{stem}_{variant}", ignore_errors=True)
    tmps = list(SVG_DIR.glob(f"{stem}_*_cam*.tmp"))
    tmps += STEP_DIR.glob(f"{stem}_*.tmp")
    for f in _source_outputs(stem) + _snapshot_outputs(stem) + tmps:
        f.unlink()


def clear_snapshots(stem: str) -> None:
    """Remove only ``stem``'s patch-snapshot SVGs (op-id-suffixed), leaving
    the cached .step / raw .svg / BOM intact. Used before a patch re-replay
    so the snapshot of a leaf op the edit removed or renamed doesn't survive
    on disk and get re-manifested into the fresh snapshot layer."""
    for f in _snapshot_outputs(stem):
        f.unlink()


def _restore(files) -> None:
    for f, sha in files:
        dest = _dest_dir(f.name)
        dest.mkdir(parents=True, exist_ok=True)
        restored = dest / f.name
        staged = restored.with_suffix(restored.suffix + ".tmp")
        try:
            shutil.copy2(f, staged)
            if _sha256_file(staged) != sha:
                raise RuntimeError(f"Cached artifact changed during restore: {f}")
            staged.replace(restored)
        finally:
            staged.unlink(missing_ok=True)


def _sha256_file(p: Path) -> str:
    """sha256 of a file's bytes, streamed (the .step files run to ~18 MB)."""
    with p.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _store_layer(
    stem: str, *, key_file: str, current_key, expected_key=None, belongs, outputs
) -> None:
    """Replace one layer of ``stem``'s entry: drop its old files (the layer's
    key + everything it ``belongs`` to), copy in the current ``outputs``, then
    write the key file LAST as a manifest ``{"key", "files": {name: sha256}}``.
    The other layer is untouched.

    Writing the manifest last makes the layer self-describing and crash-safe;
    the per-file digests let a reader verify integrity. A hit demands the key
    match AND every listed file be present with its recorded hash — so a partial
    entry (store killed mid-copy), a deleted file, or a corrupted/edited one all
    read as a miss, never a hit that restores a bad set."""
    key_value = current_key(stem)
    if expected_key is not None and key_value != expected_key:
        raise RuntimeError(f"Build inputs changed before caching: {stem}")
    entry = _entry(stem)
    entry.mkdir(parents=True, exist_ok=True)
    for f in entry.iterdir():
        if f.name == key_file or belongs(f.name):
            f.unlink()
    files: dict[str, str] = {}
    for f in outputs:
        dest = entry / f.name
        digest = _sha256_file(f)
        shutil.copy2(f, dest)
        if _sha256_file(dest) != digest:
            raise RuntimeError(f"Artifact changed while caching: {f}")
        files[f.name] = digest
    if current_key(stem) != key_value:
        raise RuntimeError(f"Build inputs changed while caching: {stem}")
    (entry / key_file).write_text(
        json.dumps({"key": key_value, "files": files}), encoding="utf-8"
    )


def _read_layer(stem: str, key_file: str) -> tuple[str | None, dict[str, str]]:
    """``(stored key, {filename: sha256})`` for a layer, or ``(None, {})`` when
    the key file is missing or unreadable — a pre-manifest entry, or one a
    killed store left half-written. Both read as a miss."""
    p = _entry(stem) / key_file
    if not p.is_file():
        return None, {}
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(m, dict) or not isinstance(m.get("files"), dict):
            return None, {}
        if not isinstance(m.get("key"), str):
            return None, {}
        for name, sha in m["files"].items():
            if (
                Path(name).name != name
                or not isinstance(sha, str)
                or not re.fullmatch(r"[a-f0-9]{64}", sha)
            ):
                return None, {}
        return m["key"], m["files"]
    except (ValueError, KeyError, TypeError, OSError):
        return None, {}


def _entry_intact(stem: str, files: dict[str, str]) -> bool:
    """Every manifested file is present in the entry with its recorded hash.
    The expensive check (it reads each stored file), so callers run the cheap
    key/manifest checks first and only reach here on a key match."""
    entry = _entry(stem)
    for name, sha in files.items():
        p = entry / name
        if (
            p.is_symlink()
            or not p.is_file()
            or p.stat().st_size == 0
            or _sha256_file(p) != sha
        ):
            return False
    return True


def store_source(
    stem: str,
    *,
    expected_key: str | None = None,
    want_bom: bool = True,
    want_delta: bool = True,
) -> None:
    """Replace ``stem``'s source layer (.step / raw .svg / .md) + source key."""
    _store_layer(
        stem,
        key_file=_SOURCE_KEY,
        current_key=source_key,
        expected_key=expected_key,
        belongs=lambda n: _is_source(stem, n),
        outputs=_source_outputs(stem, want_bom=want_bom, want_delta=want_delta),
    )


def store_snapshots(stem: str, *, expected_annotation_key: str | None = None) -> None:
    """Replace ``stem``'s snapshot layer (.svg op snapshots) + patch key."""
    _store_layer(
        stem,
        key_file=_PATCH_KEY,
        current_key=patch_key,
        expected_key=patch_key(stem, inputs_key=expected_annotation_key)
        if expected_annotation_key is not None
        else None,
        belongs=lambda n: _is_snapshot(stem, n),
        outputs=_snapshot_outputs(stem),
    )


def source_cached(
    stem: str,
    *,
    want_bom: bool,
    want_delta: bool = False,
    expected_svgs: set[str] | None = None,
    expected_key: str | None = None,
) -> bool:
    """True only if the source key matches AND every recorded file is present
    with its recorded hash — a changed, damaged, incomplete, or corrupted entry
    all fall back to a rebuild. Cheap checks first; the integrity hash runs only
    on a key match (a rebuild from a source edit pays no hashing)."""
    key, files = _read_layer(stem, _SOURCE_KEY)
    if key is None or key != (
        source_key(stem) if expected_key is None else expected_key
    ):
        return False
    required = {f"{stem}_{v}.step" for v in VARIANTS}
    if expected_svgs is not None:
        required.update(expected_svgs)
    if want_bom:
        required.add(f"{stem}.md")
    if want_delta:
        required.add(f"{stem}_delta.md")
    if not required.issubset(files):
        return False
    if any(not _is_source(stem, name) for name in files):
        return False
    return _entry_intact(stem, files)


def snapshots_cached(stem: str) -> bool:
    """True only if the patch key matches AND every recorded snapshot is present
    with its hash (a stem with no patches records an empty set — trivially true)."""
    key, files = _read_layer(stem, _PATCH_KEY)
    if key is None or key != patch_key(stem):
        return False
    from hardware.assembly.mark.replay import find_leaves

    expected = {
        f"{patch.stem}_{leaf['id']}.svg"
        for patch in _patch_files(stem)
        for leaf in find_leaves(json.loads(patch.read_text(encoding="utf-8")))
    }
    if set(files) != expected:
        return False
    return _entry_intact(stem, files)


def restore_source(stem: str, *, want_bom: bool = True) -> None:
    key, files = _read_layer(stem, _SOURCE_KEY)
    if key != source_key(stem):
        raise RuntimeError(f"Source cache changed before restore: {stem}")
    _restore(
        (_entry(stem) / name, sha)
        for name, sha in files.items()
        if _is_source(stem, name, want_bom=want_bom)
    )


def restore_snapshots(stem: str) -> None:
    key, files = _read_layer(stem, _PATCH_KEY)
    if key != patch_key(stem):
        raise RuntimeError(f"Drawing cache changed before restore: {stem}")
    _restore(
        (_entry(stem) / name, sha)
        for name, sha in files.items()
        if _is_snapshot(stem, name)
    )


def prune(valid_stems) -> int:
    """Drop cache entries for stems not in ``valid_stems``. Returns count."""
    if not CACHE_DIR.is_dir():
        return 0
    valid = set(valid_stems)
    removed = 0
    for entry in CACHE_DIR.iterdir():
        if entry.is_dir() and entry.name not in valid:
            clear_outputs(entry.name)
            shutil.rmtree(entry)
            removed += 1
    return removed
