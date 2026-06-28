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
    build_district_results_from_external_payload,
    build_governor_results_from_external_payload,
    build_governor_results,
    interpreted_area_result,
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

    def test_reads_absolute_json_without_prefix(self):
        self.client.objects[("bucket", "api-data/governor-results/sumary.json")] = _encoded({"ok": True})

        payload = self.store.read_absolute_json("api-data/governor-results/sumary.json")

        self.assertEqual(payload, {"ok": True})

    def test_reads_maybe_absolute_json_with_existing_prefix(self):
        self.client.objects[("bucket", "api-data/score/messages/src_1/draft_r1.json")] = _encoded({"ok": True})
        prefixed_store = ResultsStore(s3_client=self.client, bucket="bucket", prefix="api-data/score")

        payload = prefixed_store.read_maybe_absolute_json("api-data/score/messages/src_1/draft_r1.json")

        self.assertEqual(payload, {"ok": True})

    def test_approved_result_reads_absolute_manifest_keys(self):
        source_id = "src_abs"
        self.client.objects[("bucket", "api-data/score/indexes/by-area/default/7/submissions.json")] = _encoded(
            {
                "submission_count": 1,
                "submissions": [{"source_message_id": source_id}],
            }
        )
        self.client.objects[("bucket", f"api-data/score/messages/{source_id}/manifest.json")] = _encoded(
            {
                "state": "approved",
                "current_approval_key": f"api-data/score/messages/{source_id}/approval_r1.json",
                "current_draft_key": f"api-data/score/messages/{source_id}/draft_r1.json",
                "created_at": "2026-06-22T10:23:37Z",
            }
        )
        self.client.objects[("bucket", f"api-data/score/messages/{source_id}/approval_r1.json")] = _encoded(
            {"state": "approved", "responded_at": "2026-06-22T10:24:01Z"}
        )
        self.client.objects[("bucket", f"api-data/score/messages/{source_id}/draft_r1.json")] = _encoded(
            {
                "area_id": "7",
                "candidate_scores": [{"candidate_number": 1, "score": 100}],
            }
        )
        prefixed_store = ResultsStore(s3_client=self.client, bucket="bucket", prefix="api-data/score")

        result = prefixed_store.approved_result(source_id)

        self.assertEqual(result["area_id"], "7")
        self.assertEqual(result["candidate_scores"][0]["score"], 100)


