"""Phone watchdog hook — fires when /api/phone/watch reports an event.

Auto-discovered by `physiclaw.agent.runtime.hook.load_hooks()`. Dials
`config.server_url()` — the launcher pins `PHYSICLAW_SERVER` from the
`--server` flag before hooks are loaded, and that env var wins there.

Later siblings (e.g. a cron hook) live next to this one and return the
same `Trigger` shape, so the runtime loop treats all event sources
uniformly.
"""

import logging

import httpx

from physiclaw.agent.runtime.hook import Trigger, register
from physiclaw.common import platform
from physiclaw.common.config import server_url
from physiclaw.common.log_style import FailureState

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
_failure = FailureState()


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=server_url(), timeout=10.0, trust_env=platform.TRUST_PROXY_ENV
        )
    return _client


@register
async def phone_watch() -> Trigger | None:
    try:
        r = await _get_client().get("/api/phone/watch")
        r.raise_for_status()
        # Malformed JSON or an invalid watch envelope is the same blip as
        # a failed request. Only a real boolean can report a wake event.
        data = r.json()
        if not isinstance(data, dict) or not isinstance(data.get("wake"), bool):
            raise ValueError("expected a watch object with a boolean wake field")
        if _failure.recovered():
            log.info(
                "Phone watch connection restored",
                extra={"event": "phone.watch_recovered", "console": True},
            )
    except Exception as e:
        log.log(
            _failure.level(e),
            "Phone watch poll failed: %s — retrying automatically",
            e,
            exc_info=True,
            extra={"event": "phone.watch_failed"},
        )
        return None
    if not data["wake"]:
        return None
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = "phone screen changed"
    return Trigger(description=reason, source="phone")
