"""Tests for `physiclaw.studio.server` — the HTTP marshalling layer,
handlers called directly (the `hardware_setup` test convention)."""

from __future__ import annotations

import asyncio
import json
from html import escape
from types import SimpleNamespace

import httpx
import pytest
from symlink_helpers import symlink_to

from physiclaw.studio import server


class FakeSession:
    """A StudioSession double: canned act() results or exceptions, and
    a `drive` that lends a marker client."""

    def __init__(self, result=None, error=None, busy=False):
        self.busy = busy
        self._result = result or {"text": "ok", "image": None, "rows": []}
        self._error = error
        self.acted: list[tuple[str, dict]] = []
        self.client = object()

    def state(self):
        return {"connected": True, "mcp_url": "u", "tools": []}

    async def act(self, tool, args):
        if self._error is not None:
            raise self._error
        self.acted.append((tool, args))
        return self._result

    async def drive(self, job):
        if self._error is not None:
            raise self._error
        return await job(self.client)


def _request(body: dict | None = None) -> SimpleNamespace:
    async def read_body():
        return json.dumps(body).encode() if body is not None else b"not json"

    return SimpleNamespace(body=read_body)


def _payload(resp) -> dict:
    return json.loads(bytes(resp.body))


@pytest.mark.asyncio
async def test_page_serves_the_studio_app_no_store() -> None:
    resp = await server.handle_page(SimpleNamespace())

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = bytes(resp.body)
    assert b"PhysiClaw Studio" in body
    assert b"/api/act" in body  # drives the same API throughout


@pytest.mark.asyncio
async def test_state_reports_without_dialing() -> None:
    resp = await server.handle_state(SimpleNamespace(), FakeSession())

    payload = _payload(resp)
    assert payload["status"] == "ok"
    assert payload["connected"] is True and payload["mcp_url"] == "u"
    # The stroke ladder the page maps a drag onto — served, never re-spelled.
    assert list(payload["swipe"]["sizes"]) == ["s", "m", "l", "xl", "xxl"]
    assert payload["swipe"]["speeds"] == ["slow", "medium", "fast"]


@pytest.mark.asyncio
async def test_act_calls_the_tool_with_verbatim_args() -> None:
    session = FakeSession()

    resp = await server.handle_act(
        _request({"tool": "tap", "args": {"bbox": [0.1, 0.2, 0.3, 0.4]}}), session
    )

    assert resp.status_code == 200
    assert _payload(resp)["status"] == "ok"
    assert session.acted == [("tap", {"bbox": [0.1, 0.2, 0.3, 0.4]})]


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (None, "must be JSON"),
        ({"args": {}}, "`tool` is a string"),  # tool missing
        ({"tool": 3}, "`tool` is a string"),
        ({"tool": "tap", "args": [1]}, "`args` a mapping"),
    ],
)
@pytest.mark.asyncio
async def test_act_rejects_malformed_bodies(body, match) -> None:
    resp = await server.handle_act(_request(body), FakeSession())

    assert resp.status_code == 400
    assert match in _payload(resp)["message"]


@pytest.mark.parametrize("args", [False, 0, "", [], [1], "text"])
async def test_regression_non_mapping_args_never_execute_a_tool(args):
    session = FakeSession()

    response = await server.handle_act(_request({"tool": "tap", "args": args}), session)

    assert response.status_code == 400
    assert session.acted == []


@pytest.mark.parametrize("args", [None, {}])
async def test_act_null_or_empty_args_remain_valid(args):
    session = FakeSession()

    response = await server.handle_act(
        _request({"tool": "peek", "args": args}), session
    )

    assert response.status_code == 200
    assert session.acted == [("peek", {})]


