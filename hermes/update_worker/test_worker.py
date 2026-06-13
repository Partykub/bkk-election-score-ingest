from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout

from hermes.update_worker.__main__ import (
    DownloadedUpdateJob,
    QueueEnvelope,
    acknowledge_job,
    build_config,
    build_s3_only_result,
    build_runtime_config_log,
    call_target_api,
    fetch_job_from_queue_message,
    main,
    manifest_key_for_job,
    parse_queue_envelope,
    poll_queue_once,
    process_downloaded_job,
    update_job_id_from_manifest_key,
    with_s3_prefix,
)


class _FakeMissingKeyError(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "NoSuchKey"}}


class _FakeS3Client:
    def __init__(self, objects):
        self.objects = objects
        self.put_requests = []

    def get_object(self, *, Bucket: str, Key: str):
        if (Bucket, Key) not in self.objects:
            raise _FakeMissingKeyError()
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, **kwargs):
        self.put_requests.append(kwargs)
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]
        return {"ETag": '"etag-test"'}


class _FakeSqsClient:
    def __init__(self, messages):
        self.messages = list(messages)
        self.receive_requests = []
        self.delete_requests = []

    def receive_message(self, **kwargs):
        self.receive_requests.append(kwargs)
        return {"Messages": list(self.messages)}

    def delete_message(self, **kwargs):
        self.delete_requests.append(kwargs)


class _FakeUrlOpenResponse:
    def __init__(self, *, status: int = 200, body: bytes = b"{}") -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _RecordingUrlOpen:
    def __init__(self, *, body: bytes | None = None) -> None:
        self.body = body or b'{"ok": true}'
        self.requests = []

    def __call__(self, req, timeout=30):
        self.requests.append({"url": req.full_url, "headers": dict(req.header_items()), "body": req.data, "timeout": timeout})
        return _FakeUrlOpenResponse(body=self.body)


class UpdateWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._original_env)

    def test_manifest_key_for_job(self) -> None:
        self.assertEqual(manifest_key_for_job("upd_123"), "messages/123/update_job.json")

    def test_with_s3_prefix_prepends_only_when_needed(self) -> None:
        self.assertEqual(with_s3_prefix("messages/123/update_job.json", prefix="api-data/score"), "api-data/score/messages/123/update_job.json")
        self.assertEqual(with_s3_prefix("api-data/score/messages/123/update_job.json", prefix="api-data/score"), "api-data/score/messages/123/update_job.json")

    def test_update_job_id_from_manifest_key(self) -> None:
        self.assertEqual(update_job_id_from_manifest_key("messages/123/update_job.json"), "upd_123")

    def test_parse_queue_envelope_from_string_uses_default_bucket(self) -> None:
        envelope = parse_queue_envelope(json.dumps("upd_123"), default_bucket="bucket-a")
        self.assertEqual(
            envelope,
            QueueEnvelope(
                update_job_id="upd_123",
                manifest_bucket="bucket-a",
                manifest_key="messages/123/update_job.json",
            ),
        )

    def test_parse_queue_envelope_from_object(self) -> None:
        envelope = parse_queue_envelope(
            json.dumps({
                "update_job_id": "upd_123",
                "manifest_bucket": "bucket-a",
                "manifest_key": "custom/jobs/upd_123.json",
            }),
            default_bucket=None,
        )
        self.assertEqual(
            envelope,
            QueueEnvelope(
                update_job_id="upd_123",
                manifest_bucket="bucket-a",
                manifest_key="custom/jobs/upd_123.json",
            ),
        )

    def test_parse_queue_envelope_requires_bucket(self) -> None:
        with self.assertRaisesRegex(ValueError, "manifest_bucket"):
            parse_queue_envelope(json.dumps("upd_123"), default_bucket=None)

    def test_build_config_reads_environment(self) -> None:
        os.environ["UPDATE_WORKER_QUEUE_URL"] = "https://example.com/update-jobs.fifo"
        os.environ["UPDATE_WORKER_AWS_REGION"] = "ap-southeast-1"
        os.environ["UPDATE_WORKER_S3_BUCKET"] = "bucket-a"
        os.environ["UPDATE_WORKER_S3_PREFIX"] = "api-data/score"
        os.environ["UPDATE_WORKER_TARGET_API_BASE_URL"] = "https://api.example.com"
        config = build_config()

        self.assertEqual(config.queue_url, "https://example.com/update-jobs.fifo")
        self.assertEqual(config.aws_region, "ap-southeast-1")
        self.assertEqual(config.s3_bucket, "bucket-a")
        self.assertEqual(config.s3_prefix, "api-data/score")
        self.assertEqual(config.target_api_base_url, "https://api.example.com")

    def test_runtime_config_log_is_json_safe(self) -> None:
        os.environ["UPDATE_WORKER_QUEUE_URL"] = "https://example.com/update-jobs"
        payload = build_runtime_config_log(build_config())
        self.assertEqual(payload["queue_url"], "https://example.com/update-jobs")

    def test_fetch_job_from_queue_message_reads_update_manifest(self) -> None:
        os.environ["UPDATE_WORKER_S3_PREFIX"] = "api-data/score"
        config = build_config()
        s3_client = _FakeS3Client(
            {
                (
                    "bucket-a",
                    "api-data/score/messages/123/update_job.json",
                ): json.dumps(
                    {
                        "update_job_id": "upd_123",
                        "source_message_id": "src_123",
                        "workflow_session_id": "line_group_C123",
                    }
                ).encode("utf-8")
            }
        )
        job = fetch_job_from_queue_message(
            {
                "MessageId": "msg-1",
                "ReceiptHandle": "receipt-1",
                "Body": json.dumps({"update_job_id": "upd_123", "manifest_bucket": "bucket-a", "manifest_key": "messages/123/update_job.json"}),
            },
            s3_client=s3_client,
            config=config,
        )

        self.assertEqual(job.update_job_id, "upd_123")
        self.assertEqual(job.source_message_id, "src_123")
        self.assertEqual(job.manifest_key, "api-data/score/messages/123/update_job.json")

    def test_poll_queue_once_returns_downloaded_jobs(self) -> None:
        os.environ["UPDATE_WORKER_QUEUE_URL"] = "https://example.com/update-jobs"
        os.environ["UPDATE_WORKER_S3_BUCKET"] = "bucket-a"
        os.environ["UPDATE_WORKER_S3_PREFIX"] = "api-data/score"
        config = build_config()
        queue_client = _FakeSqsClient(
            [
                {
                    "MessageId": "msg-1",
                    "ReceiptHandle": "receipt-1",
                    "Body": json.dumps({"update_job_id": "upd_123", "manifest_bucket": "bucket-a", "manifest_key": "messages/123/update_job.json"}),
                }
            ]
        )
        s3_client = _FakeS3Client(
            {
                ("bucket-a", "api-data/score/messages/123/update_job.json"): json.dumps(
                    {"update_job_id": "upd_123", "source_message_id": "src_123", "workflow_session_id": "line_user_U123"}
                ).encode("utf-8")
            }
        )

        jobs = poll_queue_once(queue_client=queue_client, s3_client=s3_client, config=config)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].update_job_id, "upd_123")

    def test_call_target_api_posts_expected_payload(self) -> None:
        os.environ["UPDATE_WORKER_TARGET_API_BASE_URL"] = "https://api.example.com"
        config = build_config()
        opener = _RecordingUrlOpen(body=b'{"updated": true}')

        result = call_target_api(
            config=config,
            update_job_manifest={
                "update_job_id": "upd_123",
                "source_message_id": "src_123",
                "draft_id": "draft_123",
                "approval_id": "approval_123",
                "workflow_session_id": "line_user_U123",
                "idempotency_key": "sig-123",
                "payload": {"candidate_scores": [{"candidate_number": 1, "score": 99}]},
            },
            opener=opener,
        )

        self.assertEqual(opener.requests[0]["url"], "https://api.example.com/updates")
        payload = json.loads(opener.requests[0]["body"].decode("utf-8"))
        self.assertEqual(payload["update_job_id"], "upd_123")
        self.assertEqual(payload["payload"]["candidate_scores"][0]["score"], 99)
        self.assertTrue(result["body"]["updated"])

    def test_build_s3_only_result_describes_local_completion(self) -> None:
        result = build_s3_only_result(
            update_job_manifest={
                "update_job_id": "upd_123",
                "draft_id": "draft_123",
                "approval_id": "approval_123",
            }
        )

        self.assertEqual(result["mode"], "s3_only")
        self.assertEqual(result["update_job_id"], "upd_123")

    def test_process_downloaded_job_marks_completed_and_updates_source_manifest(self) -> None:
        os.environ["UPDATE_WORKER_TARGET_API_BASE_URL"] = "https://api.example.com"
        os.environ["UPDATE_WORKER_S3_PREFIX"] = "api-data/score"
        config = build_config()
        s3_client = _FakeS3Client(
            {
                ("bucket-a", "api-data/score/messages/123/update_job.json"): json.dumps(
                    {
                        "update_job_id": "upd_123",
                        "source_message_id": "src_123",
                        "draft_id": "draft_123",
                        "approval_id": "approval_123",
                        "workflow_session_id": "line_user_U123",
                        "state": "queued",
                        "attempt_count": 0,
                        "payload": {"candidate_scores": [{"candidate_number": 1, "score": 99}]},
                    }
                ).encode("utf-8"),
                ("bucket-a", "api-data/score/messages/src_123/manifest.json"): json.dumps(
                    {"source_message_id": "src_123", "state": "approved"}
                ).encode("utf-8"),
            }
        )
        opener = _RecordingUrlOpen(body=b'{"updated": true}')

        result = process_downloaded_job(
            downloaded_job=DownloadedUpdateJob(
                update_job_id="upd_123",
                source_message_id="src_123",
                workflow_session_id="line_user_U123",
                manifest_bucket="bucket-a",
                manifest_key="api-data/score/messages/123/update_job.json",
                queue_message_id="msg-1",
                receipt_handle="receipt-1",
            ),
            s3_client=s3_client,
            config=config,
            opener=opener,
        )

        self.assertEqual(result["status"], "completed")
        update_manifest = json.loads(s3_client.objects[("bucket-a", "api-data/score/messages/123/update_job.json")].decode("utf-8"))
        source_manifest = json.loads(s3_client.objects[("bucket-a", "api-data/score/messages/src_123/manifest.json")].decode("utf-8"))
        self.assertEqual(update_manifest["state"], "completed")
        self.assertEqual(source_manifest["state"], "updated")
        self.assertEqual(source_manifest["current_update_job_id"], "upd_123")

    def test_process_downloaded_job_completes_without_target_api_when_s3_is_canonical(self) -> None:
        os.environ["UPDATE_WORKER_S3_PREFIX"] = "api-data/score"
        config = build_config()
        s3_client = _FakeS3Client(
            {
                ("bucket-a", "api-data/score/messages/456/update_job.json"): json.dumps(
                    {
                        "update_job_id": "upd_456",
                        "source_message_id": "src_456",
                        "draft_id": "draft_456",
                        "approval_id": "approval_456",
                        "workflow_session_id": "line_user_U456",
                        "state": "queued",
                        "attempt_count": 0,
                        "payload": {"candidate_scores": [{"candidate_number": 1, "score": 101}]},
                    }
                ).encode("utf-8"),
                ("bucket-a", "api-data/score/messages/src_456/manifest.json"): json.dumps(
                    {"source_message_id": "src_456", "state": "approved"}
                ).encode("utf-8"),
            }
        )

        result = process_downloaded_job(
            downloaded_job=DownloadedUpdateJob(
                update_job_id="upd_456",
                source_message_id="src_456",
                workflow_session_id="line_user_U456",
                manifest_bucket="bucket-a",
                manifest_key="api-data/score/messages/456/update_job.json",
                queue_message_id="msg-4",
                receipt_handle="receipt-4",
            ),
            s3_client=s3_client,
            config=config,
        )

        self.assertEqual(result["status"], "completed")
        update_manifest = json.loads(s3_client.objects[("bucket-a", "api-data/score/messages/456/update_job.json")].decode("utf-8"))
        source_manifest = json.loads(s3_client.objects[("bucket-a", "api-data/score/messages/src_456/manifest.json")].decode("utf-8"))
        self.assertEqual(update_manifest["state"], "completed")
        self.assertEqual(update_manifest["result"]["mode"], "s3_only")
        self.assertEqual(source_manifest["state"], "approved")
        self.assertIsNone(source_manifest.get("exception"))

    def test_acknowledge_job_deletes_queue_message(self) -> None:
        os.environ["UPDATE_WORKER_QUEUE_URL"] = "https://example.com/update-jobs"
        config = build_config()
        queue_client = _FakeSqsClient([])

        acknowledge_job(
            queue_client=queue_client,
            config=config,
            downloaded_job=DownloadedUpdateJob(
                update_job_id="upd_123",
                source_message_id="src_123",
                workflow_session_id="line_user_U123",
                manifest_bucket="bucket-a",
                manifest_key="messages/123/update_job.json",
                queue_message_id="msg-1",
                receipt_handle="receipt-1",
            ),
        )

        self.assertEqual(len(queue_client.delete_requests), 1)

    def test_main_without_queue_reports_scaffold_ready(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            main([])
        self.assertIn("scaffold ready", output.getvalue())


if __name__ == "__main__":
    unittest.main()
