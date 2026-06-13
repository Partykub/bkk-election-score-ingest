# S3 Manifest Schema

เอกสารนี้นิยาม manifest schema หลักทั้งหมดสำหรับระบบเลือกตั้งใน baseline ปัจจุบันที่ใช้ S3 เป็น source of truth ของ workflow state และ artifacts

## ขอบเขต

เอกสารนี้ครอบคลุม:

- object keys หลักบน S3
- schema ของ manifests แต่ละชนิด
- common metadata fields
- state transitions ระดับ entity
- indexes แบบ derived ที่สร้างใหม่ได้จาก manifests

เอกสารนี้ไม่ครอบคลุม:

- LINE webhook payload เต็มรูปแบบ
- prompt ภายในของ Hermes
- AWS target API contract

## หลักการ

- manifests บน S3 คือ source of truth
- ใช้ key ที่ deterministic และอ่านกลับได้ตรงๆ
- พยายามเขียน immutable artifacts ก่อน แล้วค่อยขยับ pointer ล่าสุด
- downstream workers ต้องอ้างอิง IDs และ S3 keys ไม่ควร parse จาก free text ซ้ำ
- indexes เป็น derived data ที่ rebuild ได้จาก manifests ถ้าจำเป็น

## Prefix layout

ตัวอย่าง bucket:

```text
s3://election-system/
  messages/{source_message_id}/
    manifest.json
    original.bin
    upload_metadata.json
    ocr_job.json
    draft_r{revision}.json
    draft_latest.json
    approval_r{revision}.json
    approval_latest.json
    update_job.json
  sessions/{workflow_session_id}/
    latest.json
  events/{line_event_id}.json
  audit/
```

## Common field conventions

field ร่วมที่ควรใช้ให้สม่ำเสมอ:

- `schema_version`: เวอร์ชันของ schema เอกสารนั้น เช่น `2026-06-09`
- `entity_type`: ชนิดของ manifest เช่น `source_message`, `ocr_job`, `draft`, `approval`, `update_job`, `audit_event`
- `entity_id`: primary ID ของ entity นั้น
- `created_at`: เวลา UTC แบบ ISO 8601
- `updated_at`: เวลา UTC แบบ ISO 8601
- `workflow_session_id`: session ที่ผูกกับ conversation หรือ reporting thread
- `source_message_id`: ID ของ source message ต้นทางเมื่อ entity นั้นมีความเกี่ยวข้อง

## 1. Inbound metadata

ใช้เก็บ metadata ของไฟล์ต้นฉบับที่ดาวน์โหลดจาก LINE และอัปโหลดขึ้น S3 แล้ว

key:

```text
messages/{source_message_id}/original.bin
messages/{source_message_id}/upload_metadata.json
```

ตัวอย่าง `metadata.json`:

```json
{
  "schema_version": "2026-06-09",
  "entity_type": "inbound_metadata",
  "entity_id": "src_20260609_0001",
  "source_message_id": "src_20260609_0001",
  "workflow_session_id": "line_group_C123",
  "platform": "line",
  "line_event_id": "01JY...",
  "line_message_id": "548899112233",
  "sender_user_id": "Uxxxxxxxx",
  "sender_group_id": "Cxxxxxxxx",
  "sender_room_id": null,
  "content_type": "image/jpeg",
  "size_bytes": 2481931,
  "object_key": "messages/src_20260609_0001/original.bin",
  "object_etag": "9f6200f6...",
  "received_at": "2026-06-09T06:30:00Z",
  "uploaded_at": "2026-06-09T06:30:04Z",
  "created_at": "2026-06-09T06:30:04Z",
  "updated_at": "2026-06-09T06:30:04Z"
}
```

field สำคัญ:

- `source_message_id`
- `workflow_session_id`
- `platform`
- `line_event_id`
- `line_message_id`
- `content_type`
- `size_bytes`
- `object_key`

## 2. Source message manifest

ใช้เป็น root entity ของแต่ละ inbound message

key:

```text
messages/{source_message_id}/manifest.json
```

ตัวอย่าง:

