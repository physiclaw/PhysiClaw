"""Public boundaries of the standalone firmware bundle builder."""

# Accepted mutation survivors: log/help decoration and temporary-directory prefixes
# do not affect the bundle; 60→61-second timeouts, 2→3-second retry delays, and
# 64→128-KiB chunks preserve bounded downloading. Missing Content-Range defaults
# of "" and "XXXX" both fail validation. ZIP seconds 0 and 1 encode identically
# because the DOS timestamp has two-second resolution.

import importlib.util
import io
import json
import shlex
import shutil
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
from symlink_helpers import symlink_to

FILESYSTEM_IMAGE = b"filesystem".ljust(0x30000, b"\xff")


@pytest.fixture
def bundle(mocker, tmp_path):
    path = Path(__file__).resolve().parents[2] / "firmware/firmware.py"
    spec = importlib.util.spec_from_file_location("firmware", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.OUTPUT_DIR = tmp_path / "firmware/output"
    module.CACHE_DIR = module.OUTPUT_DIR / ".cache"
    module.DEFAULT_OUTPUT = module.OUTPUT_DIR / module.BUNDLE_NAME
    mocker.patch.object(module.time, "sleep")
    return module


def _partition(ptype=1, subtype=0x82, offset=0x3D0000, size=0x30000):
    return struct.pack("<HBBII16sI", 0x50AA, ptype, subtype, offset, size, b"data", 0)


def _flash_table(app_offset=0x10000, app_size=0x1E0000, fs_size=0x30000):
    return b"".join(
        [
            _partition(1, 2, 0x9000, 0x5000),
            _partition(1, 0, 0xE000, 0x2000),
            _partition(0, 0x10, app_offset, app_size),
            _partition(0, 0x11, 0x1F0000, 0x1E0000),
            _partition(size=fs_size),
        ]
    )


def _release_zip(table=None, tag="v-test"):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(f"fluidnc-{tag}-posix/wifi/bootloader.bin", b"boot")
        archive.writestr(
            f"fluidnc-{tag}-posix/wifi/partitions.bin",
            _flash_table() if table is None else table,
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
        context=SimpleNamespace(buffer=bytearray(FILESYSTEM_IMAGE)),
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


def _metadata(bundle, release, elf=b"ELF", tag="v-test"):
    assets = []
    for name, data in [
        (f"fluidnc-{tag}-posix.zip", release),
        ("esp32-noradio-firmware.elf", elf),
    ]:
        assets.append(
            {
                "name": name,
                "size": len(data),
                "digest": f"sha256:{bundle.sha256(data)}",
                "browser_download_url": f"https://github.com/bdring/FluidNC/releases/download/{tag}/{name}",
            }
        )
    return Response(json.dumps({"assets": assets}).encode())


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


@pytest.mark.parametrize("cached", [None, b"", b"stale", b"valid"])
def test_fetch_reuses_only_checksum_verified_cache(bundle, tmp_path, mocker, cached):
    dest = tmp_path / "release.zip"
    if cached is not None:
        dest.write_bytes(cached)
    # Old completion markers must not override the actual checksum.
    (tmp_path / "release.zip.done").touch()
    download = mocker.patch.object(
        bundle.urllib.request, "urlopen", return_value=Response(b"valid")
    )

    reused = bundle.fetch(
        "https://example.invalid/release.zip",
        dest,
        digest=bundle.sha256(b"valid"),
        size=5,
    )

    assert reused is (cached == b"valid")
    assert dest.read_bytes() == b"valid"
    assert download.call_count == (0 if reused else 1)
    if not reused:
        assert download.call_args.args[0].get_header("Range") is None


@pytest.mark.parametrize("old", [b"stale prefix", b"valid"])
def test_fetch_without_upstream_checksum_always_downloads_fresh(
    bundle, tmp_path, mocker, old
):
    dest = tmp_path / "release.zip"
    dest.write_bytes(old)
    download = mocker.patch.object(
        bundle.urllib.request, "urlopen", return_value=Response(b"valid")
    )

    assert bundle.fetch("https://example.invalid/release.zip", dest) is False

    assert dest.read_bytes() == b"valid"
    assert download.call_args.args[0].get_header("Range") is None


@pytest.mark.parametrize("bad", [b"wrong", b"valid but extra bytes"])
def test_fetch_retries_checksum_or_size_mismatch_from_scratch(
    bundle, tmp_path, mocker, bad
):
    dest = tmp_path / "release.zip"
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=[Response(bad), Response(b"valid")],
    )

    bundle.fetch(
        "https://example.invalid/release.zip",
        dest,
        digest=bundle.sha256(b"valid"),
        size=5,
    )

    assert dest.read_bytes() == b"valid"
    assert download.call_count == 2
    assert all(
        call.args[0].get_header("Range") is None for call in download.call_args_list
    )


def test_fetch_never_accepts_repeated_checksum_mismatch(bundle, tmp_path, mocker):
    dest = tmp_path / "release.zip"
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=lambda *a, **kw: Response(b"wrong"),
    )

    with pytest.raises(SystemExit, match="download failed"):
        bundle.fetch(
            "https://example.invalid/release.zip", dest, digest=bundle.sha256(b"valid")
        )

    assert not dest.exists()
    assert download.call_count == 8


