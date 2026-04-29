# PhysiClaw installer (Windows 11).
#
# Usage (PowerShell):
#   irm https://raw.githubusercontent.com/echosprint/PhysiClaw/main/install.ps1 | iex
#
# Optional:
#   $env:PHYSICLAW_VERSION = '0.0.4'; irm <url> | iex   # pin a version
#   $env:NO_COLOR = '1'; irm <url> | iex                # plain output
#
# What it does (and only this — hardware/model setup is separate):
#   1. Checks you're on Windows.
#   2. Installs `uv` if missing (irm https://astral.sh/uv/install.ps1 | iex).
#   3. Ensures Python 3.12 is available (no-op if already cached).
#   4. Installs/upgrades `physiclaw` as an isolated uv tool — shim goes in
#      %USERPROFILE%\.local\bin\physiclaw.exe. Idempotent; safe to re-run.
#
# Prerequisites:
#   - PowerShell 5.1+ (ships with Windows 11) or PowerShell 7+.
#   - Execution policy must allow scripts. If `iex` errors with a policy
#     warning, run once: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

# --- Colors: respect NO_COLOR and whether the host is interactive. ---------
$useColor = -not $env:NO_COLOR -and $Host.UI.RawUI -ne $null
function Info($msg) { if ($useColor) { Write-Host "→ $msg" -ForegroundColor Green } else { Write-Host "→ $msg" } }
function Warn($msg) { if ($useColor) { Write-Host "! $msg" -ForegroundColor Yellow } else { Write-Host "! $msg" } }
function Die($msg)  {
    if ($useColor) { Write-Host "✗ $msg" -ForegroundColor Red } else { Write-Host "✗ $msg" }
    exit 1
}

if (-not $IsWindows -and $PSVersionTable.PSVersion.Major -ge 6) {
    Die "PhysiClaw install.ps1 is for Windows. On macOS, use install.sh."
}

# uv drops its tool shims under %USERPROFILE%\.local\bin.
$localBin = Join-Path $env:USERPROFILE '.local\bin'
if (Test-Path $localBin) {
    $env:PATH = "$localBin;$env:PATH"
}

# git is only needed by `physiclaw skills install` (clones a repo with
# skills/<name>/SKILL.md layout). Warn-and-continue if missing — most
# users don't need skill installation immediately.
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Warn "git not found. Install one of:"
    Warn "    winget install --id Git.Git -e             # winget (built into Windows 11)"
    Warn "    https://git-scm.com/download/win           # official installer"
    Warn "Continuing — ``physiclaw skills install`` stays unavailable until git is on PATH."
}

$freshUv = $false
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Info "Installing uv (Python + tool manager)…"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $freshUv = $true
    # uv's installer adds %USERPROFILE%\.local\bin to PATH for new shells;
    # patch the current process so the next command finds `uv`.
    $uvBin = Join-Path $env:USERPROFILE '.local\bin'
    if (Test-Path $uvBin) {
        $env:PATH = "$uvBin;$env:PATH"
    }
}

# Only hit uv's python-build-standalone manifest when 3.12 isn't already cached.
$pythonOk = $false
try {
    & uv python find 3.12 *> $null
    if ($LASTEXITCODE -eq 0) { $pythonOk = $true }
} catch { $pythonOk = $false }
if (-not $pythonOk) {
    Info "Installing Python 3.12…"
    & uv python install 3.12 --quiet
    if ($LASTEXITCODE -ne 0) { Die "uv python install 3.12 failed." }
}

$version = $env:PHYSICLAW_VERSION
if ([string]::IsNullOrEmpty($version)) {
    $pkg = 'physiclaw'
} else {
    $pkg = "physiclaw==$version"
}

Info "Installing $pkg…"
& uv tool install $pkg --python 3.12 --force *> $null
if ($LASTEXITCODE -ne 0) { Die "uv tool install $pkg failed." }

if (-not (Get-Command physiclaw -ErrorAction SilentlyContinue)) {
    Warn "physiclaw CLI not on PATH. Add this to your PowerShell profile and reopen:"
    Warn "    `$env:PATH = `"`$env:USERPROFILE\.local\bin;`$env:PATH`""
    Die "Exiting."
}

$ver = & physiclaw --version
Info "Installed: $ver"

Write-Host ""
if ($useColor) { Write-Host "✓ Done." -ForegroundColor Green -NoNewline; Write-Host " Next steps:" }
else           { Write-Host "✓ Done. Next steps:" }
Write-Host "    physiclaw doctor                    check your environment"
Write-Host "    physiclaw setup local-vision-model  download the icon detector (~100 MB)"
Write-Host "    physiclaw setup hardware            calibrate the arm + camera (plug them in first)"
if ($freshUv) {
    Write-Host ""
    Warn "Open a new PowerShell window so uv is on PATH in your interactive shell."
}
