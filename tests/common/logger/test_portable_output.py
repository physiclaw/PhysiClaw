"""Portable logging checks; runnable with pytest alone and --noconftest."""

import errno
import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from physiclaw.common import log_style, terminal
from physiclaw.common.platform import windows

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "name, arguments, expected",
    [
        ("peek", {}, "peek()"),
        ("tap", {"bbox": [0.1, 0.2, 0.3, 0.4]}, "tap(bbox=[0.1, 0.2, 0.3, 0.4])"),
        ("swipe", {"direction": "up", "size": "m"}, "swipe(direction='up', size='m')"),
        ("run_macro", {"name": "channel/open"}, "run_macro(name='channel/open')"),
    ],
)
def test_tool_call_summary_preserves_the_tool_and_short_arguments(
    name, arguments, expected
):
    assert log_style.format_tool_call(name, arguments) == expected


def test_tool_call_summary_bounds_large_arguments_and_keeps_clipboard_bodies_out():
    secret = "long clipboard body" * 100
    assert len(log_style.format_tool_call("custom", {"value": secret})) <= 220
    assert secret not in log_style.format_tool_call(
        "send_to_clipboard", {"text": secret}
    )
    actions = [{"tool_name": "send_to_clipboard", "arg": secret}]
    summary = log_style.format_tool_call("sequence", {"actions": actions})
    assert (
        secret not in summary and "send_to_clipboard" in summary and "chars" in summary
    )
    assert actions[0]["arg"] == secret  # Presentation must not mutate the call.


@pytest.mark.parametrize(
    "secret", ["PIN-7391", "验证码8452", "long clipboard body" * 100]
)
def test_clipboard_redaction_is_shared_and_does_not_change_arguments(secret):
    direct = {"text": secret}
    sequence = {
        "actions": [
            {"tool_name": "send_to_clipboard", "arg": secret},
            {"tool_name": "tap", "arg": [0.1, 0.2, 0.3, 0.4]},
        ]
    }

    assert log_style.redact_tool_arguments("send_to_clipboard", direct) == {
        "text": f"<{len(secret)} chars>"
    }
    clean = log_style.redact_tool_arguments("sequence", sequence)
    assert clean["actions"][0]["arg"] == f"<{len(secret)} chars>"
    assert clean["actions"][1] == sequence["actions"][1]
    assert secret not in log_style.format_tool_call("sequence", sequence)
    assert direct["text"] == secret and sequence["actions"][0]["arg"] == secret


class Console(io.StringIO):
    def __init__(self, encoding="utf-8", *, tty=True):
        super().__init__()
        self.codec = encoding
        self.tty = tty

    @property
    def encoding(self):
        return self.codec

    def isatty(self):
        return self.tty

    def write(self, text):
        text.encode(self.encoding, "strict")
        return super().write(text)


