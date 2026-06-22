import io
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from hermes.supervisor.intake_server import LocalStateStore, S3JsonStateBackend, parse_area_id_override


class _FakeMissingKeyError(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "NoSuchKey"}}


class _RecordingS3Client:
    def __init__(self) -> None:
        self.objects = {}

    def put_object(self, **kwargs):
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]
        return {"ETag": '"etag-test"'}

    def get_object(self, *, Bucket: str, Key: str):
        object_body = self.objects.get((Bucket, Key))
        if object_body is None:
            raise _FakeMissingKeyError()
        return {"Body": io.BytesIO(object_body)}


class _RecordingQueueClient:
    def __init__(self) -> None:
        self.messages = []

    def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"MessageId": "msg-1"}


class _RecordingReplySender:
    def __init__(self) -> None:
        self.messages = []

    def __call__(self, *, reply_token: str, text=None, messages=None) -> None:
        self.messages.append({"reply_token": reply_token, "text": text, "messages": messages})


class _RecordingPushSender:
    def __init__(self) -> None:
        self.messages = []

    def __call__(self, *, destination_id: str, text=None, messages=None) -> None:
        self.messages.append({"destination_id": destination_id, "text": text, "messages": messages})


class _RecordingChatClient:
    def __init__(self, response_text: str = "สวัสดีจากแชตบอท", *, should_fail: bool = False) -> None:
        self.response_text = response_text
        self.should_fail = should_fail
        self.calls = []

    def __call__(self, *, messages) -> dict:
        self.calls.append(messages)
        if self.should_fail:
            raise RuntimeError("chat backend unavailable")
        return {"choices": [{"message": {"content": self.response_text}}]}


from hermes.supervisor.intake_server import LocalStateStore, S3JsonStateBackend, SqsOcrJobQueue, SqsUpdateJobQueue


class _FakeUploadSession:
    def __init__(self, storage_backend: str) -> None:
        self.upload_session_id = "upl_src_01JXIMAGE001"
        self.bucket = "election-system"
        self.object_key = "messages/src_01JXIMAGE001/original.bin"
        self.metadata_key = "messages/src_01JXIMAGE001/upload_metadata.json"
        self.source_message_manifest_key = "messages/src_01JXIMAGE001/manifest.json"
        self.state = "stored"
        self.storage_backend = storage_backend


class _FakeUploadService:
    def __init__(self, storage_backend: str = "s3") -> None:
        self.storage_backend = storage_backend

    def store_source_message(self, manifest, *, received_at: str):
        return _FakeUploadSession(self.storage_backend)


class LocalStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = LocalStateStore(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_persist_image_event_creates_manifest_and_indexes(self) -> None:
        event = {
            "type": "message",
            "webhookEventId": "01JXIMAGE001",
            "replyToken": "reply-token-1",
            "source": {"type": "group", "groupId": "C123", "userId": "U123"},
            "message": {"id": "548899112233", "type": "image"},
        }

        result = self.store.persist_line_event(event, received_at="2026-06-08T07:30:00Z")

        self.assertFalse(result.deduplicated)
        self.assertEqual(result.source_type, "image")
        self.assertEqual(result.state, "stored")

        manifest_path = Path(self.temp_dir.name) / "messages" / "src_01JXIMAGE001" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["workflow_session_id"], "line_group_C123")
        self.assertEqual(manifest["state"], "stored")
        self.assertEqual(manifest["source_type"], "image")
        self.assertEqual(manifest["dedupe_event_key"], "line:event:01JXIMAGE001")
        self.assertEqual(manifest["upload_session_id"], "upl_src_01JXIMAGE001")
        self.assertEqual(manifest["media"]["bucket"], "local-election-system")
        self.assertEqual(
            manifest["media"]["key"],
            "messages/src_01JXIMAGE001/original.bin",
        )

        event_index_path = Path(self.temp_dir.name) / "events" / "01JXIMAGE001.json"
        self.assertTrue(event_index_path.exists())

        metadata_path = Path(self.temp_dir.name) / "messages" / "src_01JXIMAGE001" / "upload_metadata.json"
        self.assertTrue(metadata_path.exists())
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["storage_backend"], "local-mock")
        self.assertEqual(metadata["binary_status"], "pending_line_content_fetch")

        session_pointer = Path(self.temp_dir.name) / "sessions" / "line_group_C123" / "latest.json"
        session_payload = json.loads(session_pointer.read_text(encoding="utf-8"))
        self.assertEqual(session_payload["latest_source_message_id"], "src_01JXIMAGE001")

    def test_duplicate_event_reuses_existing_manifest(self) -> None:
        event = {
            "type": "message",
            "webhookEventId": "01JXDUP001",
            "replyToken": "reply-token-1",
            "source": {"type": "user", "userId": "U123"},
            "message": {"id": "548899112233", "type": "image"},
        }

        first_result = self.store.persist_line_event(event, received_at="2026-06-08T07:30:00Z")
        duplicate_result = self.store.persist_line_event(event, received_at="2026-06-08T07:31:00Z")

        self.assertFalse(first_result.deduplicated)
        self.assertTrue(duplicate_result.deduplicated)
        self.assertEqual(first_result.source_message_id, duplicate_result.source_message_id)

        manifest_path = Path(self.temp_dir.name) / "messages" / "src_01JXDUP001" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["created_at"], "2026-06-08T07:30:00Z")
        self.assertEqual(manifest["updated_at"], "2026-06-08T07:30:00Z")

    def test_text_command_is_classified(self) -> None:
        event = {
            "type": "message",
            "webhookEventId": "01JXTEXT001",
            "replyToken": "reply-token-1",
            "source": {"type": "room", "roomId": "R999", "userId": "U123"},
            "message": {"id": "99001122", "type": "text", "text": "ยืนยัน"},
        }

        result = self.store.persist_line_event(event, received_at="2026-06-08T07:30:00Z")

        self.assertEqual(result.source_type, "approval_command")
        manifest_path = Path(self.temp_dir.name) / "messages" / "src_01JXTEXT001" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["workflow_session_id"], "line_room_R999")
        self.assertEqual(manifest["source_text"], "ยืนยัน")
        self.assertEqual(manifest["exception"]["code"], "APPROVAL_NOT_FOUND")

    def test_approval_command_applies_to_latest_source_manifest_and_creates_update_job(self) -> None:
        s3_client = _RecordingS3Client()
        queue_client = _RecordingQueueClient()
        reply_sender = _RecordingReplySender()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        update_job_queue = SqsUpdateJobQueue(
            queue_url="https://sqs.ap-southeast-1.amazonaws.com/123/update-jobs.fifo",
            region_name="ap-southeast-1",
            client=queue_client,
        )
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            update_job_queue=update_job_queue,
            line_reply_sender=reply_sender,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE777",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE777_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE777/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE777_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE777/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE777_r1",
            "draft_id": "draft_src_01JXIMAGE777_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE777_r1",
            "result_signature": "area12:1=120|2=90",
            "election_id": "election-2026",
            "area_id": "12",
            "polling_unit_id": "07",
            "report_type": "election_score_sheet",
            "candidate_scores": [{"candidate_number": 1, "score": 120}],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE777",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE777/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE777/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE777/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXAPPROVE777",
                "replyToken": "reply-token-approve",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000777", "type": "text", "text": "ยืนยัน"},
            },
            received_at="2026-06-08T07:45:00Z",
        )

        self.assertEqual(result.state, "approved")
        updated_source = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE777/manifest.json")].decode("utf-8"))
        updated_approval = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE777/approval_r1.json")].decode("utf-8"))
        self.assertEqual(updated_source["state"], "approved")
        self.assertTrue(updated_source["current_update_job_id"].startswith("upd_approval_src_01JXIMAGE777_r1"))
        self.assertEqual(updated_approval["state"], "approved")
        self.assertEqual(updated_approval["approved_by_user_id"], "U123")
        self.assertIn(("election-system", "dev/messages/src_01JXIMAGE777/update_job.json"), s3_client.objects)
        self.assertEqual(len(queue_client.messages), 1)
        queue_payload = json.loads(queue_client.messages[0]["MessageBody"])
        self.assertEqual(queue_payload["update_job_id"], "upd_approval_src_01JXIMAGE777_r1")
        self.assertEqual(queue_payload["manifest_bucket"], "election-system")
        self.assertEqual(queue_payload["manifest_key"], "dev/messages/src_01JXIMAGE777/update_job.json")
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertIn("รับรองผลเรียบร้อยแล้ว", reply_sender.messages[0]["text"])
        self.assertIn("ผลร่างล่าสุด: ครั้งที่ 1", reply_sender.messages[0]["text"])
        self.assertIn("ผู้สมัคร 1: 120", reply_sender.messages[0]["text"])
        self.assertIsNone(reply_sender.messages[0]["messages"])

    def test_approval_command_with_missing_area_requires_correction_before_save(self) -> None:
        s3_client = _RecordingS3Client()
        queue_client = _RecordingQueueClient()
        reply_sender = _RecordingReplySender()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        update_job_queue = SqsUpdateJobQueue(
            queue_url="https://sqs.ap-southeast-1.amazonaws.com/123/update-jobs.fifo",
            region_name="ap-southeast-1",
            client=queue_client,
        )
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            update_job_queue=update_job_queue,
            line_reply_sender=reply_sender,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE777A",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE777A_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE777A/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE777A_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE777A/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE777A_r1",
            "draft_id": "draft_src_01JXIMAGE777A_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE777A_r1",
            "result_signature": "unknown-area:1=120|2=90",
            "election_id": "election-2026",
            "area_id": None,
            "polling_unit_id": "07",
            "report_type": "election_score_sheet",
            "candidate_scores": [{"candidate_number": 1, "score": 120}],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE777A",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE777A/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE777A/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE777A/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXAPPROVE777A",
                "replyToken": "reply-token-approve-a",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000777A", "type": "text", "text": "ยืนยัน"},
            },
            received_at="2026-06-08T07:45:00Z",
        )

        self.assertEqual(result.state, "exception")
        updated_source = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE777A/manifest.json")].decode("utf-8"))
        updated_approval = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE777A/approval_r1.json")].decode("utf-8"))
        self.assertEqual(updated_source["state"], "awaiting_approval")
        self.assertEqual(updated_source["pending_user_action"], "awaiting_correction_input")
        self.assertEqual(updated_approval["state"], "awaiting_approval")
        self.assertEqual(len(queue_client.messages), 0)
        self.assertNotIn(("election-system", "dev/messages/src_01JXIMAGE777A/update_job.json"), s3_client.objects)
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertIn("ยังไม่พบเขต", reply_sender.messages[0]["text"])
        self.assertIn("แก้ไข เขต 13", reply_sender.messages[0]["text"])

    def test_approval_command_with_missing_candidate_scores_requires_correction_before_save(self) -> None:
        s3_client = _RecordingS3Client()
        queue_client = _RecordingQueueClient()
        reply_sender = _RecordingReplySender()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        update_job_queue = SqsUpdateJobQueue(
            queue_url="https://sqs.ap-southeast-1.amazonaws.com/123/update-jobs.fifo",
            region_name="ap-southeast-1",
            client=queue_client,
        )
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            update_job_queue=update_job_queue,
            line_reply_sender=reply_sender,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE777B",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE777B_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE777B/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE777B_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE777B/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE777B_r1",
            "draft_id": "draft_src_01JXIMAGE777B_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE777B_r1",
            "election_id": "election-2026",
            "area_id": "13",
            "polling_unit_id": "07",
            "report_type": "election_score_sheet",
            "candidate_scores": [],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE777B",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE777B/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE777B/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE777B/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXAPPROVE777B",
                "replyToken": "reply-token-approve-b",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000777B", "type": "text", "text": "ยืนยัน"},
            },
            received_at="2026-06-08T07:45:00Z",
        )

        self.assertEqual(result.state, "exception")
        updated_source = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE777B/manifest.json")].decode("utf-8"))
        updated_approval = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE777B/approval_r1.json")].decode("utf-8"))
        self.assertEqual(updated_source["state"], "awaiting_approval")
        self.assertEqual(updated_source["pending_user_action"], "awaiting_correction_input")
        self.assertEqual(updated_approval["state"], "awaiting_approval")
        self.assertEqual(len(queue_client.messages), 0)
        self.assertNotIn(("election-system", "dev/messages/src_01JXIMAGE777B/update_job.json"), s3_client.objects)
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertIn("ยังไม่พบคะแนนผู้สมัคร", reply_sender.messages[0]["text"])
        self.assertIn("แก้ไข 4=14", reply_sender.messages[0]["text"])

    def helper_approval_command_allows_ballot_summary_without_candidate_scores(self) -> None:
        s3_client = _RecordingS3Client()
        queue_client = _RecordingQueueClient()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        update_job_queue = SqsUpdateJobQueue(
            queue_url="https://sqs.ap-southeast-1.amazonaws.com/123/update-jobs.fifo",
            region_name="ap-southeast-1",
            client=queue_client,
        )
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            update_job_queue=update_job_queue,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE777C",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE777C_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE777C/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE777C_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE777C/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE777C_r1",
            "draft_id": "draft_src_01JXIMAGE777C_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE777C_r1",
            "election_id": "election-2026",
            "area_id": "13",
            "polling_unit_id": "07",
            "report_type": "ballot_summary",
            "eligible_voters": 100,
            "voter_turnout": 80,
            "valid_ballots": 70,
            "invalid_ballots": 5,
            "vote_no": 5,
            "candidate_scores": [],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE777C",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE777C/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE777C/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE777C/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXAPPROVE777C",
                "replyToken": "reply-token-approve-c",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000777C", "type": "text", "text": "เธขเธทเธเธขเธฑเธ"},
            },
            received_at="2026-06-08T07:45:00Z",
        )

        self.assertEqual(result.state, "approved")
        updated_approval = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE777C/approval_r1.json")].decode("utf-8"))
        self.assertEqual(updated_approval["state"], "approved")
        self.assertEqual(len(queue_client.messages), 1)

    def test_approval_command_allows_ballot_summary_without_candidate_scores(self) -> None:
        s3_client = _RecordingS3Client()
        queue_client = _RecordingQueueClient()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        update_job_queue = SqsUpdateJobQueue(
            queue_url="https://sqs.ap-southeast-1.amazonaws.com/123/update-jobs.fifo",
            region_name="ap-southeast-1",
            client=queue_client,
        )
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            update_job_queue=update_job_queue,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE777C",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE777C_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE777C/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE777C_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE777C/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE777C_r1",
            "draft_id": "draft_src_01JXIMAGE777C_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE777C_r1",
            "election_id": "election-2026",
            "area_id": "13",
            "polling_unit_id": "07",
            "report_type": "ballot_summary",
            "eligible_voters": 100,
            "voter_turnout": 80,
            "valid_ballots": 70,
            "invalid_ballots": 5,
            "vote_no": 5,
            "candidate_scores": [],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE777C",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE777C/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE777C/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE777C/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXAPPROVE777C",
                "replyToken": "reply-token-approve-c",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000777C", "type": "text", "text": "\u0e22\u0e37\u0e19\u0e22\u0e31\u0e19"},
            },
            received_at="2026-06-08T07:45:00Z",
        )

        self.assertEqual(result.state, "approved")
        updated_approval = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE777C/approval_r1.json")].decode("utf-8"))
        self.assertEqual(updated_approval["state"], "approved")
        self.assertEqual(len(queue_client.messages), 1)

    def test_correction_command_creates_corrected_draft_and_new_approval(self) -> None:
        s3_client = _RecordingS3Client()
        push_sender = _RecordingPushSender()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            line_push_sender=push_sender,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE888",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE888_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE888/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE888_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE888/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE888_r1",
            "draft_id": "draft_src_01JXIMAGE888_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE888_r1",
            "report_type": "election_score_sheet",
            "candidate_scores": [{"candidate_number": 1, "score": 120}],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE888",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE888/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE888/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE888/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXCORRECT888",
                "replyToken": "reply-token-correct",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000888", "type": "text", "text": "แก้ไข คะแนนผู้สมัคร 1 ควรเป็น 121"},
            },
            received_at="2026-06-08T07:50:00Z",
        )

        self.assertEqual(result.state, "awaiting_approval")
        updated_source = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE888/manifest.json")].decode("utf-8"))
        updated_approval = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE888/approval_r1.json")].decode("utf-8"))
        corrected_draft = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE888/draft_r2.json")].decode("utf-8"))
        latest_draft_pointer = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE888/draft_latest.json")].decode("utf-8"))
        next_approval = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE888/approval_r2.json")].decode("utf-8"))
        latest_approval_pointer = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE888/approval_latest.json")].decode("utf-8"))
        self.assertEqual(updated_source["state"], "awaiting_approval")
        self.assertEqual(updated_approval["state"], "rejected")
        self.assertEqual(updated_approval["rejected_by_user_id"], "U123")
        self.assertEqual(corrected_draft["draft_id"], "draft_src_01JXIMAGE888_r2")
        self.assertEqual(corrected_draft["candidate_scores"][0]["score"], 121)
        self.assertEqual(corrected_draft["created_by"], "line_correction")
        self.assertEqual(corrected_draft["corrected_from_draft_id"], "draft_src_01JXIMAGE888_r1")
        self.assertEqual(corrected_draft["correction_source_message_id"], "src_01JXCORRECT888")
        self.assertEqual(latest_draft_pointer["draft_id"], "draft_src_01JXIMAGE888_r2")
        self.assertEqual(next_approval["state"], "awaiting_approval")
        self.assertEqual(next_approval["draft_revision"], 2)
        self.assertEqual(latest_approval_pointer["approval_id"], "approval_src_01JXIMAGE888_r2")
        self.assertEqual(updated_source["approval_prompt"]["draft_id"], "draft_src_01JXIMAGE888_r2")
        self.assertEqual(updated_source["approval_prompt"]["status"], "sent")
        self.assertEqual(len(push_sender.messages), 1)
        self.assertEqual(push_sender.messages[0]["destination_id"], "U123")
        self.assertIn("ร่างครั้งที่ 2", push_sender.messages[0]["messages"][0]["text"])
        self.assertIn("กรุณาระบุเขต", push_sender.messages[0]["messages"][0]["text"])
        quick_reply_actions = [
            item["action"]["text"] for item in push_sender.messages[0]["messages"][0]["quickReply"]["items"]
        ]
        self.assertEqual(quick_reply_actions, ["แก้ไข", "ไม่ถูกต้อง"])
        self.assertNotIn(("election-system", "dev/messages/src_01JXIMAGE888/update_job.json"), s3_client.objects)

    def test_reject_command_replies_with_closed_round_message(self) -> None:
        s3_client = _RecordingS3Client()
        reply_sender = _RecordingReplySender()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            line_reply_sender=reply_sender,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE889",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE889_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE889/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE889_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE889/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE889_r1",
            "draft_id": "draft_src_01JXIMAGE889_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE889_r1",
            "report_type": "election_score_sheet",
            "candidate_scores": [{"candidate_number": 1, "score": 120}],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE889",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE889/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE889/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE889/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXREJECT889",
                "replyToken": "reply-token-reject",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000889", "type": "text", "text": "\u0e44\u0e21\u0e48\u0e16\u0e39\u0e01\u0e15\u0e49\u0e2d\u0e07"},
            },
            received_at="2026-06-08T07:55:00Z",
        )

        self.assertEqual(result.state, "rejected")
        self.assertEqual(reply_sender.messages[0]["text"], "\u0e1b\u0e0f\u0e34\u0e40\u0e2a\u0e18\u0e23\u0e48\u0e32\u0e07\u0e19\u0e35\u0e49\u0e41\u0e25\u0e49\u0e27\n\u0e23\u0e30\u0e1a\u0e1a\u0e08\u0e30\u0e22\u0e31\u0e07\u0e44\u0e21\u0e48\u0e19\u0e33\u0e1c\u0e25\u0e0a\u0e38\u0e14\u0e19\u0e35\u0e49\u0e44\u0e1b\u0e43\u0e0a\u0e49\n\u0e2b\u0e32\u0e01\u0e15\u0e49\u0e2d\u0e07\u0e01\u0e32\u0e23\u0e14\u0e33\u0e40\u0e19\u0e34\u0e19\u0e01\u0e32\u0e23\u0e15\u0e48\u0e2d \u0e01\u0e23\u0e38\u0e13\u0e32\u0e2a\u0e48\u0e07\u0e23\u0e39\u0e1b\u0e43\u0e2b\u0e21\u0e48\u0e2d\u0e35\u0e01\u0e04\u0e23\u0e31\u0e49\u0e07")

    def test_correction_command_supports_multiple_candidate_score_overrides(self) -> None:
        s3_client = _RecordingS3Client()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE889",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE889_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE889/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE889_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE889/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE889_r1",
            "draft_id": "draft_src_01JXIMAGE889_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE889_r1",
            "revision": 1,
            "area_id": "77",
            "report_type": "election_score_sheet",
            "candidate_scores": [
                {"candidate_number": 1, "score": 120},
                {"candidate_number": 2, "score": 99},
            ],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE889",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE889/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE889/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE889/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXCORRECT889",
                "replyToken": "reply-token-correct",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000889", "type": "text", "text": "แก้ไข 1=121, 2=98"},
            },
            received_at="2026-06-08T07:52:00Z",
        )

        self.assertEqual(result.state, "awaiting_approval")
        corrected_draft = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE889/draft_r2.json")].decode("utf-8"))
        self.assertEqual(corrected_draft["candidate_scores"][0]["score"], 121)
        self.assertEqual(corrected_draft["candidate_scores"][1]["score"], 98)
        self.assertEqual(corrected_draft["result_signature"], "77:1=121|2=98")

    def test_correction_command_that_cannot_be_parsed_enters_exception_without_creating_new_draft(self) -> None:
        s3_client = _RecordingS3Client()
        reply_sender = _RecordingReplySender()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            line_reply_sender=reply_sender,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE890",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE890_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE890/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE890_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE890/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE890_r1",
            "draft_id": "draft_src_01JXIMAGE890_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE890_r1",
            "revision": 1,
            "report_type": "election_score_sheet",
            "candidate_scores": [{"candidate_number": 1, "score": 120}],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE890",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE890/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE890/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE890/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXCORRECT890",
                "replyToken": "reply-token-correct",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000890", "type": "text", "text": "แก้ไข ช่วยดูใหม่"},
            },
            received_at="2026-06-08T07:53:00Z",
        )

        self.assertEqual(result.state, "exception")
        self.assertEqual(result.source_message_id, "src_01JXCORRECT890")
        correction_manifest = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXCORRECT890/manifest.json")].decode("utf-8"))
        original_source = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE890/manifest.json")].decode("utf-8"))
        self.assertEqual(correction_manifest["exception"]["code"], "CORRECTION_PARSE_FAILED")
        self.assertEqual(original_source["state"], "awaiting_approval")
        self.assertEqual(original_source["pending_user_action"], "awaiting_correction_input")
        self.assertNotIn(("election-system", "dev/messages/src_01JXIMAGE890/draft_r2.json"), s3_client.objects)
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertIn("แก้ไข ผู้สมัครเบอร์ 4 เป็น 14", reply_sender.messages[0]["text"])
        self.assertIsNone(reply_sender.messages[0]["messages"])

    def test_raw_override_after_edit_button_creates_corrected_draft(self) -> None:
        s3_client = _RecordingS3Client()
        push_sender = _RecordingPushSender()
        reply_sender = _RecordingReplySender()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            line_push_sender=push_sender,
            line_reply_sender=reply_sender,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE890A",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE890A_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE890A/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE890A_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE890A/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE890A_r1",
            "draft_id": "draft_src_01JXIMAGE890A_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE890A_r1",
            "revision": 1,
            "area_id": "12",
            "report_type": "election_score_sheet",
            "candidate_scores": [{"candidate_number": 4, "score": 14}],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE890A",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE890A/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE890A/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE890A/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        initial_result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXCORRECT890A",
                "replyToken": "reply-token-correct-a",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000890A", "type": "text", "text": "\u0e41\u0e01\u0e49\u0e44\u0e02"},
            },
            received_at="2026-06-08T07:53:00Z",
        )
        self.assertEqual(initial_result.state, "exception")

        override_result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXTEXT890A",
                "replyToken": "reply-token-raw-a",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000890B", "type": "text", "text": "4=16"},
            },
            received_at="2026-06-08T07:54:00Z",
        )

        self.assertEqual(override_result.state, "awaiting_approval")
        updated_source = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE890A/manifest.json")].decode("utf-8"))
        corrected_draft = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE890A/draft_r2.json")].decode("utf-8"))
        self.assertEqual(updated_source["current_draft_id"], "draft_src_01JXIMAGE890A_r2")
        self.assertIsNone(updated_source["pending_user_action"])
        self.assertEqual(corrected_draft["candidate_scores"][0]["score"], 16)
        self.assertEqual(len(push_sender.messages), 1)
        self.assertEqual(len(reply_sender.messages), 2)
        self.assertIn("เข้าสู่โหมดแก้ไขแล้ว", reply_sender.messages[0]["text"])
        self.assertIn("รับการแก้ไขแล้ว", reply_sender.messages[1]["text"])
        self.assertIn("รายการที่แก้ไข:", reply_sender.messages[1]["text"])
        self.assertIn("ผู้สมัคร 4: 16", reply_sender.messages[1]["text"])

    def test_invalid_text_after_edit_button_receives_correction_guidance_not_generic_fallback(self) -> None:
        s3_client = _RecordingS3Client()
        reply_sender = _RecordingReplySender()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            line_reply_sender=reply_sender,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE890B",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE890B_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE890B/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE890B_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE890B/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE890B_r1",
            "draft_id": "draft_src_01JXIMAGE890B_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE890B_r1",
            "revision": 1,
            "report_type": "election_score_sheet",
            "candidate_scores": [{"candidate_number": 4, "score": 14}],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE890B",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE890B/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE890B/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE890B/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXCORRECT890B",
                "replyToken": "reply-token-correct-b",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000890C", "type": "text", "text": "\u0e41\u0e01\u0e49\u0e44\u0e02"},
            },
            received_at="2026-06-08T07:53:00Z",
        )
        reply_sender.messages.clear()

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXTEXT890B",
                "replyToken": "reply-token-raw-b",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000890D", "type": "text", "text": "abc"},
            },
            received_at="2026-06-08T07:54:00Z",
        )

        self.assertEqual(result.state, "exception")
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertIn("4=14", reply_sender.messages[0]["text"])
        self.assertNotIn("??????", reply_sender.messages[0]["text"])
        self.assertIsNone(reply_sender.messages[0]["messages"])

    def test_ambiguous_text_during_awaiting_approval_receives_correction_guidance(self) -> None:
        s3_client = _RecordingS3Client()
        reply_sender = _RecordingReplySender()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            line_reply_sender=reply_sender,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE892",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE892_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE892/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE892_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE892/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE892_r1",
            "draft_id": "draft_src_01JXIMAGE892_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE892_r1",
            "revision": 1,
            "report_type": "election_score_sheet",
            "candidate_scores": [{"candidate_number": 4, "score": 14}],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE892",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE892/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE892/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE892/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXTEXT892",
                "replyToken": "reply-token-text",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000893", "type": "text", "text": "เหมือนเบอร์ 4 จะหายไปนะ"},
            },
            received_at="2026-06-08T07:56:00Z",
        )

        self.assertEqual(result.state, "received")
        original_source = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE892/manifest.json")].decode("utf-8"))
        self.assertEqual(original_source["state"], "awaiting_approval")
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertIn("แก้ไข ผู้สมัครเบอร์ 4 เป็น 14", reply_sender.messages[0]["text"])
        self.assertIsNone(reply_sender.messages[0]["messages"])

    def test_generic_text_during_awaiting_approval_receives_fallback_reply(self) -> None:
        s3_client = _RecordingS3Client()
        reply_sender = _RecordingReplySender()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            line_reply_sender=reply_sender,
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
                "message": {"id": "550000894", "type": "text", "text": "โอเค"},
            },
            received_at="2026-06-08T07:57:00Z",
        )

        self.assertEqual(result.state, "received")
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertIn("ยืนยัน", reply_sender.messages[0]["text"])
        self.assertIn("แก้ไข 4=14", reply_sender.messages[0]["text"])
        self.assertIsNone(reply_sender.messages[0]["messages"])

    def test_approval_like_typo_during_awaiting_approval_receives_approval_guidance(self) -> None:
        s3_client = _RecordingS3Client()
        reply_sender = _RecordingReplySender()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            line_reply_sender=reply_sender,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE894",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE894_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE894/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE894_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE894/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE894_r1",
            "draft_id": "draft_src_01JXIMAGE894_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE894_r1",
            "revision": 1,
            "report_type": "election_score_sheet",
            "candidate_scores": [{"candidate_number": 1, "score": 120}],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE894",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE894/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE894/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE894/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXTEXT894",
                "replyToken": "reply-token-text-3",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000895", "type": "text", "text": "ยันยืน"},
            },
            received_at="2026-06-08T07:58:00Z",
        )

        self.assertEqual(result.state, "received")
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertIn('"ยืนยัน"', reply_sender.messages[0]["text"])
        self.assertIn("ถูกต้องอีกครั้ง", reply_sender.messages[0]["text"])
        self.assertIsNone(reply_sender.messages[0]["messages"])

    def test_correction_after_approval_receives_closed_round_reply(self) -> None:
        s3_client = _RecordingS3Client()
        reply_sender = _RecordingReplySender()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            line_reply_sender=reply_sender,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE895",
            "workflow_session_id": "line_group_C123",
            "state": "approved",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE895_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE895/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE895_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE895/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE895_r1",
            "draft_id": "draft_src_01JXIMAGE895_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "approved",
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE895",
            "source_type": "approval_command",
            "updated_at": "2026-06-08T07:59:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE895/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE895/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXTEXT895",
                "replyToken": "reply-token-text-4",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000896", "type": "text", "text": "แก้ไข"},
            },
            received_at="2026-06-08T08:00:00Z",
        )

        self.assertEqual(result.state, "exception")
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertEqual(reply_sender.messages[0]["text"], "ร่างนี้ถูกรับรองแล้วและปิดรอบตรวจแล้ว\nหากต้องการแก้เพิ่มเติม กรุณาส่งรูปใหม่หรือเปิดรอบแก้ไขใหม่")
        self.assertIsNone(reply_sender.messages[0]["messages"])

    def test_general_text_after_approval_receives_free_chat_reply(self) -> None:
        s3_client = _RecordingS3Client()
        reply_sender = _RecordingReplySender()
        chat_client = _RecordingChatClient(response_text="คุยต่อได้ครับ แม้งานก่อนหน้าจะจบแล้ว")
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            line_reply_sender=reply_sender,
            chat_completion_client=chat_client,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE895A",
            "workflow_session_id": "line_group_C123",
            "state": "approved",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE895A_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE895A/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE895A_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE895A/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE895A_r1",
            "draft_id": "draft_src_01JXIMAGE895A_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "approved",
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE895A",
            "source_type": "approval_command",
            "updated_at": "2026-06-08T07:59:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE895A/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE895A/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXTEXT895A",
                "replyToken": "reply-token-text-5",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000897", "type": "text", "text": "วันนี้เป็นไงบ้าง"},
            },
            received_at="2026-06-08T08:01:00Z",
        )

        self.assertEqual(result.state, "received")
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertEqual(reply_sender.messages[0]["text"], "คุยต่อได้ครับ แม้งานก่อนหน้าจะจบแล้ว")

    def test_approval_after_correction_creates_update_job_from_corrected_draft(self) -> None:
        s3_client = _RecordingS3Client()
        queue_client = _RecordingQueueClient()
        reply_sender = _RecordingReplySender()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        update_job_queue = SqsUpdateJobQueue(
            queue_url="https://sqs.ap-southeast-1.amazonaws.com/123/update-jobs.fifo",
            region_name="ap-southeast-1",
            client=queue_client,
        )
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            update_job_queue=update_job_queue,
            line_reply_sender=reply_sender,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE891",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE891_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE891/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE891_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE891/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE891_r1",
            "draft_id": "draft_src_01JXIMAGE891_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE891_r1",
            "revision": 1,
            "result_signature": "12:1=120|2=99",
            "election_id": "election-2026",
            "area_id": "12",
            "polling_unit_id": "07",
            "report_type": "election_score_sheet",
            "candidate_scores": [
                {"candidate_number": 1, "score": 120},
                {"candidate_number": 2, "score": 99},
            ],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE891",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE891/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE891/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE891/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        correction_result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXCORRECT891",
                "replyToken": "reply-token-correct",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000891", "type": "text", "text": "แก้ไข 1=121, 2=98"},
            },
            received_at="2026-06-08T07:54:00Z",
        )
        self.assertEqual(correction_result.state, "awaiting_approval")

        approve_result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXAPPROVE891",
                "replyToken": "reply-token-approve",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000892", "type": "text", "text": "\u0e22\u0e37\u0e19\u0e22\u0e31\u0e19"},
            },
            received_at="2026-06-08T07:55:00Z",
        )

        self.assertEqual(approve_result.state, "approved")
        updated_source = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE891/manifest.json")].decode("utf-8"))
        update_manifest = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE891/update_job.json")].decode("utf-8"))
        self.assertEqual(updated_source["state"], "approved")
        self.assertEqual(update_manifest["draft_id"], "draft_src_01JXIMAGE891_r2")
        self.assertEqual(update_manifest["payload"]["candidate_scores"][0]["score"], 121)
        self.assertEqual(update_manifest["payload"]["candidate_scores"][1]["score"], 98)
        self.assertEqual(len(queue_client.messages), 1)
        self.assertEqual(len(reply_sender.messages), 2)
        self.assertIn("รับการแก้ไขแล้ว", reply_sender.messages[0]["text"])
        self.assertIn("ผลร่างล่าสุด: ครั้งที่ 2", reply_sender.messages[1]["text"])
        self.assertIn("รายการที่แก้ไข:", reply_sender.messages[1]["text"])
        self.assertIn("ผู้สมัคร 1: 121", reply_sender.messages[1]["text"])
        self.assertIn("ผู้สมัคร 2: 98", reply_sender.messages[1]["text"])

    def test_cancel_after_entering_correction_mode_clears_pending_action(self) -> None:
        s3_client = _RecordingS3Client()
        reply_sender = _RecordingReplySender()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            line_reply_sender=reply_sender,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE892A",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE892A_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE892A/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE892A_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE892A/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE892A_r1",
            "draft_id": "draft_src_01JXIMAGE892A_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE892A_r1",
            "revision": 1,
            "report_type": "election_score_sheet",
            "candidate_scores": [{"candidate_number": 4, "score": 14}],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE892A",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE892A/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE892A/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE892A/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        enter_result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXCORRECT892A",
                "replyToken": "reply-token-enter",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000892A", "type": "text", "text": "แก้ไข"},
            },
            received_at="2026-06-08T07:54:00Z",
        )
        self.assertEqual(enter_result.state, "exception")

        cancel_result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXTEXT892A",
                "replyToken": "reply-token-cancel",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000892B", "type": "text", "text": "ยกเลิก"},
            },
            received_at="2026-06-08T07:55:00Z",
        )

        self.assertEqual(cancel_result.state, "received")
        updated_source = json.loads(s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE892A/manifest.json")].decode("utf-8"))
        self.assertIsNone(updated_source.get("pending_user_action"))
        self.assertEqual(len(reply_sender.messages), 2)
        self.assertIn("เข้าสู่โหมดแก้ไขแล้ว", reply_sender.messages[0]["text"])
        self.assertIn("ยกเลิกโหมดแก้ไขแล้ว", reply_sender.messages[1]["text"])

    def test_plain_text_does_not_steal_session_anchor_from_latest_image(self) -> None:
        s3_client = _RecordingS3Client()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
        )
        s3_client.put_object(
            Bucket="election-system",
            Key="dev/sessions/line_user_U123/latest.json",
            Body=json.dumps(
                {
                    "workflow_session_id": "line_user_U123",
                    "latest_source_message_id": "src_01JXIMAGE999",
                    "source_type": "image",
                    "updated_at": "2026-06-08T07:29:00Z",
                }
            ).encode("utf-8"),
        )

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXTEXT999",
                "replyToken": "reply-token-text",
                "source": {"type": "user", "userId": "U123"},
                "message": {"id": "550000999", "type": "text", "text": "1"},
            },
            received_at="2026-06-08T07:55:00Z",
        )

        self.assertEqual(result.state, "received")
        session_pointer = json.loads(s3_client.objects[("election-system", "dev/sessions/line_user_U123/latest.json")].decode("utf-8"))
        self.assertEqual(session_pointer["latest_source_message_id"], "src_01JXIMAGE999")

    def test_general_text_without_active_approval_receives_free_chat_reply(self) -> None:
        s3_client = _RecordingS3Client()
        reply_sender = _RecordingReplySender()
        chat_client = _RecordingChatClient(response_text="คุยได้ครับ วันนี้อยากให้ช่วยอะไร")
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            line_reply_sender=reply_sender,
            chat_completion_client=chat_client,
        )

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXTEXTCHAT001",
                "replyToken": "reply-token-chat-1",
                "source": {"type": "user", "userId": "U123"},
                "message": {"id": "550001001", "type": "text", "text": "สวัสดี"},
            },
            received_at="2026-06-08T08:10:00Z",
        )

        self.assertEqual(result.state, "received")
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertEqual(reply_sender.messages[0]["text"], "คุยได้ครับ วันนี้อยากให้ช่วยอะไร")
        self.assertEqual(len(chat_client.calls), 1)
        self.assertEqual(chat_client.calls[0][0]["role"], "system")
        self.assertEqual(chat_client.calls[0][1]["role"], "user")

    def test_general_text_without_active_approval_falls_back_when_chat_fails(self) -> None:
        s3_client = _RecordingS3Client()
        reply_sender = _RecordingReplySender()
        chat_client = _RecordingChatClient(should_fail=True)
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            line_reply_sender=reply_sender,
            chat_completion_client=chat_client,
        )

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXTEXTCHAT002",
                "replyToken": "reply-token-chat-2",
                "source": {"type": "user", "userId": "U123"},
                "message": {"id": "550001002", "type": "text", "text": "ทำอะไรได้บ้าง"},
            },
            received_at="2026-06-08T08:11:00Z",
        )

        self.assertEqual(result.state, "received")
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertIn("รับรูป", reply_sender.messages[0]["text"])
        self.assertIn('แก้ไข 4=14', reply_sender.messages[0]["text"])

    def test_persist_image_event_writes_state_to_s3_backend(self) -> None:
        s3_client = _RecordingS3Client()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
        )
        event = {
            "type": "message",
            "webhookEventId": "01JXIMAGE001",
            "replyToken": "reply-token-1",
            "source": {"type": "group", "groupId": "C123", "userId": "U123"},
            "message": {"id": "548899112233", "type": "image"},
        }

        result = store.persist_line_event(event, received_at="2026-06-08T07:30:00Z")

        self.assertEqual(result.state, "stored")
        manifest = json.loads(
            s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE001/manifest.json")].decode("utf-8")
        )
        self.assertEqual(manifest["media"]["bucket"], "election-system")
        self.assertEqual(manifest["media"]["storage_backend"], "s3")
        self.assertIn(("election-system", "dev/events/01JXIMAGE001.json"), s3_client.objects)
        self.assertIn(("election-system", "dev/events/548899112233.json"), s3_client.objects)
        self.assertIn(("election-system", "dev/sessions/line_group_C123/latest.json"), s3_client.objects)

        duplicate_result = store.persist_line_event(event, received_at="2026-06-08T07:31:00Z")
        self.assertTrue(duplicate_result.deduplicated)
        self.assertEqual(duplicate_result.source_message_id, "src_01JXIMAGE001")

    def test_persist_image_event_creates_ocr_job_and_queue_message_when_queue_is_configured(self) -> None:
        s3_client = _RecordingS3Client()
        queue_client = _RecordingQueueClient()
        reply_sender = _RecordingReplySender()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        ocr_job_queue = SqsOcrJobQueue(
            queue_url="https://sqs.ap-southeast-1.amazonaws.com/123/ocr-jobs",
            region_name="ap-southeast-1",
            client=queue_client,
        )
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            ocr_job_queue=ocr_job_queue,
            line_reply_sender=reply_sender,
        )
        event = {
            "type": "message",
            "webhookEventId": "01JXIMAGE002",
            "replyToken": "reply-token-2",
            "source": {"type": "group", "groupId": "C123", "userId": "U123"},
            "message": {"id": "548899112244", "type": "image"},
        }

        result = store.persist_line_event(event, received_at="2026-06-08T07:35:00Z")

        self.assertEqual(result.state, "queued")
        source_manifest = json.loads(
            s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE002/manifest.json")].decode("utf-8")
        )
        self.assertEqual(source_manifest["state"], "queued")
        self.assertEqual(source_manifest["current_ocr_job_id"], "ocr_src_01JXIMAGE002")

        ocr_job_manifest = json.loads(
            s3_client.objects[("election-system", "dev/messages/src_01JXIMAGE002/ocr_job.json")].decode("utf-8")
        )
        self.assertEqual(ocr_job_manifest["state"], "queued")
        self.assertEqual(ocr_job_manifest["input"]["bucket"], "election-system")
        self.assertEqual(ocr_job_manifest["input"]["key"], "messages/src_01JXIMAGE001/original.bin")

        self.assertEqual(len(queue_client.messages), 1)
        queue_payload = json.loads(queue_client.messages[0]["MessageBody"])
        self.assertEqual(queue_payload["ocr_job_id"], "ocr_src_01JXIMAGE002")
        self.assertEqual(queue_payload["source_message_id"], "src_01JXIMAGE002")
        self.assertEqual(queue_payload["workflow_session_id"], "line_group_C123")
        self.assertEqual(queue_payload["manifest_bucket"], "election-system")
        self.assertEqual(queue_payload["manifest_key"], "dev/messages/src_01JXIMAGE002/ocr_job.json")
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertEqual(reply_sender.messages[0]["reply_token"], "reply-token-2")
        self.assertEqual(
            reply_sender.messages[0]["text"],
            "รับรูปเรียบร้อยแล้ว\nกำลังตรวจข้อมูลจากภาพให้ครับ\nเดี๋ยวส่งผลให้ตรวจทานอีกครั้งเมื่อพร้อม",
        )

        duplicate_result = store.persist_line_event(event, received_at="2026-06-08T07:36:00Z")
        self.assertTrue(duplicate_result.deduplicated)
        self.assertEqual(len(queue_client.messages), 1)
        self.assertEqual(len(reply_sender.messages), 1)

    def test_fifo_queue_includes_group_and_deduplication_ids(self) -> None:
        s3_client = _RecordingS3Client()
        queue_client = _RecordingQueueClient()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        ocr_job_queue = SqsOcrJobQueue(
            queue_url="https://sqs.ap-southeast-1.amazonaws.com/123/ocr-jobs.fifo",
            region_name="ap-southeast-1",
            client=queue_client,
        )
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            ocr_job_queue=ocr_job_queue,
        )
        event = {
            "type": "message",
            "webhookEventId": "01JXIMAGE003",
            "replyToken": "reply-token-3",
            "source": {"type": "group", "groupId": "C123", "userId": "U123"},
            "message": {"id": "548899112255", "type": "image"},
        }

        result = store.persist_line_event(event, received_at="2026-06-08T07:40:00Z")

        self.assertEqual(result.state, "queued")
        self.assertEqual(len(queue_client.messages), 1)
        queue_message = queue_client.messages[0]
        self.assertEqual(queue_message["MessageGroupId"], "line_group_C123")
        self.assertEqual(queue_message["MessageDeduplicationId"], "ocr_src_01JXIMAGE003")


    def test_correction_prompt_uses_message_action(self) -> None:
        s3_client = _RecordingS3Client()
        push_sender = _RecordingPushSender()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=s3_client)
        store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            line_push_sender=push_sender,
        )
        source_manifest = {
            "source_message_id": "src_01JXIMAGE888A",
            "workflow_session_id": "line_group_C123",
            "state": "awaiting_approval",
            "sender_user_id": "U123",
            "current_draft_id": "draft_src_01JXIMAGE888A_r1",
            "current_draft_key": "dev/messages/src_01JXIMAGE888A/draft_r1.json",
            "current_approval_id": "approval_src_01JXIMAGE888A_r1",
            "current_approval_key": "dev/messages/src_01JXIMAGE888A/approval_r1.json",
        }
        approval_manifest = {
            "approval_id": "approval_src_01JXIMAGE888A_r1",
            "draft_id": "draft_src_01JXIMAGE888A_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U123",
            "state": "awaiting_approval",
        }
        draft_manifest = {
            "draft_id": "draft_src_01JXIMAGE888A_r1",
            "report_type": "election_score_sheet",
            "candidate_scores": [{"candidate_number": 1, "score": 120}],
        }
        session_pointer = {
            "workflow_session_id": "line_group_C123",
            "latest_source_message_id": "src_01JXIMAGE888A",
            "source_type": "image",
            "updated_at": "2026-06-08T07:29:00Z",
        }
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE888A/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE888A/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/messages/src_01JXIMAGE888A/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/sessions/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

        result = store.persist_line_event(
            {
                "type": "message",
                "webhookEventId": "01JXCORRECT888A",
                "replyToken": "reply-token-correct",
                "source": {"type": "group", "groupId": "C123", "userId": "U123"},
                "message": {"id": "550000888A", "type": "text", "text": "\u0e41\u0e01\u0e49\u0e44\u0e02 1=121"},
            },
            received_at="2026-06-08T07:50:00Z",
        )

        self.assertEqual(result.state, "awaiting_approval")
        quick_reply_actions = [
            item["action"] for item in push_sender.messages[0]["messages"][0]["quickReply"]["items"]
        ]
        self.assertTrue(quick_reply_actions)
        self.assertTrue(all(action["type"] == "message" for action in quick_reply_actions))


