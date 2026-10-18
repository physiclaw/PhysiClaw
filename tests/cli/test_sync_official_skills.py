"""Tests for `physiclaw.cli.sync_official_skills` — the `skills sync official`
client.

The network is mocked at the module's `http_get` seam: a routing fake maps
each URL to canned bytes (or raises a 404 HTTPError for anything unmapped),
so no test hits the wire. Packs are built in-memory as real STORED zips so
extraction, zip-slip, and checksum paths run for real.

Mutation review accepts diagnostic/progress formatting, abbreviated hashes,
private scratch/lock names and contents, JSON indentation, and debug traceback
verbosity. missing_ok variants are equivalent under suppress(OSError); an
unknown default CI value still enables sync. Adding uppercase X to the hex
alphabet is equivalent because the input is lowercased first. Windows locks
can extend past EOF, so lock-file initialization variants do not change the
byte-zero lock (https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking).
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import logging
import os
import sys
import threading
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from freezegun import freeze_time
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

osk = importlib.import_module("physiclaw.cli.sync_official_skills")
skills_mod = importlib.import_module("physiclaw.cli.skills")

BASE = "https://site.test/downloads/official-skills"
LATEST_URL = f"{BASE}/latest.json"
ZIP_URL = f"{BASE}/physiclaw_official_skills.zip"
SHA_URL = f"{ZIP_URL}.sha256"

runner = CliRunner()


# ---------- fixtures / helpers ----------


class _FakeResp:
    """Supports both whole-body `read()` (latest.json / sha256) and the
    chunked `read(n)` loop the streaming zip download uses."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def getheader(self, _name: str) -> str | None:
        # No Content-Length → `_http.stream` quietly streams (no bar).
        return None

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            chunk = self._data[self._pos :]
        else:
            chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk


def _make_http_get(routes: dict[str, bytes], calls: list[str] | None = None):
    def _get(url: str, timeout: int = 120):
        if calls is not None:
            calls.append(url)
        if url in routes:
            return _FakeResp(routes[url])
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    return _get


def _skill_md(name: str) -> bytes:
    return f"---\nname: {name}\n---\n# {name}\n".encode()


def _skill_hash(skill_md: bytes) -> str:
    """The site builder's per-skill digest for a single-file `SKILL.md` skill:
    sha256(relpath + NUL + bytes), prefixed `sha256:`."""
    h = hashlib.sha256()
    h.update(b"SKILL.md")
    h.update(b"\x00")
    h.update(skill_md)
    return f"sha256:{h.hexdigest()}"


