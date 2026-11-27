<#
.SYNOPSIS
    Start the hail detector locally (poller + API/dashboard) for testing.

.DESCRIPTION
    Runs the radar poller in the background and the API in the foreground,
    both in this console. Press Ctrl+C to stop both.

    Telegram delivery is forced OFF unless -AllowTelegram is passed, so local
    testing never messages the real chat. Process environment variables take
    precedence over .env, so .env itself is left unchanged.

.EXAMPLE
    .\run-local.ps1
.EXAMPLE
    .\run-local.ps1 -Install -Test
.EXAMPLE
    .\run-local.ps1 -ApiOnly -Port 8080
#>
[CmdletBinding()]
param(
    # Port for the API/dashboard.
    [int]$Port = 8000,
    # Reinstall the package and dev dependencies before starting.
    [switch]$Install,
    # Run the test suite first and stop if it fails.
    [switch]$Test,
    # Start only the API (use the existing archive, no polling).
    [switch]$ApiOnly,
    # Allow real Telegram alerts (off by default for local runs).
    [switch]$AllowTelegram,
    # Force the paid OpenAI hail-risk agent off for this run.
    [switch]$NoLlm
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

Set-Location $root

if (-not (Test-Path $python)) {
    Write-Host 'Creating virtual environment in .venv ...'
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv (is Python 3.10+ on PATH?).' }
    $Install = $true
}

if ($Install) {
    & $python -m pip install -e '.[dev]'
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }
}

if (-not (Test-Path (Join-Path $root '.env'))) {
    Write-Warning 'No .env found; copy .env.example to .env to customise settings. Using defaults.'
}

if ($Test) {
    & $python -m pytest
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed; not starting the app.' }
}

# Local-run overrides (restored on exit so your shell is left as it was).
$overrides = @{}
if (-not $AllowTelegram) { $overrides['HAIL_TELEGRAM_ENABLED'] = 'false' }
if ($NoLlm) { $overrides['HAIL_LLM_ENABLED'] = 'false' }

$previous = @{}
foreach ($name in $overrides.Keys) {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    [Environment]::SetEnvironmentVariable($name, $overrides[$name], 'Process')
}

if ($AllowTelegram) {
    Write-Warning 'Telegram is allowed: real alerts may be sent.'
} else {
    Write-Host 'Telegram delivery disabled for this run.'
}

$poller = $null
try {
    if (-not $ApiOnly) {
        Write-Host 'Starting poller ...'
        $poller = Start-Process -FilePath $python -ArgumentList '-m', 'hail_detector.poller' `
            -WorkingDirectory $root -NoNewWindow -PassThru
    }

    Write-Host "Dashboard: http://127.0.0.1:$Port/   (Ctrl+C to stop)"
    & $python -m uvicorn hail_detector.api:app --app-dir src --host 127.0.0.1 --port $Port --reload
}
finally {
    if ($poller -and -not $poller.HasExited) {
        Write-Host "Stopping poller (PID $($poller.Id)) ..."
        Stop-Process -Id $poller.Id -Force -ErrorAction SilentlyContinue
    }
    foreach ($name in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
    }
}
