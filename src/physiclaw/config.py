"""User-tunable configuration loaded from ``~/.physiclaw/config.toml``.

Defaults live on the ``@dataclass`` sections — ``to_toml()`` + ``write_default()``
render the commented template from those same defaults, so bumping a default
doesn't need two edits.

Unknown top-level sections and unknown keys inside a section raise
``ConfigError`` on load. The CLI catches this and points users at
``physiclaw config edit``.

Layering (first match wins):
    CLI flag  >  env var  >  config.toml  >  built-in default
"""

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from physiclaw import paths


class ConfigError(ValueError):
    """Malformed ``config.toml`` — unknown section/key, or type mismatch."""


@dataclass
class ServerConfig:
    port: int = 8048
    host: str = "0.0.0.0"
    save_tool_calls: bool = False
    save_snapshots: bool = False
    save_screenshots: bool = False


@dataclass
class WarmStartConfig:
    bridge_wait_timeout_seconds: int = 120
    bridge_settle_seconds: float = 2.0
    port_wait_timeout_seconds: float = 10.0
    port_wait_connect_timeout_seconds: float = 0.2
    port_wait_interval_seconds: float = 0.1


@dataclass
class AutoPickConfig:
    """Timeouts for the camera auto-pick step in `physiclaw setup hardware`.
    Values are tighter than warm-start: interactive setup wants snappier
    feedback when the phone /bridge page isn't responding. Cap stays
    below the CLI's HTTP timeout (60s) so the auto-pick loop has time
    to iterate camera indices after the bridge comes online."""
    bridge_wait_timeout_seconds: int = 25
    bridge_settle_seconds: float = 1.5


@dataclass
class EngineConfig:
    max_turns: int = 300
    max_attempts: int = 3
    retry_backoff_seconds: float = 5.0
    wait_default_minutes: int = 15
    react_cooldown_seconds: float = 6.0
    stale_tick_threshold: int = 8
    state_decay_turns: int = 2


@dataclass
class AgentConfig:
    """Agent runtime selection.

    ``model`` is a ``provider/model`` ref, e.g. ``"qwen/qwen3.6-plus"`` or
    ``"claude-code/claude-sonnet-4-6"``. The first segment selects the
    engine + provider; the second selects the model within that provider's
    catalog. Empty string means "use ``PHYSICLAW_MODEL`` env var, then
    fail loudly" — there is no universal default.
    """

    model: str = ""


@dataclass
class ProviderConfig:
    """Per-provider credentials (only).

    Empty strings mean "fall back to env / built-in default" — see the
    ``*_api_key()`` helpers below for resolution order. Provider/model
    selection lives under ``[agent] model``.

    Field names match the provider id (qwen/moonshot/openai/anthropic) —
    same convention OpenClaw uses. Today only ``qwen_api_key`` is wired
    to working code; the others are accepted for forward compatibility
    but read by nothing until those providers are de-stubbed.
    """

    qwen_api_key: str = ""
    moonshot_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""


@dataclass
class CompactConfig:
    max_image_edge_px: int = 1566
    jpeg_quality: int = 85


@dataclass
class MemoryConfig:
    default_log_entries: int = 20
    bootstrap_log_entries: int = 10


@dataclass
class ClaudeConfig:
    timeout_seconds: int = 180
    stream_buffer_mb: int = 10
    max_attempts: int = 3
    retry_backoff_seconds: float = 5.0


@dataclass
class RetentionConfig:
    trace_days: int = 7


