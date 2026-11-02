"""Compare explicit provider-request dumps (paid, opt-in; no phone actions).

uv run python scripts/eval_context.py --cases /tmp/context-cases.json \
    --output /tmp/context-results.json --repeats 2

Each case names provider, session (local image directory's parent), turn,
before and after (absolute JSON request paths), and actions (accepted next
actions, each with tool and optional args). Optional forbidden_bboxes and
scratchpad_required add checks. The session must contain model.jsonl with
its session_start.output_schema. Both dumps must use the same model and the
declared provider's TurnOutput format. Native request bodies, including cache
and thinking options, are replayed unchanged; session-relative images are
restored to their original bytes. This script does not migrate old requests.
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
    from physiclaw.contract.wire import image_ref

    if isinstance(value, list):
        return [restore_images(v, session) for v in value]
    if not isinstance(value, dict):
        return value
    ref = image_ref(value)
    if ref is None or ref.startswith(("https://", "http://")):
        return {k: restore_images(v, session) for k, v in value.items()}
    path = (session / ref).resolve()
    if not path.is_relative_to((session / "images").resolve()):
        raise ValueError("image outside the session image directory")
    mime = {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(
        path.suffix.lower(), "image/jpeg"
    )
    data = base64.b64encode(path.read_bytes()).decode()
    if value["type"] == "image":
        return {**value, "source": {"type": "base64", "media_type": mime, "data": data}}
    url = f"data:{mime};base64,{data}"
    return {
        **value,
        "image_url": url
        if value["type"] == "input_image"
        else {**value["image_url"], "url": url},
    }


def load_catalog(session: Path):
    """Rebuild the production decoder from the recorded logical schema."""
    from physiclaw.agent.engine.output import ActionCatalog

    with (session / "model.jsonl").open(encoding="utf-8") as stream:
        schema = next(
            (
                row.get("output_schema")
                for line in stream
                if (row := json.loads(line)).get("kind") == "session_start"
            ),
            None,
        )
    try:
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
            raise ValueError("schema changed during reconstruction")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("session needs a current TurnOutput output_schema") from exc
    return catalog


def load_case(case: dict):
    """Validate both snapshots and restore images before any paid calls."""
    session = Path(case["session"]).expanduser().resolve()
    catalog = load_catalog(session)
    output_spec = catalog.spec()
    variants = []
    model = None
    for variant in ("before", "after"):
        payload = json.loads(
            Path(case[variant]).expanduser().read_text(encoding="utf-8")
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("model"), str)
            or not payload["model"].strip()
        ):
            raise ValueError("request needs a model")
        if model is not None and model != payload["model"]:
            raise ValueError("before and after must use the same model")
        model = payload["model"]
        field = "input" if case["provider"] == "openai" else "messages"
        if not isinstance(payload.get(field), list) or not payload[field]:
            raise ValueError(f"request needs nonempty {field}")
        if case["provider"] == "openai":
            output = payload.get("text", {}).get("format", {})
            valid = (
                output.get("type") == "json_schema"
                and output.get("name") == output_spec.name
            )
        else:
            tools = payload.get("tools", [])
            output = tools[0] if isinstance(tools, list) and len(tools) == 1 else {}
            if case["provider"] != "anthropic":
                output = output.get("function", {})
            valid = output.get("name") == output_spec.function_name
        if not valid:
            raise ValueError("request must ask for one TurnOutput")
        variants.append((variant, restore_images(payload, session)))
    return model, session, catalog, variants


def grade(completion, case):
    from physiclaw.contract.decision import decision_json

    if completion.failure is not None:
        return [f"{completion.failure.kind}: {completion.failure.message}"]
    turn = completion.value
    name, arguments = next(iter(turn.action.items()))
    errors = []

    arguments = decision_json(arguments)
    if not any(
        name == option["tool"]
        and all(arguments.get(k) == v for k, v in option.get("args", {}).items())
        for option in case["actions"]
    ):
        errors.append("action does not match an accepted grounded alternative")
    for bbox in case.get("forbidden_bboxes", []):
        if contains_box(arguments, bbox):
            errors.append("unsafe target")
    if case.get("scratchpad_required") and not (turn.scratchpad or "").strip():
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
    from physiclaw.contract.decision import decision_json
    from physiclaw.provider import make_chat_provider

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    prepared = []
    for case in cases:
        model, session, catalog, variants = load_case(case)
        output_spec = catalog.spec()
        for variant, payload in variants:
            details = {
                "session": session.name,
                "turn": case["turn"],
                "case": case.get("name", ""),
                "variant": variant,
                "model": f"{case['provider']}/{model}",
                "request_chars": len(json.dumps(payload, ensure_ascii=False)),
            }
            prepared.append((case, output_spec, payload, details))
    report = {
        "scope": "Recorded next-turn replay; no tool execution",
        "repeats": args.repeats,
        "results": [],
    }
    semaphore = asyncio.Semaphore(args.concurrency)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    async def run(case, output_spec, payload, details, repeat):
        async with semaphore:
            row = {**details, "repeat": repeat}
            provider = None
            started = time.monotonic()
            try:
                provider = make_chat_provider(case["provider"], payload["model"])
                # Diagnostic replay deliberately bypasses request assembly;
                # response parsing, validation and usage use the real adapter.
                with patch.object(provider, "build_request", return_value=payload):
                    completion = await asyncio.wait_for(
                        provider.generate_structured([], output_spec), timeout=120
                    )
                reply, turn = completion.reply, completion.value
                row.update(
                    failures=grade(completion, case),
                    output={
                        "summary": turn.summary,
                        "scratchpad": turn.scratchpad,
                        "action": decision_json(turn.action),
                    }
                    if turn is not None
                    else None,
                    content=reply.content,
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
                f"{row['session']} t{row['turn']} {row['variant']} #{repeat}: {status}",
                flush=True,
            )

    await asyncio.gather(
        *(
            run(case, output_spec, payload, details, repeat)
            for repeat in range(args.repeats)
            for case, output_spec, payload, details in prepared
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
