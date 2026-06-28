from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from time import monotonic, sleep
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from hermes.governor_results.http_fetch import fetch_json_http
from hermes.governor_results.sorkor_adapter import (
    build_sorkor_districts_from_external_payload,
    build_sorkor_summary_from_external_payload,
)
from hermes.governor_results.public_source import (
    BKK_TARGET,
    DEFAULT_PUBLIC_SOURCE,
    LINE_TARGET,
    LIVE_TARGET,
    PUBLIC_EXPORT_FILES,
    PublicSourceNotFoundError,
    SORKOR_EXPORT_FILES,
    effective_active_public_source_config as build_active_public_source_view,
    normalize_public_source,
    parent_prefix_from_static_prefix,
    prefix_for_target,
    promote_public_results,
    read_active_public_source,
    optional_promote_files_for_source,
    source_target_for_public_source,
    write_active_public_source,
)
from hermes.results_api.governor_mock_ticker import (
    DEFAULT_BMC_MOCK_S3_KEY,
    DEFAULT_MOCK_S3_KEY,
    MIN_MOCK_INTERVAL_SECONDS,
    build_dual_mock_reset_snapshots,
    build_dual_mock_tick_snapshots,
    initial_mock_state,
    load_bmc_final_fixture,
    load_final_fixture,
    validate_mock_fetch_intervals,
)


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
    parties_url: str | None
    candidates_timeout_seconds: float
    candidates_cache_seconds: int
    districts_url: str
    districts_timeout_seconds: float
    districts_cache_seconds: int
    election_id: str
    election_title: str
    result_status: str
    delayed_after_minutes: int
    default_data_mode: str
    static_results_prefix: str
    enable_static_results_fallback: bool
    external_governor_results_url: str | None
    external_governor_results_timeout_seconds: float
    external_bmc_results_url: str | None
    external_bmc_results_timeout_seconds: float
    sorkor_election_id: str
    sorkor_election_title: str


