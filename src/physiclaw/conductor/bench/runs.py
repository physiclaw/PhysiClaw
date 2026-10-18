"""What the runs log says — `runs.jsonl` read back, summarised per
playbook, and mined for where walks escalate; the CLI's `stats` and
`propose` read here. The row itself is `walk.walklog`'s to write.
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from physiclaw.common import paths
from physiclaw.common.text import iter_jsonl
from physiclaw.conductor.walk.walklog import ESCALATION_OUTCOMES, Outcome, runs_file

log = logging.getLogger(__name__)


def load() -> list[dict]:
    """Every recorded run, oldest first. Missing file → []; a line that
    won't parse is skipped (fail-open read, like macro stats)."""
    return list(iter_jsonl(runs_file()))


@dataclass
class WalkStats:
    """One playbook's aggregate — what `playbooks stats` renders."""

    runs: int = 0
    completed: int = 0
    suspended: int = 0
    handover: int = 0
    crashed: int = 0
    abandoned: int = 0
    micros: int = 0
    rescues: int = 0
    # Handover counts per node id ("?" for a row with none), plus the
    # latest reason seen there — the example the stats line quotes.
    handover_nodes: Counter = field(default_factory=Counter)
    last_reason: dict = field(default_factory=dict)

    @property
    def escalation_rate(self) -> float:
        """`ESCALATION_OUTCOMES` over all runs — the KPI."""
        if not self.runs:
            return 0.0
        return sum(getattr(self, o) for o in ESCALATION_OUTCOMES) / self.runs

    def hot_nodes(self, top: int = 3) -> list[tuple[str, int, str]]:
        """The most-escalated nodes: (node, count, latest reason)."""
        return [
            (node, count, str(self.last_reason.get(node, "")))
            for node, count in self.handover_nodes.most_common(top)
        ]


def summarize(rows: list[dict]) -> dict[str, WalkStats]:
    """Fold run rows into per-playbook stats, keyed ``app/playbook``.
    Unknown outcome spellings (a future field, a hand-edit) count into
    `runs` only — the KPI never silently reclassifies them."""
    out: dict[str, WalkStats] = {}
    for rec in rows:
        key = f"{rec.get('app', '?')}/{rec.get('playbook', '?')}"
        st = out.setdefault(key, WalkStats())
        st.runs += 1
        st.micros += int(rec.get("micros") or 0)
        st.rescues += int(rec.get("rescues") or 0)
        outcome = rec.get("outcome")
        if outcome == Outcome.COMPLETED:
            st.completed += 1
        elif outcome == Outcome.SUSPENDED:
            st.suspended += 1
        elif outcome == Outcome.HANDOVER:
            st.handover += 1
        elif outcome == Outcome.CRASHED:
            st.crashed += 1
        elif outcome == Outcome.ABANDONED:
            st.abandoned += 1
        if outcome in ESCALATION_OUTCOMES:
            node = str(rec.get("node") or "?")
            st.handover_nodes[node] += 1
            st.last_reason[node] = str(rec.get("reason") or "")
    return out


@dataclass(frozen=True)
class EscalationSite:
    """One hot escalation site — `playbooks propose`'s triage unit."""

    app: str
    playbook: str
    node: str
    count: int
    reason: str  # the latest one seen there
    sessions: tuple[str, ...]  # recent session ids to mine, oldest first


def escalation_sites(rows: list[dict], top: int = 3) -> list[EscalationSite]:
    """The hottest escalation sites across all playbooks, most-escalated
    first — every one is authoring work the author can now see (a page
    that keeps missing, a popup no hand clears, a macro whose
    rehearsal drifted)."""
    counts: Counter = Counter()
    latest: dict = {}
    sessions: dict = {}
    for r in rows:
        if r.get("outcome") not in ESCALATION_OUTCOMES:
            continue
        key = (
            str(r.get("app") or "?"),
            str(r.get("playbook") or "?"),
            str(r.get("node") or "?"),
        )
        counts[key] += 1
        latest[key] = str(r.get("reason") or "")
        if r.get("session"):
            sessions.setdefault(key, []).append(str(r["session"]))
    out: list[EscalationSite] = []
    for key, count in counts.most_common(top):
        app, playbook, node = key
        out.append(
            EscalationSite(
                app=app,
                playbook=playbook,
                node=node,
                count=count,
                reason=latest[key],
                sessions=tuple(sessions.get(key, [])[-3:]),
            )
        )
    return out


def session_dirs(rows: list[dict]) -> list[Path]:
    """The session directories the runs name, those still on disk —
    sorted, each once."""
    sessions_dir = paths.sessions_dir()
    sids = {str(r["session"]) for r in rows if r.get("session")}
    return sorted(d for d in (sessions_dir / s for s in sids) if d.exists())
