"""Shared builders for the conductor test files (the `engine_fakes`
pattern: sibling module, imported bare thanks to pytest's rootdir path)."""

from __future__ import annotations

import base64
from textwrap import indent

from physiclaw.common.listing import Element, Screen, format_elements
from physiclaw.conductor.load.pack import load_spec
from physiclaw.conductor.micro.decision import DecisionRequest
from physiclaw.conductor.spec.model import resolve_inputs
from physiclaw.contract.dto import (
    AssistantMessage,
    FinishReason,
    ImageBlock,
    TextBlock,
    Usage,
)

# One bbox convention for every fake row: ±0.05 × ±0.02 around the center.
BOX_W, BOX_H = 0.05, 0.02

# A frame as a tool result carries it — the bytes are opaque to every
# reader under test (nothing decodes them), so any bytes will do.
FRAME = ImageBlock(
    media_type="image/jpeg", data_b64=base64.b64encode(b"fake jpeg").decode()
)


def agent_reply(action: str, confidence: float = 0.9, **args) -> str:
    """An agent reply in the tool-call envelope: `action` + its `args`."""
    import json

    return json.dumps(
        {"reason": "r", "action": action, "args": args, "confidence": confidence},
        ensure_ascii=False,
    )


def make_screen(*rows: tuple) -> Screen:
    """Rows are (label, cx, cy), (label, cx, cy, conf) or (label, cx, cy,
    conf, half_w) — a row as wide as a real line, when its width
    matters; an empty label is an icon (the listing grammar's
    label-less kind)."""
    els = []
    for i, row in enumerate(rows):
        label, cx, cy = row[0], row[1], row[2]
        conf = row[3] if len(row) > 3 else 0.9
        half_w = row[4] if len(row) > 4 else BOX_W
        els.append(
            Element(
                id=i,
                kind="text" if label else "icon",
                label=label,
                bbox=(cx - half_w, cy - BOX_H, cx + half_w, cy + BOX_H),
                conf=conf,
            )
        )
    return Screen.read(format_elements(els))


# The three screens every driver test needs, and the loop contract for
# feeding a synthesized turn's result back. One home: the conductor's own
# `turns.py` centralized this on the src side, so the tests must not
# re-spell "the result is keyed to tool_calls[1]" per file.
ELSEWHERE = make_screen(("Nothing known", 0.5, 0.5)).text
# The demo pack's three pages as screens, and the two-move walk over
# them — the fixtures the walk-driving test files share.
HOME = make_screen(("Files", 0.5, 0.1)).text
RESULTS = make_screen(("综合", 0.5, 0.1)).text
DONE = make_screen(("AllDone", 0.5, 0.1)).text

FLOW = """\
description: two moves
inputs:
  keyword:
    description: what to search
route:
  - page: app.pages.home
  - do: open
    macro: app.macros.open-app
    with: {message: "{inputs.keyword}"}
  - page: app.pages.results
  - do: search
    macro: app.macros.add-cart
    with: {message: "go"}
  - page: app.pages.done
"""


def thread_screen(*bubbles: tuple) -> str:
    """The user-channel thread, anchored on the demo contact name."""
    return make_screen(("MyChat", 0.5, 0.05), *bubbles).text


def history() -> list:
    from physiclaw.contract.dto import SystemMessage, UserMessage

    return [SystemMessage(content="sys"), UserMessage(content="wake")]


def feed(
    history: list,
    turn,
    text: str = "",
    *,
    error: bool = False,
    frame: ImageBlock | None = None,
) -> None:
    """Append the synthesized turn plus its ACTION's tool result — the
    loop's contract (one result per call, in the very next messages).
    With `frame`, the result is the fused view a real read returns:
    text beside the frame."""
    from physiclaw.contract.dto import ToolResultMessage

    history.append(turn)
    content = text if frame is None else [TextBlock(text=text), frame]
    history.append(
        ToolResultMessage(
            tool_call_id=turn.tool_calls[1].id, content=content, is_error=error
        )
    )


def suspend_via_silence(driver, history: list, send, thread: str):
    """Drive an ask through its silent rounds to the suspension: the
    send lands on `thread`, every poll reads the same thread; returns
    the walk's end_session turn."""
    from physiclaw.conductor.spec import limits

    feed(history, send, thread)
    step = driver.advance(history)
    for _ in range(limits.DEFAULT_ASK_ROUNDS):
        feed(history, step, "waited")
        feed(history, driver.advance(history), thread)
        step = driver.advance(history)
    assert step.tool_calls[1].arguments["status"] == "WAIT"
    return step


