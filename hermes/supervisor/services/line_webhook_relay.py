from __future__ import annotations

import argparse
import json
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, request

from hermes.supervisor.intake_server import LocalStateStore, load_env_file


def copy_request_headers(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in handler.headers.items():
        if key.lower() in {"host", "content-length"}:
            continue
        headers[key] = value
    return headers


def forward_http_request(*, method: str, url: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    upstream_request = request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with request.urlopen(upstream_request, timeout=30) as response:
            return response.status, dict(response.headers.items()), response.read()
    except error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def persist_line_payload(store: LocalStateStore, raw_payload: bytes) -> None:
    payload = json.loads(raw_payload.decode("utf-8"))
    events = payload.get("events")
    if not isinstance(events, list):
        return
    for event in events:
        if isinstance(event, dict):
            store.persist_line_event(event)


def process_line_payload(store: LocalStateStore, raw_payload: bytes) -> dict[str, Any]:
    payload = json.loads(raw_payload.decode("utf-8"))
    events = payload.get("events")
    if not isinstance(events, list):
        raise ValueError("events must be an array")

    processed = [store.persist_line_event(event) for event in events if isinstance(event, dict)]
    return {
        "processed_count": len(processed),
        "new_count": sum(1 for item in processed if not item.deduplicated),
        "duplicate_count": sum(1 for item in processed if item.deduplicated),
        "results": [
            {
                "source_message_id": item.source_message_id,
                "line_event_id": item.line_event_id,
                "state": item.state,
                "deduplicated": item.deduplicated,
                "source_type": item.source_type,
            }
            for item in processed
        ],
    }


def make_handler(
    *,
    store: LocalStateStore,
    upstream_base_url: str,
    proxy_client: Any = forward_http_request,
):
    class RelayHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/line/webhook/health":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            status, headers, body = proxy_client(
                method="GET",
                url=f"{upstream_base_url}{self.path}",
                headers=copy_request_headers(self),
            )
            self._send_upstream_response(status, headers, body)

        def do_POST(self) -> None:
            if self.path != "/line/webhook":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            raw_payload = self.rfile.read(content_length)
            try:
                response_payload = process_line_payload(store, raw_payload)
            except json.JSONDecodeError:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "INVALID_JSON", "message": "request body must be valid json"}},
                )
                return
            except ValueError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "INVALID_REQUEST", "message": str(exc)}},
                )
                return
            except Exception as exc:
                print(f"line relay: unable to process intake payload: {exc}", file=sys.stderr)
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": {"code": "PERSIST_FAILED", "message": str(exc)}},
                )
                return

            self._send_json(HTTPStatus.OK, response_payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_upstream_response(self, status: int, headers: dict[str, str], body: bytes) -> None:
            self.send_response(status)
            for key, value in headers.items():
                if key.lower() in {"connection", "content-length", "transfer-encoding", "date", "server"}:
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return RelayHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Relay LINE webhook traffic to Hermes and persist intake state")
    parser.add_argument(
        "--env-file",
        default=str(Path(__file__).with_name(".env")),
        help="optional .env file to preload before starting the relay",
    )
    parser.add_argument("--host", default=os.environ.get("SUPERVISOR_RELAY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SUPERVISOR_RELAY_PORT", "8646")))
    parser.add_argument(
        "--upstream-base-url",
        default=os.environ.get(
            "SUPERVISOR_HERMES_LINE_UPSTREAM_URL",
            f"http://127.0.0.1:{os.environ.get('HERMES_SUPERVISOR_LINE_PORT', '8647')}",
        ),
        help="Hermes LINE adapter base URL, without a trailing path",
    )
    parser.add_argument(
        "--state-root",
        default=os.environ.get("SUPERVISOR_STATE_ROOT", str(Path("storage") / "local-state")),
        help="local state root used only when SUPERVISOR_STORAGE_BACKEND=local-mock",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_env_file(args.env_file)
    store = LocalStateStore(args.state_root)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store=store, upstream_base_url=args.upstream_base_url.rstrip("/")))
    print(f"line webhook relay listening on http://{args.host}:{args.port} -> {args.upstream_base_url}")
    server.serve_forever()


if __name__ == "__main__":
    main()