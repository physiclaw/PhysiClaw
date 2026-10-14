from pathlib import Path

import pytest

from physiclaw.conductor.load.install import read_template


@pytest.fixture
def template_root(tmp_path: Path) -> Path:
    root = tmp_path / "templates" / "demo"
    (root / "macros").mkdir(parents=True)
    (root / "APP.yml").write_text(
        "kind: manifest\nschema: 1\napp: demo\ndescription: fixture\n",
        encoding="utf-8",
    )
    (root / "macros" / "send.yml").write_text(
        "kind: macro\nschema: 1\nname: send\ndescription: Send to <<CONTACT>>\n"
        "enabled: false\nsteps:\n  - send_to_clipboard: '<<CONTACT>>'\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("original notes\n", encoding="utf-8")
    return root


@pytest.fixture
def template(template_root):
    return read_template(template_root)
