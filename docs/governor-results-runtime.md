# Governor Results Runtime

This document captures the current `governor-results` behavior after the dev/prod
alignment work.

## Source Of Truth

The public `governor-results` payloads no longer use the static S3 JSON as the
primary source.

Current flow:

1. LINE relay receives images and metadata.
2. OCR worker writes draft, approval, and source manifests under the score prefix.
3. `results-api` reads approved results from the score prefix.
4. `line-relay` exports fresh public JSON back to the governor-results prefix.

In practice this means:

- source data lives under `ELECTION_S3_PREFIX`, currently `api-data/score`
- public exports live under `GOVERNOR_RESULTS_PREFIX`
- current prod override: `api-data/governor-results-dev`

## Key Env Vars

These env vars now control the runtime:

```dotenv
GOVERNOR_RESULTS_PREFIX=api-data/governor-results
RESULTS_API_STATIC_RESULTS_PREFIX=api-data/governor-results
STATIC_RESULTS_PREFIX=api-data/governor-results
RESULTS_API_DEFAULT_DATA_MODE=incremental_delta
RESULTS_API_ENABLE_STATIC_FALLBACK=false
```

Meaning:

- `GOVERNOR_RESULTS_PREFIX` is the shared default public folder for governor results.
- `RESULTS_API_STATIC_RESULTS_PREFIX` is the static prefix read by `results-api`.
- `STATIC_RESULTS_PREFIX` is the static prefix written by `line-relay`.
- `RESULTS_API_DEFAULT_DATA_MODE=incremental_delta` sums every approved report in a district.
- `RESULTS_API_ENABLE_STATIC_FALLBACK=false` prevents the public API from falling back to stale static JSON when approved data exists.

`compose.yaml` now passes these env vars through to both `results-api` and
`line-relay`, so the folder can be changed per environment without code edits.

For the current prod env template, these three prefix vars are set to
`api-data/governor-results-dev`.

## Public Export Behavior

`line-relay` now exports static files by calling the fresh endpoints:

- `/api/v1/governor-results/summary?fresh=1`
- `/api/v1/governor-results/districts?fresh=1`

This avoids writing cached responses back to S3.

The current public export paths are:

- `s3://<STATIC_RESULTS_S3_BUCKET>/<STATIC_RESULTS_PREFIX>/sumary.json`
- `s3://<STATIC_RESULTS_S3_BUCKET>/<STATIC_RESULTS_PREFIX>/districts.json`

For the current prod setup this resolves to:

- `s3://ch7-static-bkkelection2569/api-data/governor-results-dev/sumary.json`
- `s3://ch7-static-bkkelection2569/api-data/governor-results-dev/districts.json`

## District Mapping

District metadata is read from `RESULTS_API_DISTRICTS_URL`.

For Bangkok, the current runtime expects district master data from:

- `s3://<bucket>/api-data/master-data/election-areas-bangkok.json`

Area mapping uses the district `number` field from that file. The parser also
supports the current top-level `electionAreas` shape.

## Summary Field Sources

`summary` fields come from multiple sources:

- `countedUnits`: number of districts with at least one approved result
- `totalUnits`: number of districts from district master data
- `countedPercentage`: `countedUnits / totalUnits * 100`
- `lastUpdatedAt`: latest `approved_at` among included results

The voter and ballot totals come from the approved OCR result payloads:

- `eligibleVoters`
- `voterTurnout`
- `validBallots`
- `invalidBallots`
- `abstainedBallots`

Current aggregation rules:

- `eligibleVoters` and `voterTurnout` can be partially summed if at least one district has a value.
- `validBallots`, `invalidBallots`, and `abstainedBallots` stay `null` unless the approved data is complete enough to compute them safely.
- Percentage fields based on missing ballot totals also stay `null`.

If the approved OCR result does not contain these upstream fields, the public
summary will return `null` even if `countedUnits` is already greater than zero.

## Approved Result Semantics

`results-api` now builds public governor results from approved submissions, not
from the legacy static files.

It also supports reading absolute S3 keys already stored in manifests, so
`current_draft_key` and `current_approval_key` can point directly to full keys.

## Runtime Editing Points

If you need to change the running prod behavior, edit:

- `/opt/election/.env` on the prod EC2 host
- `deploy/ec2/election-platform-prod.env.example` in the repo

Then redeploy or restart the affected containers so they reload the new env.

Changing only the repo template does not update the running host until the next
deploy. Changing only `/opt/election/.env` fixes the live host, but the next
deploy can overwrite that behavior if the repo template still disagrees.

## Production Baseline

Current prod alignment target:

- provider: `openrouter`
- model: `anthropic/claude-sonnet-4.6`
- API base: `https://openrouter.ai/api/v1`
- source bucket: `ch7-static-bkkelection2569`
- source score prefix: `api-data/score`
- public governor-results prefix: `api-data/governor-results-dev`

Runtime URL currently used for checks:

- `https://54.254.164.95.sslip.io`

Webhook path:

- `https://54.254.164.95.sslip.io/line/webhook`

## Required AWS Access

The EC2 instance profile must cover both the source and public-export prefixes:

- read/write access for `api-data/score/*`
- read/write access for `api-data/governor-results-dev/*` or the configured public prefix

At minimum the stack needs:

- `s3:GetObject`
- `s3:PutObject`
- `s3:ListBucket`
- `sqs:SendMessage`
- `sqs:ReceiveMessage`
- `sqs:DeleteMessage`
- `sqs:GetQueueAttributes`
- `sqs:ChangeMessageVisibility`
