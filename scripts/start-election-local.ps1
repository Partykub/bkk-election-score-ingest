param(
    [switch]$SkipRelay
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$supervisorRoot = Join-Path $repoRoot "hermes\supervisor"
$composePath = Join-Path $supervisorRoot "docker-compose.yml"
$composeEnvPath = Join-Path $supervisorRoot ".env"
$relayScriptPath = Join-Path $PSScriptRoot "start-line-webhook-relay.ps1"

function Get-EnvValue {
    param(
        [string]$EnvPath,
        [string]$VariableName,
        [string]$FallbackValue
    )

    if (-not (Test-Path $EnvPath)) {
        return $FallbackValue
    }

    $line = Get-Content $EnvPath | Where-Object { $_ -match ('^{0}=' -f [regex]::Escape($VariableName)) } | Select-Object -First 1
    if (-not $line) {
        return $FallbackValue
    }

    return ($line -split '=', 2)[1].Trim()
}

function Test-RelayHealth {
    param([int]$Port)

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ("http://127.0.0.1:{0}/line/webhook/health" -f $Port) -TimeoutSec 5
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
    }
    catch {
        return $false
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is required but was not found in PATH."
}

if (-not (Test-Path $composeEnvPath)) {
    throw "Missing $composeEnvPath. Run scripts/setup-hermes-supervisor.ps1 first."
}

Set-Location $repoRoot
docker compose --env-file $composeEnvPath -f $composePath up -d

if ($SkipRelay) {
    Write-Host "Docker services are up. Relay startup was skipped." -ForegroundColor Yellow
    return
}

$relayPort = [int](Get-EnvValue -EnvPath $composeEnvPath -VariableName "SUPERVISOR_RELAY_PORT" -FallbackValue "8646")

if (Test-RelayHealth -Port $relayPort) {
    Write-Host "Relay already running on http://127.0.0.1:$relayPort" -ForegroundColor Green
    return
}

Start-Process -FilePath "powershell.exe" -WorkingDirectory $repoRoot -ArgumentList @(
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    $relayScriptPath,
    "-EnvFile",
    $composeEnvPath,
    "-Port",
    $relayPort.ToString()
)

Write-Host "Started Docker services and launched relay in a new PowerShell window on http://127.0.0.1:$relayPort" -ForegroundColor Green
Write-Host "Use your tunnel script separately if LINE needs a public webhook URL." -ForegroundColor DarkGray