"""Download OmniParser V2 icon detection model and convert to ONNX.

Requires `convert` dependency group: uv sync --group convert
Usage: uv run python scripts/download_omniparser.py
"""

import logging
import urllib.request

from physiclaw import paths

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

ONNX_PATH = paths.omniparser_onnx()
MODEL_DIR = ONNX_PATH.parent
PT_PATH = MODEL_DIR / "model.pt"
PT_URL = "https://huggingface.co/microsoft/OmniParser-v2.0/resolve/main/icon_detect/model.pt"


def main():
    if ONNX_PATH.exists():
        log.info(f"Already exists: {ONNX_PATH}")
        return

    # Download
    if not PT_PATH.exists():
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        log.info(f"Downloading model.pt ...")
        urllib.request.urlretrieve(PT_URL, PT_PATH)
        log.info(f"  {PT_PATH.stat().st_size / 1024 / 1024:.1f} MB")

    # Convert
    try:
        from ultralytics import YOLO  # type: ignore[import-not-found]
    except ImportError:
        raise RuntimeError("Run `uv sync --group convert` first")
    log.info("Converting to ONNX ...")
    YOLO(str(PT_PATH)).export(format="onnx", imgsz=1280)
    exported = PT_PATH.with_suffix(".onnx")
    if exported != ONNX_PATH:
        exported.rename(ONNX_PATH)
    log.info(f"  {ONNX_PATH.stat().st_size / 1024 / 1024:.1f} MB")

    # Cleanup .pt
    PT_PATH.unlink(missing_ok=True)
    log.info("Done. Run `uv sync` to remove conversion deps.")


if __name__ == "__main__":
    main()
