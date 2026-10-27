"""Native fonts and browser discovery for hardware documents and CAD."""

import shutil

from hardware.port._select import native as _native


def prepare_fonts() -> None:
    """Use the system font parser when compatible with the CAD library."""
    _native.load_system_fontconfig()


def find_chrome() -> str | None:
    """Keep automatic Chromium-family discovery for existing callers."""
    return find_browser()


def find_browser(browser: str | None = None) -> str | None:
    """Find the selected browser, or use legacy automatic discovery."""
    if browser not in (None, "chrome", "edge"):
        raise ValueError(f"Unsupported browser: {browser}")
    locations = _native.browser_locations(browser)
    names = (
        ("msedge",)
        if browser == "edge"
        else (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "chrome",
        )
    )
    for name in names:
        if found := shutil.which(name):
            return found
    return next(
        (str(path) for path in locations if path.is_file()),
        None,
    )
