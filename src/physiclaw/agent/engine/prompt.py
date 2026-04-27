"""SYSTEM prompt composition + prefix-cache verification.

Each section is a `list[str]` builder; the final prompt is `"\\n".join(lines)`.
Disabled sections return `[]` so their absence costs nothing.

Cache layout: the system message is entirely session-stable, so it
caches once and hits on every subsequent turn — and on the first turn
of every later wake within the 5-min TTL. Wake-volatile content (the
cron-fired jobs block, the trigger stamps) lives in the wake-trigger
user message that follows, keeping the system bytes identical across
wakes. DashScope caches at message granularity, so this is the only
structure that gets cross-session hits.

The inline `## Tooling` index card duplicates the schema sent via the
provider's `tools=` API — open-weight models often miss tools that appear
only in the schema, so the card is a redundant anchor.
"""
import hashlib
import logging
from pathlib import Path

from physiclaw.agent.engine import memory, mcp_inventory

log = logging.getLogger(__name__)

CONTEXT_DIR = Path(__file__).resolve().parent.parent / "context"

# OpenClaw-style modular doctrine: each named file is a slot with a defined
# role. Files are rendered in this fixed order; any missing file contributes
# nothing. Drop a new file in src/physiclaw/agent/context/ to opt into a slot — picked up
# on next session start without touching code.
DOCTRINE_FILE_ORDER = (
    "IDENTITY.md",    # who PhysiClaw is — short, one-paragraph card
    "USER.md",        # who PhysiClaw serves — read from memory/USER.md
    "SOUL.md",        # personality / tone / voice — gets embody-note prepended
    "AGENT.md",       # operational rules: Loop / Boundaries / Rules / Continuity
    "PHYSICLAW.md",   # tool-surface mechanics — also shipped via MCP initialize
    "TOOLS.md",       # extra user-authored tool guidance
    "PERSISTENCE.md", # memory.md vs YYYY-MM-DD.md + read/write tools
    "JOBS.md",        # jobs.md + create_job/get_job/list_jobs/finish_job (immutable, append-only)
    "CONVENTION.md",  # engine turn rules — last so it sits next to mechanics
)


def render_system(
    *,
    local_tool_schemas: list[dict] | None = None,
    memory_ctx: str = "",
    skills_ctx: str = "",
    provider_id: str = "",
) -> str:
    """Compose the full SYSTEM for one session — entirely session-stable.

    Order (above → below):
      # Doctrine           file-loop over DOCTRINE_FILE_ORDER. Slots:
                           IDENTITY, USER, SOUL, AGENT, PHYSICLAW, TOOLS,
                           CONVENTION — each rendered as `## <name>` block;
                           missing = skipped. USER reads from memory/.
      ## Tooling           inline tool index card (Qwen reliability)
      ## Skill selection   decision-tree wrapper around `skills_ctx`
      ## Examples          ❌/✅ for the most common per-turn failures
      ## Reasoning Format  provider-specific reasoning wrapper (e.g. Qwen
                           `<think>...</think>`); pulled from
                           `OpenAICompatibleProvider.system_prompt_fragment()`
      ## memory.md         session-stable persistent facts — live file
                           dump (the spec lives in the PERSISTENCE.md slot)

    Wake-volatile content (fired-jobs block, trigger stamps) lives in
    the user message the engine appends right after — keeping the
    system message byte-stable across wakes for cross-session cache
    hits.
    """
    lines: list[str] = [
        *_render_doctrine(),
        *_render_tooling(local_tool_schemas or []),
        *_render_skills(skills_ctx),
        *_render_examples(),
        *_render_reasoning_format(provider_id),
        *_render_memory(memory_ctx),
    ]
    return "\n".join(lines)


# ---------- section builders ----------


def _render_doctrine() -> list[str]:
    """Emit one `## <FileName>` block per file in DOCTRINE_FILE_ORDER,
    bodies injected raw under a `# Doctrine` wrapper. Re-read every call
    so file edits take effect on the next wake without a restart."""
    files = _load_doctrine_files()
    if not files:
        return []
    out: list[str] = ["# Doctrine", ""]
    for name, body in files:
        out.append(f"## {name}")
        out.append("")
        out.append(body)
        out.append("")
    return out