@pytest.mark.parametrize(
    ("error", "code", "match"),
    [
        (
            ValueError("tool 'calibrate' is not on the published MCP surface"),
            400,
            "published",
        ),
        (ConnectionError("cannot reach the MCP server"), 502, "physiclaw mcp"),
        (RuntimeError("tool 'tap' failed: not calibrated"), 500, "not calibrated"),
    ],
)
@pytest.mark.asyncio
async def test_act_maps_session_errors_to_banner_codes(error, code, match) -> None:
    resp = await server.handle_act(_request({"tool": "tap"}), FakeSession(error=error))

    assert resp.status_code == code
    assert match in _payload(resp)["message"]


@pytest.mark.asyncio
async def test_busy_session_answers_409_one_rig() -> None:
    session = FakeSession(busy=True)

    resp = await server.handle_act(_request({"tool": "tap"}), session)

    assert resp.status_code == 409
    assert "one rig" in _payload(resp)["message"]
    assert session.acted == []


@pytest.mark.asyncio
async def test_unrecognized_exceptions_are_bugs_not_refusals() -> None:
    with pytest.raises(TypeError):
        await server.handle_act(
            _request({"tool": "tap"}), FakeSession(error=TypeError("boom"))
        )


@pytest.mark.asyncio
async def test_build_app_routes_and_closes_the_session_on_shutdown() -> None:
    class Closeable(FakeSession):
        def __init__(self):
            super().__init__()
            self.closed = False

        async def close(self):
            self.closed = True

    session = Closeable()
    app = server.build_app(session)

    assert {r.path for r in app.routes} == {
        "/",
        "/sessions/",
        "/sessions/{sid}/",
        "/sessions/{sid}/images/{name}",
        "/api/state",
        "/api/act",
        "/api/playbooks",
        "/api/step",
        "/api/step/reset",
        "/api/macros",
        "/api/macro",
    }
    async with app.router.lifespan_context(app):
        assert session.closed is False
    assert session.closed is True


async def test_selected_review_does_not_expose_siblings_or_external_frame_links(
    tmp_path,
):
    selected = tmp_path / "selected"
    (selected / "images").mkdir(parents=True)
    (selected / "summary.json").write_text('{"outcome":{"sentinel":"CHOSEN"}}')
    secret = tmp_path / "private.jpg"
    secret.write_bytes(b"private frame")
    symlink_to(selected / "images" / "linked.jpg", secret)
    app = server.build_app(FakeSession(), review_dir=selected)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://studio"
    ) as client:
        page = await client.get("/review/")
        linked = await client.get("/review/images/linked.jpg")
        sibling = await client.get("/review/private.jpg")
        other = await client.get("/review/another/")

    assert page.status_code == 200
    assert '"sentinel": "CHOSEN"' in page.text
    assert linked.status_code == sibling.status_code == other.status_code == 404
    assert b"private frame" not in linked.content
    assert page.headers["cache-control"] == "no-store"


async def test_selected_review_routes_belong_to_one_app_only(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "summary.json").write_text('{"outcome":{"sentinel":"FIRST"}}')
    (second / "summary.json").write_text('{"outcome":{"sentinel":"SECOND"}}')
    first_app = server.build_app(FakeSession(), review_dir=first)
    second_app = server.build_app(FakeSession(), review_dir=second)
    ordinary_app = server.build_app(FakeSession())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first_app), base_url="http://studio"
    ) as client:
        first_page = await client.get("/review/")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=second_app), base_url="http://studio"
    ) as client:
        second_page = await client.get("/review/")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ordinary_app), base_url="http://studio"
    ) as client:
        absent = await client.get("/review/")

    assert '"sentinel": "FIRST"' in first_page.text
    assert '"sentinel": "SECOND"' in second_page.text
    assert absent.status_code == 404


async def test_regression_lifespan_failure_still_closes_session(mocker):
    session = FakeSession()
    session.close = mocker.AsyncMock()
    app = server.build_app(session)

    with pytest.raises(RuntimeError, match="server stopped"):
        async with app.router.lifespan_context(app):
            raise RuntimeError("server stopped")

    session.close.assert_awaited_once_with()


