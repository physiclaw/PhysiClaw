"""PDF rendering via headless Chrome.

The manual's CSS is print-ready (@page A4 landscape), so it is rendered
with the browser engine it's designed for rather than a separate PDF
library. Callers fail when PDF was requested but Chrome is unavailable.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

from hardware import buildlog
from hardware.port.desktop import find_chrome as find_chrome
from hardware.port.processes import ignore_interrupts, owned_process

PDF_TIMEOUT = 90  # s per attempt — a clean print is ~40s; this is generous
PDF_ATTEMPTS = 2  # a fresh process retry covers the rare launch that never
# writes the file (cf. the OCCT crash-retry in dispatch.py).


def _chrome_print(chrome: str, src_uri: str, profile: str, pdf_path: Path) -> bool:
    """One headless-Chrome print. Returns True once the PDF is written.

    ``--headless=new --print-to-pdf`` reliably *writes* the PDF but then often
    fails to *exit*, so we watch the output file rather than the process exit
    code: once it appears and its size stops growing, the print is done and we
    kill Chrome. Chrome runs in its own process group so a stuck launch (and
    all its renderer/helper children) dies as a unit — a survivor would
    contend with the next attempt."""
    pdf_path.unlink(missing_ok=True)  # so a stale file isn't read as success

    def written() -> bool:
        return pdf_path.exists() and pdf_path.stat().st_size > 0

    with (
        tempfile.TemporaryFile(
            mode="w+", encoding="utf-8", errors="replace"
        ) as diagnostics,
        owned_process(
            [
                chrome,
                "--headless=new",
                "--no-first-run",
                "--no-default-browser-check",
                # Edge's Windows compatibility relaunch drops the print
                # process and its diagnostic handles. Keep the owned process.
                "--edge-skip-compat-layer-relaunch",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                f"--user-data-dir={profile}",
                "--no-pdf-header-footer",
                "--virtual-time-budget=30000",
                f"--print-to-pdf={pdf_path}",
                src_uri,
            ],
            stdout=diagnostics,
            stderr=subprocess.STDOUT,
        ) as owner,
    ):
        proc = owner.process
        try:
            deadline = time.monotonic() + PDF_TIMEOUT
            last_size, stable = -1, 0
            while time.monotonic() < deadline:
                size = pdf_path.stat().st_size if pdf_path.exists() else 0
                stable = stable + 1 if size > 0 and size == last_size else 0
                if stable >= 3:  # size held ~2.4s → fully written
                    return True
                last_size = size
                if proc.poll() is not None:  # exited on its own
                    return written()
                time.sleep(0.8)
            return written()
        finally:
            # Helpers may outlive an already-exited browser leader. Reap and
            # clean its private group before closing the diagnostic stream.
            with ignore_interrupts():
                owner.stop(timeout=0)
            diagnostics.seek(0)
            output = diagnostics.read()
            if output:
                buildlog.event(
                    "pdf_diagnostics",
                    output,
                    level="debug",
                    path=str(pdf_path),
                    returncode=proc.returncode,
                )


def render_pdf(html: str, pdf_path: Path, chrome: str) -> bool:
    """Render ``html`` to ``pdf_path`` via headless Chrome, retrying in a
    fresh process if a launch hangs.

    The HTML is written to a temp file beside the PDF (so its relative
    ``assets/`` figure references resolve against the emitted asset dir) and
    printed with the new headless mode, which honours the document's CSS
    @page size/margins (A4 landscape, zero margin). ``loading="lazy"`` is
    stripped so every figure is painted in the single print pass. Returns
    True once a non-empty PDF is produced, else False."""
    html = html.replace(' loading="lazy"', "")
    for attempt in range(1, PDF_ATTEMPTS + 1):
        with (
            tempfile.TemporaryDirectory() as profile,
            tempfile.TemporaryDirectory(dir=pdf_path.parent) as staging,
            tempfile.NamedTemporaryFile(
                "w", suffix=".html", dir=pdf_path.parent, encoding="utf-8", delete=False
            ) as tmp,
        ):
            tmp_path = Path(tmp.name)
            tmp.write(html)
            tmp.close()  # Windows cannot reopen/delete an open temporary file.
            try:
                staged_pdf = Path(staging) / pdf_path.name
                result = _chrome_print(chrome, tmp_path.as_uri(), profile, staged_pdf)
                if result:
                    data = staged_pdf.read_bytes()
                    result = data.startswith(b"%PDF-") and data.rstrip().endswith(
                        b"%%EOF"
                    )
                if result:
                    staged_pdf.replace(pdf_path)
            finally:
                tmp_path.unlink(missing_ok=True)
        if result:  # _chrome_print returns True only after the PDF is written
            return True
        tail = " — retrying" if attempt < PDF_ATTEMPTS else " — retries exhausted"
        buildlog.event(
            "pdf_retry",
            f"PDF render failed for {pdf_path.name}{tail}",
            level="warning",
            attempt=attempt,
            path=str(pdf_path),
        )
    return False
