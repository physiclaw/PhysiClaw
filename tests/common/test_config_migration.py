"""Old full configs become sparse automatically, without losing user settings."""

import os
import re
import subprocess
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields
from pathlib import Path

import pytest
from freezegun import freeze_time
from symlink_helpers import symlink_to

from physiclaw.common import config


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    monkeypatch.setattr(config, "CONFIG", config.Config())
    monkeypatch.setattr(config, "_UPGRADE_WARNINGS", set())


@pytest.fixture
def legacy_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "# My rig — keep this comment\n"
        "[server]\nport = 9123 # custom port\n"
        '[agent]\nmodel = "moonshot/my-model"\n'
        "[update]\nauto = false # stay offline\n"
        '[provider]\nkimi_api_key = "secret-legacy-key" # private\n'
        'name = "moonshot"\n'
        "[memory]\ndaily_lookback_days = 7\n"
        "[pitfalls]\ncapture_enabled = true\ncurate_enabled = false\n"
        "max_items = 12\n"
        "[claude]\ntimeout_seconds = 180\n"
        '[providers.custom]\nbase_url = "https://example.test/v1"\n'
        'extra = "preserve me"\n',
        encoding="utf-8",
    )
    return path


def test_cli_writes_only_explicit_overrides(tmp_path):
    path = tmp_path / "config.toml"
    config.set_dotted("server.port", "9000", path)
    assert tomllib.loads(path.read_text()) == {"server": {"port": 9000}}
    assert not list((tmp_path / "backups").glob("*.bak"))
    first = path.read_bytes()
    config.set_dotted("update.check", "false", path)
    assert tomllib.loads(path.read_text()) == {
        "server": {"port": 9000},
        "update": {"check": False},
    }
    backups = list((tmp_path / "backups").glob("*.bak"))
    assert [backup.read_bytes() for backup in backups] == [first]
    config.load(path)
    assert list((tmp_path / "backups").glob("*.bak")) == backups
    assert not list(tmp_path.rglob("*.sha256"))


def test_old_full_file_becomes_only_nondefault_overrides(tmp_path, caplog):
    path = tmp_path / "config.toml"
    cfg = config.Config()
    cfg.server.port = 9123
    cfg.agent.model = "moonshot/my-model"
    original = config.to_toml(cfg, with_comments=True).encode()
    path.write_bytes(original)

    assert config.load(path) == cfg
    assert tomllib.loads(path.read_text()) == {
        "server": {"port": 9123},
        "agent": {"model": "moonshot/my-model"},
    }
    assert next((tmp_path / "backups").glob("*.bak")).read_bytes() == original
    assert not list(tmp_path.rglob("*.sha256"))
    assert not caplog.text


@pytest.mark.parametrize("settings", ["", "[server]\nport = 9123\n"])
def test_legacy_header_is_retired_even_without_default_values(tmp_path, settings):
    path = tmp_path / "config.toml"
    original = (config._FILE_HEADER + settings).encode()
    path.write_bytes(original)

    expected = config.load(path, auto_migrate=False)
    assert config.load(path) == expected
    assert path.read_text().startswith(config._OVERRIDES_HEADER)
    assert [p.read_bytes() for p in (tmp_path / "backups").glob("*.bak")] == [original]

    # A later manual choice equal to a default must remain explicit.
    updated = path.read_bytes() + b"\n[update]\ncheck = true\n"
    path.write_bytes(updated)
    config.load(path)
    assert path.read_bytes() == updated
    assert len(list((tmp_path / "backups").glob("*.bak"))) == 1


def test_legacy_upgrade_is_silent_preserves_settings_and_is_idempotent(
    legacy_config, monkeypatch, caplog
):
    original = (
        legacy_config.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    )
    legacy_config.write_bytes(original)

    cfg = config.load(legacy_config)

    assert cfg.server.port == 9123
    assert cfg.agent.model == "moonshot/my-model"
    assert cfg.provider.moonshot_api_key == "secret-legacy-key"
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    config.CONFIG = cfg
    assert (
        config.resolve_provider_key(("MOONSHOT_API_KEY",), "moonshot_api_key")[0]
        == "secret-legacy-key"
    )
    assert cfg.update.check is False
    assert cfg.pitfalls.max_items == 12
    assert cfg.pitfalls.enabled is False
    assert cfg.memory.default_log_entries == 20
    text = legacy_config.read_text()
    assert "# My rig — keep this comment" in text
    assert "port = 9123 # custom port" in text
    assert "check = false # stay offline" in text
    assert 'moonshot_api_key = "secret-legacy-key" # private' in text
    assert tomllib.loads(text)["providers"]["custom"]["extra"] == "preserve me"
    assert "claude" not in tomllib.loads(text)
    backup = next((legacy_config.parent / "backups").glob("*.bak"))
    assert backup.read_bytes() == original
    if os.name != "nt":
        assert backup.stat().st_mode & 0o777 == 0o600
    assert not caplog.text
    files = {p: p.stat().st_mtime_ns for p in legacy_config.parent.rglob("*")}
    assert config.load(legacy_config) == cfg
    assert {p: p.stat().st_mtime_ns for p in legacy_config.parent.rglob("*")} == files


