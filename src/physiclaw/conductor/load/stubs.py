"""The texts the scaffold writes: pack templates, the format README, the
channel and ios pack stubs. Caps and vocabularies interpolate from
`limits`/`calls`/`conventions` constants, never hand-copied; the
functions that place these on disk are `scaffold.py`.
"""

from pathlib import Path

from physiclaw.common.bbox import BANDS
from physiclaw.common.paths import (
    KIND_MACRO,
    KIND_MANIFEST,
    PACK_SCHEMA,
)
from physiclaw.conductor.spec.calls import AGENT_TOOLS
from physiclaw.conductor.spec.conventions import (
    BOOT_PLAYBOOK,
    CHANNEL_APP,
    IOS_APP,
    LOCKED_PAGE,
    OPEN_MACRO,
    SEND_MACRO,
    THREAD_PAGE,
)
from physiclaw.conductor.spec.limits import (
    DEFAULT_AGENT_CALLS,
    DEFAULT_AGENT_SCROLLS,
    DEFAULT_ASK_ROUNDS,
    DEFAULT_ASK_WAIT_SECONDS,
    DEFAULT_RECOVER_LIMIT,
    MAX_NODES,
)
from physiclaw.macros import scaffold as macro_scaffold
from physiclaw.macros.model import MAX_INPUTS

MANIFEST_TEMPLATE = """\
# The pack MANIFEST — what the app is and what its playbooks share.
# Never a route: each playbook is its own folder beside this file
# (<name>/PLAYBOOK.yml, referenced as {app}/<name>), and the recorded
# hands routes share live in macros/<name>.yml. Every section here is
# optional; an empty manifest is a valid pack.
kind: manifest       # what this file is; the pack marker, never a route
schema: {schema}            # the pack grammar this file is written in
app: {app}           # which app this pack automates = the directory name
description: EDIT ME — what this pack automates, and when to adopt it

# Per-installation constants — mark them in any file below as the
# token name in doubled angle brackets; `physiclaw playbooks install`
# prompts adopters and records values in playbooks/placeholders.yml
# (files keep their tokens; parsers fill them at load):
# placeholders:
#   CONTACT:
#     description: EDIT ME — what to fill in
#     example: SomeOne

# Named fixed spots the author KNOWS — recover hands tap them, and
# agent episodes read them as a `given:` (`back: app.landmarks.back`).
# Open vocabulary; each is {{label, at, [page]}}: the tap fires at `at`
# as declared, the label says what it is; `page:` scopes it — an
# episode may read it only when that is the page it opens on, since a
# grant is decided once, with the rest of its `context:`.
# landmarks:
#   back:
#     label: "back chevron (top-left)"
#     at: [0.015, 0.045, 0.095, 0.095]
#     # page: home   # optional: refused to an episode that opens elsewhere

# Pages more than one playbook lands on are declared here once and
# referenced as `app.pages.<name>` from every route; a page only one
# route uses is declared in that route's own `pages:` block or beside
# its waypoint instead, bare there. Anchors are semantics; geometry
# is captured on YOUR device via `physiclaw playbooks pages calibrate`.
# A `recover:` here is the hand every route inherits for the page (a
# gesture, a tap on a landmark, or a pack macro BY NAME — the manifest
# carries no bodies); a route may declare its own to override it. A
# hand must land on its own page.
# pages:
#   home:
#     description: EDIT ME — what this page IS, in one line
#     anchors:
#       - {{text: ["Search", "Find"], within: top}}
#     recover: {{macro: app.macros.launch}}   # a pack hand, the one spelling everywhere
"""