def _build_pack(
    commit: str,
    skill_names: list[str],
    *,
    declared: int | None = None,
    bad_hash: tuple[str, ...] = (),
):
    """A valid STORED pack: source.json + skills/<name>/SKILL.md each, with the
    manifest carrying each shipped skill's REAL content hash (so the happy path
    verifies clean). `declared` pads the manifest with un-shipped ghost skills
    (no hash → exercises count drift); `bad_hash` names shipped skills whose
    manifest hash is deliberately wrong (exercises per-skill mismatch)."""
    contents = {n: _skill_md(n) for n in skill_names}
    skills = [
        {
            "name": n,
            "path": f"skills/{n}",
            "description": "d",
            "hash": ("sha256:" + "f" * 64)
            if n in bad_hash
            else _skill_hash(contents[n]),
        }
        for n in skill_names
    ]
    if declared is not None:  # pad with ghosts the pack doesn't actually ship
        skills += [
            {"name": f"ghost{i}", "path": f"skills/ghost{i}", "description": "d"}
            for i in range(declared - len(skill_names))
        ]
    src = {
        "schemaVersion": 1,
        "package": "physiclaw-official-skills",
        "commit": commit,
        "repo": "physiclaw/PhysiClaw",
        "subdir": "skills",
        "builtAt": "2026-07-08T09:46:19Z",
        "skills": skills,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(zipfile.ZipInfo("source.json"), json.dumps(src))
        for n in skill_names:
            zf.writestr(zipfile.ZipInfo(f"skills/{n}/SKILL.md"), contents[n])
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def _routes(
    commit: str,
    skill_names: list[str],
    *,
    declared: int | None = None,
    bad_hash: tuple[str, ...] = (),
):
    data, hexd = _build_pack(commit, skill_names, declared=declared, bad_hash=bad_hash)
    return {
        LATEST_URL: json.dumps(
            {
                "schemaVersion": 1,
                "commit": commit,
                "builtAt": "x",
                "skillCount": len(skill_names),
            }
        ).encode(),
        ZIP_URL: data,
        SHA_URL: f"{hexd}  physiclaw_official_skills.zip\n".encode(),
    }


@pytest.fixture
def official_home(tmp_path: Path, mocker) -> Path:
    d = tmp_path / "official"
    mocker.patch.object(osk.paths, "official_dir", return_value=d)
    mocker.patch.object(
        osk,
        "_load_config",
        return_value=SimpleNamespace(skills=SimpleNamespace(official_base_url=BASE)),
    )
    return d


def _patch_net(mocker, routes: dict[str, bytes], calls: list[str] | None = None):
    mocker.patch.object(osk, "http_get", _make_http_get(routes, calls))


# ---------- freshness / not-published ----------


def test_not_published_is_not_an_error(official_home: Path, mocker, capsys) -> None:
    _patch_net(mocker, {})  # latest.json 404s

    osk.sync()  # no raise

    assert "no official skills published yet" in capsys.readouterr().out
    assert not (official_home / "skills").exists()
    assert not (official_home / osk.SYNC_STATE_FILE).exists()


def test_latest_missing_commit_errors(official_home: Path, mocker, capsys) -> None:
    _patch_net(mocker, {LATEST_URL: json.dumps({"schemaVersion": 1}).encode()})

    with pytest.raises(osk.SyncError, match="missing 'commit'"):
        osk.sync()


# ---------- fresh install ----------


@freeze_time("2026-10-15 08:09:10")
def test_fresh_install_mounts_and_records_state(
    official_home: Path, mocker, capsys
) -> None:
    _, hexd = _build_pack("a" * 40, ["jd", "wechat"])
    _patch_net(mocker, _routes("a" * 40, ["jd", "wechat"]))

    osk.sync()

    assert (official_home / "skills" / "jd" / "SKILL.md").is_file()
    assert (official_home / "skills" / "wechat" / "SKILL.md").is_file()
    assert (official_home / "source.json").is_file()

    state = json.loads((official_home / osk.SYNC_STATE_FILE).read_text())
    assert state["commit"] == "a" * 40
    assert state["skillCount"] == 2
    assert state["zipHash"] == f"sha256:{hexd}"
    assert state["url"] == ZIP_URL
    assert state["schemaVersion"] == 1
    assert state["syncedAt"] == "2026-10-15T08:09:10Z"
    # No staging dir left behind.
    assert not (official_home / osk.STAGING_DIR).exists()
    assert "synced 2 official skill(s)" in capsys.readouterr().out


# ---------- idempotence / force ----------


def test_up_to_date_skips_download(official_home: Path, mocker, capsys) -> None:
    official_home.mkdir(parents=True)
    (official_home / "skills").mkdir()
    (official_home / "source.json").write_text("{}")
    (official_home / osk.SYNC_STATE_FILE).write_text(json.dumps({"commit": "a" * 40}))
    calls: list[str] = []
    _patch_net(mocker, _routes("a" * 40, ["jd"]), calls)

    osk.sync()

    assert "already up to date" in capsys.readouterr().out
    assert ZIP_URL not in calls  # freshness shortcut — never fetched the pack


def test_force_re_syncs_when_up_to_date(official_home: Path, mocker, capsys) -> None:
    official_home.mkdir(parents=True)
    (official_home / osk.SYNC_STATE_FILE).write_text(json.dumps({"commit": "a" * 40}))
    calls: list[str] = []
    _patch_net(mocker, _routes("a" * 40, ["jd"]), calls)

    osk.sync(force=True)

    assert ZIP_URL in calls
    assert (official_home / "skills" / "jd").is_dir()


def test_changed_commit_replaces_and_drops_removed_skills(
    official_home: Path, mocker
) -> None:
    # First sync: two skills at commit A.
    _patch_net(mocker, _routes("a" * 40, ["jd", "wechat"]))
    osk.sync()
    assert (official_home / "skills" / "wechat").is_dir()

    # Second sync: commit B ships only jd — wechat must disappear.
    _patch_net(mocker, _routes("b" * 40, ["jd"]))
    osk.sync()

    assert (official_home / "skills" / "jd").is_dir()
    assert not (official_home / "skills" / "wechat").exists()
    state = json.loads((official_home / osk.SYNC_STATE_FILE).read_text())
    assert state["commit"] == "b" * 40
    assert state["skillCount"] == 1


def test_resync_swap_never_rmtrees_live_tree(official_home: Path, mocker) -> None:
    # Two-rename swap: the live tree is parked via os.replace (rename) and
    # only the parked trash is rmtree'd — an rmtree of the live path would
    # reopen the multi-second empty-tree window a concurrent session
    # snapshot could observe.
    _patch_net(mocker, _routes("a" * 40, ["jd"]))
    osk.sync()

    live = official_home / "skills"
    rm_spy = mocker.spy(osk.shutil, "rmtree")
    _patch_net(mocker, _routes("b" * 40, ["jd"]))
    osk.sync()

    assert (live / "jd" / "SKILL.md").is_file()
    assert all(Path(c.args[0]) != live for c in rm_spy.call_args_list)
    # The parked trash was cleaned up after the swap.
    assert not list(official_home.glob(".sync-trash*"))


def test_swap_reclaims_stale_trash_from_a_crashed_run(
    official_home: Path, mocker
) -> None:
    # A crash between the two swap renames leaves the parked tree at the
    # fixed `.sync-trash` name. The next sync must reclaim it before
    # parking a new one, or the stale tree blocks/pollutes the swap.
    official_home.mkdir(parents=True)
    stale = official_home / ".sync-trash"
    stale.mkdir()
    (stale / "somefile").write_text("leaked by a crashed swap")
    _patch_net(mocker, _routes("a" * 40, ["jd"]))

    osk.sync()

    assert not (stale / "somefile").exists()
    assert not list(official_home.glob(".sync-trash*"))
    assert (official_home / "skills" / "jd" / "SKILL.md").is_file()


def test_resync_over_existing_source_json_windows_rename_semantics(
    official_home: Path, mocker
) -> None:
    # Guards a Windows-only bug: os.rename won't overwrite an existing target
    # there (WinError 183), so re-syncing onto the prior source.json threw —
    # leaving source.json + .sync-state.json stale. The swap must use
    # os.replace. Simulate Windows rename semantics so the guard bites on POSIX.
    _patch_net(mocker, _routes("a" * 40, ["jd"]))
    osk.sync()  # first sync creates official/source.json + .sync-state.json

    real_rename = Path.rename

    def win_rename(self, target):
        if Path(target).exists():
            raise FileExistsError(f"[WinError 183] {target}")
        return real_rename(self, target)

    mocker.patch.object(Path, "rename", win_rename)

    # Second sync must still overwrite source.json AND update sync-state
    # (the code path uses os.replace, which win_rename doesn't intercept).
    _patch_net(mocker, _routes("b" * 40, ["jd"]))
    osk.sync()

    assert json.loads((official_home / "source.json").read_text())["commit"] == "b" * 40
    state = json.loads((official_home / osk.SYNC_STATE_FILE).read_text())
    assert state["commit"] == "b" * 40


# ---------- integrity ----------


def test_checksum_mismatch_aborts_untouched(
    official_home: Path, mocker, capsys
) -> None:
    routes = _routes("a" * 40, ["jd"])
    routes[SHA_URL] = f"{'f' * 64}  physiclaw_official_skills.zip\n".encode()  # wrong
    _patch_net(mocker, routes)

    with pytest.raises(osk.SyncError, match="checksum mismatch"):
        osk.sync()

    assert not (official_home / "skills").exists()
    assert not (official_home / osk.SYNC_STATE_FILE).exists()


def test_malformed_sha256_line_aborts(official_home: Path, mocker, capsys) -> None:
    routes = _routes("a" * 40, ["jd"])
    routes[SHA_URL] = b"not-a-checksum\n"
    _patch_net(mocker, routes)

    with pytest.raises(osk.SyncError, match="not a valid sha256sum"):
        osk.sync()


# ---------- zip-slip / layout ----------


def test_zip_slip_entry_is_rejected(official_home: Path, mocker, capsys) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("source.json", json.dumps({"commit": "a" * 40, "skills": []}))
        zf.writestr("../escape.txt", "pwned")
    data = buf.getvalue()
    routes = {
        LATEST_URL: json.dumps({"commit": "a" * 40}).encode(),
        ZIP_URL: data,
        SHA_URL: f"{hashlib.sha256(data).hexdigest()}  x\n".encode(),
    }
    _patch_net(mocker, routes)

    with pytest.raises(osk.SyncError, match="zip-slip"):
        osk.sync()

    assert not (official_home / "skills").exists()


def test_unexpected_layout_rejected(official_home: Path, mocker, capsys) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("source.json", json.dumps({"commit": "a" * 40, "skills": []}))
        # missing skills/ ; stray file instead
        zf.writestr("README.txt", "hi")
    data = buf.getvalue()
    routes = {
        LATEST_URL: json.dumps({"commit": "a" * 40}).encode(),
        ZIP_URL: data,
        SHA_URL: f"{hashlib.sha256(data).hexdigest()}  x\n".encode(),
    }
    _patch_net(mocker, routes)

    with pytest.raises(osk.SyncError, match="unexpected package layout"):
        osk.sync()


# ---------- per-skill hash verification ----------


def test_skill_hash_matches_no_warning(official_home: Path, mocker, capsys) -> None:
    # Happy path: manifest carries each skill's real content hash.
    _patch_net(mocker, _routes("a" * 40, ["jd", "wechat"]))

    osk.sync()

    out = capsys.readouterr().out
    assert "skill hash mismatch" not in out
    assert "manifest lists" not in out
    assert (official_home / "skills" / "jd").is_dir()


def test_skill_hash_mismatch_warns_but_mounts(
    official_home: Path, mocker, capsys
) -> None:
    # jd's manifest hash is wrong; wechat's is right.
    _patch_net(mocker, _routes("a" * 40, ["jd", "wechat"], bad_hash=("jd",)))

    osk.sync()  # warns, does NOT abort (per-skill hash is not integrity)

    out = capsys.readouterr().out
    mismatch_lines = [ln for ln in out.splitlines() if "skill hash mismatch" in ln]
    assert len(mismatch_lines) == 1  # only jd, not wechat
    assert "jd:" in mismatch_lines[0]
    # The skill still mounts — the zip .sha256 already vouched for the bytes.
    assert (official_home / "skills" / "jd" / "SKILL.md").is_file()
    state = json.loads((official_home / osk.SYNC_STATE_FILE).read_text())
    assert state["skillCount"] == 2


def test_hash_recipe_matches_builder(tmp_path: Path) -> None:
    # Lock the recipe to the site builder's hashSkill (relpath + NUL + bytes).
    d = tmp_path / "jd"
    d.mkdir()
    (d / "SKILL.md").write_bytes(_skill_md("jd"))
    assert osk._hash_skill(d) == _skill_hash(_skill_md("jd"))


# ---------- reconcile drift ----------


def test_manifest_drift_warns(official_home: Path, mocker, capsys) -> None:
    # Manifest declares 3 skills but only 1 dir is shipped.
    _patch_net(mocker, _routes("a" * 40, ["jd"], declared=3))

    osk.sync()

    out = capsys.readouterr().out
    assert "synced 1 official skill(s)" in out
    assert "manifest lists 3 skill(s) but 1 landed" in out


# ---------- dry-run ----------


def test_dry_run_reports_install_without_downloading(
    official_home: Path, mocker, capsys
) -> None:
    calls: list[str] = []
    _patch_net(mocker, _routes("a" * 40, ["jd"]), calls)

    osk.sync(dry_run=True)

    out = capsys.readouterr().out
    assert "sync would run" in out and "install" in out
    assert calls == [LATEST_URL]  # only the freshness check
    assert not (official_home / "skills").exists()


def test_dry_run_reports_up_to_date(official_home: Path, mocker, capsys) -> None:
    official_home.mkdir(parents=True)
    (official_home / "skills").mkdir()
    (official_home / "source.json").write_text("{}")
    (official_home / osk.SYNC_STATE_FILE).write_text(json.dumps({"commit": "a" * 40}))
    _patch_net(mocker, _routes("a" * 40, ["jd"]))

    osk.sync(dry_run=True)

    assert "up to date" in capsys.readouterr().out


# ---------- CLI wiring ----------


def test_cli_sync_official_routes_flags(mocker) -> None:
    called = mocker.patch.object(osk, "sync")

    result = runner.invoke(skills_mod.skills_app, ["sync", "official", "--dry-run"])

    assert result.exit_code == 0
    called.assert_called_once_with(force=False, dry_run=True)


def test_cli_sync_no_target_shows_help() -> None:
    result = runner.invoke(skills_mod.skills_app, ["sync"])
    # no_args_is_help → non-zero exit with usage listing the `official` command.
    assert "official" in result.output


# ---------- maybe_auto_sync (server-startup hook, backgrounded) ----------


@pytest.fixture
def auto_env(mocker, monkeypatch):
    """Baseline for maybe_auto_sync: sync_auto on, not CI, no live server.
    Patches threading.Thread so the guards can be tested without actually
    spawning the background sync. Yields the Thread mock."""
    monkeypatch.delenv("CI", raising=False)
    mocker.patch.object(
        osk,
        "_load_config",
        return_value=SimpleNamespace(skills=SimpleNamespace(sync_auto=True)),
    )
    mocker.patch("physiclaw.common.runtime_state.read_live", return_value=None)
    return mocker.patch.object(osk.threading, "Thread")


def test_auto_sync_spawns_background_daemon_thread(auto_env) -> None:
    osk.maybe_auto_sync()
    # The sync runs off the startup critical path, in a daemon thread.
    auto_env.assert_called_once_with(
        target=osk._run_sync_quiet, name="physiclaw-auto-sync", daemon=True
    )
    auto_env.return_value.start.assert_called_once_with()


def test_auto_sync_skipped_when_disabled(mocker, monkeypatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    mocker.patch.object(
        osk,
        "_load_config",
        return_value=SimpleNamespace(skills=SimpleNamespace(sync_auto=False)),
    )
    thread = mocker.patch.object(osk.threading, "Thread")

    osk.maybe_auto_sync()

    thread.assert_not_called()


@pytest.mark.parametrize("value", ["true", "1", "yes", " TRUE "])
def test_auto_sync_skipped_under_ci(auto_env, monkeypatch, value) -> None:
    monkeypatch.setenv("CI", value)

    osk.maybe_auto_sync()

    auto_env.assert_not_called()  # no thread spawned


def test_auto_sync_skipped_when_server_live(auto_env, mocker) -> None:
    mocker.patch("physiclaw.common.runtime_state.read_live", return_value={"pid": 123})

    osk.maybe_auto_sync()

    auto_env.assert_not_called()  # don't race another server's swap


# ---------- _run_sync_quiet (the daemon-thread body, fail-soft) ----------


def test_run_sync_quiet_calls_sync_with_log_emitter(mocker) -> None:
    called = mocker.patch.object(osk, "sync")
    osk._run_sync_quiet()
    # The background sync must speak through the logger, not typer.echo —
    # raw ✓ lines interleaving mid-log-stream broke the startup voice.
    called.assert_called_once_with(emit=osk._log_emit)


def test_log_emit_logs_unstyled(caplog) -> None:
    styled = typer.style("✓ ", fg=typer.colors.GREEN) + "synced 1 skill @ abc1234."
    with caplog.at_level(logging.DEBUG, logger=osk.__name__):
        osk._log_emit(styled)
    assert caplog.records[-1].getMessage() == "✓ synced 1 skill @ abc1234."


def test_run_sync_quiet_logs_sync_error_at_debug(mocker, caplog) -> None:
    """A background sync failure must reach the daily log (the daemon
    thread has no visible stderr) — but only at DEBUG: the sync is
    best-effort, so a flaky network must not warn on every startup."""
    mocker.patch.object(osk, "sync", side_effect=osk.SyncError("GET x failed"))

    with caplog.at_level(logging.DEBUG, logger=osk.__name__):
        osk._run_sync_quiet()  # must never escape the thread

    assert any(
        "GET x failed" in r.getMessage() and r.levelno == logging.DEBUG
        for r in caplog.records
    )


def test_run_sync_quiet_swallows_unexpected_error(mocker) -> None:
    mocker.patch.object(osk, "sync", side_effect=RuntimeError("boom"))
    osk._run_sync_quiet()  # any error is non-fatal


def _snapshot(official: Path) -> dict[str, bytes]:
    owned = [
        official / "skills",
        official / "source.json",
        official / osk.SYNC_STATE_FILE,
    ]
    files = [
        p
        for root in owned
        for p in ([root] if root.is_file() else root.rglob("*"))
        if p.is_file()
    ]
    return {p.relative_to(official).as_posix(): p.read_bytes() for p in files}


@pytest.fixture
def installed(official_home, mocker):
    _patch_net(mocker, _routes("old", ["old"]))
    osk.sync(emit=lambda _: None)
    before = _snapshot(official_home)
    _patch_net(mocker, _routes("new", ["new"]))
    return before


def _fail_replace(mocker, official, step, error):
    replace = osk.os.replace
    action, name = step.split(":")

    def replace_once(source, destination):
        source, destination = Path(source), Path(destination)
        matches = (
            source == official / name
            if action == "park"
            else destination == official / name
            and source.parent.name.startswith(osk.STAGING_DIR)
        )
        if matches:
            raise error
        return replace(source, destination)

    mocker.patch.object(osk.os, "replace", side_effect=replace_once)


@pytest.mark.parametrize(
    "step",
    [
        "park:skills",
        "install:skills",
        "park:source.json",
        "install:source.json",
        "park:.sync-state.json",
        "install:.sync-state.json",
    ],
)
@pytest.mark.parametrize(
    "error, expected",
    [
        (PermissionError("synthetic rename failure"), osk.SyncError),
        (KeyboardInterrupt("operator stop"), KeyboardInterrupt),
    ],
)
def test_regression_failed_swap_restores_installed_pack(
    official_home, installed, mocker, step, error, expected
):
    _fail_replace(mocker, official_home, step, error)

    with pytest.raises(expected, match=str(error)):
        osk.sync()

    assert _snapshot(official_home) == installed
    assert not list(official_home.glob(".sync-backup-*"))
    assert not list(official_home.glob(".sync-staging*"))


def test_regression_state_write_failure_leaves_install_unchanged(
    official_home, installed, mocker
):
    mocker.patch.object(osk, "write_text", side_effect=OSError("synthetic disk full"))

    with pytest.raises(osk.SyncError, match="disk full"):
        osk.sync()

    assert _snapshot(official_home) == installed


def test_regression_missing_tree_is_reinstalled_despite_current_marker(
    official_home, mocker
):
    official_home.mkdir(parents=True)
    (official_home / osk.SYNC_STATE_FILE).write_text(json.dumps({"commit": "same"}))
    _patch_net(mocker, _routes("same", ["jd"]))

    osk.sync()

    assert (official_home / "skills" / "jd" / "SKILL.md").is_file()


@pytest.mark.parametrize("commit", [None, "", "   ", 12, [], {}, True])
def test_regression_latest_commit_requires_nonempty_string(
    official_home, mocker, commit
):
    _patch_net(mocker, {LATEST_URL: json.dumps({"commit": commit}).encode()})

    with pytest.raises(osk.SyncError, match="commit"):
        osk.sync()

    assert not (official_home / "skills").exists()


def test_regression_pack_commit_mismatch_preserves_installed_pack(
    official_home, installed, mocker
):
    routes = _routes("wrong", ["new"])
    routes[LATEST_URL] = json.dumps({"commit": "expected"}).encode()
    _patch_net(mocker, routes)

    with pytest.raises(osk.SyncError, match="commit"):
        osk.sync()

    assert _snapshot(official_home) == installed


def _replace_manifest(routes, source):
    original = zipfile.ZipFile(io.BytesIO(routes[ZIP_URL]))
    result = io.BytesIO()
    with original, zipfile.ZipFile(result, "w") as updated:
        for item in original.infolist():
            updated.writestr(
                item.filename,
                json.dumps(source)
                if item.filename == "source.json"
                else original.read(item.filename),
            )
    data = result.getvalue()
    return {**routes, ZIP_URL: data, SHA_URL: hashlib.sha256(data).hexdigest().encode()}


@pytest.mark.parametrize(
    "entries",
    [
        12,
        "invalid",
        {"name": "jd"},
        [None],
        [{"name": 12, "hash": "digest"}],
        [{"name": "../outside", "hash": "digest"}],
        [{"name": "/outside", "hash": "digest"}],
        [{"name": "jd", "hash": 42}],
    ],
)
def test_regression_invalid_hash_metadata_warns_without_aborting(
    official_home, mocker, capsys, entries
):
    routes = _replace_manifest(
        _routes("new", ["jd"]), {"commit": "new", "skills": entries}
    )
    _patch_net(mocker, routes)

    osk.sync()

    assert (official_home / "skills" / "jd" / "SKILL.md").is_file()
    assert "manifest" in capsys.readouterr().out


@pytest.mark.parametrize(
    "url, code, expected",
    [(LATEST_URL, 404, 0), (LATEST_URL, 500, 1), (ZIP_URL, 404, 1), (SHA_URL, 404, 1)],
)
def test_regression_http_errors_close_response_body(
    official_home, mocker, url, code, expected
):
    body = io.BytesIO(b"error response")
    routes = _routes("new", ["jd"])
    http_get = _make_http_get(routes)

    def get(request, **kwargs):
        if request == url:
            raise urllib.error.HTTPError(url, code, "fixture", {}, body)
        return http_get(request, **kwargs)

    mocker.patch.object(osk, "http_get", side_effect=get)

    result = runner.invoke(skills_mod.skills_app, ["sync", "official"])

    assert result.exit_code == expected
    assert body.closed
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_regression_thread_start_failure_is_nonfatal(auto_env, caplog):
    auto_env.return_value.start.side_effect = RuntimeError("cannot start thread")

    with caplog.at_level(logging.DEBUG, logger=osk.__name__):
        osk.maybe_auto_sync()

    assert "cannot start thread" in caplog.text


def test_regression_overlapping_sync_cannot_replace_another_staging_tree(
    official_home, mocker
):
    entered, release = threading.Event(), threading.Event()
    errors = []
    routes = _routes("new", ["jd"])
    real_get = _make_http_get(routes)

    def get(url, **kwargs):
        if url == ZIP_URL:
            entered.set()
            assert release.wait(5)
        return real_get(url, **kwargs)

    def first_sync():
        try:
            osk.sync(emit=lambda _: None)
        except BaseException as exc:
            errors.append(exc)

    mocker.patch.object(osk, "http_get", side_effect=get)
    worker = threading.Thread(target=first_sync)
    worker.start()
    assert entered.wait(5)

    try:
        with pytest.raises(osk.SyncError, match="already running"):
            osk.sync()
    finally:
        release.set()
        worker.join(5)

    assert not worker.is_alive()
    assert errors == []
    assert (official_home / "skills" / "jd" / "SKILL.md").is_file()


@given(st.text(alphabet="0123456789abcdef", min_size=64, max_size=64))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_checksum_mismatch_never_modifies_installed_pack(
    official_home, installed, mocker, checksum
):
    routes = _routes("new", ["new"])
    assume(checksum != routes[SHA_URL].decode().split()[0])
    routes[SHA_URL] = checksum.encode()
    _patch_net(mocker, routes)

    with pytest.raises(osk.SyncError, match="checksum mismatch"):
        osk.sync()

    assert _snapshot(official_home) == installed


@pytest.mark.parametrize(
    "error, expected",
    [
        (OSError("install blocked"), osk.SyncError),
        (KeyboardInterrupt("operator stop"), KeyboardInterrupt),
    ],
)
def test_failed_rollback_retains_backup_and_failure(
    official_home, installed, mocker, caplog, error, expected
):
    replace = osk.os.replace

    def fail(source, destination):
        source = Path(source)
        if source.name == "skills" and source.parent.name.startswith(osk.STAGING_DIR):
            raise error
        if source.parent.name.startswith(".sync-backup-"):
            raise PermissionError("restore blocked")
        return replace(source, destination)

    mocker.patch.object(osk.os, "replace", side_effect=fail)

    with pytest.raises(expected, match=str(error)) as caught:
        osk.sync()

    backups = list(official_home.glob(".sync-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "skills" / "old" / "SKILL.md").read_bytes() == installed[
        "skills/old/SKILL.md"
    ]
    assert "backups kept at" in str(caught.value) + caplog.text


@pytest.mark.parametrize("body", [b"not json", b"\xff", b"[]", b"null"])
def test_invalid_latest_json_is_clean_cli_error(official_home, mocker, body):
    _patch_net(mocker, {LATEST_URL: body})

    result = runner.invoke(skills_mod.skills_app, ["sync", "official"])

    assert result.exit_code == 1
    assert "JSON" in result.output
    assert isinstance(result.exception, SystemExit)


def test_insecure_base_url_is_refused_without_request(official_home, mocker):
    osk._load_config.return_value.skills.official_base_url = "http://site.test/skills"
    get = mocker.patch.object(osk, "http_get")

    with pytest.raises(osk.SyncError, match="non-HTTPS"):
        osk.sync()

    get.assert_not_called()


@pytest.mark.parametrize("url", [LATEST_URL, ZIP_URL])
@pytest.mark.parametrize(
    "error, expected, message",
    [
        (urllib.error.URLError("fixture offline"), osk.SyncError, "fixture offline"),
        (OSError("fixture disk"), osk.SyncError, "fixture disk"),
        (KeyboardInterrupt("stop"), KeyboardInterrupt, "stop"),
    ],
)
def test_transport_failure_cleans_download_and_preserves_install(
    official_home, installed, mocker, tmp_path, url, error, expected, message
):
    http_get = _make_http_get(_routes("new", ["new"]))

    def get(request, **kwargs):
        if request == url:
            raise error
        return http_get(request, **kwargs)

    mocker.patch.object(osk, "http_get", side_effect=get)
    mocker.patch.object(osk.tempfile, "tempdir", str(tmp_path))

    with pytest.raises(expected, match=message):
        osk.sync()

    assert _snapshot(official_home) == installed
    assert not list(tmp_path.glob("physiclaw-official-*.zip"))


@pytest.mark.parametrize("body", [b"", b"G" * 64, b"0" * 63, b"\xff"])
def test_invalid_checksum_keeps_install(official_home, installed, mocker, body):
    routes = _routes("new", ["new"])
    routes[SHA_URL] = body
    _patch_net(mocker, routes)

    with pytest.raises(osk.SyncError, match="valid sha256sum"):
        osk.sync()

    assert _snapshot(official_home) == installed


def test_corrupt_zip_is_reported_before_replacing_install(
    official_home, installed, mocker
):
    data = b"not a zip"
    _patch_net(
        mocker,
        {
            LATEST_URL: b'{"commit":"new"}',
            ZIP_URL: data,
            SHA_URL: hashlib.sha256(data).hexdigest().encode(),
        },
    )

    with pytest.raises(osk.SyncError, match="not a valid zip"):
        osk.sync()

    assert _snapshot(official_home) == installed


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"\xff"])
def test_corrupt_local_state_does_not_block_reinstall(official_home, installed, body):
    (official_home / osk.SYNC_STATE_FILE).write_bytes(body)

    osk.sync()

    assert (official_home / "skills" / "new" / "SKILL.md").is_file()


@pytest.mark.parametrize("source", [None, [], "invalid"])
def test_nonobject_source_metadata_uses_latest_commit(official_home, mocker, source):
    _patch_net(mocker, _replace_manifest(_routes("new", ["jd"]), source))

    osk.sync()

    assert (
        json.loads((official_home / osk.SYNC_STATE_FILE).read_text())["commit"] == "new"
    )


def test_malformed_source_json_uses_latest_commit(official_home, mocker):
    read = osk.read_text

    def read_source(path):
        if path.name == "source.json":
            return "not-json"
        return read(path)

    mocker.patch.object(osk, "read_text", side_effect=read_source)
    _patch_net(mocker, _routes("new", ["jd"]))

    osk.sync()

    assert (
        json.loads((official_home / osk.SYNC_STATE_FILE).read_text())["commit"] == "new"
    )


@pytest.mark.parametrize(
    "force, expected", [(False, "no sync needed"), (True, "--force would re-sync")]
)
def test_dry_run_current_pack_does_not_write_or_download(
    official_home, installed, mocker, capsys, force, expected
):
    calls = []
    _patch_net(mocker, _routes("old", ["old"]), calls)

    osk.sync(dry_run=True, force=force)

    assert expected in capsys.readouterr().out
    assert calls == [LATEST_URL]
    assert _snapshot(official_home) == installed


def test_dry_run_changed_commit_reports_update_without_writing(
    official_home, installed, capsys
):
    osk.sync(dry_run=True)

    assert "update old → new" in capsys.readouterr().out
    assert _snapshot(official_home) == installed


@pytest.mark.parametrize("existing", [b"", b"\0"])
def test_windows_lock_uses_byte_zero_and_closes_handle(
    official_home, monkeypatch, mocker, existing
):
    official_home.mkdir(parents=True)
    (official_home / ".sync.lock").write_bytes(existing)
    observed = []

    def locking(fd, mode, size):
        observed.append((fd, mode, size, os.lseek(fd, 0, os.SEEK_CUR)))

    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(locking=locking, LK_NBLCK=2)
    )
    _patch_net(mocker, {})

    osk.sync()

    assert len(observed) == 1
    assert observed[0][1:] == (2, 1, 0)
    with pytest.raises(OSError):
        os.fstat(observed[0][0])


