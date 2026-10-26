from hardware.port import desktop
from hardware.port.backends import windows


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
