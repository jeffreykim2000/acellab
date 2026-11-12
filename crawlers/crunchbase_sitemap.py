"""Crunchbase's published sitemap index — a free, offline slug directory.

Crunchbase serves plain-text sitemaps at

    https://static.crunchbase.com/sitemap-<n>.txt      n = 1, 2, 3, …

Each line is an organization URL. The set is published *for* crawling, so
reading it needs no API credits and no browser, and it answers the two
questions the pipeline otherwise pays Apify to answer:

    1. does a Crunchbase profile exist for this company, and at which slug?
    2. what is the direct URL of its people / funding page?

Every organization appears once per page type:

    /organization/<slug>                       the profile
    /organization/<slug>/people                team    <- Team rows
    /organization/<slug>/funding_financials    funding <- Funding rows
    /organization/<slug>/updates
    /organization/<slug>/technology

Scope, stated plainly: this index is a curated subset, not the whole of
Crunchbase. It resolves the companies it contains for free and says nothing
about the rest — `covers()` reports which, so callers can fall back to the
paid resolver rather than silently treating a miss as "no profile".

Termination: the server answers 403 (not 404) past the last file, so any
non-200 ends the walk. A hard ceiling bounds it regardless.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

SITEMAP_URL = "https://static.crunchbase.com/sitemap-{n}.txt"
MAX_SITEMAPS = 100          # ceiling so a server-side change cannot loop forever
REQUEST_TIMEOUT = 60.0
PAGE_TYPES = ("people", "funding_financials", "updates", "technology")

DDL = """
CREATE TABLE IF NOT EXISTS crunchbase_sitemap (
    slug        TEXT PRIMARY KEY,
    norm        TEXT NOT NULL,          -- match key: lowercase, alphanumeric only
    has_people  INTEGER NOT NULL DEFAULT 0,
    has_funding INTEGER NOT NULL DEFAULT 0,
    indexed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cb_sitemap_norm ON crunchbase_sitemap(norm);
CREATE TABLE IF NOT EXISTS crunchbase_sitemap_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Trailing corporate suffixes carry no identity: "Acme Inc." and "Acme" are the
# same company, and Crunchbase slugs almost never keep them.
_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|llc|llp|plc|"
    r"gmbh|ag|sa|bv|nv|oy|ab|as|pte|pty|kk|kabushiki|holdings?|group|labs?|"
    r"technologies|technology|tech|solutions|systems|ventures|partners)\b",
    re.I,
)


def normalize(name: str) -> str:
    """Collapse a company name or slug to a comparable key."""
    text = re.sub(r"[^\w\s-]", " ", (name or "").lower())
    text = _SUFFIXES.sub(" ", text)
    return re.sub(r"[^a-z0-9]", "", text)


@dataclass
class IngestReport:
    sitemaps_read: int
    urls_seen: int
    organizations: int
    with_people: int
    with_funding: int
    seconds: float

    def to_dict(self) -> dict:
        return {"sitemaps_read": self.sitemaps_read, "urls_seen": self.urls_seen,
                "organizations": self.organizations, "with_people": self.with_people,
                "with_funding": self.with_funding, "seconds": round(self.seconds, 1)}


