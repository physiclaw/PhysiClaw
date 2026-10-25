"""The user channel — `playbooks/channel/<im>/`, the ONE infrastructure
pack, a folder per IM app with `channel/ACTIVE.txt` naming the one in
use (`paths.channel_root`).

The IM thread's page fingerprint, the rehearsed send/open macros, and
the boot playbook (`boot/PLAYBOOK.yml` — the walk every wake plays
before any app playbook), recorded and declared on-device like any
pack — but app playbooks never name it: asks reach it through the
conductor's node types, and the boot is the conductor's own to run.
The convention names live in `conventions.py`; which side of the
thread the user's bubbles hang from is the IM's (`reply.incoming_box`).
"""

import logging

from physiclaw.common import paths
from physiclaw.common.log_style import FailureState
from physiclaw.conductor.load.pack import load_pack, scan_playbooks
from physiclaw.conductor.spec.channel import Channel
from physiclaw.conductor.spec.conventions import (
    BOOT_PLAYBOOK,
    CHANNEL_APP,
    THREAD_PAGE,
)
from physiclaw.conductor.spec.live import require_live
from physiclaw.conductor.spec.model import Playbook, PlaybookError
from physiclaw.conductor.spec.pack import Pack

log = logging.getLogger(__name__)
_channel_failure = FailureState()
_boot_failure = FailureState()
_CHANNEL_NOTICE = (
    "Playbook messaging unavailable — using the task model. "
    "Check the channel setup (`physiclaw playbooks check`)."
)


def load_channel(*, warn: bool = True, load_boot: bool = True) -> Channel | None:
    """The channel pack, fail-open: absent or broken → None (asks and
    activation degrade; moves run unaffected). Wakes without live
    playbooks pass ``warn=False``: optional setup is only a diagnostic.
    Required setup warns once per cause until it recovers or is unused.
    Resumed walks pass ``load_boot=False`` because they bypass startup."""
    if not warn:
        _channel_failure.recovered()
        _boot_failure.recovered()
    notice = _CHANNEL_NOTICE
    try:
        root, gap = paths.channel_root()
        if root is None and gap is None:
            notice = (
                "Playbook messaging is not set up — using the task model. "
                "Set up a channel with `physiclaw playbooks init channel/<im>`."
            )
        pack = load_pack(CHANNEL_APP, root=root)
    except Exception as e:
        _boot_failure.recovered()
        log.log(
            _channel_failure.level(e) if warn else logging.DEBUG,
            "channel pack unusable (%s) — asks will hand over",
            e,
            extra={"console_message": notice},
        )
        return None
    if not any(p.decl.name == THREAD_PAGE for p in pack.prints):
        _boot_failure.recovered()
        error = PlaybookError(f"channel pack declares no {THREAD_PAGE!r} page")
        log.log(
            _channel_failure.level(error) if warn else logging.DEBUG,
            "channel pack declares no %r page — unusable, asks will hand over",
            THREAD_PAGE,
            extra={"console_message": _CHANNEL_NOTICE},
        )
        return None
    _channel_failure.recovered()
    im = (root or paths.pack_root(CHANNEL_APP)).name  # the IM folder under channel/
    return Channel(
        pack=pack, im=im, boot=_live_boot(pack, warn=warn) if load_boot else None
    )


def _live_boot(pack: Pack, *, warn: bool) -> Playbook | None:
    """The boot playbook, held to what a wake needs (`require_live`:
    enabled, every referenced macro enabled) — or None with the reason
    logged. Fail-open: no boot means the model drives the wake itself."""
    entry = next(
        (e for e in scan_playbooks(CHANNEL_APP, pack) if e.name == BOOT_PLAYBOOK),
        None,
    )
    if entry is None:
        _boot_failure.recovered()
        log.info(
            "conductor: channel has no %s/PLAYBOOK.yml — no boot at wake", BOOT_PLAYBOOK
        )
        return None
    if entry.spec is None:
        error = PlaybookError(entry.error)
        log.log(
            _boot_failure.level(error) if warn else logging.DEBUG,
            "conductor: channel boot is invalid (%s) — no boot",
            entry.error,
            extra={
                "console_message": "Playbook startup unavailable — using the task model. "
                "Run `physiclaw playbooks check` for details."
            },
        )
        return None
    _boot_failure.recovered()
    try:
        require_live(entry.spec, pack)
    except PlaybookError as e:
        log.info("conductor: %s — no boot at wake", e)
        return None
    return entry.spec