def test_windows_lock_contention_prevents_fetch(official_home, monkeypatch, mocker):
    locking = mocker.Mock(side_effect=OSError("locked"))
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(locking=locking, LK_NBLCK=2)
    )
    get = mocker.patch.object(osk, "http_get")

    with pytest.raises(osk.SyncError, match="already running"):
        osk.sync()

    get.assert_not_called()


def test_manifest_missing_skill_warns_but_installs_valid_tree(
    official_home, mocker, capsys
):
    source = {"commit": "new", "skills": [{"name": "ghost", "hash": "sha256:absent"}]}
    _patch_net(mocker, _replace_manifest(_routes("new", ["jd"]), source))

    osk.sync()

    assert "ghost: in manifest but not extracted" in capsys.readouterr().out
    assert (official_home / "skills" / "jd" / "SKILL.md").is_file()


def test_directory_instead_of_manifest_is_rejected_before_swap(
    official_home, installed, mocker
):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("source.json/", "")
        archive.writestr("skills/new/SKILL.md", "new")
    raw = data.getvalue()
    _patch_net(
        mocker,
        {
            LATEST_URL: b'{"commit":"new"}',
            ZIP_URL: raw,
            SHA_URL: hashlib.sha256(raw).hexdigest().encode(),
        },
    )

    with pytest.raises(osk.SyncError, match="unexpected package layout"):
        osk.sync()

    assert _snapshot(official_home) == installed


