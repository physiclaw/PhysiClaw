"""Release artifacts and retries must fail closed on missing or different bytes."""

import hashlib
import io
import json
import subprocess
import tarfile
import urllib.error
import zipfile
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from scripts import check_package


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = b'[project]\nname = "physiclaw"\nversion = "1.2.3"\n'
    (tmp_path / "pyproject.toml").write_bytes(project)
    bundle = tmp_path / "bundle"
    dist = bundle / "dist"
    dist.mkdir(parents=True)
    with zipfile.ZipFile(dist / "physiclaw-1.2.3-py3-none-any.whl", "w") as archive:
        for resource in check_package.RESOURCES:
            archive.writestr("physiclaw/" + resource, "resource")
        archive.writestr("physiclaw-1.2.3.dist-info/licenses/LICENSE", "MIT")
        archive.writestr(
            "physiclaw-1.2.3.dist-info/METADATA", "Name: physiclaw\nVersion: 1.2.3\n"
        )
    with tarfile.open(dist / "physiclaw-1.2.3.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo("physiclaw-1.2.3/pyproject.toml")
        member.size = len(project)
        archive.addfile(member, io.BytesIO(project))
    monkeypatch.setattr(
        check_package.subprocess, "check_output", lambda *a, **kw: "a" * 40
    )
    check_package.record(bundle)
    return bundle


def test_manifest_records_both_files_version_and_commit(bundle):
    manifest = check_package.verify_local(bundle)
    assert manifest["version"] == "1.2.3"
    assert manifest["commit"] == "a" * 40
    assert set(manifest["files"]) == check_package.filenames("1.2.3")


@pytest.mark.parametrize("change", ["corrupt", "missing", "extra", "manifest"])
def test_changed_bundle_is_rejected(bundle, change):
    path = bundle / "dist" / "physiclaw-1.2.3.tar.gz"
    if change == "corrupt":
        path.write_bytes(b"different")
    elif change == "missing":
        path.unlink()
    elif change == "extra":
        (path.parent / "old.whl").write_bytes(b"stale")
    else:
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"]["../../unexpected"] = "0" * 64
        (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(
        ValueError, match="checksum mismatch|do not match|must contain exactly"
    ):
        check_package.verify_local(bundle)


def test_record_rejects_missing_runtime_resources(bundle):
    wheel = bundle / "dist" / "physiclaw-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("physiclaw/__init__.py", "")
    with pytest.raises(ValueError, match="resources or license"):
        check_package.record(bundle)


@pytest.fixture
def pypi(bundle, monkeypatch, mocker):
    data = {p.name: p.read_bytes() for p in (bundle / "dist").iterdir()}
    entries = {
        name: {
            "url": f"https://files.pythonhosted.org/{name}",
            "digests": {"sha256": hashlib.sha256(content).hexdigest()},
            "yanked": False,
        }
        for name, content in data.items()
    }
    monkeypatch.setattr(check_package, "published_files", lambda version: entries)
    monkeypatch.setattr(
        check_package.urllib.request,
        "urlopen",
        lambda url, **kw: io.BytesIO(data[url.rsplit("/", 1)[1]]),
    )
    monkeypatch.setattr(check_package.time, "sleep", lambda seconds: None)
    verifier = mocker.patch.object(check_package.subprocess, "run")
    return entries, data, verifier


def test_postpublication_checks_both_downloads_and_attestations(bundle, pypi):
    entries, _, verifier = pypi
    check_package.verify_pypi(bundle)
    assert verifier.call_count == 2
    assert {call.args[0][-1] for call in verifier.call_args_list} == {
        item["url"] for item in entries.values()
    }
    assert all(
        check_package.REPOSITORY in call.args[0] for call in verifier.call_args_list
    )


def test_partial_retry_verifies_existing_file_but_requires_complete_postcheck(
    bundle, pypi
):
    entries, _, verifier = pypi
    entries.pop("physiclaw-1.2.3.tar.gz")
    check_package.verify_pypi(bundle, allow_missing=True)
    assert verifier.call_count == 1
    with pytest.raises(ValueError, match="both release files"):
        check_package.verify_pypi(bundle)


def test_new_release_has_no_existing_files_to_skip(bundle, pypi):
    entries, _, verifier = pypi
    entries.clear()
    check_package.verify_pypi(bundle, allow_missing=True)
    verifier.assert_not_called()


@pytest.mark.parametrize(
    "change", ["checksum", "bytes", "yanked", "host", "unexpected"]
)
def test_retry_rejects_conflicting_remote_state(bundle, pypi, change):
    entries, data, _ = pypi
    name = next(iter(entries))
    if change == "checksum":
        entries[name]["digests"]["sha256"] = "0" * 64
    elif change == "bytes":
        data[name] = b"different download"
    elif change == "yanked":
        entries[name]["yanked"] = True
    elif change == "host":
        entries[name]["url"] = "https://example.invalid/file.whl"
    else:
        entries["unexpected.whl"] = entries[name]
    with pytest.raises(
        ValueError, match="checksum mismatch|differs or is yanked|Unexpected|unexpected"
    ):
        check_package.verify_pypi(bundle, allow_missing=True)


def test_retry_rejects_unverifiable_provenance(bundle, pypi):
    _, _, verifier = pypi
    verifier.side_effect = subprocess.CalledProcessError(1, ["pypi-attestations"])
    with pytest.raises(subprocess.CalledProcessError):
        check_package.verify_pypi(bundle, allow_missing=True)


@pytest.mark.parametrize("status", [404, 403, 500])
def test_only_not_found_is_treated_as_an_unpublished_version(monkeypatch, status):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("https://pypi.org", status, "error", {}, None)

    monkeypatch.setattr(check_package.urllib.request, "urlopen", fail)
    if status == 404:
        assert check_package.published_files("1.2.3") == {}
    else:
        with pytest.raises(urllib.error.HTTPError):
            check_package.published_files("1.2.3")


def test_release_permissions_and_artifact_promotion_are_isolated():
    workflows = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    yaml = YAML(typ="safe")
    release = yaml.load(workflows / "release.yml")
    ci = yaml.load(workflows / "ci.yml")
    package = yaml.load(workflows / "package.yml")
    jobs = release["jobs"]
    assert release["permissions"] == {"contents": "read"}
    assert release["concurrency"]["cancel-in-progress"] is False
    assert jobs["publish"]["permissions"] == {"id-token": "write"}
    assert jobs["publish"]["environment"]["name"] == "pypi"
    assert set(jobs["publish"]["needs"]) == {"ci", "prepublish"}
    assert "github.event_name == 'push'" in jobs["prepublish"]["if"]
    assert "workflow_call" in ci["on"]
    assert ci["jobs"]["package"]["uses"] == "./.github/workflows/package.yml"
    assert set(package["jobs"]["install"]["strategy"]["matrix"]["os"]) == {
        "ubuntu-latest",
        "macos-latest",
        "windows-latest",
    }
    # The privileged job cannot run project code or install dependencies.
    steps = jobs["publish"]["steps"]
    assert len(steps) == 2
    assert steps[0]["uses"].startswith("actions/download-artifact@")
    assert steps[0]["with"]["artifact-ids"] == "${{ needs.ci.outputs.artifact-id }}"
    assert steps[1]["uses"].startswith("pypa/gh-action-pypi-publish@")
    assert steps[1]["with"]["attestations"] is True
    assert jobs["github-release"]["needs"] == ["ci", "verify"]
