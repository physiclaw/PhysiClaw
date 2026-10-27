"""Native editor launch regression: exercise CreateProcess on Windows CI."""

import json
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
