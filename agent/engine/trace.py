"""Per-session jsonl trace for engine debugging.

One line per event: request / response / tool_result / sentinel / done.
Base64 image payloads are collapsed to a byte-count stub so the file stays
readable.
"""
import datetime as dt
import json
from pathlib import Path
from typing import Any

_LOG_DIR = Path("log/engine")


class Trace:
    def __init__(self, session_id: str):
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = _LOG_DIR / f"{session_id}.jsonl"
        self._f = open(self.path, "a")

    def write(self, event: dict[str, Any]) -> None:
        stamped = {"t": dt.datetime.now().isoformat(timespec="seconds"), **event}
        self._f.write(json.dumps(_safe(stamped), ensure_ascii=False) + "\n")
        self._f.flush()

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()


def _safe(obj: Any) -> Any:
    """Recursively collapse base64 image data so traces don't balloon."""
    if isinstance(obj, dict):
        # image_url block (OpenAI-style): data URI in image_url.url
        iu = obj.get("image_url")
        if isinstance(iu, dict) and isinstance(iu.get("url"), str) and iu["url"].startswith("data:"):
            head, _, data = iu["url"].partition(",")
            return {**obj, "image_url": {"url": f"{head},<{len(data)}b elided>"}}
        # MCP content block: {"type": "image", "data": "<base64>", ...}
        if obj.get("type") == "image" and isinstance(obj.get("data"), str):
            return {**obj, "data": f"<{len(obj['data'])}b elided>"}
        return {k: _safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe(x) for x in obj]
    return obj
