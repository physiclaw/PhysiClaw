"""Tests for `physiclaw.cli.doctor` — health-check command.

Mutation review accepts changes limited to prose/decoration, seven versus
eight SHA characters, one extra warmup frame, one-character clipping limits,
and equivalent falsy values for an absent status problem. Tests assert the
actual recovery commands, probe inputs, resource cleanup and diagnostic
outcomes without freezing every piece of display wording.
"""

from __future__ import annotations

import base64
import importlib
import io
import json
import logging
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import typer
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

doctor_mod = importlib.import_module("physiclaw.cli.doctor")

app = typer.Typer()
app.command()(doctor_mod.doctor)
runner = CliRunner()


# ---------- _list_cameras ----------


def test_list_cameras_returns_open_indices(mocker) -> None:
    import cv2

    captures = {}

    def make_cap(i):
        cap = MagicMock()
        cap.isOpened.return_value = i in (0, 2)
        captures[i] = cap
        return cap

    mocker.patch.object(cv2, "VideoCapture", side_effect=make_cap)
    mocker.patch(
        "physiclaw.core.hardware.camera.silenced_stderr",
        return_value=MagicMock(__enter__=lambda s: s, __exit__=lambda *a: None),
    )

    out = doctor_mod._list_cameras(max_index=5)

    assert out == [0, 2]


def test_list_cameras_breaks_after_two_misses(mocker) -> None:
    import cv2

    def make_cap(i):
        cap = MagicMock()
        cap.isOpened.return_value = i == 0
        return cap

    spy = mocker.patch.object(cv2, "VideoCapture", side_effect=make_cap)
    mocker.patch(
        "physiclaw.core.hardware.camera.silenced_stderr",
        return_value=MagicMock(__enter__=lambda s: s, __exit__=lambda *a: None),
    )

    out = doctor_mod._list_cameras(max_index=10)

    assert out == [0]
    # Stops after seeing 2 misses (indices 1 and 2).
    assert spy.call_count == 3


# ---------- _probe_server ----------


def test_probe_server_uses_live_state_when_provided(mocker) -> None:
    mocker.patch.object(doctor_mod._http, "api", return_value={"ready": True})

    host, port, bind_all, status = doctor_mod._probe_server(
        {"host": "127.0.0.1", "port": 9000},
    )

    assert host == "127.0.0.1"
    assert port == 9000
    assert bind_all is False
    assert status == {"ready": True}


def test_probe_server_renames_0_0_0_0_to_localhost(mocker) -> None:
    mocker.patch.object(doctor_mod._http, "api", return_value={"ready": False})

    host, _port, bind_all, _status = doctor_mod._probe_server(
        {"host": "0.0.0.0", "port": 8048},
    )

    assert host == "localhost"
    assert bind_all is True


def test_probe_server_falls_back_to_config(mocker) -> None:
    mocker.patch.object(doctor_mod._http, "api", return_value={"ready": True})

    host, port, bind_all, status = doctor_mod._probe_server(None)

    # Just verify the call worked — host/port come from CONFIG.
    assert isinstance(host, str)
    assert isinstance(port, int)


def test_probe_server_returns_none_on_http_error(mocker) -> None:
    # _http.api is fail-soft (None on refused/timeout) — pin the pass-through.
    mocker.patch.object(doctor_mod._http, "api", return_value=None)

    host, port, bind_all, status = doctor_mod._probe_server(
        {"host": "x", "port": 1},
    )

    assert status is None


# ---------- _probe_vision_model_deep ----------


def test_probe_vision_model_deep_ok(mocker) -> None:
    fake_input = MagicMock()
    fake_input.shape = [1, 3, 1280, 1280]
    fake_session = MagicMock()
    fake_session.get_inputs.return_value = [fake_input]
    fake_ort = MagicMock()
    fake_ort.InferenceSession.return_value = fake_session
    mocker.patch.dict("sys.modules", {"onnxruntime": fake_ort})

    out = doctor_mod._probe_vision_model_deep()

    assert "loads OK" in out


def test_probe_vision_model_deep_failure(mocker) -> None:
    fake_ort = MagicMock()
    fake_ort.InferenceSession.side_effect = RuntimeError("bad model")
    mocker.patch.dict("sys.modules", {"onnxruntime": fake_ort})

    out = doctor_mod._probe_vision_model_deep()

    assert "load failed" in out
    assert "RuntimeError" in out


# ---------- _probe_camera_frame ----------


def test_probe_camera_frame_ok(mocker) -> None:
    import cv2
    import numpy as np

    fake_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    fake_cap = MagicMock()
    fake_cap.read.return_value = (True, fake_frame)
    mocker.patch.object(cv2, "VideoCapture", return_value=fake_cap)
    mocker.patch(
        "physiclaw.core.hardware.camera.silenced_stderr",
        return_value=MagicMock(__enter__=lambda s: s, __exit__=lambda *a: None),
    )

    out = doctor_mod._probe_camera_frame(0)

    assert "frame OK" in out
    assert "640x480" in out


def test_probe_camera_frame_no_frame(mocker) -> None:
    import cv2

    fake_cap = MagicMock()
    fake_cap.read.return_value = (False, None)
    mocker.patch.object(cv2, "VideoCapture", return_value=fake_cap)
    mocker.patch(
        "physiclaw.core.hardware.camera.silenced_stderr",
        return_value=MagicMock(__enter__=lambda s: s, __exit__=lambda *a: None),
    )

    out = doctor_mod._probe_camera_frame(2)

    assert "no frame" in out


