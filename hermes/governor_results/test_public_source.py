import io
import json
import unittest

from botocore.exceptions import ClientError

from hermes.governor_results.public_source import (
    ACTIVE_PUBLIC_SOURCE_KEY,
    BKK_TARGET,
    LINE_TARGET,
    LIVE_TARGET,
    PublicSourceNotFoundError,
    SORKOR_EXPORT_FILES,
    effective_active_public_source_config,
    monitor_config_key,
    optional_promote_files_for_source,
    parent_prefix_from_static_prefix,
    prefix_for_target,
    promote_public_results,
    read_active_public_source,
    source_target_for_public_source,
    write_active_public_source,
)


class _S3Client:
    def __init__(self, objects: dict[tuple[str, str], bytes]):
        self.objects = objects

    def get_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "GetObject",
            )
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)]), "ContentType": "application/json; charset=utf-8"}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else bytes(Body)
        return {"ETag": '"fake"'}


def _encoded(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


class PublicSourceTests(unittest.TestCase):
    def test_prefix_helpers(self):
        self.assertEqual(parent_prefix_from_static_prefix("api-data/governor-results-dev"), "api-data")
        self.assertEqual(prefix_for_target(LINE_TARGET, "api-data"), "api-data/governor-results-dev")
        self.assertEqual(source_target_for_public_source("line"), LINE_TARGET)
        self.assertEqual(source_target_for_public_source("bkk"), BKK_TARGET)

    def test_read_and_write_active_public_source(self):
        client = _S3Client({})
        saved = write_active_public_source(
            s3_client=client,
            bucket="bucket",
            score_prefix="api-data/score",
            source="bkk",
        )
        self.assertEqual(saved["source"], "bkk")
        key = monitor_config_key("api-data/score", ACTIVE_PUBLIC_SOURCE_KEY)
        self.assertIn(("bucket", key), client.objects)

        loaded = read_active_public_source(s3_client=client, bucket="bucket", score_prefix="api-data/score")
        self.assertEqual(loaded["source"], "bkk")

    def test_promote_public_results_copies_live_files(self):
        client = _S3Client(
            {
                ("bucket", "api-data/governor-results-dev/sumary.json"): _encoded({"resource": "summary"}),
                ("bucket", "api-data/governor-results-dev/districts.json"): _encoded({"resource": "districts"}),
            }
        )
        result = promote_public_results(
            s3_client=client,
            bucket="bucket",
            parent_prefix="api-data",
            source_target=LINE_TARGET,
        )
        self.assertEqual(result["livePrefix"], "api-data/governor-results")
        self.assertIn(("bucket", "api-data/governor-results/sumary.json"), client.objects)
        self.assertIn(("bucket", "api-data/governor-results/districts.json"), client.objects)

    def test_promote_bkk_includes_optional_sorkor_files(self):
        client = _S3Client(
            {
                ("bucket", "api-data/governor-results-bkk/sumary.json"): _encoded({"resource": "summary"}),
                ("bucket", "api-data/governor-results-bkk/districts.json"): _encoded({"resource": "districts"}),
                ("bucket", "api-data/governor-results-bkk/sumary-sorkor.json"): _encoded({"resource": "sorkor-results"}),
                ("bucket", "api-data/governor-results-bkk/districts-sorkor.json"): _encoded({"resource": "districts"}),
            }
        )
        result = promote_public_results(
            s3_client=client,
            bucket="bucket",
            parent_prefix="api-data",
            source_target=BKK_TARGET,
            optional_files=SORKOR_EXPORT_FILES,
        )
        self.assertEqual(len(result["keys"]), 4)
        self.assertIn(("bucket", "api-data/governor-results/sumary-sorkor.json"), client.objects)

    def test_promote_bkk_skips_missing_optional_sorkor_files(self):
        client = _S3Client(
            {
                ("bucket", "api-data/governor-results-bkk/sumary.json"): _encoded({"resource": "summary"}),
                ("bucket", "api-data/governor-results-bkk/districts.json"): _encoded({"resource": "districts"}),
            }
        )
        result = promote_public_results(
            s3_client=client,
            bucket="bucket",
            parent_prefix="api-data",
            source_target=BKK_TARGET,
            optional_files=SORKOR_EXPORT_FILES,
        )
        self.assertEqual(len(result["keys"]), 2)
        self.assertEqual(result["skippedOptional"], list(SORKOR_EXPORT_FILES))

    def test_optional_promote_files_for_source(self):
        self.assertEqual(optional_promote_files_for_source("bkk"), SORKOR_EXPORT_FILES)
        self.assertEqual(optional_promote_files_for_source("line"), ())

    def test_promote_public_results_raises_when_source_missing(self):
        client = _S3Client({})
        with self.assertRaises(PublicSourceNotFoundError):
            promote_public_results(
                s3_client=client,
                bucket="bucket",
                parent_prefix="api-data",
                source_target=BKK_TARGET,
            )

    def test_effective_active_public_source_config(self):
        config = effective_active_public_source_config(
            parent_prefix="api-data",
            saved={"source": "line", "updatedAt": "2026-06-24T00:00:00Z"},
        )
        self.assertEqual(config["source"], "line")
        self.assertEqual(config["livePrefix"], "api-data/governor-results")
        self.assertEqual(config["linePrefix"], "api-data/governor-results-dev")
        self.assertEqual(config["bkkPrefix"], "api-data/governor-results-bkk")


if __name__ == "__main__":
    unittest.main()
