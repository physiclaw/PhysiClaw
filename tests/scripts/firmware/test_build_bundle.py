"""Public boundaries of the standalone firmware bundle builder."""

# Accepted mutation survivors: log/help decoration and temporary-directory prefixes
# do not affect the bundle; 60→61-second timeouts, 2→3-second retry delays, and
# 64→128-KiB chunks preserve bounded downloading. Missing Content-Range defaults
# of "" and "XXXX" both fail validation. ZIP seconds 0 and 1 encode identically
# because the DOS timestamp has two-second resolution.

import importlib.util
import io
import struct
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


@pytest.fixture
def bundle(mocker, tmp_path):
    mocker.patch.object(Path, "home", return_value=tmp_path)
    path = Path(__file__).resolve().parents[3] / "scripts/firmware/build_bundle.py"
    spec = importlib.util.spec_from_file_location("build_bundle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    mocker.patch.object(module.time, "sleep")
    return module


def _partition(ptype=1, subtype=0x82, offset=0x3D0000):
    return struct.pack(
        "<HBBII16sI", 0x50AA, ptype, subtype, offset, 0x30000, b"data", 0
    )


def _release_zip(table=None, tag="v-test"):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(f"fluidnc-{tag}-posix/wifi/bootloader.bin", b"boot")
        archive.writestr(
            f"fluidnc-{tag}-posix/wifi/partitions.bin",
            _partition() if table is None else table,
        )
        archive.writestr(f"fluidnc-{tag}-posix/common/boot_app0.bin", b"app0")
    return data.getvalue()


@pytest.fixture
def compilers(mocker):
    def convert(args):
        Path(args[args.index("-o") + 1]).write_bytes(
            b"firmware:" + Path(args[-1]).read_bytes()
        )

    elf = mocker.Mock(side_effect=convert)
    stream = mocker.MagicMock()
    stream.__enter__.return_value = stream
    fs = SimpleNamespace(
        open=mocker.Mock(return_value=stream),
        unmount=mocker.Mock(),
        context=SimpleNamespace(buffer=bytearray(b"filesystem")),
    )
    littlefs = mocker.Mock(return_value=fs)
    mocker.patch.dict(
        sys.modules,
        {
            "esptool": SimpleNamespace(main=elf),
            "littlefs": SimpleNamespace(LittleFS=littlefs),
        },
    )
    return SimpleNamespace(elf=elf, littlefs=littlefs, fs=fs, stream=stream)


class Response(io.BytesIO):
    def __init__(self, body, status=200, **headers):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Length": str(len(body)), **headers}


@pytest.mark.parametrize("prefix", [b"", _partition(ptype=0)])
def test_regression_data_partition_reads_final_complete_record(bundle, prefix):
    table = prefix + _partition()

    result = bundle.data_partition(table)

    assert result == (0x3D0000, 0x30000)


@settings(
    derandomize=True,
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    prefix=st.lists(st.sampled_from([(0, 0x82), (1, 0), (2, 0x82)]), max_size=6),
    tail=st.binary(max_size=31),
)
def test_data_partition_finds_matching_record_after_unrelated_entries(
    bundle, prefix, tail
):
    table = (
        b"".join(_partition(*kind, offset=0x10000) for kind in prefix)
        + _partition()
        + tail
    )

    result = bundle.data_partition(table)

    assert result == (0x3D0000, 0x30000)


def test_regression_fetch_missing_payload_ignores_completion_marker(
    bundle, tmp_path, mocker
):
    dest = tmp_path / "release.zip"
    marker = tmp_path / "release.zip.done"
    marker.touch()
    download = mocker.patch.object(
        bundle.urllib.request, "urlopen", return_value=Response(b"new")
    )

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"new" and marker.is_file()
    download.assert_called_once()


def test_regression_fetch_short_response_resumes_before_marking_complete(
    bundle, tmp_path, mocker
):
    dest = tmp_path / "release.zip"
    mocker.patch.object(bundle.time, "sleep")
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=[
            Response(b"abc", **{"Content-Length": "5"}),
            Response(b"de", status=206, **{"Content-Range": "bytes 3-4/5"}),
        ],
    )

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"abcde"
    assert download.call_count == 2
    assert download.call_args_list[1].args[0].get_header("Range") == "bytes=3-"
    assert (tmp_path / "release.zip.done").is_file()


