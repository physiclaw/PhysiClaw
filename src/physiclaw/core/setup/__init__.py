"""Server-side hardware setup, independent of CLI and browser clients.

The *_routes modules register HTTP endpoints; hardware_handlers and
calibration_handlers implement their actions. camera_selection identifies the
setup camera; run_log owns run journals and image artifacts; pages serves the
desktop and phone UI from static. completion_routes handles home and readiness.
server.app assembles these adapters and injects the setup recorder into the rig.

Reusable algorithms stay in core.calibration, device I/O in core.hardware,
and phone communication in core.bridge. Those layers depend only on generic
common.logger.evidence hooks, never on this feature or its HTTP handlers.
"""
