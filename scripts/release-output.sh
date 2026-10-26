# Shared presentation for the Makefile release targets. Source with /bin/sh.
# Successful command output stays quiet; failures retain their full diagnostics.

release_bold='' release_dim='' release_green='' release_cyan='' release_red='' release_reset=''
if [ -t 1 ] && [ "${NO_COLOR+x}" != x ] && [ "${TERM:-}" != dumb ]; then
    release_bold=$(printf '\033[1m')
    release_dim=$(printf '\033[2m')
    release_green=$(printf '\033[32m')
    release_cyan=$(printf '\033[36m')
    release_red=$(printf '\033[31m')
    release_reset=$(printf '\033[0m')
fi

release_version() {
    sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml
}

release_stage() {
    release_started=$(date +%s)
    printf '\n  %s%s%s\n' "$release_cyan" "$1" "$release_reset"
}

release_done() {
    release_elapsed=$(( $(date +%s) - release_started ))
    printf '        %s✓%s %s %s(%ss)%s\n' \
        "$release_green" "$release_reset" "$1" "$release_dim" "$release_elapsed" "$release_reset"
}

release_error() {
    printf '\n  %s✗%s %s\n' "$release_red" "$release_reset" "$1" >&2
    exit "${2:-1}"
}

release_run() (
    release_status=0
    if [ "${VERBOSE:-0}" = 1 ]; then
        printf '        %s$ %s%s\n' "$release_dim" "$*" "$release_reset"
        "$@" || release_status=$?
    else
        release_log=$(mktemp "${TMPDIR:-/tmp}/physiclaw-release.XXXXXX")
        trap 'rm -f "$release_log"' EXIT
        trap 'exit 130' INT
        trap 'exit 143' TERM
        "$@" >"$release_log" 2>&1 || release_status=$?
        [ "$release_status" -eq 0 ] || cat "$release_log" >&2
    fi
    [ "$release_status" -eq 0 ] || release_error "Failed (exit $release_status): $*" "$release_status"
)

release_artifact() {
    [ -f "$1" ] || release_error "Expected artifact missing: $1"
    release_size=$(wc -c <"$1" | awk '{
        if ($1 >= 1048576) printf "%.1f MiB", $1 / 1048576;
        else printf "%.1f KiB", $1 / 1024;
    }')
    printf '        %s  %s%s%s\n' "$1" "$release_dim" "$release_size" "$release_reset"
}

release_summary() {
    printf '\n  %s✓ Released physiclaw %s%s\n\n' "$release_green" "$1" "$release_reset"
    printf '  PyPI     https://pypi.org/project/physiclaw/%s/\n' "$1"
    printf '  Git tag  https://github.com/physiclaw/PhysiClaw/tree/v%s\n\n' "$1"
}
