from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from typing import Any
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse


@dataclass(frozen=True)
class Settings:
    bucket: str
    region: str
    prefix: str
    api_key: str | None
    cors_origins: list[str]
    source_election_id: str
    candidates_manifest_url: str
    candidates_featured_url: str
    candidates_timeout_seconds: float
    candidates_cache_seconds: int
    districts_url: str
    districts_timeout_seconds: float
    districts_cache_seconds: int
    election_id: str
    election_title: str
    result_status: str
    delayed_after_minutes: int
    static_results_prefix: str


def load_settings() -> Settings:
    bucket = os.environ.get("RESULTS_API_S3_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("RESULTS_API_S3_BUCKET is required")
    return Settings(
        bucket=bucket,
        region=os.environ.get("RESULTS_API_AWS_REGION", "ap-southeast-1").strip(),
        prefix=os.environ.get("RESULTS_API_S3_PREFIX", "").strip().strip("/"),
        api_key=os.environ.get("RESULTS_API_KEY", "").strip() or None,
        cors_origins=[
            value.strip()
            for value in os.environ.get("RESULTS_API_CORS_ORIGINS", "*").split(",")
            if value.strip()
        ],
        source_election_id=os.environ.get("RESULTS_API_SOURCE_ELECTION_ID", "default").strip() or "default",
        candidates_manifest_url=os.environ.get(
            "RESULTS_API_CANDIDATES_MANIFEST_URL",
            "https://w3-dev.ch7.com/api-data/candidates/manifest.json",
        ).strip(),
        candidates_featured_url=os.environ.get(
            "RESULTS_API_CANDIDATES_FEATURED_URL",
            "https://w3-dev.ch7.com/api-data/candidates/featured.json",
        ).strip(),
        candidates_timeout_seconds=max(
            0.1,
            float(os.environ.get("RESULTS_API_CANDIDATES_TIMEOUT_SECONDS", "5")),
        ),
        candidates_cache_seconds=max(
            0,
            int(os.environ.get("RESULTS_API_CANDIDATES_CACHE_SECONDS", "300")),
        ),
        districts_url=os.environ.get(
            "RESULTS_API_DISTRICTS_URL",
            "https://w3-dev.ch7.com/api-data/master-data/districts.json",
        ).strip(),
        districts_timeout_seconds=max(
            0.1,
            float(os.environ.get("RESULTS_API_DISTRICTS_TIMEOUT_SECONDS", "5")),
        ),
        districts_cache_seconds=max(
            0,
            int(os.environ.get("RESULTS_API_DISTRICTS_CACHE_SECONDS", "300")),
        ),
        election_id=os.environ.get(
            "RESULTS_API_ELECTION_ID",
            "bkk-governor-2026",
        ).strip(),
        election_title=os.environ.get(
            "RESULTS_API_ELECTION_TITLE",
            "ผลการเลือกตั้งผู้ว่าฯ กรุงเทพมหานคร",
        ).strip(),
        result_status=os.environ.get(
            "RESULTS_API_RESULT_STATUS",
            "LIVE_COUNT",
        ).strip(),
        delayed_after_minutes=max(
            1,
            int(os.environ.get("RESULTS_API_DELAYED_AFTER_MINUTES", "30")),
        ),
        static_results_prefix=os.environ.get(
            "RESULTS_API_STATIC_RESULTS_PREFIX",
            "api-data/governor-results",
        ).strip().strip("/"),
    )


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


def normalize_party(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "id": value.get("id"),
        "name": value.get("name"),
        "color": value.get("color"),
        "logoUrl": value.get("logoUrl"),
    }


def candidate_party_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    party = normalize_party(metadata.get("party")) or {}
    return {
        "id": party.get("id"),
        "name": party.get("name"),
        "color": party.get("color"),
        "logoUrl": party.get("logoUrl"),
    }


def percentage_of(value: int | None, total: int | None) -> float | None:
    if value is None or not total:
        return None
    return round(value / total * 100, 2)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


class ResultsStore:
    def __init__(self, *, s3_client: Any, bucket: str, prefix: str = "") -> None:
        self.s3_client = s3_client
        self.bucket = bucket
        self.prefix = prefix.strip().strip("/")

    def key(self, relative_key: str) -> str:
        normalized = relative_key.strip().lstrip("/")
        return f"{self.prefix}/{normalized}" if self.prefix else normalized

    def read_json(self, relative_key: str) -> dict[str, Any] | None:
        try:
            response = self.s3_client.get_object(Bucket=self.bucket, Key=self.key(relative_key))
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", "")).lower()
            if code in {"nosuchkey", "404", "notfound"}:
                return None
            raise
        return json.loads(response["Body"].read().decode("utf-8"))

    def write_json(self, relative_key: str, payload: dict[str, Any]) -> None:
        self.s3_client.put_object(
            Bucket=self.bucket,
            Key=self.key(relative_key),
            Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json; charset=utf-8",
        )

    def write_absolute_json(self, key: str, payload: dict[str, Any]) -> None:
        self.s3_client.put_object(
            Bucket=self.bucket,
            Key=key.strip().lstrip("/"),
            Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json; charset=utf-8",
        )

    def list_json_objects(self, prefix: str, *, limit: int = 50) -> list[dict[str, Any]]:
        normalized_prefix = prefix.strip().strip("/")
        s3_prefix = self.key(f"{normalized_prefix}/" if normalized_prefix else "")
        paginator = self.s3_client.get_paginator("list_objects_v2")
        relative_keys = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=s3_prefix):
            for item in page.get("Contents", []):
                key = str(item.get("Key") or "")
                if not key.endswith(".json"):
                    continue
                relative_key = key
                if self.prefix and relative_key.startswith(f"{self.prefix}/"):
                    relative_key = relative_key[len(self.prefix) + 1 :]
                relative_keys.append(relative_key)
        items = []
        for key in sorted(relative_keys, reverse=True)[:limit]:
            payload = self.read_json(key)
            if payload:
                items.append(payload)
        return items

    def list_area_indexes(self, election_id: str) -> list[str]:
        prefix = self.key(f"indexes/by-area/{election_id}/")
        paginator = self.s3_client.get_paginator("list_objects_v2")
        area_ids: set[str] = set()
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                key = str(item.get("Key") or "")
                suffix = key[len(prefix) :]
                parts = suffix.split("/")
                if len(parts) == 2 and parts[1] == "submissions.json":
                    area_ids.add(parts[0])
        return sorted(area_ids, key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value))

    def area_submissions(self, election_id: str, area_id: str) -> dict[str, Any] | None:
        return self.read_json(f"indexes/by-area/{election_id}/{area_id}/submissions.json")

    def approved_result(self, source_message_id: str) -> dict[str, Any] | None:
        manifest = self.read_json(f"messages/{source_message_id}/manifest.json")
        if not manifest or manifest.get("state") not in {"approved", "updated"}:
            return None

        approval_key = str(manifest.get("current_approval_key") or "").strip()
        draft_key = str(manifest.get("current_draft_key") or "").strip()
        if not approval_key or not draft_key:
            return None

        approval = self.read_json(approval_key)
        draft = self.read_json(draft_key)
        if not approval or approval.get("state") != "approved" or not draft:
            return None

        return without_nulls({
            "source_message_id": source_message_id,
            "election_id": draft.get("election_id"),
            "area_id": draft.get("area_id"),
            "polling_unit_id": draft.get("polling_unit_id"),
            "report_type": draft.get("report_type"),
            "candidate_scores": [
                without_nulls({
                    "candidate_number": item.get("candidate_number"),
                    "candidate_name": item.get("candidate_name"),
                    "score": item.get("score"),
                    "confidence": item.get("confidence"),
                })
                for item in draft.get("candidate_scores", [])
                if isinstance(item, dict)
            ],
            "overall_confidence": draft.get("overall_confidence"),
            "validation_flags": draft.get("validation_flags") or [],
            "eligible_voters": draft.get("eligible_voters"),
            "voter_turnout": draft.get("voter_turnout"),
            "valid_ballots": draft.get("valid_ballots"),
            "invalid_ballots": draft.get("invalid_ballots"),
            "abstained_ballots": draft.get("vote_no") if draft.get("vote_no") is not None else draft.get("abstained_ballots"),
            "approved_at": approval.get("responded_at") or approval.get("updated_at"),
            "submitted_at": manifest.get("created_at"),
            "updated_at": manifest.get("updated_at"),
        })

    def approved_results_for_area(self, election_id: str, area_id: str) -> list[dict[str, Any]]:
        index = self.area_submissions(election_id, area_id)
        if not index:
            return []

        results = []
        for submission in index.get("submissions", []):
            source_message_id = str(submission.get("source_message_id") or "").strip()
            if not source_message_id:
                continue
            result = self.approved_result(source_message_id)
            if result and str(result.get("area_id")) == area_id:
                results.append(result)
        return sorted(results, key=lambda item: str(item.get("approved_at") or ""), reverse=True)

    def latest_approved_results_by_area(self, election_id: str) -> list[dict[str, Any]]:
        results = []
        for area_id in self.list_area_indexes(election_id):
            approved_results = self.approved_results_for_area(election_id, area_id)
            if approved_results:
                results.append(approved_results[0])
        return results


class CandidateCatalog:
    def __init__(
        self,
        *,
        manifest_url: str,
        featured_url: str,
        timeout_seconds: float = 5,
        cache_seconds: int = 300,
    ) -> None:
        self.manifest_url = manifest_url
        self.featured_url = featured_url
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        self._cached_at = 0.0
        self._cached_candidates: dict[int, dict[str, Any]] = {}
        self._lock = Lock()

    def _read_json_url(self, url: str) -> dict[str, Any]:
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "election-results-api/1.0"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def candidates_by_number(self) -> dict[int, dict[str, Any]]:
        now = monotonic()
        if self._cached_candidates and now - self._cached_at < self.cache_seconds:
            return self._cached_candidates

        with self._lock:
            now = monotonic()
            if self._cached_candidates and now - self._cached_at < self.cache_seconds:
                return self._cached_candidates

            manifest = self._read_json_url(self.manifest_url)
            featured = self._read_json_url(self.featured_url)
            candidates: dict[int, dict[str, Any]] = {}

            for profile in manifest.get("profiles", []):
                if not isinstance(profile, dict):
                    continue
                try:
                    candidate_number = int(profile.get("candidateNumber"))
                except (TypeError, ValueError):
                    continue
                candidates[candidate_number] = without_nulls(
                    {
                        "candidateId": profile.get("id"),
                        "name": profile.get("name"),
                    }
                )

            for candidate in featured.get("candidates", []):
                if not isinstance(candidate, dict):
                    continue
                try:
                    candidate_number = int(candidate.get("candidateNumber"))
                except (TypeError, ValueError):
                    continue
                item = candidates.setdefault(candidate_number, {})
                if not item.get("candidateId") and candidate.get("id"):
                    item["candidateId"] = candidate["id"]
                if not item.get("name") and candidate.get("name"):
                    item["name"] = candidate["name"]
                if candidate.get("themeColor"):
                    item["color"] = candidate["themeColor"]
                if candidate.get("candidateSrc"):
                    item["candidateSrc"] = candidate["candidateSrc"]
                if candidate.get("backgroundSrc"):
                    item["backgroundSrc"] = candidate["backgroundSrc"]
                party = normalize_party(candidate.get("party"))
                if party:
                    item["party"] = party

            self._cached_candidates = candidates
            self._cached_at = monotonic()
            return candidates


