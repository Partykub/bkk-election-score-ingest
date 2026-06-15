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

$envValues = @{}
Get-Content $composeEnvPath | ForEach-Object {
    if ($_ -match '^\s*([^#=\s]+)\s*=\s*(.*)$') {
        $envValues[$matches[1]] = $matches[2].Trim()
    }
}

function Require-DeploymentValue {
    param([string]$Name)

    $value = [string]$envValues[$Name]
    if ([string]::IsNullOrWhiteSpace($value) -or $value -match '^(change-this|replace-with)') {
        throw "$Name must be set to a real value in $composeEnvPath."
    }
    return $value
}

if ([string]$envValues["API_SERVER_ENABLED"] -ne "true") {
    throw "API_SERVER_ENABLED must be true because ocr-worker calls the Hermes API on port 8642."
}

$apiServerKey = Require-DeploymentValue "API_SERVER_KEY"
$ocrWorkerApiKey = Require-DeploymentValue "OCR_WORKER_HERMES_API_KEY"
if ($apiServerKey -cne $ocrWorkerApiKey) {
    throw "OCR_WORKER_HERMES_API_KEY must exactly match API_SERVER_KEY."
}

$runtimeDir = [string]$envValues["HERMES_SUPERVISOR_RUNTIME_DIR"]
if ([string]::IsNullOrWhiteSpace($runtimeDir)) {
    $runtimeDir = "./runtime-ec2"
}
$runtimePath = [System.IO.Path]::GetFullPath((Join-Path $supervisorRoot $runtimeDir))
$runtimeConfigPath = Join-Path $runtimePath "config.yaml"
if (Test-Path $runtimeConfigPath) {
    $runtimeConfig = Get-Content $runtimeConfigPath -Raw
    $usesAuthenticatedCustomEndpoint = $runtimeConfig -match '(?m)^\s*provider:\s*custom(?::\S+)?\s*$' -and
        $runtimeConfig -match '(?m)^\s*base_url:\s*https://'
    if ($usesAuthenticatedCustomEndpoint) {
        Require-DeploymentValue "MODEL_API_KEY" | Out-Null
        if ($runtimeConfig -notmatch '(?m)^\s*key_env:\s*MODEL_API_KEY\s*$') {
            throw "$runtimeConfigPath must use a named custom provider with key_env: MODEL_API_KEY."
        }
    }
}

docker compose --env-file $composeEnvPath -f $composePath up -d
