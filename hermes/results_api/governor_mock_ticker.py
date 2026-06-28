from __future__ import annotations

import copy
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MOCK_S3_KEY = "api-data/governor-results-bkk/endpoint-mock/69-governor-electiondata.json"
DEFAULT_BMC_MOCK_S3_KEY = "api-data/governor-results-bkk/endpoint-mock/69-bmc-electiondata.json"
FIXTURE_FINAL_PATH = Path(__file__).with_name("fixtures") / "governor-mock-final.json"
FIXTURE_BMC_FINAL_PATH = Path(__file__).with_name("fixtures") / "bmc-mock-final.json"
MIN_MOCK_INTERVAL_SECONDS = 5
MIN_MOCK_FETCH_GAP_SECONDS = 2
DEFAULT_DISTRICTS_PER_TICK = 2


def fresh_mock_seed() -> int:
    return random.SystemRandom().randint(1, 2_147_483_647)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_final_for_incremental_mock(final_payload: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(final_payload)
    districts = payload.get("districts") if isinstance(payload.get("districts"), list) else []
    total_block = payload.get("total") if isinstance(payload.get("total"), dict) else {}
    if not districts:
        return payload

    districts_with_votes = sum(
        1
        for district in districts
        if int((district.get("voting") or {}).get("goodVote") or 0) > 0
    )
    if districts_with_votes >= max(1, len(districts) // 2):
        return payload

    total_good = int(total_block.get("goodVote") or 0)
    total_turnout = int(total_block.get("totalVotes") or total_good)
    total_bad = int(total_block.get("badVotes") or 0)
    total_no = int(total_block.get("noVotes") or 0)
    total_eligible = int(total_block.get("eligiblePopulation") or total_turnout)
    candidate_template = total_block.get("result") if isinstance(total_block.get("result"), list) else []
    district_count = len(districts)

    def split_value(total_value: int) -> list[int]:
        if district_count <= 0:
            return []
        base = total_value // district_count
        remainder = total_value % district_count
        return [base + (1 if index < remainder else 0) for index in range(district_count)]

    good_votes = split_value(total_good)
    turnout_votes = split_value(total_turnout)
    bad_votes = split_value(total_bad)
    no_votes = split_value(total_no)
    eligible_votes = split_value(total_eligible)

    for index, district in enumerate(districts):
        district_good = good_votes[index]
        district_turnout = turnout_votes[index]
        district_results = []
        allocated = 0
        for candidate_index, item in enumerate(candidate_template):
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("candidateId") or "").strip()
            if not candidate_id:
                continue
            if candidate_index == len(candidate_template) - 1:
                count = max(0, district_good - allocated)
            else:
                template_count = int(item.get("count") or 0)
                share = int(district_good * template_count / total_good) if total_good else 0
                count = share
                allocated += share
            district_results.append({"candidateId": candidate_id, "count": count})
        voting = district.setdefault("voting", {})
        voting.update(
            {
                "eligiblePopulation": eligible_votes[index],
                "totalVotes": district_turnout,
                "badVotes": bad_votes[index],
                "noVotes": no_votes[index],
                "goodVote": district_good,
                "progress": 100.0 if district_good or district_turnout else 0.0,
                "result": district_results,
                "pollingUnits": {"total": 1, "reported": 1 if district_good or district_turnout else 0},
            }
        )
    return payload


def load_final_fixture(*, fixture_path: Path | None = None) -> dict[str, Any]:
    path = fixture_path or FIXTURE_FINAL_PATH
    return normalize_final_for_incremental_mock(json.loads(path.read_text(encoding="utf-8")))


def load_bmc_final_fixture(*, fixture_path: Path | None = None) -> dict[str, Any]:
    path = fixture_path or FIXTURE_BMC_FINAL_PATH
    return normalize_final_for_incremental_mock(json.loads(path.read_text(encoding="utf-8")))


def _candidate_ids_from_payload(payload: dict[str, Any]) -> list[str]:
    total_block = payload.get("total") if isinstance(payload.get("total"), dict) else {}
    candidate_ids: list[str] = []
    for item in total_block.get("result") or []:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidateId") or "").strip()
        if candidate_id:
            candidate_ids.append(candidate_id)
    return candidate_ids


def _fix_vote_sum(counts: dict[str, int], *, target: int) -> None:
    current = sum(counts.values())
    delta = target - current
    if delta == 0 or not counts:
        return
    leader = max(counts, key=lambda key: counts[key])
    counts[leader] = max(0, counts[leader] + delta)


def apply_competitive_mock_splits(
    payload: dict[str, Any],
    *,
    seed: int,
    completed_cycles: int = 0,
    volatility_tick: int = 0,
) -> dict[str, Any]:
    """Randomize each district every tick so leaders swing like a volatile stock."""
    result = copy.deepcopy(payload)
    candidate_ids = _candidate_ids_from_payload(result)
    if len(candidate_ids) < 2:
        return result

    districts = result.get("districts") if isinstance(result.get("districts"), list) else []
    cycle_seed = int(seed) + int(completed_cycles) * 9973 + int(volatility_tick) * 7919

    for district_index, district in enumerate(districts):
        if not isinstance(district, dict):
            continue
        voting = district.get("voting") if isinstance(district.get("voting"), dict) else {}
        good_vote = int(voting.get("goodVote") or 0)
        if good_vote <= 0:
            continue

        rng = random.Random(cycle_seed + district_index * 131)
        shuffled = candidate_ids.copy()
        rng.shuffle(shuffled)
        favorite = shuffled[0]
        runner_up = shuffled[1]
        third = shuffled[2] if len(shuffled) > 2 else runner_up
        others = [candidate_id for candidate_id in shuffled[3:]]

        favorite_share = rng.uniform(0.30, 0.72)
        runner_share = rng.uniform(0.14, min(0.48, 1.0 - favorite_share - 0.06))
        third_share = rng.uniform(0.04, min(0.22, 1.0 - favorite_share - runner_share - 0.02))
        favorite_count = int(good_vote * favorite_share)
        runner_count = int(good_vote * runner_share)
        third_count = int(good_vote * third_share)
        remaining = max(0, good_vote - favorite_count - runner_count - third_count)

        counts = {candidate_id: 0 for candidate_id in candidate_ids}
        counts[favorite] = favorite_count
        counts[runner_up] = runner_count
        counts[third] = third_count

        if others and remaining > 0:
            weights = [rng.random() for _ in others]
            weight_total = sum(weights) or 1.0
            allocated_other = 0
            for index, candidate_id in enumerate(others):
                if index == len(others) - 1:
                    share = remaining - allocated_other
                else:
                    share = int(remaining * weights[index] / weight_total)
                    allocated_other += share
                counts[candidate_id] = share

        _fix_vote_sum(counts, target=good_vote)
        voting["result"] = [{"candidateId": candidate_id, "count": counts[candidate_id]} for candidate_id in candidate_ids]
        district["voting"] = voting

    return result


def _candidate_ids_from_district_voting(voting: dict[str, Any]) -> list[str]:
    candidate_ids: list[str] = []
    for item in voting.get("result") or []:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidateId") or "").strip()
        if candidate_id:
            candidate_ids.append(candidate_id)
    return candidate_ids


def apply_competitive_mock_splits_bmc(
    payload: dict[str, Any],
    *,
    seed: int,
    completed_cycles: int = 0,
    volatility_tick: int = 0,
) -> dict[str, Any]:
    """Randomize per-district BMC candidates (district-scoped candidateId values)."""
    result = copy.deepcopy(payload)
    districts = result.get("districts") if isinstance(result.get("districts"), list) else []
    cycle_seed = int(seed) + int(completed_cycles) * 9973 + int(volatility_tick) * 7919

    for district_index, district in enumerate(districts):
        if not isinstance(district, dict):
            continue
        voting = district.get("voting") if isinstance(district.get("voting"), dict) else {}
        good_vote = int(voting.get("goodVote") or 0)
        candidate_ids = _candidate_ids_from_district_voting(voting)
        if good_vote <= 0 or len(candidate_ids) < 2:
            continue

        rng = random.Random(cycle_seed + district_index * 131)
        shuffled = candidate_ids.copy()
        rng.shuffle(shuffled)
        favorite = shuffled[0]
        runner_up = shuffled[1]
        third = shuffled[2] if len(shuffled) > 2 else runner_up
        others = [candidate_id for candidate_id in shuffled[3:]]

        favorite_share = rng.uniform(0.30, 0.72)
        runner_share = rng.uniform(0.14, min(0.48, 1.0 - favorite_share - 0.06))
        third_share = rng.uniform(0.04, min(0.22, 1.0 - favorite_share - runner_share - 0.02))
        favorite_count = int(good_vote * favorite_share)
        runner_count = int(good_vote * runner_share)
        third_count = int(good_vote * third_share)
        remaining = max(0, good_vote - favorite_count - runner_count - third_count)

        counts = {candidate_id: 0 for candidate_id in candidate_ids}
        counts[favorite] = favorite_count
        counts[runner_up] = runner_count
        counts[third] = third_count

        if others and remaining > 0:
            weights = [rng.random() for _ in others]
            weight_total = sum(weights) or 1.0
            allocated_other = 0
            for index, candidate_id in enumerate(others):
                if index == len(others) - 1:
                    share = remaining - allocated_other
                else:
                    share = int(remaining * weights[index] / weight_total)
                    allocated_other += share
                counts[candidate_id] = share

        _fix_vote_sum(counts, target=good_vote)
        voting["result"] = [{"candidateId": candidate_id, "count": counts[candidate_id]} for candidate_id in candidate_ids]
        district["voting"] = voting

    return result


def prepare_bmc_mock_final_payload(
    state: dict[str, Any],
    *,
    final_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    final = final_payload or load_bmc_final_fixture()
    return apply_competitive_mock_splits_bmc(
        final,
        seed=int(state.get("seed") or 42),
        completed_cycles=int(state.get("completedCycles") or 0),
        volatility_tick=int(state.get("volatilityTick") or 0),
    )


def build_bmc_snapshot(
    final_payload: dict[str, Any],
    *,
    opened_district_indexes: set[int],
) -> dict[str, Any]:
    snapshot = build_snapshot(final_payload, opened_district_indexes=opened_district_indexes)
    total_block = snapshot.get("total") if isinstance(snapshot.get("total"), dict) else {}
    total_block["result"] = []
    snapshot["total"] = total_block
    return snapshot


def build_bmc_mock_tick_snapshot(
    *,
    state: dict[str, Any],
    final_payload: dict[str, Any] | None = None,
    districts_per_tick: int = DEFAULT_DISTRICTS_PER_TICK,
) -> tuple[dict[str, Any], dict[str, Any]]:
    final = prepare_bmc_mock_final_payload(state, final_payload=final_payload)
    updated_state = advance_mock_state(state, districts_per_tick=districts_per_tick)
    snapshot = build_bmc_snapshot(final, opened_district_indexes=opened_indexes_from_state(updated_state))
    snapshot["lastUpdatedAt"] = utc_now_iso()
    return snapshot, updated_state


def build_bmc_mock_reset_snapshot(
    *,
    state: dict[str, Any],
    final_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    final = prepare_bmc_mock_final_payload(state, final_payload=final_payload)
    reset_state = {
        **state,
        "openedCount": 0,
    }
    snapshot = build_bmc_snapshot(final, opened_district_indexes=set())
    return snapshot, reset_state


def prepare_mock_final_payload(
    state: dict[str, Any],
    *,
    final_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    final = final_payload or load_final_fixture()
    return apply_competitive_mock_splits(
        final,
        seed=int(state.get("seed") or 42),
        completed_cycles=int(state.get("completedCycles") or 0),
        volatility_tick=int(state.get("volatilityTick") or 0),
    )


def zero_voting_block(voting: dict[str, Any]) -> dict[str, Any]:
    zeroed_results = []
    for item in voting.get("result") or []:
        if not isinstance(item, dict):
            continue
        zeroed_results.append(
            {
                "candidateId": item.get("candidateId"),
                "count": 0,
            }
        )
    polling_units = voting.get("pollingUnits") if isinstance(voting.get("pollingUnits"), dict) else {}
    return {
        "eligiblePopulation": 0,
        "totalVotes": 0,
        "badVotes": 0,
        "noVotes": 0,
        "goodVote": 0,
        "progress": 0.0,
        "result": zeroed_results,
        "pollingUnits": {
            "total": int(polling_units.get("total") or 1),
            "reported": 0,
        },
    }


def _candidate_sort_key(candidate_id: str) -> tuple[int, int | str]:
    if candidate_id.isdigit():
        return (0, int(candidate_id))
    return (1, candidate_id)


def aggregate_candidate_results(districts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[str, int] = {}
    for district in districts:
        voting = district.get("voting") if isinstance(district.get("voting"), dict) else {}
        for item in voting.get("result") or []:
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("candidateId") or "").strip()
            if not candidate_id:
                continue
            try:
                count = int(item.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
            totals[candidate_id] = totals.get(candidate_id, 0) + count
    return [
        {"candidateId": candidate_id, "count": count}
        for candidate_id, count in sorted(totals.items(), key=lambda item: _candidate_sort_key(item[0]))
    ]


def aggregate_total_from_districts(districts: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = turnout = bad = no = good = 0
    polling_total = polling_reported = 0
    for district in districts:
        voting = district.get("voting") if isinstance(district.get("voting"), dict) else {}
        eligible += int(voting.get("eligiblePopulation") or 0)
        turnout += int(voting.get("totalVotes") or 0)
        bad += int(voting.get("badVotes") or 0)
        no += int(voting.get("noVotes") or 0)
        good += int(voting.get("goodVote") or 0)
        polling_units = voting.get("pollingUnits") if isinstance(voting.get("pollingUnits"), dict) else {}
        polling_total += int(polling_units.get("total") or 0)
        polling_reported += int(polling_units.get("reported") or 0)
    progress = round(polling_reported / polling_total * 100, 2) if polling_total else 0.0
    return {
        "eligiblePopulation": eligible,
        "totalVotes": turnout,
        "badVotes": bad,
        "noVotes": no,
        "goodVote": good,
        "progress": progress,
        "pollingUnits": {
            "total": polling_total,
            "reported": polling_reported,
        },
        "result": aggregate_candidate_results(districts),
    }


def build_snapshot(
    final_payload: dict[str, Any],
    *,
    opened_district_indexes: set[int],
) -> dict[str, Any]:
    final_districts = final_payload.get("districts") if isinstance(final_payload.get("districts"), list) else []
    districts: list[dict[str, Any]] = []
    for index, district in enumerate(final_districts):
        if not isinstance(district, dict):
            continue
        if index in opened_district_indexes:
            districts.append(copy.deepcopy(district))
            continue
        voting = district.get("voting") if isinstance(district.get("voting"), dict) else {}
        districts.append(
            {
                "name": district.get("name"),
                "voting": zero_voting_block(voting),
            }
        )
    return {
        "type": str(final_payload.get("type") or "LIVE"),
        "total": aggregate_total_from_districts(districts),
        "districts": districts,
        "lastUpdatedAt": utc_now_iso(),
    }


def initial_mock_state(*, district_count: int, seed: int | None = None) -> dict[str, Any]:
    chosen_seed = int(seed if seed is not None else fresh_mock_seed())
    order = list(range(district_count))
    random.Random(chosen_seed).shuffle(order)
    return {
        "openedCount": 0,
        "districtOrder": order,
        "seed": chosen_seed,
        "totalDistricts": district_count,
        "volatilityTick": 0,
    }


def opened_indexes_from_state(state: dict[str, Any]) -> set[int]:
    order = [int(value) for value in state.get("districtOrder") or []]
    opened_count = int(state.get("openedCount") or 0)
    return set(order[:opened_count])


def advance_mock_state(
    state: dict[str, Any],
    *,
    districts_per_tick: int = DEFAULT_DISTRICTS_PER_TICK,
) -> dict[str, Any]:
    updated = dict(state)
    updated["volatilityTick"] = int(updated.get("volatilityTick") or 0) + 1
    total_districts = int(updated.get("totalDistricts") or 0)
    opened_count = int(updated.get("openedCount") or 0)
    next_count = opened_count + max(1, int(districts_per_tick))
    if next_count > total_districts:
        updated["openedCount"] = 0
        updated["completedCycles"] = int(updated.get("completedCycles") or 0) + 1
    else:
        updated["openedCount"] = next_count
    return updated


def validate_mock_fetch_intervals(*, mock_interval_seconds: int, fetch_interval_seconds: int) -> None:
    if mock_interval_seconds < MIN_MOCK_INTERVAL_SECONDS:
        raise ValueError(f"mockIntervalSeconds must be at least {MIN_MOCK_INTERVAL_SECONDS}.")
    if fetch_interval_seconds <= mock_interval_seconds:
        raise ValueError(
            "scheduleIntervalSeconds must be greater than mockIntervalSeconds "
            f"(need at least {MIN_MOCK_FETCH_GAP_SECONDS}s gap)."
        )
    if fetch_interval_seconds - mock_interval_seconds < MIN_MOCK_FETCH_GAP_SECONDS:
        raise ValueError(
            f"scheduleIntervalSeconds must be at least {mock_interval_seconds + MIN_MOCK_FETCH_GAP_SECONDS} "
            f"when mockIntervalSeconds is {mock_interval_seconds}."
        )


def build_mock_tick_snapshot(
    *,
    state: dict[str, Any],
    final_payload: dict[str, Any] | None = None,
    districts_per_tick: int = DEFAULT_DISTRICTS_PER_TICK,
) -> tuple[dict[str, Any], dict[str, Any]]:
    final = prepare_mock_final_payload(state, final_payload=final_payload)
    updated_state = advance_mock_state(state, districts_per_tick=districts_per_tick)
    snapshot = build_snapshot(final, opened_district_indexes=opened_indexes_from_state(updated_state))
    snapshot["lastUpdatedAt"] = utc_now_iso()
    return snapshot, updated_state


def build_mock_reset_snapshot(
    *,
    state: dict[str, Any],
    final_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    final = prepare_mock_final_payload(state, final_payload=final_payload)
    reset_state = {
        **state,
        "openedCount": 0,
    }
    snapshot = build_snapshot(final, opened_district_indexes=set())
    return snapshot, reset_state


def build_dual_mock_tick_snapshots(
    *,
    state: dict[str, Any],
    governor_final_payload: dict[str, Any] | None = None,
    bmc_final_payload: dict[str, Any] | None = None,
    districts_per_tick: int = DEFAULT_DISTRICTS_PER_TICK,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    governor_final = prepare_mock_final_payload(
        state,
        final_payload=governor_final_payload or load_final_fixture(),
    )
    bmc_final = prepare_bmc_mock_final_payload(
        state,
        final_payload=bmc_final_payload or load_bmc_final_fixture(),
    )
    updated_state = advance_mock_state(state, districts_per_tick=districts_per_tick)
    opened = opened_indexes_from_state(updated_state)
    governor_snapshot = build_snapshot(governor_final, opened_district_indexes=opened)
    bmc_snapshot = build_bmc_snapshot(bmc_final, opened_district_indexes=opened)
    now = utc_now_iso()
    governor_snapshot["lastUpdatedAt"] = now
    bmc_snapshot["lastUpdatedAt"] = now
    return governor_snapshot, bmc_snapshot, updated_state


def build_dual_mock_reset_snapshots(
    *,
    state: dict[str, Any],
    governor_final_payload: dict[str, Any] | None = None,
    bmc_final_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    governor_final = prepare_mock_final_payload(
        state,
        final_payload=governor_final_payload or load_final_fixture(),
    )
    bmc_final = prepare_bmc_mock_final_payload(
        state,
        final_payload=bmc_final_payload or load_bmc_final_fixture(),
    )
    reset_state = {
        **state,
        "openedCount": 0,
    }
    governor_snapshot = build_snapshot(governor_final, opened_district_indexes=set())
    bmc_snapshot = build_bmc_snapshot(bmc_final, opened_district_indexes=set())
    return governor_snapshot, bmc_snapshot, reset_state
