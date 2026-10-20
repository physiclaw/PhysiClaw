"""Execute playbooks through the agent's neutral TurnPlugin interface.

A playbook combines deterministic actions with small, constrained model
calls. The conductor produces ordinary assistant tool-call messages;
the engine dispatches them and returns results in the conversation
history. Neither package imports the other: shared interfaces live in
``physiclaw.contract`` and architecture tests enforce the boundary.

Start reading here
------------------
1. ``plugin.py``: session setup, provider ownership, and teardown.
2. ``drive/conductor.py``: drive a Program and resolve its model requests.
3. ``walk/program.py``: execution phases, cursor transitions, and exits.
4. ``steps/``: behavior of each node (do, agent, ask, tell, select, close).
5. ``spec/model.py`` and ``walk/surface.py``: the data and step interfaces.

Execution flow
--------------
``load`` reads a pack (one app's directory). ``route`` compiles its YAML
playbooks into nodes. Page waypoints become the adjacent nodes' enter
and verify checks; they are not executable nodes themselves.

At wake, setup restores a suspension if possible. Otherwise the channel
boot opens the user's thread and selects a playbook. Its ``baton`` is
the selected Program, which Conductor drives when the boot ends.

A Program's ``advance(history)`` or ``resolve(outcome)`` returns:

    AssistantMessage   [note, action]; dispatch and feed back its result
    DecisionRequest    call MicroCaller, then pass its outcome to resolve
    Paused             stepping stopped before opening the next node
    None               this Program has finished; follow its baton or
                       let the engine's general model take over

Only one action is pending at a time. Its exact call ID identifies its
result; a missing/error result hands over rather than replaying an action.

State and ownership
-------------------
``Program`` owns the execution phase and coordinates these state objects:

    Course      current node, expanded run rounds, and checkpoint position
    Gate        current ask, reply baseline, quoted/consented total
    Ledger      task inputs, agent outputs, round results, messages, payments
    Turnsmith   synthesized call IDs and the pending action
    Thread      model conversation shared by the boot and selected playbook
    Recoveries  pending recovery action and attempt limits
    Record      terminal outcome and diagnostic/daily-log writes

Phases are FRESH -> OPENING -> LIVE, then DONE or a stepping PAUSED.
An ask can instead save ``Program.state()`` and request session WAIT.
That suspension is consumed once on the next wake. If its end_session
fails to end the current session, the next advance discards the file.

Important invariants
--------------------
* A step checks its declared page before acting and its landing afterward.
  Recovery runs only declared actions, within their attempt limits.
* Both literal and model-read approval require the current ask's position
  on the thread. A payment additionally requires a verified app page and
  the exact quoted total at execution time. Its first action spends consent.
* A run's budget counts distinct recorded/planned rounds, including work
  from earlier revisions; restoring an unfinished round does not spend it twice.
* A model reply is validated before use. Malformed replies get one repair
  attempt; low-confidence or failed calls produce no outcome.
* The live MicroCaller borrows the session provider and owns only its
  optional micro provider. A rehearsal owns its standalone provider.
  Diagnostic sink failures must not change a validated decision.

Failure policy is explicit: run miss/on_fail overrides a page's policy,
which overrides the node's. ``stop`` requests WAIT; ``handover`` returns
the session to the general model; ``miss: skip`` skips the current round.
Unexpected program errors are recorded and handed over too. Playbook
constraints are local to this execution path, not engine-wide policies.

Package map
-----------
spec/    Typed grammar, references, page matching, reply and tap restrictions
route/   Pure YAML-to-node compilation and route validation
load/    Pack/channel files, discovery, learned page data, scaffolding
micro/   Model request composition, reply validation, retries, telemetry
walk/    State objects and the Program state machine
steps/   One executor per node kind, using the Walk interface
drive/   Live plugin driver, setup/activation, and CLI/Studio rehearsal
bench/   Offline replay, recorded-screen matching, and run analysis
"""

# No re-exports: consumers import their module directly, so parse-only
# CLI paths never pay for the provider stack.
