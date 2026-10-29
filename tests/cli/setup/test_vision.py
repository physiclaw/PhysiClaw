"""Vision installation through the public CLI, with HTTP and uv isolated.

Accepted mutation survivors affect presentation/progress text, temporary names,
rounded size diagnostics, equivalent hash buffer sizes/parent creation, and the
production checksum replaced by the fixture's pin. Publication, fallback,
cleanup, contention, and conversion contracts are asserted independently.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import io
import struct
import subprocess
import zipfile
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from physiclaw.port.locks import exclusive_file_lock

vision_mod = importlib.import_module("physiclaw.cli.setup.vision")
runner = CliRunner()
app = typer.Typer()
app.command()(vision_mod.vision)
MODEL = b"verified prebuilt ONNX"
BUILT = b"fresh converted ONNX"
CDN_URL = "https://physiclaw.ai/downloads/local_vision_model.zip.b64.{i:02d}"
RELEASE_URL = "https://github.com/physiclaw/PhysiClaw/releases/download/local-vision-model/local_vision_model.zip"
WEIGHTS_URL = (
    "https://huggingface.co/microsoft/OmniParser-v2.0/resolve/main/icon_detect/model.pt"
)


class BytesResponse(io.BytesIO):
    def __init__(self, data: bytes, length: int | None = None):
        super().__init__(data)
        self.length = len(data) if length is None else length

    def getheader(self, name, default=None):
        return str(self.length) if name == "Content-Length" else default


class InterruptedResponse(BytesResponse):
    def __init__(self, error):
        super().__init__(b"partial data", length=30)
        self.error = error

    def read(self, size=-1):
        data = super().read(size)
        if not data:
            raise self.error
        return data


def archive(payload=MODEL, name="model.onnx", compression=zipfile.ZIP_STORED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as zipped:
        zipped.writestr(name, payload)
    return buffer.getvalue()


def split_parts(data):
    encoded = base64.b64encode(data)
    width = len(encoded) // 4
    return [
        encoded[:width],
        encoded[width : 2 * width],
        encoded[2 * width : 3 * width],
        encoded[3 * width :],
    ]


def unusable_archive(kind):
    if kind == "not-zip":
        return b"not an archive"
    if kind == "missing-model":
        return archive(name="different.onnx")
    if kind == "checksum":
        return archive(b"tampered")
    if kind == "deflate":
        data = bytearray(archive(compression=zipfile.ZIP_DEFLATED))
        data[40] = 255
        return bytes(data)
    data = bytearray(archive())
    central = data.index(b"PK\x01\x02")
    if kind == "encrypted":
        struct.pack_into("<H", data, 6, 1)
        struct.pack_into("<H", data, central + 8, 1)
    elif kind == "compression":
        struct.pack_into("<H", data, 8, 99)
        struct.pack_into("<H", data, central + 10, 99)
    elif kind == "crc":
        data[30 + len("model.onnx")] ^= 1
    else:
        raise AssertionError(kind)
    return bytes(data)


class InstallerIO:
    """A deterministic transport and converter; the installer owns all files."""

    def __init__(self, model):
        self.model = model
        self.requests = []
        self.responses = {}
        self.set_cdn(archive())
        self.responses[RELEASE_URL] = archive()
        self.responses[WEIGHTS_URL] = b"fresh weights"
        self.commands = []
        self.output = BUILT
        self.returncode = 0
        self.export_error = None

    @property
    def scratch(self):
        return self.model.parent / "convert"

    def set_cdn(self, data):
        self.responses.update(dict(zip(self.cdn_urls, split_parts(data))))

    @property
    def cdn_urls(self):
        return [CDN_URL.format(i=i) for i in range(4)]

    def fetch(self, url):
        self.requests.append(url)
        response = self.responses[url]
        if isinstance(response, list):
            response = response.pop(0)
        if isinstance(response, BaseException):
            raise response
        return BytesResponse(response) if isinstance(response, bytes) else response

    def convert(self, cmd, cwd):
        self.commands.append(
            (cmd, Path(cwd), (Path(cwd) / "convert.py").read_text(encoding="utf-8"))
        )
        if self.export_error is not None:
            raise self.export_error
        output = Path(cwd) / "model.onnx"
        if self.output == "directory":
            output.mkdir()
        elif self.output is not None:
            output.write_bytes(self.output)
        return subprocess.CompletedProcess(cmd, self.returncode)

    def cache_weights(self, data=b"cached weights"):
        self.scratch.mkdir(parents=True)
        (self.scratch / "model.pt").write_bytes(data)


@pytest.fixture
def installer(tmp_path, mocker):
    io = InstallerIO(tmp_path / "models" / "icons" / "model.onnx")
    mocker.patch.object(vision_mod.paths, "omniparser_onnx", return_value=io.model)
    mocker.patch.object(vision_mod, "http_get", side_effect=io.fetch)
    mocker.patch.object(
        vision_mod, "_PREBUILT_ONNX_SHA256", hashlib.sha256(MODEL).hexdigest()
    )
    mocker.patch.object(
        vision_mod.shutil, "which", side_effect={"uv": "/usr/bin/uv"}.get
    )
    mocker.patch.object(vision_mod.subprocess, "run", side_effect=io.convert)
    return io


def test_vision_cdn_installs_verified_model_without_uv(installer, mocker):
    mocker.patch.object(vision_mod.shutil, "which", return_value=None)

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert installer.model.read_bytes() == MODEL
    assert installer.requests == installer.cdn_urls
    assert installer.commands == []
    assert "vision model ready (prebuilt)" in result.output
    assert list(installer.model.parent.glob(".vision-*")) == []


@pytest.mark.parametrize(
    "kind",
    [
        "not-zip",
        "missing-model",
        "checksum",
        "encrypted",
        "compression",
        "crc",
        "deflate",
    ],
)
def test_regression_vision_invalid_cdn_tries_release(installer, kind, mocker):
    installer.set_cdn(unusable_archive(kind))
    mocker.patch.object(vision_mod.shutil, "which", return_value=None)

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert installer.model.read_bytes() == MODEL
    assert installer.requests == installer.cdn_urls + [vision_mod._PREBUILT_ZIP_URL]
    assert "CDN parts unavailable" in result.output
    assert "converting from source" not in result.output
    assert list(installer.model.parent.glob(".vision-*")) == []


@pytest.mark.parametrize(
    "bad_response", [OSError("offline"), BytesResponse(b"cut", length=100)]
)
def test_vision_retries_part_without_reusing_partial_bytes(installer, bad_response):
    part = installer.cdn_urls[1]
    installer.responses[part] = [bad_response, installer.responses[part]]

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert installer.model.read_bytes() == MODEL
    assert installer.requests == installer.cdn_urls[:2] + installer.cdn_urls[1:]


def test_vision_repeated_cdn_failure_uses_release(installer):
    installer.responses[installer.cdn_urls[0]] = OSError("offline")

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert installer.model.read_bytes() == MODEL
    assert installer.requests == [installer.cdn_urls[0]] * 2 + [
        vision_mod._PREBUILT_ZIP_URL
    ]


def test_vision_invalid_base64_uses_release(installer):
    installer.responses.update(dict.fromkeys(installer.cdn_urls, b"abcd"))
    installer.responses[installer.cdn_urls[0]] = b"abcde"

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert installer.model.read_bytes() == MODEL
    assert installer.requests[-1] == vision_mod._PREBUILT_ZIP_URL


@pytest.mark.parametrize(
    "failure", [OSError("offline"), b"invalid archive", archive(b"wrong hash")]
)
def test_vision_all_prebuilt_mirrors_fail_then_converts(installer, failure):
    installer.responses[installer.cdn_urls[0]] = OSError("CDN offline")
    installer.responses[vision_mod._PREBUILT_ZIP_URL] = failure

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert installer.model.read_bytes() == BUILT
    assert installer.requests[-2:] == [vision_mod._PREBUILT_ZIP_URL, vision_mod._PT_URL]
    assert "release unavailable" in result.output
    assert "converting from source" in result.output
    assert not installer.scratch.exists()


def test_vision_verifies_all_chunks_of_large_model(installer, mocker):
    payload = b"x" * (2**20 + 1)
    installer.set_cdn(archive(payload))
    mocker.patch.object(
        vision_mod, "_PREBUILT_ONNX_SHA256", hashlib.sha256(payload).hexdigest()
    )

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert installer.model.read_bytes() == payload
    assert "1.0 MB at" in result.output


@pytest.mark.parametrize("args", [[], ["--build"]])
@pytest.mark.parametrize("existing", [b"x", b"installed"])
def test_vision_existing_nonempty_model_is_unchanged(installer, args, existing):
    installer.model.parent.mkdir(parents=True)
    installer.model.write_bytes(existing)

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert "Already present" in result.output
    assert installer.model.read_bytes() == existing
    assert installer.requests == []
    assert installer.commands == []


@pytest.mark.parametrize("args", [[], ["--force"]])
def test_regression_vision_directory_is_not_an_installed_model(installer, args):
    installer.model.mkdir(parents=True)

    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert "not a regular file" in result.stderr
    assert isinstance(result.exception, SystemExit)
    assert installer.model.is_dir()
    assert installer.requests == []


def test_regression_vision_empty_destination_is_reinstalled(installer):
    installer.model.parent.mkdir(parents=True)
    installer.model.touch()

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert installer.model.read_bytes() == MODEL


@pytest.mark.parametrize("args", [[], ["--force", "--build"]])
def test_regression_vision_serializes_installers(installer, args):
    installer.cache_weights()

    with exclusive_file_lock(installer.model.parent / ".install.lock"):
        result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert "already running" in result.stderr
    assert isinstance(result.exception, SystemExit)
    assert not installer.model.exists()
    assert (installer.scratch / "model.pt").read_bytes() == b"cached weights"
    assert installer.requests == []
    assert installer.commands == []


def test_vision_build_requires_uv_without_downloading(installer, mocker):
    mocker.patch.object(vision_mod.shutil, "which", return_value=None)

    result = runner.invoke(app, ["--build"])

    assert result.exit_code == 1
    assert "`uv` is required" in result.output
    assert "physiclaw setup local-vision-model" in result.output
    assert isinstance(result.exception, SystemExit)
    assert installer.requests == []


def test_vision_build_downloads_exports_and_cleans_scratch(installer):
    result = runner.invoke(app, ["--build"])

    assert result.exit_code == 0, result.output
    assert installer.model.read_bytes() == BUILT
    assert installer.requests == [vision_mod._PT_URL]
    assert installer.commands == [
        (
            [
                "uv",
                "run",
                "--python",
                "3.12",
                "--no-project",
                "--with",
                "ultralytics>=8.4",
                "--with",
                "onnx>=1.18",
                "--with",
                "onnxslim>=0.1.71",
                "python",
                "convert.py",
            ],
            installer.scratch,
            'from ultralytics import YOLO\nYOLO("model.pt").export(format="onnx", imgsz=1280)\n',
        )
    ]
    assert not installer.scratch.exists()
    assert "vision model ready" in result.output


@pytest.mark.parametrize(
    "args,expected", [(["--force"], MODEL), (["--force", "--build"], BUILT)]
)
def test_vision_force_replaces_existing_model(installer, args, expected):
    installer.model.parent.mkdir(parents=True)
    installer.model.write_bytes(b"old")

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert installer.model.read_bytes() == expected


def test_vision_force_build_refreshes_cached_weights(installer):
    installer.cache_weights(b"stale weights")

    result = runner.invoke(app, ["--force", "--build"])

    assert result.exit_code == 0, result.output
    assert installer.requests == [vision_mod._PT_URL]
    assert installer.model.read_bytes() == BUILT


def test_vision_build_reuses_complete_cached_weights(installer):
    installer.cache_weights()

    result = runner.invoke(app, ["--build"])

    assert result.exit_code == 0, result.output
    assert installer.requests == []
    assert installer.model.read_bytes() == BUILT


def test_vision_build_refetches_empty_cached_weights(installer):
    installer.cache_weights(b"")

    result = runner.invoke(app, ["--build"])

    assert result.exit_code == 0, result.output
    assert installer.requests == [vision_mod._PT_URL]
    assert installer.model.read_bytes() == BUILT


@pytest.mark.parametrize("output", [None, b"", "directory"])
def test_vision_build_requires_nonempty_file_output(installer, output):
    installer.output = output

    result = runner.invoke(app, ["--build"])

    assert result.exit_code == 1
    assert "Conversion finished" in result.output
    assert "Scratch dir kept" in result.output
    assert isinstance(result.exception, SystemExit)
    assert not installer.model.exists()
    assert (installer.scratch / "model.pt").read_bytes() == b"fresh weights"


def test_regression_vision_does_not_publish_stale_export(installer):
    installer.cache_weights()
    (installer.scratch / "model.onnx").write_bytes(b"stale output")
    installer.output = None

    result = runner.invoke(app, ["--build"])

    assert result.exit_code == 1
    assert "Conversion finished" in result.output
    assert not installer.model.exists()
    assert not (installer.scratch / "model.onnx").exists()
    assert (installer.scratch / "model.pt").read_bytes() == b"cached weights"


@pytest.mark.parametrize(
    "returncode,error,message",
    [
        (2, None, "uv exit 2"),
        (0, FileNotFoundError("missing uv"), "Couldn't start conversion"),
    ],
)
def test_vision_failed_conversion_keeps_scratch_and_old_model(
    installer, returncode, error, message
):
    installer.model.parent.mkdir(parents=True)
    installer.model.write_bytes(b"old model")
    installer.returncode, installer.export_error = returncode, error

    result = runner.invoke(app, ["--force", "--build"])

    assert result.exit_code == 1
    assert message in result.output
    assert "Scratch dir kept" in result.output
    assert isinstance(result.exception, SystemExit)
    assert installer.model.read_bytes() == b"old model"
    assert (installer.scratch / "model.pt").read_bytes() == b"fresh weights"


@pytest.mark.parametrize(
    "failure,exit_code", [(OSError("offline"), 1), (KeyboardInterrupt(), 130)]
)
def test_vision_interrupted_weights_are_removed_and_retried(
    installer, failure, exit_code
):
    installer.responses[vision_mod._PT_URL] = [
        InterruptedResponse(failure),
        b"retry weights",
    ]

    failed = runner.invoke(app, ["--build"])
    succeeded = runner.invoke(app, ["--build"])

    assert failed.exit_code == exit_code
    assert succeeded.exit_code == 0, succeeded.output
    assert installer.requests == [vision_mod._PT_URL, vision_mod._PT_URL]
    assert installer.model.read_bytes() == BUILT
    assert not installer.scratch.exists()


def test_vision_failed_weights_download_explains_recovery(installer):
    installer.responses[vision_mod._PT_URL] = OSError("blocked host")

    result = runner.invoke(app, ["--build"])

    assert result.exit_code == 1
    assert "Couldn't download" in result.output
    assert "blocked host" in result.output
    assert vision_mod._PT_URL in result.output
    assert "physiclaw setup local-vision-model" in result.output
    assert isinstance(result.exception, SystemExit)
    assert list(installer.scratch.iterdir()) == []


@pytest.mark.parametrize("failure", [OSError("offline"), KeyboardInterrupt()])
def test_vision_failed_weights_download_leaves_no_partial_cache(installer, failure):
    installer.responses[vision_mod._PT_URL] = InterruptedResponse(failure)

    result = runner.invoke(app, ["--build"])

    assert result.exit_code != 0
    assert not installer.model.exists()
    assert list(installer.scratch.iterdir()) == []


@pytest.mark.parametrize("args", [["--force"], ["--force", "--build"]])
def test_regression_vision_failed_publish_preserves_old_model(installer, mocker, args):
    installer.model.parent.mkdir(parents=True)
    installer.model.write_bytes(b"old model")
    original_replace = Path.replace
    publication = []

    def refuse_publish(source, target):
        if target == installer.model:
            publication.append((source, source.read_bytes(), target.read_bytes()))
            raise OSError("destination busy")
        return original_replace(source, target)

    mocker.patch.object(Path, "replace", refuse_publish)

    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert "destination busy" in result.output
    assert isinstance(result.exception, SystemExit)
    assert installer.model.read_bytes() == b"old model"
    assert len(publication) == 1
    assert publication[0][0].parent.parent == installer.model.parent
    assert publication[0][1] in (MODEL, BUILT)
    assert publication[0][2] == b"old model"
    assert list(installer.model.parent.glob(".vision-*")) == []


def test_vision_interrupted_cdn_cleans_staging_and_releases_lock(installer):
    installer.responses[installer.cdn_urls[0]] = [
        KeyboardInterrupt(),
        installer.responses[installer.cdn_urls[0]],
    ]

    failed = runner.invoke(app, [])
    succeeded = runner.invoke(app, [])

    assert failed.exit_code == 130
    assert succeeded.exit_code == 0, succeeded.output
    assert installer.model.read_bytes() == MODEL
    assert list(installer.model.parent.glob(".vision-*")) == []
