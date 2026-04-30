"""Tests for `physiclaw.cli.setup.vision` — local vision model download."""
from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import typer
from typer.testing import CliRunner

vision_mod = importlib.import_module("physiclaw.cli.setup.vision")

runner = CliRunner()
app = typer.Typer()
app.command()(vision_mod.vision)


def _stub_download(mocker, payload: bytes = b"PT") -> None:
    """Replace urllib.request.urlretrieve with one that just touches the file."""

    def _fake_urlretrieve(_url, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(payload)

    mocker.patch.object(
        vision_mod.urllib.request, "urlretrieve",
        side_effect=_fake_urlretrieve,
    )


def _stub_uv_run(mocker, *, onnx_bytes: bytes = b"ONNX") -> object:
    """Make `uv run` write convert/model.onnx and return 0. Returns the spy."""

    def _fake_run(cmd, cwd, **_kw):
        (Path(cwd) / "model.onnx").write_bytes(onnx_bytes)
        return subprocess.CompletedProcess(cmd, 0)

    return mocker.patch.object(vision_mod.subprocess, "run", side_effect=_fake_run)


def test_vision_already_present_no_op(tmp_path: Path, mocker) -> None:
    onnx = tmp_path / "model.onnx"
    onnx.write_bytes(b"x")
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=onnx)

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "Already present" in result.output


def test_vision_aborts_when_uv_missing(tmp_path: Path, mocker) -> None:
    onnx = tmp_path / "missing.onnx"
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=onnx)
    mocker.patch.object(vision_mod.shutil, "which", return_value=None)

    result = runner.invoke(app, [])

    assert result.exit_code == 1
    assert "uv" in result.output
    assert "physiclaw setup local-vision-model" in result.output


def test_vision_downloads_converts_and_cleans_up(
    tmp_path: Path, mocker,
) -> None:
    onnx = tmp_path / "models" / "omniparser_icon_detect" / "model.onnx"
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=onnx)
    mocker.patch.object(vision_mod.shutil, "which", return_value="/usr/bin/uv")

    _stub_download(mocker, payload=b"PT-WEIGHTS")
    spy = _stub_uv_run(mocker)

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert onnx.exists()
    assert onnx.read_bytes() == b"ONNX"
    assert "vision model ready" in result.output
    assert not (onnx.parent / "convert").exists()

    cmd = spy.call_args.args[0]
    assert cmd[0] == "uv"
    assert "run" in cmd
    assert "--no-project" in cmd
    assert "--python" in cmd and "3.12" in cmd
    assert "convert.py" in cmd
    with_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--with"]
    assert any(a.startswith("ultralytics") for a in with_args)
    assert any(a.startswith("onnx>=") for a in with_args)
    assert any(a.startswith("onnxslim") for a in with_args)


def test_vision_force_redownloads(tmp_path: Path, mocker) -> None:
    onnx = tmp_path / "model.onnx"
    onnx.write_bytes(b"old")
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=onnx)
    mocker.patch.object(vision_mod.shutil, "which", return_value="/usr/bin/uv")

    _stub_download(mocker)
    _stub_uv_run(mocker, onnx_bytes=b"newly converted")

    result = runner.invoke(app, ["--force"])

    assert result.exit_code == 0, result.output
    assert onnx.read_bytes() == b"newly converted"


def test_vision_force_purges_stale_scratch(tmp_path: Path, mocker) -> None:
    """--force must drop any stale convert/model.pt and re-download."""
    onnx = tmp_path / "models" / "omniparser_icon_detect" / "model.onnx"
    onnx.parent.mkdir(parents=True)
    onnx.write_bytes(b"old onnx")
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=onnx)
    mocker.patch.object(vision_mod.shutil, "which", return_value="/usr/bin/uv")

    convert_dir = onnx.parent / "convert"
    convert_dir.mkdir()
    (convert_dir / "model.pt").write_bytes(b"stale PT")

    download_spy = mocker.patch.object(
        vision_mod.urllib.request, "urlretrieve",
        side_effect=lambda _url, dest: Path(dest).write_bytes(b"FRESH PT"),
    )
    _stub_uv_run(mocker)

    result = runner.invoke(app, ["--force"])

    assert result.exit_code == 0, result.output
    download_spy.assert_called_once()


def test_vision_skips_download_when_pt_already_exists(
    tmp_path: Path, mocker,
) -> None:
    onnx = tmp_path / "models" / "omniparser_icon_detect" / "model.onnx"
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=onnx)
    mocker.patch.object(vision_mod.shutil, "which", return_value="/usr/bin/uv")

    convert_dir = onnx.parent / "convert"
    convert_dir.mkdir(parents=True)
    (convert_dir / "model.pt").write_bytes(b"existing PT")

    download_spy = mocker.patch.object(
        vision_mod.urllib.request, "urlretrieve",
    )
    _stub_uv_run(mocker)

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    download_spy.assert_not_called()


def test_vision_keeps_scratch_on_uv_failure(tmp_path: Path, mocker) -> None:
    onnx = tmp_path / "models" / "omniparser_icon_detect" / "model.onnx"
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=onnx)
    mocker.patch.object(vision_mod.shutil, "which", return_value="/usr/bin/uv")
    _stub_download(mocker)

    mocker.patch.object(
        vision_mod.subprocess, "run",
        return_value=subprocess.CompletedProcess([], returncode=2),
    )

    result = runner.invoke(app, [])

    assert result.exit_code == 1
    assert "Conversion failed" in result.output
    assert (onnx.parent / "convert").exists()
    assert not onnx.exists()


def test_vision_keeps_scratch_when_onnx_not_produced(
    tmp_path: Path, mocker,
) -> None:
    onnx = tmp_path / "models" / "omniparser_icon_detect" / "model.onnx"
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=onnx)
    mocker.patch.object(vision_mod.shutil, "which", return_value="/usr/bin/uv")
    _stub_download(mocker)

    mocker.patch.object(
        vision_mod.subprocess, "run",
        return_value=subprocess.CompletedProcess([], returncode=0),
    )

    result = runner.invoke(app, [])

    assert result.exit_code == 1
    assert "not found" in result.output
    assert (onnx.parent / "convert").exists()
