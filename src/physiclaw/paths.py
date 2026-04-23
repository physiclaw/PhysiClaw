"""OS-standard user paths (macOS-first).

All persistent state the package reads or writes at runtime lives here —
calibration bundles, vision-model cache, logs, jobs, memory. Every path
is overridable with a matching ``PHYSICLAW_*`` env var for tests or
non-default installs.

Defaults on macOS:
  DATA_DIR   ~/Library/Application Support/physiclaw
  CACHE_DIR  ~/Library/Caches/physiclaw
  CONFIG_DIR ~/Library/Application Support/physiclaw
  LOG_DIR    ~/Library/Logs/physiclaw
"""

import os
from pathlib import Path

from platformdirs import (
    user_cache_dir,
    user_config_dir,
    user_data_dir,
    user_log_dir,
)

_APP = "physiclaw"


def _env_or(var: str, default: str) -> Path:
    raw = os.environ.get(var)
    return Path(raw).expanduser() if raw else Path(default)


DATA_DIR: Path = _env_or("PHYSICLAW_DATA_DIR", user_data_dir(_APP))
CACHE_DIR: Path = _env_or("PHYSICLAW_CACHE_DIR", user_cache_dir(_APP))
CONFIG_DIR: Path = _env_or("PHYSICLAW_CONFIG_DIR", user_config_dir(_APP))
LOG_DIR: Path = _env_or("PHYSICLAW_LOG_DIR", user_log_dir(_APP))


# --- specific resources ---

def model_cache() -> Path:
    """Root for downloaded vision models (OmniParser ONNX, OCR weights)."""
    return CACHE_DIR / "models"


def omniparser_onnx() -> Path:
    return model_cache() / "omniparser_icon_detect" / "model.onnx"


def calibration_bundle() -> Path:
    return DATA_DIR / "calibration" / "bundle.json"


def calibration_cache_dir() -> Path:
    return DATA_DIR / "calibration" / "cache"


def snapshots_dir() -> Path:
    return DATA_DIR / "snapshots"


def screenshots_dir() -> Path:
    return DATA_DIR / "screenshots"


def tool_calls_dir() -> Path:
    return DATA_DIR / "tool_calls"


def jobs_file() -> Path:
    return DATA_DIR / "jobs" / "jobs.md"


def memory_dir() -> Path:
    return DATA_DIR / "memory"


def claude_log_dir() -> Path:
    return LOG_DIR / "claude"


def ensure_dirs() -> None:
    """Create all user dirs if missing. Cheap — safe to call at import time."""
    for d in (DATA_DIR, CACHE_DIR, CONFIG_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
