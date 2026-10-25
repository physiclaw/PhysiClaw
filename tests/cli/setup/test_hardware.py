"""Hardware setup through its CLI, automatic worker, and helper APIs.

The wizard fixture models HTTP responses and elapsed time while isolating
hardware and desktop apps. Mutation review accepts cosmetic wording/borders,
one-second changes to individual operation timeouts, diagnostic fallbacks,
and equivalent nonnegative elapsed-time arithmetic. Required acknowledgments,
polling deadlines, request bodies, and resource cleanup remain asserted.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest
import typer
from typer.testing import CliRunner

hw_mod = importlib.import_module("physiclaw.cli.setup.hardware")

app = typer.Typer()
app.command()(hw_mod.hardware)
runner = CliRunner()


# ---------- api ----------


def test_api_binds_the_module_base(mocker) -> None:
    # The transport is unit-tested in tests/cli/test__http.py — this pins
    # the wiring: the wizard's api() calls _http.api with the mutable
    # module-global BASE.
    spy = mocker.patch.object(hw_mod._http, "api", return_value={"x": 1})
    mocker.patch.object(hw_mod, "BASE", "http://example:1234")

    out = hw_mod.api("GET", "/api/status", timeout=5)

    assert out == {"x": 1}
    spy.assert_called_once_with(
        "http://example:1234", "GET", "/api/status", body=None, timeout=5
    )


# ---------- ok ----------


def test_ok_returns_true_for_ok_dict() -> None:
    assert hw_mod.ok({"status": "ok"}) is True


def test_ok_returns_false_for_none() -> None:
    assert hw_mod.ok(None) is False


def test_ok_returns_false_for_other_status() -> None:
    assert hw_mod.ok({"status": "error"}) is False


# ---------- lan_ip ----------


def test_lan_ip_returns_resolved(mocker) -> None:
    from physiclaw.core.bridge import lan

    fake_sock = MagicMock()
    fake_sock.__enter__.return_value = fake_sock
    fake_sock.getsockname.return_value = ("192.168.1.5", 0)
    mocker.patch.object(lan.socket, "socket", return_value=fake_sock)

    assert hw_mod.lan_ip() == "192.168.1.5"


def test_lan_ip_falls_back_on_failure(mocker) -> None:
    from physiclaw.core.bridge import lan

    mocker.patch.object(lan.socket, "socket", side_effect=OSError("offline"))

    assert hw_mod.lan_ip() == "127.0.0.1"


# ---------- wait + ask ----------


def test_wait_calls_input(mocker) -> None:
    spy = mocker.patch("builtins.input", return_value="")

    hw_mod.wait("press to continue")

    spy.assert_called_once()


def test_ask_auto_returns_true_without_prompt() -> None:
    assert hw_mod.ask("anything", auto=True) is True


def test_ask_q_quits_the_wizard(mocker, capsys: pytest.CaptureFixture) -> None:
    """Every ask() gates a mandatory step — 'q' must abort the wizard,
    not skip the step and fall through to a false OK."""
    mocker.patch("builtins.input", return_value="q")

    with pytest.raises(SystemExit):
        hw_mod.ask("really?", auto=False)

    assert "aborted" in capsys.readouterr().out


def test_ask_other_returns_true(mocker) -> None:
    mocker.patch("builtins.input", return_value="")

    assert hw_mod.ask("really?", auto=False) is True


def test_base_port_parses_explicit_port(monkeypatch) -> None:
    monkeypatch.setattr(hw_mod, "BASE", "http://localhost:9000")

    assert hw_mod._base_port() == 9000


def test_base_port_defaults_when_port_omitted(monkeypatch) -> None:
    """PHYSICLAW_SERVER without a port must not crash the wizard."""
    monkeypatch.setattr(hw_mod, "BASE", "http://myhost")

    assert hw_mod._base_port() == 8048


# ---------- calibrate / calibrate_retry ----------


def test_calibrate_calls_api(mocker) -> None:
    spy = mocker.patch.object(hw_mod, "api", return_value={"status": "ok"})

    hw_mod.calibrate("arm", timeout=120, body={"fresh": True})

    spy.assert_called_once_with(
        "POST",
        "/api/calibrate/arm",
        body={"fresh": True},
        timeout=120,
    )


def test_calibrate_retry_returns_on_success(mocker) -> None:
    mocker.patch.object(hw_mod, "calibrate", return_value={"status": "ok"})

    out = hw_mod.calibrate_retry(
        "arm",
        "fail",
        "retry?",
        auto=True,
    )

    assert out == {"status": "ok"}


def test_calibrate_retry_auto_exits_on_failure(mocker) -> None:
    mocker.patch.object(
        hw_mod,
        "calibrate",
        return_value={"status": "error", "message": "bad"},
    )

    with pytest.raises(SystemExit):
        hw_mod.calibrate_retry(
            "arm", lambda r: f"x {r['message']}", "retry?", auto=True
        )


def test_calibrate_retry_manual_q_exits(mocker) -> None:
    mocker.patch.object(
        hw_mod,
        "calibrate",
        return_value={"status": "error"},
    )
    mocker.patch("builtins.input", return_value="q")

    with pytest.raises(SystemExit):
        hw_mod.calibrate_retry("arm", "fail", "retry?", auto=False)


def test_calibrate_retry_manual_retries_until_success(mocker) -> None:
    responses = iter(
        [
            {"status": "error"},
            {"status": "ok"},
        ]
    )
    mocker.patch.object(
        hw_mod,
        "calibrate",
        side_effect=lambda *a, **kw: next(responses),
    )
    mocker.patch.object(hw_mod, "ask", return_value=True)

    out = hw_mod.calibrate_retry("arm", "fail", "retry?", auto=False)

    assert out == {"status": "ok"}


def test_calibrate_retry_uses_custom_predicate(mocker) -> None:
    mocker.patch.object(
        hw_mod,
        "calibrate",
        return_value={"status": "ok", "passed": False},
    )

    with pytest.raises(SystemExit):
        hw_mod.calibrate_retry(
            "arm",
            "fail",
            "retry?",
            auto=True,
            predicate=lambda r: bool(r and r.get("passed")),
        )


# ---------- _done / _fail / _warn ----------


def test_done_prints_green(capsys: pytest.CaptureFixture, mocker) -> None:
    mocker.patch("physiclaw.cli._format.color_enabled", return_value=True)
    hw_mod._done("yay")
    out = capsys.readouterr().out
    assert "yay" in out
    assert "\033[32m" in out


def test_fail_prints_red(capsys: pytest.CaptureFixture, mocker) -> None:
    mocker.patch("physiclaw.cli._format.color_enabled", return_value=True)
    hw_mod._fail("bad")
    out = capsys.readouterr().out
    assert "bad" in out
    assert "\033[31m" in out


def test_warn_prints_yellow(capsys: pytest.CaptureFixture, mocker) -> None:
    mocker.patch("physiclaw.cli._format.color_enabled", return_value=True)
    hw_mod._warn("careful")
    out = capsys.readouterr().out
    assert "careful" in out
    assert "\033[33m" in out


# ---------- _camera_aim_adjust ----------


def test_camera_aim_adjust_opens_waits_quits(mocker) -> None:
    open_spy = mocker.patch.object(hw_mod.platform, "open_camera_aim_app")
    quit_spy = mocker.patch.object(hw_mod.platform, "quit_camera_aim_app")
    wait_spy = mocker.patch.object(hw_mod, "wait")
    api_spy = mocker.patch.object(hw_mod, "api", return_value={"status": "ok"})

    hw_mod._camera_aim_adjust("position")

    api_spy.assert_called_once_with("POST", "/api/disconnect-camera")
    open_spy.assert_called_once()
    wait_spy.assert_called_once_with("position")
    quit_spy.assert_called_once()
    # Disconnect must happen before the aim app opens.
    assert api_spy.call_args_list[0] == mocker.call("POST", "/api/disconnect-camera")


# ---------- _step_connect_camera fallback ----------


def _patch_camera_fallback_api(mocker, tmp_path, *, previews, connect_by_index):
    """Stub api() for the camera step: auto-pick always fails, previews and
    the by-index connect come from the arguments. Returns the api mock."""
    mocker.patch.object(hw_mod.tempfile, "gettempdir", return_value=str(tmp_path))

    def fake_api(method, path, body=None, timeout=60):
        if path == "/api/connect-camera":
            if body == {"index": "auto"}:
                return {"status": "error"}
            return connect_by_index
        if path.startswith("/api/camera-preview/"):
            return previews
        return {"status": "ok"}

    return mocker.patch.object(hw_mod, "api", side_effect=fake_api)


def test_step_connect_camera_manual_prompt_picks_index(mocker, tmp_path) -> None:
    """Auto-pick fails interactively: previews are written and opened, and
    the typed index is connected."""
    _patch_camera_fallback_api(
        mocker,
        tmp_path,
        previews={"image": "Zg=="},  # base64 for "f"
        connect_by_index={"status": "ok", "index": 2},
    )
    open_spy = mocker.patch.object(hw_mod.platform, "open_image_files")
    mocker.patch("builtins.input", return_value="2")

    cam = hw_mod._step_connect_camera(auto=False)

    assert cam == 2
    from pathlib import Path

    opened = open_spy.call_args.args[0]
    assert [Path(path).name for path in opened] == [f"camera{i}.jpg" for i in range(4)]
    assert not any(Path(path).exists() for path in opened)


def test_step_connect_camera_manual_invalid_input_defaults_to_zero(
    mocker, tmp_path
) -> None:
    """A non-numeric answer at the manual prompt falls back to camera 0,
    and blank preview frames are not written or opened."""
    _patch_camera_fallback_api(
        mocker,
        tmp_path,
        previews=None,  # preview endpoint has nothing to show
        connect_by_index={"status": "ok", "index": 0},
    )
    open_spy = mocker.patch.object(hw_mod.platform, "open_image_files")
    mocker.patch("builtins.input", return_value="not a number")

    cam = hw_mod._step_connect_camera(auto=False)

    assert cam == 0
    assert list(tmp_path.glob("physiclaw_cam*.jpg")) == []
    open_spy.assert_called_once_with([])


def test_step_connect_camera_fallback_connect_failure_exits(mocker, tmp_path) -> None:
    """A failed automatic fallback leaves other sessions' previews alone."""
    stale = tmp_path / "physiclaw_cam9.jpg"
    stale.write_bytes(b"old")
    _patch_camera_fallback_api(
        mocker,
        tmp_path,
        previews={"image": ""},  # response without a usable frame
        connect_by_index={"status": "error"},
    )

    with pytest.raises(SystemExit):
        hw_mod._step_connect_camera(auto=True)

    assert stale.read_bytes() == b"old"


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

    assert "already ready" in out


