import json
import unittest
from pathlib import Path
from typing import Any

from hermes.governor_results.sorkor_adapter import (
    build_sorkor_districts_from_external_payload,
    build_sorkor_summary_from_external_payload,
    parse_bmc_candidate_number,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
BMC_FIXTURE_CANDIDATES = (
    REPO_ROOT / "docs" / "mockupdata" / "output_resulttest" / "69-bmc-electiondata.json",
    REPO_ROOT / "hermes" / "results_api" / "fixtures" / "bmc-mock-final.json",
)


def _bmc_fixture_path() -> Path:
    for path in BMC_FIXTURE_CANDIDATES:
        if path.is_file():
            return path
    searched = ", ".join(str(path) for path in BMC_FIXTURE_CANDIDATES)
    raise FileNotFoundError(f"BMC fixture not found. Searched: {searched}")


def _sample_district_catalog() -> dict[str, dict[str, Any]]:
    districts: dict[str, dict[str, Any]] = {}
    for number, name in ((1, "พระนคร"), (2, "ดุสิต")):
        districts[str(number)] = {
            "id": number,
            "provinceCode": 10,
            "districtNameTh": name,
            "electionAreaId": str(number),
        }
    return districts


class SorkorAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw_payload = json.loads(_bmc_fixture_path().read_text(encoding="utf-8"))

    def test_parse_bmc_candidate_number(self) -> None:
        self.assertEqual(parse_bmc_candidate_number("พระนคร-3"), 3)
        self.assertIsNone(parse_bmc_candidate_number("invalid"))

    def test_build_sorkor_summary_shape(self) -> None:
        payload = build_sorkor_summary_from_external_payload(raw_payload=self.raw_payload)
        self.assertEqual(payload["resource"], "sorkor-results")
        self.assertEqual(payload["summary"]["party"], [])
        self.assertIn("% บัตรเสีย", payload["summary"])
        self.assertNotIn("candidates", payload)
        self.assertNotIn("dataQuality", payload)

    def test_build_sorkor_districts_wrapper_and_minimal_candidates(self) -> None:
        payload = build_sorkor_districts_from_external_payload(
            raw_payload=self.raw_payload,
            district_catalog=_sample_district_catalog(),
        )
        self.assertEqual(payload["resource"], "constituency-bangkok")
        self.assertIn("data", payload)
        constituencies = payload["data"]["constituencies"]
        self.assertEqual(len(constituencies), 2)

        phra_nakhon = constituencies[0]
        self.assertEqual(phra_nakhon["name"], "พระนคร")
        self.assertEqual(phra_nakhon["leadingCandidateId"], "พระนคร-1")
        self.assertEqual(len(phra_nakhon["candidates"]), 5)

        leader = phra_nakhon["candidates"][0]
        self.assertEqual(leader["candidateId"], "พระนคร-1")
        self.assertEqual(leader["candidateNumber"], 1)
        self.assertEqual(leader["voteCount"], 1174)
        self.assertTrue(leader["isLeading"])
        self.assertNotIn("name", leader)
        self.assertNotIn("party", leader)
