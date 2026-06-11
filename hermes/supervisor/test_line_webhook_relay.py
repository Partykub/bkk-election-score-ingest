import json
import threading
import unittest
from unittest import mock
from types import SimpleNamespace
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from hermes.supervisor.intake_server import build_correction_form_token
from hermes.supervisor.line_webhook_relay import make_handler


class _RecordingStore:
    def __init__(self) -> None:
        self.events = []
        self.manifests = {}

    def persist_line_event(self, event, received_at=None):
        self.events.append(event)
        return SimpleNamespace(
            source_message_id=f"src_{event.get('webhookEventId', 'unknown')}",
            line_event_id=event.get("webhookEventId", "unknown"),
            state="queued",
            deduplicated=False,
            source_type=((event.get("message") or {}).get("type") or event.get("type") or "unknown"),
        )

    def read_manifest(self, source_message_id):
        return self.manifests.get(source_message_id)


class _RecordingProxyClient:
    def __init__(self, *, status: int = 200, body: bytes | None = None) -> None:
        self.status = status
        self.body = body or b'{"ok":true}'
        self.requests = []

    def __call__(self, *, method: str, url: str, body: bytes | None = None, headers: dict[str, str] | None = None):
        self.requests.append({"method": method, "url": url, "body": body, "headers": headers or {}})
        return self.status, {"Content-Type": "application/json"}, self.body


class LineWebhookRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _RecordingStore()
        self.proxy_client = _RecordingProxyClient()
        handler = make_handler(store=self.store, upstream_base_url="http://127.0.0.1:8647", proxy_client=self.proxy_client)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_post_line_webhook_proxies_and_persists_events(self) -> None:
        payload = {
            "events": [
                {
                    "type": "message",
                    "webhookEventId": "01JXIMAGE001",
                    "replyToken": "reply-token-1",
                    "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                    "message": {"id": "548899112233", "type": "image"},
                }
            ]
        }
        response = self._request(
            method="POST",
            path="/line/webhook",
            body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Line-Signature": "sig-123"},
        )

        self.assertEqual(response["status"], 200)
        self.assertEqual(len(self.proxy_client.requests), 0)
        self.assertEqual(len(self.store.events), 1)
        self.assertEqual(self.store.events[0]["webhookEventId"], "01JXIMAGE001")

    def test_post_line_webhook_returns_bad_request_for_invalid_events_shape(self) -> None:
        payload = {"events": {"webhookEventId": "01JXFAIL001"}}
        response = self._request(
            method="POST",
            path="/line/webhook",
            body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response["status"], 400)
        self.assertEqual(len(self.store.events), 0)

    def test_health_proxies_to_hermes(self) -> None:
        response = self._request(method="GET", path="/line/webhook/health")

        self.assertEqual(response["status"], 200)
        self.assertEqual(self.proxy_client.requests[0]["url"], "http://127.0.0.1:8647/line/webhook/health")
        self.assertEqual(len(self.store.events), 0)

    def test_get_correction_liff_page_returns_html(self) -> None:
        token = build_correction_form_token(source_message_id="src_01JXIMAGE001", approval_id="approval_src_01JXIMAGE001_r1")
        with mock.patch.dict("os.environ", {"LINE_LIFF_CORRECTION_ID": "123456-test"}, clear=False):
            response = self._request(
                method="GET",
                path=f"/line/liff/correction?source_message_id=src_01JXIMAGE001&approval_id=approval_src_01JXIMAGE001_r1&token={token}",
            )

        self.assertEqual(response["status"], 200)
        self.assertEqual(len(self.proxy_client.requests), 0)
        body = response["body"].decode("utf-8")
        self.assertIn("<form id=\"correction-form\">", body)
        self.assertIn('const correctionLiffId = "123456-test";', body)
        self.assertIn("liff.sendMessages", body)

    def test_post_correction_form_creates_synthetic_correction_event(self) -> None:
        self.store.manifests["src_01JXIMAGE001"] = {
            "source_message_id": "src_01JXIMAGE001",
            "current_approval_id": "approval_src_01JXIMAGE001_r1",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "sender_group_id": "C123",
        }
        token = build_correction_form_token(source_message_id="src_01JXIMAGE001", approval_id="approval_src_01JXIMAGE001_r1")
        response = self._request(
            method="POST",
            path="/line/liff/correction/submit",
            body=f"source_message_id=src_01JXIMAGE001&approval_id=approval_src_01JXIMAGE001_r1&token={token}&candidate_number=4&score=14".encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        self.assertEqual(response["status"], 200)
        self.assertEqual(len(self.store.events), 1)
        self.assertEqual(self.store.events[0]["message"]["text"], "แก้ไข 4=14")
        self.assertEqual(self.store.events[0]["source"]["groupId"], "C123")

    def _request(self, *, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return {"status": response.status, "body": payload}


if __name__ == "__main__":
    unittest.main()
