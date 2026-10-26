"""PhysiClaw's single data root.

All persistent state — calibration, memory, jobs, model cache, logs —
lives under ``~/.physiclaw/``. Override with ``PHYSICLAW_HOME`` (read once
at import, so set it *before* the first ``import physiclaw`` in tests).

Layout::

    ~/.physiclaw/
    ├── config.toml                    (explicit user overrides only)
    ├── backups/
    │   ├── config.toml.lock            (serializes config writers)
    │   └── config.toml.<timestamp>.<id>.bak  (five recent private backups)
    ├── calibration/{bundle.json, cache/}
    ├── memory/{memory.md, USER.md, YYYY-MM-DD.md}
    ├── jobs/jobs.md
    ├── models/omniparser_icon_detect/model.onnx
    ├── skills/<name>/SKILL.md           (user-authored; overrides built-in)
    ├── macros/{README.md, <name>.yml, stats.json}  (gesture macros + run stats)
    ├── playbooks/<app>/{APP.yml, README.md, macros/, <playbook>/{PLAYBOOK.yml, macros/, prompts/}}
    ├── playbooks/channel/{ACTIVE.txt, <im>/}  (the user channel, one IM folder in use)
    ├── playbooks/placeholders.yml       (per-install <<TOKEN>> values, filled at load)
    ├── official/{source.json, skills/, .sync-state.json}  (synced official pack)
    ├── cache/update.json                (update-notice stage marker; Phase B→A)
    ├── learned/pitfalls/{pitfalls.md, history.jsonl}  (agent-flagged traps; add_pitfall)
    ├── learned/pages/<app>.json         (captured page geometry: anchor positions, OCR variants)
    ├── screen-layout/{layout.json, layout.md}  (learned on first run by the `screen-layout` skill)
    ├── snapshots/, screenshots/, tool_calls/, raw_camera/  (debug dumps; `physiclaw clear`)
    ├── debug/{thread.json, wake.json}   (e2e harness: virtual thread + one-shot wake)
    ├── run/{server.json, active-session} (live process/session state; cleared on exit)
    └── log/
        ├── README.md                    (directory guide and naming conventions)
        ├── cli.log                      (command diagnostics)
        ├── server.log                   (hardware/MCP diagnostics)
        ├── runtime.log                  (agent diagnostics)
        ├── studio.log                   (Studio diagnostics)
        ├── archive/<service>.log.<1-5>   (bounded older service logs)
        ├── session-stats.jsonl           (one summary per completed session)
        ├── sessions/{README.md, <sid>/{summary.json, events.jsonl, model.jsonl,
        │                             runtime.log, server.log, notes.md, images/}}
        └── macros/macro-run-<hex6>/{events.jsonl, images/}  (per-step macro-run records)

File-format rule — the mix above is deliberate, one format per audience
(the Python-community split: TOML for what humans edit, JSON for what
machines write):

    config.toml     the ONE user-edited settings file → TOML
    <name>.yml,     the deliberate YAML exceptions: user-edited automation
    APP.yml,        specs (macro step flows, pack pages, playbook routes)
    PLAYBOOK.yml
                    follow the GitHub-Actions shape, and YAML's
                    implicit-typing hazards are fenced by a 1.2 parser +
                    strict type checks (macros/parse.py,
                    conductor/spec/pages.py)
    *.json/*.jsonl  machine-written state, caches, and append-only logs
                    (jq-able; source.json mirrors the upstream manifest
                    verbatim)
    *.md            agent-facing documents (memory, jobs, pitfalls)

New files follow this rule; don't convert one format to another for
uniformity's sake.
"""

import functools
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from physiclaw.common.text import read_text

HOME: Path = Path(os.environ.get("PHYSICLAW_HOME", "~/.physiclaw")).expanduser()
LOG_DIR: Path = HOME / "log"

