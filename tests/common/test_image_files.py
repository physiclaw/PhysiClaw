"""Real image files exercise the portable I/O boundary on every OS."""

import cv2
import numpy as np
import pytest

from physiclaw.common.image import read_image, write_image


@pytest.mark.parametrize("suffix", [".png", ".jpg", ".bmp"])
def test_image_files_round_trip_through_unicode_paths(tmp_path, suffix):
    path = tmp_path / "测试 🚀" / f"截图{suffix}"
    path.parent.mkdir()
    frame = np.full((32, 32, 3), (100, 150, 200), dtype=np.uint8)

    write_image(path, frame)
    recovered = read_image(path)

    assert path.stat().st_size > 0
    assert recovered is not None
    np.testing.assert_allclose(recovered, frame, atol=5)


@pytest.mark.parametrize("data", [None, b"", b"not an image"])
def test_read_image_reports_unreadable_files(tmp_path, data):
    path = tmp_path / "bad.png"
    if data is not None:
        path.write_bytes(data)
    assert read_image(path) is None


def test_write_image_reports_encoder_failure_without_touching_existing_file(
    tmp_path, mocker
):
    path = tmp_path / "image.png"
    path.write_bytes(b"previous")
    mocker.patch.object(cv2, "imencode", return_value=(False, None))

    with pytest.raises(OSError, match="Could not encode image"):
        write_image(path, np.zeros((1, 1, 3), dtype=np.uint8))

    assert path.read_bytes() == b"previous"


def test_write_image_reports_filesystem_failure(tmp_path):
    with pytest.raises(FileNotFoundError):
        write_image(
            tmp_path / "missing" / "image.png", np.zeros((1, 1, 3), dtype=np.uint8)
        )
