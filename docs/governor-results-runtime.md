# Governor Results Runtime

This document captures the current `governor-results` behavior after the dev/prod
alignment work and the public-source switch.

## Three Public Folders

Governor public JSON is split into three S3 prefixes under the same parent
(typically `api-data/`):

| Prefix | Role |
| --- | --- |
| `governor-results-dev` | LINE/OCR ingest (written by `line-relay`) |
| `governor-results-bkk` | กทม external ingest (`sumary.json`, `districts.json`, `raw/*`) |
| `governor-results` | **Live** folder read by the frontend |

Ingest paths are independent. Only one source is **promoted** (copied) into
`governor-results` at a time.

## Active Public Source Switch

The active source is stored in S3:

- `monitor/config/active-public-source.json` → `{ "source": "line" | "bkk", "updatedAt": ... }`

Default when unset: **`line`**.

| `source` | Promote from | When promotion runs |
| --- | --- | --- |
| `line` | `governor-results-dev` | After each LINE static export |
| `bkk` | `governor-results-bkk` | After each กทม monitor fetch |

Promotion copies `sumary.json` and `districts.json` from the active ingest
folder into `governor-results`.

Changing the switch on `/monitor` (PUT `/api/v1/monitor/source` with
`activePublicSource`) saves the config and promotes immediately when possible.
If source files are missing, the switch is still saved and the response may
include `publicPromoteError`.

## Source Of Truth For The Public API

`results-api` builds governor summary/districts from **approved OCR results**
under the score prefix (`ELECTION_S3_PREFIX` / `RESULTS_API_S3_PREFIX`).

The external กทม endpoint configured on `/monitor` is **ingest only**. It no
longer overrides `/api/v1/governor-results/*` responses.

When `RESULTS_API_ENABLE_STATIC_FALLBACK=true` and there is no approved OCR
data, the API can fall back to static JSON in the **live** folder
(`governor-results`), not from `governor-results-dev` or `governor-results-bkk`
directly.

## LINE Ingest Path

1. LINE relay receives images and metadata.
2. OCR worker writes draft, approval, and source manifests under the score prefix.
3. `results-api` reads approved results from the score prefix.
4. `line-relay` exports fresh public JSON to `STATIC_RESULTS_PREFIX`
   (typically `governor-results-dev`).
5. If `active-public-source.json` has `source: "line"`, the exporter promotes
   `sumary.json` + `districts.json` into `governor-results`.

## กทม Mock Endpoint (Monitor)

หน้า [Monitor](/monitor) สามารถจำลอง upstream กทมได้โดยเขียนทับไฟล์ดิบที่ external URL ชี้อยู่ (เช่น
`api-data/governor-results-bkk/endpoint-mock/69-governor-electiondata.json`) แล้วให้ Auto Fetch ดึงตามปกติ

กฎเวลา:

- **Mock ทุก (วินาที)** ต้อง **น้อยกว่า** **Auto Fetch Every**
- ห่างกันอย่างน้อย **2 วินาที** เพื่อให้ mock เขียน S3 เสร็จก่อน fetch

Flow:

1. กด **เริ่มจำลอง** → เขียน snapshot เริ่มต้น (0 เขต) ไป endpoint-mock ทั้งผู้ว่าฯ และ ส.ก.
   - `endpoint-mock/69-governor-electiondata.json`
   - `endpoint-mock/69-bmc-electiondata.json`
2. ทุกช่วง Mock → เปิดเผยเขตเพิ่ม + สุ่มคะแนนคู่แข่ง + เขียนทับทั้งสองไฟล์
3. กด **เริ่มนับ** Auto Fetch → ดึง URL เดิม → export ไป `governor-results-bkk` → promote ถ้า switch เป็น กทม

## กทม Ingest Path (`/monitor`)

1. Operator saves an external JSON URL on `/monitor`.
2. Manual fetch or schedule calls the external endpoint.
3. Raw payload is archived under `governor-results-bkk/raw/*`.
4. Transformed `sumary.json` + `districts.json` are written to
   `governor-results-bkk`.
5. If `active-public-source` is `bkk`, those files are promoted to
   `governor-results`.

## ส.ก. / BMC Ingest Path (`/monitor`)

Monitor สามารถดึง `69-bmc-electiondata.json` คู่กับ governor ในรอบเดียวกันได้

1. Operator เปิดใช้ BMC endpoint บน `/monitor` (หรือตั้ง `RESULTS_API_EXTERNAL_BMC_RESULTS_URL`)
2. Manual fetch หรือ schedule ดึง BMC URL (local mock: `endpoint-mock/69-bmc-electiondata.json`)
3. Raw payload เก็บที่ `governor-results-bkk/bmc/raw/*`
4. Transform แล้วเขียนไป ingest folder เดียวกับผู้ว่าฯ:
   - `api-data/governor-results-bkk/sumary-sorkor.json`
   - `api-data/governor-results-bkk/districts-sorkor.json`