def _load_doctrine_files() -> list[tuple[str, str]]:
    """Return [(name, body)] for files in DOCTRINE_FILE_ORDER that exist
    and are non-empty, in order. Missing or empty files are skipped — the
    user opts in to a slot by creating the file.

    USER.md routes through `memory.load_user()` so the gitignored
    user-private file (in `memory/`, not `src/physiclaw/agent/context/`) renders
    inside `# Doctrine` from a single source of truth."""
    out: list[tuple[str, str]] = []
    for name in DOCTRINE_FILE_ORDER:
        if name == "USER.md":
            body = memory.load_user()
        else:
            path = CONTEXT_DIR / name
            body = path.read_text().rstrip() if path.exists() else ""
        if not body:
            continue
        out.append((name, body))
    return out


def _render_tooling(local_tool_schemas: list[dict]) -> list[str]:
    """Inline tool index card. Each tool gets one line: `- **name** — first
    sentence`.

    MCP tools come from `mcp_inventory.discover_mcp_tools()` (AST parse of
    `src/physiclaw/core/server/tools.py`) — works offline, no MCP roundtrip. Local
    engine tools come from the caller. The two sources don't overlap by
    name, so the merge is a simple concat.

    Why the inline card at all: open-weight models (Qwen, Moonshot)
    routinely forget tools that appear only in the native `tools=` payload."""
    all_tools = mcp_inventory.discover_mcp_tools() + (local_tool_schemas or [])
    if not all_tools:
        return []
    out: list[str] = [
        "## Tooling",
        "Tools below are also available via the native tool-call API. "
        "Names are case-sensitive; call them exactly as listed.",
        "",
    ]
    for s in all_tools:
        name = s.get("name") or ""
        if not name:
            continue
        desc = _first_sentence(s.get("description") or "")
        out.append(f"- **{name}** — {desc}" if desc else f"- **{name}**")
    out.append("")
    return out


def _render_skills(skills_ctx: str) -> list[str]:
    """Wrap `skills_ctx` (which already includes its own `## Available
    skills` header + bullets) with an explicit decision tree.

    OpenClaw's pattern: tell the model exactly when to read a skill and
    when not to. Vague guidance ("invoke if it fits") leaves Qwen-class
    models stuck — either they invoke nothing, or they over-invoke and
    burn turns reading every SKILL.md."""
    if not skills_ctx:
        return []
    return [
        "## Skill selection",
        "**App-specific skills are mandatory.** Before acting in any app listed below, invoke `Skill(name=...)` first and read the workflow — skills encode app-specific traps you can't see on the screen. Invoke at most one skill up-front; apps not listed, proceed without a skill.",
        "",
        skills_ctx,
        "",
    ]


