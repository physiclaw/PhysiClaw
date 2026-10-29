"""Keep repository text I/O independent of the machine's default code page.

Run this with the normal test suite, including on UTF-8 hosts where an omitted
encoding otherwise goes unnoticed. This checks recognizable file APIs; dynamic
receivers and forwarded arguments also need the runtime EncodingWarning checks
in ``make test-encoding``. Binary streams and the shared UTF-8 helpers already
have an explicit byte contract.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("src", "tests", "scripts", "hardware", "firmware")
PATH_TYPES = {"pathlib.Path", "pathlib.PosixPath", "pathlib.WindowsPath"}


def _qualified_name(node, aliases):
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{_qualified_name(node.value, aliases)}.{node.attr}"
    return ""


def _argument(node, name, index, default=None):
    """Read a keyword or positional argument, unwrapping literal values."""
    value = next((kw.value for kw in node.keywords if kw.arg == name), None)
    if value is None and index is not None and index < len(node.args):
        value = node.args[index]
    if isinstance(value, ast.Constant):
        return value.value
    return default if value is None else value


def _implicit_text_io(source):
    tree = ast.parse(source)
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and not node.level:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = _qualified_name(func, aliases)
        # A wrapper may forward the encoding. Runtime warnings check the actual
        # arguments; guessing here would reject valid forwarding wrappers.
        if any(kw.arg is None for kw in node.keywords) or any(
            isinstance(arg, ast.Starred) for arg in node.args
        ):
            continue
        if isinstance(func, ast.Attribute) and func.attr in {"read_text", "write_text"}:
            if name in {
                "physiclaw.common.text.read_text",
                "physiclaw.common.text.write_text",
            }:
                continue
            encoding_index = 0 if func.attr == "read_text" else 1
            if _qualified_name(func.value, aliases) in PATH_TYPES:
                encoding_index += 1  # Unbound Path.read_text(path, ...).
        elif name in {
            "open",
            "builtins.open",
            "io.open",
            "gzip.open",
            "bz2.open",
            "lzma.open",
        }:
            compressed = name in {"gzip.open", "bz2.open", "lzma.open"}
            mode = _argument(node, "mode", 1, "rb" if compressed else "r")
            if isinstance(mode, str) and (
                "t" not in mode if compressed else "b" in mode
            ):
                continue
            # LZMA's encoding is keyword-only; gzip and bz2 allow position 3.
            encoding_index = None if name == "lzma.open" else 3
        elif isinstance(func, ast.Attribute) and func.attr == "open":
            receiver = _qualified_name(func.value, aliases)
            if receiver in {"os", "tarfile", "webbrowser", "PIL.Image"}:
                continue  # These APIs do not open text streams.
            unbound = receiver in PATH_TYPES
            constructed = (
                isinstance(func.value, ast.Call)
                and _qualified_name(func.value.func, aliases) in PATH_TYPES
            )
            mode_index = int(unbound)
            # An explicit mode also covers Path instances held in variables.
            # Unknown zero-argument operations (e.g. step.open()) need runtime
            # evidence; the AST alone cannot establish the receiver's type.
            mode = _argument(
                node, "mode", mode_index, "r" if unbound or constructed else None
            )
            if (
                not isinstance(mode, str)
                or not mode
                or not set(mode) <= set("rwaxbt+")
                or "b" in mode
            ):
                continue
            encoding_index = mode_index + 2
        else:
            continue
        if _argument(node, "encoding", encoding_index) is None:
            violations.append(node.lineno)
    return violations


@pytest.mark.parametrize("directory", SOURCE_DIRS)
def test_text_files_have_explicit_encodings(directory):
    violations = []
    for path in sorted((ROOT / directory).rglob("*.py")):
        if path.is_relative_to(ROOT / "hardware" / "output"):
            continue
        for line in _implicit_text_io(path.read_text(encoding="utf-8")):
            violations.append(f"{path.relative_to(ROOT)}:{line}")
    assert not violations, (
        'Specify encoding="utf-8" for repository text files (or use the shared '
        "UTF-8 helpers); default encodings vary by host:\n" + "\n".join(violations)
    )


@pytest.mark.parametrize(
    "source",
    [
        "path.read_text()",
        "path.read_text(encoding=None)",
        "path.write_text('hello')",
        "path.write_text('hello', None)",
        "open(path)",
        "open(path, 'a')",
        "path.open('w')",
        "path.open(mode='r')",
        "from pathlib import Path; Path('data.txt').open()",
        "import pathlib as p; p.Path('data.txt').open()",
        "from pathlib import Path; Path.open(path)",
        "from pathlib import Path; Path.read_text(path)",
        "from pathlib import Path; Path.write_text(path, 'hello')",
        "import io; io.open('data.txt')",
        "import io; io.open('data.txt', 'r', -1)",
        "from io import open as text_open; text_open('data.txt')",
        "import builtins as b; b.open('data.txt')",
        "from builtins import open as read; read('data.txt')",
        "import gzip; gzip.open('data.gz', 'rt')",
        "import bz2; bz2.open('data.bz2', 'rt')",
        "import lzma; lzma.open('data.xz', 'wt')",
    ],
)
def test_encoding_guard_rejects_locale_dependent_io(source):
    assert _implicit_text_io(source) == [1]


@pytest.mark.parametrize(
    "source",
    [
        "path.read_text(encoding='utf-8')",
        "path.write_text('hello', 'utf-8')",
        "open(path, 'r', encoding='utf-8')",
        "path.open('a', encoding='utf-8')",
        "open(path, 'rb')",
        "path.open('a+b')",
        "step.open()",
        "from physiclaw.common import text as utf8; utf8.read_text(path)",
        "from physiclaw.common import text; text.write_text(path, 'hello')",
        "import physiclaw.common.text as utf8; utf8.read_text(path)",
        "from pathlib import Path; Path('data.txt').open(encoding='utf-8')",
        "from pathlib import Path; Path.open(path, encoding='utf-8')",
        "from pathlib import Path; Path.read_text(path, 'utf-8')",
        "import io; io.open('data.txt', 'r', -1, 'utf-8')",
        "import gzip; gzip.open('data.gz')",
        "import gzip; gzip.open('data.gz', 'r')",
        "import bz2; bz2.open('data.bz2', 'w')",
        "import lzma; lzma.open('data.xz', mode='r')",
        "import gzip; gzip.open('data.gz', 'rt', encoding='utf-8')",
        "import lzma; lzma.open('data.xz', 'rt', format=None, encoding='utf-8')",
        "import webbrowser; webbrowser.open('a')",
        "from PIL import Image; Image.open('r')",
        "import builtins; builtins.open(*args, **kwargs)",
    ],
)
def test_encoding_guard_accepts_explicit_io_and_utf8_helpers(source):
    assert not _implicit_text_io(source)
