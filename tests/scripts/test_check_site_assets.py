"""Site mismatches fail without requiring sibling checkouts or live HTTP."""

import hashlib
import io
import zipfile
from dataclasses import replace

import pytest

from scripts import check_site_assets as assets


class Downloads:
    """The HTTP boundary: supplied JSON responses and downloaded bytes only."""

    def __init__(self, directory):
        self.directory = directory
        self.responses = {}
        self.files = {}

    def json(self, url):
        return self.responses[url]

    def get(self, url):
        path = self.directory / hashlib.sha256(url.encode()).hexdigest()
        path.write_bytes(self.files[url])
        return path


@pytest.fixture
def source_audit(tmp_path):
    def make(filename, root="public"):
        repo = "physiclaw/PhysiClaw-site"
        downloads = Downloads(tmp_path)
        downloads.responses[f"{assets.API}/repos/{repo}/commits/main"] = {
            "sha": "current"
        }
        downloads.responses[
            f"{assets.API}/repos/{repo}/git/trees/current?recursive=1"
        ] = {"tree": [{"path": f"{root}/{filename}", "type": "blob", "sha": "blob"}]}
        inventory = assets.Inventory(
            files=(),
            expand=(),
            rewrite_downloads=(),
            public_repositories={repo: "https://physiclaw.ai"},
        )
        return assets.Audit(downloads, inventory, workers=1, quiet=True)

    return make


@pytest.mark.parametrize(
    "filename", ["extra.svg", "_extra.svg", ".extra.svg", "nested/_extra.svg"]
)
def test_source_inventory_unlisted_public_file_reports_failure(source_audit, filename):
    audit = source_audit(filename)

    audit.source_inventory()

    assert [(row.file, row.web_url, row.status) for row in audit.rows] == [
        (filename, f"https://physiclaw.ai/{filename}", "FAIL")
    ]
    assert "missing from inventory" in audit.rows[0].detail


@pytest.mark.parametrize("filename", ["_headers", "_redirects"])
def test_source_inventory_hosting_configuration_is_not_a_served_asset(
    source_audit, filename
):
    audit = source_audit(filename)

    audit.source_inventory()

    assert audit.rows == []


def test_source_inventory_unlisted_bundled_image_reports_failure(source_audit):
    audit = source_audit("new.svg", root="src/assets")

    audit.source_inventory()

    assert [(row.file, row.status) for row in audit.rows] == [
        ("src/assets/new.svg", "FAIL")
    ]


@pytest.mark.parametrize("stale", [False, True])
def test_release_marker_must_match_selected_hardware_release(tmp_path, stale):
    inventory = assets.INVENTORY.for_hardware("physiclaw-hardware-v1.2")
    entry = next(e for e in inventory.files if e.check == "release-tag")
    downloads = Downloads(tmp_path)
    downloads.files[entry.web_url] = (
        b"physiclaw-hardware-v1.1\n" if stale else b"physiclaw-hardware-v1.2\n"
    )
    audit = assets.Audit(downloads, inventory, workers=1, quiet=True)

    audit.run(entry, audit.check)

    assert [row.status for row in audit.rows] == ["FAIL" if stale else "PASS"]


@pytest.mark.parametrize("collision", [False, True])
def test_manual_assets_use_flat_served_names_and_reject_collisions(tmp_path, collision):
    inventory = assets.INVENTORY.for_hardware("physiclaw-hardware-v1.2")
    rule = next(e for e in inventory.expand if e.check == "members")
    downloads = Downloads(tmp_path)
    path = tmp_path / "manual.zip"
    members = {"manual/assets/sub/diagram.svg": b"nested diagram"}
    if collision:
        members["assets/diagram.svg"] = b"different diagram"
    else:
        members["wrapper/assets/other.svg"] = b"other diagram"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
            downloads.files[rule.web_url + name.rsplit("/", 1)[-1]] = data
    audit = assets.Audit(downloads, inventory, workers=1, quiet=True)
    audit.release_paths[rule.release_asset_url] = path

    audit.run(rule, audit.expanded_files)

    if collision:
        assert [row.status for row in audit.rows] == ["FAIL"]
        assert "duplicate served filenames" in audit.rows[0].detail
    else:
        assert {
            (row.web_url, row.release_member, row.status) for row in audit.rows
        } == {
            (rule.web_url + name.rsplit("/", 1)[-1], name, "PASS") for name in members
        }