def test_missing_installed_manifest_triggers_repair(official_home, installed, mocker):
    (official_home / "source.json").unlink()
    _patch_net(mocker, _routes("old", ["old"]))

    osk.sync()

    assert json.loads((official_home / "source.json").read_text())["commit"] == "old"


def test_first_install_failure_leaves_no_partial_install(official_home, mocker):
    _patch_net(mocker, _routes("new", ["jd"]))
    _fail_replace(
        mocker, official_home, "install:source.json", OSError("install blocked")
    )

    with pytest.raises(osk.SyncError, match="install blocked"):
        osk.sync()

    assert _snapshot(official_home) == {}
    assert not list(official_home.glob(".sync-backup-*"))


def test_download_cleanup_failure_preserves_checksum_diagnostic(
    official_home, installed, mocker, tmp_path
):
    routes = _routes("new", ["new"])
    routes[SHA_URL] = b"0" * 64
    _patch_net(mocker, routes)
    unlink = Path.unlink

    def fail_download_unlink(path, **kwargs):
        if path.name.startswith("physiclaw-official-"):
            raise PermissionError("cleanup blocked")
        return unlink(path, **kwargs)

    mocker.patch.object(Path, "unlink", fail_download_unlink)
    mocker.patch.object(osk.tempfile, "tempdir", str(tmp_path))

    with pytest.raises(osk.SyncError, match="checksum mismatch"):
        osk.sync()

    assert _snapshot(official_home) == installed