@pytest.mark.parametrize("size", [1, 131073])
def test_fetch_reads_entire_response(bundle, tmp_path, mocker, size):
    body = b"x" * size
    dest = tmp_path / "release.zip"
    mocker.patch.object(bundle.urllib.request, "urlopen", return_value=Response(body))

    bundle.fetch(
        "https://example.invalid/release.zip", dest, digest=bundle.sha256(body)
    )

    assert dest.read_bytes() == body


@pytest.mark.parametrize("range_status", [200, 206])
def test_fetch_resumes_only_current_download_and_handles_ignored_range(
    bundle, tmp_path, mocker, range_status
):
    dest = tmp_path / "release.zip"
    dest.write_bytes(b"previous run must be discarded")
    response = (
        Response(b"de", status=206, **{"Content-Range": "bytes 3-4/5"})
        if range_status == 206
        else Response(b"abcde")
    )
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=[Response(b"abc", **{"Content-Length": "5"}), response],
    )

    bundle.fetch(
        "https://example.invalid/release.zip", dest, digest=bundle.sha256(b"abcde")
    )

    assert dest.read_bytes() == b"abcde"
    assert download.call_args_list[0].args[0].get_header("Range") is None
    assert download.call_args_list[1].args[0].get_header("Range") == "bytes=3-"


def test_fetch_multiple_partial_ranges_continue_to_total_size(bundle, tmp_path, mocker):
    dest = tmp_path / "release.zip"
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=[
            Response(b"a", **{"Content-Length": "5"}),
            Response(b"bc", status=206, **{"Content-Range": "bytes 1-2/5"}),
            Response(b"de", status=206, **{"Content-Range": "bytes 3-4/5"}),
        ],
    )

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"abcde"
    assert [call.args[0].get_header("Range") for call in download.call_args_list] == [
        None,
        "bytes=1-",
        "bytes=3-",
    ]


def test_fetch_rejected_range_restarts_and_closes_error(bundle, tmp_path, mocker):
    dest = tmp_path / "release.zip"
    body = io.BytesIO(b"range unavailable")
    error = HTTPError("https://example.invalid/release.zip", 416, "range", {}, body)
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=[
            Response(b"a", **{"Content-Length": "3"}),
            error,
            Response(b"new"),
        ],
    )

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"new" and body.closed
    assert download.call_args_list[-1].args[0].get_header("Range") is None


@pytest.mark.parametrize(
    "status,headers",
    [
        (200, {"Content-Length": "bad"}),
        (200, {"Content-Length": "0"}),
        (200, {"Content-Length": "-1"}),
        (204, {}),
        (206, {}),
        (206, {"Content-Range": "not a range"}),
        (206, {"Content-Range": "bytes 0-0/2"}),
        (206, {"Content-Range": "bytes 1-3/3", "Content-Length": "3"}),
        (206, {"Content-Range": "bytes 1-0/3"}),
        (206, {"Content-Range": "bytes 1-2/3"}),
    ],
)
def test_download_invalid_headers_never_append(
    bundle, tmp_path, mocker, status, headers
):
    dest = tmp_path / "release.zip"
    dest.write_bytes(b"a")
    mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        return_value=Response(b"b", status=status, **headers),
    )

    with pytest.raises(ValueError):
        bundle._download_once("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"a"


def test_fetch_oversized_range_requires_valid_retry(bundle, tmp_path, mocker):
    dest = tmp_path / "release.zip"
    oversized = Response(b"bc", status=206, **{"Content-Range": "bytes 1-1/3"})
    oversized.headers.pop("Content-Length")
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=[
            Response(b"a", **{"Content-Length": "3"}),
            oversized,
            Response(b"abc"),
        ],
    )

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"abc" and download.call_count == 3


@pytest.mark.parametrize("first_body", [b"", b"valid"])
def test_fetch_no_content_length_requires_nonempty_response(
    bundle, tmp_path, mocker, first_body
):
    response = Response(first_body)
    response.headers.clear()
    download = mocker.patch.object(
        bundle.urllib.request, "urlopen", side_effect=[response, Response(b"valid")]
    )
    dest = tmp_path / "release.zip"

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert dest.read_bytes() == b"valid"
    assert download.call_count == (1 if first_body else 2)


