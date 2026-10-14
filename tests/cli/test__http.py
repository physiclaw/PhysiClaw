"""Tests for `physiclaw.cli._http` — the CLI's one HTTP layer.

`api`/`fetch_json` are tested at the `_OPENER` seam; `http_get`/`stream`
directly here (and end-to-end by the download-path tests in
setup/test_vision and test_sync_official_skills).

Accepted mutation survivors: one-second default timeout changes, diagnostic
wording/progress style, the approximate MiB display divisor, and hiding the
bar at exactly sys.maxsize (truncation detection is unchanged). Replacing the
missing length fallback with nonnumeric text still means unknown length.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.cli import _http

BASE = "http://localhost:8048"


def _resp(payload: dict | bytes) -> io.BytesIO:
    """Build a context-manager-compatible urllib response."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return io.BytesIO(body)


# ---------- api ----------


def test_api_get_no_body(mocker) -> None:
    spy = mocker.patch.object(
        _http._OPENER,
        "open",
        return_value=_resp({"x": 1}),
    )

    out = _http.api(BASE, "GET", "/api/status")

    assert out == {"x": 1}
    req = spy.call_args.args[0]
    assert req.method == "GET"
    assert req.full_url == f"{BASE}/api/status"


def test_api_post_with_body(mocker) -> None:
    spy = mocker.patch.object(
        _http._OPENER,
        "open",
        return_value=_resp({"status": "ok"}),
    )

    out = _http.api(BASE, "POST", "/api/x", body={"k": "v"})

    assert out["status"] == "ok"
    req = spy.call_args.args[0]
    assert req.headers["Content-type"] == "application/json"


def test_api_post_no_body_sends_empty_bytes(mocker) -> None:
    spy = mocker.patch.object(
        _http._OPENER,
        "open",
        return_value=_resp({"status": "ok"}),
    )

    _http.api(BASE, "POST", "/api/x")

    # Empty bytes body for POST.
    req = spy.call_args.args[0]
    assert req.data == b""


def test_api_returns_parsed_error_body_on_http_error(mocker) -> None:
    err = urllib.error.HTTPError(
        url="x",
        code=500,
        msg="boom",
        hdrs=None,
        fp=None,
    )
    err.read = lambda: b'{"status": "error", "message": "x"}'
    mocker.patch.object(_http._OPENER, "open", side_effect=err)

    out = _http.api(BASE, "GET", "/api/x")

    assert out == {"status": "error", "message": "x"}


def test_api_returns_none_on_unparseable_error_body(mocker) -> None:
    err = urllib.error.HTTPError(
        url="x",
        code=500,
        msg="boom",
        hdrs=None,
        fp=None,
    )
    err.read = lambda: b"not json"
    mocker.patch.object(_http._OPENER, "open", side_effect=err)

    assert _http.api(BASE, "GET", "/api/x") is None


def test_api_returns_none_on_connection_error(mocker) -> None:
    mocker.patch.object(
        _http._OPENER,
        "open",
        side_effect=ConnectionError("refused"),
    )

    assert _http.api(BASE, "GET", "/api/x") is None


# ---------- fetch_json ----------


def test_fetch_json_returns_parsed_body(mocker) -> None:
    mocker.patch.object(
        _http._OPENER,
        "open",
        return_value=_resp({"connected": True}),
    )

    assert _http.fetch_json(f"{BASE}/api/bridge/state") == {"connected": True}


def test_fetch_json_raises_on_transport_error(mocker) -> None:
    # Unlike `api`, callers see the failure detail.
    mocker.patch.object(
        _http._OPENER,
        "open",
        side_effect=urllib.error.URLError("refused"),
    )

    with pytest.raises(urllib.error.URLError):
        _http.fetch_json(f"{BASE}/api/bridge/state")


def test_fetch_json_raises_on_bad_json(mocker) -> None:
    mocker.patch.object(_http._OPENER, "open", return_value=_resp(b"not json"))

    with pytest.raises(ValueError):
        _http.fetch_json(f"{BASE}/api/bridge/state")


