"""Recommend sourcing-guide URLs in small groups and record them as checked.

Vendor pages open only on keypress. Unchecked or changed selections come first;
otherwise priority is elapsed days * BOM weight (2..1) * vendor weight (3..1).
Each pool completes rounds using recommendation counts from the last 30 days.
Recommendation history is local and independent of generated document cleanup.
"""

from __future__ import annotations

import argparse
import codecs
import fcntl
import hashlib
import json
import os
import random
import re
import select
import shutil
import sys
import tempfile
import termios
import tty
import webbrowser
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from hardware.build_lock import locked_main
from hardware.documents import BuildError
from hardware.documents.bom.loader import load_bom_rows
from hardware.documents.common import loc
from hardware.documents.sourcing import VENDOR_FILES
from hardware.documents.sourcing.build import (
    OPTIONAL_TOOLS_FILE,
    clean_supplier_url,
    ditto_walk,
    load_sourcing_data,
    supplier_columns,
    sync_entries,
)
from hardware.log_style import _color_enabled, _fit_line
from hardware.scheme import OUTPUT_DIR

STATE_FILE = OUTPUT_DIR / ".url-checks.json"
PER_PLATFORM = 3
COVERAGE_WINDOW = timedelta(days=30)
PLATFORMS = {
    "taobao": ("Taobao", "T"),
    "aliexpress": ("AliExpress", "A"),
}
# Soft marketplace accents and links; body text keeps the terminal's foreground.
PALETTE = {
    "taobao": "1;38;5;173",  # warm orange
    "aliexpress": "1;38;5;141",  # lavender
    "url": "38;5;74",  # sky blue
    "muted": "38;5;244",  # slate gray
    "strong": "1",
}


@dataclass(frozen=True)
class Usage:
    part_id: str
    component: str
    spec: str
    qty: str
    supplier: str
    product: str
    bom_position: int
    vendor_position: int
    bom_count: int
    source: str = "supplier"

    @property
    def importance(self) -> float:
        bom_weight = (
            0.5
            if self.source == "optional"
            else 2 - (self.bom_position - 1) / max(1, self.bom_count - 1)
        )
        return bom_weight * max(1, 4 - self.vendor_position)