def test_fetch_missing_asset_fails_immediately(bundle, tmp_path, mocker):
    dest = tmp_path / "release.zip"
    body = io.BytesIO(b"not found")
    error = HTTPError("https://example.invalid/release.zip", 404, "missing", {}, body)
    download = mocker.patch.object(bundle.urllib.request, "urlopen", side_effect=error)

    with pytest.raises(SystemExit, match="not found.*404") as failure:
        bundle.fetch("https://example.invalid/release.zip", dest)

    assert "https://example.invalid/release.zip" in str(failure.value)
    assert "Check --tag" in str(failure.value)
    download.assert_called_once()
    bundle.time.sleep.assert_not_called()
    assert body.closed and not dest.exists()


@pytest.mark.parametrize("status", [429, 500, 503])
def test_fetch_temporary_http_failure_retries(bundle, tmp_path, mocker, status):
    dest = tmp_path / "release.zip"
    body = io.BytesIO(b"temporarily unavailable")
    error = HTTPError("https://example.invalid/release.zip", status, "retry", {}, body)
    download = mocker.patch.object(
        bundle.urllib.request, "urlopen", side_effect=[error, Response(b"complete")]
    )

    bundle.fetch("https://example.invalid/release.zip", dest)

    assert download.call_count == 2
    bundle.time.sleep.assert_called_once_with(2)
    assert body.closed and dest.read_bytes() == b"complete"


@pytest.mark.parametrize("missing_asset", ["posix", "elf"])
@pytest.mark.parametrize("managed_output", [True, False])
def test_main_missing_release_asset_cleans_only_managed_output(
    bundle,
    compilers,
    tmp_path,
    mocker,
    capsys,
    missing_asset,
    managed_output,
):
    tag = "v-missing"
    name = (
        f"fluidnc-{tag}-posix.zip"
        if missing_asset == "posix"
        else "esp32-noradio-firmware.elf"
    )
    url = f"https://github.com/bdring/FluidNC/releases/download/{tag}/{name}"
    body = io.BytesIO(b"not found")
    error = HTTPError(url, 404, "Not Found", {}, body)
    release = _release_zip(tag=tag)
    responses = [_metadata(bundle, release, tag=tag)]
    if missing_asset == "elf":
        responses.append(Response(release))
    download = mocker.patch.object(
        bundle.urllib.request, "urlopen", side_effect=[*responses, error]
    )
    out = bundle.DEFAULT_OUTPUT if managed_output else tmp_path / "fluidnc.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"previous complete bundle")

    with pytest.raises(SystemExit, match="not found.*404") as failure:
        bundle.main(["--tag", tag, "--out", str(out)])

    assert url in str(failure.value)
    assert "https://github.com/bdring/FluidNC/releases" in str(failure.value)
    assert download.call_count == len(responses) + 1
    assert body.closed
    bundle.time.sleep.assert_not_called()
    compilers.elf.assert_not_called()
    compilers.littlefs.assert_not_called()
    if managed_output:
        assert not out.exists()
        assert list(out.parent.iterdir()) == [bundle.CACHE_DIR]
    else:
        assert out.read_bytes() == b"previous complete bundle"
    output = capsys.readouterr()
    assert "retry" not in output.err
    assert "7 files" not in output.out


@pytest.mark.parametrize("code", [None, 0])
def test_elf2image_accepts_successful_esptool_exit(
    bundle, compilers, tmp_path, capsys, code
):
    def convert(args):
        print("Creating ESP32 image...")
        raise SystemExit(code)

    compilers.elf.side_effect = convert
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
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("failure", [SystemExit(2), ValueError("invalid ELF")])
def test_elf2image_preserves_failure_and_diagnostics(
    bundle, compilers, tmp_path, capsys, failure
):
    def convert(args):
        print("ELF section failed validation")
        raise failure

    compilers.elf.side_effect = convert

    with pytest.raises(type(failure)) as error:
        bundle.elf2image(tmp_path / "app.elf", tmp_path / "firmware.bin")

    assert error.value is failure
    output = capsys.readouterr()
    assert output.out == ""
    assert "ELF section failed validation" in output.err
    assert "Image conversion failed" in output.err


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
    assert out.read_bytes() == FILESYSTEM_IMAGE


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