# ---------- http_get / stream ----------


class _StreamResp:
    """Minimal urlopen-response stand-in for `stream`: chunked ``read``
    plus a Content-Length header."""

    def __init__(self, data: bytes, content_length: str | None = None) -> None:
        self._buf = io.BytesIO(data)
        self._len = content_length
        self._eof = False

    def read(self, n: int = -1) -> bytes:
        # Fail immediately if a caller spins on EOF, rather than timing out.
        assert not self._eof, "read again after EOF"
        chunk = self._buf.read(n)
        self._eof = not chunk
        return chunk

    def getheader(self, name: str, default=None):
        if name.lower() == "content-length" and self._len is not None:
            return self._len
        return default


def test_http_get_sets_user_agent(mocker) -> None:
    # Cloudflare's WAF 403s the default Python-urllib UA — http_get must
    # pin one on every request.
    spy = mocker.patch.object(_http.urllib.request, "urlopen")

    _http.http_get("https://example.com/x")

    req = spy.call_args.args[0]
    assert req.get_header("User-agent") == "physiclaw"


def test_stream_writes_all_bytes_with_known_length() -> None:
    data = b"x" * 5000
    out = bytearray()

    _http.stream(_StreamResp(data, content_length=str(len(data))), out.extend, "test")

    assert bytes(out) == data


def test_stream_writes_all_bytes_with_unknown_length() -> None:
    data = b"y" * 5000
    out = bytearray()

    _http.stream(_StreamResp(data), out.extend, "test")  # no Content-Length

    assert bytes(out) == data


def test_stream_raises_when_body_shorter_than_content_length() -> None:
    # A dropped connection makes read() end early with NO error — the
    # Content-Length check must turn that silent truncation into a raise
    # (URLError subclass, so every caller's fallback path catches it).
    out = bytearray()

    with pytest.raises(urllib.error.ContentTooShortError):
        _http.stream(
            _StreamResp(b"z" * 3000, content_length="5000"), out.extend, "test"
        )


def test_stream_raises_on_truncation_in_quiet_mode_too() -> None:
    with pytest.raises(urllib.error.ContentTooShortError):
        _http.stream(
            _StreamResp(b"z" * 10, content_length="5000"),
            bytearray().extend,
            "test",
            progress=False,
        )


@pytest.mark.parametrize("body", [{}, [], False, 0, ""])
def test_regression_api_preserves_explicit_empty_json_body(mocker, body):
    spy = mocker.patch.object(_http._OPENER, "open", return_value=_resp({"ok": True}))

    result = _http.api(BASE, "POST", "/api/x", body=body)

    assert result == {"ok": True}
    request = spy.call_args.args[0]
    assert json.loads(request.data) == body
    assert request.get_header("Content-type") == "application/json"


def test_regression_api_malformed_url_fails_softly():
    result = _http.api("invalid", "GET", "/api/x")

    assert result is None


def test_regression_api_unserializable_body_fails_softly(mocker):
    spy = mocker.patch.object(_http._OPENER, "open")

    result = _http.api(BASE, "POST", "/api/x", body={"bad": object()})

    assert result is None
    spy.assert_not_called()


@pytest.mark.parametrize(
    "payload, expected",
    [(b'{"error":"failed"}', {"error": "failed"}), (b"broken", None)],
)
def test_regression_api_closes_http_error_response(mocker, payload, expected):
    body = io.BytesIO(payload)
    error = urllib.error.HTTPError(BASE, 500, "failed", None, body)
    mocker.patch.object(_http._OPENER, "open", side_effect=error)

    result = _http.api(BASE, "GET", "/api/x")

    assert result == expected
    assert body.closed


@pytest.mark.parametrize("progress", [False, True])
def test_regression_stream_nonnumeric_length_does_not_abort_download(progress):
    out = bytearray()

    _http.stream(_StreamResp(b"payload", "²"), out.extend, "test", progress=progress)

    assert out == b"payload"


