param(
    [switch]$SkipSetupWizard
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$supervisorRoot = Join-Path $repoRoot "hermes\supervisor"
$composeEnvExample = Join-Path $supervisorRoot ".env.example"
$composeEnvPath = Join-Path $supervisorRoot ".env"

function Get-ConfiguredRuntimePath {
    param(
        [string]$SupervisorRoot,
        [string]$EnvPath,
        [string]$FallbackRelativePath
    )

    $relativePath = $FallbackRelativePath

    if (Test-Path $EnvPath) {
        $runtimeLine = Get-Content $EnvPath | Where-Object { $_ -match '^HERMES_SUPERVISOR_RUNTIME_DIR=' } | Select-Object -First 1

        if ($runtimeLine) {
            $relativePath = ($runtimeLine -split '=', 2)[1].Trim()
        }
    }

    if ([System.IO.Path]::IsPathRooted($relativePath)) {
        return $relativePath
    }

    return Join-Path $SupervisorRoot $relativePath
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is required but was not found in PATH."
}

if (-not (Test-Path $composeEnvPath)) {
    Copy-Item $composeEnvExample $composeEnvPath
    Write-Host "Created $composeEnvPath from .env.example"
}

$runtimePath = Get-ConfiguredRuntimePath -SupervisorRoot $supervisorRoot -EnvPath $composeEnvPath -FallbackRelativePath "./runtime-full"
$seedSoulPath = Join-Path $supervisorRoot "seed\SOUL.md"
$runtimeSoulPath = Join-Path $runtimePath "SOUL.md"

New-Item -ItemType Directory -Force -Path $runtimePath | Out-Null

if (-not (Test-Path $runtimeSoulPath)) {
    Copy-Item $seedSoulPath $runtimeSoulPath
    Write-Host "Seeded $runtimeSoulPath"
}

if ($SkipSetupWizard) {
    Write-Host "Skipping Hermes setup wizard."
    return
}

$runtimeMount = "{0}:/opt/data" -f $runtimePath

Write-Host "Launching Hermes setup wizard against $runtimePath"
docker run -it --rm -v $runtimeMount nousresearch/hermes-agent setup