# Whether the source-tree pack layer is eligible — decided ONCE at
# import, beside HOME and by the same knob: setting PHYSICLAW_HOME
# means "this exact home and nothing layered under it" (tests, probes).
# Tests toggle it with monkeypatch.setattr, like HOME itself.
SOURCE_LAYER: bool = "PHYSICLAW_HOME" not in os.environ

# The pack layout's fixed names. A folder with APP.yml is a pack (the
# app's manifest: what its playbooks share); a folder inside it with
# PLAYBOOK.yml is an ENTRY: the whole workflow the boot may offer for a
# request. Any other `<name>.yml` beside it is one of the playbooks that
# entry runs, walked by its `run:` and never offered. The two reserved
# subfolders hold leaf files — recorded hands and the model's prose — at
# either level, and are never mistaken for a playbook; an entry's leaf
# folders serve the playbooks beside it too.
PACK_FILENAME = "APP.yml"
PLAYBOOK_FILENAME = "PLAYBOOK.yml"
PLAYBOOK_SUFFIX = ".yml"
PACK_MACROS_DIRNAME = "macros"
PACK_PROMPTS_DIRNAME = "prompts"
RESERVED_PACK_DIRS = frozenset({PACK_MACROS_DIRNAME, PACK_PROMPTS_DIRNAME})
PROMPT_SUFFIX = ".md"


# What a file IS, declared by its first line and checked against where
# it sits (`kind_gap`): the pack's manifest, an entry (the workflow the
# boot may offer), one of the playbooks that entry runs, or a recorded
# hand. Position still decides how a file is parsed — `kind:` is the
# file saying so itself, so a misplaced one names the folder it wants.
# (Not the studio job kind or an agent grant's kind — those name other
# things in other layers; this one names a FILE.)
KIND_MANIFEST = "manifest"
KIND_ENTRY = "entry"
KIND_PLAYBOOK = "playbook"
KIND_MACRO = "macro"
# Every kind with where it lives, for the sentence a misplaced file
# gets. The one listing: `FILE_KINDS` reads its keys, so a kind added
# here cannot be admitted without a home to name.
KIND_HOMES = {
    KIND_MANIFEST: f"the pack's {PACK_FILENAME}",
    KIND_ENTRY: f"<name>/{PLAYBOOK_FILENAME}",
    KIND_PLAYBOOK: f"<entry>/<name>{PLAYBOOK_SUFFIX}, beside the entry that runs it",
    KIND_MACRO: f"{PACK_MACROS_DIRNAME}/<name>{PLAYBOOK_SUFFIX}",
}
FILE_KINDS = tuple(KIND_HOMES)


# The pack grammar this physiclaw reads. Every pack file says which
# grammar it is written in, beside `kind:`, so a file and the engine
# that reads it can never disagree in silence: the number is what lets a
# refusal say "older", "newer" or "not numbered at all" instead of
# naming a key the author never typed. It moves ONLY when a change makes
# an existing file invalid or quietly changes its meaning — never for a
# key that is merely added.
PACK_SCHEMA = 1


def schema_gap(declared: Any) -> str | None:
    """What is wrong with a pack file's `schema:` — None when it is the
    grammar this physiclaw reads. Read BEFORE anything else in the file,
    since which grammar a file is written in decides what the rest of it
    means."""
    reads = f"and this physiclaw reads `schema: {PACK_SCHEMA}`"
    if declared is None:
        return f"no `schema:` — a pack file says which grammar it is in, {reads}"
    # A grammar is a counting number: below one is no grammar that ever
    # was, so it is a malformed value rather than an older file.
    if not isinstance(declared, int) or isinstance(declared, bool) or declared < 1:
        return f"`schema: {declared}` must be a whole number from 1, {reads}"
    if declared > PACK_SCHEMA:
        return f"`schema: {declared}` needs a newer physiclaw, {reads}"
    if declared < PACK_SCHEMA:
        return f"`schema: {declared}` is an older grammar, {reads}"
    return None


