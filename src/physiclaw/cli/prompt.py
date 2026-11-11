"""``physiclaw prompt`` — inspect assembled prompts and captured requests.

`prompt system` uses the engine's prompt assembly. `prompt request` adds a
representative turn-0 message array, labeled partial because live inputs and
provider options are absent. `request --capture model.jsonl` prints the last
recorded native request body. Pass `--save-as FILE` to write instead of stdout.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import click
import typer

from physiclaw.cli._format import exit_error
from physiclaw.contract.dto import ContentBlock
from physiclaw.port import filesystem

prompt_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect the SYSTEM prompt, an offline preview, or a captured request.",
)

_SaveAs = Annotated[
    Path | None,
    typer.Option("--save-as", help="Write the dump to this file instead of stdout."),
]


_McpSchemas = Annotated[
    Path | None,
    typer.Option(
        "--mcp-schemas", help="Captured MCP tool list (JSON) for a TurnOutput preview."
    ),
]
_Capture = Annotated[
    Path | None,
    typer.Option(
        "--capture",
        help="Replay the last full request body from a model.jsonl capture; no network calls.",
    ),
]


def _bundle(mcp_schemas: Path | None):
    if mcp_schemas is None:
        exit_error(
            "TurnOutput preview requires --mcp-schemas FILE, or use request --capture model.jsonl for an exact recorded body"
        )
    from physiclaw.agent.engine import assemble

    try:
        data = json.loads(mcp_schemas.read_text(encoding="utf-8"))
        tools = data.get("tools") if isinstance(data, dict) else data
        if not isinstance(tools, list):
            raise ValueError("expected an MCP schema list or an object with tools")
        return assemble.build_prompt_bundle(_provider_id(), mcp_schemas=tools)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        exit_error(f"invalid schema capture: {exc}")


def _captured_body(path: Path) -> str:
    try:
        last_body = None
        with path.open(encoding="utf-8") as capture:
            for line in capture:
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("kind") == "request" and isinstance(
                    record.get("body"), dict
                ):
                    last_body = record["body"]
        if last_body is None:
            raise ValueError("capture has no full native request body")
        return json.dumps(last_body, ensure_ascii=True, indent=2)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        exit_error(f"invalid request capture: {exc}")


def _provider_id() -> str:
    """The active provider id, or '' when no model is configured (the SYSTEM
    prompt uses it to select the inline action index)."""
    from physiclaw.common.config import model_ref
    from physiclaw.provider import validate_chat_model_ref

    try:
        ref = model_ref()
    except RuntimeError:
        return ""
    try:
        return validate_chat_model_ref(ref)[0]
    except ValueError as e:
        exit_error(str(e))


def _content_text(content: str | list[ContentBlock]) -> str:
    """Keep text blocks verbatim and label non-text blocks in the dump."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else f"[{type(block).__name__}]")
    return "\n".join(parts)


def _emit(text: str, save_as: Path | None) -> None:
    """Save a complete dump or preserve the previous one on failure.

    Stdout stays verbatim; saved files end with exactly one newline.
    """
    if save_as is None:
        click.get_binary_stream("stdout").write(text.encode("utf-8"))
        return

    from physiclaw.common.text import write_text_atomic

    content = text.rstrip("\n") + "\n"
    try:
        # Follow destination symlinks as the previous direct write did.
        target = filesystem.native_path(
            filesystem.resolve_path(save_as, allow_missing=True)
        )
        write_text_atomic(target, content)
    except (OSError, UnicodeError, RuntimeError) as e:
        # resolve() raises RuntimeError for symlink loops on Python 3.12.
        exit_error(f"could not save prompt to {save_as}: {e}")
    typer.echo(f"Wrote {len(content)} chars to {save_as}")


@prompt_app.command()
def system(save_as: _SaveAs = None, mcp_schemas: _McpSchemas = None) -> None:
    """Print the SYSTEM prompt (doctrine + tooling + skills + learned screen
    layout + memory)."""
    _emit(_bundle(mcp_schemas).system_prompt, save_as)


@prompt_app.command()
def request(
    save_as: _SaveAs = None, mcp_schemas: _McpSchemas = None, capture: _Capture = None
) -> None:
    """Print a partial turn-0 preview, or the last full body from --capture."""
    if capture is not None:
        _emit(_captured_body(capture), save_as)
        return
    from physiclaw.agent.engine import assemble
    from physiclaw.agent.engine.session import Session
    from physiclaw.agent.runtime.hook import Trigger

    bundle = _bundle(mcp_schemas)
    # A representative camera wake — the trigger text is runtime-specific.
    triggers = [Trigger(description="phone screen changed", source="phone")]
    messages = assemble.build_initial_messages(triggers, bundle.system_prompt)

    # Mirror `loop.drive`'s turn-0: tick the plan, then pin the same tail slots.
    session = Session()
    session.plan.tick_turn()
    messages = assemble.apply_request_tails(
        messages,
        session,
        layout_incomplete=bundle.layout_incomplete,
    )

    text = "".join(
        f"\n===== [{type(m).__name__.removesuffix('Message').lower()}] =====\n"
        f"{_content_text(m.content)}\n"
        for m in messages
    )
    label = "Partial offline preview: representative wake, without provider request options."
    text += "\n===== [TurnOutput schema] =====\n" + json.dumps(
        bundle.catalog.schema, ensure_ascii=True, indent=2
    )
    _emit(label + "\n" + text, save_as)
