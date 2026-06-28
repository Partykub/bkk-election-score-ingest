# API Examples

โฟลเดอร์นี้เก็บตัวอย่าง JSON ของ API ปลายทางที่ระบบต้องผลิตหรือเทียบผลให้ตรงกับ requirement ภายนอก
ไฟล์เหล่านี้ไม่ใช่ runtime input ของ Docker Compose production แต่เป็น contract examples
สำหรับพัฒนาและตรวจสอบ `results-api`

## Files

- `sumary.json` - ตัวอย่าง `GET /api/v1/governor-results/summary`
- `sumary-sorkor.json` - ตัวอย่าง summary ชุด ส.ก.
- `constituency-bangkok.json` - ตัวอย่างผลเขต กทม. ตาม resource `constituency-bangkok`
- `districts.json` - ตัวอย่าง district/constituency payload
- `districts-sorkor.json` - ตัวอย่าง district/constituency payload ชุด ส.ก.
- `parties.json` - ตัวอย่าง party master data
- `candidates-featured.json` - ตัวอย่าง candidate catalog ที่ฝัง `party` object ไว้ในผู้สมัครแต่ละคน

## Zero scores (`zero/`)

โฟลเดอร์ `zero/` ใช้ชื่อไฟล์เดียวกับด้านบน แต่คะแนนและสถิติการนับเป็น 0 ทั้งหมด:

- `zero/sumary.json`
- `zero/sumary-sorkor.json`
- `zero/districts.json`
- `zero/districts-sorkor.json`

หมายเหตุ: ชื่อ `sumary*.json` ถูกคงไว้ตามไฟล์ตัวอย่างเดิมเพื่อไม่ทำลาย reference ที่อาจถูกใช้อยู่นอก repo

Live S3 keys สำหรับ ส.ก. (promote จาก `governor-results-bkk` เมื่อ active source = bkk):

- `api-data/governor-results/sumary-sorkor.json`
- `api-data/governor-results/districts-sorkor.json`

Ingest keys (ก่อน promote):

- `api-data/governor-results-bkk/sumary-sorkor.json`
- `api-data/governor-results-bkk/districts-sorkor.json`

Runtime notes for these payloads:
[`../governor-results-runtime.md`](../governor-results-runtime.md)

## Mapping Rules

- `parties.json` เป็น master data ของพรรคเท่านั้น โดย 1 พรรคควรมี 1 object
- การผูก `candidateNumber -> party` ต้องอยู่ใน candidate data เช่น `candidates-featured.json`
- ผู้สมัครอิสระควรใช้ `party.id = "independent"`
- กลุ่มหาเสียงที่ไม่ใช่พรรค เช่น `กลุ่มกรุงเทพบินได้` ควรเก็บแยกเป็น field เช่น `groupName`
