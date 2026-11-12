"""Directory enrichment — the pipeline's second opinion on a company.

The official site is authoritative but frequently silent: a marketing site
often never states where the company is based or what year it started. Company
directories (Tracxn, Craft, Owler, Golden, Wellfound …) publish exactly those
firmographics, and unlike LinkedIn, Crunchbase or Instagram they can actually
be fetched.

Why not those three: LinkedIn's robots.txt is ``Disallow: /`` for every agent
that has not been whitelisted, and its company pages serve an authwall with no
founding date or headquarters in the markup at all; Crunchbase and Instagram
refuse the request outright. A sitemap would not change any of that — the data
is not in the response.

What this adapter is careful about is **identity**. Searching a company name
returns confident-looking profiles for entirely different companies (a search
for "Chatmonster" returns LinkedIn pages for "afterschool-hk" and Crunchbase
pages for "forexus-gmbh"). Attaching those facts to the wrong company is worse
than leaving the field empty, so a candidate page is only accepted if it cites
the company's own domain or its name matches closely. Everything rejected is
recorded, so a thin harvest is explainable rather than mysterious.

Emitted docs carry ``source_type="directory"`` — weight 0.45 in the verifier,
well below the official site. That is the point: a directory is a corroborating
second source, and two independent sources agreeing is what turns a 0.80
constant into a real confidence score.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional
from urllib.parse import urlparse

from orchestrator import SourceAdapter, ScrapedDoc

logger = logging.getLogger(__name__)

MAX_PROFILES = 3              # profiles fetched per company

# Directories that publish firmographics and are actually fetchable. Ordered by
# how reliably they carry founding year / HQ.
DIRECTORY_HOSTS = (
    "tracxn.com", "craft.co", "owler.com", "golden.com", "wellfound.com",
    "angel.co", "pitchbook.com", "cbinsights.com", "zoominfo.com",
    "dnb.com", "bloomberg.com", "rocketreach.co", "leadiq.com",
    "growjo.com", "latka.com", "startupranking.com", "f6s.com",
)

# Fetching these is pointless — measured, not assumed. See the module docstring.
BLOCKED_HOSTS = ("linkedin.com", "crunchbase.com", "instagram.com",
                 "facebook.com", "x.com", "twitter.com")

SEARCH_TEMPLATES = (
    '"{name}" {domain} company profile',
    '"{name}" founded headquarters company profile',
)


def bare_domain(domain: str) -> str:
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0]
    return d.removeprefix("www.")


def _normalize_name(name: str) -> str:
    """Company name reduced to comparable letters.

    Drops legal suffixes and punctuation so "ChatMonster", "Chat Monster" and
    "Chatmonster Ltd." compare equal.
    """
    n = (name or "").lower()
    n = re.sub(r"\b(co\.?,? ?ltd\.?|inc\.?|llc|gmbh|corp\.?|corporation|limited|"
               r"주식회사|\(주\)|유한회사)\b", " ", n)
    return re.sub(r"[^a-z0-9가-힣]", "", n)


class DirectoryAdapter(SourceAdapter):
    """Finds and reads third-party profiles of a company."""

    source_type = "directory"

    def __init__(self, api_key: Optional[str] = None,
                 max_profiles: int = MAX_PROFILES):
        self.api_key = api_key or os.environ.get("HASDATA_API_KEY", "")
        self.max_profiles = max_profiles
        self.last_rejected: list[tuple[str, str]] = []   # (url, why)
        self.rate_limited = False

    # -- discovery -----------------------------------------------------------

    def _client(self):
        from .hasdata_client import HasDataClient
        return HasDataClient(self.api_key)

    def _candidates(self, company_name: str, domain: str) -> list[str]:
        """Search for third-party profiles of this company."""
        client = self._client()
        seen: set[str] = set()
        out: list[str] = []
        for template in SEARCH_TEMPLATES:
            query = template.format(name=company_name, domain=domain or "")
            try:
                res = client.serp_search(query.strip(), num_results=10)
            except Exception as exc:                      # noqa: BLE001
                logger.warning("directory SERP failed (%s): %s", query, exc)
                continue
            results = (res.get("organicResults") or res.get("organic_results")
                       or [])
            for item in results:
                url = (item.get("link") or "").strip()
                if not url:
                    continue
                host = urlparse(url).netloc.lower().removeprefix("www.")
                if host in seen:
                    continue
                if any(b in host for b in BLOCKED_HOSTS):
                    self.last_rejected.append((url, "host blocks scraping"))
                    continue
                if host == bare_domain(domain):
                    continue                              # their own site
                if not any(h in host for h in DIRECTORY_HOSTS):
                    continue                              # not a known directory
                seen.add(host)
                out.append(url)
        return out

    # -- identity ------------------------------------------------------------

    def _is_same_company(self, text: str, company_name: str,
                         domain: str) -> tuple[bool, str]:
        """Strict match: the page must prove it is about this company.

        A directory profile that never mentions the company's domain or its
        name is, as far as this pipeline is concerned, about someone else.
        """
        blob = text.lower()
        bare = bare_domain(domain)
        if bare and bare in blob:
            return True, f"cites {bare}"

        target = _normalize_name(company_name)
        if len(target) >= 4:
            # compare against the page's own leading text, where a profile
            # states whose profile it is
            head = _normalize_name(text[:400])
            if target in head:
                return True, "name matches page heading"
        return False, "no domain reference and no name match"

    # -- fetch ---------------------------------------------------------------

    def fetch(self, company_name: str, domain: Optional[str],
              hints=None,
              company_id: Optional[str] = None) -> list[ScrapedDoc]:
        if not self.api_key:
            logger.info("directory adapter: no HASDATA_API_KEY, skipping")
            return []

        self.last_rejected = []
        self.rate_limited = False
        docs: list[ScrapedDoc] = []
        client = self._client()

        for url in self._candidates(company_name, domain or ""):
            if len(docs) >= self.max_profiles:
                break
            if hints is not None and hints.matches_exclude(url):
                continue
            try:
                res = client.web_scrape(url, render_js=True)
            except Exception as exc:                      # noqa: BLE001
                # A 429 is the account's budget, not this page's fault. Trying
                # the next candidate would spend calls that are already being
                # refused, and "rejected" would misreport why the pass is thin.
                if "429" in str(exc):
                    self.rate_limited = True
                    self.last_rejected.append(
                        (url, "HasData rate limit reached — pass stopped early"))
                    logger.warning("directory: HasData rate limited, stopping pass")
                    break
                self.last_rejected.append((url, f"fetch failed: {exc}"))
                continue

            html = res.get("content") or res.get("html") or ""
            text = self._visible_text(html)
            if len(text) < 300:
                self.last_rejected.append((url, "page had almost no text"))
                continue

            ok, why = self._is_same_company(text, company_name, domain or "")
            if not ok:
                self.last_rejected.append((url, why))
                logger.info("directory: rejected %s (%s)", url, why)
                continue

            docs.append(ScrapedDoc(
                raw_content=(
                    f"PAGE URL: {url}\n"
                    f"SOURCE: third-party company directory "
                    f"({urlparse(url).netloc})\n"
                    f"IDENTITY CHECK: {why}\n\n{text}"
                ),
                source_url=url,
            ))

        if self.last_rejected:
            logger.info("directory: %d candidate(s) rejected for %s",
                        len(self.last_rejected), company_name)
        return docs

    @staticmethod
    def _visible_text(html: str) -> str:
        if not html:
            return ""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
            tag.decompose()
        return " ".join(soup.get_text(" ", strip=True).split())
