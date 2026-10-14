"""Shared session-artifact surface: DailyLogWriter banner/rollover/footer,
atomic JSON, image naming, and — the reason the module exists —
summary.json schema parity between the two engines' finalize paths."""

from __future__ import annotations

import functools
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
    DailyLogWriter,
    append_stats,
    build_summary,
    image_filename,
    write_json_atomic,
)

# ---------- append_stats ----------


def test_append_stats_creates_then_appends_one_line_per_session(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "log" / "engine"  # not pre-created — helper mkdirs

    append_stats(log_dir, {"sid": "a", "turns": 3})
    append_stats(log_dir, {"sid": "b", "turns": 5})

    lines = (log_dir / "stats.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(ln)["sid"] for ln in lines] == ["a", "b"]


# ---------- DailyLogWriter ----------


def test_daily_log_writer_banner_then_timestamped_lines(tmp_path: Path) -> None:
    with freeze_time("2026-04-28T10:15:30"):
        w = DailyLogWriter(tmp_path / "log", "engine")
        w.line("hello")
        w.close()

    text = (tmp_path / "log" / "engine-2026-04-28.log").read_text()
    assert text == f"\n{'=' * 60}\n[10:15:30] hello\n"


def test_daily_log_writer_footer_closes_the_session_block(tmp_path: Path) -> None:
    w = DailyLogWriter(tmp_path, "claude")
    w.footer()
    w.close()

    text = next(tmp_path.glob("claude-*.log")).read_text()
    assert text.endswith(f"{'=' * 60}\n\n")


def test_daily_log_writer_rolls_over_at_midnight(tmp_path: Path) -> None:
    with freeze_time("2026-04-28T23:59:59") as ft:
        w = DailyLogWriter(tmp_path, "claude")
        w.line("before")
        ft.move_to("2026-04-29T00:00:01")
        w.line("after")
        w.close()

    old = (tmp_path / "claude-2026-04-28.log").read_text()
    new = (tmp_path / "claude-2026-04-29.log").read_text()
    assert "ROLLOVER → claude-2026-04-29.log" in old
    assert "ROLLOVER ← continued from previous day" in new
    assert "after" in new


def test_daily_log_writer_close_is_idempotent(tmp_path: Path) -> None:
    w = DailyLogWriter(tmp_path, "engine")
    w.close()
    w.close()


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
    mocker.patch.object(Path, "write_text", side_effect=OSError("disk full"))

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


def test_regression_daily_log_retries_after_failed_rollover(tmp_path, mocker) -> None:
    with freeze_time("2026-04-28T23:59:59") as clock:
        writer = DailyLogWriter(tmp_path, "engine")
        clock.move_to("2026-04-29T00:00:01")
        fail_once = mocker.patch.object(
            artifacts, "_open_log_file", side_effect=PermissionError
        )
        writer.line("during failure")
        mocker.stop(fail_once)

        writer.line("after recovery")
        writer.close()

    assert "during failure" in (tmp_path / "engine-2026-04-28.log").read_text()
    assert "after recovery" in (tmp_path / "engine-2026-04-29.log").read_text()


def test_append_stats_invalid_utf8_is_nonfatal(tmp_path, caplog) -> None:
    summary = {"recap": "unencodable \ud800"}

    append_stats(tmp_path, summary)

    assert "stats.jsonl append failed" in caplog.text


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


def _key_tree(d: dict) -> dict:
    return {k: _key_tree(v) if isinstance(v, dict) else None for k, v in d.items()}


@functools.cache
def _engine_summary() -> dict:
    from physiclaw.agent.trace.trace import _Summary

    s = _Summary("eng-sid")
    for event in (
        {"event": "env", "physiclaw": "0.3.x", "os": "darwin"},
        {"event": "wake", "model_ref": "qwen/qwen3.6-plus", "triggers": []},
        {"event": "prefix_pinned", "hash": "abc"},
        {"event": "response", "turn": 0, "elapsed_ms": 100},
        {
            "event": "usage",
            "turn": 0,
            "call": "turn",
            "model": "qwen/qwen3.6-plus",
            "input": 1000,
            "input_new": 200,
            "cache_read": 800,
            "cache_write": 0,
            "output": 5,
        },
        {"event": "tool_result", "turn": 0, "name": "peek"},
        {"event": "done", "sentinel": "DONE", "recap": "ok"},
    ):
        s.observe(event)
    return s.finalize(images=1)


@functools.cache
def _claude_summary() -> dict:
    from physiclaw.agent.claude.session_log import _ClaudeSummary
    from physiclaw.agent.runtime.hook import Trigger

    s = _ClaudeSummary(
        "claude-sid",
        [Trigger(description="wake", source="cron")],
        model_ref="claude-code/claude-sonnet-4-6",
        prompt_hash="abc",
    )
    for event in (
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [{"type": "tool_use", "name": "peek", "input": {}}],
            },
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "is_error": True}]},
        },
        {
            "type": "result",
            "duration_api_ms": 100,
            "total_cost_usd": 0.01,
            "usage": {
                "input_tokens": 200,
                "cache_read_input_tokens": 800,
                "cache_creation_input_tokens": 0,
                "output_tokens": 5,
            },
        },
    ):
        s.observe(event)
    return s.finalize(images=1)


def test_both_engines_emit_the_same_summary_key_tree() -> None:
    """The parity contract: `physiclaw logs` / `jq` read both engines'
    summary.json with one schema, modulo the declared per-engine keys:
    cost_usd is claude-only (the CLI reports it); tool_time_ms, verdicts,
    and conductor_turns are engine-only (the claude stream carries no
    per-tool timing, camera verdicts, or synthesized playbook turns)."""
    engine_full, claude_full = _engine_summary(), _claude_summary()
    engine, claude = _key_tree(engine_full), _key_tree(claude_full)

    assert claude.pop("cost_usd", "absent") is None  # present, scalar
    assert engine.pop("tool_time_ms", "absent") is None  # present, scalar
    assert engine.pop("conductor_turns", "absent") is None  # present, scalar
    assert engine.pop("micro_calls", "absent") is None  # present, scalar
    assert isinstance(engine.pop("verdicts"), dict)
    # Per-model usage is engine-only (the claude CLI reports one model).
    assert isinstance(engine["usage"].pop("by_model"), dict)
    # `env` is engine-populated data (the engine relays its env event
    # verbatim), not part of the constructed schema — opaque here.
    assert isinstance(engine.pop("env"), dict) and isinstance(claude.pop("env"), dict)
    assert claude == engine
    # Same top-level ordering too — the files diff cleanly across engines.
    _PER_ENGINE = {
        "cost_usd",
        "tool_time_ms",
        "verdicts",
        "conductor_turns",
        "micro_calls",
    }
    assert [k for k in claude_full if k not in _PER_ENGINE] == [
        k for k in engine_full if k not in _PER_ENGINE
    ]


def test_claude_usage_folds_cache_tokens_into_input_tokens() -> None:
    # 200 fresh + 800 cache-read + 0 cache-create → the engine convention
    # where input_tokens is the total and cache_hit_pct derives from it.
    usage = _claude_summary()["usage"]
    assert usage["input_tokens"] == 1000
    assert usage["cache_hit_pct"] == 80.0


def test_claude_errors_render_full_engine_key_set() -> None:
    assert _claude_summary()["errors"] == {
        "blocked_plan": 0,
        "blocked_layout": 0,
        "blocked_stuck": 0,
        "invalid_args": 0,
        "unknown_tool": 0,
        "tool_errors": 1,
        "correctives": 0,
        "provider_failures": 0,
    }


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
