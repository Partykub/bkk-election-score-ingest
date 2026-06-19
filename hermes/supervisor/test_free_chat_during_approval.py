import io
import json
import tempfile
import unittest

from hermes.supervisor.intake_server import LocalStateStore, S3JsonStateBackend


class _MissingKeyError(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "NoSuchKey"}}


class _RecordingS3Client:
    def __init__(self) -> None:
        self.objects = {}

    def put_object(self, **kwargs):
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]
        return {"ETag": '"etag-test"'}

    def get_object(self, *, Bucket: str, Key: str):
        body = self.objects.get((Bucket, Key))
        if body is None:
            raise _MissingKeyError()
        return {"Body": io.BytesIO(body)}


class _RecordingReplySender:
    def __init__(self) -> None:
        self.messages = []

    def __call__(self, *, reply_token: str, text=None, messages=None) -> None:
        self.messages.append({"reply_token": reply_token, "text": text, "messages": messages})


class _RecordingChatClient:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.calls = []

    def __call__(self, *, messages):
        self.calls.append(messages)
        return {"choices": [{"message": {"content": self.response_text}}]}


class FreeChatDuringApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_generic_text_uses_free_chat_when_backend_is_available(self) -> None:
        s3_client = _RecordingS3Client()
        reply_sender = _RecordingReplySender()
        chat_client = _RecordingChatClient(response_text="chat-mode-ok")
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=object(),
            line_reply_sender=reply_sender,
            chat_completion_client=chat_client,
        )

        source_manifest = {
            "source_message_id": "src_01JXIMAGE893",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE893_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE893/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE893_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE893/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE893_r1",
            "draft_id": "draft_src_01JXIMAGE893_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE893_r1",
            "revision": 1,
            "report_type": "election_score_sheet",
            "candidate_scores": [{"candidate_number": 1, "score": 120}],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE893",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }

        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE893/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE893/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE893/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXTEXT893",
                "replyToken": "reply-token-text-2",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000894", "type": "text", "text": "ok"},
            },
            received_at="2026-06-08T07:57:00Z",
        )

        self.assertEqual(result.state, "received")
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertEqual(reply_sender.messages[0]["text"], "chat-mode-ok")
        self.assertEqual(len(chat_client.calls), 1)
        self.assertIsNone(reply_sender.messages[0]["messages"])


if __name__ == "__main__":
    unittest.main()
