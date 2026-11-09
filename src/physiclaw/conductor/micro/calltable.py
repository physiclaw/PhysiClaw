"""The call table — one row per micro-call type: its answer space, its
legend, its outcome mapping, and whether it rides the session thread.
Adding a call type is one row of `SPECS`; the texts are `prompts.py`,
the episode vocabulary `spec.calls`, the shapes `decision.py`.

Every prompt is fixed-shape: the contract (`reason` first, then the
word, then `confidence`), the material note, the legend. A screen
enters as the model's own turns see it — the frame the tool result
carried beside its whole element listing — the listing stamped as data
(`data_block`), never compressed. The rows say what a call is;
`compose.py` turns a request into the messages the channel sends.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from physiclaw.common.bbox import parse_box
from physiclaw.conductor.micro import prompts
from physiclaw.conductor.micro.blocks import data_block, return_fields
from physiclaw.conductor.micro.decision import (
    ACT_ARM,
    AGENT_ACT,
    AGENT_FIELDS,
    ASK,
    BLOCK,
    FIELDS,
    LEAD,
    MENU,
    NOT_A_TASK,
    PARSE_TASK,
    PROMPT,
    READ_REPLY,
    REPLY_CONFIRM,
    REPLY_DENY,
    REPLY_OTHER,
    SCROLL_UP,
    SUMMARIZE,
    SUMMARY_DONE,
    Macro,
    MicroOutcome,
    MicroRequest,
    Tap,
)
from physiclaw.conductor.micro.moves import reply_args
from physiclaw.conductor.micro.prompts import (
    TEXT_CALL_LEGEND,
    TOOLS_HEADER,
    legend_line,
)
from physiclaw.conductor.spec.calls import (
    ACT_BACK,
    ACTION,
    AGENT_DONE,
    AGENT_TOOLS,
    ANSWER,
    ARGS,
    AT,
    CONTRACT_FIELDS,
    DIRECTION,
    ESCALATE,
    LABEL,
    NAME,
    SCROLL_ARMS,
    TOOL_BACK,
    TOOL_RUN,
    TOOL_SCROLL,
    TOOL_TAP,
)
from physiclaw.contract.dto import (
    ImageBlock,
)


def has_fallback(call: str) -> bool:
    """Whether a caller may resolve this call with no outcome at all and
    still get the outcome the call was FOR. True only of the close,
    which writes the walk's own recap. Deliberately not `threaded`:
    that flag shapes the prompt, and reading a walk-control question off
    it made a replay of the boot report success — `parse_task` resolved
    with None concludes "no playbook covers the thread", which is an
    answer the replay never asked for."""
    return SPECS[call].walk_fallback


# The output contract, field by field IN ORDER — the order is
# load-bearing: the model generates left to right, so `reason` first is
# chain-of-thought baked into the schema (answer-first demotes the
# reasoning to post-hoc rationalization), the word (`answer` for a
# question, `action` for a move) commits after the reasoning, an
# action's `label` and `at` say what and where, and `confidence`
# judges the committed whole. The reason is asked as the deciding
# fact, one sentence: an open "weigh the evidence" line is where a
# model narrates its whole working — the pick's reasons ran to
# paragraphs, tripling the output for the same answers — and a model
# that thinks first has already done that working out of sight.
_REASON = (
    '"reason": "<ONE sentence, at most 25 words: the deciding fact, not your working>"'
)

_CONFIDENCE = '"confidence": <0.0-1.0, your honest probability that {word} is right>'


def _contract(field: str) -> str:
    """The reply contract for one call: reason, then the word in
    `field` — a question's `answer`, or a move's `action` with its
    `args` (one envelope for every tool) — then confidence, in this
    order."""
    parts = [_REASON]
    if field == ACTION:
        parts.append(f'"{ACTION}": "<a tool name from the list below>"')
        parts.append(f'"{ARGS}": {{<that tool\'s arguments, exactly as listed>}}')
    else:
        parts.append(f'"{field}": "<see below>"')
    parts.append(_CONFIDENCE.format(word=field))
    return (
        "Reply with ONLY this JSON object — no code fence, no other text, "
        "these fields in this order:\n{" + ", ".join(parts) + "}"
    )


@dataclass(frozen=True)
class CallSpec:
    """One call type's whole shape — the table dispatch. `answer_spec` is a
    template (an optional `{allowed}` placeholder); the callables own
    answer space, prompt body, and outcome mapping. Adding a call type
    is one row here."""

    # The field the call's word rides in and the contract asking for it.
    field: str
    contract: str
    answer_space: "Callable[[MicroRequest], tuple[str, ...]]"
    # The legend: a `_template(text)` (an optional {allowed} placeholder)
    # or a builder — the episode's is built from the tools its request
    # declares.
    answer_spec: "Callable[[MicroRequest, tuple[str, ...]], str]"
    # The user block's parts in order: text, and the frame where it
    # sits (None when the request carries none — the part is skipped).
    user_parts: "Callable[[MicroRequest], list[str | ImageBlock | None]]"
    to_outcome: "Callable[[MicroRequest, str, str, float, dict], MicroOutcome]"
    # What the call's data block is made of, said once in the system
    # prompt when the shape needs saying (an episode's OCR rows).
    material_note: str = ""
    # A thread call (`thread.py`): the session's fixed system prompt
    # instead of the row's, the legend at the tail of the user block,
    # the ledger's delta at its head.
    threaded: bool = False
    # The walk can resolve this call with no outcome and still reach the
    # outcome the call was for (`has_fallback`) — a separate fact from
    # `threaded`, which only shapes the prompt.
    walk_fallback: bool = False
    # Where a thread call's payload rides in its canonical reply: under
    # this key (parse_task's `inputs`), or flat when None.
    payload_key: str | None = None


def _parse_task_space(req: MicroRequest) -> tuple[str, ...]:
    # The escapes are the ROW's own — a caller (activation)
    # passes only the playbook refs and can never forget the exits.
    return req.outcomes + (SCROLL_UP, NOT_A_TASK)


def _fixed(outcomes: tuple[str, ...]) -> "Callable[[MicroRequest], tuple[str, ...]]":
    """An answer space fixed whole in the row — nobody's to vary, and
    callers pass empty outcomes."""
    return lambda req: outcomes


def _template(text: str) -> "Callable[[MicroRequest, tuple[str, ...]], str]":
    """A static legend; `{allowed}` (when present) fills with the answer
    space, and a JSON legend's doubled braces unescape."""
    return lambda req, allowed: text.format(allowed=", ".join(allowed))


