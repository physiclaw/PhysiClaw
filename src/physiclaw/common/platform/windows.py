"""Windows implementations of platform-specific helpers.

Imported by ``physiclaw.common.platform`` on win32 only. Callers should
never import this module directly — go through ``physiclaw.common.platform``.
"""

import math
import os
import socket
import subprocess
import time
from typing import TextIO

# Windows registry's ProxyOverride is unreliable for loopback bypass —
# corporate ProxyOverride often lacks `<local>` / `localhost`, and an
# `HTTP_PROXY` env var bypasses no-proxy rules entirely. Tell urllib /
# httpx to skip system proxy when calling our own server.
TRUST_PROXY_ENV = False


def enable_terminal_color(stream: TextIO | None) -> bool:
    """Enable VT output on the actual stream handle; fall back to plain text."""
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        if stream is None:
            return False
        handle = getattr(msvcrt, "get_osfhandle")(stream.fileno())
        kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        kernel.GetConsoleMode.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.GetConsoleMode.restype = wintypes.BOOL
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.SetConsoleMode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            # ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING.
            return bool(
                mode.value & 5 == 5 or kernel.SetConsoleMode(handle, mode.value | 5)
            )
    except (ImportError, AttributeError, OSError, ValueError):
        pass
    # MSYS/Cygwin terminals expose ANSI through a PTY rather than a Win32 console.
    return (
        os.environ.get("TERM", "")
        .lower()
        .startswith(("xterm", "screen", "tmux", "vt100", "ansi"))
    )


def ensure_camera_permission() -> None:
    """Windows camera access is managed through system privacy settings."""


def local_hostname() -> str | None:
    """Return the short hostname suitable for ``<name>.local`` mDNS, or None.

    Windows doesn't have a separate Bonjour ``LocalHostName`` concept; the
    Bonjour-for-Windows service publishes ``socket.gethostname()`` as-is.
    """
    try:
        return socket.gethostname().split(".")[0] or None
    except Exception:
        return None


def open_camera_aim_app() -> None:
    """Open the built-in Camera app so the user can position the phone."""
    # `start` is a cmd builtin (not an exe); the empty "" is the window
    # title that `start` consumes when the first arg is quoted.
    subprocess.run(
        ["cmd", "/c", "start", "", "microsoft.windows.camera:"],
        capture_output=True,
    )


def quit_camera_aim_app() -> None:
    """Close the Camera app so MediaFoundation releases the camera.

    Without the close + 0.5s settle, the next ``Camera(...)`` open can hit
    a still-held device handle.
    """
    subprocess.run(
        ["taskkill", "/F", "/IM", "WindowsCamera.exe"],
        capture_output=True,
    )
    time.sleep(0.5)


def open_image_files(paths: list[str]) -> None:
    """Open one or more image files in the user's default viewer."""
    for p in paths:
        try:
            os.startfile(p)  # type: ignore[attr-defined]
        except OSError:
            pass


# ─── camera exposure ────────────────────────────────────────
#
# cv2 is imported lazily inside each function: the doctor CLI imports this
# package on its cv2-import-failure path, so a top-level import would mask
# the very error it reports.


def camera_exposure_tunable() -> bool:
    """DirectShow exposes exposure control via IAMCameraControl."""
    return True


def camera_size_cap() -> tuple[int, int] | None:
    """No cap — exposure is always tunable here, so any capture mode a
    bad firmware AE picks can be corrected by `exposure.converge`."""
    return None


def camera_backend() -> int:
    """Use DirectShow's current-value reads and focus-preserving AF toggle.

    MSMF's readComplexProperty overwrites the current value with GetRange's
    default, and its AF-off writes focus 0 (OpenCV 4.14.0 cap_msmf.cpp).
    DirectShow keeps current/default separate and switches to manual at the
    current focus (cap_dshow.cpp). Pixel verification still guards drivers.
    """
    import cv2

    return cv2.CAP_DSHOW


def camera_reset_controls() -> None:
    """Controls belong to the capture handle; reopening it resets them."""


def camera_set_auto_exposure(cap) -> None:
    """Ask the driver for firmware auto-exposure.

    DirectShow maps CAP_PROP_AUTO_EXPOSURE 1 → Auto, 0 → Manual
    (cap_dshow.cpp). set()/get() are unreliable across drivers —
    verification is measured frame brightness, never the return value."""
    import cv2

    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)


def camera_set_manual_exposure(cap, exposure: int) -> None:
    """Hold a fixed exposure. Value is log2 seconds per the DirectShow
    CameraControl_Exposure spec (-6 ≈ 1/64s; indoors -4..-8) — though
    drivers may deviate, which the caller's measured-brightness stall
    check catches. Setting CAP_PROP_EXPOSURE already carries the Manual
    mode after the explicit AE-off through DirectShow."""
    import cv2

    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0)
    cap.set(cv2.CAP_PROP_EXPOSURE, exposure)


def camera_focus_lockable() -> bool:
    """DirectShow maps CAP_PROP_AUTOFOCUS onto the CameraControl_Focus flags —
    the lock is always worth ATTEMPTING here. Whether this camera honors
    it is judged at lock time: the set() return, then (as always)
    measured frame sharpness."""
    return True


def camera_lock_focus(cap) -> bool:
    """DirectShow reads the current position and writes it in manual mode.

    A failed driver read returns False without writing. Let that backend
    operation freeze the lens; calibration verifies it by pixels and proves
    the read-back/reapply path separately before persisting the position.
    """
    import cv2

    return bool(cap.set(cv2.CAP_PROP_AUTOFOCUS, 0))


def camera_unlock_focus(cap) -> None:
    """Hand the lens back to continuous autofocus."""
    import cv2

    cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)


def camera_read_focus(cap) -> float | None:
    """Current absolute focus position, or None when the driver reports
    none. get() returns 0.0 when CAP_PROP_FOCUS is unsupported and 0
    is also a legal focus value — <= 0 reads as "no usable position"."""
    import cv2

    cur = cap.get(cv2.CAP_PROP_FOCUS)
    return cur if math.isfinite(cur) and cur > 0 else None


def camera_apply_focus(cap, value: float) -> bool:
    """Drive the lens to a caller-supplied absolute position. AF off
    first, then the absolute write — DirectShow maps both onto
    CameraControl_Focus."""
    import cv2

    if not math.isfinite(value) or value < 0:
        return False
    if not cap.set(cv2.CAP_PROP_AUTOFOCUS, 0):
        return False
    return bool(cap.set(cv2.CAP_PROP_FOCUS, value))


# ─── doctor diagnostics ─────────────────────────────────────


def camera_denied_hint() -> str:
    """Guidance when a camera opens but yields no frame, or none are detected."""
    return (
        "close Windows Camera and other camera apps; check Settings → "
        "Privacy & security → Camera, enable Camera access and allow "
        "desktop apps to access the camera"
    )


def camera_aim_hint() -> str | None:
    """No extra instruction needed — ``open_camera_aim_app`` launches the
    built-in Camera app on Windows."""
    return None


def opencv_import_hint(_exc: ImportError) -> str | None:
    """The Windows OpenCV wheel is self-contained — no system-lib remediation."""
    return None


def hardware_permission_hints() -> list[str]:
    """Windows surfaces device-access prompts itself — no group advice."""
    return []
