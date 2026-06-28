# Run results-api locally for Monitor / BKK fetch testing.
# Prereq: aws sso login --profile ch7-source-old
param(
    [int]$Port = 0,
    [string]$AwsProfile = "ch7-source-old",
    [switch]$SkipDeps
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
Set-Location $RepoRoot

# Stale session env vars override AWS_PROFILE and cause ExpiredToken.
Remove-Item Env:AWS_ACCESS_KEY_ID -ErrorAction SilentlyContinue
Remove-Item Env:AWS_SECRET_ACCESS_KEY -ErrorAction SilentlyContinue
Remove-Item Env:AWS_SESSION_TOKEN -ErrorAction SilentlyContinue

$env:AWS_PROFILE = $AwsProfile
$env:PYTHONPATH = $RepoRoot
$env:RESULTS_API_S3_BUCKET = "ch7-static-bkkelection2569"
$env:RESULTS_API_S3_PREFIX = "api-data/score"
$env:RESULTS_API_AWS_REGION = "ap-southeast-1"
$env:GOVERNOR_RESULTS_PREFIX = "api-data/governor-results"
$env:RESULTS_API_STATIC_RESULTS_PREFIX = "api-data/governor-results"
$env:RESULTS_API_DEFAULT_DATA_MODE = "latest_snapshot"
$env:RESULTS_API_ENABLE_STATIC_FALLBACK = "false"
$env:RESULTS_API_DISTRICTS_URL = "s3://ch7-static-bkkelection2569/api-data/master-data/election-areas-bangkok.json"
$env:RESULTS_API_CANDIDATES_MANIFEST_URL = "s3://ch7-static-bkkelection2569/api-data/candidates/manifest.json"
$env:RESULTS_API_CANDIDATES_FEATURED_URL = "s3://ch7-static-bkkelection2569/api-data/candidates/featured.json"
$env:RESULTS_API_PARTIES_URL = "s3://ch7-static-bkkelection2569/api-data/master-data/parties.json"
$MockPrefix = "s3://ch7-static-bkkelection2569/api-data/governor-results-bkk/endpoint-mock"
$env:RESULTS_API_EXTERNAL_GOVERNOR_RESULTS_URL = "$MockPrefix/69-governor-electiondata.json"
$env:RESULTS_API_EXTERNAL_BMC_RESULTS_URL = "$MockPrefix/69-bmc-electiondata.json"
# Only this process should run auto fetch/mock ticks; prod shares the same S3 schedule config.
$env:RESULTS_API_ENABLE_MONITOR_FETCH_SCHEDULER = "true"
$env:RESULTS_API_ENABLE_MONITOR_MOCK_SCHEDULER = "true"

function Test-PortFree([int]$Candidate) {
    $conn = Get-NetTCPConnection -LocalPort $Candidate -State Listen -ErrorAction SilentlyContinue
    return -not $conn
}

function Find-FreePort([int[]]$Candidates) {
    foreach ($candidate in $Candidates) {
        if (Test-PortFree $candidate) {
            return $candidate
        }
    }
    throw "No free port found in: $($Candidates -join ', ')"
}

if (-not $SkipDeps) {
    Write-Host "Installing Python deps ..."
    pip install -q -r hermes\results_api\requirements.txt
}

if ($Port -le 0) {
    $Port = Find-FreePort @(18082, 18083, 18084, 8080, 8081)
}

Write-Host ""
Write-Host "Monitor UI:  http://127.0.0.1:$Port/monitor"
Write-Host "Health:      http://127.0.0.1:$Port/health"
Write-Host "BKK smoke:   .\scripts\test\test-bkk-fetch.ps1"
Write-Host "Press Ctrl+C to stop."
Write-Host ""

python -m uvicorn hermes.results_api.app:app --host 127.0.0.1 --port $Port
