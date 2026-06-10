# Approval Contract

เอกสารนี้นิยามสัญญาระหว่าง Supervisor, LINE approval interaction, และ Update Worker สำหรับการขออนุมัติ draft, การรับคำตอบจากผู้ใช้, และการสร้าง update job หลัง approval

## เป้าหมาย

ทำให้ทุกฝั่งเข้าใจตรงกันว่า:

- Supervisor จะเขียน approval manifest แบบใด
- LINE interaction จะส่งคำตอบกลับมาในรูปแบบไหน
- ระบบจะ normalize ปุ่มและ text fallback ให้เป็น action กลางแบบใด
- ระบบจะถือว่า approval สำเร็จ, ถูกปฏิเสธ, หรือหมดอายุจากเกณฑ์ใด
- เมื่อ approval สำเร็จ ต้องอัปเดต S3 manifests และสร้าง update job อย่างไร

## ความสัมพันธ์กับ schema หลัก

- schema ของ approval manifest ต้องสอดคล้องกับ [005_s3_manifest_schema.md](d:/ch7/election/storage/schema/005_s3_manifest_schema.md)
- draft ที่ถูกขออนุมัติต้องอ้างอิง draft revision ล่าสุดจาก `drafts/{source_message_id}/latest.json`
- เมื่อ approval สำเร็จ ระบบต้องอัปเดต pointer ใน `manifests/source-messages/{source_message_id}.json`
- ถ้ามีความขัดกันระหว่างเอกสารนี้กับ schema รวม ให้ยึด field conventions ใน [005_s3_manifest_schema.md](d:/ch7/election/storage/schema/005_s3_manifest_schema.md) เป็นหลัก

## ภาพรวม flow

1. OCR Worker เขียน draft revision และ `drafts/{source_message_id}/latest.json`
2. Supervisor อ่าน draft ล่าสุดและสร้าง approval manifest
3. Supervisor ส่งข้อความขออนุมัติกลับไปยัง LINE
4. ผู้ใช้ตอบกลับด้วยปุ่มหรือข้อความ
5. Supervisor normalize คำตอบให้เป็น action กลาง
6. Supervisor ตรวจว่าคำตอบยังผูกกับ draft revision ล่าสุดและ approval ที่ยังไม่หมดอายุ
7. ถ้า approved Supervisor อัปเดต approval manifest, source message manifest, audit event, และสร้าง update job
8. ถ้า rejected หรือ expired Supervisor อัปเดต state และ audit event โดยไม่สร้าง update job

## Approval manifest

ที่อยู่ของ approval manifest:

```text
approvals/{source_message_id}/revision-{revision}.json
approvals/{source_message_id}/latest.json
```

กติกา:

- `revision-{revision}.json` เป็น immutable artifact สำหรับ approval ของ draft revision นั้น
- `latest.json` เป็น pointer ไปยัง approval revision ล่าสุดที่ยังเกี่ยวข้องกับ source message นั้น
- approval 1 รายการต้องผูกกับ draft revision เดียวเท่านั้น

### ตัวอย่าง approval manifest

```json
{
  "schema_version": "2026-06-09",
  "entity_type": "approval",
  "entity_id": "approval_20260609_0001_r1",
  "approval_id": "approval_20260609_0001_r1",
  "source_message_id": "src_20260609_0001",
  "draft_id": "draft_20260609_0001_r1",
  "draft_revision": 1,
  "workflow_session_id": "line_group_C123",
  "state": "awaiting_approval",
  "requested_from_user_id": "Uxxxxxxxx",
  "requested_via": "line_button",
  "requested_at": "2026-06-09T06:31:15Z",
  "expires_at": "2026-06-09T08:31:15Z",
  "responded_at": null,
  "response_type": null,
  "response_source_message_id": null,
  "response_text": null,
  "response_payload": null,
  "approved_by_user_id": null,
  "rejected_by_user_id": null,
  "approval_note": null,
  "created_at": "2026-06-09T06:31:15Z",
  "updated_at": "2026-06-09T06:31:15Z"
}
```

### ตัวอย่าง `latest.json`

