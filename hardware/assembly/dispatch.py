"""Procedure-build orchestration shared by the two drivers — the render
pipeline (``build_procedures``) and the BOM generator (``bom``).

Loads assembly classes and retries incomplete workers. CAD-free discovery,
family batching and predecessor selection live in ``discovery``. The discovery
and batching functions are re-exported here for existing callers. Neither
rendering nor BOM policy belongs in this module.

A worker can crash (SIGSEGV) mid-batch: an intermittent OCCT hazard
(geometry kernel, or exact HLR in the render pipeline), not an OOM. It's
nondeterministic, so re-running a crashed stem in a fresh process
eventually succeeds — but the worst stems fail most attempts, hence
``retry_stems`` and the deep ``MAX_STEM_RETRIES`` budget.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable

from hardware.assembly.base import BaseAssembly
from hardware.assembly.discovery import _batches as _batches
from hardware.assembly.discovery import list_procedures as list_procedures

DEFAULT_BATCH_SIZE = 5
# Exact HLR can crash repeatedly in OCCT's rejection cache (upstream #1546).
# Retry only missing outputs in fresh processes, resuming saved render
# geometry where available. Exhaustion remains a hard build failure.
MAX_STEM_RETRIES = 12


# ── Discovery & loading ───────────────────────────────────────────────────────


def load_class(module_name: str) -> type[BaseAssembly]:
    """Import a procedure module and return its BaseAssembly subclass.
    The procedure files each define exactly one such class."""
    if "." not in module_name:
        module_name = f"hardware.assembly.procedures.{module_name}"
    mod = importlib.import_module(module_name)
    for obj in vars(mod).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, BaseAssembly)
            and obj is not BaseAssembly
            and obj.__module__ == mod.__name__
        ):
            return obj
    raise LookupError(f"No BaseAssembly subclass in {module_name}")


def load_step(module_name: str) -> BaseAssembly:
    """Instantiate a procedure's BaseAssembly subclass (assembled variant)."""
    return load_class(module_name)(exploded=False)


# ── Crash-retry ────────────────────────────────────────────────────────────────


def retry_stems(
    stems: list[str],
    *,
    run: Callable[[str], object],
    done: Callable[[str], bool],
    log: Callable[[str, int], object],
) -> None:
    """Re-run each stem in a fresh process (``run(stem)``) until it
    completes (``done(stem)`` is true) or ``MAX_STEM_RETRIES`` is exhausted,
    calling ``log(stem, attempt)`` before each attempt."""
    for stem in stems:
        for attempt in range(1, MAX_STEM_RETRIES + 1):
            log(stem, attempt)
            run(stem)
            if done(stem):
                break
