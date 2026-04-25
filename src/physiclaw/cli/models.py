"""``physiclaw models`` — list, inspect, and switch the active model.

Sugar over the generic ``physiclaw config`` interface for the three
model-relevant TOML fields:

  - ``[agent] model``           → ``physiclaw models use <ref>``
  - ``[provider] *_api_key``    → ``physiclaw models key <provider> [val]``
                                  / ``physiclaw models keys``

Validates against the per-provider ``MODELS`` tuple before writing, so a
typo fails at set-time instead of the next ``physiclaw server`` start.

Subcommands:

  - ``physiclaw models``                — bare: active ref + source +
                                          catalog row + key status for
                                          the active provider
  - ``physiclaw models list [provider]``— all `provider/model` refs;
                                          optional filter to one provider
  - ``physiclaw models use <ref|keyword>``
                                        — switch active model. ``<ref>``
                                          is an exact ``provider/model``
                                          string; a bare keyword (no
                                          slash) substring-matches every
                                          catalog ref and resolves
                                          when unique
                                          (alias: ``set``)
  - ``physiclaw models key <provider> [<value>]``
                                        — set provider API key;
                                          prompts with hidden input if
                                          ``<value>`` is omitted
  - ``physiclaw models keys``           — list every provider's key
                                          status (env / config / unset),
                                          values masked
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


def _key_config_path(provider_id: str) -> str:
    """Dotted config path for one provider's API key — single source of
    truth so callers don't reconstruct `f"provider.{x}_api_key"` ad-hoc."""
    return f"provider.{provider_id}_api_key"


def _format_key_row(provider_id: str, *, indent: int = 2) -> str:
    """One-line key status for a provider, masked. Mirrors
    `BaseProvider._api_key()` resolution so what's shown is what the
    server will pick up at runtime."""
    from physiclaw.agent.provider import provider_class
    cls = provider_class(provider_id)
    pad = " " * indent
    if cls is None:
        return f"{pad}{provider_id} api key: (unknown provider)"
    env_vars = cls.API_KEY_ENV_VARS or (f"{cls.PROVIDER_ID.upper()}_API_KEY",)
    val, source = _config.resolve_provider_key(env_vars, f"{cls.PROVIDER_ID}_api_key")
    if not val:
        return f"{pad}{provider_id} api key: (unset)"
    return f"{pad}{provider_id} api key: ********  [{source}]"


@models_app.callback()
def _root(ctx: typer.Context) -> None:
    """Bare `physiclaw models` shows status; subcommands handle the rest."""
    if ctx.invoked_subcommand is not None:
        return
    typer.echo(section("Active model"))
    try:
        ref, source = _config.model_ref_with_source()
    except RuntimeError:
        typer.echo(warn("none — set one with `physiclaw models use <provider/model>`"))
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
    typer.echo(_format_key_row(provider_id, indent=2))


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
    targets for `models use`, but server start will fail until their
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


def _resolve_query(query: str) -> str:
    """Resolve a user-supplied query to a canonical `provider/model` ref.

    Slash present → treat as exact ref (caller validates against catalog).
    No slash → case-insensitive substring search across every in-process
    provider's catalog; resolve only if exactly one ref matches.
    """
    if "/" in query:
        return query
    from physiclaw.agent.provider import in_process_provider_ids, provider_class
    needle = query.lower()
    matches: list[str] = []
    for pid in in_process_provider_ids():
        cls = provider_class(pid)
        if cls is None:
            continue
        for entry in cls.MODELS:
            ref = f"{pid}/{entry.id}"
            if needle in ref.lower():
                matches.append(ref)
    if not matches:
        typer.echo(
            f"error: no model matches {query!r}\n"
            "  run `physiclaw models list` to see available refs.",
            err=True,
        )
        raise typer.Exit(code=1)
    if len(matches) > 1:
        listing = "\n".join(f"  {m}" for m in matches)
        typer.echo(
            f"error: {query!r} is ambiguous — matches:\n{listing}\n"
            "  pass the full `provider/model` ref to disambiguate.",
            err=True,
        )
        raise typer.Exit(code=1)
    return matches[0]


def _use_impl(query: str) -> None:
    """Switch the active model — resolves keyword (or accepts an exact
    ref), validates against the provider's catalog, and writes to
    `[agent] model`. Use `physiclaw models list` to see available refs."""
    from physiclaw.agent.provider import (
        CLAUDE_CODE_ID,
        in_process_provider_ids,
        provider_class,
    )

    ref = _resolve_query(query)

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
    if ref != query:
        typer.echo(ok(f"matched {query!r} → {ref}"))
    typer.echo(ok(f"agent.model = {ref}"))
    typer.echo(_format_model_row(provider_id, model_id, indent=2))
    typer.echo("Restart `physiclaw server` to apply.")


@models_app.command("use")
def _use(
    query: Annotated[
        str,
        typer.Argument(
            help="Exact `provider/model` ref OR a unique keyword "
                 "(e.g. `kimi`, `qwen3-max`, `sonnet`).",
        ),
    ],
) -> None:
    """Switch the active model. Pass an exact `provider/model` ref or a
    keyword that uniquely matches one catalog entry. Run `physiclaw
    models list` to see all refs."""
    _use_impl(query)


# `set` retained as a hidden alias for one release — early users +
# docs referenced it before the rename. Same function under two names.
models_app.command("set", hidden=True)(_use)


@models_app.command("key")
def _key(
    provider: Annotated[
        str,
        typer.Argument(help="Provider id, e.g. `moonshot`, `qwen`, `anthropic`."),
    ],
    value: Annotated[
        str | None,
        typer.Argument(
            help="Key value. Omit for an interactive hidden prompt "
                 "(keeps the secret out of shell history).",
        ),
    ] = None,
) -> None:
    """Set the API key for one provider — writes
    `[provider] <id>_api_key`. Prompts with hidden input if `value` is
    omitted; that's the safer default since shell history retains
    args."""
    from physiclaw.agent.provider import in_process_provider_ids, provider_class
    cls = provider_class(provider)
    if cls is None:
        known = ", ".join(in_process_provider_ids())
        typer.echo(
            f"error: unknown provider {provider!r} (known: {known})",
            err=True,
        )
        raise typer.Exit(code=1)
    if value is None:
        value = typer.prompt(f"{provider} api key", hide_input=True)
    path = _key_config_path(provider)
    try:
        _config.set_dotted(path, value)
    except _config.ConfigError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(code=1)
    typer.echo(ok(f"{path} set"))
    typer.echo("Restart `physiclaw server` to apply.")


@models_app.command("keys")
def _keys() -> None:
    """List every provider's API-key status (masked, with source —
    env var or config file). Mirrors `BaseProvider._api_key()`
    resolution so what's shown is what the server will use."""
    from physiclaw.agent.provider import in_process_provider_ids
    typer.echo(section("Provider API keys"))
    for pid in in_process_provider_ids():
        typer.echo(_format_key_row(pid, indent=2))


__all__ = ["models_app"]
