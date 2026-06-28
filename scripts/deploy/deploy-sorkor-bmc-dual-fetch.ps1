# Upload sorkor/BMC dual-fetch code to S3 and deploy on prod EC2 via SSM.
param(
    [string]$Profile = "ch7-source-old",
    [string]$Bucket = "ch7-static-bkkelection2569",
    [string]$Ts = "sorkor-bmc-dual-fetch-20260628",
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
    Write-Host "Uploading deploy artifacts (TS=$Ts) ..."
    Invoke-Aws s3 cp "hermes/results_api/app.py" "$Prefix/hermes-results-api-app-$Ts.py"
    Invoke-Aws s3 cp "hermes/results_api/governor_mock_ticker.py" "$Prefix/hermes-results-api-governor-mock-ticker-$Ts.py"
    Invoke-Aws s3 cp "hermes/governor_results/public_source.py" "$Prefix/hermes-governor-results-public-source-$Ts.py"
    Invoke-Aws s3 cp "hermes/governor_results/sorkor_adapter.py" "$Prefix/hermes-governor-results-sorkor-adapter-$Ts.py"
    Invoke-Aws s3 cp "hermes/governor_results/test_sorkor_adapter.py" "$Prefix/hermes-governor-results-test-sorkor-adapter-$Ts.py"
    Invoke-Aws s3 cp "hermes/governor_results/test_public_source.py" "$Prefix/hermes-governor-results-test-public-source-$Ts.py"
    Invoke-Aws s3 cp "hermes/results_api/test_governor_mock_ticker.py" "$Prefix/hermes-results-api-test-governor-mock-ticker-$Ts.py"
    Invoke-Aws s3 cp "hermes/results_api/fixtures/bmc-mock-final.json" "$Prefix/hermes-results-api-fixture-bmc-mock-final-$Ts.json"
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
$SsmJson = Join-Path $RepoRoot "deploy\ssm\deploy\sorkor-bmc-dual-fetch.json"
if (-not (Test-Path $SsmJson)) {
    throw "Missing $SsmJson"
}
$raw = Get-Content $SsmJson -Raw
$raw = $raw.Replace("sorkor-bmc-dual-fetch-20260628", $Ts)
$tmp = Join-Path $env:TEMP "sorkor-bmc-dual-fetch-ssm-$Ts.json"
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
Write-Host "Verify with:"
Write-Host "  aws --profile $Profile ssm send-command --cli-input-json file://deploy/ssm/verify/sorkor-bmc-dual-fetch.json"