def load_settings() -> Settings:
    bucket = os.environ.get("RESULTS_API_S3_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("RESULTS_API_S3_BUCKET is required")
    default_data_mode = os.environ.get(
        "RESULTS_API_DEFAULT_DATA_MODE",
        "latest_snapshot",
    ).strip()
    if default_data_mode not in {"latest_snapshot", "incremental_delta"}:
        default_data_mode = "latest_snapshot"
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
        parties_url=os.environ.get(
            "RESULTS_API_PARTIES_URL",
            "",
        ).strip()
        or None,
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
        default_data_mode=default_data_mode,
        static_results_prefix=(
            os.environ.get("RESULTS_API_STATIC_RESULTS_PREFIX", "").strip()
            or os.environ.get("GOVERNOR_RESULTS_PREFIX", "").strip()
            or "api-data/governor-results"
        ).strip().strip("/"),
        enable_static_results_fallback=os.environ.get(
            "RESULTS_API_ENABLE_STATIC_FALLBACK",
            "true",
        ).strip().lower() not in {"0", "false", "no", "off"},
        external_governor_results_url=(
            os.environ.get("RESULTS_API_EXTERNAL_GOVERNOR_RESULTS_URL", "").strip()
            or None
        ),
        external_governor_results_timeout_seconds=max(
            0.1,
            float(os.environ.get("RESULTS_API_EXTERNAL_GOVERNOR_RESULTS_TIMEOUT_SECONDS", "10")),
        ),
        external_bmc_results_url=(
            os.environ.get("RESULTS_API_EXTERNAL_BMC_RESULTS_URL", "").strip()
            or None
        ),
        external_bmc_results_timeout_seconds=max(
            0.1,
            float(os.environ.get("RESULTS_API_EXTERNAL_BMC_RESULTS_TIMEOUT_SECONDS", "10")),
        ),
        sorkor_election_id=os.environ.get(
            "RESULTS_API_SORKOR_ELECTION_ID",
            "bkk-sorkor-2026",
        ).strip(),
        sorkor_election_title=os.environ.get(
            "RESULTS_API_SORKOR_ELECTION_TITLE",
            "ผลการเลือก ส.ก. กรุงเทพมหานคร",
        ).strip(),
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


def parse_s3_uri(uri: str) -> tuple[str, str] | None:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        return None
    return parsed.netloc, parsed.path.lstrip("/")


def read_json_source(*, source: str, timeout_seconds: float, s3_client: Any | None = None) -> Any:
    s3_location = parse_s3_uri(source)
    if s3_location is not None:
        bucket, key = s3_location
        client = s3_client or boto3.client("s3")
        response = client.get_object(Bucket=bucket, Key=key)
        return json.loads(response["Body"].read().decode("utf-8"))

    return fetch_json_http(source, timeout_seconds=timeout_seconds)


def normalize_election_area_districts(payload: Any) -> list[dict[str, Any]]:
    election_areas = []
    if isinstance(payload, dict):
        election_areas = (
            payload.get("electionAreas")
            or ((payload.get("data") or {}).get("electionAreas") if isinstance(payload.get("data"), dict) else [])
            or []
        )
    normalized: list[dict[str, Any]] = []
    for item in election_areas:
        if not isinstance(item, dict):
            continue
        area_number = item.get("number")
        if area_number is None:
            continue
        normalized.append(
            without_nulls(
                {
                    "id": area_number,
                    "provinceCode": 10,
                    "districtCode": area_number,
                    "districtNameTh": item.get("name"),
                    "districtNameEn": item.get("nameEn"),
                    "electionAreaId": item.get("id"),
                    "areaNumber": area_number,
                }
            )
        )
    return normalized


def normalize_party(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "id": value.get("id"),
        "name": value.get("name"),
        "color": value.get("color"),
        "logoUrl": value.get("logoUrl"),
    }


def normalize_party_key(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text.casefold()


def normalize_parties_payload(payload: Any) -> dict[str, dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("parties"), list):
            items = payload.get("parties", [])
        elif isinstance(payload.get("data"), list):
            items = payload.get("data", [])
        elif isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("parties"), list):
            items = payload["data"].get("parties", [])
        else:
            items = []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    normalized: dict[str, dict[str, Any]] = {}
    for item in items:
        party = normalize_party(item)
        if not party:
            continue
        for key in (party.get("id"), party.get("name")):
            normalized_key = normalize_party_key(key)
            if normalized_key:
                normalized[normalized_key] = party
    return normalized


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


def counted_ballots_total(
    valid_ballots: int | None,
    invalid_ballots: int | None,
    abstained_ballots: int | None,
) -> int | None:
    if valid_ballots is None or invalid_ballots is None or abstained_ballots is None:
        return None
    return valid_ballots + invalid_ballots + abstained_ballots


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


def result_status_from_external_type(raw_type: Any) -> str:
    normalized = str(raw_type or "").strip().upper()
    if normalized == "FINAL":
        return "FINAL"
    return "LIVE_COUNT"


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

    def read_absolute_json(self, key: str) -> dict[str, Any] | None:
        try:
            response = self.s3_client.get_object(Bucket=self.bucket, Key=key.strip().lstrip("/"))
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", "")).lower()
            if code in {"nosuchkey", "404", "notfound"}:
                return None
            raise
        return json.loads(response["Body"].read().decode("utf-8"))

    def read_maybe_absolute_json(self, key: str) -> dict[str, Any] | None:
        normalized = key.strip().lstrip("/")
        if self.prefix and normalized.startswith(f"{self.prefix}/"):
            return self.read_absolute_json(normalized)
        return self.read_json(normalized)

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

        approval = self.read_maybe_absolute_json(approval_key)
        draft = self.read_maybe_absolute_json(draft_key)
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
        parties_url: str | None = None,
        timeout_seconds: float = 5,
        cache_seconds: int = 300,
    ) -> None:
        self.manifest_url = manifest_url
        self.featured_url = featured_url
        self.parties_url = parties_url
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        self._cached_at = 0.0
        self._cached_candidates: dict[int, dict[str, Any]] = {}
        self._lock = Lock()

    def _read_json_url(self, url: str) -> dict[str, Any]:
      payload = read_json_source(source=url, timeout_seconds=self.timeout_seconds)
      return payload if isinstance(payload, dict) else {}

    def _read_json_value(self, url: str) -> Any:
      return read_json_source(source=url, timeout_seconds=self.timeout_seconds)

    def _resolve_party(
        self,
        item: dict[str, Any],
        candidate: dict[str, Any],
        parties: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        embedded_party = normalize_party(candidate.get("party")) or normalize_party(item.get("party"))
        if embedded_party:
            return embedded_party
        for key_name in ("partyId", "partyName", "party"):
            for source in (candidate, item):
                party_key = normalize_party_key(source.get(key_name))
                if party_key and party_key in parties:
                    return parties[party_key]
        return None

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
            parties = (
                normalize_parties_payload(self._read_json_value(self.parties_url))
                if self.parties_url
                else {}
            )
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
                        "partyId": profile.get("partyId"),
                        "partyName": profile.get("partyName"),
                        "groupName": profile.get("groupName"),
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
                if candidate.get("partyId"):
                    item["partyId"] = candidate["partyId"]
                if candidate.get("partyName"):
                    item["partyName"] = candidate["partyName"]
                if candidate.get("groupName"):
                    item["groupName"] = candidate["groupName"]
                party = self._resolve_party(item, candidate, parties)
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

    def _read_json_url(self) -> Any:
      return read_json_source(source=self.url, timeout_seconds=self.timeout_seconds)

    def districts(self) -> list[dict[str, Any]]:
        now = monotonic()
        if self._cached_districts and now - self._cached_at < self.cache_seconds:
            return self._cached_districts
        with self._lock:
            now = monotonic()
            if self._cached_districts and now - self._cached_at < self.cache_seconds:
                return self._cached_districts
            payload = self._read_json_url()
            if isinstance(payload, list):
              districts = [
                without_nulls(item)
                for item in payload
                if isinstance(item, dict)
              ]
            else:
              districts = normalize_election_area_districts(payload)
            self._cached_districts = districts
            self._cached_at = monotonic()
            return self._cached_districts

    def districts_by_id(self) -> dict[str, dict[str, Any]]:
        mapped: dict[str, dict[str, Any]] = {}
        for item in self.districts():
            if item.get("id") is not None:
                mapped[str(item["id"])] = item
            if item.get("areaNumber") is not None:
                mapped[str(item["areaNumber"])] = item
            if item.get("electionAreaId") is not None:
                mapped[str(item["electionAreaId"])] = item
        return mapped


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
                continue
            try:
                values.append(int(value))
            except (TypeError, ValueError):
                continue
        aggregates[response_field] = sum(values) if values else None
        if counted_units and aggregates[response_field] is None:
            warnings.append(f"{response_field} is unavailable in approved results.")
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
    counted_ballots = counted_ballots_total(valid_ballots, invalid_ballots, abstained_ballots)

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
            "countedBallots": counted_ballots,
            "countedBallotsPercentage": percentage_of(counted_ballots, voter_turnout),
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
    unique_districts: dict[str, dict[str, Any]] = {}
    for district in district_catalog.values():
        district_id = str(district.get("id") or "")
        if not district_id or district_id in unique_districts:
            continue
        unique_districts[district_id] = district
    bangkok_districts = [
        district
        for district in unique_districts.values()
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
                    "backgroundSrc": metadata.get("backgroundSrc"),
                    "party": candidate_party_payload(metadata),
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

        eligible_voters = result.get("eligible_voters") if result else None
        voter_turnout = result.get("voter_turnout") if result else None
        valid_ballots = result.get("valid_ballots") if result else None
        invalid_ballots = result.get("invalid_ballots") if result else None
        abstained_ballots = result.get("abstained_ballots") if result else None
        counted_ballots = counted_ballots_total(valid_ballots, invalid_ballots, abstained_ballots)
        last_updated_at = (
            str(result.get("approved_at") or result.get("updated_at") or "").strip()
            if result
            else None
        ) or None

        constituency = {
            "areaId": str(district.get("electionAreaId") or district.get("id")),
            "number": district.get("id"),
            "name": district.get("districtNameTh"),
            "leadingCandidateId": scores[0].get("candidateId") if scores else None,
        }
        optional_summary_fields = without_nulls(
            {
                "countedPercentage": 100.0 if result else None,
                "sumaryVoteCount": total_votes if result else None,
                "eligibleVoters": eligible_voters,
                "voterTurnout": voter_turnout,
                "voterTurnoutPercentage": percentage_of(voter_turnout, eligible_voters),
                "validBallots": valid_ballots,
                "invalidBallots": invalid_ballots,
                "abstainedBallots": abstained_ballots,
                "countedBallots": counted_ballots,
                "countedBallotsPercentage": percentage_of(counted_ballots, voter_turnout),
                "lastUpdatedAt": last_updated_at,
            }
        )
        constituency.update(optional_summary_fields)
        constituency["candidates"] = scores
        constituencies.append(constituency)

    return {
        "schemaVersion": "1.0",
        "resource": "constituency-bangkok",
        "generatedAt": generated_at or utc_now_iso(),
        "constituencies": constituencies,
    }


def build_external_governor_candidates(
    *,
    raw_results: list[dict[str, Any]],
    candidate_catalog: dict[int, dict[str, Any]],
    total_votes: int | None,
    warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        candidate_number = parse_int(item.get("candidateId"))
        vote_count = parse_int(item.get("count"))
        if candidate_number is None or vote_count is None:
            continue
        metadata = candidate_catalog.get(candidate_number, {})
        candidate = {
            "candidateId": metadata.get("candidateId"),
            "candidateNumber": candidate_number,
            "name": metadata.get("name"),
            "candidateSrc": metadata.get("candidateSrc"),
            "color": metadata.get("color"),
            "voteCount": vote_count,
            "votePercentage": round(vote_count / total_votes * 100, 2) if total_votes else 0,
            "backgroundSrc": metadata.get("backgroundSrc"),
            "party": candidate_party_payload(metadata),
        }
        if warnings is not None:
            missing_metadata = [field for field in ("candidateId", "name", "color") if candidate[field] is None]
            if missing_metadata:
                warnings.append(
                    f"Candidate {candidate_number} is missing metadata: {', '.join(missing_metadata)}."
                )
        candidates.append(candidate)
    candidates.sort(key=lambda item: (-item["voteCount"], item["candidateNumber"]))
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
        candidate["isLeading"] = rank == 1
    return candidates


def build_governor_results_from_external_payload(
    *,
    raw_payload: dict[str, Any],
    candidate_catalog: dict[int, dict[str, Any]] | None = None,
    election_id: str = "bkk-governor-2026",
    title: str = "ผลการเลือกตั้งผู้ว่าฯ กรุงเทพมหานคร",
    delayed_after_minutes: int = 30,
    generated_at: str | None = None,
) -> dict[str, Any]:
    total = raw_payload.get("total") if isinstance(raw_payload.get("total"), dict) else {}
    total_polling_units = total.get("pollingUnits") if isinstance(total.get("pollingUnits"), dict) else {}
    valid_ballots = parse_int(total.get("goodVote"))
    invalid_ballots = parse_int(total.get("badVotes"))
    abstained_ballots = parse_int(total.get("noVotes"))
    voter_turnout = parse_int(total.get("totalVotes"))
    eligible_voters = parse_int(total.get("eligiblePopulation"))
    counted_ballots = counted_ballots_total(valid_ballots, invalid_ballots, abstained_ballots)
    counted_units = parse_int(total_polling_units.get("reported"))
    total_units = parse_int(total_polling_units.get("total"))
    last_updated_at = str(raw_payload.get("lastUpdatedAt") or "").strip() or None
    warnings: list[str] = []
    candidates = build_external_governor_candidates(
        raw_results=total.get("result") if isinstance(total.get("result"), list) else [],
        candidate_catalog=candidate_catalog or {},
        total_votes=valid_ballots,
        warnings=warnings,
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

    if voter_turnout is not None and counted_ballots is not None and counted_ballots != voter_turnout:
        warnings.append("counted ballots do not equal voter turnout in external payload.")
    if counted_units is not None and total_units is not None and counted_units > total_units:
        warnings.append("reported polling units exceed total polling units in external payload.")

    return {
        "schemaVersion": "1.0",
        "resource": "governor-results",
        "pageMeta": {
            "electionId": election_id,
            "title": title,
            "resultStatus": result_status_from_external_type(raw_payload.get("type")),
            "generatedAt": generated_timestamp,
        },
        "summary": {
            "countedUnits": counted_units,
            "totalUnits": total_units,
            "countedPercentage": parse_float(total.get("progress")),
            "eligibleVoters": eligible_voters,
            "voterTurnout": voter_turnout,
            "voterTurnoutPercentage": percentage_of(voter_turnout, eligible_voters),
            "validBallots": valid_ballots,
            "invalidBallots": invalid_ballots,
            "abstainedBallots": abstained_ballots,
            "countedBallots": counted_ballots,
            "countedBallotsPercentage": percentage_of(counted_ballots, voter_turnout),
            "validBallotsPercentage": percentage_of(valid_ballots, voter_turnout),
            "invalidBallotsPercentage": percentage_of(invalid_ballots, voter_turnout),
            "abstainedBallotsPercentage": percentage_of(abstained_ballots, voter_turnout),
            "lastUpdatedAt": last_updated_at,
        },
        "candidates": candidates,
        "dataQuality": {
            "isComplete": counted_units >= total_units if counted_units is not None and total_units is not None else None,
            "isDelayed": is_delayed,
            "warnings": warnings,
        },
        "dataInterpretation": {
            "mode": "external_snapshot",
            "description": "Use the latest external snapshot provided by the upstream endpoint.",
        },
    }


def build_district_results_from_external_payload(
    *,
    raw_payload: dict[str, Any],
    candidate_catalog: dict[int, dict[str, Any]],
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
        counted_ballots = counted_ballots_total(valid_ballots, invalid_ballots, abstained_ballots)
        candidates = build_external_governor_candidates(
            raw_results=voting.get("result") if isinstance(voting.get("result"), list) else [],
            candidate_catalog=candidate_catalog,
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
                "sumaryVoteCount": sum(candidate.get("voteCount", 0) for candidate in candidates) if candidates else None,
                "eligibleVoters": parse_int(voting.get("eligiblePopulation")),
                "voterTurnout": voter_turnout,
                "voterTurnoutPercentage": percentage_of(voter_turnout, parse_int(voting.get("eligiblePopulation"))),
                "validBallots": valid_ballots,
                "invalidBallots": invalid_ballots,
                "abstainedBallots": abstained_ballots,
                "countedBallots": counted_ballots,
                "countedBallotsPercentage": percentage_of(counted_ballots, voter_turnout),
                "lastUpdatedAt": last_updated_at,
            }
        )
        constituency.update(optional_summary_fields)
        constituency["candidates"] = candidates
        constituencies.append(constituency)

    return {
        "schemaVersion": "1.0",
        "resource": "constituency-bangkok",
        "generatedAt": generated_at or utc_now_iso(),
        "constituencies": constituencies,
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
    parties_url=settings.parties_url,
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


@app.middleware("http")
async def enforce_utf8_json_charset(request, call_next):
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json") and "charset=" not in content_type.lower():
        response.headers["content-type"] = f"{content_type}; charset=utf-8"
    return response


_governor_results_cache_lock = Lock()
_governor_results_cache_at = 0.0
_governor_results_cache_payload: dict[str, Any] | None = None
_governor_results_cache_seconds = 10.0
_monitor_fetch_execution_lock = Lock()
_monitor_scheduler_started = False
_monitor_scheduler_start_lock = Lock()
_monitor_scheduler_stop_event = Event()
DATA_INTERPRETATION_MODES = {
    "latest_snapshot": "Use the latest available value for each field in each district.",
    "incremental_delta": "Sum every approved report in each district as incremental deltas.",
}
SUMMARY_FIELD_TO_RESULT_FIELD = {
    "eligibleVoters": "eligible_voters",
    "voterTurnout": "voter_turnout",
    "validBallots": "valid_ballots",
    "invalidBallots": "invalid_ballots",
    "abstainedBallots": "abstained_ballots",
}
MONITOR_EXTERNAL_SOURCE_KEY = "monitor/config/external-governor-results.json"
MONITOR_EXTERNAL_BMC_SOURCE_KEY = "monitor/config/external-bmc-results.json"
MONITOR_SCHEDULE_KEY = "monitor/config/governor-results-schedule.json"
MONITOR_FETCH_LOG_KEY = "monitor/logs/governor-results-fetch-log.json"
MONITOR_FETCH_LOG_LIMIT = 50
MONITOR_SCHEDULE_ACTIONS = {"start", "stop", "resume"}
MONITOR_MOCK_KEY = "monitor/config/governor-results-mock.json"
MONITOR_MOCK_ACTIONS = {"start", "stop", "reset"}
_monitor_mock_execution_lock = Lock()
_monitor_mock_scheduler_started = False
_monitor_mock_scheduler_start_lock = Lock()
_monitor_mock_scheduler_stop_event = Event()


def invalidate_result_caches() -> None:
    global _governor_results_cache_at, _governor_results_cache_payload
    _governor_results_cache_payload = None
    _governor_results_cache_at = 0.0


def monitor_storage_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail=f"Unable to read or write monitor configuration in S3. {exc}",
    )


def read_monitor_external_source_override() -> dict[str, Any]:
    return store.read_json(MONITOR_EXTERNAL_SOURCE_KEY) or {}


def safe_read_monitor_external_source_override() -> dict[str, Any]:
    try:
        return read_monitor_external_source_override()
    except (BotoCoreError, ClientError):
        return {}


def read_monitor_external_bmc_source_override() -> dict[str, Any]:
    return store.read_json(MONITOR_EXTERNAL_BMC_SOURCE_KEY) or {}


def safe_read_monitor_external_bmc_source_override() -> dict[str, Any]:
    try:
        return read_monitor_external_bmc_source_override()
    except (BotoCoreError, ClientError):
        return {}


def read_monitor_schedule_override() -> dict[str, Any]:
    return store.read_json(MONITOR_SCHEDULE_KEY) or {}


def safe_read_monitor_schedule_override() -> dict[str, Any]:
    try:
        return read_monitor_schedule_override()
    except (BotoCoreError, ClientError):
        return {}


def read_monitor_fetch_log() -> dict[str, Any]:
    return store.read_json(MONITOR_FETCH_LOG_KEY) or {}


def safe_read_monitor_fetch_log() -> dict[str, Any]:
    try:
        return read_monitor_fetch_log()
    except (BotoCoreError, ClientError):
        return {}


def read_monitor_mock_override() -> dict[str, Any]:
    return store.read_json(MONITOR_MOCK_KEY) or {}


def safe_read_monitor_mock_override() -> dict[str, Any]:
    try:
        return read_monitor_mock_override()
    except (BotoCoreError, ClientError):
        return {}


def write_monitor_mock(payload: dict[str, Any]) -> None:
    store.write_json(MONITOR_MOCK_KEY, payload)


def mock_target_key_from_url(url: str | None, *, default_key: str = DEFAULT_MOCK_S3_KEY) -> str:
    if not str(url or "").strip():
        return default_key
    parsed = urlparse(str(url).strip())
    parts = [part for part in parsed.path.split("/") if part]
    if "api-data" in parts:
        index = parts.index("api-data")
        return "/".join(parts[index:])
    return default_key


def mock_bmc_target_key_from_url(url: str | None) -> str:
    return mock_target_key_from_url(url, default_key=DEFAULT_BMC_MOCK_S3_KEY)


def effective_monitor_mock_config() -> dict[str, Any]:
    saved = safe_read_monitor_mock_override()
    external = effective_external_governor_results_config()
    bmc_external = effective_external_bmc_results_config()
    target_key = str(saved.get("targetKey") or mock_target_key_from_url(external.get("url")))
    bmc_target_key = str(
        saved.get("bmcTargetKey") or mock_bmc_target_key_from_url(bmc_external.get("url"))
    )
    enabled = bool(saved.get("enabled"))
    interval_seconds = int(saved.get("intervalSeconds") or MIN_MOCK_INTERVAL_SECONDS)
    next_run_at = str(saved.get("nextRunAt") or "").strip() or None
    remaining_seconds = monitor_schedule_remaining_seconds(enabled=enabled, next_run_at=next_run_at)
    return {
        "enabled": enabled,
        "intervalSeconds": interval_seconds,
        "updatedAt": saved.get("updatedAt"),
        "nextRunAt": next_run_at,
        "remainingSeconds": remaining_seconds,
        "lastTickAt": saved.get("lastTickAt"),
        "openedCount": int(saved.get("openedCount") or 0),
        "totalDistricts": int(saved.get("totalDistricts") or 0),
        "lastProgress": saved.get("lastProgress"),
        "lastReportedPollingUnits": saved.get("lastReportedPollingUnits"),
        "completedCycles": int(saved.get("completedCycles") or 0),
        "targetKey": target_key,
        "bmcTargetKey": bmc_target_key,
        "savedConfig": saved,
    }


def parse_monitor_mock_interval_seconds(payload: dict[str, Any], *, existing: dict[str, Any]) -> int:
    if "mockIntervalSeconds" in payload:
        try:
            interval_seconds = int(payload.get("mockIntervalSeconds"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="mockIntervalSeconds must be an integer.") from exc
    else:
        interval_seconds = int(existing.get("intervalSeconds") or MIN_MOCK_INTERVAL_SECONDS)
    if interval_seconds < MIN_MOCK_INTERVAL_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"mockIntervalSeconds must be at least {MIN_MOCK_INTERVAL_SECONDS}.",
        )
    return interval_seconds


def assert_monitor_mock_fetch_timing(*, mock_interval_seconds: int, fetch_interval_seconds: int) -> None:
    try:
        validate_mock_fetch_intervals(
            mock_interval_seconds=mock_interval_seconds,
            fetch_interval_seconds=fetch_interval_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def build_monitor_mock_update(
    payload: dict[str, Any],
    *,
    schedule_interval_seconds: int,
    schedule_enabled: bool,
) -> dict[str, Any]:
    existing = safe_read_monitor_mock_override()
    interval_seconds = parse_monitor_mock_interval_seconds(payload, existing=existing)
    action = str(payload.get("mockAction") or "").strip().lower()
    if action and action not in MONITOR_MOCK_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail="mockAction must be one of: start, stop, reset.",
        )
    external = effective_external_governor_results_config()
    bmc_external = effective_external_bmc_results_config()
    target_key = mock_target_key_from_url(str(payload.get("mockTargetUrl") or external.get("url") or ""))
    bmc_target_key = mock_bmc_target_key_from_url(str(bmc_external.get("url") or ""))
    now_iso = utc_now_iso()
    final_payload = load_final_fixture()
    bmc_final_payload = load_bmc_final_fixture()
    district_count = len(final_payload.get("districts") or [])

    if action == "stop":
        return {
            "enabled": False,
            "intervalSeconds": interval_seconds,
            "updatedAt": now_iso,
            "nextRunAt": None,
            "targetKey": target_key,
            "bmcTargetKey": bmc_target_key,
            "totalDistricts": int(existing.get("totalDistricts") or district_count),
            "openedCount": int(existing.get("openedCount") or 0),
            "districtOrder": existing.get("districtOrder") or [],
            "seed": int(existing.get("seed") or 42),
            "completedCycles": int(existing.get("completedCycles") or 0),
            "lastTickAt": existing.get("lastTickAt"),
            "lastProgress": existing.get("lastProgress"),
            "lastReportedPollingUnits": existing.get("lastReportedPollingUnits"),
        }

    if action in {"start", "reset"}:
        if schedule_enabled or str(payload.get("scheduleAction") or "").strip().lower() in {"start", "resume"}:
            assert_monitor_mock_fetch_timing(
                mock_interval_seconds=interval_seconds,
                fetch_interval_seconds=schedule_interval_seconds,
            )
        state = {
            "enabled": True,
            "intervalSeconds": interval_seconds,
            "updatedAt": now_iso,
            "nextRunAt": iso_after_seconds(interval_seconds),
            "targetKey": target_key,
            "bmcTargetKey": bmc_target_key,
            "completedCycles": 0,
            "lastTickAt": None,
            **initial_mock_state(district_count=district_count),
        }
        governor_snapshot, bmc_snapshot, state = build_dual_mock_reset_snapshots(
            state=state,
            governor_final_payload=final_payload,
            bmc_final_payload=bmc_final_payload,
        )
        store.write_absolute_json(target_key, governor_snapshot)
        store.write_absolute_json(bmc_target_key, bmc_snapshot)
        state["lastTickAt"] = now_iso
        state["lastProgress"] = governor_snapshot.get("total", {}).get("progress")
        polling_units = (
            governor_snapshot.get("total", {}).get("pollingUnits")
            if isinstance(governor_snapshot.get("total"), dict)
            else {}
        )
        state["lastReportedPollingUnits"] = (
            polling_units.get("reported") if isinstance(polling_units, dict) else 0
        )
        return state

    enabled = bool(existing.get("enabled"))
    if "mockEnabled" in payload:
        mock_enabled = payload.get("mockEnabled")
        if not isinstance(mock_enabled, bool):
            raise HTTPException(status_code=400, detail="mockEnabled must be a boolean.")
        enabled = mock_enabled

    next_run_at = str(existing.get("nextRunAt") or "").strip() or None
    if enabled and next_run_at is None:
        next_run_at = iso_after_seconds(interval_seconds)
    if not enabled:
        next_run_at = None

    if enabled and schedule_enabled:
        assert_monitor_mock_fetch_timing(
            mock_interval_seconds=interval_seconds,
            fetch_interval_seconds=schedule_interval_seconds,
        )

    return {
        "enabled": enabled,
        "intervalSeconds": interval_seconds,
        "updatedAt": now_iso,
        "nextRunAt": next_run_at,
        "targetKey": target_key,
        "bmcTargetKey": str(existing.get("bmcTargetKey") or bmc_target_key),
        "openedCount": int(existing.get("openedCount") or 0),
        "totalDistricts": int(existing.get("totalDistricts") or district_count),
        "districtOrder": existing.get("districtOrder") or [],
        "seed": int(existing.get("seed") or 42),
        "completedCycles": int(existing.get("completedCycles") or 0),
        "lastTickAt": existing.get("lastTickAt"),
        "lastProgress": existing.get("lastProgress"),
        "lastReportedPollingUnits": existing.get("lastReportedPollingUnits"),
    }


def perform_monitor_mock_tick(*, trigger: str) -> dict[str, Any]:
    with _monitor_mock_execution_lock:
        current = effective_monitor_mock_config()
        if not current.get("enabled"):
            return {}
        saved = dict(current.get("savedConfig") or {})
        governor_final_payload = load_final_fixture()
        bmc_final_payload = load_bmc_final_fixture()
        governor_snapshot, bmc_snapshot, updated_state = build_dual_mock_tick_snapshots(
            state=saved,
            governor_final_payload=governor_final_payload,
            bmc_final_payload=bmc_final_payload,
        )
        target_key = str(updated_state.get("targetKey") or current.get("targetKey") or DEFAULT_MOCK_S3_KEY)
        bmc_target_key = str(
            updated_state.get("bmcTargetKey") or current.get("bmcTargetKey") or DEFAULT_BMC_MOCK_S3_KEY
        )
        store.write_absolute_json(target_key, governor_snapshot)
        store.write_absolute_json(bmc_target_key, bmc_snapshot)
        completed_at = utc_now_iso()
        updated_state["enabled"] = True
        updated_state["intervalSeconds"] = int(current.get("intervalSeconds") or MIN_MOCK_INTERVAL_SECONDS)
        updated_state["targetKey"] = target_key
        updated_state["bmcTargetKey"] = bmc_target_key
        updated_state["lastTickAt"] = completed_at
        updated_state["updatedAt"] = completed_at
        updated_state["lastProgress"] = governor_snapshot.get("total", {}).get("progress")
        polling_units = (
            governor_snapshot.get("total", {}).get("pollingUnits")
            if isinstance(governor_snapshot.get("total"), dict)
            else {}
        )
        updated_state["lastReportedPollingUnits"] = (
            polling_units.get("reported") if isinstance(polling_units, dict) else None
        )
        updated_state["nextRunAt"] = iso_after_seconds(int(updated_state["intervalSeconds"]))
        write_monitor_mock(updated_state)
        return {
            "trigger": trigger,
            "targetKey": target_key,
            "bmcTargetKey": bmc_target_key,
            "openedCount": int(updated_state.get("openedCount") or 0),
            "totalDistricts": int(updated_state.get("totalDistricts") or 0),
            "progress": updated_state.get("lastProgress"),
            "reportedPollingUnits": updated_state.get("lastReportedPollingUnits"),
            "lastUpdatedAt": governor_snapshot.get("lastUpdatedAt"),
            "nextRunAt": updated_state.get("nextRunAt"),
        }


def monitor_mock_scheduler_loop() -> None:
    while not _monitor_mock_scheduler_stop_event.is_set():
        if not monitor_mock_scheduler_enabled():
            sleep(1)
            continue
        try:
            mock = effective_monitor_mock_config()
            next_run_at = parse_iso_datetime(mock.get("nextRunAt"))
            if mock.get("enabled") and next_run_at is not None and datetime.now(timezone.utc) >= next_run_at:
                try:
                    perform_monitor_mock_tick(trigger="automatic")
                except Exception:
                    pass
        except Exception:
            pass
        sleep(1)


def ensure_monitor_mock_scheduler_started() -> None:
    global _monitor_mock_scheduler_started
    if _monitor_mock_scheduler_started:
        return
    with _monitor_mock_scheduler_start_lock:
        if _monitor_mock_scheduler_started:
            return
        thread = Thread(target=monitor_mock_scheduler_loop, name="results-api-monitor-mock-scheduler", daemon=True)
        thread.start()
        _monitor_mock_scheduler_started = True


def static_results_parent_prefix() -> str:
    return parent_prefix_from_static_prefix(settings.static_results_prefix)


def static_results_prefix_for_target(target: str) -> str:
    return prefix_for_target(target, static_results_parent_prefix())


def safe_read_active_public_source_saved() -> dict[str, Any]:
    try:
        return read_active_public_source(
            s3_client=store.s3_client,
            bucket=store.bucket,
            score_prefix=settings.prefix,
        )
    except (BotoCoreError, ClientError):
        return {"source": DEFAULT_PUBLIC_SOURCE, "updatedAt": None, "savedConfig": {}}


def effective_active_public_source_config() -> dict[str, Any]:
    saved = safe_read_active_public_source_saved()
    return build_active_public_source_view(
        parent_prefix=static_results_parent_prefix(),
        saved=saved.get("savedConfig") or saved,
    )


def bkk_public_export_config() -> dict[str, Any]:
    return {
        "source": "fixed",
        "target": BKK_TARGET,
        "prefix": static_results_prefix_for_target(BKK_TARGET),
        "files": [*PUBLIC_EXPORT_FILES, *SORKOR_EXPORT_FILES],
    }


def effective_raw_results_export_config() -> dict[str, Any]:
    return {
        "source": "fixed",
        "target": BKK_TARGET,
        "prefix": static_results_prefix_for_target(BKK_TARGET),
        "updatedAt": None,
        "savedConfig": {},
        "files": ["raw/latest.json", "raw/history/*.json"],
    }


def normalize_timeout_seconds(value: Any, *, default: float) -> float:
    try:
        timeout_seconds = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.1, timeout_seconds)


def normalize_interval_seconds(value: Any, *, default: int) -> int:
    try:
        interval_seconds = int(value)
    except (TypeError, ValueError):
        return default
    return max(10, interval_seconds)


def iso_after_seconds(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )


def effective_external_governor_results_config() -> dict[str, Any]:
    override = safe_read_monitor_external_source_override()
    env_url = settings.external_governor_results_url
    env_timeout_seconds = settings.external_governor_results_timeout_seconds
    if override:
        enabled = bool(override.get("enabled"))
        url = str(override.get("url") or "").strip() or None
        return {
            "source": "override",
            "enabled": enabled and bool(url),
            "url": url if enabled else None,
            "timeoutSeconds": normalize_timeout_seconds(
                override.get("timeoutSeconds"),
                default=env_timeout_seconds,
            ),
            "updatedAt": override.get("updatedAt"),
            "savedConfig": override,
            "envUrl": env_url,
            "envTimeoutSeconds": env_timeout_seconds,
        }
    return {
        "source": "env" if env_url else "none",
        "enabled": bool(env_url),
        "url": env_url,
        "timeoutSeconds": env_timeout_seconds,
        "updatedAt": None,
        "savedConfig": {},
        "envUrl": env_url,
        "envTimeoutSeconds": env_timeout_seconds,
    }


def effective_external_bmc_results_config() -> dict[str, Any]:
    override = safe_read_monitor_external_bmc_source_override()
    env_url = settings.external_bmc_results_url
    env_timeout_seconds = settings.external_bmc_results_timeout_seconds
    if override:
        enabled = bool(override.get("enabled"))
        url = str(override.get("url") or "").strip() or None
        return {
            "source": "override",
            "enabled": enabled and bool(url),
            "url": url if enabled else None,
            "timeoutSeconds": normalize_timeout_seconds(
                override.get("timeoutSeconds"),
                default=env_timeout_seconds,
            ),
            "updatedAt": override.get("updatedAt"),
            "savedConfig": override,
            "envUrl": env_url,
            "envTimeoutSeconds": env_timeout_seconds,
        }
    return {
        "source": "env" if env_url else "none",
        "enabled": bool(env_url),
        "url": env_url,
        "timeoutSeconds": env_timeout_seconds,
        "updatedAt": None,
        "savedConfig": {},
        "envUrl": env_url,
        "envTimeoutSeconds": env_timeout_seconds,
    }


def sorkor_export_target_config() -> dict[str, Any]:
    bkk_prefix = static_results_prefix_for_target(BKK_TARGET)
    live_prefix = static_results_prefix_for_target(LIVE_TARGET)
    return {
        "target": BKK_TARGET,
        "prefix": bkk_prefix,
        "livePrefix": live_prefix,
        "summaryKey": f"{bkk_prefix}/sumary-sorkor.json",
        "districtsKey": f"{bkk_prefix}/districts-sorkor.json",
        "liveSummaryKey": f"{live_prefix}/sumary-sorkor.json",
        "liveDistrictsKey": f"{live_prefix}/districts-sorkor.json",
        "files": list(SORKOR_EXPORT_FILES),
    }


def bmc_raw_export_prefix() -> str:
    return f"{static_results_prefix_for_target(BKK_TARGET)}/bmc"


def maybe_promote_public_results_for_source(source: str) -> dict[str, Any] | None:
    try:
        return promote_public_results(
            s3_client=store.s3_client,
            bucket=store.bucket,
            parent_prefix=static_results_parent_prefix(),
            source_target=source_target_for_public_source(source),
            optional_files=optional_promote_files_for_source(source),
        )
    except PublicSourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def build_monitor_active_public_source_update(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    existing = safe_read_active_public_source_saved()
    if "activePublicSource" not in payload:
        return (
            {
                "source": normalize_public_source(existing.get("source")),
                "updatedAt": existing.get("updatedAt") or utc_now_iso(),
            },
            None,
        )
    try:
        new_source = normalize_public_source(payload.get("activePublicSource"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    old_source = normalize_public_source(existing.get("source"))
    saved = {
        "source": new_source,
        "updatedAt": utc_now_iso(),
    }
    promote_source = new_source if new_source != old_source else None
    return saved, promote_source


def export_external_public_to_bkk(raw_payload: dict[str, Any]) -> dict[str, Any]:
    try:
        candidates_by_number = candidate_catalog.candidates_by_number()
    except Exception:
        candidates_by_number = {}
    try:
        districts_by_id = district_catalog.districts_by_id()
    except Exception:
        districts_by_id = {}
    summary_payload = build_governor_results_from_external_payload(
        raw_payload=raw_payload,
        candidate_catalog=candidates_by_number,
        election_id=settings.election_id,
        title=settings.election_title,
        delayed_after_minutes=settings.delayed_after_minutes,
    )
    districts_payload = build_district_results_from_external_payload(
        raw_payload=raw_payload,
        candidate_catalog=candidates_by_number,
        district_catalog=districts_by_id,
    )
    prefix = static_results_prefix_for_target(BKK_TARGET)
    summary_key = f"{prefix}/sumary.json"
    districts_key = f"{prefix}/districts.json"
    store.write_absolute_json(summary_key, summary_payload)
    store.write_absolute_json(districts_key, districts_payload)
    return {
        "target": BKK_TARGET,
        "prefix": prefix,
        "summaryKey": summary_key,
        "districtsKey": districts_key,
        "dataMode": summary_payload.get("dataInterpretation", {}).get("mode"),
        "summaryPayload": summary_payload,
        "districtsPayload": districts_payload,
    }


def export_sorkor_public_to_bkk(raw_payload: dict[str, Any]) -> dict[str, Any]:
    try:
        districts_by_id = district_catalog.districts_by_id()
    except Exception:
        districts_by_id = {}
    summary_payload = build_sorkor_summary_from_external_payload(
        raw_payload=raw_payload,
        election_id=settings.sorkor_election_id,
        title=settings.sorkor_election_title,
    )
    districts_payload = build_sorkor_districts_from_external_payload(
        raw_payload=raw_payload,
        district_catalog=districts_by_id,
    )
    prefix = static_results_prefix_for_target(BKK_TARGET)
    summary_key = f"{prefix}/sumary-sorkor.json"
    districts_key = f"{prefix}/districts-sorkor.json"
    store.write_absolute_json(summary_key, summary_payload)
    store.write_absolute_json(districts_key, districts_payload)
    return {
        "target": BKK_TARGET,
        "prefix": prefix,
        "summaryKey": summary_key,
        "districtsKey": districts_key,
        "summaryPayload": summary_payload,
        "districtsPayload": districts_payload,
    }


def monitor_schedule_remaining_seconds(*, enabled: bool, next_run_at: str | None) -> int | None:
    if not enabled:
        return None
    parsed = parse_iso_datetime(next_run_at)
    if parsed is None:
        return None
    return max(0, int((parsed - datetime.now(timezone.utc)).total_seconds()))


def effective_monitor_schedule_config() -> dict[str, Any]:
    override = safe_read_monitor_schedule_override()
    enabled = bool(override.get("enabled"))
    interval_seconds = normalize_interval_seconds(override.get("intervalSeconds"), default=300)
    next_run_at = str(override.get("nextRunAt") or "").strip() or None
    active_next_run_at = next_run_at if enabled else None
    remaining_seconds = monitor_schedule_remaining_seconds(enabled=enabled, next_run_at=active_next_run_at)
    return {
        "enabled": enabled,
        "status": "running" if enabled else "stopped",
        "intervalSeconds": interval_seconds,
        "updatedAt": override.get("updatedAt"),
        "lastTriggeredAt": override.get("lastTriggeredAt"),
        "lastCompletedAt": override.get("lastCompletedAt"),
        "nextRunAt": active_next_run_at,
        "remainingSeconds": remaining_seconds,
        "savedConfig": override,
    }


def monitor_fetch_log_entries() -> list[dict[str, Any]]:
    payload = safe_read_monitor_fetch_log()
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return []
    return [item for item in entries if isinstance(item, dict)]


def validate_monitor_external_source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return validate_monitor_source_endpoint_payload(
        payload,
        default_timeout_seconds=settings.external_governor_results_timeout_seconds,
    )


def validate_monitor_bmc_source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return validate_monitor_source_endpoint_payload(
        payload,
        default_timeout_seconds=settings.external_bmc_results_timeout_seconds,
        enabled_key="bmcEnabled",
        url_key="bmcUrl",
        timeout_key="bmcTimeoutSeconds",
    )


def validate_monitor_source_endpoint_payload(
    payload: dict[str, Any],
    *,
    default_timeout_seconds: float,
    enabled_key: str = "enabled",
    url_key: str = "url",
    timeout_key: str = "timeoutSeconds",
) -> dict[str, Any]:
    enabled = payload.get(enabled_key)
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail=f"{enabled_key} must be a boolean.")

    url = str(payload.get(url_key) or "").strip()
    if enabled and not url:
        raise HTTPException(status_code=400, detail=f"{url_key} is required when {enabled_key} is true.")
    if url:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https", "s3"}:
            raise HTTPException(status_code=400, detail=f"{url_key} must start with http://, https://, or s3://.")

    timeout_value = payload.get(timeout_key, default_timeout_seconds)
    try:
        timeout_seconds = float(timeout_value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{timeout_key} must be a number.") from None
    if timeout_seconds < 0.1:
        raise HTTPException(status_code=400, detail=f"{timeout_key} must be at least 0.1.")

    return {
        "enabled": enabled,
        "url": url or None,
        "timeoutSeconds": timeout_seconds,
        "updatedAt": utc_now_iso(),
    }


def parse_monitor_schedule_interval_seconds(payload: dict[str, Any], *, existing: dict[str, Any]) -> int:
    if "scheduleIntervalSeconds" not in payload:
        return normalize_interval_seconds(existing.get("intervalSeconds"), default=300)
    interval_value = payload.get("scheduleIntervalSeconds")
    try:
        interval_seconds = int(interval_value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="scheduleIntervalSeconds must be an integer.") from None
    if interval_seconds < 10:
        raise HTTPException(status_code=400, detail="scheduleIntervalSeconds must be at least 10.")
    return interval_seconds


def build_monitor_schedule_update(payload: dict[str, Any]) -> dict[str, Any]:
    existing = safe_read_monitor_schedule_override()
    interval_seconds = parse_monitor_schedule_interval_seconds(payload, existing=existing)
    action = str(payload.get("scheduleAction") or "").strip().lower()
    if action and action not in MONITOR_SCHEDULE_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail="scheduleAction must be one of: start, stop, resume.",
        )
    now_iso = utc_now_iso()
    preserved = {
        "lastTriggeredAt": existing.get("lastTriggeredAt"),
        "lastCompletedAt": existing.get("lastCompletedAt"),
    }

    if action in {"start", "resume"}:
        mock = effective_monitor_mock_config()
        if mock.get("enabled"):
            assert_monitor_mock_fetch_timing(
                mock_interval_seconds=int(mock.get("intervalSeconds") or MIN_MOCK_INTERVAL_SECONDS),
                fetch_interval_seconds=interval_seconds,
            )
        return {
            "enabled": True,
            "intervalSeconds": interval_seconds,
            "updatedAt": now_iso,
            **preserved,
            "nextRunAt": iso_after_seconds(interval_seconds),
        }

    if action == "stop":
        return {
            "enabled": False,
            "intervalSeconds": interval_seconds,
            "updatedAt": now_iso,
            **preserved,
            "nextRunAt": None,
        }

    enabled = bool(existing.get("enabled"))
    if "scheduleEnabled" in payload:
        schedule_enabled = payload.get("scheduleEnabled")
        if not isinstance(schedule_enabled, bool):
            raise HTTPException(status_code=400, detail="scheduleEnabled must be a boolean.")
        enabled = schedule_enabled

    next_run_at = str(existing.get("nextRunAt") or "").strip() or None
    if enabled and next_run_at is None:
        next_run_at = iso_after_seconds(interval_seconds)

    if enabled:
        mock = effective_monitor_mock_config()
        if mock.get("enabled"):
            assert_monitor_mock_fetch_timing(
                mock_interval_seconds=int(mock.get("intervalSeconds") or MIN_MOCK_INTERVAL_SECONDS),
                fetch_interval_seconds=interval_seconds,
            )

    return {
        "enabled": enabled,
        "intervalSeconds": interval_seconds,
        "updatedAt": existing.get("updatedAt") or now_iso,
        **preserved,
        "nextRunAt": next_run_at if enabled else None,
    }


def read_external_governor_results_payload() -> dict[str, Any] | None:
    config = effective_external_governor_results_config()
    if not config.get("enabled") or not config.get("url"):
        return None
    payload = read_json_source(
        source=str(config["url"]),
        timeout_seconds=float(config["timeoutSeconds"]),
    )
    return payload if isinstance(payload, dict) else None


def read_external_bmc_results_payload() -> dict[str, Any] | None:
    config = effective_external_bmc_results_config()
    if not config.get("enabled") or not config.get("url"):
        return None
    payload = read_json_source(
        source=str(config["url"]),
        timeout_seconds=float(config["timeoutSeconds"]),
    )
    return payload if isinstance(payload, dict) else None


def write_monitor_schedule(payload: dict[str, Any]) -> None:
    store.write_json(MONITOR_SCHEDULE_KEY, payload)


def refresh_monitor_schedule_after_fetch(triggered_at: str, completed_at: str) -> dict[str, Any]:
    current = effective_monitor_schedule_config()
    if not current.get("enabled"):
        return current
    saved = {
        "enabled": True,
        "intervalSeconds": int(current["intervalSeconds"]),
        "updatedAt": current.get("updatedAt") or completed_at,
        "lastTriggeredAt": triggered_at,
        "lastCompletedAt": completed_at,
        "nextRunAt": iso_after_seconds(int(current["intervalSeconds"])),
    }
    write_monitor_schedule(saved)
    return effective_monitor_schedule_config()


def append_monitor_fetch_log(entry: dict[str, Any]) -> None:
    current_entries = monitor_fetch_log_entries()
    payload = {
        "schemaVersion": "1.0",
        "resource": "governor-results-monitor-log",
        "updatedAt": utc_now_iso(),
        "entries": [entry, *current_entries][:MONITOR_FETCH_LOG_LIMIT],
    }
    store.write_json(MONITOR_FETCH_LOG_KEY, payload)


def perform_monitor_fetch(*, trigger: str) -> dict[str, Any]:
    with _monitor_fetch_execution_lock:
        governor_config = effective_external_governor_results_config()
        bmc_config = effective_external_bmc_results_config()
        governor_enabled = bool(governor_config.get("enabled") and governor_config.get("url"))
        bmc_enabled = bool(bmc_config.get("enabled") and bmc_config.get("url"))
        if not governor_enabled and not bmc_enabled:
            raise HTTPException(
                status_code=400,
                detail="No external endpoint is enabled. Save an enabled governor or BMC URL first.",
            )

        started_at = utc_now_iso()
        governor_result: dict[str, Any] = {"status": "skipped"}
        bmc_result: dict[str, Any] = {"status": "skipped"}
        static_export_response: dict[str, Any] | None = None
        sorkor_export_response: dict[str, Any] | None = None
        raw_export: dict[str, Any] | None = None
        bmc_raw_export: dict[str, Any] | None = None
        public_promote = None
        summary_payload: dict[str, Any] | None = None
        districts_payload: dict[str, Any] | None = None
        sorkor_summary_payload: dict[str, Any] | None = None
        sorkor_districts_payload: dict[str, Any] | None = None
        upstream: dict[str, Any] | None = None
        bmc_upstream: dict[str, Any] | None = None

        if governor_enabled:
            try:
                raw_payload = read_external_governor_results_payload()
                if raw_payload is None:
                    raise ValueError("Governor endpoint did not return a JSON object.")
                raw_export = export_raw_governor_results(raw_payload)
                static_export = export_external_public_to_bkk(raw_payload)
                summary_payload = static_export["summaryPayload"]
                districts_payload = static_export["districtsPayload"]
                total = raw_payload.get("total") if isinstance(raw_payload.get("total"), dict) else {}
                polling_units = total.get("pollingUnits") if isinstance(total.get("pollingUnits"), dict) else {}
                upstream = {
                    "type": raw_payload.get("type"),
                    "lastUpdatedAt": raw_payload.get("lastUpdatedAt"),
                    "districtCount": len(raw_payload.get("districts", []))
                    if isinstance(raw_payload.get("districts"), list)
                    else 0,
                    "reportedPollingUnits": polling_units.get("reported"),
                    "totalPollingUnits": polling_units.get("total"),
                }
                static_export_response = {
                    key: value
                    for key, value in static_export.items()
                    if key not in {"summaryPayload", "districtsPayload"}
                }
                governor_result = {
                    "status": "success",
                    "sourceUrl": governor_config.get("url"),
                    "summaryKey": static_export.get("summaryKey"),
                    "districtsKey": static_export.get("districtsKey"),
                    "rawLatestKey": raw_export.get("latestKey"),
                    "rawHistoryKey": raw_export.get("historyKey"),
                }
            except HTTPException as exc:
                governor_result = {
                    "status": "error",
                    "sourceUrl": governor_config.get("url"),
                    "message": str(exc.detail),
                }
            except Exception as exc:
                governor_result = {
                    "status": "error",
                    "sourceUrl": governor_config.get("url"),
                    "message": str(exc),
                }

        if bmc_enabled:
            try:
                raw_bmc_payload = read_external_bmc_results_payload()
                if raw_bmc_payload is None:
                    raise ValueError("BMC endpoint did not return a JSON object.")
                bmc_raw_export = export_raw_bmc_results(raw_bmc_payload)
                sorkor_export = export_sorkor_public_to_bkk(raw_bmc_payload)
                sorkor_summary_payload = sorkor_export["summaryPayload"]
                sorkor_districts_payload = sorkor_export["districtsPayload"]
                total = raw_bmc_payload.get("total") if isinstance(raw_bmc_payload.get("total"), dict) else {}
                polling_units = total.get("pollingUnits") if isinstance(total.get("pollingUnits"), dict) else {}
                bmc_upstream = {
                    "type": raw_bmc_payload.get("type"),
                    "lastUpdatedAt": raw_bmc_payload.get("lastUpdatedAt"),
                    "districtCount": len(raw_bmc_payload.get("districts", []))
                    if isinstance(raw_bmc_payload.get("districts"), list)
                    else 0,
                    "reportedPollingUnits": polling_units.get("reported"),
                    "totalPollingUnits": polling_units.get("total"),
                }
                sorkor_export_response = {
                    key: value
                    for key, value in sorkor_export.items()
                    if key not in {"summaryPayload", "districtsPayload"}
                }
                bmc_result = {
                    "status": "success",
                    "sourceUrl": bmc_config.get("url"),
                    "summaryKey": sorkor_export.get("summaryKey"),
                    "districtsKey": sorkor_export.get("districtsKey"),
                    "rawLatestKey": bmc_raw_export.get("latestKey"),
                    "rawHistoryKey": bmc_raw_export.get("historyKey"),
                }
            except HTTPException as exc:
                bmc_result = {
                    "status": "error",
                    "sourceUrl": bmc_config.get("url"),
                    "message": str(exc.detail),
                }
            except Exception as exc:
                bmc_result = {
                    "status": "error",
                    "sourceUrl": bmc_config.get("url"),
                    "message": str(exc),
                }

        governor_success = governor_result.get("status") == "success"
        bmc_success = bmc_result.get("status") == "success"
        if not governor_success and not bmc_success:
            messages = [
                str(item.get("message") or item.get("status"))
                for item in (governor_result, bmc_result)
                if item.get("status") == "error"
            ]
            detail = "; ".join(messages) if messages else "Unable to fetch external endpoints."
            completed_at = utc_now_iso()
            try:
                refresh_monitor_schedule_after_fetch(started_at, completed_at)
            except Exception:
                pass
            try:
                append_monitor_fetch_log(
                    {
                        "startedAt": started_at,
                        "completedAt": completed_at,
                        "status": "error",
                        "trigger": trigger,
                        "governorStatus": governor_result.get("status"),
                        "bmcStatus": bmc_result.get("status"),
                        "message": detail,
                    }
                )
            except Exception:
                pass
            raise HTTPException(status_code=502, detail=detail)

        invalidate_result_caches()
        completed_at = utc_now_iso()
        schedule = refresh_monitor_schedule_after_fetch(started_at, completed_at)
        active_public_source = effective_active_public_source_config()
        public_promote_error = None
        if active_public_source.get("source") == "bkk" and (governor_success or bmc_success):
            try:
                public_promote = maybe_promote_public_results_for_source("bkk")
            except HTTPException as exc:
                public_promote_error = exc.detail
        overall_status = "success" if governor_success and bmc_success else "partial_success"
        response: dict[str, Any] = {
            "schemaVersion": "1.0",
            "resource": "governor-results-monitor-fetch",
            "fetchedAt": completed_at,
            "trigger": trigger,
            "status": overall_status,
            "current": governor_config,
            "bmc": bmc_config,
            "governorFetch": governor_result,
            "bmcFetch": bmc_result,
            "bkkExportTarget": bkk_public_export_config(),
            "sorkorExportTarget": sorkor_export_target_config(),
            "rawExportTarget": effective_raw_results_export_config(),
            "activePublicSource": active_public_source,
            "publicPromote": public_promote,
            "schedule": schedule,
            "logs": monitor_fetch_log_entries(),
            "mock": effective_monitor_mock_config(),
        }
        if public_promote_error is not None:
            response["publicPromoteError"] = public_promote_error
        if upstream is not None:
            response["upstream"] = upstream
        if bmc_upstream is not None:
            response["bmcUpstream"] = bmc_upstream
        if summary_payload is not None:
            response["publicSummary"] = {
                "resultStatus": summary_payload.get("pageMeta", {}).get("resultStatus"),
                "countedUnits": summary_payload.get("summary", {}).get("countedUnits"),
                "totalUnits": summary_payload.get("summary", {}).get("totalUnits"),
                "lastUpdatedAt": summary_payload.get("summary", {}).get("lastUpdatedAt"),
                "dataInterpretation": summary_payload.get("dataInterpretation"),
            }
        if districts_payload is not None:
            response["districtPayloadCount"] = len(districts_payload.get("constituencies", []))
        if sorkor_summary_payload is not None:
            response["sorkorPublicSummary"] = {
                "resultStatus": sorkor_summary_payload.get("pageMeta", {}).get("resultStatus"),
                "countedUnits": sorkor_summary_payload.get("summary", {}).get("countedUnits"),
                "totalUnits": sorkor_summary_payload.get("summary", {}).get("totalUnits"),
                "lastUpdatedAt": sorkor_summary_payload.get("summary", {}).get("lastUpdatedAt"),
            }
        if sorkor_districts_payload is not None:
            response["sorkorDistrictPayloadCount"] = len(
                sorkor_districts_payload.get("data", {}).get("constituencies", [])
            )
        if raw_export is not None:
            response["rawExport"] = raw_export
        if bmc_raw_export is not None:
            response["bmcRawExport"] = bmc_raw_export
        if static_export_response is not None:
            response["staticExport"] = static_export_response
        if sorkor_export_response is not None:
            response["sorkorExport"] = sorkor_export_response
        append_monitor_fetch_log(
            {
                "startedAt": started_at,
                "completedAt": completed_at,
                "status": overall_status,
                "trigger": trigger,
                "governorStatus": governor_result.get("status"),
                "bmcStatus": bmc_result.get("status"),
                "sourceUrl": governor_config.get("url"),
                "bmcSourceUrl": bmc_config.get("url"),
                "summaryKey": governor_result.get("summaryKey"),
                "districtsKey": governor_result.get("districtsKey"),
                "sorkorSummaryKey": bmc_result.get("summaryKey"),
                "sorkorDistrictsKey": bmc_result.get("districtsKey"),
                "rawLatestKey": governor_result.get("rawLatestKey"),
                "rawHistoryKey": governor_result.get("rawHistoryKey"),
                "bmcRawLatestKey": bmc_result.get("rawLatestKey"),
                "bmcRawHistoryKey": bmc_result.get("rawHistoryKey"),
            }
        )
        response["logs"] = monitor_fetch_log_entries()
        return response


def monitor_fetch_scheduler_enabled() -> bool:
    return os.environ.get("RESULTS_API_ENABLE_MONITOR_FETCH_SCHEDULER", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def monitor_mock_scheduler_enabled() -> bool:
    return os.environ.get("RESULTS_API_ENABLE_MONITOR_MOCK_SCHEDULER", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def monitor_scheduler_loop() -> None:
    while not _monitor_scheduler_stop_event.is_set():
        if not monitor_fetch_scheduler_enabled():
            sleep(1)
            continue
        try:
            schedule = effective_monitor_schedule_config()
            next_run_at = parse_iso_datetime(schedule.get("nextRunAt"))
            if schedule.get("enabled") and next_run_at is not None and datetime.now(timezone.utc) >= next_run_at:
                try:
                    perform_monitor_fetch(trigger="automatic")
                except Exception:
                    pass
        except Exception:
            pass
        sleep(1)


def ensure_monitor_scheduler_started() -> None:
    global _monitor_scheduler_started
    if _monitor_scheduler_started:
        return
    with _monitor_scheduler_start_lock:
        if _monitor_scheduler_started:
            return
        thread = Thread(target=monitor_scheduler_loop, name="results-api-monitor-scheduler", daemon=True)
        thread.start()
        _monitor_scheduler_started = True


@app.on_event("startup")
def start_monitor_scheduler() -> None:
    if monitor_fetch_scheduler_enabled():
        ensure_monitor_scheduler_started()
    if monitor_mock_scheduler_enabled():
        ensure_monitor_mock_scheduler_started()


def current_data_mode() -> str:
    mode = str(settings.default_data_mode).strip()
    return mode if mode in DATA_INTERPRETATION_MODES else settings.default_data_mode


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


LATEST_SNAPSHOT_MERGE_FIELDS = (
    "candidate_scores",
    "eligible_voters",
    "voter_turnout",
    "valid_ballots",
    "invalid_ballots",
    "abstained_ballots",
)


def latest_snapshot_field_has_data(field: str, value: Any) -> bool:
    if field == "candidate_scores":
        return isinstance(value, list) and bool(value)
    return value is not None


def aggregate_latest_snapshot_area_results(approved_results: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not approved_results:
        return None
    latest = dict(approved_results[0])
    for field in LATEST_SNAPSHOT_MERGE_FIELDS:
        if latest_snapshot_field_has_data(field, latest.get(field)):
            continue
        for result in approved_results[1:]:
            candidate = result.get(field)
            if latest_snapshot_field_has_data(field, candidate):
                latest[field] = candidate
                break
    latest["data_interpretation_mode"] = "latest_snapshot"
    latest["included_report_count"] = 1
    return without_nulls(latest)


def interpreted_area_result(approved_results: list[dict[str, Any]], mode: str) -> dict[str, Any] | None:
    if not approved_results:
        return None
    if mode == "incremental_delta":
        return aggregate_incremental_area_results(approved_results)
    return aggregate_latest_snapshot_area_results(approved_results)


def interpreted_results_by_area(approved_results_by_area: dict[str, list[dict[str, Any]]], mode: str) -> dict[str, list[dict[str, Any]]]:
    interpreted: dict[str, list[dict[str, Any]]] = {}
    for area_id, approved_results in approved_results_by_area.items():
        result = interpreted_area_result(approved_results, mode)
        interpreted[area_id] = [result] if result else []
    return interpreted


def interpreted_public_results(mode: str) -> list[dict[str, Any]]:
    results = []
    for area_id in store.list_area_indexes(settings.source_election_id):
        approved_results = store.approved_results_for_area(settings.source_election_id, area_id)
        result = interpreted_area_result(approved_results, mode)
        if result:
            results.append(result)
    return results


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


MONITOR_HTML = """<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Governor Results Monitor</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4efe7;
      --panel: #fffaf2;
      --ink: #1f2a37;
      --muted: #5b6470;
      --line: #d8cdbb;
      --accent: #0d6c63;
      --accent-2: #c26a2e;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", Tahoma, sans-serif;
      background: radial-gradient(circle at top left, #fff8ee, var(--bg) 58%);
      color: var(--ink);
    }
    main {
      max-width: 980px;
      margin: 0 auto;
      padding: 32px 20px 60px;
    }
    .hero {
      padding: 24px;
      border: 1px solid var(--line);
      border-radius: 20px;
      background: linear-gradient(135deg, rgba(13,108,99,0.92), rgba(194,106,46,0.92));
      color: #fffef8;
      box-shadow: 0 18px 50px rgba(22, 32, 43, 0.12);
    }
    .hero h1 { margin: 0 0 10px; font-size: 30px; }
    .hero p { margin: 0; line-height: 1.6; max-width: 760px; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 18px;
      margin-top: 22px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 20px;
      box-shadow: 0 10px 30px rgba(39, 48, 61, 0.06);
    }
    h2 { margin: 0 0 14px; font-size: 18px; }
    label { display: block; margin: 0 0 12px; font-weight: 600; }
    .hint, .status { color: var(--muted); font-size: 14px; line-height: 1.5; }
    input[type="text"], input[type="number"], textarea, select {
      width: 100%;
      margin-top: 6px;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 14px;
      font: inherit;
      background: #fff;
      color: inherit;
    }
    textarea { min-height: 150px; resize: vertical; }
    .row {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 12px;
    }
    button {
      border: 0;
      border-radius: 999px;
      padding: 12px 18px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      color: white;
      background: var(--accent);
    }
    button.secondary { background: var(--accent-2); }
    button:disabled { opacity: 0.6; cursor: wait; }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: Consolas, monospace;
      font-size: 13px;
      line-height: 1.5;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      min-height: 180px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border: 1px solid rgba(255,255,255,0.35);
      border-radius: 999px;
      padding: 8px 12px;
      margin-top: 14px;
      font-size: 13px;
      background: rgba(255,255,255,0.08);
    }
    fieldset {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 14px;
      margin: 0 0 12px;
    }
    fieldset label {
      display: flex;
      align-items: center;
      gap: 8px;
      font-weight: 500;
      margin-bottom: 8px;
    }
    fieldset legend {
      padding: 0 6px;
      font-weight: 700;
    }
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <h1>Governor Results Monitor</h1>
      <p>ใช้หน้านี้เพื่อเปลี่ยน external endpoint สำหรับผลผู้ว่าฯ กรุงเทพฯ แบบ runtime, บันทึก config ลง S3 และสั่งดึงข้อมูลเพื่อ export ไฟล์ public JSON ได้ทันที</p>
      <div class="pill" id="effectiveBadge">Loading current config...</div>
    </section>
    <div class="grid">
      <section class="panel">
        <h2>Auto Fetch Schedule</h2>
        <div class="row">
          <label>Auto Fetch Every (seconds)
            <input id="scheduleIntervalSeconds" type="number" min="10" step="10" value="300">
          </label>
          <label>Time Remaining
            <input id="countdownText" type="text" disabled value="หยุดแล้ว">
          </label>
        </div>
        <p class="status" id="scheduleStatusText">สถานะ: หยุดแล้ว</p>
        <div class="actions">
          <button id="startScheduleButton">เริ่มนับ</button>
          <button id="stopScheduleButton" class="secondary" disabled>หยุด</button>
          <button id="resumeScheduleButton" class="secondary" disabled>นับใหม่</button>
        </div>
        <h2>จำลองข้อมูล กทม (Mock Endpoint)</h2>
        <p class="hint">เขียนข้อมูลใหม่ทับ S3 endpoint-mock ก่อน Auto Fetch ดึง — ช่วง Mock ต้องสั้นกว่า Auto Fetch อย่างน้อย 2 วินาที — คะแนนสลับอันดับทุก tick แบบผันผวนสูง</p>
        <label>Mock ทุก (วินาที)
          <input id="mockIntervalSeconds" type="number" min="5" step="1" value="8">
        </label>
        <p class="status" id="mockStatusText">สถานะจำลอง: หยุดแล้ว</p>
        <div class="actions">
          <button id="startMockButton">เริ่มจำลอง</button>
          <button id="stopMockButton" class="secondary" disabled>หยุดจำลอง</button>
        </div>
        <h2>Source Config (ผู้ว่าฯ)</h2>
        <label>API Key
          <input id="apiKey" type="text" placeholder="RESULTS_API_KEY">
        </label>
        <label>
          <input id="enabled" type="checkbox" checked>
          เปิดใช้ governor endpoint
        </label>
        <label>Governor Endpoint URL
          <input id="url" type="text" placeholder="https://example.com/69-governor-electiondata.json">
        </label>
        <h2>Source Config (ส.ก. / BMC)</h2>
        <label>
          <input id="bmcEnabled" type="checkbox">
          เปิดใช้ BMC endpoint
        </label>
        <label>BMC Endpoint URL
          <input id="bmcUrl" type="text" placeholder="https://bangkokvote69.bangkok.go.th/results/69-bmc-electiondata.json">
        </label>
        <label>Sorkor Export Prefix
          <input id="sorkorExportPrefix" type="text" disabled>
        </label>
        <p class="hint">sumary-sorkor.json และ districts-sorkor.json เขียนไป governor-results-bkk แล้ว promote ไป governor-results เมื่อเลือก กทม</p>
        <fieldset id="activePublicSourceFieldset">
          <legend>แหล่งข้อมูล live สำหรับ governor-results</legend>
          <label>
            <input type="radio" name="activePublicSource" id="activePublicSourceLine" value="line" checked>
            LINE → governor-results-dev
          </label>
          <label>
            <input type="radio" name="activePublicSource" id="activePublicSourceBkk" value="bkk">
            กทม → governor-results-bkk
          </label>
        </fieldset>
        <p class="hint">เลือกว่า frontend จะอ่าน sumary.json / districts.json จากแหล่งไหน (copy ไป governor-results)</p>
        <label>Live Export Prefix
          <input id="liveExportPrefix" type="text" disabled>
        </label>
        <label>Raw Export Prefix (เก็บที่ bkk เท่านั้น)
          <input id="rawExportPrefix" type="text" disabled>
        </label>
        <p class="hint">raw/latest.json และ raw/history/*.json จะเขียนไปที่ governor-results-bkk เสมอ</p>
        <div class="row">
          <label>Governor Timeout Seconds
            <input id="timeoutSeconds" type="number" min="0.1" step="0.1" value="10">
          </label>
          <label>BMC Timeout Seconds
            <input id="bmcTimeoutSeconds" type="number" min="0.1" step="0.1" value="10">
          </label>
          <label>Current Mode
            <input id="sourceMode" type="text" disabled>
          </label>
        </div>
        <div class="actions">
          <button id="saveButton">บันทึก</button>
          <button id="fetchButton" class="secondary">ดึงข้อมูล</button>
        </div>
        <p class="status" id="statusText">พร้อมใช้งาน</p>
      </section>
      <section class="panel">
        <h2>Fetch Result</h2>
        <pre id="resultBox">รอการดึงข้อมูล...</pre>
      </section>
    </div>
  </main>
  <script>
    const apiKeyInput = document.getElementById("apiKey");
    const enabledInput = document.getElementById("enabled");
    const urlInput = document.getElementById("url");
    const bmcEnabledInput = document.getElementById("bmcEnabled");
    const bmcUrlInput = document.getElementById("bmcUrl");
    const bmcTimeoutInput = document.getElementById("bmcTimeoutSeconds");
    const sorkorExportPrefixInput = document.getElementById("sorkorExportPrefix");
    const timeoutInput = document.getElementById("timeoutSeconds");
    const sourceModeInput = document.getElementById("sourceMode");
    const activePublicSourceLine = document.getElementById("activePublicSourceLine");
    const activePublicSourceBkk = document.getElementById("activePublicSourceBkk");
    const liveExportPrefixInput = document.getElementById("liveExportPrefix");
    const rawExportPrefixInput = document.getElementById("rawExportPrefix");
    const scheduleIntervalInput = document.getElementById("scheduleIntervalSeconds");
    const countdownText = document.getElementById("countdownText");
    const scheduleStatusText = document.getElementById("scheduleStatusText");
    const startScheduleButton = document.getElementById("startScheduleButton");
    const stopScheduleButton = document.getElementById("stopScheduleButton");
    const resumeScheduleButton = document.getElementById("resumeScheduleButton");
    const mockIntervalInput = document.getElementById("mockIntervalSeconds");
    const mockStatusText = document.getElementById("mockStatusText");
    const startMockButton = document.getElementById("startMockButton");
    const stopMockButton = document.getElementById("stopMockButton");
    const statusText = document.getElementById("statusText");
    const resultBox = document.getElementById("resultBox");
    const effectiveBadge = document.getElementById("effectiveBadge");
    let scheduleEnabled = false;
    let scheduleNextRunAt = null;
    let countdownTimer = null;
    let schedulePollTimer = null;
    let schedulePollInFlight = false;
    let lastZeroPollAt = 0;
    let mockEnabled = false;
    const SCHEDULE_POLL_INTERVAL_MS = 3000;
    apiKeyInput.value = localStorage.getItem("resultsApiKey") || "";

    function requestHeaders() {
      const headers = { "Content-Type": "application/json" };
      const key = apiKeyInput.value.trim();
      if (key) {
        headers["X-API-Key"] = key;
      }
      return headers;
    }

    async function fetchJson(path, options = {}) {
      const response = await fetch(path, {
        ...options,
        headers: { ...requestHeaders(), ...(options.headers || {}) },
      });
      const text = await response.text();
      const payload = text ? JSON.parse(text) : {};
      if (!response.ok) {
        throw new Error(payload.detail || text || ("HTTP " + response.status));
      }
      return payload;
    }

    function setBusy(isBusy) {
      document.getElementById("saveButton").disabled = isBusy;
      document.getElementById("fetchButton").disabled = isBusy;
      updateScheduleButtons(isBusy);
      updateMockButtons(isBusy);
    }

    function updateMockButtons(isBusy) {
      const busy = Boolean(isBusy);
      startMockButton.disabled = busy || mockEnabled;
      stopMockButton.disabled = busy || !mockEnabled;
    }

    function updateScheduleButtons(isBusy) {
      const busy = Boolean(isBusy);
      startScheduleButton.disabled = busy || scheduleEnabled;
      stopScheduleButton.disabled = busy || !scheduleEnabled;
      resumeScheduleButton.disabled = busy || scheduleEnabled;
    }

    function formatRemaining(seconds) {
      if (seconds === null || seconds === undefined) {
        return "—";
      }
      const total = Math.max(0, Math.floor(seconds));
      const minutes = Math.floor(total / 60);
      const remainder = total % 60;
      return String(minutes).padStart(2, "0") + ":" + String(remainder).padStart(2, "0");
    }

    function selectedActivePublicSource() {
      return activePublicSourceBkk.checked ? "bkk" : "line";
    }

    function setActivePublicSource(source) {
      const normalized = source === "bkk" ? "bkk" : "line";
      activePublicSourceLine.checked = normalized === "line";
      activePublicSourceBkk.checked = normalized === "bkk";
    }

    function validateMockAndFetchIntervals() {
      const mockInterval = Number(mockIntervalInput.value || 8);
      const fetchInterval = Number(scheduleIntervalInput.value || 300);
      if (fetchInterval <= mockInterval) {
        throw new Error("Auto Fetch Every ต้องมากกว่า Mock ทุก (วินาที)");
      }
      if (fetchInterval - mockInterval < 2) {
        throw new Error("Auto Fetch ต้องมากกว่า Mock อย่างน้อย 2 วินาที");
      }
    }

    function renderMockState(mock) {
      mockEnabled = Boolean(mock && mock.enabled);
      if (mock && mock.intervalSeconds) {
        mockIntervalInput.value = mock.intervalSeconds;
      }
      if (!mockEnabled) {
        mockStatusText.textContent = "สถานะจำลอง: หยุดแล้ว";
      } else {
        const opened = (mock && mock.openedCount) || 0;
        const total = (mock && mock.totalDistricts) || 0;
        const progress = (mock && mock.lastProgress) || 0;
        mockStatusText.textContent =
          "สถานะจำลอง: กำลังทำงาน | เขต " + opened + "/" + total +
          " | progress " + progress + "%" +
          (mock && mock.nextRunAt ? " | tick ถัดไป " + new Date(mock.nextRunAt).toLocaleTimeString("th-TH") : "");
      }
      updateMockButtons(false);
    }

    function activePublicSourceSummary(activePublicSource) {
      const source = (activePublicSource && activePublicSource.source) || "line";
      const livePrefix = (activePublicSource && activePublicSource.livePrefix) || "api-data/governor-results";
      return "live=" + source + " (" + livePrefix + ")";
    }

    function updateEffectiveBadge(config, bmcConfig, activePublicSource, rawExportTarget, sorkorExportTarget) {
      effectiveBadge.textContent =
        "Governor: " + ((config && config.source) || "none") +
        " | " + ((config && config.url) || "disabled") +
        " | BMC: " + ((bmcConfig && bmcConfig.source) || "none") +
        " | " + ((bmcConfig && bmcConfig.url) || "disabled") +
        " | " + activePublicSourceSummary(activePublicSource) +
        " | sorkor-bkk=" + ((sorkorExportTarget && sorkorExportTarget.prefix) || "api-data/governor-results-bkk");
    }

    function renderActivePublicSource(activePublicSource, rawExportTarget, sorkorExportTarget) {
      if (activePublicSource) {
        setActivePublicSource(activePublicSource.source || "line");
        liveExportPrefixInput.value = activePublicSource.livePrefix || "";
      }
      rawExportPrefixInput.value = (rawExportTarget && rawExportTarget.prefix) || "api-data/governor-results-bkk";
      sorkorExportPrefixInput.value = (sorkorExportTarget && sorkorExportTarget.prefix) || "api-data/governor-results-bkk";
    }

    function renderBmcConfig(bmcConfig) {
      const config = bmcConfig || {};
      bmcEnabledInput.checked = Boolean(config.enabled);
      bmcUrlInput.value = config.url || "";
      bmcTimeoutInput.value = config.timeoutSeconds || 10;
    }

    function updateCountdownDisplay() {
      if (!scheduleEnabled) {
        countdownText.value = "หยุดแล้ว";
        scheduleStatusText.textContent = "สถานะ: หยุดแล้ว";
        return;
      }
      let remainingSeconds = null;
      if (scheduleNextRunAt) {
        remainingSeconds = Math.max(0, Math.floor((Date.parse(scheduleNextRunAt) - Date.now()) / 1000));
      }
      countdownText.value = formatRemaining(remainingSeconds);
      scheduleStatusText.textContent =
        "สถานะ: กำลังนับเวลา" +
        (scheduleNextRunAt ? " | ครั้งถัดไป " + new Date(scheduleNextRunAt).toLocaleString("th-TH") : "");
      if (remainingSeconds === 0 && Date.now() - lastZeroPollAt > 2000) {
        lastZeroPollAt = Date.now();
        refreshScheduleFromServer({ updateResultBox: true });
      }
    }

    function ensureCountdownTimer() {
      if (countdownTimer) {
        return;
      }
      updateCountdownDisplay();
      countdownTimer = setInterval(updateCountdownDisplay, 1000);
    }

    function stopSchedulePolling() {
      if (schedulePollTimer) {
        clearInterval(schedulePollTimer);
        schedulePollTimer = null;
      }
    }

    function startSchedulePolling() {
      stopSchedulePolling();
      if (!scheduleEnabled) {
        return;
      }
      schedulePollTimer = setInterval(() => {
        refreshScheduleFromServer({ updateResultBox: true });
      }, SCHEDULE_POLL_INTERVAL_MS);
    }

    async function refreshScheduleFromServer(options = {}) {
      if (schedulePollInFlight) {
        return;
      }
      schedulePollInFlight = true;
      try {
        const payload = await fetchJson("/api/v1/monitor/source");
        applyScheduleState(payload.schedule || {}, {
          updateResultBox: Boolean(options.updateResultBox),
          logs: payload.logs,
          activePublicSource: payload.activePublicSource,
          rawExportTarget: payload.rawExportTarget,
          sorkorExportTarget: payload.sorkorExportTarget,
          current: payload.current,
          bmc: payload.bmc,
          mock: payload.mock,
        });
      } catch (error) {
        // Background sync should not interrupt manual actions.
      } finally {
        schedulePollInFlight = false;
      }
    }

    function applyScheduleState(schedule, meta) {
      scheduleEnabled = Boolean(schedule && schedule.enabled);
      scheduleNextRunAt = (schedule && schedule.nextRunAt) || null;
      if (schedule && schedule.intervalSeconds) {
        scheduleIntervalInput.value = schedule.intervalSeconds;
      }
      if (meta && meta.current) {
        updateEffectiveBadge(
          meta.current,
          meta.bmc,
          meta.activePublicSource,
          meta.rawExportTarget,
          meta.sorkorExportTarget
        );
      }
      if (meta && meta.bmc) {
        renderBmcConfig(meta.bmc);
      }
      if (meta && meta.activePublicSource) {
        renderActivePublicSource(meta.activePublicSource, meta.rawExportTarget, meta.sorkorExportTarget);
      }
      if (meta && meta.mock) {
        renderMockState(meta.mock);
      }
      if (meta && meta.updateResultBox) {
        resultBox.textContent = JSON.stringify({
          current: meta.current,
          bmc: meta.bmc,
          schedule: schedule,
          mock: meta.mock,
          activePublicSource: meta.activePublicSource,
          bkkExportTarget: meta.bkkExportTarget,
          sorkorExportTarget: meta.sorkorExportTarget,
          rawExportTarget: meta.rawExportTarget,
          logs: meta.logs,
        }, null, 2);
      }
      ensureCountdownTimer();
      updateCountdownDisplay();
      updateScheduleButtons();
      if (scheduleEnabled) {
        startSchedulePolling();
      } else {
        stopSchedulePolling();
      }
    }

    function renderConfig(payload) {
      const config = payload.current || {};
      enabledInput.checked = Boolean(config.enabled);
      urlInput.value = config.url || "";
      timeoutInput.value = config.timeoutSeconds || 10;
      sourceModeInput.value = config.source || "none";
      renderBmcConfig(payload.bmc || {});
      renderActivePublicSource(
        payload.activePublicSource || {},
        payload.rawExportTarget || {},
        payload.sorkorExportTarget || {}
      );
      updateEffectiveBadge(
        config,
        payload.bmc || {},
        payload.activePublicSource || {},
        payload.rawExportTarget || {},
        payload.sorkorExportTarget || {}
      );
      applyScheduleState(payload.schedule || {}, {
        current: config,
        bmc: payload.bmc,
        activePublicSource: payload.activePublicSource,
        bkkExportTarget: payload.bkkExportTarget,
        sorkorExportTarget: payload.sorkorExportTarget,
        rawExportTarget: payload.rawExportTarget,
        logs: payload.logs,
        mock: payload.mock,
        updateResultBox: true,
      });
      renderMockState(payload.mock || {});
      resultBox.textContent = JSON.stringify(payload, null, 2);
    }

    async function loadConfig() {
      setBusy(true);
      try {
        const payload = await fetchJson("/api/v1/monitor/source");
        renderConfig(payload);
        statusText.textContent = "โหลด config ปัจจุบันแล้ว";
      } catch (error) {
        statusText.textContent = "โหลด config ไม่สำเร็จ: " + error.message;
      } finally {
        setBusy(false);
      }
    }

    async function saveConfig() {
      localStorage.setItem("resultsApiKey", apiKeyInput.value.trim());
      setBusy(true);
      statusText.textContent = "กำลังบันทึก...";
      try {
        const payload = await fetchJson("/api/v1/monitor/source", {
          method: "PUT",
          body: JSON.stringify(currentConfigPayload()),
        });
        renderConfig(payload);
        statusText.textContent = "บันทึก config แล้ว";
      } catch (error) {
        statusText.textContent = "บันทึกไม่สำเร็จ: " + error.message;
      } finally {
        setBusy(false);
      }
    }

    function currentConfigPayload(extraFields) {
      return {
        enabled: enabledInput.checked,
        url: urlInput.value.trim(),
        timeoutSeconds: Number(timeoutInput.value || 10),
        bmcEnabled: bmcEnabledInput.checked,
        bmcUrl: bmcUrlInput.value.trim(),
        bmcTimeoutSeconds: Number(bmcTimeoutInput.value || 10),
        activePublicSource: selectedActivePublicSource(),
        scheduleIntervalSeconds: Number(scheduleIntervalInput.value || 300),
        mockIntervalSeconds: Number(mockIntervalInput.value || 8),
        ...(extraFields || {}),
      };
    }

    async function runMockAction(action) {
      localStorage.setItem("resultsApiKey", apiKeyInput.value.trim());
      setBusy(true);
      validateMockAndFetchIntervals();
      statusText.textContent =
        action === "start" ? "กำลังเริ่มจำลอง..." : "กำลังหยุดจำลอง...";
      try {
        const payload = await fetchJson("/api/v1/monitor/source", {
          method: "PUT",
          body: JSON.stringify(currentConfigPayload({ mockAction: action })),
        });
        renderConfig(payload);
        statusText.textContent =
          action === "start" ? "เริ่มจำลองแล้ว — ข้อมูลจะถูกเขียนทับตามช่วง Mock" :
          "หยุดจำลองแล้ว";
      } catch (error) {
        statusText.textContent = "จัดการจำลองไม่สำเร็จ: " + error.message;
        setBusy(false);
      }
    }

    async function runScheduleAction(action) {
      localStorage.setItem("resultsApiKey", apiKeyInput.value.trim());
      setBusy(true);
      if (action === "start" || action === "resume") {
        validateMockAndFetchIntervals();
      }
      statusText.textContent =
        action === "start" ? "กำลังเริ่มนับเวลา..." :
        action === "stop" ? "กำลังหยุดนับเวลา..." :
        "กำลังเริ่มนับใหม่...";
      try {
        const payload = await fetchJson("/api/v1/monitor/source", {
          method: "PUT",
          body: JSON.stringify(currentConfigPayload({ scheduleAction: action })),
        });
        renderConfig(payload);
        statusText.textContent =
          action === "start" ? "เริ่มนับเวลาแล้ว" :
          action === "stop" ? "หยุดนับเวลาแล้ว" :
          "เริ่มนับใหม่แล้ว";
      } catch (error) {
        statusText.textContent = "จัดการ schedule ไม่สำเร็จ: " + error.message;
        setBusy(false);
      }
    }

    async function fetchNow() {
      localStorage.setItem("resultsApiKey", apiKeyInput.value.trim());
      setBusy(true);
      statusText.textContent = "กำลังดึงข้อมูลและ export...";
      try {
        await fetchJson("/api/v1/monitor/source", {
          method: "PUT",
          body: JSON.stringify(currentConfigPayload()),
        });
        const payload = await fetchJson("/api/v1/monitor/source/fetch", {
          method: "POST",
          body: JSON.stringify({}),
        });
        renderConfig({
          current: payload.current,
          schedule: payload.schedule,
          mock: payload.mock,
          activePublicSource: payload.activePublicSource,
          bkkExportTarget: payload.bkkExportTarget,
          rawExportTarget: payload.rawExportTarget,
          logs: payload.logs,
        });
        resultBox.textContent = JSON.stringify(payload, null, 2);
        statusText.textContent = "ดึงข้อมูลและ export สำเร็จ";
      } catch (error) {
        statusText.textContent = "ดึงข้อมูลไม่สำเร็จ: " + error.message;
      } finally {
        setBusy(false);
      }
    }

    document.getElementById("saveButton").addEventListener("click", saveConfig);
    document.getElementById("fetchButton").addEventListener("click", fetchNow);
    startScheduleButton.addEventListener("click", () => runScheduleAction("start"));
    stopScheduleButton.addEventListener("click", () => runScheduleAction("stop"));
    resumeScheduleButton.addEventListener("click", () => runScheduleAction("resume"));
    startMockButton.addEventListener("click", () => runMockAction("start"));
    stopMockButton.addEventListener("click", () => runMockAction("stop"));
    loadConfig();
  </script>
</body>
</html>
"""


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "election-results-api"}


@app.get("/monitor", response_class=HTMLResponse)
def get_monitor_page() -> str:
    return MONITOR_HTML


@app.get("/api/v1/monitor/source", dependencies=[Depends(require_api_key)])
def get_monitor_source() -> dict[str, Any]:
    current = effective_external_governor_results_config()
    return {
        "schemaVersion": "1.0",
        "resource": "governor-results-monitor-source",
        "current": current,
        "bmc": effective_external_bmc_results_config(),
        "bkkExportTarget": bkk_public_export_config(),
        "sorkorExportTarget": sorkor_export_target_config(),
        "rawExportTarget": effective_raw_results_export_config(),
        "activePublicSource": effective_active_public_source_config(),
        "schedule": effective_monitor_schedule_config(),
        "mock": effective_monitor_mock_config(),
        "logs": monitor_fetch_log_entries(),
    }


@app.put("/api/v1/monitor/source", dependencies=[Depends(require_api_key)])
def update_monitor_source(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    saved = validate_monitor_external_source_payload(payload)
    bmc_saved = None
    if any(key in payload for key in ("bmcEnabled", "bmcUrl", "bmcTimeoutSeconds")):
        bmc_saved = validate_monitor_bmc_source_payload(payload)
    schedule = build_monitor_schedule_update(payload)
    active_public_source_saved, promote_source = build_monitor_active_public_source_update(payload)
    mock_saved = None
    if any(key in payload for key in ("mockAction", "mockIntervalSeconds", "mockEnabled")):
        mock_saved = build_monitor_mock_update(
            payload,
            schedule_interval_seconds=int(schedule.get("intervalSeconds") or 300),
            schedule_enabled=bool(schedule.get("enabled")),
        )
    try:
        store.write_json(MONITOR_EXTERNAL_SOURCE_KEY, saved)
        if bmc_saved is not None:
            store.write_json(MONITOR_EXTERNAL_BMC_SOURCE_KEY, bmc_saved)
        write_monitor_schedule(schedule)
        if mock_saved is not None:
            write_monitor_mock(mock_saved)
        write_active_public_source(
            s3_client=store.s3_client,
            bucket=store.bucket,
            score_prefix=settings.prefix,
            source=active_public_source_saved["source"],
            updated_at=active_public_source_saved["updatedAt"],
        )
    except (BotoCoreError, ClientError) as exc:
        raise monitor_storage_error(exc) from exc
    invalidate_result_caches()
    public_promote = None
    public_promote_error = None
    if promote_source is not None:
        try:
            public_promote = maybe_promote_public_results_for_source(promote_source)
        except HTTPException as exc:
            public_promote_error = exc.detail
    response = {
        "schemaVersion": "1.0",
        "resource": "governor-results-monitor-source",
        "saved": saved,
        "activePublicSourceSaved": active_public_source_saved,
        "current": effective_external_governor_results_config(),
        "bmc": effective_external_bmc_results_config(),
        "bkkExportTarget": bkk_public_export_config(),
        "sorkorExportTarget": sorkor_export_target_config(),
        "rawExportTarget": effective_raw_results_export_config(),
        "activePublicSource": effective_active_public_source_config(),
        "schedule": effective_monitor_schedule_config(),
        "mock": effective_monitor_mock_config(),
        "logs": monitor_fetch_log_entries(),
    }
    if bmc_saved is not None:
        response["bmcSaved"] = bmc_saved
    if mock_saved is not None:
        response["mockSaved"] = mock_saved
    if public_promote is not None:
        response["publicPromote"] = public_promote
    if public_promote_error is not None:
        response["publicPromoteError"] = public_promote_error
    return response


@app.post("/api/v1/monitor/source/fetch", dependencies=[Depends(require_api_key)])
def fetch_monitor_source(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    del payload
    return perform_monitor_fetch(trigger="manual")


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
            without_nulls(
                {
                    "area_id": area_id,
                    "district_code": district.get("districtCode"),
                    "district_name_th": district.get("districtNameTh"),
                    "district_name_en": district.get("districtNameEn"),
                    "submission_count": int(index.get("submission_count") or 0),
                    "approved_submission_count": len(approved_results),
                    "latest_approved_at": approved_results[0].get("approved_at") if approved_results else None,
                }
            )
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
    return without_nulls(
        {
            "election_id": election_id,
            "area_id": area_id,
            "district_code": district.get("districtCode"),
            "district_name_th": district.get("districtNameTh"),
            "district_name_en": district.get("districtNameEn"),
            "submission_count": int(index.get("submission_count") or 0),
            "approved_submission_count": len(approved_results),
            "latest_approved_result": approved_results[0] if approved_results else None,
        }
    )


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


def read_static_export(relative_name: str) -> dict[str, Any] | None:
    prefix = static_results_prefix_for_target(LIVE_TARGET).strip().strip("/")
    key = f"{prefix}/{relative_name}" if prefix else relative_name
    try:
        payload = store.read_absolute_json(key)
    except (BotoCoreError, ClientError):
        return None
    return payload if isinstance(payload, dict) else None


def build_raw_export_keys(prefix: str, *, captured_at: str) -> dict[str, str]:
    normalized_prefix = prefix.strip().strip("/")
    timestamp_for_name = captured_at.replace(":", "-")
    latest_key = f"{normalized_prefix}/raw/latest.json" if normalized_prefix else "raw/latest.json"
    history_key = (
        f"{normalized_prefix}/raw/history/{timestamp_for_name}.json"
        if normalized_prefix
        else f"raw/history/{timestamp_for_name}.json"
    )
    return {
        "latestKey": latest_key,
        "historyKey": history_key,
    }


def export_raw_governor_results(raw_payload: dict[str, Any]) -> dict[str, Any]:
    export_target = effective_raw_results_export_config()
    prefix = export_target["prefix"]
    captured_at = utc_now_iso()
    keys = build_raw_export_keys(prefix, captured_at=captured_at)
    envelope = {
        "schemaVersion": "1.0",
        "resource": "governor-results-raw",
        "capturedAt": captured_at,
        "sourceConfig": {
            "url": effective_external_governor_results_config().get("url"),
            "timeoutSeconds": effective_external_governor_results_config().get("timeoutSeconds"),
        },
        "exportTarget": {
            "target": export_target["target"],
            "prefix": prefix,
        },
        "payload": raw_payload,
    }
    store.write_absolute_json(keys["latestKey"], envelope)
    store.write_absolute_json(keys["historyKey"], envelope)
    return {
        "target": export_target["target"],
        "prefix": prefix,
        "capturedAt": captured_at,
        **keys,
    }


def export_raw_bmc_results(raw_payload: dict[str, Any]) -> dict[str, Any]:
    prefix = bmc_raw_export_prefix()
    captured_at = utc_now_iso()
    keys = build_raw_export_keys(prefix, captured_at=captured_at)
    envelope = {
        "schemaVersion": "1.0",
        "resource": "bmc-results-raw",
        "capturedAt": captured_at,
        "sourceConfig": {
            "url": effective_external_bmc_results_config().get("url"),
            "timeoutSeconds": effective_external_bmc_results_config().get("timeoutSeconds"),
        },
        "exportTarget": {
            "target": BKK_TARGET,
            "prefix": prefix,
        },
        "payload": raw_payload,
    }
    store.write_absolute_json(keys["latestKey"], envelope)
    store.write_absolute_json(keys["historyKey"], envelope)
    return {
        "target": BKK_TARGET,
        "prefix": prefix,
        "capturedAt": captured_at,
        **keys,
    }


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
    if settings.enable_static_results_fallback and not approved_results:
        static_payload = read_static_export("sumary.json")
        if static_payload:
            return static_payload
    payload = build_governor_results(
        approved_results=approved_results,
        candidate_catalog=candidates_by_number,
        election_id=settings.election_id,
        title=settings.election_title,
        result_status=settings.result_status,
        total_units=total_units,
        delayed_after_minutes=settings.delayed_after_minutes,
    )
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
    if settings.enable_static_results_fallback and not approved_results:
        static_payload = read_static_export("districts.json")
        if static_payload:
            return static_payload

    return build_district_results(
        approved_results=approved_results,
        candidate_catalog=candidates_by_number,
        district_catalog=districts_by_id,
    )


def export_static_governor_results() -> dict[str, Any]:
    summary_payload = governor_results_response(use_cache=False)
    districts_payload = _build_governor_district_results_response()
    prefix = static_results_prefix_for_target(LINE_TARGET)
    summary_key = f"{prefix}/sumary.json" if prefix else "sumary.json"
    districts_key = f"{prefix}/districts.json" if prefix else "districts.json"
    store.write_absolute_json(summary_key, summary_payload)
    store.write_absolute_json(districts_key, districts_payload)
    return {
        "target": LINE_TARGET,
        "prefix": prefix,
        "summaryKey": summary_key,
        "districtsKey": districts_key,
        "dataMode": summary_payload.get("dataInterpretation", {}).get("mode"),
    }


@app.get("/api/v1/governor-results/summary", dependencies=[Depends(require_api_key)])
def get_governor_results_summary(
    fresh: bool = Query(default=False),
) -> dict[str, Any]:
    return governor_results_response(use_cache=not fresh)


@app.get("/api/v1/governor-results/districts", dependencies=[Depends(require_api_key)])
def get_governor_district_results(
    fresh: bool = Query(default=False),
) -> dict[str, Any]:
    if fresh:
        invalidate_result_caches()
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
