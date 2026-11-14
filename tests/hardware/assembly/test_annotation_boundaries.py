"""Metadata, drawing and CAD keep separate dependency boundaries."""

import subprocess
import sys

import pytest

from hardware.assembly import cache
from hardware.scheme import REPO_ROOT
from tests.timing_helpers import PROCESS_TIMEOUT


@pytest.mark.parametrize(
    "module,allowed",
    [
        ("geometry.py", set()),
        ("svg.py", {"specs.py"}),
        ("features.py", {"geometry.py", "specs.py"}),
        (
            "callouts.py",
            {"geometry.py", "svg.py", "specs.py"},
        ),
        (
            "generator.py",
            {"geometry.py", "features.py", "callouts.py", "svg.py", "specs.py"},
        ),
    ],
)
def test_annotation_modules_depend_only_on_lower_layers(module, allowed):
    assembly = REPO_ROOT / "hardware/assembly"
    annotation = assembly / "annotation"
    layers = {
        assembly / "build_procedures.py",
        assembly / "cache.py",
    } | set(annotation.glob("*.py"))
    dependencies = {
        cache._module_to_path(name) for name in cache._imports(annotation / module)
    }
    assert dependencies & layers <= {annotation / name for name in allowed}


@pytest.mark.parametrize("module", ["__init__.py", "specs.py"])
def test_metadata_imports_only_standard_library(module):
    imports = cache._imports(REPO_ROOT / "hardware/assembly/annotation" / module)
    assert {name.split(".")[0] for name in imports} <= sys.stdlib_module_names


def test_geometry_is_independent_of_parts_and_procedures():
    imports = cache._imports(REPO_ROOT / "hardware/assembly/annotation/geometry.py")
    forbidden = ("hardware.parts", "hardware.assembly.procedures")
    assert not any(
        name == prefix or name.startswith(prefix + ".")
        for name in imports
        for prefix in forbidden
    )


def test_other_hardware_modules_use_the_public_annotation_interface():
    hardware = REPO_ROOT / "hardware"
    assembly = hardware / "assembly"
    private = {
        source
        for source in (assembly / "annotation").glob("*.py")
        if source.name not in {"__init__.py", "generator.py", "specs.py"}
    }
    violations = []
    for directory, dirs, files in hardware.walk():
        if directory == hardware:
            dirs[:] = [name for name in dirs if name != "output"]
        if directory == assembly:
            dirs[:] = [name for name in dirs if name != "annotation"]
        for name in files:
            source = directory / name
            if source.suffix != ".py":
                continue
            for module in cache._imports(source):
                if cache._module_to_path(module) in private:
                    violations.append(f"{source.relative_to(hardware)} -> {module}")
    assert not violations, (
        "Use annotation.generator or annotation.specs:\n" + "\n".join(violations)
    )


def test_metadata_and_svg_work_without_loading_cad():
    script = """
import builtins
import sys

original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split('.')[0] in {'build123d', 'OCP'}:
        raise AssertionError(f'Unexpected CAD dependency: {name}')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import

from hardware.assembly.annotation.specs import FIGURES
from hardware.assembly.annotation.svg import Drawing, compose, path_svg

drawing = Drawing()
drawing.line((0, 0), (1, 1))
source = '<svg viewBox="0 0 2 2"></svg>'
assert 'procedure-annotation' in compose(source, path_svg(source, drawing))
assert FIGURES
assert 'hardware.assembly.annotation.generator' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_implementation_helpers_belong_to_annotation_cache_only():
    assembly = REPO_ROOT / "hardware/assembly"
    annotation = assembly / "annotation"
    helpers = set(annotation.glob("*.py"))
    annotation_sources = cache._annotation_files()
    cad_sources = cache._source_files("frame_20_SHCS")

    assert helpers <= annotation_sources
    assert helpers.isdisjoint(cad_sources)
