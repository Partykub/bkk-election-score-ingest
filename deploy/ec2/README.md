# Deploy บน EC2 ด้วย Docker Compose

แนวทางนี้เหมาะกับช่วงเริ่มต้นหรือ workload ขนาดเล็กถึงกลาง โดยลดจำนวน AWS
services และค่าใช้จ่ายคงที่ลง

## สิ่งที่ใช้

- EC2 หนึ่งเครื่องพร้อม public subnet และ Elastic IP
- EBS volume ของ EC2
- S3 bucket เดิม
- SQS queue และ DLQ เดิม
- Route 53 หรือ DNS provider ที่ใช้อยู่
- Docker Engine, Docker Compose plugin, Buildx และ Git

ไม่ต้องใช้ ECS, ECR, ALB, API Gateway, NAT Gateway, EFS หรือ Cloud Map
ECR ยังสามารถเพิ่มภายหลังได้หากต้องการ build image จาก CI

## ขนาดเครื่องเริ่มต้น

เริ่มจาก x86 instance ที่มีอย่างน้อย 2 vCPU และ RAM 4 GB เพราะเครื่องนี้รัน
Hermes, relay, worker, API และ reverse proxy พร้อมกัน ควรดู CPU/RAM จริงก่อน
ลดหรือเพิ่มขนาดเครื่อง

## Network

Security Group inbound:

| Port | Source | ใช้สำหรับ |
| --- | --- | --- |
| `80` | `0.0.0.0/0`, `::/0` | TLS challenge และ redirect |
| `443` | `0.0.0.0/0`, `::/0` | LINE webhook และ Results API |

ห้ามเปิด `8080`, `8642`, `8646`, `8647` และ `9119` สู่ Internet
เครื่อง production ปัจจุบันใช้ AWS Systems Manager Session Manager แทน SSH
จึงไม่ต้องเปิด port `22`

## IAM Instance Profile

ผูก IAM role กับ EC2 แทนการใส่ `AWS_ACCESS_KEY_ID` และ
`AWS_SECRET_ACCESS_KEY` ใน `.env` โดยให้สิทธิ์เท่าที่จำเป็น:

- อ่าน/เขียนเฉพาะ prefix ที่ระบบใช้ใน S3 bucket
- รับ/ลบ/เปลี่ยน visibility ของ OCR SQS queue
- ส่ง message เข้า OCR queue และ DLQ ตามหน้าที่ของระบบ

ใน production ให้ปล่อยค่า `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` และ
`AWS_SESSION_TOKEN` ว่าง เพื่อให้ AWS SDK ใช้ Instance Profile

## ติดตั้ง

1. สร้าง DNS A record ของ domain ให้ชี้ไป Elastic IP
2. clone repository ไปที่ `/opt/election`
3. เก็บ production `.env` เป็น SSM SecureString เช่น `/election/compose-env`
4. ตั้ง `HERMES_RUNTIME_DIR=/opt/election-data/hermes-runtime`
5. ตั้ง `PUBLIC_DOMAIN`, `CADDY_EMAIL` และ `LINE_PUBLIC_URL`
  `CADDY_EMAIL` ต้องเป็น email จริง เพราะ ACME จะ reject placeholder เช่น `example.com`
6. deploy:

```bash
cd /opt/election
chmod +x deploy/ec2/deploy.sh
ENV_PARAMETER=/election/compose-env ./deploy/ec2/deploy.sh
```

ใช้ `deploy/ec2/bootstrap.sh` ติดตั้ง Docker, Compose, Buildx และ swap
บน Amazon Linux 2023 เมื่อสร้างเครื่องใหม่

ถ้าจะย้ายไปเครื่อง `election-platform-dev` ของทีม infra ให้เริ่มจาก
[`deploy/ec2/election-platform-dev.env.example`](./election-platform-dev.env.example)
และคู่มือ [`deploy/ec2/election-platform-dev-migration.md`](./election-platform-dev-migration.md)

ถ้าจะย้ายไปเครื่อง `election-platform-prod` แต่ต้องการให้ runtime เหมือน `dev`
และเปลี่ยนเฉพาะ AWS resources เป็นของ `prod` ให้เริ่มจาก
[`deploy/ec2/election-platform-prod.env.example`](./election-platform-prod.env.example)
และคู่มือ [`deploy/ec2/election-platform-prod-migration.md`](./election-platform-prod-migration.md)

ถ้าจะเตรียม env ไว้ล่วงหน้าสำหรับหลาย environment ให้ใช้ไฟล์ตัวอย่าง:
- [`deploy/ec2/election-platform-dev.env.example`](./election-platform-dev.env.example)
- [`deploy/ec2/election-platform-prod.env.example`](./election-platform-prod.env.example)

ค่าแนะนำล่าสุดสำหรับ production-like layout คือ:

```dotenv
ELECTION_S3_PREFIX=api-data/score
RESULTS_API_CANDIDATES_MANIFEST_URL=s3://<bucket>/api-data/candidates/manifest.json
RESULTS_API_CANDIDATES_FEATURED_URL=s3://<bucket>/api-data/candidates/featured.json
RESULTS_API_PARTIES_URL=s3://<bucket>/api-data/master-data/parties.json
RESULTS_API_DISTRICTS_URL=s3://<bucket>/api-data/master-data/election-areas-bangkok.json
GOVERNOR_RESULTS_PREFIX=api-data/governor-results
RESULTS_API_STATIC_RESULTS_PREFIX=api-data/governor-results
STATIC_RESULTS_PREFIX=api-data/governor-results
STATIC_RESULTS_S3_BUCKET=<bucket>
```

