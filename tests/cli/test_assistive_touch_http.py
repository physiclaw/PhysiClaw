"""Exercise actual HTTP traffic against the small stdlib server."""

import http.client
import json
import re
import socket
import threading
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from physiclaw.cli import _assistive_touch_http as service
from physiclaw.port.backends import darwin, linux, windows

PNG = b"\x89PNG\r\n\x1a\nimage"
API = service.API_PATH


@pytest.mark.parametrize("backend", [linux, windows], ids=["linux", "windows"])
def test_port_binding_does_not_depend_on_a_connection_probe(mocker, backend):
    mocker.patch.object(service.platform, "check_tcp_port", backend.check_tcp_port)
    mocker.patch.object(service.socket, "create_connection", side_effect=TimeoutError)
    with service.open_server(
        "127.0.0.1", 0, service.TestSession(), desktop=True
    ) as server:
        assert server.server_port > 0
        with pytest.raises(OSError) as error:
            service.open_server(
                "127.0.0.1", server.server_port, service.TestSession(), desktop=True
            )
        assert not isinstance(error.value, TimeoutError)


def test_wildcard_listener_does_not_hide_an_existing_loopback_server(mocker):
    mocker.patch.object(service.platform, "check_tcp_port", darwin.check_tcp_port)
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]
        with pytest.raises(OSError, match="already in use"):
            service.open_server("0.0.0.0", port, service.TestSession(), desktop=False)


def test_connection_info_uses_local_name_and_phone_port(mocker):
    mocker.patch.object(service.platform, "local_hostname", return_value="Test-Mac")
    probe = mocker.patch.object(
        service.socket, "socket"
    ).return_value.__enter__.return_value
    probe.getsockname.return_value = ("192.168.1.42", 12345)
    assert service.connection_info(18124) == {
        "hostname": "test-mac.local",
        "ip": "192.168.1.42",
        "port": 18124,
    }


@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0", "invalid"])
def test_connection_info_never_advertises_loopback_or_unknown_ip(mocker, address):
    mocker.patch.object(service.platform, "local_hostname", return_value=None)
    probe = mocker.patch.object(
        service.socket, "socket"
    ).return_value.__enter__.return_value
    probe.getsockname.return_value = (address, 12345)
    assert service.connection_info(8049) == {"hostname": None, "ip": None, "port": 8049}


def test_connection_info_keeps_hostname_if_network_is_unavailable(mocker):
    mocker.patch.object(service.platform, "local_hostname", return_value="Test-Mac")
    mocker.patch.object(service.socket, "socket", side_effect=OSError("offline"))
    assert service.connection_info(8049) == {
        "hostname": "test-mac.local",
        "ip": None,
        "port": 8049,
    }


@pytest.fixture
def clients():
    state = service.TestSession()
    with ExitStack() as cleanup:
        clients = []
        for desktop in (True, False):
            server = cleanup.enter_context(
                service.TestHTTPServer(("127.0.0.1", 0), state, desktop=desktop)
            )
            worker = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}
            )
            worker.start()
            cleanup.callback(worker.join)
            cleanup.callback(server.shutdown)

            def request(
                method,
                path,
                body=None,
                headers=None,
                *,
                chunked=False,
                port=server.server_port,
            ):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                try:
                    connection.request(
                        method, path, body, headers or {}, encode_chunked=chunked
                    )
                    response = connection.getresponse()
                    data = response.read()
                    return SimpleNamespace(
                        status=response.status,
                        content=data,
                        text=data.decode(errors="replace"),
                        headers=dict(response.getheaders()),
                        json=lambda: json.loads(data),
                    )
                finally:
                    connection.close()

            clients.append(request)
        yield state, *clients


def test_latest_upload_replaces_previous_image_even_with_identical_pixels(clients):
    state, desktop, phone = clients
    assert desktop("GET", API).json()["screenshot"]["sequence"] == 0
    for sequence in (1, 2):
        response = phone("POST", "/api/bridge/screenshot", PNG)
        assert response.status == 200
        assert response.headers["Content-Type"] == "text/plain; charset=utf-8"
        assert response.text == f"Screenshot uploaded.\nCode: {state.text}"
        latest = desktop("GET", API).json()["screenshot"]
        assert latest == {
            "sequence": sequence,
            "received_at": state.received_at,
        }
        image = desktop("GET", API + f"/screenshot/{sequence}")
        assert image.content == PNG
        assert image.headers["Content-Type"] == "image/png"
        assert image.headers["Cache-Control"] == "no-store"
    assert desktop("GET", API + "/screenshot/1").status == 404


