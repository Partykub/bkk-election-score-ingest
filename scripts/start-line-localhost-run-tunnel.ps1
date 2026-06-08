param(
    [int]$LocalPort = 8646,
    [int]$RemotePort = 80
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "OpenSSH client is required but was not found in PATH. Install the Windows OpenSSH Client feature first."
}

Write-Host "Starting localhost.run tunnel..." -ForegroundColor Cyan
Write-Host "Local target  : http://localhost:$LocalPort" -ForegroundColor DarkGray
Write-Host "Public HTTPS URL will be printed by localhost.run after the SSH session connects." -ForegroundColor DarkGray
Write-Host "Keep this terminal open while LINE webhook testing is active." -ForegroundColor Yellow

ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -R ${RemotePort}:localhost:${LocalPort} nokey@localhost.run