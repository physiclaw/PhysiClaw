"""Live provider replay through the real engine; recorded device actions only."""

import argparse
import asyncio
import base64
import hashlib
import json
import time
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path

from physiclaw.common import gesture_vocab as gv
from physiclaw.common import paths
from physiclaw.common.config import model_ref
from physiclaw.common.listing import LISTING_HEADER, decode_elements
from physiclaw.contract import wire
from physiclaw.contract.dto import ImageBlock
from physiclaw.contract.observation import OBSERVATION_META, view_metadata
from physiclaw.provider import make_chat_provider


def records(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


class Sink:
    def __init__(self, output):
        self.output = output
        self.events = []
        self.requests = []
        output.mkdir(parents=True, exist_ok=False)
        (output / "images").mkdir()

    def persist(self, mime, data):
        raw = base64.b64decode(data)
        name = f"images/{hashlib.sha256(raw).hexdigest()[:20]}.jpg"
        (self.output / name).write_bytes(raw)
        return name

    def default(self, value):
        if isinstance(value, ImageBlock):
            return {
                "type": "image",
                "file": self.persist(value.media_type, value.data_b64),
            }
        if is_dataclass(value):
            return asdict(value)
        return str(value)

    def emit(self, file, event):
        with (self.output / file).open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(event, default=self.default, ensure_ascii=False) + "\n"
            )

    def write(self, event):
        self.events.append(event)
        self.emit("events.jsonl", event)
        return True

    def write_native_request(self, turn, body, **metadata):
        body = wire.scrub_request(body, self.persist)
        self.requests.append(body)
        self.emit(
            "model.jsonl", {"kind": "request", "turn": turn, "body": body, **metadata}
        )

    def write_response(self, turn, raw, **metadata):
        self.emit(
            "model.jsonl", {"kind": "response", "turn": turn, "raw": raw, **metadata}
        )


class ReplayDevice:
    def __init__(self, directory, schemas):
        self.directory = directory
        names = {tool["name"] for tool in schemas}
        self.steps = [
            event
            for event in records(directory / "events.jsonl")
            if event.get("event") == "tool_result" and event.get("name") in names
        ]
        if not self.steps:
            raise ValueError("recording has no matching device transitions")
        self.index = 0
        self.current = None
        self.calls = []

    @staticmethod
    def matches(expected, name, arguments):
        if expected["name"] != name:
            return False
        original = expected.get("arguments", {})
        if name == "tap" and "bbox" in arguments and "bbox" in original:
            box, target = arguments["bbox"], original["bbox"]
            x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            return target[0] <= x <= target[2] and target[1] <= y <= target[3]
        return arguments == original

    def blocks(self, event):
        images = event.get("images", [])
        if len(images) != 1:
            raise ValueError("recorded observation requires exactly one image")
        text = event.get("text", "")
        prefix, separator, listing = text.partition(LISTING_HEADER)
        if not separator:
            raise ValueError("recorded observation has no element listing")
        listing = separator + listing
        decode_elements(listing)
        result = [{"type": "text", "text": prefix}] if prefix.strip() else []
        path = (self.directory / images[0]).resolve()
        if not path.is_relative_to(self.directory.resolve()):
            raise ValueError("image reference escapes session directory")
        image = path.read_bytes()
        result.append(
            {
                "type": "image",
                "mime_type": "image/jpeg",
                "data": base64.b64encode(image).decode(),
                "_meta": {
                    OBSERVATION_META: view_metadata(
                        image, listing, source="debug", observed_after=time.time()
                    )
                },
            }
        )
        result.append({"type": "text", "text": listing})
        return result

    async def call_tool(self, name, arguments, **_):
        self.calls.append({"name": name, "arguments": arguments})
        next_step = self.steps[self.index] if self.index < len(self.steps) else None
        if next_step and self.matches(next_step, name, arguments):
            blocks = self.blocks(next_step)
            self.current = next_step
            self.index += 1
            return blocks
        if name == "peek" and self.current is not None:
            return self.blocks(self.current)
        raise ValueError(
            "Recorded device has no matching transition; no state changed. Inspect the current view."
        )


def audit_requests(requests):
    violations = []
    for index, body in enumerate(requests):
        advertised = [tool["function"]["name"] for tool in body["tools"]]
        if advertised != ["submit_turn"]:
            violations.append(f"request {index}: wrong native tools")
        pending = set()
        seen = set()
        for message in body["messages"]:
            for call in message.get("tool_calls", []):
                call_id = call["id"]
                if not isinstance(call_id, str) or not call_id or call_id in seen:
                    violations.append(f"request {index}: invalid or reused envelope ID")
                if call["function"]["name"] != "submit_turn" or pending:
                    violations.append(f"request {index}: invalid envelope history")
                pending.add(call_id)
                seen.add(call_id)
            if message.get("role") == "tool":
                call_id = message["tool_call_id"]
                if call_id not in pending:
                    violations.append(f"request {index}: unmatched receipt")
                pending.discard(call_id)
        if pending:
            violations.append(f"request {index}: missing receipt")
    return {
        "requests": len(requests),
        "valid": bool(requests) and not violations,
        "violations": violations,
    }


