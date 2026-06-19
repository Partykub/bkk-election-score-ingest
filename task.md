# Task: Election Monitor/Admin Page

## Goal

สร้างหน้า monitor สำหรับ operator เพื่อดูสถานะข้อมูลผลเลือกตั้งรายเขต, ตรวจว่าข้อมูลแต่ละเขตเข้ามากี่ครั้ง, เห็น field ที่ยังขาด, และสามารถเติมหรือแก้ข้อมูลที่จำเป็น เช่น `pageMeta.title`, `resultStatus`, และ summary fields ได้โดยมี audit trail

## Current Context

- ระบบมี `results-api` อยู่แล้วที่ `hermes/results_api/app.py`
- API ที่มีแล้ว:
  - `GET /api/v1/governor-results/summary`
  - `GET /api/v1/governor-results/districts`
  - `GET /api/v1/elections/{election_id}/areas`
  - `GET /api/v1/elections/{election_id}/areas/{area_id}`
  - `GET /api/v1/elections/{election_id}/areas/{area_id}/submissions`
- ข้อมูล approved results อ่านจาก S3 ผ่าน `ResultsStore`
- Master districts ใช้สำหรับ map เขตกรุงเทพฯ 50 เขต
- Requirement หลักจาก `requirement_monitor.md`:
  - ต้องเห็นว่าแต่ละเขตมีข้อมูลเข้ามากี่ครั้ง
  - ต้องเห็นว่าแต่ละเขตขาดข้อมูลอะไร
  - ต้องเพิ่มหรือแก้ข้อมูลได้
  - ต้องแก้ค่าใน JSON เช่น `pageMeta.title` ได้
  - ใช้เป็นหน้า monitor สำหรับจัดการเหตุการณ์ไม่คาดคิด

## Phase 1: Monitor Read Model

### Phase 1 implementation status

- [x] Added pure monitor helpers in `hermes/results_api/app.py`
- [x] Added district status calculation: `no_data`, `pending`, `missing_fields`, `complete`, `delayed`, `conflict`
- [x] Added `submissionCount`, `approvedSubmissionCount`, `latestSubmittedAt`, `latestApprovedAt`
- [x] Added missing field detection for candidate scores and turnout/ballot fields
- [x] Added validation warnings for malformed scores and turnout/ballot consistency
- [x] Added monitor districts payload builder
- [x] Added monitor overview payload builder
- [x] Added unit tests for Phase 1 read model

### 1.1 Define monitor district status

- [ ] เพิ่ม helper สำหรับคำนวณสถานะรายเขต เช่น `build_monitor_districts`
- [ ] สถานะที่รองรับ:
  - `no_data`: เขตยังไม่มี submission
  - `pending`: มี submission แต่ยังไม่มี approved result
  - `missing_fields`: มี approved result แต่ข้อมูลสำคัญยังไม่ครบ
  - `complete`: ข้อมูลหลักครบ
  - `delayed`: ข้อมูลล่าสุดเก่ากว่า threshold
  - `conflict`: มี approved result หลายรายการที่ควรให้ operator ตรวจ
- [ ] คำนวณ `submissionCount`
- [ ] คำนวณ `approvedSubmissionCount`
- [ ] คำนวณ `latestSubmittedAt` ถ้ามีข้อมูลใน index
- [ ] คำนวณ `latestApprovedAt`
- [ ] คำนวณ `missingFields`
- [ ] คำนวณ `warnings`

### 1.2 Required fields for completeness

- [ ] กำหนด field ที่ใช้ตรวจความครบถ้วนระดับเขต:
  - `area_id`
  - `candidate_scores`
  - `eligible_voters`
  - `voter_turnout`
  - `valid_ballots`
  - `invalid_ballots`
  - `abstained_ballots`
- [ ] `candidate_scores` ต้องมีอย่างน้อย 1 รายการ
- [ ] score แต่ละรายการต้องมี `candidate_number` และ `score`
- [ ] ถ้า `voter_turnout`, `valid_ballots`, `invalid_ballots`, `abstained_ballots` มีครบ ให้ตรวจว่า:
  - `voter_turnout == valid_ballots + invalid_ballots + abstained_ballots`
- [ ] ถ้ามี `eligible_voters` และ `voter_turnout` ให้ตรวจว่า:
  - `voter_turnout <= eligible_voters`

### 1.3 Monitor overview payload

- [ ] สร้าง response รูปแบบนี้:

```json
{
  "schemaVersion": "1.0",
  "resource": "election-monitor",
  "generatedAt": "2026-06-16T03:12:25.334Z",
  "electionId": "bkk-governor-2026",
  "overview": {
    "totalDistricts": 50,
    "districtsWithData": 10,
    "districtsWithoutData": 40,
    "completeDistricts": 5,
    "incompleteDistricts": 5,
    "delayedDistricts": 2,
    "conflictDistricts": 0,
    "latestApprovedAt": "2026-06-15T11:19:48Z"
  },
  "dataQuality": {
    "isComplete": false,
    "warnings": []
  }
}
```