def test_page_and_phone_share_server_generated_code(clients):
    state, desktop, phone = clients
    previous = state.text
    for text_id in range(3):
        snapshot = (
            desktop("GET", API)
            if text_id == 0
            else desktop("POST", API + "/clipboard", "{}")
        ).json()
        code = snapshot["clipboard"]
        assert snapshot["instance_id"] == state.instance_id
        assert code["text_id"] == text_id
        assert code["text"] == state.text
        assert re.fullmatch(r"PhysiClaw-[0-9A-F]{3}-[0-9A-F]{3}", code["text"])
        if text_id:
            assert code["text"] != previous
        previous = code["text"]
        # Reading state (including opening another page) never changes the code.
        assert desktop("GET", API).json()["clipboard"] == code
        response = phone("GET", "/api/bridge/clipboard")
        assert response.status == 200
        assert response.text == code["text"]
        uploaded = phone("POST", "/api/bridge/screenshot", PNG)
        assert uploaded.text == f"Screenshot uploaded.\nCode: {code['text']}"


def test_screenshot_url_from_previous_session_cannot_read_a_new_image(clients):
    state, desktop, phone = clients
    previous_instance = state.instance_id
    assert phone("POST", "/api/bridge/screenshot", PNG).status == 200
    previous_url = API + f"/screenshot/1?instance={previous_instance}"
    assert desktop("GET", previous_url).content == PNG
    with state.lock:
        state.instance_id = "restarted"
        state.sequence = 0
        state.image = b""
    assert phone("POST", "/api/bridge/screenshot", PNG).status == 200
    assert desktop("GET", previous_url).status == 404
    assert desktop("GET", API + "/screenshot/1?instance=restarted").content == PNG


def test_only_phone_clipboard_requests_increment_read_sequence(clients):
    _, desktop, phone = clients
    for sequence in range(3):
        assert desktop("GET", API).json()["clipboard"]["read_sequence"] == sequence
        generated = desktop("POST", API + "/clipboard", "{}").json()["clipboard"]
        assert generated["read_sequence"] == sequence
        assert phone("POST", "/api/bridge/screenshot", PNG).status == 200
        assert desktop("GET", API).json()["clipboard"]["read_sequence"] == sequence
        assert phone("GET", "/api/bridge/clipboard").status == 200
    assert desktop("GET", API).json()["clipboard"]["read_sequence"] == 3


def test_regenerating_code_changes_it_even_on_random_collision(clients, mocker):
    state, desktop, phone = clients
    state.text = "PhysiClaw-ABC-DEF"
    mocker.patch.object(service.secrets, "token_hex", return_value="abcdef")
    code = desktop("POST", API + "/clipboard", "{}").json()["clipboard"]["text"]
    assert code == "PhysiClaw-ABC-DE0"
    assert phone("GET", "/api/bridge/clipboard").text == code


def test_desktop_reports_phone_connection_addresses(clients):
    state, desktop, _ = clients
    state.connection = {
        "hostname": "test-mac.local",
        "ip": "192.168.1.42",
        "port": 18124,
    }
    assert desktop("GET", API).json()["connection"] == state.connection


@pytest.mark.parametrize("body", [None, [], "code", 42, {"text": "ignored"}])
def test_invalid_generation_request_does_not_replace_code(clients, body):
    state, desktop, _ = clients
    state.text = "keep this"
    assert desktop("POST", API + "/clipboard", json.dumps(body)).status == 400
    assert state.text == "keep this"