@pytest.mark.parametrize("entry_point", ["cli", "python"])
def test_local_inputs_build_complete_bundle_without_network(
    bundle, compilers, tmp_path, mocker, monkeypatch, capsys, entry_point
):
    posix, elf, out = (
        tmp_path / "release.zip",
        tmp_path / "app.elf",
        tmp_path / "output/bundle.zip",
    )
    posix.write_bytes(_release_zip())
    elf.write_bytes(b"ELF")
    out.parent.mkdir()
    sibling = out.parent / "unrelated.txt"
    sibling.write_text("keep", encoding="utf-8")
    download = mocker.patch.object(bundle.urllib.request, "urlopen")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "unrelated config in working directory", encoding="utf-8"
    )

    if entry_point == "cli":
        bundle.main(
            [
                "--tag",
                "v-test",
                "--posix-zip",
                str(posix),
                "--elf",
                str(elf),
                "--out",
                str(out),
            ]  # fmt: skip
        )
    else:
        bundle.build_bundle(tag="v-test", posix_zip=posix, elf=elf, out=out)

    download.assert_not_called()
    assert sibling.read_text(encoding="utf-8") == "keep"
    assert isinstance(compilers.littlefs.call_args.kwargs["block_count"], int)
    output = capsys.readouterr().out
    assert str(Path("output/bundle.zip")) in output and "7 files" in output
    assert bundle.file_sha256(out) in output
    assert len(output.splitlines()) <= 5
    with zipfile.ZipFile(out) as archive:
        assert {name: archive.read(name) for name in archive.namelist()} == {
            "bootloader.bin": b"boot",
            "partitions.bin": _flash_table(),
            "boot_app0.bin": b"app0",
            "firmware.bin": b"firmware:ELF",
            "littlefs.bin": FILESYSTEM_IMAGE,
            "config.yaml": bundle.CONFIG_YAML.read_bytes(),
            "fluidnc_version.txt": b"v-test\n",
        }


@pytest.mark.parametrize("local_input", ["posix_zip", "elf"])
def test_resolve_release_downloads_only_missing_input(
    bundle, tmp_path, mocker, local_input
):
    posix, elf = tmp_path / "local.zip", tmp_path / "local.elf"
    release = _release_zip()
    posix.write_bytes(release)
    elf.write_bytes(b"ELF")
    local_path = posix if local_input == "posix_zip" else elf
    payload = b"ELF" if local_input == "posix_zip" else release
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=[_metadata(bundle, release), Response(payload)],
    )

    resolved_posix, resolved_elf = bundle.resolve_release_assets(
        "v-test", **{local_input: local_path}
    )

    assert resolved_posix.read_bytes() == release
    assert resolved_elf.read_bytes() == b"ELF"
    if local_input == "posix_zip":
        assert resolved_posix == posix
        missing_name = "esp32-noradio-firmware.elf"
    else:
        assert resolved_elf == elf
        missing_name = "fluidnc-v-test-posix.zip"
    assert download.call_count == 2
    assert download.call_args.args[0].full_url == (
        f"https://github.com/bdring/FluidNC/releases/download/v-test/{missing_name}"
    )


@pytest.mark.parametrize("entry_point", ["python", "cli"])
def test_conversion_failure_cleans_only_cli_managed_output(
    bundle, compilers, tmp_path, entry_point
):
    posix, elf, out = (
        tmp_path / "release.zip",
        tmp_path / "app.elf",
        bundle.DEFAULT_OUTPUT,
    )
    posix.write_bytes(_release_zip())
    elf.write_bytes(b"ELF")
    out.parent.mkdir(parents=True)
    out.write_bytes(b"previous complete bundle")
    compilers.elf.side_effect = SystemExit(2)

    with pytest.raises(SystemExit) as error:
        if entry_point == "python":
            bundle.build_bundle(tag="v-test", posix_zip=posix, elf=elf, out=out)
        else:
            bundle.main(
                ["--tag", "v-test", "--posix-zip", str(posix), "--elf", str(elf)]
            )

    assert error.value.code == 2
    compilers.littlefs.assert_not_called()
    if entry_point == "python":
        assert out.read_bytes() == b"previous complete bundle"
    else:
        assert not out.exists()


