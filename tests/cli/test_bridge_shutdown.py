"""Exercise the configured bridge loop and real Uvicorn shutdown without hardware."""

import asyncio
import importlib
import os
import socket
import struct
import threading
import time
from unittest.mock import patch

import pytest
import uvicorn

from tests.timing_helpers import WAIT_TIMEOUT, ManualTimeouts, wait_for

server_mod = importlib.import_module("physiclaw.cli.server")


@pytest.fixture
def bridge_server(mocker):
    servers = []
    entered = threading.Event()

    def run(server, *, sockets):
        servers.append(server)
        server.started = True
        if server.config.host == "0.0.0.0":
            entered.set()
            deadline = time.monotonic() + WAIT_TIMEOUT
            while not server.should_exit:
                assert time.monotonic() < deadline, "bridge was never stopped"
                time.sleep(0.001)
        else:
            assert entered.wait(WAIT_TIMEOUT), "bridge was never started"

    with patch.object(uvicorn.Server, "run", run):
        server_mod._serve(
            "127.0.0.1",
            8048,
            mocker.Mock(),
            mocker.Mock(),
            sockets=(mocker.Mock(), mocker.Mock()),
            announce_ready=False,
            on_started=lambda: None,
        )
    bridge = next(s for s in servers if s.config.host == "0.0.0.0")
    control = next(s for s in servers if s is not bridge)
    assert bridge.config.get_loop_factory() is asyncio.SelectorEventLoop
    assert control.config.loop == "auto"
    assert bridge.config.timeout_graceful_shutdown == server_mod.BRIDGE_DRAIN_SECONDS
    assert server_mod.BRIDGE_DRAIN_SECONDS < server_mod.BRIDGE_SHUTDOWN_SECONDS
    assert server_mod.BRIDGE_SHUTDOWN_SECONDS < server_mod.SERVER_JOIN_SECONDS
    return bridge


def test_bridge_loop_detaches_reset_connection_before_wait_closed(bridge_server):
    async def reset_connection():
        loop = asyncio.get_running_loop()
        accepted, lost = loop.create_future(), loop.create_future()
        errors = []
        loop.set_exception_handler(lambda loop, context: errors.append(context))

        class Peer(asyncio.Protocol):
            def connection_made(self, transport):
                accepted.set_result(transport)

            def connection_lost(self, exc):
                lost.set_result(exc)

        listener = await loop.create_server(Peer, "127.0.0.1", 0)
        client = socket.socket()
        client.setblocking(False)
        try:
            await loop.sock_connect(client, listener.sockets[0].getsockname())
            transport = await wait_for(accepted)
            # A zero linger sends TCP RST, reproducing the phone disconnect.
            linger_format = "hh" if os.name == "nt" else "ii"
            client.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack(linger_format, 1, 0)
            )
            client.close()
            await wait_for(lost)
            listener.close()
            await wait_for(listener.wait_closed())
            assert transport.is_closing()
            assert errors == []
        finally:
            client.close()
            if accepted.done():
                accepted.result().close()
            listener.close()

    with asyncio.Runner(loop_factory=bridge_server.config.get_loop_factory()) as runner:
        runner.run(reset_connection())


@pytest.mark.slow
@pytest.mark.parametrize("stuck", ["listener", "lifespan"])
async def test_bridge_shutdown_bounds_drain_and_lifespan(
    bridge_server,
    stuck,
    mocker,
    caplog,
):
    cancelled = asyncio.Event()
    timeouts = ManualTimeouts()
    mocker.patch.object(server_mod, "asyncio", timeouts)
    mocker.patch.object(uvicorn.server, "asyncio", timeouts)

    async def hang():
        try:
            # The drain and outer shutdown budgets exercise different paths.
            # Start cancellation at the intended await, not after a tiny sleep
            # that can elapse before a loaded runner enters this coroutine.
            timeouts.expire(1 if stuck == "listener" else 0)
            await asyncio.Future()
        finally:
            cancelled.set()

    listener = mocker.Mock()
    listener.wait_closed = mocker.AsyncMock(
        side_effect=hang if stuck == "listener" else None
    )
    bridge_server.servers = [listener]
    bridge_server.lifespan = mocker.Mock()
    bridge_server.lifespan.shutdown = mocker.AsyncMock(
        side_effect=hang if stuck == "lifespan" else None
    )
    await wait_for(bridge_server.shutdown())

    assert timeouts.delays == [
        server_mod.BRIDGE_SHUTDOWN_SECONDS,
        server_mod.BRIDGE_DRAIN_SECONDS,
    ]
    assert cancelled.is_set()
    listener.close.assert_called_once()
    bridge_server.lifespan.shutdown.assert_awaited_once()
    if stuck == "lifespan":
        assert bridge_server.force_exit
        assert "Phone bridge shutdown exceeded" in caplog.text
    else:
        assert "timeout graceful shutdown exceeded" in caplog.text


@pytest.mark.slow
@pytest.mark.parametrize(
    "error", [TimeoutError("application timeout"), asyncio.CancelledError()]
)
async def test_bridge_shutdown_preserves_application_errors(
    bridge_server, error, mocker, caplog
):
    bridge_server.servers = []
    bridge_server.lifespan = mocker.Mock()
    bridge_server.lifespan.shutdown = mocker.AsyncMock(side_effect=error)

    with pytest.raises(type(error)) as caught:
        await bridge_server.shutdown()

    assert caught.value is error
    assert not bridge_server.force_exit
    assert "Phone bridge shutdown exceeded" not in caplog.text


@pytest.mark.slow
@pytest.mark.parametrize("stuck", [None, "request", "lifespan"])
def test_bridge_run_exits_after_peer_reset_and_cleans_up(
    bridge_server,
    stuck,
    mocker,
):
    """Use real HTTP, lifespan, transports and asyncio.Runner thread cleanup."""
    request_started = threading.Event()
    lifespan_finished = threading.Event()
    cancelled = threading.Event()
    errors = []

    async def hang():
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            try:
                assert (await receive())["type"] == "lifespan.startup"
                await send({"type": "lifespan.startup.complete"})
                assert (await receive())["type"] == "lifespan.shutdown"
                if stuck == "lifespan":
                    await hang()
                await send({"type": "lifespan.shutdown.complete"})
            finally:
                lifespan_finished.set()
        else:
            request_started.set()
            if stuck == "request":
                await hang()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

    bridge_server.config.app = app
    bridge_server.started = bridge_server.should_exit = False

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()

        def run():
            try:
                bridge_server.run(sockets=[listener])
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            with socket.create_connection(
                listener.getsockname(), timeout=WAIT_TIMEOUT
            ) as peer:
                peer.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                assert request_started.wait(WAIT_TIMEOUT), (
                    "bridge never served the request"
                )
                linger_format = "hh" if os.name == "nt" else "ii"
                peer.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack(linger_format, 1, 0),
                )
        finally:
            bridge_server.should_exit = True
            worker.join(timeout=WAIT_TIMEOUT)

    assert not worker.is_alive(), "bridge thread outlived its shutdown budget"
    assert errors == []
    assert lifespan_finished.is_set()
    assert cancelled.is_set() == (stuck is not None)
    assert bridge_server.force_exit == (stuck == "lifespan")
    assert not bridge_server.server_state.connections
    assert not bridge_server.server_state.tasks