PLAYBOOK_TEMPLATE = """\
# One playbook — this folder is its name ({app}/{playbook}). A route
# alternates WHERE (a page, checked every time) with WHAT (a move).
# Every key below, and the layout around this file, is explained in
# ~/.physiclaw/playbooks/README.md.
kind: entry              # the workflow the boot may offer; a playbook
                         # this one runs is `kind: playbook` beside it
schema: {schema}                # the pack grammar this file is written in
name: {playbook}         # = this folder's name; referenced as {app}/{playbook}
description: EDIT ME — one line saying what task this playbook does
# A valid playbook is enabled by default; this scaffold starts off.
enabled: false
# Values filled at activation; reference them as {{inputs.name}} in
# `with:` values and an agent's `context.given:` (a prompt writes
# `{{name}}` for a given, never the ref). ≤ {max_inputs}; `default`
# present = optional.
inputs:
  message:
    description: EDIT ME — what this value means
    example: "hello"
# pages:                   # this route's own pages in the manifest's shape,
#   sheet:                  # bare in the route below; `recover:` here is the
#     description: EDIT ME — what this page IS, in one line
#     anchors: ["EDIT ME"] # page's default hand on this route. One declaration
#     recover: go_back     # per page: here, beside a waypoint, or in APP.yml.
route:
  # An agent step with no tools may open the route — derive values
  # from the user's words before the phone is touched:
  # - agent: parse
  #   context:
  #     prompt: |
  #       EDIT ME — say exactly what to derive and the rules.
  #       The user said: "{{said}}"
  #     given: {{said: inputs.message}}   # what the prompt may write as {{name}}
  #   returns:
  #     keyword: EDIT ME — what this field holds
  #   think: off
  - start: app              # ── the cold-launch; `home` below is the
    macro:                  #    landing it must reach
      steps:
        - home_screen
        - tap: "the app icon"
          at: [0.1, 0.1, 0.3, 0.2]
        - wait: 3
  - page: home
    description: EDIT ME — what this page IS, in one line
    # `anchors:` is a list; EVERY one must show for the page to read, so
    # declare few, unmistakable texts. Alternate readings of ONE anchor
    # go inside it as a list — never as separate anchors, since each
    # declared anchor is required. `within:` pins an anchor to a band
    # ({bands}) or a box; a `forbid:` term showing reads the page out.
    anchors:
      - "EDIT ME"           # a label text that identifies this page
      # - {{text: ["Search", "Find"], within: top}}
    # forbid: ["popup text"]  # veto terms, the anchor shape — kills look-alikes
    # scrollable: true        # content may scroll under fixed chrome
    recover:                # not this page → this hand, read again
      macro:                # (`tries` times), then `on_fail`
        steps:
          - home_screen
          - tap: "the app icon"
            at: [0.1, 0.1, 0.3, 0.2]
          - wait: 3
    # recover:              # or one hand per reading:
    #   covered: {{tap: app.landmarks.dismiss}}
    #   elsewhere: go_back
    #   locked: unlock_phone
    # tries: 2              # this page's tries per walk (default {recover_limit})
    # on_fail: stop         # cannot reach this page → the session ends
  - do: {macro}             # the recorded gesture
    macro: app.macros.{macro}     # the pack's macros/{macro}.yml (a bare name is this folder's)
    with: {{message: "{{inputs.message}}"}}
  - page: home              # the landing check — hand over if not reached
  # An acting agent episode — the judgment stretch, delegated whole:
  # - agent: choose
  #   context:                       # everything the call is built from
  #     prompt: |
  #       EDIT ME — the goal, the rules, and where to finish.
  #       The item: {{item}}. Leave a wrong page by the back chevron: {{back}}
  #     given:                       # what the prompt may write as {{name}}
  #       item: parse.keyword        #   a value the walk fills
  #       back: app.landmarks.back   #   a spot a tap may aim at
  #     memory: {{log: 5}}             # parts of the agent's own memory
  #   tools: [{agent_tools}, app.macros.{macro}]  # gestures, and macros to run
  #   returns:
  #     summary: EDIT ME — what to report back
  #   limit: {{calls: {agent_calls}, scrolls: {agent_scrolls}}}
  #   think: low
  # - page: home
  # A human gate before money moves — the payment move follows it:
  # - ask: confirm-pay
  #   approve: payment
  #   total_label: "EDIT ME"  # the label the sheet total sits beside
  #   message: "EDIT ME — total {{ask.total}}, reply ok to pay or no to cancel"
  #   yes: ["ok"]
  #   no: ["no"]
  #   denied: "EDIT ME — cancelled, nothing paid"   # the walk's answer to a no
  #   wait: {ask_wait}              # seconds between reply polls
  #   rounds: {ask_rounds}          # silent polls before the session suspends
  #   think: off              # a reply the words miss is read by the model
  #   resume: {macro}         # re-enter the app after the reply
  #   on_fail: stop           # a failure here ends the session, nothing paid
  # A playbook of this pack as one move, once per item of a list:
  # - run: EDIT-ME                # a playbook of this pack, once per line
  #   each: {{item: parse.items}}   # `parse` returned
  #   miss: skip                  # a round that fails is recorded, not fatal
  # - page: home                  # add's last page
  - tell: done
    # The EXACT text sent to the user — write it in THEIR language.
    message: "EDIT ME — done with {{inputs.message}}"
"""


