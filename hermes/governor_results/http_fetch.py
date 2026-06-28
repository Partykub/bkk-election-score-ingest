from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def browser_like_headers(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": DEFAULT_BROWSER_USER_AGENT,
    }
    if parsed.scheme and parsed.netloc:
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    return headers


def fetch_json_http(url: str, *, timeout_seconds: float) -> Any:
    headers = browser_like_headers(url)
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code != 403:
            raise
        return _fetch_json_with_cloudscraper(
            url,
            timeout_seconds=timeout_seconds,
            original_error=exc,
        )


def _fetch_json_with_cloudscraper(
    url: str,
    *,
    timeout_seconds: float,
    original_error: HTTPError,
) -> Any:
    try:
        import cloudscraper
    except ImportError:
        raise original_error
    scraper = cloudscraper.create_scraper()
    response = scraper.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    return response.json()
