"""Preview the real terminal renderer with sample events; no hardware required.

python scripts/preview_terminal.py
python scripts/preview_terminal.py --verbose
python scripts/preview_terminal.py --log-format json
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from physiclaw.common.log_style import (  # noqa: E402
    add_log_arguments,
    bind_log_context,
    configure_console,
    format_tool_call,
    reset_log_context,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preview sample terminal logs without connecting hardware."
    )
    add_log_arguments(parser)
    args = parser.parse_args()
    configure_console("preview", verbose=args.verbose, log_format=args.log_format)
    logging.getLogger("physiclaw.preview").info(
        "Terminal preview · sample events", extra={"console": True}
    )
    context = bind_log_context(session_id="preview-session", turn=2)
    try:
        logging.getLogger("physiclaw.agent.runtime.launcher").info(
            "engine=physiclaw, provider=preview, model=model [sample configuration]",
            extra={
                "event": "model.selected",
                "model_ref": "preview/model",
                "console": True,
                "console_message": "Model: preview/model",
            },
        )
        logging.getLogger("physiclaw.agent.runtime.runtime").info(
            "Ready — watching for tasks",
            extra={"event": "runtime.ready", "console": True},
        )
        logging.getLogger("physiclaw.agent.engine.engine").info(
            "Session preview-session started | model=preview/model",
            extra={
                "event": "session.started",
                "console": True,
                "console_message": "Task started",
            },
        )
        logging.getLogger("physiclaw.agent.engine.loop").info(
            "Turn 2 · Model: tap (1.2s)",
            extra={"event": "turn.planned", "duration_ms": 1200},
        )
        logging.getLogger("physiclaw.agent.engine.dispatch").info(
            "turn 2: note → noted: Search is visible; I will open it",
            extra={
                "event": "tool.completed",
                "tool": "note",
                "console": True,
                "console_message": "Agent: Search is visible; I will open it",
            },
        )
        logging.getLogger("physiclaw.agent.engine.builtin_tool").info(
            "Plan step started: Find the requested item",
            extra={
                "event": "task.progress",
                "console": True,
                "console_message": "Plan: 1. Search for the item; 2. Compare options; 3. Report the result",
            },
        )
        logging.getLogger("physiclaw.agent.trace.trace").info(
            "turn 2: usage[turn preview/model] in=12400 out=320 cache_read=9920",
            extra={
                "event": "usage",
                "console": True,
                "console_message": "Tokens: 12.4k in / 320 out · 80% cached · step 2",
            },
        )
        logging.getLogger("physiclaw.tools").info(
            "tool tap(bbox=[0.1, 0.2, 0.3, 0.4]) started",
            extra={
                "event": "tool.started",
                "tool": "tap",
                "tool_call_id": "preview-call",
                "console": True,
                "console_message": "Tool: "
                + format_tool_call("tap", {"bbox": [0.1, 0.2, 0.3, 0.4]}),
            },
        )
        logging.getLogger("physiclaw.agent.engine.dispatch").info(
            "turn 2: tap → screen changed (0.24s)",
            extra={
                "event": "tool.completed",
                "tool": "tap",
                "tool_call_id": "preview-call",
                "duration_ms": 240,
            },
        )
        camera = logging.getLogger("physiclaw.core.hardware.camera")
        camera.debug("Camera diagnostic: device=preview, resolution=1920x1080")
        camera.warning(
            "Camera connection lost — check USB and power; retrying automatically",
            extra={"event": "camera.reconnecting"},
        )
        camera.info(
            "Camera connection restored",
            extra={"event": "camera.frames_restored", "console": True},
        )
        logging.getLogger("physiclaw.agent.trace.trace").info(
            "END session=preview-session outcome=DONE turns=3 tools=5",
            extra={
                "event": "session.completed",
                "console": True,
                "console_message": "Task completed (4.2s)",
            },
        )
        logging.getLogger("physiclaw.preview").info(
            "Preview complete · Unicode sample: 摄像头已连接", extra={"console": True}
        )
    finally:
        reset_log_context(context)


if __name__ == "__main__":
    main()
