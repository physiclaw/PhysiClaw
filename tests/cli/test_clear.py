"""Tests for `physiclaw.cli.clear` — deleting saved debug images.

All paths derive from `paths.HOME`, which the autouse `physiclaw_home`
fixture repoints to a per-test tmp dir — no real `~/.physiclaw` is touched.

Mutation review accepts prose/decoration changes and a one-byte size offset
in the approximate megabyte display. Directory selection, deletion failures,
confirmation and aggregate size are asserted through the public command.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import typer
from symlink_helpers import symlink_to
from typer.testing import CliRunner

from physiclaw.common import paths

clear_mod = importlib.import_module("physiclaw.cli.clear")

app = typer.Typer()
app.command()(clear_mod.clear)
runner = CliRunner()


def _seed(d: Path, n: int = 1) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"img_{i}.jpg").write_bytes(b"\xff" * 100)


def _seed_all() -> list[Path]:
    dirs = [
        paths.snapshots_dir(),
        paths.screenshots_dir(),
        paths.tool_calls_dir(),
        paths.raw_camera_dir(),
    ]
    for d in dirs:
        _seed(d)
    return dirs


# ---------- empty states ----------


def test_clear_with_no_dirs_reports_nothing_to_clear() -> None:
    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "No saved images to clear" in result.output


def test_clear_with_empty_dirs_reports_nothing_to_clear() -> None:
    paths.snapshots_dir().mkdir(parents=True)
    paths.raw_camera_dir().mkdir(parents=True)

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "No saved images to clear" in result.output


# ---------- what gets removed ----------


@pytest.mark.parametrize("flag", ["--yes", "-y"])
def test_clear_yes_removes_all_image_dirs(flag: str) -> None:
    dirs = _seed_all()

    result = runner.invoke(app, [flag])

    assert result.exit_code == 0
    assert "Cleared 4 file(s)." in result.output
    assert all(not d.exists() for d in dirs)


def test_clear_removes_only_populated_dirs() -> None:
    _seed(paths.snapshots_dir(), n=2)
    paths.screenshots_dir().mkdir(parents=True)  # present but empty

    result = runner.invoke(app, ["--yes"])

    assert result.exit_code == 0
    assert not paths.snapshots_dir().exists()
    assert paths.screenshots_dir().exists()


def test_clear_leaves_unrelated_home_dirs() -> None:
    _seed_all()
    calibration = paths.calibration_bundle()
    calibration.parent.mkdir(parents=True)
    calibration.write_text("{}")

    result = runner.invoke(app, ["--yes"])

    assert result.exit_code == 0
    assert calibration.exists()


def test_clear_lists_found_dirs_with_counts() -> None:
    _seed(paths.snapshots_dir(), n=2)

    result = runner.invoke(app, ["--yes"])

    assert "Found 2 file(s)" in result.output
    assert "MB" in result.output
    assert str(paths.snapshots_dir()) in result.output


# ---------- confirmation prompt ----------


def test_clear_confirmation_accepted_deletes() -> None:
    dirs = _seed_all()

    result = runner.invoke(app, [], input="y\n")

    assert result.exit_code == 0
    assert "Cleared 4 file(s)." in result.output
    assert all(not d.exists() for d in dirs)


def test_clear_confirmation_declined_cancels() -> None:
    dirs = _seed_all()

    result = runner.invoke(app, [], input="n\n")

    assert result.exit_code == 1
    assert "Cancelled." in result.output
    assert all(d.exists() for d in dirs)


def test_clear_confirmation_default_is_decline() -> None:
    dirs = _seed_all()

    result = runner.invoke(app, [], input="\n")

    assert result.exit_code == 1
    assert all(d.exists() for d in dirs)


def test_regression_clear_failed_removal_is_not_reported_as_success(mocker) -> None:
    directory = paths.snapshots_dir()
    _seed(directory)
    mocker.patch.object(
        clear_mod.shutil, "rmtree", side_effect=PermissionError("denied")
    )

    result = runner.invoke(app, ["--yes"])

    assert result.exit_code == 1
    assert "could not remove" in result.output
    assert str(directory) in result.output and "denied" in result.output
    assert "Cleared" not in result.output
    assert (directory / "img_0.jpg").exists()


def test_regression_clear_root_symlink_reports_refusal_without_touching_target(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    _seed(outside)
    directory = paths.snapshots_dir()
    symlink_to(directory, outside, target_is_directory=True)

    result = runner.invoke(app, ["--yes"])

    assert result.exit_code == 1
    assert "could not remove" in result.output
    assert "Cleared" not in result.output
    assert directory.is_symlink()
    assert (outside / "img_0.jpg").read_bytes() == b"\xff" * 100


def test_clear_missing_earlier_directory_does_not_hide_later_images():
    _seed(paths.raw_camera_dir())

    result = runner.invoke(app, ["--yes"])

    assert result.exit_code == 0
    assert "Cleared 1 file(s)." in result.output
    assert not paths.raw_camera_dir().exists()


def test_clear_empty_earlier_directory_does_not_hide_later_images():
    paths.snapshots_dir().mkdir(parents=True)
    _seed(paths.raw_camera_dir())

    result = runner.invoke(app, ["--yes"])

    assert result.exit_code == 0
    assert "Cleared 1 file(s)." in result.output
    assert not paths.raw_camera_dir().exists()


def _sparse_file(directory, size):
    directory.mkdir(parents=True)
    with (directory / "image.bin").open("wb") as output:
        output.truncate(size)


def test_clear_reports_total_megabytes_across_directories():
    _sparse_file(paths.snapshots_dir(), 1_000_000)
    _sparse_file(paths.raw_camera_dir(), 2_000_000)

    result = runner.invoke(app, ["--yes"])

    assert result.exit_code == 0
    assert "Found 2 file(s) (3.0 MB)" in result.output
