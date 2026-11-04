"""Tests for `physiclaw.provider.catalog.cache` — model-list cache.

Module-level `_DIR = paths.HOME / "discovered"` is captured at import
time; the autouse `_dir_fixture` re-points it to a per-test directory
so writes don't bleed. The two import-time tests at the bottom use
`importlib.reload` to validate the literal "discovered" subdir name.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from physiclaw.provider import discovery


@pytest.fixture(autouse=True)
def _dir_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test cache root — replaces `discovery._DIR`."""
    cache_root = tmp_path / "discovered"
    monkeypatch.setattr(discovery, "_DIR", cache_root)
    return cache_root


# ---------- cache_path ----------


def test_cache_path_lives_under_underscore_DIR(_dir_fixture: Path) -> None:
    assert discovery.cache_path("openai") == _dir_fixture / "openai.json"


# ---------- save ----------


def test_save_writes_models_with_fetched_at_timestamp(_dir_fixture: Path) -> None:
    models = [{"id": "gpt-5"}, {"id": "gpt-4"}]

    discovery.save("openai", models)

    payload = json.loads((_dir_fixture / "openai.json").read_text(encoding="utf-8"))
    assert payload["models"] == models
    # ISO 8601 with seconds precision and UTC timezone
    assert payload["fetched_at"].endswith("+00:00")
    assert "T" in payload["fetched_at"]


def test_save_creates_parent_directory_when_missing(_dir_fixture: Path) -> None:
    assert not _dir_fixture.exists()

    discovery.save("openai", [])

    assert _dir_fixture.is_dir()


def test_save_unwritable_parent_does_not_abort_discovery(_dir_fixture: Path) -> None:
    _dir_fixture.write_text("a file blocks directory creation", encoding="utf-8")

    discovery.save("openai", [{"id": "model"}])

    assert _dir_fixture.is_file()


@pytest.mark.parametrize("bad_id", [None, [], {}, 3, True, "", "   "])
def test_model_ids_ignores_malformed_ids(_dir_fixture: Path, bad_id: object) -> None:
    discovery.save("openai", [{"id": "valid"}, {"id": bad_id}])

    result = discovery.model_ids("openai")

    assert result == {"valid"}


def test_save_creates_intermediate_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deep = tmp_path / "a" / "b" / "c" / "discovered"
    monkeypatch.setattr(discovery, "_DIR", deep)

    discovery.save("openai", [{"id": "gpt-5"}])

    assert (deep / "openai.json").is_file()


def test_save_serializes_with_two_space_indent(_dir_fixture: Path) -> None:
    discovery.save("openai", [{"id": "gpt-5"}])

    text = (_dir_fixture / "openai.json").read_text(encoding="utf-8")
    # Byte-for-byte equality with json.dumps(indent=2) — kills any
    # mutation on the indent argument.
    import json as _json

    expected = _json.dumps(_json.loads(text), indent=2) + "\n"
    assert text == expected


def test_save_is_idempotent_when_called_twice(_dir_fixture: Path) -> None:
    # `mkdir(parents=True, exist_ok=True)` — exist_ok=True must hold so
    # a second save doesn't raise FileExistsError on the existing dir.
    discovery.save("openai", [{"id": "first"}])

    discovery.save("openai", [{"id": "second"}])  # must not raise

    assert discovery.load("openai") == [{"id": "second"}]


def test_save_failed_replacement_preserves_previous_model_cache(mocker) -> None:
    discovery.save("openai", [{"id": "previous"}])
    mocker.patch.object(Path, "replace", side_effect=OSError("disk unavailable"))

    discovery.save("openai", [{"id": "new"}])

    assert discovery.load("openai") == [{"id": "previous"}]


