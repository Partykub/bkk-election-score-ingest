# 🗳️ Bangkok Governor Election 2026 - API Specification

**Version:** 1.0.0
**Base URL:** `/api/v1`

---

## 1. Get Overall Election Summary
ดึงข้อมูลสรุปผลการเลือกตั้งภาพรวมระดับจังหวัด และรายชื่อผู้สมัครพร้อมคะแนนจัดอันดับทั้งหมด

* **Endpoint:** `GET /governor-results/summary`
* **Response: `200 OK` (application/json)**

```json
{
  "schemaVersion": "1.0",
  "resource": "governor-results",
  "pageMeta": {
    "electionId": "string",       // เช่น "bkk-governor-2026"
    "title": "string",            // เช่น "ผลการเลือกตั้งผู้ว่าฯ กรุงเทพมหานคร"
    "resultStatus": "string",     // เช่น "LIVE_COUNT", "OFFICIAL"
    "generatedAt": "string (ISO 8601)"
  },
  "summary": {
    "countedUnits": "number",     // หน่วยที่นับคะแนนแล้ว
    "totalUnits": "number",       // หน่วยเลือกตั้งทั้งหมด
    "countedPercentage": "number",// เปอร์เซ็นต์ความคืบหน้าการนับคะแนน
    "eligibleVoters": "number",   // จำนวนผู้มีสิทธิเลือกตั้งทั้งหมด
    "voterTurnout": "number",     // จำนวนผู้มาใช้สิทธิ
    "voterTurnoutPercentage": "number",
    "validBallots": "number",     // บัตรดี
    "invalidBallots": "number",   // บัตรเสีย
    "abstainedBallots": "number", // ไม่ออกเสียง (Vote No)
    "lastUpdatedAt": "string (ISO 8601)"
  },
  "candidates": [
    {
      "candidateId": "string",
      "candidateNumber": "number",
      "name": "string",
      "color": "string (Hex code)",
      "voteCount": "number",
      "votePercentage": "number",
      "rank": "number",           // อันดับปัจจุบัน
      "isLeading": "boolean"      // true หากคะแนนนำเป็นอันดับ 1
    }
  ],
  "dataQuality": {
    "isComplete": "boolean",      // นับเสร็จ 100% หรือยัง
    "isDelayed": "boolean",
    "warnings": ["string"]
  }
}
```

---

## 2. Get Constituency (District) Results
ดึงข้อมูลผลการลงคะแนนแยกตามรายเขต (50 เขต กทม.) สำหรับแสดงผลบนแผนที่และกระดานจัดอันดับรายเขต

* **Endpoint:** `GET /governor-results/districts`
* **Response: `200 OK` (application/json)**

```json
{
  "schemaVersion": "1.0",
  "resource": "constituency-bangkok",
  "generatedAt": "string (ISO 8601)",
  "data": {
    "constituencies": [
      {
        "areaId": "string (UUID)",     // รหัสอ้างอิงเขต (ตรงกับแผนที่ Polygon)
        "number": "number",            // หมายเลขเขต
        "name": "string",              // ชื่อเขต (เช่น "หนองจอก")
        "leadingCandidateId": "string",// ID ของผู้สมัครที่ได้คะแนนอันดับ 1 ในเขตนี้
        "candidates": [
          {
            "candidateId": "string",
            "candidateNumber": "number",
            "name": "string",
            "candidateSrc": "string (URL)", // รูปโปรไฟล์ผู้สมัคร (เฉพาะหน้าเขต)
            "color": "string (Hex code)",
            "voteCount": "number",
            "votePercentage": "number",
            "rank": "number",
            "isLeading": "boolean"
          }
        ]
      }
    ]
  }
}
```

---

## 3. Get Master Districts List (อ้างอิงรายชื่อเขต)
ดึงข้อมูล Master Data รายชื่อเขตทั้งหมดในกรุงเทพมหานคร สำหรับจับคู่ Area ID หรือ District Code

* **Endpoint:** `GET /master/districts?provinceCode=10`
* **Response: `200 OK` (application/json)**

```json
[
  {
    "id": "number",
    "provinceCode": "number",      // 10 สำหรับ กรุงเทพมหานคร
    "districtCode": "number",      // รหัสเขตตามหลักการปกครอง (เช่น 1001)
    "districtNameEn": "string",    // "Phra Nakhon"
    "districtNameTh": "string",    // "พระนคร"
    "postalCode": "number"
  }
]
```