async def blocked_local(_session, _arguments):
    raise ValueError(
        "This replay permits session state changes only; persistent writes and external actions are disabled."
    )


async def main(args):
    # Configuration (including credentials) was loaded above from the local home.
    # Import agent state only after selecting an empty data root: the approved
    # recording is the sole private context, and local files cannot be mutated.
    sink = Sink(args.output)
    paths.HOME = args.output.resolve() / "state"
    paths.LOG_DIR = paths.HOME / "log"
    paths.SOURCE_LAYER = False
    from physiclaw.agent.engine import assemble, loop, policy
    from physiclaw.agent.engine.provenance import PluginBinding
    from physiclaw.agent.engine.runspec import EngineRun, Settings
    from physiclaw.agent.engine.session import Session
    from physiclaw.agent.llm import LLMProducer
    from physiclaw.agent.runtime.hook import Trigger

    source = args.session.resolve()
    tools = next(
        record["tools"]
        for record in records(source / "model.jsonl")
        if record["kind"] == "session_start"
    )
    # Device schemas come from this recorded session, never from AST discovery.
    device_names = (
        gv.PRESS_TOOLS
        | gv.NAV_TOOLS
        | {
            gv.PEEK,
            gv.SCREENSHOT,
            gv.SWIPE,
            gv.SEQUENCE,
            gv.SEND_TO_CLIPBOARD,
            gv.UNLOCK_PHONE,
        }
    )
    schemas = [tool for tool in tools if tool["name"] in device_names]
    ref = model_ref()
    vendor, model = ref.split("/", 1)
    if vendor != "deepseek":
        raise ValueError(
            "This approved live replay targets the configured DeepSeek endpoint"
        )
    device = ReplayDevice(source, schemas)
    bundle = assemble.build_prompt_bundle(vendor, mcp_schemas=schemas)
    registry = {
        name: tool
        if name in {"note", "update_progress", "end_session"}
        else replace(tool, handler=blocked_local)
        for name, tool in bundle.local_registry.items()
    }
    session = Session()
    provider = make_chat_provider(vendor, model, usage_sink=sink)
    settings = Settings(
        max_turns=12,
        max_session_attempts=1,
        provider_retry_attempts=3,
        retry_backoff_seconds=1,
        wait_default_minutes=15,
        max_session_seconds=240,
    )
    run = EngineRun(
        plugins=(),
        mcp=device,
        schema_by_name={
            tool["name"]: tool for tool in [*schemas, *bundle.local_schemas]
        },
        local_registry=registry,
        tr=sink,
        rlog=sink,
        settings=settings,
        policies=policy.default_policies(layout_incomplete=False),
        deadline=time.monotonic() + settings.max_session_seconds,
    )
    messages = assemble.build_initial_messages(
        [Trigger(description=args.goal, source="recorded-session-test")],
        bundle.system_prompt,
    )
    producer = LLMProducer(provider, run, session, messages, catalog=bundle.catalog)
    run.plugins = (PluginBinding("physiclaw.llm", producer, "LLM"),)
    started = time.monotonic()
    error = None
    try:
        await loop.drive(run, session)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        await provider.aclose()
    request_audit = audit_requests(sink.requests)
    observation_ids = {
        event.observation_id for event in session.facts.events if event.observation_id
    }
    summary = {
        "model": ref,
        "source_session": source.name,
        "simulated_device": True,
        "physical_actions": 0,
        "request_audit": request_audit,
        "observations": len(observation_ids),
        "observation_issue": session.facts.observation_issue,
        "status": session.sentinel_status,
        "model_turns": session.model_turns,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "recorded_transitions": len(device.steps),
        "completed_transitions": device.index,
        "device_calls": device.calls,
        "error": error,
        "corrections": sum(e.get("event") == "bad_turn_shape" for e in sink.events),
        "usage": [e for e in sink.events if e.get("event") == "usage"],
        "success": error is None
        and device.index == len(device.steps)
        and session.sentinel_status in {"DONE", "IDLE"}
        and len(observation_ids) >= len(device.steps)
        and session.facts.observation_issue is None
        and request_audit["valid"],
    }
    (args.output / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k not in {"usage", "device_calls"}},
            ensure_ascii=False,
        )
    )
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--goal", required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
