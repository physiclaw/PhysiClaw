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
from physiclaw.common.log_style import console_text
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


def load_channel() -> Channel | None:
    """The channel pack, fail-open: absent or broken → None (asks and
    activation degrade; moves run unaffected)."""
    try:
        pack = load_pack(CHANNEL_APP)
    except Exception as e:
        log.warning(
            "channel pack unusable (%s) — asks will hand over",
            e,
            extra={
                "console_message": "Phone messaging setup unavailable — requests need the task model: "
                + console_text(e, 140)
            },
        )
        return None
    thread = next((p.decl for p in pack.prints if p.decl.name == THREAD_PAGE), None)
    if thread is None:
        log.warning(
            "channel pack declares no %r page — unusable, asks will hand over",
            THREAD_PAGE,
            extra={
                "console_message": "Phone messaging setup has no thread page — requests need the task model"
            },
        )
        return None
    im = paths.pack_root(CHANNEL_APP).name  # the IM folder under channel/
    return Channel(pack=pack, im=im, boot=_live_boot(pack))


def _live_boot(pack: Pack) -> Playbook | None:
    """The boot playbook, held to what a wake needs (`require_live`:
    enabled, every referenced macro enabled) — or None with the reason
    logged. Fail-open: no boot means the model drives the wake itself."""
    entry = next(
        (e for e in scan_playbooks(CHANNEL_APP, pack) if e.name == BOOT_PLAYBOOK),
        None,
    )
    if entry is None:
        log.info(
            "conductor: channel has no %s/PLAYBOOK.yml — no boot at wake", BOOT_PLAYBOOK
        )
        return None
    if entry.spec is None:
        log.warning(
            "conductor: channel boot is invalid (%s) — no boot",
            entry.error,
            extra={
                "console_message": "Phone startup playbook unavailable — using the task model: "
                + console_text(entry.error, 140)
            },
        )
        return None
    try:
        require_live(entry.spec, pack)
    except PlaybookError as e:
        log.info("conductor: %s — no boot at wake", e)
        return None
    return entry.spec
