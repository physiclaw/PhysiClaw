"""Exercise release failure boundaries without CAD or GitHub mutations."""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from hardware import buildlog, scheme
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
    monkeypatch.setattr(rel, "CACHE_DIR", tmp_path / "cache")
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
            assert (output / ".cache").is_symlink()
            assert (output / ".cache").resolve() == rel.CACHE_DIR
            assert not (output / "step").exists()
            (output / ".cache" / "marker").write_text("retained")
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
        ("build", "--bom"),
        ("print",),
        ("manual", "--pdf"),
        ("sourcing",),
        ("drawing", "--pdf"),
        ("check",),
    ]
    assert (rel.CACHE_DIR / "marker").read_text() == "retained"
    count = len(preparation)
    assert rel.prepare(VERSION) == bundle
    assert len(preparation) == count


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
        rel.prepare(VERSION)


def test_stale_source_bundle_is_rejected(bundle, monkeypatch):
    monkeypatch.setattr(rel, "source_digest", lambda: "different")
    with pytest.raises(rel.ReleaseError, match="different sources"):
        rel.prepare(VERSION)


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


def draft(bundle, names=None):
    names = sorted(rel.ASSETS | {rel.MANIFEST}) if names is None else names
    return {
        "id": 42,
        "tag_name": rel.tag_for(VERSION),
        "draft": True,
        "prerelease": False,
        "target_commitish": COMMIT,
        "assets": [
            {
                "id": index,
                "name": name,
                "state": "uploaded",
                "size": (bundle / name).stat().st_size,
                "digest": f"sha256:{rel.digest(bundle / name)}",
                "updated_at": "2026-10-15T00:00:00Z",
            }
            for index, name in enumerate(names)
        ],
    }


@pytest.fixture
def github(bundle, monkeypatch):
    calls = []
    state = {
        "branch": "main",
        "dirty": "",
        "head": COMMIT,
        "remote": COMMIT,
        "tags": "",
        "release": None,
        "failure": None,
        "after_upload": None,
        "before_final_check": None,
        "remote_checks": 0,
        "tag_commit": COMMIT,
        "after_publish": None,
        "after_docs": None,
        "latest_id": 42,
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
            state["remote_checks"] += 1
            if state["remote_checks"] == 2 and state["before_final_check"]:
                state["before_final_check"]()
            return f"{state['remote']}\trefs/heads/main\n" + state["tags"]
        if args[:2] == ("git", "tag"):
            return ""
        if args[:2] == ("git", "remote"):
            return "git@github.com:physiclaw/PhysiClaw.git"
        if args[:2] == ("gh", "api"):
            if state["failure"] == "auth" or (
                state["failure"] == "view" and state["release"]
            ):
                raise subprocess.CalledProcessError(1, args)
            if "PATCH" in args:
                if state["failure"] == "publish":
                    raise subprocess.CalledProcessError(1, args)
                assert "repos/physiclaw/PhysiClaw/releases/42" in args
                state["release"]["draft"] = False
                state["release"]["prerelease"] = False
                if state["after_publish"]:
                    state["after_publish"]()
            if any("/commits/refs/tags/" in a for a in args):
                if state["failure"] == "tag-read":
                    raise subprocess.CalledProcessError(1, args)
                return state["tag_commit"]
            if args[-1].endswith("/releases/latest"):
                if state["failure"] == "published-read":
                    raise subprocess.CalledProcessError(1, args)
                remote = dict(state["release"], id=state["latest_id"])
                return json.dumps(remote)
            return json.dumps(state["release"]) if state["release"] else ""
        if args[:2] == ("gh", "release"):
            action = args[2]
            if state["failure"] == action:
                raise subprocess.CalledProcessError(1, args)
            if action == "create":
                assert state["release"] is None
                state["release"] = draft(bundle, [])
            if action == "upload":
                assert kwargs["timeout"] == 600
                paths = args[4 : args.index("--repo")]
                assets = draft(bundle, [Path(p).name for p in paths])["assets"]
                if state["failure"] == "partial-upload":
                    state["release"]["assets"].append(assets[0])
                    raise subprocess.TimeoutExpired(args, 600)
                if state["failure"] == "missing-asset":
                    assets.pop()
                state["release"]["assets"].extend(assets)
                if state["after_upload"]:
                    state["after_upload"]()
            if action == "download":
                assert kwargs["timeout"] == 600
                target = Path(args[args.index("--dir") + 1])
                name = args[args.index("--pattern") + 1]
                shutil.copyfile(bundle / name, target / name)
                if state["failure"] == "checksum":
                    (target / name).write_bytes(b"bad upload")
                for asset in state["release"]["assets"]:
                    asset["download_count"] = asset.get("download_count", 0) + 1
            return ""
        if args == ("make", "docs-deploy"):
            assert state["release"]["draft"] is False
            if state["failure"] == "docs":
                raise subprocess.CalledProcessError(1, args)
            if state["after_docs"]:
                state["after_docs"]()
            return "docs-site deploy triggered"
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
        ("failure", "auth"),
    ],
)
def test_preflight_fails_closed(github, field, value):
    state, calls = github
    state[field] = value
    with pytest.raises((rel.ReleaseError, subprocess.CalledProcessError)):
        rel.publish(VERSION)
    assert not any(c[:2] == ("gh", "release") for c in calls)


