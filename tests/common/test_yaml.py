"""Authored YAML rejects aliases before construction can expand merges."""

import pytest
from ruamel.yaml.error import YAMLError

from physiclaw.common.yaml import safe_yaml


@pytest.mark.parametrize(
    "text",
    [
        "first: &same {value: text}\nsecond: {<<: [*same, *same]}",
        "first: &same text\nsecond: *same",
        "first: &same [*same]",
    ],
)
def test_safe_yaml_rejects_aliases_before_construction(text, mocker) -> None:
    yaml = safe_yaml()
    construct = mocker.spy(yaml.constructor, "construct_document")

    with pytest.raises(YAMLError, match="anchors/aliases"):
        yaml.load(text)

    construct.assert_not_called()


def test_safe_yaml_keeps_yaml12_scalars_and_literal_alias_syntax() -> None:
    yaml = safe_yaml()

    data = yaml.load('word: on\nflag: true\ntext: "*same"\n')

    assert data == {"word": "on", "flag": True, "text": "*same"}