@pytest.mark.parametrize("progress", [False, True])
def test_regression_stream_oversized_integer_raises_transport_error(progress):
    out = bytearray()

    with pytest.raises(urllib.error.URLError, match="invalid Content-Length"):
        _http.stream(
            _StreamResp(b"payload", "9" * 5000), out.extend, "test", progress=progress
        )

    assert out == b""


def test_regression_stream_huge_length_raises_transport_error_after_reading():
    out = bytearray()

    with pytest.raises(urllib.error.ContentTooShortError, match="retrieval incomplete"):
        _http.stream(_StreamResp(b"payload", str(10**400)), out.extend, "test")

    assert out == b"payload"


@pytest.mark.parametrize(
    "payload, expected",
    [(b'{"ok":true}', {"ok": True}), (b"broken", None), (b"\xff", None)],
)
def test_api_closes_success_response_even_when_json_is_invalid(
    mocker, payload, expected
):
    response = _resp(payload)
    spy = mocker.patch.object(_http._OPENER, "open", return_value=response)

    result = _http.api(BASE, "GET", "/api/x", timeout=3.5)

    assert result == expected
    assert response.closed
    assert spy.call_args.kwargs["timeout"] == 3.5


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_api_does_not_swallow_process_interruptions(mocker, exception):
    mocker.patch.object(_http._OPENER, "open", side_effect=exception)

    with pytest.raises(exception):
        _http.api(BASE, "GET", "/api/x")


@pytest.mark.parametrize("size", [0, 1, 65535, 65536, 65537, 131073])
@pytest.mark.parametrize("progress", [False, True])
def test_stream_chunk_boundaries_preserve_bytes(size, progress):
    data = (b"\x00\xff\x01" * size)[:size]
    chunks = []

    _http.stream(_StreamResp(data, str(size)), chunks.append, "test", progress=progress)

    assert b"".join(chunks) == data
    assert all(0 < len(chunk) <= 65536 for chunk in chunks)


@given(
    data=st.binary(max_size=3000),
    missing=st.integers(min_value=1, max_value=100_000),
    progress=st.booleans(),
)
def test_stream_truncation_preserves_received_prefix_and_raises(
    data, missing, progress
):
    out = bytearray()
    response = _StreamResp(data, str(len(data) + missing))

    with pytest.raises(urllib.error.ContentTooShortError, match="retrieval incomplete"):
        _http.stream(response, out.extend, "test", progress=progress)

    assert out == data


@pytest.mark.parametrize("header", [None, "", "n/a", "-10", "1.5", "١٢"])
def test_stream_unknown_length_stays_quiet_and_preserves_data(header, capsys):
    out = bytearray()

    _http.stream(_StreamResp(b"payload", header), out.extend, "download")

    assert out == b"payload"
    assert capsys.readouterr().out == ""


def test_stream_whitespace_length_still_detects_truncation():
    with pytest.raises(urllib.error.ContentTooShortError, match="7 out of 8 bytes"):
        _http.stream(_StreamResp(b"payload", " 8 \t"), bytearray().extend, "test")


def test_stream_progress_can_be_disabled(capsys):
    _http.stream(
        _StreamResp(b"data", "4"), bytearray().extend, "download", progress=False
    )

    assert capsys.readouterr().out == ""


def test_stream_known_length_displays_progress_label(capsys):
    _http.stream(_StreamResp(b"data", "4"), bytearray().extend, "download")

    assert "download (0.0 MiB)" in capsys.readouterr().out


def test_stream_write_failure_reaches_caller():
    def fail_write(chunk):
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        _http.stream(_StreamResp(b"data", "4"), fail_write, "download")


def test_fetch_json_closes_response_when_parsing_fails(mocker):
    response = _resp(b"broken")
    spy = mocker.patch.object(_http._OPENER, "open", return_value=response)

    with pytest.raises(ValueError):
        _http.fetch_json(BASE, timeout=4.5)

    assert response.closed
    assert spy.call_args.kwargs["timeout"] == 4.5
