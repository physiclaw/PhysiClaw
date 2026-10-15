"""Exercise release failure boundaries without CAD or GitHub mutations."""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from hardware import release as rel

VERSION = "1.2"
COMMIT = "a" * 40
PDF = b"%PDF-1.7\nfixture\n%%EOF\n"
STEP = b"ISO-10303-21;\nfixture\nEND-ISO-10303-21;\n"
HTML = b"<!DOCTYPE html><html><body>fixture</body></html>"


def write_zip(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def build_outputs(root):
    for directory, mapping, pdf in (
        ("manual", rel.MANUALS, True),
        ("sourcing", rel.SOURCING, False),
        ("drawing", rel.DRAWINGS, True),
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
        for name in mapping.values():
            path = root / directory / name
            path.write_bytes(HTML)
            if pdf:
                path.with_suffix(".pdf").write_bytes(PDF)
    (root / "step").mkdir()
    (root / "step" / rel.FRAME_STEP).write_bytes(STEP)
    write_zip(
        root / "print_3d" / rel.PRINT_ZIP,
        {
            "part_x1.step": STEP,
            "3d_printed_parts_guide.txt": b"English guide",
            "3d打印零件说明.txt": "中文指南".encode(),
        },
    )


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "RELEASE_DIR", tmp_path / "release")
    monkeypatch.setattr(rel, "source_digest", lambda: "source-digest")
    monkeypatch.setattr(rel, "find_chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(rel, "render_pdf", lambda *args: True)
    monkeypatch.setattr(rel, "package_version", lambda name: "1.0")
    monkeypatch.setattr(rel, "run_tests", lambda: None)
    monkeypatch.setattr(rel, "run", lambda *args, **kwargs: COMMIT)
    calls = []

    def stage(output, *args):
        calls.append((output, args))
        if args[0] == "build":
            build_outputs(output)

    monkeypatch.setattr(rel, "_stage", stage)
    return calls


@pytest.fixture
def bundle(preparation):
    return rel.prepare(VERSION)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "../1.2",
        "1",
        "01.2",
        "1.2;echo bad",
        "$(echo bad)",
        "1.2\n",
        "v1.2",
        "1.2-rc1",
    ],
)
def test_invalid_version_is_rejected_before_commands(value, monkeypatch):
    monkeypatch.setattr(rel, "run", lambda *a: pytest.fail("must validate first"))
    with pytest.raises(rel.ReleaseError, match="HW_VERSION"):
        rel.tag_for(value)


def test_prepare_builds_in_private_tree_and_seals_exact_assets(bundle, preparation):
    verified, manifest = rel.verify(VERSION)
    assert verified == bundle
    assert manifest["commit"] == COMMIT
    assert set(manifest["artifacts"]) == rel.ASSETS
    roots = {root for root, _ in preparation}
    assert len(roots) == 1 and not next(iter(roots)).exists()
    assert [args for _, args in preparation] == [
        ("check",),
        ("build", "--bom", "--no-cache"),
        ("print",),
        ("manual", "--pdf"),
        ("sourcing",),
        ("drawing", "--pdf"),
        ("check",),
    ]
    with pytest.raises(rel.ReleaseError, match="already exists"):
        rel.prepare(VERSION)


def test_stage_failure_never_leaves_a_publishable_bundle(preparation, monkeypatch):
    def fail(*args):
        raise subprocess.CalledProcessError(1, "manual --pdf")

    monkeypatch.setattr(rel, "_stage", fail)
    with pytest.raises(subprocess.CalledProcessError):
        rel.prepare(VERSION)
    assert list(rel.RELEASE_DIR.iterdir()) == []


def test_sources_changed_mid_build_cannot_be_sealed(preparation, monkeypatch):
    values = iter(["before", "after"])
    monkeypatch.setattr(rel, "source_digest", lambda: next(values))
    with pytest.raises(rel.ReleaseError, match="changed during"):
        rel.prepare(VERSION)
    assert not (rel.RELEASE_DIR / rel.tag_for(VERSION)).exists()


def test_missing_chrome_fails_before_build(preparation, monkeypatch):
    monkeypatch.setattr(rel, "find_chrome", lambda: None)
    with pytest.raises(rel.ReleaseError, match="requires Chrome"):
        rel.prepare(VERSION)
    assert preparation == []


def test_failed_regression_gate_blocks_build(preparation, monkeypatch):
    def fail():
        raise subprocess.CalledProcessError(1, "pytest")

    monkeypatch.setattr(rel, "run_tests", fail)
    with pytest.raises(subprocess.CalledProcessError):
        rel.prepare(VERSION)
    assert preparation == []


def test_installed_but_unusable_browser_fails_before_build(preparation, monkeypatch):
    monkeypatch.setattr(rel, "render_pdf", lambda *args: False)
    with pytest.raises(rel.ReleaseError, match="preflight PDF"):
        rel.prepare(VERSION)
    assert preparation == []


@pytest.mark.parametrize(
    "damage", ["changed", "deleted", "empty", "extra", "symlink", "manifest"]
)
def test_bundle_damage_blocks_verification(bundle, damage):
    asset = bundle / rel.PRINT_ZIP
    if damage == "changed":
        asset.write_bytes(b"changed")
    elif damage == "deleted":
        asset.unlink()
    elif damage == "empty":
        asset.write_bytes(b"")
    elif damage == "extra":
        (bundle / "unexpected.zip").write_bytes(b"extra")
    elif damage == "symlink":
        asset.unlink()
        asset.symlink_to(bundle / rel.FRAME_ZIP)
    else:
        (bundle / rel.MANIFEST).write_text("[]")
    with pytest.raises(rel.ReleaseError):
        rel.verify(VERSION)


