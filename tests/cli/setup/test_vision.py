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
    """Make `uv run` write convert/<onnx> and return 0. Returns the spy."""

    def _fake_run(cmd, cwd, **_kw):
        (Path(cwd) / vision_mod._ONNX_NAME).write_bytes(onnx_bytes)
        return subprocess.CompletedProcess(cmd, 0)

    return mocker.patch.object(vision_mod.subprocess, "run", side_effect=_fake_run)


def _stub_prebuilt(mocker, onnx_bytes: bytes) -> None:
    """Make the prebuilt fetch return a zip containing model.onnx=onnx_bytes,
    and pin the expected sha256 to match it."""
    import hashlib
    import zipfile

    def _fake_urlretrieve(_url, dest):
        with zipfile.ZipFile(dest, "w") as z:
            z.writestr(vision_mod._ONNX_NAME, onnx_bytes)

    mocker.patch.object(
        vision_mod.urllib.request, "urlretrieve", side_effect=_fake_urlretrieve
    )
    mocker.patch.object(
        vision_mod, "_PREBUILT_ONNX_SHA256", hashlib.sha256(onnx_bytes).hexdigest()
    )


def test_vision_prebuilt_installs_without_uv(tmp_path: Path, mocker) -> None:
    # The default path fetches the prebuilt ONNX — no uv, no conversion.
    onnx = tmp_path / "models" / "omniparser_icon_detect" / "model.onnx"
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=onnx)
    _stub_prebuilt(mocker, onnx_bytes=b"PREBUILT-ONNX")
    uv_run = mocker.patch.object(vision_mod.subprocess, "run")

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert onnx.read_bytes() == b"PREBUILT-ONNX"
    assert "prebuilt" in result.output
    uv_run.assert_not_called()


def test_vision_prebuilt_checksum_mismatch_falls_back_to_build(
    tmp_path: Path, mocker,
) -> None:
    onnx = tmp_path / "models" / "omniparser_icon_detect" / "model.onnx"
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=onnx)
    mocker.patch.object(vision_mod.shutil, "which", return_value="/usr/bin/uv")

    # Prebuilt zip downloads fine but its onnx hash won't match the pinned one.
    def _fake_urlretrieve(_url, dest):
        import zipfile
        with zipfile.ZipFile(dest, "w") as z:
            z.writestr(vision_mod._ONNX_NAME, b"TAMPERED")

    mocker.patch.object(
        vision_mod.urllib.request, "urlretrieve", side_effect=_fake_urlretrieve
    )
    _stub_uv_run(mocker)

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert "checksum mismatch" in result.output
    assert "converting from source" in result.output
    assert onnx.read_bytes() == b"ONNX"  # came from the convert fallback


def test_vision_already_present_no_op(tmp_path: Path, mocker) -> None:
    onnx = tmp_path / "model.onnx"
    onnx.write_bytes(b"x")
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=onnx)

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "Already present" in result.output


def test_vision_build_aborts_when_uv_missing(tmp_path: Path, mocker) -> None:
    # --build forces the from-source path, which needs uv.
    onnx = tmp_path / "missing.onnx"
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=onnx)
    mocker.patch.object(vision_mod.shutil, "which", return_value=None)

    result = runner.invoke(app, ["--build"])

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

    result = runner.invoke(app, ["--build"])

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
    assert vision_mod._SCRIPT_NAME in cmd
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

    result = runner.invoke(app, ["--force", "--build"])

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
    (convert_dir / vision_mod._PT_NAME).write_bytes(b"stale PT")

    download_spy = mocker.patch.object(
        vision_mod.urllib.request, "urlretrieve",
        side_effect=lambda _url, dest: Path(dest).write_bytes(b"FRESH PT"),
    )
    _stub_uv_run(mocker)

    result = runner.invoke(app, ["--force", "--build"])

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
    (convert_dir / vision_mod._PT_NAME).write_bytes(b"existing PT")

    download_spy = mocker.patch.object(
        vision_mod.urllib.request, "urlretrieve",
    )
    _stub_uv_run(mocker)

    result = runner.invoke(app, ["--build"])

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

    result = runner.invoke(app, ["--build"])

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

    result = runner.invoke(app, ["--build"])

    assert result.exit_code == 1
    assert "not found" in result.output
    assert (onnx.parent / "convert").exists()
