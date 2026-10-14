"""Tests for `physiclaw.common.dumps`."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from freezegun import freeze_time

from physiclaw.common import dumps


@pytest.fixture
def fake_dirs(tmp_path: Path, mocker) -> dict[str, Path]:
    tc = tmp_path / "tool_calls"
    sn = tmp_path / "snapshots"
    ss = tmp_path / "screenshots"
    mocker.patch.object(dumps.paths, "tool_calls_dir", return_value=tc)
    mocker.patch.object(dumps.paths, "snapshots_dir", return_value=sn)
    mocker.patch.object(dumps.paths, "screenshots_dir", return_value=ss)
    return {"tool_calls": tc, "snapshots": sn, "screenshots": ss}


@freeze_time("2026-10-13T10:11:12.123456")
def test_save_screenshot_name_keeps_chronological_prefix(
    fake_dirs, monkeypatch
) -> None:
    monkeypatch.setenv("PHYSICLAW_SAVE_SCREENSHOTS", "1")

    dumps.save_screenshot(b"image")

    path = next(fake_dirs["screenshots"].iterdir())
    assert path.name.startswith("20261013_101112_123_")
    assert path.suffix == ".jpg"


# ---------- _mkdir ----------


def test_mkdir_creates_directory(tmp_path: Path) -> None:
    d = tmp_path / "newdir"

    out = dumps._mkdir(d)

    assert out is d
    assert d.exists()
    assert d.is_dir()


def test_mkdir_recreates_dir_deleted_externally(tmp_path: Path) -> None:
    """`physiclaw clear` rmtrees dump dirs from another process while a
    server is live — the next save must recreate them."""
    d = tmp_path / "x"
    dumps._mkdir(d)
    d.rmdir()

    dumps._mkdir(d)

    assert d.is_dir()


def test_mkdir_idempotent(tmp_path: Path) -> None:
    d = tmp_path / "x"
    dumps._mkdir(d)
    dumps._mkdir(d)  # second call must not raise

    assert d.is_dir()


# ---------- save_tool_call ----------


def test_save_tool_call_no_op_when_env_unset(
    fake_dirs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PHYSICLAW_SAVE_TOOL_CALLS", raising=False)

    dumps.save_tool_call("peek", "listing")

    assert not fake_dirs["tool_calls"].exists()


def test_save_tool_call_writes_listing(
    fake_dirs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHYSICLAW_SAVE_TOOL_CALLS", "1")

    dumps.save_tool_call("peek", "abc")

    files = list(fake_dirs["tool_calls"].iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".txt"
    assert files[0].read_text() == "abc"
    assert "_peek." in files[0].name


def test_save_tool_call_writes_jpeg_when_provided(
    fake_dirs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHYSICLAW_SAVE_TOOL_CALLS", "1")

    dumps.save_tool_call("screenshot", "list", jpeg=b"\xff\xd8")

    by_suffix = {p.suffix: p for p in fake_dirs["tool_calls"].iterdir()}
    assert ".txt" in by_suffix
    assert ".jpg" in by_suffix
    assert by_suffix[".jpg"].read_bytes() == b"\xff\xd8"


def test_save_tool_call_skips_jpeg_when_none(
    fake_dirs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHYSICLAW_SAVE_TOOL_CALLS", "1")

    dumps.save_tool_call("peek", "list", jpeg=None)

    suffixes = {p.suffix for p in fake_dirs["tool_calls"].iterdir()}
    assert suffixes == {".txt"}


# ---------- save_snapshot ----------


def test_save_snapshot_no_op_when_env_unset(
    fake_dirs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PHYSICLAW_SAVE_SNAPSHOTS", raising=False)
    frame = np.zeros((10, 10, 3), dtype=np.uint8)

    dumps.save_snapshot(frame)

    assert not fake_dirs["snapshots"].exists()


def test_save_snapshot_writes_jpeg(
    fake_dirs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHYSICLAW_SAVE_SNAPSHOTS", "1")
    frame = np.zeros((20, 20, 3), dtype=np.uint8)

    dumps.save_snapshot(frame)

    files = list(fake_dirs["snapshots"].iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".jpg"
    assert files[0].stat().st_size > 0  # cv2 actually wrote


# ---------- save_screenshot ----------


def test_save_screenshot_no_op_when_env_unset(
    fake_dirs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PHYSICLAW_SAVE_SCREENSHOTS", raising=False)

    dumps.save_screenshot(b"data")

    assert not fake_dirs["screenshots"].exists()


def test_save_screenshot_writes_bytes(
    fake_dirs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHYSICLAW_SAVE_SCREENSHOTS", "1")

    dumps.save_screenshot(b"\x89PNG-fake-data")

    files = list(fake_dirs["screenshots"].iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == b"\x89PNG-fake-data"
    assert files[0].suffix == ".jpg"


def test_save_screenshot_filenames_unique_per_call(
    fake_dirs,
    monkeypatch: pytest.MonkeyPatch,
    mocker,
) -> None:
    """Stamps differ across rapid-fire calls. Without ms precision, two
    calls in the same second would collide; mock _stamp to verify each
    call gets a fresh filename."""
    monkeypatch.setenv("PHYSICLAW_SAVE_SCREENSHOTS", "1")
    stamps = iter(["t1", "t2", "t3"])
    mocker.patch.object(dumps, "_stamp", side_effect=lambda: next(stamps))

    for _ in range(3):
        dumps.save_screenshot(b"x")

    assert {p.stem for p in fake_dirs["screenshots"].iterdir()} == {"t1", "t2", "t3"}


@freeze_time("2026-10-13T10:11:12.123456")
def test_regression_tool_dumps_same_tick_preserve_both_pairs(
    fake_dirs, monkeypatch
) -> None:
    monkeypatch.setenv("PHYSICLAW_SAVE_TOOL_CALLS", "1")

    dumps.save_tool_call("peek", "first", b"first")
    dumps.save_tool_call("peek", "second", b"second")

    text_files = sorted(fake_dirs["tool_calls"].glob("*.txt"))
    assert {p.read_text() for p in text_files} == {"first", "second"}
    assert {p.with_suffix(".jpg").read_bytes() for p in text_files} == {
        b"first",
        b"second",
    }


@pytest.mark.parametrize(
    "save,args,env",
    [
        (
            dumps.save_tool_call,
            ("peek", "listing", b"jpeg"),
            "PHYSICLAW_SAVE_TOOL_CALLS",
        ),
        (dumps.save_screenshot, (b"jpeg",), "PHYSICLAW_SAVE_SCREENSHOTS"),
        (dumps.save_snapshot, (None,), "PHYSICLAW_SAVE_SNAPSHOTS"),
        (dumps.save_raw_camera, (None,), "PHYSICLAW_SAVE_RAW_CAMERA"),
    ],
)
def test_regression_dump_directory_failure_does_not_escape(
    save, args, env, monkeypatch, mocker
) -> None:
    monkeypatch.setenv(env, "1")
    mkdir = mocker.patch.object(Path, "mkdir", side_effect=PermissionError("read only"))

    save(*args)

    mkdir.assert_called_once()


def test_dump_tool_pair_text_failure_is_nonfatal(
    fake_dirs, monkeypatch, mocker
) -> None:
    monkeypatch.setenv("PHYSICLAW_SAVE_TOOL_CALLS", "1")
    write = mocker.patch.object(dumps, "write_text", side_effect=OSError("disk full"))

    dumps.save_tool_call("peek", "text", b"image")

    write.assert_called_once()


def test_dump_tool_pair_image_failure_is_nonfatal(
    fake_dirs, monkeypatch, mocker
) -> None:
    monkeypatch.setenv("PHYSICLAW_SAVE_TOOL_CALLS", "1")
    # Let the text phase succeed so the failure exercises the image write.
    mocker.patch.object(dumps, "write_text")
    write = mocker.patch.object(Path, "write_bytes", side_effect=OSError("disk full"))

    dumps.save_tool_call("peek", "text", b"image")

    write.assert_called_once()


def test_dump_screenshot_write_failure_is_nonfatal(
    fake_dirs, monkeypatch, mocker
) -> None:
    monkeypatch.setenv("PHYSICLAW_SAVE_SCREENSHOTS", "1")
    write = mocker.patch.object(Path, "write_bytes", side_effect=OSError("disk full"))

    dumps.save_screenshot(b"image")

    write.assert_called_once()


@pytest.mark.parametrize(
    "save,env",
    [
        (dumps.save_snapshot, "PHYSICLAW_SAVE_SNAPSHOTS"),
        (dumps.save_raw_camera, "PHYSICLAW_SAVE_RAW_CAMERA"),
    ],
)
def test_dump_camera_encoder_failure_is_nonfatal(
    save, env, monkeypatch, mocker
) -> None:
    import cv2

    monkeypatch.setenv(env, "1")
    encode = mocker.patch.object(cv2, "imwrite", side_effect=cv2.error("bad frame"))

    save(None)

    encode.assert_called_once()


def test_dump_camera_unsuccessful_write_is_reported(
    monkeypatch, mocker, caplog
) -> None:
    import cv2

    monkeypatch.setenv("PHYSICLAW_SAVE_RAW_CAMERA", "1")
    mocker.patch.object(cv2, "imwrite", return_value=False)

    dumps.save_raw_camera(None)

    assert "could not write" in caplog.text


def test_dump_raw_camera_disabled_does_not_encode(monkeypatch, mocker) -> None:
    import cv2

    monkeypatch.delenv("PHYSICLAW_SAVE_RAW_CAMERA", raising=False)
    encode = mocker.patch.object(cv2, "imwrite")

    dumps.save_raw_camera(None)

    encode.assert_not_called()


def test_regression_dump_invalid_utf8_listing_is_nonfatal(
    fake_dirs, monkeypatch, caplog
) -> None:
    monkeypatch.setenv("PHYSICLAW_SAVE_TOOL_CALLS", "1")

    dumps.save_tool_call("peek", "unencodable \ud800", b"image")

    assert "tool-call dump failed" in caplog.text
