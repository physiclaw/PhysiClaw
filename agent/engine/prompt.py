"""SYSTEM prompt composition + prefix-cache verification.

Each section is a `list[str]` builder; the final prompt is `"\\n".join(lines)`.
Disabled sections return `[]` so their absence costs nothing.

Cache layout: session-stable content above CACHE_BOUNDARY (doctrine, tools,
memory); wake-volatile content below (cron-fired jobs). `prefix_hash`
anchors on the prefix so it stays constant across wakes whose only
difference is which crons fired.

The inline `## Tooling` index card duplicates the schema sent via the
provider's `tools=` API — open-weight models often miss tools that appear
only in the schema, so the card is a redundant anchor.
"""
import hashlib
import logging
from pathlib import Path

from agent.engine import memory, mcp_inventory

log = logging.getLogger(__name__)

CONTEXT_DIR = Path(__file__).resolve().parent.parent / "context"

CACHE_BOUNDARY = "<!-- prefix-cache-boundary -->"

# OpenClaw-style modular doctrine: each named file is a slot with a defined
# role. Files are rendered in this fixed order; any missing file contributes
# nothing. Drop a new file in agent/context/ to opt into a slot — picked up
# on next session start without touching code.
DOCTRINE_FILE_ORDER = (
    "IDENTITY.md",    # who PhysiClaw is — short, one-paragraph card
    "OWNER.md",       # who PhysiClaw serves — read from memory/OWNER.md
    "SOUL.md",        # personality / tone / voice — gets embody-note prepended
    "AGENT.md",       # operational rules: Loop / Boundaries / Rules / Continuity
    "PHYSICLAW.md",   # tool-surface mechanics — also shipped via MCP initialize
    "TOOLS.md",       # extra owner-authored tool guidance
    "PERSISTENCE.md", # memory.md vs YYYY-MM-DD.md + read/write tools
    "JOBS.md",        # jobs.md + create_job/get_job/list_jobs/finish_job (immutable, append-only)
    "CONVENTION.md",  # engine turn rules — last so it sits next to mechanics
)


def render_system(
    *,
    local_tool_schemas: list[dict] | None = None,
    memory_ctx: str = "",
    cron_ctx: str = "",
    skills_ctx: str = "",
    provider_name: str = "",
) -> str:
    """Compose the full SYSTEM for one session.

    Order (above → below):
      # Doctrine          file-loop over DOCTRINE_FILE_ORDER. Slots:
                          IDENTITY, OWNER, SOUL, AGENT, PHYSICLAW, TOOLS,
                          CONVENTION — each rendered as `## <name>` block;
                          missing = skipped. OWNER reads from memory/.
      ## Tooling          inline tool index card (Qwen reliability)
      ## Skill selection  decision-tree wrapper around `skills_ctx`
      ## Examples         ❌/✅ for the most common per-turn failures
      ## Reasoning Format  Qwen-only `<think>` wrapper
      ## memory.md        session-stable persistent facts — live file
                          dump (the spec lives in the PERSISTENCE.md slot)
      CACHE_BOUNDARY      seam between session-stable and wake-volatile
      cron_ctx            jobs firing now (changes every wake)
    """
    lines: list[str] = [
        *_render_doctrine(),
        *_render_tooling(local_tool_schemas or []),
        *_render_skills(skills_ctx),
        *_render_examples(),
        *_render_reasoning_format(provider_name),
        *_render_memory(memory_ctx),
        CACHE_BOUNDARY,
        cron_ctx,
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

    OWNER.md routes through `memory.load_owner()` so the gitignored
    owner-private file (in `memory/`, not `agent/context/`) renders
    inside `# Doctrine` from a single source of truth."""
    out: list[tuple[str, str]] = []
    for name in DOCTRINE_FILE_ORDER:
        if name == "OWNER.md":
            body = memory.load_owner()
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
    `physiclaw/server/tools.py`) — works offline, no MCP roundtrip. Local
    engine tools come from the caller. The two sources don't overlap by
    name, so the merge is a simple concat.

    Why the inline card at all: open-weight models (Qwen, Kimi) routinely
    forget tools that appear only in the native `tools=` payload."""
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
        "✅ Right: after reading the IM, `[note, update_progress(owner_said=..., understanding=..., steps=[...])]` with every step statused (first step `in_progress`, rest `pending`). When the screen confirms a step's intent (add-to-cart toast appears, search results load, etc.): `[note, update_progress(steps=[...])]` flipping that step to `completed` and the next to `in_progress`.",
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


def _render_reasoning_format(provider_name: str) -> list[str]:
    """Qwen-family wrapper: keep chain-of-thought inside `<think>...</think>`
    so it does not leak into tool arguments. No-op for Anthropic/OpenAI
    providers, which handle reasoning out-of-band."""
    if "qwen" not in (provider_name or "").lower():
        return []
    return [
        "## Reasoning Format",
        "Wrap internal reasoning in `<think>...</think>`. Anything outside `<think>` is interpreted as either a tool call or a user-visible reply.",
        "Never put reasoning inside tool arguments — handlers receive `args` raw, not your scratchpad.",
        "",
    ]


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


# ---------- cache-anchor verification ----------


def prefix_hash(messages: list[dict]) -> str:
    """sha256 of the SYSTEM message's stable prefix (everything ABOVE
    CACHE_BOUNDARY). Logged at session start so cache-hit rates are
    verifiable; the boundary anchor means cron context changes between
    sessions don't move the hash."""
    if not messages or messages[0].get("role") != "system":
        raise ValueError("prefix_hash: messages[0] must be the system message")
    content = messages[0].get("content", "")
    if not isinstance(content, str):
        raise ValueError(
            f"prefix_hash: system content must be str, got {type(content).__name__}"
        )
    stable = content.split(CACHE_BOUNDARY, 1)[0]
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


# ---------- offline dump ----------


def dump(
    *,
    local_tool_schemas: list[dict] | None = None,
    memory_ctx: str = "",
    cron_ctx: str = "",
    skills_ctx: str = "",
    provider_name: str = "",
    keep_boundary: bool = True,
) -> str:
    """Render the SYSTEM prompt the same way the engine does, with all
    inputs optional so callers can dump the static skeleton without
    spinning up MCP / loading memory / firing cron.

    `keep_boundary=False` strips the boundary marker for a cleaner read
    (useful when paging the dump into a terminal)."""
    out = render_system(
        local_tool_schemas=local_tool_schemas,
        memory_ctx=memory_ctx,
        cron_ctx=cron_ctx,
        skills_ctx=skills_ctx,
        provider_name=provider_name,
    )
    if not keep_boundary:
        out = out.replace(CACHE_BOUNDARY + "\n", "").replace(CACHE_BOUNDARY, "")
    return out