@pytest.mark.parametrize("failure", ["", "stale", "missing", "extra", "external"])
def test_bundled_logo_matches_github_and_requires_one_image_per_locale(
    tmp_path, failure
):
    rule = next(e for e in assets.INVENTORY.expand if e.check == "source-image")
    downloads = Downloads(tmp_path)
    repo = "physiclaw/docs-site"
    downloads.responses[f"{assets.API}/repos/{repo}/commits/main"] = {"sha": "current"}
    downloads.responses[f"{assets.API}/repos/{repo}/git/trees/current?recursive=1"] = {
        "tree": [
            {"path": "src/assets/crab.svg", "type": "blob", "sha": assets.blob(b"logo")}
        ]
    }
    url = "https://docs.physiclaw.ai/_astro/crab.hash.svg"
    downloads.files[url] = b"old logo" if failure == "stale" else b"logo"
    html = b'<img src="/_astro/crab.hash.svg">'
    downloads.files[rule.web_url.format(locale="zh")] = html
    if failure == "missing":
        html = b"<html>missing logo</html>"
    elif failure == "extra":
        html += b'<img src="/_astro/crab.other.svg">'
    elif failure == "external":
        html = b'<img src="https://other.example/crab.hash.svg">'
    downloads.files[rule.web_url.format(locale="en")] = html
    audit = assets.Audit(downloads, assets.INVENTORY, workers=1, quiet=True)

    audit.run(rule, audit.source_images)

    assert any(row.status == "FAIL" for row in audit.rows) == bool(failure)
    if failure != "stale":
        assert [
            (row.web_url, row.status) for row in audit.rows if row.status == "PASS"
        ] == [(url, "PASS")]
    assert not any("{locale}" in row.web_url for row in audit.rows)


@pytest.fixture
def gallery_audit(tmp_path):
    downloads = Downloads(tmp_path)
    images = {
        f"{kind}/gallery_{number:02d}.webp": f"{kind} photo {number}".encode()
        for kind in ("thumb", "full")
        for number in (1, 2)
    }
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        for name, data in images.items():
            archive.writestr(name, data)
    release_url = f"{assets.RELEASES}/gallery/photos.zip"
    downloads.files[release_url] = content.getvalue()
    downloads.responses[f"{assets.API}/repos/{assets.REPO}/releases/tags/gallery"] = {
        "assets": [
            {
                "name": "photos.zip",
                "browser_download_url": release_url,
                "size": len(content.getvalue()),
                "digest": "sha256:" + hashlib.sha256(content.getvalue()).hexdigest(),
            }
        ]
    }
    html = "".join(
        f'<a href="/_astro/gallery_{number:02d}.full.webp">'
        f'<img src="/_astro/gallery_{number:02d}.thumb.webp"></a>'
        for number in (1, 2)
    ).encode()
    for locale in ("en", "zh"):
        downloads.files[f"https://docs.physiclaw.ai/{locale}/hardware-gallery/"] = html
    for name, data in images.items():
        kind, filename = name.split("/")
        downloads.files[
            "https://docs.physiclaw.ai/_astro/"
            + filename.replace(".webp", f".{kind}.webp")
        ] = data
    downloads.files["https://other.example/gallery_01.full.webp"] = images[
        "full/gallery_01.webp"
    ]
    rule = replace(
        next(e for e in assets.INVENTORY.expand if e.check == "gallery"),
        release_asset_url=release_url,
    )
    return assets.Audit(downloads, assets.INVENTORY, workers=1, quiet=True), rule


@pytest.mark.parametrize("locale,other", [("en", "zh"), ("zh", "en")])
@pytest.mark.parametrize(
    "old,new,reason",
    [
        (
            b'<a href="/_astro/gallery_02.full.webp">'
            b'<img src="/_astro/gallery_02.thumb.webp"></a>',
            b"",
            "gallery_02.webp",
        ),
        (
            b"/_astro/gallery_01.full.webp",
            b"https://other.example/gallery_01.full.webp",
            "not served by the expected website",
        ),
    ],
)
def test_gallery_page_mismatch_reports_actual_page_and_checks_other_locale(
    gallery_audit, locale, other, old, new, reason
):
    audit, rule = gallery_audit
    failed_url = f"https://docs.physiclaw.ai/{locale}/hardware-gallery/"
    audit.downloads.files[failed_url] = audit.downloads.files[failed_url].replace(
        old, new
    )

    audit.run(rule, audit.gallery)

    assert [
        (row.web_url, row.status) for row in audit.rows if row.status == "FAIL"
    ] == [(failed_url, "FAIL")]
    assert reason in next(row.detail for row in audit.rows if row.status == "FAIL")
    assert (f"https://docs.physiclaw.ai/{other}/hardware-gallery/", "PASS") in {
        (row.web_url, row.status) for row in audit.rows
    }


def test_gallery_missing_release_reports_both_resolved_page_urls(gallery_audit):
    audit, rule = gallery_audit
    audit.downloads.responses[
        f"{assets.API}/repos/{assets.REPO}/releases/tags/gallery"
    ] = {"assets": []}

    audit.run(rule, audit.gallery)

    assert [(row.web_url, row.status) for row in audit.rows] == [
        ("https://docs.physiclaw.ai/en/hardware-gallery/", "FAIL"),
        ("https://docs.physiclaw.ai/zh/hardware-gallery/", "FAIL"),
    ]
