from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, parse, request
from urllib.parse import parse_qs, urlsplit

from hermes.supervisor.intake_server import (
    LocalStateStore,
    build_correction_form_token,
    load_env_file,
)
from hermes.supervisor.services.static_results_export import export_static_governor_results


def copy_request_headers(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in handler.headers.items():
        if key.lower() in {"host", "content-length"}:
            continue
        headers[key] = value
    return headers


def forward_http_request(*, method: str, url: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    upstream_request = request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with request.urlopen(upstream_request, timeout=30) as response:
            return response.status, dict(response.headers.items()), response.read()
    except error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def process_line_payload(store: LocalStateStore, raw_payload: bytes) -> dict[str, Any]:
    payload = json.loads(raw_payload.decode("utf-8"))
    events = payload.get("events")
    if not isinstance(events, list):
        raise ValueError("events must be an array")

    processed = [store.persist_line_event(event) for event in events if isinstance(event, dict)]
    return {
        "processed_count": len(processed),
        "new_count": sum(1 for item in processed if not item.deduplicated),
        "duplicate_count": sum(1 for item in processed if item.deduplicated),
        "results": [
            {
                "source_message_id": item.source_message_id,
                "line_event_id": item.line_event_id,
                "state": item.state,
                "deduplicated": item.deduplicated,
                "source_type": item.source_type,
            }
            for item in processed
        ],
    }


def build_correction_form_html(*, liff_id: str | None, source_message_id: str, approval_id: str, token: str) -> str:
    escaped_liff_id = (liff_id or "").replace("\\", "\\\\").replace('"', '\\"')
    escaped_source_message_id = source_message_id.replace("\\", "\\\\").replace('"', '\\"')
    escaped_approval_id = approval_id.replace("\\", "\\\\").replace('"', '\\"')
    escaped_token = token.replace("\\", "\\\\").replace('"', '\\"')
    return f"""<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>BKK Election Correction</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4efe7;
      --card: #fffdf9;
      --ink: #1f1b16;
      --muted: #6a6258;
      --accent: #0d5c63;
      --accent-strong: #083c40;
      --border: #d9d0c3;
      --danger: #8a1c1c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Segoe UI", Tahoma, sans-serif;
      background:
        radial-gradient(circle at top left, rgba(13, 92, 99, 0.12), transparent 34%),
        linear-gradient(180deg, #f8f3ec 0%, var(--bg) 100%);
      color: var(--ink);
    }}
    .wrap {{ max-width: 520px; margin: 0 auto; padding: 24px 16px 32px; }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 20px;
      box-shadow: 0 18px 48px rgba(31, 27, 22, 0.08);
      overflow: hidden;
    }}
    .hero {{
      padding: 24px 24px 18px;
      background: linear-gradient(135deg, rgba(13, 92, 99, 0.95), rgba(8, 60, 64, 0.92));
      color: #fff;
    }}
    .hero h1 {{ margin: 0; font-size: 28px; line-height: 1.1; }}
    .hero p {{ margin: 10px 0 0; font-size: 15px; line-height: 1.5; color: rgba(255, 255, 255, 0.88); }}
    form {{ padding: 20px 24px 24px; }}
    label {{ display: block; margin-bottom: 8px; font-size: 14px; font-weight: 700; }}
    .field {{ margin-bottom: 18px; }}
    input {{
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px 16px;
      font-size: 18px;
      color: var(--ink);
      background: #fff;
    }}
    input:focus {{ outline: 2px solid rgba(13, 92, 99, 0.18); border-color: var(--accent); }}
    .hint {{ font-size: 13px; line-height: 1.5; color: var(--muted); }}
    .preview {{
      margin: 20px 0;
      border-radius: 16px;
      background: #f7f2eb;
      border: 1px dashed var(--border);
      padding: 14px 16px;
    }}
    .preview code {{
      display: block;
      margin-top: 6px;
      font-size: 18px;
      color: var(--accent-strong);
      white-space: pre-wrap;
    }}
    .actions {{ display: flex; gap: 12px; margin-top: 20px; }}
    button {{
      appearance: none;
      border: 0;
      border-radius: 14px;
      padding: 14px 16px;
      font-size: 17px;
      font-weight: 700;
      cursor: pointer;
    }}
    .submit {{ flex: 1; background: var(--accent); color: #fff; }}
    .submit:disabled {{ opacity: 0.6; cursor: not-allowed; }}
    .close {{ background: #ece4d8; color: var(--ink); min-width: 108px; }}
    .status {{ margin-top: 16px; font-size: 14px; line-height: 1.5; color: var(--muted); }}
    .status.error {{ color: var(--danger); }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="hero">
        <h1>แก้ไขคะแนน</h1>
        <p>กรอกเบอร์ผู้สมัครและคะแนนใหม่ แล้วระบบจะส่งคำสั่งแก้ไขกลับเข้า workflow เดิมให้อัตโนมัติ</p>
      </div>
      <form id="correction-form">
        <div class="field">
          <label for="candidate-number">ผู้สมัครเบอร์</label>
          <input id="candidate-number" name="candidate-number" type="number" min="1" inputmode="numeric" placeholder="เช่น 4" required>
        </div>
        <div class="field">
          <label for="candidate-score">คะแนนใหม่</label>
          <input id="candidate-score" name="candidate-score" type="number" min="0" inputmode="numeric" placeholder="เช่น 14" required>
        </div>
        <div class="hint">ระบบจะส่งคำสั่งในรูปแบบ <strong>แก้ไข 4=14</strong></div>
        <div class="preview">
          ข้อความที่จะส่ง
          <code id="message-preview">แก้ไข 4=14</code>
        </div>
        <div class="actions">
          <button class="submit" id="submit-button" type="submit">ส่งการแก้ไข</button>
          <button class="close" type="button" id="close-button">ปิด</button>
        </div>
        <div class="status" id="status-text">พร้อมส่งการแก้ไข</div>
      </form>
    </div>
  </div>
  <script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
  <script>
    const correctionLiffId = "{escaped_liff_id}";
    const sourceMessageId = "{escaped_source_message_id}";
    const approvalId = "{escaped_approval_id}";
    const approvalToken = "{escaped_token}";
    const candidateInput = document.getElementById("candidate-number");
    const scoreInput = document.getElementById("candidate-score");
    const preview = document.getElementById("message-preview");
    const statusText = document.getElementById("status-text");
    const submitButton = document.getElementById("submit-button");
    const form = document.getElementById("correction-form");
    const closeButton = document.getElementById("close-button");
    let liffReady = false;

    function buildMessageText() {{
      const candidate = (candidateInput.value || "").trim();
      const score = (scoreInput.value || "").trim();
      if (!candidate || !score) {{
        return "แก้ไข 4=14";
      }}
      return `แก้ไข ${{candidate}}=${{score}}`;
    }}

    function refreshPreview() {{
      preview.textContent = buildMessageText();
    }}

    async function initializeLiff() {{
      if (!correctionLiffId) {{
        return;
      }}
      try {{
        await liff.init({{ liffId: correctionLiffId }});
        liffReady = liff.isInClient();
      }} catch (error) {{
        liffReady = false;
      }}
    }}

    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const candidate = (candidateInput.value || "").trim();
      const score = (scoreInput.value || "").trim();
      if (!candidate || !score) {{
        statusText.textContent = "กรุณากรอกเบอร์ผู้สมัครและคะแนนใหม่ให้ครบ";
        statusText.className = "status error";
        return;
      }}

      submitButton.disabled = true;
      statusText.textContent = "กำลังส่งการแก้ไข...";
      statusText.className = "status";
      try {{
        if (liffReady) {{
          await liff.sendMessages([{{ type: "text", text: buildMessageText() }}]);
          statusText.textContent = "ส่งการแก้ไขแล้ว ระบบจะสร้างร่างใหม่ในแชท";
          setTimeout(() => liff.closeWindow(), 600);
          return;
        }}

        const response = await fetch("/line/liff/correction/submit", {{
          method: "POST",
          headers: {{ "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" }},
          body: new URLSearchParams({{
            source_message_id: sourceMessageId,
            approval_id: approvalId,
            token: approvalToken,
            candidate_number: candidate,
            score: score,
          }}),
        }});
        const payload = await response.json();
        if (!response.ok || payload.ok !== true) {{
          throw new Error((payload.error && payload.error.message) || "submit_failed");
        }}
        statusText.textContent = "ส่งการแก้ไขแล้ว ระบบจะสร้างร่างใหม่ในแชท";
      }} catch (error) {{
        submitButton.disabled = false;
        statusText.textContent = `ส่งการแก้ไขไม่สำเร็จ: ${{error.message || error}}`;
        statusText.className = "status error";
      }}
    }});

    closeButton.addEventListener("click", () => {{
      if (window.liff && liffReady) {{
        liff.closeWindow();
        return;
      }}
      window.close();
    }});

    candidateInput.addEventListener("input", refreshPreview);
    scoreInput.addEventListener("input", refreshPreview);
    refreshPreview();
    initializeLiff();
  </script>
</body>
</html>
"""


def process_correction_form_submission(store: LocalStateStore, form_fields: dict[str, str]) -> dict[str, Any]:
    source_message_id = str(form_fields.get("source_message_id") or "").strip()
    approval_id = str(form_fields.get("approval_id") or "").strip()
    submitted_token = str(form_fields.get("token") or "").strip()
    candidate_number = str(form_fields.get("candidate_number") or "").strip()
    score = str(form_fields.get("score") or "").strip()

    if not source_message_id or not approval_id or not submitted_token:
        raise ValueError("missing source_message_id, approval_id, or token")
    if not candidate_number or not score:
        raise ValueError("missing candidate_number or score")

    expected_token = build_correction_form_token(source_message_id=source_message_id, approval_id=approval_id)
    if not hmac.compare_digest(submitted_token, expected_token):
        raise PermissionError("invalid correction form token")

    source_manifest = store.read_manifest(source_message_id)
    if source_manifest is None:
        raise FileNotFoundError("source message not found")
    if str(source_manifest.get("current_approval_id") or "").strip() != approval_id:
        raise ValueError("approval round has changed")
    if str(source_manifest.get("state") or "").strip() != "awaiting_approval":
        raise ValueError("approval round is no longer awaiting approval")

    source: dict[str, Any] = {"type": "user"}
    if source_manifest.get("sender_group_id"):
        source["type"] = "group"
        source["groupId"] = source_manifest["sender_group_id"]
    elif source_manifest.get("sender_room_id"):
        source["type"] = "room"
        source["roomId"] = source_manifest["sender_room_id"]
    if source_manifest.get("sender_user_id"):
        source["userId"] = source_manifest["sender_user_id"]

    correction_text = f"แก้ไข {candidate_number}={score}"
    event = {
        "type": "message",
        "webhookEventId": f"form-{uuid.uuid4().hex}",
        "replyToken": None,
        "source": source,
        "message": {
            "id": f"form-{uuid.uuid4().hex}",
            "type": "text",
            "text": correction_text,
        },
    }
    processed = store.persist_line_event(event)
    return {
        "ok": True,
        "submitted_text": correction_text,
        "state": processed.state,
        "target_source_message_id": source_message_id,
        "new_source_message_id": processed.source_message_id,
    }


def make_handler(
    *,
    store: LocalStateStore,
    upstream_base_url: str,
    proxy_client: Any = forward_http_request,
):
    class RelayHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            request_url = urlsplit(self.path)
            request_path = request_url.path

            if request_path == "/line/liff/correction":
                query = parse_qs(request_url.query, keep_blank_values=True)
                source_message_id = str((query.get("source_message_id") or [""])[0]).strip()
                approval_id = str((query.get("approval_id") or [""])[0]).strip()
                token = str((query.get("token") or [""])[0]).strip()
                if not source_message_id or not approval_id or not token:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": {"code": "INVALID_CORRECTION_LINK", "message": "missing source_message_id, approval_id, or token"}},
                    )
                    return

                body = build_correction_form_html(
                    liff_id=os.environ.get("LINE_LIFF_CORRECTION_ID"),
                    source_message_id=source_message_id,
                    approval_id=approval_id,
                    token=token,
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if request_path != "/line/webhook/health":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            status, headers, body = proxy_client(
                method="GET",
                url=f"{upstream_base_url}{request_path}",
                headers=copy_request_headers(self),
            )
            self._send_upstream_response(status, headers, body)

        def do_POST(self) -> None:
            if self.path == "/line/liff/correction/submit":
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_payload = self.rfile.read(content_length)
                form_fields = {
                    key: values[0]
                    for key, values in parse_qs(raw_payload.decode("utf-8"), keep_blank_values=True).items()
                    if values
                }
                try:
                    response_payload = process_correction_form_submission(store, form_fields)
                except PermissionError as exc:
                    self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": {"code": "INVALID_TOKEN", "message": str(exc)}})
                    return
                except FileNotFoundError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": {"code": "SOURCE_NOT_FOUND", "message": str(exc)}})
                    return
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": {"code": "INVALID_FORM", "message": str(exc)}})
                    return
                except Exception as exc:
                    print(f"line relay: unable to process correction form: {exc}", file=sys.stderr)
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": {"code": "CORRECTION_SUBMIT_FAILED", "message": str(exc)}})
                    return

                self._send_json(HTTPStatus.OK, response_payload)
                return

            if self.path not in {"/line/webhook", "/webhook"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            raw_payload = self.rfile.read(content_length)
            try:
                response_payload = process_line_payload(store, raw_payload)
            except json.JSONDecodeError:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "INVALID_JSON", "message": "request body must be valid json"}},
                )
                return
            except ValueError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "INVALID_REQUEST", "message": str(exc)}},
                )
                return
            except Exception as exc:
                print(f"line relay: unable to process intake payload: {exc}", file=sys.stderr)
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": {"code": "PERSIST_FAILED", "message": str(exc)}},
                )
                return

            try:
                static_export = export_static_governor_results()
                if static_export:
                    response_payload["staticExport"] = static_export
                    print(json.dumps({"service": "line-relay", **static_export}, ensure_ascii=False))
            except Exception as exc:
                print(f"line relay: unable to export static results: {exc}", file=sys.stderr)

            self._send_json(HTTPStatus.OK, response_payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_upstream_response(self, status: int, headers: dict[str, str], body: bytes) -> None:
            self.send_response(status)
            for key, value in headers.items():
                if key.lower() in {"connection", "content-length", "transfer-encoding", "date", "server"}:
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return RelayHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Relay LINE webhook traffic to Hermes and persist intake state")
    parser.add_argument(
        "--env-file",
        default=str(Path(__file__).with_name(".env")),
        help="optional .env file to preload before starting the relay",
    )
    parser.add_argument("--host", default=os.environ.get("SUPERVISOR_RELAY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SUPERVISOR_RELAY_PORT", "8646")))
    parser.add_argument(
        "--upstream-base-url",
        default=os.environ.get(
            "SUPERVISOR_HERMES_LINE_UPSTREAM_URL",
            f"http://127.0.0.1:{os.environ.get('HERMES_SUPERVISOR_LINE_PORT', '8647')}",
        ),
        help="Hermes LINE adapter base URL, without a trailing path",
    )
    parser.add_argument(
        "--state-root",
        default=os.environ.get("SUPERVISOR_STATE_ROOT", str(Path("storage") / "local-state")),
        help="local state root used only when SUPERVISOR_STORAGE_BACKEND=local-mock",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_env_file(args.env_file)
    store = LocalStateStore(args.state_root)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store=store, upstream_base_url=args.upstream_base_url.rstrip("/")))
    print(f"line webhook relay listening on http://{args.host}:{args.port} -> {args.upstream_base_url}")
    server.serve_forever()


if __name__ == "__main__":
    main()
