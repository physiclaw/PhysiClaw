#!/usr/bin/env python3
"""mark_assembly_step.py xx.svg — click polygons in a browser, save xx.filled.svg.

Highlights "the part being installed in this step" against "the structure
already assembled" — the standard blue-overlay convention used in
assembly manuals. Click a polygon around the new part on a FreeCAD
TechDraw SVG; the saved sibling carries the blue fill on top of the
original line art.

Polygons are inserted as direct children of the root <svg>, in viewBox
coordinates. The original file is never modified; output is a fresh
sibling whose name auto-increments to avoid clobbering.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable, List, Tuple

FILL_GROUP_ID = "manual-fill"
POLY_FILL = "#1e88ff"
POLY_FILL_OPACITY = "0.35"
POLY_STROKE = "#1e88ff"
POLY_STROKE_OPACITY = "0.35"


def vertex_from_click(x: float, y: float) -> Tuple[float, float]:
    # Identity. Future snapping hook: replace with nearest-edge/vertex
    # search against the SVG geometry. Keep snapping isolated to here.
    return (x, y)


def next_sibling_path(src: Path) -> Path:
    parent, stem = src.parent, src.stem
    candidate = parent / f"{stem}.filled.svg"
    if not candidate.exists():
        return candidate
    i = 1
    while True:
        candidate = parent / f"{stem}.filled.{i}.svg"
        if not candidate.exists():
            return candidate
        i += 1


_SVG_CLOSE_RE = re.compile(r"</\s*svg\s*>", re.IGNORECASE)


def build_fill_svg(original: bytes, polygons: Iterable[Iterable[Tuple[float, float]]]) -> bytes:
    """Append a single <g id='manual-fill'> with each polygon inside, just
    before the LAST </svg>. Everything else byte-identical."""
    text = original.decode("utf-8")
    matches = list(_SVG_CLOSE_RE.finditer(text))
    if not matches:
        raise ValueError("input does not contain </svg>")
    insert_at = matches[-1].start()

    lines: List[str] = [
        "  <!-- manual-fill: appended by hardware/assembly/mark_assembly_step.py -->",
        f'  <g id="{FILL_GROUP_ID}" class="{FILL_GROUP_ID}">',
    ]
    for poly in polygons:
        pts = " ".join(f"{x:.4f},{y:.4f}" for x, y in poly)
        lines.append(
            f'    <polygon class="{FILL_GROUP_ID}" points="{pts}" '
            f'fill="{POLY_FILL}" fill-opacity="{POLY_FILL_OPACITY}" '
            f'stroke="none"/>'
        )
    lines.append("  </g>")
    block = "\n" + "\n".join(lines) + "\n"

    return (text[:insert_at] + block + text[insert_at:]).encode("utf-8")


def _validate_polygons(raw) -> List[List[Tuple[float, float]]]:
    out: List[List[Tuple[float, float]]] = []
    if not isinstance(raw, list):
        raise ValueError("polygons must be a list")
    for poly in raw:
        if not isinstance(poly, list) or len(poly) < 3:
            continue  # plan: unclosed / too-small polygons are ignored
        verts: List[Tuple[float, float]] = []
        for pt in poly:
            if not (isinstance(pt, (list, tuple)) and len(pt) == 2):
                raise ValueError("vertex must be [x, y]")
            verts.append(vertex_from_click(float(pt[0]), float(pt[1])))
        out.append(verts)
    return out


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>mark assembly step</title>
<style>
  html, body { margin: 0; height: 100%; font-family: -apple-system, system-ui, sans-serif; background:#f3f4f6; color:#111; }
  #bar { display:flex; gap:8px; align-items:center; padding:8px 12px; background:#fff; border-bottom:1px solid #e5e7eb; box-shadow:0 1px 0 rgba(0,0,0,.02); }
  #bar button { padding:6px 12px; border:1px solid #d1d5db; background:#fff; border-radius:6px; cursor:pointer; font:inherit; }
  #bar button:hover { background:#f9fafb; }
  #bar button:disabled { opacity:.5; cursor:not-allowed; }
  #bar .finish { background:#1e88ff; color:#fff; border-color:#1e88ff; }
  #bar .finish:hover { background:#1976d2; }
  #status { margin-left:auto; font-size:13px; color:#374151; }
  #status.err { color:#b91c1c; }
  #status.ok  { color:#15803d; }
  #stage { position:absolute; inset:48px 0 0 0; overflow:hidden; }
  #stage svg { display:block; width:100%; height:100%; background:#fff; cursor:crosshair; }
  #stage svg.panning { cursor:grabbing; }
  .mark-line     { stroke:#1e88ff; stroke-width:2; fill:none; vector-effect:non-scaling-stroke; pointer-events:none; }
  .mark-rubber   { stroke:#1e88ff; stroke-dasharray:4 3; stroke-width:1.5; fill:none; vector-effect:non-scaling-stroke; pointer-events:none; opacity:.7; }
  .mark-vertex   { fill:#1e88ff; stroke:#fff; stroke-width:1; vector-effect:non-scaling-stroke; pointer-events:none; }
  .mark-vertex-first { fill:#fff; stroke:#1e88ff; stroke-width:2.5; vector-effect:non-scaling-stroke; pointer-events:none; }
  .mark-fill     { fill:#1e88ff; fill-opacity:.35; stroke:none; pointer-events:none; }
  .hint { padding: 6px 12px; font-size: 12px; color:#6b7280; background:#fff; border-bottom:1px solid #f3f4f6; }
</style>
</head>
<body>
  <div id="bar">
    <button id="clear" title="Remove all closed polygons">Clear</button>
    <button id="finish" class="finish" title="Save .filled.svg">Finish</button>
    <span id="status">Loading…</span>
  </div>
  <div class="hint">
    click to add vertex · click near first / right-click / dbl-click / Enter to close ·
    Backspace undo vertex · Esc abort · wheel zoom · Space-drag or middle-drag pan
  </div>
  <div id="stage"></div>

<script>
(() => {
  const stage  = document.getElementById('stage');
  const status = document.getElementById('status');
  const finishBtn = document.getElementById('finish');
  const clearBtn  = document.getElementById('clear');

  const CLOSE_PX = 12;          // screen-pixel threshold for "near first vertex"
  const SVG_NS = 'http://www.w3.org/2000/svg';
  let svg, staticOverlay, dynamicOverlay;
  let current = [];             // in-progress polygon, SVG coords
  let polygons = [];            // closed polygons, SVG coords
  let cursorSVG = null;         // last cursor position in SVG coords
  let spaceDown = false;
  let panning = false;
  let panStartSVG = null;
  let panStartVB  = null;

  function setStatus(msg, kind) {
    status.textContent = msg;
    status.className = kind || '';
  }

  function clientToSVG(clientX, clientY) {
    const pt = svg.createSVGPoint();
    pt.x = clientX; pt.y = clientY;
    const ctm = svg.getScreenCTM();
    if (!ctm) return null;
    const p = pt.matrixTransform(ctm.inverse());
    return [p.x, p.y];
  }

  function svgToClient(x, y) {
    const pt = svg.createSVGPoint();
    pt.x = x; pt.y = y;
    const ctm = svg.getScreenCTM();
    if (!ctm) return null;
    const p = pt.matrixTransform(ctm);
    return [p.x, p.y];
  }

  function ensureViewBox() {
    if (svg.hasAttribute('viewBox')) return;
    const w = parseFloat(svg.getAttribute('width'))  || svg.getBBox().width  || 1000;
    const h = parseFloat(svg.getAttribute('height')) || svg.getBBox().height || 1000;
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  }

  function clearChildren(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function renderStatic() {
    clearChildren(staticOverlay);
    for (const poly of polygons) {
      const el = document.createElementNS(SVG_NS, 'polygon');
      el.setAttribute('class', 'mark-fill');
      el.setAttribute('points', poly.map(p => p.join(',')).join(' '));
      staticOverlay.appendChild(el);
    }
  }

  function renderDynamic() {
    clearChildren(dynamicOverlay);
    if (current.length < 1) return;

    const path = document.createElementNS(SVG_NS, 'polyline');
    path.setAttribute('class', 'mark-line');
    path.setAttribute('points', current.map(p => p.join(',')).join(' '));
    dynamicOverlay.appendChild(path);

    if (cursorSVG) {
      const last = current[current.length - 1];
      const rb = document.createElementNS(SVG_NS, 'line');
      rb.setAttribute('class', 'mark-rubber');
      rb.setAttribute('x1', last[0]); rb.setAttribute('y1', last[1]);
      rb.setAttribute('x2', cursorSVG[0]); rb.setAttribute('y2', cursorSVG[1]);
      dynamicOverlay.appendChild(rb);
    }

    const r = (svg.viewBox.baseVal.width || 1000) * 0.005;
    current.forEach((p, i) => {
      const c = document.createElementNS(SVG_NS, 'circle');
      c.setAttribute('class', i === 0 ? 'mark-vertex-first' : 'mark-vertex');
      c.setAttribute('cx', p[0]); c.setAttribute('cy', p[1]);
      c.setAttribute('r', i === 0 ? r * 1.6 : r);
      dynamicOverlay.appendChild(c);
    });
  }

  function tryCloseNearFirst(clientX, clientY) {
    if (current.length < 3) return false;
    const first = current[0];
    const [cx, cy] = svgToClient(first[0], first[1]);
    const dx = cx - clientX, dy = cy - clientY;
    if (Math.hypot(dx, dy) <= CLOSE_PX) {
      closePolygon();
      return true;
    }
    return false;
  }

  function addVertex(clientX, clientY) {
    const p = clientToSVG(clientX, clientY);
    if (!p) return;
    current.push(p);
    renderDynamic();
  }

  function closePolygon() {
    if (current.length < 3) return;
    polygons.push(current);
    current = [];
    renderStatic();
    renderDynamic();
    setStatus(`${polygons.length} region${polygons.length === 1 ? '' : 's'} marked`);
  }

  function abortCurrent() {
    if (!current.length) return;
    current = [];
    renderDynamic();
  }

  function popVertex() {
    if (!current.length) return;
    current.pop();
    renderDynamic();
  }

  function clearAll() {
    polygons = []; current = [];
    renderStatic();
    renderDynamic();
    setStatus('cleared');
  }

  function zoomAt(clientX, clientY, factor) {
    const before = clientToSVG(clientX, clientY);
    const vb = svg.viewBox.baseVal;
    vb.width  *= factor;
    vb.height *= factor;
    const after = clientToSVG(clientX, clientY);
    vb.x += before[0] - after[0];
    vb.y += before[1] - after[1];
  }

  function bindEvents() {
    svg.addEventListener('contextmenu', e => { e.preventDefault(); closePolygon(); });

    svg.addEventListener('mousedown', e => {
      if (e.button === 1 || (e.button === 0 && spaceDown)) {
        e.preventDefault();
        panning = true;
        svg.classList.add('panning');
        const vb = svg.viewBox.baseVal;
        panStartVB = { x: vb.x, y: vb.y, w: vb.width, h: vb.height };
        panStartSVG = clientToSVG(e.clientX, e.clientY);
      }
    });

    window.addEventListener('mousemove', e => {
      if (panning) {
        // recompute SVG-coord of cursor against ORIGINAL viewBox to get
        // a stable delta independent of the live viewBox we're editing
        const vb = svg.viewBox.baseVal;
        vb.x = panStartVB.x;
        vb.y = panStartVB.y;
        const now = clientToSVG(e.clientX, e.clientY);
        vb.x = panStartVB.x - (now[0] - panStartSVG[0]);
        vb.y = panStartVB.y - (now[1] - panStartSVG[1]);
        return;
      }
      cursorSVG = clientToSVG(e.clientX, e.clientY);
      if (current.length) renderDynamic();
    });

    window.addEventListener('mouseup', () => {
      if (panning) { panning = false; svg.classList.remove('panning'); }
    });

    svg.addEventListener('click', e => {
      if (e.button !== 0) return;
      if (spaceDown) return;
      if (tryCloseNearFirst(e.clientX, e.clientY)) return;
      addVertex(e.clientX, e.clientY);
    });

    svg.addEventListener('dblclick', e => {
      // The two clicks of the dblclick each fired and added a vertex.
      // Pop the duplicate before closing.
      if (current.length >= 1) current.pop();
      closePolygon();
    });

    svg.addEventListener('wheel', e => {
      e.preventDefault();
      const factor = e.deltaY > 0 ? 1.15 : 1 / 1.15;
      zoomAt(e.clientX, e.clientY, factor);
    }, { passive: false });

    window.addEventListener('keydown', e => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      if (e.key === 'Enter')      { e.preventDefault(); closePolygon(); }
      else if (e.key === 'Escape'){ e.preventDefault(); abortCurrent(); }
      else if (e.key === 'Backspace') { e.preventDefault(); popVertex(); }
      else if (e.key === ' ')     { spaceDown = true; svg.style.cursor = 'grab'; e.preventDefault(); }
    });
    window.addEventListener('keyup', e => {
      if (e.key === ' ') { spaceDown = false; svg.style.cursor = ''; }
    });
  }

  async function finish() {
    finishBtn.disabled = true;
    setStatus('Saving…');
    try {
      const res = await fetch('/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ polygons }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
      setStatus('Saved → ' + data.path, 'ok');
    } catch (err) {
      setStatus('Save failed: ' + err.message, 'err');
    } finally {
      finishBtn.disabled = false;
    }
  }

  async function boot() {
    setStatus('Loading SVG…');
    let text;
    try {
      const res = await fetch('/svg');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      text = await res.text();
    } catch (err) {
      setStatus('Failed to load /svg: ' + err.message, 'err');
      return;
    }

    const doc = new DOMParser().parseFromString(text, 'image/svg+xml');
    const parserError = doc.querySelector('parsererror');
    if (parserError) {
      setStatus('SVG parse error', 'err');
      console.error(parserError.textContent);
      return;
    }
    svg = doc.documentElement;
    stage.appendChild(svg);

    ensureViewBox();
    const overlay = document.createElementNS(SVG_NS, 'g');
    overlay.setAttribute('class', 'mark-overlay');
    staticOverlay  = document.createElementNS(SVG_NS, 'g');
    dynamicOverlay = document.createElementNS(SVG_NS, 'g');
    overlay.appendChild(staticOverlay);
    overlay.appendChild(dynamicOverlay);
    svg.appendChild(overlay);

    bindEvents();
    setStatus('Ready — click to mark');
    finishBtn.addEventListener('click', finish);
    clearBtn.addEventListener('click', clearAll);
  }

  boot();
})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    src_path: Path  # set on the class by main()

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        sys.stderr.write(f"[mark] {format % args}\n")

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", INDEX_HTML.encode("utf-8"))
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
            polygons = _validate_polygons(payload.get("polygons", []))
            if not polygons:
                self._send_json(400, {"error": "no closed polygons received"})
                return
            original = self.src_path.read_bytes()
            new_bytes = build_fill_svg(original, polygons)
            out_path = next_sibling_path(self.src_path)
            out_path.write_bytes(new_bytes)
            self._send_json(200, {"path": str(out_path), "count": len(polygons)})
        except Exception as exc:  # surface to the browser
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _send_json(self, code: int, payload: dict):
        self._send(code, "application/json", json.dumps(payload).encode("utf-8"))

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def make_server(src: Path, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    Handler.src_path = src
    return ThreadingHTTPServer((host, port), Handler)


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        print("usage: python mark_assembly_step.py <input.svg>", file=sys.stderr)
        return 2
    src = Path(argv[1]).expanduser().resolve()
    if not src.exists():
        print(f"file not found: {src}", file=sys.stderr)
        return 2
    if src.suffix.lower() != ".svg":
        print(f"not an .svg: {src}", file=sys.stderr)
        return 2

    server = make_server(src)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    print(f"mark_assembly_step: serving {src.name}")
    print(f"         open {url}  (Ctrl+C to quit)")
    threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