# ---------- _probe_calibration_deep ----------


def test_probe_calibration_deep_no_bundle(mocker) -> None:
    mocker.patch.object(doctor_mod.paths, "load_calibration_bundle", return_value=None)

    out = doctor_mod._probe_calibration_deep()

    assert "parse failed" in out


def test_probe_calibration_deep_unreadable(mocker) -> None:
    mocker.patch.object(
        doctor_mod.paths,
        "load_calibration_bundle",
        return_value={"x": 1},
    )
    fake_cal_module = MagicMock()
    fake_cal_module.Calibration.from_dict.side_effect = ValueError("bad")
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.core.calibration.state": fake_cal_module},
    )

    out = doctor_mod._probe_calibration_deep()

    assert "unreadable" in out


def test_probe_calibration_deep_complete(mocker) -> None:
    mocker.patch.object(
        doctor_mod.paths,
        "load_calibration_bundle",
        return_value={"a": 1},
    )
    fake_cal = MagicMock(complete=True)
    fake_cal_module = MagicMock()
    fake_cal_module.Calibration.from_dict.return_value = fake_cal
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.core.calibration.state": fake_cal_module},
    )

    out = doctor_mod._probe_calibration_deep()

    assert "bundle complete" in out


def test_probe_calibration_deep_partial_with_missing(mocker) -> None:
    mocker.patch.object(
        doctor_mod.paths,
        "load_calibration_bundle",
        return_value={"a": None, "b": 1, "c": None},
    )
    fake_cal = MagicMock(complete=False)
    fake_cal_module = MagicMock()
    fake_cal_module.Calibration.from_dict.return_value = fake_cal
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.core.calibration.state": fake_cal_module},
    )

    out = doctor_mod._probe_calibration_deep()

    assert "partial" in out
    assert "'a'" in out and "'c'" in out


def test_probe_calibration_deep_partial_no_missing(mocker) -> None:
    mocker.patch.object(
        doctor_mod.paths,
        "load_calibration_bundle",
        return_value={"a": 1, "b": 2},
    )
    fake_cal = MagicMock(complete=False)
    fake_cal_module = MagicMock()
    fake_cal_module.Calibration.from_dict.return_value = fake_cal
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.core.calibration.state": fake_cal_module},
    )

    out = doctor_mod._probe_calibration_deep()

    assert "keys:" in out


# ---------- _probe_bridge_deep ----------


def test_probe_bridge_deep_connected(mocker) -> None:
    mocker.patch.object(
        doctor_mod._http, "fetch_json", return_value={"connected": True}
    )

    out = doctor_mod._probe_bridge_deep("localhost", 8048)

    assert "phone connected" in out


def test_probe_bridge_deep_not_paired(mocker) -> None:
    mocker.patch.object(
        doctor_mod._http, "fetch_json", return_value={"connected": False}
    )

    out = doctor_mod._probe_bridge_deep("localhost", 8048)

    assert "not paired" in out


def test_probe_bridge_deep_http_error(mocker) -> None:
    # The warn line carries the failure detail (exception type + message).
    mocker.patch.object(
        doctor_mod._http,
        "fetch_json",
        side_effect=urllib.error.URLError("x"),
    )

    out = doctor_mod._probe_bridge_deep("localhost", 8048)

    assert "URLError" in out


# ---------- _skills_lines ----------


def test_skills_lines_empty(mocker, tmp_path: Path) -> None:
    mocker.patch.object(doctor_mod.paths, "skills_dir", return_value=tmp_path)

    fake_skills = MagicMock()
    fake_skills.installed_skill_dirs.return_value = []
    fake_skills.read_provenance.return_value = None
    fake_skills.PROVENANCE_FILE = ".installed-from"
    mocker.patch.dict("sys.modules", {"physiclaw.cli.skills": fake_skills})

    lines = doctor_mod._skills_lines()

    assert len(lines) == 1
    assert "none installed" in lines[0]


def test_skills_lines_with_provenance(mocker, tmp_path: Path) -> None:
    skill_dir = tmp_path / "alpha"
    skill_dir.mkdir()

    fake_skills = MagicMock()
    fake_skills.installed_skill_dirs.return_value = [skill_dir]
    fake_skills.read_provenance.return_value = {
        "source": "owner/repo",
        "ref": "main",
        "sha": "abc1234",
    }
    fake_skills.PROVENANCE_FILE = ".installed-from"
    mocker.patch.dict("sys.modules", {"physiclaw.cli.skills": fake_skills})

    lines = doctor_mod._skills_lines()

    assert any("alpha" in line and "owner/repo" in line for line in lines)


def test_skills_lines_uses_sha_when_no_ref(mocker, tmp_path: Path) -> None:
    skill_dir = tmp_path / "alpha"
    skill_dir.mkdir()

    fake_skills = MagicMock()
    fake_skills.installed_skill_dirs.return_value = [skill_dir]
    fake_skills.read_provenance.return_value = {
        "source": "owner/repo",
        "ref": "",
        "sha": "abcdef0123",
    }
    fake_skills.PROVENANCE_FILE = ".installed-from"
    mocker.patch.dict("sys.modules", {"physiclaw.cli.skills": fake_skills})

    lines = doctor_mod._skills_lines()

    assert any("abcdef0" in line for line in lines)


