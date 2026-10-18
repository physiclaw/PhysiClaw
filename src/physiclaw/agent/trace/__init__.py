"""Agent session logs: one self-contained directory per task.

`Trace` saves events, summary, notes and runtime diagnostics; `RawLog`
saves model requests and replies. Both share `store.Images`, so screenshots
are filed once and referenced relative to log/sessions/<sid>/.

`physiclaw logs` renders a readable narrative from events.jsonl and exports
complete session bundles. Format details ship in sessions/README.md and
all exports; retention removes whole session folders on bootstrap.
"""

from physiclaw.agent.trace.format import (
    brief,
    brief_args,
    brief_content,
    fmt_tokens,
    format_call_args,
    format_call_result,
    summarize_event,
)
from physiclaw.agent.trace.rawlog import RawLog
from physiclaw.agent.trace.store import SESSIONS_README, new_sid
from physiclaw.agent.trace.trace import Trace

__all__ = [
    "SESSIONS_README",
    "RawLog",
    "Trace",
    "brief",
    "brief_args",
    "brief_content",
    "fmt_tokens",
    "format_call_args",
    "format_call_result",
    "new_sid",
    "summarize_event",
]