def test_regression_fetch_restarts_rejected_range_and_closes_error(
    bundle, tmp_path, mocker
):
    dest = tmp_path / "release.zip"
    dest.write_bytes(b"old oversized payload")
    body = io.BytesIO(b"range unavailable")
    error = HTTPError("https://example.invalid/release.zip", 416, "range", {}, body)
    mocker.patch.object(bundle.time, "sleep")
    download = mocker.patch.object(
        bundle.urllib.request, "urlopen", side_effect=[error, Response(b"new")]
    )

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"new" and body.closed
    assert download.call_args_list[1].args[0].get_header("Range") is None
    assert (tmp_path / "release.zip.done").is_file()


def test_regression_fetch_rejects_mismatched_partial_response(bundle, tmp_path, mocker):
    dest = tmp_path / "release.zip"
    dest.write_bytes(b"prefix")
    mocker.patch.object(bundle.time, "sleep")
    mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=lambda *a, **kw: Response(
            b"bad", status=206, **{"Content-Range": "bytes 0-2/3"}
        ),
    )

    with pytest.raises(SystemExit, match="download failed"):
        bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"prefix"
    assert not (tmp_path / "release.zip.done").exists()


def test_regression_write_bundle_preserves_previous_file_on_failure(bundle, tmp_path):
    out = tmp_path / "output/bundle.zip"
    out.parent.mkdir()
    previous = b"previous complete bundle"
    out.write_bytes(previous)

    with pytest.raises(KeyError, match="partitions.bin"):
        bundle.write_bundle({"bootloader.bin": b"new"}, out)

    assert out.read_bytes() == previous
    assert list(out.parent.iterdir()) == [out]


def test_sha256_matches_known_digest(bundle):
    data = b"abc"

    result = bundle.sha256(data)

    assert result == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


@pytest.mark.parametrize(
    "table",
    [
        b"",
        b"\xaaP" * 15,
        _partition()[:-1],
        b"\xff" * 32 + _partition(),
        _partition(subtype=0),
    ],
)
def test_data_partition_missing_entry_reports_failure(bundle, table):
    with pytest.raises(SystemExit, match="no spiffs/littlefs data partition"):
        bundle.data_partition(table)


def test_fetch_complete_cache_avoids_network(bundle, tmp_path, mocker):
    dest = tmp_path / "release.zip"
    dest.write_bytes(b"cached")
    (tmp_path / "release.zip.done").touch()
    download = mocker.patch.object(bundle.urllib.request, "urlopen")

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"cached"
    download.assert_not_called()


@pytest.mark.parametrize("size", [1, 131073])
def test_fetch_counts_entire_response_and_accepts_single_byte(
    bundle, tmp_path, mocker, size
):
    body = b"x" * size
    dest = tmp_path / "release.zip"
    download = mocker.patch.object(
        bundle.urllib.request, "urlopen", return_value=Response(body)
    )

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == body
    assert (tmp_path / "release.zip.done").is_file()
    download.assert_called_once()


def test_fetch_single_byte_range_completes_partial_file(bundle, tmp_path, mocker):
    dest = tmp_path / "release.zip"
    dest.write_bytes(b"a")
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        return_value=Response(b"b", status=206, **{"Content-Range": "bytes 1-1/2"}),
    )

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"ab"
    assert (tmp_path / "release.zip.done").is_file()
    download.assert_called_once()


