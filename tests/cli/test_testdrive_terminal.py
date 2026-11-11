"""Prompt portability through real native terminals and console API regressions."""

import ctypes
import importlib
import os
import re
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from physiclaw.common import terminal
from physiclaw.port.backends import darwin, linux, windows
from tests.terminal_fakes import Console
from tests.terminal_helpers import terminal_process

testdrive = importlib.import_module("physiclaw.cli.testdrive")


def _windows_console(monkeypatch, stream, *, native=True, vt=True):
    stream.fileno = lambda: 9
    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: 2**48 + fd)
    )

    def get_mode(handle, pointer):
        pointer._obj.value = 1
        return int(native)

    kernel = SimpleNamespace(
        GetConsoleMode=Mock(side_effect=get_mode),
        SetConsoleMode=Mock(return_value=int(vt)),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel, raising=False)
    return windows.enable_terminal_color


@pytest.fixture
def console(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)

    def activate():
        stream = Console()
        monkeypatch.setattr(sys, "stdout", stream)
        monkeypatch.setattr(sys, "stdin", Console())
        return stream

    return activate


@pytest.mark.parametrize(
    "backend", ["darwin", "linux", "windows-vt", "windows-legacy", "windows-pty"]
)
def test_prompt_uses_native_color_support(console, monkeypatch, mocker, backend):
    console = console()
    if backend.startswith("windows"):
        enable = _windows_console(
            monkeypatch,
            console,
            native=backend != "windows-pty",
            vt=backend != "windows-legacy",
        )
    else:
        enable = {"darwin": darwin, "linux": linux}[backend].enable_terminal_color
    monkeypatch.setattr(terminal.platform, "enable_terminal_color", enable)
    editor = Mock()
    editor.prompt.return_value = "tap"
    factory = mocker.patch("prompt_toolkit.PromptSession", return_value=editor)
    plain_input = mocker.patch("builtins.input", return_value="tap")

    session = testdrive._prompt_session()
    assert testdrive._read_command(True, session=session) == "tap"

    if backend == "windows-legacy":
        assert session is None
        factory.assert_not_called()
        plain_input.assert_called_once_with("testdrive → ")
    else:
        assert session is editor
        factory.assert_called_once_with()
        editor.prompt.assert_called_once_with(
            [("", "testdrive "), ("bold ansigreen", "→"), ("", " ")]
        )
        plain_input.assert_not_called()
    assert console.getvalue() == ""


@pytest.mark.parametrize(
    "mode", ["no-color", "dumb", "redirected-out", "redirected-in"]
)
def test_prompt_plain_fallbacks(console, monkeypatch, mocker, mode):
    console = console()
    monkeypatch.setattr(terminal.platform, "enable_terminal_color", lambda stream: True)
    if mode == "no-color":
        monkeypatch.setenv("NO_COLOR", "")
    elif mode == "dumb":
        monkeypatch.setenv("TERM", "dumb")
    elif mode == "redirected-out":
        console.tty = False
    elif mode == "redirected-in":
        monkeypatch.setattr(sys, "stdin", Console(tty=False))
    factory = mocker.patch("prompt_toolkit.PromptSession")
    monkeypatch.setattr("builtins.input", lambda prompt: "q")

    session = testdrive._prompt_session()
    assert session is None
    assert testdrive._read_command(True, session=session) == "q"
    factory.assert_not_called()
    assert console.getvalue() == ""


def test_unavailable_line_editor_falls_back_to_plain_input(
    console, monkeypatch, mocker
):
    console = console()
    monkeypatch.setattr(terminal.platform, "enable_terminal_color", lambda stream: True)
    mocker.patch(
        "prompt_toolkit.PromptSession", side_effect=RuntimeError("not a console")
    )
    plain_input = mocker.patch("builtins.input", return_value="q")

    session = testdrive._prompt_session()

    assert session is None
    assert testdrive._read_command(False, session=session) == "q"
    plain_input.assert_called_once_with("testdrive → ")
    assert console.getvalue() == ""


