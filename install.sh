#!/usr/bin/env bash
#
# PhysiClaw installer (macOS only for now).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/echosprint/PhysiClaw/main/install.sh | bash
#
# Optional:
#   PHYSICLAW_VERSION=0.0.3 curl -fsSL ... | bash   # pin a version
#   curl -fsSL ... | bash -s -- --no-onboard        # skip the first-run wizard
#   NO_COLOR=1 curl -fsSL ... | bash                # plain output
#
# What it does:
#   1. Checks you're on macOS.
#   2. Installs `uv` if missing (curl -fsSL https://astral.sh/uv/install.sh | sh).
#   3. Ensures Python 3.12 is available (no-op if already cached).
#   4. Installs/upgrades `physiclaw` as an isolated uv tool — shim goes in
#      ~/.local/bin/physiclaw. Idempotent; safe to re-run.
#   5. Runs `physiclaw onboard` unless you passed --no-onboard.

set -euo pipefail

# --- Parse flags up front so a typo doesn't cost a full install. ------------
NO_ONBOARD=0
for arg in "$@"; do
  case "$arg" in
    --no-onboard) NO_ONBOARD=1 ;;
    *)            printf '! unknown flag: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

# --- Colors: respect NO_COLOR and whether stdout is a TTY. ------------------
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else
  B=''; G=''; Y=''; R=''; N=''
fi
info() { printf '%s%s→%s %s\n'   "$B" "$G" "$N" "$*"; }
warn() { printf '%s%s!%s %s\n'   "$B" "$Y" "$N" "$*" >&2; }
die()  { printf '%s%s✗%s %s\n'   "$B" "$R" "$N" "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "PhysiClaw currently supports macOS only."

# uv drops its tool shims under ~/.local/bin.
export PATH="$HOME/.local/bin:$PATH"

FRESH_UV=0
if ! command -v uv >/dev/null 2>&1; then
  info "Installing uv (Python + tool manager)…"
  curl -fsSL https://astral.sh/uv/install.sh | sh
  FRESH_UV=1
fi

# Only hit uv's python-build-standalone manifest when 3.12 isn't already cached.
if ! uv python find 3.12 >/dev/null 2>&1; then
  info "Installing Python 3.12…"
  uv python install 3.12 --quiet
fi

VERSION="${PHYSICLAW_VERSION:-}"
PKG="physiclaw${VERSION:+==$VERSION}"  # ${var:+x} → "x" only when var is non-empty

info "Installing ${PKG}…"
uv tool install "$PKG" --python 3.12 --force >/dev/null

command -v physiclaw >/dev/null 2>&1 || {
  warn "physiclaw CLI not on PATH. Add this to ~/.zshrc and open a new terminal:"
  warn "    export PATH=\"\$HOME/.local/bin:\$PATH\""
  die "Exiting."
}

info "Installed: $(physiclaw --version)"

if [[ "$NO_ONBOARD" == "0" ]]; then
  printf '\n'
  # When piped from curl, stdin is the bash pipe (not a TTY). Run the wizard
  # non-interactively so typer.confirm() uses defaults instead of hanging.
  if [[ -t 0 ]]; then
    physiclaw onboard
  else
    physiclaw onboard --no-interactive
  fi
fi

printf '\n%s%s✓ Done.%s Run %sphysiclaw --help%s to explore.\n' \
  "$B" "$G" "$N" "$B" "$N"
if [[ "$FRESH_UV" == "1" ]]; then
  printf '%s%s!%s Open a new terminal so uv is on PATH in your interactive shell.\n' \
    "$B" "$Y" "$N"
fi
