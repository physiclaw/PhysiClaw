# PhysiClaw installer (Windows 11).
#
# Usage (recommended - runs in a child shell so the installer can't crash yours):
#   powershell -ExecutionPolicy ByPass -c "iwr -useb https://physiclaw.ai/install.ps1 | iex"
#
# Also works, but if the install fails the calling shell may exit:
#   iwr -useb https://physiclaw.ai/install.ps1 | iex
#
# Optional (set before invoking):
#   $env:PHYSICLAW_VERSION = '0.0.5'   # pin a version
#   $env:PHYSICLAW_DRY_RUN = '1'       # same as -DryRun
#   $env:NO_COLOR = '1'                # plain output
#
# To pass options through ``iwr | iex``, wrap the script in a scriptblock.
# Use .Content here - iwr returns a response object, not a string:
#   & ([scriptblock]::Create((iwr -useb https://physiclaw.ai/install.ps1).Content)) -Version 0.0.5 -DryRun
#
# What it does (hardware setup is still a separate step at the end):
#   1. Checks you're on Windows.
#   2. Installs `uv` if missing (via astral.sh installer, run in a child shell
#      so its `exit 1` on failure never reaches your session).
#   3. Ensures Python 3.12 is available (no-op if already cached).
#   4. Installs `physiclaw` (small - no heavy ML deps in the package).
#   5. Runs `physiclaw setup local-vision-model` to install the icon-detector
#      model. By default it fetches a prebuilt ONNX (fast, no extra deps); if
#      that's unreachable it falls back to downloading the upstream PyTorch
#      weights and converting them in an ephemeral `uv run --with` env.
#      Idempotent; no-ops if the ONNX is already cached.
#
# Prerequisites:
#   - PowerShell 5.1+ (ships with Windows 11) or PowerShell 7+.
#   - Execution policy must allow scripts. If you see a policy warning:
#       Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

