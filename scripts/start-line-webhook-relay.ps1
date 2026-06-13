param(
	[string]$EnvFile = "",
	[int]$Port = 0
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

if ([string]::IsNullOrWhiteSpace($EnvFile)) {
	$EnvFile = Join-Path $repoRoot "hermes\supervisor\.env"
}

$pythonArgs = @("-m", "hermes.supervisor.line_webhook_relay", "--env-file", $EnvFile)

if ($Port -gt 0) {
	$pythonArgs += @("--port", $Port.ToString())
}

& "C:\Program Files\Python311\python.exe" @pythonArgs