def test_skills_lines_local_skill(mocker, tmp_path: Path) -> None:
    skill_dir = tmp_path / "myown"
    skill_dir.mkdir()

    fake_skills = MagicMock()
    fake_skills.installed_skill_dirs.return_value = [skill_dir]
    fake_skills.read_provenance.return_value = None
    fake_skills.PROVENANCE_FILE = ".installed-from"
    mocker.patch.dict("sys.modules", {"physiclaw.cli.skills": fake_skills})

    lines = doctor_mod._skills_lines()

    assert any("myown" in line and "(local)" in line for line in lines)


def test_skills_lines_unparseable_provenance(mocker, tmp_path: Path) -> None:
    skill_dir = tmp_path / "broken"
    skill_dir.mkdir()
    (skill_dir / ".installed-from").write_text("not json")

    fake_skills = MagicMock()
    fake_skills.installed_skill_dirs.return_value = [skill_dir]
    fake_skills.read_provenance.return_value = None
    fake_skills.PROVENANCE_FILE = ".installed-from"
    mocker.patch.dict("sys.modules", {"physiclaw.cli.skills": fake_skills})

    lines = doctor_mod._skills_lines()

    assert any("provenance unreadable" in line for line in lines)


# ---------- doctor command (smoke) ----------


def _patch_doctor_environment(
    mocker,
    *,
    server_status: dict | None = None,
    model_exists: bool = False,
    bundle_exists: bool = False,
    live_ref: str | None = None,
    key_unset: bool = True,
) -> None:
    """Stub everything `doctor()` touches so the command runs to completion."""
    mocker.patch.object(doctor_mod.paths, "ensure_dirs")
    mocker.patch.object(doctor_mod.paths, "HOME", Path("/fake/home"))
    cp = MagicMock(spec=Path)
    cp.exists.return_value = True
    cp.__str__ = lambda s: "/fake/config.toml"
    mocker.patch("physiclaw.common.config.config_path", return_value=cp)
    # doctor re-parses the file for freshness; the fake path has no body.
    mocker.patch("physiclaw.common.config.load")

    onnx = MagicMock(spec=Path)
    onnx.exists.return_value = model_exists
    onnx.stat.return_value = MagicMock(st_size=1024 * 1024)
    onnx.__str__ = lambda s: "/fake/model.onnx"
    mocker.patch.object(doctor_mod.paths, "omniparser_onnx", return_value=onnx)

    bundle = MagicMock(spec=Path)
    bundle.exists.return_value = bundle_exists
    bundle.__str__ = lambda s: "/fake/bundle.json"
    mocker.patch.object(doctor_mod.paths, "calibration_bundle", return_value=bundle)

    fake_state = MagicMock()
    if live_ref:
        fake_state.read_live.return_value = {
            "host": "127.0.0.1",
            "port": 8048,
            "model_ref": live_ref,
            "model_source": "config",
        }
    else:
        fake_state.read_live.return_value = None
    mocker.patch.object(doctor_mod, "runtime_state", fake_state)

    mocker.patch.object(
        doctor_mod,
        "_probe_server",
        return_value=("localhost", 8048, False, server_status),
    )
    mocker.patch.object(doctor_mod, "_list_cameras", return_value=[0])
    mocker.patch.object(
        doctor_mod, "_skills_lines", return_value=["  (none installed)"]
    )

    fake_launcher = MagicMock()
    fake_launcher.engine_label.return_value = "engine=openai"
    fake_launcher.resolve.return_value = ("openai/gpt-5", "config")
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.agent.runtime.launcher": fake_launcher},
    )

    fake_provider = MagicMock()
    fake_provider.CLAUDE_CODE_ID = "claude-code"
    fake_provider.provider_key_status.return_value = (
        (None, "config") if key_unset else ("sk-***x", "env")
    )
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.provider": fake_provider},
    )

    fake_grbl = MagicMock()
    fake_grbl.detect_grbl.return_value = "/dev/cu.usbserial-X"
    fake_grbl.candidate_ports.return_value = ["/dev/cu.usbserial-X"]
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.core.hardware.grbl": fake_grbl},
    )


def test_doctor_server_running_and_ready(mocker) -> None:
    _patch_doctor_environment(
        mocker,
        server_status={
            "arm": True,
            "camera": True,
            "calibrated": True,
            "ready": True,
        },
        model_exists=True,
        bundle_exists=True,
        key_unset=False,
    )

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "PhysiClaw doctor" in result.output
    assert "Checks complete" in result.output


def test_doctor_server_not_running_probes_locally(mocker) -> None:
    _patch_doctor_environment(
        mocker,
        server_status=None,  # server offline
        model_exists=False,
        bundle_exists=False,
    )

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "server: not running" in result.output
    assert "GRBL arm" in result.output
    assert "physiclaw setup local-vision-model" in result.output
    assert "starts the server and opens the hardware-setup wizard" in result.output


def test_doctor_no_cameras_detected_offline(mocker) -> None:
    _patch_doctor_environment(mocker, server_status=None)
    mocker.patch.object(doctor_mod, "_list_cameras", return_value=[])

    result = runner.invoke(app, [])

    assert "cameras: none detected" in result.output


def test_doctor_no_grbl_detected(mocker) -> None:
    _patch_doctor_environment(mocker, server_status=None)
    fake_grbl = MagicMock()
    fake_grbl.detect_grbl.return_value = None
    fake_grbl.candidate_ports.return_value = []
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.core.hardware.grbl": fake_grbl},
    )

    result = runner.invoke(app, [])

    assert "no serial ports detected" in result.output