async def test_regression_shutdown_joins_active_job_before_closing_session(mocker):
    started = asyncio.Event()
    cleaned = asyncio.Event()
    cleanup_observed_on_close = []

    async def step(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    async def close():
        cleanup_observed_on_close.append(cleaned.is_set())

    mocker.patch.object(server.stepping, "step", side_effect=step)
    session = FakeSession()
    session.close = close
    app = server.build_app(session)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://studio"
        ) as client:
            launched = await client.post("/api/step", json={"ref": "demo/flow"})
            await asyncio.wait_for(started.wait(), 2)

    assert launched.status_code == 200
    assert cleanup_observed_on_close == [True]


# ---------- the step job: the stepping driver behind /api/step ----------


class _Result:
    def __init__(self, outcome="paused"):
        self.outcome = outcome

    def to_dict(self):
        return {"outcome": self.outcome, "message": "m", "position": None}


def _fake_step(record: dict, *, blocks=None, error=None, exchange=None):
    async def step(app, name, client, *, emit, emit_warn, observe, on_exchange, **opts):
        record.update(app=app, name=name, client=client, opts=opts)
        emit("node open (1/2)")
        emit_warn("thin anchors")
        if blocks is not None:
            observe(SimpleNamespace(name="peek"), blocks)
        if exchange is not None:
            on_exchange(exchange)
        if error is not None:
            raise error
        return _Result()

    return step


VIEW = [
    {"type": "image", "mime_type": "image/jpeg", "data": "aGk="},
    {
        "type": "text",
        "text": 'id [kind] "label" [l,t,r,b] conf\n0 [text] "hi" [0.1,0.1,0.2,0.2] 0.9',
    },
]


@pytest.mark.asyncio
async def test_step_starts_the_driver_on_the_lent_client_and_reports(
    monkeypatch,
) -> None:
    record: dict = {}
    exchange = {"call": "agent_fields", "node": "parse", "request": [], "reply": {}}
    monkeypatch.setattr(
        server.stepping, "step", _fake_step(record, blocks=VIEW, exchange=exchange)
    )
    session = FakeSession()
    job = server.Job()

    resp = await server.handle_step(
        _request(
            {
                "ref": "demo/flow",
                "inputs": {"keyword": "milk"},
                "at": "open",
                "to": "",
                "stop_after": "paste",
            }
        ),
        session,
        job,
    )
    assert resp.status_code == 200 and _payload(resp)["ref"] == "demo/flow"
    await job.task

    assert record["app"] == "demo" and record["name"] == "flow"
    assert record["client"] is session.client  # the session's own connection
    assert record["opts"] == {
        "values": {"keyword": "milk"},
        "outputs": {},
        "verbose": False,
        "at": "open",
        "stop_after": "paste",
    }
    snap = _payload(
        await server.handle_step_state(SimpleNamespace(query_params={}), job)
    )
    assert snap["running"] is False and snap["result"]["outcome"] == "paused"
    assert snap["lines"] == ["node open (1/2)", "! thin anchors"]
    assert snap["exchanges"] == [exchange]  # what the model saw and said
    assert snap["seq"] == 1 and snap["view"]["tool"] == "peek"
    assert [r["label"] for r in snap["view"]["rows"]] == ["hi"]