def test_fetch_rejected_range_without_cached_file_retries(bundle, tmp_path, mocker):
    dest = tmp_path / "release.zip"
    error = HTTPError(
        "https://example.invalid/release.zip", 416, "range", {}, io.BytesIO()
    )
    download = mocker.patch.object(
        bundle.urllib.request, "urlopen", side_effect=[error, Response(b"new")]
    )

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"new" and download.call_count == 2
    assert (tmp_path / "release.zip.done").is_file()


def test_fetch_empty_cache_is_downloaded_again(bundle, tmp_path, mocker):
    dest = tmp_path / "release.zip"
    dest.touch()
    (tmp_path / "release.zip.done").touch()
    download = mocker.patch.object(
        bundle.urllib.request, "urlopen", return_value=Response(b"new")
    )

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"new"
    download.assert_called_once()


def test_fetch_failed_redownload_clears_stale_completion_marker(
    bundle, tmp_path, mocker
):
    dest = tmp_path / "release.zip"
    marker = tmp_path / "release.zip.done"
    marker.touch()
    mocker.patch.object(bundle.time, "sleep")
    mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=lambda *a, **kw: Response(b"abc", **{"Content-Length": "5"}),
    )

    with pytest.raises(SystemExit, match="download failed"):
        bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"abc" and not marker.exists()


def test_fetch_server_ignoring_range_replaces_partial_file(bundle, tmp_path, mocker):
    dest = tmp_path / "release.zip"
    dest.write_bytes(b"old")
    download = mocker.patch.object(
        bundle.urllib.request, "urlopen", return_value=Response(b"new")
    )

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"new"
    assert download.call_args.args[0].get_header("Range") == "bytes=3-"


def test_fetch_response_without_content_length_uses_eof(bundle, tmp_path, mocker):
    response = Response(b"whole body")
    response.headers.clear()
    dest = tmp_path / "release.zip"
    mocker.patch.object(bundle.urllib.request, "urlopen", return_value=response)

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"whole body"
    assert (tmp_path / "release.zip.done").is_file()


def test_fetch_empty_response_without_length_retries_before_marking_complete(
    bundle, tmp_path, mocker
):
    empty = Response(b"")
    empty.headers.clear()
    dest = tmp_path / "release.zip"
    download = mocker.patch.object(
        bundle.urllib.request, "urlopen", side_effect=[empty, Response(b"valid")]
    )

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"valid" and download.call_count == 2
    assert (tmp_path / "release.zip.done").is_file()


def test_fetch_oversized_range_body_requires_a_valid_retry(bundle, tmp_path, mocker):
    dest = tmp_path / "release.zip"
    dest.write_bytes(b"a")
    oversized = Response(b"bc", status=206, **{"Content-Range": "bytes 1-1/3"})
    oversized.headers.pop("Content-Length")
    download = mocker.patch.object(
        bundle.urllib.request, "urlopen", side_effect=[oversized, Response(b"abc")]
    )

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"abc" and download.call_count == 2
    assert (tmp_path / "release.zip.done").is_file()


def test_fetch_partial_ranges_continue_until_total_size(bundle, tmp_path, mocker):
    dest = tmp_path / "release.zip"
    dest.write_bytes(b"a")
    mocker.patch.object(bundle.time, "sleep")
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=[
            Response(b"bc", status=206, **{"Content-Range": "bytes 1-2/5"}),
            Response(b"de", status=206, **{"Content-Range": "bytes 3-4/5"}),
        ],
    )

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"abcde"
    assert download.call_count == 2 and (tmp_path / "release.zip.done").is_file()


@pytest.mark.parametrize(
    "status,headers",
    [
        (200, {"Content-Length": "bad"}),
        (200, {"Content-Length": "0"}),
        (200, {"Content-Length": "-1"}),
        (204, {}),
        (206, {}),
        (206, {"Content-Range": "not a range"}),
        (206, {"Content-Range": "bytes 1-3/3", "Content-Length": "3"}),
        (206, {"Content-Range": "bytes 1-0/3"}),
        (206, {"Content-Range": "bytes 1-2/3"}),
    ],
)
def test_fetch_invalid_headers_never_append_or_mark_complete(
    bundle, tmp_path, mocker, status, headers
):
    dest = tmp_path / "release.zip"
    dest.write_bytes(b"a")
    sleep = mocker.patch.object(bundle.time, "sleep")
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=lambda *a, **kw: Response(b"b", status=status, **headers),
    )

    with pytest.raises(SystemExit, match="download failed"):
        bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"a" and not (tmp_path / "release.zip.done").exists()
    assert download.call_count == 8 and sleep.call_count == 7