def test_publish_uses_exact_commit_and_server_checksums_without_download(github):
    _, calls = github
    rel.publish(VERSION)
    releases = [c for c in calls if c[:2] == ("gh", "release")]
    assert [c[2] for c in releases] == ["create", "upload"]
    create = releases[0]
    assert "--draft" in create and create[create.index("--target") + 1] == COMMIT
    assert create[create.index("--repo") + 1] == "github.com/physiclaw/PhysiClaw"
    api = [c for c in calls if c[:2] == ("gh", "api")]
    assert "--paginate" in api[0]  # Includes unpublished drafts.
    publish = next(c for c in api if "PATCH" in c)
    assert "repos/physiclaw/PhysiClaw/releases/42" in publish
    assert {
        "draft=false",
        "prerelease=false",
        "make_latest=true",
        f"target_commitish={COMMIT}",
    } <= set(publish)
    latest_reads = [
        i for i, c in enumerate(calls) if c[-1].endswith("/releases/latest")
    ]
    hook_index = calls.index(("make", "docs-deploy"))
    assert len(latest_reads) == 2 and latest_reads[0] < hook_index < latest_reads[1]
    assert calls[-1][-3:] == (
        f"repos/physiclaw/PhysiClaw/commits/refs/tags/{rel.tag_for(VERSION)}",
        "--jq",
        ".sha",
    )


def test_docs_failure_leaves_published_release_and_gives_specific_retry(github):
    state, calls = github
    state["failure"] = "docs"
    with pytest.raises(
        rel.ReleaseError, match="published.*Retry only: make docs-deploy"
    ):
        rel.publish(VERSION)
    assert state["release"]["draft"] is False
    assert calls[-1] == ("make", "docs-deploy")


@pytest.mark.parametrize("count", [0, 3, 7])
def test_resume_matching_draft_uploads_only_missing_assets(github, bundle, count):
    state, calls = github
    names = sorted(rel.ASSETS | {rel.MANIFEST})
    state["release"] = draft(bundle, names[:count])
    rel.publish(VERSION)
    assert not any(c[:3] == ("gh", "release", "create") for c in calls)
    uploads = [c for c in calls if c[:3] == ("gh", "release", "upload")]
    if count == len(names):
        assert uploads == []
    else:
        assert len(uploads) == 1
        assert uploads[0][4 : uploads[0].index("--repo")] == tuple(
            str(bundle / n) for n in names[count:]
        )
    assert state["release"]["draft"] is False


