# แผนระบบเลือกตั้ง

## เป้าหมาย

สร้างระบบรับรูปคะแนนจาก LINE OA, แปลงข้อมูลในรูปให้อยู่ในรูปแบบ structured, ส่งกลับไปให้ผู้ส่งยืนยัน, และอัปเดต AWS API ปลายทางเมื่อได้รับการยืนยันแล้วเท่านั้น

## สถานะงานแบบติดตาม

### งานที่เสร็จแล้ว

- [x] Hermes Supervisor รันใน Docker ได้
- [x] วาง compose ให้ `ocr-worker` รันเป็น queue consumer deployment path จริง
- [x] Hermes เรียกใช้ Ollama ได้
- [x] เปิด LINE gateway ใน Hermes แล้ว
- [x] LINE OA ยิงเข้า Hermes supervisor ผ่าน ngrok ได้แล้ว
- [x] webhook intake ฝั่ง supervisor persist state, indexes, และ source manifests ได้จริง
- [x] direct LINE media persistence ลง S3 path ที่ใช้จริงได้แล้ว
- [x] OCR worker เรียก Hermes API และเขียน draft/approval artifacts กลับ S3 ได้แล้ว
- [x] approval flow `ยืนยัน` / `แก้ไข` ทำงานกับ draft ล่าสุดได้แล้ว
- [x] ร่าง OCR job payload และ worker contract spec แล้ว
- [x] วาง package scaffold สำหรับ `hermes.update_worker` แล้วเพื่อเตรียม Phase 5

### งานหลักที่ยังต้องทำ

- [x] ออกแบบ persistence layer และ S3 object schema
- [x] ทำ webhook workflow ของ supervisor ให้เก็บ state ได้จริง
- [x] ทำ direct S3 media persistence และ upload metadata handling ใน supervisor
- [ ] ทำ media deduplication ระดับไฟล์ถ้าจำเป็นใน production
- [x] ทำ OCR worker ให้เรียก Hermes + LLM สำหรับอ่านรูปและสร้าง structured draft
- [x] ทำ approval workflow ใน LINE
- [ ] ทำ update worker สำหรับ AWS API
- [ ] ทำ audit trail และ exception handling
- [ ] เปลี่ยนจาก `LINE_ALLOW_ALL_USERS=true` ไปเป็น allowlists ก่อน production

## สถานะปัจจุบัน

สิ่งที่ใช้งานได้แล้วในตอนนี้มีดังนี้:

- Hermes Supervisor รันใน Docker ได้
- Hermes เรียกใช้ Ollama ได้
- เปิด LINE gateway ใน Hermes แล้ว
- LINE OA ยิงเข้า Hermes supervisor ผ่าน ngrok ได้แล้ว
- local runtime ที่ใช้งานจริงคือ `hermes/supervisor/runtime-full/`

แปลว่าปัญหาหลักไม่ใช่เรื่อง network หรือ platform bootstrap แล้ว งานถัดไปคือ workflow ฝั่งแอปและการจัดการข้อมูล

## ขอบเขตงาน

### อยู่ในขอบเขต

- workflow ของ supervisor สำหรับ LINE events ขาเข้า
- การกันข้อมูลซ้ำ
- การเก็บ workflow state แบบ persistent
- การสร้าง OCR jobs และรับผล OCR กลับมา
- approval flow ในแชท
- การสร้าง update job หลัง approval
- การเชื่อมกับ AWS update worker
- audit trail ขั้นพื้นฐาน

### ยังไม่อยู่ในขอบเขตของเฟสแรก

- การ harden production deployment แบบเต็มรูปแบบ
- admin dashboard ขั้นสูง
- multi-region failover
- ช่องทางอื่นที่ไม่ใช่ LINE

## สถาปัตยกรรมเป้าหมาย

### 1. Hermes Supervisor

หน้าที่:

- รับ LINE webhook events
- ตรวจสิทธิ์ผู้ส่งและชนิดข้อความ
- เก็บ message และ workflow state
- กัน event ซ้ำและไฟล์ซ้ำ
- เก็บ metadata ของสื่อที่รับเข้ามาและ pointer ไปยัง storage
- สร้าง OCR jobs
- ส่งข้อความตอบรับและข้อความขออนุมัติกลับไปยัง LINE
- รับคำตอบแบบอนุมัติหรือแก้ไขจากผู้ใช้
- สร้าง update jobs หลังได้รับ approval

พฤติกรรมที่ต้องมีสำหรับรับข้อความจาก LINE และ route งาน:

- รับ event จาก LINE แล้วแยกก่อนว่าเป็นข้อความ, รูปภาพ, หรือคำสั่ง
- ถ้าเป็นรูปภาพใหม่: สร้าง source message, เก็บ state, เก็บไฟล์, และ route ไป OCR Worker ผ่าน OCR job
- ถ้าเป็นข้อความประเภทอนุมัติ เช่น `ยืนยัน`: route ไป approval flow ของ draft ล่าสุดที่ผูกกับ sender และ conversation นั้น
- ถ้าเป็นข้อความประเภทแก้ไข เช่น `แก้ไข`: route ไป correction flow และเปิดสถานะให้มนุษย์หรือระบบแก้ draft ต่อ
- ถ้าเป็นข้อความทั่วไปที่ไม่ใช่คำสั่ง: route ไป classifier เบื้องต้นก่อนว่าเป็นข้อความประกอบรูป, metadata เพิ่มเติม หรือข้อความที่ไม่เกี่ยวข้อง
- ถ้า draft ผ่าน approval แล้ว: route ไป Update Worker ผ่าน update job โดย supervisor ไม่ยิง AWS API เอง
- ถ้าเจอ event ซ้ำหรือไฟล์ซ้ำ: route ไป duplicate handling path แทนการสร้างงานใหม่
- ถ้า OCR ล้มเหลวหรือข้อมูลไม่ครบ: route ไป exception path และแจ้งกลับในแชทตามรูปแบบที่กำหนด

สรุปการ route ตาม role:

- Supervisor รับ event และตัดสินใจเส้นทางงาน
- OCR Worker รับเฉพาะงาน OCR ที่ถูก enqueue แล้ว
- Update Worker รับเฉพาะงาน update ที่ผ่าน approval แล้ว

ดังนั้น supervisor ต้องเป็นตัว orchestrate งานทั้งหมด แต่ไม่ควรทำงานหนักแทน role อื่น

### 2. OCR Worker

หน้าที่:

- ดึง OCR jobs จาก queue
- ดาวน์โหลดรูปจาก storage
- ใช้ Hermes + LLM อ่านข้อมูลจากรูปและแปลงให้อยู่ใน structured draft
- ส่งผลลัพธ์แบบ draft ที่เป็น structured พร้อม confidence และ validation flags กลับมา

สถานะ implementation ปัจจุบัน:

- local path ที่ใช้งานจริงคือ Python worker ที่ consume queue โดยตรง
- worker เรียก Hermes ผ่าน `OCR_WORKER_HERMES_BASE_URL` แทนการรันเป็น Hermes gateway/runtime ของตัวเอง

หมายเหตุ:

- `paddle_ocr/` ใน repo เป็นพื้นที่ทดลองเท่านั้น ไม่ใช่ production OCR path

### 3. Update Worker

หน้าที่:

- ดึง update jobs ที่ผ่าน approval แล้วจาก queue
- แปลงข้อมูล approved ให้อยู่ในรูป payload ของ AWS ปลายทาง
- เรียก downstream API แบบมี retry และ idempotency
- บันทึกสถานะสำเร็จหรือล้มเหลว

## การตัดสินใจด้านข้อมูลและโครงสร้างพื้นฐาน

หัวข้อต่อไปนี้ควรถูกสรุปให้ชัดก่อนลง implementation ลึก:

- Persistence หลัก: S3 object layout และ JSON manifests
- Upload path: Supervisor หรือ worker ที่ถือ binary file เขียนเข้า object storage โดยตรง
- Queue: Redis queue, RabbitMQ หรือ SQS
- Object storage: S3 หรือ storage ที่เข้ากันได้กับ S3
- Dedup keys: `line_event_id`, `line_message_id`, และ business-level result signature
- State model: source message, OCR job, approval, update job

## แผนการทำงานแบบเป็นเฟส

## Phase 0: ล็อก baseline ของ local development

Deliverables:

- [x] รักษา path ที่ใช้งานได้แล้วของ Hermes Supervisor + LINE OA + ngrok
- [x] ทำให้ docs และ scripts ตรงกับ runtime จริง
- [x] ยึด local config ปัจจุบันเป็น reference path ของการพัฒนา

เงื่อนไขจบเฟส:

- [x] นักพัฒนาคนใหม่สามารถยก Hermes local ขึ้นมาและรับ LINE webhook events ได้

## Phase 1: persistence และ webhook workflow ของ supervisor

Deliverables:

- [x] ออกแบบ S3 object schema สำหรับ source messages, drafts, approvals และ update jobs
- [x] ทำ behavior ของ webhook intake รอบ LINE events ใน supervisor
- [x] ทำ message classification สำหรับแยก image, approval, correction และข้อความทั่วไป
- [x] ทำ routing rules จาก LINE event ไปยัง OCR flow, approval flow, correction flow หรือ exception flow
- [x] เก็บ inbound message records และ status transitions
- [x] เพิ่ม event-level deduplication
- [x] เก็บ media metadata และ storage references
- [x] ส่ง acknowledgment กลับหาผู้ส่งให้เร็ว

เงื่อนไขจบเฟส:

- [x] LINE retry เดิมไม่ทำให้เกิด workflow rows ซ้ำ
- [x] text และ image events ขาเข้าถูกเก็บพร้อมสถานะที่ชัดเจน

## Phase 2: media storage และการสร้าง OCR jobs

Deliverables:

- [x] เก็บ inbound media ลง object storage โดยไม่ผ่าน n8n
- [ ] เพิ่ม file-level dedupe ภายหลังถ้าจำเป็น
- [x] สร้าง OCR jobs จาก inbound messages ที่ผ่านเงื่อนไข
- [x] เพิ่ม queue producer logic ฝั่ง supervisor

เงื่อนไขจบเฟส:

- [x] รูปที่ valid 1 รูป สร้าง OCR job ได้พอดี 1 งาน
- [ ] การอัปโหลดซ้ำถูกตรวจจับและจัดการได้อย่างชัดเจน

## Phase 3: implementation ของ OCR worker

Deliverables:

- [x] สร้าง worker ที่ consume OCR jobs
- [x] ทำ Hermes + LLM vision flow สำหรับ OCR worker ให้มี prompt, parsing, และ recovery path ที่ชัดเจน
- [x] กำหนด schema ของ OCR output แบบ structured
- [ ] เพิ่ม validation rules สำหรับ field ที่หาย, confidence ต่ำ และ score table ที่ผิดรูป
- [x] เก็บ draft results กลับเข้าสู่ระบบ

เงื่อนไขจบเฟส:

- [x] OCR job หนึ่งงานให้ผลเป็น draft ที่ถูกบันทึก หรือเข้า exception state

## Phase 4: approval workflow ใน LINE

Deliverables:

- [x] ส่งสรุปผล OCR กลับไปยังผู้ส่งต้นทาง
- [x] รองรับคำสั่งอนุมัติ เช่น `ยืนยัน`
- [x] รองรับคำสั่งแก้ไขหรือปฏิเสธ เช่น `แก้ไข`
- [x] บังคับให้ approval ผูกกับ draft revision ล่าสุดเท่านั้น
- [x] เก็บ approval audit data

เงื่อนไขจบเฟส:

