"""PitchBook sitemap index.

`https://pitchbook.com/sitemap.xml` is a sitemap index naming ~194 gzipped
children, each holding ~20,000 profile URLs:

    https://pitchbook.com/company-profiles-sitemap-1.xml.gz
      -> https://pitchbook.com/profiles/company/100000-00
         https://pitchbook.com/profiles/company/100000-09
         ...

Fetching them needs a browser *session*, not a browser *render*. Every URL on
pitchbook.com returns 403 to a plain HTTP client, and the crawl ladder cannot
help either -- it renders pages, and a `.gz` is a download rather than a
document. What works is Playwright's in-session request API: load the site once
so Cloudflare issues its `__cf_bm` cookie, then pull each child as bytes over
that same context and gunzip it locally. One browser, ~194 binary fetches.

What the index gives you, stated precisely: PitchBook profile URLs are numeric
(`/profiles/company/100000-00`), not slugs. So unlike the Crunchbase and CB
Insights indexes, this one **cannot map a company name to a URL** -- it is the
universe of valid profile URLs, useful for confirming a URL is real and for
measuring coverage. Finding the profile for a named company still goes through
`PitchBookAdapter.find_profile`, which searches.
"""
from __future__ import annotations

import gzip
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

MASTER_URL = "https://pitchbook.com/sitemap.xml"
WARMUP_URL = "https://pitchbook.com/"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
DEFAULT_FAMILIES = ("company-profiles", "investor-profiles")
INSERT_BATCH = 20_000

_CHILD = re.compile(r"https://pitchbook\.com/([a-z-]+)-sitemap-(\d+)\.xml\.gz", re.I)
_PROFILE = re.compile(r"https://pitchbook\.com/profiles/([a-z-]+)/([\w.-]+)", re.I)

