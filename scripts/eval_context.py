"""Compare explicit provider-request dumps (paid, opt-in; no phone actions).

uv run python scripts/eval_context.py --cases /tmp/context-cases.json \
    --output /tmp/context-results.json --repeats 2

Each case names provider, session (local image directory's parent), turn,
before and after (absolute JSON request paths), and actions (accepted next
actions, each with tool and optional args). Optional forbidden_bboxes and
scratchpad_required add checks. Both dumps must use the same model and
OpenAI-compatible request format. Messages and tool descriptions are replayed
exactly as saved; session-relative image paths are restored to their original
bytes. This script does not assemble, rewrite, or migrate prompts. Prepare the
dumps explicitly.
Only the declared provider receives the data. Reports remain local and contain
model replies; treat them like the source logs. This measures next-turn
responses, not end-to-end physical task completion.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch


def restore_images(value, session: Path):
    if isinstance(value, list):
        return [restore_images(v, session) for v in value]
    if not isinstance(value, dict):
        return value
    if value.get("type") == "image_url":
        ref = value["image_url"]["url"]
        if ref.startswith("data:"):
            return value
        path = (session / ref).resolve()
        if not path.is_relative_to((session / "images").resolve()):
            raise ValueError("image outside the session image directory")
        mime = "image/png" if path.suffix == ".png" else "image/jpeg"
        return {
            **value,
            "image_url": {
                **value["image_url"],
                "url": f"data:{mime};base64,"
                + base64.b64encode(path.read_bytes()).decode(),
            },
        }
    return {k: restore_images(v, session) for k, v in value.items()}


def load_case(case: dict):
    """Read two explicit, losslessly replayable request snapshots."""
    from physiclaw.provider.chat.wire import tool_to_wire

    session = Path(case["session"]).expanduser().resolve()
    variants = []
    model = None
    for variant in ("before", "after"):
        payload = json.loads(
            Path(case[variant]).expanduser().read_text(encoding="utf-8")
        )
        if not isinstance(payload, dict) or set(payload) != {
            "model",
            "messages",
            "tools",
            "tool_choice",
        }:
            raise ValueError(
                "request must contain model, messages, tools and tool_choice"
            )
        if not isinstance(payload["model"], str) or not payload["model"].strip():
            raise ValueError("request needs a model")
        if model is not None and model != payload["model"]:
            raise ValueError("before and after must use the same model")
        model = payload["model"]
        if payload["tool_choice"] != "auto":
            raise ValueError("replay requires tool_choice auto")
        messages = payload["messages"]
        if not isinstance(messages, list) or not any(
            isinstance(m, dict) and m.get("role") == "system" for m in messages
        ):
            raise ValueError(
                "replay requires OpenAI-compatible messages with a system prompt"
            )
        schemas = [
            {
                "name": tool["function"]["name"],
                "description": tool["function"]["description"],
                "input_schema": tool["function"]["parameters"],
            }
            for tool in payload["tools"]
        ]
        if [tool_to_wire(schema) for schema in schemas] != payload["tools"]:
            raise ValueError("tool definitions must round-trip without changes")
        variants.append((variant, messages, schemas))
    return model, session, variants


def grade(reply, case, schemas):
    from physiclaw.agent.engine.loop import _validate_turn
    from physiclaw.agent.engine.validator import ValidationError, validate_arguments

    errors = []
    try:
        _validate_turn(reply)
    except (TypeError, ValueError) as exc:
        return [str(exc)]
    calls = reply.tool_calls
    if len(calls) != 2 or sum(c.name == "note" for c in calls) != 1:
        return ["invalid note + one-action turn"]
    schemas_by_name = {s["name"]: s["input_schema"] for s in schemas}
    for call in calls:
        if call.name not in schemas_by_name:
            errors.append("unknown tool")
            continue
        try:
            validate_arguments(call.arguments, schemas_by_name[call.name])
        except ValidationError as exc:
            errors.append(str(exc))
    action = next(c for c in calls if c.name != "note")
    if not any(
        action.name == option["tool"]
        and all(action.arguments.get(k) == v for k, v in option.get("args", {}).items())
        for option in case["actions"]
    ):
        errors.append("action does not match an accepted grounded alternative")
    for bbox in case.get("forbidden_bboxes", []):
        if contains_box(action.arguments, bbox):
            errors.append("unsafe target")
    if case.get("scratchpad_required"):
        note = next(c for c in calls if c.name == "note")
        scratchpad = note.arguments.get("scratchpad")
        if not isinstance(scratchpad, str) or not scratchpad.strip():
            errors.append("missing checkpoint scratchpad")
    return errors


def contains_box(value, bbox):
    """Compare coordinate values, including nested sequence args, not JSON text."""
    if isinstance(value, dict):
        return any(contains_box(v, bbox) for v in value.values())
    if isinstance(value, list):
        return value == bbox or any(contains_box(v, bbox) for v in value)
    return False


async def evaluate(args):
    from physiclaw.provider import OpenAICompatibleProvider, make_chat_provider

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    prepared = [(case, load_case(case)) for case in cases]
    report = {
        "scope": "Recorded next-turn replay; no tool execution",
        "repeats": args.repeats,
        "results": [],
    }
    semaphore = asyncio.Semaphore(args.concurrency)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    async def run(case, model, session, variant, messages, schemas, repeat):
        async with semaphore:
            row = {
                "session": session.name,
                "turn": case["turn"],
                "case": case.get("name", ""),
                "variant": variant,
                "repeat": repeat,
                "model": f"{case['provider']}/{model}",
                "text_chars": len(json.dumps(messages, ensure_ascii=False)),
                "schema_chars": len(json.dumps(schemas, ensure_ascii=False)),
            }
            provider = None
            started = time.monotonic()
            try:
                provider = make_chat_provider(case["provider"], model)
                if not isinstance(provider, OpenAICompatibleProvider):
                    raise ValueError("recorded provider is not OpenAI-compatible")
                wire = restore_images(messages, session)
                # Preserve the exact recorded wire, including the turn metadata.
                with patch.object(provider, "serialize_history", return_value=wire):
                    reply = await asyncio.wait_for(
                        provider.chat([], schemas), timeout=120
                    )
                row.update(
                    failures=grade(reply, case, schemas),
                    calls=[asdict(c) for c in reply.tool_calls],
                    usage=asdict(reply.usage),
                    response_model=reply.response_model,
                )
            except Exception as exc:
                row["error"] = type(exc).__name__
            finally:
                if provider is not None:
                    try:
                        await provider.aclose()
                    except Exception as exc:
                        row["cleanup_error"] = type(exc).__name__
                        # Keep the original failure, including cancellation,
                        # while retaining cleanup diagnostics for this case.
                        row.setdefault("error", type(exc).__name__)
            row["seconds"] = round(time.monotonic() - started, 2)
            report["results"].append(row)
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            status = row.get("error") or (
                "FAIL " + str(row["failures"]) if row["failures"] else "PASS"
            )
            print(
                f"{session.name} t{case['turn']} {variant} #{repeat}: {status}",
                flush=True,
            )

    await asyncio.gather(
        *(
            run(case, model, session, variant, messages, schemas, repeat)
            for repeat in range(args.repeats)
            for case, (model, session, variants) in prepared
            for variant, messages, schemas in variants
        )
    )
    counts = Counter(
        (
            r["variant"],
            "error" if r.get("error") else "fail" if r["failures"] else "pass",
        )
        for r in report["results"]
    )
    print(dict(counts))
    return int(any(r.get("error") or r.get("failures") for r in report["results"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    if min(args.repeats, args.concurrency) < 1:
        parser.error("repeats and concurrency must be positive")
    logging.disable(logging.CRITICAL)
    return asyncio.run(evaluate(args))


if __name__ == "__main__":
    raise SystemExit(main())
