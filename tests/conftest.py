"""Top-level pytest fixtures and import-time isolation.

Two non-obvious pieces:

1. **`PHYSICLAW_HOME` is read once at `physiclaw.common.paths` import time** (line:
   `HOME = Path(os.environ.get("PHYSICLAW_HOME", "~/.physiclaw")).expanduser()`).
   We set it at module load, before any test or fixture runs, so the first
   `import physiclaw` resolves `paths.HOME` to a session-scoped tmp dir
   instead of the real `~/.physiclaw`.

2. **Per-test isolation** — the autouse `physiclaw_home` fixture re-points
   `paths.HOME` and `paths.LOG_DIR` to a per-test tmp dir, so tests that
   write under HOME don't bleed state across each other.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path

import pytest


def pytest_make_parametrize_id(config, val, argname):
    """Keep large payloads out of Windows' bounded per-variable environment."""
    if isinstance(val, str) and len(val) > 256:
        digest = hashlib.sha256(
            val.encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:12]
        return f"{argname}-{len(val)}chars-{digest}"
    return None


@pytest.fixture
def observation_blocks():
    """Fresh, runtime-shaped image/listing pairs with real encoded pixels."""
    import base64

    import cv2
    import numpy as np

    from physiclaw.common.listing import Element, format_elements
    from physiclaw.contract.observation import OBSERVATION_META, view_metadata

    ok, encoded = cv2.imencode(".jpg", np.zeros((120, 80, 3), dtype=np.uint8))
    assert ok
    jpeg = encoded.tobytes()

    def make(*, source="camera", listing=None):
        if listing is None:
            listing = format_elements(
                [
                    Element(0, "icon", "", (0.1, 0.2, 0.3, 0.4), 0.87),
                    Element(1, "text", "搜索", (0.4, 0.2, 0.8, 0.4), 0.93),
                ]
            )
        return [
            {
                "type": "image",
                "mime_type": "image/jpeg",
                "data": base64.b64encode(jpeg).decode(),
                "_meta": {
                    OBSERVATION_META: view_metadata(jpeg, listing, source=source)
                },
            },
            {"type": "text", "text": listing},
        ]

    return make


# ---- module-load isolation (must run before any `import physiclaw`) ----

_SESSION_HOME = Path(tempfile.mkdtemp(prefix="physiclaw-test-session-"))
os.environ["PHYSICLAW_HOME"] = str(_SESSION_HOME)


# ---- per-test fixtures ----


@pytest.fixture(autouse=True)
def _restore_logging():
    """Process-entry tests must not leave sinks or a DEBUG root in later tests."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        yield
    finally:
        for handler in root.handlers:
            if handler not in handlers and getattr(
                handler, "_physiclaw_managed", False
            ):
                handler.close()
        root.handlers = handlers
        root.setLevel(level)


@pytest.fixture(autouse=True)
def _no_real_uvc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit tests off the physical camera and host-independent.

    On macOS the darwin exposure setters reach the real camera over USB
    (IOKit control requests), and `camera_size_cap` depends on what the
    probe finds on the host. Pin the probe to "unavailable" and the cap
    to None so camera tests behave identically on every machine. The
    combination is deliberately unrealistic (on a real darwin rig
    "not tunable" implies the 1080p cap): "unavailable" is the hardware
    guard, "no cap" is the resolution default that keeps CONFIG-sized
    tests platform-independent. Tests of the probe/cap logic patch
    their own state on top."""
    import sys

    if sys.platform == "darwin":
        from physiclaw.port.backends import darwin

        monkeypatch.setattr(darwin, "_uvc_terminal", False)
    from physiclaw import port as _platform

    monkeypatch.setattr(_platform, "camera_size_cap", lambda: None)
    monkeypatch.setattr(_platform, "camera_reset_controls", lambda: None)


@pytest.fixture(autouse=True)
def physiclaw_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test isolated `~/.physiclaw`. Re-points `paths.HOME` / `paths.LOG_DIR`.

    Yields the path so tests that need to inspect on-disk state can.
    """
    home = tmp_path / "physiclaw_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PHYSICLAW_HOME", str(home))

    # `paths` was imported once at session start with the session tmp dir;
    # patch the live module attributes so callers in this test see the
    # per-test path.
    from physiclaw.common import paths as _paths

    monkeypatch.setattr(_paths, "HOME", home)
    monkeypatch.setattr(_paths, "LOG_DIR", home / "log")
    return home


@pytest.fixture(autouse=True)
def scrub_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip proxy env vars so the suite is hermetic on proxied shells.

    A VPN client's `all_proxy=socks://…` leaking in from the developer's
    shell once produced 97 bogus failures: every test constructing a
    `trust_env=True` httpx client (providers, mcp_tool, poll) died in
    the constructor on the unsupported scheme. Tests that exercise proxy
    behavior set the vars they need via monkeypatch, which runs after
    this scrub.
    """
    for key in list(os.environ):
        if key.lower() in ("all_proxy", "http_proxy", "https_proxy", "no_proxy"):
            monkeypatch.delenv(key)


@pytest.fixture
def silenced_log() -> None:
    """Suppress all `physiclaw.*` logger output for the duration of a test.

    Use when a unit test deliberately exercises an error path that emits
    `log.warning` / `log.error` and the noise drowns the test report.
    Don't apply blindly — silence by exception, not by default.
    """
    physiclaw_logger = logging.getLogger("physiclaw")
    prior_level = physiclaw_logger.level
    physiclaw_logger.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        physiclaw_logger.setLevel(prior_level)