def test_current_spelling_wins_even_when_empty_or_false(tmp_path, caplog):
    path = tmp_path / "config.toml"
    path.write_text(
        "[update]\nauto = true\ncheck = false\n"
        '[provider]\nkimi_api_key = "old-secret"\nmoonshot_api_key = ""\n'
        "[pitfalls]\ncapture_enabled = false\nenabled = true\n"
    )
    cfg = config.load(path)
    assert cfg.update.check is False
    assert cfg.provider.moonshot_api_key == ""
    assert cfg.pitfalls.enabled is True
    assert "old-secret" not in caplog.text


@pytest.mark.parametrize("legacy_model", [None, "custom-qwen-model"])
def test_legacy_qwen_selection_and_key_reach_runtime(
    physiclaw_home, monkeypatch, mocker, caplog, legacy_model
):
    from physiclaw.agent.runtime.launcher import resolve
    from physiclaw.provider.registry import make_chat_provider
    from physiclaw.provider.vendors.qwen.chat import QwenChatProvider

    path = config.config_path()
    original = (
        '[provider]\nname = "qwen"\nqwen_api_key = "synthetic-legacy-key"\n'
        '[providers.qwen]\nbase_url = "https://proxy.example.test/v1"\n'
    ).encode()
    path.write_bytes(original)
    monkeypatch.delenv(config.MODEL_ENV_VAR, raising=False)
    monkeypatch.delenv("QWEN_MODEL", raising=False)
    if legacy_model:
        monkeypatch.setenv("QWEN_MODEL", legacy_model)
    model = legacy_model or "qwen3.6-plus"
    ref = f"qwen/{model}"
    for env_var in QwenChatProvider._env_vars():
        monkeypatch.delenv(env_var, raising=False)

    # Startup resolves settings during imports, before it can persist upgrades.
    config.CONFIG = config.load(path, auto_migrate=False)
    assert resolve()[0] == ref
    assert path.read_bytes() == original
    config.CONFIG = config.load(path)
    assert resolve()[0] == ref
    client = mocker.patch.object(QwenChatProvider, "_build_client")
    provider = make_chat_provider("qwen", model)
    assert client.call_args.args[0] == "synthetic-legacy-key"
    assert provider._resolved_base_url() == "https://proxy.example.test/v1"
    assert tomllib.loads(path.read_text()) == {
        "agent": {"model": ref},
        "provider": {"qwen_api_key": "synthetic-legacy-key"},
        "providers": {"qwen": {"base_url": "https://proxy.example.test/v1"}},
    }
    assert next((physiclaw_home / "backups").glob("*.bak")).read_bytes() == original
    assert not caplog.text


@pytest.mark.parametrize("model", ["openrouter/openai/custom-model", ""])
def test_legacy_provider_does_not_replace_explicit_model(tmp_path, model):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[agent]\nmodel = "{model}"\n'
        '[provider]\nname = "qwen"\nqwen_api_key = "synthetic-key"\n'
    )
    cfg = config.load(path)
    assert cfg.agent.model == model
    assert cfg.provider.qwen_api_key == "synthetic-key"


