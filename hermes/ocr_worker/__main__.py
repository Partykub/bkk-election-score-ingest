from __future__ import annotations

import argparse
import base64
import json
import os
import re
import signal
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib import error, request

import boto3

from hermes.supervisor.intake_server import build_correction_form_url_for_source_manifest


@dataclass(frozen=True)
class WorkerConfig:
    hermes_base_url: str
    hermes_api_key: str
    hermes_model: str
    line_channel_access_token: str | None
    line_api_base_url: str
    queue_url: str | None
    aws_region: str | None
    s3_bucket: str | None
    s3_prefix: str | None
    model_name: str
    poll_seconds: int
    queue_max_messages: int
    queue_wait_seconds: int
    queue_visibility_timeout: int


@dataclass(frozen=True)
class QueueEnvelope:
    ocr_job_id: str
    manifest_bucket: str
    manifest_key: str


@dataclass(frozen=True)
class DownloadedJob:
    ocr_job_id: str
    source_message_id: str
    workflow_session_id: str
    manifest_bucket: str
    manifest_key: str
    input_bucket: str
    input_key: str
    input_size_bytes: int
    input_content_type: str | None
    queue_message_id: str | None
    receipt_handle: str | None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_")
    return cleaned or "unknown"


def draft_revision_key(source_message_id: str, revision: int) -> str:
    return f"messages/{source_message_id}/draft_r{revision}.json"


def draft_latest_key(source_message_id: str) -> str:
    return f"messages/{source_message_id}/draft_latest.json"


def approval_revision_key(source_message_id: str, revision: int) -> str:
    return f"messages/{source_message_id}/approval_r{revision}.json"


def approval_latest_key(source_message_id: str) -> str:
    return f"messages/{source_message_id}/approval_latest.json"


def source_manifest_key(source_message_id: str) -> str:
    return f"messages/{source_message_id}/manifest.json"


def is_missing_key_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    error_payload = response.get("Error", {}) if isinstance(response, dict) else {}
    error_code = str(error_payload.get("Code", "")).lower()
    return error_code in {"nosuchkey", "404", "notfound", "nosuchbucket"}


def build_config() -> WorkerConfig:
    queue_url = os.environ.get("OCR_WORKER_QUEUE_URL", "").strip() or None
    aws_region = os.environ.get("OCR_WORKER_AWS_REGION", "").strip() or None
    s3_bucket = os.environ.get("OCR_WORKER_S3_BUCKET", "").strip() or None
    s3_prefix = os.environ.get("OCR_WORKER_S3_PREFIX", "").strip() or None
    line_channel_access_token = (
        os.environ.get("OCR_WORKER_LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        or os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        or None
    )
    poll_seconds = int(os.environ.get("OCR_WORKER_POLL_SECONDS", "15"))
    queue_max_messages = int(os.environ.get("OCR_WORKER_QUEUE_MAX_MESSAGES", "1"))
    queue_wait_seconds = int(os.environ.get("OCR_WORKER_QUEUE_WAIT_SECONDS", "10"))
    queue_visibility_timeout = int(os.environ.get("OCR_WORKER_QUEUE_VISIBILITY_TIMEOUT", "300"))

    return WorkerConfig(
        hermes_base_url=os.environ.get("OCR_WORKER_HERMES_BASE_URL", "http://hermes-supervisor:8642").strip(),
        hermes_api_key=os.environ.get("OCR_WORKER_HERMES_API_KEY", "change-this-api-key").strip(),
        hermes_model=os.environ.get("OCR_WORKER_HERMES_MODEL", "hermes-agent").strip() or "hermes-agent",
        line_channel_access_token=line_channel_access_token,
        line_api_base_url=os.environ.get("OCR_WORKER_LINE_API_BASE_URL", "https://api.line.me").strip() or "https://api.line.me",
        queue_url=queue_url,
        aws_region=aws_region,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        model_name=os.environ.get("OCR_WORKER_MODEL_NAME", "gemma-vision").strip(),
        poll_seconds=max(1, poll_seconds),
        queue_max_messages=max(1, min(10, queue_max_messages)),
        queue_wait_seconds=max(0, min(20, queue_wait_seconds)),
        queue_visibility_timeout=max(1, queue_visibility_timeout),
    )


def source_message_id_from_ocr_job_id(ocr_job_id: str) -> str:
    cleaned = ocr_job_id
    if cleaned.startswith("ocr_"):
        cleaned = cleaned[4:]
    if not cleaned.startswith("src_"):
        cleaned = f"src_{cleaned}"
    return cleaned


def manifest_key_for_job(ocr_job_id: str) -> str:
    src_id = source_message_id_from_ocr_job_id(ocr_job_id)
    return f"messages/{src_id}/ocr_job.json"


def ocr_job_id_from_manifest_key(manifest_key: str) -> str:
    parts = manifest_key.split("/")
    if len(parts) >= 2 and parts[-1] == "ocr_job.json":
        return f"ocr_{parts[-2]}"
    return manifest_key.rsplit("/", 1)[-1].removesuffix(".json")


def with_s3_prefix(key: str, *, prefix: str | None) -> str:
    normalized_key = key.strip().lstrip("/")
    normalized_prefix = (prefix or "").strip().strip("/")
    if not normalized_prefix or normalized_key.startswith(f"{normalized_prefix}/"):
        return normalized_key
    return f"{normalized_prefix}/{normalized_key}"


def update_area_submissions(
    s3_client: Any,
    bucket: str,
    prefix: str | None,
    election_id: str,
    area_id: str,
    source_message_id: str,
    timestamp: str,
    old_area_id: str | None = None,
) -> None:
    election_id_safe = safe_id(election_id) if election_id else "default"

    if old_area_id and old_area_id != area_id:
        old_key = with_s3_prefix(
            f"indexes/by-area/{election_id_safe}/{safe_id(old_area_id)}/submissions.json",
            prefix=prefix,
        )
        old_data = read_json_object_if_exists(s3_client, bucket=bucket, key=old_key)
        if old_data:
            subs = old_data.get("submissions") or []
            new_subs = [s for s in subs if s.get("source_message_id") != source_message_id]
            old_data["submissions"] = new_subs
            old_data["submission_count"] = len(new_subs)
            old_data["updated_at"] = timestamp
            write_json_object(s3_client, bucket=bucket, key=old_key, payload=old_data)

    if area_id:
        new_key = with_s3_prefix(
            f"indexes/by-area/{election_id_safe}/{safe_id(area_id)}/submissions.json",
            prefix=prefix,
        )
        data = read_json_object_if_exists(s3_client, bucket=bucket, key=new_key)
        if not data:
            data = {
                "schema_version": "2026-06-09",
                "entity_type": "area_submissions",
                "election_id": election_id_safe,
                "area_id": area_id,
                "submission_count": 0,
                "submissions": [],
                "created_at": timestamp,
                "updated_at": timestamp,
            }

        subs = data.get("submissions") or []
        exists = any(s.get("source_message_id") == source_message_id for s in subs)
        if not exists:
            subs.append({
                "source_message_id": source_message_id,
                "submitted_at": timestamp,
            })
            data["submissions"] = subs
            data["submission_count"] = len(subs)
            data["updated_at"] = timestamp
            write_json_object(s3_client, bucket=bucket, key=new_key, payload=data)



def parse_queue_envelope(message_body: str, *, default_bucket: str | None) -> QueueEnvelope:
    raw_body = message_body.strip()
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        payload = parse_relaxed_object_body(raw_body) if raw_body.startswith("{") and raw_body.endswith("}") else raw_body

    if isinstance(payload, str):
        ocr_job_id = payload.strip()
        manifest_key = manifest_key_for_job(ocr_job_id)
        manifest_bucket = default_bucket
    elif isinstance(payload, dict):
        ocr_job_id = str(payload.get("ocr_job_id") or "").strip()
        manifest_key = str(payload.get("manifest_key") or "").strip()
        manifest_bucket = str(payload.get("manifest_bucket") or payload.get("bucket") or default_bucket or "").strip()
        if not manifest_key and ocr_job_id:
            manifest_key = manifest_key_for_job(ocr_job_id)
        if manifest_key and not ocr_job_id:
            ocr_job_id = ocr_job_id_from_manifest_key(manifest_key)
    else:
        raise ValueError("OCR queue message must be a string or JSON object")

    if not ocr_job_id:
        raise ValueError("OCR queue message is missing ocr_job_id")
    if not manifest_key:
        raise ValueError("OCR queue message is missing manifest_key")
    if not manifest_bucket:
        raise ValueError("OCR queue message is missing manifest_bucket and OCR_WORKER_S3_BUCKET is not set")

    return QueueEnvelope(
        ocr_job_id=ocr_job_id,
        manifest_bucket=manifest_bucket,
        manifest_key=manifest_key,
    )


def parse_relaxed_object_body(raw_body: str) -> dict[str, str] | str:
    inner_body = raw_body.strip()[1:-1].strip()
    if not inner_body:
        return raw_body

    payload: dict[str, str] = {}
    for fragment in inner_body.split(","):
        if ":" not in fragment:
            return raw_body
        key, value = fragment.split(":", 1)
        normalized_key = key.strip().strip('"\'')
        normalized_value = value.strip().strip('"\'')
        if not normalized_key:
            return raw_body
        payload[normalized_key] = normalized_value
    return payload


def read_json_object(s3_client: Any, *, bucket: str, key: str) -> dict[str, Any]:
    response = s3_client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    raw_payload = body.read() if hasattr(body, "read") else body
    return json.loads(raw_payload.decode("utf-8"))


def write_json_object(s3_client: Any, *, bucket: str, key: str, payload: dict[str, Any]) -> None:
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        ContentType="application/json",
    )