# Shared with the other directory indexes so one company name normalises the
# same way everywhere and the three can be cross-referenced.
from .cbinsights_sitemap import normalize  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS pb_profile (
    kind TEXT NOT NULL,              -- company | investor | fund | advisor | ...
    ref  TEXT NOT NULL,              -- '100000-00'
    -- Filled in once the profile has actually been read. The sitemap gives
    -- only numeric references, but the page states both the company name and
    -- its website, so every scrape teaches the index one more name.
    name TEXT,
    norm TEXT,                       -- match key for the name
    domain TEXT,                     -- the company's own website host
    scraped_at TEXT,
    PRIMARY KEY (kind, ref)
);
CREATE TABLE IF NOT EXISTS pb_sitemap (
    url TEXT PRIMARY KEY,
    family TEXT NOT NULL,
    urls INTEGER,
    status INTEGER,
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS pb_meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Applied only after the columns are known to exist. Kept out of DDL because an
# index built before naming existed has a pb_profile without these columns, and
# CREATE INDEX on a missing column aborts the whole script.
INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_pb_norm ON pb_profile(norm);
CREATE INDEX IF NOT EXISTS idx_pb_domain ON pb_profile(domain);
"""


def profile_url(kind: str, ref: str) -> str:
    return f"https://pitchbook.com/profiles/{kind}/{ref}"


@dataclass
class IngestReport:
    sitemaps_read: int
    sitemaps_total: int
    failed: int
    profiles_seen: int
    seconds: float

    def to_dict(self) -> dict:
        return {"sitemaps_read": self.sitemaps_read,
                "sitemaps_total": self.sitemaps_total, "failed": self.failed,
                "profiles_seen": self.profiles_seen,
                "seconds": round(self.seconds, 1)}


class PitchBookSitemapIndex:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            os.makedirs(os.path.join(root, "data"), exist_ok=True)
            db_path = os.path.join(root, "data", "pitchbook.db")
        self.db_path = db_path
        with sqlite3.connect(self.db_path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(DDL)
            # An index built before profiles could be named lacks these
            # columns; CREATE TABLE IF NOT EXISTS will not add them.
            columns = {row[1] for row in db.execute("PRAGMA table_info(pb_profile)")}
            for name, ddl in (("name", "TEXT"), ("norm", "TEXT"),
                              ("domain", "TEXT"), ("scraped_at", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE pb_profile ADD COLUMN {name} {ddl}")
            db.executescript(INDEX_DDL)
            # Domains recorded before they were stored bare still carry the
            # www. prefix, which lookup_by_domain strips — so they would never
            # match. Cheap and self-healing: the index makes it a range scan
            # and there is nothing to do once they are all clean.
            db.execute("UPDATE pb_profile SET domain=substr(domain,5) "
                       "WHERE domain LIKE 'www.%'")
            db.commit()

    # -- session -------------------------------------------------------------

    class _Session:
        """A warmed Playwright context whose request API carries the CF cookie."""

        def __init__(self):
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self.ctx = self._browser.new_context(user_agent=BROWSER_UA)
            page = self.ctx.new_page()
            page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(6_000)      # let the challenge settle
            page.close()

        def get(self, url: str):
            return self.ctx.request.get(url, headers={"Accept": "*/*"},
                                        timeout=60_000)

        def close(self):
            try:
                self._browser.close()
            finally:
                self._pw.stop()

    @staticmethod
    def _decode(payload: bytes) -> str:
        try:
            return gzip.decompress(payload).decode("utf-8", "ignore")
        except (OSError, EOFError):
            # Some CDNs transparently decompress; take the bytes as-is then.
            return payload.decode("utf-8", "ignore")

    # -- build ---------------------------------------------------------------

    def refresh(self, families: tuple[str, ...] = DEFAULT_FAMILIES,
                progress=None, cancel=None, limit: int | None = None) -> IngestReport:
        started = time.time()
        session = self._Session()
        try:
            master = session.get(MASTER_URL)
            if master.status != 200:
                raise RuntimeError(f"sitemap index returned HTTP {master.status}")
            children = [(url, fam) for url, fam in
                        ((m.group(0), m.group(1))
                         for m in _CHILD.finditer(master.text()))]
            # de-duplicate, keep order
            seen, ordered = set(), []
            for url, fam in children:
                if url not in seen:
                    seen.add(url)
                    ordered.append((url, fam))
            targets = [(u, f) for u, f in ordered if f in families]
            if limit:
                targets = targets[:limit]
            total = len(targets)
            done = failed = stored = 0

            with sqlite3.connect(self.db_path) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=NORMAL")
                db.executescript(DDL)
                db.executemany(
                    "INSERT INTO pb_sitemap (url,family) VALUES (?,?) "
                    "ON CONFLICT(url) DO UPDATE SET family=excluded.family", ordered)
                db.commit()
                pending: list[tuple[str, str]] = []

                def flush():
                    nonlocal pending
                    if pending:
                        db.executemany(
                            "INSERT OR IGNORE INTO pb_profile (kind,ref) VALUES (?,?)",
                            pending)
                        db.commit()
                        pending = []

                for url, family in targets:
                    if cancel and cancel():
                        flush()
                        break
                    count = 0
                    try:
                        response = session.get(url)
                        status = response.status
                        if status == 200:
                            for match in _PROFILE.finditer(self._decode(response.body())):
                                pending.append((match.group(1), match.group(2)))
                                count += 1
                                stored += 1
                        else:
                            failed += 1
                    except Exception as exc:              # noqa: BLE001
                        status = 0
                        failed += 1
                        if progress:
                            progress(done, total,
                                     f"{url.rsplit('/', 1)[-1]}: {type(exc).__name__}")
                    db.execute(
                        "INSERT INTO pb_sitemap (url,family,urls,status,fetched_at) "
                        "VALUES (?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
                        "urls=excluded.urls,status=excluded.status,"
                        "fetched_at=excluded.fetched_at",
                        (url, family, count, status,
                         datetime.now(timezone.utc).isoformat()))
                    if len(pending) >= INSERT_BATCH:
                        flush()
                    done += 1
                    if progress:
                        progress(done, total, url.rsplit("/", 1)[-1])
                flush()
                db.execute(
                    "INSERT INTO pb_meta (key,value) VALUES ('indexed_at',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (datetime.now(timezone.utc).isoformat(),))
                db.commit()
        finally:
            session.close()
        self.compact()
        return IngestReport(done, total, failed, stored, time.time() - started)

    # -- name backfill -------------------------------------------------------

    _NAME = re.compile(r'"@type"\s*:\s*"Corporation".*?"name"\s*:\s*"([^"]{2,120})"', re.S)
    _NAME_ALT = re.compile(r'"name"\s*:\s*"([^"]{2,120})"\s*,\s*"logo"', re.S)
    _SITE = re.compile(r"Website\s+((?:[a-z0-9-]+\.)+[a-z]{2,12})\b", re.I)
    _TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)

    @classmethod
    def _identify(cls, html: str) -> tuple[str | None, str | None]:
        """Pull just the company name and its website out of a profile page."""
        match = cls._NAME.search(html) or cls._NAME_ALT.search(html)
        name = match.group(1).strip() if match else None
        text = re.sub(r"\s+", " ", cls._TAGS.sub(" ", html))
        site = cls._SITE.search(text)
        return name, (site.group(1).lower() if site else None)

    def backfill_names(self, kind: str = "company", limit: int = 200,
                       progress=None, cancel=None) -> dict:
        """Read profiles that have no name yet, purely to learn what they are.

        Bounded on purpose. There are millions of references and each costs a
        browser fetch, so this is an incremental tool the operator runs for as
        long as they want rather than a job that tries to name the whole of
        PitchBook. Progress is committed per page, so stopping loses nothing.
        """
        refs = self.unscraped(kind, limit)
        if not refs:
            return {"attempted": 0, "named": 0, "failed": 0, "remaining": 0}
        started = time.time()
        session = self._Session()
        named = failed = 0
        try:
            for position, ref in enumerate(refs, 1):
                if cancel and cancel():
                    break
                url = profile_url(kind, ref)
                try:
                    response = session.get(url)
                    if response.status != 200:
                        raise RuntimeError(f"HTTP {response.status}")
                    name, domain = self._identify(response.text())
                    if name:
                        self.record_profile(kind, ref, name, domain)
                        named += 1
                    else:
                        # Mark it read so a rerun does not retry it forever.
                        self.record_profile(kind, ref, None, domain)
                        failed += 1
                except Exception:                 # noqa: BLE001
                    failed += 1
                if progress:
                    progress(position, len(refs), f"{ref}: {name if named else '—'}")
        finally:
            session.close()
        with sqlite3.connect(self.db_path) as db:
            remaining = db.execute(
                "SELECT COUNT(*) FROM pb_profile WHERE kind=? AND scraped_at IS NULL",
                (kind,)).fetchone()[0]
        return {"attempted": len(refs), "named": named, "failed": failed,
                "remaining": remaining, "seconds": round(time.time() - started, 1)}

    def compact(self) -> None:
        db = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("VACUUM")
        except sqlite3.OperationalError:
            pass
        finally:
            db.close()

    # -- read ----------------------------------------------------------------

    def counts(self) -> dict:
        with sqlite3.connect(self.db_path) as db:
            try:
                rows = db.execute(
                    "SELECT kind, COUNT(*) FROM pb_profile GROUP BY kind").fetchall()
            except sqlite3.OperationalError:
                return {}
        return dict(rows)

    def named(self) -> int:
        """How many profiles have had their name resolved by a scrape."""
        with sqlite3.connect(self.db_path) as db:
            try:
                return db.execute(
                    "SELECT COUNT(*) FROM pb_profile WHERE norm IS NOT NULL"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                return 0

    def record_profile(self, kind: str, ref: str, name: str | None,
                       domain: str | None) -> None:
        """Remember what a scraped profile turned out to be.

        Called after every successful profile read, so the name index fills in
        as a side effect of ordinary enrichment — no separate crawl needed for
        the companies you actually work with.
        """
        clean_name = (name or "").strip()
        with sqlite3.connect(self.db_path) as db:
            db.executescript(DDL)
            db.execute(
                "INSERT INTO pb_profile (kind,ref,name,norm,domain,scraped_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(kind,ref) DO UPDATE SET "
                "name=excluded.name, norm=excluded.norm, domain=excluded.domain,"
                "scraped_at=excluded.scraped_at",
                (kind, ref, clean_name or None,
                 normalize(clean_name) or None,
                 # Stored bare so it matches lookup_by_domain, which strips the
                 # same prefix — PitchBook prints some sites with www. and some
                 # without.
                 (domain or "").lower().removeprefix("www.") or None,
                 datetime.now(timezone.utc).isoformat()))
            db.commit()

    def unscraped(self, kind: str = "company", limit: int = 100) -> list[str]:
        """Profile refs whose name is not known yet, for a backfill pass."""
        with sqlite3.connect(self.db_path) as db:
            try:
                rows = db.execute(
                    "SELECT ref FROM pb_profile WHERE kind=? AND scraped_at IS NULL "
                    "ORDER BY ref LIMIT ?", (kind, max(1, min(limit, 5000)))).fetchall()
            except sqlite3.OperationalError:
                return []
        return [r[0] for r in rows]

    def lookup(self, company_name: str, kind: str = "company") -> dict | None:
        """Exact normalised-name match among profiles already scraped."""
        key = normalize(company_name)
        if not key:
            return None
        return self._one("norm=? AND kind=?", (key, kind))

    def lookup_by_domain(self, domain: str, kind: str = "company") -> dict | None:
        """Match on the company's own website — the strongest key available."""
        host = (domain or "").lower().removeprefix("www.")
        if not host:
            return None
        return self._one("domain=? AND kind=?", (host, kind))

    def _one(self, where: str, args: tuple) -> dict | None:
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            try:
                row = db.execute(
                    f"SELECT * FROM pb_profile WHERE {where} "
                    "ORDER BY scraped_at DESC LIMIT 1", args).fetchone()
            except sqlite3.OperationalError:
                return None
        if row is None:
            return None
        found = dict(row)
        found["url"] = profile_url(found["kind"], found["ref"])
        return found

    def search(self, term: str, limit: int = 50) -> list[dict]:
        """Prefix search over resolved names, for the console's browser."""
        key = normalize(term)
        if not key:
            return []
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            try:
                rows = db.execute(
                    "SELECT * FROM pb_profile WHERE norm LIKE ? "
                    "ORDER BY length(name) LIMIT ?",
                    (key + "%", max(1, min(limit, 200)))).fetchall()
            except sqlite3.OperationalError:
                return []
        return [{"slug": r["name"] or r["ref"], "kind": r["kind"],
                 "url": profile_url(r["kind"], r["ref"]), "domain": r["domain"]}
                for r in rows]

    def knows(self, url: str) -> bool:
        """Is this a profile URL the sitemap actually lists?"""
        match = _PROFILE.match((url or "").split("?")[0])
        if not match:
            return False
        with sqlite3.connect(self.db_path) as db:
            try:
                row = db.execute(
                    "SELECT 1 FROM pb_profile WHERE kind=? AND ref=?",
                    (match.group(1), match.group(2))).fetchone()
            except sqlite3.OperationalError:
                return False
        return bool(row)

    def browse(self, offset: int = 0, limit: int = 50) -> list[dict]:
        """Named profiles first: a numeric reference tells the reader nothing."""
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            try:
                rows = db.execute(
                    "SELECT * FROM pb_profile "
                    "ORDER BY (name IS NULL), name, ref LIMIT ? OFFSET ?",
                    (max(1, min(limit, 200)), max(0, offset))).fetchall()
            except sqlite3.OperationalError:
                return []
        return [{"name": r["name"] or r["ref"], "kind": r["kind"],
                 "detail": r["domain"] or ("not read yet" if not r["name"] else ""),
                 "url": profile_url(r["kind"], r["ref"])} for r in rows]

    def batch_slice(self, offset: int, limit: int) -> list[dict]:
        """A fixed slice ordered by primary key — see the note in the CB
        Insights index. `browse()` sorts named profiles first, and that order
        changes every time a name is resolved."""
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            try:
                rows = db.execute(
                    "SELECT * FROM pb_profile ORDER BY kind, ref LIMIT ? OFFSET ?",
                    (max(1, limit), max(0, offset))).fetchall()
            except sqlite3.OperationalError:
                return []
        return [{"ref": r["ref"], "name": r["name"] or r["ref"], "kind": r["kind"],
                 "url": profile_url(r["kind"], r["ref"])} for r in rows]

    def total(self) -> int:
        with sqlite3.connect(self.db_path) as db:
            try:
                return db.execute("SELECT COUNT(*) FROM pb_profile").fetchone()[0]
            except sqlite3.OperationalError:
                return 0

    def indexed_at(self) -> str | None:
        with sqlite3.connect(self.db_path) as db:
            try:
                row = db.execute(
                    "SELECT value FROM pb_meta WHERE key='indexed_at'").fetchone()
            except sqlite3.OperationalError:
                return None
        return row[0] if row else None

    def families(self) -> list[dict]:
        with sqlite3.connect(self.db_path) as db:
            try:
                rows = db.execute(
                    "SELECT family, COUNT(*) AS files, "
                    "SUM(CASE WHEN fetched_at IS NOT NULL THEN 1 ELSE 0 END) AS fetched, "
                    "COALESCE(SUM(urls),0) AS urls FROM pb_sitemap "
                    "GROUP BY family ORDER BY files DESC").fetchall()
            except sqlite3.OperationalError:
                return []
        return [{"family": r[0], "files": r[1], "fetched": r[2], "urls": r[3]}
                for r in rows]


if __name__ == "__main__":       # python -m crawlers.pitchbook_sitemap
    index = PitchBookSitemapIndex()
    print(index.refresh(progress=lambda d, t, n: print(f"  {d}/{t} {n}"),
                        limit=3).to_dict())
