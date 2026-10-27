"""Tests for hardware/assembly/mark/server.py — the /save handler.

No real socket: a ``Handler`` is built bare (``__new__`` plus the
attributes ``BaseHTTPRequestHandler``'s send path needs). Regression tests
use real patch files and replay; concurrency tests use an in-memory store
so the read→modify→write window is observable.
"""

from __future__ import annotations

import io
import json
import threading
from types import SimpleNamespace

import pytest

from hardware import buildlog
from hardware.assembly.mark import patch, replay, server

SAVE_BODY = json.dumps({"shapes": [], "viewBox": "0 0 10 10", "preop": "orig"}).encode()


def make_handler(body: bytes) -> server.Handler:
    """A Handler wired to in-memory streams instead of a socket."""
    h = server.Handler.__new__(server.Handler)
    h.command = "POST"
    h.path = "/save"
    h.request_version = "HTTP/1.1"
    h.requestline = "POST /save HTTP/1.1"
    h.client_address = ("127.0.0.1", 0)
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.headers = {"Content-Length": str(len(body))}
    return h


@pytest.fixture
def store(tmp_path, monkeypatch):
    """In-memory patch persistence for /save.

    ``load_patch`` / ``write_patch`` / ``upsert_entry`` and the replay
    chain are replaced with deterministic fakes over one shared entry
    list; ``in_load`` is a hook a test can use to dwell inside the
    read→modify→write window."""
    src = tmp_path / "belt_20_clamp_exploded_cam0.svg"
    src.write_bytes(b"<svg/>")
    monkeypatch.setattr(server.Handler, "src_path", src, raising=False)

    state = SimpleNamespace(entries=[], in_load=lambda: None)
    ids = iter(["aaaa", "bbbb", "cccc", "dddd"])
    id_lock = threading.Lock()

    def fake_load(src_path):
        entries = [dict(e) for e in state.entries]
        state.in_load()
        return entries

    def fake_write(src_path, entries):
        state.entries = entries
        return tmp_path / "belt_20_clamp_exploded_cam0.json"

    def fake_upsert(src_path, entries, edit_id, preop, shapes, viewbox):
        with id_lock:
            op_id = next(ids)
        entry = {"id": op_id, "preop": preop, "shapes": shapes, "viewBox": viewbox}
        return op_id, [*entries, entry]

    monkeypatch.setattr(server, "load_patch", fake_load)
    monkeypatch.setattr(server, "write_patch", fake_write)
    monkeypatch.setattr(server, "upsert_entry", fake_upsert)
    monkeypatch.setattr(server, "chain_to", lambda entries, op_id: [])
    monkeypatch.setattr(server, "apply_chain", lambda src_bytes, chain: b"<svg/>")
    monkeypatch.setattr(
        server, "snapshot_path", lambda src_path, op_id: tmp_path / f"snap_{op_id}.svg"
    )
    return state


def test_save_appends_the_op_and_replies_with_its_id(store):
    handler = make_handler(SAVE_BODY)

    handler.do_POST()

    response = handler.wfile.getvalue()
    assert b" 200 " in response.splitlines()[0]
    assert b"X-Op-Id: aaaa" in response
    assert [e["id"] for e in store.entries] == ["aaaa"]