@pytest.mark.parametrize("symlink_at_root", [True, False])
def test_main_cleanup_does_not_follow_output_symlinks(
    bundle, compilers, tmp_path, symlink_at_root
):
    posix, elf = tmp_path / "release.zip", tmp_path / "app.elf"
    posix.write_bytes(_release_zip())
    elf.write_bytes(b"ELF")
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("keep", encoding="utf-8")
    output_dir = bundle.OUTPUT_DIR
    output_dir.parent.mkdir()
    if symlink_at_root:
        symlink_to(output_dir, external, target_is_directory=True)
    else:
        output_dir.mkdir()
        symlink_to(output_dir / "linked-folder", external, target_is_directory=True)

    bundle.main(["--tag", "v-test", "--posix-zip", str(posix), "--elf", str(elf)])

    assert not output_dir.is_symlink()
    assert set(output_dir.iterdir()) == {bundle.CACHE_DIR, output_dir / "fluidnc.zip"}
    assert list(external.iterdir()) == [external / "keep.txt"]
    assert (external / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_main_aborts_if_managed_output_cannot_be_cleared(bundle, mocker):
    out = bundle.DEFAULT_OUTPUT
    out.parent.mkdir(parents=True)
    out.write_bytes(b"previous bundle")
    (out.parent / "old-build").mkdir()
    mocker.patch.object(bundle.shutil, "rmtree", side_effect=PermissionError("denied"))
    download = mocker.patch.object(bundle.urllib.request, "urlopen")

    with pytest.raises(
        SystemExit, match=r"Could not clear .*firmware[/\\]output: denied"
    ):
        bundle.main([])

    download.assert_not_called()
    assert not (bundle.CACHE_DIR / bundle.DEFAULT_TAG / "release.json").exists()


def test_main_downloads_release_and_uses_default_output(
    bundle, compilers, mocker, monkeypatch
):
    release = _release_zip()
    output_dir = bundle.OUTPUT_DIR
    output_dir.mkdir(parents=True)
    (output_dir / "fluidnc.zip").write_bytes(b"old bundle")
    (output_dir / ".stale").touch()
    (output_dir / "old-build").mkdir()
    (output_dir / "old-build/firmware.bin").write_bytes(b"stale image")
    monkeypatch.setattr(sys, "argv", ["firmware", "--tag", "v-test"])
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=[_metadata(bundle, release), Response(release), Response(b"ELF")],
    )

    bundle.main()

    assert set(output_dir.iterdir()) == {bundle.CACHE_DIR, output_dir / "fluidnc.zip"}
    assert [call.args[0].full_url for call in download.call_args_list] == [
        "https://api.github.com/repos/bdring/FluidNC/releases/tags/v-test",
        "https://github.com/bdring/FluidNC/releases/download/v-test/fluidnc-v-test-posix.zip",
        "https://github.com/bdring/FluidNC/releases/download/v-test/esp32-noradio-firmware.elf",
    ]
    assert (
        bundle.CACHE_DIR / "v-test/fluidnc-v-test-posix.zip"
    ).read_bytes() == release
    assert (
        bundle.CACHE_DIR / "v-test/esp32-noradio-firmware.elf"
    ).read_bytes() == b"ELF"
    generated_image = Path(compilers.elf.call_args.args[0][-2])
    assert generated_image.is_relative_to(output_dir)
    assert not generated_image.parent.exists()


def test_main_default_release_preserves_and_verifies_cache(
    bundle, compilers, mocker, capsys
):
    cache = bundle.CACHE_DIR / "v4.1.0"
    cache.mkdir(parents=True)
    release = _release_zip(tag="v4.1.0")
    posix = cache / "fluidnc-v4.1.0-posix.zip"
    posix.write_bytes(release)
    timestamp = posix.stat().st_mtime_ns
    (cache / "esp32-noradio-firmware.elf").write_bytes(b"ELF")
    (cache / "keep.txt").write_text("preserve cache contents", encoding="utf-8")
    (bundle.OUTPUT_DIR / "stale.zip").touch()
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        return_value=_metadata(bundle, release, tag="v4.1.0"),
    )

    bundle.main([])

    output = capsys.readouterr().out
    assert len(output.splitlines()) == 5
    assert output.count("cached · GitHub SHA-256 verified") == 2
    assert bundle.file_sha256(bundle.DEFAULT_OUTPUT) in output
    download.assert_called_once()
    assert (
        download.call_args.args[0].full_url
        == "https://api.github.com/repos/bdring/FluidNC/releases/tags/v4.1.0"
    )
    assert posix.stat().st_mtime_ns == timestamp
    assert (cache / "keep.txt").read_text(encoding="utf-8") == "preserve cache contents"
    assert not (bundle.OUTPUT_DIR / "stale.zip").exists()
    with zipfile.ZipFile(bundle.DEFAULT_OUTPUT) as archive:
        assert archive.read("fluidnc_version.txt") == b"v4.1.0\n"
        assert archive.read("firmware.bin") == b"firmware:ELF"


def test_script_help_describes_builder_without_optional_dependencies(bundle, tmp_path):
    previous = bundle.DEFAULT_OUTPUT
    previous.parent.mkdir(parents=True)
    previous.write_bytes(b"previous bundle")
    script = bundle.OUTPUT_DIR.parent / "firmware.py"
    shutil.copyfile(bundle.__file__, script)

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
    assert previous.read_bytes() == b"previous bundle"


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_make_firmware_runs_despite_existing_firmware_directory():
    repo = Path(__file__).resolve().parents[2]

    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "firmware",
            f"PY={shlex.join([sys.executable, '-S'])}",
            "ARGS=--help",
        ],  # fmt: skip
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "usage: firmware.py" in result.stdout
    assert "--posix-zip" in result.stdout and "--out" in result.stdout


