"""HTTP server that serves the browser UI and accepts save payloads.

Endpoints:

- ``GET  /``      — ``index.html`` (the UI bundle)
- ``GET  /svg``   — the **original** source SVG bytes (frontend uses
                    this on load and on Clear to reset the canvas)
- ``POST /save``  — ``{shapes, viewBox, preop}`` from the browser.
                    Backend appends a new op to the patch JSON, replays
                    the chain leading to the new op against the source,
                    writes the snapshot ``<stem>_<id>.svg``, and returns
                    the composited SVG as the response body
                    (``Content-Type: image/svg+xml``); op metadata
                    rides along in ``X-Op-*`` headers so the SVG isn't
                    JSON-encoded.

The original source file is never modified."""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hardware.assembly.mark.patch import (
    ORIG_SENTINEL,
    load_patch,
    make_entry,
    new_id,
    snapshot_path,
    validate_preop,
    write_patch,
)
from hardware.assembly.mark.replay import apply_chain, chain_to
from hardware.assembly.mark.validate import validate_shapes
from hardware.assembly.svg_utils import validate_viewbox

# Sibling file so it can be edited with HTML / JS tooling.
INDEX_HTML = (Path(__file__).parent / "index.html").read_bytes()


class Handler(BaseHTTPRequestHandler):
    src_path: Path  # populated by make_server() before the loop runs.

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        sys.stderr.write(f"[mark] {format % args}\n")

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", INDEX_HTML)
            return
        if self.path == "/svg":
            try:
                data = self.src_path.read_bytes()
            except OSError as exc:
                self._send(500, "text/plain; charset=utf-8", str(exc).encode("utf-8"))
                return
            self._send(200, "image/svg+xml", data)
            return
        self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self):  # noqa: N802
        if self.path != "/save":
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            shapes  = validate_shapes(payload.get("shapes", []))
            viewbox = validate_viewbox(payload.get("viewBox"))
            preop   = validate_preop(payload.get("preop", ORIG_SENTINEL))
            if not shapes and viewbox is None:
                raise ValueError("no shapes or viewBox to save")
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": f"{type(exc).__name__}: {exc}"})
            return

        # Validate preop's existence (preop has already passed the
        # 'orig | 4 letters' format check); a dangling reference would
        # corrupt the patch if we wrote it.
        try:
            entries = load_patch(self.src_path)
            existing_ids = {e["id"] for e in entries}
            if preop != ORIG_SENTINEL and preop not in existing_ids:
                raise ValueError(f"preop {preop!r} not found in patch")
        except ValueError as exc:
            self._send_json(400, {"error": f"{type(exc).__name__}: {exc}"})
            return

        try:
            op_id = new_id(self.src_path, taken=existing_ids)
            entries.append(make_entry(op_id, preop, shapes, viewbox))
            # Replay first; only persist the patch + snapshot once the
            # chain has successfully produced bytes, so a build failure
            # can't corrupt the patch file with a dangling entry.
            chain = chain_to(entries, op_id)
            new_bytes = apply_chain(self.src_path.read_bytes(), chain)
            out_path = snapshot_path(self.src_path, op_id)
            out_path.write_bytes(new_bytes)
            patch_file = write_patch(self.src_path, entries)
        except Exception as exc:  # I/O / build failure — surface to the browser
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return

        self._send(
            200, "image/svg+xml", new_bytes,
            extra_headers={
                "X-Op-Id":    op_id,
                "X-Op-Preop": preop,
                "X-Op-Path":  str(out_path),
                "X-Op-Patch": str(patch_file),
            },
        )

    def _send_json(self, code: int, payload: dict):
        self._send(code, "application/json", json.dumps(payload).encode("utf-8"))

    def _send(self, code: int, ctype: str, body: bytes,
              extra_headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


DEFAULT_PORT = 52281


def make_server(src: Path, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    Handler.src_path = src
    return ThreadingHTTPServer((host, port), Handler)
