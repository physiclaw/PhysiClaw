"""Choose the native implementation once; do not probe or open resources."""

import sys

__all__ = ["native"]

if sys.platform == "win32":
    from hardware.port.backends import windows as native
else:
    from hardware.port.backends import posix as native
