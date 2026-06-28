# สเปกการแมปข้อมูล Governor Results

เอกสารนี้อธิบายวิธีแปลงข้อมูลผลเลือกตั้งผู้ว่าฯ จาก payload ดิบของ endpoint ภายนอก
ให้เป็น payload สาธารณะรูปแบบเดียวกับที่ใช้ในโปรเจ็กต์นี้ ได้แก่

- `docs/api-examples/sumary.json`
- `docs/api-examples/districts.json`

แหล่งอ้างอิงของ payload ดิบในปัจจุบันคือ

- `docs/mockupdata/output_resulttest/69-governor-electiondata.json`
- `docs/mockupdata/output_zero/69-governor-electiondata.json`

## เป้าหมาย

แปลงข้อมูลจาก endpoint ภายนอกให้ออกมาเป็น JSON สาธารณะ 2 ไฟล์

1. `sumary.json`
2. `districts.json`

แนวทางที่แนะนำคือแบ่งการทำงานเป็นลำดับดังนี้

1. อ่าน payload ดิบจาก endpoint
2. แปลงเป็นโครงสร้างกลาง
3. เติม metadata ของผู้สมัครและเขต
4. render ออกเป็น payload สาธารณะ
5. ตรวจสอบความถูกต้องและสะสม warning/error

## ขอบเขต

สเปกนี้ครอบคลุมเฉพาะข้อมูลผลเลือกตั้งผู้ว่าฯ เท่านั้น

ไม่ครอบคลุม

- การแปลง payload ของ BMC
- flow เดิมของ OCR/approval aggregation
- เรื่อง worker, schedule, หรือการเขียน S3

## รูปแบบข้อมูลดิบที่อ้างอิง

โครงสร้างดิบที่คาดหวังมีลักษณะประมาณนี้

```json
{
  "type": "LIVE",
  "total": {
    "eligiblePopulation": 4434721,
    "totalVotes": 2313798,
    "badVotes": 23246,
    "noVotes": 409,
    "goodVote": 2290143,
    "progress": 91.93,
    "pollingUnits": {
      "total": 6628,
      "reported": 6093
    },
    "result": [
      { "candidateId": "1", "count": 1941 }
    ]
  },
  "districts": [
    {
      "name": "...",
      "voting": {
        "eligiblePopulation": 31233,
        "totalVotes": 1480,
        "badVotes": 13,
        "noVotes": 0,
        "goodVote": 1467,
        "progress": 6.45,
        "result": [
          { "candidateId": "1", "count": 1 }
        ],
        "pollingUnits": {
          "total": 62,
          "reported": 4
        }
      }
    }
  ],
  "lastUpdatedAt": "2026-06-23T00:00:00Z"
}
```

## รูปแบบปลายทางที่ต้องได้

payload ปลายทางมี 2 แบบ

- summary payload
  - `schemaVersion`
  - `resource`
  - `pageMeta`
  - `summary`
  - `candidates`
  - `dataQuality`
  - `dataInterpretation`

- districts payload
  - `schemaVersion`
  - `resource`
  - `generatedAt`
  - `constituencies`

หมายเหตุ

- `docs/api-examples/districts.json` ตอนนี้ควรถือเป็นตัวอย่าง shape มากกว่าไฟล์ JSON
  ที่ parse ตรง ๆ ได้ เพราะมี formatting artifact อยู่
- contract จริงของ payload ควรยึดพฤติกรรม serializer ใน
  `hermes/results_api/app.py`

## โครงสร้างกลางที่แนะนำ

ก่อน render เป็น payload สาธารณะ ควรแปลงข้อมูลดิบให้เป็น model กลางก่อน

ตัวอย่าง shape

```json
{
  "electionType": "governor",
  "resultStatus": "LIVE_COUNT",
  "lastUpdatedAt": "2026-06-23T00:00:00Z",
  "summary": {
    "eligibleVoters": 4434721,
    "voterTurnout": 2313798,
    "validBallots": 2290143,
    "invalidBallots": 23246,
    "abstainedBallots": 409,
    "countedPercentage": 91.93,
    "countedUnits": 6093,
    "totalUnits": 6628
  },
  "candidateResults": [
    {
      "sourceCandidateId": "1",
      "voteCount": 1941
    }
  ],
  "districts": [
    {
      "sourceDistrictName": "...",
      "areaId": "...",
      "number": 1,
      "name": "...",
      "eligibleVoters": 31233,
      "voterTurnout": 1480,
      "validBallots": 1467,
      "invalidBallots": 13,
      "abstainedBallots": 0,
      "countedPercentage": 6.45,
      "countedUnits": 4,
      "totalUnits": 62,
      "candidateResults": [
        {
          "sourceCandidateId": "1",
          "voteCount": 1
        }
      ]
    }
  ]
}
```

