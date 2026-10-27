from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hardware.port import desktop
from hardware.port.backends import posix, windows


def test_windows_browser_discovery_uses_install_directory(tmp_path, monkeypatch):
    browser = tmp_path / "Microsoft/Edge/Application/msedge.exe"
    browser.parent.mkdir(parents=True)
    browser.touch()
    monkeypatch.setattr(desktop, "_native", windows)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    found = desktop.find_chrome()

    assert found == str(browser)


@pytest.fixture
def fontconfig_cache(monkeypatch):
    posix.load_system_fontconfig.cache_clear()
    yield
    posix.load_system_fontconfig.cache_clear()


@pytest.mark.parametrize(
    "version, compatible", [(21300, False), (21301, True), (21803, True), (None, False)]
)
def test_fontconfig_uses_compatible_system_library_once(
    version, compatible, fontconfig_cache, monkeypatch
):
    monkeypatch.setattr(posix.sys, "platform", "linux")
    library = SimpleNamespace(FcGetVersion=Mock(return_value=version))
    loader = Mock(return_value=library)
    if version is None:
        loader.side_effect = OSError("library not found")
    monkeypatch.setattr(posix.ctypes, "CDLL", loader)
    expected = library if compatible else None
    assert posix.load_system_fontconfig() is expected
    assert posix.load_system_fontconfig() is expected
    if expected is library:
        assert loader.call_count == 2  # Probe locally, then export symbols.
        loader.assert_called_with("libfontconfig.so.1", mode=posix.ctypes.RTLD_GLOBAL)
    else:
        loader.assert_called_once_with("libfontconfig.so.1")


@pytest.mark.parametrize("platform, backend", [("darwin", posix), ("win32", windows)])
def test_non_linux_font_setup_does_not_load_fontconfig(
    platform, backend, fontconfig_cache, monkeypatch
):
    monkeypatch.setattr(posix.sys, "platform", platform)
    monkeypatch.setattr(desktop, "_native", backend)
    loader = Mock(side_effect=AssertionError("Fontconfig is Linux-only"))
    monkeypatch.setattr(posix.ctypes, "CDLL", loader)
    desktop.prepare_fonts()
    loader.assert_not_called()
