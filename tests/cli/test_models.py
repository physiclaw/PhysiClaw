"""Public model-management commands with isolated config and discovery caches.

Mutation review accepts text/indentation-only variants, the unused default
indent (both callers pass it explicitly), and the missing-id display fallback
(provider discovery validates every id before returning). Behavioral variants
must fail, including exposed legacy aliases and unwanted completion options.
"""

from __future__ import annotations

import asyncio
import os
import re

import pytest
import typer
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from physiclaw.cli.models import models_app
from physiclaw.common import config
from physiclaw.provider import discovered, in_process_provider_ids

runner = CliRunner()


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_lists_public_commands_without_legacy_alias_or_completion(flag):
    result = runner.invoke(models_app, [flag])

    assert result.exit_code == 0
    assert all(
        command in result.stdout
        for command in ("use", "key", "keys", "list", "discover")
    )
    assert re.search(r"\bset\b", typer.unstyle(result.output)) is None
    assert "--install-completion" not in typer.unstyle(result.output)


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch, physiclaw_home):
    monkeypatch.setattr(config, "CONFIG", config.Config())
    monkeypatch.setattr(discovered, "_DIR", physiclaw_home / "discovered")
    for name in list(os.environ):
        if name.endswith("_API_KEY") or name == "PHYSICLAW_MODEL":
            monkeypatch.delenv(name)


class DiscoveryClient:
    def __init__(self):
        self.models = [{"id": "test-model"}]
        self.fetch_error = None
        self.close_error = None
        self.events = []

    async def list_models(self):
        self.events.append("fetch")
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.models

    async def aclose(self):
        self.events.append("close")
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture
def client(mocker):
    instance = DiscoveryClient()
    factory = mocker.Mock(return_value=instance)
    lookup = mocker.patch("physiclaw.provider.provider_class", return_value=factory)
    instance.factory = factory
    instance.lookup = lookup
    return instance


def test_status_without_model_shows_setup_hint():
    result = runner.invoke(models_app, [])

    assert result.exit_code == 0
    assert "physiclaw models key <provider>" in result.stdout
    assert "physiclaw models list" in result.stdout


@pytest.mark.parametrize(
    ("env", "expected_model", "expected_source"),
    [
        ({}, "openai/config-model", "config.toml [provider]"),
        (
            {
                "PHYSICLAW_MODEL": "openai/env-model",
                "OPENAI_API_KEY": "synthetic-env-key",
            },
            "openai/env-model",
            "OPENAI_API_KEY env",
        ),
    ],
)
def test_status_reports_effective_model_and_masks_key(
    env, expected_model, expected_source
):
    config.set_dotted("agent.model", "openai/config-model")
    config.set_dotted("provider.openai_api_key", "synthetic-config-key")

    result = runner.invoke(models_app, [], env=env)

    assert result.exit_code == 0
    assert expected_model in result.stdout
    assert expected_source in result.stdout
    assert "********" in result.stdout
    assert "synthetic-" not in result.output


def test_status_without_key_reports_unset():
    config.set_dotted("agent.model", "openai/test-model")

    result = runner.invoke(models_app, [])

    assert result.exit_code == 0
    assert "openai api key: (unset)" in result.stdout


@pytest.mark.parametrize("ref", ["bare", "/model", "openai/"])
def test_status_invalid_ref_explains_problem(ref):
    result = runner.invoke(models_app, [], env={"PHYSICLAW_MODEL": ref})

    assert result.exit_code == 0
    assert "invalid ref" in result.stdout


@pytest.mark.parametrize("provider", ["openai", "claude-code"])
def test_list_without_cache_prints_provider_specific_hint(provider):
    result = runner.invoke(models_app, ["list", provider])

    assert result.exit_code == 0
    assert f"physiclaw models discover {provider}" in result.stdout


