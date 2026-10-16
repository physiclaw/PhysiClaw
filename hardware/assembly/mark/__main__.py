"""CLI entry point — opens the browser on the served SVG.

    uv run --group cad python -m hardware.assembly.mark <input.svg>
    uv run --group cad python -m hardware.assembly.mark <patch.json>

Passing a ``patch/<stem>.json`` resolves its source SVG in output/svg and
opens it with the existing patch loaded for editing (select / move / recolour).
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path
from typing import List

from hardware import buildlog
from hardware.assembly.mark.patch import source_for_patch
from hardware.assembly.mark.server import make_server


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path, help="source SVG or patch JSON")
    buildlog.add_arguments(parser)
    args = parser.parse_args(argv[1:])
    with buildlog.command("Hardware annotation editor", args, name="mark"):
        arg = args.file.expanduser().resolve()
        if not arg.exists():
            raise ValueError(f"File not found: {arg}")
        if arg.suffix.lower() == ".json":
            src = source_for_patch(arg)
            if not src.exists():
                raise ValueError(f"No source SVG for {arg.name}: expected {src}")
        elif arg.suffix.lower() == ".svg":
            src = arg
        else:
            raise ValueError(f"Expected an SVG or JSON file: {arg}")
        server = make_server(src)
        host, port = server.server_address[:2]
        url = f"http://{host}:{port}/"
        buildlog.event("annotation_ready", f"Open {url}", source=str(src), url=url)
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
        try:
            server.serve_forever()
        finally:
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