def test_doctor_no_grbl_with_candidate_ports(mocker) -> None:
    _patch_doctor_environment(mocker, server_status=None)
    fake_grbl = MagicMock()
    fake_grbl.detect_grbl.return_value = None
    fake_grbl.candidate_ports.return_value = ["/dev/cu.usbserial-A"]
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.core.hardware.grbl": fake_grbl},
    )

    result = runner.invoke(app, [])

    assert "no GRBL detected" in result.output


def test_doctor_warns_about_bind_all(mocker) -> None:
    _patch_doctor_environment(
        mocker,
        server_status={
            "arm": True,
            "camera": True,
            "calibrated": True,
            "ready": True,
        },
    )
    mocker.patch.object(
        doctor_mod,
        "_probe_server",
        return_value=(
            "localhost",
            8048,
            True,
            {
                "arm": True,
                "camera": True,
                "calibrated": True,
                "ready": True,
            },
        ),
    )

    result = runner.invoke(app, [])

    assert "0.0.0.0" in result.output


def test_doctor_engine_invalid_when_resolve_fails(mocker) -> None:
    _patch_doctor_environment(
        mocker,
        server_status=None,
    )
    fake_launcher = MagicMock()
    fake_launcher.resolve.side_effect = RuntimeError("no model set")
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.agent.runtime.launcher": fake_launcher},
    )

    result = runner.invoke(app, [])

    assert "engine: invalid" in result.output


def test_doctor_uses_live_engine_ref(mocker) -> None:
    _patch_doctor_environment(
        mocker,
        server_status={"arm": True, "camera": True, "calibrated": True, "ready": True},
        live_ref="openai/gpt-5",
    )

    result = runner.invoke(app, [])

    assert "live server" in result.output


def test_doctor_no_config_warns(mocker) -> None:
    _patch_doctor_environment(mocker, server_status=None)
    cp = MagicMock(spec=Path)
    cp.exists.return_value = False
    mocker.patch("physiclaw.common.config.config_path", return_value=cp)

    result = runner.invoke(app, [])

    assert "no config yet" in result.output


def test_doctor_deep_runs_probes(mocker) -> None:
    _patch_doctor_environment(
        mocker,
        server_status={"arm": True, "camera": True, "calibrated": True, "ready": True},
        model_exists=True,
        bundle_exists=True,
        key_unset=False,
    )
    vision_spy = mocker.patch.object(
        doctor_mod,
        "_probe_vision_model_deep",
        return_value="✓ vision OK",
    )
    bridge_spy = mocker.patch.object(
        doctor_mod,
        "_probe_bridge_deep",
        return_value="✓ bridge OK",
    )
    calib_spy = mocker.patch.object(
        doctor_mod,
        "_probe_calibration_deep",
        return_value="✓ calib OK",
    )
    provider_spy = mocker.patch.object(
        doctor_mod,
        "_probe_provider_deep",
        return_value="✓ provider OK",
    )

    runner.invoke(app, ["--deep"])

    vision_spy.assert_called_once()
    bridge_spy.assert_called_once()
    calib_spy.assert_called_once()
    provider_spy.assert_called_once()


# ---------- _probe_provider_deep ----------


def _make_async(value):
    async def _coro(*a, **kw):
        return value

    return _coro


def test_probe_provider_deep_setup_error_returns_warn(mocker) -> None:
    fake_provider = MagicMock()
    fake_provider.make_provider.side_effect = ValueError("missing api key")
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.provider": fake_provider},
    )

    out = doctor_mod._probe_provider_deep("openai", "gpt-5")

    assert "setup" in out
    assert "missing api key" in out


def test_probe_provider_deep_chat_exception_returns_warn(mocker) -> None:
    """Non-empty reply check happens before this; the chat itself raises."""

    fake_prov = MagicMock()
    fake_prov.chat = mocker.MagicMock(side_effect=RuntimeError("network"))
    fake_prov.aclose = _make_async(None)

    fake_module = MagicMock()
    fake_module.make_provider.return_value = fake_prov
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.provider": fake_module},
    )

    out = doctor_mod._probe_provider_deep("openai", "gpt-5")

    assert "RuntimeError" in out
    assert "network" in out


def test_probe_provider_deep_empty_reply_returns_warn(mocker) -> None:
    from physiclaw.contract.dto import (
        AssistantMessage,
        FinishReason,
    )

    asst = AssistantMessage(
        content="",
        tool_calls=[],
        finish_reason=FinishReason.STOP,
    )
    fake_prov = MagicMock()
    fake_prov.chat = _make_async(asst)
    fake_prov.aclose = _make_async(None)

    fake_module = MagicMock()
    fake_module.make_provider.return_value = fake_prov
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.provider": fake_module},
    )

    out = doctor_mod._probe_provider_deep("openai", "gpt-5")

    assert "empty reply" in out


def test_probe_provider_deep_vision_check_fails(mocker) -> None:
    from physiclaw.contract.dto import (
        AssistantMessage,
        FinishReason,
        Usage,
    )

    asst = AssistantMessage(
        content="something else",  # no "physiclaw" in reply
        tool_calls=[],
        finish_reason=FinishReason.STOP,
        usage=Usage(prompt_tokens=100, completion_tokens=10),
    )
    fake_prov = MagicMock()
    fake_prov.chat = _make_async(asst)
    fake_prov.aclose = _make_async(None)

    fake_module = MagicMock()
    fake_module.make_provider.return_value = fake_prov
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.provider": fake_module},
    )

    out = doctor_mod._probe_provider_deep("openai", "gpt-5")

    assert "vision check failed" in out
    assert "PhysiClaw" in out


