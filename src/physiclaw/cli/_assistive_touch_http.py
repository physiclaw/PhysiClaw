"""Small, in-memory HTTP server for the standalone AssistiveTouch command.

The HTTP server uses the standard library. The desktop listener serves the page;
the LAN listener accepts the same two URLs used by the iPhone shortcuts.
"""

import json
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from physiclaw import port as platform

PAGE_PATH = "/test-assistive-touch"
API_PATH = "/api/test-assistive-touch"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
PAGE_FILE = Path(__file__).with_name("static") / "test-assistive-touch.html"


def random_code(previous: str = "") -> str:
    digits = secrets.token_hex(3).upper()
    code = f"PhysiClaw-{digits[:3]}-{digits[3:]}"
    if code == previous:
        code = code[:-1] + f"{(int(code[-1], 16) + 1) % 16:X}"
    return code


def connection_info(phone_port: int) -> dict:
    """Addresses for the phone listener, independent of the core server."""
    name = platform.local_hostname()
    hostname = f"{name.lower()}.local" if name else None
    ip = None
    try:
        # UDP connect selects the outbound interface without sending a packet.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(1)
            probe.connect(("8.8.8.8", 80))
            address = IPv4Address(probe.getsockname()[0])
            if not address.is_loopback and not address.is_unspecified:
                ip = str(address)
    except (OSError, ValueError):
        pass
    return {"hostname": hostname, "ip": ip, "port": phone_port}


@dataclass
class TestSession:
    """One image and one clipboard value, discarded when the command exits."""

    instance_id: str = field(default_factory=lambda: secrets.token_hex(16))
    image: bytes = b""
    sequence: int = 0
    received_at: float = 0
    text: str = field(default_factory=random_code)
    text_id: int = 0
    clipboard_reads: int = 0
    connection: dict | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict:
        # The handler holds the lock for each state operation.
        return {
            "instance_id": self.instance_id,
            "connection": self.connection,
            "screenshot": {
                "sequence": self.sequence,
                "received_at": self.received_at,
            },
            "clipboard": {
                "text_id": self.text_id,
                "text": self.text,
                "read_sequence": self.clipboard_reads,
            },
        }


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


class TestHTTPServer(ThreadingHTTPServer):
    # Requests time out independently; shutdown never waits on an idle client.
    daemon_threads = True
    allow_reuse_address = platform.TCP_REUSE_ADDRESS

    def __init__(
        self, address: tuple[str, int], session: TestSession, *, desktop: bool
    ):
        self.session = session
        self.desktop = desktop
        super().__init__(address, TestHandler)


class TestHandler(BaseHTTPRequestHandler):
    server: TestHTTPServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format: str, *args) -> None:
        pass

    def _reply(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, data: dict, status: int = 200) -> None:
        self._reply(status, json.dumps(data).encode(), "application/json")

    def _read_exact(self, size: int) -> bytes:
        data = self.rfile.read(size)
        if len(data) != size:
            raise RequestError(400, "Incomplete request body")
        return data

    def _body(self, limit: int) -> bytes:
        lengths = self.headers.get_all("Content-Length", [])
        transfer = self.headers.get("Transfer-Encoding", "").lower()
        if transfer:
            if transfer != "chunked" or lengths:
                raise RequestError(400, "Invalid request framing")
            data = bytearray()
            while True:
                line = self.rfile.readline(128)
                if not line.endswith(b"\r\n"):
                    raise RequestError(400, "Invalid chunk header")
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    raise RequestError(400, "Invalid chunk size") from None
                if size < 0:
                    raise RequestError(400, "Invalid chunk size")
                if len(data) + size > limit:
                    raise RequestError(413, "Request body too large")
                if size == 0:
                    # HTTP/1.0 closes this connection after the response, so
                    # optional trailers need not be consumed or retained.
                    return bytes(data)
                data.extend(self._read_exact(size))
                if self._read_exact(2) != b"\r\n":
                    raise RequestError(400, "Invalid chunk ending")
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            raise RequestError(411, "Content-Length is required")
        try:
            size = int(lengths[0])
        except ValueError:
            raise RequestError(413, "Request body too large") from None
        if size > limit:
            raise RequestError(413, "Request body too large")
        return self._read_exact(size)

    def _check_desktop_origin(self) -> None:
        host = self.headers.get("Host", "")
        allowed = {
            f"{name}:{self.server.server_port}" for name in ("127.0.0.1", "localhost")
        }
        if host not in allowed:
            raise RequestError(421, "Use the local page URL printed in the terminal")
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{host}":
            raise RequestError(403, "Open the test page on this computer")

    def _dispatch(self) -> None:
        url = urlsplit(self.path)
        path = url.path
        state = self.server.session
        if self.server.desktop:
            self._check_desktop_origin()
            if self.command == "GET" and path in ("/", PAGE_PATH):
                self._reply(200, PAGE_FILE.read_bytes(), "text/html; charset=utf-8")
                return
            if self.command == "GET" and path == API_PATH:
                with state.lock:
                    snapshot = state.snapshot()
                self._json(snapshot)
                return
            if self.command == "POST" and path == API_PATH + "/clipboard":
                try:
                    body = json.loads(self._body(MAX_JSON_BYTES))
                except (ValueError, RecursionError):
                    raise RequestError(400, "Invalid JSON") from None
                if body != {}:
                    raise RequestError(400, "Expected an empty code-generation request")
                with state.lock:
                    state.text = random_code(state.text)
                    state.text_id += 1
                    snapshot = state.snapshot()
                self._json(snapshot)
                return
            if self.command == "GET" and path.startswith(API_PATH + "/screenshot/"):
                query = parse_qs(url.query, keep_blank_values=True)
                with state.lock:
                    data = (
                        state.image
                        if path.rsplit("/", 1)[-1] == str(state.sequence)
                        and query.get("instance", [state.instance_id])
                        == [state.instance_id]
                        else b""
                    )
                if not data:
                    raise RequestError(404, "Image replaced")
                self._reply(
                    200,
                    data,
                    "image/png" if data.startswith(b"\x89PNG") else "image/jpeg",
                )
                return
        else:
            if self.command == "POST" and path == "/api/bridge/screenshot":
                data = self._body(MAX_UPLOAD_BYTES)
                if not data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
                    raise RequestError(400, "Upload a PNG or JPEG image")
                with state.lock:
                    state.image = data
                    state.sequence += 1
                    state.received_at = time.time()
                    text = state.text
                message = f"Screenshot uploaded.\nCode: {text}"
                self._reply(200, message.encode(), "text/plain; charset=utf-8")
                return
            if self.command == "GET" and path == "/api/bridge/clipboard":
                with state.lock:
                    text = state.text
                    state.clipboard_reads += 1
                self._reply(200, text.encode(), "text/plain; charset=utf-8")
                return
        raise RequestError(404, "Not found")

    def do_GET(self) -> None:
        try:
            self._dispatch()
        except RequestError as exc:
            self._json({"error": str(exc)}, exc.status)
        except TimeoutError:
            self._json({"error": "Request timed out"}, 408)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_POST = do_GET


def open_server(
    host: str, port: int, session: TestSession, *, desktop: bool
) -> TestHTTPServer:
    platform.check_tcp_port(port)
    return TestHTTPServer((host, port), session, desktop=desktop)
