"""Platform-specific helpers — single source of truth for OS branching.

Imports the right backend at import time and re-exports a flat API.
Callers do ``from physiclaw.core import platform`` and call
``platform.local_hostname()`` etc.; they never check ``sys.platform``
themselves. New helpers go in ``darwin.py`` + ``windows.py`` (matching
``sys.platform`` literals) and get re-exported here.
"""

import sys

if sys.platform == "darwin":
    from . import darwin as _impl
elif sys.platform == "win32":
    from . import windows as _impl
else:
    raise RuntimeError(
        f"PhysiClaw does not support sys.platform={sys.platform!r}. "
        "Supported: 'darwin', 'win32'."
    )

ensure_camera_permission = _impl.ensure_camera_permission
local_hostname = _impl.local_hostname
open_camera_aim_app = _impl.open_camera_aim_app
quit_camera_aim_app = _impl.quit_camera_aim_app
open_image_files = _impl.open_image_files
TRUST_PROXY_ENV = _impl.TRUST_PROXY_ENV

__all__ = [
    "ensure_camera_permission",
    "local_hostname",
    "open_camera_aim_app",
    "quit_camera_aim_app",
    "open_image_files",
    "TRUST_PROXY_ENV",
]
