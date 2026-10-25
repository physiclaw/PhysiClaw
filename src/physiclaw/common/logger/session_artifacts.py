"""Session artifact formats and I/O shared by the engine and its readers.

The engine writes per-session directories with a schema-v1 ``summary.json``.
This module owns the log directory guide, summary constructor, timestamps,
environment snapshots, atomic JSON writes, and image storage and resolution.
Event accumulation lives in ``agent.trace``.
"""

import base64
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Mapping

from physiclaw.common.logger.logger import _open_log_file
from physiclaw.common.text import read_text, write_text, write_text_atomic

log = logging.getLogger(__name__)


LOGS_README = """\
# PhysiClaw logs

Start with sessions/<session-id>/summary.json when debugging a task.
Use `physiclaw logs` to list sessions, `physiclaw logs <id>` to read one,
and `physiclaw logs <id> --save` to export its complete debug bundle.

## Directory layout

    log/
      cli.log                    CLI diagnostics
      server.log                 Hardware and MCP server diagnostics
      runtime.log                Agent diagnostics, including idle/startup errors
      studio.log                 Studio diagnostics
      archive/<service>.log.1    Older service logs; .1 is the newest archive
      session-stats.jsonl        One summary per completed agent session
      sessions/<session-id>/
        summary.json             Outcome, timings, model and token totals
        events.jsonl             Full task events, tool arguments and results
        model.jsonl              Raw model requests, responses and decision calls
        runtime.log              Complete agent diagnostics for this session
        server.log               Server diagnostics during this session
        notes.md                 Agent notes and intent
        images/                  Screenshots referenced by the JSONL files
      macros/macro-run-<id>/
        events.jsonl             Macro step records
        images/                  Macro screenshots

## Conventions

Service logs keep stable names; every record includes its date and time.
At retention.log_max_bytes (10 MiB by default), the current file moves to
archive/<service>.log.1. Older copies become .2, .3, etc.; at most
retention.log_backup_count (5 by default) are kept per service. Archives
also expire after retention.log_days, checked at startup and midnight.
Count and age are limits, not a guaranteed history window. Session files
are independent of service-log rotation. Hidden .<service>.log.lock files
coordinate concurrent writers; live process/session markers belong in run/.

Each session id is YYYYMMDD-HHMMSS-<6 hex digits>, safe on every platform.
Its suffix is a short handle accepted by `physiclaw logs` and Studio.
Session logs are not size-rotated; whole session folders expire according
to retention.trace_days. See sessions/README.md for the full artifact schema.
session-stats.jsonl survives session-folder retention.

JSON files are single documents; JSONL files contain one JSON record per line.
Text files use UTF-8 and LF newlines. Console summaries never replace saved
diagnostic messages. Session folders contain full prompts and phone images.
"""


def iso_now() -> str:
    """Local time as ISO with millisecond precision — the one timestamp
    format across the session artifacts (`started_at`/`ended_at`,
    events.jsonl / model.jsonl `t`). ms precision makes per-turn latency
    analysis possible without correlating against the process log."""
    return dt.datetime.now().isoformat(timespec="milliseconds")


def env_snapshot() -> dict[str, Any]:
    """The session's environment — versions, OS, rig identity, and the
    behavior-relevant config, captured once per session (OTel calls these
    resource attributes). This is what makes a shared session dir
    self-describing: "which rig / version / camera settings produced
    this?" without asking.

    Deliberately NOT the whole CONFIG — the provider section holds API
    keys. Only secret-free sections that vary across rigs and change
    what the agent sees and how it runs (camera/vision, image compaction,
    engine limits and retries)."""
    import dataclasses
    import platform as _platform
    import sys

    from physiclaw import __version__
    from physiclaw.common.config import CONFIG

    return {
        "physiclaw": __version__,
        "python": _platform.python_version(),
        "os": sys.platform,
        "platform": _platform.platform(),
        "host": _platform.node(),
        "utc_offset": dt.datetime.now().astimezone().strftime("%z"),
        "config": {
            "camera": dataclasses.asdict(CONFIG.camera),
            "vision": dataclasses.asdict(CONFIG.vision),
            "compact": dataclasses.asdict(CONFIG.compact),
            "engine": dataclasses.asdict(CONFIG.engine),
        },
    }


