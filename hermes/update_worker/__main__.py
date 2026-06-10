from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from dataclasses import asdict, dataclass
from typing import Any
from urllib import error, request

import boto3


@dataclass(frozen=True)
class WorkerConfig:
    queue_url: str | None
    aws_region: str | None
    s3_bucket: str | None
    s3_prefix: str | None
    target_api_base_url: str | None
    poll_seconds: int
    queue_max_messages: int
    queue_wait_seconds: int
    queue_visibility_timeout: int


@dataclass(frozen=True)
class QueueEnvelope:
    update_job_id: str
    manifest_bucket: str
    manifest_key: str


@dataclass(frozen=True)
class DownloadedUpdateJob:
    update_job_id: str
    source_message_id: str
    workflow_session_id: str
    manifest_bucket: str
    manifest_key: str
    queue_message_id: str | None
    receipt_handle: str | None


def build_config() -> WorkerConfig:
    queue_url = os.environ.get("UPDATE_WORKER_QUEUE_URL", "").strip() or None
    aws_region = os.environ.get("UPDATE_WORKER_AWS_REGION", "").strip() or None
    s3_bucket = os.environ.get("UPDATE_WORKER_S3_BUCKET", "").strip() or None
    s3_prefix = os.environ.get("UPDATE_WORKER_S3_PREFIX", "").strip() or None
    target_api_base_url = os.environ.get("UPDATE_WORKER_TARGET_API_BASE_URL", "").strip() or None
    poll_seconds = int(os.environ.get("UPDATE_WORKER_POLL_SECONDS", "15"))
    queue_max_messages = int(os.environ.get("UPDATE_WORKER_QUEUE_MAX_MESSAGES", "1"))
    queue_wait_seconds = int(os.environ.get("UPDATE_WORKER_QUEUE_WAIT_SECONDS", "10"))
    queue_visibility_timeout = int(os.environ.get("UPDATE_WORKER_QUEUE_VISIBILITY_TIMEOUT", "300"))

    return WorkerConfig(
        queue_url=queue_url,
        aws_region=aws_region,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        target_api_base_url=target_api_base_url,
        poll_seconds=max(1, poll_seconds),
        queue_max_messages=max(1, min(10, queue_max_messages)),
        queue_wait_seconds=max(0, min(20, queue_wait_seconds)),
        queue_visibility_timeout=max(1, queue_visibility_timeout),
    )


def manifest_key_for_job(update_job_id: str) -> str:
    return f"updates/jobs/{update_job_id}.json"


def update_job_id_from_manifest_key(manifest_key: str) -> str:
    return manifest_key.rsplit("/", 1)[-1].removesuffix(".json")


def parse_queue_envelope(message_body: str, *, default_bucket: str | None) -> QueueEnvelope:
    payload = json.loads(message_body.strip())

    if isinstance(payload, str):
        update_job_id = payload.strip()
        manifest_key = manifest_key_for_job(update_job_id)
        manifest_bucket = default_bucket
    elif isinstance(payload, dict):
        update_job_id = str(payload.get("update_job_id") or "").strip()
        manifest_key = str(payload.get("manifest_key") or "").strip()
        manifest_bucket = str(payload.get("manifest_bucket") or payload.get("bucket") or default_bucket or "").strip()
        if not manifest_key and update_job_id:
            manifest_key = manifest_key_for_job(update_job_id)
        if manifest_key and not update_job_id:
            update_job_id = update_job_id_from_manifest_key(manifest_key)
    else:
        raise ValueError("Update queue message must be a string or JSON object")

    if not update_job_id:
        raise ValueError("Update queue message is missing update_job_id")
    if not manifest_key:
        raise ValueError("Update queue message is missing manifest_key")
    if not manifest_bucket:
        raise ValueError("Update queue message is missing manifest_bucket and UPDATE_WORKER_S3_BUCKET is not set")

    return QueueEnvelope(
        update_job_id=update_job_id,
        manifest_bucket=manifest_bucket,
        manifest_key=manifest_key,
    )


def build_runtime_config_log(config: WorkerConfig) -> dict[str, Any]:
    return asdict(config)


def utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def with_s3_prefix(relative_key: str, *, prefix: str | None) -> str:
    normalized = relative_key.replace("\\", "/").lstrip("/")
    clean_prefix = str(prefix or "").strip("/")
    if clean_prefix and not normalized.startswith(f"{clean_prefix}/"):
        return f"{clean_prefix}/{normalized}"
    return normalized


def source_manifest_key(source_message_id: str) -> str:
    return f"manifests/source-messages/{source_message_id}.json"


def is_missing_key_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    error_payload = response.get("Error", {}) if isinstance(response, dict) else {}
    error_code = str(error_payload.get("Code", "")).lower()
    return error_code in {"nosuchkey", "404", "notfound", "nosuchbucket"}


def read_json_object(s3_client: Any, *, bucket: str, key: str) -> dict[str, Any]:
    response = s3_client.get_object(Bucket=bucket, Key=key)
    return json.loads(response["Body"].read().decode("utf-8"))


def read_json_object_if_exists(s3_client: Any, *, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return read_json_object(s3_client, bucket=bucket, key=key)
    except Exception as exc:
        if is_missing_key_error(exc):
            return None
        raise


def write_json_object(s3_client: Any, *, bucket: str, key: str, payload: dict[str, Any]) -> None:
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        ContentType="application/json",
    )


def fetch_job_from_queue_message(message: dict[str, Any], *, s3_client: Any, config: WorkerConfig) -> DownloadedUpdateJob:
    envelope = parse_queue_envelope(str(message.get("Body") or ""), default_bucket=config.s3_bucket)
    manifest_key = with_s3_prefix(envelope.manifest_key, prefix=config.s3_prefix)
    update_job_manifest = read_json_object(s3_client, bucket=envelope.manifest_bucket, key=manifest_key)
    source_message_id = str(update_job_manifest.get("source_message_id") or "").strip()
    workflow_session_id = str(update_job_manifest.get("workflow_session_id") or "").strip()
    if not source_message_id:
        raise ValueError(f"Update job {envelope.update_job_id} is missing source_message_id")
    return DownloadedUpdateJob(
        update_job_id=envelope.update_job_id,
        source_message_id=source_message_id,
        workflow_session_id=workflow_session_id,
        manifest_bucket=envelope.manifest_bucket,
        manifest_key=manifest_key,
        queue_message_id=message.get("MessageId"),
        receipt_handle=message.get("ReceiptHandle"),
    )


def poll_queue_once(*, queue_client: Any, s3_client: Any, config: WorkerConfig) -> list[DownloadedUpdateJob]:
    if queue_client is None or not config.queue_url:
        return []
    response = queue_client.receive_message(
        QueueUrl=config.queue_url,
        MaxNumberOfMessages=config.queue_max_messages,
        WaitTimeSeconds=config.queue_wait_seconds,
        VisibilityTimeout=config.queue_visibility_timeout,
    )
    messages = response.get("Messages") or []
    return [fetch_job_from_queue_message(message, s3_client=s3_client, config=config) for message in messages]


def acknowledge_job(*, queue_client: Any, config: WorkerConfig, downloaded_job: DownloadedUpdateJob) -> None:
    if queue_client is None or not config.queue_url or not downloaded_job.receipt_handle:
        return
    queue_client.delete_message(QueueUrl=config.queue_url, ReceiptHandle=downloaded_job.receipt_handle)


