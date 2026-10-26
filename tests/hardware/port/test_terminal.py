import os

import pytest

from hardware.port import terminal


@pytest.mark.skipif(os.name != "nt", reason="Native Windows console input")
@pytest.mark.parametrize(
    "keys,expected",
    [
        (["x"], "x"),
        (["中"], "中"),
        (["\xe0", "H"], "special"),
        (["\x00", "K"], "special"),
    ],
)
def test_windows_console_key_handles_unicode_and_extended_keys(
    monkeypatch, keys, expected
):
    import msvcrt

    characters = iter(keys)
    monkeypatch.setattr(msvcrt, "getwch", lambda: next(characters))

    result = terminal.read_key()

    assert result == expected
