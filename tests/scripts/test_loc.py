"""LOC totals reflect working-tree text without counting assets or duplicates."""

import codecs
import io
import os
import shutil
import subprocess
import sys
from pathlib import PurePosixPath, PureWindowsPath

import pytest
from rich.console import Console
from symlink_helpers import symlink_to

from scripts import loc


def git(root, *args, input_data=None):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        input=input_data,
        capture_output=True,
        check=True,
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "--quiet")
    return root


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return path


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("src/main.py", "Code"),
        ("src/main.PY", "Code"),
        ("src/view.tsx", "Code"),
        ("src/styles.css", "Code"),
        ("Makefile", "Code"),
        ("Dockerfile", "Code"),
        ("CMakeLists.txt", "Code"),
        ("src/physiclaw/cli/static/test-assistive-touch.html", "Code"),
        ("src/physiclaw/cli/testdrive.py", "Code"),
        ("tests/conftest.py", "Test"),
        ("tests/ui_fixture.cjs", "Test"),
        ("src/__tests__/view.tsx", "Test"),
        ("src/view.test.js", "Test"),
        ("src/view.spec.ts", "Test"),
        ("scripts/grbl_solenoid_test.py", "Test"),
        ("test_example.py", "Test"),
        ("tests/data/fixture.json", "Text"),
        ("tests/README.md", "Text"),
        ("config.YAML", "Text"),
        ("pyproject.toml", "Text"),
        ("README.mdx", "Text"),
        ("LICENSE", "Text"),
        (".gitignore", "Text"),
    ],
)
@pytest.mark.parametrize("path_type", [PurePosixPath, PureWindowsPath])
def test_categories_keep_data_separate_from_test_source(name, expected, path_type):
    assert loc.category(path_type(name)) == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"", 0),
        (b"\n", 1),
        (b"\n\n", 2),
        (b"x", 1),
        (b"x\n", 1),
        (b"x\n\n", 2),
        (b"x\r\ny\r\n", 2),
        (b"x\ry\r", 2),
        (b"x\ry\nz\r\nlast", 4),
        (b'# comment\n"""docstring"""\n\nx = 1\n', 4),
        ('value = "a\u2028b"\n'.encode("utf-8"), 1),
    ],
)
def test_physical_lines_include_comments_blanks_and_unterminated_lines(
    repo, content, expected
):
    write(repo, "main.py", content)
    assert loc.collect(repo) == {"Code": expected, "Test": 0, "Text": 0}


@pytest.mark.parametrize(
    ("bom", "encoding"),
    [
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
        (codecs.BOM_UTF32_LE, "utf-32-le"),
        (codecs.BOM_UTF32_BE, "utf-32-be"),
    ],
)
def test_unicode_boms_are_not_mistaken_for_binary_files(repo, bom, encoding):
    write(repo, "config.json", bom + '{\r\n"label": "中文"\r\n}'.encode(encoding))
    assert loc.collect(repo) == {"Code": 0, "Test": 0, "Text": 3}


def test_python_encoding_cookie(repo):
    write(repo, "main.py", b'# coding: latin-1\nlabel = "caf\xe9"\n')
    assert loc.collect(repo) == {"Code": 2, "Test": 0, "Text": 0}


def test_working_tree_scope_and_category_totals(repo):
    write(repo, ".gitignore", "ignored/\n")
    source = write(repo, "main.py", "old = 1\n")
    deleted = write(repo, "deleted.py", "old = 1\n")
    write(repo, "ignored/tracked.py", "tracked = 1\n\n")
    git(repo, "add", ".")
    git(repo, "add", "--force", "ignored/tracked.py")
    source.write_bytes(b"# current\nvalue = 2\n\n")
    deleted.unlink()
    write(repo, "ignored/generated.py", "x\n" * 100)
    write(repo, "src/space name.js", "const value = 1;")
    write(repo, "tests/helper.py", "assert True\n")
    write(repo, "tests/fixture.json", "{}\n\n")
    write(repo, "README.md", "docs\n\n")
    for name in (
        "uv.lock",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "packages.lock.json",
        "bun.lockb",
        "diagram.svg",
        "manual.pdf",
    ):
        # ASCII assets must be excluded even if they happen to decode as UTF-8.
        write(repo, name, "asset\n" * 100)
    write(repo, "unknown-binary", b"\0binary\n")

    counts = loc.collect(repo)

    assert counts == {"Code": 6, "Test": 1, "Text": 5}


@pytest.mark.skipif(os.name == "nt", reason="Windows forbids newline filenames")
def test_newlines_in_filenames(repo):
    write(repo, "space and\nnewline.py", "x = 1\n")
    assert loc.collect(repo) == {"Code": 1, "Test": 0, "Text": 0}


