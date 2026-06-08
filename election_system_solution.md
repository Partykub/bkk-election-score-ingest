# Election Score Update System Solution

## Objective

ออกแบบระบบรับรูปคะแนนเลือกตั้งผ่าน `Line`, ใช้ `Hermes + LLM` อ่านข้อมูลจากรูป, ขอการยืนยันจากผู้ส่งในแชท, และเมื่อยืนยันแล้วจึงยิง API ไปยัง `AWS` เพื่ออัปเดตข้อมูลคะแนน

ระบบต้องรองรับกรณี:

- มีข้อความหรือรูปเข้ามาพร้อมกันสูงสุดประมาณ `55` เขต
- มีการส่งรูปซ้ำ
- มีการอ่าน OCR ผิดหรือข้อมูลไม่ครบ
- ต้องมี audit trail ว่าใครส่ง, ระบบอ่านว่าอะไร, ใครเป็นคนยืนยัน, และยิง update เมื่อไร

## Recommended Roles

### 1. Hermes Supervisor

หน้าที่:

- รับ webhook จาก `Line`
- ตรวจ signature และสิทธิ์ของผู้ส่ง
- สร้าง workflow state
- ตรวจ duplicate ระดับ event และ message
- เก็บรูปลง object storage
- enqueue งาน OCR
- ส่งข้อความสรุปผล OCR กลับไปให้ผู้ส่งยืนยัน
- รับคำสั่ง `ยืนยัน` / `แก้ไข`
- สร้างงาน update หลัง approval

ใช้ `Hermes`: ใช่

### 2. Hermes OCR Worker

หน้าที่:

- ดึงรูปจาก object storage
- ใช้ `Hermes + LLM` อ่านรูป
- extract ข้อมูลแบบ structured เช่น เขต, เบอร์ผู้สมัคร, คะแนน
- ส่งผลกลับเข้าระบบเป็น OCR result

ใช้ `Hermes`: ใช่

### 3. Update Worker

หน้าที่:

- รับข้อมูลที่ approved แล้ว
- แปลงข้อมูลเป็น payload สำหรับ API ปลายทาง
- ยิง API ไปยัง `AWS`
- retry เมื่อ API fail
- บันทึกผลสำเร็จหรือล้มเหลว

ใช้ `Hermes`: ไม่ใช้

หมายเหตุ:

- งาน update เป็นงาน deterministic ไม่ควรใช้ LLM
- ใช้ service ปกติ เช่น `Go`, `Node.js`, หรือ `Python` worker ได้

## Role vs Instance

`Role` คือหน้าที่ของ service  
`Instance` คือจำนวนตัวที่รันจริง

ตัวอย่าง:

- `Hermes Supervisor` = 1 role
- `Hermes OCR Worker` = 1 role
- `Update Worker` = 1 role

รวมทั้งระบบ = `3 roles`

แต่ตอน deploy จริงอาจรันเป็น:

- `Hermes Supervisor` = `1-2 instances`
- `Hermes OCR Worker` = `1-4 instances`
- `Update Worker` = `1-2 instances`

ดังนั้นจำนวน instance มากกว่า role ได้เป็นเรื่องปกติ

## Minimum Deployment Recommendation

เริ่มต้นแบบง่าย:

- `Hermes Supervisor` = `1`
- `Hermes OCR Worker` = `1-2`
- `Update Worker` = `1`

รวม = `3-4 instances`

ถ้ารับโหลดมากขึ้น:

- `Hermes Supervisor` = `2`
- `Hermes OCR Worker` = `2-4`
- `Update Worker` = `1-2`

รวม = `5-8 instances`

หมายเหตุ:

- `instance` ไม่จำเป็นต้องเท่ากับ `EC2 1 เครื่อง`
- 2 instances อาจเป็น 2 containers บนเครื่องเดียวกันก็ได้

## Required Infrastructure

- `Line Webhook`
- `Hermes Supervisor`
- `Queue`
- `Object Storage`
- `Hermes OCR Worker`
- `Database`
- `Update Worker`
- `AWS Target API`

แนะนำ:

- `Database`: `PostgreSQL`
- `Queue`: `SQS`, `RabbitMQ`, หรือ `Redis` queue
- `Object Storage`: `S3` หรือเทียบเท่า

## End-to-End Flow

### Normal Flow