### 1.4 Monitor districts payload

- [ ] สร้าง response รูปแบบนี้:

```json
{
  "schemaVersion": "1.0",
  "resource": "election-monitor-districts",
  "generatedAt": "2026-06-16T03:12:25.334Z",
  "electionId": "bkk-governor-2026",
  "districts": [
    {
      "areaId": "3",
      "districtCode": 1003,
      "districtNameTh": "หนองจอก",
      "districtNameEn": "Nong Chok",
      "submissionCount": 3,
      "approvedSubmissionCount": 1,
      "latestApprovedAt": "2026-06-15T11:19:48Z",
      "status": "missing_fields",
      "missingFields": ["eligible_voters", "voter_turnout"],
      "warnings": [],
      "leadingCandidateId": "pongsak"
    }
  ]
}
```

## Phase 2: Read API

### Phase 2 implementation status

- [x] Added `GET /api/v1/monitor/overview`
- [x] Added `GET /api/v1/monitor/districts`
- [x] Added `GET /api/v1/monitor/districts/{area_id}`
- [x] Added local monitor page at `GET /monitor`
- [x] Added endpoint tests for monitor page and read APIs

### 2.1 Add monitor endpoints

- [ ] เพิ่ม endpoint ใน `hermes/results_api/app.py`
  - `GET /api/v1/monitor/overview`
  - `GET /api/v1/monitor/districts`
  - `GET /api/v1/monitor/districts/{area_id}`
- [ ] ใช้ `require_api_key` เหมือน endpoint อื่น
- [ ] ใช้ `settings.source_election_id` เป็น source default เหมือน governor results
- [ ] รองรับ query `electionId` ในอนาคต ถ้าจำเป็น

### 2.2 District detail endpoint

- [ ] `GET /api/v1/monitor/districts/{area_id}` ต้องคืน:
  - district metadata
  - submission count
  - approved submission count
  - latest approved result
  - submissions ล่าสุด
  - missing fields
  - warnings
  - computed status
- [ ] ถ้าเขตอยู่ใน master districts แต่ยังไม่มีข้อมูล ต้องไม่ 404
- [ ] 404 เฉพาะกรณี `area_id` ไม่อยู่ใน master districts

## Phase 3: Manual Override / Admin Write API

### Phase 3 implementation status

- [x] Added S3 write support in `ResultsStore`
- [x] Added `GET /api/v1/monitor/overrides`
- [x] Added `PATCH /api/v1/monitor/page-meta`
- [x] Added `PATCH /api/v1/monitor/districts/{area_id}/summary`
- [x] Kept `PATCH /api/v1/monitor/summary` as a rejected compatibility route so operators use district-level entry
- [x] Added `GET /api/v1/monitor/audit-events`
- [x] Applied page metadata overrides and aggregated district summary overrides to public governor summary response
- [x] Added monitor audit events for page metadata and summary updates
- [x] Added local monitor forms for page metadata and district-level summary overrides
- [x] Added tests for override writes, validation, audit, cache invalidation, and raw result preservation

> ทำหลัง read-only monitor ใช้งานได้แล้ว

### 3.1 Configurable page metadata

- [ ] เพิ่มที่เก็บ admin override สำหรับ page metadata
- [ ] รองรับ field:
  - `title`
  - `resultStatus`
- [ ] เพิ่ม endpoint:
  - `PATCH /api/v1/monitor/page-meta`
- [ ] เมื่อมี override ให้ `governor_results_response()` ใช้ค่าจาก override แทน env config
- [ ] ทุกการแก้ต้องเขียน audit event

### 3.2 Summary manual override

- [ ] เพิ่มที่เก็บ manual override สำหรับ summary fields
- [ ] รองรับ field:
  - `eligibleVoters`
  - `voterTurnout`
  - `validBallots`
  - `invalidBallots`
  - `abstainedBallots`
- [ ] เพิ่ม endpoint:
  - `PATCH /api/v1/monitor/summary`
- [ ] Validate numeric fields ต้องเป็น integer >= 0
- [ ] Validate turnout consistency ถ้าข้อมูลครบ
- [ ] ทุกการแก้ต้องเขียน audit event

### 3.3 District manual entry

- [ ] เพิ่ม endpoint:
  - `POST /api/v1/monitor/districts/{area_id}/manual-entry`
- [ ] ใช้สำหรับกรณี OCR/LINE มีปัญหาและ operator ต้องกรอกผลเอง
- [ ] สร้าง record เป็น manual revision แยกจาก raw OCR result
- [ ] ต้องมี:
  - `area_id`
  - `candidate_scores`
  - `entered_by`
  - `reason`
  - `created_at`
- [ ] manual entry ต้องมี audit trail
- [ ] ห้ามแก้ raw OCR artifacts เดิมโดยตรง

## Phase 4: Audit Trail

### 4.1 Audit event schema

- [ ] นิยาม audit event สำหรับ monitor:

```json
{
  "schema_version": "2026-06-16",
  "entity_type": "monitor_audit_event",
  "event_id": "audit_20260616_0001",
  "event_type": "page_meta_updated",
  "election_id": "bkk-governor-2026",
  "area_id": null,
  "actor": "operator",
  "before": {},
  "after": {},
  "reason": "manual correction",
  "created_at": "2026-06-16T03:12:25Z"
}
```

### 4.2 Audit event types

- [ ] รองรับ event type:
  - `page_meta_updated`
  - `summary_override_updated`
  - `district_manual_entry_created`
  - `district_status_marked_resolved`
  - `warning_acknowledged`

### 4.3 Audit endpoints

- [ ] เพิ่ม endpoint:
  - `GET /api/v1/monitor/audit-events`
  - `GET /api/v1/monitor/districts/{area_id}/audit-events`
- [ ] รองรับ `limit`
- [ ] เรียงจากใหม่ไปเก่า

## Phase 5: Monitor UI

> ทำเมื่อ API read-only พร้อมแล้ว

### 5.1 Overview screen

- [ ] แสดง cards:
  - Total districts
  - Districts with data
  - Missing districts
  - Incomplete districts
  - Delayed districts
  - Latest update time
- [ ] แสดง global warnings จาก `dataQuality.warnings`
- [ ] มี refresh button

### 5.2 District table

- [ ] แสดงตาราง 50 เขต
- [ ] Columns:
  - เขต
  - submission count
  - approved count
  - latest approved at
  - leading candidate
  - status
  - missing fields
  - actions
- [ ] Filter:
  - all
  - no data
  - missing fields
  - delayed
  - complete
- [ ] Search ตามชื่อเขตหรือ district code

### 5.3 District detail

- [ ] แสดงข้อมูลเขต
- [ ] แสดง latest approved result
- [ ] แสดง submissions ล่าสุด
- [ ] แสดง missing fields และ warnings
- [ ] แสดง audit trail ของเขต
- [ ] มี action สำหรับ manual entry หรือ mark resolved

### 5.4 Edit forms

- [ ] Form แก้ `pageMeta.title`
- [ ] Form เปลี่ยน `resultStatus`
- [ ] Form เติม summary fields
- [ ] Form manual district entry
- [ ] ต้องแสดง validation error ก่อน submit
- [ ] หลัง submit ต้อง refresh monitor data

## Phase 6: Tests

### 6.1 Unit tests

- [ ] เพิ่ม test ใน `hermes/results_api/test_app.py`
- [ ] Test `build_monitor_districts`
  - เขตไม่มีข้อมูล -> `no_data`
  - เขตมี submission แต่ไม่มี approved -> `pending`
  - เขตมี approved แต่ขาด field -> `missing_fields`
  - เขตมีข้อมูลครบ -> `complete`
  - เขตข้อมูลเก่าเกิน threshold -> `delayed`
- [ ] Test missing fields calculation
- [ ] Test turnout consistency validation
- [ ] Test overview aggregation

### 6.2 API tests

- [ ] Test `GET /api/v1/monitor/overview`
- [ ] Test `GET /api/v1/monitor/districts`
- [ ] Test `GET /api/v1/monitor/districts/{area_id}`
- [ ] Test unknown area returns 404
- [ ] Test known area without data returns monitor status instead of 404

### 6.3 Write API tests

- [ ] Test update page meta override
- [ ] Test update summary override
- [ ] Test invalid numeric summary field returns 422 or 400
- [ ] Test manual district entry writes audit event
- [ ] Test raw OCR result is not mutated

## Implementation Order

1. เพิ่ม pure helper สำหรับ monitor read model ใน `hermes/results_api/app.py`
2. เพิ่ม unit tests ของ helper
3. เพิ่ม read-only monitor endpoints
4. เพิ่ม API tests
5. เพิ่ม storage schema สำหรับ admin overrides และ audit events
6. เพิ่ม write endpoints สำหรับ page meta และ summary
7. เพิ่ม manual district entry
8. เพิ่ม UI monitor
9. เพิ่ม runbook สั้น ๆ สำหรับ operator

## Acceptance Criteria

- [ ] Operator เห็นครบ 50 เขตในหน้า monitor แม้บางเขตยังไม่มีข้อมูล
- [ ] Operator เห็นจำนวนครั้งที่ข้อมูลแต่ละเขตเข้ามา
- [ ] Operator เห็นได้ว่าแต่ละเขตขาดข้อมูลอะไร
- [ ] Operator แก้ `pageMeta.title` ได้
- [ ] Operator เปลี่ยน `resultStatus` ได้
- [ ] Operator เติม summary fields ที่เป็น `null` ได้
- [ ] ทุกการแก้ไขมี audit event
- [ ] API เดิมของ public results ยังทำงานเหมือนเดิม
- [ ] Tests ผ่านสำหรับ results API

## Out of Scope for First Iteration

- ระบบ login/admin RBAC เต็มรูปแบบ
- realtime websocket
- multi-region failover
- dashboard ขั้นสูงสำหรับ analytics
- การแก้ raw OCR artifacts เดิมโดยตรง