def test_malformed_and_oversized_bodies_are_rejected(clients):
    _, desktop, phone = clients
    assert desktop("POST", API + "/clipboard", "{").status == 400
    assert (
        desktop(
            "POST",
            API + "/clipboard",
            headers={"Content-Length": str(service.MAX_JSON_BYTES + 1)},
        ).status
        == 413
    )
    assert phone("POST", "/api/bridge/screenshot", b"not an image").status == 400
    assert (
        phone(
            "POST",
            "/api/bridge/screenshot",
            headers={"Content-Length": str(service.MAX_UPLOAD_BYTES + 1)},
        ).status
        == 413
    )


def test_chunked_phone_upload(clients):
    state, _, phone = clients
    assert (
        phone(
            "POST", "/api/bridge/screenshot", iter([PNG[:4], PNG[4:]]), chunked=True
        ).status
        == 200
    )
    assert state.image == PNG
    assert (
        phone(
            "POST",
            "/api/bridge/screenshot",
            b"no",
            {"Transfer-Encoding": "chunked", "Content-Length": "2"},
        ).status
        == 400
    )


def test_extreme_content_length_is_rejected_without_losing_the_response(clients):
    state, _, phone = clients
    response = phone(
        "POST", "/api/bridge/screenshot", headers={"Content-Length": "9" * 5000}
    )
    assert response.status == 413
    assert state.sequence == 0
    assert state.image == b""


def test_phone_page_shares_controls_but_not_desktop_screenshots(clients):
    state, desktop, phone = clients
    page = phone("GET", service.PAGE_PATH)
    assert page.status == 200
    assert 'data-phone-view="true"' in page.text
    assert phone("GET", "/").status == 200
    assert phone("GET", API).json() == desktop("GET", API).json()
    assert phone("GET", API).json()["clipboard"]["read_sequence"] == 0
    previous = state.text
    generated = phone("POST", API + "/clipboard", "{}")
    assert generated.status == 200
    assert generated.json()["clipboard"]["text"] == state.text != previous
    assert desktop("GET", API).json() == generated.json()
    assert phone("POST", "/api/bridge/screenshot", PNG).status == 200
    assert phone("GET", API + "/screenshot/1").status == 404
    for client in (desktop, phone):
        script = client("GET", API + "/qrcode.js")
        assert script.status == 200
        assert script.headers["Content-Type"] == "text/javascript; charset=utf-8"
        assert b"qrcode" in script.content


def test_page_controls_reject_unrelated_origins_on_both_listeners(clients):
    _, desktop, phone = clients
    for path in ("/api/connect-arm", "/mcp", "/setup-hardware"):
        assert phone("GET", path).status == 404
    assert desktop("GET", "/api/bridge/clipboard").status == 404
    for client in (desktop, phone):
        assert client("GET", API, headers={"Host": "evil.example"}).status == 421
        assert (
            client(
                "POST", API + "/clipboard", "{}", {"Origin": "https://evil.example"}
            ).status
            == 403
        )
    page = desktop("GET", service.PAGE_PATH)
    assert page.status == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert "AssistiveTouch Test" in page.text
    assert 'data-phone-view="true"' not in page.text


@pytest.mark.parametrize("host", ["test-mac.local", "192.168.1.42"])
def test_phone_page_accepts_advertised_lan_address_and_origin(clients, host):
    state, _, phone = clients
    port = phone.__kwdefaults__["port"]
    state.connection = {"hostname": "test-mac.local", "ip": "192.168.1.42", "port": port}
    headers = {"Host": f"{host}:{port}", "Origin": f"http://{host}:{port}"}
    assert phone("GET", service.PAGE_PATH, headers=headers).status == 200
    assert phone("GET", API, headers=headers).status == 200
    assert phone("POST", API + "/clipboard", "{}", headers).status == 200


def test_graceful_interrupt_closes_both_loops():
    from physiclaw.cli.assistive_touch import _serve

    state = service.TestSession()
    with service.TestHTTPServer(("127.0.0.1", 0), state, desktop=True) as desktop:
        with service.TestHTTPServer(("127.0.0.1", 0), state, desktop=False) as phone:

            def interrupt():
                raise KeyboardInterrupt

            with pytest.raises(KeyboardInterrupt):
                _serve((desktop, phone), interrupt)
            assert desktop._BaseServer__is_shut_down.is_set()
            assert phone._BaseServer__is_shut_down.is_set()