@pytest.mark.asyncio
async def test_step_snapshot_sends_the_view_only_past_since(monkeypatch) -> None:
    monkeypatch.setattr(server.stepping, "step", _fake_step({}, blocks=VIEW))
    job = server.Job()
    await server.handle_step(_request({"ref": "demo/flow"}), FakeSession(), job)
    await job.task

    fresh = _payload(
        await server.handle_step_state(
            SimpleNamespace(query_params={"since": "1", "lines": "1"}), job
        )
    )
    assert "view" not in fresh and fresh["seq"] == 1
    assert fresh["lines"] == ["! thin anchors"]  # only past what the page holds
    bad = await server.handle_step_state(
        SimpleNamespace(query_params={"since": "x"}), job
    )
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_step_errors_land_in_the_snapshot_not_the_response(monkeypatch) -> None:
    from physiclaw.conductor.spec.model import PlaybookError

    monkeypatch.setattr(
        server.stepping, "step", _fake_step({}, error=PlaybookError("no node 'nope'"))
    )
    job = server.Job()
    await server.handle_step(_request({"ref": "demo/flow"}), FakeSession(), job)
    await job.task

    snap = job.snapshot(0)
    assert snap["result"] is None and "no node 'nope'" in snap["error"]


@pytest.mark.asyncio
async def test_step_unreachable_server_carries_the_start_hint(monkeypatch) -> None:
    monkeypatch.setattr(server.stepping, "step", _fake_step({}))
    job = server.Job()
    await server.handle_step(
        _request({"ref": "demo/flow"}), FakeSession(error=ConnectionError("down")), job
    )
    await job.task

    assert "physiclaw mcp" in job.snapshot(0)["error"]


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ({"ref": "demo"}, "<app>/<playbook>"),
        ({"ref": 7}, "<app>/<playbook>"),
        ({"ref": "demo/flow", "inputs": {"k": 1}}, "mapping of strings"),
        ({"ref": "demo/flow", "at": 3}, "`at` is a string"),
    ],
)
@pytest.mark.asyncio
async def test_step_rejects_malformed_requests(body, match) -> None:
    resp = await server.handle_step(_request(body), FakeSession(), server.Job())

    assert resp.status_code == 400 and match in _payload(resp)["message"]


@pytest.mark.asyncio
async def test_step_is_one_rig_too(monkeypatch) -> None:
    import asyncio

    gate = asyncio.Event()

    async def slow(app, name, client, **kw):
        await gate.wait()
        return _Result()

    monkeypatch.setattr(server.stepping, "step", slow)
    job = server.Job()
    await server.handle_step(_request({"ref": "demo/flow"}), FakeSession(), job)

    again = await server.handle_step(_request({"ref": "demo/flow"}), FakeSession(), job)
    reset = await server.handle_step_reset(_request({"ref": "demo/flow"}), job)
    busy = await server.handle_act(_request({"tool": "tap"}), FakeSession(busy=True))
    gate.set()
    await job.task

    assert again.status_code == 409 and "step" in _payload(again)["message"]
    assert reset.status_code == 409
    assert busy.status_code == 409


@pytest.mark.asyncio
async def test_reset_forgets_the_position(monkeypatch) -> None:
    monkeypatch.setattr(
        server.stepping, "reset", lambda app, name: f"{app}/{name}: cleared"
    )

    resp = await server.handle_step_reset(_request({"ref": "demo/flow"}), server.Job())

    assert _payload(resp) == {"status": "ok", "message": "demo/flow: cleared"}


@pytest.mark.asyncio
async def test_playbooks_lists_the_catalog(monkeypatch) -> None:
    monkeypatch.setattr(
        server.stepping, "catalog", lambda: [{"app": "demo", "playbooks": []}]
    )

    resp = await server.handle_playbooks(SimpleNamespace())

    assert _payload(resp) == {
        "status": "ok",
        "packs": [{"app": "demo", "playbooks": []}],
    }


# ---------- the macro job: a gesture range behind /api/macro ----------