## การแมปจาก Raw ไปเป็น Model กลาง

### ระดับ Summary

| ฟิลด์จาก raw | ฟิลด์ปลายทางใน model กลาง | กติกา |
| --- | --- | --- |
| `type` | `resultStatus` | map ผ่าน lookup table |
| `lastUpdatedAt` | `lastUpdatedAt` | ส่งผ่านตรง |
| `total.eligiblePopulation` | `summary.eligibleVoters` | จำนวนเต็ม |
| `total.totalVotes` | `summary.voterTurnout` | จำนวนเต็ม |
| `total.goodVote` | `summary.validBallots` | จำนวนเต็ม |
| `total.badVotes` | `summary.invalidBallots` | จำนวนเต็ม |
| `total.noVotes` | `summary.abstainedBallots` | จำนวนเต็ม |
| `total.progress` | `summary.countedPercentage` | ค่าร้อยละ |
| `total.pollingUnits.reported` | `summary.countedUnits` | จำนวนเต็ม |
| `total.pollingUnits.total` | `summary.totalUnits` | จำนวนเต็ม |
| `total.result[*]` | `candidateResults[*]` | เก็บ `candidateId` และ `count` |

### ระดับเขต

| ฟิลด์จาก raw | ฟิลด์ปลายทางใน model กลาง | กติกา |
| --- | --- | --- |
| `districts[*].name` | `sourceDistrictName` | เก็บชื่อดิบไว้ |
| `districts[*].name` | `areaId`, `number`, `name` | หาเพิ่มจาก district master |
| `districts[*].voting.eligiblePopulation` | `eligibleVoters` | จำนวนเต็ม |
| `districts[*].voting.totalVotes` | `voterTurnout` | จำนวนเต็ม |
| `districts[*].voting.goodVote` | `validBallots` | จำนวนเต็ม |
| `districts[*].voting.badVotes` | `invalidBallots` | จำนวนเต็ม |
| `districts[*].voting.noVotes` | `abstainedBallots` | จำนวนเต็ม |
| `districts[*].voting.progress` | `countedPercentage` | ค่าร้อยละ |
| `districts[*].voting.pollingUnits.reported` | `countedUnits` | จำนวนเต็ม |
| `districts[*].voting.pollingUnits.total` | `totalUnits` | จำนวนเต็ม |
| `districts[*].voting.result[*].candidateId` | `candidateResults[*].sourceCandidateId` | เก็บ id ดิบ |
| `districts[*].voting.result[*].count` | `candidateResults[*].voteCount` | จำนวนเต็ม |

## การแมปสถานะ

ตารางแนะนำสำหรับ map ค่า `type`

| ค่า raw `type` | ค่า `resultStatus` สำหรับ public payload |
| --- | --- |
| `LIVE` | `LIVE_COUNT` |
| `FINAL` | `FINAL` |
| อื่น ๆ | ใช้ fallback เป็น `LIVE_COUNT` และใส่ warning |

ถ้า upstream เพิ่มสถานะใหม่ ควรเพิ่มลงตารางนี้ตรง ๆ

## การเติม Metadata

ข้อมูลดิบยังไม่พอสำหรับ render payload สาธารณะ จำเป็นต้องเติมข้อมูลเพิ่ม

### Metadata ของผู้สมัครที่ต้องเติม

- `candidateId`
- `candidateNumber`
- `name`
- `candidateSrc`
- `backgroundSrc`
- `color`
- `party.id`
- `party.name`
- `party.color`
- `party.logoUrl`

### Metadata ของเขตที่ต้องเติม

- `areaId`
- `number`
- canonical district `name`

### Candidate Lookup

ลำดับที่แนะนำ

1. map raw `candidateId` ไปเป็น candidate number
2. map candidate number ไปเป็น candidate metadata

ถ้า raw `candidateId` เป็นเลข string อยู่แล้ว ควร normalize เป็น integer ตั้งแต่ต้น

### District Lookup

แนะนำให้ lookup ด้วยชื่อเขตที่ normalize แล้ว

กติกา normalize ที่แนะนำ

- trim ซ้ายขวา
- รวมช่องว่างซ้ำ
- เผื่อจุด hook สำหรับแก้ encoding ถ้าข้อมูลจริงมีปัญหา

## การ Render เป็น `sumary.json`

### ฟิลด์ระดับบน

| ฟิลด์ | แหล่งข้อมูล |
| --- | --- |
| `schemaVersion` | ค่าคงที่ `"1.0"` |
| `resource` | ค่าคงที่ `"governor-results"` |

### `pageMeta`