def test_main_refuses_moved_partition_before_converting(bundle, compilers, tmp_path):
    posix, elf, out = (
        tmp_path / "release.zip",
        tmp_path / "app.elf",
        tmp_path / "bundle.zip",
    )
    posix.write_bytes(_release_zip(_partition(offset=0x200000)))
    elf.write_bytes(b"ELF")
    out.write_bytes(b"previous")
    with pytest.raises(SystemExit, match="data partition moved"):
        bundle.main(
            [
                "--tag",
                "v-test",
                "--posix-zip",
                str(posix),
                "--elf",
                str(elf),
                "--out",
                str(out),
            ]
        )

    compilers.elf.assert_not_called()
    assert out.read_bytes() == b"previous"


@pytest.mark.parametrize("stale_asset", ["zip", "elf"])
def test_main_refreshes_changed_or_corrupt_cached_assets(
    bundle, compilers, mocker, stale_asset
):
    cache = bundle.CACHE_DIR / "v-test"
    cache.mkdir(parents=True)
    release = _release_zip()
    zip_path, elf_path = (
        cache / "fluidnc-v-test-posix.zip",
        cache / "esp32-noradio-firmware.elf",
    )
    zip_path.write_bytes(b"stale zip" if stale_asset == "zip" else release)
    elf_path.write_bytes(b"old ELF" if stale_asset == "elf" else b"ELF")
    payload = release if stale_asset == "zip" else b"ELF"
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=[_metadata(bundle, release), Response(payload)],
    )
    (bundle.OUTPUT_DIR / "fluidnc.zip").write_bytes(b"old bundle")
    (bundle.OUTPUT_DIR / ".build-interrupted").mkdir()
    (bundle.OUTPUT_DIR / ".build-interrupted/firmware.bin").write_bytes(
        b"stale compiled image"
    )

    bundle.main(["--tag", "v-test"])

    assert download.call_count == 2
    assert download.call_args.args[0].get_header("Range") is None
    assert zip_path.read_bytes() == release and elf_path.read_bytes() == b"ELF"
    assert {path.name for path in bundle.OUTPUT_DIR.iterdir()} == {
        ".cache",
        "fluidnc.zip",
    }
    with zipfile.ZipFile(bundle.DEFAULT_OUTPUT) as archive:
        assert archive.read("firmware.bin") == b"firmware:ELF"


def test_release_without_checksums_downloads_fresh_instead_of_trusting_cache(
    bundle, mocker
):
    release = _release_zip()
    metadata = json.loads(_metadata(bundle, release).getvalue())
    cache = bundle.CACHE_DIR / "v-test"
    cache.mkdir(parents=True)
    for asset in metadata["assets"]:
        asset["digest"] = None
        (cache / asset["name"]).write_bytes(b"unverified old bytes")
    download = mocker.patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=[
            Response(json.dumps(metadata).encode()),
            Response(release),
            Response(b"ELF"),
        ],
    )

    posix, elf = bundle.resolve_release_assets("v-test")

    assert posix.read_bytes() == release and elf.read_bytes() == b"ELF"
    assert download.call_count == 3


@pytest.mark.parametrize("missing", ["release", "asset"])
def test_missing_release_metadata_or_asset_preserves_cache_but_clears_outputs(
    bundle, mocker, missing
):
    cache = bundle.CACHE_DIR / "v-test"
    cache.mkdir(parents=True)
    sentinel = cache / "keep.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    bundle.DEFAULT_OUTPUT.write_bytes(b"old bundle")
    if missing == "release":
        response = HTTPError(
            "https://api.github.com/repos/bdring/FluidNC/releases/tags/v-test",
            404,
            "Not Found",
            {},
            io.BytesIO(),
        )
    else:
        response = Response(b'{"assets": []}')
    download = mocker.patch.object(
        bundle.urllib.request, "urlopen", side_effect=[response]
    )

    with pytest.raises(SystemExit, match="Check --tag"):
        bundle.main(["--tag", "v-test"])

    download.assert_called_once()
    bundle.time.sleep.assert_not_called()
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not bundle.DEFAULT_OUTPUT.exists()


