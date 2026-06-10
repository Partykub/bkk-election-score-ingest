import io
import json
import tempfile
import unittest
from pathlib import Path

from hermes.supervisor.intake_server import LocalStateStore, S3JsonStateBackend


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

    def __call__(self, *, reply_token: str, text: str) -> None:
        self.messages.append({"reply_token": reply_token, "text": text})


class _RecordingPushSender:
    def __init__(self) -> None:
        self.messages = []

    def __call__(self, *, destination_id: str, text: str) -> None:
        self.messages.append({"destination_id": destination_id, "text": text})


from hermes.supervisor.intake_server import LocalStateStore, S3JsonStateBackend, SqsOcrJobQueue, SqsUpdateJobQueue


class _FakeUploadSession:
    def __init__(self, storage_backend: str) -> None:
        self.upload_session_id = "upl_src_01JXIMAGE001"
        self.bucket = "election-system"
        self.object_key = "inbound/src_01JXIMAGE001/original.bin"
        self.metadata_key = "inbound/src_01JXIMAGE001/metadata.json"
        self.source_message_manifest_key = "manifests/source-messages/src_01JXIMAGE001.json"
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

        manifest_path = Path(self.temp_dir.name) / "manifests" / "source-messages" / "src_01JXIMAGE001.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["workflow_session_id"], "line_group_C123")
        self.assertEqual(manifest["state"], "stored")
        self.assertEqual(manifest["source_type"], "image")
        self.assertEqual(manifest["dedupe_event_key"], "line:event:01JXIMAGE001")
        self.assertEqual(manifest["upload_session_id"], "upl_src_01JXIMAGE001")
        self.assertEqual(manifest["media"]["bucket"], "local-election-system")
        self.assertEqual(
            manifest["media"]["key"],
            "inbound/src_01JXIMAGE001/original.bin",
        )

        event_index_path = Path(self.temp_dir.name) / "indexes" / "by-line-event-id" / "01JXIMAGE001.json"
        self.assertTrue(event_index_path.exists())

        metadata_path = Path(self.temp_dir.name) / "inbound" / "src_01JXIMAGE001" / "metadata.json"
        self.assertTrue(metadata_path.exists())
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["storage_backend"], "local-mock")
        self.assertEqual(metadata["binary_status"], "pending_line_content_fetch")

        session_pointer = Path(self.temp_dir.name) / "indexes" / "by-session" / "line_group_C123" / "latest.json"
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

        manifest_path = Path(self.temp_dir.name) / "manifests" / "source-messages" / "src_01JXDUP001.json"
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
        manifest_path = Path(self.temp_dir.name) / "manifests" / "source-messages" / "src_01JXTEXT001.json"
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
            "current_draft_key": "dev/drafts/src_01JXIMAGE777/revision-1.json",
            "current_approval_id": "approval_src_01JXIMAGE777_r1",
            "current_approval_key": "dev/approvals/src_01JXIMAGE777/revision-1.json",
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
        s3_client.put_object(Bucket="election-system", Key="dev/manifests/source-messages/src_01JXIMAGE777.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/approvals/src_01JXIMAGE777/revision-1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/drafts/src_01JXIMAGE777/revision-1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/indexes/by-session/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

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
        updated_source = json.loads(s3_client.objects[("election-system", "dev/manifests/source-messages/src_01JXIMAGE777.json")].decode("utf-8"))
        updated_approval = json.loads(s3_client.objects[("election-system", "dev/approvals/src_01JXIMAGE777/revision-1.json")].decode("utf-8"))
        self.assertEqual(updated_source["state"], "approved")
        self.assertTrue(updated_source["current_update_job_id"].startswith("upd_approval_src_01JXIMAGE777_r1"))
        self.assertEqual(updated_approval["state"], "approved")
        self.assertEqual(updated_approval["approved_by_user_id"], "U123")
        self.assertIn(("election-system", "dev/updates/jobs/upd_approval_src_01JXIMAGE777_r1.json"), s3_client.objects)
        self.assertEqual(len(queue_client.messages), 1)
        queue_payload = json.loads(queue_client.messages[0]["MessageBody"])
        self.assertEqual(queue_payload["update_job_id"], "upd_approval_src_01JXIMAGE777_r1")
        self.assertEqual(queue_payload["manifest_bucket"], "election-system")
        self.assertEqual(queue_payload["manifest_key"], "dev/updates/jobs/upd_approval_src_01JXIMAGE777_r1.json")
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertIn("รับรองผลเรียบร้อยแล้ว", reply_sender.messages[0]["text"])

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
            "current_draft_key": "dev/drafts/src_01JXIMAGE888/revision-1.json",
            "current_approval_id": "approval_src_01JXIMAGE888_r1",
            "current_approval_key": "dev/approvals/src_01JXIMAGE888/revision-1.json",
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
        s3_client.put_object(Bucket="election-system", Key="dev/manifests/source-messages/src_01JXIMAGE888.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/approvals/src_01JXIMAGE888/revision-1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/drafts/src_01JXIMAGE888/revision-1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/indexes/by-session/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

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
        updated_source = json.loads(s3_client.objects[("election-system", "dev/manifests/source-messages/src_01JXIMAGE888.json")].decode("utf-8"))
        updated_approval = json.loads(s3_client.objects[("election-system", "dev/approvals/src_01JXIMAGE888/revision-1.json")].decode("utf-8"))
        corrected_draft = json.loads(s3_client.objects[("election-system", "dev/drafts/src_01JXIMAGE888/revision-2.json")].decode("utf-8"))
        latest_draft_pointer = json.loads(s3_client.objects[("election-system", "dev/drafts/src_01JXIMAGE888/latest.json")].decode("utf-8"))
        next_approval = json.loads(s3_client.objects[("election-system", "dev/approvals/src_01JXIMAGE888/revision-2.json")].decode("utf-8"))
        latest_approval_pointer = json.loads(s3_client.objects[("election-system", "dev/approvals/src_01JXIMAGE888/latest.json")].decode("utf-8"))
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
        self.assertIn("ร่างครั้งที่ 2", push_sender.messages[0]["text"])
        self.assertNotIn(("election-system", "dev/updates/jobs/upd_approval_src_01JXIMAGE888_r1.json"), s3_client.objects)

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
            "current_draft_key": "dev/drafts/src_01JXIMAGE889/revision-1.json",
            "current_approval_id": "approval_src_01JXIMAGE889_r1",
            "current_approval_key": "dev/approvals/src_01JXIMAGE889/revision-1.json",
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
        s3_client.put_object(Bucket="election-system", Key="dev/manifests/source-messages/src_01JXIMAGE889.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/approvals/src_01JXIMAGE889/revision-1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/drafts/src_01JXIMAGE889/revision-1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/indexes/by-session/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

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
        corrected_draft = json.loads(s3_client.objects[("election-system", "dev/drafts/src_01JXIMAGE889/revision-2.json")].decode("utf-8"))
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
            "current_draft_key": "dev/drafts/src_01JXIMAGE890/revision-1.json",
            "current_approval_id": "approval_src_01JXIMAGE890_r1",
            "current_approval_key": "dev/approvals/src_01JXIMAGE890/revision-1.json",
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
        s3_client.put_object(Bucket="election-system", Key="dev/manifests/source-messages/src_01JXIMAGE890.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/approvals/src_01JXIMAGE890/revision-1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/drafts/src_01JXIMAGE890/revision-1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/indexes/by-session/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

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
        correction_manifest = json.loads(s3_client.objects[("election-system", "dev/manifests/source-messages/src_01JXCORRECT890.json")].decode("utf-8"))
        original_source = json.loads(s3_client.objects[("election-system", "dev/manifests/source-messages/src_01JXIMAGE890.json")].decode("utf-8"))
        self.assertEqual(correction_manifest["exception"]["code"], "CORRECTION_PARSE_FAILED")
        self.assertEqual(original_source["state"], "awaiting_approval")
        self.assertNotIn(("election-system", "dev/drafts/src_01JXIMAGE890/revision-2.json"), s3_client.objects)
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertIn("แก้ไข ผู้สมัครเบอร์ 4 เป็น 14", reply_sender.messages[0]["text"])

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
            "current_draft_key": "dev/drafts/src_01JXIMAGE892/revision-1.json",
            "current_approval_id": "approval_src_01JXIMAGE892_r1",
            "current_approval_key": "dev/approvals/src_01JXIMAGE892/revision-1.json",
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
        s3_client.put_object(Bucket="election-system", Key="dev/manifests/source-messages/src_01JXIMAGE892.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/approvals/src_01JXIMAGE892/revision-1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/drafts/src_01JXIMAGE892/revision-1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/indexes/by-session/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

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
        original_source = json.loads(s3_client.objects[("election-system", "dev/manifests/source-messages/src_01JXIMAGE892.json")].decode("utf-8"))
        self.assertEqual(original_source["state"], "awaiting_approval")
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertIn("แก้ไข ผู้สมัครเบอร์ 4 เป็น 14", reply_sender.messages[0]["text"])

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
            "current_draft_key": "dev/drafts/src_01JXIMAGE893/revision-1.json",
            "current_approval_id": "approval_src_01JXIMAGE893_r1",
            "current_approval_key": "dev/approvals/src_01JXIMAGE893/revision-1.json",
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
        s3_client.put_object(Bucket="election-system", Key="dev/manifests/source-messages/src_01JXIMAGE893.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/approvals/src_01JXIMAGE893/revision-1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/drafts/src_01JXIMAGE893/revision-1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/indexes/by-session/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

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
            "current_draft_key": "dev/drafts/src_01JXIMAGE894/revision-1.json",
            "current_approval_id": "approval_src_01JXIMAGE894_r1",
            "current_approval_key": "dev/approvals/src_01JXIMAGE894/revision-1.json",
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
        s3_client.put_object(Bucket="election-system", Key="dev/manifests/source-messages/src_01JXIMAGE894.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/approvals/src_01JXIMAGE894/revision-1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/drafts/src_01JXIMAGE894/revision-1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/indexes/by-session/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

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
            "current_draft_key": "dev/drafts/src_01JXIMAGE891/revision-1.json",
            "current_approval_id": "approval_src_01JXIMAGE891_r1",
            "current_approval_key": "dev/approvals/src_01JXIMAGE891/revision-1.json",
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
        s3_client.put_object(Bucket="election-system", Key="dev/manifests/source-messages/src_01JXIMAGE891.json", Body=json.dumps(source_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/approvals/src_01JXIMAGE891/revision-1.json", Body=json.dumps(approval_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/drafts/src_01JXIMAGE891/revision-1.json", Body=json.dumps(draft_manifest).encode("utf-8"))
        s3_client.put_object(Bucket="election-system", Key="dev/indexes/by-session/line_group_C123/latest.json", Body=json.dumps(session_pointer).encode("utf-8"))

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
                "message": {"id": "550000892", "type": "text", "text": "เธขเธทเธเธขเธฑเธ"},
            },
            received_at="2026-06-08T07:55:00Z",
        )

        self.assertEqual(approve_result.state, "approved")
        updated_source = json.loads(s3_client.objects[("election-system", "dev/manifests/source-messages/src_01JXIMAGE891.json")].decode("utf-8"))
        update_manifest = json.loads(s3_client.objects[("election-system", "dev/updates/jobs/upd_approval_src_01JXIMAGE891_r2.json")].decode("utf-8"))
        self.assertEqual(updated_source["state"], "approved")
        self.assertEqual(update_manifest["draft_id"], "draft_src_01JXIMAGE891_r2")
        self.assertEqual(update_manifest["payload"]["candidate_scores"][0]["score"], 121)
        self.assertEqual(update_manifest["payload"]["candidate_scores"][1]["score"], 98)
        self.assertEqual(len(queue_client.messages), 1)

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
            Key="dev/indexes/by-session/line_user_U123/latest.json",
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
        session_pointer = json.loads(s3_client.objects[("election-system", "dev/indexes/by-session/line_user_U123/latest.json")].decode("utf-8"))
        self.assertEqual(session_pointer["latest_source_message_id"], "src_01JXIMAGE999")

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
            s3_client.objects[("election-system", "dev/manifests/source-messages/src_01JXIMAGE001.json")].decode("utf-8")
        )
        self.assertEqual(manifest["media"]["bucket"], "election-system")
        self.assertEqual(manifest["media"]["storage_backend"], "s3")
        self.assertIn(("election-system", "dev/indexes/by-line-event-id/01JXIMAGE001.json"), s3_client.objects)
        self.assertIn(("election-system", "dev/indexes/by-line-message-id/548899112233.json"), s3_client.objects)
        self.assertIn(("election-system", "dev/indexes/by-session/line_group_C123/latest.json"), s3_client.objects)

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
            s3_client.objects[("election-system", "dev/manifests/source-messages/src_01JXIMAGE002.json")].decode("utf-8")
        )
        self.assertEqual(source_manifest["state"], "queued")
        self.assertEqual(source_manifest["current_ocr_job_id"], "ocr_src_01JXIMAGE002")

        ocr_job_manifest = json.loads(
            s3_client.objects[("election-system", "dev/manifests/ocr-jobs/ocr_src_01JXIMAGE002.json")].decode("utf-8")
        )
        self.assertEqual(ocr_job_manifest["state"], "queued")
        self.assertEqual(ocr_job_manifest["input"]["bucket"], "election-system")
        self.assertEqual(ocr_job_manifest["input"]["key"], "inbound/src_01JXIMAGE001/original.bin")

        self.assertEqual(len(queue_client.messages), 1)
        queue_payload = json.loads(queue_client.messages[0]["MessageBody"])
        self.assertEqual(queue_payload["ocr_job_id"], "ocr_src_01JXIMAGE002")
        self.assertEqual(queue_payload["source_message_id"], "src_01JXIMAGE002")
        self.assertEqual(queue_payload["workflow_session_id"], "line_group_C123")
        self.assertEqual(queue_payload["manifest_bucket"], "election-system")
        self.assertEqual(queue_payload["manifest_key"], "dev/manifests/ocr-jobs/ocr_src_01JXIMAGE002.json")
        self.assertEqual(len(reply_sender.messages), 1)
        self.assertEqual(reply_sender.messages[0]["reply_token"], "reply-token-2")
        self.assertEqual(
            reply_sender.messages[0]["text"],
            "รับรูปเรียบร้อยแล้ว กำลังตรวจข้อมูลจากภาพให้ครับ\nเดี๋ยวส่งผลให้ตรวจทานอีกครั้งเมื่อพร้อม",
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


if __name__ == "__main__":
    unittest.main()
