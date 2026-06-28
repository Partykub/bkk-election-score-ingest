# เอกสารสเปก Public Static JSON สำหรับผลเลือกตั้งผู้ว่าฯ กทม.

**เวอร์ชันเอกสาร:** `1.4.0`  
**สถานะเอกสาร:** พร้อมส่งต่อให้ทีม frontend หรือผู้ใช้งานภายนอก  
**รูปแบบข้อมูล:** `application/json; charset=utf-8`

เอกสารนี้อธิบายไฟล์ JSON แบบ public ที่ผู้ใช้งานควรเรียกใช้จริงในระบบปัจจุบัน

## แนวทางการเรียกใช้งานที่ถูกต้อง

ผู้ใช้งานภายนอกควรอ่านข้อมูลจากไฟล์ static JSON ที่ publish แล้วเท่านั้น  
ไม่ควรผูกระบบกับ internal runtime API โดยตรง

### Public URL ที่ใช้ Test

ไฟล์สรุปภาพรวม:

```text
https://www.ch7.com/bkkelection2569/api-data/governor-results-dev/sumary.json
```

ไฟล์ผลรายเขต:

```text
https://www.ch7.com/bkkelection2569/api-data/governor-results-dev/districts.json
```

### Public URL ที่ใช้งานจริง

ไฟล์สรุปภาพรวม:

```text
https://www.ch7.com/bkkelection2569/api-data/governor-results/sumary.json
```

ไฟล์ผลรายเขต:

```text
https://www.ch7.com/bkkelection2569/api-data/governor-results/districts.json
```

## ที่มาของไฟล์และการ publish

ไฟล์ public ข้างต้นถูกสร้างและ publish ตามลำดับนี้:

1. ระบบ backend สร้าง payload ของ `summary` และ `districts`
2. ระบบ export payload ออกเป็นไฟล์ JSON
3. ระบบเขียนไฟล์ไปยัง S3
4. public client อ่านไฟล์ผ่าน `www.ch7.com`

### S3 origin key

ไฟล์สรุปภาพรวม:

```text
s3://ch7-static-bkkelection2569/api-data/governor-results-dev/sumary.json
```

ไฟล์ผลรายเขต:

```text
s3://ch7-static-bkkelection2569/api-data/governor-results-dev/districts.json
```

### หมายเหตุสำคัญ

- URL public คือ endpoint ที่ consumer ควรใช้
- S3 key คือที่เก็บไฟล์ต้นทางฝั่งระบบ
- ไม่ต้องใช้ API key ในการอ่านไฟล์ public ชุดนี้
- ชื่อไฟล์ summary ปัจจุบันสะกดเป็น `sumary.json` ตามไฟล์ที่ publish จริงในระบบ

## พฤติกรรมการอัปเดตข้อมูล

พฤติกรรมปัจจุบันของระบบ:

1. backend สร้างข้อมูลล่าสุด
2. export job เขียนไฟล์ลง S3
3. ผู้ใช้งานภายนอกอ่านไฟล์ผ่าน public URL

ข้อควรเข้าใจสำหรับ consumer:

- ไฟล์ public เป็น static artifact ที่ถูก publish เป็นรอบ
- อาจมี delay เล็กน้อยระหว่างข้อมูลในระบบกับข้อมูลที่ public เห็น
- อาจมีผลจาก CDN cache หรือ browser cache ชั่วคราว
- consumer ควรออกแบบให้รองรับข้อมูลที่ยังไม่อัปเดตล่าสุดทันที

## กติกาเรื่องเวลา

- timestamp ทุกตัวอยู่ในรูปแบบ ISO 8601
- payload ปัจจุบันใช้เวลาแบบ UTC และลงท้ายด้วย `Z`
- ตัวอย่าง: `2026-06-23T02:59:37.317Z`

## กติกาเรื่อง schema

- ให้ใช้ `schemaVersion` เป็นตัวตรวจสอบ compatibility
- consumer ควร ignore field ใหม่ที่อาจถูกเพิ่มในอนาคต
- consumer ควรรองรับทั้ง field ที่เป็น `null` และ field optional ที่อาจไม่มีในบางกรณี

## 1. Summary JSON

จุดประสงค์:

- ใช้แสดงภาพรวมผลเลือกตั้งทั้งกรุงเทพฯ
- ใช้แสดง progress การนับ
- ใช้แสดงอันดับผู้สมัครรวม
- ใช้แสดงสถานะคุณภาพข้อมูล

### โครงสร้างตัวอย่าง

```json
{
  "schemaVersion": "1.0",
  "resource": "governor-results",
  "pageMeta": {
    "electionId": "bkk-governor-2026",
    "title": "ผลการเลือกตั้งผู้ว่าฯ กรุงเทพมหานคร",
    "resultStatus": "LIVE_COUNT",
    "generatedAt": "2026-06-23T02:59:37.147Z"
  },
  "summary": {
    "countedUnits": 1,
    "totalUnits": 50,
    "countedPercentage": 2.0,
    "eligibleVoters": 2000,
    "voterTurnout": 1900,
    "voterTurnoutPercentage": 95.0,
    "validBallots": 1790,
    "invalidBallots": 60,
    "abstainedBallots": 50,
    "countedBallots": 1900,
    "countedBallotsPercentage": 100.0,
    "validBallotsPercentage": 94.21,
    "invalidBallotsPercentage": 3.16,
    "abstainedBallotsPercentage": 2.63,
    "lastUpdatedAt": "2026-06-22T17:14:00Z"
  },
  "candidates": [
    {
      "candidateId": "somchai",
      "candidateNumber": 18,
      "name": "สมชัย เจริญวรเกียรติ",
      "candidateSrc": "https://example.com/candidate.png",
      "color": "#7c200a",
      "voteCount": 190,
      "votePercentage": 10.92,
      "backgroundSrc": "https://example.com/bg.png",
      "party": {
        "id": "independent",
        "name": "อิสระ",
        "color": "#6B7280",
        "logoUrl": null
      },
      "rank": 1,
      "isLeading": true
    }
  ],
  "dataQuality": {
    "isComplete": false,
    "isDelayed": true,
    "warnings": []
  },
  "dataInterpretation": {
    "mode": "latest_snapshot",
    "description": "Use the latest available value for each field in each district."
  }
}
```

### คำอธิบาย field ระดับ root

| Field | Type | Required | Nullable | ความหมาย |
| --- | --- | --- | --- | --- |
| `schemaVersion` | `string` | Yes | No | เวอร์ชันของ schema |
| `resource` | `string` | Yes | No | ประเภท resource ปัจจุบันคือ `governor-results` |
| `pageMeta` | `object` | Yes | No | metadata ของหน้าและชุดข้อมูล |
| `summary` | `object` | Yes | No | ตัวเลขสรุปภาพรวม |
| `candidates` | `array<object>` | Yes | No | รายชื่อผู้สมัครพร้อมอันดับและคะแนนรวม |
| `dataQuality` | `object` | Yes | No | สถานะความสมบูรณ์และคำเตือนของข้อมูล |
| `dataInterpretation` | `object` | Yes ใน payload ปัจจุบัน | No | วิธีที่ backend ใช้รวมและตีความข้อมูล |

### `pageMeta`

| Field | Type | Required | Nullable | ความหมาย |
| --- | --- | --- | --- | --- |
| `electionId` | `string` | Yes | No | รหัสการเลือกตั้ง |
| `title` | `string` | Yes | No | ชื่อที่ใช้แสดงบนหน้า |
| `resultStatus` | `string` | Yes | No | สถานะผล เช่น `LIVE_COUNT`, `OFFICIAL` |
| `generatedAt` | `string` | Yes | No | เวลาที่สร้าง payload ชุดนี้ |

### `summary`

