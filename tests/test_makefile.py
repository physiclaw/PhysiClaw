"""Execute Make recipes with a recording runner, never their external effects."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.slow  # Native make/Python startup is not a unit-test budget.

ENCODING_TESTS = [
    "tests/test_text_encoding.py",
    "tests/common/test_text.py",
    "tests/scripts/test_eval_context.py",
    "tests/scripts/test_loc.py",
    "tests/cli/test_session_debug.py",
    "tests/hardware/documents/sourcing/test_urls.py",
]
CAD = ["--locked", "--group", "cad", "python", "-m", "hardware"]
DOC = ["--locked", "python", "-m", "hardware"]
ROUTES = [
    ("test", [], [["pytest"]]),
    ("test-fast", [], [["pytest"]]),
    (
        "test-cov",
        [],
        [
            [
                "pytest",
                "--cov=src/physiclaw",
                "--cov-report=term-missing",
                "--cov-report=html",
                "--cov-branch",
            ]
        ],
    ),
    ("test-slow", [], [["pytest", "-m", "slow"]]),
    ("test-integration", [], [["pytest", "-m", "integration"]]),
    ("test-all", [], [["pytest", "-m", ""]]),
    (
        "test-encoding",
        [],
        [
            [
                "-X",
                "utf8=0",
                "-X",
                "warn_default_encoding",
                "-W",
                "error::EncodingWarning",
                "-m",
                "pytest",
                *ENCODING_TESTS,
            ],
            [
                "-X",
                "utf8=1",
                "-X",
                "warn_default_encoding",
                "-W",
                "error::EncodingWarning",
                "-m",
                "pytest",
                *ENCODING_TESTS,
            ],
        ],
    ),
    (
        "mutate",
        ["MOD=src/has space.py"],
        [["mutmut", "run", "--paths-to-mutate", "src/has space.py"]],
    ),
    (
        "fmt",
        [],
        [["ruff", "format", "src/", "tests/", "hardware/", "firmware/", "scripts/"]],
    ),
    ("typecheck", [], [["mypy"]]),
    ("loc", [], [["scripts/loc.py"]]),
    (
        "firmware",
        ['ARGS=--tag test --out "bundle path.zip"'],
        [["firmware/firmware.py", "--tag", "test", "--out", "bundle path.zip"]],
    ),
    ("hw-parts", [], [[*CAD, "parts"]]),
    ("hw-build", ["ARGS=--bom"], [[*CAD, "build", "--bom"]]),
    ("hw-check", ["ARGS=--help"], [[*DOC, "check", "--help"]]),
    ("hw-step", ["ARGS=procedure_name"], [[*CAD, "step", "procedure_name"]]),
    ("hw-print", [], [[*CAD, "print"]]),
    ("hw-manual", [], [[*DOC, "manual"]]),
    ("hw-manual-pdf", ["ARGS=--lang en"], [[*DOC, "manual", "--pdf", "--lang", "en"]]),
    ("hw-sourcing", [], [[*DOC, "sourcing"]]),
    ("hw-url", [], [[*DOC, "url"]]),
    ("hw-drawing", [], [[*DOC, "drawing", "--pdf"]]),
    ("hw-mark", ['ARGS="drawing path.svg"'], [[*CAD, "mark", "drawing path.svg"]]),
    ("hw-replay", ['ARGS="patch path.json"'], [[*CAD, "replay", "patch path.json"]]),
    ("hw-camera", [], [[*CAD, "camera"]]),
    ("hw-rebuild", ["ARGS=--bom"], [[*CAD, "build", "--no-cache", "--bom"]]),
    ("hw-preflight", [], [[*DOC, "release", "preflight"]]),
    ("hw-prepare", [], [[*CAD, "release", "prepare"]]),
    ("hw-verify", [], [[*DOC, "release", "verify"]]),
    ("hw-publish", [], [[*DOC, "release", "publish"]]),
    ("hw-release", [], [[*CAD, "release", "deploy"]]),
    ("_hw-package", [], [[*DOC, "release", "publish"]]),
]


@pytest.fixture
def make_project(tmp_path, request):
    project = tmp_path / getattr(request, "param", "make checkout")
    project.mkdir()
    shutil.copyfile(ROOT / "Makefile", project / "Makefile")
    capture = project / "capture runner.py"
    capture.write_text(
        "import json, os, sys\n"
        "keys = ('VERSION', 'VERBOSE', 'RELEASE_FLOW', 'HW_VERSION')\n"
        "record = {'argv': sys.argv[1:], 'env': {k: os.environ.get(k) for k in keys}}\n"
        "print('MAKE_CAPTURE=' + json.dumps(record), flush=True)\n"
        "raise SystemExit(int(os.environ.get('MAKE_CAPTURE_EXIT', '0')))\n",
        encoding="utf-8",
    )
    runner = f'"{Path(sys.executable).as_posix()}" "{capture.as_posix()}"'
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PHYSICLAW_LIVE_")
        and key
        not in {
            "MAKEFLAGS",
            "MFLAGS",
            "MAKELEVEL",
            "MAKEFILES",
            "VERSION",
            "VERBOSE",
            "RELEASE_FLOW",
            "HW_VERSION",
            "UV_PUBLISH_TOKEN",
            "DOCS_DEPLOY_SCRIPT",
        }
    }
    env["PYTHONIOENCODING"] = "utf-8"
    env["MAKE_PATH_CANARY"] = "unexpected expansion"

    def invoke(*arguments, exit_code=0, extra_env=None):
        command = [
            "make",
            "--no-print-directory",
            "-j4",
            f"PY={runner}",
            f"PYTHON={runner}",
            *arguments,
        ]
        if os.name == "nt":
            command.append("SHELL=cmd.exe")
        result = subprocess.run(
            command,
            cwd=project,
            env={**env, "MAKE_CAPTURE_EXIT": str(exit_code), **(extra_env or {})},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        records = [
            json.loads(line.removeprefix("MAKE_CAPTURE="))
            for line in result.stdout.splitlines()
            if line.startswith("MAKE_CAPTURE=")
        ]
        return result, records

    return project, invoke


@pytest.mark.parametrize(
    "target, options, expected", ROUTES, ids=[row[0] for row in ROUTES]
)
def test_make_target_forwards_exact_arguments(make_project, target, options, expected):
    _, invoke = make_project

    result, records = invoke(target, *options)

    assert result.returncode == 0, result.stdout + result.stderr
    assert [record["argv"] for record in records] == expected


@pytest.mark.parametrize("target", ["bump", "build", "publish", "release"])
def test_make_release_uses_one_guarded_driver_and_environment_data(
    make_project, target
):
    _, invoke = make_project
    version = "1.2.3 & literal-data"

    result, records = invoke(
        target, f"VERSION={version}", "VERBOSE=1", "RELEASE_FLOW=1"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert [record["argv"] for record in records] == [
        ["--no-project", "python", "scripts/release.py", target]
    ]
    assert records[0]["env"] == {
        "VERSION": version,
        "VERBOSE": "1",
        "RELEASE_FLOW": "1",
        "HW_VERSION": "",
    }


def test_make_hardware_release_passes_version_as_environment_data(make_project):
    _, invoke = make_project
    version = "1.2 & literal-data"

    result, records = invoke("hw-publish", f"HW_VERSION={version}")

    assert result.returncode == 0, result.stdout + result.stderr
    assert records[0]["argv"] == [*DOC, "release", "publish"]
    assert records[0]["env"]["HW_VERSION"] == version


@pytest.mark.parametrize("external_makefile", [False, True])
@pytest.mark.parametrize(
    "make_project",
    ["make checkout", "make checkout %MAKE_PATH_CANARY% $MAKE_PATH_CANARY & literal"],
    indirect=True,
    ids=["spaces", "shell-syntax"],
)
def test_make_docs_deploy_uses_cwd_or_explicit_driver_path(
    make_project, external_makefile
):
    project, invoke = make_project
    (project / "scripts").mkdir()
    shutil.copyfile(
        project / "capture runner.py", project / "scripts" / "docs_deploy.py"
    )
    settings = project.parent / "settings.mk"
    settings.write_text(
        "# An earlier makefile must not become part of the path.\n", encoding="utf-8"
    )
    options = {
        False: (),
        True: (
            "-C",
            str(project.parent),
            "-f",
            str(settings),
            "-f",
            str(project / "Makefile"),
        ),
    }[external_makefile]
    extra_env = (
        {"DOCS_DEPLOY_SCRIPT": str(project / "scripts" / "docs_deploy.py")}
        if external_makefile
        else {}
    )

    result, records = invoke(
        "docs-deploy",
        *options,
        f'PYTHON="{Path(sys.executable).as_posix()}"',
        extra_env=extra_env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert [record["argv"] for record in records] == [[]]


@pytest.mark.parametrize("source", ["multiple-files", "include"])
def test_make_docs_deploy_accepts_other_makefiles_from_checkout(make_project, source):
    project, invoke = make_project
    (project / "scripts").mkdir()
    shutil.copyfile(
        project / "capture runner.py", project / "scripts" / "docs_deploy.py"
    )
    (project / "settings.mk").write_text("# Local build settings.\n", encoding="utf-8")
    (project / "wrapper.mk").write_text(
        "include settings.mk\ninclude Makefile\n", encoding="utf-8"
    )
    options = {
        "multiple-files": ("-f", "settings.mk", "-f", "Makefile"),
        "include": ("-f", "wrapper.mk"),
    }[source]

    result, records = invoke(
        "docs-deploy", *options, f'PYTHON="{Path(sys.executable).as_posix()}"'
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert [record["argv"] for record in records] == [[]]


def test_make_docs_deploy_missing_driver_fails_without_running_worker(make_project):
    _, invoke = make_project

    result, records = invoke(
        "docs-deploy",
        f'PYTHON="{Path(sys.executable).as_posix()}"',
        extra_env={"DOCS_DEPLOY_SCRIPT": "missing_docs.py"},
    )

    assert result.returncode != 0
    assert "missing_docs.py" in result.stderr
    assert records == []


def test_make_lint_checks_scripts_and_formats_the_full_source_scope(make_project):
    _, invoke = make_project

    result, records = invoke("lint")

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(records) == 2
    assert records[0]["argv"][:2] == ["ruff", "check"]
    assert "scripts/" in records[0]["argv"]
    assert records[1]["argv"] == [
        "ruff",
        "format",
        "--check",
        "src/",
        "tests/",
        "hardware/",
        "firmware/",
        "scripts/",
    ]


@pytest.mark.parametrize(
    "target, variable", [("mutate", "MOD"), ("hw-step", "ARGS"), ("hw-mark", "ARGS")]
)
@pytest.mark.parametrize("value", ["", "   "])
def test_make_missing_required_argument_fails_before_running_anything(
    make_project, target, variable, value
):
    _, invoke = make_project

    result, records = invoke(target, f"{variable}={value}")

    assert result.returncode != 0
    assert f"usage: make {target} {variable}=" in result.stdout + result.stderr
    assert records == []


@pytest.mark.parametrize(
    "target", ["test-encoding", "lint", "release", "hw-release", "docs-deploy"]
)
def test_make_failure_stops_recipe_and_propagates_nonzero_result(make_project, target):
    _, invoke = make_project

    result, records = invoke(target, exit_code=7)

    assert result.returncode != 0
    assert len(records) == 1
    assert "Error 7" in result.stderr


@pytest.mark.parametrize("target", [None, "help", "hw-help"])
def test_make_help_and_default_do_not_run_a_worker(make_project, target):
    _, invoke = make_project
    arguments = () if target is None else (target,)

    result, records = invoke(*arguments)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "hw-build" in result.stdout and "hw-release" in result.stdout
    assert records == []


def test_make_studio_ui_routes_all_browser_regression_files(make_project):
    _, invoke = make_project

    result, records = invoke("-n", "test-studio-ui")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        result.stdout.strip()
        == "node --test tests/studio/test_studio_ui.cjs tests/agent/test_viewer_ui.cjs tests/core/test_setup_hardware_ui.cjs tests/cli/test_assistive_touch_ui.cjs"
    )
    assert records == []


def test_every_make_target_has_a_routing_or_help_check():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    declared = {
        name
        for targets in re.findall(r"^([\w-]+(?: [\w-]+)*):", makefile, re.MULTILINE)
        for name in targets.split()
    }

    covered = {row[0] for row in ROUTES} | {
        "bump",
        "build",
        "publish",
        "release",
        "docs-deploy",
        "lint",
        "help",
        "hw-help",
        "test-studio-ui",
    }

    assert declared == covered
