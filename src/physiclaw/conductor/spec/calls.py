"""The model-call vocabulary the parser and the walk share, code-owned.

A playbook never defines a prompt shape or an answer space; it names
steps, and every model call behind a step reads its vocabulary from
here — so the parser's allowlist and the runner's answer space can
never disagree. `ESCALATE` is every call's exit: the model hands the
walk over rather than guessing.
"""

ESCALATE = "escalate"

# The `agent` step's episode vocabulary. `AGENT_DONE` and `ESCALATE`
# are every episode's exits; each grantable tool adds its verbs. One
# map, so the parser's allowlist (`AGENT_TOOLS`), the runner's per-turn
# answer space, and the candidate builder's reserved words
# (`ACT_VERBS`) cannot drift.
AGENT_DONE = "done"
# An agent reply is a TOOL CALL — one envelope for every move:
# `{"reason", "action": <tool name>, "args": {<that tool's arguments>},
# "confidence"}`. The tools are the ones a playbook grants (`tools:`),
# plus the macro run and the two exits; each has its own argument keys,
# spelled once in `TOOL_ARGS`: the parser requires exactly those, and a
# test pins each `TOOL_LEGEND` line to them, so the prompt can never
# describe a tool the code reads differently.
TOOL_TAP = "tap"  # args: label (what the box is), at (the box)
TOOL_SCROLL = "scroll"  # args: direction ("down" | "up")
TOOL_BACK = "back"  # args: none — the OS back edge-swipe
TOOL_RUN = "run_macro"  # args: name (a granted macro)
AGENT_TOOLS = (TOOL_TAP, TOOL_SCROLL, TOOL_BACK)  # what a playbook may grant
# The walk's routing arms behind the scroll and back tools (the swipe
# to send, the edge-swipe) — and parse_task's own `scroll_up` escape.
ACT_SCROLL_DOWN = "scroll_down"  # see content further down (the swipe goes up)
ACT_SCROLL_UP = "scroll_up"  # back toward the top (the swipe goes down)
ACT_BACK = "go_back"
# The scroll tool's direction words and the arm each one swipes by.
SCROLL_ARMS = {"down": ACT_SCROLL_DOWN, "up": ACT_SCROLL_UP}
# Every word an agent call's `action` may be. A recorded call's
# `allowed` mixes these with the granted macro names (kind-tagged,
# `macro:<name>`), and the parser tells them apart by this set.
ACTION_WORDS = frozenset({AGENT_DONE, ESCALATE, TOOL_RUN, *AGENT_TOOLS})
# The reply's fields. parse_task answers a question, so its word is an
# ANSWER; an agent call's is an ACTION with its ARGS beside it. A tap's
# `label` is the macro grammar's word for the same thing — what the box
# IS (`tap: "Paste"` + `at:`), so a model tap and a recorded step share
# one shape on the wire and in the log.
ANSWER = "answer"
ACTION = "action"
ARGS = "args"
LABEL = "label"
AT = "at"
DIRECTION = "direction"
NAME = "name"
# Each tool's argument keys, in the order the legend spells them.
# `done`'s args are the step's declared return fields, so it has no
# fixed keys here.
TOOL_ARGS: dict[str, tuple[str, ...]] = {
    TOOL_TAP: (LABEL, AT),
    TOOL_SCROLL: (DIRECTION,),
    TOOL_BACK: (),
    TOOL_RUN: (NAME,),
    AGENT_DONE: (),
    ESCALATE: (),
}
# What a granted macro or landmark may never be named as. A macro's
# name rides in its own arg (`name`), never in `action`, and a landmark
# is only shown (the model taps its box), so a tool name cannot shadow
# anything — a landmark called `back` is fine. The exits, the macro run
# and the walk's arms stay reserved: a landmark spelled `done` or
# `scroll_up` would read as a lie in the block and the log.
RESERVED_KEYS = frozenset(
    {AGENT_DONE, ESCALATE, TOOL_RUN, ACT_SCROLL_DOWN, ACT_SCROLL_UP, ACT_BACK}
)
# The output contract's own fields — a declared return field may not
# reuse one (it would collide with the envelope around the args).
CONTRACT_FIELDS = frozenset({"reason", ANSWER, ACTION, ARGS, "confidence"})
# How each tool reads in the legend: its args, key by key, and what
# they take. One line per tool, the same shape for every tool, so a
# model reads the list as a menu. Every entry is a `str.format`
# template (JSON braces doubled) rendered through `legend_line`, so a
# fill such as the granted macros can join any line the same way.
TOOL_LEGEND: dict[str, str] = {
    TOOL_TAP: (
        'tap: {{"label": "<what the box is: the element\'s on-screen text, or a '
        'short description when it has none>", "at": [left, top, right, bottom]}} — '
        "the box in 0-1 fractions of the screenshot as the listing spells "
        "them: copy a listed element's or a granted landmark's box exactly "
        "when one covers the spot, else read it off the screenshot"
    ),
    TOOL_SCROLL: (
        'scroll: {{"direction": "down" | "up"}} — down shows what lies further '
        "down the page"
    ),
    TOOL_BACK: "back: {{}} — the OS back gesture, leaves the current page",
    TOOL_RUN: (
        'run_macro: {{"name": "<a granted macro name, copied exactly: {macros}>"}} '
        "— runs that recorded gesture sequence"
    ),
    AGENT_DONE: (
        'done: {{"<return field>": "<plain string>", …}} — ONLY when the goal is '
        "fully met; one key per return field listed, nothing else"
    ),
    ESCALATE: (
        "escalate: {{}} — when you are stuck, the screen is unexpected, or the "
        "goal needs a tool you were not given"
    ),
}


# The pure-text call (no screen) has two tools; its escalate is worded
# for a brief, not a screen. Same template rules as `TOOL_LEGEND`.
TEXT_CALL_LEGEND: dict[str, str] = {
    AGENT_DONE: TOOL_LEGEND[AGENT_DONE],
    ESCALATE: (
        "escalate: {{}} — when the brief cannot be fulfilled from what it gives you"
    ),
}
# Every agent legend opens with this line, then one line per tool.
TOOLS_HEADER = (
    'Tools — "action" is exactly ONE of these names and "args" is that '
    "tool's arguments, exactly these keys:"
)


def legend_line(tool: str, table: dict[str, str] | None = None, **fills: str) -> str:
    """One tool's legend line from `table` (the episode's by default),
    its fills applied (unused fills ignored)."""
    return (table or TOOL_LEGEND)[tool].format(**fills)
