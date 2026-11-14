"""Real managed parent/child and loopback MCP lifecycle; no hardware/API.

Run: pytest -m integration tests/cli/test_runtime_shutdown.py
"""

import errno
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from physiclaw.port import processes as process_port
from tests.cli.runtime_shutdown_app import STARTUP_TIMEOUT

pytestmark = pytest.mark.integration
POSIX_SIGNAL = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Direct POSIX signal delivery; Ctrl+C runs on every OS",
)
TARGET_ERRORS = (
    "ASGI callable returned without completing response",
    "Session termination failed",
)


async def test_unconfigured_server_serves_mcp_after_hardware_ready_without_agent(
    tmp_path,
):
    import httpx

    from physiclaw.agent.engine.mcp_tool import McpClient
    from physiclaw.common import runtime_state

    with running(tmp_path, "unconfigured") as (parent, path, port):
        output = wait_for(parent, path, "SERVICES_STARTED")
        assert "No provider API key configured" in output
        assert "Starting MCP-only" in output
        assert runtime_state.read_live()["no_runtime"] is True
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            status = await http.get("/api/status")
            assert status.json()["ready"] is False
            assert "MCP tools live" not in path.read_text(encoding="utf-8")
            response = await http.post("/api/ready")
            response.raise_for_status()
        wait_for(parent, path, "MCP tools live")
        async with McpClient(f"http://127.0.0.1:{port}") as client:
            tools = await client.list_tools()
            assert any(tool["name"] == "finish" for tool in tools)
        interrupt(parent)
        parent.wait(timeout=5)

    output = path.read_text(encoding="utf-8")
    assert parent.returncode == 0, output
    assert "CHILD_PID=" not in output
    assert "REACT_STARTED" not in output
    assert runtime_state.read_live() is None


@contextmanager
def reserved_ports():
    # Keep the actual bind addresses reserved until the child inherits them.
    # A loopback probe misses conflicts on other interfaces; closing the
    # sockets before Popen also lets another process take the selected ports.
    for _ in range(100):
        with socket.socket() as control, socket.socket() as bridge:
            control.bind(("127.0.0.1", 0))
            port = control.getsockname()[1]
            if port >= 65535:
                continue
            try:
                bridge.bind(("0.0.0.0", port + 1))
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise
                continue
            yield port, (control, bridge)
            return
    raise AssertionError("no adjacent free ports")


@contextmanager
def running(tmp_path, mode):
    path = tmp_path / "process.log"
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }
    proc = None
    with path.open("a", encoding="utf-8") as output:
        try:
            with reserved_ports() as (port, listeners):
                descriptors = tuple(listener.fileno() for listener in listeners)
                options = (
                    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                    if sys.platform == "win32"
                    else {"pass_fds": descriptors}
                )
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("runtime_shutdown_app.py")),
                        "parent",
                        str(port),
                        mode,
                        *(str(fd) for fd in descriptors),
                    ],
                    **options,
                    stdin=subprocess.PIPE,
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
                if sys.platform == "win32":
                    # WSADuplicateSocket keeps both addresses reserved while
                    # the child starts; Windows cannot inherit via pass_fds.
                    shared = [listener.share(proc.pid).hex() for listener in listeners]
                    proc.stdin.write(json.dumps(shared).encode("ascii") + b"\n")
                    proc.stdin.flush()
            yield proc, path, port
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
            # Do not leave a child behind even when a regression fails a test.
            match = re.search(r"CHILD_PID=(\d+)", path.read_text(encoding="utf-8"))
            if match:
                try:
                    pid = int(match[1])
                    if process_port.pid_alive(pid):
                        os.kill(
                            pid,
                            signal.SIGTERM
                            if sys.platform == "win32"
                            else signal.SIGKILL,
                        )
                except OSError:
                    if process_port.pid_alive(pid):
                        raise


def interrupt(proc, sig=None):
    if sig is not None:
        proc.send_signal(sig)
        return
    # The fixture injects Python's Ctrl+C on its own main thread. This needs
    # no shared desktop console and leaves the real server/EOF shutdown intact.
    proc.stdin.write(b"\n")
    proc.stdin.flush()


def release_hardware(proc):
    proc.stdin.write(b"r")
    proc.stdin.flush()


def wait_for(proc, path, text, *, timeout=STARTUP_TIMEOUT):
    deadline = time.monotonic() + timeout
    while True:
        # Poll before reading so an exited process's final output is included.
        code = proc.poll()
        output = path.read_text(encoding="utf-8")
        if text in output:
            return output
        if code is not None or time.monotonic() >= deadline:
            pytest.fail(
                f"Waiting for {text!r} failed (timeout={timeout}s, "
                f"returncode={code}):\n{output}"
            )
        time.sleep(0.02)


def finished(proc, path, port):
    proc.wait(timeout=8)
    output = path.read_text(encoding="utf-8")
    assert "Runtime child reaped" in output, output
    child = int(re.search(r"CHILD_PID=(\d+)", output)[1])
    # os.kill(pid, 0) terminates a process on Windows instead of probing it.
    assert not process_port.pid_alive(child), output
    for number in (port, port + 1):
        with socket.socket() as sock:
            sock.settimeout(0.2)
            assert sock.connect_ex(("127.0.0.1", number)) != 0
    return output