def test_list_all_includes_each_provider_and_catalog_alias():
    discovered.save("openai", [{"id": "openai-model"}])
    discovered.save("anthropic", [{"id": "anthropic-model"}])

    result = runner.invoke(models_app, ["list"])

    assert result.exit_code == 0
    assert all(provider in result.stdout for provider in in_process_provider_ids())
    assert "openai/openai-model" in result.stdout
    assert "anthropic/anthropic-model" in result.stdout
    assert "claude-code/anthropic-model" in result.stdout


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    ids=st.lists(
        st.text(alphabet="abcXYZ012.-/_模型", min_size=1, max_size=20), max_size=10
    )
)
def test_list_cached_ids_are_sorted_and_deduplicated(ids):
    discovered.save("openai", [{"id": model_id} for model_id in ids])

    result = runner.invoke(models_app, ["list", "openai"])

    assert result.exit_code == 0
    assert [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith("openai/")
    ] == [f"openai/{model_id}" for model_id in sorted(set(ids))]


@pytest.mark.parametrize("command", ["list", "discover", "key"])
def test_unknown_provider_fails_before_any_client_or_config_write(client, command):
    result = runner.invoke(models_app, [command, "unknown-provider"])

    assert result.exit_code == 1
    assert "unknown provider" in result.output
    assert not config.config_path().exists()
    client.lookup.assert_not_called()


@pytest.mark.parametrize("command", ["use", "set"])
def test_use_cached_model_persists_exact_ref_and_preserves_comments(command):
    config.config_path().write_text(
        '# keep this comment\n[agent]\nmodel = "old/model"\n'
    )
    discovered.save("openai", [{"id": "org/model-v1"}])

    result = runner.invoke(models_app, [command, "openai/org/model-v1"])

    assert result.exit_code == 0
    assert config.load().agent.model == "openai/org/model-v1"
    assert config.CONFIG.agent.model == "openai/org/model-v1"
    assert "# keep this comment" in config.config_path().read_text()
    assert "Restart" in result.stdout


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        ("bare", "is not a `provider/model`"),
        ("/model", "empty provider"),
        ("openai/", "empty provider"),
        ("unknown/model", "unknown provider"),
        ("openai/missing", "not in openai discovery cache"),
    ],
)
def test_use_invalid_ref_preserves_config(ref, message):
    config.set_dotted("agent.model", "openai/original")
    before = config.config_path().read_bytes()

    result = runner.invoke(models_app, ["use", ref])

    assert result.exit_code == 1
    assert message in result.output
    assert config.config_path().read_bytes() == before


def test_use_subscription_alias_without_cache_preserves_existing_support():
    result = runner.invoke(models_app, ["use", "claude-code/subscription-model"])

    assert result.exit_code == 0
    assert config.load().agent.model == "claude-code/subscription-model"


def test_use_alias_with_catalog_still_rejects_missing_model():
    discovered.save("anthropic", [{"id": "available"}])

    result = runner.invoke(models_app, ["use", "claude-code/missing"])

    assert result.exit_code == 1
    assert "not in claude-code discovery cache" in result.output
    assert not config.config_path().exists()


@pytest.mark.parametrize(
    "arguments", [["use", "openai/test-model"], ["key", "openai", "synthetic-key"]]
)
@pytest.mark.parametrize(
    "error", [PermissionError("read-only config"), OSError("disk full")]
)
def test_regression_config_write_failure_has_diagnostic_and_no_success(
    mocker, client, arguments, error
):
    discovered.save("openai", [{"id": "test-model"}])
    config.write_default()
    before = config.config_path().read_bytes()
    mocker.patch("physiclaw.common.config.write_text", side_effect=error)

    result = runner.invoke(models_app, arguments)

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert str(error) in result.stderr
    assert "Restart" not in result.stdout
    assert config.config_path().read_bytes() == before
    client.lookup.assert_not_called()