def _render_examples() -> list[str]:
    """Concrete ❌ Wrong / ✅ Right patterns for the failure modes Qwen
    hits most often. Each pattern restates a rule already in AGENT.md or
    CONVENTION.md, paired with a worked example.

    OpenClaw scatters this style throughout (Silent Replies, Reply Tags,
    etc.); we collect it into one section because our failure surface
    is small enough to enumerate."""
    return [
        "## Examples",
        "",
        "**Every turn is exactly `[note, one-other]`.** No more, no less. Splits clearly across turns — one action per turn.",
        "❌ Wrong: `[peek]` alone (missing note), or `[note, append_log, end_session]` (too many).",
        "✅ Right: `[note(summary=\"peek to find the Send button in the WeChat chat with Alice\"), peek()]`.",
        "",
        "**Draft the plan and tick it step-by-step.** (Rationale in CONVENTION.md § The plan.)",
        "❌ Wrong: complete step 1, never call `update_progress` again, lose track at step 3. Or: tick after every tap (a step spans many taps; tick on intent-achieved, not per action).",
        "✅ Right: after reading the IM, `[note, update_progress(user_said=..., understanding=..., steps=[...])]` with every step statused (first step `in_progress`, rest `pending`). When the screen confirms a step's intent (add-to-cart toast appears, search results load, etc.): `[note, update_progress(steps=[...])]` flipping that step to `completed` and the next to `in_progress`.",
        "",
        "**Only the latest screen survives.** Earlier peek/screenshot results get dropped from history.",
        "❌ Wrong: rely on a `peek` from three turns ago to plan the current tap.",
        "✅ Right: if you need to re-check, re-observe — `peek` is cheap. Your `note.summary` from past turns carries the context you need.",
        "",
        "**Bboxes come from the listing, character-for-character.** Every bbox you pass to tap/swipe/sequence must be transcribed from a listing row's bracket contents — same digits, same order, no retyping from memory, no rounding, no digit drift.",
        "❌ Wrong (eyeballing): `tap(bbox=[0.47, 0.82, 0.51, 0.88])` where those numbers came from looking at the image.",
        "❌ Wrong (digit drift): listing row is `45 [icon] \"\" [0.520,0.662,0.717,0.775] 0.56`, you emit `tap(bbox=[0.518, 0.662, 0.717, 0.775])`. The `0.518` is a regeneration, not a copy — on small targets a 0.002 drift lands on the neighboring icon.",
        "✅ Right: find the target row in the listing, read the four numbers between its second pair of brackets, transcribe them exactly (`0.520` stays `0.520`, not `0.52` and not `0.518`) into `tap(bbox=...)`. If the target isn't in the current listing or a surviving text row from an earlier stub, re-peek or escalate to `screenshot` — don't fabricate coords.",
        "",
        "**When `peek` doesn't list your target, `screenshot` — don't peek again.** Camera glare and small icons (especially app icons in a 6×4 home grid) make the camera-based `peek` lose elements. Re-peeking gives you the same gap. `screenshot` uses the phone's own pixel-perfect capture and catches what the camera misses. The ~12s cost beats burning 10 turns on a hidden target.",
        "❌ Wrong: `peek` home screen, no JD icon listed → `peek` again, still no JD → tap a Safari link labeled \"JD\" because it's the only `JD`-string match. Land on the wrong page, loop until STUCK.",
        "✅ Right: `peek` home screen, no JD icon → next turn `screenshot` for pixel-perfect bboxes. The JD icon was always there; the camera just couldn't resolve it.",
        "",
    ]


def _render_reasoning_format(provider_id: str) -> list[str]:
    """Provider-specific reasoning fragment (e.g. Qwen's `<think>` wrapper),
    pulled from the vendor class's `system_prompt_fragment()` classmethod.
    Empty when the provider doesn't override it."""
    from physiclaw.agent.provider import provider_class

    cls = provider_class(provider_id)
    if cls is None:
        return []
    fragment = cls.system_prompt_fragment()
    if not fragment:
        return []
    return ["## Reasoning Format", fragment, ""]


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
    line = (text or "").strip().split("\n", 1)[0].strip()
    if not line:
        return ""
    # Crude but predictable: cut at the first period followed by a space
    # (so "e.g. foo" inside a sentence doesn't get truncated).
    for sep in (". ", "; "):
        idx = line.find(sep)
        if 0 < idx < 200:
            return line[: idx + 1].rstrip()
    return line[:200].rstrip()


# Cache-marker placement lives in the provider classes — see
# `BaseProvider.serialize_history` (template) and the per-shape
# `_mark_system` / `_mark_stub` hooks. It's a wire-format concern that
# belongs with the request builder, not the prompt assembly.


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
    local_tool_schemas: list[dict] | None = None,
    memory_ctx: str = "",
    skills_ctx: str = "",
    provider_id: str = "",
) -> str:
    """Render the SYSTEM prompt the same way the engine does, with all
    inputs optional so callers can dump the static skeleton without
    spinning up MCP or loading memory."""
    return render_system(
        local_tool_schemas=local_tool_schemas,
        memory_ctx=memory_ctx,
        skills_ctx=skills_ctx,
        provider_id=provider_id,
    )