@pytest.mark.parametrize(
    "error", [OSError("config inaccessible"), ValueError("bad config")]
)
def test_optional_sync_guard_failure_is_nonfatal(auto_env, mocker, caplog, error):
    mocker.patch.object(osk, "_load_config", side_effect=error)

    with caplog.at_level(logging.DEBUG, logger=osk.__name__):
        osk.maybe_auto_sync()

    auto_env.assert_not_called()
    assert str(error) in caplog.text


@pytest.mark.parametrize(
    "first",
    [
        None,
        {"name": "../invalid", "hash": "bad"},
        {"name": "legacy"},
        {"name": "jd", "hash": 12},
        {"name": "ghost", "hash": "bad"},
    ],
)
def test_hash_verification_continues_after_unusable_entry(
    official_home, mocker, capsys, first
):
    source = {"commit": "new", "skills": [first, {"name": "jd", "hash": "sha256:bad"}]}
    _patch_net(mocker, _replace_manifest(_routes("new", ["jd"]), source))

    osk.sync()

    assert "jd: sha256:" in capsys.readouterr().out


def _add_entry(routes, name, content):
    data = io.BytesIO(routes[ZIP_URL])
    with zipfile.ZipFile(data, "a") as archive:
        archive.writestr(name, content)
    raw = data.getvalue()
    return {**routes, ZIP_URL: raw, SHA_URL: hashlib.sha256(raw).hexdigest().encode()}