def header_gap(data: "Mapping[str, Any]", want: str) -> str | None:
    """What is wrong with a pack file's header — its `kind:` and its
    `schema:` — or None when both are right. Both are read before any
    other key, and the GRAMMAR is read before the kind: which grammar a
    file is written in decides what the rest of it means, the kind
    included, so a file from another physiclaw is told so rather than
    told its kind is wrong. A file writes them the other way round,
    `kind:` first, because that is what a reader wants to see; a YAML
    mapping has no order, so the two never disagree. Every parser of a
    pack file calls this one function, so the rule has one home."""
    return schema_gap(data.get("schema")) or kind_gap(data.get("kind"), want)


def kind_gap(declared: Any, want: str) -> str | None:
    """What is wrong with a file's `kind:` — None when it matches where
    the file sits. ONE wording for the three parsers, each raising it as
    its own error: absent, not a kind at all, or a kind that belongs in
    another folder (then the sentence names that folder)."""
    if declared is None:
        return f"no `kind:` — a file here starts `kind: {want}`"
    if not isinstance(declared, str) or declared not in FILE_KINDS:
        return f"`kind: {declared}` is not one of {', '.join(FILE_KINDS)}"
    if declared != want:
        return (
            f"`kind: {declared}` sits where `kind: {want}` belongs — "
            f"`kind: {declared}` lives in {KIND_HOMES[declared]}"
        )
    return None


def split_playbook(playbook: str) -> tuple[str, str | None]:
    """A playbook id read once: `(entry, name)` — `("hema-buy", "add")`
    for `hema-buy.add`, `("buy", None)` for an entry itself. THE parse
    of the id; `entry_of`, `own_name`, `run_by`, `kind_of` and
    `playbook_file` are its faces, so the dot means one thing
    everywhere."""
    entry, sep, name = playbook.partition(".")
    return entry, name if sep else None


def entry_of(playbook: str) -> str:
    """The entry a playbook id belongs to — itself for an entry, the
    folder for one of the playbooks it runs (`hema-buy.add` →
    `hema-buy`). An entry's leaf folders are theirs too."""
    return split_playbook(playbook)[0]


def run_by(playbook: str) -> str | None:
    """The entry whose `run:` walks this playbook — None when the id is
    an entry's own. The predicate too: `if run_by(name)` is "the boot
    never offers this"."""
    entry, name = split_playbook(playbook)
    return entry if name is not None else None


def own_name(playbook: str) -> str:
    """A playbook id's own name — the folder's for an entry, the file
    stem for a playbook it runs — the name its file declares."""
    entry, name = split_playbook(playbook)
    return name if name is not None else entry


def kind_of(playbook: str) -> str:
    """The `kind:` a playbook id's file must declare."""
    return KIND_PLAYBOOK if run_by(playbook) else KIND_ENTRY


def playbook_file(playbook: str) -> str:
    """A playbook id's file, pack-relative: `hema-buy/PLAYBOOK.yml` for
    an entry, `hema-buy/add.yml` for a playbook it runs."""
    entry, name = split_playbook(playbook)
    leaf = PLAYBOOK_FILENAME if name is None else f"{name}{PLAYBOOK_SUFFIX}"
    return f"{entry}/{leaf}"


# The one skip convention every artifact lister shares: a `_` or `.`
# prefix parks a draft beside the real files (an editor's swap file, a
# folder the author is not done with) without it being read.
SKIP_PREFIXES = ("_", ".")


def is_skipped(name: str) -> bool:
    return name.startswith(SKIP_PREFIXES)


def leaf_files(root: Path, suffix: str) -> list[Path]:
    """``<root>/*<suffix>``, sorted, minus the skip convention — the one
    walk every leaf-file lister takes (macros, prompts, stray routes),
    so the rule is read in one place. An absent root is no files."""
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.glob(f"*{suffix}") if p.is_file() and not is_skipped(p.name)
    )


