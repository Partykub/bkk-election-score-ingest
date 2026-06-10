# Election System Architecture Baseline

เอกสารนี้สรุป baseline architecture ที่ตกลงร่วมกันสำหรับ implementation ระยะถัดไปของระบบรับรูปคะแนนเลือกตั้ง

## เป้าหมาย

- รับรูปคะแนนจาก LINE OA
- ให้ OCR Worker ใช้ Hermes + LLM vision อ่านรูปและสร้าง structured draft
- ส่ง draft กลับไปให้ผู้ส่งยืนยันผ่าน LINE
- หลัง approval แล้วจึงสร้างงานสำหรับอัปเดต AWS target API
- เก็บข้อมูลและ audit trail ทั้งหมดไว้บน S3 เป็นหลัก

## Baseline decisions

- `S3` เป็น source of truth ของ workflow state และ artifacts ทั้งหมด
- `SQS` เป็น queue ตัวแรกทั้งสำหรับ dev และ production
- `Hermes` ใช้เป็น vision extraction runtime สำหรับ OCR Worker
- `n8n` ไม่อยู่ใน critical path ของระบบหลัก
- `paddle_ocr/` เป็น sandbox เท่านั้น ไม่ใช่ production path
- approval ใน LINE รองรับทั้งปุ่มและ text fallback

## Core components

### 1. LINE Webhook

หน้าที่:

- รับรูปและข้อความจากผู้รายงาน
- ส่ง webhook event มายัง Supervisor

### 2. Supervisor

หน้าที่:

- รับ LINE webhook
- ตรวจ signature และสิทธิ์ของผู้ส่ง
- ทำ event/message deduplication
- ดาวน์โหลดรูปจาก LINE
- อัปโหลดรูปและ manifests ไปยัง S3
- สร้าง OCR job manifest
- ส่ง OCR job เข้า SQS
- ส่งข้อความตอบกลับและ approval prompt ไปยัง LINE
- รับ approval หรือ correction จากผู้ใช้
- สร้าง update job หลัง approval

ข้อกำหนด:

- Supervisor ต้อง lightweight
- ห้ามทำ OCR inline ตอนรับ inbound burst
- ห้ามเรียก AWS target API โดยตรงก่อน approval

### 3. OCR Worker

หน้าที่:

- ดึง OCR jobs จาก SQS
- โหลดรูปจาก S3
- รันเป็น dedicated queue consumer ที่เรียก Hermes ตาม `OCR_WORKER_HERMES_BASE_URL`
- parse และ normalize model output เป็น structured draft
- เขียน draft และ job result กลับลง S3

ข้อแนะนำการ deploy:

- ให้แยก `hermes-supervisor` ออกจาก `ocr-worker` ชัดเจน โดย `ocr-worker` เป็น service สำหรับ consume queue โดยตรง
- `ocr-worker` ควร build จาก code ใน repo และ mount AWS credentials/profile ของ environment นั้น
- Hermes runtime สำหรับงาน OCR ควรถูกเรียกผ่าน network endpoint แทนการทำให้ worker เป็น gateway runtime เอง

### 4. Hermes

หน้าที่:

- `hermes-supervisor` ทำ orchestration และ approval-facing interactions
- `ocr-worker` ทำ vision extraction และ OCR-specific reasoning ผ่าน Hermes endpoint ที่ worker เรียกใช้อีกทอดหนึ่ง
- ทั้งสองตัวอาจใช้ model backend ร่วมกันได้ เช่น Ollama ตัวเดียวกัน แต่ไม่จำเป็นต้องมี Hermes runtime สองชุดใน local deployment

หมายเหตุ:

- downstream services ต้องใช้ normalized draft ไม่ใช่ parse raw free text ซ้ำ

### 5. Update Worker

หน้าที่:

- ดึง update jobs จาก SQS
- map approved draft ไปเป็น payload สำหรับ AWS target API
- ยิง API แบบ deterministic พร้อม retry และ idempotency
- เขียนผลลัพธ์และ audit events กลับลง S3

สถานะปัจจุบัน:

- repo มี package `hermes.update_worker` สำหรับ config, queue envelope parsing, และ CLI scaffold แล้ว
- การยิง downstream API จริงยังเป็นงานของ Phase 5

### 6. S3

ใช้เก็บ:

- รูปต้นฉบับ
- source message manifests
- OCR job manifests
- draft revisions
- approval manifests
- update job manifests
- audit events

แนวทาง:

