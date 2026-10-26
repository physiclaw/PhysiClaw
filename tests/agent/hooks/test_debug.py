"""Tests for `physiclaw.agent.hooks.debug` — the debug wake: gated on
debug mode's env var, one-shot on fire.

Accepted mutation survivors: three log-message decorations and decoration of
the private claim filename. The claim remains unique and distinct from the
public wake.json path; consuming and cleaning it has the same effect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from physiclaw.agent.hooks import debug
from physiclaw.agent.hooks.debug import debug_wake, wake_path
from physiclaw.agent.runtime.hook import check_hooks
from physiclaw.common import paths
from physiclaw.common.config import DEBUG_ENV_VAR
from physiclaw.common.logger import write_json_atomic
from physiclaw.common.text import read_text


def _arm(payload: str) -> None:
    wake_path().parent.mkdir(parents=True, exist_ok=True)
    wake_path().write_text(payload, encoding="utf-8")


@pytest.fixture()
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEBUG_ENV_VAR, "1")


def test_off_by_default_even_with_a_wake_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DEBUG_ENV_VAR, raising=False)
    _arm(json.dumps({"description": "x"}))

    assert debug_wake() is None
    assert wake_path().exists()  # not consumed: production ignores it whole


def test_no_file_no_trigger(_enabled) -> None:
    assert debug_wake() is None


def test_fires_with_the_description_and_consumes_the_file(_enabled) -> None:
    _arm(json.dumps({"description": "debug: user sent a message"}))

    trigger = debug_wake()

    assert trigger is not None
    assert trigger.description == "debug: user sent a message"
    assert trigger.source == "debug"
    assert not wake_path().exists()  # one-shot


def test_unreadable_file_still_fires_and_is_consumed(_enabled) -> None:
    _arm("not json")

    trigger = debug_wake()

    assert trigger is not None and trigger.description == "debug wake"
    assert not wake_path().exists()


@pytest.mark.parametrize(
    "description", [None, "", " \n ", 123, ["text"], {"text": "x"}]
)
def test_unusable_debug_description_uses_the_default_and_is_consumed(
    _enabled, description
):
    _arm(json.dumps({"description": description}))

    trigger = debug_wake()
    following = debug_wake()

    assert trigger is not None and trigger.description == "debug wake"
    assert following is None


@pytest.mark.parametrize("payload", ["null", "[]", '"text"', "{}"])
def test_non_object_or_empty_debug_payload_still_wakes_once(_enabled, payload):
    _arm(payload)

    trigger = debug_wake()
    following = debug_wake()

    assert trigger is not None and trigger.description == "debug wake"
    assert following is None


def _read_and_arm_another_wake(path):
    contents = read_text(path)
    write_json_atomic(wake_path(), {"description": "new wake"})
    return contents


def test_regression_reading_one_wake_cannot_delete_a_later_wake(_enabled, mocker):
    _arm(json.dumps({"description": "original wake"}))
    reader = mocker.patch.object(
        debug, "read_text", side_effect=_read_and_arm_another_wake
    )

    first = debug_wake()
    reader.side_effect = read_text
    second = debug_wake()
    third = debug_wake()

    assert first is not None and first.description == "original wake"
    assert second is not None and second.description == "new wake"
    assert third is None
    assert [
        p for p in wake_path().parent.iterdir() if p.name != "wake.claim.lock"
    ] == []


def _another_consumer_takes_wake(path, target):
    path.unlink()
    raise FileNotFoundError("already consumed")


def test_concurrent_consumer_winning_the_claim_does_not_duplicate_a_wake(
    _enabled, mocker
):
    _arm(json.dumps({"description": "already claimed elsewhere"}))
    mocker.patch.object(
        Path, "replace", autospec=True, side_effect=_another_consumer_takes_wake
    )

    trigger = debug_wake()

    assert trigger is None
    assert [
        p for p in wake_path().parent.iterdir() if p.name != "wake.claim.lock"
    ] == []


def test_failed_claim_preserves_the_pending_wake_for_retry(_enabled, mocker):
    _arm(json.dumps({"description": "pending"}))
    mocker.patch.object(Path, "replace", side_effect=PermissionError("claim denied"))

    with pytest.raises(PermissionError, match="claim denied"):
        debug_wake()

    assert json.loads(wake_path().read_text()) == {"description": "pending"}


def test_claim_cleanup_failure_does_not_drop_or_replay_the_trigger(
    _enabled, mocker, caplog
):
    _arm(json.dumps({"description": "claimed"}))
    mocker.patch.object(Path, "unlink", side_effect=PermissionError("cleanup denied"))

    first = debug_wake()
    second = debug_wake()

    assert first is not None and first.description == "claimed"
    assert second is None
    assert "cleanup denied" in caplog.text
    assert not wake_path().exists()


def test_invalid_utf8_wake_is_consumed_with_a_default_description(_enabled):
    _arm("placeholder")
    wake_path().write_bytes(b"\xff")

    trigger = debug_wake()

    assert trigger is not None and trigger.description == "debug wake"
    assert [
        p for p in wake_path().parent.iterdir() if p.name != "wake.claim.lock"
    ] == []


@pytest.mark.asyncio
async def test_debug_hook_registers_and_consumes_the_persisted_wake_filename(
    _enabled, fresh_hook_module
):
    path = paths.debug_dir() / "wake.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"description": "persisted before startup"}))
    fresh_hook_module(debug, "debug_wake")

    triggers = await check_hooks()

    assert len(triggers) == 1
    assert triggers[0].source == "debug"
    assert triggers[0].description == "persisted before startup"
    assert not path.exists()


def _read_and_remove_claim(path):
    contents = read_text(path)
    path.unlink()
    return contents


def test_a_claim_already_removed_by_cleanup_is_not_an_error(_enabled, mocker, caplog):
    _arm(json.dumps({"description": "read successfully"}))
    mocker.patch.object(debug, "read_text", side_effect=_read_and_remove_claim)

    trigger = debug_wake()

    assert trigger is not None and trigger.description == "read successfully"
    assert not caplog.records