def test_run_finalizes_when_already_calibrated(
    mocker,
    capsys: pytest.CaptureFixture,
) -> None:
    mocker.patch.object(
        hw_mod,
        "api",
        side_effect=[
            {"ready": False, "calibrated": True},  # GET /api/status
            {"ok": True},  # POST /api/phone/home
            {"ok": True},  # POST /api/ready
            {"ready": False},  # ready poll: settle still running
            {"ready": True},  # ready poll: flipped
        ],
    )
    mocker.patch.object(hw_mod.time, "sleep")

    hw_mod.run()
    out = capsys.readouterr().out

    assert "Already calibrated" in out
    assert "PhysiClaw is ready" in out


# ---------- hardware (typer entry) ----------


@pytest.mark.parametrize(
    "responses",
    [
        [{"ready": False, "calibrated": True}, {"ok": True}, None],
        [{"ready": False, "calibrated": True}, {"ok": True}, {"ok": True}, None],
        [{"ready": False, "calibrated": True}, {"ok": True}, {"ok": True}]
        + [{"ready": False}] * 48,
    ],
    ids=["ready-request-fails", "server-disappears", "readiness-times-out"],
)
def test_regression_setup_does_not_report_unconfirmed_readiness(
    mocker, capsys, responses
):
    mocker.patch.object(hw_mod, "api", side_effect=responses)
    mocker.patch.object(hw_mod.time, "sleep")
    mocker.patch.object(hw_mod.time, "monotonic", side_effect=iter(range(1000)))

    with pytest.raises(SystemExit, match="Readiness was not confirmed"):
        hw_mod.run(auto=True)

    assert "PhysiClaw is ready" not in capsys.readouterr().out


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