| Field | Type | Required | Nullable | ความหมาย |
| --- | --- | --- | --- | --- |
| `countedUnits` | `number` | Yes | No | จำนวนเขตที่มีผลอนุมัติแล้วและถูกนำมานับ |
| `totalUnits` | `number` | Yes | Yes | จำนวนเขตทั้งหมดที่ระบบรู้จัก |
| `countedPercentage` | `number` | Yes | Yes | เปอร์เซ็นต์ความคืบหน้าของการนับเขต |
| `eligibleVoters` | `number` | Yes | Yes | จำนวนผู้มีสิทธิเลือกตั้งรวม |
| `voterTurnout` | `number` | Yes | Yes | จำนวนผู้มาใช้สิทธิรวม |
| `voterTurnoutPercentage` | `number` | Yes | Yes | เปอร์เซ็นต์ผู้มาใช้สิทธิ |
| `validBallots` | `number` | Yes | Yes | จำนวนบัตรดีรวม |
| `invalidBallots` | `number` | Yes | Yes | จำนวนบัตรเสียรวม |
| `abstainedBallots` | `number` | Yes | Yes | จำนวนบัตรไม่เลือกผู้สมัครใด |
| `countedBallots` | `number` | Yes | Yes | จำนวนบัตรที่นับแล้วรวม คำนวณจาก `validBallots + invalidBallots + abstainedBallots` |
| `countedBallotsPercentage` | `number` | Yes | Yes | เปอร์เซ็นต์บัตรที่นับแล้วเทียบกับ `voterTurnout` |
| `validBallotsPercentage` | `number` | Yes | Yes | เปอร์เซ็นต์บัตรดี |
| `invalidBallotsPercentage` | `number` | Yes | Yes | เปอร์เซ็นต์บัตรเสีย |
| `abstainedBallotsPercentage` | `number` | Yes | Yes | เปอร์เซ็นต์บัตรไม่เลือกผู้สมัครใด |
| `lastUpdatedAt` | `string` | Yes | Yes | เวลาของข้อมูลล่าสุดที่ถูกใช้ใน summary |

### `candidates[]`

| Field | Type | Required | Nullable | ความหมาย |
| --- | --- | --- | --- | --- |
| `candidateId` | `string` | Yes | Yes | รหัสผู้สมัคร |
| `candidateNumber` | `number` | Yes | No | หมายเลขผู้สมัคร |
| `name` | `string` | Yes | Yes | ชื่อผู้สมัคร |
| `candidateSrc` | `string` | Yes | Yes | URL รูปผู้สมัคร |
| `color` | `string` | Yes | Yes | สีประจำผู้สมัคร |
| `voteCount` | `number` | Yes | No | คะแนนรวมปัจจุบัน |
| `votePercentage` | `number` | Yes | No | เปอร์เซ็นต์คะแนนรวม |
| `backgroundSrc` | `string` | Yes | Yes | URL ภาพพื้นหลังผู้สมัคร |
| `party` | `object` | Yes | No | ข้อมูลพรรคของผู้สมัคร |
| `rank` | `number` | Yes | No | อันดับปัจจุบัน |
| `isLeading` | `boolean` | Yes | No | เป็นอันดับ 1 หรือไม่ |

### `candidates[].party`

| Field | Type | Required | Nullable | ความหมาย |
| --- | --- | --- | --- | --- |
| `id` | `string` | Yes | Yes | รหัสพรรค |
| `name` | `string` | Yes | Yes | ชื่อพรรค |
| `color` | `string` | Yes | Yes | สีพรรค |
| `logoUrl` | `string` | Yes | Yes | URL โลโก้พรรค |

### `dataQuality`

| Field | Type | Required | Nullable | ความหมาย |
| --- | --- | --- | --- | --- |
| `isComplete` | `boolean` | Yes | Yes | นับครบทุกเขตแล้วหรือไม่ |
| `isDelayed` | `boolean` | Yes | No | ข้อมูลถือว่าล่าช้าตาม threshold ของระบบหรือไม่ |
| `warnings` | `array<string>` | Yes | No | รายการคำเตือนที่ให้ consumer ใช้อ่านประกอบ |

### `dataInterpretation`

| Field | Type | Required | Nullable | ความหมาย |
| --- | --- | --- | --- | --- |
| `mode` | `string` | Yes | No | โหมดการรวมข้อมูล เช่น `latest_snapshot` หรือ `incremental_delta` |
| `description` | `string` | Yes | No | คำอธิบายของโหมดการรวมข้อมูล |

หมายเหตุ:

- `latest_snapshot` หมายถึงเลือก "ค่าล่าสุดราย field" ในแต่ละเขต ไม่ใช่เลือก approved result ล่าสุดทั้งก้อน
- ตัวอย่างเช่น ถ้ารูปล่าสุดมี `candidate_scores` แต่ไม่มี `valid_ballots` ระบบจะใช้ `candidate_scores` จากรูปนั้น และย้อนใช้ `valid_ballots` ล่าสุดจากรูปก่อนหน้าในเขตเดียวกัน
- `incremental_delta` ยังหมายถึงนำ approved results ทุกก้อนในเขตนั้นมาบวกสะสมตาม field เดิม

