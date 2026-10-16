import json

import pytest

from hardware import buildlog
from hardware.documents import BuildError, pdf


def test_incomplete_pdf_is_retried_and_never_replaces_previous_file(
    tmp_path, monkeypatch
):
    target = tmp_path / "guide.pdf"
    target.write_bytes(b"previous good PDF")
    calls = []

    def partial(chrome, uri, profile, output):
        calls.append(output)
        output.write_bytes(b"%PDF-1.7 partial")
        return True

    monkeypatch.setattr(pdf, "_chrome_print", partial)
    assert not pdf.render_pdf("<html></html>", target, "chrome")
    assert len(calls) == pdf.PDF_ATTEMPTS
    assert target.read_bytes() == b"previous good PDF"


def test_complete_pdf_replaces_previous_file(tmp_path, monkeypatch):
    target = tmp_path / "guide.pdf"

    def complete(chrome, uri, profile, output):
        output.write_bytes(b"%PDF-1.7\n%%EOF\n")
        return True

    monkeypatch.setattr(pdf, "_chrome_print", complete)
    assert pdf.render_pdf("<html></html>", target, "chrome")
    assert target.read_bytes() == b"%PDF-1.7\n%%EOF\n"


@pytest.mark.parametrize("document", ["manual", "drawing"])
def test_failed_pdf_render_is_not_reported_as_completed_task(
    tmp_path, monkeypatch, document
):
    from hardware.documents.drawing import extrusion
    from hardware.documents.manual import build as manual

    builder = manual if document == "manual" else extrusion
    monkeypatch.setattr(builder, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(builder, "render_pdf", lambda *args: False)
    if document == "manual":
        monkeypatch.setattr(manual, "load_pages", lambda: [])
        monkeypatch.setattr(manual, "HAND_FIGURES", {})
        monkeypatch.setattr(manual, "SVG_DIR", tmp_path / "svg")
    kwargs = {"inline": False} if document == "manual" else {}
    with (
        buildlog.session(tmp_path) as log,
        pytest.raises(BuildError, match="PDF render failed"),
    ):
        builder.build(["en"], tmp_path / "out", pdf=True, **kwargs)
    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    pdf_events = [event for event in events if event.get("item") == "Render PDF [en]"]
    assert [event["event"] for event in pdf_events] == ["task_started", "task_failed"]


def test_pdf_cleanup_kills_helpers_after_browser_leader_exits(tmp_path):
    import os
    import select
    import signal
    import sys

    marker = tmp_path / "helper.pid"
    ready = tmp_path / "ready"
    fifo = tmp_path / "helper.pipe"
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    helper = (
        "import os, time; from pathlib import Path; "
        f"fd = os.open({str(fifo)!r}, os.O_WRONLY); os.write(fd, b'ready'); "
        f"Path({str(ready)!r}).write_text('ready'); time.sleep(30)"
    )
    browser = tmp_path / "fake-chrome"
    browser.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "path = next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--print-to-pdf='))\n"
        "Path(path).write_bytes(b'%PDF-1.7\\n%%EOF\\n')\n"
        f"helper = subprocess.Popen([sys.executable, '-c', {helper!r}])\n"
        f"Path({str(marker)!r}).write_text(str(helper.pid))\n"
        f"while not Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
        "print('Browser diagnostics', file=sys.stderr)\n"
    )
    browser.chmod(0o755)
    try:
        with buildlog.session(tmp_path) as log:
            assert pdf._chrome_print(
                str(browser),
                "file:///fixture.html",
                str(tmp_path / "profile"),
                tmp_path / "out.pdf",
            )
        assert os.read(reader, 100) == b"ready"
        # The helper owns the FIFO writer. EOF proves it exited, even on
        # systems that briefly retain an orphan's PID as a zombie.
        assert select.select([reader], [], [], 2)[0] == [reader]
        assert os.read(reader, 100) == b""
        events = [json.loads(line) for line in log.path.read_text().splitlines()]
        assert (
            "Browser diagnostics"
            in next(event for event in events if event["event"] == "pdf_diagnostics")[
                "message"
            ]
        )
    finally:
        os.close(reader)
        if marker.exists():
            try:
                os.kill(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
