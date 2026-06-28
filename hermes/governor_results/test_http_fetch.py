import json
import sys
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch

from urllib.error import HTTPError

from hermes.governor_results.http_fetch import browser_like_headers, fetch_json_http


class HttpFetchTests(unittest.TestCase):
    def test_browser_like_headers_include_referer_from_url_origin(self):
        headers = browser_like_headers(
            "https://bangkokvote69.bangkok.go.th/results/69-governor-electiondata.json"
        )
        self.assertIn("Mozilla", headers["User-Agent"])
        self.assertEqual(headers["Referer"], "https://bangkokvote69.bangkok.go.th/")
        self.assertEqual(headers["Accept"], "application/json, text/plain, */*")

    @patch("hermes.governor_results.http_fetch.urlopen")
    def test_fetch_json_http_uses_browser_headers(self, mock_urlopen):
        payload = {"type": "LIVE", "districts": []}
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(payload).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        result = fetch_json_http(
            "https://bangkokvote69.bangkok.go.th/results/69-governor-electiondata.json",
            timeout_seconds=5,
        )

        self.assertEqual(result, payload)
        request = mock_urlopen.call_args.args[0]
        self.assertEqual(
            request.headers["Referer"],
            "https://bangkokvote69.bangkok.go.th/",
        )
        self.assertIn("Mozilla", request.headers["User-agent"])

    @patch("hermes.governor_results.http_fetch.urlopen")
    def test_fetch_json_http_falls_back_to_cloudscraper_on_403(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            url="https://example.test/data.json",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=BytesIO(b""),
        )
        scraper = MagicMock()
        scraper.get.return_value.json.return_value = {"ok": True}
        mock_cloudscraper = MagicMock()
        mock_cloudscraper.create_scraper.return_value = scraper

        with patch.dict(sys.modules, {"cloudscraper": mock_cloudscraper}):
            result = fetch_json_http("https://example.test/data.json", timeout_seconds=5)

        self.assertEqual(result, {"ok": True})
        scraper.get.assert_called_once_with("https://example.test/data.json", timeout=5)
