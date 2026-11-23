"""Offline PhysiClaw workflow scenario."""

HOST = "127.0.0.1"
MODEL = "dryrun"
KEY = "physiclaw-dryrun-local"
CAPTURE_SIZE = (960, 720)
PHONE_ACTIONS = (
    "peek",
    "tap",
    "update_progress",
    "tap",
    "create_job",
    "sequence",
    "append_log",
    "read_logs",
    "tap",
    "sequence",
    "go_back",
    "home_screen",
    "update_progress",
    "end_session",
)
DAILY_ACTIONS = (
    "peek",
    "tap",
    "append_log",
    "home_screen",
    "finish_job",
    "end_session",
)


class DryRunError(RuntimeError):
    """A scenario step failed or did not produce the expected result."""