def test_bad_local_zip_reports_clean_error_and_removes_old_output(bundle, tmp_path):
    posix = tmp_path / "bad.zip"
    posix.write_bytes(b"not a zip")
    bundle.OUTPUT_DIR.mkdir(parents=True)
    bundle.DEFAULT_OUTPUT.write_bytes(b"previous bundle")

    with pytest.raises(SystemExit, match="error: File is not a zip file"):
        bundle.main(["--posix-zip", str(posix), "--elf", str(tmp_path / "app.elf")])

    assert not bundle.DEFAULT_OUTPUT.exists()
    assert {path.name for path in bundle.OUTPUT_DIR.iterdir()} == {".cache"}


@pytest.mark.parametrize("use_alias", [False, True])
def test_local_inputs_in_generated_output_are_rejected_before_cleanup(
    bundle, tmp_path, use_alias
):
    bundle.OUTPUT_DIR.mkdir(parents=True)
    local = bundle.OUTPUT_DIR / "local.elf"
    local.write_bytes(b"keep this input")
    source = local
    if use_alias:
        source = tmp_path / "local.elf"
        symlink_to(source, local)

    with pytest.raises(SystemExit, match="Local input would be deleted"):
        bundle.main(["--elf", str(source)])

    assert local.read_bytes() == b"keep this input"


@pytest.mark.parametrize("destination", ["cache", "config", "script", "directory"])
def test_rejects_output_that_would_overwrite_inputs_or_cache(bundle, destination):
    out = {
        "cache": bundle.CACHE_DIR / "bundle.zip",
        "config": bundle.CONFIG_YAML,
        "script": Path(bundle.__file__),
        "directory": bundle.OUTPUT_DIR,
    }[destination]

    with pytest.raises(SystemExit, match="overwrite an input|outside output/.cache"):
        bundle.main(["--out", str(out)])

    assert not bundle.OUTPUT_DIR.exists()


@pytest.mark.parametrize(
    "tag", ["../outside", "/tmp/release", ".", "..", "a/b", "a\\b"]
)
def test_tag_cannot_escape_cache_directory(bundle, tag):
    with pytest.raises(SystemExit) as failure:
        bundle.main(["--tag", tag])
    assert failure.value.code == 2
    assert not bundle.OUTPUT_DIR.exists()


