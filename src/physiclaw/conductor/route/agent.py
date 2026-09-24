"""The `agent` move — its `context:` block (the brief, what it may name,
the memory parts), its tools and macros, its `never_tap:` fence, its
return fields and limits; the grant guard that holds the fence and the
grants to each other.
"""

from dataclasses import dataclass
from typing import Any

from physiclaw.conductor.route.fields import (
    irreversible_class,
    limit_int,
    limit_mapping,
    on_fail_mode,
    think_level,
    unique_list,
)
from physiclaw.conductor.route.resolve import argless_macro, landmark_name, prompt_text
from physiclaw.conductor.route.scope import Line, Scope
from physiclaw.conductor.spec import match, memory
from physiclaw.conductor.spec.calls import (
    AGENT_TOOLS,
    CONTRACT_FIELDS,
    RESERVED_KEYS,
    TOOL_TAP,
)
from physiclaw.conductor.spec.limits import (
    DEFAULT_AGENT_CALLS,
    DEFAULT_AGENT_SCROLLS,
    MAX_AGENT_CALLS,
    MAX_CONTEXT,
    MAX_NEVER_TAP,
    MAX_PROMPT_LEN,
    MAX_RETURNS,
)
from physiclaw.conductor.spec.match import normalize
from physiclaw.conductor.spec.model import (
    PAYMENT,
    AgentNode,
    PlaybookError,
    prose,
    require_str,
)
from physiclaw.conductor.spec.pages import AnchorDecl, parse_target
from physiclaw.conductor.spec.refs import (
    BARE_REF_RE,
    check_refs,
    field_name,
    names_in,
    refs_in,
)
from physiclaw.macros.model import (
    LANDMARKS_KIND,
    MACROS_KIND,
    Macro,
    app_ref,
    parse_ref,
)

_AGENT_LIMIT_KEYS = {"calls", "scrolls"}


def _guard_grants(
    scope: Scope,
    where: str,
    never_tap: tuple[AnchorDecl, ...],
    spots: tuple[str, ...],
    granted: tuple[Macro, ...],
) -> None:
    """Refuse a grant that walks around this episode's `never_tap:`.

    Both contradictions are declared by name, so both belong here rather
    than at run time. A granted LANDMARK is a box the model may press
    blind, and a landmark with no text row leaves the runtime check
    nothing to find. A granted MACRO presses its own recorded targets
    without ever proposing a tap — its labels are refused here, and its
    boxes at run time against the live screen (`spec.fence.macro_refusal`,
    off the same `Macro.taps`), since a label is the author's word and
    the same coordinates under another word tap the same button.
    Node-scoped on purpose: the payment move's macro presses these same
    buttons by design and stays legal."""
    if not never_tap:
        return

    def _named(labels: tuple[str, ...]) -> str | None:
        """Which target these recorded labels name, by the rule the
        RUNTIME reads a screen row with (`match.label_matches`) — not
        exact equality. The natural thing to write is the label as the
        listing shows it, price and all ("Pay now ($3.60)"), which is
        not equal to the target and is the same button."""
        written = [normalize(w) for w in labels]
        for target in never_tap:
            if any(
                match.label_matches(normalize(reading), w, ())
                for reading in target.readings
                for w in written
            ):
                return " / ".join(target.readings)
        return None

    for name in spots:
        hit = _named(scope.pack.landmarks[name].label)
        if hit is not None:
            raise PlaybookError(
                f"{where}: `context.given` names landmark {name!r}, which this step "
                f"declares never_tap ({hit}) — the grant would hand the model "
                "the box the guard exists to refuse"
            )
    for macro in granted:
        for tap in macro.taps():
            hit = _named(tap.label)
            if hit is not None:
                raise PlaybookError(
                    f"{where}: `tools` grants macro {macro.name!r}, which presses "
                    f"{hit} — this step declares that never_tap, and a macro "
                    "runs its recorded steps without proposing a tap"
                )
            if any(isinstance(v, str) for v in tap.bbox):
                # A box the run fills from an input default is one the
                # runtime guard cannot judge against the screen (a
                # placeholder has no centre); under a never_tap it is
                # refused here rather than let through unjudged.
                raise PlaybookError(
                    f"{where}: `tools` grants macro {macro.name!r}, whose "
                    f"{tap.label[0]!r} box carries a placeholder — this step "
                    "declares never_tap, and a box filled at run time cannot "
                    "be judged against it; record the box"
                )


