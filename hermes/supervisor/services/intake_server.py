from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, request

import boto3

from hermes.supervisor.upload_service import UploadServiceError, build_upload_service


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_workflow_session_id(event: dict[str, Any]) -> str:
    source = event.get("source") or {}
    if source.get("groupId"):
        return f"line_group_{source['groupId']}"
    if source.get("roomId"):
        return f"line_room_{source['roomId']}"
    if source.get("userId"):
        return f"line_user_{source['userId']}"
    return "line_unknown"


APPROVAL_TEXTS = {"ยืนยัน", "เธขเธทเธเธขเธฑเธ"}
CORRECTION_PREFIXES = ("แก้ไข", "เน\x81เธ\x81เน\x89เน\x84เธ\x82")
REJECT_TEXTS = {"ไม่ถูกต้อง", "เน\x84เธกเน\x88เธ–เธนเธ\x81เธ•เน\x89เธญเธ\x87"}
CORRECTION_HINT_KEYWORDS = ("แก้", "ก้ไข", "ไข", "เบอร์", "ผู้สมัคร", "คะแนน", "หาย", "ผิด")
APPROVAL_HINT_KEYWORDS = ("ยืนยัน", "ยัน", "ยืน", "รับรอง")


def is_approval_text(text: str) -> bool:
    return text.strip() in APPROVAL_TEXTS


def is_correction_text(text: str) -> bool:
    normalized = text.strip()
    return any(normalized.startswith(prefix) for prefix in CORRECTION_PREFIXES)


def is_reject_text(text: str) -> bool:
    return text.strip().lower() in {value.lower() for value in REJECT_TEXTS}


def looks_like_correction_text(text: str | None) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return False
    if is_correction_text(normalized):
        return True
    return any(keyword in normalized for keyword in CORRECTION_HINT_KEYWORDS)


def looks_like_approval_text(text: str | None) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return False
    if is_approval_text(normalized):
        return True
    return any(keyword in normalized for keyword in APPROVAL_HINT_KEYWORDS)


def detect_source_type(event: dict[str, Any]) -> str:
    if event.get("type") != "message":
        return event.get("type", "unknown")

    message = event.get("message") or {}
    message_type = message.get("type")
    if message_type == "image":
        return "image"
    if message_type == "text":
        text = (message.get("text") or "").strip()
        if is_approval_text(text):
            return "approval_command"
        if is_correction_text(text):
            return "correction_command"
        return "text"
    return message_type or "message"


def initial_state_for(source_type: str) -> str:
    if source_type == "image":
        return "received"
    if source_type in {"approval_command", "correction_command", "text"}:
        return "received"
    return "exception"


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_")
    return cleaned or "unknown"


def source_message_id_for(line_event_id: str) -> str:
    return f"src_{safe_id(line_event_id)}"


def approval_revision_path(source_message_id: str, revision: int) -> str:
    return f"approvals/{source_message_id}/revision-{revision}.json"


def approval_latest_path(source_message_id: str) -> str:
    return f"approvals/{source_message_id}/latest.json"


def update_job_path(update_job_id: str) -> str:
    return f"updates/jobs/{update_job_id}.json"


def normalize_approval_action(source_type: str, source_text: str | None) -> str:
    if source_type == "approval_command":
        return "approve"
    if source_type == "correction_command":
        return "correct"

    normalized_text = (source_text or "").strip().lower()
    if is_approval_text(normalized_text):
        return "approve"
    if is_correction_text(normalized_text):
        return "correct"
    if is_reject_text(normalized_text):
        return "reject"
    return "unknown"


def load_env_file(env_path: str | Path) -> None:
    target_path = Path(env_path)
    if not target_path.exists():
        return

    for raw_line in target_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue

        os.environ[key] = value.strip()


@dataclass(frozen=True)
class ProcessedEvent:
    source_message_id: str
    line_event_id: str
    state: str
    deduplicated: bool
    source_type: str


@dataclass(frozen=True)
class CandidateScoreOverride:
    candidate_number: int
    score: int


def build_image_received_text() -> str:
    return "รับรูปเรียบร้อยแล้ว กำลังตรวจข้อมูลจากภาพให้ครับ\nเดี๋ยวส่งผลให้ตรวจทานอีกครั้งเมื่อพร้อม"


def build_correction_guidance_text() -> str:
    return 'รับทราบว่าต้องการแก้ไขข้อมูล แต่ยังระบุค่าใหม่ไม่ครบ กรุณาพิมพ์ เช่น "แก้ไข ผู้สมัครเบอร์ 4 เป็น 14" หรือ "แก้ไข 4=14"'


def build_approval_guidance_text() -> str:
    return 'หากต้องการรับรองผล กรุณาพิมพ์ "ยืนยัน" ให้ถูกต้องอีกครั้ง'


def build_pending_approval_fallback_text() -> str:
    return 'หากต้องการรับรองให้ตอบ "ยืนยัน" หรือหากต้องการแก้ไขให้ตอบ เช่น "แก้ไข 4=14"'


def build_approval_success_text() -> str:
    return "รับรองผลเรียบร้อยแล้ว\nบันทึกผลล่าสุดในระบบแล้ว"


def draft_revision_path(source_message_id: str, revision: int) -> str:
    return f"drafts/{source_message_id}/revision-{revision}.json"


def draft_latest_path(source_message_id: str) -> str:
    return f"drafts/{source_message_id}/latest.json"


def build_result_signature(area_id: Any, candidate_scores: Any) -> str | None:
    if not isinstance(candidate_scores, list):
        return None

    signature_items: list[tuple[int, int]] = []
    for item in candidate_scores:
        if not isinstance(item, dict):
            continue
        candidate_number = item.get("candidate_number")
        score = item.get("score")
        try:
            normalized_candidate_number = int(candidate_number)
            normalized_score = int(str(score).replace(",", "").strip())
        except (TypeError, ValueError, AttributeError):
            continue
        signature_items.append((normalized_candidate_number, normalized_score))

    if not signature_items:
        return None

    prefix = str(area_id or "unknown-area").strip() or "unknown-area"
    fragments = [f"{candidate_number}={score}" for candidate_number, score in sorted(signature_items)]
    return f"{prefix}:" + "|".join(fragments)