# What a model writes when it means "the message didn't say". Asked for
# an object over the DECLARED inputs, models emit a key for every one of
# them and fill the unmentioned with a null spelling rather than omitting
# it. Those must not reach `resolve_inputs`, which resolves on PRESENCE:
# a present `"null"` shadows the declared default, so `criteria` becomes
# the literal string "null" in the picking decision. Empty is included
# for the same
# reason — an input filled with "" was not filled.
_UNFILLED = frozenset({"", "null", "none", "nil", "n/a", "undefined"})


def _is_unfilled(value: object) -> bool:
    """JSON `null` arrives as None; everything else is a spelling of it."""
    if value is None:
        return True
    return isinstance(value, str) and value.strip().casefold() in _UNFILLED


def _string_fields(mapping: dict) -> dict[str, str]:
    """Payload values are strings by contract. A LIST is the one
    structured value with a text shape of its own — one item per line,
    which is what a field asked for "one per line" already holds, what
    a message renders, and what a repeated run iterates — so a list of
    scalars rides as lines; any other structure rides as ITS JSON
    (str() would produce a Python repr no parser accepts). Unfilled
    spellings are dropped (a present "null" would shadow a declared
    default). ONE home: parse_task's inputs and the agent calls' return
    fields share the rule."""
    return {str(k): _text_value(v) for k, v in mapping.items() if not _is_unfilled(v)}


def _text_value(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v):
        return "\n".join(str(x).strip() for x in v if str(x).strip())
    return json.dumps(v, ensure_ascii=False)


