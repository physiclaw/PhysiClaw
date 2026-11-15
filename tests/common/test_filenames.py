"""Filesystem identity rules are identical on every supported OS."""

import pytest

from physiclaw.common.filenames import is_portable_name


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "name.",
        "name ",
        "CON",
        "nul.txt",
        "aux",
        "PRN.log",
        "com1",
        "LPT9.ext",
        "COM¹",
        "LPT².txt",
        "CONIN$",
        "conout$",
        "nul .txt",
        "a/b",
        "a\\b",
        "a:b",
        "a?b",
        'a"b',
        "a\n",
        "a\x00",
    ],
)
def test_rejects_windows_aliases_and_invalid_components_on_every_os(name):
    assert not is_portable_name(name)


@pytest.mark.parametrize(
    "name", ["alpha", "foo.bar", "my-skill_2", "console", "com10", "nul-safe", "测试"]
)
def test_preserves_portable_components(name):
    assert is_portable_name(name)
