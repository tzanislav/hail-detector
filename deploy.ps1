[CmdletBinding()]
param(
    [ValidateNotNullOrEmpty()]
    [string]$RemoteHost = "ec2-54-76-118-84.eu-west-1.compute.amazonaws.com",

    [ValidateNotNullOrEmpty()]
    [string]$RemoteUser = "ubuntu",

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$RemotePath,

    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$KeyPath = "D:\Libraries\Work\Dev\Web Development\adimari-key-pair.pem",

    [ValidateRange(1, 65535)]
    [int]$SshPort = 22,

    [switch]$SkipTests
)

<##
.SYNOPSIS
Deploy the current working tree to one EC2 host and restart Hail Detector.

.DESCRIPTION
The deployment archive includes tracked and non-ignored uncommitted files, so
the server receives precisely the version being worked on locally. On the EC2
host, all existing application files are replaced. The server's .env is the
sole preserved file because it contains deployment-specific secrets.

When the host has the Hail Detector systemd services installed, the script
restarts those services instead of running start.sh. This avoids duplicate
processes and preserves the host's service-management configuration.

.EXAMPLE
.\deploy.ps1 -RemotePath /home/ubuntu/hail-detector
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory)]
        [string]$FilePath,

        [string[]]$Arguments = @()
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath"
    }
}

function Resolve-NativeCommand {
    param(
        [Parameter(Mandatory)]
        [string]$Name
    )

    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    # Some Windows PowerShell sessions omit OpenSSH from PATH even though it
    # is installed with Windows. Use its standard location as a fallback.
    $openSshPath = Join-Path $env:WINDIR "System32\OpenSSH\$Name"
    if (Test-Path -LiteralPath $openSshPath -PathType Leaf) {
        return $openSshPath
    }

    throw "Required command is unavailable: $Name"
}

function Assert-SafeRemoteValue {
    param(
        [Parameter(Mandatory)]
        [string]$Name,

        [Parameter(Mandatory)]
        [string]$Value
    )

    if ($Value -match "[\r\n']") {
        throw "$Name cannot contain a newline or single quote."
    }
}

if ($RemoteHost -notmatch "^[A-Za-z0-9][A-Za-z0-9.-]*$") {
    throw "RemoteHost must be a hostname or IPv4 address."
}
if ($RemoteUser -notmatch "^[A-Za-z_][A-Za-z0-9_-]*$") {
    throw "RemoteUser contains unsupported characters."
}
if (-not $RemotePath.StartsWith("/") -or $RemotePath -eq "/") {
    throw "RemotePath must be an absolute directory other than '/'."
}
Assert-SafeRemoteValue -Name "RemotePath" -Value $RemotePath

$projectRoot = $PSScriptRoot
$timestamp = Get-Date -Format "yyyyMMddTHHmmss"
$archiveName = "hail-detector-$timestamp.tar.gz"
$localArchive = Join-Path ([System.IO.Path]::GetTempPath()) $archiveName
$remoteArchive = "/tmp/$archiveName"
$remoteConnection = "${RemoteUser}@${RemoteHost}"
$sshArguments = @("-i", $KeyPath, "-p", $SshPort.ToString())
$scpArguments = @("-i", $KeyPath, "-P", $SshPort.ToString())
$gitCommand = Resolve-NativeCommand -Name "git.exe"
$tarCommand = Resolve-NativeCommand -Name "tar.exe"
$sshCommand = Resolve-NativeCommand -Name "ssh.exe"
$scpCommand = Resolve-NativeCommand -Name "scp.exe"

$sourceFiles = @(
    & $gitCommand -C $projectRoot ls-files -co --exclude-standard
)
if ($LASTEXITCODE -ne 0) {
    throw "Could not list deployment files from Git."
}
if ($sourceFiles.Count -eq 0) {
    throw "No tracked or non-ignored files were found to deploy."
}

$remoteScript = @'
set -Eeuo pipefail

app_dir="$1"
archive_path="$2"
run_tests="$3"
parent_dir="$(dirname "$app_dir")"
app_name="$(basename "$app_dir")"
release_dir=""
backup_dir=""
uses_systemd=false

cleanup() {
  rm -f "$archive_path"
  if [[ -n "$release_dir" && -d "$release_dir" ]]; then
    rm -rf "$release_dir"
  fi
}
trap cleanup EXIT