def test_probe_provider_deep_success_with_usage(mocker) -> None:
    from physiclaw.contract.dto import (
        AssistantMessage,
        FinishReason,
        Usage,
    )

    asst = AssistantMessage(
        content="PhysiClaw",
        tool_calls=[],
        finish_reason=FinishReason.STOP,
        usage=Usage(prompt_tokens=200, completion_tokens=2),
    )
    fake_prov = MagicMock()
    fake_prov.chat = _make_async(asst)
    fake_prov.aclose = _make_async(None)

    fake_module = MagicMock()
    fake_module.make_provider.return_value = fake_prov
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.provider": fake_module},
    )

    out = doctor_mod._probe_provider_deep("openai", "gpt-5")

    # Success rendered as ok.
    assert "PhysiClaw" in out
    assert "200p+2c" in out


def test_probe_provider_deep_success_no_usage(mocker) -> None:
    from physiclaw.contract.dto import (
        AssistantMessage,
        FinishReason,
        Usage,
    )

    asst = AssistantMessage(
        content="PhysiClaw",
        tool_calls=[],
        finish_reason=FinishReason.STOP,
        usage=Usage(prompt_tokens=0, completion_tokens=0),
    )
    fake_prov = MagicMock()
    fake_prov.chat = _make_async(asst)
    fake_prov.aclose = _make_async(None)

    fake_module = MagicMock()
    fake_module.make_provider.return_value = fake_prov
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.provider": fake_module},
    )

    out = doctor_mod._probe_provider_deep("openai", "gpt-5")

    assert "no usage" in out


def test_doctor_invalid_active_model_ref(mocker) -> None:
    _patch_doctor_environment(
        mocker,
        server_status=None,
        live_ref="garbage-no-slash",
    )
    fake_cfg = MagicMock()
    fake_cfg.parse_model_ref.side_effect = ValueError("bad ref")
    mocker.patch.dict("sys.modules", {"physiclaw.common.config": fake_cfg})

    result = runner.invoke(app, [])

    # Doesn't crash even with unparseable ref.
    assert result.exit_code == 0


def test_doctor_warns_when_config_invalid(mocker) -> None:
    """doctor must re-parse config.toml — the file may have been edited
    since any import-time load — and surface a parse error, not crash."""
    from physiclaw.common.config import ConfigError

    _patch_doctor_environment(mocker, server_status=None)
    mocker.patch(
        "physiclaw.common.config.load", side_effect=ConfigError("unknown key: portt")
    )

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "config.toml invalid" in result.output
    assert "portt" in result.output


@pytest.fixture
def doctor_io(mocker, monkeypatch):
    """Real diagnostic flow and files; fake only network/device boundaries."""
    monkeypatch.setenv("PHYSICLAW_DISABLE_UPDATE_CHECK", "1")
    mocker.patch(
        "physiclaw.agent.runtime.launcher.resolve",
        return_value=("openai/test-model", "test"),
    )
    key_status = mocker.patch(
        "physiclaw.provider.provider_key_status", return_value=(None, "test")
    )
    mocker.patch("physiclaw.common.platform.hardware_permission_hints", return_value=[])
    mocker.patch("physiclaw.core.hardware.grbl.candidate_ports", return_value=[])
    return SimpleNamespace(
        key_status=key_status,
        live=mocker.patch.object(
            doctor_mod.runtime_state, "read_live", return_value=None
        ),
        api=mocker.patch.object(doctor_mod._http, "api", return_value={"ready": True}),
        bridge=mocker.patch.object(
            doctor_mod._http, "fetch_json", return_value={"connected": False}
        ),
        detect=mocker.patch(
            "physiclaw.core.hardware.grbl.detect_grbl", return_value=None
        ),
        camera=mocker.patch("cv2.VideoCapture", side_effect=lambda *a: _Capture()),
    )


class _Capture:
    def __init__(self, *, opened=False, failure=None, frame=None):
        self.opened = opened
        self.failure = failure
        self.released = 0
        self.frame = frame

    def _check(self, phase):
        import cv2

        if self.failure == phase:
            raise cv2.error(f"{phase} failed")
        if self.failure == "interrupt" and phase == "isOpened":
            raise KeyboardInterrupt

    def isOpened(self):
        self._check("isOpened")
        return self.opened

    def set(self, prop, value):
        self._check("configure")
        return True

    def get(self, prop):
        return 0

    def read(self):
        self._check("read")
        return self.frame is not None, self.frame

    def release(self):
        self.released += 1
        self._check("release")


def test_regression_doctor_corrupt_onnx_reports_failure_and_continues(doctor_io):
    model = doctor_mod.paths.omniparser_onnx()
    model.parent.mkdir(parents=True)
    model.write_bytes(b"not an ONNX model")

    result = runner.invoke(app, ["--deep"])

    assert result.exit_code == 0, result.exception
    assert "vision model: load failed" in result.output
    assert "Next steps" in result.output
    assert "All set" not in result.output


@pytest.mark.parametrize(
    "host, url_host",
    [("::1", "[::1]"), ("2001:db8::1", "[2001:db8::1]"), ("::", "localhost")],
)
def test_regression_doctor_bridge_uses_valid_ipv6_url(doctor_io, host, url_host):
    doctor_io.live.return_value = {"host": host, "port": 9048}

    result = runner.invoke(app, ["--deep"])

    assert result.exit_code == 0, result.exception
    doctor_io.bridge.assert_called_once_with(
        f"http://{url_host}:9049/api/bridge/state", timeout=1.0
    )