@pytest.mark.asyncio
async def test_macro_runs_the_range_on_the_lent_client(monkeypatch) -> None:
    record: dict = {}
    spec = SimpleNamespace(name="open-app")
    monkeypatch.setattr(server.stepping, "find_macro", lambda name: spec)

    async def run_macro(spec_, values, client, *, emit, observe, caller, **span):
        record.update(spec=spec_, values=values, client=client, caller=caller, **span)
        emit("macro open-app: steps 2-2 completed")
        observe(SimpleNamespace(name="run_macro"), VIEW)
        return {"ok": True, "message": "done", "run_id": "r1"}

    monkeypatch.setattr(server.stepping, "run_macro", run_macro)
    session = FakeSession()
    job = server.Job()

    resp = await server.handle_macro(
        _request(
            {
                "name": "demo/open-app",
                "inputs": {"message": "hi"},
                "start_at": "clip",
                "stop_after": "clip",
            }
        ),
        session,
        job,
    )
    assert resp.status_code == 200 and _payload(resp)["ref"] == "demo/open-app"
    await job.task

    assert record["spec"] is spec and record["client"] is session.client
    assert record["values"] == {"message": "hi"} and record["caller"] == "studio"
    assert record["record_as"] == "demo/open-app"
    assert record["start_at"] == "clip" and record["stop_after"] == "clip"
    snap = job.snapshot(0)
    assert snap["kind"] == "macro" and snap["result"]["ok"] is True
    assert snap["lines"] == ["macro open-app: steps 2-2 completed"]
    assert snap["view"]["tool"] == "run_macro"


@pytest.mark.asyncio
async def test_macro_unknown_name_is_refused_before_any_job(monkeypatch) -> None:
    from physiclaw.macros.model import MacroError

    def missing(name):
        raise MacroError(f"no macro named {name!r}")

    monkeypatch.setattr(server.stepping, "find_macro", missing)
    job = server.Job()

    resp = await server.handle_macro(_request({"name": "nope"}), FakeSession(), job)

    assert (
        resp.status_code == 400 and "no macro named 'nope'" in _payload(resp)["message"]
    )
    assert job.task is None


@pytest.mark.asyncio
async def test_macros_lists_the_catalog(monkeypatch) -> None:
    monkeypatch.setattr(server.stepping, "macro_catalog", lambda: [{"name": "mine"}])

    resp = await server.handle_macros(SimpleNamespace())

    assert _payload(resp) == {"status": "ok", "macros": [{"name": "mine"}]}


# ---------- /sessions — the session viewer ----------


def _recorded(sid: str = "20260908-100000-abc123"):
    from physiclaw.common import paths

    d = paths.sessions_dir() / sid
    (d / "images").mkdir(parents=True)
    (d / "images" / "100001_000_t0.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (d / "events.jsonl").write_text(
        '{"t": "2026-09-08T10:00:01.500", "event": "tool_result", "turn": 0, '
        '"name": "peek", "text": "Screen", "images": ["images/100001_000_t0.jpg"]}\n'
    )
    (d / "summary.json").write_text(json.dumps({"outcome": {"sentinel": "DONE"}}))
    return d


def _path_request(**params) -> SimpleNamespace:
    return SimpleNamespace(path_params=params)


@pytest.mark.asyncio
async def test_sessions_root_redirects_to_the_newest(physiclaw_home) -> None:
    _recorded("20260908-090000-aaaaaa")
    _recorded("20260908-100000-bbbbbb")

    resp = server.handle_sessions(SimpleNamespace())

    assert resp.status_code == 307
    assert resp.headers["location"] == "/sessions/20260908-100000-bbbbbb/"


@pytest.mark.asyncio
async def test_sessions_root_without_recordings_is_404(physiclaw_home) -> None:
    resp = server.handle_sessions(SimpleNamespace())

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_session_page_sends_a_suffix_to_its_canonical_address(
    physiclaw_home,
) -> None:
    _recorded()

    resp = server.handle_session_page(_path_request(sid="abc123"))

    assert resp.status_code == 307
    assert resp.headers["location"] == "/sessions/20260908-100000-abc123/"


