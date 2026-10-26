"""Find installed Chromium-family browsers without launching a user application."""

import shutil

from hardware.port._select import native as _native


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
