"""Status snapshot tests through the public command.

Mutation review accepts display prose/decoration variants; readiness colors,
metadata errors, file types, reported sizes and continuation are asserted.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from symlink_helpers import symlink_to
from typer.testing import CliRunner

from physiclaw.cli import app

status_mod = importlib.import_module("physiclaw.cli.status")
status = status_mod.status


def _bundle(**overrides) -> dict:
    """A complete v1 bundle shaped like Calibration.to_dict() writes it —
    `complete` is a @property there, so no such key exists on disk."""
    base = {
        "version": 1,
        "viewport_shift": {
            "offset_x": 0.0,
            "offset_y": 120.0,
            "dpr": 3.0,
            "screenshot_width": 1170,
            "screenshot_height": 2532,
        },
        "cam_rotation": 2,
        "pct_to_grbl": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        "pct_to_cam": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        "cam_size": [1920, 1080],
        "cam_index": 0,
        "screen_dimension": {
            "width": 390,
            "height": 844,
            "viewport_width": 390,
            "viewport_height": 700,
        },
    }
    base.update(overrides)
    return base


def test_status_reports_vision_model_ok(
    tmp_path: Path,
    mocker,
    capsys: pytest.CaptureFixture,
) -> None:
    model = tmp_path / "model.onnx"
    model.write_bytes(b"x" * 1024 * 1024)  # 1 MB
    mocker.patch.object(status_mod.paths, "omniparser_onnx", return_value=model)
    mocker.patch.object(status_mod.paths, "load_calibration_bundle", return_value=None)
    mocker.patch.object(status_mod.paths, "jobs_file", return_value=tmp_path / "no.md")

    status()
    out = capsys.readouterr().out

    assert "vision model" in out
    assert "ok" in out
    assert "1 MB" in out


def test_status_reports_vision_model_missing(
    tmp_path: Path,
    mocker,
    capsys: pytest.CaptureFixture,
) -> None:
    mocker.patch.object(
        status_mod.paths,
        "omniparser_onnx",
        return_value=tmp_path / "missing.onnx",
    )
    mocker.patch.object(status_mod.paths, "load_calibration_bundle", return_value=None)
    mocker.patch.object(status_mod.paths, "jobs_file", return_value=tmp_path / "no.md")

    status()
    out = capsys.readouterr().out

    assert "vision model" in out
    assert "missing" in out


def test_status_reports_calibration_complete(
    tmp_path: Path,
    mocker,
    capsys: pytest.CaptureFixture,
) -> None:
    mocker.patch.object(
        status_mod.paths,
        "omniparser_onnx",
        return_value=tmp_path / "missing.onnx",
    )
    mocker.patch.object(
        status_mod.paths,
        "load_calibration_bundle",
        return_value=_bundle(),
    )
    mocker.patch.object(status_mod.paths, "jobs_file", return_value=tmp_path / "no.md")

    status()
    out = capsys.readouterr().out

    assert "calibration" in out
    assert "complete" in out


@pytest.mark.parametrize(
    "overrides",
    [
        {"pct_to_cam": None},
        {"screen_dimension": {}},
        {"screen_dimension": {"width": 390, "height": 844}},
    ],
)
def test_status_reports_calibration_partial(
    tmp_path: Path,
    mocker,
    capsys: pytest.CaptureFixture,
    overrides: dict,
) -> None:
    mocker.patch.object(
        status_mod.paths,
        "omniparser_onnx",
        return_value=tmp_path / "missing.onnx",
    )
    mocker.patch.object(
        status_mod.paths,
        "load_calibration_bundle",
        return_value=_bundle(**overrides),
    )
    mocker.patch.object(status_mod.paths, "jobs_file", return_value=tmp_path / "no.md")

    status()
    out = capsys.readouterr().out

    assert "partial" in out


def test_status_reports_unreadable_bundle_as_partial(
    tmp_path: Path,
    mocker,
    capsys: pytest.CaptureFixture,
) -> None:
    # from_dict raises on a schema-version mismatch — status degrades to
    # "partial" rather than crashing (doctor carries the deep diagnosis).
    mocker.patch.object(
        status_mod.paths,
        "omniparser_onnx",
        return_value=tmp_path / "missing.onnx",
    )
    mocker.patch.object(
        status_mod.paths,
        "load_calibration_bundle",
        return_value=_bundle(version=99),
    )
    mocker.patch.object(status_mod.paths, "jobs_file", return_value=tmp_path / "no.md")

    status()
    out = capsys.readouterr().out

    assert "partial" in out


def test_status_reports_calibration_missing(
    tmp_path: Path,
    mocker,
    capsys: pytest.CaptureFixture,
) -> None:
    mocker.patch.object(
        status_mod.paths,
        "omniparser_onnx",
        return_value=tmp_path / "missing.onnx",
    )
    mocker.patch.object(status_mod.paths, "load_calibration_bundle", return_value=None)
    mocker.patch.object(status_mod.paths, "jobs_file", return_value=tmp_path / "no.md")

    status()
    out = capsys.readouterr().out

    assert "calibration" in out
    assert "missing" in out


def test_status_reports_jobs_file_present(
    tmp_path: Path,
    mocker,
    capsys: pytest.CaptureFixture,
) -> None:
    jobs = tmp_path / "jobs.md"
    jobs.write_text("")
    mocker.patch.object(
        status_mod.paths,
        "omniparser_onnx",
        return_value=tmp_path / "missing.onnx",
    )
    mocker.patch.object(status_mod.paths, "load_calibration_bundle", return_value=None)
    mocker.patch.object(status_mod.paths, "jobs_file", return_value=jobs)

    status()
    out = capsys.readouterr().out

    assert "jobs file" in out
    assert str(jobs) in out


def test_status_reports_jobs_file_missing(
    tmp_path: Path,
    mocker,
    capsys: pytest.CaptureFixture,
) -> None:
    jobs = tmp_path / "jobs.md"
    mocker.patch.object(
        status_mod.paths,
        "omniparser_onnx",
        return_value=tmp_path / "missing.onnx",
    )
    mocker.patch.object(status_mod.paths, "load_calibration_bundle", return_value=None)
    mocker.patch.object(status_mod.paths, "jobs_file", return_value=jobs)

    status()
    out = capsys.readouterr().out

    assert "none yet" in out


def test_status_shows_doctor_hint_on_tty(
    tmp_path: Path,
    mocker,
    capsys: pytest.CaptureFixture,
) -> None:
    mocker.patch.object(
        status_mod.paths,
        "omniparser_onnx",
        return_value=tmp_path / "missing.onnx",
    )
    mocker.patch.object(status_mod.paths, "load_calibration_bundle", return_value=None)
    mocker.patch.object(status_mod.paths, "jobs_file", return_value=tmp_path / "no.md")
    mocker.patch.object(status_mod.sys.stdout, "isatty", return_value=True)

    status()
    out = capsys.readouterr().out

    assert "physiclaw doctor" in out


def test_status_suppresses_doctor_hint_on_pipe(
    tmp_path: Path,
    mocker,
    capsys: pytest.CaptureFixture,
) -> None:
    mocker.patch.object(
        status_mod.paths,
        "omniparser_onnx",
        return_value=tmp_path / "missing.onnx",
    )
    mocker.patch.object(status_mod.paths, "load_calibration_bundle", return_value=None)
    mocker.patch.object(status_mod.paths, "jobs_file", return_value=tmp_path / "no.md")
    mocker.patch.object(status_mod.sys.stdout, "isatty", return_value=False)

    status()
    out = capsys.readouterr().out

    assert "physiclaw doctor" not in out


@pytest.mark.parametrize("resource", ["omniparser_onnx", "jobs_file"])
def test_regression_status_rejects_directories(resource, capsys):
    path = getattr(status_mod.paths, resource)()
    path.mkdir(parents=True)

    status()

    output = capsys.readouterr().out
    assert "not a file" in output
    assert "vision model" in output
    assert "calibration" in output
    assert "jobs file" in output
    assert "ok  (0 MB)" not in output


@pytest.mark.parametrize(
    "resource", ["omniparser_onnx", "jobs_file", "calibration_bundle"]
)
def test_regression_status_continues_after_metadata_error(resource, mocker, capsys):
    target = getattr(status_mod.paths, resource)()
    stat = Path.stat

    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError("access denied")
        return stat(path, *args, **kwargs)

    mocker.patch.object(Path, "stat", denied)

    status()

    output = capsys.readouterr().out
    assert "access denied" in output
    assert "vision model" in output
    assert "calibration" in output
    assert "jobs file" in output


def test_regression_status_reads_model_metadata_once(mocker, capsys):
    target = status_mod.paths.omniparser_onnx()
    target.parent.mkdir(parents=True)
    target.write_bytes(b"model")
    stat = Path.stat
    reads = []

    def disappear_after_first_stat(path, *args, **kwargs):
        if path == target:
            reads.append(path)
            if len(reads) > 1:
                raise FileNotFoundError("model removed")
        return stat(path, *args, **kwargs)

    mocker.patch.object(Path, "stat", disappear_after_first_stat)

    status()

    assert "vision model  ok" in capsys.readouterr().out
    assert reads == [target]


@pytest.mark.parametrize("resource", ["omniparser_onnx", "jobs_file"])
def test_status_accepts_symlink_to_regular_file(resource, tmp_path, capsys):
    target = tmp_path / "actual-file"
    target.write_bytes(b"data")
    path = getattr(status_mod.paths, resource)()
    path.parent.mkdir(parents=True)
    symlink_to(path, target)

    status()

    output = capsys.readouterr().out
    assert "not a file" not in output
    assert str(path) in output or "vision model  ok" in output


@pytest.mark.parametrize("resource", ["omniparser_onnx", "jobs_file"])
def test_status_dangling_symlink_is_missing(resource, tmp_path, capsys):
    path = getattr(status_mod.paths, resource)()
    path.parent.mkdir(parents=True)
    symlink_to(path, tmp_path / "missing")

    status()

    output = capsys.readouterr().out
    assert "vision model  missing" in output
    assert "jobs file     none yet" in output


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(size=st.integers(min_value=0, max_value=128 * 1024 * 1024))
@example(size=3 * 1024 * 1024 // 2 + 1)
def test_status_formats_regular_model_size(size, capsys):
    model = status_mod.paths.omniparser_onnx()
    model.parent.mkdir(parents=True, exist_ok=True)
    with model.open("wb") as stream:
        stream.truncate(size)

    status()

    assert (
        f"vision model  ok  ({size / (1024 * 1024):.0f} MB)" in capsys.readouterr().out
    )


@pytest.mark.parametrize("contents", [b"{", b"[]", b"\xff", b'{"version":99}'])
def test_status_reports_malformed_calibration_without_aborting(contents, capsys):
    bundle = status_mod.paths.calibration_bundle()
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(contents)

    status()

    output = capsys.readouterr().out
    assert "calibration" in output
    assert "complete" not in output
    assert "jobs file" in output


@pytest.mark.parametrize("label", ["vision model", "calibration", "jobs file"])
def test_status_marks_missing_rows_yellow(label):
    result = CliRunner().invoke(app, ["status"], color=True)

    assert result.exit_code == 0
    assert f"\x1b[33m  {label:<14}\x1b[0m" in result.output


@pytest.mark.parametrize("label", ["vision model", "calibration", "jobs file"])
def test_status_marks_healthy_rows_green(label):
    model = status_mod.paths.omniparser_onnx()
    model.parent.mkdir(parents=True)
    model.write_bytes(b"model")
    jobs = status_mod.paths.jobs_file()
    jobs.parent.mkdir(parents=True)
    jobs.write_text("", encoding="utf-8")
    bundle = status_mod.paths.calibration_bundle()
    bundle.parent.mkdir(parents=True)
    bundle.write_text(json.dumps(_bundle()), encoding="utf-8")

    result = CliRunner().invoke(app, ["status"], color=True)

    assert result.exit_code == 0
    assert f"\x1b[32m  {label:<14}\x1b[0m" in result.output


@pytest.mark.parametrize("bundle", [_bundle(pct_to_cam=None), _bundle(version=99)])
def test_status_marks_partial_calibration_yellow(bundle):
    path = status_mod.paths.calibration_bundle()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = CliRunner().invoke(app, ["status"], color=True)

    assert result.exit_code == 0
    assert "\x1b[33m  calibration   \x1b[0mpartial" in result.output