# ---------- _poll_bridge ----------


def test_poll_bridge_returns_true_when_bridge_appears(mocker) -> None:
    mocker.patch.object(hw_mod.time, "sleep")
    mocker.patch.object(
        hw_mod,
        "api",
        side_effect=[{"bridge": False}, {"bridge": False}, {"bridge": True}],
    )

    assert hw_mod._poll_bridge() is True


def test_poll_bridge_times_out_without_bridge(mocker) -> None:
    # Fake clock: each monotonic() call advances 1s, so the 15s deadline
    # expires after ~15 iterations without any real waiting.
    mocker.patch.object(hw_mod.time, "sleep")
    clock = iter(range(1000))
    mocker.patch.object(hw_mod.time, "monotonic", side_effect=lambda: next(clock))
    api = mocker.patch.object(hw_mod, "api", return_value={"bridge": False})

    assert hw_mod._poll_bridge() is False
    assert api.call_count > 1  # it really polled, not a single-shot check


# ---------- run() full happy-path (auto mode) ----------


def _ready_after_request(api):
    """Model the server completing readiness after its POST is accepted."""
    ready = False

    def request(method, path, *args, **kwargs):
        nonlocal ready
        response = api(method, path, *args, **kwargs)
        if path in {"/api/phone/home", "/api/ready"}:
            response = {"ok": True}
        if method == "POST" and path == "/api/ready":
            ready = True
        if path == "/api/status" and response is not None:
            return {**response, "ready": ready}
        return response

    return request


def test_run_full_auto_path(mocker, tmp_path) -> None:
    """Walk every step in --auto mode with api() stubbed to always succeed."""
    mocker.patch.object(hw_mod.time, "sleep")
    mocker.patch.object(hw_mod, "_camera_aim_adjust")
    mocker.patch.object(
        hw_mod,
        "_viewport_cache_candidates",
        return_value=[],  # no cache → fresh measurement.
    )

    # Endpoint-specific responses. bridge=True per the "always succeed"
    # contract — the phone step verifies it now, and False would spin
    # _poll_bridge's 15s deadline against the real clock.
    def fake_api(method, path, body=None, timeout=60):
        if path == "/api/status":
            return {
                "ready": False,
                "calibrated": False,
                "bridge": True,
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
                "status": "ok",
                "pairs": 18,
                "tilt_ratio": 0.001,
                "aligned": True,
            }
        if step == "camera":
            return {
                "status": "ok",
                "rotation_name": "0°",
                "coverage": 0.95,
                "issues": [],
            }
        if step == "validate":
            return {"status": "ok", "calibrated": True}
        if step == "assistive-touch/verify":
            return {"status": "ok", "passed": True, "clipboard": {"fetched": False}}
        return {"status": "ok"}

    mocker.patch.object(hw_mod, "api", side_effect=_ready_after_request(fake_api))
    mocker.patch.object(hw_mod, "calibrate", side_effect=fake_calibrate)

    hw_mod.run(auto=True, trace=True)


def test_run_full_auto_with_warn_issues(mocker) -> None:
    """The camera-calibration step surfaces issues from calibrate via _warn."""
    mocker.patch.object(hw_mod.time, "sleep")
    mocker.patch.object(hw_mod, "_camera_aim_adjust")
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
                "status": "ok",
                "pairs": 18,
                "tilt_ratio": 0.5,
                "aligned": False,
            }
        if step == "camera":
            return {
                "status": "ok",
                "rotation_name": "0°",
                "coverage": 0.5,
                "issues": ["phone partially out of frame"],
            }
        if step == "validate":
            return {"status": "ok", "calibrated": True}
        if step == "assistive-touch/verify":
            return {
                "status": "ok",
                "passed": True,
                "clipboard": {"fetched": True, "text": "PhysiClaw OK"},
            }
        return {"status": "ok"}

    mocker.patch.object(hw_mod, "api", side_effect=_ready_after_request(fake_api))
    mocker.patch.object(hw_mod, "calibrate", side_effect=fake_calibrate)

    hw_mod.run(auto=True, trace=False)


def test_run_arm_connect_failure_exits(mocker) -> None:
    mocker.patch.object(hw_mod.time, "sleep")
    mocker.patch.object(hw_mod, "_camera_aim_adjust")
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


def test_run_camera_auto_pick_falls_back_to_manual(mocker, tmp_path) -> None:
    mocker.patch.object(hw_mod.time, "sleep")
    mocker.patch.object(hw_mod.tempfile, "gettempdir", return_value=str(tmp_path))
    mocker.patch.object(hw_mod.platform, "open_image_files")
    mocker.patch.object(hw_mod, "_camera_aim_adjust")
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
            "status": "ok",
            "pairs": 18,
            "tilt_ratio": 0.001,
            "aligned": True,
            "rotation_name": "0°",
            "coverage": 0.95,
            "issues": [],
            "calibrated": True,
            "passed": True,
            "clipboard": {"fetched": False},
        }

    mocker.patch.object(hw_mod, "api", side_effect=_ready_after_request(fake_api))
    mocker.patch.object(hw_mod, "calibrate", side_effect=fake_calibrate)

    hw_mod.run(auto=True, trace=False)


