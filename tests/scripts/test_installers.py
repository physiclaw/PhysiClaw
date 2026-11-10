"""Installer regressions run with fake tools and never install or download."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer")
def test_bash_installer_uses_the_configured_uv_tool_directory(tmp_path):
    wrapper = tmp_path / "installer-test.sh"
    wrapper.write_text(
        """\
uv() { return 0; }
physiclaw() {
  if [[ "${PATH%%:*}" != "$UV_TOOL_BIN_DIR" ]]; then
    echo 'wrong tool directory' >&2
    return 1
  fi
  if [[ "$1" == --version ]]; then
    echo 'EXPECTED_INSTALLED_CLI'
  else
    echo 'EXPECTED_MODEL_SETUP'
  fi
}
export -f uv physiclaw
bash "$1" --version 0.11.0
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(wrapper), str(REPO_ROOT / "install.sh")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "UV_TOOL_BIN_DIR": str(tmp_path / "tools with spaces"),
            "NO_COLOR": "1",
            "PHYSICLAW_DRY_RUN": "0",
        },
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EXPECTED_INSTALLED_CLI" in result.stdout
    assert "EXPECTED_MODEL_SETUP" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer")
@pytest.mark.parametrize(
    "args", [["--version"], ["--version="], ["--version", "--dry-run"]]
)
def test_bash_version_option_requires_a_value(args):
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "install.sh"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )

    assert result.returncode == 2
    assert "--version requires a version" in result.stderr
    assert "Installing" not in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell installer")
@pytest.mark.parametrize(
    "install_exit,version_exit,model_exit",
    [(7, 0, 0), (0, 9, 0), (0, 0, 7), (0, 0, 0)],
)
def test_powershell_install_failure_cannot_be_hidden_by_an_old_cli(
    tmp_path, install_exit, version_exit, model_exit
):
    # Functions shadow external tools in the child shell. A working old CLI
    # must not turn a failed installation into success. A failed version check
    # must report its own failure; model setup remains optional after verification.
    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell is not None
    installer = str(REPO_ROOT / "install.ps1").replace("'", "''")
    wrapper = tmp_path / "installer-test.ps1"
    wrapper.write_text(
        f"""
function global:uv {{
    if ($args[0] -eq 'python') {{ $global:LASTEXITCODE = 0; return }}
    $global:LASTEXITCODE = {install_exit}
}}
function global:physiclaw {{
    if (($env:PATH -split ';')[0] -ne $env:UV_TOOL_BIN_DIR) {{
        throw 'Wrong tool directory'
    }}
    if ($args[0] -eq '--version') {{
        Write-Output '{"VERSION_CHECK_FAILED" if version_exit else "physiclaw 0.8.4"}'
        $global:LASTEXITCODE = {version_exit}
    }} else {{
        Write-Output 'MODEL_SETUP_CALLED'
        $global:LASTEXITCODE = {model_exit}
    }}
}}
& '{installer}' -Version 0.10.0
exit $global:LASTEXITCODE
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            **os.environ,
            "UV_TOOL_BIN_DIR": str(tmp_path / "tools with spaces"),
            "NO_COLOR": "1",
            "PHYSICLAW_DRY_RUN": "0",
        },
        timeout=30,
    )

    if install_exit:
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Installation failed" in result.stdout
        assert "MODEL_SETUP_CALLED" not in result.stdout
        assert "Done." not in result.stdout
    elif version_exit:
        assert result.returncode == 1, result.stdout + result.stderr
        assert "uv installed physiclaw==0.10.0" in result.stdout
        assert "could not be verified" in result.stdout
        assert f"Exit code: {version_exit}" in result.stdout
        assert "VERSION_CHECK_FAILED" in result.stdout
        assert "failed (exit 0)" not in result.stdout
        assert "MODEL_SETUP_CALLED" not in result.stdout
        assert "Done." not in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "MODEL_SETUP_CALLED" in result.stdout
        assert "Done." in result.stdout