README_CONTENT = f"""\
# App packs

One directory per app, self-contained — everything its playbooks use:

    playbooks/<app>/
      APP.yml              the MANIFEST: what the app is and what its
                           playbooks share — app, description,
                           placeholders, landmarks, and a `pages:`
                           appendix for pages more than one route
                           lands on. Every section optional; an empty
                           file is a valid pack. Never a route.
      README.md            for people: what the pack does, the device
                           it was recorded on, the traps. Never loaded.
      macros/<n>.yml       the pack's hands, shared by every route
                           (same format as ~/.physiclaw/macros/, never
                           shown to the model's macro list).
      <name>/              one ENTRY per folder — the whole workflow
        PLAYBOOK.yml       the boot may offer (`kind: entry`); the
                           folder is the name (referenced as
                           <app>/<name>, and `name:` inside must agree)
                           and the body is the playbook: kind, name,
                           description, enabled, inputs, route (page →
                           move → page → move), and optionally pages
                           (this route's own, the manifest's shape) and
                           returns.
        <n>.yml            a playbook the entry runs (`kind: playbook`),
                           the same body: walked by its `run: <n>` only,
                           never offered (referenced as
                           <app>/<name>.<n>).
        macros/<n>.yml     the entry's recorded hands, shared by what it
                           runs (dispatch <app>/<name>.<n>): `macro: <n>`
                           here; the pack's is `macro: app.macros.<n>`.
        prompts/<n>.md     the entry's prose for the model, verbatim
                           (`prompt: prompts.<n>`; the pack's: `app.prompts.<n>`).
        README.md          the entry's notes for people. Never loaded.

Every file above starts with what it IS and which grammar it is written
in — `kind:` then `schema:`, read before anything else. The number moves
only when a change makes an existing file invalid, so a pack that meets
a physiclaw it was not written for is told "older", "newer" or "not
numbered" instead of being refused for a key its author never typed.

Everything ships disabled: rehearse a macro and a route on YOUR device
(`physiclaw macros run`, `physiclaw playbooks run`), then set
`enabled: true`. Validate everything: `physiclaw playbooks check`. Scaffold a pack:
`physiclaw playbooks init <app>` — or start from a shared template
(`physiclaw playbooks install <dir>` records its `<<PLACEHOLDER>>`
values in `playbooks/placeholders.yml`; the repo's `playbooks/`
directory ships some, and when physiclaw runs from a source checkout
those load directly — home packs shadow same-named tree packs).

The route's shape IS the contract the checker enforces: an optional
prefix of pure-text `agent` steps, `tell`s and self-starting `run`s
plus one `start` (the unconditional cold-launch) opens the route, the
first page is the start contract, every `do` — and every acting
`agent` episode — is followed by the page it lands on,
the route runs forward only and never loops back, a trailing `tell`
ends the walk,
`{{inputs.name}}` refs name declared inputs and `{{move.field}}` refs
name EARLIER agent outputs or the quoting step's own last answer, and
`irreversible: payment` moves (do and agent alike) directly follow an
`ask` with `approve: payment` — the total the user consented to is the
fire-time bound.

A `run: <n>` walks one of the playbooks beside this entry (`<n>.yml`
beside its PLAYBOOK.yml) as one move: `with:` fills its inputs,
`each: {{<input>: <move>.<field>}}` walks it once per line of a list
an earlier agent returned, `miss: skip` records a failed round and
goes on, `revise: <agent>` re-plans from that agent when a reply the
ask's words miss comes back, and `limit: {{rounds, revisions}}` bounds
both. It declares `returns: {{field: template}}`, read downstream as
`{{<run>.field}}` — the lines of its finished rounds. It is never
offered on its own and never runs a playbook itself: only an entry
runs, one level deep.

What the playbook declares is what runs — no more, no less: nothing
retries, unlocks, waits or recovers unless a line says so. An
`agent` step is the model's, inside the author's fence. `context:` is
everything the call is built from, in one block: `prompt:` the APP brief
— what the screen is, which tap means what, the traps — and nothing
else (the conductor writes the mechanism around it, and `never_tap:` is
withheld from the model on purpose, so a prompt names a label to say
which control is which, never to forbid one); `given:` the whole of what
the prompt may write as `{{name}}`: `<name>: <ref>` with the ref bare
(`inputs.item`) or in braces, a value the walk fills, or `<name>:
app.landmarks.<n>`, a fixed spot a tap may aim at, rendered as its
reading and box, stated as the fact it is (what MAY be tapped is the
tap legend's to say; it needs the `tap` tool) — each `{{name}}` is filled once when the step opens (`{{{{` and `}}}}`
are a literal brace), a name the block does not hold is
refused, a given the prompt never writes is refused, and a ref in a
prompt is refused (a prompt names a value, never its ref); `memory:`
which parts of the agent's own memory travel — `<slug>: true` for one
`## <slug>` section, `all: true` for the whole file, `log: <n>` for the
n newest daily-log entries, and nothing unless a line says so, riding
ONE block stamped as data below the brief, because a memory line is a
fact and never an instruction.
Then `tools:` the gestures it may make AND the macros it may run whole
(`macros.<name>`, this route's; `app.macros.<name>`, the pack's —
argument-less, like every helper hand), one menu, as the model reads
it, `never_tap:` the
targets its taps may NEVER press —
a reading, alternate readings of one target, or `{{label, within}}` to
say which band the target sits in; a granted macro's recorded taps are
held to it too; never shown to the model, so a pay button stays
unnameable, and a refused move costs one call while the episode goes
on, `returns:` the fields it must fill, `limit:` its
call/scroll budget, `think:` how much hidden thinking each call may
spend (off, low, medium, high — the vendor translates the word; the
reply's own `reason` field is always written); each episode turn the
model sees the screen as its own turns would — the screenshot beside
the whole element listing, icons and text rows with ids — and answers
a tool call the way its own turns would, one envelope for every tool
(`action` + `args`): `tap {{label, at}}` (what the box is; its box
`[left, top, right, bottom]`, a listed element's, a granted landmark's
or one read off the screenshot), `scroll {{direction}}`,
`back {{}}`, `run_macro {{name}}`, `done {{return fields}}`,
`escalate {{}}` — and `done` counts only on the following page, judged
by the matcher. An `ask` reads the reply against its own
`yes:`/`no:` words first (no model call when they decide); a reply
they do not cover is read by the model in the session's thread — the
same conversation that parsed the request — as confirm, deny, or
other, and other hands over (`think:` bounds that reading, as on an
agent step). A no is answered with the ask's `denied:` line, then the
entry's `on_fail` word decides. It waits by its own `wait:` seconds for
`rounds:` silent polls, and a payment ask reads the amount beside the
label its `total_label:` names. When the route completes, the walk
asks that thread for the record (the session's recap and the daily
log's memory line) and closes the session DONE itself.

A pack may declare `landmarks:` — named fixed spots ({{label, at,
[page]}}, open vocabulary) that recover hands tap and agent episodes
read as a `given:` (`back: app.landmarks.back`, written `{{back}}` in
the prompt); a `page:` scope is checked against the page the episode
opens on. A
page's `recover:` declares its recovery hand — a bare gesture
(`go_back`, `force_quit`, `home_screen`, `unlock_phone`), `{{tap:
app.landmarks.<name>}}`, or `{{macro: app.macros.<name>}}` — or one hand per reading
(`covered:` for a sheet over the page itself, `locked:` for the
phone's lock screen, `elsewhere:` for any other screen), with `tries:`
beside it as its own bound: the hand runs, the page is read again, at
most `tries` times, then `on_fail` decides — nothing before the page
runs again, so the hand must land on the page itself; nothing recovers
in the background; a page declaring none hands over. A page's
`anchors:` is the list of texts that identify it, and EVERY one must
show — declare few, unmistakable
texts; alternate readings of one text go inside it (`text: [..]`),
`within:` pins it to a band or a box, and a `forbid:` term (the same
shape) showing reads the page out. Its `description:` says what the
page IS, once, for every route that names it, and `scrollable: true`
lets the matcher look for its anchors under fixed chrome. No score: a
screen reading exactly one page whole is that page, none is unknown
(the log names each page's missing anchor), two is ambiguous.

The channel pack (`playbooks/channel/<im>/`) is the conductor's own: the
thread page, the send/open macros an `ask` runs, and `boot/` — the
walk every wake plays before any playbook (reach the thread, read the
request, hand the matching playbook the baton), a route like any
other whose `select` step is the one entry only that file may carry.

Replay a walk offline against a recorded session's screens —
`physiclaw playbooks replay <app>/<name> --session <id>` — to see
where it would hand over before touching the phone.

A page is declared ONCE per pack — in the manifest's `pages:` appendix
when routes share it (`page: app.pages.<n>`), or as one route's own, in
that file's `pages:` block or beside its waypoint (`page: <n>`, bare, in
that route only); a second declaration, in another file or in the
same one, is a pack error, never a merge.
Macros embed as `macro: {{steps: [...]}}` (the macro-file grammar minus
name/description/enabled, enabled with the playbook); an ask's
`resume:` and a page's `recover:` take the same form and dispatch
argument-less.

Limits: ≤ {MAX_NODES} moves, ≤ {MAX_INPUTS} inputs per playbook.
Macro format: see ~/.physiclaw/macros/README.md (identical grammar).
"""


