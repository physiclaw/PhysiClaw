"""PhysiClaw's single data root.

All persistent state — calibration, memory, jobs, model cache, logs —
lives under ``~/.physiclaw/``. Override with ``PHYSICLAW_HOME`` (read once
at import, so set it *before* the first ``import physiclaw`` in tests).

Layout::

    ~/.physiclaw/
    ├── calibration/{bundle.json, cache/}
    ├── memory/{memory.md, OWNER.md, YYYY-MM-DD.md}
    ├── jobs/jobs.md
    ├── models/omniparser_icon_detect/model.onnx
    ├── snapshots/, screenshots/, tool_calls/
    └── log/{claude/, engine/}
"""

import os
from pathlib import Path

HOME: Path = Path(os.environ.get("PHYSICLAW_HOME", "~/.physiclaw")).expanduser()
LOG_DIR: Path = HOME / "log"


def ensure_dirs() -> None:
    """Create HOME and LOG_DIR. Safe to call repeatedly."""
    HOME.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def model_cache() -> Path:
    return HOME / "models"


def omniparser_onnx() -> Path:
    return model_cache() / "omniparser_icon_detect" / "model.onnx"


def calibration_bundle() -> Path:
    return HOME / "calibration" / "bundle.json"


def calibration_cache_dir() -> Path:
    return HOME / "calibration" / "cache"


def snapshots_dir() -> Path:
    return HOME / "snapshots"


def screenshots_dir() -> Path:
    return HOME / "screenshots"


def tool_calls_dir() -> Path:
    return HOME / "tool_calls"


def jobs_file() -> Path:
    return HOME / "jobs" / "jobs.md"


def memory_dir() -> Path:
    return HOME / "memory"


def claude_log_dir() -> Path:
    return LOG_DIR / "claude"


def engine_log_dir() -> Path:
    return LOG_DIR / "engine"
