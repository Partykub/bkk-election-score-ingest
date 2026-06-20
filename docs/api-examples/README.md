# API Examples

โฟลเดอร์นี้เก็บตัวอย่าง JSON ของ API ปลายทางที่ระบบต้องผลิตหรือเทียบผลให้ตรง
กับ requirement ภายนอก ไฟล์เหล่านี้ไม่ใช่ runtime input ของ Docker Compose
production แต่เป็น contract examples สำหรับพัฒนาและตรวจสอบ `results-api`

## Files

- `sumary.json` — ตัวอย่าง `GET /api/v1/governor-results/summary`
- `sumary-sorkor.json` — ตัวอย่าง summary ชุด ส.ก.
- `constituency-bangkok.json` — ตัวอย่างผลเขต กทม. ตาม resource `constituency-bangkok`
- `districts.json` — ตัวอย่าง district/constituency payload
- `districts-sorkor.json` — ตัวอย่าง district/constituency payload ชุด ส.ก.
- `parties.json` — ตัวอย่าง party master data

หมายเหตุ: ชื่อ `sumary*.json` ถูกคงไว้ตามไฟล์ตัวอย่างเดิมเพื่อไม่ทำลาย reference
ที่อาจถูกใช้อยู่นอก repo
