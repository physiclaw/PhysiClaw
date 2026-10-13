"""Shared scalar layer for the conductor's YAML spec files.

A pack's manifest (APP.yml) follows the macro-file conventions — same
naming rule, same prose caps, same strict-string coercion hints, same
YAML 1.2 pure loader. The rules' one true home is the macro layer
(`macros/model.py` constants, `check_name`'s wording); this module
re-exports them for the conductor and binds the scalar validators to
each spec's own error class, so `pages.py` and `model.py` keep their
distinct catch surfaces without re-spelling the validation:

    _require_str, _prose, _opt_prose, _check_name = specfile.bind(PagesError)
"""

from typing import Any, Callable

from physiclaw.common.placeholders import resolve_data_placeholders
from physiclaw.common.yaml import safe_yaml
from physiclaw.macros.model import (
    INPUT_NAME_RE as INPUT_NAME_RE,
)
from physiclaw.macros.model import (
    MAX_NAME_LEN as MAX_NAME_LEN,
)
from physiclaw.macros.model import (
    MAX_PROSE_LEN as MAX_PROSE_LEN,
)
from physiclaw.macros.model import (
    NAME_RE as NAME_RE,
)
from physiclaw.macros.model import MacroError
from physiclaw.macros.model import check_name as _model_check_name


class SpecError(ValueError):
    """A pack's spec file is invalid — the one base every spec error
    (`PlaybookError`, `PagesError`) derives from, so a skin catches one
    class. Messages are user-facing and name their place in the file."""


# YAML 1.2 safe loader, pure-python, load-only — the same construction as
# `macros/parse.py`. `load_yaml` below is the resolved-spec door; the
# install flow's raw template read (`load.files.read_template_manifest`)
# shares the instance.
yaml_loader = safe_yaml()


def load_yaml(
    text: str,
    error_cls: type[Exception],
    where: str = "",
    values: dict[str, str] | None = None,
) -> Any:
    """The one load ritual every spec door shares: YAML 1.2 load,
    reject aliases before expansion, fill string `<<TOKEN>>`s
    from the local values file, wrap errors in the door's own
    error class. The alias guard is shared with the macro parser: inline macros
    put clause parsing — which materializes a Clause per path, the
    alias-bomb ride — behind these doors, and running it HERE keeps the
    grammar door-independent (a text that parses at the test/tooling
    door must not fail only at the live pack file)."""
    prefix = f"{where}: " if where else ""
    try:
        data = yaml_loader.load(text)
    except Exception as e:  # loader errors are not confined to YAMLError
        raise error_cls(f"{prefix}invalid YAML: {e or type(e).__name__}") from e
    return resolve_data_placeholders(data, error_cls, values)


def bind(
    err: type[ValueError],
) -> tuple[
    Callable[[Any, str], str],
    Callable[..., str],
    Callable[[Any, str], str | None],
    Callable[[Any, str], None],
]:
    """The four scalar terminals, raising `err`. Wording matches the macro
    parser's — one user-facing voice for one rule set."""

    def require_str(value: Any, where: str) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is None or isinstance(value, str):
            raise err(f"{where} is required and must be a non-empty string")
        raise err(
            f"{where} must be a string but YAML parsed {value!r} "
            f'({type(value).__name__}) — quote it: "{value}"'
        )

    def prose(value: Any, where: str, *, lines: int = 1) -> str:
        """Bounded prose: one line by default, each line under
        MAX_PROSE_LEN; `lines` admits that many (a message to the user)."""
        text = require_str(value, where)
        held = text.splitlines()
        if lines == 1 and len(held) > 1:
            raise err(f"{where} must be a single line")
        if len(held) > lines:
            raise err(f"{where} has {len(held)} lines (max {lines})")
        for line in held:
            if len(line) > MAX_PROSE_LEN:
                raise err(
                    f"{where}{' has a line that' if lines > 1 else ''} is "
                    f"{len(line)} characters (max {MAX_PROSE_LEN})"
                )
        return text

    def opt_prose(value: Any, where: str) -> str | None:
        return None if value is None else prose(value, where)

    def name_check(name: Any, where: str) -> None:
        if not isinstance(name, str):
            raise err(f"{where} must be a string (got {name!r})")
        try:
            # The macro rule verbatim (wording included), translated to
            # this spec's error class at the one seam.
            _model_check_name(name, where)
        except MacroError as e:
            raise err(str(e)) from e

    return require_str, prose, opt_prose, name_check