CHANNEL_PACK_STUB = """\
kind: manifest
schema: {schema}
app: {im}
description: >-
  The user-channel pack — the conductor's route to YOUR user's IM
  thread: the thread page, the send/open macros its asks run, and the
  boot ({boot}/PLAYBOOK.yml) every wake walks first.

# The ONE page the conductor must recognize: your own chat thread in
# your IM app. Anchor on the chat header (your name / the contact
# name) + stable chrome.
pages:
  {thread}:
    description: EDIT ME — your own chat thread, open on its message list
    anchors:
      - "EDIT ME"                  # the thread header text
"""


CHANNEL_SEND_STUB = f"""\
kind: {KIND_MACRO}
schema: {PACK_SCHEMA}
name: {SEND_MACRO}
description: open the user's IM thread ({OPEN_MACRO}) and send {{{{message}}}} there
enabled: false
inputs:
  message:
    description: the text to send to the user
steps:
  - run: {OPEN_MACRO}                  # the navigation, recorded once
  - tap: "the input box (hidden)"
    at: [0.1, 0.9, 0.7, 0.96]        # EDIT ME
  - send_to_clipboard: "{{message}}"
  - long_press: "the input box (visible)"
    at: [0.1, 0.5, 0.7, 0.56]        # EDIT ME
  - tap: "Paste"
    at: [0.1, 0.44, 0.3, 0.5]        # EDIT ME: the Paste button
  - tap: "Send"
    at: [0.8, 0.5, 0.98, 0.56]       # EDIT ME: the Send key
"""


