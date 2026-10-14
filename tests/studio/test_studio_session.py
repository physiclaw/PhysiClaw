"""Tests for `physiclaw.studio.session` — block normalization and the
session's connect/act lifecycle (against a fake McpClient)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from physiclaw.studio import session as session_mod
from physiclaw.studio.session import StudioSession, view_reply

LISTING = (
    'listing (id [kind] "label" [left,top,right,bottom] conf):\n'
    '1 [text] "加入购物车" [0.100,0.900,0.400,0.950] 0.95\n'
    '2 [icon] "" [0.850,0.050,0.950,0.120] 0.80'
)
IMAGE = {"type": "image", "mime_type": "image/jpeg", "data": "aGk="}


# ---------- view_reply ----------


def test_view_reply_peek_shape_image_then_listing() -> None:
    out = view_reply([IMAGE, {"type": "text", "text": LISTING}])

    assert out["text"] == ""
    assert out["image"] == {"mime_type": "image/jpeg", "data": "aGk="}
    assert [r["label"] for r in out["rows"]] == ["加入购物车", ""]
    assert out["rows"][0]["bbox"] == [0.1, 0.9, 0.4, 0.95]


def test_view_reply_gesture_shape_text_before_image_is_action_text() -> None:
    out = view_reply(
        [
            {"type": "text", "text": "tap ok — screen changed"},
            IMAGE,
            {"type": "text", "text": LISTING},
        ]
    )

    assert out["text"] == "tap ok — screen changed"
    assert len(out["rows"]) == 2


def test_view_reply_text_only_fallback_has_no_view() -> None:
    out = view_reply([{"type": "text", "text": "copied to clipboard"}])

    assert out == {
        "text": "copied to clipboard",
        "image": None,
        "rows": [],
        "listing": "",
    }


# ---------- session lifecycle ----------


class FakeClient:
    """Stands in for McpClient: canned tools/replies, records calls."""

    def __init__(self, base_url=None, reply=None, fail=None):
        self.calls: list[tuple[str, dict]] = []
        self._reply = reply or [IMAGE, {"type": "text", "text": LISTING}]
        self._fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def list_tools(self):
        return [{"name": "peek"}, {"name": "tap"}]

    async def call_tool(self, name, args=None):
        if self._fail is not None:
            raise self._fail
        self.calls.append((name, args or {}))
        return self._reply


def _session(monkeypatch, **kwargs) -> StudioSession:
    monkeypatch.setattr(
        session_mod, "McpClient", lambda base: FakeClient(base, **kwargs)
    )
    return StudioSession(base_url="http://127.0.0.1:8048/mcp")


@pytest.mark.asyncio
async def test_act_connects_lazily_and_normalizes(monkeypatch) -> None:
    s = _session(monkeypatch)
    assert s.state()["connected"] is False

    out = await s.act("tap", {"bbox": [0.1, 0.2, 0.3, 0.4]})

    assert s.state()["connected"] is True
    assert s.state()["tools"] == ["peek", "tap"]
    assert len(out["rows"]) == 2
    await s.close()


@pytest.mark.asyncio
async def test_act_refuses_a_tool_off_the_published_surface(monkeypatch) -> None:
    s = _session(monkeypatch)

    with pytest.raises(ValueError, match="published"):
        await s.act("calibrate", {})
    await s.close()


@pytest.mark.asyncio
async def test_tool_error_keeps_the_session(monkeypatch) -> None:
    # The tool ran and said no — a rig-state complaint, not a dead
    # transport. The client must survive for the next action.
    s = _session(monkeypatch, fail=RuntimeError("tool 'tap' failed: not calibrated"))

    with pytest.raises(RuntimeError, match="not calibrated"):
        await s.act("tap", {})

    assert s.state()["connected"] is True
    await s.close()


@pytest.mark.asyncio
async def test_transport_error_drops_the_client_for_reconnect(monkeypatch) -> None:
    s = _session(monkeypatch, fail=OSError("connection reset"))

    with pytest.raises(ConnectionError, match="connection reset"):
        await s.act("tap", {})

    assert s.state()["connected"] is False

    await s.close()


def test_mcp_url_accepts_both_spellings() -> None:
    with_suffix = StudioSession(base_url="http://127.0.0.1:8048/mcp")
    without = StudioSession(base_url="http://127.0.0.1:8048")

    assert with_suffix.mcp_url == "http://127.0.0.1:8048/mcp"
    assert without.mcp_url == "http://127.0.0.1:8048/mcp"
    assert with_suffix.base_url == without.base_url == "http://127.0.0.1:8048"


class OwnedClient(FakeClient):
    """Model the SDK's task ownership, including connection discovery."""

    def __init__(self):
        super().__init__()
        self.owner = None
        self.closed = False
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.allow_close.set()
        self.started = asyncio.Event()
        self.allow_call = asyncio.Event()
        self.allow_call.set()
        self.list_error = None
        self.list_started = asyncio.Event()
        self.allow_list = asyncio.Event()
        self.allow_list.set()
        self.close_error = None
        self.enter_error = None

    def _check_owner(self):
        if asyncio.current_task() is not self.owner:
            raise RuntimeError("MCP used from a different task")

    async def __aenter__(self):
        if self.enter_error is not None:
            raise self.enter_error
        self.owner = asyncio.current_task()
        return self

    async def __aexit__(self, *exc):
        self._check_owner()
        self.close_started.set()
        await self.allow_close.wait()
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

    async def list_tools(self):
        self._check_owner()
        self.list_started.set()
        await self.allow_list.wait()
        if self.list_error is not None:
            raise self.list_error
        return await super().list_tools()

    async def call_tool(self, name, args=None):
        self._check_owner()
        self.started.set()
        await self.allow_call.wait()
        return await super().call_tool(name, args)


