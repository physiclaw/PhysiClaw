"""Windows implementations of platform-specific helpers.

Imported by ``physiclaw.port`` on win32 only. Callers should
never import this module directly — go through ``physiclaw.port``.
"""

import logging
import math
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import BinaryIO, TextIO

log = logging.getLogger(__name__)

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
            getattr(os, "startfile")(p)
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
    """No platform size cap; capture negotiation supplies fallbacks."""
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
    """No separate native control cache to clear.

    Device settings can survive reopening; configure_capture reapplies the
    requested settings instead of relying on a driver reset.
    """


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
    check catches. The AE-off toggle explicitly selects Manual; the following
    absolute write passes flags=0 in OpenCV, so driver support still matters.
    """
    import cv2

    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0)
    cap.set(cv2.CAP_PROP_EXPOSURE, exposure)


def camera_focus_lockable() -> bool:
    """DirectShow exposes the AF toggle, so a lock can be attempted.

    Calibration checks driver acceptance and sharpness separately.
    """
    return True


def camera_lock_focus(cap) -> bool:
    """DirectShow reads the current position and writes it in manual mode.

    A failed driver read returns False without writing. Let that backend
    operation request the freeze; calibration checks sharpness and the
    read-back/reapply path separately before persisting the position.
    A successful write and sharp frame cannot prove a driver honored AF-off.
    """
    import cv2

    # OpenCV's setter takes 0/1. Its DirectShow getter instead returns raw
    # CameraControlFlags (Auto=1, Manual=2), which must not be read as bool.
    return bool(cap.set(cv2.CAP_PROP_AUTOFOCUS, 0))


def camera_unlock_focus(cap) -> None:
    """Request continuous autofocus; restoration is best-effort."""
    import cv2

    if not cap.set(cv2.CAP_PROP_AUTOFOCUS, 1):
        log.warning(
            "DirectShow: could not re-enable autofocus; lens state is unknown",
            extra={
                "console_message": "Camera could not re-enable autofocus — lens state is unknown"
            },
        )


def camera_read_focus(cap) -> float | None:
    """Current absolute focus position, including zero.

    OpenCV's DirectShow backend returns -1 when CAP_PROP_FOCUS is unavailable
    (cap_dshow.cpp, OpenCV 4.10–4.14), unlike VideoCapture's generic 0 fallback.
    """
    import cv2

    cur = cap.get(cv2.CAP_PROP_FOCUS)
    return cur if math.isfinite(cur) and cur >= 0 else None


def camera_apply_focus(cap, value: float) -> bool:
    """Request AF-off, then an absolute position, propagating either refusal.

    OpenCV's AF toggle explicitly writes Manual, but its CAP_PROP_FOCUS write
    passes flags=0. Some drivers may reject that write even after AF-off;
    callers must restore AF on failure and verify calibration sharpness.
    """
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


# Native desktop/process/filesystem mechanisms. Workflow policy stays with the caller.
REMOTE_SHUTDOWN_SUPPORTED = False
EDITOR_CANDIDATES = ("notepad",)


def editor_command(editor: str, filename: str) -> str:
    # Popen passes a string to CreateProcess unchanged when shell=False.
    # Preserve the user's Windows quoting; quote only the appended filename.
    return f"{editor} {subprocess.list2cmdline([filename])}"


def process_group_options() -> dict:
    return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP")}


def prepare_managed_runtime() -> None:
    """Load NumPy before a thread reads piped stdin (numpy/numpy#24290)."""
    import numpy  # noqa: F401


def pid_alive(pid: int) -> bool:
    """Query a process handle; os.kill(pid, 0) can terminate it on Windows."""
    import ctypes
    from ctypes import wintypes

    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if handle:
        kernel.CloseHandle(handle)
        return True
    return getattr(ctypes, "get_last_error")() == 5


def request_shutdown(pid: int) -> None:
    raise NotImplementedError("Remote graceful shutdown is unavailable on Windows")


def native_path(path: Path) -> Path:
    name = str(path)
    if name.startswith("\\\\?\\"):
        return path
    if name.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + name[2:])
    return Path("\\\\?\\" + name)


def replace_file(source: Path, target: Path) -> None:
    """Retry only transient Windows sharing/access errors, within 200 ms."""
    for attempt in range(41):
        try:
            source.replace(target)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 40:
                raise
            time.sleep(0.005)


def lock_stream(stream: BinaryIO) -> None:
    import importlib

    msvcrt = importlib.import_module("msvcrt")
    if stream.tell() == 0:
        stream.write(b"\0")
        stream.flush()
    stream.seek(0)
    try:
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        raise BlockingIOError(str(exc)) from exc
