"""Crawl public profiles explicitly linked by a company's official evidence."""
from __future__ import annotations

from typing import Optional

import json
import re
import sqlite3
from urllib.parse import urlparse

import startline_agent  # noqa: F401
from orchestrator import SourceAdapter, ScrapedDoc
from site_adapter import OfficialSiteAdapter
from . import scrape_ladder

PUBLIC_HOSTS = {
    "linkedin.com", "crunchbase.com", "github.com", "facebook.com",
    "instagram.com", "youtube.com", "x.com", "twitter.com",
}
URL_RE = re.compile(r"https?://[^\s|<>\"]+", re.I)


def _allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return any(host == allowed or host.endswith("." + allowed)
               for allowed in PUBLIC_HOSTS)


class PublicSourceAdapter(SourceAdapter):
    deferred_until_discovery = True

    def __init__(self, db_path: str, source_type: str = "directory",
                 hosts: set[str] | None = None, max_profiles: int = 5):
        self.db_path = db_path
        self.source_type = source_type
        self.hosts = hosts or PUBLIC_HOSTS
        self.max_profiles = max_profiles
        self.converter = OfficialSiteAdapter(max_pages=max_profiles)

    def _urls(self, domain: str, company_id: Optional[str] = None) -> list[str]:
        """Profile URLs this company's own evidence links to (see
        OfficialDocumentAdapter._urls for why the scope matters)."""
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        if company_id:
            rows = db.execute(
                "SELECT f.value FROM facts f JOIN verdicts v ON v.fact_id=f.id "
                "WHERE f.company_id=? AND f.field IN ('social_profiles','team_linkedin_urls') "
                "AND v.status IN ('verified','unverified')", (company_id,)).fetchall()
            raw = db.execute(
                "SELECT content FROM raw_documents WHERE company_id=? "
                "AND source_type='official_site'", (company_id,)).fetchall()
        else:
            rows = db.execute(
                "SELECT f.value FROM facts f JOIN verdicts v ON v.fact_id=f.id "
                "JOIN companies c ON c.id=f.company_id WHERE c.domain=? "
                "AND f.field IN ('social_profiles','team_linkedin_urls') "
                "AND v.status IN ('verified','unverified')", (domain,),
            ).fetchall()
            raw = db.execute(
                "SELECT r.content FROM raw_documents r JOIN companies c ON c.id=r.company_id "
                "WHERE c.domain=? AND r.source_type='official_site'", (domain,),
            ).fetchall()
        db.close()
        found = []
        for row in rows:
            try:
                value = json.loads(row["value"])
            except ValueError:
                continue
            items = value if isinstance(value, list) else [value]
            for item in items:
                url = item.get("url") if isinstance(item, dict) else item
                if isinstance(url, str) and self._allowed(url) and url not in found:
                    found.append(url)
        for row in raw:
            for url in URL_RE.findall(row["content"] or ""):
                url = url.rstrip(".,);]")
                if self._allowed(url) and url not in found:
                    found.append(url)
        return found[:self.max_profiles]

    def _allowed(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        return any(host == allowed or host.endswith("." + allowed)
                   for allowed in self.hosts)

    def has_sources(self, domain: str, company_id: Optional[str] = None) -> bool:
        return bool(self._urls(domain, company_id))

    def fetch(self, company_name: str, domain: str | None, hints=None,
              company_id: Optional[str] = None) -> list[ScrapedDoc]:
        if not domain:
            return []
        docs = []
        for url in self._urls(domain, company_id):
            result = scrape_ladder.retrieve_url(url)
            for page in result.pages[:1]:
                docs.append(self.converter._to_doc(page.url, page.html))
        return docs
