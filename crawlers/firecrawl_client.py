"""Firecrawl API client — scrape, crawl, and map endpoints."""
import time
import random
import logging
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

FIRECRAWL_BASE = "https://api.firecrawl.dev/v1"
FIRECRAWL_API_KEY = ""

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 4
_JITTER = 0.20
_CRAWL_POLL_INTERVAL = 5    # seconds between crawl status polls
_CRAWL_MAX_WAIT = 300       # abort crawl after 5 minutes


class FirecrawlError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class FirecrawlClient:
    """
    Firecrawl v1 API wrapper.
    - scrape_url: single page → markdown + HTML + links
    - crawl_site: whole domain → polls until done, returns all pages
    - map_site:   returns list of all URLs on a domain
    """

    def __init__(self, api_key: str = FIRECRAWL_API_KEY, timeout: float = 60.0):
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    def _backoff(self, attempt: int) -> None:
        delay = (_BACKOFF_BASE ** attempt) * (1 + random.uniform(-_JITTER, _JITTER))
        time.sleep(delay)

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        url = FIRECRAWL_BASE + path
        last_exc: Optional[Exception] = None

        for attempt in range(_MAX_ATTEMPTS):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    if method == "GET":
                        r = client.get(url, headers=self._headers)
                    else:
                        r = client.post(url, json=payload or {}, headers=self._headers)

                if r.status_code in _RETRY_STATUSES:
                    logger.warning("Firecrawl %s → %d (attempt %d)", path, r.status_code, attempt + 1)
                    if attempt < _MAX_ATTEMPTS - 1:
                        self._backoff(attempt)
                    last_exc = FirecrawlError(f"HTTP {r.status_code}", r.status_code)
                    continue

                if r.status_code >= 400:
                    raise FirecrawlError(f"Firecrawl {r.status_code}: {r.text[:300]}", r.status_code)

                return r.json()

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                logger.warning("Firecrawl %s network error (attempt %d): %s", path, attempt + 1, exc)
                if attempt < _MAX_ATTEMPTS - 1:
                    self._backoff(attempt)
                last_exc = exc

        raise last_exc or FirecrawlError("Max retries exceeded")

    def scrape_url(
        self,
        url: str,
        formats: Optional[List[str]] = None,
        only_main_content: bool = True,
        extract_schema: Optional[dict] = None,
        extract_prompt: str = "",
    ) -> dict:
        """
        Scrape a single page. Returns markdown, HTML, links, and metadata.
        formats default: ["markdown", "html", "links"]
        Pass extract_schema + extract_prompt to enable Firecrawl's AI extraction.
        """
        logger.info("Firecrawl scrape: %s", url)
        payload: dict = {
            "url": url,
            "formats": formats or ["markdown", "html", "links"],
            "onlyMainContent": only_main_content,
        }
        if extract_schema:
            if "extract" not in payload["formats"]:
                payload["formats"] = list(payload["formats"]) + ["extract"]
            payload["extract"] = {
                "prompt": extract_prompt or "Extract all available company information.",
                "schema": extract_schema,
            }
        return self._request("POST", "/scrape", payload)

    def batch_extract(
        self,
        urls: List[str],
        schema: dict,
        prompt: str = "",
        max_wait: int = 300,
    ) -> List[dict]:
        """
        Run Firecrawl's AI schema-based extraction on multiple URLs concurrently.
        Uses /batch/scrape with formats=["extract"].
        Returns a list of extracted dicts (one per URL, in order); empty dict on failure.
        """
        if not urls:
            return []

        logger.info("Firecrawl batch extract: %d URLs", len(urls))
        payload = {
            "urls": urls,
            "formats": ["extract"],
            "extract": {
                "prompt": prompt or "Extract all available company information.",
                "schema": schema,
            },
        }
        resp = self._request("POST", "/batch/scrape", payload)
        batch_id = resp.get("id")

        # Synchronous response — already has results
        if not batch_id:
            items = resp.get("data") or []
            return [item.get("extract") or {} for item in items]

        # Poll until done
        waited = 0
        poll_interval = 5
        while waited < max_wait:
            time.sleep(poll_interval)
            waited += poll_interval
            status = self._request("GET", f"/batch/scrape/{batch_id}")
            if status.get("status") == "completed":
                items = status.get("data") or []
                return [item.get("extract") or {} for item in items]
            if status.get("status") == "failed":
                raise FirecrawlError(f"Batch {batch_id} failed")
            logger.debug("Batch %s: %s (%ds)", batch_id, status.get("status"), waited)

        raise FirecrawlError(f"Batch {batch_id} timed out after {max_wait}s")

    def crawl_site(
        self,
        url: str,
        max_pages: int = 10,
        include_paths: Optional[List[str]] = None,
        exclude_paths: Optional[List[str]] = None,
    ) -> dict:
        """
        Crawl an entire site. Blocks until the crawl job completes (up to
        _CRAWL_MAX_WAIT seconds) by polling the async crawl status endpoint.
        Returns the completed crawl result object containing all pages.
        """
        payload: dict = {
            "url": url,
            "limit": max_pages,
            "scrapeOptions": {"formats": ["markdown", "links"]},
        }
        if include_paths:
            payload["includePaths"] = include_paths
        if exclude_paths:
            payload["excludePaths"] = exclude_paths

        logger.info("Firecrawl crawl: %s (max %d pages)", url, max_pages)
        resp = self._request("POST", "/crawl", payload)
        crawl_id = resp.get("id")
        if not crawl_id:
            # Synchronous response — Firecrawl returned results immediately
            return resp

        # Poll until done
        waited = 0
        while waited < _CRAWL_MAX_WAIT:
            time.sleep(_CRAWL_POLL_INTERVAL)
            waited += _CRAWL_POLL_INTERVAL
            status = self._request("GET", f"/crawl/{crawl_id}")
            if status.get("status") == "completed":
                return status
            if status.get("status") == "failed":
                raise FirecrawlError(f"Crawl {crawl_id} failed: {status.get('error', 'unknown')}")
            logger.debug("Crawl %s status: %s (%ds elapsed)", crawl_id, status.get("status"), waited)

        raise FirecrawlError(f"Crawl {crawl_id} timed out after {_CRAWL_MAX_WAIT}s")

    def map_site(self, url: str, search: Optional[str] = None) -> dict:
        """
        Returns a flat list of all URLs on the domain.
        Pass search= to filter URLs by keyword (e.g. "pricing" or "team").
        """
        payload: dict = {"url": url}
        if search:
            payload["search"] = search
        logger.info("Firecrawl map: %s (search=%r)", url, search)
        return self._request("POST", "/map", payload)
