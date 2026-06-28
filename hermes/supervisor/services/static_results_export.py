from __future__ import annotations

import json
import os
from typing import Any
from urllib import request

import boto3

from hermes.governor_results.public_source import (
    DEFAULT_PUBLIC_SOURCE,
    LINE_TARGET,
    parent_prefix_from_static_prefix,
    promote_public_results,
    read_active_public_source,
    source_target_for_public_source,
)


def read_json_url(url: str, *, api_key: str | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "election-line-relay/1.0"}
    if api_key:
        headers["x-api-key"] = api_key
    upstream_request = request.Request(url, headers=headers)
    with request.urlopen(upstream_request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def export_static_governor_results() -> dict[str, Any] | None:
    bucket = os.environ.get("STATIC_RESULTS_S3_BUCKET", "").strip() or os.environ.get("SUPERVISOR_S3_BUCKET", "").strip()
    if not bucket:
        return None
    api_base_url = os.environ.get("RESULTS_API_INTERNAL_BASE_URL", "http://results-api:8080").strip().rstrip("/")
    static_prefix = (
        os.environ.get("STATIC_RESULTS_PREFIX", "").strip()
        or os.environ.get("GOVERNOR_RESULTS_PREFIX", "").strip()
        or "api-data/governor-results"
    ).strip().strip("/")
    score_prefix = (
        os.environ.get("ELECTION_S3_PREFIX", "").strip()
        or os.environ.get("SUPERVISOR_S3_PREFIX", "").strip()
        or os.environ.get("RESULTS_API_S3_PREFIX", "").strip()
    ).strip().strip("/")
    region = os.environ.get("STATIC_RESULTS_S3_REGION", "").strip() or os.environ.get("SUPERVISOR_S3_REGION", "").strip() or None
    api_key = os.environ.get("RESULTS_API_KEY", "").strip() or None
    summary = read_json_url(f"{api_base_url}/api/v1/governor-results/summary?fresh=1", api_key=api_key)
    districts = read_json_url(f"{api_base_url}/api/v1/governor-results/districts?fresh=1", api_key=api_key)
    s3_client = boto3.client("s3", region_name=region)
    keys = {
        "summaryKey": f"{static_prefix}/sumary.json" if static_prefix else "sumary.json",
        "districtsKey": f"{static_prefix}/districts.json" if static_prefix else "districts.json",
    }
    for key_name, payload in (("summaryKey", summary), ("districtsKey", districts)):
        s3_client.put_object(
            Bucket=bucket,
            Key=keys[key_name],
            Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json; charset=utf-8",
        )
    result: dict[str, Any] = {
        "status": "static_results_exported",
        "keys": [keys["summaryKey"], keys["districtsKey"]],
        "dataMode": summary.get("dataInterpretation", {}).get("mode"),
    }
    try:
        active_source = read_active_public_source(
            s3_client=s3_client,
            bucket=bucket,
            score_prefix=score_prefix,
        ).get("source", DEFAULT_PUBLIC_SOURCE)
    except Exception:
        active_source = DEFAULT_PUBLIC_SOURCE
    if active_source == "line":
        parent_prefix = parent_prefix_from_static_prefix(static_prefix)
        try:
            result["publicPromote"] = promote_public_results(
                s3_client=s3_client,
                bucket=bucket,
                parent_prefix=parent_prefix,
                source_target=LINE_TARGET,
            )
        except Exception as exc:
            result["publicPromoteError"] = str(exc)
    return result
