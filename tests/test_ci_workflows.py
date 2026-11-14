"""CI coverage contracts: native parity, independent failures and diagnostics."""

import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from coverage import CoverageData
from ruamel.yaml import YAML

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_release_checkout_preserves_annotated_tags():
    job = YAML(typ="safe").load(WORKFLOWS / "release.yml")["jobs"]["validate"]
    checkout = next(
        s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")
    )
    # With the default tag+SHA inputs checkout force-fetches the peeled commit
    # into refs/tags/vX.Y.Z, destroying the annotation required by validation.
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["persist-credentials"] is False


def test_release_and_local_uv_versions_cannot_drift():
    project = tomllib.loads((WORKFLOWS.parents[1] / "pyproject.toml").read_text())
    required = project["tool"]["uv"]["required-version"]
    assert required.startswith("==")
    for filename in ("ci.yml", "package.yml", "release.yml"):
        workflow = YAML(typ="safe").load(WORKFLOWS / filename)
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if step.get("uses", "").startswith("astral-sh/setup-uv@"):
                    assert "==" + step["with"]["version"] == required


def test_publication_requires_validation_and_verified_artifacts():
    jobs = YAML(typ="safe").load(WORKFLOWS / "release.yml")["jobs"]
    assert jobs["ci"]["needs"] == "validate"
    assert jobs["prepublish"]["needs"] == "ci"
    assert "github.event_name == 'push'" in jobs["prepublish"]["if"]
    publish = jobs["publish"]
    assert set(publish["needs"]) == {"ci", "prepublish"}
    assert publish["environment"]["name"] == "pypi"
    assert publish["permissions"] == {"id-token": "write"}
    assert all(
        "run" not in step and "checkout@" not in step.get("uses", "")
        for step in publish["steps"]
    )
    assert set(jobs["verify"]["needs"]) == {"ci", "publish"}
    assert set(jobs["github-release"]["needs"]) == {"ci", "verify"}


@pytest.fixture(
    params=[("ci.yml", "test", "bootstrap"), ("package.yml", "install", "bundle")]
)
def native_job(request):
    filename, job, ready = request.param
    return YAML(typ="safe").load(WORKFLOWS / filename)["jobs"][job], ready


def test_native_jobs_cover_all_os_and_python_combinations(native_job):
    job, _ = native_job
    assert job["strategy"] == {
        "fail-fast": False,
        "matrix": {
            "os": ["ubuntu-latest", "macos-latest", "windows-latest"],
            "python": ["3.12", "3.13", "3.14"],
        },
    }
    assert job["runs-on"] == "${{ matrix.os }}"
    assert not job.get("continue-on-error", False)
    assert "if" not in job


def test_required_checks_run_after_test_failures_but_require_setup(native_job):
    job, ready = native_job
    required = (
        {
            "make test-cov",
            "make test-slow",
            "make test-integration",
            'make test-encoding-0 "PY=uv run --locked"',
            'make test-encoding-1 "PY=uv run --locked"',
            "uv run --locked pytest -m slow tests/cli/test_fresh_start.py",
            "uv run --locked mypy",
            "uv run --locked python -m hardware check",
            'make hw-build ARGS="--stems fastener_10_BHCS --bom"',
            "uv run --locked coverage combine --keep .test-results",
            "uv run --locked coverage report",
            "uv run --locked coverage html",
        }
        if ready == "bootstrap"
        else {
            "uv run --no-project python scripts/check_package.py smoke",
            "uv run --no-project python scripts/check_package.py smoke-installer --log-dir .test-results/installer",
            "uv run --no-project python scripts/check_package.py smoke-installer --upgrade --log-dir .test-results/upgrade",
        }
    )
    steps = job["steps"]
    setup = next(i for i, step in enumerate(steps) if step.get("id") == ready)
    if ready == "bootstrap":
        # On PowerShell a later successful native command can mask a failed
        # Make invocation. The bootstrap must be a single command/exit status.
        assert steps[setup]["run"] == "make test-bootstrap"
    checks = {step["run"]: (i, step) for i, step in enumerate(steps) if "run" in step}
    assert required <= checks.keys()
    for command in required:
        index, step = checks[command]
        assert index > setup
        # A status function overrides GitHub's implicit success() guard. The
        # setup outcome still gates execution; no OS or Python exclusions.
        assert (
            step["if"]
            == f"${{{{ !cancelled() && steps.{ready}.outcome == 'success' }}}}"
        )
        assert not step.get("continue-on-error", False)