def test_fetch_http_failure_closes_response_and_preserves_partial_file(
    bundle, tmp_path, mocker
):
    dest = tmp_path / "release.zip"
    dest.write_bytes(b"a")
    body = io.BytesIO(b"not found")
    error = HTTPError("https://example.invalid/release.zip", 404, "missing", {}, body)
    mocker.patch.object(bundle.time, "sleep")
    mocker.patch.object(bundle.urllib.request, "urlopen", side_effect=error)

    with pytest.raises(SystemExit, match="download failed"):
        bundle.fetch("https://example.invalid/release.zip", dest)

    assert body.closed and dest.read_bytes() == b"a"
    assert not (tmp_path / "release.zip.done").exists()


@pytest.mark.parametrize("code", [None, 0])
def test_elf2image_accepts_successful_esptool_exit(bundle, compilers, tmp_path, code):
    compilers.elf.side_effect = SystemExit(code)
    source, out = tmp_path / "app.elf", tmp_path / "firmware.bin"

    bundle.elf2image(source, out)

    compilers.elf.assert_called_once_with(
        [
            "--chip",
            "esp32",
            "elf2image",
            "--flash_mode",
            "dio",
            "--flash_freq",
            "80m",
            "--flash_size",
            "4MB",
            "-o",
            str(out),
            str(source),
        ]
    )


def test_elf2image_preserves_esptool_failure(bundle, compilers, tmp_path):
    compilers.elf.side_effect = SystemExit(2)

    with pytest.raises(SystemExit) as error:
        bundle.elf2image(tmp_path / "app.elf", tmp_path / "firmware.bin")

    assert error.value.code == 2


def test_build_littlefs_flushes_config_before_writing_image(
    bundle, compilers, tmp_path
):
    out = tmp_path / "littlefs.bin"
    config = b"machine: test\n"

    bundle.build_littlefs(config, 48, out)

    compilers.littlefs.assert_called_once_with(block_size=4096, block_count=48)
    compilers.fs.open.assert_called_once_with("/config.yaml", "wb")
    compilers.stream.write.assert_called_once_with(config)
    compilers.fs.unmount.assert_called_once()
    assert out.read_bytes() == b"filesystem"


def test_write_bundle_is_deterministic_and_replaces_complete_archive(bundle, tmp_path):
    files = {name: name.encode() for name in bundle.ORDER}
    out = tmp_path / "output/bundle.zip"
    out.parent.mkdir()
    out.write_bytes(b"previous")

    bundle.write_bundle(files, out)
    first = out.read_bytes()
    bundle.write_bundle(files, out)

    assert out.read_bytes() == first
    with zipfile.ZipFile(out) as archive:
        assert archive.namelist() == [
            "bootloader.bin",
            "partitions.bin",
            "boot_app0.bin",
            "firmware.bin",
            "littlefs.bin",
            "config.yaml",
            "fluidnc_version.txt",
        ]
        assert archive.read("config.yaml") == b"config.yaml"
        assert {entry.date_time for entry in archive.infolist()} == {
            (1980, 1, 1, 0, 0, 0)
        }
        assert {entry.create_system for entry in archive.infolist()} == {3}
        assert {entry.external_attr for entry in archive.infolist()} == {0o644 << 16}
    assert list(out.parent.iterdir()) == [out]