class DistrictCatalog:
    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: float = 5,
        cache_seconds: int = 300,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        self._cached_at = 0.0
        self._cached_districts: list[dict[str, Any]] = []
        self._lock = Lock()

    def _read_json_url(self) -> list[dict[str, Any]]:
        request = Request(self.url, headers={"Accept": "application/json", "User-Agent": "election-results-api/1.0"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, list) else []

    def districts(self) -> list[dict[str, Any]]:
        now = monotonic()
        if self._cached_districts and now - self._cached_at < self.cache_seconds:
            return self._cached_districts
        with self._lock:
            now = monotonic()
            if self._cached_districts and now - self._cached_at < self.cache_seconds:
                return self._cached_districts
            self._cached_districts = [
                without_nulls(item)
                for item in self._read_json_url()
                if isinstance(item, dict)
            ]
            self._cached_at = monotonic()
            return self._cached_districts

    def districts_by_id(self) -> dict[str, dict[str, Any]]:
        return {
            str(item["id"]): item
            for item in self.districts()
            if item.get("id") is not None
        }


def build_governor_results(
    *,
    approved_results: list[dict[str, Any]],
    candidate_catalog: dict[int, dict[str, Any]] | None = None,
    election_id: str = "bkk-governor-2026",
    title: str = "ผลการเลือกตั้งผู้ว่าฯ กรุงเทพมหานคร",
    result_status: str = "LIVE_COUNT",
    total_units: int | None = None,
    delayed_after_minutes: int = 30,
    generated_at: str | None = None,
) -> dict[str, Any]:
    vote_totals: dict[int, int] = {}
    candidate_names: dict[int, str] = {}
    last_updated_at = None
    for result in approved_results:
        result_updated_at = str(result.get("approved_at") or result.get("updated_at") or "")
        if result_updated_at and (last_updated_at is None or result_updated_at > last_updated_at):
            last_updated_at = result_updated_at
        for score in result.get("candidate_scores", []):
            try:
                candidate_number = int(score.get("candidate_number"))
                candidate_score = int(score.get("score"))
            except (TypeError, ValueError):
                continue
            vote_totals[candidate_number] = vote_totals.get(candidate_number, 0) + candidate_score
            candidate_name = str(score.get("candidate_name") or "").strip()
            if candidate_name:
                candidate_names[candidate_number] = candidate_name

    total_votes = sum(vote_totals.values())
    catalog = candidate_catalog or {}
    candidates = [
        {
            "candidateId": catalog.get(candidate_number, {}).get("candidateId"),
            "candidateNumber": candidate_number,
            "name": catalog.get(candidate_number, {}).get("name") or candidate_names.get(candidate_number),
            "candidateSrc": catalog.get(candidate_number, {}).get("candidateSrc"),
            "color": catalog.get(candidate_number, {}).get("color"),
            "voteCount": vote_count,
            "votePercentage": round(vote_count / total_votes * 100, 2) if total_votes else 0,
            "backgroundSrc": catalog.get(candidate_number, {}).get("backgroundSrc"),
            "party": candidate_party_payload(catalog.get(candidate_number, {})),
        }
        for candidate_number, vote_count in vote_totals.items()
    ]
    candidates.sort(key=lambda item: (-item["voteCount"], item["candidateNumber"]))
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
        candidate["isLeading"] = rank == 1

    counted_units = len(approved_results)
    counted_percentage = (
        round(counted_units / total_units * 100, 2)
        if total_units
        else None
    )

    aggregate_fields = {
        "eligibleVoters": "eligible_voters",
        "voterTurnout": "voter_turnout",
        "validBallots": "valid_ballots",
        "invalidBallots": "invalid_ballots",
        "abstainedBallots": "abstained_ballots",
    }
    aggregates: dict[str, int | None] = {}
    warnings = []
    if total_units is None:
        warnings.append("totalUnits is unavailable because district master data could not be loaded.")
    for response_field, source_field in aggregate_fields.items():
        values = []
        for result in approved_results:
            value = result.get(source_field)
            if value is None:
                values = []
                break
            try:
                values.append(int(value))
            except (TypeError, ValueError):
                values = []
                break
        aggregates[response_field] = sum(values) if values and len(values) == counted_units else None
        if counted_units and aggregates[response_field] is None:
            warnings.append(f"{response_field} is unavailable or incomplete in approved results.")
    for candidate in candidates:
        missing_metadata = [
            field
            for field in ("candidateId", "name", "color")
            if candidate[field] is None
        ]
        if missing_metadata:
            warnings.append(
                f"Candidate {candidate['candidateNumber']} is missing metadata: {', '.join(missing_metadata)}."
            )

    eligible_voters = aggregates["eligibleVoters"]
    voter_turnout = aggregates["voterTurnout"]
    voter_turnout_percentage = (
        round(voter_turnout / eligible_voters * 100, 2)
        if eligible_voters and voter_turnout is not None
        else None
    )
    valid_ballots = aggregates["validBallots"]
    invalid_ballots = aggregates["invalidBallots"]
    abstained_ballots = aggregates["abstainedBallots"]

    generated_timestamp = generated_at or utc_now_iso()
    is_delayed = None
    if last_updated_at:
        try:
            last_updated = datetime.fromisoformat(last_updated_at.replace("Z", "+00:00"))
            generated_datetime = datetime.fromisoformat(generated_timestamp.replace("Z", "+00:00"))
            is_delayed = (generated_datetime - last_updated).total_seconds() > delayed_after_minutes * 60
        except ValueError:
            warnings.append("lastUpdatedAt is not a valid ISO 8601 timestamp.")

    return {
        "schemaVersion": "1.0",
        "resource": "governor-results",
        "pageMeta": {
            "electionId": election_id,
            "title": title,
            "resultStatus": result_status,
            "generatedAt": generated_timestamp,
        },
        "summary": {
            "countedUnits": counted_units,
            "totalUnits": total_units,
            "countedPercentage": counted_percentage,
            "eligibleVoters": eligible_voters,
            "voterTurnout": voter_turnout,
            "voterTurnoutPercentage": voter_turnout_percentage,
            "validBallots": valid_ballots,
            "invalidBallots": invalid_ballots,
            "abstainedBallots": abstained_ballots,
            "validBallotsPercentage": percentage_of(valid_ballots, voter_turnout),
            "invalidBallotsPercentage": percentage_of(invalid_ballots, voter_turnout),
            "abstainedBallotsPercentage": percentage_of(abstained_ballots, voter_turnout),
            "lastUpdatedAt": last_updated_at,
        },
        "candidates": candidates,
        "dataQuality": {
            "isComplete": counted_units >= total_units if total_units is not None else None,
            "isDelayed": is_delayed,
            "warnings": warnings,
        },
    }


def build_district_results(
    *,
    approved_results: list[dict[str, Any]],
    candidate_catalog: dict[int, dict[str, Any]],
    district_catalog: dict[str, dict[str, Any]],
    generated_at: str | None = None,
) -> dict[str, Any]:
    results_by_area = {
        str(result.get("area_id")): result
        for result in approved_results
        if result.get("area_id") is not None
    }
    constituencies = []
    bangkok_districts = [
        district
        for district in district_catalog.values()
        if district.get("provinceCode") == 10
    ]
    bangkok_districts.sort(key=lambda district: int(district.get("id") or 0))

    for district in bangkok_districts:
        area_id = str(district.get("id"))
        result = results_by_area.get(area_id)
        scores = []
        total_votes = 0
        for score in (result or {}).get("candidate_scores", []):
            try:
                candidate_number = int(score.get("candidate_number"))
                vote_count = int(score.get("score"))
            except (TypeError, ValueError):
                continue
            total_votes += vote_count
            metadata = candidate_catalog.get(candidate_number, {})
            scores.append(
                {
                    "candidateId": metadata.get("candidateId"),
                    "candidateNumber": candidate_number,
                    "name": metadata.get("name") or score.get("candidate_name"),
                    "candidateSrc": metadata.get("candidateSrc"),
                    "color": metadata.get("color"),
                    "voteCount": vote_count,
                }
            )

        scores.sort(key=lambda item: (-item["voteCount"], item["candidateNumber"]))
        for rank, candidate in enumerate(scores, start=1):
            candidate["votePercentage"] = (
                round(candidate["voteCount"] / total_votes * 100, 2)
                if total_votes
                else 0
            )
            candidate["rank"] = rank
            candidate["isLeading"] = rank == 1

        constituencies.append(
            {
                "areaId": area_id,
                "number": district.get("id"),
                "name": district.get("districtNameTh"),
                "leadingCandidateId": scores[0].get("candidateId") if scores else None,
                "candidates": scores,
            }
        )

    return {
        "schemaVersion": "1.0",
        "resource": "constituency-bangkok",
        "generatedAt": generated_at or utc_now_iso(),
        "data": {"constituencies": constituencies},
    }


MONITOR_REQUIRED_RESULT_FIELDS = (
    "candidate_scores",
    "eligible_voters",
    "voter_turnout",
    "valid_ballots",
    "invalid_ballots",
    "abstained_ballots",
)


def monitor_missing_fields(result: dict[str, Any] | None) -> list[str]:
    if not result:
        return list(MONITOR_REQUIRED_RESULT_FIELDS)

    missing = []
    for field in MONITOR_REQUIRED_RESULT_FIELDS:
        value = result.get(field)
        if field == "candidate_scores":
            if not isinstance(value, list) or not value:
                missing.append(field)
        elif value is None:
            missing.append(field)
    return missing


def monitor_validation_warnings(result: dict[str, Any] | None) -> list[str]:
    if not result:
        return []

    warnings = []
    scores = result.get("candidate_scores")
    if isinstance(scores, list):
        for index, score in enumerate(scores, start=1):
            if not isinstance(score, dict):
                warnings.append(f"candidate_scores[{index}] is not an object.")
                continue
            if score.get("candidate_number") is None:
                warnings.append(f"candidate_scores[{index}].candidate_number is missing.")
            if score.get("score") is None:
                warnings.append(f"candidate_scores[{index}].score is missing.")
            else:
                try:
                    if int(score["score"]) < 0:
                        warnings.append(f"candidate_scores[{index}].score is negative.")
                except (TypeError, ValueError):
                    warnings.append(f"candidate_scores[{index}].score is not a valid integer.")

    try:
        voter_turnout = int(result["voter_turnout"]) if result.get("voter_turnout") is not None else None
        valid_ballots = int(result["valid_ballots"]) if result.get("valid_ballots") is not None else None
        invalid_ballots = int(result["invalid_ballots"]) if result.get("invalid_ballots") is not None else None
        abstained_ballots = int(result["abstained_ballots"]) if result.get("abstained_ballots") is not None else None
    except (TypeError, ValueError):
        warnings.append("Turnout or ballot fields contain a non-integer value.")
        voter_turnout = valid_ballots = invalid_ballots = abstained_ballots = None

    if all(value is not None for value in (voter_turnout, valid_ballots, invalid_ballots, abstained_ballots)):
        ballot_total = valid_ballots + invalid_ballots + abstained_ballots
        if voter_turnout != ballot_total:
            warnings.append("voter_turnout does not equal valid_ballots + invalid_ballots + abstained_ballots.")

    try:
        eligible_voters = int(result["eligible_voters"]) if result.get("eligible_voters") is not None else None
    except (TypeError, ValueError):
        warnings.append("eligible_voters is not a valid integer.")
        eligible_voters = None
    if eligible_voters is not None and voter_turnout is not None and voter_turnout > eligible_voters:
        warnings.append("voter_turnout exceeds eligible_voters.")

    return warnings


def latest_submission_timestamp(index: dict[str, Any] | None) -> str | None:
    if not index:
        return None
    timestamps = [
        str(item.get("submitted_at") or "").strip()
        for item in index.get("submissions", [])
        if isinstance(item, dict) and item.get("submitted_at")
    ]
    if index.get("updated_at"):
        timestamps.append(str(index["updated_at"]))
    return max(timestamps) if timestamps else None


def leading_candidate_id(
    result: dict[str, Any] | None,
    candidate_catalog: dict[int, dict[str, Any]] | None,
) -> str | None:
    if not result:
        return None
    scores = []
    for score in result.get("candidate_scores", []):
        try:
            candidate_number = int(score.get("candidate_number"))
            vote_count = int(score.get("score"))
        except (AttributeError, TypeError, ValueError):
            continue
        scores.append((vote_count, candidate_number))
    if not scores:
        return None
    _, candidate_number = sorted(scores, key=lambda item: (-item[0], item[1]))[0]
    metadata = (candidate_catalog or {}).get(candidate_number, {})
    return metadata.get("candidateId") or str(candidate_number)


def build_monitor_districts(
    *,
    district_catalog: dict[str, dict[str, Any]],
    area_indexes: dict[str, dict[str, Any]],
    approved_results_by_area: dict[str, list[dict[str, Any]]],
    raw_approved_results_by_area: dict[str, list[dict[str, Any]]] | None = None,
    candidate_catalog: dict[int, dict[str, Any]] | None = None,
    election_id: str = "bkk-governor-2026",
    delayed_after_minutes: int = 30,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_timestamp = generated_at or utc_now_iso()
    generated_datetime = parse_iso_datetime(generated_timestamp)
    districts = [
        district
        for district in district_catalog.values()
        if district.get("provinceCode") == 10
    ]
    districts.sort(key=lambda district: int(district.get("id") or 0))

    monitor_districts = []
    for district in districts:
        area_id = str(district.get("id"))
        index = area_indexes.get(area_id) or {}
        approved_results = approved_results_by_area.get(area_id) or []
        raw_approved_results = (raw_approved_results_by_area or {}).get(area_id) or approved_results
        latest_result = approved_results[0] if approved_results else None
        latest_raw_result = raw_approved_results[0] if raw_approved_results else latest_result
        missing_fields = monitor_missing_fields(latest_result) if latest_result else []
        warnings = monitor_validation_warnings(latest_result)
        submission_count = int(index.get("submission_count") or len(index.get("submissions", []) or []))
        approved_submission_count = len(raw_approved_results)
        latest_approved_at = latest_raw_result.get("approved_at") if latest_raw_result else None
        latest_approved_datetime = parse_iso_datetime(latest_approved_at)
        is_delayed = (
            bool(generated_datetime and latest_approved_datetime)
            and (generated_datetime - latest_approved_datetime).total_seconds() > delayed_after_minutes * 60
        )

        if submission_count == 0 and approved_submission_count == 0:
            status = "no_data"
        elif approved_submission_count == 0:
            status = "pending"
        elif missing_fields:
            status = "missing_fields"
        elif warnings:
            status = "conflict"
        elif is_delayed:
            status = "delayed"
        else:
            status = "complete"

        monitor_districts.append(
            without_nulls(
                {
                    "areaId": area_id,
                    "districtCode": district.get("districtCode"),
                    "districtNameTh": district.get("districtNameTh"),
                    "districtNameEn": district.get("districtNameEn"),
                    "submissionCount": submission_count,
                    "approvedSubmissionCount": approved_submission_count,
                    "latestSubmittedAt": latest_submission_timestamp(index),
                    "latestApprovedAt": latest_approved_at,
                    "status": status,
                    "missingFields": missing_fields,
                    "warnings": warnings,
                    "leadingCandidateId": leading_candidate_id(latest_result, candidate_catalog),
                }
            )
        )

    return {
        "schemaVersion": "1.0",
        "resource": "election-monitor-districts",
        "generatedAt": generated_timestamp,
        "electionId": election_id,
        "districts": monitor_districts,
    }


def build_monitor_overview(
    *,
    monitor_districts: list[dict[str, Any]],
    election_id: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_timestamp = generated_at or utc_now_iso()
    total_districts = len(monitor_districts)
    districts_with_data = sum(1 for district in monitor_districts if district.get("submissionCount", 0) > 0)
    complete_districts = sum(1 for district in monitor_districts if district.get("status") == "complete")
    delayed_districts = sum(1 for district in monitor_districts if district.get("status") == "delayed")
    conflict_districts = sum(1 for district in monitor_districts if district.get("status") == "conflict")
    latest_approved_values = [
        str(district.get("latestApprovedAt"))
        for district in monitor_districts
        if district.get("latestApprovedAt")
    ]
    warnings = []
    if total_districts and complete_districts < total_districts:
        warnings.append("Some districts are incomplete or require operator review.")

    return {
        "schemaVersion": "1.0",
        "resource": "election-monitor",
        "generatedAt": generated_timestamp,
        "electionId": election_id,
        "overview": {
            "totalDistricts": total_districts,
            "districtsWithData": districts_with_data,
            "districtsWithoutData": total_districts - districts_with_data,
            "completeDistricts": complete_districts,
            "incompleteDistricts": total_districts - complete_districts,
            "delayedDistricts": delayed_districts,
            "conflictDistricts": conflict_districts,
            "latestApprovedAt": max(latest_approved_values) if latest_approved_values else None,
        },
        "dataQuality": {
            "isComplete": total_districts > 0 and complete_districts == total_districts,
            "warnings": warnings,
        },
    }


settings = load_settings()
store = ResultsStore(
    s3_client=boto3.client("s3", region_name=settings.region),
    bucket=settings.bucket,
    prefix=settings.prefix,
)
candidate_catalog = CandidateCatalog(
    manifest_url=settings.candidates_manifest_url,
    featured_url=settings.candidates_featured_url,
    timeout_seconds=settings.candidates_timeout_seconds,
    cache_seconds=settings.candidates_cache_seconds,
)
district_catalog = DistrictCatalog(
    url=settings.districts_url,
    timeout_seconds=settings.districts_timeout_seconds,
    cache_seconds=settings.districts_cache_seconds,
)
app = FastAPI(title="Election Results API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "PATCH", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)
_monitor_cache_lock = Lock()
_monitor_cache_at = 0.0
_monitor_cache_payload: dict[str, Any] | None = None
_monitor_cache_seconds = 10.0
_governor_results_cache_lock = Lock()
_governor_results_cache_at = 0.0
_governor_results_cache_payload: dict[str, Any] | None = None
_governor_results_cache_seconds = 10.0
PAGE_META_OVERRIDE_KEY = "monitor/overrides/page-meta.json"
DATA_MODE_OVERRIDE_KEY = "monitor/overrides/data-mode.json"
DISTRICT_SUMMARY_OVERRIDE_PREFIX = "monitor/overrides/districts"
DISTRICT_ROUND_OVERRIDE_PREFIX = "monitor/overrides/district-rounds"
MONITOR_AUDIT_PREFIX = "monitor/audit"
ALLOWED_RESULT_STATUSES = {"LIVE_COUNT", "OFFICIAL", "PAUSED", "DELAYED"}
DATA_INTERPRETATION_MODES = {
    "latest_snapshot": "Use only the latest approved report per district.",
    "incremental_delta": "Sum every approved report in each district as incremental deltas.",
}
SUMMARY_OVERRIDE_FIELDS = (
    "eligibleVoters",
    "voterTurnout",
    "validBallots",
    "invalidBallots",
    "abstainedBallots",
)
SUMMARY_FIELD_TO_RESULT_FIELD = {
    "eligibleVoters": "eligible_voters",
    "voterTurnout": "voter_turnout",
    "validBallots": "valid_ballots",
    "invalidBallots": "invalid_ballots",
    "abstainedBallots": "abstained_ballots",
}


def invalidate_result_caches() -> None:
    global _monitor_cache_at, _monitor_cache_payload, _governor_results_cache_at, _governor_results_cache_payload
    _monitor_cache_payload = None
    _monitor_cache_at = 0.0
    _governor_results_cache_payload = None
    _governor_results_cache_at = 0.0


def monitor_storage_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail=f"S3 data is unavailable. Check AWS credentials/session and RESULTS_API_S3_BUCKET. {exc}",
    )


def audit_timestamp() -> tuple[str, str, str, str]:
    now = datetime.now(timezone.utc)
    created_at = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return created_at, now.strftime("%Y"), now.strftime("%m"), now.strftime("%d")


def read_page_meta_override() -> dict[str, Any]:
    return store.read_json(PAGE_META_OVERRIDE_KEY) or {}


def read_data_mode_override() -> dict[str, Any]:
    return store.read_json(DATA_MODE_OVERRIDE_KEY) or {}


def current_data_mode() -> str:
    mode = str(read_data_mode_override().get("mode") or "latest_snapshot").strip()
    return mode if mode in DATA_INTERPRETATION_MODES else "latest_snapshot"


def data_mode_options() -> list[dict[str, str]]:
    return [
        {"mode": mode, "description": description}
        for mode, description in DATA_INTERPRETATION_MODES.items()
    ]


def district_summary_override_key(area_id: str) -> str:
    return f"{DISTRICT_SUMMARY_OVERRIDE_PREFIX}/{area_id}/summary.json"


def read_district_summary_override(area_id: str) -> dict[str, Any]:
    return store.read_json(district_summary_override_key(area_id)) or {}


def district_round_overrides_key(area_id: str) -> str:
    return f"{DISTRICT_ROUND_OVERRIDE_PREFIX}/{area_id}/rounds.json"


def read_district_round_overrides(area_id: str) -> dict[str, Any]:
    return store.read_json(district_round_overrides_key(area_id)) or {"rounds": {}}


def round_id_for_result(result: dict[str, Any]) -> str:
    source_id = str(result.get("source_message_id") or "").strip()
    if source_id:
        return f"source:{source_id}"
    approved_at = str(result.get("approved_at") or result.get("updated_at") or "").strip()
    return f"round:{approved_at or 'unknown'}"


def result_to_round(result: dict[str, Any], index: int) -> dict[str, Any]:
    return without_nulls(
        {
            "roundId": round_id_for_result(result),
            "sourceMessageId": result.get("source_message_id"),
            "areaId": str(result.get("area_id") or ""),
            "position": (index + 1) * 1000,
            "reportedAt": result.get("approved_at") or result.get("updated_at") or result.get("submitted_at"),
            "sourceType": "approved_result",
            "deleted": False,
            "candidateScores": result.get("candidate_scores") or [],
            "eligibleVoters": result.get("eligible_voters"),
            "voterTurnout": result.get("voter_turnout"),
            "validBallots": result.get("valid_ballots"),
            "invalidBallots": result.get("invalid_ballots"),
            "abstainedBallots": result.get("abstained_ballots"),
            "rawResult": result,
        }
    )


def round_to_result(round_item: dict[str, Any]) -> dict[str, Any]:
    base = dict(round_item.get("rawResult") or {})
    base.update(
        without_nulls(
            {
                "source_message_id": round_item.get("sourceMessageId") or round_item.get("roundId"),
                "area_id": round_item.get("areaId"),
                "approved_at": round_item.get("reportedAt"),
                "candidate_scores": round_item.get("candidateScores") or [],
                "eligible_voters": round_item.get("eligibleVoters"),
                "voter_turnout": round_item.get("voterTurnout"),
                "valid_ballots": round_item.get("validBallots"),
                "invalid_ballots": round_item.get("invalidBallots"),
                "abstained_ballots": round_item.get("abstainedBallots"),
                "round_id": round_item.get("roundId"),
                "round_position": round_item.get("position"),
                "round_source_type": round_item.get("sourceType"),
            }
        )
    )
    return without_nulls(base)


def apply_round_overrides(area_id: str, approved_results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rounds = {
        item["roundId"]: item
        for index, result in enumerate(approved_results)
        for item in [result_to_round(result, index)]
    }
    overrides = read_district_round_overrides(area_id).get("rounds") or {}
    for round_id, override in overrides.items():
        if not isinstance(override, dict):
            continue
        existing = rounds.get(round_id, {"roundId": round_id, "areaId": area_id, "sourceType": "manual_round"})
        rounds[round_id] = without_nulls({**existing, **override, "roundId": round_id, "areaId": area_id})

    ordered_rounds = sorted(
        rounds.values(),
        key=lambda item: (
            int(item.get("position") or 0),
            str(item.get("reportedAt") or ""),
            str(item.get("roundId") or ""),
        ),
    )
    effective_rounds = [item for item in ordered_rounds if not item.get("deleted")]
    return ordered_rounds, [round_to_result(item) for item in effective_rounds]


def apply_district_summary_overrides(approved_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    updated_results = []
    for result in approved_results:
        area_id = str(result.get("area_id") or "").strip()
        if not area_id:
            updated_results.append(result)
            continue
        override = read_district_summary_override(area_id)
        if not override:
            updated_results.append(result)
            continue
        updated = dict(result)
        for response_field, source_field in SUMMARY_FIELD_TO_RESULT_FIELD.items():
            if response_field in override:
                updated[source_field] = override[response_field]
        updated_results.append(updated)
    return updated_results


def aggregate_incremental_area_results(approved_results: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not approved_results:
        return None
    latest = dict(approved_results[0])
    score_totals: dict[int, dict[str, Any]] = {}
    for result in approved_results:
        for score in result.get("candidate_scores", []):
            try:
                candidate_number = int(score.get("candidate_number"))
                score_value = int(score.get("score"))
            except (AttributeError, TypeError, ValueError):
                continue
            existing = score_totals.setdefault(candidate_number, {"candidate_number": candidate_number, "score": 0})
            existing["score"] += score_value
            if score.get("candidate_name"):
                existing["candidate_name"] = score.get("candidate_name")
            if score.get("confidence") is not None:
                existing["confidence"] = score.get("confidence")

    latest["candidate_scores"] = [
        score_totals[candidate_number]
        for candidate_number in sorted(score_totals)
    ]
    for field in SUMMARY_FIELD_TO_RESULT_FIELD.values():
        values = []
        for result in approved_results:
            value = result.get(field)
            if value is None:
                values = []
                break
            try:
                values.append(int(value))
            except (TypeError, ValueError):
                values = []
                break
        if values:
            latest[field] = sum(values)
    latest["data_interpretation_mode"] = "incremental_delta"
    latest["included_report_count"] = len(approved_results)
    return without_nulls(latest)


def interpreted_area_result(approved_results: list[dict[str, Any]], mode: str) -> dict[str, Any] | None:
    if not approved_results:
        return None
    if mode == "incremental_delta":
        return aggregate_incremental_area_results(approved_results)
    latest = dict(approved_results[0])
    latest["data_interpretation_mode"] = mode
    latest["included_report_count"] = 1
    return without_nulls(latest)


def interpreted_results_by_area(approved_results_by_area: dict[str, list[dict[str, Any]]], mode: str) -> dict[str, list[dict[str, Any]]]:
    interpreted: dict[str, list[dict[str, Any]]] = {}
    for area_id, approved_results in approved_results_by_area.items():
        result = interpreted_area_result(approved_results, mode)
        interpreted[area_id] = [result] if result else []
    return interpreted


def interpreted_public_results(mode: str) -> list[dict[str, Any]]:
    results = []
    for area_id in store.list_area_indexes(settings.source_election_id):
        _, effective_results = apply_round_overrides(
            area_id,
            store.approved_results_for_area(settings.source_election_id, area_id),
        )
        approved_results = apply_district_summary_overrides(effective_results)
        result = interpreted_area_result(approved_results, mode)
        if result:
            results.append(result)
    return results


def monitor_overrides_response() -> dict[str, Any]:
    try:
        return {
            "schemaVersion": "1.0",
            "resource": "election-monitor-overrides",
            "electionId": settings.election_id,
            "pageMeta": read_page_meta_override(),
            "dataMode": read_data_mode_override(),
            "dataModeOptions": data_mode_options(),
            "districtSummaries": store.list_json_objects(DISTRICT_SUMMARY_OVERRIDE_PREFIX, limit=200),
        }
    except BotoCoreError as exc:
        raise monitor_storage_error(exc) from exc


def normalize_actor_reason(payload: dict[str, Any]) -> tuple[str, str | None]:
    actor = str(payload.get("actor") or "operator").strip() or "operator"
    reason = str(payload.get("reason") or "").strip() or None
    return actor, reason


def write_monitor_audit_event(
    *,
    event_type: str,
    actor: str,
    reason: str | None,
    before: dict[str, Any],
    after: dict[str, Any],
    area_id: str | None = None,
) -> dict[str, Any]:
    created_at, year, month, day = audit_timestamp()
    event_id = f"audit_{created_at.replace('-', '').replace(':', '').replace('.', '')}_{event_type}"
    event = without_nulls(
        {
            "schema_version": "2026-06-16",
            "entity_type": "monitor_audit_event",
            "event_id": event_id,
            "event_type": event_type,
            "election_id": settings.election_id,
            "area_id": area_id,
            "actor": actor,
            "reason": reason,
            "before": before,
            "after": after,
            "created_at": created_at,
        }
    )
    key = f"{MONITOR_AUDIT_PREFIX}/{year}/{month}/{day}/{event_id}.json"
    store.write_json(key, event)
    return event


def validate_page_meta_payload(payload: dict[str, Any]) -> dict[str, str]:
    updates: dict[str, str] = {}
    if "title" in payload:
        title = str(payload.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title must be a non-empty string.")
        updates["title"] = title
    if "resultStatus" in payload:
        result_status = str(payload.get("resultStatus") or "").strip()
        if result_status not in ALLOWED_RESULT_STATUSES:
            raise HTTPException(status_code=400, detail="resultStatus is not supported.")
        updates["resultStatus"] = result_status
    if not updates:
        raise HTTPException(status_code=400, detail="No supported page metadata fields were provided.")
    return updates


def validate_data_mode_payload(payload: dict[str, Any]) -> dict[str, str]:
    mode = str(payload.get("mode") or "").strip()
    if mode not in DATA_INTERPRETATION_MODES:
        raise HTTPException(status_code=400, detail="data mode is not supported.")
    return {
        "mode": mode,
        "description": DATA_INTERPRETATION_MODES[mode],
    }


def validate_summary_override_payload(payload: dict[str, Any], current: dict[str, Any]) -> dict[str, int]:
    updates: dict[str, int] = {}
    for field in SUMMARY_OVERRIDE_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HTTPException(status_code=400, detail=f"{field} must be an integer >= 0.")
        updates[field] = value
    if not updates:
        raise HTTPException(status_code=400, detail="No supported summary fields were provided.")

    merged = {**current, **updates}
    ballot_fields = ("voterTurnout", "validBallots", "invalidBallots", "abstainedBallots")
    if all(field in merged for field in ballot_fields):
        if merged["voterTurnout"] != merged["validBallots"] + merged["invalidBallots"] + merged["abstainedBallots"]:
            raise HTTPException(
                status_code=400,
                detail="voterTurnout must equal validBallots + invalidBallots + abstainedBallots.",
            )
    if "eligibleVoters" in merged and "voterTurnout" in merged and merged["voterTurnout"] > merged["eligibleVoters"]:
        raise HTTPException(status_code=400, detail="voterTurnout must not exceed eligibleVoters.")
    return updates


def validate_candidate_scores_payload(payload: dict[str, Any]) -> list[dict[str, int]]:
    scores = payload.get("candidateScores")
    if not isinstance(scores, list) or not scores:
        raise HTTPException(status_code=400, detail="candidateScores must be a non-empty array.")
    normalized = []
    for index, item in enumerate(scores, start=1):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"candidateScores[{index}] must be an object.")
        try:
            candidate_number = int(item.get("candidateNumber", item.get("candidate_number")))
            score = int(item.get("score"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"candidateScores[{index}] must include integer candidateNumber and score.")
        if candidate_number <= 0 or score < 0:
            raise HTTPException(status_code=400, detail=f"candidateScores[{index}] contains an invalid value.")
        normalized.append({"candidate_number": candidate_number, "score": score})
    return normalized


def validate_round_payload(payload: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if not partial or "candidateScores" in payload:
        updates["candidateScores"] = validate_candidate_scores_payload(payload)
    if "reportedAt" in payload:
        reported_at = str(payload.get("reportedAt") or "").strip()
        if not reported_at:
            raise HTTPException(status_code=400, detail="reportedAt must be a non-empty string.")
        updates["reportedAt"] = reported_at
    if "position" in payload:
        try:
            updates["position"] = int(payload.get("position"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="position must be an integer.")
    for field in SUMMARY_OVERRIDE_FIELDS:
        if field in payload:
            value = payload[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HTTPException(status_code=400, detail=f"{field} must be an integer >= 0.")
            updates[field] = value
    if not updates:
        raise HTTPException(status_code=400, detail="No supported round fields were provided.")
    return updates


MONITOR_HTML = """<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>แดชบอร์ดติดตามผลเลือกตั้ง</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #17202a;
      --muted: #64748b;
      --line: #d8dee8;
      --ok: #087f5b;
      --warn: #b7791f;
      --bad: #c92a2a;
      --info: #1c7ed6;
      --neutral: #475569;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Arial, "Noto Sans Thai", sans-serif;
      font-size: 14px;
    }
    header {
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      padding: 16px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 700; }
    main { padding: 20px 24px 28px; }
    .toolbar {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    input, select, button {
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      padding: 0 10px;
      font: inherit;
    }
    input { min-width: 220px; }
    button {
      cursor: pointer;
      background: #17202a;
      color: #fff;
      border-color: #17202a;
    }
    button.secondary {
      background: #fff;
      color: var(--text);
      border-color: var(--line);
    }
    .cards {
      display: grid;
      grid-template-columns: repeat(6, minmax(120px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-height: 84px;
    }
    .metric .label { color: var(--muted); font-size: 12px; }
    .metric .value { font-size: 26px; font-weight: 700; margin-top: 8px; }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      margin-bottom: 18px;
    }
    .panel-head {
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 10px 12px;
      text-align: left;
      vertical-align: top;
      white-space: nowrap;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      background: #fbfcfe;
    }
    td.wrap { white-space: normal; min-width: 220px; }
    .dashboard-grid {
      display: grid;
      grid-template-columns: minmax(280px, 1fr) minmax(360px, 2fr);
      gap: 16px;
      padding: 14px;
    }
    .summary-list {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px 16px;
      margin: 0;
    }
    .summary-list dt { color: var(--muted); }
    .summary-list dd { margin: 0; font-weight: 700; text-align: right; }
    .edit-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(260px, 1fr));
      gap: 16px;
      padding: 14px;
    }
    .edit-form {
      display: grid;
      gap: 10px;
    }
    .edit-form label {
      display: grid;
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .edit-form input,
    .edit-form select {
      width: 100%;
      min-width: 0;
    }
    .form-row {
      display: grid;
      grid-template-columns: repeat(2, minmax(120px, 1fr));
      gap: 10px;
    }
    .round-layout {
      display: grid;
      grid-template-columns: minmax(360px, 1.25fr) minmax(280px, 0.75fr);
      gap: 16px;
      align-items: start;
    }
    .round-zone {
      display: grid;
      gap: 10px;
      align-content: start;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
    }
    .candidate-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 10px;
      max-height: 360px;
      overflow: auto;
      padding-right: 4px;
    }
    .round-card {
      margin: 12px 0;
    }
    .round-card > summary {
      cursor: pointer;
      list-style: none;
    }
    .round-card > summary::-webkit-details-marker {
      display: none;
    }
    .round-summary-title {
      display: grid;
      gap: 3px;
      min-width: 0;
    }
    .round-summary-action {
      color: var(--info);
      font-weight: 700;
      white-space: nowrap;
    }
    .panel-actions {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .is-collapsed {
      display: none;
    }
    .leaderboard {
      display: grid;
      gap: 8px;
    }
    .leader {
      display: grid;
      grid-template-columns: 34px minmax(160px, 1fr) 90px 80px;
      gap: 10px;
      align-items: center;
      min-height: 34px;
    }
    .bar {
      height: 8px;
      background: #e2e8f0;
      border-radius: 999px;
      overflow: hidden;
      margin-top: 5px;
    }
    .bar span {
      display: block;
      height: 100%;
      background: var(--info);
      width: 0%;
    }
    .candidate-name {
      min-width: 0;
      white-space: normal;
    }
    .swatch {
      width: 12px;
      height: 12px;
      border-radius: 3px;
      display: inline-block;
      margin-right: 6px;
      vertical-align: -1px;
      background: var(--neutral);
    }
    .status {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid;
    }
    .complete { color: var(--ok); background: #e6fcf5; border-color: #96f2d7; }
    .missing_fields, .pending, .delayed { color: var(--warn); background: #fff4db; border-color: #ffd43b; }
    .conflict { color: var(--bad); background: #fff5f5; border-color: #ffc9c9; }
    .no_data { color: var(--neutral); background: #f1f5f9; border-color: #cbd5e1; }
    .muted { color: var(--muted); }
    .error {
      margin-bottom: 14px;
      padding: 10px 12px;
      border: 1px solid #ffc9c9;
      background: #fff5f5;
      color: var(--bad);
      border-radius: 8px;
      display: none;
    }
    .notice {
      margin-bottom: 14px;
      padding: 10px 12px;
      border: 1px solid #96f2d7;
      background: #e6fcf5;
      color: var(--ok);
      border-radius: 8px;
      display: none;
      font-weight: 700;
    }
    .detail {
      position: fixed;
      inset: 0;
      z-index: 50;
      border-top: 0;
      padding: 24px;
      display: none;
      background: rgba(15, 23, 42, 0.45);
      overflow: auto;
    }
    body.modal-open {
      overflow: hidden;
    }
    .detail-card {
      max-width: min(1180px, calc(100vw - 32px));
      margin: 28px auto;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 20px 60px rgba(15, 23, 42, 0.22);
      overflow: hidden;
    }
    .detail-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfe;
    }
    .detail-body {
      padding: 14px;
      max-height: calc(100vh - 132px);
      overflow: auto;
    }
    .icon-button {
      width: 34px;
      height: 34px;
      padding: 0;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      line-height: 1;
    }
    pre {
      overflow: auto;
      background: #0f172a;
      color: #e2e8f0;
      padding: 12px;
      border-radius: 6px;
      max-height: 360px;
    }
    @media (max-width: 960px) {
      header { align-items: flex-start; flex-direction: column; }
      .cards { grid-template-columns: repeat(2, minmax(140px, 1fr)); }
      .dashboard-grid { grid-template-columns: 1fr; }
      .edit-grid { grid-template-columns: 1fr; }
      .round-layout { grid-template-columns: 1fr; }
      .candidate-grid { max-height: none; }
      .leader { grid-template-columns: 30px minmax(120px, 1fr) 72px; }
      .leader .percent { display: none; }
      .table-wrap { overflow-x: auto; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>แดชบอร์ดติดตามผลเลือกตั้ง</h1>
      <div class="muted" id="subtitle">หน้าติดตามข้อมูลบนเครื่อง local</div>
    </div>
    <div class="toolbar">
      <input id="apiKey" type="password" placeholder="X-API-Key ถ้ามี">
      <button class="secondary" id="saveKey">บันทึกคีย์</button>
      <button id="refresh">รีเฟรช</button>
    </div>
  </header>
  <main>
    <div class="error" id="error"></div>
    <div class="notice" id="notice"></div>
    <section class="cards" id="cards"></section>
    <section class="panel">
      <div class="panel-head">
        <strong>Dashboard รวมทุกเขต</strong>
        <div class="muted" id="summaryGeneratedAt"></div>
      </div>
      <div class="dashboard-grid">
        <div>
          <dl class="summary-list" id="summaryList"></dl>
        </div>
        <div>
          <div class="leaderboard" id="leaderboard"></div>
        </div>
      </div>
    </section>
    <section class="panel">
      <div class="panel-head">
        <strong>แก้ข้อมูลหน้ารวม</strong>
        <div class="panel-actions">
          <div class="muted" id="overrideStatus"></div>
          <button type="button" class="secondary" id="togglePageMetaEditor" aria-expanded="false">เปิดแก้ไข</button>
        </div>
      </div>
      <div class="edit-grid is-collapsed" id="pageMetaEditor">
        <form class="edit-form" id="pageMetaForm">
          <label>ชื่อหัวข้อ
            <input id="overrideTitle" name="title" type="text" placeholder="ชื่อหัวข้อผลเลือกตั้ง">
          </label>
          <label>สถานะผล
            <select id="overrideResultStatus" name="resultStatus">
              <option value="">ไม่เปลี่ยน</option>
              <option value="LIVE_COUNT">กำลังนับคะแนน</option>
              <option value="OFFICIAL">ผลทางการ</option>
              <option value="PAUSED">หยุดชั่วคราว</option>
              <option value="DELAYED">ล่าช้า</option>
            </select>
          </label>
          <div class="form-row">
            <label>ผู้แก้
              <input id="pageMetaActor" name="actor" type="text" value="operator">
            </label>
            <label>เหตุผล
              <input id="pageMetaReason" name="reason" type="text" placeholder="manual correction">
            </label>
          </div>
          <button type="submit">บันทึกข้อมูลหน้ารวม</button>
        </form>
        <form class="edit-form" id="dataModeForm">
          <label>วิธีตีความรอบรายงาน
            <select id="dataModeSelect" name="mode">
              <option value="latest_snapshot">ใช้ยอดล่าสุดของแต่ละเขต</option>
              <option value="incremental_delta">บวกทุกรอบที่ approved ในเขต</option>
            </select>
          </label>
          <div class="muted" id="dataModeDescription"></div>
          <div class="form-row">
            <label>ผู้แก้
              <input id="dataModeActor" name="actor" type="text" value="operator">
            </label>
            <label>เหตุผล
              <input id="dataModeReason" name="reason" type="text" placeholder="เลือกวิธีรวมข้อมูลวันจริง">
            </label>
          </div>
          <button type="submit">บันทึกวิธีดึงข้อมูล</button>
        </form>
        <form class="edit-form" id="summaryForm" style="display:none">
          <div class="form-row">
            <label>ผู้มีสิทธิ
              <input id="overrideEligibleVoters" name="eligibleVoters" type="number" min="0" step="1">
            </label>
            <label>ผู้มาใช้สิทธิ
              <input id="overrideVoterTurnout" name="voterTurnout" type="number" min="0" step="1">
            </label>
          </div>
          <div class="form-row">
            <label>บัตรดี
              <input id="overrideValidBallots" name="validBallots" type="number" min="0" step="1">
            </label>
            <label>บัตรเสีย
              <input id="overrideInvalidBallots" name="invalidBallots" type="number" min="0" step="1">
            </label>
          </div>
          <div class="form-row">
            <label>Vote No
              <input id="overrideAbstainedBallots" name="abstainedBallots" type="number" min="0" step="1">
            </label>
            <label>ผู้แก้
              <input id="summaryActor" name="actor" type="text" value="operator">
            </label>
          </div>
          <label>เหตุผล
            <input id="summaryReason" name="reason" type="text" placeholder="manual correction">
          </label>
          <button type="submit">บันทึกข้อมูลสรุป</button>
        </form>
      </div>
    </section>
    <section class="panel">
      <div class="panel-head">
        <div class="toolbar">
          <input id="search" type="search" placeholder="ค้นหาเขต / district code">
          <select id="statusFilter">
            <option value="all">ทุกสถานะ</option>
            <option value="no_data">ยังไม่มีข้อมูล</option>
            <option value="pending">รออนุมัติ</option>
            <option value="missing_fields">ข้อมูลไม่ครบ</option>
            <option value="delayed">ล่าช้า</option>
            <option value="conflict">ข้อมูลขัดแย้ง</option>
            <option value="complete">ครบถ้วน</option>
          </select>
        </div>
        <div class="muted" id="updatedAt"></div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>เขต</th>
              <th>ข้อมูลเข้า</th>
              <th>อนุมัติแล้ว</th>
              <th>อนุมัติล่าสุด</th>
              <th>สถานะ</th>
              <th>ข้อมูลที่ขาด / คำเตือน</th>
              <th>จัดการ</th>
            </tr>
          </thead>
          <tbody id="rows"></tbody>
        </table>
      </div>
      <div class="detail" id="detail"></div>
    </section>
  </main>
  <script>
    const state = { overview: null, summary: null, districts: [], overrides: null, selected: null };
    const keyInput = document.getElementById('apiKey');
    const savedKey = localStorage.getItem('monitorApiKey') || '';
    keyInput.value = savedKey;

    function headers() {
      const key = keyInput.value.trim();
      return key ? {'X-API-Key': key} : {};
    }
    function jsonHeaders() {
      return {...headers(), 'Content-Type': 'application/json'};
    }
    function showError(message) {
      const el = document.getElementById('error');
      el.textContent = message || '';
      el.style.display = message ? 'block' : 'none';
    }
    function showNotice(message) {
      const el = document.getElementById('notice');
      if (state.noticeTimer) window.clearTimeout(state.noticeTimer);
      el.textContent = message || '';
      el.style.display = message ? 'block' : 'none';
      if (message && !message.endsWith('...')) {
        state.noticeTimer = window.setTimeout(() => showNotice(''), 5000);
      }
    }
    function setSubmitState(form, saving, text) {
      const button = form.querySelector('button[type="submit"]');
      if (!button) return;
      if (!button.dataset.defaultText) button.dataset.defaultText = button.textContent;
      button.disabled = saving;
      button.textContent = saving ? text : button.dataset.defaultText;
    }
    function resetSubmitStates() {
      document.querySelectorAll('#pageMetaForm, #dataModeForm, #districtSummaryForm, #newRoundForm, .roundEditForm').forEach((form) => {
        setSubmitState(form, false);
      });
    }
    async function fetchJson(url) {
      const response = await fetch(url, { headers: headers() });
      if (!response.ok) {
        let detail = response.statusText;
        try {
          const payload = await response.json();
          detail = payload.detail || detail;
        } catch (_) {}
        throw new Error(`${response.status} ${detail}`);
      }
      return response.json();
    }
    async function writeJson(method, url, payload) {
      showNotice('กำลังบันทึกข้อมูล...');
      const response = await fetch(url, {
        method,
        headers: jsonHeaders(),
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        showNotice('');
        let detail = response.statusText;
        try {
          const body = await response.json();
          detail = body.detail || detail;
        } catch (_) {}
        resetSubmitStates();
        throw new Error(`${response.status} ${detail}`);
      }
      showNotice('บันทึกสำเร็จ');
      resetSubmitStates();
      return response.json();
    }
    async function patchJson(url, payload) {
      return writeJson('PATCH', url, payload);
    }
    async function postJson(url, payload) {
      return writeJson('POST', url, payload);
    }
    async function deleteJson(url, payload) {
      return writeJson('DELETE', url, payload);
    }
    const statusLabels = {
      no_data: 'ยังไม่มีข้อมูล',
      pending: 'รออนุมัติ',
      missing_fields: 'ข้อมูลไม่ครบ',
      complete: 'ครบถ้วน',
      delayed: 'ล่าช้า',
      conflict: 'ข้อมูลขัดแย้ง',
    };
    const fieldLabels = {
      candidate_scores: 'คะแนนผู้สมัคร',
      eligible_voters: 'จำนวนผู้มีสิทธิ',
      voter_turnout: 'จำนวนผู้มาใช้สิทธิ',
      valid_ballots: 'บัตรดี',
      invalid_ballots: 'บัตรเสีย',
      abstained_ballots: 'Vote No',
      area_id: 'รหัสเขต',
    };
    function statusText(value) {
      return statusLabels[value] || value || '-';
    }
    function fieldText(value) {
      return fieldLabels[value] || value;
    }
    function warningText(value) {
      const map = {
        'Multiple approved submissions exist for this district.': 'มีผลที่อนุมัติแล้วมากกว่าหนึ่งรายการในเขตนี้',
        'voter_turnout does not equal valid_ballots + invalid_ballots + abstained_ballots.': 'จำนวนผู้มาใช้สิทธิไม่เท่ากับผลรวมบัตรดี บัตรเสีย และ Vote No',
        'voter_turnout exceeds eligible_voters.': 'จำนวนผู้มาใช้สิทธิมากกว่าจำนวนผู้มีสิทธิ',
        'Turnout or ballot fields contain a non-integer value.': 'ข้อมูลผู้มาใช้สิทธิหรือบัตรมีค่าที่ไม่ใช่จำนวนเต็ม',
        'eligible_voters is not a valid integer.': 'จำนวนผู้มีสิทธิไม่ใช่จำนวนเต็มที่ถูกต้อง',
      };
      return map[value] || value;
    }
    function card(label, value) {
      return `<div class="metric"><div class="label">${label}</div><div class="value">${value ?? '-'}</div></div>`;
    }
    function renderOverview() {
      const overview = state.overview?.overview || {};
      document.getElementById('cards').innerHTML = [
        card('เขตทั้งหมด', overview.totalDistricts),
        card('มีข้อมูลแล้ว', overview.districtsWithData),
        card('ยังไม่มีข้อมูล', overview.districtsWithoutData),
        card('ครบถ้วน', overview.completeDistricts),
        card('ยังไม่ครบ', overview.incompleteDistricts),
        card('ล่าช้า', overview.delayedDistricts),
      ].join('');
      document.getElementById('subtitle').textContent = state.overview
        ? `${state.overview.electionId} · ${state.overview.resource}`
        : 'หน้าติดตามข้อมูลบนเครื่อง local';
      document.getElementById('updatedAt').textContent = state.overview
        ? `สร้างข้อมูลเมื่อ ${timeText(state.overview.generatedAt)}`
        : '';
    }
    function numberText(value) {
      return typeof value === 'number' ? value.toLocaleString('th-TH') : '-';
    }
    function timeText(value) {
      if (!value) return '-';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString('th-TH', {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      });
    }
    function renderDashboard() {
      const summary = state.summary?.summary || {};
      const meta = state.summary?.pageMeta || {};
      const candidates = state.summary?.candidates || [];
      document.getElementById('summaryGeneratedAt').textContent = meta.generatedAt ? `สร้างข้อมูลเมื่อ ${timeText(meta.generatedAt)}` : '';
      document.getElementById('summaryList').innerHTML = [
        ['สถานะ', meta.resultStatus || '-'],
        ['นับแล้ว', `${numberText(summary.countedUnits)} / ${numberText(summary.totalUnits)} เขต`],
        ['ความคืบหน้า', summary.countedPercentage != null ? `${summary.countedPercentage}%` : '-'],
        ['ผู้มีสิทธิ', numberText(summary.eligibleVoters)],
        ['ผู้มาใช้สิทธิ', numberText(summary.voterTurnout)],
        ['บัตรดี', numberText(summary.validBallots)],
        ['บัตรเสีย', numberText(summary.invalidBallots)],
        ['Vote No', numberText(summary.abstainedBallots)],
        ['อัปเดตล่าสุด', timeText(summary.lastUpdatedAt)],
      ].map(([label, value]) => `<dt>${label}</dt><dd>${value}</dd>`).join('');

      document.getElementById('leaderboard').innerHTML = candidates.length
        ? candidates.map((candidate) => {
            const pct = Math.max(0, Math.min(100, Number(candidate.votePercentage || 0)));
            const color = candidate.color || '#475569';
            return `<div class="leader">
              <strong>#${candidate.rank ?? '-'}</strong>
              <div class="candidate-name">
                <span class="swatch" style="background:${color}"></span>${candidate.name || candidate.candidateId || `หมายเลข ${candidate.candidateNumber}`}
                <div class="bar"><span style="width:${pct}%; background:${color}"></span></div>
              </div>
              <strong>${numberText(candidate.voteCount)}</strong>
              <span class="percent muted">${candidate.votePercentage ?? 0}%</span>
            </div>`;
          }).join('')
        : '<div class="muted">ยังไม่มีคะแนนรวม</div>';
    }
    function setInputValue(id, value) {
      const element = document.getElementById(id);
      if (element) element.value = value ?? '';
    }
    const dataModeText = {
      latest_snapshot: 'ใช้ยอดล่าสุดของแต่ละเขต เหมาะกับรูปที่เป็นยอดรวม ณ เวลานั้น',
      incremental_delta: 'บวกทุกรอบที่ approved ในเขต เหมาะเมื่อรูปแต่ละรอบเป็นยอดเพิ่มเฉพาะรอบ',
    };
    function updateDataModeDescription() {
      const mode = document.getElementById('dataModeSelect').value;
      document.getElementById('dataModeDescription').textContent = dataModeText[mode] || '';
    }
    function populateOverrideForms() {
      const pageMeta = state.overrides?.pageMeta || {};
      const dataMode = state.overrides?.dataMode?.mode || state.summary?.dataInterpretation?.mode || 'latest_snapshot';
      setInputValue('overrideTitle', pageMeta.title || state.summary?.pageMeta?.title || '');
      setInputValue('overrideResultStatus', pageMeta.resultStatus || '');
      setInputValue('dataModeSelect', dataMode);
      updateDataModeDescription();
      document.getElementById('overrideStatus').textContent = state.overrides
        ? 'โหลดค่า override แล้ว'
        : '';
    }
    function setPageMetaEditorOpen(open) {
      const editor = document.getElementById('pageMetaEditor');
      const toggle = document.getElementById('togglePageMetaEditor');
      editor.classList.toggle('is-collapsed', !open);
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.textContent = open ? 'ปิด' : 'เปิดแก้ไข';
    }
    function numericFormValue(id) {
      const raw = document.getElementById(id).value.trim();
      return raw === '' ? undefined : Number(raw);
    }
    async function submitPageMeta(event) {
      event.preventDefault();
      const form = event.currentTarget;
      try {
        showError('');
        showNotice('กำลังบันทึกข้อมูลหน้ารวม...');
        setSubmitState(form, true, 'กำลังบันทึก...');
        const payload = {
          title: document.getElementById('overrideTitle').value.trim(),
          resultStatus: document.getElementById('overrideResultStatus').value,
          actor: document.getElementById('pageMetaActor').value.trim() || 'operator',
          reason: document.getElementById('pageMetaReason').value.trim() || 'manual correction',
        };
        if (!payload.resultStatus) delete payload.resultStatus;
        await patchJson('/api/v1/monitor/page-meta', payload);
        await refresh();
        showNotice('บันทึกข้อมูลหน้ารวมสำเร็จ');
      } catch (error) {
        showError(`บันทึกข้อมูลหน้ารวมไม่สำเร็จ: ${error.message}`);
      }
    }
    async function submitDataMode(event) {
      event.preventDefault();
      const form = event.currentTarget;
      try {
        showError('');
        const payload = {
          mode: form.elements.mode.value,
          actor: document.getElementById('dataModeActor').value.trim() || 'operator',
          reason: document.getElementById('dataModeReason').value.trim() || 'เลือกวิธีรวมข้อมูลวันจริง',
        };
        await patchJson('/api/v1/monitor/data-mode', payload);
        await refresh();
      } catch (error) {
        showError(`บันทึกวิธีดึงข้อมูลไม่สำเร็จ: ${error.message}`);
      }
    }
    async function submitSummary(event) {
      event.preventDefault();
      try {
        showError('');
        const payload = {
          actor: document.getElementById('summaryActor').value.trim() || 'operator',
          reason: document.getElementById('summaryReason').value.trim() || 'manual correction',
        };
        const fields = [
          ['eligibleVoters', 'overrideEligibleVoters'],
          ['voterTurnout', 'overrideVoterTurnout'],
          ['validBallots', 'overrideValidBallots'],
          ['invalidBallots', 'overrideInvalidBallots'],
          ['abstainedBallots', 'overrideAbstainedBallots'],
        ];
        for (const [field, id] of fields) {
          const value = numericFormValue(id);
          if (value !== undefined) payload[field] = value;
        }
        await patchJson('/api/v1/monitor/summary', payload);
        await refresh();
      } catch (error) {
        showError(`บันทึกข้อมูลสรุปไม่สำเร็จ: ${error.message}`);
      }
    }
    function rowMatches(district) {
      const q = document.getElementById('search').value.trim().toLowerCase();
      const status = document.getElementById('statusFilter').value;
      const text = [
        district.areaId,
        district.districtCode,
        district.districtNameTh,
        district.districtNameEn,
      ].join(' ').toLowerCase();
      return (!q || text.includes(q)) && (status === 'all' || district.status === status);
    }
    function renderRows() {
      const rows = state.districts.filter(rowMatches).map((district) => {
        const notes = [
          ...(district.missingFields || []).map(fieldText),
          ...(district.warnings || []).map(warningText),
        ];
        return `<tr>
          <td><strong>${district.districtNameTh || '-'}</strong><div class="muted">${district.areaId} · ${district.districtCode || '-'}</div></td>
          <td>${district.submissionCount ?? 0}</td>
          <td>${district.approvedSubmissionCount ?? 0}</td>
          <td>${timeText(district.latestApprovedAt)}</td>
          <td><span class="status ${district.status}">${statusText(district.status)}</span></td>
          <td class="wrap">${notes.length ? notes.join('<br>') : '<span class="muted">-</span>'}</td>
          <td><button class="secondary" data-area="${district.areaId}">รายละเอียด</button></td>
        </tr>`;
      }).join('');
      document.getElementById('rows').innerHTML = rows || '<tr><td colspan="7" class="muted">ไม่พบเขตที่ตรงกับเงื่อนไข</td></tr>';
      document.querySelectorAll('button[data-area]').forEach((button) => {
        button.addEventListener('click', () => loadDetail(button.dataset.area));
      });
    }
    function datetimeLocalValue(value) {
      const date = value ? new Date(value) : new Date();
      if (Number.isNaN(date.getTime())) return '';
      const offset = date.getTimezoneOffset() * 60000;
      return new Date(date.getTime() - offset).toISOString().slice(0, 16);
    }
    function candidateInputs(scores = []) {
      const scoreMap = new Map((scores || []).map((item) => [Number(item.candidate_number ?? item.candidateNumber), item.score ?? 0]));
      const numbers = new Set([
        ...Array.from(scoreMap.keys()).filter(Boolean),
        ...(state.summary?.candidates || []).map((item) => Number(item.candidateNumber)).filter(Boolean),
      ]);
      if (!numbers.size) numbers.add(1);
      return Array.from(numbers).sort((a, b) => a - b).map((candidateNumber) => {
        const candidate = (state.summary?.candidates || []).find((item) => Number(item.candidateNumber) === candidateNumber) || {};
        const label = candidate.name || `ผู้สมัครเบอร์ ${candidateNumber}`;
        return `<label>${label}<input name="candidate_${candidateNumber}" type="number" min="0" step="1" value="${scoreMap.get(candidateNumber) ?? 0}"></label>`;
      }).join('');
    }
    function roundNumberInputs(roundItem = {}) {
      return `<div class="form-row">
          <label>ลำดับรอบ<input name="position" type="number" step="1" value="${roundItem.position ?? 100000}"></label>
          <label>เวลารายงาน<input name="reportedAt" type="datetime-local" value="${datetimeLocalValue(roundItem.reportedAt)}"></label>
        </div>
        <div class="form-row">
          <label>ผู้มีสิทธิ<input name="eligibleVoters" type="number" min="0" step="1" value="${roundItem.eligibleVoters ?? ''}"></label>
          <label>ผู้มาใช้สิทธิ<input name="voterTurnout" type="number" min="0" step="1" value="${roundItem.voterTurnout ?? ''}"></label>
        </div>
        <div class="form-row">
          <label>บัตรดี<input name="validBallots" type="number" min="0" step="1" value="${roundItem.validBallots ?? ''}"></label>
          <label>บัตรเสีย<input name="invalidBallots" type="number" min="0" step="1" value="${roundItem.invalidBallots ?? ''}"></label>
        </div>
        <label>Vote No<input name="abstainedBallots" type="number" min="0" step="1" value="${roundItem.abstainedBallots ?? ''}"></label>`;
    }
    function roundEditPayload(roundItem) {
      return {
        position: roundItem.position,
        reportedAt: roundItem.reportedAt,
        candidateScores: roundItem.candidateScores || [],
        eligibleVoters: roundItem.eligibleVoters,
        voterTurnout: roundItem.voterTurnout,
        validBallots: roundItem.validBallots,
        invalidBallots: roundItem.invalidBallots,
        abstainedBallots: roundItem.abstainedBallots,
      };
    }
    function renderRoundEditor(roundItem) {
      const deletedText = roundItem.deleted ? ' · deleted' : '';
      return `<details class="panel round-card">
        <summary class="panel-head">
          <strong>${roundItem.roundId}${deletedText}</strong>
          <div class="muted">${timeText(roundItem.reportedAt)} · position ${roundItem.position ?? '-'}</div>
          <span class="round-summary-action">เปิดแก้ไข</span>
        </summary>
        <form class="edit-form roundEditForm" data-area="${roundItem.areaId}" data-round="${roundItem.roundId}" style="padding:12px">
          <div class="round-layout">
            <section class="round-zone">
          <strong>คะแนนผู้สมัคร</strong>
          <div class="candidate-grid">${candidateInputs(roundItem.candidateScores)}</div>
            </section>
            <section class="round-zone">
              <strong>ข้อมูลสรุปรอบนี้</strong>
          ${roundNumberInputs(roundItem)}
          <div class="form-row">
            <label>ผู้แก้<input name="actor" type="text" value="operator"></label>
            <label>เหตุผล<input name="reason" type="text" placeholder="แก้ข้อมูลรอบนี้"></label>
          </div>
            </section>
          </div>
          <div class="toolbar">
            <button type="submit">บันทึกรอบนี้</button>
            <button type="button" class="secondary deleteRound" data-area="${roundItem.areaId}" data-round="${roundItem.roundId}">ลบรอบนี้</button>
          </div>
        </form>
      </details>`;
    }
    function renderNewRoundPayload() {
      return JSON.stringify({
        position: 100000,
        reportedAt: new Date().toISOString(),
        candidateScores: [{candidateNumber: 1, score: 0}],
        voterTurnout: 0,
        validBallots: 0,
        invalidBallots: 0,
        abstainedBallots: 0,
      }, null, 2);
    }
    function closeDetail() {
      const el = document.getElementById('detail');
      el.style.display = 'none';
      el.innerHTML = '';
      document.body.classList.remove('modal-open');
      state.selected = null;
    }
    async function loadDetail(areaId) {
      try {
        showError('');
        const detail = await fetchJson(`/api/v1/monitor/districts/${encodeURIComponent(areaId)}`);
        const el = document.getElementById('detail');
        const latest = detail.latestApprovedResult || {};
        const override = detail.summaryOverride || {};
        const fieldValue = (camel, snake) => override[camel] ?? latest[snake] ?? '';
        state.selected = areaId;
        document.body.classList.add('modal-open');
        el.style.display = 'block';
        el.innerHTML = `
          <div class="detail-card" role="dialog" aria-modal="true" aria-labelledby="detailTitle">
          <div class="detail-head">
            <strong id="detailTitle">${detail.districtNameTh || areaId}</strong>
            <button type="button" class="secondary icon-button" id="closeDetail" aria-label="Close">&times;</button>
          </div>
          <div class="detail-body">
          <form class="edit-form" id="districtSummaryForm" data-area="${areaId}" style="margin:12px 0">
            <div class="form-row">
              <label>ผู้มีสิทธิ
                <input name="eligibleVoters" type="number" min="0" step="1" value="${fieldValue('eligibleVoters', 'eligible_voters')}">
              </label>
              <label>ผู้มาใช้สิทธิ
                <input name="voterTurnout" type="number" min="0" step="1" value="${fieldValue('voterTurnout', 'voter_turnout')}">
              </label>
            </div>
            <div class="form-row">
              <label>บัตรดี
                <input name="validBallots" type="number" min="0" step="1" value="${fieldValue('validBallots', 'valid_ballots')}">
              </label>
              <label>บัตรเสีย
                <input name="invalidBallots" type="number" min="0" step="1" value="${fieldValue('invalidBallots', 'invalid_ballots')}">
              </label>
            </div>
            <div class="form-row">
              <label>Vote No
                <input name="abstainedBallots" type="number" min="0" step="1" value="${fieldValue('abstainedBallots', 'abstained_ballots')}">
              </label>
              <label>ผู้แก้
                <input name="actor" type="text" value="operator">
              </label>
            </div>
            <label>เหตุผล
              <input name="reason" type="text" placeholder="manual correction">
            </label>
            <button type="submit">บันทึกข้อมูลเขตนี้</button>
          </form>
          <details class="panel round-card">
            <summary class="panel-head">
              <strong>เพิ่ม / แก้ไขข้อมูลรายรอบ</strong>
              <span class="muted">${detail.dataInterpretation?.mode || '-'}</span>
              <span class="round-summary-action">เปิดเพิ่มรอบ</span>
            </summary>
            <form class="edit-form" id="newRoundForm" data-area="${areaId}" style="padding:12px">
              <div class="round-layout">
                <section class="round-zone">
              <strong>คะแนนผู้สมัคร</strong>
              <div class="candidate-grid">${candidateInputs([])}</div>
                </section>
                <section class="round-zone">
                  <strong>ข้อมูลสรุปรอบใหม่</strong>
              ${roundNumberInputs({position: 100000, reportedAt: new Date().toISOString()})}
              <div class="form-row">
                <label>ผู้แก้<input name="actor" type="text" value="operator"></label>
                <label>เหตุผล<input name="reason" type="text" placeholder="เพิ่มรอบใหม่หรือแทรกระหว่างรอบ"></label>
              </div>
              <button type="submit">เพิ่มรอบใหม่</button>
                </section>
              </div>
            </form>
          </details>
          ${(detail.rounds || []).map(renderRoundEditor).join('')}
          <details>
            <summary>ข้อมูลเทคนิค</summary>
            <pre>${JSON.stringify(detail, null, 2)}</pre>
          </details>
          </div>
          </div>`;
        document.getElementById('closeDetail').addEventListener('click', closeDetail);
        document.getElementById('districtSummaryForm').addEventListener('submit', submitDistrictSummary);
        document.getElementById('newRoundForm').addEventListener('submit', submitNewRound);
        document.querySelectorAll('.roundEditForm').forEach((form) => form.addEventListener('submit', submitRoundEdit));
        document.querySelectorAll('.deleteRound').forEach((button) => button.addEventListener('click', deleteRound));
      } catch (error) {
        showError(`โหลดรายละเอียดเขตไม่สำเร็จ: ${error.message}`);
      }
    }
    async function submitDistrictSummary(event) {
      event.preventDefault();
      try {
        showError('');
        const form = event.currentTarget;
        const areaId = form.dataset.area;
        const payload = {
          actor: form.elements.actor.value.trim() || 'operator',
          reason: form.elements.reason.value.trim() || 'manual correction',
        };
        for (const field of ['eligibleVoters', 'voterTurnout', 'validBallots', 'invalidBallots', 'abstainedBallots']) {
          const raw = form.elements[field].value.trim();
          if (raw !== '') payload[field] = Number(raw);
        }
        await patchJson(`/api/v1/monitor/districts/${encodeURIComponent(areaId)}/summary`, payload);
        await refresh();
        await loadDetail(areaId);
      } catch (error) {
        showError(`บันทึกข้อมูลเขตไม่สำเร็จ: ${error.message}`);
      }
    }
    function parseRoundPayload(form) {
      const payload = {candidateScores: []};
      for (const element of Array.from(form.elements)) {
        if (!element.name || element.name === 'actor' || element.name === 'reason') continue;
        if (element.name.startsWith('candidate_')) {
          payload.candidateScores.push({
            candidateNumber: Number(element.name.replace('candidate_', '')),
            score: Number(element.value || 0),
          });
        } else if (element.name === 'reportedAt') {
          payload.reportedAt = element.value ? new Date(element.value).toISOString() : new Date().toISOString();
        } else if (element.value !== '') {
          payload[element.name] = Number(element.value);
        }
      }
      payload.actor = form.elements.actor.value.trim() || 'operator';
      payload.reason = form.elements.reason.value.trim() || 'manual round update';
      return payload;
    }
    async function submitNewRound(event) {
      event.preventDefault();
      const form = event.currentTarget;
      const areaId = form.dataset.area;
      try {
        showError('');
        await postJson(`/api/v1/monitor/districts/${encodeURIComponent(areaId)}/rounds`, parseRoundPayload(form));
        await refresh();
        await loadDetail(areaId);
      } catch (error) {
        showError(`บันทึกรอบใหม่ไม่สำเร็จ: ${error.message}`);
      }
    }
    async function submitRoundEdit(event) {
      event.preventDefault();
      const form = event.currentTarget;
      const areaId = form.dataset.area;
      const roundId = form.dataset.round;
      try {
        showError('');
        await patchJson(`/api/v1/monitor/districts/${encodeURIComponent(areaId)}/rounds/${encodeURIComponent(roundId)}`, parseRoundPayload(form));
        await refresh();
        await loadDetail(areaId);
      } catch (error) {
        showError(`บันทึกรอบไม่สำเร็จ: ${error.message}`);
      }
    }
    async function deleteRound(event) {
      const areaId = event.currentTarget.dataset.area;
      const roundId = event.currentTarget.dataset.round;
      try {
        showError('');
        await deleteJson(`/api/v1/monitor/districts/${encodeURIComponent(areaId)}/rounds/${encodeURIComponent(roundId)}`, {
          actor: 'operator',
          reason: 'delete round from monitor',
        });
        await refresh();
        await loadDetail(areaId);
      } catch (error) {
        showError(`ลบรอบไม่สำเร็จ: ${error.message}`);
      }
    }
    async function refresh() {
      try {
        showError('');
        const [overview, districts, summary, overrides] = await Promise.all([
          fetchJson('/api/v1/monitor/overview'),
          fetchJson('/api/v1/monitor/districts'),
          fetchJson('/api/v1/governor-results/summary'),
          fetchJson('/api/v1/monitor/overrides'),
        ]);
        state.overview = overview;
        state.districts = districts.districts || [];
        state.summary = summary;
        state.overrides = overrides;
        renderOverview();
        renderDashboard();
        populateOverrideForms();
        renderRows();
      } catch (error) {
        showError(`โหลดข้อมูล monitor ไม่สำเร็จ: ${error.message}`);
      }
    }
    document.getElementById('saveKey').addEventListener('click', () => {
      localStorage.setItem('monitorApiKey', keyInput.value.trim());
      refresh();
    });
    document.addEventListener('submit', (event) => {
      if (event.target.matches('#pageMetaForm, #dataModeForm, #districtSummaryForm, #newRoundForm, .roundEditForm')) {
        setSubmitState(event.target, true, 'กำลังบันทึก...');
      }
    }, true);
    document.getElementById('refresh').addEventListener('click', refresh);
    document.getElementById('pageMetaForm').addEventListener('submit', submitPageMeta);
    document.getElementById('dataModeForm').addEventListener('submit', submitDataMode);
    document.getElementById('dataModeSelect').addEventListener('change', updateDataModeDescription);
    document.getElementById('togglePageMetaEditor').addEventListener('click', () => {
      const editor = document.getElementById('pageMetaEditor');
      setPageMetaEditorOpen(editor.classList.contains('is-collapsed'));
    });
    document.getElementById('detail').addEventListener('click', (event) => {
      if (event.target.id === 'detail') closeDetail();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && state.selected) closeDetail();
    });
    document.getElementById('search').addEventListener('input', renderRows);
    document.getElementById('statusFilter').addEventListener('change', renderRows);
    refresh();
  </script>
</body>
</html>
"""


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def monitor_districts_response(*, use_cache: bool = True) -> dict[str, Any]:
    global _monitor_cache_at, _monitor_cache_payload
    now = monotonic()
    if use_cache and _monitor_cache_payload and now - _monitor_cache_at < _monitor_cache_seconds:
        return _monitor_cache_payload

    with _monitor_cache_lock:
        now = monotonic()
        if use_cache and _monitor_cache_payload and now - _monitor_cache_at < _monitor_cache_seconds:
            return _monitor_cache_payload

        payload = _build_monitor_districts_response()
        _monitor_cache_payload = payload
        _monitor_cache_at = monotonic()
        return payload


def _build_monitor_districts_response() -> dict[str, Any]:
    try:
        districts_by_id = district_catalog.districts_by_id()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="District master data is unavailable") from exc
    try:
        candidates_by_number = candidate_catalog.candidates_by_number()
    except Exception:
        candidates_by_number = {}

    area_indexes = {}
    raw_approved_results_by_area = {}
    approved_results_by_area = {}
    bangkok_area_ids = [
        str(district.get("id"))
        for district in districts_by_id.values()
        if district.get("provinceCode") == 10 and district.get("id") is not None
    ]
    try:
        mode = current_data_mode()
        indexed_area_ids = set(store.list_area_indexes(settings.source_election_id))
    except BotoCoreError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"S3 data is unavailable. Check AWS credentials/session and RESULTS_API_S3_BUCKET. {exc}",
        ) from exc

    for area_id in bangkok_area_ids:
        if area_id in indexed_area_ids:
            try:
                index = store.area_submissions(settings.source_election_id, area_id) or {}
                raw_approved_results = store.approved_results_for_area(settings.source_election_id, area_id)
                _, effective_results = apply_round_overrides(
                    area_id,
                    raw_approved_results,
                )
                approved_results = apply_district_summary_overrides(effective_results)
            except BotoCoreError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"S3 data is unavailable. Check AWS credentials/session and RESULTS_API_S3_BUCKET. {exc}",
                ) from exc
        else:
            index = {}
            raw_approved_results = []
            approved_results = []
        area_indexes[area_id] = index
        raw_approved_results_by_area[area_id] = raw_approved_results
        approved_results_by_area[area_id] = approved_results

    return build_monitor_districts(
        district_catalog=districts_by_id,
        area_indexes=area_indexes,
        approved_results_by_area=interpreted_results_by_area(approved_results_by_area, mode),
        raw_approved_results_by_area=raw_approved_results_by_area,
        candidate_catalog=candidates_by_number,
        election_id=settings.election_id,
        delayed_after_minutes=settings.delayed_after_minutes,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "election-results-api"}


@app.get("/monitor", response_class=HTMLResponse)
def get_monitor_page() -> str:
    return MONITOR_HTML


@app.get("/api/v1/monitor/districts", dependencies=[Depends(require_api_key)])
def get_monitor_districts() -> dict[str, Any]:
    return monitor_districts_response()


@app.get("/api/v1/monitor/overview", dependencies=[Depends(require_api_key)])
def get_monitor_overview() -> dict[str, Any]:
    districts_payload = monitor_districts_response()
    return build_monitor_overview(
        monitor_districts=districts_payload["districts"],
        election_id=settings.election_id,
        generated_at=districts_payload["generatedAt"],
    )


@app.get("/api/v1/monitor/districts/{area_id}", dependencies=[Depends(require_api_key)])
def get_monitor_district(area_id: str, limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    try:
        districts_by_id = district_catalog.districts_by_id()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="District master data is unavailable") from exc

    district = districts_by_id.get(area_id)
    if not district or district.get("provinceCode") != 10:
        raise HTTPException(status_code=404, detail="District not found")

    try:
        candidates_by_number = candidate_catalog.candidates_by_number()
    except Exception:
        candidates_by_number = {}

    try:
        index = store.area_submissions(settings.source_election_id, area_id) or {}
        raw_approved_results = store.approved_results_for_area(settings.source_election_id, area_id)
        rounds, effective_results = apply_round_overrides(area_id, raw_approved_results)
        approved_results = apply_district_summary_overrides(effective_results)
    except BotoCoreError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"S3 data is unavailable. Check AWS credentials/session and RESULTS_API_S3_BUCKET. {exc}",
        ) from exc
    mode = current_data_mode()
    interpreted_result = interpreted_area_result(approved_results, mode)
    monitor_payload = build_monitor_districts(
        district_catalog={area_id: district},
        area_indexes={area_id: index},
        approved_results_by_area={area_id: [interpreted_result] if interpreted_result else []},
        raw_approved_results_by_area={area_id: approved_results},
        candidate_catalog=candidates_by_number,
        election_id=settings.election_id,
        delayed_after_minutes=settings.delayed_after_minutes,
    )
    monitor_row = monitor_payload["districts"][0]
    submissions = [
        item
        for item in index.get("submissions", [])
        if isinstance(item, dict)
    ][:limit]

    return without_nulls(
        {
            **monitor_row,
            "schemaVersion": "1.0",
            "resource": "election-monitor-district",
            "generatedAt": monitor_payload["generatedAt"],
            "electionId": settings.election_id,
            "submissions": submissions,
            "summaryOverride": read_district_summary_override(area_id),
            "dataInterpretation": {"mode": mode, "description": DATA_INTERPRETATION_MODES[mode]},
            "latestApprovedResult": interpreted_result,
            "rounds": rounds[:limit],
            "approvedResults": approved_results[:limit],
            "rawApprovedResults": raw_approved_results[:limit],
        }
    )


@app.get("/api/v1/monitor/overrides", dependencies=[Depends(require_api_key)])
def get_monitor_overrides() -> dict[str, Any]:
    return monitor_overrides_response()


@app.patch("/api/v1/monitor/page-meta", dependencies=[Depends(require_api_key)])
def update_monitor_page_meta(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    actor, reason = normalize_actor_reason(payload)
    updates = validate_page_meta_payload(payload)
    try:
        before = read_page_meta_override()
        after = {**before, **updates}
        store.write_json(PAGE_META_OVERRIDE_KEY, after)
        audit_event = write_monitor_audit_event(
            event_type="page_meta_updated",
            actor=actor,
            reason=reason,
            before=before,
            after=after,
        )
    except BotoCoreError as exc:
        raise monitor_storage_error(exc) from exc
    invalidate_result_caches()
    return {
        "schemaVersion": "1.0",
        "resource": "election-monitor-page-meta-override",
        "electionId": settings.election_id,
        "pageMeta": after,
        "auditEvent": audit_event,
    }


@app.patch("/api/v1/monitor/data-mode", dependencies=[Depends(require_api_key)])
def update_monitor_data_mode(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    actor, reason = normalize_actor_reason(payload)
    updates = validate_data_mode_payload(payload)
    try:
        before = read_data_mode_override()
        after = {**before, **updates}
        store.write_json(DATA_MODE_OVERRIDE_KEY, after)
        audit_event = write_monitor_audit_event(
            event_type="data_mode_updated",
            actor=actor,
            reason=reason,
            before=before,
            after=after,
        )
    except BotoCoreError as exc:
        raise monitor_storage_error(exc) from exc
    invalidate_result_caches()
    try:
        static_export = export_static_governor_results()
    except (BotoCoreError, ClientError) as exc:
        raise monitor_storage_error(exc) from exc
    return {
        "schemaVersion": "1.0",
        "resource": "election-monitor-data-mode-override",
        "electionId": settings.election_id,
        "dataMode": after,
        "auditEvent": audit_event,
        "staticExport": static_export,
    }


@app.patch("/api/v1/monitor/summary", dependencies=[Depends(require_api_key)])
def update_monitor_summary(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    raise HTTPException(
        status_code=400,
        detail="Summary overrides must be submitted per district via /api/v1/monitor/districts/{area_id}/summary.",
    )


@app.patch("/api/v1/monitor/districts/{area_id}/summary", dependencies=[Depends(require_api_key)])
def update_monitor_district_summary(area_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    actor, reason = normalize_actor_reason(payload)
    try:
        districts_by_id = district_catalog.districts_by_id()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="District master data is unavailable") from exc
    district = districts_by_id.get(area_id)
    if not district or district.get("provinceCode") != 10:
        raise HTTPException(status_code=404, detail="District not found")
    try:
        before = read_district_summary_override(area_id)
        updates = validate_summary_override_payload(payload, before)
        after = {**before, **updates}
        store.write_json(district_summary_override_key(area_id), {**after, "areaId": area_id})
        audit_event = write_monitor_audit_event(
            event_type="summary_override_updated",
            actor=actor,
            reason=reason,
            before=before,
            after=after,
            area_id=area_id,
        )
    except BotoCoreError as exc:
        raise monitor_storage_error(exc) from exc
    invalidate_result_caches()
    return {
        "schemaVersion": "1.0",
        "resource": "election-monitor-district-summary-override",
        "electionId": settings.election_id,
        "areaId": area_id,
        "summary": after,
        "auditEvent": audit_event,
    }


def ensure_monitor_district(area_id: str) -> None:
    try:
        districts_by_id = district_catalog.districts_by_id()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="District master data is unavailable") from exc
    district = districts_by_id.get(area_id)
    if not district or district.get("provinceCode") != 10:
        raise HTTPException(status_code=404, detail="District not found")


def write_round_override(area_id: str, round_id: str, update: dict[str, Any], *, actor: str, reason: str | None, event_type: str) -> dict[str, Any]:
    before_doc = read_district_round_overrides(area_id)
    before_rounds = dict(before_doc.get("rounds") or {})
    before = before_rounds.get(round_id, {})
    after_round = without_nulls({**before, **update, "roundId": round_id, "areaId": area_id})
    after_rounds = {**before_rounds, round_id: after_round}
    after_doc = {"areaId": area_id, "rounds": after_rounds}
    store.write_json(district_round_overrides_key(area_id), after_doc)
    audit_event = write_monitor_audit_event(
        event_type=event_type,
        actor=actor,
        reason=reason,
        before=before,
        after=after_round,
        area_id=area_id,
    )
    invalidate_result_caches()
    return {"round": after_round, "auditEvent": audit_event}


@app.post("/api/v1/monitor/districts/{area_id}/rounds", dependencies=[Depends(require_api_key)])
def create_monitor_district_round(area_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ensure_monitor_district(area_id)
    actor, reason = normalize_actor_reason(payload)
    updates = validate_round_payload(payload)
    round_id = str(payload.get("roundId") or "").strip() or f"manual:{audit_timestamp()[0]}"
    updates.setdefault("position", 100000)
    updates.setdefault("reportedAt", utc_now_iso())
    updates["sourceType"] = "manual_round"
    try:
        written = write_round_override(
            area_id,
            round_id,
            updates,
            actor=actor,
            reason=reason,
            event_type="district_round_created",
        )
    except BotoCoreError as exc:
        raise monitor_storage_error(exc) from exc
    return {
        "schemaVersion": "1.0",
        "resource": "election-monitor-district-round",
        "electionId": settings.election_id,
        "areaId": area_id,
        **written,
    }


@app.patch("/api/v1/monitor/districts/{area_id}/rounds/{round_id:path}", dependencies=[Depends(require_api_key)])
def update_monitor_district_round(area_id: str, round_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ensure_monitor_district(area_id)
    actor, reason = normalize_actor_reason(payload)
    updates = validate_round_payload(payload, partial=True)
    try:
        written = write_round_override(
            area_id,
            round_id,
            updates,
            actor=actor,
            reason=reason,
            event_type="district_round_updated",
        )
    except BotoCoreError as exc:
        raise monitor_storage_error(exc) from exc
    return {
        "schemaVersion": "1.0",
        "resource": "election-monitor-district-round",
        "electionId": settings.election_id,
        "areaId": area_id,
        **written,
    }


@app.delete("/api/v1/monitor/districts/{area_id}/rounds/{round_id:path}", dependencies=[Depends(require_api_key)])
def delete_monitor_district_round(area_id: str, round_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    ensure_monitor_district(area_id)
    actor, reason = normalize_actor_reason(payload or {})
    try:
        written = write_round_override(
            area_id,
            round_id,
            {"deleted": True},
            actor=actor,
            reason=reason,
            event_type="district_round_deleted",
        )
    except BotoCoreError as exc:
        raise monitor_storage_error(exc) from exc
    return {
        "schemaVersion": "1.0",
        "resource": "election-monitor-district-round",
        "electionId": settings.election_id,
        "areaId": area_id,
        **written,
    }


@app.get("/api/v1/monitor/audit-events", dependencies=[Depends(require_api_key)])
def get_monitor_audit_events(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    try:
        events = store.list_json_objects(MONITOR_AUDIT_PREFIX, limit=limit)
    except BotoCoreError as exc:
        raise monitor_storage_error(exc) from exc
    return {
        "schemaVersion": "1.0",
        "resource": "election-monitor-audit-events",
        "electionId": settings.election_id,
        "events": events,
    }


@app.get("/api/v1/elections/{election_id}/areas", dependencies=[Depends(require_api_key)])
def list_areas(election_id: str) -> dict[str, Any]:
    try:
        districts_by_id = district_catalog.districts_by_id()
    except Exception:
        districts_by_id = {}
    areas = []
    for area_id in store.list_area_indexes(election_id):
        index = store.area_submissions(election_id, area_id) or {}
        approved_results = store.approved_results_for_area(election_id, area_id)
        district = districts_by_id.get(area_id, {})
        areas.append(
            without_nulls({
                "area_id": area_id,
                "district_code": district.get("districtCode"),
                "district_name_th": district.get("districtNameTh"),
                "district_name_en": district.get("districtNameEn"),
                "submission_count": int(index.get("submission_count") or 0),
                "approved_submission_count": len(approved_results),
                "latest_approved_at": approved_results[0].get("approved_at") if approved_results else None,
            })
        )
    return {"election_id": election_id, "area_count": len(areas), "areas": areas}


@app.get("/api/v1/elections/{election_id}/areas/{area_id}", dependencies=[Depends(require_api_key)])
def get_area(election_id: str, area_id: str) -> dict[str, Any]:
    index = store.area_submissions(election_id, area_id)
    if not index:
        raise HTTPException(status_code=404, detail="Area not found")
    approved_results = store.approved_results_for_area(election_id, area_id)
    try:
        district = district_catalog.districts_by_id().get(area_id, {})
    except Exception:
        district = {}
    return without_nulls({
        "election_id": election_id,
        "area_id": area_id,
        "district_code": district.get("districtCode"),
        "district_name_th": district.get("districtNameTh"),
        "district_name_en": district.get("districtNameEn"),
        "submission_count": int(index.get("submission_count") or 0),
        "approved_submission_count": len(approved_results),
        "latest_approved_result": approved_results[0] if approved_results else None,
    })


@app.get(
    "/api/v1/elections/{election_id}/areas/{area_id}/submissions",
    dependencies=[Depends(require_api_key)],
)
def get_area_submissions(
    election_id: str,
    area_id: str,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    index = store.area_submissions(election_id, area_id)
    if not index:
        raise HTTPException(status_code=404, detail="Area not found")
    approved_results = store.approved_results_for_area(election_id, area_id)[:limit]
    return {
        "election_id": election_id,
        "area_id": area_id,
        "submission_count": int(index.get("submission_count") or 0),
        "approved_submission_count": len(approved_results),
        "submissions": approved_results,
    }


@app.get("/api/v1/submissions/{source_message_id}", dependencies=[Depends(require_api_key)])
def get_submission(source_message_id: str) -> dict[str, Any]:
    result = store.approved_result(source_message_id)
    if not result:
        raise HTTPException(status_code=404, detail="Approved submission not found")
    return result


def governor_results_response(*, use_cache: bool = True) -> dict[str, Any]:
    global _governor_results_cache_at, _governor_results_cache_payload
    now = monotonic()
    if use_cache and _governor_results_cache_payload and now - _governor_results_cache_at < _governor_results_cache_seconds:
        return _governor_results_cache_payload

    with _governor_results_cache_lock:
        now = monotonic()
        if use_cache and _governor_results_cache_payload and now - _governor_results_cache_at < _governor_results_cache_seconds:
            return _governor_results_cache_payload

        payload = _build_governor_results_response()
        _governor_results_cache_payload = payload
        _governor_results_cache_at = monotonic()
        return payload


def _build_governor_results_response() -> dict[str, Any]:
    try:
        candidates_by_number = candidate_catalog.candidates_by_number()
    except Exception:
        candidates_by_number = {}
    try:
        total_units = sum(
            1
            for district in district_catalog.districts()
            if district.get("provinceCode") == 10
        )
    except Exception:
        total_units = None
    mode = current_data_mode()
    approved_results = interpreted_public_results(mode)
    payload = build_governor_results(
        approved_results=approved_results,
        candidate_catalog=candidates_by_number,
        election_id=settings.election_id,
        title=settings.election_title,
        result_status=settings.result_status,
        total_units=total_units,
        delayed_after_minutes=settings.delayed_after_minutes,
    )
    page_meta_override = read_page_meta_override()
    if page_meta_override:
        if page_meta_override.get("title"):
            payload["pageMeta"]["title"] = page_meta_override["title"]
        if page_meta_override.get("resultStatus"):
            payload["pageMeta"]["resultStatus"] = page_meta_override["resultStatus"]
    payload["dataInterpretation"] = {
        "mode": mode,
        "description": DATA_INTERPRETATION_MODES[mode],
    }
    return payload


def _build_governor_district_results_response() -> dict[str, Any]:
    try:
        candidates_by_number = candidate_catalog.candidates_by_number()
    except Exception:
        candidates_by_number = {}
    try:
        districts_by_id = district_catalog.districts_by_id()
    except Exception:
        districts_by_id = {}

    mode = current_data_mode()
    approved_results = interpreted_public_results(mode)

    return build_district_results(
        approved_results=approved_results,
        candidate_catalog=candidates_by_number,
        district_catalog=districts_by_id,
    )


def export_static_governor_results() -> dict[str, Any]:
    summary_payload = governor_results_response(use_cache=False)
    districts_payload = _build_governor_district_results_response()
    prefix = settings.static_results_prefix
    summary_key = f"{prefix}/sumary.json" if prefix else "sumary.json"
    districts_key = f"{prefix}/districts.json" if prefix else "districts.json"
    store.write_absolute_json(summary_key, summary_payload)
    store.write_absolute_json(districts_key, districts_payload)
    return {
        "summaryKey": summary_key,
        "districtsKey": districts_key,
        "dataMode": summary_payload.get("dataInterpretation", {}).get("mode"),
    }


@app.get("/api/v1/governor-results/summary", dependencies=[Depends(require_api_key)])
def get_governor_results_summary() -> dict[str, Any]:
    return governor_results_response()


@app.get("/api/v1/governor-results/districts", dependencies=[Depends(require_api_key)])
def get_governor_district_results() -> dict[str, Any]:
    return _build_governor_district_results_response()


@app.get("/api/v1/master/districts", dependencies=[Depends(require_api_key)])
def get_master_districts(
    province_code: int | None = Query(default=None, alias="provinceCode"),
) -> list[dict[str, Any]]:
    try:
        districts = district_catalog.districts()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="District master data is unavailable") from exc
    if province_code is None:
        return districts
    return [
        district
        for district in districts
        if district.get("provinceCode") == province_code
    ]


@app.get("/api/v1/elections/{election_id}/governor-results", dependencies=[Depends(require_api_key)])
def get_governor_results(election_id: str) -> dict[str, Any]:
    return governor_results_response()


@app.get("/api/governor-results.json", dependencies=[Depends(require_api_key)])
def get_governor_results_json(
    election_id: str = Query(default="bkk-governor-2026", alias="electionId"),
) -> dict[str, Any]:
    return governor_results_response()
