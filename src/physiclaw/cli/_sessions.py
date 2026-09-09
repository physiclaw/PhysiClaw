"""Recorded-session addressing for the CLI — one convention, one home.

Every command that reads a recorded session (`logs`, `playbooks
replay`, `playbooks pages match/extract`, `studio --session`) takes the
id or any unique suffix of it, resolved against the engine's session
directory the same way.
"""

from physiclaw.cli._format import exit_error


def resolve_sid(suffix: str) -> str:
    """A session id from a unique suffix. Exits with the ambiguity, or
    with "no session matches", the way every CLI reader reports it."""
    from physiclaw.agent.trace.store import resolve_session
    from physiclaw.common import paths

    try:
        return resolve_session(paths.engine_sessions_dir(), suffix).name
    except LookupError as e:
        exit_error(str(e))
