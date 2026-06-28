from __future__ import annotations

import json
import unittest
from pathlib import Path

from hermes.results_api.governor_mock_ticker import (
    advance_mock_state,
    apply_competitive_mock_splits,
    apply_competitive_mock_splits_bmc,
    build_bmc_mock_tick_snapshot,
    build_mock_tick_snapshot,
    build_snapshot,
    initial_mock_state,
    load_bmc_final_fixture,
    load_final_fixture,
    validate_mock_fetch_intervals,
)


def _leader_candidate_id(snapshot: dict) -> str:
    results = snapshot["total"]["result"]
    return max(results, key=lambda item: item["count"])["candidateId"]


class GovernorMockTickerTests(unittest.TestCase):
    def test_build_snapshot_opens_districts_incrementally(self) -> None:
        final = load_final_fixture()
        district_count = len(final["districts"])
        state = initial_mock_state(district_count=district_count, seed=1)

        snapshot, state = build_mock_tick_snapshot(state=state, final_payload=final, districts_per_tick=1)
        self.assertEqual(state["openedCount"], 1)
        self.assertEqual(snapshot["total"]["pollingUnits"]["reported"], 1)
        self.assertGreater(snapshot["total"]["goodVote"], 0)

        opened = 0
        while opened < district_count:
            snapshot, state = build_mock_tick_snapshot(state=state, final_payload=final, districts_per_tick=1)
            opened = int(state["openedCount"]) if int(state["openedCount"]) > 0 else district_count
        self.assertEqual(snapshot["total"]["pollingUnits"]["reported"], district_count)
        self.assertEqual(snapshot["total"]["goodVote"], final["total"]["goodVote"])

    def test_advance_mock_state_loops_after_final_district(self) -> None:
        state = {"openedCount": 2, "districtOrder": [0, 1], "totalDistricts": 2}
        advanced = advance_mock_state(state)
        self.assertEqual(advanced["openedCount"], 0)
        self.assertEqual(advanced["completedCycles"], 1)

    def test_validate_mock_fetch_intervals(self) -> None:
        validate_mock_fetch_intervals(mock_interval_seconds=8, fetch_interval_seconds=10)
        with self.assertRaises(ValueError):
            validate_mock_fetch_intervals(mock_interval_seconds=10, fetch_interval_seconds=10)
        with self.assertRaises(ValueError):
            validate_mock_fetch_intervals(mock_interval_seconds=9, fetch_interval_seconds=10)

    def test_competitive_splits_change_leader_often(self) -> None:
        final = load_final_fixture()
        competitive = apply_competitive_mock_splits(final, seed=7, volatility_tick=3)
        district_count = len(competitive["districts"])
        state = initial_mock_state(district_count=district_count, seed=7)

        leaders: list[str] = []
        for _ in range(30):
            snapshot, state = build_mock_tick_snapshot(state=state, final_payload=competitive, districts_per_tick=1)
            leaders.append(_leader_candidate_id(snapshot))
            if int(state["openedCount"]) == 0:
                break

        leader_changes = sum(1 for index in range(1, len(leaders)) if leaders[index] != leaders[index - 1])
        self.assertGreaterEqual(leader_changes, 15, f"leaders={leaders}")
        self.assertGreaterEqual(len(set(leaders)), 8, f"unique leaders={sorted(set(leaders), key=int)}")

    def test_bmc_snapshot_keeps_empty_total_result(self) -> None:
        final = load_bmc_final_fixture()
        district_count = len(final["districts"])
        state = initial_mock_state(district_count=district_count, seed=3)
        snapshot, state = build_bmc_mock_tick_snapshot(state=state, final_payload=final, districts_per_tick=1)
        self.assertEqual(snapshot["total"]["result"], [])
        self.assertGreater(snapshot["total"]["pollingUnits"]["reported"], 0)
        opened_districts = [
            district
            for district in snapshot["districts"]
            if int((district.get("voting") or {}).get("pollingUnits", {}).get("reported") or 0) > 0
        ]
        self.assertTrue(any(int((district.get("voting") or {}).get("goodVote") or 0) > 0 for district in opened_districts))

    def test_bmc_competitive_splits_shuffle_per_district(self) -> None:
        final = load_bmc_final_fixture()
        competitive = apply_competitive_mock_splits_bmc(final, seed=11, volatility_tick=2)
        district_count = len(competitive["districts"])
        state = initial_mock_state(district_count=district_count, seed=11)

        leaders: list[str] = []
        for _ in range(20):
            snapshot, state = build_bmc_mock_tick_snapshot(
                state=state,
                final_payload=competitive,
                districts_per_tick=1,
            )
            opened_index = max(0, int(state["openedCount"]) - 1)
            district = snapshot["districts"][opened_index]
            results = district["voting"]["result"]
            leader = max(results, key=lambda item: item["count"])["candidateId"]
            leaders.append(leader)
            if int(state["openedCount"]) == 0:
                break

        leader_changes = sum(1 for index in range(1, len(leaders)) if leaders[index] != leaders[index - 1])
        self.assertGreaterEqual(leader_changes, 8, f"leaders={leaders}")
        self.assertGreaterEqual(len(set(leaders)), 4, f"unique leaders={sorted(set(leaders))}")


if __name__ == "__main__":
    unittest.main()