CHANNEL_OPEN_STUB = f"""\
kind: {KIND_MACRO}
schema: {PACK_SCHEMA}
name: {OPEN_MACRO}
description: open the user's IM thread (read only, no send)
enabled: false
steps:
  - home_screen
  - tap: "the IM app's dock icon"
    at: [0.1, 0.9, 0.2, 0.98]        # EDIT ME
  - tap: "your user's chat-row name"
    at: [0.1, 0.15, 0.9, 0.22]       # EDIT ME
"""


CHANNEL_BOOT_STUB = """\
# {app}/{boot} — the walk every wake plays before any playbook:
# reach YOUR user's thread, read the request there, hand the matching
# playbook the baton. A route like any other: edit the hands and the
# limits, step it (physiclaw playbooks step {app}/{boot}),
# replay it over a recorded wake. Live once `{open}` is enabled.
kind: entry
schema: {schema}
name: {boot}
description: reach the user's thread and read the request there
enabled: true
route:
  - page: app.pages.{thread}        # where the walk must BE (the manifest declares it)
    recover:                       # …and what to do when it is not
      locked: unlock_phone           # a sleeping phone gets no taps: wake it first
      covered: {{macro: app.macros.{open}}}     # the thread under a sheet or keyboard
      elsewhere: {{macro: app.macros.{open}}}   # any other screen: the rehearsed hand
    tries: 4                         # unlocks + opens together (an unlock races the keypad)
  - select: parse                  # the boot's own step: read the thread and select
    limit: {{scrolls: 2}}            # the playbook it asks for (scrolls up for an
                                   # older request)
    think: off                     # a thread is read, not deliberated over
"""


PACK_README_TEMPLATE = """\
# {app}

What this pack automates, in one paragraph, and when the boot should
offer it.

## Device

The phone, OS version, app version, and system language the hands were
recorded on. Coordinates replay as-is on the same model; note what
differs elsewhere.

## Traps

What broke a run and how the pack now avoids it — one line each.
"""


