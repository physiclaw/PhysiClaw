"""Native fonts and browser discovery for hardware documents and CAD."""

import shutil

from hardware.port._select import native as _native


def prepare_fonts() -> None:
    """Use the system font parser when compatible with the CAD library."""
    _native.load_system_fontconfig()


def find_chrome() -> str | None:
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
    ):
        if found := shutil.which(name):
            return found
    return next(
        (str(path) for path in _native.browser_locations() if path.is_file()), None
    )
