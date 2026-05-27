"""CLI entry point — opens the browser on the served SVG.

    uv run --group cad python -m hardware.assembly.mark <input.svg>
"""

from __future__ import annotations

import sys
import threading
import webbrowser
from pathlib import Path
from typing import List

from hardware.assembly.mark.server import make_server


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m hardware.assembly.mark <input.svg>", file=sys.stderr)
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
    print(f"mark: serving {src.name}")
    print(f"      open {url}  (Ctrl+C to quit)")
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