def test_partial_upload_timeout_can_resume_without_duplicate_upload(github):
    state, calls = github
    state["failure"] = "partial-upload"
    with pytest.raises(subprocess.TimeoutExpired):
        rel.publish(VERSION)
    retained = state["release"]["assets"][0]["name"]
    assert state["release"]["draft"] is True
    calls.clear()
    state["failure"] = None
    rel.publish(VERSION)
    upload = next(c for c in calls if c[:3] == ("gh", "release", "upload"))
    assert retained not in [Path(p).name for p in upload[4 : upload.index("--repo")]]


@pytest.mark.parametrize(
    "damage",
    ["published", "target", "tag", "extra", "duplicate", "size", "state", "digest"],
)
def test_conflicting_draft_is_never_modified(github, bundle, damage):
    state, calls = github
    remote = state["release"] = draft(bundle)
    asset = remote["assets"][0]
    if damage == "published":
        remote["draft"] = False
    elif damage == "target":
        remote["target_commitish"] = "b" * 40
    elif damage == "tag":
        remote["tag_name"] = "unexpected"
    elif damage == "extra":
        asset["name"] = "unexpected.zip"
    elif damage == "duplicate":
        remote["assets"].append(asset.copy())
    elif damage == "size":
        asset["size"] += 1
    elif damage == "state":
        asset["state"] = "starter"
    else:
        asset["digest"] = "sha256:" + "0" * 64
    with pytest.raises(rel.ReleaseError):
        rel.publish(VERSION)
    assert not any(c[:2] == ("gh", "release") for c in calls)


@pytest.mark.parametrize(
    "failure", ["create", "upload", "view", "missing-asset", "publish"]
)
def test_upload_failure_never_publishes_or_deletes_draft(github, failure):
    state, calls = github
    state["failure"] = failure
    with pytest.raises((rel.ReleaseError, subprocess.CalledProcessError)):
        rel.publish(VERSION)
    assert state["release"] is None or state["release"]["draft"] is True
    assert ("make", "docs-deploy") not in calls
    assert not any(c[:3] == ("gh", "release", "delete") for c in calls)


@pytest.mark.parametrize("failure", [None, "download", "checksum"])
def test_download_fallback_only_for_asset_without_server_digest(
    github, bundle, failure
):
    state, calls = github
    state["release"] = draft(bundle)
    asset = state["release"]["assets"][0]
    asset["digest"] = None
    state["failure"] = failure
    if failure:
        with pytest.raises((rel.ReleaseError, subprocess.CalledProcessError)):
            rel.publish(VERSION)
        assert state["release"]["draft"] is True
    else:
        rel.publish(VERSION)
        assert state["release"]["draft"] is False
    downloads = [c for c in calls if c[:3] == ("gh", "release", "download")]
    assert len(downloads) == 1
    assert downloads[0][downloads[0].index("--pattern") + 1] == asset["name"]


@pytest.mark.parametrize(
    "change", ["head", "remote", "source", "local-asset", "draft", "asset"]
)
def test_changes_during_transfer_block_publication(github, bundle, monkeypatch, change):
    state, calls = github

    def mutate():
        if change in {"head", "remote"}:
            state[change] = "b" * 40
        elif change == "source":
            monkeypatch.setattr(rel, "source_digest", lambda: "changed")
        elif change == "local-asset":
            (bundle / rel.FRAME_ZIP).write_bytes(b"changed")
        elif change == "draft":
            state["release"]["target_commitish"] = "b" * 40
        else:
            state["release"]["assets"][0]["id"] += 100

    state["before_final_check" if change in {"draft", "asset"} else "after_upload"] = (
        mutate
    )
    with pytest.raises(rel.ReleaseError):
        rel.publish(VERSION)
    assert not any("PATCH" in c for c in calls)


