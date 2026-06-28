# Local Artifacts

โฟลเดอร์นี้เก็บ output ชั่วคราวจาก local dev / ops (log, API response dump, env snapshot)
**ไม่ commit** ลง git

ตัวอย่างที่มักวางไว้ที่นี่:

- `results-api-local*.log`
- `tmp-*-response.json`
- `.env.results-api-local*`
- `ssm-*.json`, `recovered-prod.env` — snapshot ops ที่มี secrets (อย่า commit)