def _never_tap(entry: dict, where: str) -> tuple[AnchorDecl, ...]:
    """`never_tap:` — the targets an episode's taps may never land on.
    Each item is a reading, alternate readings of ONE target, or a
    mapping with `label:` and an optional `within:` band — the target
    shape a page anchor takes, read by the same parser
    (`pages.parse_target`), since the same row matcher reads both."""
    raw = entry.get("never_tap")
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise PlaybookError(f"{where}: `never_tap` takes a non-empty LIST")
    if len(raw) > MAX_NEVER_TAP:
        raise PlaybookError(f"{where}: at most {MAX_NEVER_TAP} `never_tap` targets")
    return tuple(
        parse_target(
            item,
            f"{where}: `never_tap[{i}]`",
            key="label",
            require_str=require_str,
            err=PlaybookError,
        )
        for i, item in enumerate(raw)
    )


# What an agent's `context:` may hold: the brief, what it may name,
# and the parts of the agent's memory to read. One listing, so the
# unknown-key check and the prose can never drift apart.
_CONTEXT_KEYS = ("prompt", "given", "memory")


@dataclass(frozen=True)
class _Context:
    """An agent's `context:`, parsed — everything the call is built
    from, under `AgentNode`'s names."""

    prompt: str
    given: dict[str, str]
    memory: dict[str, Any]
    landmarks: dict[str, str]