@pytest.mark.parametrize("when", ["after_publish", "after_docs"])
@pytest.mark.parametrize(
    "change",
    [
        "latest",
        "tag",
        "draft",
        "prerelease",
        "target",
        "missing",
        "extra",
        "duplicate",
        "bytes",
        "replacement",
        "manifest",
    ],
)
def test_deploy_cannot_succeed_with_wrong_published_assets_or_tag(github, when, change):
    state, calls = github

    def mutate():
        remote = state["release"]
        if change == "latest":
            state["latest_id"] = 99
        elif change == "tag":
            state["tag_commit"] = "b" * 40
        elif change in {"draft", "prerelease"}:
            remote[change] = True
        elif change == "target":
            remote["target_commitish"] = "b" * 40
        elif change == "missing":
            remote["assets"].pop()
        elif change == "extra":
            remote["assets"].append(dict(remote["assets"][0], name="extra.zip"))
        elif change == "duplicate":
            remote["assets"].append(remote["assets"][0].copy())
        elif change == "replacement":
            remote["assets"][0]["id"] += 100
        else:
            asset = next(
                a
                for a in remote["assets"]
                if a["name"]
                == (rel.MANIFEST if change == "manifest" else rel.FRAME_ZIP)
            )
            asset["digest"] = "sha256:" + "0" * 64

    state[when] = mutate
    # Exercise the actual deploy entry point and its exit status, not just a helper.
    assert rel.main.__wrapped__(["deploy", "--version", VERSION]) == 1
    assert (("make", "docs-deploy") in calls) == (when == "after_docs")


@pytest.mark.parametrize("failure", ["published-read", "tag-read"])
def test_unreadable_published_state_never_reports_success_or_triggers_docs(
    github, failure
):
    state, calls = github
    state["failure"] = failure
    assert rel.main.__wrapped__(["deploy", "--version", VERSION]) == 1
    assert ("make", "docs-deploy") not in calls


def test_deploy_success_requires_published_assets_tag_and_docs(github):
    state, calls = github
    assert rel.main.__wrapped__(["deploy", "--version", VERSION]) == 0
    assert state["release"]["draft"] is False
    assert calls.count(("make", "docs-deploy")) == 1


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


@pytest.mark.parametrize("failed", [False, True])
def test_preparation_retains_child_logs_once_before_cleanup(
    preparation, tmp_path, monkeypatch, failed
):
    monkeypatch.setattr(scheme, "OUTPUT_DIR", tmp_path / "run")
    original_stage = rel._stage

    def logged_stage(output, *args):
        original_stage(output, *args)
        logs = output / "logs"
        logs.mkdir(exist_ok=True)
        stem = f"build-child-{len(preparation)}"
        (logs / f"{stem}.jsonl").write_text('{"event":"build_started"}\n')
        (logs / f"{stem}.crash.log").write_text("Native crash stack\n")
        if failed and args[0] == "build":
            raise subprocess.CalledProcessError(1, "build")

    monkeypatch.setattr(rel, "_stage", logged_stage)
    if failed:
        with (
            pytest.raises(SystemExit, match="1"),
            buildlog.command("Hardware release", name="release"),
        ):
            rel.prepare(VERSION)
    else:
        with buildlog.command("Hardware release", name="release"):
            rel.prepare(VERSION)
    assert all(not output.exists() for output, _ in preparation)
    parent_log = next((scheme.OUTPUT_DIR / "logs").glob("*.jsonl"))
    retained = parent_log.with_suffix(".children")
    assert len(list(retained.glob("*.jsonl"))) == len(preparation)
    for path in retained.glob("*.crash.log"):
        assert path.read_text() == "Native crash stack\n"
    events = [json.loads(line) for line in parent_log.read_text().splitlines()]
    assert len(
        [event for event in events if event["event"] == "diagnostic_saved"]
    ) == 2 * len(preparation)
    assert events[-1]["status"] == ("failed" if failed else "success")
