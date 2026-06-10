from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request

import boto3


def safe_id(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value).strip("_") or "unknown"


def object_key_for(*, source_message_id: str, received_at: str) -> str:
    return f"inbound/{source_message_id}/original.bin"


def build_metadata_payload(manifest: dict[str, Any], *, upload_session_id: str, received_at: str, storage_backend: str) -> dict[str, Any]:
    return {
        "upload_session_id": upload_session_id,
        "source_message_id": manifest["source_message_id"],
        "platform": manifest["platform"],
        "line_event_id": manifest["line_event_id"],
        "line_message_id": manifest["line_message_id"],
        "sender_user_id": manifest["sender_user_id"],
        "sender_group_id": manifest["sender_group_id"],
        "sender_room_id": manifest["sender_room_id"],
        "content_type": "image/jpeg",
        "size_bytes": None,
        "object_etag": None,
        "received_at": received_at,
        "storage_backend": storage_backend,
        "binary_status": "pending_line_content_fetch",
    }


@dataclass(frozen=True)
class LineMessageContent:
    body: bytes
    content_type: str


@dataclass(frozen=True)
class UploadSession:
    upload_session_id: str
    bucket: str
    object_key: str
    metadata_key: str
    source_message_manifest_key: str
    state: str
    storage_backend: str


class UploadServiceError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, response_body: Any | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class LocalMockUploadService:
    def __init__(
        self,
        root_path: str | Path,
        *,
        bucket_name: str = "local-election-system",
        line_content_fetcher: Any | None = None,
    ) -> None:
        self.root_path = Path(root_path)
        self.bucket_name = bucket_name
        self.line_content_fetcher = line_content_fetcher

    def store_source_message(self, manifest: dict[str, Any], *, received_at: str) -> UploadSession:
        source_message_id = manifest["source_message_id"]
        upload_session_id = f"upl_{safe_id(source_message_id)}"
        object_key = object_key_for(source_message_id=source_message_id, received_at=received_at)
        metadata_key = object_key.replace("/original.bin", "/metadata.json")
        source_message_manifest_key = f"manifests/source-messages/{source_message_id}.json"

        metadata = build_metadata_payload(
            manifest,
            upload_session_id=upload_session_id,
            received_at=received_at,
            storage_backend="local-mock",
        )

        line_content = self._fetch_line_content(manifest)
        if line_content is not None:
            metadata["content_type"] = line_content.content_type
            metadata["size_bytes"] = len(line_content.body)
            metadata["binary_status"] = "stored"
            self._write_bytes(object_key, line_content.body)

        self._write_json(metadata_key, metadata)

        return UploadSession(
            upload_session_id=upload_session_id,
            bucket=self.bucket_name,
            object_key=object_key,
            metadata_key=metadata_key,
            source_message_manifest_key=source_message_manifest_key,
            state="stored",
            storage_backend="local-mock",
        )

    def _write_json(self, relative_path: str, payload: dict[str, Any]) -> None:
        target_path = self.root_path / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _write_bytes(self, relative_path: str, body: bytes) -> None:
        target_path = self.root_path / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(body)

    def _fetch_line_content(self, manifest: dict[str, Any]) -> LineMessageContent | None:
        if self.line_content_fetcher is None:
            return None
        line_message_id = manifest.get("line_message_id")
        if not line_message_id:
            return None
        return self.line_content_fetcher(line_message_id)


