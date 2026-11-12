"""Apify API client — run actors and retrieve dataset results."""
import time
import random
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

APIFY_BASE = "https://api.apify.com/v2"
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 4
_JITTER = 0.20
_RUN_POLL_INTERVAL = 10   # seconds between actor run status checks
_RUN_MAX_WAIT = 600       # abort after 10 minutes

# Well-known public actor IDs used by CompanyDataScraper
ACTORS = {
    "linkedin_company":  "2SyF0bVxmgGr8IVCZ",   # LinkedIn Company Scraper
    "linkedin_profiles": "2SyF0bVxmgGr8IVCZ",   # LinkedIn Profile Scraper (same actor)
    "crunchbase":        "cHGxzBHGENcB7eDjt",   # Crunchbase Companies Scraper
    "web_scraper":       "apify/web-scraper",
}


class ApifyError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class ApifyClient:
    """
    Minimal Apify wrapper that:
    1. Starts an actor run via POST
    2. Polls run status until SUCCEEDED/FAILED
    3. Fetches dataset items and returns them as a list
    """

    def __init__(self, api_token: str, timeout: float = 120.0):
        self._token = api_token
        self._timeout = timeout

    def _backoff(self, attempt: int) -> None:
        delay = (_BACKOFF_BASE ** attempt) * (1 + random.uniform(-_JITTER, _JITTER))
        time.sleep(delay)

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        url = APIFY_BASE + path
        p = {"token": self._token}
        if params:
            p.update(params)
        last_exc: Optional[Exception] = None

        for attempt in range(_MAX_ATTEMPTS):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    r = client.get(url, params=p)
                if r.status_code in _RETRY_STATUSES:
                    if attempt < _MAX_ATTEMPTS - 1:
                        self._backoff(attempt)
                    last_exc = ApifyError(f"HTTP {r.status_code}", r.status_code)
                    continue
                if r.status_code >= 400:
                    raise ApifyError(f"Apify {r.status_code}: {r.text[:300]}", r.status_code)
                return r.json()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < _MAX_ATTEMPTS - 1:
                    self._backoff(attempt)
                last_exc = exc

        raise last_exc or ApifyError("Max retries exceeded")

    def _post(self, path: str, payload: dict) -> Any:
        url = APIFY_BASE + path
        last_exc: Optional[Exception] = None

        for attempt in range(_MAX_ATTEMPTS):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    r = client.post(url, json=payload, params={"token": self._token})
                if r.status_code in _RETRY_STATUSES:
                    if attempt < _MAX_ATTEMPTS - 1:
                        self._backoff(attempt)
                    last_exc = ApifyError(f"HTTP {r.status_code}", r.status_code)
                    continue
                if r.status_code >= 400:
                    raise ApifyError(f"Apify {r.status_code}: {r.text[:300]}", r.status_code)
                return r.json()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < _MAX_ATTEMPTS - 1:
                    self._backoff(attempt)
                last_exc = exc

        raise last_exc or ApifyError("Max retries exceeded")

    def run_actor(self, actor_id: str, input_data: dict) -> List[dict]:
        """
        Start an actor run, wait until finished, return dataset items.
        Raises ApifyError on failure or timeout.
        """
        logger.info("Apify: starting actor %s", actor_id)
        resp = self._post(f"/acts/{actor_id}/runs", input_data)
        run_id = (resp.get("data") or {}).get("id")
        if not run_id:
            raise ApifyError(f"No run ID from actor {actor_id}: {resp}")

        # Poll until terminal state
        waited = 0
        while waited < _RUN_MAX_WAIT:
            time.sleep(_RUN_POLL_INTERVAL)
            waited += _RUN_POLL_INTERVAL
            status = self._get(f"/actor-runs/{run_id}")
            state = (status.get("data") or {}).get("status", "")
            logger.debug("Apify run %s → %s (%ds)", run_id, state, waited)

            if state == "SUCCEEDED":
                dataset_id = status["data"]["defaultDatasetId"]
                return self.get_dataset(dataset_id)
            if state in ("FAILED", "ABORTED", "TIMED-OUT"):
                raise ApifyError(f"Actor run {run_id} ended: {state}")

        raise ApifyError(f"Actor run {run_id} timed out after {_RUN_MAX_WAIT}s")

    def get_dataset(self, dataset_id: str, limit: int = 200) -> List[dict]:
        """Fetch and return dataset items as a plain list."""
        result = self._get(f"/datasets/{dataset_id}/items", {"limit": limit, "format": "json"})
        if isinstance(result, list):
            return result
        return result.get("items", [])