@pytest.mark.parametrize(
    "arguments", [["use", "openai/test-model"], ["key", "openai", "synthetic-key"]]
)
def test_invalid_config_is_reported_without_overwriting_it(arguments, client):
    discovered.save("openai", [{"id": "test-model"}])
    config.config_path().write_text("[broken")

    result = runner.invoke(models_app, arguments)

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "config.toml" in result.stderr
    assert config.config_path().read_text() == "[broken"
    client.lookup.assert_not_called()


@pytest.mark.parametrize("provider", in_process_provider_ids())
def test_key_saves_provider_field_and_caches_live_models(client, provider):
    result = runner.invoke(models_app, ["key", provider, "synthetic-key"])

    assert result.exit_code == 0
    assert getattr(config.load().provider, f"{provider}_api_key") == "synthetic-key"
    assert discovered.model_ids(provider) == {"test-model"}
    assert "synthetic-key" not in result.output
    assert f"physiclaw models use {provider}/" in result.stdout
    client.lookup.assert_called_once_with(provider)
    client.factory.assert_called_once_with(model="")
    assert client.events == ["fetch", "close"]


def test_key_hidden_prompt_does_not_echo_secret(client):
    result = runner.invoke(
        models_app, ["key", "openai"], input="synthetic-prompt-key\n"
    )

    assert result.exit_code == 0
    assert config.load().provider.openai_api_key == "synthetic-prompt-key"
    assert "synthetic-prompt-key" not in result.output
    assert discovered.model_ids("openai") == {"test-model"}


def test_key_aborted_prompt_does_not_write_or_connect(client):
    result = runner.invoke(models_app, ["key", "openai"], input="")

    assert result.exit_code == 1
    assert not config.config_path().exists()
    client.lookup.assert_not_called()


def test_key_alias_points_to_credential_owner(client):
    result = runner.invoke(models_app, ["key", "claude-code", "synthetic-key"])

    assert result.exit_code == 1
    assert "physiclaw models key anthropic" in result.output
    assert not config.config_path().exists()
    client.lookup.assert_not_called()


def test_keys_show_masked_config_and_environment_sources():
    config.set_dotted("provider.qwen_api_key", "synthetic-config-key")

    result = runner.invoke(
        models_app, ["keys"], env={"OPENAI_API_KEY": "synthetic-env-key"}
    )

    assert result.exit_code == 0
    assert (
        "qwen api key: ********  [config.toml [provider] qwen_api_key]" in result.stdout
    )
    assert "openai api key: ********  [OPENAI_API_KEY env]" in result.stdout
    assert "moonshot api key: (unset)" in result.stdout
    assert "synthetic-" not in result.output
    assert result.stdout.count("api key:") == len(in_process_provider_ids())


@pytest.mark.parametrize(
    "provider,source", [("openai", "openai"), ("claude-code", "anthropic")]
)
def test_discover_caches_result_under_source_and_displays_requested_provider(
    client, provider, source
):
    result = runner.invoke(models_app, ["discover", provider])

    assert result.exit_code == 0
    assert discovered.model_ids(source) == {"test-model"}
    assert f"{provider} — 1 model(s) live" in result.stdout
    assert f"physiclaw models use {provider}/" in result.stdout
    client.lookup.assert_called_once_with(source)
    assert client.events == ["fetch", "close"]


def test_discover_empty_response_replaces_stale_cache(client):
    discovered.save("openai", [{"id": "old-model"}])
    client.models = []

    result = runner.invoke(models_app, ["discover", "openai"])

    assert result.exit_code == 0
    assert discovered.model_ids("openai") == set()
    assert "0 model(s) live" in result.stdout
    assert client.events == ["fetch", "close"]


@pytest.mark.parametrize(
    ("arguments", "exit_code"),
    [(["discover", "openai"], 1), (["key", "openai", "synthetic-key"], 0)],
)
@pytest.mark.parametrize("close_error", [None, OSError("secondary close failure")])
def test_regression_discovery_failure_remains_visible_after_cleanup(
    client, arguments, exit_code, close_error
):
    discovered.save("openai", [{"id": "old-model"}])
    client.fetch_error = RuntimeError("primary discovery failure")
    client.close_error = close_error

    result = runner.invoke(models_app, arguments)

    assert result.exit_code == exit_code
    assert "primary discovery failure" in result.output
    assert discovered.model_ids("openai") == {"old-model"}
    assert client.events == ["fetch", "close"]