```json
{
  "schema_version": "2026-06-09",
  "entity_type": "source_message",
  "entity_id": "src_20260609_0001",
  "source_message_id": "src_20260609_0001",
  "workflow_session_id": "line_group_C123",
  "platform": "line",
  "line_event_id": "01JY...",
  "line_message_id": "548899112233",
  "line_reply_token": "abcd...",
  "source_type": "image",
  "source_text": null,
  "sender_user_id": "Uxxxxxxxx",
  "sender_group_id": "Cxxxxxxxx",
  "sender_room_id": null,
  "state": "stored",
  "dedupe": {
    "event_key": "line:event:01JY...",
    "message_key": "line:message:548899112233"
  },
  "media": {
    "bucket": "election-system",
    "original_key": "messages/src_20260609_0001/original.bin",
    "metadata_key": "messages/src_20260609_0001/upload_metadata.json",
    "content_type": "image/jpeg",
    "size_bytes": 2481931
  },
  "current": {
    "draft_id": null,
    "draft_key": null,
    "approval_id": null,
    "approval_key": null,
    "update_job_id": null,
    "update_job_key": null,
    "exception_id": null,
    "exception_key": null
  },
  "created_at": "2026-06-09T06:30:00Z",
  "updated_at": "2026-06-09T06:30:05Z"
}
```

state ที่รองรับ:

- `received`
- `stored`
- `queued`
- `ocr_processing`
- `awaiting_approval`
- `approved`
- `rejected`
- `updating`
- `updated`
- `exception`

field ขั้นต่ำ:

- `source_message_id`
- `workflow_session_id`
- `platform`
- `source_type`
- `state`
- `sender_user_id`
- `created_at`
- `updated_at`

## 3. OCR job manifest

ใช้แทน job record สำหรับ OCR worker

key:

```text
messages/{source_message_id}/ocr_job.json
```

ตัวอย่าง:

```json
{
  "schema_version": "2026-06-09",
  "entity_type": "ocr_job",
  "entity_id": "ocr_20260609_0001",
  "ocr_job_id": "ocr_20260609_0001",
  "source_message_id": "src_20260609_0001",
  "workflow_session_id": "line_group_C123",
  "state": "queued",
  "queue_name": "ocr-jobs",
  "attempt_count": 0,
  "max_attempts": 5,
  "requested_by": "hermes-supervisor",
  "input": {
    "bucket": "election-system",
    "key": "messages/src_20260609_0001/original.bin",
    "metadata_key": "messages/src_20260609_0001/upload_metadata.json",
    "content_type": "image/jpeg",
    "size_bytes": 2481931
  },
  "line_context": {
    "platform": "line",
    "line_event_id": "01JY...",
    "line_message_id": "548899112233",
    "sender_user_id": "Uxxxxxxxx",
    "sender_group_id": "Cxxxxxxxx",
    "sender_room_id": null
  },
  "ocr_options": {
    "language_hint": "th",
    "expected_document_type": "election_score_sheet",
    "prompt_version": "ocr-v1",
    "model_name": "gemma-vision"
  },
  "result": null,
  "error": null,
  "created_at": "2026-06-09T06:30:06Z",
  "updated_at": "2026-06-09T06:30:06Z"
}
```

state ที่รองรับ:

- `queued`
- `processing`
- `completed`
- `failed`

field ขั้นต่ำ:

- `ocr_job_id`
- `source_message_id`
- `workflow_session_id`
- `state`
- `queue_name`
- `attempt_count`
- `max_attempts`
- `input.bucket`
- `input.key`

## 4. Draft manifest

ใช้เก็บ normalized output ของ OCR worker และ revision history

key:

```text
messages/{source_message_id}/draft_r{revision}.json
messages/{source_message_id}/draft_latest.json
```

หลักการ:

- `revision-{revision}.json` เป็น immutable artifact
- `latest.json` เป็น pointer ที่ชี้ draft revision ล่าสุด

ตัวอย่าง revision:

```json
{
  "schema_version": "2026-06-09",
  "entity_type": "draft",
  "entity_id": "draft_20260609_0001_r1",
  "draft_id": "draft_20260609_0001_r1",
  "source_message_id": "src_20260609_0001",
  "ocr_job_id": "ocr_20260609_0001",
  "revision": 1,
  "status": "awaiting_approval",
  "election_id": "election-2026",
  "area_id": "12",
  "polling_unit_id": null,
  "report_type": "score_sheet",
  "observed_at": null,
  "result_signature": "area12:1=12345|2=11980|3=8750",
  "overall_confidence": 0.91,
  "validation_flags": [],
  "image_quality_flags": [],
  "candidate_scores": [
    {
      "candidate_number": 1,
      "candidate_name": null,
      "score": 12345,
      "confidence": 0.97,
      "raw_text": "เบอร์ 1 12,345"
    },
    {
      "candidate_number": 2,
      "candidate_name": null,
      "score": 11980,
      "confidence": 0.94,
      "raw_text": "เบอร์ 2 11,980"
    }
  ],
  "notes": null,
  "raw_model_output": {
    "text": "...raw Hermes response..."
  },
  "model_name": "gemma-vision",
  "prompt_version": "ocr-v1",
  "created_by": "ocr-worker",
  "created_at": "2026-06-09T06:31:10Z",
  "updated_at": "2026-06-09T06:31:10Z"
}
```

