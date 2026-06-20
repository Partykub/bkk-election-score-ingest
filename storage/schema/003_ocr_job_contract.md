# OCR Job Contract

เอกสารนี้นิยามสัญญาระหว่าง Hermes Supervisor กับ OCR Worker สำหรับการสร้าง OCR job, การอ่านข้อความจาก S3, และการคืนผล draft กลับเข้าสู่ระบบ

## เป้าหมาย

ทำให้ทั้งสองฝั่งเข้าใจตรงกันว่า:

- Supervisor จะเขียน `ocr job manifest` แบบใด
- OCR Worker ต้องอ่าน field ไหนเป็นอย่างต่ำ
- OCR Worker ต้องคืนผลลัพธ์ draft กลับมาในรูปแบบไหน
- ระบบจะถือว่าผล OCR สำเร็จ, ล้มเหลว, หรือเข้า exception จากเกณฑ์ใด

## ภาพรวม flow

1. Supervisor รับ LINE image event และทำ upload เข้า S3 สำเร็จ
2. Supervisor เขียน metadata/manifests และ indexes ที่จำเป็นลง S3 สำเร็จ
3. Supervisor สร้าง `ocr_job_id`
4. Supervisor เขียน `manifests/ocr-jobs/{ocr_job_id}.json`
5. OCR Worker ดึง job จาก queue หรือ list ของ pending jobs
6. OCR Worker ดาวน์โหลดไฟล์จาก S3 ตาม `bucket/key`
7. OCR Worker ส่งรูปเข้า Hermes + LLM vision flow ตาม prompt และ policy ที่กำหนด
8. OCR Worker parse และ normalize output ให้อยู่ใน structured draft
9. OCR Worker เขียน draft revision ลง S3
10. OCR Worker อัปเดต `ocr job manifest` และ `source message manifest`

## ที่อยู่ของ OCR job manifest

```text
manifests/ocr-jobs/{ocr_job_id}.json
```

กติกา:

- schema ของ `ocr job manifest` ควรสอดคล้องกับ [005_s3_manifest_schema.md](005_s3_manifest_schema.md) ในเรื่อง `schema_version`, `entity_type`, `entity_id`, และ key naming
- ถ้ามีความขัดกันระหว่างเอกสารนี้กับ schema รวม ให้ยึด field conventions ใน [005_s3_manifest_schema.md](005_s3_manifest_schema.md) เป็นหลัก แล้วใช้เอกสารนี้อธิบาย behavior ของ OCR worker เพิ่มเติม

## OCR job manifest

### ตัวอย่าง payload

```json
{
  "schema_version": "2026-06-09",
  "entity_type": "ocr_job",
  "entity_id": "ocr_20260608_0001",
  "ocr_job_id": "ocr_20260608_0001",
  "source_message_id": "src_20260608_0001",
  "workflow_session_id": "line_group_C123",
  "state": "queued",
  "queue_name": "ocr-jobs",
  "attempt_count": 0,
  "max_attempts": 5,
  "requested_by": "hermes-supervisor",
  "created_at": "2026-06-08T06:30:06Z",
  "updated_at": "2026-06-08T06:30:06Z",
  "input": {
    "bucket": "election-system",
    "key": "inbound/src_20260608_0001/original.bin",
    "metadata_key": "inbound/src_20260608_0001/metadata.json",
    "content_type": "image/jpeg",
    "size_bytes": 2481931
  },
  "line_context": {
    "platform": "line",
    "line_event_id": "01JX...",
    "line_message_id": "548899112233",
    "sender_user_id": "Uxxxxxxxx",
    "sender_group_id": "Cxxxxxxxx",
    "sender_room_id": null
  },
  "ocr_options": {
    "language_hint": "th",
    "expected_document_type": "election_score_sheet",
    "prompt_version": "ocr-v1",
    "model_name": "gemma4:26b"
  },
  "result": null,
  "error": null
}
```

### Field requirements

ต้องมีอย่างน้อย:

- `schema_version`
- `entity_type`
- `entity_id`
- `ocr_job_id`
- `source_message_id`
- `workflow_session_id`
- `state`
- `queue_name`
- `attempt_count`
- `max_attempts`
- `input.bucket`
- `input.key`
- `line_context.line_event_id`

## สถานะของ OCR job

ค่าที่รองรับ:

- `queued`
- `processing`
- `completed`
- `failed`

กติกา:

- Supervisor เป็นคนสร้าง job ในสถานะ `queued`
- OCR Worker เปลี่ยนเป็น `processing` เมื่อเริ่มทำงาน
- OCR Worker เปลี่ยนเป็น `completed` เมื่อเขียน draft สำเร็จ
- OCR Worker เปลี่ยนเป็น `failed` เมื่อทำไม่สำเร็จและบันทึก `error`

## สิ่งที่ OCR Worker ต้องทำเมื่อเริ่มงาน

