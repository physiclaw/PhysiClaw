"""Setup page rendering and phone listener URL substitution."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from physiclaw.core.setup import pages
from physiclaw.core.setup.pages import (
    handle_setup_page,
    serve_bridge_page,
    serve_qr_page,
)


def _fake_request(url_port=8048):
    return SimpleNamespace(url=SimpleNamespace(port=url_port))


# ---------- serve_bridge_page ----------


@pytest.mark.asyncio
async def test_serve_bridge_page_reads_html_and_sets_no_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "bridge.html").write_text("<html>bridge</html>", encoding="utf-8")
    monkeypatch.setattr(pages, "STATIC_DIR", static)

    resp = await serve_bridge_page(_fake_request())

    assert resp.body == b"<html>bridge</html>"
    assert resp.headers["cache-control"] == "no-store"


# ---------- serve_qr_page ----------


@pytest.mark.asyncio
async def test_serve_qr_page_substitutes_phone_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "qr.html").write_text(
        "<html>__PHONE_URL__ / __PHONE_URL_FALLBACK__</html>", encoding="utf-8"
    )
    monkeypatch.setattr(pages, "STATIC_DIR", static)
    mocker.patch.object(
        pages,
        "bridge_base_urls",
        return_value=("http://mac.local:8048", "http://192.168.1.1:8048"),
    )

    resp = await serve_qr_page(_fake_request(url_port=8048))

    body = resp.body.decode()
    assert "http://mac.local:8048/bridge / http://192.168.1.1:8048/bridge" in body


@pytest.mark.asyncio
async def test_serve_qr_page_uses_default_port_when_url_port_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "qr.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(pages, "STATIC_DIR", static)
    base_urls = mocker.patch.object(
        pages,
        "bridge_base_urls",
        return_value=("p", "f"),
    )

    await serve_qr_page(_fake_request(url_port=None))

    base_urls.assert_called_once_with(8048)


@pytest.mark.asyncio
@pytest.mark.parametrize("no_runtime", [False, True])
async def test_handle_setup_page_serves_wizard(mocker, no_runtime) -> None:
    mocker.patch.object(
        pages.runtime_state, "read_live", return_value={"no_runtime": no_runtime}
    )
    mocker.patch.object(
        pages,
        "bridge_base_urls",
        return_value=("http://device.local:8048", "http://10.0.0.5:8048"),
    )
    req = _fake_request()
    req.url = SimpleNamespace(port=8048)

    resp = await handle_setup_page(req)

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = bytes(resp.body)
    assert b"Hardware Setup" in body
    # drives the same API the CLI does
    assert b"/api/calibrate/arm" in body
    # phone bridge URLs are substituted in so the QR renders inline (no iframe)
    assert b"__PHONE_URL__" not in body
    assert b"http://device.local:8048/bridge" in body
    assert b"<iframe" not in body
    assert b"__NO_RUNTIME__" not in body
    assert (b"if (true)" if no_runtime else b"if (false)") in body
