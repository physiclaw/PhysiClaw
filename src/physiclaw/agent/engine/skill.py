"""Skill discovery and tiered dispatch.

A skill is a folder containing:
  SKILL.md             frontmatter (name, description) + body (the workflow)
  references/          detail files — loaded on demand (tier 3)
  assets/              freeform resources a reference may point at

Two roots are scanned, in order. The primary root is
``~/.physiclaw/skills/<name>/`` so a ``uv tool install``-ed server finds
skills without a repo cwd. The fallback is ``./skills/<name>/`` relative
to the current working directory — convenient when iterating on skills
from inside the repo. Primary wins on name collision.

Tiers (progressive disclosure):
  1. Metadata (name + description) is injected into the SYSTEM prompt by
     `render_section`. This is the ONLY signal the model has to decide
     whether the skill is relevant — descriptions must name triggers.
  2. `Skill(name=...)` returns the SKILL.md body as a tool_result.
  3. `Skill(name=..., reference=<path>)` loads `<skill>/references/<path>`
     as text, resolved under the winning skill's dir only.

Discovery is realpath-scoped within each root: a symlink that escapes
its root is rejected so third-party skill installs can't path-traverse.
"""
import logging
from dataclasses import dataclass
from pathlib import Path

from physiclaw import paths
from physiclaw.text import read_text

log = logging.getLogger(__name__)

HOME_SKILLS_DIR = paths.skills_dir()
CWD_SKILLS_DIR = Path("skills")


@dataclass
class Skill:
    """Snapshot of a skill at discover() time. `body` and `dir` are
    captured on session start; edits to SKILL.md mid-session are not
    reflected (deliberate: protects the SYSTEM prompt cache prefix)."""
    name: str
    description: str
    body: str
    dir: Path   # realpath; used to resolve references/ safely


def discover() -> dict[str, Skill]:
    """Scan cwd then home skill roots. Home wins on name collision
    (scanned last so `out[name] =` replaces). Missing SKILL.md skips
    silently — a broken skill shouldn't take down the session.
    """
    out: dict[str, Skill] = {}
    _scan_root(CWD_SKILLS_DIR, out)
    _scan_root(HOME_SKILLS_DIR, out)
    return out


def _scan_root(root: Path, out: dict[str, Skill]) -> None:
    if not root.exists():
        return
    root_real = root.resolve()
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        real = d.resolve()
        if not real.is_relative_to(root_real):
            log.warning(
                "skill %s resolves outside %s; skipping (path traversal guard)",
                d.name, root,
            )
            continue
        md = real / "SKILL.md"
        if not md.exists():
            continue
        fm, body = _split_frontmatter(read_text(md))
        name = fm.get("name") or d.name
        out[name] = Skill(
            name=name,
            description=(fm.get("description") or "").strip(),
            body=body.strip(),
            dir=real,
        )


def dispatch(skills: dict[str, Skill], args: dict) -> str:
    """Route a Skill() invocation. Raises ValueError / FileNotFoundError
    on bad input so the engine's _dispatch marks the tool_result as
    is_error=True (principle 5)."""
    name = (args.get("name") or "").strip()
    if not name:
        raise ValueError("Skill call requires a 'name' argument")
    skill = skills.get(name)
    if skill is None:
        available = ", ".join(sorted(skills.keys())) or "(none)"
        raise ValueError(f"unknown skill {name!r}. Available: {available}")

    ref = args.get("reference")
    if ref:
        return _load_reference(skill, ref)
    return skill.body


def _load_reference(skill: Skill, ref_path: str) -> str:
    # skill.dir is already a realpath from discover(); no need to resolve it
    # again. Only the target needs resolving to collapse any `..` segments
    # in `ref_path` before the scope check.
    root = skill.dir / "references"
    target = (root / ref_path).resolve()
    if not target.is_relative_to(root):
        raise ValueError(
            f"reference path {ref_path!r} escapes skill directory"
        )
    if not target.exists():
        raise FileNotFoundError(
            f"reference {ref_path!r} not found in skill {skill.name!r}"
        )
    return read_text(target)


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse `---\\nkey: value\\n...\\n---\\n<body>`. Simple scalar-only
    format; missing or malformed frontmatter returns ({}, text)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    fm_text = text[4:end]
    body = text[end + 4:].lstrip("\n")
    fm: dict[str, str] = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, body


def render_section(skills: dict[str, Skill]) -> str:
    """Markdown section listing available skills for the SYSTEM prompt.

    Tier-1 loading: description is the ONLY signal the model sees before
    invoking, so a skill with a weak description sits idle even when
    relevant. Authors: write descriptions as "use when X, Y, Z; NOT for
    A, B" — concrete triggers, not abstract capability blurbs.

    Invocation syntax is documented in the Skill tool's description (via
    the provider's tools= API); don't duplicate it here.
    """
    if not skills:
        return ""
    lines = ["## Available skills\n"]
    for s in skills.values():
        desc = s.description or "(no description)"
        lines.append(f"- **{s.name}** — {desc}")
    return "\n".join(lines)
