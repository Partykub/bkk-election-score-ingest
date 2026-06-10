param(
    [string]$TunnelName = "hermes-line",
    [string]$ConfigPath = ""
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    throw "cloudflared is required but was not found in PATH. Install it first, for example with: winget install Cloudflare.cloudflared"
}

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $PSScriptRoot "..\hermes\supervisor\cloudflared\config.yml"
}

$resolvedConfigPath = Resolve-Path $ConfigPath -ErrorAction SilentlyContinue

if (-not $resolvedConfigPath) {
    throw "Missing $ConfigPath. Copy hermes/supervisor/cloudflared/config.example.yml to hermes/supervisor/cloudflared/config.yml and fill in the real tunnel values first."
}

$envPath = Join-Path $PSScriptRoot "..\hermes\supervisor\.env"
$relayPort = 8646
if (Test-Path $envPath) {
    $line = Get-Content $envPath | Where-Object { $_ -match '^SUPERVISOR_RELAY_PORT=' } | Select-Object -First 1
    if ($line) {
        $value = ($line -split '=', 2)[1].Trim()
        $parsed = 0
        if ([int]::TryParse($value, [ref]$parsed)) {
            $relayPort = $parsed
        }
    }
}

Write-Host "Starting Cloudflare Tunnel..." -ForegroundColor Cyan
Write-Host "Tunnel name : $TunnelName" -ForegroundColor DarkGray
Write-Host "Config path : $resolvedConfigPath" -ForegroundColor DarkGray
Write-Host "Target      : http://localhost:$relayPort" -ForegroundColor DarkGray

cloudflared tunnel --config $resolvedConfigPath run $TunnelName
