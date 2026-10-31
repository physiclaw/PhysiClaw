"""Tests for `physiclaw.cli.studio` — public commands and real ASGI routes.

Whole-module mutation review detects 48 of 60 variants. Accepted variants
only change display text, bounded polling/join timing, or equivalent strip
character sets for the fixed review route and configured host/port URL.
The 17 variants on changed server lines and seven viewer confinement
variants are all detected by their public behavior tests.
"""

from __future__ import annotations

import importlib
import json
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import typer
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from starlette.testclient import TestClient
from typer.testing import CliRunner

studio_mod = importlib.import_module("physiclaw.cli.studio")

BASE = "http://127.0.0.1:8048"
STUDIO_URL = "http://127.0.0.1:8058"
app = typer.Typer()
app.command()(studio_mod.studio)
runner = CliRunner()


@pytest.fixture
def launched(mocker, physiclaw_home):
    """Capture the real ASGI app without binding a port or opening a browser."""
    servers = []

    class Server:
        def __init__(self, config):
            self.config = config
            self.started = False
            servers.append(self)

        def run(self):
            pass

    mocker.patch("uvicorn.Server", Server)
    mocker.patch("physiclaw.common.runtime_state.read_live", return_value=None)
    mocker.patch("physiclaw.common.config.server_url", return_value=BASE)
    mocker.patch("physiclaw.common.ready.check_ready_once", return_value=False)
    return servers


def _recording(directory: Path, marker: str) -> Path:
    (directory / "images").mkdir(parents=True)
    (directory / "images" / "frame.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (directory / "summary.json").write_text(
        json.dumps({"outcome": {"sentinel": marker}}), encoding="utf-8"
    )
    (directory / "events.jsonl").write_text("", encoding="utf-8")
    return directory


def _printed_path(result) -> str:
    url = next(
        line.split("Open Studio: ", 1)[1]
        for line in result.stderr.splitlines()
        if "Open Studio: http://127.0.0.1:8058/" in line
    )
    return urlsplit(url).path


@pytest.mark.parametrize(
    "dirname", ["external-recording", "20261015-000000-abc123", "记录 #%"]
)
def test_regression_studio_review_serves_selected_directory(
    launched, tmp_path, dirname
):
    from physiclaw.common import paths

    _recording(paths.sessions_dir() / "20261015-000000-abc123", "WRONG_INTERNAL")
    selected = _recording(tmp_path / "export" / dirname, "SELECTED_EXTERNAL")

    result = runner.invoke(app, ["--review", "--session", str(selected), "--no-open"])
    with TestClient(launched[0].config.app, base_url=STUDIO_URL) as client:
        page = client.get(_printed_path(result))
        frame = client.get(_printed_path(result) + "images/frame.jpg")

    assert result.exit_code == 0
    assert page.status_code == 200
    assert '"sentinel": "SELECTED_EXTERNAL"' in page.text
    assert '"summary": {"outcome": {"sentinel": "WRONG_INTERNAL"' not in page.text
    assert frame.status_code == 200
    assert frame.content == b"\xff\xd8\xff\xd9"


def test_studio_selected_recording_does_not_replace_internal_browsing(
    launched, tmp_path
):
    from physiclaw.common import paths

    internal = _recording(paths.sessions_dir() / "20261015-000000-abc123", "INTERNAL")
    selected = _recording(tmp_path / "export" / internal.name, "EXTERNAL")

    result = runner.invoke(app, ["--review", "--session", str(selected), "--no-open"])
    with TestClient(launched[0].config.app, base_url=STUDIO_URL) as client:
        chosen = client.get(_printed_path(result))
        ordinary = client.get(f"/sessions/{internal.name}/")

    assert chosen.status_code == ordinary.status_code == 200
    assert '"sentinel": "EXTERNAL"' in chosen.text
    assert '"summary": {"outcome": {"sentinel": "INTERNAL"' in ordinary.text
    assert '"review_url": "/review/"' in chosen.text
    assert '"review_url"' not in ordinary.text