@pytest.mark.parametrize("key_field", [f.name for f in fields(config.ProviderConfig)])
@pytest.mark.parametrize("env_override", [False, True])
def test_full_file_preserves_all_credentials_and_custom_providers(
    physiclaw_home, monkeypatch, caplog, key_field, env_override
):
    from physiclaw.provider import registry

    provider_id = key_field.removesuffix("_api_key")
    vendor = registry._VENDORS[provider_id]
    adapter = vendor.chat or vendor.decision.create
    for env_var in adapter._env_vars():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv(config.MODEL_ENV_VAR, raising=False)
    if env_override:
        monkeypatch.setenv(adapter._env_vars()[0], "synthetic-env-key")
        monkeypatch.setenv(config.MODEL_ENV_VAR, "openai/env-model")

    cfg = config.Config()
    for field in fields(cfg.provider):
        setattr(cfg.provider, field.name, f'synthetic-{field.name}-\\"云')
    cfg.agent.model = "openrouter/openai/custom-model"
    cfg.decision.model = f"{provider_id}/custom-decision-model"
    path = config.config_path()
    original = (
        config.to_toml(cfg, with_comments=True)
        + f'\n[providers.{provider_id}]\nbase_url = "https://proxy.example.test/v1"\n'
        '[providers.custom]\nbase_url = "https://custom.example.test/v1"\n'
        'api_key = "synthetic-custom-key"\n'
        '[providers.custom.headers]\nAuthorization = "Bearer synthetic-token"\n'
    )
    path.write_text(original, encoding="utf-8")
    provider_overrides = tomllib.loads(original)["providers"]

    config.CONFIG = config.load(path)
    assert config.CONFIG == cfg
    saved = tomllib.loads(path.read_text())
    assert saved["provider"] == asdict(cfg.provider)
    assert saved["providers"] == provider_overrides
    assert saved["agent"]["model"] == cfg.agent.model
    assert saved["decision"]["model"] == cfg.decision.model
    expected_key = (
        "synthetic-env-key" if env_override else getattr(cfg.provider, key_field)
    )
    assert (
        config.resolve_provider_key(adapter._env_vars(), adapter._config_key())[0]
        == expected_key
    )
    assert adapter._resolved_base_url() == "https://proxy.example.test/v1"
    assert config.model_ref() == (
        "openai/env-model" if env_override else cfg.agent.model
    )
    assert "synthetic-env-key" not in path.read_text()
    assert next((physiclaw_home / "backups").glob("*.bak")).read_text() == original
    assert not caplog.text
    assert config.load(path) == cfg
    assert len(list((physiclaw_home / "backups").glob("*.bak"))) == 1


def test_read_only_import_loading_never_changes_files(legacy_config):
    files = {p: p.stat().st_mtime_ns for p in legacy_config.parent.rglob("*")}
    assert config.load(legacy_config, auto_migrate=False).server.port == 9123
    assert {p: p.stat().st_mtime_ns for p in legacy_config.parent.rglob("*")} == files


@pytest.mark.parametrize("contents", ["", "[server]\nport = 9000\n", None])
def test_current_or_missing_file_is_not_rewritten(tmp_path, contents):
    path = tmp_path / "config.toml"
    if contents is not None:
        path.write_text(contents)
    config.load(path)
    assert path.exists() is (contents is not None)
    if contents is not None:
        assert path.read_text() == contents
    assert not list((tmp_path / "backups").glob("*.bak"))


@pytest.mark.parametrize(
    "invalid",
    [
        b"[server]\nportt = 9000\n",
        b"[server]\nport = '9000'\n",
        b"[unknown]\nvalue = true\n",
        b"[server\n",
        b"# bad UTF-8 \xff\n",
        b"[providers.custom]\nbase_url = 7\n",
        b"[update]\nauto = 'false'\n",
    ],
)
def test_invalid_config_is_never_partially_migrated(tmp_path, invalid):
    path = tmp_path / "config.toml"
    original = b"[claude]\ntimeout_seconds = 180\n" + invalid
    path.write_bytes(original)
    files = set(tmp_path.iterdir())
    with pytest.raises(config.ConfigError):
        config.load(path)
    assert path.read_bytes() == original
    assert set(tmp_path.iterdir()) == files


@pytest.mark.parametrize(
    "target", ["tempfile.mkstemp", "os.fsync", "pathlib.Path.replace"]
)
def test_write_failure_keeps_original_and_runtime_settings_then_retries(
    legacy_config, mocker, caplog, target
):
    original = legacy_config.read_bytes()
    patched = mocker.patch(target, side_effect=PermissionError("read only"))
    cfg = config.load(legacy_config)
    mocker.stop(patched)
    assert cfg.server.port == 9123
    assert cfg.provider.moonshot_api_key == "secret-legacy-key"
    assert legacy_config.read_bytes() == original
    assert not list(legacy_config.parent.glob("*.tmp"))
    backups = list((legacy_config.parent / "backups").glob("*.bak"))
    assert "settings are applied" in caplog.text
    assert config.load(legacy_config) == cfg
    assert legacy_config.read_bytes() != original
    for backup in backups:
        assert backup.read_bytes() == original


