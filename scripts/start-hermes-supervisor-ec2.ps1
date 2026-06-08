$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$supervisorRoot = Join-Path $repoRoot "hermes\supervisor"
$composePath = Join-Path $supervisorRoot "docker-compose.ec2.yml"
$composeEnvPath = Join-Path $supervisorRoot ".env.ec2"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is required but was not found in PATH."
}

if (-not (Test-Path $composeEnvPath)) {
    throw "Missing $composeEnvPath. Copy hermes/supervisor/.env.ec2.example to .env.ec2 and set your values first."
}

docker compose --env-file $composeEnvPath -f $composePath up -d
