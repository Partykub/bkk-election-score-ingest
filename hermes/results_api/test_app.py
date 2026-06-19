import io
import json
import os
import unittest

from botocore.exceptions import BotoCoreError, ClientError
from fastapi.testclient import TestClient

os.environ.setdefault("RESULTS_API_S3_BUCKET", "test-bucket")
import hermes.results_api.app as results_app
from hermes.results_api.app import (
    CandidateCatalog,
    DistrictCatalog,
    ResultsStore,
    aggregate_incremental_area_results,
    build_district_results,
    build_governor_results,
    build_monitor_districts,
    build_monitor_overview,
    interpreted_area_result,
    monitor_missing_fields,
    monitor_validation_warnings,
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
        if (Bucket, Key) not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "GetObject",
            )
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else bytes(Body)
        return {"ETag": '"fake"'}

    def get_paginator(self, name):
        return _Paginator(self)


class _FailingS3Client:
    def get_object(self, *, Bucket, Key):
        raise BotoCoreError(error="credentials expired")

    def get_paginator(self, name):
        raise BotoCoreError(error="credentials expired")


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


class MonitorReadModelTests(unittest.TestCase):
    def setUp(self):
        self.district_catalog = {
            "1": {
                "id": 1,
                "provinceCode": 10,
                "districtCode": 1001,
                "districtNameTh": "District One TH",
                "districtNameEn": "District One",
            },
            "2": {
                "id": 2,
                "provinceCode": 10,
                "districtCode": 1002,
                "districtNameTh": "District Two TH",
                "districtNameEn": "District Two",
            },
            "3": {
                "id": 3,
                "provinceCode": 10,
                "districtCode": 1003,
                "districtNameTh": "District Three TH",
                "districtNameEn": "District Three",
            },
            "51": {
                "id": 51,
                "provinceCode": 11,
                "districtCode": 1101,
                "districtNameTh": "Outside Bangkok TH",
                "districtNameEn": "Outside Bangkok",
            },
        }
        self.complete_result = {
            "area_id": "1",
            "approved_at": "2026-06-16T03:00:00Z",
            "candidate_scores": [
                {"candidate_number": 1, "score": 70},
                {"candidate_number": 2, "score": 30},
            ],
            "eligible_voters": 120,
            "voter_turnout": 100,
            "valid_ballots": 90,
            "invalid_ballots": 5,
            "abstained_ballots": 5,
        }

    def test_monitor_missing_fields_requires_scores_and_ballot_fields(self):
        self.assertEqual(
            monitor_missing_fields(None),
            [
                "candidate_scores",
                "eligible_voters",
                "voter_turnout",
                "valid_ballots",
                "invalid_ballots",
                "abstained_ballots",
            ],
        )
        self.assertEqual(
            monitor_missing_fields({"candidate_scores": [{"candidate_number": 1, "score": 1}]}),
            [
                "eligible_voters",
                "voter_turnout",
                "valid_ballots",
                "invalid_ballots",
                "abstained_ballots",
            ],
        )
        self.assertEqual(monitor_missing_fields(self.complete_result), [])

    def test_monitor_validation_warnings_checks_ballot_consistency(self):
        warnings = monitor_validation_warnings({**self.complete_result, "voter_turnout": 101})

        self.assertIn(
            "voter_turnout does not equal valid_ballots + invalid_ballots + abstained_ballots.",
            warnings,
        )

    def test_build_monitor_districts_marks_no_data_pending_missing_and_complete(self):
        payload = build_monitor_districts(
            district_catalog=self.district_catalog,
            area_indexes={
                "1": {
                    "submission_count": 1,
                    "updated_at": "2026-06-16T02:59:00Z",
                    "submissions": [{"source_message_id": "src_1", "submitted_at": "2026-06-16T02:59:00Z"}],
                },
                "2": {
                    "submission_count": 1,
                    "submissions": [{"source_message_id": "src_2", "submitted_at": "2026-06-16T02:58:00Z"}],
                },
                "3": {
                    "submission_count": 1,
                    "submissions": [{"source_message_id": "src_3", "submitted_at": "2026-06-16T02:57:00Z"}],
                },
            },
            approved_results_by_area={
                "1": [self.complete_result],
                "3": [
                    {
                        "area_id": "3",
                        "approved_at": "2026-06-16T03:00:00Z",
                        "candidate_scores": [{"candidate_number": 2, "score": 10}],
                    }
                ],
            },
            candidate_catalog={1: {"candidateId": "one"}, 2: {"candidateId": "two"}},
            delayed_after_minutes=30,
            generated_at="2026-06-16T03:10:00Z",
        )

        districts = {district["areaId"]: district for district in payload["districts"]}
        self.assertEqual(payload["electionId"], "bkk-governor-2026")
        self.assertEqual(set(districts), {"1", "2", "3"})
        self.assertEqual(districts["1"]["status"], "complete")
        self.assertEqual(districts["1"]["leadingCandidateId"], "one")
        self.assertEqual(districts["2"]["status"], "pending")
        self.assertEqual(districts["3"]["status"], "missing_fields")
        self.assertEqual(
            districts["3"]["missingFields"],
            [
                "eligible_voters",
                "voter_turnout",
                "valid_ballots",
                "invalid_ballots",
                "abstained_ballots",
            ],
        )

    def test_build_monitor_districts_marks_no_data_when_master_district_has_no_index(self):
        payload = build_monitor_districts(
            district_catalog=self.district_catalog,
            area_indexes={},
            approved_results_by_area={},
            generated_at="2026-06-16T03:10:00Z",
        )

        statuses = {district["areaId"]: district["status"] for district in payload["districts"]}

        self.assertEqual(statuses, {"1": "no_data", "2": "no_data", "3": "no_data"})

    def test_build_monitor_districts_allows_repeated_approved_reports(self):
        payload = build_monitor_districts(
            district_catalog=self.district_catalog,
            area_indexes={
                "1": {"submission_count": 1, "submissions": [{"source_message_id": "src_1"}]},
                "2": {
                    "submission_count": 2,
                    "submissions": [{"source_message_id": "src_2"}, {"source_message_id": "src_3"}],
                },
            },
            approved_results_by_area={
                "1": [{**self.complete_result, "approved_at": "2026-06-16T02:00:00Z"}],
                "2": [
                    {**self.complete_result, "area_id": "2", "approved_at": "2026-06-16T03:00:00Z"},
                    {**self.complete_result, "area_id": "2", "approved_at": "2026-06-16T02:59:00Z"},
                ],
            },
            generated_at="2026-06-16T03:10:00Z",
            delayed_after_minutes=30,
        )

        districts = {district["areaId"]: district for district in payload["districts"]}

        self.assertEqual(districts["1"]["status"], "delayed")
        self.assertEqual(districts["2"]["status"], "complete")
        self.assertEqual(districts["2"]["approvedSubmissionCount"], 2)
        self.assertEqual(districts["2"]["latestApprovedAt"], "2026-06-16T03:00:00Z")
        self.assertEqual(districts["2"].get("warnings"), [])

    def test_build_monitor_districts_keeps_raw_approved_count_in_latest_snapshot_mode(self):
        payload = build_monitor_districts(
            district_catalog=self.district_catalog,
            area_indexes={
                "2": {
                    "submission_count": 2,
                    "submissions": [{"source_message_id": "src_2"}, {"source_message_id": "src_3"}],
                },
            },
            approved_results_by_area={
                "2": [{**self.complete_result, "area_id": "2", "approved_at": "2026-06-16T03:00:00Z"}],
            },
            raw_approved_results_by_area={
                "2": [
                    {**self.complete_result, "area_id": "2", "approved_at": "2026-06-16T03:00:00Z"},
                    {**self.complete_result, "area_id": "2", "approved_at": "2026-06-16T02:59:00Z"},
                ],
            },
            generated_at="2026-06-16T03:10:00Z",
            delayed_after_minutes=30,
        )

        district = {district["areaId"]: district for district in payload["districts"]}["2"]

        self.assertEqual(district["status"], "complete")
        self.assertEqual(district["approvedSubmissionCount"], 2)
        self.assertEqual(district["latestApprovedAt"], "2026-06-16T03:00:00Z")

    def test_build_monitor_districts_marks_validation_warning_as_conflict(self):
        payload = build_monitor_districts(
            district_catalog=self.district_catalog,
            area_indexes={"1": {"submission_count": 1, "submissions": [{"source_message_id": "src_1"}]}},
            approved_results_by_area={
                "1": [{**self.complete_result, "voter_turnout": 101, "approved_at": "2026-06-16T03:00:00Z"}],
            },
            generated_at="2026-06-16T03:10:00Z",
            delayed_after_minutes=30,
        )

        district = {district["areaId"]: district for district in payload["districts"]}["1"]

        self.assertEqual(district["status"], "conflict")
        self.assertIn(
            "voter_turnout does not equal valid_ballots + invalid_ballots + abstained_ballots.",
            district["warnings"],
        )

    def test_aggregate_incremental_area_results_sums_report_history(self):
        result = aggregate_incremental_area_results(
            [
                {
                    **self.complete_result,
                    "approved_at": "2026-06-16T03:30:00Z",
                    "candidate_scores": [{"candidate_number": 1, "score": 35}, {"candidate_number": 2, "score": 7}],
                    "eligible_voters": 100,
                    "voter_turnout": 42,
                    "valid_ballots": 40,
                    "invalid_ballots": 1,
                    "abstained_ballots": 1,
                },
                {
                    **self.complete_result,
                    "approved_at": "2026-06-16T03:00:00Z",
                    "candidate_scores": [{"candidate_number": 1, "score": 30}, {"candidate_number": 2, "score": 5}],
                    "eligible_voters": 100,
                    "voter_turnout": 35,
                    "valid_ballots": 33,
                    "invalid_ballots": 1,
                    "abstained_ballots": 1,
                },
            ]
        )

        scores = {score["candidate_number"]: score["score"] for score in result["candidate_scores"]}
        self.assertEqual(scores, {1: 65, 2: 12})
        self.assertEqual(result["voter_turnout"], 77)
        self.assertEqual(result["included_report_count"], 2)

    def test_latest_snapshot_uses_latest_approved_result_only(self):
        result = interpreted_area_result(
            [
                {
                    **self.complete_result,
                    "approved_at": "2026-06-16T03:30:00Z",
                    "candidate_scores": [],
                    "eligible_voters": 200,
                    "voter_turnout": 155,
                    "valid_ballots": 105,
                    "invalid_ballots": 50,
                    "abstained_ballots": 0,
                    "validation_flags": ["missing_candidate_scores", "no_candidate_scores_found"],
                },
                {
                    **self.complete_result,
                    "approved_at": "2026-06-16T03:00:00Z",
                    "candidate_scores": [
                        {"candidate_number": 1, "score": 35},
                        {"candidate_number": 2, "score": 7},
                    ],
                },
            ],
            "latest_snapshot",
        )

        self.assertEqual(result["candidate_scores"], [])
        self.assertEqual(result["approved_at"], "2026-06-16T03:30:00Z")
        self.assertEqual(result["voter_turnout"], 155)
        self.assertEqual(result["data_interpretation_mode"], "latest_snapshot")
        self.assertEqual(result["included_report_count"], 1)
        self.assertEqual(result.get("validation_flags"), ["missing_candidate_scores", "no_candidate_scores_found"])

    def test_build_monitor_overview_aggregates_district_statuses(self):
        monitor_payload = build_monitor_districts(
            district_catalog=self.district_catalog,
            area_indexes={"1": {"submission_count": 1, "submissions": [{"source_message_id": "src_1"}]}},
            approved_results_by_area={"1": [self.complete_result]},
            generated_at="2026-06-16T03:10:00Z",
        )

        overview = build_monitor_overview(
            monitor_districts=monitor_payload["districts"],
            election_id="bkk-governor-2026",
            generated_at="2026-06-16T03:10:00Z",
        )

        self.assertEqual(overview["overview"]["totalDistricts"], 3)
        self.assertEqual(overview["overview"]["districtsWithData"], 1)
        self.assertEqual(overview["overview"]["districtsWithoutData"], 2)
        self.assertEqual(overview["overview"]["completeDistricts"], 1)
        self.assertEqual(overview["overview"]["incompleteDistricts"], 2)
        self.assertEqual(overview["overview"]["latestApprovedAt"], "2026-06-16T03:00:00Z")
        self.assertFalse(overview["dataQuality"]["isComplete"])


