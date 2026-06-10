param(
    [int]$LocalPort = 0,
    [int]$RemotePort = 80
)

$ErrorActionPreference = "Stop"

function Get-ConfiguredRelayPort {
    $envPath = Join-Path $PSScriptRoot "..\hermes\supervisor\.env"
    if (-not (Test-Path $envPath)) {
        return 8646
    }

    $line = Get-Content $envPath | Where-Object { $_ -match '^SUPERVISOR_RELAY_PORT=' } | Select-Object -First 1
    if (-not $line) {
        return 8646
    }

    $value = ($line -split '=', 2)[1].Trim()
    $parsed = 0
    if ([int]::TryParse($value, [ref]$parsed)) {
        return $parsed
    }

    return 8646
}

if ($LocalPort -le 0) {
    $LocalPort = Get-ConfiguredRelayPort
}

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "OpenSSH client is required but was not found in PATH. Install the Windows OpenSSH Client feature first."
}

Write-Host "Starting localhost.run tunnel..." -ForegroundColor Cyan
Write-Host "Local target  : http://localhost:$LocalPort" -ForegroundColor DarkGray
Write-Host "Public HTTPS URL will be printed by localhost.run after the SSH session connects." -ForegroundColor DarkGray
Write-Host "Keep this terminal open while LINE webhook testing is active." -ForegroundColor Yellow

ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -R ${RemotePort}:localhost:${LocalPort} nokey@localhost.run