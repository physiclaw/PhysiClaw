"""SYSTEM prompt composition + prefix-cache verification.

Each section is a `list[str]` builder; the final prompt is `"\\n".join(lines)`.
Disabled sections return `[]` so their absence costs nothing.

Cache layout: the system message is stable within a session. Later wakes
can reuse the same prefix while its inputs stay unchanged, subject to the
provider's cache policy. Wake-volatile content (the cron-fired jobs block,
the trigger stamps) lives in the wake-trigger user message that follows,
so those changes do not invalidate the system prefix.

The inline `## Tooling` index card lists actions from the TurnOutput
schema — open-weight models often miss actions that appear
only in the schema, so the card is a redundant anchor. Providers whose
models read the schema reliably opt out through the LLM context policy
(see `agent.llm.context_policy`) and skip the card entirely.
"""

import hashlib
from pathlib import Path

from physiclaw.agent import layout
from physiclaw.agent.engine import memory
from physiclaw.agent.llm.context_policy import context_policy_for
from physiclaw.common.doctrine import fill_tokens
from physiclaw.common.text import read_text

CONTEXT_DIR = Path(__file__).resolve().parent.parent / "context"

# Doctrine quotes engine-enforced thresholds via `{{token}}` placeholders
# rendered by `common.doctrine.fill_tokens` — shared with the MCP
# instructions path (`core.server.mcp`), so both deliveries of the same
# file substitute identically. Config is process-constant, so the
# rendered bytes stay cache-stable across turns.


# OpenClaw-style modular doctrine: each named file is a slot with a defined
# role. Files are rendered in this fixed order; any missing file contributes
# nothing. Drop a new file in src/physiclaw/agent/context/ to opt into a slot — picked up
# on next session start without touching code.
DOCTRINE_FILE_ORDER = (
    "IDENTITY.md",  # who PhysiClaw is — short, one-paragraph card
    "USER.md",  # who PhysiClaw serves — read from memory/USER.md
    "SOUL.md",  # personality / tone / voice
    "AGENT.md",  # operational rules: Loop / Boundaries / Rules / Continuity
    "PHYSICLAW.md",  # tool-surface mechanics — also shipped via MCP initialize
    "TOOLS.md",  # extra user-authored tool guidance
    "PERSISTENCE.md",  # memory.md vs YYYY-MM-DD.md + read/write tools
    "JOBS.md",  # jobs.md + create_job/get_job/list_jobs/finish_job (immutable, append-only)
    "MACRO.md",  # run_macro semantics — rendered only when enabled macros exist
    "CONVENTION.md",  # engine turn rules — last so it sits next to mechanics
)


def render_system_prompts(
    *,
    memory_ctx: str = "",
    builtin_skills_ctx: str = "",
    user_skills_ctx: str = "",
    macros_ctx: str = "",
    pitfalls_ctx: str = "",
    provider_id: str = "",
    action_catalog: list[dict] | None = None,
) -> str:
    """Compose the full SYSTEM for one session — entirely session-stable.

    Order (above → below):
      # Doctrine           file-loop over DOCTRINE_FILE_ORDER. Slots:
                           IDENTITY, USER, SOUL, AGENT, PHYSICLAW, TOOLS,
                           PERSISTENCE, JOBS, MACRO, CONVENTION — each rendered as a
                           `## <name>` block; missing = skipped. USER reads
                           from memory/.
      ## Tooling           inline tool index card — a redundant anchor for
                           models that miss schema-only tools; skipped when
                           the LLM context policy disables the inline index
      ## Built-in Skills   built-in skills, FULL bodies (always active)
      ## Screen layout     learned input/keyboard bboxes the skills above use
      ## Available skills  official + user skills (### subsections),
                           name+description index (load on demand via Skill)
      ## Available Macros  enabled user macros — name/description/inputs
                           index (executed as one step via run_macro)
      ## Learned pitfalls  opt-in lessons from past runs (add_pitfall appends)
      ## memory.md         session-stable persistent facts — live file
                           dump (the spec lives in the PERSISTENCE.md slot)

    Built-in and user skills render into separate sections (both already
    carry their own `##` header + framing) so they never collide. Wake-
    volatile content (fired-jobs block, trigger stamps) lives in the user
    message the engine appends right after — keeping the system message
    byte-stable across wakes for cross-session cache hits.
    """
    # Pay only for what's used: with no enabled macros there is no
    # `## Available Macros` section, no run_macro tool, and no MACRO.md
    # doctrine — zero macro bytes in SYSTEM. Keyed on the same condition
    # as the section, so the prompt stays session-stable either way.
    doctrine_skip = () if macros_ctx else ("MACRO.md",)
    lines: list[str] = [
        *_render_doctrine(skip=doctrine_skip),
        *_render_tooling(action_catalog or [], provider_id),
        *_render_section(builtin_skills_ctx),
        *_render_screen_layout(),
        *_render_section(user_skills_ctx),
        *_render_section(macros_ctx),
        *_render_section(pitfalls_ctx),
        *_render_memory(memory_ctx),
    ]
    # No trailing newline: sections end with a separator blank, and the
    # CLI dump asserts byte-equality with this exact string.
    return "\n".join(lines).rstrip("\n")


# ---------- section builders ----------


