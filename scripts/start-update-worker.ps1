$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$supervisorRoot = Join-Path $repoRoot "hermes\supervisor"
$composePath = Join-Path $supervisorRoot "docker-compose.yml"
$composeEnvPath = Join-Path $supervisorRoot ".env"
Set-Location $repoRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
	throw "Docker is required but was not found in PATH."
}

if (-not (Test-Path $composeEnvPath)) {
	throw "Missing $composeEnvPath. Run scripts/setup-hermes-supervisor.ps1 first."
}

docker compose --profile update-worker --env-file $composeEnvPath -f $composePath up -d update-worker