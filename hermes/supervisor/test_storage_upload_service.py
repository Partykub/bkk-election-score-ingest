import json
import os
import tempfile
import unittest
from pathlib import Path

from hermes.supervisor.upload_service import LocalMockUploadService, S3UploadService, UploadServiceError, build_upload_service


class _RecordingS3Client:
    def __init__(self) -> None:
        self.requests = []

    def put_object(self, **kwargs):
        self.requests.append(kwargs)
        return {"ETag": '"etag-test"'}


class _FailingS3Client:
    def put_object(self, **kwargs):
        raise RuntimeError("s3 failure")


def _fake_line_content_fetcher(line_message_id: str):
    return type(
        "_FakeLineMessageContent",
        (),
        {"body": b"fake-image-bytes", "content_type": "image/jpeg"},
    )()


class UploadServiceTests(unittest.TestCase):
    def test_local_mock_store_source_message_writes_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LocalMockUploadService(temp_dir)
            session = service.store_source_message(
                {
                    "source_message_id": "src_test_0001",
                    "platform": "line",
                    "line_event_id": "01JXTEST0001",
                    "line_message_id": "548899112233",
                    "sender_user_id": "Uxxxxxxxx",
                    "sender_group_id": "Cxxxxxxxx",
                    "sender_room_id": None,
                },
                received_at="2026-06-09T06:30:00Z",
            )

            self.assertEqual(session.storage_backend, "local-mock")

    def test_local_mock_store_source_message_writes_binary_when_fetcher_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LocalMockUploadService(temp_dir, line_content_fetcher=_fake_line_content_fetcher)
            session = service.store_source_message(
                {
                    "source_message_id": "src_test_0002",
                    "platform": "line",
                    "line_event_id": "01JXTEST0002",
                    "line_message_id": "548899112244",
                    "sender_user_id": "Uxxxxxxxx",
                    "sender_group_id": "Cxxxxxxxx",
                    "sender_room_id": None,
                },
                received_at="2026-06-09T06:30:00Z",
            )

            binary_path = Path(temp_dir) / session.object_key
            self.assertTrue(binary_path.exists())
            self.assertEqual(binary_path.read_bytes(), b"fake-image-bytes")

            metadata_path = Path(temp_dir) / session.metadata_key
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["binary_status"], "stored")
            self.assertEqual(metadata["size_bytes"], len(b"fake-image-bytes"))
            self.assertEqual(metadata["content_type"], "image/jpeg")

    def test_s3_store_source_message_writes_metadata_object(self) -> None:
        client = _RecordingS3Client()
        service = S3UploadService(bucket_name="election-system", key_prefix="dev", client=client)

        session = service.store_source_message(
            {
                "source_message_id": "src_test_0001",
                "platform": "line",
                "line_event_id": "01JXTEST0001",
                "line_message_id": "548899112233",
                "sender_user_id": "Uxxxxxxxx",
                "sender_group_id": "Cxxxxxxxx",
                "sender_room_id": None,
            },
            received_at="2026-06-09T06:30:00Z",
        )

        self.assertEqual(session.storage_backend, "s3")
        self.assertEqual(session.bucket, "election-system")
        self.assertEqual(session.object_key, "dev/messages/src_test_0001/original.bin")
        self.assertEqual(client.requests[0]["Bucket"], "election-system")
        self.assertEqual(client.requests[0]["Key"], "dev/messages/src_test_0001/upload_metadata.json")
        metadata = json.loads(client.requests[0]["Body"].decode("utf-8"))
        self.assertEqual(metadata["storage_backend"], "s3")
        self.assertEqual(metadata["binary_status"], "pending_line_content_fetch")

    def test_s3_store_source_message_writes_binary_and_metadata_when_fetcher_is_configured(self) -> None:
        client = _RecordingS3Client()
        service = S3UploadService(
            bucket_name="election-system",
            key_prefix="dev",
            client=client,
            line_content_fetcher=_fake_line_content_fetcher,
        )

        session = service.store_source_message(
            {
                "source_message_id": "src_test_0002",
                "platform": "line",
                "line_event_id": "01JXTEST0002",
                "line_message_id": "548899112244",
                "sender_user_id": "Uxxxxxxxx",
                "sender_group_id": "Cxxxxxxxx",
                "sender_room_id": None,
            },
            received_at="2026-06-09T06:30:00Z",
        )

        self.assertEqual(session.object_key, "dev/messages/src_test_0002/original.bin")
        self.assertEqual(client.requests[0]["Key"], "dev/messages/src_test_0002/original.bin")
        self.assertEqual(client.requests[0]["Body"], b"fake-image-bytes")
        self.assertEqual(client.requests[1]["Key"], "dev/messages/src_test_0002/upload_metadata.json")
        metadata = json.loads(client.requests[1]["Body"].decode("utf-8"))
        self.assertEqual(metadata["binary_status"], "stored")
        self.assertEqual(metadata["size_bytes"], len(b"fake-image-bytes"))
        self.assertEqual(metadata["content_type"], "image/jpeg")
        self.assertEqual(metadata["object_etag"], '"etag-test"')

    def test_s3_store_source_message_wraps_write_errors(self) -> None:
        service = S3UploadService(bucket_name="election-system", client=_FailingS3Client())

        with self.assertRaises(UploadServiceError):
            service.store_source_message(
                {
                    "source_message_id": "src_test_0001",
                    "platform": "line",
                    "line_event_id": "01JXTEST0001",
                    "line_message_id": "548899112233",
                    "sender_user_id": "Uxxxxxxxx",
                    "sender_group_id": "Cxxxxxxxx",
                    "sender_room_id": None,
                },
                received_at="2026-06-09T06:30:00Z",
            )

    def test_build_upload_service_returns_s3_when_configured(self) -> None:
        original_backend = os.environ.get("SUPERVISOR_STORAGE_BACKEND")
        original_bucket = os.environ.get("SUPERVISOR_S3_BUCKET")
        original_region = os.environ.get("SUPERVISOR_S3_REGION")
        try:
            os.environ["SUPERVISOR_STORAGE_BACKEND"] = "s3"
            os.environ["SUPERVISOR_S3_BUCKET"] = "election-system"
            os.environ["SUPERVISOR_S3_REGION"] = "ap-southeast-1"
            service = build_upload_service(".")
            self.assertIsInstance(service, S3UploadService)
        finally:
            if original_backend is None:
                os.environ.pop("SUPERVISOR_STORAGE_BACKEND", None)
            else:
                os.environ["SUPERVISOR_STORAGE_BACKEND"] = original_backend
            if original_bucket is None:
                os.environ.pop("SUPERVISOR_S3_BUCKET", None)
            else:
                os.environ["SUPERVISOR_S3_BUCKET"] = original_bucket
            if original_region is None:
                os.environ.pop("SUPERVISOR_S3_REGION", None)
            else:
                os.environ["SUPERVISOR_S3_REGION"] = original_region

    def test_build_upload_service_requires_bucket_for_s3(self) -> None:
        original_backend = os.environ.get("SUPERVISOR_STORAGE_BACKEND")
        original_bucket = os.environ.get("SUPERVISOR_S3_BUCKET")
        try:
            os.environ["SUPERVISOR_STORAGE_BACKEND"] = "s3"
            os.environ.pop("SUPERVISOR_S3_BUCKET", None)
            with self.assertRaises(UploadServiceError):
                build_upload_service(".")
        finally:
            if original_backend is None:
                os.environ.pop("SUPERVISOR_STORAGE_BACKEND", None)
            else:
                os.environ["SUPERVISOR_STORAGE_BACKEND"] = original_backend
            if original_bucket is None:
                os.environ.pop("SUPERVISOR_S3_BUCKET", None)
            else:
                os.environ["SUPERVISOR_S3_BUCKET"] = original_bucket


if __name__ == "__main__":
    unittest.main()