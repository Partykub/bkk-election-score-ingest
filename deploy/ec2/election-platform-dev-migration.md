# election-platform-dev migration guide

คู่มือนี้ใช้สำหรับย้าย stack ปัจจุบันไปยัง EC2 ของทีม infra ชุดนี้:

- AWS account: `ch7-dev`
- EC2: `election-platform-dev`
- Instance ID: `i-00dc21912d495beed`
- Access: AWS Systems Manager Session Manager
- Elastic IP: `18.142.219.248`
- S3 bucket: `bkk-election-images-dev`
- SQS jobs: `election-ocr-jobs-dev.fifo`
- SQS DLQ: `election-ocr-dlq-dev.fifo`

## สิ่งที่ไฟล์ใน repo นี้เตรียมไว้ให้แล้ว

- env template: [election-platform-dev.env.example](./election-platform-dev.env.example)
- deploy script: [deploy.sh](./deploy.sh)
- proxy config: [Caddyfile](./Caddyfile)

## ค่าที่ต้องมีจากผู้ใช้หรือ infra

- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_CHANNEL_SECRET`
- `CADDY_EMAIL` (ใช้เมลเดิมได้)
- ค่า secret สำหรับ `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD`
- ค่า secret สำหรับ `HERMES_DASHBOARD_BASIC_AUTH_SECRET`
- ค่า secret สำหรับ `API_SERVER_KEY`
- Access key ที่มีสิทธิ์กับ S3/SQS ชุดนี้

ถ้ายังไม่มี domain จริง ให้ใช้ `18.142.219.248.sslip.io` ชั่วคราวก่อน

## ขั้นตอน deploy แบบใช้ .env บนเครื่อง + Access Key

1. เข้าเครื่องผ่าน SSM
2. ติดตั้ง Docker/Compose/Buildx ถ้าเครื่องยังใหม่ โดยใช้ [bootstrap.sh](./bootstrap.sh)
3. clone repo ไปที่ `/opt/election`
4. สร้าง runtime directory

```bash
sudo mkdir -p /opt/election-data/hermes-runtime
sudo chown -R $USER:$USER /opt/election-data
```

5. คัดลอก [election-platform-dev.env.example](./election-platform-dev.env.example) ไปเป็น `/opt/election/.env` แล้วกรอกค่าจริง
6. ใส่ `AWS_ACCESS_KEY_ID` และ `AWS_SECRET_ACCESS_KEY` ลงใน `.env`
7. ตรวจสิทธิ์ access key ด้วยคำสั่งในหัวข้อถัดไป
8. deploy

```bash
cd /opt/election
chmod +x deploy/ec2/deploy.sh
./deploy/ec2/deploy.sh
```

## ขั้นตอน deploy แบบใช้ SSM Parameter

ถ้าต้องการไม่เก็บ `.env` ไว้บน disk ถาวร ให้เก็บค่าเป็น SecureString เช่น `/election/dev/compose-env`

```bash
cd /opt/election
chmod +x deploy/ec2/deploy.sh
ENV_PARAMETER=/election/dev/compose-env ./deploy/ec2/deploy.sh
```

EC2 role ต้องมีสิทธิ์ `ssm:GetParameter` และ `kms:Decrypt` สำหรับ parameter นี้

## คำสั่งเช็กสิทธิ์ access key แบบไม่เขียนข้อมูลจริง

ถ้ามี AWS CLI บนเครื่อง EC2 แล้ว ให้ export key ชั่วคราวและรันคำสั่งต่อไปนี้:

```bash
export AWS_ACCESS_KEY_ID=YOUR_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY=YOUR_SECRET_ACCESS_KEY
export AWS_DEFAULT_REGION=ap-southeast-1

aws sts get-caller-identity
aws s3api list-objects-v2 --bucket bkk-election-images-dev --max-items 1
aws sqs get-queue-attributes \
	--queue-url https://sqs.ap-southeast-1.amazonaws.com/119557435758/election-ocr-jobs-dev.fifo \
	--attribute-names QueueArn VisibilityTimeout RedrivePolicy
