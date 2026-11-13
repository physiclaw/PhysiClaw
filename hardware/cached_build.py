"""Validate and restore a complete warm build before importing the CAD dispatcher.

The receipt records the views checked by a successful ordinary build. It is
only a completeness hint: source keys and artifact contents are checked again
on every run. Any miss returns to the normal dispatcher and its CAD checks.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from hardware import buildlog
from hardware.assembly import cache
from hardware.assembly.discovery import list_procedures
from hardware.build_lock import locked_main
from hardware.port.processes import CancellationEvent
from hardware.scheme import OUTPUT_DIR, VARIANTS, svg_path_for

RECEIPT = "complete-build.json"
RECEIPT_SCHEMA = 2


@dataclass(frozen=True)
class _Entry:
    source: tuple[str | None, dict[str, str]]
    annotations: tuple[str | None, dict[str, str]]

    @property
    def files(self):
        return {
            name: sha
            for name, sha in (self.source[1] | self.annotations[1]).items()
            if not name.endswith(".md")
        }

    def unchanged(self, stem):
        return (
            cache._read_layer(stem, cache._SOURCE_KEY) == self.source
            and cache._read_layer(stem, cache._ANNOTATION_KEY) == self.annotations
        )


def _check_cancelled():
    if CancellationEvent().is_set():
        raise KeyboardInterrupt


def _read_receipt():
    try:
        path = cache.CACHE_DIR / RECEIPT
        if path.is_symlink():
            return None
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema") != RECEIPT_SCHEMA
            or not isinstance(receipt.get("steps"), dict)
            or set(receipt["steps"]) != set(list_procedures())
        ):
            return None
        for stem, step in receipt["steps"].items():
            if (
                not isinstance(step, dict)
                or not isinstance(step.get("key"), str)
                or not isinstance(step.get("svgs"), list)
            ):
                return None
            if any(not cache.raw_svg_re(stem).fullmatch(name) for name in step["svgs"]):
                return None
        return receipt["steps"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _record_build(checked_views, source_keys):
    """Record the dispatcher's verified view declarations, never infer completeness."""
    if set(checked_views) != set(list_procedures()) or set(source_keys) != set(
        checked_views
    ):
        return False
    steps = {}
    for stem, views in checked_views.items():
        _check_cancelled()
        key = source_keys[stem]
        if cache.source_key(stem) != key or not cache.source_cached(
            stem, expected_key=key, expected_svgs=set(views), want_bom=False
        ):
            return False
        steps[stem] = {
            "key": key,
            "svgs": sorted(views),
        }
    path = cache.CACHE_DIR / RECEIPT
    staged = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".complete-build-",
            delete=False,
        ) as stream:
            staged = Path(stream.name)
            json.dump({"schema": RECEIPT_SCHEMA, "steps": steps}, stream)
        staged.replace(path)
        return True
    except OSError:
        # The receipt is optional; its failure must not undo a successful build.
        return False
    finally:
        if staged is not None:
            with suppress(OSError):
                staged.unlink(missing_ok=True)


def _cached_plan():
    steps = _read_receipt()
    if steps is None:
        return None
    annotation_key = cache.annotation_key("")
    plan = {}
    for stem, step in steps.items():
        _check_cancelled()
        entry = _Entry(
            cache._read_layer(stem, cache._SOURCE_KEY),
            cache._read_layer(stem, cache._ANNOTATION_KEY),
        )
        if (
            entry.source[0] != step["key"]
            or cache.source_key(stem) != step["key"]
            or not cache.source_cached(
                stem,
                expected_key=step["key"],
                expected_svgs=set(step["svgs"]),
                want_bom=False,
            )
        ):
            return None
        key, annotations = entry.annotations
        names = cache.figure_names(stem)
        expected = names | {name.removesuffix(".svg") + ".paths.svg" for name in names}
        if (
            key != cache.illustration_key(stem, inputs_key=annotation_key)
            or annotations.keys() != expected
            or not cache._entry_intact(stem, annotations)
            or not entry.unchanged(stem)
        ):
            return None
        plan[stem] = entry
    return steps, annotation_key, plan


