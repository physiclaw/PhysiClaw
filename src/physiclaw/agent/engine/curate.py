"""Optional post-session consolidation of learned pitfalls.

When pitfalls are enabled and `add_pitfall` was called (up to three items per
call, including an empty list), the engine invokes this pass on the saved list.
It reuses the session provider without tools. The curator can merge and rank
lesson text, but cannot verify it against the original observations.

`pitfalls.replace` caps the result and snapshots it for recovery. Provider or
parse errors leave the list untouched; curation never changes the task outcome.
"""

import logging
import time
from contextlib import suppress

from physiclaw.agent.engine import pitfalls
from physiclaw.common.config import CONFIG
from physiclaw.common.text import json_span
from physiclaw.contract.dto import USAGE_CALL_CURATE, SystemMessage, UserMessage

log = logging.getLogger(__name__)


def _system() -> str:
    cap = CONFIG.pitfalls.max_items
    return (
        "You curate a list of learned pitfalls — one-line traps an agent hit "
        "while driving phone apps, each `app: trap → avoid/fix`. The agent "
        "authors these mid-run, so quality varies: some are sharp and reusable, "
        "others are vague, over-specific to one screen, or restate the obvious. "
        "Your job is to consolidate the list and surface the best.\n"
        "Rules:\n"
        f"- Return AT MOST {cap} items — a hard cap, not a target. Fewer is fine.\n"
        "- ORDER BY QUALITY, best first: put the most durable, broadly-useful, "
        "actionable traps at the top; the weakest last. This ordering matters — "
        "if the list is trimmed to the cap, the lowest ones are dropped.\n"
        "- Merge near-duplicates into one line that keeps the nuance; when unsure "
        "whether two are the same trap, keep both (as separate lines).\n"
        "- Drop stale, vague, one-off, or low-signal entries rather than pad to "
        "the cap. Tighten wording; lead each line with the app; keep it terse.\n"
        "- Do NOT invent traps or add information not already present.\n"
        "Return ONLY a JSON array of strings, no prose, no code fence."
    )


def _parse(text: str) -> list[str] | None:
    """Parse the model's JSON array reply into a list of strings (the
    shared `json_span` idiom: fence/prose-tolerant outermost `[...]`).
    None on anything malformed, so the caller skips and the list is
    untouched."""
    arr = json_span(text or "", "[", "]")
    if not isinstance(arr, list):
        return None
    return [x for x in arr if isinstance(x, str) and x.strip()]


async def curate(provider, *, tr=None, rlog=None) -> bool:
    """Consolidate the pitfalls list in place. Returns True if it was rewritten.
    Fail-open: any error / unparseable / empty reply leaves the list as-is."""
    try:
        current = pitfalls.read()
        if not current:
            return False
        messages = [
            SystemMessage(content=_system()),
            UserMessage(content="\n".join(f"- {x}" for x in current)),
        ]
        turn = (tr.turn if tr is not None else None) or 0
        if rlog is not None:
            # Record before sending so a failed call keeps its input. Optional
            # diagnostics must never prevent consolidation or lose its result.
            with suppress(Exception):
                rlog.write_request(
                    turn, provider.serialize_history(messages), call=USAGE_CALL_CURATE
                )
        started = time.perf_counter()
        asst = await provider.chat(
            messages,
            [],
            purpose=USAGE_CALL_CURATE,
        )
        if rlog is not None:
            with suppress(Exception):
                rlog.write_response(
                    turn,
                    asst.raw,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    call=USAGE_CALL_CURATE,
                )
        items = _parse(asst.content or "")
        if not items:
            log.warning(
                "curate: unparseable/empty pitfalls reply — skipped",
                extra={
                    "console_message": "Could not consolidate learned lessons — existing lessons kept"
                },
            )
            return False
        res = pitfalls.replace(items)
        # Surface the change (never silent) — the pre-curate snapshot is one
        # history entry back, so a lossy consolidation is recoverable.
        log.info(
            "curate pitfalls: %d → %d (%d dropped over cap)",
            len(current),
            res["total"],
            res["dropped"],
        )
        if tr is not None:
            tr.write(
                {
                    "event": "curated_pitfalls",
                    "before": len(current),
                    "after": res["total"],
                    "dropped": res["dropped"],
                }
            )
        return True
    except Exception:
        log.exception(
            "curate: pitfalls pass failed (non-fatal)",
            extra={
                "console_message": "Lesson consolidation did not finish — see saved logs"
            },
        )
        return False