1. ตรวจว่า job ยังอยู่ในสถานะ `queued` หรือ `processing` ที่ worker นี้รับต่อได้
2. อ่าน `input.bucket` และ `input.key`
3. ดาวน์โหลดไฟล์จาก S3
4. เรียก Hermes + LLM ให้สกัดข้อมูลจากรูปตาม OCR contract นี้
5. สร้าง structured draft

## Structured draft output

ที่อยู่ของ draft revision:

```text
drafts/{source_message_id}/revision-{revision}.json
drafts/{source_message_id}/latest.json
```

กติกา:

- `revision-{revision}.json` เป็น immutable artifact ของแต่ละรอบ OCR
- `latest.json` เป็น pointer ไปยัง draft revision ล่าสุด และควรถูกอัปเดตทุกครั้งที่ worker เขียน draft ใหม่สำเร็จ
- schema ของ draft ควรสอดคล้องกับ [005_s3_manifest_schema.md](005_s3_manifest_schema.md)

### ตัวอย่าง draft

```json
{
  "schema_version": "2026-06-09",
  "entity_type": "draft",
  "entity_id": "draft_20260608_0001_r1",
  "draft_id": "draft_20260608_0001_r1",
  "source_message_id": "src_20260608_0001",
  "ocr_job_id": "ocr_20260608_0001",
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
  "model_name": "gemma4:26b",
  "prompt_version": "ocr-v1",
  "created_by": "ocr-worker",
  "created_at": "2026-06-08T06:31:10Z",
  "updated_at": "2026-06-08T06:31:10Z"
}
```

### Field requirements ของ draft

- `draft_id`
- `source_message_id`
- `ocr_job_id`
- `revision`
- `status`
- `raw_model_output`
- `overall_confidence`
- `validation_flags`
- `candidate_scores`

ถ้ามีข้อมูลเขตได้ ควรมี:

- `election_id`
- `area_id`
- `polling_unit_id`

## กติกาการตัดสินผล OCR

### ถือว่าสำเร็จ

ถ้า:

- อ่านรูปได้
- สร้าง structured draft ได้
- ไม่มี validation error ระดับบังคับหยุด

ผลลัพธ์:

- เขียน draft revision
- อัปเดต `drafts/{source_message_id}/latest.json`
- อัปเดต `ocr_job.state = completed`
- อัปเดต `source_message.state = awaiting_approval` พร้อม pointer ใน `current.draft_id` และ `current.draft_key`

### ถือว่าล้มเหลว

ถ้า:

- โหลดไฟล์ไม่ได้
- OCR runtime พัง
- parse output ไม่ได้
- confidence ต่ำเกินเกณฑ์และระบบตัดสินว่าใช้ต่อไม่ได้

ผลลัพธ์:

- อัปเดต `ocr_job.state = failed`
- เขียน `error.code` และ `error.message`
- อัปเดต `source_message.state = exception`
- เขียน exception object และ audit event

## Validation flags ที่แนะนำ

ใช้เป็น array ของ string เช่น:

- `missing_area_id`
- `missing_candidate_scores`
- `low_confidence`
- `malformed_score_table`
- `duplicate_business_result`
- `requires_human_review`

## OCR job result block

หลัง OCR สำเร็จ ควรอัปเดต block `result` ใน `ocr job manifest`

ตัวอย่าง

```json
"result": {
  "draft_id": "draft_20260608_0001_r1",
  "draft_key": "drafts/src_20260608_0001/revision-1.json",
  "draft_latest_key": "drafts/src_20260608_0001/latest.json",
  "overall_confidence": 0.91,
  "validation_flags": []
}
```

กรณีล้มเหลว ควรอัปเดต block `error`

```json
"error": {
  "code": "LOW_CONFIDENCE",
  "message": "OCR confidence below acceptance threshold"
}
```

## Idempotency rules

- `ocr_job_id` เป็น idempotency key หลักของงาน OCR
- ถ้า worker เริ่มทำงานซ้ำกับ `ocr_job_id` เดิม ต้องไม่สร้าง draft revision ใหม่โดยไม่จำเป็น
- ถ้า draft revision ถูกเขียนไปแล้วสำหรับ job เดิม ควร reuse draft เดิมหรือคืนสถานะเดิมแทน

## Retry guidance

- retry ได้เมื่อโหลดไฟล์จาก S3 ไม่สำเร็จชั่วคราว
- retry ได้เมื่อ OCR runtime พังชั่วคราว
- ไม่ควร retry แบบอัตโนมัติถ้าไฟล์อ่านไม่ออกเชิงคุณภาพ เช่น `low_confidence` หรือ `malformed_score_table`

## Logging requirements

OCR Worker ควร log อย่างน้อย:

- `ocr_job_id`
- `source_message_id`
- `workflow_session_id`
- `input.bucket`
- `input.key`
- `draft_id` หรือ `error.code`

## Related contracts

- [005_s3_manifest_schema.md](005_s3_manifest_schema.md)
- [006_approval_contract.md](006_approval_contract.md)
