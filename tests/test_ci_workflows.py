"""CI coverage contracts: native parity, independent failures and diagnostics."""

import shlex
from pathlib import Path

import pytest
from ruamel.yaml import YAML

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


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
        args = dict(arg[2:].split("=", 1) for arg in shlex.split(options))
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
    )
    assert upload["if"] == "${{ always() }}"
    assert upload["with"]["include-hidden-files"] is True
    assert ".test-results/*.xml" in upload["with"]["path"]
    assert ".test-results/**/*.log" in upload["with"]["path"]
    assert "${{ matrix.os }}" in upload["with"]["name"]
    assert "${{ matrix.python }}" in upload["with"]["name"]
