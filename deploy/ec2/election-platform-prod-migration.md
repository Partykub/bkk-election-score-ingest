# election-platform-prod migration guide

คู่มือนี้ใช้สำหรับย้าย stack ปัจจุบันไปยัง EC2 ของทีม infra ชุดนี้:

- AWS account: `ch7-prod`
- EC2: `election-platform-prod`
- Instance ID: `i-06edd717a43f763b7`
- Access: AWS Systems Manager Session Manager
- Elastic IP: `54.254.164.95`
- S3 bucket: `ch7-static-bkkelection2569`
- SQS jobs: `election-ocr-jobs-prod.fifo`
- SQS DLQ: `election-ocr-dlq-prod.fifo`

## สิ่งที่ไฟล์ใน repo นี้เตรียมไว้ให้แล้ว

- env template: [election-platform-prod.env.example](./election-platform-prod.env.example)
- deploy script: [deploy.sh](./deploy.sh)
- proxy config: [Caddyfile](./Caddyfile)

## เป้าหมายของ env นี้

ชุดนี้ตั้งใจให้ `prod` ใช้ runtime, model, และ flow เหมือน `dev` env example มากที่สุด
โดยเปลี่ยนเฉพาะ AWS resources ให้ชี้ไปที่ `ch7-prod`:

- `PUBLIC_DOMAIN=54.254.164.95.sslip.io`
- `LINE_PUBLIC_URL=https://54.254.164.95.sslip.io`
- `ELECTION_S3_BUCKET=ch7-static-bkkelection2569`
- `OCR_QUEUE_URL=https://sqs.ap-southeast-1.amazonaws.com/511996147186/election-ocr-jobs-prod.fifo`
- `HERMES_PROVIDER=openrouter`
- `HERMES_MODEL=anthropic/claude-sonnet-4.6`
- `OPENAI_API_BASE=https://openrouter.ai/api/v1`
- `OLLAMA_API_BASE=`
- `OCR_WORKER_HERMES_MODEL=anthropic/claude-sonnet-4.6`
- `OCR_WORKER_MODEL_NAME=anthropic/claude-sonnet-4.6`

ถ้าภายหลังต้องการแยก provider หรือ model ของ prod ออกจาก dev ค่อยแก้ `.env` อีกครั้ง

## ค่าที่ต้องมีจากผู้ใช้หรือ infra

- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_CHANNEL_SECRET`
- `CADDY_EMAIL`
- secret สำหรับ `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD`
- secret สำหรับ `HERMES_DASHBOARD_BASIC_AUTH_SECRET`
- secret สำหรับ `API_SERVER_KEY`
- secret สำหรับ `OPENROUTER_API_KEY`

หมายเหตุ:

- ถ้าใช้ IAM instance role บน EC2 แล้ว ให้ปล่อย `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, และ `AWS_SESSION_TOKEN` ว่าง
- ถ้ายังไม่มี role ค่อยใช้ access key ชั่วคราว

## ขั้นตอน deploy แบบใช้ SSM parameter

1. เข้าเครื่องผ่าน SSM
2. ติดตั้ง Docker/Compose/Buildx ถ้าเครื่องยังใหม่ โดยใช้ [bootstrap.sh](./bootstrap.sh)
3. clone repo ไปที่ `/opt/election`
4. สร้าง runtime directory

```bash
sudo mkdir -p /opt/election-data/hermes-runtime
sudo chown -R $USER:$USER /opt/election-data
```

5. คัดลอก [election-platform-prod.env.example](./election-platform-prod.env.example) ไปกรอกค่าจริง
6. เก็บ `.env` เป็น SSM SecureString เช่น `/election/prod/compose-env`
7. deploy

```bash
cd /opt/election
chmod +x deploy/ec2/deploy.sh
ENV_PARAMETER=/election/prod/compose-env ./deploy/ec2/deploy.sh
```

EC2 role ต้องมีสิทธิ์ `ssm:GetParameter` และ `kms:Decrypt` สำหรับ parameter นี้

## คำสั่งตรวจหลัง deploy

```bash
cd /opt/election
docker compose --env-file .env ps
docker compose --env-file .env logs --tail=200 caddy line-relay ocr-worker results-api
curl -fsS "https://54.254.164.95.sslip.io/health"
curl -fsS "https://54.254.164.95.sslip.io/api/v1/governor-results/summary"
```

## สิทธิ์ขั้นต่ำที่ EC2 role ควรมี

- `s3:GetObject`
- `s3:PutObject`
- `s3:ListBucket`
- `sqs:SendMessage`
- `sqs:ReceiveMessage`
- `sqs:DeleteMessage`
- `sqs:GetQueueAttributes`
- `sqs:ChangeMessageVisibility`

## สิ่งที่ยังต้องถาม infra

- security group เปิด `80/443` จาก internet และไม่เปิด `8080/8642/8646/8647/9119` สู่ public ใช่หรือไม่
- จะใช้โดเมนจริงเมื่อไร เพื่อเปลี่ยนทั้ง `PUBLIC_DOMAIN` และ `LINE_PUBLIC_URL`
- EC2 role ใน account `ch7-prod` ผูกสิทธิ์กับ bucket `ch7-static-bkkelection2569` และ queue `election-ocr-jobs-prod.fifo` แล้วหรือยัง