@pytest.mark.parametrize("preop", ["orig", "aaaa"])
def test_browser_save_preserves_parts_through_reopen_and_replay(
    tmp_path, monkeypatch, preop
):
    monkeypatch.setattr(patch, "PATCH_DIR", tmp_path / "patch")
    src = tmp_path / "linear_10_y_exploded_cam0.svg"
    original = b'<svg viewBox="0 0 100 100"><g id="art"/></svg>'
    src.write_bytes(original)
    monkeypatch.setattr(server.Handler, "src_path", src, raising=False)
    parents = (
        [patch.make_entry("aaaa", "orig", [], "0 0 80 80")] if preop != "orig" else []
    )
    entry = patch.make_entry("bbbb", preop, [], None)
    entry["parts"] = ["Screw", "TNut"]
    patch.write_patch(src, [*parents, entry])
    snapshot = patch.snapshot_path(src, "bbbb")

    for x, fill in [(10, "#1e88ff"), (20, "#e53935")]:
        shape = {
            "type": "rect",
            "geom": {"x": x, "y": 10, "w": 5, "h": 5, "rx": 0},
            "color": {"fill": fill, "opacity": 0.35},
            "outlined": False,
        }
        # The browser sends only editable fields, not refit's parts metadata.
        payload = {
            "id": "bbbb",
            "preop": preop,
            "shapes": [shape],
            "viewBox": "0 0 50 50",
        }
        handler = make_handler(json.dumps(payload).encode())
        handler.do_POST()
        response, image = handler.wfile.getvalue().split(b"\r\n\r\n", 1)

        assert b" 200 " in response.splitlines()[0]
        assert b"X-Op-Id: bbbb" in response
        saved = patch.load_patch(src)
        assert saved == [*parents, {**payload, "parts": ["Screw", "TNut"]}]
        assert snapshot.read_bytes() == image
        assert f'x="{x:.4f}"'.encode() in image
        assert f'fill="{fill}"'.encode() in image
        assert replay.replay_one(src) == [snapshot]
        assert snapshot.read_bytes() == image

        reopened = make_handler(b"")
        reopened.command = "GET"
        reopened.path = "/patch"
        reopened.requestline = "GET /patch HTTP/1.1"
        reopened.do_GET()
        response, body = reopened.wfile.getvalue().split(b"\r\n\r\n", 1)
        assert b" 200 " in response.splitlines()[0]
        assert json.loads(body)["edit"] == saved[-1]

    # A new chained op must not inherit the edited op's part selection.
    appended = make_handler(
        json.dumps({"preop": "bbbb", "shapes": [], "viewBox": "0 0 40 40"}).encode()
    )
    appended.do_POST()
    response, image = appended.wfile.getvalue().split(b"\r\n\r\n", 1)
    assert b" 200 " in response.splitlines()[0]
    updated = patch.load_patch(src)
    assert updated[:-1] == saved
    assert updated[-1]["preop"] == "bbbb"
    assert "parts" not in updated[-1]
    (new_snapshot,) = replay.replay_one(src)
    assert new_snapshot.read_bytes() == image
    assert src.read_bytes() == original


def test_save_failure_keeps_response_and_traceback_in_run_log(
    store, tmp_path, monkeypatch
):
    def fail(*args):
        raise OSError("Cannot write annotation patch")

    monkeypatch.setattr(server, "write_patch", fail)
    handler = make_handler(SAVE_BODY)
    with buildlog.session(tmp_path) as log:
        handler.do_POST()
    assert b" 500 " in handler.wfile.getvalue().splitlines()[0]
    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    error = next(event for event in events if event["event"] == "http_error")
    assert error["status_code"] == 500
    assert "Cannot write annotation patch" in error["message"]
    assert "OSError: Cannot write annotation patch" in error["traceback"]


def test_concurrent_saves_do_not_lose_an_op(store):
    # ThreadingHTTPServer runs each /save on its own thread. Two saves that
    # both read the patch before either writes would each upsert their own
    # op and the last write would clobber the other — _SAVE_LOCK must
    # serialize the whole read→modify→write.
    #
    # The barrier releases the two threads together ONLY if both can sit
    # inside load_patch at once (the lost-update interleaving). With the
    # lock held, the second thread can't reach it: the first times out,
    # proceeds alone, and both ops land.
    barrier = threading.Barrier(2)

    def dwell():
        try:
            # Short timeout: with a correct lock the second thread can
            # NEVER arrive, so this wait always times out — it prices
            # the happy path, not the detection (a broken lock releases
            # the barrier instantly).
            barrier.wait(timeout=0.1)
        except threading.BrokenBarrierError:
            pass

    store.in_load = dwell
    errors: list[Exception] = []

    def save():
        try:
            make_handler(SAVE_BODY).do_POST()
        except Exception as exc:  # surface thread failures to the test
            errors.append(exc)

    threads = [threading.Thread(target=save) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sorted(e["id"] for e in store.entries) == ["aaaa", "bbbb"]
