# Generated From Public Payloads

ไฟล์ในโฟลเดอร์นี้สร้างย้อนกลับจาก public payload ที่มีอยู่ใน repo เพื่อใช้เป็น
fixture สำหรับทดสอบ mapping ในอนาคต

แหล่งข้อมูลต้นทาง:

- [sumary.json](/D:/ch7/election/docs/sumary.json)
- [districts.json](/D:/ch7/election/docs/districts.json)

ไฟล์ที่สร้าง:

- `69-governor-electiondata.json`

## กติกา reverse-map ที่ใช้

- `type` map มาจาก `pageMeta.resultStatus`
  - `LIVE_COUNT` -> `LIVE`
  - `FINAL` -> `FINAL`
- `total.*` map มาจาก `summary.*`
- `total.result[*].candidateId` ใช้ `candidateNumber` แปลงเป็น string
- `districts[*].voting.*` map มาจาก `constituencies[*]`
- เขตที่ไม่มีข้อมูลคะแนน จะถูกสร้างเป็น `result: []` และ `progress: 0`

## สมมติฐานสำคัญ

public payload ไม่มีข้อมูล `pollingUnits` ระดับหน่วยเลือกตั้งแบบดิบ จึงต้องใส่ค่า
สังเคราะห์เพื่อให้ fixture มี raw shape ครบ

กติกาที่ใช้:

- ทุกเขตมี `pollingUnits.total = 1`
- ถ้าเขตนั้นมีคะแนนหรือ `countedPercentage > 0` ให้ `pollingUnits.reported = 1`
- ถ้าไม่มีข้อมูล ให้ `pollingUnits.reported = 0`
- ระดับ `total.pollingUnits` ใช้ผลรวมจากทุกเขต

ดังนั้น fixture นี้เหมาะสำหรับทดสอบ logic mapping shape และ field transform
ไม่ใช่สำหรับทดสอบความหมายจริงของจำนวนหน่วยเลือกตั้งดิบ
