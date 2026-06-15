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
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware


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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
            "abstained_ballots": draft.get("abstained_ballots"),
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
            "color": catalog.get(candidate_number, {}).get("color"),
            "voteCount": vote_count,
            "votePercentage": round(vote_count / total_votes * 100, 2) if total_votes else 0,
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
            "validBallots": aggregates["validBallots"],
            "invalidBallots": aggregates["invalidBallots"],
            "abstainedBallots": aggregates["abstainedBallots"],
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
    allow_methods=["GET"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "election-results-api"}


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


def governor_results_response() -> dict[str, Any]:
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
    return build_governor_results(
        approved_results=store.latest_approved_results_by_area(settings.source_election_id),
        candidate_catalog=candidates_by_number,
        election_id=settings.election_id,
        title=settings.election_title,
        result_status=settings.result_status,
        total_units=total_units,
        delayed_after_minutes=settings.delayed_after_minutes,
    )


@app.get("/api/v1/governor-results/summary", dependencies=[Depends(require_api_key)])
def get_governor_results_summary() -> dict[str, Any]:
    return governor_results_response()


@app.get("/api/v1/governor-results/districts", dependencies=[Depends(require_api_key)])
def get_governor_district_results() -> dict[str, Any]:
    try:
        candidates_by_number = candidate_catalog.candidates_by_number()
    except Exception:
        candidates_by_number = {}
    try:
        districts_by_id = district_catalog.districts_by_id()
    except Exception:
        districts_by_id = {}
    return build_district_results(
        approved_results=store.latest_approved_results_by_area(settings.source_election_id),
        candidate_catalog=candidates_by_number,
        district_catalog=districts_by_id,
    )


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
