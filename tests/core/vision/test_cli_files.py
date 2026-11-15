"""Diagnostic entry points keep filesystem I/O independent of OpenCV paths."""

import importlib.util
import runpy
import sys
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from physiclaw.common import paths
from physiclaw.common.image import read_image, write_image


@pytest.mark.parametrize("module", ["icon_detect", "ocr", "ui_elements"])
def test_image_cli_reads_and_writes_unicode_paths(
    tmp_path, monkeypatch, mocker, module
):
    source = tmp_path / "截图.png"
    target = tmp_path / "标注.png"
    write_image(source, np.zeros((32, 32, 3), dtype=np.uint8))
    model = paths.omniparser_onnx()
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"stubbed inference model")
    net = mocker.Mock()
    net.forward.return_value = np.zeros((1, 5, 1), dtype=np.float32)
    mocker.patch.object(cv2.dnn, "readNetFromONNX", return_value=net)
    reader = mocker.Mock(return_value=SimpleNamespace(boxes=None, txts=None))
    monkeypatch.setitem(
        sys.modules, "rapidocr", SimpleNamespace(RapidOCR=lambda **kwargs: reader)
    )
    monkeypatch.setattr(sys, "argv", [module, str(source), "--output", str(target)])

    spec = importlib.util.find_spec(f"physiclaw.core.vision.{module}")
    assert spec is not None and spec.origin is not None
    runpy.run_path(spec.origin, run_name="__main__")

    assert read_image(target) is not None