@pytest.mark.asyncio
async def test_session_page_serves_the_viewer_no_store(physiclaw_home) -> None:
    _recorded()

    resp = server.handle_session_page(_path_request(sid="20260908-100000-abc123"))

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = bytes(resp.body).decode()
    assert '"sid": "20260908-100000-abc123"' in body
    assert "images/100001_000_t0.jpg" in body  # the frame, by its relative path
    assert '"sessions": [{' in body  # the picker rides the served page


@pytest.mark.asyncio
async def test_session_page_unknown_or_ambiguous_sid_is_404(physiclaw_home) -> None:
    resp = server.handle_session_page(_path_request(sid="nope"))

    assert resp.status_code == 404
    assert b"no session matches" in bytes(resp.body)

    _recorded("20260908-090000-abc123")
    _recorded("20260908-100000-abc123")
    resp = server.handle_session_page(_path_request(sid="abc123"))

    assert resp.status_code == 404
    assert b"ambiguous session" in bytes(resp.body)


@pytest.mark.parametrize("sid", ["<img src=x onerror=alert(1)>", "\"<&'>"])
def test_regression_missing_session_ref_is_html_text(physiclaw_home, sid):
    response = server.handle_session_page(_path_request(sid=sid))

    body = bytes(response.body).decode()
    assert response.status_code == 404
    assert sid not in body
    assert escape(repr(sid)) in body


def test_regression_session_image_cannot_follow_external_symlink(
    physiclaw_home, tmp_path
):
    recorded = _recorded()
    external = tmp_path / "private.jpg"
    external.write_bytes(b"private bytes")
    symlink_to(recorded / "images" / "linked.jpg", external)

    response = server.handle_session_image(
        _path_request(sid="abc123", name="linked.jpg")
    )

    assert response.status_code == 404


def test_regression_session_page_tolerates_non_object_summary(physiclaw_home):
    recorded = _recorded()
    (recorded / "summary.json").write_text('["broken"]', encoding="utf-8")

    response = server.handle_session_page(_path_request(sid=recorded.name))

    assert response.status_code == 200
    assert b'"summary": null' in bytes(response.body)


def test_regression_session_page_rejects_external_directory_alias(
    physiclaw_home, tmp_path
):
    recorded = _recorded()
    external = tmp_path / "private"
    external.mkdir()
    (external / "notes.md").write_text("private notes", encoding="utf-8")
    symlink_to(recorded.parent / "linked", external, target_is_directory=True)

    response = server.handle_session_page(_path_request(sid="linked"))

    assert response.status_code == 404
    assert b"private notes" not in bytes(response.body)


@pytest.mark.parametrize("name", ["page.html", "frame.svg", "unknown.bin"])
def test_regression_recorded_non_raster_files_are_not_served_as_active_content(
    physiclaw_home, name
):
    recorded = _recorded()
    (recorded / "images" / name).write_text(
        "<script>alert(1)</script>", encoding="utf-8"
    )

    response = server.handle_session_image(_path_request(sid="abc123", name=name))

    assert response.media_type == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_regression_cancelled_job_reports_why_it_stopped():
    job = server.Job()
    started = asyncio.Event()

    async def driver(*args):
        started.set()
        await asyncio.Event().wait()

    job.start(FakeSession(), server.KIND_STEP, "demo/flow", driver)
    await started.wait()

    job.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job.task

    assert job.snapshot(0)["error"] == "step cancelled"
    assert job.running is False


@pytest.mark.asyncio
async def test_session_image_serves_a_frame_and_nothing_outside_images(
    physiclaw_home,
) -> None:
    _recorded()

    ok = server.handle_session_image(
        _path_request(sid="abc123", name="100001_000_t0.jpg")
    )
    assert ok.status_code == 200
    assert ok.media_type == "image/jpeg"

    for name in ("missing.jpg", "../summary.json", "..", ""):
        resp = server.handle_session_image(_path_request(sid="abc123", name=name))
        assert resp.status_code == 404, name
    assert (
        server.handle_session_image(_path_request(sid="nope", name="x.jpg"))
    ).status_code == 404