def _parse_context(
    scope: Scope, where: str, entry: dict, payloads: dict[str, tuple[str, ...]]
) -> _Context:
    """An agent's `context:` — everything the call is built from, in one
    block: `prompt:` the APP brief, `given:` what it may name (`<name>:
    <ref>` for a value the walk fills, `<name>: app.landmarks.<n>` for
    a fixed spot a tap may aim at — either written `{name}` in the
    prompt and rendered there once when the step opens), and `memory:`
    the parts of the agent's own memory to read (`spec.memory`). `given:`
    is the whole of what a prompt may name: a `{name}` it does not hold
    is refused, and a name the prompt never writes is refused too, so
    what a prompt reads is declared, exactly. Memory rides one stamped
    data block below the brief, because a memory line is never an
    instruction. Permission is in neither: the tap legend is what says
    a box may be a granted landmark's, and a landmark given only says
    where it is — whether this step's hands can use it is the step's
    question (`parse_agent`), not the block's.

    The two are bounded TOGETHER (`MAX_CONTEXT`): they are what a
    reader has to hold in their head to read the prompt."""
    raw = entry.get("context")
    keys = ", ".join(f"`{k}:`" for k in _CONTEXT_KEYS)
    if not isinstance(raw, dict):
        raise PlaybookError(
            f"{where}: `context:` is the block the call is built from — {keys}"
        )
    unknown = sorted(set(raw) - set(_CONTEXT_KEYS))
    if unknown:
        raise PlaybookError(
            f"{where}: `context:` has unknown key(s) {', '.join(unknown)} — "
            f"it holds {keys}"
        )
    parts = raw.get("memory", {})
    gap = memory.memory_gap(parts)
    if gap is not None:
        raise PlaybookError(f"{where}: `context.memory` {gap}")
    # A memory part is rendered under its name, so the label the model
    # reads cannot be spelled like an answer it gives.
    for part in parts:
        if part in RESERVED_KEYS:
            raise PlaybookError(
                f"{where}: `context.memory` names {part!r} — that is a fixed "
                "episode answer, and a label the model reads cannot be spelled "
                "like one"
            )

    # `given:` — each entry under the name the prompt writes it by, a
    # landmark reference or a ref the walk fills, told apart by the
    # spelling (`app.landmarks.<n>` is never a walk ref).
    block = raw.get("given", {})
    at = f"{where}: `context.given`"
    if not isinstance(block, dict):
        raise PlaybookError(
            f"{at} must be a mapping of `<name>: <ref>` or `<name>: "
            f"{app_ref(LANDMARKS_KIND, '<name>')}` — each one under the name "
            "its prompt writes"
        )
    given: dict[str, str] = {}
    landmarks: dict[str, str] = {}
    for name, value in block.items():
        field_name(name, f"{at} name")
        one = f"{at}.{name}"
        # A pack reference (`parse_ref` reads a plain word as a kindless
        # one, so the kind is the test) is a landmark or nothing.
        r = parse_ref(value) if isinstance(value, str) else None
        if r is not None and r.kind is not None:
            if not r.is_app(LANDMARKS_KIND):
                raise PlaybookError(
                    f"{one}: {value!r} — a given is a value the walk fills "
                    f"(`inputs.x`, `node.field`) or a landmark "
                    f"(`{app_ref(LANDMARKS_KIND, '<name>')}`)"
                )
            landmarks[name] = landmark_name(scope, value, one)
            continue
        # A value the walk fills, checked like any other move's `with:`
        # and filled the same way when the step opens — written as the
        # bare ref when it is nothing else (`inputs.user_said`), or as a
        # template holding one (`"{inputs.user_said}"`).
        text = require_str(value, one)
        if BARE_REF_RE.fullmatch(text):
            text = f"{{{text}}}"
        refs = refs_in(text, one)
        if not refs:
            raise PlaybookError(
                f"{one}: {text!r} holds no ref — a given is a value the walk "
                "fills (`{{inputs.x}}`, `{{node.field}}`); text that never "
                "changes belongs in the prompt"
            )
        check_refs(refs, scope.input_names, payloads, one)
        given[name] = text
    if len(block) + len(parts) > MAX_CONTEXT:
        raise PlaybookError(
            f"{where}: `context:` reads {len(block) + len(parts)} things > max "
            f"{MAX_CONTEXT}"
        )
    # One namespace across the two: a prompt reads a given as `{name}`
    # and a memory part by its label in the block below, so a name in
    # both could mean either.
    twice = sorted(set(block) & set(parts))
    if twice:
        raise PlaybookError(
            f"{where}: `context:` reads {', '.join(twice)} twice — the block "
            "is one list of labels, so each name is read once"
        )
    # The brief, read against the block it may name.
    at = f"{where}: `context.prompt`"
    prompt = prompt_text(scope, require_str(raw.get("prompt"), at), where)
    if len(prompt) > MAX_PROMPT_LEN:
        raise PlaybookError(f"{at} is {len(prompt)} characters (max {MAX_PROMPT_LEN})")
    # `given:` is the whole of what a prompt may name — each way round.
    written = names_in(prompt, at)
    declared = ", ".join(block) or "nothing"
    unknown = sorted(written - set(block))
    if unknown:
        raise PlaybookError(
            f"{at} refers to {{{unknown[0]}}}, which `context.given:` does not "
            f"hold (it holds {declared}) — every name a prompt writes is "
            "declared there"
        )
    unused = sorted(set(block) - written)
    if unused:
        raise PlaybookError(
            f"{where}: `context.given` holds {unused[0]!r}, which the prompt "
            f"never writes as {{{unused[0]}}} — a given is a value the prompt "
            "reads; refer to it or drop it"
        )
    return _Context(prompt=prompt, given=given, memory=dict(parts), landmarks=landmarks)


