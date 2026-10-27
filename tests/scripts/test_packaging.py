"""Build a real sdist and rebuild its wheel without walking local artifacts."""

import os
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

from hatchling.build import build_sdist, build_wheel

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_sdist_skips_local_artifacts_and_rebuilds_wheel(tmp_path, monkeypatch):
    project = tmp_path / "package checkout"
    project.mkdir()
    source_files = (
        "pyproject.toml",
        ".gitignore",
        "README.md",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "licenses/GPL-3.0.txt",
        "src/physiclaw/__init__.py",
        "src/physiclaw/agent/context/AGENT.md",
    )
    for name in source_files:
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO_ROOT / name).read_bytes())

    excluded = (
        ".test-tmp",
        ".uv-cache",
        ".uv-python",
        ".hypothesis",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "hardware/output",
        "data",
        "dist",
    )
    for name in excluded:
        directory = project / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "local-only.txt").write_text("must not ship", encoding="utf-8")

    monkeypatch.chdir(project)
    output = tmp_path / "artifacts"
    output.mkdir()
    blocked = {project / name for name in excluded}
    scandir = os.scandir

    def checked_scandir(path):
        # Even visiting an ignored tree can hit broken/overlong Windows paths.
        assert Path(path) not in blocked, f"Build traversed local artifacts: {path}"
        return scandir(path)

    with monkeypatch.context() as guard:
        guard.setattr(os, "scandir", checked_scandir)
        sdist = output / build_sdist(str(output))

    with (project / "pyproject.toml").open("rb") as source:
        version = tomllib.load(source)["project"]["version"]
    root = f"physiclaw-{version}"
    unpacked = tmp_path / "unpacked"
    with tarfile.open(sdist) as archive:
        members = {name.removeprefix(f"{root}/") for name in archive.getnames()}
        assert set(source_files) <= members
        assert not any(
            name == prefix or name.startswith(f"{prefix}/")
            for name in members
            for prefix in excluded
        )
        archive.extractall(unpacked, filter="data")

    # The wheel must build from only the published sources, outside the checkout.
    monkeypatch.chdir(unpacked / root)
    wheel = output / build_wheel(str(output))
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
        assert "physiclaw/__init__.py" in members
        assert "physiclaw/agent/context/AGENT.md" in members
        metadata_root = f"physiclaw-{version}.dist-info"
        assert f"{metadata_root}/licenses/LICENSE" in members
        metadata = BytesParser().parsebytes(archive.read(f"{metadata_root}/METADATA"))
        assert metadata["Name"] == "physiclaw"
        assert metadata["Version"] == version
        entry_points = archive.read(f"{metadata_root}/entry_points.txt").decode("utf-8")
        assert "physiclaw = physiclaw.cli:app" in entry_points
        assert not any("local-only.txt" in name for name in members)