def test_main_builds_local_inputs_without_network(
    bundle, compilers, tmp_path, mocker, monkeypatch, capsys
):
    posix, elf, out = (
        tmp_path / "release.zip",
        tmp_path / "app.elf",
        tmp_path / "bundle.zip",
    )
    posix.write_bytes(_release_zip())
    elf.write_bytes(b"ELF")
    download = mocker.patch.object(bundle.urllib.request, "urlopen")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_bundle",
            "--tag",
            "v-test",
            "--posix-zip",
            str(posix),
            "--elf",
            str(elf),
            "--out",
            str(out),
        ],
    )

    bundle.main()

    download.assert_not_called()
    assert isinstance(compilers.littlefs.call_args.kwargs["block_count"], int)
    assert "192K config.yaml filesystem" in capsys.readouterr().out
    with zipfile.ZipFile(out) as archive:
        assert archive.read("firmware.bin") == b"firmware:ELF"
        assert archive.read("littlefs.bin") == b"filesystem"
        assert archive.read("config.yaml") == bundle.CONFIG_YAML.read_bytes()
        assert archive.read("fluidnc_version.txt") == b"v-test\n"


def test_main_downloads_release_and_uses_default_output(
    bundle, compilers, tmp_path, mocker, monkeypatch
):
    release = _release_zip()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bundle, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(sys, "argv", ["build_bundle", "--tag", "v-test"])
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=[Response(release), Response(b"ELF")],
    )

    bundle.main()

    assert (tmp_path / "dist/firmware/fluidnc.zip").is_file()
    assert [call.args[0].full_url for call in download.call_args_list] == [
        "https://github.com/bdring/FluidNC/releases/download/v-test/fluidnc-v-test-posix.zip",
        "https://github.com/bdring/FluidNC/releases/download/v-test/esp32-noradio-firmware.elf",
    ]
    assert (tmp_path / "cache/v-test/fluidnc-v-test-posix.zip").read_bytes() == release
    assert (tmp_path / "cache/v-test/esp32-noradio-firmware.elf").read_bytes() == b"ELF"


def test_main_default_release_reuses_documented_cache_directory(
    bundle, compilers, tmp_path, mocker, monkeypatch
):
    cache = tmp_path / ".cache/physiclaw-firmware/v4.0.3"
    cache.mkdir(parents=True)
    (cache / "fluidnc-v4.0.3-posix.zip").write_bytes(_release_zip(tag="v4.0.3"))
    (cache / "fluidnc-v4.0.3-posix.zip.done").touch()
    (cache / "esp32-noradio-firmware.elf").write_bytes(b"ELF")
    (cache / "esp32-noradio-firmware.elf.done").touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["build_bundle"])
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=AssertionError("unexpected download"),
    )

    bundle.main()

    download.assert_not_called()
    with zipfile.ZipFile(tmp_path / "dist/firmware/fluidnc.zip") as archive:
        assert archive.read("fluidnc_version.txt") == b"v4.0.3\n"
        assert archive.read("firmware.bin") == b"firmware:ELF"


def test_script_help_describes_builder_without_optional_dependencies(bundle, tmp_path):
    script = Path(bundle.__file__)

    result = subprocess.run(
        [sys.executable, "-S", str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "firmware bundle from a FluidNC release" in result.stdout
    assert "--tag" in result.stdout and "--posix-zip" in result.stdout


def test_main_refuses_moved_partition_before_converting(
    bundle, compilers, tmp_path, monkeypatch
):
    posix, elf, out = (
        tmp_path / "release.zip",
        tmp_path / "app.elf",
        tmp_path / "bundle.zip",
    )
    posix.write_bytes(_release_zip(_partition(offset=0x200000)))
    elf.write_bytes(b"ELF")
    out.write_bytes(b"previous")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_bundle",
            "--tag",
            "v-test",
            "--posix-zip",
            str(posix),
            "--elf",
            str(elf),
            "--out",
            str(out),
        ],
    )

    with pytest.raises(SystemExit, match="data partition moved"):
        bundle.main()

    compilers.elf.assert_not_called()
    assert out.read_bytes() == b"previous"
