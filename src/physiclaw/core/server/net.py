"""Server networking helpers.

``wait_for_port`` lives here — not in ``warm_start`` — so both the warm-start
thread and the ``physiclaw auto`` setup worker can share it. The module body
is light (stdlib + config only), but note it sits under the ``core.server``
package: importing it runs ``core.server.__init__`` (which builds the app
singletons), so callers outside the server process (e.g. the setup CLI)
import it lazily.
"""

import socket
import time

from physiclaw.common.config import CONFIG

# How long to wait for uvicorn's listening socket to accept connections, in
# seconds. Probe either address family, including IPv6-only loopback binds.
PORT_WAIT_TIMEOUT = CONFIG.warm_start.port_wait_timeout_seconds
PORT_WAIT_CONNECT_TIMEOUT = CONFIG.warm_start.port_wait_connect_timeout_seconds
PORT_WAIT_INTERVAL = CONFIG.warm_start.port_wait_interval_seconds


def wait_for_port(host: str, port: int, timeout: float = PORT_WAIT_TIMEOUT) -> bool:
    """Block until something is accepting TCP connections on (host, port).

    Returns True on first successful connect, False if ``timeout`` elapses.
    All resolved addresses share one connection/retry deadline. DNS lookup
    itself uses the OS resolver and can outlast this deadline; no socket is
    opened after an overdue lookup returns.
    Used to synchronize with uvicorn startup — signalling SIGINT mid-startup
    leaks CancelledError tracebacks through the lifespan machinery.
    """
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    if host == "::":
        probe_host = "::1"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            addresses = socket.getaddrinfo(probe_host, port, type=socket.SOCK_STREAM)
        except OSError:
            addresses = []
        for family, kind, protocol, _, address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                with socket.socket(family, kind, protocol) as connection:
                    connection.settimeout(min(PORT_WAIT_CONNECT_TIMEOUT, remaining))
                    connection.connect(address)
                    return True
            except OSError:
                pass
        time.sleep(max(0, min(PORT_WAIT_INTERVAL, deadline - time.monotonic())))
    return False