```json
{
  "schema_version": "2026-06-09",
  "entity_type": "approval_pointer",
  "entity_id": "src_20260609_0001",
  "source_message_id": "src_20260609_0001",
  "approval_id": "approval_20260609_0001_r1",
  "approval_key": "approvals/src_20260609_0001/revision-1.json",
  "draft_id": "draft_20260609_0001_r1",
  "draft_revision": 1,
  "state": "awaiting_approval",
  "updated_at": "2026-06-09T06:31:15Z"
}
```

### Field requirements

ต้องมีอย่างน้อย:

- `schema_version`
- `entity_type`
- `entity_id`
- `approval_id`
- `source_message_id`
- `draft_id`
- `draft_revision`
- `workflow_session_id`
- `state`
- `requested_from_user_id`
- `requested_at`
- `expires_at`
- `created_at`
- `updated_at`

## สถานะของ approval

ค่าที่รองรับ:

- `awaiting_approval`
- `approved`
- `rejected`
- `expired`

กติกา:

- Supervisor เป็นคนสร้าง approval ในสถานะ `awaiting_approval`
- approval จะกลายเป็น `approved` ได้เมื่อ action ที่ normalize แล้วเป็น `approve` และผ่าน validation ทั้งหมด
- approval จะกลายเป็น `rejected` ได้เมื่อ action ที่ normalize แล้วเป็น `correct` หรือ `reject`
- approval จะกลายเป็น `expired` ได้เมื่อเกิน `expires_at` หรือมี draft revision ใหม่กว่าเกิดขึ้น

## LINE approval interaction

ระบบต้องรองรับ 2 ช่องทางพร้อมกัน:

1. ปุ่มใน LINE
2. text fallback

### รูปแบบ action กลาง

ค่าที่ normalize แล้วควรใช้ชุดเดียวกัน:

- `approve`
- `correct`
- `reject`
- `unknown`

### ปุ่มใน LINE

ตัวอย่างค่าที่ควร map:

- ปุ่ม `ยืนยัน` -> `approve`
- ปุ่ม `แก้ไข` -> `correct`

คำแนะนำ:

- postback payload ควรมีอย่างน้อย `approval_id`, `draft_id`, `draft_revision`, และ `action`
- อย่าพึ่งเฉพาะข้อความที่แสดงบนปุ่ม เพราะ label เปลี่ยนได้ แต่ payload ต้องคงที่

ตัวอย่าง postback payload:

```json
{
  "approval_id": "approval_20260609_0001_r1",
  "draft_id": "draft_20260609_0001_r1",
  "draft_revision": 1,
  "action": "approve"
}
```

### Text fallback

ตัวอย่างข้อความที่ควร map:

- `ยืนยัน` -> `approve`
- `แก้ไข` -> `correct`
- `ไม่ถูกต้อง` -> `reject`

กติกา:

- ให้ trim whitespace และ normalize ตัวอักษรก่อนเทียบคำสั่ง
- ถ้าคำตอบไม่ตรง command ที่รู้จัก ให้เป็น `unknown`
- ถ้าเป็น `unknown` อย่าเปลี่ยน approval state ทันที ให้ตอบกลับเพื่อขอคำสั่งใหม่

## Validation rules

ก่อนเปลี่ยน approval state ต้องตรวจอย่างน้อย:

1. approval manifest ยังอยู่ในสถานะ `awaiting_approval`
2. เวลาปัจจุบันยังไม่เกิน `expires_at`
3. ผู้ตอบเป็น user ที่มีสิทธิ์อย่างน้อยเท่ากับ `requested_from_user_id` หรือตาม allowlist ที่ระบบกำหนด
4. `draft_id` และ `draft_revision` ในคำตอบตรงกับ draft ล่าสุดของ source message
5. ไม่มี approval ใหม่กว่าที่ supersede approval นี้แล้ว

ถ้า validation ไม่ผ่าน:

- ห้ามสร้าง update job
- ต้องเขียน audit event ที่อธิบายเหตุผล
- ควรตอบกลับ LINE ด้วยข้อความสั้นที่บอกว่าต้องยืนยัน draft ล่าสุดหรือให้ส่งคำสั่งใหม่