@pytest.mark.parametrize("cache_name", ["viewport.img", "viewport.png", "viewport.jpg"])
def test_run_uses_cached_viewport_in_auto_mode(
    mocker,
    tmp_path,
    capsys: pytest.CaptureFixture,
    cache_name,
) -> None:
    cache = tmp_path / cache_name
    cache.write_bytes(b"x")
    mocker.patch.object(hw_mod.paths, "calibration_cache_dir", return_value=tmp_path)
    mocker.patch.object(hw_mod.time, "sleep")
    mocker.patch.object(hw_mod, "_camera_aim_adjust")

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
            "status": "ok",
            "pairs": 18,
            "tilt_ratio": 0.001,
            "aligned": True,
            "rotation_name": "0°",
            "coverage": 0.95,
            "issues": [],
            "calibrated": True,
            "passed": True,
            "clipboard": {"fetched": False},
        }

    mocker.patch.object(hw_mod, "api", side_effect=_ready_after_request(fake_api))
    mocker.patch.object(hw_mod, "calibrate", side_effect=fake_calibrate)

    hw_mod.run(auto=True, trace=False)
    out = capsys.readouterr().out

    assert f"Using cached screenshot: {cache}" in out


# ---------- _step_locate_screen ----------


def test_step_locate_screen_auto_fails_without_prompting(mocker) -> None:
    """Unattended --auto must fail the step like the calibrate_retry
    siblings do — never block on a stdin prompt (hangs `physiclaw auto`)."""
    mocker.patch.object(hw_mod, "_viewport_cache_candidates", return_value=[])
    mocker.patch.object(hw_mod, "calibrate", return_value={"status": "error"})
    ask_spy = mocker.patch.object(hw_mod, "ask")

    with pytest.raises(SystemExit):
        hw_mod._step_locate_screen(auto=True)

    ask_spy.assert_not_called()


def test_step_locate_screen_interactive_prompts_then_retries(mocker) -> None:
    mocker.patch.object(hw_mod, "_viewport_cache_candidates", return_value=[])
    mocker.patch.object(
        hw_mod,
        "calibrate",
        side_effect=[{"status": "error"}, {"status": "ok"}],
    )
    ask_spy = mocker.patch.object(hw_mod, "ask", return_value=True)

    hw_mod._step_locate_screen(auto=False)  # succeeds on the retry

    ask_spy.assert_called_once()


# ---------- await_bridge_and_calibrate ----------


def _patch_auto_worker(mocker, *, port_ok=True):
    """Patch await_bridge_and_calibrate's collaborators: the lazy
    core.server.net.wait_for_port import (faked so the test never loads the
    real core.server package), the HTTP api(), sleep, run(), and the tagged
    logger. `BASE` is patched so the worker's reassignment is restored after
    the test. Returns a dict of the mocks."""
    fake_net = MagicMock()
    fake_net.wait_for_port.return_value = port_ok
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.core.server.net": fake_net},
    )
    mocker.patch("physiclaw.common.logger.make_tagged_logger", return_value=MagicMock())
    mocker.patch.object(
        hw_mod, "BASE", hw_mod.BASE
    )  # restore after (worker mutates it)
    return {
        "api": mocker.patch.object(hw_mod, "api", return_value={"bridge": True}),
        "sleep": mocker.patch.object(hw_mod.time, "sleep"),
        "run": mocker.patch.object(hw_mod, "run"),
    }


def test_await_bridge_and_calibrate_runs_when_bridge_connects(mocker) -> None:
    m = _patch_auto_worker(mocker)

    hw_mod.await_bridge_and_calibrate("127.0.0.1", 8048)

    assert hw_mod.BASE == "http://127.0.0.1:8048"
    m["run"].assert_called_once_with(auto=True)


def test_await_bridge_and_calibrate_skips_on_port_timeout(mocker) -> None:
    m = _patch_auto_worker(mocker, port_ok=False)

    hw_mod.await_bridge_and_calibrate("127.0.0.1", 8048)

    m["run"].assert_not_called()


def test_await_bridge_and_calibrate_polls_until_bridge(mocker) -> None:
    m = _patch_auto_worker(mocker)
    # No response, then not-connected, then connected — waits through both.
    m["api"].side_effect = [None, {"bridge": False}, {"bridge": True}]

    hw_mod.await_bridge_and_calibrate("127.0.0.1", 8048)

    m["run"].assert_called_once_with(auto=True)
    assert m["sleep"].call_count == 2  # one wait per non-connected poll


def test_await_bridge_and_calibrate_starts_on_already_calibrated(mocker) -> None:
    # A resumed server may already be calibrated/ready — proceed (run() itself
    # short-circuits) rather than waiting forever for a fresh bridge flag.
    m = _patch_auto_worker(mocker)
    m["api"].return_value = {"calibrated": True}

    hw_mod.await_bridge_and_calibrate("127.0.0.1", 8048)

    m["run"].assert_called_once_with(auto=True)


def test_await_bridge_and_calibrate_swallows_setup_exit(mocker) -> None:
    # run() calls sys.exit on a step failure — must not crash the server.
    m = _patch_auto_worker(mocker)
    m["run"].side_effect = SystemExit(1)

    hw_mod.await_bridge_and_calibrate("127.0.0.1", 8048)  # must not raise

    m["run"].assert_called_once()


def test_await_bridge_and_calibrate_routes_wizard_output_through_logger(mocker) -> None:
    # The wizard's print() output is badged via LineLogStream, not dumped raw
    # to stdout amid the tagged server logs.
    m = _patch_auto_worker(mocker)
    lines: list[str] = []
    mocker.patch(
        "physiclaw.common.logger.make_tagged_logger",
        return_value=MagicMock(info=lambda x: lines.append(x)),
    )
    m["run"].side_effect = lambda auto: print("  ✓ Phone connected")

    hw_mod.await_bridge_and_calibrate("127.0.0.1", 8048)

    assert "  ✓ Phone connected" in lines