class ReviewQueueTests(unittest.TestCase):
    """Tests for the Sequential Review Queue feature (multi-image support)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.reply_sender = _RecordingReplySender()
        self.push_sender = _RecordingPushSender()
        self.s3_client = _RecordingS3Client()
        self.queue_client = _RecordingQueueClient()
        state_backend = S3JsonStateBackend(bucket_name="election-system", key_prefix="dev", client=self.s3_client)
        update_job_queue = SqsUpdateJobQueue(
            queue_url="https://sqs.ap-southeast-1.amazonaws.com/123/update-jobs.fifo",
            region_name="ap-southeast-1",
            client=self.queue_client,
        )
        self.store = LocalStateStore(
            self.temp_dir.name,
            state_backend=state_backend,
            upload_service=_FakeUploadService(storage_backend="s3"),
            line_reply_sender=self.reply_sender,
            line_push_sender=self.push_sender,
            update_job_queue=update_job_queue,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_image_event(self, event_id: str, group_id: str = "CGRP_MULTI", user_id: str = "U_MULTI") -> dict:
        return {
            "type": "message",
            "webhookEventId": event_id,
            "replyToken": f"reply-{event_id}",
            "source": {"type": "group", "groupId": group_id, "userId": user_id},
            "message": {"id": f"msg_{event_id}", "type": "image"},
        }

    def _make_approval_event(self, event_id: str, text: str = "ยืนยัน", group_id: str = "CGRP_MULTI", user_id: str = "U_MULTI") -> dict:
        return {
            "type": "message",
            "webhookEventId": event_id,
            "replyToken": f"reply-{event_id}",
            "source": {"type": "group", "groupId": group_id, "userId": user_id},
            "message": {"id": f"msg_{event_id}", "type": "text", "text": text},
        }

    def _read_session_pointer(self, session_id: str = "line_group_CGRP_MULTI") -> dict:
        key = ("election-system", f"dev/sessions/{session_id}/latest.json")
        if key not in self.s3_client.objects:
            return {}
        return json.loads(self.s3_client.objects[key].decode("utf-8"))

    def _setup_awaiting_approval(self, source_message_id: str, session_id: str = "line_group_CGRP_MULTI") -> None:
        """Simulate OCR completing for a source message by creating draft + approval artifacts."""
        draft_manifest = {
            "draft_id": f"draft_{source_message_id}_r1",
            "revision": 1,
            "report_type": "election_score_sheet",
            "area_id": "5",
            "polling_unit_id": "12",
            "candidate_scores": [{"candidate_number": 1, "score": 100}, {"candidate_number": 2, "score": 200}],
            "result_signature": "5:1=100|2=200",
        }
        approval_manifest = {
            "approval_id": f"approval_{source_message_id}_r1",
            "draft_id": f"draft_{source_message_id}_r1",
            "draft_revision": 1,
            "requested_from_user_id": "U_MULTI",
            "state": "awaiting_approval",
        }
        source_manifest_path = Path(self.temp_dir.name) / "messages" / source_message_id / "manifest.json"
        if source_manifest_path.exists():
            source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
        else:
            source_manifest = {"source_message_id": source_message_id, "workflow_session_id": session_id}
        source_manifest["state"] = "awaiting_approval"
        source_manifest["sender_user_id"] = "U_MULTI"
        source_manifest["sender_group_id"] = "CGRP_MULTI"
        source_manifest["current_draft_id"] = f"draft_{source_message_id}_r1"
        source_manifest["current_draft_key"] = f"dev/messages/{source_message_id}/draft_r1.json"
        source_manifest["current_approval_id"] = f"approval_{source_message_id}_r1"
        source_manifest["current_approval_key"] = f"dev/messages/{source_message_id}/approval_r1.json"
        source_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        source_manifest_path.write_text(json.dumps(source_manifest, ensure_ascii=False), encoding="utf-8")

        self.s3_client.put_object(Bucket="election-system", Key=f"dev/messages/{source_message_id}/manifest.json", Body=json.dumps(source_manifest).encode("utf-8"))
        self.s3_client.put_object(Bucket="election-system", Key=f"dev/messages/{source_message_id}/draft_r1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        self.s3_client.put_object(Bucket="election-system", Key=f"dev/messages/{source_message_id}/approval_r1.json", Body=json.dumps(approval_manifest).encode("utf-8"))

    def test_three_images_build_sequential_review_queue(self) -> None:
        """Sending 3 images should set the first as active_review and queue the other 2."""
        r1 = self.store.persist_line_event(self._make_image_event("IMG_Q_001"), received_at="2026-06-10T10:00:00Z")
        r2 = self.store.persist_line_event(self._make_image_event("IMG_Q_002"), received_at="2026-06-10T10:00:05Z")
        r3 = self.store.persist_line_event(self._make_image_event("IMG_Q_003"), received_at="2026-06-10T10:00:10Z")

        self.assertFalse(r1.deduplicated)
        self.assertFalse(r2.deduplicated)
        self.assertFalse(r3.deduplicated)

        sp = self._read_session_pointer()
        self.assertEqual(sp["active_review_source_message_id"], "src_IMG_Q_001")
        self.assertEqual(sp["pending_review_queue"], ["src_IMG_Q_002", "src_IMG_Q_003"])
        self.assertEqual(sp["total_received_count"], 3)
        self.assertEqual(sp["completed_review_count"], 0)
        self.assertEqual(sp["latest_source_message_id"], "src_IMG_Q_003")

    def test_approval_advances_review_queue(self) -> None:
        """After approving image 1, image 2 should become active_review and get approval prompt."""
        self.store.persist_line_event(self._make_image_event("IMG_ADV_001"), received_at="2026-06-10T10:00:00Z")
        self.store.persist_line_event(self._make_image_event("IMG_ADV_002"), received_at="2026-06-10T10:00:05Z")
        self.store.persist_line_event(self._make_image_event("IMG_ADV_003"), received_at="2026-06-10T10:00:10Z")

        # Simulate OCR completing for all 3
        self._setup_awaiting_approval("src_IMG_ADV_001")
        self._setup_awaiting_approval("src_IMG_ADV_002")
        self._setup_awaiting_approval("src_IMG_ADV_003")

        # Also update the S3 session pointer for _resolve_active_approval
        sp = self._read_session_pointer()
        self.s3_client.put_object(
            Bucket="election-system",
            Key="dev/sessions/line_group_CGRP_MULTI/latest.json",
            Body=json.dumps(sp).encode("utf-8"),
        )

        # User approves image 1
        approve_result = self.store.persist_line_event(
            self._make_approval_event("APPROVE_001", "ยืนยัน"),
            received_at="2026-06-10T10:05:00Z",
        )

        self.assertEqual(approve_result.state, "approved")

        sp_after = self._read_session_pointer()
        self.assertEqual(sp_after["active_review_source_message_id"], "src_IMG_ADV_002")
        self.assertEqual(sp_after["pending_review_queue"], ["src_IMG_ADV_003"])
        self.assertEqual(sp_after["completed_review_count"], 1)

        # Check that approval prompt for image 2 was sent via push
        prompt_messages = [m for m in self.push_sender.messages if m.get("messages")]
        self.assertTrue(len(prompt_messages) > 0, "Should have sent approval prompt for image 2")

    def test_rejection_advances_review_queue(self) -> None:
        """After rejecting image 1, image 2 should become active_review."""
        self.store.persist_line_event(self._make_image_event("IMG_REJ_001"), received_at="2026-06-10T10:00:00Z")
        self.store.persist_line_event(self._make_image_event("IMG_REJ_002"), received_at="2026-06-10T10:00:05Z")

        self._setup_awaiting_approval("src_IMG_REJ_001")
        self._setup_awaiting_approval("src_IMG_REJ_002")

        sp = self._read_session_pointer()
        self.s3_client.put_object(
            Bucket="election-system",
            Key="dev/sessions/line_group_CGRP_MULTI/latest.json",
            Body=json.dumps(sp).encode("utf-8"),
        )

        reject_result = self.store.persist_line_event(
            self._make_approval_event("REJECT_001", "ไม่ถูกต้อง"),
            received_at="2026-06-10T10:05:00Z",
        )

        self.assertEqual(reject_result.state, "rejected")

        sp_after = self._read_session_pointer()
        self.assertEqual(sp_after["active_review_source_message_id"], "src_IMG_REJ_002")
        self.assertEqual(sp_after["pending_review_queue"], [])
        self.assertEqual(sp_after["completed_review_count"], 1)

    def test_image_during_review_appends_to_queue(self) -> None:
        """Sending a new image while review is active should append it to the queue."""
        self.store.persist_line_event(self._make_image_event("IMG_MID_001"), received_at="2026-06-10T10:00:00Z")

        sp1 = self._read_session_pointer()
        self.assertEqual(sp1["active_review_source_message_id"], "src_IMG_MID_001")
        self.assertEqual(sp1["pending_review_queue"], [])

        # Simulate that image 1 OCR completed and is being reviewed
        self._setup_awaiting_approval("src_IMG_MID_001")

        # User sends image 2 during review
        self.store.persist_line_event(self._make_image_event("IMG_MID_002"), received_at="2026-06-10T10:01:00Z")

        sp2 = self._read_session_pointer()
        self.assertEqual(sp2["active_review_source_message_id"], "src_IMG_MID_001")
        self.assertEqual(sp2["pending_review_queue"], ["src_IMG_MID_002"])
        self.assertEqual(sp2["total_received_count"], 2)

        # User sends image 3 during review
        self.store.persist_line_event(self._make_image_event("IMG_MID_003"), received_at="2026-06-10T10:02:00Z")

        sp3 = self._read_session_pointer()
        self.assertEqual(sp3["active_review_source_message_id"], "src_IMG_MID_001")
        self.assertEqual(sp3["pending_review_queue"], ["src_IMG_MID_002", "src_IMG_MID_003"])
        self.assertEqual(sp3["total_received_count"], 3)

    def test_queue_empty_after_all_approved(self) -> None:
        """After approving all images, the queue should be empty."""
        self.store.persist_line_event(self._make_image_event("IMG_ALL_001"), received_at="2026-06-10T10:00:00Z")
        self.store.persist_line_event(self._make_image_event("IMG_ALL_002"), received_at="2026-06-10T10:00:05Z")

        self._setup_awaiting_approval("src_IMG_ALL_001")
        self._setup_awaiting_approval("src_IMG_ALL_002")

        sp = self._read_session_pointer()
        self.s3_client.put_object(
            Bucket="election-system",
            Key="dev/sessions/line_group_CGRP_MULTI/latest.json",
            Body=json.dumps(sp).encode("utf-8"),
        )

        # Approve image 1
        self.store.persist_line_event(
            self._make_approval_event("APPROVE_ALL_001", "ยืนยัน"),
            received_at="2026-06-10T10:05:00Z",
        )

        sp_mid = self._read_session_pointer()
        self.assertEqual(sp_mid["active_review_source_message_id"], "src_IMG_ALL_002")
        self.assertEqual(sp_mid["completed_review_count"], 1)

        # Update S3 session pointer for second approval
        self.s3_client.put_object(
            Bucket="election-system",
            Key="dev/sessions/line_group_CGRP_MULTI/latest.json",
            Body=json.dumps(sp_mid).encode("utf-8"),
        )

        # Approve image 2
        self.store.persist_line_event(
            self._make_approval_event("APPROVE_ALL_002", "ยืนยัน"),
            received_at="2026-06-10T10:10:00Z",
        )

        sp_final = self._read_session_pointer()
        self.assertIsNone(sp_final["active_review_source_message_id"])
        self.assertEqual(sp_final["pending_review_queue"], [])
        self.assertEqual(sp_final["completed_review_count"], 2)
        self.assertEqual(sp_final["total_received_count"], 2)

    def test_parse_area_id_override(self) -> None:
        self.assertEqual(parse_area_id_override("แก้ไข เขต 15"), "15")
        self.assertEqual(parse_area_id_override("เขต 44"), "44")
        self.assertEqual(parse_area_id_override("เขต=101"), "101")
        self.assertEqual(parse_area_id_override("แก้ไขเขต: 12"), "12")
        self.assertIsNone(parse_area_id_override("แก้ไข เบอร์ 4=14"))

    def test_correction_command_updates_area_id_and_submissions(self) -> None:
        self.store.persist_line_event(self._make_image_event("IMG_CORR_AREA"), received_at="2026-06-10T10:00:00Z")
        self._setup_awaiting_approval("src_IMG_CORR_AREA")

        source_manifest_path = "dev/messages/src_IMG_CORR_AREA/manifest.json"
        src_manifest = json.loads(self.s3_client.objects[("election-system", source_manifest_path)].decode("utf-8"))
        src_manifest["area_id"] = "12"
        self.s3_client.put_object(
            Bucket="election-system",
            Key=source_manifest_path,
            Body=json.dumps(src_manifest).encode("utf-8")
        )

        draft_path = src_manifest["current_draft_key"]
        draft_manifest = json.loads(self.s3_client.objects[("election-system", draft_path)].decode("utf-8"))
        draft_manifest["area_id"] = "12"
        draft_manifest["election_id"] = "election-2026"
        self.s3_client.put_object(
            Bucket="election-system",
            Key=draft_path,
            Body=json.dumps(draft_manifest).encode("utf-8")
        )

        area12_subs_path = "dev/indexes/by-area/election_2026/12/submissions.json"
        self.store._update_area_submissions(
            election_id="election-2026",
            area_id="12",
            source_message_id="src_IMG_CORR_AREA",
            timestamp="2026-06-10T10:00:00Z",
        )

        data12 = json.loads(self.s3_client.objects[("election-system", area12_subs_path)].decode("utf-8"))
        self.assertEqual(data12["submission_count"], 1)

        self.store.persist_line_event(
            self._make_approval_event("CORRECT_AREA", "แก้ไข เขต 13"),
            received_at="2026-06-10T10:15:00Z",
        )

        updated_src_manifest = json.loads(self.s3_client.objects[("election-system", source_manifest_path)].decode("utf-8"))
        self.assertEqual(updated_src_manifest["area_id"], "13")

        data12 = json.loads(self.s3_client.objects[("election-system", area12_subs_path)].decode("utf-8"))
        self.assertEqual(data12["submission_count"], 0)

        area13_subs_path = "dev/indexes/by-area/election_2026/13/submissions.json"
        data13 = json.loads(self.s3_client.objects[("election-system", area13_subs_path)].decode("utf-8"))
        self.assertEqual(data13["submission_count"], 1)
        self.assertEqual(data13["submissions"][0]["source_message_id"], "src_IMG_CORR_AREA")


if __name__ == "__main__":
    unittest.main()