def _state(launched):
    with TestClient(launched[-1].config.app, base_url=STUDIO_URL) as client:
        return client.get("/api/state").json()


def test_live_server_record_wins_without_probing(launched, mocker):
    mocker.patch(
        "physiclaw.common.runtime_state.read_live",
        return_value={"pid": 1, "host": "0.0.0.0", "port": 9999},
    )
    probe = mocker.patch(
        "physiclaw.common.ready.check_ready_once",
        side_effect=AssertionError("must not probe"),
    )

    result = runner.invoke(app, ["--no-open"])

    assert result.exit_code == 0
    assert _state(launched)["mcp_url"] == "http://127.0.0.1:9999/mcp"
    probe.assert_not_called()


@pytest.mark.parametrize(
    "reply",
    [
        False,
        True,
        httpx.HTTPStatusError("503", request=None, response=None),
        httpx.ReadTimeout("slow"),
    ],
)
def test_studio_accepts_listening_server_before_it_is_ready(launched, mocker, reply):
    probe = mocker.patch(
        "physiclaw.common.ready.check_ready_once", side_effect=_reply(reply)
    )
    mocker.patch("physiclaw.common.config.server_url", return_value=BASE + "/")

    result = runner.invoke(app, ["--no-open"])

    assert result.exit_code == 0
    assert _state(launched)["mcp_url"] == BASE + "/mcp"
    probe.assert_called_once_with(BASE, timeout=1.0)


def _reply(value):
    def answer(*args, **kwargs):
        if isinstance(value, Exception):
            raise value
        return value

    return answer


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("refused"), httpx.ConnectTimeout("timeout")]
)
def test_studio_requires_existing_mcp_server(launched, mocker, error):
    mocker.patch("physiclaw.common.ready.check_ready_once", side_effect=error)

    result = runner.invoke(app, ["--no-open"])

    assert result.exit_code == 1
    assert "physiclaw mcp" in result.stderr
    assert BASE in result.stderr
    assert launched == []


def test_studio_explicit_address_and_port_are_preserved(launched, mocker):
    probe = mocker.patch("physiclaw.common.ready.check_ready_once")

    result = runner.invoke(
        app, ["--mcp-url", "http://127.0.0.1:9998", "--port", "8123", "--no-open"]
    )

    assert result.exit_code == 0
    assert _state(launched)["mcp_url"] == "http://127.0.0.1:9998/mcp"
    assert launched[0].config.port == 8123
    assert launched[0].config.host == "127.0.0.1"
    assert "http://127.0.0.1:8123/" in result.stderr
    probe.assert_not_called()


@pytest.mark.parametrize(
    ("arguments", "live", "expected"),
    [
        ([], {"pid": 1, "host": "0.0.0.0", "port": 9997}, "http://127.0.0.1:9997/mcp"),
        (["--mcp-url", "http://127.0.0.1:9996"], None, "http://127.0.0.1:9996/mcp"),
        ([], None, "http://127.0.0.1:9995/mcp"),
    ],
)
def test_studio_review_preserves_driver_address_without_probing(
    launched, mocker, arguments, live, expected
):
    mocker.patch("physiclaw.common.runtime_state.read_live", return_value=live)
    mocker.patch(
        "physiclaw.common.config.server_url", return_value="http://127.0.0.1:9995"
    )
    probe = mocker.patch("physiclaw.common.ready.check_ready_once")

    result = runner.invoke(app, ["--review", "--no-open", *arguments])

    assert result.exit_code == 0
    assert _state(launched)["mcp_url"] == expected
    probe.assert_not_called()


@pytest.mark.parametrize("port", ["-1", "0", "65536"])
def test_studio_rejects_invalid_listening_ports(launched, port):
    result = runner.invoke(app, ["--review", "--port", port, "--no-open"])

    assert result.exit_code == 2
    assert "--port" in typer.unstyle(result.output)
    assert launched == []


