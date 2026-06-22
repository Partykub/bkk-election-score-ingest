# Election Platform Architecture

## Services

| Service | Port (internal) | หน้าที่ |
|---|---|---|
| **Caddy** | 80, 443 (public) | Reverse proxy, TLS termination |
| **line-relay** | 8646 | รับ LINE webhook → ตรวจ signature → download รูปจาก LINE → เขียน S3 → push SQS |
| **hermes-supervisor** | ดูด้านล่าง | Model gateway — 1 container ทำ 2 role |
| **ocr-worker** | — | Consume `ocr-jobs` queue → เรียก Hermes → เขียน draft ลง S3 |
| **results-api** | 8080 | อ่าน approved results จาก S3 → เปิด REST API |

## hermes-supervisor: 1 container, 2 roles

Container นี้มีแค่ตัวเดียว แต่เปิด 3 port ทำงานคนละ role:

| Port | Role | เชื่อมต่อโดย |
|---|---|---|
| **:8642** | OpenAI-compatible API — รับ vision/chat request | `ocr-worker` (OCR role) |
| **:8647** | LINE plugin server — ส่ง/รับข้อความ LINE | `line-relay` (approval role) |
| **:9119** | Dashboard (web UI) | Admin เท่านั้น |

### การเชื่อมต่อ AI Provider

```
Model Provider (OpenAI API / OpenRouter / Ollama)
    ↑
    │  OLLAMA_API_BASE หรือ OPENAI_API_BASE
    │  + OPENAI_API_KEY / OPENROUTER_API_KEY / MODEL_API_KEY
    │
hermes-supervisor
    ├── :8642  ← ocr-worker เรียก vision request (model: gemma4:26b)
    └── :8647  ← line-relay ส่ง/รับ approval ผ่าน LINE plugin
```

Model provider ที่รองรับ (config ผ่าน `.env`):
- **OpenRouter** — `OPENAI_API_BASE=https://openrouter.ai/api/v1`
- **OpenAI** — `OPENAI_API_BASE=https://api.openai.com/v1`
- **Ollama (local)** — `OPENAI_API_BASE=http://host.docker.internal:11434/v1`
- **Ollama (remote via ngrok)** — `OLLAMA_API_BASE=https://your-name.ngrok-free.dev/v1`

## AWS Resources (ที่ใช้จริง)

| Resource | ชื่อ | ใช้โดย |
|---|---|---|
| **S3** | `bkk-election-images-dev` | line-relay (write), ocr-worker (read/write), results-api (read) |
| **SQS FIFO** | `election-ocr-jobs-dev.fifo` | line-relay (produce) → ocr-worker (consume) |
| **SQS DLQ** | `election-ocr-dlq-dev.fifo` | รับงาน OCR ที่ fail เกิน retry limit |
| **EC2** | `election-platform-dev` (`i-00dc21912d495beed`) | รัน Docker Compose ทั้งหมด |
| **Elastic IP** | `18.142.219.248` | DNS ชี้เข้าเครื่อง |
| **SSM Parameter** | `/election/compose-env` | เก็บ `.env` production |

## Data Flow

### Ingress → OCR → Approval

```
LINE User
  └→ LINE Platform
       └→ Caddy :443
            └→ line-relay :8646
                 ├→ S3            (รูปต้นฉบับ + source manifest)
                 └→ SQS ocr-jobs  (job manifest)
                       └→ ocr-worker
                             ├→ S3                         (load รูป)
                             ├→ hermes-supervisor :8642    (vision OCR)
                             │     └→ Model Provider
                             └→ S3                         (write draft)
                                   └→ hermes-supervisor :8647 (ส่ง approval prompt ทาง LINE)
                                         └→ LINE Platform → LINE User

LINE User (กด ยืนยัน / แก้ไข)
  └→ LINE Platform → Caddy → line-relay
       └→ S3  (write approval manifest)
```

### Results API

```
API Consumer
  └→ Caddy :443
       └→ results-api :8080
            └→ S3  (read approved results)
```

## Caddy Routing

| Path | Target |
|---|---|
| `/line/webhook`, `/webhook`, `/line/liff/*` | line-relay :8646 |
| `/api/*` | results-api :8080 |
| `/health` | Caddy built-in |

## S3 Object Types

- รูปต้นฉบับ
- source message manifest
- OCR job manifest
- draft revisions (raw + normalized)
- approval manifest
- audit events

## Deduplication

| ระดับ | Key | กฎ |
|---|---|---|
| Event | `line_event_id` | ซ้ำ → ignore ทันที |
| Message | `line_message_id` | ซ้ำ → ไม่สร้าง source message ใหม่ |
| Business | `result_signature` | ตรงกับ approved result → ไม่ยิง update ซ้ำ |

## ไม่อยู่ใน scope ปัจจุบัน

- `update-worker` + `SQS update-jobs` — phase 5, ยังไม่ deploy