@dataclass
class SkillsConfig:
    """Source repo for ``physiclaw skills install``. Empty = no default;
    users must pass ``--from`` or set this key. Convention: the source
    repo must contain a top-level ``skills/<name>/SKILL.md`` layout."""
    default_source: str = ""


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    warm_start: WarmStartConfig = field(default_factory=WarmStartConfig)
    auto_pick: AutoPickConfig = field(default_factory=AutoPickConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    compact: CompactConfig = field(default_factory=CompactConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)


_SECTION_TYPES: dict[str, type] = {
    "server": ServerConfig,
    "warm_start": WarmStartConfig,
    "auto_pick": AutoPickConfig,
    "engine": EngineConfig,
    "agent": AgentConfig,
    "provider": ProviderConfig,
    "compact": CompactConfig,
    "memory": MemoryConfig,
    "claude": ClaudeConfig,
    "retention": RetentionConfig,
    "skills": SkillsConfig,
}


_FILE_HEADER = """\
# PhysiClaw config. Edit with `physiclaw config edit`. Changes apply on
# next `physiclaw server` start. Delete a key to revert to the built-in
# default. Unknown keys / sections fail loudly on load.
"""

_SECTION_COMMENTS: dict[str, str] = {
    "warm_start": "Timeouts for `physiclaw server --warm-start` hardware reconnect.",
    "auto_pick": "Timeouts for the camera auto-pick step in `physiclaw setup hardware`.",
    "engine": "Runaway safeguard + retry + pacing for the agent's tool-call loop.",
    "agent": (
        "Engine + model selection. `model` is a `provider/model` ref, e.g. "
        "`qwen/qwen3.6-plus` or `claude-code/claude-sonnet-4-6`. "
        "`PHYSICLAW_MODEL` env var overrides."
    ),
    "provider": (
        "Per-provider API keys. Field names match the provider id "
        "(qwen/moonshot/openai/anthropic). Env vars (QWEN_API_KEY, "
        "MOONSHOT_API_KEY, OPENAI_API_KEY, …) override these. Treat "
        "keys here like ssh keys."
    ),
    "compact": "Screenshot compression before sending to the LLM.",
    "memory": "Daily-log loading: bootstrap preload + on-demand `read_logs` defaults.",
    "claude": "Applied when [agent] model = 'claude-code/...' (external CLI subprocess).",
    "retention": "Purge window for on-disk engine trace logs + cron job history.",
    "skills": (
        "Default source repo for `physiclaw skills install`. Empty = require "
        "`--from`. Accepts `owner/repo` shorthand or a full git URL."
    ),
}

_FIELD_COMMENTS: dict[tuple[str, str], str] = {
    ("server", "save_tool_calls"): "dump every peek/screenshot output",
    ("server", "save_snapshots"): "dump every raw camera frame",
    ("server", "save_screenshots"): "dump every raw phone-own screenshot",
    ("memory", "default_log_entries"): "on-demand `read_logs` default size (max 200)",
    ("memory", "bootstrap_log_entries"): "auto-preloaded into the memory slot at every wake",
}


def config_path() -> Path:
    return paths.HOME / "config.toml"


def _build_section(name: str, cls: type, overrides: dict[str, Any]) -> Any:
    known = {f.name for f in fields(cls)}
    extra = set(overrides) - known
    if extra:
        raise ConfigError(
            f"unknown key(s) in [{name}]: {sorted(extra)} — valid keys: {sorted(known)}"
        )
    return cls(**overrides)


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config()
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"failed to read {path}: {e}") from e

    unknown_sections = set(raw) - set(_SECTION_TYPES)
    if unknown_sections:
        raise ConfigError(
            f"unknown section(s) in {path}: {sorted(unknown_sections)} — "
            f"valid sections: {sorted(_SECTION_TYPES)}"
        )

    built: dict[str, Any] = {}
    for key, cls in _SECTION_TYPES.items():
        overrides = raw.get(key, {})
        if not isinstance(overrides, dict):
            raise ConfigError(f"[{key}] must be a table, got {type(overrides).__name__}")
        built[key] = _build_section(key, cls, overrides)

    return Config(**built)


def _toml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return repr(v)
    if isinstance(v, float):
        import math

        if math.isnan(v) or math.isinf(v):
            raise ConfigError(f"non-finite float not representable in TOML: {v!r}")
        return repr(v)
    if isinstance(v, str):
        return f'"{v}"'
    raise ConfigError(f"cannot serialize {v!r} ({type(v).__name__}) to TOML")


def to_toml(cfg: Config, *, with_comments: bool = False) -> str:
    parts: list[str] = []
    if with_comments:
        parts.append(_FILE_HEADER.rstrip() + "\n")
    for name in _SECTION_TYPES:
        header_comment = _SECTION_COMMENTS.get(name) if with_comments else None
        if header_comment:
            parts.append(f"# {header_comment}")
        parts.append(f"[{name}]")
        section = getattr(cfg, name)
        for f in fields(section):
            val = getattr(section, f.name)
            line = f"{f.name} = {_toml_scalar(val)}"
            inline = _FIELD_COMMENTS.get((name, f.name)) if with_comments else None
            if inline:
                line = f"{line:<32} # {inline}"
            parts.append(line)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def write_default(path: Path | None = None) -> Path:
    """Write a commented default ``config.toml`` if absent. No-op if present."""
    path = path or config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(to_toml(Config(), with_comments=True))
    return path


