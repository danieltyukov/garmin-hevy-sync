# Installs garmin-hevy-sync on Windows.
#
#   powershell -ExecutionPolicy ByPass -c "irm https://danieltyukov.github.io/garmin-hevy-sync/install.ps1 | iex"
#
# Environment:
#   GH_SYNC_VERSION    Install this release (e.g. 0.2.0) instead of the latest.
#   GH_SYNC_SOURCE     Install from this source instead (a path or URL; for testing).
#   GH_SYNC_NO_SETUP   Set to 1 to install only and skip the interactive setup.
#
# What it does: installs uv (Astral's Python tool manager) if it is missing,
# uses it to install garmin-hevy-sync in its own environment with its own
# Python 3.12, then starts the interactive setup. Re-running it upgrades an
# existing install; your settings are kept.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Repo = 'danieltyukov/garmin-hevy-sync'

function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($candidate in @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe")) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

# ---------------------------------------------------------------------- uv
$uv = Find-Uv
if (-not $uv) {
    Write-Host 'Installing uv, which manages the Python environment for garmin-hevy-sync.'
    Write-Host '(https://docs.astral.sh/uv/)'
    powershell -NoProfile -ExecutionPolicy ByPass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
    $uv = Find-Uv
    if (-not $uv) { throw 'uv was installed but could not be found. Open a new terminal and run this again.' }
}

# ------------------------------------------------------------------ source
if ($env:GH_SYNC_SOURCE) {
    $Source = $env:GH_SYNC_SOURCE
} else {
    $Version = $env:GH_SYNC_VERSION
    if (-not $Version) {
        try {
            $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ 'User-Agent' = 'garmin-hevy-sync-installer' }
            $Version = $release.tag_name
        } catch {
            $Version = $null
        }
    }
    if ($Version) {
        $Version = $Version.TrimStart('v')
        $Source = "https://github.com/$Repo/archive/refs/tags/v$Version.tar.gz"
        Write-Host "Installing garmin-hevy-sync $Version"
    } else {
        $Source = "https://github.com/$Repo/archive/refs/heads/main.tar.gz"
        Write-Host 'Installing garmin-hevy-sync from the main branch'
    }
}

if ($Source -match '^https?://') { $Spec = "garmin-hevy-sync @ $Source" } else { $Spec = $Source }
& $uv tool install --force --python 3.12 $Spec
if ($LASTEXITCODE -ne 0) { throw "uv tool install failed with exit code $LASTEXITCODE" }

# Make sure the command is on PATH in new terminals.
& $uv tool update-shell *> $null
$BinDir = (& $uv tool dir --bin).Trim()
$Exe = Join-Path $BinDir 'garmin-hevy-sync.exe'
if (-not (Test-Path $Exe)) { throw "Installed, but $Exe is missing. Run 'uv tool list' to investigate." }

Write-Host ''
Write-Host "Installed: $(& $Exe --version)"
if (-not (($env:Path -split ';') -contains $BinDir)) {
    Write-Host "Open a new terminal to use the garmin-hevy-sync command (it lives in $BinDir)."
}

if ($env:GH_SYNC_NO_SETUP -eq '1') {
    Write-Host "Next: run 'garmin-hevy-sync setup'."
    return
}

Write-Host ''
& $Exe setup
