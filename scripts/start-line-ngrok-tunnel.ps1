param(
    [int]$LocalPort = 8646,
    [string]$Hostname = ""
)

$ErrorActionPreference = "Stop"

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