@pytest.fixture
def output(monkeypatch):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", root.level)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("PHYSICLAW_LOG_FORMAT", raising=False)
    monkeypatch.delenv("PHYSICLAW_LOG_COMPONENTS", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setattr(terminal.platform, "enable_terminal_color", lambda stream: True)
    stream = Console()

    def activate():
        monkeypatch.setattr(sys, "stderr", stream)
        return stream

    yield activate
    for handler in root.handlers:
        handler.close()


def emit(message="Camera connected", level=logging.INFO, **extra):
    extra.setdefault(
        "console", True
    )  # Explicit user-facing notice, not a camera diagnostic.
    logging.getLogger("physiclaw.core.hardware.camera").log(level, message, extra=extra)


@pytest.mark.parametrize("width", [40, 48, 80, 100, 160])
def test_layout_fits_narrow_and_wide_terminals_without_losing_words(
    output, monkeypatch, width
):
    output = output()
    monkeypatch.setenv("COLUMNS", str(width))
    log_style.configure_console("runtime")
    message = (
        "Camera connection lost; check the USB cable and power. Retrying automatically."
    )
    emit(message, logging.WARNING)
    rendered = terminal.ANSI_RE.sub("", output.getvalue())
    assert all(terminal.cell_width(line) < width for line in rendered.splitlines())
    assert " ".join(rendered.split()).endswith(message)
    assert (
        "WARN" in rendered
        and "[runtime]" in rendered
        and "hardware.camera" not in rendered
    )


def test_resize_and_redirection_are_detected_after_configuration(output, monkeypatch):
    output = output()
    log_style.configure_console("runtime")
    emit("First message")
    assert "\033[" in output.getvalue()
    output.seek(0)
    output.truncate()
    monkeypatch.setenv("COLUMNS", "40")
    emit("Second message")
    assert "Second message" in output.getvalue()
    assert "hardware.camera" not in output.getvalue()
    pipe = Console(tty=False)
    monkeypatch.setattr(sys, "stderr", pipe)
    message = "path=" + "/long-path" * 30
    emit(message)
    assert "\033" not in pipe.getvalue()
    assert len(pipe.getvalue().splitlines()) == 1
    assert message in pipe.getvalue()


@pytest.mark.parametrize("env", [{"NO_COLOR": ""}, {"TERM": "dumb"}, {"TERM": "DUMB"}])
def test_monochrome_output_remains_readable(output, monkeypatch, env):
    output = output()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    log_style.configure_console("runtime")
    emit("Camera unavailable", logging.ERROR)
    assert "\033" not in output.getvalue()
    assert "ERROR [runtime] Camera unavailable" in output.getvalue()


@pytest.mark.parametrize("encoding", ["ascii", "cp1252", "gbk", "utf-8"])
def test_encoding_fallback_does_not_lose_a_record(output, encoding, monkeypatch):
    output = output()
    stream = Console(encoding, tty=False)
    monkeypatch.setattr(sys, "stderr", stream)
    log_style.configure_console("runtime")
    emit("✓ Ready → 测试 — café \ud800")
    text = stream.getvalue()
    text.encode(encoding, "strict")
    assert "Ready" in text and "\\ud800" in text
    assert "Logging error" not in text
    assert len(text.splitlines()) == 1
    assert ("测试" in text) is (encoding in {"utf-8", "gbk"})
    if encoding == "ascii":
        assert "OK Ready ->" in text and "\\u6d4b\\u8bd5" in text


def test_unicode_display_width_and_copyable_paths(output, monkeypatch):
    output = output()
    monkeypatch.setenv("COLUMNS", "48")
    log_style.configure_console("runtime")
    emit("状态 已连接 摄像头 准备 就绪 " * 4)
    url = "http://127.0.0.1:8048/a/very/long/path?session=unchanged"
    emit(url)
    text = terminal.ANSI_RE.sub("", output.getvalue())
    assert all(
        terminal.cell_width(line) < 48 for line in text.splitlines() if url not in line
    )
    assert url in text
    assert terminal.cell_width("e\u0301 中文") == 6


def test_external_control_sequences_cannot_repaint_the_log(output):
    output = output()
    log_style.configure_console("runtime")
    emit(
        "before\rAFTER\0\033[2J\033]0;new title\7\033]8;;https://example.com\033\\label\033]8;;\033\\"
    )
    text = terminal.ANSI_RE.sub("", output.getvalue())
    assert "before\\rAFTER\\x00label" in text
    assert "new title" not in text and "\r" not in text and "\0" not in text


def test_json_is_one_lossless_record_even_on_ascii_streams(output, monkeypatch):
    output = output()
    stream = Console("ascii", tty=True)
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setenv("COLUMNS", "32")
    log_style.configure_console("runtime", log_format="json")
    token = log_style.bind_log_context(session_id="session-中文", turn=2)
    try:
        emit("第一行\n第二行\r\0", event="camera.ready")
    finally:
        log_style.reset_log_context(token)
    (line,) = stream.getvalue().splitlines()
    record = json.loads(line)
    assert record["message"] == "第一行\n第二行\r\0"
    assert record["session_id"] == "session-中文" and record["turn"] == 2
    assert record["event"] == "camera.ready"
    assert "\033" not in line


@pytest.mark.parametrize("kind", ["closed", "pipe", "none"])
def test_unavailable_console_does_not_interrupt_other_sinks(output, monkeypatch, kind):
    output = output()
    log_style.configure_console("runtime")
    captured = io.StringIO()
    logging.getLogger().addHandler(logging.StreamHandler(captured))
    if kind == "closed":
        stream = Console()
        stream.close()
    elif kind == "pipe":
        stream = Console()
        stream.write = Mock(side_effect=BrokenPipeError(errno.EPIPE, "closed"))
    else:
        stream = None
    monkeypatch.setattr(sys, "stderr", stream)
    emit("Saved despite console closing", logging.ERROR)
    assert captured.getvalue() == "Saved despite console closing\n"


def test_verbose_console_keeps_traceback_even_for_normally_concise_failures(output):
    output = output()
    log_style.configure_console("runtime", verbose=True)
    try:
        raise RuntimeError("camera failed")
    except RuntimeError:
        logging.getLogger("physiclaw.camera").exception(
            "Read failed", extra={"console_traceback": False}
        )
    assert "Traceback" in output.getvalue()
    assert "RuntimeError: camera failed" in output.getvalue()


@pytest.mark.parametrize(
    "initial,success,expected", [(1, True, True), (5, True, True), (1, False, False)]
)
def test_windows_enables_vt_on_the_actual_stream_handle(
    monkeypatch, initial, success, expected
):
    import ctypes

    monkeypatch.delenv("TERM", raising=False)
    stream = Mock()
    stream.fileno.return_value = 9
    handle = 2**48 + 7  # Must not truncate a 64-bit HANDLE to a C int.
    native = SimpleNamespace(get_osfhandle=Mock(return_value=handle))
    monkeypatch.setitem(sys.modules, "msvcrt", native)

    def get_mode(h, pointer):
        assert h == handle
        pointer._obj.value = initial
        return 1

    kernel = SimpleNamespace(
        GetConsoleMode=Mock(side_effect=get_mode),
        SetConsoleMode=Mock(return_value=int(success)),
    )
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=kernel), raising=False)
    assert windows.enable_terminal_color(stream) is expected
    native.get_osfhandle.assert_called_once_with(9)
    if initial == 5:
        kernel.SetConsoleMode.assert_not_called()
    else:
        kernel.SetConsoleMode.assert_called_once_with(handle, 5)
    assert kernel.GetConsoleMode.argtypes[0] == ctypes.c_void_p


