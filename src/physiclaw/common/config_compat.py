"""Known field changes from earlier releases; keep these when retiring settings."""

import os
from collections.abc import MutableMapping
from typing import Any

RENAMED_KEYS = {
    ("update", "auto"): "check",
    ("provider", "kimi_api_key"): "moonshot_api_key",
}

# Provider selection is migrated below where the old model is known. Memory
# now uses entry counts, and learned pitfalls are explicitly opt-in.
_REMOVED_KEYS = (
    ("provider", "name"),
    ("memory", "daily_lookback_days"),
    ("pitfalls", "capture_enabled"),
    ("pitfalls", "curate_enabled"),
)


def apply_compatibility(raw: MutableMapping[str, Any]) -> bool:
    """Normalize a dict or tomlkit document; current spellings always win."""
    changed = False
    provider = raw.get("provider")
    if isinstance(provider, MutableMapping) and provider.get("name") == "qwen":
        # Qwen was the only working API provider before explicit model refs.
        # Keep its historical model, including the old environment override.
        raw.setdefault("agent", {})
        agent = raw["agent"]
        if isinstance(agent, MutableMapping) and "model" not in agent:
            agent["model"] = "qwen/" + os.environ.get("QWEN_MODEL", "qwen3.6-plus")
            changed = True
    for (section, old), new in RENAMED_KEYS.items():
        table = raw.get(section)
        if not isinstance(table, MutableMapping) or old not in table:
            continue
        if new not in table:
            # tomlkit's [] unwraps bools, losing their inline comments.
            get_item = getattr(table, "item", None)
            table[new] = get_item(old) if callable(get_item) else table[old]
        del table[old]
        changed = True
    for section, key in _REMOVED_KEYS:
        table = raw.get(section)
        if isinstance(table, MutableMapping) and key in table:
            del table[key]
            changed = True
    if "claude" in raw:  # Removed Claude Code subprocess runtime.
        del raw["claude"]
        changed = True
    return changed