1. Reporter ส่งรูปคะแนนเข้ามาทาง `Line`
2. `Line` ส่ง webhook event ไปที่ `Hermes Supervisor`
3. `Hermes Supervisor` ตรวจ signature และสิทธิ์ผู้ส่ง
4. `Hermes Supervisor` สร้าง `source_message`
5. `Hermes Supervisor` สร้าง dedupe keys เช่น `event_id`, `message_id`, `file_hash`
6. `Hermes Supervisor` เก็บรูปลง object storage
7. `Hermes Supervisor` enqueue OCR job
8. `Hermes Supervisor` ตอบกลับในแชทว่าได้รับข้อมูลแล้ว
9. `Hermes OCR Worker` ดึงงานจาก queue
10. `Hermes OCR Worker` ใช้ `Hermes + LLM` อ่านรูป
11. ระบบได้ structured result เช่น `area_id`, `candidate_number`, `score`, `confidence`
12. ระบบตรวจ validation เบื้องต้น
13. ระบบสร้าง `draft`
14. `Hermes Supervisor` ส่งข้อความสรุปกลับไปยังผู้ส่งเพื่อขอการยืนยัน
15. ผู้ส่งพิมพ์ `ยืนยัน` หรือกดปุ่ม approve
16. `Hermes Supervisor` ตรวจว่า approval นี้ผูกกับ draft revision ล่าสุดจริง
17. ถ้าถูกต้อง ระบบเปลี่ยนสถานะเป็น `approved`
18. `Hermes Supervisor` enqueue update job
19. `Update Worker` ยิง API ไปยัง `AWS`
20. ถ้า API success ระบบบันทึกสถานะ `updated`
21. `Hermes Supervisor` ตอบกลับว่าอัปเดตเรียบร้อย

## Example OCR Confirmation Message

```text
ระบบอ่านข้อมูลจากรูปได้ดังนี้

เขต 12
เบอร์ 1: 12,345
เบอร์ 2: 11,980
เบอร์ 3: 8,750

หากถูกต้อง พิมพ์ "ยืนยัน"
หากไม่ถูกต้อง พิมพ์ "แก้ไข"
```

## Flow for 55 Messages Arriving Together

กรณีมี `55` ข้อความเข้ามาพร้อมกัน ห้ามให้ `Hermes Supervisor` ทำ OCR เองทันทีทุกข้อความ

`Hermes Supervisor` ต้องทำเฉพาะ:

- รับ event
- verify
- persist state
- เก็บไฟล์
- enqueue งาน
- ตอบกลับเร็ว

### Burst Handling Flow

1. `Line` ส่งเข้ามา `55` events ในเวลาใกล้กัน
2. `Hermes Supervisor` รับและบันทึกทุก event ให้เร็วที่สุด
3. แต่ละ event ถูกสร้างเป็น `source_message`
4. แต่ละ event ถูก hash เพื่อทำ dedupe
5. รูปทั้งหมดถูกเก็บลง storage
6. สร้าง `55 OCR jobs`
7. OCR jobs ทั้งหมดถูกส่งเข้า queue
8. `Hermes OCR Worker` ดึงงานไปทำตาม concurrency limit เช่น `5` หรือ `10`
9. งานที่ OCR เสร็จก่อนจะได้ `draft` ก่อน
10. `Hermes Supervisor` ส่งผล OCR ไปให้แต่ละผู้ส่งยืนยันเป็นรายข้อความ
11. รายการที่ได้รับการยืนยันแล้วจะถูกส่งเข้า update queue
12. `Update Worker` ยิง API ไป `AWS` ตามลำดับ
13. รายการที่ OCR ผิดหรือข้อมูลไม่ครบจะถูกแยกเป็น `exception`

### Important Burst Principle

- scale ที่ `OCR Worker` ไม่ใช่ที่ `Supervisor`
- `Supervisor` ต้อง lightweight
- ทุกงานหนักต้องถูก queue
- ระบบต้องยอมให้บางรายการสำเร็จก่อนบางรายการได้

## Duplicate Handling

ระบบต้องกัน duplicate อย่างน้อย 3 ระดับ

### 1. Event Deduplication

ใช้ค่าเช่น:

- `line_event_id`
- `message_id`

กรณี `Line` retry webhook เดิม ระบบต้องไม่สร้างงานซ้ำ

### 2. File Deduplication

ใช้:

- `file_hash` เช่น `sha256`

