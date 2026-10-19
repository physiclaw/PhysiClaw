"""Tests for `physiclaw.provider.registry` — id → class lookups.

The registry is a tiny dispatch table; tests pin the membership and
exercise provider construction, validation, and unknown-provider errors.
"""

from __future__ import annotations

import pytest

from physiclaw.provider import registry
from physiclaw.provider.registry import (
    in_process_provider_ids,
    is_known,
    make_provider,
    provider_class,
    provider_key_status,
    validate_model_ref,
)
from physiclaw.provider.vendors.anthropic.chat import (
    AnthropicChatProvider as AnthropicProvider,
)
from physiclaw.provider.vendors.deepseek.chat import (
    DeepSeekChatProvider as DeepSeekProvider,
)
from physiclaw.provider.vendors.google.chat import GoogleChatProvider as GoogleProvider
from physiclaw.provider.vendors.moonshot.chat import (
    MoonshotChatProvider as MoonshotProvider,
)
from physiclaw.provider.vendors.openai.chat import OpenAIChatProvider as OpenAIProvider
from physiclaw.provider.vendors.qwen.chat import QwenChatProvider as QwenProvider

# ---------- constants ----------


def test_provider_classes_dict_has_exactly_six_known_ids() -> None:
    assert set(in_process_provider_ids()) == {
        "qwen",
        "moonshot",
        "openai",
        "anthropic",
        "google",
        "deepseek",
    }


@pytest.mark.parametrize(
    "provider_id, cls",
    [
        ("qwen", QwenProvider),
        ("moonshot", MoonshotProvider),
        ("openai", OpenAIProvider),
        ("anthropic", AnthropicProvider),
        ("google", GoogleProvider),
        ("deepseek", DeepSeekProvider),
    ],
)
def test_provider_classes_each_id_maps_to_its_vendor_class(
    provider_id: str, cls: type
) -> None:
    assert registry._VENDORS[provider_id].chat is cls


# ---------- in_process_provider_ids ----------


def test_in_process_provider_ids_returns_tuple() -> None:
    out = in_process_provider_ids()

    assert isinstance(out, tuple)
    assert set(out) == {
        name for name, vendor in registry._VENDORS.items() if vendor.chat is not None
    }


# ---------- is_known ----------


@pytest.mark.parametrize(
    "provider_id", ["qwen", "moonshot", "openai", "anthropic", "google", "deepseek"]
)
def test_is_known_true_for_each_in_process_provider(provider_id: str) -> None:
    assert is_known(provider_id) is True


@pytest.mark.parametrize("unknown", ["claude-code", "mystery", "", "Qwen"])
def test_is_known_false_for_other_ids(unknown: str) -> None:
    assert is_known(unknown) is False


# ---------- provider_class ----------


def test_provider_class_returns_class_for_known_id() -> None:
    assert provider_class("openai") is OpenAIProvider


def test_provider_class_returns_none_for_unknown_id() -> None:
    assert provider_class("mystery") is None


# ---------- provider_key_status ----------


def test_provider_key_status_returns_none_for_unknown_provider_id() -> None:
    assert provider_key_status("mystery") == (None, None)


def test_provider_key_status_returns_masked_value_when_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-1234")

    masked, source = provider_key_status("openai")

    # The actual secret is never returned — always shown as 8 stars.
    assert masked == "********"
    assert source is not None
    assert "OPENAI_API_KEY" in source


def test_provider_key_status_returns_none_source_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Strip every env var the resolver looks at + clear config.
    for var in ["OPENAI_API_KEY", "OPENAI_KEY"]:
        monkeypatch.delenv(var, raising=False)

    from physiclaw.common import config

    monkeypatch.setattr(config, "CONFIG", config.Config())

    masked, source = provider_key_status("openai")

    assert masked is None
    assert source is None


# ---------- make_provider ----------


def test_make_provider_instantiates_known_provider_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    p = make_provider("openai", "gpt-5")

    assert isinstance(p, OpenAIProvider)


def test_make_provider_passes_model_id_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    p = make_provider("anthropic", "claude-opus-4-7")

    assert p.model == "claude-opus-4-7"


def test_make_provider_applies_the_configured_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A reasoning model can spend minutes on one decision; the ceiling is
    # the operator's knob, not a constant buried in the client.
    from physiclaw.common.config import CONFIG

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(CONFIG.engine, "provider_timeout_seconds", 42.0)

    p = make_provider("openai", "gpt-5")

    assert p._client.timeout.read == 42.0


def test_make_provider_raises_value_error_for_unknown_id() -> None:
    with pytest.raises(
        ValueError,
        match=(r"^unknown provider 'mystery' \(known: .+\)$"),
    ):
        make_provider("mystery", "any-model")


def test_make_provider_error_message_lists_each_in_process_id() -> None:
    with pytest.raises(ValueError) as exc_info:
        make_provider("nope", "model")

    msg = str(exc_info.value)
    for known in ("qwen", "moonshot", "openai", "anthropic", "google", "deepseek"):
        assert known in msg
    # Comma-space join — kills the `', '` ↔ `'XX, XX'` separator mutation.
    assert "qwen, moonshot" in msg or ", " in msg
    assert "XX" not in msg


@pytest.mark.parametrize("provider", in_process_provider_ids())
def test_validate_model_ref_accepts_registered_provider_without_credentials(provider):
    ref = f"{provider}/org/model-id"

    result = validate_model_ref(ref)

    assert result == (provider, "org/model-id")


@pytest.mark.parametrize("ref", ["unknown/model", "claude-code/model"])
def test_validate_model_ref_rejects_unknown_provider(ref):
    with pytest.raises(ValueError, match="unknown provider"):
        validate_model_ref(ref)


@pytest.mark.parametrize("ref", ["bare", "/model", "openai/"])
def test_validate_model_ref_rejects_malformed_reference(ref):
    with pytest.raises(ValueError):
        validate_model_ref(ref)
