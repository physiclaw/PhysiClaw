"""Check dryrun CLI isolation and worker cleanup."""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
from typer.testing import CliRunner

from physiclaw.cli import app
from physiclaw.cli.dryrun import _run_worker
from physiclaw.common import paths
from physiclaw.dryrun import KEY


@pytest.mark.parametrize("worker_exit", [0, 1])
def test_dryrun_worker_owns_config_and_logs(
    physiclaw_home, tmp_path, monkeypatch, worker_exit
):
    config = physiclaw_home / "config.toml"
    config.write_text("broken live config", encoding="utf-8")
    model = paths.omniparser_onnx()
    model.parent.mkdir(parents=True)
    model.write_bytes(b"local model")
    worker_home = tmp_path / "dryrun"
    worker_home.mkdir()
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **_: str(worker_home))
    monkeypatch.setenv("OPENAI_API_KEY", "live-test-key")
    monkeypatch.setenv("PHYSICLAW_SERVER", "http://127.0.0.1:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://example.invalid")

    def spawn(cmd, *, env, **_):
        assert Path(env["PHYSICLAW_HOME"]) == worker_home.resolve()
        assert env["OPENAI_API_KEY"] == KEY
        assert "PHYSICLAW_SERVER" not in env
        assert "HTTPS_PROXY" not in env
        assert (
            worker_home / model.relative_to(physiclaw_home)
        ).read_bytes() == b"local model"
        if worker_exit:
            (worker_home / "failure.json").write_text(
                json.dumps("Virtual touch missed"), encoding="utf-8"
            )
        worker = MagicMock()
        worker.__enter__.return_value = worker
        worker.wait.return_value = worker_exit
        return worker

    monkeypatch.setattr(subprocess, "Popen", spawn)

    result = CliRunner().invoke(app, ["dryrun"])

    assert result.exit_code == worker_exit
    assert config.read_text(encoding="utf-8") == "broken live config"
    assert not (physiclaw_home / "log").exists()
    if worker_exit:
        assert "Virtual touch missed" in result.stderr
        assert str(worker_home.resolve()) in result.stderr
        assert worker_home.exists()
    else:
        assert "chat reply" in result.stdout
        assert "schedule re-armed" in result.stdout
        assert not worker_home.exists()


def test_interrupt_allows_worker_to_close_its_trace(tmp_path, mocker):
    worker = mocker.patch.object(
        subprocess, "Popen"
    ).return_value.__enter__.return_value
    worker.wait.side_effect = [KeyboardInterrupt, 130]

    assert _run_worker(tmp_path, verbose=False) == 130
    assert worker.wait.call_args_list == [call(), call(timeout=20)]
    worker.terminate.assert_not_called()