@pytest.fixture
async def owned_session(mocker):
    first, second = OwnedClient(), OwnedClient()
    factory = mocker.patch.object(session_mod, "McpClient", side_effect=[first, second])
    session = StudioSession("http://fixture")
    try:
        yield SimpleNamespace(
            session=session, first=first, second=second, factory=factory
        )
    finally:
        first.allow_close.set()
        second.allow_close.set()
        await session.close()


async def test_regression_requests_and_close_share_one_connection_owner(owned_session):
    s = owned_session.session

    first = await asyncio.create_task(s.act("peek", {}))
    second = await asyncio.create_task(s.act("peek", {}))
    await asyncio.create_task(s.close())

    assert first == second
    owned_session.factory.assert_called_once()
    assert owned_session.first.closed is True
    assert s.state()["connected"] is False


async def test_drive_job_runs_in_the_connection_owner(owned_session):
    s = owned_session.session
    await asyncio.create_task(s.act("peek", {}))

    async def job(client):
        return await client.call_tool("tap", {"x": 1})

    result = await asyncio.create_task(s.drive(job))

    assert result == [IMAGE, {"type": "text", "text": LISTING}]
    assert owned_session.first.calls == [("peek", {}), ("tap", {"x": 1})]
    owned_session.factory.assert_called_once()


@pytest.mark.parametrize("error_type", [ConnectionError, OSError, RuntimeError])
async def test_regression_failed_discovery_closes_client_and_allows_retry(
    owned_session, error_type
):
    s = owned_session.session
    owned_session.first.list_error = error_type("listing failed")

    with pytest.raises(ConnectionError, match="listing failed"):
        await s.act("peek", {})
    disconnected = s.state()["connected"]
    result = await s.act("peek", {})

    assert disconnected is False
    assert owned_session.first.closed is True
    assert result["rows"][0]["label"] == "加入购物车"
    assert owned_session.factory.call_count == 2


async def test_regression_cancelled_action_waits_for_cleanup_before_unlock(
    owned_session,
):
    s = owned_session.session
    client = owned_session.first
    client.allow_call.clear()
    client.allow_close.clear()
    action = asyncio.create_task(s.act("tap", {}))
    await client.started.wait()

    action.cancel("stop action")
    await asyncio.wait_for(client.close_started.wait(), 2)
    busy_during_close = s.busy
    client.allow_close.set()
    with pytest.raises(asyncio.CancelledError, match="stop action"):
        await action

    assert busy_during_close is True
    assert client.closed is True
    assert client.calls == []
    assert s.busy is False
    assert s.state()["connected"] is False


async def test_regression_cancelled_discovery_cleans_up_before_next_request(
    owned_session,
):
    s = owned_session.session
    client = owned_session.first
    client.allow_list.clear()
    action = asyncio.create_task(s.act("peek", {}))
    await client.list_started.wait()

    action.cancel()
    with pytest.raises(asyncio.CancelledError):
        await action
    result = await s.act("peek", {})

    assert client.closed is True
    assert client.calls == []
    assert owned_session.factory.call_count == 2
    assert result["rows"]


async def test_close_can_interrupt_an_inflight_driver(owned_session):
    s = owned_session.session
    client = owned_session.first
    client.allow_call.clear()

    async def job(mcp):
        return await mcp.call_tool("tap", {})

    action = asyncio.create_task(s.drive(job))
    await client.started.wait()

    await asyncio.wait_for(s.close(), 2)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(action, 2)

    assert client.closed is True
    assert client.calls == []
    assert s.busy is False


async def test_close_waits_for_cleanup_despite_repeated_cancellation(owned_session):
    s = owned_session.session
    client = owned_session.first
    await s.act("peek", {})
    client.allow_close.clear()
    closing = asyncio.create_task(s.close())
    await client.close_started.wait()

    closing.cancel("stop close")
    await asyncio.sleep(0)
    closing.cancel("stop again")
    await asyncio.sleep(0)
    finished_early = closing.done()
    client.allow_close.set()
    with pytest.raises(asyncio.CancelledError, match="stop again"):
        await closing
    result = await s.act("peek", {})

    assert finished_early is False
    assert client.closed is True
    assert owned_session.factory.call_count == 2
    assert result["rows"]


