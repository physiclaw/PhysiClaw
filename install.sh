#!/usr/bin/env bash
#
# PhysiClaw installer (macOS only for now).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/echosprint/PhysiClaw/main/install.sh | bash
#
# Optional:
#   PHYSICLAW_VERSION=0.0.3 curl -fsSL ... | bash   # pin a version
#   NO_COLOR=1 curl -fsSL ... | bash                # plain output
#
# What it does (hardware setup is still a separate step at the end):
#   1. Checks you're on macOS.
#   2. Installs `uv` if missing (curl -fsSL https://astral.sh/uv/install.sh | sh).
#   3. Ensures Python 3.12 is available (no-op if already cached).
#   4. Installs `physiclaw` (small — no heavy ML deps in the package).
#   5. Runs `physiclaw setup local-vision-model` to convert the upstream
#      PyTorch icon-detector weights to ONNX. The conversion runs inside
#      an ephemeral `uv run --with` env in a scratch dir under
#      ~/.physiclaw/models/, which is rm -rf'd on success. The heavy
#      conversion deps never enter the physiclaw install.
#      Idempotent; re-running this script no-ops conversion if the ONNX
#      is already cached.

set -euo pipefail

if (( $# > 0 )); then
  printf '! unknown flag: %s\n' "$1" >&2
  exit 2
fi

# --- Colors: respect NO_COLOR and whether stdout is a TTY. ------------------
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else
  B=''; G=''; Y=''; R=''; N=''
fi
info() { printf '%s%s→%s %s\n'   "$B" "$G" "$N" "$*"; }
warn() { printf '%s%s!%s %s\n'   "$B" "$Y" "$N" "$*" >&2; }
die()  { printf '%s%s✗%s %s\n'   "$B" "$R" "$N" "$*" >&2; exit 1; }

if [[ "$(uname -s)" != "Darwin" ]]; then
  die "install.sh is for macOS. On Windows, use install.ps1 (irm <url>/install.ps1 | iex)."
fi

# uv drops its tool shims under ~/.local/bin.
export PATH="$HOME/.local/bin:$PATH"

# git is only needed by `physiclaw skills install` (clones a repo with
# skills/<name>/SKILL.md layout). Escalation: native CLT installer (ships
# git) → brew if already present → docs warn. Warns-and-continues on
# miss so users without CLT/brew can still finish the physiclaw install.
if ! command -v git >/dev/null 2>&1; then
  if ! xcode-select -p >/dev/null 2>&1; then
    info "Triggering Xcode Command Line Tools installer (ships git)…"
    xcode-select --install >/dev/null 2>&1 || true
    warn "Click Install in the popup and wait ~5 min, then re-run this script."
    warn "Continuing — \`physiclaw skills install\` stays unavailable until CLT finishes."
  elif command -v brew >/dev/null 2>&1; then
    info "CLT present but git missing — installing via Homebrew…"
    brew install git >/dev/null
  else
    warn "git not found. Run one of:"
    warn "    xcode-select --install    # macOS Command Line Tools (native)"
    warn "    brew install git          # if you install Homebrew first"
  fi
fi

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
SPEC_PLAIN="physiclaw${VERSION:+==$VERSION}"

# Step 4: install physiclaw. No [vision] extra — conversion deps live in
# a uv-managed ephemeral env, not in the physiclaw install. `--refresh`
# invalidates uv's cached PyPI metadata so re-runs always resolve to the
# actual latest version of physiclaw, not whatever was in the cache from
# the last `uv tool install` an hour ago.
info "Installing ${SPEC_PLAIN}…"
uv tool install "$SPEC_PLAIN" --python 3.12 --force --refresh >/dev/null

command -v physiclaw >/dev/null 2>&1 || {
  warn "physiclaw CLI not on PATH. Add this to ~/.zshrc and open a new terminal:"
  warn "    export PATH=\"\$HOME/.local/bin:\$PATH\""
  die "Exiting."
}

info "Installed: $(physiclaw --version)"

# Step 5: convert PyTorch weights to ONNX in an ephemeral uv env (no-op
# if already cached). The setup command creates a scratch dir under
# ~/.physiclaw/models/, runs the conversion via `uv run --with`, moves
# the ONNX into place, and rm -rf's the scratch dir.
info "Converting vision model to ONNX (one-time, ~30 s)…"
physiclaw setup local-vision-model

printf '\n%s%s✓ Done.%s Next steps:\n' "$B" "$G" "$N"
printf '    %sphysiclaw doctor%s            check your environment\n' "$B" "$N"
printf '    %sphysiclaw setup hardware%s    calibrate the arm + camera (plug them in first)\n' "$B" "$N"
if [[ "$FRESH_UV" == "1" ]]; then
  printf '\n%s%s!%s Open a new terminal so uv is on PATH in your interactive shell.\n' \
    "$B" "$Y" "$N"
fi
