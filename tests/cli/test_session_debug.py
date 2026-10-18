"""Session capture, service-log rotation and the installed CLI's reader contract.

Real writers and CLI subprocesses; a server process emits simulated hardware
records without connecting to a camera, arm, MCP server or model provider.
"""

import base64
import io
import json
import logging
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from physiclaw.agent.trace import RawLog, Trace
from physiclaw.agent.trace.store import Images
from physiclaw.common import config, paths
from physiclaw.common.logger import bind_log_context, reset_log_context, setup_logging
from physiclaw.contract.dto import ImageBlock, TextBlock

ROOT = Path(__file__).resolve().parents[2]
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/n1sAAAAASUVORK5CYII="


def test_logs_reads_and_exports_complete_session_after_service_rotation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PHYSICLAW_LOG_FORMAT", "pretty")
    monkeypatch.setenv("PHYSICLAW_LOG_COMPONENTS", "engine.dispatch")
    monkeypatch.setattr(config.CONFIG.retention, "log_max_bytes", 256)
    monkeypatch.setattr(config.CONFIG.retention, "log_backup_count", 1)
    console = io.StringIO()
    monkeypatch.setattr(sys, "stderr", console)
    setup_logging("runtime", file_dir=paths.LOG_DIR, file_level=logging.DEBUG)
    guide = (paths.LOG_DIR / "README.md").read_text(encoding="utf-8")
    assert "server.log" in guide and "model.jsonl" in guide and "archive/" in guide

    sid = "20261018-100000-abc123"
    directory = paths.sessions_dir() / sid
    assert directory == paths.LOG_DIR / "sessions" / sid
    images = Images(sid)
    trace, raw = Trace(sid, images), RawLog(sid, images)
    context = bind_log_context(session_id=sid, turn=0, tool_call_id="call-peek")
    note = ("Search is visible; next I will inspect the results. " * 20).rstrip()
    listing = "Full screen listing: " + "label bbox confidence 中文\n" * 300
    prompt = "Full system prompt and task context " * 100
    try:
        log = logging.getLogger("physiclaw.agent.engine.dispatch")
        log.debug("Runtime detail before rotation")
        trace.write(
            {"event": "wake", "session": sid, "model_ref": "test/model", "triggers": []}
        )
        raw.write_session_start(
            provider="test", model="model", prompt_hash="hash", tools=[{"name": "peek"}]
        )
        trace.write({"event": "request", "turn": 0, "message_count": 1})
        raw.write_request(0, [{"role": "system", "content": prompt}])
        trace.write(
            {
                "event": "usage",
                "call": "turn",
                "model": "test/model",
                "input": 100,
                "input_new": 20,
                "cache_read": 80,
                "cache_write": 0,
                "output": 10,
                "reasoning": 4,
                "elapsed_ms": 1234,
            }
        )
        trace.write(
            {
                "event": "response",
                "turn": 0,
                "finish_reason": "tool_calls",
                "tool_calls": [{"name": "peek"}],
                "elapsed_ms": 1234,
            }
        )
        reply = {
            "choices": [
                {
                    "message": {
                        "content": "Inspect results",
                        "tool_calls": [
                            {
                                "id": "call-peek",
                                "function": {"name": "peek", "arguments": "{}"},
                            }
                        ],
                    }
                }
            ]
        }
        raw.write_response(0, reply, elapsed_ms=1234)
        trace.write(
            {
                "event": "tool_result",
                "turn": 0,
                "name": "note",
                "arguments": {"summary": note},
                "text": "noted: " + note,
            }
        )
        trace.write(
            {
                "event": "tool_result",
                "turn": 0,
                "name": "peek",
                "arguments": {},
                "elapsed_ms": 456,
                "blocks": [TextBlock(listing), ImageBlock("image/png", PNG)],
            }
        )
        raw.write_request(
            1,
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + PNG},
                        }
                    ],
                }
            ],
        )
        try:
            raise RuntimeError("runtime failure detail")
        except RuntimeError:
            log.debug("Runtime traceback", exc_info=True)
        for i in range(15):
            log.debug("Runtime detail %d: %s", i, "saved only on disk " * 20)

        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "PHYSICLAW_HOME": str(paths.HOME),
        }
        server = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                """
import logging
from physiclaw.common import paths
from physiclaw.common.config import CONFIG
from physiclaw.common.logger import attach_server_mcp_tee, setup_logging
CONFIG.retention.log_max_bytes = 256
CONFIG.retention.log_backup_count = 1
setup_logging('physiclaw', file_dir=paths.LOG_DIR, file_level=logging.DEBUG, file_prefix='server')
attach_server_mcp_tee()
log = logging.getLogger('physiclaw.core.hardware.camera')
log.debug('Camera diagnostic before rotation')
try:
    raise OSError('simulated camera failure detail')
except OSError:
    log.warning('Camera read failed', exc_info=True, extra={'console_message': 'Camera reconnecting', 'console_traceback': False})
for i in range(15):
    log.debug('Camera detail %d: %s', i, 'exposure and frame diagnostics ' * 20)
""",
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        assert server.returncode == 0, server.stderr
        assert not server.stdout
        assert "Camera reconnecting" in server.stderr
        assert "Traceback" not in server.stderr
        trace.write(
            {"event": "done", "sentinel": "DONE", "recap": "Verified all results"}
        )
    finally:
        raw.close()
        trace.close()
        reset_log_context(context)

    assert "Runtime detail" not in console.getvalue()
    assert "runtime failure detail" not in console.getvalue()
    runtime = (directory / "runtime.log").read_text(encoding="utf-8")
    mcp = (directory / "server.log").read_text(encoding="utf-8")
    assert (
        "Runtime detail before rotation" in runtime and "Runtime detail 14:" in runtime
    )
    assert "RuntimeError: runtime failure detail" in runtime and "Traceback" in runtime
    assert "session_id=" + sid in runtime and "tool_call_id=call-peek" in runtime
    assert "Camera diagnostic before rotation" in mcp and "Camera detail 14:" in mcp
    assert "OSError: simulated camera failure detail" in mcp and "Traceback" in mcp
    assert note in (directory / "notes.md").read_text(encoding="utf-8")
    assert not paths.active_session_marker().exists()
    # Rotating service files have already dropped the oldest details; sidecars
    # and replay artifacts preserve the complete session independently.
    for prefix, missing in [
        ("runtime", "Runtime detail before rotation"),
        ("server", "Camera diagnostic before rotation"),
    ]:
        active = paths.LOG_DIR / f"{prefix}.log"
        archives = list((paths.LOG_DIR / "archive").glob(f"{prefix}.log.*"))
        assert active.is_file() and len(archives) == 1
        assert all(
            missing not in p.read_text(encoding="utf-8") for p in [active, *archives]
        )
    assert not list(directory.glob("*.log.*"))
    assert not any(
        (paths.LOG_DIR / name).exists()
        for name in ("engine", "runtime", "server", "cli", "studio")
    )
    assert not list(paths.LOG_DIR.glob("engine-*.log"))
    assert (paths.LOG_DIR / "session-stats.jsonl").is_file()

    events = [
        json.loads(line)
        for line in (directory / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    peek = next(e for e in events if e.get("name") == "peek")
    assert peek["text"] == listing and peek["elapsed_ms"] == 456
    assert (directory / peek["images"][0]).read_bytes() == base64.b64decode(PNG)
    wire = [
        json.loads(line)
        for line in (directory / "model.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert wire[1]["messages"][0]["content"] == prompt
    assert wire[2]["raw"] == reply
    assert wire[3]["messages"][0]["content"][0]["image_url"]["url"] == peek["images"][0]

    def cli(*args):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from physiclaw.cli import app; app()",
                "logs",
                *args,
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert not result.stderr
        return result.stdout

    assert sid in cli("-n", "1")
    summary = json.loads(cli("abc123", "--json"))
    assert summary["model_ref"] == "test/model"
    assert summary["outcome"]["sentinel"] == "DONE" and summary["images"] == 1
    assert (
        summary["usage"]["input_tokens"] == 100
        and summary["usage"]["output_tokens"] == 10
    )
    assert summary["usage"]["cache_hit_pct"] == 80
    detail = cli("abc123", "-n", "100")
    assert "Verified all results" in detail
    assert (
        str(directory / "runtime.log") in detail
        and str(directory / "server.log") in detail
    )
    usage = json.loads(cli("abc123", "--usage", "--json"))
    assert usage == [
        {
            k: v
            for k, v in next(e for e in events if e["event"] == "usage").items()
            if k not in ("event", "t")
        }
    ]
    assert "test/model" in cli("abc123", "--usage")

    out = tmp_path / "debug.zip"
    originals = {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in directory.rglob("*")
        if p.is_file()
    }
    assert "saved" in cli("abc123", "--save", str(out))
    with zipfile.ZipFile(out) as archive:
        assert archive.testzip() is None
        for name, data in originals.items():
            assert archive.read(f"{sid}/{name}") == data, name
        html = archive.read(f"{sid}/session.html").decode("utf-8")
        assert (
            "runtime failure detail" in html
            and "simulated camera failure detail" in html
        )
        readme = archive.read(f"{sid}/README.md").decode("utf-8")
        assert all(
            name in readme for name in ("runtime.log", "server.log", "model.jsonl")
        )