@pytest.mark.parametrize(
    "mode, marker",
    [
        ("cooldown", "REACT_DONE"),
        ("tool", "TOOL_STARTED"),
        ("model", "MCP_READY"),
        ("not-ready", "ready=False"),
    ],
)
@pytest.mark.parametrize(
    "sig",
    [
        pytest.param(None, id="ctrl-c"),
        pytest.param(signal.SIGINT, id="sigint", marks=POSIX_SIGNAL),
        pytest.param(
            signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM,
            id="ctrl-break" if sys.platform == "win32" else "sigterm",
        ),
    ],
)
def test_managed_exit_keeps_services_alive_for_cleanup(tmp_path, mode, marker, sig):
    with running(tmp_path, mode) as (proc, path, port):
        wait_for(proc, path, marker)
        interrupt(proc, sig)
        if mode == "tool":
            wait_for(proc, path, "Runtime stop requested")
            release_hardware(proc)
        output = finished(proc, path, port)
    assert proc.returncode == 0, output
    assert not any(error in output for error in (*TARGET_ERRORS, "cancel scope")), (
        output
    )
    ordered = [
        "CLOSE_MCP_DONE",
        "CHILD_EXIT",
        "Runtime child reaped",
        "Control service stopping",
        "Phone bridge stopping",
        "HARDWARE_SHUTDOWN",
    ]
    positions = [output.index(item) for item in ordered]
    assert positions == sorted(positions)
    assert output.count("REACT_STARTED") <= 1
    if mode == "tool":
        assert output.index("HARDWARE_DONE") < output.index("CLOSE_MCP_DONE")
    if mode == "model":
        assert "TOOL_STARTED" not in output
    if mode == "not-ready":
        assert "MCP client connected" not in output


@pytest.mark.parametrize(
    "mode, marker",
    [
        ("unresponsive", "CHILD_UNRESPONSIVE"),
        ("early-exit", "SERVICES_STARTED"),
        ("startup-failure", "deliberate startup-failure"),
        ("bridge-failure", "deliberate bridge-failure"),
    ],
)
def test_abnormal_exit_is_bounded_and_reaps_child(tmp_path, mode, marker):
    with running(tmp_path, mode) as (proc, path, port):
        # Imports and startup must not consume the shutdown deadline.
        wait_for(proc, path, marker)
        if mode not in {"startup-failure", "bridge-failure"}:
            interrupt(proc)
        output = finished(proc, path, port)
    if mode == "unresponsive":
        assert "Runtime exit forced (timeout)" in output
    if mode == "startup-failure":
        assert "deliberate startup-failure" in output
    if mode == "bridge-failure":
        assert "phone bridge failed" in output
    if mode == "early-exit":
        assert "code=0" in output
        assert "Fatal Python error" not in output


def test_repeated_interrupt_forces_exit_without_repeating_cleanup(tmp_path):
    with running(tmp_path, "tool") as (proc, path, port):
        wait_for(proc, path, "TOOL_STARTED")
        interrupt(proc)
        wait_for(proc, path, "Requesting runtime exit", timeout=8)
        interrupt(proc)
        wait_for(proc, path, "Runtime exit forced (interrupt)", timeout=8)
        release_hardware(proc)
        output = finished(proc, path, port)
    assert "Runtime exit forced (interrupt)" in output
    assert output.count("Runtime child reaped") == 1
    assert output.index("HARDWARE_DONE") < output.index("Phone bridge stopping")


def test_server_first_negative_control_detects_wrong_order(tmp_path):
    with running(tmp_path, "server-first") as (proc, path, port):
        wait_for(proc, path, "REACT_DONE")
        interrupt(proc)
        output = finished(proc, path, port)
    assert output.index("Control service stopping") < output.index(
        "Requesting runtime exit"
    ), output
    # Forced termination on Windows can prevent the child from logging its
    # session error. Require failure evidence without depending on that race.
    assert any(error in output for error in (*TARGET_ERRORS, "Runtime exit forced")), (
        output
    )


def test_no_runtime_server_and_standalone_runtime_keep_real_mcp_lifecycle(tmp_path):
    with running(tmp_path, "external") as (parent, path, port):
        wait_for(parent, path, "MCP tools live")
        with path.open("a", encoding="utf-8") as output:
            child = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("runtime_shutdown_app.py")),
                    "child",
                    f"http://127.0.0.1:{port}",
                    "standalone",
                ],
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.STDOUT,
                env={
                    **os.environ,
                    "PYTHONIOENCODING": "utf-8",
                    "NO_PROXY": "*",
                    "no_proxy": "*",
                },
            )
            try:
                wait_for(child, path, "REACT_DONE")
                interrupt(child)
                child.wait(timeout=5)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                child.stdin.close()
        interrupt(parent)
        parent.wait(timeout=8)
    output = path.read_text(encoding="utf-8")
    assert child.returncode == 0, output
    assert "CLOSE_MCP_DONE" in output
    assert parent.returncode == 0, output
    assert not any(error in output for error in (*TARGET_ERRORS, "cancel scope"))


def test_regression_listeners_cannot_be_stolen_during_spawn(tmp_path, mocker):
    popen = subprocess.Popen

    def spawn(args, **kwargs):
        for host, offset in (("127.0.0.1", 0), ("0.0.0.0", 1)):
            with socket.socket() as competitor:
                with pytest.raises(OSError) as caught:  # noqa: PT011 -- errno is asserted below.
                    competitor.bind((host, int(args[3]) + offset))
            assert caught.value.errno == errno.EADDRINUSE
        return popen(args, **kwargs)

    mocker.patch.object(subprocess, "Popen", side_effect=spawn)

    with running(tmp_path, "external") as (parent, path, port):
        wait_for(parent, path, "MCP tools live")
        interrupt(parent)
        parent.wait(timeout=5)

    assert parent.returncode == 0, path.read_text(encoding="utf-8")