def get(cfg: Config, dotted: str) -> Any:
    """Walk a dotted path like ``engine.max_turns`` against ``cfg``.

    Raises ``ConfigError`` with the list of siblings at the failing level so
    the CLI can print an actionable message.
    """
    parts = dotted.split(".")
    if not parts or not all(parts):
        raise ConfigError(f"empty path: {dotted!r}")
    cursor: Any = cfg
    for i, part in enumerate(parts):
        if not dataclasses.is_dataclass(cursor):
            walked = ".".join(parts[:i])
            raise ConfigError(f"{walked!r} is a leaf value, not a section")
        siblings = [f.name for f in fields(cursor)]
        if part not in siblings:
            walked = ".".join(parts[:i]) or "<root>"
            raise ConfigError(
                f"unknown key {part!r} at {walked}; valid: {sorted(siblings)}"
            )
        cursor = getattr(cursor, part)
    return cursor


def _coerce(raw: str, current: Any) -> Any:
    """Cast a CLI string argument to the type of ``current`` (the field default)."""
    if isinstance(current, bool):  # bool before int — bool is an int subclass
        if raw.lower() in ("true", "1", "yes", "on"):
            return True
        if raw.lower() in ("false", "0", "no", "off"):
            return False
        raise ConfigError(f"can't parse {raw!r} as bool (use true/false)")
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError as e:
            raise ConfigError(f"can't parse {raw!r} as int") from e
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError as e:
            raise ConfigError(f"can't parse {raw!r} as float") from e
    return raw  # str


def _validate_dotted(dotted: str) -> tuple[str, str]:
    """Confirm ``dotted`` is a known ``section.field`` and return the parts."""
    parts = dotted.split(".")
    if len(parts) != 2 or not all(parts):
        raise ConfigError(f"key must be section.field (got {dotted!r})")
    section, field_name = parts
    if section not in _SECTION_TYPES:
        raise ConfigError(
            f"unknown section {section!r}; valid: {sorted(_SECTION_TYPES)}"
        )
    field_names = {f.name for f in fields(_SECTION_TYPES[section])}
    if field_name not in field_names:
        raise ConfigError(
            f"unknown key {field_name!r} at {section}; valid: {sorted(field_names)}"
        )
    return section, field_name


def set_dotted(dotted: str, raw_value: str, path: Path | None = None) -> None:
    """Update one ``section.field`` in ``config.toml`` in place.

    Preserves comments + ordering via tomlkit. The new value is coerced to
    the field's default type. Re-validates by reloading via the strict
    ``load()`` so the file is never left unparseable.
    """
    import tomlkit

    path = path or config_path()
    section, field_name = _validate_dotted(dotted)
    current = getattr(getattr(load(path), section), field_name)
    coerced = _coerce(raw_value, current)

    if not path.exists():
        write_default(path)
    doc = tomlkit.parse(path.read_text())
    if section not in doc:
        doc.add(section, tomlkit.table())
    doc[section][field_name] = coerced
    path.write_text(tomlkit.dumps(doc))
    load(path)  # re-validate; raises ConfigError on schema drift


def unset_dotted(dotted: str, path: Path | None = None) -> bool:
    """Remove one ``section.field`` from ``config.toml`` so the built-in
    default applies. Returns True if a key was actually removed.
    """
    import tomlkit

    path = path or config_path()
    section, field_name = _validate_dotted(dotted)
    if not path.exists():
        return False
    doc = tomlkit.parse(path.read_text())
    if section not in doc or field_name not in doc[section]:
        return False
    del doc[section][field_name]
    path.write_text(tomlkit.dumps(doc))
    load(path)  # re-validate
    return True


# Module-level singleton, evaluated once at import. See CONFIG usage in the
# migrated consumers (engine, claude, runtime, plan, compact, memory, trace,
# warm_start, job_store).
CONFIG: Config = load()