def parse_candidate_score_overrides(source_text: str | None) -> list[CandidateScoreOverride]:
    if not source_text:
        return []

    normalized_text = re.sub(r"^\S+\s*", "", source_text.strip(), count=1)
    if not normalized_text:
        return []

    override_pattern = re.compile(
        r"(?:(?:ผู้สมัคร|เบอร์)\s*)?(\d+)\s*(?:=|เป็น|คือ|ควรเป็น)?\s*(\d[\d,]*)",
        re.IGNORECASE,
    )
    overrides: list[CandidateScoreOverride] = []
    seen_candidate_numbers: set[int] = set()
    for candidate_number_text, score_text in override_pattern.findall(normalized_text):
        candidate_number = int(candidate_number_text)
        score = int(score_text.replace(",", ""))
        if candidate_number in seen_candidate_numbers:
            overrides = [item for item in overrides if item.candidate_number != candidate_number]
        overrides.append(CandidateScoreOverride(candidate_number=candidate_number, score=score))
        seen_candidate_numbers.add(candidate_number)
    return overrides


def infer_draft_revision(draft_manifest: dict[str, Any], fallback_revision: int = 1) -> int:
    try:
        revision = int(draft_manifest.get("revision") or 0)
        if revision > 0:
            return revision
    except (TypeError, ValueError):
        pass

    draft_id = str(draft_manifest.get("draft_id") or "").strip()
    match = re.search(r"_r(\d+)$", draft_id)
    if match:
        return int(match.group(1))
    return fallback_revision


def normalize_candidate_scores(candidate_scores: Any) -> list[dict[str, Any]]:
    if not isinstance(candidate_scores, list):
        return []

    normalized_scores: list[dict[str, Any]] = []
    for item in candidate_scores:
        if not isinstance(item, dict):
            continue
        candidate_number = item.get("candidate_number")
        score = item.get("score")
        try:
            normalized_candidate_number = int(candidate_number) if candidate_number is not None else None
        except (TypeError, ValueError):
            normalized_candidate_number = None
        try:
            normalized_score = int(str(score).replace(",", "").strip()) if score is not None else None
        except (TypeError, ValueError, AttributeError):
            normalized_score = None
        normalized_scores.append(
            {
                "candidate_number": normalized_candidate_number,
                "candidate_name": item.get("candidate_name"),
                "score": normalized_score,
                "confidence": item.get("confidence"),
                "raw_text": item.get("raw_text"),
            }
        )
    return normalized_scores


def build_approval_prompt_text(draft_manifest: dict[str, Any]) -> str:
    revision = int(draft_manifest.get("revision") or 1)
    report_type = str(draft_manifest.get("report_type") or "score_sheet").strip()
    area_id = str(draft_manifest.get("area_id") or "").strip()
    polling_unit_id = str(draft_manifest.get("polling_unit_id") or "").strip()
    candidate_scores = normalize_candidate_scores(draft_manifest.get("candidate_scores"))

    lines = [f"ตรวจรูปเสร็จแล้ว: ร่างครั้งที่ {revision}"]
    if area_id:
        lines.append(f"เขต: {area_id}")
    if polling_unit_id:
        lines.append(f"หน่วย: {polling_unit_id}")
    lines.append(f"เอกสาร: {report_type}")

    if candidate_scores:
        lines.append("คะแนนที่อ่านได้:")
        for score in candidate_scores:
            candidate_number = score.get("candidate_number")
            candidate_value = score.get("score")
            if candidate_number is None or candidate_value is None:
                continue
            lines.append(f"ผู้สมัคร {candidate_number}: {candidate_value}")
    else:
        lines.append("ยังไม่พบคะแนนที่เชื่อถือได้จาก OCR")

    lines.append("ตอบ 'ยืนยัน' เพื่อรับรองร่างนี้")
    lines.append("ตอบ 'แก้ไข <รายละเอียด>' หากต้องการแก้ข้อมูล")
    return "\n".join(lines)


