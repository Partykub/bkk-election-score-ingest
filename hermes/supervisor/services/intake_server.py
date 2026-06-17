from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, parse, request

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


APPROVAL_TEXTS = {"ยืนยัน"}
CORRECTION_PREFIXES = ("แก้ไข",)
REJECT_TEXTS = {"ไม่ถูกต้อง"}
CORRECTION_HINT_KEYWORDS = ("แก้", "แก้ไข", "ก้ไข", "เบอร์", "ผู้สมัคร", "คะแนน", "หาย", "ผิด", "=")
APPROVAL_HINT_KEYWORDS = ("ยืนยัน", "ยัน", "ยืน", "รับรอง")
CANCEL_TEXTS = {"ยกเลิก", "กลับ"}


def normalize_command_text(text: str | None) -> str:
    normalized = str(text or "").strip()
    return normalized.strip("\"'“”‘’")


def is_approval_text(text: str) -> bool:
    return normalize_command_text(text) in APPROVAL_TEXTS


def is_correction_text(text: str) -> bool:
    normalized = normalize_command_text(text)
    return any(normalized.startswith(prefix) for prefix in CORRECTION_PREFIXES)


def is_reject_text(text: str) -> bool:
    return normalize_command_text(text).lower() in {value.lower() for value in REJECT_TEXTS}


def is_cancel_text(text: str | None) -> bool:
    return normalize_command_text(text) in CANCEL_TEXTS


def looks_like_correction_text(text: str | None) -> bool:
    normalized = normalize_command_text(text).lower()
    if not normalized:
        return False
    if is_correction_text(normalized):
        return True
    return any(keyword in normalized for keyword in CORRECTION_HINT_KEYWORDS)


def looks_like_approval_text(text: str | None) -> bool:
    normalized = normalize_command_text(text).lower()
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
    return f"messages/{source_message_id}/approval_r{revision}.json"


def approval_latest_path(source_message_id: str) -> str:
    return f"messages/{source_message_id}/approval_latest.json"


def source_message_id_from_update_job_id(update_job_id: str) -> str:
    cleaned = update_job_id
    if cleaned.startswith("upd_approval_"):
        cleaned = cleaned[13:]
    elif cleaned.startswith("upd_"):
        cleaned = cleaned[4:]
    if "_r" in cleaned:
        cleaned = cleaned.rsplit("_r", 1)[0]
    return cleaned


def update_job_path(update_job_id: str) -> str:
    src_id = source_message_id_from_update_job_id(update_job_id)
    return f"messages/{src_id}/update_job.json"


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


def build_image_received_text(*, queue_position: int = 0, total_in_queue: int = 0) -> str:
    base = "รับรูปเรียบร้อยแล้ว\nกำลังตรวจข้อมูลจากภาพให้ครับ"
    if total_in_queue > 1:
        base += f"\n(รอคิว: {total_in_queue} รูป)"
    base += "\nเดี๋ยวส่งผลให้ตรวจทานอีกครั้งเมื่อพร้อม"
    return base


def build_correction_guidance_text() -> str:
    return 'รับทราบว่าต้องการแก้ไขข้อมูล\nกรุณาพิมพ์รายละเอียดเพิ่ม เช่น "แก้ไข ผู้สมัครเบอร์ 4 เป็น 14" หรือ "แก้ไข 4=14"\nถ้าเข้าโหมดแก้ไขแล้ว จะพิมพ์สั้น ๆ เป็น "4=14" ได้เช่นกัน'


def build_enter_correction_mode_text() -> str:
    return 'เข้าสู่โหมดแก้ไขแล้ว\nพิมพ์รายละเอียด เช่น "แก้ไข 4=14" หรือ "4=14"\nหากไม่ต้องการแก้แล้ว พิมพ์ "ยกเลิก"'


def build_approval_guidance_text() -> str:
    return 'หากต้องการรับรองผล กรุณาพิมพ์ "ยืนยัน" ให้ถูกต้องอีกครั้ง'


def build_pending_approval_fallback_text() -> str:
    return 'หากต้องการรับรองให้ตอบ "ยืนยัน" หากต้องการแก้ไขให้ตอบ เช่น "แก้ไข 4=14" หรือหากต้องการปฏิเสธร่างนี้ให้ตอบ "ไม่ถูกต้อง"'


def build_general_help_text() -> str:
    return 'ส่งรูปผลคะแนนมาได้เลย แล้วผมจะช่วยอ่านตัวเลขและส่งร่างให้ตรวจ\nถ้ายังไม่มีงานค้างอยู่ ตอนนี้คุยเล่น ทักทาย หรือถามวิธีใช้งานได้เหมือนกัน'


def build_smalltalk_reply_text(text: str | None) -> str:
    normalized = normalize_command_text(text).lower()
    if not normalized:
        return build_general_help_text()

    greeting_keywords = ("สวัสดี", "หวัดดี", "ดีครับ", "ดีจ้า", "hello", "hi", "hey")
    thanks_keywords = ("ขอบคุณ", "thank", "thx")
    capability_keywords = ("ทำอะไรได้", "ช่วยอะไรได้", "ใช้งานยังไง", "ทำงานยังไง", "help")
    status_keywords = ("เป็นไง", "เป็นยังไง", "สบายดี", "อยู่ไหม", "อยู่มั้ย")

    if any(keyword in normalized for keyword in greeting_keywords):
        return "สวัสดีครับ\nส่งรูปผลคะแนนมาได้เลย เดี๋ยวผมช่วยอ่านและจัดร่างให้ตรวจต่อ"
    if any(keyword in normalized for keyword in thanks_keywords):
        return "ยินดีครับ\nถ้ามีรูปผลคะแนนหรืออยากแก้ข้อมูลต่อ ส่งมาได้เลย"
    if any(keyword in normalized for keyword in capability_keywords):
        return 'ตอนนี้ผมช่วยได้หลัก ๆ คือรับรูป, อ่านคะแนน OCR, เปิดร่างให้ยืนยัน, และรับคำสั่งแก้ไข เช่น "แก้ไข 4=14"'
    if any(keyword in normalized for keyword in status_keywords):
        return "อยู่ครับ\nถ้าพร้อมแล้วส่งรูปผลคะแนนมาได้เลย หรือจะถามวิธีใช้งานต่อก็ได้"
    return build_general_help_text()


