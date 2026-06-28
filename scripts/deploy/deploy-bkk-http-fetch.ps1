# Upload BKK http-fetch fix to S3 and deploy on prod EC2 via SSM.
param(
    [string]$Profile = "ch7-source-old",
    [string]$Bucket = "ch7-static-bkkelection2569",
    [string]$Ts = "bkk-http-fetch-20260625",
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
    Invoke-Aws s3 cp "hermes/governor_results/http_fetch.py" "$Prefix/hermes-governor-results-http-fetch-$Ts.py"
    Invoke-Aws s3 cp "hermes/results_api/requirements.txt" "$Prefix/hermes-results-api-requirements-$Ts.txt"
    Invoke-Aws s3 cp "hermes/governor_results/test_http_fetch.py" "$Prefix/hermes-governor-results-test-http-fetch-$Ts.py"
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
$SsmJson = Join-Path $RepoRoot "deploy\ssm\deploy\bkk-http-fetch.json"
if (-not (Test-Path $SsmJson)) {
    throw "Missing $SsmJson"
}
$raw = Get-Content $SsmJson -Raw
$raw = $raw.Replace("bkk-http-fetch-20260625", $Ts)
$tmp = Join-Path $env:TEMP "bkk-http-fetch-ssm-$Ts.json"
[System.IO.File]::WriteAllText($tmp, $raw, [System.Text.UTF8Encoding]::new($false))
$inputJson = "file://" + ($tmp -replace '\\', '/')

$result = Invoke-Aws ssm send-command --cli-input-json $inputJson --output json | ConvertFrom-Json
$commandId = $result.Command.CommandId
Write-Host "CommandId: $commandId"
Write-Host "Waiting for completion ..."
for ($i = 1; $i -le 45; $i++) {
    Start-Sleep -Seconds 4
    $inv = Invoke-Aws ssm get-command-invocation --command-id $commandId --instance-id $InstanceId --output json | ConvertFrom-Json
    if ($inv.Status -in @("Success", "Failed", "Cancelled", "TimedOut")) {
        break
    }
    Write-Host "  ... $($inv.Status) ($i/45)"
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
Write-Host "  aws --profile $Profile ssm send-command --cli-input-json file://deploy/ssm/verify/bkk-http-fetch.json"
Write-Host "Monitor: https://<your-domain>/monitor"