## 2. Districts JSON

จุดประสงค์:

- ใช้แสดงผลรายเขต
- ใช้ทำแผนที่
- ใช้ทำตารางหรือหน้ารายละเอียดรายเขต

### โครงสร้างตัวอย่าง

```json
{
  "schemaVersion": "1.0",
  "resource": "constituency-bangkok",
  "generatedAt": "2026-06-23T02:59:37.317Z",
  "constituencies": [
    {
      "areaId": "26b4aad6-94b3-490a-9390-71636d5e97a4",
      "number": 1,
      "name": "พระนคร",
      "leadingCandidateId": "somchai",
      "countedPercentage": 100.0,
      "sumaryVoteCount": 190,
      "eligibleVoters": 2000,
      "voterTurnout": 1900,
      "voterTurnoutPercentage": 95.0,
      "validBallots": 1790,
      "invalidBallots": 60,
      "abstainedBallots": 50,
      "countedBallots": 1900,
      "countedBallotsPercentage": 100.0,
      "lastUpdatedAt": "2026-06-22T17:14:00Z",
      "candidates": [
        {
          "candidateId": "somchai",
          "candidateNumber": 18,
          "name": "สมชัย เจริญวรเกียรติ",
          "candidateSrc": "https://example.com/candidate.png",
          "color": "#7c200a",
          "backgroundSrc": "https://example.com/bg.png",
          "party": {
            "id": "independent",
            "name": "อิสระ",
            "color": "#6B7280",
            "logoUrl": null
          },
          "voteCount": 190,
          "votePercentage": 10.92,
          "rank": 1,
          "isLeading": true
        }
      ]
    }
  ]
}
```

### คำอธิบาย field ระดับ root

| Field | Type | Required | Nullable | ความหมาย |
| --- | --- | --- | --- | --- |
| `schemaVersion` | `string` | Yes | No | เวอร์ชันของ schema |
| `resource` | `string` | Yes | No | ประเภท resource ปัจจุบันคือ `constituency-bangkok` |
| `generatedAt` | `string` | Yes | No | เวลาที่สร้าง payload ชุดนี้ |
| `constituencies` | `array<object>` | Yes | No | รายการผลรายเขต |

### `constituencies[]`

| Field | Type | Required | Nullable | ความหมาย |
| --- | --- | --- | --- | --- |
| `areaId` | `string` | Yes | No | รหัสเขตที่ frontend ใช้จับกับ election area หรือ polygon โดยจะเลือกใช้ `electionAreaId` ก่อนถ้ามี |
| `number` | `number` | Yes | Yes | หมายเลขเขต |
| `name` | `string` | Yes | Yes | ชื่อเขต |
| `leadingCandidateId` | `string` | Yes | Yes | รหัสผู้สมัครที่นำคะแนนในเขตนั้น |
| `countedPercentage` | `number` | No | Yes | ความคืบหน้าของเขตนั้น |
| `sumaryVoteCount` | `number` | No | Yes | ผลรวมคะแนนผู้สมัครทั้งหมดในเขตนั้น |
| `eligibleVoters` | `number` | No | Yes | จำนวนผู้มีสิทธิในเขตนั้น |
| `voterTurnout` | `number` | No | Yes | จำนวนผู้มาใช้สิทธิในเขตนั้น |
| `voterTurnoutPercentage` | `number` | No | Yes | เปอร์เซ็นต์ผู้มาใช้สิทธิในเขตนั้น |
| `validBallots` | `number` | No | Yes | จำนวนบัตรดีในเขตนั้น |
| `invalidBallots` | `number` | No | Yes | จำนวนบัตรเสียในเขตนั้น |
| `abstainedBallots` | `number` | No | Yes | จำนวนบัตรไม่เลือกผู้สมัครใดในเขตนั้น |
| `countedBallots` | `number` | No | Yes | จำนวนบัตรที่นับแล้วในเขตนั้น คำนวณจาก `validBallots + invalidBallots + abstainedBallots` |
| `countedBallotsPercentage` | `number` | No | Yes | เปอร์เซ็นต์บัตรที่นับแล้วในเขตนั้นเทียบกับ `voterTurnout` |
| `lastUpdatedAt` | `string` | No | Yes | เวลาของข้อมูลล่าสุดในเขตนั้น |
| `candidates` | `array<object>` | Yes | No | รายการคะแนนผู้สมัครในเขตนั้น |

