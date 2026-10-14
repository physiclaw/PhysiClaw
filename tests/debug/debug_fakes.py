"""Shared builders for the debug-harness tests."""

from __future__ import annotations

from physiclaw.common import paths


def write_channel_pages(anchors: tuple[str, ...] = ("MyChat",)) -> None:
    """A minimal channel pack declaring the thread page — what the
    renderer draws anchors from and the matcher scores against."""
    root = paths.playbooks_dir() / "channel" / "wechat"
    root.mkdir(parents=True, exist_ok=True)
    lines = "".join(f'      - "{a}"\n' for a in anchors)
    (root / "APP.yml").write_text(
        "kind: manifest\nschema: 1\napp: wechat\ndescription: test channel\n"
        f"pages:\n  thread:\n    description: the user's chat thread\n    anchors:\n{lines}",
        encoding="utf-8",
    )