PLAYBOOK_README_TEMPLATE = """\
# {app}/{playbook}

What this route does, end to end, and what the user says to start it.

## Recorded facts

Where each hand's coordinates came from, and what the screens looked
like when they were recorded.

## Rehearsal

    physiclaw playbooks replay {app}/{playbook} --session <id>
    physiclaw playbooks run {app}/{playbook} -i <input>=<value>
"""


IOS_PACK_STUB = f"""\
kind: {KIND_MANIFEST}
schema: {PACK_SCHEMA}
app: {IOS_APP}
description: >-
  iOS system states the conductor must name — no playbooks, no macros.
  Geometry is captured on YOUR device (conductor calibrate ios --guided).

pages:
  # The lock screen. Telling "locked" apart from "a screen I don't
  # recognize" matters because they demand opposite actions —
  # `unlock_phone` costs 20-40s and does nothing on an unlocked phone,
  # while a recovery macro's taps land uselessly on a lock screen. Every
  # other unrecognized screen collapses into one arm, which is why no
  # other system page is declared yet.
  #
  # THIS DECLARATION IS A BONUS, NOT THE MECHANISM. Measured across
  # every state a real iPhone's cover can be put in — resting Always-On
  # Display, woken and fully lit, and after a swipe — iOS printed no
  # hint text at all, so the anchor below has nothing to match and the
  # page never reads. The matcher therefore recognizes the cover by its
  # SHAPE first (a clock and nothing else — `match.reads_as_locked`),
  # which needs no declaration and no calibration, and every page's
  # `locked:` recover hand fires on it. Keep this page: on a device or
  # version that DOES print a hint it is the sharper signal, and it
  # costs nothing when it never matches.
  {LOCKED_PAGE}:
    description: the phone's lock screen, when it prints an unlock hint
    anchors:
      # ONE anchor, several acceptable readings — never separate
      # anchors: every declared anchor must show, so a second spelling
      # of the same label would demand both at once.
      #
      # Verify this reading against YOUR phone before trusting it —
      # lock it and run `physiclaw playbooks pages propose --live` to see
      # what it actually prints, then put that beside this one:
      #
      #     - text: ["Swipe up for Face ID or Enter Passcode", "<yours>"]
      - text: ["Swipe up for Face ID or Enter Passcode"]
        within: bottom
"""


# ---------- rendered ----------


EXAMPLE_MACRO = "example-move"
EXAMPLE_PLAYBOOK = "example"


def render_manifest_stub(app: str) -> str:
    return MANIFEST_TEMPLATE.format(app=app, schema=PACK_SCHEMA)


def render_playbook_stub(app: str) -> str:
    """The scaffold's example playbook file (`<EXAMPLE_PLAYBOOK>.yml`)."""
    return PLAYBOOK_TEMPLATE.format(
        app=app,
        schema=PACK_SCHEMA,
        playbook=EXAMPLE_PLAYBOOK,
        macro=EXAMPLE_MACRO,
        max_inputs=MAX_INPUTS,
        agent_tools=", ".join(AGENT_TOOLS),
        agent_calls=DEFAULT_AGENT_CALLS,
        agent_scrolls=DEFAULT_AGENT_SCROLLS,
        recover_limit=DEFAULT_RECOVER_LIMIT,
        bands=" / ".join(BANDS),
        ask_wait=DEFAULT_ASK_WAIT_SECONDS,
        ask_rounds=DEFAULT_ASK_ROUNDS,
    )


def channel_boot_stub(root: Path) -> str:
    """The boot for a channel pack: its hands are the pack's `open`."""
    return CHANNEL_BOOT_STUB.format(
        schema=PACK_SCHEMA,
        app=CHANNEL_APP,
        boot=BOOT_PLAYBOOK,
        open=OPEN_MACRO,
        thread=THREAD_PAGE,
        im=root.name,
    )


def render_pack_readme(app: str) -> str:
    """The pack's `README.md` seed — the author's notes, never loaded.
    Written once by `init`; the loader reads no `.md` outside `prompts/`."""
    return PACK_README_TEMPLATE.format(app=app)


def render_playbook_readme(app: str, playbook: str) -> str:
    """A playbook folder's `README.md` seed, same rule."""
    return PLAYBOOK_README_TEMPLATE.format(app=app, playbook=playbook)


def render_example_macro() -> str:
    """The pack's example macro — the macro scaffold verbatim (it parses
    clean and is the macro-format documentation)."""
    return macro_scaffold.render_init(EXAMPLE_MACRO)