def _answer_outcome(
    req: MicroRequest, answer: str, reason: str, confidence: float, obj: dict
) -> MicroOutcome:
    """A question's outcome: the word, and the fields beside it where
    the row keeps them — under `payload_key` (parse_task's `inputs`) or
    flat (a summary's recap and memory) — as strings."""
    key = SPECS[req.call].payload_key
    if key is not None:
        raw = obj.get(key)
        fields = _string_fields(raw) if isinstance(raw, dict) else {}
    else:
        fields = _string_fields(
            {k: v for k, v in obj.items() if str(k) not in CONTRACT_FIELDS}
        )
    return MicroOutcome(
        out=answer, reason=reason, confidence=confidence, payload=fields
    )


def _parse_task_outcome(
    req: MicroRequest, answer: str, reason: str, confidence: float, obj: dict
) -> MicroOutcome:
    """parse_task: the inputs ride only with a playbook. An escape
    carries none — a `scroll_up` or `not_a_task` answer has read only
    part of the thread, so anything it extracted is a guess, and
    dropping it keeps it out of the settled history too."""
    outcome = _answer_outcome(req, answer, reason, confidence, obj)
    if answer in (NOT_A_TASK, SCROLL_UP):
        return replace(outcome, payload=None)
    return outcome


def _agent_done_outcome(
    req: MicroRequest, action: str, reason: str, confidence: float, obj: dict
) -> MicroOutcome:
    """done / escalate: done's args are the return fields (the program
    validates them against the node's DECLARED fields; a missing one
    escalates there, never guesses here)."""
    payload = _string_fields(reply_args(obj)) if action == AGENT_DONE else None
    return MicroOutcome(
        out=action, reason=reason, confidence=confidence, payload=payload
    )


def _act_outcome(
    req: MicroRequest, action: str, reason: str, confidence: float, obj: dict
) -> MicroOutcome:
    """A validated tool call to the walk's arm: tap and run_macro
    ground to a pick (`ACT_ARM`), scroll and back to their swipe arms,
    done and escalate to themselves. The parser already proved the
    args, so nothing here can miss."""
    args = reply_args(obj)
    picked: Tap | Macro | None = None
    if action == TOOL_TAP:
        picked = Tap(label=str(args[LABEL]).strip(), bbox=parse_box(args[AT]))
    elif action == TOOL_RUN:
        picked = Macro(name=str(args[NAME]))
    if picked is not None:
        return MicroOutcome(
            out=ACT_ARM, reason=reason, confidence=confidence, picked=picked
        )
    if action == TOOL_SCROLL:
        return MicroOutcome(
            out=SCROLL_ARMS[args[DIRECTION]], reason=reason, confidence=confidence
        )
    if action == TOOL_BACK:
        return MicroOutcome(out=ACT_BACK, reason=reason, confidence=confidence)
    return _agent_done_outcome(req, action, reason, confidence, obj)


def macro_key(name: str) -> str:
    """How a granted macro reads in an answer space and a recorded
    call's `allowed`: kind-tagged, so a replay can judge a run's `name`
    without the request (and no screen word can spell it)."""
    return f"macro:{name}"


def _act_space(req: MicroRequest) -> tuple[str, ...]:
    """The episode's answer space: the tools the request declares (the
    granted ones, the macro run, the two exits) and the granted macro
    names, kind-tagged. Landmarks are tapped by box like anything else,
    so their names are not answers. Never a word off the screen."""
    return req.outcomes + tuple(macro_key(n) for n in req.macros)


def _legend(lines: list[str]) -> str:
    """The tool menu: the header, then one line per tool."""
    return TOOLS_HEADER + "\n" + "\n".join(f"- {line}" for line in lines)


def _fields_legend(req: MicroRequest, allowed: tuple[str, ...]) -> str:
    """The pure-text call's tool list: done and escalate, the same
    envelope as an episode's (escalate worded for a call with no
    screen, `TEXT_CALL_LEGEND`)."""
    return _legend([legend_line(t, TEXT_CALL_LEGEND) for t in (AGENT_DONE, ESCALATE)])


