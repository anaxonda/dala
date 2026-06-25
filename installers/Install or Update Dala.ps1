param(
    [switch]$HeadlessBrowser,
    [switch]$NoPrompt
)

$ErrorActionPreference = "Stop"

$installerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $installerDir
$installScript = Join-Path $repoRoot "scripts\install-dala.ps1"

if (-not (Test-Path $installScript)) {
    throw "Could not find installer helper: $installScript"
}

if (-not $HeadlessBrowser -and -not $NoPrompt) {
    Write-Host ""
    Write-Host "Install optional headless browser support?"
    Write-Host "This lets the Dala server control Chrome/Chromium in the background."
    Write-Host "It is needed for PDF output and some JavaScript-heavy pages."
    Write-Host "It is separate from the normal Dala browser extension."
    $answer = Read-Host "Install headless browser support now? [y/N]"
    if ($answer -match "^(y|yes)$") {
        $HeadlessBrowser = $true
    }
}

if ($HeadlessBrowser) {
    & $installScript -Upgrade -HeadlessBrowser
} else {
    & $installScript -Upgrade
}

$desktop = [Environment]::GetFolderPath("Desktop")
if ($desktop) {
    $launcher = Join-Path $desktop "Start Dala Server.bat"
    Set-Content -Path $launcher -Encoding ASCII -Value "@echo off`r`ndala-server --open`r`npause`r`n"
    Write-Host "Created launcher: $launcher"
}

Write-Host ""
Write-Host "Dala is installed or updated."
Write-Host "Start it with the Desktop launcher or run: dala-server --open"