| ฟิลด์ | แหล่งข้อมูล | กติกา |
| --- | --- | --- |
| `electionId` | config | ต้องมี |
| `title` | config หรือ override | ต้องมี |
| `resultStatus` | จาก model กลาง | ต้องมี |
| `generatedAt` | เวลาตอน transform | ISO 8601 |

### `summary`

| ฟิลด์ | แหล่งข้อมูล | กติกา |
| --- | --- | --- |
| `countedUnits` | model กลาง | จำนวนเต็ม |
| `totalUnits` | model กลาง | จำนวนเต็ม |
| `countedPercentage` | model กลาง | ใช้ค่าจาก source ก่อน |
| `eligibleVoters` | model กลาง | จำนวนเต็ม |
| `voterTurnout` | model กลาง | จำนวนเต็ม |
| `voterTurnoutPercentage` | คำนวณ | `voterTurnout / eligibleVoters * 100` |
| `validBallots` | model กลาง | จำนวนเต็ม |
| `invalidBallots` | model กลาง | จำนวนเต็ม |
| `abstainedBallots` | model กลาง | จำนวนเต็ม |
| `countedBallots` | คำนวณ | `valid + invalid + abstained` |
| `countedBallotsPercentage` | คำนวณ | `countedBallots / voterTurnout * 100` |
| `validBallotsPercentage` | คำนวณ | `validBallots / voterTurnout * 100` |
| `invalidBallotsPercentage` | คำนวณ | `invalidBallots / voterTurnout * 100` |
| `abstainedBallotsPercentage` | คำนวณ | `abstainedBallots / voterTurnout * 100` |
| `lastUpdatedAt` | model กลาง | ISO 8601 |

### `candidates`

สร้างจากผลรวมคะแนนระดับ summary แล้วเติม metadata

ฟิลด์ต่อคน

- `candidateId`
- `candidateNumber`
- `name`
- `candidateSrc`
- `color`
- `voteCount`
- `votePercentage`
- `rank`
- `isLeading`
- `backgroundSrc`
- `party`

กติกา

- sort จาก `voteCount` มากไปน้อย
- ถ้าคะแนนเท่ากันให้เรียงตาม `candidateNumber`
- `votePercentage = voteCount / validBallots * 100`
- `rank` เริ่มที่ 1
- `isLeading = true` เฉพาะอันดับ 1

### `dataQuality`

| ฟิลด์ | กติกา |
| --- | --- |
| `isComplete` | `countedUnits >= totalUnits` |
| `isDelayed` | เทียบเวลาปัจจุบันกับ `lastUpdatedAt` ตาม threshold |
| `warnings` | สะสม warning จาก validation |

### `dataInterpretation`

ค่าที่แนะนำ

```json
{
  "mode": "external_snapshot",
  "description": "Use the latest external snapshot provided by the upstream endpoint."
}
```

## การ Render เป็น `districts.json`

### ฟิลด์ระดับบน

| ฟิลด์ | แหล่งข้อมูล |
| --- | --- |
| `schemaVersion` | ค่าคงที่ `"1.0"` |
| `resource` | ค่าคงที่ `"constituency-bangkok"` |
| `generatedAt` | เวลาตอน transform |

### `constituencies[*]`

| ฟิลด์ | แหล่งข้อมูล | กติกา |
| --- | --- | --- |
| `areaId` | district lookup | ต้องมี |
| `number` | district lookup | ต้องมี |
| `name` | district lookup | ใช้ชื่อ canonical |
| `leadingCandidateId` | คำนวณ | ผู้สมัครอันดับ 1 |
| `countedPercentage` | model กลาง | ใช้ค่าจาก source ก่อน |
| `eligibleVoters` | model กลาง | จำนวนเต็ม |
| `voterTurnout` | model กลาง | จำนวนเต็ม |
| `voterTurnoutPercentage` | คำนวณ | `voterTurnout / eligibleVoters * 100` |
| `validBallots` | model กลาง | จำนวนเต็ม |
| `invalidBallots` | model กลาง | จำนวนเต็ม |
| `abstainedBallots` | model กลาง | จำนวนเต็ม |
| `countedBallots` | คำนวณ | `valid + invalid + abstained` |
| `countedBallotsPercentage` | คำนวณ | `countedBallots / voterTurnout * 100` |
| `lastUpdatedAt` | model กลาง | ถ้ามี timestamp ระดับเขตในอนาคตค่อยใช้ |
| `candidates` | model กลาง | เติม metadata และจัด rank |

### `constituencies[*].candidates[*]`

ใช้กติกาเดียวกับ summary candidate

- `candidateId`
- `candidateNumber`
- `name`
- `candidateSrc`
- `color`
- `voteCount`
- `votePercentage`
- `rank`
- `isLeading`
- `backgroundSrc`
- `party`

กติกา `votePercentage`

- `votePercentage = voteCount / district.validBallots * 100`

