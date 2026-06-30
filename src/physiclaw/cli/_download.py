"""Shared HTTP download helpers for the CLI — a UA-pinned fetch and a
chunked reader that draws a progress bar. Used by every CLI download (vision
model, firmware) so the fetch behaviour and UX stay uniform.
"""

import urllib.request

import typer

# Cloudflare's WAF 403s the default Python-urllib User-Agent, so every request
# to the physiclaw.ai mirror must set one. Applied to all CLI downloads for
# uniformity (and so flash.py's firmware fetch keeps working too).
USER_AGENT = "physiclaw"


def http_get(url: str, timeout: int = 120):
    """urlopen with a User-Agent set — the CDN's WAF blocks the default one."""
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": USER_AGENT}),
        timeout=timeout,
    )


def stream(resp, write, label: str) -> None:
    """Read ``resp`` in 64 KiB chunks into ``write``, drawing a progress bar
    sized from Content-Length (quietly streams if the length is unknown)."""
    raw = resp.getheader("Content-Length")
    total = int(raw) if raw and raw.isdigit() else None
    if total is None:
        for chunk in iter(lambda: resp.read(1 << 16), b""):
            write(chunk)
        return
    with typer.progressbar(
        length=total, label=f"{label} ({total / 1048576:.1f} MiB)"
    ) as bar:
        for chunk in iter(lambda: resp.read(1 << 16), b""):
            write(chunk)
            bar.update(len(chunk))