def build_free_chat_system_prompt() -> str:
    return (
        "You are a helpful Thai-speaking LINE assistant for election score intake.\n"
        "You can chat naturally in Thai.\n"
        "Keep replies concise, friendly, and practical.\n"
        "Do not pretend to have completed OCR or approval actions unless the user explicitly asked and the workflow handled it.\n"
        "If a score draft is awaiting approval, only guide approval/correction when the user intent is clearly approve, reject, or correct.\n"
        "For general questions or casual chat, answer normally and do not force the user back into the approval flow.\n"
        "If the user asks what you can do, mention receiving score photos, OCR draft review, approval, and corrections.\n"
        "Prefer Thai in replies unless the user writes in another language."
    )


def build_rule_based_chat_reply(text: str | None) -> str | None:
    normalized = normalize_command_text(text).lower()
    if not normalized:
        return None

    identity_keywords = (
        "\u0e0a\u0e37\u0e48\u0e2d\u0e2d\u0e30\u0e44\u0e23",
        "\u0e04\u0e38\u0e13\u0e0a\u0e37\u0e48\u0e2d\u0e2d\u0e30\u0e44\u0e23",
        "\u0e19\u0e32\u0e22\u0e0a\u0e37\u0e48\u0e2d\u0e2d\u0e30\u0e44\u0e23",
        "\u0e41\u0e19\u0e30\u0e19\u0e33\u0e15\u0e31\u0e27",
    )
    getting_started_keywords = (
        "\u0e40\u0e23\u0e34\u0e48\u0e21\u0e08\u0e32\u0e01",
        "\u0e40\u0e23\u0e34\u0e48\u0e21\u0e22\u0e31\u0e07\u0e44\u0e07",
        "\u0e2a\u0e48\u0e07\u0e23\u0e39\u0e1b\u0e01\u0e48\u0e2d\u0e19",
        "\u0e15\u0e49\u0e2d\u0e07\u0e17\u0e33\u0e22\u0e31\u0e07\u0e44\u0e07",
    )

    if any(keyword in normalized for keyword in identity_keywords):
        return (
            "\u0e1c\u0e21\u0e40\u0e1b\u0e47\u0e19\u0e1c\u0e39\u0e49\u0e0a\u0e48\u0e27\u0e22 LINE "
            "\u0e2a\u0e33\u0e2b\u0e23\u0e31\u0e1a\u0e23\u0e31\u0e1a\u0e1c\u0e25\u0e04\u0e30\u0e41\u0e19\u0e19\u0e40\u0e25\u0e37\u0e2d\u0e01\u0e15\u0e31\u0e49\u0e07\u0e04\u0e23\u0e31\u0e1a\n"
            "\u0e16\u0e49\u0e32\u0e08\u0e30\u0e43\u0e2b\u0e49\u0e40\u0e23\u0e35\u0e22\u0e01\u0e2a\u0e31\u0e49\u0e19 \u0e46 "
            "\u0e40\u0e23\u0e35\u0e22\u0e01\u0e1c\u0e21\u0e27\u0e48\u0e32 BKK Election \u0e44\u0e14\u0e49"
        )
    if any(keyword in normalized for keyword in getting_started_keywords):
        return (
            "\u0e40\u0e23\u0e34\u0e48\u0e21\u0e08\u0e32\u0e01\u0e2a\u0e48\u0e07\u0e23\u0e39\u0e1b\u0e1c\u0e25\u0e04\u0e30\u0e41\u0e19\u0e19\u0e21\u0e32\u0e44\u0e14\u0e49\u0e40\u0e25\u0e22\n"
            "\u0e1c\u0e21\u0e08\u0e30\u0e2d\u0e48\u0e32\u0e19\u0e15\u0e31\u0e27\u0e40\u0e25\u0e02\u0e41\u0e25\u0e30\u0e2a\u0e23\u0e38\u0e1b"
            "\u0e40\u0e1b\u0e47\u0e19\u0e23\u0e48\u0e32\u0e07\u0e43\u0e2b\u0e49\u0e15\u0e23\u0e27\u0e08 "
            "\u0e01\u0e48\u0e2d\u0e19\u0e04\u0e48\u0e2d\u0e22\u0e22\u0e37\u0e19\u0e22\u0e31\u0e19\u0e2b\u0e23\u0e37\u0e2d\u0e41\u0e01\u0e49\u0e44\u0e02"
        )
    return None


def build_candidate_score_lines(draft_manifest: dict[str, Any]) -> list[str]:
    candidate_scores = normalize_candidate_scores(draft_manifest.get("candidate_scores"))
    lines: list[str] = []
    for score in candidate_scores:
        candidate_number = score.get("candidate_number")
        candidate_value = score.get("score")
        if candidate_number is None or candidate_value is None:
            continue
        lines.append(f"ผู้สมัคร {candidate_number}: {candidate_value}")
    return lines


