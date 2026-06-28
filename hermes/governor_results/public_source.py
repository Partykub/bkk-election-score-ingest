from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError

ACTIVE_PUBLIC_SOURCE_KEY = "monitor/config/active-public-source.json"
LIVE_TARGET = "governor-results"
LINE_TARGET = "governor-results-dev"
BKK_TARGET = "governor-results-bkk"
AVAILABLE_PUBLIC_SOURCES = ("line", "bkk")
DEFAULT_PUBLIC_SOURCE = "line"
PUBLIC_EXPORT_FILES = ("sumary.json", "districts.json")
SORKOR_EXPORT_FILES = ("sumary-sorkor.json", "districts-sorkor.json")


class PublicSourceNotFoundError(FileNotFoundError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parent_prefix_from_static_prefix(static_results_prefix: str) -> str:
    prefix = static_results_prefix.strip().strip("/")
    if not prefix or "/" not in prefix:
        return ""
    return prefix.rsplit("/", 1)[0]


def prefix_for_target(target: str, parent_prefix: str) -> str:
    normalized_target = target.strip().strip("/")
    normalized_parent = parent_prefix.strip().strip("/")
    if normalized_parent:
        return f"{normalized_parent}/{normalized_target}"
    return normalized_target


def normalize_public_source(value: Any) -> str:
    source = str(value or "").strip().lower() or DEFAULT_PUBLIC_SOURCE
    if source not in AVAILABLE_PUBLIC_SOURCES:
        allowed = ", ".join(AVAILABLE_PUBLIC_SOURCES)
        raise ValueError(f"activePublicSource must be one of: {allowed}.")
    return source


def source_target_for_public_source(source: str) -> str:
    normalized = normalize_public_source(source)
    if normalized == "line":
        return LINE_TARGET
    return BKK_TARGET


def optional_promote_files_for_source(source: str) -> tuple[str, ...]:
    if normalize_public_source(source) == "bkk":
        return SORKOR_EXPORT_FILES
    return ()


def monitor_config_key(score_prefix: str, relative_key: str = ACTIVE_PUBLIC_SOURCE_KEY) -> str:
    normalized_prefix = score_prefix.strip().strip("/")
    normalized_key = relative_key.strip().lstrip("/")
    if normalized_prefix:
        return f"{normalized_prefix}/{normalized_key}"
    return normalized_key


def _read_json_object(*, s3_client: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", "")).lower()
        if code in {"nosuchkey", "404", "notfound"}:
            return None
        raise
    payload = json.loads(response["Body"].read().decode("utf-8"))
    return payload if isinstance(payload, dict) else None


def _write_json_object(*, s3_client: Any, bucket: str, key: str, payload: dict[str, Any]) -> None:
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )


def read_active_public_source(*, s3_client: Any, bucket: str, score_prefix: str) -> dict[str, Any]:
    payload = _read_json_object(
        s3_client=s3_client,
        bucket=bucket,
        key=monitor_config_key(score_prefix),
    )
    if not payload:
        return {
            "source": DEFAULT_PUBLIC_SOURCE,
            "updatedAt": None,
            "savedConfig": {},
        }
    return {
        "source": normalize_public_source(payload.get("source")),
        "updatedAt": payload.get("updatedAt"),
        "savedConfig": payload,
    }


def write_active_public_source(
    *,
    s3_client: Any,
    bucket: str,
    score_prefix: str,
    source: str,
    updated_at: str | None = None,
) -> dict[str, Any]:
    saved = {
        "source": normalize_public_source(source),
        "updatedAt": updated_at or utc_now_iso(),
    }
    _write_json_object(
        s3_client=s3_client,
        bucket=bucket,
        key=monitor_config_key(score_prefix),
        payload=saved,
    )
    return saved


def promote_public_results(
    *,
    s3_client: Any,
    bucket: str,
    parent_prefix: str,
    source_target: str,
    optional_files: tuple[str, ...] = (),
) -> dict[str, Any]:
    source_prefix = prefix_for_target(source_target, parent_prefix)
    live_prefix = prefix_for_target(LIVE_TARGET, parent_prefix)
    promoted_keys: dict[str, str] = {}
    skipped_optional: list[str] = []

    def copy_file(file_name: str, *, required: bool) -> None:
        source_key = f"{source_prefix}/{file_name}"
        live_key = f"{live_prefix}/{file_name}"
        try:
            response = s3_client.get_object(Bucket=bucket, Key=source_key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", "")).lower()
            if code in {"nosuchkey", "404", "notfound"}:
                if required:
                    raise PublicSourceNotFoundError(f"Missing source object: s3://{bucket}/{source_key}") from exc
                skipped_optional.append(file_name)
                return
            raise
        body = response["Body"].read()
        content_type = response.get("ContentType") or "application/json; charset=utf-8"
        s3_client.put_object(
            Bucket=bucket,
            Key=live_key,
            Body=body,
            ContentType=content_type,
        )
        promoted_keys[file_name] = live_key

    for file_name in PUBLIC_EXPORT_FILES:
        copy_file(file_name, required=True)
    for file_name in optional_files:
        copy_file(file_name, required=False)

    return {
        "sourceTarget": source_target,
        "sourcePrefix": source_prefix,
        "livePrefix": live_prefix,
        "keys": promoted_keys,
        "skippedOptional": skipped_optional,
    }


def effective_active_public_source_config(*, parent_prefix: str, saved: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = saved or {}
    source = normalize_public_source(payload.get("source"))
    return {
        "source": source,
        "updatedAt": payload.get("updatedAt"),
        "availableSources": list(AVAILABLE_PUBLIC_SOURCES),
        "livePrefix": prefix_for_target(LIVE_TARGET, parent_prefix),
        "linePrefix": prefix_for_target(LINE_TARGET, parent_prefix),
        "bkkPrefix": prefix_for_target(BKK_TARGET, parent_prefix),
        "savedConfig": payload,
    }
