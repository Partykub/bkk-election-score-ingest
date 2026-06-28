from __future__ import annotations

import json
import os
import re
from threading import Lock
from time import monotonic
from typing import Any

import boto3

from hermes.governor_results.http_fetch import fetch_json_http

_AREA_LOOKUP: DistrictLookup | None = None
_AREA_LOOKUP_LOCK = Lock()

AREA_PREFIX_PATTERN = r"(?:เขต|แขต|khet|area|district|constituency|เขตเลือกตั้ง)"
AREA_NUMBER_PATTERNS: tuple[str, ...] = (
    rf"{AREA_PREFIX_PATTERN}\s*(?:ที่|เบอร์|no\.?|number|#)?\s*(?:=|เป็น|คือ|ควรเป็น|:|-)?\s*(\d{{1,2}})\b",
    rf"{AREA_PREFIX_PATTERN}(\d{{1,2}})\b",
    r"(\d{1,2})\s*(?:เขต|แขต|khet|area|district|constituency)\b",
    r"(\d{1,2})(?:เขต|แขต)\b",
    r"ลำดับ(?:เขต)?\s*(?:ที่)?\s*(\d{1,2})\b",
    r"(?:^|\s)(?:no\.?|#)\s*(\d{1,2})(?:\s|$|[,.])",
)
AREA_NAME_GLUE_PATTERN = re.compile(rf"{AREA_PREFIX_PATTERN}([\u0e00-\u0e7f]{{2,}})", re.IGNORECASE)
CORRECTION_PREFIX_PATTERN = re.compile(r"^(?:แก้ไข|แก้)\s*", re.IGNORECASE)