[CmdletBinding()]
param(
    [string]$Version,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 can default to TLS 1.0, which astral.sh and PyPI
# refuse - force TLS 1.2+ before any web request. No-op on PowerShell 7+,
# which already negotiates TLS 1.2/1.3 from the OS.
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

# Env var mirrors the -DryRun flag (handy when invoking via ``iwr | iex``,
# where passing a switch needs the scriptblock wrapper).
if (-not $DryRun -and $env:PHYSICLAW_DRY_RUN -eq '1') { $DryRun = $true }

# --- Colors: respect NO_COLOR and whether the host is interactive. ---------
$useColor = -not $env:NO_COLOR -and $Host.UI.RawUI -ne $null
function Info($msg) { if ($useColor) { Write-Host "-> $msg" -ForegroundColor Green } else { Write-Host "-> $msg" } }
function Warn($msg) { if ($useColor) { Write-Host "! $msg" -ForegroundColor Yellow } else { Write-Host "! $msg" } }
function Die($msg)  {
    # `throw` so this script terminates without killing the user's shell
    # when invoked via ``iwr | iex``. `exit 1` would tear down the host
    # PowerShell process - `iex` runs in the caller's scope.
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
    # skills/<name>/SKILL.md layout). Warn-and-continue - most users don't
    # need skill installation immediately.
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Warn "git not found. Install one of:"
        Warn "    winget install --id Git.Git -e             # winget (built into Windows 11)"
        Warn "    https://git-scm.com/download/win           # official installer"
        Warn "Continuing - ``physiclaw skills install`` stays unavailable until git is on PATH."
    }

    # -Version flag wins over the env var, which wins over "latest".
    $version = if ($Version) { $Version } else { $env:PHYSICLAW_VERSION }
    $specPlain = if ([string]::IsNullOrEmpty($version)) { 'physiclaw' } else { "physiclaw==$version" }

    if ($DryRun) {
        Info "Dry run - would:"
        Write-Host "    install uv (if missing) from https://astral.sh/uv/install.ps1"
        Write-Host "    uv python install 3.12 (if not cached)"
        Write-Host "    uv tool install $specPlain --python 3.12 --force --refresh"
        Write-Host "    physiclaw setup local-vision-model"
        if ($useColor) { Write-Host "[OK] Dry run complete. No changes made." -ForegroundColor Green }
        else           { Write-Host "[OK] Dry run complete. No changes made." }
        $global:LASTEXITCODE = 0
        return
    }

    $freshUv = $false
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Info "Installing uv (Python + tool manager)..."

        # Run uv's installer in a CHILD PowerShell process. Its top-level
        # ``catch { exit 1 }`` (line ~653 of astral.sh's install.ps1) would
        # otherwise tear down our host - and the user's shell, if our script
        # was loaded via ``iwr | iex``. Download to a temp file, run with
        # `-File`, capture the child's exit code.
        $uvTempDir = Join-Path ([IO.Path]::GetTempPath()) ("physiclaw-uv-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $uvTempDir | Out-Null
        $uvScript = Join-Path $uvTempDir 'install.ps1'
        try {
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
            & $psHost -NoProfile -ExecutionPolicy ByPass -File $uvScript
            $uvExit = $LASTEXITCODE
        } finally {
            Remove-Item -LiteralPath $uvTempDir -Recurse -Force -ErrorAction SilentlyContinue
        }

        if ($uvExit -ne 0) {
            Die @"
uv installer exited with code $uvExit.

Common causes on Windows:
  - Windows Defender / SmartScreen blocked the download or the new uv.exe.
    Check Defender's Protection History and allow it, or install uv manually:
      https://docs.astral.sh/uv/getting-started/installation/
  - Corporate proxy / VPN is intercepting HTTPS to astral.sh
    (TLS errors, MITM cert).
  - %USERPROFILE%\.local\bin\uv.exe is locked by another process - close
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
        Info "Installing Python 3.12..."
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

    Info "Installing $specPlain..."
    # `--refresh` invalidates uv's cached PyPI metadata so re-runs always
    # resolve to the actual latest version, not whatever was in the cache
    # from a previous `uv tool install` an hour ago.
    # Keep uv's diagnostics visible and treat its failure as authoritative:
    # a previously installed CLI may still run after an unsuccessful update.
    & uv tool install $specPlain --python 3.12 --force --refresh
    $installExit = $LASTEXITCODE
    if ($installExit -ne 0) {
        Die "``uv tool install $specPlain`` failed (exit $installExit). See uv output above and retry. A previous installation may still be available."
    }

    # Also verify that the newly installed shim is on PATH and runnable.
    $physiclawCmd = Get-Command physiclaw -ErrorAction SilentlyContinue
    $verifiedVersion = $null
    $versionFailure = 'No version output was returned.'
    if ($physiclawCmd) {
        try {
            $verOutput = & physiclaw --version 2>&1
            $versionExit = $LASTEXITCODE
            if ($versionExit -eq 0 -and $verOutput) {
                $verifiedVersion = ($verOutput | Select-Object -First 1).ToString()
            } elseif ($versionExit -ne 0) {
                $versionFailure = "Exit code: $versionExit`n$($verOutput | Out-String)"
            }
        } catch {
            $versionFailure = $_.Exception.Message
        }
    }

    if (-not $verifiedVersion) {
        # Installation succeeded, but the CLI is missing or could not be verified.
        if (-not $physiclawCmd) {
            Warn "uv installed $specPlain, but ``physiclaw`` isn't on PATH for this session."
            Warn ""
            Warn "PATH may not be refreshed. Add this to your PowerShell profile"
            Warn "(`$PROFILE`), then open a new shell:"
            Warn "    `$env:PATH = `"`$env:USERPROFILE\.local\bin;`$env:PATH`""
            Warn ""
            Warn "Or run it directly without changing your profile:"
            Warn "    & `"`$env:USERPROFILE\.local\bin\physiclaw.exe`" --version"
            Die "``physiclaw`` not on PATH after install. See uv output above + the fixes above."
        }

        Die @"
uv installed $specPlain, but ``physiclaw --version`` could not be verified.

$versionFailure

To diagnose, run the CLI directly:
  physiclaw --version

To reinstall with verbose output:
  uv tool install $specPlain --python 3.12 --force --verbose
"@
    }

    Info "Installed: $verifiedVersion"

    # Step 5: install the icon-detector model - fetch the prebuilt ONNX, or
    # fall back to download + convert. No-op if already cached.
    Info "Installing the vision model (one-time)..."
    & physiclaw setup local-vision-model
    if ($LASTEXITCODE -ne 0) {
        # Non-fatal: physiclaw itself is installed by now. The model download
        # needs huggingface.co, which a locked-down network may block - warn
        # and point to the re-run rather than aborting a complete install.
        Warn "Vision model not set up - the download or convert step failed (see above)."
        Warn "physiclaw itself is installed. Re-run this once your machine can reach"
        Warn "huggingface.co:"
        Warn "    physiclaw setup local-vision-model"
    }

    Write-Host ""
    if ($useColor) { Write-Host "[OK] Done." -ForegroundColor Green -NoNewline; Write-Host " Next steps:" }
    else           { Write-Host "[OK] Done. Next steps:" }
    Write-Host "    physiclaw models key <provider>        add your LLM API key"
    Write-Host "    physiclaw models use <provider/model>  pick the model the agent runs on"
    Write-Host "    physiclaw doctor                       check your environment"
    Write-Host "    physiclaw                              start the server - opens the hardware-setup wizard"
    if ($freshUv) {
        Write-Host ""
        Warn "Open a new PowerShell window so uv is on PATH in your interactive shell."
    }
    $global:LASTEXITCODE = 0
}
catch {
    # Single, clean failure block. Don't dump PowerShell's script-position
    # stack trace - users don't need that to fix the problem; the message
    # already says what went wrong.
    $msg = $_.Exception.Message
    Write-Host ""
    if ($useColor) { Write-Host "[X] Installation failed." -ForegroundColor Red }
    else           { Write-Host "[X] Installation failed." }
    Write-Host ""
    Write-Host $msg
    Write-Host ""
    # Set exit status without exiting the host process. `return` ends the
    # iex'd block; $LASTEXITCODE lets a caller check.
    $global:LASTEXITCODE = 1
    return
}