class DistrictCatalogTests(unittest.TestCase):
    def test_normalizes_top_level_election_areas_payload(self):
        catalog = results_app.DistrictCatalog(url="https://example.test/districts.json")
        catalog._read_json_url = lambda: {
            "schemaVersion": "1.0",
            "resource": "election-areas-bangkok",
            "electionAreas": [
                {
                    "id": "26b4aad6-94b3-490a-9390-71636d5e97a4",
                    "number": 1,
                    "name": "เธเธฃเธฐเธเธเธฃ",
                }
            ],
        }

        districts = catalog.districts()

        self.assertEqual(len(districts), 1)
        self.assertEqual(districts[0]["id"], 1)
        self.assertEqual(districts[0]["districtNameTh"], "เธเธฃเธฐเธเธเธฃ")
        self.assertEqual(districts[0]["electionAreaId"], "26b4aad6-94b3-490a-9390-71636d5e97a4")

    def test_districts_by_id_maps_number_and_uuid(self):
        catalog = results_app.DistrictCatalog(url="https://example.test/districts.json")
        catalog.districts = lambda: [
            {
                "id": 1,
                "provinceCode": 10,
                "districtCode": 1,
                "districtNameTh": "เธเธฃเธฐเธเธเธฃ",
                "electionAreaId": "26b4aad6-94b3-490a-9390-71636d5e97a4",
                "areaNumber": 1,
            }
        ]

        mapped = catalog.districts_by_id()

        self.assertEqual(mapped["1"]["districtNameTh"], "เธเธฃเธฐเธเธเธฃ")
        self.assertEqual(mapped["26b4aad6-94b3-490a-9390-71636d5e97a4"]["districtNameTh"], "เธเธฃเธฐเธเธเธฃ")

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
                1: {
                    "candidateId": "one",
                    "name": "Candidate One",
                    "color": "#111111",
                    "candidateSrc": "https://example.test/one.png",
                    "backgroundSrc": "https://example.test/one-bg.png",
                    "party": {
                        "id": "party-one",
                        "name": "Party One",
                        "color": "#010101",
                        "logoUrl": "https://example.test/party-one.png",
                    },
                },
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
        self.assertIsNone(payload["summary"]["countedBallots"])
        self.assertIsNone(payload["summary"]["countedBallotsPercentage"])
        self.assertIsNone(payload["summary"]["validBallotsPercentage"])
        self.assertIsNone(payload["summary"]["invalidBallotsPercentage"])
        self.assertIsNone(payload["summary"]["abstainedBallotsPercentage"])
        self.assertEqual(payload["candidates"][0]["candidateNumber"], 1)
        self.assertEqual(payload["candidates"][0]["voteCount"], 150)
        self.assertEqual(payload["candidates"][0]["votePercentage"], 50.0)
        self.assertEqual(payload["candidates"][0]["rank"], 1)
        self.assertTrue(payload["candidates"][0]["isLeading"])
        self.assertEqual(payload["candidates"][0]["name"], "Candidate One")
        self.assertEqual(payload["candidates"][0]["candidateId"], "one")
        self.assertEqual(payload["candidates"][0]["color"], "#111111")
        self.assertEqual(payload["candidates"][0]["candidateSrc"], "https://example.test/one.png")
        self.assertEqual(payload["candidates"][0]["backgroundSrc"], "https://example.test/one-bg.png")
        self.assertEqual(
            payload["candidates"][0]["party"],
            {
                "id": "party-one",
                "name": "Party One",
                "color": "#010101",
                "logoUrl": "https://example.test/party-one.png",
            },
        )
        self.assertNotIn("sources", payload)
        self.assertFalse(payload["dataQuality"]["isComplete"])
        self.assertFalse(payload["dataQuality"]["isDelayed"])
        self.assertTrue(payload["dataQuality"]["warnings"])

    def test_district_results_deduplicates_catalog_aliases(self):
        payload = build_district_results(
            approved_results=[
                {
                    "area_id": "1",
                    "candidate_scores": [{"candidate_number": 1, "score": 100}],
                }
            ],
            candidate_catalog={1: {"candidateId": "one", "name": "One", "color": "#111111"}},
            district_catalog={
                "1": {
                    "id": 1,
                    "provinceCode": 10,
                    "districtCode": 1,
                    "districtNameTh": "เธเธฃเธฐเธเธเธฃ",
                    "areaNumber": 1,
                    "electionAreaId": "uuid-1",
                },
                "uuid-1": {
                    "id": 1,
                    "provinceCode": 10,
                    "districtCode": 1,
                    "districtNameTh": "เธเธฃเธฐเธเธเธฃ",
                    "areaNumber": 1,
                    "electionAreaId": "uuid-1",
                },
            },
        )

        constituencies = payload["constituencies"]
        self.assertEqual(len(constituencies), 1)
        self.assertEqual(constituencies[0]["areaId"], "uuid-1")
        self.assertEqual(constituencies[0]["name"], "เธเธฃเธฐเธเธเธฃ")

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
        self.assertIsNone(candidate["candidateSrc"])
        self.assertIsNone(candidate["backgroundSrc"])
        self.assertEqual(
            candidate["party"],
            {"id": None, "name": None, "color": None, "logoUrl": None},
        )
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
        self.assertEqual(payload["summary"]["countedBallots"], 180)
        self.assertEqual(payload["summary"]["countedBallotsPercentage"], 100.0)
        self.assertEqual(payload["summary"]["validBallotsPercentage"], 91.67)
        self.assertEqual(payload["summary"]["invalidBallotsPercentage"], 4.44)
        self.assertEqual(payload["summary"]["abstainedBallotsPercentage"], 3.89)
        self.assertTrue(payload["dataQuality"]["isComplete"])
        self.assertFalse(payload["dataQuality"]["isDelayed"])

    def test_governor_results_partially_aggregates_turnout_fields(self):
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
                },
            ],
            total_units=2,
            generated_at="2026-06-15T01:03:00Z",
        )

        self.assertEqual(payload["summary"]["eligibleVoters"], 100)
        self.assertEqual(payload["summary"]["voterTurnout"], 80)
        self.assertEqual(payload["summary"]["voterTurnoutPercentage"], 80.0)
        self.assertEqual(payload["summary"]["validBallots"], 75)
        self.assertEqual(payload["summary"]["invalidBallots"], 3)
        self.assertEqual(payload["summary"]["abstainedBallots"], 2)
        self.assertEqual(payload["summary"]["countedBallots"], 80)
        self.assertEqual(payload["summary"]["countedBallotsPercentage"], 100.0)
        self.assertNotIn("validBallots is unavailable in approved results.", payload["dataQuality"]["warnings"])

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
                        "backgroundSrc": "https://example.test/one-bg.png",
                        "party": {
                            "id": "party-one",
                            "name": "Party One",
                            "color": "#010101",
                            "logoUrl": "https://example.test/party-one.png",
                        },
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
                "backgroundSrc": "https://example.test/one-bg.png",
                "party": {
                    "id": "party-one",
                    "name": "Party One",
                    "color": "#010101",
                    "logoUrl": "https://example.test/party-one.png",
                },
            },
        )

    def test_candidate_catalog_resolves_party_from_master_by_party_id(self):
        catalog = CandidateCatalog(
            manifest_url="https://example.test/manifest.json",
            featured_url="https://example.test/featured.json",
            parties_url="https://example.test/parties.json",
            cache_seconds=300,
        )
        payloads = {
            catalog.manifest_url: {
                "profiles": [
                    {
                        "id": "five",
                        "candidateNumber": 5,
                        "name": "Candidate Five",
                        "partyId": "democrat-party",
                    },
                ]
            },
            catalog.featured_url: {
                "candidates": [
                    {
                        "id": "five",
                        "candidateNumber": 5,
                        "themeColor": "#123456",
                    },
                ]
            },
            catalog.parties_url: [
                {
                    "id": "democrat-party",
                    "name": "Party Five",
                    "color": "#010101",
                    "logoUrl": "https://example.test/party-five.png",
                },
            ],
        }
        catalog._read_json_url = payloads.__getitem__
        catalog._read_json_value = payloads.__getitem__

        candidates = catalog.candidates_by_number()

        self.assertEqual(
            candidates[5]["party"],
            {
                "id": "democrat-party",
                "name": "Party Five",
                "color": "#010101",
                "logoUrl": "https://example.test/party-five.png",
            },
        )

    def test_candidate_catalog_resolves_party_from_master_by_party_name(self):
        catalog = CandidateCatalog(
            manifest_url="https://example.test/manifest.json",
            featured_url="https://example.test/featured.json",
            parties_url="https://example.test/parties.json",
            cache_seconds=300,
        )
        payloads = {
            catalog.manifest_url: {
                "profiles": [
                    {
                        "id": "one",
                        "candidateNumber": 1,
                        "name": "Candidate One",
                        "partyName": "เธญเธดเธชเธฃเธฐ",
                    },
                ]
            },
            catalog.featured_url: {"candidates": [{"id": "one", "candidateNumber": 1}]},
            catalog.parties_url: [
                {
                    "id": "independent",
                    "name": "เธญเธดเธชเธฃเธฐ",
                    "color": "#6B7280",
                    "logoUrl": None,
                },
            ],
        }
        catalog._read_json_url = payloads.__getitem__
        catalog._read_json_value = payloads.__getitem__

        candidates = catalog.candidates_by_number()

        self.assertEqual(
            candidates[1]["party"],
            {
                "id": "independent",
                "name": "เธญเธดเธชเธฃเธฐ",
                "color": "#6B7280",
                "logoUrl": None,
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
                "districtNameTh": "เธซเธเธญเธเธเธญเธ",
            }
        ]

        self.assertEqual(catalog.districts_by_id()["3"]["districtNameTh"], "เธซเธเธญเธเธเธญเธ")

    def test_district_catalog_supports_election_areas_payload(self):
        catalog = DistrictCatalog(url="s3://bucket/api-data/master-data/election-areas-bangkok.json")
        catalog._read_json_url = lambda: {
            "schemaVersion": "1.0",
            "resource": "election-areas-bangkok",
            "data": {
                "electionAreas": [
                    {
                        "id": "uuid-area-1",
                        "number": 1,
                        "name": "เธเธฃเธฐเธเธเธฃ",
                    }
                ]
            },
        }

        district = catalog.districts_by_id()["1"]

        self.assertEqual(district["districtNameTh"], "เธเธฃเธฐเธเธเธฃ")
        self.assertEqual(district["provinceCode"], 10)
        self.assertEqual(district["electionAreaId"], "uuid-area-1")

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
                    "districtNameTh": "เธซเธเธญเธเธเธญเธ",
                },
                "4": {
                    "id": 4,
                    "provinceCode": 10,
                    "districtCode": 1004,
                    "districtNameTh": "เธเธฒเธเธฃเธฑเธ",
                }
            },
            generated_at="2026-06-15T01:03:00.000Z",
        )

        constituency = payload["constituencies"][0]
        self.assertEqual(constituency["areaId"], "3")
        self.assertEqual(constituency["number"], 3)
        self.assertEqual(constituency["name"], "เธซเธเธญเธเธเธญเธ")
        self.assertEqual(constituency["leadingCandidateId"], "one")
        self.assertEqual(constituency["sumaryVoteCount"], 200)
        self.assertNotIn("countedBallots", constituency)
        self.assertNotIn("countedBallotsPercentage", constituency)
        self.assertEqual(constituency["candidates"][0]["votePercentage"], 60.0)
        self.assertEqual(len(payload["constituencies"]), 2)
        empty_constituency = payload["constituencies"][1]
        self.assertEqual(empty_constituency["areaId"], "4")
        self.assertIsNone(empty_constituency["leadingCandidateId"])
        self.assertEqual(empty_constituency["candidates"], [])
        self.assertNotIn("sumaryVoteCount", empty_constituency)

    def test_build_district_results_prefers_election_area_id_and_candidate_assets(self):
        payload = build_district_results(
            approved_results=[
                {
                    "area_id": "3",
                    "approved_at": "2026-06-15T01:02:00Z",
                    "candidate_scores": [{"candidate_number": 1, "score": 120}],
                    "eligible_voters": 200,
                    "voter_turnout": 150,
                    "valid_ballots": 140,
                    "invalid_ballots": 7,
                    "abstained_ballots": 3,
                }
            ],
            candidate_catalog={
                1: {
                    "candidateId": "one",
                    "name": "Candidate One",
                    "color": "#111111",
                    "candidateSrc": "https://example.test/one.png",
                    "backgroundSrc": "https://example.test/one-bg.png",
                    "party": {
                        "id": "party-one",
                        "name": "Party One",
                        "color": "#101010",
                        "logoUrl": "https://example.test/party-one.png",
                    },
                }
            },
            district_catalog={
                "3": {
                    "id": 3,
                    "provinceCode": 10,
                    "districtCode": 1003,
                    "districtNameTh": "District Three TH",
                    "electionAreaId": "uuid-area-3",
                }
            },
            generated_at="2026-06-15T01:03:00.000Z",
        )

        constituency = payload["constituencies"][0]
        self.assertEqual(constituency["areaId"], "uuid-area-3")
        self.assertEqual(constituency["countedPercentage"], 100.0)
        self.assertEqual(constituency["sumaryVoteCount"], 120)
        self.assertEqual(constituency["countedBallots"], 150)
        self.assertEqual(constituency["countedBallotsPercentage"], 100.0)
        self.assertEqual(constituency["lastUpdatedAt"], "2026-06-15T01:02:00Z")
        self.assertNotIn("% บัตรเสีย", constituency)
        self.assertEqual(
            constituency["candidates"][0]["backgroundSrc"],
            "https://example.test/one-bg.png",
        )
        self.assertEqual(
            constituency["candidates"][0]["party"],
            {
                "id": "party-one",
                "name": "Party One",
                "color": "#101010",
                "logoUrl": "https://example.test/party-one.png",
            },
        )

    def test_build_district_results_excludes_non_bangkok_districts(self):
        payload = build_district_results(
            approved_results=[],
            candidate_catalog={},
            district_catalog={
                "1": {"id": 1, "provinceCode": 10, "districtNameTh": "เธเธฃเธฐเธเธเธฃ"},
                "51": {"id": 51, "provinceCode": 11, "districtNameTh": "เน€เธกเธทเธญเธเธชเธกเธธเธ—เธฃเธเธฃเธฒเธเธฒเธฃ"},
            },
            generated_at="2026-06-15T01:03:00.000Z",
        )

        constituencies = payload["constituencies"]
        self.assertEqual(len(constituencies), 1)
        self.assertEqual(constituencies[0]["name"], "เธเธฃเธฐเธเธเธฃ")

    def test_build_governor_results_from_external_payload_uses_raw_totals_and_catalog(self):
        raw_payload = {
            "type": "LIVE",
            "total": {
                "eligiblePopulation": 3500,
                "totalVotes": 3200,
                "badVotes": 10,
                "noVotes": 200,
                "goodVote": 2990,
                "progress": 2.0,
                "pollingUnits": {"total": 50, "reported": 1},
                "result": [
                    {"candidateId": "18", "count": 300},
                    {"candidateId": "17", "count": 290},
                ],
            },
            "districts": [],
            "lastUpdatedAt": "2026-06-23T07:38:12Z",
        }

        payload = build_governor_results_from_external_payload(
            raw_payload=raw_payload,
            candidate_catalog={
                18: {"candidateId": "somchai", "name": "Somchai", "color": "#7c200a"},
                17: {"candidateId": "lallana", "name": "Lallana", "color": "#166500"},
            },
            election_id="bkk-governor-2026",
            title="Governor",
            generated_at="2026-06-23T07:39:15.221Z",
        )

        self.assertEqual(payload["pageMeta"]["resultStatus"], "LIVE_COUNT")
        self.assertEqual(payload["summary"]["countedUnits"], 1)
        self.assertEqual(payload["summary"]["totalUnits"], 50)
        self.assertEqual(payload["summary"]["validBallots"], 2990)
        self.assertEqual(payload["candidates"][0]["candidateId"], "somchai")
        self.assertEqual(payload["candidates"][0]["votePercentage"], 10.03)
        self.assertEqual(payload["dataInterpretation"]["mode"], "external_snapshot")

    def test_build_district_results_from_external_payload_maps_district_names_to_master(self):
        raw_payload = {
            "type": "LIVE",
            "total": {},
            "districts": [
                {
                    "name": "เธ”เธธเธชเธดเธ•",
                    "voting": {
                        "eligiblePopulation": 3500,
                        "totalVotes": 3200,
                        "badVotes": 10,
                        "noVotes": 200,
                        "goodVote": 2990,
                        "progress": 100.0,
                        "result": [
                            {"candidateId": "18", "count": 300},
                            {"candidateId": "17", "count": 290},
                        ],
                    },
                }
            ],
            "lastUpdatedAt": "2026-06-23T07:38:12Z",
        }

        payload = build_district_results_from_external_payload(
            raw_payload=raw_payload,
            candidate_catalog={
                18: {"candidateId": "somchai", "name": "Somchai", "color": "#7c200a"},
                17: {"candidateId": "lallana", "name": "Lallana", "color": "#166500"},
            },
            district_catalog={
                "1": {"id": 1, "provinceCode": 10, "districtNameTh": "เธเธฃเธฐเธเธเธฃ", "electionAreaId": "area-1"},
                "2": {"id": 2, "provinceCode": 10, "districtNameTh": "เธ”เธธเธชเธดเธ•", "electionAreaId": "area-2"},
            },
            generated_at="2026-06-23T07:39:15.479Z",
        )

        self.assertEqual(len(payload["constituencies"]), 2)
        self.assertEqual(payload["constituencies"][0]["areaId"], "area-1")
        self.assertEqual(payload["constituencies"][0]["candidates"], [])
        self.assertEqual(payload["constituencies"][1]["areaId"], "area-2")
        self.assertEqual(payload["constituencies"][1]["leadingCandidateId"], "somchai")
        self.assertEqual(payload["constituencies"][1]["sumaryVoteCount"], 590)
        self.assertEqual(payload["constituencies"][1]["candidates"][0]["rank"], 1)