if [[ "$app_dir" != /* || "$app_dir" == / ]]; then
  echo "Remote application path must be an absolute directory other than /." >&2
  exit 2
fi
if [[ ! -f "$archive_path" ]]; then
  echo "Deployment archive is missing: $archive_path" >&2
  exit 2
fi
if ! command -v python3 >/dev/null; then
  echo "python3 is required on the remote host." >&2
  exit 2
fi

mkdir -p "$parent_dir"
release_dir="$(mktemp -d "$parent_dir/.${app_name}.deploy.XXXXXX")"
tar -xzf "$archive_path" -C "$release_dir"

if [[ ! -f "$release_dir/pyproject.toml" || ! -f "$release_dir/start.sh" ]]; then
  echo "Deployment archive is not a Hail Detector application." >&2
  exit 2
fi

# The archive is made on Windows; ensure the Bash runner has Unix line endings.
sed -i 's/\r$//' "$release_dir/start.sh"
if [[ -f "$app_dir/.env" ]]; then
  cp -p "$app_dir/.env" "$release_dir/.env"
fi

python3 -m venv "$release_dir/.venv"
(
  cd "$release_dir"
  "$release_dir/.venv/bin/python" -m pip install --upgrade pip
  "$release_dir/.venv/bin/python" -m pip install '.[dev]'
  if [[ "$run_tests" == "true" ]]; then
    "$release_dir/.venv/bin/python" -m pytest
  fi
)

if command -v systemctl >/dev/null \
  && systemctl cat hail-detector-api.service >/dev/null 2>&1 \
  && systemctl cat hail-detector-poller.service >/dev/null 2>&1; then
  if ! sudo -n true; then
    echo "Passwordless sudo is required to restart the installed Hail Detector systemd services." >&2
    exit 2
  fi
  uses_systemd=true
fi

stop_previous_processes() {
  # PID files can be absent after a host reboot or an older manual launch.
  # Match both supported processes against this exact app path instead of
  # trusting a potentially recycled PID from a stale file.
  mapfile -t process_ids < <(
    pgrep -u "$(id -u)" -f "$app_dir/.venv/bin/python.*hail_detector\\.(poller|api:app)" || true
  )
  for pid in "${process_ids[@]}"; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    if kill -0 "$pid" 2>/dev/null; then
      echo "Stopping previous Hail Detector process (PID $pid)"
      kill "$pid"
    fi
  done

  [[ "${#process_ids[@]}" -gt 0 ]] || return 0

  for _ in {1..15}; do
    running=false
    for pid in "${process_ids[@]}"; do
      [[ "$pid" =~ ^[0-9]+$ ]] || continue
      if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        running=true
      fi
    done
    [[ "$running" == false ]] && return 0
    sleep 1
  done

  echo "The previous application processes did not stop within 15 seconds." >&2
  exit 1
}

stop_application() {
  if [[ "$uses_systemd" == true ]]; then
    echo "Stopping Hail Detector systemd services"
    sudo -n systemctl stop hail-detector-api.service hail-detector-poller.service
  fi
  stop_previous_processes
}

start_application() {
  if [[ "$uses_systemd" == true ]]; then
    echo "Starting Hail Detector systemd services"
    sudo -n systemctl start hail-detector-poller.service hail-detector-api.service
    sleep 2
    sudo -n systemctl is-active --quiet hail-detector-poller.service
    sudo -n systemctl is-active --quiet hail-detector-api.service
  else
    (
      cd "$app_dir"
      bash ./start.sh
    )
  fi
}

stop_application
if [[ -d "$app_dir" ]]; then
  backup_dir="$parent_dir/.${app_name}.previous.$(date +%s)"
  mv "$app_dir" "$backup_dir"
fi
mv "$release_dir" "$app_dir"
release_dir=""

if ! start_application; then
  # start.sh can fail after launching only one of its two processes; systemd
  # can also leave a unit restarting after a failed launch.
  stop_application || true
  failed_dir="$parent_dir/.${app_name}.failed.$(date +%s)"
  mv "$app_dir" "$failed_dir"
  if [[ -n "$backup_dir" && -d "$backup_dir" ]]; then
    mv "$backup_dir" "$app_dir"
    start_application || true
  fi
  echo "The new release did not start; the previous release was restored." >&2
  exit 1
fi

if [[ -n "$backup_dir" && -d "$backup_dir" ]]; then
  rm -rf "$backup_dir"
fi

echo "Deployment completed successfully."
if command -v curl >/dev/null; then
  port="${HAIL_PORT:-8001}"
  echo "Health endpoint response: $(curl --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:${port}/health" || true)"
fi
'@

# The deployment helper runs under Bash on Linux; preserve its required LF
# line endings even when this PowerShell file is checked out on Windows.
$remoteScript = $remoteScript.Replace("`r`n", "`n")

$encodedRemoteScript = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes($remoteScript)
)
$testFlag = if ($SkipTests) { "false" } else { "true" }
$remoteCommand = "echo '$encodedRemoteScript' | base64 --decode | bash -s -- '$RemotePath' '$remoteArchive' '$testFlag'"

try {
    Write-Host "Packaging $($sourceFiles.Count) files from $projectRoot"
    Invoke-NativeCommand -FilePath $tarCommand -Arguments (
        @("-czf", $localArchive, "-C", $projectRoot, "--") + $sourceFiles
    )

    Write-Host "Uploading the release to $remoteConnection"
    Invoke-NativeCommand -FilePath $scpCommand -Arguments (
        $scpArguments + @($localArchive, "${remoteConnection}:$remoteArchive")
    )

    Write-Host "Building, testing, and restarting on $remoteConnection"
    Invoke-NativeCommand -FilePath $sshCommand -Arguments (
        $sshArguments + @($remoteConnection, $remoteCommand)
    )
}
finally {
    Remove-Item -LiteralPath $localArchive -Force -ErrorAction SilentlyContinue
}
