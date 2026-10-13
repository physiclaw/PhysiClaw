"""Rehearsed gesture sequences, executed as one tool call.

A macro lives in ``~/.physiclaw/macros/<name>.yml`` or a conductor pack's
macro directory. The user rehearses it with ``physiclaw macros run``;
the agent invokes it through ``run_macro``. Valid files are enabled by
default, while newly generated scaffolds start disabled.

The path from authoring to execution:

    scaffold.py  the init template and installed format reference
    store.py     discover files, resolve callees, expose enabled macros
    parse.py     validate YAML, inputs, checks, jumps, and run budgets
    model.py     immutable macro shapes and screen predicates
    template.py  string-only {name} substitution with escaped braces
    inputs.py    resolve declared inputs and substitute step arguments
    steps.py     gestures, waits, forward jumps, marks, and macro calls
    runner.py    walk steps and compose one result with the current view
    runlog.py    per-run events and screenshots
    stats.py     best-effort run counters and consecutive-abort history

Execution is bounded: no loops, only forward page-based jumps and one
level of ``run`` calls. Callees share the caller's deadline, screen, and
gesture count. ``when`` / ``skip_when`` avoid redundant steps;
``require`` / ``forbid`` gate actions, and ``wait`` with ``expect`` checks
the settled screen. A failed guard, expectation, or tool call aborts.
Recovery and decisions belong to the agent.

Gesture counts track attempted actuating calls, including calls that
raise after possibly touching the phone. Checks, peeks, and timeouts
before dispatch do not count. The result uses this distinction to tell
callers whether retrying could repeat an action. Taps use their authored
boxes; labels describe targets rather than relocating them.

The package depends on common utilities, never on the agent engine.
Consumers import the submodule they need; nothing is re-exported here,
so importing a data type need not load YAML parsing or the runner.
"""