class MonitorApiTests(unittest.TestCase):
    def setUp(self):
        self.original_store = results_app.store
        self.original_district_catalog = results_app.district_catalog
        self.original_candidate_catalog = results_app.candidate_catalog
        results_app._monitor_cache_payload = None
        results_app._monitor_cache_at = 0.0
        results_app._governor_results_cache_payload = None
        results_app._governor_results_cache_at = 0.0

        source_id = "src_monitor_1"
        self.s3_client = _S3Client(
            {
                ("bucket", "indexes/by-area/default/1/submissions.json"): _encoded(
                    {
                        "submission_count": 1,
                        "updated_at": "2026-06-16T02:59:00Z",
                        "submissions": [{"source_message_id": source_id, "submitted_at": "2026-06-16T02:59:00Z"}],
                    }
                ),
                ("bucket", f"messages/{source_id}/manifest.json"): _encoded(
                    {
                        "state": "approved",
                        "current_approval_key": f"messages/{source_id}/approval_r1.json",
                        "current_draft_key": f"messages/{source_id}/draft_r1.json",
                        "created_at": "2026-06-16T02:59:00Z",
                        "updated_at": "2099-06-16T03:00:00Z",
                    }
                ),
                ("bucket", f"messages/{source_id}/approval_r1.json"): _encoded(
                    {"state": "approved", "responded_at": "2099-06-16T03:00:00Z"}
                ),
                ("bucket", f"messages/{source_id}/draft_r1.json"): _encoded(
                    {
                        "area_id": "1",
                        "candidate_scores": [
                            {"candidate_number": 1, "score": 70},
                            {"candidate_number": 2, "score": 30},
                        ],
                        "eligible_voters": 120,
                        "voter_turnout": 100,
                        "valid_ballots": 90,
                        "invalid_ballots": 5,
                        "abstained_ballots": 5,
                    }
                ),
                ("bucket", "indexes/by-area/default/2/submissions.json"): _encoded(
                    {
                        "submission_count": 1,
                        "submissions": [{"source_message_id": "src_pending", "submitted_at": "2026-06-16T03:01:00Z"}],
                    }
                ),
            }
        )
        results_app.store = ResultsStore(s3_client=self.s3_client, bucket="bucket")

        district_catalog = DistrictCatalog(url="https://example.test/districts.json")
        district_catalog._read_json_url = lambda: [
            {
                "id": 1,
                "provinceCode": 10,
                "districtCode": 1001,
                "districtNameTh": "District One TH",
                "districtNameEn": "District One",
            },
            {
                "id": 2,
                "provinceCode": 10,
                "districtCode": 1002,
                "districtNameTh": "District Two TH",
                "districtNameEn": "District Two",
            },
            {
                "id": 3,
                "provinceCode": 10,
                "districtCode": 1003,
                "districtNameTh": "District Three TH",
                "districtNameEn": "District Three",
            },
        ]
        results_app.district_catalog = district_catalog

        candidate_catalog = CandidateCatalog(
            manifest_url="https://example.test/manifest.json",
            featured_url="https://example.test/featured.json",
        )
        candidate_catalog._read_json_url = lambda url: {
            candidate_catalog.manifest_url: {
                "profiles": [
                    {"id": "one", "candidateNumber": 1, "name": "Candidate One"},
                    {"id": "two", "candidateNumber": 2, "name": "Candidate Two"},
                ]
            },
            candidate_catalog.featured_url: {
                "candidates": [
                    {"id": "one", "candidateNumber": 1, "themeColor": "#111111"},
                    {"id": "two", "candidateNumber": 2, "themeColor": "#222222"},
                ]
            },
        }[url]
        results_app.candidate_catalog = candidate_catalog
        self.client = TestClient(results_app.app)

    def tearDown(self):
        results_app.store = self.original_store
        results_app.district_catalog = self.original_district_catalog
        results_app.candidate_catalog = self.original_candidate_catalog
        results_app._monitor_cache_payload = None
        results_app._monitor_cache_at = 0.0
        results_app._governor_results_cache_payload = None
        results_app._governor_results_cache_at = 0.0

    def test_monitor_page_is_served_locally(self):
        response = self.client.get("/monitor")

        self.assertEqual(response.status_code, 200)
        self.assertIn("แดชบอร์ดติดตามผลเลือกตั้ง", response.text)
        self.assertIn("Dashboard รวมทุกเขต", response.text)
        self.assertIn("/api/v1/monitor/overview", response.text)
        self.assertIn("/api/v1/governor-results/summary", response.text)
        self.assertIn("function timeText", response.text)

    def test_monitor_districts_endpoint_returns_all_master_districts(self):
        response = self.client.get("/api/v1/monitor/districts")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        districts = {district["areaId"]: district for district in payload["districts"]}
        self.assertEqual(set(districts), {"1", "2", "3"})
        self.assertEqual(districts["1"]["status"], "complete")
        self.assertEqual(districts["2"]["status"], "pending")
        self.assertEqual(districts["3"]["status"], "no_data")

    def test_monitor_overview_endpoint_aggregates_statuses(self):
        response = self.client.get("/api/v1/monitor/overview")

        self.assertEqual(response.status_code, 200)
        overview = response.json()["overview"]
        self.assertEqual(overview["totalDistricts"], 3)
        self.assertEqual(overview["districtsWithData"], 2)
        self.assertEqual(overview["districtsWithoutData"], 1)
        self.assertEqual(overview["completeDistricts"], 1)

    def test_monitor_district_detail_returns_known_empty_district(self):
        response = self.client.get("/api/v1/monitor/districts/3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["areaId"], "3")
        self.assertEqual(payload["status"], "no_data")
        self.assertEqual(payload["submissions"], [])

    def test_monitor_district_detail_rejects_unknown_area(self):
        response = self.client.get("/api/v1/monitor/districts/99")

        self.assertEqual(response.status_code, 404)

    def test_monitor_districts_returns_502_when_s3_is_unavailable(self):
        results_app.store = ResultsStore(s3_client=_FailingS3Client(), bucket="bucket")

        response = self.client.get("/api/v1/monitor/districts")

        self.assertEqual(response.status_code, 502)
        self.assertIn("S3 data is unavailable", response.json()["detail"])

    def test_page_meta_override_updates_governor_summary_and_writes_audit(self):
        original = self.client.get("/api/v1/governor-results/summary").json()
        self.assertNotEqual(original["pageMeta"]["title"], "New Thai Title")

        response = self.client.patch(
            "/api/v1/monitor/page-meta",
            json={
                "title": "New Thai Title",
                "resultStatus": "OFFICIAL",
                "actor": "tester",
                "reason": "test update",
            },
        )

        self.assertEqual(response.status_code, 200)
        summary = self.client.get("/api/v1/governor-results/summary").json()
        self.assertEqual(summary["pageMeta"]["title"], "New Thai Title")
        self.assertEqual(summary["pageMeta"]["resultStatus"], "OFFICIAL")

        audit_events = self.client.get("/api/v1/monitor/audit-events").json()["events"]
        self.assertEqual(audit_events[0]["event_type"], "page_meta_updated")
        self.assertEqual(audit_events[0]["actor"], "tester")

    def test_summary_override_updates_public_summary_without_mutating_raw_result(self):
        response = self.client.patch(
            "/api/v1/monitor/districts/1/summary",
            json={
                "eligibleVoters": 200,
                "voterTurnout": 120,
                "validBallots": 100,
                "invalidBallots": 10,
                "abstainedBallots": 10,
                "actor": "tester",
            },
        )

        self.assertEqual(response.status_code, 200)
        summary = self.client.get("/api/v1/governor-results/summary").json()["summary"]
        self.assertEqual(summary["eligibleVoters"], 200)
        self.assertEqual(summary["voterTurnout"], 120)
        self.assertEqual(summary["validBallots"], 100)
        self.assertEqual(summary["invalidBallots"], 10)
        self.assertEqual(summary["abstainedBallots"], 10)

        raw_draft = json.loads(self.s3_client.objects[("bucket", "messages/src_monitor_1/draft_r1.json")].decode("utf-8"))
        self.assertEqual(raw_draft["eligible_voters"], 120)
        self.assertEqual(raw_draft["voter_turnout"], 100)

    def test_page_meta_override_rejects_invalid_result_status(self):
        response = self.client.patch(
            "/api/v1/monitor/page-meta",
            json={"title": "Valid title", "resultStatus": "BAD"},
        )

        self.assertEqual(response.status_code, 400)

    def test_data_mode_override_updates_summary_metadata_and_writes_audit(self):
        response = self.client.patch(
            "/api/v1/monitor/data-mode",
            json={"mode": "incremental_delta", "actor": "tester", "reason": "field reports are deltas"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["dataMode"]["mode"], "incremental_delta")

        summary = self.client.get("/api/v1/governor-results/summary").json()
        self.assertEqual(summary["dataInterpretation"]["mode"], "incremental_delta")

        audit_events = self.client.get("/api/v1/monitor/audit-events").json()["events"]
        self.assertEqual(audit_events[0]["event_type"], "data_mode_updated")
        self.assertEqual(audit_events[0]["actor"], "tester")

    def test_data_mode_override_rejects_invalid_mode(self):
        response = self.client.patch("/api/v1/monitor/data-mode", json={"mode": "guess_wrong"})

        self.assertEqual(response.status_code, 400)

    def test_incremental_data_mode_sums_multiple_approved_reports_per_area(self):
        self.s3_client.objects[("bucket", "indexes/by-area/default/1/submissions.json")] = _encoded(
            {
                "submission_count": 2,
                "updated_at": "2026-06-16T03:29:00Z",
                "submissions": [
                    {"source_message_id": "src_monitor_1", "submitted_at": "2026-06-16T02:59:00Z"},
                    {"source_message_id": "src_monitor_2", "submitted_at": "2026-06-16T03:29:00Z"},
                ],
            }
        )
        self.s3_client.objects[("bucket", "messages/src_monitor_2/manifest.json")] = _encoded(
            {
                "state": "approved",
                "current_approval_key": "messages/src_monitor_2/approval_r1.json",
                "current_draft_key": "messages/src_monitor_2/draft_r1.json",
                "created_at": "2026-06-16T03:29:00Z",
                "updated_at": "2099-06-16T03:30:00Z",
            }
        )
        self.s3_client.objects[("bucket", "messages/src_monitor_2/approval_r1.json")] = _encoded(
            {"state": "approved", "responded_at": "2099-06-16T03:30:00Z"}
        )
        self.s3_client.objects[("bucket", "messages/src_monitor_2/draft_r1.json")] = _encoded(
            {
                "area_id": "1",
                "candidate_scores": [
                    {"candidate_number": 1, "score": 5},
                    {"candidate_number": 2, "score": 7},
                ],
                "eligible_voters": 20,
                "voter_turnout": 12,
                "valid_ballots": 10,
                "invalid_ballots": 1,
                "abstained_ballots": 1,
            }
        )

        self.client.patch("/api/v1/monitor/data-mode", json={"mode": "incremental_delta"})
        results_app.invalidate_result_caches()
        self.assertEqual(results_app.store.list_area_indexes(results_app.settings.source_election_id), ["1", "2"])
        self.assertEqual(len(results_app.store.approved_results_for_area(results_app.settings.source_election_id, "1")), 2)
        interpreted = results_app.interpreted_public_results("incremental_delta")
        interpreted_scores = {
            score["candidate_number"]: score["score"]
            for score in interpreted[0]["candidate_scores"]
        }

        self.assertEqual(interpreted_scores, {1: 75, 2: 37})
        summary = self.client.get("/api/v1/governor-results/summary").json()

        candidates = {candidate["candidateNumber"]: candidate["voteCount"] for candidate in summary["candidates"]}
        self.assertEqual(candidates, {1: 75, 2: 37})
        self.assertEqual(summary["summary"]["voterTurnout"], 112)
        self.assertEqual(summary["dataInterpretation"]["mode"], "incremental_delta")

    def test_latest_snapshot_mode_uses_latest_approved_result_in_public_summary(self):
        self.s3_client.objects[("bucket", "indexes/by-area/default/1/submissions.json")] = _encoded(
            {
                "submission_count": 2,
                "updated_at": "2026-06-16T03:29:00Z",
                "submissions": [
                    {"source_message_id": "src_monitor_1", "submitted_at": "2026-06-16T02:59:00Z"},
                    {"source_message_id": "src_monitor_2", "submitted_at": "2026-06-16T03:29:00Z"},
                ],
            }
        )
        self.s3_client.objects[("bucket", "messages/src_monitor_2/manifest.json")] = _encoded(
            {
                "state": "approved",
                "current_approval_key": "messages/src_monitor_2/approval_r1.json",
                "current_draft_key": "messages/src_monitor_2/draft_r1.json",
                "created_at": "2026-06-16T03:29:00Z",
                "updated_at": "2099-06-16T03:30:00Z",
            }
        )
        self.s3_client.objects[("bucket", "messages/src_monitor_2/approval_r1.json")] = _encoded(
            {"state": "approved", "responded_at": "2099-06-16T03:30:00Z"}
        )
        self.s3_client.objects[("bucket", "messages/src_monitor_2/draft_r1.json")] = _encoded(
            {
                "area_id": "1",
                "candidate_scores": [],
                "eligible_voters": 200,
                "voter_turnout": 155,
                "valid_ballots": 105,
                "invalid_ballots": 50,
                "abstained_ballots": 0,
                "validation_flags": ["missing_candidate_scores", "no_candidate_scores_found"],
            }
        )

        results_app.invalidate_result_caches()
        self.assertEqual(results_app.store.list_area_indexes(results_app.settings.source_election_id), ["1", "2"])
        self.assertEqual(len(results_app.store.approved_results_for_area(results_app.settings.source_election_id, "1")), 2)
        interpreted = results_app.interpreted_public_results("latest_snapshot")
        self.assertEqual(interpreted[0]["candidate_scores"], [])
        summary = self.client.get("/api/v1/governor-results/summary").json()
        candidates = {candidate["candidateNumber"]: candidate["voteCount"] for candidate in summary["candidates"]}

        self.assertEqual(candidates, {})

        detail = self.client.get("/api/v1/monitor/districts/1").json()

        self.assertEqual(detail["status"], "missing_fields")
        self.assertEqual(detail["approvedSubmissionCount"], 2)
        self.assertEqual(detail["latestApprovedAt"], "2099-06-16T03:30:00Z")
        self.assertEqual(detail["latestApprovedResult"]["candidate_scores"], [])
        self.assertEqual(detail["latestApprovedResult"]["voter_turnout"], 155)

    def test_round_update_overrides_single_source_round_without_mutating_raw_result(self):
        response = self.client.patch(
            "/api/v1/monitor/districts/1/rounds/source:src_monitor_1",
            json={
                "candidateScores": [{"candidateNumber": 1, "score": 9}, {"candidateNumber": 2, "score": 91}],
                "actor": "tester",
            },
        )

        self.assertEqual(response.status_code, 200)
        summary = self.client.get("/api/v1/governor-results/summary").json()
        candidates = {candidate["candidateNumber"]: candidate["voteCount"] for candidate in summary["candidates"]}
        self.assertEqual(candidates, {1: 9, 2: 91})

        raw_draft = json.loads(self.s3_client.objects[("bucket", "messages/src_monitor_1/draft_r1.json")].decode("utf-8"))
        self.assertEqual(raw_draft["candidate_scores"][0]["score"], 70)

    def test_round_create_adds_manual_round_for_incremental_mode(self):
        self.client.patch("/api/v1/monitor/data-mode", json={"mode": "incremental_delta"})

        response = self.client.post(
            "/api/v1/monitor/districts/1/rounds",
            json={
                "roundId": "manual:test-round",
                "position": 1500,
                "reportedAt": "2099-06-16T03:15:00Z",
                "candidateScores": [{"candidateNumber": 1, "score": 5}, {"candidateNumber": 2, "score": 7}],
                "voterTurnout": 12,
                "validBallots": 10,
                "invalidBallots": 1,
                "abstainedBallots": 1,
                "actor": "tester",
            },
        )

        self.assertEqual(response.status_code, 200)
        summary = self.client.get("/api/v1/governor-results/summary").json()
        candidates = {candidate["candidateNumber"]: candidate["voteCount"] for candidate in summary["candidates"]}
        self.assertEqual(candidates, {1: 75, 2: 37})

        detail = self.client.get("/api/v1/monitor/districts/1").json()
        self.assertTrue(any(item["roundId"] == "manual:test-round" for item in detail["rounds"]))

    def test_round_delete_removes_round_from_effective_results(self):
        response = self.client.request(
            "DELETE",
            "/api/v1/monitor/districts/1/rounds/source:src_monitor_1",
            json={"actor": "tester", "reason": "bad photo"},
        )

        self.assertEqual(response.status_code, 200)
        detail = self.client.get("/api/v1/monitor/districts/1").json()
        self.assertEqual(detail["status"], "pending")
        self.assertTrue(any(item["roundId"] == "source:src_monitor_1" and item["deleted"] for item in detail["rounds"]))

    def test_summary_override_rejects_invalid_numeric_values(self):
        response = self.client.patch(
            "/api/v1/monitor/districts/1/summary",
            json={"eligibleVoters": -1},
        )

        self.assertEqual(response.status_code, 400)

        response = self.client.patch(
            "/api/v1/monitor/districts/1/summary",
            json={"eligibleVoters": 1.5},
        )

        self.assertEqual(response.status_code, 400)

    def test_summary_override_rejects_invalid_turnout_consistency(self):
        response = self.client.patch(
            "/api/v1/monitor/districts/1/summary",
            json={
                "voterTurnout": 10,
                "validBallots": 8,
                "invalidBallots": 1,
                "abstainedBallots": 0,
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_monitor_overrides_endpoint_returns_current_overrides(self):
        self.client.patch("/api/v1/monitor/page-meta", json={"title": "Override title"})
        self.client.patch("/api/v1/monitor/districts/1/summary", json={"eligibleVoters": 321})

        response = self.client.get("/api/v1/monitor/overrides")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["pageMeta"]["title"], "Override title")
        self.assertEqual(payload["districtSummaries"][0]["eligibleVoters"], 321)

    def test_global_summary_override_endpoint_is_rejected(self):
        response = self.client.patch("/api/v1/monitor/summary", json={"eligibleVoters": 321})

        self.assertEqual(response.status_code, 400)
        self.assertIn("per district", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
