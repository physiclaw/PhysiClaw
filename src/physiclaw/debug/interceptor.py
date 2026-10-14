"""Virtualize conductor channel observations after real execution.

Channel macros always run on the phone. Only a confirmed success, carried
in the runner's header metadata, can replace the observation with the
scripted thread. A successful send appends the agent's virtual bubble.
An aborted or unconfirmed macro keeps its real result and clears the
on-thread state; header wording and screen-change verdicts are not success.

While on the thread, conductor peeks render the virtual conversation and
release staged replies through `thread.peek_bubbles`. A real peek matching
the channel fingerprint can also enter this state after a session resumes.
Other phone-moving tools leave it. Model-authored calls are never rewritten;
dispatch supplies the session's synthesized-turn bit as provenance.

State belongs to one session. The channel fingerprint is cached, while the
thread file is re-read on each render. Harness errors clear the state and
keep the real observation so a subsequent peek must identify the thread again.
"""

import logging
from functools import cached_property

from physiclaw.common import gesture_vocab, verdict
from physiclaw.common.bbox import Bbox
from physiclaw.common.listing import Screen
from physiclaw.conductor.spec.conventions import CHANNEL_APP, SEND_MACRO
from physiclaw.conductor.spec.match import match_screen
from physiclaw.conductor.spec.pack import macro_app, qualified_macro
from physiclaw.conductor.spec.pages import PagePrint
from physiclaw.contract.dto import ToolCall
from physiclaw.debug import thread as vthread
from physiclaw.macros.model import MACRO_OK_META

log = logging.getLogger(__name__)

# Tools whose result cannot mean the phone left the thread — the
# on-thread state survives them; ANY other transformed-or-passed tool
# drops it (conservative: a wrong drop costs one open-macro retry, a
# wrong keep would read a fake thread over a real app screen). Local
# text-returning tools (note, end_session, …) never reach the
# transformer at all — none of them can move the phone.
_KEEP_STATE = frozenset({gesture_vocab.PEEK, "wait"})


def build() -> "FakeChannel":
    """The factory `agent.engine.plugins.load_debug_intercept` names by
    dotted path — the engine never imports this package."""
    return FakeChannel()


class FakeChannel:
    """One session's virtual user channel behind the dispatch seam."""

    def __init__(self) -> None:
        self._on_thread = False

    @cached_property
    def _view(self) -> "tuple[PagePrint | None, Bbox]":
        # Per-session cache — the channel pack cannot change mid-session
        # (a None fingerprint, a valid answer, is cached too).
        return vthread.channel_view()

    @property
    def _pp(self) -> PagePrint | None:
        return self._view[0]

    @property
    def incoming(self) -> Bbox:
        """The IM's incoming box, as the real channel's."""
        return self._view[1]

    def intercept(
        self, call: ToolCall, synthesized: bool, blocks: list[dict]
    ) -> list[dict] | None:
        """Replacement blocks for a REAL result just produced by `call`,
        or None — keep it untouched. `synthesized` is the session's
        plugin-minted-turn bit. Never raises: a harness bug must fail
        open into the real observation, not kill the session."""
        try:
            return self._intercept(call, synthesized, blocks)
        except Exception:
            self._on_thread = False
            log.exception("debug fake-channel crashed — keeping the real result")
            return None

    def _render(self, bubbles: list[vthread.Bubble]) -> str:
        return vthread.render_listing(bubbles, self._pp, self.incoming)

    def _intercept(
        self, call: ToolCall, synthesized: bool, blocks: list[dict]
    ) -> list[dict] | None:
        args = call.arguments or {}
        macro = str(args.get("name") or "")
        if (
            synthesized
            and call.name == gesture_vocab.RUN_MACRO
            and macro_app(macro) == CHANNEL_APP
        ):
            metadata = blocks[0].get("_meta") if blocks else None
            if (
                not isinstance(metadata, dict)
                or metadata.get(MACRO_OK_META) is not True
            ):
                self._on_thread = False
                return None
            if macro == qualified_macro(CHANNEL_APP, SEND_MACRO):
                message = str((args.get("inputs") or {}).get("message") or "")
                bubbles = vthread.record_send(message)
                action = f"debug: {macro} ran on the phone — observation virtualized"
            else:
                bubbles = vthread.load().bubbles
                action = f"debug: {macro} ran on the phone — now on the virtual thread"
            self._on_thread = True
            log.info("debug fake-channel: %s", action)
            return vthread.gesture_blocks(
                verdict.attach(action, True), self._render(bubbles)
            )
        if synthesized and call.name == gesture_vocab.PEEK:
            if not self._on_thread and self._reads_as_thread(blocks):
                self._on_thread = True
                log.info(
                    "debug fake-channel: the real thread is on screen — virtualized"
                )
            if self._on_thread:
                log.info("debug fake-channel: virtualized peek of the thread")
                return vthread.view_blocks(self._render(vthread.peek_bubbles()))
        if call.name not in _KEEP_STATE:
            self._on_thread = False
        return None

    def _reads_as_thread(self, blocks: list[dict]) -> bool:
        """Whether a REAL peek shows the channel thread — judged by the
        same matcher and fingerprint the conductor would use on it."""
        if self._pp is None:
            return False
        screen = Screen.read(verdict.screen_text(blocks))
        return match_screen(screen, [self._pp]).matches(self._pp.page_id)
