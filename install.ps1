# PhysiClaw installer (Windows 11).
#
# Usage (recommended — runs in a child shell so the installer can't crash yours):
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/echosprint/PhysiClaw/main/install.ps1 | iex"
#
# Also works, but if the install fails the calling shell may exit:
#   irm https://raw.githubusercontent.com/echosprint/PhysiClaw/main/install.ps1 | iex
#
# Optional (set before invoking):
#   $env:PHYSICLAW_VERSION = '0.0.5'   # pin a version
#   $env:NO_COLOR = '1'                # plain output
#
# What it does (and only this — hardware/model setup is separate):
#   1. Checks you're on Windows.
#   2. Installs `uv` if missing (via astral.sh installer, run in a child shell
#      so its `exit 1` on failure never reaches your session).
#   3. Ensures Python 3.12 is available (no-op if already cached).
#   4. Installs/upgrades `physiclaw` as an isolated uv tool — shim goes in
#      %USERPROFILE%\.local\bin\physiclaw.exe. Idempotent; safe to re-run.
#
# Prerequisites:
#   - PowerShell 5.1+ (ships with Windows 11) or PowerShell 7+.
#   - Execution policy must allow scripts. If you see a policy warning:
#       Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

# --- Colors: respect NO_COLOR and whether the host is interactive. ---------
$useColor = -not $env:NO_COLOR -and $Host.UI.RawUI -ne $null
function Info($msg) { if ($useColor) { Write-Host "→ $msg" -ForegroundColor Green } else { Write-Host "→ $msg" } }
function Warn($msg) { if ($useColor) { Write-Host "! $msg" -ForegroundColor Yellow } else { Write-Host "! $msg" } }
function Die($msg)  {
    # `throw` so this script terminates without killing the user's shell
    # when invoked via ``irm | iex``. `exit 1` would tear down the host
    # PowerShell process — `iex` runs in the caller's scope.
    throw $msg
}