def call_target_api(*, config: WorkerConfig, update_job_manifest: dict[str, Any], opener: Any = request.urlopen) -> dict[str, Any]:
    if not config.target_api_base_url:
        raise RuntimeError("UPDATE_WORKER_TARGET_API_BASE_URL is not set")

    payload = {
        "update_job_id": update_job_manifest.get("update_job_id"),
        "source_message_id": update_job_manifest.get("source_message_id"),
        "draft_id": update_job_manifest.get("draft_id"),
        "approval_id": update_job_manifest.get("approval_id"),
        "workflow_session_id": update_job_manifest.get("workflow_session_id"),
        "idempotency_key": update_job_manifest.get("idempotency_key"),
        "payload": update_job_manifest.get("payload") or {},
    }
    req = request.Request(
        f"{config.target_api_base_url.rstrip('/')}/updates",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(req, timeout=60) as response:
            raw_body = response.read().decode("utf-8")
            try:
                parsed_body = json.loads(raw_body) if raw_body.strip() else {}
            except json.JSONDecodeError:
                parsed_body = {"raw_body": raw_body}
            return {
                "status_code": getattr(response, "status", None),
                "endpoint": f"{config.target_api_base_url.rstrip('/')}/updates",
                "body": parsed_body,
            }
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"target API request failed with status {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"target API request failed: {exc.reason}") from exc


def build_s3_only_result(*, update_job_manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": "s3_only",
        "message": "No downstream API configured; canonical result stored in S3",
        "update_job_id": update_job_manifest.get("update_job_id"),
        "draft_id": update_job_manifest.get("draft_id"),
        "approval_id": update_job_manifest.get("approval_id"),
    }


def persist_failure(
    *,
    s3_client: Any,
    downloaded_job: DownloadedUpdateJob,
    prefix: str | None,
    update_job_manifest: dict[str, Any] | None,
    source_manifest: dict[str, Any] | None,
    code: str,
    message: str,
    timestamp: str,
) -> None:
    if update_job_manifest is not None:
        update_job_manifest["state"] = "failed"
        update_job_manifest["error"] = {"code": code, "message": message}
        update_job_manifest["updated_at"] = timestamp
        update_job_manifest["attempt_count"] = int(update_job_manifest.get("attempt_count") or 0)
        write_json_object(s3_client, bucket=downloaded_job.manifest_bucket, key=downloaded_job.manifest_key, payload=update_job_manifest)

    if source_manifest is not None:
        source_manifest["state"] = "exception"
        source_manifest["exception"] = {"code": code, "message": message}
        source_manifest["updated_at"] = timestamp
        write_json_object(
            s3_client,
            bucket=downloaded_job.manifest_bucket,
            key=with_s3_prefix(source_manifest_key(downloaded_job.source_message_id), prefix=prefix),
            payload=source_manifest,
        )


def process_downloaded_job(
    *,
    downloaded_job: DownloadedUpdateJob,
    s3_client: Any,
    config: WorkerConfig,
    opener: Any = request.urlopen,
) -> dict[str, Any]:
    timestamp = utc_now_iso()
    update_job_manifest = read_json_object(s3_client, bucket=downloaded_job.manifest_bucket, key=downloaded_job.manifest_key)
    source_manifest_path = with_s3_prefix(source_manifest_key(downloaded_job.source_message_id), prefix=config.s3_prefix)
    source_manifest = read_json_object_if_exists(s3_client, bucket=downloaded_job.manifest_bucket, key=source_manifest_path)

    if update_job_manifest.get("state") == "completed" and isinstance(update_job_manifest.get("result"), dict):
        return {
            "service": "update-worker",
            "status": "completed",
            "update_job_id": downloaded_job.update_job_id,
            "source_message_id": downloaded_job.source_message_id,
            "result": update_job_manifest.get("result"),
            "idempotent": True,
        }

    update_job_manifest["state"] = "processing"
    update_job_manifest["attempt_count"] = int(update_job_manifest.get("attempt_count") or 0) + 1
    update_job_manifest["updated_at"] = timestamp
    write_json_object(s3_client, bucket=downloaded_job.manifest_bucket, key=downloaded_job.manifest_key, payload=update_job_manifest)

    if source_manifest is not None:
        source_manifest["state"] = "updating"
        source_manifest["current_update_job_id"] = downloaded_job.update_job_id
        source_manifest["current_update_job_key"] = downloaded_job.manifest_key
        source_manifest["updated_at"] = timestamp
        write_json_object(s3_client, bucket=downloaded_job.manifest_bucket, key=source_manifest_path, payload=source_manifest)

    try:
        if config.target_api_base_url:
            target_result = call_target_api(config=config, update_job_manifest=update_job_manifest, opener=opener)
        else:
            target_result = build_s3_only_result(update_job_manifest=update_job_manifest)
        update_job_manifest["state"] = "completed"
        update_job_manifest["result"] = target_result
        update_job_manifest["error"] = None
        update_job_manifest["updated_at"] = timestamp
        write_json_object(s3_client, bucket=downloaded_job.manifest_bucket, key=downloaded_job.manifest_key, payload=update_job_manifest)

        if source_manifest is not None:
            source_manifest["state"] = "updated" if config.target_api_base_url else "approved"
            source_manifest["current_update_job_id"] = downloaded_job.update_job_id
            source_manifest["current_update_job_key"] = downloaded_job.manifest_key
            source_manifest["exception"] = None
            source_manifest["updated_at"] = timestamp
            write_json_object(s3_client, bucket=downloaded_job.manifest_bucket, key=source_manifest_path, payload=source_manifest)

        return {
            "service": "update-worker",
            "status": "completed",
            "update_job_id": downloaded_job.update_job_id,
            "source_message_id": downloaded_job.source_message_id,
            "workflow_session_id": downloaded_job.workflow_session_id,
            "target_result": target_result,
        }
    except Exception as exc:
        error_message = str(exc)
        persist_failure(
            s3_client=s3_client,
            downloaded_job=downloaded_job,
            prefix=config.s3_prefix,
            update_job_manifest=update_job_manifest,
            source_manifest=source_manifest,
            code="UPDATE_PROCESSING_FAILED",
            message=error_message,
            timestamp=utc_now_iso(),
        )
        return {
            "service": "update-worker",
            "status": "failed",
            "update_job_id": downloaded_job.update_job_id,
            "source_message_id": downloaded_job.source_message_id,
            "workflow_session_id": downloaded_job.workflow_session_id,
            "error": {"code": "UPDATE_PROCESSING_FAILED", "message": error_message},
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministic update worker")
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="print resolved worker configuration and exit",
    )
    parser.add_argument(
        "--drain-once",
        action="store_true",
        help="poll SQS one time, fetch matching update job artifacts from S3, print a summary, and exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    config = build_config()

    if args.print_config:
        print(json.dumps(build_runtime_config_log(config), ensure_ascii=False, indent=2))
        return

    if not config.queue_url:
        print("update worker scaffold ready; set UPDATE_WORKER_QUEUE_URL to enable queue consumption")
        return

    queue_client = boto3.client("sqs", region_name=config.aws_region)
    s3_client = boto3.client("s3", region_name=config.aws_region)

    if args.drain_once:
        drained_jobs = poll_queue_once(queue_client=queue_client, s3_client=s3_client, config=config)
        print(
            json.dumps(
                {
                    "service": "update-worker",
                    "status": "drain_complete",
                    "processed_jobs": [asdict(job) for job in drained_jobs],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    stop_event = threading.Event()

    def request_stop(signum: int, frame: object | None) -> None:
        print(f"update-worker received signal {signum}, shutting down")
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    while not stop_event.wait(config.poll_seconds):
        try:
            downloaded_jobs = poll_queue_once(queue_client=queue_client, s3_client=s3_client, config=config)
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "service": "update-worker",
                        "status": "failed",
                        "reason": "queue_fetch_failed",
                        "error": str(exc),
                        "queue_url": config.queue_url,
                    },
                    ensure_ascii=False,
                )
            )
            continue

        if not downloaded_jobs:
            print(
                json.dumps(
                    {
                        "service": "update-worker",
                        "status": "idle",
                        "reason": "no_messages",
                        "queue_url": config.queue_url,
                        "target_api_base_url": config.target_api_base_url,
                    },
                    ensure_ascii=False,
                )
            )
            continue

        for downloaded_job in downloaded_jobs:
            result_payload = process_downloaded_job(downloaded_job=downloaded_job, s3_client=s3_client, config=config)
            print(json.dumps(result_payload, ensure_ascii=False))
            acknowledge_job(queue_client=queue_client, config=config, downloaded_job=downloaded_job)


if __name__ == "__main__":
    main(sys.argv[1:])
