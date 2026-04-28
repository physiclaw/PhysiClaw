"""Tests for `physiclaw.cli.setup.vision` — local vision model download."""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer
from typer.testing import CliRunner

vision_mod = importlib.import_module("physiclaw.cli.setup.vision")

runner = CliRunner()
app = typer.Typer()
app.command()(vision_mod.vision)


def test_vision_already_present_no_op(
    tmp_path: Path, mocker, capsys: pytest.CaptureFixture,
) -> None:
    onnx = tmp_path / "model.onnx"
    onnx.write_bytes(b"x")
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=onnx)

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "Already present" in result.output


def test_vision_aborts_when_ultralytics_missing(
    tmp_path: Path, mocker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocker.patch.object(
        vision_mod.paths, "omniparser_onnx",
        return_value=tmp_path / "missing.onnx",
    )
    real_import = __builtins__["__import__"] if isinstance(
        __builtins__, dict) else __builtins__.__import__

    def fake_import(name, *a, **kw):
        if name == "ultralytics":
            raise ImportError("not installed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", fake_import)

    result = runner.invoke(app, [])

    # typer.Abort exits with code 1.
    assert result.exit_code == 1
    assert "Conversion deps missing" in result.output


def test_vision_downloads_and_converts(tmp_path: Path, mocker) -> None:
    onnx = tmp_path / "models" / "icon" / "model.onnx"
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=onnx)
    mocker.patch.object(vision_mod.paths, "ensure_dirs")

    fake_yolo_cls = MagicMock()
    fake_yolo_instance = MagicMock()
    fake_yolo_cls.return_value = fake_yolo_instance
    fake_ultralytics = MagicMock(YOLO=fake_yolo_cls)
    mocker.patch.dict("sys.modules", {"ultralytics": fake_ultralytics})

    def _fake_urlretrieve(url, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"PT-WEIGHTS")

    mocker.patch.object(
        vision_mod.urllib.request, "urlretrieve",
        side_effect=_fake_urlretrieve,
    )

    def _fake_export(format, imgsz):
        # YOLO exports a sibling .onnx file at the .pt's location.
        out = onnx.with_suffix(".onnx")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"ONNX")

    fake_yolo_instance.export.side_effect = _fake_export

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert onnx.exists()
    assert "vision model ready" in result.output


def test_vision_force_redownloads(tmp_path: Path, mocker) -> None:
    onnx = tmp_path / "model.onnx"
    onnx.write_bytes(b"old")
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=onnx)
    mocker.patch.object(vision_mod.paths, "ensure_dirs")

    fake_yolo_cls = MagicMock()
    mocker.patch.dict("sys.modules", {"ultralytics": MagicMock(YOLO=fake_yolo_cls)})
    fake_yolo_instance = MagicMock()
    fake_yolo_cls.return_value = fake_yolo_instance

    def _fake_urlretrieve(url, dest):
        Path(dest).write_bytes(b"PT")

    mocker.patch.object(
        vision_mod.urllib.request, "urlretrieve",
        side_effect=_fake_urlretrieve,
    )

    def _fake_export(format, imgsz):
        onnx.with_suffix(".onnx").write_bytes(b"newly converted")

    fake_yolo_instance.export.side_effect = _fake_export

    result = runner.invoke(app, ["--force"])

    assert result.exit_code == 0


def test_vision_skips_download_when_pt_already_exists(
    tmp_path: Path, mocker,
) -> None:
    onnx = tmp_path / "model.onnx"
    pt = tmp_path / "model.pt"
    pt.write_bytes(b"existing PT weights")
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=onnx)
    mocker.patch.object(vision_mod.paths, "ensure_dirs")

    fake_yolo_cls = MagicMock()
    fake_yolo_instance = MagicMock()
    fake_yolo_cls.return_value = fake_yolo_instance
    mocker.patch.dict("sys.modules", {"ultralytics": MagicMock(YOLO=fake_yolo_cls)})

    download_spy = mocker.patch.object(
        vision_mod.urllib.request, "urlretrieve",
    )

    def _fake_export(format, imgsz):
        onnx.with_suffix(".onnx").write_bytes(b"ONNX")

    fake_yolo_instance.export.side_effect = _fake_export

    runner.invoke(app, [])

    download_spy.assert_not_called()
