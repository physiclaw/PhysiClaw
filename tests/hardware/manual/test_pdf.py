from hardware.manual import pdf


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