def test_windows_without_vt_uses_plain_output_unless_it_is_an_ansi_pty(monkeypatch):
    import ctypes

    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(get_osfhandle=Mock(side_effect=OSError("not a console"))),
    )
    monkeypatch.setattr(
        ctypes, "WinDLL", Mock(side_effect=OSError("not available")), raising=False
    )
    monkeypatch.delenv("TERM", raising=False)
    assert not windows.enable_terminal_color(Console())
    monkeypatch.setenv("TERM", "xterm-256color")
    assert windows.enable_terminal_color(Console())


def test_preview_script_exercises_the_real_json_contract_without_hardware():
    env = dict(os.environ)
    env.pop("PHYSICLAW_LOG_COMPONENTS", None)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "preview_terminal.py"),
            "--log-format",
            "json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0 and result.stdout == ""
    records = [json.loads(line) for line in result.stderr.splitlines()]
    assert all(record["service"] == "preview" for record in records)
    (tool,) = [
        record
        for record in records
        if record["event"] == "tool.completed" and record.get("tool") == "tap"
    ]
    assert tool["session_id"] == "preview-session" and tool["turn"] == 2
    assert tool["duration_ms"] == 240 and tool["tool_call_id"] == "preview-call"
    assert tool["path"].endswith("preview_terminal.py")
    assert records[0]["message"] == "Terminal preview · sample events"


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX PTY; Windows has native console tests above"
)
@pytest.mark.parametrize("no_color", [False, True])
def test_real_pty_and_redirected_stdout_are_independent(no_color):
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 48, 0, 0))
        env = {**os.environ, "TERM": "xterm-256color", "PYTHONPATH": str(ROOT / "src")}
        env.pop("PHYSICLAW_LOG_FORMAT", None)
        env.pop("PHYSICLAW_LOG_COMPONENTS", None)
        env.pop("NO_COLOR", None)
        if no_color:
            env["NO_COLOR"] = ""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from physiclaw.common.log_style import configure_console; import logging; configure_console('runtime'); logging.getLogger('physiclaw.camera').warning('Check camera USB and power; retrying automatically'); print('payload')",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=slave,
            timeout=10,
        )
        # macOS may discard unread PTY output when the last slave closes.
        # The child has finished writing; drain before closing the parent's copy.
        os.set_blocking(master, False)
        chunks = []
        while True:
            try:
                chunk = os.read(master, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            except BlockingIOError:
                break
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
        text = b"".join(chunks).decode()
        assert result.returncode == 0 and result.stdout.strip() == b"payload"
        assert ("\033[" in text) is not no_color
        clean = terminal.ANSI_RE.sub("", text)
        assert all(terminal.cell_width(line) < 48 for line in clean.splitlines())
        assert "retrying automatically" in " ".join(clean.split())
    finally:
        os.close(master)
        if slave is not None:
            os.close(slave)