หมายเหตุ:

- ถ้าเขตนั้นยังไม่มี approved result `candidates` อาจเป็น `[]`
- field สรุปของเขต เช่น `countedPercentage`, `sumaryVoteCount` หรือ `eligibleVoters` อาจไม่มีใน payload ของเขตนั้นเลย
- ในโหมด `latest_snapshot` field สรุปของเขตอาจมาจาก approved คนละรายการกันได้ เช่นคะแนนผู้สมัครจาก score sheet ล่าสุด แต่จำนวนบัตรจาก ballot summary ล่าสุด

### `constituencies[].candidates[]`

| Field | Type | Required | Nullable | ความหมาย |
| --- | --- | --- | --- | --- |
| `candidateId` | `string` | Yes | Yes | รหัสผู้สมัคร |
| `candidateNumber` | `number` | Yes | No | หมายเลขผู้สมัคร |
| `name` | `string` | Yes | Yes | ชื่อผู้สมัคร |
| `candidateSrc` | `string` | Yes | Yes | URL รูปผู้สมัคร |
| `color` | `string` | Yes | Yes | สีประจำผู้สมัคร |
| `backgroundSrc` | `string` | Yes | Yes | URL ภาพพื้นหลังผู้สมัคร |
| `party` | `object` | Yes | No | ข้อมูลพรรคของผู้สมัคร |
| `voteCount` | `number` | Yes | No | คะแนนของผู้สมัครในเขตนั้น |
| `votePercentage` | `number` | Yes | No | เปอร์เซ็นต์คะแนนของผู้สมัครในเขตนั้น |
| `rank` | `number` | Yes | No | อันดับของผู้สมัครในเขตนั้น |
| `isLeading` | `boolean` | Yes | No | ผู้สมัครคนนั้นเป็นผู้นำในเขตนั้นหรือไม่ |

### `constituencies[].candidates[].party`

| Field | Type | Required | Nullable | ความหมาย |
| --- | --- | --- | --- | --- |
| `id` | `string` | Yes | Yes | รหัสพรรค |
| `name` | `string` | Yes | Yes | ชื่อพรรค |
| `color` | `string` | Yes | Yes | สีพรรค |
| `logoUrl` | `string` | Yes | Yes | URL โลโก้พรรค |

## พฤติกรรมกรณีข้อมูลยังไม่ครบหรือมีบางส่วน

consumer ควรรองรับกรณีเหล่านี้:

- ค่าใน summary อาจเป็น `null`
- field บางตัวใน district อาจไม่มีมาเลย
- `candidates` ของบางเขตอาจเป็น array ว่าง
- `party.logoUrl` อาจเป็น `null`
- `candidateId`, `name`, หรือ URL บางตัวอาจไม่มี หาก metadata ต้นทางยังไม่ครบ

## Checklist สำหรับ consumer

สิ่งที่แนะนำให้รองรับ:

- อ่านข้อมูลจาก public URL เท่านั้น
- อย่าสมมติว่าทุก field ตัวเลขจะมีเสมอ
- อย่าสมมติว่าทุก field string จะไม่เป็น `null`
- ควร ignore field ใหม่ที่อาจถูกเพิ่มในอนาคต
- ใช้ `schemaVersion` ช่วยตรวจ compatibility
- รองรับกรณีข้อมูลยังไม่ทัน export หรือยังติด cache

## นโยบายการเปลี่ยน schema

แนวทางปัจจุบัน:

- ถ้าเป็นการเพิ่ม field แบบไม่กระทบของเดิม สามารถเกิดขึ้นได้
- ถ้าเป็น breaking change ควรมีการเปลี่ยน `schemaVersion`

## ไฟล์อ้างอิงใน repo

- ตัวอย่าง summary: [docs/api-examples/sumary.json](/d:/ch7/election/docs/api-examples/sumary.json)
- ตัวอย่าง districts: [docs/api-examples/districts.json](/d:/ch7/election/docs/api-examples/districts.json)
