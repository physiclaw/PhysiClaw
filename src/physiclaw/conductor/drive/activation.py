"""Activation — the boot's menu, its one parse_task call, and the
program a positive answer builds.

`discover` reads every pack on disk once at wake: the entries the boot
may offer (enabled and live), the whole dispatch table, and the roster
— every playbook with its state — the wake log prints so a plain model
session never leaves "why no playbook?" to guesswork. `Activation`
turns a THREAD screen into the scoped parse_task request and its
outcome into a `Program` — the baton the boot's `select` step
(`steps/select.py`) hands the conductor. Reaching the thread is the
boot route's job; this owns only the menu, the call, and the build.
"""

import logging
from dataclasses import dataclass

from physiclaw.common import daylog
from physiclaw.common.config import CONFIG
from physiclaw.common.text import clip
from physiclaw.conductor.drive.build import build_program
from physiclaw.conductor.load.pack import discover
from physiclaw.conductor.micro.decision import (
    MENU,
    NOT_A_TASK,
    PARSE_TASK,
    DecisionRequest,
    MicroOutcome,
)
from physiclaw.conductor.spec.channel import Channel
from physiclaw.conductor.spec.model import Playbook, PlaybookError, resolve_inputs
from physiclaw.conductor.spec.pack import Pack
from physiclaw.conductor.walk.program import Program
from physiclaw.conductor.walk.surface import Walk
from physiclaw.conductor.walk.thread import Thread
from physiclaw.contract.dto import Thinking
from physiclaw.contract.plugin import EventSink
from physiclaw.macros.model import MacroInput

log = logging.getLogger(__name__)


def _menu_input(i: MacroInput) -> str:
    """One declared input on the parse_task menu: name, description, the
    authored `example:` (the extraction hint — "Milk 1L" shows the
    shape a value should take better than any rule prose), and whether
    the message may leave it out: an input with a `default:` is
    optional, and the model is told so rather than left to guess which
    omissions the activation will forgive."""
    example = f"; e.g. {i.example}" if i.example else ""
    optional = f"; optional, default {i.default!r}" if not i.required else ""
    return f"{i.name} ({i.description}{example}{optional})"


@dataclass
class Activation:
    """The parse_task half of the boot: turn a THREAD screen into one
    scoped ask, and its answer into a Program. Reaching a thread screen
    is the boot route's job (`channel/boot/PLAYBOOK.yml`, walked like any
    playbook); this owns only the menu, the call, and the build, and
    rides the boot program for its `select` step (`steps/select.py`).
    `entries` is the single source — the answer space is its keys, the
    menu a render of it, each value the parsed spec+pack so a positive
    answer activates without re-reading disk."""

    entries: dict[str, tuple[Playbook, Pack]]
    channel: Channel | None
    # parse_task's context: the recent daily-log entries, assembled by
    # `activation_from`.
    context: str = ""
    # The session's event stream the activated program records into.
    events: EventSink | None = None

    def request(
        self, walk: "Walk", node_id: str, thinking: "Thinking | None" = None
    ) -> DecisionRequest:
        """The parse_task request over the walk's current screen — the
        thread: the CALLER establishes that (the boot's select step
        knows, its enter check just read it). It opens the session's
        thread (`walk.thread`), which the activated walk's later calls
        extend, at the think level the boot's step declares. `entries`
        is non-empty by construction — `activation_for` stands down
        before building an Activation with nothing to offer."""
        assert walk.screen is not None
        return walk.thread.request(
            PARSE_TASK,
            node_id,
            tuple(self.entries),
            {MENU: self._menu()},
            ledger=walk.ledger,
            listing=walk.screen.labels_text,
            frame=walk.frame,
            context=self.context,
            thinking=thinking,
        )

    def _menu(self) -> str:
        """One line per playbook: the ref (the answer key), what it does,
        its inputs. The description is what the model chooses by, so a
        playbook's description names its app the way users say it
        (two shopping apps) — `playbooks check` warns when two enabled
        playbooks across packs read the same."""
        lines = ["Available playbooks:"]
        for ref, (spec, _pack) in self.entries.items():
            inputs = ", ".join(_menu_input(i) for i in spec.inputs)
            lines.append(
                f"- {ref}: {spec.description}"
                + (f" [inputs: {inputs}]" if inputs else "")
            )
        return "\n".join(lines)

    def build(self, outcome: MicroOutcome | None, thread: "Thread") -> Program | None:
        """A ready Program from a parse_task outcome, or None (not a
        task, low confidence, or inputs that don't resolve — all stay in
        default mode, fail-open)."""
        if outcome is None or outcome.out == NOT_A_TASK:
            return None
        spec, pack = self.entries[outcome.out]
        try:
            values = resolve_inputs(spec, outcome.payload or {})
        except PlaybookError as e:
            log.warning(
                "activation %s: inputs did not resolve (%s)",
                outcome.out,
                e,
                exc_info=True,
                extra={
                    "console_message": f"Playbook {outcome.out} has missing or invalid inputs — using the task model"
                },
            )
            return None
        log.info(
            "conductor: activated %s (%s)",
            outcome.out,
            outcome.reason,
            extra={
                "event": "task.playbook_started",
                "console": True,
                "console_message": "Plan: "
                + clip(" ".join(spec.description.split()), 180)
                + f" ({outcome.out})",
            },
        )
        return build_program(
            spec, pack, values, self.channel, events=self.events, thread=thread
        )


def activation_for(channel: Channel | None) -> Activation | None:
    """The activation a boot walked OUTSIDE a wake (a stepping tool, a
    rehearsal) runs with — the same discovery, or None when no enabled
    playbook is on disk (the step then hands over, saying so)."""
    found = discover()
    return activation_from(found.entries, channel) if found.entries else None


def activation_from(
    entries: "dict[str, tuple[Playbook, Pack]]",
    channel: Channel | None,
    events: EventSink | None = None,
) -> Activation:
    return Activation(
        entries=entries,
        channel=channel,
        context=_activation_context(entries),
        events=events,
    )


def _activation_context(entries: dict[str, tuple[Playbook, Pack]]) -> str:
    """parse_task's context, assembled once at wake, from the agent's
    OWN memory convention (never a conductor-private store): the recent
    daily-log window — where completed purchases and suspensions are
    recorded, so "never re-run a finished task" reads off the same
    record the model would. The same window an agent step names as
    `context.memory: {log: <n>}`, labelled here because a wake block has
    no author to label it."""
    recent = daylog.load_recent_entries(CONFIG.memory.bootstrap_log_entries)
    return f"Recent daily-log entries (newest first):\n{recent}" if recent else ""
