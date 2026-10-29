"""Tests for `physiclaw.core.bridge.handler` — Starlette route handlers.

Each handler takes a Starlette `request` plus state objects. Tests
construct minimal fake requests (with streamed bodies) and use real
`BridgeState` / `CalibrationState` / `PageState`
instances.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from physiclaw.core.bridge import handler
from physiclaw.core.bridge.calib import CalibrationState
from physiclaw.core.bridge.handler import (
    handle_calib_touch,
    handle_clipboard_copied,
    handle_clipboard_fetch,
    handle_mode_switch,
    handle_phone_state,
    handle_screen_dimension,
    handle_screenshot_upload,
    serve_bridge_page,
    serve_qr_page,
)
from physiclaw.core.bridge.page import PageState
from physiclaw.core.bridge.state import BridgeState
from physiclaw.core.calibration.transforms import ViewportShift
from tests.core.conftest import stream_request


@pytest.fixture(autouse=True)
def _no_real_save_screenshot(mocker) -> None:
    mocker.patch("physiclaw.core.bridge.state.save_screenshot")


def _fake_request(
    body_bytes: bytes | None = None,
    json_obj: Any = None,
    url_port: int | None = 8048,
    client_host: str = "127.0.0.1",
    headers: dict[str, str] | None = None,
):
    data = body_bytes if body_bytes is not None else json.dumps(json_obj).encode()
    return stream_request(
        data,
        url=SimpleNamespace(port=url_port),
        client=SimpleNamespace(host=client_host),
        headers=headers or {},
    )


# A minimal body that passes the upload magic-byte check.
PNG = b"\x89PNG\r\n\x1a\n" + b"fake png payload"


@pytest.mark.parametrize("declared", [None, "1", "1000000"])
async def test_json_stream_is_bounded_before_state_changes(monkeypatch, declared):
    from starlette.exceptions import HTTPException

    monkeypatch.setattr(handler, "MAX_JSON_BYTES", 32)
    request = _fake_request(
        headers={} if declared is None else {"content-length": declared}
    )

    async def stream():
        yield b" " * 32
        yield b" "
        pytest.fail("oversized JSON stream must stop being read")

    request.stream = stream
    cal = CalibrationState()
    with pytest.raises(HTTPException) as error:
        await handle_screen_dimension(request, cal)

    assert error.value.status_code == 413
    assert cal.screen_dimension is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"", {}),
        (b"null", None),
        (b"[]", None),
        (b"false", None),
        (b"not json", None),
        (b"\xff", None),
        (b" ", None),
    ],
)
async def test_optional_json_defaults_only_for_absent_body(payload, expected):
    assert (
        await handler.json_or_none(_fake_request(body_bytes=payload), allow_empty=True)
        == expected
    )


async def test_json_stream_accepts_exact_limit_and_split_utf8(monkeypatch):
    data = '{"text":"你好"}'.encode()
    monkeypatch.setattr(handler, "MAX_JSON_BYTES", len(data))
    request = _fake_request()

    async def stream():
        for value in data:
            yield bytes([value])

    request.stream = stream
    assert await handler.json_or_none(request) == {"text": "你好"}


def test_oversized_json_returns_http_413_without_mutating_bridge(monkeypatch):
    from starlette.testclient import TestClient

    from physiclaw.core.server.app import build_server

    monkeypatch.setattr(handler, "MAX_JSON_BYTES", 32)
    bundle = build_server()
    with TestClient(bundle.phone_app.build()) as client:
        response = client.post("/api/bridge/screen-dimension", content=b" " * 33)

    assert response.status_code == 413
    assert bundle.calib.screen_dimension is None


# ---------- serve_bridge_page ----------


@pytest.mark.asyncio
async def test_serve_bridge_page_reads_html_and_sets_no_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "bridge.html").write_text("<html>bridge</html>", encoding="utf-8")
    monkeypatch.setattr(handler, "STATIC_DIR", static)

    resp = await serve_bridge_page(_fake_request())

    assert resp.body == b"<html>bridge</html>"
    assert resp.headers["cache-control"] == "no-store"


# ---------- handle_phone_state ----------


@pytest.mark.asyncio
async def test_handle_phone_state_calls_poll_and_returns_state(mocker) -> None:
    bridge = BridgeState()
    cal = CalibrationState()
    phone = PageState(bridge, cal)

    resp = await handle_phone_state(_fake_request(), phone)

    import json

    body = json.loads(resp.body)
    assert body["mode"] == "bridge"
    # `bridge.poll` updates last_seen — was 0, now > 0.
    assert bridge.last_seen > 0


# ---------- handle_clipboard_copied ----------


@pytest.mark.asyncio
async def test_handle_clipboard_copied_marks_event() -> None:
    bridge = BridgeState()
    bridge.send_text("hi")

    bridge.send_text("example")
    request = _fake_request(json_obj={"text_id": bridge.clipboard_state()["text_id"]})

    resp = await handle_clipboard_copied(request, bridge)

    import json

    assert json.loads(resp.body) == {"ok": True}
    assert bridge._clipboard_copied.is_set()


# ---------- handle_screen_dimension ----------


@pytest.mark.asyncio
async def test_handle_screen_dimension_records_into_calib() -> None:
    cal = CalibrationState()

    resp = await handle_screen_dimension(
        _fake_request(
            json_obj={
                "screen_width": 1170,
                "screen_height": 2532,
                "viewport_width": 390,
                "viewport_height": 844,
            }
        ),
        cal,
    )

    import json

    assert json.loads(resp.body) == {"ok": True}
    assert cal.screen_dimension == {
        "width": 1170,
        "height": 2532,
        "viewport_width": 390,
        "viewport_height": 844,
    }


@pytest.mark.asyncio
async def test_handle_screen_dimension_rejects_missing_fields() -> None:
    # Missing keys int() to 0 — accepting them overwrote valid geometry
    # with zeros, stickily. 400, and the state stays untouched.
    cal = CalibrationState()

    resp = await handle_screen_dimension(_fake_request(json_obj={}), cal)

    assert resp.status_code == 400
    assert cal.screen_dimension is None


@pytest.mark.asyncio
async def test_handle_screen_dimension_rejects_zero_viewport() -> None:
    # A backgrounded tab reports a zero viewport — must not clobber state.
    cal = CalibrationState()
    body = {
        "screen_width": 390,
        "screen_height": 844,
        "viewport_width": 0,
        "viewport_height": 0,
    }

    resp = await handle_screen_dimension(_fake_request(json_obj=body), cal)

    assert resp.status_code == 400
    assert cal.screen_dimension is None


# ---------- handle_screenshot_upload ----------


@pytest.mark.asyncio
async def test_handle_screenshot_upload_stores_data_and_returns_size() -> None:
    bridge = BridgeState()
    bridge.clear_screenshot()  # arms the upload window

    resp = await handle_screenshot_upload(_fake_request(body_bytes=PNG), bridge)

    import json

    body = json.loads(resp.body)
    assert body == {"ok": True, "size": len(PNG)}
    assert bridge._screenshot_data == PNG


@pytest.mark.asyncio
async def test_handle_screenshot_upload_400_on_empty_body() -> None:
    bridge = BridgeState()
    bridge.clear_screenshot()

    resp = await handle_screenshot_upload(_fake_request(body_bytes=b""), bridge)

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_handle_screenshot_upload_403_when_not_armed(silenced_log) -> None:
    """No consumer expects an upload → an unsolicited POST is rejected."""
    bridge = BridgeState()

    resp = await handle_screenshot_upload(_fake_request(body_bytes=PNG), bridge)

    assert resp.status_code == 403
    assert bridge._screenshot_data is None


@pytest.mark.asyncio
async def test_handle_screenshot_upload_consumes_the_armed_window(
    silenced_log,
) -> None:
    """One armed window admits one upload — the second is rejected."""
    bridge = BridgeState()
    bridge.clear_screenshot()

    first = await handle_screenshot_upload(_fake_request(body_bytes=PNG), bridge)
    second = await handle_screenshot_upload(_fake_request(body_bytes=PNG), bridge)

    assert first.status_code == 200
    assert second.status_code == 403


@pytest.mark.asyncio
async def test_handle_screenshot_upload_pins_first_lan_ip(silenced_log) -> None:
    """Trust-on-first-use: with no fresh poller known, the first LAN
    uploader is admitted (and remembered); a different IP is rejected."""
    bridge = BridgeState()
    bridge.clear_screenshot()
    first = await handle_screenshot_upload(
        _fake_request(body_bytes=PNG, client_host="192.168.1.30"), bridge
    )
    assert first.status_code == 200

    bridge.clear_screenshot()
    other = await handle_screenshot_upload(
        _fake_request(body_bytes=PNG, client_host="192.168.1.66"), bridge
    )
    assert other.status_code == 403

    bridge.clear_screenshot()
    same = await handle_screenshot_upload(
        _fake_request(body_bytes=PNG, client_host="192.168.1.30"), bridge
    )
    assert same.status_code == 200


@pytest.mark.asyncio
async def test_handle_screenshot_upload_415_on_non_image(silenced_log) -> None:
    bridge = BridgeState()
    bridge.clear_screenshot()

    resp = await handle_screenshot_upload(
        _fake_request(body_bytes=b"<html>not an image</html>"), bridge
    )

    assert resp.status_code == 415
    assert bridge._screenshot_data is None


@pytest.mark.asyncio
async def test_handle_screenshot_upload_413_on_oversized_body(
    monkeypatch: pytest.MonkeyPatch, silenced_log
) -> None:
    monkeypatch.setattr(handler, "MAX_UPLOAD_BYTES", 8)
    bridge = BridgeState()
    bridge.clear_screenshot()

    streamed = await handle_screenshot_upload(_fake_request(body_bytes=PNG), bridge)
    declared = await handle_screenshot_upload(
        _fake_request(body_bytes=PNG, headers={"content-length": "999999"}), bridge
    )

    assert streamed.status_code == 413
    assert declared.status_code == 413
    assert bridge._screenshot_data is None


# ---------- handle_recent_screenshots ----------


def _fake_get(query: dict[str, str] | None = None):
    req = SimpleNamespace()
    req.query_params = query or {}
    return req


@pytest.mark.asyncio
async def test_handle_recent_screenshots_returns_base64_oldest_to_newest() -> None:
    import base64
    import json

    from physiclaw.core.bridge.handler import handle_recent_screenshots

    bridge = BridgeState()
    for p in (b"a", b"b"):
        bridge.receive_screenshot(p)

    resp = await handle_recent_screenshots(_fake_get(), bridge)

    shots = json.loads(resp.body)["screenshots"]
    assert [base64.b64decode(s) for s in shots] == [b"a", b"b"]


@pytest.mark.asyncio
async def test_handle_recent_screenshots_honours_n_param() -> None:
    import json

    from physiclaw.core.bridge.handler import handle_recent_screenshots

    bridge = BridgeState()
    for p in (b"a", b"b", b"c"):
        bridge.receive_screenshot(p)

    resp = await handle_recent_screenshots(_fake_get({"n": "1"}), bridge)

    assert len(json.loads(resp.body)["screenshots"]) == 1


@pytest.mark.asyncio
async def test_handle_recent_screenshots_defaults_on_bad_n() -> None:
    import json

    from physiclaw.core.bridge.handler import handle_recent_screenshots

    bridge = BridgeState()
    bridge.receive_screenshot(b"a")

    resp = await handle_recent_screenshots(_fake_get({"n": "notint"}), bridge)

    assert len(json.loads(resp.body)["screenshots"]) == 1


# ---------- handle_clipboard_fetch ----------


@pytest.mark.asyncio
async def test_handle_clipboard_fetch_returns_text_when_queued() -> None:
    bridge = BridgeState()
    bridge.send_text("payload")

    resp = await handle_clipboard_fetch(_fake_request(), bridge)

    assert resp.body == b"payload"
    assert bridge._clipboard_copied.is_set()


@pytest.mark.asyncio
async def test_handle_clipboard_fetch_returns_204_when_no_text() -> None:
    bridge = BridgeState()

    resp = await handle_clipboard_fetch(_fake_request(), bridge)

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_handle_clipboard_fetch_coerces_non_string_value(mocker) -> None:
    bridge = BridgeState()
    bridge._text = 42  # type: ignore[assignment]
    # Pretend it's queued so fetch returns it.

    resp = await handle_clipboard_fetch(_fake_request(), bridge)

    assert resp.body == b"42"


# ---------- handle_mode_switch ----------


@pytest.mark.asyncio
async def test_mode_switch_bridge_returns_ok() -> None:
    bridge = BridgeState()
    cal = CalibrationState()
    phone = PageState(bridge, cal)

    resp = await handle_mode_switch(_fake_request(json_obj={"mode": "bridge"}), phone)

    import json

    body = json.loads(resp.body)
    assert body == {"ok": True, "mode": "bridge"}


@pytest.mark.asyncio
async def test_mode_switch_calibrate_with_phase_passes_kwargs() -> None:
    bridge = BridgeState()
    cal = CalibrationState()
    phone = PageState(bridge, cal)

    resp = await handle_mode_switch(
        _fake_request(
            json_obj={
                "mode": "calibrate",
                "phase": "dot",
                "dot_x": 0.3,
                "dot_y": 0.7,
            }
        ),
        phone,
    )

    import json

    body = json.loads(resp.body)
    assert body == {"ok": True, "mode": "calibrate", "phase": "dot"}
    assert cal.phase == "dot"
    assert cal.dot_position == (0.3, 0.7)


@pytest.mark.asyncio
async def test_mode_switch_400_on_unknown_mode() -> None:
    phone = PageState(BridgeState(), CalibrationState())

    resp = await handle_mode_switch(_fake_request(json_obj={"mode": "weird"}), phone)

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_mode_switch_400_on_calibrate_without_phase() -> None:
    phone = PageState(BridgeState(), CalibrationState())

    resp = await handle_mode_switch(
        _fake_request(json_obj={"mode": "calibrate"}), phone
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_mode_switch_400_on_unknown_phase() -> None:
    phone = PageState(BridgeState(), CalibrationState())

    resp = await handle_mode_switch(
        _fake_request(
            json_obj={
                "mode": "calibrate",
                "phase": "non-existent",
            }
        ),
        phone,
    )

    assert resp.status_code == 400


# ---------- serve_qr_page ----------


@pytest.mark.asyncio
async def test_serve_qr_page_substitutes_phone_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "qr.html").write_text(
        "<html>__PHONE_URL__ / __PHONE_URL_FALLBACK__</html>", encoding="utf-8"
    )
    monkeypatch.setattr(handler, "STATIC_DIR", static)
    mocker.patch.object(
        handler,
        "bridge_base_urls",
        return_value=("http://mac.local:8048", "http://192.168.1.1:8048"),
    )

    resp = await serve_qr_page(_fake_request(url_port=8048))

    body = resp.body.decode()
    assert "http://mac.local:8048/bridge / http://192.168.1.1:8048/bridge" in body


@pytest.mark.asyncio
async def test_serve_qr_page_uses_default_port_when_url_port_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "qr.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(handler, "STATIC_DIR", static)
    base_urls = mocker.patch.object(
        handler,
        "bridge_base_urls",
        return_value=("p", "f"),
    )

    await serve_qr_page(_fake_request(url_port=None))

    base_urls.assert_called_once_with(8048)


# ---------- handle_calib_touch ----------


def _touch_ready_cal() -> CalibrationState:
    """A CalibrationState with a touch-consuming phase on screen and the
    viewport shift measured — the state every touch-driven step runs in."""
    cal = CalibrationState()
    cal.set_phase("grid")
    cal.viewport_shift = ViewportShift(
        offset_x=0,
        offset_y=0,
        dpr=1.0,
        screenshot_width=200,
        screenshot_height=400,
    )
    return cal


@pytest.mark.asyncio
async def test_handle_calib_touch_records_touch_with_screenshot_pcts() -> None:
    cal = _touch_ready_cal()

    resp = await handle_calib_touch(
        _fake_request(json_obj={"clientX": 100, "clientY": 200, "phase_id": 1}),
        cal,
    )

    import json

    assert json.loads(resp.body) == {"ok": True}
    assert len(cal.touches) == 1
    touch = cal.touches[0]
    assert touch["x"] == 0.5  # 100/200
    assert touch["y"] == 0.5  # 200/400


@pytest.mark.asyncio
async def test_handle_calib_touch_409_when_no_step_waiting() -> None:
    """An idle server has no calibration step collecting touches — a
    report then is rejected before it can reach the touch queue."""
    cal = _touch_ready_cal()
    cal.set_phase("idle")

    resp = await handle_calib_touch(
        _fake_request(json_obj={"clientX": 100, "clientY": 200, "phase_id": 1}),
        cal,
    )

    assert resp.status_code == 409
    assert cal.touches == []


# ---------- malformed-input guards (400/409, never 500) ----------


@pytest.mark.asyncio
async def test_handle_screen_dimension_400_on_malformed_json() -> None:
    resp = await handle_screen_dimension(
        _fake_request(body_bytes=b"not json"), CalibrationState()
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_handle_screen_dimension_400_on_non_numeric_fields() -> None:
    resp = await handle_screen_dimension(
        _fake_request(json_obj={"screen_width": "wide"}), CalibrationState()
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_mode_switch_400_on_malformed_json() -> None:
    bridge = BridgeState()
    cal = CalibrationState()

    resp = await handle_mode_switch(
        _fake_request(body_bytes=b"not json"), PageState(bridge, cal)
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_handle_calib_touch_400_on_missing_coords() -> None:
    cal = CalibrationState()
    cal.set_phase("grid")

    resp = await handle_calib_touch(_fake_request(json_obj={"clientX": 100}), cal)

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_handle_calib_touch_409_before_viewport_shift_measured() -> None:
    """A touch that lands before pre-cal has measured the shift is a
    state conflict — the phone page is ahead of the server, not broken."""
    cal = CalibrationState()  # viewport_shift is None
    cal.set_phase("grid")

    resp = await handle_calib_touch(
        _fake_request(json_obj={"clientX": 100, "clientY": 200, "phase_id": 1}), cal
    )

    assert resp.status_code == 409
    assert cal.touches == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [float("inf"), float("nan"), True, 10**400])
async def test_regression_invalid_dimension_preserves_previous_geometry(value):
    cal = CalibrationState()
    previous = {
        "width": 200,
        "height": 400,
        "viewport_width": 200,
        "viewport_height": 400,
    }
    cal.screen_dimension = previous
    body = {
        "screen_width": value,
        "screen_height": 400,
        "viewport_width": 200,
        "viewport_height": 400,
    }

    response = await handle_screen_dimension(_fake_request(json_obj=body), cal)

    assert response.status_code == 400
    assert cal.screen_dimension is previous


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), "-Infinity", True, 10**400]
)
@pytest.mark.parametrize("field", ["clientX", "clientY"])
async def test_regression_nonfinite_touch_never_enters_calibration(value, field):
    cal = _touch_ready_cal()
    body = {
        "clientX": 100,
        "clientY": 200,
        "phase_id": cal.get_state()["phase_id"],
        field: value,
    }

    response = await handle_calib_touch(_fake_request(json_obj=body), cal)

    assert response.status_code == 400
    assert cal.flush_touches() == []


@pytest.mark.asyncio
async def test_touch_conversion_overflow_never_enters_calibration():
    cal = _touch_ready_cal()
    cal.viewport_shift = ViewportShift(0, 0, 3, 200, 400)
    body = {"clientX": 1e308, "clientY": 200, "phase_id": cal.get_state()["phase_id"]}

    response = await handle_calib_touch(_fake_request(json_obj=body), cal)

    assert response.status_code == 400
    assert cal.flush_touches() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, {"text_id": True}, {"text_id": "1"}])
async def test_clipboard_ack_requires_a_typed_text_identity(body):
    bridge = BridgeState()
    bridge.send_text("new")

    response = await handle_clipboard_copied(_fake_request(json_obj=body), bridge)

    assert response.status_code == 400
    assert not bridge.wait_clipboard(timeout=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("next_text", ["old", "new"])
async def test_regression_old_clipboard_ack_cannot_confirm_new_queue(next_text):
    bridge = BridgeState()
    bridge.send_text("old")
    old = bridge.clipboard_state()
    bridge.send_text(next_text)

    response = await handle_clipboard_copied(
        _fake_request(json_obj={"text_id": old["text_id"]}), bridge
    )

    assert response.status_code == 409
    assert not bridge.wait_clipboard(timeout=0)
    assert bridge.current_text() == next_text


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["idle", "dot", "drain"])
async def test_regression_touch_body_read_cannot_cross_collection_boundary(transition):
    cal = _touch_ready_cal()
    body = {"clientX": 100, "clientY": 200, "phase_id": cal.get_state()["phase_id"]}
    actions = {
        "idle": lambda: cal.set_phase("idle"),
        "dot": lambda: cal.set_phase("dot"),
        "drain": cal.flush_touches,
    }
    request = _fake_request()

    async def read_body():
        actions[transition]()
        yield json.dumps(body).encode()

    request.stream = read_body

    response = await handle_calib_touch(request, cal)

    assert response.status_code == 409
    assert cal.flush_touches() == []


@pytest.mark.asyncio
async def test_old_rendered_dot_cannot_report_touch_for_new_dot():
    cal = _touch_ready_cal()
    cal.set_phase("dot", dot_x=0.2)
    body = {"clientX": 40, "clientY": 200, "phase_id": cal.get_state()["phase_id"]}
    cal.set_phase("dot", dot_x=0.8)

    response = await handle_calib_touch(_fake_request(json_obj=body), cal)

    assert response.status_code == 409
    assert cal.flush_touches() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [{"clientX": 1, "clientY": 1}, {"clientX": 1, "clientY": 1, "phase_id": True}],
)
async def test_touch_requires_rendered_phase_identity(body):
    cal = _touch_ready_cal()

    response = await handle_calib_touch(_fake_request(json_obj=body), cal)

    assert response.status_code == 400
    assert cal.flush_touches() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("clear_first", [False, True])
async def test_regression_old_stream_cannot_overwrite_a_newer_upload(clear_first):
    bridge = BridgeState()
    bridge.clear_screenshot()
    request = _fake_request()
    newer = PNG + b"new"

    async def stream():
        if clear_first:
            bridge.clear_screenshot()
        accepted = await handle_screenshot_upload(
            _fake_request(body_bytes=newer), bridge
        )
        assert accepted.status_code == 200
        yield PNG + b"old"

    request.stream = stream

    response = await handle_screenshot_upload(request, bridge)

    assert response.status_code == 409
    assert bridge.take_pending_screenshot() == newer
    assert bridge.recent_screenshots() == [newer]
    assert not bridge.upload_armed()


@pytest.mark.asyncio
async def test_old_stream_cannot_consume_newly_cleared_window():
    bridge = BridgeState()
    bridge.clear_screenshot()
    request = _fake_request()

    async def stream():
        bridge.clear_screenshot()
        yield PNG

    request.stream = stream

    response = await handle_screenshot_upload(request, bridge)

    assert response.status_code == 409
    assert bridge.upload_armed()
    assert bridge.take_pending_screenshot() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"mode": "calibrate", "phase": ["grid"]},
        {"mode": "calibrate", "phase": "dot", "dot_x": float("nan")},
        {"mode": "calibrate", "phase": "assistive_touch", "nonce_bits": 1},
    ],
)
async def test_invalid_mode_request_preserves_pollable_phone_state(body):
    phone = PageState(BridgeState(), CalibrationState())
    before = phone.get_state()

    response = await handle_mode_switch(_fake_request(json_obj=body), phone)
    poll = await handle_phone_state(_fake_request(), phone)

    assert response.status_code == 400
    assert poll.status_code == 200
    assert phone.get_state() == before


@pytest.mark.asyncio
async def test_nonfinite_touch_is_bad_input_even_before_viewport_calibration():
    cal = CalibrationState()
    cal.set_phase("grid")
    body = {
        "clientX": float("nan"),
        "clientY": 0,
        "phase_id": cal.get_state()["phase_id"],
    }

    response = await handle_calib_touch(_fake_request(json_obj=body), cal)

    assert response.status_code == 400
    assert cal.flush_touches() == []
