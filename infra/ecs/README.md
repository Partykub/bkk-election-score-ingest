# เอกสารส่งมอบ ECS Fargate

## โครงสร้างเป้าหมาย

ใช้หนึ่ง ECS Cluster และสาม Services:

| ECS Service | Containers ใน Task | Scaling | ALB Route |
| --- | --- | --- | --- |
| `election-gateway` | `hermes-supervisor`, `line-relay` | Request count / CPU | `/line/*`, `/webhook` |
| `election-ocr-worker` | `ocr-worker` | จำนวนข้อความใน SQS | ไม่มี |
| `election-results-api` | `results-api` | Request count / CPU | `/api/*` |

LINE Relay และ Hermes สามารถอยู่ Task เดียวกันได้เพราะทำงานเป็น Gateway ชุดเดียว
Hermes ต้องฟัง port `8647`, Relay ฟัง port `8646` และ Relay เรียก Hermes ผ่าน
`http://127.0.0.1:8647`

ไม่ควรรวม OCR Worker หรือ Results API เข้า Gateway เพราะมีการ Scale, Availability,
IAM และรอบ Deploy ต่างกัน

## Docker Images

Images ที่ Repository ต้อง Build:

```text
line-relay
ocr-worker
results-api
update-worker  # optional
```

Hermes ใช้ upstream image `nousresearch/hermes-agent` ทุก Production image ต้อง
Pin เป็น Version หรือ Digest ห้ามใช้ `latest`

```powershell
docker build -f hermes/supervisor/Dockerfile.relay -t line-relay .
docker build -f hermes/ocr_worker/Dockerfile -t ocr-worker .
docker build -f hermes/results_api/Dockerfile -t results-api .
```

## AWS Resources ที่ต้องมี

- ECS Cluster หนึ่งชุด
- ECS Services และ Task definitions สามชุด
- ALB พร้อม Target groups สำหรับ Relay `8646` และ Results API `8080`
- Cloud Map สำหรับให้ OCR Worker เรียก Hermes ภายใน
- S3 Bucket สำหรับ Images, Manifests, Drafts, Approvals และ Indexes
- FIFO SQS Queue และ Dead Letter Queue
- CloudWatch Log group แยกตาม Service
- Secrets Manager หรือ SSM Parameter Store
- Execution role สำหรับ ECR, Logs และ Secret injection
- Task role แยกตาม Service และเป็น Least privilege

Tasks ต้องอยู่ใน Private subnets และมีเพียง ALB ที่เปิด Public

## Configuration

ตัวแปรมาตรฐานอยู่ใน `.env.example`

- ค่าทั่วไปใส่ใน `environment`
- ข้อมูลลับใส่ใน `secrets`
- ECS ใช้ IAM Task Role แทน AWS Access Key
- Persist `/opt/data` ของ Hermes ด้วย EFS

## การ Migration

AWS ปัจจุบันมีสี่ Services:

```text
hermes-supervisor-task-service-mtkmjgeq
line-relay-svc
ocr-worker-svc-1
results-api-svc
```

ขั้นตอนลดเหลือสาม Services:

1. สร้าง `election-gateway` ที่มี Hermes และ Relay ใน Task เดียว
2. ตั้ง Hermes `LINE_PORT=8647`
3. ตั้ง Relay upstream เป็น `http://127.0.0.1:8647`
4. ต่อ LINE Target group เข้ากับ Relay port `8646`
5. ทดสอบ Health และส่งภาพจริง
6. Scale Hermes และ Relay ชุดเก่าเป็นศูนย์
7. คง OCR Worker และ Results API เป็น Services แยก

รายละเอียด Architecture, Security, IAM, CI/CD และ Runbook อยู่ใน
[`../../INFRASTRUCTURE.md`](../../INFRASTRUCTURE.md)