def test_migration_does_not_overwrite_concurrent_edit(legacy_config, mocker):
    concurrent = b"[server]\nport = 9001\n"
    read = Path.read_bytes

    def changed_read(path):
        if path == legacy_config and list((path.parent / "backups").glob("*.bak")):
            path.write_bytes(concurrent)
        return read(path)

    mocker.patch.object(Path, "read_bytes", changed_read)
    config.load(legacy_config)
    assert read(legacy_config) == concurrent


@pytest.mark.parametrize("edited", [False, True])
def test_explicit_default_stays_pinned_in_new_overrides_file(tmp_path, edited):
    path = tmp_path / "config.toml"
    config.set_dotted("server.port", "8048", path)
    if edited:
        path.write_text(path.read_text() + "# manual comment\n")
    before = path.read_bytes()
    config.load(path)
    assert path.read_bytes() == before
    assert tomllib.loads(path.read_text())["server"]["port"] == 8048
    assert not list((tmp_path / "backups").glob("*.bak"))


def test_manual_edits_are_backed_up_before_the_next_write(tmp_path):
    path = tmp_path / "config.toml"
    config.set_dotted("server.port", "9000", path)
    original = path.read_bytes() + (
        b'\n[provider]\nopenai_api_key = "manual-secret" # keep this key\n'
        b'[agent]\nmodel = "openai/custom-model"\n'
    )
    path.write_bytes(original)
    files = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}

    assert config.load(path).provider.openai_api_key == "manual-secret"
    assert {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")} == files
    assert not list((tmp_path / "backups").glob("*.bak"))

    config.set_dotted("server.port", "9123", path)
    assert [p.read_bytes() for p in (tmp_path / "backups").glob("*.bak")] == [original]
    cfg = config.load(path)
    assert cfg.provider.openai_api_key == "manual-secret"
    assert cfg.agent.model == "openai/custom-model"
    assert cfg.server.port == 9123
    assert "# keep this key" in path.read_text()


def test_unset_removes_override_and_inherits_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[update]\nauto = false\n")
    config.set_dotted("update.check", "true", path)
    assert config.unset_dotted("update.check", path) is True
    assert config.load(path).update.check is True
    assert tomllib.loads(path.read_text()).get("update", {}) == {}


@pytest.mark.skipif(os.name == "nt", reason="symlinks need Windows privileges")
def test_upgrade_preserves_symlink(legacy_config):
    original = legacy_config.read_bytes()
    link = legacy_config.parent / "linked.toml"
    symlink_to(link, legacy_config)
    config.load(link)
    assert link.is_symlink()
    assert link.read_bytes() == legacy_config.read_bytes() != original
    assert (
        next((legacy_config.parent / "backups").glob("*.bak")).read_bytes() == original
    )


def test_handwritten_defaults_stay_explicit(tmp_path):
    path = tmp_path / "config.toml"
    original = b"# My explicit choice\n[server]\nport = 8048 # keep this port\n"
    path.write_bytes(original)
    config.load(path)
    assert path.read_bytes() == original
    config.set_dotted("update.check", "false", path)
    assert tomllib.loads(path.read_text())["server"]["port"] == 8048
    assert "port = 8048 # keep this port" in path.read_text()


def test_old_backups_move_without_rewriting_config(tmp_path):
    path = tmp_path / "config.toml"
    original = b"[server]\nport = 9000\n"
    path.write_bytes(original)
    old_backup = tmp_path / "config.toml.20260101T120000.abcdefgh.bak"
    old_backup.write_bytes(b"original secret settings")
    mtime = path.stat().st_mtime_ns

    config.load(path)

    state = tmp_path / "backups"
    assert not old_backup.exists()
    assert (state / old_backup.name).read_bytes() == b"original secret settings"
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == mtime


def test_timestamped_history_keeps_five_backups_and_noop_writes_are_idle(tmp_path):
    path = tmp_path / "config.toml"
    config.set_dotted("server.port", "9000", path)
    state = tmp_path / "backups"
    other = state / "other.toml.20260101T120000.abcdefgh.bak"
    other.write_text("keep unrelated history")
    originals = []
    with freeze_time("2026-10-26 15:30:45"):
        for port in range(9001, 9008):
            originals.append(path.read_bytes())
            config.set_dotted("server.port", str(port), path)
    backups = list(state.glob("config.toml.*.bak"))
    assert len(backups) == 5
    assert {p.read_bytes() for p in backups} == set(originals[-5:])
    assert all(
        re.fullmatch(r"config\.toml\.20261026T153045\.[a-z0-9_]+\.bak", p.name)
        for p in backups
    )
    if os.name != "nt":
        assert state.stat().st_mode & 0o777 == 0o700
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in backups)
    assert other.read_text() == "keep unrelated history"

    files = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    config.load(path)
    config.set_dotted("server.port", "9007", path)
    assert {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")} == files


def test_rotation_keeps_new_backup_when_older_backups_have_future_times(tmp_path):
    path = tmp_path / "config.toml"
    config.set_dotted("server.port", "9000", path)
    original = path.read_bytes()
    directory = tmp_path / "backups"
    future = path.stat().st_mtime_ns + 10**15
    for index in range(5):
        backup = directory / f"config.toml.20990101T000000.{index}.bak"
        backup.write_text(f"older snapshot {index}")
        os.utime(backup, ns=(future, future))

    config.set_dotted("server.port", "9123", path)

    backups = list(directory.glob("*.bak"))
    assert len(backups) == 5
    assert original in [backup.read_bytes() for backup in backups]


def test_failed_write_preserves_config_and_history(tmp_path, mocker):
    path = tmp_path / "config.toml"
    for port in range(9000, 9006):
        config.set_dotted("server.port", str(port), path)
    before = path.read_bytes()
    history = {p: p.read_bytes() for p in (tmp_path / "backups").glob("*.bak")}
    mocker.patch.object(config, "write_text_atomic", side_effect=OSError("disk full"))

    with pytest.raises(OSError, match="disk full"):
        config.set_dotted("server.port", "9999", path)

    assert path.read_bytes() == before
    assert all(p.read_bytes() == data for p, data in history.items())


def test_cli_edit_detects_external_change_during_backup(tmp_path, mocker):
    path = tmp_path / "config.toml"
    config.set_dotted("server.port", "9000", path)
    original = path.read_bytes()
    concurrent = b"[server]\nport = 9001\n"
    backup = config._backup_config

    def external_edit(target, data):
        backup(target, data)
        target.write_bytes(concurrent)

    mocker.patch.object(config, "_backup_config", side_effect=external_edit)
    with pytest.raises(config.ConfigError, match="changed during update"):
        config.set_dotted("server.port", "9999", path)
    assert path.read_bytes() == concurrent
    assert next((tmp_path / "backups").glob("*.bak")).read_bytes() == original


def test_concurrent_process_writers_preserve_both_edits(physiclaw_home):
    # Hold the first writer just after its read. The second must report lock
    # contention before it can read, then include the first writer's update.
    config.set_dotted("provider.openai_api_key", "synthetic-secret")
    first_script = """
from physiclaw.common import config
read_document = config._config_document
def paused_read(original, path):
    print('read', flush=True)
    input()
    return read_document(original, path)
config._config_document = paused_read
config.set_dotted('server.port', '9123')
"""
    second_script = """
from contextlib import contextmanager
from physiclaw.common import config
from physiclaw.port import locks as file_lock
try_lock = file_lock._try_lock
reported = False
@contextmanager
def observed_lock(path):
    global reported
    try:
        with try_lock(path):
            yield
    except file_lock.FileLockBusy:
        if not reported:
            print('waiting', flush=True)
            reported = True
        raise
file_lock._try_lock = observed_lock
config.set_dotted('agent.model', 'openai/custom-model')
print('done', flush=True)
"""
    processes = []
    with ThreadPoolExecutor(max_workers=1) as reader:
        try:
            for script, expected in [
                (first_script, "read"),
                (second_script, "waiting"),
            ]:
                process = subprocess.Popen(
                    [sys.executable, "-c", script],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                processes.append(process)
                assert (
                    reader.submit(process.stdout.readline).result(timeout=10).strip()
                    == expected
                )
            _, err = processes[0].communicate("\n", timeout=10)
            assert processes[0].returncode == 0, err
            _, err = processes[1].communicate(timeout=10)
            assert processes[1].returncode == 0, err
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)

    cfg = config.load()
    assert cfg.server.port == 9123
    assert cfg.agent.model == "openai/custom-model"
    assert cfg.provider.openai_api_key == "synthetic-secret"