## ผลลัพธ์เมื่อ approved

เมื่อ action เป็น `approve` และ validation ผ่าน:

1. อัปเดต approval manifest ให้มี:
   - `state = approved`
   - `responded_at`
   - `response_type`
   - `response_source_message_id`
   - `response_text` หรือ `response_payload`
   - `approved_by_user_id`
2. อัปเดต `approvals/{source_message_id}/latest.json`
3. อัปเดต `manifests/source-messages/{source_message_id}.json` ให้มี:
   - `state = approved`
   - `current.approval_id`
   - `current.approval_key`
4. เขียน audit event ประเภท `approved`
5. สร้าง `updates/jobs/{update_job_id}.json`
6. ส่ง update job เข้า `update-jobs`

## ผลลัพธ์เมื่อ rejected หรือ correct

เมื่อ action เป็น `correct` หรือ `reject`:

1. อัปเดต approval manifest ให้มี:
   - `state = rejected`
   - `responded_at`
   - `response_type`
   - `response_source_message_id`
   - `response_text` หรือ `response_payload`
   - `rejected_by_user_id`
2. อัปเดต `approvals/{source_message_id}/latest.json`
3. อัปเดต `manifests/source-messages/{source_message_id}.json` ให้มี:
   - `state = rejected`
   - `current.approval_id`
   - `current.approval_key`
4. เขียน audit event ประเภท `rejected`
5. ห้ามสร้าง update job

หมายเหตุ:

- ถ้าธุรกิจภายหลังอยากแยก `correct` กับ `reject` ออกจากกันจริง ค่อยขยาย source message state หรือเพิ่ม correction entity ภายหลัง

## ผลลัพธ์เมื่อ expired

approval ควรถูก mark เป็น `expired` เมื่อ:

- เกิน `expires_at`
- มี draft revision ใหม่กว่าและ approval เดิมไม่ควรใช้ต่อ

ผลลัพธ์:

- อัปเดต approval manifest และ `latest.json`
- เขียน audit event ประเภท `approval_expired`
- ห้ามสร้าง update job

## Response normalization block

เพื่อให้ง่ายต่อการ trace ควรเก็บข้อมูลคำตอบที่ normalize แล้วใน approval manifest

ตัวอย่างเมื่อ approved:

```json
{
  "state": "approved",
  "responded_at": "2026-06-09T06:32:10Z",
  "response_type": "line_postback",
  "response_source_message_id": "msg_20260609_0002",
  "response_text": null,
  "response_payload": {
    "action": "approve",
    "draft_revision": 1
  },
  "approved_by_user_id": "Uxxxxxxxx"
}
```

ตัวอย่างเมื่อ rejected จากข้อความ:

```json
{
  "state": "rejected",
  "responded_at": "2026-06-09T06:32:20Z",
  "response_type": "line_text",
  "response_source_message_id": "msg_20260609_0003",
  "response_text": "แก้ไข",
  "response_payload": {
    "action": "correct"
  },
  "rejected_by_user_id": "Uxxxxxxxx"
}
```

## Idempotency rules

- `approval_id` เป็น idempotency key หลักของ approval record
- การตอบ approval เดิมซ้ำด้วย payload เดิม ควรคืนผลเดิมและไม่สร้าง update job ซ้ำ
- ถ้า approval อยู่ในสถานะ terminal แล้ว (`approved`, `rejected`, `expired`) ห้ามเปลี่ยนกลับ
- update job ที่สร้างจาก approval สำเร็จต้องมี idempotency key ที่ deterministic จาก approved draft

## Logging requirements

Supervisor ควร log อย่างน้อย:

- `approval_id`
- `source_message_id`
- `draft_id`
- `draft_revision`
- `workflow_session_id`
- `requested_from_user_id`
- `response_type`
- `normalized_action`
- `approved_by_user_id` หรือ `rejected_by_user_id`

## งานถัดจาก spec นี้

1. นิยาม update job contract ให้ละเอียดกว่าระดับ manifest schema
2. นิยาม LINE approval message template และ postback payload ให้ใช้จริงได้
3. เริ่ม implement ฝั่ง Supervisor สำหรับ approval state transition และ update job creation