def grant(scope: Scope, value: Any, where: str, nid: str) -> Macro:
    """One macro in a `tools:` list: `macros.<name>` for this route's own
    hand, `app.macros.<name>` for the pack's — argument-less, like every
    helper hand, and never spelled like a fixed answer (`done`,
    `escalate`, a verb) that the episode legend already owns. A macro's
    name is its dispatch name (`argless_macro`)."""
    r = parse_ref(value) if isinstance(value, str) else None
    own = r is not None and not r.shared and r.kind == MACROS_KIND
    if r is None or not (own or r.is_app(MACROS_KIND)):
        raise PlaybookError(
            f"{where}: {value!r} is neither a gesture "
            f"({', '.join(AGENT_TOOLS)}) nor a macro to run "
            f"(`macros.<name>`, this route's own hand, or "
            f"`{app_ref(MACROS_KIND, '<name>')}`, the pack's)"
        )
    if r.name in RESERVED_KEYS:
        raise PlaybookError(
            f"{where}: {r.name!r} is a fixed episode answer — a granted "
            "macro cannot be spelled like one"
        )
    return argless_macro(
        value if r.shared else r.name, "tools", where, nid, scope.resolve
    )


def parse_agent(scope: Scope, line: Line) -> AgentNode:
    """An `agent` move. No `tools` = a pure-text call (needs `returns`,
    no pages); tools = an acting episode framed by the adjacent
    waypoints exactly like a `do`.

    Everything the call is built from is `context:` — the brief, the
    values it names, the memory parts — parsed by `_parse_context`. A
    given may quote the step's own returns (its last answer, empty the
    first time — what a revision re-reads), so they are declared before
    it."""
    where, nid, entry = line.where, line.name, line.entry
    before, after = line.before, line.after

    # One menu, as the model reads it: the gesture words and the macros
    # it may run, both answered in the same envelope (`_act_legend`).
    # Each comes back under the NAME the model answers with, so
    # `unique_list` catches a repeat by that name and prints it rather
    # than a macro body; the bodies ride beside, for the grant guard.
    by_name: dict[str, Macro] = {}

    def _one_tool(t: Any) -> str:
        if isinstance(t, str) and t in AGENT_TOOLS:
            return t
        macro = grant(scope, t, f"{where}: `tools` entry", nid)
        by_name[macro.name] = macro
        return macro.name

    named = unique_list(entry.get("tools", []), f"{where}: `tools`", _one_tool)
    tools = tuple(n for n in named if n not in by_name)
    granted = tuple(by_name.values())
    macros = tuple(by_name)
    # `tools:` is the one thing that says whether this step ACTS. A
    # macro is a hand like any gesture, so a step granted one alone is
    # an episode: framed by pages, and walked down the episode path
    # (`AgentNode.acts`, which the walker reads off the same two).
    acts = bool(named)
    if not acts:
        # Everything below `tools` is about the screen an episode acts
        # on; on a pure-text call it is dead config.
        for key in ("irreversible", "limit"):
            if key in entry:
                raise PlaybookError(
                    f"{where}: `{key}` is for acting episodes — a pure-text "
                    "call has no screen"
                )
    never_tap = _never_tap(entry, where)

    raw_returns = entry.get("returns")
    returns: list[tuple[str, str]] = []
    if raw_returns is not None:
        if not isinstance(raw_returns, dict) or not raw_returns:
            raise PlaybookError(
                f"{where}: `returns` must be a mapping of field → description"
            )
        if len(raw_returns) > MAX_RETURNS:
            raise PlaybookError(
                f"{where}: {len(raw_returns)} return fields > max {MAX_RETURNS}"
            )
        for fname, desc in raw_returns.items():
            field_name(fname, f"{where}: return field")
            if fname in CONTRACT_FIELDS:
                raise PlaybookError(
                    f"{where}: return field {fname!r} is one of the reply "
                    f"contract's own fields ({', '.join(sorted(CONTRACT_FIELDS))})"
                    " — rename it"
                )
            returns.append((fname, prose(desc, f"{where}: `returns.{fname}`")))
    scope.payloads[nid] = tuple(f for f, _ in returns)
    if not acts and not returns:
        raise PlaybookError(
            f"{where}: an agent with neither `tools` nor `returns` can do "
            "nothing — give it hands, fields to fill, or both"
        )
    irreversible = irreversible_class(entry, where)

    enter = verify = ""
    if acts:
        if before is None:
            raise PlaybookError(
                f"{where}: an acting agent needs the page it starts on — "
                "put a page waypoint before it"
            )
        if after is None:
            raise PlaybookError(
                f"{where}: an acting agent must be followed by the page it "
                "finishes on — the landing check is its exit contract"
            )
        if "." in before or "." in after:
            raise PlaybookError(
                f"{where}: an agent episode runs on this pack's own pages — "
                "reserved built-ins cannot frame it"
            )
        enter, verify = before, after

    raw_limit = limit_mapping(entry, where, _AGENT_LIMIT_KEYS)
    max_calls = limit_int(
        raw_limit.get("calls", DEFAULT_AGENT_CALLS),
        f"{where}: `limit.calls`",
        1,
        MAX_AGENT_CALLS,
    )
    max_scrolls = limit_int(
        raw_limit.get("scrolls", min(DEFAULT_AGENT_SCROLLS, max_calls)),
        f"{where}: `limit.scrolls`",
        0,
        MAX_AGENT_CALLS,
    )
    if "scroll" not in tools:
        max_scrolls = 0
    elif max_scrolls == 0:
        raise PlaybookError(
            f"{where}: `scroll` is granted but `limit.scrolls` is 0 — the "
            "first scroll would hand over; raise it or drop the tool"
        )

    g_payloads = (
        scope.payloads_with_total() if irreversible == PAYMENT else scope.payloads
    )
    reads = _parse_context(scope, where, entry, g_payloads)
    # The hands, the fence and the frame, held to each other — every
    # rule of "what this step may press" in one place.
    if reads.landmarks and TOOL_TAP not in tools:
        raise PlaybookError(
            f"{where}: `context.given` names landmark "
            f"{', '.join(reads.landmarks)} to aim a tap at, but without `tap` "
            "this step cannot press one — grant `tap` or drop it"
        )
    for name, spot in reads.landmarks.items():
        # A landmark scoped to a page (`page:` in the manifest) is held
        # to the page this episode opens on, because the grant is
        # decided once with the rest of the context — a scope that
        # cannot hold is the author's mistake, named at load, never a
        # spot that silently fails to appear.
        held_to = scope.pack.landmarks[spot].page
        if held_to is not None and held_to != enter:
            raise PlaybookError(
                f"{where}: `context.given.{name}`: landmark {spot!r} is scoped "
                f"to page {held_to!r}, but this episode opens on {enter!r} — a "
                "grant is decided once, when the step opens"
            )
    # A granted macro presses its own recorded boxes without ever
    # proposing a tap, so `never_tap` has something to guard on a
    # macro-only episode too (`_guard_grants` at parse, and
    # `spec.fence.macro_refusal` on the live screen).
    if never_tap and TOOL_TAP not in tools and not granted:
        raise PlaybookError(
            f"{where}: `never_tap` guards what this episode presses, but it has "
            "neither a `tap` tool nor a granted macro — grant one or drop the "
            "targets"
        )
    _guard_grants(scope, where, never_tap, tuple(reads.landmarks.values()), granted)

    return AgentNode(
        id=nid,
        prompt=reads.prompt,
        tools=tuple(tools),
        landmarks=reads.landmarks,
        never_tap=never_tap,
        returns=tuple(returns),
        enter=enter,
        verify=verify,
        max_calls=max_calls,
        max_scrolls=max_scrolls,
        irreversible=irreversible,
        given=reads.given,
        memory=reads.memory,
        macros=macros,
        think=think_level(entry, where),
        on_fail=on_fail_mode(entry, where),
    )
