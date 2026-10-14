"""Tests for `physiclaw.cli._update_check` — soft PyPI version check.

Network is mocked at the ``urllib.request.urlopen`` boundary; the
``physiclaw_home`` autouse fixture (see ``tests/conftest.py``) gives
each test a clean ``paths.HOME`` so the cache file is per-test.

Mutation review: accepted survivors change invalid fallback strings to other
invalid strings, add cosmetic text around the banner, or disable its explicit
flush (TTY output already ends in a newline). Cache creation, validity and
expiration are asserted through the public banner entry point below.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from physiclaw.cli import _update_check as uc

# ---------- _is_newer comparator ----------


@pytest.mark.parametrize(
    "current,latest,expected",
    [
        ("0.0.5", "0.1.0", True),
        ("0.0.5", "0.0.10", True),  # numeric, not lexical
        ("0.0.5", "0.0.5", False),
        ("0.1.0", "0.0.9", False),
        ("1.0.0", "1.0.0", False),
        ("0.0.5", "1.0.0", True),
    ],
)
def test_is_newer_dotted_versions(current: str, latest: str, expected: bool) -> None:
    assert uc._is_newer(current, latest) is expected


@pytest.mark.parametrize(
    "current,latest",
    [
        ("0.0.5", "0.1.0a1"),  # pre-release suffix → bail
        ("0.0.5", "0.1.0+local"),  # local version → bail
        ("0.0.5", ""),
    ],
)
def test_is_newer_returns_false_on_non_numeric(current: str, latest: str) -> None:
    # Conservative: don't nudge on versions we can't safely compare.
    assert uc._is_newer(current, latest) is False


# ---------- env disable ----------


@pytest.mark.parametrize("val", ["1", "true", "yes", "TRUE", "Yes"])
def test_env_disable_skips_check(val: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHYSICLAW_DISABLE_UPDATE_CHECK", val)
    assert uc._disabled_via_env() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", " "])
def test_env_disable_inactive_for_other_values(
    val: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHYSICLAW_DISABLE_UPDATE_CHECK", val)
    assert uc._disabled_via_env() is False


def test_env_disable_short_circuits_banner(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv("PHYSICLAW_DISABLE_UPDATE_CHECK", "1")
    # Even if PyPI would say "newer", the env disable wins before any I/O.
    fake_resolve = MagicMock(return_value="99.99.99")
    monkeypatch.setattr(uc, "_resolve_latest", fake_resolve)

    uc.maybe_print_update_banner()

    fake_resolve.assert_not_called()
    assert capsys.readouterr().out == ""


# ---------- TTY guard ----------


def test_non_tty_skips_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHYSICLAW_DISABLE_UPDATE_CHECK", raising=False)
    fake_stdout = io.StringIO()  # io.StringIO.isatty() returns False
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    fake_resolve = MagicMock(return_value="99.99.99")
    monkeypatch.setattr(uc, "_resolve_latest", fake_resolve)

    uc.maybe_print_update_banner()

    fake_resolve.assert_not_called()
    assert fake_stdout.getvalue() == ""


# ---------- cache freshness ----------


def test_cache_is_fresh_within_ttl() -> None:
    recent = datetime.now(timezone.utc) - timedelta(days=3)
    assert uc._cache_is_fresh(recent.isoformat()) is True


def test_cache_is_stale_past_ttl() -> None:
    old = datetime.now(timezone.utc) - timedelta(days=8)
    assert uc._cache_is_fresh(old.isoformat()) is False


def test_cache_is_fresh_handles_naive_timestamp() -> None:
    naive = (datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None)
    # Treats naive as UTC (forward-compatible).
    assert uc._cache_is_fresh(naive.isoformat()) is True


def test_cache_is_fresh_returns_false_on_garbage() -> None:
    assert uc._cache_is_fresh("not-a-timestamp") is False
    assert uc._cache_is_fresh("") is False


# ---------- cache read / write ----------


def test_write_then_read_cache_roundtrip(physiclaw_home: Path) -> None:
    uc._write_cache("1.2.3")
    cache = uc._read_cache()
    assert cache is not None
    assert cache["latest_version"] == "1.2.3"
    # checked_at is ISO-format; parses cleanly.
    assert datetime.fromisoformat(cache["checked_at"]).tzinfo is not None


def test_read_cache_returns_none_when_missing(physiclaw_home: Path) -> None:
    # No cache file written.
    assert uc._read_cache() is None


def test_read_cache_returns_none_on_corrupt_json(physiclaw_home: Path) -> None:
    cache_file = physiclaw_home / "run" / "version-check.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("not json {")
    assert uc._read_cache() is None


def test_read_cache_returns_none_when_top_level_is_not_object(
    physiclaw_home: Path,
) -> None:
    cache_file = physiclaw_home / "run" / "version-check.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps([1, 2, 3]))
    assert uc._read_cache() is None


def test_write_cache_swallows_oserror(
    physiclaw_home: Path,
    mocker,
) -> None:
    write = mocker.patch.object(uc, "write_text", side_effect=OSError("read-only"))

    uc._write_cache("9.9.9")

    write.assert_called_once()


# ---------- _resolve_latest: cache vs. network ----------


def test_resolve_latest_uses_fresh_cache(
    physiclaw_home: Path,
    mocker,
) -> None:
    uc._write_cache("0.0.7")
    fetch_spy = mocker.patch.object(uc, "_fetch_pypi_version")

    assert uc._resolve_latest() == "0.0.7"
    fetch_spy.assert_not_called()


def test_resolve_latest_refetches_when_stale(
    physiclaw_home: Path,
    mocker,
) -> None:
    # Manually plant a stale cache.
    cache_file = physiclaw_home / "run" / "version-check.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(
            {
                "checked_at": (
                    datetime.now(timezone.utc) - timedelta(days=30)
                ).isoformat(),
                "latest_version": "0.0.4",
            }
        )
    )
    mocker.patch.object(uc, "_fetch_pypi_version", return_value="0.0.9")

    assert uc._resolve_latest() == "0.0.9"
    # Cache has been updated with the new fetch.
    refreshed = uc._read_cache()
    assert refreshed["latest_version"] == "0.0.9"


def test_resolve_latest_returns_none_when_pypi_unreachable(
    physiclaw_home: Path,
    mocker,
) -> None:
    mocker.patch.object(uc, "_fetch_pypi_version", return_value=None)
    assert uc._resolve_latest() is None


def test_resolve_latest_falls_back_to_network_on_corrupt_cache(
    physiclaw_home: Path,
    mocker,
) -> None:
    cache_file = physiclaw_home / "run" / "version-check.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("garbage")
    mocker.patch.object(uc, "_fetch_pypi_version", return_value="2.0.0")

    assert uc._resolve_latest() == "2.0.0"


# ---------- _fetch_pypi_version ----------


def test_fetch_pypi_version_extracts_info_version(mocker) -> None:
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps({"info": {"version": "0.1.5"}}).encode()
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: None
    mocker.patch("urllib.request.urlopen", return_value=fake_resp)

    assert uc._fetch_pypi_version() == "0.1.5"


def test_fetch_pypi_version_returns_none_on_http_error(mocker) -> None:
    mocker.patch(
        "urllib.request.urlopen",
        side_effect=OSError("network unreachable"),
    )
    assert uc._fetch_pypi_version() is None


def test_fetch_pypi_version_returns_none_on_unexpected_payload(mocker) -> None:
    fake_resp = MagicMock()
    fake_resp.read.return_value = b'{"unexpected": "shape"}'
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: None
    mocker.patch("urllib.request.urlopen", return_value=fake_resp)

    assert uc._fetch_pypi_version() is None


# ---------- banner formatting ----------


@pytest.fixture
def banner_env(monkeypatch: pytest.MonkeyPatch, mocker) -> MagicMock:
    """Disable env override, force isatty=True, and intercept builtins.print.

    Yields the print mock so each test can inspect call args.
    """
    monkeypatch.delenv("PHYSICLAW_DISABLE_UPDATE_CHECK", raising=False)
    mocker.patch.object(sys.stdout, "isatty", return_value=True)
    return mocker.patch("builtins.print")


def test_banner_prints_when_newer(
    banner_env: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(uc, "_pkg_version", "0.0.5")
    monkeypatch.setattr(uc, "_resolve_latest", lambda: "1.2.3")

    uc.maybe_print_update_banner()

    banner_env.assert_called_once()
    msg = banner_env.call_args.args[0]
    assert "physiclaw 0.0.5 → 1.2.3 available" in msg
    assert "uv tool upgrade physiclaw" in msg


def test_banner_silent_when_at_latest(
    banner_env: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(uc, "_pkg_version", "1.2.3")
    monkeypatch.setattr(uc, "_resolve_latest", lambda: "1.2.3")

    uc.maybe_print_update_banner()

    banner_env.assert_not_called()


def test_banner_silent_when_pypi_unreachable(
    banner_env: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(uc, "_resolve_latest", lambda: None)

    uc.maybe_print_update_banner()

    banner_env.assert_not_called()


def test_banner_silent_when_local_is_newer_than_pypi(
    banner_env: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Dev install or pre-release ahead of public PyPI.
    monkeypatch.setattr(uc, "_pkg_version", "9.9.9")
    monkeypatch.setattr(uc, "_resolve_latest", lambda: "0.0.5")

    uc.maybe_print_update_banner()

    banner_env.assert_not_called()


class _TTY(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def public_banner(monkeypatch, mocker):
    monkeypatch.delenv("PHYSICLAW_DISABLE_UPDATE_CHECK", raising=False)
    terminal = _TTY()
    fetch = mocker.patch(
        "physiclaw.cli._update_check.urllib.request.urlopen",
        side_effect=lambda *a, **kw: io.BytesIO(b'{"info":{"version":"999.0.0"}}'),
    )
    return terminal, fetch


def _plant_cache(home, checked_at, latest="998.0.0"):
    path = home / "run" / "version-check.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"checked_at": checked_at, "latest_version": latest}),
        encoding="utf-8",
    )
    return path


def test_regression_banner_rechecks_future_dated_cache(physiclaw_home, public_banner):
    terminal, fetch = public_banner
    _plant_cache(physiclaw_home, "2027-01-01T00:00:00+00:00")

    with freeze_time("2026-10-14T12:00:00+00:00"), redirect_stdout(terminal):
        uc.maybe_print_update_banner()

    assert fetch.call_count == 1
    assert "999.0.0 available" in terminal.getvalue()


def test_regression_banner_reads_cache_without_fallible_stat_probe(
    physiclaw_home, public_banner, mocker
):
    terminal, fetch = public_banner
    _plant_cache(physiclaw_home, "2026-10-14T12:00:00+00:00")
    mocker.patch.object(Path, "exists", side_effect=PermissionError("stat denied"))

    with freeze_time("2026-10-14T12:00:00+00:00"), redirect_stdout(terminal):
        uc.maybe_print_update_banner()

    assert "998.0.0 available" in terminal.getvalue()
    fetch.assert_not_called()


class _BrokenOutput(_TTY):
    def __init__(self, failure):
        super().__init__()
        self.failure = failure
        self.attempts = 0

    def isatty(self):
        if self.failure == "isatty":
            self.attempts += 1
            raise ValueError("closed stream")
        return True

    def write(self, text):
        self.attempts += 1
        raise BrokenPipeError("closed pipe")


@pytest.mark.parametrize("failure", ["isatty", "write"])
def test_regression_optional_banner_contains_output_failure(
    physiclaw_home, public_banner, failure
):
    _plant_cache(physiclaw_home, "2026-10-14T12:00:00+00:00")
    terminal = _BrokenOutput(failure)

    with freeze_time("2026-10-14T12:00:00+00:00"), redirect_stdout(terminal):
        uc.maybe_print_update_banner()

    assert terminal.attempts == 1


def test_banner_without_stdout_skips_network(public_banner):
    _, fetch = public_banner

    with redirect_stdout(None):
        uc.maybe_print_update_banner()

    fetch.assert_not_called()


@given(
    age=st.integers(min_value=-86400, max_value=864000),
    offset=st.integers(min_value=-23, max_value=23),
)
@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_banner_cache_ttl_depends_on_instant_not_timezone(
    physiclaw_home, public_banner, age, offset
):
    terminal, fetch = public_banner
    fetch.reset_mock()
    terminal.seek(0)
    terminal.truncate()
    now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
    checked = (now - timedelta(seconds=age)).astimezone(
        timezone(timedelta(hours=offset))
    )
    _plant_cache(physiclaw_home, checked.isoformat())

    with freeze_time(now), redirect_stdout(terminal):
        uc.maybe_print_update_banner()

    assert fetch.call_count == int(not 0 <= age < 7 * 86400)


@pytest.mark.parametrize("latest", [None, "", [], 123])
def test_banner_rechecks_fresh_cache_with_invalid_version(
    physiclaw_home, public_banner, latest
):
    terminal, fetch = public_banner
    _plant_cache(physiclaw_home, "2026-10-14T12:00:00+00:00", latest)

    with freeze_time("2026-10-14T12:00:00+00:00"), redirect_stdout(terminal):
        uc.maybe_print_update_banner()

    assert fetch.call_count == 1
    assert "999.0.0 available" in terminal.getvalue()


def test_regression_banner_closes_unconsumed_http_error_response(public_banner):
    terminal, fetch = public_banner
    body = io.BytesIO(b"unavailable")
    fetch.side_effect = urllib.error.HTTPError(
        "https://pypi.org", 503, "unavailable", None, body
    )

    with redirect_stdout(terminal):
        uc.maybe_print_update_banner()

    assert terminal.getvalue() == ""
    assert body.closed


class _AsciiTTY(io.TextIOWrapper):
    def isatty(self):
        return True


def test_banner_with_ascii_output_does_not_fail_diagnostics(
    physiclaw_home, public_banner
):
    _plant_cache(physiclaw_home, "2026-10-14T12:00:00+00:00")
    buffer = io.BytesIO()

    with _AsciiTTY(buffer, encoding="ascii") as terminal:
        with freeze_time("2026-10-14T12:00:00+00:00"), redirect_stdout(terminal):
            uc.maybe_print_update_banner()

        assert buffer.getvalue() == b""


@pytest.mark.parametrize(
    "checked_at, fetch_count",
    [
        ("2026-10-14T12:00:00+00:00", 0),
        ("2026-10-07T12:00:00.000001+00:00", 0),
        ("2026-10-07T12:00:00+00:00", 1),
        ("2026-10-14T12:00:00.000001+00:00", 1),
        ("2026-10-14T12:00:00", 0),
    ],
)
def test_banner_cache_expiration_boundaries(
    physiclaw_home, public_banner, checked_at, fetch_count
):
    terminal, fetch = public_banner
    _plant_cache(physiclaw_home, checked_at)

    with freeze_time("2026-10-14T12:00:00+00:00"), redirect_stdout(terminal):
        uc.maybe_print_update_banner()

    assert fetch.call_count == fetch_count


def test_banner_unusable_cache_path_still_displays_available_update(
    physiclaw_home, public_banner
):
    terminal, fetch = public_banner
    (physiclaw_home / "run" / "version-check.json").mkdir(parents=True)

    with redirect_stdout(terminal):
        uc.maybe_print_update_banner()

    assert fetch.call_count == 1
    assert "999.0.0 available" in terminal.getvalue()


@pytest.mark.parametrize(
    "payload", [b"broken", b"[]", b'{"info":null}', b'{"info":{"version":123}}']
)
def test_banner_bad_response_is_silent_and_closed(
    physiclaw_home, public_banner, payload
):
    terminal, fetch = public_banner
    response = io.BytesIO(payload)
    fetch.side_effect = None
    fetch.return_value = response

    with redirect_stdout(terminal):
        uc.maybe_print_update_banner()

    assert terminal.getvalue() == ""
    assert response.closed
    assert not (physiclaw_home / "run" / "version-check.json").exists()


def test_banner_first_lookup_creates_missing_home_and_reuses_cache(
    physiclaw_home, public_banner
):
    terminal, fetch = public_banner
    physiclaw_home.rmdir()

    with freeze_time("2026-10-14T12:00:00+00:00"), redirect_stdout(terminal):
        uc.maybe_print_update_banner()
        uc.maybe_print_update_banner()

    fetch.assert_called_once()
    assert json.loads(
        (physiclaw_home / "run" / "version-check.json").read_text(encoding="utf-8")
    ) == {"checked_at": "2026-10-14T12:00:00+00:00", "latest_version": "999.0.0"}


def test_banner_fetch_uses_pypi_with_short_timeout(public_banner):
    terminal, fetch = public_banner

    with redirect_stdout(terminal):
        uc.maybe_print_update_banner()

    assert fetch.call_args.args == ("https://pypi.org/pypi/physiclaw/json",)
    assert fetch.call_args.kwargs["timeout"] == 2.0
    assert "999.0.0 available" in terminal.getvalue()


def test_banner_network_interrupt_is_not_swallowed(public_banner):
    terminal, fetch = public_banner
    fetch.side_effect = KeyboardInterrupt

    with redirect_stdout(terminal), pytest.raises(KeyboardInterrupt):
        uc.maybe_print_update_banner()