- ใช้ deterministic keys
- มอง manifest เป็น source of truth
- อนุญาตให้มี derived indexes ได้ถ้าสร้างใหม่จาก manifests ได้

### 7. SQS

queue หลัก:

- `ocr-jobs`
- `update-jobs`

แนวทาง:

- ใช้ AWS จริงตั้งแต่ dev ถ้าต้องการความเร็วในการส่งมอบ
- แยก environment ด้วย queue names หรือ prefixes เช่น `dev` และ `prod`
- ตั้ง visibility timeout และ dead-letter queue ให้เหมาะกับงานแต่ละชนิด

## End-to-end flow

1. ผู้ใช้ส่งรูปหรือข้อความเข้ามาทาง LINE
2. LINE ส่ง webhook event ไปยัง Supervisor
3. Supervisor ตรวจสิทธิ์และทำ dedupe ระดับ event กับ message
4. ถ้าเป็นรูป Supervisor ดาวน์โหลดไฟล์จาก LINE
5. Supervisor อัปโหลดไฟล์ต้นฉบับและ source message manifest ไปยัง S3
6. Supervisor สร้าง OCR job manifest และส่ง job เข้า `ocr-jobs`
7. OCR Worker ดึงงานจาก SQS และโหลดรูปจาก S3
8. OCR Worker เรียก Hermes เพื่ออ่านรูป
9. OCR Worker normalize ผลเป็น structured draft และเขียนกลับลง S3
10. Supervisor ส่งผลกลับไปยัง LINE เพื่อขอ approval
11. ผู้ใช้กดปุ่ม `ยืนยัน` หรือ `แก้ไข` หรือพิมพ์ข้อความแทน
12. Supervisor ตรวจว่าคำตอบผูกกับ draft revision ล่าสุด
13. ถ้า approved Supervisor เขียน approval manifest และส่ง update job เข้า `update-jobs`
14. Update Worker เรียก AWS target API
15. Update Worker เขียนผลลัพธ์และ audit trail กลับลง S3

## Structured output baseline

OCR Worker ควรเก็บทั้ง raw output และ normalized output แยกกัน

field ขั้นต่ำที่ควรมีใน normalized draft:

- `source_message_id`
- `draft_id`
- `revision`
- `status`
- `raw_model_output`
- `overall_confidence`
- `validation_flags`
- `candidate_scores`

field ที่ควรพยายามมีถ้าระบุได้:

- `election_id`
- `area_id`
- `polling_unit_id`
- `report_type`
- `observed_at`
- `result_signature`
- `notes`
- `image_quality_flags`
- `model_name`
- `prompt_version`

field ขั้นต่ำต่อผู้สมัคร:

- `candidate_number`
- `candidate_name`
- `score`
- `confidence`
- `raw_text`

## Deduplication policy

ใช้ dedupe 3 ระดับ:

1. Event dedupe ด้วย `line_event_id`
2. Message dedupe ด้วย `line_message_id`
3. Business dedupe ด้วย `result_signature`

กติกาเบื้องต้น:

- ถ้า `line_event_id` ซ้ำ ให้ ignore ทันที
- ถ้า `line_message_id` ซ้ำ ไม่สร้าง source message ใหม่
- ถ้า `result_signature` ตรงกับ approved result ล่าสุด ไม่ยิง AWS update ซ้ำ

## Approval UX baseline

- ใช้ปุ่ม `ยืนยัน` และ `แก้ไข` เป็น primary path
- รองรับ text fallback เช่น `ยืนยัน` และ `แก้ไข`
- approval ต้องผูกกับ draft revision ล่าสุดเท่านั้น

## Dev and production baseline

### Development

- รัน `hermes-supervisor` และ `ocr-worker` ใน local Docker โดยให้ `ocr-worker` consume queue โดยตรง
- ใช้ AWS จริงสำหรับ `S3` และ `SQS`
- ใช้ `AWS CLI profile` หรือ `aws configure sso` สำหรับ credentials ในเครื่อง dev

### Production

- deploy services ขึ้น AWS ภายหลังเมื่อ flow หลักนิ่งแล้ว
- ใช้ `IAM role` แทน static credentials
- แยก resources และ config ตาม environment ชัดเจน

## Out of critical path

- `n8n` ไม่ควรเป็น dependency ของ ingest, OCR, approval, หรือ update path
- ถ้าจะใช้ `n8n` ต่อ ให้ใช้ในงานรอง เช่น admin utility, manual repair flow, หรือ notification ที่ไม่กระทบ workflow หลัก