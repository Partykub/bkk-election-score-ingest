import io
import json
import os
import unittest

os.environ.setdefault("RESULTS_API_S3_BUCKET", "test-bucket")
from hermes.results_api.app import (
    CandidateCatalog,
    DistrictCatalog,
    ResultsStore,
    build_district_results,
    build_governor_results,
)


class _Paginator:
    def __init__(self, client):
        self.client = client

    def paginate(self, *, Bucket, Prefix):
        contents = [
            {"Key": key}
            for bucket, key in self.client.objects
            if bucket == Bucket and key.startswith(Prefix)
        ]
        return [{"Contents": contents}]


class _S3Client:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def get_paginator(self, name):
        return _Paginator(self)


def _encoded(payload):
    return json.dumps(payload).encode("utf-8")


class ResultsStoreTests(unittest.TestCase):
    def setUp(self):
        source_id = "src_1"
        self.client = _S3Client(
            {
                ("bucket", "indexes/by-area/default/3/submissions.json"): _encoded(
                    {
                        "submission_count": 1,
                        "submissions": [{"source_message_id": source_id}],
                    }
                ),
                ("bucket", f"messages/{source_id}/manifest.json"): _encoded(
                    {
                        "state": "approved",
                        "current_approval_key": f"messages/{source_id}/approval_r1.json",
                        "current_draft_key": f"messages/{source_id}/draft_r1.json",
                        "created_at": "2026-06-15T01:00:00Z",
                    }
                ),
                ("bucket", f"messages/{source_id}/approval_r1.json"): _encoded(
                    {"state": "approved", "responded_at": "2026-06-15T01:01:00Z"}
                ),
                ("bucket", f"messages/{source_id}/draft_r1.json"): _encoded(
                    {
                        "area_id": "3",
                        "candidate_scores": [{"candidate_number": 1, "score": 120, "raw_text": "1 120"}],
                    }
                ),
            }
        )
        self.store = ResultsStore(s3_client=self.client, bucket="bucket")

    def test_lists_area_indexes(self):
        self.assertEqual(self.store.list_area_indexes("default"), ["3"])

    def test_returns_only_public_approved_fields(self):
        result = self.store.approved_result("src_1")
        self.assertEqual(result["candidate_scores"][0]["score"], 120)
        self.assertNotIn("raw_text", result["candidate_scores"][0])
        self.assertNotIn("candidate_name", result["candidate_scores"][0])
        self.assertNotIn("election_id", result)
        self.assertNotIn("polling_unit_id", result)

    def test_returns_approved_results_for_area(self):
        results = self.store.approved_results_for_area("default", "3")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["approved_at"], "2026-06-15T01:01:00Z")

    def test_governor_results_sum_latest_approved_result_per_area(self):
        payload = build_governor_results(
            approved_results=[
                {
                    "source_message_id": "src_area_3",
                    "election_id": "default",
                    "area_id": "3",
                    "approved_at": "2026-06-15T01:01:00Z",
                    "candidate_scores": [
                        {"candidate_number": 1, "score": 120},
                        {"candidate_number": 2, "score": 80},
                    ],
                },
                {
                    "source_message_id": "src_area_18",
                    "election_id": "default",
                    "area_id": "18",
                    "approved_at": "2026-06-15T01:02:00Z",
                    "candidate_scores": [
                        {"candidate_number": 1, "score": 30},
                        {"candidate_number": 2, "score": 70},
                    ],
                },
            ],
            candidate_catalog={
                1: {"candidateId": "one", "name": "Candidate One", "color": "#111111"},
                2: {"candidateId": "two", "name": "Candidate Two", "color": "#222222"},
            },
            total_units=50,
            generated_at="2026-06-15T01:03:00.000Z",
        )

        self.assertEqual(payload["schemaVersion"], "1.0")
        self.assertEqual(payload["resource"], "governor-results")
        self.assertEqual(payload["pageMeta"]["electionId"], "bkk-governor-2026")
        self.assertEqual(payload["pageMeta"]["resultStatus"], "LIVE_COUNT")
        self.assertEqual(payload["pageMeta"]["generatedAt"], "2026-06-15T01:03:00.000Z")
        self.assertEqual(payload["summary"]["countedUnits"], 2)
        self.assertEqual(payload["summary"]["totalUnits"], 50)
        self.assertEqual(payload["summary"]["countedPercentage"], 4.0)
        self.assertIsNone(payload["summary"]["eligibleVoters"])
        self.assertIsNone(payload["summary"]["voterTurnout"])
        self.assertIsNone(payload["summary"]["voterTurnoutPercentage"])
        self.assertIsNone(payload["summary"]["validBallots"])
        self.assertIsNone(payload["summary"]["invalidBallots"])
        self.assertIsNone(payload["summary"]["abstainedBallots"])
        self.assertEqual(payload["candidates"][0]["candidateNumber"], 1)
        self.assertEqual(payload["candidates"][0]["voteCount"], 150)
        self.assertEqual(payload["candidates"][0]["votePercentage"], 50.0)
        self.assertEqual(payload["candidates"][0]["rank"], 1)
        self.assertTrue(payload["candidates"][0]["isLeading"])
        self.assertEqual(payload["candidates"][0]["name"], "Candidate One")
        self.assertEqual(payload["candidates"][0]["candidateId"], "one")
        self.assertEqual(payload["candidates"][0]["color"], "#111111")
        self.assertNotIn("sources", payload)
        self.assertFalse(payload["dataQuality"]["isComplete"])
        self.assertFalse(payload["dataQuality"]["isDelayed"])
        self.assertTrue(payload["dataQuality"]["warnings"])

    def test_governor_results_keeps_candidate_fields_when_catalog_is_unavailable(self):
        payload = build_governor_results(
            approved_results=[
                {
                    "approved_at": "2026-06-15T01:02:00Z",
                    "candidate_scores": [{"candidate_number": 1, "score": 80}],
                }
            ],
            total_units=None,
            generated_at="2026-06-15T01:03:00Z",
        )

        candidate = payload["candidates"][0]
        self.assertIsNone(candidate["candidateId"])
        self.assertIsNone(candidate["name"])
        self.assertIsNone(candidate["color"])
        self.assertIsNone(payload["summary"]["totalUnits"])
        self.assertIsNone(payload["summary"]["countedPercentage"])
        self.assertIsNone(payload["dataQuality"]["isComplete"])

    def test_governor_results_aggregates_complete_turnout_fields(self):
        payload = build_governor_results(
            approved_results=[
                {
                    "approved_at": "2026-06-15T01:02:00Z",
                    "candidate_scores": [{"candidate_number": 1, "score": 80}],
                    "eligible_voters": 100,
                    "voter_turnout": 80,
                    "valid_ballots": 75,
                    "invalid_ballots": 3,
                    "abstained_ballots": 2,
                },
                {
                    "approved_at": "2026-06-15T01:02:30Z",
                    "candidate_scores": [{"candidate_number": 1, "score": 90}],
                    "eligible_voters": 120,
                    "voter_turnout": 100,
                    "valid_ballots": 90,
                    "invalid_ballots": 5,
                    "abstained_ballots": 5,
                },
            ],
            total_units=2,
            generated_at="2026-06-15T01:03:00Z",
        )

        self.assertEqual(payload["summary"]["eligibleVoters"], 220)
        self.assertEqual(payload["summary"]["voterTurnout"], 180)
        self.assertEqual(payload["summary"]["voterTurnoutPercentage"], 81.82)
        self.assertEqual(payload["summary"]["validBallots"], 165)
        self.assertEqual(payload["summary"]["invalidBallots"], 8)
        self.assertEqual(payload["summary"]["abstainedBallots"], 7)
        self.assertTrue(payload["dataQuality"]["isComplete"])
        self.assertFalse(payload["dataQuality"]["isDelayed"])

    def test_candidate_catalog_joins_manifest_and_featured_by_candidate_number(self):
        catalog = CandidateCatalog(
            manifest_url="https://example.test/manifest.json",
            featured_url="https://example.test/featured.json",
            cache_seconds=300,
        )
        payloads = {
            catalog.manifest_url: {
                "profiles": [
                    {"id": "one", "candidateNumber": 1, "name": "Candidate One"},
                ]
            },
            catalog.featured_url: {
                "candidates": [
                    {
                        "id": "one",
                        "candidateNumber": 1,
                        "themeColor": "#123456",
                        "candidateSrc": "https://example.test/one.png",
                    },
                ]
            },
        }
        catalog._read_json_url = payloads.__getitem__

        candidates = catalog.candidates_by_number()

        self.assertEqual(
            candidates[1],
            {
                "candidateId": "one",
                "name": "Candidate One",
                "color": "#123456",
                "candidateSrc": "https://example.test/one.png",
            },
        )

    def test_district_catalog_maps_area_id_to_district_master(self):
        catalog = DistrictCatalog(url="https://example.test/districts.json")
        catalog._read_json_url = lambda: [
            {
                "id": 3,
                "provinceCode": 10,
                "districtCode": 1003,
                "districtNameEn": "Nong Chok",
                "districtNameTh": "หนองจอก",
            }
        ]

        self.assertEqual(catalog.districts_by_id()["3"]["districtNameTh"], "หนองจอก")

    def test_build_district_results_uses_district_and_candidate_master_data(self):
        payload = build_district_results(
            approved_results=[
                {
                    "area_id": "3",
                    "candidate_scores": [
                        {"candidate_number": 1, "score": 120},
                        {"candidate_number": 2, "score": 80},
                    ],
                }
            ],
            candidate_catalog={
                1: {
                    "candidateId": "one",
                    "name": "Candidate One",
                    "color": "#111111",
                    "candidateSrc": "https://example.test/one.png",
                },
                2: {"candidateId": "two", "name": "Candidate Two", "color": "#222222"},
            },
            district_catalog={
                "3": {
                    "id": 3,
                    "provinceCode": 10,
                    "districtCode": 1003,
                    "districtNameTh": "หนองจอก",
                },
                "4": {
                    "id": 4,
                    "provinceCode": 10,
                    "districtCode": 1004,
                    "districtNameTh": "บางรัก",
                }
            },
            generated_at="2026-06-15T01:03:00.000Z",
        )

        constituency = payload["data"]["constituencies"][0]
        self.assertEqual(constituency["areaId"], "3")
        self.assertEqual(constituency["number"], 3)
        self.assertEqual(constituency["name"], "หนองจอก")
        self.assertEqual(constituency["leadingCandidateId"], "one")
        self.assertEqual(constituency["candidates"][0]["votePercentage"], 60.0)
        self.assertEqual(len(payload["data"]["constituencies"]), 2)
        empty_constituency = payload["data"]["constituencies"][1]
        self.assertEqual(empty_constituency["areaId"], "4")
        self.assertIsNone(empty_constituency["leadingCandidateId"])
        self.assertEqual(empty_constituency["candidates"], [])

    def test_build_district_results_excludes_non_bangkok_districts(self):
        payload = build_district_results(
            approved_results=[],
            candidate_catalog={},
            district_catalog={
                "1": {"id": 1, "provinceCode": 10, "districtNameTh": "พระนคร"},
                "51": {"id": 51, "provinceCode": 11, "districtNameTh": "เมืองสมุทรปราการ"},
            },
            generated_at="2026-06-15T01:03:00.000Z",
        )

        constituencies = payload["data"]["constituencies"]
        self.assertEqual(len(constituencies), 1)
        self.assertEqual(constituencies[0]["name"], "พระนคร")


if __name__ == "__main__":
    unittest.main()
