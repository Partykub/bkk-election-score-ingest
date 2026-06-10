param(
    [int]$LocalPort = 0,
    [string]$Hostname = ""
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

if (-not (Get-Command ngrok -ErrorAction SilentlyContinue)) {
    throw "ngrok is required but was not found in PATH. Install it first from https://ngrok.com/download or via winget."
}

$command = @("http", "http://localhost:$LocalPort")

if (-not [string]::IsNullOrWhiteSpace($Hostname)) {
    $command += @("--url=$Hostname")
}

Write-Host "Starting ngrok tunnel..." -ForegroundColor Cyan
Write-Host "Local target : http://localhost:$LocalPort" -ForegroundColor DarkGray

if (-not [string]::IsNullOrWhiteSpace($Hostname)) {
    Write-Host "Requested URL: $Hostname" -ForegroundColor DarkGray
}
else {
    Write-Host "Public URL will be shown by ngrok after the session starts." -ForegroundColor DarkGray
}

Write-Host "Keep this terminal open while LINE webhook testing is active." -ForegroundColor Yellow

& ngrok @command