def leaf_dirs(root: Path) -> list[Path]:
    """``<root>/*/``, sorted, minus the skip convention — `leaf_files`'
    twin for the folder walk (a pack's entry folders, an entry's
    subfolders). An absent root is no folders."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not is_skipped(p.name))


def marked_subdirs(dirs: list[Path], marker: str) -> set[str]:
    """Subdir names bearing ``marker`` across a search path — the one
    union the pack lister takes, with the one skip convention."""
    names: set[str] = set()
    for root in dirs:
        if not root.is_dir():
            continue
        names.update(
            d.name
            for d in root.iterdir()
            if d.is_dir() and not is_skipped(d.name) and (d / marker).exists()
        )
    return names


@functools.cache
def _checkout_root() -> Path | None:
    root = Path(__file__).resolve().parents[3]
    if (root / "pyproject.toml").exists() and (root / "playbooks").is_dir():
        return root
    return None


def source_root() -> Path | None:
    """The repo checkout physiclaw runs from (editable install, `uv run`
    in the tree) — None on an installed wheel or when ``SOURCE_LAYER``
    is off."""
    return _checkout_root() if SOURCE_LAYER else None


def _layered(primary: Path, sub: str) -> list[Path]:
    dirs = [primary]
    root = source_root()
    if root is not None and (root / sub).is_dir():
        dirs.append(root / sub)
    return dirs


def playbooks_dirs() -> list[Path]:
    """The pack search path, precedence first: the local home (an
    installed pack shadows the same-named tree pack — this is THE
    layering rule; `macros_dirs` and the artifact listers follow it),
    then the source tree's ``playbooks/`` when running from a checkout —
    so repo edits are live in dev without an install step. Writes always
    target ``playbooks_dir()`` (the home)."""
    return _layered(playbooks_dir(), "playbooks")


# The user channel: `playbooks/channel/<im>/` is a pack per IM app, and
# `channel/ACTIVE.txt` holds the one word naming the folder in use. One
# folder needs no file.
CHANNEL_DIRNAME = "channel"
CHANNEL_ACTIVE_FILENAME = "ACTIVE.txt"


def channel_folder(root: Path) -> tuple[Path | None, str | None]:
    """The IM folder the channel under ``root`` resolves to, or why
    none does: (folder, None) / (None, the gap) / (None, None) when
    ``root`` holds no channel at all."""
    base = root / CHANNEL_DIRNAME
    if not base.is_dir():
        return None, None
    folders = sorted(marked_subdirs([base], PACK_FILENAME))
    active = base / CHANNEL_ACTIVE_FILENAME
    where = f"{CHANNEL_DIRNAME}/{CHANNEL_ACTIVE_FILENAME}"
    if active.is_file():
        word = read_text(active).strip()
        if word in folders:
            return base / word, None
        have = ", ".join(folders) or "none"
        return None, f"{where} names {word!r}, which is not an IM folder here ({have})"
    if len(folders) == 1:
        return base / folders[0], None
    if not folders:
        return None, f"{CHANNEL_DIRNAME}/ holds no IM folder with {PACK_FILENAME}"
    return (
        None,
        f"{CHANNEL_DIRNAME}/ holds {', '.join(folders)} — write {where} naming one",
    )


def channel_root() -> tuple[Path | None, str | None]:
    """The channel's pack folder, or why there is none: the first search
    dir holding a channel dir at all decides (the layering rule, the
    home first) — a misconfigured home channel is reported, never
    silently shadowed by the tree's template."""
    for d in playbooks_dirs():
        folder, why = channel_folder(d)
        if folder is not None or why is not None:
            return folder, why
    return None, None


def pack_root(app: str) -> Path:
    """The directory pack ``app`` loads from — the first search dir
    carrying ``<app>/APP.yml``; the home dir (the write target) when
    none does. The channel is the one pack that lives a level down
    (`channel_root`), so its root's name is the IM, not the app."""
    if app == CHANNEL_DIRNAME:
        folder, _ = channel_root()
        return folder if folder is not None else playbooks_dir() / CHANNEL_DIRNAME
    for d in playbooks_dirs():
        if (d / app / PACK_FILENAME).exists():
            return d / app
    return playbooks_dir() / app