def finish(driver, history: list, step) -> str:
    """The terminal contract: a handover mints ONE final synthesized
    [note, peek] brief turn; a completion or a stop ONE synthesized
    [note, end_session]. Feed the action's result and the driver is
    permanently quiet. Returns the note's summary so tests can assert
    on the report itself."""
    assert step is not None, "expected the terminal turn, got quiet"
    if isinstance(step, DecisionRequest):
        # A completion asks the session thread for its record first; with
        # nobody to answer, the walk's own recap closes it.
        step = driver.resolve(None)
    assert step.synthesized and step.tool_names() in (
        ["note", "peek"],
        ["note", "end_session"],
    )
    feed(history, step, ELSEWHERE if step.tool_names()[1] == "peek" else "ended")
    assert driver.advance(history) is None
    return step.tool_calls[0].arguments["summary"]


# The `run` pair the run tests walk: a leg playbook of the demo pack,
# and an entry that runs it once per listed item (`each:`).

LEG = """\
kind: playbook
schema: 1
name: leg
description: one leg — open, then search
inputs:
  what:
    description: what to search
returns:
  did: "searched {inputs.what}"
route:
  - start: app
    macro: app.macros.open-app
  - page: app.pages.home
  - do: search
    macro: app.macros.add-cart
    with: {message: "{inputs.what}"}
  - page: app.pages.results
"""

EACH = """\
kind: entry
schema: 1
name: flow
description: one leg per item
inputs:
  keyword:
    description: what
route:
  - agent: parse
    context:
      prompt: |
        List the items for {keyword}.
      given: {keyword: "{inputs.keyword}"}
    returns:
      items: the items, one per line
  - run: leg
    each: {what: parse.items}
    limit: {rounds: 2}
  - page: app.pages.results
  - tell: report
    message: "done: {leg.did}"
"""


# One canonical demo pack for the pack-consuming test files (playbook,
# program): two declared pages, two enabled macros.

PAGES = """\
home:
  description: the home page
  anchors: ["Files"]
results:
  description: the results page
  anchors: ["综合"]
done:
  description: the done page
  anchors: ["AllDone"]
"""

PACK_MACRO = """\
kind: macro
schema: 1
name: {name}
description: test leg
inputs:
  message:
    description: text to use
    default: hi
steps:
  - tap: t
    at: [0.1, 0.1, 0.2, 0.2]
"""


def compose_pack_doc(
    app: str,
    pages: str,
    landmarks: str | None = None,
) -> str:
    """The manifest (`APP.yml`) from the fixtures' pieces: meta, a
    `pages:` appendix, optional landmarks. Playbooks are files beside
    it (`write_pack` writes them), never manifest content."""
    doc = (
        f"kind: manifest\nschema: 1\napp: {app}\ndescription: test pack\n"
        f"pages:\n{indent(pages, '  ')}\n"
    )
    if landmarks:
        doc += f"landmarks:\n{indent(landmarks, '  ')}\n"
    return doc


def write_pack(
    app: str = "demo",
    *,
    macros: tuple[str, ...] = ("open-app", "add-cart"),
    playbooks: dict[str, str] | None = None,
    pages: str = PAGES,
    landmarks: str | None = None,
):
    """Write a pack under the (fixture-scoped) playbooks dir — the
    manifest, one file per playbook keyed by its id (an entry's
    `<name>/PLAYBOOK.yml`, or `<entry>/<name>.yml` for one it runs),
    the pack macros; returns its root."""
    from physiclaw.common import paths

    root = paths.playbooks_dir() / app
    (root / "macros").mkdir(parents=True, exist_ok=True)
    (root / "APP.yml").write_text(
        compose_pack_doc(app, pages, landmarks), encoding="utf-8"
    )
    for name, text in (playbooks or {}).items():
        write_playbook(root, name, text)
    for m in macros:
        (root / "macros" / f"{m}.yml").write_text(
            PACK_MACRO.format(name=m), encoding="utf-8"
        )
    return root


def write_playbook(root, name: str, text: str):
    """One playbook under a pack root — an entry's `<name>/PLAYBOOK.yml`,
    or `<entry>/<name>.yml` for a dotted `<entry>.<name>`. A playbook
    says what it is and names itself; a fixture that writes only the
    body gets both header lines its position implies. Returns the
    entry's folder."""
    from physiclaw.common import paths
    from physiclaw.common.text import write_text

    if not text.startswith("kind:"):
        text = (
            f"kind: {paths.kind_of(name)}\nschema: {paths.PACK_SCHEMA}\n"
            f"name: {paths.own_name(name)}\n{text}"
        )
    path = root / paths.playbook_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, text)
    return path.parent


def write_leaf(root, playbook: str | None, leaf: str, name: str, text: str):
    """One leaf file under a pack root — `<root>/[<playbook>/]<leaf>/<name>` —
    the layout's two leaf folders (`macros`, `prompts`) at either level.
    `name` carries its suffix. Returns the path."""
    from physiclaw.common.text import write_text

    folder = (root / playbook if playbook else root) / leaf
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    write_text(path, text)
    return path


