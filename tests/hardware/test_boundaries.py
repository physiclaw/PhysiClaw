"""Keep part models, assembly workflows and document builders independent."""

import ast
import subprocess
import sys
from importlib.util import resolve_name

import pytest

from hardware.scheme import REPO_ROOT

ROOT = REPO_ROOT / "hardware"


def imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # Standalone model exporters are explicit composition entry points.
    tree.body = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.If)
            and ast.unparse(node.test) == "__name__ == '__main__'"
        )
    ]
    package = ".".join(path.relative_to(REPO_ROOT).parts[:-1])
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            name = node.module or ""
            if node.level:
                name = resolve_name("." * node.level + name, package)
            modules.add(name)
            modules.update(f"{name}.{alias.name}" for alias in node.names)
    return modules


def forbidden_imports(paths, forbidden):
    return [
        f"{path.relative_to(ROOT)} -> {module}"
        for path in paths
        for module in imports(path)
        if any(
            module == prefix or module.startswith(prefix + ".") for prefix in forbidden
        )
    ]


def test_reusable_parts_do_not_import_assembly_or_documents():
    paths = [ROOT / "parts/base.py", ROOT / "parts/_fits.py"]
    paths += list((ROOT / "parts/standard").glob("*.py"))
    paths += list((ROOT / "parts/custom").glob("*.py"))
    assert not forbidden_imports(
        paths, ("hardware.assembly", "hardware.documents", "hardware.release")
    )


def test_assembly_models_do_not_import_pipeline_or_documents():
    paths = list((ROOT / "assembly/procedures").glob("*.py"))
    paths += [
        ROOT / "assembly" / name
        for name in (
            "base.py",
            "projection.py",
            "render_checkpoint.py",
            "belt_routing.py",
        )
    ]
    assert not forbidden_imports(
        paths,
        (
            "hardware.documents",
            "hardware.release",
            "hardware.assembly.cache",
            "hardware.assembly.build_procedures",
            "hardware.assembly.annotation",
        ),
    )


def test_document_families_do_not_import_each_other():
    violations = []
    families = ("manual", "sourcing", "drawing")
    for family in families:
        violations += forbidden_imports(
            (ROOT / "documents" / family).rglob("*.py"),
            tuple(
                f"hardware.documents.{other}" for other in families if other != family
            ),
        )
    assert not violations


@pytest.mark.parametrize(
    "module",
    [
        "hardware.assembly.cache",
        "hardware.assembly.discovery",
        "hardware.check",
        "hardware.documents.manual.build",
        "hardware.documents.sourcing.build",
        "hardware.documents.drawing.extrusion",
        "hardware.release",
    ],
)
def test_metadata_and_documents_import_without_cad(module):
    script = """
import importlib
import sys
class NoCad:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'build123d', 'OCP', 'shapely'}:
            raise AssertionError(f'Unexpected CAD import: {fullname}')
sys.meta_path.insert(0, NoCad())
importlib.import_module(sys.argv[1])
"""
    result = subprocess.run(
        [sys.executable, "-c", script, module],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
