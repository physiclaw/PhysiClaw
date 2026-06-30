"""Tests for `physiclaw.core.platform.linux`.

The module only imports stdlib, so it imports and runs on any host — the
OS calls (`shutil.which`, `subprocess`) are mocked so nothing actually
launches.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from physiclaw.core.platform import linux


def test_trust_proxy_env_is_true() -> None:
    assert linux.TRUST_PROXY_ENV is True


def test_ensure_camera_permission_is_a_noop() -> None:
    # No prompt on Linux — must not raise and must not shell out.
    assert linux.ensure_camera_permission() is None


def test_local_hostname_strips_dns_suffix(monkeypatch) -> None:
    monkeypatch.setattr(linux.socket, "gethostname", lambda: "rig.local")
    assert linux.local_hostname() == "rig"


def test_local_hostname_returns_none_on_failure(monkeypatch) -> None:
    def boom() -> str:
        raise OSError("no hostname")

    monkeypatch.setattr(linux.socket, "gethostname", boom)
    assert linux.local_hostname() is None


def test_open_camera_aim_app_launches_first_available(monkeypatch) -> None:
    # cheese is missing, guvcview is present → guvcview is launched.
    monkeypatch.setattr(
        linux.shutil, "which", lambda app: app if app == "guvcview" else None
    )
    popen = MagicMock()
    monkeypatch.setattr(linux.subprocess, "Popen", popen)
    linux.open_camera_aim_app()
    popen.assert_called_once()
    assert popen.call_args[0][0] == ["guvcview"]


def test_open_camera_aim_app_noop_when_none_installed(monkeypatch) -> None:
    monkeypatch.setattr(linux.shutil, "which", lambda app: None)
    popen = MagicMock()
    monkeypatch.setattr(linux.subprocess, "Popen", popen)
    linux.open_camera_aim_app()
    popen.assert_not_called()


def test_quit_camera_aim_app_pkills_candidates(monkeypatch) -> None:
    run = MagicMock()
    monkeypatch.setattr(linux.subprocess, "run", run)
    monkeypatch.setattr(linux.time, "sleep", lambda _s: None)
    linux.quit_camera_aim_app()
    killed = {call.args[0][-1] for call in run.call_args_list}
    assert killed == set(linux._AIM_APPS)


def test_open_image_files_uses_xdg_open(monkeypatch) -> None:
    run = MagicMock()
    monkeypatch.setattr(linux.subprocess, "run", run)
    linux.open_image_files(["/tmp/a.jpg", "/tmp/b.jpg"])
    assert [call.args[0] for call in run.call_args_list] == [
        ["xdg-open", "/tmp/a.jpg"],
        ["xdg-open", "/tmp/b.jpg"],
    ]


def test_camera_denied_hint_mentions_video_group() -> None:
    assert "video" in linux.camera_denied_hint()


def test_camera_aim_hint_is_present() -> None:
    assert linux.camera_aim_hint()  # non-empty on Linux


def test_opencv_import_hint_only_for_libgl() -> None:
    assert linux.opencv_import_hint(ImportError("libGL.so.1: cannot open")) is not None
    assert linux.opencv_import_hint(ImportError("some other error")) is None


def test_hardware_permission_hints_warns_when_not_in_group(monkeypatch) -> None:
    # video + ttyUSB nodes exist; user is in neither 'video' nor 'dialout'.
    monkeypatch.setattr(
        linux.glob, "glob",
        lambda pat: ["/dev/video0"] if "video" in pat else ["/dev/ttyUSB0"],
    )
    monkeypatch.setattr(linux, "_in_group", lambda name: False)
    monkeypatch.setattr(linux, "_group_exists", lambda name: name == "dialout")
    hints = linux.hardware_permission_hints()
    assert any("video" in h for h in hints)
    assert any("dialout" in h for h in hints)


def test_hardware_permission_hints_silent_when_in_group(monkeypatch) -> None:
    monkeypatch.setattr(linux.glob, "glob", lambda pat: ["/dev/video0"])
    monkeypatch.setattr(linux, "_in_group", lambda name: True)
    monkeypatch.setattr(linux, "_group_exists", lambda name: True)
    assert linux.hardware_permission_hints() == []