def _restore(plan):
    for stem, entry in plan.items():
        _check_cancelled()
        if not entry.unchanged(stem):
            raise RuntimeError(
                f"Cached manifests changed during the run; rebuild {stem}"
            )
        files = entry.files
        existing = cache._source_outputs(stem) + cache._annotation_outputs(stem)
        expected = {cache._dest_dir(name) / name for name in files}
        transient = any(
            any(directory.glob(pattern))
            for directory, pattern in (
                (cache.STEP_DIR, f"{stem}_*.tmp"),
                (cache.SVG_DIR, f"{stem}_*_cam*.tmp"),
                (cache.ANNOTATION_DIR, f"{stem}_*_cam*.tmp"),
            )
        ) or any(
            (cache.RENDER_WORK_DIR / f"{stem}_{variant}").exists()
            for variant in VARIANTS
        )
        try:
            intact = (
                set(existing) == expected
                and not transient
                and all(
                    not path.is_symlink()
                    and path.is_file()
                    and cache._sha256_file(path) == files[path.name]
                    for path in existing
                )
            )
        except OSError:
            intact = False
        if not intact:
            for path in existing:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
            cache.clear_outputs(stem)
            cache._restore(
                (cache._entry(stem) / name, sha) for name, sha in files.items()
            )


@locked_main
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(add_help=False)
    buildlog.add_arguments(parser)
    args, other = parser.parse_known_args(argv)
    eligible = not other
    started = time.monotonic()
    hit = _cached_plan() if eligible else None
    if hit is None:
        # Bind the checked views to the sources present before CAD imports/run.
        # An edit during the ordinary build must not seed a receipt for other code.
        source_keys = (
            {stem: cache.source_key(stem) for stem in list_procedures()}
            if eligible
            else {}
        )
        from hardware.assembly import build_procedures

        rc = build_procedures.main(argv)
        if rc == 0 and eligible:
            _record_build(
                {
                    stem: {
                        svg_path_for(stem, variant == "exploded", index=index).name
                        for variant in VARIANTS
                        for index in build_procedures._view_indices(
                            stem, variant == "exploded"
                        )
                    }
                    for stem in source_keys
                },
                source_keys,
            )
        return rc

    steps, annotation_key, plan = hit
    with (
        buildlog.command(
            f"Hardware build / {len(steps)} assemblies",
            args,
            name="build",
            output_dir=OUTPUT_DIR,
        ),
        buildlog.step("Reuse cached assemblies"),
    ):
        cache.prune(steps)
        _restore(plan)
        for stem, step in steps.items():
            _check_cancelled()
            if cache.source_key(stem) != step["key"]:
                raise RuntimeError(
                    f"Build inputs changed during the run; rebuild {stem}"
                )
            if not plan[stem].unchanged(stem):
                raise RuntimeError(
                    f"Cached manifests changed during the run; rebuild {stem}"
                )
        if cache.annotation_key("") != annotation_key:
            raise RuntimeError("Annotation inputs changed during the run; rebuild")
        if set(list_procedures()) != set(steps):
            raise RuntimeError("Procedures changed during the run; rebuild")
        total = len(steps)
        buildlog.event(
            "build_summary",
            f"{total}/{total} assemblies have complete cached outputs",
            status="success",
            total=total,
            complete=total,
            cached=total,
            reannotated=0,
            rebuilt=0,
            retries=0,
            failed_outputs=0,
            failed_batches=0,
            step_files=len(list(cache.STEP_DIR.glob("*.step"))),
            svg_files=len(list(cache.SVG_DIR.glob("*.svg"))),
            duration_s=round(time.monotonic() - started, 3),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
