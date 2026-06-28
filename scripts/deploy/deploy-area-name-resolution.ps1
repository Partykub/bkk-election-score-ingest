# Upload area-name-resolution deploy artifacts to S3 and deploy on prod EC2 via SSM.
param(
    [string]$Profile = "ch7-source-old",
    [string]$Bucket = "ch7-static-bkkelection2569",
    [string]$Ts = "area-name-resolution-20260626c",
    [string]$InstanceId = "i-06edd717a43f763b7",
    [switch]$UploadOnly,
    [switch]$DeployOnly
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
Set-Location $RepoRoot

$Prefix = "s3://$Bucket/tmp/deploy"
function Invoke-Aws {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    & aws --profile $Profile @Args
}

if (-not $DeployOnly) {
    Invoke-Aws sts get-caller-identity | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "AWS login failed. Run: aws sso login --profile $Profile"
    }

    Write-Host "Running local tests ..."
    python -m unittest hermes.governor_results.test_area_resolution `
        hermes.supervisor.test_intake_server.ReviewQueueTests.test_parse_area_id_override_resolves_district_name `
        hermes.supervisor.test_intake_server.ReviewQueueTests.test_build_approval_prompt_text_shows_district_name -q
    if ($LASTEXITCODE -ne 0) {
        throw "Local tests failed"
    }

    Write-Host "Uploading deploy artifacts (TS=$Ts) ..."
    Invoke-Aws s3 cp "hermes/supervisor/services/intake_server.py" "$Prefix/hermes-supervisor-intake-server-$Ts.py"
    Invoke-Aws s3 cp "hermes/ocr_worker/__main__.py" "$Prefix/hermes-ocr-worker-main-$Ts.py"
    Invoke-Aws s3 cp "hermes/governor_results/area_resolution.py" "$Prefix/hermes-governor-results-area-resolution-$Ts.py"
    Invoke-Aws s3 cp "hermes/governor_results/test_area_resolution.py" "$Prefix/hermes-governor-results-test-area-resolution-$Ts.py"
    Invoke-Aws s3 cp "compose.yaml" "$Prefix/compose-$Ts.yaml"
    Write-Host "Uploaded to $Prefix/*-$Ts.*"
}

if ($UploadOnly) {
    Write-Host "UploadOnly set; skipping SSM deploy."
    exit 0
}

Invoke-Aws sts get-caller-identity | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "AWS login failed. Run: aws sso login --profile $Profile"
}

Write-Host "Sending SSM deploy command ..."
$SsmJson = Join-Path $RepoRoot "deploy\ssm\deploy\area-name-resolution.json"
if (-not (Test-Path $SsmJson)) {
    throw "Missing $SsmJson"
}
$raw = Get-Content $SsmJson -Raw
if ($Ts -notmatch "20260626c$") {
    $raw = $raw.Replace("area-name-resolution-20260626c", $Ts)
} else {
    $raw = $raw.Replace("area-name-resolution-20260626c", $Ts)
}
$tmp = Join-Path $env:TEMP "area-name-resolution-ssm-$Ts.json"
[System.IO.File]::WriteAllText($tmp, $raw, [System.Text.UTF8Encoding]::new($false))
$inputJson = "file://" + ($tmp -replace '\\', '/')

$result = Invoke-Aws ssm send-command --cli-input-json $inputJson --output json | ConvertFrom-Json
$commandId = $result.Command.CommandId
Write-Host "CommandId: $commandId"
Write-Host "Waiting for completion ..."
$inv = $null
for ($i = 1; $i -le 60; $i++) {
    Start-Sleep -Seconds 5
    $inv = Invoke-Aws ssm get-command-invocation --command-id $commandId --instance-id $InstanceId --output json | ConvertFrom-Json
    if ($inv.Status -in @("Success", "Failed", "Cancelled", "TimedOut")) {
        break
    }
    Write-Host "  ... $($inv.Status) ($i/60)"
}
Write-Host "Status: $($inv.Status)"
Write-Host $inv.StandardOutputContent
if ($inv.StandardErrorContent) {
    Write-Host "STDERR:" -ForegroundColor Yellow
    Write-Host $inv.StandardErrorContent
}
if ($inv.Status -ne "Success") {
    exit 1
}

Write-Host ""
Write-Host "Deploy succeeded. Verify with:"
Write-Host "  aws --profile $Profile ssm send-command --cli-input-json file://deploy/ssm/verify/area-name-resolution.json"
