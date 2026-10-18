"""Real managed parent/child and loopback MCP lifecycle; no hardware/API.

Run: pytest -m integration tests/cli/test_runtime_shutdown.py
"""

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

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal replay tests"),
]
TARGET_ERRORS = (
    "ASGI callable returned without completing response",
    "Session termination failed",
)


def free_ports():
    # The server's bridge always uses control+1.
    for _ in range(100):
        with socket.socket() as control, socket.socket() as bridge:
            control.bind(("127.0.0.1", 0))
            port = control.getsockname()[1]
            if port >= 65535:
                continue
            try:
                bridge.bind(("127.0.0.1", port + 1))
            except OSError:
                continue
            return port
    raise AssertionError("no adjacent free ports")


@contextmanager
def running(tmp_path, mode):
    port = free_ports()
    path = tmp_path / "process.log"
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "PYTHONUNBUFFERED": "1",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }
    with path.open("a") as output:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("runtime_shutdown_app.py")),
                "parent",
                str(port),
                mode,
            ],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            yield proc, path, port
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            # Do not leave a child behind even when a regression fails a test.
            match = re.search(r"CHILD_PID=(\d+)", path.read_text())
            if match:
                try:
                    os.kill(int(match[1]), signal.SIGKILL)
                except ProcessLookupError:
                    pass


def wait_for(proc, path, text):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        output = path.read_text()
        if text in output:
            return output
        assert proc.poll() is None, output
        time.sleep(0.02)
    pytest.fail(path.read_text())


def finished(proc, path, port):
    proc.wait(timeout=8)
    output = path.read_text()
    assert "Runtime child reaped" in output, output
    child = int(re.search(r"CHILD_PID=(\d+)", output)[1])
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
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
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_managed_exit_keeps_services_alive_for_cleanup(tmp_path, mode, marker, sig):
    with running(tmp_path, mode) as (proc, path, port):
        wait_for(proc, path, marker)
        proc.send_signal(sig)
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
        if mode not in {"startup-failure", "bridge-failure"}:
            wait_for(proc, path, marker)
            proc.send_signal(signal.SIGINT)
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
        proc.send_signal(signal.SIGINT)
        wait_for(proc, path, "Requesting runtime exit")
        proc.send_signal(signal.SIGINT)
        output = finished(proc, path, port)
    assert "Runtime exit forced (interrupt)" in output
    assert output.count("Runtime child reaped") == 1
    assert output.index("HARDWARE_DONE") < output.index("Phone bridge stopping")


def test_server_first_negative_control_detects_wrong_order(tmp_path):
    with running(tmp_path, "server-first") as (proc, path, port):
        wait_for(proc, path, "REACT_DONE")
        proc.send_signal(signal.SIGINT)
        output = finished(proc, path, port)
    assert all(error in output for error in TARGET_ERRORS), output


def test_no_runtime_server_and_standalone_runtime_keep_real_mcp_lifecycle(tmp_path):
    with running(tmp_path, "external") as (parent, path, port):
        wait_for(parent, path, "MCP tools live")
        with path.open("a") as output:
            child = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("runtime_shutdown_app.py")),
                    "child",
                    f"http://127.0.0.1:{port}",
                    "standalone",
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
                env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"},
            )
            try:
                wait_for(child, path, "REACT_DONE")
                child.send_signal(signal.SIGINT)
                child.wait(timeout=5)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
        parent.send_signal(signal.SIGINT)
        parent.wait(timeout=5)
    output = path.read_text()
    assert child.returncode == 0, output
    assert "CLOSE_MCP_DONE" in output
    assert parent.returncode == 0, output
    assert not any(error in output for error in (*TARGET_ERRORS, "cancel scope"))
