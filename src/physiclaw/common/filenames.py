"""Portable filesystem identities, independent of the host OS."""

_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³"}
)


def is_portable_name(name: str) -> bool:
    """Reject Windows aliases even when authoring a name on a POSIX host."""
    return bool(
        name
        and not name.endswith((".", " "))
        and not any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name)
        and name.partition(".")[0].rstrip(" ").upper() not in _DEVICES
    )