5. ถ้า `active-public-source` เป็น `bkk` จะ promote ทั้ง 4 ไฟล์ไป live:
   - `sumary.json`, `districts.json`, `sumary-sorkor.json`, `districts-sorkor.json`

ไฟล์ sorkor ใช้ logic เดียวกับผู้ว่าฯ — เขียน `governor-results-bkk` ก่อน แล้ว promote ไป `governor-results` เมื่อเลือก กทม เท่านั้น

## Key Env Vars

```dotenv
GOVERNOR_RESULTS_PREFIX=api-data/governor-results
RESULTS_API_STATIC_RESULTS_PREFIX=api-data/governor-results
STATIC_RESULTS_PREFIX=api-data/governor-results-dev
RESULTS_API_DEFAULT_DATA_MODE=latest_snapshot
RESULTS_API_ENABLE_STATIC_FALLBACK=false
RESULTS_API_EXTERNAL_BMC_RESULTS_URL=s3://ch7-static-bkkelection2569/api-data/governor-results-bkk/endpoint-mock/69-bmc-electiondata.json
RESULTS_API_SORKOR_ELECTION_ID=bkk-sorkor-2026
RESULTS_API_SORKOR_ELECTION_TITLE=ผลการเลือก ส.ก. กรุงเทพมหานคร
```

Meaning:

- `GOVERNOR_RESULTS_PREFIX` / `RESULTS_API_STATIC_RESULTS_PREFIX` — live folder
  (`governor-results`) used for static fallback reads.
- `STATIC_RESULTS_PREFIX` — LINE ingest folder (`governor-results-dev`).
- `RESULTS_API_DEFAULT_DATA_MODE=latest_snapshot` — use only the latest approved
  report in each district.
- `RESULTS_API_ENABLE_STATIC_FALLBACK=false` — do not fall back to stale static
  JSON when approved OCR data exists.

`compose.yaml` passes these env vars through to both `results-api` and
`line-relay`.

For the current prod env template, `STATIC_RESULTS_PREFIX` is often
`api-data/governor-results-dev` while the live folder stays
`api-data/governor-results`.

## Public Export Behavior (LINE)

`line-relay` exports static files by calling the fresh endpoints:

- `/api/v1/governor-results/summary?fresh=1`
- `/api/v1/governor-results/districts?fresh=1`

LINE ingest paths (current prod example):

- `s3://<bucket>/api-data/governor-results-dev/sumary.json`
- `s3://<bucket>/api-data/governor-results-dev/districts.json`

Live paths (frontend):

- `s3://<bucket>/api-data/governor-results/sumary.json`
- `s3://<bucket>/api-data/governor-results/districts.json`
- `s3://<bucket>/api-data/governor-results/sumary-sorkor.json` (promote จาก bkk เมื่อ active source = bkk)
- `s3://<bucket>/api-data/governor-results/districts-sorkor.json` (promote จาก bkk เมื่อ active source = bkk)

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

Nationwide summary fields are **partial sums**: each field is totaled from
districts that have that value, without waiting for every counted district to
report it. A field is `null` only when no approved district has provided it.

## Runtime Editing Points

If you need to change the running prod behavior, edit:

- `/opt/election/.env` on the prod EC2 host
- `deploy/ec2/election-platform-prod.env.example` in the repo

Then redeploy or restart the affected containers so they reload the new env.

Use `/monitor` to change the live public source (`line` vs `bkk`) without a
code deploy.

## Production Baseline

Current prod alignment target:

- provider: `openrouter`
- model: `anthropic/claude-sonnet-4.6`
- API base: `https://openrouter.ai/api/v1`
- source bucket: `ch7-static-bkkelection2569`
- source score prefix: `api-data/score`
- LINE ingest prefix: `api-data/governor-results-dev`
- live prefix: `api-data/governor-results`

Runtime URL currently used for checks:

- `https://54.254.164.95.sslip.io`

Webhook path:

- `https://54.254.164.95.sslip.io/line/webhook`

## Required AWS Access

The EC2 instance profile must cover score, ingest, and live prefixes:

- read/write access for `api-data/score/*`
- read/write access for `api-data/governor-results-dev/*`
- read/write access for `api-data/governor-results-bkk/*`
- read/write access for `api-data/governor-results/*`
- read/write access for `monitor/config/*` under the score prefix (active source)

At minimum the stack needs:

- `s3:GetObject`
- `s3:PutObject`
- `s3:ListBucket`
- `sqs:SendMessage`
- `sqs:ReceiveMessage`
- `sqs:DeleteMessage`
- `sqs:GetQueueAttributes`
- `sqs:ChangeMessageVisibility`
