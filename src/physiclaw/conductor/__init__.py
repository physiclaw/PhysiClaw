"""The conductor — deterministic turns for the routine parts of a task.

A peer of the agent, not part of it. The engine loads this package only
through the `TurnPlugin` seam (`contract.plugin`, named by the
`[agent] plugins` config path); neither package imports the other, and
`tests/test_architecture.py` enforces that. Each turn the conductor
either synthesizes the assistant message itself (no provider call) or
passes, and the LLM speaks.

Vocabulary, in the order a reader meets it:

    pack       one app's directory: APP.yml + macros/ + <playbook>/PLAYBOOK.yml
    playbook   one task in a pack, written as a route
    entry      one YAML item of a route (its leading key is the kind)
    page       a waypoint entry: where the walk must BE, checked every
               time; declared once (anchors), referenced bare elsewhere
    move       any non-page entry: start, do, agent, ask, tell
    node       a compiled move (pages compile away into the adjacent
               moves' enter/verify checks)
    walk       one playbook executing (`Program`), cursor over nodes
    step       the executor running the node at the cursor
    turn       one synthesized [note, action] assistant message; the
               action's result comes back as ordinary history
    verdict    the matcher's reading of a screen: match / occluded /
               unknown, with the page id
    hand       a declared recovery action: one gesture or one macro
    landmark   a named fixed spot the author knows ({label, bbox})
    grant      what an episode is given: a landmark (shown with its box,
               tapped like any other) or a macro (run by name)
    gate       the ask-and-hold state (reply words decide first; a reply
               they miss is read in the thread; consent)
    ledger     the walk's one account: task, decisions, said, paid
    thread     the session's conversation with the model about the errand:
               the parse, a vague reply, the closing record — append-only,
               the ledger's delta between calls, one cached prefix
    brief      the walk's last note: why it stopped, where it stands
    handover   the walk goes quiet; the model takes the session

One wake, end to end:

    plugin.session_setup  → setup: a suspension resumes, else the boot
    the boot              → Program over channel/boot/PLAYBOOK.yml: peek, the thread
                            page's declared hands, then `select` —
                            parse_task over the enabled playbooks
    the baton             → activation builds the matching playbook's
                            Program; the conductor drives it next
    the walk              → each node's step: a move's enter check, its
                            macro, its verify; an agent's fenced calls;
                            an ask's send, hold, and read; a tell's send
    a deviation           → the page's `recover:` hand, or handover
    the end               → completion (the walk closes the session DONE
                            itself), handover (one brief turn, the model
                            continues), a stop, or an ask out of patience
                            (suspended.json); the record writes the runs
                            row and the daily log

The seam:
    plugin.py           the composition point the engine names: wake setup,
                        micro wiring, one Conductor per session

The packages, in the order a reader meets them — each imports only the
ones above it:

spec/ — the grammar and the page model, as data:
    conventions.py      the fixed names: channel, ios, thread, boot, lock
    limits.py           every bound a walk runs under, in one place
    specfile.py         shared YAML substrate
    model.py            the playbook grammar as dataclasses
    pack.py             the Pack a playbook validates against; `app/<name>` addressing
    live.py             the live rule: what a wake needs of a playbook
    channel.py          the user channel, loaded, as data: thread, send, open, boot
    refs.py             the `{inputs.x}` / `{move.field}` / `{name}` grammar
    calls.py            the episode vocabulary the parser and walk share
    memory.py           the `context.memory:` vocabulary
    manifest.py         the manifest's top-level grammar: sections and meta
    fence.py            the never_tap fence, judged on a live screen
    reply.py            deterministic confirm/deny reply reading
    pages.py            declared page fingerprints, landmarks, learned geometry
    match.py            the open-set page matcher (the lock screen by shape)

route/ — the route compiler, pure over spec (reads no file):
    compile.py          the door: `route:` → moves, start page, hands
    scope.py            the `Scope` one compile threads through, the `Line` each parser reads
    fields.py           the field rules every move shares
    resolve.py          macros, prompt files and landmarks by reference
    do.py, agent.py, ask.py, tell.py, select.py, run.py   one parser per move kind
    recover.py          declared recovery: pages' hands, layered
    playbook.py         a playbook file → its `Playbook`, the route compiled
    lints.py            the whole-route checks and the check-time advisories

load/ — reading packs off the disk into the grammar, and writing new ones:
    files.py            a pack's YAML files, read: manifest and playbooks
    pack.py             the pack door: load, scan, discover, load_spec
    prints.py           declared pages off the disk, and the learned store
    channel.py          the user-channel pack door: load it, hold its boot live
    scaffold.py         pack init and the format README, on disk
    stubs.py            the texts the scaffold writes

micro/ — asking the model: the scoped calls, one subsystem:
    decision.py         the shapes: request, outcome, the call names
    blocks.py           the blocks a user turn is built from, data-stamped
    moves.py            a move as (action, args): read, keyed, said
    calltable.py        one row per call: legend, answer space, outcome
    compose.py          the request as messages; the settled exchange
    answers.py          reading a reply against what its call declares
    prompts.py          what the calls say — the prompt texts
    channel.py          the channel: the provider call, retries, the logs

walk/ — one playbook executing:
    program.py          the walk: phase, cursor moves, verdicts, the terminal moments
    surface.py          the seam with the steps: `Step`, `Walk`, `Steps`
    course.py           the Course: the route as slots, and the cursor on it
    rounds.py           a run's rounds as values, and the refs read at the cursor
    speak.py            the walk's voice: send, land, read replies
    gate.py             the ask-and-hold state and consent's moves, one suspension projection
    money.py            the declared total and the payment guard
    recover.py          declared recovery: the page's hands, bounds, and counts
    turns.py            minting a synthesized turn, the one in flight
    views.py            reading tool results out of the transcript
    brief.py            the report the walk's last turn carries
    ledger.py           the walk's one account: what was asked, decided,
                        said, answered, paid — every step writes it
    thread.py           the session's one conversation with the model
    suspension.py       suspended.json, the one cross-wake file
    record.py           the walk's writes: the runs row, the daily log, the purchase line
    walklog.py          runs.jsonl — the per-walk row, written

steps/ — one executor per route line kind, on the walk's surface:
    table.py            the one table of executors the walk is handed
    do.py               `do`/`start`: enter check, the macro, verify
    agent.py            `agent`: the pure-text call, or the episode
    memory.py           reading the memory parts an agent step declared
    ask.py              `ask`: send, hold, judge the reply, bind consent
    tell.py             `tell`: send, then move on
    select.py           the boot's `select`: parse_task, the baton
    close.py            a completed walk's last step: the record, DONE

drive/ — the live doors that drive a walk:
    conductor.py        the per-turn arbiter; None means "the LLM speaks"
    setup.py            the doors: a resumed suspension, the boot, `arm` for a ref
    build.py            the one Program constructor call
    activation.py       the boot's menu, parse_task, the program it builds
    rehearsal.py        the engine's loop without the session: run, step
    exchange.py         what a rehearsal reports: the model round-trips, as lines
    hooks.py            the typed callable seams a driver takes

bench/ — off the phone, over what sessions recorded:
    replay.py           the real walk over recorded screens, writing nothing
    decisions.py        recorded micro calls re-asked and measured
    capture.py          mining fingerprints from recorded observations
    corpus.py           recorded-session listings for offline matching
    runs.py             runs.jsonl read back: stats, escalation sites
"""

# No re-exports: consumers import their module directly, so parse-only
# CLI paths never pay for the provider stack.
