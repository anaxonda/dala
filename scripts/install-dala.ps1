param(
    [switch]$HeadlessBrowser,
    [switch]$Browser,
    [switch]$Upgrade,
    [switch]$UpgradeUv,
    [string]$PackageSpec = $(if ($env:DALA_PACKAGE_SPEC) { $env:DALA_PACKAGE_SPEC } else { "dala" })
)

$ErrorActionPreference = "Stop"

function Command-Exists($Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

if (Command-Exists "uv") {
    Write-Host "Found uv: $(uv --version)"
    if ($UpgradeUv) {
        try {
            uv self update
        } catch {
            Write-Host "uv self update is not available for this install; continuing."
        }
    }
} else {
    Write-Host "uv not found; installing uv..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

$installArgs = @("tool", "install")
$installHeadlessBrowser = $HeadlessBrowser -or $Browser

if ($Upgrade -or $installHeadlessBrowser) {
    $installArgs += "--force"
}

if ($installHeadlessBrowser) {
    Write-Host "Installing Dala with optional headless browser support..."
    Write-Host "This lets the Dala server control Chrome/Chromium in the background."
    Write-Host "It is needed for PDF output and some JavaScript-heavy pages."
    Write-Host "It is separate from the normal Dala browser extension."
    $installArgs += @("--with", "playwright", $PackageSpec)
    uv @installArgs
    if (Command-Exists "dala-setup-browser") {
        dala-setup-browser
    } else {
        uv tool run --with playwright --from $PackageSpec dala-setup-browser
    }
} else {
    Write-Host "Installing Dala server..."
    $installArgs += $PackageSpec
    uv @installArgs
}

Write-Host ""
Write-Host "Dala installed. Start the server with:"
Write-Host "  dala-server"
Write-Host "If dala-server is not found, restart your terminal or run: uv tool update-shell"
