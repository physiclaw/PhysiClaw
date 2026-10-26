"""Native Windows import regression, also exercising EOF cleanup on other OSes.

Run: pytest -m slow tests/agent/runtime/test_managed_startup.py
"""

import os
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

import pytest


@pytest.mark.slow
def test_first_image_completes_before_control_pipe_eof(tmp_path):
    app = Path(__file__).with_name("managed_image_app.py").resolve()
    src = Path(__file__).resolve().parents[3] / "src"
    # Keep native imports cold: bypass PYTHONPATH, .pth files and sitecustomize.
    # Add the checkout and its dependencies explicitly for the -I -S child.
    paths = list(
        dict.fromkeys(
            [str(src), sysconfig.get_path("purelib"), sysconfig.get_path("platlib")]
        )
    )
    pywin32_setup = ""
    if sys.platform == "win32":
        # Restore pywin32.pth's paths and DLL setup without other preload hooks.
        paths.extend(
            str(Path(site_path) / subdir)
            for site_path in paths[1:]
            for subdir in ("win32", "win32/lib", "Pythonwin")
        )
        pywin32_setup = "import pywin32_bootstrap; "
    bootstrap = (
        f"import sys, runpy; sys.path[:0] = {paths!r}; "
        f"{pywin32_setup}runpy.run_path({str(app)!r}, run_name='__main__')"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() != "PHYSICLAW_PRELOAD_NUMPY"
    }
    env["PHYSICLAW_HOME"] = str(tmp_path / "home")
    output = tmp_path / "runtime.log"
    kwargs = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    with output.open("wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-u", "-c", bootstrap],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                text = output.read_text(encoding="utf-8")
                if "IMAGE_VALIDATED" in text:
                    break
                assert proc.poll() is None, text
                if time.monotonic() >= deadline:
                    pytest.fail(
                        f"First image did not complete with stdin open:\n{text}"
                    )
                time.sleep(0.02)
            assert proc.poll() is None, text
            assert "STOP_REQUESTED" not in text, text
            # Only successful image validation authorizes EOF. Waiting for the
            # marker with communicate() would close stdin and hide the freeze.
            assert proc.stdin is not None and not proc.stdin.closed
            proc.stdin.close()
            proc.wait(timeout=15)
            text = output.read_text(encoding="utf-8")
            assert proc.returncode == 0, text
            assert "STOP_REQUESTED" in text and "CLEAN_EXIT" in text, text
            assert text.index("IMAGE_VALIDATED") < text.index("STOP_REQUESTED")
            assert "Fatal Python error" not in text
        finally:
            # A failed native import can be stuck indefinitely. Reap even when
            # the assertion/deadline fails; never leave a test child behind.
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            if proc.stdin is not None:
                proc.stdin.close()