@pytest.mark.parametrize("encoding", ["ascii", "cp1252", "gbk", "utf-8"])
def test_session_supports_console_encodings(monkeypatch, encoding):
    stream = Console(encoding, tty=False)
    monkeypatch.setattr(sys, "stdout", stream)
    arm = SimpleNamespace(port="COM3", setup=Mock(), tap=Mock(), close=Mock())
    monkeypatch.setattr("physiclaw.core.hardware.arm.StylusArm", lambda port: arm)
    monkeypatch.setattr("physiclaw.common.runtime_state.read_live", lambda: None)
    commands = iter(["tap", "q"])
    prompts = []

    def read(prompt):
        prompts.append(prompt)
        return next(commands)

    monkeypatch.setattr("builtins.input", read)

    testdrive.testdrive()

    output = stream.getvalue()
    output.encode(encoding, "strict")
    assert "Connected on" in output
    assert "Board disconnected." not in output
    assert "X10" in output and "longpress" in output
    assert "\033" not in output
    marker = "->" if encoding in {"ascii", "cp1252"} else "→"
    assert prompts == [f"testdrive {marker} "] * 2
    arm.tap.assert_called_once_with()
    arm.close.assert_called_once_with()


@pytest.mark.parametrize("eager_input", [False, True])
def test_portable_editor_keeps_history_and_handles_eof_and_interrupt(
    monkeypatch, eager_input
):
    from asyncio import get_running_loop
    from contextlib import contextmanager

    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        if eager_input:
            original_attach = pipe.attach

            @contextmanager
            def attach(callback):
                with original_attach(callback):
                    # Windows can dispatch already queued keys before history loads.
                    get_running_loop().call_soon(callback)
                    yield

            monkeypatch.setattr(pipe, "attach", attach)

        session = PromptSession(input=pipe, output=DummyOutput())

        def read(keys, result):
            def send_after_render(app):
                app.after_render -= send_after_render
                # Let the initial render's history task run before sending keys.
                get_running_loop().call_soon(pipe.send_text, keys)

            session.app.after_render += send_after_render
            return testdrive._read_command(result, session=session)

        assert read("tap\n", None) == "tap"
        assert read("\033[A\001\013Y-1\n", True) == "Y-1"
        assert read("\033[A\n", False) == "Y-1"
        for key, error in (("\004", EOFError), ("\003", KeyboardInterrupt)):
            with pytest.raises(error):
                read(key, True)


@pytest.mark.parametrize("no_color", [False, True])
def test_real_line_editor_recalls_and_edits_colored_prompts(no_color):
    env = {**os.environ, "TERM": "xterm-256color", "PROMPT_TOOLKIT_NO_CPR": "1"}
    env.pop("NO_COLOR", None)
    if no_color:
        env["NO_COLOR"] = ""
    code = """
import sys
if sys.platform != "win32":
    import readline
from physiclaw.cli.testdrive import _prompt_session, _read_command
session = _prompt_session()
commands = []
for index, result in enumerate((None, True, False)):
    print('ready:' + str(index), flush=True)
    commands.append(_read_command(result, session=session))
print('received=' + repr(commands))
"""
    with terminal_process([sys.executable, "-c", code], env=env) as console:
        console.expect(r"ready:0.*testdrive →")
        console.write("tap\r")
        console.expect(r"ready:1.*testdrive →")
        # Recall tap, go Home and Delete its three characters, then enter a jog.
        console.write("\033[A\033[H\033[3~\033[3~\033[3~Y-1\r")
        console.expect(r"ready:2.*testdrive →")
        console.write("\033[A\r")
        console.expect(re.escape("received=['tap', 'Y-1', 'Y-1']"))
        assert console.wait() == 0
        styles = re.findall(r"\x1b\[([0-9;]*)m", console.output)
        for normal, bright in (("32", "92"), ("31", "91")):
            # ConPTY can render bold ANSI colors as their bright equivalents.
            assert (
                any(
                    ("1" in style.split(";") and normal in style.split(";"))
                    or bright in style.split(";")
                    for style in styles
                )
                is not no_color
            )