def normalize_text_key(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    return text.casefold()


def sanitize_area_token(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    text = text.strip("\"'`“”‘’()[]{}<>")
    text = re.sub(r"^(?:เขต|แขต|khet|area|district|constituency)\s*[.:]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*(?:กทม|กรุงเทพ(?:มหานคร)?|bangkok)\.?\s*$", "", text, flags=re.IGNORECASE).strip()
    return " ".join(text.split())


def parse_s3_uri(source: str) -> tuple[str, str] | None:
    if not source.startswith("s3://"):
        return None
    remainder = source[5:]
    if "/" not in remainder:
        return None
    bucket, key = remainder.split("/", 1)
    if not bucket or not key:
        return None
    return bucket, key


def normalize_election_area_districts(payload: Any) -> list[dict[str, Any]]:
    election_areas: list[Any] = []
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
    return normalized


def normalize_district_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return normalize_election_area_districts(payload)


def read_districts_payload(*, source: str, timeout_seconds: float, s3_client: Any | None = None) -> Any:
    s3_location = parse_s3_uri(source)
    if s3_location is not None:
        bucket, key = s3_location
        client = s3_client or boto3.client("s3")
        response = client.get_object(Bucket=bucket, Key=key)
        return json.loads(response["Body"].read().decode("utf-8"))
    return fetch_json_http(source, timeout_seconds=timeout_seconds)


def districts_url_from_env() -> str:
    return (
        os.environ.get("DISTRICTS_URL", "").strip()
        or os.environ.get("RESULTS_API_DISTRICTS_URL", "").strip()
    )


def strip_area_prefix(raw: str) -> str:
    text = sanitize_area_token(raw)
    if not text:
        return ""
    stripped = re.sub(
        rf"^{AREA_PREFIX_PATTERN}\s*(?:ที่|เบอร์|no\.?|number|#)?\s*(?:=|เป็น|คือ|ควรเป็น|:|-)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    if stripped != text:
        return stripped
    return re.sub(rf"^(?:เขต|แขต)(?=\S)", "", text, flags=re.IGNORECASE).strip()


def register_district_name_aliases(by_name: dict[str, dict[str, Any]], district: dict[str, Any], raw_name: Any) -> None:
    name = str(raw_name or "").strip()
    if not name:
        return
    keys = {
        normalize_text_key(name),
        normalize_text_key(strip_area_prefix(name)),
        normalize_text_key(sanitize_area_token(name)),
    }
    th_name = str(district.get("districtNameTh") or "").strip()
    if th_name and th_name in name:
        keys.add(normalize_text_key(f"เขต{th_name}"))
        keys.add(normalize_text_key(f"เขต {th_name}"))
    for key in keys:
        if key and key not in by_name:
            by_name[key] = district


def extract_area_numbers_from_text(text: str | None) -> list[str]:
    if not str(text or "").strip():
        return []
    found: list[str] = []
    seen: set[str] = set()
    for pattern in AREA_NUMBER_PATTERNS:
        for match in re.finditer(pattern, str(text), flags=re.IGNORECASE):
            value = match.group(1)
            if value and value not in seen:
                seen.add(value)
                found.append(value)
    return found


def extract_glued_area_names_from_text(text: str | None) -> list[str]:
    if not str(text or "").strip():
        return []
    names: list[str] = []
    seen: set[str] = set()
    for match in AREA_NAME_GLUE_PATTERN.finditer(str(text)):
        candidate = sanitize_area_token(match.group(1))
        key = normalize_text_key(candidate)
        if candidate and key and key not in seen:
            seen.add(key)
            names.append(candidate)
    return names


def iter_document_hint_strings(
    *,
    area_id: Any = None,
    notes: Any = None,
    summary_text: Any = None,
    raw_model_text: Any = None,
) -> list[str]:
    hints: list[str] = []
    seen: set[str] = set()
    for value in (area_id, notes, summary_text, raw_model_text):
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        hints.append(text)
        sanitized = sanitize_area_token(text)
        if sanitized and sanitized not in seen:
            seen.add(sanitized)
            hints.append(sanitized)
        stripped = strip_area_prefix(text)
        if stripped and stripped not in seen:
            seen.add(stripped)
            hints.append(stripped)
    combined = " ".join(hints)
    if combined and combined not in seen:
        hints.append(combined)
    for number in extract_area_numbers_from_text(combined):
        token = f"เขต {number}"
        if token not in seen:
            seen.add(token)
            hints.append(token)
    for name in extract_glued_area_names_from_text(combined):
        if name not in seen:
            seen.add(name)
            hints.append(name)
    return hints


class DistrictLookup:
    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: float = 5,
        cache_seconds: int = 300,
        s3_client: Any | None = None,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        self._s3_client = s3_client
        self._cached_at = 0.0
        self._districts: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._by_name: dict[str, dict[str, Any]] = {}
        self._lock = Lock()

    def _load(self) -> None:
        now = monotonic()
        if self._districts and now - self._cached_at < self.cache_seconds:
            return
        with self._lock:
            now = monotonic()
            if self._districts and now - self._cached_at < self.cache_seconds:
                return
            payload = read_districts_payload(
                source=self.url,
                timeout_seconds=self.timeout_seconds,
                s3_client=self._s3_client,
            )
            districts = normalize_district_list(payload)
            by_id: dict[str, dict[str, Any]] = {}
            by_name: dict[str, dict[str, Any]] = {}
            for district in districts:
                if district.get("provinceCode") not in (None, 10):
                    continue
                district_id = district.get("id")
                if district_id is None:
                    continue
                by_id[str(district_id)] = district
                area_number = district.get("areaNumber")
                if area_number is not None:
                    by_id[str(area_number)] = district
                election_area_id = district.get("electionAreaId")
                if election_area_id is not None:
                    by_id[str(election_area_id)] = district
                th_name = district.get("districtNameTh")
                en_name = district.get("districtNameEn")
                register_district_name_aliases(by_name, district, th_name)
                register_district_name_aliases(by_name, district, en_name)
                if th_name:
                    register_district_name_aliases(by_name, district, f"เขต{th_name}")
                    register_district_name_aliases(by_name, district, f"เขต {th_name}")
            self._districts = districts
            self._by_id = by_id
            self._by_name = by_name
            self._cached_at = monotonic()

    def district_for_area_id(self, area_id: str | None) -> dict[str, Any] | None:
        if not str(area_id or "").strip():
            return None
        self._load()
        return self._by_id.get(str(area_id).strip())

    def resolve_area_id(self, raw: str | None) -> str | None:
        text = strip_area_prefix(str(raw or "").strip())
        if not text:
            return None

        trailing_area_match = re.search(r"^(\d{1,2})\s*(?:เขต|แขต|khet|area|district|constituency)\b", text, re.IGNORECASE)
        if trailing_area_match:
            text = trailing_area_match.group(1)

        self._load()

        district = self._by_id.get(text)
        if district is not None:
            return str(district.get("id"))

        name_key = normalize_text_key(text)
        if name_key:
            district = self._by_name.get(name_key)
            if district is not None:
                return str(district.get("id"))

        for number in extract_area_numbers_from_text(str(raw or "")):
            district = self._by_id.get(number)
            if district is not None:
                return str(district.get("id"))
            if number.isdigit():
                return number

        for glued_name in extract_glued_area_names_from_text(str(raw or "")):
            resolved = self.resolve_area_id(glued_name)
            if resolved:
                return resolved

        if text.isdigit():
            return text
        return None

    def find_area_id_in_text_blob(self, text: str | None) -> str | None:
        combined_key = normalize_text_key(text)
        if not combined_key:
            return None
        self._load()

        for number in extract_area_numbers_from_text(str(text or "")):
            district = self._by_id.get(number)
            if district is not None:
                return str(district.get("id"))

        for glued_name in extract_glued_area_names_from_text(str(text or "")):
            resolved = self.resolve_area_id(glued_name)
            if resolved:
                return resolved

        matches: list[tuple[int, dict[str, Any]]] = []
        for name_key, district in self._by_name.items():
            if name_key and name_key in combined_key:
                matches.append((len(name_key), district))
        if not matches:
            return None
        matches.sort(key=lambda item: (-item[0], int(item[1].get("id") or 0)))
        return str(matches[0][1].get("id"))

    def format_area_label(self, area_id: str | None) -> str:
        normalized_area_id = str(area_id or "").strip()
        if not normalized_area_id:
            return "เขต: ยังไม่พบ"

        district = self.district_for_area_id(normalized_area_id)
        if district is None:
            return f"เขต: {normalized_area_id}"

        number = district.get("areaNumber") or district.get("id")
        name = str(district.get("districtNameTh") or district.get("districtNameEn") or "").strip()
        if name and number is not None:
            return f"เขต: เขต {number} {name}"
        if name:
            return f"เขต: {name}"
        return f"เขต: {normalized_area_id}"


def get_district_lookup() -> DistrictLookup | None:
    global _AREA_LOOKUP
    url = districts_url_from_env()
    if not url:
        return None
    with _AREA_LOOKUP_LOCK:
        if _AREA_LOOKUP is None or _AREA_LOOKUP.url != url:
            timeout_seconds = float(
                os.environ.get("DISTRICTS_TIMEOUT_SECONDS")
                or os.environ.get("RESULTS_API_DISTRICTS_TIMEOUT_SECONDS", "5")
            )
            cache_seconds = int(
                os.environ.get("DISTRICTS_CACHE_SECONDS")
                or os.environ.get("RESULTS_API_DISTRICTS_CACHE_SECONDS", "300")
            )
            _AREA_LOOKUP = DistrictLookup(
                url=url,
                timeout_seconds=timeout_seconds,
                cache_seconds=cache_seconds,
            )
        return _AREA_LOOKUP


def normalize_area_id_value(raw: str | None) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    lookup = get_district_lookup()
    if lookup is None:
        return text if text.isdigit() else None
    resolved = lookup.resolve_area_id(text)
    return resolved or (text if text.isdigit() else None)


def infer_area_id_from_document_hints(
    *,
    area_id: Any = None,
    notes: Any = None,
    summary_text: Any = None,
    raw_model_text: Any = None,
) -> str | None:
    lookup = get_district_lookup()
    hints = iter_document_hint_strings(
        area_id=area_id,
        notes=notes,
        summary_text=summary_text,
        raw_model_text=raw_model_text,
    )
    for candidate in hints:
        resolved = normalize_area_id_value(candidate)
        if resolved:
            return resolved
    if lookup is None:
        return None
    for candidate in hints:
        resolved = lookup.find_area_id_in_text_blob(candidate)
        if resolved:
            return resolved
    return lookup.find_area_id_in_text_blob(" ".join(hints)) if hints else None


def format_area_label(area_id: str | None) -> str:
    lookup = get_district_lookup()
    if lookup is None:
        normalized_area_id = str(area_id or "").strip()
        return f"เขต: {normalized_area_id}" if normalized_area_id else "เขต: ยังไม่พบ"
    return lookup.format_area_label(area_id)


def parse_area_id_from_text(source_text: str | None) -> str | None:
    if not source_text:
        return None

    normalized_text = CORRECTION_PREFIX_PATTERN.sub("", source_text.strip()).strip()
    if not normalized_text:
        return None

    direct = infer_area_id_from_document_hints(area_id=normalized_text)
    if direct:
        return direct

    label_match = re.search(
        rf"{AREA_PREFIX_PATTERN}\s*(?:ที่|เบอร์|no\.?|number|#)?\s*(?:=|เป็น|คือ|ควรเป็น|:|-)?\s*(.+?)\s*$",
        normalized_text,
        re.IGNORECASE,
    )
    if label_match:
        resolved = normalize_area_id_value(label_match.group(1).strip())
        if resolved:
            return resolved

    return infer_area_id_from_document_hints(area_id=normalized_text, notes=normalized_text)
