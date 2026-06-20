# Web Intake Requirements

เอกสารนี้สรุปแนวทางเพิ่มช่องทาง `Web/PWA` เพื่อทำหน้าที่แทน `LINE` ได้ในอนาคต โดยยังคงใช้ backend, OCR flow และ dashboard เดิมร่วมกัน

## Goal

- เพิ่มช่องทางรับงานผ่านเว็บจากมือถือ
- ให้ Web/PWA และ LINE ใช้งานร่วมกันได้ในช่วง migration
- ไม่แยก workflow เป็นสองระบบ
- คง dashboard และ results flow เดิมไว้

## Target Flow

```text
Reporter เปิด Web/PWA
-> ถ่ายรูปหรืออัปโหลดรูป
-> ระบบสร้าง submission
-> OCR worker ประมวลผล
-> ผู้ใช้เปิดหน้าเดิมเพื่อดู draft
-> แก้ไข/ยืนยันผล
-> dashboard และ results API เห็นข้อมูลจากแหล่งเดียวกัน
```

## Architecture Direction

- Web/PWA ต้องเป็นอีก `intake channel` หนึ่ง ไม่ใช่ระบบแยก
- LINE และ Web ต้องเขียนเข้า submission/workflow กลางเดียวกัน
- OCR, draft, approval manifest, audit event และ approved result ต้องเก็บใน storage เดิม
- dashboard เดิมควรอ่านข้อมูลจาก source กลางเหมือนเดิม และเพิ่มการแยก `source_channel`

## Channel Strategy

- ระยะสั้น: เปิดทั้ง `LINE` และ `WEB`
- ระยะกลาง: ให้ reporter ใหม่ใช้ Web/PWA เป็นหลัก
- ระยะยาว: ปิด LINE intake ได้โดยไม่ต้องแก้ OCR, dashboard, results API

ค่าข้อมูลที่ควรมีเพิ่ม:

- `source_channel = line | web`
- `source_user_id` หรือ identifier ที่เทียบได้กับผู้ส่งจากแต่ละช่องทาง
- `submission_id` กลางที่ไม่ผูกกับ LINE message id

## Web/PWA Scope

ฟังก์ชันขั้นต่ำ:

- login หรือ identify ผู้ใช้ก่อนส่งข้อมูล
- ถ่ายรูปจากมือถือหรืออัปโหลดรูป
- ดูสถานะงาน: received, queued, processing, awaiting_approval, approved, rejected
- ดู draft OCR
- แก้ไขข้อมูล
- กดยืนยันหรือปฏิเสธ
- ดูประวัติ submission ของตัวเอง

ข้อเสนอ UX:

- mobile-first
- ใช้ PWA เพื่อเปิดจาก home screen ได้
- ใช้ polling ก่อน ถ้ายังไม่จำเป็นต้องเพิ่ม websocket
- แสดงสถานะชัดเจนแทนการ push ผ่าน chat

## Dashboard Impact

dashboard เดิมไม่ควรถูกแทนที่ แต่ควรเพิ่มความสามารถดังนี้:

- filter ตาม `source_channel`
- เห็นว่างานมาจาก `LINE` หรือ `WEB`
- เปิด/ปิด intake channel ได้ในระดับ config
- operator เข้ามาดูและช่วยแก้ไขงานจากทั้งสองช่องทางได้

## Infra Constraints

อ้างอิงจาก [architecture.md](/d:/ch7/election/docs/architecture.md:1) และ [Caddyfile](/d:/ch7/election/deploy/ec2/Caddyfile:1)

- public ingress ใช้แค่ `80` และ `443`
- service ภายในยังใช้ internal ports เดิม
- Caddy เป็นจุดรับ traffic สาธารณะจุดเดียว
- route ปัจจุบันมี:
  - `/line/webhook`, `/webhook`, `/line/liff/*` -> `line-relay:8646`
  - `/api/*` -> `results-api:8080`
  - `/monitor` -> `results-api:8080`
  - `/health` -> Caddy built-in
- route ใหม่ของ Web/PWA ต้องเพิ่มใน Caddy ก่อน fallback 404

ข้อเสนอ path:

- `/app/*` -> web app
- `/web/*` -> web intake API หรือ web backend

ข้อเสนอ container:

- เพิ่ม container ใหม่ เช่น `web-intake`
- ไม่ควรยัด Web/PWA เข้า `results-api` ถ้าไม่จำเป็น

## Domain and Networking

- ระบบต้องอิง `domain` ไม่อิง `IP`
- infra สามารถเปลี่ยน public IP ได้ ตราบใดที่ DNS ชี้มาที่เครื่องถูกต้อง
- production ควรใช้ `Elastic IP` หรือ endpoint ที่นิ่ง
- LINE webhook และ Web/PWA ต้องวิ่งผ่าน domain เดียวกันได้

ตัวอย่าง:

```text
https://election.example.com/line/webhook
https://election.example.com/app/
https://election.example.com/api/...
```

## Suggested Backend Shape

ควรมี API กลางสำหรับ submission โดยไม่ผูกกับ LINE:

- `POST /web/submissions`
- `POST /web/submissions/{id}/images`
- `GET /web/submissions/{id}`
- `POST /web/submissions/{id}/approve`
- `POST /web/submissions/{id}/correct`
- `POST /web/submissions/{id}/reject`

LINE relay และ Web/PWA ควรเรียก backend กลางชุดเดียวกันนี้

## Migration Plan

1. แยก submission workflow ให้เป็น channel-agnostic
2. เพิ่ม field `source_channel`
3. ทำ Web/PWA intake screen
4. เพิ่ม route ใหม่ใน Caddy
5. เปิดใช้งาน LINE + WEB พร้อมกัน
6. เพิ่ม filter/source switch ใน dashboard
7. ค่อยย้ายผู้ใช้จาก LINE มา Web
8. ปิด LINE intake เมื่อพร้อม

## Non-Goals

- ไม่เปลี่ยน OCR worker หลักโดยไม่มีเหตุจำเป็น
- ไม่รื้อ dashboard เดิมทิ้ง
- ไม่เปิด public ports เพิ่มนอกจาก `80/443`
- ไม่ผูก flow ใหม่กับ IP address

## Open Questions

- ผู้ใช้ Web จะ auth ด้วยวิธีใด: OTP, magic link, account ภายใน, หรือ token จากหน่วยงาน
- ภาพที่อัปโหลดจาก Web จะเก็บ metadata อะไรเพิ่มบ้าง
- จะให้ Web approval ใช้ polling หรือ realtime
- dashboard เดิมอยู่ที่ `results-api` ต่อไป หรือจะแยก admin UI ออกภายหลัง
- จะเปิด Web intake พร้อม LINE ทันที หรือ rollout ทีละกลุ่ม