def _render_doctrine(skip: tuple[str, ...] = ()) -> list[str]:
    """Emit one `## <FileName>` block per file in DOCTRINE_FILE_ORDER,
    bodies injected raw under a `# Doctrine` wrapper. Re-read every call
    so file edits take effect on the next wake without a restart. `skip`
    drops slots whose feature is inactive this session (MACRO.md)."""
    files = _load_doctrine_files(skip)
    if not files:
        return []
    out: list[str] = ["# Doctrine", ""]
    for name, body in files:
        out.append(f"## {name}")
        out.append("")
        out.append(body)
        out.append("")
    return out


def _load_doctrine_files(skip: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """Return [(name, body)] for files in DOCTRINE_FILE_ORDER that exist
    and are non-empty, in order. Missing or empty files are skipped — the
    user opts in to a slot by creating the file.

    USER.md routes through `memory.load_user()` so the gitignored
    user-private file (in `memory/`, not `src/physiclaw/agent/context/`) renders
    inside `# Doctrine` from a single source of truth."""
    out: list[tuple[str, str]] = []
    for name in DOCTRINE_FILE_ORDER:
        if name in skip:
            continue
        if name == "USER.md":
            body = memory.load_user()
        else:
            path = CONTEXT_DIR / name
            body = read_text(path).rstrip() if path.exists() else ""
        if not body:
            continue
        out.append((name, fill_tokens(name, body)))
    return out


def _render_tooling(action_catalog: list[dict], provider_id: str = "") -> list[str]:
    """Render the same eligible actions used by the TurnOutput schema.

    Context policies with inline_tool_index=False omit this redundant index.
    An empty catalog renders no index, useful for static doctrine previews.
    """
    if not context_policy_for(provider_id).inline_tool_index or not action_catalog:
        return []
    out: list[str] = [
        "## Tooling",
        "",
        "Choose exactly one case-sensitive action key in TurnOutput.action.",
        "",
    ]
    for s in action_catalog:
        name = s.get("name") or ""
        if not name:
            continue
        desc = _first_sentence(s.get("description") or "")
        out.append(f"- **{name}** — {desc}" if desc else f"- **{name}**")
    out.append("")
    return out


def _render_section(ctx: str) -> list[str]:
    """Include a pre-rendered skill section (built-in bodies or user index).
    Each already carries its own `##` header + framing from the skill
    module, so this just drops it in with a trailing blank line."""
    return [ctx, ""] if ctx else []


def _render_screen_layout() -> list[str]:
    """Inject the learned screen layout (Spotlight + chat input boxes +
    keyboard) once ALL three pages are captured. Read from
    ``~/.physiclaw/screen-layout/`` at session start — cache-stable.

    Nothing here until setup is complete: while pages are still missing the
    turn-volatile tail reminder (`layout.inject_tail`) lists the fields
    left to capture, and each `report_screen_layout` result confirms the save
    and how many remain. Once the last box lands, this loads the full layout on
    the next wake.
    """
    if not layout.is_learned():
        return []
    md = layout.load_layout_md() or "not set yet"
    return ["## Screen layout", "", md, ""]


def _render_memory(memory_ctx: str) -> list[str]:
    """Memory snapshot for this session. Sits ABOVE the cache boundary
    because it is byte-stable for the session's lifetime — re-emitted
    identically on every turn within one wake."""
    if not memory_ctx:
        return []
    return ["## memory.md", "", memory_ctx, ""]


# ---------- helpers ----------


def _first_sentence(text: str) -> str:
    """First sentence (or first line) of a tool description. Used to
    keep the inline tooling card to one line per tool."""
    text = text.strip()
    if not text:
        return ""
    line = text.splitlines()[0].strip()
    # Prefer a period-space cutoff, then a semicolon-space cutoff. This
    # intentionally simple heuristic does not recognize abbreviations.
    for sep in (". ", "; "):
        idx = line.find(sep)
        if 0 < idx < 200:
            return line[: idx + 1].rstrip()
    return line[:200].rstrip()


# provider.chat.history.serialize_history places cache markers using each
# adapter's CacheMarkers strategy. Prompt assembly supplies the content;
# the provider owns its wire representation.


def prefix_hash(system_prompt: str) -> str:
    """sha256 of the SYSTEM prompt content. Logged at session start so
    cache-hit rates are verifiable across wakes — the hash stays
    identical while doctrine/memory/tools don't change."""
    if not isinstance(system_prompt, str):
        raise ValueError(
            f"prefix_hash: system_prompt must be str, got {type(system_prompt).__name__}"
        )
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()


# ---------- offline dump ----------


def dump(
    *,
    action_catalog: list[dict] | None = None,
    memory_ctx: str = "",
    builtin_skills_ctx: str = "",
    user_skills_ctx: str = "",
    macros_ctx: str = "",
    pitfalls_ctx: str = "",
    provider_id: str = "",
) -> str:
    """Render the SYSTEM prompt the same way the engine does, with all
    inputs optional so callers can dump the static skeleton without
    spinning up MCP or loading memory."""
    return render_system_prompts(
        action_catalog=action_catalog,
        memory_ctx=memory_ctx,
        builtin_skills_ctx=builtin_skills_ctx,
        user_skills_ctx=user_skills_ctx,
        macros_ctx=macros_ctx,
        pitfalls_ctx=pitfalls_ctx,
        provider_id=provider_id,
    )