# --- Model + provider selection ----------------------------------------------
# Order: env var > config.toml > raise. There is no implicit default —
# the user must configure a model. Refs use `provider/model` shape.


MODEL_ENV_VAR = "PHYSICLAW_MODEL"

_NO_MODEL_MSG = (
    f"no model configured. Set {MODEL_ENV_VAR} env var or [agent] model "
    "in ~/.physiclaw/config.toml — e.g. 'qwen/qwen3.6-plus' or "
    "'claude-code/claude-sonnet-4-6'."
)


def model_ref() -> str:
    """Resolve effective model ref: PHYSICLAW_MODEL > [agent] model > raise.

    Returns a `provider/model` string like `"qwen/qwen3.6-plus"`. Use
    `parse_model_ref` to split into the two parts. Display callers
    that want the source label too should call `model_ref_with_source`.
    """
    return model_ref_with_source()[0]


def model_ref_with_source() -> tuple[str, str]:
    """`(ref, source)` for the active model — same env > config order as
    `model_ref`. Raises `RuntimeError` when nothing is configured.
    `source` is a human-readable string for log / diagnostic output
    (`"PHYSICLAW_MODEL env"` or `"config.toml [agent] model"`).
    """
    if os.environ.get(MODEL_ENV_VAR):
        return os.environ[MODEL_ENV_VAR], f"{MODEL_ENV_VAR} env"
    if CONFIG.agent.model:
        return CONFIG.agent.model, "config.toml [agent] model"
    raise RuntimeError(_NO_MODEL_MSG)


def parse_model_ref(ref: str) -> tuple[str, str]:
    """Split `"provider/model-id"` on the FIRST slash.

    `"qwen/qwen3.6-plus"`  →  `("qwen", "qwen3.6-plus")`.
    `"openrouter/openai/gpt-5"`  →  `("openrouter", "openai/gpt-5")`.
    """
    if "/" not in ref:
        raise ValueError(
            f"model ref {ref!r} must be 'provider/model' "
            "(e.g. 'qwen/qwen3.6-plus')"
        )
    provider_id, model_id = ref.split("/", 1)
    if not (provider_id and model_id):
        raise ValueError(
            f"model ref {ref!r} has empty provider or model segment"
        )
    return provider_id, model_id


# --- Provider credential resolution. -----------------------------------------
# Order: env var(s) in declaration order > config.toml > None. Empty
# strings in config count as "unset" and fall through to the next layer.


def resolve_provider_key(
    env_vars: tuple[str, ...],
    config_key: str,
) -> tuple[str | None, str | None]:
    """Generic credential resolver. Returns ``(key, source)``; both
    ``None`` if not set anywhere.

    ``env_vars`` are checked in order (first hit wins). If none match,
    falls through to ``CONFIG.provider.<config_key>``. ``source`` is a
    human-readable string for diagnostic output (``"OPENAI_API_KEY env"``
    or ``"config.toml [provider] openai_api_key"``).
    """
    for var in env_vars:
        val = os.environ.get(var)
        if val:
            return val, f"{var} env"
    val = getattr(CONFIG.provider, config_key, "")
    if val:
        return val, f"config.toml [provider] {config_key}"
    return None, None


def qwen_api_key() -> str | None:
    """Resolve Qwen credential: QWEN_API_KEY > DASHSCOPE_API_KEY > config > None."""
    return resolve_provider_key(("QWEN_API_KEY", "DASHSCOPE_API_KEY"), "qwen_api_key")[0]


def qwen_api_key_source() -> str | None:
    """Where ``qwen_api_key()`` found a value, or None if unset."""
    return resolve_provider_key(("QWEN_API_KEY", "DASHSCOPE_API_KEY"), "qwen_api_key")[1]


__all__ = [
    "CONFIG",
    "Config",
    "ConfigError",
    "MODEL_ENV_VAR",
    "config_path",
    "get",
    "load",
    "model_ref",
    "model_ref_with_source",
    "parse_model_ref",
    "qwen_api_key",
    "resolve_provider_key",
    "qwen_api_key_source",
    "set_dotted",
    "to_toml",
    "unset_dotted",
    "write_default",
]