ถ้าผู้ใช้ส่งรูปเดิมซ้ำในช่วงเวลาใกล้กัน ระบบควร mark ว่าเป็น duplicate candidate

### 3. Business Deduplication

แม้จะเป็นคนละรูป แต่ถ้าผลลัพธ์คะแนนเหมือนกับข้อมูลล่าสุดของเขตนั้น ระบบควรตรวจจับได้ว่าเป็นข้อมูลซ้ำ

ตัวอย่าง key:

- `election_id`
- `area_id`
- `candidate_scores_signature`

## Duplicate Decision Rules

- ถ้า `event_id` ซ้ำ: ignore ทันที
- ถ้า `file_hash` ซ้ำและยังอยู่ใน session เดิม: ไม่ต้อง OCR ซ้ำ
- ถ้า OCR result ตรงกับข้อมูลล่าสุดในระบบ: ไม่ต้องยิง update ซ้ำ
- ถ้าเป็นรูปใหม่แต่คะแนนเปลี่ยน: เข้ากระบวนการ approval ตามปกติ

## State Model

แนะนำให้แยก state ตามชนิด entity ไม่ควรใช้ state ชุดเดียวครอบทุกอย่าง

### Source Message State

- `received`
- `stored`
- `queued`
- `ocr_processing`
- `draft`
- `awaiting_approval`
- `approved`
- `rejected`
- `exception`
- `updating`
- `updated`

### OCR Job State

- `queued`
- `processing`
- `completed`
- `failed`

### Approval State

- `draft`
- `awaiting_approval`
- `approved`
- `rejected`
- `expired`

## Approval Rules

- ห้ามยิง API ไป `AWS` ก่อน approval
- approval ต้องผูกกับ `revision`
- ถ้ามี draft ใหม่กว่าเกิดขึ้น prompt เก่าต้องหมดอายุ
- คนที่กดยืนยันต้องเป็น user ที่มีสิทธิ์
- ต้องเก็บว่าใครกดยืนยันและกดเมื่อไร

## Failure Handling

### OCR Failure

กรณี:

- รูปไม่ชัด
- confidence ต่ำ
- อ่านคะแนนไม่ครบ

แนวทาง:

- mark เป็น `exception`
- แจ้งผู้ส่งให้ส่งรูปใหม่หรือแก้ไข
- ยังไม่สร้าง update job

### Update API Failure

กรณี:

- ปลายทาง `AWS` ล่ม
- timeout
- ได้ `5xx`

แนวทาง:

- retry ด้วย idempotency key
- เก็บ error log
- ถ้าเกิน retry limit ให้เข้า `exception`

## AWS Update Rules

`Update Worker` ควรส่ง:

- `idempotency_key`
- `source_message_id`
- `approved_revision_id`
- `updated_by`
- `updated_at`

จุดประสงค์:

- กัน update ซ้ำ
- audit ได้
- replay ได้

## Recommended First Version

ถ้าจะเริ่มแบบไม่ซับซ้อน:

1. `Hermes Supervisor` 1 instance
2. `Hermes OCR Worker` 1 instance
3. `Update Worker` 1 instance
4. `PostgreSQL` 1 ตัว
5. `Queue` 1 ตัว
6. `Object Storage` 1 ตัว

พอเริ่มใช้งานจริงแล้วค่อย scale `Hermes OCR Worker` ก่อนเป็นลำดับแรก

## Key Design Principles

- งาน orchestration ให้อยู่ที่ `Hermes Supervisor`
- งาน OCR ให้แยกไป `Hermes OCR Worker`
- งาน update ไป `AWS` อย่าใช้ LLM
- ใช้ queue เพื่อรองรับ burst
- approval ต้องเกิดก่อน update เสมอ
- duplicate ต้องกันหลายชั้น
- audit log ต้องมีตั้งแต่วันแรก

## Final Summary

ระบบที่เหมาะกับ use case นี้ควรมี `3 roles` หลัก:

1. `Hermes Supervisor`
2. `Hermes OCR Worker`
3. `Update Worker`

ถ้านับเฉพาะส่วนที่เป็น `Hermes` จะมี `2 Hermes roles`:

1. `Hermes Supervisor`
2. `Hermes OCR Worker`

เริ่มต้น deploy แบบง่ายได้ที่ `3-4 instances` และถ้า workload สูงขึ้นให้ scale ฝั่ง `OCR Worker` ก่อนเป็นอันดับแรก
