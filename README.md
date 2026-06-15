# Election Platform

ระบบประกอบด้วย 4 process หลัก:

| Service | หน้าที่ | Public |
| --- | --- | --- |
| `hermes-supervisor` | Model gateway และ LINE plugin | ไม่ public |
| `line-relay` | รับ LINE webhook, เก็บ S3 และส่งงานเข้า SQS | ผ่าน Caddy |
| `ocr-worker` | อ่านงานจาก SQS และประมวลผลรูป | ไม่ public |
| `results-api` | API อ่านผลเลือกตั้งจาก S3 | ผ่าน Caddy |

## Local

```powershell
Copy-Item .env.example .env
# แก้ค่าจริงใน .env
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d --build
```

Endpoints สำหรับพัฒนา:

- Results API: `http://localhost:8080`
- LINE relay: `http://localhost:8646`
- Hermes API: `http://localhost:8642`
- Hermes dashboard: `http://localhost:9119`

## Production แบบต้นทุนต่ำ

ค่าเริ่มต้นที่แนะนำคือ EC2 หนึ่งเครื่อง + Docker Compose + Caddy โดยใช้ S3
และ SQS เดิม ไม่จำเป็นต้องมี ECS, ALB, API Gateway, NAT Gateway, EFS หรือ
Cloud Map

```bash
cd /opt/election
cp .env.example .env
# แก้ .env และตั้ง PUBLIC_DOMAIN/CADDY_EMAIL
./deploy/ec2/deploy.sh
```

Caddy เปิดเฉพาะ port `80/443` และออก TLS certificate ให้อัตโนมัติ
ส่วน port ของ service ภายใน bind อยู่ที่ `127.0.0.1` เท่านั้น

รายละเอียดการติดตั้งอยู่ที่
[`deploy/ec2/README.md`](deploy/ec2/README.md) และ
[`INFRASTRUCTURE.md`](INFRASTRUCTURE.md)

## ECS Fargate

ใช้เมื่อจำเป็นต้องทำ High Availability, autoscaling หรือแยกการ deploy
แต่ละ service รายละเอียดอยู่ที่ [`infra/ecs/README.md`](infra/ecs/README.md)
