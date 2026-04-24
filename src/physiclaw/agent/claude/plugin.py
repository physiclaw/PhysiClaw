"""Materialize a Claude Code plugin dir for each session.

Merges two skill sources into one ./skills subdir:
  1. agent/claude/skills/*   — claude-only skills (jobs, …) that shell
                                out to engine-module scripts
  2. skill.discover()         — shared user skills (wechat, jd, …) from
                                ~/.physiclaw/skills/ and ./skills/

Name collisions: claude-only wins. Those skills are curated next to
CLAUDE.md; a user skill accidentally shadowing one would defeat the
purpose of bundling it here.

Dir is created under TMPDIR with a session-specific prefix. Caller is
responsible for cleanup (or the OS reclaims TMPDIR across reboots).

Isolation — what this module prevents and what it doesn't:

  prevented
    • Plugin-name collision with a user-level Claude Code plugin
      named "physiclaw": our plugin.json uses a session-unique name
      (`physiclaw-agent-<sid>`), so the two never share an identity.
    • Accidental promotion of `CLAUDE.md` / `.claude/` files that
      live INSIDE a symlinked skill dir: we forward the skill dir
      itself, not its parent — Claude Code's plugin loader scans
      `<plugin-dir>/skills/<name>/SKILL.md`, not `<plugin-dir>/..`.

  not prevented (by design)
    • User-level `~/.claude/skills/`, `~/CLAUDE.md`, and
      `~/.claude/settings.json` still load — those are the user's
      across-all-invocations config. `spawn.py` passes
      `--setting-sources user` to bound settings to that layer,
      but CLAUDE.md and skills at user level are intentional.

  flagged (not fatal)
    • Stray `CLAUDE.md` / `.claude/` inside PROJECT_ROOT
      (~/.physiclaw). `spawn._warn_stray_context()` logs at WARN so
      the operator notices drift.
"""
import json
import logging
import shutil
import tempfile
from pathlib import Path

from physiclaw.agent.engine import skill
from physiclaw.agent.engine.skill import Skill

log = logging.getLogger(__name__)

# ./skills relative to this file — claude-only bundled skills.
_CLAUDE_ONLY_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
# Plugin name prefix. The per-session sid is appended so two concurrent
# spawns don't collide on Claude Code's plugin registry, and so a
# user-level plugin named "physiclaw" (if any) stays distinct from ours.
_PLUGIN_NAME_PREFIX = "physiclaw-agent"


def prepare_plugin_dir(
    sid: str,
    *,
    skills: dict[str, Skill] | None = None,
) -> Path:
    """Create a temp plugin dir. Returns its path.

    `skills` lets the caller reuse an already-discovered map — the
    spawn path scans once at wake start and threads the result through
    here and the system-prompt renderer. When None, we scan ourselves
    (preview.py, tests).

    Layout:
        <tmp>/physiclaw-plugin-<sid>-XXXX/
          .claude-plugin/plugin.json   {"name": "physiclaw-agent-<sid>", ...}
          skills/<name>                symlink to the skill's real dir
    """
    if skills is None:
        skills = skill.discover()

    root = Path(tempfile.mkdtemp(prefix=f"physiclaw-plugin-{sid}-"))
    meta = root / ".claude-plugin"
    meta.mkdir()
    (meta / "plugin.json").write_text(
        json.dumps(
            {"name": f"{_PLUGIN_NAME_PREFIX}-{sid}", "skills": "./skills"},
            indent=2,
        ) + "\n"
    )
    skills_dir = root / "skills"
    skills_dir.mkdir()

    linked: list[str] = []
    skipped: list[tuple[str, str]] = []

    # Claude-only first so they win on name collision.
    if _CLAUDE_ONLY_SKILLS_DIR.exists():
        for d in sorted(_CLAUDE_ONLY_SKILLS_DIR.iterdir()):
            if not d.is_dir() or not (d / "SKILL.md").exists():
                continue
            if _has_context_pollution(d):
                skipped.append((d.name, "contains stray CLAUDE.md or .claude/"))
                continue
            _link_or_copy(d.resolve(), skills_dir / d.name)
            linked.append(d.name)

    already = set(linked)
    for name, sk in skills.items():
        if name in already:
            continue
        if _has_context_pollution(sk.dir):
            skipped.append((name, "contains stray CLAUDE.md or .claude/"))
            continue
        _link_or_copy(sk.dir, skills_dir / name)
        linked.append(name)

    if skipped:
        for name, why in skipped:
            log.warning("skill %s skipped: %s — clean it up or delete it", name, why)
    log.info("claude plugin dir: %s skills=%s", root, sorted(linked))
    return root


def _has_context_pollution(skill_dir: Path) -> bool:
    """True if the skill dir contains files Claude Code would mistake
    for project-level context. A `SKILL.md` is expected; a `CLAUDE.md`
    or `.claude/` at any depth inside the skill is not — those would
    get discovered by Claude Code's auto-loader and leak doctrine that
    the skill author didn't explicitly publish through the plugin
    manifest.
    """
    if (skill_dir / "CLAUDE.md").exists():
        return True
    if (skill_dir / ".claude").is_dir():
        return True
    return False


def _link_or_copy(src: Path, dst: Path) -> None:
    """Symlink src→dst; fall back to copytree if the FS refuses symlinks
    (e.g. Windows without dev mode). Symlinks keep edits live."""
    try:
        dst.symlink_to(src)
    except OSError:
        shutil.copytree(src, dst, symlinks=True)
