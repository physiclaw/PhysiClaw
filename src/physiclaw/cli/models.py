"""``physiclaw models`` — list, inspect, and switch the active model.

Sugar over ``physiclaw config set agent.model <ref>`` plus a discovery
view of each provider's catalog. Validates against the per-provider
``MODELS`` tuple before writing, so a typo fails at set-time instead of
the next ``physiclaw server`` start.

Subcommands:

  - ``physiclaw models``                — bare: current ref + source +
                                          catalog row (active model's
                                          reasoning / context window)
  - ``physiclaw models list [provider]``— all `provider/model` refs;
                                          optional filter to one provider
  - ``physiclaw models set <ref>``      — switch active model
"""
from typing import Annotated

import typer

from physiclaw import config as _config
from physiclaw.cli._format import info, next_hint, ok, section, warn

# Provider package imports happen inside command bodies — pulling
# `agent.provider` at module load drags httpx (~80ms) into every
# `physiclaw --help` invocation.

models_app = typer.Typer(
    help="List, inspect, and switch the active model.",
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    add_completion=False,
)


def _format_model_row(provider_id: str, m_id: str, *, indent: int = 2) -> str:
    from physiclaw.agent.provider import provider_class
    cls = provider_class(provider_id)
    if cls is None:
        return " " * indent + f"{provider_id}/{m_id}"
    entry = cls.find_model(m_id)
    if entry is None:
        return " " * indent + f"{provider_id}/{m_id}  (not in catalog)"
    bits = []
    if entry.reasoning:
        bits.append("reasoning")
    if entry.context_window is not None:
        bits.append(f"ctx={entry.context_window:,}")
    suffix = f"  [{' · '.join(bits)}]" if bits else ""
    return " " * indent + f"{provider_id}/{m_id}{suffix}"


@models_app.callback()
def _root(ctx: typer.Context) -> None:
    """Bare `physiclaw models` shows status; subcommands handle the rest."""
    if ctx.invoked_subcommand is not None:
        return
    typer.echo(section("Active model"))
    try:
        ref, source = _config.model_ref_with_source()
    except RuntimeError:
        typer.echo(warn("none — set one with `physiclaw models set <provider/model>`"))
        typer.echo()
        typer.echo(next_hint("physiclaw models list"))
        return
    try:
        provider_id, model_id = _config.parse_model_ref(ref)
    except ValueError as e:
        typer.echo(warn(f"{ref} (from {source}) — invalid ref: {e}"))
        return
    typer.echo(ok(f"{ref}  (from {source})"))
    typer.echo(_format_model_row(provider_id, model_id, indent=2))


@models_app.command("list")
def _list(
    provider: Annotated[
        str | None,
        typer.Argument(
            help="Filter to one provider (e.g. `qwen`). Omit for all.",
        ),
    ] = None,
) -> None:
    """List `provider/model` refs declared in each provider's catalog.

    Stubs (moonshot, openai) are included — their catalog entries are valid
    targets for `models set`, but server start will fail until their
    `_api_key()` is wired up.
    """
    from physiclaw.agent.provider import (
        CLAUDE_CODE_ID,
        in_process_provider_ids,
        provider_class,
    )

    if provider is not None and provider == CLAUDE_CODE_ID:
        # claude-code has no in-process catalog — model selection is
        # whatever the `claude` CLI accepts. Show a hint instead of
        # silently empty output.
        typer.echo(info(
            "claude-code has no in-process catalog — pass any model id "
            "the `claude` CLI accepts, e.g. `claude-code/claude-sonnet-4-6`."
        ))
        return
    targets = (provider,) if provider else in_process_provider_ids()
    for pid in targets:
        cls = provider_class(pid)
        if cls is None:
            typer.echo(warn(f"unknown provider {pid!r}; "
                            f"known: {in_process_provider_ids()}"))
            raise typer.Exit(code=1)
        typer.echo(section(pid))
        for entry in cls.MODELS:
            typer.echo(_format_model_row(pid, entry.id, indent=2))
        typer.echo()


@models_app.command("set")
def _set(
    ref: Annotated[
        str,
        typer.Argument(help="Model ref to activate, e.g. `qwen/qwen3-max`."),
    ],
) -> None:
    """Switch the active model — writes to `[agent] model` after
    validating `ref` against the provider's catalog. Use
    `physiclaw models list` to see available refs."""
    from physiclaw.agent.provider import (
        CLAUDE_CODE_ID,
        in_process_provider_ids,
        provider_class,
    )

    try:
        provider_id, model_id = _config.parse_model_ref(ref)
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(code=1)

    # claude-code has no in-process catalog; trust the claude CLI to
    # validate the model id at spawn time.
    if provider_id != CLAUDE_CODE_ID:
        cls = provider_class(provider_id)
        if cls is None:
            known = (*in_process_provider_ids(), CLAUDE_CODE_ID)
            typer.echo(
                f"error: unknown provider {provider_id!r} (known: {known})",
                err=True,
            )
            raise typer.Exit(code=1)
        if not cls.has_model(model_id):
            known = ", ".join(m.id for m in cls.MODELS)
            typer.echo(
                f"error: model {model_id!r} not in {provider_id} catalog "
                f"(known: {known})",
                err=True,
            )
            raise typer.Exit(code=1)

    try:
        _config.set_dotted("agent.model", ref)
    except _config.ConfigError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(code=1)
    typer.echo(ok(f"agent.model = {ref}"))
    typer.echo("Restart `physiclaw server` to apply.")


__all__ = ["models_app"]
