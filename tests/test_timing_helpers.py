"""The test clock must preserve cancellation semantics and isolate watchdogs."""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.timing_helpers import PROCESS_TIMEOUT, ManualTimeouts, wait_for


@pytest.mark.slow
def test_regression_frozen_application_clock_keeps_pytest_reports_monotonic(tmp_path):
    root = Path(__file__).resolve().parents[1]
    probe = tmp_path / "test_report_clock.py"
    shutil.copyfile(root / "tests/data/timing/report_clock.py", probe)
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    report = tmp_path / "report.xml"
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(root),
        "PYTEST_ADDOPTS": "",
        "PYTEST_PLUGINS": "",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }

    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "-c",
            str(config),
            "--durations=0",
            "--durations-min=0",
            f"--junitxml={report}",
            str(probe),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=PROCESS_TIMEOUT,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stdout + result.stderr
    durations = tuple(
        map(
            float,
            re.findall(
                r"^([\d.]+)s (?:setup|call|teardown)\s",
                result.stdout,
                re.MULTILINE,
            ),
        )
    )
    assert len(durations) == 3, result.stdout
    # Bound reports by the enclosing process lifetime, allowing the CLI's
    # centisecond rounding. This is not a performance target.
    reported_limit = elapsed + 0.01
    assert 0 <= min(durations) <= max(durations) <= reported_limit, result.stdout
    case = ET.parse(report).find(".//testcase")
    assert case is not None
    assert 0 <= float(case.attrib["time"]) <= reported_limit


async def test_expiring_inner_scope_cancels_its_work_without_expiring_outer():
    controlled = ManualTimeouts(now=100.0)
    cancelled = []

    async def work():
        controlled.expire()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    async with controlled.timeout_at(110.0) as outer:
        with pytest.raises(TimeoutError):
            await wait_for(controlled.wait_for(work(), timeout=2.0))
        assert not outer.expired()

    assert cancelled == [True]
    assert controlled.scopes[1].expired()
    assert controlled.delays == [10.0, 2.0]


async def test_advancing_application_clock_leaves_real_scheduling_clock_alone():
    real_loop = asyncio.get_running_loop()
    controlled = ManualTimeouts(now=100.0)
    scheduled = real_loop.create_future()
    real_loop.call_soon(scheduled.set_result, "scheduled")

    async with controlled.timeout_at(101.0) as scope:
        controlled.now = 100000.0
        assert await wait_for(scheduled) == "scheduled"
        assert controlled.get_running_loop().time() == 100000.0
        assert asyncio.get_running_loop() is real_loop
        # Expiry is explicit; advancing an application clock must not starve
        # real event-loop callbacks or unexpectedly cancel the test runner.
        assert not scope.expired()
