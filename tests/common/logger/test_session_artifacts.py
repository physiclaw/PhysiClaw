"""Shared session artifacts: atomic JSON, image naming and summary schema."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from freezegun import freeze_time
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from physiclaw.common.logger import session_artifacts as artifacts
from physiclaw.common.logger.session_artifacts import (
    append_stats,
    build_summary,
    image_filename,
    write_json_atomic,
)

# ---------- append_stats ----------


def test_append_stats_creates_then_appends_one_line_per_session(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "log"  # not pre-created — helper mkdirs

    append_stats(log_dir, {"sid": "a", "turns": 3})
    append_stats(log_dir, {"sid": "b", "turns": 5})

    lines = (log_dir / "session-stats.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(ln)["sid"] for ln in lines] == ["a", "b"]


# ---------- helpers ----------


def test_write_json_atomic_leaves_no_tmp_file(tmp_path: Path) -> None:
    d = tmp_path / "session"
    d.mkdir()
    path = d / "summary.json"
    write_json_atomic(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    assert list(d.iterdir()) == [path]


def test_regression_atomic_json_writers_do_not_share_temporary_file(
    tmp_path, mocker
) -> None:
    directory = tmp_path / "records"
    directory.mkdir()
    path = directory / "state.json"
    rendezvous = Barrier(2)
    replace = Path.replace

    def simultaneous_replace(source, target):
        rendezvous.wait(timeout=5)
        return replace(source, target)

    mocker.patch.object(Path, "replace", simultaneous_replace)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write_json_atomic, path, {"writer": 1})
        second = pool.submit(write_json_atomic, path, {"writer": 2})
        first.result(timeout=5)
        second.result(timeout=5)

    assert json.loads(path.read_text()) in [{"writer": 1}, {"writer": 2}]
    assert list(directory.iterdir()) == [path]


def test_write_json_atomic_replace_failure_preserves_previous_file(
    tmp_path, mocker
) -> None:
    directory = tmp_path / "records"
    directory.mkdir()
    path = directory / "state.json"
    path.write_text('{"previous": true}', encoding="utf-8")
    mocker.patch.object(Path, "replace", side_effect=PermissionError("cannot replace"))

    with pytest.raises(PermissionError, match="cannot replace"):
        write_json_atomic(path, {"new": True})

    assert json.loads(path.read_text()) == {"previous": True}
    assert list(directory.iterdir()) == [path]


def test_write_json_atomic_write_failure_preserves_previous_file(
    tmp_path, mocker
) -> None:
    directory = tmp_path / "records"
    directory.mkdir()
    path = directory / "state.json"
    path.write_text('{"previous": true}', encoding="utf-8")
    mocker.patch(
        "physiclaw.common.logger.session_artifacts.write_text",
        side_effect=OSError("disk full"),
    )

    with pytest.raises(OSError, match="disk full"):
        write_json_atomic(path, {"new": True})

    assert json.loads(path.read_text()) == {"previous": True}
    assert list(directory.iterdir()) == [path]


# Each example replaces the same isolated file; no example depends on its old contents.
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    st.dictionaries(
        st.text(), st.one_of(st.text(), st.integers(), st.booleans(), st.none())
    )
)
def test_atomic_json_roundtrips_unicode_objects(tmp_path, payload) -> None:
    path = tmp_path / "roundtrip.json"

    write_json_atomic(path, payload)

    assert json.loads(path.read_text(encoding="utf-8")) == payload


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.integers(min_value=0xD800, max_value=0xDFFF), st.text())
def test_regression_atomic_json_roundtrips_unpaired_surrogates(
    tmp_path, codepoint, suffix
):
    path = tmp_path / "surrogate.json"
    payload = {"recap": chr(codepoint) + " " + suffix}

    write_json_atomic(path, payload)

    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_regression_concurrent_image_saves_keep_both_frames(tmp_path, mocker) -> None:
    rendezvous = Barrier(2)
    open_file = Path.open
    mocker.patch.object(artifacts, "image_filename", return_value="same-tick.jpg")

    def simultaneous_open(path, mode="r", *args, **kwargs):
        if path.name == "same-tick.jpg" and mode in {"wb", "xb"}:
            rendezvous.wait(timeout=5)
        return open_file(path, mode, *args, **kwargs)

    mocker.patch.object(Path, "open", simultaneous_open)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(artifacts.save_image, tmp_path, 1, "image/jpeg", "Zmlyc3Q=")
        second = pool.submit(
            artifacts.save_image, tmp_path, 1, "image/jpeg", "c2Vjb25k"
        )
        first_name = first.result(timeout=5)
        second_name = second.result(timeout=5)

    assert first_name != second_name
    assert (tmp_path / first_name).read_bytes() == b"first"
    assert (tmp_path / second_name).read_bytes() == b"second"


def test_regression_append_stats_preserves_unpaired_surrogate(tmp_path) -> None:
    summary = {"recap": "unencodable \ud800"}

    append_stats(tmp_path, summary)

    assert (
        json.loads((tmp_path / "session-stats.jsonl").read_text(encoding="utf-8"))
        == summary
    )


def test_regression_ensure_readme_repairs_invalid_utf8(tmp_path) -> None:
    path = tmp_path / "README.md"
    path.write_bytes(b"\xff")

    artifacts.ensure_readme(tmp_path, "format documentation")

    assert path.read_text(encoding="utf-8") == "format documentation"


def test_image_filename_stamps_time_and_turn() -> None:
    with freeze_time("2026-04-28T10:45:42.123456"):
        assert image_filename(20, "image/jpeg") == "104542_123_t20.jpg"
        assert image_filename(3, "application/x-unknown") == "104542_123_t3.bin"


@pytest.mark.parametrize("name", ["frame.jpg", "frame.png", "截图.webp"])
def test_resolve_image_accepts_recorded_regular_files(tmp_path, name):
    images = tmp_path / "images"
    images.mkdir()
    path = images / name
    path.write_bytes(b"frame")

    result = artifacts.resolve_image(tmp_path, f"images/{name}")

    assert result == path.resolve()


@pytest.mark.parametrize(
    "ref",
    [
        None,
        1,
        {},
        [],
        "",
        "frame.jpg",
        "../frame.jpg",
        "/tmp/frame.jpg",
        "C:/frame.jpg",
        "images/",
        "images/.",
        "images/..",
        "images/../private.jpg",
        "images/sub/frame.jpg",
        "images/..\\private.jpg",
        "images/C:frame.jpg",
        "images/frame.jpg:stream",
        "images/null\0.jpg",
    ],
)
def test_resolve_image_rejects_nonportable_or_escaping_references(tmp_path, ref):
    result = artifacts.resolve_image(tmp_path, ref)

    assert result is None


@pytest.mark.parametrize(
    "kind",
    ["missing", "directory", "symlink_loop", "external_symlink", "images_symlink"],
)
def test_resolve_image_requires_a_contained_regular_file(tmp_path, kind):
    session = tmp_path / "session"
    images = session / "images"
    images.mkdir(parents=True)
    target = images / "frame.jpg"
    _prepare_image_boundary(tmp_path, images, target, kind)

    result = artifacts.resolve_image(session, "images/frame.jpg")

    assert result is None


def _prepare_image_boundary(root, images, target, kind):
    if kind == "directory":
        target.mkdir()
    elif kind == "symlink_loop":
        target.symlink_to(target)
    elif kind == "external_symlink":
        outside = root / "private.jpg"
        outside.write_bytes(b"private")
        target.symlink_to(outside)
    elif kind == "images_symlink":
        outside = root / "other"
        outside.mkdir()
        (outside / "frame.jpg").write_bytes(b"private")
        images.rmdir()
        images.symlink_to(outside, target_is_directory=True)


@pytest.mark.parametrize("error_type", [OSError, ValueError, RuntimeError])
def test_resolve_image_filesystem_errors_are_unavailable_frames(
    tmp_path, mocker, error_type
):
    mocker.patch.object(Path, "resolve", side_effect=error_type("unavailable"))

    result = artifacts.resolve_image(tmp_path, "images/frame.jpg")

    assert result is None


@pytest.mark.parametrize("ref", ["images/frame.jpg:stream", "images/..\\private.jpg"])
def test_resolve_image_forbids_windows_path_syntax_even_if_the_file_exists(
    tmp_path, mocker, ref
):
    mocker.patch.object(Path, "is_file", return_value=True)

    result = artifacts.resolve_image(tmp_path, ref)

    assert result is None


@pytest.fixture
def image_directory(tmp_path):
    directory = tmp_path / "images"
    directory.mkdir()
    (directory / "frame.jpg").write_bytes(b"frame")
    return tmp_path


# Each example only reads the same fixture directory.
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    st.one_of(
        st.just("images/frame.jpg"),
        st.text(max_size=150),
        st.text(max_size=150).map(lambda s: "images/" + s),
    )
)
def test_resolve_image_arbitrary_references_cannot_return_outside_files(
    image_directory, ref
):
    result = artifacts.resolve_image(image_directory, ref)

    assert result in (None, (image_directory / "images" / "frame.jpg").resolve())


# ---------- summary schema v1 parity ----------


def test_build_summary_zero_input_tokens_yields_zero_pct() -> None:
    s = build_summary(
        sid="s",
        started_at="2026-04-28T10:00:00.000",
        duration_s=1.0,
        model_ref="p/m",
        prompt_hash="",
        triggers=[],
        sentinel=None,
        recap="",
        crashed=False,
        turns=0,
        provider_calls=0,
        provider_time_ms=0,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        tool_calls={},
        errors={},
        stuck_events=0,
        images=0,
        env={},
    )
    assert s["usage"]["cache_hit_pct"] == 0.0
    assert "cost_usd" not in s
