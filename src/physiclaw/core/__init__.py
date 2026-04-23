"""PhysiClaw core — phone-control stack (hardware, vision, MCP server)."""

__all__ = ["PhysiClaw"]


def __getattr__(name: str):
    # Same lazy-import trick as `physiclaw/__init__.py` — touching the
    # class is the only thing that should pay the cv2/numpy/pyserial cost.
    if name == "PhysiClaw":
        from physiclaw.core.orchestration import PhysiClaw as _P

        return _P
    raise AttributeError(f"module 'physiclaw.core' has no attribute {name!r}")