class WizardServer:
    """Owned HTTP boundary with a deterministic clock and route-shaped replies."""

    def __init__(self):
        self.calls = []
        self.overrides = {}
        self.initial = {"ready": False, "calibrated": False, "bridge": True}
        self.ready = False
        self.clock = 0.0
        self.poll_delay = 0.0
        self.ready_delay = 0.0

    def sleep(self, seconds):
        self.clock += seconds

    def request(self, base, method, path, *, body=None, timeout=60):
        self.calls.append((method, path, body, timeout, base))
        expected_method = (
            "GET"
            if path == "/api/status" or path.startswith("/api/camera-preview/")
            else "POST"
        )
        assert method == expected_method, f"wrong method for {path}: {method}"
        if path in self.overrides:
            response = self.overrides[path]
            if isinstance(response, BaseException):
                raise response
            if callable(response):
                return response(method, path, body, timeout)
            return response
        if path == "/api/status":
            if self.ready:
                self.clock += min(timeout, self.poll_delay)
                return {"ready": self.poll_delay == 0}
            return dict(self.initial)
        if path == "/api/ready":
            self.clock += min(timeout, self.ready_delay)
            self.ready = True
            return {"ok": True}
        if path in {"/api/phone/home", "/api/bridge/switch"}:
            return {"ok": True}
        if path == "/api/connect-camera":
            return {"status": "ok", "index": 2}
        if path == "/api/calibrate/arm":
            return {"status": "ok", "pairs": 18, "aligned": True}
        if path == "/api/calibrate/camera":
            return {"status": "ok", "rotation_name": "0°", "coverage": 0.95}
        if path == "/api/calibrate/validate":
            return {"status": "ok", "calibrated": True, "passed": 9, "total": 9}
        if path == "/api/calibrate/assistive-touch/verify":
            return {"status": "ok", "passed": True, "clipboard": {"fetched": False}}
        return {"status": "ok"}

    def paths(self):
        return [call[1] for call in self.calls]


@pytest.fixture
def wizard_server(mocker):
    server = WizardServer()
    mocker.patch.object(hw_mod, "BASE", "http://127.0.0.1:8048")
    mocker.patch.object(hw_mod._http, "api", side_effect=server.request)
    mocker.patch.object(hw_mod.time, "monotonic", side_effect=lambda: server.clock)
    mocker.patch.object(hw_mod.time, "time", side_effect=lambda: server.clock)
    mocker.patch.object(hw_mod.time, "sleep", side_effect=server.sleep)
    mocker.patch.object(hw_mod.platform, "open_camera_aim_app")
    mocker.patch.object(hw_mod.platform, "quit_camera_aim_app")
    mocker.patch.object(hw_mod.platform, "open_image_files")
    mocker.patch.object(hw_mod.platform, "camera_aim_hint", return_value="")
    mocker.patch.object(hw_mod.webbrowser, "open")
    mocker.patch("builtins.input", return_value="")
    return server


@pytest.mark.parametrize(
    "host,expected",
    [("192.0.2.10", "192.0.2.10"), ("::1", "[::1]"), ("0.0.0.0", "127.0.0.1")],
)
def test_regression_auto_setup_dials_configured_host(
    host, expected, wizard_server, mocker
):
    import types

    fake_net = types.ModuleType("physiclaw.core.server.net")
    fake_net.wait_for_port = lambda *_: True
    mocker.patch.dict("sys.modules", {fake_net.__name__: fake_net})
    wizard_server.initial = {"ready": True}

    hw_mod.await_bridge_and_calibrate(host, 9000)

    assert {call[4] for call in wizard_server.calls} == {f"http://{expected}:9000"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/phone/home",
        "/api/calibrate/trace-edge",
        "/api/calibrate/assistive-touch/show",
    ],
)
def test_regression_setup_stops_on_failed_required_request(path, wizard_server):
    wizard_server.overrides[path] = {
        "status": "error",
        "message": "operation failed",
        "error": "operation failed",
    }

    result = runner.invoke(app, ["--auto", "--trace"])

    assert result.exit_code == 1
    assert "operation failed" in result.output
    assert isinstance(result.exception, SystemExit)
    assert "/api/ready" not in wizard_server.paths()
    assert "PhysiClaw is ready" not in result.output


def test_regression_failed_ready_request_never_polls_stale_ready(wizard_server):
    wizard_server.initial = {"calibrated": True}
    wizard_server.overrides["/api/ready"] = {"error": "not accepted"}

    result = runner.invoke(app, ["--auto"])

    assert result.exit_code == 1
    assert wizard_server.paths() == ["/api/status", "/api/phone/home", "/api/ready"]


@pytest.mark.parametrize("poll_delay", [0.1, 1, 5, 1000])
def test_regression_readiness_timeout_bounds_http_polling(poll_delay, wizard_server):
    wizard_server.initial = {"calibrated": True}
    wizard_server.poll_delay = poll_delay
    wizard_server.ready_delay = 4

    result = runner.invoke(app, ["--auto"])

    assert result.exit_code == 1
    assert (
        wizard_server.clock <= 48
    )  # Three-second home settle plus 45-second readiness.
    assert "Readiness was not confirmed" in result.output


def test_regression_camera_aim_app_closes_when_operator_interrupts(
    wizard_server, mocker
):
    def answer(prompt):
        if "Adjust the camera" in prompt:
            raise KeyboardInterrupt
        return ""

    mocker.patch("builtins.input", side_effect=answer)

    result = runner.invoke(app, [])

    assert result.exit_code == 130
    hw_mod.platform.open_camera_aim_app.assert_called_once()
    hw_mod.platform.quit_camera_aim_app.assert_called_once()
    assert "/api/ready" not in wizard_server.paths()


