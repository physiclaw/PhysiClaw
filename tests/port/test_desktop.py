"""Native editor launch regression: exercise CreateProcess on Windows CI."""

import json
import os
import subprocess
import sys

import pytest

from physiclaw.port import desktop


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows CreateProcess")
def test_windows_editor_receives_arguments_and_filename_without_a_shell(tmp_path):
    script = tmp_path / "record arguments.py"
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[-1]).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    target = tmp_path / "config with spaces & symbols.toml"
    arguments = ["hello world", "C:\\trailing\\", 'a"b', "&", "%PATH%", ""]
    editor = subprocess.list2cmdline([sys.executable, str(script), *arguments])

    desktop.edit_file(target, editor=editor)

    assert json.loads(target.read_text(encoding="utf-8")) == [*arguments, str(target)]


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows batch launchers")
@pytest.mark.parametrize("absolute", [False, True])
def test_windows_batch_editor_preserves_unicode_and_literal_filename(
    tmp_path, monkeypatch, absolute
):
    bin_dir = tmp_path / "编辑器 & %PHYSICLAW_EDITOR_EXPAND% bin"
    bin_dir.mkdir()
    script = bin_dir / "record arguments.py"
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[-1]).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    wrapper = bin_dir / "audit-editor.cmd"
    wrapper.write_text(
        '@echo off\n@"%PHYSICLAW_TEST_PYTHON%" "%~dp0record arguments.py" %*\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PHYSICLAW_TEST_PYTHON", sys.executable)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PHYSICLAW_EDITOR_EXPAND", "should-not-expand")
    target = tmp_path / "配置 %PHYSICLAW_EDITOR_EXPAND% !literal! & symbols.toml"
    prefix = f'"{wrapper}"' if absolute else "audit-editor"

    desktop.edit_file(target, editor=f'{prefix} --wait "argument with spaces"')

    assert json.loads(target.read_text(encoding="utf-8")) == [
        "--wait",
        "argument with spaces",
        str(target),
    ]
