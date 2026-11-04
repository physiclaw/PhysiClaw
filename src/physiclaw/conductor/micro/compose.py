"""The request as messages — the system prompt a call's row assembles,
the newest user block (`user_content`), the whole message list the
channel sends (`messages`), and the two forms an episode history keeps:
the settled exchange and the canonical assistant reply.
"""

import json

from physiclaw.conductor.micro import prompts
from physiclaw.conductor.micro.blocks import data_block
from physiclaw.conductor.micro.calltable import SPECS
from physiclaw.conductor.micro.decision import (
    SINCE,
    Content,
    MicroOutcome,
    MicroRequest,
)
from physiclaw.conductor.micro.moves import move_of
from physiclaw.conductor.spec.calls import (
    ACTION,
    ANSWER,
    ARGS,
)
from physiclaw.contract.dto import (
    ContentBlock,
    ImageBlock,
    Message,
    SystemMessage,
    TextBlock,
    UserMessage,
    message_of,
)


def messages(req: MicroRequest) -> list[Message]:
    """The request as messages: the system contract, an episode's prior
    turns replayed verbatim (append-only — the byte-identical-prefix
    contract the provider cache pays), then the newest user block."""
    messages: list[Message] = [SystemMessage(content=system(req))]
    messages.extend(
        message_of(role, content if isinstance(content, str) else list(content))
        for role, content in req.history
    )
    messages.append(UserMessage(content=user_content(req)))
    return messages


def canonical(req: MicroRequest, outcome: MicroOutcome) -> str:
    """A validated outcome re-serialized in the contract's own spelling
    — the assistant turn a history replays. Rebuilt from the outcome
    (never the raw reply), so repair-retry noise can't enter the
    byte-stable prefix. The row says the shape: a move is `reason,
    action, args, confidence` (`move_of` is the inverse of the parse); a
    question is `reason, answer, confidence` with its fields where the
    row keeps them (`payload_key`, or flat)."""
    spec = SPECS[req.call]
    obj: dict = {"reason": outcome.reason}
    if spec.field == ACTION:
        action, args = move_of(outcome)
        obj[ACTION] = action
        obj[ARGS] = args
        obj["confidence"] = round(outcome.confidence, 2)
    else:
        obj[ANSWER] = outcome.out
        obj["confidence"] = round(outcome.confidence, 2)
        if outcome.payload:
            if spec.payload_key:
                obj[spec.payload_key] = outcome.payload
            else:
                obj.update(outcome.payload)
    return json.dumps(obj, ensure_ascii=False)


def settled(req: MicroRequest, outcome: MicroOutcome) -> list[tuple[str, Content]]:
    """The exchange as a history keeps it: the user block exactly as
    sent (the frame by reference), then the canonical reply — the one
    settle both the agent episode and the session thread append."""
    asked = user_content(req)
    return [
        ("user", asked if isinstance(asked, str) else tuple(asked)),
        ("assistant", canonical(req, outcome)),
    ]


def system(req: MicroRequest) -> str:
    # One skeleton owns the prompt's load-bearing order (contract →
    # material note, when the row has one → answer legend); the row
    # supplies only the texts.
    # `format` fills the optional {allowed} placeholder and unescapes a
    # JSON legend's doubled braces; a legend with neither (agent_act —
    # its rows change per turn, so the system prompt stays byte-stable
    # for the prefix cache) passes through unchanged.
    spec = SPECS[req.call]
    if spec.threaded:
        # The session thread's prompt is the same for all its calls —
        # role and contract only; the call's legend rides the user
        # block — so the whole thread is one cached prefix.
        return f"{prompts.THREAD_ROLE} {spec.contract}"
    note = f"{spec.material_note}\n" if spec.material_note else ""
    legend = spec.answer_spec(req, spec.answer_space(req))
    return f"{spec.contract}\n{note}{legend}"


def user_content(req: MicroRequest) -> str | list[ContentBlock]:
    """The newest user block of a request: plain text when no frame
    rides, else the typed blocks — text runs joined, the frame where
    the row places it. The one composer: the agent step settles exactly
    this into the episode history, so the replayed turn is byte for
    byte what was sent."""
    spec = SPECS[req.call]
    parts: list[str | ImageBlock | None] = []
    if spec.threaded and req.material.get(SINCE):
        # The playbook's own doing between two calls — the ledger's
        # delta, one block, in place of the steps' turns.
        parts.append(data_block(prompts.SINCE_HEADER, req.material[SINCE]))
    parts.extend(spec.user_parts(req))
    if req.context:
        # What a step reads beside its brief: the agent's own memory and
        # the spots it may aim at. Facts to judge, under the one stamp.
        parts.append(data_block(prompts.CONTEXT_HEADER, req.context))
    if spec.threaded:
        # What to answer THIS time — the tail, never the system prompt.
        parts.append("This time: " + spec.answer_spec(req, spec.answer_space(req)))
    if not any(isinstance(p, ImageBlock) for p in parts):
        return "\n".join(p for p in parts if isinstance(p, str) and p)
    blocks: list[ContentBlock] = []
    run: list[str] = []
    for part in parts:
        if isinstance(part, ImageBlock):
            if run:
                blocks.append(TextBlock(text="\n".join(run)))
                run = []
            blocks.append(part)
        elif part:
            run.append(part)
    if run:
        blocks.append(TextBlock(text="\n".join(run)))
    return blocks
