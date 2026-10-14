"""Learned-box substitutions preserve prose and caller-owned skill maps.

Accepted mutation survivors: the unevaluated local fence annotation and three
equivalent marker-index changes. A matched fence contains at least three
identical characters, so its first and second characters are interchangeable.
"""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.agent.engine.skill import Skill
from physiclaw.agent.layout import fill_builtin_boxes, prune_builtin_skills

SEND = "[0.752, 0.868, 0.990, 0.918]"


def _skill(body):
    return Skill(
        name="im", description="Send a message", body=body, dir=Path("/builtin/im")
    )


def test_regression_pruning_an_incomplete_layout_returns_a_fresh_mapping():
    skills = {"screen-layout": object(), "im": object()}

    pruned = prune_builtin_skills(skills)
    pruned.pop("im")

    assert "im" in skills


def test_complete_layout_prunes_the_capture_skill_only(learned_layout):
    skills = {"screen-layout": object(), "im": object()}

    pruned = prune_builtin_skills(skills)

    assert pruned == {"im": skills["im"]}
    assert "screen-layout" in skills


def test_all_im_targets_are_filled_from_their_respective_landmarks(learned_layout):
    skill = _skill(
        "```\n{{input-hidden}} {{input-visible}} {{paste-button}} {{send}}\n```"
    )

    filled = fill_builtin_boxes({"im": skill})["im"].body

    assert filled == (
        "```\n[0.098, 0.900, 0.716, 0.950] [0.098, 0.586, 0.716, 0.634] "
        "[0.120, 0.520, 0.300, 0.566] " + SEND + "\n```"
    )


@pytest.mark.parametrize(
    "body, expected",
    [
        (
            "Prose {{send}}\n```python\n{{send}}\n```\nTail {{send}}",
            "Prose {{send}}\n```python\n" + SEND + "\n```\nTail {{send}}",
        ),
        (
            "Use inline ```` ``` {{send}} ```` here.",
            "Use inline ```` ``` {{send}} ```` here.",
        ),
        (
            "~~~~python\n{{send}}\n~~~~\n{{send}}",
            "~~~~python\n" + SEND + "\n~~~~\n{{send}}",
        ),
        (
            "````python\n```\n{{send}}\n````\n{{send}}",
            "````python\n```\n" + SEND + "\n````\n{{send}}",
        ),
        ("```python {{send}}\n{{send}}\n```", "```python {{send}}\n" + SEND + "\n```"),
        ("    ```\n    {{send}}\n    ```", "    ```\n    {{send}}\n    ```"),
        (
            "```\n```X\n{{send}}\n```\n{{send}}",
            "```\n```X\n" + SEND + "\n```\n{{send}}",
        ),
        (
            "```` inline `code`\n```\n{{send}}\n```\n{{send}}",
            "```` inline `code`\n```\n" + SEND + "\n```\n{{send}}",
        ),
        (
            "~~~js `code`\n{{send}}\n~~~\n{{send}}",
            "~~~js `code`\n" + SEND + "\n~~~\n{{send}}",
        ),
        (
            "  ```python\r\n{{send}}\r\n  ```\r\n{{send}}",
            "  ```python\r\n" + SEND + "\r\n  ```\r\n{{send}}",
        ),
    ],
)
def test_regression_only_fenced_code_bodies_receive_coordinates(
    learned_layout, body, expected
):
    original = _skill(body)

    result = fill_builtin_boxes({"im": original})["im"]

    assert result.body == expected
    assert original.body == body
    assert result.description == original.description and result.dir == original.dir


def test_unlearned_layout_preserves_placeholders_and_mapping():
    original = {"im": _skill("```\n{{send}}\n```")}

    result = fill_builtin_boxes(original)

    assert result is not original and result["im"] is original["im"]


@given(
    prose=st.text(alphabet=st.characters(blacklist_characters="`~\r\n"), max_size=60)
)
def test_prose_substitution_is_never_triggered_by_inline_fence_text(prose):
    from unittest.mock import patch

    from physiclaw.agent.layout import store

    body = prose + " ```` ``` {{send}} ````\n```\n{{send}}\n```\n" + prose
    data = {field: [0, 0, 1, 1] for field in store.ALL_FIELDS}
    with patch.object(store, "_load", return_value=data):
        result = fill_builtin_boxes({"im": _skill(body)})["im"].body

    assert (
        result
        == prose
        + " ```` ``` {{send}} ````\n```\n[0.000, 0.000, 1.000, 1.000]\n```\n"
        + prose
    )
