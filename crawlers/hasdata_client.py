"""HasData API client — SERP, Maps, and Web scraping endpoints."""
import time
import random
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

HASDATA_BASE = "https://api.hasdata.com"
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 4      # seconds: 4s, 16s, 64s
_JITTER = 0.20         # ±20% of calculated backoff


def _backoff(attempt: int) -> None:
    delay = (_BACKOFF_BASE ** attempt) * (1 + random.uniform(-_JITTER, _JITTER))
    time.sleep(delay)


class HasDataError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class HasDataClient:
    """
    Thin wrapper around the HasData REST API with retry logic.
    All methods return the parsed JSON body on success, raise HasDataError on failure.
    """

    def __init__(self, api_key: str, timeout: float = 60.0):
        self._headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        self._timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        url = HASDATA_BASE + path
        last_exc: Optional[Exception] = None

        for attempt in range(_MAX_ATTEMPTS):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    r = client.post(url, json=payload, headers=self._headers)

                if r.status_code in _RETRY_STATUSES:
                    logger.warning("HasData %s → %d (attempt %d)", path, r.status_code, attempt + 1)
                    if attempt < _MAX_ATTEMPTS - 1:
                        _backoff(attempt)
                    last_exc = HasDataError(f"HTTP {r.status_code}", r.status_code)
                    continue

                if r.status_code >= 400:
                    raise HasDataError(f"HasData {r.status_code}: {r.text[:300]}", r.status_code)

                return r.json()

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                logger.warning("HasData %s network error (attempt %d): %s", path, attempt + 1, exc)
                if attempt < _MAX_ATTEMPTS - 1:
                    _backoff(attempt)
                last_exc = exc

        raise last_exc or HasDataError("Max retries exceeded")

    def serp_search(self, query: str, num_results: int = 10) -> dict:
        """
        Google SERP — best for discovering official website and knowledge-panel
        fields (founded year, HQ, employee count, industry).
        Returns full HasData SERP response: organic_results, knowledge_graph, etc.
        """
        return self._post("/scrape/google/serp", {
            "q": query, "num": num_results, "gl": "us", "hl": "en",
        })

    def maps_search(self, query: str, location: str = "") -> dict:
        """
        Google Maps — highest confidence for address, phone, and website.
        Returns: {localResults: [{title, address, phone, website, rating, ...}]}
        Note: requires Maps access on the HasData plan.
        """
        keyword = f"{query} {location}".strip() if location else query
        result = self._post("/scrape/google/maps", {
            "keyword": keyword,
            "country": "US",
            "domain":  "com",
        })
        # Normalise response: support both camelCase and snake_case key variants
        local = result.get("localResults") or result.get("local_results") or []
        return {"local_results": local, "_raw": result}

    def web_scrape(self, url: str, render_js: bool = False) -> dict:
        """
        Raw page scrape. render_js=True uses headless browser for JS-heavy pages.
        Returns: {content: "<html>...", status_code: 200}
        """
        return self._post("/scrape/web", {
            "url": url, "proxyType": "datacenter", "renderJs": render_js,
        })