## กติกาการตรวจสอบข้อมูล

### Hard Fail

ให้ถือว่าผิดและหยุด ถ้าเจอกรณีนี้

- ไม่มี `total`
- ไม่มี `districts`
- `total.result` ไม่ใช่ list
- district ไม่มี `voting`
- มี district จำนวนมากที่ match กับ district master ไม่ได้

### Warning

ให้เดินต่อได้แต่สะสม warning ถ้าเจอกรณีนี้

- map ผู้สมัครเข้า metadata ไม่ได้บางคน
- จำนวนเขตไม่ตรงกับจำนวนเขตที่ควรเป็น
- `goodVote + badVotes + noVotes != totalVotes`
- `pollingUnits.reported > pollingUnits.total`
- timestamp หายหรือ format ไม่ถูก
- `type` ไม่รู้จักและต้อง fallback

## กรณีพิเศษ

### Zero หรือผลลัพธ์ว่าง

fixture `output_zero` บอกว่า upstream อาจส่ง

- `total.result` ว่าง
- district บางเขตมี candidate result ไม่ครบ

แนวทาง

- ยังต้อง render `sumary.json` ได้
- อนุญาตให้ `candidates` เป็น list ว่างได้
- อนุญาตให้ candidate list ของบางเขตว่างได้
- ใส่ warning ถ้า source ว่างในจุดที่ไม่คาดหวัง

### ปัญหา Encoding

mockup ใน repo มีอาการตัวอักษรไทยเพี้ยนบางส่วน

แนวทาง

- อย่า hardcode วิธีแก้จาก fixture อย่างเดียว
- ยืนยัน encoding ของ endpoint จริงก่อน
- เตรียม hook สำหรับ text normalization เผื่อใช้ภายหลัง

### BMC Payload

ไฟล์ BMC ใช้ semantics และ candidate identifier คนละแบบ

แนวทาง

- อย่าใช้ adapter ตัวเดียวกับ governor
- ถ้าต้องรองรับภายหลัง ให้ทำ adapter แยก

## การแบ่งส่วน implementation ที่แนะนำ

ฟังก์ชันหลักที่แนะนำ

1. `parse_raw_governor_payload(raw) -> NormalizedGovernorModel`
2. `validate_normalized_governor(model) -> warnings/errors`
3. `render_governor_summary(model, candidate_catalog, config) -> dict`
4. `render_governor_districts(model, candidate_catalog, district_catalog, config) -> dict`

helper ที่ควรมี

- `normalize_district_name(value: str) -> str`
- `map_raw_status(raw_type: str) -> str`
- `derive_percentages(...)`
- `rank_candidates(...)`

## นโยบายที่ควรล็อกก่อนลงมือ

### ตัวหารของเปอร์เซ็นต์

แนะนำดังนี้

- เปอร์เซ็นต์คะแนนผู้สมัคร ใช้ `validBallots`
- เปอร์เซ็นต์ turnout ใช้ `eligibleVoters`

### แหล่งของ countedPercentage

ลำดับที่แนะนำ

1. ใช้ `progress` จาก upstream ก่อน
2. ถ้าไม่มี ค่อยคำนวณจาก `reported / total * 100`

### กรณี metadata ผู้สมัครหาไม่เจอ

แนะนำ

- อย่า hard fail ทันที
- ใส่ warning
- อนุญาตให้ field บางตัวเป็น null ได้ ถ้า consumer รับได้

ถ้า downstream ต้อง strict มาก ค่อยยกระดับเป็น hard fail

## ชุดทดสอบที่ควรมี

fixture อ้างอิงหลัก

- `docs/mockupdata/output_resulttest/69-governor-electiondata.json`
- `docs/mockupdata/output_zero/69-governor-electiondata.json`

test ขั้นต่ำที่ควรมี

1. payload แบบ live แปลงเป็น summary ได้ถูกต้อง
2. payload แบบ live แปลงเป็น districts ได้ถูกต้อง
3. payload แบบ zero-result ยัง render public payload ได้
4. candidate ranking คงที่
5. district lookup fail แล้วได้ warning
6. arithmetic ไม่ตรงแล้วได้ warning
7. `type` ที่ไม่รู้จัก fallback พร้อม warning

## สิ่งที่เอกสารนี้ยังไม่ตัดสิน

สเปกนี้ยังไม่ตัดสินเรื่องต่อไปนี้

- จะใช้ worker แบบไหน
- poll ทุกกี่วินาที
- จะตั้งชื่อ key ใน S3 ว่าอะไร
- จะ deploy บน EC2 อย่างไร
- จะให้ public payload ชุดนี้แทน flow OCR เดิมหรือไม่

ประเด็นเหล่านี้ควรคุยแยกหลังจากยอมรับ mapping contract นี้แล้ว
