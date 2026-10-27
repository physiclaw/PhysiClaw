from pathlib import Path
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


@pytest.mark.parametrize(
    "selection,installed,expected",
    [
        ("chrome", ("chrome", "edge"), "chrome"),
        ("edge", ("chrome", "edge"), "edge"),
        ("chrome", ("edge",), None),
        ("edge", ("chrome",), None),
        ("chrome", ("chromium",), "chromium"),
    ],
)
def test_windows_browser_selection_does_not_fall_back_to_other_browser(
    tmp_path, monkeypatch, selection, installed, expected
):
    paths = {
        "chrome": tmp_path / "Google/Chrome/Application/chrome.exe",
        "edge": tmp_path / "Microsoft/Edge/Application/msedge.exe",
        "chromium": tmp_path / "Chromium/Application/chrome.exe",
    }
    for name in installed:
        paths[name].parent.mkdir(parents=True, exist_ok=True)
        paths[name].touch()
    monkeypatch.setattr(desktop, "_native", windows)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert desktop.find_browser(selection) == (
        str(paths[expected]) if expected else None
    )


def test_edge_selection_ignores_chrome_on_path(monkeypatch):
    executables = {"google-chrome": "/bin/chrome", "msedge": "/bin/edge"}
    monkeypatch.setattr(desktop, "_native", windows)
    monkeypatch.setattr(desktop.shutil, "which", executables.get)

    assert desktop.find_browser("edge") == "/bin/edge"


def test_macos_chrome_discovery(monkeypatch):
    monkeypatch.setattr(desktop, "_native", posix)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "is_file", lambda path: True)

    assert Path(desktop.find_browser("chrome")).name == "Google Chrome"


def test_posix_rejects_edge_before_searching_path(monkeypatch):
    finder = Mock(return_value="/bin/msedge")
    monkeypatch.setattr(desktop, "_native", posix)
    monkeypatch.setattr(desktop.shutil, "which", finder)

    with pytest.raises(ValueError, match="only on Windows"):
        desktop.find_browser("edge")

    finder.assert_not_called()


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
