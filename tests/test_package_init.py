"""The public package stays cheap to import and exposes the core on demand."""

import importlib.metadata
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

import physiclaw


def _load_package():
    spec = importlib.util.spec_from_file_location(
        "_physiclaw_package_probe", physiclaw.__file__
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_package_version_matches_installed_distribution():
    expected = importlib.metadata.version("physiclaw")

    package = _load_package()

    assert package.__version__ == expected


def test_package_version_without_distribution_uses_local_fallback(mocker):
    mocker.patch(
        "importlib.metadata.version",
        side_effect=importlib.metadata.PackageNotFoundError("physiclaw"),
    )

    package = _load_package()

    assert package.__version__ == "0.0.0+local"


def test_package_version_preserves_unexpected_metadata_failure(mocker):
    mocker.patch(
        "importlib.metadata.version", side_effect=RuntimeError("broken metadata")
    )

    with pytest.raises(RuntimeError, match="broken metadata"):
        _load_package()


def test_package_import_works_without_loading_hardware_stack():
    script = Path(__file__).parent / "data/package/lazy_import.py"
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(physiclaw.__file__).resolve().parents[1]),
    }

    result = subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == physiclaw.__version__


def test_package_exposes_the_core_class_on_demand():
    from physiclaw.core.orchestration import PhysiClaw as Core

    package = _load_package()

    exported = package.PhysiClaw

    assert exported is Core
    assert package.__all__ == ["PhysiClaw", "__version__"]


@pytest.mark.parametrize("name", ["missing", "physiclaw", "_private", "不存在"])
def test_package_unknown_attribute_raises_attribute_error(name):
    package = _load_package()

    with pytest.raises(AttributeError) as exc:
        getattr(package, name)

    assert str(exc.value) == f"module 'physiclaw' has no attribute {name!r}"