@dataclass
class Candidate:
    url: str
    platform: str
    usages: list[Usage]

    @property
    def importance(self) -> float:
        # Shared URLs inherit their most important use, rather than accumulating
        # an arbitrary bonus for appearing in several rows.
        return max(usage.importance for usage in self.usages)

    @property
    def fingerprint(self) -> str:
        # Moving rows or supplier slots changes priority, not what was checked.
        selections = [
            {
                key: value
                for key, value in asdict(usage).items()
                if key not in {"bom_position", "vendor_position", "bom_count"}
                and (key != "source" or value != "supplier")
            }
            for usage in self.usages
        ]
        payload = json.dumps(
            sorted(selections, key=lambda item: json.dumps(item, sort_keys=True)),
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


def platform_for(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname
    for platform, domains in (
        ("taobao", ("taobao.com", "tmall.com")),
        ("aliexpress", ("aliexpress.com", "aliexpress.us")),
    ):
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return platform
    return "other"


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.urls.extend(
            value for key, value in attrs if key in {"href", "src"} and value
        )

    def handle_data(self, data):
        self.text.append(data)


def extract_urls(value: object, lang: str) -> list[str]:
    """Read structured URLs, inline HTML and bare links in localized source data."""
    found: dict[str, None] = {}

    def add(url: str) -> None:
        if platform_for(url) is not None:
            found[clean_supplier_url(url)] = None

    def walk(item: object) -> None:
        if isinstance(item, dict):
            if item and set(item) <= {"en", "zh"}:
                walk(loc(item, lang))
            else:
                for child in item.values():
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str):
            text = item.strip()
            if text.startswith(("https://", "http://")) and not re.search(
                r"[\s<>]", text
            ):
                add(text)
                return
            parser = _Links()
            parser.feed(text)
            for url in parser.urls:
                add(url)
            for url in re.findall(
                r"https?://[^\s<>\"'，。；！？]+", " ".join(parser.text)
            ):
                url = url.rstrip(".,;!?")
                for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
                    while url.endswith(closing) and url.count(closing) > url.count(
                        opening
                    ):
                        url = url[:-1]
                add(url)

    walk(value)
    return list(found)


def load_optional_tools() -> list[dict]:
    try:
        tools = json.loads(OPTIONAL_TOOLS_FILE.read_text(encoding="utf-8"))
        if not isinstance(tools, list):
            raise ValueError("expected a JSON array")
        return tools
    except (OSError, ValueError) as exc:
        raise BuildError(
            f"Cannot read optional tools {OPTIONAL_TOOLS_FILE}: {exc}"
        ) from exc


def collect_candidates(
    rows: list[dict],
    vendors: dict[str, list[dict]],
    optional_tools: list[dict] | None = None,
) -> list[Candidate]:
    by_url: dict[str, Candidate] = {}
    ids = [row["part_id"] for row in rows]

    def add_links(
        value, row, index, lang, *, supplier=None, position=1, source="supplier"
    ) -> None:
        primary_url = None
        if supplier is not None and source == "supplier":
            primary_url = clean_supplier_url(supplier.get("url", ""))
        for url in extract_urls(value, lang):
            platform = platform_for(url)
            if platform not in PLATFORMS:
                continue
            vendor = (
                supplier if supplier is not None else {"name": urlsplit(url).hostname}
            )
            link_source = source
            if supplier is not None and source == "supplier" and url != primary_url:
                link_source = "details"
            usage = Usage(
                part_id=row["part_id"],
                component=loc(row.get("component", ""), lang),
                spec=loc(row.get("spec", ""), lang),
                qty=row.get("qty", ""),
                supplier=loc(vendor.get("name", ""), lang),
                product=loc(vendor.get("product") or vendor.get("spec") or "", lang),
                bom_position=index + 1,
                vendor_position=position,
                bom_count=len(rows),
                source=link_source,
            )
            candidate = by_url.setdefault(url, Candidate(url, platform, []))
            if usage not in candidate.usages:
                candidate.usages.append(usage)

    for lang, authored in vendors.items():
        entries, added, stale = sync_entries(rows, authored)
        if added or stale:
            raise BuildError(
                f"Supplier rows for {lang} are out of sync; run make hw-check"
            )
        for vendor_index, column in enumerate(supplier_columns(entries)):
            _, resolved = ditto_walk(column, "suppliers", ids)
            for bom_index, (row, supplier) in enumerate(zip(rows, resolved)):
                if not isinstance(supplier, dict):
                    continue
                add_links(
                    supplier,
                    row,
                    bom_index,
                    lang,
                    supplier=supplier,
                    position=vendor_index + 1,
                )

        # Ditto notes inherit the owning BOM context, just like supplier cells.
        for key in sorted(
            {key for entry in entries for key in entry}
            - {"part_id", "suppliers", "accessories"}
        ):
            _, resolved = ditto_walk([entry.get(key) for entry in entries], key, ids)
            for index, (row, value) in enumerate(zip(rows, resolved)):
                add_links(value, row, index, lang, source=key)
        for index, (row, entry) in enumerate(zip(rows, entries)):
            for position, accessory in enumerate(entry.get("accessories", []), 1):
                add_links(
                    accessory,
                    row,
                    index,
                    lang,
                    supplier=accessory,
                    position=position,
                    source="accessory",
                )
            add_links(row, row, index, lang, source="bom")

        for index, tool in enumerate(optional_tools or []):
            row = {
                "part_id": loc(tool["name"], lang),
                "component": tool["name"],
                "spec": tool.get("spec", ""),
            }
            for position, supplier in enumerate(
                tool.get("sources", {}).get(lang, []), 1
            ):
                add_links(
                    supplier,
                    row,
                    index,
                    lang,
                    supplier=supplier,
                    position=position,
                    source="optional",
                )
            metadata = {key: value for key, value in tool.items() if key != "sources"}
            add_links(metadata, row, index, lang, source="optional")
    return list(by_url.values())


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("check times must include a timezone")
    return parsed


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "round_index": 0, "checks": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state["version"] != 1:
            raise ValueError("unsupported version")
        round_index = state["round_index"]
        if type(round_index) is not int or round_index < 0:
            raise ValueError("invalid recommendation round")
        for url, check in state["checks"].items():
            if not isinstance(url, str) or not isinstance(check["fingerprint"], str):
                raise ValueError("invalid check record")
            parse_time(check["checked_at"])
            if not isinstance(check["shown_at"], list):
                raise ValueError("invalid shown_at history")
            for timestamp in check["shown_at"]:
                parse_time(timestamp)
            last_round = check["last_round"]
            if type(last_round) is not int or not 0 <= last_round <= round_index:
                raise ValueError("invalid URL recommendation round")
        return {"version": 1, "round_index": round_index, "checks": state["checks"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise BuildError(f"Cannot read URL review history {path}: {exc}") from exc


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A failed/interrupted write must not erase previous verification dates.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def history_lock(path: Path):
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the sidecar: replacing the JSON must not replace the locked inode.
    with path.with_name(path.name + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BuildError(f"URL history already in use: {path}") from None
        yield path


def last_checked(candidate: Candidate, state: dict) -> datetime | None:
    check = state["checks"].get(candidate.url)
    if check is None or check["fingerprint"] != candidate.fingerprint:
        return None
    return parse_time(check["checked_at"])


def recent_recommendations(
    check: dict, cutoff: datetime, *, until: datetime | None = None
) -> list[str]:
    timestamps = []
    for timestamp in check["shown_at"]:
        when = parse_time(timestamp)
        if when > cutoff and (until is None or when <= until):
            timestamps.append(timestamp)
    return timestamps


def select_round(
    candidates: list[Candidate],
    state: dict,
    now: datetime,
    *,
    rng: random.Random | None = None,
) -> dict[str, Candidate]:
    rng = rng or random.SystemRandom()
    cutoff = now - COVERAGE_WINDOW
    checked_times = {
        candidate.url: last_checked(candidate, state) for candidate in candidates
    }
    counts = {
        candidate.url: len(
            recent_recommendations(state["checks"][candidate.url], cutoff, until=now)
        )
        if checked_times[candidate.url] is not None
        else 0
        for candidate in candidates
    }
    # Cache draws per URL: sort comparisons and coverage selection must use the
    # same priority. Outside roughly a 5% score gap, ordering cannot reverse.
    draws = {
        candidate.url: (rng.uniform(0.975, 1.025), rng.random())
        for candidate in sorted(candidates, key=lambda item: item.url)
    }

    def priority(candidate: Candidate) -> tuple:
        checked = checked_times[candidate.url]
        elapsed_days = max(0, (now - checked).total_seconds()) / 86400 if checked else 1
        score = candidate.importance * elapsed_days
        first = min(
            (usage.bom_position, usage.vendor_position) for usage in candidate.usages
        )
        return (
            checked is not None,
            -score * draws[candidate.url][0],
            state["checks"][candidate.url]["last_round"] if checked else 0,
            draws[candidate.url][1],
            first[0],
            first[1],
            candidate.url,
        )

    selected = {}
    for platform, (_, prefix) in PLATFORMS.items():
        pool = [candidate for candidate in candidates if candidate.platform == platform]
        if not pool:
            continue
        # Finish count 0 before count 1, then count 1 before count 2, and so on.
        # A final batch can be smaller than three; pools advance independently.
        minimum = min(counts[candidate.url] for candidate in pool)
        ranked = sorted(
            (candidate for candidate in pool if counts[candidate.url] == minimum),
            key=priority,
        )
        chosen = ranked[:PER_PLATFORM]
        for index, candidate in enumerate(chosen, 1):
            selected[f"{prefix}{index}"] = candidate
    return selected


def record_round(selected: dict[str, Candidate], state: dict, now: datetime) -> dict:
    round_index = state["round_index"] + 1
    timestamp = now.isoformat()
    checks = dict(state["checks"])
    for candidate in selected.values():
        fingerprint = candidate.fingerprint
        previous = checks.get(candidate.url)
        timestamps = (
            recent_recommendations(previous, now - COVERAGE_WINDOW)
            if previous and previous["fingerprint"] == fingerprint
            else []
        )
        checks[candidate.url] = {
            "fingerprint": fingerprint,
            "checked_at": timestamp,
            "last_round": round_index,
            "shown_at": [*timestamps, timestamp],
        }
    return {
        "version": 1,
        "round_index": round_index,
        "checks": checks,
    }


def print_round(
    selected: dict[str, Candidate],
    state: dict,
    now: datetime,
    state_path: Path,
    *,
    verbose: bool = False,
) -> None:
    color = _color_enabled()
    columns = max(1, shutil.get_terminal_size(fallback=(100, 24)).columns - 1)

    def styled(text: str, tone: str | None) -> str:
        return f"\033[{PALETTE[tone]}m{text}\033[0m" if color and tone else text

    printed_group = False
    for platform, (label, _) in PLATFORMS.items():
        group = [item for item in selected.values() if item.platform == platform]
        if not group:
            continue
        if printed_group:
            print()
        printed_group = True
        print(styled(label, platform) + styled(f" ({len(group)})", "muted"))
        for candidate in group:
            checked = last_checked(candidate, state)
            age = ""
            if checked:
                age = f"{max(0, (now - checked).total_seconds()) / 86400:.1f}d ago"
            elif candidate.url in state["checks"]:
                age = "changed"
            usages = sorted(candidate.usages, key=lambda item: item.bom_position)
            part_ids = list(dict.fromkeys(usage.part_id for usage in usages))
            parts = ", ".join(part_ids)
            if not verbose and len(parts) > 48 and len(part_ids) > 1:
                families = {part_id.split("-", 1)[0] for part_id in part_ids}
                parts = (
                    f"{next(iter(families))} ({len(part_ids)} parts)"
                    if len(families) == 1
                    else f"{part_ids[0]} (+{len(part_ids) - 1})"
                )
            suppliers = ", ".join(dict.fromkeys(usage.supplier for usage in usages))
            primary = max(usages, key=lambda usage: usage.importance)
            if primary.source == "optional":
                rank = f"Tool / Vendor {primary.vendor_position}"
            elif primary.source in {"supplier", "details"}:
                rank = f"Bom {primary.bom_position} / Vendor {primary.vendor_position}"
            else:
                rank = f"Bom {primary.bom_position} / {primary.source}"
            option_text = "; ".join(
                dict.fromkeys(usage.product for usage in usages if usage.product)
            )
            spans = [
                (parts, "strong"),
                (" | ", "muted"),
                (suppliers, None),
                (
                    f" | {rank}" + (f" | {age}" if age else ""),
                    "muted",
                ),
            ]
            if option_text and not verbose:
                spans.extend([(" | ", "muted"), (option_text, None)])
            summary = "".join(text for text, _ in spans)
            if not verbose:
                summary = _fit_line(summary, columns)
            # Fit visible text before adding ANSI codes; shared fitting handles
            # wide Chinese characters without truncating the URL below it.
            rendered = []
            for text, tone in spans:
                fragment, summary = summary[: len(text)], summary[len(text) :]
                if fragment:
                    rendered.append(styled(fragment, tone))
            print("".join(rendered))
            print(styled(candidate.url, "url"))
            if verbose:
                for usage in usages:
                    print(
                        f"  BOM #{usage.bom_position}, vendor {usage.vendor_position}: "
                        f"{usage.part_id} — {usage.supplier}"
                    )
                    print(f"    Need: {usage.component}; {usage.spec}; qty {usage.qty}")
                    if usage.product:
                        print(f"    {usage.product}")
    if verbose:
        print(styled(f"History: {state_path}", "muted"))


def _read_key(fd: int) -> str:
    decoder = codecs.getincrementaldecoder(sys.stdin.encoding or "utf-8")("replace")

    def character() -> str:
        while chunk := os.read(fd, 1):
            if text := decoder.decode(chunk):
                return text
        return ""

    key = character()
    if key != "\x1b" or not select.select([fd], [], [], 0.05)[0]:
        return key
    # Arrow/function keys send an escape sequence; only a bare Esc quits.
    following = character()
    if following == "\x1b":
        return following
    if following in {"[", "O"}:
        while select.select([fd], [], [], 0.05)[0]:
            if "@" <= character() <= "~":
                break
    return "special"


def open_round(selected: dict[str, Candidate]) -> None:
    """Open one listed URL per keypress when running in a terminal."""
    if not selected or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    fd = sys.stdin.fileno()
    settings = termios.tcgetattr(fd)

    def redraw(message: str) -> None:
        columns = max(1, shutil.get_terminal_size(fallback=(100, 24)).columns - 1)
        print("\r" + _fit_line(message, columns).ljust(columns), end="", flush=True)

    print()
    try:
        tty.setcbreak(fd)
        for index, candidate in enumerate(selected.values(), 1):
            label = PLATFORMS[candidate.platform][0]
            prompt = f"Open {index}/{len(selected)} · {label} · any key (q/Esc quits): "
            redraw(prompt)
            if _read_key(fd) in {"", "q", "Q", "\x1b", "\x04"}:
                break
            opened = webbrowser.open(candidate.url, new=2, autoraise=False)
            redraw(prompt + ("opened" if opened else "failed; use the URL above"))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, settings)
        print()


@locked_main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state", type=Path, default=STATE_FILE, help="review history JSON path"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show full BOM specifications and history path",
    )
    args = parser.parse_args(argv)
    try:
        candidates = collect_candidates(
            load_bom_rows(),
            {lang: load_sourcing_data(path) for lang, path in VENDOR_FILES.items()},
            load_optional_tools(),
        )
        with history_lock(args.state) as state_path:
            state = load_state(state_path)
            now = datetime.now(timezone.utc)
            selected = select_round(candidates, state, now)
            print_round(selected, state, now, state_path, verbose=args.verbose)
            sys.stdout.flush()
            # Record every shown URL before optional browser interaction.
            save_state(state_path, record_round(selected, state, now))
        open_round(selected)
        return 0
    except (BuildError, OSError) as exc:
        print(f"hw-url: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