class CrunchbaseSitemapIndex:
    """Local index of Crunchbase organization slugs and their sub-pages."""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            os.makedirs(os.path.join(root, "data"), exist_ok=True)
            db_path = os.path.join(root, "data", "slug_cache.db")
        self.db_path = db_path
        with sqlite3.connect(self.db_path) as db:
            db.executescript(DDL)

    # -- build ---------------------------------------------------------------

    def refresh(self, log=None, max_sitemaps: int = MAX_SITEMAPS) -> IngestReport:
        """Walk sitemap-1..N and rebuild the index. Safe to re-run."""
        def _log(msg: str) -> None:
            if log:
                log(msg)

        started = time.time()
        # slug -> [has_people, has_funding]
        found: dict[str, list[int]] = {}
        urls = read = 0

        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": "StartlineBot/0.1"}) as client:
            for n in range(1, max_sitemaps + 1):
                try:
                    response = client.get(SITEMAP_URL.format(n=n))
                except httpx.HTTPError as exc:
                    _log(f"sitemap-{n}: network error ({type(exc).__name__}), stopping")
                    break
                if response.status_code != 200:
                    # 403 is what the CDN returns past the last file.
                    _log(f"sitemap-{n}: HTTP {response.status_code}, end of range")
                    break
                read += 1
                lines = response.text.splitlines()
                urls += len(lines)
                for line in lines:
                    parts = line.strip().split("/")
                    if len(parts) < 5 or parts[3] != "organization" or not parts[4]:
                        continue
                    entry = found.setdefault(parts[4], [0, 0])
                    page = parts[5] if len(parts) > 5 else ""
                    if page == "people":
                        entry[0] = 1
                    elif page == "funding_financials":
                        entry[1] = 1
                _log(f"sitemap-{n}: {len(lines):,} URLs, {len(found):,} organizations so far")

        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as db:
            db.executescript(DDL)
            db.executemany(
                "INSERT INTO crunchbase_sitemap (slug,norm,has_people,has_funding,indexed_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET "
                "norm=excluded.norm, has_people=excluded.has_people, "
                "has_funding=excluded.has_funding, indexed_at=excluded.indexed_at",
                [(slug, normalize(slug), flags[0], flags[1], now)
                 for slug, flags in found.items()],
            )
            db.execute(
                "INSERT INTO crunchbase_sitemap_meta (key,value) VALUES ('refreshed_at',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now,))
            db.commit()

        report = IngestReport(
            sitemaps_read=read, urls_seen=urls, organizations=len(found),
            with_people=sum(1 for f in found.values() if f[0]),
            with_funding=sum(1 for f in found.values() if f[1]),
            seconds=time.time() - started)
        _log(f"indexed {report.organizations:,} organizations "
             f"({report.with_people:,} with a people page)")
        return report

    # -- read ----------------------------------------------------------------

    def _row(self, name: str):
        key = normalize(name)
        if not key:
            return None
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            # Exact normalized match only. A prefix or fuzzy match here would
            # confidently return the wrong company, and a wrong slug is worse
            # than no slug: it produces verified facts about someone else.
            return db.execute(
                "SELECT * FROM crunchbase_sitemap WHERE norm=? "
                "ORDER BY length(slug) LIMIT 1", (key,)).fetchone()

    def lookup(self, company_name: str) -> dict | None:
        """Resolve a company name to its slug and sub-page URLs, or None."""
        row = self._row(company_name)
        if row is None:
            return None
        base = f"https://www.crunchbase.com/organization/{row['slug']}"
        return {
            "slug": row["slug"],
            "crunchbase_url": base,
            "people_url": f"{base}/people" if row["has_people"] else None,
            "funding_url": f"{base}/funding_financials" if row["has_funding"] else None,
            "resolution_method": "sitemap_index",
        }

    def search(self, term: str, limit: int = 50) -> list[dict]:
        """Prefix search over the normalised names, for the console's browser."""
        key = normalize(term)
        if not key:
            return []
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM crunchbase_sitemap WHERE norm LIKE ? "
                "ORDER BY length(slug) LIMIT ?",
                (key + "%", max(1, min(limit, 200)))).fetchall()
        out = []
        for row in rows:
            base = f"https://www.crunchbase.com/organization/{row['slug']}"
            out.append({
                "slug": row["slug"], "kind": "company", "url": base,
                "people_url": f"{base}/people" if row["has_people"] else None,
                "funding_url": (f"{base}/funding_financials"
                                if row["has_funding"] else None),
            })
        return out

    def browse(self, offset: int = 0, limit: int = 50) -> list[dict]:
        """A page of the index, for the console's browse table."""
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM crunchbase_sitemap ORDER BY slug LIMIT ? OFFSET ?",
                (max(1, min(limit, 200)), max(0, offset))).fetchall()
        out = []
        for row in rows:
            base = f"https://www.crunchbase.com/organization/{row['slug']}"
            out.append({"name": row["slug"], "kind": "company", "url": base,
                        "detail": "",
                        "people_url": f"{base}/people" if row["has_people"] else None,
                        "funding_url": (f"{base}/funding_financials"
                                        if row["has_funding"] else None)})
        return out

    def covers(self) -> int:
        """How many organizations the index currently holds (0 = never built)."""
        with sqlite3.connect(self.db_path) as db:
            try:
                return db.execute("SELECT COUNT(*) FROM crunchbase_sitemap").fetchone()[0]
            except sqlite3.OperationalError:
                return 0

    def refreshed_at(self) -> str | None:
        with sqlite3.connect(self.db_path) as db:
            try:
                row = db.execute("SELECT value FROM crunchbase_sitemap_meta "
                                 "WHERE key='refreshed_at'").fetchone()
            except sqlite3.OperationalError:
                return None
        return row[0] if row else None


if __name__ == "__main__":       # python -m crawlers.crunchbase_sitemap
    index = CrunchbaseSitemapIndex()
    print(index.refresh(log=print).to_dict())
