"""Phone-control stack, independent of the agent's reasoning loop.

Start with ``server.app.build_server`` for assembly or ``PhysiClaw`` for
the action API. Responsibilities follow the request through these layers:

* ``server`` validates HTTP/MCP requests and wires the two network planes.
* ``orchestration`` coordinates actions and observations. ``HardwareRig``
  owns devices, calibration and the lock; ``Perception`` acquires views.
* ``hardware`` implements device protocols and physical motion.
* ``calibration`` measures and persists coordinate mappings; ``geometry``
  supplies the shared coordinate math.
* ``bridge`` owns phone-page state, screenshot uploads and clipboard handoff.
* ``vision`` processes images without owning the hardware lifecycle.

Gesture calls hold ``rig.engaged()`` through action, observation and parking.
Setup/calibration hold ``rig.locked()`` because they run before the hardware
is ready. Reject invalid inputs before acquiring devices or pressing the tip.
"""

__all__ = ["PhysiClaw"]


def __getattr__(name: str):
    # Same lazy-import trick as `physiclaw/__init__.py` — touching the
    # class is the only thing that should pay the cv2/numpy/pyserial cost.
    if name == "PhysiClaw":
        from physiclaw.core.orchestration import PhysiClaw as _P

        return _P
    raise AttributeError(f"module 'physiclaw.core' has no attribute {name!r}")