@pytest.mark.parametrize(
    "payload", [[], None, "bad", 42, {"connected": "false"}, {"connected": 1}]
)
def test_regression_doctor_bad_bridge_state_reports_failure_and_continues(
    doctor_io, payload
):
    doctor_io.bridge.return_value = payload

    result = runner.invoke(app, ["--deep"])

    assert result.exit_code == 0, result.exception
    assert "! bridge:" in result.output
    assert "phone connected" not in result.output
    assert "Next steps" in result.output


@pytest.mark.parametrize("payload", [[], False, "bad", 42])
def test_regression_doctor_invalid_status_does_not_open_hardware(doctor_io, payload):
    doctor_io.api.return_value = payload

    result = runner.invoke(app, ["--deep"])

    assert result.exit_code == 0, result.exception
    assert "invalid status" in result.output
    doctor_io.detect.assert_not_called()
    doctor_io.camera.assert_not_called()
    assert "Next steps" in result.output


def test_regression_doctor_unresponsive_live_server_does_not_open_hardware(doctor_io):
    doctor_io.live.return_value = {"host": "127.0.0.1", "port": 9048}
    doctor_io.api.return_value = None

    result = runner.invoke(app, ["--deep"])

    assert result.exit_code == 0, result.exception
    assert "status unavailable" in result.output
    doctor_io.detect.assert_not_called()
    doctor_io.camera.assert_not_called()
    assert "Next steps" in result.output


def test_regression_doctor_camera_enumeration_failure_releases_capture(doctor_io):
    doctor_io.api.return_value = None
    capture = _Capture(failure="isOpened")
    doctor_io.camera.side_effect = lambda *a: capture

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.exception
    assert capture.released == 1
    assert "cameras:" in result.output and "isOpened failed" in result.output
    assert "Next steps" in result.output


@pytest.mark.parametrize("failure", ["configure", "read", "release"])
def test_regression_doctor_camera_frame_failure_is_contained(doctor_io, failure):
    doctor_io.api.return_value = None
    capture = _Capture(opened=True, failure=failure)
    doctor_io.camera.side_effect = lambda index, *backend: (
        capture if backend else _Capture(opened=index == 0)
    )

    result = runner.invoke(app, ["--deep"])

    assert result.exit_code == 0, result.exception
    assert capture.released == 1
    assert f"{failure} failed" in result.output
    assert "Next steps" in result.output


def test_regression_doctor_serial_failure_does_not_abort_report(doctor_io):
    doctor_io.api.return_value = None
    doctor_io.detect.side_effect = OSError("serial disconnected")

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.exception
    assert "serial disconnected" in result.output
    assert "cameras:" in result.output
    assert "Next steps" in result.output


@pytest.mark.parametrize(
    "payload", [[], 3, {"sha": 123}, {"ref": ["main"]}, {"source": 42}]
)
def test_regression_doctor_bad_skill_provenance_preserves_other_results(
    doctor_io, payload
):
    broken = doctor_mod.paths.skills_dir() / "broken"
    broken.mkdir(parents=True)
    (broken / ".installed-from").write_text(json.dumps(payload), encoding="utf-8")
    (broken.parent / "healthy").mkdir()

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.exception
    assert "broken: provenance unreadable" in result.output
    assert "healthy  (local)" in result.output
    assert "Next steps" in result.output


def test_doctor_enumeration_interrupt_propagates_after_release(doctor_io):
    doctor_io.api.return_value = None
    capture = _Capture(failure="interrupt")
    doctor_io.camera.side_effect = lambda *a: capture

    with pytest.raises(KeyboardInterrupt):
        doctor_mod.doctor()

    assert capture.released == 1


@given(host=st.ip_addresses(v=6), port=st.integers(min_value=1, max_value=65534))
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_doctor_ipv6_probe_urls_keep_host_and_successor_port(doctor_io, host, port):
    doctor_io.live.return_value = {"host": str(host), "port": port}
    doctor_io.bridge.reset_mock()
    doctor_io.api.reset_mock()
    authority = {"::": "localhost"}.get(str(host), f"[{host}]")

    result = runner.invoke(app, ["--deep"])

    assert result.exit_code == 0, result.exception
    doctor_io.api.assert_called_once_with(
        f"http://{authority}:{port}", "GET", "/api/status", timeout=1.0
    )
    doctor_io.bridge.assert_called_once_with(
        f"http://{authority}:{port + 1}/api/bridge/state", timeout=1.0
    )


@pytest.mark.parametrize("value, verdict", [(True, "yes"), (1, "no"), ("false", "no")])
def test_doctor_status_flags_require_json_true(doctor_io, value, verdict):
    doctor_io.api.return_value = {
        "arm": value,
        "camera": value,
        "calibrated": value,
        "ready": value,
    }

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.exception
    assert f"arm: {verdict}" in result.output
    assert f"camera: {verdict}" in result.output
    assert f"calibrated: {verdict}" in result.output
    assert f"ready: {verdict}" in result.output


def _use_camera_frame(doctor_io, frame):
    doctor_io.api.return_value = None
    capture = _Capture(opened=True, frame=frame)
    doctor_io.camera.side_effect = lambda index, *backend: (
        capture if backend else _Capture(opened=index == 0)
    )
    return capture