def test_studio_review_without_selection_opens_newest_without_mcp(launched, mocker):
    from physiclaw.common import paths

    recording = _recording(paths.sessions_dir() / "20260908-100000-abc123", "LATEST")
    probe = mocker.patch("physiclaw.common.ready.check_ready_once")

    result = runner.invoke(app, ["--review", "--no-open"])
    with TestClient(launched[0].config.app, base_url=STUDIO_URL) as client:
        page = client.get(_printed_path(result))

    assert result.exit_code == 0
    assert str(page.url).endswith(f"/sessions/{recording.name}/")
    assert '"sentinel": "LATEST"' in page.text
    probe.assert_not_called()


def test_studio_session_suffix_resolves_to_selected_recording(launched):
    from physiclaw.common import paths

    _recording(paths.sessions_dir() / "20260908-100000-abc123", "SUFFIX_SELECTED")

    result = runner.invoke(app, ["--review", "--session", "abc123", "--no-open"])
    with TestClient(launched[0].config.app, base_url=STUDIO_URL) as client:
        page = client.get(_printed_path(result))

    assert result.exit_code == 0
    assert '"sentinel": "SUFFIX_SELECTED"' in page.text


def test_studio_missing_session_is_reported_before_startup(launched):
    result = runner.invoke(app, ["--review", "--session", "missing", "--no-open"])

    assert result.exit_code == 1
    assert "no session matches" in result.stderr
    assert launched == []


def test_studio_recorded_driver_requires_screen_frames(launched, tmp_path):
    result = runner.invoke(app, ["--session", str(tmp_path), "--no-open"])

    assert result.exit_code == 2
    assert "model.jsonl" in result.output
    assert launched == []