def _act_legend(req: MicroRequest, allowed: tuple[str, ...]) -> str:
    """The episode's tool list — one line per tool, the same shape for
    each (name, its args, what they take), read off what the request
    itself declares: the tools in `outcomes`, the macros in `macros`.
    Both are fixed for the episode, so the system prompt
    stays byte-stable and the provider prefix cache pays. The rows
    themselves live in each turn's user block, never here."""
    lines = [legend_line(t) for t in AGENT_TOOLS if t in req.outcomes]
    if req.macros:
        lines.append(legend_line(TOOL_RUN, macros=", ".join(req.macros)))
    lines.append(legend_line(AGENT_DONE))
    lines.append(legend_line(ESCALATE))
    return _legend(lines)


SPECS: dict[str, CallSpec] = {
    PARSE_TASK: CallSpec(
        field=ANSWER,
        contract=_contract(ANSWER),
        answer_space=_parse_task_space,
        answer_spec=_template(prompts.PARSE_TASK_LEGEND),
        # The thread as the screenshot (who said what sits in the
        # bubbles' sides) and its text read off the screen.
        user_parts=lambda req: [
            req.material.get(MENU, ""),
            req.frame,
            data_block("The user's message thread", req.listing),
        ],
        to_outcome=_parse_task_outcome,
        threaded=True,
        payload_key="inputs",
    ),
    READ_REPLY: CallSpec(
        field=ANSWER,
        contract=_contract(ANSWER),
        answer_space=_fixed((REPLY_CONFIRM, REPLY_DENY, REPLY_OTHER)),
        answer_spec=_template(prompts.READ_REPLY_LEGEND),
        # The ask as it was put, then the thread: its screenshot and the
        # user's messages since the ask, newest last.
        user_parts=lambda req: [
            data_block("The ask, as sent to the user", req.material.get(ASK, "")),
            req.frame,
            data_block("The user's replies since, oldest first", req.listing),
        ],
        to_outcome=_answer_outcome,
        threaded=True,
    ),
    SUMMARIZE: CallSpec(
        field=ANSWER,
        contract=_contract(ANSWER),
        answer_space=_fixed((SUMMARY_DONE,)),
        answer_spec=_template(prompts.SUMMARIZE_LEGEND),
        # No material of its own: the thread's history and its
        # since-block already carry the whole walk.
        user_parts=lambda req: [],
        to_outcome=_answer_outcome,
        threaded=True,
        walk_fallback=True,
    ),
    # The two agent rows: the author's prompt is the APP brief with its
    # givens filled once (the first user block — replayed verbatim in an
    # episode), the conductor adds the output contract, the answers the
    # author's tools grant, the step's memory parts as data
    # (`user_content` appends it), and — for an episode — what its
    # screen block is made of. The brief runs prompt → contract → data.
    AGENT_FIELDS: CallSpec(
        field=ACTION,
        contract=_contract(ACTION),
        answer_space=_fixed((AGENT_DONE, ESCALATE)),
        answer_spec=_fields_legend,
        user_parts=lambda req: [
            req.material.get(PROMPT, ""),
            *(
                [return_fields(req.material[FIELDS])]
                if req.material.get(FIELDS)
                else []
            ),
        ],
        to_outcome=_agent_done_outcome,
    ),
    AGENT_ACT: CallSpec(
        # Episode requests come from the agent step
        # (`steps.agent.AgentStep._request`): only it has the replayed
        # history and the granted macros.
        field=ACTION,
        contract=_contract(ACTION),
        material_note=prompts.SCREEN_ROWS_NOTE,
        answer_space=_act_space,
        answer_spec=_act_legend,
        # What happened (or the brief), the screen as the model would
        # see it on its own turn — the frame, then the listing — and
        # what the episode may name beside the rows.
        user_parts=lambda req: [
            req.material.get(LEAD, ""),
            req.frame,
            req.material.get(BLOCK, ""),
        ],
        to_outcome=_act_outcome,
    ),
}
