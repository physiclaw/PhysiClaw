"""Tests for `physiclaw.port` — import-time dispatch.

The right backend is bound at import time based on ``sys.platform``;
the package re-exports a flat API that callers use without checking
the platform themselves.
"""

from __future__ import annotations

import sys

from physiclaw import port as platform


def test_dispatch_binds_correct_backend_for_this_os() -> None:
    if sys.platform == "darwin":
        from physiclaw.port.backends import darwin as expected
    elif sys.platform == "win32":
        from physiclaw.port.backends import windows as expected
    elif sys.platform.startswith("linux"):
        from physiclaw.port.backends import linux as expected
    else:  # pragma: no cover — package import would already have raised
        return
    assert platform.ensure_camera_permission is expected.ensure_camera_permission
    assert platform.local_hostname is expected.local_hostname
    assert platform.TCP_REUSE_ADDRESS == expected.TCP_REUSE_ADDRESS
    assert platform.check_tcp_port is expected.check_tcp_port
    assert platform.open_camera_aim_app is expected.open_camera_aim_app
    assert platform.quit_camera_aim_app is expected.quit_camera_aim_app
    assert platform.open_image_files is expected.open_image_files
    assert platform.TRUST_PROXY_ENV == expected.TRUST_PROXY_ENV


def test_public_api_surface_is_exhaustive() -> None:
    # Every entry in __all__ must be defined (callable or constant).
    for name in platform.__all__:
        assert hasattr(platform, name), name


def test_all_backends_export_the_same_api() -> None:
    from physiclaw.port.backends import darwin, linux, windows

    public = set(platform.__all__)
    for module in (darwin, linux, windows):
        missing = [n for n in public if not hasattr(module, n)]
        assert not missing, f"{module.__name__} missing {missing}"