def test_studio_recorded_driver_uses_selected_frames_without_mcp(
    launched, tmp_path, mocker
):
    from physiclaw.common.listing import LISTING_HEADER

    selected = _recording(tmp_path / "recorded", "RECORDED")
    (selected / "events.jsonl").write_text(
        json.dumps(
            {
                "event": "tool_result",
                "text": LISTING_HEADER + '\n1 [text] "hello" [0.1,0.2,0.3,0.4] 0.95',
                "images": ["images/frame.jpg"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    probe = mocker.patch("physiclaw.common.ready.check_ready_once")

    result = runner.invoke(app, ["--session", str(selected), "--no-open"])

    assert result.exit_code == 0
    assert "Recorded session recorded | frames=1" in result.stderr
    assert _state(launched)["mcp_url"] == "mock:recorded"
    probe.assert_not_called()


@pytest.mark.parametrize(
    "error", [PermissionError("denied"), ValueError("invalid frames")]
)
def test_studio_recorded_read_failure_is_a_parameter_error(
    launched, tmp_path, mocker, error
):
    mocker.patch("physiclaw.studio.mock.session_frames", side_effect=error)

    result = runner.invoke(app, ["--session", str(tmp_path), "--no-open"])

    assert result.exit_code == 2
    assert str(error) in result.output
    assert launched == []


def test_studio_path_expansion_failure_is_a_parameter_error(launched, mocker):
    mocker.patch.object(Path, "expanduser", side_effect=RuntimeError("unknown home"))

    result = runner.invoke(app, ["--review", "--session", "~missing-user", "--no-open"])

    assert result.exit_code == 2
    assert "unknown home" in result.output
    assert launched == []


def test_studio_no_open_creates_no_browser_thread(launched, mocker):
    thread = mocker.patch.object(studio_mod.threading, "Thread")
    browser = mocker.patch.object(studio_mod.webbrowser, "open")

    result = runner.invoke(app, ["--review", "--no-open"])

    assert result.exit_code == 0
    thread.assert_not_called()
    browser.assert_not_called()


def test_studio_opens_browser_only_after_server_started(launched, mocker):
    import uvicorn

    opened = threading.Event()
    observations = []
    browser = mocker.patch.object(
        studio_mod.webbrowser,
        "open",
        side_effect=lambda url: (
            observations.append(launched[0].started),
            opened.set(),
        ),
    )

    def run(server):
        assert not opened.is_set()
        server.started = True
        assert opened.wait(2)

    mocker.patch.object(uvicorn.Server, "run", autospec=True, side_effect=run)

    result = runner.invoke(app, ["--review"])

    assert result.exit_code == 0
    assert observations == [True]
    browser.assert_called_once_with("http://127.0.0.1:8058/sessions/")


def test_studio_browser_waits_through_unfinished_startup(launched, mocker):
    import uvicorn

    inspected = threading.Event()
    ready = threading.Event()
    opened = threading.Event()
    observations = []

    def started(server):
        value = ready.is_set()
        observations.append(value)
        inspected.set()
        return value

    def run(server):
        type(server).started = property(started)
        assert inspected.wait(2)
        assert not opened.is_set()
        ready.set()
        assert opened.wait(2)

    mocker.patch.object(uvicorn.Server, "run", autospec=True, side_effect=run)
    mocker.patch.object(
        studio_mod.webbrowser, "open", side_effect=lambda url: opened.set()
    )

    result = runner.invoke(app, ["--review"])

    assert result.exit_code == 0
    assert observations[0] is False
    assert observations[-1] is True


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (OSError("address in use"), 1, "could not start Studio: address in use"),
        (SystemExit(7), 7, ""),
        (KeyboardInterrupt(), 130, ""),
    ],
)
def test_studio_failed_start_cancels_browser_worker(
    launched, mocker, error, code, message
):
    import uvicorn

    workers = []
    original = threading.Thread

    def thread(*args, **kwargs):
        worker = original(*args, **kwargs)
        workers.append(worker)
        return worker

    mocker.patch.object(studio_mod.threading, "Thread", side_effect=thread)
    mocker.patch.object(uvicorn.Server, "run", side_effect=error)
    browser = mocker.patch.object(studio_mod.webbrowser, "open")

    result = runner.invoke(app, ["--review"])

    assert result.exit_code == code
    assert message in result.stderr
    assert isinstance(result.exception, SystemExit)
    assert len(workers) == 1
    assert workers[0].daemon
    assert not workers[0].is_alive()
    browser.assert_not_called()


def test_studio_browser_failure_does_not_stop_server(launched, mocker):
    import uvicorn

    attempted = threading.Event()

    def fail(url):
        attempted.set()
        raise OSError("headless")

    def run(server):
        server.started = True
        assert attempted.wait(2)

    mocker.patch.object(studio_mod.webbrowser, "open", side_effect=fail)
    mocker.patch.object(uvicorn.Server, "run", autospec=True, side_effect=run)

    result = runner.invoke(app, ["--review"])

    assert result.exit_code == 0
    assert "Could not open browser: headless" in result.stderr


@settings(
    max_examples=20,
    # This checks path/route identity through filesystem and TestClient threads,
    # whose scheduling is not a property of the generated directory name.
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(name=st.text(alphabet="abc09记录 #%[]", min_size=1, max_size=12))
def test_studio_directory_name_does_not_change_review_route(launched, tmp_path, name):
    launched.clear()
    selected = tmp_path / ("record-" + name + "-end")
    selected.mkdir(exist_ok=True)
    (selected / "summary.json").write_text(
        json.dumps({"outcome": {"sentinel": "SELECTED"}}), encoding="utf-8"
    )

    result = runner.invoke(app, ["--review", "--session", str(selected), "--no-open"])
    with TestClient(launched[0].config.app, base_url=STUDIO_URL) as client:
        page = client.get(_printed_path(result))

    assert result.exit_code == 0
    assert _printed_path(result) == "/review/"
    assert page.status_code == 200
    assert '"sentinel": "SELECTED"' in page.text
