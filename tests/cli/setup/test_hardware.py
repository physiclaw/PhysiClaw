"""Tests for `physiclaw.cli.setup.hardware` — interactive setup helpers.

The full `run()` flow is interactive and 12 steps deep; we cover the
testable helpers (api / ok / lan_ip / calibrate / calibrate_retry /
ask / _done / _fail / _photo_booth_adjust) and the early-exit
branches of `run` (server down, already ready, already calibrated)
plus the `hardware` typer entry point.
"""
from __future__ import annotations

import importlib
import io
import json
from unittest.mock import MagicMock

import pytest
import typer
from typer.testing import CliRunner

hw_mod = importlib.import_module("physiclaw.cli.setup.hardware")

app = typer.Typer()
app.command()(hw_mod.hardware)
runner = CliRunner()


# ---------- api ----------


def _resp(payload: dict, status: int = 200) -> MagicMock:
    """Build a context-manager-compatible urllib response."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: None
    return resp


def test_api_get_no_body(mocker) -> None:
    spy = mocker.patch.object(
        hw_mod.urllib.request, "urlopen",
        return_value=_resp({"x": 1}),
    )

    out = hw_mod.api("GET", "/api/status")

    assert out == {"x": 1}
    req = spy.call_args.args[0]
    assert req.method == "GET"


def test_api_post_with_body(mocker) -> None:
    spy = mocker.patch.object(
        hw_mod.urllib.request, "urlopen",
        return_value=_resp({"status": "ok"}),
    )

    out = hw_mod.api("POST", "/api/x", body={"k": "v"})

    assert out["status"] == "ok"
    req = spy.call_args.args[0]
    assert req.headers["Content-type"] == "application/json"


def test_api_post_no_body_sends_empty_bytes(mocker) -> None:
    spy = mocker.patch.object(
        hw_mod.urllib.request, "urlopen",
        return_value=_resp({"status": "ok"}),
    )

    hw_mod.api("POST", "/api/x")

    # Empty bytes body for POST.
    req = spy.call_args.args[0]
    assert req.data == b""


def test_api_returns_parsed_error_body_on_http_error(mocker) -> None:
    err = hw_mod.urllib.error.HTTPError(
        url="x", code=500, msg="boom", hdrs=None, fp=None,
    )
    err.read = lambda: b'{"status": "error", "message": "x"}'
    mocker.patch.object(hw_mod.urllib.request, "urlopen", side_effect=err)

    out = hw_mod.api("GET", "/api/x")

    assert out == {"status": "error", "message": "x"}


def test_api_returns_none_on_unparseable_error_body(mocker) -> None:
    err = hw_mod.urllib.error.HTTPError(
        url="x", code=500, msg="boom", hdrs=None, fp=None,
    )
    err.read = lambda: b"not json"
    mocker.patch.object(hw_mod.urllib.request, "urlopen", side_effect=err)

    assert hw_mod.api("GET", "/api/x") is None


def test_api_returns_none_on_connection_error(mocker) -> None:
    mocker.patch.object(
        hw_mod.urllib.request, "urlopen",
        side_effect=ConnectionError("refused"),
    )

    assert hw_mod.api("GET", "/api/x") is None


# ---------- ok ----------


def test_ok_returns_true_for_ok_dict() -> None:
    assert hw_mod.ok({"status": "ok"}) is True


def test_ok_returns_false_for_none() -> None:
    assert hw_mod.ok(None) is False


def test_ok_returns_false_for_other_status() -> None:
    assert hw_mod.ok({"status": "error"}) is False


# ---------- lan_ip ----------


def test_lan_ip_returns_resolved(mocker) -> None:
    fake_sock = MagicMock()
    fake_sock.getsockname.return_value = ("192.168.1.5", 0)
    mocker.patch.object(hw_mod.socket, "socket", return_value=fake_sock)

    assert hw_mod.lan_ip() == "192.168.1.5"


def test_lan_ip_falls_back_on_failure(mocker) -> None:
    mocker.patch.object(hw_mod.socket, "socket", side_effect=OSError("offline"))

    assert hw_mod.lan_ip() == "127.0.0.1"


# ---------- wait + ask ----------


def test_wait_calls_input(mocker) -> None:
    spy = mocker.patch("builtins.input", return_value="")

    hw_mod.wait("press to continue")

    spy.assert_called_once()


def test_ask_auto_returns_true_without_prompt() -> None:
    assert hw_mod.ask("anything", auto=True) is True


def test_ask_q_returns_false(mocker) -> None:
    mocker.patch("builtins.input", return_value="q")

    assert hw_mod.ask("really?", auto=False) is False


def test_ask_other_returns_true(mocker) -> None:
    mocker.patch("builtins.input", return_value="")

    assert hw_mod.ask("really?", auto=False) is True


# ---------- calibrate / calibrate_retry ----------


def test_calibrate_calls_api(mocker) -> None:
    spy = mocker.patch.object(hw_mod, "api", return_value={"status": "ok"})

    hw_mod.calibrate("arm", timeout=120, body={"fresh": True})

    spy.assert_called_once_with(
        "POST", "/api/calibrate/arm", body={"fresh": True}, timeout=120,
    )


def test_calibrate_retry_returns_on_success(mocker) -> None:
    mocker.patch.object(hw_mod, "calibrate", return_value={"status": "ok"})

    out = hw_mod.calibrate_retry(
        "arm", "fail", "retry?", auto=True,
    )

    assert out == {"status": "ok"}


def test_calibrate_retry_auto_exits_on_failure(mocker) -> None:
    mocker.patch.object(
        hw_mod, "calibrate", return_value={"status": "error", "message": "bad"},
    )

    with pytest.raises(SystemExit):
        hw_mod.calibrate_retry("arm", lambda r: f"x {r['message']}", "retry?", auto=True)


def test_calibrate_retry_manual_q_exits(mocker) -> None:
    mocker.patch.object(
        hw_mod, "calibrate", return_value={"status": "error"},
    )
    mocker.patch.object(hw_mod, "ask", return_value=False)

    with pytest.raises(SystemExit):
        hw_mod.calibrate_retry("arm", "fail", "retry?", auto=False)


def test_calibrate_retry_manual_retries_until_success(mocker) -> None:
    responses = iter([
        {"status": "error"},
        {"status": "ok"},
    ])
    mocker.patch.object(
        hw_mod, "calibrate",
        side_effect=lambda *a, **kw: next(responses),
    )
    mocker.patch.object(hw_mod, "ask", return_value=True)

    out = hw_mod.calibrate_retry("arm", "fail", "retry?", auto=False)

    assert out == {"status": "ok"}


def test_calibrate_retry_uses_custom_predicate(mocker) -> None:
    mocker.patch.object(
        hw_mod, "calibrate",
        return_value={"status": "ok", "passed": False},
    )

    with pytest.raises(SystemExit):
        hw_mod.calibrate_retry(
            "arm", "fail", "retry?", auto=True,
            predicate=lambda r: bool(r and r.get("passed")),
        )


# ---------- _done / _fail / _warn ----------


def test_done_prints_green(capsys: pytest.CaptureFixture) -> None:
    hw_mod._done("yay")
    out = capsys.readouterr().out
    assert "yay" in out
    assert "\033[32m" in out


def test_fail_prints_red(capsys: pytest.CaptureFixture) -> None:
    hw_mod._fail("bad")
    out = capsys.readouterr().out
    assert "bad" in out
    assert "\033[31m" in out


def test_warn_prints_yellow(capsys: pytest.CaptureFixture) -> None:
    hw_mod._warn("careful")
    out = capsys.readouterr().out
    assert "careful" in out
    assert "\033[33m" in out


# ---------- _photo_booth_adjust ----------


def test_photo_booth_adjust_opens_quits_and_settles(mocker) -> None:
    run_spy = mocker.patch.object(hw_mod.subprocess, "run")
    sleep_spy = mocker.patch.object(hw_mod.time, "sleep")
    mocker.patch.object(hw_mod, "wait")

    hw_mod._photo_booth_adjust("position")

    # Two subprocess.run calls: open + osascript quit.
    assert run_spy.call_count == 2
    sleep_spy.assert_called_once_with(0.5)


# ---------- run() early-exit branches ----------


def test_run_exits_when_server_down(mocker) -> None:
    mocker.patch.object(hw_mod, "api", return_value=None)

    with pytest.raises(SystemExit) as exc:
        hw_mod.run()

    assert "Server not running" in str(exc.value)


def test_run_returns_when_already_ready(mocker, capsys: pytest.CaptureFixture) -> None:
    mocker.patch.object(hw_mod, "api", return_value={"ready": True, "calibrated": True})

    hw_mod.run()
    out = capsys.readouterr().out

    assert "Already ready" in out


def test_run_finalizes_when_already_calibrated(
    mocker, capsys: pytest.CaptureFixture,
) -> None:
    api_spy = mocker.patch.object(
        hw_mod, "api",
        side_effect=[
            {"ready": False, "calibrated": True},  # GET /api/status
            {"status": "ok"},                       # POST /api/phone/home
            {"status": "ok"},                       # POST /api/ready
        ],
    )
    mocker.patch.object(hw_mod.time, "sleep")

    hw_mod.run()
    out = capsys.readouterr().out

    assert "Already calibrated" in out
    assert "Phone on Home Screen" in out


# ---------- hardware (typer entry) ----------


def test_hardware_sets_base_and_calls_run(mocker) -> None:
    run_spy = mocker.patch.object(hw_mod, "run")

    result = runner.invoke(app, ["--server-url", "http://example.com:9000"])

    assert result.exit_code == 0
    assert hw_mod.BASE == "http://example.com:9000"
    run_spy.assert_called_once_with(auto=False, trace=False)


def test_hardware_passes_auto_and_trace(mocker) -> None:
    run_spy = mocker.patch.object(hw_mod, "run")

    runner.invoke(app, ["--auto", "--trace"])

    run_spy.assert_called_once_with(auto=True, trace=True)


# ---------- run() full happy-path (auto mode) ----------


def test_run_full_auto_path(mocker, tmp_path) -> None:
    """Walk every step in --auto mode with api() stubbed to always succeed."""
    mocker.patch.object(hw_mod.time, "sleep")
    mocker.patch.object(hw_mod.subprocess, "run")
    mocker.patch.object(hw_mod, "_photo_booth_adjust")
    mocker.patch.object(
        hw_mod, "_viewport_cache_candidates",
        return_value=[],  # no cache → fresh measurement.
    )

    # Endpoint-specific responses.
    def fake_api(method, path, body=None, timeout=60):
        if path == "/api/status":
            return {
                "ready": False, "calibrated": False, "bridge": False,
            }
        if path == "/api/connect-arm":
            return {"status": "ok"}
        if path == "/api/connect-camera":
            return {"status": "ok", "index": 2}
        if path.startswith("/api/camera-preview/"):
            return {"image": ""}
        if path == "/api/bridge/switch":
            return {"ok": True}
        if path == "/api/phone/home":
            return {"status": "ok"}
        if path == "/api/ready":
            return {"status": "ok"}
        return {"status": "ok"}

    def fake_calibrate(step, timeout=60, body=None):
        if step == "arm":
            return {
                "status": "ok", "z_tap": -2.5, "pairs": 18,
                "tilt_ratio": 0.001, "aligned": True, "z_cached": False,
            }
        if step == "camera":
            return {
                "status": "ok", "rotation_name": "0°", "coverage": 0.95,
                "issues": [],
            }
        if step == "validate":
            return {"status": "ok", "calibrated": True}
        if step == "assistive-touch/verify":
            return {"status": "ok", "passed": True, "clipboard": {"fetched": False}}
        return {"status": "ok"}

    mocker.patch.object(hw_mod, "api", side_effect=fake_api)
    mocker.patch.object(hw_mod, "calibrate", side_effect=fake_calibrate)

    hw_mod.run(auto=True, trace=True)


def test_run_full_auto_with_warn_issues(mocker) -> None:
    """Step 8 surfaces issues from camera calibrate via _warn."""
    mocker.patch.object(hw_mod.time, "sleep")
    mocker.patch.object(hw_mod.subprocess, "run")
    mocker.patch.object(hw_mod, "_photo_booth_adjust")
    mocker.patch.object(hw_mod, "_viewport_cache_candidates", return_value=[])

    def fake_api(method, path, body=None, timeout=60):
        if path == "/api/status":
            return {"ready": False, "calibrated": False, "bridge": True}
        if path == "/api/connect-camera":
            return {"status": "ok", "index": 1}
        if path == "/api/bridge/switch":
            return {"ok": True}
        return {"status": "ok"}

    def fake_calibrate(step, timeout=60, body=None):
        if step == "arm":
            return {
                "status": "ok", "z_tap": -2.5, "pairs": 18,
                "tilt_ratio": 0.5, "aligned": False, "z_cached": True,
            }
        if step == "camera":
            return {
                "status": "ok", "rotation_name": "0°", "coverage": 0.5,
                "issues": ["phone partially out of frame"],
            }
        if step == "validate":
            return {"status": "ok", "calibrated": True}
        if step == "assistive-touch/verify":
            return {
                "status": "ok", "passed": True,
                "clipboard": {"fetched": True, "text": "PhysiClaw OK"},
            }
        return {"status": "ok"}

    mocker.patch.object(hw_mod, "api", side_effect=fake_api)
    mocker.patch.object(hw_mod, "calibrate", side_effect=fake_calibrate)

    hw_mod.run(auto=True, trace=False)


def test_run_arm_connect_failure_exits(mocker) -> None:
    mocker.patch.object(hw_mod.time, "sleep")
    mocker.patch.object(hw_mod, "_photo_booth_adjust")
    mocker.patch.object(hw_mod, "_viewport_cache_candidates", return_value=[])

    def fake_api(method, path, body=None, timeout=60):
        if path == "/api/status":
            return {"ready": False, "calibrated": False, "bridge": True}
        if path == "/api/connect-arm":
            return {"status": "error", "message": "no port"}
        return {"status": "ok"}

    mocker.patch.object(hw_mod, "api", side_effect=fake_api)

    with pytest.raises(SystemExit):
        hw_mod.run(auto=True, trace=False)


def test_run_camera_auto_pick_falls_back_to_manual(mocker) -> None:
    mocker.patch.object(hw_mod.time, "sleep")
    mocker.patch.object(hw_mod.subprocess, "run")
    mocker.patch.object(hw_mod, "_photo_booth_adjust")
    mocker.patch.object(hw_mod, "_viewport_cache_candidates", return_value=[])

    call_count = {"connect": 0}

    def fake_api(method, path, body=None, timeout=60):
        if path == "/api/status":
            return {"ready": False, "calibrated": False, "bridge": True}
        if path == "/api/connect-arm":
            return {"status": "ok"}
        if path == "/api/connect-camera":
            call_count["connect"] += 1
            # First auto-pick fails, second by-index succeeds, third in step 8 succeeds.
            if call_count["connect"] == 1:
                return {"status": "error"}
            return {"status": "ok", "index": 0}
        if path.startswith("/api/camera-preview/"):
            return {"image": "Zg=="}  # base64 for "f"
        if path == "/api/bridge/switch":
            return {"ok": True}
        return {"status": "ok"}

    def fake_calibrate(step, timeout=60, body=None):
        return {
            "status": "ok", "z_tap": -2.5, "pairs": 18,
            "tilt_ratio": 0.001, "aligned": True, "z_cached": False,
            "rotation_name": "0°", "coverage": 0.95, "issues": [],
            "calibrated": True, "passed": True,
            "clipboard": {"fetched": False},
        }

    mocker.patch.object(hw_mod, "api", side_effect=fake_api)
    mocker.patch.object(hw_mod, "calibrate", side_effect=fake_calibrate)

    hw_mod.run(auto=True, trace=False)


def test_run_uses_cached_viewport_in_auto_mode(
    mocker, tmp_path, capsys: pytest.CaptureFixture,
) -> None:
    cache = tmp_path / "viewport.png"
    cache.write_bytes(b"x")
    mocker.patch.object(hw_mod, "_viewport_cache_candidates", return_value=[cache])
    mocker.patch.object(hw_mod.time, "sleep")
    mocker.patch.object(hw_mod, "_photo_booth_adjust")

    def fake_api(method, path, body=None, timeout=60):
        if path == "/api/status":
            return {"ready": False, "calibrated": False, "bridge": True}
        if path == "/api/bridge/switch":
            return {"ok": True}
        if path == "/api/connect-camera":
            return {"status": "ok", "index": 0}
        return {"status": "ok"}

    def fake_calibrate(step, timeout=60, body=None):
        return {
            "status": "ok", "z_tap": -2.5, "pairs": 18,
            "tilt_ratio": 0.001, "aligned": True, "z_cached": False,
            "rotation_name": "0°", "coverage": 0.95, "issues": [],
            "calibrated": True, "passed": True,
            "clipboard": {"fetched": False},
        }

    mocker.patch.object(hw_mod, "api", side_effect=fake_api)
    mocker.patch.object(hw_mod, "calibrate", side_effect=fake_calibrate)

    hw_mod.run(auto=True, trace=False)
    out = capsys.readouterr().out

    assert "Using cached screenshot" in out