def test_extra_top_level_file_is_rejected_with_other_required_entries_present(
    official_home, installed, mocker
):
    _patch_net(mocker, _add_entry(_routes("new", ["jd"]), "README.txt", "stray"))

    with pytest.raises(osk.SyncError, match="unexpected package layout"):
        osk.sync()

    assert _snapshot(official_home) == installed


def test_landed_count_ignores_hidden_directories_and_plain_files(official_home, mocker):
    routes = _add_entry(_routes("new", ["jd"]), "skills/.hidden/SKILL.md", "hidden")
    _patch_net(mocker, _add_entry(routes, "skills/readme.txt", "not a skill"))

    osk.sync()

    assert (
        json.loads((official_home / osk.SYNC_STATE_FILE).read_text())["skillCount"] == 1
    )


def test_first_sync_creates_missing_home_parents(tmp_path, mocker):
    official = tmp_path / "missing" / "nested" / "official"
    mocker.patch.object(osk.paths, "official_dir", return_value=official)
    mocker.patch.object(
        osk,
        "_load_config",
        return_value=SimpleNamespace(skills=SimpleNamespace(official_base_url=BASE)),
    )
    _patch_net(mocker, _routes("new", ["jd"]))

    osk.sync()

    assert (official / "skills" / "jd" / "SKILL.md").is_file()


