"""Session discovery stays within its root and tolerates damaged summaries."""

# Mutation review accepts message/traceback changes and diagnostic-only purge
# counters. A missing-mtime fallback changing 0 to 1 second after the Unix epoch
# is also accepted: either treats the unreadable entry as older than ordinary sessions.

import base64
import json
import os
import tempfile
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from symlink_helpers import symlink_to

from physiclaw.agent.trace import store


@pytest.fixture
def sessions(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    return root


@pytest.mark.parametrize(
    "payload", [b"[1]", b'"text"', b"1", b"true", b"null", b"{", b"\xff"]
)
def test_regression_load_summary_rejects_non_objects(tmp_path, payload):
    (tmp_path / "summary.json").write_bytes(payload)

    summary = store.load_summary(tmp_path)

    assert summary is None


@given(
    st.dictionaries(
        st.text(), st.one_of(st.none(), st.booleans(), st.integers(), st.text())
    )
)
def test_load_summary_preserves_json_objects(summary):
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

        loaded = store.load_summary(directory)

        assert loaded == summary


def test_load_summary_missing_file_returns_none(tmp_path):
    summary = store.load_summary(tmp_path)

    assert summary is None


def test_regression_recent_sessions_keeps_bad_summary_as_stub(sessions):
    session = sessions / "session-abc123"
    session.mkdir()
    (session / "summary.json").write_text('["broken"]', encoding="utf-8")

    rows = store.recent_sessions(sessions, 10)

    assert rows == [
        {
            "sid": session.name,
            "outcome": {
                "sentinel": "?",
                "recap": "(no summary — killed or still running)",
            },
        }
    ]


def test_recent_sessions_limits_newest_and_uses_directory_identity(sessions):
    old = sessions / "z-old"
    new = sessions / "a-new"
    old.mkdir()
    new.mkdir()
    (new / "summary.json").write_text('{"sid":"wrong","turns":7}', encoding="utf-8")
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))

    rows = store.recent_sessions(sessions, 1)

    assert rows == [{"sid": "a-new", "turns": 7}]


def test_recent_sessions_tied_mtimes_use_name_order(sessions):
    first = sessions / "a"
    second = sessions / "b"
    first.mkdir()
    second.mkdir()
    os.utime(first, (1, 1))
    os.utime(second, (1, 1))

    rows = store.recent_sessions(sessions, 10)

    assert [row["sid"] for row in rows] == ["b", "a"]


@pytest.mark.parametrize(
    "ref",
    [
        "",
        ".",
        "..",
        "../outside",
        "nested/child",
        "nested\\child",
        "C:outside",
        "bad\0name",
    ],
)
def test_regression_resolve_session_rejects_path_syntax(tmp_path, ref):
    root = tmp_path / "sessions"
    (root / "nested" / "child").mkdir(parents=True)
    (tmp_path / "outside").mkdir()

    with pytest.raises(LookupError, match="no session matches"):
        store.resolve_session(root, ref)


def test_regression_resolve_session_rejects_absolute_directory(tmp_path):
    root = tmp_path / "sessions"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()

    with pytest.raises(LookupError, match="no session matches"):
        store.resolve_session(root, str(outside))


@pytest.mark.parametrize("ref", ["outside-abc123", "abc123"])
def test_regression_resolve_session_rejects_external_directory_symlink(tmp_path, ref):
    root = tmp_path / "sessions"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    symlink_to(root / "outside-abc123", outside, target_is_directory=True)

    with pytest.raises(LookupError, match="no session matches"):
        store.resolve_session(root, ref)


def test_regression_recent_sessions_ignores_external_directory_symlinks(tmp_path):
    root = tmp_path / "sessions"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    symlink_to(root / "linked", outside, target_is_directory=True)
    (root / "README.md").write_text("not a session", encoding="utf-8")

    rows = store.recent_sessions(root, 10)

    assert rows == []


