"""Optional debug dumps to the user data dir.

Each helper is a no-op unless its env var is set (typically via a CLI
flag on ``physiclaw server``):

| env var                    | CLI flag            | subdir         |
| -------------------------- | ------------------- | -------------- |
| PHYSICLAW_SAVE_TOOL_CALLS  | --save-tool-calls   | tool_calls/    |
| PHYSICLAW_SAVE_SNAPSHOTS   | --save-snapshots    | snapshots/     |
| PHYSICLAW_SAVE_SCREENSHOTS | --save-screenshots  | screenshots/   |
| PHYSICLAW_SAVE_RAW_CAMERA  | --save-raw-camera   | raw_camera/    |

Filenames have a millisecond timestamp and a random suffix to keep
same-tick captures distinct. Dump failures are logged and never replace
the result of the action or camera read that produced them.
"""

import logging
import os
import secrets
from datetime import datetime
from pathlib import Path

from physiclaw.common import paths
from physiclaw.common.text import write_text

log = logging.getLogger(__name__)


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3] + "_" + secrets.token_hex(6)


def _mkdir(d: Path) -> Path:
    # Unconditional: `physiclaw clear` may rmtree these dirs while a
    # server is live, and promises they're recreated on the next save.
    d.mkdir(parents=True, exist_ok=True)
    return d


# The view-producing tool being dumped: "peek", "screenshot", or any
# gesture name — every gesture attaches a fused post-action view now.
ToolKind = str


def save_tool_call(kind: ToolKind, listing: str, jpeg: bytes | None = None) -> None:
    if not os.environ.get("PHYSICLAW_SAVE_TOOL_CALLS"):
        return
    try:
        d = _mkdir(paths.tool_calls_dir())
        stamp = _stamp()
        write_text(d / f"{stamp}_{kind}.txt", listing)
        if jpeg is not None:
            (d / f"{stamp}_{kind}.jpg").write_bytes(jpeg)
    except (OSError, UnicodeError):
        log.warning("tool-call dump failed", exc_info=True)


def save_snapshot(frame) -> None:
    if not os.environ.get("PHYSICLAW_SAVE_SNAPSHOTS"):
        return
    _save_frame(paths.snapshots_dir(), frame)


def save_screenshot(data: bytes) -> None:
    if not os.environ.get("PHYSICLAW_SAVE_SCREENSHOTS"):
        return
    try:
        d = _mkdir(paths.screenshots_dir())
        (d / f"{_stamp()}.jpg").write_bytes(data)
    except OSError:
        log.warning("screenshot dump failed", exc_info=True)


def save_raw_camera(frame) -> None:
    if not os.environ.get("PHYSICLAW_SAVE_RAW_CAMERA"):
        return
    _save_frame(paths.raw_camera_dir(), frame)


def _save_frame(directory: Path, frame) -> None:
    import cv2

    try:
        d = _mkdir(directory)
        if not cv2.imwrite(str(d / f"{_stamp()}.jpg"), frame):
            log.warning("camera dump encoder could not write to %s", directory)
    except (OSError, cv2.error):
        log.warning("camera dump failed", exc_info=True)
