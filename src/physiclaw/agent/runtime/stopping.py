"""Cooperative stop checkpoints in the runtime's owning task.

Finish the current awaited operation, then unwind without scheduling another
model/tool call or retry. A stuck operation is bounded by the parent process.
"""

import asyncio
from contextvars import ContextVar

stop_event: ContextVar[asyncio.Event | None] = ContextVar(
    "runtime_stop_event", default=None
)


def checkpoint() -> None:
    event = stop_event.get()
    if event is not None and event.is_set():
        # Raise in this task, without cancelling the SDK's enclosing AnyIO
        # scopes. The launcher closes those scopes in their original order.
        raise asyncio.CancelledError("runtime stop requested")