async def test_new_action_waits_for_connection_teardown(owned_session):
    s = owned_session.session
    client = owned_session.first
    await s.act("peek", {})
    client.allow_close.clear()
    closing = asyncio.create_task(s.close())
    await client.close_started.wait()

    action = asyncio.create_task(s.act("tap", {}))
    await asyncio.sleep(0)
    opened_before_close = owned_session.factory.call_count
    client.allow_close.set()
    await asyncio.gather(closing, action)

    assert opened_before_close == 1
    assert client.closed is True
    assert owned_session.second.calls == [("tap", {})]


async def test_cancelled_action_waiting_for_rig_does_not_run_later(owned_session):
    s = owned_session.session
    client = owned_session.first
    client.allow_call.clear()
    first = asyncio.create_task(s.act("peek", {}))
    await client.started.wait()
    queued = asyncio.create_task(s.act("tap", {}))
    await asyncio.sleep(0)

    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    client.allow_call.set()
    await first

    assert client.calls == [("peek", {})]
    assert s.state()["connected"] is True


async def test_cancelled_driver_cannot_keep_the_worker_alive(owned_session):
    s = owned_session.session
    started = asyncio.Event()

    async def job(client):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return "driver swallowed cancellation"

    action = asyncio.create_task(s.drive(job))
    await started.wait()

    action.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(action, 2)

    assert owned_session.first.closed is True
    assert s.state()["connected"] is False
    assert s.busy is False


async def test_close_finishes_when_driver_swallows_cancellation(owned_session):
    s = owned_session.session
    started = asyncio.Event()

    async def job(client):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return "finished during shutdown"

    action = asyncio.create_task(s.drive(job))
    await started.wait()

    closing = asyncio.create_task(s.close())
    result = await action
    # Bound the observation without cancelling close itself: the regression
    # being checked is precisely a close that ignores caller cancellation.
    finished, pending = await asyncio.wait((closing,), timeout=0.05)
    closing_completed = closing in finished
    owned_session.first.owner.cancel()
    await closing

    assert closing_completed is True
    assert result == "finished during shutdown"
    assert owned_session.first.closed is True


@pytest.mark.parametrize("error_type", [ConnectionError, ValueError, RuntimeError])
async def test_drive_error_reconnects_only_for_transport_failures(
    owned_session, error_type
):
    s = owned_session.session

    async def job(client):
        raise error_type("driver failed")

    with pytest.raises(error_type, match="driver failed"):
        await s.drive(job)
    connected = s.state()["connected"]
    result = await s.act("peek", {})

    assert connected is (error_type is not ConnectionError)
    assert owned_session.first.closed is (error_type is ConnectionError)
    assert result["rows"]


async def test_discovery_cleanup_error_does_not_mask_original_failure(owned_session):
    s = owned_session.session
    owned_session.first.list_error = OSError("listing failed")
    owned_session.first.close_error = RuntimeError("teardown failed")

    with pytest.raises(ConnectionError, match="listing failed"):
        await s.act("peek", {})
    result = await s.act("peek", {})

    assert owned_session.first.closed is True
    assert result["rows"]


async def test_close_failure_does_not_prevent_a_new_connection(owned_session):
    s = owned_session.session
    await s.act("peek", {})
    owned_session.first.close_error = RuntimeError("teardown failed")

    await s.close()
    result = await s.act("peek", {})

    assert owned_session.first.closed is True
    assert owned_session.factory.call_count == 2
    assert result["rows"]


async def test_failed_connection_can_be_retried(owned_session):
    s = owned_session.session
    owned_session.first.enter_error = ConnectionError("server offline")

    with pytest.raises(ConnectionError, match="server offline"):
        await s.act("peek", {})
    result = await s.act("peek", {})

    assert owned_session.factory.call_count == 2
    assert result["rows"]


async def test_close_before_worker_starts_does_not_hang_or_execute(owned_session):
    s = owned_session.session

    action = asyncio.create_task(s.act("tap", {}))
    closing = asyncio.create_task(s.close())
    with pytest.raises(asyncio.CancelledError, match="MCP session closed"):
        await asyncio.wait_for(action, 2)
    await closing
    opened_before_retry = owned_session.factory.call_count
    result = await s.act("peek", {})

    assert opened_before_retry == 0
    assert owned_session.first.calls == [("peek", {})]
    assert result["rows"]


async def test_job_error_racing_with_cancellation_preserves_cancellation(owned_session):
    s = owned_session.session

    async def job(client):
        action.cancel("cancel alongside failure")
        raise ValueError("driver failed simultaneously")

    action = asyncio.create_task(s.drive(job))
    with pytest.raises(asyncio.CancelledError, match="cancel alongside failure"):
        await action

    assert owned_session.first.closed is True


@pytest.mark.parametrize("error_type", [ConnectionError, ValueError])
async def test_job_error_after_cancellation_does_not_retain_connection(
    owned_session, error_type
):
    s = owned_session.session

    async def job(client):
        action.cancel("cancel before failure")
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise error_type("driver failed during cancellation")

    action = asyncio.create_task(s.drive(job))
    with pytest.raises(asyncio.CancelledError, match="cancel before failure"):
        await action

    assert owned_session.first.closed is True
    assert s.state()["connected"] is False
