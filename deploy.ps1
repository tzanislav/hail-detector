<#
.SYNOPSIS
Build Hail Detector, ship it to the EC2 host and restart its services.

.DESCRIPTION
1. Build  - run the tests and build a wheel (the app plus its dashboard files).
2. Tar    - pack the wheel together with the systemd unit files.
3. Copy   - scp the archive to the server.
4. Reload - install the wheel into the server's venv, install the unit files,
            and restart the poller, API and Telegram bot services.

The server keeps its own .env and var/radar; neither is touched.

.EXAMPLE
.\deploy.ps1
.\deploy.ps1 -SkipTests
#>
param(
    [string]$Server = "my-ec2",                       # ssh host alias from ~/.ssh/config
    [string]$AppDir = "/home/ubuntu/hail-detector",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

function Invoke-Step([string]$Exe) {
    & $Exe @args
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed with exit code $LASTEXITCODE" }
}

$root    = $PSScriptRoot
$python  = Join-Path $root ".venv\Scripts\python.exe"
$staging = Join-Path ([IO.Path]::GetTempPath()) "hail-detector-deploy"
$archive = Join-Path ([IO.Path]::GetTempPath()) "hail-detector-deploy.tar.gz"
$remoteArchive = "/tmp/hail-detector-deploy.tar.gz"

# 1. Build
if (-not $SkipTests) {
    Write-Host "== Running tests"
    Push-Location $root
    try { Invoke-Step $python -m pytest } finally { Pop-Location }
}

Write-Host "== Building wheel"
Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $staging | Out-Null
Invoke-Step $python -m pip wheel $root --no-deps --quiet --wheel-dir $staging
Copy-Item (Join-Path $root "deploy\systemd\*.service") $staging

# 2. Tar
Write-Host "== Packing $archive"
Invoke-Step tar.exe -czf $archive -C $staging .

# 3. Copy
Write-Host "== Uploading to ${Server}:$remoteArchive"
Invoke-Step scp.exe $archive "${Server}:$remoteArchive"

# 4. Install and reload services
Write-Host "== Installing and restarting services on $Server"
$remote = @"
set -e
tmp=`$(mktemp -d)
tar -xzf $remoteArchive -C `$tmp
whl=`$(ls `$tmp/*.whl)
$AppDir/.venv/bin/python -m pip install --quiet --force-reinstall --no-deps `$whl
$AppDir/.venv/bin/python -m pip install --quiet `$whl
sudo install -m 644 `$tmp/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --quiet hail-detector-bot
sudo systemctl restart hail-detector-poller hail-detector-api hail-detector-bot
rm -rf `$tmp $remoteArchive
sleep 3
systemctl is-active hail-detector-poller hail-detector-api
systemctl is-active hail-detector-bot || true
curl -s -o /dev/null -w 'health: %{http_code}\n' http://127.0.0.1:8001/health || true
"@ -replace "`r", ""
Invoke-Step ssh.exe $Server $remote

Remove-Item $staging, $archive -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "== Deployed."