def write_prompt(root, playbook: str | None, name: str, text: str):
    return write_leaf(root, playbook, "prompts", f"{name}.md", text)


def write_local_macro(root, playbook: str, name: str, text: str | None = None):
    return write_leaf(
        root,
        playbook,
        "macros",
        f"{name}.yml",
        PACK_MACRO.format(name=name) if text is None else text,
    )


def build_program(
    app: str = "demo", name: str = "flow", *, dry: bool = False, **values
):
    """Build the walk the way `playbooks run` does — the factory that
    replaced arming as the way to get a Program without a wake. `dry`
    builds the replay's no-trace walk."""
    from physiclaw.conductor.drive import build
    from physiclaw.conductor.load import channel

    spec, pack = load_spec(app, name)
    return build.build_program(
        spec,
        pack,
        resolve_inputs(spec, values),
        channel.load_channel(),
        dry=dry,
    )


# The user-channel pack the gate tests send over: one thread page and
# a send macro; `write_channel` lays it down (plus an `open` macro when
# the test's walk needs the resume/boot hand).
CHANNEL_PAGES = """\
thread:
  description: the user's chat thread
  anchors: ["MyChat"]
"""

CHANNEL_OPEN = """\
kind: macro
schema: 1
name: open
description: open the thread
steps:
  - tap: t
    at: [0.1, 0.1, 0.2, 0.2]
"""

CHANNEL_SEND = """\
kind: macro
schema: 1
name: send
description: send to the user
inputs:
  message:
    description: text
steps:
  - send_to_clipboard: "{message}"
"""


def channel_root(im: str = "wechat"):
    """Where a test channel pack lives: an IM folder under `channel/`
    (no ACTIVE.txt needed with a single folder)."""
    from physiclaw.common import paths

    return paths.playbooks_dir() / "channel" / im


def write_active(word: str) -> None:
    """`channel/ACTIVE.txt` — the IM folder in use."""
    from physiclaw.common import paths

    (paths.playbooks_dir() / "channel" / "ACTIVE.txt").write_text(
        word + "\n", encoding="utf-8"
    )


def write_channel(open_macro: str | None = None, im: str = "wechat") -> None:
    root = channel_root(im)
    (root / "macros").mkdir(parents=True, exist_ok=True)
    (root / "boot").mkdir(exist_ok=True)  # a test may write its own boot
    (root / "APP.yml").write_text(
        compose_pack_doc(im, CHANNEL_PAGES),
        encoding="utf-8",
    )
    (root / "macros" / "send.yml").write_text(CHANNEL_SEND, encoding="utf-8")
    if open_macro is not None:
        (root / "macros" / "open.yml").write_text(open_macro, encoding="utf-8")


class Sink:
    """An `EventSink` that keeps what it is given — the session event
    stream a test reads back (the trace, a micro-caller, a record)."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def write(self, event: dict) -> None:
        self.events.append(event)


def make_learned(text: str, cx: float, cy: float, *, pos_tol=0.02, variants=()):
    """One captured anchor — the shape `pages.LearnedAnchor` has today."""
    from physiclaw.conductor.spec.pages import LearnedAnchor

    return LearnedAnchor(
        text=text, cx=cx, cy=cy, pos_tol=pos_tol, freq=1.0, variants=tuple(variants)
    )


def make_print(
    *,
    anchors,
    learned_anchors=None,
    forbid=(),
    scrollable=False,
    name="page",
    app="app",
):
    """A matchable page: declaration plus optional captured geometry."""
    from physiclaw.conductor.spec.pages import LearnedPage, PageDecl, PagePrint

    decl = PageDecl(
        name=name,
        description=f"the {name} page",
        anchors=tuple(anchors),
        forbid=tuple(forbid),
        scrollable=scrollable,
    )
    learned = None
    if learned_anchors is not None:
        learned = LearnedPage(
            anchors={a.text: a for a in learned_anchors}, observations=6
        )
    return PagePrint(app=app, decl=decl, learned=learned)


class ScriptedProvider:
    """A `ChatProvider` that answers scripted reply strings (or raises
    the exceptions) in order, keeping every call's messages and keyword
    arguments."""

    def __init__(self, replies, *, reasoning: int = 0):
        self._replies = list(replies)
        self.calls: list[list] = []
        self.asks: list[dict] = []
        self.reasoning = reasoning
        self.closed = False

    async def chat(self, history, tools, **kw):
        self.calls.append(list(history))
        self.asks.append(kw)
        nxt = self._replies.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return AssistantMessage(
            content=nxt,
            tool_calls=[],
            finish_reason=FinishReason.STOP,
            usage=Usage(
                prompt_tokens=100, completion_tokens=20, reasoning_tokens=self.reasoning
            ),
        )

    async def aclose(self):
        self.closed = True