def write_json_atomic(path: Path, obj: dict[str, Any]) -> None:
    """Replace with one complete JSON document, even with concurrent writers.

    Each writer owns a sibling temporary file; the last rename wins.
    A failed write/rename leaves the previous document intact.
    """
    # ASCII JSON escapes preserve every code point, including lone surrogates.
    text = json.dumps(obj, indent=2, default=repr) + "\n"
    write_text_atomic(path, text)


def append_stats(log_dir: Path, summary: dict[str, Any]) -> None:
    """Append one session's summary as a line to `<log_dir>/session-stats.jsonl` —
    the durable per-session aggregate that outlives session-dir retention
    (the engine appends to the sweep-free `log/` root).
    Fail-open: losing a stats line must never cost the session close."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(summary, default=repr)
        with _open_log_file(log_dir / "session-stats.jsonl") as f:
            f.write(line + "\n")
    except (OSError, UnicodeError):
        log.warning("session-stats.jsonl append failed", exc_info=True)


def ensure_readme(dir: Path, content: str) -> None:
    """Keep the format doc at `<dir>/README.md` current — rewritten
    whenever the shipped constant changed, so existing installs don't
    keep documenting a retired format forever. Fail-open, cheap (one
    small read per session start)."""
    path = dir / "README.md"
    try:
        try:
            current = read_text(path)
        except (FileNotFoundError, UnicodeError):
            current = None
        if current != content:
            dir.mkdir(parents=True, exist_ok=True)
            write_text(path, content)
    except OSError:
        log.debug("log README write failed: %s", path, exc_info=True)


# mime → filename suffix for images extracted from data-URLs. Everything
# we actually serve is JPEG via common.image.scale_image_bytes, but keep the
# fallback open for PNG / WebP in case an upstream tool starts emitting
# them.
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def mime_for(name: str) -> str:
    """The mime a frame was filed under — `image_filename`'s inverse over
    the same table, so the reader and the writer cannot disagree; an
    unknown suffix (the `.bin` fallback) reads as octet-stream."""
    ext = Path(name).suffix
    return next(
        (m for m, e in _MIME_EXT.items() if e == ext), "application/octet-stream"
    )


def resolve_image(session_dir: Path, ref: object) -> Path | None:
    """Resolve a recorded ``images/<filename>`` to a regular file in that
    session's image directory. Logs and URL segments can be untrusted; reject
    traversal, Windows path syntax and symlinks escaping the image directory.
    Missing, malformed or unreadable references are not frames.
    """
    if not isinstance(ref, str) or not ref.startswith("images/"):
        return None
    name = ref.removeprefix("images/")
    if not name or name in {".", ".."} or any(c in name for c in "/\\:\0"):
        return None
    try:
        images = session_dir.resolve() / "images"
        image = (images / name).resolve()
        return image if image.is_relative_to(images) and image.is_file() else None
    except (OSError, ValueError, RuntimeError):  # includes invalid paths/symlink loops
        return None


def image_filename(turn: int, mime: str) -> str:
    """Name a captured screenshot `<HHMMSS>_<mmm>_t<turn>.<ext>`: a
    local-time stamp (hour-minute-second + milliseconds, so the names sort
    chronologically within a session) plus the turn that carried the image.
    `.bin` is the fallback for an unknown mime."""
    now = dt.datetime.now()
    ext = _MIME_EXT.get(mime, ".bin")
    return f"{now:%H%M%S}_{now.microsecond // 1000:03d}_t{turn}{ext}"


def save_image(img_dir: Path, turn: int, mime: str, b64: str) -> str | None:
    """Decode a base64 screenshot and write it to
    `<img_dir>/<image_filename(turn, mime)>`; returns the filename, or
    None on undecodable data. A failed write raises OSError — each
    caller picks its own fail-open policy (the engine's session store
    stubs the reference)."""
    try:
        raw = base64.b64decode(b64, validate=False)
    except (ValueError, TypeError):
        return None
    name = image_filename(turn, mime)
    path = img_dir / name
    # The stamp has millisecond resolution — two images in one
    # tool_result (or trace's per-request re-persist loop) can collide
    # within 1ms, and a silent overwrite loses a frame the model saw.
    stem, _, ext = name.rpartition(".")
    n = 1
    while True:
        try:
            with path.open("xb") as f:
                f.write(raw)
            return path.name
        except FileExistsError:
            path = img_dir / f"{stem}_{n}.{ext}"
            n += 1


# Session summaries include every error counter, using zero when absent.
_ERROR_KEYS = (
    "blocked_plan",
    "blocked_layout",
    "blocked_stuck",
    "invalid_args",
    "unknown_tool",
    "tool_errors",
    "correctives",
    "provider_failures",
)


def build_summary(
    *,
    sid: str,
    started_at: str,
    duration_s: float,
    model_ref: str,
    prompt_hash: str,
    triggers: list[dict],
    sentinel: str | None,
    recap: str,
    crashed: bool,
    turns: int,
    provider_calls: int,
    conductor_turns: int = 0,
    plugin_turns: int = 0,
    micro_calls: int = 0,
    decision_calls: int = 0,
    decision_time_ms: int = 0,
    provider_time_ms: int,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    new_tokens: int | None = None,
    by_model: Mapping[str, Mapping[str, int]] | None = None,
    tool_calls: Mapping[str, int],
    tool_time_ms: int | None = None,
    verdicts: Mapping[str, int] | None = None,
    walks: list[dict[str, Any]] | None = None,
    errors: Mapping[str, int],
    stuck_events: int,
    images: int,
    env: dict[str, Any],
) -> dict[str, Any]:
    """Build the schema-v1 summary, stamping ``ended_at`` at finalize time.

    Missing error counters render as zero. Optional timing, verdict, and walk
    fields are omitted when the writer has no corresponding data. Producer
    and inference counters are always present.
    """
    unknown = set(errors) - set(_ERROR_KEYS)
    if unknown:
        # A counter incremented somewhere without a matching _ERROR_KEYS
        # entry would otherwise silently never reach summary.json.
        log.warning("summary errors dropped unknown counter(s): %s", sorted(unknown))
    summary: dict[str, Any] = {
        "schema": 1,
        "sid": sid,
        "started_at": started_at,
        "ended_at": iso_now(),
        "duration_s": round(duration_s, 1),
        "model_ref": model_ref,
        "provider": model_ref.partition("/")[0],
        "prompt_hash": prompt_hash,
        "triggers": triggers,
        "outcome": {
            "sentinel": sentinel,
            "recap": recap,
            "crashed": crashed,
        },
        "turns": turns,
        "provider_calls": provider_calls,
        "conductor_turns": conductor_turns,
        "plugin_turns": plugin_turns,
        "micro_calls": micro_calls,
        "decision_calls": decision_calls,
        "decision_time_ms": decision_time_ms,
        "provider_time_ms": provider_time_ms,
        "tool_time_ms": tool_time_ms,
        "usage": {
            "input_tokens": input_tokens,
            # The full-price part of the input: what was neither read
            # from nor written to the cache — summed per call when the
            # caller has the calls (the engine), else derived from totals.
            "new_tokens": (
                new_tokens
                if new_tokens is not None
                else max(0, input_tokens - cache_read_tokens - cache_creation_tokens)
            ),
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_hit_pct": (
                round(100 * cache_read_tokens / input_tokens, 1)
                if input_tokens
                else 0.0
            ),
            # Per model: calls and the same buckets, so a
            # session mixing a decision tier with the main model can be
            # billed model by model — multiply each bucket by its rate.
            **(
                {"by_model": {m: dict(c) for m, c in by_model.items()}}
                if by_model is not None
                else {}
            ),
        },
        "tool_calls": dict(tool_calls),
        "verdicts": dict(verdicts) if verdicts is not None else None,
        # The conductor's walks, in order — app, playbook, outcome, the
        # node it ended at, and the reason (absent when no
        # walk ran, so "was a playbook used?" is one key away).
        "walks": list(walks) if walks is not None else None,
        "errors": {key: int(errors.get(key, 0)) for key in _ERROR_KEYS},
        "stuck_events": stuck_events,
        "images": images,
        "env": env,
    }
    for optional in (
        "tool_time_ms",
        "verdicts",
        "walks",
    ):
        if summary[optional] is None:
            del summary[optional]
    return summary