class S3UploadService:
    def __init__(
        self,
        *,
        bucket_name: str,
        region_name: str | None = None,
        endpoint_url: str | None = None,
        key_prefix: str = "",
        client: Any | None = None,
        line_content_fetcher: Any | None = None,
    ) -> None:
        self.bucket_name = bucket_name
        self.region_name = region_name
        self.endpoint_url = endpoint_url
        self.key_prefix = key_prefix.strip("/")
        self.client = client or boto3.client("s3", region_name=region_name, endpoint_url=endpoint_url)
        self.line_content_fetcher = line_content_fetcher

    def store_source_message(self, manifest: dict[str, Any], *, received_at: str) -> UploadSession:
        source_message_id = manifest["source_message_id"]
        upload_session_id = f"upl_{safe_id(source_message_id)}"
        object_key = self._with_prefix(object_key_for(source_message_id=source_message_id, received_at=received_at))
        metadata_key = object_key.replace("/original.bin", "/metadata.json")
        source_message_manifest_key = self._with_prefix(f"manifests/source-messages/{source_message_id}.json")
        metadata = build_metadata_payload(
            manifest,
            upload_session_id=upload_session_id,
            received_at=received_at,
            storage_backend="s3",
        )

        try:
            line_content = self._fetch_line_content(manifest)
            if line_content is not None:
                binary_response = self.client.put_object(
                    Bucket=self.bucket_name,
                    Key=object_key,
                    Body=line_content.body,
                    ContentType=line_content.content_type,
                )
                metadata["content_type"] = line_content.content_type
                metadata["size_bytes"] = len(line_content.body)
                metadata["object_etag"] = binary_response.get("ETag")
                metadata["binary_status"] = "stored"

            self.client.put_object(
                Bucket=self.bucket_name,
                Key=metadata_key,
                Body=json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
                ContentType="application/json",
            )
        except Exception as exc:
            raise UploadServiceError(f"unable to write metadata to s3://{self.bucket_name}/{metadata_key}") from exc

        return UploadSession(
            upload_session_id=upload_session_id,
            bucket=self.bucket_name,
            object_key=object_key,
            metadata_key=metadata_key,
            source_message_manifest_key=source_message_manifest_key,
            state="stored",
            storage_backend="s3",
        )

    def _with_prefix(self, relative_key: str) -> str:
        if not self.key_prefix:
            return relative_key
        return f"{self.key_prefix}/{relative_key}"

    def _fetch_line_content(self, manifest: dict[str, Any]) -> LineMessageContent | None:
        if self.line_content_fetcher is None:
            return None
        line_message_id = manifest.get("line_message_id")
        if not line_message_id:
            return None
        return self.line_content_fetcher(line_message_id)


def build_line_content_fetcher_from_env() -> Any | None:
    channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not channel_access_token:
        return None

    def fetch_line_message_content(line_message_id: str) -> LineMessageContent:
        url = f"https://api-data.line.me/v2/bot/message/{line_message_id}/content"
        line_request = request.Request(
            url,
            headers={"Authorization": f"Bearer {channel_access_token}"},
            method="GET",
        )

        try:
            with request.urlopen(line_request, timeout=30) as response:
                content_type = response.headers.get_content_type() or "application/octet-stream"
                return LineMessageContent(body=response.read(), content_type=content_type)
        except error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            raise UploadServiceError(
                f"unable to fetch line message content for {line_message_id}",
                status_code=exc.code,
                response_body=response_body,
            ) from exc
        except error.URLError as exc:
            raise UploadServiceError(f"unable to reach line content api for {line_message_id}: {exc.reason}") from exc

    return fetch_line_message_content


def build_upload_service(root_path: str | Path) -> LocalMockUploadService | S3UploadService:
    backend = os.environ.get("SUPERVISOR_STORAGE_BACKEND", "local-mock").strip().lower()
    line_content_fetcher = build_line_content_fetcher_from_env()
    if backend == "s3":
        bucket_name = os.environ.get("SUPERVISOR_S3_BUCKET", "").strip()
        if not bucket_name:
            raise UploadServiceError("SUPERVISOR_S3_BUCKET is required when SUPERVISOR_STORAGE_BACKEND=s3")
        region_name = os.environ.get("SUPERVISOR_S3_REGION", "").strip() or None
        endpoint_url = os.environ.get("SUPERVISOR_S3_ENDPOINT", "").strip() or None
        key_prefix = os.environ.get("SUPERVISOR_S3_PREFIX", "").strip()
        return S3UploadService(
            bucket_name=bucket_name,
            region_name=region_name,
            endpoint_url=endpoint_url,
            key_prefix=key_prefix,
            line_content_fetcher=line_content_fetcher,
        )

    return LocalMockUploadService(root_path, line_content_fetcher=line_content_fetcher)