- [x] ผู้ใช้สามารถอนุมัติ draft ปัจจุบันจาก LINE และขยับ workflow ต่อได้
- [x] approval เก่าถูกปฏิเสธได้อย่างปลอดภัย

## Phase 5: update worker และการเชื่อมกับ AWS

Deliverables:

- [ ] สร้าง deterministic update worker
- [ ] นิยามการ map payload จาก approved draft ไปยังรูปแบบ API ของ AWS
- [ ] เพิ่ม retry, idempotency และการบันทึก error
- [ ] ส่งสถานะสำเร็จหรือล้มเหลวกลับเข้าสู่ state ของ supervisor

เงื่อนไขจบเฟส:

- [ ] ผลที่ approved แล้วถูกส่งถึง AWS API เพียงครั้งเดียว
- [ ] ความล้มเหลวสามารถมองเห็นได้และ retry ได้โดยไม่ทำให้ข้อมูลเพี้ยน

## Phase 6: exceptions, audit และ operations

Deliverables:

- [ ] มี exception queue หรือ exception state สำหรับ OCR failures และ drafts ที่ไม่ valid
- [ ] มี audit trail สำหรับผู้ส่ง, การอัปโหลด, OCR output, approval และผล update
- [ ] มี operator queries พื้นฐานหรือเครื่องมือ inspection สำหรับ admin
- [ ] มี structured logs และ runbook notes

เงื่อนไขจบเฟส:

- [ ] operator สามารถไล่ได้ว่าแต่ละ message เกิดอะไรขึ้นบ้างตั้งแต่ต้นจนจบ

## ลำดับการ implement ที่แนะนำ

ลำดับที่ควรทำต่อจากนี้:

1. กำหนด data model และ persistence บน S3 สำหรับ source messages, drafts, approvals และ update jobs
2. ทำ event intake และ event deduplication ฝั่ง supervisor
3. ทำ direct S3 media persistence และ finalize manifests หลังรับไฟล์สำเร็จ
4. ทำ OCR worker ให้เรียก Hermes + LLM เพื่ออ่านรูปและ normalize ผลลัพธ์
5. ทำ approval handling ใน LINE
6. ทำ AWS update worker

## ประเด็นที่ยังต้องตัดสินใจ

คำถามต่อไปนี้ควรถูกตอบก่อนหรือระหว่าง Phase 1:

- local dev และ production จะใช้ queue อะไรเป็นตัวแรก?
- local dev จะเก็บรูปขาเข้าไว้ที่ไหน?
- local dev และ production จะจัดการ S3 indexes และ manifests อย่างไรเมื่อเกิด partial failure?
- จะใช้วิธี reconcile orphaned uploads อย่างไรเมื่อ upload สำเร็จแต่ finalize upload ไม่สำเร็จ?
- AWS API ปลายทางมี contract และ auth method แบบใดแน่ชัด?
- ธุรกิจต้องการ OCR output schema แบบไหนอย่างแน่นอน?
- ใน production จะ allow users หรือ groups ใดบ้างแทน `LINE_ALLOW_ALL_USERS=true`?

## งานถัดไปที่ควรทำทันที

งานที่มีมูลค่าสูงสุดจากสถานะ repo ปัจจุบันคือ:

- [x] สร้าง persistence layout และ schema สำหรับ workflow entities บน S3
- [ ] ทำ deduplication และ workflow state transitions ฝั่ง supervisor
- [ ] นิยาม supervisor routing rules สำหรับ image, approval, correction และ duplicate cases
- [ ] ทำ direct S3 persistence path ใน supervisor และ worker
- [ ] นิยาม OCR job payload และ worker contract
- [ ] ทำให้ OCR worker เรียก Hermes + LLM ได้แบบ worker-friendly และควบคุม retry/idempotency ได้
- [ ] เปลี่ยนจาก LINE access แบบเปิดกว้างใน dev ไปเป็น allowlists ที่ชัดเจนก่อนขึ้น production
