# End-to-end: save monitor URL + trigger fetch against local results-api.
param(
    [string]$BaseUrl = "http://127.0.0.1:18083",
    [string]$GovernorUrl = "s3://ch7-static-bkkelection2569/api-data/governor-results-bkk/endpoint-mock/69-governor-electiondata.json",
    [string]$BmcUrl = "s3://ch7-static-bkkelection2569/api-data/governor-results-bkk/endpoint-mock/69-bmc-electiondata.json",
    [switch]$GovernorOnly,
    [switch]$BmcOnly,
    [string]$ActivePublicSource = "bkk",
    [string]$ApiKey = ""
)

$ErrorActionPreference = "Stop"
$headers = @{ "Content-Type" = "application/json" }
if ($ApiKey) {
    $headers["x-api-key"] = $ApiKey
}

$governorEnabled = -not $BmcOnly
$bmcEnabled = -not $GovernorOnly

$saveBody = @{
    enabled = $governorEnabled
    url = $(if ($governorEnabled) { $GovernorUrl } else { "" })
    timeoutSeconds = 30
    bmcEnabled = $bmcEnabled
    bmcUrl = $(if ($bmcEnabled) { $BmcUrl } else { "" })
    bmcTimeoutSeconds = 30
    activePublicSource = $ActivePublicSource
} | ConvertTo-Json

Write-Host "PUT $BaseUrl/api/v1/monitor/source"
$save = Invoke-RestMethod -Method Put -Uri "$BaseUrl/api/v1/monitor/source" -Headers $headers -Body $saveBody
Write-Host "Governor:" $save.current.url "enabled=$($save.current.enabled)"
Write-Host "BMC:" $save.bmc.url "enabled=$($save.bmc.enabled)"
Write-Host "Active source:" $save.activePublicSource.source

Write-Host "POST $BaseUrl/api/v1/monitor/source/fetch"
try {
    $fetch = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/v1/monitor/source/fetch" -Headers $headers -Body "{}"
    Write-Host "Fetch OK status=$($fetch.status)"
    if ($fetch.governorFetch) {
        Write-Host "  governorFetch:" $fetch.governorFetch.status
        if ($fetch.upstream) {
            Write-Host "    districts:" $fetch.upstream.districtCount
            Write-Host "    pollingUnits:" $fetch.upstream.reportedPollingUnits "/" $fetch.upstream.totalPollingUnits
        }
        if ($fetch.staticExport) {
            Write-Host "    summaryKey:" $fetch.staticExport.summaryKey
        }
    }
    if ($fetch.bmcFetch) {
        Write-Host "  bmcFetch:" $fetch.bmcFetch.status
        if ($fetch.bmcUpstream) {
            Write-Host "    districts:" $fetch.bmcUpstream.districtCount
        }
        if ($fetch.sorkorExport) {
            Write-Host "    sorkorSummaryKey:" $fetch.sorkorExport.summaryKey
        }
    }
    if ($fetch.publicPromote) {
        Write-Host "  publicPromote livePrefix:" $fetch.publicPromote.livePrefix
        Write-Host "  promoted keys:" ($fetch.publicPromote.keys.PSObject.Properties.Name -join ", ")
    }
    if ($fetch.publicPromoteError) {
        Write-Host "  publicPromoteError:" $fetch.publicPromoteError -ForegroundColor Yellow
    }
} catch {
    $detail = $_.ErrorDetails.Message
    if ($detail) {
        Write-Host "Fetch failed:" $detail -ForegroundColor Red
    } else {
        Write-Host "Fetch failed:" $_.Exception.Message -ForegroundColor Red
    }
    exit 1
}
