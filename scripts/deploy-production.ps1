[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^ghcr\.io/[a-z0-9._-]+/raceframe-backend@sha256:[a-f0-9]{64}$')]
    [string]$BackendImage,

    [Parameter(Mandatory)]
    [ValidatePattern('^ghcr\.io/[a-z0-9._-]+/raceframe-worker@sha256:[a-f0-9]{64}$')]
    [string]$WorkerImage,

    [string]$BackendHost = 'oci',
    [string]$WorkerHost = 'oci-worker'
)

$ErrorActionPreference = 'Stop'

function Invoke-RemoteScript {
    param(
        [Parameter(Mandatory)][string]$HostName,
        [Parameter(Mandatory)][string]$Script,
        [AllowEmptyCollection()][string[]]$Arguments = @()
    )

    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Script))
    $quotedArguments = ($Arguments | ForEach-Object { "'$_'" }) -join ' '
    $command = "echo $encoded | base64 --decode | bash -s -- $quotedArguments"
    if ($PSCmdlet.ShouldProcess($HostName, 'run production deployment step')) {
        & ssh $HostName $command
        if ($LASTEXITCODE -ne 0) {
            throw "Remote deployment step failed on $HostName."
        }
    }
}

$drainWorker = @'
set -Eeuo pipefail
cd "$HOME/raceframe/raceframe-worker"
docker compose stop worker
'@

$resumeWorker = @'
set -Eeuo pipefail
cd "$HOME/raceframe/raceframe-worker"
docker compose up -d worker
'@

$deployBackend = @'
set -Eeuo pipefail
image="$1"
cd "$HOME/raceframe/raceframe-backend"
test -r .migration.env && test "$(stat -c %a .migration.env)" = 600
snapshot=".env.deploy-backup-$(date -u +%Y%m%dT%H%M%SZ)"
cp .env "$snapshot"
chmod 600 "$snapshot"
restore() {
  cp "$snapshot" .env
  docker compose up -d app || true
}
trap 'restore' ERR
sed -i -E "s|^RACEFRAME_BACKEND_IMAGE=.*$|RACEFRAME_BACKEND_IMAGE=${image}|" .env
grep -q "^RACEFRAME_BACKEND_IMAGE=${image}$" .env
docker compose pull app migrate
docker compose --profile migration run --rm migrate
docker compose up -d app
for attempt in $(seq 1 18); do
  if curl --fail --silent --show-error http://127.0.0.1:8008/readyz >/dev/null; then
    trap - ERR
    echo "Backend deployment healthy; snapshot retained at $snapshot"
    exit 0
  fi
  sleep 5
done
echo "Backend readiness did not succeed; restoring prior image." >&2
exit 1
'@

$deployWorker = @'
set -Eeuo pipefail
image="$1"
cd "$HOME/raceframe/raceframe-worker"
snapshot=".env.deploy-backup-$(date -u +%Y%m%dT%H%M%SZ)"
cp .env "$snapshot"
chmod 600 "$snapshot"
restore() {
  cp "$snapshot" .env
  docker compose up -d worker || true
}
trap 'restore' ERR
sed -i -E "s|^RACEFRAME_WORKER_IMAGE=.*$|RACEFRAME_WORKER_IMAGE=${image}|" .env
grep -q "^RACEFRAME_WORKER_IMAGE=${image}$" .env
docker compose pull worker
docker compose up -d worker
for attempt in $(seq 1 12); do
  if test "$(docker inspect --format='{{.State.Running}}' raceframe-worker 2>/dev/null || true)" = true; then
    trap - ERR
    echo "Worker deployment running; snapshot retained at $snapshot"
    exit 0
  fi
  sleep 5
done
echo "Worker did not start; restoring prior image." >&2
exit 1
'@

Write-Host "Draining worker on $WorkerHost..."
Invoke-RemoteScript -HostName $WorkerHost -Script $drainWorker -Arguments @()

try {
    Write-Host "Deploying backend image by immutable digest..."
    Invoke-RemoteScript -HostName $BackendHost -Script $deployBackend -Arguments @($BackendImage)
    Write-Host "Deploying worker image by immutable digest..."
    Invoke-RemoteScript -HostName $WorkerHost -Script $deployWorker -Arguments @($WorkerImage)
}
catch {
    Write-Warning 'Deployment failed; restoring the worker with its retained image configuration.'
    try {
        Invoke-RemoteScript -HostName $WorkerHost -Script $resumeWorker
    }
    catch {
        Write-Warning 'The worker could not be restarted automatically; use its latest .env.deploy-backup-* snapshot.'
    }
    Write-Error "The changed service restores its prior image automatically; inspect its retained .env.deploy-backup-* snapshot before retrying."
    throw
}

Write-Host 'Deployment completed. Verify public user search and one worker job before closing the release.'