def send_line_push_message(
    *,
    channel_access_token: str,
    destination_id: str,
    text: str,
    api_base_url: str = "https://api.line.me",
    opener: Any = request.urlopen,
) -> None:
    line_request = request.Request(
        f"{api_base_url.rstrip('/')}/v2/bot/message/push",
        data=json.dumps(
            {
                "to": destination_id,
                "messages": [{"type": "text", "text": text[:5000]}],
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {channel_access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with opener(line_request, timeout=30):
            return
    except error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"line push failed with status {exc.code}: {response_body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"unable to reach line push api: {exc.reason}") from exc


def send_line_reply_message(
    *,
    channel_access_token: str,
    reply_token: str,
    text: str,
    api_base_url: str = "https://api.line.me",
    opener: Any = request.urlopen,
) -> None:
    line_request = request.Request(
        f"{api_base_url.rstrip('/')}/v2/bot/message/reply",
        data=json.dumps(
            {
                "replyToken": reply_token,
                "messages": [{"type": "text", "text": text[:5000]}],
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {channel_access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with opener(line_request, timeout=30):
            return
    except error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"line reply failed with status {exc.code}: {response_body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"unable to reach line reply api: {exc.reason}") from exc


def build_line_reply_sender_from_env(opener: Any = request.urlopen) -> Any | None:
    channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not channel_access_token:
        return None

    api_base_url = os.environ.get("LINE_API_BASE_URL", "https://api.line.me").strip() or "https://api.line.me"

    def send_reply(*, reply_token: str, text: str) -> None:
        send_line_reply_message(
            channel_access_token=channel_access_token,
            reply_token=reply_token,
            text=text,
            api_base_url=api_base_url,
            opener=opener,
        )

    return send_reply


def build_line_push_sender_from_env(opener: Any = request.urlopen) -> Any | None:
    channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not channel_access_token:
        return None

    api_base_url = os.environ.get("LINE_API_BASE_URL", "https://api.line.me").strip() or "https://api.line.me"

    def send_push(*, destination_id: str, text: str) -> None:
        send_line_push_message(
            channel_access_token=channel_access_token,
            destination_id=destination_id,
            text=text,
            api_base_url=api_base_url,
            opener=opener,
        )

    return send_push


class SqsOcrJobQueue:
    def __init__(self, *, queue_url: str, region_name: str | None = None, client: Any | None = None) -> None:
        self.queue_url = queue_url
        self.client = client or boto3.client("sqs", region_name=region_name)

    def enqueue(self, payload: dict[str, Any]) -> None:
        request = {
            "QueueUrl": self.queue_url,
            "MessageBody": json.dumps(payload, ensure_ascii=False),
        }
        if self._is_fifo_queue():
            request["MessageGroupId"] = self._message_group_id(payload)
            request["MessageDeduplicationId"] = self._message_deduplication_id(payload)
        self.client.send_message(**request)

    def _is_fifo_queue(self) -> bool:
        return self.queue_url.lower().endswith(".fifo")

    @staticmethod
    def _message_group_id(payload: dict[str, Any]) -> str:
        group_id = payload.get("workflow_session_id") or payload.get("source_message_id") or "ocr-jobs"
        return safe_id(str(group_id))

    @staticmethod
    def _message_deduplication_id(payload: dict[str, Any]) -> str:
        dedup_id = payload.get("ocr_job_id") or payload.get("source_message_id") or payload.get("manifest_key") or utc_now_iso()
        return safe_id(str(dedup_id))


class SqsUpdateJobQueue:
    def __init__(self, *, queue_url: str, region_name: str | None = None, client: Any | None = None) -> None:
        self.queue_url = queue_url
        self.client = client or boto3.client("sqs", region_name=region_name)

    def enqueue(self, payload: dict[str, Any]) -> None:
        request = {
            "QueueUrl": self.queue_url,
            "MessageBody": json.dumps(payload, ensure_ascii=False),
        }
        if self._is_fifo_queue():
            request["MessageGroupId"] = self._message_group_id(payload)
            request["MessageDeduplicationId"] = self._message_deduplication_id(payload)
        self.client.send_message(**request)

    def _is_fifo_queue(self) -> bool:
        return self.queue_url.lower().endswith(".fifo")

    @staticmethod
    def _message_group_id(payload: dict[str, Any]) -> str:
        group_id = payload.get("workflow_session_id") or payload.get("source_message_id") or "update-jobs"
        return safe_id(str(group_id))

    @staticmethod
    def _message_deduplication_id(payload: dict[str, Any]) -> str:
        dedup_id = payload.get("update_job_id") or payload.get("idempotency_key") or payload.get("manifest_key") or utc_now_iso()
        return safe_id(str(dedup_id))


class LocalJsonStateBackend:
    def __init__(self, root_path: str | Path):
        self.root_path = Path(root_path)

    def write_json(self, relative_path: str, payload: dict[str, Any]) -> str:
        target_path = self.root_path / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return relative_path.replace("\\", "/")

    def read_json(self, relative_path: str) -> dict[str, Any] | None:
        target_path = self.root_path / relative_path
        if not target_path.exists():
            return None
        return json.loads(target_path.read_text(encoding="utf-8"))


class S3JsonStateBackend:
    def __init__(
        self,
        *,
        bucket_name: str,
        region_name: str | None = None,
        endpoint_url: str | None = None,
        key_prefix: str = "",
        client: Any | None = None,
    ) -> None:
        self.bucket_name = bucket_name
        self.key_prefix = key_prefix.strip("/")
        self.client = client or boto3.client("s3", region_name=region_name, endpoint_url=endpoint_url)

    def write_json(self, relative_path: str, payload: dict[str, Any]) -> str:
        object_key = self._with_prefix(relative_path)
        self.client.put_object(
            Bucket=self.bucket_name,
            Key=object_key,
            Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
            ContentType="application/json",
        )
        return object_key

    def read_json(self, relative_path: str) -> dict[str, Any] | None:
        object_key = self._with_prefix(relative_path)
        try:
            response = self.client.get_object(Bucket=self.bucket_name, Key=object_key)
        except Exception as exc:
            if self._is_missing_key(exc):
                return None
            raise

        body = response["Body"]
        if hasattr(body, "read"):
            raw_payload = body.read()
        else:
            raw_payload = body
        return json.loads(raw_payload.decode("utf-8"))

    def _with_prefix(self, relative_path: str) -> str:
        relative_path = relative_path.replace("\\", "/")
        if not self.key_prefix:
            return relative_path
        return f"{self.key_prefix}/{relative_path}"

    @staticmethod
    def _is_missing_key(exc: Exception) -> bool:
        if isinstance(exc, KeyError):
            return True
        response = getattr(exc, "response", None) or {}
        error_payload = response.get("Error", {}) if isinstance(response, dict) else {}
        error_code = str(error_payload.get("Code", "")).lower()
        return error_code in {"nosuchkey", "404", "notfound"}


def build_state_backend(root_path: str | Path) -> LocalJsonStateBackend | S3JsonStateBackend:
    backend = os.environ.get("SUPERVISOR_STORAGE_BACKEND", "local-mock").strip().lower()
    if backend == "s3":
        bucket_name = os.environ.get("SUPERVISOR_S3_BUCKET", "").strip()
        if not bucket_name:
            raise UploadServiceError("SUPERVISOR_S3_BUCKET is required when SUPERVISOR_STORAGE_BACKEND=s3")
        region_name = os.environ.get("SUPERVISOR_S3_REGION", "").strip() or None
        endpoint_url = os.environ.get("SUPERVISOR_S3_ENDPOINT", "").strip() or None
        key_prefix = os.environ.get("SUPERVISOR_S3_PREFIX", "").strip()
        return S3JsonStateBackend(
            bucket_name=bucket_name,
            region_name=region_name,
            endpoint_url=endpoint_url,
            key_prefix=key_prefix,
        )
    return LocalJsonStateBackend(root_path)


def build_ocr_job_queue() -> SqsOcrJobQueue | None:
    queue_url = (
        os.environ.get("SUPERVISOR_OCR_QUEUE_URL", "").strip()
        or os.environ.get("OCR_WORKER_QUEUE_URL", "").strip()
    )
    if not queue_url:
        return None

    region_name = (
        os.environ.get("SUPERVISOR_OCR_QUEUE_REGION", "").strip()
        or os.environ.get("OCR_WORKER_AWS_REGION", "").strip()
        or os.environ.get("SUPERVISOR_S3_REGION", "").strip()
        or None
    )
    return SqsOcrJobQueue(queue_url=queue_url, region_name=region_name)


def build_update_job_queue() -> SqsUpdateJobQueue | None:
    queue_url = (
        os.environ.get("SUPERVISOR_UPDATE_QUEUE_URL", "").strip()
        or os.environ.get("UPDATE_WORKER_QUEUE_URL", "").strip()
    )
    if not queue_url:
        return None

    region_name = (
        os.environ.get("SUPERVISOR_UPDATE_QUEUE_REGION", "").strip()
        or os.environ.get("UPDATE_WORKER_AWS_REGION", "").strip()
        or os.environ.get("SUPERVISOR_S3_REGION", "").strip()
        or None
    )
    return SqsUpdateJobQueue(queue_url=queue_url, region_name=region_name)


class LocalStateStore:
    def __init__(
        self,
        root_path: str | Path,
        *,
        state_backend: LocalJsonStateBackend | S3JsonStateBackend | None = None,
        upload_service: Any | None = None,
        ocr_job_queue: SqsOcrJobQueue | None = None,
        update_job_queue: SqsUpdateJobQueue | None = None,
        line_reply_sender: Any | None = None,
        line_push_sender: Any | None = None,
    ):
        self.root_path = Path(root_path)
        self.state_backend = state_backend or build_state_backend(self.root_path)
        self.upload_service = upload_service or build_upload_service(self.root_path)
        self.ocr_job_queue = ocr_job_queue or build_ocr_job_queue()
        self.update_job_queue = update_job_queue or build_update_job_queue()
        self.line_reply_sender = line_reply_sender or build_line_reply_sender_from_env()
        self.line_push_sender = line_push_sender or build_line_push_sender_from_env()

    def _write_json(self, relative_path: str, payload: dict[str, Any]) -> str:
        return self.state_backend.write_json(relative_path, payload)

    def _read_json(self, relative_path: str) -> dict[str, Any] | None:
        return self.state_backend.read_json(relative_path)

    def _normalize_state_path(self, path: str) -> str:
        normalized = path.replace("\\", "/").lstrip("/")
        key_prefix = getattr(self.state_backend, "key_prefix", "")
        normalized_prefix = str(key_prefix or "").strip("/")
        if normalized_prefix and normalized.startswith(f"{normalized_prefix}/"):
            return normalized[len(normalized_prefix) + 1 :]
        return normalized

    def event_index_path(self, line_event_id: str) -> str:
        return f"indexes/by-line-event-id/{safe_id(line_event_id)}.json"

    def message_index_path(self, line_message_id: str) -> str:
        return f"indexes/by-line-message-id/{safe_id(line_message_id)}.json"

    def session_pointer_path(self, workflow_session_id: str) -> str:
        return f"indexes/by-session/{safe_id(workflow_session_id)}/latest.json"

    def source_manifest_path(self, source_message_id: str) -> str:
        return f"manifests/source-messages/{source_message_id}.json"

    def ocr_job_manifest_path(self, ocr_job_id: str) -> str:
        return f"manifests/ocr-jobs/{ocr_job_id}.json"

    def ocr_job_id_for(self, source_message_id: str) -> str:
        return f"ocr_{safe_id(source_message_id)}"

    def _default_ocr_options(self) -> dict[str, Any]:
        return {
            "language_hint": os.environ.get("SUPERVISOR_OCR_LANGUAGE_HINT", "th").strip() or "th",
            "expected_document_type": os.environ.get("SUPERVISOR_OCR_DOCUMENT_TYPE", "election_score_sheet").strip()
            or "election_score_sheet",
            "prompt_version": os.environ.get("SUPERVISOR_OCR_PROMPT_VERSION", "ocr-v1").strip() or "ocr-v1",
            "model_name": os.environ.get("OCR_WORKER_MODEL_NAME", "gemma-vision").strip() or "gemma-vision",
        }

    def _queue_name(self) -> str:
        return os.environ.get("SUPERVISOR_OCR_QUEUE_NAME", "ocr-jobs.fifo").strip() or "ocr-jobs.fifo"

    def _build_ocr_job_manifest(self, manifest: dict[str, Any], *, timestamp: str) -> tuple[str, dict[str, Any]]:
        ocr_job_id = self.ocr_job_id_for(manifest["source_message_id"])
        media = manifest.get("media") or {}
        ocr_manifest = {
            "schema_version": "2026-06-09",
            "entity_type": "ocr_job",
            "entity_id": ocr_job_id,
            "ocr_job_id": ocr_job_id,
            "source_message_id": manifest["source_message_id"],
            "workflow_session_id": manifest["workflow_session_id"],
            "state": "queued",
            "queue_name": self._queue_name(),
            "attempt_count": 0,
            "max_attempts": 5,
            "requested_by": "hermes-supervisor",
            "input": {
                "bucket": media.get("bucket"),
                "key": media.get("key"),
                "metadata_key": media.get("metadata_key"),
            },
            "line_context": {
                "platform": manifest["platform"],
                "line_event_id": manifest["line_event_id"],
                "line_message_id": manifest["line_message_id"],
                "sender_user_id": manifest["sender_user_id"],
                "sender_group_id": manifest["sender_group_id"],
                "sender_room_id": manifest["sender_room_id"],
            },
            "ocr_options": self._default_ocr_options(),
            "result": None,
            "error": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        return ocr_job_id, ocr_manifest

    def _enqueue_ocr_job_if_configured(self, manifest: dict[str, Any], *, timestamp: str) -> str | None:
        if self.ocr_job_queue is None:
            return None

        ocr_job_id, ocr_manifest = self._build_ocr_job_manifest(manifest, timestamp=timestamp)
        manifest_key = self._write_json(self.ocr_job_manifest_path(ocr_job_id), ocr_manifest)

        queue_payload = {
            "ocr_job_id": ocr_job_id,
            "source_message_id": manifest["source_message_id"],
            "workflow_session_id": manifest["workflow_session_id"],
            "manifest_key": manifest_key,
            "manifest_bucket": ocr_manifest["input"]["bucket"],
        }
        self.ocr_job_queue.enqueue(queue_payload)
        return ocr_job_id

    def has_seen_event(self, line_event_id: str) -> bool:
        return self._read_json(self.event_index_path(line_event_id)) is not None

    def read_manifest(self, source_message_id: str) -> dict[str, Any] | None:
        return self._read_json(self.source_manifest_path(source_message_id))

    def update_job_manifest_path(self, update_job_id: str) -> str:
        return update_job_path(update_job_id)

    def read_session_pointer(self, workflow_session_id: str) -> dict[str, Any] | None:
        return self._read_json(self.session_pointer_path(workflow_session_id))

    def _session_anchor_source_message_id(self, workflow_session_id: str, fallback: str) -> str:
        session_pointer = self.read_session_pointer(workflow_session_id) or {}
        return str(session_pointer.get("latest_source_message_id") or "").strip() or fallback

    def _resolve_active_approval(self, workflow_session_id: str) -> tuple[str | None, dict[str, Any] | None, str | None, dict[str, Any] | None]:
        session_pointer = self.read_session_pointer(workflow_session_id) or {}
        target_source_message_id = str(session_pointer.get("latest_source_message_id") or "").strip() or None
        if not target_source_message_id:
            return None, None, None, None

        target_source_manifest = self.read_manifest(target_source_message_id)
        if target_source_manifest is None:
            return target_source_message_id, None, None, None

        approval_key = str(target_source_manifest.get("current_approval_key") or "").strip() or approval_latest_path(target_source_message_id)
        approval_manifest = self._read_json(self._normalize_state_path(approval_key))
        return target_source_message_id, target_source_manifest, approval_key, approval_manifest

    def _build_update_job_manifest(
        self,
        *,
        source_manifest: dict[str, Any],
        approval_manifest: dict[str, Any],
        draft_manifest: dict[str, Any],
        timestamp: str,
    ) -> tuple[str, dict[str, Any]]:
        update_job_id = f"upd_{safe_id(str(approval_manifest['approval_id']))}"
        update_manifest = {
            "schema_version": "2026-06-09",
            "entity_type": "update_job",
            "entity_id": update_job_id,
            "update_job_id": update_job_id,
            "source_message_id": source_manifest["source_message_id"],
            "draft_id": draft_manifest["draft_id"],
            "approval_id": approval_manifest["approval_id"],
            "workflow_session_id": source_manifest["workflow_session_id"],
            "state": "queued",
            "queue_name": os.environ.get("SUPERVISOR_UPDATE_QUEUE_NAME", "update-jobs").strip() or "update-jobs",
            "attempt_count": 0,
            "max_attempts": 5,
            "idempotency_key": draft_manifest.get("result_signature") or draft_manifest["draft_id"],
            "payload": {
                "election_id": draft_manifest.get("election_id"),
                "area_id": draft_manifest.get("area_id"),
                "polling_unit_id": draft_manifest.get("polling_unit_id"),
                "report_type": draft_manifest.get("report_type"),
                "candidate_scores": draft_manifest.get("candidate_scores") or [],
            },
            "result": None,
            "error": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        return update_job_id, update_manifest

    def _enqueue_update_job_if_configured(self, update_manifest: dict[str, Any], *, manifest_key: str) -> None:
        if self.update_job_queue is None:
            return

        manifest_bucket = str(getattr(self.state_backend, "bucket_name", "") or "").strip()
        if not manifest_bucket:
            return

        self.update_job_queue.enqueue(
            {
                "update_job_id": update_manifest["update_job_id"],
                "source_message_id": update_manifest["source_message_id"],
                "workflow_session_id": update_manifest["workflow_session_id"],
                "approval_id": update_manifest["approval_id"],
                "idempotency_key": update_manifest.get("idempotency_key"),
                "manifest_bucket": manifest_bucket,
                "manifest_key": manifest_key,
            }
        )

    def _line_destination_id_for_source_manifest(self, source_manifest: dict[str, Any]) -> str | None:
        for key in ("sender_group_id", "sender_room_id", "sender_user_id"):
            value = str(source_manifest.get(key) or "").strip()
            if value:
                return value
        return None

    def _update_approval_prompt_status(
        self,
        *,
        source_manifest: dict[str, Any],
        draft_manifest: dict[str, Any],
        destination_id: str | None,
        status: str,
        timestamp: str,
        reason: str | None = None,
        error_message: str | None = None,
    ) -> None:
        approval_prompt = {
            "status": status,
            "draft_id": draft_manifest.get("draft_id"),
            "updated_at": timestamp,
        }
        if destination_id:
            approval_prompt["destination_id"] = destination_id
        if status == "sent":
            approval_prompt["message_type"] = "push"
            approval_prompt["sent_at"] = timestamp
        if reason:
            approval_prompt["reason"] = reason
        if error_message:
            approval_prompt["error"] = error_message
        source_manifest["approval_prompt"] = approval_prompt

    def _send_approval_prompt_for_draft(
        self,
        *,
        source_manifest: dict[str, Any],
        draft_manifest: dict[str, Any],
        timestamp: str,
    ) -> None:
        if source_manifest.get("approval_prompt", {}).get("draft_id") == draft_manifest.get("draft_id") and source_manifest.get("approval_prompt", {}).get("status") == "sent":
            return

        destination_id = self._line_destination_id_for_source_manifest(source_manifest)
        if self.line_push_sender is None:
            self._update_approval_prompt_status(
                source_manifest=source_manifest,
                draft_manifest=draft_manifest,
                destination_id=destination_id,
                status="skipped",
                timestamp=timestamp,
                reason="missing_line_channel_access_token",
            )
            return

        if not destination_id:
            self._update_approval_prompt_status(
                source_manifest=source_manifest,
                draft_manifest=draft_manifest,
                destination_id=None,
                status="skipped",
                timestamp=timestamp,
                reason="missing_line_destination",
            )
            return

        try:
            self.line_push_sender(destination_id=destination_id, text=build_approval_prompt_text(draft_manifest))
            self._update_approval_prompt_status(
                source_manifest=source_manifest,
                draft_manifest=draft_manifest,
                destination_id=destination_id,
                status="sent",
                timestamp=timestamp,
            )
        except Exception as exc:
            self._update_approval_prompt_status(
                source_manifest=source_manifest,
                draft_manifest=draft_manifest,
                destination_id=destination_id,
                status="failed",
                timestamp=timestamp,
                error_message=str(exc),
            )

    def _reply_text(self, manifest: dict[str, Any], text: str) -> None:
        if self.line_reply_sender is None:
            return

        reply_token = str(manifest.get("line_reply_token") or "").strip()
        if not reply_token:
            return

        try:
            self.line_reply_sender(reply_token=reply_token, text=text)
        except Exception as exc:
            print(f"line intake: unable to send text reply: {exc}", file=sys.stderr)

    def _reply_text_for_active_approval(self, manifest: dict[str, Any]) -> None:
        source_type = str(manifest.get("source_type") or "").strip()
        if source_type not in {"text", "correction_command"}:
            return

        workflow_session_id = str(manifest.get("workflow_session_id") or "").strip()
        if not workflow_session_id:
            return

        target_source_message_id, target_source_manifest, _, approval_manifest = self._resolve_active_approval(workflow_session_id)
        if target_source_manifest is None or approval_manifest is None or not target_source_message_id:
            return
        if target_source_message_id == manifest.get("source_message_id"):
            return
        if approval_manifest.get("state") != "awaiting_approval":
            return

        source_text = str(manifest.get("source_text") or "").strip()
        if not source_text:
            return

        if source_type == "correction_command" and (manifest.get("exception") or {}).get("code") == "CORRECTION_PARSE_FAILED":
            self._reply_text(manifest, build_correction_guidance_text())
            return

        if source_type == "text":
            if looks_like_approval_text(source_text):
                self._reply_text(manifest, build_approval_guidance_text())
            elif looks_like_correction_text(source_text):
                self._reply_text(manifest, build_correction_guidance_text())
            else:
                self._reply_text(manifest, build_pending_approval_fallback_text())

    def _build_approval_documents(
        self,
        *,
        source_message_id: str,
        workflow_session_id: str,
        draft_id: str,
        draft_revision: int,
        requested_from_user_id: str | None,
        timestamp: str,
    ) -> tuple[dict[str, Any], dict[str, Any], str, str]:
        approval_id = f"approval_{source_message_id}_r{draft_revision}"
        approval_key = approval_revision_path(source_message_id, draft_revision)
        approval_manifest = {
            "schema_version": "2026-06-09",
            "entity_type": "approval",
            "entity_id": approval_id,
            "approval_id": approval_id,
            "source_message_id": source_message_id,
            "draft_id": draft_id,
            "draft_revision": draft_revision,
            "workflow_session_id": workflow_session_id,
            "state": "awaiting_approval",
            "requested_from_user_id": requested_from_user_id,
            "requested_via": "line_text_push",
            "requested_at": timestamp,
            "expires_at": None,
            "responded_at": None,
            "response_type": None,
            "response_source_message_id": None,
            "response_text": None,
            "response_payload": None,
            "approved_by_user_id": None,
            "rejected_by_user_id": None,
            "approval_note": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        approval_pointer = {
            "schema_version": "2026-06-09",
            "entity_type": "approval_pointer",
            "entity_id": source_message_id,
            "source_message_id": source_message_id,
            "approval_id": approval_id,
            "approval_key": approval_key,
            "draft_id": draft_id,
            "draft_revision": draft_revision,
            "state": "awaiting_approval",
            "updated_at": timestamp,
        }
        return approval_manifest, approval_pointer, approval_id, approval_key

    def _build_corrected_draft_documents(
        self,
        *,
        source_message_id: str,
        workflow_session_id: str,
        source_manifest: dict[str, Any],
        draft_manifest: dict[str, Any],
        correction_source_message_id: str,
        correction_note: str,
        correction_payload: dict[str, Any],
        candidate_score_overrides: list[CandidateScoreOverride],
        timestamp: str,
    ) -> tuple[dict[str, Any], dict[str, Any], str, str, int]:
        next_revision = infer_draft_revision(draft_manifest) + 1
        draft_id = f"draft_{source_message_id}_r{next_revision}"
        draft_key = draft_revision_path(source_message_id, next_revision)
        latest_key = draft_latest_path(source_message_id)
        existing_scores = draft_manifest.get("candidate_scores")
        candidate_scores = [dict(item) for item in existing_scores] if isinstance(existing_scores, list) else []
        candidate_scores_by_number: dict[int, dict[str, Any]] = {}
        for item in candidate_scores:
            try:
                candidate_number = int(item.get("candidate_number"))
            except (TypeError, ValueError):
                continue
            candidate_scores_by_number[candidate_number] = item

        for override in candidate_score_overrides:
            target_score = candidate_scores_by_number.get(override.candidate_number)
            if target_score is None:
                target_score = {
                    "candidate_number": override.candidate_number,
                    "candidate_name": None,
                    "score": override.score,
                    "confidence": 1.0,
                    "raw_text": correction_note,
                }
                candidate_scores.append(target_score)
                candidate_scores_by_number[override.candidate_number] = target_score
            else:
                target_score["score"] = override.score
                target_score["confidence"] = 1.0
                target_score["raw_text"] = correction_note

        candidate_scores.sort(
            key=lambda item: (
                int(item.get("candidate_number")) if str(item.get("candidate_number", "")).isdigit() else 999999
            )
        )

        corrected_draft = dict(draft_manifest)
        corrected_draft["schema_version"] = "2026-06-09"
        corrected_draft["entity_type"] = "draft"
        corrected_draft["entity_id"] = draft_id
        corrected_draft["draft_id"] = draft_id
        corrected_draft["source_message_id"] = source_message_id
        corrected_draft["workflow_session_id"] = workflow_session_id
        corrected_draft["ocr_job_id"] = draft_manifest.get("ocr_job_id")
        corrected_draft["revision"] = next_revision
        corrected_draft["status"] = "awaiting_approval"
        corrected_draft["candidate_scores"] = candidate_scores
        corrected_draft["result_signature"] = build_result_signature(corrected_draft.get("area_id"), candidate_scores)
        corrected_draft["created_by"] = "line_correction"
        corrected_draft["corrected_from_draft_id"] = draft_manifest.get("draft_id")
        corrected_draft["correction_source_message_id"] = correction_source_message_id
        corrected_draft["correction_note"] = correction_note
        corrected_draft["correction_payload"] = correction_payload
        corrected_draft["updated_at"] = timestamp
        corrected_draft["created_at"] = timestamp

        latest_pointer = {
            "schema_version": "2026-06-09",
            "entity_type": "draft_pointer",
            "entity_id": source_message_id,
            "source_message_id": source_message_id,
            "draft_id": draft_id,
            "draft_key": draft_key,
            "revision": next_revision,
            "updated_at": timestamp,
        }
        return corrected_draft, latest_pointer, draft_id, draft_key, next_revision

    def _apply_approval_response(
        self,
        *,
        manifest: dict[str, Any],
        workflow_session_id: str,
        source_type: str,
        source_text: str | None,
        sender_user_id: str | None,
        timestamp: str,
    ) -> tuple[str, str | None]:
        action = normalize_approval_action(source_type, source_text)
        manifest["approval_action"] = action

        if action not in {"approve", "correct", "reject"}:
            manifest["state"] = "exception"
            manifest["exception"] = {"code": "UNKNOWN_APPROVAL_COMMAND", "message": "approval command did not match a supported action"}
            return manifest["state"], None

        target_source_message_id, target_source_manifest, approval_key, approval_manifest = self._resolve_active_approval(workflow_session_id)
        manifest["target_source_message_id"] = target_source_message_id
        normalized_approval_key = self._normalize_state_path(approval_key) if approval_key else None

        if target_source_manifest is None or approval_manifest is None or not approval_key:
            manifest["state"] = "exception"
            manifest["exception"] = {"code": "APPROVAL_NOT_FOUND", "message": "no active approval was found for this workflow session"}
            return manifest["state"], target_source_message_id

        manifest["target_draft_id"] = approval_manifest.get("draft_id")
        manifest["target_approval_id"] = approval_manifest.get("approval_id")

        if approval_manifest.get("state") != "awaiting_approval":
            manifest["state"] = "exception"
            manifest["exception"] = {"code": "APPROVAL_NOT_PENDING", "message": "active approval is no longer awaiting approval"}
            return manifest["state"], target_source_message_id

        requested_from_user_id = str(approval_manifest.get("requested_from_user_id") or "").strip()
        if requested_from_user_id and sender_user_id and requested_from_user_id != sender_user_id:
            manifest["state"] = "exception"
            manifest["exception"] = {"code": "APPROVAL_FORBIDDEN", "message": "approval response came from a different user than the requested recipient"}
            return manifest["state"], target_source_message_id

        draft_key = str(target_source_manifest.get("current_draft_key") or "").strip()
        draft_manifest = self._read_json(self._normalize_state_path(draft_key)) if draft_key else None
        if draft_manifest is None:
            manifest["state"] = "exception"
            manifest["exception"] = {"code": "DRAFT_NOT_FOUND", "message": "active draft could not be loaded for approval"}
            return manifest["state"], target_source_message_id

        if action == "correct":
            candidate_score_overrides = parse_candidate_score_overrides(source_text)
            if not candidate_score_overrides:
                manifest["state"] = "exception"
                manifest["exception"] = {
                    "code": "CORRECTION_PARSE_FAILED",
                    "message": "correction command could not be parsed into candidate score overrides",
                }
                manifest["correction_payload"] = {
                    "normalized_action": action,
                    "candidate_score_overrides": [],
                    "requires_manual_review": True,
                }
                return manifest["state"], target_source_message_id

        new_approval_state = "approved" if action == "approve" else "rejected"
        approval_manifest["state"] = new_approval_state
        approval_manifest["responded_at"] = timestamp
        approval_manifest["response_type"] = "line_text"
        approval_manifest["response_source_message_id"] = manifest["source_message_id"]
        approval_manifest["response_text"] = source_text
        approval_manifest["response_payload"] = {"normalized_action": action}
        approval_manifest["approved_by_user_id"] = sender_user_id if action == "approve" else None
        approval_manifest["rejected_by_user_id"] = sender_user_id if action != "approve" else None
        approval_manifest["approval_note"] = source_text if action != "approve" else None
        approval_manifest["updated_at"] = timestamp
        self._write_json(normalized_approval_key or approval_key, approval_manifest)

        approval_pointer = {
            "schema_version": "2026-06-09",
            "entity_type": "approval_pointer",
            "entity_id": target_source_message_id,
            "source_message_id": target_source_message_id,
            "approval_id": approval_manifest["approval_id"],
            "approval_key": approval_revision_path(target_source_message_id, int(approval_manifest.get("draft_revision") or 1)),
            "draft_id": approval_manifest["draft_id"],
            "draft_revision": approval_manifest["draft_revision"],
            "state": new_approval_state,
            "updated_at": timestamp,
        }
        self._write_json(approval_latest_path(target_source_message_id), approval_pointer)

        target_source_manifest["state"] = new_approval_state
        target_source_manifest["current_approval_id"] = approval_manifest["approval_id"]
        target_source_manifest["current_approval_key"] = approval_key
        target_source_manifest["updated_at"] = timestamp

        update_job_id = None
        if action == "approve":
            update_job_id, update_manifest = self._build_update_job_manifest(
                source_manifest=target_source_manifest,
                approval_manifest=approval_manifest,
                draft_manifest=draft_manifest,
                timestamp=timestamp,
            )
            update_job_manifest_key = self._write_json(self.update_job_manifest_path(update_job_id), update_manifest)
            self._enqueue_update_job_if_configured(update_manifest, manifest_key=update_job_manifest_key)
            target_source_manifest["current_update_job_id"] = update_job_id
            target_source_manifest["current_update_job_key"] = update_job_manifest_key
        elif action == "correct":
            correction_payload = {
                "normalized_action": action,
                "candidate_score_overrides": [
                    {"candidate_number": override.candidate_number, "score": override.score}
                    for override in candidate_score_overrides
                ],
                "requires_manual_review": False,
            }
            approval_manifest["response_payload"] = correction_payload
            self._write_json(normalized_approval_key or approval_key, approval_manifest)

            corrected_draft, draft_pointer, corrected_draft_id, corrected_draft_key, next_revision = self._build_corrected_draft_documents(
                source_message_id=target_source_message_id,
                workflow_session_id=workflow_session_id,
                source_manifest=target_source_manifest,
                draft_manifest=draft_manifest,
                correction_source_message_id=manifest["source_message_id"],
                correction_note=source_text or "",
                correction_payload=correction_payload,
                candidate_score_overrides=candidate_score_overrides,
                timestamp=timestamp,
            )
            self._write_json(corrected_draft_key, corrected_draft)
            self._write_json(draft_latest_path(target_source_message_id), draft_pointer)

            next_approval_manifest, next_approval_pointer, next_approval_id, next_approval_key = self._build_approval_documents(
                source_message_id=target_source_message_id,
                workflow_session_id=workflow_session_id,
                draft_id=corrected_draft_id,
                draft_revision=next_revision,
                requested_from_user_id=target_source_manifest.get("sender_user_id"),
                timestamp=timestamp,
            )
            self._write_json(next_approval_key, next_approval_manifest)
            self._write_json(approval_latest_path(target_source_message_id), next_approval_pointer)

            target_source_manifest["state"] = "awaiting_approval"
            target_source_manifest["current_draft_id"] = corrected_draft_id
            target_source_manifest["current_draft_key"] = corrected_draft_key
            target_source_manifest["current_approval_id"] = next_approval_id
            target_source_manifest["current_approval_key"] = next_approval_key
            target_source_manifest["current_update_job_id"] = None
            target_source_manifest["current_update_job_key"] = None
            target_source_manifest["exception"] = None
            self._send_approval_prompt_for_draft(
                source_manifest=target_source_manifest,
                draft_manifest=corrected_draft,
                timestamp=timestamp,
            )
            manifest["correction_payload"] = correction_payload

        self._write_json(self.source_manifest_path(target_source_message_id), target_source_manifest)

        manifest["state"] = target_source_manifest["state"] if action == "correct" else new_approval_state
        manifest["current_draft_id"] = target_source_manifest.get("current_draft_id") or draft_manifest.get("draft_id")
        manifest["current_approval_id"] = target_source_manifest.get("current_approval_id") or approval_manifest.get("approval_id")
        manifest["current_update_job_id"] = update_job_id
        return manifest["state"], target_source_message_id

    def _maybe_send_image_acknowledgment(self, manifest: dict[str, Any]) -> None:
        if self.line_reply_sender is None:
            return

        reply_token = str(manifest.get("line_reply_token") or "").strip()
        if not reply_token:
            return

        if manifest.get("source_type") != "image":
            return

        if manifest.get("state") not in {"stored", "queued"}:
            return

        try:
            self.line_reply_sender(reply_token=reply_token, text=build_image_received_text())
        except Exception as exc:
            print(f"line intake: unable to send image acknowledgment: {exc}", file=sys.stderr)

    def _maybe_send_approval_acknowledgment(self, manifest: dict[str, Any]) -> None:
        if self.line_reply_sender is None:
            return

        reply_token = str(manifest.get("line_reply_token") or "").strip()
        if not reply_token:
            return

        if manifest.get("source_type") != "approval_command":
            return

        if manifest.get("state") != "approved":
            return

        try:
            self.line_reply_sender(reply_token=reply_token, text=build_approval_success_text())
        except Exception as exc:
            print(f"line intake: unable to send approval acknowledgment: {exc}", file=sys.stderr)

    def persist_line_event(self, event: dict[str, Any], received_at: str | None = None) -> ProcessedEvent:
        line_event_id = event.get("webhookEventId") or "unknown-event"
        source_message_id = source_message_id_for(line_event_id)
        source_type = detect_source_type(event)

        if self.has_seen_event(line_event_id):
            manifest = self.read_manifest(source_message_id) or {}
            return ProcessedEvent(
                source_message_id=source_message_id,
                line_event_id=line_event_id,
                state=manifest.get("state", "received"),
                deduplicated=True,
                source_type=manifest.get("source_type", source_type),
            )

        timestamp = received_at or utc_now_iso()
        source = event.get("source") or {}
        message = event.get("message") or {}
        workflow_session_id = stable_workflow_session_id(event)
        state = initial_state_for(source_type)
        line_message_id = message.get("id")
        source_text = message.get("text") if source_type in {"text", "approval_command", "correction_command"} else None
        session_pointer_source_message_id = self._session_anchor_source_message_id(workflow_session_id, source_message_id)

        manifest = {
            "source_message_id": source_message_id,
            "workflow_session_id": workflow_session_id,
            "platform": "line",
            "line_event_id": line_event_id,
            "line_message_id": line_message_id,
            "line_reply_token": event.get("replyToken"),
            "source_type": source_type,
            "source_text": source_text,
            "sender_user_id": source.get("userId"),
            "sender_group_id": source.get("groupId"),
            "sender_room_id": source.get("roomId"),
            "state": state,
            "dedupe_event_key": f"line:event:{line_event_id}",
            "dedupe_message_key": f"line:message:{line_message_id}" if line_message_id else None,
            "upload_session_id": None,
            "media": None,
            "current_draft_id": None,
            "current_approval_id": None,
            "current_update_job_id": None,
            "exception": None if state != "exception" else {"code": "UNSUPPORTED_EVENT", "message": "event type is not supported yet"},
            "created_at": timestamp,
            "updated_at": timestamp,
        }

        if source_type == "image":
            session_pointer_source_message_id = source_message_id
            try:
                upload_session = self.upload_service.store_source_message(manifest, received_at=timestamp)
                manifest["state"] = upload_session.state
                manifest["upload_session_id"] = upload_session.upload_session_id
                manifest["media"] = {
                    "bucket": upload_session.bucket,
                    "key": upload_session.object_key,
                    "metadata_key": upload_session.metadata_key,
                    "storage_backend": upload_session.storage_backend,
                }
                ocr_job_id = self._enqueue_ocr_job_if_configured(manifest, timestamp=timestamp)
                if ocr_job_id is not None:
                    manifest["state"] = "queued"
                    manifest["current_ocr_job_id"] = ocr_job_id
                manifest["updated_at"] = timestamp
            except UploadServiceError as exc:
                manifest["state"] = "exception"
                manifest["exception"] = {
                    "code": "UPLOAD_SESSION_FAILED",
                    "message": str(exc),
                    "status_code": exc.status_code,
                    "response_body": exc.response_body,
                }
        elif source_type in {"approval_command", "correction_command"}:
            manifest["state"], session_pointer_source_message_id = self._apply_approval_response(
                manifest=manifest,
                workflow_session_id=workflow_session_id,
                source_type=source_type,
                source_text=source_text,
                sender_user_id=source.get("userId"),
                timestamp=timestamp,
            )
        event_index = {
            "line_event_id": line_event_id,
            "source_message_id": source_message_id,
            "state": manifest["state"],
            "created_at": timestamp,
        }

        self._write_json(self.source_manifest_path(source_message_id), manifest)
        self._write_json(self.event_index_path(line_event_id), event_index)

        if line_message_id:
            message_index = {
                "line_message_id": line_message_id,
                "source_message_id": source_message_id,
                "state": manifest["state"],
                "created_at": timestamp,
            }
            self._write_json(self.message_index_path(line_message_id), message_index)

        session_pointer = {
            "workflow_session_id": workflow_session_id,
            "latest_source_message_id": locals().get("session_pointer_source_message_id", source_message_id),
            "source_type": source_type,
            "updated_at": timestamp,
        }
        self._write_json(self.session_pointer_path(workflow_session_id), session_pointer)
        self._maybe_send_image_acknowledgment(manifest)
        self._maybe_send_approval_acknowledgment(manifest)
        self._reply_text_for_active_approval(manifest)

        return ProcessedEvent(
            source_message_id=source_message_id,
            line_event_id=line_event_id,
            state=manifest["state"],
            deduplicated=False,
            source_type=source_type,
        )


def make_handler(store: LocalStateStore):
    class SupervisorHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/health":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            self._send_json(HTTPStatus.OK, {"status": "ok", "service": "supervisor-intake"})

        def do_POST(self) -> None:
            if self.path != "/line/events":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            raw_payload = self.rfile.read(content_length)

            try:
                payload = json.loads(raw_payload.decode("utf-8"))
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"code": "INVALID_JSON", "message": "request body must be valid json"}})
                return

            events = payload.get("events")
            if not isinstance(events, list):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"code": "INVALID_REQUEST", "message": "events must be an array"}})
                return

            processed = [store.persist_line_event(event) for event in events]
            response_payload = {
                "processed_count": len(processed),
                "new_count": sum(1 for item in processed if not item.deduplicated),
                "duplicate_count": sum(1 for item in processed if item.deduplicated),
                "results": [
                    {
                        "source_message_id": item.source_message_id,
                        "line_event_id": item.line_event_id,
                        "source_type": item.source_type,
                        "state": item.state,
                        "deduplicated": item.deduplicated,
                    }
                    for item in processed
                ],
            }
            self._send_json(HTTPStatus.OK, response_payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return SupervisorHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local supervisor LINE intake service")
    parser.add_argument(
        "--env-file",
        default=str(Path(__file__).with_name(".env")),
        help="optional .env file to preload before building the intake store",
    )
    parser.add_argument("--host", default=os.environ.get("SUPERVISOR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SUPERVISOR_PORT", "8650")))
    parser.add_argument(
        "--state-root",
        default=os.environ.get("SUPERVISOR_STATE_ROOT", str(Path("storage") / "local-state")),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_env_file(args.env_file)
    store = LocalStateStore(args.state_root)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store))
    print(f"supervisor intake listening on http://{args.host}:{args.port} with state root {args.state_root}")
    server.serve_forever()


if __name__ == "__main__":
    main()
