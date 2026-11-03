r"""Paid, opt-in provider checks using local production session snapshots.

Run with --help. No actions in model replies are dispatched. Credentials are
resolved by the production registry and never written to the report. Reports
contain raw replies and session-derived content; keep them local like the logs.
Discovery and inference require --live; audit is entirely offline. Model IDs
are explicit, so the harness never silently substitutes a different model.

Synthetic live text, schema, image, tool and ten-turn cache checks::

    python scripts/provider_e2e.py --phase chat --live \
        --models openai/gpt-6.1-terra deepseek/deepseek-flash --output data/providers

Offline session accounting and cross-adapter serialization::

    python scripts/provider_e2e.py --phase audit --sessions SESSION_ID --output data/providers
    python scripts/provider_e2e.py --phase serialize --sessions SESSION_ID \
        --models openai/gpt-6.1-terra --output data/providers

Live session transfer additionally requires --share-session-data. A nonzero
exit status indicates at least one failed check in the current invocation.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import time
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from functools import cache
from hashlib import sha256
from pathlib import Path

from physiclaw.common import paths
from physiclaw.common.text import append_text
from physiclaw.contract.dto import (
    AssistantMessage,
    FinishReason,
    ImageBlock,
    OutputResultMessage,
    SystemMessage,
    TextBlock,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from physiclaw.contract.structured import OutputSpec
from physiclaw.contract.turn import TurnMetadata
from physiclaw.contract.wire import image_ref
from physiclaw.provider.registry import (
    in_process_provider_ids,
    make_chat_provider,
    make_decision_provider,
)


def rows(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@cache
def encoded_image(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def restore_images(value, session: Path):
    if isinstance(value, list):
        return [restore_images(v, session) for v in value]
    if not isinstance(value, dict):
        return value
    ref = image_ref(value)
    if ref is None or ref.startswith(("http://", "https://", "data:")):
        return {k: restore_images(v, session) for k, v in value.items()}
    path = (session / ref).resolve()
    if not path.is_relative_to((session / "images").resolve()):
        raise ValueError("image outside the session image directory")
    mime = {".png": "image/png", ".webp": "image/webp"}.get(path.suffix, "image/jpeg")
    data = encoded_image(path)
    if value["type"] == "image":
        return {**value, "source": {"type": "base64", "media_type": mime, "data": data}}
    url = f"data:{mime};base64,{data}"
    return {
        **value,
        "image_url": url if value["type"] == "input_image" else {"url": url},
    }


def content_dto(content):
    if isinstance(content, str):
        return content
    result = []
    for block in content or []:
        if block.get("type") in {"text", "input_text", "output_text"}:
            result.append(TextBlock(block["text"]))
        elif block.get("type") in {"image_url", "input_image"}:
            url = block["image_url"]
            url = url if isinstance(url, str) else url["url"]
            if not url.startswith("data:"):
                raise ValueError("only local restored images are supported")
            head, data = url.split(",", 1)
            result.append(ImageBlock(head[5:].split(";")[0], data))
        else:
            raise ValueError(f"unsupported corpus content: {block.get('type')}")
    return result


def text_of(content) -> str:
    return (
        content
        if isinstance(content, str)
        else "\n".join(b.text for b in content if isinstance(b, TextBlock))
    )


@cache
def session_records(session: Path):
    return rows(session / "model.jsonl")


@cache
def session_output_spec(session: Path):
    """Compile each immutable recorded schema once per replay run."""
    from physiclaw.agent.engine.output import ActionCatalog

    header = next(r for r in session_records(session) if r["kind"] == "session_start")
    schema = header["output_schema"]
    tools = []
    for branch in schema["properties"]["action"]["oneOf"]:
        ((name, arguments),) = branch["properties"].items()
        arguments = dict(arguments)
        tools.append(
            {
                "name": name,
                "description": arguments.pop("description", ""),
                "input_schema": arguments,
            }
        )
    catalog = ActionCatalog.compile(
        tools, [], max_scratchpad=schema["properties"]["scratchpad"]["maxLength"]
    )
    if catalog.schema != schema:
        raise ValueError("recorded output schema no longer matches the decoder")
    return catalog.spec()


def load_snapshot(session: Path, turn: int, *, native: bool, record=None):
    """Rebuild logical structured history; target adapter owns the wire shape.

    Historical reasoning/signatures belong to the original model and are not
    portable. Live continuation checks below exercise native opaque-state replay.
    """
    records = session_records(session)
    if record is None:
        record = next(
            r for r in records if r["kind"] == "request" and r["turn"] == turn
        )
    wire = restore_images(record["messages"], session)
    history = []
    replies = {}
    previous = None
    for entry in wire:
        if entry.get("type") == "reasoning":
            continue
        role = entry.get("role")
        content = content_dto(entry.get("content", ""))
        if role == "system":
            history.append(SystemMessage(text_of(content)))
        elif role == "assistant":
            calls = entry.get("tool_calls", [])
            value = (
                json.loads(calls[0]["function"]["arguments"])
                if calls
                else json.loads(text_of(content))
            )
            ident = calls[0]["id"] if calls else f"replay_{len(history)}"
            previous = AssistantMessage(
                json.dumps(value, ensure_ascii=False) if native else "",
                [] if native else [ToolCall(ident, "submit_turn", value)],
                FinishReason.STOP if native else FinishReason.TOOL_CALLS,
            )
            replies[ident] = previous
            history.append(previous)
        elif role == "tool":
            reply = replies[entry["tool_call_id"]]
            history.append(
                OutputResultMessage(
                    reply, content, is_superseded="(superseded " in text_of(content)
                )
            )
        elif role == "user":
            text = text_of(content)
            if text.startswith("Structured output result") and previous is not None:
                # Retain block boundaries while removing the transport label.
                if isinstance(content, list):
                    first = content[0].text.partition(":")[2].lstrip("\n")
                    content = ([TextBlock(first)] if first else []) + content[1:]
                else:
                    content = content.partition(":")[2].lstrip("\n")
                history.append(
                    OutputResultMessage(
                        previous, content, is_superseded="(superseded " in text
                    )
                )
            elif not text.startswith("<turn_metadata>"):
                history.append(UserMessage(content))
        else:
            raise ValueError(f"unsupported corpus message: {entry.get('type', role)}")
    # These are already-built wire requests. Remove exactly the original
    # adapter's trailing output instruction before the target adapter adds its
    # own, avoiding duplicated/conflicting tool-vs-native directions.
    spec = session_output_spec(session)
    for i, message in enumerate(history):
        if isinstance(message, SystemMessage):
            for instruction in (
                f"Submit one {spec.name} through the {spec.function_name} function.",
                f"Return one {spec.name} JSON object matching the response schema.",
            ):
                suffix = "\n\n" + instruction + " Do not include standalone prose."
                if message.content.endswith(suffix):
                    history[i] = SystemMessage(message.content[: -len(suffix)])
                    break
            break
    return history, spec


def request_cache_facts(payload):
    """Local byte fingerprints are diagnostics; only API usage proves a hit."""

    def marker_keys(value):
        # Only wire content blocks carry our cache annotations. A tool schema
        # can legitimately define a property named cache_control; that is part
        # of the prompt and must remain in its fingerprint.
        if value.get("type") in ("text", "input_text", "tool_result"):
            return {"cache_control", "prompt_cache_breakpoint"}
        return set()

    def clean(value):
        if isinstance(value, dict):
            ignored = marker_keys(value)
            return {k: clean(v) for k, v in value.items() if k not in ignored}
        return [clean(v) for v in value] if isinstance(value, list) else value

    def encode(value):
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")

    def marked(value):
        if isinstance(value, dict):
            return any(k in value for k in marker_keys(value)) or any(
                marked(v) for v in value.values()
            )
        return any(marked(v) for v in value) if isinstance(value, list) else False

    configuration = {
        k: v for k, v in payload.items() if k not in {"messages", "input", "system"}
    }
    state = sha256(encode(clean(configuration)))
    state.update(b"\x00" + encode(clean(payload.get("system"))))
    prefixes = [
        {
            "index": -1,
            "hash": state.hexdigest(),
            "marked": marked(payload.get("system")),
        }
    ]
    for i, entry in enumerate(payload.get("input", payload.get("messages", []))):
        state.update(b"\x00" + encode(clean(entry)))
        prefixes.append(
            {"index": i, "hash": state.hexdigest(), "marked": marked(entry)}
        )
    return {
        "request_hash": sha256(encode(payload)).hexdigest(),
        "prefixes": prefixes,
        "cache_options": payload.get("prompt_cache_options"),
        "tool_choice": payload.get("tool_choice"),
    }


def raw_usage(raw: dict, provider_id: str) -> Usage:
    """Independent accounting oracle; no adapter parsing helpers."""

    def reported(value):
        if type(value) is int:
            return value if value >= 0 else None
        if isinstance(value, str):
            try:
                parsed = int(value)
                return parsed if parsed >= 0 else None
            except ValueError:
                pass
        return None

    def count(value):
        return reported(value) or 0

    def mapping(value):
        return value if isinstance(value, dict) else {}

    u = mapping(raw.get("usage"))
    if provider_id == "anthropic":
        read = count(u.get("cache_read_input_tokens"))
        write = count(u.get("cache_creation_input_tokens"))
        return Usage(
            count(u.get("input_tokens")) + read + write,
            count(u.get("output_tokens")),
            read,
            write,
        )
    # The adapter determines the API, not incidental extra keys in usage.
    responses_api = provider_id == "openai"
    details = mapping(
        u.get("input_tokens_details" if responses_api else "prompt_tokens_details")
    )
    outputs = mapping(
        u.get("output_tokens_details" if responses_api else "completion_tokens_details")
    )
    prompt = count(u.get("input_tokens" if responses_api else "prompt_tokens"))
    output = count(u.get("output_tokens" if responses_api else "completion_tokens"))
    reasoning = count(outputs.get("reasoning_tokens"))
    cached = count(details.get("cached_tokens"))
    writes = details.get("cache_write_tokens")
    if not responses_api:
        cached = (
            cached
            or count(u.get("cached_tokens"))
            or count(u.get("prompt_cache_hit_tokens"))
        )
        writes = details.get(
            "cache_write_tokens", details.get("cache_creation_input_tokens")
        )
    if provider_id == "google":
        source = [
            reported(u.get(key))
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        ]
        if all(value is not None for value in source):
            missing = max(0, source[2] - source[0] - source[1])
            output += missing
            reasoning = max(reasoning, missing)
    return Usage(
        prompt,
        output,
        cached,
        count(writes),
        reasoning,
    )


def audit(session: Path) -> dict:
    records = rows(session / "model.jsonl")
    events = rows(session / "events.jsonl")
    header = next(r for r in records if r["kind"] == "session_start")
    pid = header["provider"]
    accounts = [e for e in events if e["event"] == "usage" and e["call"] == "turn"]
    by_id = {e["response_id"]: e for e in accounts}
    total = Counter()
    mismatches = []
    responses = [r for r in records if r["kind"] == "response"]
    for record in responses:
        raw = record["raw"]
        usage = raw_usage(raw, pid).buckets()
        total.update(usage)
        event = by_id.get(raw["id"])
        if event is None or any(event.get(k) != v for k, v in usage.items()):
            mismatches.append(
                {"response_id": raw["id"], "expected": usage, "actual": event}
            )
    return {
        "session": session.name,
        "provider": pid,
        "model": header["model"],
        "responses": len(responses),
        "usage_events": len(accounts),
        "mismatches": mismatches,
        "totals": dict(total),
        "hit_rate": total["cache_read"] / total["input"] if total["input"] else None,
        "duplicate_response_ids": len(accounts) - len(by_id),
    }


class Sink:
    def __init__(self):
        self.events = []

    def write(self, event):
        self.events.append(event)


class Runner:
    def __init__(self, args):
        self.args = args
        self.output = Path(args.output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.failed = False
        self.exhausted_providers = set()
        self.previous_requests = {}

    def record(self, value):
        self.failed = self.failed or value.get("status") in {"fail", "error"}
        append_text(
            self.output / "results.jsonl",
            json.dumps(value, ensure_ascii=False, default=str) + "\n",
        )
        compact = {
            k: v
            for k, v in value.items()
            if k not in {"raw", "event", "request", "reply", "value"}
        }
        print(json.dumps(compact, ensure_ascii=False, default=str), flush=True)

    async def call(
        self,
        provider,
        name,
        history,
        *,
        spec=None,
        thinking="off",
        tools=None,
        metadata=None,
        details=None,
    ):
        before = len(provider.usage_sink.events)
        started = time.perf_counter()
        result = {
            "model": provider.ref,
            "case": name,
            "thinking": thinking,
            **(details or {}),
        }

        def observe(payload):
            facts = request_cache_facts(payload)
            previous = self.previous_requests.get(provider.ref)
            facts["identical_to_previous"] = bool(
                previous and previous["request_hash"] == facts["request_hash"]
            )
            current_hashes = {p["hash"] for p in facts["prefixes"]}
            facts["preserved_previous_markers"] = (
                [
                    p["index"]
                    for p in previous["prefixes"]
                    if p["marked"] and p["hash"] in current_hashes
                ]
                if previous
                else []
            )
            result["request"] = facts
            self.previous_requests[provider.ref] = facts

        snapshot = deepcopy(history)
        try:
            async with asyncio.timeout(self.args.timeout):
                if spec is None:
                    reply = await provider.chat(
                        history,
                        tools or [],
                        thinking=thinking,
                        metadata=metadata,
                        on_request=observe,
                    )
                else:
                    completion = await provider.generate_structured(
                        history,
                        spec,
                        thinking=thinking,
                        metadata=metadata,
                        on_request=observe,
                    )
                    reply = completion.reply
                    result["value"] = str(completion.value)
                    result["failure"] = (
                        asdict(completion.failure) if completion.failure else None
                    )
            result.update(
                status="pass",
                raw=reply.raw,
                usage=reply.usage.buckets(),
                response_id=reply.response_id,
            )
            expected = raw_usage(reply.raw, provider.PROVIDER_ID)
            checks = {
                "raw_usage_matches": expected == reply.usage,
                "input_buckets_sum": reply.usage.new_tokens
                + reply.usage.cached_tokens
                + reply.usage.cache_creation_tokens
                == reply.usage.prompt_tokens,
                "reasoning_in_output": reply.usage.reasoning_tokens
                <= reply.usage.completion_tokens,
                "usage_reported": reply.usage.prompt_tokens > 0
                and reply.usage.completion_tokens > 0,
                "history_unchanged": snapshot == history,
                "structured_valid": not result.get("failure"),
                "complete": reply.finish_reason
                not in {
                    FinishReason.LENGTH,
                    FinishReason.ERROR,
                    FinishReason.CONTENT_FILTER,
                },
            }
            result["checks"] = checks
            if not all(checks.values()):
                result["status"] = "fail"
        except Exception as exc:
            reply = None
            result.update(
                status="error",
                error=type(exc).__name__,
                detail=str(exc)[:800],
                http_status=getattr(exc, "status_code", None),
            )
            if (
                type(exc).__name__ == "ProviderPermanentError"
                and getattr(exc, "status_code", None) == 429
            ):
                self.exhausted_providers.add(provider.PROVIDER_ID)
                result["quota_exhausted"] = True
        accounts = provider.usage_sink.events[before:]
        result["event_count"] = len(accounts)
        if len(accounts) != 1:
            result["status"] = "fail"
        else:
            result["event"] = accounts[0]
            if reply is not None and any(
                accounts[0][k] != v for k, v in reply.usage.buckets().items()
            ):
                result["status"] = "fail"
                result["accounting_mismatch"] = True
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        self.record(result)
        return reply, result

    async def chat(self, ref):
        pid, model = ref.split("/", 1)
        provider = make_chat_provider(pid, model, usage_sink=Sink())
        provider.max_output_tokens = self.args.max_output_tokens
        catalog = self.output / "openrouter-models.json"
        if pid == "openrouter" and catalog.exists():
            provider._catalog = {m["id"]: m for m in rows_json(catalog)}
        try:
            if self.args.contract_only:
                await self.contract(provider)
                return
            if not self.args.replay_only and not self.args.cache_only:
                await self.smoke(provider)
            if self.args.smoke_only:
                return
            if not self.args.sessions:
                await self.cache(provider)
                return
            thinking = (
                None
                if self.args.replay_thinking == "default"
                else self.args.replay_thinking
            )
            for sid in self.args.sessions:
                session = paths.sessions_dir() / sid
                requests = [
                    r for r in session_records(session) if r["kind"] == "request"
                ]
                selected = (
                    requests
                    if self.args.all_turns
                    else [
                        next(r for r in requests if r["turn"] == turn)
                        for turn in self.args.turns
                    ]
                )
                previous = None
                for index, record in enumerate(selected):
                    turn = record["turn"]
                    history, spec = load_snapshot(
                        session,
                        turn,
                        native=provider.structured_transport == "native",
                        record=record,
                    )
                    if previous is None:
                        kind = "initial"
                    elif previous is record:
                        kind = "repeat"
                    elif len(record["messages"]) < len(previous["messages"]) * 0.7:
                        kind = "compaction"
                    else:
                        kind = "growing"
                    repeats = (
                        2
                        if self.args.all_turns and turn in self.args.repeat_turns
                        else 1
                    )
                    for repeat in range(repeats):
                        await self.call(
                            provider,
                            f"replay/{sid}/{turn}/{index}/{repeat}",
                            history,
                            spec=spec,
                            thinking=thinking,
                            metadata=TurnMetadata(
                                f"replay-{sid}", turn, None, max(1, 300 - turn)
                            ),
                            details={
                                "source_session": sid,
                                "source_turn": turn,
                                "source_attempt": record.get("attempt"),
                                "replay_kind": "repeat" if repeat else kind,
                            },
                        )
                        if pid in self.exhausted_providers:
                            return
                    previous = record
        except Exception as exc:
            self.record(
                {
                    "model": ref,
                    "case": "harness",
                    "status": "error",
                    "error": type(exc).__name__,
                    "detail": str(exc)[:800],
                }
            )
        finally:
            await provider.aclose()

    async def contract(self, provider):
        """Compile public tool definitions without constructing real hardware."""
        from unittest.mock import MagicMock

        from mcp.server.mcpserver import MCPServer

        from physiclaw.agent.engine.output import ActionCatalog
        from physiclaw.core.server.tools import register

        server = MCPServer("provider-contract-test")
        register(server, MagicMock())
        tools = [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.input_schema,
            }
            for tool in await server.list_tools()
        ]
        tools.append(
            {
                "name": "wait",
                "description": "Wait a specified number of seconds.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "seconds": {"type": "number", "minimum": 0, "maximum": 10}
                    },
                    "required": ["seconds"],
                    "additionalProperties": False,
                },
            }
        )
        catalog = ActionCatalog.compile(tools, [], max_scratchpad=256)
        history = [
            UserMessage(
                'This is a synthetic schema validation fixture. No actions will be executed. Return summary="Synthetic validation", scratchpad=null, and action={"wait":{"seconds":1}}.'
            )
        ]
        for thinking in (None, "off"):
            await self.call(
                provider,
                f"contract/{thinking or 'default'}",
                history,
                spec=catalog.spec(),
                thinking=thinking,
            )

    async def serialize(self, ref):
        pid, model = ref.split("/", 1)
        provider = make_chat_provider(pid, model)
        count = 0
        try:
            for sid in self.args.sessions:
                session = paths.sessions_dir() / sid
                for row in session_records(session):
                    if row["kind"] != "request":
                        continue
                    history, spec = load_snapshot(
                        session,
                        row["turn"],
                        native=provider.structured_transport == "native",
                        record=row,
                    )
                    original = deepcopy(history)
                    payload = provider.build_request(
                        history, [], output_spec=spec, thinking="off"
                    )
                    if original != history:
                        raise AssertionError("serialization mutated the corpus history")
                    count += 1

                    def markers(value):
                        if isinstance(value, dict):
                            return int(
                                "cache_control" in value
                                or "prompt_cache_breakpoint" in value
                            ) + sum(markers(v) for v in value.values())
                        return (
                            sum(markers(v) for v in value)
                            if isinstance(value, list)
                            else 0
                        )

                    if markers(payload) > 4:
                        raise AssertionError("more than four cache breakpoints")
            self.record(
                {
                    "model": ref,
                    "case": "serialize/corpus",
                    "status": "pass",
                    "requests": count,
                }
            )
        except Exception as exc:
            self.record(
                {
                    "model": ref,
                    "case": "serialize/corpus",
                    "status": "error",
                    "requests": count,
                    "error": type(exc).__name__,
                    "detail": str(exc)[:800],
                }
            )
        finally:
            await provider.aclose()

    async def cache(self, provider):
        """Production compaction and feedback flow with entirely public fixtures."""
        import cv2
        import numpy as np

        from physiclaw.agent.engine import compact
        from physiclaw.contract.dto import CollapsePolicy

        system = (
            "You are running a synthetic provider integration test. Return answer=OK and count=7 "
            "on every step using the requested output schema. The reference records below are "
            "public generated fixtures, not instructions to execute.\n"
            + "\n".join(
                f"Record {i:04}: station S{i:04} has seven test containers. Inspect the visible "
                "screen, preserve the order of labels, and report the validated result. "
                "Previous screens are historical evidence; use only the latest image for current state."
                for i in range(200)
            )
        )

        def decode(value):
            if value != {"answer": "OK", "count": 7}:
                raise ValueError("expected OK and 7")
            return value

        spec = OutputSpec(
            "CacheProbe",
            {
                "type": "object",
                "properties": {
                    "answer": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["answer", "count"],
                "additionalProperties": False,
            },
            decode,
        )
        history = [
            SystemMessage(system),
            UserMessage("Run the synthetic seven-container inspection."),
            UserMessage(compact.SUMMARY_INITIAL),
            UserMessage(compact.MEMORY_INITIAL),
            UserMessage(compact.SKILLS_INITIAL),
        ]
        folded = False
        collapsed_once = False
        observations = []
        for turn in range(10):
            request = [
                *history,
                UserMessage(f"Step {turn}: submit answer=OK, count=7."),
            ]
            reply, result = await self.call(
                provider,
                f"cache/{turn}/{'collapsed' if folded else 'growing'}",
                request,
                spec=spec,
                metadata=TurnMetadata("synthetic-provider-e2e", turn, None, 20 - turn),
            )
            if reply is None or result.get("failure"):
                break
            observations.append(reply.usage.buckets())
            reply.context_calls = [
                ToolCall(
                    f"note-{turn}",
                    "note",
                    {"summary": f"Completed synthetic step {turn}"},
                )
            ]
            frame = np.full((320, 320, 3), (turn * 23) % 255, dtype=np.uint8)
            cv2.putText(
                frame,
                str(turn),
                (120, 180),
                cv2.FONT_HERSHEY_SIMPLEX,
                2,
                (0, 0, 255),
                3,
            )
            ok, encoded = cv2.imencode(".png", frame)
            assert ok
            content = [
                TextBlock(
                    f'Screen step {turn}: seven containers confirmed.\nid [kind] "label" [left,top,right,bottom] conf\n0 [text] "Continue" [0.1,0.6,0.9,0.8] 0.99'
                ),
                ImageBlock("image/png", base64.b64encode(encoded).decode()),
            ]
            if provider.ref in self.args.text_only_models:
                content = [block for block in content if isinstance(block, TextBlock)]
            history.extend([reply, OutputResultMessage(reply, content)])
            compact.drop_stale_screens(history)
            folded = compact.collapse_old_turns(
                history,
                policy=CollapsePolicy(first_at=6, keep=2, interval=4),
                collapsed_once=collapsed_once,
            )
            collapsed_once = collapsed_once or folded
        total = Counter()
        for usage in observations:
            total.update(usage)
        self.record(
            {
                "model": provider.ref,
                "case": "cache/summary",
                "status": "fail"
                if len(observations) != 10
                else "pass"
                if total["cache_read"]
                else "unobserved",
                "calls": len(observations),
                "usage": dict(total),
                "hit_rate": total["cache_read"] / total["input"]
                if total["input"]
                else None,
            }
        )

    async def smoke(self, provider):
        history = [
            SystemMessage("Follow the user's instructions precisely."),
            UserMessage("Reply with exactly OK."),
        ]
        reply, result = await self.call(provider, "text", history)
        if reply is None:
            return
        if reply.content.strip() != "OK":
            self.record(
                {
                    "model": provider.ref,
                    "case": "text/assert",
                    "status": "fail",
                    "reply": reply.content,
                }
            )

        def decode(value):
            if (
                not isinstance(value, dict)
                or set(value) != {"answer", "count"}
                or value != {"answer": "OK", "count": 7}
            ):
                raise ValueError("expected exact semantic values")
            return value

        spec = OutputSpec(
            "Probe",
            {
                "type": "object",
                "properties": {
                    "answer": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["answer", "count"],
                "additionalProperties": False,
            },
            decode,
        )
        for thinking in ("off", "low"):
            await self.call(
                provider,
                f"structured/{thinking}",
                [UserMessage('Return answer="OK" and count=7.')],
                spec=spec,
                thinking=thinking,
            )

        # Real image pixels, with no color-name hint in text: detect providers
        # that silently ignore images as well as outright protocol rejections.
        import cv2
        import numpy as np

        frame = np.zeros((320, 320, 3), dtype=np.uint8)
        frame[:] = (0, 0, 255)
        ok, encoded = cv2.imencode(".png", frame)
        assert ok
        image = ImageBlock("image/png", base64.b64encode(encoded).decode())
        if provider.ref in self.args.text_only_models:
            reply = None
            self.record(
                {
                    "model": provider.ref,
                    "case": "vision",
                    "status": "skip",
                    "reason": "Explicitly declared text-only model",
                }
            )
        else:
            reply, result = await self.call(
                provider,
                "vision",
                [
                    UserMessage(
                        [
                            TextBlock(
                                "What is the dominant color in this image? Reply with exactly one lowercase English color word."
                            ),
                            image,
                        ]
                    )
                ],
            )
        if reply is not None and reply.content.strip().lower().rstrip(".") != "red":
            self.record(
                {
                    "model": provider.ref,
                    "case": "vision/assert",
                    "status": "fail",
                    "reply": reply.content,
                }
            )

        tool = {
            "name": "probe_echo",
            "description": "A test echo function. Call it exactly once with value=7 when asked.",
            "input_schema": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        }
        history = [
            SystemMessage(
                "Use probe_echo exactly once when the user asks. After its result, reply with exactly DONE. Never repeat the call."
            ),
            UserMessage("Call probe_echo with value=7."),
        ]
        reply, result = await self.call(
            provider, "tool/start", history, tools=[tool], thinking="low"
        )
        if (
            reply is not None
            and len(reply.tool_calls) == 1
            and reply.tool_calls[0].arguments == {"value": 7}
        ):
            history.extend(
                [
                    reply,
                    ToolResultMessage(
                        reply.tool_calls[0].id,
                        [TextBlock("Echo result: 7. The probe succeeded.")]
                        + (
                            []
                            if provider.ref in self.args.text_only_models
                            else [image]
                        ),
                    ),
                ]
            )
            last, result = await self.call(
                provider,
                "tool/continuation-image",
                history,
                tools=[tool],
                thinking="low",
            )
            if last is not None and (
                last.tool_calls or last.content.strip().rstrip(".") != "DONE"
            ):
                self.record(
                    {
                        "model": provider.ref,
                        "case": "tool/continuation/assert",
                        "status": "fail",
                        "reply": last.content,
                    }
                )
        else:
            self.record(
                {"model": provider.ref, "case": "tool/start/assert", "status": "fail"}
            )

    async def decision(self, ref):
        from dataclasses import replace

        import cv2
        import numpy as np

        from physiclaw.contract.decision import (
            ActionCandidate,
            DecisionContext,
            DecisionElement,
            DecisionFrame,
            DecisionQuestion,
            DecisionRequest,
        )

        pid, model = ref.split("/", 1)
        provider = make_decision_provider(pid, model)
        responses = []

        async def capture(response):
            await response.aread()
            try:
                responses.append(response.json())
            except ValueError:
                responses.append({})

        provider._client.event_hooks["response"].append(capture)
        request = DecisionRequest(
            "provider-e2e",
            DecisionContext(
                observation_id="observation-e2e",
                facts={"goal": "Select the visible Continue button."},
                ocr=(
                    DecisionElement(
                        "observation-e2e:0", (0.1, 0.6, 0.9, 0.8), "Continue"
                    ),
                ),
            ),
            (
                DecisionQuestion(
                    "next_action",
                    "Choose the action that taps the visible Continue button.",
                    (
                        ActionCandidate("YIELD", "Execute no action."),
                        ActionCandidate(
                            "tap",
                            "Tap Continue",
                            "tap",
                            {"bbox": [0.1, 0.6, 0.9, 0.8]},
                            "observation-e2e:0",
                        ),
                    ),
                ),
            ),
        )
        frame = np.full((320, 320, 3), 255, dtype=np.uint8)
        cv2.rectangle(frame, (32, 192), (288, 256), (0, 150, 0), -1)
        cv2.putText(
            frame,
            "Continue",
            (70, 235),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (255, 255, 255),
            2,
        )
        ok, image = cv2.imencode(".jpg", frame)
        assert ok
        try:
            names = (
                ("cache", "cache/repeat")
                if self.args.cache_only
                else ("text", "text/repeat", "image", "batch", "cache", "cache/repeat")
            )
            for name in names:
                current = request
                if name.startswith("cache"):
                    current = replace(
                        request,
                        context=replace(
                            request.context,
                            facts={
                                "reference_records": "\n".join(
                                    f"Station {i:04}: seven containers, quality check passed, Continue advances to the next station."
                                    for i in range(150)
                                ),
                                "goal": "Select the visible Continue button.",
                            },
                        ),
                    )
                if name == "image":
                    current = replace(
                        request,
                        context=replace(
                            request.context,
                            frame=DecisionFrame(
                                "observation-e2e",
                                image.tobytes(),
                                "image/jpeg",
                                320,
                                320,
                            ),
                        ),
                    )
                if name == "batch":
                    current = replace(
                        request,
                        questions=(
                            *request.questions,
                            replace(request.questions[0], id="verify"),
                        ),
                    )
                started = time.perf_counter()
                try:
                    result = await provider.evaluate(
                        current,
                        deadline=asyncio.get_running_loop().time() + self.args.timeout,
                    )
                    valid = all(a.selected_id == "tap" for a in result.answers.values())
                    raw = responses[-1] if responses else {}
                    usage = raw.get("usage") or {}
                    details = usage.get("input_tokens_details") or {}
                    expected = {
                        "input_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens"),
                        "cached_tokens": details.get("cached_tokens"),
                        "cache_creation_tokens": details.get("cache_write_tokens"),
                    }
                    actual_usage = asdict(result.usage)
                    usage_matches = actual_usage == expected
                    valid = valid and usage_matches
                    self.record(
                        {
                            "model": ref,
                            "case": f"decision/{name}",
                            "status": "pass" if valid else "fail",
                            "raw": raw,
                            "usage_matches": usage_matches,
                            "result": {
                                "request_id": result.request_id,
                                "provider_id": result.provider_id,
                                "model_id": result.model_id,
                                "usage": actual_usage,
                                "answers": {
                                    key: {
                                        "selected_id": answer.selected_id,
                                        "confidence": answer.confidence,
                                        "probabilities": dict(answer.probabilities)
                                        if answer.probabilities is not None
                                        else None,
                                    }
                                    for key, answer in result.answers.items()
                                },
                            },
                            "elapsed_ms": round((time.perf_counter() - started) * 1000),
                        }
                    )
                except Exception as exc:
                    self.record(
                        {
                            "model": ref,
                            "case": f"decision/{name}",
                            "status": "error",
                            "error": type(exc).__name__,
                            "detail": str(exc)[:800],
                        }
                    )
        finally:
            await provider.aclose()


def rows_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


async def main(args):
    if args.timeout <= 0 or args.max_output_tokens < 1:
        raise SystemExit("Timeout and output-token budget must be positive.")
    if args.phase == "chat":
        if args.replay_only and not args.sessions:
            raise SystemExit("--replay-only requires explicit --sessions.")
        if args.sessions and (args.smoke_only or args.cache_only or args.contract_only):
            raise SystemExit("Synthetic-only modes do not accept --sessions.")
        if args.sessions and not args.share_session_data:
            raise SystemExit(
                "Live session transfer requires --share-session-data. It sends private histories and screenshots to every named provider."
            )
    runner = Runner(args)
    if args.phase == "audit":
        if not args.sessions:
            raise SystemExit("Audit requires --sessions.")
        report = [audit(paths.sessions_dir() / sid) for sid in args.sessions]
        (runner.output / "audit.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, indent=2))
        return int(
            any(
                r["mismatches"]
                or r["duplicate_response_ids"]
                or r["responses"] != r["usage_events"]
                for r in report
            )
        )
    if args.phase == "serialize":
        if not args.sessions or not args.models:
            raise SystemExit("Serialization requires --sessions and --models.")
        for ref in args.models:
            await runner.serialize(ref)
        return int(runner.failed)
    if not args.live:
        raise SystemExit("Network phases require --live (paid API calls).")
    if args.phase == "discover":

        async def discover(pid):
            provider = make_chat_provider(pid, "discovery")
            try:
                async with asyncio.timeout(args.timeout):
                    models = await provider.list_models()
                (runner.output / f"{pid}-models.json").write_text(
                    json.dumps(models, indent=2), encoding="utf-8"
                )
                runner.record(
                    {
                        "model": pid,
                        "case": "discovery",
                        "status": "pass",
                        "count": len(models),
                    }
                )
            except Exception as exc:
                runner.record(
                    {
                        "model": pid,
                        "case": "discovery",
                        "status": "error",
                        "error": type(exc).__name__,
                        "detail": str(exc)[:500],
                    }
                )
            finally:
                await provider.aclose()

        await asyncio.gather(*(discover(p) for p in in_process_provider_ids()))
        return int(runner.failed)
    refs = args.models
    if not refs:
        raise SystemExit("Supply explicit --models provider/model references.")
    # One worker per vendor avoids cross-model bursts against a single quota.
    groups = {}
    for ref in refs:
        groups.setdefault(ref.split("/", 1)[0], []).append(ref)

    async def group(refs):
        for ref in refs:
            if ref.split("/", 1)[0] in runner.exhausted_providers:
                runner.record(
                    {
                        "model": ref,
                        "case": "quota/skip",
                        "status": "skip",
                        "reason": "Provider reported exhausted quota; account action required.",
                    }
                )
                continue
            if args.phase == "chat":
                await runner.chat(ref)
            else:
                await runner.decision(ref)

    await asyncio.gather(*(group(refs) for refs in groups.values()))
    return int(runner.failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--phase",
        choices=["audit", "discover", "serialize", "chat", "decision"],
        default="audit",
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--models", nargs="+", default=[])
    parser.add_argument("--text-only-models", nargs="+", default=[])
    parser.add_argument("--sessions", nargs="+", default=[])
    parser.add_argument("--share-session-data", action="store_true")
    parser.add_argument("--all-turns", action="store_true")
    parser.add_argument("--repeat-turns", nargs="*", type=int, default=[8])
    parser.add_argument(
        "--replay-thinking",
        choices=["default", "off", "low", "medium", "high"],
        default="off",
    )
    parser.add_argument(
        "--turns", nargs="+", type=int, default=[8, 8, 9, 10, 29, 30, 31]
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke-only", action="store_true")
    mode.add_argument("--replay-only", action="store_true")
    mode.add_argument("--cache-only", action="store_true")
    mode.add_argument("--contract-only", action="store_true")
    logging.basicConfig(level=logging.ERROR)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
