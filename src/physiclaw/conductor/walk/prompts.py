"""The conductor's prompt texts — what its three micro-calls say.

Text only: the mechanism (`micro.py`) assembles the skeleton — role
sentence → reply contract → legend — and the rows there name these
(the tool legend itself lives beside the tools, `calls.TOOL_LEGEND`).
Kept apart so a prompt can be read and edited as prose, and the
agent rows stay what they are: the author's prompt is the whole brief
and the conductor adds only the output contract and, for an episode,
what its own screen block is made of (`SCREEN_ROWS_NOTE`).
"""

from physiclaw.common.listing import LISTING_HEADER

# parse_task — the boot's one question over the user's thread.
PARSE_TASK_ROLE = (
    "You read an instant-message thread — its screenshot, when one is "
    "attached, and its text read off the screen, oldest line first and "
    "newest last — and decide whether the user has a request still "
    "OUTSTANDING that one of the available playbooks performs."
)
PARSE_TASK_LEGEND = (
    '"answer" is one playbook EXACTLY as listed; or "not_a_task" '
    "for greetings, chat, questions, or anything no playbook "
    'covers (when unsure, "not_a_task"); or "scroll_up" when the '
    "newest message refers to an earlier request that is NOT "
    "visible in this thread — older messages sit above the fold "
    "and must be read before deciding. "
    # A wake is usually the user's SECOND prod, not their first:
    # the run they asked for was cut short, so the newest line is
    # a nudge and the request it refers to sits above it. Reading
    # only the newest line answered `not_a_task` to a user who
    # was asking for exactly the playbook on the menu.
    "WHICH request: normally the user's newest one — but when "
    "their newest message is only a nudge to carry on (a bare "
    '"go on" / "继续" / "any update?"), it refers to their most '
    "recent request above it that is still outstanding. "
    # The completion reply is what makes this safe to widen: the
    # assistant reports every finished task into this same
    # thread, so "already done" is visible rather than inferred.
    "A request is FINISHED — never answer it with a playbook — "
    "once the assistant has reported it done or the user "
    "cancelled it; only a request with no such reply after it is "
    "still outstanding. Judge that from THIS thread: the report or "
    "cancellation must sit after the request here. The Context "
    "block is background only — it records earlier, separate "
    "tasks (even for the same product on another day) and never "
    "finishes a request this thread shows unanswered. Re-running "
    "a finished task can spend money twice, so on any doubt about "
    'which it is, answer "not_a_task". '
    "When you answer with a "
    'playbook, ALSO add a fourth field "inputs": an object filling '
    "that playbook's declared inputs from the words of THAT "
    "request. "
    "OMIT any input the message does not specify — leave the key "
    "out entirely rather than filling it with null or an empty "
    "string; each omitted input falls back to its own default. "
    "Each value contains ONLY what that input's description asks "
    "for — a search-keyword input takes the bare product/search "
    "term (follow its e.g. example when shown), never quantity or "
    "count words; those belong only in an input that asks for "
    "them."
)

# agent_act — what the screen block IS, said once in the system prompt:
# the screenshot the phone showed and the element listing read off it,
# the same pair the model's own turns see. Text rows are OCR boxes: a
# title breaks across rows, its price sits on the row below, a bundle's
# ×4 on another row than its brand; a model that reads rows as whole
# items taps a continuation row or a bundle it never saw. Icon rows
# carry no label — the picture says what they are. Written for a model
# that answers: with hidden thinking on, the sentence is one more thing
# to ruminate about.
SCREEN_ROWS_NOTE = (
    "Each screen block is the phone's screenshot (when one is attached; "
    "icon boxes are drawn on it) followed by its element listing, one row "
    "per detected element, top "
    f"to bottom: `{LISTING_HEADER}`. A "
    "[text] row is one OCR box: one on-screen item (a listing, a card, a "
    "message) usually spans several consecutive rows, a title may be cut "
    "at the row edge and continue on the next, and a price or sales row "
    "belongs to the title rows just above it. An [icon] row has no label; "
    "read what it is from the screenshot at its box (icons are drawn there "
    "with their id). Boxes are fractions of the screenshot's width and "
    "height. Use the screenshot to see layout and what the rows belong "
    "to; never reconstruct beyond what the screen shows."
)
RETURN_FIELDS_HEADER = (
    'Return fields (the keys of "args" when "action" is "done", each a plain string):'
)
