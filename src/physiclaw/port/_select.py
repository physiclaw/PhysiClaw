"""Select native backends once, without probing devices or opening resources."""

import sys

__all__ = ["device", "native"]

if sys.platform == "darwin":
    from physiclaw.port.backends import darwin as device
elif sys.platform == "win32":
    from physiclaw.port.backends import windows as device
elif sys.platform.startswith("linux"):
    from physiclaw.port.backends import linux as device
else:
    raise RuntimeError(f"Unsupported platform: {sys.platform}")

if sys.platform == "win32":
    from physiclaw.port.backends import windows as native
else:
    from physiclaw.port.backends import posix as native