def test_resolve_session_keeps_internal_aliases_and_symlinked_roots(tmp_path):
    root = tmp_path / "sessions"
    real = root / "session-abc123"
    real.mkdir(parents=True)
    alias = root / "linked"
    symlink_to(alias, real, target_is_directory=True)
    linked_root = tmp_path / "linked-root"
    symlink_to(linked_root, root, target_is_directory=True)

    resolved = store.resolve_session(linked_root, "linked")

    assert resolved == linked_root / "linked"
    assert resolved.resolve() == real


@pytest.mark.parametrize("ref", ["abc123", "123"])
def test_resolve_session_finds_unique_suffix(tmp_path, ref):
    session = tmp_path / "session-abc123"
    session.mkdir()

    resolved = store.resolve_session(tmp_path, ref)

    assert resolved == session


@given(st.from_regex(r"[A-Za-z0-9_-]{1,24}", fullmatch=True))
@example(name="abcX123")
def test_find_session_dirs_preserves_valid_names_and_suffixes(name):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        session = root / f"session-{name}"
        session.mkdir()

        exact = store.find_session_dirs(root, session.name)
        suffix = store.find_session_dirs(root, name)

        assert exact == suffix == [session]


@pytest.mark.parametrize("ref", [".", ".."])
def test_resolve_session_rejects_dot_reference_even_with_suffix_match(sessions, ref):
    (sessions / f"ends{ref}").mkdir()

    with pytest.raises(LookupError, match="no session matches"):
        store.resolve_session(sessions, ref)


def test_resolve_session_exact_match_wins_over_suffix_matches(tmp_path):
    exact = tmp_path / "abc123"
    exact.mkdir()
    (tmp_path / "session-abc123").mkdir()

    resolved = store.resolve_session(tmp_path, "abc123")

    assert resolved == exact


def test_resolve_session_ambiguity_lists_sorted_matches(tmp_path):
    (tmp_path / "b-abc123").mkdir()
    (tmp_path / "a-abc123").mkdir()

    with pytest.raises(
        LookupError, match="ambiguous session 'abc123': a-abc123, b-abc123"
    ):
        store.resolve_session(tmp_path, "abc123")


def test_find_session_dirs_missing_root_returns_empty(tmp_path):
    matches = store.find_session_dirs(tmp_path / "missing", "abc123")

    assert matches == []


def test_recent_sessions_missing_root_returns_empty(tmp_path):
    rows = store.recent_sessions(tmp_path / "missing", 10)

    assert rows == []


@pytest.mark.parametrize(
    "error",
    [OSError("unreadable"), ValueError("invalid"), RuntimeError("symlink loop")],
)
def test_recent_sessions_skips_unresolvable_directory(sessions, mocker, error):
    (sessions / "broken").mkdir()
    mocker.patch.object(Path, "resolve", side_effect=error)

    rows = store.recent_sessions(sessions, 10)

    assert rows == []


def test_images_retries_after_write_failure(physiclaw_home, mocker):
    images = store.Images("session-abc123")
    encoded = base64.b64encode(b"frame").decode()
    save = store.save_image
    writer = mocker.patch.object(store, "save_image", side_effect=OSError("full disk"))
    failed = images.put(0, "image/png", encoded)
    writer.side_effect = save

    recovered = images.put(1, "image/png", encoded)
    reused = images.put(2, "image/png", encoded)

    assert failed == ""
    assert recovered.startswith("images/")
    assert recovered == reused
    assert (images.dir.parent / recovered).read_bytes() == b"frame"
    assert writer.call_count == 2


def test_images_preserves_distinct_frames(physiclaw_home):
    images = store.Images("session-abc123")

    first = images.put(0, "image/png", base64.b64encode(b"first").decode())
    second = images.put(0, "image/png", base64.b64encode(b"second").decode())

    assert first != second
    assert (images.dir.parent / first).read_bytes() == b"first"
    assert (images.dir.parent / second).read_bytes() == b"second"


def test_images_bad_base64_returns_empty_reference(physiclaw_home):
    images = store.Images("session-abc123")

    reference = images.put(0, "image/png", "not-base64")

    assert reference == ""
    assert list(images.dir.iterdir()) == []