def test_each_pytest_suite_keeps_its_own_report_and_temporary_logs():
    job = YAML(typ="safe").load(WORKFLOWS / "ci.yml")["jobs"]["test"]
    reports, temporary = set(), set()
    for step in job["steps"]:
        options = step.get("env", {}).get("PYTEST_ADDOPTS")
        if not options:
            continue
        args = dict(arg[2:].split("=", 1) for arg in shlex.split(options) if "=" in arg)
        report, tmp = Path(args["junitxml"]), Path(args["basetemp"])
        assert report.parent == tmp.parent == Path(".test-results")
        assert report not in reports and tmp not in temporary
        assert int(args["durations"]) > 0
        reports.add(report)
        temporary.add(tmp)
    assert len(reports) == 6  # fast, slow, integration, fresh CLI, encoding off/on
    upload = next(
        s
        for s in job["steps"]
        if s.get("uses", "").startswith("actions/upload-artifact@")
        and s["with"]["name"].startswith("tests-")
    )
    assert upload["if"] == "${{ always() }}"
    assert upload["with"]["include-hidden-files"] is True
    assert ".test-results/*.xml" in upload["with"]["path"]
    assert ".test-results/subprocess-logs.zip" in upload["with"]["path"]
    collect = next(
        step
        for step in job["steps"]
        if step.get("run") == "uv run --no-project python scripts/collect_test_logs.py"
    )
    assert collect["if"] == "${{ always() }}"
    assert job["steps"].index(collect) < job["steps"].index(upload)
    assert "${{ matrix.os }}" in upload["with"]["name"]
    assert "${{ matrix.python }}" in upload["with"]["name"]


def test_coverage_keeps_suite_and_platform_data_separate_until_combining():
    jobs = YAML(typ="safe").load(WORKFLOWS / "ci.yml")["jobs"]
    suites = {
        step["run"]: step["env"]
        for step in jobs["test"]["steps"]
        if "COVERAGE_FILE" in step.get("env", {})
    }
    assert set(suites) == {"make test-cov", "make test-slow", "make test-integration"}
    assert len({env["COVERAGE_FILE"] for env in suites.values()}) == 3
    for command in ("make test-slow", "make test-integration"):
        assert "--cov" in shlex.split(suites[command]["PYTEST_ADDOPTS"])
    combined = jobs["coverage"]
    assert combined["needs"] == "test"
    download = next(
        step
        for step in combined["steps"]
        if step.get("uses", "").startswith("actions/download-artifact@")
    )
    assert not download["with"].get("merge-multiple", False)
    assert download["with"]["pattern"] == "coverage-*-${{ github.run_attempt }}"


@pytest.mark.slow
def test_coverage_combination_reads_every_downloaded_platform_file(tmp_path):
    jobs = YAML(typ="safe").load(WORKFLOWS / "ci.yml")["jobs"]
    command = next(
        step["run"]
        for step in jobs["coverage"]["steps"]
        if "coverage combine" in step.get("run", "")
    )
    linux = tmp_path / "coverage-data" / "coverage-linux" / ".coverage"
    windows = tmp_path / "coverage-data" / "coverage-windows" / ".coverage"
    linux.parent.mkdir(parents=True)
    windows.parent.mkdir(parents=True)
    first, second = CoverageData(basename=linux), CoverageData(basename=windows)
    first.add_arcs({"src/physiclaw/example.py": [(1, 2)]})
    second.add_arcs({"src/physiclaw/example.py": [(1, 3)]})
    first.write()
    second.write()
    args = shlex.split(command)
    args = args[args.index("combine") :]
    # Expand the Linux job's glob without requiring a shell on Windows.
    inputs = sorted(tmp_path.glob(args.pop()))
    # The synthetic merge must not overwrite an enclosing pytest-cov report.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("COVERAGE_")
    }

    result = subprocess.run(
        [sys.executable, "-m", "coverage", *args, *map(str, inputs)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    combined = CoverageData(basename=tmp_path / ".coverage")
    combined.read()
    assert combined.arcs("src/physiclaw/example.py") == [(1, 2), (1, 3)]
    assert linux.exists() and windows.exists()