def test_regression_camera_aim_does_not_open_when_disconnect_fails(wizard_server):
    wizard_server.overrides["/api/disconnect-camera"] = {
        "status": "error",
        "message": "camera busy",
    }

    result = runner.invoke(app, [])

    assert result.exit_code == 1
    assert "camera busy" in result.output
    hw_mod.platform.open_camera_aim_app.assert_not_called()


def test_regression_auto_setup_stops_before_arm_when_phone_absent(
    wizard_server, mocker
):
    wizard_server.initial["bridge"] = False
    mocker.patch.object(hw_mod, "lan_ip", return_value="192.0.2.10")

    result = runner.invoke(app, ["--auto"])

    assert result.exit_code == 1
    assert "/api/connect-arm" not in wizard_server.paths()


def test_regression_camera_preview_uses_owned_temporary_directory(
    wizard_server, mocker, tmp_path
):
    stale = tmp_path / "physiclaw_cam0.jpg"
    stale.write_bytes(b"another setup session")
    mocker.patch.object(hw_mod.tempfile, "gettempdir", return_value=str(tmp_path))
    wizard_server.overrides["/api/connect-camera"] = {"status": "error"}
    wizard_server.overrides["/api/camera-preview/0?watermark=1"] = {"image": "Zg=="}
    previews = []

    def inspect(paths):
        from pathlib import Path

        previews.extend(Path(path) for path in paths)
        assert previews[0].read_bytes() == b"f"

    hw_mod.platform.open_image_files.side_effect = inspect

    result = runner.invoke(app, [])

    assert result.exit_code == 1
    assert stale.read_bytes() == b"another setup session"
    assert len(previews) == 1
    assert not previews[0].exists()


def test_regression_cli_server_url_normalizes_trailing_slash(wizard_server):
    wizard_server.initial = {"ready": True}

    result = runner.invoke(app, ["--server-url", "http://server:9000/"])

    assert result.exit_code == 0
    assert wizard_server.calls[0][4] == "http://server:9000"


def test_setup_interactive_happy_path_uses_verified_steps(
    wizard_server, mocker, tmp_path
):
    cache = tmp_path / "viewport.img"
    cache.write_bytes(b"cached")
    mocker.patch.object(hw_mod.paths, "calibration_cache_dir", return_value=tmp_path)
    hw_mod.platform.camera_aim_hint.return_value = "Use the OS camera preview."
    wizard_server.overrides["/api/calibrate/assistive-touch/verify"] = {
        "status": "ok",
        "passed": True,
        "clipboard": {"fetched": True, "text": "match"},
    }

    result = runner.invoke(app, ["--trace"])

    assert result.exit_code == 0, result.output
    assert wizard_server.paths() == [
        "/api/status",
        "/api/connect-arm",
        "/api/connect-camera",
        "/api/calibrate/viewport-shift",
        "/api/bridge/switch",
        "/api/calibrate/arm",
        "/api/disconnect-camera",
        "/api/connect-camera",
        "/api/calibrate/camera",
        "/api/calibrate/camera-mapping",
        "/api/calibrate/validate",
        "/api/calibrate/assistive-touch/show",
        "/api/calibrate/assistive-touch/verify",
        "/api/calibrate/trace-edge",
        "/api/phone/home",
        "/api/ready",
        "/api/status",
    ]
    assert "ignored (interactive: fresh measurement)" in result.output
    assert "Use the OS camera preview." in result.output
    assert "Clipboard text: match" in result.output
    assert "Arm calibrated — mapped 18 points" in result.output
    assert "Validated — 9/9 taps on target" in result.output
    assert "Camera calibrated — found all 15 dots" in result.output
    assert "PhysiClaw is ready" in result.output
    hw_mod.platform.quit_camera_aim_app.assert_called_once()