def test_concurrent_build_is_rejected_before_cleaning_and_lock_releases(
    bundle,
):
    directory = bundle.OUTPUT_DIR.parent
    directory.mkdir(parents=True)
    script = directory / "firmware.py"
    shutil.copyfile(bundle.__file__, script)
    shutil.copyfile(bundle.CONFIG_YAML, directory / "config.yaml")
    with bundle.build_workspace():
        bundle.DEFAULT_OUTPUT.write_bytes(b"active build output")
        result = subprocess.run(
            [sys.executable, "-S", str(script)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0
        assert "Another firmware build" in result.stderr
        assert "Traceback" not in result.stderr
        assert bundle.DEFAULT_OUTPUT.read_bytes() == b"active build output"
    with bundle.build_workspace():
        assert not bundle.DEFAULT_OUTPUT.exists()


@pytest.mark.parametrize(
    "tty,no_color,term,colored",
    [
        (True, False, "xterm", True),
        (False, False, "xterm", False),
        (True, True, "xterm", False),
        (True, False, "dumb", False),
    ],
)
def test_terminal_color_respects_pipes_no_color_and_dumb_terminals(
    bundle, mocker, monkeypatch, capsys, tty, no_color, term, colored
):
    mocker.patch.object(sys.stdout, "isatty", return_value=tty)
    monkeypatch.setenv("TERM", term)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)

    bundle.log("Firmware bundle ready", "green")

    output = capsys.readouterr().out
    assert ("\033[" in output) == colored
    assert "Firmware bundle ready" in output


@pytest.mark.parametrize("size", [0, 4097])
def test_invalid_partition_size_fails_before_image_conversion(
    bundle, compilers, tmp_path, size
):
    table = bytearray(_partition())
    struct.pack_into("<I", table, 8, size)
    posix = tmp_path / "release.zip"
    posix.write_bytes(_release_zip(bytes(table)))

    with pytest.raises(SystemExit, match="Invalid LittleFS partition size"):
        bundle.main(
            [
                "--tag",
                "v-test",
                "--posix-zip",
                str(posix),
                "--elf",
                str(tmp_path / "app.elf"),
            ]
        )

    compilers.elf.assert_not_called()
    assert not bundle.DEFAULT_OUTPUT.exists()


def test_cache_symlink_cannot_redirect_downloads_outside_output(
    bundle, tmp_path, mocker
):
    bundle.CACHE_DIR.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("keep", encoding="utf-8")
    symlink_to(bundle.CACHE_DIR / "v-test", external, target_is_directory=True)
    download = mocker.patch.object(bundle.urllib.request, "urlopen")

    with pytest.raises(SystemExit, match="must not be a symlink"):
        bundle.main(["--tag", "v-test"])

    download.assert_not_called()
    assert list(external.iterdir()) == [external / "keep.txt"]


def test_failed_build_releases_lock_for_next_attempt(bundle):
    with pytest.raises(RuntimeError):
        with bundle.build_workspace():
            raise RuntimeError("failed build")
    with bundle.build_workspace():
        assert bundle.CACHE_DIR.is_dir()


@pytest.fixture
def local_release(tmp_path):
    posix = tmp_path / "release.zip"
    elf = tmp_path / "app.elf"
    out = tmp_path / "out.zip"
    posix.write_bytes(_release_zip())
    elf.write_bytes(b"ELF")
    out.write_bytes(b"previous bundle")
    return posix, elf, out


@pytest.mark.parametrize(
    "table",
    [
        _flash_table(app_offset=0x20000, app_size=0x1D0000),
        _flash_table(app_size=0x1F0000),
        _flash_table(fs_size=0x40000),
    ],
    ids=["moved-app", "overlapping-apps", "filesystem-beyond-flash"],
)
def test_incompatible_flash_layout_is_rejected_before_conversion(
    bundle, compilers, local_release, table
):
    posix, elf, out = local_release
    posix.write_bytes(_release_zip(table))

    with pytest.raises(SystemExit):
        bundle.build_bundle(tag="v-test", posix_zip=posix, elf=elf, out=out)

    compilers.elf.assert_not_called()
    assert out.read_bytes() == b"previous bundle"


def test_firmware_larger_than_app_partition_is_rejected(
    bundle, compilers, local_release
):
    posix, elf, out = local_release
    posix.write_bytes(_release_zip(_flash_table(app_size=0x1000)))
    elf.write_bytes(b"x" * 4096)

    with pytest.raises(SystemExit, match="firmware.bin"):
        bundle.build_bundle(tag="v-test", posix_zip=posix, elf=elf, out=out)

    assert out.read_bytes() == b"previous bundle"


@pytest.mark.parametrize(
    "extra",
    [
        _partition(1, 2, 0x8000, 0x1000),
        _partition(1, 2, 0x11000, 0),
        _partition(1, 2, 0x11001, 0x1000),
        _partition(1, 2, 0x11000, 1),
        _partition(0, 0x10, 0x20000, 0x1000),
        _partition(1, 0, 0x20000, 0x1000),
    ],
    ids=[
        "reserved-flash",
        "empty",
        "unaligned-offset",
        "unaligned-size",
        "duplicate-app",
        "duplicate-ota-data",
    ],
)
def test_invalid_partition_records_are_rejected(bundle, extra):
    with pytest.raises(SystemExit):
        bundle.image_capacities(_flash_table(app_size=0x1000) + extra)


@pytest.mark.parametrize("missing_index", [1, 2])
def test_missing_boot_partition_is_rejected(bundle, missing_index):
    table = _flash_table()
    table = table[: missing_index * 32] + table[(missing_index + 1) * 32 :]

    with pytest.raises(SystemExit, match="Expected one partition"):
        bundle.image_capacities(table)


@pytest.mark.parametrize(
    "name",
    [
        "bootloader.bin",
        "partitions.bin",
        "boot_app0.bin",
        "firmware.bin",
        "littlefs.bin",
    ],
)
@pytest.mark.parametrize("invalid_size", ["empty", "too-large"])
def test_images_cannot_exceed_their_flash_regions(bundle, name, invalid_size):
    capacities = bundle.image_capacities(_flash_table())
    data = b"" if invalid_size == "empty" else b"x" * (capacities[name] + 1)

    with pytest.raises(SystemExit, match=name):
        bundle.validate_image_sizes({name: data}, capacities)


def test_truncated_filesystem_cannot_be_published(bundle, compilers, local_release):
    posix, elf, out = local_release
    compilers.fs.context.buffer = bytearray(b"truncated filesystem")

    with pytest.raises(SystemExit, match="littlefs.bin must fill"):
        bundle.build_bundle(tag="v-test", posix_zip=posix, elf=elf, out=out)

    assert out.read_bytes() == b"previous bundle"


def test_firmware_can_exactly_fill_its_application_slot(
    bundle, compilers, local_release
):
    posix, elf, out = local_release
    posix.write_bytes(_release_zip(_flash_table(app_size=0x1000)))
    elf.write_bytes(b"x" * (4096 - len(b"firmware:")))

    bundle.build_bundle(tag="v-test", posix_zip=posix, elf=elf, out=out)

    with zipfile.ZipFile(out) as archive:
        assert archive.getinfo("firmware.bin").file_size == 4096
