"""Feeding the learned layout into the built-in skills."""

import re
from dataclasses import replace

from physiclaw.agent.layout import store
from physiclaw.agent.layout.store import _fmt, is_learned

# The built-in skill that drives first-run capture — dropped from the prompt
# once the layout is learned (its body is only useful during setup).
SKILL_NAME = "screen-layout"


def prune_builtin_skills(skills: dict) -> dict:
    """Drop the first-run `screen-layout` skill once the layout is learned —
    after setup its body is dead weight in `## Built-in Skills`. No-op (and a
    fresh dict) while still incomplete, so the skill stays available for
    capture."""
    if is_learned():
        return {k: v for k, v in skills.items() if k != SKILL_NAME}
    return dict(skills)


# Built-in skill markdown placeholder -> learned layout field. Keyed by skill:
# the same token means different fields in different skills (im's <paste-button>
# is the chat paste; open-app's is the Spotlight paste). Both have gesture
# templates the agent runs verbatim. Only tokens in those fenced templates
# are filled (filling is code-block-only, so a
# prose-only token like <backspace> would never substitute).
# Skill placeholders use the same `{{token}}` syntax as doctrine's
# config tokens (common.doctrine.DOCTRINE_TOKENS) — one placeholder
# style everywhere. Two domains though: doctrine tokens are ALL substituted
# (leftovers warn), while skill element tokens are substituted only
# here, only inside code blocks, only once the layout is learned —
# elsewhere they stay symbolic and the agent resolves them from
# SYSTEM § Screen layout.
_SKILL_BOX_TOKENS = {
    "im": {
        "{{input-hidden}}": "chat_input_kb_hidden",
        "{{input-visible}}": "chat_input_kb_visible",
        "{{paste-button}}": "chat_paste",
        "{{send}}": "send",
    },
    # Same token name, different field per skill: {{paste-button}} is the
    # CHAT paste box in `im` but the SPOTLIGHT paste box here.
    "open-app": {
        "{{search-field}}": "spotlight_input",
        "{{backspace}}": "backspace",
        "{{paste-button}}": "spotlight_paste",
    },
}


_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")


def _sub_in_code_blocks(body: str, subs: dict) -> str:
    """Substitute fenced code bodies, preserving prose, info strings and EOLs."""
    fence: str | None = None
    parts = []
    for line in body.splitlines(keepends=True):
        match = _FENCE.fullmatch(line.rstrip("\r\n"))
        if fence is None:
            if match:
                marker, info = match.groups()
                # Backticks inside a backtick fence's info string make it
                # inline/literal text, not an opening code fence.
                if marker[0] != "`" or "`" not in info:
                    fence = marker
        elif (
            match
            and match[1][0] == fence[0]
            and len(match[1]) >= len(fence)
            and not match[2].strip()
        ):
            fence = None
        else:
            for token, val in subs.items():
                line = line.replace(token, val)
        parts.append(line)
    return "".join(parts)


def fill_builtin_boxes(skills: dict) -> dict:
    """Once the layout is learned, swap the bbox placeholders
    (`{{input-hidden}}`, `{{send}}`, ...) for concrete coordinates INSIDE the
    skill's fenced code template (im's send `sequence`) so the agent runs it
    verbatim. Prose keeps the readable placeholder names — they reference
    § Screen layout. No-op (a fresh dict) while incomplete. Inputs aren't
    mutated; filled skills are copies."""
    layout = store._load()
    if not all(field in layout for field in store.ALL_FIELDS):
        return dict(skills)
    out = {}
    for name, s in skills.items():
        subs = {
            token: _fmt(layout[field])
            for token, field in _SKILL_BOX_TOKENS.get(name, {}).items()
            if layout.get(field)
        }
        body = _sub_in_code_blocks(s.body, subs) if subs else s.body
        out[name] = replace(s, body=body) if body != s.body else s
    return out
