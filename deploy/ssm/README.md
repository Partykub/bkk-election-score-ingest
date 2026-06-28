# SSM Run Command Scripts

JSON payloads สำหรับ `aws ssm send-command` บน prod EC2

- **Instance:** `i-06edd717a43f763b7`
- **Document:** `AWS-RunShellScript`
- **Repo path บนเครื่อง:** `/opt/election`

ไฟล์เหล่านี้เป็น **one-off ops scripts** จากช่วง deploy/debug จริง
เก็บไว้เป็นอ้างอิง ไม่ใช่ pipeline หลัก — deploy ปกติใช้ `deploy/ec2/deploy.sh`

## โครงสร้าง

| โฟลเดอร์ | ใช้เมื่อ |
| --- | --- |
| [`deploy/`](./deploy/) | deploy โค้ดหรือ config ขึ้น prod |
| [`verify/`](./verify/) | ตรวจหลัง deploy |
| [`inspect/`](./inspect/) | ดู env, log, container, S3 |
| [`fix/`](./fix/) | แก้ config/runtime เฉพาะจุด |
| [`ops/`](./ops/) | sync, redeploy, dump, export ชั่วคราว |

อย่าสร้างไฟล์ `tmp-ssm-*.json` ที่ root อีก — วาง script ใหม่ในโฟลเดอร์ที่เหมาะสม

## วิธีรัน

```bash
aws ssm send-command \
  --cli-input-json file://deploy/ssm/deploy/intake-correction-parsing.json \
  --output json
```

ดูผล:

```bash
aws ssm get-command-invocation \
  --command-id <id> \
  --instance-id i-06edd717a43f763b7
```

## Deploy scripts ที่ยังอ้างอิงบ่อย

| ไฟล์ | หมายเหตุ |
| --- | --- |
| [`deploy/intake-correction-parsing.json`](./deploy/intake-correction-parsing.json) | LINE text correction parsing |
| [`deploy/area-name-resolution.json`](./deploy/area-name-resolution.json) | LINE area name → id + district label in approval |
| [`deploy/bkk-http-fetch.json`](./deploy/bkk-http-fetch.json) | BKK external fetch (browser headers + cloudscraper) |
| [`deploy/governor-public-source-switch.json`](./deploy/governor-public-source-switch.json) | LINE/กทม public source switch |
| [`verify/intake-correction-parsing.json`](./verify/intake-correction-parsing.json) | smoke test หลัง deploy intake |
| [`verify/area-name-resolution.json`](./verify/area-name-resolution.json) | smoke test หลัง deploy area name resolution |
| [`verify/bkk-http-fetch.json`](./verify/bkk-http-fetch.json) | smoke test หลัง deploy BKK http fetch |
| [`inspect/latest-line-image.json`](./inspect/latest-line-image.json) | ดู manifest/draft รูป LINE ล่าสุดบน prod |

PowerShell wrappers: [`scripts/deploy/`](../scripts/deploy/) (ดู [`scripts/README.md`](../scripts/README.md))

สคริปต์เก่าใน `inspect/` และ `ops/` อาจอ้าง path หรือ timestamp เฉพาะช่วงนั้น
อ่าน `Comment` และ `commands` ใน JSON ก่อนรันซ้ำ