def test_stale_source_bundle_is_rejected(bundle, monkeypatch):
    monkeypatch.setattr(rel, "source_digest", lambda: "different")
    with pytest.raises(rel.ReleaseError, match="different sources"):
        rel.verify(VERSION)


def test_manifest_cannot_bless_broken_offline_document(bundle):
    path = bundle / rel.MANUAL_ZIP
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    name = f"manual/{rel.MANUALS['en']}"
    members[name] = b'<html><img src="assets/missing.svg"></html>'
    write_zip(path, members)
    manifest = json.loads((bundle / rel.MANIFEST).read_text())
    manifest["artifacts"][rel.MANUAL_ZIP] = rel.digest(path)
    (bundle / rel.MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(rel.ReleaseError, match="Missing or unsafe"):
        rel.verify(VERSION)


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("x.pdf", b"%PDF-1.7 partial"),
        ("x.step", b"ISO-10303-21; partial"),
        ("x.svg", b"<svg>"),
        ("x.html", b"<html>"),
    ],
)
def test_truncated_formats_are_rejected(name, data):
    with pytest.raises(rel.ReleaseError):
        rel._validate_bytes(name, data)


def test_archive_traversal_rejected(tmp_path):
    path = tmp_path / "bad.zip"
    write_zip(path, {"../outside.txt": b"bad"})
    with pytest.raises(rel.ReleaseError, match="Unsafe archive"):
        rel._read_zip(path)


@pytest.fixture
def github(bundle, monkeypatch):
    calls = []
    state = {
        "branch": "main",
        "dirty": "",
        "head": COMMIT,
        "remote": COMMIT,
        "tags": "",
        "releases": "",
        "failure": None,
    }

    def run(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return state["head"]
        if args[:3] == ("git", "branch", "--show-current"):
            return state["branch"]
        if args[:2] == ("git", "status"):
            return state["dirty"]
        if args[:2] == ("git", "ls-remote"):
            return f"{state['remote']}\trefs/heads/main\n" + state["tags"]
        if args[:2] == ("git", "tag"):
            return ""
        if args[:2] == ("git", "remote"):
            return "git@github.com:physiclaw/PhysiClaw.git"
        if args[:2] == ("gh", "api"):
            if state["failure"] == "auth":
                raise subprocess.CalledProcessError(1, args)
            return state["releases"]
        if args[:2] == ("gh", "release"):
            action = args[2]
            if state["failure"] == action:
                raise subprocess.CalledProcessError(1, args)
            if action == "view":
                names = sorted(rel.ASSETS | {rel.MANIFEST})
                if state["failure"] == "missing-asset":
                    names.pop()
                return json.dumps(
                    {
                        "isDraft": True,
                        "targetCommitish": COMMIT,
                        "assets": [{"name": n} for n in names],
                    }
                )
            if action == "download":
                target = Path(args[args.index("--dir") + 1])
                for path in bundle.iterdir():
                    shutil.copyfile(path, target / path.name)
                if state["failure"] == "checksum":
                    (target / rel.PRINT_ZIP).write_bytes(b"bad upload")
            return ""
        pytest.fail(f"Unexpected command: {args}")

    monkeypatch.setattr(rel, "run", run)
    return state, calls


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("branch", "feature"),
        ("branch", ""),
        ("dirty", " M hardware/scheme.py"),
        ("remote", "b" * 40),
        ("tags", f"{COMMIT}\trefs/tags/{rel.tag_for(VERSION)}"),
        ("releases", rel.tag_for(VERSION)),
        ("failure", "auth"),
    ],
)
def test_preflight_fails_closed(github, field, value):
    state, calls = github
    state[field] = value
    with pytest.raises((rel.ReleaseError, subprocess.CalledProcessError)):
        rel.publish(VERSION)
    assert not any(c[:3] == ("gh", "release", "create") for c in calls)


def test_publish_uses_exact_commit_and_promotes_only_after_download_verification(
    github,
):
    _, calls = github
    rel.publish(VERSION)
    releases = [c for c in calls if c[:2] == ("gh", "release")]
    assert [c[2] for c in releases] == ["create", "upload", "view", "download", "edit"]
    create = releases[0]
    assert "--draft" in create and create[create.index("--target") + 1] == COMMIT
    assert create[create.index("--repo") + 1] == "github.com/physiclaw/PhysiClaw"
    assert "--draft=false" in releases[-1]


@pytest.mark.parametrize(
    "failure", ["create", "upload", "view", "download", "missing-asset", "checksum"]
)
def test_upload_failure_never_publishes_or_deletes_draft(github, failure):
    state, calls = github
    state["failure"] = failure
    with pytest.raises((rel.ReleaseError, subprocess.CalledProcessError)):
        rel.publish(VERSION)
    assert not any(
        c[:3] in [("gh", "release", "edit"), ("gh", "release", "delete")] for c in calls
    )


def test_source_digest_includes_untracked_files_and_renames(tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "REPO_ROOT", tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / "model.py"
    source.write_text("SIZE = 1")
    first = rel.source_digest()
    source.write_text("SIZE = 2")
    assert rel.source_digest() != first
    second = rel.source_digest()
    source.rename(tmp_path / "renamed.py")
    assert rel.source_digest() != second
