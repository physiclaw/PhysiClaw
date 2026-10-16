"""Canonical session outcome statuses used by the engine and runtime."""

DONE = "DONE"
STUCK = "STUCK"
FAIL = "FAIL"
IDLE = "IDLE"
WAIT = "WAIT"
STATUSES: frozenset[str] = frozenset({DONE, STUCK, FAIL, IDLE, WAIT})