class ResultsApiTests(unittest.TestCase):
    def setUp(self):
        self.original_store = results_app.store
        self.original_district_catalog = results_app.district_catalog
        self.original_candidate_catalog = results_app.candidate_catalog
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
        results_app._governor_results_cache_payload = None
        results_app._governor_results_cache_at = 0.0

    def test_monitor_page_is_served(self):
        response = self.client.get("/monitor")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Governor Results Monitor", response.text)
        self.assertIn("/api/v1/monitor/source", response.text)
        self.assertIn('id="startScheduleButton"', response.text)
        self.assertIn('id="stopScheduleButton"', response.text)
        self.assertIn('id="resumeScheduleButton"', response.text)
        self.assertIn('id="countdownText"', response.text)
        self.assertIn('id="activePublicSourceLine"', response.text)
        self.assertIn('id="activePublicSourceBkk"', response.text)
        self.assertIn('id="rawExportPrefix"', response.text)
        self.assertIn('id="bmcEnabled"', response.text)
        self.assertIn('id="bmcUrl"', response.text)
        self.assertIn('id="sorkorExportPrefix"', response.text)

    def _fake_external_governor_source(self):
        original_read_json_source = results_app.read_json_source
        self.addCleanup(setattr, results_app, "read_json_source", original_read_json_source)

        def fake_read_json_source(*, source, timeout_seconds, s3_client=None):
            if source == "https://example.test/external-governor.json":
                return {
                    "type": "LIVE",
                    "total": {
                        "eligiblePopulation": 3500,
                        "totalVotes": 3200,
                        "badVotes": 10,
                        "noVotes": 200,
                        "goodVote": 2990,
                        "progress": 2.0,
                        "pollingUnits": {"total": 50, "reported": 1},
                        "result": [{"candidateId": "1", "count": 300}],
                    },
                    "districts": [],
                    "lastUpdatedAt": "2026-06-23T07:38:12Z",
                }
            return original_read_json_source(source=source, timeout_seconds=timeout_seconds, s3_client=s3_client)

        results_app.read_json_source = fake_read_json_source

    def _fake_external_bmc_source(self):
        original_read_json_source = results_app.read_json_source
        self.addCleanup(setattr, results_app, "read_json_source", original_read_json_source)

        bmc_payload = {
            "type": "LIVE",
            "total": {
                "eligiblePopulation": 4384712,
                "totalVotes": 2040,
                "badVotes": 72,
                "noVotes": 0,
                "goodVote": 1968,
                "progress": 6.45,
                "pollingUnits": {"total": 6628, "reported": 4, "cap": 58},
                "result": [],
            },
            "districts": [
                {
                    "name": "District One TH",
                    "voting": {
                        "eligiblePopulation": 30980,
                        "totalVotes": 2040,
                        "badVotes": 72,
                        "noVotes": 0,
                        "goodVote": 1968,
                        "progress": 6.45,
                        "result": [
                            {"candidateId": "District One TH-1", "count": 1174},
                            {"candidateId": "District One TH-3", "count": 421},
                        ],
                        "pollingUnits": {"total": 62, "reported": 4, "cap": 58},
                    },
                }
            ],
            "lastUpdatedAt": "2026-06-23T07:38:12Z",
        }

        def fake_read_json_source(*, source, timeout_seconds, s3_client=None):
            if source == "https://example.test/external-bmc.json":
                return bmc_payload
            return original_read_json_source(source=source, timeout_seconds=timeout_seconds, s3_client=s3_client)

        def combined_fake(*, source, timeout_seconds, s3_client=None):
            if source == "https://example.test/external-bmc.json":
                return bmc_payload
            if source == "https://example.test/external-governor.json":
                return {
                    "type": "LIVE",
                    "total": {
                        "eligiblePopulation": 3500,
                        "totalVotes": 3200,
                        "badVotes": 10,
                        "noVotes": 200,
                        "goodVote": 2990,
                        "progress": 2.0,
                        "pollingUnits": {"total": 50, "reported": 1},
                        "result": [{"candidateId": "1", "count": 300}],
                    },
                    "districts": [],
                    "lastUpdatedAt": "2026-06-23T07:38:12Z",
                }
            return original_read_json_source(source=source, timeout_seconds=timeout_seconds, s3_client=s3_client)

        results_app.read_json_source = combined_fake

    def test_monitor_fetch_exports_sorkor_to_bkk_prefix(self):
        self._fake_external_bmc_source()
        self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": False,
                "url": "",
                "timeoutSeconds": 5,
                "bmcEnabled": True,
                "bmcUrl": "https://example.test/external-bmc.json",
                "bmcTimeoutSeconds": 5,
            },
        )
        response = self.client.post("/api/v1/monitor/source/fetch", json={})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["bmcFetch"]["status"], "success")
        self.assertEqual(payload["governorFetch"]["status"], "skipped")
        self.assertEqual(
            payload["sorkorExport"]["summaryKey"],
            "api-data/governor-results-bkk/sumary-sorkor.json",
        )
        self.assertIn(("bucket", "api-data/governor-results-bkk/sumary-sorkor.json"), self.s3_client.objects)
        self.assertIn(("bucket", "api-data/governor-results-bkk/districts-sorkor.json"), self.s3_client.objects)
        self.assertNotIn(("bucket", "api-data/governor-results/sumary-sorkor.json"), self.s3_client.objects)
        summary = json.loads(self.s3_client.objects[("bucket", "api-data/governor-results-bkk/sumary-sorkor.json")])
        self.assertEqual(summary["resource"], "sorkor-results")

    def test_monitor_fetch_promotes_sorkor_when_active_bkk(self):
        self._fake_external_bmc_source()
        self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": True,
                "url": "https://example.test/external-governor.json",
                "timeoutSeconds": 5,
                "bmcEnabled": True,
                "bmcUrl": "https://example.test/external-bmc.json",
                "bmcTimeoutSeconds": 5,
                "activePublicSource": "bkk",
            },
        )
        response = self.client.post("/api/v1/monitor/source/fetch", json={})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsNotNone(payload.get("publicPromote"))
        self.assertIn(("bucket", "api-data/governor-results/sumary-sorkor.json"), self.s3_client.objects)
        self.assertIn(("bucket", "api-data/governor-results/districts-sorkor.json"), self.s3_client.objects)

    def test_monitor_fetch_dual_governor_and_bmc(self):
        self._fake_external_bmc_source()
        self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": True,
                "url": "https://example.test/external-governor.json",
                "timeoutSeconds": 5,
                "bmcEnabled": True,
                "bmcUrl": "https://example.test/external-bmc.json",
                "bmcTimeoutSeconds": 5,
            },
        )
        response = self.client.post("/api/v1/monitor/source/fetch", json={})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["governorFetch"]["status"], "success")
        self.assertEqual(payload["bmcFetch"]["status"], "success")
        self.assertIn(("bucket", "api-data/governor-results-bkk/sumary.json"), self.s3_client.objects)
        self.assertIn(("bucket", "api-data/governor-results-bkk/sumary-sorkor.json"), self.s3_client.objects)

    def test_monitor_bmc_failure_does_not_block_governor(self):
        self._fake_external_governor_source()
        self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": True,
                "url": "https://example.test/external-governor.json",
                "timeoutSeconds": 5,
                "bmcEnabled": True,
                "bmcUrl": "https://example.test/missing-bmc.json",
                "bmcTimeoutSeconds": 5,
            },
        )
        response = self.client.post("/api/v1/monitor/source/fetch", json={})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "partial_success")
        self.assertEqual(payload["governorFetch"]["status"], "success")
        self.assertEqual(payload["bmcFetch"]["status"], "error")
        self.assertIn(("bucket", "api-data/governor-results-bkk/sumary.json"), self.s3_client.objects)

    def test_active_source_switch_promotes_to_live(self):
        self._fake_external_governor_source()
        self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": True,
                "url": "https://example.test/external-governor.json",
                "timeoutSeconds": 5,
            },
        )
        fetch_response = self.client.post("/api/v1/monitor/source/fetch", json={})
        self.assertEqual(fetch_response.status_code, 200)

        switch_response = self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": True,
                "url": "https://example.test/external-governor.json",
                "timeoutSeconds": 5,
                "activePublicSource": "bkk",
            },
        )
        self.assertEqual(switch_response.status_code, 200)
        self.assertIn("publicPromote", switch_response.json())
        self.assertIn(("bucket", "api-data/governor-results/sumary.json"), self.s3_client.objects)
        self.assertIn(("bucket", "api-data/governor-results/districts.json"), self.s3_client.objects)

    def test_monitor_fetch_writes_bkk_and_promotes_when_active_bkk(self):
        self._fake_external_governor_source()
        self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": True,
                "url": "https://example.test/external-governor.json",
                "timeoutSeconds": 5,
            },
        )
        from hermes.governor_results.public_source import write_active_public_source

        write_active_public_source(
            s3_client=results_app.store.s3_client,
            bucket=results_app.store.bucket,
            score_prefix=results_app.settings.prefix,
            source="bkk",
        )
        response = self.client.post("/api/v1/monitor/source/fetch", json={})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["staticExport"]["summaryKey"], "api-data/governor-results-bkk/sumary.json")
        self.assertIsNotNone(payload.get("publicPromote"))
        self.assertIn(("bucket", "api-data/governor-results/sumary.json"), self.s3_client.objects)

    def test_monitor_fetch_writes_bkk_only_when_active_line(self):
        self._fake_external_governor_source()
        self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": True,
                "url": "https://example.test/external-governor.json",
                "timeoutSeconds": 5,
            },
        )
        response = self.client.post("/api/v1/monitor/source/fetch", json={})
        self.assertEqual(response.status_code, 200)
        self.assertIn(("bucket", "api-data/governor-results-bkk/sumary.json"), self.s3_client.objects)
        self.assertNotIn(("bucket", "api-data/governor-results/sumary.json"), self.s3_client.objects)

    def test_monitor_save_preserves_active_public_source(self):
        self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": True,
                "url": "https://example.test/external-governor.json",
                "timeoutSeconds": 5,
                "activePublicSource": "bkk",
            },
        )

        save_response = self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": True,
                "url": "https://example.test/external-governor.json",
                "timeoutSeconds": 8,
            },
        )

        self.assertEqual(save_response.status_code, 200)
        self.assertEqual(save_response.json()["activePublicSource"]["source"], "bkk")

    def test_monitor_schedule_start_stop_resume(self):
        source_payload = {
            "enabled": True,
            "url": "https://example.test/external-governor.json",
            "timeoutSeconds": 5,
        }

        start_response = self.client.put(
            "/api/v1/monitor/source",
            json={
                **source_payload,
                "scheduleIntervalSeconds": 60,
                "scheduleAction": "start",
            },
        )
        self.assertEqual(start_response.status_code, 200)
        start_schedule = start_response.json()["schedule"]
        self.assertTrue(start_schedule["enabled"])
        self.assertEqual(start_schedule["status"], "running")
        self.assertIsNotNone(start_schedule["nextRunAt"])
        self.assertGreaterEqual(start_schedule["remainingSeconds"], 59)

        stop_response = self.client.put(
            "/api/v1/monitor/source",
            json={
                **source_payload,
                "scheduleAction": "stop",
            },
        )
        self.assertEqual(stop_response.status_code, 200)
        stop_schedule = stop_response.json()["schedule"]
        self.assertFalse(stop_schedule["enabled"])
        self.assertEqual(stop_schedule["status"], "stopped")
        self.assertIsNone(stop_schedule["nextRunAt"])
        self.assertIsNone(stop_schedule["remainingSeconds"])

        resume_response = self.client.put(
            "/api/v1/monitor/source",
            json={
                **source_payload,
                "scheduleIntervalSeconds": 90,
                "scheduleAction": "resume",
            },
        )
        self.assertEqual(resume_response.status_code, 200)
        resume_schedule = resume_response.json()["schedule"]
        self.assertTrue(resume_schedule["enabled"])
        self.assertEqual(resume_schedule["intervalSeconds"], 90)
        self.assertGreaterEqual(resume_schedule["remainingSeconds"], 89)

    def test_monitor_mock_start_writes_endpoint_snapshot(self) -> None:
        mock_key = "api-data/governor-results-bkk/endpoint-mock/69-governor-electiondata.json"
        bmc_mock_key = "api-data/governor-results-bkk/endpoint-mock/69-bmc-electiondata.json"
        response = self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": True,
                "url": f"https://example.test/{mock_key}",
                "timeoutSeconds": 5,
                "bmcEnabled": True,
                "bmcUrl": f"https://example.test/{bmc_mock_key}",
                "bmcTimeoutSeconds": 5,
                "mockAction": "start",
                "mockIntervalSeconds": 8,
            },
        )
        self.assertEqual(response.status_code, 200)
        mock = response.json()["mock"]
        self.assertTrue(mock["enabled"])
        self.assertEqual(mock["intervalSeconds"], 8)
        self.assertEqual(mock["targetKey"], mock_key)
        self.assertEqual(mock["bmcTargetKey"], bmc_mock_key)
        uploaded = json.loads(self.s3_client.objects[("bucket", mock_key)].decode("utf-8"))
        self.assertEqual(uploaded["type"], "LIVE")
        self.assertEqual(uploaded["total"]["pollingUnits"]["reported"], 0)
        bmc_uploaded = json.loads(self.s3_client.objects[("bucket", bmc_mock_key)].decode("utf-8"))
        self.assertEqual(bmc_uploaded["type"], "LIVE")
        self.assertEqual(bmc_uploaded["total"]["result"], [])
        self.assertEqual(bmc_uploaded["total"]["pollingUnits"]["reported"], 0)

        tick = results_app.perform_monitor_mock_tick(trigger="manual")
        self.assertEqual(tick["targetKey"], mock_key)
        self.assertEqual(tick["bmcTargetKey"], bmc_mock_key)
        self.assertGreaterEqual(tick["openedCount"], 1)
        uploaded_after_tick = json.loads(self.s3_client.objects[("bucket", mock_key)].decode("utf-8"))
        self.assertGreaterEqual(uploaded_after_tick["total"]["pollingUnits"]["reported"], 1)
        bmc_after_tick = json.loads(self.s3_client.objects[("bucket", bmc_mock_key)].decode("utf-8"))
        self.assertEqual(bmc_after_tick["total"]["result"], [])
        self.assertGreaterEqual(bmc_after_tick["total"]["pollingUnits"]["reported"], 1)

    def test_monitor_mock_rejects_fetch_interval_not_greater_than_mock(self) -> None:
        response = self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": True,
                "url": "https://example.test/external-governor.json",
                "timeoutSeconds": 5,
                "mockAction": "start",
                "mockIntervalSeconds": 10,
                "scheduleIntervalSeconds": 10,
                "scheduleAction": "start",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_monitor_save_preserves_running_schedule(self):
        self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": True,
                "url": "https://example.test/external-governor.json",
                "timeoutSeconds": 5,
                "scheduleIntervalSeconds": 120,
                "scheduleAction": "start",
            },
        )

        save_response = self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": True,
                "url": "https://example.test/external-governor.json",
                "timeoutSeconds": 8,
            },
        )

        self.assertEqual(save_response.status_code, 200)
        saved_schedule = save_response.json()["schedule"]
        self.assertTrue(saved_schedule["enabled"])
        self.assertEqual(saved_schedule["intervalSeconds"], 120)
        self.assertIsNotNone(saved_schedule["nextRunAt"])

    def test_monitor_source_can_be_saved_and_used(self):
        self._fake_external_governor_source()

        response = self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": True,
                "url": "https://example.test/external-governor.json",
                "timeoutSeconds": 5,
            },
        )

        self.assertEqual(response.status_code, 200)
        saved_payload = response.json()
        self.assertEqual(saved_payload["current"]["source"], "override")
        self.assertEqual(saved_payload["current"]["url"], "https://example.test/external-governor.json")
        self.assertEqual(saved_payload["activePublicSource"]["source"], "line")

        summary_response = self.client.get("/api/v1/governor-results/summary")
        self.assertEqual(summary_response.status_code, 200)
        summary_payload = summary_response.json()
        self.assertEqual(summary_payload["dataInterpretation"]["mode"], "latest_snapshot")
        self.assertEqual(summary_payload["summary"]["countedUnits"], 1)

    def test_api_ignores_external_when_config_enabled(self):
        self._fake_external_governor_source()
        self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": True,
                "url": "https://example.test/external-governor.json",
                "timeoutSeconds": 5,
            },
        )

        summary_response = self.client.get("/api/v1/governor-results/summary")
        self.assertEqual(summary_response.status_code, 200)
        self.assertEqual(summary_response.json()["dataInterpretation"]["mode"], "latest_snapshot")

    def test_monitor_fetch_exports_static_results(self):
        self._fake_external_governor_source()
        self.client.put(
            "/api/v1/monitor/source",
            json={
                "enabled": True,
                "url": "https://example.test/external-governor.json",
                "timeoutSeconds": 5,
            },
        )

        response = self.client.post("/api/v1/monitor/source/fetch", json={})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["upstream"]["reportedPollingUnits"], 1)
        self.assertEqual(payload["bkkExportTarget"]["target"], "governor-results-bkk")
        self.assertEqual(payload["rawExport"]["target"], "governor-results-bkk")
        self.assertEqual(payload["rawExport"]["prefix"], "api-data/governor-results-bkk")
        self.assertEqual(payload["rawExport"]["latestKey"], "api-data/governor-results-bkk/raw/latest.json")
        self.assertIn(("bucket", "api-data/governor-results-bkk/raw/latest.json"), self.s3_client.objects)
        self.assertTrue(payload["rawExport"]["historyKey"].startswith("api-data/governor-results-bkk/raw/history/"))
        self.assertIn(("bucket", payload["rawExport"]["historyKey"]), self.s3_client.objects)
        self.assertEqual(payload["staticExport"]["summaryKey"], "api-data/governor-results-bkk/sumary.json")
        self.assertIn(("bucket", "api-data/governor-results-bkk/sumary.json"), self.s3_client.objects)
        self.assertIn(("bucket", "api-data/governor-results-bkk/districts.json"), self.s3_client.objects)

    def test_governor_results_summary_falls_back_to_static_export_when_live_data_is_empty(self):
        original_settings = results_app.settings
        self.addCleanup(setattr, results_app, "settings", original_settings)
        results_app.settings = results_app.Settings(
            **{
                **original_settings.__dict__,
                "enable_static_results_fallback": True,
            }
        )
        self.s3_client = _S3Client(
            {
                ("bucket", "api-data/governor-results/sumary.json"): _encoded(
                    {
                        "schemaVersion": "1.0",
                        "resource": "governor-results",
                        "pageMeta": {"title": "Static Summary"},
                        "summary": {"countedUnits": 30000, "totalUnits": 99954},
                        "candidates": [],
                        "dataQuality": {"isComplete": False, "isDelayed": False, "warnings": []},
                        "dataInterpretation": {"mode": "latest_snapshot", "description": "static"},
                    }
                )
            }
        )
        results_app.store = ResultsStore(s3_client=self.s3_client, bucket="bucket")
        results_app.invalidate_result_caches()

        response = self.client.get("/api/v1/governor-results/summary")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["pageMeta"]["title"], "Static Summary")
        self.assertEqual(payload["summary"]["countedUnits"], 30000)
        self.assertEqual(payload["summary"]["totalUnits"], 99954)

    def test_governor_results_summary_env_external_url_does_not_override_api(self):
        original_settings = results_app.settings
        original_read_json_source = results_app.read_json_source
        self.addCleanup(setattr, results_app, "settings", original_settings)
        self.addCleanup(setattr, results_app, "read_json_source", original_read_json_source)
        results_app.settings = results_app.Settings(
            **{
                **original_settings.__dict__,
                "external_governor_results_url": "https://example.test/external-governor.json",
                "external_governor_results_timeout_seconds": 5.0,
            }
        )

        def fake_read_json_source(*, source, timeout_seconds, s3_client=None):
            if source == "https://example.test/external-governor.json":
                return {
                    "type": "LIVE",
                    "total": {
                        "eligiblePopulation": 3500,
                        "totalVotes": 3200,
                        "badVotes": 10,
                        "noVotes": 200,
                        "goodVote": 2990,
                        "progress": 2.0,
                        "pollingUnits": {"total": 50, "reported": 1},
                        "result": [{"candidateId": "18", "count": 300}],
                    },
                    "districts": [],
                    "lastUpdatedAt": "2026-06-23T07:38:12Z",
                }
            return original_read_json_source(source=source, timeout_seconds=timeout_seconds, s3_client=s3_client)

        results_app.read_json_source = fake_read_json_source
        results_app.invalidate_result_caches()

        response = self.client.get("/api/v1/governor-results/summary")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["dataInterpretation"]["mode"], "latest_snapshot")
        self.assertEqual(payload["summary"]["countedUnits"], 1)
        self.assertNotEqual(payload["summary"]["totalUnits"], 50)

    def test_governor_results_endpoints_return_utf8_json_content_type(self):
        summary_response = self.client.get("/api/v1/governor-results/summary")
        districts_response = self.client.get("/api/v1/governor-results/districts")

        self.assertIn("application/json", summary_response.headers["content-type"])
        self.assertIn("charset=utf-8", summary_response.headers["content-type"].lower())
        self.assertIn("application/json", districts_response.headers["content-type"])
        self.assertIn("charset=utf-8", districts_response.headers["content-type"].lower())

    def test_governor_results_districts_falls_back_to_static_export_when_live_data_is_empty(self):
        original_settings = results_app.settings
        self.addCleanup(setattr, results_app, "settings", original_settings)
        results_app.settings = results_app.Settings(
            **{
                **original_settings.__dict__,
                "enable_static_results_fallback": True,
            }
        )
        self.s3_client = _S3Client(
            {
                ("bucket", "api-data/governor-results/districts.json"): _encoded(
                    {
                        "schemaVersion": "1.0",
                        "resource": "governor-district-results",
                        "constituencies": [{"areaId": "1", "name": "Static District"}],
                    }
                )
            }
        )
        results_app.store = ResultsStore(s3_client=self.s3_client, bucket="bucket")
        results_app.invalidate_result_caches()

        response = self.client.get("/api/v1/governor-results/districts")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["constituencies"][0]["name"], "Static District")

    def test_governor_results_summary_does_not_fall_back_when_disabled(self):
        original_settings = results_app.settings
        self.addCleanup(setattr, results_app, "settings", original_settings)
        results_app.settings = results_app.Settings(
            **{
                **original_settings.__dict__,
                "enable_static_results_fallback": False,
            }
        )
        self.s3_client = _S3Client(
            {
                ("bucket", "api-data/governor-results/sumary.json"): _encoded(
                    {
                        "schemaVersion": "1.0",
                        "resource": "governor-results",
                        "summary": {"countedUnits": 30000},
                    }
                )
            }
        )
        results_app.store = ResultsStore(s3_client=self.s3_client, bucket="bucket")
        results_app.invalidate_result_caches()

        response = self.client.get("/api/v1/governor-results/summary")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["summary"]["countedUnits"], 0)

    def test_governor_results_summary_fresh_bypasses_cache(self):
        original_builder = results_app._build_governor_results_response
        self.addCleanup(setattr, results_app, "_build_governor_results_response", original_builder)
        call_count = {"count": 0}

        def fake_builder():
            call_count["count"] += 1
            return {
                "schemaVersion": "1.0",
                "resource": "governor-results",
                "summary": {"countedUnits": call_count["count"]},
            }

        results_app._build_governor_results_response = fake_builder
        results_app.invalidate_result_caches()

        cached_response = self.client.get("/api/v1/governor-results/summary")
        fresh_response = self.client.get("/api/v1/governor-results/summary?fresh=1")

        self.assertEqual(cached_response.status_code, 200)
        self.assertEqual(fresh_response.status_code, 200)
        self.assertEqual(cached_response.json()["summary"]["countedUnits"], 1)
        self.assertEqual(fresh_response.json()["summary"]["countedUnits"], 2)


if __name__ == "__main__":
    unittest.main()