@pytest.mark.parametrize(
    "luma, verdict", [(0, "DARK"), (127, "BLURRY"), (255, "BLOWN")]
)
def test_doctor_frame_quality_reports_actionable_cause(doctor_io, luma, verdict):
    import numpy as np

    capture = _use_camera_frame(doctor_io, np.full((48, 64, 3), luma, dtype=np.uint8))

    result = runner.invoke(app, ["--deep"])

    assert result.exit_code == 0, result.exception
    assert f"quality {verdict}" in result.output
    assert "! camera 0:" in result.output
    assert "whole frame — no calibration crop" in result.output
    assert capture.released == 1


@pytest.mark.parametrize(
    "cam_size, rotation, scope",
    [
        ((64, 48), -1, "phone-screen crop)"),
        ((128, 96), -1, "phone-screen crop (calibrated 128x96, rescaled to 64x48)"),
        ((48, 64), -1, "aspect 64x48 != calibrated 48x64; recalibrate"),
        ((48, 64), 0, "phone-screen crop)"),
    ],
)
def test_doctor_frame_quality_respects_calibrated_geometry(
    doctor_io, mocker, cam_size, rotation, scope
):
    import numpy as np

    from physiclaw.core.calibration.state import Calibration

    cal = Calibration(
        cam_size=cam_size,
        cam_rotation=rotation,
        pct_to_cam=np.array([[1.0, 0, 0], [0, 1.0, 0]]),
        pct_to_grbl=np.array([[1.0, 0, 0], [0, 1.0, 0]]),
    )
    mocker.patch.object(Calibration, "load", return_value=cal)
    frame = np.random.default_rng(42).integers(30, 220, (48, 64, 3), dtype=np.uint8)
    _use_camera_frame(doctor_io, frame)

    result = runner.invoke(app, ["--deep"])

    assert result.exit_code == 0, result.exception
    assert "quality ok" in result.output
    assert "✓ camera 0:" in result.output
    assert scope in result.output


@pytest.mark.parametrize("hint", ["", "install OpenCV system libraries"])
def test_doctor_missing_opencv_still_finishes_report(doctor_io, mocker, hint):
    doctor_io.api.return_value = None
    mocker.patch.dict("sys.modules", {"cv2": None})
    mocker.patch("physiclaw.common.platform.opencv_import_hint", return_value=hint)

    result = runner.invoke(app, ["--deep"])

    assert result.exit_code == 0, result.exception
    assert "OpenCV (cv2) import failed" in result.output
    assert hint in result.output
    assert "Next steps" in result.output
    doctor_io.camera.assert_not_called()


@pytest.mark.parametrize("ref", ["missing-slash", 42, ["openai/test-model"]])
def test_regression_doctor_invalid_live_model_ref_is_reported(doctor_io, ref):
    doctor_io.live.return_value = {"host": "127.0.0.1", "port": 9048, "model_ref": ref}

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.exception
    assert "engine: invalid" in result.output
    doctor_io.key_status.assert_not_called()
    assert "Next steps" in result.output


def test_regression_doctor_bridge_http_error_body_is_closed(doctor_io):
    body = io.BytesIO(b"unavailable")
    doctor_io.bridge.side_effect = urllib.error.HTTPError(
        "http://localhost", 503, "unavailable", None, body
    )

    result = runner.invoke(app, ["--deep"])

    assert result.exit_code == 0, result.exception
    assert "bridge: HTTPError" in result.output
    assert body.closed


@pytest.mark.parametrize(
    "failure, summary",
    [
        (None, "reply='PhysiClaw ok' (2500ms,"),
        (RuntimeError("disconnected"), "RuntimeError"),
    ],
)
def test_doctor_provider_probe_sends_valid_image_and_closes_client(
    doctor_io, mocker, failure, summary
):
    from PIL import Image

    from physiclaw.contract.dto import AssistantMessage, FinishReason

    mocker.patch("physiclaw.provider.provider_key_status", return_value=("***", "test"))
    mocker.patch("time.perf_counter", side_effect=[10.0, 12.5])
    provider = SimpleNamespace(
        chat=mocker.AsyncMock(
            return_value=AssistantMessage(
                content="PhysiClaw\nok", tool_calls=[], finish_reason=FinishReason.STOP
            ),
            side_effect=failure,
        ),
        aclose=mocker.AsyncMock(),
    )
    mocker.patch("physiclaw.provider.make_provider", return_value=provider)

    result = runner.invoke(app, ["--deep"])

    assert result.exit_code == 0, result.exception
    assert summary in result.output
    provider.aclose.assert_awaited_once()
    image = provider.chat.call_args.args[0][1].content[1]
    assert image.media_type == "image/png"
    with Image.open(io.BytesIO(base64.b64decode(image.data_b64, validate=True))) as png:
        assert png.format == "PNG"
        png.verify()


def test_doctor_enumerates_only_bounded_camera_indices(doctor_io):
    doctor_io.api.return_value = None
    capture = _Capture(opened=True)
    doctor_io.camera.side_effect = lambda *a: capture

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.exception
    assert "cameras: 5 detected" in result.output
    assert [call.args[0] for call in doctor_io.camera.call_args_list] == [0, 1, 2, 3, 4]
    assert capture.released == 5


def test_doctor_serial_interrupt_restores_logging(doctor_io, mocker):
    doctor_io.api.return_value = None
    doctor_io.detect.side_effect = KeyboardInterrupt
    logger = logging.getLogger("physiclaw.core.hardware.grbl")
    mocker.patch.object(logger, "level", logging.INFO)

    with pytest.raises(KeyboardInterrupt):
        doctor_mod.doctor()

    assert logger.level == logging.INFO