def test_save_swallows_oserror_and_logs_warning(
    _dir_fixture: Path,
    mocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mocker.patch.object(
        discovery, "write_json_atomic", side_effect=OSError("disk full")
    )

    with caplog.at_level(logging.WARNING, logger="physiclaw.provider.catalog.cache"):
        # Must not raise.
        discovery.save("openai", [{"id": "gpt-5"}])

    assert any(
        r.getMessage().startswith("failed to write discovered cache for openai:")
        for r in caplog.records
    )


# ---------- load ----------


def test_load_returns_empty_list_when_cache_file_missing() -> None:
    assert discovery.load("openai") == []


def test_load_returns_models_list_when_cache_present(_dir_fixture: Path) -> None:
    discovery.save("openai", [{"id": "gpt-5"}, {"id": "gpt-4"}])

    assert discovery.load("openai") == [{"id": "gpt-5"}, {"id": "gpt-4"}]


def test_load_returns_empty_list_when_models_key_absent(_dir_fixture: Path) -> None:
    _dir_fixture.mkdir(parents=True)
    (_dir_fixture / "openai.json").write_text(
        json.dumps({"fetched_at": "2026-04-28T00:00:00+00:00"}), encoding="utf-8"
    )

    assert discovery.load("openai") == []


def test_load_returns_empty_when_models_key_is_null(_dir_fixture: Path) -> None:
    # `payload.get("models") or []` — null in JSON becomes None in Python,
    # falsy, so the `or []` fallback fires.
    _dir_fixture.mkdir(parents=True)
    (_dir_fixture / "openai.json").write_text(
        json.dumps({"fetched_at": "2026-04-28T00:00:00+00:00", "models": None}),
        encoding="utf-8",
    )

    assert discovery.load("openai") == []


def test_load_returns_empty_and_logs_on_malformed_json(
    _dir_fixture: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _dir_fixture.mkdir(parents=True)
    (_dir_fixture / "openai.json").write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="physiclaw.provider.catalog.cache"):
        out = discovery.load("openai")

    assert out == []
    assert any(
        r.getMessage().startswith("failed to read discovered cache for openai:")
        for r in caplog.records
    )


def test_load_returns_empty_and_logs_on_oserror(
    _dir_fixture: Path,
    mocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _dir_fixture.mkdir(parents=True)
    (_dir_fixture / "openai.json").write_text("{}", encoding="utf-8")
    mocker.patch.object(Path, "read_text", side_effect=OSError("permission denied"))

    with caplog.at_level(logging.WARNING, logger="physiclaw.provider.catalog.cache"):
        out = discovery.load("openai")

    assert out == []
    assert any(
        r.getMessage().startswith("failed to read discovered cache for openai:")
        for r in caplog.records
    )


# ---------- model_ids ----------


def test_model_ids_returns_set_of_ids(_dir_fixture: Path) -> None:
    discovery.save("openai", [{"id": "gpt-5"}, {"id": "gpt-4"}])

    assert discovery.model_ids("openai") == {"gpt-5", "gpt-4"}


def test_model_ids_filters_empty_string_ids(_dir_fixture: Path) -> None:
    # Defensive — if a provider returns a model with empty id, it must
    # not pollute the membership check.
    discovery.save("openai", [{"id": "gpt-5"}, {"id": ""}, {"name": "no-id-key"}])

    assert discovery.model_ids("openai") == {"gpt-5"}


def test_model_ids_returns_empty_set_when_no_cache() -> None:
    assert discovery.model_ids("openai") == set()


# ---------- import-time _DIR resolution ----------


def test_dir_at_import_lives_under_paths_HOME(physiclaw_home: Path) -> None:
    # Reload the module to recompute `_DIR = paths.HOME / "discovered"`
    # against the per-test paths.HOME. Pin the literal "discovered"
    # subdir name — kills `"discovered"` ↔ `"XXdiscoveredXX"` and
    # `_DIR = None` mutations.
    import importlib

    importlib.reload(discovery)

    assert discovery._DIR == physiclaw_home / "discovered"
    assert discovery._DIR.name == "discovered"
