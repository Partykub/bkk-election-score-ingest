import io
import json
import unittest
from unittest import mock

from hermes.ocr_worker.__main__ import (
    DownloadedJob,
    WorkerConfig,
    acknowledge_job,
    build_approval_documents,
    build_draft_documents,
    build_approval_prompt_text,
    build_ocr_prompt,
    build_runtime_config_log,
    build_result_signature,
    ensure_approval_artifacts,
    extract_first_json_object,
    fetch_job_from_queue_message,
    manifest_key_for_job,
    maybe_send_approval_prompt,
    normalize_candidate_scores,
    parse_queue_envelope,
    poll_queue_once,
    process_downloaded_job,
    send_line_push_message,
    should_acknowledge_result,
    advance_session_pointer_on_failure,
    update_area_submissions,
    with_s3_prefix,
)


class _FakeS3Client:
    def __init__(self, objects):
        self.objects = objects
        self.put_requests = []

    def get_object(self, *, Bucket: str, Key: str):
        if (Bucket, Key) not in self.objects:
            missing = RuntimeError("missing key")
            missing.response = {"Error": {"Code": "NoSuchKey"}}
            raise missing
        payload = self.objects[(Bucket, Key)]
        if isinstance(payload, tuple):
            body, content_type = payload
            return {"Body": io.BytesIO(body), "ContentType": content_type}
        return {"Body": io.BytesIO(payload)}

    def put_object(self, **kwargs):
        self.put_requests.append(kwargs)
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]
        return {"ETag": '"etag-test"'}


class _FakeSqsClient:
    def __init__(self, messages):
        self.messages = list(messages)
        self.delete_requests = []
        self.receive_requests = []

    def receive_message(self, **kwargs):
        self.receive_requests.append(kwargs)
        return {"Messages": list(self.messages)}

    def delete_message(self, **kwargs):
        self.delete_requests.append(kwargs)


class _FakeUrlOpenResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _RecordingUrlOpen:
    def __init__(self) -> None:
        self.requests = []

    def __call__(self, req, timeout=30):
        self.requests.append({"url": req.full_url, "headers": dict(req.header_items()), "body": req.data, "timeout": timeout})
        return _FakeUrlOpenResponse()


class QueueEnvelopeTests(unittest.TestCase):
    def test_with_s3_prefix_prepends_only_when_needed(self):
        self.assertEqual(with_s3_prefix("messages/a/ocr_job.json", prefix="api-data/score"), "api-data/score/messages/a/ocr_job.json")
        self.assertEqual(with_s3_prefix("api-data/score/messages/a/ocr_job.json", prefix="api-data/score"), "api-data/score/messages/a/ocr_job.json")

    def test_parse_queue_envelope_accepts_raw_job_id(self):
        envelope = parse_queue_envelope("ocr_20260609_0001", default_bucket="bucket-a")

        self.assertEqual(envelope.ocr_job_id, "ocr_20260609_0001")
        self.assertEqual(envelope.manifest_bucket, "bucket-a")
        self.assertEqual(envelope.manifest_key, manifest_key_for_job("ocr_20260609_0001"))

    def test_parse_queue_envelope_accepts_json_payload(self):
        envelope = parse_queue_envelope(
            json.dumps(
                {
                    "ocr_job_id": "ocr_20260609_0002",
                    "manifest_bucket": "bucket-b",
                    "manifest_key": "custom/jobs/ocr_20260609_0002.json",
                }
            ),
            default_bucket="bucket-a",
        )

        self.assertEqual(envelope.ocr_job_id, "ocr_20260609_0002")
        self.assertEqual(envelope.manifest_bucket, "bucket-b")
        self.assertEqual(envelope.manifest_key, "custom/jobs/ocr_20260609_0002.json")

    def test_parse_queue_envelope_accepts_relaxed_object_payload(self):
        envelope = parse_queue_envelope(
            "{ocr_job_id:ocr_20260609_0005,manifest_bucket:bucket-c,manifest_key:custom/jobs/ocr_20260609_0005.json}",
            default_bucket="bucket-a",
        )

        self.assertEqual(envelope.ocr_job_id, "ocr_20260609_0005")
        self.assertEqual(envelope.manifest_bucket, "bucket-c")
        self.assertEqual(envelope.manifest_key, "custom/jobs/ocr_20260609_0005.json")


class WorkerProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = WorkerConfig(
            hermes_base_url="http://hermes-supervisor:8642",
            hermes_api_key="change-this-api-key",
            hermes_model="hermes-agent",
            line_channel_access_token="line-channel-token",
            line_api_base_url="https://api.line.me",
            queue_url="https://sqs.ap-southeast-1.amazonaws.com/123/ocr-jobs",
            aws_region="ap-southeast-1",
            s3_bucket="bucket-a",
            s3_prefix="api-data/score",
            model_name="gemma4:26b",
            poll_seconds=15,
            queue_max_messages=1,
            processing_concurrency=1,
            queue_wait_seconds=10,
            queue_visibility_timeout=60,
        )

    def test_fetch_job_from_queue_message_reads_manifest_and_binary(self):
        manifest_key = manifest_key_for_job("ocr_20260609_0003")
        s3_client = _FakeS3Client(
            {
                ("bucket-a", "api-data/score/" + manifest_key): json.dumps(
                    {
                        "ocr_job_id": "ocr_20260609_0003",
                        "source_message_id": "src_20260609_0003",
                        "input": {
                            "bucket": "bucket-a",
                            "key": "messages/src_20260609_0003/original.bin",
                            "content_type": "image/jpeg",
                        },
                    }
                ).encode("utf-8"),
                ("bucket-a", "api-data/score/messages/src_20260609_0003/original.bin"): (b"image-bytes", "image/jpeg"),
            }
        )
        message = {
            "MessageId": "msg-1",
            "ReceiptHandle": "receipt-1",
            "Body": json.dumps({"ocr_job_id": "ocr_20260609_0003"}),
        }

        downloaded_job = fetch_job_from_queue_message(message, s3_client=s3_client, config=self.config)

        self.assertEqual(downloaded_job.ocr_job_id, "ocr_20260609_0003")
        self.assertEqual(downloaded_job.source_message_id, "src_20260609_0003")
        self.assertEqual(downloaded_job.workflow_session_id, "")
        self.assertEqual(downloaded_job.manifest_key, "api-data/score/messages/src_20260609_0003/ocr_job.json")
        self.assertEqual(downloaded_job.input_key, "api-data/score/messages/src_20260609_0003/original.bin")
        self.assertEqual(downloaded_job.input_size_bytes, len(b"image-bytes"))
        self.assertEqual(downloaded_job.input_content_type, "image/jpeg")
        self.assertEqual(downloaded_job.queue_message_id, "msg-1")
        self.assertEqual(downloaded_job.receipt_handle, "receipt-1")

    def test_poll_queue_once_returns_downloaded_jobs_without_deleting_messages(self):
        manifest_key = manifest_key_for_job("ocr_20260609_0004")
        s3_client = _FakeS3Client(
            {
                ("bucket-a", "api-data/score/" + manifest_key): json.dumps(
                    {
                        "ocr_job_id": "ocr_20260609_0004",
                        "source_message_id": "src_20260609_0004",
                        "input": {
                            "bucket": "bucket-a",
                            "key": "messages/src_20260609_0004/original.bin",
                        },
                    }
                ).encode("utf-8"),
                ("bucket-a", "api-data/score/messages/src_20260609_0004/original.bin"): (b"binary-data", "application/octet-stream"),
            }
        )
        queue_client = _FakeSqsClient(
            [
                {
                    "MessageId": "msg-2",
                    "ReceiptHandle": "receipt-2",
                    "Body": json.dumps({"ocr_job_id": "ocr_20260609_0004"}),
                }
            ]
        )

        downloaded_jobs = poll_queue_once(queue_client=queue_client, s3_client=s3_client, config=self.config)

        self.assertEqual(len(downloaded_jobs), 1)
        self.assertEqual(queue_client.receive_requests[0]["QueueUrl"], self.config.queue_url)
        self.assertEqual(queue_client.delete_requests, [])

    def test_acknowledge_job_deletes_message_after_processing(self):
        queue_client = _FakeSqsClient([])
        downloaded_job = fetch_job_from_queue_message(
            {
                "MessageId": "msg-2",
                "ReceiptHandle": "receipt-2",
                "Body": json.dumps({
                    "ocr_job_id": "ocr_20260609_0004",
                    "manifest_bucket": "bucket-a",
                    "manifest_key": "messages/src_20260609_0004/ocr_job.json",
                }),
            },
            s3_client=_FakeS3Client(
                {
                    ("bucket-a", "api-data/score/messages/src_20260609_0004/ocr_job.json"): json.dumps(
                        {
                            "ocr_job_id": "ocr_20260609_0004",
                            "source_message_id": "src_20260609_0004",
                            "input": {"bucket": "bucket-a", "key": "messages/src_20260609_0004/original.bin"},
                        }
                    ).encode("utf-8"),
                    ("bucket-a", "api-data/score/messages/src_20260609_0004/original.bin"): (b"binary-data", "application/octet-stream"),
                }
            ),
            config=self.config,
        )

        acknowledge_job(queue_client=queue_client, config=self.config, downloaded_job=downloaded_job)

        self.assertEqual(queue_client.delete_requests[0]["QueueUrl"], self.config.queue_url)
        self.assertEqual(queue_client.delete_requests[0]["ReceiptHandle"], "receipt-2")

    def test_extract_first_json_object_handles_code_fences(self):
        payload = extract_first_json_object("```json\n{\"ok\": true, \"count\": 2}\n```")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 2)

    def test_build_result_signature_orders_candidates(self):
        signature = build_result_signature(
            [
                {"candidate_number": 2, "score": 90},
                {"candidate_number": 1, "score": 120},
            ],
            "area12",
        )

        self.assertEqual(signature, "area12:1=120|2=90")

    def test_normalize_candidate_scores_accepts_candidate_number_alias(self):
        normalized_scores = normalize_candidate_scores(
            [
                {"candiate_number": 4, "score": "14", "confidence": "0.98", "raw_text": "4 | 14"},
            ]
        )

        self.assertEqual(normalized_scores[0]["candidate_number"], 4)
        self.assertEqual(normalized_scores[0]["score"], 14)

    def test_build_draft_documents_records_normalization_warnings_for_unreadable_candidate_number(self):
        downloaded_job = DownloadedJob(
            ocr_job_id="ocr_20260610_0009",
            source_message_id="src_20260610_0009",
            workflow_session_id="line_user_U123",
            manifest_bucket="bucket-a",
            manifest_key="api-data/score/messages/src_20260610_0009/ocr_job.json",
            input_bucket="bucket-a",
            input_key="api-data/score/messages/src_20260610_0009/original.bin",
            input_size_bytes=8,
            input_content_type="image/jpeg",
            queue_message_id="msg-9",
            receipt_handle="receipt-9",
        )

        draft_manifest, _, _ = build_draft_documents(
            downloaded_job=downloaded_job,
            ocr_job_manifest={"ocr_options": {"prompt_version": "ocr-v1", "model_name": "gemma4:26b"}},
            normalized_payload={
                "document_type": "election_score_sheet",
                "candidate_scores": [{"score": 14, "raw_text": "4 | 14"}],
                "validation_flags": [],
                "image_quality_flags": [],
                "overall_confidence": 0.98,
            },
            raw_model_text="{}",
            model_name="gemma4:26b",
            revision=1,
            timestamp="2026-06-10T06:00:00Z",
        )

        self.assertIn("candidate_number_unreadable", draft_manifest["validation_flags"])
        self.assertIn("candidate_number_unreadable", draft_manifest["normalization_warnings"])

    def test_build_draft_documents_records_ballot_summary_fields(self):
        downloaded_job = DownloadedJob(
            ocr_job_id="ocr_20260610_0010",
            source_message_id="src_20260610_0010",
            workflow_session_id="line_user_U123",
            manifest_bucket="bucket-a",
            manifest_key="api-data/score/messages/src_20260610_0010/ocr_job.json",
            input_bucket="bucket-a",
            input_key="api-data/score/messages/src_20260610_0010/original.bin",
            input_size_bytes=8,
            input_content_type="image/jpeg",
            queue_message_id="msg-10",
            receipt_handle="receipt-10",
        )

        draft_manifest, _, _ = build_draft_documents(
            downloaded_job=downloaded_job,
            ocr_job_manifest={"ocr_options": {"prompt_version": "ocr-v1", "model_name": "gemma4:26b"}},
            normalized_payload={
                "document_type": "election_score_sheet",
                "eligibleVoters": "100",
                "voterTurnout": 80,
                "validBallots": "70",
                "invalidBallots": 5,
                "abstainedBallots": "5",
                "validation_flags": [],
                "image_quality_flags": [],
                "overall_confidence": 0.95,
                "candidate_scores": [],
            },
            raw_model_text="{}",
            model_name="gemma4:26b",
            revision=1,
            timestamp="2026-06-10T08:00:00Z",
        )

        self.assertEqual(draft_manifest["eligible_voters"], 100)
        self.assertEqual(draft_manifest["voter_turnout"], 80)
        self.assertEqual(draft_manifest["valid_ballots"], 70)
        self.assertEqual(draft_manifest["invalid_ballots"], 5)
        self.assertEqual(draft_manifest["vote_no"], 5)

    def test_build_ocr_prompt_allows_handwritten_score_lists(self):
        downloaded_job = DownloadedJob(
            ocr_job_id="ocr_20260610_0001",
            source_message_id="src_20260610_0001",
            workflow_session_id="line_user_U123",
            manifest_bucket="bucket-a",
            manifest_key="api-data/score/messages/src_20260610_0001/ocr_job.json",
            input_bucket="bucket-a",
            input_key="api-data/score/messages/src_20260610_0001/original.bin",
            input_size_bytes=123,
            input_content_type="image/jpeg",
            queue_message_id="msg-1",
            receipt_handle="receipt-1",
        )

        prompt = build_ocr_prompt(downloaded_job=downloaded_job, model_name=self.config.model_name, prompt_version="ocr-v1")

        self.assertIn("sticky note", prompt)
        self.assertIn("ห้ามเดา area_id", prompt)
        self.assertIn("ให้หาตัวเลขคะแนนของผู้สมัครแต่ละคน", prompt)
        self.assertIn('"candidate_scores"', prompt)

    def test_build_approval_prompt_text_summarizes_draft(self):
        prompt = build_approval_prompt_text(
            {
                "draft_id": "draft_src_20260609_0004_r1",
                "revision": 1,
                "report_type": "election_score_sheet",
                "area_id": "401",
                "polling_unit_id": "12",
                "overall_confidence": 0.95,
                "validation_flags": ["requires_human_review"],
                "eligible_voters": 100,
                "voter_turnout": 80,
                "valid_ballots": 70,
                "invalid_ballots": 5,
                "vote_no": 5,
                "candidate_scores": [
                    {"candidate_number": 1, "score": 120},
                    {"candidate_number": 2, "score": 90},
                ],
            }
        )

        self.assertIn("ตรวจรูปเสร็จแล้ว: ร่างครั้งที่ 1", prompt)
        self.assertIn("ผู้สมัคร 1: 120", prompt)
        self.assertNotIn("ความมั่นใจ:", prompt)
        self.assertNotIn("ธงตรวจสอบ:", prompt)
        self.assertIn("ตอบ 'ยืนยัน'", prompt)
        self.assertIn("ตอบ 'ไม่ถูกต้อง'", prompt)

    def test_build_draft_documents_infers_ballot_summary_report_type(self):
        downloaded_job = DownloadedJob(
            ocr_job_id="ocr_src_123",
            source_message_id="src_123",
            workflow_session_id="line_group_C123",
            manifest_bucket="bucket",
            manifest_key="messages/src_123/ocr_job.json",
            input_bucket="bucket",
            input_key="image.jpg",
            input_size_bytes=1,
            input_content_type="image/jpeg",
            queue_message_id=None,
            receipt_handle=None,
        )
        draft_manifest, _, _ = build_draft_documents(
            downloaded_job=downloaded_job,
            ocr_job_manifest={"ocr_options": {"prompt_version": "ocr-v1"}},
            normalized_payload={
                "document_type": "election_score_sheet",
                "area_id": "13",
                "eligible_voters": 100,
                "voter_turnout": 80,
                "valid_ballots": 70,
                "invalid_ballots": 5,
                "vote_no": 5,
                "candidate_scores": [],
                "overall_confidence": 0.9,
                "validation_flags": [],
                "image_quality_flags": [],
            },
            raw_model_text="{}",
            model_name="test-model",
            revision=1,
            timestamp="2026-06-22T00:00:00Z",
        )

        self.assertEqual(draft_manifest["report_type"], "ballot_summary")
        self.assertNotIn("missing_candidate_scores", draft_manifest["validation_flags"])

    def test_build_approval_prompt_text_includes_ballot_summary(self):
        prompt = build_approval_prompt_text(
            {
                "revision": 1,
                "report_type": "election_score_sheet",
                "eligible_voters": 100,
                "voter_turnout": 80,
                "valid_ballots": 70,
                "invalid_ballots": 5,
                "vote_no": 5,
                "candidate_scores": [],
            }
        )

        self.assertIn("\u0e1c\u0e39\u0e49\u0e21\u0e35\u0e2a\u0e34\u0e17\u0e18\u0e34: 100", prompt)
        self.assertIn("\u0e1c\u0e39\u0e49\u0e21\u0e32\u0e43\u0e0a\u0e49\u0e2a\u0e34\u0e17\u0e18\u0e34: 80", prompt)
        self.assertIn("\u0e1a\u0e31\u0e15\u0e23\u0e14\u0e35: 70", prompt)
        self.assertIn("\u0e1a\u0e31\u0e15\u0e23\u0e40\u0e2a\u0e35\u0e22: 5", prompt)
        self.assertIn("Vote No: 5", prompt)

    def test_build_approval_prompt_text_warns_when_candidate_scores_missing(self):
        prompt = build_approval_prompt_text(
            {
                "revision": 1,
                "report_type": "election_score_sheet",
                "area_id": "13",
                "candidate_scores": [],
            }
        )

        self.assertIn("ยังยืนยันร่างนี้ไม่ได้จนกว่าจะมีคะแนนผู้สมัคร", prompt)
        self.assertIn("แก้ไข 4=14", prompt)
        self.assertNotIn("ตอบ 'ยืนยัน' เพื่อรับรองร่างนี้", prompt)

    def test_build_approval_prompt_text_warns_when_area_missing(self):
        prompt = build_approval_prompt_text(
            {
                "revision": 1,
                "report_type": "election_score_sheet",
                "candidate_scores": [{"candidate_number": 1, "score": 40}],
            }
        )

        self.assertIn("เขต: ยังไม่พบ", prompt)
        self.assertIn("ยังยืนยันร่างนี้ไม่ได้จนกว่าจะระบุเขต", prompt)
        self.assertIn("กรุณาระบุเขตให้ถูกต้องก่อนบันทึก", prompt)
        self.assertIn("แก้ไข เขต 13", prompt)
        self.assertNotIn("ตอบ 'ยืนยัน' เพื่อรับรองร่างนี้", prompt)

    def helper_build_approval_prompt_text_allows_ballot_summary_without_candidate_scores(self):
        prompt = build_approval_prompt_text(
            {
                "revision": 1,
                "report_type": "ballot_summary",
                "area_id": "13",
                "eligible_voters": 100,
                "voter_turnout": 80,
                "valid_ballots": 70,
                "invalid_ballots": 5,
                "vote_no": 5,
                "candidate_scores": [],
            }
        )

        self.assertIn("เธ•เธญเธ 'เธขเธทเธเธขเธฑเธ'", prompt)
        self.assertNotIn("\u0e04\u0e30\u0e41\u0e19\u0e19\u0e1c\u0e39\u0e49\u0e2a\u0e21\u0e31\u0e04\u0e23", prompt)

    def test_build_approval_prompt_text_allows_ballot_summary_without_candidate_scores(self):
        prompt = build_approval_prompt_text(
            {
                "revision": 1,
                "report_type": "ballot_summary",
                "area_id": "13",
                "eligible_voters": 100,
                "voter_turnout": 80,
                "valid_ballots": 70,
                "invalid_ballots": 5,
                "vote_no": 5,
                "candidate_scores": [],
            }
        )

        self.assertIn("\u0e15\u0e2d\u0e1a '\u0e22\u0e37\u0e19\u0e22\u0e31\u0e19'", prompt)
        self.assertNotIn("\u0e04\u0e30\u0e41\u0e19\u0e19\u0e1c\u0e39\u0e49\u0e2a\u0e21\u0e31\u0e04\u0e23", prompt)

    def test_send_line_push_message_posts_expected_payload(self):
        opener = _RecordingUrlOpen()

        send_line_push_message(
            channel_access_token="line-channel-token",
            destination_id="U123",
            text="พร้อมตรวจร่าง",
            opener=opener,
        )

        self.assertEqual(opener.requests[0]["url"], "https://api.line.me/v2/bot/message/push")
        self.assertEqual(opener.requests[0]["headers"]["Authorization"], "Bearer line-channel-token")
        payload = json.loads(opener.requests[0]["body"].decode("utf-8"))
        self.assertEqual(payload["to"], "U123")
        self.assertEqual(payload["messages"][0]["text"], "พร้อมตรวจร่าง")

    def test_send_line_push_message_includes_multiple_messages_when_requested(self):
        opener = _RecordingUrlOpen()

        send_line_push_message(
            channel_access_token="line-channel-token",
            destination_id="U123",
            messages=[
                {
                    "type": "text",
                    "text": "พร้อมตรวจร่าง",
                    "quickReply": {
                        "items": [
                            {"type": "action", "imageUrl": None, "action": {"type": "message", "label": "ยืนยัน", "text": "ยืนยัน"}}
                        ]
                    },
                },
            ],
            opener=opener,
        )

        payload = json.loads(opener.requests[0]["body"].decode("utf-8"))
        self.assertEqual(payload["messages"][0]["quickReply"]["items"][0]["action"]["text"], "ยืนยัน")

    def test_maybe_send_approval_prompt_updates_source_manifest_after_push(self):
        source_manifest_path = "api-data/score/messages/src_20260609_0004/manifest.json"
        source_manifest = {
            "source_message_id": "src_20260609_0004",
            "sender_user_id": "U123",
            "sender_group_id": None,
            "sender_room_id": None,
        }
        s3_client = _FakeS3Client({("bucket-a", source_manifest_path): json.dumps(source_manifest).encode("utf-8")})
        opener = _RecordingUrlOpen()
        draft_manifest = {
            "draft_id": "draft_src_20260609_0004_r1",
            "revision": 1,
            "report_type": "election_score_sheet",
            "area_id": "401",
            "overall_confidence": 0.95,
            "validation_flags": [],
            "candidate_scores": [{"candidate_number": 1, "score": 120}],
        }

        result = maybe_send_approval_prompt(
            s3_client=s3_client,
            bucket="bucket-a",
            source_manifest_path=source_manifest_path,
            source_manifest=source_manifest,
            draft_manifest=draft_manifest,
            config=self.config,
            timestamp="2026-06-09T10:00:00Z",
            opener=opener,
        )

        self.assertEqual(result["status"], "sent")
        updated_manifest = json.loads(s3_client.objects[("bucket-a", source_manifest_path)].decode("utf-8"))
        self.assertEqual(updated_manifest["approval_prompt"]["status"], "sent")
        self.assertEqual(updated_manifest["approval_prompt"]["draft_id"], "draft_src_20260609_0004_r1")
        payload = json.loads(opener.requests[0]["body"].decode("utf-8"))
        self.assertEqual(payload["messages"][0]["quickReply"]["items"][1]["action"]["text"], "แก้ไข")

    def test_maybe_send_approval_prompt_hides_approve_button_when_area_missing(self):
        source_manifest_path = "api-data/score/messages/src_20260609_0004/manifest.json"
        source_manifest = {
            "source_message_id": "src_20260609_0004",
            "sender_user_id": "U123",
            "sender_group_id": None,
            "sender_room_id": None,
        }
        s3_client = _FakeS3Client({("bucket-a", source_manifest_path): json.dumps(source_manifest).encode("utf-8")})
        opener = _RecordingUrlOpen()
        draft_manifest = {
            "draft_id": "draft_src_20260609_0004_r1",
            "revision": 1,
            "report_type": "election_score_sheet",
            "area_id": None,
            "overall_confidence": 0.95,
            "validation_flags": ["missing_area_id", "requires_human_review"],
            "candidate_scores": [{"candidate_number": 1, "score": 120}],
        }

        result = maybe_send_approval_prompt(
            s3_client=s3_client,
            bucket="bucket-a",
            source_manifest_path=source_manifest_path,
            source_manifest=source_manifest,
            draft_manifest=draft_manifest,
            config=self.config,
            timestamp="2026-06-09T10:00:00Z",
            opener=opener,
        )

        self.assertEqual(result["status"], "sent")
        payload = json.loads(opener.requests[0]["body"].decode("utf-8"))
        actions = [item["action"]["text"] for item in payload["messages"][0]["quickReply"]["items"]]
        self.assertNotIn("ยืนยัน", actions)
        self.assertEqual(actions, ["แก้ไข", "ไม่ถูกต้อง"])

    def test_build_runtime_config_log_redacts_secrets(self):
        payload = build_runtime_config_log(self.config)

        self.assertEqual(payload["hermes_api_key"], "***redacted***")
        self.assertEqual(payload["line_channel_access_token"], "***redacted***")

    def test_build_approval_documents_targets_single_draft_revision(self):
        downloaded_job = fetch_job_from_queue_message(
            {
                "MessageId": "msg-3",
                "ReceiptHandle": "receipt-3",
                "Body": json.dumps({"ocr_job_id": "ocr_20260609_0005"}),
            },
            s3_client=_FakeS3Client(
                {
                    ("bucket-a", "api-data/score/messages/src_20260609_0005/ocr_job.json"): json.dumps(
                        {
                            "ocr_job_id": "ocr_20260609_0005",
                            "source_message_id": "src_20260609_0005",
                            "workflow_session_id": "line_user_U123",
                            "input": {"bucket": "bucket-a", "key": "messages/src_20260609_0005/original.bin"},
                        }
                    ).encode("utf-8"),
                    ("bucket-a", "api-data/score/messages/src_20260609_0005/original.bin"): (b"binary-data", "application/octet-stream"),
                }
            ),
            config=self.config,
        )

        approval_manifest, approval_pointer, approval_id, approval_key = build_approval_documents(
            downloaded_job=downloaded_job,
            draft_manifest={"draft_id": "draft_src_20260609_0005_r2", "revision": 2},
            source_manifest={"sender_user_id": "U123"},
            timestamp="2026-06-09T10:00:00Z",
        )

        self.assertEqual(approval_id, "approval_src_20260609_0005_r2")
        self.assertEqual(approval_key, "messages/src_20260609_0005/approval_r2.json")
        self.assertEqual(approval_manifest["draft_revision"], 2)
        self.assertEqual(approval_pointer["approval_id"], approval_id)

    def test_ensure_approval_artifacts_writes_new_artifacts_when_missing(self):
        s3_client = _FakeS3Client({})
        downloaded_job = fetch_job_from_queue_message(
            {
                "MessageId": "msg-4",
                "ReceiptHandle": "receipt-4",
                "Body": json.dumps({"ocr_job_id": "ocr_20260609_0006"}),
            },
            s3_client=_FakeS3Client(
                {
                    ("bucket-a", "api-data/score/messages/src_20260609_0006/ocr_job.json"): json.dumps(
                        {
                            "ocr_job_id": "ocr_20260609_0006",
                            "source_message_id": "src_20260609_0006",
                            "workflow_session_id": "line_user_U456",
                            "input": {"bucket": "bucket-a", "key": "messages/src_20260609_0006/original.bin"},
                        }
                    ).encode("utf-8"),
                    ("bucket-a", "api-data/score/messages/src_20260609_0006/original.bin"): (b"binary-data", "application/octet-stream"),
                }
            ),
            config=self.config,
        )

        approval_manifest, approval_id, approval_key = ensure_approval_artifacts(
            s3_client=s3_client,
            manifest_bucket="bucket-a",
            source_manifest={"sender_user_id": "U456"},
            downloaded_job=downloaded_job,
            draft_manifest={"draft_id": "draft_src_20260609_0006_r1", "revision": 1},
            prefix="api-data/score",
            timestamp="2026-06-09T10:05:00Z",
        )

        self.assertEqual(approval_id, "approval_src_20260609_0006_r1")
        self.assertEqual(approval_manifest["requested_from_user_id"], "U456")
        self.assertEqual(approval_key, "api-data/score/messages/src_20260609_0006/approval_r1.json")
        self.assertIn(("bucket-a", "api-data/score/messages/src_20260609_0006/approval_latest.json"), s3_client.objects)

    def test_process_downloaded_job_creates_approval_artifacts_on_first_completion(self):
        config = WorkerConfig(
            hermes_base_url="http://hermes-supervisor:8642",
            hermes_api_key="change-this-api-key",
            hermes_model="hermes-agent",
            line_channel_access_token=None,
            line_api_base_url="https://api.line.me",
            queue_url="https://sqs.ap-southeast-1.amazonaws.com/123/ocr-jobs",
            aws_region="ap-southeast-1",
            s3_bucket="bucket-a",
            s3_prefix="api-data/score",
            model_name="gemma4:26b",
            poll_seconds=15,
            queue_max_messages=1,
            processing_concurrency=1,
            queue_wait_seconds=10,
            queue_visibility_timeout=300,
        )
        s3_client = _FakeS3Client(
            {
                (
                    "bucket-a",
                    "api-data/score/messages/src_20260609_0007/ocr_job.json",
                ): json.dumps(
                    {
                        "ocr_job_id": "ocr_20260609_0007",
                        "source_message_id": "src_20260609_0007",
                        "workflow_session_id": "line_user_U789",
                        "state": "queued",
                        "input": {
                            "bucket": "bucket-a",
                            "key": "messages/src_20260609_0007/original.bin",
                        },
                        "ocr_options": {"prompt_version": "ocr-v1"},
                    }
                ).encode("utf-8"),
                (
                    "bucket-a",
                    "api-data/score/messages/src_20260609_0007/original.bin",
                ): (b"binary-data", "image/jpeg"),
                (
                    "bucket-a",
                    "api-data/score/messages/src_20260609_0007/manifest.json",
                ): json.dumps(
                    {
                        "source_message_id": "src_20260609_0007",
                        "workflow_session_id": "line_user_U789",
                        "sender_user_id": "U789",
                        "state": "queued",
                    }
                ).encode("utf-8"),
            }
        )
        downloaded_job = fetch_job_from_queue_message(
            {
                "MessageId": "msg-7",
                "ReceiptHandle": "receipt-7",
                "Body": json.dumps({"ocr_job_id": "ocr_20260609_0007"}),
            },
            s3_client=s3_client,
            config=config,
        )
        hermes_response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "document_type": "election_score_sheet",
                                "overall_confidence": 0.95,
                                "validation_flags": [],
                                "image_quality_flags": [],
                                "candidate_scores": [
                                    {"candidate_number": 1, "score": 120, "confidence": 0.99, "raw_text": "1 | 120"}
                                ],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

        with mock.patch("hermes.ocr_worker.__main__.call_hermes_ocr", return_value=hermes_response):
            result = process_downloaded_job(downloaded_job=downloaded_job, s3_client=s3_client, config=config)

        self.assertEqual(result["status"], "completed")
        updated_source_manifest = json.loads(
            s3_client.objects[("bucket-a", "api-data/score/messages/src_20260609_0007/manifest.json")].decode("utf-8")
        )
        self.assertEqual(updated_source_manifest["state"], "awaiting_approval")
        self.assertEqual(updated_source_manifest["current_approval_id"], "approval_src_20260609_0007_r1")
        self.assertIn(("bucket-a", "api-data/score/messages/src_20260609_0007/approval_latest.json"), s3_client.objects)

    def test_process_downloaded_job_keeps_queue_message_for_retryable_ocr_failure(self):
        s3_client = _FakeS3Client(
            {
                (
                    "bucket-a",
                    "api-data/score/messages/src_20260611_0001/ocr_job.json",
                ): json.dumps(
                    {
                        "ocr_job_id": "ocr_20260611_0001",
                        "source_message_id": "src_20260611_0001",
                        "workflow_session_id": "line_user_U111",
                        "state": "queued",
                        "max_attempts": 5,
                        "attempt_count": 0,
                        "input": {
                            "bucket": "bucket-a",
                            "key": "messages/src_20260611_0001/original.bin",
                        },
                        "ocr_options": {"prompt_version": "ocr-v1"},
                    }
                ).encode("utf-8"),
                (
                    "bucket-a",
                    "api-data/score/messages/src_20260611_0001/original.bin",
                ): (b"binary-data", "image/jpeg"),
                (
                    "bucket-a",
                    "api-data/score/messages/src_20260611_0001/manifest.json",
                ): json.dumps(
                    {
                        "source_message_id": "src_20260611_0001",
                        "workflow_session_id": "line_user_U111",
                        "sender_user_id": "U111",
                        "state": "queued",
                    }
                ).encode("utf-8"),
            }
        )
        downloaded_job = fetch_job_from_queue_message(
            {
                "MessageId": "msg-11",
                "ReceiptHandle": "receipt-11",
                "Body": json.dumps({"ocr_job_id": "ocr_20260611_0001"}),
            },
            s3_client=s3_client,
            config=self.config,
        )

        with mock.patch(
            "hermes.ocr_worker.__main__.call_hermes_ocr",
            side_effect=RuntimeError("Remote end closed connection without response"),
        ):
            result = process_downloaded_job(downloaded_job=downloaded_job, s3_client=s3_client, config=self.config)

        self.assertEqual(result["status"], "retry_pending")
        self.assertFalse(should_acknowledge_result(result))
        updated_job_manifest = json.loads(
            s3_client.objects[("bucket-a", "api-data/score/messages/src_20260611_0001/ocr_job.json")].decode("utf-8")
        )
        updated_source_manifest = json.loads(
            s3_client.objects[("bucket-a", "api-data/score/messages/src_20260611_0001/manifest.json")].decode("utf-8")
        )
        self.assertEqual(updated_job_manifest["state"], "queued")
        self.assertTrue(updated_job_manifest["error"]["retryable"])
        self.assertEqual(updated_source_manifest["state"], "queued")
        self.assertEqual(updated_source_manifest["ocr_retry"]["status"], "pending")

    def test_process_downloaded_job_sends_failure_notice_after_retry_exhausted(self):
        s3_client = _FakeS3Client(
            {
                (
                    "bucket-a",
                    "api-data/score/messages/src_20260611_0002/ocr_job.json",
                ): json.dumps(
                    {
                        "ocr_job_id": "ocr_20260611_0002",
                        "source_message_id": "src_20260611_0002",
                        "workflow_session_id": "line_user_U222",
                        "state": "queued",
                        "max_attempts": 1,
                        "attempt_count": 0,
                        "input": {
                            "bucket": "bucket-a",
                            "key": "messages/src_20260611_0002/original.bin",
                        },
                        "ocr_options": {"prompt_version": "ocr-v1"},
                    }
                ).encode("utf-8"),
                (
                    "bucket-a",
                    "api-data/score/messages/src_20260611_0002/original.bin",
                ): (b"binary-data", "image/jpeg"),
                (
                    "bucket-a",
                    "api-data/score/messages/src_20260611_0002/manifest.json",
                ): json.dumps(
                    {
                        "source_message_id": "src_20260611_0002",
                        "workflow_session_id": "line_user_U222",
                        "sender_user_id": "U222",
                        "state": "queued",
                    }
                ).encode("utf-8"),
            }
        )
        downloaded_job = fetch_job_from_queue_message(
            {
                "MessageId": "msg-12",
                "ReceiptHandle": "receipt-12",
                "Body": json.dumps({"ocr_job_id": "ocr_20260611_0002"}),
            },
            s3_client=s3_client,
            config=self.config,
        )
        with mock.patch(
            "hermes.ocr_worker.__main__.call_hermes_ocr",
            side_effect=RuntimeError("Remote end closed connection without response"),
        ), mock.patch("hermes.ocr_worker.__main__.send_line_push_message") as send_line_push:
            result = process_downloaded_job(downloaded_job=downloaded_job, s3_client=s3_client, config=self.config)

        self.assertEqual(result["status"], "failed")
        self.assertTrue(should_acknowledge_result(result))
        updated_job_manifest = json.loads(
            s3_client.objects[("bucket-a", "api-data/score/messages/src_20260611_0002/ocr_job.json")].decode("utf-8")
        )
        updated_source_manifest = json.loads(
            s3_client.objects[("bucket-a", "api-data/score/messages/src_20260611_0002/manifest.json")].decode("utf-8")
        )
        self.assertEqual(updated_job_manifest["state"], "failed")
        self.assertEqual(updated_source_manifest["state"], "exception")
        self.assertEqual(updated_source_manifest["ocr_failure_notice"]["status"], "sent")
        self.assertEqual(send_line_push.call_args.kwargs["destination_id"], "U222")


    def test_maybe_send_approval_prompt_uses_message_action(self):
        source_manifest_path = "api-data/score/messages/src_20260609_0004/manifest.json"
        source_manifest = {
            "source_message_id": "src_20260609_0004",
            "sender_user_id": "U123",
            "sender_group_id": None,
            "sender_room_id": None,
        }
        s3_client = _FakeS3Client({("bucket-a", source_manifest_path): json.dumps(source_manifest).encode("utf-8")})
        opener = _RecordingUrlOpen()
        draft_manifest = {
            "draft_id": "draft_src_20260609_0004_r1",
            "revision": 1,
            "report_type": "election_score_sheet",
            "area_id": "401",
            "overall_confidence": 0.95,
            "validation_flags": [],
            "candidate_scores": [{"candidate_number": 1, "score": 120}],
        }

        result = maybe_send_approval_prompt(
            s3_client=s3_client,
            bucket="bucket-a",
            source_manifest_path=source_manifest_path,
            source_manifest=source_manifest,
            draft_manifest=draft_manifest,
            config=self.config,
            timestamp="2026-06-09T10:00:00Z",
            opener=opener,
        )

        self.assertEqual(result["status"], "sent")
        payload = json.loads(opener.requests[0]["body"].decode("utf-8"))
        correction_action = payload["messages"][0]["quickReply"]["items"][1]["action"]
        self.assertEqual(correction_action["type"], "message")
        self.assertEqual(correction_action["text"], "แก้ไข")

    def test_update_area_submissions_records_and_updates_on_s3(self):
        s3_client = _FakeS3Client({})
        update_area_submissions(
            s3_client=s3_client,
            bucket="test-bucket",
            prefix="test-prefix",
            election_id="election-2026",
            area_id="12",
            source_message_id="src_1",
            timestamp="2026-06-09T06:30:00Z",
        )

        expected_key = "test-prefix/indexes/by-area/election_2026/12/submissions.json"
        self.assertIn(("test-bucket", expected_key), s3_client.objects)

        data = json.loads(s3_client.objects[("test-bucket", expected_key)].decode("utf-8"))
        self.assertEqual(data["election_id"], "election_2026")
        self.assertEqual(data["area_id"], "12")
        self.assertEqual(data["submission_count"], 1)
        self.assertEqual(data["submissions"][0]["source_message_id"], "src_1")

        # Test adding a second submission
        update_area_submissions(
            s3_client=s3_client,
            bucket="test-bucket",
            prefix="test-prefix",
            election_id="election-2026",
            area_id="12",
            source_message_id="src_2",
            timestamp="2026-06-09T06:45:00Z",
        )
        data = json.loads(s3_client.objects[("test-bucket", expected_key)].decode("utf-8"))
        self.assertEqual(data["submission_count"], 2)
        self.assertEqual(data["submissions"][1]["source_message_id"], "src_2")

        # Test changing area_id (from 12 to 13)
        update_area_submissions(
            s3_client=s3_client,
            bucket="test-bucket",
            prefix="test-prefix",
            election_id="election-2026",
            area_id="13",
            source_message_id="src_2",
            timestamp="2026-06-09T07:00:00Z",
            old_area_id="12",
        )

        # Area 12 should have count 1 (src_1 only)
        data12 = json.loads(s3_client.objects[("test-bucket", expected_key)].decode("utf-8"))
        self.assertEqual(data12["submission_count"], 1)
        self.assertEqual(data12["submissions"][0]["source_message_id"], "src_1")

        # Area 13 should have count 1 (src_2)
        expected_key13 = "test-prefix/indexes/by-area/election_2026/13/submissions.json"
        data13 = json.loads(s3_client.objects[("test-bucket", expected_key13)].decode("utf-8"))
        self.assertEqual(data13["submission_count"], 1)
        self.assertEqual(data13["submissions"][0]["source_message_id"], "src_2")


    def test_advance_session_pointer_on_failure_clears_active_review(self):
        session_pointer = {
            "workflow_session_id": "line_group_G001",
            "active_review_source_message_id": "src_IMG_001",
            "pending_review_queue": [],
            "total_received_count": 1,
            "completed_review_count": 0,
            "updated_at": "2026-06-09T06:00:00Z",
        }
        s3_client = _FakeS3Client({
            ("bucket-a", "dev/sessions/line_group_G001/latest.json"): json.dumps(session_pointer).encode("utf-8"),
        })

        result = advance_session_pointer_on_failure(
            s3_client=s3_client,
            bucket="bucket-a",
            prefix="dev",
            workflow_session_id="line_group_G001",
            failed_source_message_id="src_IMG_001",
            timestamp="2026-06-09T06:05:00Z",
        )

        self.assertIsNotNone(result)
        self.assertIsNone(result["active_review_source_message_id"])
        self.assertEqual(result["pending_review_queue"], [])

    def test_advance_session_pointer_on_failure_promotes_next_in_queue(self):
        session_pointer = {
            "workflow_session_id": "line_group_G002",
            "active_review_source_message_id": "src_IMG_A",
            "pending_review_queue": ["src_IMG_B", "src_IMG_C"],
            "total_received_count": 3,
            "completed_review_count": 0,
            "updated_at": "2026-06-09T06:00:00Z",
        }
        s3_client = _FakeS3Client({
            ("bucket-a", "dev/sessions/line_group_G002/latest.json"): json.dumps(session_pointer).encode("utf-8"),
        })

        result = advance_session_pointer_on_failure(
            s3_client=s3_client,
            bucket="bucket-a",
            prefix="dev",
            workflow_session_id="line_group_G002",
            failed_source_message_id="src_IMG_A",
            timestamp="2026-06-09T06:05:00Z",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["active_review_source_message_id"], "src_IMG_B")
        self.assertEqual(result["pending_review_queue"], ["src_IMG_C"])

    def test_advance_session_pointer_on_failure_removes_from_pending_queue(self):
        session_pointer = {
            "workflow_session_id": "line_group_G003",
            "active_review_source_message_id": "src_IMG_X",
            "pending_review_queue": ["src_IMG_Y", "src_IMG_Z"],
            "total_received_count": 3,
            "completed_review_count": 0,
            "updated_at": "2026-06-09T06:00:00Z",
        }
        s3_client = _FakeS3Client({
            ("bucket-a", "dev/sessions/line_group_G003/latest.json"): json.dumps(session_pointer).encode("utf-8"),
        })

        result = advance_session_pointer_on_failure(
            s3_client=s3_client,
            bucket="bucket-a",
            prefix="dev",
            workflow_session_id="line_group_G003",
            failed_source_message_id="src_IMG_Y",
            timestamp="2026-06-09T06:05:00Z",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["active_review_source_message_id"], "src_IMG_X")
        self.assertEqual(result["pending_review_queue"], ["src_IMG_Z"])


if __name__ == "__main__":
    unittest.main()
