"""Calibration — the typed container for all values learned during setup.

One object, one source of truth. Each field is ``None`` until its step runs.
``complete`` flips True when every required field is filled, and
``transforms()`` returns the :class:`ScreenTransforms` used by MCP tools.

Hardware-side mutable state (``arm.MOVE_DIRECTIONS``, ``cam.rotation``)
is still set imperatively by each step, but its source of truth lives here.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
from pathlib import Path

import cv2
import numpy as np

from physiclaw.common import paths
from physiclaw.common.logger import write_json_atomic
from physiclaw.common.text import read_text
from physiclaw.core.calibration.transforms import ScreenTransforms, ViewportShift
from physiclaw.core.geometry import apply_affine, validate_affine

log = logging.getLogger(__name__)


ROTATION_NAMES: dict[int, str] = {
    -1: "none",
    cv2.ROTATE_90_CLOCKWISE: "90° CW",
    cv2.ROTATE_180: "180°",
    cv2.ROTATE_90_COUNTERCLOCKWISE: "90° CCW",
}

DEFAULT_ROTATION: int = cv2.ROTATE_90_COUNTERCLOCKWISE

BUNDLE_PATH = paths.calibration_bundle()

# Schema version of the persisted bundle. Bump when a field's meaning or
# shape changes so an old on-disk bundle fails loudly at load instead of
# reconstructing wrong (``from_dict`` raises on mismatch). Bundles
# written before versioning carry no key and count as version 1.
# ``cam_focus`` rides version 1: a bundle without the key loads as None
# (lens left on live AF — degraded, not wrong), so no forced
# recalibration; re-running the camera-mapping step pins it.
BUNDLE_VERSION = 1


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _positive_size(value: object, field: str) -> tuple[int, int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(type(v) is not int or v <= 0 for v in value)
    ):
        raise ValueError(f"{field} must contain two positive integer dimensions")
    for dimension in value:
        _finite_number(dimension, field)
    return (value[0], value[1])


def _load_affine(value: object, field: str) -> np.ndarray | None:
    if value is None:
        return None
    try:
        affine = np.array(value, dtype=np.float64)
        validate_affine(affine)
    except (TypeError, ValueError, OverflowError, np.linalg.LinAlgError) as exc:
        raise ValueError(f"invalid {field}: {exc}") from exc
    return affine


def _load_viewport_shift(value: object) -> ViewportShift | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("viewport_shift must be an object")
    try:
        shift = ViewportShift(**value)
    except TypeError as exc:
        raise ValueError(f"invalid viewport_shift: {exc}") from exc
    _finite_number(shift.offset_x, "viewport_shift.offset_x")
    _finite_number(shift.offset_y, "viewport_shift.offset_y")
    if _finite_number(shift.dpr, "viewport_shift.dpr") <= 0:
        raise ValueError("viewport_shift.dpr must be positive")
    _positive_size(
        (shift.screenshot_width, shift.screenshot_height), "viewport_shift size"
    )
    return shift


@dataclasses.dataclass
class Calibration:
    viewport_shift: ViewportShift | None = None
    cam_rotation: int | None = None  # cv2 rotation code: -1, 0, 1, 2
    pct_to_grbl: np.ndarray | None = None  # 2×3 affine: screen 0-1 → arm mm
    pct_to_cam: np.ndarray | None = None  # 2×3 affine: screen 0-1 → camera 0-1
    cam_size: tuple[int, int] | None = None  # (width, height) in camera pixels
    cam_index: int | None = None  # USB camera index (for warm-restart)
    screen_dimension: dict | None = None  # width, height, viewport_w/h in CSS pt
    # Absolute lens position pinned during the camera-mapping step, in
    # the driver's native units. None on rigs that can't lock (fixed-
    # focus camera, macOS without the UVC channel) or when the pin
    # didn't verify end-to-end (see `camera_map._pin_focus`) — those
    # run on live AF. Deliberately NOT part of `complete`: a rig
    # without it is degraded, not broken.
    cam_focus: float | None = None

    @property
    def transforms_ready(self) -> bool:
        """Cheap existence check — true iff transforms() would return non-None."""
        return (
            self.pct_to_grbl is not None
            and self.pct_to_cam is not None
            and self.cam_size is not None
        )

    @property
    def complete(self) -> bool:
        """True when every field (including pre-req ones) is set."""
        return (
            self.viewport_shift is not None
            and self.cam_rotation is not None
            and self.screen_dimension is not None
            and "viewport_width" in self.screen_dimension
            and "viewport_height" in self.screen_dimension
            and self.transforms_ready
        )

    def transforms(self) -> ScreenTransforms | None:
        """Build a ScreenTransforms if arm + camera mappings are both set."""
        # Spelled per-field (same condition as transforms_ready) so each
        # argument below is narrowed to its non-None type.
        if self.pct_to_grbl is None or self.pct_to_cam is None or self.cam_size is None:
            return None
        return ScreenTransforms(
            pct_to_grbl=self.pct_to_grbl,
            pct_to_cam=self.pct_to_cam,
            cam_size=self.cam_size,
        )

    def reconcile_cam_size(self, live_size: tuple[int, int]) -> bool:
        """Adopt the live camera's frame size when it differs from the
        calibrated one but keeps the aspect ratio (the same camera
        negotiated a different mode — e.g. the requested capture
        resolution changed between calibrations).

        ``pct_to_cam`` maps screen 0-1 to camera 0-1, so it holds across
        any same-FOV mode switch; only ``cam_size`` (the pixel boundary)
        needs updating. A camera that crops its FOV at some modes breaks
        the 0-1 mapping despite matching aspect — warm-start's sanity tap
        is the end-to-end check that catches that case.

        Returns True when compatible (updated in place, or already
        equal); False on an aspect mismatch — the mapping can't be
        salvaged, recalibrate. ``live_size`` is the ROTATED frame size,
        the same space ``cam_size`` was measured in.
        """
        if self.cam_size is None or tuple(live_size) == tuple(self.cam_size):
            return True
        cal_w, cal_h = self.cam_size
        live_w, live_h = live_size
        if abs(live_w * cal_h - cal_w * live_h) > 0.01 * cal_w * live_h:
            log.error(
                f"Camera aspect changed: calibrated {cal_w}x{cal_h}, "
                f"live {live_w}x{live_h} — recalibration required"
            )
            return False
        log.info(
            f"Camera resolution changed: calibrated {cal_w}x{cal_h} → "
            f"live {live_w}x{live_h} (same aspect) — cam_size rescaled"
        )
        self.cam_size = (live_w, live_h)
        return True

    def pct_to_grbl_mm(self, x: float, y: float) -> tuple[float, float] | None:
        """Convert screen pct (0-1, x=horizontal, y=vertical) to GRBL mm
        using just `pct_to_grbl`. Returns None until that affine is set
        (e.g. before arm calibration). Unlike `transforms()`, doesn't
        require the camera mapping — usable after the arm fit but before
        the camera mapping. Negative values
        and values >1 are valid (off-phone positions in the same
        coordinate frame)."""
        if self.pct_to_grbl is None:
            return None
        return apply_affine(self.pct_to_grbl, x, y)

    def summary(self) -> dict:
        """Per-step status for /api/status — one line per filled field."""
        out: dict = {}
        if self.viewport_shift is not None:
            t = self.viewport_shift
            out["viewport_shift"] = f"dpr={t.dpr}, offset=({t.offset_x}, {t.offset_y})"
        if self.cam_rotation is not None:
            out["rotation"] = ROTATION_NAMES.get(
                self.cam_rotation, str(self.cam_rotation)
            )
        if self.pct_to_grbl is not None:
            out["mapping_a"] = "OK"
        if self.pct_to_cam is not None:
            out["mapping_b"] = "OK"
        if self.cam_focus is not None:
            out["focus"] = f"pinned @ {self.cam_focus:g}"
        if self.transforms_ready:
            out["validated"] = True
        return out

    def effective_rotation(self) -> int:
        """Rotation code for camera frame processing; falls back to DEFAULT_ROTATION."""
        return self.cam_rotation if self.cam_rotation is not None else DEFAULT_ROTATION

    # ─── Persistence ────────────────────────────────────────

    def to_dict(self) -> dict:
        """JSON-safe snapshot of this bundle. numpy arrays become nested lists."""
        return {
            "version": BUNDLE_VERSION,
            "viewport_shift": (
                dataclasses.asdict(self.viewport_shift)
                if self.viewport_shift is not None
                else None
            ),
            "cam_rotation": self.cam_rotation,
            "pct_to_grbl": (
                self.pct_to_grbl.tolist() if self.pct_to_grbl is not None else None
            ),
            "pct_to_cam": (
                self.pct_to_cam.tolist() if self.pct_to_cam is not None else None
            ),
            "cam_size": list(self.cam_size) if self.cam_size is not None else None,
            "cam_index": self.cam_index,
            "screen_dimension": self.screen_dimension,
            "cam_focus": self.cam_focus,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Calibration":
        """Reconstruct from the output of to_dict().

        Populated fields must be usable before readiness checks or motion.
        Raises ``ValueError`` for malformed data or an incompatible schema.
        Missing/None fields remain unset; pre-versioning bundles count as
        version 1 and unknown top-level fields are ignored.
        """
        if not isinstance(payload, dict):
            raise ValueError("calibration bundle must be an object")
        found = payload.get("version", 1)
        if type(found) is not int or found != BUNDLE_VERSION:
            raise ValueError(
                f"calibration bundle schema version {found} != "
                f"expected {BUNDLE_VERSION} — recalibrate"
            )
        cs = payload.get("cam_size")
        rotation = payload.get("cam_rotation")
        if rotation is not None and (
            type(rotation) is not int or rotation not in ROTATION_NAMES
        ):
            raise ValueError("cam_rotation must be -1, 0, 1 or 2")
        index = payload.get("cam_index")
        if index is not None and (type(index) is not int or index < 0):
            raise ValueError("cam_index must be a non-negative integer")
        focus = payload.get("cam_focus")
        if focus is not None and _finite_number(focus, "cam_focus") < 0:
            raise ValueError("cam_focus must be non-negative")
        dimension = payload.get("screen_dimension")
        if dimension is not None:
            if not isinstance(dimension, dict):
                raise ValueError("screen_dimension must be an object")
            for field in (
                "width",
                "height",
                "viewport_width",
                "viewport_height",
                "dpr",
            ):
                if (
                    field in dimension
                    and _finite_number(dimension[field], f"screen_dimension.{field}")
                    <= 0
                ):
                    raise ValueError(f"screen_dimension.{field} must be positive")
        return cls(
            viewport_shift=_load_viewport_shift(payload.get("viewport_shift")),
            cam_rotation=rotation,
            pct_to_grbl=_load_affine(payload.get("pct_to_grbl"), "pct_to_grbl"),
            pct_to_cam=_load_affine(payload.get("pct_to_cam"), "pct_to_cam"),
            cam_size=_positive_size(cs, "cam_size") if cs is not None else None,
            cam_index=index,
            screen_dimension=dimension,
            cam_focus=focus,
        )

    def save(self, path: Path = BUNDLE_PATH) -> None:
        """Write this bundle to disk as JSON — atomically (tmp + rename),
        so a kill or full disk mid-write can't truncate the previous good
        bundle and cost the operator a full re-calibration."""
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, self.to_dict())
        log.info(f"Saved calibration bundle → {path}")

    @classmethod
    def load(cls, path: Path = BUNDLE_PATH) -> "Calibration | None":
        """Return the bundle at ``path``, or None if missing/unreadable."""
        try:
            return cls.from_dict(json.loads(read_text(path)))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError) as e:
            log.warning(f"Failed to load calibration bundle from {path}: {e}")
            return None