def test_symlinked_files_and_cached_parent_directories_are_excluded(repo, tmp_path):
    write(repo, "src/main.py", "x = 1\n")
    git(repo, "add", ".")
    (repo / "src/main.py").unlink()
    (repo / "src").rmdir()
    external = tmp_path / "external"
    write(external, "main.py", "external\n" * 100)
    symlink_to(repo / "src", external, target_is_directory=True)
    symlink_to(repo / "alias.py", external / "main.py")
    assert loc.collect(repo) == {"Code": 0, "Test": 0, "Text": 0}


def test_merge_conflict_index_entries_count_the_working_file_once(repo):
    write(repo, "conflicted.py", "# current working tree\nvalue = 1\n")
    blob = git(repo, "hash-object", "-w", "--stdin", input_data=b"old\n")
    object_id = blob.stdout.strip().decode("ascii")
    entries = "".join(
        f"100644 {object_id} {stage}\tconflicted.py\n" for stage in (1, 2, 3)
    )
    git(repo, "update-index", "--index-info", input_data=entries.encode("ascii"))
    assert git(repo, "ls-files", "-z").stdout.count(b"conflicted.py\0") == 3

    assert loc.collect(repo) == {"Code": 2, "Test": 0, "Text": 0}


def test_invalid_text_fails_without_printing_a_partial_total(repo, monkeypatch, capsys):
    write(repo, "good.py", "x = 1\n")
    write(repo, "bad.json", b'{"label": "\xff"}\n')
    monkeypatch.setattr(loc, "ROOT", repo)
    monkeypatch.setenv("NO_COLOR", "1")

    assert loc.main() == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert "bad.json" in output.err
    assert "Cannot count lines" in output.err


def test_unicode_and_space_filenames_in_git(repo):
    write(repo, "src/界面 helper.py", "value = 1\n")
    git(repo, "add", ".")
    write(repo, "tests/测试 helper.py", "assert True\n")
    write(repo, "设置.json", "{}\n")
    assert loc.collect(repo) == {"Code": 1, "Test": 1, "Text": 1}


@pytest.mark.parametrize("encoding", ["ascii", "cp1252", "cp932", "gbk", "utf-8"])
def test_report_supports_legacy_and_unicode_streams(encoding):
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding=encoding, write_through=True)
    console = Console(file=stream, force_terminal=False, width=120)

    loc.show_report(console, {"Code": 1, "Test": 2, "Text": 3}, "PhysiClaw 项目")

    output = buffer.getvalue().decode(encoding)
    assert "PhysiClaw" in output
    assert "Line count" in output
    assert {
        line.split()[0]: line.split()[1]
        for line in output.splitlines()[3:]
        if line.strip()
    } == {"Total": "6", "Code": "1", "Test": "2", "Text": "3"}
    assert "\x1b" not in output
    if encoding == "utf-8":
        assert "项目  ·  Line count" in output
    elif encoding == "ascii":
        assert "|  Line count" in output


def test_errors_are_readable_on_ascii_stderr(repo, monkeypatch):
    write(repo, "bad-坏.json", b"\xff")
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="ascii", write_through=True)
    monkeypatch.setattr(loc.sys, "stderr", stream)
    monkeypatch.setattr(loc, "ROOT", repo)
    monkeypatch.setenv("NO_COLOR", "1")

    assert loc.main() == 1

    output = buffer.getvalue().decode("ascii")
    assert "Cannot count lines" in output
    assert "bad-" in output
    assert "\x1b" not in output


@pytest.mark.skipif(shutil.which("make") is None, reason="GNU Make is not installed")
def test_make_loc_in_checkout_with_spaces(repo):
    checkout = repo.with_name("repo with spaces 中文")
    repo.rename(checkout)
    (checkout / "scripts").mkdir()
    shutil.copyfile(loc.ROOT / "scripts/loc.py", checkout / "scripts/loc.py")
    shutil.copyfile(loc.ROOT / "Makefile", checkout / "Makefile")
    write(checkout, "tests/test_example.py", "assert True\n")
    write(checkout, "config.json", "{}\n")
    # Use the already-provisioned interpreter; this smoke test needs no network.
    python = f'"{sys.executable}" -X utf8={sys.flags.utf8_mode}'
    if sys.flags.warn_default_encoding:
        python += " -X warn_default_encoding -W error::EncodingWarning"
    command = [
        "make",
        "--no-print-directory",
        "loc",
        f"PY={python}",
        "NO_COLOR=1",
    ]
    if os.name == "nt":
        command.append("SHELL=cmd.exe")
    result = subprocess.run(
        command,
        cwd=checkout,
        capture_output=True,
        timeout=30,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    output = result.stdout.decode("utf-8")
    expected = loc.collect(checkout)
    expected["Total"] = sum(expected.values())
    rows = [line.split() for line in output.splitlines()]
    actual = {row[0]: int(row[1].replace(",", "")) for row in rows if len(row) == 2}
    assert actual == expected
    assert "repo with spaces 中文" in output
    assert "\x1b" not in output