def read_json_object_if_exists(s3_client: Any, *, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return read_json_object(s3_client, bucket=bucket, key=key)
    except Exception as exc:
        if is_missing_key_error(exc):
            return None
        raise


def read_binary_object(s3_client: Any, *, bucket: str, key: str) -> tuple[bytes, str | None]:
    response = s3_client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    raw_payload = body.read() if hasattr(body, "read") else body
    return raw_payload, response.get("ContentType")


def fetch_job_from_queue_message(message: dict[str, Any], *, s3_client: Any, config: WorkerConfig) -> DownloadedJob:
    envelope = parse_queue_envelope(message.get("Body", ""), default_bucket=config.s3_bucket)
    candidate_manifest_keys = []
    deterministic_manifest_key = with_s3_prefix(manifest_key_for_job(envelope.ocr_job_id), prefix=config.s3_prefix)
    for candidate in (deterministic_manifest_key, with_s3_prefix(envelope.manifest_key, prefix=config.s3_prefix), envelope.manifest_key.strip()):
        if candidate and candidate not in candidate_manifest_keys:
            candidate_manifest_keys.append(candidate)

    manifest = None
    manifest_key = deterministic_manifest_key
    for candidate_manifest_key in candidate_manifest_keys:
        try:
            manifest = read_json_object(s3_client, bucket=envelope.manifest_bucket, key=candidate_manifest_key)
            manifest_key = candidate_manifest_key
            break
        except Exception as exc:
            if is_missing_key_error(exc):
                continue
            raise

    if manifest is None:
        raise FileNotFoundError(
            f"Unable to locate OCR manifest for {envelope.ocr_job_id} in bucket {envelope.manifest_bucket}. Tried keys: {candidate_manifest_keys}"
        )
    input_payload = manifest.get("input") or {}
    input_bucket = str(input_payload.get("bucket") or envelope.manifest_bucket).strip()
    input_key = with_s3_prefix(str(input_payload.get("key") or "").strip(), prefix=config.s3_prefix)
    if not input_bucket:
        raise ValueError(f"OCR job {envelope.ocr_job_id} is missing input.bucket")
    if not input_key:
        raise ValueError(f"OCR job {envelope.ocr_job_id} is missing input.key")

    input_body, content_type = read_binary_object(s3_client, bucket=input_bucket, key=input_key)
    return DownloadedJob(
        ocr_job_id=envelope.ocr_job_id,
        source_message_id=str(manifest.get("source_message_id") or "").strip(),
        workflow_session_id=str(manifest.get("workflow_session_id") or "").strip(),
        manifest_bucket=envelope.manifest_bucket,
        manifest_key=manifest_key,
        input_bucket=input_bucket,
        input_key=input_key,
        input_size_bytes=len(input_body),
        input_content_type=content_type or input_payload.get("content_type"),
        queue_message_id=message.get("MessageId"),
        receipt_handle=message.get("ReceiptHandle"),
    )


def poll_queue_once(*, queue_client: Any, s3_client: Any, config: WorkerConfig) -> list[DownloadedJob]:
    if not config.queue_url:
        return []

    response = queue_client.receive_message(
        QueueUrl=config.queue_url,
        MaxNumberOfMessages=config.queue_max_messages,
        WaitTimeSeconds=config.queue_wait_seconds,
        VisibilityTimeout=config.queue_visibility_timeout,
    )
    messages = response.get("Messages", [])
    downloaded_jobs: list[DownloadedJob] = []

    for message in messages:
        downloaded_job = fetch_job_from_queue_message(message, s3_client=s3_client, config=config)
        downloaded_jobs.append(downloaded_job)

    return downloaded_jobs


def acknowledge_job(*, queue_client: Any, config: WorkerConfig, downloaded_job: DownloadedJob) -> None:
    if not config.queue_url or not downloaded_job.receipt_handle:
        return
    queue_client.delete_message(QueueUrl=config.queue_url, ReceiptHandle=downloaded_job.receipt_handle)


def max_attempts_for_job(ocr_job_manifest: dict[str, Any]) -> int:
    try:
        return max(1, int(ocr_job_manifest.get("max_attempts") or 5))
    except (TypeError, ValueError):
        return 5


def is_retryable_ocr_error_message(message: str) -> bool:
    normalized = (message or "").strip().lower()
    if not normalized:
        return False
    retryable_fragments = (
        "remote end closed connection without response",
        "connection reset",
        "connection aborted",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "too many requests",
    )
    if any(fragment in normalized for fragment in retryable_fragments):
        return True
    return bool(re.search(r"status\s+(429|500|502|503|504)\b", normalized))


def should_acknowledge_result(result_payload: dict[str, Any]) -> bool:
    return str(result_payload.get("status") or "").strip() != "retry_pending"


def extract_first_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("Hermes OCR response did not contain a valid JSON object")


CANDIDATE_NUMBER_KEYS = ("candidate_number", "candiate_number")


def normalize_candidate_scores(raw_scores: Any) -> list[dict[str, Any]]:
    normalized_scores: list[dict[str, Any]] = []
    if not isinstance(raw_scores, list):
        return normalized_scores

    for item in raw_scores:
        if not isinstance(item, dict):
            continue

        candidate_number = None
        for key in CANDIDATE_NUMBER_KEYS:
            if item.get(key) not in {None, ""}:
                candidate_number = item.get(key)
                break
        try:
            candidate_number = int(candidate_number) if candidate_number is not None else None
        except (TypeError, ValueError):
            candidate_number = None

        score = item.get("score")
        if isinstance(score, str):
            score = score.replace(",", "").strip()
        try:
            score = int(score) if score not in {None, ""} else None
        except (TypeError, ValueError):
            score = None

        confidence = item.get("confidence")
        try:
            confidence = round(float(confidence), 4) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None

        normalized_scores.append(
            {
                "candidate_number": candidate_number,
                "candidate_name": item.get("candidate_name"),
                "score": score,
                "confidence": confidence,
                "raw_text": item.get("raw_text"),
            }
        )

    return normalized_scores


def ensure_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def build_result_signature(candidate_scores: list[dict[str, Any]], area_id: str | None) -> str | None:
    valid_scores = [score for score in candidate_scores if score.get("candidate_number") is not None and score.get("score") is not None]
    if not valid_scores:
        return None
    prefix = area_id or "unknown-area"
    fragments = [f"{score['candidate_number']}={score['score']}" for score in sorted(valid_scores, key=lambda item: item["candidate_number"])]
    return f"{prefix}:" + "|".join(fragments)


def detect_candidate_score_normalization_warnings(raw_scores: Any, normalized_scores: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    if not isinstance(raw_scores, list):
        return warnings

    for raw_item, normalized_item in zip(raw_scores, normalized_scores):
        if not isinstance(raw_item, dict):
            continue
        used_alias_key = raw_item.get("candidate_number") in {None, ""} and raw_item.get("candiate_number") not in {None, ""}
        if used_alias_key:
            warnings.append("candidate_number_alias_used")
        if normalized_item.get("candidate_number") is None and raw_item.get("score") not in {None, ""}:
            warnings.append("candidate_number_unreadable")

    return sorted(set(warnings))


def build_runtime_config_log(config: WorkerConfig) -> dict[str, Any]:
    config_payload = asdict(config)
    if config_payload.get("hermes_api_key"):
        config_payload["hermes_api_key"] = "***redacted***"
    if config_payload.get("line_channel_access_token"):
        config_payload["line_channel_access_token"] = "***redacted***"
    return config_payload


def line_destination_id_for_source_manifest(source_manifest: dict[str, Any]) -> str | None:
    for key in ("sender_group_id", "sender_room_id", "sender_user_id"):
        value = str(source_manifest.get(key) or "").strip()
        if value:
            return value
    return None


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
    lines.append("ตอบ 'แก้ไข' เพื่อเริ่มแก้ข้อมูล หรือพิมพ์ เช่น 'แก้ไข 4=14'")
    lines.append("ตอบ 'ไม่ถูกต้อง' หากต้องการปฏิเสธร่างนี้")
    return "\n".join(lines)

def build_line_text_message(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text[:5000]}


def build_line_correction_liff_url() -> str | None:
    liff_id = os.environ.get("LINE_LIFF_CORRECTION_ID", "").strip()
    if not liff_id:
        return None
    return f"https://liff.line.me/{liff_id}"


def build_approval_quick_reply_items(*, correction_url: str | None = None) -> list[dict[str, Any]]:
    return [
        {
            "type": "action",
            "imageUrl": None,
            "action": {"type": "message", "label": "ยืนยัน", "text": "ยืนยัน"},
        },
        {
            "type": "action",
            "imageUrl": None,
            "action": {"type": "message", "label": "แก้ไข", "text": "แก้ไข"},
        },
        {
            "type": "action",
            "imageUrl": None,
            "action": {"type": "message", "label": "ไม่ถูกต้อง", "text": "ไม่ถูกต้อง"},
        },
    ]

def build_approval_action_messages(text: str, *, correction_url: str | None = None) -> list[dict[str, Any]]:
    message = build_line_text_message(text)
    message["quickReply"] = {"items": build_approval_quick_reply_items(correction_url=correction_url)}
    return [message]


def send_line_push_message(
    *,
    channel_access_token: str,
    destination_id: str,
    text: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    api_base_url: str = "https://api.line.me",
    opener: Any = request.urlopen,
) -> None:
    payload_messages = messages or [build_line_text_message(text or "")]
    line_request = request.Request(
        f"{api_base_url.rstrip('/')}/v2/bot/message/push",
        data=json.dumps(
            {
                "to": destination_id,
                "messages": payload_messages,
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


def maybe_send_approval_prompt(
    *,
    s3_client: Any,
    bucket: str,
    source_manifest_path: str,
    source_manifest: dict[str, Any] | None,
    draft_manifest: dict[str, Any] | None,
    config: WorkerConfig,
    timestamp: str,
    opener: Any = request.urlopen,
) -> dict[str, Any] | None:
    if source_manifest is None or draft_manifest is None:
        return None

    approval_prompt = source_manifest.get("approval_prompt") or {}
    if approval_prompt.get("status") == "sent" and approval_prompt.get("draft_id") == draft_manifest.get("draft_id"):
        return approval_prompt

    if not config.line_channel_access_token:
        source_manifest["approval_prompt"] = {
            "status": "skipped",
            "reason": "missing_line_channel_access_token",
            "draft_id": draft_manifest.get("draft_id"),
            "updated_at": timestamp,
        }
        write_json_object(s3_client, bucket=bucket, key=source_manifest_path, payload=source_manifest)
        return source_manifest["approval_prompt"]

    destination_id = line_destination_id_for_source_manifest(source_manifest)
    if not destination_id:
        source_manifest["approval_prompt"] = {
            "status": "skipped",
            "reason": "missing_line_destination",
            "draft_id": draft_manifest.get("draft_id"),
            "updated_at": timestamp,
        }
        write_json_object(s3_client, bucket=bucket, key=source_manifest_path, payload=source_manifest)
        return source_manifest["approval_prompt"]

    try:
        correction_url = build_correction_form_url_for_source_manifest(source_manifest)
        send_line_push_message(
            channel_access_token=config.line_channel_access_token,
            destination_id=destination_id,
            messages=build_approval_action_messages(build_approval_prompt_text(draft_manifest), correction_url=correction_url),
            api_base_url=config.line_api_base_url,
            opener=opener,
        )
        source_manifest["approval_prompt"] = {
            "status": "sent",
            "draft_id": draft_manifest.get("draft_id"),
            "destination_id": destination_id,
            "message_type": "push",
            "sent_at": timestamp,
            "updated_at": timestamp,
        }
    except Exception as exc:
        source_manifest["approval_prompt"] = {
            "status": "failed",
            "draft_id": draft_manifest.get("draft_id"),
            "destination_id": destination_id,
            "message_type": "push",
            "error": str(exc),
            "updated_at": timestamp,
        }

    write_json_object(s3_client, bucket=bucket, key=source_manifest_path, payload=source_manifest)
    return source_manifest["approval_prompt"]


def advance_session_pointer_on_failure(
    *,
    s3_client: Any,
    bucket: str,
    prefix: str,
    workflow_session_id: str,
    failed_source_message_id: str,
    timestamp: str,
) -> dict[str, Any] | None:
    """Clear active_review_source_message_id when OCR fails, so next image doesn't queue behind a dead item."""
    if not workflow_session_id:
        return None

    session_pointer_key = with_s3_prefix(
        f"sessions/{safe_id(workflow_session_id)}/latest.json",
        prefix=prefix,
    )
    session_pointer = read_json_object_if_exists(s3_client, bucket=bucket, key=session_pointer_key)
    if session_pointer is None:
        return None

    active_review_id = str(session_pointer.get("active_review_source_message_id") or "").strip()
    if active_review_id != failed_source_message_id:
        # The failed item is not the active review, check if it's in the pending queue
        pending_queue = list(session_pointer.get("pending_review_queue") or [])
        if failed_source_message_id in pending_queue:
            pending_queue.remove(failed_source_message_id)
            session_pointer["pending_review_queue"] = pending_queue
            session_pointer["updated_at"] = timestamp
            write_json_object(s3_client, bucket=bucket, key=session_pointer_key, payload=session_pointer)
        return session_pointer

    # The failed item IS the active review — advance the queue
    pending_queue = list(session_pointer.get("pending_review_queue") or [])
    completed_count = int(session_pointer.get("completed_review_count") or 0)

    if pending_queue:
        next_id = pending_queue.pop(0)
        session_pointer["active_review_source_message_id"] = next_id
    else:
        session_pointer["active_review_source_message_id"] = None

    session_pointer["pending_review_queue"] = pending_queue
    session_pointer["completed_review_count"] = completed_count
    session_pointer["updated_at"] = timestamp
    write_json_object(s3_client, bucket=bucket, key=session_pointer_key, payload=session_pointer)
    return session_pointer


def maybe_send_ocr_failure_notice(
    *,
    s3_client: Any,
    bucket: str,
    source_manifest_path: str,
    source_manifest: dict[str, Any] | None,
    config: WorkerConfig,
    timestamp: str,
    opener: Any = request.urlopen,
) -> dict[str, Any] | None:
    if source_manifest is None:
        return None

    destination_id = line_destination_id_for_source_manifest(source_manifest)
    if not config.line_channel_access_token or not destination_id:
        source_manifest["ocr_failure_notice"] = {
            "status": "skipped",
            "reason": "missing_line_channel_access_token" if not config.line_channel_access_token else "missing_line_destination",
            "updated_at": timestamp,
        }
        write_json_object(s3_client, bucket=bucket, key=source_manifest_path, payload=source_manifest)
        return source_manifest["ocr_failure_notice"]

    try:
        send_line_push_message(
            channel_access_token=config.line_channel_access_token,
            destination_id=destination_id,
            text="ขออภัย ระบบอ่านรูปนี้ไม่สำเร็จ กรุณาส่งรูปเดิมอีกครั้ง หรือลองถ่ายใหม่ให้ชัดขึ้น",
            api_base_url=config.line_api_base_url,
            opener=opener,
        )
        source_manifest["ocr_failure_notice"] = {
            "status": "sent",
            "destination_id": destination_id,
            "message_type": "push",
            "sent_at": timestamp,
            "updated_at": timestamp,
        }
    except Exception as exc:
        source_manifest["ocr_failure_notice"] = {
            "status": "failed",
            "destination_id": destination_id,
            "message_type": "push",
            "error": str(exc),
            "updated_at": timestamp,
        }

    write_json_object(s3_client, bucket=bucket, key=source_manifest_path, payload=source_manifest)
    return source_manifest["ocr_failure_notice"]


def build_ocr_prompt(*, downloaded_job: DownloadedJob, model_name: str, prompt_version: str) -> str:
    return (
        "คุณคือผู้ช่วยดึงข้อมูลจากรูปภาพใบนับคะแนนเลือกตั้ง (ภาษาไทย) "
        "กรุณาถอดข้อความและตัวเลขทั้งหมดที่เห็นในรูปภาพ โดยเฉพาะข้อความที่เขียนด้วยลายมือบนหัวกระดาษ เช่น 'เขต 3' หรือบางครั้งอาจจะอ่านคล้ายๆ 'เลข 3', '69' ขอให้ตีความว่าเป็นหมายเลขเขต (area_id) "
        "ให้หาตัวเลขคะแนนของผู้สมัครแต่ละคน และนำข้อมูลทั้งหมดมาจัดเรียงในรูปแบบ JSON ตามโครงสร้างด้านล่างนี้เท่านั้น ห้ามพิมพ์ข้อความอธิบายใดๆ เพิ่มเติม\n\n"
        "ข้อควรระวัง: สังเกตข้อความมุมซ้ายบนหรือขวาบนของกระดาษให้ดี หากพบตัวเลขเดี่ยวๆ หรือคำว่า 'เขต' หรือ 'เลข' ตามด้วยตัวเลข ให้ใส่ตัวเลขนั้นในช่อง 'area_id' ทันที (เช่น เขต 3 -> area_id: \"3\")\n\n"
        "ตัวอย่างถ้าในรูปมีข้อความ 'เขต 3' และมีคะแนนเบอร์ 1 ได้ 120 คะแนน ให้ตอบ JSON แบบนี้:\n"
        "{\n"
        '  "document_type": "election_score_sheet",\n'
        '  "summary_text": "พบคะแนนและระบุเขต 3",\n'
        '  "election_id": null,\n'
        '  "area_id": "3",\n'
        '  "polling_unit_id": null,\n'
        '  "observed_at": null,\n'
        '  "overall_confidence": 0.95,\n'
        '  "validation_flags": [],\n'
        '  "image_quality_flags": [],\n'
        '  "notes": "เห็นเขต 3 ที่หัวกระดาษ",\n'
        '  "candidate_scores": [\n'
        "    {\n"
        '      "candidate_number": 1,\n'
        '      "candidate_name": null,\n'
        '      "score": 120,\n'
        '      "confidence": 0.95,\n'
        '      "raw_text": "1 120"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "ตอนนี้ตาคุณแล้ว คืนค่าเฉพาะข้อมูล JSON ของรูปภาพนี้ ห้ามมี Markdown หรือคำอธิบายเพิ่มเติม:\n"
        "{\n"
        '  "document_type": "election_score_sheet" | "other",\n'
        '  "summary_text": string,\n'
        '  "election_id": string | null,\n'
        '  "area_id": string | null,\n'
        '  "polling_unit_id": string | null,\n'
        '  "observed_at": string | null,\n'
        '  "overall_confidence": number,\n'
        '  "validation_flags": string[],\n'
        '  "image_quality_flags": string[],\n'
        '  "notes": string | null,\n'
        '  "candidate_scores": [\n'
        "    {\n"
        '      "candidate_number": number | null,\n'
        '      "candidate_name": string | null,\n'
        '      "score": number | null,\n'
        '      "confidence": number | null,\n'
        '      "raw_text": string | null\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        f"Workflow session: {downloaded_job.workflow_session_id or 'unknown'}\n"
        f"Source message ID: {downloaded_job.source_message_id}\n"
        f"Requested OCR model label: {model_name}\n"
        f"Prompt version: {prompt_version}"
    )


def call_hermes_ocr(*, config: WorkerConfig, image_bytes: bytes, mime_type: str, prompt_text: str) -> dict[str, Any]:
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    payload = {
        "model": config.hermes_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    req = request.Request(
        f"{config.hermes_base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.hermes_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=180) as response:
            raw_body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Hermes API request failed with status {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Hermes API request failed: {exc}") from exc
    return json.loads(raw_body)


def extract_assistant_text(response_payload: dict[str, Any]) -> str:
    choices = response_payload.get("choices") or []
    if not choices:
        raise ValueError("Hermes response did not contain any choices")

    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                chunks.append(str(item.get("text") or ""))
        return "\n".join(chunk for chunk in chunks if chunk).strip()
    raise ValueError("Hermes response content was empty")


def build_draft_documents(
    *,
    downloaded_job: DownloadedJob,
    ocr_job_manifest: dict[str, Any],
    normalized_payload: dict[str, Any],
    raw_model_text: str,
    revision: int,
    timestamp: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    draft_id = f"draft_{downloaded_job.source_message_id}_r{revision}"
    draft_key = draft_revision_key(downloaded_job.source_message_id, revision)
    latest_key = draft_latest_key(downloaded_job.source_message_id)
    raw_candidate_scores = normalized_payload.get("candidate_scores")
    candidate_scores = normalize_candidate_scores(raw_candidate_scores)
    validation_flags = ensure_string_list(normalized_payload.get("validation_flags"))
    image_quality_flags = ensure_string_list(normalized_payload.get("image_quality_flags"))
    normalization_warnings = detect_candidate_score_normalization_warnings(raw_candidate_scores, candidate_scores)

    if not candidate_scores:
        validation_flags = sorted(set(validation_flags + ["missing_candidate_scores", "requires_human_review"]))
    if normalization_warnings:
        validation_flags = sorted(set(validation_flags + normalization_warnings))

    overall_confidence_raw = normalized_payload.get("overall_confidence")
    try:
        overall_confidence = round(float(overall_confidence_raw), 4)
    except (TypeError, ValueError):
        overall_confidence = 0.0
        validation_flags = sorted(set(validation_flags + ["low_confidence", "requires_human_review"]))

    area_id = normalized_payload.get("area_id")
    result_signature = build_result_signature(candidate_scores, str(area_id).strip() if area_id else None)
    prompt_version = str((ocr_job_manifest.get("ocr_options") or {}).get("prompt_version") or "ocr-v1")

    draft_manifest = {
        "schema_version": "2026-06-09",
        "entity_type": "draft",
        "entity_id": draft_id,
        "draft_id": draft_id,
        "source_message_id": downloaded_job.source_message_id,
        "ocr_job_id": downloaded_job.ocr_job_id,
        "revision": revision,
        "status": "awaiting_approval",
        "election_id": normalized_payload.get("election_id"),
        "area_id": area_id,
        "polling_unit_id": normalized_payload.get("polling_unit_id"),
        "report_type": normalized_payload.get("document_type") or "score_sheet",
        "observed_at": normalized_payload.get("observed_at"),
        "result_signature": result_signature,
        "overall_confidence": overall_confidence,
        "validation_flags": validation_flags,
        "image_quality_flags": image_quality_flags,
        "candidate_scores": candidate_scores,
        "normalization_warnings": normalization_warnings,
        "notes": normalized_payload.get("notes") or normalized_payload.get("summary_text"),
        "raw_model_output": {"text": raw_model_text},
        "model_name": str((ocr_job_manifest.get("ocr_options") or {}).get("model_name") or "gemma-vision"),
        "prompt_version": prompt_version,
        "created_by": "ocr-worker",
        "created_at": timestamp,
        "updated_at": timestamp,
    }

    latest_pointer = {
        "schema_version": "2026-06-09",
        "entity_type": "draft_pointer",
        "entity_id": downloaded_job.source_message_id,
        "source_message_id": downloaded_job.source_message_id,
        "draft_id": draft_id,
        "draft_key": draft_key,
        "ocr_job_id": downloaded_job.ocr_job_id,
        "revision": revision,
        "updated_at": timestamp,
    }

    result_block = {
        "draft_id": draft_id,
        "draft_key": draft_key,
        "draft_latest_key": latest_key,
        "overall_confidence": overall_confidence,
        "validation_flags": validation_flags,
    }
    return draft_manifest, latest_pointer, result_block


def build_approval_documents(
    *,
    downloaded_job: DownloadedJob,
    draft_manifest: dict[str, Any],
    source_manifest: dict[str, Any] | None,
    timestamp: str,
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    revision = int(draft_manifest.get("revision") or 1)
    approval_id = f"approval_{downloaded_job.source_message_id}_r{revision}"
    approval_key = approval_revision_key(downloaded_job.source_message_id, revision)
    latest_key = approval_latest_key(downloaded_job.source_message_id)
    requested_from_user_id = None
    if isinstance(source_manifest, dict):
        requested_from_user_id = source_manifest.get("sender_user_id")

    approval_manifest = {
        "schema_version": "2026-06-09",
        "entity_type": "approval",
        "entity_id": approval_id,
        "approval_id": approval_id,
        "source_message_id": downloaded_job.source_message_id,
        "draft_id": draft_manifest["draft_id"],
        "draft_revision": revision,
        "workflow_session_id": downloaded_job.workflow_session_id,
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

    latest_pointer = {
        "schema_version": "2026-06-09",
        "entity_type": "approval_pointer",
        "entity_id": downloaded_job.source_message_id,
        "source_message_id": downloaded_job.source_message_id,
        "approval_id": approval_id,
        "approval_key": approval_key,
        "draft_id": draft_manifest["draft_id"],
        "draft_revision": revision,
        "state": "awaiting_approval",
        "updated_at": timestamp,
    }
    return approval_manifest, latest_pointer, approval_id, approval_key


def ensure_approval_artifacts(
    *,
    s3_client: Any,
    manifest_bucket: str,
    source_manifest: dict[str, Any] | None,
    downloaded_job: DownloadedJob,
    draft_manifest: dict[str, Any] | None,
    prefix: str | None,
    timestamp: str,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    if source_manifest is None or draft_manifest is None:
        return None, None, None

    approval_key = str(source_manifest.get("current_approval_key") or "").strip()
    approval_manifest = None
    if approval_key:
        approval_manifest = read_json_object_if_exists(s3_client, bucket=manifest_bucket, key=approval_key)
    if approval_manifest is None:
        pointer = read_json_object_if_exists(
            s3_client,
            bucket=manifest_bucket,
            key=with_s3_prefix(approval_latest_key(downloaded_job.source_message_id), prefix=prefix),
        )
        pointer_key = str((pointer or {}).get("approval_key") or "").strip()
        if pointer_key:
            approval_manifest = read_json_object_if_exists(
                s3_client,
                bucket=manifest_bucket,
                key=with_s3_prefix(pointer_key, prefix=prefix),
            )

    expected_revision = int(draft_manifest.get("revision") or 1)
    if approval_manifest is not None and int(approval_manifest.get("draft_revision") or 0) == expected_revision:
        approval_key = with_s3_prefix(
            str(approval_manifest.get("approval_key") or approval_key or approval_revision_key(downloaded_job.source_message_id, expected_revision)),
            prefix=prefix,
        )
        return approval_manifest, approval_manifest.get("approval_id"), approval_key

    approval_manifest, latest_pointer, approval_id, approval_key = build_approval_documents(
        downloaded_job=downloaded_job,
        draft_manifest=draft_manifest,
        source_manifest=source_manifest,
        timestamp=timestamp,
    )
    approval_key_with_prefix = with_s3_prefix(approval_key, prefix=prefix)
    latest_key_with_prefix = with_s3_prefix(approval_latest_key(downloaded_job.source_message_id), prefix=prefix)
    write_json_object(s3_client, bucket=manifest_bucket, key=approval_key_with_prefix, payload=approval_manifest)
    write_json_object(s3_client, bucket=manifest_bucket, key=latest_key_with_prefix, payload=latest_pointer)
    return approval_manifest, approval_id, approval_key_with_prefix


def next_draft_revision(*, s3_client: Any, bucket: str, source_message_id: str, prefix: str | None) -> int:
    latest_pointer = read_json_object_if_exists(s3_client, bucket=bucket, key=with_s3_prefix(draft_latest_key(source_message_id), prefix=prefix))
    if latest_pointer is None:
        return 1
    try:
        return int(latest_pointer.get("revision") or 0) + 1
    except (TypeError, ValueError):
        return 1


def persist_failure(
    *,
    s3_client: Any,
    downloaded_job: DownloadedJob,
    prefix: str | None,
    ocr_job_manifest: dict[str, Any] | None,
    source_manifest: dict[str, Any] | None,
    code: str,
    message: str,
    timestamp: str,
) -> None:
    if ocr_job_manifest is not None:
        ocr_job_manifest["state"] = "failed"
        ocr_job_manifest["error"] = {"code": code, "message": message}
        ocr_job_manifest["updated_at"] = timestamp
        ocr_job_manifest["attempt_count"] = int(ocr_job_manifest.get("attempt_count") or 0)
        write_json_object(s3_client, bucket=downloaded_job.manifest_bucket, key=downloaded_job.manifest_key, payload=ocr_job_manifest)

    if source_manifest is not None:
        source_manifest["state"] = "exception"
        source_manifest["exception"] = {"code": code, "message": message}
        source_manifest["updated_at"] = timestamp
        write_json_object(
            s3_client,
            bucket=downloaded_job.manifest_bucket,
            key=with_s3_prefix(source_manifest_key(downloaded_job.source_message_id), prefix=prefix),
            payload=source_manifest,
        )


def persist_retry_pending(
    *,
    s3_client: Any,
    downloaded_job: DownloadedJob,
    prefix: str | None,
    ocr_job_manifest: dict[str, Any] | None,
    source_manifest: dict[str, Any] | None,
    code: str,
    message: str,
    timestamp: str,
) -> None:
    if ocr_job_manifest is not None:
        ocr_job_manifest["state"] = "queued"
        ocr_job_manifest["error"] = {"code": code, "message": message, "retryable": True}
        ocr_job_manifest["updated_at"] = timestamp
        write_json_object(s3_client, bucket=downloaded_job.manifest_bucket, key=downloaded_job.manifest_key, payload=ocr_job_manifest)

    if source_manifest is not None:
        source_manifest["state"] = "queued"
        source_manifest["exception"] = None
        source_manifest["ocr_retry"] = {
            "status": "pending",
            "code": code,
            "message": message,
            "attempt_count": int((ocr_job_manifest or {}).get("attempt_count") or 0),
            "max_attempts": max_attempts_for_job(ocr_job_manifest or {}),
            "updated_at": timestamp,
        }
        source_manifest["updated_at"] = timestamp
        write_json_object(
            s3_client,
            bucket=downloaded_job.manifest_bucket,
            key=with_s3_prefix(source_manifest_key(downloaded_job.source_message_id), prefix=prefix),
            payload=source_manifest,
        )


def process_downloaded_job(*, downloaded_job: DownloadedJob, s3_client: Any, config: WorkerConfig) -> dict[str, Any]:
    timestamp = utc_now_iso()
    ocr_job_manifest = read_json_object(s3_client, bucket=downloaded_job.manifest_bucket, key=downloaded_job.manifest_key)
    source_manifest_path = with_s3_prefix(source_manifest_key(downloaded_job.source_message_id), prefix=config.s3_prefix)
    source_manifest = read_json_object_if_exists(s3_client, bucket=downloaded_job.manifest_bucket, key=source_manifest_path)

    if (ocr_job_manifest.get("state") == "completed") and isinstance(ocr_job_manifest.get("result"), dict):
        notification_result = None
        draft_key = ""
        if source_manifest is not None:
            draft_key = str(source_manifest.get("current_draft_key") or "").strip()
        if not draft_key:
            draft_key = str((ocr_job_manifest.get("result") or {}).get("draft_key") or "").strip()
        if draft_key:
            draft_manifest = read_json_object_if_exists(
                s3_client,
                bucket=downloaded_job.manifest_bucket,
                key=with_s3_prefix(draft_key, prefix=config.s3_prefix),
            )
            approval_manifest, approval_id, approval_key = ensure_approval_artifacts(
                s3_client=s3_client,
                manifest_bucket=downloaded_job.manifest_bucket,
                source_manifest=source_manifest,
                downloaded_job=downloaded_job,
                draft_manifest=draft_manifest,
                prefix=config.s3_prefix,
                timestamp=timestamp,
            )
            if source_manifest is not None and approval_id and approval_key:
                source_manifest["current_approval_id"] = approval_id
                source_manifest["current_approval_key"] = approval_key
                source_manifest["updated_at"] = timestamp
                write_json_object(s3_client, bucket=downloaded_job.manifest_bucket, key=source_manifest_path, payload=source_manifest)
            notification_result = maybe_send_approval_prompt(
                s3_client=s3_client,
                bucket=downloaded_job.manifest_bucket,
                source_manifest_path=source_manifest_path,
                source_manifest=source_manifest,
                draft_manifest=draft_manifest,
                config=config,
                timestamp=timestamp,
            )
        return {
            "service": "ocr-worker",
            "status": "completed",
            "ocr_job_id": downloaded_job.ocr_job_id,
            "source_message_id": downloaded_job.source_message_id,
            "result": ocr_job_manifest.get("result"),
            "idempotent": True,
            "approval_prompt": notification_result,
        }

    ocr_job_manifest["state"] = "processing"
    ocr_job_manifest["attempt_count"] = int(ocr_job_manifest.get("attempt_count") or 0) + 1
    ocr_job_manifest["updated_at"] = timestamp
    write_json_object(s3_client, bucket=downloaded_job.manifest_bucket, key=downloaded_job.manifest_key, payload=ocr_job_manifest)

    if source_manifest is not None:
        source_manifest["state"] = "ocr_processing"
        source_manifest["updated_at"] = timestamp
        write_json_object(s3_client, bucket=downloaded_job.manifest_bucket, key=source_manifest_path, payload=source_manifest)

    try:
        image_bytes, content_type = read_binary_object(s3_client, bucket=downloaded_job.input_bucket, key=downloaded_job.input_key)
        prompt_version = str((ocr_job_manifest.get("ocr_options") or {}).get("prompt_version") or "ocr-v1")
        prompt_text = build_ocr_prompt(downloaded_job=downloaded_job, model_name=config.model_name, prompt_version=prompt_version)
        hermes_response = call_hermes_ocr(
            config=config,
            image_bytes=image_bytes,
            mime_type=content_type or downloaded_job.input_content_type or "image/jpeg",
            prompt_text=prompt_text,
        )
        raw_model_text = extract_assistant_text(hermes_response)
        normalized_payload = extract_first_json_object(raw_model_text)

        revision = next_draft_revision(
            s3_client=s3_client,
            bucket=downloaded_job.manifest_bucket,
            source_message_id=downloaded_job.source_message_id,
            prefix=config.s3_prefix,
        )
        draft_manifest, latest_pointer, result_block = build_draft_documents(
            downloaded_job=downloaded_job,
            ocr_job_manifest=ocr_job_manifest,
            normalized_payload=normalized_payload,
            raw_model_text=raw_model_text,
            revision=revision,
            timestamp=timestamp,
        )

        revision_key = with_s3_prefix(draft_revision_key(downloaded_job.source_message_id, revision), prefix=config.s3_prefix)
        latest_key = with_s3_prefix(draft_latest_key(downloaded_job.source_message_id), prefix=config.s3_prefix)
        write_json_object(s3_client, bucket=downloaded_job.manifest_bucket, key=revision_key, payload=draft_manifest)
        write_json_object(s3_client, bucket=downloaded_job.manifest_bucket, key=latest_key, payload=latest_pointer)

        ocr_job_manifest["state"] = "completed"
        ocr_job_manifest["result"] = result_block
        ocr_job_manifest["error"] = None
        ocr_job_manifest["updated_at"] = timestamp
        write_json_object(s3_client, bucket=downloaded_job.manifest_bucket, key=downloaded_job.manifest_key, payload=ocr_job_manifest)

        if source_manifest is not None:
            source_manifest["state"] = "awaiting_approval"
            source_manifest["area_id"] = draft_manifest.get("area_id")
            source_manifest["current_draft_id"] = draft_manifest["draft_id"]
            source_manifest["current_draft_key"] = revision_key
            source_manifest["current_ocr_job_id"] = downloaded_job.ocr_job_id
            source_manifest["exception"] = None
            source_manifest["ocr_retry"] = None
            source_manifest["ocr_failure_notice"] = None
            source_manifest["updated_at"] = timestamp
            approval_manifest, approval_id, approval_key = ensure_approval_artifacts(
                s3_client=s3_client,
                manifest_bucket=downloaded_job.manifest_bucket,
                source_manifest=source_manifest,
                downloaded_job=downloaded_job,
                draft_manifest=draft_manifest,
                prefix=config.s3_prefix,
                timestamp=timestamp,
            )
            if approval_id and approval_key:
                source_manifest["current_approval_id"] = approval_id
                source_manifest["current_approval_key"] = approval_key
            write_json_object(s3_client, bucket=downloaded_job.manifest_bucket, key=source_manifest_path, payload=source_manifest)

            election_id = draft_manifest.get("election_id") or "default"
            area_id = draft_manifest.get("area_id")
            if area_id:
                update_area_submissions(
                    s3_client=s3_client,
                    bucket=downloaded_job.manifest_bucket,
                    prefix=config.s3_prefix,
                    election_id=election_id,
                    area_id=area_id,
                    source_message_id=downloaded_job.source_message_id,
                    timestamp=timestamp,
                )


        session_pointer_key = with_s3_prefix(
            f"sessions/{safe_id(downloaded_job.workflow_session_id)}/latest.json",
            prefix=config.s3_prefix,
        )
        session_pointer = read_json_object_if_exists(s3_client, bucket=downloaded_job.manifest_bucket, key=session_pointer_key)
        active_review_id = str((session_pointer or {}).get("active_review_source_message_id") or "").strip()
        is_active_review = (active_review_id == downloaded_job.source_message_id) or not active_review_id

        if is_active_review:
            approval_prompt = maybe_send_approval_prompt(
                s3_client=s3_client,
                bucket=downloaded_job.manifest_bucket,
                source_manifest_path=source_manifest_path,
                source_manifest=source_manifest,
                draft_manifest=draft_manifest,
                config=config,
                timestamp=timestamp,
            )
        else:
            approval_prompt = {"status": "deferred", "reason": "not_active_review", "active_review_id": active_review_id}

        return {
            "service": "ocr-worker",
            "status": "completed",
            "ocr_job_id": downloaded_job.ocr_job_id,
            "source_message_id": downloaded_job.source_message_id,
            "workflow_session_id": downloaded_job.workflow_session_id,
            "draft_id": draft_manifest["draft_id"],
            "draft_key": revision_key,
            "overall_confidence": draft_manifest["overall_confidence"],
            "validation_flags": draft_manifest["validation_flags"],
            "approval_prompt": approval_prompt,
            "hermes_base_url": config.hermes_base_url,
            "model_name": config.model_name,
        }
    except Exception as exc:
        error_message = str(exc)
        failure_timestamp = utc_now_iso()
        attempt_count = int(ocr_job_manifest.get("attempt_count") or 0)
        max_attempts = max_attempts_for_job(ocr_job_manifest)
        retryable = is_retryable_ocr_error_message(error_message)
        if retryable and attempt_count < max_attempts:
            persist_retry_pending(
                s3_client=s3_client,
                downloaded_job=downloaded_job,
                prefix=config.s3_prefix,
                ocr_job_manifest=ocr_job_manifest,
                source_manifest=source_manifest,
                code="OCR_PROCESSING_RETRY_PENDING",
                message=error_message,
                timestamp=failure_timestamp,
            )
            return {
                "service": "ocr-worker",
                "status": "retry_pending",
                "ocr_job_id": downloaded_job.ocr_job_id,
                "source_message_id": downloaded_job.source_message_id,
                "workflow_session_id": downloaded_job.workflow_session_id,
                "error": {"code": "OCR_PROCESSING_RETRY_PENDING", "message": error_message},
                "attempt_count": attempt_count,
                "max_attempts": max_attempts,
                "hermes_base_url": config.hermes_base_url,
                "model_name": config.model_name,
            }

        persist_failure(
            s3_client=s3_client,
            downloaded_job=downloaded_job,
            prefix=config.s3_prefix,
            ocr_job_manifest=ocr_job_manifest,
            source_manifest=source_manifest,
            code="OCR_PROCESSING_FAILED",
            message=error_message,
            timestamp=failure_timestamp,
        )
        failure_notice = maybe_send_ocr_failure_notice(
            s3_client=s3_client,
            bucket=downloaded_job.manifest_bucket,
            source_manifest_path=source_manifest_path,
            source_manifest=source_manifest,
            config=config,
            timestamp=failure_timestamp,
        )
        advance_session_pointer_on_failure(
            s3_client=s3_client,
            bucket=downloaded_job.manifest_bucket,
            prefix=config.s3_prefix,
            workflow_session_id=downloaded_job.workflow_session_id,
            failed_source_message_id=downloaded_job.source_message_id,
            timestamp=failure_timestamp,
        )
        return {
            "service": "ocr-worker",
            "status": "failed",
            "ocr_job_id": downloaded_job.ocr_job_id,
            "source_message_id": downloaded_job.source_message_id,
            "workflow_session_id": downloaded_job.workflow_session_id,
            "error": {"code": "OCR_PROCESSING_FAILED", "message": error_message},
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
            "failure_notice": failure_notice,
            "hermes_base_url": config.hermes_base_url,
            "model_name": config.model_name,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scaffold OCR worker process")
    parser.add_argument(
        "--once",
        action="store_true",
        help="print the resolved configuration once and exit",
    )
    parser.add_argument(
        "--drain-once",
        action="store_true",
        help="poll SQS one time, fetch matching OCR job artifacts from S3, print a summary, and exit",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = build_config()
    print("ocr-worker boot configuration")
    print(json.dumps(build_runtime_config_log(config), ensure_ascii=False, indent=2))

    if args.once:
        return

    queue_client = boto3.client("sqs", region_name=config.aws_region) if config.queue_url else None
    s3_client = boto3.client("s3", region_name=config.aws_region)

    if args.drain_once:
        try:
            drained_jobs = poll_queue_once(queue_client=queue_client, s3_client=s3_client, config=config) if queue_client else []
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "service": "ocr-worker",
                        "status": "failed",
                        "reason": "queue_fetch_failed",
                        "error": str(exc),
                        "queue_url": config.queue_url,
                    },
                    ensure_ascii=False,
                )
            )
            return
        print(
            json.dumps(
                {
                    "service": "ocr-worker",
                    "status": "drain_complete",
                    "processed_jobs": [asdict(job) for job in drained_jobs],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    stop_event = threading.Event()

    def request_stop(signum: int, frame: object | None) -> None:
        print(f"ocr-worker received signal {signum}, shutting down")
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    while not stop_event.wait(config.poll_seconds):
        if queue_client is None:
            print(
                json.dumps(
                    {
                        "service": "ocr-worker",
                        "status": "idle",
                        "reason": "queue_not_configured",
                        "hermes_base_url": config.hermes_base_url,
                        "model_name": config.model_name,
                    },
                    ensure_ascii=False,
                )
            )
            continue

        try:
            downloaded_jobs = poll_queue_once(queue_client=queue_client, s3_client=s3_client, config=config)
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "service": "ocr-worker",
                        "status": "failed",
                        "reason": "queue_fetch_failed",
                        "error": str(exc),
                        "queue_url": config.queue_url,
                        "hermes_base_url": config.hermes_base_url,
                        "model_name": config.model_name,
                    },
                    ensure_ascii=False,
                )
            )
            continue

        if not downloaded_jobs:
            print(
                json.dumps(
                    {
                        "service": "ocr-worker",
                        "status": "idle",
                        "reason": "no_messages",
                        "queue_url": config.queue_url,
                        "hermes_base_url": config.hermes_base_url,
                        "model_name": config.model_name,
                    },
                    ensure_ascii=False,
                )
            )
            continue

        for downloaded_job in downloaded_jobs:
            result_payload = process_downloaded_job(downloaded_job=downloaded_job, s3_client=s3_client, config=config)
            print(json.dumps(result_payload, ensure_ascii=False))
            if should_acknowledge_result(result_payload):
                acknowledge_job(queue_client=queue_client, config=config, downloaded_job=downloaded_job)


if __name__ == "__main__":
    main()