@pytest.mark.parametrize(
    "arguments", [["discover", "openai"], ["key", "openai", "synthetic-key"]]
)
def test_regression_cleanup_failure_preserves_fetched_catalog(client, arguments):
    client.close_error = OSError("close failed")

    result = runner.invoke(models_app, arguments)

    assert result.exit_code == 0
    assert "couldn't close provider client" in result.stderr
    assert discovered.model_ids("openai") == {"test-model"}
    assert "test-model" in result.stdout
    assert client.events == ["fetch", "close"]


@pytest.mark.parametrize("close_error", [None, OSError("secondary close failure")])
def test_regression_discovery_cancellation_survives_cleanup(client, close_error):
    client.fetch_error = asyncio.CancelledError()
    client.close_error = close_error

    with pytest.raises(asyncio.CancelledError):
        runner.invoke(models_app, ["discover", "openai"])

    assert client.events == ["fetch", "close"]
    assert not discovered.cache_path("openai").exists()


def test_cleanup_cancellation_is_not_swallowed(client):
    client.close_error = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        runner.invoke(models_app, ["discover", "openai"])

    assert client.events == ["fetch", "close"]
    assert not discovered.cache_path("openai").exists()


@pytest.mark.parametrize("close_error", [None, OSError("secondary close failure")])
def test_regression_discovery_system_exit_survives_cleanup(client, close_error):
    client.fetch_error = SystemExit(7)
    client.close_error = close_error

    result = runner.invoke(models_app, ["discover", "openai"])

    assert result.exit_code == 7
    assert client.events == ["fetch", "close"]
    assert not discovered.cache_path("openai").exists()


@pytest.mark.parametrize("close_error", [None, OSError("secondary close failure")])
def test_regression_keyboard_interrupt_survives_cleanup(client, close_error):
    client.fetch_error = KeyboardInterrupt()
    client.close_error = close_error

    result = runner.invoke(models_app, ["discover", "openai"])

    assert result.exit_code == 130
    assert client.events == ["fetch", "close"]
    assert not discovered.cache_path("openai").exists()


def test_discover_missing_provider_implementation_has_clear_error(mocker):
    mocker.patch("physiclaw.provider.provider_class", return_value=None)

    result = runner.invoke(models_app, ["discover", "openai"])

    assert result.exit_code == 1
    assert "unknown provider 'openai'" in result.output


def test_discover_client_construction_failure_has_clear_error(mocker):
    mocker.patch(
        "physiclaw.provider.provider_class",
        return_value=mocker.Mock(side_effect=RuntimeError("missing credential")),
    )

    result = runner.invoke(models_app, ["discover", "openai"])

    assert result.exit_code == 1
    assert "missing credential" in result.output


def test_discover_cache_write_failure_keeps_live_list(client, physiclaw_home, caplog):
    (physiclaw_home / "discovered").write_text("not a directory")

    result = runner.invoke(models_app, ["discover", "openai"])

    assert result.exit_code == 0
    assert "test-model" in result.stdout
    assert "failed to write discovered cache" in caplog.text
    assert client.events == ["fetch", "close"]


def test_key_immediately_uses_new_credential_for_real_provider(respx_mock):
    route = respx_mock.get("https://api.openai.com/v1/models").respond(
        200, json={"data": [{"id": "test-model"}]}
    )

    result = runner.invoke(models_app, ["key", "openai", "synthetic-new-key"])

    assert result.exit_code == 0
    assert (
        route.calls.last.request.headers["authorization"] == "Bearer synthetic-new-key"
    )
    assert discovered.model_ids("openai") == {"test-model"}
    assert "synthetic-new-key" not in result.output
