"""Runtime config constants — leaf module, zero heavy imports.

Importable from anywhere (including physiclaw.main) without dragging in
engine/provider/httpx. The launcher composes its full resolution logic on
top of these; physiclaw.main only needs the constants for log labels.
"""

# The env var that selects the engine + provider stack. Unset = default.
PROVIDER_ENV_VAR = "PHYSICLAW_PROVIDER"

# Sentinel value meaning "use the external Claude Code subprocess instead
# of the in-process physiclaw engine". Same string is the default when
# PROVIDER_ENV_VAR is unset.
EXTERNAL = "claude-code"
PROVIDER_DEFAULT = EXTERNAL
