"""``physiclaw prompt`` — dump the assembled SYSTEM prompt / full LLM request.

A debugging aid: `prompt system` prints the SYSTEM prompt exactly as a session
would build it; `prompt request` prints that plus the turn-0 message array (the
full request handed to the model). Both go through the engine's own assembly
(`assemble.build_prompt_bundle` / `assemble.build_initial_messages`) so the
output can't drift from what the running agent actually sends. Pass
`--save-as FILE` to write the dump to a file instead of stdout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import click
import typer

from physiclaw.cli._format import exit_error
from physiclaw.contract.dto import ContentBlock
from physiclaw.port import filesystem

prompt_app = typer.Typer(
    no_args_is_help=True,
    help="Dump the SYSTEM prompt / full LLM request for inspection.",
)

_SaveAs = Annotated[
    Path | None,
    typer.Option("--save-as", help="Write the dump to this file instead of stdout."),
]


def _provider_id() -> str:
    """The active provider id, or '' when no model is configured (the SYSTEM
    prompt only uses it to pick the provider's reasoning-format fragment)."""
    from physiclaw.common.config import model_ref
    from physiclaw.provider import validate_model_ref

    try:
        ref = model_ref()
    except RuntimeError:
        return ""
    try:
        return validate_model_ref(ref)[0]
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
        target = filesystem.native_path(save_as.resolve())
        write_text_atomic(target, content)
    except (OSError, UnicodeError, RuntimeError) as e:
        # resolve() raises RuntimeError for symlink loops on Python 3.12.
        exit_error(f"could not save prompt to {save_as}: {e}")
    typer.echo(f"Wrote {len(content)} chars to {save_as}")


@prompt_app.command()
def system(save_as: _SaveAs = None) -> None:
    """Print the SYSTEM prompt (doctrine + tooling + skills + learned screen
    layout + memory)."""
    from physiclaw.agent.engine import assemble

    _emit(assemble.build_prompt_bundle(_provider_id()).system_prompt, save_as)


@prompt_app.command()
def request(save_as: _SaveAs = None) -> None:
    """Print the full turn-0 request: the SYSTEM prompt plus the message array
    (wake trigger + compaction slots + plan/scratchpad/first-run tails)."""
    from physiclaw.agent.engine import assemble
    from physiclaw.agent.engine.session import Session
    from physiclaw.agent.runtime.hook import Trigger

    bundle = assemble.build_prompt_bundle(_provider_id())
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
    _emit(text, save_as)
