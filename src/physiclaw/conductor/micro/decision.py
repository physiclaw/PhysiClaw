"""The shapes of a micro-call — what the walk asks and what it gets
back, and the vocabulary both sides spell the same way: the call names
(`parse_task`, `read_reply`, `summarize`, `agent_fields`, `agent_act`),
the material keys a builder fills and a `calltable` row reads, the
`MicroRequest` the walk hands the channel, and the `MicroOutcome`
the channel hands back. Nothing here talks to a provider.
"""

from dataclasses import dataclass

from physiclaw.common.bbox import Bbox
from physiclaw.common.listing import Element
from physiclaw.conductor.spec.calls import (
    ACT_SCROLL_UP,
)
from physiclaw.conductor.spec.reply import Answer
from physiclaw.contract.dto import (
    ContentBlock,
    ImageBlock,
    Thinking,
)

# Conductor-internal call names — a playbook never names them.


PARSE_TASK = "parse_task"

NOT_A_TASK = "not_a_task"

# The session thread's other two calls (`thread.py`): a reply the ask's
# declared words could not read, and the closing record.
READ_REPLY = "read_reply"

# The two verdicts the declared yes/no words also produce — spelled by
# `reply.Answer` so the model's word and the words' word cannot drift.
REPLY_CONFIRM = Answer.CONFIRM.value

REPLY_DENY = Answer.DENY.value

REPLY_OTHER = "other"

SUMMARIZE = "summarize"

SUMMARY_DONE = "done"

# parse_task's second escape: the newest message is a nudge whose
# request sits ABOVE the visible thread — the boot's select step
# scrolls up (bounded) and re-asks over the accumulated listing. The
# episode's scroll verb, one spelling.
SCROLL_UP = ACT_SCROLL_UP

# The playbook `agent` step's two calls. `agent_fields` is the
# pure-text form: the authored prompt in, the declared return fields
# out. `agent_act` is one EPISODE turn: the model sees the screen as
# the frame plus its element listing and answers a TOOL CALL the way
# its own turns would — one envelope, `action` + `args`, for every
# tool: `tap {label, at}` (what the box is; the box
# `[left, top, right, bottom]`, copied off the listing or a granted
# landmark or read off the screenshot), `scroll {direction}`, `back
# {}`, `run_macro {name}`, `done {return fields}`, `escalate {}`. A tap
# fires at the box the model sent and is journaled by the label it
# gave — nothing is matched or renamed under the hood. Episode
# context rides
# `MicroRequest.history`,
# append-only and uncompressed (every earlier frame and listing stays),
# so every call's prefix is byte-identical to the previous call's whole
# request (the provider prefix cache pays for all but the newest
# block). The verbs and `done` are `calls.py`'s episode vocabulary.
AGENT_FIELDS = "agent_fields"

AGENT_ACT = "agent_act"

ACT_ARM = "act"  # the routing arm a tap or a macro run maps to

# The material keys a call's builder fills and its `SPECS` row reads —
# one spelling, so a typo cannot yield a silently empty part.
LEAD = "lead"  # an episode turn's opening line: the brief, or what just happened

BLOCK = "block"  # an episode turn's element listing

PROMPT = "prompt"  # the pure-text call's authored prompt

FIELDS = "fields"  # the declared return fields, rendered

MENU = "menu"  # parse_task's playbook menu

SINCE = "since"  # a thread call's ledger delta (`Thread.request` fills it)

ASK = "ask"  # read_reply's question, as it was put to the user


@dataclass(frozen=True)
class Tap:
    """A tap exactly as the model spoke it: its label (what the box is)
    and its box — nothing matched or renamed."""

    label: str
    bbox: Bbox


@dataclass(frozen=True)
class Macro:
    """A granted pack macro the model chose to run, by name."""

    name: str


# What one message of a request holds: text, or the typed blocks of a
# screen (its frame beside its listing). A settled episode turn keeps
# the tuple form so the request stays hashable and frozen.
Content = str | tuple[ContentBlock, ...]


@dataclass(frozen=True)
class MicroRequest:
    """Everything one micro-call needs — the node scalars it reads (never
    the whole node: tooling builds requests without minting fake nodes)
    plus the material assembled from the screen at call time."""

    call: str  # key into SPECS
    node_id: str  # for logs/trace
    outcomes: tuple[str, ...]  # the caller's arms (an episode's tools)
    material: dict[str, str]  # the call's texts, keyed by LEAD / BLOCK / PROMPT …
    macros: tuple[str, ...] = ()  # an episode's granted macros, by name
    # The elements the episode's screen block lists (the trace records
    # how many the decision saw).
    elements: tuple[Element, ...] = ()
    listing: str = ""  # label text of the screen (listing-material calls)
    context: str = ""  # assembled context, "" when none
    # The frame the screen's tool result carried — sent beside the
    # listing so the model sees the screen itself; None when the read
    # had no image (a text-only result, a replayed screen).
    frame: ImageBlock | None = None
    # An agent episode's prior turns, append-only: ("user"|"assistant",
    # content) pairs replayed VERBATIM before the newest user block, so
    # each call's prefix is byte-identical to the previous call's whole
    # request. A thread call carries the session's turns the same way
    # (`thread.py`); only a lone one-shot call has none.
    history: tuple[tuple[str, Content], ...] = ()
    # The step's `think:` — how much hidden thinking the call asks the
    # model for; None leaves the vendor's default.
    thinking: Thinking | None = None


@dataclass(frozen=True)
class MicroOutcome:
    """A validated, confident answer. `out` is the answer's arm (a verb,
    an escape, or `ACT_ARM` for a tap or a macro run — `picked` then
    says which)."""

    out: str
    reason: str
    confidence: float
    picked: Tap | Macro | None = None
    # The fields riding beside the answer: parse_task's extracted
    # playbook inputs, an agent call's return fields, a summary's recap
    # and memory line. Empty when the reply carried none; None only
    # where a row suppresses them (a parse escape).
    payload: dict[str, str] | None = None


@dataclass(frozen=True)
class MicroResult:
    """One call's full account: the outcome (None = escalate) plus the
    stats the trace event records."""

    outcome: MicroOutcome | None
    detail: str  # the outcome's reason, or why there is none
    attempts: int
    elapsed_ms: int