ตัวอย่าง `latest.json`:

```json
{
  "schema_version": "2026-06-09",
  "entity_type": "draft_pointer",
  "entity_id": "src_20260609_0001",
  "source_message_id": "src_20260609_0001",
  "draft_id": "draft_20260609_0001_r1",
  "draft_key": "messages/src_20260609_0001/draft_r1.json",
  "revision": 1,
  "updated_at": "2026-06-09T06:31:10Z"
}
```

field ขั้นต่ำ:

- `draft_id`
- `source_message_id`
- `ocr_job_id`
- `revision`
- `status`
- `raw_model_output`
- `overall_confidence`
- `validation_flags`
- `candidate_scores`

## 5. Approval manifest

ใช้เก็บ approval prompt และผลการตอบกลับของผู้ใช้

key:

```text
messages/{source_message_id}/approval_r{revision}.json
messages/{source_message_id}/approval_latest.json
```

ตัวอย่าง:

```json
{
  "schema_version": "2026-06-09",
  "entity_type": "approval",
  "entity_id": "approval_20260609_0001_r1",
  "approval_id": "approval_20260609_0001_r1",
  "source_message_id": "src_20260609_0001",
  "draft_id": "draft_20260609_0001_r1",
  "draft_revision": 1,
  "state": "awaiting_approval",
  "requested_from_user_id": "Uxxxxxxxx",
  "requested_via": "line_button",
  "requested_at": "2026-06-09T06:31:15Z",
  "expires_at": "2026-06-09T08:31:15Z",
  "responded_at": null,
  "response_type": null,
  "response_source_message_id": null,
  "approved_by_user_id": null,
  "rejected_by_user_id": null,
  "approval_note": null,
  "created_at": "2026-06-09T06:31:15Z",
  "updated_at": "2026-06-09T06:31:15Z"
}
```

state ที่รองรับ:

- `awaiting_approval`
- `approved`
- `rejected`
- `expired`

field ขั้นต่ำ:

- `approval_id`
- `source_message_id`
- `draft_id`
- `draft_revision`
- `state`
- `requested_from_user_id`
- `requested_at`

## 6. Update job manifest

ใช้เก็บงานที่พร้อมส่งไป AWS target API แล้ว

key:

```text
messages/{source_message_id}/update_job.json
```

ตัวอย่าง:

```json
{
  "schema_version": "2026-06-09",
  "entity_type": "update_job",
  "entity_id": "upd_20260609_0001",
  "update_job_id": "upd_20260609_0001",
  "source_message_id": "src_20260609_0001",
  "draft_id": "draft_20260609_0001_r1",
  "approval_id": "approval_20260609_0001_r1",
  "workflow_session_id": "line_group_C123",
  "state": "queued",
  "queue_name": "update-jobs",
  "attempt_count": 0,
  "max_attempts": 5,
  "idempotency_key": "election-2026:area12:area12:1=12345|2=11980|3=8750",
  "payload": {
    "election_id": "election-2026",
    "area_id": "12",
    "candidate_scores": [
      {
        "candidate_number": 1,
        "score": 12345
      },
      {
        "candidate_number": 2,
        "score": 11980
      }
    ]
  },
  "result": null,
  "error": null,
  "created_at": "2026-06-09T06:32:00Z",
  "updated_at": "2026-06-09T06:32:00Z"
}
```

state ที่รองรับ:

- `queued`
- `processing`
- `completed`
- `failed`

field ขั้นต่ำ:

- `update_job_id`
- `source_message_id`
- `draft_id`
- `approval_id`
- `state`
- `queue_name`
- `idempotency_key`
- `payload`

## 7. Audit event manifest

ใช้เก็บ event trail แบบ append-only สำหรับ trace การทำงานย้อนหลัง

key:

```text
audit/{yyyy}/{mm}/{dd}/{source_message_id}/{timestamp}_{event_type}.json
```

ตัวอย่าง:

```json
{
  "schema_version": "2026-06-09",
  "entity_type": "audit_event",
  "entity_id": "audit_20260609_063005_message_received",
  "source_message_id": "src_20260609_0001",
  "workflow_session_id": "line_group_C123",
  "event_type": "message_received",
  "actor_type": "system",
  "actor_id": "hermes-supervisor",
  "related": {
    "ocr_job_id": null,
    "draft_id": null,
    "approval_id": null,
    "update_job_id": null
  },
  "details": {
    "state": "stored",
    "line_event_id": "01JY..."
  },
  "created_at": "2026-06-09T06:30:05Z",
  "updated_at": "2026-06-09T06:30:05Z"
}
```

field ขั้นต่ำ:

- `entity_id`
- `source_message_id`
- `event_type`
- `actor_type`
- `actor_id`
- `details`
- `created_at`

## 8. Exception manifest

ใช้เก็บข้อผิดพลาดที่ต้องรอการแก้, manual review, หรือ retry policy พิเศษ

key:

```text
exceptions/{source_message_id}/{exception_id}.json
exceptions/{source_message_id}/latest.json
```

ตัวอย่าง:

```json
{
  "schema_version": "2026-06-09",
  "entity_type": "exception",
  "entity_id": "exc_20260609_0001",
  "exception_id": "exc_20260609_0001",
  "source_message_id": "src_20260609_0001",
  "workflow_session_id": "line_group_C123",
  "state": "open",
  "category": "ocr_failure",
  "code": "LOW_CONFIDENCE",
  "message": "OCR confidence below acceptance threshold",
  "severity": "warning",
  "retryable": false,
  "related": {
    "ocr_job_id": "ocr_20260609_0001",
    "draft_id": null,
    "approval_id": null,
    "update_job_id": null
  },
  "created_at": "2026-06-09T06:31:20Z",
  "updated_at": "2026-06-09T06:31:20Z"
}
```

state ที่รองรับ:

- `open`
- `resolved`
- `ignored`

## 9. Derived indexes

indexes ไม่ใช่ source of truth และสามารถ rebuild ได้จาก manifests

key ที่แนะนำ:

```text
events/{line_event_id}.json
events/{line_message_id}.json
sessions/{workflow_session_id}/latest.json
indexes/by-area/{election_id}/{area_id}/latest-approved.json
```

ตัวอย่าง index โดย `line_event_id`:

```json
{
  "schema_version": "2026-06-09",
  "entity_type": "index_line_event",
  "entity_id": "01JY...",
  "line_event_id": "01JY...",
  "source_message_id": "src_20260609_0001",
  "source_message_key": "messages/src_20260609_0001/manifest.json",
  "created_at": "2026-06-09T06:30:05Z",
  "updated_at": "2026-06-09T06:30:05Z"
}
```

ตัวอย่าง latest approved per area:

```json
{
  "schema_version": "2026-06-09",
  "entity_type": "index_area_latest_approved",
  "entity_id": "election-2026:12",
  "election_id": "election-2026",
  "area_id": "12",
  "draft_id": "draft_20260609_0001_r1",
  "draft_key": "messages/src_20260609_0001/draft_r1.json",
  "approval_id": "approval_20260609_0001_r1",
  "update_job_id": "upd_20260609_0001",
  "result_signature": "area12:1=12345|2=11980|3=8750",
  "updated_at": "2026-06-09T06:32:20Z"
}
```

## State transition summary

### Source message

`received -> stored -> queued -> ocr_processing -> awaiting_approval -> approved -> updating -> updated`

ทางเลือกอื่น:

- `awaiting_approval -> rejected`
- `queued -> exception`
- `ocr_processing -> exception`
- `updating -> exception`

### OCR job

`queued -> processing -> completed`

หรือ:

`queued -> processing -> failed`

### Approval

`awaiting_approval -> approved`

หรือ:

- `awaiting_approval -> rejected`
- `awaiting_approval -> expired`

### Update job

`queued -> processing -> completed`

หรือ:

`queued -> processing -> failed`

## Minimum write sequence

เมื่อรับรูปใหม่ 1 รายการ ระบบควรเขียนอย่างน้อยตามลำดับนี้:

1. `messages/{source_message_id}/original.bin`
2. `messages/{source_message_id}/upload_metadata.json`
3. `messages/{source_message_id}/manifest.json`
4. `events/{line_event_id}.json`
5. `events/{line_message_id}.json`
6. `messages/{source_message_id}/ocr_job.json`
7. `audit/.../message_received.json`

หลัง OCR สำเร็จ:

1. `messages/{source_message_id}/draft_r{revision}.json`
2. `messages/{source_message_id}/draft_latest.json`
3. `messages/{source_message_id}/approval_r{revision}.json`
4. `messages/{source_message_id}/approval_latest.json`
5. `audit/.../draft_created.json`

หลัง approval และ update สำเร็จ:

1. `messages/{source_message_id}/update_job.json`
2. `indexes/by-area/{election_id}/{area_id}/latest-approved.json`
3. `audit/.../approved.json`
4. `audit/.../updated.json`

## Open points

เรื่องที่ยังต้อง finalize ต่อจาก schema นี้:

- payload contract ของ AWS target API
- prompt contract ระหว่าง OCR Worker กับ Hermes
- retention policy ของ raw model output และ audit objects
- strategy การ rebuild indexes เมื่อเกิด partial failure