"""The source hashed by the CAD cache must also be the source Python executes."""

import os
import py_compile
import subprocess
import sys


def stale_bytecode(path, original, edited):
    path.write_text(original, encoding="utf-8")
    py_compile.compile(
        str(path),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    )
    before = path.stat()
    path.write_text(edited, encoding="utf-8")
    assert path.stat().st_size == before.st_size
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def run(root, *args):
    env = {**os.environ, "PYTHONPATH": str(root)}
    result = subprocess.run(
        [sys.executable, *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def test_namespace_module_uses_current_travel_despite_valid_timestamp_pyc(source_tree):
    path = source_tree / "hardware/assembly/travel_ranges.py"
    original = path.read_text(encoding="utf-8")
    stale_bytecode(path, original, original.replace("X_TRAVEL = 110", "X_TRAVEL = 120"))
    result = run(
        source_tree,
        "-c",
        "from hardware.documents.dimensions import dimension_values; "
        "print(dimension_values()['X_RAIL_LENGTH'])",
    )
    assert result == "160"


def test_package_initializer_uses_current_source_despite_valid_timestamp_pyc(
    source_tree,
):
    path = source_tree / "hardware/port/__init__.py"
    original = path.read_text(encoding="utf-8") + "\nBYTECODE_PROBE = 1\n"
    stale_bytecode(
        path, original, original.replace("BYTECODE_PROBE = 1", "BYTECODE_PROBE = 2")
    )
    assert (
        run(
            source_tree,
            "-c",
            "from hardware.port import BYTECODE_PROBE; print(BYTECODE_PROBE)",
        )
        == "2"
    )


def test_direct_module_entry_point_uses_current_source(source_tree):
    path = source_tree / "hardware/_bytecode_probe.py"
    stale_bytecode(path, "print(1)\n", "print(2)\n")
    assert run(source_tree, "-m", "hardware._bytecode_probe") == "2"


def test_bootstrap_bytecode_is_content_checked_on_subsequent_starts(source_tree):
    path = source_tree / "hardware/__init__.py"
    original = path.read_text(encoding="utf-8") + "\nBYTECODE_PROBE = 1\n"
    path.write_text(original, encoding="utf-8")
    assert (
        run(source_tree, "-c", "import hardware; print(hardware.BYTECODE_PROBE)") == "1"
    )
    before = path.stat()
    path.write_text(
        original.replace("BYTECODE_PROBE = 1", "BYTECODE_PROBE = 2"), encoding="utf-8"
    )
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert (
        run(source_tree, "-c", "import hardware; print(hardware.BYTECODE_PROBE)") == "2"
    )


def test_source_policy_leaves_non_hardware_imports_alone(source_tree):
    path = source_tree / "ordinary_module.py"
    stale_bytecode(path, "VALUE = 1\n", "VALUE = 2\n")
    assert (
        run(source_tree, "-c", "import ordinary_module; print(ordinary_module.VALUE)")
        == "1"
    )