@pytest.mark.parametrize(
    "path,message",
    [
        ("/api/bridge/switch", ("Couldn't show the center circle",)),
        ("/api/connect-arm", ("Couldn't connect",)),
        ("/api/calibrate/arm", ("Couldn't calibrate: failed",)),
        ("/api/calibrate/camera", ("Couldn't read the camera: failed",)),
        ("/api/calibrate/camera-mapping", ("Couldn't map the dots: failed",)),
        ("/api/calibrate/validate", ("Validation failed",)),
        (
            "/api/calibrate/assistive-touch/verify",
            ("Couldn't verify", "clipboard fetched: 'stale'"),
        ),
    ],
)
def test_setup_calibration_failure_stops_before_readiness(path, message, wizard_server):
    wizard_server.overrides[path] = {
        "status": "error",
        "message": "failed",
        "clipboard": {"fetched": True, "text": "stale"},
    }

    result = runner.invoke(app, ["--auto"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert all(fragment in result.output for fragment in message)
    assert "/api/ready" not in wizard_server.paths()
    assert "PhysiClaw is ready" not in result.output


@pytest.mark.parametrize(
    "path,field",
    [
        ("/api/bridge/switch", "ok"),
        ("/api/calibrate/validate", "calibrated"),
        ("/api/calibrate/assistive-touch/verify", "passed"),
    ],
)
def test_setup_requires_boolean_confirmation(path, field, wizard_server):
    wizard_server.overrides[path] = {"status": "ok", field: "false"}

    result = runner.invoke(app, ["--auto"])

    assert result.exit_code == 1
    assert "/api/ready" not in wizard_server.paths()


def test_setup_stops_if_camera_cannot_reopen(wizard_server):
    calls = iter(
        [{"status": "ok", "index": 2}, {"status": "error", "message": "still in use"}]
    )
    wizard_server.overrides["/api/connect-camera"] = lambda *_: next(calls)

    result = runner.invoke(app, [])

    assert result.exit_code == 1
    assert "Couldn't reopen the camera: still in use" in result.output
    assert "/api/calibrate/camera" not in wizard_server.paths()


def test_setup_phone_qr_waits_for_connection(wizard_server, mocker):
    statuses = iter([{"bridge": False}, {"bridge": True}, {"ready": True}])
    wizard_server.overrides["/api/status"] = lambda *_: next(statuses)
    mocker.patch.object(hw_mod, "lan_ip", return_value="192.0.2.10")

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    hw_mod.webbrowser.open.assert_called_once_with(
        "http://127.0.0.1:8048/api/bridge/qr"
    )
    assert "http://192.0.2.10:8049/bridge" in result.output


def test_setup_operator_can_abort_phone_retry(wizard_server, mocker):
    wizard_server.initial["bridge"] = False
    mocker.patch.object(hw_mod, "lan_ip", return_value="192.0.2.10")
    mocker.patch("builtins.input", side_effect=["", "q"])

    result = runner.invoke(app, [])

    assert result.exit_code == 1
    assert "Setup aborted" in result.output
    assert "/api/connect-arm" not in wizard_server.paths()


@pytest.mark.parametrize("payload", [[], True, "ready", 3])
def test_setup_rejects_non_object_api_status(payload, wizard_server):
    wizard_server.overrides["/api/status"] = payload

    result = runner.invoke(app, ["--auto"])

    assert result.exit_code == 1
    assert "Server not running" in result.output
    assert wizard_server.paths() == ["/api/status"]


@pytest.mark.parametrize("preview", ["invalid!", "Zg==!", {"bad": "image"}])
def test_setup_invalid_preview_is_reported_and_manual_selection_continues(
    preview, wizard_server
):
    calls = iter(
        [
            {"status": "error"},
            {"status": "ok", "index": 0},
            {"status": "ok", "index": 0},
        ]
    )
    wizard_server.overrides["/api/connect-camera"] = lambda *_: next(calls)
    wizard_server.overrides["/api/camera-preview/0?watermark=1"] = {"image": preview}

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert "Camera 0 preview unavailable" in result.output
    hw_mod.platform.open_image_files.assert_called_once_with([])


def test_auto_setup_does_not_fetch_unused_camera_previews(wizard_server):
    calls = iter([{"status": "error"}, {"status": "ok", "index": 0}])
    wizard_server.overrides["/api/connect-camera"] = lambda *_: next(calls)

    result = runner.invoke(app, ["--auto"])

    assert result.exit_code == 0, result.output
    assert not any("camera-preview" in path for path in wizard_server.paths())
    assert [c[2] for c in wizard_server.calls if c[1] == "/api/connect-camera"] == [
        {"index": "auto"},
        {"index": 0},
    ]
    hw_mod.platform.open_image_files.assert_not_called()


def test_auto_worker_reports_unexpected_calibration_exception(
    wizard_server, mocker, caplog
):
    import types

    fake_net = types.ModuleType("physiclaw.core.server.net")
    fake_net.wait_for_port = lambda *_: True
    mocker.patch.dict("sys.modules", {fake_net.__name__: fake_net})
    wizard_server.overrides["/api/connect-arm"] = OSError("transport broke")

    hw_mod.await_bridge_and_calibrate("127.0.0.1", 8048)

    assert "auto-calibration failed" in caplog.text
    assert "transport broke" in caplog.text


def test_setup_does_not_treat_text_calibration_flag_as_success(wizard_server):
    wizard_server.initial["calibrated"] = "false"

    result = runner.invoke(app, ["--auto"])

    assert result.exit_code == 0, result.output
    assert "/api/connect-arm" in wizard_server.paths()
    assert "/api/calibrate/validate" in wizard_server.paths()


def test_auto_worker_waits_for_boolean_bridge_confirmation(wizard_server, mocker):
    import types

    fake_net = types.ModuleType("physiclaw.core.server.net")
    fake_net.wait_for_port = lambda *_: True
    mocker.patch.dict("sys.modules", {fake_net.__name__: fake_net})
    statuses = iter([{"bridge": "false"}, {"bridge": True}, {"ready": True}])
    wizard_server.overrides["/api/status"] = lambda *_: next(statuses)

    hw_mod.await_bridge_and_calibrate("127.0.0.1", 8048)

    assert wizard_server.clock == 1
    assert wizard_server.paths() == ["/api/status", "/api/status", "/api/status"]


@pytest.mark.parametrize(
    "args,arm_body,viewport_body,camera_bodies",
    [
        (["--auto"], {"from_park": True}, {"fresh": False}, [{"index": "auto"}]),
        ([], None, {"fresh": True}, [{"index": "auto"}, {"index": 2}]),
    ],
)
def test_setup_sends_mode_specific_calibration_requests(
    args, arm_body, viewport_body, camera_bodies, wizard_server
):
    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert [c[2] for c in wizard_server.calls if c[1] == "/api/calibrate/arm"] == [
        arm_body
    ]
    assert [
        c[2] for c in wizard_server.calls if c[1] == "/api/calibrate/viewport-shift"
    ] == [viewport_body]
    assert [
        c[2] for c in wizard_server.calls if c[1] == "/api/connect-camera"
    ] == camera_bodies
    assert [c[2] for c in wizard_server.calls if c[1] == "/api/bridge/switch"] == [
        {"mode": "calibrate", "phase": "center"}
    ]


@pytest.mark.parametrize(
    "response,message", [(None, "no response"), ({"error": "offline"}, "offline")]
)
def test_setup_home_failure_has_actionable_diagnosis(response, message, wizard_server):
    wizard_server.initial = {"calibrated": True}
    wizard_server.overrides["/api/phone/home"] = response

    result = runner.invoke(app, ["--auto"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert f"Couldn't return the phone home: {message}" in result.output


@pytest.mark.parametrize("first_preview", [None, {"image": "invalid!"}])
def test_setup_skips_missing_or_bad_preview_and_opens_later_camera(
    first_preview, wizard_server
):
    from pathlib import Path

    cameras = iter([{"status": "error"}, {"status": "ok"}, {"status": "ok"}])
    wizard_server.overrides["/api/connect-camera"] = lambda *_: next(cameras)
    wizard_server.overrides["/api/camera-preview/0?watermark=1"] = first_preview
    wizard_server.overrides["/api/camera-preview/1?watermark=1"] = {"image": "Zg=="}

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert [
        Path(p).name for p in hw_mod.platform.open_image_files.call_args.args[0]
    ] == ["camera1.jpg"]


def test_setup_missing_camera_index_defaults_to_zero_on_reopen(wizard_server):
    wizard_server.overrides["/api/connect-camera"] = {"status": "ok"}

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert [c[2] for c in wizard_server.calls if c[1] == "/api/connect-camera"] == [
        {"index": "auto"},
        {"index": 0},
    ]


@pytest.mark.parametrize("args", [[], ["--auto"]])
def test_setup_shows_low_coverage_warning_during_calibration(args, wizard_server):
    issue = "Move camera closer — phone covers only 23% of image (need ≥25%)"
    wizard_server.overrides["/api/calibrate/camera"] = {
        "status": "ok",
        "setup_ok": False,
        "rotation_name": "90°",
        "coverage": 0.23,
        "issues": [issue],
    }

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert result.output.count(issue) == 1
    assert "Camera calibrated — found all 15 dots" in result.output
    assert "PhysiClaw is ready" in result.output

    # A ready rig skips calibration, so the advice is not replayed.
    previous_calls = len(wizard_server.calls)
    resumed = runner.invoke(app, args)
    assert resumed.exit_code == 0, resumed.output
    assert issue not in resumed.output
    assert wizard_server.paths()[previous_calls:] == ["/api/status"]


@pytest.mark.parametrize("aligned", [True, False])
def test_setup_reports_rotation_only_when_alignment_failed(aligned, wizard_server):
    wizard_server.overrides["/api/calibrate/arm"] = {
        "status": "ok",
        "pairs": 18,
        "tilt_ratio": 0.15,
        "aligned": aligned,
    }
    wizard_server.overrides["/api/calibrate/camera"] = {
        "status": "ok",
        "rotation_name": "90°",
        "coverage": 0.8,
        "issues": ["phone partly outside frame"],
    }

    result = runner.invoke(app, ["--auto"])

    assert result.exit_code == 0, result.output
    assert ("Phone looks slightly rotated" in result.output) is not aligned
    assert ("(15.0%)" in result.output) is not aligned
    assert "phone partly outside frame" in result.output
    assert "rotation 90°" in result.output


def test_setup_elapsed_time_is_measured_from_run_start(wizard_server):
    wizard_server.clock = 120

    def calibrate(*_):
        wizard_server.clock += 57
        return {"status": "ok", "pairs": 18, "aligned": True}

    wizard_server.overrides["/api/calibrate/arm"] = calibrate

    result = runner.invoke(app, ["--auto"])

    assert result.exit_code == 0, result.output
    assert "set up in 1m 0s" in result.output


def test_setup_run_defaults_to_interactive_without_edge_trace(wizard_server):
    hw_mod.run()

    hw_mod.platform.open_camera_aim_app.assert_called_once()
    assert "/api/calibrate/trace-edge" not in wizard_server.paths()


def test_setup_phone_timeout_caps_last_sleep(wizard_server, mocker):
    calls = 0

    def status(method, path, body, timeout):
        nonlocal calls
        calls += 1
        wizard_server.clock += 0 if calls == 1 else min(0.3, timeout)
        return {"bridge": False}

    wizard_server.overrides["/api/status"] = status
    mocker.patch.object(hw_mod, "lan_ip", return_value="192.0.2.10")

    result = runner.invoke(app, ["--auto"])

    assert result.exit_code == 1
    assert wizard_server.clock <= 15
    assert "/api/connect-arm" not in wizard_server.paths()


def test_setup_phone_can_connect_in_final_fraction_of_wait(wizard_server, mocker):
    replies = iter(
        [
            (0, {"bridge": False}),
            (4.5, {"bridge": False}),
            (4.5, {"bridge": False}),
            (2.5, {"bridge": False}),
            (0, {"bridge": True}),
            (0, {"ready": True}),
        ]
    )

    def status(method, path, body, timeout):
        delay, response = next(replies)
        assert delay <= timeout
        wizard_server.clock += delay
        return response

    wizard_server.overrides["/api/status"] = status
    mocker.patch.object(hw_mod, "lan_ip", return_value="192.0.2.10")

    result = runner.invoke(app, ["--auto"])

    assert result.exit_code == 0, result.output
    assert "Phone connected" in result.output


def test_setup_accepts_readiness_in_final_fraction_of_timeout(wizard_server):
    replies = iter(
        [
            (0, {"calibrated": True}),
            *[(5, {"ready": False})] * 7,
            (1.5, {"ready": False}),
            (0, {"ready": True}),
        ]
    )

    def status(method, path, body, timeout):
        delay, response = next(replies)
        assert delay <= timeout
        wizard_server.clock += delay
        return response

    wizard_server.overrides["/api/status"] = status

    result = runner.invoke(app, ["--auto"])

    assert result.exit_code == 0, result.output
    assert wizard_server.clock == 47.5


@pytest.mark.parametrize(
    "url", ["http://serverX", "http://serverX/", "http://serverX///"]
)
def test_setup_normalization_preserves_hostname_from_config(url, wizard_server, mocker):
    import importlib.util

    mocker.patch.object(hw_mod.config, "server_url", return_value=url)
    wizard_server.initial = {"ready": True}
    spec = importlib.util.spec_from_file_location("hardware_fresh", hw_mod.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)

    fresh.hardware(auto=True)

    assert wizard_server.calls[0][4] == "http://serverX"
