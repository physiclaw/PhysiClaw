"""Debug wake hook — the e2e harness's trigger, one-shot and gated.

Fires when `debug/wake.json` exists AND the server runs in debug mode
(`physiclaw debug` exports DEBUG_ENV_VAR; this runtime inherits it) —
the file alone must never wake a production runtime. Atomically claimed
before reading, so another wake arriving during the read stays pending.
A claimed file is never replayed, even after an unreadable payload or a
crash. The `physiclaw debug` CLI writes it; the wake then flows
through the same hook loop, ready gate, and session lifecycle as a real
phone trigger.
"""

import json
import logging
import os
from uuid import uuid4

from physiclaw.agent.runtime.hook import Trigger, register
from physiclaw.common import paths
from physiclaw.common.config import DEBUG_ENV_VAR
from physiclaw.common.text import read_text
from physiclaw.port.locks import FileLockBusy, exclusive_file_lock

log = logging.getLogger(__name__)


def wake_path():
    return paths.debug_dir() / "wake.json"


@register
def debug_wake() -> Trigger | None:
    if not os.environ.get(DEBUG_ENV_VAR):
        return None
    p = wake_path()
    if not p.exists():
        return None
    claimed = p.with_name(f".{p.name}.{uuid4().hex}.claimed")
    try:
        # Concurrent MoveFileEx calls on Windows can open the same source
        # before either rename finishes. Serialize claims so exactly one
        # consumer owns the file; release before reading to admit new wakes.
        with exclusive_file_lock(p.with_suffix(".claim.lock")):
            p.replace(claimed)
    except (FileNotFoundError, FileLockBusy):
        return None  # another consumer or CLI clear already took this wake
    description = "debug wake"
    try:
        value = json.loads(read_text(claimed)).get("description")
        if isinstance(value, str) and value.strip():
            description = value
    except Exception:
        log.warning("debug wake file unreadable — waking with the default reason")
    finally:
        try:
            claimed.unlink(missing_ok=True)
        except OSError as exc:
            # The claim is no longer a wake; cleanup failure must not lose
            # the trigger or touch a new wake published at the original path.
            log.warning("could not remove consumed debug wake: %s", exc)
    log.info("debug wake fired: %s", description)
    return Trigger(description=description, source="debug")
