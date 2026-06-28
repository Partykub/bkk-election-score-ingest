# Smoke test: fetch กทม JSON via browser headers (+ cloudscraper fallback on 403).
param(
    [string]$Url = "https://bangkokvote69.bangkok.go.th/results/69-governor-electiondata.json",
    [int]$TimeoutSeconds = 30,
    [switch]$SkipDeps
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "../..")
$env:PYTHONPATH = (Get-Location).Path

if (-not $SkipDeps) {
    python -m pip install -q -r hermes\results_api\requirements.txt
}

$env:TEST_BKK_FETCH_URL = $Url
$env:TEST_BKK_FETCH_TIMEOUT = "$TimeoutSeconds"

python -c @"
import os
from hermes.governor_results.http_fetch import browser_like_headers, fetch_json_http
import json

url = os.environ['TEST_BKK_FETCH_URL']
timeout = float(os.environ['TEST_BKK_FETCH_TIMEOUT'])
print('URL:', url)
print('Headers:', json.dumps(browser_like_headers(url), ensure_ascii=False))
payload = fetch_json_http(url, timeout_seconds=timeout)
total = payload.get('total') if isinstance(payload.get('total'), dict) else {}
polling = total.get('pollingUnits') if isinstance(total.get('pollingUnits'), dict) else {}
print('OK')
print('  type:', payload.get('type'))
print('  lastUpdatedAt:', payload.get('lastUpdatedAt'))
print('  districts:', len(payload.get('districts') or []))
print('  reportedPollingUnits:', polling.get('reported'), '/', polling.get('total'))
"@