def build_ballot_summary_lines(draft_manifest: dict[str, Any]) -> list[str]:
    fields = [
        ("\u0e1c\u0e39\u0e49\u0e21\u0e35\u0e2a\u0e34\u0e17\u0e18\u0e34", "eligible_voters"),
        ("\u0e1c\u0e39\u0e49\u0e21\u0e32\u0e43\u0e0a\u0e49\u0e2a\u0e34\u0e17\u0e18\u0e34", "voter_turnout"),
        ("\u0e1a\u0e31\u0e15\u0e23\u0e14\u0e35", "valid_ballots"),
        ("\u0e1a\u0e31\u0e15\u0e23\u0e40\u0e2a\u0e35\u0e22", "invalid_ballots"),
        ("Vote No", "vote_no"),
    ]
    lines: list[str] = []
    for label, key in fields:
        value = draft_manifest.get(key)
        if value is not None:
            lines.append(f"{label}: {value}")
    return lines


def build_changed_candidate_score_lines(draft_manifest: dict[str, Any]) -> list[str]:
    correction_payload = draft_manifest.get("correction_payload")
    if not isinstance(correction_payload, dict):
        return []
    overrides = correction_payload.get("candidate_score_overrides")
    if not isinstance(overrides, list):
        return []

    current_scores = {
        score.get("candidate_number"): score.get("score")
        for score in normalize_candidate_scores(draft_manifest.get("candidate_scores"))
        if score.get("candidate_number") is not None and score.get("score") is not None
    }
    lines: list[str] = []
    for item in overrides:
        if not isinstance(item, dict):
            continue
        candidate_number = item.get("candidate_number")
        score = item.get("score")
        try:
            candidate_number = int(candidate_number)
            score = int(score)
        except (TypeError, ValueError):
            continue
        current_score = current_scores.get(candidate_number, score)
        lines.append(f"ผู้สมัคร {candidate_number}: {current_score}")
    return lines


def build_approval_success_text(draft_manifest: dict[str, Any] | None = None) -> str:
    lines = ["รับรองผลเรียบร้อยแล้ว", "บันทึกผลล่าสุดในระบบแล้ว"]
    if isinstance(draft_manifest, dict):
        revision = int(draft_manifest.get("revision") or 1)
        lines.append(f"ผลร่างล่าสุด: ครั้งที่ {revision}")
        changed_lines = build_changed_candidate_score_lines(draft_manifest)
        if changed_lines:
            lines.append("รายการที่แก้ไข:")
            lines.extend(changed_lines)
        else:
            lines.extend(build_candidate_score_lines(draft_manifest))
    return "\n".join(lines)


def build_correction_received_text(draft_manifest: dict[str, Any] | None = None) -> str:
    if isinstance(draft_manifest, dict):
        revision = int(draft_manifest.get("revision") or 1)
        changed_lines = build_changed_candidate_score_lines(draft_manifest)
        lines = ["รับการแก้ไขแล้ว"]
        if changed_lines:
            lines.append("รายการที่แก้ไข:")
            lines.extend(changed_lines)
        lines.append(f"กำลังส่งร่างครั้งที่ {revision} ให้ตรวจอีกครั้ง")
        return "\n".join(lines)
    return "รับการแก้ไขแล้ว\nกำลังส่งร่างฉบับใหม่ให้ตรวจอีกครั้ง"


def build_correction_cancelled_text() -> str:
    return 'ยกเลิกโหมดแก้ไขแล้ว\nหากต้องการรับรองให้ตอบ "ยืนยัน" หรือหากต้องการแก้ไขให้เริ่มใหม่ด้วย "แก้ไข"'


def build_reject_acknowledgment_text() -> str:
    return "ปฏิเสธร่างนี้แล้ว\nระบบจะยังไม่นำผลชุดนี้ไปใช้\nหากต้องการดำเนินการต่อ กรุณาส่งรูปใหม่อีกครั้ง"


def build_post_approval_approval_text() -> str:
    return "ร่างนี้ถูกรับรองแล้ว ไม่ต้องยืนยันซ้ำ"


def build_post_approval_correction_text() -> str:
    return "ร่างนี้ถูกรับรองแล้วและปิดรอบตรวจแล้ว\nหากต้องการแก้เพิ่มเติม กรุณาส่งรูปใหม่หรือเปิดรอบแก้ไขใหม่"


def build_line_text_message(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text[:5000]}


def correction_form_secret() -> str:
    return (
        os.environ.get("SUPERVISOR_CORRECTION_FORM_SECRET", "").strip()
        or os.environ.get("LINE_CHANNEL_SECRET", "").strip()
        or "change-this-correction-form-secret"
    )


def build_correction_form_token(*, source_message_id: str, approval_id: str) -> str:
    payload = f"{source_message_id}:{approval_id}".encode("utf-8")
    return hmac.new(correction_form_secret().encode("utf-8"), payload, hashlib.sha256).hexdigest()


def build_correction_form_url(*, source_message_id: str, approval_id: str) -> str | None:
    public_url = os.environ.get("LINE_PUBLIC_URL", "").strip().rstrip("/")
    if not public_url:
        return None
    query = parse.urlencode(
        {
            "source_message_id": source_message_id,
            "approval_id": approval_id,
            "token": build_correction_form_token(source_message_id=source_message_id, approval_id=approval_id),
        }
    )
    return f"{public_url}/line/liff/correction?{query}"


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