@pytest.mark.parametrize("prefix", [".sync-backup-", osk.STAGING_DIR])
def test_cleanup_failure_after_success_does_not_report_failed_install(
    official_home, installed, mocker, capsys, prefix
):
    rmdir = osk.os.rmdir

    def blocked(path, *args, **kwargs):
        if Path(path).name.startswith(prefix):
            raise PermissionError("cleanup blocked")
        return rmdir(path, *args, **kwargs)

    mocker.patch.object(osk.os, "rmdir", side_effect=blocked)

    osk.sync()

    assert "synced 1" in capsys.readouterr().out
    assert (official_home / "skills" / "new" / "SKILL.md").is_file()


@pytest.fixture
def blocked_staging_cleanup(mocker):
    """Model Linux's unlink-directory error, which differs from macOS's."""
    rmdir, unlink = osk.os.rmdir, osk.os.unlink

    def blocked_rmdir(path, *args, **kwargs):
        if Path(path).name.startswith(osk.STAGING_DIR):
            raise PermissionError("cleanup blocked")
        return rmdir(path, *args, **kwargs)

    def linux_unlink(path, *args, **kwargs):
        if Path(path).name.startswith(osk.STAGING_DIR):
            raise IsADirectoryError("cannot unlink directory")
        return unlink(path, *args, **kwargs)

    mocker.patch.object(osk.os, "rmdir", side_effect=blocked_rmdir)
    mocker.patch.object(osk.os, "unlink", side_effect=linux_unlink)