โดย layout นี้แยกชัดเจนว่า:

- chat / OCR / draft / approval / update manifests อยู่ใต้ `api-data/score/`
- approved summary export อยู่ใต้ `api-data/governor-results/`
- candidate-to-party mapping can come from `partyId` / `partyName` in candidate data, resolved via `RESULTS_API_PARTIES_URL`

`deploy.sh` จะ sync ค่า `OLLAMA_API_BASE` หรือ `OPENAI_API_BASE` จาก `.env` ไปยัง
`HERMES_RUNTIME_DIR/config.yaml` ก่อน restart container เพื่อป้องกัน runtime
เก่าชี้กลับไปที่ `host.docker.internal`

ถ้าต้องการสลับ provider ผ่าน `.env` ให้ตรงกับ runtime มากขึ้น `deploy.sh` จะ sync
ค่า `HERMES_PROVIDER`, `HERMES_MODEL`, `AWS_REGION`/`AWS_DEFAULT_REGION` และ
`OLLAMA_API_BASE`/`OPENAI_API_BASE` ลงใน runtime `config.yaml` ด้วย

ค่าเริ่มต้นของ env ตัวอย่างฝั่ง EC2 ใช้ OpenRouter + Claude Sonnet 4.6:

```dotenv
HERMES_PROVIDER=openrouter
HERMES_MODEL=anthropic/claude-sonnet-4.6
OPENAI_API_BASE=https://openrouter.ai/api/v1
OPENROUTER_API_KEY=CHANGE_ME_OPENROUTER_API_KEY
OLLAMA_API_BASE=
OPENAI_API_KEY=
MODEL_API_KEY=
```

ถ้าต้องการย้ายจาก OpenRouter ไปใช้ Ollama ที่ปล่อยผ่าน `ngrok` ให้ตั้งค่าใน
SSM parameter หรือ `.env` ประมาณนี้:

```dotenv
HERMES_MODEL=gemma4:26b
OLLAMA_API_BASE=https://stranger-lanky-outmost.ngrok-free.dev/v1
OPENROUTER_API_KEY=
OPENAI_API_KEY=
MODEL_API_KEY=
```

หมายเหตุ:

- endpoint ต้องเป็น OpenAI-compatible path ที่ลงท้ายด้วย `/v1`
- ถ้า ngrok หรือ reverse proxy ของปลายทางมี auth เพิ่ม ให้ใส่ token ใน
  `MODEL_API_KEY` และตั้ง runtime provider ให้ใช้ `key_env: MODEL_API_KEY`
- ถ้ายังใช้ชื่อ env เดิม `OPENAI_API_BASE` อยู่ ระบบยังทำงานได้เหมือนเดิม

ถ้าต้องการย้ายจาก Ollama ไปใช้ AWS Bedrock ให้ตั้งค่าใน `.env` ประมาณนี้:

```dotenv
HERMES_PROVIDER=bedrock
HERMES_MODEL=CHANGE_ME_BEDROCK_MODEL_ID
AWS_DEFAULT_REGION=ap-southeast-1
AWS_BEARER_TOKEN_BEDROCK=
OLLAMA_API_BASE=
OPENAI_API_BASE=
OPENROUTER_API_KEY=
OPENAI_API_KEY=
MODEL_API_KEY=
```

หมายเหตุ:

- ถ้า EC2 instance role ใช้ Bedrock ได้อยู่แล้ว ให้ปล่อย `AWS_BEARER_TOKEN_BEDROCK=` ว่างไว้
- ถ้าจะใช้ Bedrock API key ให้ใส่ค่าไว้ที่ `AWS_BEARER_TOKEN_BEDROCK`
- ตัวอย่าง family ที่ใช้ได้คือ Claude, Nova, Llama, DeepSeek แต่ model ID ต้องตรงกับรุ่นที่เปิดใช้ใน account นั้น

ตรวจสถานะ:

```bash
docker compose ps
docker compose logs --tail=200 caddy line-relay ocr-worker results-api
curl -fsS "https://${PUBLIC_DOMAIN}/health"
curl -fsS "https://${PUBLIC_DOMAIN}/api/v1/governor-results/summary"
```

## Update และ rollback

Update:

```bash
cd /opt/election
git pull --ff-only
./deploy/ec2/deploy.sh
```

ก่อน release ให้ใช้ Git tag หรือ commit SHA ที่ผ่านการทดสอบ หลีกเลี่ยง image
tag `latest` ใน production เพื่อให้ rollback กลับไปยัง commit และ image เดิมได้

## Backup

- ข้อมูลผลและรูปอยู่ใน S3 ควรเปิด Versioning
- Hermes runtime อยู่บน EBS ควรทำ EBS snapshot ตามรอบ
- สำรอง `.env` ในระบบจัดการ secrets ขององค์กร ไม่เก็บใน Git

## ข้อจำกัด

EC2 เครื่องเดียวเป็น Single Point of Failure ระหว่าง reboot หรือ deploy จะมี
downtime และไม่รองรับ autoscaling อัตโนมัติ เมื่อ SLA ต้องสูงหรือ workload
เพิ่มจนเครื่องเดียวไม่พอ ให้แยกบริการออกจาก EC2 เครื่องเดียวและออกแบบ
deployment ใหม่จากสถานะ production ปัจจุบัน
See also: [`../../docs/governor-results-runtime.md`](../../docs/governor-results-runtime.md)
for the current governor-results source-of-truth, env flags, public export path,
and production editing points.