def build_correction_form_url_for_source_manifest(source_manifest: dict[str, Any]) -> str | None:
    source_message_id = str(source_manifest.get("source_message_id") or "").strip()
    approval_id = str(source_manifest.get("current_approval_id") or "").strip()
    if not source_message_id or not approval_id:
        return None
    return build_correction_form_url(source_message_id=source_message_id, approval_id=approval_id)


def draft_revision_path(source_message_id: str, revision: int) -> str:
    return f"messages/{source_message_id}/draft_r{revision}.json"


def draft_latest_path(source_message_id: str) -> str:
    return f"messages/{source_message_id}/draft_latest.json"


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

    normalized_text = source_text.strip()
    if is_correction_text(normalized_text):
        normalized_text = re.sub(r"^\S+\s*", "", normalized_text, count=1)
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


def parse_area_id_override(source_text: str | None) -> str | None:
    if not source_text:
        return None

    match = re.search(r"เขต\s*(?:=|เป็น|คือ|ควรเป็น|:|เบอร์)?\s*(\d+)", source_text, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def looks_like_raw_correction_override(text: str | None) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    if is_correction_text(normalized):
        return False
    return bool(re.search(r"\b\d+\s*(?:=|เป็น|คือ|ควรเป็น)\s*\d[\d,]*\b", normalized, re.IGNORECASE))


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
    ballot_summary_lines = build_ballot_summary_lines(draft_manifest)

    lines = [f"ตรวจรูปเสร็จแล้ว: ร่างครั้งที่ {revision}"]
    if area_id:
        lines.append(f"เขต: {area_id}")
    if polling_unit_id:
        lines.append(f"หน่วย: {polling_unit_id}")
    lines.append(f"เอกสาร: {report_type}")

    if ballot_summary_lines:
        lines.append("\u0e02\u0e49\u0e2d\u0e21\u0e39\u0e25\u0e1a\u0e31\u0e15\u0e23\u0e17\u0e35\u0e48\u0e2d\u0e48\u0e32\u0e19\u0e44\u0e14\u0e49:")
        lines.extend(ballot_summary_lines)

    if candidate_scores:
        lines.append("คะแนนที่อ่านได้:")
        lines.extend(build_candidate_score_lines(draft_manifest))
    elif not ballot_summary_lines:
        lines.append("ยังไม่พบคะแนนที่เชื่อถือได้จาก OCR")

    lines.append("ตอบ 'ยืนยัน' เพื่อรับรองร่างนี้")
    lines.append("ตอบ 'แก้ไข' เพื่อเริ่มแก้ข้อมูล หรือพิมพ์ เช่น 'แก้ไข 4=14'")
    lines.append("ตอบ 'ไม่ถูกต้อง' หากต้องการปฏิเสธร่างนี้")
    return "\n".join(lines)


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


def send_line_reply_message(
    *,
    channel_access_token: str,
    reply_token: str,
    text: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    api_base_url: str = "https://api.line.me",
    opener: Any = request.urlopen,
) -> None:
    payload_messages = messages or [build_line_text_message(text or "")]
    line_request = request.Request(
        f"{api_base_url.rstrip('/')}/v2/bot/message/reply",
        data=json.dumps(
            {
                "replyToken": reply_token,
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
        raise RuntimeError(f"line reply failed with status {exc.code}: {response_body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"unable to reach line reply api: {exc.reason}") from exc


def build_line_reply_sender_from_env(opener: Any = request.urlopen) -> Any | None:
    channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not channel_access_token:
        return None

    api_base_url = os.environ.get("LINE_API_BASE_URL", "https://api.line.me").strip() or "https://api.line.me"

    def send_reply(*, reply_token: str, text: str | None = None, messages: list[dict[str, Any]] | None = None) -> None:
        send_line_reply_message(
            channel_access_token=channel_access_token,
            reply_token=reply_token,
            text=text,
            messages=messages,
            api_base_url=api_base_url,
            opener=opener,
        )

    return send_reply


def build_line_push_sender_from_env(opener: Any = request.urlopen) -> Any | None:
    channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not channel_access_token:
        return None

    api_base_url = os.environ.get("LINE_API_BASE_URL", "https://api.line.me").strip() or "https://api.line.me"

    def send_push(*, destination_id: str, text: str | None = None, messages: list[dict[str, Any]] | None = None) -> None:
        send_line_push_message(
            channel_access_token=channel_access_token,
            destination_id=destination_id,
            text=text,
            messages=messages,
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


def build_supervisor_chat_client_from_env(opener: Any = request.urlopen) -> Any | None:
    base_url = (
        os.environ.get("SUPERVISOR_CHAT_HERMES_BASE_URL", "").strip()
        or os.environ.get("OCR_WORKER_HERMES_BASE_URL", "").strip()
    )
    api_key = (
        os.environ.get("SUPERVISOR_CHAT_HERMES_API_KEY", "").strip()
        or os.environ.get("OCR_WORKER_HERMES_API_KEY", "").strip()
    )
    model = (
        os.environ.get("SUPERVISOR_CHAT_HERMES_MODEL", "").strip()
        or os.environ.get("OCR_WORKER_HERMES_MODEL", "").strip()
        or "hermes-agent"
    )
    if not base_url or not api_key or api_key == "change-this-api-key":
        return None

    def send_chat(*, messages: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {"model": model, "messages": messages}
        req = request.Request(
            f"{base_url.rstrip('/')}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with opener(req, timeout=45) as response:
                raw_body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", "ignore")
            raise RuntimeError(f"Hermes chat request failed with status {exc.code}: {body}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Hermes chat request failed: {exc}") from exc
        return json.loads(raw_body)

    return send_chat


def extract_chat_assistant_text(response_payload: dict[str, Any]) -> str:
    choices = response_payload.get("choices") or []
    if not choices:
        raise ValueError("Hermes chat response did not contain any choices")

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
    raise ValueError("Hermes chat response content was empty")


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
        chat_completion_client: Any | None = None,
    ):
        self.root_path = Path(root_path)
        self.state_backend = state_backend or build_state_backend(self.root_path)
        self.upload_service = upload_service or build_upload_service(self.root_path)
        self.ocr_job_queue = ocr_job_queue or build_ocr_job_queue()
        self.update_job_queue = update_job_queue or build_update_job_queue()
        self.line_reply_sender = line_reply_sender or build_line_reply_sender_from_env()
        self.line_push_sender = line_push_sender or build_line_push_sender_from_env()
        self.chat_completion_client = chat_completion_client or build_supervisor_chat_client_from_env()
        self._locks_lock = threading.Lock()
        self._session_locks = {}

    def _write_json(self, relative_path: str, payload: dict[str, Any]) -> str:
        return self.state_backend.write_json(relative_path, payload)

    def _read_json(self, relative_path: str) -> dict[str, Any] | None:
        return self.state_backend.read_json(relative_path)

    def _update_area_submissions(
        self,
        *,
        election_id: str,
        area_id: str,
        source_message_id: str,
        timestamp: str,
        old_area_id: str | None = None,
    ) -> None:
        election_id_safe = safe_id(election_id) if election_id else "default"

        if old_area_id and old_area_id != area_id:
            old_path = f"indexes/by-area/{election_id_safe}/{safe_id(old_area_id)}/submissions.json"
            old_data = self._read_json(old_path)
            if old_data:
                subs = old_data.get("submissions") or []
                new_subs = [s for s in subs if s.get("source_message_id") != source_message_id]
                old_data["submissions"] = new_subs
                old_data["submission_count"] = len(new_subs)
                old_data["updated_at"] = timestamp
                self._write_json(old_path, old_data)

        if area_id:
            new_path = f"indexes/by-area/{election_id_safe}/{safe_id(area_id)}/submissions.json"
            data = self._read_json(new_path)
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
                self._write_json(new_path, data)

    def _normalize_state_path(self, path: str) -> str:
        normalized = path.replace("\\", "/").lstrip("/")
        key_prefix = getattr(self.state_backend, "key_prefix", "")
        normalized_prefix = str(key_prefix or "").strip("/")
        if normalized_prefix and normalized.startswith(f"{normalized_prefix}/"):
            return normalized[len(normalized_prefix) + 1 :]
        return normalized

    def event_index_path(self, line_event_id: str) -> str:
        return f"events/{safe_id(line_event_id)}.json"

    def message_index_path(self, line_message_id: str) -> str:
        return f"events/{safe_id(line_message_id)}.json"

    def session_pointer_path(self, workflow_session_id: str) -> str:
        return f"sessions/{safe_id(workflow_session_id)}/latest.json"

    def source_manifest_path(self, source_message_id: str) -> str:
        return f"messages/{source_message_id}/manifest.json"

    def ocr_job_manifest_path(self, ocr_job_id: str) -> str:
        if ocr_job_id.startswith("ocr_"):
            src_id = ocr_job_id[4:]
        else:
            src_id = ocr_job_id
        if not src_id.startswith("src_"):
            src_id = f"src_{src_id}"
        return f"messages/{src_id}/ocr_job.json"

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
        target_source_message_id = (
            str(session_pointer.get("active_review_source_message_id") or "").strip()
            or str(session_pointer.get("latest_source_message_id") or "").strip()
            or None
        )
        if not target_source_message_id:
            return None, None, None, None

        target_source_manifest = self.read_manifest(target_source_message_id)
        if target_source_manifest is None:
            return target_source_message_id, None, None, None

        approval_key = str(target_source_manifest.get("current_approval_key") or "").strip() or approval_latest_path(target_source_message_id)
        approval_manifest = self._read_json(self._normalize_state_path(approval_key))
        return target_source_message_id, target_source_manifest, approval_key, approval_manifest

    @staticmethod
    def _pending_user_action(source_manifest: dict[str, Any] | None) -> str | None:
        if not isinstance(source_manifest, dict):
            return None
        value = str(source_manifest.get("pending_user_action") or "").strip()
        return value or None

    def _source_waits_correction_input(self, source_manifest: dict[str, Any] | None) -> bool:
        return self._pending_user_action(source_manifest) == "awaiting_correction_input"

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
                "eligible_voters": draft_manifest.get("eligible_voters"),
                "voter_turnout": draft_manifest.get("voter_turnout"),
                "valid_ballots": draft_manifest.get("valid_ballots"),
                "invalid_ballots": draft_manifest.get("invalid_ballots"),
                "abstained_ballots": draft_manifest.get("vote_no"),
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
            correction_url = build_correction_form_url_for_source_manifest(source_manifest)
            self.line_push_sender(
                destination_id=destination_id,
                messages=build_approval_action_messages(build_approval_prompt_text(draft_manifest), correction_url=correction_url),
            )
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

    def _reply_text(self, manifest: dict[str, Any], text: str, *, messages: list[dict[str, Any]] | None = None) -> None:
        if self.line_reply_sender is None:
            return

        reply_token = str(manifest.get("line_reply_token") or "").strip()
        if not reply_token:
            return

        try:
            self.line_reply_sender(reply_token=reply_token, text=text, messages=messages)
        except Exception as exc:
            print(f"line intake: unable to send text reply: {exc}", file=sys.stderr)

    def _build_free_chat_messages(self, user_text: str, *, workflow_session_id: str) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": build_free_chat_system_prompt()},
            {
                "role": "user",
                "content": (
                    f"session_id={workflow_session_id}\n"
                    f"user_message={user_text}"
                ),
            },
        ]

    def _reply_free_chat_text(self, manifest: dict[str, Any]) -> bool:
        source_text = normalize_command_text(manifest.get("source_text"))
        if not source_text:
            return False
        if self.chat_completion_client is None:
            return False

        workflow_session_id = str(manifest.get("workflow_session_id") or "").strip() or "line_unknown"
        try:
            response_payload = self.chat_completion_client(
                messages=self._build_free_chat_messages(source_text, workflow_session_id=workflow_session_id)
            )
            assistant_text = extract_chat_assistant_text(response_payload)
        except Exception as exc:
            print(f"line intake: unable to generate free chat response: {exc}", file=sys.stderr)
            return False

        if not assistant_text:
            return False
        self._reply_text(manifest, assistant_text[:5000])
        return True

    def _reply_text_for_active_approval(self, manifest: dict[str, Any]) -> None:
        source_type = str(manifest.get("source_type") or "").strip()
        if source_type not in {"text", "correction_command", "approval_command"}:
            return
        if manifest.get("correction_cancelled"):
            self._reply_text(manifest, build_correction_cancelled_text())
            return
        if manifest.get("approval_action") in {"approve", "correct", "reject"} and manifest.get("exception") is None:
            return

        workflow_session_id = str(manifest.get("workflow_session_id") or "").strip()
        if not workflow_session_id:
            return

        target_source_message_id, target_source_manifest, _, approval_manifest = self._resolve_active_approval(workflow_session_id)
        if source_type == "text" and (target_source_manifest is None or approval_manifest is None or not target_source_message_id):
            if not self._reply_free_chat_text(manifest):
                rule_based_reply = build_rule_based_chat_reply(manifest.get("source_text"))
                if rule_based_reply:
                    self._reply_text(manifest, rule_based_reply)
                    return
                self._reply_text(manifest, build_smalltalk_reply_text(manifest.get("source_text")))
            return
        if target_source_manifest is None or approval_manifest is None or not target_source_message_id:
            return
        if target_source_message_id == manifest.get("source_message_id"):
            return
        source_text = str(manifest.get("source_text") or "").strip()
        if not source_text:
            return

        if approval_manifest.get("state") != "awaiting_approval":
            if source_type == "approval_command" or is_approval_text(source_text):
                self._reply_text(manifest, build_post_approval_approval_text())
            elif source_type == "correction_command" or looks_like_correction_text(source_text):
                self._reply_text(manifest, build_post_approval_correction_text())
            elif source_type == "text":
                if not self._reply_free_chat_text(manifest):
                    rule_based_reply = build_rule_based_chat_reply(source_text)
                    if rule_based_reply:
                        self._reply_text(manifest, rule_based_reply)
                        return
                    self._reply_text(manifest, build_smalltalk_reply_text(source_text))
            return

        if is_cancel_text(source_text) and self._source_waits_correction_input(target_source_manifest):
            self._reply_text(manifest, build_correction_cancelled_text())
            return

        if source_type == "correction_command" and normalize_command_text(source_text) == "แก้ไข":
            self._reply_text(manifest, build_enter_correction_mode_text())
            return

        if self._source_waits_correction_input(target_source_manifest):
            self._reply_text(manifest, build_correction_guidance_text())
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
                if self.chat_completion_client is None:
                    rule_based_reply = build_rule_based_chat_reply(source_text)
                    if rule_based_reply:
                        self._reply_text(manifest, rule_based_reply)
                    else:
                        fallback_text = build_smalltalk_reply_text(source_text)
                        if fallback_text == build_general_help_text():
                            self._reply_text(manifest, build_pending_approval_fallback_text())
                        else:
                            self._reply_text(manifest, fallback_text)
                elif not self._reply_free_chat_text(manifest):
                    rule_based_reply = build_rule_based_chat_reply(source_text)
                    if rule_based_reply:
                        self._reply_text(manifest, rule_based_reply)
                        return
                    self._reply_text(manifest, build_smalltalk_reply_text(source_text))

    def _load_target_draft_manifest(self, manifest: dict[str, Any]) -> dict[str, Any] | None:
        target_source_message_id = str(manifest.get("target_source_message_id") or "").strip()
        if not target_source_message_id:
            return None
        target_source_manifest = self.read_manifest(target_source_message_id)
        if target_source_manifest is None:
            return None
        draft_key = str(target_source_manifest.get("current_draft_key") or "").strip()
        if not draft_key:
            return None
        return self._read_json(self._normalize_state_path(draft_key))

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
        area_id_override: str | None = None,
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
        if area_id_override:
            corrected_draft["area_id"] = area_id_override
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
            area_id_override = parse_area_id_override(source_text)
            if not candidate_score_overrides and not area_id_override:
                target_source_manifest["pending_user_action"] = "awaiting_correction_input"
                target_source_manifest["updated_at"] = timestamp
                self._write_json(self.source_manifest_path(target_source_message_id), target_source_manifest)
                manifest["state"] = "exception"
                manifest["exception"] = {
                    "code": "CORRECTION_PARSE_FAILED",
                    "message": "correction command could not be parsed into candidate score overrides or area id override",
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
        target_source_manifest["pending_user_action"] = None
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
            if area_id_override:
                correction_payload["area_id_override"] = area_id_override

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
                area_id_override=area_id_override,
            )
            self._write_json(corrected_draft_key, corrected_draft)
            self._write_json(draft_latest_path(target_source_message_id), draft_pointer)

            # Update submissions count!
            old_area_id = target_source_manifest.get("area_id")
            new_area_id = corrected_draft.get("area_id")
            election_id = corrected_draft.get("election_id") or "default"
            self._update_area_submissions(
                election_id=election_id,
                area_id=new_area_id,
                source_message_id=target_source_message_id,
                timestamp=timestamp,
                old_area_id=old_area_id,
            )

            # Update target_source_manifest area_id
            target_source_manifest["area_id"] = new_area_id

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
            target_source_manifest["pending_user_action"] = None
            manifest["correction_payload"] = correction_payload

        self._write_json(self.source_manifest_path(target_source_message_id), target_source_manifest)

        manifest["state"] = target_source_manifest["state"] if action == "correct" else new_approval_state
        manifest["current_draft_id"] = target_source_manifest.get("current_draft_id") or draft_manifest.get("draft_id")
        manifest["current_approval_id"] = target_source_manifest.get("current_approval_id") or approval_manifest.get("approval_id")
        manifest["current_update_job_id"] = update_job_id

        if action == "correct":
            self._maybe_send_correction_acknowledgment(manifest)
            manifest["line_reply_token"] = None
            import time
            time.sleep(1.0)
            self._send_approval_prompt_for_draft(
                source_manifest=target_source_manifest,
                draft_manifest=corrected_draft,
                timestamp=timestamp,
            )
            self._write_json(self.source_manifest_path(target_source_message_id), target_source_manifest)
        elif action in {"approve", "reject"}:
            if action == "approve":
                self._maybe_send_approval_acknowledgment(manifest)
            else:
                self._maybe_send_reject_acknowledgment(manifest)
            manifest["line_reply_token"] = None
            import time
            time.sleep(1.0)
            self._advance_review_queue(workflow_session_id, timestamp=timestamp)

        return manifest["state"], target_source_message_id

    def _advance_review_queue(self, workflow_session_id: str, *, timestamp: str) -> None:
        session_pointer = self.read_session_pointer(workflow_session_id) or {}
        pending_queue = list(session_pointer.get("pending_review_queue") or [])
        completed_count = int(session_pointer.get("completed_review_count") or 0) + 1
        total_count = int(session_pointer.get("total_received_count") or 0)

        if not pending_queue:
            session_pointer["active_review_source_message_id"] = None
            session_pointer["completed_review_count"] = completed_count
            session_pointer["updated_at"] = timestamp
            self._write_json(self.session_pointer_path(workflow_session_id), session_pointer)
            return

        next_source_message_id = pending_queue.pop(0)
        session_pointer["active_review_source_message_id"] = next_source_message_id
        session_pointer["pending_review_queue"] = pending_queue
        session_pointer["completed_review_count"] = completed_count
        session_pointer["updated_at"] = timestamp
        self._write_json(self.session_pointer_path(workflow_session_id), session_pointer)

        next_manifest = self.read_manifest(next_source_message_id)
        if next_manifest is not None and next_manifest.get("state") == "awaiting_approval":
            draft_key = str(next_manifest.get("current_draft_key") or "").strip()
            draft_manifest = self._read_json(self._normalize_state_path(draft_key)) if draft_key else None
            if draft_manifest is not None:
                self._send_approval_prompt_for_draft(
                    source_manifest=next_manifest,
                    draft_manifest=draft_manifest,
                    timestamp=timestamp,
                )
                self._write_json(self.source_manifest_path(next_source_message_id), next_manifest)
                return

        destination_id = self._line_destination_id_for_source_manifest(next_manifest or {})
        if self.line_push_sender is not None and destination_id:
            try:
                remaining = len(pending_queue) + 1
                self.line_push_sender(
                    destination_id=destination_id,
                    text=f"รอผล OCR ของรูปถัดไปอยู่ ({completed_count + 1}/{total_count})\nจะส่งให้ตรวจเมื่อพร้อม",
                )
            except Exception:
                pass

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

        queue_position = int(manifest.get("_queue_position") or 0)
        total_in_queue = int(manifest.get("_total_in_queue") or 0)

        try:
            self.line_reply_sender(reply_token=reply_token, text=build_image_received_text(queue_position=queue_position, total_in_queue=total_in_queue))
        except Exception as exc:
            print(f"line intake: unable to send image acknowledgment: {exc}", file=sys.stderr)

    def _maybe_send_approval_acknowledgment(self, manifest: dict[str, Any]) -> None:
        if self.line_reply_sender is None:
            return

        reply_token = str(manifest.get("line_reply_token") or "").strip()
        if not reply_token:
            return

        if manifest.get("approval_action") != "approve":
            return

        if manifest.get("state") != "approved":
            return

        try:
            self.line_reply_sender(reply_token=reply_token, text=build_approval_success_text(self._load_target_draft_manifest(manifest)))
        except Exception as exc:
            print(f"line intake: unable to send approval acknowledgment: {exc}", file=sys.stderr)

    def _maybe_send_correction_acknowledgment(self, manifest: dict[str, Any]) -> None:
        if self.line_reply_sender is None:
            return

        reply_token = str(manifest.get("line_reply_token") or "").strip()
        if not reply_token:
            return

        if manifest.get("approval_action") != "correct":
            return

        if manifest.get("state") != "awaiting_approval":
            return

        if manifest.get("exception") is not None:
            return

        try:
            self.line_reply_sender(reply_token=reply_token, text=build_correction_received_text(self._load_target_draft_manifest(manifest)))
        except Exception as exc:
            print(f"line intake: unable to send correction acknowledgment: {exc}", file=sys.stderr)

    def _maybe_send_reject_acknowledgment(self, manifest: dict[str, Any]) -> None:
        if self.line_reply_sender is None:
            return

        reply_token = str(manifest.get("line_reply_token") or "").strip()
        if not reply_token:
            return

        if manifest.get("approval_action") != "reject":
            return

        if manifest.get("state") != "rejected":
            return

        if manifest.get("exception") is not None:
            return

        try:
            self.line_reply_sender(reply_token=reply_token, text=build_reject_acknowledgment_text())
        except Exception as exc:
            print(f"line intake: unable to send reject acknowledgment: {exc}", file=sys.stderr)

    def persist_line_event(self, event: dict[str, Any], received_at: str | None = None) -> ProcessedEvent:
        workflow_session_id = stable_workflow_session_id(event)
        with self._locks_lock:
            if workflow_session_id not in self._session_locks:
                self._session_locks[workflow_session_id] = threading.Lock()
            session_lock = self._session_locks[workflow_session_id]

        with session_lock:
            return self._persist_line_event_locked(event, received_at)

    def _persist_line_event_locked(self, event: dict[str, Any], received_at: str | None = None) -> ProcessedEvent:
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
            existing_session_pointer = self.read_session_pointer(workflow_session_id) or {}
            active_review_id = str(existing_session_pointer.get("active_review_source_message_id") or "").strip()
            existing_pending_queue = list(existing_session_pointer.get("pending_review_queue") or [])
            existing_total = int(existing_session_pointer.get("total_received_count") or 0)

            if not active_review_id:
                session_pointer_source_message_id = source_message_id
                new_active_review_id = source_message_id
                new_pending_queue = existing_pending_queue
            else:
                session_pointer_source_message_id = active_review_id
                new_active_review_id = active_review_id
                new_pending_queue = existing_pending_queue + [source_message_id]

            new_total = existing_total + 1
            manifest["_queue_position"] = new_total
            manifest["_total_in_queue"] = len(new_pending_queue) + 1

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
        elif source_type == "text":
            target_source_message_id, target_source_manifest, _, approval_manifest = self._resolve_active_approval(workflow_session_id)
            if target_source_message_id and target_source_manifest is not None and approval_manifest is not None:
                if approval_manifest.get("state") == "awaiting_approval" and is_cancel_text(source_text) and self._source_waits_correction_input(target_source_manifest):
                    target_source_manifest["pending_user_action"] = None
                    target_source_manifest["updated_at"] = timestamp
                    self._write_json(self.source_manifest_path(target_source_message_id), target_source_manifest)
                    manifest["correction_cancelled"] = True
                    session_pointer_source_message_id = target_source_message_id
                elif approval_manifest.get("state") == "awaiting_approval" and (
                    is_approval_text(source_text)
                    or is_reject_text(source_text)
                    or self._source_waits_correction_input(target_source_manifest)
                    or is_correction_text(source_text)
                    or looks_like_raw_correction_override(source_text)
                ):
                    effective_source_type = "correction_command" if (
                        self._source_waits_correction_input(target_source_manifest)
                        or is_correction_text(source_text)
                        or looks_like_raw_correction_override(source_text)
                    ) else "text"
                    manifest["state"], session_pointer_source_message_id = self._apply_approval_response(
                        manifest=manifest,
                        workflow_session_id=workflow_session_id,
                        source_type=effective_source_type,
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

        if source_type == "image":
            session_pointer = {
                "workflow_session_id": workflow_session_id,
                "latest_source_message_id": source_message_id,
                "active_review_source_message_id": locals().get("new_active_review_id", source_message_id),
                "pending_review_queue": locals().get("new_pending_queue", []),
                "total_received_count": locals().get("new_total", 1),
                "completed_review_count": int((locals().get("existing_session_pointer") or {}).get("completed_review_count") or 0),
                "source_type": source_type,
                "updated_at": timestamp,
            }
        else:
            existing_sp = self.read_session_pointer(workflow_session_id) or {}
            session_pointer = {
                "workflow_session_id": workflow_session_id,
                "latest_source_message_id": locals().get("session_pointer_source_message_id", source_message_id),
                "active_review_source_message_id": existing_sp.get("active_review_source_message_id"),
                "pending_review_queue": existing_sp.get("pending_review_queue", []),
                "total_received_count": existing_sp.get("total_received_count", 0),
                "completed_review_count": existing_sp.get("completed_review_count", 0),
                "source_type": source_type,
                "updated_at": timestamp,
            }
        self._write_json(self.session_pointer_path(workflow_session_id), session_pointer)
        self._maybe_send_image_acknowledgment(manifest)
        self._maybe_send_approval_acknowledgment(manifest)
        self._maybe_send_correction_acknowledgment(manifest)
        self._maybe_send_reject_acknowledgment(manifest)
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