```

ถ้าสามคำสั่งนี้ผ่าน แปลว่า key ใช้งานพื้นฐานกับ account, S3 bucket และ SQS queue ได้แล้ว

ถ้าต้องการเช็กสิทธิ์ write แบบตรงขึ้นอีกระดับ ให้เขียน object ทดสอบขนาดเล็กแล้วลบทิ้ง:

```bash
printf '{"ok":true}\n' >/tmp/election-access-check.json
aws s3 cp /tmp/election-access-check.json s3://bkk-election-images-dev/access-check/election-access-check.json
aws s3 rm s3://bkk-election-images-dev/access-check/election-access-check.json
```

ถ้า upload และลบไฟล์ทดสอบผ่าน แปลว่า key มี `PutObject` และใช้งานกับ bucket นี้ได้จริง

## SSO หรือ Access Key

- `Access Key` ใช้งานง่ายและตรงที่สุดสำหรับ Docker Compose ชุดนี้ เพราะส่งค่าเข้า container ได้ตรงผ่าน `.env`
- `AWS SSO` เหมาะกับเครื่อง developer มากกว่า เพราะต้องพึ่ง session cache และการ refresh token นอก container
- ถ้าจะใช้ SSO บน EC2 จริง ต้องจัดการ credential bootstrap เพิ่ม และไม่เหมาะกับ service ยาวใน Docker เท่า access key
- ถ้า infra ยังไม่ได้ให้ IAM role บน EC2 มา `Access Key` คือทางเลือกที่ใช้งานได้ทันที

## ค่าที่สำคัญใน env ชุดนี้

- `PUBLIC_DOMAIN=18.142.219.248.sslip.io`
- `LINE_PUBLIC_URL=https://18.142.219.248.sslip.io`
- `ELECTION_S3_BUCKET=bkk-election-images-dev`
- `OCR_QUEUE_URL=https://sqs.ap-southeast-1.amazonaws.com/119557435758/election-ocr-jobs-dev.fifo`
- `HERMES_MODEL=gemma4:26b`
- `OLLAMA_API_BASE=https://stranger-lanky-outmost.ngrok-free.dev/v1`
- `OCR_WORKER_HERMES_MODEL=gemma4:26b`
- `OCR_WORKER_MODEL_NAME=gemma4:26b`

ระบบนี้ไม่ต้องตั้ง DLQ ใน env เพราะแอปใช้ jobs queue โดยตรง ส่วน DLQ ควรผูกด้วย SQS redrive policy ที่ AWS

## การตรวจหลัง deploy

```bash
cd /opt/election
docker compose --env-file .env ps
docker compose --env-file .env logs --tail=200 caddy line-relay ocr-worker results-api
curl -fsS "https://18.142.219.248.sslip.io/health"
curl -fsS "https://18.142.219.248.sslip.io/api/v1/governor-results/summary"
```

## สิทธิ์ขั้นต่ำที่ access key ควรมี

- `s3:GetObject`
- `s3:PutObject`
- `s3:ListBucket`
- `sqs:SendMessage`
- `sqs:ReceiveMessage`
- `sqs:DeleteMessage`
- `sqs:GetQueueAttributes`

`sqs:ChangeMessageVisibility` ยังไม่เห็นถูกเรียกโดยตรงจาก code path ปัจจุบัน แต่ให้มีไว้ได้ถ้า infra อยากเผื่อ worker behavior ภายหน้า

## สิ่งที่ยังต้องถาม infra ถ้าจะใช้ Access Key ต่อ

- access key นี้อยู่ใน account `ch7-dev` หรือ cross-account
- key นี้ผูก policy ครอบคลุม bucket `bkk-election-images-dev` และ queue `election-ocr-jobs-dev.fifo` จริงหรือไม่
- security group เปิด `80/443` จาก internet และไม่เปิด `8080/8642/8646/8647/9119` สู่ public ใช่หรือไม่

## หมายเหตุ

- ชุดนี้ตั้งใจเริ่มใหม่บน bucket dev ไม่ต้องย้ายข้อมูลจาก bucket เก่า
- เมื่อ infra ให้ domain จริงแล้ว ให้เปลี่ยนทั้ง `PUBLIC_DOMAIN` และ `LINE_PUBLIC_URL` ให้เป็น domain เดียวกัน