try {
    if (-not $IsWindows -and $PSVersionTable.PSVersion.Major -ge 6) {
        Die "PhysiClaw install.ps1 is for Windows. On macOS, use install.sh."
    }

    # uv drops its tool shims under %USERPROFILE%\.local\bin.
    $localBin = Join-Path $env:USERPROFILE '.local\bin'
    if (Test-Path $localBin) {
        $env:PATH = "$localBin;$env:PATH"
    }

    # git is only needed by `physiclaw skills install` (clones a repo with
    # skills/<name>/SKILL.md layout). Warn-and-continue — most users don't
    # need skill installation immediately.
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Warn "git not found. Install one of:"
        Warn "    winget install --id Git.Git -e             # winget (built into Windows 11)"
        Warn "    https://git-scm.com/download/win           # official installer"
        Warn "Continuing — ``physiclaw skills install`` stays unavailable until git is on PATH."
    }

    $freshUv = $false
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Info "Installing uv (Python + tool manager)…"

        # Run uv's installer in a CHILD PowerShell process. Its top-level
        # ``catch { exit 1 }`` (line ~653 of astral.sh's install.ps1) would
        # otherwise tear down our host — and the user's shell, if our script
        # was loaded via ``irm | iex``. Download to a temp file, run with
        # `-File`, capture the child's exit code.
        $uvScript = Join-Path $env:TEMP "physiclaw-uv-install.ps1"
        try {
            Invoke-WebRequest -Uri https://astral.sh/uv/install.ps1 `
                              -OutFile $uvScript -UseBasicParsing
        } catch {
            Die @"
Could not download the uv installer from astral.sh.
  Cause: $($_.Exception.Message)

Likely causes:
  - No internet, or a corporate proxy / firewall is blocking HTTPS to astral.sh
  - DNS or routing is broken for astral.sh

Fix the connection and retry, or install uv manually first:
  https://docs.astral.sh/uv/getting-started/installation/
"@
        }

        # Use the same PowerShell host that's running this script.
        $psHost = (Get-Process -Id $PID).Path
        & $psHost -NoProfile -ExecutionPolicy Bypass -File $uvScript
        $uvExit = $LASTEXITCODE
        Remove-Item $uvScript -ErrorAction SilentlyContinue

        if ($uvExit -ne 0) {
            Die @"
uv installer exited with code $uvExit.

Common causes on Windows:
  - Windows Defender / SmartScreen blocked the download or the new uv.exe.
    Check Defender's Protection History and allow it, or install uv manually:
      https://docs.astral.sh/uv/getting-started/installation/
  - Corporate proxy / VPN is intercepting HTTPS to astral.sh
    (TLS errors, MITM cert).
  - %USERPROFILE%\.local\bin\uv.exe is locked by another process — close
    any running ``uv`` / ``physiclaw`` and retry.
  - Restricted execution policy. Run once, then retry:
      Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
"@
        }

        $freshUv = $true
        # uv adds %USERPROFILE%\.local\bin to PATH for new shells; patch our
        # current process so the next command finds `uv`.
        if (Test-Path $localBin) {
            $env:PATH = "$localBin;$env:PATH"
        }
    }

    # Only hit uv's python-build-standalone manifest when 3.12 isn't cached.
    $pythonOk = $false
    try {
        & uv python find 3.12 *> $null
        if ($LASTEXITCODE -eq 0) { $pythonOk = $true }
    } catch { $pythonOk = $false }
    if (-not $pythonOk) {
        Info "Installing Python 3.12…"
        & uv python install 3.12 --quiet
        if ($LASTEXITCODE -ne 0) {
            Die @"
``uv python install 3.12`` failed (exit $LASTEXITCODE).

Likely causes:
  - Network issue downloading the Python build
    (~30 MB from python-build-standalone).
  - Disk full, or %USERPROFILE%\.local\share\uv is read-only.

uv caches per-version, so re-running this script when the connection is
stable will resume where it stopped.
"@
        }
    }

    $version = $env:PHYSICLAW_VERSION
    if ([string]::IsNullOrEmpty($version)) {
        $pkg = 'physiclaw'
    } else {
        $pkg = "physiclaw==$version"
    }

    Info "Installing $pkg…"
    & uv tool install $pkg --python 3.12 --force *> $null
    if ($LASTEXITCODE -ne 0) {
        $verHint = if ([string]::IsNullOrEmpty($version)) { 'physiclaw' } else { "physiclaw==$version" }
        Die @"
``uv tool install $pkg`` failed (exit $LASTEXITCODE).

Likely causes:
  - Pinned version does not exist. Check available versions:
      https://pypi.org/project/physiclaw/#history
  - Network blip while downloading wheels — retry.
  - A native build dep (e.g. opencv-python) failed to install. Run the
    command manually with output visible to see the real error:
      uv tool install $verHint --python 3.12 --force
"@
    }

    if (-not (Get-Command physiclaw -ErrorAction SilentlyContinue)) {
        Warn "Install succeeded but ``physiclaw`` isn't on PATH for this session."
        Warn ""
        Warn "Add this to your PowerShell profile (`$PROFILE`), then open a new shell:"
        Warn "    `$env:PATH = `"`$env:USERPROFILE\.local\bin;`$env:PATH`""
        Warn ""
        Warn "Or run it directly without changing your profile:"
        Warn "    & `"`$env:USERPROFILE\.local\bin\physiclaw.exe`" --version"
        Die "``physiclaw`` not found on PATH. See above for fixes."
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
}
catch {
    # Single, clean failure block. Don't dump PowerShell's script-position
    # stack trace — users don't need that to fix the problem; the message
    # already says what went wrong.
    $msg = $_.Exception.Message
    Write-Host ""
    if ($useColor) { Write-Host "✗ Installation failed." -ForegroundColor Red }
    else           { Write-Host "✗ Installation failed." }
    Write-Host ""
    Write-Host $msg
    Write-Host ""
    # Set exit status without exiting the host process. `return` ends the
    # iex'd block; $LASTEXITCODE lets a caller check.
    $global:LASTEXITCODE = 1
    return
}
