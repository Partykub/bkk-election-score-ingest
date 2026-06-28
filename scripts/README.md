# Scripts

สคริปต์ช่วย deploy / ทดสอบ local — ไม่ใช่ runtime ของ Docker Compose production

## โครงสร้าง

| โฟลเดอร์ | ใช้เมื่อ |
| --- | --- |
| [`deploy/`](./deploy/) | อัปโหลด artifact ไป S3 แล้ว deploy prod ผ่าน SSM |
| [`test/`](./test/) | smoke test การ fetch / monitor แบบ local |
| [`local/`](./local/) | รัน service บนเครื่อง dev |
| [`dev-test-env.sh`](./dev-test-env.sh) | helper env สำหรับ dev test |

## Deploy (prod)

```powershell
# LINE area name resolution (line-relay + ocr-worker)
.\scripts\deploy\deploy-area-name-resolution.ps1

# BKK external fetch (results-api + http_fetch)
.\scripts\deploy\deploy-bkk-http-fetch.ps1
```

SSM JSON ที่สคริปต์เรียกอยู่ที่ [`deploy/ssm/`](../deploy/ssm/README.md)

## Local dev

```powershell
.\scripts\local\run-results-api-local-mock.ps1
.\scripts\test\test-bkk-fetch.ps1
.\scripts\test\test-monitor-fetch-local.ps1 -BaseUrl http://127.0.0.1:18083
```

Output / env snapshot ชั่วคราวเก็บที่ [`deploy/local-artifacts/`](../deploy/local-artifacts/README.md) (ไม่ commit)