def test_regression_linux_staging_cleanup_failure_preserves_success(
    official_home, installed, blocked_staging_cleanup, capsys
):
    osk.sync()

    assert "synced 1" in capsys.readouterr().out
    assert (official_home / "skills" / "new" / "SKILL.md").is_file()


def test_regression_linux_staging_cleanup_failure_preserves_install_error(
    official_home, installed, blocked_staging_cleanup, mocker
):
    _fail_replace(mocker, official_home, "install:skills", OSError("install blocked"))

    with pytest.raises(osk.SyncError, match="install blocked"):
        osk.sync()

    assert _snapshot(official_home) == installed


def test_backup_cleanup_failure_does_not_replace_original_install_error(
    official_home, installed, mocker
):
    _fail_replace(mocker, official_home, "install:skills", OSError("install blocked"))
    rmdir = osk.os.rmdir

    def blocked(path, *args, **kwargs):
        if Path(path).name.startswith(".sync-backup-"):
            raise PermissionError("cleanup blocked")
        return rmdir(path, *args, **kwargs)

    mocker.patch.object(osk.os, "rmdir", side_effect=blocked)

    with pytest.raises(osk.SyncError, match="install blocked"):
        osk.sync()

    assert _snapshot(official_home) == installed


@pytest.mark.parametrize("commit", [12, True, [], {}])
def test_dry_run_invalid_cached_commit_reports_install(
    official_home, mocker, capsys, commit
):
    official_home.mkdir(parents=True)
    (official_home / osk.SYNC_STATE_FILE).write_text(json.dumps({"commit": commit}))
    _patch_net(mocker, _routes("new", ["jd"]))

    osk.sync(dry_run=True)

    assert "install new" in capsys.readouterr().out


def test_missing_manifest_count_does_not_report_count_drift(
    official_home, mocker, capsys
):
    _patch_net(mocker, _replace_manifest(_routes("new", ["jd", "wechat"]), {}))

    osk.sync()

    assert "manifest lists" not in capsys.readouterr().out


def test_base_url_trimming_preserves_last_path_component(official_home, mocker):
    base = BASE + "X"
    osk._load_config.return_value.skills.official_base_url = base + "///"
    routes = {
        url.replace(BASE, base): body for url, body in _routes("new", ["jd"]).items()
    }
    _patch_net(mocker, routes)

    osk.sync()

    assert (official_home / "skills" / "jd" / "SKILL.md").is_file()
