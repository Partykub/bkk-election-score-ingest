from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def without_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: without_nulls(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [without_nulls(item) for item in value if item is not None]
    return value


def normalize_text_key(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    return text.casefold()


def parse_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def percentage_of(value: int | None, total: int | None) -> float | None:
    if value is None or not total:
        return None
    return round(value / total * 100, 2)


def result_status_from_external_type(raw_type: Any) -> str:
    normalized = str(raw_type or "").strip().upper()
    if normalized == "FINAL":
        return "FINAL"
    return "LIVE_COUNT"


def parse_bmc_candidate_number(raw_id: Any) -> int | None:
    text = str(raw_id or "").strip()
    if not text or "-" not in text:
        return None
    suffix = text.rsplit("-", 1)[-1]
    return parse_int(suffix)


def build_sorkor_candidates_from_voting_result(
    *,
    raw_results: list[Any],
    total_votes: int | None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        raw_candidate_id = str(item.get("candidateId") or "").strip()
        vote_count = parse_int(item.get("count"))
        candidate_number = parse_bmc_candidate_number(raw_candidate_id)
        if not raw_candidate_id or vote_count is None or candidate_number is None:
            continue
        candidates.append(
            {
                "candidateId": raw_candidate_id,
                "candidateNumber": candidate_number,
                "voteCount": vote_count,
                "votePercentage": round(vote_count / total_votes * 100, 2) if total_votes else 0,
            }
        )
    candidates.sort(key=lambda item: (-item["voteCount"], item["candidateNumber"]))
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
        candidate["isLeading"] = rank == 1
    return candidates


def build_sorkor_summary_from_external_payload(
    *,
    raw_payload: dict[str, Any],
    election_id: str = "bkk-sorkor-2026",
    title: str = "ผลการเลือก ส.ก. กรุงเทพมหานคร",
    generated_at: str | None = None,
) -> dict[str, Any]:
    total = raw_payload.get("total") if isinstance(raw_payload.get("total"), dict) else {}
    total_polling_units = total.get("pollingUnits") if isinstance(total.get("pollingUnits"), dict) else {}
    valid_ballots = parse_int(total.get("goodVote"))
    invalid_ballots = parse_int(total.get("badVotes"))
    abstained_ballots = parse_int(total.get("noVotes"))
    voter_turnout = parse_int(total.get("totalVotes"))
    eligible_voters = parse_int(total.get("eligiblePopulation"))
    counted_units = parse_int(total_polling_units.get("reported"))
    total_units = parse_int(total_polling_units.get("total"))
    last_updated_at = str(raw_payload.get("lastUpdatedAt") or "").strip() or None
    generated_timestamp = generated_at or utc_now_iso()

    summary: dict[str, Any] = without_nulls(
        {
            "countedUnits": counted_units,
            "totalUnits": total_units,
            "countedPercentage": parse_float(total.get("progress")),
            "eligibleVoters": eligible_voters,
            "voterTurnout": voter_turnout,
            "voterTurnoutPercentage": percentage_of(voter_turnout, eligible_voters),
            "validBallots": valid_ballots,
            "invalidBallots": invalid_ballots,
            "abstainedBallots": abstained_ballots,
            "lastUpdatedAt": last_updated_at,
            "% บัตรเสีย": percentage_of(invalid_ballots, voter_turnout),
            "party": [],
        }
    )

    return {
        "schemaVersion": "1.0",
        "resource": "sorkor-results",
        "pageMeta": {
            "electionId": election_id,
            "title": title,
            "resultStatus": result_status_from_external_type(raw_payload.get("type")),
            "generatedAt": generated_timestamp,
        },
        "summary": summary,
    }


def build_sorkor_districts_from_external_payload(
    *,
    raw_payload: dict[str, Any],
    district_catalog: dict[str, dict[str, Any]],
    generated_at: str | None = None,
) -> dict[str, Any]:
    districts_by_name: dict[str, dict[str, Any]] = {}
    for district in district_catalog.values():
        if district.get("provinceCode") != 10:
            continue
        key = normalize_text_key(district.get("districtNameTh"))
        if key and key not in districts_by_name:
            districts_by_name[key] = district

    raw_districts_by_name: dict[str, dict[str, Any]] = {}
    for raw_district in raw_payload.get("districts", []):
        if not isinstance(raw_district, dict):
            continue
        key = normalize_text_key(raw_district.get("name"))
        if key:
            raw_districts_by_name[key] = raw_district

    unique_districts: dict[str, dict[str, Any]] = {}
    for district in district_catalog.values():
        district_id = str(district.get("id") or "")
        if not district_id or district_id in unique_districts:
            continue
        unique_districts[district_id] = district
    bangkok_districts = [district for district in unique_districts.values() if district.get("provinceCode") == 10]
    bangkok_districts.sort(key=lambda district: int(district.get("id") or 0))

    constituencies: list[dict[str, Any]] = []
    last_updated_at = str(raw_payload.get("lastUpdatedAt") or "").strip() or None
    for district in bangkok_districts:
        raw_district = raw_districts_by_name.get(normalize_text_key(district.get("districtNameTh")))
        voting = raw_district.get("voting") if isinstance(raw_district, dict) and isinstance(raw_district.get("voting"), dict) else {}
        valid_ballots = parse_int(voting.get("goodVote"))
        invalid_ballots = parse_int(voting.get("badVotes"))
        abstained_ballots = parse_int(voting.get("noVotes"))
        voter_turnout = parse_int(voting.get("totalVotes"))
        eligible_voters = parse_int(voting.get("eligiblePopulation"))
        candidates = build_sorkor_candidates_from_voting_result(
            raw_results=voting.get("result") if isinstance(voting.get("result"), list) else [],
            total_votes=valid_ballots,
        )
        constituency = {
            "areaId": str(district.get("electionAreaId") or district.get("id")),
            "number": district.get("id"),
            "name": district.get("districtNameTh"),
            "leadingCandidateId": candidates[0].get("candidateId") if candidates else None,
        }
        optional_summary_fields = without_nulls(
            {
                "countedPercentage": parse_float(voting.get("progress")),
                "eligibleVoters": eligible_voters,
                "voterTurnout": voter_turnout,
                "voterTurnoutPercentage": percentage_of(voter_turnout, eligible_voters),
                "validBallots": valid_ballots,
                "invalidBallots": invalid_ballots,
                "abstainedBallots": abstained_ballots,
                "lastUpdatedAt": last_updated_at,
                "% บัตรเสีย": percentage_of(invalid_ballots, voter_turnout),
            }
        )
        constituency.update(optional_summary_fields)
        constituency["candidates"] = candidates
        constituencies.append(constituency)

    return {
        "schemaVersion": "1.0",
        "resource": "constituency-bangkok",
        "generatedAt": generated_at or utc_now_iso(),
        "data": {
            "constituencies": constituencies,
        },
    }