def test_doctor_camera_scan_stops_after_initial_consecutive_misses(doctor_io):
    doctor_io.api.return_value = None
    capture = _Capture()
    doctor_io.camera.side_effect = lambda *a: capture

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.exception
    assert doctor_io.camera.call_count == capture.released == 2


def test_doctor_suppresses_grbl_probe_logging_only_during_detection(doctor_io):
    doctor_io.api.return_value = None
    logger = logging.getLogger("physiclaw.core.hardware.grbl")
    before = logger.level
    observed = []
    doctor_io.detect.side_effect = lambda: observed.append(logger.level)

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.exception
    assert observed == [logging.CRITICAL]
    assert logger.level == before


def test_doctor_model_probe_uses_cpu_and_reports_input_shape(doctor_io, mocker):
    model = doctor_mod.paths.omniparser_onnx()
    model.parent.mkdir(parents=True)
    model.touch()
    session = SimpleNamespace(
        get_inputs=lambda: [SimpleNamespace(shape=[1, 3, 320, 320])]
    )
    factory = mocker.patch("onnxruntime.InferenceSession", return_value=session)

    result = runner.invoke(app, ["--deep"])

    assert result.exit_code == 0, result.exception
    factory.assert_called_once_with(str(model), providers=["CPUExecutionProvider"])
    assert "input [1, 3, 320, 320]" in result.output


def test_doctor_ignores_frames_from_failed_camera_reads(doctor_io, mocker):
    import numpy as np

    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    capture = _use_camera_frame(doctor_io, frame)
    mocker.patch.object(capture, "read", return_value=(False, frame))

    result = runner.invoke(app, ["--deep"])

    assert result.exit_code == 0, result.exception
    assert "opens but no frame" in result.output
    assert "frame OK" not in result.output


def test_doctor_partial_calibration_names_missing_values(doctor_io):
    bundle = doctor_mod.paths.calibration_bundle()
    bundle.parent.mkdir(parents=True)
    bundle.write_text('{"pct_to_cam":null}', encoding="utf-8")

    result = runner.invoke(app, ["--deep"])

    assert result.exit_code == 0, result.exception
    assert "partial (missing: ['pct_to_cam'])" in result.output


def test_doctor_bridge_requires_explicit_connection_flag(doctor_io):
    doctor_io.bridge.return_value = {}

    result = runner.invoke(app, ["--deep"])

    assert result.exit_code == 0, result.exception
    assert "phone not paired yet" in result.output
    assert "phone connected" not in result.output


@pytest.mark.parametrize(
    "provenance, suffix",
    [
        ({"source": "owner/repo", "ref": "stable", "sha": "abcdef012345"}, "@ stable"),
        ({"source": "owner/repo", "sha": "abcdef012345"}, "@ abcdef0"),
        ({"source": "owner/repo"}, "@ ?"),
        ({"source": "owner/repo", "ref": None, "sha": None}, "@ ?"),
    ],
)
def test_doctor_skill_provenance_describes_installed_revision(
    doctor_io, provenance, suffix
):
    skill = doctor_mod.paths.skills_dir() / "installed"
    skill.mkdir(parents=True)
    (skill / ".installed-from").write_text(json.dumps(provenance), encoding="utf-8")

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.exception
    assert f"installed  ← owner/repo {suffix}" in result.output


def test_doctor_reports_python_and_uv_context(doctor_io, mocker):
    import sys

    mocker.patch("shutil.which", return_value="/test/bin/uv")

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.exception
    assert f"python:     {sys.version.split()[0]} ({sys.executable})" in result.output
    assert "uv:         /test/bin/uv" in result.output
    assert "provider=openai, model=test-model (from test)" in result.output


def test_doctor_model_size_uses_megabytes(doctor_io):
    model = doctor_mod.paths.omniparser_onnx()
    model.parent.mkdir(parents=True)
    with model.open("wb") as output:
        output.truncate(100 * 1024 * 1024)

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.exception
    assert "100.0 MB" in result.output


def test_doctor_live_engine_metadata_takes_precedence(doctor_io):
    doctor_io.live.return_value = {
        "host": "127.0.0.1",
        "port": 9048,
        "model_ref": "deepseek/live-model",
        "model_source": "environment",
    }

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.exception
    assert (
        "provider=deepseek, model=live-model (live server, environment)"
        in result.output
    )
    doctor_io.key_status.assert_called_once_with("deepseek")


def test_doctor_shallow_check_does_not_contact_provider(doctor_io, mocker):
    doctor_io.key_status.return_value = ("***", "test")
    make = mocker.patch("physiclaw.provider.make_provider")

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.exception
    make.assert_not_called()


@pytest.mark.parametrize(
    "status, recommendation",
    [
        (None, "  2. physiclaw   (starts the server"),
        ({"ready": False}, "  2. finish setup in the browser"),
    ],
)
def test_doctor_next_step_distinguishes_offline_from_unready_server(
    doctor_io, status, recommendation
):
    doctor_io.api.return_value = status

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.exception
    assert "\n  1. physiclaw setup local-vision-model\n" in result.output
    assert recommendation in result.output.split("Next steps", 1)[1]


def test_doctor_surfaces_platform_hardware_permission_hints(doctor_io, mocker):
    doctor_io.api.return_value = None
    mocker.patch(
        "physiclaw.common.platform.hardware_permission_hints",
        return_value=["camera permission requires attention"],
    )

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.exception
    assert "! camera permission requires attention" in result.output
