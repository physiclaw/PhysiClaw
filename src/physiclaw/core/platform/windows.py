"""Windows implementations of platform-specific helpers.

Imported by ``physiclaw.core.platform`` on win32 only. Callers should
never import this module directly — go through ``physiclaw.core.platform``.
"""

import os
import socket
import subprocess
import time

# Windows registry's ProxyOverride is unreliable for loopback bypass —
# corporate ProxyOverride often lacks `<local>` / `localhost`, and an
# `HTTP_PROXY` env var bypasses no-proxy rules entirely. Tell urllib /
# httpx to skip system proxy when calling our own server.
TRUST_PROXY_ENV = False


def ensure_camera_permission() -> None:
    """No-op on Windows — MediaFoundation surfaces the camera-access prompt
    itself when the device is opened."""


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


# ─── doctor diagnostics ─────────────────────────────────────


def camera_denied_hint() -> str:
    """Guidance when a camera opens but yields no frame, or none are detected."""
    return (
        "camera access may be off — Settings → Privacy & security → Camera, "
        "and allow desktop apps to access the camera"
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