def pack_gap(app: str) -> str:
    """Why ``app`` has no manifest to load — the channel's own reason
    (`channel_root`) when it has one, else the plain fact."""
    if app == CHANNEL_DIRNAME:
        _, gap = channel_root()
        if gap is not None:
            return gap
    return f"no pack {app!r} on disk (missing {PACK_FILENAME})"


def macros_dirs() -> list[Path]:
    """The standalone-macro search path — home, then the source tree's
    ``macros/`` (the `playbooks_dirs` layering rule). Writes —
    recordings, stats — always target ``macros_dir()``."""
    return _layered(macros_dir(), "macros")


def ensure_dirs() -> None:
    """Create HOME and LOG_DIR. Safe to call repeatedly."""
    HOME.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def model_cache() -> Path:
    return HOME / "models"


def cache_dir() -> Path:
    """Scratch space for background-staged/prefetched artifacts — currently the
    update-notice stage marker (``cache/update.json``, written by Phase B and
    consumed by Phase A at the next startup). Safe to delete; regenerated on
    demand. Distinct from uv's own wheel cache, which the marker points into."""
    return HOME / "cache"


def omniparser_onnx() -> Path:
    return model_cache() / "omniparser_icon_detect" / "model.onnx"


def calibration_dir() -> Path:
    """Arm/camera/screen calibration state — ``bundle.json`` + ``cache/``.
    Cleared by ``physiclaw reset`` so a new phone gets re-calibrated."""
    return HOME / "calibration"


def calibration_bundle() -> Path:
    return calibration_dir() / "bundle.json"


def load_calibration_bundle() -> dict | None:
    """Read+parse ``calibration/bundle.json``. None on missing/unreadable/invalid."""
    p = calibration_bundle()
    if not p.exists():
        return None
    try:
        data = json.loads(read_text(p))
    except (OSError, ValueError):  # ValueError covers JSON + UTF-8 decode errors
        return None
    return data if isinstance(data, dict) else None


def calibration_cache_dir() -> Path:
    return calibration_dir() / "cache"


def snapshots_dir() -> Path:
    return HOME / "snapshots"


def screenshots_dir() -> Path:
    return HOME / "screenshots"


def tool_calls_dir() -> Path:
    return HOME / "tool_calls"


def raw_camera_dir() -> Path:
    return HOME / "raw_camera"


def jobs_file() -> Path:
    return HOME / "jobs" / "jobs.md"


def memory_dir() -> Path:
    return HOME / "memory"


def memory_file() -> Path:
    """`memory/memory.md` — the persistent-fact store. The one spelling of
    the filename: `engine.memory` reads it for the SYSTEM prompt, the
    conductor for `memory.*` decision-context slices (which may not
    import engine behavior)."""
    return memory_dir() / "memory.md"


def skills_dir() -> Path:
    return HOME / "skills"


def macros_dir() -> Path:
    """User-authored gesture macros — one ``<name>.yml`` file per macro
    (rehearsed MCP-gesture sequences the agent runs via ``run_macro``), plus a
    single machine-written ``stats.json`` and the format ``README.md`` at the
    root. Only ``*.yml`` is scanned, so neither can be read as a macro."""
    return HOME / "macros"


def playbooks_dir() -> Path:
    """Self-contained conductor app packs — one ``<app>/`` dir per app
    holding its ``APP.yml`` manifest (meta, placeholders, landmarks,
    shared pages), one ``<playbook>/PLAYBOOK.yml`` folder per playbook
    (with its own ``macros/`` and ``prompts/``), and the pack's
    private ``macros/``.
    Geometry never lives here; it is captured on-device into
    ``learned_pages_dir()`` — device variance (iPhone models, iOS and app
    versions) makes shipped or authored geometry infeasible."""
    return HOME / "playbooks"


def debug_dir() -> Path:
    """The e2e debug harness's files (`physiclaw debug`) —
    ``thread.json`` (the virtual user-channel thread) and ``wake.json``
    (the one-shot debug wake trigger). Seeded by the ``physiclaw debug``
    CLI; the runtime consumes the wake and keeps the thread current
    (agent sends, released replies). Deleting the directory resets the
    harness."""
    return HOME / "debug"


def learned_pages_dir() -> Path:
    """Machine-captured page geometry per app — ``<app>.json`` holding the
    learned half of each page fingerprint (anchor positions/tolerances,
    OCR variants), merged with the authored
    declarations at load. Deleting a file resets that app's capture
    without touching any authoring."""
    return HOME / "learned" / "pages"


def live_session_id() -> str | None:
    """The engine session currently running, per the cross-process
    marker the runtime maintains — None when absent or unreadable (CLI
    rehearsals, probes). One reader spelling: macro run logs and walk
    telemetry both tag their records with it."""
    try:
        marker = active_session_marker()
        if marker.exists():
            return marker.read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeError):
        pass
    return None


def learned_dismiss_dir() -> Path:
    """Machine-learned popup dismissals per app — ``<app>.json``, labels
    the rescue micro tier (`clear_overlay`) successfully tapped, so the
    free vocabulary tier handles the same popup next time. Deny-list
    gated at USE, not just at write; deleting a file resets the app."""
    return HOME / "learned" / "dismiss"


def official_dir() -> Path:
    """Official skill pack synced from the site (``physiclaw skills sync
    official``). Kept separate from ``skills_dir()`` (user-authored): the
    sync owns this tree wholesale — an atomic swap replaces ``official/skills``
    on every run, so anything a user drops here is blown away. Layout::

        official/
        ├── source.json       (upstream manifest, extracted verbatim)
        ├── skills/<name>/…    (the mounted skills)
        └── .sync-state.json   (client-written sync bookkeeping)
    """
    return HOME / "official"


def official_skills_dir() -> Path:
    """The mounted official skills tree — a ``<name>/SKILL.md`` root like
    ``skills_dir()``. Empty until the first successful ``skills sync official``."""
    return official_dir() / "skills"


def pitfalls_dir() -> Path:
    """Agent-flagged turn-wasting traps — ``learned/pitfalls/pitfalls.md`` (the
    live list, always injected into SYSTEM after user skills) plus an
    append-only ``history.jsonl``. The agent only *appends* ≤3 items per
    long DONE session; a post-session curator consolidates and caps the list.
    Pitfalls-only (no how-to) sidesteps the self-praise that poisons an
    agent-authored 'working flow'."""
    return HOME / "learned" / "pitfalls"


def screen_layout_dir() -> Path:
    """Learned key input-box + on-screen-keyboard bboxes (Spotlight search,
    chat input with/without keyboard), captured on first run by the
    ``screen-layout`` skill via ``report_screen_layout``. Empty until
    learned — the runtime keys first-run setup on that emptiness."""
    return HOME / "screen-layout"


def screen_layout_json() -> Path:
    return screen_layout_dir() / "layout.json"


def screen_layout_md() -> Path:
    return screen_layout_dir() / "layout.md"


def runtime_state_file() -> Path:
    return HOME / "run" / "server.json"


def macros_log_dir() -> Path:
    """Per-step forensic records of macro runs — one
    ``<macro-run-hex6>/{events.jsonl, images/}`` dir per run (the engine
    session layout in miniature), engine runs and CLI rehearsals alike.
    Read by `physiclaw macros runs`."""
    return LOG_DIR / "macros"


def sessions_dir() -> Path:
    """Per-session artifact dirs: sessions/<sid>/{events.jsonl, model.jsonl,
    summary.json, images/}."""
    return LOG_DIR / "sessions"


def active_session_marker() -> Path:
    """Cross-process pointer to the agent's current session id. The runtime
    writes the session id here at session start and clears it at close; the
    MCP-server process (a separate process — no shared memory) reads it,
    resolves the id to its session dir, and tees `server.log` there."""
    return HOME / "run" / "active-session"
